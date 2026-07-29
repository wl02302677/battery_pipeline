"""Database access layer shared by the ETL pipeline and the API.

The project supports two backends: PostgreSQL for the container runtime and
SQLite for local development and tests. Rather than branching on the backend at
every call site, the differences are isolated here:

- placeholder style (``?`` vs ``%s``)
- column types (``REAL`` vs ``DOUBLE PRECISION``)
- autoincrement primary keys

All SQL elsewhere in the codebase is written with ``?`` placeholders and is
translated on the way to the driver.

The target table layouts (``tests``, ``timeseries``, ``data_quality_issues``)
are declared in ``schema/targets/*.yaml``, not as SQL literals here — see
``app/schema_loader.py``. This module turns a declared layout into backend-
appropriate DDL (``_render_create_table``) and derives the column-name tuples
used for drift detection.
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

from app.schema_loader import ColumnSpec, TableSchema, load_target_schema

logger = logging.getLogger(__name__)

# URL prefixes used to tell the two backends apart.
SQLITE_SCHEME = "sqlite:///"
POSTGRES_SCHEMES = ("postgresql://", "postgres://")

#: Target table layouts, declared in schema/targets/*.yaml (see
#: app/schema_loader.py) rather than as SQL literals here. Loaded once at
#: import time, so a malformed schema file fails before any connection opens.
_TESTS_SCHEMA: TableSchema = load_target_schema("tests")
_TIMESERIES_SCHEMA: TableSchema = load_target_schema("timeseries")
_QUALITY_ISSUES_SCHEMA: TableSchema = load_target_schema("data_quality_issues")

#: Columns of the ``tests`` table, in declaration order. Used to detect schema
#: drift against a database created by an older version of the pipeline.
TESTS_COLUMNS: tuple[str, ...] = tuple(column.name for column in _TESTS_SCHEMA.columns)

#: Columns of the ``timeseries`` table, in declaration order.
TIMESERIES_COLUMNS: tuple[str, ...] = tuple(column.name for column in _TIMESERIES_SCHEMA.columns)

#: Columns of the ``data_quality_issues`` table. Not subject to the
#: drift-rebuild below: it is an append-only audit log, not a reflection of
#: source file content, so a schema change to `tests`/`timeseries` must never
#: silently wipe its history (``schema/targets/data_quality_issues.yaml``
#: declares ``append_only: true`` for the same reason; ``ensure_schema``
#: still only ever rebuilds ``tests``/``timeseries``, see below).
QUALITY_ISSUES_COLUMNS: tuple[str, ...] = tuple(
    column.name for column in _QUALITY_ISSUES_SCHEMA.columns
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
    # An explicit DATABASE_URL (set by docker-compose.yml) always takes
    # priority, regardless of what db_path was passed in.
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    # No PostgreSQL configured, so fall back to a SQLite file on disk.
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

    # Retry loop: on startup the Postgres container may not be accepting
    # connections yet, so keep trying with a short pause instead of failing
    # on the very first attempt.
    last_error: BaseException | None = None
    for attempt in range(max(retries, 1)):
        try:
            return psycopg2.connect(database_url)
        except psycopg2.OperationalError as exc:
            last_error = exc
            if attempt == max(retries, 1) - 1:
                # Out of retries — let the caller see the real error.
                raise
            logger.warning("Database not ready (attempt %s/%s): %s", attempt + 1, retries, exc)
            time.sleep(delay_seconds)

    raise last_error  # pragma: no cover - defensive


class Database:
    """Thin wrapper over a DB-API connection that hides backend differences.

    Every method here accepts SQL written with ``?`` placeholders, the SQLite
    style. When the connection is PostgreSQL, `_prepare` rewrites them to
    ``%s`` before the statement reaches the driver, so calling code never has
    to branch on which backend it is talking to.
    """

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
        # Work out which database URL to use if the caller didn't pass one.
        url = database_url or build_database_url(db_path=str(db_path) if db_path else None)

        if is_postgres_url(url):
            return cls(connect_database(url, retries=retries, delay_seconds=delay_seconds), True)

        # SQLite: make sure the parent folder exists, then open the file.
        path = sqlite_path_from_url(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(path), False)

    # -- statement execution ---------------------------------------------

    def _prepare(self, statement: str) -> str:
        """Translate ``?`` placeholders for the PostgreSQL driver."""
        return statement.replace("?", "%s") if self.is_postgres else statement

    def execute(self, statement: str, params: Sequence[Any] = ()) -> Any:
        """Run one SQL statement and return its cursor (for INSERT/UPDATE/DDL)."""
        cursor = self.connection.cursor()
        cursor.execute(self._prepare(statement), tuple(params))
        return cursor

    def executemany(self, statement: str, rows: Iterable[Sequence[Any]]) -> None:
        """Run the same statement once per row, e.g. a batch of INSERTs."""
        batch = [tuple(row) for row in rows]
        if not batch:
            # Nothing to insert — skip opening a cursor for an empty batch.
            return
        cursor = self.connection.cursor()
        try:
            cursor.executemany(self._prepare(statement), batch)
        finally:
            cursor.close()

    def query(self, statement: str, params: Sequence[Any] = ()) -> list[tuple]:
        """Run a SELECT and return every matching row."""
        cursor = self.execute(statement, params)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def query_one(self, statement: str, params: Sequence[Any] = ()) -> tuple | None:
        """Run a SELECT and return the first row, or None if there isn't one."""
        cursor = self.execute(statement, params)
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def commit(self) -> None:
        """Commit the current transaction."""
        self.connection.commit()

    def close(self) -> None:
        """Close the underlying connection, ignoring any error while doing so."""
        try:
            self.connection.close()
        except Exception:  # pragma: no cover - closing should never break callers
            logger.debug("Ignoring error while closing the database connection", exc_info=True)

    def __enter__(self) -> Database:
        # Lets callers write `with Database.connect(...) as db:` and have the
        # connection closed automatically at the end of the block.
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- schema ----------------------------------------------------------

    @property
    def _float_type(self) -> str:
        """The floating-point column type for whichever backend this is."""
        return "DOUBLE PRECISION" if self.is_postgres else "REAL"

    @property
    def _serial_pk(self) -> str:
        """The auto-incrementing primary key syntax for whichever backend this is."""
        return "BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"

    def _sql_type(self, column: ColumnSpec) -> str:
        """Resolve a declared Python-level column type to this backend's SQL type."""
        return {
            "str": "TEXT",
            "int": "INTEGER",
            "float": self._float_type,
            "serial_pk": self._serial_pk,
        }[column.type]

    def _render_create_table(self, schema: TableSchema) -> str:
        """Build a ``CREATE TABLE IF NOT EXISTS`` statement from a target schema."""
        columns = []
        for column in schema.columns:
            parts = [column.name, self._sql_type(column)]
            if column.primary_key:
                parts.append("PRIMARY KEY")
            if not column.nullable:
                parts.append("NOT NULL")
            if column.default is not None:
                parts.append(f"DEFAULT {column.default}")
            if column.references:
                parts.append(f"REFERENCES {column.references}")
            columns.append(" ".join(parts))
        body = ",\n    ".join(columns)
        return f"CREATE TABLE IF NOT EXISTS {schema.table} (\n    {body}\n)"

    def _create_indexes(self, schema: TableSchema) -> None:
        """Create every index declared for a target schema."""
        for index in schema.indexes:
            self.execute(
                f"CREATE INDEX IF NOT EXISTS {index.name}"
                f" ON {schema.table} ({', '.join(index.columns)})"
            )

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
        # Check whether the tables that already exist (if any) still match the
        # columns this version of the code expects.
        drifted = [
            table
            for table, expected in (("tests", TESTS_COLUMNS), ("timeseries", TIMESERIES_COLUMNS))
            if (columns := self.table_columns(table)) and columns != set(expected)
        ]
        if drifted:
            # Schema changed since these tables were created — drop and let
            # the CREATE TABLE statements below rebuild them from scratch.
            logger.warning(
                "Existing schema for %s does not match the current model; recreating both tables",
                ", ".join(drifted),
            )
            # timeseries first: it references tests.
            self.execute("DROP TABLE IF EXISTS timeseries")
            self.execute("DROP TABLE IF EXISTS tests")
            self.commit()

        # One row per source file, plus the ingestion counters the API exposes.
        self.execute(self._render_create_table(_TESTS_SCHEMA))
        # One row per normalized sample.
        self.execute(self._render_create_table(_TIMESERIES_SCHEMA))
        # Every API query filters by test_id; without these indexes each request
        # is a full table scan.
        self._create_indexes(_TIMESERIES_SCHEMA)

        # Deliberately outside the drift-rebuild above: see QUALITY_ISSUES_COLUMNS.
        self.execute(self._render_create_table(_QUALITY_ISSUES_SCHEMA))
        self._create_indexes(_QUALITY_ISSUES_SCHEMA)
        self.commit()
