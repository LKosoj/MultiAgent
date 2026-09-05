"""Tests for `custom_tools/text_to_sql/dsn_glossary.py` (EPIC W2-2.2).

`GlossaryEntry` is owned by a parallel change
(`custom_tools/text_to_sql/dsn_profile.py`, not necessarily on disk yet), so
these tests use a local dataclass with the fixed shape:
``term: str, synonyms: tuple[str, ...], table: str, column: str | None,
kind: str | None, note: str | None``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from custom_tools.text_to_sql.dsn_glossary import (
    apply_dsn_glossary,
    glossary_semantic_facts,
    merge_glossary_synonyms_into_schema,
)
from custom_tools.text_to_sql.schema_loader import LoadedSchema
from custom_tools.text_to_sql.schema_memory_sqlite import SemanticFact
from custom_tools.text_to_sql.schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)


@dataclass(frozen=True)
class _GlossaryEntry:
    term: str
    synonyms: tuple[str, ...]
    table: str
    column: str | None
    kind: str | None
    note: str | None


def _scope() -> SchemaScope:
    return SchemaScope.from_mapping(
        {
            "serialization_version": 1,
            "tenant_id": "tenant",
            "access_scope_id": "owner:alice",
            "connection_view_id": "registry:orders",
            "transient": False,
        }
    )


def _loaded_schema(schema: dict) -> LoadedSchema:
    namespace = SchemaNamespace(_scope(), canonical_schema_fingerprint(schema))
    return LoadedSchema(schema, namespace, "live", ())


_SCHEMA = {
    "public.orders": {
        "description": "Orders",
        "columns": {
            "amount": {"type": "DECIMAL", "description": "Order amount"},
        },
    },
    "public.customers": {"description": "", "columns": {"id": {"type": "INTEGER"}}},
}


def test_glossary_semantic_facts_expands_term_and_synonyms() -> None:
    """Each of [term, *synonyms] becomes its own approved fact."""
    entry = _GlossaryEntry(
        term="выручка",
        synonyms=("доход", "оборот"),
        table="public.orders",
        column="amount",
        kind="measure",
        note="sum by period",
    )

    facts = glossary_semantic_facts((entry,), _loaded_schema(_SCHEMA))

    assert [fact.value for fact in facts] == ["выручка", "доход", "оборот"]
    for fact in facts:
        assert fact.subject == "column"
        assert fact.table_fqn == "public.orders"
        assert fact.column == "amount"
        assert fact.fact_kind == "glossary_term"
        assert fact.source == "dsn_glossary"
        assert fact.status == "approved"
        assert fact.kind == "measure"
        assert fact.note == "sum by period"


def test_glossary_semantic_facts_table_level_entry_has_no_column() -> None:
    entry = _GlossaryEntry(
        term="клиент",
        synonyms=(),
        table="public.customers",
        column=None,
        kind="entity",
        note=None,
    )

    facts = glossary_semantic_facts((entry,), _loaded_schema(_SCHEMA))

    assert len(facts) == 1
    assert facts[0].subject == "table"
    assert facts[0].column is None
    assert facts[0].table_fqn == "public.customers"


def test_glossary_semantic_facts_deduplicates_repeated_terms() -> None:
    entry = _GlossaryEntry(
        term="выручка",
        synonyms=("выручка", "  ", "доход"),
        table="public.orders",
        column="amount",
        kind="measure",
        note=None,
    )

    facts = glossary_semantic_facts((entry,), _loaded_schema(_SCHEMA))

    assert [fact.value for fact in facts] == ["выручка", "доход"]


def test_glossary_semantic_facts_skips_unknown_table_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = _GlossaryEntry(
        term="ghost",
        synonyms=(),
        table="public.no_such_table",
        column=None,
        kind=None,
        note=None,
    )

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.dsn_glossary"):
        facts = glossary_semantic_facts((entry,), _loaded_schema(_SCHEMA))

    assert facts == ()
    assert "no_such_table" in caplog.text


def test_glossary_semantic_facts_skips_unknown_column_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = _GlossaryEntry(
        term="bad",
        synonyms=(),
        table="public.orders",
        column="no_such_column",
        kind=None,
        note=None,
    )

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.dsn_glossary"):
        facts = glossary_semantic_facts((entry,), _loaded_schema(_SCHEMA))

    assert facts == ()
    assert "no_such_column" in caplog.text


def test_merge_glossary_synonyms_into_schema_appends_without_mutating_input() -> None:
    entry = _GlossaryEntry(
        term="выручка",
        synonyms=("доход",),
        table="public.orders",
        column="amount",
        kind="measure",
        note=None,
    )
    original = {
        "public.orders": {
            "description": "Orders",
            "columns": {"amount": {"type": "DECIMAL", "description": "Order amount"}},
        }
    }

    merged = merge_glossary_synonyms_into_schema(original, (entry,))

    assert merged["public.orders"]["columns"]["amount"]["description"] == (
        "Order amount Синонимы: выручка, доход"
    )
    # Input schema is untouched.
    assert original["public.orders"]["columns"]["amount"]["description"] == "Order amount"
    # Unrelated fields survive the merge.
    assert merged["public.orders"]["description"] == "Orders"


def test_merge_glossary_synonyms_into_schema_skips_unknown_target() -> None:
    entry = _GlossaryEntry(
        term="ghost",
        synonyms=(),
        table="public.no_such_table",
        column=None,
        kind=None,
        note=None,
    )

    merged = merge_glossary_synonyms_into_schema(_SCHEMA, (entry,))

    assert merged == _SCHEMA


class _FakeMemoryManager:
    def __init__(self) -> None:
        self.calls: list[tuple[SchemaNamespace, tuple[SemanticFact, ...]]] = []

    def replace_dsn_glossary_facts(
        self, namespace: SchemaNamespace, facts: tuple[SemanticFact, ...]
    ) -> None:
        self.calls.append((namespace, facts))


def test_apply_dsn_glossary_round_trip() -> None:
    """apply_dsn_glossary validates entries, then hands the exact facts to
    replace_dsn_glossary_facts and returns them."""
    loaded_schema = _loaded_schema(_SCHEMA)
    entries = (
        _GlossaryEntry(
            term="выручка",
            synonyms=(),
            table="public.orders",
            column="amount",
            kind="measure",
            note=None,
        ),
        _GlossaryEntry(
            term="ghost",
            synonyms=(),
            table="public.no_such_table",
            column=None,
            kind=None,
            note=None,
        ),
    )
    memory_manager = _FakeMemoryManager()

    result = apply_dsn_glossary(memory_manager, loaded_schema, entries)

    assert [fact.value for fact in result] == ["выручка"]
    assert len(memory_manager.calls) == 1
    called_namespace, called_facts = memory_manager.calls[0]
    assert called_namespace == loaded_schema.namespace
    assert called_facts == result
