# Test logic summary

This document captures the purpose of each automated test in the current suite so the normalization rules are easy to review later.

## 1. `test_build_test_id_uses_cycler_prefix_and_stem`

Purpose:
- Verifies that a test ID is derived from both the cycler folder and the source filename.
- Confirms that the expected format is `cycler_stem`, for example `biologic_cell_001`.

Why it matters:
- The repository contains repeated file names such as `cell_001` in different cycler folders, so the test ID must remain unique.

## 2. `test_build_test_id_handles_explicit_cycler`

Purpose:
- Verifies that an explicit cycler argument overrides path-based inference when needed.
- Confirms the function still returns a clean, predictable identifier.

Why it matters:
- This keeps the contract stable for fallback cases where a path is not available or is ambiguous.

## 3. `test_normalize_numeric_converts_milliamp_to_amp`

Purpose:
- Verifies that a milliamps value is converted to amps correctly.
- Confirms the unit conversion logic for current values.

Why it matters:
- Battery data often mixes units across exporters, so this conversion must be explicit and reliable.

## 4. `test_normalize_numeric_returns_none_for_bad_values`

Purpose:
- Ensures malformed data does not crash the ETL pipeline.
- Confirms that invalid values are treated as missing and return `None`.

Why it matters:
- Real-world exports often contain bad rows or non-numeric placeholders, and the pipeline should handle these gracefully.

## 5. `test_normalize_timeseries_row_maps_cycler_specific_columns`

Purpose:
- Verifies that a cycler-specific row is mapped into the shared normalized schema.
- Confirms that values such as time, voltage, current, temperature, and cycle index are assigned to the correct output fields.

Why it matters:
- This is the core ETL contract test: it ensures the pipeline can turn heterogeneous source columns into one consistent structure.
