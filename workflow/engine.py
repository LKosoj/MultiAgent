"""
Workflow Engine - Главный движок выполнения рабочих процессов
==========================================================

WorkflowEngine расширяет DynamicAgentSystem, добавляя возможности
надежного выполнения многоэтапных процессов с персистентностью,
retry логикой и управлением ресурсами.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Any, Optional, Union
from pathlib import Path
import traceback

# Импорт из существующей системы (НЕ ИЗМЕНЯЕМ!)
from agent_system import DynamicAgentSystem

# Импорт компонентов workflow engine
from .models import (
    WorkflowDefinition, WorkflowResult, WorkflowContext, WorkflowStatus,
    StepResult, StepStatus, WorkflowStep, RetryPolicy, ResourceLimits,
    WorkflowExecutionError, WorkflowStepError
)
from .state_manager import WorkflowStateManager
from .retry_engine import RetryEngine
from .resource_manager import ResourceManager
from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context
from workflow_redaction import _redact_workflow_log_value

logger = logging.getLogger(__name__)

# Маркер «ключ отсутствовал в словаре» для безопасного отката step_outputs
# в _maybe_run_output_retry (None — валидное значение output, его нельзя
# использовать в качестве sentinel).
_SENTINEL_MISSING = object()

# Декларативное имя схемы для пост-парсинга str-output шага через json.loads.
# См. WorkflowStep.output_schema и _normalize_step_output ниже.
_JSON_OBJECT_SCHEMA = "json_object"


class WorkflowEngine(DynamicAgentSystem):
    """
    Workflow Engine как расширение DynamicAgentSystem
    
    НАСЛЕДУЕТ ВСЕ существующие возможности:
    - ✅ factory.create_agent() 
    - ✅ coordinate() методы
    - ✅ get_available_agents()
    - ✅ agent_pool управление
    
    ДОБАВЛЯЕТ новые workflow возможности:
    - 🆕 execute_workflow() - надежное выполнение
    - 🆕 resume_workflow() - восстановление после сбоев  
    - 🆕 workflow state management
    - 🆕 resource isolation
    - 🆕 retry policies
    """
    
    def __init__(self):
        # Инициализируем родительский класс БЕЗ изменений
        super().__init__()
        
        # Добавляем ТОЛЬКО новую функциональность
        self.state_manager = WorkflowStateManager()
        self.retry_engine = RetryEngine()
        self.resource_manager = ResourceManager()
        
        # Добавляем агрегатор для единого формирования результатов
        from .intelligence.aggregator import FinalAggregator
        self.aggregator = FinalAggregator()
        
        # Внутреннее состояние workflow engine
        self.active_workflows: Dict[str, Dict[str, Any]] = {}
        
        logger.info("🔄 WorkflowEngine инициализирован как расширение DynamicAgentSystem")
    
    # ===========================================
    # НОВЫЕ МЕТОДЫ ДЛЯ WORKFLOW FUNCTIONALITY  
    # ===========================================
    
    async def execute_workflow(self, workflow_definition: WorkflowDefinition,
                              context: Optional[WorkflowContext] = None,
                              client_id: Optional[str] = None) -> WorkflowResult:
        """
        НОВЫЙ метод: Надежное выполнение workflow
        
        Args:
            workflow_definition: Определение workflow (объект WorkflowDefinition)
            context: Контекст выполнения (опционально)
            client_id: ID клиента для квотирования (опционально)

        Returns:
            WorkflowResult с полными результатами выполнения
        """
        
        # Используем переданный WorkflowDefinition напрямую
        workflow_def = workflow_definition
        
        # Создаем контекст если не передан
        if context is None:
            context = WorkflowContext(
                workflow_id=str(uuid.uuid4()),
                session_id=str(uuid.uuid4()),
                client_id=client_id
            )
        
        workflow_id = context.workflow_id
        start_time = datetime.now()
        
        logger.info(f"🚀 Начинаем выполнение workflow '{workflow_def.name}' (ID: {workflow_id})")
        
        try:
            # 1-2. Инициализация и начальный checkpoint (через helper)
            resource_lease = await self._on_workflow_started(workflow_def, context, client_id, start_time)

            # 3. Выполняем шаги workflow
            if workflow_def.parallel_execution:
                step_results = await self._execute_steps_parallel(workflow_def, context)
            else:
                step_results = await self._execute_steps_sequential(workflow_def, context)

            # 4. Обработка результатов и завершение (общая логика)
            result = await self._finalize_workflow_execution(
                workflow_def, context, step_results, start_time, resource_lease
            )
            return result

        except Exception as e:
            logger.error(f"❌ Workflow '{workflow_def.name}' failed: {e}")
            if 'resource_lease' in locals():
                await self._on_workflow_failed(workflow_def, context, resource_lease, e)
            raise

    async def _get_workflow_status(self, workflow_id: str) -> Optional[WorkflowStatus]:
        """Возвращает текущий status workflow из checkpoint store."""
        try:
            return await self.state_manager.get_workflow_status(workflow_id)
        except Exception as exc:
            logger.warning(
                "⚠️ Не удалось получить статус workflow %s: %s",
                workflow_id,
                exc,
            )
            return None

    async def _is_workflow_cancelled(self, workflow_id: str) -> bool:
        """Проверяет, был ли workflow помечен как cancelled."""
        return await self._get_workflow_status(workflow_id) == WorkflowStatus.CANCELLED

    async def _build_cancelled_workflow_result(
        self,
        workflow_def: WorkflowDefinition,
        context: WorkflowContext,
        step_results: Dict[str, StepResult],
        start_time: datetime,
    ) -> WorkflowResult:
        """Формирует итоговый результат для мягко отменённого workflow."""
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        result = WorkflowResult(
            workflow_id=context.workflow_id,
            status=WorkflowStatus.CANCELLED,
            start_time=start_time,
            end_time=end_time,
            duration_seconds=duration,
            total_steps=len(workflow_def.steps),
            completed_steps=len(
                [r for r in step_results.values() if r.status == StepStatus.COMPLETED]
            ),
            failed_steps=len(
                [r for r in step_results.values() if r.status == StepStatus.FAILED]
            ),
            step_results=step_results,
            final_output=None,
            metadata={"cancelled": True},
        )
        logger.info(
            "🚫 Workflow %s остановлен после завершения активных шагов за %.1fс",
            context.workflow_id,
            duration,
        )
        return result
    
    async def _execute_steps_sequential(self, workflow_def, context):
        """Последовательное выполнение шагов (старая логика)"""
        step_results = {}
        
        for step in workflow_def.steps:
            if await self._is_workflow_cancelled(context.workflow_id):
                logger.info(
                    "🚫 Workflow %s отменён до запуска шага %s",
                    context.workflow_id,
                    step.id,
                )
                break

            # Проверяем зависимости
            if not self._check_step_dependencies(step, step_results):
                logger.info(f"⏸️ Пропускаем шаг {step.id} - зависимости не выполнены")
                step_results[step.id] = StepResult(
                    step_id=step.id,
                    status=StepStatus.SKIPPED,
                    start_time=datetime.now(),
                    end_time=datetime.now()
                )
                continue
            
            # Проверяем условие выполнения
            if self._should_skip_step_by_condition(step, context):
                step_results[step.id] = StepResult(
                    step_id=step.id,
                    status=StepStatus.SKIPPED,
                    output=context.step_outputs.get(step.id),
                    start_time=datetime.now(),
                    end_time=datetime.now()
                )
                continue
            
            # Выполняем шаг
            step_result = await self._execute_workflow_step(
                step, context, workflow_def, step_results=step_results
            )
            step_results[step.id] = step_result
            
            # Обрабатываем успешный/неуспешный шаг единым способом
            if step_result.status == StepStatus.COMPLETED:
                await self._on_step_completed(context.workflow_id, step, step_result, context, step_results)
                if await self._is_workflow_cancelled(context.workflow_id):
                    logger.info(
                        "🚫 Workflow %s отменён после шага %s",
                        context.workflow_id,
                        step.id,
                    )
                    break
            else:
                # Шаг провален
                logger.error(f"❌ Шаг {step.id} провален: {step_result.error}")
                
                # Выполняем rollback если определен
                if step.rollback_action:
                    await self._execute_rollback(step.rollback_action, context, step_result)
                
                # Прерываем выполнение workflow
                raise WorkflowExecutionError(f"Workflow step {step.id} failed: {step_result.error}")
        
        return step_results
    
    async def _execute_steps_parallel(self, workflow_def, context):
        """Параллельное выполнение шагов"""
        from .orchestration.parallel_executor import ParallelWorkflowExecutor
        
        # Сохраняем workflow_definition как временный атрибут для параллельных задач
        context._workflow_definition = workflow_def
        
        parallel_executor = ParallelWorkflowExecutor(
            max_concurrent=workflow_def.max_parallel_steps
        )
        
        return await parallel_executor.execute_steps_parallel(
            workflow_def.steps,
            context,
            step_executor=self._execute_workflow_step_wrapper,
            dependency_checker=self._check_step_dependencies,
            condition_checker=self._should_skip_step_by_condition,
            stop_checker=lambda: self._is_workflow_cancelled(context.workflow_id),
            stop_on_failure=(workflow_def.error_handling.get("on_failure", "continue") != "continue"),
        )
    
    async def _execute_workflow_step_wrapper(self, step, context):
        """Обертка для выполнения шага с обработкой событий"""
        workflow_def = context._workflow_definition if hasattr(context, '_workflow_definition') else None
        step_results = getattr(context, "_workflow_step_results", None)
        step_result = await self._execute_workflow_step(
            step, context, workflow_def, step_results=step_results
        )
        
        # Обрабатываем события шага
        if step_result.status == StepStatus.COMPLETED:
            if step_results is not None:
                step_results[step.id] = step_result
            await self._on_step_completed(
                context.workflow_id,
                step,
                step_result,
                context,
                step_results if step_results is not None else {},
            )
        elif step_result.status == StepStatus.FAILED:
            logger.error(f"❌ Шаг {step.id} провален: {step_result.error}")
            if step.rollback_action:
                await self._execute_rollback(step.rollback_action, context, step_result)
            raise WorkflowExecutionError(f"Workflow step {step.id} failed: {step_result.error}")
        
        return step_result
    
    async def _finalize_workflow_execution(self, workflow_def, context, step_results, start_time, resource_lease):
        """Финализация выполнения workflow"""
        try:
            if await self._is_workflow_cancelled(context.workflow_id):
                return await self._build_cancelled_workflow_result(
                    workflow_def,
                    context,
                    step_results,
                    start_time,
                )

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            # Формируем итоговый результат через агрегатор
            final_output = await self.aggregator.aggregate_final_result(
                step_results, workflow_def, context
            )
            completed_steps = len([r for r in step_results.values() if r.status == StepStatus.COMPLETED])
            failed_steps = len([r for r in step_results.values() if r.status == StepStatus.FAILED])
            stop_on_failure = workflow_def.error_handling.get("on_failure", "continue") != "continue"
            workflow_status = WorkflowStatus.FAILED if failed_steps and stop_on_failure else WorkflowStatus.COMPLETED

            if workflow_status == WorkflowStatus.COMPLETED:
                await self._on_workflow_completed(context.workflow_id, final_output)
            else:
                await self.state_manager.save_checkpoint(
                    workflow_id=context.workflow_id,
                    status=WorkflowStatus.FAILED,
                    context=context,
                    step_results=step_results,
                    metadata={"workflow_name": workflow_def.name, "error": "Workflow failed steps"},
                )
            result = WorkflowResult(
                workflow_id=context.workflow_id,
                status=workflow_status,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                total_steps=len(workflow_def.steps),
                completed_steps=completed_steps,
                failed_steps=failed_steps,
                step_results=step_results,
                final_output=final_output
            )

            logger.info(
                "✅ Workflow %s завершен со статусом %s за %.1fс",
                context.workflow_id,
                workflow_status.value,
                duration,
            )
            return result
        finally:
            if resource_lease is not None:
                await self._release_workflow_resources(context.workflow_id)
    
    async def resume_workflow(self, workflow_id: str,
                            client_id: Optional[str] = None) -> WorkflowResult:
        """
        Восстановление workflow с последнего checkpoint'а.

        НЕ РЕАЛИЗОВАНО (см. Raises). Для корректного resume нужны две вещи, которых
        сейчас нет: (1) checkpoint должен хранить WorkflowDefinition — но
        state_manager.save_checkpoint его не сериализует, а metadata не содержит
        yaml_path; (2) цикл _execute_steps_sequential/_execute_steps_parallel должен
        пропускать уже завершённые шаги — сейчас он прогоняет ВСЕ шаги, поэтому
        наивный повторный запуск дублировал бы побочные эффекты. Прод восстанавливает
        иначе и этот метод не вызывает: StoryBookManager/core/pipeline_runner.py
        (resume_workflow_from_checkpoint / resume_pipeline) делает from_yaml +
        execute_workflow. Реализация полноценного resume — отдельная фича (миграция
        схемы checkpoint + skip-completed в исполнителе), а не багфикс.

        Args:
            workflow_id: ID workflow для восстановления
            client_id: ID клиента (опционально)

        Returns:
            WorkflowResult с результатами продолжения выполнения

        Raises:
            WorkflowExecutionError: всегда — resume пока не реализован (см. описание выше).
                Согласуется с объявленным типом возврата WorkflowResult и общим контрактом
                ошибок workflow-слоя, поэтому existing except WorkflowExecutionError ловят его
                штатно (в отличие от NotImplementedError, который прошёл бы мимо как 500).
                Вызывающий код должен использовать execute_workflow с from_yaml (как
                pipeline_runner) для реального восстановления.
        """
        logger.info(f"🔄 Восстанавливаем workflow {workflow_id}")
        raise WorkflowExecutionError(
            f"Cannot resume workflow {workflow_id}: resume is not yet implemented. "
            "Workflow definition (yaml_path) must be provided alongside the checkpoint "
            "to reconstruct execution from a saved step."
        )
    
    async def get_workflow_status(self, workflow_id: str) -> Optional[WorkflowStatus]:
        """Получение статуса workflow"""
        return await self.state_manager.get_workflow_status(workflow_id)
    
    async def cancel_workflow(self, workflow_id: str, reason: str = "User cancelled"):
        """Отмена выполнения workflow"""
        logger.info(f"🚫 Отменяем workflow {workflow_id}: {reason}")
        
        # Освобождаем ресурсы
        await self.resource_manager.release_workflow_resources(workflow_id)
        
        # Обновляем статус
        latest_checkpoint = await self.state_manager.store.get_latest_checkpoint(workflow_id)
        context = (
            latest_checkpoint.context
            if latest_checkpoint and latest_checkpoint.context
            else WorkflowContext(workflow_id=workflow_id, session_id=workflow_id)
        )
        step_results = latest_checkpoint.step_results if latest_checkpoint else {}
        current_step = latest_checkpoint.current_step if latest_checkpoint else None
        await self.state_manager.save_checkpoint(
            workflow_id=workflow_id,
            status=WorkflowStatus.CANCELLED,
            context=context,
            step_results=step_results,
            current_step=current_step,
            metadata={"cancellation_reason": reason}
        )
    
    def get_system_metrics(self) -> Dict[str, Any]:
        """Получение метрик системы workflow"""
        return {
            "resource_manager": self.resource_manager.get_system_stats(),
            "retry_engine": self.retry_engine.get_retry_statistics(),
            "active_workflows": len(self.active_workflows),
            "timestamp": datetime.now().isoformat()
        }
    
    # ===========================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # ===========================================
    
    async def _on_workflow_started(self, workflow_def: WorkflowDefinition, context: WorkflowContext,
                                   client_id: Optional[str], start_time: datetime):
        """Общая инициализация workflow: выделение ресурсов и первый checkpoint"""
        resource_lease = await self.resource_manager.acquire_workflow_resources(
            workflow_id=context.workflow_id,
            client_id=client_id,
            requirements=workflow_def.global_resource_limits or ResourceLimits()
        )
        await self.state_manager.save_checkpoint(
            workflow_id=context.workflow_id,
            status=WorkflowStatus.RUNNING,
            context=context,
            step_results={},
            metadata={"workflow_name": workflow_def.name, "start_time": start_time.isoformat()}
        )
        return resource_lease

    async def _on_workflow_completed(self, workflow_id: str, final_output: Dict[str, Any]) -> None:
        """Общий финал workflow: сохранение финального состояния"""
        await self.state_manager.mark_workflow_completed(workflow_id, final_output)

    async def _on_workflow_failed(
        self,
        workflow_def: WorkflowDefinition,
        context: WorkflowContext,
        resource_lease,
        error: Exception,
    ) -> None:
        """Общий финал workflow при ошибке: checkpoint FAILED и освобождение ресурсов."""
        try:
            await self.state_manager.save_checkpoint(
                workflow_id=context.workflow_id,
                status=WorkflowStatus.FAILED,
                context=context,
                step_results={},
                current_step=context.current_step,
                metadata={"workflow_name": workflow_def.name, "error": str(error)},
            )
        finally:
            if resource_lease is not None:
                await self._release_workflow_resources(context.workflow_id)

    async def _release_workflow_resources(self, workflow_id: str) -> None:
        try:
            if workflow_id not in self.resource_manager.active_workflows:
                return
            await self.resource_manager.release_workflow_resources(workflow_id)
        except Exception as exc:
            logger.warning("⚠️ Не удалось освободить ресурсы workflow %s: %s", workflow_id, exc)

    def _normalize_step_output(
        self, step: WorkflowStep, raw_output: Any
    ) -> tuple[Any, bool]:
        """Нормализует output шага с учётом step.output_schema.

        Контракт:
        - Если raw_output не str и schema не задана — возвращает (raw_output, False).
        - Если schema == "json_object" и raw_output уже dict — возвращает dict.
        - Если schema == "json_object" и raw_output не str/dict → WorkflowStepError.
        - Если schema == "json_object" и raw пустой → WorkflowStepError.
        - Пытается json.loads. На JSONDecodeError при schema == "json_object"
          → WorkflowStepError с первыми 200 символами raw.
          Без schema — возвращает (raw_output, False) (бэк-совместимо).
        - Если parsed — dict → (dict, True). Иначе array/scalar
          при json_object → WorkflowStepError.
        """
        schema = getattr(step, "output_schema", None)
        if not isinstance(raw_output, str):
            if schema is None:
                return raw_output, False
            if schema != _JSON_OBJECT_SCHEMA:
                raise WorkflowStepError(
                    f"Step '{step.id}' declared unsupported output_schema={schema!r}"
                )
            if isinstance(raw_output, dict):
                self._validate_step_output_requirements(step, raw_output)
                return raw_output, False
            raise WorkflowStepError(
                f"Step '{step.id}' declared output_schema=json_object but "
                f"value is {type(raw_output).__name__}, expected dict"
            )
        if schema is None:
            return raw_output, False

        if schema == _JSON_OBJECT_SCHEMA and raw_output.strip() == "":
            raise WorkflowStepError(
                f"Step '{step.id}' declared output_schema=json_object but "
                f"returned empty string"
            )

        if schema != _JSON_OBJECT_SCHEMA:
            raise WorkflowStepError(
                f"Step '{step.id}' declared unsupported output_schema={schema!r}"
            )

        # W1-review: LLM-агенты иногда оборачивают JSON в ```json ... ```
        # code-fence несмотря на инструкцию. Снимаем обёртку до json.loads,
        # чтобы не зависеть только от prompt-discipline.
        candidate = raw_output
        fence_match = re.match(
            r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$",
            raw_output,
            flags=re.DOTALL,
        )
        if fence_match is not None:
            candidate = fence_match.group(1).strip()

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            snippet = _redact_workflow_log_value(raw_output[:200])
            raise WorkflowStepError(
                f"Step '{step.id}' declared output_schema=json_object but "
                f"output is not valid JSON: {exc}. Raw (first 200 chars): {snippet!r}"
            ) from exc

        if isinstance(parsed, dict):
            self._validate_step_output_requirements(step, parsed)
            return parsed, True
        # W1-review (#14): json_object schema требует dict — массив/скаляр
        # → fail-fast, чтобы downstream-step не получил неожиданный shape.
        raise WorkflowStepError(
            f"Step '{step.id}' declared output_schema=json_object but parsed "
            f"value is {type(parsed).__name__}, expected dict"
        )

    def _validate_step_output_requirements(self, step: WorkflowStep, output: Dict[str, Any]) -> None:
        requirements = getattr(step, "output_schema_requirements", None)
        if not requirements:
            return
        if not isinstance(requirements, dict):
            raise WorkflowStepError(
                f"Step '{step.id}' has invalid output_schema_requirements: expected dict"
            )

        required = requirements.get("required", [])
        if not isinstance(required, list) or not all(isinstance(field, str) for field in required):
            raise WorkflowStepError(
                f"Step '{step.id}' has invalid output_schema_requirements.required"
            )
        missing = [field for field in required if field not in output]
        if missing:
            raise WorkflowStepError(
                f"Step '{step.id}' output_schema_requirements missing required fields: {missing}"
            )

        properties = requirements.get("properties", {})
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            raise WorkflowStepError(
                f"Step '{step.id}' has invalid output_schema_requirements.properties"
            )
        for field, field_schema in properties.items():
            if field not in output:
                continue
            if not isinstance(field_schema, dict):
                raise WorkflowStepError(
                    f"Step '{step.id}' has invalid output_schema_requirements for field '{field}'"
                )
            allowed_values = field_schema.get("enum")
            if allowed_values is not None:
                if not isinstance(allowed_values, list):
                    raise WorkflowStepError(
                        f"Step '{step.id}' has invalid enum for field '{field}'"
                    )
                if output[field] not in allowed_values:
                    raise WorkflowStepError(
                        f"Step '{step.id}' field '{field}' must be one of {allowed_values}, "
                        f"got {output[field]!r}"
                    )

    async def _on_step_completed(self, workflow_id: str, step: WorkflowStep, step_result: StepResult,
                                context: WorkflowContext, step_results: Dict[str, StepResult]) -> None:
        """Общая обработка успешного шага: обновление контекста и checkpoint"""
        # Нормализуем output по step.output_schema (если задан и raw — str с JSON).
        # При успешном парсе подменяем step_result.output, чтобы downstream видел dict.
        normalized, parsed = self._normalize_step_output(step, step_result.output)
        if parsed:
            step_result.output = normalized

        # Обновляем контекст результатами шага
        self._write_step_output(context, step.id, normalized)

        # Сохраняем checkpoint после каждого успешного шага
        await self.state_manager.save_checkpoint(
            workflow_id=workflow_id,
            status=WorkflowStatus.RUNNING,
            context=context,
            step_results=step_results,
            current_step=step.id
        )

    def _step_dotted_output_keys(self, context: WorkflowContext, step_id: str) -> List[str]:
        prefix = f"{step_id}."
        return [
            key for key in context.step_outputs
            if isinstance(key, str) and key.startswith(prefix)
        ]

    def _clear_step_dotted_outputs(self, context: WorkflowContext, step_id: str) -> None:
        for key in self._step_dotted_output_keys(context, step_id):
            context.step_outputs.pop(key, None)

    def _write_step_output(self, context: WorkflowContext, step_id: str, output: Any) -> None:
        self._clear_step_dotted_outputs(context, step_id)
        context.step_outputs[step_id] = output
        if isinstance(output, dict):
            for key, value in output.items():
                context.step_outputs[f"{step_id}.{key}"] = value
                logger.debug(
                    "📝 Сохранили %s.%s = %s",
                    step_id,
                    key,
                    _redact_workflow_log_value(value),
                )

    def _collect_context_variables(self, context: WorkflowContext) -> Dict[str, Any]:
        """Собирает все доступные для подстановки переменные из контекста."""
        all_variables: Dict[str, Any] = {}
        if context.variables:
            all_variables.update(context.variables)
        if context.step_outputs:
            all_variables.update(context.step_outputs)
        # Глобальные идентификаторы доступны всегда
        if context.session_id:
            all_variables.setdefault("session_id", context.session_id)
        if context.workflow_id:
            all_variables.setdefault("workflow_id", context.workflow_id)
        # 6.14: run_id — отдельный идентификатор запуска, доступный наравне с session_id.
        # Берётся из context.variables (передаётся через parameters в WorkflowManager),
        # с fallback на context.run_id (если когда-нибудь появится атрибут).
        ctx_run_id = getattr(context, "run_id", None)
        if ctx_run_id:
            all_variables.setdefault("run_id", ctx_run_id)
        return all_variables

    @staticmethod
    def _format_value_for_task(value: Any) -> Any:
        """6.2: dict/list значения сериализуются через json.dumps для подстановки в task,
        чтобы избежать некорректного str({'key': 'value'}) с одинарными кавычками.
        Скалярные значения остаются как есть (str() в str.format)."""
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return str(value)
        return value

    def _format_task_with_variables(self, task: str, context: WorkflowContext, step_id: str) -> str:
        """Подстановка переменных в задачу шага"""
        all_variables = self._collect_context_variables(context)

        if all_variables:
            try:
                # 6.2: оборачиваем dict/list значения в json.dumps до format()
                format_vars = {k: self._format_value_for_task(v) for k, v in all_variables.items()}
                formatted_task = task.format(**format_vars)
                logger.info(
                    "📝 Подставлены переменные: %s -> %s",
                    _redact_workflow_log_value(task),
                    _redact_workflow_log_value(formatted_task),
                )
                return formatted_task
            except KeyError as e:
                logger.warning(f"⚠️ Переменная {e} не найдена в контексте для шага '{step_id}'")
                logger.info(f"📋 Доступные переменные: {list(all_variables.keys())}")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка подстановки переменных в шаге '{step_id}': {e}")

        return task

    def _substitute_variables_in_metadata(
        self,
        metadata: Any,
        context_vars: Dict[str, Any],
        *,
        step_id: str,
        path: str = "metadata",
    ) -> Any:
        """6.1: Рекурсивная подстановка переменных в step.metadata (dict/list/str).
        Fail-fast при unresolved {var} — это критично, потому что metadata пробрасывается
        в agent/tool (через preload_agents, manager prompt, db-tool параметры), и
        unresolved строка вида '{max_rows}' попадёт в БД-инструмент как литерал.
        """
        if isinstance(metadata, dict):
            return {
                key: self._substitute_variables_in_metadata(
                    value, context_vars, step_id=step_id, path=f"{path}.{key}"
                )
                for key, value in metadata.items()
            }
        if isinstance(metadata, list):
            return [
                self._substitute_variables_in_metadata(
                    item, context_vars, step_id=step_id, path=f"{path}[{idx}]"
                )
                for idx, item in enumerate(metadata)
            ]
        if isinstance(metadata, str):
            unresolved = [
                name
                for name in re.findall(r"\{([^{}]+)\}", metadata)
                if not self._resolve_variable_reference(name, context_vars)[1]
            ]
            if unresolved:
                raise WorkflowExecutionError(
                    f"Unresolved metadata placeholders {unresolved} в "
                    f"шаге '{step_id}' (поле {path}). "
                    f"Доступные переменные: {sorted(context_vars.keys())}"
                )
            substituted = self._substitute_variables_in_string(metadata, context_vars)
            return substituted
        return metadata

    def _step_with_substituted_metadata(
        self, step: WorkflowStep, context: WorkflowContext
    ) -> WorkflowStep:
        """6.1: Возвращает shallow-копию шага со step.metadata, в котором подставлены
        переменные из контекста. Используется до передачи step в agent/tool."""
        if not step.metadata:
            return step
        context_vars = self._collect_context_variables(context)
        try:
            new_metadata = self._substitute_variables_in_metadata(
                step.metadata, context_vars, step_id=step.id
            )
        except WorkflowExecutionError:
            raise
        except Exception as e:
            # Сохраняем поведение — если непредвиденная ошибка, бросаем как fail-fast,
            # чтобы не отправлять полу-подставленную metadata в downstream.
            raise WorkflowExecutionError(
                f"Ошибка подстановки переменных в metadata шага '{step.id}': {e}"
            ) from e

        if new_metadata is step.metadata:
            return step

        # Создаём shallow-копию шага с новой metadata. dataclasses.replace в проекте не
        # используется в этом модуле, поэтому конструируем вручную через __class__.
        new_step = WorkflowStep(
            id=step.id,
            task=step.task,
            depends_on=list(step.depends_on),
            condition=step.condition,
            retry_policy=step.retry_policy,
            resource_limits=step.resource_limits,
            timeout=step.timeout,
            rollback_action=step.rollback_action,
            metadata=new_metadata,
            step_type=step.step_type,
            agent_type=step.agent_type,
            tool_name=step.tool_name,
            tool_params=dict(step.tool_params) if step.tool_params else {},
            output_retry_policy=step.output_retry_policy,
            output_schema=step.output_schema,
        )
        return new_step

    def _should_skip_step_by_condition(self, step: WorkflowStep, context: WorkflowContext) -> bool:
        """Единая проверка условия выполнения шага"""
        if step.condition and not self._evaluate_condition(step.condition, context):
            skip_output = step.metadata.get("skip_output") if isinstance(step.metadata, dict) else None
            if skip_output is not None:
                all_variables = self._collect_context_variables(context)
                try:
                    if isinstance(skip_output, dict):
                        skip_output = self._substitute_variables_in_params(skip_output, all_variables)
                    elif isinstance(skip_output, str):
                        skip_output = self._substitute_variables_in_string(skip_output, all_variables)
                except Exception as e:
                    logger.warning(f"⚠️ Ошибка подготовки skip_output для шага '{step.id}': {e}")
                self._write_step_output(context, step.id, skip_output)
            logger.info(f"⏸️ Пропускаем шаг {step.id} - условие не выполнено")
            return True
        return False
    
    async def _execute_workflow_step(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        workflow_def: WorkflowDefinition,
        step_results: Optional[Dict[str, StepResult]] = None,
    ) -> StepResult:
        """Выполнение одного шага workflow"""

        logger.info(f"🔄 Выполняем шаг {step.id} с агентом {step.agent_type}")

        # 6.1: Подставляем переменные в step.metadata ДО передачи в agent/tool.
        # Fail-fast при unresolved placeholders в metadata.
        step_with_metadata = self._step_with_substituted_metadata(step, context)

        # Создаем функцию для выполнения шага
        async def step_executor(exec_context: Dict[str, Any]) -> Any:
            # Подставляем переменные из контекста в задачу
            formatted_task = self._format_task_with_variables(step_with_metadata.task, context, step_with_metadata.id)

            # Обработка в зависимости от типа шага
            if step_with_metadata.step_type == "tool":
                # Прямой вызов инструмента
                return await self._execute_tool_step(step_with_metadata, context, formatted_task)
            else:
                # Выполнение через агента (как раньше)
                return await self._execute_agent_step(step_with_metadata, context, formatted_task)

        # Выполняем с retry логикой
        retry_policy = step.retry_policy or workflow_def.global_retry_policy

        retry_kwargs = {
            "step_id": step.id,
            "step_func": step_executor,
            "context": context.__dict__,
            "retry_policy": retry_policy,
        }
        if step.timeout is not None:
            retry_kwargs["timeout"] = step.timeout
        try:
            step_result = await self.retry_engine.execute_with_retry(**retry_kwargs)
        except TypeError as exc:
            if "unexpected keyword argument 'timeout'" not in str(exc) or "timeout" not in retry_kwargs:
                raise
            retry_kwargs.pop("timeout", None)
            step_result = await self.retry_engine.execute_with_retry(**retry_kwargs)

        # Cross-step feedback retry (EPIC 6 Block A фикс): после успешного шага
        # проверяем step.output_retry_policy. Если condition (на свежем output
        # этого шага) выполнено и счётчик итераций не превышен, упаковываем output
        # в context.variables[feedback_field] и заново выполняем rerun_step,
        # затем повторяем текущий шаг (рекурсивно через _execute_workflow_step).
        # Сравнение по value — см. NB в _maybe_run_output_retry.
        if getattr(step_result.status, "value", step_result.status) == StepStatus.COMPLETED.value:
            async def execute_step_with_results(
                retry_step: WorkflowStep,
                retry_context: WorkflowContext,
                retry_workflow_def: WorkflowDefinition,
            ) -> StepResult:
                return await self._execute_workflow_step(
                    retry_step,
                    retry_context,
                    retry_workflow_def,
                    step_results=step_results,
                )

            retried = await self._maybe_run_output_retry(
                step,
                step_result,
                context,
                workflow_def,
                step_executor=execute_step_with_results,
                step_results=step_results,
            )
            if retried is not None:
                return retried

        return step_result

    async def _maybe_run_output_retry(
        self,
        step: WorkflowStep,
        step_result: StepResult,
        context: WorkflowContext,
        workflow_def: WorkflowDefinition,
        step_executor: Optional[
            Callable[[WorkflowStep, WorkflowContext, WorkflowDefinition], Awaitable[StepResult]]
        ] = None,
        step_results: Optional[Dict[str, StepResult]] = None,
        rerun_step_committed_by_executor: bool = False,
    ) -> Optional[StepResult]:
        """Проверка output_retry_policy и cross-step rerun с feedback.

        Возвращает новый StepResult, если был выполнен retry; иначе None.
        """
        policy = getattr(step, "output_retry_policy", None)
        if not isinstance(policy, dict):
            return None

        condition = policy.get("condition")
        rerun_step_id = policy.get("rerun_step")
        max_iterations = int(policy.get("max_iterations", 1))
        feedback_field = policy.get("feedback_field")
        if not condition or not rerun_step_id or not feedback_field:
            logger.warning(
                "⚠️ output_retry_policy для шага '%s' неполный: %s",
                step.id,
                policy,
            )
            return None

        # Output этого шага уже сохранён в context.step_outputs/_on_step_completed
        # для sequential-пути; для parallel-пути _on_step_completed вызывается в
        # wrapper'е, который тоже работает до возврата сюда. Однако в общем случае
        # на момент вызова _execute_workflow_step мы НЕ можем гарантировать, что
        # _on_step_completed уже отработал. Поэтому явно подкладываем output
        # текущего шага в локальный snapshot context перед оценкой condition.
        prev_output = context.step_outputs.get(step.id, _SENTINEL_MISSING)
        prev_dotted: Dict[str, Any] = {
            key: context.step_outputs[key]
            for key in self._step_dotted_output_keys(context, step.id)
        }
        keep_current_output = False
        try:
            # W1-review: шаг уже помечен COMPLETED (мы попали сюда из ветки
            # status == COMPLETED). WorkflowStepError из _normalize_step_output
            # на этом этапе раньше рушил FSM (исключение пробивало наверх ДО
            # очистки prev_output). Логируем и выходим без retry — output_retry
            # не применим к ответам, которые не парсятся как JSON.
            try:
                normalized, parsed = self._normalize_step_output(step, step_result.output)
            except WorkflowStepError as norm_exc:
                logger.error(
                    "❌ output_retry_policy: невалидный output_schema у шага '%s' "
                    "(%s); пропускаем retry, оставляем исходный COMPLETED-результат",
                    step.id,
                    norm_exc,
                )
                return None
            if parsed:
                step_result.output = normalized
            output = normalized
            self._write_step_output(context, step.id, output)

            if not self._evaluate_condition(condition, context):
                keep_current_output = True
                return None

            counters = context.variables.setdefault("__output_retry_counters__", {})
            current_iter = int(counters.get(step.id, 0))
            if current_iter >= max_iterations:
                logger.info(
                    "🔁 output_retry_policy: шаг '%s' исчерпал лимит итераций (%d/%d), feedback retry не запускается",
                    step.id,
                    current_iter,
                    max_iterations,
                )
                keep_current_output = True
                return None

            # Ищем rerun_step в workflow_def
            rerun_step = next(
                (s for s in workflow_def.steps if s.id == rerun_step_id),
                None,
            )
            if rerun_step is None:
                logger.warning(
                    "⚠️ output_retry_policy: rerun_step '%s' не найден в workflow",
                    rerun_step_id,
                )
                keep_current_output = True
                return None

            # Готовим feedback: сериализуем output в JSON-строку для подстановки
            # в task rerun_step (через context.variables[feedback_field]).
            try:
                feedback_payload = json.dumps(
                    step_result.output, ensure_ascii=False, default=str
                )
            except (TypeError, ValueError):
                feedback_payload = str(step_result.output)

            counters[step.id] = current_iter + 1
            context.variables[feedback_field] = feedback_payload
            logger.info(
                "🔁 output_retry_policy: условие '%s' выполнено, перезапускаем '%s' "
                "(итерация %d/%d, feedback_field=%s)",
                condition,
                rerun_step_id,
                current_iter + 1,
                max_iterations,
                feedback_field,
            )

            execute_step = step_executor or self._execute_workflow_step

            # Запускаем rerun_step заново
            rerun_result = await execute_step(rerun_step, context, workflow_def)
            # NB: enum identity не используем — в тестах с lightweight workflow
            # модуль models переинициализируется, и StepStatus.COMPLETED в engine
            # может не совпасть с StepStatus.COMPLETED в моках. Сравниваем по value.
            rerun_status_value = getattr(rerun_result.status, "value", rerun_result.status)
            if rerun_status_value != StepStatus.COMPLETED.value:
                logger.error(
                    "❌ output_retry_policy: rerun_step '%s' завершился со статусом %s; "
                    "оставляем исходный результат шага '%s'",
                    rerun_step_id,
                    rerun_result.status,
                    step.id,
                )
                keep_current_output = True
                return None
            if step_results is not None:
                step_results[rerun_step_id] = rerun_result
            # Сохраняем результат rerun_step в context, чтобы зависящие от него
            # шаги (в т.ч. текущий) видели свежий output.
            if not rerun_step_committed_by_executor:
                await self._on_step_completed(
                    context.workflow_id,
                    rerun_step,
                    rerun_result,
                    context,
                    step_results if step_results is not None else {},
                )

            # Перезапускаем текущий шаг. Если он успешен, не откатываем
            # context.step_outputs в finally: enhanced executor может уже
            # сохранить свежий output через _on_step_completed внутри executor.
            retried_step_result = await execute_step(step, context, workflow_def)
            retried_status_value = getattr(
                retried_step_result.status,
                "value",
                retried_step_result.status,
            )
            keep_current_output = retried_status_value == StepStatus.COMPLETED.value
            return retried_step_result
        finally:
            # Восстанавливаем step_outputs только если retry не дошёл до
            # финального успешного output текущего шага. Если condition не
            # сработал или rerun_step не удался, текущий output остаётся
            # финальным COMPLETED-результатом и должен быть виден downstream.
            if not keep_current_output:
                self._clear_step_dotted_outputs(context, step.id)
                if prev_output is _SENTINEL_MISSING:
                    context.step_outputs.pop(step.id, None)
                else:
                    context.step_outputs[step.id] = prev_output
                context.step_outputs.update(prev_dotted)
    
    async def _execute_agent_step(self, step: WorkflowStep, context: WorkflowContext, task: str) -> Any:
        """Выполнение шага через агента в thread pool"""
        # Специальная обработка для менеджера с предзагрузкой агентов
        if step.agent_type == 'manager' and step.metadata and step.metadata.get('preload_agents'):
            return await self._execute_manager_with_preloaded_agents(
                step, context, task
            )
        
        logger.info(f"🤖 Выполняем шаг '{step.id}' с агентом '{step.agent_type}' в thread pool")
        logger.info("📋 Задача: %s", _redact_workflow_log_value(task))
        
        # Выполняем агента в thread pool для разблокировки event loop
        def _execute_agent_sync():
            """Синхронное выполнение агента в отдельном потоке"""
            token = set_tool_runtime_context(step.metadata or {})
            try:
                # ПРЯМОЙ вызов указанного агента (без анализа задачи!)
                # _enhanced_pipeline_type позволяет subclass'у передать pipeline_type
                # без мутации self.factory (thread-safe альтернатива monkey-patching).
                _pipeline_type = getattr(step, '_enhanced_pipeline_type', None) or "workflow"
                agent = self.factory.create_agent(
                    profile_type=step.agent_type,
                    session_id=context.session_id,
                    task=task,
                    pipeline_type=_pipeline_type,
                )
                
                # Выполняем задачу напрямую через агента
                result = agent.run(task, stream=False)
                
                # Регистрируем API вызов для мониторинга
                self.resource_manager.record_api_call(context.workflow_id)
                
                return result
                
            except Exception as e:
                logger.error(
                    "❌ Ошибка выполнения агента '%s': %s",
                    step.agent_type,
                    _redact_workflow_log_value(str(e)),
                )
                raise
            finally:
                reset_tool_runtime_context(token)
        
        # Выполняем в thread pool текущего event loop
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _execute_agent_sync)

        return result

    async def _execute_tool_step(self, step: WorkflowStep, context: WorkflowContext, task: str) -> Any:
        """Выполнение шага через прямой вызов инструмента в thread pool"""
        logger.info(f"🔧 Выполняем шаг '{step.id}' с инструментом '{step.tool_name}' в thread pool")
        logger.info("📋 Задача: %s", _redact_workflow_log_value(task))
        logger.info(
            "📋 Параметры инструмента (до подстановки): %s",
            _redact_workflow_log_value(step.tool_params),
        )
        
        # Получаем инструмент из фабрики
        tool_function = self.factory._create_tool(step.tool_name)
        if tool_function is None:
            logger.error(f"❌ Инструмент '{step.tool_name}' не найден в tool_mapping")
            logger.error(f"📋 Доступные инструменты: {list(self.factory.tool_mapping.keys())[:20]}...")
            raise ValueError(f"Инструмент '{step.tool_name}' не найден")
        
        logger.info(f"✅ Инструмент '{step.tool_name}' успешно загружен: {type(tool_function)}")
        
        # Подготавливаем параметры инструмента
        tool_params = step.tool_params.copy()
        
        # Подставляем переменные из контекста в параметры
        # Объединяем исходные переменные с результатами предыдущих шагов
        # 6.14: помимо session_id/workflow_id также пробрасываем run_id (если он есть в variables/context)
        all_variables = self._collect_context_variables(context)

        if all_variables:
            try:
                # Рекурсивная подстановка переменных в параметрах
                tool_params = self._substitute_variables_in_params(tool_params, all_variables)
                logger.info(
                    "📝 Подставлены переменные в параметры инструмента: %s",
                    _redact_workflow_log_value(tool_params),
                )
            except Exception as e:
                logger.warning(f"⚠️ Ошибка подстановки переменных в параметры инструмента '{step.id}': {e}")
                logger.info(f"📋 Доступные переменные: {list(all_variables.keys())}")
        
        # Удаляем session_id из tool_params, если он там есть, так как он передается отдельным параметром
        session_id = tool_params.pop('session_id', context.session_id)
        
        logger.info(
            "📋 Финальные параметры для инструмента '%s': %s",
            step.tool_name,
            _redact_workflow_log_value(tool_params),
        )
        logger.info(f"📋 session_id: {session_id}")
        
        # Выполняем инструмент в thread pool для разблокировки event loop
        def _execute_tool_sync():
            """Синхронное выполнение инструмента в отдельном потоке"""
            try:
                # Используем ToolManager для выполнения инструмента с телеметрией
                from tool_manager import get_tool_manager
                tool_manager = get_tool_manager()
                
                logger.info(
                    "🚀 Запуск инструмента '%s' через ToolManager с параметрами: %s",
                    step.tool_name,
                    _redact_workflow_log_value(tool_params),
                )
                result = tool_manager.run_tool(
                    tool_name=step.tool_name,
                    tool_function=tool_function,
                    task_description=task,
                    session_id=session_id,
                    **tool_params
                )
                logger.info(f"✅ Инструмент '{step.tool_name}' завершился успешно")
                
                # Регистрируем API вызов для мониторинга
                self.resource_manager.record_api_call(context.workflow_id)
                
                return result
                
            except Exception as e:
                logger.error(
                    "❌ Ошибка выполнения инструмента '%s': %s",
                    step.tool_name,
                    _redact_workflow_log_value(str(e)),
                )
                raise
        
        # Выполняем в thread pool текущего event loop
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _execute_tool_sync)

        return result

    def _substitute_variables_in_params(self, params: Dict[str, Any], variables: Dict[str, Any]) -> Dict[str, Any]:
        """Рекурсивная подстановка переменных в параметрах"""
        result = {}
        for key, value in params.items():
            if isinstance(value, str):
                try:
                    substituted = self._substitute_variables_in_string(value, variables)
                    result[key] = substituted
                except Exception as e:
                    logger.warning(f"⚠️ Ошибка подстановки переменных в параметре '{key}': {e}")
                    result[key] = value
            elif isinstance(value, dict):
                result[key] = self._substitute_variables_in_params(value, variables)
            elif isinstance(value, list):
                result[key] = [
                    self._substitute_variables_in_string(item, variables) if isinstance(item, str) else item 
                    for item in value
                ]
            else:
                result[key] = value
        return result
    
    def _substitute_variables_in_string(self, template: str, variables: Dict[str, Any]) -> Any:
        """Подстановка переменных в строке с поддержкой имен переменных содержащих точки"""
        import re
        
        logger.debug(f"🔧 _substitute_variables_in_string: template='{template}', variables={list(variables.keys())}")
        
        # Проверяем является ли весь шаблон одной переменной без дополнительного текста
        full_var_match = re.match(r'^\{([^}]+)\}$', template)
        if full_var_match:
            var_name = full_var_match.group(1)
            value, found = self._resolve_variable_reference(var_name, variables)
            if found:
                logger.debug(
                    "🔧 Полная подстановка переменной: '%s' -> %s(%s)",
                    template,
                    type(value).__name__,
                    _redact_workflow_log_value(value),
                )
                return value  # Возвращаем объект как есть, не преобразуя в строку
            else:
                logger.warning(f"⚠️ Переменная '{var_name}' не найдена в контексте")
                return template
        
        # Если это не полная подстановка переменной, обрабатываем как строку с подстановками
        
        # Сначала пробуем стандартную подстановку для совместимости
        # Но только если шаблон не содержит переменных с точками
        has_dotted_vars = bool(re.search(r'\{[^}]*\.[^}]*\}', template))
        
        if not has_dotted_vars:
            try:
                # Создаем версию variables только с простыми именами (без точек)
                simple_vars = {k: v for k, v in variables.items() if '.' not in k}
                logger.debug(f"🔧 Пробуем стандартную подстановку с simple_vars: {list(simple_vars.keys())}")
                result = template.format(**simple_vars)
                logger.debug(f"🔧 Стандартная подстановка успешна: '{template}' -> '{result}'")
                return result
            except (KeyError, ValueError) as e:
                logger.debug(f"🔧 Стандартная подстановка не сработала: {e}")
                pass
        else:
            logger.debug(f"🔧 Обнаружены переменные с точками, пропускаем стандартную подстановку")
        
        # Если стандартная подстановка не сработала, используем кастомную
        result = template
        
        # Находим все шаблоны вида {variable_name}
        pattern = r'\{([^}]+)\}'
        matches = re.findall(pattern, template)
        logger.debug(f"🔧 Найдены переменные в шаблоне: {matches}")
        
        for var_name in matches:
            placeholder = f"{{{var_name}}}"
            value, found = self._resolve_variable_reference(var_name, variables)
            if found:
                # Прямая замена для строковых шаблонов
                logger.debug(
                    "🔧 Заменяем %s -> %s",
                    placeholder,
                    _redact_workflow_log_value(value),
                )
                result = result.replace(placeholder, str(value))
            else:
                logger.warning(f"⚠️ Переменная '{var_name}' не найдена в контексте")
        
        logger.debug(
            "🔧 Итоговый результат: '%s' -> '%s'",
            template,
            _redact_workflow_log_value(result),
        )
        return result

    def _resolve_variable_reference(self, var_name: str, variables: Dict[str, Any]) -> tuple[Any, bool]:
        """Resolve direct variables and dotted references like step_id.field."""
        if var_name in variables:
            return variables[var_name], True

        parts = var_name.split(".")
        if len(parts) < 2:
            return None, False

        current = variables.get(parts[0])
        if current is None and len(parts) > 1:
            current = variables.get(f"{parts[0]}.{parts[1]}")
            parts = parts[2:] if current is not None else parts
        else:
            parts = parts[1:]

        if current is None:
            return None, False

        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return None, False
        return current, True
    
    def _check_step_dependencies(self, step: WorkflowStep, 
                               step_results: Dict[str, StepResult]) -> bool:
        """Проверка выполнения зависимостей шага"""
        for dep_step_id in step.depends_on:
            if (dep_step_id not in step_results or 
                step_results[dep_step_id].status not in [StepStatus.COMPLETED, StepStatus.SKIPPED]):
                return False
        return True
    
    def _evaluate_condition(self, condition: str, context: WorkflowContext) -> bool:
        """Простая оценка условий выполнения шага.

        Поддерживаемые форматы:
          - `{step.field}` — boolean-проверка значения переменной.
          - `{step.field} == "value"` / `{step.field} != "value"` — сравнение
            подставленного значения со строковым/числовым литералом. Используется
            для condition-based skip (db_audit при verifier=Rejected) и для
            output_retry_policy.
        """

        stripped = condition.strip() if isinstance(condition, str) else condition

        # Поддержка операторов сравнения: '{path} == "value"' / '{path} != "value"'
        if isinstance(stripped, str):
            comp_match = re.match(
                r'^\{([^{}]+)\}\s*(==|!=)\s*(.+?)\s*$',
                stripped,
            )
            if comp_match:
                var_path = comp_match.group(1)
                operator = comp_match.group(2)
                literal_raw = comp_match.group(3).strip()

                # Парсим литерал: строка в кавычках или число/булевая
                if (len(literal_raw) >= 2 and literal_raw[0] == literal_raw[-1]
                        and literal_raw[0] in ('"', "'")):
                    literal_value: Any = literal_raw[1:-1]
                elif literal_raw.lower() == 'true':
                    literal_value = True
                elif literal_raw.lower() == 'false':
                    literal_value = False
                elif literal_raw.lower() in ('null', 'none'):
                    literal_value = None
                else:
                    try:
                        literal_value = int(literal_raw)
                    except ValueError:
                        try:
                            literal_value = float(literal_raw)
                        except ValueError:
                            literal_value = literal_raw

                all_variables: Dict[str, Any] = {}
                if getattr(context, "variables", None):
                    all_variables.update(context.variables)
                if getattr(context, "step_outputs", None):
                    all_variables.update(context.step_outputs)
                resolved, found = self._resolve_variable_reference(var_path, all_variables)
                if not found:
                    logger.warning(
                        f"⚠️ Условие {condition}: переменная {var_path} не найдена → False"
                    )
                    return False
                logger.debug(
                    f"🔍 Сравнение {condition}: {resolved!r} {operator} {literal_value!r}"
                )
                if operator == '==':
                    return resolved == literal_value
                return resolved != literal_value

        # Обрабатываем условия вида {step_name.field}
        if condition.startswith('{') and condition.endswith('}'):
            condition_expr = condition[1:-1]  # Убираем {}
            all_variables: Dict[str, Any] = {}
            if getattr(context, "variables", None):
                all_variables.update(context.variables)
            if getattr(context, "step_outputs", None):
                all_variables.update(context.step_outputs)

            value, found = self._resolve_variable_reference(condition_expr, all_variables)
            if found:
                logger.debug(f"🔍 Проверяем условие {condition}: {condition_expr} = {value}")
                return bool(value)
            
            # Разбираем выражение step_name.field
            if '.' in condition_expr:
                step_name, field = condition_expr.split('.', 1)
                
                # Получаем результат шага из контекста
                if hasattr(context, 'step_outputs') and context.step_outputs:
                    # Сначала пытаемся найти прямой ключ step_name.field
                    direct_key = f"{step_name}.{field}"
                    if direct_key in context.step_outputs:
                        value = context.step_outputs[direct_key]
                        logger.debug(f"🔍 Проверяем условие {condition}: {direct_key} = {value}")
                        return bool(value)  # Преобразуем в bool
                    
                    # Если не нашли, пытаемся найти в объекте шага
                    step_output = context.step_outputs.get(step_name)
                    if step_output and isinstance(step_output, dict):
                        value = step_output.get(field)
                        logger.debug(f"🔍 Проверяем условие {condition}: {step_name}.{field} = {value}")
                        return bool(value)  # Преобразуем в bool
                    else:
                        logger.warning(f"⚠️ Результат шага {step_name} не найден в контексте для условия {condition}")
                        return False
                else:
                    logger.warning(f"⚠️ step_outputs отсутствует в контексте для условия {condition}")
                    return False
            else:
                logger.warning(f"⚠️ Неподдерживаемый формат условия: {condition}")
                return False
        
        # Специальные условия
        if "no_response_after_48h" in condition:
            return False  # Заглушка
            
        # По умолчанию - если условие не распознано, считаем его False
        logger.warning(f"⚠️ Неизвестное условие: {condition}, принимаем False")
        return False
    
    async def _execute_rollback(self, rollback_action: str, 
                              context: WorkflowContext, step_result: StepResult):
        """Выполнение rollback действия"""
        logger.info(f"🔄 Выполняем rollback: {rollback_action}")
        
        try:
            if rollback_action == "log_failure":
                logger.error(f"Rollback: Logged failure for step {step_result.step_id}")
            elif rollback_action == "mark_as_failed":
                context.metadata["rollback_executed"] = rollback_action
            # Можно добавить другие rollback действия
            
        except Exception as e:
            logger.error(f"❌ Ошибка при rollback: {e}")
    

    
    # Устаревший метод удален - теперь используется только YAML формат
    # Для загрузки workflow используйте:
    # - WorkflowDefinition.from_yaml() для прямой загрузки
    # - execute_workflow_from_yaml() для выполнения из файла
    
    # ===========================================
    # МЕТОДЫ ДЛЯ РАБОТЫ С YAML ФАЙЛАМИ
    # ===========================================
    
    async def execute_workflow_from_yaml(self, yaml_path: Union[str, Path],
                                       context: Optional[WorkflowContext] = None,
                                       client_id: Optional[str] = None,
                                       **variables) -> WorkflowResult:
        """
        НОВЫЙ метод: Выполнение workflow из YAML файла
        
        Args:
            yaml_path: Путь к YAML файлу с определением workflow
            context: Контекст выполнения (опционально)
            client_id: ID клиента для квотирования (опционально)
            **variables: Переменные для подстановки в workflow
            
        Returns:
            WorkflowResult с результатами выполнения
        """
        try:
            # Загружаем workflow definition из YAML
            workflow_def = WorkflowDefinition.from_yaml(yaml_path)
            logger.info(f"📄 Загружен workflow '{workflow_def.name}' из {yaml_path}")
            
            # Создаем контекст если не передан
            if context is None:
                # Начинаем с inputs из workflow definition как базовых переменных
                initial_variables = workflow_def.inputs.copy()
                # Дополняем/перезаписываем переданными переменными
                initial_variables.update(variables)
                
                context = WorkflowContext(
                    workflow_id=f"{workflow_def.name}_{str(uuid.uuid4())[:8]}",
                    session_id=str(uuid.uuid4()),
                    client_id=client_id,
                    variables=initial_variables
                )
            else:
                # Сначала применяем inputs из workflow definition (если они есть)
                if workflow_def.inputs:
                    # inputs имеют приоритет перед существующими переменными context
                    merged_variables = workflow_def.inputs.copy()
                    merged_variables.update(context.variables)
                    context.variables = merged_variables
                # Затем дополняем переданными переменными (они имеют наивысший приоритет)
                context.variables.update(variables)
            
            # Выполняем workflow
            return await self.execute_workflow(workflow_def, context, client_id)
            
        except Exception as e:
            logger.error(f"❌ Ошибка выполнения workflow из {yaml_path}: {e}")
            raise WorkflowExecutionError(f"Не удалось выполнить workflow из {yaml_path}: {e}") from e
    
    async def load_and_validate_yaml(self, yaml_path: Union[str, Path]) -> WorkflowDefinition:
        """
        НОВЫЙ метод: Загрузка и валидация YAML workflow
        
        Args:
            yaml_path: Путь к YAML файлу
            
        Returns:
            WorkflowDefinition object
            
        Raises:
            WorkflowExecutionError: При ошибках загрузки или валидации
        """
        try:
            workflow_def = WorkflowDefinition.from_yaml(yaml_path)
            
            # Базовая валидация
            if not workflow_def.name:
                raise WorkflowExecutionError("Workflow должен иметь имя")
            
            if not workflow_def.steps:
                raise WorkflowExecutionError("Workflow должен содержать хотя бы один шаг")
            
            # Проверяем корректность зависимостей
            step_ids = {step.id for step in workflow_def.steps}
            for step in workflow_def.steps:
                for dep in step.depends_on:
                    if dep not in step_ids:
                        raise WorkflowExecutionError(
                            f"Шаг '{step.id}' зависит от несуществующего шага '{dep}'"
                        )
            
            logger.info(f"ℹ️ Workflow содержит агентов: {[step.agent_type for step in workflow_def.steps]}")
            
            logger.info(f"✅ Workflow '{workflow_def.name}' успешно загружен и валидирован")
            return workflow_def
            
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки YAML: {e}")
            raise WorkflowExecutionError(f"Ошибка загрузки workflow из {yaml_path}: {e}")
    
    def list_available_pipelines(self, pipelines_dir: Union[str, Path] = "workflow_pipelines") -> List[Dict[str, Any]]:
        """
        НОВЫЙ метод: Получение списка доступных YAML пайплайнов
        
        Args:
            pipelines_dir: Директория с пайплайнами
            
        Returns:
            Список словарей с информацией о пайплайнах
        """
        pipelines = []
        pipelines_path = Path(pipelines_dir)
        
        if not pipelines_path.exists():
            logger.warning(f"⚠️ Директория пайплайнов не найдена: {pipelines_dir}")
            return pipelines
        
        for yaml_file in pipelines_path.glob("*.yaml"):
            try:
                workflow_def = WorkflowDefinition.from_yaml(yaml_file)
                pipeline_info = {
                    "file": str(yaml_file),
                    "name": workflow_def.name,
                    "version": workflow_def.version,
                    "description": workflow_def.description,
                    "steps_count": len(workflow_def.steps),
                    "estimated_duration": workflow_def.metadata.get("estimated_duration", "неизвестно"),
                    "complexity": workflow_def.metadata.get("complexity", "неизвестно"),
                    "category": workflow_def.metadata.get("category", "general"),
                    "agents_used": list(set(step.agent_type for step in workflow_def.steps))
                }
                pipelines.append(pipeline_info)
                
            except Exception as e:
                logger.warning(f"⚠️ Не удалось загрузить {yaml_file}: {e}")
        
        # Сортируем по категориям, затем по имени
        pipelines.sort(key=lambda x: (x["category"], x["name"]))
        
        logger.info(f"📋 Найдено {len(pipelines)} пайплайнов в {pipelines_dir}")
        return pipelines
    
    async def execute_pipeline_by_name(self, pipeline_name: str,
                                     context: Optional[WorkflowContext] = None,
                                     pipelines_dir: Union[str, Path] = "workflow_pipelines",
                                     **variables) -> WorkflowResult:
        """
        НОВЫЙ метод: Выполнение пайплайна по имени
        
        Args:
            pipeline_name: Имя пайплайна (без .yaml расширения)
            context: Контекст выполнения
            pipelines_dir: Директория с пайплайнами
            **variables: Переменные для workflow
            
        Returns:
            WorkflowResult с результатами выполнения
        """
        yaml_path = Path(pipelines_dir) / f"{pipeline_name}.yaml"
        
        if not yaml_path.exists():
            raise WorkflowExecutionError(f"Пайплайн '{pipeline_name}' не найден в {pipelines_dir}")
        
        return await self.execute_workflow_from_yaml(yaml_path, context, **variables)
    
    def get_pipeline_info(self, pipeline_name: str,
                         pipelines_dir: Union[str, Path] = "workflow_pipelines") -> Dict[str, Any]:
        """
        НОВЫЙ метод: Получение информации о конкретном пайплайне
        
        Args:
            pipeline_name: Имя пайплайна
            pipelines_dir: Директория с пайплайнами
            
        Returns:
            Словарь с подробной информацией о пайплайне
        """
        yaml_path = Path(pipelines_dir) / f"{pipeline_name}.yaml"
        
        if not yaml_path.exists():
            raise WorkflowExecutionError(f"Пайплайн '{pipeline_name}' не найден в {pipelines_dir}")
        
        try:
            workflow_def = WorkflowDefinition.from_yaml(yaml_path)
            
            # Анализируем зависимости шагов
            dependency_graph = {}
            for step in workflow_def.steps:
                dependency_graph[step.id] = step.depends_on
            
            # Вычисляем приблизительное время выполнения
            total_timeout = sum(step.timeout or 60 for step in workflow_def.steps)
            
            # Собираем информацию о ресурсах
            resource_requirements = {
                "max_memory_mb": 0,
                "max_api_calls": 0,
                "concurrent_steps": 1
            }
            
            for step in workflow_def.steps:
                if step.resource_limits:
                    if step.resource_limits.max_memory_mb:
                        resource_requirements["max_memory_mb"] = max(
                            resource_requirements["max_memory_mb"],
                            step.resource_limits.max_memory_mb
                        )
                    if step.resource_limits.max_api_calls_per_minute:
                        resource_requirements["max_api_calls"] += step.resource_limits.max_api_calls_per_minute
                    resource_requirements["concurrent_steps"] = max(
                        resource_requirements["concurrent_steps"],
                        step.resource_limits.max_concurrent_steps
                    )
            
            return {
                "name": workflow_def.name,
                "version": workflow_def.version,
                "description": workflow_def.description,
                "file_path": str(yaml_path),
                "steps": [
                    {
                        "id": step.id,
                        "agent_type": step.agent_type,
                        "task": step.task,
                        "depends_on": step.depends_on,
                        "timeout": step.timeout,
                        "has_condition": bool(step.condition),
                        "has_retry_policy": bool(step.retry_policy),
                        "has_resource_limits": bool(step.resource_limits)
                    }
                    for step in workflow_def.steps
                ],
                "dependency_graph": dependency_graph,
                "estimated_duration_seconds": total_timeout,
                "resource_requirements": resource_requirements,
                "global_settings": {
                    "has_global_retry": bool(workflow_def.global_retry_policy),
                    "has_global_limits": bool(workflow_def.global_resource_limits),
                    "notifications": len(workflow_def.notifications),
                    "error_handling": workflow_def.error_handling
                },
                "metadata": workflow_def.metadata,
                "agents_used": list(set(step.agent_type for step in workflow_def.steps)),
                "total_steps": len(workflow_def.steps)
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка анализа пайплайна {pipeline_name}: {e}")
            raise WorkflowExecutionError(f"Не удалось проанализировать пайплайн {pipeline_name}: {e}")
    
    async def _execute_manager_with_preloaded_agents(self, step: WorkflowStep, 
                                                   context: WorkflowContext, 
                                                   formatted_task: str) -> Any:
        """Выполнение менеджера с предзагрузкой указанных агентов"""
        
        preload_agents = step.metadata.get('preload_agents', [])
        pipeline_type = step.metadata.get('pipeline_type', 'workflow')
        
        logger.info(f"👥 Предзагрузка агентов для менеджера '{step.id}': {preload_agents}")
        
        # Валидация списка агентов
        if not isinstance(preload_agents, list):
            raise WorkflowExecutionError(f"preload_agents должен быть списком, получен: {type(preload_agents)}")
        
        # Удаляем дубликаты и ограничиваем количество
        unique_agents = list(dict.fromkeys(preload_agents))  # Сохраняем порядок
        if len(unique_agents) > 10:
            raise WorkflowExecutionError(f"Слишком много агентов для предзагрузки: {len(unique_agents)} (максимум 10)")
        
        # Запрещаем менеджера в списке предзагрузки
        if 'manager' in unique_agents:
            raise WorkflowExecutionError("Менеджер не может быть в списке preload_agents (риск рекурсии)")
        
        # Проверяем глубину менеджеров (защита от рекурсии)
        manager_depth = context.metadata.get('manager_depth', 0)
        if manager_depth >= 1:
            raise WorkflowExecutionError(f"Превышена максимальная глубина менеджеров: {manager_depth} (максимум 1)")
        
        # Сохраняем текущее состояние фабрики
        original_agents = self.factory.agents.copy()
        original_manager = self.factory.manager_agent
        
        try:
            # Увеличиваем глубину менеджеров
            context.metadata['manager_depth'] = manager_depth + 1
            
            # Предзагружаем агентов в том же порядке
            preloaded_count = 0
            for agent_profile in unique_agents:
                try:
                    logger.info(f"🔧 Создаем агента '{agent_profile}' для команды менеджера")
                    
                    agent = self.factory.create_agent(
                        profile_type=agent_profile,
                        session_id=context.session_id,
                        task=formatted_task,
                        pipeline_type=pipeline_type
                    )
                    
                    if agent:
                        preloaded_count += 1
                        logger.info(f"✅ Агент '{agent_profile}' успешно создан")
                    else:
                        logger.error(f"❌ Не удалось создать агента '{agent_profile}'")
                        raise WorkflowExecutionError(f"Не удалось создать агента '{agent_profile}'")
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка создания агента '{agent_profile}': {e}")
                    raise WorkflowExecutionError(f"Ошибка создания агента '{agent_profile}': {e}")
            
            logger.info(f"👥 Предзагружено {preloaded_count} агентов для менеджера")
            
            # Создаем менеджера (он получит предзагруженных агентов как managed_agents)
            logger.info(f"👨‍💼 Создаем менеджера с командой из {preloaded_count} агентов")
            
            manager = self.factory.create_agent(
                profile_type='manager',
                session_id=context.session_id,
                task=formatted_task,
                pipeline_type=pipeline_type,
                preload_agents=unique_agents
            )
            
            if not manager:
                raise WorkflowExecutionError("Не удалось создать менеджера")
            
            logger.info(f"🤖 Запускаем менеджера '{step.id}' с командой")
            logger.info(
                "📋 Задача: %s...",
                _redact_workflow_log_value(formatted_task[:100]),
            )
            
            # Выполняем менеджера
            result = manager.run(formatted_task, stream=False)
            
            # Регистрируем API вызов
            self.resource_manager.record_api_call(context.workflow_id)
            
            logger.info(f"✅ Менеджер '{step.id}' завершил работу с командой")
            
            return result
            
        finally:
            # Очищаем временно созданных агентов и восстанавливаем состояние
            try:
                # Восстанавливаем исходное состояние фабрики
                self.factory.agents = original_agents
                self.factory.manager_agent = original_manager
                
                # Уменьшаем глубину менеджеров
                context.metadata['manager_depth'] = manager_depth
                
                logger.info(f"🧹 Очистка временных агентов для шага '{step.id}' завершена")
                
            except Exception as cleanup_error:
                logger.warning(f"⚠️ Ошибка при очистке временных агентов: {cleanup_error}")
    
    def _validate_preload_agents(self, preload_agents: List[str]) -> List[str]:
        """Валидация списка агентов для предзагрузки"""
        
        if not isinstance(preload_agents, list):
            raise WorkflowExecutionError(f"preload_agents должен быть списком, получен: {type(preload_agents)}")
        
        # Удаляем дубликаты, сохраняя порядок
        unique_agents = list(dict.fromkeys(preload_agents))
        
        # Проверяем лимиты
        if len(unique_agents) > 10:
            raise WorkflowExecutionError(f"Слишком много агентов для предзагрузки: {len(unique_agents)} (максимум 10)")
        
        # Запрещаем менеджера
        if 'manager' in unique_agents:
            raise WorkflowExecutionError("Менеджер не может быть в списке preload_agents (риск рекурсии)")
        
        # Проверяем существование профилей агентов
        from agent_command import AGENT_PROFILES
        invalid_agents = [agent for agent in unique_agents if agent not in AGENT_PROFILES]
        if invalid_agents:
            raise WorkflowExecutionError(f"Неизвестные профили агентов: {invalid_agents}")
        
        return unique_agents
