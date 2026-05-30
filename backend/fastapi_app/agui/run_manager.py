"""Run registry and event publishing for AG-UI streams."""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from .encoder import EventEncoder
from .events import BaseEvent, RunErrorEvent, EventType
from .models import RunAgentInput
from .redaction import _redact_payload, redact_pii_in_payload
from .runner import run_agent
from .store import EventStore

logger = logging.getLogger(__name__)


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "finished"
    ERRORED = "errored"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.FINISHED, RunStatus.ERRORED, RunStatus.CANCELLED}
)
_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value}
)
_WORKFLOW_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)


def is_terminal_event_payload(event_type: str, payload: dict) -> bool:
    if event_type in _TERMINAL_EVENT_TYPES:
        return True
    if event_type == "WORKFLOW_RESULT":
        return str(payload.get("status") or "").lower() in _WORKFLOW_TERMINAL_STATUSES
    if event_type != EventType.CUSTOM.value:
        return False
    name = payload.get("name")
    value = payload.get("value")
    if name in {"workflow.result", "service.result"}:
        return True
    if name == "workflow.progress" and isinstance(value, dict):
        return str(value.get("status") or "").lower() in _WORKFLOW_TERMINAL_STATUSES
    return False


def is_terminal_event(event: BaseEvent) -> bool:
    payload = event.model_dump(by_alias=True, exclude_none=True)
    return is_terminal_event_payload(event.type.value, payload)


@dataclass
class RunInfo:
    run_id: str
    thread_id: str
    status: RunStatus
    started_at_ms: int
    finished_at_ms: Optional[int] = None
    task: Optional[asyncio.Task] = None
    events: list[tuple[int, BaseEvent]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[Optional[tuple[int, BaseEvent]]]] = field(
        default_factory=set
    )


_EVICT_TTL_SECONDS: int = 30 * 60  # 30 minutes


