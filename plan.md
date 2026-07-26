# Plan: battery data ETL and API

Working plan and status. Implementation notes and assumptions are in the
[README](README.md); the task brief is in [docs/brief.md](docs/brief.md).

---

## 1. CI on pull requests — done

`.github/workflows/tests.yml` runs on every push and pull request with four jobs:
`lint`, `test` (Python 3.11 + 3.12 on SQLite), `test-postgres` (real PostgreSQL
service), and `compose` (`docker compose up --wait`, then curl the endpoints).

To actually block merges, the repository still needs branch protection on `main`
with these jobs marked as required checks — that is a GitHub setting, not
something the workflow file can assert.

## 2. Data contract and edge cases — done

Reviewed the three raw formats and pinned the rules in
[app/etl/contract.py](app/etl/contract.py):

- Test ID from the file path (`biologic_cell_001`), since `cell_001` exists under
  more than one cycler.
- One shared schema: `test_id`, `cycler`, `timestamp_s`, `voltage_v`, `current_a`,
  `temperature_c`, `capacity_ah`, `cycle_index`.
- **Units are declared per source column, not per field.** This is the important
  one: only BioLogic reports mA and mAh, only Novonix reports hours. A single
  conversion applied to a canonical field is wrong for the other two cyclers.
- Unusable rows are dropped with a counted reason instead of failing the run.

Edge cases found in the data and handled:

| Issue | Where | Handling |
|---|---|---|
| Mixed V and mV in one column | biologic, 210 rows | rescaled above 100 V, counted in `rows_rescaled` |
| Shared lab clock across files | neware, `cell_010` starts at 540,277 s | rebased per test, offset kept in `tests.start_offset_s` |
| Exact duplicate rows | neware `cell_003` (20), `cell_009` (1) | dropped, counted in `rows_duplicated` |
| Non-monotonic time | neware `cell_003`, `cell_007` | sorted on ingest |
| Blank / non-numeric cells | 83 rows overall | dropped, counted in `rows_skipped` |
| Mojibake header (`Temperature/ï¿½C`) | biologic | latin-1 → UTF-8 repair before header matching |
| No temperature channel | neware | stored as `NULL` |

## 3. Implementation skeleton — done

Python, split by responsibility: `app/etl/contract.py` (normalization rules),
`app/etl/pipeline.py` (orchestration), `app/db.py` (persistence and schema),
`app/api.py` (HTTP), `tests/`.

## 4. ETL pipeline — done

Walks the data root, infers the cycler from the `cycler_<x>_<name>` directory
convention, derives the test ID, normalizes per the column map, then validates,
de-duplicates, sorts, rebases time, and loads in batches.

Run it with `python -m app.etl.pipeline --data-root data`. Re-running replaces
each test's rows rather than appending, so a repeated `docker compose up` against
a persisted volume does not double the data.

## 5. Database model — done

`tests` (one row per file, plus per-test quality counters) and `timeseries` (one
row per sample), indexed on `(test_id, timestamp_s)` and `(test_id, cycle_index)`.
PostgreSQL at runtime, SQLite as the local and test fallback, selected by
`DATABASE_URL`. Both backends sit behind one interface in `app/db.py`, so no
endpoint or ETL code branches on the backend.

## 6. API — done

`GET /health`, `GET /tests`, `GET /tests/{test_id}`,
`GET /tests/{test_id}/timeseries`, `GET /tests/{test_id}/cycles`.

`/timeseries` is paginated and filterable by time window and cycle, and returns a
page envelope with `total` and `next_offset`. `/cycles` reports duration, min/max
voltage, current and temperature, and capacity split by current direction.

## 7. Frontend (bonus) — done

A single-page dashboard at `GET /` (`app/static/dashboard.html`): plain JS and
`<canvas>`, no build step, no external dependency, grouped test picker, three
single-axis charts (voltage/current/temperature — deliberately never dual-axis,
since voltage and current are unrelated scales), crosshair-and-tooltip on
hover, a per-test KPI row, the per-cycle table, and light/dark mode.

