from pathlib import Path

from fastapi.testclient import TestClient

from app.api import app
from app.etl.pipeline import ingest_directory


def test_api_endpoints_return_aggregated_data(tmp_path, monkeypatch):
    repo_data = Path(__file__).resolve().parents[1] / "data"
    db_path = tmp_path / "battery.sqlite3"
    ingest_directory(repo_data, db_path=db_path)

    monkeypatch.setenv("BATTERY_DB_PATH", str(db_path))
    client = TestClient(app)

    tests_response = client.get("/tests")
    assert tests_response.status_code == 200
    tests_payload = tests_response.json()
    assert any(item["test_id"] == "biologic_cell_001" for item in tests_payload)
    assert any(item["cycler"] == "neware" for item in tests_payload)

    timeseries_response = client.get("/tests/biologic_cell_001/timeseries")
    assert timeseries_response.status_code == 200
    timeseries_payload = timeseries_response.json()
    assert len(timeseries_payload) > 0
    assert timeseries_payload[0]["timestamp_s"] == 0.0
    assert timeseries_payload[0]["voltage_v"] > 0

    cycles_response = client.get("/tests/novonix_cell_001/cycles")
    assert cycles_response.status_code == 200
    cycles_payload = cycles_response.json()
    assert len(cycles_payload) > 0
    assert cycles_payload[0]["cycle_index"] == 1
    assert cycles_payload[0]["sample_count"] > 0
