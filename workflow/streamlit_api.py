"""
Публичные контракты для интеграции со Streamlit
==============================================

Этот модуль предоставляет стабильный API для Streamlit UI,
не изменяя существующую бизнес-логику workflow engine.
"""

import os
import logging
import uuid
import signal
import time
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Any, Optional, Callable, Tuple
from pathlib import Path
from dataclasses import dataclass
from contextlib import contextmanager
import threading
import json
import sys
import re
import hashlib
from collections.abc import Mapping

from tool_runtime_context import SupervisorExecutionEvidence

from ._result_common import _validate_private_claim
from .deadline import DeadlineBudget
from .text_to_sql_contract import (
    TEXT_TO_SQL_WORKFLOW_NAME,
    is_text_to_sql_workflow_name,
)
from .models import (
    WorkflowDefinition, WorkflowContext, WorkflowStatus,
    StepStatus, WorkflowExecutionError, TEXT_TO_SQL_WORKFLOW_CATEGORY,
    TextToSqlTerminalResult as TextToSqlTerminalResult,
)
from .result_repository import (
    WorkflowResultCollisionError as WorkflowResultCollisionError,
)
from backend.fastapi_app.agui.errors import WorkflowRunAlreadyReservedError
from custom_tools.text_to_sql.redaction import redact_pii_in_payload

if TYPE_CHECKING:
    from backend.fastapi_app.agui._t2s_requests import TextToSqlWorkflowAdmission

logger = logging.getLogger(__name__)


def _validate_private_workflow_claim(
    supervisor_id: Any,
    attempt_generation: Any,
) -> Tuple[Optional[str], Optional[int]]:
    return _validate_private_claim(supervisor_id, attempt_generation)


def _capture_active_yaml_config_versions() -> Dict[
    str,
    Dict[str, Optional[str]],
]:
    from custom_tools.text_to_sql._yaml_config_loader import (
        get_active_yaml_config_versions,
    )

    return get_active_yaml_config_versions()


def _setup_comprehensive_logging_from_env() -> None:
    try:
        from logging_setup import setup_comprehensive_logging
        log_level_str = os.getenv("SMOLAGENTS_LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, log_level_str, logging.INFO)
        setup_comprehensive_logging(log_level=log_level)
    except Exception:
        pass


def _setup_process_run_log_capture(run_id: str) -> None:
    try:
        from unified_logging import get_logging_manager
        get_logging_manager(logs_dir=str(Path(__file__).resolve().parents[1] / "logs"))
    except Exception:
        pass

    try:
        from unified_logging import RunIdLogHandler

        def _remove_run_log_handler(logger_instance: logging.Logger) -> None:
            for handler in list(logger_instance.handlers):
                if isinstance(handler, RunIdLogHandler):
                    logger_instance.removeHandler(handler)

        _remove_run_log_handler(logging.getLogger())
        for logger_instance in logging.Logger.manager.loggerDict.values():
            if isinstance(logger_instance, logging.Logger):
                _remove_run_log_handler(logger_instance)
    except Exception:
        pass

    try:
        log_dir = Path(__file__).resolve().parents[1] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{run_id}_logs.jsonl"
        log_lock = threading.Lock()

        original_stdout = sys.stdout
        original_stderr = sys.stderr

        ansi_re = re.compile(r"\x1b\[[0-9;]*[mK]")

        def _strip_ansi(text: str) -> str:
            return ansi_re.sub("", text)

        def _write_json_line(level: str, logger_name: str, message: str) -> None:
            if not message:
                return
            entry = {
                "run_id": run_id,
                "timestamp": datetime.now().isoformat(),
                "level": level,
                "logger_name": logger_name,
                "message": _redact_public_payload(_strip_ansi(message)),
            }
            with log_lock:
                with open(log_file, "a", encoding="utf-8") as handle:
                    json.dump(entry, handle, ensure_ascii=False, default=str)
                    handle.write("\n")

        class StreamToJsonl:
            def __init__(self, level: str, logger_name: str, stream):
                self.level = level
                self.logger_name = logger_name
                self.stream = stream
                self._buffer = ""

            def write(self, message):
                if not message:
                    return
                try:
                    self.stream.write(message)
                    self.stream.flush()
                except Exception:
                    pass
                self._buffer += message
                while "\n" in self._buffer:
                    line, self._buffer = self._buffer.split("\n", 1)
                    line = _strip_ansi(line.rstrip())
                    if line:
                        _write_json_line(self.level, self.logger_name, line)

            def flush(self):
                if self._buffer.strip():
                    _write_json_line(self.level, self.logger_name, self._buffer.strip())
                self._buffer = ""
                try:
                    self.stream.flush()
                except Exception:
                    pass

            def isatty(self):
                return bool(getattr(self.stream, "isatty", lambda: False)())

            def fileno(self):
                return self.stream.fileno()

        sys.stdout = StreamToJsonl("INFO", "workflow_stdout", original_stdout)
        sys.stderr = StreamToJsonl("WARNING", "workflow_stderr", original_stderr)

        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.stream = sys.stderr
        for logger_instance in logging.Logger.manager.loggerDict.values():
            if not isinstance(logger_instance, logging.Logger):
                continue
            for handler in logger_instance.handlers:
                if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                    handler.stream = sys.stderr
    except Exception:
        pass

# Глобальный реестр активных запусков (разделяемый между всеми экземплярами)
_GLOBAL_WORKFLOW_ACTIVE_RUNS = {}
_GLOBAL_WORKFLOW_RUN_CALLBACKS = {}
_GLOBAL_WORKFLOW_PROCESS_SUPERVISOR = None
_GLOBAL_WORKFLOW_PROCESS_SUPERVISOR_LOCK = threading.Lock()
_GLOBAL_WORKFLOW_ENV_LOCK = threading.Lock()
# RLock для _GLOBAL_WORKFLOW_ACTIVE_RUNS и _GLOBAL_WORKFLOW_RUN_CALLBACKS:
# parent/worker threads and UI callbacks share these compatibility projections.
_GLOBAL_WORKFLOW_RUNS_LOCK = threading.RLock()


def configure_workflow_process_supervisor(store):
    """Create the one parent supervisor used by all WorkflowManager instances."""
    global _GLOBAL_WORKFLOW_PROCESS_SUPERVISOR
    with _GLOBAL_WORKFLOW_PROCESS_SUPERVISOR_LOCK:
        if _GLOBAL_WORKFLOW_PROCESS_SUPERVISOR is None:
            from .process_supervisor import SupervisorConfig, WorkflowProcessSupervisor

            _GLOBAL_WORKFLOW_PROCESS_SUPERVISOR = WorkflowProcessSupervisor(
                store,
                process_entrypoint=_workflow_supervisor_process_entry,
                config=SupervisorConfig.from_environment(),
                terminalizer=_terminalize_supervised_workflow,
            )
        return _GLOBAL_WORKFLOW_PROCESS_SUPERVISOR


def _default_workflow_process_supervisor():
    global _GLOBAL_WORKFLOW_PROCESS_SUPERVISOR
    if _GLOBAL_WORKFLOW_PROCESS_SUPERVISOR is None:
        from backend.fastapi_app.agui.store import EventStore

        store = EventStore(str(_agui_event_store_path()))
        supervisor = configure_workflow_process_supervisor(store)
        supervisor.start()
    return _GLOBAL_WORKFLOW_PROCESS_SUPERVISOR


# Типы для callbacks
ProgressCallback = Callable[[str, str, Dict[str, Any]], None]  # (run_id, event_type, data)
LogCallback = Callable[[str, str, str, str], None]  # (run_id, level, message, timestamp)


@contextmanager
def _workflow_dsn_env(
    parameters: Dict[str, Any],
    *,
    workflow_name: Any,
):
    if not isinstance(parameters, dict):
        yield
        return

    # WONTFIX (аудит L4): рекомендация «отпускать лок на время yield» сознательно
    # ОТКЛОНЕНА — она ломает корректность. Держим мьютекс на всё время выполнения
    # (включая yield), чтобы гарантировать атомарность set→yield→restore для os.environ.
    # os.environ — process-global, а workflow исполняется ВНУТРИ этого процесса
    # (loop.run_until_complete у вызывающего) и читает DB_DSN/лимиты на ПРОТЯЖЕНИИ
    # всего исполнения. Если отпустить лок на время yield, конкурентный запуск перезапишет
    # эти переменные посреди работы текущего — поэтому одновременные in-process запуски
    # обязаны сериализоваться. Это намеренный bottleneck ради корректности, а не упущение.
    with _GLOBAL_WORKFLOW_ENV_LOCK:
        manage_text_to_sql_env = is_text_to_sql_workflow_name(workflow_name)
        dsn = parameters.get("dsn")
        row_limit = parameters.get("max_rows")
        has_dry_run_only = manage_text_to_sql_env and "dry_run_only" in parameters
        dry_run_only = parameters.get("dry_run_only")
        validate_schema = parameters.get("validate_schema")
        effective_dry_run = None
        if manage_text_to_sql_env:
            if dry_run_only is not None and type(dry_run_only) is not bool:
                raise ValueError("dry_run_only must be boolean")
            from custom_tools.text_to_sql.utils import is_dry_run_only
            effective_dry_run = is_dry_run_only(payload_flag=dry_run_only)
        previous_dsn = os.environ.get("DB_DSN")
        previous_row_limit = os.environ.get("DB_EXECUTOR_ROW_LIMIT")
        previous_dry_run = os.environ.get("TEXT_TO_SQL_DRY_RUN_ONLY")
        previous_validate_schema = os.environ.get("TEXT_TO_SQL_VALIDATE_SCHEMA")
        if dsn:
            os.environ["DB_DSN"] = str(dsn)
        if row_limit is not None:
            os.environ["DB_EXECUTOR_ROW_LIMIT"] = str(row_limit)
        if has_dry_run_only and dry_run_only is not None:
            os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] = (
                "true" if effective_dry_run else "false"
            )
        if validate_schema is not None:
            os.environ["TEXT_TO_SQL_VALIDATE_SCHEMA"] = str(validate_schema)
        try:
            yield
        finally:
            if dsn:
                if previous_dsn is None:
                    os.environ.pop("DB_DSN", None)
                else:
                    os.environ["DB_DSN"] = previous_dsn
            if row_limit is not None:
                if previous_row_limit is None:
                    os.environ.pop("DB_EXECUTOR_ROW_LIMIT", None)
                else:
                    os.environ["DB_EXECUTOR_ROW_LIMIT"] = previous_row_limit
            if has_dry_run_only and dry_run_only is not None:
                if previous_dry_run is None:
                    os.environ.pop("TEXT_TO_SQL_DRY_RUN_ONLY", None)
                else:
                    os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] = previous_dry_run
            if validate_schema is not None:
                if previous_validate_schema is None:
                    os.environ.pop("TEXT_TO_SQL_VALIDATE_SCHEMA", None)
                else:
                    os.environ["TEXT_TO_SQL_VALIDATE_SCHEMA"] = previous_validate_schema


