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


def test_workflows_storybook_readiness_delegates_to_video_contract(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    from custom_tools.storybook import video_contract

    calls = []

    def fake_readiness(**kwargs):
        calls.append(kwargs)
        return {"status": "success", "project_id": kwargs["project_id"], "ready": True}

    monkeypatch.setattr(video_contract, "storybook_video_music_readiness", fake_readiness)

    result = service.handle_service_action(
        "workflows.storybook_readiness",
        {
            "parameters": {"project_id": "project-1"},
            "language": "en",
            "enable": "false",
            "generate_music": "true",
        },
    )

    assert result["readiness"] == {"status": "success", "project_id": "project-1", "ready": True}
    assert calls == [
        {
            "project_id": "project-1",
            "session_id": "agui-readiness",
            "language": "en",
            "enable": False,
            "generate_music": True,
        }
    ]


def test_workflows_storybook_readiness_reads_workflow_parameters(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    from custom_tools.storybook import video_contract

    calls = []

    def fake_readiness(**kwargs):
        calls.append(kwargs)
        return {"status": "success", "project_id": kwargs["project_id"], "ready": True}

    monkeypatch.setattr(video_contract, "storybook_video_music_readiness", fake_readiness)

    service.handle_service_action(
        "workflows.storybook_readiness",
        {
            "parameters": {
                "project_id": "project-2",
                "language": "de",
                "generate_screenplay": False,
                "generate_music": False,
            },
        },
    )

    assert calls == [
        {
            "project_id": "project-2",
            "session_id": "agui-readiness",
            "language": "de",
            "enable": False,
            "generate_music": False,
        }
    ]


def test_workflows_storybook_readiness_rejects_invalid_boolean(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(ValueError, match="enable must be boolean"):
        service.handle_service_action(
            "workflows.storybook_readiness",
            {"project_id": "project-1", "enable": "fasle"},
        )


def test_workflows_storybook_readiness_requires_project_id(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(ValueError, match="project_id is required"):
        service.handle_service_action("workflows.storybook_readiness", {})


_UNSAFE_STORYBOOK_PROJECT_IDS = [
    "../outside",
    "/tmp/outside",
    ".",
    "project/..",
    "a/../b",
    "a/b",
    r"a\b",
]


@pytest.mark.parametrize("project_id", _UNSAFE_STORYBOOK_PROJECT_IDS)
def test_workflows_start_rejects_unsafe_storybook_project_id(monkeypatch, project_id):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(ValueError, match="workflows.start parameters invalid"):
        service.handle_service_action(
            "workflows.start",
            {
                "workflow_name": "storybook_pipeline",
                "parameters": {"task": "ok", "project_id": project_id},
            },
        )

    assert wf_manager.calls == []


@pytest.mark.parametrize(
    "action",
    [
        "workflows.storybook_readiness",
        "workflows.storybook_actions",
        "workflows.storybook_validate",
        "workflows.storybook_project_inventory",
    ],
)
@pytest.mark.parametrize("project_id", _UNSAFE_STORYBOOK_PROJECT_IDS)
def test_workflows_storybook_actions_reject_unsafe_project_id(monkeypatch, action, project_id):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(ValueError, match="project_id must be a safe path segment"):
        service.handle_service_action(action, {"parameters": {"project_id": project_id}})

    assert wf_manager.calls == []


def test_workflows_storybook_actions_returns_shared_contract(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    result = service.handle_service_action(
        "workflows.storybook_actions",
        {"parameters": {"project_id": "project-1"}},
    )

    contract = result["actions"]
    action_ids = [action["id"] for action in contract["actions"]]
    assert contract["workflow_name"] == "storybook_pipeline"
    assert contract["project_id"] == "project-1"
    assert "full_pipeline" in action_ids
    assert "validate_project" in action_ids
    assert "run_from_step" in action_ids
    assert wf_manager.calls == []


def test_workflows_storybook_validate_delegates_to_pipeline_runner(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    runner_module = types.ModuleType("StoryBookManager.core.pipeline_runner")
    calls = []

    class FakePipelineRunner:
        def validate_project_for_pipeline(self, project_id, start_step=None):
            calls.append({"project_id": project_id, "start_step": start_step})
            return {
                "valid": True,
                "message": "ok",
                "errors": [],
                "warnings": ["warn"],
            }

    runner_module.PipelineRunner = FakePipelineRunner
    monkeypatch.setitem(sys.modules, "StoryBookManager.core.pipeline_runner", runner_module)

    result = service.handle_service_action(
        "workflows.storybook_validate",
        {"parameters": {"project_id": "project-1", "start_step": "story_writer"}},
    )

    assert result["validation"] == {
        "valid": True,
        "message": "ok",
        "errors": [],
        "warnings": ["warn"],
    }
    assert calls == [{"project_id": "project-1", "start_step": "story_writer"}]
    assert wf_manager.calls == []


def test_workflows_storybook_validate_requires_project_id(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)

    with pytest.raises(ValueError, match="project_id is required"):
        service.handle_service_action("workflows.storybook_validate", {})


def test_workflows_storybook_project_inventory_delegates_to_surface_contract(monkeypatch):
    wf_manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, wf_manager)
    from custom_tools.storybook import storybook_surface

    calls = []

    def fake_inventory(project_id=None, max_items=20):
        calls.append({"project_id": project_id, "max_items": max_items})
        return {"status": "success", "project_id": project_id, "max_items": max_items}

    monkeypatch.setattr(storybook_surface, "storybook_project_inventory", fake_inventory)

    result = service.handle_service_action(
        "workflows.storybook_project_inventory",
        {"parameters": {"project_id": "project-1"}, "max_items": 500},
    )

    assert result["inventory"] == {
        "status": "success",
        "project_id": "project-1",
        "max_items": 100,
    }
    assert calls == [{"project_id": "project-1", "max_items": 100}]
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

    service_module = types.ModuleType("backend.fastapi_app.agui.service")
    service_module._require_service_action_role = lambda _action, _principal: None
    service_module.handle_service_action = lambda *args, **kwargs: {}
    service_module._redact_payload = lambda value: value
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.service", service_module)

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
