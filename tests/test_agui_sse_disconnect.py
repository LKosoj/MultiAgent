"""SSE disconnect lifecycle для AG-UI run.

Проверяем два инварианта:
1. ``RunManager.subscriber_count`` корректно отражает число активных live-подписчиков
   до/после ``stream_live.aclose()``.
2. Gateway cancel-on-disconnect отменяет run, если live-подписчиков больше нет,
   но не трогает run при наличии другого подписчика.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import time
import types
import uuid

import pytest

from backend.fastapi_app.agui.events import CustomEvent
from backend.fastapi_app.agui.events import EventType
from backend.fastapi_app.agui.events import RunFinishedEvent
from backend.fastapi_app.agui.encoder import EventEncoder
from backend.fastapi_app.agui.models import RunAgentInput
from backend.fastapi_app.agui.store import EventStore


def _make_payload(run_id: str) -> dict:
    return {
        "threadId": f"thread-{run_id}",
        "runId": run_id,
        "state": {},
        "messages": [
            {"id": "msg-1", "role": "user", "content": "stream"},
        ],
        "tools": [],
        "context": [],
        "forwardedProps": {
            "service_action": "logs.stream",
            "service_payload": {"run_id": "*", "duration_seconds": 30},
        },
    }


def _install_runner_stub(monkeypatch) -> None:
    """Подменяет тяжёлые зависимости runner.py на лёгкие стабы.

    Зеркалит логику ``_load_runner_with_service_stub`` из test_ag_ui_gateway.py, но
    оставляет реальный runner.run_agent: нам нужны настоящие _stream_logs/
    _stream_progress, потому что мы проверяем именно их отмену.
    """
    stub_agent_system = types.ModuleType("agent_system")

    class DynamicAgentSystem:
        async def coordinate(self, *args, **kwargs):
            return ""

    stub_agent_system.DynamicAgentSystem = DynamicAgentSystem
    monkeypatch.setitem(sys.modules, "agent_system", stub_agent_system)

    stub_service = types.ModuleType("backend.fastapi_app.agui.service")
    class ForbiddenWorkflowNameError(ValueError):
        pass

    stub_service.ForbiddenWorkflowNameError = ForbiddenWorkflowNameError
    stub_service.handle_service_action = lambda *a, **k: {}
    stub_service._redact_payload = lambda value: value
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.service", stub_service)

    stub_logging = types.ModuleType("unified_logging")

    class _LoggingManager:
        def __init__(self) -> None:
            self.subscribed: list[tuple[str, object]] = []
            self.unsubscribed: list[tuple[str, object]] = []

        def subscribe_all_logs(self, callback):
            self.subscribed.append(("all_logs", callback))

        def unsubscribe_all_logs(self, callback):
            self.unsubscribed.append(("all_logs", callback))

        def subscribe_run_logs(self, run_id, callback):
            self.subscribed.append((f"run_logs:{run_id}", callback))

        def unsubscribe_run_logs(self, run_id, callback):
            self.unsubscribed.append((f"run_logs:{run_id}", callback))

        def subscribe_all_progress(self, callback):
            self.subscribed.append(("all_progress", callback))

        def unsubscribe_all_progress(self, callback):
            self.unsubscribed.append(("all_progress", callback))

        def subscribe_run_progress(self, run_id, callback):
            self.subscribed.append((f"run_progress:{run_id}", callback))

        def unsubscribe_run_progress(self, run_id, callback):
            self.unsubscribed.append((f"run_progress:{run_id}", callback))

    manager_instance = _LoggingManager()

    class _RunIdContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    stub_logging.get_logging_manager = lambda *a, **k: manager_instance
    stub_logging.run_id_context = lambda *a, **k: _RunIdContext()
    monkeypatch.setitem(sys.modules, "unified_logging", stub_logging)

    stub_utils = types.ModuleType("utils")
    stub_utils.call_openai_api_streaming = lambda *a, **k: ""
    monkeypatch.setitem(sys.modules, "utils", stub_utils)

    # Перезагрузить runner и run_manager, чтобы они подцепили стабы.
    monkeypatch.delitem(sys.modules, "backend.fastapi_app.agui.runner", raising=False)
    monkeypatch.delitem(sys.modules, "backend.fastapi_app.agui.run_manager", raising=False)
    importlib.import_module("backend.fastapi_app.agui.runner")
    importlib.import_module("backend.fastapi_app.agui.run_manager")

    # Возвращаем manager_instance через атрибут модуля для интроспекции в тесте.
    stub_logging._manager_instance = manager_instance


@pytest.mark.asyncio
async def test_subscriber_count_decrements_after_aclose(tmp_path, monkeypatch):
    """После aclose у stream_live подписчик должен быть удалён из info.subscribers."""
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app.agui.run_manager import RunManager

    manager = RunManager(EventStore(str(tmp_path / "agui.db")))
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))

    # Дать run-task стартануть до RUNNING (RunStartedEvent публикуется первым).
    await asyncio.sleep(0.05)

    stream = manager.stream_live(run_id)
    # Дёрнем итератор один раз — гарантирует, что подписчик зарегистрирован.
    first_event = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    assert first_event.type == EventType.RUN_STARTED
    assert manager.subscriber_count(run_id) == 1

    await stream.aclose()
    assert manager.subscriber_count(run_id) == 0

    # Тушим run, чтобы не оставлять висящий task между тестами.
    await manager.cancel(run_id)
    info = manager.get_info(run_id)
    if info is not None and info.task is not None:
        with pytest.raises((asyncio.CancelledError, Exception)):
            await info.task


@pytest.mark.asyncio
async def test_gateway_disconnect_cancels_orphaned_run(tmp_path, monkeypatch):
    """Disconnect единственного live SSE-подписчика отменяет orphaned run."""
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app.agui.run_manager import RunManager, RunStatus
    from backend.fastapi_app import main as main_module
    import unified_logging as stub_logging

    logging_manager = stub_logging._manager_instance

    manager = RunManager(EventStore(str(tmp_path / "agui.db")))
    monkeypatch.setattr(main_module, "run_manager", manager)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))

    # Воспроизводим логику event_stream из main.py: читаем live-события,
    # после первого секунды «отваливаемся» и закрываем стрим.
    disconnected = False
    stream = manager.stream_live(run_id)
    started = time.monotonic()
    try:
        async for event in stream:
            # Получаем хотя бы RUN_STARTED, дальше эмулируем disconnect.
            if event.type == EventType.RUN_STARTED:
                await asyncio.sleep(0.2)  # короткая «работа» клиента
                disconnected = True
                break
    finally:
        await stream.aclose()

    assert disconnected
    assert manager.subscriber_count(run_id) == 0

    cancel_started = time.monotonic()
    await main_module._cancel_if_orphaned(run_id)

    # Ожидаем терминальный статус.
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        info = manager.get_info(run_id)
        if info is not None and info.status == RunStatus.CANCELLED:
            break
        await asyncio.sleep(0.01)

    info = manager.get_info(run_id)
    assert info is not None
    assert info.status == RunStatus.CANCELLED, f"status={info.status}, elapsed={time.monotonic()-cancel_started:.3f}s"
    elapsed = time.monotonic() - cancel_started
    assert elapsed < 0.5, f"cancel took {elapsed:.3f}s, expected <0.5s"

    # Дать finally в _stream_logs дорасти до unsubscribe.
    await asyncio.sleep(0.05)

    # Главная проверка: callback отписан, нет утечки в logging_manager.
    unsubscribe_keys = [key for key, _cb in logging_manager.unsubscribed]
    subscribe_keys = [key for key, _cb in logging_manager.subscribed]
    assert "all_logs" in subscribe_keys, f"subscribed={subscribe_keys}"
    assert "all_logs" in unsubscribe_keys, f"unsubscribed={unsubscribe_keys}"

    total_elapsed = time.monotonic() - started
    assert total_elapsed < 1.5, f"whole disconnect+explicit-cancel cycle took {total_elapsed:.3f}s"


@pytest.mark.asyncio
async def test_agent_stream_aclose_cancels_orphaned_run(tmp_path, monkeypatch):
    """ASGI close while awaiting more events still cancels the orphaned run."""
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app.agui.run_manager import RunManager, RunStatus
    from backend.fastapi_app import main as main_module

    manager = RunManager(EventStore(str(tmp_path / "agui.db")))
    monkeypatch.setattr(main_module, "run_manager", manager)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))

    class _Request:
        async def is_disconnected(self):
            return False

    events = main_module._stream_agent_events(run_id, _Request(), manager.encoder(None))
    first_payload = await asyncio.wait_for(events.__anext__(), timeout=1.0)
    assert "RUN_STARTED" in first_payload
    assert manager.subscriber_count(run_id) == 1

    await events.aclose()
    assert manager.subscriber_count(run_id) == 0

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        info = manager.get_info(run_id)
        if info is not None and info.status == RunStatus.CANCELLED:
            break
        await asyncio.sleep(0.01)

    info = manager.get_info(run_id)
    assert info is not None
    assert info.status == RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_agent_stream_aclose_after_terminal_event_does_not_cancel(monkeypatch):
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app import main as main_module

    class _Request:
        async def is_disconnected(self):
            return False

    class _RunManager:
        def stream_live(self, run_id):
            async def _events():
                yield RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    thread_id="thread-terminal",
                    run_id=run_id,
                    result={"ok": True},
                    timestamp=1,
                )

            return _events()

    cancelled = False

    async def _cancel_if_orphaned(_run_id):
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr(main_module, "run_manager", _RunManager())
    monkeypatch.setattr(main_module, "_cancel_if_orphaned", _cancel_if_orphaned)

    events = main_module._stream_agent_events("run-terminal", _Request(), EventEncoder())
    payload = await asyncio.wait_for(events.__anext__(), timeout=1.0)
    assert "RUN_FINISHED" in payload

    await events.aclose()

    assert cancelled is False


@pytest.mark.asyncio
async def test_agent_stream_aclose_after_terminal_workflow_progress_does_not_cancel(monkeypatch):
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app import main as main_module

    class _Request:
        async def is_disconnected(self):
            return False

    class _RunManager:
        def stream_live(self, run_id):
            async def _events():
                yield CustomEvent(
                    type=EventType.CUSTOM,
                    name="workflow.progress",
                    value={
                        "workflow_run_id": run_id,
                        "status": "completed",
                        "progress_percentage": 100.0,
                    },
                    timestamp=1,
                )

            return _events()

    cancelled = False

    async def _cancel_if_orphaned(_run_id):
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr(main_module, "run_manager", _RunManager())
    monkeypatch.setattr(main_module, "_cancel_if_orphaned", _cancel_if_orphaned)

    events = main_module._stream_agent_events("run-terminal-progress", _Request(), EventEncoder())
    payload = await asyncio.wait_for(events.__anext__(), timeout=1.0)
    assert "workflow.progress" in payload

    await events.aclose()

    assert cancelled is False


@pytest.mark.asyncio
async def test_agent_stream_aclose_after_service_result_does_not_cancel(monkeypatch):
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app import main as main_module

    class _Request:
        async def is_disconnected(self):
            return False

    class _RunManager:
        def stream_live(self, run_id):
            async def _events():
                yield CustomEvent(
                    type=EventType.CUSTOM,
                    name="service.result",
                    value={"ok": True, "data": {"value": 1}},
                    timestamp=1,
                )

            return _events()

    cancelled = False

    async def _cancel_if_orphaned(_run_id):
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr(main_module, "run_manager", _RunManager())
    monkeypatch.setattr(main_module, "_cancel_if_orphaned", _cancel_if_orphaned)

    events = main_module._stream_agent_events("run-service-result", _Request(), EventEncoder())
    payload = await asyncio.wait_for(events.__anext__(), timeout=1.0)
    assert "service.result" in payload

    await events.aclose()

    assert cancelled is False


@pytest.mark.asyncio
async def test_replay_follow_aclose_does_not_cancel_detached_run(monkeypatch):
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app import main as main_module

    class _Request:
        headers = {}

        async def is_disconnected(self):
            return False

    class _Store:
        def list_after(self, run_id, after):
            return []

    class _LiveStream:
        def __init__(self):
            self.closed = False
            self._yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                await asyncio.sleep(60)
            self._yielded = True
            return CustomEvent(
                type=EventType.CUSTOM,
                name="demo.event",
                value={"ok": True},
                timestamp=1,
            )

        async def aclose(self):
            self.closed = True

    live_stream = _LiveStream()

    class _RunManager:
        def get_info(self, run_id):
            return object()

        def stream_live(self, run_id, after=0):
            return live_stream

    cancelled: list[str] = []

    async def _cancel_if_orphaned(run_id):
        cancelled.append(run_id)

    monkeypatch.setattr(main_module, "store", _Store())
    monkeypatch.setattr(main_module, "run_manager", _RunManager())
    monkeypatch.setattr(main_module, "_cancel_if_orphaned", _cancel_if_orphaned)

    response = await main_module.replay_events("run-follow", _Request(), after=0, follow=True)
    events = response.body_iterator
    payload = await asyncio.wait_for(events.__anext__(), timeout=1.0)
    assert "CUSTOM" in payload

    await events.aclose()

    assert live_stream.closed is True
    assert cancelled == []


@pytest.mark.asyncio
async def test_cancel_if_orphaned_skips_persisted_terminal_event(tmp_path, monkeypatch):
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app.agui.run_manager import RunManager, RunStatus

    store = EventStore(str(tmp_path / "agui.db"))
    manager = RunManager(store)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))
    await asyncio.sleep(0.05)

    store.append(
        run_id,
        EventType.RUN_FINISHED.value,
        {"type": "RUN_FINISHED", "threadId": f"thread-{run_id}", "runId": run_id},
    )

    cancelled = await manager.cancel_if_orphaned(run_id)

    info = manager.get_info(run_id)
    assert cancelled is False
    assert info is not None
    assert info.status == RunStatus.RUNNING
    assert info.task is not None
    assert not info.task.cancelled()

    if info.task is not None:
        info.task.cancel()
        try:
            await info.task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_cancel_if_orphaned_skips_persisted_terminal_workflow_progress(tmp_path, monkeypatch):
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app.agui.run_manager import RunManager, RunStatus

    store = EventStore(str(tmp_path / "agui.db"))
    manager = RunManager(store)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))
    await asyncio.sleep(0.05)

    store.append(
        run_id,
        EventType.CUSTOM.value,
        {
            "type": "CUSTOM",
            "name": "workflow.progress",
            "value": {"workflow_run_id": run_id, "status": "completed"},
        },
    )

    cancelled = await manager.cancel_if_orphaned(run_id)

    info = manager.get_info(run_id)
    assert cancelled is False
    assert info is not None
    assert info.status == RunStatus.RUNNING
    assert info.task is not None
    assert not info.task.cancelled()

    if info.task is not None:
        info.task.cancel()
        try:
            await info.task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_cancel_if_orphaned_skips_persisted_terminal_workflow_result(tmp_path, monkeypatch):
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app.agui.run_manager import RunManager, RunStatus

    store = EventStore(str(tmp_path / "agui.db"))
    manager = RunManager(store)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))
    await asyncio.sleep(0.05)

    store.append(
        run_id,
        "WORKFLOW_RESULT",
        {"status": "completed", "artifacts": {"final_output": "ok"}},
    )

    cancelled = await manager.cancel_if_orphaned(run_id)

    info = manager.get_info(run_id)
    assert cancelled is False
    assert info is not None
    assert info.status == RunStatus.RUNNING
    assert info.task is not None
    assert not info.task.cancelled()

    if info.task is not None:
        info.task.cancel()
        try:
            await info.task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_cancel_if_orphaned_skips_persisted_service_result(tmp_path, monkeypatch):
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app.agui.run_manager import RunManager, RunStatus

    store = EventStore(str(tmp_path / "agui.db"))
    manager = RunManager(store)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))
    await asyncio.sleep(0.05)

    store.append(
        run_id,
        EventType.CUSTOM.value,
        {
            "type": "CUSTOM",
            "name": "service.result",
            "value": {"ok": True, "data": {"status": "done"}},
        },
    )

    cancelled = await manager.cancel_if_orphaned(run_id)

    info = manager.get_info(run_id)
    assert cancelled is False
    assert info is not None
    assert info.status == RunStatus.RUNNING
    assert info.task is not None
    assert not info.task.cancelled()

    if info.task is not None:
        info.task.cancel()
        try:
            await info.task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_explicit_cancel_skips_persisted_terminal_workflow_result(tmp_path, monkeypatch):
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app.agui.run_manager import RunManager, RunStatus

    store = EventStore(str(tmp_path / "agui.db"))
    manager = RunManager(store)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))
    await asyncio.sleep(0.05)

    store.append(
        run_id,
        "WORKFLOW_RESULT",
        {"status": "completed", "artifacts": {"final_output": "ok"}},
    )

    cancelled = await manager.cancel(run_id)

    info = manager.get_info(run_id)
    assert cancelled is False
    assert info is not None
    assert info.status == RunStatus.RUNNING
    assert info.task is not None
    assert not info.task.cancelled()

    info.task.cancel()
    try:
        await info.task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_disconnect_with_other_subscriber_keeps_run_alive(tmp_path, monkeypatch):
    """Если после disconnect одного клиента остался другой подписчик, run жив."""
    _install_runner_stub(monkeypatch)
    from backend.fastapi_app.agui.run_manager import RunManager

    manager = RunManager(EventStore(str(tmp_path / "agui.db")))
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await manager.start_run(RunAgentInput(**_make_payload(run_id)))

    # Два независимых подписчика.
    stream_a = manager.stream_live(run_id)
    stream_b = manager.stream_live(run_id)
    await asyncio.wait_for(stream_a.__anext__(), timeout=1.0)
    await asyncio.wait_for(stream_b.__anext__(), timeout=1.0)
    assert manager.subscriber_count(run_id) == 2

    # Клиент A отвалился.
    await stream_a.aclose()
    assert manager.subscriber_count(run_id) == 1

    from backend.fastapi_app import main as main_module

    monkeypatch.setattr(main_module, "run_manager", manager)
    await main_module._cancel_if_orphaned(run_id)
    assert manager.is_active(run_id)

    # Чистим за собой.
    await stream_b.aclose()
    await manager.cancel(run_id)
    info = manager.get_info(run_id)
    if info is not None and info.task is not None:
        try:
            await info.task
        except (asyncio.CancelledError, Exception):
            pass
