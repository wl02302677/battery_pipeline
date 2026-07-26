# Tech Test Data

You have been provided with raw data exports from **three different battery cyclers** used in our lab.

## Files

| Folder | Cycler | Format | Files |
|---|---|---|---|
| `cycler_a_biologic/` | BioLogic SP-150 | Tab-separated text | 1 file |
| `cycler_b_neware/` | Neware BTS4000 | CSV | 10 files |
| `cycler_c_novonix/` | Novonix UHPC | CSV | 1 file |

Each file represents a single battery test. The data includes some or all of: time, voltage, current, temperature, capacity, cycle index.

## What you should expect

These are **real-world-style exports** — they have not been pre-cleaned.
Each cycler uses its own column naming, units, and file structure.
Some files may contain data quality issues typical of lab exports.

Your ETL pipeline should handle these gracefully and load all files into a single unified schema.

## Test IDs

Note that `cell_001` appears in more than one cycler folder. Use the file path to derive a unique **test ID** for each file — for example `biologic_cell_001` or `neware_cell_003`. This should be the primary identifier used throughout your pipeline and API.

## Target schema

Your pipeline should produce at least these fields for every row:

| Field | Type | Notes |
|---|---|---|
| `test_id` | string | Derived from file path, e.g. `biologic_cell_001` |
| `cycler` | string | `biologic`, `neware`, or `novonix` |
| `timestamp_s` | float | Seconds since test start |
| `voltage_v` | float | Volts |
| `current_a` | float | Amps (not mA) |
| `temperature_c` | float | Celsius — include if present in source |
| `cycle_index` | int | Include if present in source |

Document any assumptions you make about units or how you handle missing or malformed data.
