"""Deterministic code<->label cascade hints after an empty value search (W3-3.2).

If a ``SEARCH_VALUE`` probe looked for a label-shaped value (e.g. "Moscow")
inside a column that turns out to hold codes (e.g. ``region_code``) and found
zero rows, the label may simply live somewhere else. This module proposes
deterministic candidates for where that might be:

  * ``fk_lookup``: a string column on the table reached by following a
    foreign key declared (or inferred by naming convention) from the
    triggering column.
  * ``sibling_label``: a column in the *same* table whose name is the
    triggering column with a code suffix (``_id``/``_code``/``_key``) swapped
    for a label suffix (``_name``/``_label``/``_title``/``_desc``, or a bare
    ``name``/``title``) - or the reverse swap, so the same function also
    catches a label-shaped column that was searched for a code value.

Every function here is pure and deterministic: no I/O, no randomness, no
model calls. Gating (shadow/on/off) and logging live in the caller
(``production_research._bounded_research_context``), not here.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from ..schema_metadata import get_type
from ..type_categories_config import load_type_categories_config
from ..utils import get_table_columns
from .data_probes import _DataProbeFailure, resolve_schema_table_key
from .models import (
    ColumnRef,
    EvidenceRecord,
    EvidenceSourceKind,
    ResearchActionKind,
    ResearchState,
    TableRef,
)
from .provenance import parse_probe_observation
from .schema_probes import _relationship_edges, relationship_edges_cached

_TOP_N = 3
_STRING_CATEGORY = "string"
_CODE_SUFFIXES: tuple[str, ...] = ("_id", "_code", "_key")
_LABEL_SUFFIXES: tuple[str, ...] = ("_name", "_label", "_title", "_desc")
_BARE_LABEL_COLUMNS: frozenset[str] = frozenset({"name", "title"})

_CASCADE_HINT_ENV = "TEXT_TO_SQL_CODE_LABEL_CASCADE_HINT"


@dataclass(frozen=True, slots=True)
class CascadeCandidate:
    """One deterministic guess for where a searched label might actually live."""

    table: str
    column: str
    reason: Literal["fk_lookup", "sibling_label"]


def cascade_hint_mode() -> Literal["shadow", "on", "off"]:
    """Return the configured cascade-hint mode.

    Unknown or unset values fail closed to ``"shadow"`` (log only, never
    shape model input) rather than silently disabling or enabling the hint.
    """
    raw = os.getenv(_CASCADE_HINT_ENV, "shadow").strip().lower()
    if raw == "on":
        return "on"
    if raw == "off":
        return "off"
    return "shadow"


def code_label_cascade_hints(
    state: ResearchState,
    schema: Mapping[str, object],
    *,
    version_key: str | None = None,
) -> tuple[CascadeCandidate, ...]:
    """Return deduplicated, sorted, top-N cascade candidates for one state.

    Only ``SEARCH_VALUE`` evidence with zero observed rows for a non-empty
    string ``requested_value`` triggers candidates; every other evidence
    record is ignored.

    ``version_key`` (the schema's ``SchemaNamespace.version_key``), when
    given, routes the relationship-edge lookup through the same bounded
    cache ``search_schema_catalog`` uses, instead of recomputing edges for
    every call within a research loop. Omit it (e.g. when no stable schema
    identity is available) to compute edges directly, as before.
    """
    if type(state) is not ResearchState:
        raise TypeError("state must be ResearchState")
    if not isinstance(schema, Mapping):
        raise TypeError("schema must be a Mapping")
    edges = (
        relationship_edges_cached(schema, version_key)
        if version_key is not None
        else _relationship_edges(schema)
    )
    candidates: set[CascadeCandidate] = set()
    for record in state.evidence:
        trigger = _empty_value_search_trigger(record)
        if trigger is None:
            continue
        table_ref, column_name = trigger
        table_key = _schema_table_key(table_ref, schema)
        if table_key is None:
            continue
        table_body = schema.get(table_key)
        if not isinstance(table_body, Mapping):
            continue
        columns = get_table_columns(table_body)
        if column_name not in columns:
            continue
        candidates.update(_fk_lookup_candidates(edges, schema, table_key, column_name))
        candidates.update(_sibling_label_candidates(table_key, column_name, columns))
    ordered = sorted(
        candidates,
        key=lambda candidate: (candidate.table, candidate.column, candidate.reason),
    )
    return tuple(ordered[:_TOP_N])


def _empty_value_search_trigger(
    record: EvidenceRecord,
) -> tuple[TableRef, str] | None:
    """Return the (table, column) that triggered an empty label-shaped search."""
    if record.source_kind is not EvidenceSourceKind.VALUE_SEARCH:
        return None
    if record.cost.rows != 0:
        return None
    if type(record.target) is not ColumnRef:
        return None
    observation = parse_probe_observation(record.observation)
    if observation is None:
        return None
    if observation.storage != "inline":
        return None
    if observation.provenance.probe_kind is not ResearchActionKind.SEARCH_VALUE:
        return None
    payload = observation.payload
    if not isinstance(payload, dict):
        return None
    requested_value = payload.get("requested_value")
    if type(requested_value) is not str or not requested_value.strip():
        return None
    return record.target.table, record.target.column


def _schema_table_key(table_ref: TableRef, schema: Mapping[str, object]) -> str | None:
    """Resolve a ``TableRef`` to its schema dict key, or None if ambiguous/missing.

    Thin wrapper over ``data_probes.resolve_schema_table_key`` (same table
    matching rules), translating its raised failure into ``None`` since
    every caller here already treats "unresolved" as "skip", not an error.
    """
    try:
        table_key, _ = resolve_schema_table_key(table_ref, schema)
    except _DataProbeFailure:
        return None
    return table_key


def _fk_lookup_candidates(
    edges: Iterable[Mapping[str, object]],
    schema: Mapping[str, object],
    table_key: str,
    column_name: str,
) -> tuple[CascadeCandidate, ...]:
    """String columns on the table reached by an FK declared on ``column_name``.

    Excludes the FK's own target column(s) (``column_pairs[*]["to_column"]``)
    - that column is the code being looked up, often itself a string PK
    (e.g. an OKTMO-style code), never the human-readable label.
    """
    type_config = load_type_categories_config()
    candidates: list[CascadeCandidate] = []
    for edge in edges:
        if edge["from_table"] != table_key:
            continue
        if not any(
            pair["from_column"] == column_name for pair in edge["column_pairs"]
        ):
            continue
        target_table = edge["to_table"]
        target_body = schema.get(target_table)
        if not isinstance(target_body, Mapping):
            continue
        excluded_columns = {pair["to_column"] for pair in edge["column_pairs"]}
        for column, metadata in get_table_columns(target_body).items():
            if column in excluded_columns:
                continue
            if not isinstance(metadata, Mapping):
                continue
            if type_config.get_category(get_type(dict(metadata))) == _STRING_CATEGORY:
                candidates.append(
                    CascadeCandidate(
                        table=target_table,
                        column=column,
                        reason="fk_lookup",
                    )
                )
    return tuple(candidates)


def _sibling_label_candidates(
    table_key: str,
    column_name: str,
    columns: Mapping[str, object],
) -> tuple[CascadeCandidate, ...]:
    """Candidates for a code<->label sibling column in the same table."""
    return tuple(
        CascadeCandidate(table=table_key, column=sibling, reason="sibling_label")
        for sibling in _sibling_label_columns(column_name, columns.keys())
    )


def _sibling_label_columns(column: str, columns: Iterable[str]) -> tuple[str, ...]:
    """Return sibling column names for a code<->label suffix swap.

    Symmetric: a code-suffixed column (``region_code``) yields label
    candidates (``region_name``, plus a bare ``name``/``title`` fallback);
    a label-suffixed column (``status_name``) yields code candidates
    (``status_code``). Neither direction matches the column against itself.
    """
    lowered = column.casefold()
    by_lower = {name.casefold(): name for name in columns}
    results: list[str] = []
    for code_suffix in _CODE_SUFFIXES:
        if not lowered.endswith(code_suffix):
            continue
        base = column[: -len(code_suffix)]
        for label_suffix in _LABEL_SUFFIXES:
            candidate = f"{base}{label_suffix}".casefold()
            if candidate in by_lower and candidate != lowered:
                results.append(by_lower[candidate])
        for bare in _BARE_LABEL_COLUMNS:
            if bare in by_lower and bare != lowered:
                results.append(by_lower[bare])
        return tuple(dict.fromkeys(results))
    for label_suffix in _LABEL_SUFFIXES:
        if not lowered.endswith(label_suffix):
            continue
        base = column[: -len(label_suffix)]
        for code_suffix in _CODE_SUFFIXES:
            candidate = f"{base}{code_suffix}".casefold()
            if candidate in by_lower and candidate != lowered:
                results.append(by_lower[candidate])
        return tuple(dict.fromkeys(results))
    return ()


__all__ = [
    "CascadeCandidate",
    "cascade_hint_mode",
    "code_label_cascade_hints",
]
