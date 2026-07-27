"""The data contract: mapping heterogeneous cycler exports onto one schema.

Each cycler exports different column names *and different units*. The unit is
therefore declared alongside the column name it belongs to, so a value can never
be converted with the wrong factor. An earlier version kept a flat list of
candidate column names and applied a single hardcoded ``mA -> A`` conversion to
whichever one matched, which silently divided already-in-amps readings by 1000.
"""

from __future__ import annotations

import logging
import math
import unicodedata
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Fields carrying a measurement, in the order they are stored.
VALUE_FIELDS: tuple[str, ...] = (
    "timestamp_s",
    "voltage_v",
    "current_a",
    "temperature_c",
    "capacity_ah",
    "cycle_index",
)

#: A row is only stored when all of these are present. Time plus at least one
#: electrical reading is the minimum needed for any downstream analysis, and
#: current is required so per-cycle charge/discharge summaries stay meaningful.
REQUIRED_FIELDS: tuple[str, ...] = ("timestamp_s", "voltage_v", "current_a")

#: Canonical unit for each field.
TARGET_UNITS: dict[str, str] = {
    "timestamp_s": "s",
    "voltage_v": "V",
    "current_a": "A",
    "temperature_c": "C",
    "capacity_ah": "Ah",
}

#: Conversions expressed as (multiplier, divisor) so exact decimal factors stay
#: exact in floating point (1234 * 1 / 1000 == 1.234, whereas 1234 * 0.001 does
#: not round-trip cleanly).
UNIT_CONVERSIONS: dict[tuple[str, str], tuple[float, float]] = {
    ("ms", "s"): (1, 1000),
    ("min", "s"): (60, 1),
    ("h", "s"): (3600, 1),
    ("mV", "V"): (1, 1000),
    ("mA", "A"): (1, 1000),
    ("mAh", "Ah"): (1, 1000),
    ("mA.h", "Ah"): (1, 1000),
}

#: Per-cycler column map: canonical field -> ordered (source column, source
#: unit) candidates. The first candidate present in a row wins. ``None`` as a
#: unit means the value is dimensionless (e.g. a cycle counter).
#:
#: Adding a new cycler is a matter of adding an entry here plus a directory named
#: ``cycler_<x>_<name>``; no parsing code needs to change.
COLUMN_MAP: dict[str, dict[str, tuple[tuple[str, str | None], ...]]] = {
    # BioLogic SP-150 export: tab-separated, reports current in mA and
    # capacity in mAh, so both need converting to the canonical A / Ah.
    "biologic": {
        "timestamp_s": (("time/s", "s"),),
        "voltage_v": (("voltage_measured", "V"),),
        "current_a": (("I/mA", "mA"),),
        "temperature_c": (("Temperature/°C", "C"),),
        "capacity_ah": (("Capacity/mA.h", "mA.h"), ("Q discharge/mA.h", "mA.h")),
        "cycle_index": (("cycle number", None),),
    },
    # Neware BTS4000 export: already in the canonical units (A, Ah, seconds),
    # and does not report temperature at all.
    "neware": {
        "timestamp_s": (("Time [s]", "s"),),
        "voltage_v": (("Voltage [V]", "V"),),
        "current_a": (("Current [A]", "A"),),
        "temperature_c": (("Temperature [°C]", "C"),),
        "capacity_ah": (("Capacity [Ah]", "Ah"),),
        "cycle_index": (("Cycle", None),),
    },
    # Novonix UHPC export: the only one of the three that reports time in
    # hours rather than seconds.
    "novonix": {
        # "Run Time (h)" is the test clock. "Step Time (h)" restarts on every
        # step, so it is deliberately not used as a fallback: mixing the two
        # would put two different time bases in one column.
        "timestamp_s": (("Run Time (h)", "h"),),
        "voltage_v": (("cell_voltage", "V"), ("Voltage (V)", "V")),
        "current_a": (("Current (A)", "A"),),
        "temperature_c": (("Temperature (°C)", "C"),),
        "capacity_ah": (("Capacity (Ah)", "Ah"),),
        "cycle_index": (("Cycle Number", None),),
    },
}

#: Single-cell chemistries stay well under this. A reading above it means the
#: exporter wrote millivolts into a volts column (see `repair_voltage_scale`).
#: Module or pack level data would need this raised.
MAX_PLAUSIBLE_CELL_VOLTAGE_V = 100.0


