import sqlite3

import pytest

from custom_tools.text_to_sql.value_grounding import (
    _execute_lookup,
    ground_linked_filter_values,
    value_grounding_enabled,
)


@pytest.fixture(autouse=True)
def _admit_auxiliary_reads(monkeypatch):
    from custom_tools.text_to_sql import core
    from tool_runtime_context import (
        SupervisorExecutionEvidence,
        reset_tool_runtime_context,
        set_tool_runtime_context,
    )
    from workflow.deadline import DeadlineBudget

    monkeypatch.setattr(
        core,
        "sql_safety_check",
        lambda sql_query, **kwargs: {"is_safe": True, "issues": []},
    )
    token = set_tool_runtime_context(
        {
            "deadline_budget": DeadlineBudget.from_duration(60),
            "supervisor_evidence": SupervisorExecutionEvidence(
                "value-grounding-test",
                1,
            ),
        }
    )
    try:
        yield
    finally:
        reset_tool_runtime_context(token)


def _schema():
    return {
        "orders": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "city_id": {"type": "INTEGER", "constraint_type": "FK", "references": "cities.id"},
                "region": {"type": "TEXT"},
            }
        },
        "cities": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "name": {"type": "TEXT"},
            }
        },
    }


def _result(filter_info):
    return {
        "linked_entities": {
            "metrics": [],
            "dimensions": [],
            "filters": {"city": filter_info},
        },
        "joins": [],
        "join_success": True,
    }


def _sqlite_dsn(tmp_path):
    db_path = tmp_path / "grounding.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cities (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, city_id INTEGER, region TEXT)")
    conn.execute("INSERT INTO cities (id, name) VALUES (1, 'Москва')")
    conn.execute("INSERT INTO orders (id, city_id, region) VALUES (42, 1, 'north')")
    conn.commit()
    conn.close()
    return "sqlite://" + str(db_path)


def test_value_grounding_string_false_is_disabled():
    assert value_grounding_enabled("false") is False
    assert value_grounding_enabled("1") is True


def test_value_grounding_disabled_by_default_does_not_call_db(monkeypatch):
    def fail_get_plugin(_dsn):
        raise AssertionError("DB lookup should not run")

    monkeypatch.delenv("SCHEMA_LINKING_VALUE_GROUNDING", raising=False)
    monkeypatch.setattr("db_plugins.get_plugin", fail_get_plugin)
    source = _result({"table": "orders", "column": "region", "value": "north"})

    grounded = ground_linked_filter_values(
        source,
        original_entities={"filters": {"city": "north"}},
        db_schema=_schema(),
        dsn="sqlite:///tmp/missing.db",
    )

    assert grounded is source


def test_value_grounding_same_column_exact_match_preserves_db_type(tmp_path):
    dsn = _sqlite_dsn(tmp_path)

    grounded = ground_linked_filter_values(
        _result({"table": "orders", "column": "id", "value": "42"}),
        original_entities={"filters": {"city": "42"}},
        db_schema=_schema(),
        dsn=dsn,
        value_grounding=True,
    )

    filter_info = grounded["linked_entities"]["filters"]["city"]
    assert filter_info["value"] == 42
    assert filter_info["value_grounding"]["status"] == "matched_same_column"


def test_value_grounding_no_match_leaves_original_value(tmp_path):
    dsn = _sqlite_dsn(tmp_path)

    grounded = ground_linked_filter_values(
        _result({"table": "orders", "column": "region", "value": "west"}),
        original_entities={"filters": {"city": "west"}},
        db_schema=_schema(),
        dsn=dsn,
        value_grounding=True,
    )

    filter_info = grounded["linked_entities"]["filters"]["city"]
    assert filter_info["value"] == "west"
    assert filter_info["value_grounding"]["status"] == "no_match"


