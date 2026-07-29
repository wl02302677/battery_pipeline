"""REST API over the normalized battery data.

All backend-specific SQL handling lives in `app.db`, so each endpoint is written
once regardless of whether it is talking to SQLite or PostgreSQL.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg2
from fastapi import FastAPI, HTTPException, Query
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.db import Database, build_database_url, is_postgres_url

logger = logging.getLogger(__name__)

#: Connection errors that mean "the database is not reachable right now".
DATABASE_ERRORS = (psycopg2.Error, sqlite3.Error, OSError)

DEFAULT_PAGE_SIZE = 1_000
MAX_PAGE_SIZE = 50_000

#: The optional bonus dashboard: a single static file, no build step, no JS
#: dependencies, so it needs nothing beyond what `docker compose up` already runs.
DASHBOARD_PATH = Path(__file__).parent / "static" / "dashboard.html"

# The FastAPI application. `--reload`/uvicorn import this object by name
# ("app.api:app") to start the server.
app = FastAPI(
    title="Battery ETL API",
    version="1.0.0",
    description=(
        "Query normalized battery cycler data. Timestamps are seconds since the "
        "start of each test; currents are amps, voltages volts, capacities amp-hours."
    ),
)


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Serve the visualization dashboard at the API root."""
    return FileResponse(DASHBOARD_PATH)


# --------------------------------------------------------------------------- #
# Response models (they also give the generated OpenAPI schema real types)
# --------------------------------------------------------------------------- #


class Health(BaseModel):
    """Response shape for GET /health."""

    status: str
    backend: str
    tests: int


class TestSummary(BaseModel):
    """One row of GET /tests, or the body of GET /tests/{test_id}."""

    test_id: str
    cycler: str
    source_path: str
    rows_loaded: int
    rows_skipped: int = Field(description="Source rows dropped for missing required values")
    rows_duplicated: int = Field(description="Exact duplicate source rows dropped")
    rows_rescaled: int = Field(description="Rows whose voltage was converted from mV to V")
    start_offset_s: float | None = Field(
        default=None,
        description="Raw source clock value at the start of this test, before rebasing to zero",
    )
    duration_s: float | None = None
    cycle_count: int
    ingested_at: str | None = None


class TimeseriesPoint(BaseModel):
    """One sample inside a GET /tests/{test_id}/timeseries page."""

    timestamp_s: float | None
    voltage_v: float | None
    current_a: float | None
    temperature_c: float | None
    capacity_ah: float | None
    cycle_index: int | None


class TimeseriesPage(BaseModel):
    """Response shape for GET /tests/{test_id}/timeseries: one page of samples
    plus enough metadata for the client to fetch the next page.
    """

    test_id: str
    total: int = Field(description="Rows matching the filters, ignoring pagination")
    returned: int
    limit: int
    offset: int
    next_offset: int | None = Field(
        default=None, description="Offset for the next page, or null when exhausted"
    )
    data: list[TimeseriesPoint]


class CycleSummary(BaseModel):
    """One row of GET /tests/{test_id}/cycles: stats for a single cycle."""

    cycle_index: int
    sample_count: int
    start_time_s: float | None
    end_time_s: float | None
    duration_s: float | None
    min_voltage_v: float | None
    max_voltage_v: float | None
    min_current_a: float | None
    max_current_a: float | None
    min_temperature_c: float | None
    max_temperature_c: float | None
    capacity_ah: float | None = Field(
        default=None, description="Span of the source capacity channel across the cycle"
    )
    charge_capacity_ah: float | None = Field(
        default=None, description="Capacity span over samples with positive current"
    )
    discharge_capacity_ah: float | None = Field(
        default=None, description="Capacity span over samples with negative current"
    )


# --------------------------------------------------------------------------- #
# Database access
# --------------------------------------------------------------------------- #


