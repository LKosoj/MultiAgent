"""
Enhanced Workflow Engine с интеллектуальным управлением качеством
"""

import asyncio
import logging
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Any, Optional, Set, Union
from pathlib import Path
from datetime import datetime
import yaml

from tool_runtime_context import (
    SupervisorExecutionEvidence,
    reset_tool_runtime_context,
    set_tool_runtime_context,
)
from workflow_redaction import _redact_workflow_log_value

from .engine import WorkflowEngine
from .deadline import DeadlineBudget, WorkflowDeadlineExceeded, execute_step_attempt
from .models import (
    WorkflowDefinition,
    WorkflowResult,
    WorkflowContext,
    WorkflowStatus,
    StepResult,
    StepStatus,
    WorkflowStep,
    StepPlan,
    ValidationResult,
    Decision,
    ResourceLimits,
    RetryPolicy,
    TextToSqlTerminalResult,
    TextToSqlTerminalStatus,
    WorkflowExecutionError,
    TEXT_TO_SQL_WORKFLOW_CATEGORY,
    bound_text_to_sql_error,
    is_text_to_sql_workflow_name,
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
from .resilience.retry import AdaptiveRetryEngine, JudgeRetryRequested
from .resilience.budget import BudgetManager, BudgetType
from .resilience.loop_detection import LoopDetector

# Orchestration components
from .orchestration.conditions import ConditionalEngine
from .orchestration.alternatives import AlternativeExecutor, ExecutionStrategy
from .orchestration.cache import CacheManager
from .orchestration.predictor import QualityPredictor, PerformanceOptimizer

# Monitoring components
from .monitoring.metrics import MetricsCollector
from .monitoring.alerts import (
    AlertManager,
    log_notification_handler,
    console_notification_handler,
)
from .monitoring.analytics import AnalyticsEngine
from .monitoring.dashboard import DashboardGenerator, ReportBuilder

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _SettledThreadOperation:
    value: Any = None
    cancellation: asyncio.CancelledError | None = None
    error: BaseException | None = None


async def _settle_thread_operation(function, /, *args) -> _SettledThreadOperation:
    """Let one durable thread operation finish even when its waiter is cancelled."""

    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        except BaseException:
            break
    try:
        return _SettledThreadOperation(task.result(), cancellation)
    except BaseException as exc:
        return _SettledThreadOperation(cancellation=cancellation, error=exc)


def _step_status_value(status: Any) -> Any:
    """Normalize a StepStatus (or already-plain value) to its `.value`.

    Collapses the 13 repeated `getattr(status, "value", status)` call sites
    across this module into one helper.
    """
    return getattr(status, "value", status)


class FeatureManager:
    """Менеджер включения/отключения фич"""

    def __init__(self, config_path: Optional[Path] = None):
        self.global_config = {
            "enhanced_workflow": {
                "enabled": True,
                "rollout_percentage": 100,
                "fallback_to_legacy": True,
            }
        }

        self.feature_flags = {
            "features": {
                "pre_step_planner": {"enabled": True, "rollout": 100},
                "post_step_judge": {"enabled": True, "rollout": 100},
                "semantic_validation": {"enabled": False, "rollout": 0},
                "multi_agent_consensus": {"enabled": False, "rollout": 0},
                "human_in_the_loop": {"enabled": True, "rollout": 100},
                "circuit_breaker": {"enabled": True, "rollout": 100},
            }
        }
        self.workflow_overrides: Dict[str, Dict[str, Any]] = {}
        self._load_config(
            config_path
            or Path(__file__).resolve().parent / "config" / "enhanced_global.yaml"
        )

    def _load_config(self, config_path: Path) -> None:
        if not config_path.exists():
            return
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning(
                "⚠️ Не удалось загрузить enhanced config %s: %s", config_path, exc
            )
            return
        if isinstance(data.get("enhanced_workflow"), dict):
            self.global_config["enhanced_workflow"].update(data["enhanced_workflow"])
        if isinstance(data.get("features"), dict):
            for name, config in data["features"].items():
                if isinstance(config, dict):
                    self.feature_flags.setdefault("features", {}).setdefault(
                        name, {}
                    ).update(config)
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

        rollout = self.global_config.get("enhanced_workflow", {}).get(
            "rollout_percentage", 0
        )
        if workflow_id:
            return self._check_rollout(workflow_id, rollout)
        return rollout == 100

    def is_feature_enabled(
        self, feature: str, workflow_id: str = None, step_id: str = None
    ) -> bool:
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

    def __init__(self, *, document_registry=None):
        # Инициализируем родительский класс
        super().__init__()

        # Добавляем enhanced компоненты
        self.feature_manager = FeatureManager()
        self.policy_engine = PolicyEngine()
        self.contract_registry = ContractRegistry()
        self.document_registry = document_registry
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
        by_name = is_text_to_sql_workflow_name(workflow_definition.name)
        by_category = (
            workflow_definition.metadata.get("category")
            == TEXT_TO_SQL_WORKFLOW_CATEGORY
        )
        if by_name != by_category:
            logger.warning(
                "Text-to-SQL identity drift: name=%r category=%r; exact name is authoritative",
                workflow_definition.name,
                workflow_definition.metadata.get("category"),
            )
        return by_name

    @staticmethod
    def _context_deadline(context: WorkflowContext) -> Optional[DeadlineBudget]:
        deadline = getattr(context, "_deadline_budget", None)
        if deadline is not None and not isinstance(deadline, DeadlineBudget):
            raise TypeError("context._deadline_budget must be a DeadlineBudget")
        return deadline

    def _ensure_workflow_deadline(
        self,
        workflow_definition: WorkflowDefinition,
        context: WorkflowContext,
    ) -> Optional[DeadlineBudget]:
        return self._context_deadline(context)

    @staticmethod
    def _require_deadline(
        context: WorkflowContext,
        boundary: str,
    ) -> None:
        deadline = EnhancedWorkflowEngine._context_deadline(context)
        if deadline is not None:
            deadline.require_remaining(boundary)

    def _text_to_sql_failure_outcome(
        self,
        context: WorkflowContext,
        step_results: Dict[str, StepResult],
        reason_code: str,
        error: str,
    ) -> TextToSqlTerminalResult:
        solving_result = step_results.get("sql_solving")
        solving = (
            solving_result.output
            if solving_result is not None
            and _step_status_value(solving_result.status)
            == StepStatus.COMPLETED.value
            else None
        )
        raw_sql = solving.get("sql", "") if isinstance(solving, dict) else ""
        sql = raw_sql if isinstance(raw_sql, str) and raw_sql.strip() else ""
        generated = bool(sql)
        approved = generated and self._has_authoritative_solver_gate(context)
        return TextToSqlTerminalResult.from_mapping(
            {
                "run_id": str(context.variables.get("run_id") or context.workflow_id),
                "status": TextToSqlTerminalStatus.FAILED.value,
                "reason_code": reason_code,
                "sql": sql,
                "generated": generated,
                "approved": approved,
                "executed": False,
                "dry_run": False,
                "audited": False,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "error": bound_text_to_sql_error(error),
                "execution": {},
                "audit": {},
                "persistence": {"status": "not_attempted"},
                "result_review": {},
                "ambiguity": None,
            }
        )

    def _derive_text_to_sql_terminal_outcome(
        self,
        workflow_definition: WorkflowDefinition,
        context: WorkflowContext,
        step_results: Dict[str, StepResult],
    ) -> TextToSqlTerminalResult:
        """Read the terminal result produced by the Typed pipeline."""
        if not self._is_text_to_sql_workflow(workflow_definition):
            raise WorkflowExecutionError(
                "Text-to-SQL terminal derivation called for a generic workflow"
            )

        research_terminal = self._research_terminal_outcome(context)
        if research_terminal is not None:
            return research_terminal

        runtime = self._exact_typed_runtime(context)
        solver_terminal = (
            runtime.verified_solver_terminal if runtime is not None else None
        )
        if type(solver_terminal) is TextToSqlTerminalResult:
            return TextToSqlTerminalResult.from_mapping(solver_terminal.to_mapping())

        solving_result = step_results.get("sql_solving")
        if (
            solving_result is not None
            and _step_status_value(solving_result.status) == StepStatus.FAILED.value
        ):
            return self._text_to_sql_failure_outcome(
                context,
                step_results,
                "MANDATORY_STEP_NOT_COMPLETED",
                solving_result.error or "Typed solver did not complete",
            )

        audit_result = step_results.get("db_audit")
        if audit_result is None:
            return self._text_to_sql_failure_outcome(
                context,
                step_results,
                "DB_AUDIT_MISSING",
                "Typed finalizer result is missing",
            )
        if _step_status_value(audit_result.status) != StepStatus.COMPLETED.value:
            return self._text_to_sql_failure_outcome(
                context,
                step_results,
                "DB_AUDIT_FAILED",
                audit_result.error or "Typed finalizer did not complete",
            )
        try:
            outcome = TextToSqlTerminalResult.from_mapping(audit_result.output)
        except (TypeError, ValueError) as exc:
            return self._text_to_sql_failure_outcome(
                context,
                step_results,
                "DB_AUDIT_OUTPUT_INVALID",
                str(exc),
            )
        expected_run_id = str(context.variables.get("run_id") or context.workflow_id)
        if outcome.run_id != expected_run_id:
            return self._text_to_sql_failure_outcome(
                context,
                step_results,
                "DB_AUDIT_RUN_ID_MISMATCH",
                "Typed finalizer result belongs to a different run",
            )
        return outcome

    @staticmethod
    def _research_terminal_outcome(
        context: WorkflowContext,
    ) -> TextToSqlTerminalResult | None:
        runtime = EnhancedWorkflowEngine._exact_typed_runtime(context)
        if runtime is None:
            return None
        outcome = runtime.verified_research_outcome
        if outcome is None:
            return None
        from custom_tools.text_to_sql.adaptive.terminal import (
            research_stop_terminal_result,
        )

        return research_stop_terminal_result(
            runtime.run_id,
            outcome.stop_reason,
            outcome.ambiguity,
        )

    @staticmethod
    def _db_audit_has_terminal_step_result(
        step_results: Dict[str, StepResult],
    ) -> bool:
        audit_result = step_results.get("db_audit")
        if audit_result is None:
            return False
        status_value = _step_status_value(audit_result.status)
        return status_value in {
            StepStatus.COMPLETED.value,
            StepStatus.SKIPPED.value,
            StepStatus.FAILED.value,
        }

    @staticmethod
    def _text_to_sql_cancelled_outcome(
        context: WorkflowContext,
        workflow_id: str,
    ) -> TextToSqlTerminalResult:
        return TextToSqlTerminalResult.from_mapping(
            {
                "run_id": str(context.variables.get("run_id") or workflow_id),
                "status": TextToSqlTerminalStatus.CANCELLED.value,
                "reason_code": "CANCELLED",
                "sql": "",
                "generated": False,
                "approved": False,
                "executed": False,
                "dry_run": False,
                "audited": False,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "error": "Workflow was cancelled",
                "execution": {},
                "audit": {},
                "persistence": {"status": "not_attempted"},
                "result_review": {},
                "ambiguity": None,
            }
        )

    @staticmethod
    def _text_to_sql_timed_out_outcome(
        context: WorkflowContext,
        workflow_id: str,
        error: Exception,
    ) -> TextToSqlTerminalResult:
        return TextToSqlTerminalResult.from_mapping(
            {
                "run_id": str(context.variables.get("run_id") or workflow_id),
                "status": TextToSqlTerminalStatus.TIMED_OUT.value,
                "reason_code": "TIMED_OUT",
                "sql": "",
                "generated": False,
                "approved": False,
                "executed": False,
                "dry_run": False,
                "audited": False,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "error": bound_text_to_sql_error(error),
                "execution": {},
                "audit": {},
                "persistence": {"status": "not_attempted"},
                "result_review": {},
                "ambiguity": None,
            }
        )

    def _build_text_to_sql_timed_out_result(
        self,
        workflow_definition: WorkflowDefinition,
        context: WorkflowContext,
        step_results: Dict[str, StepResult],
        start_time: datetime,
        error: Exception,
    ) -> WorkflowResult:
        end_time = datetime.now()
        outcome = self._text_to_sql_timed_out_outcome(
            context,
            context.workflow_id,
            error,
        )
        return WorkflowResult(
            workflow_id=context.workflow_id,
            status=WorkflowStatus.FAILED,
            start_time=start_time,
            end_time=end_time,
            duration_seconds=(end_time - start_time).total_seconds(),
            total_steps=len(workflow_definition.steps),
            completed_steps=sum(
                result.status == StepStatus.COMPLETED
                for result in step_results.values()
            ),
            failed_steps=sum(
                result.status == StepStatus.FAILED for result in step_results.values()
            ),
            step_results=step_results,
            final_output=self._safe_text_to_sql_final_output(
                workflow_definition,
                outcome,
            ),
            error=outcome.error,
            metadata={"timed_out": True},
            terminal_outcome=outcome,
        )

    def _build_text_to_sql_preserved_terminal_result(
        self,
        workflow_definition: WorkflowDefinition,
        context: WorkflowContext,
        step_results: Dict[str, StepResult],
        start_time: datetime,
        outcome: TextToSqlTerminalResult,
    ) -> WorkflowResult:
        """Return an already-derived authoritative terminal outcome unchanged."""
        end_time = datetime.now()
        succeeded = outcome.status is TextToSqlTerminalStatus.SUCCEEDED
        return WorkflowResult(
            workflow_id=context.workflow_id,
            status=(WorkflowStatus.COMPLETED if succeeded else WorkflowStatus.FAILED),
            start_time=start_time,
            end_time=end_time,
            duration_seconds=(end_time - start_time).total_seconds(),
            total_steps=len(workflow_definition.steps),
            completed_steps=sum(
                result.status == StepStatus.COMPLETED
                for result in step_results.values()
            ),
            failed_steps=sum(
                result.status == StepStatus.FAILED for result in step_results.values()
            ),
            step_results=step_results,
            final_output=self._attach_text_to_sql_early_stop_evidence(
                context,
                outcome,
                self._canonicalize_text_to_sql_final_output(
                    workflow_definition,
                    outcome,
                    self._safe_text_to_sql_final_output(
                        workflow_definition,
                        outcome,
                    ),
                ),
            ),
            error=None if succeeded else (outcome.error or outcome.reason_code),
            metadata={
                "deadline_expired_after_terminal": True,
                "persistence": "deadline_exhausted",
            },
            terminal_outcome=outcome,
        )

    @staticmethod
    def _text_to_sql_aggregation_failure_outcome(
        outcome: TextToSqlTerminalResult,
        error: Exception,
    ) -> TextToSqlTerminalResult:
        if outcome.status is not TextToSqlTerminalStatus.SUCCEEDED:
            return outcome
        mapping = outcome.to_mapping()
        mapping.update(
            {
                "status": TextToSqlTerminalStatus.FAILED.value,
                "reason_code": "RESULT_AGGREGATION_FAILED",
                "error": bound_text_to_sql_error(error),
            }
        )
        return TextToSqlTerminalResult.from_mapping(mapping)

    @staticmethod
    def _safe_text_to_sql_final_output(
        workflow_definition: WorkflowDefinition,
        outcome: TextToSqlTerminalResult,
    ) -> Dict[str, Any]:
        return {
            "type": "workflow_outputs",
            "workflow_name": workflow_definition.name,
            "final": outcome.to_mapping(),
            "outputs": {"final": outcome.to_mapping()},
        }

    @staticmethod
    def _canonicalize_text_to_sql_final_output(
        workflow_definition: WorkflowDefinition,
        outcome: TextToSqlTerminalResult,
        final_output: Any,
    ) -> Dict[str, Any]:
        """Publish only the derived terminal proof at both public final paths."""
        if not isinstance(final_output, dict):
            final_output = EnhancedWorkflowEngine._safe_text_to_sql_final_output(
                workflow_definition,
                outcome,
            )

        canonical = dict(final_output)
        raw_outputs = canonical.get("outputs")
        outputs = dict(raw_outputs) if isinstance(raw_outputs, dict) else {}
        canonical.update(
            {
                "type": "workflow_outputs",
                "workflow_name": workflow_definition.name,
                "final": outcome.to_mapping(),
                "outputs": outputs,
            }
        )
        outputs["final"] = outcome.to_mapping()
        return canonical

    def _attach_text_to_sql_early_stop_evidence(
        self,
        context: WorkflowContext,
        outcome: TextToSqlTerminalResult,
        final_output: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Publish evidence derived from the Typed runtime."""

        canonical = dict(final_output)
        raw_outputs = canonical.get("outputs")
        outputs = dict(raw_outputs) if isinstance(raw_outputs, dict) else {}
        outputs.pop("early_stop_semantic_evidence", None)
        outputs.pop("early_stop_stagnation_evidence", None)
        canonical["outputs"] = outputs

        runtime = self._exact_typed_runtime(context)
        if runtime is None:
            return canonical

        from .text_to_sql_early_stop_evidence import (
            build_text_to_sql_early_stop_evidence,
            build_text_to_sql_stagnation_evidence,
        )

        evidence = build_text_to_sql_early_stop_evidence(
            research_outcome=runtime.verified_research_outcome,
            solver_state=runtime.verified_solver_state,
            terminal_status=outcome.status.value,
            terminal_reason_code=outcome.reason_code,
        )
        if evidence is not None:
            outputs["early_stop_semantic_evidence"] = evidence
        stagnation_evidence = build_text_to_sql_stagnation_evidence(
            research_outcome=runtime.verified_research_outcome,
            terminal_status=outcome.status.value,
            terminal_reason_code=outcome.reason_code,
        )
        if stagnation_evidence is not None:
            outputs["early_stop_stagnation_evidence"] = stagnation_evidence
        return canonical

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
            return False
        return self._coerce_bool(
            self.feature_manager.global_config.get("enhanced_workflow", {}).get(
                "fallback_to_legacy"
            ),
            True,
        )

    async def execute_workflow(
        self,
        workflow_definition: WorkflowDefinition,
        context: Optional[WorkflowContext] = None,
        client_id: Optional[str] = None,
        *,
        skip_steps: Optional[Set[str]] = None,
        restored_step_results: Optional[Dict[str, StepResult]] = None,
    ) -> WorkflowResult:
        """Enhanced выполнение workflow"""

        is_text_to_sql = self._is_text_to_sql_workflow(workflow_definition)
        if is_text_to_sql and (
            context is None or self._context_deadline(context) is None
        ):
            raise WorkflowExecutionError(
                "Text-to-SQL requires a persisted deadline before execution"
            )

        # Resume (skip_steps задан): пропускаем уже завершённые шаги.
        # Пайплайны с requires_enhanced_engine исполняем через enhanced-исполнитель
        # (M-3), а не деградируем в legacy — иначе поздний сбой (montage/video)
        # требовал бы полного дорогого ре-рана. Перезапускаемые side-effect-шаги
        # опираются на per-tool идемпотентность (provider_jobs.json): движковый
        # resume их НЕ пропускает, но повторный вызов тула переиспользует джобу.
        # Остальные (не-required) пайплайны резюмируются базовым исполнителем.
        if skip_steps is not None:
            if workflow_definition.requires_enhanced_engine:
                return await self._execute_enhanced_workflow(
                    workflow_definition,
                    context,
                    client_id,
                    skip_steps=skip_steps,
                    restored_step_results=restored_step_results,
                )
            return await super().execute_workflow(
                workflow_definition,
                context,
                client_id,
                skip_steps=skip_steps,
                restored_step_results=restored_step_results,
            )

        # Проверяем включен ли enhanced layer
        if not is_text_to_sql and not self.feature_manager.is_enhanced_enabled(
            context.workflow_id if context else None
        ):
            if workflow_definition.requires_enhanced_engine:
                raise WorkflowExecutionError(
                    f"Workflow '{workflow_definition.name}' requires enhanced engine "
                    "(pipeline.requires_enhanced_engine=true), but enhanced layer is disabled"
                )
            logger.info("📋 Enhanced layer disabled, falling back to legacy execution")
            return await super().execute_workflow(
                workflow_definition, context, client_id
            )

        try:
            logger.info(
                f"🧠 Starting enhanced execution of '{workflow_definition.name}'"
            )
            return await self._execute_enhanced_workflow(
                workflow_definition, context, client_id
            )

        except WorkflowDeadlineExceeded:
            raise
        except Exception as e:
            logger.error(f"❌ Enhanced execution failed: {e}")

            # Fallback к legacy если включен
            if self._should_fallback_to_legacy(workflow_definition, context):
                if workflow_definition.requires_enhanced_engine:
                    raise WorkflowExecutionError(
                        f"Workflow '{workflow_definition.name}' requires enhanced engine "
                        "(pipeline.requires_enhanced_engine=true), legacy fallback is not allowed"
                    ) from e
                logger.info("🔄 Falling back to legacy execution")
                return await super().execute_workflow(
                    workflow_definition, context, client_id
                )
            else:
                raise

    async def _execute_enhanced_workflow(
        self,
        workflow_def: WorkflowDefinition,
        context: Optional[WorkflowContext] = None,
        client_id: Optional[str] = None,
        *,
        skip_steps: Optional[Set[str]] = None,
        restored_step_results: Optional[Dict[str, StepResult]] = None,
    ) -> WorkflowResult:
        """Enhanced выполнение с интеллектуальным управлением"""

        # M-1: checkpoint_strategy — best-effort валидация. Реально чекпойнт пишется
        # после каждого шага (см. _on_step_completed); поле лишь документирует это.
        # Предупреждаем оператора при неизвестном значении, чтобы не строить ложных
        # ожиданий (сам ключ не влияет на исполнение).
        checkpoint_strategy = workflow_def.error_handling.get("checkpoint_strategy")
        if checkpoint_strategy is not None and checkpoint_strategy not in {
            "after_each_step"
        }:
            logger.warning(
                "⚠️ Неизвестный checkpoint_strategy=%r — поддерживается только "
                "'after_each_step'; значение игнорируется",
                checkpoint_strategy,
            )

        # Создаем контекст если не передан
        if context is None:
            context = WorkflowContext(
                workflow_id=f"{workflow_def.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                session_id=f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                client_id=client_id,
            )

        workflow_id = context.workflow_id
        start_time = datetime.now()
        is_text_to_sql = self._is_text_to_sql_workflow(workflow_def)
        if is_text_to_sql and self._context_deadline(context) is None:
            raise WorkflowExecutionError(
                "Text-to-SQL requires a persisted deadline before execution"
            )
        self._ensure_workflow_deadline(workflow_def, context)
        if is_text_to_sql:
            self._install_text_to_sql_typed_runtime(context)
        try:
            self._require_deadline(context, "workflow start")
        except WorkflowDeadlineExceeded as exc:
            if is_text_to_sql:
                return self._build_text_to_sql_timed_out_result(
                    workflow_def,
                    context,
                    dict(restored_step_results or {}),
                    start_time,
                    exc,
                )
            raise

        # Записываем начало workflow в метрики
        self.metrics_collector.record_workflow_start(workflow_id, workflow_def.name)

        try:
            # Создаем бюджеты для workflow
            workflow_budget = self.budget_manager.create_workflow_budget(workflow_id)
            logger.info(f"💰 Created budget for workflow '{workflow_id}'")

            # Сохраняем начальное состояние (используем базовый helper).
            # На resume пред-заполняем checkpoint восстановленными шагами.
            self._require_deadline(context, "initial checkpoint")
            resource_lease = await self._on_workflow_started(
                workflow_def,
                context,
                client_id,
                start_time,
                step_results=restored_step_results,
            )

            # Выполняем шаги с enhanced логикой
            executed_steps = await self._execute_enhanced_steps(
                workflow_def,
                context,
                skip_steps=skip_steps,
                restored_step_results=restored_step_results,
            )
            step_results = executed_steps

            terminal_outcome = (
                self._research_terminal_outcome(context)
                if is_text_to_sql
                else None
            )
            if is_text_to_sql and terminal_outcome is None:
                solver_terminal = self._adaptive_solver_terminal(context)
                if solver_terminal is not None:
                    terminal_outcome = TextToSqlTerminalResult.from_mapping(
                        solver_terminal.to_mapping()
                    )
            if (
                is_text_to_sql
                and terminal_outcome is None
                and self._db_audit_has_terminal_step_result(step_results)
            ):
                terminal_outcome = self._derive_text_to_sql_terminal_outcome(
                    workflow_def,
                    context,
                    step_results,
                )

            self._require_deadline(context, "terminalization")

            is_cancelled = await self._is_workflow_cancelled(workflow_id)
            if is_cancelled and (not is_text_to_sql or terminal_outcome is None):
                cancelled_result = await self._build_cancelled_workflow_result(
                    workflow_def,
                    context,
                    step_results,
                    start_time,
                )
                if is_text_to_sql:
                    cancelled_result.terminal_outcome = (
                        self._text_to_sql_cancelled_outcome(context, workflow_id)
                    )
                return cancelled_result

            if is_cancelled and terminal_outcome is not None:
                logger.info(
                    "Cancellation observed after terminal db_audit for %s; "
                    "preserving terminal outcome %s",
                    workflow_id,
                    terminal_outcome.status.value,
                )

            if is_text_to_sql and terminal_outcome is None:
                terminal_outcome = self._derive_text_to_sql_terminal_outcome(
                    workflow_def,
                    context,
                    step_results,
                )

            aggregation_failed = False
            self._require_deadline(context, "result aggregation")

            async def aggregate(_context: object) -> Any:
                return await self.aggregator.aggregate_final_result(
                    step_results,
                    workflow_def,
                    context,
                )

            try:
                final_output = await execute_step_attempt(
                    "result aggregation",
                    aggregate,
                    None,
                    attempt_timeout=None,
                    deadline=self._context_deadline(context),
                )
            except WorkflowDeadlineExceeded:
                raise
            except Exception as exc:
                if not is_text_to_sql or terminal_outcome is None:
                    raise
                aggregation_failed = True
                terminal_outcome = self._text_to_sql_aggregation_failure_outcome(
                    terminal_outcome,
                    exc,
                )
                final_output = self._safe_text_to_sql_final_output(
                    workflow_def,
                    terminal_outcome,
                )
                logger.error(
                    "Text-to-SQL result aggregation failed after terminalization: %s",
                    bound_text_to_sql_error(exc),
                )
            if (
                is_text_to_sql
                and terminal_outcome is not None
                and isinstance(final_output, dict)
                and final_output.get("type") == "fallback_result"
            ):
                fallback_error = final_output.get("error") or (
                    "FinalAggregator returned fallback_result"
                )
                aggregation_failed = True
                terminal_outcome = self._text_to_sql_aggregation_failure_outcome(
                    terminal_outcome,
                    RuntimeError(bound_text_to_sql_error(fallback_error)),
                )
                final_output = self._safe_text_to_sql_final_output(
                    workflow_def,
                    terminal_outcome,
                )
                logger.error(
                    "Text-to-SQL FinalAggregator returned fallback_result: %s",
                    bound_text_to_sql_error(fallback_error),
                )

            self._require_deadline(context, "result finalization")
            if is_text_to_sql and terminal_outcome is not None:
                final_output = self._canonicalize_text_to_sql_final_output(
                    workflow_def,
                    terminal_outcome,
                    final_output,
                )
                final_output = self._attach_text_to_sql_early_stop_evidence(
                    context,
                    terminal_outcome,
                    final_output,
                )

            # Завершаем workflow
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            # Подсчитываем статистику
            completed_steps = len(
                [r for r in step_results.values() if r.status == StepStatus.COMPLETED]
            )
            failed_steps = len(
                [r for r in step_results.values() if r.status == StepStatus.FAILED]
            )
            stop_on_failure = (
                workflow_def.error_handling.get("on_failure", "continue") != "continue"
            )
            if is_text_to_sql:
                workflow_status = (
                    WorkflowStatus.COMPLETED
                    if not aggregation_failed
                    and terminal_outcome.status is TextToSqlTerminalStatus.SUCCEEDED
                    else WorkflowStatus.FAILED
                )
            else:
                workflow_status = (
                    WorkflowStatus.FAILED
                    if failed_steps and stop_on_failure
                    else WorkflowStatus.COMPLETED
                )

            if workflow_status == WorkflowStatus.COMPLETED:
                self._require_deadline(context, "completion checkpoint")
                await self._on_workflow_completed(
                    workflow_id,
                    final_output,
                    context=context,
                )
            else:
                await self._await_state_manager_write(
                    context,
                    "failure checkpoint",
                    lambda: self.state_manager.save_checkpoint(
                        workflow_id=workflow_id,
                        status=WorkflowStatus.FAILED,
                        context=context,
                        step_results=step_results,
                        current_step=context.current_step,
                        metadata={
                            "workflow_name": workflow_def.name,
                            "error": (
                                terminal_outcome.error or terminal_outcome.reason_code
                                if terminal_outcome is not None
                                else f"Workflow failed steps: {failed_steps}"
                            ),
                        },
                    ),
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
                final_output=final_output,
                error=(
                    terminal_outcome.error or terminal_outcome.reason_code
                    if terminal_outcome is not None
                    and terminal_outcome.status is not TextToSqlTerminalStatus.SUCCEEDED
                    else None
                ),
                terminal_outcome=terminal_outcome,
            )

            logger.info(
                f"✅ Enhanced workflow {workflow_id} finished with status {workflow_status.value} in {duration:.1f}s"
            )
            return result

        except WorkflowDeadlineExceeded as exc:
            logger.error("⏱️ Enhanced workflow %s timed out: %s", workflow_id, exc)
            partial_results = dict(
                getattr(context, "_workflow_step_results", None)
                or restored_step_results
                or {}
            )
            if "resource_lease" in locals():
                try:
                    await self._on_workflow_failed(
                        workflow_def,
                        context,
                        resource_lease,
                        exc,
                        step_results=partial_results,
                    )
                except WorkflowDeadlineExceeded:
                    logger.warning(
                        "⏱️ Failure checkpoint skipped after deadline for %s",
                        workflow_id,
                    )
            if is_text_to_sql:
                primary_outcome = locals().get("terminal_outcome")
                if isinstance(primary_outcome, TextToSqlTerminalResult):
                    return self._build_text_to_sql_preserved_terminal_result(
                        workflow_def,
                        context,
                        partial_results,
                        start_time,
                        primary_outcome,
                    )
                return self._build_text_to_sql_timed_out_result(
                    workflow_def,
                    context,
                    partial_results,
                    start_time,
                    exc,
                )
            raise
        except Exception as e:
            logger.error(f"❌ Enhanced workflow {workflow_id} failed: {e}")
            if "resource_lease" in locals():
                await self._on_workflow_failed(workflow_def, context, resource_lease, e)
            raise
        finally:
            if "resource_lease" in locals():
                await self._release_workflow_resources(workflow_id)

    async def _execute_enhanced_steps(
        self,
        workflow_def: WorkflowDefinition,
        context: WorkflowContext,
        *,
        skip_steps: Optional[Set[str]] = None,
        restored_step_results: Optional[Dict[str, StepResult]] = None,
    ) -> Dict[str, StepResult]:
        """Выполнение шагов с enhanced логикой"""

        if workflow_def.parallel_execution:
            return await self._execute_enhanced_steps_parallel(
                workflow_def,
                context,
                skip_steps=skip_steps,
                restored_step_results=restored_step_results,
            )
        else:
            return await self._execute_enhanced_steps_sequential(
                workflow_def,
                context,
                skip_steps=skip_steps,
                restored_step_results=restored_step_results,
            )

    async def _execute_enhanced_steps_sequential(
        self,
        workflow_def: WorkflowDefinition,
        context: WorkflowContext,
        *,
        skip_steps: Optional[Set[str]] = None,
        restored_step_results: Optional[Dict[str, StepResult]] = None,
    ) -> Dict[str, StepResult]:
        """Последовательное выполнение с enhanced логикой"""
        # Resume: пред-заполняем результатами восстановленных шагов.
        step_results = dict(restored_step_results or {})
        context._workflow_step_results = step_results
        # H-2 (part B): публикуем workflow_def на context, чтобы _execute_enhanced_step
        # мог прочитать retry-политику (в parallel-пути это делается отдельно).
        context._workflow_definition = workflow_def

        for step in workflow_def.steps:
            self._require_deadline(context, f"step '{step.id}' scheduling")
            # Resume: пропускаем уже завершённые шаги (их результат уже в step_results).
            if skip_steps is not None and step.id in skip_steps:
                logger.info("⏭️ Enhanced resume: пропускаем завершённый шаг %s", step.id)
                continue

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
                    end_time=datetime.now(),
                )
                continue

            # Проверяем условие выполнения (используем helper из базового класса)
            if self._should_skip_step_by_condition(step, context):
                step_results[step.id] = StepResult(
                    step_id=step.id,
                    status=StepStatus.SKIPPED,
                    output=context.step_outputs.get(step.id),
                    start_time=datetime.now(),
                    end_time=datetime.now(),
                )
                continue

            # Выполняем шаг с enhanced обработкой
            step_result = await self._execute_enhanced_step(
                step, context, step_results
            )
            step_results[step.id] = step_result

            if _step_status_value(step_result.status) == StepStatus.COMPLETED.value:
                step_result = await self._complete_enhanced_step_with_output_retry(
                    step, step_result, context, workflow_def, step_results
                )
                step_results[step.id] = step_result
                if self._adaptive_solver_terminal(context) is not None:
                    break
                if await self._is_workflow_cancelled(context.workflow_id):
                    logger.info(
                        "🚫 Enhanced workflow %s отменён после шага %s",
                        context.workflow_id,
                        step.id,
                    )
                    break

            if _step_status_value(step_result.status) != StepStatus.COMPLETED.value:
                # Шаг провален - выполняем rollback если определен (используем метод из базового класса)
                logger.error(
                    f"❌ Enhanced: Шаг {step.id} провален: {step_result.error}"
                )

                if step.rollback_action:
                    await self._execute_rollback(
                        step.rollback_action, context, step_result
                    )

                if (
                    workflow_def.error_handling.get("on_failure", "continue")
                    != "continue"
                ):
                    logger.error(
                        "⛔ Enhanced: прерываем workflow из-за on_failure=%s",
                        workflow_def.error_handling.get("on_failure"),
                    )
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
        self._require_deadline(context, f"step '{step.id}' output retry")

        async def retry_executor(
            retry_step: WorkflowStep,
            retry_context: WorkflowContext,
            retry_workflow_def: WorkflowDefinition,
        ) -> StepResult:
            retry_result = await self._execute_enhanced_step(
                retry_step, retry_context, previous_results
            )
            if _step_status_value(retry_result.status) == StepStatus.COMPLETED.value:
                previous_results[retry_step.id] = retry_result
                completed_retry_result = (
                    await self._complete_enhanced_step_with_output_retry(
                        retry_step,
                        retry_result,
                        retry_context,
                        retry_workflow_def,
                        previous_results,
                    )
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
            self._require_deadline(context, f"step '{step.id}' checkpoint")
            await self._on_step_completed(
                context.workflow_id, step, step_result, context, previous_results
            )
            return step_result
        return retried

    async def _maybe_run_output_retry(
        self,
        step: WorkflowStep,
        step_result: StepResult,
        context: WorkflowContext,
        workflow_def: WorkflowDefinition,
        step_executor=None,
        step_results=None,
        rerun_step_committed_by_executor: bool = False,
    ) -> Optional[StepResult]:
        """Typed owns SQL retries; skip the workflow's older retry chain."""

        if step.id in {"sql_solving", "db_audit"} and (
            self._exact_typed_runtime(context) is not None
        ):
            return None
        return await super()._maybe_run_output_retry(
            step,
            step_result,
            context,
            workflow_def,
            step_executor=step_executor,
            step_results=step_results,
            rerun_step_committed_by_executor=rerun_step_committed_by_executor,
        )

    def _install_text_to_sql_typed_runtime(
        self,
        context: WorkflowContext,
    ) -> None:
        """Install the runtime for the only supported Text-to-SQL pipeline."""
        from .text_to_sql_typed_runtime import install_text_to_sql_typed_runtime

        install_text_to_sql_typed_runtime(
            context,
            state_manager=self.state_manager,
            cancellation_checker=lambda: self._is_workflow_cancelled(
                context.workflow_id
            ),
            document_registry=self.document_registry,
        )

    async def _execute_tool_step(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        task: str,
    ) -> Any:
        runtime = self._exact_typed_runtime(context)
        if step.id == "schema_research" and step.tool_name == "typed_schema_research":
            if runtime is None:
                raise RuntimeError("Text-to-SQL Typed runtime is unavailable")
            from .text_to_sql_typed_research import run_typed_schema_research

            supervisor_evidence = getattr(context, "_supervisor_evidence", None)
            if supervisor_evidence is not None and not isinstance(
                supervisor_evidence,
                SupervisorExecutionEvidence,
            ):
                raise TypeError(
                    "context._supervisor_evidence must be a "
                    "SupervisorExecutionEvidence"
                )
            token = set_tool_runtime_context(
                {"supervisor_evidence": supervisor_evidence}
            )
            try:
                return await run_typed_schema_research(runtime)
            finally:
                reset_tool_runtime_context(token)
        if step.id == "db_audit" and runtime is not None:
            terminal = self._adaptive_solver_terminal(context)
            if terminal is not None:
                return terminal.to_mapping()
            if not self._has_authoritative_solver_gate(context):
                checkpoint = await asyncio.to_thread(
                    self._open_result_contradiction_checkpoint,
                    runtime,
                )
                if checkpoint is not None:
                    return await self._continue_result_contradiction_finalizer(
                        step,
                        context,
                        task,
                    )
                raise RuntimeError("adaptive solver has no terminal or ready candidate")
            return await self._execute_reserved_text_to_sql_finalizer(
                step,
                context,
                task,
            )
        if not self._requires_typed_execution_freshness(step, context):
            return await super()._execute_tool_step(step, context, task)
        if self._has_authoritative_solver_gate(context):
            return await self._execute_reserved_text_to_sql_finalizer(
                step,
                context,
                task,
            )
        return await self._execute_on_db_audit_tool_once(step, context, task)

    async def _execute_reserved_text_to_sql_finalizer(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        task: str,
    ) -> Any:
        from .text_to_sql_adaptive_solver import (
            apply_finalizer_checkpoint,
            prepare_finalizer_execution,
            reconcile_known_finalizer,
            reconcile_reserved_finalizer_unknown,
        )

        runtime = self._exact_typed_runtime(context)
        if runtime is None:
            raise RuntimeError("Typed finalizer runtime is unavailable")
        request = self._adaptive_finalizer_request(step, context)
        preparation = await _settle_thread_operation(
            prepare_finalizer_execution,
            runtime,
            request,
        )
        if preparation.error is not None or preparation.cancellation is not None:
            checkpoint, later_cancellation = await self._settle_adaptive_finalizer(
                runtime,
                reconcile_reserved_finalizer_unknown,
            )
            mapping = apply_finalizer_checkpoint(runtime, checkpoint)
            control = preparation.cancellation or later_cancellation
            if control is not None:
                raise control
            if preparation.error is not None:
                raise preparation.error
            return mapping
        prepared = preparation.value
        if prepared.terminal is not None:
            return prepared.terminal.to_mapping()
        reservation = prepared.reservation
        if reservation is None:
            raise RuntimeError("adaptive finalizer reservation is missing")
        try:
            result = await self._execute_on_db_audit_tool_once(step, context, task)
        except (asyncio.CancelledError, WorkflowDeadlineExceeded) as control:
            checkpoint, later_cancellation = await self._settle_adaptive_finalizer(
                runtime,
                reconcile_reserved_finalizer_unknown,
            )
            apply_finalizer_checkpoint(runtime, checkpoint)
            raise later_cancellation or control
        except Exception:
            checkpoint, cancellation = await self._settle_adaptive_finalizer(
                runtime,
                reconcile_reserved_finalizer_unknown,
            )
            mapping = apply_finalizer_checkpoint(runtime, checkpoint)
            if cancellation is not None:
                raise cancellation
            return mapping
        reconciliation = await _settle_thread_operation(
            reconcile_known_finalizer,
            runtime.solver_checkpoint_store,
            reservation,
            prepared.state,
            result,
        )
        if reconciliation.error is not None or reconciliation.cancellation is not None:
            checkpoint, later_cancellation = await self._settle_adaptive_finalizer(
                runtime,
                reconcile_reserved_finalizer_unknown,
            )
            mapping = apply_finalizer_checkpoint(runtime, checkpoint)
            control = reconciliation.cancellation or later_cancellation
            if control is not None:
                raise control
            return mapping
        if reconciliation.value.terminal is None:
            checkpoint = await asyncio.to_thread(
                self._open_result_contradiction_checkpoint,
                runtime,
            )
            if checkpoint is not None:
                return await self._continue_result_contradiction_finalizer(
                    step,
                    context,
                    task,
                )
        return apply_finalizer_checkpoint(runtime, reconciliation.value)

    @staticmethod
    def _open_result_contradiction_checkpoint(runtime):
        from custom_tools.text_to_sql.adaptive.result_review import ResultReviewReceipt
        from custom_tools.text_to_sql.adaptive.result_validation import ResultContradictionReceipt
        from custom_tools.text_to_sql.adaptive.serialization import canonical_json_bytes
        from .adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore

        store = getattr(runtime, "solver_checkpoint_store", None)
        run_id = getattr(runtime, "run_id", None)
        run_incarnation = getattr(runtime, "run_incarnation", None)
        if (
            type(store) is not AdaptiveSolverCheckpointStore
            or type(run_id) is not str
            or type(run_incarnation) is not str
        ):
            return None
        checkpoint = store.load(run_id, run_incarnation)
        if (
            checkpoint is None
            or checkpoint.terminal is not None
            or checkpoint.pending_execution is not None
        ):
            return None
        chain = store.load_replay_chain(run_id, run_incarnation)
        if (
            chain is None
            or chain.run_id != run_id
            or chain.run_incarnation != run_incarnation
            or chain.state_revision != checkpoint.state.revision
            or chain.state_digest != checkpoint.cursor.state_digest
            or chain.next_action_revision != checkpoint.cursor.next_action_revision
            or chain.pending_execution_action_revision is not None
            or chain.terminal is not None
            or not chain.actions
        ):
            return None
        action = chain.actions[-1]
        if action.action_kind != "execution":
            return None
        reconciliations = tuple(
            item
            for item in chain.reconciliations
            if item.action_revision == action.action_revision
            and item.outcome == "KNOWN"
            and item.result_state_revision == checkpoint.state.revision
            and item.result_state_digest == checkpoint.cursor.state_digest
        )
        if len(reconciliations) != 1:
            return None
        try:
            result = reconciliations[0].result
            if result.get("record_kind") == "text2sql_result_contradiction":
                receipt = ResultContradictionReceipt.model_validate_json(
                    canonical_json_bytes(result)
                )
            elif result.get("record_kind") == "text2sql_result_review":
                receipt = ResultReviewReceipt.model_validate_json(
                    canonical_json_bytes(result)
                )
            else:
                return None
        except (TypeError, ValueError):
            return None
        if (
            type(receipt) is ResultReviewReceipt
            and receipt.verdict not in {"contradicted", "ambiguous"}
        ):
            return None
        candidates = tuple(
            item
            for item in checkpoint.state.sql_candidates
            if item.candidate_id == receipt.candidate_id
        )
        if (
            receipt.run_id != run_id
            or receipt.run_incarnation != run_incarnation
            or len(candidates) != 1
            or candidates[0].revision != receipt.research_state_revision
            or candidates[0].normalized_ast_digest != receipt.normalized_ast_digest
            or action.candidate_id != receipt.candidate_id
            or action.normalized_ast_digest != receipt.normalized_ast_digest
        ):
            return None
        return checkpoint

    async def _continue_result_contradiction_finalizer(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        task: str,
    ) -> Any:
        import copy

        from custom_tools.text_to_sql.adaptive.models import ResearchState
        from .text_to_sql_typed_research import run_typed_schema_research

        runtime = self._exact_typed_runtime(context)
        if runtime is None:
            raise RuntimeError("Typed finalizer runtime is unavailable")
        if type(runtime.verified_research_state) is not ResearchState or (
            runtime.loaded_schema is None or runtime.verified_research_policy is None
        ):
            research = await run_typed_schema_research(runtime)
            if research.get("ready_for_sql") is not True:
                terminal = self._research_terminal_outcome(context)
                if terminal is not None:
                    return terminal.to_mapping()
                raise RuntimeError("Typed schema research did not become ready for SQL")
        local_solving_step = copy.copy(step)
        local_solving_step.metadata = dict(step.metadata)
        request = self._adaptive_finalizer_request(step, context)
        local_solving_step.metadata["max_rows"] = request["row_limit"]
        generation = await self._execute_typed_sql_solving(
            local_solving_step,
            context,
            runtime,
        )
        terminal = self._adaptive_solver_terminal(context)
        if terminal is not None:
            return terminal.to_mapping()
        sql_query = generation.get("sql") if isinstance(generation, dict) else None
        if type(sql_query) is not str or not sql_query:
            raise RuntimeError("adaptive solver did not return a ready SQL candidate")
        local_finalizer_step = copy.copy(step)
        local_finalizer_step.tool_params = dict(step.tool_params)
        local_finalizer_step.tool_params["sql_query"] = sql_query
        return await self._execute_reserved_text_to_sql_finalizer(
            local_finalizer_step,
            context,
            task,
        )

    @staticmethod
    async def _settle_adaptive_finalizer(runtime, reconcile_unknown):
        """Reload the exact checkpoint, then close only a still-pending execution."""

        loaded = await _settle_thread_operation(
            runtime.solver_checkpoint_store.load,
            runtime.run_id,
            runtime.run_incarnation,
        )
        if loaded.error is not None:
            raise loaded.error
        checkpoint = loaded.value
        if checkpoint is None:
            raise RuntimeError("adaptive finalizer checkpoint disappeared")
        if checkpoint.terminal is not None:
            return checkpoint, loaded.cancellation
        reservation = checkpoint.pending_execution
        if reservation is None:
            raise RuntimeError("adaptive finalizer has no durable settlement")
        reconciled = await _settle_thread_operation(
            reconcile_unknown,
            runtime.solver_checkpoint_store,
            reservation,
            checkpoint.state,
        )
        if reconciled.error is not None:
            raise reconciled.error
        return reconciled.value, loaded.cancellation or reconciled.cancellation

    def _adaptive_finalizer_request(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
    ) -> dict[str, object]:
        parameters = self._substitute_variables_in_params(
            step.tool_params.copy(),
            self._collect_context_variables(context),
        )
        return {
            "operation": "finalize_text_to_sql_run",
            "sql_query": parameters.get("sql_query"),
            "row_limit": parameters.get("row_limit"),
            "dry_run_only": parameters.get("dry_run_only"),
        }

    async def _execute_on_db_audit_tool_once(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        task: str,
    ) -> Any:
        """Run tools as before, adding W4-04 only in the db_audit worker thread."""

        logger.info(
            "🔧 Выполняем шаг '%s' с инструментом '%s' в thread pool",
            step.id,
            step.tool_name,
        )
        logger.info("📋 Задача: %s", _redact_workflow_log_value(task))
        logger.info(
            "📋 Параметры инструмента (до подстановки): %s",
            _redact_workflow_log_value(step.tool_params),
        )
        tool_function = self.factory._create_tool(step.tool_name)
        if tool_function is None:
            logger.error("❌ Инструмент '%s' не найден в tool_mapping", step.tool_name)
            raise ValueError(f"Инструмент '{step.tool_name}' не найден")

        tool_params = step.tool_params.copy()
        all_variables = self._collect_context_variables(context)
        if all_variables:
            try:
                tool_params = self._substitute_variables_in_params(
                    tool_params,
                    all_variables,
                )
            except Exception as exc:
                logger.warning(
                    "⚠️ Ошибка подстановки параметров инструмента '%s': %s",
                    step.id,
                    exc,
                )
        session_id = tool_params.pop("session_id", context.session_id)
        workflow_run_id = str(context.variables.get("run_id") or context.workflow_id)

        def _execute_tool_sync() -> Any:
            runtime_metadata = dict(step.metadata or {})
            deadline = getattr(context, "_deadline_budget", None)
            if deadline is not None:
                from .deadline import DeadlineBudget

                if not isinstance(deadline, DeadlineBudget):
                    raise TypeError("context._deadline_budget must be a DeadlineBudget")
                runtime_metadata["deadline_budget"] = deadline
            supervisor_evidence = getattr(context, "_supervisor_evidence", None)
            if supervisor_evidence is not None and not isinstance(
                supervisor_evidence,
                SupervisorExecutionEvidence,
            ):
                raise TypeError(
                    "context._supervisor_evidence must be a SupervisorExecutionEvidence"
                )
            runtime_metadata["supervisor_evidence"] = supervisor_evidence
            self._install_pre_execution_gate_runtime_metadata(
                runtime_metadata,
                getattr(context, "_text_to_sql_typed_runtime", None),
                sql_query=tool_params.get("sql_query"),
                executor_dsn=tool_params.get("dsn"),
            )
            self._install_result_validation_runtime_metadata(
                runtime_metadata,
                getattr(context, "_text_to_sql_typed_runtime", None),
                sql_query=tool_params.get("sql_query"),
                executor_dsn=tool_params.get("dsn"),
            )
            self._install_result_review_runtime_metadata(
                runtime_metadata,
                getattr(context, "_text_to_sql_typed_runtime", None),
                sql_query=tool_params.get("sql_query"),
                executor_dsn=tool_params.get("dsn"),
            )
            token = set_tool_runtime_context(runtime_metadata)
            try:
                from tool_manager import get_tool_manager

                result = get_tool_manager().run_tool(
                    tool_name=step.tool_name,
                    tool_function=tool_function,
                    task_description=task,
                    session_id=session_id,
                    workflow_run_id=workflow_run_id,
                    **tool_params,
                )
                self.resource_manager.record_api_call(context.workflow_id)
                return result
            finally:
                reset_tool_runtime_context(token)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _execute_tool_sync)

    @staticmethod
    def _requires_typed_execution_freshness(
        step: WorkflowStep,
        context: WorkflowContext,
    ) -> bool:
        if step.id != "db_audit":
            return False
        return EnhancedWorkflowEngine._has_authoritative_typed_gate(context)

    @staticmethod
    def _has_authoritative_typed_gate(context: WorkflowContext) -> bool:
        runtime = getattr(context, "_text_to_sql_typed_runtime", None)
        if runtime is None:
            return False
        from custom_tools.text_to_sql.adaptive.models import ResearchStopReason
        from .text_to_sql_typed_runtime import TextToSqlTypedRuntime

        return bool(
            isinstance(runtime, TextToSqlTypedRuntime)
            and getattr(runtime.verified_research_outcome, "stop_reason", None)
            is ResearchStopReason.COMPLETE
        )

    @staticmethod
    def _exact_typed_runtime(context: WorkflowContext):
        runtime = getattr(context, "_text_to_sql_typed_runtime", None)
        if runtime is None:
            return None
        from .text_to_sql_typed_runtime import (
            TextToSqlTypedRuntime,
            _ADMISSION_CAPABILITY,
        )

        if (
            type(runtime) is TextToSqlTypedRuntime
            and runtime._capability is _ADMISSION_CAPABILITY
        ):
            return runtime
        return None

    @staticmethod
    def _adaptive_solver_terminal(
        context: WorkflowContext,
    ) -> TextToSqlTerminalResult | None:
        runtime = EnhancedWorkflowEngine._exact_typed_runtime(context)
        terminal = getattr(runtime, "verified_solver_terminal", None)
        if type(terminal) is TextToSqlTerminalResult:
            return terminal
        return None

    @staticmethod
    def _has_authoritative_solver_gate(context: WorkflowContext) -> bool:
        runtime = EnhancedWorkflowEngine._exact_typed_runtime(context)
        if runtime is None:
            return False
        from custom_tools.text_to_sql.adaptive.models import SolverState
        from .adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore

        return bool(
            type(runtime.solver_checkpoint_store) is AdaptiveSolverCheckpointStore
            and type(runtime.verified_solver_state) is SolverState
            and isinstance(runtime.verified_solver_candidate_id, str)
            and runtime.verified_solver_candidate_id
        )

    @staticmethod
    def _install_pre_execution_gate_runtime_metadata(
        metadata: dict[str, object],
        runtime: object,
        *,
        sql_query: object,
        executor_dsn: object,
    ) -> None:
        from custom_tools.text_to_sql.adaptive.pre_execution_gate import (
            PRE_EXECUTION_GATE_REQUIRED_RUNTIME_KEY,
            PRE_EXECUTION_GATE_RUNTIME_KEY,
        )
        from custom_tools.text_to_sql.adaptive.pre_execution_gate_runtime import (
            INVALID_PRE_EXECUTION_GATE_RUNTIME,
            build_pre_execution_gate_runtime,
        )

        metadata[PRE_EXECUTION_GATE_REQUIRED_RUNTIME_KEY] = True
        runtime_dsn = getattr(runtime, "dsn", None)
        if (
            type(executor_dsn) is not str
            or not executor_dsn.strip()
            or type(runtime_dsn) is not str
            or executor_dsn != runtime_dsn
        ):
            metadata[PRE_EXECUTION_GATE_RUNTIME_KEY] = (
                INVALID_PRE_EXECUTION_GATE_RUNTIME
            )
            return
        metadata[PRE_EXECUTION_GATE_RUNTIME_KEY] = build_pre_execution_gate_runtime(
            runtime,
            sql_query=sql_query,
        )

    @staticmethod
    def _install_result_validation_runtime_metadata(
        metadata: dict[str, object],
        runtime: object,
        *,
        sql_query: object,
        executor_dsn: object,
    ) -> None:
        from custom_tools.text_to_sql.adaptive.result_validation import (
            RESULT_VALIDATION_RUNTIME_KEY,
        )
        from custom_tools.text_to_sql.adaptive.result_validation_runtime import (
            INVALID_RESULT_VALIDATION_RUNTIME,
            build_result_validation_runtime,
        )
        from .text_to_sql_typed_runtime import (
            TextToSqlTypedRuntime,
            _ADMISSION_CAPABILITY,
        )

        if (
            type(runtime) is not TextToSqlTypedRuntime
            or runtime._capability is not _ADMISSION_CAPABILITY
        ):
            return
        runtime_dsn = runtime.dsn
        if (
            type(executor_dsn) is not str
            or not executor_dsn.strip()
            or type(runtime_dsn) is not str
            or executor_dsn != runtime_dsn
        ):
            metadata[RESULT_VALIDATION_RUNTIME_KEY] = (
                INVALID_RESULT_VALIDATION_RUNTIME
            )
            return
        metadata[RESULT_VALIDATION_RUNTIME_KEY] = build_result_validation_runtime(
            runtime,
            sql_query=sql_query,
        )

    @staticmethod
    def _install_result_review_runtime_metadata(
        metadata: dict[str, object],
        runtime: object,
        *,
        sql_query: object,
        executor_dsn: object,
    ) -> None:
        from custom_tools.text_to_sql.adaptive.result_review import (
            RESULT_REVIEW_REQUIRED_RUNTIME_KEY,
            RESULT_REVIEW_RUNTIME_KEY,
        )
        from custom_tools.text_to_sql.adaptive.result_review_runtime import (
            INVALID_RESULT_REVIEW_RUNTIME,
            build_result_review_runtime,
        )
        from .text_to_sql_typed_runtime import (
            TextToSqlTypedRuntime,
            _ADMISSION_CAPABILITY,
        )

        if (
            type(runtime) is not TextToSqlTypedRuntime
            or runtime._capability is not _ADMISSION_CAPABILITY
        ):
            return
        metadata[RESULT_REVIEW_REQUIRED_RUNTIME_KEY] = True
        if (
            type(executor_dsn) is not str
            or not executor_dsn.strip()
            or executor_dsn != runtime.dsn
        ):
            metadata[RESULT_REVIEW_RUNTIME_KEY] = INVALID_RESULT_REVIEW_RUNTIME
            return
        metadata[RESULT_REVIEW_RUNTIME_KEY] = build_result_review_runtime(
            runtime,
            sql_query=sql_query,
        )

    async def _execute_enhanced_steps_parallel(
        self,
        workflow_def: WorkflowDefinition,
        context: WorkflowContext,
        *,
        skip_steps: Optional[Set[str]] = None,
        restored_step_results: Optional[Dict[str, StepResult]] = None,
    ) -> Dict[str, StepResult]:
        """Параллельное выполнение с enhanced логикой"""
        from .orchestration.parallel_executor import ParallelWorkflowExecutor

        # Сохраняем workflow_definition как временный атрибут для параллельных задач
        context._workflow_definition = workflow_def

        parallel_executor = ParallelWorkflowExecutor(
            max_concurrent=workflow_def.max_parallel_steps
        )

        logger.info(f"🚀 Enhanced: Начинаем параллельное выполнение с enhanced логикой")

        # Resume: восстановленные шаги передаём в initial_step_results — их ключи
        # попадают в completed_steps исполнителя, поэтому они не запускаются повторно.
        async def execute_step(step, step_context):
            return await self._execute_enhanced_step_wrapper(
                step,
                step_context,
            )

        step_results = await parallel_executor.execute_steps_parallel(
            workflow_def.steps,
            context,
            step_executor=execute_step,
            dependency_checker=self._check_step_dependencies,
            condition_checker=lambda step, step_context: bool(
                skip_steps is not None and step.id in skip_steps
            )
            or self._should_skip_step_by_condition(step, step_context),
            stop_checker=lambda: self._should_stop_enhanced_steps(context),
            stop_on_failure=(
                workflow_def.error_handling.get("on_failure", "continue") != "continue"
            ),
            initial_step_results=restored_step_results,
        )
        return step_results

    async def _should_stop_enhanced_steps(self, context: WorkflowContext) -> bool:
        if self._adaptive_solver_terminal(context) is not None:
            return True
        return await self._is_workflow_cancelled(context.workflow_id)

    async def _execute_enhanced_step_wrapper(
        self,
        step,
        context,
    ):
        """Enhanced обертка для выполнения шага с дополнительной логикой"""
        step_results = getattr(context, "_workflow_step_results", {})
        step_result = await self._execute_enhanced_step(
            step, context, step_results
        )

        # Обрабатываем события шага с enhanced логикой
        if _step_status_value(step_result.status) == StepStatus.COMPLETED.value:
            workflow_def = getattr(context, "_workflow_definition", None)
            if workflow_def is not None:
                step_results[step.id] = step_result
                step_result = await self._complete_enhanced_step_with_output_retry(
                    step, step_result, context, workflow_def, step_results
                )
                step_results[step.id] = step_result
            else:
                step_results[step.id] = step_result
                self._require_deadline(context, f"step '{step.id}' checkpoint")
                await self._on_step_completed(
                    context.workflow_id, step, step_result, context, step_results
                )
        elif step_result.status == StepStatus.FAILED:
            logger.error(f"❌ Enhanced: Шаг {step.id} провален: {step_result.error}")
            if step.rollback_action:
                await self._execute_rollback(step.rollback_action, context, step_result)
            # В Enhanced режиме можем продолжить или прервать в зависимости от политики
            # TODO: Добавить политику обработки ошибок

        return step_result

    async def _execute_enhanced_step(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        previous_results: Dict[str, StepResult],
    ) -> StepResult:
        """Выполнение одного шага с enhanced логикой включая resilience"""
        self._require_deadline(context, f"step '{step.id}' start")
        step_start_time = datetime.now()

        # Создаем бюджет для шага
        budget_limits = None
        if (
            step.resource_limits is not None
            and step.resource_limits.max_duration_seconds is not None
        ):
            budget_limits = {
                BudgetType.TIME: step.resource_limits.max_duration_seconds,
            }
        if budget_limits is None:
            step_budget = self.budget_manager.create_step_budget(step.id)
        else:
            step_budget = self.budget_manager.create_step_budget(step.id, budget_limits)

        # Проверяем доступность агента через circuit breaker
        if not self.circuit_breaker_manager.is_agent_available(step.agent_type):
            logger.error(
                f"🚫 Agent '{step.agent_type}' unavailable due to circuit breaker"
            )
            end_time = datetime.now()
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                start_time=step_start_time,
                end_time=end_time,
                error=f"Agent {step.agent_type} circuit breaker is OPEN",
                error_class="circuit_breaker_open",
                # W0-0.3: заполняем и для этого короткого замыкания — иначе
                # наблюдаемость дырявая именно там, где она нужнее всего.
                resource_usage={
                    "duration_seconds": (end_time - step_start_time).total_seconds()
                },
            )

        # Проверяем зацикливание
        is_loop, loop_pattern = self.loop_detector.is_step_in_loop(
            context.workflow_id, step.id
        )
        if is_loop:
            suggestion = self.loop_detector.get_loop_prevention_suggestion(
                context.workflow_id, step.id
            )
            logger.error(f"🔄 Loop detected for step '{step.id}': {suggestion}")
            end_time = datetime.now()
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                start_time=step_start_time,
                end_time=end_time,
                error=f"Loop detected: {suggestion}",
                error_class="loop_detected",
                resource_usage={
                    "duration_seconds": (end_time - step_start_time).total_seconds()
                },
            )

        retry_context = {
            "step": step,
            "workflow_context": context,
            "previous_results": previous_results,
            "step_budget": step_budget,
        }

        # H-2: уважаем retry-политику из YAML (step.retry_policy > global_retry_policy >
        # дефолт), а не хардкод 3/1.0/30.0. workflow_def доступен через context
        # (публикуется sequential/parallel исполнителями перед диспетчеризацией).
        workflow_def = getattr(context, "_workflow_definition", None)
        policy = (
            step.retry_policy
            or (workflow_def.global_retry_policy if workflow_def is not None else None)
            or RetryPolicy()
        )
        # M-1: auto_retry_transient=false отключает движковый retry (0 доп. попыток).
        auto_retry_transient = True
        if workflow_def is not None:
            auto_retry_transient = self._coerce_bool(
                workflow_def.error_handling.get("auto_retry_transient", True), True
            )
        effective_max_retries = policy.max_retries if auto_retry_transient else 0

        # Выполняем с adaptive retry, кроме явно non-retryable шагов с side effects.
        try:
            if self._is_enhanced_step_retryable(step):
                step_result = await self.retry_engine.execute_with_retry(
                    step_id=step.id,
                    step_func=self._execute_single_step_attempt,
                    context=retry_context,
                    max_retries=effective_max_retries,
                    base_delay=policy.base_delay,
                    max_delay=policy.max_delay,
                    backoff_multiplier=1.5,
                    retry_on_errors=policy.retry_on_errors,
                    attempt_timeout=step.timeout,
                    deadline=self._context_deadline(context),
                )
            else:
                step_result = await self._execute_non_retryable_attempt(
                    step,
                    retry_context,
                    context,
                )

            # Записываем выполнение в loop detector
            execution_data = {
                "task": step.task,
                "output": step_result.output,
                "error": step_result.error,
                "quality_score": getattr(step_result, "quality_score", 0.0),
                "decision": getattr(step_result, "decision", ""),
                "retry_count": getattr(step_result, "retry_count", 0),
            }

            loop_detected = self.loop_detector.record_step_execution(
                context.workflow_id, step.id, execution_data
            )

            if loop_detected:
                logger.warning(f"🔄 New loop pattern detected for step '{step.id}'")

            return step_result

        except WorkflowDeadlineExceeded:
            raise
        except Exception as e:
            logger.error(f"❌ Enhanced step execution failed: {e}")
            end_time = datetime.now()
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                start_time=step_start_time,
                end_time=end_time,
                error=str(e),
                error_class="execution_error",
                resource_usage={
                    "duration_seconds": (end_time - step_start_time).total_seconds()
                },
            )

    async def _execute_non_retryable_attempt(
        self,
        step: WorkflowStep,
        retry_context: Dict[str, Any],
        workflow_context: WorkflowContext,
    ) -> StepResult:
        # T13b: тонкая обёртка над общим helper'ом workflow.deadline.execute_step_attempt
        # (раньше дублировался здесь и в AdaptiveRetryEngine._execute_attempt).
        # Побочный эффект ужесточения: step.timeout теперь валидируется
        # (validate_attempt_timeout) даже при deadline=None — раньше в этом
        # non-retryable пути timeout при отсутствующем deadline не валидировался.
        return await execute_step_attempt(
            step.id,
            self._execute_single_step_attempt,
            retry_context,
            attempt_timeout=step.timeout,
            deadline=self._context_deadline(workflow_context),
        )

    @staticmethod
    def _is_enhanced_step_retryable(step: WorkflowStep) -> bool:
        return (step.metadata or {}).get("retryable", True) is not False

    async def _check_result_cache(
        self, step: WorkflowStep, context: WorkflowContext
    ) -> Optional[StepResult]:
        """Проверить кэш результатов"""

        if not self.feature_manager.is_feature_enabled(
            "result_cache", context.workflow_id, step.id
        ):
            return None

        cached_result = self.cache_manager.result_cache.get_cached_result(
            step.agent_type, step.task, context.variables or {}
        )

        if cached_result:
            logger.info(f"⚡ Using cached result for step '{step.id}'")
            return cached_result

        return None

    async def _execute_with_alternatives(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        previous_results: Dict[str, StepResult],
    ) -> StepResult:
        """Выполнить шаг с альтернативными стратегиями"""

        # Проверяем есть ли альтернативы в метаданных шага
        alternatives = step.metadata.get("alternatives", [])

        if not alternatives or not self.feature_manager.is_feature_enabled(
            "alternative_execution", context.workflow_id
        ):
            # Обычное выполнение
            return await self._execute_enhanced_step(step, context, previous_results)

        logger.info(
            f"🔀 Executing step '{step.id}' with {len(alternatives)} alternatives"
        )

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

    async def _predict_and_optimize(
        self, workflow_definition: WorkflowDefinition, context: WorkflowContext
    ) -> Dict[str, Any]:
        """Предсказать производительность и предложить оптимизации"""

        # Предсказание производительности
        performance_prediction = self.performance_optimizer.predict_performance(
            workflow_definition.__dict__, context.__dict__
        )

        # Предложения по оптимизации
        optimizations = self.performance_optimizer.suggest_optimizations(
            workflow_definition.__dict__
        )

        logger.info(
            f"📊 Workflow prediction: {performance_prediction.estimated_duration:.1f}s, "
            f"${performance_prediction.estimated_cost:.2f}, "
            f"{len(optimizations)} optimization suggestions"
        )

        return {
            "performance_prediction": performance_prediction,
            "optimization_suggestions": optimizations,
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
        if self.feature_manager.is_feature_enabled(
            "pre_step_planner", workflow_context.workflow_id, step.id
        ):
            plan = await self.planner.plan_step(
                step, workflow_context, previous_results
            )
            logger.info(f"📋 Created execution plan for step '{step.id}'")

        # Выполняем шаг через circuit breaker
        try:
            step_result = await self.circuit_breaker_manager.call_agent_safely(
                agent_name=step.agent_type,
                agent_func=self._execute_step_with_policy,
                step=step,
                context=workflow_context,
                plan=plan,
                attempt=1,
            )

            # Потребляем бюджет времени
            duration = (datetime.now() - attempt_start).total_seconds()
            self.budget_manager.consume_budget(
                step.id, BudgetType.TIME, duration, "step", f"Step execution time"
            )
            if _step_status_value(step_result.status) == StepStatus.FAILED.value:
                return step_result

            # Post-step validation and decision.
            # M-4: LLM-судья валидирует output против text-плана и на детерминированных
            # tool-шагах даёт ложные RETRY/STOP (валидный dict/путь оценивается как «не text»).
            # Судим только НЕ-tool (агентные) шаги.
            if step.step_type == "tool":
                # M-4: детерминированный tool-шаг НЕ судим LLM-судьёй (он даёт
                # ложные RETRY/STOP: валидный dict/путь оценивается как «не text»).
                # Успешно завершённый tool-шаг детерминирован → присваиваем полную
                # оценку качества. Без этого дефолтный quality_score=0.0 будет
                # отвергнут gate'ом приемлемости в execute_with_retry
                # (_is_result_acceptable: quality_score<0.3 → «poor quality» →
                # ложный провал детерминированного шага после ретраев).
                step_result.quality_score = 1.0
            elif self.feature_manager.is_feature_enabled(
                "post_step_judge", workflow_context.workflow_id, step.id
            ):
                default_plan = StepPlan(
                    step_id=step.id,
                    refined_task=step.task,
                    expected_output_format="text",
                    quality_criteria={"min_score": 0.7},
                    resource_budget={},
                    timeout_seconds=300,
                    retry_budget=3,
                )

                validation_result = await self.judge.validate_result(
                    step_result, plan or default_plan, step
                )

                # Принимаем решение
                decision = await self.decision_engine.make_decision(
                    validation_result,
                    step_result,
                    step,
                    workflow_context,
                    list(previous_results.values()),
                )

                # Обновляем результат с информацией о решении
                step_result.quality_score = validation_result.overall_score
                step_result.decision = decision.action
                step_result.decision_reason = decision.reason
                step_result.validator_results = validation_result.validator_results

                logger.info(
                    f"⚖️ Step '{step.id}' validation: score={validation_result.overall_score:.2f}, "
                    f"decision={decision.action}"
                )

                # Если решение не "proceed", выбрасываем исключение для retry.
                # JudgeRetryRequested помечает вердикт судьи как повтор ПО КАЧЕСТВУ,
                # чтобы execute_with_retry не отсекал его retry_on_errors-фильтром
                # (класс "unknown_error") и ретраил как прежде — иначе агентный шаг
                # валится без единой повторной попытки (регресс H-2).
                if decision.action != "proceed":
                    raise JudgeRetryRequested(
                        f"Decision: {decision.action} - {decision.reason}"
                    )

            step_result.status = StepStatus.COMPLETED
            return step_result

        except Exception as e:
            # Потребляем бюджет даже при ошибке
            duration = (datetime.now() - attempt_start).total_seconds()
            self.budget_manager.consume_budget(
                step.id,
                BudgetType.TIME,
                duration,
                "step",
                f"Failed step execution time",
            )
            raise

    async def _execute_step_with_policy(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        plan: Optional[StepPlan],
        attempt: int,
    ) -> StepResult:
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

                valid_terminal_result = False
                if step.tool_name == "finalize_text_to_sql_run":
                    try:
                        terminal_result = TextToSqlTerminalResult.from_mapping(result)
                        result = terminal_result.to_mapping()
                        valid_terminal_result = True
                    except (TypeError, ValueError) as exc:
                        if self._is_tool_error_result(result):
                            error_msg = self._extract_error_from_result(result)
                        else:
                            error_msg = f"invalid terminal result: {exc}"
                        raise RuntimeError(
                            f"Инструмент {step.tool_name} завершился "
                            f"с ошибкой: {error_msg}"
                        ) from exc

                # Проверяем результат инструмента на наличие ошибок
                # Если tool_manager перехватил исключение и вернул ошибку как строку
                if not valid_terminal_result and self._is_tool_error_result(result):
                    error_msg = self._extract_error_from_result(result)
                    raise RuntimeError(
                        f"Инструмент {step.tool_name} завершился с ошибкой: {error_msg}"
                    )

            else:
                # Выполнение через агента с enhanced логикой
                result = await self._execute_enhanced_agent_step(
                    step, context, task, plan, budget
                )

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
                agent_name=step.agent_type,
                # W0-0.3: duration дублируется здесь намеренно — generic-потребители
                # (например workflow/intelligence/aggregator.py) читают метрики через
                # resource_usage по path-маппингу, а не через top-level поля StepResult.
                resource_usage={"duration_seconds": duration},
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
                agent_name=step.agent_type,
                # W0-0.3: см. комментарий в success-ветке выше — дублирование
                # duration_seconds для generic path-маппинга через resource_usage.
                resource_usage={"duration_seconds": duration},
            )

    def _apply_decision_modifications(
        self, step: WorkflowStep, modifications: Dict[str, Any]
    ) -> WorkflowStep:
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
            modified.task = (
                step.task
                + "\n\nДополнительные требования: предоставьте подробное обоснование и примеры."
            )

        if modifications.get("add_format_examples"):
            modified.task = (
                modified.task
                + "\n\nПример желаемого формата ответа: структурированный текст с четкими разделами."
            )

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
                "operators": self.conditional_engine.get_supported_operators(),
            },
            # Monitoring stats
            "metrics_summary": self.metrics_collector.get_metrics_summary(),
            "alerts_summary": self.alert_manager.get_alerts_summary(),
            "analytics_summary": self.analytics_engine.get_analytics_summary(),
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
                "retry_success_rate": self.metrics_collector.workflow_metrics.get_retry_success_rate(),
            }

            self.alert_manager.evaluate_rules(metrics_values)

            # Запускаем аналитику
            insights = self.analytics_engine.analyze_workflow_performance(
                metrics_values
            )

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
            metrics_data.update(
                {
                    "workflow_success_rate": self.metrics_collector.workflow_metrics.get_success_rate(),
                    "avg_workflow_duration": self.metrics_collector.workflow_metrics.avg_workflow_duration,
                    "avg_quality_score": self.metrics_collector.workflow_metrics.avg_quality_score,
                    "cache_hit_rate": self.metrics_collector.workflow_metrics.get_cache_hit_rate(),
                    "total_cost": self.metrics_collector.workflow_metrics.total_cost,
                    "workflow_executions_total": self.metrics_collector.workflow_metrics.workflow_executions_total,
                    "circuit_breaker_opens": self.metrics_collector.workflow_metrics.circuit_breaker_opens,
                    "retry_success_rate": self.metrics_collector.workflow_metrics.get_retry_success_rate(),
                }
            )

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
                "total_tokens": self.metrics_collector.workflow_metrics.total_tokens,
            }

            return self.report_builder.generate_report(report_type, data)

        except Exception as e:
            logger.error(f"❌ Error generating report: {e}")
            return {"error": str(e)}

    def record_step_metrics(
        self,
        step_id: str,
        agent_type: str,
        duration: float,
        success: bool,
        retry_count: int = 0,
        quality_score: float = None,
    ):
        """Записать метрики выполнения шага"""

        self.metrics_collector.record_step_execution(
            step_id, agent_type, duration, success, retry_count, quality_score
        )

        # Записываем события в других компонентах
        if retry_count > 0:
            self.metrics_collector.record_resource_usage(
                cost=retry_count * 0.01
            )  # Примерная стоимость retry

        if quality_score is not None and quality_score < 0.7:
            self.metrics_collector.workflow_metrics.quality_below_threshold_count += 1

    async def _execute_enhanced_agent_step(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        task: str,
        plan: Optional[StepPlan],
        budget: ResourceLimits,
    ) -> Any:
        """Выполнение шага через агента в enhanced режиме с дополнительной логикой"""
        runtime = self._exact_typed_runtime(context)
        if runtime is not None:
            if step.id == "sql_solving":
                return await self._execute_typed_sql_solving(
                    step,
                    context,
                    runtime,
                )
        # Специальная обработка для менеджера с предзагрузкой агентов
        if (
            step.agent_type == "manager"
            and step.metadata
            and step.metadata.get("preload_agents")
        ):
            # Работаем на per-call копии step и его metadata, чтобы не мутировать
            # общий объект WorkflowStep (он может разделяться параллельными шагами
            # из workflow_def.steps). Прежний код писал pipeline_type прямо в
            # step.metadata и «восстанавливал» его — это гонка при конкурентных шагах.
            import copy

            local_step = copy.copy(step)
            local_step.metadata = dict(step.metadata)
            local_step.metadata.setdefault("pipeline_type", "enhanced_workflow")

            return await self._execute_manager_with_preloaded_agents(
                local_step, context, task
            )
        else:
            logger.info(
                f"🤖 Delegating enhanced step '{step.id}' to parent with enhanced pipeline_type"
            )

            # Работаем на per-call копии, чтобы избежать гонки при параллельных шагах,
            # которые могут разделять один объект WorkflowStep из workflow_def.steps.
            import copy

            local_step = copy.copy(step)
            local_step._enhanced_pipeline_type = "enhanced_workflow"

            result = await super()._execute_agent_step(local_step, context, task)
            return result

    async def _execute_typed_sql_solving(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        runtime: object,
    ) -> dict[str, object]:
        from custom_tools.text_to_sql.validators import TextToSqlSafetyPolicy
        from .text_to_sql_typed_research import default_table_namespace
        from .text_to_sql_adaptive_solver import (
            run_production_adaptive_sql_generation,
        )

        metadata = step.metadata if isinstance(step.metadata, dict) else {}
        row_limit = metadata.get("max_rows")
        safety_policy = metadata.get("safety_policy")
        dry_run_only = context.variables.get("dry_run_only")
        if type(row_limit) is not int or row_limit <= 0:
            raise TypeError("Typed SQL solving requires max_rows")
        if type(dry_run_only) is not bool:
            raise TypeError("Typed SQL solving requires dry_run_only")
        if type(safety_policy) is not TextToSqlSafetyPolicy:
            raise TypeError("Typed SQL solving requires exact safety_policy")
        supervisor_evidence = getattr(context, "_supervisor_evidence", None)
        if supervisor_evidence is not None and not isinstance(
            supervisor_evidence,
            SupervisorExecutionEvidence,
        ):
            raise TypeError(
                "context._supervisor_evidence must be a SupervisorExecutionEvidence"
            )
        token = set_tool_runtime_context(
            {"supervisor_evidence": supervisor_evidence}
        )
        try:
            return await run_production_adaptive_sql_generation(
                runtime,
                safety_policy=safety_policy,
                row_limit=row_limit,
                dry_run_only=dry_run_only,
                table_namespace=default_table_namespace(runtime.dsn),
            )
        finally:
            reset_tool_runtime_context(token)

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
                "Traceback",
            ]

            result_lower = result.lower()
            for pattern in error_patterns:
                if pattern.lower() in result_lower:
                    return True

        # Если результат - словарь с полем error
        if isinstance(result, dict):
            # Проверяем истинность значения, а не наличие ключа: {"error": None}
            # или {"exception": ""} у успешного результата не должны трактоваться
            # как ошибка (иначе любой новый тул с декоративным error=None упадёт).
            if result.get("error") or result.get("exception"):
                return True
            if str(result.get("status", "")).strip().lower() == "error":
                return True

        return False

    def _extract_error_from_result(self, result: Any) -> str:
        """Извлекает сообщение об ошибке из результата инструмента."""
        if isinstance(result, str):
            return result
        elif isinstance(result, dict):
            return result.get(
                "error", result.get("exception", result.get("message", str(result)))
            )
        else:
            return str(result)
