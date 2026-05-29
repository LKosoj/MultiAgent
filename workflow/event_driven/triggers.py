"""TriggerManager для event-driven WorkflowEngine."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from ..events import Event, EventBus, EventPattern

try:  # pragma: no cover - опциональная зависимость
    from croniter import croniter  # type: ignore
except Exception:  # pragma: no cover
    croniter = None

logger = logging.getLogger(__name__)


class TriggerType(Enum):
    TIME_BASED = "time_based"
    EVENT_BASED = "event_based"
    CONDITION_BASED = "condition_based"


ContextBuilder = Callable[[Dict[str, Any]], Dict[str, Any]]
WorkflowExecutor = Callable[[str, Dict[str, Any]], Awaitable[Any]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MemoryQuery:
    """Запрос к памяти для context enrichment."""
    
    query: str
    inject_as: str
    limit: int = 5
    session_id: Optional[str] = None
    agent_name: Optional[str] = None


@dataclass
class ContextEnrichment:
    """Конфигурация обогащения контекста из памяти."""
    
    memory_queries: List[MemoryQuery] = field(default_factory=list)
    include_goals: bool = False
    include_system_context: bool = False


@dataclass(slots=True)
class AgentTrigger:
    trigger_id: str
    trigger_type: TriggerType
    target_workflow: str
    enabled: bool = True
    schedule: Optional[str] = None
    event_pattern: Optional[str] = None
    condition: Optional[str] = None
    check_interval: timedelta = timedelta(minutes=1)
    context_builder: Optional[ContextBuilder] = None
    context_enrichment: Optional[ContextEnrichment] = None  # ✨ Новое поле
    metadata: Dict[str, Any] = field(default_factory=dict)
    last_triggered: Optional[datetime] = None
    trigger_count: int = 0

    def mark_triggered(self) -> None:
        self.last_triggered = _utcnow()
        self.trigger_count += 1


class TriggerManager:
    """Управляет триггерами для event-driven workflow."""

    def __init__(self, event_bus: EventBus, workflow_executor: WorkflowExecutor) -> None:
        self._event_bus = event_bus
        self._workflow_executor = workflow_executor
        self._triggers: Dict[str, AgentTrigger] = {}

        self._running = asyncio.Event()
        self._scheduler_task: Optional[asyncio.Task[None]] = None
        self._condition_task: Optional[asyncio.Task[None]] = None

        self._time_next_run: Dict[str, datetime] = {}
        self._condition_next_check: Dict[str, datetime] = {}
        self._event_subscription_registered = False

        self._lock = asyncio.Lock()
        
        # ✨ Memory manager для context enrichment
        self._memory_manager: Optional[Any] = None

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    async def start(self) -> None:
        if self.is_running:
            return

        self._running.set()

        if not self._event_bus.running:
            await self._event_bus.start()

        if not self._event_subscription_registered:
            self._event_bus.subscribe_pattern(EventPattern(pattern="*"), self._on_event)
            self._event_subscription_registered = True

        self._scheduler_task = asyncio.create_task(self._schedule_loop(), name="pepper-trigger-schedule")
        self._condition_task = asyncio.create_task(self._condition_loop(), name="pepper-trigger-conditions")
        logger.info("Trigger manager started")

    async def stop(self) -> None:
        if not self.is_running:
            return

        self._running.clear()

        for task in (self._scheduler_task, self._condition_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._scheduler_task = None
        self._condition_task = None
        logger.info("Trigger manager stopped")

    async def register_trigger(self, trigger: AgentTrigger) -> None:
        async with self._lock:
            self._triggers[trigger.trigger_id] = trigger
            if trigger.trigger_type == TriggerType.TIME_BASED:
                self._schedule_next_run(trigger)
            elif trigger.trigger_type == TriggerType.CONDITION_BASED:
                self._condition_next_check[trigger.trigger_id] = _utcnow()

        logger.info("Trigger %s (%s) registered", trigger.trigger_id, trigger.trigger_type.value)

    async def unregister_trigger(self, trigger_id: str) -> None:
        async with self._lock:
            self._triggers.pop(trigger_id, None)
            self._time_next_run.pop(trigger_id, None)
            self._condition_next_check.pop(trigger_id, None)

        logger.info("Trigger %s removed", trigger_id)

    def list_triggers(self) -> Iterable[AgentTrigger]:
        return list(self._triggers.values())
    
    def set_memory_manager(self, memory_manager: Any) -> None:
        """Установить memory manager для context enrichment."""
        self._memory_manager = memory_manager
        logger.info("Memory manager установлен для context enrichment")

    # ------------------------------------------------------------------
    # Внутренние циклы
    # ------------------------------------------------------------------
    async def _schedule_loop(self) -> None:
        while self.is_running:
            await asyncio.sleep(1)
            now = _utcnow()

            for trigger in self._iter_triggers(TriggerType.TIME_BASED):
                if not trigger.enabled:
                    continue

                next_run = self._time_next_run.get(trigger.trigger_id)
                if next_run is None or now >= next_run:
                    await self._fire_trigger(trigger, {
                        "trigger": "time",
                        "scheduled_for": (next_run or now).isoformat(),
                    })
                    self._schedule_next_run(trigger, reference=now)

    async def _condition_loop(self) -> None:
        while self.is_running:
            await asyncio.sleep(1)
            now = _utcnow()

            for trigger in self._iter_triggers(TriggerType.CONDITION_BASED):
                if not trigger.enabled:
                    continue

                next_check = self._condition_next_check.get(trigger.trigger_id, now)
                if now < next_check:
                    continue

                self._condition_next_check[trigger.trigger_id] = now + trigger.check_interval

                if await self._evaluate_condition(trigger):
                    await self._fire_trigger(trigger, {"trigger": "condition"})

    async def _on_event(self, event: Event) -> None:
        if not self.is_running:
            return

        import fnmatch

        for trigger in self._iter_triggers(TriggerType.EVENT_BASED):
            if not trigger.enabled or not trigger.event_pattern:
                continue

            if fnmatch.fnmatch(event.event_type, trigger.event_pattern):
                await self._fire_trigger(trigger, {
                    "trigger": "event",
                    "event": event.to_dict(),
                })

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------
    async def _fire_trigger(self, trigger: AgentTrigger, base_context: Optional[Dict[str, Any]] = None) -> None:
        if not trigger.enabled:
            return

        context: Dict[str, Any] = {
            "trigger_id": trigger.trigger_id,
            "trigger_type": trigger.trigger_type.value,
        }
        if base_context:
            context.update(base_context)

        try:
            # ✨ Context enrichment из памяти
            if trigger.context_enrichment:
                enriched = await self._enrich_context_from_memory(trigger.context_enrichment, context)
                context.update(enriched)
            
            # Существующий context builder
            if trigger.context_builder:
                builder_result = trigger.context_builder(context.copy())
                if asyncio.iscoroutine(builder_result):
                    builder_result = await builder_result
                if isinstance(builder_result, dict):
                    context.update(builder_result)

            await self._workflow_executor(trigger.target_workflow, context)
            trigger.mark_triggered()

            if trigger.trigger_type == TriggerType.TIME_BASED:
                self._schedule_next_run(trigger)

        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(
                "Failed to execute workflow %s for trigger %s: %s",
                trigger.target_workflow,
                trigger.trigger_id,
                exc,
            )
    
    async def _enrich_context_from_memory(
        self, enrichment: ContextEnrichment, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Обогащает контекст данными из памяти."""
        enriched_data: Dict[str, Any] = {}
        
        if not self._memory_manager:
            logger.warning("Memory manager не установлен, context enrichment пропущен")
            return enriched_data
        
        try:
            # Обработка memory queries
            for query_config in enrichment.memory_queries:
                # Подстановка переменных из контекста в запрос
                query_text = query_config.query.format(**context)
                
                # Определяем session_id
                session_id = query_config.session_id or context.get("session_id")
                agent_name = query_config.agent_name or context.get("agent_name")
                
                if not session_id:
                    logger.warning("session_id не найден для memory query, пропускаем")
                    continue
                
                # Выполняем поиск в памяти
                results = self._memory_manager.get_memory(
                    session_id=session_id,
                    agent_name=agent_name,
                    query=query_text,
                    limit=query_config.limit
                )
                
                # Сохраняем результаты под заданным именем
                enriched_data[query_config.inject_as] = results
                logger.debug(
                    "Memory enrichment: %s -> %d results",
                    query_config.inject_as,
                    len(results) if isinstance(results, list) else 1
                )
            
            # Включаем цели если запрошено
            if enrichment.include_goals:
                session_id = context.get("session_id")
                if session_id:
                    goals = self._memory_manager.get_goals(session_id)
                    enriched_data["goals"] = goals
            
            # Включаем системный контекст если запрошено
            if enrichment.include_system_context:
                session_id = context.get("session_id")
                if session_id:
                    sys_context = self._memory_manager.get_context(session_id)
                    enriched_data["system_context"] = sys_context
        
        except Exception as exc:
            logger.error("Ошибка при context enrichment: %s", exc, exc_info=True)
        
        return enriched_data

    async def _evaluate_condition(self, trigger: AgentTrigger) -> bool:
        if trigger.condition is None:
            return False

        safe_globals = {"__builtins__": {}}
        context = dict(trigger.metadata.get("condition_context", {}))

        try:
            result = eval(trigger.condition, safe_globals, context)  # noqa: S307
            if asyncio.iscoroutine(result):
                result = await result
            return bool(result)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Condition evaluation failed for %s: %s", trigger.trigger_id, exc)
            return False

    def _iter_triggers(self, trigger_type: TriggerType):
        return [
            trigger for trigger in self._triggers.values() if trigger.trigger_type == trigger_type
        ]

    def _schedule_next_run(self, trigger: AgentTrigger, reference: Optional[datetime] = None) -> None:
        reference_time = reference or _utcnow()

        if trigger.schedule and croniter:
            try:
                iterator = croniter(trigger.schedule, trigger.last_triggered or reference_time)
                next_time = iterator.get_next(datetime)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Invalid cron schedule for %s: %s", trigger.trigger_id, exc)
                next_time = reference_time + max(trigger.check_interval, timedelta(seconds=1))
        else:
            next_time = reference_time + max(trigger.check_interval, timedelta(seconds=1))

        self._time_next_run[trigger.trigger_id] = next_time


