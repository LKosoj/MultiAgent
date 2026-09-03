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


def test_resolve_dsn_precedence(monkeypatch):
    from custom_tools.text_to_sql.utils import resolve_dsn
    from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context

    monkeypatch.setenv("DB_DSN", "sqlite:///env.db")
    token = set_tool_runtime_context({"dsn": "sqlite:///runtime.db"})
    try:
        assert resolve_dsn("sqlite:///explicit.db", allow_env=True) == "sqlite:///explicit.db"
        assert resolve_dsn(None, allow_env=True) == "sqlite:///runtime.db"
    finally:
        reset_tool_runtime_context(token)

    assert resolve_dsn(None, allow_env=True) == "sqlite:///env.db"
    assert resolve_dsn(None, allow_env=False) is None


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
    from custom_tools.text_to_sql import core
    from db_plugins.base import (
        Capability,
        DatabaseCapabilities,
        EnforcementMode,
        PluginHealth,
    )
    from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context

    dsn = "sapiq://user:pass@host:2638/runtime.analytics"
    seen = {}

    class Plugin:
        dialect = "sapiq"

        def connect(self, dsn_arg):
            seen["connect_dsn"] = dsn_arg
            return object()

        def close(self, conn):
            pass

        def get_capabilities(self, _dsn=None):
            return DatabaseCapabilities(
                dialect=self.dialect,
                read_only=Capability.supported(
                    EnforcementMode.DATABASE,
                    "TEST_READ_ONLY",
                ),
                statement_timeout=Capability.supported(
                    EnforcementMode.DRIVER,
                    "TEST_DRIVER_TIMEOUT",
                ),
                cancellation=Capability.supported(
                    EnforcementMode.DRIVER,
                    "TEST_DRIVER_CANCELLATION",
                ),
                explain=Capability.unsupported("TEST_NOT_REQUIRED"),
                introspection=Capability.unsupported("TEST_NOT_REQUIRED"),
                composite_fk_introspection=Capability.unsupported(
                    "TEST_NOT_REQUIRED"
                ),
                parameter_binding=Capability.supported(
                    EnforcementMode.DRIVER,
                    "TEST_PARAMETER_BINDING",
                ),
            )

        def probe_capabilities(self, _conn=None, dsn=None):
            return PluginHealth(
                self.dialect,
                self.get_capabilities(dsn),
                True,
                ("TEST_PROBE_OK",),
            )

        def set_statement_timeout(self, _conn, timeout_ms):
            seen["timeout_ms"] = timeout_ms

        def build_distinct_values_query(self, table_name, column_name, limit):
            return f"SELECT DISTINCT {column_name} FROM {table_name} LIMIT {limit}"

        def execute_select(self, conn, sql, row_limit=500):
            seen["sql"] = sql
            return {
                "success": True,
                "data": [("north",)],
                "columns": ["region"],
                "rows_affected": 1,
                "error_message": None,
            }

    monkeypatch.setenv("DB_DSN", "sapiq://user:pass@host:2638/stale.analytics")

    def get_plugin(dsn_arg):
        seen["plugin_dsn"] = dsn_arg
        return Plugin()

    monkeypatch.setattr(db_plugins, "get_plugin", get_plugin)
    monkeypatch.setattr(
        core,
        "sql_safety_check",
        lambda sql_query, **kwargs: {"is_safe": True, "issues": []},
    )

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


def test_get_distinct_values_routes_read_through_query_executor(monkeypatch):
    import db_plugins
    from custom_tools import sql_tools
    from custom_tools.text_to_sql.core._db_exec import (
        QueryExecutionResult,
        QueryPurpose,
    )

    seen = {}

    class BuilderPlugin:
        def build_distinct_values_query(self, table_name, column_name, limit):
            return f"SELECT DISTINCT {column_name} FROM {table_name} LIMIT {limit}"

        def execute_select(self, *args, **kwargs):
            raise AssertionError("QueryExecutor owns execution")

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
    monkeypatch.setattr(sql_tools, "QueryExecutor", Executor)

    result = sql_tools.get_distinct_values(
        "DBA.sales",
        "region",
        limit=5,
        dsn="sqlite:///tmp/app.db",
    )

    assert result == {
        "success": True,
        "values": ["north"],
        "count": 1,
        "error_message": None,
    }
    assert seen["resolver"] is resolver
    assert seen["request"].purpose is QueryPurpose.DISTINCT
    assert seen["request"].row_limit == 5


