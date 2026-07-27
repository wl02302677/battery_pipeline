"""Tests for the normalization rules in app/etl/contract.py.

This is where the real bugs found in the raw data live, so it's the most
heavily tested module: per-cycler unit conversion, identity, header matching,
and the value repairs (millivolt rescaling, corrupted headers).
"""

import pytest

from app.etl.contract import (
    MAX_PLAUSIBLE_CELL_VOLTAGE_V,
    build_test_id,
    infer_cycler,
    is_usable_row,
    normalize_numeric,
    normalize_timeseries_row,
    repair_voltage_scale,
)

# -- identity ------------------------------------------------------------- #


def test_build_test_id_uses_cycler_prefix_and_stem():
    assert build_test_id("data/cycler_a_biologic/cell_001.txt") == "biologic_cell_001"


def test_build_test_id_handles_explicit_cycler():
    assert build_test_id("some/random/file.csv", cycler="novonix") == "novonix_file"


def test_build_test_id_disambiguates_the_same_filename_across_cyclers():
    biologic = build_test_id("data/cycler_a_biologic/cell_001.txt")
    neware = build_test_id("data/cycler_b_neware/cell_001.csv")
    assert biologic != neware


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("data/cycler_a_biologic/cell_001.txt", "biologic"),
        ("data/cycler_b_neware/cell_003.csv", "neware"),
        ("data/cycler_c_novonix/cell_001.csv", "novonix"),
        # A new cycler folder is recognised without any code change.
        ("data/cycler_d_arbin/cell_001.csv", "arbin"),
        ("data/cycler_maccor/cell_001.csv", "maccor"),
        ("data/loose_file.csv", "unknown"),
    ],
)
def test_infer_cycler_reads_the_directory_convention(path, expected):
    assert infer_cycler(path) == expected


# -- unit conversion ------------------------------------------------------ #


def test_normalize_numeric_converts_milliamp_to_amp():
    assert normalize_numeric("1234", unit="mA", target_unit="A") == 1.234


def test_normalize_numeric_returns_none_for_bad_values():
    assert normalize_numeric("bad", unit="mA", target_unit="A") is None


@pytest.mark.parametrize("value", ["", None, "n/a", float("nan"), float("inf"), float("-inf")])
def test_normalize_numeric_rejects_unusable_values(value):
    assert normalize_numeric(value) is None


def test_normalize_numeric_leaves_matching_units_untouched():
    assert normalize_numeric(0.004, unit="A", target_unit="A") == 0.004


def test_normalize_numeric_converts_hours_to_seconds():
    assert normalize_numeric(0.0003556, unit="h", target_unit="s") == pytest.approx(1.28016)


# -- per-cycler mapping --------------------------------------------------- #


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


def test_biologic_capacity_is_converted_from_milliamp_hours():
    row = {"time/s": 1.0, "voltage_measured": 3.5, "I/mA": -900.0, "Capacity/mA.h": 32.4}

    normalized = normalize_timeseries_row(row, cycler="biologic", test_id="biologic_cell_001")

    assert normalized["capacity_ah"] == pytest.approx(0.0324)


def test_neware_current_is_already_amps_and_must_not_be_divided():
    """Regression: a hardcoded mA->A conversion used to scale every cycler."""
    row = {"Time [s]": 0.0, "Voltage [V]": 3.7172, "Current [A]": 0.00399929, "Cycle": 1}

    normalized = normalize_timeseries_row(row, cycler="neware", test_id="neware_cell_001")

    assert normalized["current_a"] == pytest.approx(0.00399929)
    assert normalized["voltage_v"] == pytest.approx(3.7172)
    assert normalized["temperature_c"] is None  # neware exports carry no temperature


