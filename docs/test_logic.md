# Testing approach

An earlier version of this file listed every test by name. That goes stale the
moment a test is added, so it now describes *what is covered and why* instead.
The tests themselves are the specification.

```bash
pytest                                   # 85 tests on SQLite, ~3s
pytest tests/test_contract.py -v         # the normalization rules
DATABASE_URL=postgresql://... pytest tests/test_postgres.py
```

## Where the effort goes

Coverage is weighted towards **normalization**, because that is where the
real-world messiness lives. A wrong unit conversion produces plausible-looking
numbers and no error, so it is exactly the class of defect that needs tests
rather than inspection. Loading and serving are comparatively mechanical.

| File | Scope |
|---|---|
| `test_contract.py` | Pure functions: identity, unit conversion, column mapping, value repairs |
| `test_pipeline.py` | End-to-end ingestion against synthetic files and the bundled dataset |
| `test_db.py` | URL resolution, schema creation and rebuild, placeholder translation |
| `test_api.py` | Endpoint behaviour, pagination, filtering, status codes |
| `test_api_errors.py` | Behaviour when the database is unreachable |
| `test_postgres.py` | The same flows against real PostgreSQL; skipped unless `DATABASE_URL` is set |

Synthetic fixtures cover the rules; the bundled dataset covers the data as it
actually is. Both matter — a synthetic file proves the rule, and the real file
proves the rule matches reality.

## What each area asserts

**Identity.** A test ID combines cycler and filename, so `cell_001` under two
cyclers yields two distinct IDs. The cycler is derived from the
`cycler_<x>_<name>` directory convention, including for directories that do not
exist yet (`cycler_d_arbin` → `arbin`), which is what keeps adding a cycler from
being a code change.

**Units.** Each cycler's current is asserted separately, in both a unit test and
an end-to-end range check. This is a regression guard: a single hardcoded
`mA → A` conversion once applied to every cycler, which pushed the Neware and
Novonix currents into microamps. It produced no error and no obviously wrong
value, only numbers 1000× too small. The end-to-end check asserts a plausible amp
range per cycler, which would catch the same class of mistake in a new cycler.

**Value repairs.** The mV rescale is tested at the boundary, on a real
mixed-scale row, and end to end (no stored voltage exceeds 5 V). The mojibake
header is tested with the byte sequence the real file contains.

**Rebasing.** Asserted both on a synthetic file with a known offset and across
the whole dataset: every test starts at `t=0`, and the offsets of the later Neware
files are non-zero and preserved.

**Robustness.** One test per failure mode, each asserting the run *continues*:
missing required values, exact duplicates, an unparseable file, a file with no
usable rows, a file outside any cycler directory. The counters are asserted too —
"handled gracefully" has to mean reported, not swallowed.

**Idempotency.** Ingesting twice produces an identical summary and no extra rows.
This is not hypothetical: `docker compose up` re-runs the ETL against a persisted
volume, and an append-only load doubles the data every time.

**HTTP semantics.** 404 only for an unknown test; 200 with an empty array when a
known test's filters match nothing; 422 for out-of-range pagination; 503 when the
database is unreachable. There is also a timing assertion that a request fails
fast rather than working through the ETL's connection retry budget.

**Pagination.** Beyond the happy path, one test walks every page and asserts the
union is the full set, in order, with no repeats — the property that actually
matters to a client.

## Dashboard

`app/static/dashboard.html` has no unit tests — it is plain JS in one file with
no build step, so there is nothing to import into pytest. It has one automated
check instead: [tests/test_api.py](../tests/test_api.py) asserts `GET /` returns
the file with an HTML content type, which catches the file going missing or the
route breaking.

The actual behaviour was checked by driving a headless Chromium browser against
the running app with Playwright (installed ad hoc for this, not a project
dependency) and inspecting the canvases and DOM, not just the HTTP status:

- the test dropdown populates and is grouped by cycler;
- each of the three charts draws non-blank pixels, checked by reading the
  canvas's `ImageData` alpha channel — a chart that silently fails to render
  (the actual bug found this way: `_scales()` omitted `padding` from its
  return, so every downstream `padding.left`/`padding.top` read threw and
  aborted the render after the gridlines) looks identical to an empty one from
  the HTTP response alone;
- hovering fires the crosshair and tooltip, and the tooltip text matches the
  hovered point;
- `page.on("console")` / `page.on("pageerror")` catch anything thrown, in both
  `color-scheme: light` and `color-scheme: dark`;
- screenshots were read back and inspected for the failure mode automated
  checks don't catch: axis-tick labels that all rounded to the same integer
  (a `formatCompact` meant for row counts applied to sub-1 voltage/current
  values), and an endpoint value label landing exactly on a gridline and
  reading as struck through.

All three cyclers were exercised this way — BioLogic (confirms the mV rescale
produces a smooth curve, not a spiky one), Neware (confirms the "not reported"
placeholder for the missing temperature channel, and that current reads in
amps rather than the pre-fix microamps), and Novonix — against both the local
SQLite path and the containerized PostgreSQL stack via `docker compose up`.

## Choices worth knowing

- **SQLite for the default suite** so `pytest` needs no services, with
  `test_postgres.py` and the `compose` CI job covering the backend used at
  runtime. Without those two, the production path would be untested.
- **Warnings are errors** (`pytest.ini`), so a pandas parsing warning on new data
  fails the run instead of scrolling past.
- **`ruff check` and `ruff format --check` in CI**, so review discusses behaviour
  rather than layout.
- **No coverage gate.** At this size it measures effort, not confidence. The
  meaningful question is whether each known data defect has a test, and each one
  above does.
