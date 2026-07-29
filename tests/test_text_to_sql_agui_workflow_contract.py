from pathlib import Path
import asyncio
import base64
import concurrent.futures
import contextlib
import gzip
import importlib
import importlib.util
import inspect
import json
import logging
import multiprocessing
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import types
from typing import Any

import pytest

from workflow.result_identity import (
    parse_workflow_result_event_key,
    workflow_result_event_key,
)

_LIGHT_WORKFLOW_MODULES = [
    "workflow",
    "workflow.engine",
    "workflow.enhanced_engine",
    "workflow.models",
    "workflow.state_manager",
    "workflow.retry_engine",
    "workflow.resource_manager",
    "workflow.result_outbox",
    "workflow.result_delivery",
    "workflow.streamlit_api",
    "agent_system",
]
_MISSING_MODULE = object()


@pytest.fixture(autouse=True)
def _restore_light_workflow_modules():
    saved = {name: sys.modules.get(name, _MISSING_MODULE) for name in _LIGHT_WORKFLOW_MODULES}
    yield
    for name, module in saved.items():
        if module is _MISSING_MODULE:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _load_service_with_stubs(monkeypatch, wf_manager):
    monkeypatch.setenv("AG_UI_AUTH_MODE", "disabled")
    for module_name in [
        "backend.fastapi_app.agui.service",
        "agent_streamlit_api",
        "configuration_api",
        "db_plugins",
        "db_plugins.streamlit_api",
        "memory",
        "memory.streamlit_api",
        "telemetry",
        "tool_manager",
        "unified_logging",
        "workflow",
        "workflow.streamlit_api",
        "utils",
    ]:
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    agent_module = types.ModuleType("agent_streamlit_api")
    agent_module.AgentManager = object
    agent_module.DynamicAgentDefinition = object
    monkeypatch.setitem(sys.modules, "agent_streamlit_api", agent_module)

    config_module = types.ModuleType("configuration_api")
    for name in [
        "ConfigurationManager",
        "LLMConfig",
        "LoggingConfig",
        "MemoryConfig",
        "NetworkConfig",
        "PerformanceConfig",
        "ResourceLimits",
        "SecurityConfig",
        "SystemConfig",
        "SystemConfiguration",
        "TelemetryConfig",
        "UIConfig",
    ]:
        setattr(config_module, name, object)
    monkeypatch.setitem(sys.modules, "configuration_api", config_module)

    db_pkg = types.ModuleType("db_plugins")
    db_streamlit = types.ModuleType("db_plugins.streamlit_api")
    db_streamlit.get_db_plugin_manager = lambda: object()
    monkeypatch.setitem(sys.modules, "db_plugins", db_pkg)
    monkeypatch.setitem(sys.modules, "db_plugins.streamlit_api", db_streamlit)

    memory_pkg = types.ModuleType("memory")
    memory_streamlit = types.ModuleType("memory.streamlit_api")
    memory_streamlit.get_memory_rag_manager = lambda: object()
    monkeypatch.setitem(sys.modules, "memory", memory_pkg)
    monkeypatch.setitem(sys.modules, "memory.streamlit_api", memory_streamlit)

    telemetry_module = types.ModuleType("telemetry")
    telemetry_module.get_telemetry_manager = lambda *args, **kwargs: object()
    monkeypatch.setitem(sys.modules, "telemetry", telemetry_module)

    tool_manager_module = types.ModuleType("tool_manager")
    tool_manager_module.get_tool_manager = lambda: object()
    monkeypatch.setitem(sys.modules, "tool_manager", tool_manager_module)

    logging_module = types.ModuleType("unified_logging")
    logging_module.get_logging_manager = lambda: object()
    monkeypatch.setitem(sys.modules, "unified_logging", logging_module)

    workflow_pkg = types.ModuleType("workflow")
    workflow_pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "workflow")]
    workflow_streamlit = types.ModuleType("workflow.streamlit_api")
    workflow_streamlit.WorkflowManager = lambda: wf_manager

    class WorkflowOwner:
        def __init__(self, subject, tenant_id, roles):
            self.subject = subject
            self.tenant_id = tenant_id
            self.roles = roles

        @property
        def quota_identity(self):
            return f"owner:{self.tenant_id}:{self.subject}"

    workflow_streamlit.WorkflowOwner = WorkflowOwner
    workflow_streamlit.WorkflowRunAlreadyReservedError = type(
        "WorkflowRunAlreadyReservedError",
        (ValueError,),
        {},
    )
    monkeypatch.setitem(sys.modules, "workflow", workflow_pkg)
    monkeypatch.setitem(sys.modules, "workflow.streamlit_api", workflow_streamlit)

    utils_module = types.ModuleType("utils")
    utils_module.call_openai_api_streaming = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "utils", utils_module)

    import backend.fastapi_app.agui as agui_pkg

    monkeypatch.delattr(agui_pkg, "service", raising=False)
    service = importlib.import_module("backend.fastapi_app.agui.service")
    event_store_module = importlib.import_module("backend.fastapi_app.agui.store")
    service_test_dir = Path(tempfile.mkdtemp(prefix="agui-service-test-"))
    service._AGUI_EVENT_STORE = event_store_module.EventStore(
        str(service_test_dir / "events.db")
    )
    monkeypatch.setattr(
        service,
        "_workflow_result_outbox_path",
        lambda: service_test_dir / "workflow_result_outbox.db",
    )

    monkeypatch.setattr(service, "_agent_manager", lambda: object())
    monkeypatch.setattr(service, "_wf_manager", lambda: wf_manager)
    monkeypatch.setattr(service, "_memory_manager", lambda: object())
    monkeypatch.setattr(service, "_db_manager", lambda: object())
    monkeypatch.setattr(service, "_config_manager", lambda: object())
    monkeypatch.setattr(service, "_telemetry_manager", lambda: object())
    monkeypatch.setattr(service, "_logging_manager", lambda: object())
    monkeypatch.setattr(service, "_tool_manager", lambda: object())
    from backend.fastapi_app.agui.connection_registry import ConnectionTargetPolicy

    service._CONNECTION_TARGET_POLICY = ConnectionTargetPolicy(
        allowed_schemes={
            "duckdb",
            "impala",
            "mysql",
            "pg",
            "postgres",
            "postgresql",
            "psql",
            "sapiq",
            "sqlite",
        },
        allowed_network_targets={
            "db.example:5432",
            "db.internal:5432",
            "example.com:5432",
            "host:5432",
            "srv:5432",
        },
        allowed_file_roots={Path(tempfile.gettempdir())},
        path_resolver=lambda path: path.resolve(strict=False),
    )
    service._CONNECTION_REGISTRY = None
    return service


def _register_legacy_admin_run(service, run_id):
    """Create the durable row expected for an admin-only legacy projection."""
    from backend.fastapi_app.agui.auth import Principal

    admin = Principal(
        subject="legacy-admin",
        tenant_id="legacy-tenant",
        roles=frozenset({"admin", "user"}),
    )
    store = service._AGUI_EVENT_STORE
    store.create_run(
        run_id,
        f"thread-{run_id}",
        admin,
        run_kind="legacy",
    )
    # ``legacy`` is a migration-only lifecycle value, so the public creation
    # boundary intentionally cannot produce it. These tests exercise reads of
    # rows migrated from the pre-lifecycle schema.
    with store._lock:
        store._conn.execute(
            "UPDATE agui_runs SET status = 'legacy' WHERE run_id = ?",
            (run_id,),
        )
        store._conn.commit()
    return admin


class _WorkflowManagerStub:
    def __init__(self):
        self.calls = []
        self.active_runs = {}

    def start_workflow(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs["run_id"]

    def get_active_run_snapshot(self, run_id):
        run_data = self.active_runs.get(run_id)
        return dict(run_data) if isinstance(run_data, dict) else {}

    def update_active_run(self, run_id, updates):
        run_data = self.active_runs.get(run_id)
        if not isinstance(run_data, dict):
            return False
        run_data.update(updates)
        return True


class _StepResultStub:
    def __init__(self, output):
        self.output = output


def _terminal_contract_payload(status="succeeded", run_id="run-terminal"):
    succeeded = status == "succeeded"
    execution_failed = status == "failed"
    reason_code = {
        "succeeded": "",
        "abstained": "VERIFIER_REJECTED",
        "failed": "EXECUTION_FAILED",
        "cancelled": "CANCELLED",
        "timed_out": "TIMED_OUT",
    }[status]
    execution = {}
    audit = {}
    if succeeded:
        execution = {
            "success": True,
            "sql_query": "SELECT 1",
            "data": [[1]],
            "columns": ["value"],
            "rows_affected": 1,
            "execution_time_ms": 1,
            "dry_run_only": False,
            "skipped_execution": False,
            "applied_row_limit": 100,
        }
        audit = {"status": "logged", "log_id": "audit-1"}
    elif execution_failed:
        execution = {
            "success": False,
            "sql_query": "SELECT 1",
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": 1,
            "error_message": "execution failed",
            "dry_run_only": False,
            "skipped_execution": False,
            "applied_row_limit": 100,
        }
        audit = {"status": "logged", "log_id": "audit-1"}
    return {
        "run_id": run_id,
        "status": status,
        "reason_code": reason_code,
        "sql": "SELECT 1",
        "generated": True,
        "approved": succeeded or execution_failed,
        "executed": succeeded or execution_failed,
        "dry_run": False,
        "audited": succeeded or execution_failed,
        "data": [[1]] if succeeded else [],
        "columns": ["value"] if succeeded else [],
        "rows_affected": 1 if succeeded else 0,
        "error": None if succeeded else status,
        "execution": execution,
        "audit": audit,
        "persistence": {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
        } if succeeded else {"status": "not_attempted"},
    }


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec for {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _install_light_workflow_package():
    root = Path(__file__).resolve().parents[1]
    for module_name in _LIGHT_WORKFLOW_MODULES:
        sys.modules.pop(module_name, None)

    workflow_pkg = types.ModuleType("workflow")
    workflow_pkg.__path__ = [str(root / "workflow")]
    workflow_pkg.__lightweight__ = True
    sys.modules["workflow"] = workflow_pkg

    agent_system = types.ModuleType("agent_system")

    class DynamicAgentSystem:
        pass

    agent_system.DynamicAgentSystem = DynamicAgentSystem
    sys.modules["agent_system"] = agent_system

    for module_name in [
        "workflow.models",
        "workflow.state_manager",
        "workflow.retry_engine",
        "workflow.resource_manager",
    ]:
        relative_path = module_name.split(".", 1)[1].replace(".", "/") + ".py"
        module = _load_module(module_name, root / "workflow" / relative_path)
        setattr(workflow_pkg, module_name.rsplit(".", 1)[1], module)

    engine_module = _load_module("workflow.engine", root / "workflow" / "engine.py")
    workflow_pkg.engine = engine_module
    return workflow_pkg


def _load_light_workflow_engine():
    workflow_pkg = _install_light_workflow_package()
    return workflow_pkg.engine


def _load_light_workflow_streamlit_api():
    root = Path(__file__).resolve().parents[1]
    workflow_pkg = _install_light_workflow_package()

    enhanced_engine = types.ModuleType("workflow.enhanced_engine")

    class EnhancedWorkflowEngine(workflow_pkg.engine.WorkflowEngine):
        pass

    enhanced_engine.EnhancedWorkflowEngine = EnhancedWorkflowEngine
    sys.modules["workflow.enhanced_engine"] = enhanced_engine
    workflow_pkg.enhanced_engine = enhanced_engine

    streamlit_api = _load_module("workflow.streamlit_api", root / "workflow" / "streamlit_api.py")
    workflow_pkg.streamlit_api = streamlit_api
    storage_dir = Path(tempfile.mkdtemp(prefix="workflow-streamlit-api-test-"))
    streamlit_api._agui_event_store_path = lambda: storage_dir / "events.db"
    if hasattr(streamlit_api, "_workflow_result_outbox_path"):
        streamlit_api._test_default_workflow_result_outbox_path = (
            streamlit_api._workflow_result_outbox_path
        )
        streamlit_api._workflow_result_outbox_path = (
            lambda: storage_dir / "result-outbox.db"
        )
    return streamlit_api


class _QueuedWorkflowSupervisor:
    def __init__(self, streamlit_api):
        self.streamlit_api = streamlit_api
        self.submissions = []

    def submit(self, run_id, work_spec, *, deadline_at_ms):
        from backend.fastapi_app.agui.store import EventStore

        store = EventStore(str(self.streamlit_api._agui_event_store_path()))
        try:
            store.enqueue_run(run_id, work_spec, deadline_at_ms, 100)
        finally:
            store.close()
        self.submissions.append((run_id, work_spec, deadline_at_ms))
        return types.SimpleNamespace(
            run_id=run_id,
            state="queued",
            deadline_at_ms=deadline_at_ms,
        )

    def cancel(self, run_id):
        return types.SimpleNamespace(
            run_id=run_id,
            accepted=True,
            state="cancelled",
            local=False,
        )


def test_text_to_sql_generate_requires_payload_dsn(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setenv("DB_DSN", "sqlite:///unexpected.db")

    with pytest.raises(ValueError, match="dsn is required"):
        service.handle_service_action("presets.text_to_sql.generate", {"query": "show users"})

    assert wf_manager.calls == []


def test_text_to_sql_generate_uses_unique_run_id_and_records_parameters(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setenv("DB_DSN", "sqlite:///must-not-be-used.db")

    first = service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show users",
            "dsn": "sqlite:///tmp/app.db",
            "admin_raw_dsn_compat": True,
            "max_rows": 7,
            "dry_run_only": True,
            "validate_schema": False,
        },
    )
    second = service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show users",
            "dsn": "sqlite:///tmp/app.db",
            "admin_raw_dsn_compat": True,
            "max_rows": 7,
            "dry_run_only": True,
            "validate_schema": False,
        },
    )

    assert first["run_id"] != second["run_id"]
    assert first["session_id"] == second["session_id"]
    assert first["run_id"] != first["session_id"]
    assert len(wf_manager.calls) == 2
    call = wf_manager.calls[0]
    assert call["run_id"] == first["run_id"]
    assert call["session_id"] == first["session_id"]
    assert call["parameters"]["dsn"] == "sqlite:///tmp/app.db"
    assert call["parameters"]["max_rows"] == 7
    assert call["parameters"]["safety_level"] == "strict"
    assert call["parameters"]["include_explanation"] is True
    assert call["parameters"]["dry_run_only"] is True
    assert call["parameters"]["validate_schema"] is False
    assert call["parameters"]["use_schema_suggestions"] is True
    assert call["parameters"]["allow_enhanced_fallback"] is False
    assert call["parameters"]["run_id"] == first["run_id"]
    assert call["parameters"]["session_id"] == first["session_id"]


def test_text_to_sql_session_id_is_scoped_by_principal(monkeypatch):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    dsn = "sqlite:///tmp/app.db"
    alice = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))
    bob = Principal(subject="bob", tenant_id="tenant-1", roles=frozenset({"user"}))

    base = service._compute_text_to_sql_session_id(dsn)
    alice_session = service._compute_text_to_sql_session_id(dsn, principal=alice)
    bob_session = service._compute_text_to_sql_session_id(dsn, principal=bob)

    assert alice_session == service._compute_text_to_sql_session_id(dsn, principal=alice)
    assert alice_session != bob_session
    assert alice_session != base
    assert bob_session != base
    assert alice_session.startswith(f"{base}__u_")
    assert service._scope_text_to_sql_session_id(alice_session, alice) == alice_session
    with pytest.raises(PermissionError, match="session_id scope"):
        service._scope_text_to_sql_session_id(alice_session, bob)


def test_text_to_sql_generate_scopes_explicit_session_by_principal(monkeypatch, tmp_path):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    dsn = "sqlite:///tmp/app.db"
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        service,
        "_workflow_agui_entrypoint",
        lambda _name: "presets.text_to_sql.generate",
    )
    admin = Principal(
        subject="admin",
        tenant_id="tenant-1",
        roles=frozenset({"admin", "user"}),
    )
    alice = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))
    bob = Principal(subject="bob", tenant_id="tenant-1", roles=frozenset({"user"}))
    connection_ref = service.handle_service_action(
        "db.connections.register",
        {
            "display_name": "Tenant warehouse",
            "dsn": dsn,
            "owner_subject": None,
            "tenant_id": "tenant-1",
        },
        principal=admin,
    )["connection"]["connection_ref"]

    alice_result = service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show users",
            "connection_ref": connection_ref,
            "session_id": "client-session",
        },
        principal=alice,
    )
    bob_result = service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show users",
            "connection_ref": connection_ref,
            "session_id": "client-session",
        },
        principal=bob,
    )
    alice_reuse = service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show users",
            "connection_ref": connection_ref,
            "session_id": alice_result["session_id"],
        },
        principal=alice,
    )

    assert alice_result["session_id"] != bob_result["session_id"]
    assert alice_reuse["session_id"] == alice_result["session_id"]
    assert wf_manager.calls[0]["parameters"]["session_id"] == alice_result["session_id"]
    assert wf_manager.calls[1]["parameters"]["session_id"] == bob_result["session_id"]
    assert wf_manager.calls[2]["parameters"]["session_id"] == alice_result["session_id"]
    with pytest.raises(PermissionError, match="session_id scope"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {
                "query": "show users",
                "connection_ref": connection_ref,
                "session_id": alice_result["session_id"],
            },
            principal=bob,
        )


def test_text_to_sql_generate_rejects_admin_saved_db_config_for_plain_user(monkeypatch, tmp_path):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        service,
        "_workflow_agui_entrypoint",
        lambda _name: "presets.text_to_sql.generate",
    )
    admin = Principal(
        subject="admin",
        tenant_id="tenant-1",
        roles=frozenset({"admin", "user"}),
    )
    user = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))
    saved = service.handle_service_action(
        "db.test_configs.save",
        {"name": "prod", "dsn": "postgresql://alice:secret@example.com:5432/app"},
        principal=admin,
    )
    ref = saved["configs"][0]["connection_ref"]

    with pytest.raises(PermissionError, match="admin"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {"query": "show users", "connection_ref": ref},
            principal=user,
        )

    assert wf_manager.calls == []


def test_text_to_sql_generate_rejects_foreign_agui_entrypoint(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(
        service,
        "_workflow_agui_entrypoint",
        lambda _workflow_name: "other.service.action",
    )

    with pytest.raises(service.ForbiddenWorkflowNameError) as ei:
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {
                "query": "show users",
                "dsn": "sqlite:///tmp/app.db",
                "admin_raw_dsn_compat": True,
                "workflow_name": "text_to_sql_pipeline",
            },
        )

    assert "other.service.action" in str(ei.value)
    assert wf_manager.calls == []


def test_text_to_sql_generate_rejects_workflow_without_text_to_sql_entrypoint(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_workflow_agui_entrypoint", lambda _workflow_name: None)

    with pytest.raises(service.ForbiddenWorkflowNameError) as ei:
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {
                "query": "show users",
                "dsn": "sqlite:///tmp/app.db",
                "admin_raw_dsn_compat": True,
                "workflow_name": "text_to_sql_pipeline",
            },
        )

    assert "workflows.start" in str(ei.value)
    assert wf_manager.calls == []


def test_text_to_sql_generate_validates_runtime_limits(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(ValueError, match="max_rows"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {"query": "show users", "dsn": "sqlite:///tmp/app.db", "max_rows": 0},
        )

    for max_rows in [True, 1.9, "1.9", "  "]:
        with pytest.raises(ValueError, match="max_rows"):
            service.handle_service_action(
                "presets.text_to_sql.generate",
                {"query": "show users", "dsn": "sqlite:///tmp/app.db", "max_rows": max_rows},
            )

    with pytest.raises(ValueError, match="safety_level"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {"query": "show users", "dsn": "sqlite:///tmp/app.db", "safety_level": "moderate"},
        )

    with pytest.raises(ValueError, match="allow_enhanced_fallback"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {
                "query": "show users",
                "dsn": "sqlite:///tmp/app.db",
                "allow_enhanced_fallback": "maybe",
            },
        )

    assert wf_manager.calls == []


@pytest.mark.parametrize("validate_schema", [True, False])
def test_text_to_sql_generate_rejects_schema_suggestions_disabled(
    monkeypatch,
    validate_schema,
):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(ValueError, match="schema grounding is required"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {
                "query": "show users",
                "dsn": "sqlite:///tmp/app.db",
                "use_schema_suggestions": False,
                "validate_schema": validate_schema,
            },
        )

    assert wf_manager.calls == []


# ---------------------------------------------------------------------------
# EPIC 7.23: Pydantic TextToSqlGenerateRequest — расширенный контракт
# ---------------------------------------------------------------------------
def test_text_to_sql_generate_missing_query_raises(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(ValueError, match="query is required"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {"dsn": "sqlite:///tmp/app.db"},
        )
    with pytest.raises(ValueError, match="query is required"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {"query": "   ", "dsn": "sqlite:///tmp/app.db"},
        )
    assert wf_manager.calls == []


def test_text_to_sql_generate_pydantic_defaults_match_legacy(monkeypatch):
    """Контракт: Pydantic-модель не должна менять defaults для существующих payload."""
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    result = service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show users",
            "dsn": "sqlite:///tmp/app.db",
            "admin_raw_dsn_compat": True,
        },
    )
    assert len(wf_manager.calls) == 1
    call = wf_manager.calls[0]
    params = call["parameters"]
    # Все defaults совпадают с задокументированными в AG_UI_SERVICE_ACTIONS.md
    assert params["max_rows"] == 100
    assert params["safety_level"] == "strict"
    assert params["include_explanation"] is True
    assert params["validate_schema"] is True
    assert params["dry_run_only"] is False
    assert params["use_schema_suggestions"] is True
    assert params["allow_enhanced_fallback"] is False
    assert call["use_enhanced"] is True
    assert call["enable_telemetry"] is False
    assert call["workflow_name"] == "text_to_sql_pipeline"
    assert result["workflow_name"] == "text_to_sql_pipeline"


def test_text_to_sql_generate_pydantic_strict_bool_for_allow_enhanced_fallback(monkeypatch):
    """7.23: allow_enhanced_fallback идёт через coerce_strict_bool — отвергает мусор,
    принимает canonical значения."""
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    # Canonical TRUE → bool True
    service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show users",
            "dsn": "sqlite:///tmp/app.db",
            "admin_raw_dsn_compat": True,
            "allow_enhanced_fallback": "yes",
        },
    )
    assert wf_manager.calls[-1]["parameters"]["allow_enhanced_fallback"] is True

    # Невалидное → ValueError
    for bad in ["maybe", "2", 2, 1.5]:
        with pytest.raises(ValueError, match="allow_enhanced_fallback"):
            service.handle_service_action(
                "presets.text_to_sql.generate",
                {
                    "query": "show users",
                    "dsn": "sqlite:///tmp/app.db",
                    "allow_enhanced_fallback": bad,
                },
            )


def test_text_to_sql_generate_pydantic_natural_query_takes_priority(monkeypatch):
    """Backwards-compat: natural_query имеет приоритет над query (контракт _extract_query)."""
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "fallback",
            "natural_query": "primary",
            "dsn": "sqlite:///tmp/app.db",
            "admin_raw_dsn_compat": True,
        },
    )
    assert wf_manager.calls[-1]["parameters"]["query"] == "primary"


def test_text_to_sql_generate_pydantic_unknown_fields_ignored(monkeypatch):
    """7.23: extra='ignore' — payload может содержать лишние поля, они отбрасываются,
    а не падают."""
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    result = service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show users",
            "dsn": "sqlite:///tmp/app.db",
            "admin_raw_dsn_compat": True,
            "future_field": "ignored",
            "another_extra": 42,
        },
    )
    assert "run_id" in result


def test_db_test_configs_save_list_resolve_and_delete(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)

    dsn = "postgresql://alice:secret@example.com:5432/app"
    saved = service.handle_service_action(
        "db.test_configs.save",
        {"name": "prod", "dsn": dsn, "description": "Production"},
    )

    saved_config = saved["configs"][0]
    assert saved_config["name"] == "prod"
    assert saved_config["dsn"] != dsn
    assert "secret" not in saved_config["dsn"]
    assert saved_config["connection_ref"] == "db_config:prod"
    assert service._resolve_dsn_reference(saved_config["connection_ref"]) == dsn

    literal_stars_dsn = "postgresql://alice:a***b@example.com:5432/app"
    stars_saved = service.handle_service_action(
        "db.test_configs.save",
        {"name": "stars", "dsn": literal_stars_dsn},
    )
    stars_config = next(config for config in stars_saved["configs"] if config["name"] == "stars")
    assert service._resolve_dsn_reference(stars_config["connection_ref"]) == literal_stars_dsn

    listed = service.handle_service_action("db.test_configs.list", {})
    assert {config["name"] for config in listed["configs"]} == {"prod", "stars"}

    deleted = service.handle_service_action("db.test_configs.delete", {"name": "prod"})
    assert deleted["deleted"] is True
    assert [config["name"] for config in deleted["configs"]] == ["stars"]
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference(saved_config["connection_ref"])


def test_db_test_configs_parallel_saves_preserve_all_entries(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)

    def save_one(index: int):
        return service.handle_service_action(
            "db.test_configs.save",
            {
                "name": f"prod-{index}",
                "dsn": f"sqlite:///tmp/app-{index}.db",
            },
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save_one, range(20)))

    listed = service.handle_service_action("db.test_configs.list", {})
    names = {item["name"] for item in listed["configs"]}
    assert names == {f"prod-{index}" for index in range(20)}


def test_db_test_configs_migrates_legacy_raw_dsn(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    dsn = "postgresql://alice:secret@example.com:5432/app"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"legacy": {"dsn": dsn, "description": "Legacy"}}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    assert listed["configs"][0]["dsn"] != dsn
    assert service._resolve_dsn_reference("db_config:legacy") == dsn
    public_text = (logs_dir / "db_test_configs.json").read_text(encoding="utf-8")
    assert dsn not in public_text
    assert "secret" not in public_text