@contextmanager
def database() -> Iterator[Database]:
    """Open a connection for one request, mapping outages onto 503.

    `retries=1`: the ETL waits for a slow container on startup, but a web request
    must fail fast rather than hold the worker for the full retry budget.
    """
    try:
        db = Database.connect(db_path=os.getenv("BATTERY_DB_PATH"), retries=1, delay_seconds=0)
    except DATABASE_ERRORS as exc:
        # Could not even open a connection — the database is down or not
        # reachable yet. Report this as a 503, not a 500.
        logger.warning("Database unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable") from exc

    try:
        yield db
    except DATABASE_ERRORS as exc:
        # A query failed after the connection was already open — still a
        # "the database isn't working right now" situation from the caller's
        # point of view, so it gets the same 503 treatment.
        logger.warning("Database query failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    finally:
        db.close()


# Shared column list for the two endpoints that return a TestSummary.
TEST_COLUMNS_SQL = """
    test_id, cycler, source_path, rows_loaded, rows_skipped, rows_duplicated,
    rows_rescaled, start_offset_s, last_timestamp_s, cycle_count, ingested_at
"""


def _test_summary(row: tuple) -> TestSummary:
    """Turn one raw `tests` row (in TEST_COLUMNS_SQL order) into a TestSummary."""
    (
        test_id,
        cycler,
        source_path,
        rows_loaded,
        rows_skipped,
        rows_duplicated,
        rows_rescaled,
        start_offset_s,
        last_timestamp_s,
        cycle_count,
        ingested_at,
    ) = row
    return TestSummary(
        test_id=test_id,
        cycler=cycler,
        source_path=source_path,
        rows_loaded=rows_loaded,
        rows_skipped=rows_skipped,
        rows_duplicated=rows_duplicated,
        rows_rescaled=rows_rescaled,
        start_offset_s=start_offset_s,
        # Timestamps are rebased to zero, so the last one is the test duration.
        duration_s=last_timestamp_s,
        cycle_count=cycle_count,
        ingested_at=ingested_at,
    )


def _require_test(db: Database, test_id: str) -> None:
    """Raise 404 when a test does not exist.

    Checked separately from the data query so that "no such test" (404) is not
    confused with "this test has no rows matching your filters" (200 + empty).
    """
    if db.query_one("SELECT 1 FROM tests WHERE test_id = ?", (test_id,)) is None:
        raise HTTPException(status_code=404, detail=f"unknown test_id: {test_id}")


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@app.get("/health", response_model=Health, tags=["meta"])
def health() -> Health:
    """Liveness probe that also confirms the schema is queryable."""
    backend = "postgresql" if is_postgres_url(build_database_url()) else "sqlite"
    with database() as db:
        row = db.query_one("SELECT COUNT(*) FROM tests")
    return Health(status="ok", backend=backend, tests=row[0] if row else 0)


@app.get("/tests", response_model=list[TestSummary], tags=["tests"])
def list_tests(
    cycler: str | None = Query(default=None, description="Filter by cycler name"),
) -> list[TestSummary]:
    """List every ingested test with its ingestion and data quality counters."""
    # Build the query: filter by cycler only when one was requested, then
    # always return results sorted by test_id for a stable ordering.
    statement = f"SELECT {TEST_COLUMNS_SQL} FROM tests"
    params: list[object] = []
    if cycler:
        statement += " WHERE cycler = ?"
        params.append(cycler)
    statement += " ORDER BY test_id"

    with database() as db:
        rows = db.query(statement, params)
    return [_test_summary(row) for row in rows]


@app.get("/tests/{test_id}", response_model=TestSummary, tags=["tests"])
def get_test(test_id: str = PathParam(description="e.g. biologic_cell_001")) -> TestSummary:
    """Return the summary for a single test."""
    with database() as db:
        row = db.query_one(f"SELECT {TEST_COLUMNS_SQL} FROM tests WHERE test_id = ?", (test_id,))
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown test_id: {test_id}")
    return _test_summary(row)


@app.get("/tests/{test_id}/timeseries", response_model=TimeseriesPage, tags=["tests"])
def get_timeseries(
    test_id: str,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    start_s: float | None = Query(default=None, description="Inclusive lower bound on time"),
    end_s: float | None = Query(default=None, description="Inclusive upper bound on time"),
    cycle_index: int | None = Query(default=None, description="Restrict to one cycle"),
) -> TimeseriesPage:
    """Return a page of the normalized time series for a test.

    A single test can hold far more rows than a client wants at once, so results
    are always paginated and can be narrowed by time window or cycle.
    """
    # Build up the WHERE clause from whichever optional filters were passed.
    filters = ["test_id = ?"]
    params: list[object] = [test_id]
    if start_s is not None:
        filters.append("timestamp_s >= ?")
        params.append(start_s)
    if end_s is not None:
        filters.append("timestamp_s <= ?")
        params.append(end_s)
    if cycle_index is not None:
        filters.append("cycle_index = ?")
        params.append(cycle_index)
    where_clause = " AND ".join(filters)

    with database() as db:
        _require_test(db, test_id)

        # Total count (for the response envelope), then the actual page of rows.
        total_row = db.query_one(f"SELECT COUNT(*) FROM timeseries WHERE {where_clause}", params)
        total = total_row[0] if total_row else 0

        rows = db.query(
            f"""
            SELECT timestamp_s, voltage_v, current_a, temperature_c, capacity_ah, cycle_index
            FROM timeseries
            WHERE {where_clause}
            ORDER BY timestamp_s, id
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )

    data = [
        TimeseriesPoint(
            timestamp_s=timestamp_s,
            voltage_v=voltage_v,
            current_a=current_a,
            temperature_c=temperature_c,
            capacity_ah=capacity_ah,
            cycle_index=cycle_index_value,
        )
        for timestamp_s, voltage_v, current_a, temperature_c, capacity_ah, cycle_index_value in rows
    ]
    # There's a next page only if this page didn't reach the total row count.
    next_offset = offset + len(data) if offset + len(data) < total else None

    return TimeseriesPage(
        test_id=test_id,
        total=total,
        returned=len(data),
        limit=limit,
        offset=offset,
        next_offset=next_offset,
        data=data,
    )


@app.get("/tests/{test_id}/cycles", response_model=list[CycleSummary], tags=["tests"])
def get_cycles(test_id: str) -> list[CycleSummary]:
    """Return per-cycle summary statistics for a test.

    Capacity is reported as the span of the source capacity channel within the
    cycle, split by current sign, rather than coulomb-counted from current.
    """
    with database() as db:
        _require_test(db, test_id)
        # One row per cycle_index, with min/max/aggregate stats computed by
        # the database rather than in Python.
        rows = db.query(
            """
            SELECT
                cycle_index,
                COUNT(*),
                MIN(timestamp_s),
                MAX(timestamp_s),
                MIN(voltage_v),
                MAX(voltage_v),
                MIN(current_a),
                MAX(current_a),
                MIN(temperature_c),
                MAX(temperature_c),
                MAX(capacity_ah) - MIN(capacity_ah),
                MAX(CASE WHEN current_a > 0 THEN capacity_ah END)
                    - MIN(CASE WHEN current_a > 0 THEN capacity_ah END),
                MAX(CASE WHEN current_a < 0 THEN capacity_ah END)
                    - MIN(CASE WHEN current_a < 0 THEN capacity_ah END)
            FROM timeseries
            WHERE test_id = ? AND cycle_index IS NOT NULL
            GROUP BY cycle_index
            ORDER BY cycle_index
            """,
            (test_id,),
        )

    summaries = []
    for row in rows:
        (
            cycle_index,
            sample_count,
            start_time_s,
            end_time_s,
            min_voltage_v,
            max_voltage_v,
            min_current_a,
            max_current_a,
            min_temperature_c,
            max_temperature_c,
            capacity_ah,
            charge_capacity_ah,
            discharge_capacity_ah,
        ) = row
        duration_s = (
            end_time_s - start_time_s
            if start_time_s is not None and end_time_s is not None
            else None
        )
        summaries.append(
            CycleSummary(
                cycle_index=cycle_index,
                sample_count=sample_count,
                start_time_s=start_time_s,
                end_time_s=end_time_s,
                duration_s=duration_s,
                min_voltage_v=min_voltage_v,
                max_voltage_v=max_voltage_v,
                min_current_a=min_current_a,
                max_current_a=max_current_a,
                min_temperature_c=min_temperature_c,
                max_temperature_c=max_temperature_c,
                capacity_ah=capacity_ah,
                charge_capacity_ah=charge_capacity_ah,
                discharge_capacity_ah=discharge_capacity_ah,
            )
        )
    return summaries
