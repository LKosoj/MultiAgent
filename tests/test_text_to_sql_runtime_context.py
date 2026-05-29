import sys
import types
import urllib.parse

import pytest


def _clear_workflow_cached_attrs():
    workflow_pkg = sys.modules.get("workflow")
    if workflow_pkg is None:
        return
    for attr in ("engine", "enhanced_engine", "WorkflowEngine", "EnhancedWorkflowEngine"):
        workflow_pkg.__dict__.pop(attr, None)


def _agent_system_stub():
    module = types.ModuleType("agent_system")

    class DynamicAgentSystem:
        pass

    module.DynamicAgentSystem = DynamicAgentSystem
    return module


def _agent_factory_stub():
    module = types.ModuleType("agent_factory")

    class AgentFactory:
        pass

    module.AgentFactory = AgentFactory
    module.AGENT_PROFILES = {}
    return module


@pytest.fixture
def stub_mcp_tools(monkeypatch):
    module_names = (
        "workflow.engine",
        "workflow.enhanced_engine",
        "agent_system",
        "agent_factory",
        "mcp_tools",
    )
    for module_name in module_names:
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    _clear_workflow_cached_attrs()
    monkeypatch.setitem(sys.modules, "agent_factory", _agent_factory_stub())
    monkeypatch.setitem(sys.modules, "agent_system", _agent_system_stub())
    monkeypatch.setitem(
        sys.modules,
        "mcp_tools",
        types.ModuleType("mcp_tools"),
    )
    sys.modules["mcp_tools"].mcp_clients = []
    sys.modules["mcp_tools"].mcp_tools = []
    yield
    for module_name in module_names:
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    _clear_workflow_cached_attrs()


def test_sql_generation_plugin_reads_runtime_dsn_when_argument_omitted():
    from custom_tools.text_to_sql.core import _sql_generation_api
    from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context

    class Generator:
        def generate_sql(self, context, user_query, dsn=None):
            return {"context": context, "query": user_query, "dsn": dsn}

    dsn = "postgresql://alice:secret@db.example.com/app"
    token = set_tool_runtime_context({"dsn": dsn})
    try:
        result = _sql_generation_api.sql_generation_plugin(
            "{}",
            "show revenue",
            sql_generator=Generator(),
        )
    finally:
        reset_tool_runtime_context(token)

    assert result["dsn"] == dsn


def test_sql_generation_plugin_requires_explicit_or_runtime_dsn(monkeypatch):
    from custom_tools.text_to_sql.core import _sql_generation_api

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/env.db")

    class Generator:
        def generate_sql(self, context, user_query, dsn=None):
            raise AssertionError("generate_sql must not run without runtime dsn")

    with pytest.raises(ValueError, match="requires explicit dsn"):
        _sql_generation_api.sql_generation_plugin(
            "{}",
            "show revenue",
            sql_generator=Generator(),
        )


def test_sql_safety_check_reads_runtime_dsn_when_argument_omitted():
    from custom_tools.text_to_sql.core import _sql_generation_api
    from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context

    seen = {}

    class Validator:
        def validate(self, sql_query, dsn=None):
            seen["dsn"] = dsn
            return {"is_safe": False, "issues": [{"issue_type": "TEST", "description": "stop"}]}

    dsn = "postgresql://alice:secret@db.example.com/app"
    token = set_tool_runtime_context({"dsn": dsn})
    try:
        result = _sql_generation_api.sql_safety_check(
            "select 1",
            sql_validator=Validator(),
        )
    finally:
        reset_tool_runtime_context(token)

    assert result["safety_status"] == "unsafe"
    assert seen["dsn"] == dsn


def test_sql_safety_check_preserves_empty_dsn_sentinel(monkeypatch):
    from custom_tools.text_to_sql.core import _sql_generation_api

    monkeypatch.setenv("DB_DSN", "postgresql://env_user:env_pass@db.example.com/env_db")
    seen = {}

    class Validator:
        def validate(self, sql_query, dsn=None):
            seen["dsn"] = dsn
            return {"is_safe": False, "issues": [{"issue_type": "TEST", "description": "stop"}]}

    result = _sql_generation_api.sql_safety_check(
        "select 1",
        sql_validator=Validator(),
        dsn="",
    )

    assert result["safety_status"] == "unsafe"
    assert seen["dsn"] == ""


