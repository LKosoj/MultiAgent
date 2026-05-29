"""EPIC 7 (db_exec) — unit-тесты для нового и переработанного поведения.

Покрывает:
- 7.8: _normalize_jsonable без json round-trip;
- 7.25: _build_failure_result helper;
- 7.26: дедуп _parse_table_*;
- 7.7: finally без UnboundLocalError при сбое connect/close;
- 7.5: EXPLAIN slice через regex;
- 7.6/7.2: routing через sqlglot Command.
"""
import json
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql import core as core_module
from custom_tools.text_to_sql.core import secure_db_executor
from custom_tools.text_to_sql.core._db_exec import (
    _build_failure_result,
    _classify_statement,
    _classify_statement_regex,
    _extract_explain_body,
    _normalize_executor_result,
    _normalize_jsonable,
    _parse_table_parts_from_describe,
)


# === 7.8: _normalize_jsonable ===


def test_normalize_jsonable_matches_legacy_json_roundtrip_for_complex_payload():
    """_normalize_jsonable должен давать тот же результат, что и
    json.loads(json.dumps(..., ensure_ascii=False, default=str))."""
    payload = {
        "amount": Decimal("1.50"),
        "created_at": datetime(2024, 5, 12, 10, 30, 0),
        "birthday": date(1990, 1, 2),
        "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "nested": [
            (Decimal("0.1"), Decimal("0.2")),
            {"k": Decimal("3")},
        ],
        "bin": b"hello",
        "name": "alice",
        "active": True,
        "n": 42,
        "ratio": 3.14,
        "none": None,
    }
    expected = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    actual = _normalize_jsonable(payload)
    assert actual == expected


def test_normalize_jsonable_handles_top_level_list():
    rows = [(1, Decimal("2.5"), None), [3, "x"]]
    expected = json.loads(json.dumps(rows, ensure_ascii=False, default=str))
    assert _normalize_jsonable(rows) == expected


def test_normalize_jsonable_dict_keys_stringified():
    assert _normalize_jsonable({1: "a", (2, 3): "b"}) == {"1": "a", "(2, 3)": "b"}


def test_normalize_executor_result_preserves_data_columns_after_refactor():
    """Гарантируем, что normalize всё ещё переводит data/columns в JSON-форму."""
    start = time.time()
    result = _normalize_executor_result(
        {"success": True, "data": [(1, Decimal("2.5"))], "columns": ["a", "b"]},
        start_time=start,
        sql_query="SELECT 1",
        row_limit=10,
    )
    assert result["data"] == [[1, "2.5"]]
    assert result["columns"] == ["a", "b"]


# === 7.25: _build_failure_result helper ===


def test_build_failure_result_minimal_fields():
    start = time.time() - 0.001
    failure = _build_failure_result(start, "boom")
    assert failure["success"] is False
    assert failure["error_message"] == "boom"
    assert failure["data"] == []
    assert failure["columns"] == []
    assert failure["rows_affected"] == 0
    assert isinstance(failure["execution_time_ms"], int)
    assert failure["execution_time_ms"] >= 0


def test_build_failure_result_includes_safety_issues_when_provided():
    issues = [{"issue_type": "X", "description": "y"}]
    failure = _build_failure_result(time.time(), "Unsafe query.", safety_issues=issues)
    assert failure["safety_issues"] == issues


# === 7.26: _parse_table_parts_from_describe (dedup) ===


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("DESCRIBE users", ["users"]),
        ("DESC public.orders", ["public", "orders"]),
        ('DESCRIBE "My.Schema"."My.Table"', ["My.Schema", "My.Table"]),
        ("DESCRIBE `db`.`schema`.`table`", ["db", "schema", "table"]),
    ],
)
def test_parse_table_parts_from_describe(sql, expected, monkeypatch):
    monkeypatch.setenv("USE_SQLGLOT", "0")  # текстовый парсер не требует sqlglot
    assert _parse_table_parts_from_describe(sql) == expected


def test_legacy_aliases_still_importable():
    """Удаление имён сломало бы tests/test_sqlglot_integration.py — оставляем
    как алиасы."""
    from custom_tools.text_to_sql.core import (
        _parse_table_name_from_describe_sqlglot,
        _parse_table_name_simple,
        _parse_table_parts_from_describe_sqlglot,
    )

    assert _parse_table_name_from_describe_sqlglot("DESC public.orders") == "public.orders"
    assert _parse_table_name_simple("DESC users") == "users"
    assert _parse_table_parts_from_describe_sqlglot("DESC users") == ["users"]


# === 7.5: EXPLAIN slice через regex ===


@pytest.mark.parametrize(
    "sql,expected_body",
    [
        ("EXPLAIN SELECT 1", "SELECT 1"),
        ("  explain   select 1", "select 1"),
        ("EXPLAIN(VERBOSE, COSTS) SELECT 1", "SELECT 1"),
        ("EXPLAIN\nSELECT 1", "SELECT 1"),
    ],
)
def test_extract_explain_body(sql, expected_body):
    assert _extract_explain_body(sql) == expected_body


def test_extract_explain_body_rejects_non_explain():
    with pytest.raises(ValueError):
        _extract_explain_body("SELECT 1")


# === 7.7: finally без UnboundLocalError ===


def test_secure_executor_connect_failure_does_not_call_close(monkeypatch):
    close_calls = []

    class Plugin:
        def connect(self, dsn):
            raise RuntimeError("conn refused")

        def close(self, conn):
            close_calls.append(conn)

    dsn = "sqlite:///tmp/app.db"
    monkeypatch.setenv("DB_DSN", dsn)
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setattr(core_module, "get_plugin", lambda dsn: Plugin())
    monkeypatch.setattr(core_module, "call_openai_api", lambda **kw: '{"issues": []}')

    result = secure_db_executor("SELECT 1", dsn=dsn)

    assert result["success"] is False
    assert "conn refused" in (result["error_message"] or "")
    assert close_calls == []


