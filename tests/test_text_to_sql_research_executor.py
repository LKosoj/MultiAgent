"""W2-01 contracts for the bounded ``QueryPurpose.RESEARCH`` boundary."""

from __future__ import annotations

import sqlite3

import pytest

from custom_tools.text_to_sql import core as core_facade
from custom_tools.text_to_sql.core import _sql_generation_api
from custom_tools.text_to_sql.core._db_exec import (
    QueryExecutionRequest,
    QueryExecutor,
    QueryPurpose,
)
from custom_tools.text_to_sql.validators import safety_config
from db_plugins.base import Capability, DatabaseCapabilities, EnforcementMode, PluginHealth
from tool_runtime_context import (
    SupervisorExecutionEvidence,
    reset_tool_runtime_context,
    set_tool_runtime_context,
)
from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded


def _capabilities(**overrides):
    native = Capability.supported(EnforcementMode.DRIVER, "TEST_NATIVE")
    values = {
        "dialect": "sqlite",
        "read_only": native,
        "statement_timeout": native,
        "cancellation": native,
        "explain": native,
        "introspection": native,
        "composite_fk_introspection": Capability.unsupported("TEST_UNUSED"),
        "parameter_binding": Capability.unsupported("TEST_UNUSED"),
    }
    values.update(overrides)
    return DatabaseCapabilities(**values)


class _ResearchPlugin:
    dialect = "sqlite"

    def __init__(self, capabilities=None):
        self.capabilities = capabilities or _capabilities()
        self.calls: list[object] = []

    def get_capabilities(self, _dsn=None):
        return self.capabilities

    def probe_capabilities(self, _conn=None, _dsn=None):
        self.calls.append("probe")
        return PluginHealth("sqlite", self.capabilities, True, ("TEST_PROBE_OK",))

    def connect(self, dsn):
        self.calls.append(("connect", dsn))
        return object()

    def close(self, _conn):
        self.calls.append("close")

    def set_statement_timeout(self, _conn, timeout_ms):
        self.calls.append(("timeout", timeout_ms))

    def execute_select(self, _conn, sql, row_limit):
        self.calls.append(("execute", sql, row_limit))
        return {
            "success": True,
            "data": [[1]],
            "columns": ["id"],
            "rows_affected": 1,
            "error_message": None,
        }

    def execute_select_bound(self, _conn, sql, parameters, row_limit):
        self.calls.append(("execute_bound", sql, parameters, row_limit))
        return {
            "success": True,
            "data": [[parameters[0]]],
            "columns": ["value"],
            "rows_affected": 1,
            "error_message": None,
        }


def _research_request(
    sql: str,
    *,
    row_limit: int | None = 5,
    dsn: str = "sqlite:///tmp/research.db",
    **kwargs,
):
    return QueryExecutionRequest(
        sql_query=sql,
        purpose=QueryPurpose.RESEARCH,
        row_limit=row_limit,
        dsn=dsn,
        **kwargs,
    )


def _policy(profile: str):
    return safety_config._policy_from_profile(
        profile,
        safety_config.load_safety_profile(profile),
    )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO items(id) VALUES (1)",
        "DROP TABLE items",
        "WITH changed AS (DELETE FROM items RETURNING id) SELECT id FROM changed LIMIT 1",
        "SELECT * FROM items LIMIT 1",
        "SELECT items.* FROM items LIMIT 1",
        "SELECT id FROM items",
        "SELECT id FROM items LIMIT 0",
        "SELECT id FROM items LIMIT ?",
        "SELECT id FROM items LIMIT (1 + 1)",
        "SELECT id FROM items LIMIT (SELECT 1)",
        "SELECT id FROM items LIMIT 1; /* stacked */ SELECT id FROM items LIMIT 1",
        "SELECT id FROM items LIMIT 6",
    ],
)
def test_research_rejects_unsafe_or_unbounded_sql_before_connection(monkeypatch, sql):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("RESEARCH static validation must run before safety execution")
        ),
    )
    plugin = _ResearchPlugin()

    with pytest.raises(ValueError):
        QueryExecutor(get_plugin=lambda _dsn: plugin).execute(_research_request(sql))

    assert plugin.calls == []


