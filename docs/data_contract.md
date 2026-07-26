# Data contract

The authoritative version of these rules is
[app/etl/contract.py](../app/etl/contract.py) — this document explains the
reasoning behind them. The README summarises the same assumptions for a reader
who only wants to run the thing.

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

Per-row warnings are capped at five per file, followed by a total, so one bad
file cannot bury the log.

## Adding a cycler

1. Create `data/cycler_<x>_<name>/`. The cycler name is derived from the
   directory, so nothing else needs to know about it.
2. Add a `COLUMN_MAP` entry with the source column names and their units.
3. Add a normalization test for a representative row.

No parsing or loading code changes, as long as the export is delimited text.