def test_secure_executor_select_failure_still_closes_conn(monkeypatch):
    close_calls = []

    class Plugin:
        def connect(self, dsn):
            return "the-conn"

        def close(self, conn):
            close_calls.append(conn)

        def execute_select(self, conn, sql, row_limit=500):
            raise RuntimeError("query fail")

    dsn = "sqlite:///tmp/app.db"
    monkeypatch.setenv("DB_DSN", dsn)
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setattr(core_module, "get_plugin", lambda dsn: Plugin())
    monkeypatch.setattr(core_module, "call_openai_api", lambda **kw: '{"issues": []}')

    result = secure_db_executor("SELECT 1", dsn=dsn)

    assert result["success"] is False
    assert "query fail" in (result["error_message"] or "")
    assert close_calls == ["the-conn"]


# === 7.6/7.2: routing через sqlglot ===


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT 1", "select"),
        ("WITH cte AS (SELECT 1) SELECT * FROM cte", "select"),
        ("DESCRIBE users", "describe"),
        ("DESC public.orders", "describe"),
        ("EXPLAIN SELECT 1", "explain"),
        ("EXPLAIN(VERBOSE) SELECT 1", "explain"),
        ("-- leading comment\nDESCRIBE users", "describe"),
        (";\nEXPLAIN SELECT 1", "explain"),
        ("SHOW TABLES", "show"),
    ],
)
def test_classify_statement(monkeypatch, sql, expected):
    monkeypatch.setenv("USE_SQLGLOT", "1")
    assert _classify_statement(sql) == expected


def test_classify_statement_unknown_for_dml(monkeypatch):
    monkeypatch.setenv("USE_SQLGLOT", "1")
    # safety раньше отбьёт; classify обязан вернуть unknown для не-SELECT.
    kind = _classify_statement("INSERT INTO t VALUES (1)")
    assert kind == "unknown"


# === T9-exec: новые тесты ===


def test_explain_strategy_returns_failure_on_explain_unsupported_issue(monkeypatch):
    """_explain_strategy должна возвращать success=False при EXPLAIN_UNSUPPORTED."""
    import time as _time
    from custom_tools.text_to_sql.core._db_exec import _explain_strategy

    class MockPlugin:
        def explain(self, conn, sql):
            return {
                "plan": None,
                "estimated_cost": None,
                "rows_to_scan": None,
                "issues": [{"issue_type": "EXPLAIN_UNSUPPORTED", "description": "EXPLAIN not supported"}],
            }

    result = _explain_strategy(
        sql_query="EXPLAIN SELECT 1",
        plugin=MockPlugin(),
        conn=None,
        start=_time.time(),
        row_limit=500,
    )
    assert result["success"] is False
    assert "EXPLAIN_UNSUPPORTED" in result["error_message"] or "not supported" in result["error_message"]


def test_explain_strategy_returns_failure_on_plan_none_no_issues(monkeypatch):
    """_explain_strategy должна возвращать success=False если plan=None даже без явных issues."""
    import time as _time
    from custom_tools.text_to_sql.core._db_exec import _explain_strategy

    class MockPlugin:
        def explain(self, conn, sql):
            return {"plan": None, "estimated_cost": None, "rows_to_scan": None, "issues": []}

    result = _explain_strategy(
        sql_query="EXPLAIN SELECT 1",
        plugin=MockPlugin(),
        conn=None,
        start=_time.time(),
        row_limit=500,
    )
    assert result["success"] is False
    assert result["error_message"] is not None


def test_explain_strategy_returns_success_on_valid_plan(monkeypatch):
    """_explain_strategy должна возвращать success=True если план есть и issues пустые."""
    import time as _time
    from custom_tools.text_to_sql.core._db_exec import _explain_strategy

    class MockPlugin:
        def explain(self, conn, sql):
            return {"plan": "Seq Scan on t", "estimated_cost": None, "rows_to_scan": None, "issues": []}

    result = _explain_strategy(
        sql_query="EXPLAIN SELECT 1",
        plugin=MockPlugin(),
        conn=None,
        start=_time.time(),
        row_limit=500,
    )
    assert result["success"] is True
    assert result["data"] == [["Seq Scan on t"]]


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT 1", "select"),
        ("  select * from t", "select"),
        ("WITH cte AS (SELECT 1) SELECT * FROM cte", "select"),
        ("DESCRIBE users", "describe"),
        ("DESC public.orders", "describe"),
        ("EXPLAIN SELECT 1", "explain"),
        ("SHOW TABLES", "show"),
        ("-- comment\nSELECT 1", "select"),
        ("/* block comment */ DESCRIBE t", "describe"),
        ("INSERT INTO t VALUES (1)", "unknown"),
        ("UPDATE t SET a=1", "unknown"),
        ("DELETE FROM t", "unknown"),
    ],
)
def test_classify_statement_regex_fallback_without_sqlglot(sql, expected):
    """_classify_statement_regex должен корректно классифицировать SQL без sqlglot."""
    assert _classify_statement_regex(sql) == expected


def test_classify_statement_uses_regex_when_sqlglot_disabled(monkeypatch):
    """При USE_SQLGLOT=0 _classify_statement должен использовать regex-классификатор."""
    monkeypatch.setenv("USE_SQLGLOT", "0")
    assert _classify_statement("SELECT 1") == "select"
    assert _classify_statement("DESCRIBE t") == "describe"
    assert _classify_statement("EXPLAIN SELECT 1") == "explain"
    assert _classify_statement("SHOW TABLES") == "show"
    assert _classify_statement("INSERT INTO t VALUES (1)") == "unknown"
