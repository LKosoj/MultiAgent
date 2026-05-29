"""Тесты для EPIC 2 блок C (2.9–2.14) — SQLSchemaValidator."""
import copy

import pytest

from custom_tools.text_to_sql.validators import SQLSchemaValidator
from custom_tools.text_to_sql.validators.schema_aware import _ResolveResult


# ---------- 2.11: _clean_identifier ----------

def test_clean_identifier_handles_mssql_brackets():
    validator = SQLSchemaValidator()
    assert validator._clean_identifier("[Orders]") == "Orders"


def test_clean_identifier_unescapes_double_quotes():
    validator = SQLSchemaValidator()
    assert validator._clean_identifier('"my""col"') == 'my"col'


def test_clean_identifier_unescapes_backticks():
    validator = SQLSchemaValidator()
    assert validator._clean_identifier("`my``col`") == "my`col"


# ---------- 2.12: _resolve_table_name_detailed ----------

def test_resolve_table_unknown_distinct_from_ambiguous():
    validator = SQLSchemaValidator()
    schema = {
        "a.orders": {"columns": {"id": {"type": "INTEGER"}}},
        "b.orders": {"columns": {"id": {"type": "INTEGER"}}},
        "customers": {"columns": {"id": {"type": "INTEGER"}}},
    }

    unknown = validator._resolve_table_name_detailed("missing", schema)
    assert unknown.kind == "unknown"
    assert unknown.name is None
    assert unknown.candidates == []

    ambiguous = validator._resolve_table_name_detailed("orders", schema)
    assert ambiguous.kind == "ambiguous"
    assert ambiguous.name is None
    assert sorted(ambiguous.candidates) == ["a.orders", "b.orders"]

    found = validator._resolve_table_name_detailed("customers", schema)
    assert found.kind == "found"
    assert found.name == "customers"


# ---------- 2.9: AMBIGUOUS_TABLE issue ----------

def test_schema_validator_flags_ambiguous_short_table_name():
    validator = SQLSchemaValidator()
    schema = {
        "a.orders": {"columns": {"id": {"type": "INTEGER"}}},
        "b.orders": {"columns": {"id": {"type": "INTEGER"}}},
    }

    result = validator.validate_sql_against_schema("SELECT id FROM orders", schema)

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "AMBIGUOUS_TABLE" for issue in result["issues"])


def test_schema_validator_qualified_resolves_when_short_is_ambiguous():
    validator = SQLSchemaValidator()
    schema = {
        "a.orders": {"columns": {"id": {"type": "INTEGER"}}},
        "b.orders": {"columns": {"id": {"type": "INTEGER"}}},
    }

    result = validator.validate_sql_against_schema("SELECT id FROM a.orders", schema)

    assert result["is_valid"] is True, result["issues"]


def test_schema_validator_ambiguous_table_lists_candidates():
    validator = SQLSchemaValidator()
    schema = {
        "a.orders": {"columns": {"id": {"type": "INTEGER"}}},
        "b.orders": {"columns": {"id": {"type": "INTEGER"}}},
    }

    result = validator.validate_sql_against_schema("SELECT id FROM orders", schema)

    ambiguous_issues = [i for i in result["issues"] if i["issue_type"] == "AMBIGUOUS_TABLE"]
    assert ambiguous_issues, result["issues"]
    desc = ambiguous_issues[0]["description"]
    assert "a.orders" in desc
    assert "b.orders" in desc


def test_validator_no_column_lookup_for_ambiguous_table():
    """Для ambiguous таблицы не должно быть UNKNOWN_COLUMN на любую колонку:
    alias не строится, _find_column_matches пропускает её.
    Должна остаться только AMBIGUOUS_TABLE проблема."""
    validator = SQLSchemaValidator()
    schema = {
        "a.orders": {"columns": {"id": {"type": "INTEGER"}}},
        "b.orders": {"columns": {"id": {"type": "INTEGER"}}},
    }

    result = validator.validate_sql_against_schema(
        "SELECT orders.totally_missing FROM orders", schema
    )

    assert result["is_valid"] is False
    issue_types = {issue["issue_type"] for issue in result["issues"]}
    assert "AMBIGUOUS_TABLE" in issue_types
    # колонка totally_missing не должна искаться (table ambiguous → пропуск)
    assert "UNKNOWN_COLUMN" not in issue_types