def test_novonix_current_is_already_amps_and_time_is_hours():
    row = {
        "Run Time (h)": 0.0003556,
        "cell_voltage": 3.85240349,
        "Current (A)": 0.49989602,
        "Capacity (Ah)": 0.00013502,
        "Temperature (°C)": 24.648,
        "Cycle Number": 1,
    }

    normalized = normalize_timeseries_row(row, cycler="novonix", test_id="novonix_cell_001")

    assert normalized["current_a"] == pytest.approx(0.49989602)
    assert normalized["timestamp_s"] == pytest.approx(1.28016)
    assert normalized["capacity_ah"] == pytest.approx(0.00013502)


def test_novonix_step_time_is_not_used_as_a_time_fallback():
    """Step time restarts each step, so it must not stand in for the test clock."""
    row = {"Step Time (h)": 0.5, "cell_voltage": 3.9, "Current (A)": 0.5}

    normalized = normalize_timeseries_row(row, cycler="novonix", test_id="novonix_cell_001")

    assert normalized["timestamp_s"] is None
    assert not is_usable_row(normalized)


def test_mojibake_degree_sign_in_the_biologic_header_still_maps():
    """The BioLogic export ships a corrupted degree sign in its header."""
    row = {"time/s": 0.0, "voltage_measured": 3.5, "I/mA": 0.0, "Temperature/ï¿½C": 22.18}

    normalized = normalize_timeseries_row(row, cycler="biologic", test_id="biologic_cell_001")

    assert normalized["temperature_c"] == pytest.approx(22.18)


def test_an_empty_first_candidate_falls_through_to_the_next_one():
    """pandas writes NaN for a blank cell, which must not stop the chain."""
    row = {
        "Run Time (h)": 0.001,
        "cell_voltage": float("nan"),  # present but empty
        "Voltage (V)": 3.9,  # the declared fallback
        "Current (A)": 0.5,
    }

    normalized = normalize_timeseries_row(row, cycler="novonix", test_id="novonix_cell_001")

    assert normalized["voltage_v"] == pytest.approx(3.9)


def test_unknown_cycler_falls_back_to_every_known_column_name():
    row = {"Time [s]": 5.0, "Voltage [V]": 3.6, "Current [A]": 0.01}

    normalized = normalize_timeseries_row(row, cycler="unknown", test_id="unknown_cell")

    assert normalized["timestamp_s"] == 5.0
    assert normalized["current_a"] == pytest.approx(0.01)


# -- data quality repairs ------------------------------------------------- #


def test_repair_voltage_scale_rescales_millivolt_readings():
    voltage, repaired = repair_voltage_scale(3518.0154)
    assert voltage == pytest.approx(3.5180154)
    assert repaired is True


def test_repair_voltage_scale_leaves_plausible_readings_alone():
    assert repair_voltage_scale(3.5180154) == (3.5180154, False)
    assert repair_voltage_scale(MAX_PLAUSIBLE_CELL_VOLTAGE_V) == (
        MAX_PLAUSIBLE_CELL_VOLTAGE_V,
        False,
    )
    assert repair_voltage_scale(None) == (None, False)


def test_millivolt_rows_are_repaired_and_flagged_during_normalization():
    row = {"time/s": 2.3, "voltage_measured": 3517.9365, "I/mA": 0.0}

    normalized = normalize_timeseries_row(row, cycler="biologic", test_id="biologic_cell_001")

    assert normalized["voltage_v"] == pytest.approx(3.5179365)
    assert "voltage_rescaled_from_mv" in normalized["quality_flags"]


def test_missing_required_fields_are_flagged_and_make_a_row_unusable():
    row = {"time/s": 1.0, "voltage_measured": 3.2}  # no current

    normalized = normalize_timeseries_row(row, cycler="biologic", test_id="biologic_cell_001")

    assert "missing:current_a" in normalized["quality_flags"]
    assert not is_usable_row(normalized)


def test_a_complete_row_carries_no_quality_flags():
    row = {"Time [s]": 0.0, "Voltage [V]": 3.7, "Current [A]": 0.004, "Cycle": 1}

    normalized = normalize_timeseries_row(row, cycler="neware", test_id="neware_cell_001")

    assert normalized["quality_flags"] == ()
    assert is_usable_row(normalized)
