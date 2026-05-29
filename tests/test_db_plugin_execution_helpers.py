from __future__ import annotations

import sqlite3
import sys
from types import SimpleNamespace

import pytest

from custom_tools import sql_tools
from db_plugins.base import BaseDBPlugin
from db_plugins.duckdb import DuckDBPlugin
from db_plugins.impala import ImpalaPlugin
from db_plugins.mysql import MySQLPlugin
from db_plugins.postgres import PostgresPlugin
from db_plugins.sapiq import SAPIQPlugin
from db_plugins.sqlite import SQLitePlugin


class RecordingCursor:
    def __init__(self, rows=None):
        self.rows = rows or [(idx,) for idx in range(10)]
        self.description = [("value",)]
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self

    def fetchmany(self, size):
        return self.rows[:size]

    def fetchall(self):
        return self.rows


class RecordingConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


@pytest.mark.parametrize(
    ("plugin", "expected_cap", "uses_subquery"),
    [
        (SQLitePlugin(), "LIMIT 3", False),
        (PostgresPlugin(), "LIMIT 3", False),
        (MySQLPlugin(), "LIMIT 3", False),
        (DuckDBPlugin(), "LIMIT 3", False),
        (ImpalaPlugin(), "LIMIT 3", False),
        (SAPIQPlugin(), "SELECT TOP 3 * FROM", True),
    ],
)
def test_execute_select_applies_row_cap(plugin, expected_cap, uses_subquery):
    cursor = RecordingCursor(rows=[(idx,) for idx in range(10)])
    result = plugin.execute_select(
        RecordingConn(cursor),
        "SELECT * FROM big_table WHERE id IN (SELECT id FROM small_table LIMIT 1)",
        row_limit=3,
    )

    executed_sql = cursor.executed[0][0]
    assert expected_cap in executed_sql
    assert ("limited_subquery" in executed_sql) is uses_subquery
    assert result["data"] == [(0,), (1,), (2,)]
    assert result["rows_affected"] == 3


@pytest.mark.parametrize("plugin", [SQLitePlugin(), PostgresPlugin(), MySQLPlugin(), DuckDBPlugin(), ImpalaPlugin()])
def test_execute_select_preserves_top_level_order_when_capping(plugin):
    cursor = RecordingCursor(rows=[(1, 10), (2, 5)])
    plugin.execute_select(
        RecordingConn(cursor),
        "SELECT id, score FROM big_table WHERE id IN (SELECT id FROM small_table LIMIT 1) ORDER BY score DESC",
        row_limit=3,
    )

    executed_sql = cursor.executed[0][0]
    assert "ORDER BY" in executed_sql.upper()
    assert "LIMIT 3" in executed_sql
    assert "limited_subquery" not in executed_sql


@pytest.mark.parametrize(
    ("sql", "row_limit", "expected_sql"),
    [
        ("SELECT * FROM big_table LIMIT 10", 500, "SELECT * FROM big_table LIMIT 10"),
        ("SELECT * FROM big_table LIMIT 1000", 500, "SELECT * FROM big_table LIMIT 500"),
        ("SELECT * FROM big_table", 500, "SELECT * FROM big_table LIMIT 500"),
        (
            "SELECT * FROM big_table WHERE id IN (SELECT id FROM small_table LIMIT 1)",
            500,
            "SELECT * FROM big_table WHERE id IN (SELECT id FROM small_table LIMIT 1) LIMIT 500",
        ),
        ("SELECT * FROM big_table LIMIT ALL", 500, "SELECT * FROM big_table LIMIT 500"),
        ("SELECT * FROM big_table LIMIT -1", 500, "SELECT * FROM big_table LIMIT 500"),
    ],
)
def test_limit_select_sql_caps_without_expanding_existing_top_level_limit(sql, row_limit, expected_sql):
    assert BaseDBPlugin().limit_select_sql(sql, row_limit) == expected_sql


def test_split_connection_dsn_and_schema_preserves_query():
    plugin = BaseDBPlugin()

    clean_dsn, schema = plugin.split_connection_dsn_and_schema(
        "postgresql://user:pass@host:5432/sales.analytics?sslmode=require"
    )

    assert clean_dsn == "postgresql://user:pass@host:5432/sales?sslmode=require"
    assert schema == "analytics"


