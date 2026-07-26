"""Database access layer shared by the ETL pipeline and the API.

The project supports two backends: PostgreSQL for the container runtime and
SQLite for local development and tests. Rather than branching on the backend at
every call site, the differences are isolated here:

- placeholder style (``?`` vs ``%s``)
- column types (``REAL`` vs ``DOUBLE PRECISION``)
- autoincrement primary keys

All SQL elsewhere in the codebase is written with ``?`` placeholders and is
translated on the way to the driver.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import psycopg2

logger = logging.getLogger(__name__)

SQLITE_SCHEME = "sqlite:///"
POSTGRES_SCHEMES = ("postgresql://", "postgres://")

#: Columns of the ``tests`` table, in declaration order. Used to detect schema
#: drift against a database created by an older version of the pipeline.
TESTS_COLUMNS: tuple[str, ...] = (
    "test_id",
    "cycler",
    "source_path",
    "start_offset_s",
    "rows_loaded",
    "rows_skipped",
    "rows_duplicated",
    "rows_rescaled",
    "first_timestamp_s",
    "last_timestamp_s",
    "cycle_count",
    "ingested_at",
)

#: Columns of the ``timeseries`` table, in declaration order.
TIMESERIES_COLUMNS: tuple[str, ...] = (
    "id",
    "test_id",
    "timestamp_s",
    "voltage_v",
    "current_a",
    "temperature_c",
    "capacity_ah",
    "cycle_index",
)


def is_postgres_url(database_url: str) -> bool:
    """Return True when the URL points at PostgreSQL."""
    return database_url.startswith(POSTGRES_SCHEMES)


def build_database_url(db_path: str | None = None) -> str:
    """Build a database URL for SQLite or PostgreSQL.

    ``DATABASE_URL`` always wins so the container runtime can select PostgreSQL
    without any code change. Otherwise a local SQLite file is used, taken from
    ``db_path``, then ``BATTERY_DB_PATH``, then a default in the working
    directory.
    """
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    resolved_db_path = Path(db_path or os.getenv("BATTERY_DB_PATH", "battery.sqlite3")).resolve()
    return f"sqlite:///{resolved_db_path}"


def sqlite_path_from_url(database_url: str) -> Path:
    """Extract the filesystem path from a SQLite URL."""
    if database_url.startswith(SQLITE_SCHEME):
        return Path(database_url[len(SQLITE_SCHEME) :])
    return Path(database_url)


def connect_database(database_url: str, retries: int = 10, delay_seconds: float = 2.0):
    """Connect to PostgreSQL with retries so the ETL can wait for the container.

    Returns ``None`` for non-PostgreSQL URLs to preserve the original contract
    of this helper. Prefer :meth:`Database.connect`, which handles both
    backends.
    """
    if not is_postgres_url(database_url):
        return None

    last_error: BaseException | None = None
    for attempt in range(max(retries, 1)):
        try:
            return psycopg2.connect(database_url)
        except psycopg2.OperationalError as exc:
            last_error = exc
            if attempt == max(retries, 1) - 1:
                raise
            logger.warning("Database not ready (attempt %s/%s): %s", attempt + 1, retries, exc)
            time.sleep(delay_seconds)

    raise last_error  # pragma: no cover - defensive


class Database:
    """Thin wrapper over a DB-API connection that hides backend differences."""

    def __init__(self, connection: Any, is_postgres: bool) -> None:
        self.connection = connection
        self.is_postgres = is_postgres

    # -- construction ----------------------------------------------------

    @classmethod
    def connect(
        cls,
        database_url: str | None = None,
        db_path: str | Path | None = None,
        retries: int = 10,
        delay_seconds: float = 2.0,
    ) -> Database:
        """Open a connection to whichever backend is configured."""
        url = database_url or build_database_url(db_path=str(db_path) if db_path else None)

        if is_postgres_url(url):
            return cls(connect_database(url, retries=retries, delay_seconds=delay_seconds), True)

        path = sqlite_path_from_url(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(path), False)

    # -- statement execution ---------------------------------------------

    def _prepare(self, statement: str) -> str:
        """Translate ``?`` placeholders for the PostgreSQL driver."""
        return statement.replace("?", "%s") if self.is_postgres else statement

    def execute(self, statement: str, params: Sequence[Any] = ()) -> Any:
        cursor = self.connection.cursor()
        cursor.execute(self._prepare(statement), tuple(params))
        return cursor

    def executemany(self, statement: str, rows: Iterable[Sequence[Any]]) -> None:
        batch = [tuple(row) for row in rows]
        if not batch:
            return
        cursor = self.connection.cursor()
        try:
            cursor.executemany(self._prepare(statement), batch)
        finally:
            cursor.close()

    def query(self, statement: str, params: Sequence[Any] = ()) -> list[tuple]:
        cursor = self.execute(statement, params)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def query_one(self, statement: str, params: Sequence[Any] = ()) -> tuple | None:
        cursor = self.execute(statement, params)
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:  # pragma: no cover - closing should never break callers
            logger.debug("Ignoring error while closing the database connection", exc_info=True)

    def __enter__(self) -> Database:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- schema ----------------------------------------------------------

    @property
    def _float_type(self) -> str:
        return "DOUBLE PRECISION" if self.is_postgres else "REAL"

    @property
    def _serial_pk(self) -> str:
        return "BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"

    def table_columns(self, table: str) -> set[str]:
        """Return the column names of ``table``, or an empty set if absent."""
        if self.is_postgres:
            rows = self.query(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                (table,),
            )
            return {row[0] for row in rows}

        # PRAGMA does not accept bound parameters; `table` is always a literal
        # defined in this module, never user input.
        rows = self.query(f"PRAGMA table_info({table})")
        return {row[1] for row in rows}

    def ensure_schema(self) -> None:
        """Create the schema, recreating it if an older layout is present.

        This is a prototype, so instead of a migration tool the pipeline detects
        a column mismatch and rebuilds both tables. Ingestion is a full reload
        of the source files, so nothing that cannot be regenerated is lost.
        """
        drifted = [
            table
            for table, expected in (("tests", TESTS_COLUMNS), ("timeseries", TIMESERIES_COLUMNS))
            if (columns := self.table_columns(table)) and columns != set(expected)
        ]
        if drifted:
            logger.warning(
                "Existing schema for %s does not match the current model; recreating both tables",
                ", ".join(drifted),
            )
            # timeseries first: it references tests.
            self.execute("DROP TABLE IF EXISTS timeseries")
            self.execute("DROP TABLE IF EXISTS tests")
            self.commit()

        self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS tests (
                test_id TEXT PRIMARY KEY,
                cycler TEXT NOT NULL,
                source_path TEXT NOT NULL,
                start_offset_s {self._float_type},
                rows_loaded INTEGER NOT NULL DEFAULT 0,
                rows_skipped INTEGER NOT NULL DEFAULT 0,
                rows_duplicated INTEGER NOT NULL DEFAULT 0,
                rows_rescaled INTEGER NOT NULL DEFAULT 0,
                first_timestamp_s {self._float_type},
                last_timestamp_s {self._float_type},
                cycle_count INTEGER NOT NULL DEFAULT 0,
                ingested_at TEXT
            )
            """
        )
        self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS timeseries (
                id {self._serial_pk},
                test_id TEXT NOT NULL REFERENCES tests(test_id),
                timestamp_s {self._float_type},
                voltage_v {self._float_type},
                current_a {self._float_type},
                temperature_c {self._float_type},
                capacity_ah {self._float_type},
                cycle_index INTEGER
            )
            """
        )
        # Every API query filters by test_id; without these indexes each request
        # is a full table scan.
        self.execute(
            "CREATE INDEX IF NOT EXISTS ix_timeseries_test_timestamp"
            " ON timeseries (test_id, timestamp_s)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS ix_timeseries_test_cycle"
            " ON timeseries (test_id, cycle_index)"
        )
        self.commit()
