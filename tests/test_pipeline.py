import sqlite3
from pathlib import Path

import pytest

from app.etl.pipeline import ingest_directory

REPO_DATA = Path(__file__).resolve().parents[1] / "data"

BIOLOGIC_HEADER = "time/s\tvoltage_measured\tI/mA\tTemperature/°C\tcycle number\n"
NEWARE_HEADER = "Time [s],Voltage [V],Current [A],Cycle,Capacity [Ah]\n"


def write_file(root: Path, cycler_dir: str, name: str, content: str) -> Path:
    directory = root / cycler_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def repo_db(tmp_path):
    """Ingest the bundled dataset once into a throwaway SQLite file."""
    db_path = tmp_path / "battery.sqlite3"
    summary = ingest_directory(REPO_DATA, db_path=db_path)
    return db_path, summary


# -- basic ingestion ------------------------------------------------------ #


def test_ingest_directory_creates_timeseries_rows(tmp_path):
    data_root = tmp_path / "data"
    write_file(
        data_root,
        "cycler_a_biologic",
        "cell_001.txt",
        BIOLOGIC_HEADER + "0\t3.2\t500\t25\t1\n" + "1\t3.1\t450\t24\t1\n",
    )

    db_path = tmp_path / "battery.sqlite3"
    summary = ingest_directory(data_root, db_path=db_path)

    assert summary["tests_loaded"] == 1
    assert summary["rows_loaded"] == 2

    with sqlite3.connect(db_path) as conn:
        test_rows = conn.execute("SELECT test_id, cycler FROM tests").fetchall()
        series_rows = conn.execute(
            "SELECT test_id, timestamp_s, voltage_v, current_a, temperature_c, cycle_index"
            " FROM timeseries ORDER BY timestamp_s"
        ).fetchall()

    assert test_rows[0][0] == "biologic_cell_001"
    assert test_rows[0][1] == "biologic"
    assert series_rows[0][1] == 0.0
    assert series_rows[0][2] == 3.2
    assert series_rows[0][3] == 0.5
    assert series_rows[0][4] == 25.0
    assert series_rows[0][5] == 1


def test_ingest_directory_handles_repository_cyclers(repo_db):
    db_path, summary = repo_db

    assert summary["files_discovered"] == 12
    assert summary["tests_loaded"] == 12
    assert summary["rows_loaded"] > 100

    with sqlite3.connect(db_path) as conn:
        cycler_counts = dict(
            conn.execute("SELECT cycler, COUNT(*) FROM tests GROUP BY cycler").fetchall()
        )
        biologic_row = conn.execute(
            "SELECT timestamp_s, voltage_v, current_a, temperature_c FROM timeseries"
            " WHERE test_id = 'biologic_cell_001' ORDER BY timestamp_s LIMIT 1"
        ).fetchone()
        novonix_row = conn.execute(
            "SELECT timestamp_s, voltage_v, current_a FROM timeseries"
            " WHERE test_id = 'novonix_cell_001' ORDER BY timestamp_s LIMIT 2"
        ).fetchall()[1]

    assert cycler_counts == {"biologic": 1, "neware": 10, "novonix": 1}
    assert biologic_row[1] > 0
    assert biologic_row[3] > 0
    assert novonix_row[0] == pytest.approx(1.28016)
    assert novonix_row[1] > 0
    assert novonix_row[2] > 0


# -- units end to end ----------------------------------------------------- #


@pytest.mark.parametrize(
    ("test_id", "low", "high"),
    [
        # Regression: every cycler used to be divided by 1000, which pushed the
        # neware and novonix currents down to microamps.
        ("neware_cell_001", 1e-3, 1.0),
        ("novonix_cell_001", 0.1, 1.0),
        ("biologic_cell_001", 0.1, 1.0),
    ],
)
def test_currents_land_in_a_plausible_amp_range(repo_db, test_id, low, high):
    db_path, _ = repo_db
    with sqlite3.connect(db_path) as conn:
        peak = conn.execute(
            "SELECT MAX(ABS(current_a)) FROM timeseries WHERE test_id = ?", (test_id,)
        ).fetchone()[0]
    assert low <= peak <= high


def test_no_voltage_survives_in_millivolts(repo_db):
    """The BioLogic export mixes V and mV in one column."""
    db_path, summary = repo_db
    with sqlite3.connect(db_path) as conn:
        peak = conn.execute("SELECT MAX(voltage_v) FROM timeseries").fetchone()[0]
        rescaled = conn.execute(
            "SELECT rows_rescaled FROM tests WHERE test_id = 'biologic_cell_001'"
        ).fetchone()[0]

    assert peak < 5.0
    assert rescaled > 0
    assert summary["rows_rescaled"] == rescaled


def test_capacity_is_ingested_for_every_cycler(repo_db):
    db_path, _ = repo_db
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT cycler, MAX(capacity_ah) FROM tests JOIN timeseries USING (test_id)"
            " GROUP BY cycler"
        ).fetchall()

    assert {cycler for cycler, _ in rows} == {"biologic", "neware", "novonix"}
    for cycler, peak in rows:
        assert peak is not None and peak > 0, cycler


# -- timestamps ----------------------------------------------------------- #


