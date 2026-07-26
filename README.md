# Battery Pipeline

An ETL pipeline and REST API for battery cycler data. It ingests raw exports from
three different cyclers, normalizes them into one schema, loads them into
PostgreSQL, and serves them over HTTP.

The task brief that this repository answers is in [docs/brief.md](docs/brief.md).

## Quick start

```bash
docker compose up --build
```

That starts PostgreSQL, waits for it to be healthy, runs the ETL to completion,
then starts the API. No other setup is needed.

Open <http://localhost:8000/> for the dashboard, or use the API directly:

```bash
curl localhost:8000/health
curl localhost:8000/tests
curl "localhost:8000/tests/biologic_cell_001/timeseries?limit=5"
curl localhost:8000/tests/novonix_cell_001/cycles
```

Interactive docs: <http://localhost:8000/docs>

### Running locally without Docker

Falls back to SQLite, so no database server is required:

```bash
python -m pip install -r requirements-dev.txt
python -m app.etl.pipeline --data-root data --db-path battery.sqlite3
BATTERY_DB_PATH=battery.sqlite3 python -m uvicorn app.api:app --reload
```

### Tests

```bash
pytest                                  # the full suite, on SQLite
ruff check . && ruff format --check .    # lint

# Also exercise the PostgreSQL path (otherwise these tests skip):
DATABASE_URL=postgresql://battery:battery@localhost:5432/battery pytest tests/test_postgres.py
```

## Architecture

```
data/cycler_*/            raw exports, one file per test
  |
  v
app/etl/contract.py       per-cycler column map, units, identity, repairs
app/etl/pipeline.py       discover -> normalize -> validate -> dedupe -> rebase -> load
  |
  v
app/db.py                 PostgreSQL or SQLite behind one interface
  |
  v
app/api.py                FastAPI read layer
```

| File | Responsibility |
|---|---|
| [app/etl/contract.py](app/etl/contract.py) | Which source column maps to which field, in which unit. All normalization rules live here. |
| [app/etl/pipeline.py](app/etl/pipeline.py) | File discovery, row validation, de-duplication, time rebasing, batched loading. |
| [app/db.py](app/db.py) | Schema definition and the one place the two backends differ. |
| [app/api.py](app/api.py) | HTTP endpoints and response models. |
| [app/static/dashboard.html](app/static/dashboard.html) | Optional visualization dashboard, served at `/`. |

`DATABASE_URL` selects the backend. When it is unset the code falls back to a
local SQLite file, which is what the test suite and the local workflow use — the
same code path otherwise.

## Schema

`tests` — one row per source file, plus the ingestion counters the API exposes:

| Column | Notes |
|---|---|
| `test_id` | Primary key, e.g. `biologic_cell_001` |
| `cycler` | `biologic`, `neware`, `novonix` |
| `source_path` | Provenance: which file produced this test |
| `start_offset_s` | Raw source clock at the start of the test (see [time rebasing](#3-timestamps-are-rebased-per-test)) |
| `rows_loaded`, `rows_skipped`, `rows_duplicated`, `rows_rescaled` | Per-test data quality counters |
| `first_timestamp_s`, `last_timestamp_s`, `cycle_count` | Denormalized so `GET /tests` needs no aggregation |
| `ingested_at` | When this test was last loaded |

`timeseries` — one row per sample:

| Column | Unit |
|---|---|
| `test_id` | FK to `tests` |
| `timestamp_s` | seconds since the start of the test |
| `voltage_v` | volts |
| `current_a` | amps (positive = charge) |
| `temperature_c` | Celsius, null when the cycler does not report it |
| `capacity_ah` | amp-hours |
| `cycle_index` | integer, null when absent |

Indexed on `(test_id, timestamp_s)` and `(test_id, cycle_index)` — every API
query filters by `test_id`, so without these each request is a full scan.

## Dashboard

`GET /` serves a single-page dashboard (`app/static/dashboard.html`) — plain
JS and `<canvas>`, no build step and no external dependency, so it needs
nothing beyond what `docker compose up` already runs. It:

- lists every test grouped by cycler in a dropdown;
- plots voltage, current and temperature against time as three separate
  charts, never sharing an axis — voltage and current live on genuinely
  different scales, and a dual-axis chart makes that relationship look
  meaningful when it is really just two unrelated scales sharing a canvas;
- shows a crosshair-and-tooltip on hover, and a per-test KPI row (rows loaded,
  skipped, duplicated, rescaled, cycle count, duration);
- renders the per-cycle table from `GET .../cycles`;
- supports light and dark mode (`prefers-color-scheme`, with a manual toggle).

Temperature is plotted as "not reported" rather than a blank chart for Neware,
which has no temperature channel. A "view raw JSON" link next to the test
picker is the escape hatch to the full, unrounded values behind any chart.

It was built and checked with Playwright against a headless Chromium browser
(both themes, all three cyclers, hover interaction, zero console errors) — see
[docs/test_logic.md](docs/test_logic.md#dashboard) for how.

## API

| Endpoint | Notes |
|---|---|
| `GET /` | The dashboard above. |
| `GET /health` | Backend in use and number of loaded tests. Used by the compose healthcheck. |
| `GET /tests` | All tests with their cycler and quality counters. `?cycler=neware` to filter. |
| `GET /tests/{test_id}` | One test's summary. |
| `GET /tests/{test_id}/timeseries` | Paginated samples. `limit` (default 1000, max 50000), `offset`, `start_s`, `end_s`, `cycle_index`. |
| `GET /tests/{test_id}/cycles` | Per-cycle statistics: duration, min/max voltage, current and temperature, and capacity split by current direction. |

`/timeseries` returns a page envelope rather than a bare array, because a client
needs to know whether more data exists:

```json
{ "test_id": "novonix_cell_001", "total": 201, "returned": 50,
  "limit": 50, "offset": 0, "next_offset": 50, "data": [ ... ] }
```

Status codes: `404` means the `test_id` does not exist; a known test whose
filters match nothing returns `200` with an empty `data` array. `503` means the
database is unreachable. Out-of-range pagination parameters return `422`.

## Assumptions and handling rules

### 1. Units are declared per column, not per field

Each cycler names its columns differently *and* uses different units. The unit is
declared next to the column name it belongs to, in `COLUMN_MAP`:

| Field | BioLogic | Neware | Novonix |
|---|---|---|---|
| time | `time/s` (s) | `Time [s]` (s) | `Run Time (h)` (**hours**) |
| voltage | `voltage_measured` | `Voltage [V]` | `cell_voltage` |
| current | `I/mA` (**mA**) | `Current [A]` (A) | `Current (A)` (A) |
| capacity | `Capacity/mA.h` (**mAh**) | `Capacity [Ah]` (Ah) | `Capacity (Ah)` (Ah) |
| temperature | `Temperature/°C` | *not exported* | `Temperature (°C)` |
| cycle | `cycle number` | `Cycle` | `Cycle Number` |

Only BioLogic reports milliamps and milliamp-hours; only Novonix reports hours.
Attaching the unit to the column is what prevents a conversion being applied to
the wrong cycler.

Adding a cycler means adding an entry to `COLUMN_MAP` and a `cycler_<x>_<name>`
directory. The cycler name is derived from the directory, so no parsing code
changes.

### 2. BioLogic mixes volts and millivolts in one column

210 of the 1397 rows in `cycler_a_biologic/cell_001.txt` report voltage as
`3517.94` where the rest report `3.51`. Any reading above 100 V is treated as
millivolts and divided by 1000; the affected rows are counted in `rows_rescaled`
and flagged individually during normalization. The 100 V threshold suits
single-cell data and would need raising for module or pack level data.

### 3. Timestamps are rebased per test

`timestamp_s` is defined as seconds since the start of the test, but the ten
Neware files share one lab clock: `cell_010` starts near 540,277 s. Each test is
rebased so its first sample is `0`, and the original offset is kept in
`tests.start_offset_s` so the source ordering is not lost.

### 4. Rows must have time, voltage and current

A row missing any of the three is dropped and counted in `rows_skipped`
(83 rows across the dataset). Requiring current keeps the per-cycle
charge/discharge summaries meaningful. That is a deliberate trade-off: 6 Novonix
rows have a valid voltage but no current, and they are discarded. Temperature,
capacity and cycle index are optional and stored as `NULL` when absent.

Skipped rows are reported per test through the API rather than only logged, so
the loss is visible without reading container logs.

### 5. Exact duplicate rows are dropped

`neware_cell_003` repeats 20 rows verbatim, including their timestamps, and
`cell_009` repeats one. A repeated timestamp is an export defect rather than a
real re-measurement, so duplicates are dropped and counted in `rows_duplicated`.

### 6. Other handling

- **Non-monotonic time.** Two Neware files are not time-ordered; rows are sorted
  on ingest, so `id` order matches time order.
- **Corrupted headers.** The BioLogic temperature column arrives as
  `Temperature/ï¿½C` — UTF-8 bytes that were decoded as latin-1. Headers are
  matched after a repair attempt and after stripping non-alphanumerics.
- **Whole-file failures.** An unparseable file is logged and skipped; the run
  continues and reports it in `files_skipped`.
- **Files outside a `cycler_*` directory** are skipped, since neither the parser
  nor a unique test ID can be determined.
- **Re-ingestion is idempotent.** Each test's rows are replaced, not appended, so
  re-running `docker compose up` against a persisted volume does not double the
  data.
- **Capacity** is taken from each cycler's capacity channel. Per-cycle capacity is
  the span of that channel within the cycle, split by current sign — not
  coulomb-counted from current.

### Ingestion summary for the bundled dataset

```
files_discovered  12      rows_loaded      11525
tests_loaded      12      rows_skipped        83   (missing time, voltage or current)
files_skipped      0      rows_duplicated     21   (exact repeats)
                          rows_rescaled      168   (mV written into a volts column)
```

## CI

[.github/workflows/tests.yml](.github/workflows/tests.yml) runs on every push and
pull request:

| Job | What it covers |
|---|---|
| `lint` | `ruff check` and `ruff format --check` |
| `test` | The suite on Python 3.11 and 3.12 (SQLite) |
| `test-postgres` | Ingest plus API against a real PostgreSQL service |
| `compose` | `docker compose up --wait`, then curls the endpoints |

`test-postgres` exists because the rest of the suite runs on SQLite; without it
the backend used in production would only be exercised by hand.

## What I would do next

Roughly in priority order:

1. **Schema migrations.** `ensure_schema` currently rebuilds the tables when it
   detects a column mismatch. That is fine while the data is a reproducible load
   of files in the repo, and wrong as soon as it is not — Alembic instead.
2. **Streaming ingestion.** Files are read into memory in full. Chunking through
   `pandas.read_csv(chunksize=...)` and `COPY` instead of `INSERT` would let this
   handle files that do not fit in RAM, and would be much faster on PostgreSQL.
3. **Connection pooling.** The API opens a connection per request. Fine at this
   size, wasteful under load.
4. **Parallel ingestion.** Files are independent, so ingestion is embarrassingly
   parallel — one worker per file, or per cycler directory.
5. **Server-side downsampling** on `/timeseries` — the dashboard currently pages
   through every sample, which is fine at ~1000 rows per test and would not be
   at 1M; a LTTB or stride-based downsample parameter is the fix.
6. **Explicit data quality rules.** `rows_skipped` / `rows_rescaled` /
   `rows_duplicated` are the beginning of this. The next step is thresholds that
   fail a run or raise an alert: voltage outside the chemistry window, sampling
   gaps, capacity moving in the wrong direction for the current sign.
7. **Partitioning `timeseries` by test or time** once it is large, and an
   `ingested_files` table keyed by content hash so unchanged files are skipped.
8. **Dashboard: overlay multiple tests.** Useful for comparing cells side by
   side; would need the single-series-per-chart layout to grow a legend once a
   chart holds more than one series (see the dataviz notes in
   [docs/test_logic.md](docs/test_logic.md#dashboard)).
