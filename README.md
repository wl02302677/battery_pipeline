# Battery Pipeline

An ETL pipeline and REST API for battery cycler data. It reads raw files from
three different cyclers, converts them into one shared format, loads them
into PostgreSQL, and serves the data over HTTP with a small dashboard.

The task brief this repository answers is in [docs/brief.md](docs/brief.md).
For deeper reasoning, see [docs/data_contract.md](docs/data_contract.md)
(normalization rules) and [docs/test_logic.md](docs/test_logic.md) (testing
strategy) — this file is the short version.

## 🏛️ 3 Main Design Principles

When building this project, I focused on three main engineering goals:

1. **Easy to extend for new machines (domain decoupling)**
   - Different battery cyclers use different column names and units (like
     `mV` vs `V`, or hours vs seconds).
   - I separated the file-reading logic from the rest of the pipeline. Adding
     a new vendor is as simple as adding a schema configuration file.
   - Hardware quirks (like Neware not having a temperature sensor) are
     handled smoothly without crashing the system.

2. **Data contracts & audit logs**
   - Schema definitions live in simple **YAML files** that are checked
     before any code runs.
   - Quality checks happen in two steps: first while reading (structure and
     type checks), then after loading (checking physical limits like 0V to
     5V).
   - Bad data is saved into a special `data_quality_issues` audit table
     instead of being quietly dropped.

3. **Separation of dev & production (safe CI/CD)**
   - **Production mode**: uses PostgreSQL inside Docker.
   - **Local dev / testing**: automatically falls back to a local SQLite
     file, so you can develop without running a database server.
   - **Automated tests**: GitHub Actions runs unit tests, lint checks, and a
     quality gate on every pull request.

## Run it

```bash
docker compose up --build
```

This starts PostgreSQL, waits for it to be ready, runs the ETL job, then
starts the API. Nothing else to set up.

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

# Also run the PostgreSQL path (otherwise these tests skip):
DATABASE_URL=postgresql://battery:battery@localhost:5432/battery pytest tests/test_postgres.py

# Run the same contract/quality checks CI runs:
python -m app.etl.quality_gate --data-root data
```

## How it's put together

```
schema/                   the data contract: canonical fields, per-cycler
                           column maps, target table layouts (see Schema below)
  |
  v
data/cycler_*/            raw exports, one file per test
  |
  v
app/schema_loader.py      loads + validates schema/ into Pydantic models (mandatory: a
                           broken file raises an error before any pipeline code runs)
app/etl/contract.py       applies the loaded column map: which column, which unit, per field
app/etl/pipeline.py       discover -> hash -> normalize -> validate -> dedupe -> rebase -> load
app/etl/quality.py        contract + quality checks over the loaded data
app/etl/quality_gate.py   CI entry point: ingest, check, save findings, set exit code
  |
  v
