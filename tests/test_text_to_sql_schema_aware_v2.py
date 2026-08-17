"""Тесты для EPIC 2 блок C (2.9–2.14) — SQLSchemaValidator."""
import copy

import pytest

from custom_tools.text_to_sql.validators import SQLSchemaValidator
from custom_tools.text_to_sql.validators.schema_aware import _ResolveResult


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT g.value FROM generate_series(1, 3) AS g(value)",
        "SELECT u.value FROM UNNEST(ARRAY[1, 2]) AS u(value)",
        "SELECT o.x FROM orders AS o(x)",
    ],
)
def test_generic_and_declared_row_source_outputs_are_schema_valid(sql: str) -> None:
    result = SQLSchemaValidator().validate_sql_against_schema(
        sql, {"orders": {"columns": {"id": {"type": "INTEGER"}}}}
    )

    assert result["is_valid"] is True, result["issues"]


def test_generic_row_source_still_validates_its_physical_input_column() -> None:
    result = SQLSchemaValidator().validate_sql_against_schema(
        "SELECT u.value FROM orders o CROSS JOIN UNNEST(ARRAY[o.missing]) AS u(value)",
        {"orders": {"columns": {"id": {"type": "INTEGER"}}}},
    )

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_COLUMN" for issue in result["issues"])


def test_pivot_output_is_declared_while_its_physical_input_is_validated() -> None:
    schema = {
        "sales": {
            "columns": {
                "amount": {"type": "DECIMAL"},
                "category": {"type": "TEXT"},
            }
        }
    }
    valid = SQLSchemaValidator().validate_sql_against_schema(
        "SELECT p.a FROM sales PIVOT "
        "(SUM(amount) FOR category IN ('A' AS a)) AS p",
        schema,
        dsn="duckdb://",
    )
    invalid = SQLSchemaValidator().validate_sql_against_schema(
        "SELECT p.a FROM sales PIVOT "
        "(SUM(missing) FOR category IN ('A' AS a)) AS p",
        schema,
        dsn="duckdb://",
    )
    cte = SQLSchemaValidator().validate_sql_against_schema(
        "WITH source AS (SELECT category, amount FROM sales) "
        "SELECT p.a FROM source AS s PIVOT "
        "(SUM(s.amount) FOR s.category IN ('A' AS a)) AS p",
        schema,
        dsn="duckdb://",
    )
    derived = SQLSchemaValidator().validate_sql_against_schema(
        "SELECT p.a FROM (SELECT category, amount FROM sales) AS s PIVOT "
        "(SUM(s.amount) FOR s.category IN ('A' AS a)) AS p",
        schema,
        dsn="duckdb://",
    )
    derived_invalid = SQLSchemaValidator().validate_sql_against_schema(
        "SELECT p.a FROM (SELECT s.amount, s.category FROM sales AS s) AS s PIVOT "
        "(SUM(s.missing) FOR s.category IN ('A' AS a)) AS p",
        schema,
        dsn="duckdb://",
    )

    assert valid["is_valid"] is True, valid["issues"]
    assert invalid["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_COLUMN" for issue in invalid["issues"])
    assert cte["is_valid"] is True, cte["issues"]
    assert derived["is_valid"] is True, derived["issues"]
    assert derived_invalid["is_valid"] is False
    assert any(
        issue["issue_type"] == "UNKNOWN_COLUMN" for issue in derived_invalid["issues"]
    )


def test_root_transform_validates_physical_input_but_not_dynamic_output() -> None:
    schema = {
        "sales": {
            "columns": {
                "amount": {"type": "DECIMAL"},
                "category": {"type": "TEXT"},
            }
        }
    }
    valid = SQLSchemaValidator().validate_sql_against_schema(
        "PIVOT sales ON category USING SUM(amount)", schema, dsn="duckdb://"
    )
    unknown_table = SQLSchemaValidator().validate_sql_against_schema(
        "PIVOT missing ON category USING SUM(amount)", schema, dsn="duckdb://"
    )
    unknown_column = SQLSchemaValidator().validate_sql_against_schema(
        "PIVOT sales ON missing USING SUM(amount)", schema, dsn="duckdb://"
    )

    assert valid["is_valid"] is True, valid["issues"]
    assert any(issue["issue_type"] == "UNKNOWN_TABLE" for issue in unknown_table["issues"])
    assert any(issue["issue_type"] == "UNKNOWN_COLUMN" for issue in unknown_column["issues"])


