from pathlib import Path
import asyncio
import base64
import contextlib
import gzip
import importlib
import importlib.util
import inspect
import json
import logging
import os
import sqlite3
import sys
import types
from typing import Any

import pytest

_LIGHT_WORKFLOW_MODULES = [
    "workflow",
    "workflow.engine",
    "workflow.enhanced_engine",
    "workflow.models",
    "workflow.state_manager",
    "workflow.retry_engine",
    "workflow.resource_manager",
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
    workflow_streamlit = types.ModuleType("workflow.streamlit_api")
    workflow_streamlit.WorkflowManager = lambda: wf_manager
    monkeypatch.setitem(sys.modules, "workflow", workflow_pkg)
    monkeypatch.setitem(sys.modules, "workflow.streamlit_api", workflow_streamlit)

    utils_module = types.ModuleType("utils")
    utils_module.call_openai_api_streaming = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "utils", utils_module)

    import backend.fastapi_app.agui as agui_pkg

    monkeypatch.delattr(agui_pkg, "service", raising=False)
    service = importlib.import_module("backend.fastapi_app.agui.service")

    monkeypatch.setattr(service, "_agent_manager", lambda: object())
    monkeypatch.setattr(service, "_wf_manager", lambda: wf_manager)
    monkeypatch.setattr(service, "_memory_manager", lambda: object())
    monkeypatch.setattr(service, "_db_manager", lambda: object())
    monkeypatch.setattr(service, "_config_manager", lambda: object())
    monkeypatch.setattr(service, "_telemetry_manager", lambda: object())
    monkeypatch.setattr(service, "_logging_manager", lambda: object())
    monkeypatch.setattr(service, "_tool_manager", lambda: object())
    return service


