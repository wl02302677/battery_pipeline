import os
import sqlite3
from pathlib import Path
from typing import Any

import psycopg2
from fastapi import FastAPI, HTTPException

from app.db import build_database_url, connect_database

app = FastAPI(title="Battery ETL API")


def get_db_path() -> Path:
    return Path(os.getenv("BATTERY_DB_PATH", "battery.sqlite3")).resolve()


def get_connection():
    database_url = build_database_url(db_path=str(get_db_path()))
    if database_url.startswith("postgresql://"):
        return connect_database(database_url)
    return sqlite3.connect(get_db_path())


@app.get("/tests")
def list_tests() -> list[dict[str, Any]]:
    try:
        conn = get_connection()
    except (psycopg2.Error, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc

    try:
        if isinstance(conn, psycopg2.extensions.connection):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT test_id, cycler, source_path FROM tests ORDER BY test_id"
            )
            rows = cursor.fetchall()
        else:
            rows = conn.execute(
                "SELECT test_id, cycler, source_path FROM tests ORDER BY test_id"
            ).fetchall()
    except (psycopg2.Error, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    finally:
        conn.close()
    return [{"test_id": test_id, "cycler": cycler, "source_path": source_path} for test_id, cycler, source_path in rows]


@app.get("/tests/{test_id}/timeseries")
def get_timeseries(test_id: str) -> list[dict[str, Any]]:
    try:
        conn = get_connection()
    except (psycopg2.Error, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc

    try:
        if isinstance(conn, psycopg2.extensions.connection):
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT timestamp_s, voltage_v, current_a, temperature_c
                FROM timeseries
                WHERE test_id = %s
                ORDER BY id
                """,
                (test_id,),
            )
            rows = cursor.fetchall()
        else:
            rows = conn.execute(
                """
                SELECT timestamp_s, voltage_v, current_a, temperature_c
                FROM timeseries
                WHERE test_id = ?
                ORDER BY id
                """,
                (test_id,),
            ).fetchall()
    except (psycopg2.Error, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    finally:
        conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="test not found")

    return [
        {
            "timestamp_s": timestamp_s,
            "voltage_v": voltage_v,
            "current_a": current_a,
            "temperature_c": temperature_c,
        }
        for timestamp_s, voltage_v, current_a, temperature_c in rows
    ]


@app.get("/tests/{test_id}/cycles")
def get_cycles(test_id: str) -> list[dict[str, Any]]:
    try:
        conn = get_connection()
    except (psycopg2.Error, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc

    try:
        if isinstance(conn, psycopg2.extensions.connection):
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT cycle_index, COUNT(*) AS sample_count, MIN(voltage_v) AS min_voltage_v, MAX(voltage_v) AS max_voltage_v
                FROM timeseries
                WHERE test_id = %s AND cycle_index IS NOT NULL
                GROUP BY cycle_index
                ORDER BY cycle_index
                """,
                (test_id,),
            )
            rows = cursor.fetchall()
        else:
            rows = conn.execute(
                """
                SELECT cycle_index, COUNT(*) AS sample_count, MIN(voltage_v) AS min_voltage_v, MAX(voltage_v) AS max_voltage_v
                FROM timeseries
                WHERE test_id = ? AND cycle_index IS NOT NULL
                GROUP BY cycle_index
                ORDER BY cycle_index
                """,
                (test_id,),
            ).fetchall()
    except (psycopg2.Error, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    finally:
        conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="test not found")

    return [
        {
            "cycle_index": cycle_index,
            "sample_count": sample_count,
            "min_voltage_v": min_voltage_v,
            "max_voltage_v": max_voltage_v,
        }
        for cycle_index, sample_count, min_voltage_v, max_voltage_v in rows
    ]
