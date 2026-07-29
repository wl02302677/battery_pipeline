"""Tests for app/schema_loader.py: the declarative data contract under schema/.

Two things matter here: that a malformed schema file fails loudly (the
"mandatory" part), and that the shipped YAML actually reconstructs the exact
values the pipeline used to hardcode as Python literals — a golden-value
check so a transcription slip in the YAML rewrite is caught here rather than
surfacing later as a silent normalization regression.
"""

import pytest

from app.schema_loader import (
    SchemaError,
    load_canonical_schema,
    load_source_schemas,
    load_target_schema,
)

# -- golden values: what contract.py/db.py hardcoded before the schema/ move ##

EXPECTED_VALUE_FIELDS = (
    "timestamp_s",
    "voltage_v",
    "current_a",
    "temperature_c",
    "capacity_ah",
    "cycle_index",
)
EXPECTED_REQUIRED_FIELDS = ("timestamp_s", "voltage_v", "current_a")
EXPECTED_TARGET_UNITS = {
    "timestamp_s": "s",
    "voltage_v": "V",
    "current_a": "A",
    "temperature_c": "C",
    "capacity_ah": "Ah",
}

EXPECTED_COLUMN_MAP = {
    "biologic": {
        "timestamp_s": (("time/s", "s"),),
        "voltage_v": (("voltage_measured", "V"),),
        "current_a": (("I/mA", "mA"),),
        "temperature_c": (("Temperature/°C", "C"),),
        "capacity_ah": (("Capacity/mA.h", "mA.h"), ("Q discharge/mA.h", "mA.h")),
        "cycle_index": (("cycle number", None),),
    },
    "neware": {
        "timestamp_s": (("Time [s]", "s"),),
        "voltage_v": (("Voltage [V]", "V"),),
        "current_a": (("Current [A]", "A"),),
        "temperature_c": (("Temperature [°C]", "C"),),
        "capacity_ah": (("Capacity [Ah]", "Ah"),),
        "cycle_index": (("Cycle", None),),
    },
    "novonix": {
        "timestamp_s": (("Run Time (h)", "h"),),
        "voltage_v": (("cell_voltage", "V"), ("Voltage (V)", "V")),
        "current_a": (("Current (A)", "A"),),
        "temperature_c": (("Temperature (°C)", "C"),),
        "capacity_ah": (("Capacity (Ah)", "Ah"),),
        "cycle_index": (("Cycle Number", None),),
    },
}

EXPECTED_TESTS_COLUMNS = (
    "test_id",
    "cycler",
    "source_path",
    "source_hash",
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
EXPECTED_TIMESERIES_COLUMNS = (
    "id",
    "test_id",
    "timestamp_s",
    "voltage_v",
    "current_a",
    "temperature_c",
    "capacity_ah",
    "cycle_index",
)
EXPECTED_QUALITY_ISSUES_COLUMNS = (
    "id",
    "test_id",
    "rule",
    "severity",
    "message",
    "source_path",
    "detected_at",
)


# -- shipped schema files -------------------------------------------------- #


def test_canonical_schema_matches_the_original_field_list():
    canonical = load_canonical_schema()

    assert tuple(field.name for field in canonical.fields) == EXPECTED_VALUE_FIELDS
    assert tuple(field.name for field in canonical.fields if field.required) == (
        EXPECTED_REQUIRED_FIELDS
    )
    assert {
        field.name: field.unit for field in canonical.fields if field.unit is not None
    } == EXPECTED_TARGET_UNITS


def test_source_schemas_reconstruct_the_original_column_map():
    sources = load_source_schemas()

    assert set(sources) == set(EXPECTED_COLUMN_MAP)
    for cycler, expected_fields in EXPECTED_COLUMN_MAP.items():
        actual = {
            field: tuple((c.column, c.unit) for c in candidates)
            for field, candidates in sources[cycler].fields.items()
        }
        assert actual == expected_fields


@pytest.mark.parametrize(
    ("name", "expected_columns"),
    [
        ("tests", EXPECTED_TESTS_COLUMNS),
        ("timeseries", EXPECTED_TIMESERIES_COLUMNS),
        ("data_quality_issues", EXPECTED_QUALITY_ISSUES_COLUMNS),
    ],
)
def test_target_schema_column_order_matches_the_original_tables(name, expected_columns):
    schema = load_target_schema(name)

    assert tuple(column.name for column in schema.columns) == expected_columns


def test_data_quality_issues_is_marked_append_only():
    assert load_target_schema("data_quality_issues").append_only is True


def test_tests_and_timeseries_are_not_marked_append_only():
    assert load_target_schema("tests").append_only is False
    assert load_target_schema("timeseries").append_only is False


# -- mandatory validation: bad input fails loudly -------------------------- #


def test_missing_canonical_schema_file_raises_schema_error(tmp_path):
    with pytest.raises(SchemaError, match="not found"):
        load_canonical_schema(schema_root=tmp_path)


def test_invalid_yaml_raises_schema_error(tmp_path):
    (tmp_path / "canonical_fields.yaml").write_text("fields: [this is: not: valid")

    with pytest.raises(SchemaError, match="invalid YAML"):
        load_canonical_schema(schema_root=tmp_path)


def test_source_schema_missing_required_key_raises_schema_error(tmp_path):
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    # No "cycler:" key — the model requires it.
    (sources_dir / "bad.yaml").write_text("fields:\n  timestamp_s:\n    - column: time\n")

    with pytest.raises(SchemaError, match="bad.yaml"):
        load_source_schemas(schema_root=tmp_path)


def test_target_schema_bad_column_type_raises_schema_error(tmp_path):
    targets_dir = tmp_path / "targets"
    targets_dir.mkdir()
    (targets_dir / "widgets.yaml").write_text(
        "table: widgets\ncolumns:\n  - name: id\n    type: not_a_real_type\n"
    )

    with pytest.raises(SchemaError, match="widgets.yaml"):
        load_target_schema("widgets", schema_root=tmp_path)
