"""
Enhanced Workflow Engine с интеллектуальным управлением качеством
"""
import logging
import hashlib
from typing import Dict, List, Any, Optional, Union
from pathlib import Path
from datetime import datetime
import yaml

from .engine import WorkflowEngine
from .models import (
    WorkflowDefinition, WorkflowResult, WorkflowContext, WorkflowStatus,
    StepResult, StepStatus, WorkflowStep, StepPlan, ValidationResult, Decision,
    ResourceLimits, WorkflowExecutionError
)

# Enhanced components
from .policy.engine import PolicyEngine
from .contracts.registry import ContractRegistry
from .intelligence.planner import PreStepPlanner
from .intelligence.judge import PostStepJudge
from .intelligence.decision import DecisionEngine
from .intelligence.aggregator import FinalAggregator

# Resilience components
from .resilience.circuit_breaker import CircuitBreakerManager
from .resilience.retry import AdaptiveRetryEngine
from .resilience.budget import BudgetManager, BudgetType
from .resilience.loop_detection import LoopDetector

# Orchestration components
from .orchestration.conditions import ConditionalEngine
from .orchestration.alternatives import AlternativeExecutor, ExecutionStrategy
from .orchestration.cache import CacheManager
from .orchestration.predictor import QualityPredictor, PerformanceOptimizer

# Monitoring components
from .monitoring.metrics import MetricsCollector
from .monitoring.alerts import AlertManager, log_notification_handler, console_notification_handler
from .monitoring.analytics import AnalyticsEngine
from .monitoring.dashboard import DashboardGenerator, ReportBuilder

logger = logging.getLogger(__name__)


class FeatureManager:
    """Менеджер включения/отключения фич"""
    
    def __init__(self, config_path: Optional[Path] = None):
        self.global_config = {
            "enhanced_workflow": {
                "enabled": True,
                "rollout_percentage": 100,
                "fallback_to_legacy": True
            }
        }
        
        self.feature_flags = {
            "features": {
                "pre_step_planner": {"enabled": True, "rollout": 100},
                "post_step_judge": {"enabled": True, "rollout": 100},
                "semantic_validation": {"enabled": False, "rollout": 0},
                "multi_agent_consensus": {"enabled": False, "rollout": 0},
                "human_in_the_loop": {"enabled": True, "rollout": 100},
                "circuit_breaker": {"enabled": True, "rollout": 100}
            }
        }
        self.workflow_overrides: Dict[str, Dict[str, Any]] = {}
        self._load_config(config_path or Path(__file__).resolve().parent / "config" / "enhanced_global.yaml")

    def _load_config(self, config_path: Path) -> None:
        if not config_path.exists():
            return
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("⚠️ Не удалось загрузить enhanced config %s: %s", config_path, exc)
            return
        if isinstance(data.get("enhanced_workflow"), dict):
            self.global_config["enhanced_workflow"].update(data["enhanced_workflow"])
        if isinstance(data.get("features"), dict):
            for name, config in data["features"].items():
                if isinstance(config, dict):
                    self.feature_flags.setdefault("features", {}).setdefault(name, {}).update(config)
        if isinstance(data.get("workflow_overrides"), dict):
            self.workflow_overrides = {
                str(name): config
                for name, config in data["workflow_overrides"].items()
                if isinstance(config, dict)
            }
    
    def is_enhanced_enabled(self, workflow_id: str = None) -> bool:
        """Проверить включен ли enhanced layer"""
        if not self.global_config.get("enhanced_workflow", {}).get("enabled", False):
            return False
        
        rollout = self.global_config.get("enhanced_workflow", {}).get("rollout_percentage", 0)
        if workflow_id:
            return self._check_rollout(workflow_id, rollout)
        return rollout == 100
    
    def is_feature_enabled(self, feature: str, workflow_id: str = None, 
                          step_id: str = None) -> bool:
        """Проверить включена ли конкретная фича"""
        if not self.is_enhanced_enabled(workflow_id):
            return False
        
        feature_config = self.feature_flags.get("features", {}).get(feature, {})
        if not feature_config.get("enabled", False):
            return False
        
        rollout = feature_config.get("rollout", 0)
        check_id = f"{workflow_id}:{step_id}" if step_id else workflow_id
        return self._check_rollout(check_id or "", rollout)
    
    def _check_rollout(self, identifier: str, rollout_percentage: int) -> bool:
        """Проверить попадает ли идентификатор в rollout"""
        if rollout_percentage >= 100:
            return True
        if rollout_percentage <= 0:
            return False
        
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        hash_val = int(digest[:8], 16) % 100
        return hash_val < rollout_percentage