@pytest.mark.parametrize(
    ("dsn", "clean_suffix", "schema"),
    [
        ("duckdb:///warehouse/file.duckdb.analytics", "/warehouse/file.duckdb", "analytics"),
        ("duckdb:///warehouse/file.duckdb/analytics", "/warehouse/file.duckdb", "analytics"),
        ("duckdb:///warehouse/file.duckdb", "/warehouse/file.duckdb", None),
        ("duckdb:///warehouse/my.db.backup.duckdb", "/warehouse/my.db.backup.duckdb", None),
    ],
)
def test_split_duckdb_connection_dsn_and_schema(dsn, clean_suffix, schema):
    clean_dsn, parsed_schema = BaseDBPlugin().split_connection_dsn_and_schema(dsn)

    assert clean_dsn.endswith(clean_suffix)
    assert parsed_schema == schema


def test_duckdb_parse_schema_does_not_treat_database_filename_as_schema():
    plugin = DuckDBPlugin()

    assert plugin.parse_schema_from_dsn("duckdb:///warehouse/my.db.backup.duckdb") is None
    assert plugin.normalize_schema_names(
        "duckdb:///warehouse/my.db.backup.duckdb",
        {"orders": {"columns": {}}},
    ) == {"main.orders": {"columns": {}}}


def test_parse_schema_from_dsn_uses_explicit_schema_or_database_semantics():
    assert PostgresPlugin().parse_schema_from_dsn("postgresql://user:pass@host:5432/sales") is None
    assert PostgresPlugin().parse_schema_from_dsn("postgresql://user:pass@host:5432/sales.analytics") == "analytics"
    assert SAPIQPlugin().parse_schema_from_dsn("sapiq://user:pass@host:2638/sales") is None
    assert SQLitePlugin().parse_schema_from_dsn("sqlite:///tmp/sales.db") is None
    assert MySQLPlugin().parse_schema_from_dsn("mysql://user:pass@host:3306/sales") == "sales"
    assert MySQLPlugin().parse_schema_from_dsn("mysql://user:pass@host:3306/sales.analytics") == "analytics"
    assert ImpalaPlugin().parse_schema_from_dsn("impala://user:pass@host:21050/default") == "default"
    assert ImpalaPlugin().parse_schema_from_dsn("impala://user:pass@host:21050/db.analytics") == "analytics"


def test_mysql_connect_uses_clean_database_target(monkeypatch):
    captured = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql):
            captured["read_only_sql"] = sql

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return FakeConn()

    fake_pymysql = SimpleNamespace(
        connect=fake_connect,
        cursors=SimpleNamespace(Cursor=object),
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)

    MySQLPlugin().connect("mysql://user%40domain:p%40ss%2Fword@localhost:3306/sales.analytics")

    # В MySQL schema IS database: explicit_schema ("analytics") передаётся как db
    # при подключении, а не путь URL ("sales"). Это intentional — см. connect() в mysql.py.
    assert captured["db"] == "analytics"
    assert captured["user"] == "user@domain"
    assert captured["password"] == "p@ss/word"


def test_mysql_connect_missing_pymysql_has_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymysql", None)

    with pytest.raises(RuntimeError, match="PyMySQL is required"):
        MySQLPlugin().connect("mysql://user:pass@localhost:3306/sales")


def test_postgres_connect_sets_explicit_schema_search_path_safely(monkeypatch):
    executed = []

    class FakeIdentifier:
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return '"' + self.value.replace('"', '""') + '"'

    class FakeSQL:
        def __init__(self, template):
            self.template = template

        def format(self, identifier):
            return self.template.replace("{}", str(identifier))

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed.append((str(query), params))

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    fake_psycopg = SimpleNamespace(
        connect=lambda **kwargs: FakeConn(),
        sql=SimpleNamespace(SQL=FakeSQL, Identifier=FakeIdentifier),
    )
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    PostgresPlugin().connect('postgresql://user:pass@localhost:5432/sales.analytics";drop')

    assert executed[0][0] == 'SET search_path TO "analytics"";drop"'
    assert executed[1][0] == "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;"


def test_sapiq_odbc_connection_string_decodes_credentials():
    _, base = SAPIQPlugin()._build_odbc_conn_str(
        "sapiq://user%40domain:p%40ss%2Fword@localhost:2638/sales.analytics"
    )

    assert "UID={user@domain}" in base
    assert "PWD={p@ss/word}" in base


