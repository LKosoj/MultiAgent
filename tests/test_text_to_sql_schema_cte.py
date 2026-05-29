"""EPIC 8.9: тесты CTECollector — projected/alias columns CTE/subquery."""
import sqlglot

from custom_tools.text_to_sql.validators._schema_scope import ScopeResolver
from custom_tools.text_to_sql.validators._schema_cte import CTECollector


def _parse_select(sql: str):
    statements = sqlglot.parse(sql)
    assert statements, sql
    return statements[0]


def test_collect_cte_columns_returns_projected_columns_for_select_body():
    scope = ScopeResolver()
    ctes = CTECollector(scope)
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}, "id": {"type": "INTEGER"}}}}

    stmt = _parse_select("WITH sub AS (SELECT amount FROM orders) SELECT sub.amount FROM sub")
    select = stmt if hasattr(stmt, "args") and stmt.args.get("with") else stmt.this
    collected = ctes.collect_cte_columns(select, schema)

    assert "sub" in collected
    assert "amount" in collected["sub"]


def test_collect_cte_columns_uses_alias_columns_for_union_body():
    scope = ScopeResolver()
    ctes = CTECollector(scope)
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    sql = (
        "WITH sub(value) AS ("
        "SELECT amount FROM orders UNION ALL SELECT amount FROM orders"
        ") SELECT value FROM sub"
    )
    stmt = _parse_select(sql)
    collected = ctes.collect_cte_columns(stmt, schema)

    # Union с alias-columns: должны вернуться alias-имена колонок CTE.
    assert "sub" in collected
    assert "value" in collected["sub"]


def test_collect_cte_columns_returns_empty_when_no_with_clause():
    scope = ScopeResolver()
    ctes = CTECollector(scope)
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    stmt = _parse_select("SELECT amount FROM orders")
    collected = ctes.collect_cte_columns(stmt, schema)

    assert collected == {}


def test_alias_column_names_returns_set_of_clean_names():
    scope = ScopeResolver()
    ctes = CTECollector(scope)
    sql = "WITH sub(value, total) AS (SELECT amount, amount FROM orders) SELECT value FROM sub"
    stmt = _parse_select(sql)
    # Достаём первый CTE.
    cte_node = stmt.args["with"].expressions[0]
    names = ctes.alias_column_names(cte_node)
    assert names == {"value", "total"}