app/db.py                 PostgreSQL or SQLite behind one interface; table DDL generated
                           from schema/targets/*.yaml
  |
  v
app/api.py                FastAPI read layer + the dashboard route
app/static/dashboard.html single-page dashboard, no build step
```

`DATABASE_URL` picks the backend everywhere; if it's unset, everything falls
back to a local SQLite file, which is what tests and local runs use — same
code path either way.

## Schema

Declared in [schema/](schema/), not as SQL or Python literals: canonical
fields and per-cycler column maps live in `schema/sources/*.yaml` and
`schema/canonical_fields.yaml`; target table layouts live in
`schema/targets/*.yaml`. [app/schema_loader.py](app/schema_loader.py) loads
and validates every file once, at import time — a broken file raises a
`SchemaError` before any ingestion runs. `app/db.py` then generates its
`CREATE TABLE`/`CREATE INDEX` statements from that loaded layout, instead of
hardcoding them. Adding a cycler or a column just means editing or adding one
YAML file, not touching Python.

`tests` — one row per source file: identity (`test_id`, `cycler`,
`source_path`, `source_hash`), plus the per-test counters the API exposes
directly (`rows_loaded`/`skipped`/`duplicated`/`rescaled`, `cycle_count`,
`start_offset_s`, `ingested_at`).

`timeseries` — one row per sample: `test_id`, `timestamp_s` (seconds since
the test started), `voltage_v`, `current_a` (positive = charge),
`temperature_c` (null when the cycler doesn't report it), `capacity_ah`,
`cycle_index`. Indexed on `(test_id, timestamp_s)` and `(test_id,
cycle_index)`, since every query filters by `test_id`.

`data_quality_issues` — an append-only log of findings from the
[quality gate](#data-quality-gate) below.

Full column-by-column reasoning: [docs/data_contract.md](docs/data_contract.md).

## API

| Endpoint                          | Notes                                                                                                 |
| --------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `GET /`                           | The dashboard.                                                                                        |
| `GET /health`                     | Which backend is in use, and the test count; used by the compose healthcheck.                        |
| `GET /tests`                      | All tests, with cycler and quality counters. `?cycler=neware` to filter.                              |
| `GET /tests/{test_id}`            | One test's summary.                                                                                   |
| `GET /tests/{test_id}/timeseries` | Paginated (`limit`/`offset`, default 1000, max 50000), filterable by `start_s`/`end_s`/`cycle_index`. |
| `GET /tests/{test_id}/cycles`     | Per-cycle stats: duration, min/max voltage/current/temperature, capacity split by current direction.  |

`/timeseries` returns `{total, returned, limit, offset, next_offset, data}` —
a page envelope, not a bare array, so a client can tell whether more data
exists. `404` means the `test_id` doesn't exist; a known test whose filters
match nothing is `200` with an empty `data`. `503` means the database is
unreachable; `422` means a pagination parameter is out of range.

## Dashboard

`GET /` serves `app/static/dashboard.html` — plain JS and `<canvas>`, no
build step, no external dependency. It has a grouped test picker;
voltage/current/temperature as three separate single-axis charts (never
dual-axis, since they're unrelated scales); a crosshair-and-tooltip on hover;
a per-test KPI row; a per-cycle table; and light/dark mode. Checked with a
headless Chromium browser via Playwright, not just an HTTP status check —
see [docs/test_logic.md#dashboard](docs/test_logic.md#dashboard).

## Key assumptions

The full reasoning for each of these is in
[docs/data_contract.md](docs/data_contract.md); this is just the summary.

- **Units are declared per source column, not per field.** Only BioLogic
  reports mA/mAh; only Novonix reports hours. Keying the conversion to the
  field instead of the column would silently misconvert the other two
  cyclers.
- **BioLogic mixes V and mV in one column** (210/1397 rows). Anything read
  above 100 V is treated as millivolts and rescaled; counted in
  `rows_rescaled`.
- **Timestamps are rebased per test.** The ten Neware files share one lab
  clock (`cell_010` starts near 540,277 s); each test is shifted to start at
  `0`, with the original offset kept in `tests.start_offset_s`.
- **A row needs time, voltage, and current to be stored.** Temperature,
  capacity, and cycle index are optional. Rows missing a required field are
  dropped and counted in `rows_skipped`, not silently zeroed.
- **Exact duplicate rows are dropped** (`neware_cell_003` repeats 20 rows
  verbatim) and counted in `rows_duplicated`.
- **Re-ingestion replaces, not appends,** so running `docker compose up`
  again against a persisted volume never doubles the data.
- **Unchanged files are skipped by content hash**, not by modified time; a
  newly added file needs no separate detection step, since directory
  discovery always walks the whole tree. See `files_unchanged` in the run
  summary.
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

Both were checked against the bundled dataset first: its known quirks
produce **zero** findings — the gate catches what isn't already understood,
not what the pipeline already fixes and documents.

Every finding is saved to `data_quality_issues`, as a warning or critical.

```bash
python -m app.etl.quality_gate --data-root data
```

Exits non-zero on any `critical` finding. In CI (the `data-quality` job
below) that fails the check and blocks the PR, with each finding also
printed as a `::error::`/`::warning::` annotation on the PR diff — that's
the "alert" implemented here; no credentials needed, but it only reaches
someone looking at the PR. A real Slack/email/webhook integration is a
follow-up (see [Next](#what-i-would-do-next)).

## CI

[.github/workflows/tests.yml](.github/workflows/tests.yml) runs on every
push and pull request:

| Job             | What it covers                                                            |
| --------------- | ------------------------------------------------------------------------- |
| `lint`          | `ruff check` and `ruff format --check`                                    |
| `test`          | The suite on Python 3.11 and 3.12 (SQLite)                                |
| `test-postgres` | Ingest plus API against a real PostgreSQL service                         |
| `data-quality`  | The [quality gate](#data-quality-gate); fails the PR on a critical finding |
| `compose`       | `docker compose up --wait`, then curls the endpoints                      |

## What I would do next

1. Databricks architecture & data governance

   - Medallion architecture:
     - Bronze: Auto Loader for incremental, schema-evolution-safe file streaming.
     - Silver: DLT (Delta Live Tables) with expectations converted from `contract.py`.
     - Gold: analytical aggregates for SOH/SOC models.
   - Performance: liquid clustering over `cell_id` and `timestamp` (replacing manual Z-ordering).
   - Governance: Unity Catalog for fine-grained access, data lineage, and audit logs.
2. Advanced battery-domain quality gates

   - Dynamic per-cycler validation windows (e.g., custom voltage thresholds per cell chemistry/equipment type, instead of static global bounds).
   - Sampling gap detection and automatic interpolation flags.
3. Production operations (ops & CI/CD)

   - Real-time alerting: integration with Slack/email/PagerDuty for critical contract failures.
   - Infrastructure as code (IaC): Terraform for workspace, storage, and pipeline provisioning.
   - Data contract CI/CD: automated schema drift and breaking-change detection on GitHub PRs.
4. Developer experience & observability

   - Auto-generated API specs (OpenAPI/FastAPI) and data catalog documentation.
   - Integrated data observability dashboard (Databricks Lakeview / Grafana).
5. More clean layout split
6. MCP server integration, set common standard and skill for agent