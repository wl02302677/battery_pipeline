# Data contract and edge cases

## Normalized schema

Each ingested row should be normalized into the following fields:

- `test_id`: derived from the source file path and cycler, such as `biologic_cell_001`
- `cycler`: one of `biologic`, `neware`, or `novonix`
- `timestamp_s`: seconds from the start of the test
- `voltage_v`: volts
- `current_a`: amps
- `temperature_c`: degrees Celsius when present
- `cycle_index`: cycle number when present

## Assumptions

- Current values are converted to amps when the source uses milliamps.
- Missing or malformed values are stored as `None` and skipped from downstream aggregation where appropriate.
- The implementation will log a warning when a row cannot be parsed instead of failing the whole run.

## Known edge cases

- Different cyclers use different column names and units.
- Some rows may contain empty values or malformed numbers.
- The same `cell_001` name appears in more than one cycler directory, so the file path must participate in test ID generation.