def test_sql_safety_check_without_runtime_dsn_does_not_use_env_dsn(monkeypatch):
    from custom_tools.text_to_sql.core import _sql_generation_api
    from custom_tools.text_to_sql.validators import SQLSafetyValidator

    monkeypatch.setenv("DB_DSN", "mysql://env_user:env_pass@db.example.com/env_db")
    monkeypatch.setenv("USE_SQLGLOT", "1")
    _sql_generation_api._clear_llm_safety_cache()

    def fail_get_plugin(dsn):
        raise AssertionError(f"DB_DSN fallback must not be used, got {dsn}")

    monkeypatch.setattr("db_plugins.get_plugin", fail_get_plugin)
    monkeypatch.setattr(
        _sql_generation_api,
        "_run_llm_safety_audit_with_timeout",
        lambda sql_query, dsn=None: {"issues": []},
    )

    result = _sql_generation_api.sql_safety_check(
        "SELECT 1",
        sql_validator=SQLSafetyValidator(),
    )

    assert result["is_safe"] is True
    assert result["safety_status"] == "safe"
    assert result["llm_audit"] == "ok"


def test_workflow_tool_param_log_redaction_masks_nested_dsn():
    from workflow_redaction import _redact_workflow_log_value

    raw_dsn = "postgresql://alice:secret@db.example.com/app?api_key=rawkey"
    odbc_connect = urllib.parse.quote_plus(
        "Driver={ODBC Driver 17};Server=db1.example.com;"
        "Database=orders;UID=odbcuser;PWD=odbcsecret"
    )
    raw_pyodbc_dsn = f"mssql+pyodbc:///?odbc_connect={odbc_connect}&driver=ODBC+Driver+17"
    redacted = _redact_workflow_log_value({
        "dsn": raw_dsn,
        "pyodbc_dsn": raw_pyodbc_dsn,
        "nested": {"message": f"failed {raw_dsn} and {raw_pyodbc_dsn}"},
    })
    serialized = repr(redacted)

    assert "alice:secret" not in serialized
    assert "rawkey" not in serialized
    assert "odbcuser" not in serialized
    assert "odbcsecret" not in serialized
    assert "UID%3D" not in serialized
    assert "PWD%3D" not in serialized
    assert redacted["dsn"].startswith("postgresql://***:***@")
    assert "odbc_connect=***" in redacted["pyodbc_dsn"]


def test_workflow_log_redaction_fails_closed_when_dependency_import_fails(monkeypatch):
    import builtins

    from workflow_redaction import _redact_workflow_log_value

    real_import = builtins.__import__

    def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "custom_tools.text_to_sql.utils":
            raise ImportError("blocked for test")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    assert _redact_workflow_log_value({
        "dsn": "postgresql://alice:secret@db.example.com/app"
    }) == "<redacted>"


def test_save_successful_sql_reads_runtime_dsn_for_sqlrag_session(tmp_path, monkeypatch):
    from custom_tools.text_to_sql.core import _audit
    from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context

    dsn = "postgresql://alice:secret@db.example.com/app"
    monkeypatch.setattr(_audit, "get_facade_repo_root", lambda: tmp_path)
    monkeypatch.delenv("DB_DSN", raising=False)

    token = set_tool_runtime_context({"dsn": dsn})
    try:
        result = _audit.save_successful_sql("select 1", "show one", "{}")
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "saved"
    assert result["filename"].startswith("postgresql_db_example_com_app_")
    assert (tmp_path / "sqlrag" / result["filename"]).exists()


def test_save_successful_sql_requires_explicit_or_runtime_dsn(tmp_path, monkeypatch):
    from custom_tools.text_to_sql.core import _audit

    monkeypatch.setattr(_audit, "get_facade_repo_root", lambda: tmp_path)
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/env.db")

    with pytest.raises(ValueError, match="requires explicit dsn"):
        _audit.save_successful_sql("select 1")


def test_get_distinct_values_reads_runtime_dsn_when_argument_omitted(monkeypatch):
    import db_plugins
    from custom_tools import sql_tools
    from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context

    dsn = "sapiq://user:pass@host:2638/runtime.analytics"
    seen = {}

    class Plugin:
        def connect(self, dsn_arg):
            seen["connect_dsn"] = dsn_arg
            return object()

        def close(self, conn):
            pass

        def build_distinct_values_query(self, table_name, column_name, limit):
            return f"SELECT DISTINCT {column_name} FROM {table_name} LIMIT {limit}"

        def execute_select(self, conn, sql, row_limit=500):
            seen["sql"] = sql
            return {"success": True, "data": [("north",)], "error_message": None}

    monkeypatch.setenv("DB_DSN", "sapiq://user:pass@host:2638/stale.analytics")

    def get_plugin(dsn_arg):
        seen["plugin_dsn"] = dsn_arg
        return Plugin()

    monkeypatch.setattr(db_plugins, "get_plugin", get_plugin)

    token = set_tool_runtime_context({"dsn": dsn})
    try:
        result = sql_tools.get_distinct_values("DBA.sales", "region", limit=5)
    finally:
        reset_tool_runtime_context(token)

    assert result["success"] is True
    assert result["values"] == ["north"]
    assert seen["plugin_dsn"] == dsn
    assert seen["connect_dsn"] == dsn