def test_runtime_deadline_stops_distinct_read_before_connection(monkeypatch):
    import db_plugins
    from custom_tools import sql_tools
    from tool_runtime_context import (
        reset_tool_runtime_context,
        set_tool_runtime_context,
    )
    from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded

    class Plugin:
        def build_distinct_values_query(self, table_name, column_name, limit):
            return f"SELECT DISTINCT {column_name} FROM {table_name} LIMIT {limit}"

        def execute_select(self, *args, **kwargs):
            raise AssertionError("expired deadline must not execute")

        def connect(self, dsn):
            raise AssertionError("expired deadline must not connect")

    monkeypatch.setattr(db_plugins, "get_plugin", lambda _dsn: Plugin())
    deadline = DeadlineBudget(
        deadline_monotonic=10.0,
        deadline_at_ms=1,
        monotonic=lambda: 10.0,
    )
    token = set_tool_runtime_context({
        "dsn": "sqlite:///tmp/app.db",
        "deadline_budget": deadline,
    })
    try:
        with pytest.raises(WorkflowDeadlineExceeded):
            sql_tools.get_distinct_values("orders", "region", limit=5)
    finally:
        reset_tool_runtime_context(token)


def test_runtime_context_exposes_only_typed_deadline_budget():
    from tool_runtime_context import (
        get_runtime_context_deadline,
        reset_tool_runtime_context,
        set_tool_runtime_context,
    )
    from workflow.deadline import DeadlineBudget

    deadline = DeadlineBudget.from_duration(5)
    token = set_tool_runtime_context({"deadline_budget": deadline})
    try:
        assert get_runtime_context_deadline() is deadline
    finally:
        reset_tool_runtime_context(token)

    token = set_tool_runtime_context({"deadline_budget": "five seconds"})
    try:
        with pytest.raises(TypeError, match="DeadlineBudget"):
            get_runtime_context_deadline()
    finally:
        reset_tool_runtime_context(token)


def test_model_sql_explain_uses_operator_dry_run_and_runtime_deadline(monkeypatch):
    from custom_tools import sql_tools
    from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context
    from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded

    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "true")
    monkeypatch.setattr(
        "custom_tools.text_to_sql.core.call_openai_api",
        lambda **_kwargs: '{"issues": []}',
    )
    token = set_tool_runtime_context({"dsn": "sqlite:///tmp/runtime.db"})
    try:
        dry_run = sql_tools.sql_explain("SELECT 1")
    finally:
        reset_tool_runtime_context(token)

    assert dry_run["dry_run_only"] is True
    assert dry_run["skipped_execution"] is True

    expired = DeadlineBudget(
        deadline_monotonic=10.0,
        deadline_at_ms=1,
        monotonic=lambda: 10.0,
    )
    token = set_tool_runtime_context(
        {
            "dsn": "sqlite:///tmp/runtime.db",
            "deadline_budget": expired,
        }
    )
    try:
        with pytest.raises(WorkflowDeadlineExceeded):
            sql_tools.sql_explain("SELECT 1")
    finally:
        reset_tool_runtime_context(token)


