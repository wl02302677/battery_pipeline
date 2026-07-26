from pathlib import Path

from app.db import build_database_url


def test_build_database_url_prefers_explicit_postgres_config(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/battery")

    assert build_database_url() == "postgresql://postgres:postgres@db:5432/battery"


def test_build_database_url_falls_back_to_sqlite_when_no_env_present(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_path = tmp_path / "battery.sqlite3"

    assert build_database_url(db_path=str(db_path)) == f"sqlite:///{db_path}"