@pytest.mark.parametrize(
    "sql",
    [
        "WITH selected AS (SELECT id INTO copied FROM items) "
        "SELECT id FROM selected LIMIT 1",
        "WITH selected AS (SELECT id FROM items FOR UPDATE) "
        "SELECT id FROM selected LIMIT 1",
        "SELECT id FROM (SELECT id FROM items FOR SHARE) selected LIMIT 1",
        "SELECT id FROM (SELECT id FROM items FOR KEY SHARE) selected LIMIT 1",
    ],
)
def test_research_rejects_nested_into_and_locks_before_connection(monkeypatch, sql):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("nested lock rejection must precede safety execution")
        ),
    )
    plugin = _ResearchPlugin()

    with pytest.raises(ValueError, match="read-only SELECT"):
        QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
            _research_request(
                sql,
                row_limit=1,
                dsn="postgresql://user:pass@db/research",
            )
        )

    assert plugin.calls == []


@pytest.mark.parametrize(
    ("dsn", "sql"),
    [
        (
            "postgresql://user:pass@db/research",
            "SELECT ROW(items.*) FROM items LIMIT 1",
        ),
        ("mysql://user:pass@db/research", "SELECT (items).* FROM items LIMIT 1"),
        ("duckdb:///tmp/research.db", "SELECT COLUMNS(*) FROM items LIMIT 1"),
        (
            "duckdb:///tmp/research.db",
            "SELECT COLUMNS('^id$') FROM items LIMIT 1",
        ),
        (
            "impala://user:pass@db/research",
            "WITH values_cte AS (SELECT * FROM items) "
            "SELECT id FROM values_cte LIMIT 1",
        ),
    ],
)
def test_research_rejects_recursive_and_dynamic_star_forms_before_connection(
    monkeypatch,
    dsn,
    sql,
):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("star rejection must precede safety execution")
        ),
    )
    plugin = _ResearchPlugin()

    with pytest.raises(ValueError, match="star"):
        QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
            _research_request(sql, row_limit=1, dsn=dsn)
        )

    assert plugin.calls == []


@pytest.mark.parametrize(
    ("dsn", "sql"),
    [
        (
            "sqlite:///tmp/research.db",
            "SELECT COUNT(*) AS total FROM items LIMIT (1)",
        ),
        (
            "postgresql://user:pass@db/research",
            "SELECT COUNT(*) AS total FROM items FETCH FIRST (1) ROWS ONLY",
        ),
        (
            "mysql://user:pass@db/research",
            "SELECT COUNT(*) AS total FROM items LIMIT (1)",
        ),
        (
            "duckdb:///tmp/research.db",
            "SELECT COUNT(*) AS total FROM items LIMIT (1)",
        ),
        (
            "impala://user:pass@db/research",
            "SELECT COUNT(*) AS total FROM items LIMIT (1)",
        ),
        (
            "sapiq://user:pass@db/research",
            "SELECT TOP (1) COUNT(*) AS total FROM items",
        ),
    ],
)
def test_research_accepts_count_star_across_dialects(monkeypatch, dsn, sql):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    plugin = _ResearchPlugin()

    result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
        _research_request(sql, row_limit=1, dsn=dsn)
    )

    assert result.success is True, result.outcome["error_message"]
    assert ("execute", sql, 1) in plugin.calls


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(items.*) FROM items LIMIT 1",
        "SELECT COUNT(DISTINCT *) FROM items LIMIT 1",
        "SELECT SUM(*) FROM items LIMIT 1",
        "SELECT ROW(COUNT(items.*)) FROM items LIMIT 1",
    ],
)
def test_research_count_exception_does_not_allow_other_star_forms_before_connect(
    monkeypatch,
    sql,
):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe star rejection must precede safety execution")
        ),
    )
    plugin = _ResearchPlugin()

    with pytest.raises(ValueError, match="star"):
        QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
            _research_request(sql, row_limit=1)
        )

    assert plugin.calls == []


