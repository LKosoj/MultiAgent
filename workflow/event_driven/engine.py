"""Event-driven вариант WorkflowEngine с поддержкой PEPPER."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Set

from ..enhanced_engine import EnhancedWorkflowEngine
from ..models import (
    StepStatus,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowResult,
)
from ..events import Event, EventBus, EventPriority, EventStore
from ..temporal import TemporalEngine
from .config_loader import ParsedTrigger, TriggerConfigError, load_trigger_config
from .registry import clear_event_resources, set_event_bus, set_event_store
from .triggers import AgentTrigger, TriggerManager

logger = logging.getLogger(__name__)


class EventDrivenWorkflowEngine(EnhancedWorkflowEngine):
    """Расширение базового движка с event-driven функциями."""

    def __init__(self, memory_manager=None) -> None:
        super().__init__()

        self.event_bus = EventBus()
        self.event_store = EventStore()
        self.trigger_manager = TriggerManager(
            event_bus=self.event_bus,
            workflow_executor=self._execute_workflow_from_trigger,
        )
        
        # ✨ Temporal Engine для таймеров и сигналов
        self.temporal_engine = TemporalEngine(
            workflow_executor=self._execute_workflow_from_trigger
        )
        
        set_event_bus(self.event_bus)
        set_event_store(self.event_store)
        
        # ✨ Подключаем memory manager если предоставлен
        if memory_manager:
            self.trigger_manager.set_memory_manager(memory_manager)

        self._trigger_config_path: Optional[Path] = None
        self._trigger_reload_task: Optional[asyncio.Task[None]] = None
        self._config_triggers: Dict[str, AgentTrigger] = {}
        self._config_signatures: Dict[str, Dict[str, Any]] = {}
        self._config_mtime: Optional[float] = None
        self._config_reload_interval: float = 5.0

    # ------------------------------------------------------------------
    # Публичное API расширения
    # ------------------------------------------------------------------
    async def start_event_layer(self, config_path: Optional[str] = None, reload_interval: float = 5.0) -> None:
        """Запуск event-driven компонентов."""

        await self.event_bus.start()
        await self.trigger_manager.start()
        await self.temporal_engine.start()  # ✨ Запускаем temporal engine

        if config_path:
            await self.apply_trigger_config(config_path, reload_interval=reload_interval)

    async def stop_event_layer(self) -> None:
        """Остановка event-driven компонентов."""

        if self._trigger_reload_task:
            self._trigger_reload_task.cancel()
            try:
                await self._trigger_reload_task
            except asyncio.CancelledError:
                pass
            self._trigger_reload_task = None

        for trigger_id in list(self._config_triggers.keys()):
            try:
                await self.trigger_manager.unregister_trigger(trigger_id)
            except Exception as exc:  # pragma: no cover - логирование
                logger.warning("Не удалось снять триггер %s: %s", trigger_id, exc)

        self._config_triggers.clear()
        self._config_signatures.clear()
        self._trigger_config_path = None
        self._config_mtime = None

        await self.temporal_engine.stop()  # ✨ Останавливаем temporal engine
        await self.trigger_manager.stop()
        await self.event_bus.stop()
        clear_event_resources()

    async def register_trigger(self, trigger: AgentTrigger) -> None:
        await self.trigger_manager.register_trigger(trigger)

    async def unregister_trigger(self, trigger_id: str) -> None:
        await self.trigger_manager.unregister_trigger(trigger_id)

    def list_triggers(self) -> list[AgentTrigger]:
        return list(self.trigger_manager.list_triggers())
    
    def set_memory_manager(self, memory_manager) -> None:
        """Установить memory manager для context enrichment."""
        self.trigger_manager.set_memory_manager(memory_manager)

    async def apply_trigger_config(self, config_path: str, reload_interval: float = 5.0) -> None:
        """Загрузить триггеры из YAML и запустить горячую перезагрузку."""

        path = Path(config_path)
        self._trigger_config_path = path
        self._config_reload_interval = reload_interval

        if not self.trigger_manager.is_running:
            logger.warning("TriggerManager не запущен. Вызовите start_event_layer() перед загрузкой конфигурации.")

        await self._reload_trigger_config(force=True)

        if self._trigger_reload_task is None or self._trigger_reload_task.done():
            loop = asyncio.get_running_loop()
            self._trigger_reload_task = loop.create_task(self._watch_trigger_config())

    # ------------------------------------------------------------------
    # Переопределённые методы WorkflowEngine
    # ------------------------------------------------------------------
    async def execute_workflow(
        self,
        workflow_definition: WorkflowDefinition,
        context: Optional[WorkflowContext] = None,
        client_id: Optional[str] = None,
        *,
        skip_steps: Optional[Set[str]] = None,
        restored_step_results: Optional[Dict[str, "StepResult"]] = None,
    ) -> WorkflowResult:
        """Выполнение workflow c генерацией событий lifecycle."""

        if context is None:
            workflow_context = WorkflowContext(client_id=client_id)
        else:
            workflow_context = context
            if client_id and not workflow_context.client_id:
                workflow_context.client_id = client_id

        await self._emit_event(
            "workflow.started",
            source=workflow_context.workflow_id,
            payload={
                "workflow_name": workflow_definition.name,
                "client_id": client_id,
                "parallel_execution": workflow_definition.parallel_execution,
                "steps": [step.id for step in workflow_definition.steps],
            },
        )

        try:
            result = await super().execute_workflow(
                workflow_definition,
                context=workflow_context,
                client_id=client_id,
                skip_steps=skip_steps,
                restored_step_results=restored_step_results,
            )
        except Exception as exc:  # pylint: disable=broad-except
            await self._emit_event(
                "workflow.failed",
                source=workflow_context.workflow_id,
                payload={
                    "workflow_name": workflow_definition.name,
                    "error": str(exc),
                    "client_id": client_id,
                },
                priority=EventPriority.HIGH,
            )
            raise

        await self._emit_event(
            "workflow.completed",
            source=result.workflow_id,
            payload={
                "workflow_name": workflow_definition.name,
                "status": result.status.value if hasattr(result.status, "value") else str(result.status),
                "duration_seconds": result.duration_seconds,
                "completed_steps": result.completed_steps,
                "failed_steps": result.failed_steps,
            },
        )

        return result

    async def _execute_workflow_step(self, step, context, workflow_def, step_results=None):  # type: ignore[override]
        await self._emit_event(
            "workflow.step.started",
            source=context.workflow_id,
            payload={
                "step_id": step.id,
                "step_type": step.step_type,
                "agent_type": step.agent_type,
            },
        )

        result = await super()._execute_workflow_step(
            step, context, workflow_def, step_results=step_results
        )

        if result.status == StepStatus.COMPLETED:
            await self._emit_event(
                "workflow.step.completed",
                source=context.workflow_id,
                payload={
                    "step_id": step.id,
                    "duration_seconds": result.duration_seconds,
                },
            )
        elif result.status == StepStatus.FAILED:
            await self._emit_event(
                "workflow.step.failed",
                source=context.workflow_id,
                payload={
                    "step_id": step.id,
                    "error": result.error,
                },
                priority=EventPriority.HIGH,
            )

        return result

    async def _execute_enhanced_step(self, step, context, step_results):  # type: ignore[override]
        await self._emit_event(
            "workflow.step.started",
            source=context.workflow_id,
            payload={
                "step_id": step.id,
                "step_type": step.step_type,
                "agent_type": step.agent_type,
            },
        )

        result = await super()._execute_enhanced_step(step, context, step_results)

        if result.status == StepStatus.COMPLETED:
            await self._emit_event(
                "workflow.step.completed",
                source=context.workflow_id,
                payload={
                    "step_id": step.id,
                    "duration_seconds": result.duration_seconds,
                },
            )
        elif result.status == StepStatus.FAILED:
            await self._emit_event(
                "workflow.step.failed",
                source=context.workflow_id,
                payload={
                    "step_id": step.id,
                    "error": result.error,
                },
                priority=EventPriority.HIGH,
            )

        return result

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------
    async def _reload_trigger_config(self, *, force: bool = False) -> None:
        if not self._trigger_config_path:
            return

        path = self._trigger_config_path

        try:
            parsed: Dict[str, ParsedTrigger] = load_trigger_config(path)
        except TriggerConfigError as exc:
            logger.error("Ошибка загрузки конфигурации триггеров: %s", exc)
            return

        try:
            new_mtime = path.stat().st_mtime
        except FileNotFoundError:
            new_mtime = None

        old_ids = set(self._config_triggers.keys())
        new_ids = set(parsed.keys())

        removed = old_ids - new_ids
        for trigger_id in removed:
            try:
                await self.trigger_manager.unregister_trigger(trigger_id)
            except Exception as exc:  # pragma: no cover - логирование
                logger.warning("Не удалось снять удаленный триггер %s: %s", trigger_id, exc)
            finally:
                self._config_triggers.pop(trigger_id, None)
                self._config_signatures.pop(trigger_id, None)

        for trigger_id, parsed_trigger in parsed.items():
            signature = parsed_trigger.signature
            if (
                not force
                and trigger_id in self._config_signatures
                and self._config_signatures[trigger_id] == signature
            ):
                continue

            if trigger_id in self._config_triggers:
                try:
                    await self.trigger_manager.unregister_trigger(trigger_id)
                except Exception as exc:  # pragma: no cover - логирование
                    logger.warning("Не удалось переустановить триггер %s: %s", trigger_id, exc)

            try:
                await self.trigger_manager.register_trigger(parsed_trigger.trigger)
                self._config_triggers[trigger_id] = parsed_trigger.trigger
                self._config_signatures[trigger_id] = signature
            except Exception as exc:  # pragma: no cover - логирование
                logger.error("Не удалось зарегистрировать триггер %s: %s", trigger_id, exc)

        if new_mtime is not None:
            self._config_mtime = new_mtime
        else:
            self._config_mtime = 0.0

        if parsed:
            logger.info("Загружено %s триггер(ов) из конфигурации", len(parsed))
        else:
            logger.info("Конфигурация триггеров пуста")

    async def _watch_trigger_config(self) -> None:
        if not self._trigger_config_path:
            return

        logger.info("Запущена горячая перезагрузка триггеров из %s", self._trigger_config_path)

        try:
            while self._trigger_config_path:
                await asyncio.sleep(self._config_reload_interval)
                path = self._trigger_config_path
                if not path:
                    break

                try:
                    mtime = path.stat().st_mtime
                except FileNotFoundError:
                    mtime = 0.0

                if self._config_mtime is None or mtime > self._config_mtime:
                    await self._reload_trigger_config()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - критическая ошибка
            logger.error("Watcher конфигурации триггеров остановлен из-за ошибки: %s", exc, exc_info=True)

    async def _emit_event(
        self,
        event_type: str,
        *,
        source: Optional[str],
        payload: Optional[Dict[str, Any]] = None,
        priority: EventPriority = EventPriority.NORMAL,
    ) -> None:
        """
        Генерация PEPPER Event с автоматическим добавлением в OpenTelemetry span.
        
        Создаёт бизнес-событие в EventBus/EventStore и одновременно
        добавляет его как span event в текущий OpenTelemetry trace.
        """
        payload = payload or {}
        source_id = source or "event_driven_engine"

        if not self.event_bus.running:
            await self.event_bus.start()

        event = Event(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            source=source_id,
            priority=priority,
            payload=payload,
        )

        # Публикуем PEPPER Event
        await self.event_bus.publish(event)
        await self.event_store.append(event)
        
        # ✨ Добавляем PEPPER Event в текущий OpenTelemetry span
        self._add_pepper_event_to_span(event)

    async def _execute_workflow_from_trigger(self, workflow_name: str, context: Dict[str, Any]):
        yaml_path = Path("workflow_pipelines") / f"{workflow_name}.yaml"
        workflow_definition = await self.load_and_validate_yaml(yaml_path)

        workflow_context = WorkflowContext(
            workflow_id=f"trigger::{workflow_name}::{uuid.uuid4().hex[:8]}",
            session_id=str(uuid.uuid4()),
            variables=context.copy(),
        )

        return await self.execute_workflow(workflow_definition, workflow_context)
    
    def _add_pepper_event_to_span(self, event: Event) -> None:
        """
        Добавляет PEPPER Event как событие в текущий OpenTelemetry span.
        
        Args:
            event: PEPPER Event для добавления в span
        """
        try:
            from opentelemetry import trace
            import json
            
            # Получаем текущий span
            current_span = trace.get_current_span()
            
            if current_span and current_span.is_recording():
                # Подготавливаем атрибуты события
                event_attributes = {
                    "pepper.event_id": event.event_id,
                    "pepper.event_type": event.event_type,
                    "pepper.source": event.source,
                    "pepper.priority": event.priority.name,
                    "pepper.timestamp": event.timestamp.isoformat(),
                }
                
                # Добавляем correlation_id если есть
                if event.correlation_id:
                    event_attributes["pepper.correlation_id"] = event.correlation_id
                
                # Добавляем aggregate_id если есть
                if event.aggregate_id:
                    event_attributes["pepper.aggregate_id"] = event.aggregate_id
                
                # Сериализуем payload (с ограничением размера)
                if event.payload:
                    try:
                        payload_str = json.dumps(event.payload, ensure_ascii=False, default=str)
                        # Ограничиваем размер до 1000 символов для span event
                        if len(payload_str) > 1000:
                            payload_str = payload_str[:997] + "..."
                        event_attributes["pepper.payload"] = payload_str
                    except Exception as e:
                        event_attributes["pepper.payload"] = f"<serialization error: {e}>"
                
                # Добавляем metadata если есть
                if event.metadata:
                    try:
                        metadata_str = json.dumps(event.metadata, ensure_ascii=False, default=str)
                        if len(metadata_str) > 500:
                            metadata_str = metadata_str[:497] + "..."
                        event_attributes["pepper.metadata"] = metadata_str
                    except Exception:
                        pass
                
                # Добавляем событие в span
                current_span.add_event(
                    name=f"pepper.{event.event_type}",
                    attributes=event_attributes
                )
                
                logger.debug(
                    f"✨ PEPPER Event '{event.event_type}' добавлен в OpenTelemetry span"
                )
                
        except Exception as e:
            # Не прерываем выполнение при ошибках добавления в span
            logger.debug(
                f"⚠️ Не удалось добавить PEPPER Event в OpenTelemetry span: {e}"
            )
