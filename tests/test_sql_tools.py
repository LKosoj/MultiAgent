import os
import json
import logging
import re
import importlib
import urllib.parse
from types import SimpleNamespace

from custom_tools import sql_tools


def test_dsn_sanitizer():
    f = sql_tools._dsn_to_sanitized_name
    assert f("postgresql://user:pass@h:5432/sales.analytics") == "postgresql_h_5432_sales_analytics"
    assert f("sqlite:///abs/path.db") == "sqlite_abs_path_db"
    assert f("duckdb:///var/db/wh.duckdb") == "duckdb_var_db_wh_duckdb"


def _raw_pyodbc_dsn() -> str:
    odbc_connect = urllib.parse.quote_plus(
        "DRIVER={ODBC Driver 17};SERVER=db.example.com;UID=alice;PWD=topsecret"
    )
    return f"mssql+pyodbc:///?odbc_connect={odbc_connect}&driver=ODBC+Driver+17"


def test_get_distinct_values_redacts_exception_boundary(monkeypatch, caplog):
    raw_dsn = _raw_pyodbc_dsn()

    def fail_get_plugin(dsn):
        raise RuntimeError(f"connect failed {raw_dsn} person@example.com")

    monkeypatch.setattr("db_plugins.get_plugin", fail_get_plugin)

    with caplog.at_level(logging.ERROR, logger="custom_tools.sql_tools"):
        result = sql_tools.get_distinct_values("orders", "status", dsn=raw_dsn)

    serialized = json.dumps(result, ensure_ascii=False) + caplog.text
    assert result["success"] is False
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_get_distinct_values_redacts_plugin_error_message(monkeypatch):
    raw_dsn = _raw_pyodbc_dsn()

    class Plugin:
        def connect(self, dsn):
            return object()

        def close(self, conn):
            return None

        def build_distinct_values_query(self, table_name, column_name, limit):
            return "SELECT status FROM orders"

        def execute_select(self, conn, sql_query, row_limit=None):
            return {
                "success": False,
                "error_message": f"driver failed {raw_dsn} person@example.com",
            }

    monkeypatch.setattr("db_plugins.get_plugin", lambda dsn: Plugin())

    result = sql_tools.get_distinct_values("orders", "status", dsn=raw_dsn)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["success"] is False
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_get_distinct_values_redacts_success_values(monkeypatch):
    raw_dsn = _raw_pyodbc_dsn()

    class Plugin:
        def connect(self, dsn):
            return object()

        def close(self, conn):
            return None

        def build_distinct_values_query(self, table_name, column_name, limit):
            return "SELECT email FROM users"

        def execute_select(self, conn, sql_query, row_limit=None):
            return {
                "success": True,
                "data": [
                    ("person@example.com",),
                    ("+7 (495) 123-45-67",),
                    (raw_dsn,),
                ],
            }

    monkeypatch.setattr("db_plugins.get_plugin", lambda dsn: Plugin())

    result = sql_tools.get_distinct_values("users", "email", dsn=raw_dsn)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["success"] is True
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com", "+7 (495) 123-45-67"):
        assert raw_fragment not in serialized
    assert "[EMAIL]" in serialized
    assert "[PHONE]" in serialized
    assert "odbc_connect=***" in serialized


def test_sql_safety_check_redacts_llm_audit_failure(monkeypatch, caplog):
    raw_dsn = _raw_pyodbc_dsn()

    def fail_llm(**kwargs):
        raise RuntimeError(f"provider failed {raw_dsn} person@example.com")

    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.sql_validator",
        SimpleNamespace(validate=lambda sql_query, dsn=None: {"is_safe": True, "issues": []}),
    )
    monkeypatch.setattr("custom_tools.text_to_sql.core.call_openai_api", fail_llm)

    with caplog.at_level(logging.ERROR, logger="custom_tools.text_to_sql.core._sql_generation_api"):
        result = sql_tools.sql_safety_check("SELECT id FROM orders")

    serialized = json.dumps(result, ensure_ascii=False) + caplog.text
    assert result["is_safe"] is False
    assert result["llm_audit"] == "failed"
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_code_formatter_redacts_sqlglot_exception_and_sql(monkeypatch, caplog):
    raw_dsn = _raw_pyodbc_dsn()

    def fail_parse(*_args, **_kwargs):
        raise RuntimeError(f"format failed {raw_dsn} person@example.com")

    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setattr("custom_tools.text_to_sql.utils.parse_with_timeout", fail_parse)

    sql_query = f"SELECT '{raw_dsn}' AS dsn, 'person@example.com' AS email"
    with caplog.at_level(logging.ERROR, logger="custom_tools.text_to_sql.core._sql_generation_api"):
        result = sql_tools.code_formatter(sql_query)

    serialized = json.dumps(result, ensure_ascii=False) + caplog.text
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_schema_info_redacts_exception_boundary(monkeypatch, caplog):
    raw_dsn = _raw_pyodbc_dsn()

    class FailingLoader:
        def __init__(self, repo_root):
            pass

        def _load_sqlrag_schema(self, dsn):
            raise RuntimeError(f"schema failed {raw_dsn} person@example.com")

    monkeypatch.setattr("custom_tools.text_to_sql.schema_loader.SchemaLoader", FailingLoader)

    with caplog.at_level(logging.ERROR, logger="custom_tools.sql_tools"):
        result = sql_tools.schema_info("orders", dsn=raw_dsn)

    serialized = json.dumps(result, ensure_ascii=False) + caplog.text
    assert result["success"] is False
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