@pytest.mark.parametrize(
    ("dsn", "sql"),
    [
        ("sqlite:///tmp/research.db", "SELECT id FROM items LIMIT (((2)))"),
        (
            "postgresql://user:pass@db/research",
            "SELECT id FROM items FETCH FIRST (((2))) ROWS ONLY",
        ),
        ("mysql://user:pass@db/research", "SELECT id FROM items LIMIT ((2))"),
        ("duckdb:///tmp/research.db", "SELECT id FROM items LIMIT (((2)))"),
        ("impala://user:pass@db/research", "SELECT id FROM items LIMIT ((2))"),
        ("sapiq://user:pass@db/research", "SELECT TOP (((2))) id FROM items"),
    ],
)
def test_research_accepts_parenthesized_literal_limits_across_dialects(
    monkeypatch,
    dsn,
    sql,
):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    plugin = _ResearchPlugin()

    result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
        _research_request(sql, row_limit=2, dsn=dsn)
    )

    assert result.success is True, result.outcome["error_message"]
    assert ("execute", sql, 2) in plugin.calls


@pytest.mark.parametrize(
    ("dsn", "sql"),
    [
        ("sqlite:///tmp/research.db", "SELECT id FROM items LIMIT ((?))"),
        (
            "postgresql://user:pass@db/research",
            "SELECT id FROM items FETCH FIRST (($1)) ROWS ONLY",
        ),
        ("mysql://user:pass@db/research", "SELECT id FROM items LIMIT ((1 + 1))"),
        ("duckdb:///tmp/research.db", "SELECT id FROM items LIMIT ((SELECT 1))"),
        (
            "impala://user:pass@db/research",
            "SELECT id FROM items LIMIT (CAST(2 AS INT))",
        ),
        ("sapiq://user:pass@db/research", "SELECT TOP (@rows) id FROM items"),
    ],
)
def test_research_rejects_non_literal_limits_across_dialects_before_connection(
    monkeypatch,
    dsn,
    sql,
):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("LIMIT rejection must precede safety execution")
        ),
    )
    plugin = _ResearchPlugin()

    with pytest.raises(ValueError, match="positive literal"):
        QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
            _research_request(sql, row_limit=2, dsn=dsn)
        )

    assert plugin.calls == []


def test_research_runtime_row_limit_is_authoritative_before_safety_and_connect(
    monkeypatch,
):
    monkeypatch.setenv("DB_EXECUTOR_ROW_LIMIT", "3")
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime row-limit rejection must precede safety execution")
        ),
    )
    plugin = _ResearchPlugin()

    result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
        _research_request("SELECT id FROM items LIMIT 3", row_limit=4)
    )

    assert result.success is False
    assert "exceeds DB_EXECUTOR_ROW_LIMIT 3" in result.outcome["error_message"]
    assert plugin.calls == []


def test_research_query_limit_cannot_exceed_runtime_default(monkeypatch):
    monkeypatch.setenv("DB_EXECUTOR_ROW_LIMIT", "3")
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("query row-limit rejection must precede safety execution")
        ),
    )
    plugin = _ResearchPlugin()

    with pytest.raises(ValueError, match="SQL LIMIT"):
        QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
            _research_request("SELECT id FROM items LIMIT 4", row_limit=None)
        )

    assert plugin.calls == []


@pytest.mark.parametrize(
    "parameters",
    (
        ["value"],
        (b"value",),
        (float("nan"),),
        (float("inf"),),
        (object(),),
    ),
)
def test_research_bound_parameters_require_exact_immutable_json_scalars(parameters):
    with pytest.raises((TypeError, ValueError)):
        _research_request(
            "SELECT id FROM items WHERE label = ? LIMIT 1",
            parameters=parameters,
        )


def test_research_bound_parameters_reject_custom_scalar_subclasses():
    class CustomString(str):
        pass

    with pytest.raises(TypeError):
        _research_request(
            "SELECT id FROM items WHERE label = ? LIMIT 1",
            parameters=(CustomString("value"),),
        )


def test_bound_parameters_are_research_only():
    with pytest.raises(ValueError, match="RESEARCH"):
        QueryExecutionRequest(
            sql_query="SELECT id FROM items LIMIT 1",
            purpose=QueryPurpose.FINAL,
            row_limit=1,
            dsn="sqlite:///tmp/research.db",
            parameters=(1,),
        )


