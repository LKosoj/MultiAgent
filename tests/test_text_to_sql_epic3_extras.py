"""Тесты для EPIC 3 (блок SQL-Generator extras + misc):

- 3.19: SchemaLoader — отсутствие `enable` → fail-fast (ValueError).
- 3.24: SCHEMA_INCLUDE_TABLES — case-insensitive нормализация.
- 3.25: type-aware quoting для bytes/Decimal в _sql_literal.
- 3.26: multi-part `table.column.alias` — alias квотируется как single identifier.
- 3.27: error-path с sql_query — safety_issues propagate в caller.
"""
import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_tools.text_to_sql.schema_loader import SchemaLoader, SchemaFilter
from custom_tools.text_to_sql.sql_generator import SQLGenerator


# ---------------------------------------------------------------------------
# 3.19: enable default-on policy
# ---------------------------------------------------------------------------

def test_schema_loader_missing_enable_fails_fast(tmp_path):
    """Файл без ключа `enable` должен явно падать с ValueError (fail-fast).

    Политика: silent skip и silent default-on одинаково плохо — пользователь
    не понимает, почему его файл проигнорирован или принят. Поэтому
    отсутствие `enable` — ошибка конфигурации.
    """
    # Подготовим sqlrag/<sanitized>.json без ключа enable
    repo_root = tmp_path
    sqlrag_dir = repo_root / "sqlrag"
    sqlrag_dir.mkdir()

    dsn = "sqlite:///nowhere.db"
    from custom_tools.text_to_sql.utils import dsn_to_sanitized_name
    sanitized = dsn_to_sanitized_name(dsn)

    schema_payload = {"orders": {"description": "x", "columns": {"id": {"type": "int"}}}}
    (sqlrag_dir / f"{sanitized}.json").write_text(
        json.dumps({"schema_info": schema_payload}),  # БЕЗ enable
        encoding="utf-8",
    )

    loader = SchemaLoader(repo_root=repo_root)
    with pytest.raises(ValueError, match="'enable' key is required"):
        loader._load_sqlrag_schema(dsn)


def test_schema_loader_explicit_enable_false_returns_none(tmp_path):
    """Явный enable=false по-прежнему пропускает файл — это сигнал отключения."""
    repo_root = tmp_path
    sqlrag_dir = repo_root / "sqlrag"
    sqlrag_dir.mkdir()

    dsn = "sqlite:///nowhere.db"
    from custom_tools.text_to_sql.utils import dsn_to_sanitized_name
    sanitized = dsn_to_sanitized_name(dsn)

    (sqlrag_dir / f"{sanitized}.json").write_text(
        json.dumps({"enable": False, "schema_info": {"t": {}}}),
        encoding="utf-8",
    )

    loader = SchemaLoader(repo_root=repo_root)
    assert loader._load_sqlrag_schema(dsn) is None