# === Точка входа дочернего процесса для запуска workflow (должна быть на верхнем уровне для spawn) ===
def _workflow_supervisor_process_entry(
    run_id: str,
    work_spec: Dict[str, Any],
    claim_envelope: Mapping[str, Any],
) -> None:
    """Точка входа для выполнения workflow в отдельном процессе"""
    from .process_supervisor import validate_work_spec

    if not isinstance(claim_envelope, Mapping) or set(claim_envelope) != {
        "supervisor_id",
        "attempt_generation",
        "run_kind",
        "workflow_name",
    }:
        raise ValueError("claim_envelope has an invalid shape")
    supervisor_id, attempt_generation = _validate_private_workflow_claim(
        claim_envelope.get("supervisor_id"),
        claim_envelope.get("attempt_generation"),
    )
    if supervisor_id is None or attempt_generation is None:
        raise ValueError("spawned workflow requires a claimed attempt")
    spec = validate_work_spec(work_spec)
    workflow_path = str(spec["workflow_path"])
    parameters = dict(spec["parameters"])
    session_id = str(spec["session_id"])
    client_id = spec["client_id"]
    use_enhanced = bool(spec["use_enhanced"])
    enable_telemetry = bool(spec["enable_telemetry"])
    run_incarnation = str(spec["run_incarnation"])
    deadline_budget = DeadlineBudget.from_deadline_at_ms(
        int(spec["deadline_at_ms"])
    )
    typed_admission = None
    reserved_workflow_name = claim_envelope.get("workflow_name")
    if not isinstance(reserved_workflow_name, str):
        reserved_workflow_name = None
    if claim_envelope.get("run_kind") == "text_to_sql" and (
        is_text_to_sql_workflow_name(reserved_workflow_name)
    ):
        from .text_to_sql_typed_runtime import capture_text_to_sql_typed_admission

        typed_admission = capture_text_to_sql_typed_admission(
            run_id=run_id,
            run_incarnation=run_incarnation,
            deadline=deadline_budget,
            query=parameters.get("query"),
            context_documents=parameters.get("context_documents", ()),
            dsn=parameters.get("dsn"),
            schema_scope=parameters.get("schema_scope"),
        )
    # Глобальные переменные в контексте процесса для доступа из signal handler
    _process_telemetry_manager = None
    _process_root_span = None

    def graceful_shutdown(signum, frame):
        """Корректное завершение с закрытием спана."""
        nonlocal _process_telemetry_manager, _process_root_span
        plog = logging.getLogger(__name__)
        plog.warning(f"🚨 Процесс workflow {run_id} получил сигнал {signum}, начинаем корректное завершение.")

        if _process_telemetry_manager and _process_root_span:
            try:
                plog.info(f"🏁 Закрываем корневой span для {run_id} с ошибкой...")
                _process_telemetry_manager.finish_run_trace(
                    _process_root_span,
                    success=False,
                    error_message=f"Процесс прерван сигналом {signum}"
                )
                plog.info(f"✅ Корневой span для {run_id} закрыт.")
            except Exception as e:
                plog.error("❌ Ошибка при закрытии корневого спана для %s: %s", run_id, _redact_error_text(e))

        # Даем время на отправку телеметрии, если это возможно
        time.sleep(1)
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, graceful_shutdown)
        signal.signal(signal.SIGINT, graceful_shutdown)
    except Exception:
        pass # May fail on some platforms

    try:
        os.setsid()
    except Exception:
        pass

    try:
        os.environ["RUN_ID"] = run_id
    except Exception:
        pass

    _setup_comprehensive_logging_from_env()

    _setup_process_run_log_capture(run_id)

    child_manager = WorkflowManager(use_enhanced=use_enhanced)

    def span_setter(span):
        nonlocal _process_root_span
        _process_root_span = span

    try:
        if "safety_policy" in parameters:
            from custom_tools.text_to_sql.validators import TextToSqlSafetyPolicy

            parameters = dict(parameters)
            parameters["safety_policy"] = TextToSqlSafetyPolicy.from_mapping(
                parameters["safety_policy"]
            )

        from telemetry import get_telemetry_manager
        # Use enable_telemetry parameter here
        _process_telemetry_manager = get_telemetry_manager(enabled=enable_telemetry)

        child_manager._run_workflow_thread(
            run_id, Path(workflow_path), parameters, session_id, client_id,
            span_setter=span_setter,
            enable_telemetry=enable_telemetry, # Pass down the telemetry flag
            run_incarnation=run_incarnation,
            deadline_budget=deadline_budget,
            typed_admission=typed_admission,
            reserved_workflow_name=reserved_workflow_name,
            supervisor_id=supervisor_id,
            attempt_generation=attempt_generation,
        )
    except SystemExit:
        raise
    except Exception as e:
        # Внутри дочернего процесса просто логируем ошибку
        safe_error = _redact_error_text(e)
        process_logger = logging.getLogger(__name__)
        process_logger.error("Ошибка дочернего процесса workflow %s: %s", run_id, safe_error)
        if _process_telemetry_manager and _process_root_span:
            _process_telemetry_manager.finish_run_trace(_process_root_span, success=False, error_message=safe_error)
        sys.exit(1)

@dataclass
class WorkflowInfo:
    """Информация о доступном пайплайне"""
    file_path: str
    name: str
    version: str = "1.0"
    description: str = ""
    steps_count: int = 0
    estimated_duration: str = "неизвестно"
    complexity: str = "неизвестно"
    category: str = "general"
    agents_used: List[str] = None
    parameters: Dict[str, Any] = None

    def __post_init__(self):
        if self.agents_used is None:
            self.agents_used = []
        if self.parameters is None:
            self.parameters = {}

@dataclass
class WorkflowRunStatus:
    """Статус выполнения workflow"""
    run_id: str
    workflow_name: str
    status: str  # queued, running, completed, failed, cancelled
    current_step: Optional[str] = None
    current_step_index: int = 0
    total_steps: int = 0
    progress_percentage: float = 0.0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None
    step_results: Dict[str, Any] = None
    parameters: Dict[str, Any] = None
    use_enhanced_engine: bool = False # Added for monitoring
    enable_telemetry: bool = False # Added for monitoring
    terminal_outcome: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.step_results is None:
            self.step_results = {}
        if self.parameters is None:
            self.parameters = {}

@dataclass
class WorkflowArtifacts:
    """Артефакты выполнения workflow"""
    run_id: str
    final_output: Any = None
    step_outputs: Dict[str, Any] = None
    metadata: Dict[str, Any] = None
    logs_path: Optional[str] = None
    traces_path: Optional[str] = None
    terminal_outcome: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.step_outputs is None:
            self.step_outputs = {}
        if self.metadata is None:
            self.metadata = {}


@dataclass(frozen=True)
class WorkflowOwner:
    """Immutable owner scope used for lifecycle and quota identity."""

    subject: str
    tenant_id: str
    roles: frozenset[str]

    @property
    def quota_identity(self) -> str:
        material = f"{self.tenant_id}\0{self.subject}".encode("utf-8")
        return f"owner:{hashlib.sha256(material).hexdigest()}"


def _require_owner_lifecycle(run_id: str, owner: WorkflowOwner) -> None:
    from backend.fastapi_app.agui.store import EventStore

    store = EventStore(str(_agui_event_store_path()))
    try:
        stored = store.get_run(run_id)
        if (
            stored is None
            or stored.run_kind != "text_to_sql"
            or stored.owner_subject != owner.subject
            or stored.tenant_id != owner.tenant_id
        ):
            raise ValueError("run not found")
        if stored.status not in {
            "pending",
            "queued",
            "running",
            "result_pending",
        }:
            raise ValueError(f"run is already terminal: {run_id}")
    finally:
        store.close()