class EnhancedWorkflowEngine(WorkflowEngine):
    """Enhanced Workflow Engine с интеллектуальными возможностями"""
    
    def __init__(self):
        # Инициализируем родительский класс
        super().__init__()
        
        # Добавляем enhanced компоненты
        self.feature_manager = FeatureManager()
        self.policy_engine = PolicyEngine()
        self.contract_registry = ContractRegistry()
        self.planner = PreStepPlanner()
        self.judge = PostStepJudge()
        self.decision_engine = DecisionEngine()
        # aggregator наследуется от базового класса
        
        # Добавляем resilience компоненты
        self.circuit_breaker_manager = CircuitBreakerManager()
        self.retry_engine = AdaptiveRetryEngine()
        self.budget_manager = BudgetManager()
        self.loop_detector = LoopDetector()
        
        # Добавляем orchestration компоненты
        self.conditional_engine = ConditionalEngine()
        self.alternative_executor = AlternativeExecutor()
        self.cache_manager = CacheManager()
        self.quality_predictor = QualityPredictor()
        self.performance_optimizer = PerformanceOptimizer()
        
        # Добавляем monitoring компоненты
        self.metrics_collector = MetricsCollector()
        self.alert_manager = AlertManager()
        self.analytics_engine = AnalyticsEngine()
        self.dashboard_generator = DashboardGenerator()
        self.report_builder = ReportBuilder()
        
        # Настраиваем notification handlers для алертов
        self.alert_manager.add_notification_handler(log_notification_handler)
        self.alert_manager.add_notification_handler(console_notification_handler)
        
        logger.info("🚀 Enhanced Workflow Engine initialized")

    def _is_text_to_sql_workflow(self, workflow_definition: WorkflowDefinition) -> bool:
        return (
            workflow_definition.name == "text_to_sql_pipeline"
            or workflow_definition.metadata.get("category") == "text_to_sql"
        )

    def _coerce_bool(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    def _should_fallback_to_legacy(
        self,
        workflow_definition: WorkflowDefinition,
        context: Optional[WorkflowContext],
    ) -> bool:
        if self._is_text_to_sql_workflow(workflow_definition):
            variables = context.variables if context and isinstance(context.variables, dict) else {}
            override = self.feature_manager.workflow_overrides.get("text_to_sql", {})
            default_enabled = self._coerce_bool(override.get("fallback_to_legacy"), False)
            # EPIC 7.22: allow_enhanced_fallback приходит из AG-UI payload и должен
            # валидироваться строго (fail-fast). Service.py уже валидирует через
            # _coerce_strict_bool, но workflow-engine — публичная точка для legacy/direct
            # caller-ов: не доверяем входу и переиспользуем общий strict-coercer.
            from custom_tools.text_to_sql.utils import coerce_strict_bool
            return coerce_strict_bool(
                variables.get("allow_enhanced_fallback"),
                default=default_enabled,
                field_name="allow_enhanced_fallback",
            )
        return self._coerce_bool(
            self.feature_manager.global_config.get("enhanced_workflow", {}).get("fallback_to_legacy"),
            True,
        )
    
    async def execute_workflow(self, workflow_definition: WorkflowDefinition,
                              context: Optional[WorkflowContext] = None,
                              client_id: Optional[str] = None) -> WorkflowResult:
        """Enhanced выполнение workflow"""
        
        # Проверяем включен ли enhanced layer
        if not self.feature_manager.is_enhanced_enabled(context.workflow_id if context else None):
            if workflow_definition.requires_enhanced_engine:
                raise WorkflowExecutionError(
                    f"Workflow '{workflow_definition.name}' requires enhanced engine "
                    "(pipeline.requires_enhanced_engine=true), but enhanced layer is disabled"
                )
            logger.info("📋 Enhanced layer disabled, falling back to legacy execution")
            return await super().execute_workflow(workflow_definition, context, client_id)
        
        try:
            logger.info(f"🧠 Starting enhanced execution of '{workflow_definition.name}'")
            return await self._execute_enhanced_workflow(workflow_definition, context, client_id)
            
        except Exception as e:
            logger.error(f"❌ Enhanced execution failed: {e}")
            
            # Fallback к legacy если включен
            if self._should_fallback_to_legacy(workflow_definition, context):
                logger.info("🔄 Falling back to legacy execution")
                return await super().execute_workflow(workflow_definition, context, client_id)
            else:
                raise
    
    async def _execute_enhanced_workflow(self, workflow_def: WorkflowDefinition,
                                        context: Optional[WorkflowContext] = None,
                                        client_id: Optional[str] = None) -> WorkflowResult:
        """Enhanced выполнение с интеллектуальным управлением"""
        
        # Создаем контекст если не передан
        if context is None:
            context = WorkflowContext(
                workflow_id=f"{workflow_def.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                session_id=f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                client_id=client_id
            )
        
        workflow_id = context.workflow_id
        start_time = datetime.now()
        
        # Записываем начало workflow в метрики
        self.metrics_collector.record_workflow_start(workflow_id, workflow_def.name)
        
        try:
            # Создаем бюджеты для workflow
            workflow_budget = self.budget_manager.create_workflow_budget(workflow_id)
            logger.info(f"💰 Created budget for workflow '{workflow_id}'")
            
            # Сохраняем начальное состояние (используем базовый helper)
            resource_lease = await self._on_workflow_started(workflow_def, context, client_id, start_time)
            
            # Выполняем шаги с enhanced логикой
            step_results = await self._execute_enhanced_steps(workflow_def, context)

            if await self._is_workflow_cancelled(workflow_id):
                return await self._build_cancelled_workflow_result(
                    workflow_def,
                    context,
                    step_results,
                    start_time,
                )
            
            # Агрегируем финальный результат
            final_output = await self.aggregator.aggregate_final_result(
                step_results, workflow_def, context
            )
            
            # Завершаем workflow
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            # Подсчитываем статистику
            completed_steps = len([r for r in step_results.values() if r.status == StepStatus.COMPLETED])
            failed_steps = len([r for r in step_results.values() if r.status == StepStatus.FAILED])
            stop_on_failure = workflow_def.error_handling.get("on_failure", "continue") != "continue"
            is_text_to_sql = (
                workflow_def.name == "text_to_sql_pipeline"
                or workflow_def.metadata.get("category") == "text_to_sql"
            )
            workflow_status = WorkflowStatus.FAILED if failed_steps and (stop_on_failure or is_text_to_sql) else WorkflowStatus.COMPLETED

            if workflow_status == WorkflowStatus.COMPLETED:
                await self._on_workflow_completed(workflow_id, final_output)
            else:
                await self.state_manager.save_checkpoint(
                    workflow_id=workflow_id,
                    status=WorkflowStatus.FAILED,
                    context=context,
                    step_results=step_results,
                    current_step=context.current_step,
                    metadata={
                        "workflow_name": workflow_def.name,
                        "error": f"Workflow failed steps: {failed_steps}",
                    },
                )
            
            result = WorkflowResult(
                workflow_id=workflow_id,
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
            
            logger.info(f"✅ Enhanced workflow {workflow_id} finished with status {workflow_status.value} in {duration:.1f}s")
            return result
            
        except Exception as e:
            logger.error(f"❌ Enhanced workflow {workflow_id} failed: {e}")
            if 'resource_lease' in locals():
                await self._on_workflow_failed(workflow_def, context, resource_lease, e)
            raise
        finally:
            if 'resource_lease' in locals():
                await self._release_workflow_resources(workflow_id)
    
    async def _execute_enhanced_steps(self, workflow_def: WorkflowDefinition,
                                     context: WorkflowContext) -> Dict[str, StepResult]:
        """Выполнение шагов с enhanced логикой"""
        
        if workflow_def.parallel_execution:
            return await self._execute_enhanced_steps_parallel(workflow_def, context)
        else:
            return await self._execute_enhanced_steps_sequential(workflow_def, context)
    
    async def _execute_enhanced_steps_sequential(self, workflow_def: WorkflowDefinition,
                                               context: WorkflowContext) -> Dict[str, StepResult]:
        """Последовательное выполнение с enhanced логикой"""
        step_results = {}
        
        for step in workflow_def.steps:
            logger.info(f"🔄 Processing step '{step.id}' with enhanced logic")

            if await self._is_workflow_cancelled(context.workflow_id):
                logger.info(
                    "🚫 Enhanced workflow %s отменён до запуска шага %s",
                    context.workflow_id,
                    step.id,
                )
                break
            
            # Проверяем зависимости
            if not self._check_step_dependencies(step, step_results):
                logger.info(f"⏸️ Skipping step {step.id} - dependencies not met")
                step_results[step.id] = StepResult(
                    step_id=step.id,
                    status=StepStatus.SKIPPED,
                    start_time=datetime.now(),
                    end_time=datetime.now()
                )
                continue
            
            # Проверяем условие выполнения (используем helper из базового класса)
            if self._should_skip_step_by_condition(step, context):
                step_results[step.id] = StepResult(
                    step_id=step.id,
                    status=StepStatus.SKIPPED,
                    output=context.step_outputs.get(step.id),
                    start_time=datetime.now(),
                    end_time=datetime.now()
                )
                continue
            
            # Выполняем шаг с enhanced обработкой
            step_result = await self._execute_enhanced_step(step, context, step_results)
            step_results[step.id] = step_result

            if getattr(step_result.status, "value", step_result.status) == StepStatus.COMPLETED.value:
                step_result = await self._complete_enhanced_step_with_output_retry(
                    step, step_result, context, workflow_def, step_results
                )
                step_results[step.id] = step_result
                if await self._is_workflow_cancelled(context.workflow_id):
                    logger.info(
                        "🚫 Enhanced workflow %s отменён после шага %s",
                        context.workflow_id,
                        step.id,
                    )
                    break

            if getattr(step_result.status, "value", step_result.status) != StepStatus.COMPLETED.value:
                # Шаг провален - выполняем rollback если определен (используем метод из базового класса)
                logger.error(f"❌ Enhanced: Шаг {step.id} провален: {step_result.error}")
                
                if step.rollback_action:
                    await self._execute_rollback(step.rollback_action, context, step_result)
                
                if workflow_def.error_handling.get("on_failure", "continue") != "continue":
                    logger.error("⛔ Enhanced: прерываем workflow из-за on_failure=%s", workflow_def.error_handling.get("on_failure"))
                    break
        
        return step_results
    
    async def _complete_enhanced_step_with_output_retry(
        self,
        step: WorkflowStep,
        step_result: StepResult,
        context: WorkflowContext,
        workflow_def: WorkflowDefinition,
        previous_results: Dict[str, StepResult],
    ) -> StepResult:
        """Сохраняет успешный enhanced-шаг и применяет output_retry_policy.

        Сам retry выполняет enhanced step executor, а не базовый
        ``_execute_workflow_step``: пайплайны с ``requires_enhanced_engine`` не
        должны деградировать на base runtime.
        """
        async def retry_executor(
            retry_step: WorkflowStep,
            retry_context: WorkflowContext,
            retry_workflow_def: WorkflowDefinition,
        ) -> StepResult:
            retry_result = await self._execute_enhanced_step(
                retry_step, retry_context, previous_results
            )
            if getattr(retry_result.status, "value", retry_result.status) == StepStatus.COMPLETED.value:
                previous_results[retry_step.id] = retry_result
                completed_retry_result = await self._complete_enhanced_step_with_output_retry(
                    retry_step,
                    retry_result,
                    retry_context,
                    retry_workflow_def,
                    previous_results,
                )
                previous_results[retry_step.id] = completed_retry_result
                return completed_retry_result
            return retry_result

        retried = await self._maybe_run_output_retry(
            step,
            step_result,
            context,
            workflow_def,
            step_executor=retry_executor,
            step_results=previous_results,
            rerun_step_committed_by_executor=True,
        )
        if retried is None:
            await self._on_step_completed(
                context.workflow_id, step, step_result, context, previous_results
            )
            return step_result
        return retried

    async def _execute_enhanced_steps_parallel(self, workflow_def: WorkflowDefinition,
                                             context: WorkflowContext) -> Dict[str, StepResult]:
        """Параллельное выполнение с enhanced логикой"""
        from .orchestration.parallel_executor import ParallelWorkflowExecutor
        
        # Сохраняем workflow_definition как временный атрибут для параллельных задач
        context._workflow_definition = workflow_def
        
        parallel_executor = ParallelWorkflowExecutor(
            max_concurrent=workflow_def.max_parallel_steps
        )
        
        logger.info(f"🚀 Enhanced: Начинаем параллельное выполнение с enhanced логикой")
        
        return await parallel_executor.execute_steps_parallel(
            workflow_def.steps,
            context,
            step_executor=self._execute_enhanced_step_wrapper,
            dependency_checker=self._check_step_dependencies,
            condition_checker=self._should_skip_step_by_condition,
            stop_checker=lambda: self._is_workflow_cancelled(context.workflow_id),
            stop_on_failure=(workflow_def.error_handling.get("on_failure", "continue") != "continue"),
        )
    
    async def _execute_enhanced_step_wrapper(self, step, context):
        """Enhanced обертка для выполнения шага с дополнительной логикой"""
        step_results = getattr(context, "_workflow_step_results", {})
        step_result = await self._execute_enhanced_step(step, context, step_results)
        
        # Обрабатываем события шага с enhanced логикой
        if getattr(step_result.status, "value", step_result.status) == StepStatus.COMPLETED.value:
            workflow_def = getattr(context, "_workflow_definition", None)
            if workflow_def is not None:
                step_results[step.id] = step_result
                step_result = await self._complete_enhanced_step_with_output_retry(
                    step, step_result, context, workflow_def, step_results
                )
                step_results[step.id] = step_result
            else:
                step_results[step.id] = step_result
                await self._on_step_completed(context.workflow_id, step, step_result, context, step_results)
        elif step_result.status == StepStatus.FAILED:
            logger.error(f"❌ Enhanced: Шаг {step.id} провален: {step_result.error}")
            if step.rollback_action:
                await self._execute_rollback(step.rollback_action, context, step_result)
            # В Enhanced режиме можем продолжить или прервать в зависимости от политики
            # TODO: Добавить политику обработки ошибок
        
        return step_result
    
    async def _execute_enhanced_step(self, step: WorkflowStep, context: WorkflowContext,
                                    previous_results: Dict[str, StepResult]) -> StepResult:
        """Выполнение одного шага с enhanced логикой включая resilience"""
        
        step_start_time = datetime.now()
        
        # Создаем бюджет для шага
        step_budget = self.budget_manager.create_step_budget(step.id)
        
        # Проверяем доступность агента через circuit breaker
        if not self.circuit_breaker_manager.is_agent_available(step.agent_type):
            logger.error(f"🚫 Agent '{step.agent_type}' unavailable due to circuit breaker")
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                start_time=step_start_time,
                end_time=datetime.now(),
                error=f"Agent {step.agent_type} circuit breaker is OPEN",
                error_class="circuit_breaker_open"
            )
        
        # Проверяем зацикливание
        is_loop, loop_pattern = self.loop_detector.is_step_in_loop(context.workflow_id, step.id)
        if is_loop:
            suggestion = self.loop_detector.get_loop_prevention_suggestion(context.workflow_id, step.id)
            logger.error(f"🔄 Loop detected for step '{step.id}': {suggestion}")
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                start_time=step_start_time,
                end_time=datetime.now(),
                error=f"Loop detected: {suggestion}",
                error_class="loop_detected"
            )
        
        # Выполняем с adaptive retry
        try:
            step_result = await self.retry_engine.execute_with_retry(
                step_id=step.id,
                step_func=self._execute_single_step_attempt,
                context={
                    "step": step,
                    "workflow_context": context,
                    "previous_results": previous_results,
                    "step_budget": step_budget
                },
                max_retries=3,
                base_delay=1.0,
                max_delay=30.0,
                backoff_multiplier=1.5
            )
            
            # Записываем выполнение в loop detector
            execution_data = {
                "task": step.task,
                "output": step_result.output,
                "error": step_result.error,
                "quality_score": getattr(step_result, 'quality_score', 0.0),
                "decision": getattr(step_result, 'decision', ''),
                "retry_count": getattr(step_result, 'retry_count', 0)
            }
            
            loop_detected = self.loop_detector.record_step_execution(
                context.workflow_id, step.id, execution_data
            )
            
            if loop_detected:
                logger.warning(f"🔄 New loop pattern detected for step '{step.id}'")
            
            return step_result
            
        except Exception as e:
            logger.error(f"❌ Enhanced step execution failed: {e}")
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                start_time=step_start_time,
                end_time=datetime.now(),
                error=str(e),
                error_class="execution_error"
            )
    
    async def _check_result_cache(self, step: WorkflowStep, context: WorkflowContext) -> Optional[StepResult]:
        """Проверить кэш результатов"""
        
        if not self.feature_manager.is_feature_enabled("result_cache", context.workflow_id, step.id):
            return None
        
        cached_result = self.cache_manager.result_cache.get_cached_result(
            step.agent_type, step.task, context.variables or {}
        )
        
        if cached_result:
            logger.info(f"⚡ Using cached result for step '{step.id}'")
            return cached_result
        
        return None
    
    async def _execute_with_alternatives(self, step: WorkflowStep, context: WorkflowContext,
                                       previous_results: Dict[str, StepResult]) -> StepResult:
        """Выполнить шаг с альтернативными стратегиями"""
        
        # Проверяем есть ли альтернативы в метаданных шага
        alternatives = step.metadata.get("alternatives", [])
        
        if not alternatives or not self.feature_manager.is_feature_enabled("alternative_execution", context.workflow_id):
            # Обычное выполнение
            return await self._execute_enhanced_step(step, context, previous_results)
        
        logger.info(f"🔀 Executing step '{step.id}' with {len(alternatives)} alternatives")
        
        # Выбираем стратегию выполнения
        strategy_name = step.metadata.get("execution_strategy", "race")
        try:
            strategy = ExecutionStrategy(strategy_name)
        except ValueError:
            strategy = ExecutionStrategy.RACE
        
        # Выполняем альтернативы
        return await self.alternative_executor.execute_alternatives(
            step, context, alternatives, strategy
        )
    
    async def _predict_and_optimize(self, workflow_definition: WorkflowDefinition, 
                                   context: WorkflowContext) -> Dict[str, Any]:
        """Предсказать производительность и предложить оптимизации"""
        
        # Предсказание производительности
        performance_prediction = self.performance_optimizer.predict_performance(
            workflow_definition.__dict__, context.__dict__
        )
        
        # Предложения по оптимизации
        optimizations = self.performance_optimizer.suggest_optimizations(
            workflow_definition.__dict__
        )
        
        logger.info(f"📊 Workflow prediction: {performance_prediction.estimated_duration:.1f}s, "
                   f"${performance_prediction.estimated_cost:.2f}, "
                   f"{len(optimizations)} optimization suggestions")
        
        return {
            "performance_prediction": performance_prediction,
            "optimization_suggestions": optimizations
        }

    async def _execute_single_step_attempt(self, context: Dict[str, Any]) -> StepResult:
        """Выполнение одной попытки шага"""
        
        step = context["step"]
        workflow_context = context["workflow_context"]
        previous_results = context["previous_results"]
        step_budget = context["step_budget"]
        
        attempt_start = datetime.now()
        
        # Pre-step planning
        plan = None
        if self.feature_manager.is_feature_enabled("pre_step_planner", workflow_context.workflow_id, step.id):
            plan = await self.planner.plan_step(step, workflow_context, previous_results)
            logger.info(f"📋 Created execution plan for step '{step.id}'")
        
        # Выполняем шаг через circuit breaker
        try:
            step_result = await self.circuit_breaker_manager.call_agent_safely(
                agent_name=step.agent_type,
                agent_func=self._execute_step_with_policy,
                step=step,
                context=workflow_context,
                plan=plan,
                attempt=1
            )
            
            # Потребляем бюджет времени
            duration = (datetime.now() - attempt_start).total_seconds()
            self.budget_manager.consume_budget(
                step.id, BudgetType.TIME, duration, "step", 
                f"Step execution time"
            )
            
            # Post-step validation and decision
            if self.feature_manager.is_feature_enabled("post_step_judge", workflow_context.workflow_id, step.id):
                default_plan = StepPlan(
                    step_id=step.id,
                    refined_task=step.task,
                    expected_output_format="text",
                    quality_criteria={"min_score": 0.7},
                    resource_budget={},
                    timeout_seconds=300,
                    retry_budget=3
                )
                
                validation_result = await self.judge.validate_result(
                    step_result, plan or default_plan, step
                )
                
                # Принимаем решение
                decision = await self.decision_engine.make_decision(
                    validation_result, step_result, step, workflow_context, 
                    list(previous_results.values())
                )
                
                # Обновляем результат с информацией о решении
                step_result.quality_score = validation_result.overall_score
                step_result.decision = decision.action
                step_result.decision_reason = decision.reason
                step_result.validator_results = validation_result.validator_results
                
                logger.info(f"⚖️ Step '{step.id}' validation: score={validation_result.overall_score:.2f}, "
                           f"decision={decision.action}")
                
                # Если решение не "proceed", выбрасываем исключение для retry
                if decision.action != "proceed":
                    raise Exception(f"Decision: {decision.action} - {decision.reason}")
            
            step_result.status = StepStatus.COMPLETED
            return step_result
            
        except Exception as e:
            # Потребляем бюджет даже при ошибке
            duration = (datetime.now() - attempt_start).total_seconds()
            self.budget_manager.consume_budget(
                step.id, BudgetType.TIME, duration, "step", 
                f"Failed step execution time"
            )
            raise
    
    async def _execute_step_with_policy(self, step: WorkflowStep, context: WorkflowContext,
                                       plan: Optional[StepPlan], attempt: int) -> StepResult:
        """Выполнение шага с учетом политик"""
        
        start_time = datetime.now()
        
        try:
            step = self._step_with_substituted_metadata(step, context)

            # Получаем бюджет для шага
            budget = self.policy_engine.get_budget("per_step", step)
            
            # Формируем задачу (используем plan если есть)
            task = plan.refined_task if plan else step.task
            
            # Подставляем переменные из контекста (используем базовый метод)
            task = self._format_task_with_variables(task, context, step.id)
            
            # Обработка в зависимости от типа шага
            if step.step_type == "tool":
                # Прямой вызов инструмента через базовый метод
                result = await self._execute_tool_step(step, context, task)
                
                # Проверяем результат инструмента на наличие ошибок
                # Если tool_manager перехватил исключение и вернул ошибку как строку
                if self._is_tool_error_result(result):
                    error_msg = self._extract_error_from_result(result)
                    raise RuntimeError(f"Инструмент {step.tool_name} завершился с ошибкой: {error_msg}")
                    
            else:
                # Выполнение через агента с enhanced логикой
                result = await self._execute_enhanced_agent_step(step, context, task, plan, budget)
            
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            step_result = StepResult(
                step_id=step.id,
                status=StepStatus.COMPLETED,
                output=result,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                attempt_number=attempt,
                agent_name=step.agent_type
            )
            
            logger.info(f"✅ Step '{step.id}' completed in {duration:.1f}s")
            return step_result
            
        except Exception as e:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            logger.error(f"❌ Step '{step.id}' failed: {e}")
            
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                error=str(e),
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                attempt_number=attempt,
                agent_name=step.agent_type
            )
    
    def _apply_decision_modifications(self, step: WorkflowStep,
                                      modifications: Dict[str, Any]) -> WorkflowStep:
        """Применить модификации к шагу для retry.

        Возвращает shallow-copy шага с изменённым task, чтобы не мутировать
        оригинальный объект (который разделяется между retry-итерациями).

        ВНИМАНИЕ: метод сейчас НЕ вызывается из кода (зарезервирован под интеграцию
        с on_retry_modify_context_func, см. docs). Фикс M75 устранил мутацию
        оригинала; сама проводка вызова — отдельная задача и здесь намеренно не
        добавляется во избежание изменения поведения retry вне scope аудита.
        """
        import copy
        modified = copy.copy(step)

        if modifications.get("enhance_prompt"):
            modified.task = step.task + "\n\nДополнительные требования: предоставьте подробное обоснование и примеры."

        if modifications.get("add_format_examples"):
            modified.task = modified.task + "\n\nПример желаемого формата ответа: структурированный текст с четкими разделами."

        return modified
    
    def get_enhanced_stats(self) -> Dict[str, Any]:
        """Получить статистику enhanced компонентов"""
        
        return {
            "feature_flags": self.feature_manager.feature_flags,
            "policy_stats": self.policy_engine.get_policy_stats(),
            "contract_stats": self.contract_registry.get_validation_stats(),
            "decision_history": self.decision_engine.get_decision_history(),
            "enhanced_enabled": self.feature_manager.is_enhanced_enabled(),
            
            # Resilience stats
            "circuit_breaker_stats": self.circuit_breaker_manager.get_all_stats(),
            "retry_stats": self.retry_engine.get_retry_statistics(),
            "budget_summary": self.budget_manager.get_budget_summary(),
            "loop_detection_stats": self.loop_detector.get_loop_statistics(),
            
            # Orchestration stats
            "conditional_engine_stats": self.conditional_engine.get_evaluation_statistics(),
            "alternative_execution_stats": self.alternative_executor.get_execution_statistics(),
            "cache_stats": self.cache_manager.get_combined_stats(),
            "supported_conditions": {
                "variables": self.conditional_engine.get_supported_variables(),
                "operators": self.conditional_engine.get_supported_operators()
            },
            
            # Monitoring stats
            "metrics_summary": self.metrics_collector.get_metrics_summary(),
            "alerts_summary": self.alert_manager.get_alerts_summary(),
            "analytics_summary": self.analytics_engine.get_analytics_summary()
        }
    
    async def _run_monitoring_analysis(self):
        """Запустить анализ мониторинга и проверку алертов"""
        
        try:
            # Получаем текущие метрики
            metrics_summary = self.metrics_collector.get_metrics_summary()
            
            # Проверяем алерты
            metrics_values = {
                "workflow_success_rate": self.metrics_collector.workflow_metrics.get_success_rate(),
                "avg_workflow_duration": self.metrics_collector.workflow_metrics.avg_workflow_duration,
                "avg_quality_score": self.metrics_collector.workflow_metrics.avg_quality_score,
                "cache_hit_rate": self.metrics_collector.workflow_metrics.get_cache_hit_rate(),
                "circuit_breaker_opens": self.metrics_collector.workflow_metrics.circuit_breaker_opens,
                "total_cost": self.metrics_collector.workflow_metrics.total_cost,
                "retry_success_rate": self.metrics_collector.workflow_metrics.get_retry_success_rate()
            }
            
            self.alert_manager.evaluate_rules(metrics_values)
            
            # Запускаем аналитику
            insights = self.analytics_engine.analyze_workflow_performance(metrics_values)
            
            if insights:
                logger.info(f"📊 Generated {len(insights)} performance insights")
                for insight in insights[:3]:  # Показываем топ-3
                    if insight.impact == "negative" and insight.priority >= 3:
                        logger.warning(f"⚠️ {insight.title}: {insight.description}")
                    elif insight.impact == "positive":
                        logger.info(f"✅ {insight.title}: {insight.description}")
            
        except Exception as e:
            logger.error(f"❌ Error in monitoring analysis: {e}")
    
    def generate_dashboard(self, dashboard_type: str = "overview") -> Dict[str, Any]:
        """Сгенерировать dashboard"""
        
        try:
            # Собираем данные
            metrics_data = self.metrics_collector.get_metrics_summary()
            alerts_data = self.alert_manager.get_alerts_summary()
            
            # Добавляем агрегированные метрики
            metrics_data.update({
                "workflow_success_rate": self.metrics_collector.workflow_metrics.get_success_rate(),
                "avg_workflow_duration": self.metrics_collector.workflow_metrics.avg_workflow_duration,
                "avg_quality_score": self.metrics_collector.workflow_metrics.avg_quality_score,
                "cache_hit_rate": self.metrics_collector.workflow_metrics.get_cache_hit_rate(),
                "total_cost": self.metrics_collector.workflow_metrics.total_cost,
                "workflow_executions_total": self.metrics_collector.workflow_metrics.workflow_executions_total,
                "circuit_breaker_opens": self.metrics_collector.workflow_metrics.circuit_breaker_opens,
                "retry_success_rate": self.metrics_collector.workflow_metrics.get_retry_success_rate()
            })
            
            # Генерируем dashboard
            return self.dashboard_generator.generate_dashboard(
                dashboard_type, metrics_data, alerts_data
            )
            
        except Exception as e:
            logger.error(f"❌ Error generating dashboard: {e}")
            return {"error": str(e)}
    
    def generate_report(self, report_type: str = "daily") -> Dict[str, Any]:
        """Сгенерировать отчет"""
        
        try:
            # Собираем данные для отчета
            data = {
                "workflow_executions_total": self.metrics_collector.workflow_metrics.workflow_executions_total,
                "workflow_success_rate": self.metrics_collector.workflow_metrics.get_success_rate(),
                "avg_workflow_duration": self.metrics_collector.workflow_metrics.avg_workflow_duration,
                "avg_quality_score": self.metrics_collector.workflow_metrics.avg_quality_score,
                "total_cost": self.metrics_collector.workflow_metrics.total_cost,
                "total_tokens": self.metrics_collector.workflow_metrics.total_tokens
            }
            
            return self.report_builder.generate_report(report_type, data)
            
        except Exception as e:
            logger.error(f"❌ Error generating report: {e}")
            return {"error": str(e)}
    
    def record_step_metrics(self, step_id: str, agent_type: str, duration: float,
                           success: bool, retry_count: int = 0, quality_score: float = None):
        """Записать метрики выполнения шага"""
        
        self.metrics_collector.record_step_execution(
            step_id, agent_type, duration, success, retry_count, quality_score
        )
        
        # Записываем события в других компонентах
        if retry_count > 0:
            self.metrics_collector.record_resource_usage(cost=retry_count * 0.01)  # Примерная стоимость retry
        
        if quality_score is not None and quality_score < 0.7:
            self.metrics_collector.workflow_metrics.quality_below_threshold_count += 1
    
    async def _execute_enhanced_agent_step(self, step: WorkflowStep, context: WorkflowContext, 
                                         task: str, plan: Optional[StepPlan], budget: ResourceLimits) -> Any:
        """Выполнение шага через агента в enhanced режиме с дополнительной логикой"""
        # Специальная обработка для менеджера с предзагрузкой агентов
        if step.agent_type == 'manager' and step.metadata and step.metadata.get('preload_agents'):
            # Работаем на per-call копии step и его metadata, чтобы не мутировать
            # общий объект WorkflowStep (он может разделяться параллельными шагами
            # из workflow_def.steps). Прежний код писал pipeline_type прямо в
            # step.metadata и «восстанавливал» его — это гонка при конкурентных шагах.
            import copy
            local_step = copy.copy(step)
            local_step.metadata = dict(step.metadata)
            local_step.metadata.setdefault('pipeline_type', 'enhanced_workflow')

            return await self._execute_manager_with_preloaded_agents(
                local_step, context, task
            )
        else:
            logger.info(f"🤖 Delegating enhanced step '{step.id}' to parent with enhanced pipeline_type")

            # Работаем на per-call копии, чтобы избежать гонки при параллельных шагах,
            # которые могут разделять один объект WorkflowStep из workflow_def.steps.
            import copy
            local_step = copy.copy(step)
            local_step._enhanced_pipeline_type = 'enhanced_workflow'

            result = await super()._execute_agent_step(local_step, context, task)
            return result
    
    def _is_tool_error_result(self, result: Any) -> bool:
        """
        Проверяет, содержит ли результат инструмента информацию об ошибке.
        
        Декоратор @tool от SmolagAgents может перехватывать исключения и возвращать
        их как часть результата вместо того, чтобы пробрасывать исключение.
        """
        if result is None:
            return False
            
        # Если результат - строка, проверяем на характерные паттерны ошибок
        if isinstance(result, str):
            error_patterns = [
                "Error:",
                "FileNotFoundError:",
                "ValueError:",
                "RuntimeError:",
                "Exception:",
                "Ошибка:",
                "Отсутствуют",
                "не найден",
                "не существует",
                "Traceback"
            ]
            
            result_lower = result.lower()
            for pattern in error_patterns:
                if pattern.lower() in result_lower:
                    return True
        
        # Если результат - словарь с полем error
        if isinstance(result, dict):
            if 'error' in result or 'exception' in result:
                return True
                
        return False
    
    def _extract_error_from_result(self, result: Any) -> str:
        """Извлекает сообщение об ошибке из результата инструмента."""
        if isinstance(result, str):
            return result
        elif isinstance(result, dict):
            return result.get('error', result.get('exception', str(result)))
        else:
            return str(result)
