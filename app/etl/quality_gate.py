"""CI entry point: ingest, then run the contract and quality checks.

    python -m app.etl.quality_gate --data-root data

Exits non-zero when any *critical* issue is found, which fails the CI job —
the credential-free way to "ping" a reviewer without a Slack or email
integration: a red check on the pull request, plus a `::error::` annotation
naming the exact file or test. Wiring an actual webhook/notification is a
follow-up that needs real credentials this environment doesn't have; see
docs/data_contract.md for the checks this runs and why.

Every issue found — warning or critical — is also written to the
`data_quality_issues` table, so there is a durable, queryable record beyond
whatever scrolled past in a CI log.
"""

from __future__ import annotations

import argparse
import logging
import os

from app.db import Database
from app.etl.pipeline import ingest_directory
from app.etl.quality import Issue, check_contract, check_quality, save_issues

logger = logging.getLogger(__name__)


def _annotate(issue: Issue) -> None:
    """Emit a GitHub Actions annotation so the issue shows up on the PR diff,
    not just buried in the raw log. Harmless outside of GitHub Actions — an
    unrecognized `::...::` line is simply printed as-is.
    """
    level = "error" if issue.severity == "critical" else "warning"
    location = issue.source_path or issue.test_id or ""
    print(f"::{level} file={location}::{issue.rule}: {issue.message}")


def run(
    data_root: str,
    database_url: str | None = None,
    db_path: str | None = None,
) -> list[Issue]:
    """Ingest `data_root`, check the result, and persist every issue found."""
    summary = ingest_directory(data_root, db_path=db_path, database_url=database_url)

    with Database.connect(database_url=database_url, db_path=db_path) as db:
        issues = check_contract(summary) + check_quality(db)
        save_issues(db, issues)

    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL for this run")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    issues = run(args.data_root, database_url=args.database_url, db_path=args.db_path)

    if not issues:
        print("Data quality gate: no issues found.")
        return 0

    for issue in issues:
        _annotate(issue)
        logger.log(
            logging.ERROR if issue.severity == "critical" else logging.WARNING,
            "[%s] %s",
            issue.rule,
            issue.message,
        )

    critical = [issue for issue in issues if issue.severity == "critical"]
    print(f"Data quality gate: {len(issues)} issue(s) found ({len(critical)} critical).")
    return 1 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