@pytest.mark.parametrize(
    ("sql", "parameters"),
    (
        ("SELECT id FROM items WHERE label = ? LIMIT 1", ()),
        ("SELECT id FROM items WHERE label = ? LIMIT 1", ("a", "b")),
        ("SELECT id FROM items WHERE label = :label LIMIT 1", ("a",)),
        ("SELECT id FROM items WHERE label = $1 LIMIT 1", ("a",)),
    ),
)
def test_research_rejects_parameter_shape_before_safety_or_connection(
    monkeypatch,
    sql,
    parameters,
):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("parameter validation must precede safety execution")
        ),
    )
    plugin = _ResearchPlugin()

    with pytest.raises(ValueError, match="parameter"):
        QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
            _research_request(sql, row_limit=1, parameters=parameters)
        )

    assert plugin.calls == []


def test_research_bound_parameters_require_capability_before_connection(monkeypatch):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    plugin = _ResearchPlugin(
        _capabilities(
            parameter_binding=Capability.unverified(
                "TEST_PARAMETER_BINDING_UNVERIFIED",
                EnforcementMode.DRIVER,
            )
        )
    )

    result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
        _research_request(
            "SELECT id FROM items WHERE label = ? LIMIT 1",
            row_limit=1,
            parameters=("gold",),
        )
    )

    assert result.success is False
    assert result.outcome["capability_error"] == {
        "capability": "parameter_binding",
        "reason_code": "TEST_PARAMETER_BINDING_UNVERIFIED",
    }
    assert plugin.calls == []


def test_research_uses_static_safety_without_an_llm_call(monkeypatch):
    monkeypatch.setattr(
        _sql_generation_api,
        "_run_llm_safety_audit_with_timeout",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("RESEARCH must not spend a model call on trusted SQL")
        ),
    )
    plugin = _ResearchPlugin()

    result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
        _research_request("SELECT id FROM items ORDER BY id LIMIT 1", row_limit=1)
    )

    assert result.success is True


def test_research_preserves_exact_json_cell_types(monkeypatch):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )

    class TypedPlugin(_ResearchPlugin):
        def execute_select(self, _conn, sql, row_limit):
            self.calls.append(("execute", sql, row_limit))
            return {
                "success": True,
                "data": [[None, True, 7, "text"]],
                "columns": ["missing", "flag", "number", "text"],
                "rows_affected": 1,
                "error_message": None,
            }

    result = QueryExecutor(get_plugin=lambda _dsn: TypedPlugin()).execute(
        _research_request(
            "SELECT NULL AS missing, TRUE AS flag, 7 AS number, "
            "'text' AS text LIMIT 1",
            row_limit=1,
        )
    )

    assert result.data == [[None, True, 7, "text"]]
    assert [type(value) for value in result.data[0]] == [
        type(None),
        bool,
        int,
        str,
    ]


def test_research_rejects_non_json_cells_instead_of_stringifying(monkeypatch):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )

    class BytesPlugin(_ResearchPlugin):
        def execute_select(self, _conn, sql, row_limit):
            self.calls.append(("execute", sql, row_limit))
            return {
                "success": True,
                "data": [[b"binary"]],
                "columns": ["value"],
                "rows_affected": 1,
                "error_message": None,
            }

    plugin = BytesPlugin()
    result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
        _research_request("SELECT value FROM items LIMIT 1", row_limit=1)
    )

    assert result.success is False
    assert "exact JSON scalars" in result.error_message
    assert ("execute", "SELECT value FROM items LIMIT 1", 1) in plugin.calls


def test_research_executes_bound_parameters_without_interpolation(monkeypatch):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    supported = Capability.supported(EnforcementMode.DRIVER, "TEST_SUPPORTED")
    plugin = _ResearchPlugin(_capabilities(parameter_binding=supported))
    literal = "gold' OR 1=1 --"
    sql = "SELECT id FROM items WHERE label = ? LIMIT 1"

    result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
        _research_request(
            sql,
            row_limit=1,
            parameters=(literal,),
        )
    )

    assert result.success is True
    assert result.data == [[literal]]
    assert ("execute_bound", sql, (literal,), 1) in plugin.calls
    assert literal not in sql


