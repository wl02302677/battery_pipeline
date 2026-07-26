import time

import pytest
from fastapi.testclient import TestClient

from app.api import app

#: A port nothing listens on, so the connection is refused immediately.
UNREACHABLE_DATABASE_URL = "postgresql://bad:bad@127.0.0.1:5599/battery"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL)
    return TestClient(app)


@pytest.mark.parametrize(
    "url",
    [
        "/health",
        "/tests",
        "/tests/biologic_cell_001",
        "/tests/biologic_cell_001/timeseries",
        "/tests/biologic_cell_001/cycles",
    ],
)
def test_database_errors_return_503(client, url):
    response = client.get(url)

    assert response.status_code == 503
    assert response.json()["detail"] == "database unavailable"


def test_the_api_fails_fast_instead_of_exhausting_the_retry_budget(client):
    """The ETL waits for a slow container; a web request must not hold a worker."""
    started = time.monotonic()
    client.get("/tests")

    assert time.monotonic() - started < 5.0


def test_a_missing_sqlite_schema_is_reported_as_unavailable(tmp_path, monkeypatch):
    """Querying before the ETL has ever run should not raise a 500."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BATTERY_DB_PATH", str(tmp_path / "never-ingested.sqlite3"))

    response = TestClient(app).get("/tests")

    assert response.status_code == 503