def test_mysql_read_only_setup_failure_fails_closed(monkeypatch):
    captured = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql):
            raise RuntimeError("read-only unsupported")

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            captured["closed"] = True

    def fake_connect(**kwargs):
        return FakeConn()

    fake_pymysql = SimpleNamespace(
        connect=fake_connect,
        cursors=SimpleNamespace(Cursor=object),
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)

    with pytest.raises(RuntimeError, match="read-only"):
        MySQLPlugin().connect("mysql://user:pass@localhost:3306/sales")

    assert captured["closed"] is True


def test_mysql_read_only_setup_failure_can_explicitly_fail_open(monkeypatch):
    captured = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql):
            raise RuntimeError("read-only unsupported")

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            captured["closed"] = True

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return FakeConn()

    fake_pymysql = SimpleNamespace(
        connect=fake_connect,
        cursors=SimpleNamespace(Cursor=object),
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)

    conn = MySQLPlugin().connect("mysql://user:pass@localhost:3306/sales?read_only_fail_open=true")

    assert conn is not None
    assert "closed" not in captured


def test_duckdb_quote_identifier_keeps_schema_qualified_parts_separate():
    plugin = DuckDBPlugin()

    assert plugin.quote_identifier("analytics.sales") == "analytics.sales"
    assert plugin.quote_identifier("analytics.sales table") == 'analytics."sales table"'
    assert plugin.quote_identifier("main.orders") == "orders"


def test_quote_identifier_quotes_reserved_and_case_sensitive_identifiers():
    assert BaseDBPlugin().quote_identifier("select") == '"select"'
    assert BaseDBPlugin().quote_identifier("references") == '"references"'
    assert BaseDBPlugin().quote_identifier("default") == '"default"'
    assert BaseDBPlugin().quote_identifier("MixedCase") == '"MixedCase"'
    assert BaseDBPlugin().quote_identifier("public.User") == 'public."User"'
    assert MySQLPlugin().quote_identifier("order") == "`order`"
    assert MySQLPlugin().quote_identifier("CamelCase") == "`CamelCase`"
    assert ImpalaPlugin().quote_identifier("group") == "`group`"


def test_sapiq_distinct_values_query_uses_top_not_limit():
    query = SAPIQPlugin().build_distinct_values_query("DBA.sales", "region", 5)

    assert "TOP 5" in query
    assert "LIMIT" not in query.upper()
    assert "SELECT DISTINCT" in query


def test_sapiq_execute_select_keeps_existing_top_query():
    cursor = RecordingCursor(rows=[(idx,) for idx in range(10)])
    query = SAPIQPlugin().build_distinct_values_query("DBA.sales", "region", 5)

    result = SAPIQPlugin().execute_select(RecordingConn(cursor), query, row_limit=5)

    executed_sql = cursor.executed[0][0]
    assert executed_sql == query
    assert result["data"] == [(0,), (1,), (2,), (3,), (4,)]


def test_sapiq_execute_select_does_not_treat_top_inside_identifier_as_cap():
    cursor = RecordingCursor(rows=[(idx,) for idx in range(10)])

    result = SAPIQPlugin().execute_select(
        RecordingConn(cursor),
        "SELECT top_score FROM sales",
        row_limit=3,
    )

    executed_sql = cursor.executed[0][0]
    assert executed_sql.startswith("SELECT TOP 3 * FROM (SELECT top_score FROM sales)")
    assert result["data"] == [(0,), (1,), (2,)]


def test_sapiq_execute_select_keeps_cte_query_with_top_level_top():
    cursor = RecordingCursor(rows=[(idx,) for idx in range(10)])
    query = "WITH x AS (SELECT 1 AS id) SELECT TOP 5 * FROM x"

    result = SAPIQPlugin().execute_select(RecordingConn(cursor), query, row_limit=3)

    executed_sql = cursor.executed[0][0]
    assert executed_sql == query
    assert result["data"] == [(0,), (1,), (2,)]


def test_sapiq_odbc_connection_values_are_brace_escaped():
    _, base = SAPIQPlugin()._build_odbc_conn_str(
        "sapiq://user%7Dname:p%3BDBN%3Dother@host:2638/sales"
    )

    assert "UID={user}}name}" in base
    assert "PWD={p;DBN=other}" in base
    assert ";DBN=other;" not in base


