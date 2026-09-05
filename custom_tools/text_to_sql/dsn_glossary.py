"""EPIC W2-2.2: DSN glossary terms as durable ``SemanticFact`` values.

``GlossaryEntry`` (term, synonyms, table, column, kind, note) is produced by
a per-DSN profile (``custom_tools.text_to_sql.dsn_profile``, built by a
parallel change). This module only needs its four attributes — ``table``,
``column``, ``term``/``synonyms``, ``kind``, ``note`` — so it depends on that
shape via duck typing (``TYPE_CHECKING`` import) rather than a hard runtime
import, since the profile module may not exist yet.

Three entry points:
  * ``glossary_semantic_facts`` — one approved ``SemanticFact`` per
    ``[term, *synonyms]`` value, for entries whose table/column exist in the
    live schema. Invalid entries are skipped with a warning, never raise.
  * ``merge_glossary_synonyms_into_schema`` — appends a "Синонимы: ..." note
    to the matching table/column description, mirroring
    ``schema_loader.SchemaLoader._merge_editable_schema``'s
    attach-don't-replace approach.
  * ``apply_dsn_glossary`` — validates entries against the schema and
    replaces the durable DSN-glossary fact set via
    ``SchemaMemoryManager.replace_dsn_glossary_facts``.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Sequence

from pydantic import ValidationError

from .schema_loader import LoadedSchema
from .schema_memory_sqlite import SemanticFact
from .utils import get_table_columns, get_table_description, set_table_description

if TYPE_CHECKING:
    from .dsn_profile import GlossaryEntry

logger = logging.getLogger(__name__)


def _entry_terms(entry: "GlossaryEntry") -> tuple[str, ...]:
    """Every distinct, non-empty ``[term, *synonyms]`` value, order preserved."""
    terms: list[str] = []
    for candidate in (entry.term, *entry.synonyms):
        if isinstance(candidate, str) and candidate.strip() and candidate not in terms:
            terms.append(candidate)
    return tuple(terms)


def _entry_target(
    entry: "GlossaryEntry",
    schema: dict[str, Any],
) -> tuple[str, str | None] | None:
    """Return ``(table, column)`` for one entry, or ``None`` if it does not
    resolve against ``schema`` (unknown table, or unknown column)."""
    table = getattr(entry, "table", None)
    if not isinstance(table, str) or table not in schema:
        logger.warning(
            "dsn_glossary: пропущен термин %r — таблица %r отсутствует в схеме",
            getattr(entry, "term", None),
            table,
        )
        return None
    column = getattr(entry, "column", None)
    if column is not None:
        if column not in get_table_columns(schema[table]):
            logger.warning(
                "dsn_glossary: пропущен термин %r — колонка %s.%s отсутствует в схеме",
                getattr(entry, "term", None),
                table,
                column,
            )
            return None
    return table, column


def glossary_semantic_facts(
    entries: Sequence["GlossaryEntry"],
    loaded_schema: LoadedSchema,
) -> tuple[SemanticFact, ...]:
    """Expand each valid glossary entry into one approved fact per term/synonym.

    An entry whose table or column is not part of ``loaded_schema`` is
    skipped (logged as a warning) — no fact is created for it.
    """
    facts: list[SemanticFact] = []
    for entry in entries:
        target = _entry_target(entry, loaded_schema.schema)
        if target is None:
            continue
        table, column = target
        for term in _entry_terms(entry):
            try:
                facts.append(
                    SemanticFact(
                        subject="column" if column is not None else "table",
                        table_fqn=table,
                        column=column,
                        fact_kind="glossary_term",
                        value=term,
                        source="dsn_glossary",
                        status="approved",
                        kind=getattr(entry, "kind", None),
                        note=getattr(entry, "note", None),
                    )
                )
            except ValidationError:
                logger.warning(
                    "dsn_glossary: пропущен термин %r для %s%s — некорректная запись",
                    term,
                    table,
                    f".{column}" if column else "",
                )
    return tuple(facts)


def merge_glossary_synonyms_into_schema(
    schema: dict[str, Any],
    entries: Sequence["GlossaryEntry"],
) -> dict[str, Any]:
    """Append a "Синонимы: ..." note to each valid entry's description.

    Returns a new schema dict (the input is not mutated), matching
    ``SchemaLoader._merge_editable_schema``'s "attach text, don't replace
    it" approach. Entries that do not resolve against ``schema`` are
    skipped, same as ``glossary_semantic_facts``.
    """
    merged = json.loads(json.dumps(schema, ensure_ascii=False))
    grouped: dict[tuple[str, str | None], list[str]] = {}
    for entry in entries:
        target = _entry_target(entry, merged)
        if target is None:
            continue
        bucket = grouped.setdefault(target, [])
        for term in _entry_terms(entry):
            if term not in bucket:
                bucket.append(term)
    for (table, column), terms in grouped.items():
        if not terms:
            continue
        suffix = "Синонимы: " + ", ".join(terms)
        if column is None:
            table_schema = merged[table]
            current = get_table_description(table_schema)
            set_table_description(table_schema, f"{current} {suffix}".strip())
        else:
            column_info = get_table_columns(merged[table])[column]
            current = str(column_info.get("description", "")).strip()
            column_info["description"] = f"{current} {suffix}".strip()
    return merged


def apply_dsn_glossary(
    memory_manager: Any,
    loaded_schema: LoadedSchema,
    entries: Sequence["GlossaryEntry"],
) -> tuple[SemanticFact, ...]:
    """Validate ``entries`` against the schema and persist them as facts.

    ``memory_manager`` is duck-typed as ``SchemaMemoryManager``-shaped: only
    its ``replace_dsn_glossary_facts(namespace, facts)`` method is used.
    """
    facts = glossary_semantic_facts(entries, loaded_schema)
    memory_manager.replace_dsn_glossary_facts(loaded_schema.namespace, facts)
    return facts
