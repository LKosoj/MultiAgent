from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import subprocess
import sys
import threading
import types

import pytest

from backend.fastapi_app.agui.auth import Principal, principal_context
from backend.fastapi_app.agui.events import EventType
from backend.fastapi_app.agui.models import RunAgentInput


@pytest.fixture(autouse=True)
def _clear_text_to_sql_llm_safety_cache():
    yield


def _principal() -> Principal:
    return Principal(
        subject="owner",
        tenant_id="tenant",
        roles=frozenset({"user"}),
    )


def _input() -> RunAgentInput:
    return RunAgentInput(
        **{
            "threadId": "outer-thread",
            "runId": "outer-run",
            "state": {},
            "messages": [
                {"id": "message-1", "role": "user", "content": "count orders"}
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {
                "service_action": "presets.text_to_sql.generate",
                "service_payload": {
                    "query": "count orders",
                    "dsn": "sqlite:///example.db",
                    "idempotency_key": "handoff-key",
                    "__request_id": "request-1",
                },
            },
        }
    )


def test_text_to_sql_imports_do_not_resolve_agent_runtime() -> None:
    script = """
import sys
import types

class AgentSystemSentinel(types.ModuleType):
    def __getattr__(self, name):
        if name == "DynamicAgentSystem":
            raise RuntimeError("DynamicAgentSystem resolved during Text-to-SQL import")
        raise AttributeError(name)

sys.modules["agent_system"] = AgentSystemSentinel("agent_system")
sys.modules["agent_streamlit_api"] = AgentSystemSentinel("agent_streamlit_api")
import backend.fastapi_app.agui.run_manager
import backend.fastapi_app.agui.service
assert "mcp_tools" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _install_service_stub(monkeypatch, handler) -> None:
    agent_system = types.ModuleType("agent_system")
    agent_system.DynamicAgentSystem = type("DynamicAgentSystem", (), {})
    monkeypatch.setitem(sys.modules, "agent_system", agent_system)
    unified_logging = types.ModuleType("unified_logging")
    unified_logging.get_logging_manager = lambda *_args, **_kwargs: None
    unified_logging.run_id_context = lambda *_args, **_kwargs: contextlib.nullcontext()
    monkeypatch.setitem(sys.modules, "unified_logging", unified_logging)
    utils = types.ModuleType("utils")
    utils.call_openai_api_streaming = lambda *_args, **_kwargs: ""
    monkeypatch.setitem(sys.modules, "utils", utils)
    service_module = types.ModuleType("backend.fastapi_app.agui.service")

    class ServiceTransportContext:
        def __init__(
            self,
            *,
            run_id,
            principal,
            cancellation_request_id=None,
            cancellation_provenance=None,
        ) -> None:
            self.run_id = run_id
            self.principal = principal
            self.cancellation_request_id = cancellation_request_id
            self.cancellation_provenance = cancellation_provenance

    service_module.ServiceTransportContext = ServiceTransportContext
    service_module.handle_service_action = handler
    monkeypatch.setitem(
        sys.modules,
        "backend.fastapi_app.agui.service",
        service_module,
    )


class _ReservationThenStartError:
    def __init__(self) -> None:
        self.reserved = False
        self.completed = False
        self.cancel_calls = 0
        self.status_calls = 0
        self.follow_entered = threading.Event()
        self.follow_gate = threading.Event()
        self._lock = threading.Lock()

    def __call__(
        self,
        action,
        payload,
        principal=None,
        *,
        transport_context=None,
    ):
        assert principal == _principal()
        assert payload.get("run_id", "outer-run") == "outer-run"
        if action == "presets.text_to_sql.generate":
            assert transport_context.run_id == "outer-run"
            self.reserved = True
            raise RuntimeError("start failed after reservation")
        if action == "workflows.status":
            with self._lock:
                self.status_calls += 1
                status_call = self.status_calls
            if status_call >= 3:
                self.follow_entered.set()
                assert self.follow_gate.wait(timeout=5)
            return {
                "status": {
                    "run_id": "outer-run",
                    "workflow_name": "text_to_sql_pipeline",
                    "session_id": "workflow-session",
                    "status": "completed" if self.completed else "running",
                    "result_seq": 1 if self.completed else None,
                    "invocation_registered": self.reserved,
                    "worker_pid": 12345,
                }
            }
        if action == "workflows.cancel":
            self.cancel_calls += 1
            return {"cancelled": False}
        if action == "workflows.result":
            if not self.completed:
                return {
                    "status": "running",
                    "success": False,
                    "terminal_outcome": None,
                }
            return {
                "status": "completed",
                "success": True,
                "terminal_outcome": {
                    "run_id": "outer-run",
                    "status": "succeeded",
                    "reason_code": "COMPLETED",
                },
            }
        raise AssertionError(action)


class _BlockedStartWithoutSideEffects:
    def __init__(self) -> None:
        self.start_entered = threading.Event()
        self.start_gate = threading.Event()
        self.start_finished = threading.Event()
        self.cancel_calls = 0

    def __call__(
        self,
        action,
        payload,
        principal=None,
        *,
        transport_context=None,
    ):
        assert principal == _principal()
        if action == "presets.text_to_sql.generate":
            assert transport_context.run_id == "outer-run"
            self.start_entered.set()
            try:
                assert self.start_gate.wait(timeout=5)
                raise RuntimeError("late detached start failure")
            finally:
                self.start_finished.set()
        if action == "workflows.status":
            return {
                "status": {
                    "run_id": "outer-run",
                    "status": "pending",
                    "result_seq": None,
                    "invocation_registered": False,
                    "worker_pid": None,
                }
            }
        if action == "workflows.cancel":
            self.cancel_calls += 1
            return {"cancelled": False}
        raise AssertionError(action)


class _ConfirmedCancellationAfterStartError:
    def __init__(self) -> None:
        self.reserved = False
        self.cancelled = False
        self.cancel_calls = 0

    def __call__(
        self,
        action,
        payload,
        principal=None,
        *,
        transport_context=None,
    ):
        assert principal == _principal()
        if action == "presets.text_to_sql.generate":
            assert transport_context.run_id == "outer-run"
            self.reserved = True
            raise RuntimeError("start failed after reservation")
        if action == "workflows.status":
            return {
                "status": {
                    "run_id": "outer-run",
                    "workflow_name": "text_to_sql_pipeline",
                    "session_id": "workflow-session",
                    "status": "cancelled" if self.cancelled else "queued",
                    "result_seq": 1 if self.cancelled else None,
                    "invocation_registered": self.reserved,
                    "worker_pid": None,
                }
            }
        if action == "workflows.cancel":
            self.cancel_calls += 1
            self.cancelled = True
            return {"cancelled": True}
        if action == "workflows.result":
            assert self.cancelled
            return {
                "status": "cancelled",
                "success": False,
                "terminal_outcome": {
                    "run_id": "outer-run",
                    "status": "cancelled",
                    "reason_code": "CANCELLED",
                },
            }
        raise AssertionError(action)


class _RetryableCancellationAfterStartError:
    def __init__(self, first_cancel_mode: str = "false") -> None:
        self.reserved = False
        self.cancelled = False
        self.cancel_calls = 0
        self.status_calls = 0
        self.first_cancel_mode = first_cancel_mode
        self.follow_entered = threading.Event()
        self.follow_gate = threading.Event()
        self.retry_follow_entered = threading.Event()
        self.cancel_gate = threading.Event()
        self.first_cancel_finished = threading.Event()
        self.active_cancel_calls = 0
        self.max_active_cancel_calls = 0
        self._lock = threading.Lock()

    def __call__(
        self,
        action,
        payload,
        principal=None,
        *,
        transport_context=None,
    ):
        assert principal == _principal()
        if action == "presets.text_to_sql.generate":
            assert transport_context.run_id == "outer-run"
            self.reserved = True
            raise RuntimeError("start failed after reservation")
        if action == "workflows.status":
            self.status_calls += 1
            if self.status_calls == 3:
                self.follow_entered.set()
                if self.first_cancel_mode == "timeout":
                    assert self.follow_gate.wait(timeout=5)
            if self.status_calls >= 5:
                self.retry_follow_entered.set()
            return {
                "status": {
                    "run_id": "outer-run",
                    "workflow_name": "text_to_sql_pipeline",
                    "session_id": "workflow-session",
                    "status": "cancelled" if self.cancelled else "queued",
                    "result_seq": 1 if self.cancelled else None,
                    "invocation_registered": self.reserved,
                    "worker_pid": None,
                }
            }
        if action == "workflows.cancel":
            with self._lock:
                self.cancel_calls += 1
                call_number = self.cancel_calls
                self.active_cancel_calls += 1
                self.max_active_cancel_calls = max(
                    self.max_active_cancel_calls,
                    self.active_cancel_calls,
                )
            try:
                if call_number == 1:
                    if self.first_cancel_mode == "exception":
                        raise RuntimeError("cancel transport failed")
                    if self.first_cancel_mode == "timeout":
                        assert self.cancel_gate.wait(timeout=5)
                        raise RuntimeError("late cancel transport failure")
                    return {"cancelled": False}
                self.cancelled = True
                return {"cancelled": True}
            finally:
                with self._lock:
                    self.active_cancel_calls -= 1
                if call_number == 1:
                    self.first_cancel_finished.set()
        if action == "workflows.result":
            assert self.cancelled
            return {
                "status": "cancelled",
                "success": False,
                "terminal_outcome": {
                    "run_id": "outer-run",
                    "status": "cancelled",
                    "reason_code": "CANCELLED",
                },
            }
        raise AssertionError(action)


@pytest.mark.asyncio
async def test_start_error_after_worker_handoff_keeps_following_without_cancel(
    monkeypatch,
):
    service = _ReservationThenStartError()
    _install_service_stub(monkeypatch, service)
    import backend.fastapi_app.agui.runner as runner_module

    monkeypatch.setattr(runner_module, "get_logging_manager", lambda *_args: None)
    monkeypatch.setattr(runner_module, "_WORKFLOW_CANCEL_OBSERVE_SECONDS", 0)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0)

    async def collect_events():
        with principal_context(_principal()):
            return [event async for event in runner_module.run_agent(_input())]

    task = asyncio.create_task(collect_events())
    try:
        assert await asyncio.to_thread(service.follow_entered.wait, 2)
        assert task.done() is False
        assert service.cancel_calls == 0

        service.completed = True
        service.follow_gate.set()
        events = await asyncio.wait_for(task, timeout=2)

        start_results = [
            event
            for event in events
            if event.type == EventType.CUSTOM and event.name == "service.result"
        ]
        terminals = [
            event
            for event in events
            if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
        ]
        assert len(start_results) == 1
        assert start_results[0].value["ok"] is True
        assert start_results[0].value["data"]["run_id"] == "outer-run"
        assert [event.type for event in terminals] == [EventType.RUN_FINISHED]
    finally:
        service.follow_gate.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_cancel_during_permanently_blocked_start_is_bounded_and_nonterminal(
    monkeypatch,
):
    service = _BlockedStartWithoutSideEffects()
    _install_service_stub(monkeypatch, service)
    import backend.fastapi_app.agui.runner as runner_module

    monkeypatch.setattr(runner_module, "get_logging_manager", lambda *_args: None)
    monkeypatch.setattr(runner_module, "_WORKFLOW_START_HANDOFF_SECONDS", 0.01)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0)
    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    events = []

    async def collect_events():
        with principal_context(_principal()):
            async for event in runner_module.run_agent(_input()):
                events.append(event)

    task = asyncio.create_task(collect_events())
    try:
        assert await asyncio.to_thread(service.start_entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert service.cancel_calls == 0
        assert not any(
            event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
            for event in events
        )

        service.start_gate.set()
        assert await asyncio.to_thread(service.start_finished.wait, 2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not any(
            context.get("message") == "Task exception was never retrieved"
            for context in unhandled
        )
    finally:
        service.start_gate.set()
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_start_error_with_confirmed_cancellation_emits_one_cancelled_terminal(
    monkeypatch,
):
    service = _ConfirmedCancellationAfterStartError()
    _install_service_stub(monkeypatch, service)
    import backend.fastapi_app.agui.runner as runner_module

    monkeypatch.setattr(runner_module, "get_logging_manager", lambda *_args: None)
    monkeypatch.setattr(runner_module, "_WORKFLOW_CANCEL_OBSERVE_SECONDS", 0.1)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0)

    with principal_context(_principal()):
        events = [event async for event in runner_module.run_agent(_input())]

    workflow_results = [
        event
        for event in events
        if event.type == EventType.CUSTOM and event.name == "workflow.result"
    ]
    terminals = [
        event
        for event in events
        if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
    ]
    assert service.cancel_calls == 1
    assert len(workflow_results) == 1
    assert workflow_results[0].value["terminal_outcome"]["status"] == "cancelled"
    assert [event.type for event in terminals] == [EventType.RUN_ERROR]
    assert terminals[0].code == "text_to_sql_cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_cancel_mode", ["false", "exception"])
async def test_completed_retryable_cancel_dispatch_can_be_retried_without_overlap(
    monkeypatch,
    first_cancel_mode,
):
    _install_service_stub(monkeypatch, None)
    import backend.fastapi_app.agui.runner as runner_module

    service = _RetryableCancellationAfterStartError(first_cancel_mode)
    sys.modules["backend.fastapi_app.agui.service"].handle_service_action = service
    monkeypatch.setattr(runner_module, "get_logging_manager", lambda *_args: None)
    monkeypatch.setattr(runner_module, "_WORKFLOW_CANCEL_OBSERVE_SECONDS", 0)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0)

    events = []

    async def collect_events():
        with principal_context(_principal()):
            async for event in runner_module.run_agent(_input()):
                events.append(event)

    task = asyncio.create_task(collect_events())
    assert await asyncio.to_thread(service.follow_entered.wait, 2)
    monkeypatch.setattr(runner_module, "_WORKFLOW_CANCEL_OBSERVE_SECONDS", 0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    terminals = [
        event
        for event in events
        if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
    ]
    assert service.cancel_calls == 2
    assert terminals == []
    workflow_results = [
        event
        for event in events
        if event.type == EventType.CUSTOM and event.name == "workflow.result"
    ]
    assert len(workflow_results) == 1
    assert workflow_results[0].value["terminal_outcome"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_timeout_keeps_runner_dispatch_singleflight_until_late_exception(
    monkeypatch,
):
    _install_service_stub(monkeypatch, None)
    import backend.fastapi_app.agui.runner as runner_module

    service = _RetryableCancellationAfterStartError("timeout")
    sys.modules["backend.fastapi_app.agui.service"].handle_service_action = service
    monkeypatch.setattr(runner_module, "get_logging_manager", lambda *_args: None)
    monkeypatch.setattr(runner_module, "_WORKFLOW_CANCEL_OBSERVE_SECONDS", 0)
    monkeypatch.setattr(runner_module, "_WORKFLOW_CANCEL_REQUEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0)

    async def collect_events():
        with principal_context(_principal()):
            return [event async for event in runner_module.run_agent(_input())]

    task = asyncio.create_task(collect_events())
    try:
        assert await asyncio.to_thread(service.follow_entered.wait, 2)
        assert service.cancel_calls == 1
        assert service.max_active_cancel_calls == 1

        task.cancel()
        assert await asyncio.to_thread(service.retry_follow_entered.wait, 2)
        assert task.done() is False
        assert service.cancel_calls == 1
        assert service.max_active_cancel_calls == 1

        service.cancel_gate.set()
        assert await asyncio.to_thread(service.first_cancel_finished.wait, 2)
        monkeypatch.setattr(runner_module, "_WORKFLOW_CANCEL_OBSERVE_SECONDS", 0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        assert service.cancel_calls == 2
        assert service.max_active_cancel_calls == 1
    finally:
        service.cancel_gate.set()
        service.follow_gate.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
