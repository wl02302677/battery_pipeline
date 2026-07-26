from pathlib import Path

import pytest

from app.db import Database
from app.etl import quality_gate
from app.etl.quality import (
    Issue,
    check_contract,
    check_quality,
    save_issues,
)

REPO_DATA = Path(__file__).resolve().parents[1] / "data"

NEWARE_HEADER = "Time [s],Voltage [V],Current [A],Cycle,Capacity [Ah]\n"


def write_file(root: Path, cycler_dir: str, name: str, content: str) -> Path:
    directory = root / cycler_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


# -- contract checks -------------------------------------------------------- #


def test_check_contract_flags_files_with_zero_usable_rows():
    summary = {"skipped_file_paths": ["data/cycler_b_neware/broken.csv"]}

    issues = check_contract(summary)

    assert len(issues) == 1
    assert issues[0].rule == "file_produced_no_usable_rows"
    assert issues[0].severity == "critical"
    assert issues[0].source_path == "data/cycler_b_neware/broken.csv"


def test_check_contract_is_empty_when_nothing_was_skipped():
    assert check_contract({"skipped_file_paths": []}) == []
    assert check_contract({}) == []


# -- quality checks ---------------------------------------------------------- #


@pytest.fixture
def db(tmp_path):
    with Database.connect(db_path=tmp_path / "battery.sqlite3") as database:
        database.ensure_schema()
        yield database


def _insert_test(db, test_id, cycler, rows_loaded=100, rows_skipped=0, rows_duplicated=0):
    db.execute(
        "INSERT INTO tests (test_id, cycler, source_path, source_hash, rows_loaded,"
        " rows_skipped, rows_duplicated, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            test_id,
            cycler,
            f"{test_id}.csv",
            "hash",
            rows_loaded,
            rows_skipped,
            rows_duplicated,
            "now",
        ),
    )
    db.commit()


def _insert_row(db, test_id, timestamp_s, voltage_v, current_a, temperature_c=None):
    db.execute(
        "INSERT INTO timeseries (test_id, timestamp_s, voltage_v, current_a, temperature_c,"
        " capacity_ah, cycle_index) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (test_id, timestamp_s, voltage_v, current_a, temperature_c, 0.0, 1),
    )
    db.commit()


def test_check_quality_flags_high_skip_rate(db):
    _insert_test(db, "neware_cell_001", "neware", rows_loaded=80, rows_skipped=20)
    _insert_row(db, "neware_cell_001", 0.0, 3.7, 0.004, temperature_c=25.0)

    issues = check_quality(db)

    assert any(i.rule == "high_skip_rate" and i.severity == "warning" for i in issues)


def test_check_quality_flags_high_duplicate_rate(db):
    _insert_test(db, "neware_cell_001", "neware", rows_loaded=90, rows_duplicated=10)
    _insert_row(db, "neware_cell_001", 0.0, 3.7, 0.004, temperature_c=25.0)

    issues = check_quality(db)

    assert any(i.rule == "high_duplicate_rate" for i in issues)


def test_check_quality_flags_voltage_out_of_range(db):
    _insert_test(db, "biologic_cell_001", "biologic")
    _insert_row(db, "biologic_cell_001", 0.0, 3517.9, 0.0, temperature_c=22.0)  # unrescaled mV

    issues = check_quality(db)

    assert any(i.rule == "voltage_out_of_range" and i.severity == "critical" for i in issues)


def test_check_quality_flags_current_out_of_range(db):
    _insert_test(db, "novonix_cell_001", "novonix")
    _insert_row(db, "novonix_cell_001", 0.0, 3.7, 500.0, temperature_c=24.0)

    issues = check_quality(db)

    assert any(i.rule == "current_out_of_range" and i.severity == "critical" for i in issues)


def test_check_quality_flags_temperature_out_of_range(db):
    _insert_test(db, "novonix_cell_001", "novonix")
    _insert_row(db, "novonix_cell_001", 0.0, 3.7, 0.4, temperature_c=250.0)

    issues = check_quality(db)

    assert any(i.rule == "temperature_out_of_range" and i.severity == "critical" for i in issues)