def test_value_grounding_date_range_does_not_query_db(monkeypatch):
    def fail_get_plugin(_dsn):
        raise AssertionError("date ranges must not query DB")

    monkeypatch.setattr("db_plugins.get_plugin", fail_get_plugin)
    value = {"start": "2026-01-01", "end": "2026-01-31"}

    grounded = ground_linked_filter_values(
        _result({"table": "orders", "column": "created_at", "value": value}),
        original_entities={"filters": {"city": value}},
        db_schema=_schema(),
        dsn="sqlite:///tmp/missing.db",
        value_grounding=True,
    )

    filter_info = grounded["linked_entities"]["filters"]["city"]
    assert filter_info["value"] == value
    assert filter_info["value_grounding"]["status"] == "skipped_date_range"


def test_value_grounding_fk_label_to_id_maps_text_to_code(tmp_path):
    dsn = _sqlite_dsn(tmp_path)

    grounded = ground_linked_filter_values(
        _result({"table": "orders", "column": "city_id", "value": "Москва"}),
        original_entities={"filters": {"city": "Москва"}},
        db_schema=_schema(),
        dsn=dsn,
        value_grounding=True,
    )

    filter_info = grounded["linked_entities"]["filters"]["city"]
    assert filter_info["value"] == 1
    assert filter_info["value_grounding"] == {
        "status": "matched_label_to_code",
        "source": "cities",
        "label_column": "name",
    }


def test_value_grounding_lookup_errors_fail_open_and_mask_dsn(monkeypatch):
    monkeypatch.setenv("PII_MASKING_ENABLED", "1")

    def fail_get_plugin(_dsn):
        raise RuntimeError(
            "lookup failed for person@example.com +7 (495) 123-45-67 "
            "postgresql://user:secret@host/db"
        )

    monkeypatch.setattr("db_plugins.get_plugin", fail_get_plugin)

    grounded = ground_linked_filter_values(
        _result({"table": "orders", "column": "region", "value": "north"}),
        original_entities={"filters": {"city": "north"}},
        db_schema=_schema(),
        dsn="postgresql://user:secret@host/db",
        value_grounding=True,
    )

    filter_info = grounded["linked_entities"]["filters"]["city"]
    assert filter_info["value"] == "north"
    assert filter_info["value_grounding"]["status"] == "lookup_error"
    error = filter_info["value_grounding"]["error"]
    assert "secret" not in error
    assert "person@example.com" not in error
    assert "+7 (495) 123-45-67" not in error


def test_value_grounding_lookup_routes_read_through_query_executor(monkeypatch):
    import db_plugins
    from custom_tools.text_to_sql import value_grounding as grounding_module
    from custom_tools.text_to_sql.core._db_exec import (
        QueryExecutionResult,
        QueryPurpose,
    )

    seen = {}

    class BuilderPlugin:
        def build_distinct_values_query(self, table, column, limit):
            return f"SELECT DISTINCT {column} FROM {table} LIMIT {limit}"

    class Executor:
        def __init__(self, *, get_plugin=None):
            seen["resolver"] = get_plugin

        def execute(self, request):
            seen["request"] = request
            return QueryExecutionResult(
                request.purpose,
                {
                    "success": True,
                    "data": [["north"]],
                    "columns": ["region"],
                    "rows_affected": 1,
                    "execution_time_ms": 1,
                    "error_message": None,
                    "dry_run_only": False,
                    "skipped_execution": False,
                    "sql_query": request.sql_query,
                    "applied_row_limit": request.row_limit,
                },
            )

    def resolver(_dsn):
        return BuilderPlugin()

    monkeypatch.setattr(db_plugins, "get_plugin", resolver)
    monkeypatch.setattr(grounding_module, "QueryExecutor", Executor)

    rows = _execute_lookup(
        "sqlite:///tmp/app.db",
        lambda plugin: plugin.build_distinct_values_query(
            "orders",
            "region",
            5,
        ),
        row_limit=5,
    )

    assert rows == [["north"]]
    assert seen["resolver"] is resolver
    assert seen["request"].purpose is QueryPurpose.GROUNDING
