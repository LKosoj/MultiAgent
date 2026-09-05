"""Tests for W4-4.1's human-readable provenance footer.

``format_text_to_sql_provenance_footer`` renders the terminal contract's
``provenance`` evidence mapping (see ``workflow.text_to_sql_contract`` and
``custom_tools/text_to_sql/core/_terminal.py::_build_text_to_sql_provenance``)
into a short Russian-language line an operator can read alongside the answer.
It is purely presentational: empty/malformed input must degrade to ``""``
rather than raise.
"""

from __future__ import annotations

import pytest

from workflow.text_to_sql_provenance import format_text_to_sql_provenance_footer


def _provenance(**overrides):
    base = {
        "run_id": "run-1",
        "tables": ["customers", "orders"],
        "columns": ["customers.id", "orders.customer_id"],
        "row_count": 2,
        "row_limit": 10,
        "possibly_truncated": False,
        "safety_llm_audit": "ok",
        "result_review_verdict": "consistent",
        "parse_error": False,
        "has_derived_tables": False,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "value",
    [
        {},
        None,
        [],
        "not a mapping",
        42,
    ],
)
def test_empty_or_non_mapping_provenance_yields_empty_footer(value):
    assert format_text_to_sql_provenance_footer(value) == ""


@pytest.mark.parametrize("bad_run_id", [None, "", "   ", 1])
def test_missing_or_invalid_run_id_yields_empty_footer(bad_run_id):
    assert format_text_to_sql_provenance_footer(_provenance(run_id=bad_run_id)) == ""


def test_full_happy_path_renders_all_clauses():
    footer = format_text_to_sql_provenance_footer(_provenance())

    assert footer == (
        "Источник: таблицы customers, orders; "
        "строк: 2 (лимит 10); "
        "проверки: safety=ok, review=consistent; "
        "run run-1."
    )


def test_possibly_truncated_is_called_out():
    footer = format_text_to_sql_provenance_footer(
        _provenance(row_count=10, possibly_truncated=True)
    )

    assert "строк: 10 (лимит 10, возможно усечено)" in footer


def test_parse_error_reports_unresolved_tables():
    footer = format_text_to_sql_provenance_footer(
        _provenance(tables=[], columns=[], parse_error=True)
    )

    assert footer.startswith(
        "Источник: таблицы не определены (не удалось разобрать SQL);"
    )


def test_no_tables_without_parse_error_reports_none_used():
    footer = format_text_to_sql_provenance_footer(_provenance(tables=[], columns=[]))

    assert footer.startswith("Источник: таблицы не использованы;")


def test_no_tables_with_derived_tables_reports_indirect_use():
    # tables=[] + has_derived_tables=True happens e.g. for
    # `WITH c AS (SELECT 1 AS x) SELECT c.x FROM c`: the only source is a
    # CTE, so no physical table name survives into `tables`. That must not
    # look like "no tables were used at all" — the query did read through a
    # derived source.
    footer = format_text_to_sql_provenance_footer(
        _provenance(tables=[], columns=["c.x"], has_derived_tables=True)
    )

    assert footer.startswith(
        "Источник: таблицы не использованы напрямую (только промежуточные подзапросы);"
    )


def test_has_derived_tables_adds_subquery_note():
    footer = format_text_to_sql_provenance_footer(
        _provenance(has_derived_tables=True)
    )

    assert (
        "таблицы customers, orders (через промежуточные подзапросы)" in footer
    )


def test_has_derived_tables_false_omits_note():
    footer = format_text_to_sql_provenance_footer(_provenance(has_derived_tables=False))

    assert "промежуточные подзапросы" not in footer


def test_missing_checks_omit_the_checks_clause():
    footer = format_text_to_sql_provenance_footer(
        _provenance(safety_llm_audit=None, result_review_verdict=None)
    )

    assert "проверки:" not in footer
    assert footer.endswith("run run-1.")


def test_only_safety_check_present():
    footer = format_text_to_sql_provenance_footer(
        _provenance(result_review_verdict=None)
    )

    assert "проверки: safety=ok" in footer
    assert "review=" not in footer


def test_only_review_verdict_present():
    footer = format_text_to_sql_provenance_footer(_provenance(safety_llm_audit=None))

    assert "проверки: review=consistent" in footer
    assert "safety=" not in footer


def test_missing_row_count_or_limit_omits_row_clause():
    footer = format_text_to_sql_provenance_footer(_provenance(row_limit=None))

    assert "строк:" not in footer


@pytest.mark.parametrize(
    "overrides",
    [
        {"tables": "not-a-list"},
        {"row_count": "two"},
        {"row_limit": True},
        {"possibly_truncated": "yes"},
        {"safety_llm_audit": 123},
        {"result_review_verdict": True},
        {"has_derived_tables": "yes"},
    ],
)
def test_malformed_optional_fields_degrade_without_raising(overrides):
    footer = format_text_to_sql_provenance_footer(_provenance(**overrides))

    assert isinstance(footer, str)
