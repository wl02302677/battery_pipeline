import sqlite3
from pathlib import Path

from app.etl.pipeline import ingest_directory


def test_ingest_directory_creates_timeseries_rows(tmp_path):
    data_root = tmp_path / "data"
    cycler_dir = data_root / "cycler_a_biologic"
    cycler_dir.mkdir(parents=True)

    sample_file = cycler_dir / "cell_001.txt"
    sample_file.write_text(
        "time/s\tvoltage_measured\tI/mA\tTemperature/°C\tcycle number\n"
        "0\t3.2\t500\t25\t1\n"
        "1\t3.1\t450\t24\t1\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "battery.sqlite3"
    summary = ingest_directory(data_root, db_path=db_path)

    assert summary["tests_loaded"] == 1
    assert summary["rows_loaded"] == 2

    with sqlite3.connect(db_path) as conn:
        test_rows = conn.execute("SELECT test_id, cycler FROM tests").fetchall()
        series_rows = conn.execute("SELECT test_id, timestamp_s, voltage_v, current_a, temperature_c, cycle_index FROM timeseries ORDER BY timestamp_s").fetchall()

    assert test_rows[0][0] == "biologic_cell_001"
    assert test_rows[0][1] == "biologic"
    assert series_rows[0][1] == 0.0
    assert series_rows[0][2] == 3.2
    assert series_rows[0][3] == 0.5
    assert series_rows[0][4] == 25.0
    assert series_rows[0][5] == 1


def test_ingest_directory_handles_repository_cyclers(tmp_path):
    repo_data = Path(__file__).resolve().parents[1] / "data"
    db_path = tmp_path / "battery.sqlite3"

    summary = ingest_directory(repo_data, db_path=db_path)

    assert summary["tests_loaded"] == 12
    assert summary["rows_loaded"] > 100
    assert summary["rows_skipped"] >= 0

    with sqlite3.connect(db_path) as conn:
        cycler_counts = dict(conn.execute("SELECT cycler, COUNT(*) FROM tests GROUP BY cycler").fetchall())
        biologic_row = conn.execute(
            "SELECT timestamp_s, voltage_v, current_a, temperature_c FROM timeseries WHERE test_id = 'biologic_cell_001' ORDER BY id LIMIT 1"
        ).fetchone()
        novonix_row = conn.execute(
            "SELECT timestamp_s, voltage_v, current_a FROM timeseries WHERE test_id = 'novonix_cell_001' ORDER BY id LIMIT 2"
        ).fetchall()[1]

    assert cycler_counts["biologic"] == 1
    assert cycler_counts["neware"] == 10
    assert cycler_counts["novonix"] == 1
    assert biologic_row[1] > 0
    assert biologic_row[3] > 0
    assert abs(novonix_row[0] - 1.28016) < 1e-6
    assert novonix_row[1] > 0
    assert novonix_row[2] > 0


def test_ingest_directory_skips_rows_with_missing_required_values(tmp_path):
    data_root = tmp_path / "data"
    cycler_dir = data_root / "cycler_a_biologic"
    cycler_dir.mkdir(parents=True)

    sample_file = cycler_dir / "cell_002.txt"
    sample_file.write_text(
        "time/s\tvoltage_measured\tI/mA\tTemperature/°C\tcycle number\n"
        "0\t3.2\t500\t25\t1\n"
        "1\tbad\t450\t24\t1\n"
        "2\t3.1\t\t24\t1\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "battery.sqlite3"
    summary = ingest_directory(data_root, db_path=db_path)

    assert summary["tests_loaded"] == 1
    assert summary["rows_loaded"] == 1
    assert summary["rows_skipped"] == 2
