"""ETL pipeline: discover raw cycler exports, normalize them, load them.

The pipeline is a full reload per test: re-running it replaces a test's rows
rather than appending to them, so `docker compose up` on a persisted PostgreSQL
volume is safe to repeat.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.db import Database
from app.etl.contract import (
    REQUIRED_FIELDS,
    VALUE_FIELDS,
    build_test_id,
    infer_cycler,
    is_usable_row,
    normalize_timeseries_row,
)

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = frozenset({".csv", ".txt"})

#: Tab-separated for BioLogic, comma-separated for the others.
SEPARATORS = {".txt": "\t", ".csv": ","}

#: Rows per executemany call, so a very large export does not build one huge
#: parameter list.
INSERT_BATCH_SIZE = 5_000

#: Per-file cap on individual skipped-row warnings; the total is always reported.
MAX_SKIP_WARNINGS_PER_FILE = 5


def discover_files(root: Path) -> Iterator[Path]:
    """Yield supported data files under ``root`` in a deterministic order."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path


def read_raw_rows(path: Path) -> list[dict[str, Any]]:
    """Read a delimited export into a list of raw row dicts."""
    import pandas as pd

    separator = SEPARATORS.get(path.suffix.lower(), ",")
    frame = pd.read_csv(
        path,
        sep=separator,
        encoding="utf-8",
        # The BioLogic export already contains a corrupted degree sign; never
        # let an encoding error abort the whole run.
        encoding_errors="replace",
    )
    # to_dict("records") preserves per-column dtypes and is far faster than
    # iterrows(), which builds a Series per row.
    return frame.to_dict("records")


def normalize_file(path: Path, cycler: str, test_id: str) -> tuple[list[dict[str, Any]], Counter]:
    """Normalize one file into storable rows plus a counter of what happened.

    Applies, in order: per-row normalization, dropping unusable rows,
    de-duplication, sorting by time, and rebasing time to the start of the test.
    """
    stats: Counter = Counter()
    raw_rows = read_raw_rows(path)
    stats["rows_read"] = len(raw_rows)

    usable: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        row = normalize_timeseries_row(raw_row, cycler=cycler, test_id=test_id)

        if not is_usable_row(row):
            stats["rows_skipped"] += 1
            if stats["rows_skipped"] <= MAX_SKIP_WARNINGS_PER_FILE:
                missing = [field for field in REQUIRED_FIELDS if row.get(field) is None]
                logger.warning("Skipping row in %s: missing %s", path, ", ".join(missing))
            continue

        usable.append(row)

    if stats["rows_skipped"] > MAX_SKIP_WARNINGS_PER_FILE:
        logger.warning(
            "Skipped %s rows in total for %s (%s warnings suppressed)",
            stats["rows_skipped"],
            test_id,
            stats["rows_skipped"] - MAX_SKIP_WARNINGS_PER_FILE,
        )

    # Exact duplicate samples appear in some exports (neware cell_003 repeats 20
    # rows, including their timestamps). Identical timestamps are a defect, not
    # a real re-measurement, so the repeats are dropped.
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for row in usable:
        key = tuple(row[field] for field in VALUE_FIELDS)
        if key in seen:
            stats["rows_duplicated"] += 1
            continue
        seen.add(key)
        deduplicated.append(row)

    # A few files are not time-ordered; sorting makes `ORDER BY id` in the API
    # equivalent to time order and keeps per-cycle start/end times correct.
    deduplicated.sort(key=lambda row: row["timestamp_s"])

    # `timestamp_s` is defined as seconds since the start of the test, but the
    # neware exports share one lab clock across files (cell_010 starts near
    # 540,277 s). Rebase each test to zero and keep the original offset on the
    # `tests` row so nothing is lost.
    if deduplicated:
        offset = deduplicated[0]["timestamp_s"]
        stats["start_offset_s"] = offset
        if offset:
            for row in deduplicated:
                row["timestamp_s"] -= offset

    stats["rows_loaded"] = len(deduplicated)
    stats["rows_rescaled"] = sum(
        1 for row in deduplicated if "voltage_rescaled_from_mv" in row["quality_flags"]
    )
    return deduplicated, stats


