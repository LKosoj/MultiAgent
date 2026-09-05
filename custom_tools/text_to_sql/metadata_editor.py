"""Text-to-SQL metadata editor orchestration (design doc 2026-09-05).

Ties together the three sources of "editable metadata" the redactор works
with — the ``sqlrag/<dsn>.json`` schema overlay (table/column descriptions +
value examples), the ``sqlrag/<dsn>.profile.yaml`` glossary section, and the
``typed_probe`` semantic facts accumulated in tactical memory — into a single
entry point for ``backend/fastapi_app/agui/service.py``, the same role
``dsn_glossary.py`` already plays for glossary-only orchestration.

Four entry points (mirroring the AG-UI ``text_to_sql.metadata.*`` actions):
  * ``load_metadata_view`` — read.
  * ``save_table_descriptions`` — partial read-modify-write of the schema
    overlay file.
  * ``save_glossary`` — full replace of the glossary section of the DSN
    profile.
  * ``set_fact_status`` — approve/reject a ``typed_probe`` semantic fact.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import urlsplit

from .dsn_profile import (
    DsnProfile,
    GlossaryEntry,
    MetricHints,
    _GLOSSARY_KIND_VALUES,
    dsn_profile_path,
    load_dsn_profile,
    save_dsn_profile_atomic,
    glossary_entry_to_mapping,
)
from .schema_loader import SchemaLoader, compute_editable_schema_digest
from .schema_memory_sqlite import SchemaMemoryManager, SemanticFact, _semantic_fact_key
from .schema_namespace import SchemaNamespace, SchemaScope, canonical_schema_fingerprint
from .utils import (
    dsn_to_sanitized_name,
    get_table_columns,
    get_table_description,
    set_table_description,
)

# ---------------------------------------------------------------------------
# Limits (§1.3/§1.4/§6 point 6 of the design doc — storage limits, distinct
# from the SCHEMA_DESC_LIMIT prompt-truncation limit in validators/).
# ---------------------------------------------------------------------------

_MAX_TABLES_PER_SAVE = 500
_MAX_COLUMNS_PER_TABLE = 200
_MAX_TABLE_DESCRIPTION_LENGTH = 2000
_MAX_COLUMN_DESCRIPTION_LENGTH = 1000
_MAX_EXAMPLES_PER_COLUMN = 20
_MAX_GLOSSARY_ENTRIES = 500
_MAX_GLOSSARY_TERM_LENGTH = 200
_MAX_GLOSSARY_NOTE_LENGTH = 2000


class SchemaMetadataConflictError(ValueError):
    """``expected_*_digest`` did not match current state — client must reload."""


_VERSION_CONFLICT_MESSAGE = (
    "metadata version conflict: reload table/column metadata before saving"
)


# ---------------------------------------------------------------------------
# View model (read side)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnMetadataView:
    type: str | None
    description: str
    description_source: Literal["file", "none"]
    examples: tuple[Any, ...]
    examples_source: Literal["file", "none"]


@dataclass(frozen=True)
class TableMetadataView:
    description: str
    description_source: Literal["file", "none"]
    columns: dict[str, ColumnMetadataView]


@dataclass(frozen=True)
class MetadataView:
    connection_ref: str
    dsn_dialect: str
    schema_digest: str | None
    editable_file_enabled: bool | None
    tables: dict[str, TableMetadataView]
    glossary_digest: str
    glossary_profile_exists: bool
    glossary_dsn_fingerprint: str | None
    glossary_schema_namespace_version: str | None
    glossary_entries: tuple[GlossaryEntry, ...]
    typed_probe_facts: tuple[tuple[SemanticFact, str], ...]


# ---------------------------------------------------------------------------
# Edit model (write side) — shape-validated by parse_*, business-validated by
# save_* (schema/glossary existence, length/count limits, dedup).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnDescriptionEdit:
    column: str
    description: str | None  # None = don't touch; "" = explicit clear
    examples: list[Any] | None  # None = don't touch; list = replace


@dataclass(frozen=True)
class GlossarySaveResult:
    digest: str
    entries: tuple[GlossaryEntry, ...]


@dataclass(frozen=True)
class TableDescriptionEdit:
    table_fqn: str
    description: str | None
    columns: tuple[ColumnDescriptionEdit, ...]



# Проверка digest и запись файла — две операции; без блокировки два
# администратора, прошедшие проверку с одним и тем же digest, молча
# затёрли бы правки друг друга. flock снимается ядром при смерти процесса,
# поэтому «висящей» блокировки не остаётся; на Windows flock нет —
# остаётся только внутрипроцессная блокировка (как в schema_memory_sqlite).
# Блокировка — per-DSN: описания (sqlrag/<dsn>.json) и глоссарий
# (<dsn>.profile.yaml) принадлежат одному подключению, а правки разных
# подключений не пересекаются по данным и не должны ждать друг друга.
_PROCESS_WRITE_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_WRITE_LOCKS_GUARD = threading.Lock()
_WRITE_LOCK_FILE_TEMPLATE = ".metadata-editor.{name}.lock"

if sys.platform != "win32":
    import fcntl
else:  # pragma: no cover - Windows only
    fcntl = None  # type: ignore[assignment]


@contextmanager
def _metadata_write_lock(loader: SchemaLoader, dsn: str):
    loader.file_manager.ensure_sqlrag_directory()
    lock_name = dsn_to_sanitized_name(dsn)
    lock_path = loader.file_manager.sqlrag_dir / _WRITE_LOCK_FILE_TEMPLATE.format(name=lock_name)
    with _PROCESS_WRITE_LOCKS_GUARD:
        process_lock = _PROCESS_WRITE_LOCKS.setdefault(lock_name, threading.Lock())
    with process_lock:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def compute_glossary_digest(entries: Sequence[GlossaryEntry]) -> str:
    """Sha256 digest of the glossary entry list (``sha256([])`` if empty)."""
    import hashlib

    payload = json.dumps(
        [glossary_entry_to_mapping(entry) for entry in entries],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Shape-only parsing (called by service.py before any live-schema access).
# ---------------------------------------------------------------------------


def parse_table_description_edits(payload: object) -> tuple[TableDescriptionEdit, ...]:
    if not isinstance(payload, list):
        raise ValueError("tables must be a list")
    edits: list[TableDescriptionEdit] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"tables[{index}] must be an object")
        table_fqn = item.get("table_fqn")
        if not isinstance(table_fqn, str) or not table_fqn.strip():
            raise ValueError(f"tables[{index}].table_fqn must be a non-empty string")
        description = item.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError(f"tables[{index}].description must be a string or null")
        raw_columns = item.get("columns", [])
        if not isinstance(raw_columns, list):
            raise ValueError(f"tables[{index}].columns must be a list")
        columns: list[ColumnDescriptionEdit] = []
        for col_index, col_item in enumerate(raw_columns):
            if not isinstance(col_item, dict):
                raise ValueError(
                    f"tables[{index}].columns[{col_index}] must be an object"
                )
            column = col_item.get("column")
            if not isinstance(column, str) or not column.strip():
                raise ValueError(
                    f"tables[{index}].columns[{col_index}].column must be a non-empty string"
                )
            col_description = col_item.get("description")
            if col_description is not None and not isinstance(col_description, str):
                raise ValueError(
                    f"tables[{index}].columns[{col_index}].description must be a string or null"
                )
            examples = col_item.get("examples")
            if examples is not None and not isinstance(examples, list):
                raise ValueError(
                    f"tables[{index}].columns[{col_index}].examples must be a list or null"
                )
            columns.append(
                ColumnDescriptionEdit(
                    column=column, description=col_description, examples=examples
                )
            )
        edits.append(
            TableDescriptionEdit(
                table_fqn=table_fqn,
                description=description,
                columns=tuple(columns),
            )
        )
    return tuple(edits)


def parse_glossary_entries(payload: object) -> tuple[GlossaryEntry, ...]:
    if not isinstance(payload, list):
        raise ValueError("entries must be a list")
    entries: list[GlossaryEntry] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"entries[{index}] must be an object")
        term = item.get("term")
        if not isinstance(term, str):
            raise ValueError(f"entries[{index}].term must be a string")
        synonyms_raw = item.get("synonyms", [])
        if not isinstance(synonyms_raw, list) or not all(
            isinstance(value, str) for value in synonyms_raw
        ):
            raise ValueError(f"entries[{index}].synonyms must be a list of strings")
        table = item.get("table")
        if not isinstance(table, str):
            raise ValueError(f"entries[{index}].table must be a string")
        column = item.get("column")
        if column is not None and not isinstance(column, str):
            raise ValueError(f"entries[{index}].column must be a string or null")
        kind = item.get("kind")
        if kind is not None and kind not in _GLOSSARY_KIND_VALUES:
            raise ValueError(
                f"entries[{index}].kind must be one of {_GLOSSARY_KIND_VALUES} or null"
            )
        note = item.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError(f"entries[{index}].note must be a string or null")
        entries.append(
            GlossaryEntry(
                term=term,
                synonyms=tuple(synonyms_raw),
                table=table,
                column=column,
                kind=kind,
                note=note,
            )
        )
    return tuple(entries)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def load_metadata_view(
    *,
    project_root: Path,
    connection_ref: str,
    dsn: str,
    scope_mapping: dict[str, object],
) -> MetadataView:
    scope = SchemaScope.from_mapping(scope_mapping)
    loader = SchemaLoader(project_root)
    loaded = loader.load_scoped_schema({}, dsn, scope)

    raw_doc = loader.load_raw_sqlrag_document(dsn)
    if raw_doc is None:
        editable_file_enabled: bool | None = None
        raw_schema_info: dict[str, Any] | None = None
    else:
        editable_file_enabled = bool(raw_doc.get("enable"))
        candidate_schema_info = raw_doc.get("schema_info")
        raw_schema_info = (
            candidate_schema_info if isinstance(candidate_schema_info, dict) else None
        )
    schema_digest = compute_editable_schema_digest(raw_schema_info)

    file_table_descriptions: dict[str, str] = {}
    file_column_descriptions: dict[tuple[str, str], str] = {}
    file_column_examples: dict[tuple[str, str], list[Any]] = {}
    for fact in loaded.semantic_facts:
        if fact.source != "file":
            continue
        if fact.fact_kind == "description":
            if fact.subject == "table":
                file_table_descriptions[fact.table_fqn] = str(fact.value)
            else:
                file_column_descriptions[(fact.table_fqn, fact.column)] = str(fact.value)
        elif fact.fact_kind == "example" and fact.column is not None:
            file_column_examples.setdefault((fact.table_fqn, fact.column), []).append(
                fact.value
            )

    tables: dict[str, TableMetadataView] = {}
    for table_fqn, table_schema in loaded.schema.items():
        if not isinstance(table_schema, dict):
            continue
        description_source: Literal["file", "none"] = (
            "file" if table_fqn in file_table_descriptions else "none"
        )
        columns: dict[str, ColumnMetadataView] = {}
        for column_name, column_info in get_table_columns(table_schema).items():
            if not isinstance(column_info, dict):
                continue
            key = (table_fqn, column_name)
            examples = column_info.get("examples")
            columns[column_name] = ColumnMetadataView(
                type=column_info.get("type"),
                description=str(column_info.get("description", "")),
                description_source=(
                    "file" if key in file_column_descriptions else "none"
                ),
                examples=tuple(examples) if isinstance(examples, list) else (),
                examples_source="file" if key in file_column_examples else "none",
            )
        tables[table_fqn] = TableMetadataView(
            description=get_table_description(table_schema),
            description_source=description_source,
            columns=columns,
        )

    profile = load_dsn_profile(
        dsn, live_schema_fingerprint=loaded.namespace.schema_fingerprint
    )
    memory_manager = SchemaMemoryManager(project_root)
    typed_probe_facts = tuple(
        memory_manager.list_semantic_facts(
            loaded.namespace, sources=frozenset({"typed_probe"})
        )
    )

    return MetadataView(
        connection_ref=connection_ref,
        dsn_dialect=urlsplit(dsn).scheme.lower(),
        schema_digest=schema_digest,
        editable_file_enabled=editable_file_enabled,
        tables=tables,
        glossary_digest=compute_glossary_digest(profile.glossary),
        glossary_profile_exists=dsn_profile_path(dsn).is_file(),
        glossary_dsn_fingerprint=profile.dsn_fingerprint,
        glossary_schema_namespace_version=profile.schema_namespace_version,
        glossary_entries=profile.glossary,
        typed_probe_facts=typed_probe_facts,
    )


def metadata_view_to_mapping(view: MetadataView) -> dict[str, Any]:
    """Serialize a ``MetadataView`` to the exact §1.2 response shape."""
    return {
        "connection_ref": view.connection_ref,
        "dsn_dialect": view.dsn_dialect,
        "schema_digest": view.schema_digest,
        "editable_file_enabled": view.editable_file_enabled,
        "tables": {
            table_fqn: {
                "description": table.description,
                "description_source": table.description_source,
                "columns": {
                    column_name: {
                        "type": column.type,
                        "description": column.description,
                        "description_source": column.description_source,
                        "examples": list(column.examples),
                        "examples_source": column.examples_source,
                    }
                    for column_name, column in table.columns.items()
                },
            }
            for table_fqn, table in view.tables.items()
        },
        "glossary": {
            "digest": view.glossary_digest,
            "profile_exists": view.glossary_profile_exists,
            "dsn_fingerprint": view.glossary_dsn_fingerprint,
            "schema_namespace_version": view.glossary_schema_namespace_version,
            "entries": [glossary_entry_to_mapping(entry) for entry in view.glossary_entries],
        },
        "facts": [
            {
                "fact_key": _semantic_fact_key(fact),
                "subject": fact.subject,
                "table_fqn": fact.table_fqn,
                "column": fact.column,
                "fact_kind": fact.fact_kind,
                "value": fact.value,
                "status": status,
            }
            for fact, status in view.typed_probe_facts
        ],
    }


# ---------------------------------------------------------------------------
# Write: table/column descriptions + examples
# ---------------------------------------------------------------------------


def _validate_and_filter_examples(
    examples: Sequence[object], table_fqn: str, column: str
) -> list[Any]:
    if len(examples) > _MAX_EXAMPLES_PER_COLUMN:
        raise ValueError(
            f"too many examples for {table_fqn}.{column} (max {_MAX_EXAMPLES_PER_COLUMN})"
        )
    filtered = [value for value in examples if SchemaLoader._is_example_value(value)]
    if examples and not filtered:
        raise ValueError(
            f"examples for {table_fqn}.{column} contain no valid scalar values"
        )
    return filtered


def save_table_descriptions(
    *,
    project_root: Path,
    dsn: str,
    expected_schema_digest: str | None,
    table_edits: Sequence[TableDescriptionEdit],
) -> str:
    """Read-modify-write ``sqlrag/<dsn>.json``; returns the new schema digest."""
    if len(table_edits) > _MAX_TABLES_PER_SAVE:
        raise ValueError(f"too many tables in one save (max {_MAX_TABLES_PER_SAVE})")

    loader = SchemaLoader(project_root)
    live_schema = loader.introspect_live_schema(dsn)
    with _metadata_write_lock(loader, dsn):
        return _save_table_descriptions_locked(
            loader, dsn, live_schema, expected_schema_digest, table_edits
        )


def _save_table_descriptions_locked(
    loader: SchemaLoader,
    dsn: str,
    live_schema: dict[str, Any],
    expected_schema_digest: str | None,
    table_edits: Sequence[TableDescriptionEdit],
) -> str:
    raw_doc = loader.load_raw_sqlrag_document(dsn)
    current_schema_info = (
        raw_doc.get("schema_info") if isinstance(raw_doc, dict) else None
    )
    if not isinstance(current_schema_info, dict):
        current_schema_info = None
    current_digest = compute_editable_schema_digest(current_schema_info)
    if current_digest != expected_schema_digest:
        raise SchemaMetadataConflictError(_VERSION_CONFLICT_MESSAGE)

    schema_info: dict[str, Any] = (
        json.loads(json.dumps(current_schema_info, ensure_ascii=False))
        if current_schema_info
        else {}
    )

    for table_edit in table_edits:
        if table_edit.table_fqn not in live_schema:
            raise ValueError(f"unknown table: {table_edit.table_fqn}")
        if len(table_edit.columns) > _MAX_COLUMNS_PER_TABLE:
            raise ValueError(
                f"too many columns for {table_edit.table_fqn} "
                f"(max {_MAX_COLUMNS_PER_TABLE})"
            )
        live_columns = get_table_columns(live_schema[table_edit.table_fqn])

        table_entry = schema_info.get(table_edit.table_fqn)
        if not isinstance(table_entry, dict):
            table_entry = {}
            schema_info[table_edit.table_fqn] = table_entry

        if table_edit.description is not None:
            if len(table_edit.description) > _MAX_TABLE_DESCRIPTION_LENGTH:
                raise ValueError(
                    f"description for {table_edit.table_fqn} exceeds "
                    f"{_MAX_TABLE_DESCRIPTION_LENGTH} characters"
                )
            set_table_description(table_entry, table_edit.description)

        if table_edit.columns:
            columns_entry = table_entry.get("columns")
            if not isinstance(columns_entry, dict):
                columns_entry = {}
                table_entry["columns"] = columns_entry
            for column_edit in table_edit.columns:
                if column_edit.column not in live_columns:
                    raise ValueError(
                        f"unknown column: {table_edit.table_fqn}.{column_edit.column}"
                    )
                column_entry = columns_entry.get(column_edit.column)
                if not isinstance(column_entry, dict):
                    column_entry = {}
                    columns_entry[column_edit.column] = column_entry
                if column_edit.description is not None:
                    if len(column_edit.description) > _MAX_COLUMN_DESCRIPTION_LENGTH:
                        raise ValueError(
                            f"description for {table_edit.table_fqn}.{column_edit.column} "
                            f"exceeds {_MAX_COLUMN_DESCRIPTION_LENGTH} characters"
                        )
                    column_entry["description"] = column_edit.description
                if column_edit.examples is not None:
                    column_entry["examples"] = _validate_and_filter_examples(
                        column_edit.examples, table_edit.table_fqn, column_edit.column
                    )

    document: dict[str, Any] = dict(raw_doc) if isinstance(raw_doc, dict) else {"enable": True}
    document["schema_info"] = schema_info
    loader.file_manager.save_schema_document_atomic(dsn, document)
    return compute_editable_schema_digest(schema_info)


# ---------------------------------------------------------------------------
# Write: glossary (full replace)
# ---------------------------------------------------------------------------


def _validate_and_clean_glossary_entry(
    entry: GlossaryEntry, live_schema: dict[str, Any]
) -> GlossaryEntry:
    term = entry.term.strip()
    if not term:
        raise ValueError("glossary term must be a non-empty string")
    if len(term) > _MAX_GLOSSARY_TERM_LENGTH:
        raise ValueError(
            f"glossary term {term!r} exceeds {_MAX_GLOSSARY_TERM_LENGTH} characters"
        )

    cleaned_synonyms: list[str] = []
    seen = {term.casefold()}
    for synonym in entry.synonyms:
        stripped = synonym.strip()
        if not stripped:
            raise ValueError(f"glossary term {term!r}: synonyms must be non-empty strings")
        if len(stripped) > _MAX_GLOSSARY_TERM_LENGTH:
            raise ValueError(
                f"glossary term {term!r}: synonym {stripped!r} exceeds "
                f"{_MAX_GLOSSARY_TERM_LENGTH} characters"
            )
        key = stripped.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned_synonyms.append(stripped)

    if entry.table not in live_schema:
        raise ValueError(f"glossary term {term!r}: unknown table {entry.table!r}")
    if entry.column is not None and entry.column not in get_table_columns(
        live_schema[entry.table]
    ):
        raise ValueError(
            f"glossary term {term!r}: unknown column {entry.table}.{entry.column}"
        )

    if entry.kind is not None and entry.kind not in _GLOSSARY_KIND_VALUES:
        raise ValueError(
            f"glossary term {term!r}: kind must be one of {_GLOSSARY_KIND_VALUES} or null"
        )

    note = entry.note
    if note is not None and len(note) > _MAX_GLOSSARY_NOTE_LENGTH:
        raise ValueError(
            f"glossary term {term!r}: note exceeds {_MAX_GLOSSARY_NOTE_LENGTH} characters"
        )

    return GlossaryEntry(
        term=term,
        synonyms=tuple(cleaned_synonyms),
        table=entry.table,
        column=entry.column,
        kind=entry.kind,
        note=note,
    )


def save_glossary(
    *,
    project_root: Path,
    dsn: str,
    expected_glossary_digest: str,
    entries: Sequence[GlossaryEntry],
) -> GlossarySaveResult:
    """Full replace of the glossary section.

    Returns the new digest together with the entries as actually persisted
    (trimmed, synonyms deduplicated) so UIs can show the stored state without
    re-introspecting the database.
    """
    if len(entries) > _MAX_GLOSSARY_ENTRIES:
        raise ValueError(f"too many glossary entries (max {_MAX_GLOSSARY_ENTRIES})")

    loader = SchemaLoader(project_root)
    live_schema = loader.introspect_live_schema(dsn)
    with _metadata_write_lock(loader, dsn):
        return _save_glossary_locked(dsn, live_schema, expected_glossary_digest, entries)


def _save_glossary_locked(
    dsn: str,
    live_schema: dict[str, Any],
    expected_glossary_digest: str,
    entries: Sequence[GlossaryEntry],
) -> GlossarySaveResult:
    live_fingerprint = canonical_schema_fingerprint(live_schema)

    current_profile = load_dsn_profile(dsn, live_schema_fingerprint=live_fingerprint)
    current_digest = compute_glossary_digest(current_profile.glossary)
    if current_digest != expected_glossary_digest:
        raise SchemaMetadataConflictError(_VERSION_CONFLICT_MESSAGE)

    validated_entries = tuple(
        _validate_and_clean_glossary_entry(entry, live_schema) for entry in entries
    )

    if dsn_profile_path(dsn).is_file():
        new_profile = replace(current_profile, glossary=validated_entries)
    else:
        from .schema_cache import _dsn_host_port_db

        new_profile = DsnProfile(
            version=1,
            dsn_fingerprint=_dsn_host_port_db(dsn),
            schema_namespace_version=live_fingerprint,
            captured_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            glossary=validated_entries,
            aliases={},
            type_hints={},
            metric_hints=MetricHints(),
            nlu_hints={},
            few_shots_ref=None,
        )

    save_dsn_profile_atomic(new_profile, dsn)
    return GlossarySaveResult(
        digest=compute_glossary_digest(validated_entries), entries=validated_entries
    )


# ---------------------------------------------------------------------------
# Write: typed_probe fact status
# ---------------------------------------------------------------------------


def set_fact_status(
    *,
    project_root: Path,
    dsn: str,
    scope_mapping: dict[str, object],
    fact_key: str,
    status: Literal["approved", "rejected"],
) -> None:
    if not isinstance(fact_key, str) or not fact_key.strip():
        raise ValueError("fact_key is required")
    if status not in ("approved", "rejected"):
        raise ValueError("status must be 'approved' or 'rejected'")

    scope = SchemaScope.from_mapping(scope_mapping)
    loader = SchemaLoader(project_root)
    loaded = loader.load_scoped_schema({}, dsn, scope)

    memory_manager = SchemaMemoryManager(project_root)
    current_facts = memory_manager.list_semantic_facts(
        loaded.namespace, sources=frozenset({"typed_probe"})
    )
    matching_fact = next(
        (fact for fact, _status in current_facts if _semantic_fact_key(fact) == fact_key),
        None,
    )
    if matching_fact is None:
        raise ValueError(f"unknown typed_probe fact_key: {fact_key}")

    memory_manager.set_semantic_fact_status_override(loaded.namespace, fact_key, status)


__all__ = [
    "SchemaMetadataConflictError",
    "ColumnMetadataView",
    "TableMetadataView",
    "MetadataView",
    "ColumnDescriptionEdit",
    "TableDescriptionEdit",
    "compute_glossary_digest",
    "parse_table_description_edits",
    "parse_glossary_entries",
    "load_metadata_view",
    "metadata_view_to_mapping",
    "save_table_descriptions",
    "save_glossary",
    "GlossarySaveResult",
    "set_fact_status",
]