def _record_owner_worker_pid(
    run_id: str,
    owner: WorkflowOwner,
    worker_pid: int,
) -> None:
    from backend.fastapi_app.agui.store import EventStore

    store = EventStore(str(_agui_event_store_path()))
    try:
        stored = store.get_run(run_id)
        if (
            stored is None
            or stored.owner_subject != owner.subject
            or stored.tenant_id != owner.tenant_id
            or not store.set_worker_pid(run_id, worker_pid)
        ):
            raise ValueError("worker lifecycle registration was rejected")
    finally:
        store.close()


def _owner_primary_workflow_result(
    run_id: str,
    owner: WorkflowOwner,
) -> Optional[Dict[str, Any]]:
    return _authoritative_workflow_result_payload(
        run_id,
        owner_subject=owner.subject,
        tenant_id=owner.tenant_id,
    )


class WorkflowManager:
    """
    Менеджер для управления workflow через Streamlit UI
    Предоставляет простой и стабильный API без изменения существующего кода
    """

    def __init__(
        self,
        use_enhanced: bool = True,
        pipelines_dir: str = "workflow_pipelines",
        supervisor: Any = None,
    ):
        """
        Args:
            use_enhanced: Использовать EnhancedWorkflowEngine или базовый WorkflowEngine
            pipelines_dir: Директория с YAML пайплайнами
        """
        self.pipelines_dir = Path(pipelines_dir)
        self.use_enhanced = use_enhanced
        self._engine = None
        self._supervisor = supervisor

        # Используем глобальные переменные для разделения состояния между экземплярами
        global _GLOBAL_WORKFLOW_ACTIVE_RUNS, _GLOBAL_WORKFLOW_RUN_CALLBACKS
        self.active_runs = _GLOBAL_WORKFLOW_ACTIVE_RUNS
        self.run_callbacks = _GLOBAL_WORKFLOW_RUN_CALLBACKS

        _schedule_workflow_result_outbox_drain()
        logger.info(f"🔧 WorkflowManager инициализирован с глобальным состоянием (enhanced={use_enhanced})")

    def _process_supervisor(self):
        if self._supervisor is None:
            self._supervisor = _default_workflow_process_supervisor()
        return self._supervisor

    @property
    def engine(self):
        if self._engine is None:
            if self.use_enhanced:
                from .enhanced_engine import EnhancedWorkflowEngine

                self._engine = EnhancedWorkflowEngine()
            else:
                from .engine import WorkflowEngine

                self._engine = WorkflowEngine()
        return self._engine

    @engine.setter
    def engine(self, value):
        self._engine = value

    def list_workflows(self) -> List[WorkflowInfo]:
        """
        Получить список доступных workflow пайплайнов

        Returns:
            Список объектов WorkflowInfo с информацией о пайплайнах
        """
        logger.info(f"Searching for workflows in: {self.pipelines_dir.resolve()}")
        workflows = []

        if not self.pipelines_dir.exists():
            logger.warning(f"⚠️ Директория пайплайнов не найдена: {self.pipelines_dir}")
            return workflows

        for yaml_file in self.pipelines_dir.glob("*.yaml"):
            try:
                workflow_def = WorkflowDefinition.from_yaml(yaml_file)

                # Извлекаем параметры из метаданных
                parameters = workflow_def.metadata.get("parameters", {})

                # Собираем агентов из шагов, учитывая что могут быть как agent, так и tool шаги
                agents_used = []
                for step in workflow_def.steps:
                    # Для шагов типа agent берем agent_type
                    if hasattr(step, 'agent_type') and step.agent_type:
                        agents_used.append(step.agent_type)
                    # Для шагов типа tool берем tool_name
                    elif hasattr(step, 'tool_name') and step.tool_name:
                        agents_used.append(f"tool:{step.tool_name}")
                    # Также проверяем step_type для наглядности
                    elif hasattr(step, 'step_type') and step.step_type == 'tool':
                        tool_name = getattr(step, 'tool_name', 'unknown_tool')
                        agents_used.append(f"tool:{tool_name}")

                workflow_info = WorkflowInfo(
                    file_path=str(yaml_file),
                    name=workflow_def.name,
                    version=workflow_def.version,
                    description=workflow_def.description,
                    steps_count=len(workflow_def.steps),
                    estimated_duration=workflow_def.metadata.get("estimated_duration", "неизвестно"),
                    complexity=workflow_def.metadata.get("complexity", "неизвестно"),
                    category=workflow_def.metadata.get("category", "general"),
                    agents_used=sorted(list(set(agents_used))),
                    parameters=parameters
                )
                workflows.append(workflow_info)

            except Exception as e:
                logger.warning(
                    "⚠️ Не удалось загрузить %s: %s",
                    yaml_file,
                    _redact_error_text(e),
                )

        # Сортируем по категориям, затем по имени
        workflows.sort(key=lambda x: (x.category, x.name))

        logger.info(f"📋 Найдено {len(workflows)} пайплайнов")
        return workflows

    def start_workflow(
        self,
        workflow_name: str,
        parameters: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        client_id: Optional[str] = None,
        progress_callback: Optional[ProgressCallback] = None,
        log_callback: Optional[LogCallback] = None,
        use_enhanced: bool = True,
        enable_telemetry: bool = False,
        run_id: Optional[str] = None,
        owner: Optional[WorkflowOwner] = None,
        text_to_sql_admission: Optional["TextToSqlWorkflowAdmission"] = None,
    ) -> str:
        """Durably enqueue one workflow for the process supervisor."""
        from backend.fastapi_app.agui.auth import Principal
        from backend.fastapi_app.agui.store import EventStore

        explicit_run_id = run_id is not None
        if explicit_run_id and (
            not isinstance(run_id, str) or not run_id or run_id != run_id.strip()
        ):
            raise ValueError("run_id must be a non-empty canonical string")
        session_id = session_id or f"session-{uuid.uuid4().hex}"
        parameters = {} if parameters is None else dict(parameters)

        workflow_file: Optional[Path] = None
        workflow_definition: Optional[WorkflowDefinition] = None
        load_errors: List[str] = []
        for yaml_file in self.pipelines_dir.glob("*.yaml"):
            try:
                candidate = WorkflowDefinition.from_yaml(yaml_file)
            except Exception as exc:
                load_errors.append(f"{yaml_file.name}: {exc}")
                continue
            if candidate.name == workflow_name:
                workflow_file = yaml_file.resolve()
                workflow_definition = candidate
                break
        if workflow_file is None or workflow_definition is None:
            if load_errors:
                raise ValueError(
                    f"Пайплайн '{workflow_name}' не найден. Ошибки загрузки YAML: "
                    + "; ".join(load_errors)
                )
            raise ValueError(f"Пайплайн '{workflow_name}' не найден")
        if is_text_to_sql_workflow_name(workflow_definition.name):
            use_enhanced = True
        if text_to_sql_admission is not None:
            from backend.fastapi_app.agui._t2s_requests import (
                TextToSqlWorkflowAdmission,
            )

            if not isinstance(text_to_sql_admission, TextToSqlWorkflowAdmission):
                raise TypeError("text_to_sql_admission must be server-internal")
            if not is_text_to_sql_workflow_name(workflow_definition.name):
                raise ValueError(
                    "text_to_sql_admission is only valid for "
                    f"{TEXT_TO_SQL_WORKFLOW_NAME}"
                )
        if workflow_definition.requires_enhanced_engine and not use_enhanced:
            raise ValueError(
                f"Pipeline '{workflow_name}' requires EnhancedWorkflowEngine "
                "(pipeline.requires_enhanced_engine=true), but use_enhanced=False"
            )
        limits = getattr(workflow_definition, "global_resource_limits", None)
        duration_seconds = (
            getattr(limits, "max_duration_seconds", None)
            if limits is not None
            else None
        )
        if duration_seconds is None:
            duration_seconds = 300
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int)
            or duration_seconds <= 0
        ):
            raise ValueError(
                "global_resource_limits.max_duration_seconds must be positive"
            )

        caller_supplied_owner = owner is not None
        if owner is None:
            owner = WorkflowOwner(
                subject="legacy-workflow",
                tenant_id="legacy",
                roles=frozenset({"legacy"}),
            )
        elif not explicit_run_id:
            raise ValueError("owner-scoped workflow requires an explicit run_id")
        # client_id всегда выводится сервером из owner: доверять переданному
        # значению нельзя (например, forwardedProps.client_id из runner.py
        # позволил бы обходить per-client квоты произвольными идентификаторами).
        client_id = owner.quota_identity

        if run_id is None:
            run_id = f"run-{uuid.uuid4().hex}"

        from workflow.process_supervisor import SupervisorConfig
        from backend.fastapi_app.agui.store import WorkflowAdmissionConflictError

        queue_limit = SupervisorConfig.from_environment().queue_limit
        store = EventStore(str(_agui_event_store_path()))
        try:
            existing = None
            existing_invocation = None
            existing_spec = None
            if caller_supplied_owner and text_to_sql_admission is None:
                existing = store.get_run(run_id)
                if existing is not None:
                    existing_invocation = store.get_workflow_run_invocation(run_id)
                    existing_spec = store.load_work_spec(run_id)
                    if (existing_invocation is None) != (existing_spec is None):
                        raise WorkflowRunAlreadyReservedError(
                            f"run_id is already reserved: {run_id}"
                        )
            if existing_invocation is not None and existing_spec is not None:
                stored_work_spec = existing_spec.to_mapping()
                if (
                    existing is None
                    or existing.owner_subject != owner.subject
                    or existing.tenant_id != owner.tenant_id
                    or existing.run_kind != "agui"
                    or existing.status not in {"queued", "running", "result_pending"}
                    or existing_invocation.workflow_name != workflow_definition.name
                    or existing_invocation.session_id != session_id
                    or stored_work_spec["run_incarnation"]
                    != existing_invocation.run_incarnation
                    or stored_work_spec["session_id"] != session_id
                ):
                    raise WorkflowRunAlreadyReservedError(
                        f"run_id is already reserved: {run_id}"
                    )
                stored = existing
                newly_admitted = False
                run_incarnation = existing_invocation.run_incarnation
                work_spec = stored_work_spec
                deadline_at_ms = work_spec["deadline_at_ms"]
                parameters = dict(work_spec["parameters"])
            else:
                run_incarnation = uuid.uuid4().hex
                deadline_at_ms = int(time.time() * 1_000) + duration_seconds * 1_000
                work_spec = {
                    "spec_version": 1,
                    "workflow_path": str(workflow_file),
                    "parameters": parameters,
                    "session_id": session_id,
                    "client_id": client_id,
                    "use_enhanced": use_enhanced,
                    "enable_telemetry": enable_telemetry,
                    "run_incarnation": run_incarnation,
                    "deadline_at_ms": deadline_at_ms,
                }
                try:
                    stored, newly_admitted = store.admit_workflow_run(
                        run_id=run_id,
                        thread_id=session_id,
                        principal=Principal(owner.subject, owner.tenant_id, owner.roles),
                        run_kind=(
                            "text_to_sql"
                            if text_to_sql_admission is not None
                            else "agui" if caller_supplied_owner else "legacy"
                        ),
                        run_incarnation=run_incarnation,
                        session_id=session_id,
                        workflow_name=workflow_definition.name,
                        work_spec=work_spec,
                        deadline_at_ms=deadline_at_ms,
                        queue_limit=queue_limit,
                        create_if_missing=(
                            not caller_supplied_owner
                            or text_to_sql_admission is not None
                        ),
                        idempotency_key=(
                            text_to_sql_admission.idempotency_key
                            if text_to_sql_admission is not None
                            else None
                        ),
                        request_fingerprint=(
                            text_to_sql_admission.request_fingerprint
                            if text_to_sql_admission is not None
                            else None
                        ),
                    )
                except WorkflowAdmissionConflictError as exc:
                    raise WorkflowRunAlreadyReservedError(
                        f"run_id is already reserved: {run_id}"
                    ) from exc
            if not newly_admitted:
                if not caller_supplied_owner and explicit_run_id:
                    raise WorkflowRunAlreadyReservedError(
                        f"run_id is already reserved: {run_id}"
                    )
                invocation = store.get_workflow_run_invocation(stored.run_id)
                stored_spec = store.load_work_spec(stored.run_id)
                if invocation is None or stored_spec is None:
                    raise WorkflowAdmissionConflictError(
                        "workflow admission replay is missing durable state"
                    )
                run_id = stored.run_id
                run_incarnation = invocation.run_incarnation
                session_id = invocation.session_id
                work_spec = stored_spec.to_mapping()
                deadline_at_ms = work_spec["deadline_at_ms"]
                parameters = dict(work_spec["parameters"])
        finally:
            store.close()

        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            existing = self.active_runs.get(run_id)
            if existing is not None and existing.get("run_incarnation") != (
                run_incarnation
            ):
                raise ValueError(f"run_id collision with live invocation: {run_id}")
            self.active_runs[run_id] = {
                "run_id": run_id,
                "run_incarnation": run_incarnation,
                "workflow_name": workflow_definition.name,
                "status": stored.status,
                "start_time": datetime.now(),
                "parameters": _public_workflow_parameters(parameters),
                "session_id": session_id,
                "client_id": client_id,
                "owner_subject": owner.subject,
                "tenant_id": owner.tenant_id,
                "use_enhanced_engine": use_enhanced,
                "enable_telemetry": enable_telemetry,
            }
            callbacks = []
            if progress_callback is not None:
                callbacks.append(("progress", progress_callback))
            if log_callback is not None:
                callbacks.append(("log", log_callback))
            if callbacks:
                self.run_callbacks[run_id] = callbacks
        if newly_admitted:
            try:
                submission = self._process_supervisor().submit(
                    run_id,
                    work_spec,
                    deadline_at_ms=deadline_at_ms,
                )
                if str(getattr(submission, "state", "")) != "queued":
                    raise WorkflowExecutionError(
                        "supervisor did not return queued admission"
                    )
            except Exception:
                with _GLOBAL_WORKFLOW_RUNS_LOCK:
                    live = self.active_runs.get(run_id)
                    if live is not None and live.get("run_incarnation") == (
                        run_incarnation
                    ):
                        self.active_runs.pop(run_id, None)
                        self.run_callbacks.pop(run_id, None)
                raise
        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            live = self.active_runs.get(run_id)
            if live is not None and live.get("run_incarnation") == run_incarnation:
                live["status"] = stored.status
        logger.info("Workflow '%s' queued with run_id=%s", workflow_name, run_id)
        return run_id

    def _run_workflow_thread(self, run_id: str, workflow_file: Path,
                           parameters: Dict[str, Any], session_id: str, client_id: Optional[str],
                           span_setter: Optional[Callable] = None,
                           enable_telemetry: bool = False,
                           run_incarnation: Optional[str] = None,
                           deadline_budget: Optional[DeadlineBudget] = None,
                           typed_admission: Optional[object] = None,
                           reserved_workflow_name: Optional[str] = None,
                           supervisor_id: Optional[str] = None,
                           attempt_generation: Optional[int] = None):
        """Выполнение workflow в отдельном потоке"""
        supervisor_id, attempt_generation = _validate_private_workflow_claim(
            supervisor_id,
            attempt_generation,
        )
        claim_kwargs = (
            {
                "supervisor_id": supervisor_id,
                "attempt_generation": attempt_generation,
            }
            if supervisor_id is not None
            else {}
        )
        try:
            # Используем thread-safe run_id_context для workflow
            try:
                from unified_logging import get_run_logger, run_id_context

                with run_id_context(run_id):
                    rlog = get_run_logger(run_id, __name__)
                    rlog.info(f"Workflow поток запущен с run_id: {run_id}")

                    # КРИТИЧНО: Создаём корневой span ВНУТРИ run_id_context
                    root_span = None
                    try:
                        from telemetry import get_telemetry_manager
                        # Use enable_telemetry parameter here
                        telemetry_manager = get_telemetry_manager(enabled=enable_telemetry)
                        workflow_def = WorkflowDefinition.from_yaml(workflow_file)
                        if telemetry_manager and telemetry_manager.is_enabled(): # Check if manager is enabled AND if telemetry is requested
                            root_span = telemetry_manager.start_run_trace(
                                run_id=run_id,
                                agent_name="WorkflowEngine",
                                task=f"Workflow: {workflow_def.name}",
                                profile_type="workflow_execution",
                                pipeline_name=workflow_def.name,
                                session_id=session_id
                            )
                            if span_setter:
                                span_setter(root_span)
                            rlog.info(f"🔍 Создан корневой span для Workflow run_id: {run_id}")
                    except Exception as e:
                        rlog.warning("⚠️ Не удалось создать корневой span для workflow: %s", _redact_error_text(e))
                        telemetry_manager = None

                    # Выполняем workflow в контексте run_id и span
                    try:
                        if root_span is not None:
                            from opentelemetry import trace
                            with trace.use_span(root_span):
                                result = self._execute_workflow_in_context(
                                    run_id,
                                    workflow_file,
                                    parameters,
                                    session_id,
                                    client_id,
                                    run_incarnation=run_incarnation,
                                    deadline_budget=deadline_budget,
                                    typed_admission=typed_admission,
                                    reserved_workflow_name=reserved_workflow_name,
                                    **claim_kwargs,
                                )
                        else:
                            result = self._execute_workflow_in_context(
                                run_id,
                                workflow_file,
                                parameters,
                                session_id,
                                client_id,
                                run_incarnation=run_incarnation,
                                deadline_budget=deadline_budget,
                                typed_admission=typed_admission,
                                reserved_workflow_name=reserved_workflow_name,
                                **claim_kwargs,
                            )

                        if root_span is not None:
                            # Сохраняем результат workflow в корневой span перед завершением
                            try:
                                if result and hasattr(result, 'final_output') and result.final_output:
                                    # Добавляем результат в атрибуты span
                                    if hasattr(root_span, 'set_attribute'):
                                        import json
                                        # Сериализуем результат для сохранения в телеметрии
                                        result_json = json.dumps(_redact_public_payload(result.final_output), ensure_ascii=False, default=str)
                                        root_span.set_attribute("output.value", result_json)
                                        root_span.set_attribute("output.mime_type", "application/json")
                                        if rlog:
                                            rlog.info(f"💾 Результат workflow сохранён в span: {type(result.final_output)}")
                            except Exception as save_err:
                                if rlog:
                                    rlog.warning("⚠️ Не удалось сохранить результат в span: %s", _redact_error_text(save_err))

                            telemetry_manager.finish_run_trace(root_span, success=True)
                    except Exception as wf_err:
                        if root_span is not None:
                            telemetry_manager.finish_run_trace(root_span, success=False, error_message=_redact_error_text(wf_err))
                        raise

            except ImportError as import_err:
                raise RuntimeError("run_id_context is required for workflow execution") from import_err

        except Exception as e:
            # Обработка ошибок workflow
            safe_error = _redact_error_text(e)
            current_invocation = False
            with _GLOBAL_WORKFLOW_RUNS_LOCK:
                # Guard: run_id мог не успеть зарегистрироваться в active_runs, если
                # исключение возникло до инициализации записи — не падаем KeyError'ом
                # (консистентно с защищённым except в _execute_workflow_in_context).
                live_run = self.active_runs.get(run_id)
                current_invocation = bool(
                    isinstance(live_run, dict)
                    and (
                        run_incarnation is None
                        or live_run.get("run_incarnation") == run_incarnation
                    )
                )
                terminal_already_resolved = bool(
                    current_invocation
                    and live_run.get("workflow_result_event_appended") is True
                )
                if current_invocation and not terminal_already_resolved:
                    live_run.update({
                        "status": "failed",
                        "end_time": datetime.now(),
                        "error": safe_error
                    })
            if current_invocation and not terminal_already_resolved:
                self._notify_progress(run_id, "failed", {"error": safe_error})
            logger.error("❌ Ошибка выполнения workflow %s: %s", run_id, safe_error)
            raise

    def _execute_workflow_in_context(self, run_id: str, workflow_file: Path,
                                    parameters: Dict[str, Any], session_id: str,
                                    client_id: Optional[str] = None,
                                    run_incarnation: Optional[str] = None,
                                    deadline_budget: Optional[DeadlineBudget] = None,
                                    typed_admission: Optional[object] = None,
                                    reserved_workflow_name: Optional[str] = None,
                                    supervisor_id: Optional[str] = None,
                                    attempt_generation: Optional[int] = None) -> Any:
        """Выполнение workflow в контексте run_id"""
        worker_config_versions: Optional[
            Dict[str, Dict[str, Optional[str]]]
        ] = None
        supervisor_id, attempt_generation = _validate_private_workflow_claim(
            supervisor_id,
            attempt_generation,
        )
        supervisor_evidence = (
            SupervisorExecutionEvidence(supervisor_id, attempt_generation)
            if supervisor_id is not None and attempt_generation is not None
            else None
        )
        if reserved_workflow_name is not None and (
            not isinstance(reserved_workflow_name, str)
            or not reserved_workflow_name
            or reserved_workflow_name != reserved_workflow_name.strip()
        ):
            raise ValueError("reserved_workflow_name must be canonical text")
        reserved_text_to_sql = is_text_to_sql_workflow_name(
            reserved_workflow_name
        )
        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            preexisting_run = self.active_runs.get(run_id)
            if (
                run_incarnation is not None
                and isinstance(preexisting_run, dict)
                and preexisting_run.get("run_incarnation") not in {
                    None,
                    run_incarnation,
                }
            ):
                raise WorkflowExecutionError(
                    f"Stale workflow result incarnation for run_id {run_id}"
                )
            effective_incarnation = run_incarnation or (
                preexisting_run.get("run_incarnation")
                if isinstance(preexisting_run, dict)
                else None
            )
            if preexisting_run is None and reserved_text_to_sql:
                preexisting_run = {
                    "run_id": run_id,
                    "run_incarnation": effective_incarnation,
                    "workflow_name": reserved_workflow_name,
                    "status": "running",
                    "start_time": datetime.now(),
                }
                self.active_runs[run_id] = preexisting_run
            elif (
                isinstance(preexisting_run, dict)
                and reserved_text_to_sql
                and preexisting_run.get("workflow_name") not in {
                    None,
                    "",
                    reserved_workflow_name,
                }
            ):
                raise WorkflowExecutionError(
                    f"Reserved workflow changed for run_id {run_id}"
                )
        try:
            # Загружаем workflow definition до входа в лок (медленный from_yaml вне лока)
            workflow_def = WorkflowDefinition.from_yaml(workflow_file)
            if (
                reserved_text_to_sql
                and workflow_def.name != reserved_workflow_name
            ):
                raise WorkflowExecutionError(
                    f"Loaded workflow does not match reservation for run_id {run_id}"
                )

            # Инициализируем статус выполнения и пишем метаданные в одном блоке под локом
            with _GLOBAL_WORKFLOW_RUNS_LOCK:
                if run_id not in self.active_runs:
                    self.active_runs[run_id] = {
                        "run_id": run_id,
                        "run_incarnation": effective_incarnation,
                        "workflow_name": "",
                        "status": "running",
                        "start_time": datetime.now(),
                    }
                run_data = self.active_runs[run_id]
                if run_data.get("run_incarnation") not in {
                    None,
                    effective_incarnation,
                }:
                    raise WorkflowExecutionError(
                        f"Stale workflow result incarnation for run_id {run_id}"
                    )
                if effective_incarnation is not None:
                    run_data["run_incarnation"] = effective_incarnation
                run_data["status"] = "running"
                run_data["start_time"] = datetime.now()
                run_data["workflow_name"] = workflow_def.name
                run_data["total_steps"] = len(workflow_def.steps)

            self._notify_progress(run_id, "started", {"workflow_name": workflow_def.name})
            
            # Выполняем workflow асинхронно
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                context = WorkflowContext(
                    session_id=session_id,
                    client_id=client_id,
                    variables=parameters,
                )
                if deadline_budget is not None:
                    context._deadline_budget = deadline_budget
                if typed_admission is not None:
                    context._text_to_sql_typed_admission = typed_admission
                if supervisor_evidence is not None:
                    context._supervisor_evidence = supervisor_evidence
                with _workflow_dsn_env(
                    parameters,
                    workflow_name=workflow_def.name,
                ):
                    result = loop.run_until_complete(
                        self.engine.execute_workflow_from_yaml(
                            workflow_file,
                            context=context,
                            client_id=client_id,
                            **parameters
                        )
                    )
                worker_config_versions = _capture_active_yaml_config_versions()
                
                result_status = getattr(result, "status", WorkflowStatus.COMPLETED)
                if hasattr(result_status, "value"):
                    result_status_value = result_status.value
                else:
                    result_status_value = str(result_status)
                failed_step_ids = [
                    step_id
                    for step_id, step_result in (result.step_results or {}).items()
                    if step_result.status == StepStatus.FAILED
                ]
                # Explicit identification of text-to-sql workflows for failure-policy.
                # name — authoritative identifier (стабильный контракт пайплайна),
                # category — secondary signal. Drift между ними логируется как bug.
                is_text_to_sql_by_name = is_text_to_sql_workflow_name(
                    workflow_def.name
                )
                is_text_to_sql_by_category = (
                    workflow_def.metadata.get("category")
                    == TEXT_TO_SQL_WORKFLOW_CATEGORY
                )
                if is_text_to_sql_by_name != is_text_to_sql_by_category:
                    logger.warning(
                        f"text_to_sql identification drift: name={workflow_def.name}, category={workflow_def.metadata.get('category')}"
                    )
                is_text_to_sql = is_text_to_sql_by_name  # name — authoritative
                terminal_outcome = _terminal_outcome_mapping(
                    getattr(result, "terminal_outcome", None)
                )
                authoritative_terminal_outcome = terminal_outcome
                if is_text_to_sql:
                    if terminal_outcome is None:
                        run_status = "failed"
                        is_success = False
                        error_message = "Text-to-SQL result is missing terminal_outcome"
                    else:
                        run_status, is_success = _terminal_legacy_fields(terminal_outcome)
                        error_message = None if is_success else str(
                            terminal_outcome.get("error")
                            or terminal_outcome.get("reason_code")
                            or "Text-to-SQL terminal gate failed"
                        )
                else:
                    is_success = result_status_value == WorkflowStatus.COMPLETED.value
                    run_status = "completed" if is_success else "failed"
                    error_message = None
                    if not is_success:
                        if failed_step_ids:
                            error_message = "Workflow failed steps: " + ", ".join(failed_step_ids)
                        else:
                            error_message = f"Workflow завершился со статусом {result_status_value}"

                # Build the terminal projection off-registry. Durable resolution
                # must happen before the shared run is published terminal.
                with _GLOBAL_WORKFLOW_RUNS_LOCK:
                    live_run = self.active_runs.get(run_id)
                    if (
                        live_run is not run_data
                        or (
                            effective_incarnation is not None
                            and run_data.get("run_incarnation")
                            != effective_incarnation
                        )
                    ):
                        raise WorkflowExecutionError(
                            f"Stale workflow result incarnation for run_id {run_id}"
                        )
                    candidate_run = dict(run_data)
                    candidate_run.update({
                        "run_id": run_id,
                        "status": run_status,
                        "end_time": datetime.now(),
                        "current_step": None,
                        "progress_percentage": 100.0 if is_success else run_data.get("progress_percentage", 0.0),
                        "workflow_id": getattr(result, "workflow_id", None),
                        "final_output": getattr(result, "final_output", None),
                        "error": error_message,
                        "terminal_outcome": terminal_outcome,
                        "config_versions": worker_config_versions,
                        "step_outputs": {
                            step_id: result.step_results[step_id].output
                            for step_id in result.step_results.keys()
                        } if result.step_results else {},
                        "step_results": {step_id: {
                            "status": result.step_results[step_id].status.value if hasattr(result.step_results[step_id].status, "value") else str(result.step_results[step_id].status),
                            "output": str(result.step_results[step_id].output)[:500] if result.step_results[step_id].output else None
                        } for step_id in result.step_results.keys()} if result.step_results else {}
                    })
                    if terminal_outcome is not None:
                        candidate_run["execution"] = terminal_outcome.get("execution")
                    elif is_text_to_sql:
                        terminal_outcome = _apply_text_to_sql_terminal_failure(
                            candidate_run,
                            run_id=run_id,
                            reason_code="MANDATORY_STEP_NOT_COMPLETED",
                            error=error_message or (
                                "Text-to-SQL result is missing terminal_outcome"
                            ),
                        )
                    _status_snap = candidate_run["status"]
                    _error_snap = candidate_run.get("error")
                    _artifacts_snap = _workflow_artifacts_from_run_data(candidate_run)

                result_resolution = _persist_workflow_result(
                    run_id,
                    candidate_run.get("final_output"),
                    _status_snap,
                    _error_snap,
                    artifacts=_artifacts_snap,
                    snapshot={
                        "workflow_name": workflow_def.name,
                        "parameters": _public_workflow_parameters(parameters),
                        "session_id": session_id,
                        "client_id": client_id,
                    },
                    terminal_outcome=terminal_outcome,
                    run_incarnation=effective_incarnation,
                    supervisor_id=supervisor_id,
                    attempt_generation=attempt_generation,
                )
                if not result_resolution.persistence_succeeded:
                    append_error = "Не удалось записать terminal WORKFLOW_RESULT для workflow"
                    persistence_run = dict(candidate_run)
                    persistence_terminal = _apply_text_to_sql_terminal_failure(
                        persistence_run,
                        run_id=run_id,
                        reason_code="RESULT_PERSISTENCE_FAILED",
                        error=append_error,
                        source_terminal=authoritative_terminal_outcome,
                    )
                    persistence_output = persistence_run.get("final_output")
                    persistence_artifacts = _workflow_artifacts_from_run_data(
                        persistence_run
                    )
                    fallback_resolution = _persist_workflow_result(
                        run_id,
                        persistence_output,
                        persistence_run["status"],
                        error=persistence_run.get("error"),
                        artifacts=persistence_artifacts,
                        snapshot={
                            "workflow_name": workflow_def.name,
                            "parameters": _public_workflow_parameters(parameters),
                            "session_id": session_id,
                            "client_id": client_id,
                        },
                        terminal_outcome=persistence_terminal,
                        run_incarnation=effective_incarnation,
                        supervisor_id=supervisor_id,
                        attempt_generation=attempt_generation,
                    )
                    resolved_fallback = fallback_resolution.resolved_payload
                    with _GLOBAL_WORKFLOW_RUNS_LOCK:
                        live_run = self.active_runs.get(run_id)
                        if (
                            live_run is not run_data
                            or (
                                effective_incarnation is not None
                                and run_data.get("run_incarnation")
                                != effective_incarnation
                            )
                        ):
                            raise WorkflowExecutionError(
                                f"Stale workflow result incarnation for run_id {run_id}"
                            )
                        if resolved_fallback is not None:
                            _merge_workflow_result_payload(run_data, resolved_fallback)
                        else:
                            run_data.update(persistence_run)
                        run_data["workflow_result_append_failure_handled"] = True
                        if fallback_resolution.persistence_succeeded:
                            run_data["workflow_result_event_appended"] = True
                    if (
                        fallback_resolution.persistence_succeeded
                        and not fallback_resolution.candidate_won
                        and resolved_fallback is not None
                    ):
                        return resolved_fallback
                    self._notify_progress(run_id, "failed", {"error": append_error})
                    raise WorkflowExecutionError(append_error)
                with _GLOBAL_WORKFLOW_RUNS_LOCK:
                    live_run = self.active_runs.get(run_id)
                    if (
                        live_run is not run_data
                        or (
                            effective_incarnation is not None
                            and run_data.get("run_incarnation")
                            != effective_incarnation
                        )
                    ):
                        raise WorkflowExecutionError(
                            f"Stale workflow result incarnation for run_id {run_id}"
                        )
                    if result_resolution.resolved_payload is None:
                        run_data["workflow_result_event_appended"] = True
                        raise WorkflowExecutionError(
                            "Terminal WORKFLOW_RESULT winner could not be resolved"
                        )
                    _merge_workflow_result_payload(
                        run_data,
                        result_resolution.resolved_payload,
                    )
                    run_data["workflow_result_event_appended"] = True
                    _notify_status = run_data["status"]
                    _notify_error = run_data.get("error")
                if result_resolution.candidate_won:
                    self._notify_progress(
                        run_id,
                        _notify_status,
                        {
                            "result": "success" if is_success else "failed",
                            "error": _notify_error,
                        },
                    )
                if _notify_status == "failed":
                    raise WorkflowExecutionError(_notify_error or "Workflow failed")
                if not result_resolution.candidate_won:
                    return result_resolution.resolved_payload
                return result
                
            finally:
                loop.close()
                
        except Exception as e:
            if worker_config_versions is None:
                worker_config_versions = _capture_active_yaml_config_versions()
            safe_error = _redact_error_text(e)
            with _GLOBAL_WORKFLOW_RUNS_LOCK:
                live_run = self.active_runs.get(run_id)
                current_invocation = bool(
                    isinstance(live_run, dict)
                    and (
                        effective_incarnation is None
                        or live_run.get("run_incarnation")
                        == effective_incarnation
                    )
                )
                had_stored_result = bool(
                    current_invocation
                    and (
                        live_run.get("workflow_result_event_appended") is True
                        or live_run.get("workflow_result_append_failure_handled")
                        is True
                    )
                )
                if current_invocation and not had_stored_result:
                    candidate_run = dict(live_run)
                    candidate_run.update({
                        "status": "failed",
                        "end_time": datetime.now(),
                        "error": safe_error,
                        "config_versions": worker_config_versions,
                    })
                    candidate_terminal = candidate_run.get("terminal_outcome")
                    if is_text_to_sql_workflow_name(
                        candidate_run.get("workflow_name")
                    ):
                        candidate_terminal = _apply_text_to_sql_terminal_failure(
                            candidate_run,
                            run_id=run_id,
                            reason_code="MANDATORY_STEP_NOT_COMPLETED",
                            error=safe_error,
                            source_terminal=candidate_terminal,
                        )
                    existing_output = candidate_run.get("final_output")
                    artifacts = _workflow_artifacts_from_run_data(candidate_run)
                else:
                    existing_output = None
                    artifacts = None
                    candidate_terminal = None
            if not current_invocation or had_stored_result:
                raise

            resolution = _persist_workflow_result(
                run_id,
                existing_output,
                "failed",
                error=safe_error,
                artifacts=artifacts,
                terminal_outcome=candidate_terminal,
                run_incarnation=effective_incarnation,
                supervisor_id=supervisor_id,
                attempt_generation=attempt_generation,
            )
            if resolution.persistence_succeeded and resolution.resolved_payload is not None:
                with _GLOBAL_WORKFLOW_RUNS_LOCK:
                    live_run = self.active_runs.get(run_id)
                    if not isinstance(live_run, dict) or (
                        effective_incarnation is not None
                        and live_run.get("run_incarnation") != effective_incarnation
                    ):
                        raise WorkflowExecutionError(
                            f"Stale workflow result incarnation for run_id {run_id}"
                        ) from e
                    _merge_workflow_result_payload(
                        live_run,
                        resolution.resolved_payload,
                    )
                    live_run["workflow_result_event_appended"] = True
                    resolved_status = str(live_run.get("status") or "").lower()
                    resolved_error = live_run.get("error")
                if not resolution.candidate_won:
                    if resolved_status in {"completed", "cancelled"}:
                        return resolution.resolved_payload
                    raise WorkflowExecutionError(
                        resolved_error or "Workflow failed"
                    ) from e
                self._notify_progress(run_id, "failed", {"error": safe_error})
                raise

            if resolution.persistence_succeeded:
                with _GLOBAL_WORKFLOW_RUNS_LOCK:
                    live_run = self.active_runs.get(run_id)
                    if isinstance(live_run, dict) and (
                        effective_incarnation is None
                        or live_run.get("run_incarnation") == effective_incarnation
                    ):
                        live_run["workflow_result_event_appended"] = True
                raise WorkflowExecutionError(
                    "Terminal WORKFLOW_RESULT winner could not be resolved"
                ) from e

            append_error = "Не удалось записать terminal WORKFLOW_RESULT для workflow"
            persistence_run = dict(candidate_run)
            persistence_terminal = _apply_text_to_sql_terminal_failure(
                persistence_run,
                run_id=run_id,
                reason_code="RESULT_PERSISTENCE_FAILED",
                error=append_error,
                source_terminal=candidate_terminal,
            )
            fallback_resolution = _persist_workflow_result(
                run_id,
                persistence_run.get("final_output"),
                persistence_run["status"],
                error=persistence_run.get("error"),
                artifacts=_workflow_artifacts_from_run_data(persistence_run),
                terminal_outcome=persistence_terminal,
                run_incarnation=effective_incarnation,
                supervisor_id=supervisor_id,
                attempt_generation=attempt_generation,
            )
            with _GLOBAL_WORKFLOW_RUNS_LOCK:
                live_run = self.active_runs.get(run_id)
                if isinstance(live_run, dict) and (
                    effective_incarnation is None
                    or live_run.get("run_incarnation") == effective_incarnation
                ):
                    if fallback_resolution.resolved_payload is not None:
                        _merge_workflow_result_payload(
                            live_run,
                            fallback_resolution.resolved_payload,
                        )
                    else:
                        live_run.update(persistence_run)
                    live_run["workflow_result_append_failure_handled"] = True
                    if fallback_resolution.persistence_succeeded:
                        live_run["workflow_result_event_appended"] = True
            if (
                fallback_resolution.persistence_succeeded
                and not fallback_resolution.candidate_won
                and fallback_resolution.resolved_payload is not None
            ):
                return fallback_resolution.resolved_payload
            self._notify_progress(run_id, "failed", {"error": append_error})
            raise WorkflowExecutionError(append_error) from e

    def _notify_progress(self, run_id: str, event_type: str, data: Dict[str, Any]):
        """Уведомление о прогрессе выполнения workflow"""
        # Всегда записываем событие в статус для мониторинга
        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            if run_id in self.active_runs:
                self.active_runs[run_id][f"last_{event_type}"] = {
                    "timestamp": datetime.now(),
                    "data": data
                }
            callbacks_snapshot = list(self.run_callbacks.get(run_id, []))

        # Вызываем зарегистрированные callbacks вне блокировки
        for callback_type, callback_func in callbacks_snapshot:
            if callback_type == "progress":
                try:
                    callback_func(run_id, event_type, data)
                except Exception as e:
                    logger.warning(
                        "⚠️ Ошибка в progress callback для %s: %s",
                        run_id,
                        _redact_error_text(e),
                    )
        
        # Получаем EventBus и отправляем ProgressEvent
        try:
            from unified_logging import get_logging_manager
            event_bus = get_logging_manager().event_bus
            event_bus.emit_progress(run_id, event_type, "workflow", data)
        except Exception as e:
            logger.debug(
                "Не удалось отправить событие '%s' в EventBus для run_id '%s': %s",
                event_type,
                run_id,
                _redact_error_text(e),
            )

    def get_workflow_status(self, run_id: str) -> Optional[WorkflowRunStatus]:
        """
        Получить статус выполнения workflow
        
        Args:
            run_id: Идентификатор запуска
            
        Returns:
            Объект WorkflowRunStatus или None
        """
        terminal_statuses = {"completed", "failed", "cancelled"}
        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            initial_run = self.active_runs.get(run_id)
            initial_version = (
                _workflow_run_store_merge_version(initial_run)
                if isinstance(initial_run, dict)
                else None
            )
            initial_incarnation = (
                initial_run.get("run_incarnation")
                if isinstance(initial_run, dict)
                else None
            )
            should_read_store = not isinstance(initial_run, dict) or str(
                initial_run.get("status") or ""
            ).lower() not in terminal_statuses

        stored_payload = (
            _workflow_result_payload_from_store(
                run_id,
                **(
                    {"run_incarnation": initial_incarnation}
                    if initial_incarnation is not None
                    else {}
                ),
            )
            if should_read_store
            else None
        )

        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            live_run = self.active_runs.get(run_id)
            if isinstance(live_run, dict):
                stored_status = (
                    str(stored_payload.get("status") or "").lower()
                    if stored_payload
                    else ""
                )
                if (
                    stored_payload
                    and stored_status in terminal_statuses
                    and live_run is initial_run
                    and _workflow_run_store_merge_version(live_run) == initial_version
                    and str(live_run.get("status") or "").lower()
                    not in terminal_statuses
                ):
                    _merge_workflow_result_payload(live_run, stored_payload)
                    if not live_run.get("end_time"):
                        live_run["end_time"] = datetime.now()
                run_data = dict(live_run)
            else:
                run_data = None

        if run_data is None:
            if not stored_payload:
                return None
            artifacts = (
                stored_payload.get("artifacts")
                if isinstance(stored_payload.get("artifacts"), dict)
                else {}
            )
            snapshot = (
                stored_payload.get("snapshot")
                if isinstance(stored_payload.get("snapshot"), dict)
                else {}
            )
            workflow_name = snapshot.get("workflow_name", "unknown")
            terminal_candidate = (
                stored_payload.get("terminal_outcome")
                or artifacts.get("terminal_outcome")
            )
            terminal_outcome = _validated_terminal_outcome(terminal_candidate)
            stored_status = stored_payload.get("status", "unknown")
            if is_text_to_sql_workflow_name(workflow_name):
                if terminal_outcome is None:
                    stored_status = "invalid_terminal"
                else:
                    stored_status, _ = _terminal_legacy_fields(terminal_outcome)
            snapshot_parameters = snapshot.get("parameters")
            return WorkflowRunStatus(
                run_id=run_id,
                workflow_name=workflow_name,
                status=stored_status,
                progress_percentage=100.0 if stored_status == "completed" else 0.0,
                error_message=(
                    _redact_error_text(stored_payload.get("error"))
                    if stored_payload.get("error")
                    else None
                ),
                step_results=_redact_public_payload(
                    artifacts.get("step_results") or {}
                ),
                parameters=(
                    _public_workflow_parameters(snapshot_parameters)
                    if isinstance(snapshot_parameters, dict)
                    else {}
                ),
                terminal_outcome=_redact_public_payload(terminal_outcome),
            )

        terminal_candidate = run_data.get("terminal_outcome")
        terminal_outcome = _validated_terminal_outcome(terminal_candidate)
        if is_text_to_sql_workflow_name(run_data.get("workflow_name")):
            if terminal_outcome is not None:
                run_data["status"], _ = _terminal_legacy_fields(terminal_outcome)
            elif run_data.get("status") in {"completed", "failed", "cancelled"}:
                run_data["status"] = "invalid_terminal"
        
        # Вычисляем длительность
        duration = None
        if run_data.get("end_time") and run_data.get("start_time"):
            duration = (run_data["end_time"] - run_data["start_time"]).total_seconds()
        
        # Получаем информацию о шагах из run_data, если есть
        current_step = run_data.get("current_step")
        current_step_index = run_data.get("current_step_index", 0)
        total_steps = run_data.get("total_steps", 0)
        progress_percentage = run_data.get("progress_percentage", 0.0)
        step_results = run_data.get("step_results", {})
        
        run_parameters = run_data.get("parameters")
        return WorkflowRunStatus(
            run_id=run_id,
            workflow_name=run_data.get("workflow_name", "unknown"),
            status=run_data.get("status", "unknown"),
            current_step=current_step,
            current_step_index=current_step_index,
            total_steps=total_steps,
            progress_percentage=progress_percentage,
            start_time=run_data.get("start_time"),
            end_time=run_data.get("end_time"),
            duration_seconds=duration,
            error_message=_redact_error_text(run_data.get("error")) if run_data.get("error") else None,
            step_results=_redact_public_payload(step_results),
            parameters=(
                _public_workflow_parameters(run_parameters)
                if isinstance(run_parameters, dict)
                else {}
            ),
            use_enhanced_engine=run_data.get("use_enhanced_engine", False),
            enable_telemetry=run_data.get("enable_telemetry", False),
            terminal_outcome=_redact_public_payload(terminal_outcome),
        )

    def get_workflow_artifacts(self, run_id: str) -> Optional[WorkflowArtifacts]:
        """Получить артефакты выполнения workflow."""
        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            initial_run = self.active_runs.get(run_id)
            initial_version = (
                _workflow_run_store_merge_version(initial_run)
                if isinstance(initial_run, dict)
                else None
            )
            initial_incarnation = (
                initial_run.get("run_incarnation")
                if isinstance(initial_run, dict)
                else None
            )
            should_read_store = not isinstance(initial_run, dict) or (
                not initial_run.get("final_output")
                and not initial_run.get("step_outputs")
                and not initial_run.get("terminal_outcome")
            )

        stored_payload = (
            _workflow_result_payload_from_store(
                run_id,
                **(
                    {"run_incarnation": initial_incarnation}
                    if initial_incarnation is not None
                    else {}
                ),
            )
            if should_read_store
            else None
        )

        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            live_run = self.active_runs.get(run_id)
            if isinstance(live_run, dict):
                if (
                    stored_payload
                    and live_run is initial_run
                    and _workflow_run_store_merge_version(live_run) == initial_version
                    and not live_run.get("final_output")
                    and not live_run.get("step_outputs")
                    and not live_run.get("terminal_outcome")
                ):
                    _merge_workflow_result_payload(live_run, stored_payload)
                run_data = dict(live_run)
            else:
                run_data = None

        if run_data is None:
            if not stored_payload:
                return None
            artifacts = (
                stored_payload.get("artifacts")
                if isinstance(stored_payload.get("artifacts"), dict)
                else {}
            )
            terminal_outcome = _validated_terminal_outcome(
                stored_payload.get("terminal_outcome")
                or artifacts.get("terminal_outcome")
            )
            raw_metadata = artifacts.get("metadata") or {}
            metadata = _redact_public_payload(raw_metadata)
            if isinstance(raw_metadata, dict) and isinstance(
                raw_metadata.get("parameters"),
                dict,
            ):
                metadata["parameters"] = _public_workflow_parameters(
                    raw_metadata["parameters"]
                )
            if (
                isinstance(raw_metadata, dict)
                and "config_versions" in raw_metadata
            ):
                metadata["config_versions"] = (
                    _validated_config_versions_metadata(
                        raw_metadata.get("config_versions")
                    )
                )
            return WorkflowArtifacts(
                run_id=run_id,
                final_output=_redact_public_payload(
                    artifacts.get("final_output", stored_payload.get("result"))
                ),
                step_outputs=_redact_public_payload(
                    artifacts.get("step_outputs") or {}
                ),
                metadata=metadata,
                terminal_outcome=_redact_public_payload(terminal_outcome),
            )
        terminal_outcome = _validated_terminal_outcome(run_data.get("terminal_outcome"))
        metadata = {
            "workflow_id": run_data.get("workflow_id"),
            "workflow_name": run_data.get("workflow_name"),
            "execution": run_data.get("execution"),
        }
        if run_data.get("config_versions") is not None:
            metadata["config_versions"] = run_data.get("config_versions")
        public_metadata = _redact_public_payload(metadata)
        if "config_versions" in metadata:
            public_metadata["config_versions"] = (
                _validated_config_versions_metadata(
                    metadata.get("config_versions")
                )
            )
        return WorkflowArtifacts(
            run_id=run_id,
            final_output=_redact_public_payload(run_data.get("final_output")),
            step_outputs=_redact_public_payload(run_data.get("step_outputs")),
            metadata=public_metadata,
            terminal_outcome=_redact_public_payload(terminal_outcome),
        )

    def get_active_run_snapshot(self, run_id: str) -> Dict[str, Any]:
        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            run_data = self.active_runs.get(run_id)
            if not isinstance(run_data, dict):
                return {}
            snapshot = dict(run_data)
            parameters = snapshot.get("parameters")
            if isinstance(parameters, dict):
                snapshot["parameters"] = _public_workflow_parameters(parameters)
            return snapshot

    def update_active_run(self, run_id: str, updates: Dict[str, Any]) -> bool:
        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            run_data = self.active_runs.get(run_id)
            if not isinstance(run_data, dict):
                return False
            run_data.update(updates)
            return True

    def list_active_run_snapshots(self) -> List[Tuple[str, Dict[str, Any]]]:
        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            snapshots: List[Tuple[str, Dict[str, Any]]] = []
            for run_id, run_data in self.active_runs.items():
                if not isinstance(run_data, dict):
                    continue
                snapshot = dict(run_data)
                parameters = snapshot.get("parameters")
                if isinstance(parameters, dict):
                    snapshot["parameters"] = _public_workflow_parameters(
                        parameters
                    )
                snapshots.append((run_id, snapshot))
            return snapshots

    def cleanup_completed_runs(self, max_age_hours: float = 24) -> int:
        cutoff = datetime.now().timestamp() - max_age_hours * 3600
        cleaned = 0
        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            for run_id, run_data in list(self.active_runs.items()):
                if not isinstance(run_data, dict):
                    continue
                status = run_data.get("status")
                end_time = run_data.get("end_time")
                end_ts = end_time.timestamp() if isinstance(end_time, datetime) else None
                if status in {"completed", "failed", "cancelled"} and end_ts is not None and end_ts < cutoff:
                    self.active_runs.pop(run_id, None)
                    cleaned += 1
        return cleaned

    def delete_workflow(self, name: str) -> Tuple[bool, str]:
        """
        Удалить YAML-файл пайплайна по имени.

        Args:
            name: Имя пайплайна (поле WorkflowDefinition.name).

        Returns:
            (True, путь) при успехе; (False, сообщение об ошибке) при неудаче.
        """
        if not self.pipelines_dir.exists():
            return False, f"Директория пайплайнов не найдена: {self.pipelines_dir}"

        target_file: Optional[Path] = None
        for yaml_file in self.pipelines_dir.glob("*.yaml"):
            try:
                wf_def = WorkflowDefinition.from_yaml(yaml_file)
                if wf_def.name == name:
                    target_file = yaml_file
                    break
            except Exception:
                continue

        if target_file is None:
            return False, f"Пайплайн '{name}' не найден"

        if any(v.get("workflow_name") == name for v in self.active_runs.values()):
            return False, "Пайплайн выполняется, дождитесь завершения"

        try:
            target_file.unlink()
            logger.info("Пайплайн '%s' удалён: %s", name, target_file)
            return True, str(target_file)
        except Exception as exc:
            return False, f"Ошибка удаления '{target_file}': {_redact_error_text(exc)}"

    def cancel_workflow(
        self,
        run_id: str,
        *,
        cancellation_request_id: str | None = None,
        cancellation_provenance: str | None = None,
    ) -> bool:
        """Delegate durable cancellation and process ownership to the supervisor."""
        supervisor = self._process_supervisor()
        if cancellation_request_id is None:
            result = supervisor.cancel(run_id)
        else:
            result = supervisor.cancel(
                run_id,
                cancellation_request_id=cancellation_request_id,
                cancellation_provenance=cancellation_provenance,
            )
        accepted = bool(getattr(result, "accepted", False))
        state = str(getattr(result, "state", ""))
        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            live = self.active_runs.get(run_id)
            if live is not None:
                if state == "cancelled":
                    live.update({"status": "cancelled", "end_time": datetime.now()})
                elif accepted:
                    live["status"] = "cancelling"
        return accepted


