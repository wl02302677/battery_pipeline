from __future__ import annotations

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
                if "_" in suffix:
                    cycler = suffix.split("_", 1)[1]
                else:
                    cycler = suffix
                break

    if cycler is None:
        cycler = "unknown"

    return f"{cycler}_{stem}"


def normalize_numeric(value: Any, unit: str | None = None, target_unit: str | None = None) -> float | None:
    """Coerce numeric values and convert basic units when possible."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        numeric_value = float(value)
    else:
        try:
            numeric_value = float(str(value).strip())
        except (TypeError, ValueError):
            return None

    if unit == "mA" and target_unit == "A":
        return numeric_value / 1000.0

    return numeric_value


def normalize_timeseries_row(row: dict[str, Any], cycler: str, test_id: str) -> dict[str, Any]:
    """Map a cycler-specific row into the common normalized schema."""
    timestamp_s = row.get("time/s") or row.get("Time [s]") or row.get("Run Time (h)") or row.get("Run Time (h)")
    voltage_v = row.get("voltage_measured") or row.get("Voltage [V]") or row.get("cell_voltage") or row.get("Voltage")
    current_a = normalize_numeric(row.get("I/mA") or row.get("Current [A]") or row.get("Current (A)") or row.get("Current"), unit="mA", target_unit="A")
    temperature_c = row.get("Temperature/°C") or row.get("Temperature (°C)") or row.get("Temperature")
    cycle_index = row.get("cycle number") or row.get("Cycle") or row.get("Cycle Number") or row.get("Cycle Number")

    if current_a is None:
        current_a = normalize_numeric(row.get("Current [A]") or row.get("Current (A)") or row.get("Current"), unit=None, target_unit=None)

    return {
        "test_id": test_id,
        "cycler": cycler,
        "timestamp_s": normalize_numeric(timestamp_s),
        "voltage_v": normalize_numeric(voltage_v),
        "current_a": current_a,
        "temperature_c": normalize_numeric(temperature_c),
        "cycle_index": int(cycle_index) if isinstance(cycle_index, int) else None,
    }