def test_research_accepts_row_limit_narrower_than_runtime_default(monkeypatch):
    monkeypatch.setenv("DB_EXECUTOR_ROW_LIMIT", "3")
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    plugin = _ResearchPlugin()

    result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
        _research_request("SELECT id FROM items LIMIT 2", row_limit=2)
    )

    assert result.success is True, result.outcome["error_message"]
    assert ("execute", "SELECT id FROM items LIMIT 2", 2) in plugin.calls


def test_research_dry_run_keeps_preparation_read_semantics(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "1")
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    plugin = _ResearchPlugin()

    result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
        _research_request("SELECT id FROM items LIMIT 2", row_limit=2)
    )

    assert result.success is True
    assert result.executed is True
    assert result.to_mapping()["dry_run_only"] is False
    assert any(call == ("execute", "SELECT id FROM items LIMIT 2", 2) for call in plugin.calls)


def test_research_expired_deadline_stops_before_connection(monkeypatch):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    plugin = _ResearchPlugin()
    deadline = DeadlineBudget(10.0, 1, monotonic=lambda: 10.0)

    with pytest.raises(WorkflowDeadlineExceeded):
        QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
            _research_request("SELECT id FROM items LIMIT 1", deadline=deadline)
        )

    assert plugin.calls == []


@pytest.mark.parametrize("capability_name", ["read_only", "statement_timeout", "cancellation"])
def test_research_requires_each_execution_capability_before_connection(
    monkeypatch,
    capability_name,
):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    plugin = _ResearchPlugin(
        _capabilities(**{capability_name: Capability.unsupported("TEST_UNAVAILABLE")})
    )

    result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
        _research_request("SELECT id FROM items LIMIT 1")
    )

    assert result.success is False
    assert result.failure_code == "EXECUTION_FAILED"
    assert result.outcome["capability_error"] == {
        "capability": capability_name,
        "reason_code": "TEST_UNAVAILABLE",
    }
    assert plugin.calls == []


def test_research_cannot_widen_runtime_deadline_or_safety_policy(monkeypatch):
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    runtime_deadline = DeadlineBudget.from_duration(5)
    token = set_tool_runtime_context(
        {
            "deadline_budget": runtime_deadline,
            "safety_policy": _policy("extended"),
        }
    )
    try:
        with pytest.raises(ValueError, match="deadline cannot widen"):
            QueryExecutor(get_plugin=lambda _dsn: _ResearchPlugin()).execute(
                _research_request(
                    "SELECT id FROM items LIMIT 1",
                    deadline=DeadlineBudget.from_duration(10),
                )
            )
        with pytest.raises(ValueError, match="safety_policy cannot widen"):
            QueryExecutor(get_plugin=lambda _dsn: _ResearchPlugin()).execute(
                _research_request(
                    "SELECT id FROM items LIMIT 1",
                    safety_policy=_policy("default"),
                )
            )
    finally:
        reset_tool_runtime_context(token)


def test_research_uses_a_narrower_runtime_policy_and_real_read_only_sqlite_file(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "research.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE items (id INTEGER)")
        connection.execute("INSERT INTO items(id) VALUES (7)")

    seen = {}
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda _sql, **kwargs: seen.update(kwargs) or {"is_safe": True, "issues": []},
    )
    runtime_policy = _policy("default")
    requested_policy = _policy("extended")
    token = set_tool_runtime_context(
        {
            "safety_policy": runtime_policy,
            "supervisor_evidence": SupervisorExecutionEvidence("research-test", 1),
        }
    )
    try:
        result = QueryExecutor().execute(
            QueryExecutionRequest(
                sql_query="SELECT id FROM items LIMIT 1",
                purpose=QueryPurpose.RESEARCH,
                row_limit=1,
                dsn=f"sqlite:///{str(database_path).lstrip('/')}",
                safety_policy=requested_policy,
                deadline=DeadlineBudget.from_duration(5),
            )
        )
    finally:
        reset_tool_runtime_context(token)

    assert result.success is True, result.outcome["error_message"]
    assert result.data == [[7]]
    assert seen["safety_policy"] is requested_policy
    assert result.to_mapping()["timeout_enforcement_mode"] == "supervisor"
    assert result.to_mapping()["cancellation_enforcement_mode"] == "supervisor"
