"""End-to-end coverage for the PostgreSQL backend.

The rest of the suite runs on SQLite so it needs no services. These tests only
run when a PostgreSQL instance is configured, which is how CI covers the code
path the container runtime actually uses.

    DATABASE_URL=postgresql://battery:battery@localhost:5432/battery pytest tests/test_postgres.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.db import TESTS_COLUMNS, TIMESERIES_COLUMNS, Database, is_postgres_url
from app.etl.pipeline import ingest_directory

REPO_DATA = Path(__file__).resolve().parents[1] / "data"
DATABASE_URL = os.getenv("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not is_postgres_url(DATABASE_URL),
    reason="set DATABASE_URL to a PostgreSQL instance to run these tests",
)


@pytest.fixture(scope="module")
def postgres_summary():
    """Ingest the bundled dataset into PostgreSQL once."""
    try:
        with Database.connect(database_url=DATABASE_URL, retries=3, delay_seconds=1) as db:
            db.execute("DROP TABLE IF EXISTS timeseries")
            db.execute("DROP TABLE IF EXISTS tests")
            db.commit()
    except Exception as exc:  # pragma: no cover - environment problem, not a defect
        pytest.skip(f"PostgreSQL is not reachable: {exc}")

    return ingest_directory(REPO_DATA, database_url=DATABASE_URL)


@pytest.fixture
def client(postgres_summary, monkeypatch):
    """A FastAPI test client pointed at the PostgreSQL database above."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    return TestClient(app)


def test_schema_is_created_with_postgres_types(postgres_summary):
    with Database.connect(database_url=DATABASE_URL) as db:
        assert db.is_postgres
        assert db.table_columns("tests") == set(TESTS_COLUMNS)
        assert db.table_columns("timeseries") == set(TIMESERIES_COLUMNS)


def test_ingestion_loads_every_test(postgres_summary, repo_test_count):
    assert postgres_summary["tests_loaded"] == repo_test_count
    assert postgres_summary["rows_loaded"] > 10_000


def test_reingesting_into_postgres_is_idempotent(postgres_summary, repo_test_count):
    """`docker compose up` re-runs the ETL against a persisted volume; a
    second run over unchanged files should do no reparsing or reloading."""
    ingest_directory(REPO_DATA, database_url=DATABASE_URL)
    second = ingest_directory(REPO_DATA, database_url=DATABASE_URL)

    assert second["tests_loaded"] == 0
    assert second["files_unchanged"] == repo_test_count

    with Database.connect(database_url=DATABASE_URL) as db:
        total = db.query_one("SELECT COUNT(*) FROM timeseries")[0]
    assert total > 10_000


def test_units_survive_the_postgres_round_trip(postgres_summary):
    with Database.connect(database_url=DATABASE_URL) as db:
        peak_voltage = db.query_one("SELECT MAX(voltage_v) FROM timeseries")[0]
        peak_current = db.query_one(
            "SELECT MAX(ABS(current_a)) FROM timeseries WHERE test_id = ?", ("neware_cell_001",)
        )[0]

    assert peak_voltage < 5.0
    assert 1e-3 <= peak_current <= 1.0


def test_api_endpoints_work_against_postgres(client, repo_test_count):
    assert client.get("/health").json() == {
        "status": "ok",
        "backend": "postgresql",
        "tests": repo_test_count,
    }

    tests_payload = client.get("/tests").json()
    assert len(tests_payload) == repo_test_count

    page = client.get("/tests/novonix_cell_001/timeseries", params={"limit": 10}).json()
    assert page["returned"] == 10
    assert page["next_offset"] == 10

    cycles = client.get("/tests/novonix_cell_001/cycles").json()
    assert cycles[0]["capacity_ah"] > 0

    assert client.get("/tests/does_not_exist/timeseries").status_code == 404