# Ре-экспорт слоя доставки результатов (T9: декомпозиция god-модуля).
# Логика терминализации/доставки WORKFLOW_RESULT и durable outbox переехала
# в .result_delivery; имена ниже сохранены на streamlit_api для обратной
# совместимости внешних импортов и monkeypatch в тестах.
from . import result_delivery  # noqa: E402  (нужен как якорь для monkeypatch)
from .result_delivery import (  # noqa: E402
    _OUTBOX_DRAIN_BATCH_LIMIT,
    _OUTBOX_DRAIN_TIME_BUDGET_SECONDS,
    _OUTBOX_DRAIN_RETRY_DELAY_SECONDS,
    _OUTBOX_DRAIN_SCHEDULE_LOCK,
    _OUTBOX_DRAIN_SCHEDULED,
    _reset_outbox_drain_scheduler_after_fork,
    _terminalize_supervised_workflow,
    _is_retryable_outbox_error,
    _SENSITIVE_DSN_QUERY_KEYS,
    _SENSITIVE_PAYLOAD_KEYS,
    _SENSITIVE_SCALAR_KEYS,
    _URL_LIKE_PAYLOAD_KEYS,
    _DSN_TEXT_RE,
    _SECRET_KEY_PATTERN,
    _SENSITIVE_TEXT_ASSIGNMENT_RE,
    _project_root,
    _agui_event_store_path,
    _workflow_result_outbox_path,
    _workflow_result_payload_from_store,
    _authoritative_workflow_result_payload,
    _safe_serialize_result,
    _dsn_fingerprint,
    _is_sensitive_dsn_query_key,
    _is_sensitive_dsn_payload_key,
    _is_url_like_payload_key,
    _redact_sensitive_assignment,
    _redact_dsn,
    _looks_like_dsn,
    _redact_query_string,
    _redact_text,
    _redact_payload,
    _redact_error_text,
    _redact_public_payload,
    _public_workflow_parameters,
    _terminal_outcome_mapping,
    _validated_terminal_outcome,
    _terminal_legacy_fields,
    _apply_text_to_sql_terminal_failure,
    _validated_config_versions_metadata,
    _transform_workflow_result_event_payload,
    _build_workflow_result_event_payload,
    _build_text_to_sql_no_runtime_terminal,
    _append_workflow_result_payload_to_primary,
    _enqueue_workflow_result_payload,
    _WorkflowResultResolution,
    _coerce_workflow_result_resolution,
    _append_workflow_result_event,
    _persist_workflow_result,
    _drain_workflow_result_outbox_batch,
    _drain_workflow_result_outbox,
    _workflow_result_outbox_pending_state,
    _start_scheduled_workflow_result_outbox_drain,
    _scheduled_workflow_result_outbox_drain,
    _schedule_workflow_result_outbox_drain,
    _reserve_workflow_run_invocation,
    _workflow_artifacts_from_run_data,
    _merge_workflow_result_payload,
    _workflow_run_store_merge_version,
)
