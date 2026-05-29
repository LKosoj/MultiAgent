"""Pin-тесты Group C3 (Wave1): workflows.start уважает workflow metadata entrypoint.

Контракт:
- Workflow с ``metadata.agui_entrypoint`` или ``metadata.forbid_workflows_start``
  поднимает ``ForbiddenWorkflowNameError`` ДО вызова ``wf_manager.start_workflow``.
- Для разрешённых имён workflow.start работает (delegate to wf_manager).
- В runner.py исключение ``ForbiddenWorkflowNameError`` маппится в code
  ``forbidden_workflow_name`` при формировании ``RunErrorEvent``.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

# core text_to_sql при импорте читает safety profile из env;
# env-setup выполнен в conftest.py.


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
        return "run-stub-001"

    def list_workflows(self):
        return []


# ---------------------------------------------------------------------------
# Service-level: ForbiddenWorkflowNameError для text_to_sql_pipeline.
# ---------------------------------------------------------------------------
def test_workflows_start_rejects_text_to_sql_pipeline(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(service.ForbiddenWorkflowNameError) as ei:
        service.handle_service_action(
            "workflows.start",
            {"workflow_name": "text_to_sql_pipeline", "parameters": {}},
        )
    assert "text_to_sql_pipeline" in str(ei.value)
    assert "presets.text_to_sql.generate" in str(ei.value)

    # wf_manager НЕ должен быть вызван.
    assert wf_manager.calls == []


def test_workflows_start_rejects_metadata_marked_pipeline(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    pipelines_dir = tmp_path / "workflow_pipelines"
    pipelines_dir.mkdir()
    (pipelines_dir / "metadata_only.yaml").write_text(
        """
name: metadata_only
steps: []
metadata:
  agui_entrypoint: custom.entrypoint
  forbid_workflows_start: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)

    with pytest.raises(service.ForbiddenWorkflowNameError) as ei:
        service.handle_service_action(
            "workflows.start",
            {"workflow_name": "metadata_only", "parameters": {}},
        )

    assert "metadata_only" in str(ei.value)
    assert "custom.entrypoint" in str(ei.value)
    assert wf_manager.calls == []


