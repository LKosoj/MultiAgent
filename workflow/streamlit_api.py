"""
Публичные контракты для интеграции со Streamlit
==============================================

Этот модуль предоставляет стабильный API для Streamlit UI,
не изменяя существующую бизнес-логику workflow engine.
"""

import asyncio
import os
import logging
import uuid
import signal
import time
from datetime import datetime
from typing import Dict, List, Any, Optional, Union, Callable
from pathlib import Path
from dataclasses import dataclass, asdict
from contextlib import contextmanager
import threading
import json
import sys
import re
import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import (
    WorkflowDefinition, WorkflowResult, WorkflowContext, WorkflowStatus,
    StepResult, StepStatus, WorkflowStep, WorkflowExecutionError
)
from backend.fastapi_app.agui.redaction import (
    _is_sensitive_key,
    _normalize_sensitive_key,
    _redact_payload as _agui_redact_payload,
    redact_pii_in_payload,
)

logger = logging.getLogger(__name__)
_SENSITIVE_DSN_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "key",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
}
_SENSITIVE_PAYLOAD_KEYS = {
    "connection_string",
    "database_dsn",
    "database_url",
    "db_dsn",
    "db_url",
    "dsn",
}
_SENSITIVE_SCALAR_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "key",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
}
_URL_LIKE_PAYLOAD_KEYS = {"url"}
_DSN_TEXT_RE = re.compile(r"(?P<dsn>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+)")
_SECRET_KEY_PATTERN = r"[A-Za-z0-9_%+\-.\[\]]+"
_SENSITIVE_TEXT_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>\b(?P<key>{_SECRET_KEY_PATTERN})\s*[:=]\s*)"
    r"(?P<secret>[^\s,;&]+)",
    re.IGNORECASE,
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _agui_event_store_path() -> Path:
    return _project_root() / "data" / "agui_events.db"


def _workflow_result_payload_from_store(
    run_id: str,
    *,
    strict: bool = False,
) -> Optional[Dict[str, Any]]:
    try:
        from backend.fastapi_app.agui.store import EventStore

        db_path = _agui_event_store_path()
        if not db_path.exists():
            return None
        store = EventStore(str(db_path))
        latest_payload = None
        for event in store.list_after(run_id, 0):
            if event.event_type == "WORKFLOW_RESULT":
                latest_payload = event.payload
        return latest_payload
    except Exception:
        if strict:
            raise
        return None


def _safe_serialize_result(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


def _dsn_fingerprint(dsn: str) -> str:
    return hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]


def _is_sensitive_dsn_query_key(key: Any) -> bool:
    normalized = _normalize_sensitive_key(key)
    return normalized == "odbc_connect" or normalized in _SENSITIVE_DSN_QUERY_KEYS or _is_sensitive_key(key)


def _is_sensitive_dsn_payload_key(key: Any) -> bool:
    return _normalize_sensitive_key(key) in _SENSITIVE_PAYLOAD_KEYS


def _is_url_like_payload_key(key: Any) -> bool:
    return _normalize_sensitive_key(key) in _URL_LIKE_PAYLOAD_KEYS


def _redact_sensitive_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    return f"{match.group('prefix')}***"


def _redact_dsn(dsn: Any) -> Any:
    if not isinstance(dsn, str):
        return dsn
    try:
        parts = urlsplit(dsn)
        if not parts.scheme:
            return "<redacted>"
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, hostinfo = netloc.rsplit("@", 1)
            netloc = f"***:***@{hostinfo}" if ":" in userinfo else f"***@{hostinfo}"
        query_items = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query_items.append((key, "***" if _is_sensitive_dsn_query_key(key) else value))
        query = urlencode(query_items, doseq=True).replace("%2A%2A%2A", "***")
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except Exception:
        return "<redacted>"


def _looks_like_dsn(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.scheme and (parts.netloc or parts.scheme in {"sqlite", "duckdb"}))
    except Exception:
        return False


def _redact_query_string(value: str) -> str:
    try:
        items = parse_qsl(value, keep_blank_values=True)
    except Exception:
        return value
    if not items or not any(_is_sensitive_dsn_query_key(key) for key, _ in items):
        return value
    return urlencode(
        [(key, "***" if _is_sensitive_dsn_query_key(key) else item) for key, item in items],
        doseq=True,
    )


