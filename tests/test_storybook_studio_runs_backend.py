"""Бэкенд-лаунчер прогона: подставные сервис и авторизация, без реальных воркфлоу."""

import asyncio
import json
import sys
import types

import pytest

from custom_tools.storybook.studio import run_exec, run_launchers
from custom_tools.storybook.studio.errors import StudioError
from tests.studio_run_fakes import FakeEngine, make_plan


class _Store:
    def __init__(self):
        self.created = []

    def create_run(self, run_id, session_id, principal, run_kind=None):
        self.created.append((run_id, session_id, principal, run_kind))


class _Manager:
    def __init__(self):
        self.started = []
        self.cancelled = []

    def start_workflow(self, **kwargs):
        self.started.append(kwargs)

    def cancel_workflow(self, run_id):
        self.cancelled.append(run_id)
        return True


@pytest.fixture
def backend(monkeypatch):
    store, manager = _Store(), _Manager()
    service = types.ModuleType("backend.fastapi_app.agui.service")
    service._agui_event_store = lambda: store
    service._wf_manager = lambda: manager
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.service", service)
    owner = types.SimpleNamespace(quota_identity="system")
    monkeypatch.setattr(run_launchers, "system_owner", lambda: ("principal", owner))
    return store, manager, owner


def test_start_backend_passes_plan_and_run_id(backend):
    store, manager, owner = backend
    plan = make_plan("p", ["a", "b"])
    run_launchers.start("p", plan, "run-1", "backend")
    assert store.created[0][0] == "run-1" and store.created[0][3] == "agui"
    call = manager.started[0]
    assert call["workflow_name"] == "storybook_pipeline" and call["run_id"] == "run-1"
    assert call["parameters"] == {"project_id": "p", "studio_plan": plan, "studio_run_id": "run-1"}
    assert call["owner"] is owner and call["use_enhanced"] is True


def test_cancel_backend_goes_through_manager(backend):
    _, manager, _ = backend
    assert run_launchers.cancel_backend("run-1") is True
    assert manager.cancelled == ["run-1"]


def test_start_backend_without_service_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.service", None)
    with pytest.raises(StudioError) as info:
        run_launchers.start_backend("p", make_plan("p", ["a"]), "run-2")
    assert info.value.code == "unavailable"


def test_unknown_launcher_is_invalid():
    with pytest.raises(StudioError) as info:
        run_launchers.start("p", make_plan("p", ["a"]), "run-3", "other")
    assert info.value.code == "invalid_argument"


def test_execute_plan_raw_with_held_lock_returns_workflow_result(tmp_path, monkeypatch):
    # так воркер бэкенда вызывает прогон: замок проекта уже взят снаружи
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project = tmp_path / "p"
    project.mkdir()
    (project / "00_brief.json").write_text(json.dumps({"title": "t"}))
    engine = FakeEngine(tmp_path)
    plan = make_plan("p", ["brief_from_prompt", "init_project"])
    result = asyncio.run(run_exec.execute_plan_raw(engine, plan, run_id="run-4", surface="react",
                                                   launcher="backend", lock_held=True))
    assert result.status.value == "completed"
    assert engine.executed == ["brief_from_prompt", "init_project"]


def test_web_worker_runs_studio_plan_instead_of_yaml(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from custom_tools.storybook.project_paths import acquire_storybook_project_lock
    from workflow import streamlit_api
    from workflow.models import WorkflowStatus

    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    manager = streamlit_api.WorkflowManager(use_enhanced=False)
    workflow_def = SimpleNamespace(name="storybook_pipeline", metadata={}, steps=[])
    monkeypatch.setattr(streamlit_api.WorkflowDefinition, "from_yaml", lambda _path: workflow_def)
    monkeypatch.setattr(streamlit_api, "_persist_workflow_result", lambda *a, **k: SimpleNamespace(
        persistence_succeeded=True, resolved_payload={}, candidate_won=True))
    seen = {}

    async def fake_raw(engine, plan, **kwargs):
        seen.update(plan=plan, **kwargs)
        assert acquire_storybook_project_lock("web-project") is None  # замок держит воркер
        return SimpleNamespace(status=WorkflowStatus.COMPLETED, workflow_id="w", final_output=None,
                               step_results={}, terminal_outcome=None)

    class _Engine:
        async def execute_workflow_from_yaml(self, *a, **k):
            raise AssertionError("YAML-путь не должен вызываться для плана студии")

    monkeypatch.setattr(run_exec, "execute_plan_raw", fake_raw)
    from custom_tools.storybook.studio import run_control
    taken = []
    real = run_control.acquire_lock_with_retries
    monkeypatch.setattr(run_control, "acquire_lock_with_retries", lambda pid: taken.append(pid) or real(pid))
    manager.engine = _Engine()
    plan = {"project_id": "web-project", "steps_to_run": ["x"]}
    manager._execute_workflow_in_context(
        "run-id", tmp_path / "storybook.yaml",
        {"project_id": "web-project", "studio_plan": plan, "studio_run_id": "run-9"}, "session-id")
    assert seen["plan"] == plan and seen["run_id"] == "run-9" and seen["lock_held"] is True
    assert taken == ["web-project"]  # замок взят через повторы
    released = acquire_storybook_project_lock("web-project")
    assert released is not None
    from custom_tools.storybook.project_paths import release_storybook_project_lock
    release_storybook_project_lock(released)


def test_start_local_does_not_leak_lock_when_engine_fails(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_control

    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "p").mkdir()

    def broken():
        raise RuntimeError("движок не поднялся")

    monkeypatch.setattr(run_exec, "get_engine", broken)
    with pytest.raises(RuntimeError):
        run_launchers.start_local("p", make_plan("p", ["brief_from_prompt"]), "tk-1")
    assert run_control.probe_lock(tmp_path / "p") is False