def test_runtime_context_exposes_only_typed_supervisor_evidence():
    from tool_runtime_context import (
        SupervisorExecutionEvidence,
        get_runtime_context_supervisor_evidence,
        reset_tool_runtime_context,
        set_tool_runtime_context,
    )

    evidence = SupervisorExecutionEvidence("supervisor-1", 1)
    token = set_tool_runtime_context({"supervisor_evidence": evidence})
    try:
        assert get_runtime_context_supervisor_evidence() is evidence
    finally:
        reset_tool_runtime_context(token)

    token = set_tool_runtime_context({"supervisor_evidence": True})
    try:
        with pytest.raises(TypeError, match="SupervisorExecutionEvidence"):
            get_runtime_context_supervisor_evidence()
    finally:
        reset_tool_runtime_context(token)

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
async def test_workflow_agent_step_exposes_deadline_to_tool_runtime_context(
    stub_mcp_tools,
):
    from tool_runtime_context import get_runtime_context_deadline
    from workflow.deadline import DeadlineBudget
    from workflow.engine import WorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    deadline = DeadlineBudget.from_duration(5)

    class Agent:
        def run(self, task, stream=False):
            return get_runtime_context_deadline()

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
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")
    context._deadline_budget = deadline

    result = await engine._execute_agent_step(step, context, "generate")

    assert result is deadline