def _redact_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        candidate = match.group("dsn")
        return _redact_dsn(candidate) if _looks_like_dsn(candidate) else candidate

    redacted = _DSN_TEXT_RE.sub(replace, value)
    return _SENSITIVE_TEXT_ASSIGNMENT_RE.sub(_redact_sensitive_assignment, redacted)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if _is_sensitive_dsn_payload_key(key) or (_is_url_like_payload_key(key) and _looks_like_dsn(item)):
                redacted[key] = _redact_dsn(item)
                if isinstance(item, str):
                    redacted.setdefault(f"{key}_fingerprint", _dsn_fingerprint(item))
            elif _is_sensitive_key(key) or _normalize_sensitive_key(key) in _SENSITIVE_SCALAR_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_error_text(error: Any) -> str:
    return str(redact_pii_in_payload(_agui_redact_payload(str(error))))


def _redact_public_payload(value: Any) -> Any:
    return redact_pii_in_payload(_agui_redact_payload(value))


def _append_workflow_result_event(
    run_id: str,
    result: Any,
    status: str,
    error: Optional[str] = None,
    artifacts: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
) -> bool:
    try:
        from backend.fastapi_app.agui.store import EventStore

        db_path = _agui_event_store_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = EventStore(str(db_path))
        payload = {
            "run_id": run_id,
            "thread_id": run_id,
            "status": status,
            "success": status == "completed",
            "result": _redact_payload(_safe_serialize_result(result)),
            "error": _redact_payload(error),
            "artifacts": _redact_payload(_safe_serialize_result(artifacts or {})),
            "snapshot": _redact_payload(_safe_serialize_result(snapshot or {})),
        }
        payload = redact_pii_in_payload(_agui_redact_payload(payload))
        store.append(run_id, "WORKFLOW_RESULT", payload)
        return True
    except Exception as exc:
        logger.warning(
            "⚠️ Не удалось записать WORKFLOW_RESULT для workflow %s: %s",
            run_id,
            _redact_error_text(exc),
        )
        return False


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _text_to_sql_execution_state(parameters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Контракт для downstream `db_audit` step: при dry_run_only=True
    # execution_state должен явно сигнализировать о пропуске запуска и
    # содержать `skip_audit=True`, чтобы аудит не писал запись о фантомном
    # исполнении. Поля `executed`/`dry_run`/`status` потребляются UI и
    # storage слоем.
    if not isinstance(parameters, dict) or not _coerce_bool(parameters.get("dry_run_only")):
        return None
    return {
        "dry_run_only": True,
        "dry_run": True,
        "executed": False,
        "status": "skipped",
        "reason": "dry_run_only=True",
        "skip_audit": True,
    }


def _workflow_artifacts_from_run_data(run_data: Dict[str, Any]) -> Dict[str, Any]:
    metadata = {
        "workflow_id": run_data.get("workflow_id"),
        "workflow_name": run_data.get("workflow_name"),
    }
    if run_data.get("execution") is not None:
        metadata["execution"] = run_data.get("execution")
    return {
        "run_id": run_data.get("run_id"),
        "final_output": run_data.get("final_output"),
        "step_outputs": run_data.get("step_outputs") or {},
        "step_results": run_data.get("step_results") or {},
        "metadata": metadata,
    }


def _merge_workflow_result_payload(run_data: Dict[str, Any], payload: Dict[str, Any]) -> None:
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    if payload.get("status"):
        run_data["status"] = payload.get("status")
    if payload.get("error") is not None:
        run_data["error"] = payload.get("error")
    if payload.get("result") is not None:
        run_data["final_output"] = payload.get("result")
    if artifacts.get("final_output") is not None:
        run_data["final_output"] = artifacts.get("final_output")
    if artifacts.get("step_outputs") is not None:
        run_data["step_outputs"] = artifacts.get("step_outputs")
    if artifacts.get("step_results") is not None:
        run_data["step_results"] = artifacts.get("step_results")
    metadata = artifacts.get("metadata") if isinstance(artifacts.get("metadata"), dict) else {}
    if metadata.get("execution") is not None:
        run_data["execution"] = metadata.get("execution")
    if snapshot.get("workflow_name"):
        run_data["workflow_name"] = snapshot.get("workflow_name")
    if snapshot.get("parameters") is not None:
        run_data["parameters"] = snapshot.get("parameters")

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
_GLOBAL_WORKFLOW_PROCESSES = {}
# RLock: watchdog-поток и cancel-вызовы могут одновременно читать/менять реестр,
# что без mutex даёт race condition (особенно pop+get на одном run_id).
_GLOBAL_WORKFLOW_PROCESSES_LOCK = threading.RLock()
_GLOBAL_WORKFLOW_ENV_LOCK = threading.Lock()

# Типы для callbacks
ProgressCallback = Callable[[str, str, Dict[str, Any]], None]  # (run_id, event_type, data)
LogCallback = Callable[[str, str, str, str], None]  # (run_id, level, message, timestamp)


@contextmanager
def _workflow_dsn_env(parameters: Dict[str, Any]):
    if not isinstance(parameters, dict):
        yield
        return

    with _GLOBAL_WORKFLOW_ENV_LOCK:
        dsn = parameters.get("dsn")
        row_limit = parameters.get("max_rows")
        dry_run_only = parameters.get("dry_run_only")
        safety_level = parameters.get("safety_level")
        validate_schema = parameters.get("validate_schema")
        previous_dsn = os.environ.get("DB_DSN")
        previous_row_limit = os.environ.get("DB_EXECUTOR_ROW_LIMIT")
        previous_dry_run = os.environ.get("TEXT_TO_SQL_DRY_RUN_ONLY")
        previous_safety_level = os.environ.get("TEXT_TO_SQL_SAFETY_LEVEL")
        previous_validate_schema = os.environ.get("TEXT_TO_SQL_VALIDATE_SCHEMA")
        if dsn:
            os.environ["DB_DSN"] = str(dsn)
        if row_limit is not None:
            os.environ["DB_EXECUTOR_ROW_LIMIT"] = str(row_limit)
        if dry_run_only is not None:
            os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] = str(dry_run_only)
        if safety_level is not None:
            os.environ["TEXT_TO_SQL_SAFETY_LEVEL"] = str(safety_level)
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
            if dry_run_only is not None:
                if previous_dry_run is None:
                    os.environ.pop("TEXT_TO_SQL_DRY_RUN_ONLY", None)
                else:
                    os.environ["TEXT_TO_SQL_DRY_RUN_ONLY"] = previous_dry_run
            if safety_level is not None:
                if previous_safety_level is None:
                    os.environ.pop("TEXT_TO_SQL_SAFETY_LEVEL", None)
                else:
                    os.environ["TEXT_TO_SQL_SAFETY_LEVEL"] = previous_safety_level
            if validate_schema is not None:
                if previous_validate_schema is None:
                    os.environ.pop("TEXT_TO_SQL_VALIDATE_SCHEMA", None)
                else:
                    os.environ["TEXT_TO_SQL_VALIDATE_SCHEMA"] = previous_validate_schema