def test_timestamps_are_rebased_to_the_start_of_each_test(tmp_path):
    """The neware exports share one lab clock, so raw time is not test time."""
    data_root = tmp_path / "data"
    write_file(
        data_root,
        "cycler_b_neware",
        "cell_002.csv",
        NEWARE_HEADER + "78949.9,3.6,0.004,1,0.02\n78950.9,3.61,0.004,1,0.021\n",
    )

    db_path = tmp_path / "battery.sqlite3"
    ingest_directory(data_root, db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        timestamps = [
            row[0]
            for row in conn.execute(
                "SELECT timestamp_s FROM timeseries ORDER BY timestamp_s"
            ).fetchall()
        ]
        offset = conn.execute("SELECT start_offset_s FROM tests").fetchone()[0]

    assert timestamps == [0.0, 1.0]
    assert offset == pytest.approx(78949.9)


def test_every_bundled_test_starts_at_zero_and_records_its_offset(repo_db):
    db_path, _ = repo_db
    with sqlite3.connect(db_path) as conn:
        first_timestamps = conn.execute(
            "SELECT test_id, MIN(timestamp_s) FROM timeseries GROUP BY test_id"
        ).fetchall()
        offsets = dict(conn.execute("SELECT test_id, start_offset_s FROM tests").fetchall())

    for test_id, first in first_timestamps:
        assert first == 0.0, test_id
    # The later neware files start well into the shared clock.
    assert offsets["neware_cell_010"] > 500_000
    assert offsets["neware_cell_001"] == 0.0


def test_rows_are_stored_in_time_order_even_when_the_source_is_not(tmp_path):
    data_root = tmp_path / "data"
    write_file(
        data_root,
        "cycler_b_neware",
        "cell_001.csv",
        NEWARE_HEADER + "2,3.6,0.004,1,0.02\n0,3.5,0.004,1,0.01\n1,3.55,0.004,1,0.015\n",
    )

    db_path = tmp_path / "battery.sqlite3"
    ingest_directory(data_root, db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT timestamp_s FROM timeseries ORDER BY id").fetchall()
    timestamps = [row[0] for row in rows]

    assert timestamps == [0.0, 1.0, 2.0]


# -- malformed input ------------------------------------------------------ #


def test_ingest_directory_skips_rows_with_missing_required_values(tmp_path):
    data_root = tmp_path / "data"
    write_file(
        data_root,
        "cycler_a_biologic",
        "cell_002.txt",
        BIOLOGIC_HEADER + "0\t3.2\t500\t25\t1\n" + "1\tbad\t450\t24\t1\n" + "2\t3.1\t\t24\t1\n",
    )

    db_path = tmp_path / "battery.sqlite3"
    summary = ingest_directory(data_root, db_path=db_path)

    assert summary["tests_loaded"] == 1
    assert summary["rows_loaded"] == 1
    assert summary["rows_skipped"] == 2


def test_exact_duplicate_rows_are_dropped(tmp_path):
    data_root = tmp_path / "data"
    write_file(
        data_root,
        "cycler_b_neware",
        "cell_003.csv",
        NEWARE_HEADER
        + "0,3.2,0.004,2,0.02\n"
        + "0,3.2,0.004,2,0.02\n"  # byte-for-byte repeat, including the timestamp
        + "1,3.21,0.004,2,0.021\n",
    )

    db_path = tmp_path / "battery.sqlite3"
    summary = ingest_directory(data_root, db_path=db_path)

    assert summary["rows_loaded"] == 2
    assert summary["rows_duplicated"] == 1

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT rows_duplicated FROM tests").fetchone()[0] == 1


def test_the_bundled_duplicate_rows_are_dropped(repo_db):
    db_path, summary = repo_db
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM timeseries WHERE test_id = 'neware_cell_003'"
        ).fetchone()[0]

    assert count == 1000  # the file holds 1020 rows, 20 of them repeats
    assert summary["rows_duplicated"] > 0


def test_a_file_outside_a_cycler_directory_is_skipped(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "loose_export.csv").write_text(NEWARE_HEADER + "0,3.6,0.004,1,0.02\n")

    summary = ingest_directory(data_root, db_path=tmp_path / "battery.sqlite3")

    assert summary["files_discovered"] == 1
    assert summary["files_skipped"] == 1
    assert summary["tests_loaded"] == 0


def test_an_unparseable_file_does_not_abort_the_run(tmp_path):
    data_root = tmp_path / "data"
    write_file(data_root, "cycler_b_neware", "broken.csv", "a,b\n1,2,3,4,5\n")
    write_file(data_root, "cycler_b_neware", "cell_001.csv", NEWARE_HEADER + "0,3.6,0.004,1,0.02\n")

    summary = ingest_directory(data_root, db_path=tmp_path / "battery.sqlite3")

    assert summary["files_skipped"] == 1
    assert summary["tests_loaded"] == 1
    assert "skipped_file_paths" in summary


def test_a_file_with_no_usable_rows_creates_no_test_record(tmp_path):
    data_root = tmp_path / "data"
    write_file(data_root, "cycler_a_biologic", "empty.txt", BIOLOGIC_HEADER + "\t\t\t\t\n")

    summary = ingest_directory(data_root, db_path=tmp_path / "battery.sqlite3")

    assert summary["tests_loaded"] == 0
    assert summary["files_skipped"] == 1


# -- idempotency ---------------------------------------------------------- #


def test_reingesting_replaces_rows_instead_of_appending(tmp_path):
    """`docker compose up` on a persisted volume re-runs the ETL."""
    data_root = tmp_path / "data"
    write_file(
        data_root,
        "cycler_b_neware",
        "cell_001.csv",
        NEWARE_HEADER + "0,3.6,0.004,1,0.02\n1,3.61,0.004,1,0.021\n",
    )
    db_path = tmp_path / "battery.sqlite3"

    first = ingest_directory(data_root, db_path=db_path)
    second = ingest_directory(data_root, db_path=db_path)

    assert first == second
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM timeseries").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM tests").fetchone()[0] == 1
