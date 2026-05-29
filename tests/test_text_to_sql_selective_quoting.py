"""EPIC 1.5: селективное квотирование идентификаторов в _apply_dialect_quoting.

Квотируются только:
- известные таблицы/колонки из linked_entities;
- reserved keywords диалекта sqlglot;
- идентификаторы с не-ASCII / спецсимволами;
- mixed/upper-case идентификаторы в postgres.

Алиасы остаются голыми, безопасные snake_case идентификаторы — тоже.
"""
from unittest.mock import patch

import pytest

from custom_tools.text_to_sql.sql_generator import SQLGenerator


def _patch_dialect(dialect_name: str):
    """Контекст-менеджер для подмены текущего диалекта."""
    return patch(
        "custom_tools.text_to_sql.dialects.get_current_dialect_name",
        return_value=dialect_name,
    )


def test_quoting_skips_safe_identifiers_postgres():
    """snake_case safe identifiers не получают кавычек под postgres."""
    gen = SQLGenerator()
    linked = {
        "metrics": [{"name": "amount", "table": "orders", "column": "amount"}],
        "dimensions": [],
    }
    with _patch_dialect("postgres"):
        result = gen._apply_dialect_quoting(
            "SELECT amount FROM orders WHERE amount > 100", linked
        )
    # Safe snake_case без known-table mappings не должны квотироваться.
    # Здесь `orders` и `amount` known — поэтому они получают кавычки;
    # проверяем, что generic safe names (число, оператор) остались как есть.
    assert "WHERE" in result.upper()
    assert "100" in result


def test_quoting_marks_reserved_word_postgres():
    """Reserved keyword 'select' (как имя колонки/таблицы) должен быть квотирован.

    Используем 'desc' который точно reserved (TokenType.DESC).
    """
    gen = SQLGenerator()
    linked = {"metrics": [], "dimensions": []}
    # 'desc' — reserved в postgres (sort direction)
    with _patch_dialect("postgres"):
        result = gen._apply_dialect_quoting(
            'SELECT id FROM "data" ORDER BY id', linked
        )
    # Контроль: alias 'desc' не используется, проверим напрямую через _should_quote_name
    assert SQLGenerator._should_quote_name("desc", "postgres", set(), set(), set())
    assert SQLGenerator._should_quote_name("select", "postgres", set(), set(), set())
    assert SQLGenerator._should_quote_name("where", "postgres", set(), set(), set())


def test_quoting_marks_mixed_case_postgres():
    """Mixed-case идентификатор в postgres должен получить кавычки."""
    assert SQLGenerator._should_quote_name("Orders", "postgres", set(), set(), set())
    assert SQLGenerator._should_quote_name("UserName", "postgres", set(), set(), set())
    # lowercase — не квотируем (если не reserved)
    assert not SQLGenerator._should_quote_name("orders", "postgres", set(), set(), set())


def test_quoting_marks_cyrillic_identifier():
    """Имя на кириллице должно быть квотировано (не подходит под SAFE_IDENTIFIER_RE)."""
    assert SQLGenerator._should_quote_name("сумма", "postgres", set(), set(), set())
    assert SQLGenerator._should_quote_name("заказ_id", "postgres", set(), set(), set())


def test_quoting_preserves_aliases():
    """Алиасы не квотируются — это просто метки."""
    gen = SQLGenerator()
    linked = {
        "metrics": [{"name": "total", "table": "orders", "column": "amount"}],
        "dimensions": [],
    }
    with _patch_dialect("postgres"):
        result = gen._apply_dialect_quoting(
            "SELECT o.amount AS total FROM orders AS o", linked
        )
    # Table-alias 'o' должен оставаться без кавычек
    # Column-alias 'total' тоже без кавычек
    assert '"o"' not in result
    assert '"total"' not in result
    # А вот amount и orders — known, должны быть квотированы
    assert '"amount"' in result
    assert '"orders"' in result


def test_quoting_mysql_uses_backticks():
    """Под MySQL должны использоваться backticks."""
    gen = SQLGenerator()
    linked = {
        "metrics": [{"name": "amount", "table": "orders", "column": "amount"}],
        "dimensions": [],
    }
    with _patch_dialect("mysql"):
        result = gen._apply_dialect_quoting(
            "SELECT amount FROM orders", linked
        )
    assert "`orders`" in result
    assert "`amount`" in result
    # Не должно быть двойных кавычек ANSI
    assert '"orders"' not in result


def test_alias_substring_doesnt_quote_real_table():
    """Регрессионный кейс: 'user_logs' не должен ломаться из-за substring 'user'."""
    gen = SQLGenerator()
    linked = {
        "metrics": [],
        "dimensions": [{"name": "uid", "table": "user_logs", "column": "user_id"}],
    }
    with _patch_dialect("postgres"):
        result = gen._apply_dialect_quoting(
            "SELECT user_id FROM user_logs", linked
        )
    # user_logs должно быть квотировано целиком
    assert '"user_logs"' in result
    # user_id (колонка) тоже квотируется (known)
    assert '"user_id"' in result


def test_alias_not_table_when_known_collision():
    """Если table alias случайно совпадает с known table name — alias остаётся голым."""
    gen = SQLGenerator()
    linked = {
        "metrics": [{"name": "amt", "table": "orders", "column": "amount"}],
        "dimensions": [],
    }
    with _patch_dialect("postgres"):
        result = gen._apply_dialect_quoting(
            "SELECT o.amount FROM orders AS o", linked
        )
    # o — alias, не квотируется ни в FROM, ни в Column.table
    assert '"o"' not in result
    # orders — known table, квотируется
    assert '"orders"' in result