def test_schema_loader_non_dict_json_logs_warning_and_returns_none(tmp_path, caplog):
    """_load_sqlrag_schema: JSON с верхним уровнем-списком → logger.warning + None.

    Проверяет, что реальное поведение соответствует fail-fast docstring:
    вместо молчаливого None — явный WARNING в лог.
    """
    import logging

    repo_root = tmp_path
    sqlrag_dir = repo_root / "sqlrag"
    sqlrag_dir.mkdir()

    dsn = "sqlite:///nowhere.db"
    from custom_tools.text_to_sql.utils import dsn_to_sanitized_name
    sanitized = dsn_to_sanitized_name(dsn)

    # Верхний уровень — список, а не dict
    (sqlrag_dir / f"{sanitized}.json").write_text(
        json.dumps([1, 2, 3]),
        encoding="utf-8",
    )

    loader = SchemaLoader(repo_root=repo_root)
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.schema_loader"):
        result = loader._load_sqlrag_schema(dsn)

    assert result is None
    # Должен быть warning с упоминанием типа (list)
    assert any("list" in record.message for record in caplog.records), (
        f"Ожидался WARNING с типом 'list', records: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# 3.24: SCHEMA_INCLUDE_TABLES case-insensitive
# ---------------------------------------------------------------------------

def test_schema_include_tables_case_insensitive(monkeypatch):
    """SCHEMA_INCLUDE_TABLES должен матчить таблицы независимо от регистра."""
    db_schema = {
        "public.Orders": {"columns": {"id": {"type": "int"}}},
        "public.Customers": {"columns": {"id": {"type": "int"}}},
        "public.events": {"columns": {"id": {"type": "int"}}},
    }
    # Пользователь задаёт нижним регистром, БД хранит CamelCase
    monkeypatch.setenv("SCHEMA_INCLUDE_TABLES", "orders,EVENTS")

    filtered = SchemaFilter.filter_schema_by_include_list(db_schema)

    assert set(filtered.keys()) == {"public.Orders", "public.events"}


def test_schema_include_tables_case_insensitive_base_match(monkeypatch):
    """Матч по короткому имени (base) тоже case-insensitive."""
    db_schema = {
        "schema_a.Orders": {"columns": {}},
        "schema_b.OrDeRs": {"columns": {}},
        "schema_c.other": {"columns": {}},
    }
    monkeypatch.setenv("SCHEMA_INCLUDE_TABLES", "orders")
    filtered = SchemaFilter.filter_schema_by_include_list(db_schema)
    assert set(filtered.keys()) == {"schema_a.Orders", "schema_b.OrDeRs"}


# ---------------------------------------------------------------------------
# 3.25: _sql_literal для bytes/Decimal
# ---------------------------------------------------------------------------

def test_quoting_handles_bytes_and_decimal():
    """_sql_literal должен:
    - Decimal → числовой литерал (без кавычек),
    - bytes/bytearray → SQL:2008 hex literal X'...'.
    """
    gen = SQLGenerator()

    # Decimal: числовой литерал
    assert gen._sql_literal(Decimal("10.5")) == "10.5"
    assert gen._sql_literal(Decimal("0")) == "0"
    assert gen._sql_literal(Decimal("-3.14")) == "-3.14"

    # bytes: hex literal
    assert gen._sql_literal(b"\x00\xff") == "X'00ff'"
    assert gen._sql_literal(b"abc") == "X'616263'"

    # bytearray: то же
    assert gen._sql_literal(bytearray(b"\x01\x02")) == "X'0102'"

    # Регрессия: int/float/str/None/bool ведут себя как раньше
    assert gen._sql_literal(None) == "NULL"
    assert gen._sql_literal(True) == "TRUE"
    assert gen._sql_literal(False) == "FALSE"
    assert gen._sql_literal(42) == "42"
    assert gen._sql_literal(3.14) == "3.14"
    assert gen._sql_literal("hi") == "'hi'"


# ---------------------------------------------------------------------------
# 3.26: alias multi-part quoting
# ---------------------------------------------------------------------------

def test_multi_part_identifier_quoted_correctly(monkeypatch):
    """Alias с точкой не должен split'иться на multi-part identifier.

    Регрессия: `quote_identifier("foo.bar")` → `"foo"."bar"` AS — синтаксическая
    ошибка. Решение: для alias используется quote_single_identifier.
    """
    from custom_tools.text_to_sql import dialects

    # Подменим диалект, чтобы тест был детерминирован
    monkeypatch.setattr(dialects, "get_current_dialect_name", lambda dsn=None: "postgres")

    # Одиночный alias — не split'ится по точке
    assert dialects.quote_single_identifier("foo.bar") == '"foo.bar"'
    # Multi-part identifier — split'ится (для FQ-имён)
    assert dialects.quote_identifier("schema.table.column") == '"schema"."table"."column"'

    # End-to-end: alias с точкой в _generate_from_linked_entities → корректный AS
    gen = SQLGenerator()
    context = {
        "linked_entities": {
            "metrics": [
                {"name": "metric.total", "table": "orders", "column": "amount", "aggregation": "sum"},
            ],
            "dimensions": [
                {"name": "dim.label", "table": "orders", "column": "region"},
            ],
            "filters": {},
        },
        "joins": [],
    }
    result = gen._generate_from_linked_entities(context)
    assert "error" not in result, result
    sql = result["sql_query"]
    # Alias 'metric.total' и 'dim.label' должны остаться unsplit
    assert 'AS "metric.total"' in sql
    assert 'AS "dim.label"' in sql
    # А expression FQ-имя orders.amount — multi-part квотировано
    assert '"orders"."amount"' in sql
    assert '"orders"."region"' in sql


# ---------------------------------------------------------------------------
# 3.27: safety_issues propagate в error-path
# ---------------------------------------------------------------------------

def test_safety_error_propagates(monkeypatch):
    """Если structured_result содержит error + sql_query, и SQL не безопасен,
    safety_issues должны быть добавлены к propagated error, а не теряться.
    """
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "1")
    # Отключим schema-validation, чтобы он не съел error раньше
    monkeypatch.setenv("TEXT_TO_SQL_VALIDATE_SCHEMA", "0")

    gen = SQLGenerator()

    # Подменим _generate_from_linked_entities, чтобы вернул error+sql_query.
    unsafe_sql = "DROP TABLE users; SELECT 1"
    monkeypatch.setattr(
        gen,
        "_generate_from_linked_entities",
        lambda ctx: {"error": "schema check failed", "sql_query": unsafe_sql},
    )

    # Подменим safety validator, чтобы он реально отметил SQL как unsafe.
    monkeypatch.setattr(
        gen.safety_validator,
        "validate",
        lambda sql: {"is_safe": False, "issues": [{"type": "DML_FORBIDDEN", "detail": "DROP found"}]},
    )

    result = gen.generate_sql(json.dumps({"linked_entities": {}}), "anything")
    # Оригинальный error сохранён
    assert result.get("error") == "schema check failed"
    assert result.get("sql_query") == unsafe_sql
    # И safety_issues пробросились
    assert "safety_issues" in result, result
    assert result["safety_issues"]
    assert result["safety_issues"][0]["type"] == "DML_FORBIDDEN"