def test_db_test_configs_preserves_masked_semicolon_query_dsn(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    masked_dsn = "postgresql://srv:5432/db?password=***;sslmode=require"
    raw_dsn = "postgresql://srv:5432/db?password=top;secret;sslmode=require"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({
            "masked": {"dsn": masked_dsn, "description": "Masked"},
            "raw": {"dsn": raw_dsn, "description": "Raw"},
        }),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    configs = {item["name"]: item for item in listed["configs"]}
    assert configs["masked"]["dsn"] == masked_dsn
    assert service._resolve_dsn_reference("db_config:raw") == raw_dsn
    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert "masked" not in secrets
    assert secrets["raw"] == raw_dsn


def test_db_test_configs_preserves_urlencoded_masked_query_dsn(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    masked_dsn = "mssql+pyodbc://srv/db?password=%2A%2A%2A&driver=ODBC+Driver+17"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"masked": {"dsn": masked_dsn, "description": "Masked"}}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    config = listed["configs"][0]
    assert config["dsn"] == "mssql+pyodbc://srv/db?password=***&driver=ODBC+Driver+17"
    assert not (logs_dir / "db_test_config_secrets.json").exists()
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:masked")


def test_db_test_configs_preserves_encoded_odbc_connect_secret_after_migration(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    raw_dsn = (
        "postgresql://srv:5432/orders?"
        "odbc_connect=Driver%3D%7BODBC+Driver+17%7D%3B"
        "Server%3Ddb1.example.com%3BDatabase%3Dorders%3BUID%3Dalice%3BPWD%3Dtopsecret"
        "&sslmode=require"
    )
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"odbc": {"dsn": raw_dsn, "description": "ODBC"}}),
        encoding="utf-8",
    )

    first_list = service.handle_service_action("db.test_configs.list", {})

    public_config = first_list["configs"][0]
    assert "odbc_connect=***" in public_config["dsn"]
    assert "alice" not in public_config["dsn"]
    assert "topsecret" not in public_config["dsn"]
    secrets_path = logs_dir / "db_test_config_secrets.json"
    assert json.loads(secrets_path.read_text(encoding="utf-8"))["odbc"] == raw_dsn

    second_list = service.handle_service_action("db.test_configs.list", {})

    second_public_config = second_list["configs"][0]
    assert "odbc_connect=***" in second_public_config["dsn"]
    assert json.loads(secrets_path.read_text(encoding="utf-8"))["odbc"] == raw_dsn
    assert service._resolve_dsn_reference("db_config:odbc") == raw_dsn


def test_db_test_configs_preserves_masked_key_value_dsns(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    libpq_dsn = "host=db.example.com password=*** user=*** dbname=app"
    odbc_dsn = "Driver={ODBC Driver 17};Server=db.example.com;Pwd=***;UID=***"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({
            "libpq": {"dsn": libpq_dsn, "description": "Masked libpq"},
            "odbc": {"dsn": odbc_dsn, "description": "Masked ODBC"},
        }),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    configs = {item["name"]: item for item in listed["configs"]}
    assert configs["libpq"]["dsn"] == "<redacted>"
    assert configs["odbc"]["dsn"] == "<redacted>"
    public_configs = json.loads((logs_dir / "db_test_configs.json").read_text(encoding="utf-8"))
    assert public_configs["libpq"]["dsn"] == libpq_dsn
    assert public_configs["odbc"]["dsn"] == odbc_dsn
    assert not (logs_dir / "db_test_config_secrets.json").exists()
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:libpq")
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:odbc")


def test_db_test_configs_migrates_mixed_masked_and_raw_query_secrets(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    mixed_dsn = "mssql+pyodbc://srv/db?password=***;token=rawsecret;driver=ODBC+Driver+17"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"mixed": {"dsn": mixed_dsn, "description": "Mixed"}}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    config = listed["configs"][0]
    assert config["dsn"] != mixed_dsn
    assert "rawsecret" not in config["dsn"]
    assert not (logs_dir / "db_test_config_secrets.json").exists()
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:mixed")


def test_db_test_configs_migrates_raw_userinfo_with_masked_query_secret(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    mixed_dsn = "postgresql://alice:secret@example.com/db?api_key=***"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"mixed": {"dsn": mixed_dsn, "description": "Mixed"}}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    config = listed["configs"][0]
    assert config["dsn"] != mixed_dsn
    assert "alice:secret" not in config["dsn"]
    assert not (logs_dir / "db_test_config_secrets.json").exists()
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:mixed")
    public_text = (logs_dir / "db_test_configs.json").read_text(encoding="utf-8")
    assert "alice:secret" not in public_text


def test_db_test_configs_normalizes_legacy_masked_userinfo_without_secret_persist(
    monkeypatch,
    tmp_path,
):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    masked_dsn = "postgresql://alice:***@example.com/db?api_key=***"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"legacy": {"dsn": masked_dsn, "description": "Legacy masked"}}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    config = listed["configs"][0]
    assert config["dsn"] == "postgresql://***:***@example.com/db?api_key=***"
    assert not (logs_dir / "db_test_config_secrets.json").exists()
    public_text = (logs_dir / "db_test_configs.json").read_text(encoding="utf-8")
    assert "alice" not in public_text
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:legacy")


def test_db_test_configs_normalizes_legacy_masked_public_dsn_without_dropping_valid_secret(
    monkeypatch,
    tmp_path,
):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    raw_dsn = "postgresql://alice:secret@example.com:5432/db?api_key=raw-key"
    legacy_masked_dsn = "postgresql://alice:***@example.com:5432/db?api_key=***"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({
            "prod": {
                "dsn": legacy_masked_dsn,
                "dsn_fingerprint": service._dsn_fingerprint(raw_dsn),
                "description": "Legacy masked",
            }
        }),
        encoding="utf-8",
    )
    (logs_dir / "db_test_config_secrets.json").write_text(
        json.dumps({"prod": raw_dsn}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    assert listed["configs"][0]["dsn"] == "postgresql://***:***@example.com:5432/db?api_key=***"
    assert service._resolve_dsn_reference("db_config:prod") == raw_dsn
    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert secrets["prod"] == raw_dsn
    public_text = (logs_dir / "db_test_configs.json").read_text(encoding="utf-8")
    assert "alice" not in public_text
    assert "secret" not in public_text
    assert "raw-key" not in public_text
    public_config = json.loads(public_text)["prod"]
    assert public_config["dsn_fingerprint"] == service._dsn_fingerprint(raw_dsn)


def test_db_test_configs_normalizes_partially_masked_key_value_without_secret_persist(
    monkeypatch,
    tmp_path,
):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    masked_dsn = "Driver={ODBC Driver 17};Server=db;UID=alice;PWD=***"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"legacy": {"dsn": masked_dsn, "description": "Legacy masked"}}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    assert listed["configs"][0]["dsn"] == "<redacted>"
    assert not (logs_dir / "db_test_config_secrets.json").exists()
    public_text = (logs_dir / "db_test_configs.json").read_text(encoding="utf-8")
    assert "alice" not in public_text
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:legacy")


def test_db_test_configs_partial_public_config_drops_stale_secret(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stale_secret = "postgresql://alice:oldsecret@example.com/db"
    partial_dsn = "postgresql://host/db?user=alice&password=***"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"prod": {"dsn": partial_dsn, "description": "Partial"}}),
        encoding="utf-8",
    )
    (logs_dir / "db_test_config_secrets.json").write_text(
        json.dumps({"prod": stale_secret}),
        encoding="utf-8",
    )

    service.handle_service_action("db.test_configs.list", {})

    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert "prod" not in secrets
    public_text = (logs_dir / "db_test_configs.json").read_text(encoding="utf-8")
    assert "alice" not in public_text
    assert "oldsecret" not in public_text
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:prod")
    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert "prod" not in secrets


def test_db_test_configs_missing_public_config_rejects_stale_secret(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stale_secret = "postgresql://alice:oldsecret@example.com/db"
    (logs_dir / "db_test_configs.json").write_text(json.dumps({}), encoding="utf-8")
    (logs_dir / "db_test_config_secrets.json").write_text(
        json.dumps({"prod": stale_secret}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:prod")
    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert "prod" not in secrets


def test_db_test_configs_resolve_migrates_raw_public_config_before_reading_secret(
    monkeypatch,
    tmp_path,
):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    raw_dsn = "postgresql://alice:newsecret@example.com:5432/db"
    stale_secret = "postgresql://alice:oldsecret@example.com:5432/db"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"prod": {"dsn": raw_dsn, "description": "Raw legacy"}}),
        encoding="utf-8",
    )
    (logs_dir / "db_test_config_secrets.json").write_text(
        json.dumps({"prod": stale_secret}),
        encoding="utf-8",
    )

    assert service._resolve_dsn_reference("db_config:prod") == raw_dsn
    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert secrets["prod"] == raw_dsn
    public_config = json.loads((logs_dir / "db_test_configs.json").read_text(encoding="utf-8"))["prod"]
    assert public_config["dsn"] == "postgresql://***:***@example.com:5432/db"
    assert public_config["dsn_fingerprint"] == service._dsn_fingerprint(raw_dsn)


def test_db_test_configs_absent_public_config_prunes_orphan_stale_secret(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stale_secret = "postgresql://alice:oldsecret@example.com/db"
    (logs_dir / "db_test_config_secrets.json").write_text(
        json.dumps({"prod": stale_secret}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    assert listed["configs"] == []
    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert "prod" not in secrets
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:prod")


def test_db_test_configs_invalid_public_config_prunes_orphan_stale_secret(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stale_secret = "postgresql://alice:oldsecret@example.com/db"
    (logs_dir / "db_test_configs.json").write_text("not-json", encoding="utf-8")
    (logs_dir / "db_test_config_secrets.json").write_text(
        json.dumps({"prod": stale_secret}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    assert listed["configs"] == []
    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert "prod" not in secrets
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:prod")


def test_db_test_configs_non_dict_public_config_prunes_orphan_stale_secret(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stale_secret = "postgresql://alice:oldsecret@example.com/db"
    (logs_dir / "db_test_configs.json").write_text(json.dumps([]), encoding="utf-8")
    (logs_dir / "db_test_config_secrets.json").write_text(
        json.dumps({"prod": stale_secret}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    assert listed["configs"] == []
    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert "prod" not in secrets
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:prod")


def test_db_test_configs_public_only_masked_config_drops_stale_secret(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stale_secret = "postgresql://alice:oldsecret@example.com/db"
    masked_dsn = "postgresql://***:***@example.com/db?api_key=***"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"prod": {"dsn": masked_dsn, "description": "Public"}}),
        encoding="utf-8",
    )
    (logs_dir / "db_test_config_secrets.json").write_text(
        json.dumps({"prod": stale_secret}),
        encoding="utf-8",
    )

    service.handle_service_action("db.test_configs.list", {})

    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert "prod" not in secrets
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:prod")


def test_db_test_configs_masked_config_drops_stale_secret_on_fingerprint_mismatch(
    monkeypatch,
    tmp_path,
):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stale_secret = "postgresql://alice:oldsecret@example.com/db"
    masked_dsn = "postgresql://***:***@example.com/db?api_key=***"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({
            "prod": {
                "dsn": masked_dsn,
                "dsn_fingerprint": "notmatching",
                "description": "Public",
            }
        }),
        encoding="utf-8",
    )
    (logs_dir / "db_test_config_secrets.json").write_text(
        json.dumps({"prod": stale_secret}),
        encoding="utf-8",
    )

    service.handle_service_action("db.test_configs.list", {})

    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert "prod" not in secrets
    with pytest.raises(ValueError, match="secret is unavailable"):
        service._resolve_dsn_reference("db_config:prod")


def test_db_test_configs_save_rejects_partially_masked_dsn(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)

    with pytest.raises(ValueError, match="valid raw dsn"):
        service.handle_service_action(
            "db.test_configs.save",
            {
                "name": "partial",
                "dsn": "postgresql://host/db?user=alice&password=***",
            },
        )


def test_db_test_configs_migrates_urlencoded_raw_query_secret(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    dsn = "postgresql://host:5432/db?api%5Fkey=rawsecret&sslmode=require"
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"encoded": {"dsn": dsn, "description": "Encoded"}}),
        encoding="utf-8",
    )

    listed = service.handle_service_action("db.test_configs.list", {})

    config = listed["configs"][0]
    assert config["dsn"] != dsn
    assert "rawsecret" not in config["dsn"]
    assert "api%5Fkey=***" in config["dsn"]
    assert service._resolve_dsn_reference("db_config:encoded") == dsn
    public_text = (logs_dir / "db_test_configs.json").read_text(encoding="utf-8")
    assert "rawsecret" not in public_text
    secrets = json.loads((logs_dir / "db_test_config_secrets.json").read_text(encoding="utf-8"))
    assert secrets["encoded"] == dsn


def test_db_test_configs_migration_errors_are_not_silenced(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "db_test_configs.json").write_text(
        json.dumps({"legacy": {"dsn": "postgresql://alice:secret@example.com/db"}}),
        encoding="utf-8",
    )

    def fail_migration(_configs):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_persist_legacy_db_test_config_secrets", fail_migration)

    with pytest.raises(OSError, match="disk full"):
        service._load_db_test_configs()


def test_db_test_configs_save_resolves_connection_ref(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    dsn = "postgresql://alice:secret@example.com:5432/app"
    first = service.handle_service_action("db.test_configs.save", {"name": "prod", "dsn": dsn})
    ref = first["configs"][0]["connection_ref"]

    service.handle_service_action("db.test_configs.save", {"name": "copy", "dsn": ref})

    assert service._resolve_dsn_reference("db_config:copy") == dsn


def test_connection_actions_persist_restore_and_filter_by_owner(monkeypatch, tmp_path):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    admin = Principal(
        subject="admin",
        tenant_id="ops",
        roles=frozenset({"admin", "user"}),
    )
    alice = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))
    bob = Principal(subject="bob", tenant_id="tenant-1", roles=frozenset({"user"}))
    dsn = "postgresql://svc:registry-secret@db.example:5432/app"

    alice_connection = service.handle_service_action(
        "db.connections.register",
        {
            "display_name": "Alice warehouse",
            "dsn": dsn,
            "owner_subject": "alice",
            "tenant_id": "tenant-1",
        },
        principal=admin,
    )["connection"]
    service.handle_service_action(
        "db.connections.register",
        {
            "display_name": "Bob warehouse",
            "dsn": dsn,
            "owner_subject": "bob",
            "tenant_id": "tenant-1",
        },
        principal=admin,
    )

    assert service.handle_service_action(
        "db.connections.list", {}, principal=alice
    )["connections"] == [alice_connection]
    assert [
        item["display_name"]
        for item in service.handle_service_action(
            "db.connections.list", {}, principal=bob
        )["connections"]
    ] == ["Bob warehouse"]
    public_text = (tmp_path / "logs" / "db_test_configs.json").read_text(
        encoding="utf-8"
    )
    assert "registry-secret" not in public_text
    assert "dsn" not in json.loads(public_text)[alice_connection["connection_ref"]]
    secret_path = tmp_path / "logs" / "db_test_config_secrets.json"
    assert secret_path.stat().st_mode & 0o777 == 0o600

    service._CONNECTION_REGISTRY = None
    restored = service.handle_service_action(
        "db.connections.list", {}, principal=alice
    )["connections"]
    assert restored == [alice_connection]
    assert service._resolve_dsn_reference(
        alice_connection["connection_ref"], principal=alice
    ) == dsn

    deleted = service.handle_service_action(
        "db.connections.delete",
        {"connection_ref": alice_connection["connection_ref"]},
        principal=admin,
    )
    assert deleted["deleted"] is True
    service._CONNECTION_REGISTRY = None
    assert service.handle_service_action(
        "db.connections.list", {}, principal=alice
    )["connections"] == []


def test_legacy_connection_requires_admin_and_explicit_migration(monkeypatch, tmp_path):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    admin = Principal(
        subject="admin",
        tenant_id="ops",
        roles=frozenset({"admin", "user"}),
    )
    alice = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))
    dsn = "postgresql://svc:legacy-secret@db.example:5432/app"
    legacy = service.handle_service_action(
        "db.test_configs.save",
        {"name": "prod", "dsn": dsn},
        principal=admin,
    )["configs"][0]

    with pytest.raises(PermissionError, match="admin"):
        service._resolve_dsn_reference(legacy["connection_ref"], principal=alice)
    with pytest.raises(ValueError, match="owner_subject"):
        service.handle_service_action(
            "db.connections.migrate_legacy",
            {
                "connection_ref": legacy["connection_ref"],
                "tenant_id": "tenant-1",
            },
            principal=admin,
        )

    migrated = service.handle_service_action(
        "db.connections.migrate_legacy",
        {
            "connection_ref": legacy["connection_ref"],
            "display_name": "Production",
            "owner_subject": "alice",
            "tenant_id": "tenant-1",
        },
        principal=admin,
    )["connection"]

    assert migrated["connection_ref"].startswith("conn-")
    assert service._resolve_dsn_reference(
        migrated["connection_ref"], principal=alice
    ) == dsn
    assert service.handle_service_action(
        "db.test_configs.list", {}, principal=admin
    )["configs"][0]["connection_ref"] == "db_config:prod"


def test_generate_and_schema_legacy_reference_paths_remain_admin_only(
    monkeypatch,
    tmp_path,
):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        service,
        "_workflow_agui_entrypoint",
        lambda _name: "presets.text_to_sql.generate",
    )
    admin = Principal(
        subject="admin",
        tenant_id="ops",
        roles=frozenset({"admin", "user"}),
    )
    user = Principal(
        subject="alice",
        tenant_id="tenant-1",
        roles=frozenset({"user"}),
    )
    dsn = "postgresql://svc:legacy-secret@db.example:5432/app"
    legacy_ref = service.handle_service_action(
        "db.test_configs.save",
        {"name": "prod", "dsn": dsn},
        principal=admin,
    )["configs"][0]["connection_ref"]

    for action, payload in (
        (
            "presets.text_to_sql.generate",
            {"query": "show users", "connection_ref": legacy_ref},
        ),
        ("text_to_sql.schema.load", {"connection_ref": legacy_ref}),
    ):
        with pytest.raises(PermissionError, match="admin"):
            service.handle_service_action(action, payload, principal=user)

    generated = service.handle_service_action(
        "presets.text_to_sql.generate",
        {"query": "show users", "connection_ref": legacy_ref},
        principal=admin,
    )
    assert generated["parameters"]["connection_ref"] == legacy_ref
    assert wf_manager.calls[-1]["parameters"]["dsn"] == dsn
    assert wf_manager.calls[-1]["parameters"]["schema_scope"] == {
        "serialization_version": 1,
        "tenant_id": "ops",
        "access_scope_id": "owner:admin",
        "connection_view_id": f"compatibility-run:{generated['run_id']}",
        "transient": True,
    }
    assert "schema_scope" not in generated["parameters"]
    monkeypatch.setattr(
        service,
        "_load_text_to_sql_schema_from_memory",
        lambda resolved_dsn, **_kwargs: (
            {"users": {"columns": ["id"]}} if resolved_dsn == dsn else None
        ),
    )
    loaded = service.handle_service_action(
        "text_to_sql.schema.load",
        {"connection_ref": legacy_ref},
        principal=admin,
    )
    assert loaded["source"] == "memory"


def test_generate_connection_admission_keeps_secret_private_and_events_admin_compat(
    monkeypatch,
    tmp_path,
):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        service,
        "_workflow_agui_entrypoint",
        lambda _name: "presets.text_to_sql.generate",
    )
    admin = Principal(
        subject="admin",
        tenant_id="ops",
        roles=frozenset({"admin", "user"}),
    )
    alice = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))
    dsn = "postgresql://svc:worker-secret@db.example:5432/app"
    connection = service.handle_service_action(
        "db.connections.register",
        {
            "display_name": "Production",
            "dsn": dsn,
            "owner_subject": "alice",
            "tenant_id": "tenant-1",
        },
        principal=admin,
    )["connection"]

    with pytest.raises(PermissionError, match="connection_ref"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {"query": "show users", "dsn": dsn},
            principal=alice,
        )
    with pytest.raises(PermissionError, match="admin_raw_dsn_compat"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {"query": "show users", "dsn": dsn},
            principal=admin,
        )
    assert wf_manager.calls == []

    generated = service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show users",
            "connection_ref": connection["connection_ref"],
            "schema_scope": {
                "serialization_version": 1,
                "tenant_id": "attacker",
                "access_scope_id": "tenant-shared",
                "connection_view_id": "registry:attacker",
                "transient": False,
            },
        },
        principal=alice,
    )
    private_parameters = wf_manager.calls[-1]["parameters"]
    assert private_parameters["dsn"] == dsn
    assert private_parameters["connection_ref"] == connection["connection_ref"]
    assert private_parameters["schema_scope"] == {
        "serialization_version": 1,
        "tenant_id": "tenant-1",
        "access_scope_id": "owner:alice",
        "connection_view_id": f"registry:{connection['connection_ref']}",
        "transient": False,
    }
    assert "dsn" not in generated["parameters"]
    assert "safety_policy" not in generated["parameters"]
    assert "schema_scope" not in generated["parameters"]
    assert generated["parameters"]["connection_ref"] == connection["connection_ref"]
    assert "worker-secret" not in json.dumps(generated, ensure_ascii=False)

    shared_connection = service.handle_service_action(
        "db.connections.register",
        {
            "display_name": "Tenant shared",
            "dsn": dsn,
            "owner_subject": None,
            "tenant_id": "tenant-1",
        },
        principal=admin,
    )["connection"]
    service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show shared users",
            "connection_ref": shared_connection["connection_ref"],
        },
        principal=alice,
    )
    assert wf_manager.calls[-1]["parameters"]["schema_scope"] == {
        "serialization_version": 1,
        "tenant_id": "tenant-1",
        "access_scope_id": "tenant-shared",
        "connection_view_id": f"registry:{shared_connection['connection_ref']}",
        "transient": False,
    }

    raw_payload = {
        "query": "show users",
        "dsn": dsn,
        "admin_raw_dsn_compat": True,
        "idempotency_key": "admin-compat-once",
    }
    first = service.handle_service_action(
        "presets.text_to_sql.generate", raw_payload, principal=admin
    )
    second = service.handle_service_action(
        "presets.text_to_sql.generate", raw_payload, principal=admin
    )
    assert second["run_id"] == first["run_id"]
    compat_events = [
        event
        for event in service._AGUI_EVENT_STORE.list_after(first["run_id"], 0)
        if event.event_type == "TEXT_TO_SQL_ADMIN_RAW_DSN_COMPAT"
    ]
    assert len(compat_events) == 1
    event_text = json.dumps(compat_events[0].payload, ensure_ascii=False)
    assert "worker-secret" not in event_text
    assert dsn not in event_text
    assert compat_events[0].payload["connection_ref"] == "admin_raw_dsn_compat"
    assert wf_manager.calls[-1]["parameters"]["connection_ref"] == "admin_raw_dsn_compat"
    assert wf_manager.calls[-1]["parameters"]["schema_scope"] == {
        "serialization_version": 1,
        "tenant_id": "ops",
        "access_scope_id": "owner:admin",
        "connection_view_id": f"compatibility-run:{first['run_id']}",
        "transient": True,
    }
    assert "dsn" not in first["parameters"]
    assert "schema_scope" not in first["parameters"]


def test_schema_load_accepts_user_connection_ref_but_rejects_user_raw_dsn(
    monkeypatch,
    tmp_path,
):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    admin = Principal(
        subject="admin",
        tenant_id="ops",
        roles=frozenset({"admin", "user"}),
    )
    alice = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))
    dsn = "postgresql://svc:schema-secret@db.example:5432/app"
    connection = service.handle_service_action(
        "db.connections.register",
        {
            "display_name": "Production",
            "dsn": dsn,
            "owner_subject": "alice",
            "tenant_id": "tenant-1",
        },
        principal=admin,
    )["connection"]
    seen: list[str] = []

    def load_from_memory(resolved_dsn, **_kwargs):
        seen.append(resolved_dsn)
        return {"users": {"columns": ["id"]}}

    monkeypatch.setattr(service, "_load_text_to_sql_schema_from_memory", load_from_memory)

    loaded = service.handle_service_action(
        "text_to_sql.schema.load",
        {"connection_ref": connection["connection_ref"]},
        principal=alice,
    )
    assert loaded["source"] == "memory"
    assert seen == [dsn]
    with pytest.raises(PermissionError, match="connection_ref"):
        service.handle_service_action(
            "text_to_sql.schema.load",
            {"dsn": dsn},
            principal=alice,
        )


@pytest.mark.parametrize(
    "connection_ref",
    [
        "not-a-connection",
        "postgresql://svc:secret@db.example:5432/app",
    ],
)
def test_generate_rejects_noncanonical_connection_ref_before_registry_resolution(
    monkeypatch,
    connection_ref,
):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    principal = Principal(
        subject="alice",
        tenant_id="tenant-1",
        roles=frozenset({"user"}),
    )

    def reject_registry_access():
        raise AssertionError("invalid connection_ref reached registry resolution")

    monkeypatch.setattr(service, "_connection_registry", reject_registry_access)

    with pytest.raises(ValueError, match="connection_ref|reference"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {"query": "show users", "connection_ref": connection_ref},
            principal=principal,
        )

    assert wf_manager.calls == []


@pytest.mark.parametrize(
    "connection_ref",
    [
        "not-a-connection",
        "postgresql://svc:secret@db.example:5432/app",
    ],
)
def test_schema_load_rejects_noncanonical_connection_ref_before_registry_resolution(
    monkeypatch,
    connection_ref,
):
    from backend.fastapi_app.agui.auth import Principal

    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())
    principal = Principal(
        subject="alice",
        tenant_id="tenant-1",
        roles=frozenset({"user"}),
    )

    def reject_registry_access():
        raise AssertionError("invalid connection_ref reached registry resolution")

    monkeypatch.setattr(service, "_connection_registry", reject_registry_access)

    with pytest.raises(ValueError, match="connection_ref|reference"):
        service.handle_service_action(
            "text_to_sql.schema.load",
            {"connection_ref": connection_ref},
            principal=principal,
        )


def test_public_workflow_parameter_snapshot_omits_private_dsn_and_safety_policy():
    streamlit_api = _load_light_workflow_streamlit_api()
    dsn = "postgresql://svc:snapshot-secret@db.example:5432/app"

    public = streamlit_api._public_workflow_parameters(
        {
            "query": "show users",
            "dsn": dsn,
            "connection_ref": "conn-a1b2c3d4-e5f6-4abc-8def-a1b2c3d4e5f6",
            "safety_policy": {"forbidden_functions": ["pg_sleep"]},
            "schema_scope": {
                "serialization_version": 1,
                "tenant_id": "tenant-1",
                "access_scope_id": "owner:alice",
                "connection_view_id": "registry:conn-private",
                "transient": False,
            },
        }
    )

    assert public == {
        "query": "show users",
        "connection_ref": "conn-a1b2c3d4-e5f6-4abc-8def-a1b2c3d4e5f6",
    }