class _WorkflowManagerStub:
    def __init__(self):
        self.calls = []

    def start_workflow(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs["run_id"]


class _StepResultStub:
    def __init__(self, output):
        self.output = output


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
    return streamlit_api


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


def test_text_to_sql_generate_rejects_schema_suggestions_disabled_with_validation(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(ValueError, match="use_schema_suggestions=false requires validate_schema=false"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {
                "query": "show users",
                "dsn": "sqlite:///tmp/app.db",
                "use_schema_suggestions": False,
                "validate_schema": True,
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
        {"query": "show users", "dsn": "sqlite:///tmp/app.db"},
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
    masked_dsn = "mssql+pyodbc://srv/db?password=***;driver=ODBC+Driver+17"
    raw_dsn = "mssql+pyodbc://srv/db?password=top;secret;driver=ODBC+Driver+17"
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
        "mssql+pyodbc:///?odbc_connect=Driver%3D%7BODBC+Driver+17%7D%3B"
        "Server%3Ddb1.example.com%3BDatabase%3Dorders%3BUID%3Dalice%3BPWD%3Dtopsecret"
        "&driver=ODBC+Driver+17"
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
    raw_dsn = "postgresql://alice:secret@example.com/db?api_key=raw-key"
    legacy_masked_dsn = "postgresql://alice:***@example.com/db?api_key=***"
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

    assert listed["configs"][0]["dsn"] == "postgresql://***:***@example.com/db?api_key=***"
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
    raw_dsn = "postgresql://alice:newsecret@example.com/db"
    stale_secret = "postgresql://alice:oldsecret@example.com/db"
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
    assert public_config["dsn"] == "postgresql://***:***@example.com/db"
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
    dsn = "postgresql://host/db?api%5Fkey=rawsecret&sslmode=require"
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


def test_text_to_sql_schema_load_requires_explicit_db_fallback(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_load_text_to_sql_schema_from_memory", lambda dsn: None)

    with pytest.raises(ValueError, match="allow_db_schema_fallback=true"):
        service.handle_service_action(
            "text_to_sql.schema.load",
            {"dsn": "sqlite:///tmp/app.db"},
        )


def test_text_to_sql_schema_load_rejects_non_boolean_fallback_flag(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_load_text_to_sql_schema_from_memory", lambda dsn: None)

    with pytest.raises(ValueError, match="allow_db_schema_fallback"):
        service.handle_service_action(
            "text_to_sql.schema.load",
            {"dsn": "sqlite:///tmp/app.db", "allow_db_schema_fallback": "typo"},
        )


def test_workflow_result_artifacts_and_logs_are_redacted(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
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

    result = service.handle_service_action("workflows.result", {"run_id": "run-1"})
    artifacts = service.handle_service_action("workflows.artifacts", {"run_id": "run-1"})
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


def test_workflow_cached_report_action_is_redacted(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    raw_dsn = "postgresql://alice:secret@example.com/app?api_key=abc"
    raw_email = "person@example.com"
    raw_phone = "+7 (495) 123-45-67"
    encoded = base64.b64encode(
        gzip.compress(f"<html>{raw_dsn} {raw_email} {raw_phone}</html>".encode("utf-8"))
    ).decode("ascii")
    wf_manager.active_runs = {
        "run-1": {
            "report": {
                "run_id": "run-1",
                "mime_type": "text/html",
                "base64_gzip": encoded,
            }
        }
    }

    result = service.handle_service_action("workflows.generate_report", {"run_id": "run-1"})
    decoded = gzip.decompress(base64.b64decode(result["report"]["base64_gzip"])).decode("utf-8")

    assert "secret" not in decoded
    assert "api_key=abc" not in decoded
    assert raw_email not in decoded
    assert raw_phone not in decoded
    assert "***:***@example.com" in decoded
    assert "[EMAIL]" in decoded
    assert "[PHONE]" in decoded


def test_workflow_cached_report_rewrites_html_file_without_pii(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    raw_dsn = "postgresql://alice:secret@example.com/app?api_key=abc"
    raw_email = "person@example.com"
    raw_phone = "+7 (495) 123-45-67"
    session_id = "sess-workflow-cached"
    filename = f"interactive_plots_{session_id}.html"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    html_path = output_dir / filename
    raw_html = f"<html>{raw_dsn} {raw_email} {raw_phone}</html>"
    html_path.write_text(raw_html, encoding="utf-8")
    encoded = base64.b64encode(gzip.compress(raw_html.encode("utf-8"))).decode("ascii")
    wf_manager.active_runs = {
        "run-1": {
            "session_id": session_id,
            "report": {
                "run_id": "run-1",
                "session_id": session_id,
                "filename": filename,
                "mime_type": "text/html",
                "base64_gzip": encoded,
            },
        }
    }

    result = service.handle_service_action("workflows.generate_report", {"run_id": "run-1"})
    decoded = gzip.decompress(base64.b64decode(result["report"]["base64_gzip"])).decode("utf-8")
    disk_html = html_path.read_text(encoding="utf-8")

    for content in (decoded, disk_html):
        assert "secret" not in content
        assert "api_key=abc" not in content
        assert raw_email not in content
        assert raw_phone not in content
        assert "***:***@example.com" in content
        assert "[EMAIL]" in content
        assert "[PHONE]" in content


def test_workflow_generate_report_rewrites_html_file_without_pii(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    raw_email = "person@example.com"
    raw_phone = "+7 (495) 123-45-67"
    raw_dsn = "postgresql://alice:secret@example.com/app?api_key=abc"
    html_path = tmp_path / "interactive_plots_run-1.html"

    class _Artifacts:
        final_output = {
            "content": f"{raw_dsn} {raw_email} {raw_phone}",
        }

    wf_manager.active_runs = {"run-1": {"session_id": "run-1"}}
    monkeypatch.setattr(wf_manager, "get_workflow_artifacts", lambda run_id: _Artifacts(), raising=False)

    html_utils = types.ModuleType("html_utils")

    class _HtmlVisualizer:
        @staticmethod
        def advanced_visualization(report_text, session_id, show=True):
            html_path.write_text(f"<html>{report_text}</html>", encoding="utf-8")
            return str(html_path)

    html_utils.html_visualizer = _HtmlVisualizer()
    monkeypatch.setitem(sys.modules, "html_utils", html_utils)

    result = service.handle_service_action("workflows.generate_report", {"run_id": "run-1"})
    decoded = gzip.decompress(base64.b64decode(result["report"]["base64_gzip"])).decode("utf-8")
    disk_html = html_path.read_text(encoding="utf-8")

    for content in (decoded, disk_html):
        assert "secret" not in content
        assert "api_key=abc" not in content
        assert raw_email not in content
        assert raw_phone not in content
        assert "***:***@example.com" in content
        assert "[EMAIL]" in content
        assert "[PHONE]" in content


def test_telemetry_generate_report_rewrites_html_file_without_pii(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    raw_email = "person@example.com"
    raw_phone = "+7 (495) 123-45-67"
    raw_dsn = "postgresql://alice:secret@example.com/app?api_key=abc"
    run_id = "run-telemetry"
    html_path = tmp_path / f"interactive_plots_{run_id}.html"
    traces_dir = tmp_path / "logs" / "traces"
    traces_dir.mkdir(parents=True)
    (traces_dir / f"{run_id}.jsonl").write_text(
        json.dumps({"name": "agent_run_demo", "events": []}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)

    class _TelemetryManager:
        def load_trace_file(self, _run_id):
            return {
                "spans": [{
                    "attributes": {
                        "output.value": json.dumps({
                            "content": f"{raw_dsn} {raw_email} {raw_phone}",
                        }),
                    },
                }],
            }

    monkeypatch.setattr(service, "_telemetry_manager", lambda: _TelemetryManager())

    telemetry_helpers = types.ModuleType("telemetry.helpers")
    telemetry_helpers.get_trace_status = lambda spans: {"status": "completed"}
    monkeypatch.setitem(sys.modules, "telemetry.helpers", telemetry_helpers)

    html_utils = types.ModuleType("html_utils")

    class _HtmlVisualizer:
        @staticmethod
        def advanced_visualization(report_text, session_id, show=True):
            html_path.write_text(f"<html>{report_text}</html>", encoding="utf-8")
            return str(html_path)

    html_utils.html_visualizer = _HtmlVisualizer()
    monkeypatch.setitem(sys.modules, "html_utils", html_utils)

    result = service.handle_service_action("telemetry.generate_report", {"run_id": run_id})
    decoded = gzip.decompress(base64.b64decode(result["report"]["base64_gzip"])).decode("utf-8")
    disk_html = html_path.read_text(encoding="utf-8")

    for content in (decoded, disk_html):
        assert "secret" not in content
        assert "api_key=abc" not in content
        assert raw_email not in content
        assert raw_phone not in content
        assert "***:***@example.com" in content
        assert "[EMAIL]" in content
        assert "[PHONE]" in content


def test_telemetry_cached_report_rewrites_html_file_without_pii(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    raw_email = "person@example.com"
    raw_phone = "+7 (495) 123-45-67"
    raw_dsn = "postgresql://alice:secret@example.com/app?api_key=abc"
    run_id = "run-cached-report"
    session_id = "sess-cached"
    filename = f"interactive_plots_{session_id}.html"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    html_path = output_dir / filename
    raw_html = f"<html>{raw_dsn} {raw_email} {raw_phone}</html>"
    html_path.write_text(raw_html, encoding="utf-8")
    encoded = base64.b64encode(gzip.compress(raw_html.encode("utf-8"))).decode("ascii")
    traces_dir = tmp_path / "logs" / "traces"
    traces_dir.mkdir(parents=True)
    (traces_dir / f"{run_id}.jsonl").write_text(
        json.dumps(
            {
                "name": "agent_run_demo",
                "events": [{
                    "name": "report_generated",
                    "attributes": {
                        "report.mime_type": "text/html",
                        "report.filename": filename,
                        "report.session_id": session_id,
                        "report.content_b64_gzip": encoded,
                    },
                }],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)

    class _TelemetryManager:
        def load_trace_file(self, _run_id):
            return {"spans": [{"attributes": {"output.value": "unused"}}]}

    monkeypatch.setattr(service, "_telemetry_manager", lambda: _TelemetryManager())

    telemetry_helpers = types.ModuleType("telemetry.helpers")
    telemetry_helpers.get_trace_status = lambda spans: {"status": "completed"}
    monkeypatch.setitem(sys.modules, "telemetry.helpers", telemetry_helpers)

    result = service.handle_service_action("telemetry.generate_report", {"run_id": run_id})
    decoded = gzip.decompress(base64.b64decode(result["report"]["base64_gzip"])).decode("utf-8")
    disk_html = html_path.read_text(encoding="utf-8")

    for content in (decoded, disk_html):
        assert "secret" not in content
        assert "api_key=abc" not in content
        assert raw_email not in content
        assert raw_phone not in content
        assert "***:***@example.com" in content
        assert "[EMAIL]" in content
        assert "[PHONE]" in content


def test_text_to_sql_workflow_result_reports_failure_and_dry_run_execution(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    monkeypatch.setattr(
        service,
        "_workflow_result_from_store",
        lambda run_id: {
            "run_id": run_id,
            "status": "failed",
            "success": False,
            "error": "Workflow failed steps: sql_pipeline",
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

    result = service.handle_service_action("workflows.result", {"run_id": "run-text-to-sql"})

    assert result["status"] == "failed"
    assert result["success"] is False
    assert result["error"] == "Workflow failed steps: sql_pipeline"
    assert result["execution"]["executed"] is False
    assert result["artifacts"]["step_outputs"]["sql_pipeline"]["sql_query"] == "SELECT 1"


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


def test_workflow_result_store_prefers_workflow_result_over_run_finished(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    class Event:
        def __init__(self, event_type, payload):
            self.event_type = event_type
            self.payload = payload

    class Store:
        def list_after(self, run_id, after_seq):
            return [
                Event("WORKFLOW_RESULT", {"status": "completed", "artifacts": {"final_output": "ok"}}),
                Event("RUN_FINISHED", {"type": "RUN_FINISHED", "run_id": run_id, "result": None}),
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
    manager = streamlit_api.WorkflowManager(use_enhanced=False, pipelines_dir=str(tmp_path))
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

    class Process:
        pid = 12345
        exitcode = None

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

        def join(self):
            return None

    multiprocessing = types.ModuleType("multiprocessing")
    multiprocessing.Process = Process
    monkeypatch.setitem(sys.modules, "multiprocessing", multiprocessing)

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


@pytest.mark.filterwarnings("ignore")
def test_workflow_manager_accepts_explicit_run_id_contract():
    streamlit_api = _load_light_workflow_streamlit_api()
    WorkflowManager = streamlit_api.WorkflowManager
    _workflow_dsn_env = streamlit_api._workflow_dsn_env

    signature = inspect.signature(WorkflowManager.start_workflow)
    assert "run_id" in signature.parameters
    assert signature.parameters["run_id"].default is None

    previous_dsn = os.environ.get("DB_DSN")
    previous_limit = os.environ.get("DB_EXECUTOR_ROW_LIMIT")
    previous_dry_run = os.environ.get("TEXT_TO_SQL_DRY_RUN_ONLY")
    previous_safety = os.environ.get("TEXT_TO_SQL_SAFETY_LEVEL")
    previous_validate = os.environ.get("TEXT_TO_SQL_VALIDATE_SCHEMA")
    try:
        with _workflow_dsn_env({
            "dsn": "sqlite:///tmp/app.db",
            "max_rows": 7,
            "dry_run_only": True,
            "safety_level": "strict",
            "validate_schema": False,
        }):
            assert os.environ["DB_DSN"] == "sqlite:///tmp/app.db"
            assert os.environ["DB_EXECUTOR_ROW_LIMIT"] == "7"
            assert os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] == "True"
            assert os.environ["TEXT_TO_SQL_SAFETY_LEVEL"] == "strict"
            assert os.environ["TEXT_TO_SQL_VALIDATE_SCHEMA"] == "False"
    finally:
        if previous_dsn is None:
            os.environ.pop("DB_DSN", None)
        else:
            os.environ["DB_DSN"] = previous_dsn
        if previous_limit is None:
            os.environ.pop("DB_EXECUTOR_ROW_LIMIT", None)
        else:
            os.environ["DB_EXECUTOR_ROW_LIMIT"] = previous_limit
        if previous_dry_run is None:
            os.environ.pop("TEXT_TO_SQL_DRY_RUN_ONLY", None)
        else:
            os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] = previous_dry_run
        if previous_safety is None:
            os.environ.pop("TEXT_TO_SQL_SAFETY_LEVEL", None)
        else:
            os.environ["TEXT_TO_SQL_SAFETY_LEVEL"] = previous_safety
        if previous_validate is None:
            os.environ.pop("TEXT_TO_SQL_VALIDATE_SCHEMA", None)
        else:
            os.environ["TEXT_TO_SQL_VALIDATE_SCHEMA"] = previous_validate


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
    assert workflow.inputs["query"] == ""
    assert workflow.inputs["dsn"] == ""
    assert schema_step.tool_params["entities"] == "{intent_extraction_step.entities}"
    assert schema_step.condition == "{use_schema_suggestions}"
    # EPIC 6.9: skip_output теперь использует status: "skipped_disabled" вместо disabled: true.
    assert schema_step.metadata["skip_output"]["status"] == "skipped_disabled"
    assert workflow.inputs["max_rows"] == 100
    assert "dry_run_only" in workflow.inputs
    assert workflow.inputs["allow_enhanced_fallback"] is False
    assert audit_step.metadata["max_rows"] == "{max_rows}"
    assert audit_step.metadata["dry_run_only"] == "{dry_run_only}"
    assert audit_step.metadata["allow_enhanced_fallback"] == "{allow_enhanced_fallback}"
    assert schema_step.tool_params["dsn"] == "{dsn}"
    assert generation_step.metadata["dsn"] == "{dsn}"
    assert verification_step.metadata["dsn"] == "{dsn}"
    assert audit_step.metadata["dsn"] == "{dsn}"
    assert "dsn={dsn}" not in generation_step.task
    assert "dsn={dsn}" not in verification_step.task
    assert "dsn={dsn}" not in audit_step.task
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
    assert "sql_generation_plugin(context=..., user_query=...)" in generation_step.task
    assert "sql_safety_check(sql_query=...)" in verification_step.task
    assert "sql_explain(sql_query=...)" in verification_step.task


@pytest.mark.filterwarnings("ignore")
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


@pytest.mark.filterwarnings("ignore")
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


@pytest.mark.filterwarnings("ignore")
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
                },
            },
            "snapshot": {
                "workflow_name": "text_to_sql_pipeline",
                "parameters": {"dry_run_only": True, "dsn": raw_dsn, "note": "person@example.com"},
            },
        },
    )

    status = manager.get_workflow_status("run-process")
    artifacts = manager.get_workflow_artifacts("run-process")

    assert status.status == "failed"
    assert "Workflow failed steps: sql_pipeline" in status.error_message
    assert status.parameters["dry_run_only"] is True
    assert artifacts.step_outputs["sql_pipeline"]["sql_query"] == "SELECT 1"
    assert artifacts.metadata["execution"]["executed"] is False
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


@pytest.mark.filterwarnings("ignore")
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
            "parameters": {"dsn": raw_dsn, "note": "person@example.com"},
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


@pytest.mark.filterwarnings("ignore")
def test_workflow_manager_watchdog_marks_result_read_failure_explicit(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = streamlit_api.WorkflowManager(use_enhanced=False, pipelines_dir=str(tmp_path))
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text("name: watchdog_workflow\nsteps: []\n", encoding="utf-8")

    class WorkflowDef:
        name = "watchdog_workflow"
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

    class Process:
        pid = 12345
        exitcode = 0

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

        def join(self):
            return None

    multiprocessing = types.ModuleType("multiprocessing")
    multiprocessing.Process = Process
    monkeypatch.setitem(sys.modules, "multiprocessing", multiprocessing)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda _run_id, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("sqlite busy odbc_connect=UID=alice;PWD=topsecret person@example.com")
        ),
    )

    run_id = manager.start_workflow(
        "watchdog_workflow",
        parameters={},
        use_enhanced=False,
        run_id="wf-watchdog-read-error",
    )

    for _ in range(100):
        if manager.active_runs[run_id]["status"] == "failed":
            break
        streamlit_api.time.sleep(0.01)

    status = manager.get_workflow_status(run_id)
    serialized = json.dumps(status.__dict__, ensure_ascii=False, default=str)

    assert status.status == "failed"
    for raw_fragment in ("UID", "PWD", "alice", "topsecret", "person@example.com"):
        assert raw_fragment not in serialized
    assert "odbc_connect=***" in serialized
    assert "[EMAIL]" in serialized


@pytest.mark.filterwarnings("ignore")
def test_workflow_manager_watchdog_fails_on_success_exit_without_result(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = streamlit_api.WorkflowManager(use_enhanced=False, pipelines_dir=str(tmp_path))
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text("name: watchdog_missing_result\nsteps: []\n", encoding="utf-8")

    class WorkflowDef:
        name = "watchdog_missing_result"
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

    class Process:
        pid = 23456
        exitcode = 0

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

        def join(self):
            return None

    multiprocessing = types.ModuleType("multiprocessing")
    multiprocessing.Process = Process
    monkeypatch.setitem(sys.modules, "multiprocessing", multiprocessing)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda _run_id, **_kwargs: None,
    )

    run_id = manager.start_workflow(
        "watchdog_missing_result",
        parameters={},
        use_enhanced=False,
        run_id="wf-watchdog-missing-result",
    )

    for _ in range(100):
        if manager.active_runs[run_id]["status"] == "failed":
            break
        streamlit_api.time.sleep(0.01)

    status = manager.get_workflow_status(run_id)

    assert status.status == "failed"
    assert "WORKFLOW_RESULT" in status.error_message


@pytest.mark.filterwarnings("ignore")
def test_workflow_manager_start_reports_failed_result_append_failure(monkeypatch, tmp_path):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = streamlit_api.WorkflowManager(use_enhanced=False, pipelines_dir=str(tmp_path))
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text("name: start_append_failure\nsteps: []\n", encoding="utf-8")

    class WorkflowDef:
        name = "start_append_failure"
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

    class Process:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("spawn failed")

    multiprocessing = types.ModuleType("multiprocessing")
    multiprocessing.Process = Process
    monkeypatch.setitem(sys.modules, "multiprocessing", multiprocessing)
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(streamlit_api.WorkflowExecutionError, match="WORKFLOW_RESULT"):
        manager.start_workflow(
            "start_append_failure",
            parameters={},
            use_enhanced=False,
            run_id="wf-start-append-failure",
        )

    run_data = manager.active_runs["wf-start-append-failure"]
    assert run_data["status"] == "failed"
    assert "WORKFLOW_RESULT" in run_data["error"]


@pytest.mark.filterwarnings("ignore")
def test_workflow_manager_cancel_preserves_persisted_terminal_result(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = "run-process-terminal"
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
            "pid": 12345,
        }
    }
    manager.run_callbacks = {}
    appended: list[tuple] = []

    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda _run_id, **_kwargs: {
            "run_id": run_id,
            "status": "completed",
            "success": True,
            "result": {"message": "ok"},
            "artifacts": {"final_output": {"message": "ok"}},
            "snapshot": {"workflow_name": "text_to_sql_pipeline", "parameters": {}},
        },
    )
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: appended.append((args, kwargs)),
    )

    assert manager.cancel_workflow(run_id) is False
    assert manager.active_runs[run_id]["status"] == "completed"
    assert manager.active_runs[run_id]["final_output"] == {"message": "ok"}
    assert appended == []


@pytest.mark.filterwarnings("ignore")
def test_workflow_manager_cancel_rechecks_terminal_result_before_writing_cancel(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = "run-process-terminal-race"
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
    appended: list[tuple] = []
    terminal_payload = {
        "run_id": run_id,
        "status": "completed",
        "success": True,
        "result": {"message": "ok"},
        "artifacts": {"final_output": {"message": "ok"}},
        "snapshot": {"workflow_name": "text_to_sql_pipeline", "parameters": {}},
    }
    store_reads = iter([None, terminal_payload])

    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda _run_id, **_kwargs: next(store_reads),
    )
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: appended.append((args, kwargs)),
    )

    assert manager.cancel_workflow(run_id) is False
    assert manager.active_runs[run_id]["status"] == "completed"
    assert manager.active_runs[run_id]["final_output"] == {"message": "ok"}
    assert appended == []


@pytest.mark.filterwarnings("ignore")
def test_workflow_manager_cancel_does_not_persist_cancelled_when_process_survives(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = "run-process-still-alive"
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
            "pid": 43210,
        }
    }
    manager.run_callbacks = {}
    appended: list[tuple] = []

    class _AliveProcess:
        pid = 43210

        def terminate(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True

        def kill(self):
            pass

    monkeypatch.setattr(streamlit_api, "_workflow_result_payload_from_store", lambda _run_id, **_kwargs: None)
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: appended.append((args, kwargs)),
    )
    monkeypatch.setattr(streamlit_api.os, "killpg", lambda *_args, **_kwargs: (_ for _ in ()).throw(ProcessLookupError()))
    with streamlit_api._GLOBAL_WORKFLOW_PROCESSES_LOCK:
        streamlit_api._GLOBAL_WORKFLOW_PROCESSES[run_id] = _AliveProcess()
    try:
        assert manager.cancel_workflow(run_id) is False
        assert manager.active_runs[run_id]["status"] == "running"
        assert appended == []
    finally:
        with streamlit_api._GLOBAL_WORKFLOW_PROCESSES_LOCK:
            streamlit_api._GLOBAL_WORKFLOW_PROCESSES.pop(run_id, None)


@pytest.mark.filterwarnings("ignore")
def test_workflow_manager_cancel_does_not_cancel_on_initial_store_read_error(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = "run-store-read-error"
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
    appended: list[tuple] = []

    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda _run_id, **_kwargs: (_ for _ in ()).throw(RuntimeError("sqlite busy")),
    )
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: appended.append((args, kwargs)),
    )

    assert manager.cancel_workflow(run_id) is False
    assert manager.active_runs[run_id]["status"] == "running"
    assert appended == []


@pytest.mark.filterwarnings("ignore")
def test_workflow_manager_cancel_does_not_persist_cancelled_after_store_reread_fails(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = "run-store-reread-error"
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
            "pid": 54321,
        }
    }
    manager.run_callbacks = {}
    appended: list[tuple] = []
    store_reads = iter([None, RuntimeError("sqlite busy after kill")])

    class _StoppedProcess:
        pid = 54321

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

    def read_store(_run_id, **_kwargs):
        value = next(store_reads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(streamlit_api, "_workflow_result_payload_from_store", read_store)
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: appended.append((args, kwargs)),
    )
    monkeypatch.setattr(streamlit_api.os, "killpg", lambda *_args, **_kwargs: None)
    with streamlit_api._GLOBAL_WORKFLOW_PROCESSES_LOCK:
        streamlit_api._GLOBAL_WORKFLOW_PROCESSES[run_id] = _StoppedProcess()
    try:
        assert manager.cancel_workflow(run_id) is False
        assert manager.active_runs[run_id]["status"] == "running"
        assert appended == []
    finally:
        with streamlit_api._GLOBAL_WORKFLOW_PROCESSES_LOCK:
            streamlit_api._GLOBAL_WORKFLOW_PROCESSES.pop(run_id, None)


@pytest.mark.filterwarnings("ignore")
def test_workflow_manager_cancel_fails_when_cancelled_result_append_fails(monkeypatch):
    streamlit_api = _load_light_workflow_streamlit_api()
    manager = object.__new__(streamlit_api.WorkflowManager)
    run_id = "run-cancel-append-fails"
    manager.active_runs = {
        run_id: {
            "run_id": run_id,
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "start_time": streamlit_api.datetime.now(),
            "parameters": {},
            "pid": 65432,
        }
    }
    manager.run_callbacks = {}
    store_reads = iter([None, None])

    class _StoppedProcess:
        pid = 65432

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_payload_from_store",
        lambda _run_id, **_kwargs: next(store_reads),
    )
    monkeypatch.setattr(
        streamlit_api,
        "_append_workflow_result_event",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(streamlit_api.os, "killpg", lambda *_args, **_kwargs: None)
    with streamlit_api._GLOBAL_WORKFLOW_PROCESSES_LOCK:
        streamlit_api._GLOBAL_WORKFLOW_PROCESSES[run_id] = _StoppedProcess()
    try:
        assert manager.cancel_workflow(run_id) is False
        assert manager.active_runs[run_id]["status"] == "failed"
        assert "WORKFLOW_RESULT" in manager.active_runs[run_id]["error"]
        assert "last_cancelled" not in manager.active_runs[run_id]
        assert "last_failed" in manager.active_runs[run_id]
    finally:
        with streamlit_api._GLOBAL_WORKFLOW_PROCESSES_LOCK:
            streamlit_api._GLOBAL_WORKFLOW_PROCESSES.pop(run_id, None)


@pytest.mark.filterwarnings("ignore")
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


@pytest.mark.filterwarnings("ignore")
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


def test_text_to_sql_ui_history_uses_sql_fields_not_prompt_as_sql():
    react_source = Path("frontend/client/src/app/components/sections/TextToSqlSection.tsx").read_text(encoding="utf-8")
    streamlit_source = Path("streamlit_app/pages/05_Text_to_SQL.py").read_text(encoding="utf-8")
    service_source = Path("backend/fastapi_app/agui/service.py").read_text(encoding="utf-8")
    workflow_api_source = Path("workflow/streamlit_api.py").read_text(encoding="utf-8")

    assert "result?.parameters?.query" not in react_source
    assert "extractSqlFromText" not in react_source
    assert "step_outputs" in react_source
    assert "moderate" not in react_source
    assert "permissive" not in react_source
    assert "setMaxRows(Number" not in react_source
    assert 'type="number"' not in react_source
    assert "maxRows.trim()" in react_source
    assert "/^\\d+$/" in react_source
    assert "'allow_enhanced_fallback': False" in streamlit_source
    assert "_extract_sql_from_text" not in streamlit_source
    assert "_extract_sql_from_trace_line" not in streamlit_source
    assert "\"moderate\"" not in streamlit_source
    assert "\"permissive\"" not in streamlit_source
    assert "sql_query" in react_source
    assert "\"final_output\"" in streamlit_source
    assert "\"sql_query\"" in streamlit_source
    assert '"backend" / "data" / "agui_events.db"' not in service_source
    assert '"backend" / "data" / "agui_events.db"' not in workflow_api_source
    # EPIC 6.10: маршрут через AG-UI service action — обязателен.
    assert "handle_service_action(\"presets.text_to_sql.generate\"" in streamlit_source
    assert "from backend.fastapi_app.agui.service import handle_service_action" in streamlit_source


def test_streamlit_text_to_sql_options_rejects_non_integer_max_rows():
    source = Path("streamlit_app/pages/05_Text_to_SQL.py").read_text(encoding="utf-8")
    start = source.index("def _validate_text_to_sql_options")
    end = source.index("\ndef main", start)
    namespace = {"Any": Any}
    exec(source[start:end], namespace)

    validate = namespace["_validate_text_to_sql_options"]
    for value in [True, 1.9, "1.9", "1e2", ""]:
        with pytest.raises(ValueError, match="max_rows"):
            validate(value, "strict")

    assert validate("100", "strict") == (100, "strict")


def test_streamlit_structured_sql_extractor_reads_step_result_outputs():
    source = Path("streamlit_app/pages/05_Text_to_SQL.py").read_text(encoding="utf-8")
    start = source.index("def _extract_sql_from_structured_payload")
    end = source.index("\ndef generate_sql_query", start)
    namespace = {}
    exec(source[start:end], namespace)

    extractor = namespace["_extract_sql_from_structured_payload"]
    payload = {
        "sql_pipeline": _StepResultStub({
            "result": {
                "sql_query": "SELECT amount FROM orders"
            }
        })
    }

    assert extractor(payload) == "SELECT amount FROM orders"


def _extract_generate_sql_query_slice(source: str) -> str:
    """Возвращает срез исходника, относящийся к функции generate_sql_query."""
    start = source.index("def generate_sql_query")
    end = source.index("\ndef execute_sql_query", start)
    return source[start:end]


def test_streamlit_generate_sql_routes_through_service_action():
    """EPIC 6.10: generate_sql_query маршрутизирует Text-to-SQL через AG-UI service action.

    Запрещены прямые вызовы WorkflowEngine.execute_workflow и блок _temporary_env_var
    внутри generate_sql_query — резолв DSN/env переменных живёт на стороне backend.
    """
    source = Path("streamlit_app/pages/05_Text_to_SQL.py").read_text(encoding="utf-8")
    slice_text = _extract_generate_sql_query_slice(source)

    assert "from backend.fastapi_app.agui.service import handle_service_action" in slice_text
    assert "handle_service_action(\"presets.text_to_sql.generate\"" in slice_text
    assert "engine.execute_workflow(" not in slice_text
    assert "_temporary_env_var(\"DB_DSN\"" not in slice_text
    assert "_temporary_env_var(" not in slice_text
    assert "_WORKFLOW_ENV_LOCK" not in slice_text


def test_streamlit_generate_sql_payload_contract(monkeypatch):
    """exec-slice generate_sql_query: payload содержит whitelist параметров AG-UI."""
    source = Path("streamlit_app/pages/05_Text_to_SQL.py").read_text(encoding="utf-8")
    slice_text = _extract_generate_sql_query_slice(source)

    captured: dict = {}
    polling_status_holder = {"value": "running"}

    class _FakeArtifacts:
        def __init__(self):
            self.final_output = "report-body"
            self.step_outputs = {
                "nlu_processing": {"ok": True},
                "intent_extraction_step": {"ok": True},
                "schema_linking_step": {"ok": True},
                "sql_generation": {"sql_query": "SELECT 1"},
                "sql_verification": {"ok": True},
                "db_audit": {"sql_query": "SELECT 1"},
            }
            self.metadata = {}

    class _FakeStatus:
        def __init__(self, status: str):
            self.status = status
            self.step_results = {"db_audit": {"status": "completed"}}
            self.error_message = None

    class _FakeWorkflowManager:
        def __init__(self):
            captured["wf_manager_constructed"] = True

        def get_workflow_status(self, run_id):
            captured.setdefault("status_polls", []).append(run_id)
            polling_status_holder["value"] = "completed"
            return _FakeStatus(polling_status_holder["value"])

        def get_workflow_artifacts(self, run_id):
            captured["artifacts_fetched_for"] = run_id
            return _FakeArtifacts()

    def _fake_handle_service_action(action, payload):
        captured["action"] = action
        captured["payload"] = payload
        return {
            "run_id": "run-fixed-1234567890ab",
            "session_id": "sess-fixed",
            "workflow_name": payload.get("workflow_name"),
            "parameters": payload,
        }

    fake_streamlit = types.SimpleNamespace()

    class _Session(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as exc:
                raise AttributeError(name) from exc

        def __setattr__(self, name, value):
            self[name] = value

    session_state = _Session()
    session_state["selected_dsn"] = "sqlite:///tmp/app.db"
    session_state["generated_sql"] = ""
    session_state["sql_history"] = []
    fake_streamlit.session_state = session_state

    from contextlib import contextmanager as _cm

    @_cm
    def _noop_cm(*args, **kwargs):
        yield None

    fake_streamlit.spinner = _noop_cm
    fake_streamlit.expander = _noop_cm

    class _ColStub:
        def write(self, *_args, **_kwargs):
            return None

    def _columns(spec):
        if isinstance(spec, int):
            return [_ColStub() for _ in range(spec)]
        return [_ColStub() for _ in spec]

    fake_streamlit.columns = _columns
    fake_streamlit.error = lambda *a, **kw: None
    fake_streamlit.success = lambda *a, **kw: None
    fake_streamlit.warning = lambda *a, **kw: None
    fake_streamlit.info = lambda *a, **kw: None
    fake_streamlit.exception = lambda *a, **kw: None
    fake_streamlit.markdown = lambda *a, **kw: None

    fake_backend_pkg = types.ModuleType("backend")
    fake_backend_fastapi = types.ModuleType("backend.fastapi_app")
    fake_backend_agui = types.ModuleType("backend.fastapi_app.agui")
    fake_backend_service = types.ModuleType("backend.fastapi_app.agui.service")
    fake_backend_service.handle_service_action = _fake_handle_service_action
    monkeypatch.setitem(sys.modules, "backend", fake_backend_pkg)
    monkeypatch.setitem(sys.modules, "backend.fastapi_app", fake_backend_fastapi)
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui", fake_backend_agui)
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.service", fake_backend_service)

    fake_workflow_pkg = types.ModuleType("workflow")
    fake_workflow_streamlit = types.ModuleType("workflow.streamlit_api")
    fake_workflow_streamlit.WorkflowManager = _FakeWorkflowManager
    monkeypatch.setitem(sys.modules, "workflow", fake_workflow_pkg)
    monkeypatch.setitem(sys.modules, "workflow.streamlit_api", fake_workflow_streamlit)

    def _extract_sql_from_structured_payload(payload):
        if isinstance(payload, dict):
            sql = payload.get("sql_query")
            if isinstance(sql, str):
                return sql
            for v in payload.values():
                got = _extract_sql_from_structured_payload(v)
                if got:
                    return got
        return ""

    def _validate_text_to_sql_options(max_rows, safety_level):
        return int(max_rows), str(safety_level)

    namespace = {
        "st": fake_streamlit,
        "time": __import__("time"),
        "datetime": __import__("datetime").datetime,
        "_validate_text_to_sql_options": _validate_text_to_sql_options,
        "_extract_sql_from_structured_payload": _extract_sql_from_structured_payload,
        "save_to_history": lambda *a, **kw: None,
    }

    # Делаем time.sleep no-op чтобы polling не блокировал тест
    monkeypatch.setattr("time.sleep", lambda *_: None)

    exec(slice_text, namespace)
    generate_sql_query = namespace["generate_sql_query"]

    generate_sql_query(
        natural_query="show users",
        max_rows=7,
        safety_level="strict",
        include_explanation=True,
        validate_schema=False,
        dry_run_only=True,
        use_schema_suggestions=True,
    )

    assert captured["action"] == "presets.text_to_sql.generate"
    payload = captured["payload"]
    assert payload["query"] == "show users"
    assert payload["dsn"] == "sqlite:///tmp/app.db"
    assert payload["max_rows"] == 7
    assert payload["safety_level"] == "strict"
    assert payload["include_explanation"] is True
    assert payload["validate_schema"] is False
    assert payload["dry_run_only"] is True
    assert payload["use_schema_suggestions"] is True
    assert payload["allow_enhanced_fallback"] is False
    assert payload["workflow_name"] == "text_to_sql_pipeline"
    # session_id / run_id НЕ передаются клиентом — их вычисляет сервер
    assert "session_id" not in payload
    assert "run_id" not in payload

    # UI сохраняет run_id и session_id из ответа сервера
    stored = fake_streamlit.session_state["generated_sql"]
    assert stored["run_id"] == "run-fixed-1234567890ab"
    assert stored["session_id"] == "sess-fixed"
    assert stored["sql_query"] == "SELECT 1"
    assert stored["final_output"] == "report-body"
    assert stored["steps"]["db_audit"] == {"sql_query": "SELECT 1"}
    assert captured["artifacts_fetched_for"] == "run-fixed-1234567890ab"


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


@pytest.mark.filterwarnings("ignore")
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


@pytest.mark.filterwarnings("ignore")
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
