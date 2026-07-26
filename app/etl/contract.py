from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any


def build_test_id(path: str | Path, cycler: str | None = None) -> str:
    """Derive a stable test ID from a file path."""
    path = Path(str(path))
    stem = path.stem

    if cycler is None:
        parts = path.parts
        for part in parts:
            if part.startswith("cycler_"):
                suffix = part[len("cycler_"):]
                cycler = suffix.split("_", 1)[1] if "_" in suffix else suffix
                break

    return f"{cycler or 'unknown'}_{stem}"


def normalize_numeric(value: Any, unit: str | None = None, target_unit: str | None = None) -> float | None:
    """Coerce numeric values and convert basic units when possible."""
    if value is None:
        return None

    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        return None

    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        try:
            numeric_value = float(str(value).strip())
        except (TypeError, ValueError):
            return None

    if numeric_value != numeric_value or numeric_value in {float("inf"), float("-inf")}:
        return None

    if unit == "mA" and target_unit == "A":
        return numeric_value / 1000.0
    if unit == "h" and target_unit == "s":
        return numeric_value * 3600.0

    return numeric_value


def _normalize_header(value: Any) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return "".join(ch for ch in normalized.casefold() if ch.isalnum())


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            value = row[key]
            if value is not None:
                return value

        normalized_key = _normalize_header(key)
        if normalized_key:
            for row_key, value in row.items():
                if _normalize_header(row_key) == normalized_key and value is not None:
                    return value
    return None


def normalize_timeseries_row(row: dict[str, Any], cycler: str, test_id: str) -> dict[str, Any]:
    """Map a cycler-specific row into the common normalized schema."""
    timestamp_key = next(
        (key for key in ("time/s", "Time [s]", "Run Time (h)", "Step Time (h)") if row.get(key) is not None),
        None,
    )
    timestamp_value = _first_present(row, "time/s", "Time [s]", "Run Time (h)", "Step Time (h)")
    timestamp_s = normalize_numeric(timestamp_value, unit="h", target_unit="s") if timestamp_key in {"Run Time (h)", "Step Time (h)"} else normalize_numeric(timestamp_value)
    voltage_v = _first_present(row, "voltage_measured", "Voltage [V]", "cell_voltage", "Voltage")
    current_value = _first_present(row, "I/mA", "Current [A]", "Current (A)", "Current")
    current_a = normalize_numeric(current_value, unit="mA", target_unit="A")
    temperature_c = _first_present(
        row,
        "Temperature/°C",
        "Temperature (°C)",
        "Temperature",
        "Temperature/ï¿½C",
        "Temperature/Ã°C",
    )
    cycle_index = _first_present(row, "cycle number", "Cycle", "Cycle Number")

    if current_a is None:
        current_a = normalize_numeric(current_value)

    cycle_number = normalize_numeric(cycle_index)
    if cycle_number is not None:
        cycle_index = int(cycle_number)
    else:
        cycle_index = None

    return {
        "test_id": test_id,
        "cycler": cycler,
        "timestamp_s": normalize_numeric(timestamp_s),
        "voltage_v": normalize_numeric(voltage_v),
        "current_a": current_a,
        "temperature_c": normalize_numeric(temperature_c),
        "cycle_index": cycle_index,
    }