def test_sapiq_connect_requires_explicit_fail_open_when_read_only_unenforced(monkeypatch):
    closed = {"called": False}
    attempts = {"count": 0}

    class FakeConn:
        def close(self):
            closed["called"] = True

    def fake_connect(*args, **kwargs):
        attempts["count"] += 1
        return FakeConn()

    fake_pyodbc = SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)

    with pytest.raises(RuntimeError, match="read-only session enforcement is not implemented"):
        SAPIQPlugin().connect("sapiq://user:pass@host:2638/sales")

    assert closed["called"] is True
    assert attempts["count"] == 1


def test_impala_connect_requires_explicit_fail_open_when_read_only_unenforced(monkeypatch):
    closed = {"called": False}

    class FakeConn:
        def close(self):
            closed["called"] = True

    fake_impala_dbapi = SimpleNamespace(connect=lambda **kwargs: FakeConn())
    monkeypatch.setitem(sys.modules, "impala", SimpleNamespace(dbapi=fake_impala_dbapi))
    monkeypatch.setitem(sys.modules, "impala.dbapi", fake_impala_dbapi)

    with pytest.raises(RuntimeError, match="read-only session enforcement is not implemented"):
        ImpalaPlugin().connect("impala://user:pass@host:21050/default")

    assert closed["called"] is True


def test_get_distinct_values_uses_plugin_query_builder(monkeypatch):
    import db_plugins

    dsn = "sapiq://user:pass@host:2638/sales.analytics"
    seen = {}

    class FakePlugin:
        def __init__(self):
            self.executed_sql = None

        def connect(self, dsn):
            seen["connect_dsn"] = dsn
            return object()

        def close(self, conn):
            pass

        def build_distinct_values_query(self, table_name, column_name, limit):
            return SAPIQPlugin().build_distinct_values_query(table_name, column_name, limit)

        def execute_select(self, conn, sql, row_limit=500):
            self.executed_sql = sql
            return {
                "success": True,
                "data": [("north",), ("south",)],
                "columns": ["region"],
                "error_message": None,
            }

    plugin = FakePlugin()
    monkeypatch.setenv("DB_DSN", "sapiq://user:pass@host:2638/stale")

    def get_plugin(dsn_arg):
        seen["get_plugin_dsn"] = dsn_arg
        return plugin

    monkeypatch.setattr(db_plugins, "get_plugin", get_plugin)

    result = sql_tools.get_distinct_values("DBA.sales", "region", limit=5, dsn=dsn)

    assert result["success"] is True
    assert result["values"] == ["north", "south"]
    assert seen["get_plugin_dsn"] == dsn
    assert seen["connect_dsn"] == dsn
    assert "TOP 5" in plugin.executed_sql
    assert "LIMIT" not in plugin.executed_sql.upper()


def test_sqlite_fk_preview_uses_referenced_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "FOREIGN KEY(user_id) REFERENCES users(id))"
    )
    conn.execute("INSERT INTO users (id, name) VALUES (1, 'Alice')")
    conn.execute("INSERT INTO orders (id, user_id) VALUES (10, 1)")

    result = SQLitePlugin().get_fk_preview(conn, "orders", "user_id", "users", max_rows=5)

    assert result["success"] is True
    assert result["data"] == [(1, "Alice")]
    schema = SQLitePlugin().introspect_schema(conn)
    assert schema["orders"]["columns"]["user_id"]["references"] == "users.id"


class PostgresFKPreviewCursor:
    def __init__(self, fk_ref_column=None):
        self.fk_ref_column = fk_ref_column
        self.executed = []
        self.last_sql = ""
        self.description = [("user_code",), ("name",)]

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.executed.append((sql, params))

    def fetchone(self):
        if "referential_constraints" in self.last_sql and self.fk_ref_column:
            return (self.fk_ref_column,)
        return None

    def fetchall(self):
        if "LOWER(column_name) LIKE '%name%'" in self.last_sql:
            return [("name",)]
        if "JOIN users r" in self.last_sql:
            return [("A1", "Alice")]
        return []


class PostgresFKPreviewConn:
    def __init__(self, cursor):
        self.cursor_obj = cursor

    def cursor(self):
        return self.cursor_obj


