"""Регресс на гонку _publish_event / stream_live в AG-UI RunManager.

T3.5: каждый подписчик stream_live должен получить полный поток событий
короткого run'а (CustomEvent + RunFinishedEvent), даже если подписка
происходит параллельно с публикацией событий и установкой терминального
статуса в _execute_run.finally.
"""

import asyncio
import importlib
import sys
import time
import types
import uuid

import pytest

from backend.fastapi_app.agui.events import (
    CustomEvent,
    EventType,
    RunFinishedEvent,
)
from backend.fastapi_app.agui.models import RunAgentInput
from backend.fastapi_app.agui.store import EventStore


def _make_payload(run_id: str) -> dict:
    return {
        "threadId": f"thread-{run_id}",
        "runId": run_id,
        "state": {},
        "messages": [{"id": "msg-1", "role": "user", "content": "ping"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


def _load_run_manager_with_runner_stub(monkeypatch, run_agent):
    stub_runner = types.ModuleType("backend.fastapi_app.agui.runner")
    stub_runner.run_agent = run_agent
    monkeypatch.setitem(sys.modules, "backend.fastapi_app.agui.runner", stub_runner)
    sys.modules.pop("backend.fastapi_app.agui.run_manager", None)
    return importlib.import_module("backend.fastapi_app.agui.run_manager")


@pytest.mark.asyncio
async def test_start_run_rejects_run_id_with_persisted_history(tmp_path, monkeypatch):
    async def fake_run_agent(_input_data):
        raise AssertionError("run_agent must not start for an existing persisted run_id")
        yield

    rm = _load_run_manager_with_runner_stub(monkeypatch, fake_run_agent)
    store = EventStore(str(tmp_path / "agui_events.db"))
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    store.append(
        run_id,
        EventType.RUN_FINISHED.value,
        {
            "type": EventType.RUN_FINISHED.value,
            "threadId": f"thread-{run_id}",
            "runId": run_id,
            "timestamp": int(time.time() * 1000),
        },
    )
    manager = rm.RunManager(store, evict_ttl_seconds=0)

    with pytest.raises(ValueError, match="run_id already exists"):
        await manager.start_run(RunAgentInput(**_make_payload(run_id)))


@pytest.mark.asyncio
async def test_short_run_subscribers_always_receive_finished(tmp_path, monkeypatch):
    """100 одновременных коротких run'ов: каждый подписчик должен получить RUN_FINISHED.

    Сценарий моделирует service-action: один CustomEvent + RunFinishedEvent подряд,
    без задержек между ними. Это типичный паттерн, на котором ловится гонка
    snapshot/subscribe в stream_live и установки терминального статуса в
    _execute_run.finally.
    """

    async def fake_run_agent(input_data):
        # Без await-задержек между событиями: имитация быстрого service-action,
        # где CustomEvent и RunFinishedEvent уходят последовательно в одном такте.
        yield CustomEvent(
            type=EventType.CUSTOM,
            name="service.result",
            value={"ok": True, "data": {"echo": input_data.run_id}},
            timestamp=int(time.time() * 1000),
        )
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result=None,
            timestamp=int(time.time() * 1000),
        )

    rm = _load_run_manager_with_runner_stub(monkeypatch, fake_run_agent)
    manager = rm.RunManager(EventStore(str(tmp_path / "agui_events.db")))

    run_count = 100

    async def run_one(idx: int) -> list[str]:
        run_id = f"run-{idx:03d}-{uuid.uuid4().hex[:8]}"
        await manager.start_run(RunAgentInput(**_make_payload(run_id)))
        # Без явной паузы — подписка должна работать в любой момент гонки
        # между публикацией событий и установкой терминального статуса.
        return [event.type.value async for event in manager.stream_live(run_id)]

    results = await asyncio.wait_for(
        asyncio.gather(*(run_one(i) for i in range(run_count))),
        timeout=10,
    )

    assert len(results) == run_count
    for idx, events in enumerate(results):
        assert "RUN_FINISHED" in events, (
            f"run #{idx} missed RUN_FINISHED, got events={events}"
        )
        # Если RUN_FINISHED получен, перед ним должен идти CUSTOM,
        # либо CUSTOM пришёл из buffered snapshot — оба варианта валидны,
        # но RUN_FINISHED обязан быть последним полученным событием.
        assert events[-1] == "RUN_FINISHED", (
            f"run #{idx}: RUN_FINISHED must be terminal, got events={events}"
        )


@pytest.mark.asyncio
async def test_subscribe_after_terminal_status_drains_buffer(tmp_path, monkeypatch):
    """Stream_live, подписавшийся после терминального статуса, должен отдать
    весь буфер событий и завершиться, не зависая на ожидании None из очереди.
    """

    async def fake_run_agent(input_data):
        yield CustomEvent(
            type=EventType.CUSTOM,
            name="demo.event",
            value={"step": 1},
            timestamp=int(time.time() * 1000),
        )
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result=None,
            timestamp=int(time.time() * 1000),
        )

    rm = _load_run_manager_with_runner_stub(monkeypatch, fake_run_agent)
    manager = rm.RunManager(EventStore(str(tmp_path / "agui_events.db")))

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    info = await manager.start_run(RunAgentInput(**_make_payload(run_id)))
    # Дождёмся завершения run'а полностью.
    await info.task

    assert info.status == rm.RunStatus.FINISHED
    assert info.subscribers == set()  # subscribers очищены в finally

    events = await asyncio.wait_for(
        _collect(manager.stream_live(run_id)),
        timeout=2,
    )
    assert events == ["CUSTOM", "RUN_FINISHED"]


async def _collect(aiter) -> list[str]:
    return [event.type.value async for event in aiter]