def _build_fallback_map() -> dict[str, tuple[tuple[str, str | None], ...]]:
    """Merge every cycler's candidates, for files from an unrecognised cycler."""
    # Walk every cycler's column map and collect every candidate column name
    # per field, skipping ones already seen so the list stays short.
    merged: dict[str, list[tuple[str, str | None]]] = {}
    for spec in COLUMN_MAP.values():
        for field, candidates in spec.items():
            bucket = merged.setdefault(field, [])
            for candidate in candidates:
                if candidate not in bucket:
                    bucket.append(candidate)
    return {field: tuple(candidates) for field, candidates in merged.items()}


# Built once at import time, since COLUMN_MAP does not change at runtime.
FALLBACK_COLUMNS = _build_fallback_map()


def infer_cycler(path: str | Path) -> str:
    """Infer the cycler from the nearest ``cycler_*`` directory in the path.

    ``data/cycler_a_biologic/cell_001.txt`` -> ``biologic``. Deriving this from
    the directory naming convention rather than a hardcoded lookup means new
    cycler folders work without a code change.
    """
    # Walk the path segments from the file back towards the root, looking for
    # the first one that starts with "cycler_".
    parts = Path(str(path)).parts
    for part in reversed(parts[:-1] if len(parts) > 1 else parts):
        if part.startswith("cycler_"):
            suffix = part[len("cycler_") :]
            # Strip the "a_"/"b_" ordering prefix when present.
            return suffix.split("_", 1)[1] if "_" in suffix else suffix
    # No cycler_* directory found anywhere in the path.
    return "unknown"


def build_test_id(path: str | Path, cycler: str | None = None) -> str:
    """Derive a stable test ID from a file path.

    ``cell_001`` exists under more than one cycler, so the cycler must be part
    of the identifier: ``biologic_cell_001`` vs ``neware_cell_001``.
    """
    path = Path(str(path))
    if cycler is None:
        cycler = infer_cycler(path)
    # path.stem is the filename without its extension, e.g. "cell_001".
    return f"{cycler or 'unknown'}_{path.stem}"


def _to_float(value: Any) -> float | None:
    """Coerce a value to a finite float, or None if it cannot be represented."""
    if value is None:
        return None

    # Try a direct conversion first, then fall back to stripping whitespace
    # from a string value before trying again.
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        try:
            numeric_value = float(str(value).strip())
        except (TypeError, ValueError):
            return None

    # Rejects NaN and +/-inf, which pandas produces for blank and malformed cells.
    if not math.isfinite(numeric_value):
        return None
    return numeric_value


def normalize_numeric(
    value: Any,
    unit: str | None = None,
    target_unit: str | None = None,
) -> float | None:
    """Coerce a value to a float and convert its unit when needed."""
    numeric_value = _to_float(value)
    if numeric_value is None:
        return None

    # Only convert when a unit was declared and it actually differs from the
    # field's canonical unit; otherwise the value is already in the right unit.
    if unit and target_unit and unit != target_unit:
        conversion = UNIT_CONVERSIONS.get((unit, target_unit))
        if conversion is None:
            # No known conversion for this pair — keep the raw value rather
            # than guessing, but say so, since this should not normally happen.
            logger.warning(
                "No conversion known from %s to %s; storing the raw value", unit, target_unit
            )
            return numeric_value
        multiplier, divisor = conversion
        return numeric_value * multiplier / divisor

    return numeric_value


def repair_voltage_scale(voltage_v: float | None) -> tuple[float | None, bool]:
    """Rescale a voltage that was exported in millivolts.

    The BioLogic file mixes both scales inside ``voltage_measured``: most rows
    read ``3.51`` while ~15% read ``3518.05``. Returns the corrected value and
    whether a repair was applied.
    """
    if voltage_v is None or abs(voltage_v) <= MAX_PLAUSIBLE_CELL_VOLTAGE_V:
        # Already a sensible cell voltage, or missing — nothing to repair.
        return voltage_v, False
    # Above the plausible ceiling: assume it is millivolts and scale it down.
    return voltage_v / 1000.0, True


def _normalize_header(value: Any) -> str:
    """Reduce a header to lowercase alphanumerics for tolerant matching."""
    if value is None:
        return ""
    # Strip accents/special characters, then keep only letters and digits so
    # small header spelling differences ("Time [s]" vs "time/s") still match.
    normalized = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return "".join(ch for ch in normalized.casefold() if ch.isalnum())