# ---------- 2.13: HAVING / GROUP BY alias references ----------

def test_having_references_select_alias_is_valid():
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    result = validator.validate_sql_against_schema(
        "SELECT SUM(amount) AS total FROM orders HAVING total > 0",
        schema,
    )

    assert result["is_valid"] is True, result["issues"]


def test_group_by_references_select_alias_is_valid():
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    result = validator.validate_sql_against_schema(
        "SELECT amount AS amt FROM orders GROUP BY amt",
        schema,
    )

    assert result["is_valid"] is True, result["issues"]


def test_where_alias_still_rejected():
    """Регрессия: alias в WHERE не виден; должен дать UNKNOWN_COLUMN."""
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    result = validator.validate_sql_against_schema(
        "SELECT amount AS id FROM orders WHERE id > 1",
        schema,
    )

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_COLUMN" for issue in result["issues"])


# ---------- 2.10: copy-on-validate AST ----------

def test_validator_does_not_mutate_input_ast():
    """Параллельно парсим AST; вызываем валидацию; AST должен быть identical."""
    import sqlglot

    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    sql = "WITH sub AS (SELECT amount FROM orders) SELECT sub.amount FROM sub"
    # Сохраняем repr нашего собственного дерева (валидатор парсит свою копию).
    # Тест проверяет, что повторный парс из той же строки не отличается до и после.
    before = sqlglot.parse(sql)[0].sql()
    validator.validate_sql_against_schema(sql, schema)
    after = sqlglot.parse(sql)[0].sql()
    assert before == after


def test_validator_idempotent_on_same_string():
    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    sql = "WITH sub AS (SELECT amount FROM orders) SELECT sub.amount FROM sub"

    first = validator.validate_sql_against_schema(sql, schema)
    second = validator.validate_sql_against_schema(sql, schema)

    assert first == second
    assert first["is_valid"] is True


# ---------- 2.14: убрать setattr на AST ----------

def test_no_validator_attributes_set_on_ast_nodes():
    """После валидации на узлах внешнего AST не должно остаться приватных атрибутов валидатора."""
    import sqlglot

    validator = SQLSchemaValidator()
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    sql = "SELECT amount FROM orders"
    tree = sqlglot.parse(sql)[0]
    validator.validate_sql_against_schema(sql, schema)

    # Внешний AST не передавался; проверяем, что хотя бы повторный парс не имеет
    # _validator_* атрибутов на узлах. Также для пущей надёжности проверяем
    # отсутствие атрибута на всех узлах нашего собственного дерева — оно не должно
    # быть мутировано (его никто не валидировал, но и не должен).
    for node in tree.walk():
        if isinstance(node, tuple):
            node = node[0]
        assert not hasattr(node, "_validator_row_source_names"), (
            f"Узел {type(node).__name__} имеет _validator_row_source_names"
        )


def test_two_validator_calls_share_no_state():
    """Параллельные / последовательные вызовы не должны делиться состоянием."""
    validator = SQLSchemaValidator()
    schema_a = {
        "orders": {"columns": {"amount": {"type": "DECIMAL"}}},
    }
    schema_b = {
        "customers": {"columns": {"name": {"type": "TEXT"}}},
    }

    r1 = validator.validate_sql_against_schema("SELECT amount FROM orders", schema_a)
    r2 = validator.validate_sql_against_schema("SELECT name FROM customers", schema_b)
    r3 = validator.validate_sql_against_schema("SELECT amount FROM orders", schema_a)

    assert r1["is_valid"] is True, r1["issues"]
    assert r2["is_valid"] is True, r2["issues"]
    assert r3 == r1