def test_get_distinct_values_without_runtime_dsn_does_not_use_env_dsn(monkeypatch):
    from custom_tools import sql_tools

    monkeypatch.setenv("DB_DSN", "sapiq://user:pass@host:2638/stale.analytics")
    monkeypatch.setattr("db_plugins.get_plugin", lambda dsn: (_ for _ in ()).throw(AssertionError(dsn)))

    result = sql_tools.get_distinct_values("DBA.sales", "region", limit=5)

    assert result["success"] is False
    assert "DSN is required" in result["error_message"]


def test_schema_info_reads_runtime_dsn_when_argument_omitted(monkeypatch):
    from custom_tools import sql_tools
    from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context

    dsn = "sqlite:///tmp/runtime.db"
    seen = {}

    def load_schema(self, dsn_arg):
        seen["dsn"] = dsn_arg
        return {
            "orders": {
                "description": "Orders",
                "columns": {
                    "id": {"type": "INTEGER", "description": "identifier"},
                },
            }
        }

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/stale.db")
    monkeypatch.delenv("TEXT_TO_SQL_SCHEMA_INFO_ALLOW_INTROSPECTION", raising=False)
    monkeypatch.setattr("custom_tools.text_to_sql.schema_loader.SchemaLoader._load_sqlrag_schema", load_schema)

    token = set_tool_runtime_context({"dsn": dsn})
    try:
        result = sql_tools.schema_info("orders")
    finally:
        reset_tool_runtime_context(token)

    assert result["success"] is True
    assert seen["dsn"] == dsn
    assert result["table_info"]["table_name"].endswith("orders")


def test_schema_info_without_runtime_dsn_does_not_use_env_dsn(monkeypatch):
    from custom_tools import sql_tools

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/stale.db")
    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_loader.SchemaLoader._load_sqlrag_schema",
        lambda self, dsn: (_ for _ in ()).throw(AssertionError(dsn)),
    )

    result = sql_tools.schema_info("orders")

    assert result["success"] is False
    assert "DSN is required" in result["error_message"]


@pytest.mark.asyncio
async def test_workflow_agent_step_exposes_metadata_to_tool_runtime_context(stub_mcp_tools):
    from tool_runtime_context import get_tool_runtime_value
    from workflow.engine import WorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    dsn = "postgresql://alice:secret@db.example.com/app"

    class Agent:
        def run(self, task, stream=False):
            return get_tool_runtime_value("dsn")

    class Factory:
        def create_agent(self, **kwargs):
            return Agent()

    class ResourceManager:
        def record_api_call(self, workflow_id):
            pass

    engine = object.__new__(WorkflowEngine)
    engine.factory = Factory()
    engine.resource_manager = ResourceManager()

    step = WorkflowStep(
        id="sql_generation",
        task="generate",
        step_type="agent",
        agent_type="sql_generator_agent",
        metadata={"dsn": dsn},
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    assert await engine._execute_agent_step(step, context, "generate") == dsn


@pytest.mark.asyncio
async def test_enhanced_workflow_step_substitutes_metadata_before_agent_execution(stub_mcp_tools):
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    dsn = "postgresql://alice:secret@db.example.com/app"

    class PolicyEngine:
        def get_budget(self, scope, step):
            return None

    async def execute_agent(step, context, task, plan, budget):
        return step.metadata["dsn"]

    engine = object.__new__(EnhancedWorkflowEngine)
    engine.policy_engine = PolicyEngine()
    engine._execute_enhanced_agent_step = execute_agent

    step = WorkflowStep(
        id="sql_generation",
        task="generate",
        step_type="agent",
        agent_type="sql_generator_agent",
        metadata={"dsn": "{dsn}"},
    )
    context = WorkflowContext(
        workflow_id="wf-1",
        session_id="session-1",
        variables={"dsn": dsn},
    )

    result = await engine._execute_step_with_policy(step, context, None, 1)

    assert result.status.value == "completed"
    assert result.output == dsn
