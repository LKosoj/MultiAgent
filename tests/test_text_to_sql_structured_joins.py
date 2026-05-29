"""Тесты для рендеринга JOIN в _generate_from_linked_entities.

EPIC 1.2: CROSS/NATURAL JOIN должны рендериться без ON, остальные с ON.
Передача from_column/to_column для CROSS/NATURAL — контрактная ошибка.
"""
import pytest

from custom_tools.text_to_sql.sql_generator import SQLGenerator


def _context(join_type, *, with_columns=True):
    join = {
        "from_table": "orders",
        "to_table": "regions",
        "join_type": join_type,
    }
    if with_columns:
        join["from_column"] = "region_id"
        join["to_column"] = "id"
    return {
        "linked_entities": {
            "metrics": [
                {"name": "total", "table": "orders", "column": "amount", "aggregation": "sum"},
            ],
            "dimensions": [
                {"name": "region", "table": "regions", "column": "name"},
            ],
            "filters": {},
        },
        "joins": [join],
    }


def test_cross_join_rendered_without_on():
    gen = SQLGenerator()
    result = gen._generate_from_linked_entities(_context("CROSS", with_columns=False))
    assert "error" not in result, result
    sql = result["sql_query"]
    assert "CROSS JOIN" in sql
    assert " ON " not in sql.upper().split("CROSS JOIN", 1)[1]


def test_natural_join_rendered_without_on():
    gen = SQLGenerator()
    result = gen._generate_from_linked_entities(_context("NATURAL", with_columns=False))
    assert "error" not in result, result
    sql = result["sql_query"]
    assert "NATURAL JOIN" in sql
    assert " ON " not in sql.upper().split("NATURAL JOIN", 1)[1]


def test_cross_join_alias_with_space():
    """'CROSS JOIN' как одно значение должно нормализоваться к 'CROSS'."""
    gen = SQLGenerator()
    result = gen._generate_from_linked_entities(_context("CROSS JOIN", with_columns=False))
    assert "error" not in result, result
    sql = result["sql_query"]
    assert "CROSS JOIN" in sql
    # Не должно появиться двойное "JOIN JOIN"
    assert "CROSS JOIN JOIN" not in sql


def test_cross_join_with_columns_is_rejected():
    gen = SQLGenerator()
    result = gen._generate_from_linked_entities(_context("CROSS", with_columns=True))
    assert "error" in result
    assert "CROSS" in result.get("error", "") or "ON" in result.get("error", "")


def test_inner_join_still_has_on():
    """Регрессия: INNER/LEFT с ON."""
    gen = SQLGenerator()
    for jt in ("INNER", "LEFT"):
        result = gen._generate_from_linked_entities(_context(jt, with_columns=True))
        assert "error" not in result, result
        sql = result["sql_query"]
        assert f"{jt} JOIN" in sql
        assert " ON " in sql


def test_unknown_join_type_still_rejected():
    gen = SQLGenerator()
    result = gen._generate_from_linked_entities(_context("SIDEWAYS", with_columns=True))
    assert "error" in result
    assert "unsupported join_type" in result["error"]