def test_agui_redaction_masks_scalar_secrets_query_strings_and_error_text(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    payload = service._redact_payload(
        {
            "password": "secret",
            "parsed_components": {"query": "sslmode=require&password=secret&token=abc"},
            "error": "driver failed for postgresql://alice:secret@example.com/db?api_key=abc",
            "plain_error": "driver failed password=secret token=abc",
        }
    )

    assert payload["password"] == "<redacted>"
    assert "secret" not in payload["parsed_components"]["query"]
    assert "password=secret" not in payload["plain_error"]
    assert "token=abc" not in payload["plain_error"]
    assert "api_key=%2A%2A%2A" in payload["error"] or "api_key=***" in payload["error"]
    assert "***:***@example.com" in payload["error"]


def test_raw_text_to_sql_schema_load_is_request_local_and_live_only(
    monkeypatch,
    tmp_path,
):
    from backend.fastapi_app.agui.auth import Principal
    from custom_tools.text_to_sql.schema_loader import SchemaFileManager, SchemaLoader

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    admin = Principal(
        subject="admin",
        tenant_id="ops",
        roles=frozenset({"admin", "user"}),
    )
    database = tmp_path / "app.db"
    database.touch()
    dsn = f"sqlite://{database}"
    introspections: list[int] = []

    class LivePlugin:
        @staticmethod
        def connect(resolved_dsn):
            assert resolved_dsn == dsn
            return object()

        @staticmethod
        def parse_schema_from_dsn(resolved_dsn):
            assert resolved_dsn == dsn
            return None

        @staticmethod
        def introspect_schema(_conn, _schema):
            generation = len(introspections) + 1
            introspections.append(generation)
            return {
                "users": {
                    "columns": {
                        f"live_{generation}": {"type": "INTEGER"},
                    }
                }
            }

        @staticmethod
        def normalize_schema_names(_dsn, schema):
            return schema

        @staticmethod
        def close(_conn):
            return None

    db_plugins = sys.modules["db_plugins"]
    monkeypatch.setattr(db_plugins, "get_plugin", lambda _dsn: LivePlugin(), raising=False)
    monkeypatch.setattr(
        service,
        "_load_text_to_sql_schema_from_memory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("raw one-shot schema load read reusable memory")
        ),
    )
    monkeypatch.setattr(
        SchemaFileManager,
        "load_scoped_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("raw one-shot schema load read a reusable snapshot")
        ),
    )
    monkeypatch.setattr(
        SchemaFileManager,
        "save_scoped_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("raw one-shot schema load wrote a reusable snapshot")
        ),
    )
    monkeypatch.setattr(
        SchemaLoader,
        "_load_sqlrag_schema",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("raw one-shot schema load read a legacy snapshot")
        ),
    )
    monkeypatch.setattr(
        SchemaLoader,
        "autosave_schema",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("raw one-shot schema load wrote a legacy snapshot")
        ),
    )
    scopes = []
    load_scoped_schema = SchemaLoader.load_scoped_schema

    def observe_scope(loader, schema_info, resolved_dsn, scope):
        scopes.append(scope)
        return load_scoped_schema(loader, schema_info, resolved_dsn, scope)

    monkeypatch.setattr(SchemaLoader, "load_scoped_schema", observe_scope)

    first = service.handle_service_action(
        "text_to_sql.schema.load",
        {"dsn": dsn},
        principal=admin,
    )
    second = service.handle_service_action(
        "text_to_sql.schema.load",
        {"dsn": dsn},
        principal=admin,
    )

    assert first == {
        "schema": {"users": {"columns": {"live_1": {"type": "INTEGER"}}}},
        "source": "db",
        "warnings": [],
    }
    assert second == {
        "schema": {"users": {"columns": {"live_2": {"type": "INTEGER"}}}},
        "source": "db",
        "warnings": [],
    }
    assert introspections == [1, 2]
    assert len(scopes) == 2
    assert all(scope.transient for scope in scopes)
    assert {scope.tenant_id for scope in scopes} == {"ops"}
    assert {scope.access_scope_id for scope in scopes} == {"owner:admin"}
    assert scopes[0].scope_key != scopes[1].scope_key


def test_raw_text_to_sql_schema_load_failure_never_returns_reusable_schema(
    monkeypatch,
    tmp_path,
):
    from backend.fastapi_app.agui.auth import Principal
    from custom_tools.text_to_sql.schema_loader import SchemaFileManager
    from custom_tools.text_to_sql.schema_namespace import SchemaFreshnessUnavailable

    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())
    admin = Principal(
        subject="admin",
        tenant_id="ops",
        roles=frozenset({"admin", "user"}),
    )
    database = tmp_path / "app.db"
    database.touch()
    dsn = f"sqlite://{database}"
    events: list[str] = []

    class FailingPlugin:
        @staticmethod
        def connect(_dsn):
            events.append("connect")
            return object()

        @staticmethod
        def parse_schema_from_dsn(_dsn):
            return None

        @staticmethod
        def introspect_schema(_conn, _schema):
            events.append("introspect")
            raise RuntimeError("live schema unavailable")

        @staticmethod
        def close(_conn):
            events.append("close")

    db_plugins = sys.modules["db_plugins"]
    monkeypatch.setattr(db_plugins, "get_plugin", lambda _dsn: FailingPlugin(), raising=False)
    memory_reads: list[str] = []

    def stale_memory(*_args, **_kwargs):
        memory_reads.append("memory")
        return {"stale_users": {"columns": {"id": {"type": "INTEGER"}}}}

    monkeypatch.setattr(service, "_load_text_to_sql_schema_from_memory", stale_memory)
    monkeypatch.setattr(
        SchemaFileManager,
        "load_scoped_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("raw one-shot schema load read a reusable snapshot")
        ),
    )

    with pytest.raises(SchemaFreshnessUnavailable, match="live schema introspection"):
        service.handle_service_action(
            "text_to_sql.schema.load",
            {"dsn": dsn},
            principal=admin,
        )

    assert events == ["connect", "introspect", "close"]
    assert memory_reads == []