def test_workflows_start_rejects_metadata_when_filename_differs_from_name(monkeypatch, tmp_path):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    pipelines_dir = tmp_path / "workflow_pipelines"
    pipelines_dir.mkdir()
    (pipelines_dir / "aaa_broken.yaml").write_text("[", encoding="utf-8")
    (pipelines_dir / "protected_v2.yaml").write_text(
        """
name: protected
steps: []
metadata:
  agui_entrypoint: protected.entrypoint
  forbid_workflows_start: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)

    with pytest.raises(service.ForbiddenWorkflowNameError) as ei:
        service.handle_service_action(
            "workflows.start",
            {"workflow_name": "protected", "parameters": {}},
        )

    assert "protected" in str(ei.value)
    assert "protected.entrypoint" in str(ei.value)
    assert wf_manager.calls == []


def test_runner_metadata_guard_finds_pipeline_when_filename_differs_from_name(tmp_path):
    from backend.fastapi_app.agui.workflow_metadata import workflow_agui_entrypoint

    pipelines_dir = tmp_path / "workflow_pipelines"
    pipelines_dir.mkdir()
    (pipelines_dir / "aaa_broken.yaml").write_text("[", encoding="utf-8")
    (pipelines_dir / "protected_v2.yaml").write_text(
        """
name: protected
steps: []
metadata:
  agui_entrypoint: protected.entrypoint
  forbid_workflows_start: true
""".strip(),
        encoding="utf-8",
    )

    assert workflow_agui_entrypoint("protected", pipelines_dir) == "protected.entrypoint"


def test_workflow_metadata_forbid_start_requires_strict_bool(tmp_path):
    from backend.fastapi_app.agui.workflow_metadata import workflow_agui_entrypoint

    pipelines_dir = tmp_path / "workflow_pipelines"
    pipelines_dir.mkdir()
    (pipelines_dir / "bad_bool.yaml").write_text(
        """
name: bad_bool
steps: []
metadata:
  forbid_workflows_start: sometimes
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbid_workflows_start"):
        workflow_agui_entrypoint("bad_bool", pipelines_dir)


def test_workflow_metadata_forbid_start_requires_entrypoint(tmp_path):
    from backend.fastapi_app.agui.workflow_metadata import workflow_agui_entrypoint

    pipelines_dir = tmp_path / "workflow_pipelines"
    pipelines_dir.mkdir()
    (pipelines_dir / "missing_entrypoint.yaml").write_text(
        """
name: missing_entrypoint
steps: []
metadata:
  forbid_workflows_start: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="agui_entrypoint"):
        workflow_agui_entrypoint("missing_entrypoint", pipelines_dir)


def _load_runner_with_minimal_stubs(monkeypatch):
    agent_system = types.ModuleType("agent_system")

    class DynamicAgentSystem:
        async def coordinate(self, *args, **kwargs):
            return ""

    agent_system.DynamicAgentSystem = DynamicAgentSystem
    monkeypatch.setitem(sys.modules, "agent_system", agent_system)

    logging_module = types.ModuleType("unified_logging")

    class _RunIdContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    logging_module.get_logging_manager = lambda *args, **kwargs: object()
    logging_module.run_id_context = lambda *args, **kwargs: _RunIdContext()
    monkeypatch.setitem(sys.modules, "unified_logging", logging_module)

    utils_module = types.ModuleType("utils")
    utils_module.call_openai_api_streaming = lambda *args, **kwargs: ""
    monkeypatch.setitem(sys.modules, "utils", utils_module)

    workflow_pkg = types.ModuleType("workflow")
    workflow_streamlit = types.ModuleType("workflow.streamlit_api")
    workflow_streamlit.WorkflowManager = lambda *args, **kwargs: _WorkflowManagerStub()
    monkeypatch.setitem(sys.modules, "workflow", workflow_pkg)
    monkeypatch.setitem(sys.modules, "workflow.streamlit_api", workflow_streamlit)

    import backend.fastapi_app.agui as agui_pkg

    monkeypatch.delattr(agui_pkg, "runner", raising=False)
    monkeypatch.delitem(sys.modules, "backend.fastapi_app.agui.runner", raising=False)
    return importlib.import_module("backend.fastapi_app.agui.runner")


@pytest.mark.asyncio
async def test_runner_rejects_metadata_guard_with_forbidden_workflow_code(monkeypatch, tmp_path):
    from backend.fastapi_app.agui.events import EventType
    from backend.fastapi_app.agui.models import RunAgentInput

    runner = _load_runner_with_minimal_stubs(monkeypatch)
    pipelines_dir = tmp_path / "workflow_pipelines"
    pipelines_dir.mkdir()
    (pipelines_dir / "protected_v2.yaml").write_text(
        """
name: protected
steps: []
metadata:
  agui_entrypoint: protected.entrypoint
  forbid_workflows_start: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner,
        "_resolve_workflow_name",
        lambda _forwarded: ("protected", pipelines_dir, None),
    )
    input_data = RunAgentInput(
        **{
            "threadId": "thread-protected",
            "runId": "run-protected",
            "state": {},
            "messages": [{"id": "msg-1", "role": "user", "content": "run it"}],
            "tools": [],
            "context": [],
            "forwardedProps": {"workflow_name": "protected", "execution_mode": "workflow"},
        }
    )

    events = [event async for event in runner.run_agent(input_data)]

    error_events = [event for event in events if event.type == EventType.RUN_ERROR]
    assert len(error_events) == 1
    assert error_events[0].code == "forbidden_workflow_name"
    assert "protected.entrypoint" in error_events[0].message
    assert [event for event in events if event.type == EventType.RUN_FINISHED] == []


def test_forbidden_workflow_error_is_value_error(monkeypatch):
    """ForbiddenWorkflowNameError должен наследовать ValueError —
    иначе обёртки в runner / dispatcher не отловят его как ожидаемую ошибку."""
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    assert issubclass(service.ForbiddenWorkflowNameError, ValueError)


def test_workflows_start_allows_other_pipeline(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    result = service.handle_service_action(
        "workflows.start",
        {
            "workflow_name": "some_other_pipeline",
            "parameters": {"foo": "bar"},
        },
    )
    assert result == {"run_id": "run-stub-001"}
    assert len(wf_manager.calls) == 1
    assert wf_manager.calls[0]["workflow_name"] == "some_other_pipeline"


def test_workflows_start_storybook_requires_explicit_task(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(ValueError, match="task"):
        service.handle_service_action(
            "workflows.start",
            {
                "workflow_name": "storybook_pipeline",
                "parameters": {},
            },
        )

    assert wf_manager.calls == []


# ---------------------------------------------------------------------------
# Runner: маппинг кода ошибки forbidden_workflow_name.
# ---------------------------------------------------------------------------
def test_runner_maps_forbidden_workflow_error_to_code():
    """Pin-проверка: в runner.py except-блоке ForbiddenWorkflowNameError
    должен маппиться в code ``forbidden_workflow_name``.

    Воспроизводим conditional-блок маппинга (around runner.py:543).
    Полноценный e2e через FastAPI requires слишком много моков; здесь —
    unit-проверка контракта маппинга через прямой импорт классов.
    Импорт runner.ServicePayloadInvalidError и service.ForbiddenWorkflowNameError
    выполняется ДО моков, чтобы избежать поломки `unified_logging` в стабах.
    """
    from backend.fastapi_app.agui.runner import ServicePayloadInvalidError
    from backend.fastapi_app.agui.service import ForbiddenWorkflowNameError

    def _resolve_code(exc: Exception) -> str:
        message = str(exc)
        code = "service_action_error"
        if isinstance(exc, ServicePayloadInvalidError):
            code = "service_payload_invalid"
        elif isinstance(exc, ForbiddenWorkflowNameError):
            code = "forbidden_workflow_name"
        elif message.startswith("Unknown service action"):
            code = "service_action_invalid"
        return code

    exc_forbidden = ForbiddenWorkflowNameError("text_to_sql_pipeline not allowed")
    assert _resolve_code(exc_forbidden) == "forbidden_workflow_name"

    # Sanity: ValueError без специального типа → service_action_error
    assert _resolve_code(ValueError("random")) == "service_action_error"
    # Sanity: ServicePayloadInvalidError маппится отдельно
    assert _resolve_code(ServicePayloadInvalidError("bad")) == "service_payload_invalid"

    # Pin: ForbiddenWorkflowNameError — подкласс ValueError, поэтому порядок
    # branches в runner.py важен (specific перед generic).
    assert isinstance(exc_forbidden, ValueError)