class RunManager:
    def __init__(self, store: EventStore, evict_ttl_seconds: int = _EVICT_TTL_SECONDS) -> None:
        self._store = store
        self._runs: dict[str, RunInfo] = {}
        self._lock = asyncio.Lock()
        self._evict_ttl_seconds = evict_ttl_seconds

    def _evict_stale(self) -> int:
        """Remove terminal RunInfo entries whose finished_at_ms is older than TTL.

        Returns the number of evicted entries. MUST be called while holding
        self._lock (it mutates self._runs); does not cancel running tasks.
        """
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - self._evict_ttl_seconds * 1000
        to_delete = [
            run_id
            for run_id, info in self._runs.items()
            if info.status in _TERMINAL_STATUSES
            and info.finished_at_ms is not None
            and info.finished_at_ms < cutoff_ms
        ]
        for run_id in to_delete:
            del self._runs[run_id]
        return len(to_delete)

    async def start_run(self, input_data: RunAgentInput) -> RunInfo:
        async with self._lock:
            self._evict_stale()
            if input_data.run_id in self._runs:
                raise ValueError(f"run_id already exists: {input_data.run_id}")
            if self._store.latest_seq(input_data.run_id) is not None:
                raise ValueError(f"run_id already exists: {input_data.run_id}")
            info = RunInfo(
                run_id=input_data.run_id,
                thread_id=input_data.thread_id,
                status=RunStatus.PENDING,
                started_at_ms=int(time.time() * 1000),
            )
            task = asyncio.create_task(self._execute_run(input_data, info))
            info.task = task
            self._runs[input_data.run_id] = info
            return info

    async def _execute_run(self, input_data: RunAgentInput, info: RunInfo) -> None:
        info.status = RunStatus.RUNNING
        terminal_status = RunStatus.FINISHED
        try:
            async for event in run_agent(input_data):
                await self._publish_event(info.run_id, event)
                if event.type == EventType.RUN_ERROR:
                    terminal_status = RunStatus.ERRORED
        except asyncio.CancelledError:
            terminal_status = RunStatus.CANCELLED
            await self._publish_event(
                info.run_id,
                RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message="run cancelled",
                    code="cancelled",
                    timestamp=int(time.time() * 1000),
                ),
            )
            raise
        except Exception as exc:  # noqa: BLE001
            terminal_status = RunStatus.ERRORED
            await self._publish_event(
                info.run_id,
                RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=redact_pii_in_payload(_redact_payload(str(exc))),
                    code="run_manager_error",
                    timestamp=int(time.time() * 1000),
                ),
            )
        finally:
            # Атомарно фиксируем терминальный статус и снимаем snapshot подписчиков
            # под одним lock'ом: любой stream_live, увидевший не-терминальный статус,
            # гарантированно был добавлен в info.subscribers до этой точки и получит
            # сигнал завершения через queue.put(None). Stream_live, пришедший после
            # установки статуса, увидит терминальный статус и не подпишется вовсе.
            async with self._lock:
                info.status = terminal_status
                info.finished_at_ms = int(time.time() * 1000)
                subscribers = list(info.subscribers)
                info.subscribers.clear()
            for queue in subscribers:
                await queue.put(None)

    async def _publish_event(self, run_id: str, event: BaseEvent) -> None:
        payload = event.model_dump(by_alias=True, exclude_none=True)
        redacted_payload = redact_pii_in_payload(_redact_payload(payload))
        redacted_event = event.__class__.model_validate(redacted_payload)
        seq = self._store.append(run_id, event.type.value, redacted_payload)
        async with self._lock:
            info = self._runs.get(run_id)
            subscribers: list[asyncio.Queue[Optional[tuple[int, BaseEvent]]]] = []
            if info is not None:
                if info.status in _TERMINAL_STATUSES:
                    # Событие пришло после терминального статуса — это баг логики выше
                    # по стеку (run уже завершён). Логируем WARNING, но событие всё равно
                    # фиксируем в info.events и store: оно уже персистировано через
                    # self._store.append выше, и историческая выдача через replay должна
                    # остаться согласованной с persisted store.
                    logger.warning(
                        "AG-UI event %s published after terminal status %s for run_id=%s seq=%s",
                        event.type.value,
                        info.status.value,
                        run_id,
                        seq,
                    )
                info.events.append((seq, redacted_event))
                subscribers = list(info.subscribers)
        for queue in subscribers:
            await queue.put((seq, redacted_event))

    async def stream_live(self, run_id: str, after: int = 0) -> AsyncIterator[BaseEvent]:
        queue: asyncio.Queue[Optional[tuple[int, BaseEvent]]] = asyncio.Queue()
        # Snapshot буфера и подписка должны быть атомарными относительно
        # _publish_event и _execute_run.finally: иначе подписчик может пропустить
        # события, опубликованные после snapshot, но до добавления в subscribers,
        # либо подписаться на уже завершённый run и зависнуть в ожидании None.
        async with self._lock:
            info = self._runs.get(run_id)
            if info is None:
                return
            buffered = [(seq, event) for seq, event in info.events if seq > after]
            follow = info.status not in _TERMINAL_STATUSES
            if follow:
                info.subscribers.add(queue)

        last_seq = after
        # try/finally охватывает и буфер, и live-цикл: если потребитель закроет
        # генератор (aclose / GeneratorExit) на любом yield, подписчик гарантированно
        # будет удалён из info.subscribers. Без этого был возможен подвисший подписчик
        # при отвале клиента до первого live-события (см. T3.6: subscriber_count
        # должен корректно отражать число активных читателей для cancel-on-disconnect).
        try:
            for seq, event in buffered:
                if seq <= last_seq:
                    continue
                last_seq = seq
                yield event

            if not follow:
                return

            while True:
                item = await queue.get()
                if item is None:
                    break
                seq, event = item
                if seq <= last_seq:
                    continue
                last_seq = seq
                yield event
        finally:
            async with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current.subscribers.discard(queue)

    async def cancel(self, run_id: str) -> bool:
        async with self._lock:
            info = self._runs.get(run_id)
            if info is None or info.task is None:
                return False
            if info.status in _TERMINAL_STATUSES:
                return False
            if any(is_terminal_event(event) for _seq, event in info.events):
                return False
            if any(
                is_terminal_event_payload(stored.event_type, stored.payload)
                for stored in self._store.list_after(run_id, 0)
            ):
                return False
            info.task.cancel()
        return True

    async def cancel_if_orphaned(self, run_id: str) -> bool:
        """Cancel a live run only when no subscriber and no terminal event exist."""
        async with self._lock:
            info = self._runs.get(run_id)
            if info is None or info.task is None:
                return False
            if info.status in _TERMINAL_STATUSES or info.subscribers:
                return False
            if any(is_terminal_event(event) for _seq, event in info.events):
                return False
            if any(
                is_terminal_event_payload(stored.event_type, stored.payload)
                for stored in self._store.list_after(run_id, 0)
            ):
                return False
            info.task.cancel()
            return True

    def subscriber_count(self, run_id: str) -> int:
        """Возвращает число активных live-подписчиков run'а.

        Используется политикой «никого нет — отменяем» при disconnect SSE-клиента.
        Для несуществующих run'ов возвращает 0.
        """
        info = self._runs.get(run_id)
        if info is None:
            return 0
        return len(info.subscribers)

    def is_active(self, run_id: str) -> bool:
        """True, если run существует и его статус не терминальный."""
        info = self._runs.get(run_id)
        if info is None:
            return False
        return info.status not in _TERMINAL_STATUSES

    def get_info(self, run_id: str) -> Optional[RunInfo]:
        return self._runs.get(run_id)

    def encoder(self, accept: Optional[str]) -> EventEncoder:
        return EventEncoder(accept=accept)
