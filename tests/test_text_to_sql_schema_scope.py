"""EPIC 8.9: тесты ScopeResolver — резолв таблиц и алиасов."""
import pytest

from custom_tools.text_to_sql.validators._schema_scope import ScopeResolver, _ResolveResult


def test_clean_identifier_handles_mssql_brackets():
    scope = ScopeResolver()
    assert scope.clean_identifier("[Orders]") == "Orders"


def test_clean_identifier_unescapes_ansi_quotes():
    scope = ScopeResolver()
    assert scope.clean_identifier('"my""col"') == 'my"col'


def test_clean_identifier_unescapes_backticks():
    scope = ScopeResolver()
    assert scope.clean_identifier("`my``col`") == "my`col"


def test_clean_identifier_passthrough_for_bare_name():
    scope = ScopeResolver()
    assert scope.clean_identifier("orders") == "orders"


def test_clean_identifier_empty_value():
    scope = ScopeResolver()
    assert scope.clean_identifier(None) == ""
    assert scope.clean_identifier("  ") == ""


def test_resolve_table_name_detailed_found_exact():
    scope = ScopeResolver()
    schema = {"customers": {"columns": {"id": {"type": "INTEGER"}}}}
    result = scope.resolve_table_name_detailed("customers", schema)
    assert isinstance(result, _ResolveResult)
    assert result.kind == "found"
    assert result.name == "customers"
    assert result.candidates == ["customers"]


def test_resolve_table_name_detailed_unknown():
    scope = ScopeResolver()
    schema = {"customers": {"columns": {}}}
    result = scope.resolve_table_name_detailed("missing", schema)
    assert result.kind == "unknown"
    assert result.name is None
    assert result.candidates == []


def test_resolve_table_name_detailed_ambiguous():
    scope = ScopeResolver()
    schema = {
        "a.orders": {"columns": {}},
        "b.orders": {"columns": {}},
    }
    result = scope.resolve_table_name_detailed("orders", schema)
    assert result.kind == "ambiguous"
    assert result.name is None
    assert sorted(result.candidates) == ["a.orders", "b.orders"]


def test_resolve_table_name_detailed_short_match_unique():
    scope = ScopeResolver()
    schema = {"public.customers": {"columns": {}}}
    result = scope.resolve_table_name_detailed("customers", schema)
    assert result.kind == "found"
    assert result.name == "public.customers"


def test_resolve_table_name_shim_returns_only_found():
    scope = ScopeResolver()
    schema = {"a.orders": {"columns": {}}, "b.orders": {"columns": {}}}
    assert scope.resolve_table_name("orders", schema) is None
    assert scope.resolve_table_name("missing", schema) is None
    assert scope.resolve_table_name("a.orders", schema) == "a.orders"


def test_table_exists_in_schema():
    scope = ScopeResolver()
    schema = {"customers": {"columns": {}}}
    assert scope.table_exists_in_schema("customers", schema) is True
    assert scope.table_exists_in_schema("missing", schema) is False
