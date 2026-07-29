# Data contract

The authoritative version of the schema itself — canonical fields, per-cycler
column mappings, target table layouts — is the declarative YAML under
[schema/](../schema/); see [Schema files](#schema-files) below.
[app/etl/contract.py](../app/etl/contract.py) loads and applies it (the
normalization logic: unit conversion, value repairs, header matching). This
document explains the reasoning behind the rules. The README summarises the
same assumptions for a reader who only wants to run the thing.

## Schema files

```
schema/
  canonical_fields.yaml       # normalized field order, required-ness, canonical unit
  sources/<cycler>.yaml       # per-cycler: canonical field -> ordered (column, unit) candidates
  targets/<table>.yaml        # tests / timeseries / data_quality_issues: columns, types, indexes
```

Loaded once at import time by [app/schema_loader.py](../app/schema_loader.py)
— `app/etl/contract.py` derives `VALUE_FIELDS`/`REQUIRED_FIELDS`/
`TARGET_UNITS`/`COLUMN_MAP` from it, `app/db.py` derives
`TESTS_COLUMNS`/`TIMESERIES_COLUMNS`/`QUALITY_ISSUES_COLUMNS` and the
`CREATE TABLE`/`CREATE INDEX` statements from it. Every file is validated
against a Pydantic model on load; a missing file, invalid YAML, or a field
that doesn't match the expected shape raises `SchemaError` and aborts before
any ingestion runs — a broken contract cannot silently degrade into a partial
or mis-parsed load. `UNIT_CONVERSIONS` (the ms→s, mA→A, ... table) stays a
Python constant in `contract.py`: it's small, physics-derived, and shared by
every cycler rather than something a new cycler needs to redeclare.

## Normalized schema

Every stored row carries:

| Field | Type | Unit | Notes |
|---|---|---|---|
| `test_id` | string | — | `<cycler>_<filename stem>`, e.g. `biologic_cell_001` |
| `cycler` | string | — | `biologic`, `neware`, `novonix` |
| `timestamp_s` | float | seconds | Since the start of *this* test |
| `voltage_v` | float | volts | |
| `current_a` | float | amps | Positive = charge |
| `temperature_c` | float | Celsius | `NULL` when the cycler does not report it |
| `capacity_ah` | float | amp-hours | |
| `cycle_index` | int | — | `NULL` when absent |

`test_id` includes the cycler because `cell_001` exists under more than one
cycler directory; the filename alone is not unique.

## Source columns and their units

| Field | BioLogic | Neware | Novonix |
|---|---|---|---|
| `timestamp_s` | `time/s` (s) | `Time [s]` (s) | `Run Time (h)` (**h**) |
| `voltage_v` | `voltage_measured` (V) | `Voltage [V]` (V) | `cell_voltage` (V) |
| `current_a` | `I/mA` (**mA**) | `Current [A]` (A) | `Current (A)` (A) |
| `temperature_c` | `Temperature/°C` | — | `Temperature (°C)` |
| `capacity_ah` | `Capacity/mA.h` (**mAh**) | `Capacity [Ah]` (Ah) | `Capacity (Ah)` (Ah) |
| `cycle_index` | `cycle number` | `Cycle` | `Cycle Number` |

### Why the unit is attached to the column

The obvious design is a list of candidate column names per canonical field, plus a
conversion for that field. It is also wrong here: `current_a` is populated from
`I/mA` for BioLogic but from `Current [A]` for the other two. A single conversion
on the field silently scales two of the three cyclers by 1000. So `COLUMN_MAP`
pairs every candidate column with the unit *that column* is in, and
`normalize_numeric` converts from that unit to the field's canonical unit.

The same applies to time: only Novonix exports hours.

### Column matching

Candidates are matched in declaration order. A row's headers are compared
exactly first, then after normalization (lowercase, alphanumerics only), then
after a mojibake repair — see below. The first candidate that yields a non-null
value wins.

`Step Time (h)` is deliberately **not** a fallback for the Novonix timestamp. It
restarts at each step, so using it when `Run Time (h)` is missing would put two
different time bases in one column.

An unrecognised cycler falls back to the union of every known cycler's
candidates, so an unfamiliar export still produces something usable.

## Handling rules

### Rows are dropped only when unusable

A row must have `timestamp_s`, `voltage_v` and `current_a`. Anything else is
optional and stored as `NULL`.

Requiring current is a judgement call. It keeps the per-cycle charge/discharge
summaries meaningful, and it costs 6 Novonix rows that have a valid voltage but
no current. The alternative — keep the row, leave `current_a` null — would make
`/cycles` capacity figures depend on which rows happened to have a current
reading. If that trade-off needs revisiting, change `REQUIRED_FIELDS`.

Blank cells, non-numeric text, `NaN` and `±inf` all normalize to `None`, which is
what makes a row unusable rather than raising.

### Values are repaired when the fix is unambiguous

- **mV in a volts column.** BioLogic writes `3517.94` in 210 of 1397 rows where
  the rest write `3.51`. Above 100 V, a reading is divided by 1000 and flagged
  `voltage_rescaled_from_mv`. The threshold suits single cells; module or pack
  data needs it raised.
- **Corrupted headers.** BioLogic's temperature column arrives as
  `Temperature/ï¿½C`: UTF-8 bytes that were decoded as latin-1. Stripping
  non-alphanumerics is not sufficient, because NFKD turns `ï` into `i` and the
  header normalizes to `temperatureic`. Re-encoding latin-1 → UTF-8 recovers the
  replacement character, which then drops out cleanly.

No other value repairs are applied. Anything ambiguous is dropped and counted
rather than guessed at.

### Time is rebased per test

`timestamp_s` is defined as seconds since the start of the test, but the ten
Neware files share one lab clock — `cell_010` starts at 540,277 s. Each test is
shifted so its first sample is `0`, and the original offset is stored in
`tests.start_offset_s`, which preserves the original ordering across files.

### Duplicates and ordering

Rows identical across every value field, including the timestamp, are dropped:
`neware_cell_003` repeats 20 rows and `cell_009` repeats one. A repeated
timestamp is an export defect, not a second measurement.

Rows are sorted by time on ingest, because `neware_cell_003` and `cell_007` are
not time-ordered in the source.

### Failures are contained and counted

Every drop is counted, and the counters land on the `tests` row so
`GET /tests` surfaces them. Nothing is silently discarded:

| Counter | Meaning |
|---|---|
| `rows_skipped` | Missing a required field |
| `rows_duplicated` | Exact repeat of an earlier row |
| `rows_rescaled` | Voltage converted from mV |
| `files_skipped` | Unparseable, no usable rows, or outside a `cycler_*` directory |
| `files_unchanged` | Content hash matched what was already stored; not reparsed |

Per-row warnings are capped at five per file, followed by a total, so one bad
file cannot bury the log.

### Unchanged files are skipped by content hash

Each file is hashed (SHA-256, streamed) before parsing. If a test's stored
`source_hash` already matches, the file is skipped entirely — no reparse, no
reload — and counted in `files_unchanged`. This is a content check, not an
mtime check: a bind-mounted or freshly checked-out file can carry a new mtime
with unchanged bytes, which would defeat an mtime-based skip on every run.

A newly added file needs no separate detection step: directory discovery
(`discover_files`) walks the whole tree on every run regardless of what is
already loaded, so a file with no matching `test_id` in the database always
falls through to a full ingest. The skip only ever applies to a `test_id`
that's already present with an identical hash.

One side effect worth knowing: `tests.ingested_at` now means "last actually
loaded," not "last time the pipeline ran and saw this file" — an unchanged
file's row is left untouched, timestamp included.

Not handled: a file removed from `data/` leaves its test row in place. Nothing
asked for that, and it's a reasonable next step rather than an oversight.

## Data quality gate

[app/etl/quality.py](../app/etl/quality.py) runs two checks after ingestion,
turning findings into `Issue` records
([app/etl/quality_gate.py](../app/etl/quality_gate.py) is the CLI/CI entry
point that persists them and sets the exit code):

- **`check_contract`** — did a file structurally satisfy the schema for its
  cycler? It reuses `ingest_directory`'s own `skipped_file_paths`: a file that
  produced zero usable rows already means every row failed on a required
  field, which is a stronger signal than "some rows were blank" — `critical`.
- **`check_quality`** — does what's already in the database look physically
  plausible? Runs as SQL aggregates over `tests`/`timeseries`, not by
  re-reading source files:

  | Rule | Severity | Threshold |
  |---|---|---|
  | `high_skip_rate` | warning | `rows_skipped` > 10% of rows seen |
  | `high_duplicate_rate` | warning | `rows_duplicated` > 5% of rows seen |
  | `voltage_out_of_range` | critical | outside 0–5.5 V |
  | `current_out_of_range` | critical | magnitude above 100 A |
  | `temperature_out_of_range` | critical | outside -40–100 °C |
  | `unexpected_missing_temperature` | warning | no temperature for any row, and the cycler isn't a documented exception |

  `KNOWN_MISSING_OPTIONAL_FIELDS` currently holds `(neware, temperature_c)` —
  Neware genuinely has no temperature channel (see above), so that gap is
  excluded from the warning rather than re-flagging a defect the pipeline
  already understands and reports through `rows_skipped`/`rows_rescaled`
  elsewhere. Both checks were run against the bundled dataset before being
  wired into CI specifically to confirm this: it produces zero findings.

Every finding — warning or critical — is written to `data_quality_issues`
(`app/db.py`), a durable, queryable record separate from the CI log. The
thresholds above are module constants in `app/etl/quality.py`, chosen to be
generous enough not to fire on the bundled dataset's genuine peculiarities
while still catching a real corruption (e.g. an unrescaled millivolt reading,
or a current column read in the wrong unit). Reusing the file/database-level
checks the pipeline already computes, rather than adding new instrumentation,
is deliberate: it's the same information `rows_skipped`/`rows_rescaled` already
report, turned into a pass/fail gate with a persisted history.

## Adding a cycler

1. Create `data/cycler_<x>_<name>/`. The cycler name is derived from the
   directory, so nothing else needs to know about it.
2. Add `schema/sources/<name>.yaml` with the source column names and their
   units (see the existing files for the shape). Picked up automatically by
   `load_source_schemas()`'s glob — nothing else references the file by name.
3. Add a normalization test for a representative row.

No parsing or loading code changes, as long as the export is delimited text.
