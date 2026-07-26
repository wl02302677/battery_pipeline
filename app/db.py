from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import psycopg2


def build_database_url(db_path: Optional[str] = None) -> str:
    """Build a database URL for SQLite or PostgreSQL."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    resolved_db_path = Path(db_path or os.getenv("BATTERY_DB_PATH", "battery.sqlite3")).resolve()
    return f"sqlite:///{resolved_db_path}"


def connect_database(database_url: str, retries: int = 10, delay_seconds: float = 2.0):
    """Connect to PostgreSQL with retries and a short delay for container startup."""
    if not database_url.startswith("postgresql://"):
        return None

    last_error = None
    for attempt in range(retries):
        try:
            return psycopg2.connect(database_url)
        except psycopg2.OperationalError as exc:
            last_error = exc
            if attempt == retries - 1:
                raise
            time.sleep(delay_seconds)

    raise last_error