def test_sql_safety_check_ignores_comments_inside_strings(monkeypatch):
    monkeypatch.setenv("USE_SQLGLOT", "0")
    # После EPIC 1.9 LLM-аудит обязателен (fail-fast). Подменяем call_openai_api
    # на возвращающий валидный JSON без issues, чтобы проверять именно поведение
    # статического валидатора на строковых литералах.
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.call_openai_api",
        lambda **kwargs: '{"issues": []}',
    )
    q = "SELECT '-- not a comment' AS s;"
    res = sql_tools.sql_safety_check(q)
    assert res["is_safe"] is True
    issue_types = {i.get("issue_type") for i in res.get("issues", [])}
    assert "COMMENT" not in issue_types
    assert "MULTI_STATEMENT" not in issue_types


def test_sql_generation_mysql_quotes_and_joins(monkeypatch):
    # Устанавливаем диалект MySQL, чтобы проверить обратные кавычки
    monkeypatch.setenv("DB_DSN", "mysql://user:pass@localhost:3306/sales.analytics")
    monkeypatch.setenv("USE_SQLGLOT", "0")
    monkeypatch.setenv("TEXT_TO_SQL_VALIDATE_SCHEMA", "0")

    def fake_call_openai_api(**kwargs):
        return json.dumps({
            "sql_query": (
                "SELECT regions.region_name, SUM(orders.amount) AS revenue "
                "FROM orders LEFT JOIN regions ON orders.region_id = regions.id "
                "GROUP BY regions.region_name"
            )
        })

    monkeypatch.setattr("custom_tools.text_to_sql.sql_generator.call_openai_api", fake_call_openai_api)

    linked = {
        "linked_entities": {
            "metrics": [
                {"name": "revenue", "table": "orders", "column": "amount"}
            ],
            "dimensions": [
                {"name": "region", "table": "regions", "column": "region_name"}
            ],
            "filters": {}
        },
        # Связь между таблицами
        "joins": [
            {"from_table": "orders", "from_column": "region_id", "to_table": "regions", "to_column": "id", "join_type": "LEFT"}
        ]
    }
    ctx = json.dumps(linked, ensure_ascii=False)
    out = sql_tools.sql_generation_plugin(
        context=ctx,
        user_query="Доход по регионам",
        dsn="mysql://user:pass@localhost:3306/sales.analytics",
    )
    sql = out["sql_query"].lower()
    # Проверяем JOIN
    assert " join " in sql
    # Проверяем обратные кавычки MySQL
    assert "`orders`." in out["sql_query"] or "`regions`." in out["sql_query"]


def test_sql_generation_postgres_quotes(monkeypatch):
    # Проверяем двойные кавычки для Postgres
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@localhost:5432/sales.analytics")
    monkeypatch.setenv("USE_SQLGLOT", "0")
    monkeypatch.setenv("TEXT_TO_SQL_VALIDATE_SCHEMA", "0")

    def fake_call_openai_api(**kwargs):
        return json.dumps({
            "sql_query": (
                "SELECT orders.created_at, COUNT(orders.id) AS count "
                "FROM orders GROUP BY orders.created_at"
            )
        })

    monkeypatch.setattr("custom_tools.text_to_sql.sql_generator.call_openai_api", fake_call_openai_api)
    linked = {
        "linked_entities": {
            "metrics": [
                {"name": "count", "table": "orders", "column": "id"}
            ],
            "dimensions": [
                {"name": "date", "table": "orders", "column": "created_at"}
            ],
            "filters": {}
        },
        "joins": []
    }
    ctx = json.dumps(linked, ensure_ascii=False)
    out = sql_tools.sql_generation_plugin(
        context=ctx,
        user_query="Количество заказов по дням",
        dsn="postgresql://u:p@localhost:5432/sales.analytics",
    )
    # Должны быть двойные кавычки
    assert '"orders"."created_at"' in out["sql_query"] or '"orders"."id"' in out["sql_query"]