def test_postgres_fk_preview_uses_fk_metadata_ref_column():
    cursor = PostgresFKPreviewCursor(fk_ref_column="code")

    result = PostgresPlugin().get_fk_preview(
        PostgresFKPreviewConn(cursor),
        "orders",
        "user_code",
        "users",
        max_rows=5,
    )

    join_sql = cursor.executed[-1][0]
    metadata_sql = cursor.executed[0][0]
    assert result["success"] is True
    assert result["data"] == [("A1", "Alice")]
    assert "position_in_unique_constraint" in metadata_sql
    assert "ON t.user_code = r.code" in join_sql
    assert "r.id" not in join_sql


def test_postgres_fk_preview_fails_closed_without_ref_column_metadata():
    cursor = PostgresFKPreviewCursor(fk_ref_column=None)

    result = PostgresPlugin().get_fk_preview(
        PostgresFKPreviewConn(cursor),
        "orders",
        "user_code",
        "users",
        max_rows=5,
    )

    assert result["success"] is False
    assert "Referenced FK column is unknown" in result["error_message"]
    assert all("column_name = 'id'" not in sql for sql, _ in cursor.executed)
    assert all("JOIN users r" not in sql for sql, _ in cursor.executed)


class EmptyCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []


class EmptyConn:
    def __init__(self):
        self.cursor_obj = EmptyCursor()

    def cursor(self):
        return self.cursor_obj


def test_postgres_introspection_constraint_joins_include_constraint_scope():
    conn = EmptyConn()

    assert PostgresPlugin().introspect_schema(conn) == {}
    sql = conn.cursor_obj.executed[0][0]
    assert "tc.constraint_schema = kcu.constraint_schema" in sql
    assert "tc.table_schema = kcu.table_schema" in sql
    assert "rc.unique_constraint_schema" in sql


def test_mysql_introspection_constraint_joins_include_constraint_scope():
    conn = EmptyConn()

    assert MySQLPlugin().introspect_schema(conn) == {}
    sql = conn.cursor_obj.executed[0][0]
    assert "kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA" in sql
    assert "kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA" in sql


class BrokenConn:
    def cursor(self):
        raise RuntimeError("metadata unavailable")


def test_sapiq_introspection_error_is_not_silent_empty_schema():
    with pytest.raises(RuntimeError, match="SAP IQ schema introspection failed"):
        SAPIQPlugin().introspect_schema(BrokenConn())


class BrokenImpalaCursor:
    def execute(self, sql, params=None):
        raise RuntimeError("metadata unavailable")


class BrokenImpalaConn:
    def cursor(self):
        return BrokenImpalaCursor()


def test_impala_introspection_error_is_not_silent_empty_schema():
    with pytest.raises(RuntimeError, match="Impala schema introspection failed"):
        ImpalaPlugin().introspect_schema(BrokenImpalaConn())


class ImpalaCursor:
    def __init__(self):
        self.executed = []
        self.last_sql = ""

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.executed.append((sql, params))

    def fetchall(self):
        if "INFORMATION_SCHEMA.COLUMNS" in self.last_sql:
            return [
                ("analytics", "orders", "id", "INT", "NO", None),
                ("analytics", "orders", "amount", "DOUBLE", "YES", None),
            ]
        if "SHOW TABLE STATS" in self.last_sql:
            return []
        return []

    def fetchone(self):
        if "COUNT(*)" in self.last_sql:
            return (42,)
        return None


class ImpalaConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_impala_introspection_returns_standard_schema_shape():
    schema = ImpalaPlugin().introspect_schema(ImpalaConn(ImpalaCursor()), schema="analytics")

    assert schema == {
        "analytics.orders": {
            "description": "",
            "columns": {
                "id": {
                    "type": "INT",
                    "description": "",
                    "not_null": "True",
                    "default_value": "",
                    "constraint_type": "",
                    "references": "",
                },
                "amount": {
                    "type": "DOUBLE",
                    "description": "",
                    "not_null": "False",
                    "default_value": "",
                    "constraint_type": "",
                    "references": "",
                },
            },
        }
    }


def test_impala_estimate_row_count_does_not_compute_stats():
    cursor = ImpalaCursor()

    assert ImpalaPlugin().estimate_row_count(ImpalaConn(cursor), "analytics.orders") == 42
    assert all("COMPUTE STATS" not in sql for sql, _ in cursor.executed)
