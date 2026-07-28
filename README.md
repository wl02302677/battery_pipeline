# Battery Pipeline

An ETL pipeline and REST API for battery cycler data. It ingests raw exports from
three different cyclers, normalizes them into one schema, loads them into
PostgreSQL, and serves them over HTTP with a small visualization dashboard.

The task brief this repository answers is in [docs/brief.md](docs/brief.md).
Deeper rationale for anything below lives in
[docs/data_contract.md](docs/data_contract.md) (normalization rules) and
[docs/test_logic.md](docs/test_logic.md) (testing strategy) — this file is the
short version.

## Run it

```bash
docker compose up --build
```

Starts PostgreSQL, waits for it to be healthy, runs the ETL to completion,
then starts the API. No other setup is needed.

- Dashboard: [http://localhost:8000/](http://localhost:8000/)
- Interactive API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

```bash
curl localhost:8000/health
curl localhost:8000/tests
curl "localhost:8000/tests/biologic_cell_001/timeseries?limit=5"
curl localhost:8000/tests/novonix_cell_001/cycles
```

### Without Docker

Falls back to SQLite, so no database server is required:

```bash
python -m pip install -r requirements-dev.txt
python -m app.etl.pipeline --data-root data --db-path battery.sqlite3
BATTERY_DB_PATH=battery.sqlite3 python -m uvicorn app.api:app --reload
```

### Tests

```bash
pytest                                   # the full suite, on SQLite
ruff check . && ruff format --check .    # lint

# Exercise the PostgreSQL path too (otherwise these tests skip):
DATABASE_URL=postgresql://battery:battery@localhost:5432/battery pytest tests/test_postgres.py

# Run the same contract/quality checks CI runs:
python -m app.etl.quality_gate --data-root data
```

## How it's put together

```
data/cycler_*/            raw exports, one file per test
  |
  v
app/etl/contract.py       per-cycler column map: which column, which unit, per field
app/etl/pipeline.py       discover -> hash -> normalize -> validate -> dedupe -> rebase -> load
app/etl/quality.py        contract + quality checks over the loaded data
app/etl/quality_gate.py   CI entry point: ingest, check, persist findings, set exit code
  |
  v
app/db.py                 PostgreSQL or SQLite behind one interface
  |
  v
app/api.py                FastAPI read layer + the dashboard route
app/static/dashboard.html single-page visualization, no build step
```

`DATABASE_URL` selects the backend everywhere; unset, everything falls back to
a local SQLite file, which is what tests and local runs use — same code path
either way.

## Schema

`tests` — one row per source file: identity (`test_id`, `cycler`,
`source_path`, `source_hash`), and per-test counters the API exposes directly
(`rows_loaded`/`skipped`/`duplicated`/`rescaled`, `cycle_count`,
`start_offset_s`, `ingested_at`).

`timeseries` — one row per sample: `test_id`, `timestamp_s` (seconds since the
test started), `voltage_v`, `current_a` (positive = charge), `temperature_c`
(null when the cycler doesn't report it), `capacity_ah`, `cycle_index`.
Indexed on `(test_id, timestamp_s)` and `(test_id, cycle_index)`, since every
query filters by `test_id`.

`data_quality_issues` — append-only log of findings from the
[quality gate](#data-quality-gate) below.

Full column-by-column rationale: [docs/data_contract.md](docs/data_contract.md).

## API


| Endpoint                          | Notes                                                                                                 |
| --------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `GET /`                           | The dashboard.                                                                                        |
| `GET /health`                     | Backend in use and test count; used by the compose healthcheck.                                       |
| `GET /tests`                      | All tests, with cycler and quality counters.`?cycler=neware` to filter.                               |
| `GET /tests/{test_id}`            | One test's summary.                                                                                   |
| `GET /tests/{test_id}/timeseries` | Paginated (`limit`/`offset`, default 1000, max 50000), filterable by `start_s`/`end_s`/`cycle_index`. |
| `GET /tests/{test_id}/cycles`     | Per-cycle stats: duration, min/max voltage/current/temperature, capacity split by current direction.  |

`/timeseries` returns `{total, returned, limit, offset, next_offset, data}` —
a page envelope, not a bare array, so a client knows whether more data exists.
`404` means the `test_id` doesn't exist; a known test whose filters match
nothing is `200` with an empty `data`. `503` means the database is
unreachable; `422` is an out-of-range pagination parameter.

## Dashboard

`GET /` serves `app/static/dashboard.html` — plain JS and `<canvas>`, no build
step, no external dependency. Grouped test picker; voltage/current/temperature
as three separate single-axis charts (never dual-axis — they're unrelated
scales); crosshair-and-tooltip on hover; a per-test KPI row; the per-cycle
table; light/dark mode. Checked with a headless Chromium browser via
Playwright, not just an HTTP status check — see
[docs/test_logic.md#dashboard](docs/test_logic.md#dashboard).

## Key assumptions

The full reasoning for each of these is in
[docs/data_contract.md](docs/data_contract.md); this is the summary.

- **Units are declared per source column, not per field.** Only BioLogic
  reports mA/mAh; only Novonix reports hours. A conversion keyed to the field
  rather than the column would silently misconvert the other two cyclers.
- **BioLogic mixes V and mV in one column** (210/1397 rows). Anything read
  above 100 V is treated as millivolts and rescaled; counted in `rows_rescaled`.
- **Timestamps are rebased per test.** The ten Neware files share one lab
  clock (`cell_010` starts near 540,277 s); each test is shifted to start at
  `0`, with the original offset kept in `tests.start_offset_s`.
- **A row needs time, voltage, and current to be stored**; temperature,
  capacity, and cycle index are optional. Rows missing a required field are
  dropped and counted in `rows_skipped`, not silently zeroed.
- **Exact duplicate rows are dropped** (`neware_cell_003` repeats 20 rows
  verbatim) and counted in `rows_duplicated`.
- **Re-ingestion replaces, not appends,** so a repeated `docker compose up`
  against a persisted volume never doubles the data.
- **Unchanged files are skipped by content hash**, not mtime; a newly added
  file needs no separate detection step since directory discovery always
  walks the whole tree. `files_unchanged` in the run summary.
- **Capacity** comes from each cycler's own capacity channel, not
  coulomb-counted from current; per-cycle capacity is split by current sign.

## Data quality gate

[app/etl/quality.py](app/etl/quality.py) checks two things after ingestion,
and [app/etl/quality_gate.py](app/etl/quality_gate.py) is the CI entry point:

- **Contract check** — did a file structurally satisfy its cycler's schema?
  Zero usable rows from a file is `critical`.
- **Quality check** — does the loaded data look physically plausible?
  Voltage/current/temperature outside a sane window, a skip/duplicate rate
  above 10%/5%, or an optional field unexpectedly missing for a cycler that
  isn't a documented exception (Neware's missing temperature channel is).

Both were verified against the bundled dataset first: its known quirks produce
**zero** findings — the gate catches what isn't already understood, not what
the pipeline already fixes and documents.

Every finding is persisted to `data_quality_issues`, warning or critical.

```bash
python -m app.etl.quality_gate --data-root data
```

Exits non-zero on any `critical` finding. In CI (`data-quality` job below)
that fails the check and blocks the PR, with each finding also printed as a
`::error::`/`::warning::` annotation on the PR diff — that's the "alert"
implemented here; no credentials needed, but it only reaches someone looking
at the PR. A real Slack/email/webhook integration is a follow-up (see
[Next](#what-i-would-do-next)).

## CI

[.github/workflows/tests.yml](.github/workflows/tests.yml) runs on every push
and pull request:


| Job             | What it covers                                                            |
| --------------- | ------------------------------------------------------------------------- |
| `lint`          | `ruff check` and `ruff format --check`                                    |
| `test`          | The suite on Python 3.11 and 3.12 (SQLite)                                |
| `test-postgres` | Ingest plus API against a real PostgreSQL service                         |
| `data-quality`  | The[quality gate](#data-quality-gate); fails the PR on a critical finding |
| `compose`       | `docker compose up --wait`, then curls the endpoints                      |

## What I would do next

1. Databricks Medalion framework, Event-driven: AutoLoader, Unity Catalog for meta data control, DLT for data quality control, Z-ordering/Liquid for partition improvement, Quality Dashboard by Ontos
2. IaC
3. More configuration
4. **Real alerting** for the quality gate (Slack/email/webhook) once
   credentials exist.
5. **More quality rules** — sampling gaps, capacity drifting against current
   sign, per-cycler rather than global plausibility windows.
6. Auto-generated document, i.e. api spec
