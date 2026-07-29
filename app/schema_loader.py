"""Loads and validates the declarative data contract under ``schema/``.

The data contract used to live entirely as Python literals inside
``app/etl/contract.py`` (which columns map to which field, in which unit) and
``app/db.py`` (which columns the target tables have). That made both
"structured" (shape-checked) and "auto-scalable" (add a cycler/column without
touching Python) harder than they needed to be: the shape of a dict literal is
whatever the last edit left it as.

The schema files are now the source of truth, and this module is the only
place that reads them. Every loader is called once, at import time, by
``app.etl.contract`` and ``app.db`` — so a malformed schema file fails before
any pipeline code runs, the same way ``app.etl.contract.FALLBACK_COLUMNS`` is
already built once at import time rather than lazily.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ValidationError

#: Repo root (or, inside the container, the WORKDIR that mirrors it) — this
#: file lives at ``app/schema_loader.py``, so its grandparent is the root
#: ``schema/`` sits next to.
SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schema"


class SchemaError(RuntimeError):
    """Raised when a schema file is missing, not valid YAML, or malformed.

    Every load failure in this module surfaces as this one exception type,
    always naming the offending file, so a broken contract fails loudly and
    specifically instead of as a generic KeyError deep inside the pipeline.
    """


# -- source schemas (per-cycler column mapping) --------------------------- #


class ColumnCandidate(BaseModel):
    """One candidate source column for a canonical field, with its unit."""

    column: str
    unit: str | None = None


class SourceSchema(BaseModel):
    """One cycler's column mapping: canonical field -> ordered candidates."""

    cycler: str
    fields: dict[str, list[ColumnCandidate]]


# -- canonical schema (the normalized field list) -------------------------- #


class CanonicalField(BaseModel):
    """One field of the normalized schema every cycler is mapped onto."""

    name: str
    unit: str | None = None
    required: bool = False


class CanonicalSchema(BaseModel):
    fields: list[CanonicalField]


# -- target schemas (database table layout) -------------------------------- #

#: ``serial_pk`` is resolved to whichever backend's auto-increment primary key
#: syntax applies (see `Database._serial_pk`); the other three map to a fixed
#: SQL type regardless of backend.
ColumnType = Literal["text", "int", "float", "serial_pk"]


class ColumnSpec(BaseModel):
    name: str
    type: ColumnType
    nullable: bool = True
    primary_key: bool = False
    default: int | str | None = None
    references: str | None = None


class IndexSpec(BaseModel):
    name: str
    columns: list[str]


class TableSchema(BaseModel):
    table: str
    columns: list[ColumnSpec]
    indexes: list[IndexSpec] = []
    append_only: bool = False


def _load_yaml(path: Path) -> Any:
    """Read and parse one YAML file, raising `SchemaError` on any problem."""
    if not path.exists():
        raise SchemaError(f"Schema file not found: {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SchemaError(f"{path}: invalid YAML ({exc})") from exc


def load_canonical_schema(schema_root: Path = SCHEMA_ROOT) -> CanonicalSchema:
    """Load the normalized field list from ``canonical_fields.yaml``."""
    path = schema_root / "canonical_fields.yaml"
    try:
        return CanonicalSchema.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise SchemaError(f"{path}: {exc}") from exc


def load_source_schemas(schema_root: Path = SCHEMA_ROOT) -> dict[str, SourceSchema]:
    """Load every cycler's column mapping from ``sources/*.yaml``.

    A new cycler needs only a new file here — this glob picks it up with no
    code change. Keyed by each file's own declared ``cycler:``, not its
    filename, so the two are free to differ.
    """
    sources: dict[str, SourceSchema] = {}
    for path in sorted((schema_root / "sources").glob("*.yaml")):
        try:
            schema = SourceSchema.model_validate(_load_yaml(path))
        except ValidationError as exc:
            raise SchemaError(f"{path}: {exc}") from exc
        sources[schema.cycler] = schema
    return sources


def load_target_schema(name: str, schema_root: Path = SCHEMA_ROOT) -> TableSchema:
    """Load one target table's column/index layout from ``targets/<name>.yaml``."""
    path = schema_root / "targets" / f"{name}.yaml"
    try:
        return TableSchema.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise SchemaError(f"{path}: {exc}") from exc