def test_text_to_sql_schema_load_rejects_non_boolean_fallback_flag(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_load_text_to_sql_schema_from_memory", lambda *a, **k: None)

    with pytest.raises(ValueError, match="allow_db_schema_fallback"):
        service.handle_service_action(
            "text_to_sql.schema.load",
            {"dsn": "sqlite:///tmp/app.db", "allow_db_schema_fallback": "typo"},
        )


def test_workflow_result_artifacts_and_logs_are_redacted(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    admin = _register_legacy_admin_run(service, "run-1")
    raw_dsn = "postgresql://alice:secret@example.com/app?api_key=abc"
    wf_manager.active_runs = {"wf-1": {"final_output": {"dsn": raw_dsn}}}
    monkeypatch.setattr(service, "_agent_manager", lambda: types.SimpleNamespace(active_runs={"agent-1": {"error": raw_dsn}}))
    monkeypatch.setattr(
        service,
        "_workflow_result_from_store",
        lambda run_id: {
            "status": "failed",
            "success": False,
            "result": {"dsn": raw_dsn},
            "error": f"failed {raw_dsn}",
            "artifacts": {"metadata": {"database_url": raw_dsn}},
        },
    )
    monkeypatch.setattr(wf_manager, "get_workflow_artifacts", lambda run_id: {"parameters": {"dsn": raw_dsn}}, raising=False)

    class TelemetryManager:
        def read_trace_events(self, run_id):
            return [{"attributes": {"database_url": raw_dsn}}]

        def load_trace_file(self, run_id):
            return {"spans": [{"attributes": {"output.value": raw_dsn}}]}

    class LoggingManager:
        def get_run_logs(self, run_id, limit=1000):
            return [{"message": f"driver failed {raw_dsn}", "password": "secret"}]

    monkeypatch.setattr(service, "_telemetry_manager", lambda: TelemetryManager())
    monkeypatch.setattr(service, "_logging_manager", lambda: LoggingManager())

    result = service.handle_service_action(
        "workflows.result",
        {"run_id": "run-1"},
        principal=admin,
    )
    artifacts = service.handle_service_action(
        "workflows.artifacts",
        {"run_id": "run-1"},
        principal=admin,
    )
    active_runs = service._active_runs()
    trace_events = service.handle_service_action("telemetry.trace_events", {"run_id": "run-1"})
    trace_file = service.handle_service_action("telemetry.trace_file", {"run_id": "run-1"})
    logs = service.handle_service_action("logs.run_logs", {"run_id": "run-1"})
    serialized = json.dumps(
        {"result": result, "artifacts": artifacts, "active_runs": active_runs, "trace_events": trace_events, "trace_file": trace_file, "logs": logs},
        ensure_ascii=False,
    )

    assert "secret" not in serialized
    assert "api_key=abc" not in serialized
    assert "***:***@example.com" in serialized


def test_agui_redaction_sanitizes_gzip_base64_report(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    raw_dsn = "postgresql://alice:secret@example.com/app?api_key=abc"
    raw_email = "person@example.com"
    raw_phone = "+7 (495) 123-45-67"
    encoded = base64.b64encode(
        gzip.compress(f"<html>{raw_dsn} {raw_email} {raw_phone}</html>".encode("utf-8"))
    ).decode("ascii")

    redacted = service._redact_payload({"base64_gzip": encoded})
    decoded = gzip.decompress(base64.b64decode(redacted["base64_gzip"])).decode("utf-8")

    assert "secret" not in decoded
    assert "api_key=abc" not in decoded
    assert raw_email not in decoded
    assert raw_phone not in decoded
    assert "***:***@example.com" in decoded
    assert "[EMAIL]" in decoded
    assert "[PHONE]" in decoded











def test_ssrf_dns_timeout_cancels_future(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    callbacks = []

    class _Future:
        cancelled = False

        def add_done_callback(self, callback):
            callbacks.append(callback)

        def result(self, timeout=None):
            raise concurrent.futures.TimeoutError()

        def cancel(self):
            self.cancelled = True
            return True

    future = _Future()

    class _Executor:
        def submit(self, *_args, **_kwargs):
            return future

    monkeypatch.setattr(service, "_get_dns_resolve_executor", lambda: _Executor())

    try:
        with pytest.raises(ValueError, match="таймаут"):
            service._validate_url_no_ssrf("https://example.test/image.png")
        assert future.cancelled is True
    finally:
        for callback in callbacks:
            callback(future)


def test_ssrf_dns_resolver_busy_fails_before_queueing(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    acquired = 0
    while service._DNS_RESOLVE_SEMAPHORE.acquire(blocking=False):
        acquired += 1

    class _Executor:
        def submit(self, *_args, **_kwargs):
            raise AssertionError("DNS work must not be queued while resolver is busy")

    monkeypatch.setattr(service, "_get_dns_resolve_executor", lambda: _Executor())
    try:
        with pytest.raises(ValueError, match="busy"):
            service._validate_url_no_ssrf("https://example.test/image.png")
    finally:
        for _ in range(acquired):
            service._DNS_RESOLVE_SEMAPHORE.release()








def test_text_to_sql_workflow_result_reports_failure_and_dry_run_execution(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    admin = _register_legacy_admin_run(service, "run-text-to-sql")

    monkeypatch.setattr(
        service,
        "_workflow_result_from_store",
        lambda run_id: {
            "run_id": run_id,
            "status": "failed",
            "success": False,
            "error": "Workflow failed steps: sql_pipeline",
            "terminal_outcome": {
                "run_id": run_id,
                "status": "failed",
                    "reason_code": "RESULT_AGGREGATION_FAILED",
                "sql": "SELECT 1",
                "generated": True,
                "approved": True,
                "executed": False,
                "dry_run": True,
                "audited": True,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "error": "Workflow failed steps: sql_pipeline",
                    "execution": {
                        "success": True,
                        "sql_query": "SELECT 1",
                        "data": [],
                        "columns": [],
                        "rows_affected": 0,
                        "execution_time_ms": 1,
                        "dry_run_only": True,
                        "skipped_execution": True,
                        "applied_row_limit": 100,
                },
                "audit": {"status": "logged", "log_id": "audit-1"},
                "persistence": {"status": "not_attempted"},
            },
            "result": {"message": "partial"},
            "artifacts": {
                "final_output": {"message": "partial"},
                "step_outputs": {"sql_pipeline": {"sql_query": "SELECT 1"}},
                "metadata": {
                    "execution": {
                        "dry_run_only": True,
                        "executed": False,
                        "status": "skipped",
                    }
                },
            },
            "snapshot": {"workflow_name": "text_to_sql_pipeline"},
        },
    )

    result = service.handle_service_action(
        "workflows.result",
        {"run_id": "run-text-to-sql"},
        principal=admin,
    )

    assert result["status"] == "failed"
    assert result["success"] is False
    assert result["error"] == "Workflow failed steps: sql_pipeline"
    assert result["execution"]["dry_run_only"] is True
    assert result["execution"]["skipped_execution"] is True
    assert result["terminal_outcome"]["status"] == "failed"
    assert result["artifacts"]["step_outputs"]["sql_pipeline"]["sql_query"] == "SELECT 1"


@pytest.mark.parametrize(
    ("terminal_outcome", "expected_status", "expected_progress", "has_terminal"),
    [
        (None, "unknown_legacy", 0.0, False),
        ({"status": "succeeded"}, "unknown_legacy", 0.0, False),
        (_terminal_contract_payload("succeeded", "run-status"), "completed", 100.0, True),
    ],
)
def test_text_to_sql_stored_status_uses_strict_terminal_contract(
    monkeypatch,
    terminal_outcome,
    expected_status,
    expected_progress,
    has_terminal,
):
    class StoredOnlyManager(_WorkflowManagerStub):
        def get_workflow_status(self, run_id):
            return None

    wf_manager = StoredOnlyManager()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    admin = _register_legacy_admin_run(service, "run-status")
    stored = {
        "run_id": "run-status",
        "status": "completed",
        "success": True,
        "result": {"message": "stored output"},
        "snapshot": {"workflow_name": "text_to_sql_pipeline"},
    }
    if terminal_outcome is not None:
        stored["terminal_outcome"] = terminal_outcome
    monkeypatch.setattr(service, "_workflow_result_from_store", lambda run_id: stored)

    response = service.handle_service_action(
        "workflows.status",
        {"run_id": "run-status"},
        principal=admin,
    )
    status = response["status"]

    assert status["status"] == expected_status
    assert status["progress_percentage"] == expected_progress
    assert bool(status.get("terminal_outcome")) is has_terminal


def test_generic_stored_result_is_not_reclassified_by_terminal_candidate(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    admin = _register_legacy_admin_run(service, "generic-run")
    terminal = _terminal_contract_payload("failed", "generic-run")
    monkeypatch.setattr(
        service,
        "_workflow_result_from_store",
        lambda run_id: {
            "run_id": run_id,
            "status": "completed",
            "success": True,
            "result": {"message": "generic output"},
            "terminal_outcome": terminal,
            "snapshot": {
                "workflow_name": "generic_pipeline",
                "category": "text_to_sql",
            },
        },
    )

    result = service.handle_service_action(
        "workflows.result",
        {"run_id": "generic-run"},
        principal=admin,
    )

    assert result["status"] == "completed"
    assert result["success"] is True


def test_text_to_sql_legacy_result_without_terminal_outcome_is_not_success(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    admin = _register_legacy_admin_run(service, "legacy")
    monkeypatch.setattr(
        service,
        "_workflow_result_from_store",
        lambda run_id: {
            "run_id": run_id,
            "status": "completed",
            "success": True,
            "result": {"message": "legacy output"},
            "snapshot": {"workflow_name": "text_to_sql_pipeline"},
        },
    )

    result = service.handle_service_action(
        "workflows.result",
        {"run_id": "legacy"},
        principal=admin,
    )

    assert result["status"] == "unknown_legacy"
    assert result["success"] is False


@pytest.mark.parametrize(
    "terminal_outcome",
    [
        {"status": "succeeded"},
        {**_terminal_contract_payload(), "generated": "true"},
        {
            **_terminal_contract_payload(),
            "execution": {
                **_terminal_contract_payload()["execution"],
                "success": False,
            },
        },
        {
            **_terminal_contract_payload(),
            "execution": {
                **_terminal_contract_payload()["execution"],
                "data": [[999]],
            },
        },
        {
            **_terminal_contract_payload(),
            "audit": {"status": "logged", "error": "sink failed"},
        },
        {
            **_terminal_contract_payload(),
            "execution": {
                **_terminal_contract_payload()["execution"],
                "error_message": "contradictory executor error",
            },
        },
        {
            **_terminal_contract_payload(),
            "status": "failed",
            "reason_code": "EXECUTION_FAILED",
            "executed": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "error": "execution failed",
            "execution": {
                "success": False,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "dry_run_only": False,
                "skipped_execution": False,
            },
        },
        {
            **_terminal_contract_payload(),
            "status": "failed",
            "reason_code": "AUDIT_FAILED",
            "audited": False,
            "error": "audit failed",
            "audit": {"status": "banana", "error": "audit failed"},
        },
        {
            **_terminal_contract_payload(),
            "status": "failed",
            "reason_code": "AUDIT_FAILED",
            "audited": False,
            "error": "audit failed",
            "audit": {"status": "error"},
        },
    ],
)
def test_text_to_sql_service_rejects_partial_or_malformed_terminal_outcome(
    monkeypatch,
    terminal_outcome,
):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    admin = _register_legacy_admin_run(service, "run-1")
    monkeypatch.setattr(
        service,
        "_workflow_result_from_store",
        lambda run_id: {
            "run_id": run_id,
            "status": "completed",
            "success": True,
            "result": {"message": "stored output"},
            "terminal_outcome": terminal_outcome,
            "snapshot": {"workflow_name": "text_to_sql_pipeline"},
        },
    )

    result = service.handle_service_action(
        "workflows.result",
        {"run_id": "run-1"},
        principal=admin,
    )

    assert result["status"] == "unknown_legacy"
    assert result["success"] is False
    assert "terminal_outcome" not in result


@pytest.mark.parametrize(
    ("terminal_status", "legacy_status"),
    [("cancelled", "cancelled"), ("timed_out", "failed")],
)
def test_text_to_sql_service_derives_cancel_and_timeout_from_valid_terminal(
    monkeypatch,
    terminal_status,
    legacy_status,
):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    admin = _register_legacy_admin_run(service, "run-1")
    terminal = _terminal_contract_payload(terminal_status, "run-1")
    monkeypatch.setattr(
        service,
        "_workflow_result_from_store",
        lambda run_id: {
            "run_id": run_id,
            "status": "completed",
            "success": True,
            "result": {"message": "must not imply success"},
            "terminal_outcome": terminal,
            "snapshot": {"workflow_name": "text_to_sql_pipeline"},
        },
    )

    result = service.handle_service_action(
        "workflows.result",
        {"run_id": "run-1"},
        principal=admin,
    )

    assert result["status"] == legacy_status
    assert result["success"] is False
    assert result["terminal_outcome"]["status"] == terminal_status


def test_workflow_yaml_actions_reject_path_traversal(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    for action in ["workflows.get_yaml", "workflows.parse_yaml"]:
        with pytest.raises(ValueError, match="invalid workflow_name"):
            service.handle_service_action(action, {"workflow_name": "../secret"})

    with pytest.raises(ValueError, match="invalid workflow_name"):
        service.handle_service_action(
            "workflows.save_yaml",
            {"workflow_name": "../secret", "yaml": "name: secret\nsteps: []"},
        )


def test_service_policy_blocks_admin_actions_for_user(monkeypatch):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    user = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))

    blocked_actions = [
        ("workflows.save_yaml", {"workflow_name": "demo", "yaml": "name: demo\nsteps: []"}),
        ("tools.invoke", {"tool_name": "demo"}),
        ("files.read", {"path": "README.md"}),
        ("config.update", {"config": {}}),
        ("db.test_configs.save", {"name": "prod", "dsn": "sqlite:///tmp/app.db"}),
        ("memory.export", {"format": "json"}),
        ("memory.full_cleanup", {"confirm": True}),
        ("logs.file_content", {"filename": "app_logs.jsonl"}),
        ("logs.file_search", {"filename": "app_logs.jsonl"}),
        ("telemetry.enable", {}),
        ("telemetry.cleanup", {}),
        ("agents.result", {"run_id": "run-other"}),
        ("system.prompt_optimizer.run", {"prompt": "x"}),
        ("system.stale_monitor.start", {}),
        ("db.introspect_schema", {"dsn": "sqlite:///tmp/app.db"}),
        ("utils.call_openai_api_streaming", {"messages": []}),
        ("presets.image.generate", {"prompt": "x"}),
    ]

    for action, payload in blocked_actions:
        with pytest.raises(PermissionError, match="requires role"):
            service.handle_service_action(action, payload, principal=user)


def test_service_action_policy_is_explicit_for_every_dispatch_branch(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    source = Path(service.__file__).read_text(encoding="utf-8")
    handled = set(re.findall(r'action == "([^"]+)"', source))
    for match in re.finditer(r"action in \(([^)]*)\)", source):
        handled.update(re.findall(r'"([^"]+)"', match.group(1)))

    assert handled <= service._ALL_SERVICE_ACTIONS
    assert service._ALL_SERVICE_ACTIONS == (
        service._USER_ACTIONS
        | service._OWNER_SCOPED_ACTIONS
        | service._MEMORY_ARCHIVIST_ACTIONS
        | service._ADMIN_ONLY_ACTIONS
    )
    assert not service._USER_ACTIONS & service._ADMIN_ONLY_ACTIONS
    assert not service._USER_ACTIONS & service._MEMORY_ARCHIVIST_ACTIONS
    assert not service._USER_ACTIONS & service._OWNER_SCOPED_ACTIONS
    assert not service._ADMIN_ONLY_ACTIONS & service._MEMORY_ARCHIVIST_ACTIONS
    assert not service._ADMIN_ONLY_ACTIONS & service._OWNER_SCOPED_ACTIONS
    assert not service._MEMORY_ARCHIVIST_ACTIONS & service._OWNER_SCOPED_ACTIONS
    assert service._ALL_SERVICE_ACTIONS - handled == {"logs.stream", "progress.stream"}


def test_service_action_policy_rejects_unclassified_action(monkeypatch):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    user = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))

    with pytest.raises(PermissionError, match="not classified"):
        service._require_service_action_role("demo.echo", user)


def test_service_policy_allows_admin_workflow_save(monkeypatch, tmp_path):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    admin = Principal(
        subject="admin",
        tenant_id="tenant-1",
        roles=frozenset({"admin", "user"}),
    )

    result = service.handle_service_action(
        "workflows.save_yaml",
        {"workflow_name": "demo_policy", "yaml": "name: demo_policy\nsteps: []"},
        principal=admin,
    )

    assert result["saved"] is True
    assert (tmp_path / "workflow_pipelines" / "demo_policy.yaml").exists()


def test_memory_search_rejects_oversize_limit_before_manager_call(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    class MemoryManager:
        def search_memory(self, **_kwargs):
            raise AssertionError("oversize limit must be rejected before manager call")

    monkeypatch.setattr(service, "_memory_manager", lambda: MemoryManager())

    with pytest.raises(ValueError, match="limit must be between 1 and 100"):
        service.handle_service_action(
            "memory.search",
            {"query": "orders", "limit": 101},
        )


def test_memory_export_passes_bounded_limit_to_manager(monkeypatch):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    calls = []

    class MemoryManager:
        def export_memory(self, **kwargs):
            calls.append(kwargs)
            return {"success": True, "format": kwargs["format"], "count": 0, "data": []}

    monkeypatch.setattr(service, "_memory_manager", lambda: MemoryManager())
    archivist = Principal(
        subject="archivist",
        tenant_id="tenant-1",
        roles=frozenset({"memory_archivist", "user"}),
    )

    result = service.handle_service_action(
        "memory.export",
        {"format": "json", "limit": 25},
        principal=archivist,
    )

    assert result["result"]["success"] is True
    assert calls[0]["limit"] == 25


def test_service_file_read_has_size_limit(monkeypatch, tmp_path):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    monkeypatch.setenv("AG_UI_MAX_FILE_READ_BYTES", "8")
    (tmp_path / "large.txt").write_text("0123456789", encoding="utf-8")
    admin = Principal(
        subject="admin",
        tenant_id="tenant-1",
        roles=frozenset({"admin", "user"}),
    )

    with pytest.raises(ValueError, match="file is too large"):
        service.handle_service_action("files.read", {"path": "large.txt"}, principal=admin)


def test_service_log_file_actions_stay_inside_logs_dir(monkeypatch, tmp_path):
    from backend.fastapi_app.agui.auth import Principal

    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "secret_logs.jsonl").write_text(
        json.dumps({"message": "outside"}) + "\n",
        encoding="utf-8",
    )
    admin = Principal(
        subject="admin",
        tenant_id="tenant-1",
        roles=frozenset({"admin", "user"}),
    )

    with pytest.raises(ValueError, match="logs directory"):
        service.handle_service_action(
            "logs.file_content",
            {"filename": "../secret_logs.jsonl"},
            principal=admin,
        )


def test_workflow_result_store_prefers_workflow_result_over_run_finished(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    class Event:
        def __init__(self, event_type, payload, *, run_id):
            self.run_id = run_id
            self.event_type = event_type
            self.payload = payload
            self.event_key = None
            self.run_incarnation = None

    class Store:
        def list_after(self, run_id, after_seq):
            return [
                Event(
                    "WORKFLOW_RESULT",
                    {"status": "completed", "artifacts": {"final_output": "ok"}},
                    run_id=run_id,
                ),
                Event(
                    "RUN_FINISHED",
                    {"type": "RUN_FINISHED", "run_id": run_id, "result": None},
                    run_id=run_id,
                ),
            ]

    monkeypatch.setattr(service, "_agui_event_store", lambda: Store())

    assert service._workflow_result_from_store("run-text-to-sql") == {
        "status": "completed",
        "artifacts": {"final_output": "ok"},
    }


def test_workflow_state_redaction_masks_dsn_inside_query_field():
    workflow_pkg = _install_light_workflow_package()
    raw = (
        "connect postgresql://alice:secret@db/app"
        "?api_key=raw-key&sslmode=require"
    )

    redacted = workflow_pkg.state_manager._redact_payload({"query": raw})

    assert "alice" not in redacted["query"]
    assert "secret" not in redacted["query"]
    assert "raw-key" not in redacted["query"]
    assert "***:***@db" in redacted["query"]
    assert "api_key=***" in redacted["query"]
    assert "sslmode=require" in redacted["query"]


def test_workflow_streamlit_redaction_masks_camel_case_secret_keys():
    streamlit_api = _load_light_workflow_streamlit_api()

    redacted = streamlit_api._redact_payload({
        "clientSecret": "client-secret",
        "accessToken": "access-token",
        "dbPassword": "db-password",
        "privateKey": "private-key",
        "message": "clientSecret=inline-secret accessToken=inline-token",
    })
    serialized = json.dumps(redacted, ensure_ascii=False)

    for secret in (
        "client-secret",
        "access-token",
        "db-password",
        "private-key",
        "inline-secret",
        "inline-token",
    ):
        assert secret not in serialized
    assert redacted["clientSecret"] == "<redacted>"
    assert redacted["accessToken"] == "<redacted>"


def test_workflow_thread_telemetry_failure_redacts_pii(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = streamlit_api.WorkflowManager(use_enhanced=False, pipelines_dir=str(tmp_path))
    run_id = "wf-telemetry-pii"
    raw_error = "driver failed person@example.com +7 (495) 123-45-67 password=topsecret"
    manager.active_runs[run_id] = {
        "run_id": run_id,
        "workflow_name": "Test Workflow",
        "status": "running",
        "start_time": streamlit_api.datetime.now(),
    }

    class WorkflowDef:
        name = "Test Workflow"

    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(lambda _path: WorkflowDef()),
    )

    class Logger:
        def info(self, *_args, **_kwargs):
            return None

        def warning(self, *_args, **_kwargs):
            return None

    unified_logging = types.ModuleType("unified_logging")
    unified_logging.get_run_logger = lambda *_args, **_kwargs: Logger()
    unified_logging.run_id_context = lambda _run_id: contextlib.nullcontext()
    monkeypatch.setitem(sys.modules, "unified_logging", unified_logging)

    class TelemetryManager:
        def __init__(self):
            self.calls = []

        def is_enabled(self):
            return True

        def start_run_trace(self, **_kwargs):
            return object()

        def finish_run_trace(self, span, success, error_message=None):
            self.calls.append({
                "span": span,
                "success": success,
                "error_message": error_message,
            })

    telemetry_manager = TelemetryManager()
    telemetry = types.ModuleType("telemetry")
    telemetry.get_telemetry_manager = lambda enabled=False: telemetry_manager
    monkeypatch.setitem(sys.modules, "telemetry", telemetry)

    opentelemetry = types.ModuleType("opentelemetry")
    opentelemetry.trace = types.SimpleNamespace(use_span=lambda _span: contextlib.nullcontext())
    monkeypatch.setitem(sys.modules, "opentelemetry", opentelemetry)

    def fail_execute(self, *_args, **_kwargs):
        raise ValueError(raw_error)

    monkeypatch.setattr(
        manager,
        "_execute_workflow_in_context",
        types.MethodType(fail_execute, manager),
    )

    with pytest.raises(ValueError):
        manager._run_workflow_thread(
            run_id,
            tmp_path / "workflow.yaml",
            {},
            "session-1",
            None,
            enable_telemetry=True,
        )

    error_message = telemetry_manager.calls[-1]["error_message"]
    assert "person@example.com" not in error_message
    assert "+7 (495) 123-45-67" not in error_message
    assert "topsecret" not in error_message
    assert "[EMAIL]" in error_message
    assert "[PHONE]" in error_message
    assert "password=***" in error_message


def test_workflow_thread_telemetry_success_output_redacts_pii(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = streamlit_api.WorkflowManager(use_enhanced=False, pipelines_dir=str(tmp_path))
    run_id = "wf-telemetry-success-pii"
    manager.active_runs[run_id] = {
        "run_id": run_id,
        "workflow_name": "Test Workflow",
        "status": "running",
        "start_time": streamlit_api.datetime.now(),
    }

    class WorkflowDef:
        name = "Test Workflow"

    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(lambda _path: WorkflowDef()),
    )

    class Logger:
        def info(self, *_args, **_kwargs):
            return None

        def warning(self, *_args, **_kwargs):
            return None

    unified_logging = types.ModuleType("unified_logging")
    unified_logging.get_run_logger = lambda *_args, **_kwargs: Logger()
    unified_logging.run_id_context = lambda _run_id: contextlib.nullcontext()
    monkeypatch.setitem(sys.modules, "unified_logging", unified_logging)

    class Span:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, key, value):
            self.attrs[key] = value

    class TelemetryManager:
        def __init__(self):
            self.span = Span()

        def is_enabled(self):
            return True

        def start_run_trace(self, **_kwargs):
            return self.span

        def finish_run_trace(self, *_args, **_kwargs):
            return None

    telemetry_manager = TelemetryManager()
    telemetry = types.ModuleType("telemetry")
    telemetry.get_telemetry_manager = lambda enabled=False: telemetry_manager
    monkeypatch.setitem(sys.modules, "telemetry", telemetry)

    opentelemetry = types.ModuleType("opentelemetry")
    opentelemetry.trace = types.SimpleNamespace(use_span=lambda _span: contextlib.nullcontext())
    monkeypatch.setitem(sys.modules, "opentelemetry", opentelemetry)

    def execute(self, *_args, **_kwargs):
        return types.SimpleNamespace(
            final_output={
                "note": "contact person@example.com +7 (495) 123-45-67",
                "password": "topsecret",
            },
        )

    monkeypatch.setattr(
        manager,
        "_execute_workflow_in_context",
        types.MethodType(execute, manager),
    )

    manager._run_workflow_thread(
        run_id,
        tmp_path / "workflow.yaml",
        {},
        "session-1",
        None,
        enable_telemetry=True,
    )

    output_value = telemetry_manager.span.attrs["output.value"]
    assert "person@example.com" not in output_value
    assert "+7 (495) 123-45-67" not in output_value
    assert "topsecret" not in output_value
    assert "[EMAIL]" in output_value
    assert "[PHONE]" in output_value


def test_workflow_thread_telemetry_warning_logs_redact_pii(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = streamlit_api.WorkflowManager(use_enhanced=False, pipelines_dir=str(tmp_path))
    run_id = "wf-telemetry-warning-pii"
    warnings = []
    raw_error = "telemetry failed person@example.com +7 (495) 123-45-67 password=topsecret"
    manager.active_runs[run_id] = {
        "run_id": run_id,
        "workflow_name": "Test Workflow",
        "status": "running",
        "start_time": streamlit_api.datetime.now(),
    }

    class WorkflowDef:
        name = "Test Workflow"

    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(lambda _path: WorkflowDef()),
    )

    class Logger:
        def info(self, *_args, **_kwargs):
            return None

        def warning(self, message, *args, **_kwargs):
            warnings.append(message % args if args else message)

    unified_logging = types.ModuleType("unified_logging")
    unified_logging.get_run_logger = lambda *_args, **_kwargs: Logger()
    unified_logging.run_id_context = lambda _run_id: contextlib.nullcontext()
    monkeypatch.setitem(sys.modules, "unified_logging", unified_logging)

    telemetry = types.ModuleType("telemetry")
    telemetry.get_telemetry_manager = lambda enabled=False: (_ for _ in ()).throw(ValueError(raw_error))
    monkeypatch.setitem(sys.modules, "telemetry", telemetry)

    def execute(self, *_args, **_kwargs):
        return types.SimpleNamespace(final_output=None)

    monkeypatch.setattr(
        manager,
        "_execute_workflow_in_context",
        types.MethodType(execute, manager),
    )

    manager._run_workflow_thread(
        run_id,
        tmp_path / "workflow.yaml",
        {},
        "session-1",
        None,
        enable_telemetry=True,
    )

    serialized = "\n".join(warnings)
    assert "person@example.com" not in serialized
    assert "+7 (495) 123-45-67" not in serialized
    assert "topsecret" not in serialized
    assert "[EMAIL]" in serialized
    assert "[PHONE]" in serialized
    assert "password=***" in serialized


def test_workflow_process_log_capture_redacts_pii(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    run_id = "wf-log-capture-pii"
    log_path = Path(__file__).resolve().parents[1] / "logs" / f"{run_id}_logs.jsonl"
    log_path.unlink(missing_ok=True)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    handler_streams = []
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler_streams.append((handler, handler.stream))
    for logger_instance in logging.Logger.manager.loggerDict.values():
        if isinstance(logger_instance, logging.Logger):
            for handler in logger_instance.handlers:
                if isinstance(handler, logging.StreamHandler):
                    handler_streams.append((handler, handler.stream))
    try:
        streamlit_api._setup_process_run_log_capture(run_id)
        print("contact person@example.com +7 (495) 123-45-67 password=topsecret")
        sys.stdout.flush()
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        for handler, stream in handler_streams:
            handler.stream = stream

    content = log_path.read_text(encoding="utf-8")
    log_path.unlink(missing_ok=True)
    assert "person@example.com" not in content
    assert "+7 (495) 123-45-67" not in content
    assert "topsecret" not in content
    assert "[EMAIL]" in content
    assert "[PHONE]" in content
    assert "password=***" in content


def test_workflow_status_parameters_redact_pii(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = streamlit_api.WorkflowManager(
        use_enhanced=False,
        pipelines_dir=str(tmp_path),
        supervisor=_QueuedWorkflowSupervisor(streamlit_api),
    )
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text("name: pii_workflow\nsteps: []\n", encoding="utf-8")

    class WorkflowDef:
        name = "pii_workflow"
        version = "1.0"
        description = ""
        steps = []
        metadata = {}
        inputs = {}
        requires_enhanced_engine = False

    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(lambda _path: WorkflowDef()),
    )

    run_id = manager.start_workflow(
        "pii_workflow",
        parameters={
            "note": "contact person@example.com +7 (495) 123-45-67",
            "password": "topsecret",
        },
        use_enhanced=False,
        run_id="wf-status-pii",
    )
    status = manager.get_workflow_status(run_id)
    serialized = json.dumps(status.parameters, ensure_ascii=False)

    assert "person@example.com" not in serialized
    assert "+7 (495) 123-45-67" not in serialized
    assert "topsecret" not in serialized
    assert "[EMAIL]" in serialized
    assert "[PHONE]" in serialized


@pytest.mark.asyncio
async def test_workflow_checkpoint_persists_secret_refs_and_restores_raw_context(tmp_path):
    workflow_pkg = _install_light_workflow_package()
    store = workflow_pkg.state_manager.SQLiteWorkflowStore(str(tmp_path / "workflow_state.db"))
    raw_dsn = "postgresql://alice:secret@example.com/app"
    checkpoint = workflow_pkg.models.WorkflowCheckpoint(
        workflow_id="wf-1",
        timestamp=workflow_pkg.state_manager.datetime.now(),
        status=workflow_pkg.models.WorkflowStatus.RUNNING,
        context=workflow_pkg.models.WorkflowContext(
            workflow_id="wf-1",
            variables={"dsn": raw_dsn, "nested": {"password": "secret"}},
        ),
        metadata={"database_url": raw_dsn},
    )

    await store.save_checkpoint(checkpoint)

    with sqlite3.connect(str(tmp_path / "workflow_state.db")) as conn:
        row = conn.execute("SELECT context, metadata FROM workflow_checkpoints WHERE workflow_id = ?", ("wf-1",)).fetchone()
    assert raw_dsn not in row[0]
    assert raw_dsn not in row[1]
    assert "__workflow_secret_ref__" in row[0]
    assert (store.secrets_path.stat().st_mode & 0o777) == 0o600

    restored = await store.get_latest_checkpoint("wf-1")

    assert restored is not None
    assert restored.context.variables["dsn"] == raw_dsn
    assert restored.context.variables["nested"]["password"] == "secret"
    assert restored.metadata["database_url"] == raw_dsn

    store.secrets_path.unlink()
    with pytest.raises(RuntimeError, match="Missing workflow checkpoint secret"):
        await store.get_latest_checkpoint("wf-1")


@pytest.mark.asyncio
async def test_workflow_checkpoint_persists_bare_odbc_connect_refs(tmp_path):
    workflow_pkg = _install_light_workflow_package()
    store = workflow_pkg.state_manager.SQLiteWorkflowStore(str(tmp_path / "workflow_state.db"))
    raw_query = "odbc_connect=DRIVER={ODBC Driver 17};SERVER=db.example.com;UID=alice;PWD=topsecret"
    checkpoint = workflow_pkg.models.WorkflowCheckpoint(
        workflow_id="wf-odbc",
        timestamp=workflow_pkg.state_manager.datetime.now(),
        status=workflow_pkg.models.WorkflowStatus.RUNNING,
        context=workflow_pkg.models.WorkflowContext(
            workflow_id="wf-odbc",
            variables={"query": raw_query},
        ),
        metadata={"connection": raw_query},
    )

    await store.save_checkpoint(checkpoint)

    with sqlite3.connect(str(tmp_path / "workflow_state.db")) as conn:
        row = conn.execute(
            "SELECT context, metadata FROM workflow_checkpoints WHERE workflow_id = ?",
            ("wf-odbc",),
        ).fetchone()
    public_text = json.dumps({"context": row[0], "metadata": row[1]}, ensure_ascii=False)
    for raw_fragment in ("UID", "PWD", "alice", "topsecret"):
        assert raw_fragment not in public_text
    assert "__workflow_secret_ref__" in public_text
    assert "odbc_connect=***" in public_text

    restored = await store.get_latest_checkpoint("wf-odbc")

    assert restored.context.variables["query"] == raw_query
    assert restored.metadata["connection"] == raw_query


@pytest.mark.asyncio
async def test_workflow_checkpoint_persists_camel_case_secret_refs(tmp_path):
    workflow_pkg = _install_light_workflow_package()
    store = workflow_pkg.state_manager.SQLiteWorkflowStore(str(tmp_path / "workflow_state.db"))
    checkpoint = workflow_pkg.models.WorkflowCheckpoint(
        workflow_id="wf-camel",
        timestamp=workflow_pkg.state_manager.datetime.now(),
        status=workflow_pkg.models.WorkflowStatus.RUNNING,
        context=workflow_pkg.models.WorkflowContext(
            workflow_id="wf-camel",
            variables={
                "clientSecret": "client-secret",
                "accessToken": "access-token",
                "nested": {
                    "dbPassword": "db-password",
                    "privateKey": "private-key",
                },
            },
        ),
    )

    await store.save_checkpoint(checkpoint)

    with sqlite3.connect(str(tmp_path / "workflow_state.db")) as conn:
        row = conn.execute(
            "SELECT context FROM workflow_checkpoints WHERE workflow_id = ?",
            ("wf-camel",),
        ).fetchone()
    public_text = row[0]
    for secret in ("client-secret", "access-token", "db-password", "private-key"):
        assert secret not in public_text
    assert "__workflow_secret_ref__" in public_text

    restored = await store.get_latest_checkpoint("wf-camel")

    assert restored.context.variables["clientSecret"] == "client-secret"
    assert restored.context.variables["accessToken"] == "access-token"
    assert restored.context.variables["nested"]["dbPassword"] == "db-password"
    assert restored.context.variables["nested"]["privateKey"] == "private-key"


@pytest.mark.asyncio
async def test_workflow_checkpoint_persists_pii_refs_and_restores_raw_context(tmp_path):
    workflow_pkg = _install_light_workflow_package()
    store = workflow_pkg.state_manager.SQLiteWorkflowStore(str(tmp_path / "workflow_state.db"))
    raw_note = "contact person@example.com +7 (495) 123-45-67"
    checkpoint = workflow_pkg.models.WorkflowCheckpoint(
        workflow_id="wf-pii",
        timestamp=workflow_pkg.state_manager.datetime.now(),
        status=workflow_pkg.models.WorkflowStatus.RUNNING,
        context=workflow_pkg.models.WorkflowContext(
            workflow_id="wf-pii",
            variables={"note": raw_note},
        ),
        metadata={"owner_note": raw_note},
    )

    await store.save_checkpoint(checkpoint)

    with sqlite3.connect(str(tmp_path / "workflow_state.db")) as conn:
        row = conn.execute(
            "SELECT context, metadata FROM workflow_checkpoints WHERE workflow_id = ?",
            ("wf-pii",),
        ).fetchone()
    public_text = json.dumps({"context": row[0], "metadata": row[1]}, ensure_ascii=False)
    assert "person@example.com" not in public_text
    assert "+7 (495) 123-45-67" not in public_text
    assert "__workflow_secret_ref__" in public_text
    assert "[EMAIL]" in public_text
    assert "[PHONE]" in public_text

    restored = await store.get_latest_checkpoint("wf-pii")

    assert restored.context.variables["note"] == raw_note
    assert restored.metadata["owner_note"] == raw_note


@pytest.mark.asyncio
async def test_workflow_checkpoint_migrates_legacy_raw_secrets(tmp_path):
    workflow_pkg = _install_light_workflow_package()
    store = workflow_pkg.state_manager.SQLiteWorkflowStore(str(tmp_path / "workflow_state.db"))
    raw_dsn = "postgresql://alice:secret@example.com/app"
    timestamp = workflow_pkg.state_manager.datetime.now().isoformat()
    legacy_context = {
        "workflow_id": "wf-legacy",
        "variables": {
            "dsn": raw_dsn,
            "driver_error": "driver failed password=secret token=abc person@example.com +7 (495) 123-45-67",
        },
    }
    legacy_metadata = {"database_url": raw_dsn}

    with sqlite3.connect(str(tmp_path / "workflow_state.db")) as conn:
        conn.execute(
            """
            INSERT INTO workflow_checkpoints (
                workflow_id, timestamp, status, current_step,
                completed_steps, failed_steps, context, step_results,
                resumable, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "wf-legacy",
                timestamp,
                workflow_pkg.models.WorkflowStatus.RUNNING.value,
                None,
                json.dumps([]),
                json.dumps([]),
                json.dumps(legacy_context),
                json.dumps({}),
                True,
                json.dumps(legacy_metadata),
            ),
        )

    restored = await store.get_latest_checkpoint("wf-legacy")

    assert restored is not None
    assert restored.context.variables["dsn"] == raw_dsn
    assert restored.context.variables["driver_error"] == (
        "driver failed password=secret token=abc person@example.com +7 (495) 123-45-67"
    )
    assert restored.metadata["database_url"] == raw_dsn
    with sqlite3.connect(str(tmp_path / "workflow_state.db")) as conn:
        row = conn.execute(
            "SELECT context, metadata FROM workflow_checkpoints WHERE workflow_id = ?",
            ("wf-legacy",),
        ).fetchone()
    public_text = json.dumps({"context": row[0], "metadata": row[1]}, ensure_ascii=False)
    assert raw_dsn not in public_text
    assert "password=secret" not in public_text
    assert "person@example.com" not in public_text
    assert "+7 (495) 123-45-67" not in public_text
    assert "[EMAIL]" in public_text
    assert "[PHONE]" in public_text
    assert "__workflow_secret_ref__" in public_text


def test_workflow_manager_accepts_explicit_run_id_contract(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    WorkflowManager = streamlit_api.WorkflowManager
    _workflow_dsn_env = streamlit_api._workflow_dsn_env

    signature = inspect.signature(WorkflowManager.start_workflow)
    assert "run_id" in signature.parameters
    assert signature.parameters["run_id"].default is None

    # Используем monkeypatch для изоляции env-переменных: cleanup гарантирован
    # даже при падении теста для всех ключей, независимо от того, были ли
    # они установлены до теста.
    _ENV_KEYS = [
        "DB_DSN",
        "DB_EXECUTOR_ROW_LIMIT",
        "TEXT_TO_SQL_DRY_RUN_ONLY",
        "TEXT_TO_SQL_SAFETY_LEVEL",
        "TEXT_TO_SQL_VALIDATE_SCHEMA",
    ]
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    with _workflow_dsn_env(
        {
            "dsn": "sqlite:///tmp/app.db",
            "max_rows": 7,
            "dry_run_only": True,
            "safety_level": "strict",
            "validate_schema": False,
        },
        workflow_name="text_to_sql_pipeline",
    ):
        assert os.environ["DB_DSN"] == "sqlite:///tmp/app.db"
        assert os.environ["DB_EXECUTOR_ROW_LIMIT"] == "7"
        assert os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] == "true"
        assert "TEXT_TO_SQL_SAFETY_LEVEL" not in os.environ
        assert os.environ["TEXT_TO_SQL_VALIDATE_SCHEMA"] == "False"


def test_workflow_dsn_env_cannot_downgrade_operator_dry_run(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "YES")

    with streamlit_api._workflow_dsn_env(
        {"dry_run_only": False},
        workflow_name="text_to_sql_pipeline",
    ):
        assert os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] == "true"

    assert os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] == "YES"


@pytest.mark.parametrize("parameters", [{}, {"dry_run_only": False}])
def test_workflow_dsn_env_rejects_invalid_operator_dry_run(
    monkeypatch,
    parameters,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "maybe")

    with pytest.raises(ValueError, match="TEXT_TO_SQL_DRY_RUN_ONLY"):
        with streamlit_api._workflow_dsn_env(
            parameters,
            workflow_name="text_to_sql_pipeline",
        ):
            pytest.fail("invalid operator configuration must fail before workflow code")

    assert os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] == "maybe"


def test_workflow_dsn_env_generic_run_ignores_unrelated_invalid_dry_run_env(
    monkeypatch,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "maybe")

    with streamlit_api._workflow_dsn_env(
        {"dry_run_only": False},
        workflow_name="generic_pipeline",
    ):
        assert os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] == "maybe"

    assert os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] == "maybe"


def test_workflow_dsn_env_service_default_is_false_without_operator_override(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    monkeypatch.delenv("TEXT_TO_SQL_DRY_RUN_ONLY", raising=False)

    with streamlit_api._workflow_dsn_env(
        {"dry_run_only": False},
        workflow_name="text_to_sql_pipeline",
    ):
        assert os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] == "false"

    assert "TEXT_TO_SQL_DRY_RUN_ONLY" not in os.environ


def test_text_to_sql_pipeline_contract_is_fail_fast_and_uses_entities():
    models = importlib.import_module("tests.workflow_test_utils").load_light_workflow_models()
    WorkflowDefinition = models.WorkflowDefinition

    workflow = WorkflowDefinition.from_yaml(Path("workflow_pipelines/text_to_sql_pipeline.yaml"))
    round_tripped = WorkflowDefinition.from_yaml_string(workflow.to_yaml_string())
    schema_step = next(step for step in workflow.steps if step.id == "schema_linking_step")
    # EPIC 6.3: god-manager sql_pipeline декомпозирован на sql_generation / sql_verification / db_audit.
    # max_rows/dry_run_only/allow_enhanced_fallback теперь живут в metadata db_audit.
    audit_step = next(step for step in workflow.steps if step.id == "db_audit")
    generation_step = next(step for step in workflow.steps if step.id == "sql_generation")
    verification_step = next(step for step in workflow.steps if step.id == "sql_verification")

    assert workflow.error_handling["on_failure"] != "continue"
    assert workflow.requires_enhanced_engine is True
    assert round_tripped.requires_enhanced_engine is True
    assert workflow.metadata["agui_entrypoint"] == "presets.text_to_sql.generate"
    assert workflow.metadata["forbid_workflows_start"] is True
    assert workflow.inputs["query"] == ""
    assert workflow.inputs["dsn"] == ""
    assert workflow.inputs["schema_scope"] is None
    assert schema_step.tool_params["entities"] == "{intent_extraction_step.entities}"
    assert schema_step.condition == "{use_schema_suggestions}"
    # EPIC 6.9: skip_output теперь использует status: "skipped_disabled" вместо disabled: true.
    schema_skip_output = schema_step.metadata["skip_output"]
    assert schema_skip_output["status"] == "skipped_disabled"
    assert schema_skip_output["decision"] == "ABSTAIN"
    assert schema_skip_output["terminal_reason_code"] == "SCHEMA_GROUNDING_FAILED"
    assert schema_skip_output["sql_generation_allowed"] is False
    assert workflow.inputs["max_rows"] == 100
    assert "dry_run_only" in workflow.inputs
    assert workflow.inputs["allow_enhanced_fallback"] is False
    assert audit_step.metadata["max_rows"] == "{max_rows}"
    assert audit_step.metadata["retryable"] is False
    assert audit_step.metadata["required_runtime_gate"] == "secure_db_executor"
    assert audit_step.metadata["dry_run_only"] == "{dry_run_only}"
    assert audit_step.metadata["allow_enhanced_fallback"] == "{allow_enhanced_fallback}"
    assert schema_step.tool_params["dsn"] == "{dsn}"
    assert schema_step.tool_params["schema_scope"] == "{schema_scope}"
    assert generation_step.metadata["dsn"] == "{dsn}"
    assert verification_step.metadata["dsn"] == "{dsn}"
    assert audit_step.metadata["dsn"] == "{dsn}"
    assert schema_step.tool_params["session_id"] == "{session_id}"
    assert "dsn={dsn}" not in generation_step.task
    assert "dsn={dsn}" not in verification_step.task
    assert "dsn={dsn}" not in audit_step.task
    assert generation_step.output_schema == "json_object"
    assert generation_step.output_schema_requirements["required"] == [
        "sql",
        "description",
    ]
    assert generation_step.condition == "{schema_linking_step.sql_generation_allowed}"
    assert generation_step.metadata["skip_output"]["sql"] == ""
    assert generation_step.metadata["skip_output"]["error"] == "schema_linking_join_failed"
    assert verification_step.output_schema_requirements["required"] == [
        "verification_status",
        "safety_check",
        "performance_check",
        "recommendations",
    ]
    assert verification_step.output_schema_requirements["properties"]["verification_status"]["enum"] == [
        "Approved",
        "Rejected",
    ]
    assert verification_step.metadata["advisory_only"] is True
    assert verification_step.metadata["required_runtime_gate"] == "db_audit"
    assert 'sql_generation_plugin(context=..., user_query=..., session_id="{session_id}")' in generation_step.task
    assert "sql_safety_check(sql_query=...)" in verification_step.task
    assert "sql_explain(sql_query=...)" in verification_step.task


def test_workflow_engine_resolves_full_dotted_variable_to_object():
    WorkflowEngine = _load_light_workflow_engine().WorkflowEngine
    WorkflowContext = sys.modules["workflow.models"].WorkflowContext
    WorkflowDefinition = sys.modules["workflow.models"].WorkflowDefinition
    WorkflowStep = sys.modules["workflow.models"].WorkflowStep

    engine = object.__new__(WorkflowEngine)
    entities = {"metrics": ["revenue"], "dimensions": ["region"], "filters": {}}
    variables = {"intent_extraction_step": {"entities": entities}}

    assert engine._substitute_variables_in_string("{intent_extraction_step.entities}", variables) is entities
    assert engine._substitute_variables_in_string("entities={intent_extraction_step.entities}", variables) == f"entities={entities}"

    context = WorkflowContext(variables={"use_schema_suggestions": False})
    step = WorkflowStep(
        id="schema_linking_step",
        task="schema linking",
        condition="{use_schema_suggestions}",
        metadata={"skip_output": {"disabled": True, "reason": "use_schema_suggestions=false"}},
    )

    assert engine._should_skip_step_by_condition(step, context) is True
    assert context.step_outputs["schema_linking_step"]["disabled"] is True
    assert context.step_outputs["schema_linking_step.disabled"] is True

    async def not_cancelled(_workflow_id):
        return False

    engine._is_workflow_cancelled = not_cancelled
    workflow = WorkflowDefinition(name="test_skip_output", steps=[step])
    results = asyncio.run(engine._execute_steps_sequential(workflow, context))
    assert results["schema_linking_step"].output["disabled"] is True


def test_enhanced_text_to_sql_fallback_requires_opt_in(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    workflow_pkg = _install_light_workflow_package()

    for module_name in [
        "workflow.policy.engine",
        "workflow.contracts.registry",
        "workflow.intelligence.planner",
        "workflow.intelligence.judge",
        "workflow.intelligence.decision",
        "workflow.intelligence.aggregator",
        "workflow.resilience.circuit_breaker",
        "workflow.resilience.retry",
        "workflow.resilience.budget",
        "workflow.resilience.loop_detection",
        "workflow.orchestration.conditions",
        "workflow.orchestration.alternatives",
        "workflow.orchestration.cache",
        "workflow.orchestration.predictor",
        "workflow.monitoring.metrics",
        "workflow.monitoring.alerts",
        "workflow.monitoring.analytics",
        "workflow.monitoring.dashboard",
    ]:
        module = types.ModuleType(module_name)
        monkeypatch.setitem(sys.modules, module_name, module)

    sys.modules["workflow.policy.engine"].PolicyEngine = object
    sys.modules["workflow.contracts.registry"].ContractRegistry = object
    sys.modules["workflow.intelligence.planner"].PreStepPlanner = object
    sys.modules["workflow.intelligence.judge"].PostStepJudge = object
    sys.modules["workflow.intelligence.decision"].DecisionEngine = object
    sys.modules["workflow.intelligence.aggregator"].FinalAggregator = object
    sys.modules["workflow.resilience.circuit_breaker"].CircuitBreakerManager = object
    sys.modules["workflow.resilience.retry"].AdaptiveRetryEngine = object
    sys.modules["workflow.resilience.retry"].JudgeRetryRequested = type(
        "JudgeRetryRequested",
        (Exception,),
        {},
    )
    sys.modules["workflow.resilience.budget"].BudgetManager = object
    sys.modules["workflow.resilience.budget"].BudgetType = object
    sys.modules["workflow.resilience.loop_detection"].LoopDetector = object
    sys.modules["workflow.orchestration.conditions"].ConditionalEngine = object
    sys.modules["workflow.orchestration.alternatives"].AlternativeExecutor = object
    sys.modules["workflow.orchestration.alternatives"].ExecutionStrategy = object
    sys.modules["workflow.orchestration.cache"].CacheManager = object
    sys.modules["workflow.orchestration.predictor"].QualityPredictor = object
    sys.modules["workflow.orchestration.predictor"].PerformanceOptimizer = object
    sys.modules["workflow.monitoring.metrics"].MetricsCollector = object
    sys.modules["workflow.monitoring.alerts"].AlertManager = object
    sys.modules["workflow.monitoring.alerts"].log_notification_handler = object()
    sys.modules["workflow.monitoring.alerts"].console_notification_handler = object()
    sys.modules["workflow.monitoring.analytics"].AnalyticsEngine = object
    sys.modules["workflow.monitoring.dashboard"].DashboardGenerator = object
    sys.modules["workflow.monitoring.dashboard"].ReportBuilder = object

    previous_enhanced_module = sys.modules.get("workflow.enhanced_engine")
    enhanced_module = _load_module("workflow.enhanced_engine", root / "workflow" / "enhanced_engine.py")
    if previous_enhanced_module is None:
        sys.modules.pop("workflow.enhanced_engine", None)
    else:
        sys.modules["workflow.enhanced_engine"] = previous_enhanced_module
    workflow_pkg.enhanced_engine = enhanced_module
    engine = object.__new__(enhanced_module.EnhancedWorkflowEngine)
    engine.feature_manager = types.SimpleNamespace(
        workflow_overrides={"text_to_sql": {"fallback_to_legacy": False}},
        global_config={"enhanced_workflow": {"fallback_to_legacy": True}},
    )
    WorkflowContext = sys.modules["workflow.models"].WorkflowContext
    WorkflowDefinition = sys.modules["workflow.models"].WorkflowDefinition
    workflow = WorkflowDefinition(name="text_to_sql_pipeline", metadata={"category": "text_to_sql"})

    assert engine._should_fallback_to_legacy(workflow, WorkflowContext(variables={})) is False
    assert engine._should_fallback_to_legacy(
        workflow,
        WorkflowContext(variables={"allow_enhanced_fallback": True}),
    ) is True


def test_workflow_manager_reads_process_mode_artifacts_from_event_store(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    manager.active_runs = {}
    raw_dsn = (
        "mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BODBC+Driver+17%7D%3B"
        "SERVER%3Ddb.example.com%3BUID%3Dalice%3BPWD%3Dtopsecret&driver=ODBC+Driver+17"
    )

    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda run_id: {
            "run_id": run_id,
            "status": "failed",
            "success": False,
            "error": f"Workflow failed steps: sql_pipeline {raw_dsn} person@example.com",
            "result": {"message": raw_dsn},
            "artifacts": {
                "final_output": {"message": raw_dsn},
                "step_outputs": {"sql_pipeline": {"sql_query": "SELECT 1", "dsn": raw_dsn}},
                "step_results": {"sql_pipeline": {"status": "failed", "note": "person@example.com"}},
                "metadata": {
                    "workflow_name": "text_to_sql_pipeline",
                    "execution": {"dry_run_only": True, "executed": False, "status": "skipped", "dsn": raw_dsn},
                    "parameters": {
                        "dry_run_only": True,
                        "schema_scope": {
                            "serialization_version": 1,
                            "tenant_id": "tenant-1",
                            "access_scope_id": "owner:alice",
                            "connection_view_id": "registry:conn-private",
                            "transient": False,
                        },
                    },
                },
            },
            "snapshot": {
                "workflow_name": "text_to_sql_pipeline",
                "parameters": {
                    "dry_run_only": True,
                    "dsn": raw_dsn,
                    "note": "person@example.com",
                    "schema_scope": {
                        "serialization_version": 1,
                        "tenant_id": "tenant-1",
                        "access_scope_id": "owner:alice",
                        "connection_view_id": "registry:conn-private",
                        "transient": False,
                    },
                },
            },
        },
    )

    status = manager.get_workflow_status("run-process")
    artifacts = manager.get_workflow_artifacts("run-process")

    assert status.status == "unknown_legacy"
    assert "Workflow failed steps: sql_pipeline" in status.error_message
    assert status.parameters["dry_run_only"] is True
    assert artifacts.step_outputs["sql_pipeline"]["sql_query"] == "SELECT 1"
    assert artifacts.metadata["execution"]["executed"] is False
    assert "schema_scope" not in status.parameters
    assert "schema_scope" not in artifacts.metadata["parameters"]
    serialized = json.dumps(
        {
            "status": status.error_message,
            "parameters": status.parameters,
            "step_results": status.step_results,
            "artifacts": artifacts.__dict__,
        },
        ensure_ascii=False,
        default=str,
    )
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


@pytest.mark.parametrize(
    ("workflow_name", "stored_status", "terminal", "expected_status"),
    [
        (
            "text_to_sql_pipeline",
            "completed",
            _terminal_contract_payload("succeeded", "stored-run"),
            "completed",
        ),
        ("text_to_sql_pipeline", "completed", {"status": "succeeded"}, "unknown_legacy"),
        (
            "generic_pipeline",
            "completed",
            _terminal_contract_payload("failed", "stored-run"),
            "completed",
        ),
    ],
)
def test_workflow_manager_stored_status_uses_exact_identity_and_strict_terminal(
    monkeypatch,
    workflow_name,
    stored_status,
    terminal,
    expected_status,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    manager.active_runs = {}
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda run_id: {
            "run_id": run_id,
            "status": stored_status,
            "terminal_outcome": terminal,
            "snapshot": {"workflow_name": workflow_name},
        },
    )

    status = manager.get_workflow_status("stored-run")

    assert status.status == expected_status
    assert status.progress_percentage == (
        100.0 if expected_status == "completed" else 0.0
    )


def test_workflow_manager_redacts_active_status_and_artifacts():
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    raw_dsn = (
        "mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BODBC+Driver+17%7D%3B"
        "SERVER%3Ddb.example.com%3BUID%3Dalice%3BPWD%3Dtopsecret&driver=ODBC+Driver+17"
    )
    run_id = "run-active-pii"
    manager.active_runs = {
        run_id: {
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {
                "dsn": raw_dsn,
                "note": "person@example.com",
                "schema_scope": {
                    "serialization_version": 1,
                    "tenant_id": "tenant-1",
                    "access_scope_id": "owner:alice",
                    "connection_view_id": "registry:conn-private",
                    "transient": False,
                },
            },
            "step_results": {"step": {"dsn": raw_dsn, "note": "person@example.com"}},
            "final_output": {"dsn": raw_dsn},
            "step_outputs": {"step": {"dsn": raw_dsn}},
            "workflow_id": "wf-1",
            "execution": {"dsn": raw_dsn},
            "error": f"failed {raw_dsn} person@example.com",
        }
    }

    status = manager.get_workflow_status(run_id)
    artifacts = manager.get_workflow_artifacts(run_id)
    snapshot = manager.get_active_run_snapshot(run_id)
    listed_snapshot = dict(manager.list_active_run_snapshots())[run_id]
    assert "schema_scope" not in status.parameters
    assert "schema_scope" not in snapshot["parameters"]
    assert "schema_scope" not in listed_snapshot["parameters"]
    serialized = json.dumps(
        {
            "parameters": status.parameters,
            "step_results": status.step_results,
            "error": status.error_message,
            "artifacts": artifacts.__dict__,
        },
        ensure_ascii=False,
        default=str,
    )

    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


@pytest.mark.parametrize("unrelated_action", ["status", "cancel"])
def test_workflow_manager_store_read_does_not_block_unrelated_run(
    monkeypatch,
    unrelated_action,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    manager._supervisor = _QueuedWorkflowSupervisor(streamlit_api)
    slow_run_id = "run-slow-store-read"
    other_run_id = f"run-unrelated-{unrelated_action}"
    manager.active_runs = {
        slow_run_id: {
            "run_id": slow_run_id,
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
        },
        other_run_id: {
            "run_id": other_run_id,
            "workflow_name": "generic_pipeline",
            "status": "completed" if unrelated_action == "status" else "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
        },
    }
    manager.run_callbacks = {}
    store_read_started = threading.Event()
    release_store_read = threading.Event()
    unrelated_finished = threading.Event()

    def read_store(run_id, **_kwargs):
        if run_id == slow_run_id:
            store_read_started.set()
            assert release_store_read.wait(2), "slow store read was not released"
        return None

    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        read_store,
    )
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: True,
    )
    manager._notify_progress = lambda *args, **kwargs: None

    slow_reader = threading.Thread(
        target=manager.get_workflow_status,
        args=(slow_run_id,),
        daemon=True,
    )
    slow_reader.start()
    assert store_read_started.wait(1), "slow store read did not start"

    def run_unrelated_action():
        if unrelated_action == "status":
            manager.get_workflow_status(other_run_id)
        else:
            manager.cancel_workflow(other_run_id)
        unrelated_finished.set()

    unrelated = threading.Thread(target=run_unrelated_action, daemon=True)
    unrelated.start()
    completed_while_store_blocked = unrelated_finished.wait(0.2)
    release_store_read.set()
    slow_reader.join(1)
    unrelated.join(1)

    assert completed_while_store_blocked


@pytest.mark.parametrize("accessor", ["status", "artifacts"])
def test_workflow_manager_stale_store_read_cannot_overwrite_newer_terminal(
    monkeypatch,
    accessor,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = f"run-stale-store-{accessor}"
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
        }
    }
    manager.run_callbacks = {}
    newer_terminal = _terminal_contract_payload("succeeded", run_id)
    stale_terminal = _terminal_contract_payload("failed", run_id)

    def read_stale_store(_run_id, **_kwargs):
        assert manager.update_active_run(
            run_id,
            {
                "status": "completed",
                "end_time": streamlit_api.datetime.now(),
                "final_output": {"winner": "runtime"},
                "terminal_outcome": newer_terminal,
            },
        )
        return {
            "run_id": run_id,
            "status": "failed",
            "error": "stale store result",
            "result": {"winner": "store"},
            "terminal_outcome": stale_terminal,
            "artifacts": {
                "final_output": {"winner": "store"},
                "terminal_outcome": stale_terminal,
            },
            "snapshot": {"workflow_name": "text_to_sql_pipeline"},
        }

    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        read_stale_store,
    )

    if accessor == "status":
        result = manager.get_workflow_status(run_id)
        assert result.status == "completed"
        assert result.terminal_outcome == newer_terminal
    else:
        result = manager.get_workflow_artifacts(run_id)
        assert result.final_output == {"winner": "runtime"}
        assert result.terminal_outcome == newer_terminal

    live = manager.get_active_run_snapshot(run_id)
    assert live["status"] == "completed"
    assert live["final_output"] == {"winner": "runtime"}
    assert live["terminal_outcome"] == newer_terminal


@pytest.mark.parametrize(
    "workflow_name",
    ["text_to_sql_pipeline", "generic_pipeline"],
)
def test_workflow_manager_same_session_invocations_get_unique_run_ids(
    monkeypatch,
    tmp_path,
    workflow_name,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    (tmp_path / "workflow.yaml").write_text(
        f"name: {workflow_name}\nsteps: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(
            lambda _path: types.SimpleNamespace(
                name=workflow_name,
                requires_enhanced_engine=False,
            )
        ),
    )
    manager = streamlit_api.WorkflowManager(
        use_enhanced=False,
        pipelines_dir=str(tmp_path),
        supervisor=_QueuedWorkflowSupervisor(streamlit_api),
    )

    first_run_id = manager.start_workflow(
        workflow_name,
        session_id="shared-session",
        use_enhanced=False,
    )
    second_run_id = manager.start_workflow(
        workflow_name,
        session_id="shared-session",
        use_enhanced=False,
    )

    assert first_run_id != second_run_id
    assert first_run_id != "shared-session"
    assert second_run_id != "shared-session"
    first = manager.get_active_run_snapshot(first_run_id)
    second = manager.get_active_run_snapshot(second_run_id)
    assert first["session_id"] == second["session_id"] == "shared-session"
    assert first["run_incarnation"]
    assert second["run_incarnation"]
    assert first["run_incarnation"] != second["run_incarnation"]


@pytest.mark.parametrize(
    "workflow_name",
    ["text_to_sql_pipeline", "generic_pipeline"],
)
def test_workflow_manager_rejects_durable_explicit_run_id_reuse_after_restart(
    monkeypatch,
    tmp_path,
    workflow_name,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    (tmp_path / "workflow.yaml").write_text(
        f"name: {workflow_name}\nsteps: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(
            lambda _path: types.SimpleNamespace(
                name=workflow_name,
                requires_enhanced_engine=False,
            )
        ),
    )
    supervisor = _QueuedWorkflowSupervisor(streamlit_api)
    run_id = f"durable-one-shot-{workflow_name}"
    manager = streamlit_api.WorkflowManager(
        use_enhanced=False,
        pipelines_dir=str(tmp_path),
        supervisor=supervisor,
    )

    assert manager.start_workflow(
        workflow_name,
        session_id="session-first",
        use_enhanced=False,
        run_id=run_id,
    ) == run_id
    with streamlit_api._GLOBAL_WORKFLOW_RUNS_LOCK:
        manager.active_runs.pop(run_id, None)
        manager.run_callbacks.pop(run_id, None)

    restarted = streamlit_api.WorkflowManager(
        use_enhanced=False,
        pipelines_dir=str(tmp_path),
        supervisor=supervisor,
    )
    with pytest.raises(ValueError, match="run_id"):
        restarted.start_workflow(
            workflow_name,
            session_id="session-second",
            use_enhanced=False,
            run_id=run_id,
        )


def test_workflow_manager_explicit_run_id_collision_is_atomic_across_managers(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    workflow_name = "generic_pipeline"
    (tmp_path / "workflow.yaml").write_text(
        f"name: {workflow_name}\nsteps: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(
            lambda _path: types.SimpleNamespace(
                name=workflow_name,
                requires_enhanced_engine=False,
            )
        ),
    )
    supervisor = _QueuedWorkflowSupervisor(streamlit_api)
    managers = [
        streamlit_api.WorkflowManager(
            use_enhanced=False,
            pipelines_dir=str(tmp_path),
            supervisor=supervisor,
        )
        for _ in range(2)
    ]
    barrier = threading.Barrier(2)
    successes = []
    errors = []

    def start(manager):
        barrier.wait()
        try:
            successes.append(
                manager.start_workflow(
                    workflow_name,
                    session_id="shared-session",
                    use_enhanced=False,
                    run_id="atomic-explicit-run",
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(manager,)) for manager in managers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)

    assert successes == ["atomic-explicit-run"]
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "run_id" in str(errors[0])
    assert len(supervisor.submissions) == 1


@pytest.mark.parametrize(
    "workflow_name",
    ["text_to_sql_pipeline", "generic_pipeline"],
)
def test_workflow_manager_stale_result_cannot_mutate_newer_incarnation(
    monkeypatch,
    tmp_path,
    workflow_name,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        f"name: {workflow_name}\nsteps: []\n",
        encoding="utf-8",
    )
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = f"stale-result-{workflow_name}"
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "run_incarnation": "new-incarnation",
            "workflow_name": workflow_name,
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "session_id": "new-session",
            "parameters": {},
        }
    }
    manager.run_callbacks = {}
    manager._engine = types.SimpleNamespace(
        execute_workflow_from_yaml=lambda *args, **kwargs: pytest.fail(
            "stale incarnation reached workflow engine"
        )
    )
    appended = []
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: appended.append((args, kwargs)) or True,
    )

    with pytest.raises(streamlit_api.WorkflowExecutionError, match="incarnation"):
        manager._execute_workflow_in_context(
            run_id,
            workflow_file,
            {},
            "old-session",
            run_incarnation="old-incarnation",
        )

    live = manager.get_active_run_snapshot(run_id)
    assert live["run_incarnation"] == "new-incarnation"
    assert live["status"] == "running"
    assert appended == []


def test_event_store_additive_migration_reserves_runs_and_deduplicates_results(
    tmp_path,
):
    db_path = tmp_path / "legacy-events.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE agui_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.commit()

    from backend.fastapi_app.agui.store import EventStore

    store = EventStore(str(db_path))
    try:
        assert store.reserve_workflow_run(
            "reserved-run",
            "incarnation-1",
            "session-1",
            "text_to_sql_pipeline",
        ) is True
        assert store.reserve_workflow_run(
            "reserved-run",
            "incarnation-2",
            "session-2",
            "text_to_sql_pipeline",
        ) is False
        payload = {
            "run_id": "reserved-run",
            "run_incarnation": "incarnation-1",
            "event_key": workflow_result_event_key(
                "reserved-run",
                "incarnation-1",
            ),
            "status": "failed",
        }
        first_seq = store.append("reserved-run", "WORKFLOW_RESULT", payload)
        second_seq = store.append("reserved-run", "WORKFLOW_RESULT", payload)
        assert first_seq == second_seq
        events = list(
            store.list_after(
                "reserved-run",
                0,
                run_incarnation="incarnation-1",
            )
        )
        assert len(events) == 1
        assert events[0].run_incarnation == "incarnation-1"
        assert events[0].event_key == payload["event_key"]
        with sqlite3.connect(db_path) as conn:
            event_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(agui_events)")
            }
            reservation_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(workflow_run_invocations)"
                )
            }
        assert {"run_incarnation", "event_key"} <= event_columns
        assert {
            "run_id",
            "run_incarnation",
            "session_id",
            "workflow_name",
        } <= reservation_columns
    finally:
        store.close()


def test_workflow_result_outbox_is_secure_bounded_and_immutable(tmp_path):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    path = tmp_path / "outbox" / "results.db"
    outbox = result_outbox.WorkflowResultOutbox(
        str(path),
        max_entries=2,
        max_payload_bytes=512,
        max_total_bytes=1024,
    )
    try:
        for index in range(2):
            run_id = f"run-{index}"
            incarnation = f"inc-{index}"
            outbox.enqueue({
                "run_id": run_id,
                "run_incarnation": incarnation,
                "event_key": workflow_result_event_key(run_id, incarnation),
                "status": "failed",
            })
        with pytest.raises(OverflowError, match="capacity"):
            outbox.enqueue({
                "run_id": "run-unrelated",
                "run_incarnation": "inc-unrelated",
                "event_key": workflow_result_event_key(
                    "run-unrelated",
                    "inc-unrelated",
                ),
                "status": "failed",
            })
        with pytest.raises(ValueError, match="identity"):
            outbox.enqueue({
                "run_id": "run-hostile",
                "run_incarnation": "inc-hostile",
                "event_key": workflow_result_event_key("run-0", "inc-0"),
                "status": "failed",
            })
        original = {
            "run_id": "run-1",
            "run_incarnation": "inc-1",
            "event_key": workflow_result_event_key("run-1", "inc-1"),
            "status": "failed",
        }
        assert outbox.enqueue(original) == original["event_key"]
        with pytest.raises(ValueError, match="different payload"):
            outbox.enqueue({**original, "error": "conflicting terminal"})
        with pytest.raises(ValueError, match="identity"):
            outbox.enqueue({
                **original,
                "event_key": workflow_result_event_key(
                    "run-1",
                    "inc-1-new",
                ),
            })
        with pytest.raises(ValueError, match="max_payload_bytes"):
            outbox.enqueue({
                "run_id": "run-1",
                "run_incarnation": "inc-1",
                "event_key": workflow_result_event_key("run-1", "inc-1"),
                "status": "failed",
                "data": "x" * 1024,
            })
        pending = outbox.list_pending()
        assert len(pending) == 2
        assert [entry.payload["event_key"] for entry in pending] == [
            workflow_result_event_key("run-0", "inc-0"),
            workflow_result_event_key("run-1", "inc-1"),
        ]
        assert (
            outbox.latest_payload("run-1", "inc-1")["event_key"]
            == workflow_result_event_key("run-1", "inc-1")
        )
        assert outbox.latest_payload("run-0", "inc-0")["event_key"] == (
            workflow_result_event_key("run-0", "inc-0")
        )
        assert outbox.count() == 2
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        outbox.close()

    first = {
        "run_id": "byte-run-1",
        "run_incarnation": "byte-inc-1",
        "event_key": workflow_result_event_key("byte-run-1", "byte-inc-1"),
        "status": "failed",
        "data": "x" * 64,
    }
    second = {
        **first,
        "run_id": "byte-run-2",
        "run_incarnation": "byte-inc-2",
        "event_key": workflow_result_event_key("byte-run-2", "byte-inc-2"),
    }
    encoded_size = len(
        json.dumps(
            first,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    total_path = tmp_path / "outbox" / "total-results.db"
    total_outbox = result_outbox.WorkflowResultOutbox(
        str(total_path),
        max_entries=4,
        max_payload_bytes=encoded_size + 16,
        max_total_bytes=(encoded_size * 2) - 1,
    )
    try:
        total_outbox.enqueue(first)
        with pytest.raises(OverflowError, match="byte capacity"):
            total_outbox.enqueue(second)
        assert total_outbox.count() == 1
        assert total_outbox.latest_payload("byte-run-1") == first
    finally:
        total_outbox.close()


def test_workflow_result_outbox_survives_primary_failure_and_replays_idempotently(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    event_path = tmp_path / "events.db"
    outbox_path = tmp_path / "outbox.db"
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
        raising=False,
    )
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    result_outbox = importlib.import_module("workflow.result_outbox")
    real_event_store = store_module.EventStore

    class ToggleEventStore:
        down = True
        append_attempts = 0

        def __init__(self, path):
            self.delegate = real_event_store(path)

        def append(self, *args, **kwargs):
            type(self).append_attempts += 1
            if type(self).down:
                raise sqlite3.OperationalError("database is temporarily locked")
            return self.delegate.append(*args, **kwargs)

        def list_after(self, *args, **kwargs):
            if type(self).down:
                raise sqlite3.OperationalError("database is temporarily locked")
            return self.delegate.list_after(*args, **kwargs)

        def close(self):
            self.delegate.close()

    monkeypatch.setattr(store_module, "EventStore", ToggleEventStore)
    run_id = "outbox-reconciliation-run"
    incarnation = "outbox-incarnation"
    reservation_store = real_event_store(str(event_path))
    try:
        assert reservation_store.reserve_workflow_run(
            run_id,
            incarnation,
            "outbox-session",
            "text_to_sql_pipeline",
        ) is True
    finally:
        reservation_store.close()
    error = "terminal WORKFLOW_RESULT could not be read"
    terminal = _terminal_contract_payload("failed", run_id)
    terminal.update({
        "reason_code": "RESULT_RECONCILIATION_FAILED",
        "error": error,
        "persistence": {"status": "error", "error": error},
    })

    resolution = streamlit_api._append_workflow_result_event(
        run_id,
        terminal,
        "failed",
        error=error,
        artifacts={
            "run_id": run_id,
            "final_output": terminal,
            "terminal_outcome": terminal,
            "metadata": {"workflow_name": "text_to_sql_pipeline"},
        },
        snapshot={"workflow_name": "text_to_sql_pipeline"},
        terminal_outcome=terminal,
        run_incarnation=incarnation,
    )
    assert resolution.persistence_succeeded is True
    assert resolution.candidate_won is True
    assert ToggleEventStore.append_attempts >= 2

    with streamlit_api._GLOBAL_WORKFLOW_RUNS_LOCK:
        streamlit_api._GLOBAL_WORKFLOW_ACTIVE_RUNS.clear()
    while_down = streamlit_api.WorkflowManager(use_enhanced=False)
    status = while_down.get_workflow_status(run_id)
    artifacts = while_down.get_workflow_artifacts(run_id)
    assert status.status == "failed"
    assert status.terminal_outcome["reason_code"] == "RESULT_RECONCILIATION_FAILED"
    assert artifacts.terminal_outcome == status.terminal_outcome
    assert artifacts.final_output == status.terminal_outcome

    ToggleEventStore.down = False
    streamlit_api.WorkflowManager(use_enhanced=False)
    streamlit_api.WorkflowManager(use_enhanced=False)

    def persisted_event_count():
        probe = real_event_store(str(event_path))
        try:
            return len(
                list(
                    probe.list_after(
                        run_id,
                        0,
                        run_incarnation=incarnation,
                    )
                )
            )
        finally:
            probe.close()

    def outbox_is_empty():
        probe = result_outbox.WorkflowResultOutbox(str(outbox_path))
        try:
            return probe.count() == 0
        finally:
            probe.close()

    assert _wait_until(
        lambda: persisted_event_count() == 1 and outbox_is_empty(),
        timeout=10,
    )
    persisted = real_event_store(str(event_path))
    try:
        events = list(
            persisted.list_after(
                run_id,
                0,
                run_incarnation=incarnation,
            )
        )
        assert len(events) == 1
        assert events[0].payload["terminal_outcome"] == terminal
    finally:
        persisted.close()

    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.count() == 0
    finally:
        outbox.close()


def _outbox_test_payload(index: int, *, prefix: str = "backlog") -> dict[str, Any]:
    suffix = f"{index:04d}"
    run_id = f"{prefix}-run-{suffix}"
    run_incarnation = f"{prefix}-inc-{suffix}"
    return {
        "run_id": run_id,
        "run_incarnation": run_incarnation,
        "event_key": workflow_result_event_key(run_id, run_incarnation),
        "status": "failed",
    }


def _review11_outbox_enqueue_worker(
    db_path: str,
    worker_id: int,
    start_event,
    result_queue,
) -> None:
    try:
        if not start_event.wait(10):
            raise RuntimeError("outbox enqueue start gate timed out")
        from workflow.result_outbox import WorkflowResultOutbox

        outbox = WorkflowResultOutbox(
            db_path,
            max_entries=32,
        )
        try:
            event_key = outbox.enqueue(
                _outbox_test_payload(worker_id, prefix="multiprocess")
            )
        finally:
            outbox.close()
        result_queue.put(("ok", worker_id, event_key))
    except Exception as exc:
        result_queue.put(("error", worker_id, type(exc).__name__, str(exc)))


def test_outbox_versioned_sequence_migration_preserves_legacy_rowid_fifo(
    tmp_path,
):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / "legacy-sequence.db"
    inserted_keys = [
        f"workflow-result:legacy-run-{index}:legacy-inc-{index}"
        for index in range(3)
    ]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE workflow_result_outbox (
                event_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL
            )
            """
        )
        for index, event_key in enumerate(inserted_keys):
            payload = {
                "event_key": event_key,
                "run_id": f"legacy-run-{index}",
                "run_incarnation": f"legacy-inc-{index}",
                "status": "failed",
            }
            payload_json = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO workflow_result_outbox
                    (event_key, run_id, run_incarnation, payload,
                     payload_bytes, created_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    payload["run_id"],
                    payload["run_incarnation"],
                    payload_json,
                    len(payload_json.encode("utf-8")),
                    1_700_000_000_000,
                ),
            )
        connection.commit()
    os.chmod(db_path, 0o600)

    outbox = result_outbox.WorkflowResultOutbox(str(db_path))
    try:
        pending = outbox.list_pending()
        assert [entry.event_key for entry in pending] == inserted_keys
        assert [entry.enqueue_seq for entry in pending] == [1, 2, 3]
        with pytest.raises(ValueError, match="v2|event_key"):
            outbox.enqueue(pending[0].payload)
        assert outbox.delete(pending[-1].event_key) is True
        replacement = _outbox_test_payload(99, prefix="post-migration")
        outbox.enqueue(replacement)
        replacement_entry = next(
            entry
            for entry in outbox.list_pending()
            if entry.event_key == replacement["event_key"]
        )
        assert replacement_entry.enqueue_seq == 4
    finally:
        outbox.close()

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            result_outbox.WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION
        )
        columns = {
            str(row[1]): int(row[5])
            for row in connection.execute(
                "PRAGMA table_info(workflow_result_outbox)"
            )
        }
        assert columns["enqueue_seq"] == 1


@pytest.mark.parametrize("corrupt_index", [0, 1])
def test_outbox_sequence_fifo_survives_corrupt_first_or_middle_row(
    monkeypatch,
    tmp_path,
    corrupt_index,
):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    monkeypatch.setattr(result_outbox.time, "time", lambda: 1_700_000_000.0)
    db_path = tmp_path / f"corrupt-sequence-{corrupt_index}.db"
    payloads = [
        {
            "event_key": workflow_result_event_key(
                f"sequence-run-{index}",
                f"sequence-inc-{index}",
            ),
            "run_id": f"sequence-run-{index}",
            "run_incarnation": f"sequence-inc-{index}",
            "status": "failed",
        }
        for index in range(3)
    ]
    outbox = result_outbox.WorkflowResultOutbox(str(db_path))
    try:
        for payload in payloads:
            outbox.enqueue(payload)
    finally:
        outbox.close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE workflow_result_outbox
            SET payload = '{', payload_bytes = 1
            WHERE event_key = ?
            """,
            (payloads[corrupt_index]["event_key"],),
        )
        connection.commit()

    outbox = result_outbox.WorkflowResultOutbox(str(db_path))
    try:
        pending = outbox.list_pending()
    finally:
        outbox.close()

    expected = [
        payload["event_key"]
        for index, payload in enumerate(payloads)
        if index != corrupt_index
    ]
    assert [entry.event_key for entry in pending] == expected
    assert [entry.enqueue_seq for entry in pending] == sorted(
        entry.enqueue_seq for entry in pending
    )


def test_outbox_multiprocess_enqueue_assigns_unique_monotonic_sequence(tmp_path):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / "multiprocess-sequence.db"
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    process_count = 12
    processes = [
        context.Process(
            target=_review11_outbox_enqueue_worker,
            args=(str(db_path), worker_id, start_event, result_queue),
        )
        for worker_id in range(process_count)
    ]
    for process in processes:
        process.start()
    start_event.set()
    results = [result_queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert all(process.exitcode == 0 for process in processes)
    worker_errors = [result for result in results if result[0] != "ok"]
    assert not worker_errors, worker_errors
    outbox = result_outbox.WorkflowResultOutbox(
        str(db_path),
        max_entries=32,
    )
    try:
        pending = outbox.list_pending()
    finally:
        outbox.close()
    sequences = [entry.enqueue_seq for entry in pending]
    assert sequences == list(range(1, process_count + 1))
    assert len({entry.event_key for entry in pending}) == process_count


@pytest.mark.parametrize(
    "malformation",
    ["missing_pk", "missing_autoincrement", "missing_unique", "partial_unique"],
)
def test_outbox_rejects_malformed_claimed_v2_schema(tmp_path, malformation):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / f"malformed-v2-{malformation}.db"
    enqueue_seq = (
        "enqueue_seq INTEGER NOT NULL"
        if malformation == "missing_pk"
        else (
            "enqueue_seq INTEGER PRIMARY KEY"
            if malformation == "missing_autoincrement"
            else "enqueue_seq INTEGER PRIMARY KEY AUTOINCREMENT"
        )
    )
    event_key = (
        "event_key TEXT NOT NULL"
        if malformation in {"missing_unique", "partial_unique"}
        else "event_key TEXT NOT NULL UNIQUE"
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"""
            CREATE TABLE workflow_result_outbox (
                {enqueue_seq},
                {event_key},
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL
            )
            """
        )
        if malformation == "partial_unique":
            connection.execute(
                """
                CREATE UNIQUE INDEX partial_event_key
                ON workflow_result_outbox(event_key)
                WHERE event_key <> ''
                """
            )
        connection.execute(
            f"PRAGMA user_version="
            f"{result_outbox.WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION}"
        )
        connection.commit()
    os.chmod(db_path, 0o600)

    with pytest.raises(RuntimeError, match="schema"):
        result_outbox.WorkflowResultOutbox(str(db_path))


def _create_review11_claimed_v2_outbox(connection, result_outbox):
    connection.execute(
        """
        CREATE TABLE workflow_result_outbox (
            enqueue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            run_incarnation TEXT NOT NULL,
            payload TEXT NOT NULL,
            payload_bytes INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        f"PRAGMA user_version="
        f"{result_outbox.WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION}"
    )


def test_outbox_rejects_autoincrement_sql_substring_spoof(tmp_path):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / "autoincrement-spoof.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE workflow_result_outbox (
                enqueue_seq INTEGER PRIMARY KEY,
                event_key TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL,
                CHECK(length(
                    'ENQUEUE_SEQ INTEGER PRIMARY KEY AUTOINCREMENT'
                ) > 0)
            ) WITHOUT ROWID
            """
        )
        connection.execute(
            f"PRAGMA user_version="
            f"{result_outbox.WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION}"
        )
        connection.commit()
    os.chmod(db_path, 0o600)

    with pytest.raises(RuntimeError, match="schema"):
        result_outbox.WorkflowResultOutbox(str(db_path))


def test_outbox_rejects_nocase_event_key_uniqueness(tmp_path):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / "nocase-event-key.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE workflow_result_outbox (
                enqueue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT COLLATE NOCASE NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            f"PRAGMA user_version="
            f"{result_outbox.WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION}"
        )
        connection.commit()
    os.chmod(db_path, 0o600)

    with pytest.raises(RuntimeError, match="schema"):
        result_outbox.WorkflowResultOutbox(str(db_path))


def test_outbox_rejects_wrong_same_name_incarnation_index(tmp_path):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / "wrong-incarnation-index.db"
    result_outbox.WorkflowResultOutbox(str(db_path)).close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DROP INDEX idx_workflow_result_outbox_incarnation"
        )
        connection.execute(
            """
            CREATE INDEX idx_workflow_result_outbox_incarnation
            ON workflow_result_outbox(created_at_ms)
            """
        )
        connection.commit()
    os.chmod(db_path, 0o600)

    with pytest.raises(RuntimeError, match="index"):
        result_outbox.WorkflowResultOutbox(str(db_path))


def test_outbox_rejects_malformed_dlq_without_data_loss(tmp_path):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / "malformed-dlq.db"
    result_outbox.WorkflowResultOutbox(str(db_path)).close()
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE workflow_result_outbox_dead_letter")
        connection.execute(
            """
            CREATE TABLE workflow_result_outbox_dead_letter (
                dead_letter_id TEXT,
                event_key TEXT,
                reason_code TEXT,
                metadata_json TEXT,
                quarantined_at_ms INTEGER
            )
            """
        )
        connection.execute(
            """
            INSERT INTO workflow_result_outbox_dead_letter
            VALUES ('legacy-id', 'event', 'reason', '{}', 1)
            """
        )
        connection.commit()
    os.chmod(db_path, 0o600)

    with pytest.raises(RuntimeError, match="dead-letter schema"):
        result_outbox.WorkflowResultOutbox(str(db_path))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_result_outbox_dead_letter"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("identity_field", "identity_value"),
    [
        ("event_key", " event-key "),
        ("event_key", 123),
        ("run_id", " run-id "),
        ("run_id", 123),
        ("run_incarnation", " incarnation "),
        ("run_incarnation", 123),
    ],
)
def test_outbox_enqueue_rejects_noncanonical_identity_without_persisting(
    tmp_path,
    identity_field,
    identity_value,
):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / f"invalid-identity-{identity_field}.db"
    payload = {
        "event_key": workflow_result_event_key("run-id", "incarnation"),
        "run_id": "run-id",
        "run_incarnation": "incarnation",
        identity_field: identity_value,
    }
    outbox = result_outbox.WorkflowResultOutbox(str(db_path))
    try:
        with pytest.raises(
            (TypeError, ValueError),
            match="identity|requires|canonical",
        ):
            outbox.enqueue(payload)
        assert outbox.count() == 0
    finally:
        outbox.close()


def test_outbox_quarantines_nonfinite_huge_and_deep_json_before_valid_fifo(
    tmp_path,
):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / "adversarial-json.db"
    outbox = result_outbox.WorkflowResultOutbox(str(db_path))
    outbox.close()
    corrupt_rows = [
        (
            "nonfinite-event",
            "nonfinite-run",
            "nonfinite-inc",
            '{"event_key":"nonfinite-event","run_id":"nonfinite-run",'
            '"run_incarnation":"nonfinite-inc","value":NaN}',
        ),
        (
            "huge-int-event",
            "huge-int-run",
            "huge-int-inc",
            '{"event_key":"huge-int-event","run_id":"huge-int-run",'
            '"run_incarnation":"huge-int-inc","value":'
            + ("9" * 5000)
            + "}",
        ),
        (
            "deep-event",
            "deep-run",
            "deep-inc",
            '{"event_key":"deep-event","run_id":"deep-run",'
            '"run_incarnation":"deep-inc","value":'
            + ("[" * 10_000)
            + "0"
            + ("]" * 10_000)
            + "}",
        ),
    ]
    with sqlite3.connect(db_path) as connection:
        for event_key, run_id, incarnation, payload_json in corrupt_rows:
            connection.execute(
                """
                INSERT INTO workflow_result_outbox
                    (event_key, run_id, run_incarnation, payload,
                     payload_bytes, created_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    run_id,
                    incarnation,
                    payload_json,
                    len(payload_json.encode("utf-8")),
                    1,
                ),
            )
        connection.commit()
    valid = _outbox_test_payload(1, prefix="after-adversarial-json")
    outbox = result_outbox.WorkflowResultOutbox(str(db_path))
    try:
        outbox.enqueue(valid)
        pending = outbox.list_pending()
    finally:
        outbox.close()

    assert [entry.event_key for entry in pending] == [valid["event_key"]]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_result_outbox_dead_letter"
        ).fetchone() == (3,)


def test_outbox_idempotent_retry_uses_canonical_stored_payload_semantics(
    tmp_path,
):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / "noncanonical-idempotent.db"
    payload = _outbox_test_payload(1, prefix="noncanonical-idempotent")
    outbox = result_outbox.WorkflowResultOutbox(str(db_path))
    try:
        outbox.enqueue(payload)
        original_seq = outbox.list_pending()[0].enqueue_seq
    finally:
        outbox.close()
    reformatted = json.dumps(payload, ensure_ascii=False, indent=2)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE workflow_result_outbox
            SET payload = ?, payload_bytes = ?
            WHERE event_key = ?
            """,
            (
                reformatted,
                len(reformatted.encode("utf-8")),
                payload["event_key"],
            ),
        )
        connection.commit()

    outbox = result_outbox.WorkflowResultOutbox(str(db_path))
    try:
        assert outbox.enqueue(payload) == payload["event_key"]
        pending = outbox.list_pending()
        assert len(pending) == 1
        assert pending[0].enqueue_seq == original_seq
        assert pending[0].payload == payload
    finally:
        outbox.close()


def test_outbox_idempotent_retry_rejects_corrupt_existing_metadata(tmp_path):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / "corrupt-idempotent-metadata.db"
    payload = _outbox_test_payload(1, prefix="corrupt-idempotent")
    outbox = result_outbox.WorkflowResultOutbox(str(db_path))
    try:
        outbox.enqueue(payload)
    finally:
        outbox.close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE workflow_result_outbox SET payload_bytes = -1"
        )
        connection.commit()

    outbox = result_outbox.WorkflowResultOutbox(str(db_path))
    try:
        with pytest.raises(ValueError, match="corrupt stored row"):
            outbox.enqueue(payload)
        assert outbox.count() == 1
        assert outbox.list_pending() == []
        assert outbox.count() == 0
    finally:
        outbox.close()


@pytest.mark.parametrize("schema_object", ["unique_index", "trigger"])
def test_outbox_rejects_unexpected_semantic_schema_objects(
    tmp_path,
    schema_object,
):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / f"unexpected-{schema_object}.db"
    with sqlite3.connect(db_path) as connection:
        _create_review11_claimed_v2_outbox(connection, result_outbox)
        if schema_object == "unique_index":
            connection.execute(
                """
                CREATE UNIQUE INDEX unexpected_unique_run
                ON workflow_result_outbox(run_id)
                """
            )
        else:
            connection.execute(
                """
                CREATE TRIGGER unexpected_delete_after_insert
                AFTER INSERT ON workflow_result_outbox
                BEGIN
                    DELETE FROM workflow_result_outbox
                    WHERE enqueue_seq = NEW.enqueue_seq;
                END
                """
            )
        connection.commit()
    os.chmod(db_path, 0o600)

    with pytest.raises(RuntimeError, match="schema|index|trigger"):
        result_outbox.WorkflowResultOutbox(str(db_path))


def test_outbox_capacity_uses_actual_payload_size_after_metadata_corruption(
    tmp_path,
):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    db_path = tmp_path / "corrupt-capacity.db"
    first = _outbox_test_payload(1, prefix="actual-capacity")
    second = _outbox_test_payload(2, prefix="actual-capacity")

    def encoded_size(payload):
        return len(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )

    max_total_bytes = encoded_size(first) + encoded_size(second) - 1
    outbox = result_outbox.WorkflowResultOutbox(
        str(db_path),
        max_payload_bytes=max_total_bytes,
        max_total_bytes=max_total_bytes,
    )
    try:
        outbox.enqueue(first)
    finally:
        outbox.close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE workflow_result_outbox SET payload_bytes = -100000"
        )
        connection.commit()

    outbox = result_outbox.WorkflowResultOutbox(
        str(db_path),
        max_payload_bytes=max_total_bytes,
        max_total_bytes=max_total_bytes,
    )
    try:
        with pytest.raises(OverflowError, match="byte capacity"):
            outbox.enqueue(second)
        assert outbox.count() == 1
    finally:
        outbox.close()


def test_outbox_drain_stale_ack_never_deletes_replacement(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    result_outbox = importlib.import_module("workflow.result_outbox")
    outbox_path = tmp_path / "stale-ack.db"
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
    )
    original = _outbox_test_payload(1, prefix="stale-ack-original")
    replacement = {
        **original,
        "status": "replacement",
    }
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(original)
    finally:
        outbox.close()

    def replace_before_ack(_payload):
        peer = result_outbox.WorkflowResultOutbox(str(outbox_path))
        try:
            assert peer.delete(original["event_key"]) is True
            peer.enqueue(replacement)
        finally:
            peer.close()

    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_payload_to_primary",
        replace_before_ack,
    )

    assert streamlit_api._drain_workflow_result_outbox(limit=1) == 0
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.count() == 1
        assert outbox.latest_payload(replacement["run_id"]) == replacement
    finally:
        outbox.close()


def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_workflow_result_payload_uses_shared_canonical_event_key(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    identity_module = importlib.import_module("workflow.result_identity")
    calls = []

    real_canonical_key = identity_module.workflow_result_event_key

    def canonical_key(run_id, run_incarnation):
        calls.append((run_id, run_incarnation))
        return real_canonical_key(run_id, run_incarnation)

    monkeypatch.setattr(identity_module, "workflow_result_event_key", canonical_key)

    payload = streamlit_api._build_workflow_result_event_payload(
        "canonical-run",
        {},
        "failed",
        run_incarnation="canonical-incarnation",
    )

    assert calls
    assert set(calls) == {("canonical-run", "canonical-incarnation")}
    assert payload["event_key"] == workflow_result_event_key(
        "canonical-run",
        "canonical-incarnation",
    )


def test_workflow_result_payload_preserves_validated_identity_only(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    run_id = "run-immediate-cancel"
    incarnation = "agui-fc373f933a00425885e6624119768858"
    event_key = workflow_result_event_key(run_id, incarnation)
    transformed_values = {run_id, incarnation, event_key, "content-sentinel"}

    def neutral_public_transform(value):
        if isinstance(value, dict):
            return {
                key: neutral_public_transform(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [neutral_public_transform(item) for item in value]
        if isinstance(value, str) and value in transformed_values:
            return f"public::{value}"
        return value

    monkeypatch.setattr(
        streamlit_api,
        "redact_pii_in_payload",
        neutral_public_transform,
    )
    terminal = _terminal_contract_payload("failed", run_id)
    terminal["error"] = "content-sentinel"

    payload = streamlit_api._build_workflow_result_event_payload(
        run_id,
        dict(terminal),
        "failed",
        error="content-sentinel",
        artifacts={
            "terminal_outcome": dict(terminal),
            "final_output": dict(terminal),
        },
        snapshot={
            "workflow_name": "text_to_sql_pipeline",
            "payload_value": "content-sentinel",
        },
        terminal_outcome=terminal,
        run_incarnation=incarnation,
    )

    identity = parse_workflow_result_event_key(payload["event_key"])
    assert identity.run_id == run_id
    assert identity.run_incarnation == incarnation
    assert payload["run_id"] == run_id
    assert payload["thread_id"] == run_id
    assert payload["run_incarnation"] == incarnation
    assert payload["snapshot"]["run_incarnation"] == incarnation
    assert payload["terminal_outcome"]["run_id"] == run_id
    assert payload["result"]["run_id"] == run_id
    assert payload["artifacts"]["terminal_outcome"]["run_id"] == run_id
    assert payload["artifacts"]["final_output"]["run_id"] == run_id
    assert payload["snapshot"]["payload_value"] == "public::content-sentinel"


@pytest.mark.parametrize(
    "terminal_location",
    ["top", "result", "artifacts.terminal_outcome", "artifacts.final_output"],
)
def test_workflow_result_payload_rejects_terminal_copy_run_id_mismatch(
    terminal_location,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    run_id = "outer-run"
    terminal = _terminal_contract_payload("failed", run_id)
    mismatched = _terminal_contract_payload("failed", "other-run")
    result = dict(terminal)
    artifacts = {
        "terminal_outcome": dict(terminal),
        "final_output": dict(terminal),
    }
    top_terminal = terminal
    if terminal_location == "top":
        top_terminal = mismatched
    elif terminal_location == "result":
        result = mismatched
    elif terminal_location == "artifacts.terminal_outcome":
        artifacts["terminal_outcome"] = mismatched
    else:
        artifacts["final_output"] = mismatched

    with pytest.raises(ValueError, match="run_id"):
        streamlit_api._build_workflow_result_event_payload(
            run_id,
            result,
            "failed",
            artifacts=artifacts,
            snapshot={"workflow_name": "text_to_sql_pipeline"},
            terminal_outcome=top_terminal,
            run_incarnation="canonical-incarnation",
        )


@pytest.mark.parametrize(
    ("terminal_location", "loser_status"),
    [
        ("result", "cancelled"),
        ("result", "succeeded"),
        ("artifacts.terminal_outcome", "cancelled"),
        ("artifacts.terminal_outcome", "succeeded"),
        ("artifacts.final_output", "cancelled"),
        ("artifacts.final_output", "succeeded"),
    ],
)
def test_workflow_result_payload_rejects_contradictory_terminal_copy(
    terminal_location,
    loser_status,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    run_id = "coherent-terminal-run"
    winner = _terminal_contract_payload("failed", run_id)
    loser = _terminal_contract_payload(loser_status, run_id)
    result = dict(winner)
    artifacts = {
        "terminal_outcome": dict(winner),
        "final_output": dict(winner),
    }
    if terminal_location == "result":
        result = loser
    elif terminal_location == "artifacts.terminal_outcome":
        artifacts["terminal_outcome"] = loser
    else:
        artifacts["final_output"] = loser

    with pytest.raises(ValueError, match="terminal_outcome"):
        streamlit_api._build_workflow_result_event_payload(
            run_id,
            result,
            "failed",
            artifacts=artifacts,
            snapshot={"workflow_name": "text_to_sql_pipeline"},
            terminal_outcome=winner,
            run_incarnation="canonical-incarnation",
        )


def test_workflow_result_payload_rejects_terminal_copy_without_authority():
    streamlit_api = _load_light_workflow_streamlit_api()
    run_id = "missing-terminal-authority"

    with pytest.raises(ValueError, match="terminal_outcome"):
        streamlit_api._build_workflow_result_event_payload(
            run_id,
            _terminal_contract_payload("failed", run_id),
            "failed",
            snapshot={"workflow_name": "text_to_sql_pipeline"},
            run_incarnation="canonical-incarnation",
        )


def test_generic_workflow_preserves_terminal_shaped_result_content():
    streamlit_api = _load_light_workflow_streamlit_api()
    generic_result = _terminal_contract_payload("failed", "generic-content-run")

    payload = streamlit_api._build_workflow_result_event_payload(
        "outer-run",
        generic_result,
        "failed",
        snapshot={"workflow_name": "generic_pipeline"},
        run_incarnation="generic-incarnation",
    )

    assert payload["result"] == generic_result


@pytest.mark.parametrize(
    ("terminal_location", "loser_status"),
    [
        ("result", "cancelled"),
        ("result", "succeeded"),
        ("artifacts.terminal_outcome", "cancelled"),
        ("artifacts.terminal_outcome", "succeeded"),
        ("artifacts.final_output", "cancelled"),
        ("artifacts.final_output", "succeeded"),
    ],
)
def test_event_store_rejects_contradictory_terminal_copy(
    tmp_path,
    terminal_location,
    loser_status,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    auth_module = importlib.import_module("backend.fastapi_app.agui.auth")
    run_id = "store-terminal-coherence"
    incarnation = "store-terminal-incarnation"
    winner = _terminal_contract_payload("failed", run_id)
    loser = _terminal_contract_payload(loser_status, run_id)
    payload = streamlit_api._build_workflow_result_event_payload(
        run_id,
        winner,
        "failed",
        artifacts={
            "terminal_outcome": winner,
            "final_output": winner,
        },
        snapshot={"workflow_name": "text_to_sql_pipeline"},
        terminal_outcome=winner,
        run_incarnation=incarnation,
    )
    if terminal_location == "result":
        payload["result"] = loser
    elif terminal_location == "artifacts.terminal_outcome":
        payload["artifacts"]["terminal_outcome"] = loser
    else:
        payload["artifacts"]["final_output"] = loser

    store = store_module.EventStore(str(tmp_path / "events.db"))
    try:
        store.create_run(
            run_id,
            f"thread-{run_id}",
            auth_module.Principal(subject="owner", tenant_id="tenant"),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            run_id,
            incarnation,
            f"thread-{run_id}",
            "text_to_sql_pipeline",
        )

        with pytest.raises(ValueError, match="terminal_outcome"):
            store.append(run_id, "WORKFLOW_RESULT", payload)

        stored = store.get_run(run_id)
        assert stored is not None
        assert stored.status == "pending"
        assert stored.result_seq is None
        assert list(store.list_after(run_id, 0)) == []
    finally:
        store.close()


def test_outbox_retains_contradictory_terminal_copy(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    result_outbox = importlib.import_module("workflow.result_outbox")
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    auth_module = importlib.import_module("backend.fastapi_app.agui.auth")
    event_path = tmp_path / "outbox-coherence-events.db"
    outbox_path = tmp_path / "outbox-coherence.db"
    run_id = "outbox-terminal-coherence"
    incarnation = "outbox-terminal-incarnation"
    winner = _terminal_contract_payload("failed", run_id)
    payload = streamlit_api._build_workflow_result_event_payload(
        run_id,
        winner,
        "failed",
        artifacts={
            "terminal_outcome": winner,
            "final_output": winner,
        },
        snapshot={"workflow_name": "text_to_sql_pipeline"},
        terminal_outcome=winner,
        run_incarnation=incarnation,
    )
    payload["artifacts"]["final_output"] = _terminal_contract_payload(
        "succeeded",
        run_id,
    )
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)

    store = store_module.EventStore(str(event_path))
    try:
        store.create_run(
            run_id,
            f"thread-{run_id}",
            auth_module.Principal(subject="owner", tenant_id="tenant"),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            run_id,
            incarnation,
            f"thread-{run_id}",
            "text_to_sql_pipeline",
        )
    finally:
        store.close()

    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(payload)
    finally:
        outbox.close()

    delivered, retryable = streamlit_api._drain_workflow_result_outbox_batch(
        path=outbox_path,
        time_budget_seconds=1,
    )
    assert delivered == 0
    assert retryable is False
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.count() == 1
    finally:
        outbox.close()
    store = store_module.EventStore(str(event_path))
    try:
        stored = store.get_run(run_id)
        assert stored is not None
        assert stored.status == "pending"
        assert stored.result_seq is None
        assert list(store.list_after(run_id, 0)) == []
    finally:
        store.close()


@pytest.mark.parametrize(
    "corruption",
    ["event_key", "snapshot_incarnation", "thread_id"],
)
def test_workflow_result_payload_identity_restore_fails_closed(corruption):
    streamlit_api = _load_light_workflow_streamlit_api()
    payload = streamlit_api._build_workflow_result_event_payload(
        "outer-run",
        {"message": "generic"},
        "failed",
        run_incarnation="canonical-incarnation",
    )
    if corruption == "event_key":
        payload["event_key"] = workflow_result_event_key(
            "outer-run",
            "other-incarnation",
        )
    elif corruption == "snapshot_incarnation":
        payload["snapshot"]["run_incarnation"] = "other-incarnation"
    else:
        payload["thread_id"] = " noncanonical-thread "

    with pytest.raises(ValueError, match="event_key|incarnation|thread_id"):
        streamlit_api._transform_workflow_result_event_payload(payload)


def test_generic_result_run_id_is_not_exempted_from_public_transform(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    generic_run_id = "generic-content-run-id"

    def neutral_public_transform(value):
        if isinstance(value, dict):
            return {
                key: neutral_public_transform(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [neutral_public_transform(item) for item in value]
        if value in {generic_run_id, "content-sentinel"}:
            return f"public::{value}"
        return value

    monkeypatch.setattr(
        streamlit_api,
        "redact_pii_in_payload",
        neutral_public_transform,
    )

    payload = streamlit_api._build_workflow_result_event_payload(
        "outer-run",
        {"run_id": generic_run_id, "payload_value": "content-sentinel"},
        "failed",
        run_incarnation="canonical-incarnation",
    )

    assert payload["result"]["run_id"] == f"public::{generic_run_id}"
    assert payload["result"]["payload_value"] == "public::content-sentinel"


def test_workflow_result_outbox_scheduler_coalesces_per_path(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    started = []
    monkeypatch.setattr(
        streamlit_api,
        "_start_scheduled_workflow_result_outbox_drain",
        lambda **kwargs: started.append(kwargs),
    )
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"

    try:
        assert streamlit_api._schedule_workflow_result_outbox_drain(
            path=first_path
        ) is True
        assert streamlit_api._schedule_workflow_result_outbox_drain(
            path=first_path
        ) is False
        assert streamlit_api._schedule_workflow_result_outbox_drain(
            path=second_path
        ) is True
        assert [call["path"] for call in started] == [first_path, second_path]
    finally:
        streamlit_api._reset_outbox_drain_scheduler_after_fork()


@pytest.mark.parametrize("failure_stage", ["open", "list"])
def test_outbox_scheduler_retries_transient_failure_without_new_trigger(
    monkeypatch,
    tmp_path,
    failure_stage,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    result_outbox = importlib.import_module("workflow.result_outbox")
    outbox_path = tmp_path / f"transient-{failure_stage}.db"
    payload = _outbox_test_payload(1, prefix=f"transient-{failure_stage}")
    real_outbox = result_outbox.WorkflowResultOutbox
    outbox = real_outbox(str(outbox_path))
    try:
        outbox.enqueue(payload)
    finally:
        outbox.close()
    failures_remaining = 1

    class TransientOutbox:
        def __init__(self, *args, **kwargs):
            nonlocal failures_remaining
            if failure_stage == "open" and failures_remaining:
                failures_remaining -= 1
                raise sqlite3.OperationalError("database is temporarily locked")
            self.delegate = real_outbox(*args, **kwargs)

        def list_pending(self, *args, **kwargs):
            nonlocal failures_remaining
            if failure_stage == "list" and failures_remaining:
                failures_remaining -= 1
                raise sqlite3.OperationalError("database is temporarily locked")
            return self.delegate.list_pending(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.delegate, name)

    delivered = []
    monkeypatch.setattr(result_outbox, "WorkflowResultOutbox", TransientOutbox)
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_payload_to_primary",
        lambda queued: delivered.append(queued["event_key"]),
    )
    try:
        assert streamlit_api._schedule_workflow_result_outbox_drain(
            path=outbox_path
        ) is True
        assert _wait_until(
            lambda: delivered == [payload["event_key"]],
            timeout=3,
        )
        probe = real_outbox(str(outbox_path))
        try:
            assert probe.count() == 0
        finally:
            probe.close()
        assert failures_remaining == 0
    finally:
        streamlit_api._reset_outbox_drain_scheduler_after_fork()


@pytest.mark.parametrize("backlog_size", [101, 512])
def test_workflow_result_outbox_constructor_schedules_bounded_drain_to_exhaustion(
    monkeypatch,
    tmp_path,
    backlog_size,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    result_outbox = importlib.import_module("workflow.result_outbox")
    outbox_path = tmp_path / f"outbox-{backlog_size}.db"
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
    )
    outbox = result_outbox.WorkflowResultOutbox(
        str(outbox_path),
        max_entries=backlog_size,
    )
    try:
        for index in range(backlog_size):
            outbox.enqueue(_outbox_test_payload(index))
    finally:
        outbox.close()

    delivered = []
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_payload_to_primary",
        lambda payload: delivered.append(payload["event_key"]),
    )

    streamlit_api.WorkflowManager(use_enhanced=False)

    def outbox_is_empty():
        probe = result_outbox.WorkflowResultOutbox(
            str(outbox_path),
            max_entries=backlog_size,
        )
        try:
            return probe.count() == 0
        finally:
            probe.close()

    assert _wait_until(outbox_is_empty, timeout=10)
    assert delivered == [
        _outbox_test_payload(index)["event_key"]
        for index in range(backlog_size)
    ]


def test_workflow_manager_constructor_never_waits_for_outbox_io(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    result_outbox = importlib.import_module("workflow.result_outbox")
    outbox_path = tmp_path / "constructor-nonblocking.db"
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(_outbox_test_payload(0, prefix="constructor"))
    finally:
        outbox.close()
    release = threading.Event()

    def slow_path_resolution():
        release.wait(1)
        return outbox_path

    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        slow_path_resolution,
    )
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_payload_to_primary",
        lambda _payload: None,
    )
    started = time.monotonic()
    streamlit_api.WorkflowManager(use_enhanced=False)
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.2

    def outbox_is_empty():
        probe = result_outbox.WorkflowResultOutbox(str(outbox_path))
        try:
            return probe.count() == 0
        finally:
            probe.close()

    assert _wait_until(outbox_is_empty)


def test_outbox_recovers_live_primary_without_constructing_another_manager(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    event_path = tmp_path / "live-events.db"
    outbox_path = tmp_path / "live-outbox.db"
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
    )
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    real_event_store = store_module.EventStore

    class ToggleEventStore:
        down = True

        def __init__(self, path):
            self.delegate = real_event_store(path)

        def append(self, *args, **kwargs):
            if type(self).down:
                raise sqlite3.OperationalError("database is temporarily locked")
            return self.delegate.append(*args, **kwargs)

        def list_after(self, *args, **kwargs):
            if type(self).down:
                raise sqlite3.OperationalError("database is temporarily locked")
            return self.delegate.list_after(*args, **kwargs)

        def close(self):
            self.delegate.close()

    monkeypatch.setattr(store_module, "EventStore", ToggleEventStore)
    run_id = "live-recovery-run"
    incarnation = "live-recovery-inc"
    reservation_store = real_event_store(str(event_path))
    try:
        assert reservation_store.reserve_workflow_run(
            run_id,
            incarnation,
            "s",
            "text_to_sql_pipeline",
        ) is True
    finally:
        reservation_store.close()
    terminal = _terminal_contract_payload("failed", run_id)
    resolution = streamlit_api._append_workflow_result_event(
        run_id,
        terminal,
        "failed",
        error="failed",
        artifacts={"final_output": terminal, "terminal_outcome": terminal},
        snapshot={"workflow_name": "text_to_sql_pipeline", "session_id": "s"},
        terminal_outcome=terminal,
        run_incarnation=incarnation,
    )
    assert resolution.persistence_succeeded is True
    assert resolution.candidate_won is True

    manager = streamlit_api.WorkflowManager(use_enhanced=False)
    ToggleEventStore.down = False

    def result_is_durable():
        store = real_event_store(str(event_path))
        try:
            return len(
                list(store.list_after(run_id, 0, run_incarnation=incarnation))
            ) == 1
        finally:
            store.close()

    assert _wait_until(result_is_durable, timeout=10)
    status = manager.get_workflow_status(run_id)
    artifacts = manager.get_workflow_artifacts(run_id)
    assert status.status == "failed"
    assert status.terminal_outcome == terminal
    assert artifacts.final_output == terminal


def test_event_store_identity_collision_never_acknowledges_outbox_row(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    result_outbox = importlib.import_module("workflow.result_outbox")
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    event_path = tmp_path / "collision-events.db"
    outbox_path = tmp_path / "collision-outbox.db"
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
    )
    event_key = workflow_result_event_key("collision-run", "original-inc")
    primary_payload = {
        "run_id": "collision-run",
        "run_incarnation": "original-inc",
        "event_key": event_key,
        "status": "failed",
    }
    primary = store_module.EventStore(str(event_path))
    try:
        assert primary.reserve_workflow_run(
            "collision-run",
            "original-inc",
            "collision-session",
            "text_to_sql_pipeline",
        ) is True
        primary.append("collision-run", "WORKFLOW_RESULT", primary_payload)
    finally:
        primary.close()
    hostile_payload = {
        **primary_payload,
        "error": "conflicting terminal",
    }
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(hostile_payload)
    finally:
        outbox.close()

    streamlit_api._drain_workflow_result_outbox(limit=100)

    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.count() == 1
        assert outbox.latest_payload("collision-run") == hostile_payload
    finally:
        outbox.close()


def _seed_review11_primary_and_outbox(
    monkeypatch,
    tmp_path,
    mismatch: str | None,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    result_outbox = importlib.import_module("workflow.result_outbox")
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    event_path = tmp_path / f"reconcile-{mismatch or 'exact'}-events.db"
    outbox_path = tmp_path / f"reconcile-{mismatch or 'exact'}-outbox.db"
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
    )
    monkeypatch.setattr(
        streamlit_api,
        "_schedule_workflow_result_outbox_drain",
        lambda **_kwargs: False,
    )
    run_id = "review11-reconcile-run"
    incarnation = "review11-reconcile-inc"
    terminal = _terminal_contract_payload("failed", run_id)
    primary_payload = streamlit_api._build_workflow_result_event_payload(
        run_id,
        terminal,
        "failed",
        error="primary terminal",
        artifacts={"terminal_outcome": terminal, "final_output": terminal},
        snapshot={
            "workflow_name": "text_to_sql_pipeline",
            "session_id": "review11-session",
        },
        terminal_outcome=terminal,
        run_incarnation=incarnation,
    )
    primary_event_type = "OTHER_EVENT" if mismatch == "event_type" else "WORKFLOW_RESULT"
    primary = store_module.EventStore(str(event_path))
    try:
        assert primary.reserve_workflow_run(
            run_id,
            incarnation,
            "review11-session",
            "text_to_sql_pipeline",
        ) is True
        if mismatch == "event_type":
            with primary._lock:
                primary._conn.execute(
                    """
                    INSERT INTO agui_events
                        (run_id, seq, event_type, payload, created_at_ms,
                         run_incarnation, event_key)
                    VALUES (?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        primary_event_type,
                        json.dumps(
                            primary_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                        int(time.time() * 1000),
                        incarnation,
                        primary_payload["event_key"],
                    ),
                )
                primary._conn.commit()
        else:
            primary.append(run_id, primary_event_type, primary_payload)
    finally:
        primary.close()

    if mismatch == "incarnation":
        outbox_payload = streamlit_api._build_workflow_result_event_payload(
            run_id,
            terminal,
            "failed",
            error="primary terminal",
            artifacts={"terminal_outcome": terminal, "final_output": terminal},
            snapshot={
                "workflow_name": "text_to_sql_pipeline",
                "session_id": "review11-session",
            },
            terminal_outcome=terminal,
            run_incarnation="review11-other-inc",
        )
    elif mismatch == "canonical_payload":
        outbox_payload = {
            **primary_payload,
            "error": "conflicting outbox terminal",
        }
    elif mismatch == "event_key":
        corrupted_primary = {
            **primary_payload,
            "event_key": "not-the-canonical-workflow-result-key",
        }
        payload_json = json.dumps(
            corrupted_primary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with sqlite3.connect(event_path) as connection:
            connection.execute(
                """
                UPDATE agui_events
                SET event_key = ?, payload = ?
                WHERE run_id = ?
                """,
                (corrupted_primary["event_key"], payload_json, run_id),
            )
        outbox_payload = dict(primary_payload)
    else:
        outbox_payload = dict(primary_payload)
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(outbox_payload)
    finally:
        outbox.close()
    return (
        streamlit_api,
        result_outbox,
        run_id,
        incarnation,
        primary_payload,
        outbox_path,
    )


def test_primary_and_outbox_exact_match_returns_primary_then_drains(
    monkeypatch,
    tmp_path,
):
    (
        streamlit_api,
        result_outbox,
        run_id,
        incarnation,
        primary_payload,
        outbox_path,
    ) = _seed_review11_primary_and_outbox(monkeypatch, tmp_path, None)
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    newer_incarnation = "review11-unrelated-newer-inc"
    newer_primary_payload = {
        **primary_payload,
        "run_incarnation": newer_incarnation,
        "event_key": store_module.workflow_result_event_key(
            run_id,
            newer_incarnation,
        ),
    }
    primary = store_module.EventStore(str(streamlit_api._agui_event_store_path()))
    try:
        # This intentionally simulates an unrelated corrupt/pre-validation row.
        # The public append boundary correctly forbids a second incarnation for
        # the same reserved run, while reconciliation must still ignore it.
        with primary._lock:
            primary._conn.execute(
                """
                INSERT INTO agui_events
                    (run_id, seq, event_type, payload, created_at_ms,
                     run_incarnation, event_key)
                SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?
                FROM agui_events WHERE run_id = ?
                """,
                (
                    run_id,
                    "WORKFLOW_RESULT",
                    json.dumps(
                        newer_primary_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    int(time.time() * 1000),
                    newer_incarnation,
                    newer_primary_payload["event_key"],
                    run_id,
                ),
            )
            unkeyed_legacy_payload = {
                "run_id": run_id,
                "run_incarnation": incarnation,
                "status": "failed",
                "error": "later unkeyed legacy result",
            }
            primary._conn.execute(
                """
                INSERT INTO agui_events
                    (run_id, seq, event_type, payload, created_at_ms,
                     run_incarnation, event_key)
                SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, NULL
                FROM agui_events WHERE run_id = ?
                """,
                (
                    run_id,
                    "WORKFLOW_RESULT",
                    json.dumps(
                        unkeyed_legacy_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    int(time.time() * 1000),
                    incarnation,
                    run_id,
                ),
            )
            primary._conn.commit()
    finally:
        primary.close()

    assert streamlit_api._workflow_result_payload_from_store(run_id) == primary_payload
    assert streamlit_api._workflow_result_payload_from_store(
        run_id,
        run_incarnation=incarnation,
    ) == primary_payload
    assert streamlit_api._drain_workflow_result_outbox(limit=10) == 1
    assert streamlit_api._workflow_result_payload_from_store(
        run_id,
        run_incarnation=incarnation,
    ) == primary_payload
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.count() == 0
    finally:
        outbox.close()


@pytest.mark.parametrize(
    "mismatch",
    ["incarnation", "event_type", "event_key", "canonical_payload"],
)
def test_primary_and_outbox_mismatch_raises_typed_collision_fail_closed(
    monkeypatch,
    tmp_path,
    mismatch,
):
    (
        streamlit_api,
        result_outbox,
        run_id,
        incarnation,
        _primary_payload,
        outbox_path,
    ) = _seed_review11_primary_and_outbox(monkeypatch, tmp_path, mismatch)
    collision_error = getattr(streamlit_api, "WorkflowResultCollisionError", None)

    assert isinstance(collision_error, type)
    with pytest.raises(collision_error, match="collision|mismatch"):
        streamlit_api._workflow_result_payload_from_store(
            run_id,
            strict=False,
            run_incarnation=incarnation,
        )
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.count() == 1
    finally:
        outbox.close()


@pytest.mark.parametrize("accessor", ["status", "artifacts"])
def test_workflow_consumers_fail_closed_on_primary_outbox_collision(
    monkeypatch,
    tmp_path,
    accessor,
):
    (
        streamlit_api,
        result_outbox,
        run_id,
        _incarnation,
        _primary_payload,
        outbox_path,
    ) = _seed_review11_primary_and_outbox(
        monkeypatch,
        tmp_path,
        "canonical_payload",
    )
    collision_error = getattr(streamlit_api, "WorkflowResultCollisionError", None)
    assert isinstance(collision_error, type)
    manager = streamlit_api.WorkflowManager(use_enhanced=False)

    with pytest.raises(collision_error, match="collision|mismatch"):
        if accessor == "status":
            manager.get_workflow_status(run_id)
        else:
            manager.get_workflow_artifacts(run_id)
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.count() == 1
    finally:
        outbox.close()


def test_reconciliation_collision_drain_never_acknowledges_outbox(
    monkeypatch,
    tmp_path,
):
    (
        streamlit_api,
        result_outbox,
        _run_id,
        _incarnation,
        _primary_payload,
        outbox_path,
    ) = _seed_review11_primary_and_outbox(
        monkeypatch,
        tmp_path,
        "canonical_payload",
    )

    assert streamlit_api._drain_workflow_result_outbox(limit=10) == 0
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.count() == 1
    finally:
        outbox.close()


@pytest.mark.parametrize(
    ("failure", "expected_retryable", "expected_attempts"),
    [
        (RuntimeError("permanent schema mismatch"), False, 1),
        (sqlite3.OperationalError("database is temporarily locked"), True, 2),
        (sqlite3.OperationalError("no such table: agui_events"), False, 1),
        (
            sqlite3.OperationalError(
                "attempt to write a readonly database"
            ),
            False,
            1,
        ),
    ],
)
def test_outbox_drain_classifies_each_primary_delivery_failure(
    monkeypatch,
    tmp_path,
    failure,
    expected_retryable,
    expected_attempts,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    result_outbox = importlib.import_module("workflow.result_outbox")
    outbox_path = tmp_path / "classified-delivery.db"
    run_id = "classified-delivery-run"
    incarnation = "classified-delivery-inc"
    payload = {
        "run_id": run_id,
        "run_incarnation": incarnation,
        "event_key": workflow_result_event_key(run_id, incarnation),
        "status": "failed",
    }
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(payload)
    finally:
        outbox.close()

    attempts = 0

    def fail_primary(_payload):
        nonlocal attempts
        attempts += 1
        raise failure

    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_payload_to_primary",
        fail_primary,
    )

    assert streamlit_api._drain_workflow_result_outbox_batch(
        limit=1,
        path=outbox_path,
    ) == (0, expected_retryable)
    assert attempts == expected_attempts
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.count() == 1
        assert outbox.latest_payload(run_id, incarnation) == payload
    finally:
        outbox.close()


@pytest.mark.parametrize(
    ("position", "corruption"),
    [
        ("first", "json"),
        ("middle", "json"),
        ("first", "identity"),
        ("middle", "identity"),
    ],
)
def test_outbox_quarantines_each_corrupt_row_and_preserves_valid_fifo(
    tmp_path,
    position,
    corruption,
):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    outbox_path = tmp_path / f"corrupt-{position}-{corruption}.db"
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        for index in range(3):
            outbox.enqueue(_outbox_test_payload(index, prefix="corrupt"))
    finally:
        outbox.close()
    target_index = 0 if position == "first" else 1
    target = _outbox_test_payload(target_index, prefix="corrupt")["event_key"]
    raw_marker = "UNBOUNDED_RAW_PAYLOAD_" + ("x" * 10_000)
    with sqlite3.connect(outbox_path) as connection:
        if corruption == "json":
            corrupted_payload = "{" + raw_marker
        else:
            corrupted = _outbox_test_payload(
                0 if position == "first" else 1,
                prefix="corrupt",
            )
            corrupted["event_key"] = "different-event-key-" + raw_marker
            corrupted_payload = json.dumps(corrupted)
        connection.execute(
            """
            UPDATE workflow_result_outbox
            SET payload = ?, payload_bytes = ?
            WHERE event_key = ?
            """,
            (corrupted_payload, len(corrupted_payload.encode("utf-8")), target),
        )

    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        pending = outbox.list_pending()
    finally:
        outbox.close()
    expected = [
        _outbox_test_payload(index, prefix="corrupt")["event_key"]
        for index in range(3)
    ]
    expected.remove(target)
    assert [entry.event_key for entry in pending] == expected
    with sqlite3.connect(outbox_path) as connection:
        rows = connection.execute(
            "SELECT metadata_json FROM workflow_result_outbox_dead_letter"
        ).fetchall()
    assert len(rows) == 1
    assert len(rows[0][0].encode("utf-8")) < 4096
    assert raw_marker not in rows[0][0]


def test_outbox_dead_letter_metadata_is_count_bounded(tmp_path):
    _install_light_workflow_package()
    result_outbox = importlib.import_module("workflow.result_outbox")
    outbox_path = tmp_path / "bounded-dlq.db"
    outbox = result_outbox.WorkflowResultOutbox(
        str(outbox_path),
        max_dead_letters=3,
    )
    outbox.close()
    with sqlite3.connect(outbox_path) as connection:
        for index in range(5):
            payload = "{invalid-json-" + str(index)
            connection.execute(
                """
                INSERT INTO workflow_result_outbox
                    (event_key, run_id, run_incarnation, payload,
                     payload_bytes, created_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"dlq-event-{index}",
                    f"dlq-run-{index}",
                    f"dlq-inc-{index}",
                    payload,
                    len(payload),
                    index,
                ),
            )
    outbox = result_outbox.WorkflowResultOutbox(
        str(outbox_path),
        max_dead_letters=3,
    )
    try:
        assert outbox.list_pending() == []
    finally:
        outbox.close()
    with sqlite3.connect(outbox_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM workflow_result_outbox_dead_letter"
        ).fetchone()[0]
    assert count == 3


def test_corrupt_outbox_row_never_aborts_manager_constructor_or_valid_drain(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    result_outbox = importlib.import_module("workflow.result_outbox")
    outbox_path = tmp_path / "constructor-corrupt.db"
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
    )
    outbox = result_outbox.WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(_outbox_test_payload(0, prefix="constructor-corrupt"))
        outbox.enqueue(_outbox_test_payload(1, prefix="constructor-corrupt"))
    finally:
        outbox.close()
    corrupt_key = _outbox_test_payload(0, prefix="constructor-corrupt")[
        "event_key"
    ]
    with sqlite3.connect(outbox_path) as connection:
        connection.execute(
            """
            UPDATE workflow_result_outbox
            SET payload = '{invalid', payload_bytes = 8
            WHERE event_key = ?
            """,
            (corrupt_key,),
        )
    delivered = []
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_payload_to_primary",
        lambda payload: delivered.append(payload["event_key"]),
    )

    streamlit_api.WorkflowManager(use_enhanced=False)

    assert _wait_until(
        lambda: delivered
        == [_outbox_test_payload(1, prefix="constructor-corrupt")["event_key"]],
        timeout=5,
    )


def test_outbox_default_state_path_migrates_only_known_owned_regular_file(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    project_root = tmp_path / "project"
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True)
    data_dir.chmod(0o755)
    unrelated = data_dir / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    unrelated.chmod(0o644)
    legacy = data_dir / "workflow_result_outbox.db"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_marker VALUES ('legacy-outbox')")
    legacy.chmod(0o600)
    monkeypatch.setattr(streamlit_api, "_project_root", lambda: project_root)

    resolved = streamlit_api._test_default_workflow_result_outbox_path()

    assert resolved == data_dir / "multiagent_state" / "workflow_result_outbox.db"
    with sqlite3.connect(resolved) as connection:
        assert connection.execute("SELECT value FROM legacy_marker").fetchone() == (
            "legacy-outbox",
        )
    assert legacy.exists()
    with sqlite3.connect(legacy) as connection:
        assert connection.execute("SELECT value FROM legacy_marker").fetchone() == (
            "legacy-outbox",
        )
    assert data_dir.stat().st_mode & 0o777 == 0o755
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert unrelated.stat().st_mode & 0o777 == 0o644


def test_outbox_default_state_path_never_migrates_legacy_symlink(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    project_root = tmp_path / "project"
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"outside")
    legacy = data_dir / "workflow_result_outbox.db"
    legacy.symlink_to(outside)
    monkeypatch.setattr(streamlit_api, "_project_root", lambda: project_root)

    resolved = streamlit_api._test_default_workflow_result_outbox_path()

    assert resolved == data_dir / "multiagent_state" / "workflow_result_outbox.db"
    assert resolved.is_file()
    assert legacy.is_symlink()
    assert outside.read_bytes() == b"outside"


def _startup_process_type(
    pid,
    *,
    terminate_stops=True,
    kill_stops=True,
):
    class Process:
        instances = []

        def __init__(self, *args, **kwargs):
            self.pid = pid
            self.alive = True
            self.actions = []
            self.instances.append(self)

        def start(self):
            self.actions.append("start")

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.actions.append("terminate")
            if terminate_stops:
                self.alive = False

        def join(self, timeout=None):
            self.actions.append(("join", timeout))

        def kill(self):
            self.actions.append("kill")
            if kill_stops:
                self.alive = False

    return Process


def _configure_startup_process_test(
    monkeypatch,
    tmp_path,
    streamlit_api,
    process_type,
    *,
    workflow_name="text_to_sql_pipeline",
):
    (tmp_path / "workflow.yaml").write_text(
        f"name: {workflow_name}\nsteps: []\n",
        encoding="utf-8",
    )
    workflow_def = types.SimpleNamespace(
        name=workflow_name,
        version="1.0",
        description="",
        steps=[],
        metadata={"category": "text_to_sql"} if workflow_name == "text_to_sql_pipeline" else {},
        inputs={},
        requires_enhanced_engine=False,
    )
    multiprocessing_module = types.ModuleType("multiprocessing")
    multiprocessing_module.Process = process_type
    monkeypatch.setitem(sys.modules, "multiprocessing", multiprocessing_module)
    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(lambda _path: workflow_def),
    )
    monkeypatch.setattr(streamlit_api, "_require_owner_lifecycle", lambda *_args: None)
    return streamlit_api.WorkflowManager(
        use_enhanced=False,
        pipelines_dir=str(tmp_path),
    )


def _startup_owner(streamlit_api):
    return streamlit_api.WorkflowOwner(
        subject="alice",
        tenant_id="tenant-a",
        roles=frozenset({"user"}),
    )


def _startup_test_context(
    monkeypatch,
    tmp_path,
    pid,
    *,
    workflow_name="text_to_sql_pipeline",
    terminate_stops=True,
    kill_stops=True,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    process_type = _startup_process_type(
        pid,
        terminate_stops=terminate_stops,
        kill_stops=kill_stops,
    )
    manager = _configure_startup_process_test(
        monkeypatch,
        tmp_path,
        streamlit_api,
        process_type,
        workflow_name=workflow_name,
    )
    return streamlit_api, manager, process_type


def _start_test_workflow(manager, run_id, *, owner=None, workflow_name="text_to_sql_pipeline"):
    return manager.start_workflow(
        workflow_name,
        parameters={},
        use_enhanced=False,
        run_id=run_id,
        owner=owner,
    )


def _startup_winner(run_id, terminal, result, status="completed", error=None):
    return {
        "run_id": run_id,
        "status": status,
        "success": status == "completed",
        "result": result,
        "error": error,
        "terminal_outcome": terminal,
        "artifacts": {"final_output": result, "terminal_outcome": terminal},
        "snapshot": {"workflow_name": "text_to_sql_pipeline", "parameters": {}},
    }


def _install_primary_terminal_conflict(monkeypatch, streamlit_api, winner):
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    candidates = []

    def reject_candidate(payload):
        candidates.append(payload)
        raise store_module.TerminalWorkflowResultConflictError("durable winner exists")

    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_payload_to_primary",
        reject_candidate,
    )
    monkeypatch.setattr(
        streamlit_api,
        "_authoritative_workflow_result_payload",
        lambda *_args, **_kwargs: winner,
        raising=False,
    )
    return candidates


def _configure_execute_conflict_test(
    monkeypatch,
    tmp_path,
    streamlit_api,
    run_id,
    engine,
    winner,
):
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: text_to_sql_pipeline\nsteps: []\n",
        encoding="utf-8",
    )
    manager = object.__new__(streamlit_api.WorkflowManager)
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "run_incarnation": "execute-conflict-inc",
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
            "progress_percentage": 37.0,
        }
    }
    callbacks = []
    manager.run_callbacks = {
        run_id: [("progress", lambda *args: callbacks.append(args))]
    }
    manager.engine = engine
    workflow_def = types.SimpleNamespace(
        name="text_to_sql_pipeline",
        steps=[],
        metadata={"category": "text_to_sql"},
    )
    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(lambda _path: workflow_def),
    )
    candidates = _install_primary_terminal_conflict(
        monkeypatch,
        streamlit_api,
        winner,
    )
    return manager, workflow_file, callbacks, candidates


def test_execute_success_conflict_raises_authoritative_failed_winner(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    run_id = "execute-success-loses"
    candidate_terminal = streamlit_api.TextToSqlTerminalResult.from_mapping(
        _terminal_contract_payload("succeeded", run_id)
    )
    winner_terminal = _terminal_contract_payload("failed", run_id)
    winner = _startup_winner(
        run_id,
        winner_terminal,
        {"winner": "failed"},
        status="failed",
        error="authoritative execution failure",
    )

    class Engine:
        async def execute_workflow_from_yaml(self, *_args, **_kwargs):
            return types.SimpleNamespace(
                status=streamlit_api.WorkflowStatus.COMPLETED,
                workflow_id="candidate-success",
                final_output={"candidate": "success"},
                step_results={},
                terminal_outcome=candidate_terminal,
            )

    manager, workflow_file, callbacks, candidates = _configure_execute_conflict_test(
        monkeypatch,
        tmp_path,
        streamlit_api,
        run_id,
        Engine(),
        winner,
    )

    with pytest.raises(streamlit_api.WorkflowExecutionError, match="authoritative"):
        manager._execute_workflow_in_context(
            run_id,
            workflow_file,
            {},
            "session-1",
            run_incarnation="execute-conflict-inc",
        )

    run_data = manager.active_runs[run_id]
    assert len(candidates) == 1
    assert run_data["status"] == "failed"
    assert run_data["final_output"] == {"winner": "failed"}
    assert run_data["error"] == "authoritative execution failure"
    assert run_data["progress_percentage"] == 0.0
    assert "last_completed" not in run_data
    assert [item[1] for item in callbacks] == ["started"]


def test_execute_exception_conflict_returns_authoritative_success(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    run_id = "execute-exception-loses"
    winner_terminal = _terminal_contract_payload("succeeded", run_id)
    winner = _startup_winner(
        run_id,
        winner_terminal,
        {"winner": "succeeded"},
    )

    class Engine:
        async def execute_workflow_from_yaml(self, *_args, **_kwargs):
            raise RuntimeError("candidate engine failure")

    manager, workflow_file, callbacks, candidates = _configure_execute_conflict_test(
        monkeypatch,
        tmp_path,
        streamlit_api,
        run_id,
        Engine(),
        winner,
    )

    result = manager._execute_workflow_in_context(
        run_id,
        workflow_file,
        {},
        "session-1",
        run_incarnation="execute-conflict-inc",
    )

    run_data = manager.active_runs[run_id]
    assert result == winner
    assert len(candidates) == 1
    assert run_data["status"] == "completed"
    assert run_data["final_output"] == {"winner": "succeeded"}
    assert run_data["error"] is None
    assert run_data["progress_percentage"] == 100.0
    assert "last_failed" not in run_data
    assert [item[1] for item in callbacks] == ["started"]


def test_workflow_manager_execute_fails_when_terminal_result_append_fails(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = "run-append-fails"
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text("name: append_fails_workflow\nsteps: []\n", encoding="utf-8")
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "workflow_name": "append_fails_workflow",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
        }
    }
    manager.run_callbacks = {}

    class WorkflowDef:
        name = "append_fails_workflow"
        steps = []
        metadata = {}

    class _Engine:
        async def execute_workflow_from_yaml(self, *_args, **_kwargs):
            return types.SimpleNamespace(
                status=streamlit_api.WorkflowStatus.COMPLETED,
                workflow_id="wf-append-fails",
                final_output={"ok": True},
                step_results={},
            )

    manager.engine = _Engine()
    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(lambda _path: WorkflowDef()),
    )
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(streamlit_api.WorkflowExecutionError, match="WORKFLOW_RESULT"):
        manager._execute_workflow_in_context(run_id, workflow_file, {}, "session-1")

    assert manager.active_runs[run_id]["status"] == "failed"
    assert "WORKFLOW_RESULT" in manager.active_runs[run_id]["error"]
    assert "last_failed" in manager.active_runs[run_id]


def test_workflow_manager_text_to_sql_serializes_typed_terminal_outcome(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = "run-typed-terminal"
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text("name: text_to_sql_pipeline\nsteps: []\n", encoding="utf-8")
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
        }
    }
    manager.run_callbacks = {}
    terminal = streamlit_api.TextToSqlTerminalResult.from_mapping({
        "run_id": run_id,
        "status": "succeeded",
        "reason_code": "",
        "sql": "SELECT 1",
        "generated": True,
        "approved": True,
        "executed": False,
        "dry_run": True,
        "audited": True,
        "data": [],
        "columns": [],
        "rows_affected": 0,
        "error": None,
        "execution": {
            "success": True,
            "sql_query": "SELECT 1",
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": 1,
            "dry_run_only": True,
            "skipped_execution": True,
            "applied_row_limit": 100,
        },
        "audit": {"status": "logged", "log_id": "audit-1"},
        "persistence": {"status": "not_attempted"},
    })

    class WorkflowDef:
        name = "text_to_sql_pipeline"
        steps = []
        metadata = {"category": "text_to_sql"}

    class _Engine:
        async def execute_workflow_from_yaml(self, *_args, **_kwargs):
            return types.SimpleNamespace(
                status=streamlit_api.WorkflowStatus.FAILED,
                workflow_id="wf-typed-terminal",
                final_output={"ok": True},
                step_results={},
                terminal_outcome=terminal,
            )

    manager.engine = _Engine()
    manager._notify_progress = lambda *args, **kwargs: None
    appended = []
    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(lambda _path: WorkflowDef()),
    )
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: appended.append((args, kwargs)) or True,
    )

    result = manager._execute_workflow_in_context(
        run_id,
        workflow_file,
        {"run_id": run_id},
        "session-1",
    )

    assert result.terminal_outcome is terminal
    assert manager.active_runs[run_id]["status"] == "completed"
    assert manager.active_runs[run_id]["terminal_outcome"]["status"] == "succeeded"
    assert appended[0][1]["terminal_outcome"]["dry_run"] is True
    assert manager.get_workflow_status(run_id).terminal_outcome["status"] == "succeeded"
    assert manager.get_workflow_artifacts(run_id).terminal_outcome["executed"] is False

    stored_payload = {
        "status": appended[0][0][2],
        "result": appended[0][0][1],
        "terminal_outcome": appended[0][1]["terminal_outcome"],
        "artifacts": appended[0][1]["artifacts"],
        "snapshot": appended[0][1]["snapshot"],
    }
    manager.active_runs = {}
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda _run_id: stored_payload,
    )
    assert manager.get_workflow_status(run_id).terminal_outcome["status"] == "succeeded"
    assert manager.get_workflow_artifacts(run_id).terminal_outcome["dry_run"] is True


@pytest.mark.parametrize("fallback_append_succeeds", [True, False])
def test_text_to_sql_result_append_failure_publishes_coherent_terminal_failure(
    monkeypatch,
    tmp_path,
    fallback_append_succeeds,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = f"run-result-append-{fallback_append_succeeds}"
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text("name: text_to_sql_pipeline\nsteps: []\n", encoding="utf-8")
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
        }
    }
    manager.run_callbacks = {}
    terminal = streamlit_api.TextToSqlTerminalResult.from_mapping(
        _terminal_contract_payload("succeeded", run_id)
    )

    class WorkflowDef:
        name = "text_to_sql_pipeline"
        steps = []
        metadata = {"category": "text_to_sql"}

    class _Engine:
        async def execute_workflow_from_yaml(self, *_args, **_kwargs):
            return types.SimpleNamespace(
                status=streamlit_api.WorkflowStatus.COMPLETED,
                workflow_id="wf-result-append",
                final_output={"final": terminal.to_mapping()},
                step_results={},
                terminal_outcome=terminal,
            )

    manager.engine = _Engine()
    manager._notify_progress = lambda *args, **kwargs: None
    append_results = iter([False, fallback_append_succeeds])
    append_calls = []

    def append_result(*args, **kwargs):
        append_calls.append((args, kwargs))
        return next(append_results)

    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(lambda _path: WorkflowDef()),
    )
    monkeypatch.setattr(streamlit_api, "_append_workflow_result_event", append_result)

    with pytest.raises(streamlit_api.WorkflowExecutionError, match="WORKFLOW_RESULT"):
        manager._execute_workflow_in_context(
            run_id,
            workflow_file,
            {},
            "session-1",
        )

    status = manager.get_workflow_status(run_id)
    artifacts = manager.get_workflow_artifacts(run_id)
    assert status.status == "failed"
    assert status.terminal_outcome["reason_code"] == "RESULT_PERSISTENCE_FAILED"
    assert artifacts.terminal_outcome == status.terminal_outcome
    assert artifacts.final_output == status.terminal_outcome
    assert manager.active_runs[run_id]["final_output"] == status.terminal_outcome
    assert len(append_calls) == 2
    assert append_calls[1][0][1] == status.terminal_outcome
    assert append_calls[1][0][2] == "failed"
    assert append_calls[1][1]["terminal_outcome"] == status.terminal_outcome
    assert append_calls[1][1]["artifacts"]["final_output"] == status.terminal_outcome


@pytest.mark.parametrize(
    "terminal_outcome",
    [
        {"status": "succeeded"},
        {**_terminal_contract_payload(), "audited": "true"},
    ],
)
def test_workflow_manager_stored_terminal_validation_fails_closed(
    monkeypatch,
    terminal_outcome,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    manager.active_runs = {}
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda run_id: {
            "status": "completed",
            "success": True,
            "terminal_outcome": terminal_outcome,
            "artifacts": {"final_output": "stored output"},
            "snapshot": {"workflow_name": "text_to_sql_pipeline"},
        },
    )

    status = manager.get_workflow_status("run-1")
    artifacts = manager.get_workflow_artifacts("run-1")

    assert status.status == "unknown_legacy"
    assert status.terminal_outcome is None
    assert artifacts.terminal_outcome is None


@pytest.mark.parametrize(
    ("terminal_status", "legacy_status"),
    [("cancelled", "cancelled"), ("timed_out", "failed")],
)
def test_workflow_manager_stored_cancel_and_timeout_are_typed(
    monkeypatch,
    terminal_status,
    legacy_status,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    manager.active_runs = {}
    terminal = _terminal_contract_payload(terminal_status, "run-1")
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda run_id: {
            "status": "completed",
            "success": True,
            "terminal_outcome": terminal,
            "artifacts": {"final_output": "stored output"},
            "snapshot": {"workflow_name": "text_to_sql_pipeline"},
        },
    )

    status = manager.get_workflow_status("run-1")
    artifacts = manager.get_workflow_artifacts("run-1")

    assert status.status == legacy_status
    assert status.terminal_outcome["status"] == terminal_status
    assert artifacts.terminal_outcome["status"] == terminal_status


@pytest.mark.parametrize(
    ("terminal_status", "event_status"),
    [("cancelled", "cancelled"), ("timed_out", "failed")],
)
def test_workflow_result_event_derives_cancel_and_timeout_from_terminal(
    monkeypatch,
    tmp_path,
    terminal_status,
    event_status,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    captured = []

    class _EventStore:
        def __init__(self, path):
            self.path = path

        def append(self, run_id, event_type, payload):
            captured.append((run_id, event_type, payload))

    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    monkeypatch.setattr(store_module, "EventStore", _EventStore)
    monkeypatch.setattr(
        streamlit_api,
        "_agui_event_store_path",
        lambda: tmp_path / "events.db",
    )
    terminal = _terminal_contract_payload(terminal_status, "run-1")

    resolution = streamlit_api._append_workflow_result_event(
        "run-1",
        {"output": "must not imply success"},
        "completed",
        terminal_outcome=terminal,
        snapshot={"workflow_name": "text_to_sql_pipeline"},
    )
    assert resolution.persistence_succeeded is True
    assert resolution.candidate_won is True

    payload = captured[0][2]
    assert payload["status"] == event_status
    assert payload["success"] is False
    assert payload["terminal_outcome"]["status"] == terminal_status


def test_workflow_manager_exception_path_reports_failed_result_append_failure(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = "run-exception-append-fails"
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text("name: exception_append_fails_workflow\nsteps: []\n", encoding="utf-8")
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "workflow_name": "exception_append_fails_workflow",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
        }
    }
    manager.run_callbacks = {}

    class WorkflowDef:
        name = "exception_append_fails_workflow"
        steps = []
        metadata = {}

    class _Engine:
        async def execute_workflow_from_yaml(self, *_args, **_kwargs):
            raise RuntimeError("engine failed")

    manager.engine = _Engine()
    monkeypatch.setattr(
        streamlit_api.WorkflowDefinition,
        "from_yaml",
        staticmethod(lambda _path: WorkflowDef()),
    )
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(streamlit_api.WorkflowExecutionError, match="WORKFLOW_RESULT"):
        manager._execute_workflow_in_context(run_id, workflow_file, {}, "session-1")

    assert manager.active_runs[run_id]["status"] == "failed"
    assert "WORKFLOW_RESULT" in manager.active_runs[run_id]["error"]
    assert "last_failed" in manager.active_runs[run_id]


def test_tools_active_runs_serializes_cyclic_values(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    cycle: dict[str, Any] = {"openai_api_key": "sk-live"}
    cycle["self"] = cycle

    class _ToolManager:
        active_runs = {"run-cycle": {"status": "completed", "result": cycle}}

    monkeypatch.setattr(service, "_tool_manager", lambda: _ToolManager())

    result = service.handle_service_action("tools.active_runs", {})
    serialized = json.dumps(result, ensure_ascii=False)

    assert "sk-live" not in serialized
    assert "[Circular]" in serialized
    assert result["runs"]["run-cycle"]["result"]["openai_api_key"] == "<redacted>"


def _load_streamlit_text_to_sql_page():
    if "streamlit" not in sys.modules:
        sys.modules["streamlit"] = types.ModuleType("streamlit")
    page_path = Path("streamlit_app/pages/05_Text_to_SQL.py")
    spec = importlib.util.spec_from_file_location("t13_streamlit_text_to_sql", page_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_streamlit_text_to_sql_page_imports_without_in_process_runtime(monkeypatch):
    class _ForbiddenRuntime(types.ModuleType):
        def __getattr__(self, name):
            raise AssertionError(f"in-process runtime accessed: {self.__name__}.{name}")

    for module_name in (
        "backend.fastapi_app.agui.service",
        "workflow.streamlit_api",
        "db_plugins.streamlit_api",
        "memory.tools",
    ):
        monkeypatch.setitem(sys.modules, module_name, _ForbiddenRuntime(module_name))

    module = _load_streamlit_text_to_sql_page()

    assert hasattr(module, "TextToSqlApiClient")
    assert not hasattr(module, "LegacyTextToSqlHistory")


def test_streamlit_text_to_sql_options_rejects_non_integer_max_rows():
    validate = _load_streamlit_text_to_sql_page()._validate_text_to_sql_options

    for value in [True, 1.9, "1.9", "1e2", ""]:
        with pytest.raises(ValueError, match="max_rows"):
            validate(value, "strict")

    assert validate("100", "strict") == (100, "strict")


def test_streamlit_start_uses_typed_client_and_opaque_state(monkeypatch):
    module = _load_streamlit_text_to_sql_page()
    captured = {}

    class _Session(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as exc:
                raise AttributeError(name) from exc

        def __setattr__(self, name, value):
            self[name] = value

    session_state = _Session(
        agent_run_id="",
        agent_run_status=None,
        text_to_sql_result=None,
        text_to_sql_query="",
    )
    monkeypatch.setattr(
        module,
        "st",
        types.SimpleNamespace(session_state=session_state),
    )

    class _Client:
        def start(self, request):
            captured["request"] = request
            return types.SimpleNamespace(run_id="run-http-1")

    module.generate_sql_query(
        _Client(),
        natural_query="show users",
        connection_ref="conn-123e4567-e89b-42d3-a456-426614174000",
        max_rows="7",
        safety_level="strict",
        include_explanation=True,
        validate_schema=False,
        dry_run_only=True,
        use_schema_suggestions=True,
    )

    request = captured["request"]
    assert request.connection_ref == "conn-123e4567-e89b-42d3-a456-426614174000"
    assert request.query == "show users"
    assert request.max_rows == 7
    assert request.dry_run_only is True
    assert "dsn" not in request.__dataclass_fields__
    assert session_state.agent_run_id == "run-http-1"
    assert "selected_dsn" not in session_state


# ---------------------------------------------------------------------------
# T12-pipeline: pipeline-конфиг и профиль
# ---------------------------------------------------------------------------

def test_sql_verification_step_has_condition_on_empty_sql():
    """T12: шаг sql_verification должен иметь condition, проверяющий непустой sql из sql_generation."""
    models = importlib.import_module("tests.workflow_test_utils").load_light_workflow_models()
    WorkflowDefinition = models.WorkflowDefinition

    workflow = WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    verification_step = next(
        step for step in workflow.steps if step.id == "sql_verification"
    )

    assert verification_step.condition is not None, (
        "sql_verification должен иметь condition для guard пустого SQL"
    )
    assert "sql_generation.sql" in verification_step.condition, (
        "condition должен проверять поле sql из sql_generation"
    )


def test_sql_verification_step_receives_semantic_context():
    models = importlib.import_module("tests.workflow_test_utils").load_light_workflow_models()
    workflow = models.WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    verification_step = next(
        step for step in workflow.steps if step.id == "sql_verification"
    )
    task = verification_step.task

    assert "{query}" in task
    assert "{intent_extraction_step}" in task
    assert "{schema_linking_step}" in task
    for required_check in (
        "mandatory filters",
        "aggregation",
        "grouping",
        "cardinality",
        "ordering",
        "limit",
    ):
        assert required_check in task.lower()


def test_sql_generation_step_requires_successful_schema_linking():
    """P-2: join_success=False не должен пускать пайплайн в успешную генерацию SQL."""
    models = importlib.import_module("tests.workflow_test_utils").load_light_workflow_models()
    WorkflowDefinition = models.WorkflowDefinition

    workflow = WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    generation_step = next(
        step for step in workflow.steps if step.id == "sql_generation"
    )

    assert generation_step.condition == "{schema_linking_step.sql_generation_allowed}"
    skip_output = generation_step.metadata.get("skip_output")
    assert skip_output is not None
    assert skip_output["sql"] == ""
    assert skip_output["error"] == "schema_linking_join_failed"


def test_sql_generation_join_failure_condition_writes_explicit_skip_output():
    """P-2: при join_success=False downstream видит явный failure payload, а не успешный SQL."""
    WorkflowEngine = _load_light_workflow_engine().WorkflowEngine
    WorkflowContext = sys.modules["workflow.models"].WorkflowContext
    WorkflowDefinition = sys.modules["workflow.models"].WorkflowDefinition

    workflow = WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    generation_step = next(
        step for step in workflow.steps if step.id == "sql_generation"
    )
    engine = object.__new__(WorkflowEngine)
    context = WorkflowContext(
        variables={},
        step_outputs={
            "schema_linking_step": {
                "join_success": False,
                "sql_generation_allowed": False,
                "schema_info": {},
                "joins": [],
            },
            "schema_linking_step.sql_generation_allowed": False,
        },
    )

    assert engine._should_skip_step_by_condition(generation_step, context) is True
    assert context.step_outputs["sql_generation"]["sql"] == ""
    assert context.step_outputs["sql_generation.error"] == "schema_linking_join_failed"


def test_sql_generation_stays_blocked_when_schema_linking_is_disabled():
    """Legacy schema-disable payload remains fail-closed."""
    WorkflowEngine = _load_light_workflow_engine().WorkflowEngine
    WorkflowContext = sys.modules["workflow.models"].WorkflowContext
    WorkflowDefinition = sys.modules["workflow.models"].WorkflowDefinition

    workflow = WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    generation_step = next(
        step for step in workflow.steps if step.id == "sql_generation"
    )
    engine = object.__new__(WorkflowEngine)
    context = WorkflowContext(
        variables={},
        step_outputs={
            "schema_linking_step": {
                "status": "skipped_disabled",
                "decision": "ABSTAIN",
                "terminal_reason_code": "SCHEMA_GROUNDING_FAILED",
                "join_success": False,
                "sql_generation_allowed": False,
                "schema_info": {},
                "joins": [],
            },
            "schema_linking_step.sql_generation_allowed": False,
        },
    )

    assert engine._should_skip_step_by_condition(generation_step, context) is True
    assert context.step_outputs["sql_generation"]["sql"] == ""
    assert context.step_outputs["sql_generation.error"] == "schema_linking_join_failed"


def test_sql_verification_step_skip_output_has_rejected_status():
    """T12: при пропуске sql_verification должен возвращать verification_status=Rejected,
    чтобы db_audit тоже пропустился через свой condition."""
    models = importlib.import_module("tests.workflow_test_utils").load_light_workflow_models()
    WorkflowDefinition = models.WorkflowDefinition

    workflow = WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    verification_step = next(
        step for step in workflow.steps if step.id == "sql_verification"
    )

    skip_output = verification_step.metadata.get("skip_output")
    assert skip_output is not None, (
        "sql_verification.metadata должен содержать skip_output"
    )
    assert skip_output.get("verification_status") == "Rejected", (
        "skip_output.verification_status должен быть Rejected, "
        "чтобы db_audit пропустился через свой condition"
    )
    assert "recommendations" in skip_output, (
        "skip_output должен содержать ключ recommendations (контракт output_schema)"
    )


def test_evaluate_condition_empty_sql_returns_false():
    """T12: _evaluate_condition с пустым sql_generation.sql должен вернуть False."""
    WorkflowEngine = _load_light_workflow_engine().WorkflowEngine
    WorkflowContext = sys.modules["workflow.models"].WorkflowContext

    engine = object.__new__(WorkflowEngine)
    # sql_generation вернул пустую строку
    context = WorkflowContext(
        variables={},
        step_outputs={"sql_generation": {"sql": "", "description": "failed"}},
    )
    result = engine._evaluate_condition('{sql_generation.sql} != ""', context)
    assert result is False, (
        "Пустой sql должен приводить к False, чтобы sql_verification пропустился"
    )


def test_evaluate_condition_nonempty_sql_returns_true():
    """T12: _evaluate_condition с непустым sql_generation.sql должен вернуть True."""
    WorkflowEngine = _load_light_workflow_engine().WorkflowEngine
    WorkflowContext = sys.modules["workflow.models"].WorkflowContext

    engine = object.__new__(WorkflowEngine)
    context = WorkflowContext(
        variables={},
        step_outputs={"sql_generation": {"sql": "SELECT 1", "description": "ok"}},
    )
    result = engine._evaluate_condition('{sql_generation.sql} != ""', context)
    assert result is True, (
        "Непустой sql должен приводить к True, чтобы sql_verification выполнился"
    )


def test_db_audit_agent_prompt_has_no_format_placeholder_max_rows():
    """T12: prompt_templates db_audit_agent.yaml не должен содержать Python-формат-плейсхолдер {max_rows}."""
    import yaml

    profile_path = Path("agent_profiles/db_audit_agent.yaml")
    with profile_path.open(encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    prompt = profile.get("prompt_templates", "")
    assert "{max_rows}" not in prompt, (
        "prompt_templates не должен содержать {max_rows} — "
        "этот плейсхолдер никогда не подставляется в instructions"
    )
