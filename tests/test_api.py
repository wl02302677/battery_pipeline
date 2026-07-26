from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.etl.pipeline import ingest_directory

REPO_DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="module")
def ingested_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("api") / "battery.sqlite3"
    ingest_directory(REPO_DATA, db_path=db_path)
    return db_path


@pytest.fixture
def client(ingested_db, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BATTERY_DB_PATH", str(ingested_db))
    return TestClient(app)


# -- meta ----------------------------------------------------------------- #


def test_health_reports_the_backend_and_test_count(client):
    payload = client.get("/health").json()

    assert payload["status"] == "ok"
    assert payload["backend"] == "sqlite"
    assert payload["tests"] == 12


def test_root_serves_the_dashboard(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Battery Pipeline Dashboard" in response.text


# -- /tests --------------------------------------------------------------- #


def test_list_tests_returns_every_test_with_its_cycler(client):
    response = client.get("/tests")
    assert response.status_code == 200
    payload = response.json()

    assert len(payload) == 12
    assert {item["cycler"] for item in payload} == {"biologic", "neware", "novonix"}
    assert [item["test_id"] for item in payload] == sorted(item["test_id"] for item in payload)


def test_list_tests_surfaces_data_quality_counters(client):
    biologic = next(
        item for item in client.get("/tests").json() if item["test_id"] == "biologic_cell_001"
    )

    assert biologic["rows_loaded"] > 0
    assert biologic["rows_skipped"] > 0  # rows without a current reading
    assert biologic["rows_rescaled"] > 0  # rows exported in millivolts
    assert biologic["duration_s"] > 0


def test_list_tests_can_filter_by_cycler(client):
    payload = client.get("/tests", params={"cycler": "neware"}).json()

    assert len(payload) == 10
    assert {item["cycler"] for item in payload} == {"neware"}


def test_get_single_test_returns_its_summary(client):
    payload = client.get("/tests/neware_cell_010").json()

    assert payload["test_id"] == "neware_cell_010"
    # Raw source clock, kept after rebasing timestamps to zero.
    assert payload["start_offset_s"] > 500_000


def test_unknown_test_returns_404_everywhere(client):
    for url in (
        "/tests/does_not_exist",
        "/tests/does_not_exist/timeseries",
        "/tests/does_not_exist/cycles",
    ):
        response = client.get(url)
        assert response.status_code == 404, url
        assert "does_not_exist" in response.json()["detail"]


# -- /timeseries ---------------------------------------------------------- #


def test_timeseries_returns_a_page_of_normalized_samples(client):
    payload = client.get("/tests/biologic_cell_001/timeseries").json()

    assert payload["test_id"] == "biologic_cell_001"
    assert payload["returned"] == len(payload["data"])
    first = payload["data"][0]
    assert first["timestamp_s"] == 0.0
    assert 0 < first["voltage_v"] < 5
    assert first["temperature_c"] > 0
    assert "capacity_ah" in first


def test_timeseries_paginates_and_advertises_the_next_offset(client):
    first = client.get("/tests/novonix_cell_001/timeseries", params={"limit": 50}).json()

    assert first["total"] == 201
    assert first["returned"] == 50
    assert first["next_offset"] == 50

    second = client.get(
        "/tests/novonix_cell_001/timeseries", params={"limit": 50, "offset": 50}
    ).json()
    assert second["data"][0] != first["data"][0]

    last = client.get(
        "/tests/novonix_cell_001/timeseries", params={"limit": 50, "offset": 200}
    ).json()
    assert last["returned"] == 1
    assert last["next_offset"] is None


def test_timeseries_pages_cover_every_row_exactly_once(client):
    collected = []
    offset = 0
    while True:
        page = client.get(
            "/tests/neware_cell_001/timeseries", params={"limit": 300, "offset": offset}
        ).json()
        collected.extend(page["data"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    assert len(collected) == page["total"]
    timestamps = [point["timestamp_s"] for point in collected]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)


def test_timeseries_can_be_filtered_by_time_window(client):
    payload = client.get(
        "/tests/neware_cell_001/timeseries", params={"start_s": 10, "end_s": 20}
    ).json()

    assert payload["total"] == 11  # inclusive on both ends
    assert all(10 <= point["timestamp_s"] <= 20 for point in payload["data"])


def test_timeseries_can_be_filtered_by_cycle(client):
    payload = client.get("/tests/novonix_cell_001/timeseries", params={"cycle_index": 1}).json()

    assert payload["total"] > 0
    assert {point["cycle_index"] for point in payload["data"]} == {1}


def test_a_known_test_with_no_matching_rows_is_empty_not_404(client):
    """404 means "no such test", not "your filter matched nothing"."""
    response = client.get("/tests/neware_cell_001/timeseries", params={"start_s": 10_000_000})

    assert response.status_code == 200
    assert response.json() == {
        "test_id": "neware_cell_001",
        "total": 0,
        "returned": 0,
        "limit": 1000,
        "offset": 0,
        "next_offset": None,
        "data": [],
    }


def test_timeseries_rejects_an_out_of_range_limit(client):
    assert client.get("/tests/neware_cell_001/timeseries?limit=0").status_code == 422
    assert client.get("/tests/neware_cell_001/timeseries?limit=999999").status_code == 422
    assert client.get("/tests/neware_cell_001/timeseries?offset=-1").status_code == 422


# -- /cycles -------------------------------------------------------------- #


def test_cycles_returns_per_cycle_summary_statistics(client):
    payload = client.get("/tests/novonix_cell_001/cycles").json()

    assert len(payload) == 1
    cycle = payload[0]
    assert cycle["cycle_index"] == 1
    assert cycle["sample_count"] > 0
    assert cycle["min_voltage_v"] <= cycle["max_voltage_v"]
    assert cycle["duration_s"] == pytest.approx(cycle["end_time_s"] - cycle["start_time_s"])
    assert cycle["capacity_ah"] > 0
    assert cycle["min_temperature_c"] <= cycle["max_temperature_c"]


def test_cycles_splits_capacity_by_current_direction(client):
    # The novonix export is a charge only, the biologic one a discharge only.
    charging = client.get("/tests/novonix_cell_001/cycles").json()[0]
    assert charging["charge_capacity_ah"] > 0
    assert charging["discharge_capacity_ah"] is None

    discharging = client.get("/tests/biologic_cell_001/cycles").json()[0]
    assert discharging["discharge_capacity_ah"] > 0


def test_every_test_reports_at_least_one_cycle(client):
    for item in client.get("/tests").json():
        payload = client.get(f"/tests/{item['test_id']}/cycles").json()
        assert len(payload) == item["cycle_count"], item["test_id"]