def _store_test(db: Database, test_id: str, cycler: str, path: Path, rows, stats: Counter) -> None:
    """Replace all stored data for one test inside the current transaction."""
    cycle_count = len({row["cycle_index"] for row in rows if row["cycle_index"] is not None})

    db.execute(
        """
        INSERT INTO tests (
            test_id, cycler, source_path, start_offset_s,
            rows_loaded, rows_skipped, rows_duplicated, rows_rescaled,
            first_timestamp_s, last_timestamp_s, cycle_count, ingested_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (test_id) DO UPDATE SET
            cycler = EXCLUDED.cycler,
            source_path = EXCLUDED.source_path,
            start_offset_s = EXCLUDED.start_offset_s,
            rows_loaded = EXCLUDED.rows_loaded,
            rows_skipped = EXCLUDED.rows_skipped,
            rows_duplicated = EXCLUDED.rows_duplicated,
            rows_rescaled = EXCLUDED.rows_rescaled,
            first_timestamp_s = EXCLUDED.first_timestamp_s,
            last_timestamp_s = EXCLUDED.last_timestamp_s,
            cycle_count = EXCLUDED.cycle_count,
            ingested_at = EXCLUDED.ingested_at
        """,
        (
            test_id,
            cycler,
            str(path),
            stats.get("start_offset_s", 0.0),
            stats["rows_loaded"],
            stats["rows_skipped"],
            stats["rows_duplicated"],
            stats["rows_rescaled"],
            rows[0]["timestamp_s"] if rows else None,
            rows[-1]["timestamp_s"] if rows else None,
            cycle_count,
            datetime.now(UTC).isoformat(timespec="seconds"),
        ),
    )

    # Makes re-ingestion idempotent instead of doubling every row.
    db.execute("DELETE FROM timeseries WHERE test_id = ?", (test_id,))

    statement = """
        INSERT INTO timeseries (
            test_id, timestamp_s, voltage_v, current_a, temperature_c, capacity_ah, cycle_index
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    for start in range(0, len(rows), INSERT_BATCH_SIZE):
        db.executemany(
            statement,
            [
                (
                    row["test_id"],
                    row["timestamp_s"],
                    row["voltage_v"],
                    row["current_a"],
                    row["temperature_c"],
                    row["capacity_ah"],
                    row["cycle_index"],
                )
                for row in rows[start : start + INSERT_BATCH_SIZE]
            ],
        )


def ingest_directory(
    root: str | Path,
    db_path: str | Path | None = None,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Ingest every supported file under ``root`` into SQLite or PostgreSQL.

    Returns a summary of what was loaded, skipped, de-duplicated and repaired.
    """
    root_path = Path(root)
    if db_path is None:
        db_path = os.getenv("BATTERY_DB_PATH") or root_path / "battery.sqlite3"

    summary: Counter = Counter()
    skipped_files: list[str] = []

    with Database.connect(database_url=database_url, db_path=db_path) as db:
        db.ensure_schema()

        for path in discover_files(root_path):
            summary["files_discovered"] += 1

            cycler = infer_cycler(path)
            if cycler == "unknown":
                # Not inside a cycler_* directory, so there is no reliable way to
                # pick a parser or build a unique test ID.
                logger.warning("Skipping %s: not inside a cycler_* directory", path)
                summary["files_skipped"] += 1
                skipped_files.append(str(path))
                continue

            test_id = build_test_id(path, cycler=cycler)

            try:
                rows, stats = normalize_file(path, cycler=cycler, test_id=test_id)
            except Exception:
                # One unreadable file must not abort the whole run.
                logger.exception("Failed to parse %s; skipping the file", path)
                summary["files_skipped"] += 1
                skipped_files.append(str(path))
                continue

            summary["rows_skipped"] += stats["rows_skipped"]
            summary["rows_duplicated"] += stats["rows_duplicated"]
            summary["rows_rescaled"] += stats["rows_rescaled"]

            if not rows:
                logger.warning("No usable rows in %s; not creating a test record", path)
                summary["files_skipped"] += 1
                skipped_files.append(str(path))
                continue

            _store_test(db, test_id, cycler, path, rows, stats)
            summary["tests_loaded"] += 1
            summary["rows_loaded"] += stats["rows_loaded"]
            logger.info(
                "Loaded %s rows for %s (skipped %s, duplicates %s, rescaled %s)",
                stats["rows_loaded"],
                test_id,
                stats["rows_skipped"],
                stats["rows_duplicated"],
                stats["rows_rescaled"],
            )

        db.commit()

    result: dict[str, Any] = {
        "files_discovered": summary["files_discovered"],
        "files_skipped": summary["files_skipped"],
        "tests_loaded": summary["tests_loaded"],
        "rows_loaded": summary["rows_loaded"],
        "rows_skipped": summary["rows_skipped"],
        "rows_duplicated": summary["rows_duplicated"],
        "rows_rescaled": summary["rows_rescaled"],
    }
    if skipped_files:
        result["skipped_file_paths"] = skipped_files
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest raw battery cycler exports into the database.",
    )
    parser.add_argument(
        "--data-root",
        default=os.getenv("BATTERY_DATA_ROOT", "data"),
        help="Directory containing the cycler_* folders (default: data)",
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("BATTERY_DB_PATH"),
        help="SQLite file to write when DATABASE_URL is not set",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL for this run",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    summary = ingest_directory(
        args.data_root,
        db_path=args.db_path,
        database_url=args.database_url,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