# === Точка входа дочернего процесса для запуска workflow (должна быть на верхнем уровне для spawn) ===
def _workflow_process_entry(run_id: str, workflow_path: str, parameters: Dict[str, Any],
                           session_id: str, client_id: Optional[str], use_enhanced: bool, enable_telemetry: bool):
    """Точка входа для выполнения workflow в отдельном процессе"""
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
        from telemetry import get_telemetry_manager
        # Use enable_telemetry parameter here
        _process_telemetry_manager = get_telemetry_manager(enabled=enable_telemetry)

        child_manager._run_workflow_thread(
            run_id, Path(workflow_path), parameters, session_id, client_id,
            span_setter=span_setter,
            enable_telemetry=enable_telemetry # Pass down the telemetry flag
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

    def __post_init__(self):
        if self.step_outputs is None:
            self.step_outputs = {}
        if self.metadata is None:
            self.metadata = {}


class WorkflowManager:
    """
    Менеджер для управления workflow через Streamlit UI
    Предоставляет простой и стабильный API без изменения существующего кода
    """

    def __init__(self, use_enhanced: bool = True, pipelines_dir: str = "workflow_pipelines"):
        """
        Args:
            use_enhanced: Использовать EnhancedWorkflowEngine или базовый WorkflowEngine
            pipelines_dir: Директория с YAML пайплайнами
        """
        self.pipelines_dir = Path(pipelines_dir)
        self.use_enhanced = use_enhanced
        self._engine = None

        # Используем глобальные переменные для разделения состояния между экземплярами
        global _GLOBAL_WORKFLOW_ACTIVE_RUNS, _GLOBAL_WORKFLOW_RUN_CALLBACKS
        self.active_runs = _GLOBAL_WORKFLOW_ACTIVE_RUNS
        self.run_callbacks = _GLOBAL_WORKFLOW_RUN_CALLBACKS

        logger.info(f"🔧 WorkflowManager инициализирован с глобальным состоянием (enhanced={use_enhanced})")

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

    def start_workflow(self,
                      workflow_name: str,
                      parameters: Optional[Dict[str, Any]] = None,
                      session_id: Optional[str] = None,
                      client_id: Optional[str] = None,
                      progress_callback: Optional[ProgressCallback] = None,
                      log_callback: Optional[LogCallback] = None,
                      use_enhanced: bool = True, # New parameter
                      enable_telemetry: bool = False, # New parameter
                      run_id: Optional[str] = None
                      ) -> str:
        """
        Запустить workflow асинхронно

        Args:
            workflow_name: Имя пайплайна (из YAML файла)
            parameters: Параметры для пайплайна
            session_id: ID сессии (если None, генерируется автоматически)
            client_id: ID клиента для квотирования
            progress_callback: Функция для получения уведомлений о прогрессе
            log_callback: Функция для получения логов
            use_enhanced: Использовать EnhancedWorkflowEngine
            enable_telemetry: Включить телеметрию для этого запуска
            run_id: ID конкретного запуска. Если None, сохраняется старое поведение:
                run_id совпадает с session_id.

        Returns:
            run_id для отслеживания выполнения
        """
        if session_id is None:
            session_id = f"run-{uuid.uuid4().hex[:16]}"
        if run_id is None:
            run_id = session_id

        # T3.1: legacy вызовы (без явного run_id) приводят к совпадению
        # AG-UI run_id и workflow run_id. Логируем для постепенной миграции.
        if run_id == session_id:
            logger.debug(
                "start_workflow: legacy call detected (run_id == session_id=%s, workflow=%s)",
                session_id,
                workflow_name,
            )

        # Пробрасываем RUN_ID для всего запуска (наследуется потоками)
        try:
            os.environ["RUN_ID"] = run_id
        except Exception:
            pass

        if parameters is None:
            parameters = {}

        # Ищем YAML файл пайплайна
        workflow_file = None
        matched_workflow_def: Optional[WorkflowDefinition] = None
        load_errors: List[str] = []
        for yaml_file in self.pipelines_dir.glob("*.yaml"):
            try:
                workflow_def = WorkflowDefinition.from_yaml(yaml_file)
                if workflow_def.name == workflow_name:
                    workflow_file = yaml_file
                    matched_workflow_def = workflow_def
                    break
            except Exception as exc:
                load_errors.append(f"{yaml_file.name}: {exc}")
                continue

        if workflow_file is None or matched_workflow_def is None:
            if load_errors:
                raise ValueError(
                    f"Пайплайн '{workflow_name}' не найден. Ошибки загрузки YAML: "
                    + "; ".join(load_errors)
                )
            raise ValueError(f"Пайплайн '{workflow_name}' не найден")

        # Enforce контракта `pipeline.requires_enhanced_engine`: пайплайны с
        # output_retry_policy и прочими enhanced-фичами не должны молча
        # деградировать на базовом WorkflowEngine. Fail-fast до старта процесса.
        if matched_workflow_def.requires_enhanced_engine and not use_enhanced:
            raise ValueError(
                f"Pipeline '{workflow_name}' requires EnhancedWorkflowEngine "
                f"(pipeline.requires_enhanced_engine=true), but use_enhanced=False"
            )

        # Регистрируем callbacks
        if progress_callback or log_callback:
            self.run_callbacks[run_id] = []
            if progress_callback:
                self.run_callbacks[run_id].append(('progress', progress_callback))
            if log_callback:
                self.run_callbacks[run_id].append(('log', log_callback))

        # Запускаем в отдельном процессе для возможности реальной отмены
        try:
            from multiprocessing import Process

            proc = Process(
                target=_workflow_process_entry,
                args=(run_id, str(workflow_file), parameters, session_id, client_id, use_enhanced, enable_telemetry), # Pass new args
                daemon=True,
            )
            proc.start()

            # Регистрируем запуск в активных с PID
            self.active_runs[run_id] = {
                "run_id": run_id,
                "workflow_name": WorkflowDefinition.from_yaml(workflow_file).name,
                "status": "running",
                "start_time": datetime.now(),
                "parameters": _redact_public_payload(parameters),
                "session_id": session_id,
                "client_id": client_id,
                "pid": proc.pid,
                "use_enhanced_engine": use_enhanced, # Store options for monitoring
                "enable_telemetry": enable_telemetry, # Store options for monitoring
            }

            # Сохраняем процесс в глобальном реестре и запускаем наблюдатель
            with _GLOBAL_WORKFLOW_PROCESSES_LOCK:
                _GLOBAL_WORKFLOW_PROCESSES[run_id] = proc

            def _watchdog(_rid: str):
                with _GLOBAL_WORKFLOW_PROCESSES_LOCK:
                    p = _GLOBAL_WORKFLOW_PROCESSES.get(_rid)
                if not p:
                    return
                p.join()
                # Обновляем статус по завершению процесса, если не был отмечен иначе
                run_data = self.active_runs.get(_rid)
                try:
                    if not run_data:
                        return
                    if run_data.get("status") in ["completed", "failed", "cancelled"]:
                        return
                    exit_code = p.exitcode
                    stored_payload = _workflow_result_payload_from_store(_rid, strict=True)
                    if stored_payload:
                        _merge_workflow_result_payload(run_data, stored_payload)
                    stored_status = stored_payload.get("status") if stored_payload else None
                    stored_error = stored_payload.get("error") if stored_payload else None
                    if not stored_payload and exit_code == 0:
                        stored_status = "failed"
                        stored_error = "Workflow process exited successfully without terminal WORKFLOW_RESULT"
                    run_data.update({
                        "end_time": datetime.now(),
                        "status": stored_status or "failed",
                        "error": stored_error if stored_payload else (stored_error or f"Процесс завершился с кодом {exit_code}"),
                    })
                except Exception as exc:
                    safe_error = _redact_error_text(exc)
                    logger.warning(
                        "⚠️ Не удалось обработать завершение workflow-процесса %s: %s",
                        _rid,
                        safe_error,
                    )
                    if run_data and run_data.get("status") not in ["completed", "failed", "cancelled"]:
                        run_data.update({
                            "end_time": datetime.now(),
                            "status": "failed",
                            "error": f"Не удалось обработать результат workflow-процесса: {safe_error}",
                        })

            watcher = threading.Thread(target=_watchdog, args=(run_id,), daemon=True)
            watcher.start()
        except Exception as e:
            error_message = f"Не удалось запустить workflow в отдельном процессе: {_redact_error_text(e)}"
            self.active_runs[run_id] = {
                "run_id": run_id,
                "workflow_name": WorkflowDefinition.from_yaml(workflow_file).name,
                "status": "failed",
                "start_time": datetime.now(),
                "end_time": datetime.now(),
                "parameters": _redact_public_payload(parameters),
                "session_id": session_id,
                "client_id": client_id,
                "error": error_message,
                "use_enhanced_engine": use_enhanced,
                "enable_telemetry": enable_telemetry,
            }
            result_appended = _append_workflow_result_event(
                run_id,
                None,
                "failed",
                error_message,
                artifacts={"metadata": {"workflow_name": WorkflowDefinition.from_yaml(workflow_file).name}},
                snapshot={
                    "workflow_name": WorkflowDefinition.from_yaml(workflow_file).name,
                    "parameters": parameters,
                    "session_id": session_id,
                    "client_id": client_id,
                },
            )
            if not result_appended:
                append_error = "Не удалось записать terminal WORKFLOW_RESULT для workflow"
                error_message = f"{error_message}; {append_error}"
                self.active_runs[run_id]["error"] = error_message
            logger.error(error_message)
            raise WorkflowExecutionError(error_message) from e

        try:
            from unified_logging import get_run_logger
            _rlog = get_run_logger(run_id, __name__)
            _rlog.info(f"🚀 Запущен workflow '{workflow_name}'")
        except Exception:
            logger.info(f"🚀 Запущен workflow '{workflow_name}' с run_id: {run_id}")
        return run_id

    def _run_workflow_thread(self, run_id: str, workflow_file: Path,
                           parameters: Dict[str, Any], session_id: str, client_id: Optional[str],
                           span_setter: Optional[Callable] = None,
                           enable_telemetry: bool = False): # New parameter
        """Выполнение workflow в отдельном потоке"""
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
                                result = self._execute_workflow_in_context(run_id, workflow_file, parameters, session_id, client_id)
                        else:
                            result = self._execute_workflow_in_context(run_id, workflow_file, parameters, session_id, client_id)

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
            self.active_runs[run_id].update({
                "status": "failed",
                "end_time": datetime.now(),
                "error": safe_error
            })
            self._notify_progress(run_id, "failed", {"error": safe_error})
            logger.error("❌ Ошибка выполнения workflow %s: %s", run_id, safe_error)
            raise

    def _execute_workflow_in_context(self, run_id: str, workflow_file: Path,
                                    parameters: Dict[str, Any], session_id: str,
                                    client_id: Optional[str] = None) -> Any:
        """Выполнение workflow в контексте run_id"""
        try:
            # Инициализируем статус выполнения
            if run_id not in self.active_runs:
                self.active_runs[run_id] = {
                    "run_id": run_id,
                    "workflow_name": "",
                    "status": "running",
                    "start_time": datetime.now(),
                }
            
            run_data = self.active_runs[run_id]
            run_data["status"] = "running"
            run_data["start_time"] = datetime.now()
            
            # Загружаем workflow definition
            workflow_def = WorkflowDefinition.from_yaml(workflow_file)
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
                with _workflow_dsn_env(parameters):
                    result = loop.run_until_complete(
                        self.engine.execute_workflow_from_yaml(
                            workflow_file,
                            context=context,
                            client_id=client_id,
                            **parameters
                        )
                    )
                
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
                is_text_to_sql_by_name = workflow_def.name == "text_to_sql_pipeline"
                is_text_to_sql_by_category = workflow_def.metadata.get("category") == "text_to_sql"
                if is_text_to_sql_by_name != is_text_to_sql_by_category:
                    logger.warning(
                        f"text_to_sql identification drift: name={workflow_def.name}, category={workflow_def.metadata.get('category')}"
                    )
                is_text_to_sql = is_text_to_sql_by_name  # name — authoritative
                is_success = (
                    result_status_value == WorkflowStatus.COMPLETED.value
                    and not (is_text_to_sql and failed_step_ids)
                )
                error_message = None
                if not is_success:
                    if failed_step_ids:
                        error_message = "Workflow failed steps: " + ", ".join(failed_step_ids)
                    else:
                        error_message = f"Workflow завершился со статусом {result_status_value}"
                execution_state = _text_to_sql_execution_state(parameters)

                run_data.update({
                    "run_id": run_id,
                    "status": "completed" if is_success else "failed",
                    "end_time": datetime.now(),
                    "current_step": None,
                    "progress_percentage": 100.0 if is_success else run_data.get("progress_percentage", 0.0),
                    "workflow_id": getattr(result, "workflow_id", None),
                    "final_output": getattr(result, "final_output", None),
                    "error": error_message,
                    "step_outputs": {
                        step_id: result.step_results[step_id].output
                        for step_id in result.step_results.keys()
                    } if result.step_results else {},
                    "step_results": {step_id: {
                        "status": result.step_results[step_id].status.value if hasattr(result.step_results[step_id].status, "value") else str(result.step_results[step_id].status),
                        "output": str(result.step_results[step_id].output)[:500] if result.step_results[step_id].output else None
                    } for step_id in result.step_results.keys()} if result.step_results else {}
                })
                if execution_state:
                    run_data["execution"] = execution_state

                result_appended = _append_workflow_result_event(
                    run_id,
                    getattr(result, "final_output", None),
                    run_data["status"],
                    run_data.get("error"),
                    artifacts=_workflow_artifacts_from_run_data(run_data),
                    snapshot={
                        "workflow_name": workflow_def.name,
                        "parameters": parameters,
                        "session_id": session_id,
                        "client_id": client_id,
                    },
                )
                if not result_appended:
                    append_error = "Не удалось записать terminal WORKFLOW_RESULT для workflow"
                    run_data.update({
                        "status": "failed",
                        "end_time": datetime.now(),
                        "error": append_error,
                    })
                    self._notify_progress(run_id, "failed", {"error": append_error})
                    raise WorkflowExecutionError(append_error)
                run_data["workflow_result_event_appended"] = True
                self._notify_progress(run_id, run_data["status"], {"result": "success" if is_success else "failed", "error": run_data.get("error")})
                if not is_success:
                    raise WorkflowExecutionError(error_message or "Workflow failed")
                return result
                
            finally:
                loop.close()
                
        except Exception as e:
            # Обновляем статус при ошибке
            safe_error = _redact_error_text(e)
            had_stored_result = False
            if run_id in self.active_runs:
                existing_output = self.active_runs[run_id].get("final_output")
                had_stored_result = self.active_runs[run_id].get("workflow_result_event_appended") is True
                self.active_runs[run_id].update({
                    "status": "failed",
                    "end_time": datetime.now(),
                    "error": safe_error
                })
            else:
                existing_output = None
            if not had_stored_result:
                artifacts = _workflow_artifacts_from_run_data(self.active_runs[run_id]) if run_id in self.active_runs else None
                result_appended = _append_workflow_result_event(run_id, existing_output, "failed", error=safe_error, artifacts=artifacts)
                if not result_appended:
                    append_error = "Не удалось записать terminal WORKFLOW_RESULT для workflow"
                    if run_id in self.active_runs:
                        self.active_runs[run_id]["error"] = append_error
                    self._notify_progress(run_id, "failed", {"error": append_error})
                    raise WorkflowExecutionError(append_error) from e
            self._notify_progress(run_id, "failed", {"error": safe_error})
            raise

    def _notify_progress(self, run_id: str, event_type: str, data: Dict[str, Any]):
        """Уведомление о прогрессе выполнения workflow"""
        # Всегда записываем событие в статус для мониторинга
        if run_id in self.active_runs:
            self.active_runs[run_id][f"last_{event_type}"] = {
                "timestamp": datetime.now(),
                "data": data
            }
        
        # Вызываем зарегистрированные callbacks
        if run_id in self.run_callbacks:
            for callback_type, callback_func in self.run_callbacks[run_id]:
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
        if run_id not in self.active_runs:
            stored_payload = _workflow_result_payload_from_store(run_id)
            if not stored_payload:
                return None
            artifacts = stored_payload.get("artifacts") if isinstance(stored_payload.get("artifacts"), dict) else {}
            snapshot = stored_payload.get("snapshot") if isinstance(stored_payload.get("snapshot"), dict) else {}
            return WorkflowRunStatus(
                run_id=run_id,
                workflow_name=snapshot.get("workflow_name", "unknown"),
                status=stored_payload.get("status", "unknown"),
                progress_percentage=100.0 if stored_payload.get("status") == "completed" else 0.0,
                error_message=_redact_error_text(stored_payload.get("error")) if stored_payload.get("error") else None,
                step_results=_redact_public_payload(artifacts.get("step_results") or {}),
                parameters=_redact_public_payload(snapshot.get("parameters") or {}),
            )
            
        run_data = self.active_runs[run_id]
        if run_data.get("status") not in ["completed", "failed", "cancelled"]:
            stored_payload = _workflow_result_payload_from_store(run_id)
            stored_status = str(stored_payload.get("status") or "").lower() if stored_payload else ""
            if stored_status in {"completed", "failed", "cancelled"}:
                _merge_workflow_result_payload(run_data, stored_payload)
                if not run_data.get("end_time"):
                    run_data["end_time"] = datetime.now()
        
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
            parameters=_redact_public_payload(run_data.get("parameters", {})),
            use_enhanced_engine=run_data.get("use_enhanced_engine", False),
            enable_telemetry=run_data.get("enable_telemetry", False)
        )

    def get_workflow_artifacts(self, run_id: str) -> Optional[WorkflowArtifacts]:
        """Получить артефакты выполнения workflow."""
        if run_id not in self.active_runs:
            stored_payload = _workflow_result_payload_from_store(run_id)
            if not stored_payload:
                return None
            artifacts = stored_payload.get("artifacts") if isinstance(stored_payload.get("artifacts"), dict) else {}
            return WorkflowArtifacts(
                run_id=run_id,
                final_output=_redact_public_payload(artifacts.get("final_output", stored_payload.get("result"))),
                step_outputs=_redact_public_payload(artifacts.get("step_outputs") or {}),
                metadata=_redact_public_payload(artifacts.get("metadata") or {}),
            )
        run_data = self.active_runs[run_id]
        if not run_data.get("final_output") and not run_data.get("step_outputs"):
            stored_payload = _workflow_result_payload_from_store(run_id)
            if stored_payload:
                _merge_workflow_result_payload(run_data, stored_payload)
        return WorkflowArtifacts(
            run_id=run_id,
            final_output=_redact_public_payload(run_data.get("final_output")),
            step_outputs=_redact_public_payload(run_data.get("step_outputs")),
            metadata=_redact_public_payload({
                "workflow_id": run_data.get("workflow_id"),
                "workflow_name": run_data.get("workflow_name"),
                "execution": run_data.get("execution"),
            }),
        )

    def cancel_workflow(self, run_id: str) -> bool:
        """
        Отменить выполнение workflow
        
        Args:
            run_id: Идентификатор запуска
            
        Returns:
            True если отмена успешна
        """
        if run_id not in self.active_runs:
            logger.warning(f"Попытка отменить несуществующий workflow: {run_id}")
            return False
            
        run_data = self.active_runs[run_id]
        
        try:
            stored_payload = _workflow_result_payload_from_store(run_id, strict=True)
        except Exception as exc:
            logger.warning(
                "Отмена workflow %s остановлена: не удалось прочитать WORKFLOW_RESULT до остановки процесса: %s",
                run_id,
                _redact_error_text(exc),
            )
            return False
        stored_status = str(stored_payload.get("status") or "").lower() if stored_payload else ""
        if stored_status in {"completed", "failed", "cancelled"}:
            _merge_workflow_result_payload(run_data, stored_payload)
            if not run_data.get("end_time"):
                run_data["end_time"] = datetime.now()
            logger.warning(
                f"Попытка отменить workflow с уже сохранённым WORKFLOW_RESULT: {run_id} "
                f"(статус: {stored_status})"
            )
            return False

        if run_data["status"] in ["completed", "failed", "cancelled"]:
            logger.warning(f"Попытка отменить уже завершенный workflow: {run_id} (статус: {run_data['status']})")
            return False

        # Пытаемся завершить дочерний процесс
        pid = run_data.get("pid")
        with _GLOBAL_WORKFLOW_PROCESSES_LOCK:
            proc = _GLOBAL_WORKFLOW_PROCESSES.get(run_id)
        killed = False
        
        if proc is not None:
            try:
                # Мягкое завершение группы процессов
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate() # Fallback
                
                proc.join(timeout=5.0)
                
                if proc.is_alive():
                    # Жёсткое завершение группы процессов
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        proc.kill() # Fallback
                    proc.join(timeout=3.0)

                killed = not proc.is_alive()
                logger.info(f"Процесс workflow {run_id} (PID: {pid}) завершен: {'успешно' if killed else 'неуспешно'}")

            except Exception as e:
                logger.warning(
                    "⚠️ Ошибка при завершении процесса workflow %s: %s",
                    pid,
                    _redact_error_text(e),
                )

        elif pid:
            # Fallback на сигналы по PID, если нет объекта процесса
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(2) # Даем время на обработку
                try:
                    os.kill(pid, 0) # Проверяем жив ли процесс
                except OSError:
                    killed = True
                
                if not killed:
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.5)
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        killed = True
                
                logger.info(f"Процесс workflow {run_id} (PID: {pid}) завершен по fallback: {'успешно' if killed else 'неуспешно'}")
            
            except ProcessLookupError:
                killed = True
                logger.info(f"Процесс workflow {run_id} (PID: {pid}) уже не существует.")
            except Exception as e:
                logger.warning(
                    "⚠️ Не удалось послать сигнал процессу %s (fallback): %s",
                    pid,
                    _redact_error_text(e),
                )

        try:
            stored_payload = _workflow_result_payload_from_store(run_id, strict=True)
        except Exception as exc:
            logger.warning(
                "Отмена workflow %s не записана как cancelled: не удалось перечитать WORKFLOW_RESULT после остановки процесса: %s",
                run_id,
                _redact_error_text(exc),
            )
            return False
        stored_status = str(stored_payload.get("status") or "").lower() if stored_payload else ""
        if stored_status in {"completed", "failed", "cancelled"}:
            _merge_workflow_result_payload(run_data, stored_payload)
            if not run_data.get("end_time"):
                run_data["end_time"] = datetime.now()
            logger.warning(
                f"Workflow {run_id} уже сохранил terminal WORKFLOW_RESULT во время cancel "
                f"(статус: {stored_status})"
            )
            return False
        if (proc is not None or pid) and not killed:
            logger.warning(
                f"Workflow {run_id} не помечен cancelled: процесс PID {pid} не завершён"
            )
            return False

        # Обновляем статус
        run_data.update({
            "status": "cancelled",
            "end_time": datetime.now()
        })
        if not _append_workflow_result_event(run_id, None, "cancelled"):
            error_message = "Не удалось записать terminal WORKFLOW_RESULT для отмененного workflow"
            run_data.update({
                "status": "failed",
                "end_time": datetime.now(),
                "error": error_message,
            })
            self._notify_progress(run_id, "failed", {"error": error_message})
            return False

        # Удаляем из реестра процессов
        with _GLOBAL_WORKFLOW_PROCESSES_LOCK:
            if run_id in _GLOBAL_WORKFLOW_PROCESSES:
                try:
                    _GLOBAL_WORKFLOW_PROCESSES.pop(run_id)
                except Exception:
                    pass

        # Уведомляем подписчиков
        self._notify_progress(run_id, "cancelled", {})
        
        logger.info(f"🛑 Workflow {run_id} отменен.")
        return True
