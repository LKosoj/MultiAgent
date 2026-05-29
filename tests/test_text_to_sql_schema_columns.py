"""EPIC 8.9: тесты ColumnResolver — резолв column references."""
import sqlglot
from sqlglot import expressions as exp

from custom_tools.text_to_sql.validators._schema_scope import ScopeResolver
from custom_tools.text_to_sql.validators._schema_columns import ColumnResolver


def test_find_column_matches_returns_actual_table_names():
    scope = ScopeResolver()
    cols = ColumnResolver(scope)
    schema = {
        "orders": {"columns": {"amount": {"type": "DECIMAL"}}},
        "customers": {"columns": {"name": {"type": "TEXT"}}},
    }

    matches = cols.find_column_matches("amount", ["orders", "customers"], schema)
    assert matches == ["orders"]


def test_find_column_matches_unqualified_lookup_in_all_tables_when_empty_candidates():
    scope = ScopeResolver()
    cols = ColumnResolver(scope)
    schema = {
        "orders": {"columns": {"amount": {"type": "DECIMAL"}}},
        "customers": {"columns": {"amount": {"type": "DECIMAL"}}},
    }

    matches = cols.find_column_matches("amount", [], schema)
    assert sorted(matches) == ["customers", "orders"]


def test_column_exists_in_schema_case_insensitive():
    scope = ScopeResolver()
    cols = ColumnResolver(scope)
    schema = {"orders": {"columns": {"Amount": {"type": "DECIMAL"}}}}

    assert cols.column_exists_in_schema("AMOUNT", "orders", schema) is True
    assert cols.column_exists_in_schema("missing", "orders", schema) is False


def test_row_source_has_column_handles_wildcard():
    scope = ScopeResolver()
    cols = ColumnResolver(scope)
    assert cols.row_source_has_column("anything", {"*"}) is True
    assert cols.row_source_has_column("amount", {"AMOUNT"}) is True
    assert cols.row_source_has_column("missing", {"amount"}) is False


def test_is_select_alias_reference_true_in_having():
    scope = ScopeResolver()
    cols = ColumnResolver(scope)
    stmt = sqlglot.parse_one("SELECT SUM(amount) AS total FROM orders HAVING total > 0")
    column_in_having = None
    for col in stmt.find_all(exp.Column):
        if col.name == "total" and col.find_ancestor(exp.Having) is not None:
            column_in_having = col
            break
    assert column_in_having is not None
    assert cols.is_select_alias_reference(column_in_having, {"total"}) is True


def test_is_select_alias_reference_false_in_where():
    scope = ScopeResolver()
    cols = ColumnResolver(scope)
    stmt = sqlglot.parse_one("SELECT amount AS id FROM orders WHERE id > 1")
    column_in_where = None
    for col in stmt.find_all(exp.Column):
        if col.name == "id" and col.find_ancestor(exp.Where) is not None:
            column_in_where = col
            break
    assert column_in_where is not None
    assert cols.is_select_alias_reference(column_in_where, {"id"}) is False
