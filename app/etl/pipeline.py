from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import psycopg2

from app.db import build_database_url, connect_database
from app.etl.contract import build_test_id, normalize_timeseries_row

logger = logging.getLogger(__name__)


def ingest_directory(root: str | Path, db_path: str | Path | None = None) -> dict[str, int]:
    """Ingest all supported files under a data root into a SQLite or PostgreSQL database."""
    root_path = Path(root)
    if db_path is None:
        db_path = root_path / "battery.sqlite3"

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    database_url = build_database_url(db_path=str(db_path))
    if database_url.startswith("postgresql://"):
        conn = connect_database(database_url)
        conn.autocommit = True
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tests (
                        test_id TEXT PRIMARY KEY,
                        cycler TEXT NOT NULL,
                        source_path TEXT NOT NULL
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS timeseries (
                        id BIGSERIAL PRIMARY KEY,
                        test_id TEXT NOT NULL REFERENCES tests(test_id),
                        timestamp_s DOUBLE PRECISION,
                        voltage_v DOUBLE PRECISION,
                        current_a DOUBLE PRECISION,
                        temperature_c DOUBLE PRECISION,
                        cycle_index INTEGER
                    )
                    """
                )
        finally:
            conn.close()
    else:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tests (
                    test_id TEXT PRIMARY KEY,
                    cycler TEXT NOT NULL,
                    source_path TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS timeseries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    test_id TEXT NOT NULL,
                    timestamp_s REAL,
                    voltage_v REAL,
                    current_a REAL,
                    temperature_c REAL,
                    cycle_index INTEGER,
                    FOREIGN KEY(test_id) REFERENCES tests(test_id)
                )
                """
            )

    if database_url.startswith("postgresql://"):
        conn = connect_database(database_url)
        try:
            with conn.cursor() as cursor:
                loaded_tests = 0
                rows_loaded = 0
                rows_skipped = 0

                for path in sorted(root_path.rglob("*")):
                    if not path.is_file():
                        continue
                    if path.suffix.lower() not in {".csv", ".txt"}:
                        continue

                    cycler = infer_cycler(path)
                    test_id = build_test_id(path, cycler=cycler)
                    rows = load_rows(path, cycler=cycler, test_id=test_id)
                    if not rows:
                        continue

                    valid_rows = []
                    for row in rows:
                        if not is_usable_row(row):
                            rows_skipped += 1
                            logger.warning(
                                "Skipping row for %s from %s because required values are missing",
                                row.get("test_id"),
                                path,
                            )
                            continue
                        valid_rows.append(row)

                    if not valid_rows:
                        continue

                    cursor.execute(
                        "INSERT INTO tests (test_id, cycler, source_path) VALUES (%s, %s, %s) ON CONFLICT (test_id) DO UPDATE SET cycler = EXCLUDED.cycler, source_path = EXCLUDED.source_path",
                        (test_id, cycler, str(path)),
                    )
                    cursor.executemany(
                        """
                        INSERT INTO timeseries (test_id, timestamp_s, voltage_v, current_a, temperature_c, cycle_index)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                row["test_id"],
                                row["timestamp_s"],
                                row["voltage_v"],
                                row["current_a"],
                                row["temperature_c"],
                                row["cycle_index"],
                            )
                            for row in valid_rows
                        ],
                    )
                    loaded_tests += 1
                    rows_loaded += len(valid_rows)

                conn.commit()
        finally:
            conn.close()

        return {"tests_loaded": loaded_tests, "rows_loaded": rows_loaded, "rows_skipped": rows_skipped}

    with sqlite3.connect(db_path) as conn:
        loaded_tests = 0
        rows_loaded = 0
        rows_skipped = 0

        for path in sorted(root_path.rglob("*")):
            if not path.is_file():
                continue

            if path.suffix.lower() not in {".csv", ".txt"}:
                continue

            cycler = infer_cycler(path)
            test_id = build_test_id(path, cycler=cycler)

            rows = load_rows(path, cycler=cycler, test_id=test_id)
            if not rows:
                continue

            valid_rows = []
            for row in rows:
                if not is_usable_row(row):
                    rows_skipped += 1
                    logger.warning(
                        "Skipping row for %s from %s because required values are missing",
                        row.get("test_id"),
                        path,
                    )
                    continue
                valid_rows.append(row)

            if not valid_rows:
                continue

            conn.execute(
                "INSERT OR REPLACE INTO tests (test_id, cycler, source_path) VALUES (?, ?, ?)",
                (test_id, cycler, str(path)),
            )
            conn.executemany(
                """
                INSERT INTO timeseries (test_id, timestamp_s, voltage_v, current_a, temperature_c, cycle_index)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["test_id"],
                        row["timestamp_s"],
                        row["voltage_v"],
                        row["current_a"],
                        row["temperature_c"],
                        row["cycle_index"],
                    )
                    for row in valid_rows
                ],
            )
            loaded_tests += 1
            rows_loaded += len(valid_rows)

        conn.commit()

    return {"tests_loaded": loaded_tests, "rows_loaded": rows_loaded, "rows_skipped": rows_skipped}


def is_usable_row(row: dict[str, Any]) -> bool:
    """Return True when a normalized row contains the required numeric fields."""
    required_fields = ("timestamp_s", "voltage_v", "current_a")
    return all(row.get(field) is not None for field in required_fields)


def infer_cycler(path: str | Path) -> str:
    """Infer the cycler from the file path."""
    path_str = str(path)
    if "cycler_a_biologic" in path_str:
        return "biologic"
    if "cycler_b_neware" in path_str:
        return "neware"
    if "cycler_c_novonix" in path_str:
        return "novonix"
    return "unknown"


def load_rows(path: str | Path, cycler: str, test_id: str) -> list[dict[str, Any]]:
    """Load and normalize a supported file into a list of normalized rows."""
    path = Path(path)

    if path.suffix.lower() == ".txt":
        import pandas as pd

        frame = pd.read_csv(path, sep="\t", encoding="utf-8", engine="python")
        normalized_rows = []
        for _, raw_row in frame.iterrows():
            normalized = normalize_timeseries_row(raw_row.to_dict(), cycler=cycler, test_id=test_id)
            normalized_rows.append(normalized)
        return normalized_rows

    import pandas as pd

    frame = pd.read_csv(path, encoding="utf-8", engine="python")
    normalized_rows = []
    for _, raw_row in frame.iterrows():
        normalized = normalize_timeseries_row(raw_row.to_dict(), cycler=cycler, test_id=test_id)
        normalized_rows.append(normalized)
    return normalized_rows
