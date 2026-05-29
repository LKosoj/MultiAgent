"""Асинхронная шина событий для workflow."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from .models import Event, EventPattern

logger = logging.getLogger(__name__)

EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Простая очередь событий с pub/sub API."""

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._event_queue: asyncio.Queue[Event] = asyncio.Queue(max_queue_size)
        self._topic_subscribers: Dict[str, List[EventHandler]] = defaultdict(list)
        self._pattern_subscribers: List[Tuple[EventPattern, EventHandler]] = []
        self._processor_task: Optional[asyncio.Task[None]] = None
        self._running = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._stats = {
            "published": 0,
            "processed": 0,
            "failed": 0,
            "subscribers": 0,
        }

    @property
    def running(self) -> bool:
        return self._running.is_set()

    async def start(self) -> None:
        """Запуск фоновой обработки событий."""

        if self.running:
            return

        self._running.set()
        self._loop = asyncio.get_running_loop()
        self._processor_task = asyncio.create_task(self._process_events())
        logger.info("Event bus started")

    async def stop(self) -> None:
        """Остановка фоновой обработки."""

        if not self.running:
            return

        self._running.clear()
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        self._processor_task = None
        self._loop = None
        logger.info("Event bus stopped")

    async def publish(self, event: Event) -> None:
        """Публикация события в очередь."""

        if not self.running:
            logger.debug("Event bus not running, starting automatically")
            await self.start()

        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            self._stats["failed"] += 1
            logger.error("Event queue overflow, dropping event %s", event.event_id)
            return

        self._stats["published"] += 1

    def publish_sync(self, event: Event, timeout: float = 5.0) -> None:
        """Синхронная публикация события из другого потока."""

        if not self._loop:
            raise RuntimeError("Event bus loop is not initialized. Ensure start_event_layer() was called.")

        future = asyncio.run_coroutine_threadsafe(self.publish(event), self._loop)
        try:
            future.result(timeout=timeout)
        except FutureTimeoutError as exc:  # pragma: no cover - крайний случай
            future.cancel()
            raise TimeoutError("Timed out while publishing event") from exc

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Подписка на конкретный тип события."""

        self._topic_subscribers[event_type].append(handler)
        self._stats["subscribers"] += 1

    def subscribe_pattern(self, pattern: EventPattern, handler: EventHandler) -> None:
        """Подписка на набор событий по паттерну."""

        self._pattern_subscribers.append((pattern, handler))
        self._stats["subscribers"] += 1

    def unsubscribe(self, handler: EventHandler) -> None:
        """Отписка обработчика."""

        removed = 0
        for handlers in self._topic_subscribers.values():
            if handler in handlers:
                handlers.remove(handler)
                removed += 1

        self._pattern_subscribers = [
            (pattern, hdl) for pattern, hdl in self._pattern_subscribers if hdl is not handler
        ]

        if removed:
            self._stats["subscribers"] = max(0, self._stats["subscribers"] - removed)

    def get_stats(self) -> Dict[str, int]:
        return dict(self._stats)

    async def _process_events(self) -> None:
        while self.running:
            try:
                event = await self._event_queue.get()
            except asyncio.CancelledError:
                break

            try:
                await self._dispatch(event)
                self._stats["processed"] += 1
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Failed to dispatch event %s: %s", event.event_id, exc)
                self._stats["failed"] += 1

    async def _dispatch(self, event: Event) -> None:
        handlers: List[EventHandler] = []

        handlers.extend(self._topic_subscribers.get(event.event_type, []))

        for pattern, handler in self._pattern_subscribers:
            if pattern.matches(event):
                handlers.append(handler)

        if not handlers:
            logger.debug("No handlers for event %s", event.event_type)
            return

        await asyncio.gather(*(self._call_handler(handler, event) for handler in handlers), return_exceptions=True)

    async def _call_handler(self, handler: EventHandler, event: Event) -> None:
        try:
            await handler(event)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Event handler %s failed: %s", getattr(handler, "__name__", handler), exc)
            self._stats["failed"] += 1


