import pytest
from fastapi.testclient import TestClient

from app.api import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://bad:bad@localhost:5432/battery")
    return TestClient(app)


def test_database_errors_return_503(client):
    response = client.get("/tests")
    assert response.status_code == 503
    assert response.json()["detail"] == "database unavailable"
