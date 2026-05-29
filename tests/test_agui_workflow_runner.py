"""Тесты T3.1: AG-UI runner должен использовать отдельный workflow run_id.

См. backend/fastapi_app/agui/runner.py::_run_workflow.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import uuid

import pytest

from backend.fastapi_app.agui.events import EventType
from backend.fastapi_app.agui.models import RunAgentInput


def _make_payload(run_id: str, forwarded: dict) -> dict:
    return {
        "threadId": f"thread-{run_id}",
        "runId": run_id,
        "state": {},
        "messages": [
            {"id": "msg-1", "role": "user", "content": "show users"},
        ],
        "tools": [],
        "context": [],
        "forwardedProps": forwarded,
    }


class _StubWorkflowStatus:
    def __init__(
        self,
        status: str,
        error_message: str | None = None,
        current_step: str | None = None,
        progress_percentage: float = 0.0,
    ) -> None:
        self.status = status
        self.error_message = error_message
        self.current_step = current_step
        self.progress_percentage = progress_percentage


class _StubWorkflowArtifacts:
    def __init__(self, final_output) -> None:
        self.final_output = final_output


class _StubWorkflow:
    def __init__(self, name: str) -> None:
        self.name = name


class _StubWorkflowManager:
    """Минимальная подмена ``workflow.streamlit_api.WorkflowManager``."""

    # Класс-атрибут: переопределяется в тестах прогресса/cancel.
    status_sequence: list[_StubWorkflowStatus] | None = None

    def __init__(self, **_kwargs) -> None:
        self.start_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        # Локальная копия последовательности, иначе разные инстансы конфликтуют.
        if type(self).status_sequence is not None:
            self._status_iter = list(type(self).status_sequence)
        else:
            self._status_iter = None

    def list_workflows(self):
        return [_StubWorkflow("demo_pipeline")]

    def start_workflow(self, **kwargs):
        self.start_calls.append(kwargs)
        run_id = kwargs.get("run_id")
        if not run_id:
            raise AssertionError("runner must pass explicit run_id to start_workflow")
        return run_id

    def get_workflow_status(self, run_id: str):
        if self._status_iter:
            if len(self._status_iter) > 1:
                return self._status_iter.pop(0)
            return self._status_iter[0]
        return _StubWorkflowStatus("completed")

    def get_workflow_artifacts(self, run_id: str):
        return _StubWorkflowArtifacts(final_output="ok")

    def cancel_workflow(self, run_id: str) -> bool:
        self.cancel_calls.append(run_id)
        self._status_iter = [_StubWorkflowStatus("cancelled")]
        return True


def _load_runner_with_workflow_stub(monkeypatch, manager_holder: list):
    """Изолированно перезагружает runner, подменяя тяжёлые зависимости."""

    stub_agent_system = types.ModuleType("agent_system")

    class DynamicAgentSystem:
        async def coordinate(self, *args, **kwargs):
            return ""

    stub_agent_system.DynamicAgentSystem = DynamicAgentSystem
    monkeypatch.setitem(sys.modules, "agent_system", stub_agent_system)

    stub_service = types.ModuleType("backend.fastapi_app.agui.service")
    stub_service.handle_service_action = lambda *_a, **_k: {}
    stub_service._redact_payload = lambda value: value
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.service", stub_service)

    stub_logging = types.ModuleType("unified_logging")

    class RunIdContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    stub_logging.get_logging_manager = lambda *args, **kwargs: types.SimpleNamespace()
    stub_logging.run_id_context = lambda *args, **kwargs: RunIdContext()
    monkeypatch.setitem(sys.modules, "unified_logging", stub_logging)

    stub_utils = types.ModuleType("utils")
    stub_utils.call_openai_api_streaming = lambda *args, **kwargs: ""
    monkeypatch.setitem(sys.modules, "utils", stub_utils)

    # Подмена пакета workflow.streamlit_api
    workflow_pkg = types.ModuleType("workflow")
    workflow_streamlit = types.ModuleType("workflow.streamlit_api")

    def _factory(**kwargs):
        manager = _StubWorkflowManager(**kwargs)
        manager_holder.append(manager)
        return manager

    workflow_streamlit.WorkflowManager = _factory
    # Реестр процессов: runner импортирует его в ветке cancel.
    workflow_streamlit._GLOBAL_WORKFLOW_PROCESSES = {}
    monkeypatch.setitem(sys.modules, "workflow", workflow_pkg)
    monkeypatch.setitem(sys.modules, "workflow.streamlit_api", workflow_streamlit)

    sys.modules.pop("backend.fastapi_app.agui.runner", None)
    return importlib.import_module("backend.fastapi_app.agui.runner")


@pytest.mark.asyncio
async def test_run_workflow_uses_distinct_workflow_run_id_and_emits_started_event(monkeypatch):
    manager_holder: list[_StubWorkflowManager] = []
    runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)

    agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(
        agui_run_id,
        {"workflow_name": "demo_pipeline", "execution_mode": "workflow"},
    )
    input_data = RunAgentInput(**payload)

    events = [event async for event in runner.run_agent(input_data)]

    # WorkflowManager должен быть создан ровно один раз
    assert len(manager_holder) == 1
    manager = manager_holder[0]
    assert len(manager.start_calls) == 1
    call = manager.start_calls[0]

    # AG-UI run_id передан как session_id, а workflow run_id — отдельный
    assert call["session_id"] == agui_run_id
    workflow_run_id = call["run_id"]
    assert workflow_run_id != agui_run_id
    assert workflow_run_id.startswith("run-")

    # Должен быть эмитнут CustomEvent workflow.started с корректным payload
    custom_events = [
        event for event in events if event.type == EventType.CUSTOM
    ]
    started_events = [event for event in custom_events if event.name == "workflow.started"]
    assert len(started_events) == 1, f"expected 1 workflow.started event, got {len(started_events)}"
    started = started_events[0]
    assert started.value == {
        "workflow_run_id": workflow_run_id,
        "workflow_name": "demo_pipeline",
        "session_id": agui_run_id,
    }


@pytest.mark.asyncio
async def test_run_workflow_validates_storybook_task_before_start(monkeypatch):
    manager_holder: list[_StubWorkflowManager] = []
    runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)

    agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(
        agui_run_id,
        {
            "workflow_name": "storybook_pipeline",
            "execution_mode": "workflow",
            "variables": {"task": "   "},
        },
    )
    payload["messages"][0]["content"] = "   "

    events = [event async for event in runner.run_agent(RunAgentInput(**payload))]

    error_events = [event for event in events if event.type == EventType.RUN_ERROR]
    assert len(error_events) == 1
    assert error_events[0].code == "execution_error"
    assert "forwardedProps parameters invalid for 'storybook_pipeline'" in error_events[0].message
    assert "task" in error_events[0].message
    assert manager_holder == [] or manager_holder[0].start_calls == []


@pytest.mark.asyncio
async def test_run_workflow_emits_progress_events(monkeypatch):
    """T3.2: runner должен эмитить CustomEvent workflow.progress при изменении статуса."""
    # Последовательность: сначала running с одним шагом, затем running с другим
    # шагом, затем completed. Должно получиться ≥ 1 workflow.progress (минимум —
    # переход к completed; на практике также переход running→running по шагам).
    _StubWorkflowManager.status_sequence = [
        _StubWorkflowStatus("running", current_step="step_1", progress_percentage=25.0),
        _StubWorkflowStatus("running", current_step="step_2", progress_percentage=60.0),
        _StubWorkflowStatus("completed", progress_percentage=100.0),
    ]
    try:
        manager_holder: list[_StubWorkflowManager] = []
        runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)
        # Ускоряем polling, чтобы тест не висел секундами.
        monkeypatch.setattr(runner, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.0)

        agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
        payload = _make_payload(
            agui_run_id,
            {"workflow_name": "demo_pipeline", "execution_mode": "workflow"},
        )
        input_data = RunAgentInput(**payload)

        events = [event async for event in runner.run_agent(input_data)]

        progress_events = [
            event
            for event in events
            if event.type == EventType.CUSTOM and event.name == "workflow.progress"
        ]
        assert progress_events, "expected at least one workflow.progress event"
        # Все progress содержат workflow_run_id и status
        for ev in progress_events:
            assert "workflow_run_id" in ev.value
            assert "status" in ev.value
        # Должен быть переход к терминальному статусу
        statuses = [ev.value["status"] for ev in progress_events]
        assert "completed" in statuses
    finally:
        _StubWorkflowManager.status_sequence = None


@pytest.mark.asyncio
async def test_run_workflow_cancel_calls_cancel_workflow_and_joins_child(monkeypatch):
    """T3.2: при cancel runner должен вызвать cancel_workflow и дождаться завершения процесса."""
    # Workflow «бесконечно» крутится в статусе running, пока не придёт cancel.
    _StubWorkflowManager.status_sequence = [
        _StubWorkflowStatus("running", current_step="step_loop", progress_percentage=10.0),
    ]
    try:
        manager_holder: list[_StubWorkflowManager] = []
        runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)
        monkeypatch.setattr(runner, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.01)

        # Поддельный процесс с join()/is_alive() для проверки взаимодействия.
        class _FakeProc:
            def __init__(self) -> None:
                self._alive = True
                self.join_calls: list[float | None] = []

            def is_alive(self) -> bool:
                return self._alive

            def join(self, timeout=None):
                self.join_calls.append(timeout)
                # Симулируем что cancel_workflow дождался выхода процесса.
                self._alive = False

        # Подменяем cancel_workflow так, чтобы он повторил контракт реального
        # WorkflowManager: убил процесс через proc.join и удалил его из реестра.
        from workflow.streamlit_api import _GLOBAL_WORKFLOW_PROCESSES

        fake_proc = _FakeProc()

        def _cancel_workflow(run_id: str):
            proc = _GLOBAL_WORKFLOW_PROCESSES.get(run_id)
            if proc is not None:
                proc.join(timeout=5.0)
                _GLOBAL_WORKFLOW_PROCESSES.pop(run_id, None)
            # Записываем как делает существующий стаб.
            manager = manager_holder[0]
            manager.cancel_calls.append(run_id)
            manager._status_iter = [_StubWorkflowStatus("cancelled")]
            return True

        agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
        payload = _make_payload(
            agui_run_id,
            {"workflow_name": "demo_pipeline", "execution_mode": "workflow"},
        )
        input_data = RunAgentInput(**payload)

        # Запускаем _run_workflow напрямую как асинхронную задачу и отменяем её,
        # как только runner успеет start_workflow + первый progress.
        forwarded = payload["forwardedProps"]

        async def _collect():
            collected = []
            async for item in runner._run_workflow(input_data, "task", forwarded):
                collected.append(item)
                # После старта/первого прогресса регистрируем поддельный процесс
                # и просим отмену.
                if len(collected) == 1 and manager_holder:
                    started = manager_holder[0].start_calls[0]
                    _GLOBAL_WORKFLOW_PROCESSES[started["run_id"]] = fake_proc
                    # Подменяем cancel_workflow на интрумент с join'ом
                    manager_holder[0].cancel_workflow = _cancel_workflow
                    task.cancel()
            return collected

        task = asyncio.create_task(_collect())
        with pytest.raises(asyncio.CancelledError):
            await task

        assert manager_holder, "WorkflowManager должен быть создан"
        manager = manager_holder[0]
        workflow_run_id = manager.start_calls[0]["run_id"]
        # cancel_workflow вызван
        assert workflow_run_id in manager.cancel_calls
        # proc.join был вызван хотя бы один раз (в _cancel_workflow или повторно
        # из runner'а на финальной проверке).
        assert fake_proc.join_calls, "ожидался proc.join во время cancel"
        assert not fake_proc.is_alive(), "процесс должен быть мёртв после cancel"
    finally:
        _StubWorkflowManager.status_sequence = None


@pytest.mark.asyncio
async def test_run_workflow_fails_fast_when_workflow_name_not_found(monkeypatch):
    manager_holder: list[_StubWorkflowManager] = []
    runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)

    agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(
        agui_run_id,
        {"workflow_name": "missing_pipeline", "execution_mode": "workflow"},
    )
    input_data = RunAgentInput(**payload)

    events = [event async for event in runner.run_agent(input_data)]

    error_events = [event for event in events if event.type == EventType.RUN_ERROR]
    assert len(error_events) == 1
    assert error_events[0].code == "workflow_not_found"
    # start_workflow вызываться не должен
    assert manager_holder == [] or manager_holder[0].start_calls == []


@pytest.mark.asyncio
async def test_service_action_workflows_start_missing_workflow_maps_not_found(monkeypatch):
    manager_holder: list[_StubWorkflowManager] = []
    runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)
    service_module = sys.modules["backend.fastapi_app.agui.service"]

    def fail_workflow_start(action, payload):
        assert action == "workflows.start"
        assert payload["workflow_name"] == "missing_pipeline"
        raise ValueError("Пайплайн 'missing_pipeline' не найден")

    service_module.handle_service_action = fail_workflow_start

    agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(
        agui_run_id,
        {
            "service_action": "workflows.start",
            "service_payload": {"workflow_name": "missing_pipeline"},
        },
    )
    input_data = RunAgentInput(**payload)

    events = [event async for event in runner.run_agent(input_data)]

    error_events = [event for event in events if event.type == EventType.RUN_ERROR]
    assert len(error_events) == 1
    assert error_events[0].code == "workflow_not_found"


@pytest.mark.asyncio
async def test_run_workflow_run_finished_contains_envelope(monkeypatch):
    """T3.3: RUN_FINISHED.result должен содержать workflow envelope."""
    _StubWorkflowManager.status_sequence = [
        _StubWorkflowStatus("completed", progress_percentage=100.0),
    ]
    try:
        manager_holder: list[_StubWorkflowManager] = []
        runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)
        monkeypatch.setattr(runner, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.0)

        agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
        payload = _make_payload(
            agui_run_id,
            {"workflow_name": "demo_pipeline", "execution_mode": "workflow"},
        )
        input_data = RunAgentInput(**payload)

        events = [event async for event in runner.run_agent(input_data)]

        finished = [e for e in events if e.type == EventType.RUN_FINISHED]
        assert len(finished) == 1, f"expected 1 RUN_FINISHED, got {len(finished)}"
        envelope = finished[0].result
        assert isinstance(envelope, dict), f"result must be dict, got {type(envelope)}"
        assert envelope.get("type") == "workflow_result"
        assert envelope.get("workflow_name") == "demo_pipeline"
        assert envelope.get("status") == "completed"
        assert envelope.get("final_output") == "ok"
        # artifacts_ref должен присутствовать как ключ; значение None — это валидно
        # (manager не выставил logs_path/traces_path), но ключ должен быть.
        assert "artifacts_ref" in envelope
        assert envelope["artifacts_ref"] is None

        # workflow_run_id должен совпадать с тем, что отдал start_workflow
        manager = manager_holder[0]
        workflow_run_id = manager.start_calls[0]["run_id"]
        assert envelope.get("workflow_run_id") == workflow_run_id

        # CUSTOM workflow.result должен быть эмитнут перед RUN_FINISHED с тем же envelope
        custom_results = [
            e for e in events
            if e.type == EventType.CUSTOM and e.name == "workflow.result"
        ]
        assert len(custom_results) == 1
        assert custom_results[0].value == envelope
    finally:
        _StubWorkflowManager.status_sequence = None


@pytest.mark.asyncio
async def test_run_workflow_failed_envelope_has_status_failed(monkeypatch):
    """T3.3: при status=failed envelope должен содержать status: failed."""
    _StubWorkflowManager.status_sequence = [
        _StubWorkflowStatus("failed", error_message="boom", progress_percentage=42.0),
    ]

    # Подкласс, который возвращает None final_output (как реальный manager при failure).
    class _FailingManager(_StubWorkflowManager):
        def get_workflow_artifacts(self, run_id: str):
            return _StubWorkflowArtifacts(final_output=None)

    try:
        manager_holder: list[_FailingManager] = []
        # Перезагружаем runner с собственным factory; используем уже
        # настроенные _load_runner_with_workflow_stub-стабы для прочих модулей.
        runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)

        def _factory(**kwargs):
            mgr = _FailingManager(**kwargs)
            manager_holder.append(mgr)
            return mgr

        wf_module = sys.modules["workflow.streamlit_api"]
        monkeypatch.setattr(wf_module, "WorkflowManager", _factory)
        # После замены фабрики нужно перезагрузить runner, чтобы импортированный
        # WorkflowManager внутри _run_workflow тоже стал новым (он импортируется
        # внутри функции, так что повторная перезагрузка runner-модуля не нужна).
        monkeypatch.setattr(runner, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.0)

        agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
        payload = _make_payload(
            agui_run_id,
            {"workflow_name": "demo_pipeline", "execution_mode": "workflow"},
        )
        input_data = RunAgentInput(**payload)

        events = [event async for event in runner.run_agent(input_data)]

        finished = [e for e in events if e.type == EventType.RUN_FINISHED]
        assert len(finished) == 1
        envelope = finished[0].result
        assert isinstance(envelope, dict)
        assert envelope.get("status") == "failed"
        # final_output для failed — error_message из status (источник: реальное поле)
        assert envelope.get("final_output") == "boom"
        assert envelope.get("type") == "workflow_result"
        assert envelope.get("workflow_name") == "demo_pipeline"
    finally:
        _StubWorkflowManager.status_sequence = None


@pytest.mark.asyncio
async def test_run_workflow_rejects_text_to_sql_pipeline_via_forwarded_props(monkeypatch):
    """T3.4: text_to_sql_pipeline через forwardedProps должен быть отклонён.

    Единственный поддерживаемый путь — service action
    ``presets.text_to_sql.generate``; forwardedProps-путь обходит валидацию
    max_rows/safety_level/DSN-резолвинг (см. AGENTS.md, doc/AG_UI_SERVICE_ACTIONS.md).
    """

    manager_holder: list[_StubWorkflowManager] = []
    runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)

    agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
    payload = _make_payload(
        agui_run_id,
        {
            "workflow_name": "text_to_sql_pipeline",
            "execution_mode": "workflow",
            # Эти variables не должны быть прочитаны — fail-fast до старта.
            "variables": {"query": "show users", "dsn": "sqlite:///tmp/app.db"},
        },
    )
    input_data = RunAgentInput(**payload)

    events = [event async for event in runner.run_agent(input_data)]

    error_events = [event for event in events if event.type == EventType.RUN_ERROR]
    assert len(error_events) == 1, f"expected 1 RUN_ERROR, got {len(error_events)}"
    assert error_events[0].code == "text_to_sql_must_use_service_action"
    assert "presets.text_to_sql.generate" in error_events[0].message
    # WorkflowManager не должен создаваться/стартовать ничего.
    assert manager_holder == [] or manager_holder[0].start_calls == []
    # RUN_FINISHED не должен быть эмитнут.
    assert not [e for e in events if e.type == EventType.RUN_FINISHED]
