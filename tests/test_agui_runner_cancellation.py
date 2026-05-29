"""W9-A12: runner должен emit workflow envelope с code="cancelled" при отмене.

См. backend/fastapi_app/agui/runner.py::_run_workflow и
doc/AG_UI_SERVICE_ACTIONS.md секция «Cancellation envelope».
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
    status_sequence: list[_StubWorkflowStatus] | None = None

    def __init__(self, **_kwargs) -> None:
        self.start_calls: list[dict] = []
        self.cancel_calls: list[str] = []
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

    workflow_pkg = types.ModuleType("workflow")
    workflow_streamlit = types.ModuleType("workflow.streamlit_api")

    def _factory(**kwargs):
        manager = _StubWorkflowManager(**kwargs)
        manager_holder.append(manager)
        return manager

    workflow_streamlit.WorkflowManager = _factory
    workflow_streamlit._GLOBAL_WORKFLOW_PROCESSES = {}
    monkeypatch.setitem(sys.modules, "workflow", workflow_pkg)
    monkeypatch.setitem(sys.modules, "workflow.streamlit_api", workflow_streamlit)

    sys.modules.pop("backend.fastapi_app.agui.runner", None)
    return importlib.import_module("backend.fastapi_app.agui.runner")


@pytest.mark.asyncio
async def test_cancel_envelope_contains_code_cancelled(monkeypatch):
    """W9-A12: при отмене runner должен эмитить envelope с code="cancelled" до re-raise."""
    _StubWorkflowManager.status_sequence = [
        _StubWorkflowStatus("running", current_step="step_loop", progress_percentage=10.0),
    ]
    try:
        manager_holder: list[_StubWorkflowManager] = []
        runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)
        monkeypatch.setattr(runner, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.01)

        agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
        payload = _make_payload(
            agui_run_id,
            {"workflow_name": "demo_pipeline", "execution_mode": "workflow"},
        )
        input_data = RunAgentInput(**payload)
        forwarded = payload["forwardedProps"]

        collected: list = []

        async def _collect():
            async for item in runner._run_workflow(input_data, "task", forwarded):
                collected.append(item)
                # После первого события (started) запрашиваем отмену.
                if len(collected) == 1:
                    task.cancel()

        task = asyncio.create_task(_collect())
        with pytest.raises(asyncio.CancelledError):
            await task

        # Должен быть эмитнут CustomEvent workflow.result с status=cancelled
        # и envelope-полем code="cancelled" (до re-raise CancelledError).
        cancel_events = [
            item for item in collected
            if hasattr(item, "type") and item.type == EventType.CUSTOM and item.name == "workflow.result"
        ]
        assert len(cancel_events) == 1, (
            f"expected 1 workflow.result event on cancel, got {len(cancel_events)}"
        )
        envelope = cancel_events[0].value
        assert isinstance(envelope, dict)
        assert envelope.get("type") == "workflow_result"
        assert envelope.get("status") == "cancelled"
        assert envelope.get("code") == "cancelled", (
            f"envelope must contain code='cancelled', got envelope={envelope!r}"
        )
        # final_output для cancel — null (контракт из doc).
        assert envelope.get("final_output") is None
        # artifacts_ref присутствует, но None для cancel.
        assert "artifacts_ref" in envelope
        assert envelope["artifacts_ref"] is None
    finally:
        _StubWorkflowManager.status_sequence = None


@pytest.mark.asyncio
async def test_cancel_after_terminal_workflow_status_returns_final_envelope(monkeypatch):
    """Disconnect cancel must not rewrite an already-completed workflow to cancelled."""
    _StubWorkflowManager.status_sequence = [
        _StubWorkflowStatus("running", current_step="step_loop", progress_percentage=10.0),
        _StubWorkflowStatus("completed", progress_percentage=100.0),
    ]

    class _CompletedManager(_StubWorkflowManager):
        def cancel_workflow(self, run_id: str) -> bool:
            self.cancel_calls.append(run_id)
            return False

        def get_workflow_artifacts(self, run_id: str):
            return _StubWorkflowArtifacts(final_output="already done")

    try:
        manager_holder: list[_CompletedManager] = []
        runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)

        def _factory(**kwargs):
            mgr = _CompletedManager(**kwargs)
            manager_holder.append(mgr)
            return mgr

        monkeypatch.setattr(sys.modules["workflow.streamlit_api"], "WorkflowManager", _factory)
        monkeypatch.setattr(runner, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.01)

        agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
        payload = _make_payload(
            agui_run_id,
            {"workflow_name": "demo_pipeline", "execution_mode": "workflow"},
        )
        input_data = RunAgentInput(**payload)
        forwarded = payload["forwardedProps"]
        collected: list = []

        async def _collect():
            async for item in runner._run_workflow(input_data, "task", forwarded):
                collected.append(item)
                if len(collected) == 2:
                    task.cancel()

        task = asyncio.create_task(_collect())
        await task

        assert manager_holder, "WorkflowManager должен быть создан"
        assert manager_holder[0].cancel_calls == []
        final_envelopes = [item for item in collected if isinstance(item, dict)]
        assert len(final_envelopes) == 1
        envelope = final_envelopes[0]
        assert envelope["status"] == "completed"
        assert envelope["final_output"] == "already done"
        assert "code" not in envelope
        cancel_events = [
            item for item in collected
            if hasattr(item, "type") and item.type == EventType.CUSTOM and item.name == "workflow.result"
        ]
        assert cancel_events == []
    finally:
        _StubWorkflowManager.status_sequence = None


@pytest.mark.asyncio
async def test_cancel_rejected_running_status_raises_without_workflow_result(monkeypatch):
    """cancel_workflow=False with still-running status must not emit terminal workflow.result."""
    _StubWorkflowManager.status_sequence = [
        _StubWorkflowStatus("running", current_step="step_loop", progress_percentage=10.0),
    ]

    class _RejectedCancelManager(_StubWorkflowManager):
        def cancel_workflow(self, run_id: str) -> bool:
            self.cancel_calls.append(run_id)
            return False

    try:
        manager_holder: list[_RejectedCancelManager] = []
        runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)

        def _factory(**kwargs):
            mgr = _RejectedCancelManager(**kwargs)
            manager_holder.append(mgr)
            return mgr

        monkeypatch.setattr(sys.modules["workflow.streamlit_api"], "WorkflowManager", _factory)
        monkeypatch.setattr(runner, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.01)

        agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
        payload = _make_payload(
            agui_run_id,
            {"workflow_name": "demo_pipeline", "execution_mode": "workflow"},
        )
        input_data = RunAgentInput(**payload)
        collected: list = []

        async def _collect():
            async for item in runner._run_workflow(input_data, "task", payload["forwardedProps"]):
                collected.append(item)
                task.cancel()

        task = asyncio.create_task(_collect())
        with pytest.raises(RuntimeError, match="did not reach terminal status|not accepted"):
            await task

        assert manager_holder and manager_holder[0].cancel_calls
        assert [
            item for item in collected
            if hasattr(item, "type") and item.type == EventType.CUSTOM and item.name == "workflow.result"
        ] == []
    finally:
        _StubWorkflowManager.status_sequence = None


@pytest.mark.asyncio
async def test_cancel_false_with_terminal_cancelled_still_emits_cancel_envelope(monkeypatch):
    """cancel_workflow=False is acceptable when manager already reports cancelled."""
    _StubWorkflowManager.status_sequence = [
        _StubWorkflowStatus("running", current_step="step_loop", progress_percentage=10.0),
    ]

    class _AlreadyCancelledManager(_StubWorkflowManager):
        def cancel_workflow(self, run_id: str) -> bool:
            self.cancel_calls.append(run_id)
            self._status_iter = [_StubWorkflowStatus("cancelled")]
            return False

    try:
        manager_holder: list[_AlreadyCancelledManager] = []
        runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)

        def _factory(**kwargs):
            mgr = _AlreadyCancelledManager(**kwargs)
            manager_holder.append(mgr)
            return mgr

        monkeypatch.setattr(sys.modules["workflow.streamlit_api"], "WorkflowManager", _factory)
        monkeypatch.setattr(runner, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.01)

        agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
        payload = _make_payload(
            agui_run_id,
            {"workflow_name": "demo_pipeline", "execution_mode": "workflow"},
        )
        input_data = RunAgentInput(**payload)
        collected: list = []

        async def _collect():
            async for item in runner._run_workflow(input_data, "task", payload["forwardedProps"]):
                collected.append(item)
                task.cancel()

        task = asyncio.create_task(_collect())
        with pytest.raises(asyncio.CancelledError):
            await task

        cancel_events = [
            item for item in collected
            if hasattr(item, "type") and item.type == EventType.CUSTOM and item.name == "workflow.result"
        ]
        assert len(cancel_events) == 1
        assert cancel_events[0].value["status"] == "cancelled"
        assert cancel_events[0].value["code"] == "cancelled"
    finally:
        _StubWorkflowManager.status_sequence = None


@pytest.mark.asyncio
async def test_success_envelope_does_not_contain_code(monkeypatch):
    """W9-A12 regression: success envelope не должен содержать поле code (additive-only)."""
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
        assert len(finished) == 1
        envelope = finished[0].result
        assert isinstance(envelope, dict)
        assert envelope.get("status") == "completed"
        # Ключ "code" не должен присутствовать в success envelope.
        assert "code" not in envelope, (
            f"success envelope must not contain 'code' field, got {envelope!r}"
        )
    finally:
        _StubWorkflowManager.status_sequence = None


@pytest.mark.asyncio
async def test_manager_reported_cancelled_emits_cancel_event_without_run_finished(monkeypatch):
    """WorkflowManager status=cancelled must use cancel contract, not success finish."""
    _StubWorkflowManager.status_sequence = [
        _StubWorkflowStatus("cancelled", progress_percentage=20.0),
    ]

    class _CancelledManager(_StubWorkflowManager):
        def get_workflow_artifacts(self, run_id: str):
            return _StubWorkflowArtifacts(final_output="should not be emitted")

    try:
        manager_holder: list = []
        runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)

        def _factory(**kwargs):
            mgr = _CancelledManager(**kwargs)
            manager_holder.append(mgr)
            return mgr

        wf_module = sys.modules["workflow.streamlit_api"]
        monkeypatch.setattr(wf_module, "WorkflowManager", _factory)
        monkeypatch.setattr(runner, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.0)

        agui_run_id = f"run-{uuid.uuid4().hex[:8]}"
        payload = _make_payload(
            agui_run_id,
            {"workflow_name": "demo_pipeline", "execution_mode": "workflow"},
        )
        input_data = RunAgentInput(**payload)
        events = []

        with pytest.raises(asyncio.CancelledError):
            async for event in runner.run_agent(input_data):
                events.append(event)

        cancel_events = [
            event
            for event in events
            if event.type == EventType.CUSTOM and event.name == "workflow.result"
        ]
        assert len(cancel_events) == 1
        envelope = cancel_events[0].value
        assert envelope["status"] == "cancelled"
        assert envelope["code"] == "cancelled"
        assert envelope["final_output"] is None
        assert envelope["artifacts_ref"] is None
        assert "should not be emitted" not in str(envelope)
        assert [event for event in events if event.type == EventType.RUN_FINISHED] == []
    finally:
        _StubWorkflowManager.status_sequence = None


@pytest.mark.asyncio
async def test_failed_envelope_does_not_contain_code(monkeypatch):
    """W9-A12 regression: failed envelope не должен содержать поле code."""
    _StubWorkflowManager.status_sequence = [
        _StubWorkflowStatus("failed", error_message="boom", progress_percentage=42.0),
    ]

    class _FailingManager(_StubWorkflowManager):
        def get_workflow_artifacts(self, run_id: str):
            return _StubWorkflowArtifacts(final_output=None)

    try:
        manager_holder: list = []
        runner = _load_runner_with_workflow_stub(monkeypatch, manager_holder)

        def _factory(**kwargs):
            mgr = _FailingManager(**kwargs)
            manager_holder.append(mgr)
            return mgr

        wf_module = sys.modules["workflow.streamlit_api"]
        monkeypatch.setattr(wf_module, "WorkflowManager", _factory)
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
        assert "code" not in envelope, (
            f"failed envelope must not contain 'code' field, got {envelope!r}"
        )
    finally:
        _StubWorkflowManager.status_sequence = None
