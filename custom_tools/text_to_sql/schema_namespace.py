"""Explicit identity for scoped Text-to-SQL schema storage."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .schema_metadata import get_foreign_key_constraints
from .utils import get_table_columns

SCHEMA_NAMESPACE_SERIALIZATION_VERSION = 1
_SCOPE_FIELDS = frozenset(
    {
        "serialization_version",
        "tenant_id",
        "access_scope_id",
        "connection_view_id",
        "transient",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(mapping: Mapping[str, object], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"SchemaScope.{field} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class SchemaScope:
    """Unversioned authorization-view identity created at admission."""

    serialization_version: int
    tenant_id: str
    access_scope_id: str
    connection_view_id: str
    transient: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SchemaScope":
        if not isinstance(value, Mapping):
            raise TypeError("schema_scope must be a mapping")
        fields = set(value)
        missing = _SCOPE_FIELDS - fields
        unexpected = fields - _SCOPE_FIELDS
        if missing:
            raise ValueError(f"SchemaScope missing fields: {sorted(missing)}")
        if unexpected:
            raise ValueError(
                f"SchemaScope has unexpected fields: {sorted(unexpected)}"
            )
        serialization_version = value.get("serialization_version")
        if serialization_version != SCHEMA_NAMESPACE_SERIALIZATION_VERSION:
            raise ValueError(
                "SchemaScope.serialization_version must be "
                f"{SCHEMA_NAMESPACE_SERIALIZATION_VERSION}"
            )
        transient = value.get("transient")
        if type(transient) is not bool:
            raise TypeError("SchemaScope.transient must be a boolean")
        return cls(
            serialization_version=SCHEMA_NAMESPACE_SERIALIZATION_VERSION,
            tenant_id=_required_text(value, "tenant_id"),
            access_scope_id=_required_text(value, "access_scope_id"),
            connection_view_id=_required_text(value, "connection_view_id"),
            transient=transient,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "serialization_version": self.serialization_version,
            "tenant_id": self.tenant_id,
            "access_scope_id": self.access_scope_id,
            "connection_view_id": self.connection_view_id,
            "transient": self.transient,
        }

    @property
    def scope_key(self) -> str:
        return _sha256(self.to_mapping())


@dataclass(frozen=True)
class SchemaNamespace:
    """Versioned namespace used by every scoped schema storage layer."""

    scope: SchemaScope
    schema_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SchemaScope):
            raise TypeError("SchemaNamespace.scope must be a SchemaScope")
        if not isinstance(self.schema_fingerprint, str) or not _SHA256_RE.fullmatch(
            self.schema_fingerprint
        ):
            raise ValueError(
                "SchemaNamespace.schema_fingerprint must be a lowercase SHA-256"
            )

    @property
    def version_key(self) -> str:
        return _sha256(
            {
                "schema_scope": self.scope.to_mapping(),
                "schema_fingerprint": self.schema_fingerprint,
            }
        )


class SchemaFreshnessUnavailable(RuntimeError):
    """Live schema validation required for a scoped run was unavailable."""


def _canonical_metadata_value(value: Any) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value.strip()
    raise TypeError(
        "schema structural metadata must contain only JSON scalar values"
    )


def canonical_schema_fingerprint(schema: Mapping[str, object]) -> str:
    """Return a full, order-independent fingerprint of structural metadata."""

    if not isinstance(schema, Mapping):
        raise TypeError("schema must be a mapping")

    normalized_tables: list[dict[str, object]] = []
    for table_name in sorted(schema, key=str):
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("schema table names must be non-empty strings")
        table_schema = schema[table_name]
        if not isinstance(table_schema, dict):
            raise TypeError(f"schema table {table_name!r} must be a mapping")
        columns = get_table_columns(table_schema)
        normalized_columns: list[dict[str, object]] = []
        for column_name in sorted(columns, key=str):
            if not isinstance(column_name, str) or not column_name.strip():
                raise ValueError("schema column names must be non-empty strings")
            metadata = columns[column_name]
            if not isinstance(metadata, Mapping):
                raise TypeError(
                    f"schema column {table_name}.{column_name} must be a mapping"
                )
            normalized_columns.append(
                {
                    "name": column_name,
                    "type": _canonical_metadata_value(metadata.get("type", "")),
                    "not_null": _canonical_metadata_value(
                        metadata.get("not_null", "")
                    ),
                    "default_value": _canonical_metadata_value(
                        metadata.get("default_value", "")
                    ),
                    "constraint_type": _canonical_metadata_value(
                        metadata.get("constraint_type", "")
                    ),
                    "references": _canonical_metadata_value(
                        metadata.get("references", "")
                    ),
                }
            )
        foreign_keys: dict[str, object]
        if "foreign_keys" not in table_schema:
            foreign_keys = {"present": False}
        else:
            envelope = table_schema["foreign_keys"]
            if not isinstance(envelope, Mapping):
                raise TypeError(
                    f"foreign_keys for schema table {table_name!r} must be a mapping"
                )
            complete = envelope.get("complete")
            if not isinstance(complete, bool):
                raise TypeError("foreign_keys.complete must be a boolean")
            constraints = get_foreign_key_constraints(table_name, schema)
            foreign_keys = {
                "present": True,
                "complete": complete,
                "constraints": sorted(
                    constraints,
                    key=lambda constraint: str(constraint["constraint_id"]),
                ),
            }
        normalized_tables.append(
            {
                "name": table_name,
                "columns": normalized_columns,
                "foreign_keys": foreign_keys,
            }
        )

    return _sha256(
        {
            "fingerprint_version": 2,
            "tables": normalized_tables,
        }
    )


__all__ = [
    "SchemaFreshnessUnavailable",
    "SchemaNamespace",
    "SchemaScope",
    "canonical_schema_fingerprint",
]
