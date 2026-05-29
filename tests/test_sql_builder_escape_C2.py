"""Pin-тесты Group C2 (Wave1): диалект-aware экранирование строковых литералов.

Контракт:
- ``dialects.sql_string_literal(value)`` возвращает строку в одинарных
  кавычках, экранирует ``'`` и (для mysql/sqlite) ``\\``. Делегирует в
  sqlglot.
- При наличии NUL-байта поднимает ``ValueError``.
- При отсутствии sqlglot поднимает ``RuntimeError`` без самописного fallback.
- ``sql_builder.sql_literal(str_value)`` использует ``sql_string_literal``
  вместо устаревшего ``escape_sql_string``.
"""
from __future__ import annotations

import builtins

import pytest
import sqlglot  # noqa: F401

# core text_to_sql при импорте читает safety profile из env;
# env-setup выполнен в conftest.py.

from custom_tools.text_to_sql import dialects, sql_builder  # noqa: E402


# ---------------------------------------------------------------------------
# sql_string_literal: ANSI / default
# ---------------------------------------------------------------------------
def test_sql_string_literal_escapes_single_quote_ansi(monkeypatch):
    # Без указания DSN sqlglot диалект разрешается через plugin-mapping;
    # для пустого DSN получаем "ansi" — здесь ' → ''.
    out = dialects.sql_string_literal("O'Brien")
    # sqlglot ANSI оборачивает в одинарные кавычки и удваивает '
    assert out == "'O''Brien'"


def test_sql_string_literal_returns_empty_quoted_for_empty():
    out = dialects.sql_string_literal("")
    assert out == "''"


def test_sql_string_literal_returns_empty_quoted_for_none():
    out = dialects.sql_string_literal(None)
    assert out == "''"


def test_sql_string_literal_raises_on_null_byte():
    with pytest.raises(ValueError) as ei:
        dialects.sql_string_literal("foo\x00bar")
    assert "NUL" in str(ei.value)


def test_sql_string_literal_does_not_fallback_on_sqlglot_runtime_error(monkeypatch):
    from sqlglot import exp

    def _raise(_value):
        raise RuntimeError("literal boom")

    monkeypatch.setattr(exp.Literal, "string", staticmethod(_raise))

    with pytest.raises(RuntimeError, match="literal boom"):
        dialects.sql_string_literal("safe")


def test_sql_string_literal_requires_sqlglot(monkeypatch):
    original_import = builtins.__import__

    def _blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sqlglot":
            raise ImportError("blocked sqlglot")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    with pytest.raises(RuntimeError, match="sqlglot is required"):
        dialects.sql_string_literal("safe")


# ---------------------------------------------------------------------------
# sql_string_literal: dialect-specific (mysql)
# ---------------------------------------------------------------------------
def test_sql_string_literal_escapes_backslash_for_mysql(monkeypatch):
    # Подменяем resolve dialect → mysql, sqlglot mapping → mysql.
    monkeypatch.setattr(dialects, "get_current_dialect_name", lambda dsn=None: "mysql")
    monkeypatch.setattr(dialects, "get_sqlglot_dialect", lambda dsn=None: "mysql")
    payload = "' OR 1=1 -- \\"
    out = dialects.sql_string_literal(payload)
    # Должна быть строка в одинарных кавычках с экранированным backslash.
    # sqlglot.exp.Literal.string генерирует '\\' для MySQL; даже в fallback-ветке —
    # тоже '\\\\'.
    assert out.startswith("'") and out.endswith("'")
    assert "\\\\" in out
    # Одиночная кавычка тоже должна быть экранирована.
    assert "''" in out


# ---------------------------------------------------------------------------
# Integration: sql_builder.sql_literal делегирует в sql_string_literal
# ---------------------------------------------------------------------------
def test_sql_builder_uses_new_escape_for_string_values():
    out = sql_builder.sql_literal("O'Brien")
    assert out == "'O''Brien'"


def test_sql_builder_passes_dsn_to_string_literals(monkeypatch):
    captured = []
    dsn = "sqlite:///tmp/app.db"

    def _literal(value, dsn=None):
        captured.append((value, dsn))
        return f"'{value}'"

    monkeypatch.setattr(sql_builder, "sql_string_literal", _literal)

    assert sql_builder.sql_literal("paid", dsn=dsn) == "'paid'"
    conditions = sql_builder.filter_value_conditions(
        '"orders"."status"',
        ["new", "paid"],
        {"operator": "IN"},
        dsn=dsn,
    )

    assert conditions == ['"orders"."status" IN (\'new\', \'paid\')']
    assert captured == [("paid", dsn), ("new", dsn), ("paid", dsn)]


def test_sql_builder_extracts_context_dsn_for_filters(monkeypatch):
    captured = []
    validation = {}
    dsn = "sqlite:///tmp/app.db"

    class _SchemaValidator:
        def validate_sql_against_schema(self, sql, schema, dsn=None):
            validation["dsn"] = dsn
            return {"is_valid": True}

    def _literal(value, dsn=None):
        captured.append((value, dsn))
        return f"'{value}'"

    monkeypatch.setattr(sql_builder, "sql_string_literal", _literal)
    result = sql_builder.build_sql_from_linked_entities(
        {
            "dsn": dsn,
            "schema_info": {"orders": {"columns": ["amount", "status"]}},
            "linked_entities": {
                "metrics": [
                    {
                        "name": "total",
                        "table": "orders",
                        "column": "amount",
                        "aggregation": "sum",
                    }
                ],
                "filters": {
                    "status": {
                        "table": "orders",
                        "column": "status",
                        "value": "paid",
                    }
                },
            },
        },
        schema_validator=_SchemaValidator(),
    )

    assert result["sql_query"]
    assert captured == [("paid", dsn)]
    assert validation["dsn"] == dsn


def test_sqlglot_dialect_requires_mapping_for_explicit_unknown_dsn(monkeypatch):
    monkeypatch.setattr(
        dialects,
        "get_current_dialect_name",
        lambda dsn=None, **kwargs: "mystery",
    )

    with pytest.raises(RuntimeError, match="sqlglot dialect is not configured"):
        dialects.get_sqlglot_dialect("mystery://host/db", strict=True)

    with pytest.raises(RuntimeError, match="sqlglot dialect is not configured"):
        dialects.sql_string_literal("safe", dsn="mystery://host/db")


def test_sql_builder_returns_null_for_none():
    assert sql_builder.sql_literal(None) == "NULL"


def test_sql_builder_returns_unquoted_number():
    assert sql_builder.sql_literal(42) == "42"
