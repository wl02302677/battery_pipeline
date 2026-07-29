"""Data contract and data-quality checks, run after ingestion.

Two kinds of check, both turned into `Issue` records that the caller persists
to `data_quality_issues` and uses to decide whether to fail a CI run:

- **Contract checks** ask "did this file structurally satisfy the schema?" A
  file that produced zero usable rows failed the contract outright — every
  required column (timestamp, voltage, current) was missing, unreadable, or
  malformed for every single row, not merely blank here and there. That
  distinction already exists in `ingest_directory`'s summary
  (`skipped_file_paths`); this module just gives it a severity and a
  persisted record instead of only a log line.
- **Quality checks** ask "does what actually got loaded look physically
  plausible?" Applied to the database after ingestion, via SQL aggregates,
  rather than by re-reading source files.

Thresholds are module constants rather than magic numbers so they are easy to
tune and to reference from tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from app.db import Database

# A finding is either a hard failure ("critical") or something worth flagging
# without failing the build ("warning").
Severity = Literal["warning", "critical"]


@dataclass(frozen=True)
class Issue:
    """One data contract or data quality finding."""

    rule: str
    severity: Severity
    message: str
    test_id: str | None = None
    source_path: str | None = None


#: Above this fraction of a file's rows dropped for missing required values,
#: something is probably wrong with the export rather than a few bad samples.
MAX_SKIPPED_ROW_FRACTION = 0.10

#: Above this fraction of exact-duplicate rows, the export is probably
#: double-writing rather than occasionally repeating a sample.
MAX_DUPLICATE_ROW_FRACTION = 0.05

#: A generous window for a single Li-ion cell; catches a corrupt value (e.g. a
#: rescale that fired on the wrong threshold) rather than encoding real limits
#: for a specific chemistry.
PLAUSIBLE_VOLTAGE_RANGE_V = (0.0, 5.5)

#: Generous single-cell bound; a real pack-level import would need this raised.
PLAUSIBLE_CURRENT_MAGNITUDE_A = 100.0

# Generous window covering both lab and field temperatures.
PLAUSIBLE_TEMPERATURE_RANGE_C = (-40.0, 100.0)

#: (cycler, field) pairs already known and documented not to report a given
#: optional field — see docs/data_contract.md. Prevents an already-understood,
#: deliberate gap (Neware ships no temperature channel) from being reported as
#: a regression every time the gate runs.
KNOWN_MISSING_OPTIONAL_FIELDS: frozenset[tuple[str, str]] = frozenset({("neware", "temperature_c")})


def check_contract(ingest_summary: dict[str, Any]) -> list[Issue]:
    """Flag files that produced zero usable rows.

    `ingest_directory` already refuses to create a test record for a file with
    no usable rows and lists it in `skipped_file_paths`; this turns that into
    a `critical` issue instead of only a warning in the run's log.
    """
    # One issue per file that produced no usable rows at all.
    return [
        Issue(
            rule="file_produced_no_usable_rows",
            severity="critical",
            message=(
                f"{source_path}: every row was dropped (missing timestamp, voltage, "
                "or current) — the file's columns likely don't match the declared "
                "contract for its cycler"
            ),
            source_path=source_path,
        )
        for source_path in ingest_summary.get("skipped_file_paths", [])
    ]


def check_quality(db: Database) -> list[Issue]:
    """Flag statistically implausible or unexpectedly-absent data already in
    the database, using SQL aggregates rather than re-reading source files.
    """
    issues: list[Issue] = []

    # -- check 1: how many rows were skipped or duplicated, per test? -------
    for test_id, rows_loaded, rows_skipped, rows_duplicated in db.query(
        "SELECT test_id, rows_loaded, rows_skipped, rows_duplicated FROM tests"
    ):
        total_seen = rows_loaded + rows_skipped + rows_duplicated
        if not total_seen:
            continue

        if rows_skipped / total_seen > MAX_SKIPPED_ROW_FRACTION:
            issues.append(
                Issue(
                    rule="high_skip_rate",
                    severity="warning",
                    message=(
                        f"{test_id}: {rows_skipped}/{total_seen} rows skipped "
                        f"({rows_skipped / total_seen:.0%}), above the "
                        f"{MAX_SKIPPED_ROW_FRACTION:.0%} threshold"
                    ),
                    test_id=test_id,
                )
            )

        if rows_duplicated / total_seen > MAX_DUPLICATE_ROW_FRACTION:
            issues.append(
                Issue(
                    rule="high_duplicate_rate",
                    severity="warning",
                    message=(
                        f"{test_id}: {rows_duplicated}/{total_seen} rows were exact "
                        f"duplicates ({rows_duplicated / total_seen:.0%}), above the "
                        f"{MAX_DUPLICATE_ROW_FRACTION:.0%} threshold"
                    ),
                    test_id=test_id,
                )
            )

    # -- check 2: are the stored values themselves plausible? ---------------
    v_min, v_max = PLAUSIBLE_VOLTAGE_RANGE_V
    t_min, t_max = PLAUSIBLE_TEMPERATURE_RANGE_C
    for test_id, cycler, min_v, max_v, min_i, max_i, temp_count, min_t, max_t in db.query(
        """
        SELECT
            tests.test_id, tests.cycler,
            MIN(voltage_v), MAX(voltage_v),
            MIN(current_a), MAX(current_a),
            COUNT(temperature_c), MIN(temperature_c), MAX(temperature_c)
        FROM tests JOIN timeseries USING (test_id)
        GROUP BY tests.test_id, tests.cycler
        """
    ):
        if min_v < v_min or max_v > v_max:
            issues.append(
                Issue(
                    rule="voltage_out_of_range",
                    severity="critical",
                    message=(
                        f"{test_id}: voltage {min_v:.3f}-{max_v:.3f} V outside the "
                        f"plausible {v_min}-{v_max} V window for a single cell"
                    ),
                    test_id=test_id,
                )
            )

        peak_current = max(abs(min_i), abs(max_i))
        if peak_current > PLAUSIBLE_CURRENT_MAGNITUDE_A:
            issues.append(
                Issue(
                    rule="current_out_of_range",
                    severity="critical",
                    message=(
                        f"{test_id}: current magnitude up to {peak_current:.3f} A exceeds "
                        f"the plausible {PLAUSIBLE_CURRENT_MAGNITUDE_A} A bound for a single cell"
                    ),
                    test_id=test_id,
                )
            )

        if temp_count == 0:
            # No temperature reported anywhere in this test. Fine for a
            # cycler already known not to report it (Neware); worth flagging
            # otherwise, since it could mean the column got renamed.
            if (cycler, "temperature_c") not in KNOWN_MISSING_OPTIONAL_FIELDS:
                issues.append(
                    Issue(
                        rule="unexpected_missing_temperature",
                        severity="warning",
                        message=(
                            f"{test_id} ({cycler}): no temperature reported for any row, "
                            "and this cycler is not a known exception"
                        ),
                        test_id=test_id,
                    )
                )
        elif min_t < t_min or max_t > t_max:
            issues.append(
                Issue(
                    rule="temperature_out_of_range",
                    severity="critical",
                    message=(
                        f"{test_id}: temperature {min_t:.1f}-{max_t:.1f} °C outside the "
                        f"plausible {t_min}-{t_max} °C window"
                    ),
                    test_id=test_id,
                )
            )

    return issues


def save_issues(db: Database, issues: list[Issue]) -> None:
    """Persist findings to `data_quality_issues` for a durable, queryable history."""
    if not issues:
        # Nothing to write — skip opening a cursor for an empty batch.
        return

    # All issues from one run share the same detected_at timestamp.
    detected_at = datetime.now(UTC).isoformat(timespec="seconds")
    db.executemany(
        """
        INSERT INTO data_quality_issues (test_id, rule, severity, message, source_path, detected_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                issue.test_id,
                issue.rule,
                issue.severity,
                issue.message,
                issue.source_path,
                detected_at,
            )
            for issue in issues
        ],
    )
    db.commit()