def repair_mojibake(value: str) -> str | None:
    """Undo one round of UTF-8 bytes having been decoded as latin-1.

    The BioLogic export ships its temperature header as ``Temperature/ï¿½C``.
    Stripping non-alphanumerics is not enough on its own: NFKD turns ``ï`` into
    ``i`` plus a combining mark, so the header would normalize to
    ``temperatureic`` and never match the declared ``Temperature/°C``.
    Re-encoding recovers the original replacement character, which then drops
    out cleanly. Returns None when the value is not mojibake.
    """
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # Not mojibake (or not repairable this way) — leave it alone.
        return None


def _is_present(value: Any) -> bool:
    """Return True when a cell holds something worth reading.

    pandas represents a blank cell as ``NaN`` rather than ``None``, so treating
    only ``None`` as absent would stop the candidate chain in `_resolve` at a
    column that is present but empty, and never reach the fallback column.
    """
    if value is None:
        return False
    return not (isinstance(value, float) and math.isnan(value))


def normalized_header_index(row: dict[str, Any]) -> dict[str, Any]:
    """Index a raw row by normalized header, keeping the first of any collision.

    Each header is indexed under both its normalized form and, when it looks
    like mojibake, the normalized form of its repaired spelling.
    """
    index: dict[str, Any] = {}
    for key, value in row.items():
        if not _is_present(value):
            # Nothing useful in this cell — don't let it shadow a real value
            # under the same normalized key from another column.
            continue
        candidate_keys = [_normalize_header(key)]

        # If the header looks like corrupted text, also index it under the
        # repaired spelling so a declared column name can still find it.
        repaired = repair_mojibake(str(key))
        if repaired is not None and repaired != key:
            candidate_keys.append(_normalize_header(repaired))

        for candidate_key in candidate_keys:
            if candidate_key and candidate_key not in index:
                index[candidate_key] = value
    return index


def _resolve(
    row: dict[str, Any],
    header_index: dict[str, Any],
    candidates: tuple[tuple[str, str | None], ...],
) -> tuple[Any, str | None]:
    """Return the first present candidate's value together with its source unit."""
    # Try each candidate column name in order, first as an exact match, then
    # via the normalized header index (handles spelling/formatting differences).
    for name, unit in candidates:
        value = row.get(name)
        if _is_present(value):
            return value, unit
        normalized_key = _normalize_header(name)
        if normalized_key in header_index:
            return header_index[normalized_key], unit
    # None of the candidate columns had a usable value.
    return None, None


def normalize_timeseries_row(row: dict[str, Any], cycler: str, test_id: str) -> dict[str, Any]:
    """Map a cycler-specific row onto the common normalized schema.

    The returned dict contains the schema fields plus ``quality_flags``, a tuple
    describing any repair or omission applied to this row. The pipeline
    aggregates those flags so data quality issues are reported rather than
    silently absorbed.
    """
    # Pick this cycler's column map, or the merged fallback for an unknown one.
    column_map = COLUMN_MAP.get(cycler, FALLBACK_COLUMNS)
    header_index = normalized_header_index(row)
    flags: list[str] = []

    normalized: dict[str, Any] = {"test_id": test_id, "cycler": cycler}

    # For every schema field, find the matching source column and convert it
    # to the field's canonical unit.
    for field in VALUE_FIELDS:
        raw_value, source_unit = _resolve(row, header_index, column_map.get(field, ()))
        normalized[field] = normalize_numeric(
            raw_value, unit=source_unit, target_unit=TARGET_UNITS.get(field)
        )

    # Voltage gets one more pass: some BioLogic rows are in millivolts.
    normalized["voltage_v"], rescaled = repair_voltage_scale(normalized["voltage_v"])
    if rescaled:
        flags.append("voltage_rescaled_from_mv")

    # cycle_index is a counter, not a measurement.
    if normalized["cycle_index"] is not None:
        normalized["cycle_index"] = int(normalized["cycle_index"])

    # Record which required fields, if any, ended up missing so the pipeline
    # can report exactly why a row was dropped.
    flags.extend(f"missing:{field}" for field in REQUIRED_FIELDS if normalized.get(field) is None)

    normalized["quality_flags"] = tuple(flags)
    return normalized


def is_usable_row(row: dict[str, Any]) -> bool:
    """Return True when a normalized row contains every required field."""
    return all(row.get(field) is not None for field in REQUIRED_FIELDS)
