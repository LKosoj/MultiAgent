"""Тесты для накопительной обработки структурированных фильтров в SQLGenerator.

EPIC 1.4: _filter_value_conditions должен аккумулировать conditions от всех
типов условий (start/end/min/max/values/value), а не делать ранний return на
первом совпадении.
"""
import os

import pytest

os.environ.setdefault("USE_SQLGLOT", "1")

from custom_tools.text_to_sql.sql_generator import SQLGenerator


@pytest.fixture
def gen():
    return SQLGenerator()


def _expr():
    # Используем тот же фасад quote_identifier, что и в _build_filter_clauses.
    from custom_tools.text_to_sql.dialects import quote_identifier
    return quote_identifier("orders.created_at")


def test_filter_combines_range_and_in_list(gen):
    """{"start": ..., "end": ..., "values": [...]} должен дать три условия."""
    expr = _expr()
    value = {
        "start": "2024-01-01",
        "end": "2024-12-31",
        "values": [1, 2],
    }
    conditions = gen._filter_value_conditions(expr, value, {"operator": "="})

    assert conditions is not None
    joined = " AND ".join(conditions)
    assert "'2024-01-01'" in joined and ">=" in joined
    assert "'2024-12-31'" in joined and "<=" in joined
    assert "IN (1, 2)" in joined
    # Все три условия присутствуют отдельно
    assert len(conditions) == 3


def test_filter_combines_min_max_with_values(gen):
    expr = _expr()
    value = {"min": 0, "max": 100, "values": [50]}
    conditions = gen._filter_value_conditions(expr, value, {"operator": "="})

    assert conditions is not None
    assert len(conditions) == 3
    joined = " AND ".join(conditions)
    assert ">= 0" in joined
    assert "<= 100" in joined
    assert "IN (50)" in joined


def test_filter_start_only_still_works(gen):
    """Регрессия: start без end/values/value возвращает одно условие."""
    expr = _expr()
    conditions = gen._filter_value_conditions(expr, {"start": "2024-01-01"}, {"operator": "="})
    assert conditions == [f"{expr} >= '2024-01-01'"]


def test_filter_values_only_still_works(gen):
    """Регрессия: только values → IN-list."""
    expr = _expr()
    conditions = gen._filter_value_conditions(expr, {"values": [1, 2, 3]}, {"operator": "="})
    assert conditions == [f"{expr} IN (1, 2, 3)"]


def test_filter_values_non_list_returns_none(gen):
    """Не-list для values считается некорректным контрактом."""
    expr = _expr()
    conditions = gen._filter_value_conditions(expr, {"values": "oops"}, {"operator": "="})
    assert conditions is None


def test_filter_combines_range_and_nested_value(gen):
    """start + value: оба условия применяются (не ранний return на range)."""
    expr = _expr()
    value = {"start": "2024-01-01", "value": "2024-06-15"}
    conditions = gen._filter_value_conditions(expr, value, {"operator": "="})
    assert conditions is not None
    joined = " AND ".join(conditions)
    assert ">= '2024-01-01'" in joined
    assert "= '2024-06-15'" in joined