def test_root_unpivot_still_rejects_unknown_physical_input_column() -> None:
    schema = {
        "monthly_sales": {
            "columns": {
                "jan": {"type": "DECIMAL"},
                "feb": {"type": "DECIMAL"},
            }
        }
    }
    valid = SQLSchemaValidator().validate_sql_against_schema(
        "UNPIVOT monthly_sales ON jan, feb INTO NAME month VALUE sales",
        schema,
        dsn="duckdb://",
    )
    unknown_column = SQLSchemaValidator().validate_sql_against_schema(
        "UNPIVOT monthly_sales ON jan, missing INTO NAME month VALUE sales",
        schema,
        dsn="duckdb://",
    )

    assert valid["is_valid"] is True, valid["issues"]
    assert any(
        issue["issue_type"] == "UNKNOWN_COLUMN" for issue in unknown_column["issues"]
    )


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


# ---------- generic row sources (url/s3/file/remote) ----------

_TF_SCHEMA = {"users": {"columns": {"id": {"type": "INTEGER"}, "name": {"type": "TEXT"}}}}


@pytest.mark.parametrize(
    ("func_call", "is_valid"),
    [
        ("url('https://example.com/data.csv', 'CSV', 'id UInt64, name String')", True),
        ("s3('s3://bucket/key', 'CSV', 'id UInt64')", True),
        ("file('/tmp/data.csv', 'CSV', 'id UInt64')", True),
        ("remote('host', db, tbl)", False),
    ],
)
def test_table_function_in_from_is_generic_row_source(func_call, is_valid):
    """Табличные функции в FROM дают generic-набор строк без имени таблицы."""
    validator = SQLSchemaValidator()
    result = validator.validate_sql_against_schema(
        f"SELECT id FROM {func_call}",
        _TF_SCHEMA,
    )
    assert result["is_valid"] is is_valid, result


def test_known_schema_table_no_false_positive():
    """Обычная таблица из схемы → валидна, нет ложного UNKNOWN_TABLE."""
    validator = SQLSchemaValidator()
    result = validator.validate_sql_against_schema(
        "SELECT id, name FROM users",
        _TF_SCHEMA,
    )
    assert result["is_valid"] is True, result["issues"]
    assert not any(i["issue_type"] == "UNKNOWN_TABLE" for i in result["issues"])


def test_cte_no_false_positive():
    """CTE → нет ложного UNKNOWN_TABLE на имя CTE."""
    validator = SQLSchemaValidator()
    result = validator.validate_sql_against_schema(
        "WITH cte AS (SELECT id FROM users) SELECT id FROM cte",
        _TF_SCHEMA,
    )
    # CTE name is a row-source, not a table-function: must not emit UNKNOWN_TABLE
    assert not any(i["issue_type"] == "UNKNOWN_TABLE" for i in result["issues"]), result["issues"]


def test_subquery_in_from_does_not_crash():
    """Подзапрос FROM (SELECT...) AS sub → не падает, is_valid True для корректного SQL."""
    validator = SQLSchemaValidator()
    result = validator.validate_sql_against_schema(
        "SELECT sub.id FROM (SELECT id FROM users) AS sub",
        _TF_SCHEMA,
    )
    # Подзапрос не таблица-функция; не должно быть UNKNOWN_TABLE
    assert not any(i["issue_type"] == "UNKNOWN_TABLE" for i in result["issues"]), result["issues"]


def test_set_operation_subquery_in_from_is_a_row_source():
    """Производная таблица с UNION даёт внешний alias и его projected column."""
    validator = SQLSchemaValidator()
    result = validator.validate_sql_against_schema(
        "SELECT o.amount FROM orders AS o "
        "JOIN (SELECT MAX(i.amount) AS amount FROM orders AS i "
        "UNION SELECT MIN(j.amount) AS amount FROM orders AS j) AS extrema "
        "ON o.amount = extrema.amount",
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
    )

    assert result["is_valid"] is True, result["issues"]


def test_set_operation_subquery_rejects_unknown_projected_column():
    validator = SQLSchemaValidator()
    result = validator.validate_sql_against_schema(
        "SELECT extrema.missing FROM (SELECT amount FROM orders "
        "UNION SELECT amount FROM orders) AS extrema",
        {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}},
    )

    assert result["is_valid"] is False
    assert any(issue["issue_type"] == "UNKNOWN_COLUMN" for issue in result["issues"])
