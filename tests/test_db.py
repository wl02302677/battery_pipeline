"""Tests for app/db.py: URL resolution, schema creation, and the ? -> %s
placeholder translation that lets the same SQL run against SQLite or
PostgreSQL.
"""

import sqlite3

import pytest

from app.db import (
    QUALITY_ISSUES_COLUMNS,
    TESTS_COLUMNS,
    TIMESERIES_COLUMNS,
    Database,
    build_database_url,
    is_postgres_url,
)

# -- URL resolution ------------------------------------------------------- #


def test_build_database_url_prefers_explicit_postgres_config(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/battery")

    assert build_database_url() == "postgresql://postgres:postgres@db:5432/battery"


def test_build_database_url_falls_back_to_sqlite_when_no_env_present(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_path = tmp_path / "battery.sqlite3"

    assert build_database_url(db_path=str(db_path)) == f"sqlite:///{db_path}"


def test_build_database_url_uses_the_battery_db_path_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BATTERY_DB_PATH", str(tmp_path / "custom.sqlite3"))

    assert build_database_url().endswith("custom.sqlite3")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@db:5432/battery", True),
        ("postgres://u:p@db:5432/battery", True),
        ("sqlite:////tmp/battery.sqlite3", False),
    ],
)
def test_is_postgres_url(url, expected):
    assert is_postgres_url(url) is expected


# -- schema --------------------------------------------------------------- #


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """A fresh, empty SQLite database for schema tests."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with Database.connect(db_path=tmp_path / "battery.sqlite3") as db:
        yield db


def test_ensure_schema_creates_both_tables_with_the_expected_columns(sqlite_db):
    sqlite_db.ensure_schema()

    assert sqlite_db.table_columns("tests") == set(TESTS_COLUMNS)
    assert sqlite_db.table_columns("timeseries") == set(TIMESERIES_COLUMNS)


def test_ensure_schema_creates_the_quality_issues_table(sqlite_db):
    sqlite_db.ensure_schema()

    assert sqlite_db.table_columns("data_quality_issues") == set(QUALITY_ISSUES_COLUMNS)


def test_ensure_schema_is_safe_to_run_twice(sqlite_db):
    sqlite_db.ensure_schema()
    sqlite_db.execute(
        "INSERT INTO tests (test_id, cycler, source_path, source_hash) VALUES (?, ?, ?, ?)",
        ("neware_cell_001", "neware", "x.csv", "deadbeef"),
    )
    sqlite_db.commit()

    sqlite_db.ensure_schema()

    assert sqlite_db.query_one("SELECT COUNT(*) FROM tests")[0] == 1


def test_ensure_schema_rebuilds_a_stale_layout(tmp_path, monkeypatch):
    """An older pipeline version wrote a narrower `timeseries` table."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_path = tmp_path / "battery.sqlite3"

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE tests (test_id TEXT PRIMARY KEY, cycler TEXT, source_path TEXT)")
        conn.execute("CREATE TABLE timeseries (id INTEGER PRIMARY KEY, test_id TEXT)")

    with Database.connect(db_path=db_path) as db:
        db.ensure_schema()

        assert db.table_columns("tests") == set(TESTS_COLUMNS)
        assert db.table_columns("timeseries") == set(TIMESERIES_COLUMNS)


def test_indexes_exist_so_lookups_by_test_id_are_not_full_scans(sqlite_db):
    sqlite_db.ensure_schema()

    plan = sqlite_db.query(
        "EXPLAIN QUERY PLAN SELECT timestamp_s FROM timeseries WHERE test_id = ?",
        ("neware_cell_001",),
    )

    assert any("ix_timeseries_test_timestamp" in str(row) for row in plan), plan