Verified with a headless Chromium browser via Playwright rather than only by
curling the route — see [docs/test_logic.md](docs/test_logic.md#dashboard) for
what that caught (a chart that silently failed to render, and axis labels that
all rounded to the same integer) that an HTTP-status check alone would have
missed.

## 8. Tests — done

87 tests, plus 5 that run only when PostgreSQL is configured. Coverage is
deliberately weighted towards the normalization rules, since that is where the
real-world messiness lives: per-cycler unit correctness, the mV repair, time
rebasing, de-duplication, idempotent re-ingestion, malformed and unparseable
input, pagination, and HTTP status semantics.

## 9. Containerization and documentation — done

`docker compose up --build` starts PostgreSQL, waits for its healthcheck, runs
the ETL to completion, then starts the API (which now also serves the
dashboard). Assumptions are documented in the README.

## 10. Verification — done

- ETL loads all 12 files: 11,525 rows, 83 skipped, 21 duplicates, 168 rescaled.
- Value ranges sanity-check per cycler: voltage 3.2–4.2 V, current 0.004–0.9 A,
  every test starting at `t=0`.
- Every endpoint exercised, including the 404 / 422 / 503 paths.
- Dashboard checked in a real headless browser, both themes, all three
  cyclers, against both SQLite and the containerized PostgreSQL stack.
- Full suite green, lint clean.

---

## Next

Ordered by what would matter most on real data. Expanded in the README's
[What I would do next](README.md#what-i-would-do-next).

1. Alembic migrations, replacing the current rebuild-on-drift behaviour.
2. Streaming ingestion (`chunksize` + `COPY`) so file size is not bounded by RAM.
3. Connection pooling in the API.
4. Parallel ingestion — files are independent.
5. Server-side downsampling — the dashboard currently pages through every
   sample, fine at ~1000 rows/test and not at 1M.
6. Wire the quality gate's alerting to a real channel (Slack/email/PagerDuty)
   once credentials exist. Today "ping alert" means a CI failure with a
   `::error::` annotation plus a persisted row in `data_quality_issues` — see
   [Resolved TODOs](#resolved-todos) — which needs no credentials but also
   doesn't reach anyone who isn't watching the PR.
7. More quality rules: sampling gaps, capacity drifting against the current
   sign, per-cycler (not just global) plausibility windows.
8. Partition `timeseries` by test or time once it is large.
9. Dashboard: overlay multiple tests for side-by-side comparison.

---

## Resolved TODOs

Kept for the record; all now implemented.

- **PostgreSQL first, then ETL, then API.** The compose file has three services in
  that order, gated on a `pg_isready` healthcheck and
  `service_completed_successfully`. Replaced an entrypoint script that polled the
  port from inside the app container.
- **Return proper HTTP error codes.** `404` for an unknown `test_id`, `422` for
  invalid pagination parameters, `503` when the database is unreachable. A known
  test whose filters match nothing is `200` with an empty array, not `404`.
- **Generated API documentation.** FastAPI serves it at `/docs`; the endpoints
  declare Pydantic response models, so the schema is typed rather than
  `list[dict[str, Any]]`.
- **Skip unchanged files; detect new ones without a separate step.** Yes, the
  compose `etl` service re-runs on every `docker compose up`, but it now hashes
  each file's content and skips reparsing/reloading a test whose hash matches
  what's already stored (`files_unchanged` in the run summary). A new file is
  found automatically, since directory discovery always walks the whole tree —
  there's no separate "detect additions" step needed, just nothing to skip for
  a `test_id` that isn't in the database yet. `tests.source_hash` holds the
  hash; `tests.ingested_at` now means "last actually loaded," not "last run
  seen," since an unchanged file no longer touches it. See
  [app/etl/pipeline.py](app/etl/pipeline.py)'s `file_hash` and the check in
  `ingest_directory`. Deletions aren't handled (a file removed from `data/`
  leaves its test row in place) — not asked for, and a reasonable next step.
- **Data contract and data quality checks in CI, persisted to Postgres.**
  [app/etl/quality.py](app/etl/quality.py) has two checks:
  `check_contract` flags a file that produced zero usable rows (its columns
  didn't structurally satisfy the schema for its cycler — `critical`);
  `check_quality` flags implausible values already in the database — voltage,
  current, or temperature outside a physically sane window, an unexpectedly
  missing optional field, or a skip/duplicate rate above a threshold. Every
  finding is written to a new `data_quality_issues` table (`app/db.py`) — a
  durable, queryable record, not just a log line. Both checks were validated
  against the bundled dataset first: its known quirks (rescaled voltage,
  missing Neware temperature, the skipped rows) produce **zero** findings, so
  the gate doesn't fail CI over defects the pipeline already understands and
  documents — only over ones it doesn't.
  [app/etl/quality_gate.py](app/etl/quality_gate.py) is the CI entry point
  (`python -m app.etl.quality_gate --data-root data`): it exits non-zero on any
  `critical` finding, which fails the `data-quality` job in
  [.github/workflows/tests.yml](.github/workflows/tests.yml) and blocks the PR,
  and prints a `::error::`/`::warning::` annotation per finding so it shows up
  on the PR diff. That — CI failure plus a GitHub annotation, no external
  service — is the "ping alert" implemented here; a real Slack/email/webhook
  integration needs credentials this environment doesn't have (see
  [Next](#next)).