def test_check_quality_flags_unexpected_missing_temperature_for_an_unknown_cycler(db):
    _insert_test(db, "arbin_cell_001", "arbin")
    _insert_row(db, "arbin_cell_001", 0.0, 3.7, 0.4, temperature_c=None)

    issues = check_quality(db)

    assert any(i.rule == "unexpected_missing_temperature" for i in issues)


def test_check_quality_does_not_flag_newares_known_missing_temperature(db):
    """Regression: Neware genuinely has no temperature channel — see
    docs/data_contract.md. That is a documented, known gap, not a defect."""
    _insert_test(db, "neware_cell_001", "neware")
    _insert_row(db, "neware_cell_001", 0.0, 3.7, 0.004, temperature_c=None)

    issues = check_quality(db)

    assert not any(i.rule == "unexpected_missing_temperature" for i in issues)


# -- persistence -------------------------------------------------------------- #


def test_save_issues_persists_rows(db):
    issues = [
        Issue(rule="voltage_out_of_range", severity="critical", message="bad", test_id="x"),
        Issue(rule="high_skip_rate", severity="warning", message="meh", test_id="y"),
    ]

    save_issues(db, issues)

    rows = db.query("SELECT test_id, rule, severity, message FROM data_quality_issues ORDER BY id")
    assert rows == [
        ("x", "voltage_out_of_range", "critical", "bad"),
        ("y", "high_skip_rate", "warning", "meh"),
    ]


# -- the gate, end to end ------------------------------------------------------ #


def test_quality_gate_is_clean_against_the_bundled_dataset(tmp_path):
    """The reference dataset's known quirks (skipped rows, the mV rescale, the
    missing Neware temperature channel) must not trip the gate — if they did,
    every ordinary run would fail CI for reasons that are already understood
    and documented, not a regression."""
    issues = quality_gate.run(str(REPO_DATA), db_path=tmp_path / "battery.sqlite3")

    assert issues == []


def test_quality_gate_main_exits_zero_on_the_bundled_dataset(tmp_path, capsys):
    exit_code = quality_gate.main(
        ["--data-root", str(REPO_DATA), "--db-path", str(tmp_path / "battery.sqlite3")]
    )

    assert exit_code == 0
    assert "no issues found" in capsys.readouterr().out


def test_quality_gate_main_fails_and_annotates_on_a_critical_issue(tmp_path, capsys):
    data_root = tmp_path / "data"
    write_file(
        data_root,
        "cycler_b_neware",
        "cell_001.csv",
        NEWARE_HEADER + "0,3.7,0.004,1,0.02\n1,9999.0,0.004,1,0.03\n",  # corrupt voltage
    )
    db_path = tmp_path / "battery.sqlite3"

    exit_code = quality_gate.main(["--data-root", str(data_root), "--db-path", str(db_path)])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "::error" in output
    assert "voltage_out_of_range" in output

    with Database.connect(db_path=db_path) as db:
        saved = db.query("SELECT rule, severity FROM data_quality_issues")
    assert ("voltage_out_of_range", "critical") in saved


def test_quality_gate_run_persists_contract_issues_for_an_unreadable_file(tmp_path):
    data_root = tmp_path / "data"
    write_file(data_root, "cycler_b_neware", "broken.csv", "a,b\n1,2,3,4,5\n")
    db_path = tmp_path / "battery.sqlite3"

    issues = quality_gate.run(str(data_root), db_path=db_path)

    assert any(i.rule == "file_produced_no_usable_rows" for i in issues)
    with Database.connect(db_path=db_path) as db:
        saved = db.query("SELECT rule FROM data_quality_issues")
    assert ("file_produced_no_usable_rows",) in saved


def test_quality_gate_run_reingests_the_data_root(tmp_path):
    """`run` performs a real ingest, not just a check against whatever is
    already in the database."""
    data_root = tmp_path / "data"
    write_file(data_root, "cycler_b_neware", "cell_001.csv", NEWARE_HEADER + "0,3.6,0.004,1,0.02\n")
    db_path = tmp_path / "battery.sqlite3"

    quality_gate.run(str(data_root), db_path=db_path)

    with Database.connect(db_path=db_path) as db:
        assert db.query_one("SELECT COUNT(*) FROM tests")[0] == 1
