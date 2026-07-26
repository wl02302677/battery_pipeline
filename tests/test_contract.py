from pathlib import Path

from app.etl.contract import build_test_id, normalize_numeric, normalize_timeseries_row


def test_build_test_id_uses_cycler_prefix_and_stem():
    assert build_test_id("data/cycler_a_biologic/cell_001.txt") == "biologic_cell_001"


def test_build_test_id_handles_explicit_cycler():
    assert build_test_id("some/random/file.csv", cycler="novonix") == "novonix_file"


def test_normalize_numeric_converts_milliamp_to_amp():
    assert normalize_numeric("1234", unit="mA", target_unit="A") == 1.234


def test_normalize_numeric_returns_none_for_bad_values():
    assert normalize_numeric("bad", unit="mA", target_unit="A") is None


def test_normalize_timeseries_row_maps_cycler_specific_columns():
    row = {
        "time/s": 12.0,
        "voltage_measured": 3.2,
        "I/mA": 500.0,
        "Temperature/°C": 25.0,
        "cycle number": 3,
    }

    normalized = normalize_timeseries_row(row, cycler="biologic", test_id="biologic_cell_001")

    assert normalized["test_id"] == "biologic_cell_001"
    assert normalized["cycler"] == "biologic"
    assert normalized["timestamp_s"] == 12.0
    assert normalized["voltage_v"] == 3.2
    assert normalized["current_a"] == 0.5
    assert normalized["temperature_c"] == 25.0
    assert normalized["cycle_index"] == 3