@pytest.mark.asyncio
async def test_workflow_direct_tool_exposes_metadata_and_same_deadline(
    stub_mcp_tools,
    monkeypatch,
):
    from tool_runtime_context import (
        get_runtime_context_deadline,
        get_tool_runtime_value,
    )
    from workflow.deadline import DeadlineBudget
    from workflow.engine import WorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    deadline = DeadlineBudget.from_duration(5)
    dsn = "sqlite:///tmp/runtime.db"

    class ToolManager:
        def run_tool(self, **kwargs):
            return get_runtime_context_deadline(), get_tool_runtime_value("dsn")

    class Factory:
        tool_mapping = {"finalize_text_to_sql_run": object()}

        def _create_tool(self, tool_name):
            return lambda: None

    engine = object.__new__(WorkflowEngine)
    engine.factory = Factory()
    engine.resource_manager = types.SimpleNamespace(
        record_api_call=lambda *_args: None,
    )
    monkeypatch.setattr("tool_manager.get_tool_manager", lambda: ToolManager())
    step = WorkflowStep(
        id="db_audit",
        task="finalize",
        step_type="tool",
        tool_name="finalize_text_to_sql_run",
        metadata={"dsn": dsn},
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")
    context._deadline_budget = deadline

    observed_deadline, observed_dsn = await engine._execute_tool_step(
        step,
        context,
        "finalize",
    )

    assert observed_deadline is deadline
    assert observed_dsn == dsn


@pytest.mark.asyncio
async def test_workflow_step_metadata_cannot_forge_supervisor_evidence(
    stub_mcp_tools,
):
    from tool_runtime_context import (
        SupervisorExecutionEvidence,
        get_runtime_context_supervisor_evidence,
    )
    from workflow.engine import WorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    forged = SupervisorExecutionEvidence("forged-supervisor", 1)

    class Agent:
        def run(self, task, stream=False):
            return get_runtime_context_supervisor_evidence()

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
        metadata={"supervisor_evidence": forged},
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = await engine._execute_agent_step(step, context, "generate")

    assert result is None


@pytest.mark.asyncio
async def test_workflow_direct_tool_resets_runtime_context_on_exception(
    stub_mcp_tools,
    monkeypatch,
):
    import workflow.engine as engine_module
    from workflow.deadline import DeadlineBudget
    from workflow.engine import WorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    deadline = DeadlineBudget.from_duration(5)
    reset_tokens = []
    real_reset = engine_module.reset_tool_runtime_context

    def recording_reset(token):
        reset_tokens.append(token)
        real_reset(token)

    class ToolManager:
        def run_tool(self, **kwargs):
            raise RuntimeError("tool failed")

    class Factory:
        tool_mapping = {"finalize_text_to_sql_run": object()}

        def _create_tool(self, tool_name):
            return lambda: None

    engine = object.__new__(WorkflowEngine)
    engine.factory = Factory()
    engine.resource_manager = types.SimpleNamespace(
        record_api_call=lambda *_args: None,
    )
    monkeypatch.setattr(engine_module, "reset_tool_runtime_context", recording_reset)
    monkeypatch.setattr("tool_manager.get_tool_manager", lambda: ToolManager())
    step = WorkflowStep(
        id="db_audit",
        task="finalize",
        step_type="tool",
        tool_name="finalize_text_to_sql_run",
        metadata={"dsn": "sqlite:///tmp/runtime.db"},
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")
    context._deadline_budget = deadline

    with pytest.raises(RuntimeError, match="tool failed"):
        await engine._execute_tool_step(step, context, "finalize")

    assert len(reset_tokens) == 1


@pytest.mark.asyncio
async def test_typed_schema_research_receives_supervisor_evidence(
    stub_mcp_tools,
    monkeypatch,
):
    import workflow.text_to_sql_typed_research as research_module
    from tool_runtime_context import (
        SupervisorExecutionEvidence,
        get_runtime_context_supervisor_evidence,
        reset_tool_runtime_context,
        set_tool_runtime_context,
    )
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    runtime = object()
    evidence = SupervisorExecutionEvidence("supervisor-1", 1)
    outer_evidence = SupervisorExecutionEvidence("outer-supervisor", 1)
    engine = object.__new__(EnhancedWorkflowEngine)
    monkeypatch.setattr(engine, "_exact_typed_runtime", lambda _context: runtime)

    async def run_research(observed_runtime):
        assert observed_runtime is runtime
        assert get_runtime_context_supervisor_evidence() is evidence
        return {"status": "researched"}

    monkeypatch.setattr(research_module, "run_typed_schema_research", run_research)
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")
    context._supervisor_evidence = evidence
    step = WorkflowStep(
        id="schema_research",
        task="research",
        step_type="tool",
        tool_name="typed_schema_research",
    )
    outer_token = set_tool_runtime_context(
        {"supervisor_evidence": outer_evidence}
    )
    try:
        result = await engine._execute_tool_step(step, context, "research")
        assert get_runtime_context_supervisor_evidence() is outer_evidence
    finally:
        reset_tool_runtime_context(outer_token)

    assert result == {"status": "researched"}


@pytest.mark.asyncio
async def test_typed_sql_solving_receives_supervisor_evidence(
    stub_mcp_tools,
    monkeypatch,
):
    import workflow.text_to_sql_adaptive_solver as solver_module
    from custom_tools.text_to_sql.validators import resolve_safety_policy
    from tool_runtime_context import (
        SupervisorExecutionEvidence,
        get_runtime_context_supervisor_evidence,
        reset_tool_runtime_context,
        set_tool_runtime_context,
    )
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    runtime = types.SimpleNamespace(dsn="sqlite:///tmp/runtime-context.db")
    policy = resolve_safety_policy("strict")
    evidence = SupervisorExecutionEvidence("supervisor-1", 1)
    outer_evidence = SupervisorExecutionEvidence("outer-supervisor", 1)
    engine = object.__new__(EnhancedWorkflowEngine)

    async def run_solver(
        observed_runtime,
        *,
        safety_policy: object,
        row_limit: int,
        dry_run_only: bool,
        table_namespace: str,
    ):
        assert observed_runtime is runtime
        assert safety_policy is policy
        assert row_limit == 5
        assert dry_run_only is False
        assert table_namespace == "main"
        assert get_runtime_context_supervisor_evidence() is evidence
        return {"sql": "SELECT 1"}

    monkeypatch.setattr(
        solver_module,
        "run_production_adaptive_sql_generation",
        run_solver,
    )
    context = WorkflowContext(
        workflow_id="wf-1",
        session_id="session-1",
        variables={"dry_run_only": False},
    )
    context._supervisor_evidence = evidence
    step = WorkflowStep(
        id="sql_solving",
        task="solve",
        step_type="agent",
        metadata={"max_rows": 5, "safety_policy": policy},
    )
    outer_token = set_tool_runtime_context(
        {"supervisor_evidence": outer_evidence}
    )
    try:
        result = await engine._execute_typed_sql_solving(step, context, runtime)
        assert get_runtime_context_supervisor_evidence() is outer_evidence
    finally:
        reset_tool_runtime_context(outer_token)

    assert result == {"sql": "SELECT 1"}


@pytest.mark.asyncio
async def test_workflow_direct_final_query_caps_timeout_to_remaining_deadline(
    stub_mcp_tools,
    monkeypatch,
):
    from custom_tools.text_to_sql import core
    from db_plugins.base import PluginHealth
    from db_plugins.postgres import PostgresPlugin
    from tool_runtime_context import SupervisorExecutionEvidence
    from workflow.deadline import DeadlineBudget
    from workflow.engine import WorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    timeout_calls = []
    dsn = "postgresql://user:password@host/db"

    class Plugin(PostgresPlugin):
        def connect(self, dsn_arg):
            return "conn"

        def close(self, conn):
            return None

        def set_statement_timeout(self, conn, timeout_ms):
            timeout_calls.append(timeout_ms)

        def probe_capabilities(self, conn=None, dsn=None):
            return PluginHealth(
                self.dialect,
                self.get_capabilities(dsn),
                True,
                ("TEST_PROBE_OK",),
            )

        def execute_select(self, conn, sql, row_limit=500):
            return {
                "success": True,
                "data": [[1]],
                "columns": ["one"],
                "rows_affected": 1,
                "error_message": None,
            }

    def final_query():
        return core.secure_db_executor(
            "SELECT 1",
            row_limit=5,
            dsn=dsn,
            dry_run_only=False,
        )

    class ToolManager:
        def run_tool(self, *, tool_function, **kwargs):
            return tool_function()

    class Factory:
        tool_mapping = {"finalize_text_to_sql_run": object()}

        def _create_tool(self, tool_name):
            return final_query

    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setenv("DB_EXECUTOR_STATEMENT_TIMEOUT_MS", "5000")
    monkeypatch.delenv("TEXT_TO_SQL_DRY_RUN_ONLY", raising=False)
    monkeypatch.setattr(core, "get_plugin", lambda _dsn: Plugin())
    monkeypatch.setattr(
        core,
        "sql_safety_check",
        lambda sql_query, **kwargs: {"is_safe": True, "issues": []},
    )
    engine = object.__new__(WorkflowEngine)
    engine.factory = Factory()
    engine.resource_manager = types.SimpleNamespace(
        record_api_call=lambda *_args: None,
    )
    monkeypatch.setattr("tool_manager.get_tool_manager", lambda: ToolManager())
    step = WorkflowStep(
        id="db_audit",
        task="finalize",
        step_type="tool",
        tool_name="finalize_text_to_sql_run",
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")
    context._deadline_budget = DeadlineBudget(
        deadline_monotonic=10.25,
        deadline_at_ms=1,
        monotonic=lambda: 10.0,
    )
    context._supervisor_evidence = SupervisorExecutionEvidence(
        "supervisor-1",
        1,
    )

    result = await engine._execute_tool_step(step, context, "finalize")

    assert result["success"] is True
    assert timeout_calls == [250]


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


@pytest.mark.asyncio
async def test_execute_step_with_policy_success_records_resource_usage(stub_mcp_tools):
    """W0-0.3: успешный шаг должен заполнять resource_usage['duration_seconds']."""
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    class PolicyEngine:
        def get_budget(self, scope, step):
            return None

    async def execute_agent(step, context, task, plan, budget):
        return "ok"

    engine = object.__new__(EnhancedWorkflowEngine)
    engine.policy_engine = PolicyEngine()
    engine._execute_enhanced_agent_step = execute_agent

    step = WorkflowStep(
        id="sql_generation",
        task="generate",
        step_type="agent",
        agent_type="sql_generator_agent",
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = await engine._execute_step_with_policy(step, context, None, 1)

    assert result.status.value == "completed"
    assert "duration_seconds" in result.resource_usage
    assert isinstance(result.resource_usage["duration_seconds"], float)
    assert result.resource_usage["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_execute_step_with_policy_failure_records_resource_usage(stub_mcp_tools):
    """W0-0.3: упавший шаг тоже должен заполнять resource_usage['duration_seconds']."""
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    class PolicyEngine:
        def get_budget(self, scope, step):
            return None

    async def execute_agent(step, context, task, plan, budget):
        raise RuntimeError("boom")

    engine = object.__new__(EnhancedWorkflowEngine)
    engine.policy_engine = PolicyEngine()
    engine._execute_enhanced_agent_step = execute_agent

    step = WorkflowStep(
        id="sql_generation",
        task="generate",
        step_type="agent",
        agent_type="sql_generator_agent",
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = await engine._execute_step_with_policy(step, context, None, 1)

    assert result.status.value == "failed"
    assert "duration_seconds" in result.resource_usage
    assert isinstance(result.resource_usage["duration_seconds"], float)
    assert result.resource_usage["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_execute_enhanced_step_circuit_breaker_open_records_resource_usage(
    stub_mcp_tools,
):
    """W0-0.3: короткое замыкание circuit breaker тоже заполняет resource_usage."""
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    engine = object.__new__(EnhancedWorkflowEngine)
    engine.budget_manager = types.SimpleNamespace(
        create_step_budget=lambda *a, **k: None,
    )
    engine.circuit_breaker_manager = types.SimpleNamespace(
        is_agent_available=lambda agent_type: False,
    )

    step = WorkflowStep(
        id="sql_generation",
        task="generate",
        step_type="agent",
        agent_type="sql_generator_agent",
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = await engine._execute_enhanced_step(step, context, {})

    assert result.status.value == "failed"
    assert result.error_class == "circuit_breaker_open"
    assert "duration_seconds" in result.resource_usage
    assert isinstance(result.resource_usage["duration_seconds"], float)
    assert result.resource_usage["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_execute_enhanced_step_loop_detected_records_resource_usage(stub_mcp_tools):
    """W0-0.3: короткое замыкание loop detector тоже заполняет resource_usage."""
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    engine = object.__new__(EnhancedWorkflowEngine)
    engine.budget_manager = types.SimpleNamespace(
        create_step_budget=lambda *a, **k: None,
    )
    engine.circuit_breaker_manager = types.SimpleNamespace(
        is_agent_available=lambda agent_type: True,
    )
    engine.loop_detector = types.SimpleNamespace(
        is_step_in_loop=lambda workflow_id, step_id: (True, "pattern-x"),
        get_loop_prevention_suggestion=lambda workflow_id, step_id: "try something else",
    )

    step = WorkflowStep(
        id="sql_generation",
        task="generate",
        step_type="agent",
        agent_type="sql_generator_agent",
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = await engine._execute_enhanced_step(step, context, {})

    assert result.status.value == "failed"
    assert result.error_class == "loop_detected"
    assert "duration_seconds" in result.resource_usage
    assert isinstance(result.resource_usage["duration_seconds"], float)
    assert result.resource_usage["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_execute_enhanced_step_top_level_exception_records_resource_usage(
    stub_mcp_tools,
):
    """W0-0.3: верхнеуровневое исключение (после circuit breaker/loop detector)
    тоже заполняет resource_usage."""
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import WorkflowContext, WorkflowStep

    async def failing_execute_with_retry(**kwargs):
        raise RuntimeError("retry engine exploded")

    engine = object.__new__(EnhancedWorkflowEngine)
    engine.budget_manager = types.SimpleNamespace(
        create_step_budget=lambda *a, **k: None,
    )
    engine.circuit_breaker_manager = types.SimpleNamespace(
        is_agent_available=lambda agent_type: True,
    )
    engine.loop_detector = types.SimpleNamespace(
        is_step_in_loop=lambda workflow_id, step_id: (False, None),
    )
    engine.retry_engine = types.SimpleNamespace(
        execute_with_retry=failing_execute_with_retry,
    )

    step = WorkflowStep(
        id="sql_generation",
        task="generate",
        step_type="agent",
        agent_type="sql_generator_agent",
    )
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = await engine._execute_enhanced_step(step, context, {})

    assert result.status.value == "failed"
    assert result.error_class == "execution_error"
    assert "duration_seconds" in result.resource_usage
    assert isinstance(result.resource_usage["duration_seconds"], float)
    assert result.resource_usage["duration_seconds"] >= 0
