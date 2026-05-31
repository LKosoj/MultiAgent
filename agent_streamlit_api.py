"""
Публичные контракты для работы с агентами через Streamlit
=======================================================

Предоставляет стабильный API для управления агентами без изменения
существующего кода AgentFactory и DynamicAgentSystem.
"""

import logging
import uuid
import threading
import warnings
import os
import signal
import time
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable, Union, Tuple
from dataclasses import dataclass, asdict
from pathlib import Path
import yaml
import json
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Подавляем специфические предупреждения Streamlit в многопоточной среде
warnings.filterwarnings('ignore', message='.*missing ScriptRunContext.*')

from agent_factory import AgentFactory, AGENT_PROFILES
from agent_system import DynamicAgentSystem

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
_DSN_TEXT_RE = re.compile(r"(?P<dsn>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+)")
_SENSITIVE_TEXT_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\b(?:access_token|api_key|apikey|auth|key|password|passwd|pwd|secret|token)\b\s*[:=]\s*)"
    r"(?P<secret>[^\s,;]+)",
    re.IGNORECASE,
)


def _redact_dsn(value: str) -> str:
    try:
        parts = urlsplit(value)
        if not parts.scheme:
            return "<redacted>"
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, hostinfo = netloc.rsplit("@", 1)
            username = userinfo.split(":", 1)[0]
            netloc = f"{username}:***@{hostinfo}" if username else f"***@{hostinfo}"
        query_items = [
            (key, "***" if key.lower() in _SENSITIVE_DSN_QUERY_KEYS else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query_items, doseq=True), parts.fragment))
    except Exception:
        return "<redacted>"


def _redact_text(value: str) -> str:
    redacted = _DSN_TEXT_RE.sub(lambda match: _redact_dsn(match.group("dsn")), value)
    return _SENSITIVE_TEXT_ASSIGNMENT_RE.sub(r"\g<prefix>***", redacted)

def _setup_comprehensive_logging_from_env() -> None:
    try:
        from logging_setup import setup_comprehensive_logging
        log_level_str = os.getenv("SMOLAGENTS_LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, log_level_str, logging.INFO)
        setup_comprehensive_logging(log_level=log_level)
    except Exception:
        pass

# Перехват stdout/stderr в JSONL с run_id для процесса агента.
def _setup_process_run_log_capture(run_id: str) -> None:
    try:
        from unified_logging import get_logging_manager
        get_logging_manager(logs_dir=str(Path(__file__).resolve().parent / "logs"))
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
        log_dir = Path(__file__).resolve().parent / "logs"
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
                "message": _redact_text(_strip_ansi(message)),
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
                    _write_json_line(self.level, self.logger_name, _strip_ansi(self._buffer.strip()))
                self._buffer = ""
                try:
                    self.stream.flush()
                except Exception:
                    pass

            def isatty(self):
                return bool(getattr(self.stream, "isatty", lambda: False)())

            def fileno(self):
                return self.stream.fileno()

        sys.stdout = StreamToJsonl("INFO", "agent_stdout", original_stdout)
        sys.stderr = StreamToJsonl("WARNING", "agent_stderr", original_stderr)

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

# === Точка входа дочернего процесса для запуска агента (должна быть на верхнем уровне для spawn) ===
def _agent_process_entry(run_id: str, profile_name: str, task: str, session_id: str, result_queue=None):
    # Глобальные переменные в контексте процесса для доступа из signal handler
    _process_telemetry_manager = None
    _process_root_span = None

    def graceful_shutdown(signum, frame):
        nonlocal _process_telemetry_manager, _process_root_span
        plog = logging.getLogger(__name__)
        plog.warning(f"🚨 Процесс агента {run_id} получил сигнал {signum}, начинаем корректное завершение.")

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
                plog.error(f"❌ Ошибка при закрытии корневого спана для {run_id}: {e}")

        # Выходим сразу: блокировать signal handler недопустимо — он может
        # потерять повторный сигнал и отложить завершение процесса.
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, graceful_shutdown)
        signal.signal(signal.SIGINT, graceful_shutdown)
    except Exception:
        pass

    try:
        os.setsid()
    except Exception:
        pass

    try:
        os.environ["RUN_ID"] = run_id
    except Exception:
        pass

    _setup_comprehensive_logging_from_env()

    try:
        from unified_logging import get_logging_manager
        get_logging_manager(logs_dir=str(Path(__file__).resolve().parent / "logs"))
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
        log_dir = Path(__file__).resolve().parent / "logs"
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
                "message": _redact_text(_strip_ansi(message)),
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
                    _write_json_line(self.level, self.logger_name, _strip_ansi(self._buffer.strip()))
                self._buffer = ""
                try:
                    self.stream.flush()
                except Exception:
                    pass

            def isatty(self):
                return bool(getattr(self.stream, "isatty", lambda: False)())

            def fileno(self):
                return self.stream.fileno()

        sys.stdout = StreamToJsonl("INFO", "agent_stdout", original_stdout)
        sys.stderr = StreamToJsonl("WARNING", "agent_stderr", original_stderr)

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

    try:
        from telemetry import get_telemetry_manager
        _process_telemetry_manager = get_telemetry_manager()
    except Exception:
        pass

    local_factory = AgentFactory()
    agent = local_factory.create_agent(
        profile_type=profile_name,
        session_id=session_id,
        task=task,
        pipeline_type="general"
    )

    if _process_telemetry_manager and _process_telemetry_manager.is_enabled():
        _process_root_span = _process_telemetry_manager.start_run_trace(
            run_id=run_id,
            agent_name=getattr(agent, 'name', profile_name),
            task=task,
            profile_type=profile_name,
            pipeline_name="general",
            session_id=session_id,
        )

    try:
        if _process_root_span:
            from opentelemetry import trace
            with trace.use_span(_process_root_span):
                result = agent.run(task)
        else:
            result = agent.run(task)

        if result_queue is not None:
            try:
                result_queue.put({
                    "status": "completed",
                    "result": json.loads(json.dumps(result, ensure_ascii=False, default=str)),
                })
            except Exception:
                result_queue.put({"status": "completed", "result": str(result)})

        if _process_root_span and _process_telemetry_manager:
            try:
                if isinstance(result, (dict, list)):
                    _process_root_span.set_attribute("output.mime_type", "application/json")
                    _process_root_span.set_attribute("output.value", json.dumps(result, ensure_ascii=False, default=str))
                elif isinstance(result, str):
                    _process_root_span.set_attribute("output.mime_type", "text/plain")
                    _process_root_span.set_attribute("output.value", result)
                elif result is not None:
                    _process_root_span.set_attribute("output.mime_type", "text/plain")
                    _process_root_span.set_attribute("output.value", str(result))
            except Exception:
                pass
            _process_telemetry_manager.finish_run_trace(_process_root_span, success=True)

    except Exception as e:
        logging.getLogger(__name__).error(f"Ошибка выполнения в дочернем процессе агента {run_id}: {e}")
        if result_queue is not None:
            try:
                result_queue.put({"status": "failed", "error": str(e)})
            except Exception:
                pass
        if _process_root_span and _process_telemetry_manager:
            _process_telemetry_manager.finish_run_trace(_process_root_span, success=False, error_message=str(e))
        sys.exit(1)


def _manager_team_process_entry(run_id: str, manager_def_payload, team_defs_payload, task: str, session_id: str):
    try:
        try:
            os.environ["RUN_ID"] = run_id
        except Exception:
            pass
        _setup_comprehensive_logging_from_env()
        _setup_process_run_log_capture(run_id)

        child = AgentManager()

        def _restore_def(item):
            if isinstance(item, dict):
                return DynamicAgentDefinition(**item)
            return item

        manager_def = _restore_def(manager_def_payload)
        team_defs = [_restore_def(item) for item in (team_defs_payload or [])]

        child._run_manager_with_team_thread(run_id, manager_def, team_defs, task, session_id)
    except Exception as e:
        try:
            logging.getLogger(__name__).error(f"Ошибка дочернего процесса manager team {run_id}: {e}")
        except Exception:
            pass
        sys.exit(1)


# Глобальный реестр активных запусков (разделяемый между всеми экземплярами)
_GLOBAL_ACTIVE_RUNS = {}
_GLOBAL_RUN_CALLBACKS = {}
_GLOBAL_AGENT_PROCESSES = {}
_GLOBAL_AGENT_RESULT_QUEUES = {}
# Блокировки для потокобезопасного доступа к глобальным реестрам
_GLOBAL_RUNS_LOCK = threading.RLock()
_GLOBAL_PROCESSES_LOCK = threading.RLock()

# Типы для callbacks
AgentCallback = Callable[[str, str, Dict[str, Any]], None]  # (run_id, event_type, data)

@dataclass
class AgentProfile:
    """Информация о профиле агента"""
    name: str
    type: str = "code"  # code, tool_calling, multi_step
    description: str = ""
    model: str = ""
    model_key: str = ""
    tools: List[str] = None
    max_steps: int = 20
    planning_interval: Optional[Union[int, str]] = None
    memory_policy: Dict[str, Any] = None
    pipeline_prompts: Dict[str, str] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.tools is None:
            self.tools = []
        if self.memory_policy is None:
            self.memory_policy = {}
        if self.pipeline_prompts is None:
            self.pipeline_prompts = {}
        if self.metadata is None:
            self.metadata = {}
    def to_dict(self) -> Dict[str, Any]:
        """Возвращает JSON-сериализуемое представление объекта."""
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "model": self.model.model_id if hasattr(self.model, 'model_id') else str(self.model),
            "model_key": self.model_key,
            "tools": self.tools,
            "max_steps": self.max_steps,
            "planning_interval": self.planning_interval,
            "memory_policy": self.memory_policy,
            "pipeline_prompts": self.pipeline_prompts,
            "metadata": self.metadata,
        }

@dataclass
class AgentRunStatus:
    """Статус выполнения агента"""
    run_id: str
    agent_name: str
    profile_type: str
    status: str  # queued, running, completed, failed, cancelled
    task: str = ""
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None
    step_count: int = 0
    current_step: str = ""

@dataclass
class AgentRunResult:
    """Результат выполнения агента"""
    run_id: str
    agent_name: str
    final_output: Any = None
    steps_history: List[Dict[str, Any]] = None
    memory_entries: List[Dict[str, Any]] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.steps_history is None:
            self.steps_history = []
        if self.memory_entries is None:
            self.memory_entries = []
        if self.metadata is None:
            self.metadata = {}

@dataclass
class DynamicAgentDefinition:
    """Определение динамического агента"""
    name: str
    type: str = "code"
    description: str = ""
    model: str = ""
    tools: List[str] = None
    instructions: str = ""
    max_steps: int = 20
    planning_interval: Optional[Union[int, str]] = None
    memory_policy: Dict[str, Any] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.tools is None:
            self.tools = []
        if self.memory_policy is None:
            self.memory_policy = {}
        if self.metadata is None:
            self.metadata = {}

    def to_profile_dict(self) -> Dict[str, Any]:
        """Конвертирует в формат профиля агента"""
        return {
            "type": self.type,
            "description": self.description,
            "model": self.model,
            "tools": self.tools,
            "prompt_templates": self.instructions,
            "max_steps": self.max_steps,
            "planning_interval": self.planning_interval,
            "memory_policy": self.memory_policy,
            "metadata": self.metadata
        }


class AgentManager:
    """
    Менеджер для управления агентами через Streamlit UI
    Предоставляет единый интерфейс для работы со стандартными и динамическими агентами
    """
    
    def __init__(self, profiles_dir: str = "agent_profiles"):
        """
        Args:
            profiles_dir: Директория с YAML профилями агентов
        """
        self.profiles_dir = Path(profiles_dir)
        self.factory = AgentFactory()
        self.agent_system = DynamicAgentSystem()
        
        # Используем глобальные переменные для разделения состояния между экземплярами
        global _GLOBAL_ACTIVE_RUNS, _GLOBAL_RUN_CALLBACKS
        self.active_runs = _GLOBAL_ACTIVE_RUNS
        self.run_callbacks = _GLOBAL_RUN_CALLBACKS
        
        # Реестр динамических профилей (в памяти)
        self.dynamic_profiles: Dict[str, DynamicAgentDefinition] = {}
        
        logger.info("🤖 AgentManager инициализирован с глобальным состоянием")

    def list_agents(self) -> List[AgentProfile]:
        """
        Получить список доступных профилей агентов
        
        Returns:
            Список объектов AgentProfile
        """
        profiles = []
        
        # Загружаем стандартные профили из AGENT_PROFILES
        for profile_name, profile_data in AGENT_PROFILES.items():
            try:
                agent_profile = AgentProfile(
                    name=profile_name,
                    type=profile_data.get("type", "code"),
                    description=profile_data.get("description", ""),
                    model=profile_data.get("model", ""),
                    model_key=profile_data.get("model_key", "") or "",
                    tools=profile_data.get("tools", []),
                    max_steps=profile_data.get("max_steps", 20),
                    planning_interval=profile_data.get("planning_interval"),
                    memory_policy=profile_data.get("memory_policy", {}),
                    pipeline_prompts=profile_data.get("pipeline_prompts", {}),
                    metadata=profile_data.get("metadata", {})
                )
                profiles.append(agent_profile)
                
            except Exception as e:
                logger.warning(f"⚠️ Не удалось загрузить профиль {profile_name}: {e}")
        
        # Сортируем по имени
        profiles.sort(key=lambda x: x.name)
        
        logger.info(f"📋 Найдено {len(profiles)} профилей агентов")
        return profiles

    def get_agent_profile(self, profile_name: str) -> Optional[AgentProfile]:
        """
        Получить конкретный профиль агента
        
        Args:
            profile_name: Имя профиля
            
        Returns:
            Объект AgentProfile или None
        """
        if profile_name in AGENT_PROFILES:
            profile_data = AGENT_PROFILES[profile_name]
            return AgentProfile(
                name=profile_name,
                type=profile_data.get("type", "code"),
                description=profile_data.get("description", ""),
                model=profile_data.get("model", ""),
                model_key=profile_data.get("model_key", "") or "",
                tools=profile_data.get("tools", []),
                max_steps=profile_data.get("max_steps", 20),
                planning_interval=profile_data.get("planning_interval"),
                memory_policy=profile_data.get("memory_policy", {}),
                pipeline_prompts=profile_data.get("pipeline_prompts", {}),
                metadata=profile_data.get("metadata", {})
            )
        return None

    def create_agent(self, profile_name: str, session_id: Optional[str] = None) -> str:
        """
        Создать экземпляр агента из профиля
        
        Args:
            profile_name: Имя профиля агента
            session_id: ID сессии (генерируется автоматически если None)
            
        Returns:
            agent_id для дальнейшего использования
        """
        if session_id is None:
            session_id = str(uuid.uuid4())
            
        agent_id = f"{profile_name}_{uuid.uuid4().hex[:8]}"
        
        try:
            # Создаем агента через фабрику
            # Используем пустую задачу для создания, реальная задача будет в run_agent
            agent = self.factory.create_agent(
                profile_type=profile_name,
                session_id=session_id,
                task="placeholder_task",
                pipeline_type="general"
            )
            
            # Сохраняем информацию об агенте
            with _GLOBAL_RUNS_LOCK:
                self.active_runs[agent_id] = {
                    "agent": agent,
                    "profile_name": profile_name,
                    "session_id": session_id,
                    "status": "created",
                    "created_time": datetime.now()
                }

            logger.info(f"✅ Создан агент {agent_id} из профиля {profile_name}")
            return agent_id
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания агента {profile_name}: {e}")
            raise

    def run_agent(self, 
                  agent_id_or_profile: str,
                  task: str,
                  session_id: Optional[str] = None,
                  callback: Optional[AgentCallback] = None,
                  allow_thread_fallback: bool = False) -> str:
        """
        Запустить агента для выполнения задачи
        
        Args:
            agent_id_or_profile: ID созданного агента или имя профиля для быстрого создания
            task: Задача для выполнения
            session_id: ID сессии (если None, генерируется автоматически)
            callback: Функция для получения уведомлений о прогрессе
            
        Returns:
            run_id для отслеживания выполнения
        """
        if session_id is None:
            session_id = f"run-{uuid.uuid4().hex[:16]}"
        run_id = str(uuid.uuid4())

        # Определяем профиль агента (для процесса создадим внутри)
        with _GLOBAL_RUNS_LOCK:
            agent_data = self.active_runs.get(agent_id_or_profile)
        if agent_data:
            profile_name = agent_data["profile_name"]
        else:
            profile_name = agent_id_or_profile

        # Регистрируем callback если предоставлен
        if callback:
            self.run_callbacks[run_id] = [callback]

        # Запускаем в отдельном процессе для возможности реальной отмены
        try:
            from multiprocessing import Process, Queue
            result_queue = Queue(maxsize=1)
            proc = Process(target=_agent_process_entry, args=(run_id, profile_name, task, session_id, result_queue), daemon=True)
            proc.start()

            # Регистрируем активный запуск с PID
            with _GLOBAL_RUNS_LOCK:
                self.active_runs[run_id] = {
                    "profile_name": profile_name,
                    "status": "running",
                    "task": task,
                    "session_id": session_id,
                    "start_time": datetime.now(),
                    "step_count": 0,
                    "pid": proc.pid,
                }
            with _GLOBAL_PROCESSES_LOCK:
                _GLOBAL_AGENT_PROCESSES[run_id] = proc
                _GLOBAL_AGENT_RESULT_QUEUES[run_id] = result_queue

            def _watchdog(_rid: str):
                with _GLOBAL_PROCESSES_LOCK:
                    p = _GLOBAL_AGENT_PROCESSES.get(_rid)
                if not p:
                    return
                p.join()
                try:
                    exit_code = p.exitcode
                    with _GLOBAL_PROCESSES_LOCK:
                        child_result_queue = _GLOBAL_AGENT_RESULT_QUEUES.pop(_rid, None)
                    child_result = None
                    if child_result_queue is not None:
                        try:
                            child_result = child_result_queue.get(timeout=0.2)
                        except Exception:
                            child_result = None
                    child_status = child_result.get("status") if isinstance(child_result, dict) else None
                    child_error = child_result.get("error") if isinstance(child_result, dict) else None
                    with _GLOBAL_RUNS_LOCK:
                        run_data = self.active_runs.get(_rid)
                        if not run_data:
                            return
                        if run_data.get("status") in ["completed", "failed", "cancelled"]:
                            return
                        run_data.update({
                            "end_time": datetime.now(),
                            "status": "completed" if exit_code == 0 and child_status != "failed" else "failed",
                            "error": child_error if child_error else (None if exit_code == 0 else f"Процесс завершился с кодом {exit_code}"),
                        })
                        if isinstance(child_result, dict) and "result" in child_result:
                            run_data["result"] = child_result["result"]
                        _notify_status = run_data["status"]
                        _notify_error = run_data.get("error")
                    self._notify_callback(_rid, _notify_status, {"error": _notify_error})
                except Exception:
                    pass

            watcher = threading.Thread(target=_watchdog, args=(run_id,), daemon=True)
            watcher.start()
        except Exception as e:
            if not allow_thread_fallback:
                message = f"Не удалось запустить агента в отдельном процессе: {e}"
                logger.error("❌ %s", _redact_text(message))
                raise RuntimeError(message) from e
            logger.warning(f"⚠️ Не удалось запустить агента в отдельном процессе, используем поток: {_redact_text(str(e))}")
            # Fallback на старый потоковый запуск
            try:
                agent = self.factory.create_agent(
                    profile_type=profile_name,
                    session_id=session_id,
                    task=task,
                    pipeline_type="general"
                )
            except Exception as ce:
                logger.error(f"❌ Ошибка создания агента {profile_name}: {ce}")
                raise
            thread = threading.Thread(
                target=self._run_agent_thread,
                args=(run_id, agent, profile_name, task, session_id)
            )
            thread.daemon = True
            thread.start()

        logger.info(f"🚀 Запущен агент из профиля {profile_name} с run_id: {run_id}")
        return run_id

    def _run_agent_thread(self, run_id: str, agent, profile_name: str, task: str, session_id: str, enable_telemetry: bool = False, enable_memory: bool = False):
        """Выполнение агента в отдельном потоке"""
        try:
            # Настраиваем корневой span телеметрии для этого запуска
            # Use enable_telemetry parameter
            if enable_telemetry:
                try:
                    from telemetry import get_telemetry_manager
                    telemetry_manager = get_telemetry_manager()
                except Exception:
                    telemetry_manager = None
            else:
                telemetry_manager = None

            # Регистрируем начальное состояние
            # Создаем запись, если её еще нет
            with _GLOBAL_RUNS_LOCK:
                if run_id not in self.active_runs:
                    self.active_runs[run_id] = {}
                self.active_runs[run_id].update({ # Use update to add new fields
                    "profile_name": profile_name,
                    "status": "running",
                    "task": task,
                    "session_id": session_id,
                    "start_time": datetime.now(),
                    "step_count": 0,
                    "enable_telemetry": enable_telemetry, # Store options for monitoring
                    "enable_memory": enable_memory,
                })
            
            self._notify_callback(run_id, "started", {
                "profile_name": profile_name,
                "task": task,
                "enable_telemetry": enable_telemetry,
                "enable_memory": enable_memory,
            })
            
            # КРИТИЧНО: Устанавливаем run_id_context ПЕРЕД созданием корневого span
            try:
                from unified_logging import run_id_context
                with run_id_context(run_id):
                    # Создаём корневой span уже внутри run_id_context
                    root_span = None
                    if telemetry_manager and telemetry_manager.is_enabled(): # Check if manager is enabled AND if telemetry is requested
                        root_span = telemetry_manager.start_run_trace(
                            run_id=run_id,
                            agent_name=getattr(agent, 'name', profile_name),
                            task=task,
                            profile_type=profile_name,
                            pipeline_name="general",
                            session_id=session_id,
                        )
                    # КРИТИЧНО: Выполняем задачу в контексте корневого span
                    if root_span is not None:
                        from opentelemetry import trace
                        with trace.use_span(root_span):
                            result = agent.run(task)
                    else:
                        result = agent.run(task)
                    
                    # Успешное завершение: пишем итог в атрибуты и закрываем span
                    if root_span is not None:
                        try:
                            if isinstance(result, (dict, list)):
                                root_span.set_attribute("output.mime_type", "application/json")
                                root_span.set_attribute("output.value", json.dumps(result, ensure_ascii=False, default=str))
                            elif isinstance(result, str):
                                root_span.set_attribute("output.mime_type", "text/plain")
                                root_span.set_attribute("output.value", result)
                            elif result is not None:
                                root_span.set_attribute("output.mime_type", "text/plain")
                                root_span.set_attribute("output.value", str(result))
                        except Exception:
                            pass
                        telemetry_manager.finish_run_trace(root_span, success=True)
                    
            except ImportError:
                # Fallback если run_id_context недоступен
                root_span = None
                if telemetry_manager and telemetry_manager.is_enabled():
                    root_span = telemetry_manager.start_run_trace(
                        run_id=run_id,
                        agent_name=getattr(agent, 'name', profile_name),
                        task=task,
                        profile_type=profile_name,
                        pipeline_name="general",
                        session_id=session_id,
                    )
                
                if root_span is not None:
                    from opentelemetry import trace
                    with trace.use_span(root_span):
                        result = agent.run(task)
                    try:
                        if isinstance(result, (dict, list)):
                            root_span.set_attribute("output.mime_type", "application/json")
                            root_span.set_attribute("output.value", json.dumps(result, ensure_ascii=False, default=str))
                        elif isinstance(result, str):
                            root_span.set_attribute("output.mime_type", "text/plain")
                            root_span.set_attribute("output.value", result)
                        elif result is not None:
                            root_span.set_attribute("output.mime_type", "text/plain")
                            root_span.set_attribute("output.value", str(result))
                    except Exception:
                        pass
                    telemetry_manager.finish_run_trace(root_span, success=True)
                else:
                    result = agent.run(task)
                    
            except Exception as run_err:
                # Ошибка выполнения
                if root_span is not None:
                    telemetry_manager.finish_run_trace(root_span, success=False, error_message=str(run_err))
                raise
            
            # Сохраняем результат
            # Создаем запись, если её еще нет
            with _GLOBAL_RUNS_LOCK:
                if run_id not in self.active_runs:
                    self.active_runs[run_id] = {}
                self.active_runs[run_id].update({
                    "status": "completed",
                    "end_time": datetime.now(),
                    "result": result
                })
            
            self._notify_callback(run_id, "completed", {
                "result": str(result) if result else None
            })
            
        except Exception as e:
            # Сохраняем ошибку
            # Создаем запись, если её еще нет
            with _GLOBAL_RUNS_LOCK:
                if run_id not in self.active_runs:
                    self.active_runs[run_id] = {}
                self.active_runs[run_id].update({
                    "status": "failed",
                    "end_time": datetime.now(),
                    "error": str(e)
                })

            self._notify_callback(run_id, "failed", {
                "error": str(e)
            })

            logger.error(f"❌ Ошибка выполнения агента {run_id}: {e}")

    def _notify_callback(self, run_id: str, event_type: str, data: Dict[str, Any]):
        """Уведомление EventBus о событии"""
        # Всегда записываем событие в статус для мониторинга
        with _GLOBAL_RUNS_LOCK:
            if run_id in self.active_runs:
                self.active_runs[run_id][f"last_{event_type}"] = {
                    "timestamp": datetime.now(),
                    "data": data
                }
        
        # Получаем EventBus и отправляем ProgressEvent
        try:
            from unified_logging import get_logging_manager
            event_bus = get_logging_manager().event_bus
            event_bus.emit_progress(run_id, event_type, "agent", data)
        except Exception as e:
            logger.error(f"❌ Ошибка отправки события '{event_type}' в EventBus для run_id '{run_id}': {e}", exc_info=True)


    def get_agent_events(self, run_id: str) -> Dict[str, Any]:
        """
        Получить события агента из многопоточной среды
        
        Args:
            run_id: Идентификатор запуска
            
        Returns:
            Словарь с событиями агента
        """
        with _GLOBAL_RUNS_LOCK:
            if run_id not in self.active_runs:
                return {}
            run_data = dict(self.active_runs[run_id])
        events = {}
        
        # Извлекаем все события, записанные в статус
        for key, value in run_data.items():
            if key.startswith("last_"):
                event_type = key[5:]  # Убираем "last_"
                events[event_type] = value
                
        return events

    def get_agent_status(self, run_id: str) -> Optional[AgentRunStatus]:
        """
        Получить статус выполнения агента
        
        Args:
            run_id: Идентификатор запуска
            
        Returns:
            Объект AgentRunStatus или None
        """
        with _GLOBAL_RUNS_LOCK:
            if run_id not in self.active_runs:
                return None
            run_data = dict(self.active_runs[run_id])
        
        duration = None
        if run_data.get("end_time"):
            duration = (run_data["end_time"] - run_data["start_time"]).total_seconds()
            
        return AgentRunStatus(
            run_id=run_id,
            agent_name=run_data.get("profile_name", "unknown"),
            profile_type=run_data.get("profile_name", "unknown"),
            status=run_data["status"],
            task=run_data.get("task", ""),
            start_time=run_data.get("start_time"),
            end_time=run_data.get("end_time"),
            duration_seconds=duration,
            error_message=run_data.get("error"),
            step_count=run_data.get("step_count", 0),
            current_step=run_data.get("current_step", "")
        )

    def get_agent_result(self, run_id: str) -> Optional[AgentRunResult]:
        """
        Получить результат выполнения агента
        
        Args:
            run_id: Идентификатор запуска
            
        Returns:
            Объект AgentRunResult или None
        """
        with _GLOBAL_RUNS_LOCK:
            if run_id not in self.active_runs:
                return None
            run_data = dict(self.active_runs[run_id])
        
        result = AgentRunResult(
            run_id=run_id,
            agent_name=run_data.get("profile_name", "unknown"),
            final_output=run_data.get("result"),
            metadata={
                "session_id": run_data.get("session_id"),
                "start_time": run_data.get("start_time").isoformat() if run_data.get("start_time") else None,
                "end_time": run_data.get("end_time").isoformat() if run_data.get("end_time") else None
            }
        )
        
        return result

    def list_active_run_snapshots(self) -> List[Tuple[str, Dict[str, Any]]]:
        with _GLOBAL_RUNS_LOCK:
            return [
                (run_id, dict(run_data))
                for run_id, run_data in self.active_runs.items()
                if isinstance(run_data, dict)
            ]

    # === Динамические агенты ===
    
    def register_dynamic_profile(self, name: str, definition: DynamicAgentDefinition) -> bool:
        """
        Зарегистрировать динамический профиль агента
        
        Args:
            name: Уникальное имя профиля
            definition: Определение агента
            
        Returns:
            True если успешно зарегистрирован
        """
        try:
            self.dynamic_profiles[name] = definition
            logger.info(f"📝 Зарегистрирован динамический профиль: {name}")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка регистрации динамического профиля {name}: {e}")
            return False

    def list_dynamic_profiles(self) -> List[DynamicAgentDefinition]:
        """
        Получить список динамических профилей
        
        Returns:
            Список объектов DynamicAgentDefinition
        """
        return list(self.dynamic_profiles.values())

    def create_dynamic_agent(self, 
                           definition: DynamicAgentDefinition,
                           session_id: Optional[str] = None) -> str:
        """
        Создать агента из динамического определения
        
        Args:
            definition: Определение агента
            session_id: ID сессии
            
        Returns:
            agent_id
        """
        if session_id is None:
            session_id = str(uuid.uuid4())
            
        agent_id = f"dynamic_{definition.name}_{uuid.uuid4().hex[:8]}"
        
        try:
            agent = self.factory.create_agent(
                profile_type=definition.name,
                session_id=session_id,
                task="placeholder_task",
                pipeline_type="general",
                profile_override=definition.to_profile_dict(),
            )

            # Сохраняем информацию об агенте
            with _GLOBAL_RUNS_LOCK:
                self.active_runs[agent_id] = {
                    "agent": agent,
                    "profile_name": definition.name,
                    "session_id": session_id,
                    "status": "created",
                    "created_time": datetime.now(),
                    "is_dynamic": True,
                    "definition": definition
                }

            logger.info(f"✅ Создан динамический агент {agent_id}")
            return agent_id

        except Exception as e:
            logger.error(f"❌ Ошибка создания динамического агента: {e}")
            raise

    def run_manager_with_team(self,
                            manager_definition_or_name: Union[str, DynamicAgentDefinition],
                            team_definitions_or_names: List[Union[str, DynamicAgentDefinition]],
                            task: str,
                            session_id: Optional[str] = None,
                            callback: Optional[AgentCallback] = None) -> str:
        """
        Запустить менеджера с предзагруженной командой
        
        Args:
            manager_definition_or_name: Определение или имя профиля менеджера
            team_definitions_or_names: Список определений или имен профилей команды
            task: Задача для выполнения
            session_id: ID сессии
            callback: Функция для получения уведомлений
            
        Returns:
            run_id для отслеживания выполнения
        """
        if session_id is None:
            session_id = f"run-{uuid.uuid4().hex[:16]}"
        run_id = str(uuid.uuid4())

        # Регистрируем callback если предоставлен
        if callback:
            self.run_callbacks[run_id] = [callback]

        # Запускаем в отдельном процессе для корректной отмены
        try:
            from multiprocessing import Process
            def _serialize_def(item):
                if isinstance(item, DynamicAgentDefinition):
                    return asdict(item)
                return item

            manager_payload = _serialize_def(manager_definition_or_name)
            team_payloads = [_serialize_def(item) for item in team_definitions_or_names]

            proc = Process(
                target=_manager_team_process_entry,
                args=(run_id, manager_payload, team_payloads, task, session_id),
                daemon=True,
            )
            proc.start()

            # Регистрируем начальное состояние с PID
            with _GLOBAL_RUNS_LOCK:
                self.active_runs[run_id] = {
                    "manager_profile": manager_definition_or_name if isinstance(manager_definition_or_name, str) else "manager",
                    "team_profiles": [td if isinstance(td, str) else getattr(td, 'name', 'unknown') for td in team_definitions_or_names],
                    "profile_name": f"Команда: {manager_definition_or_name if isinstance(manager_definition_or_name, str) else 'manager'} + {len(team_definitions_or_names)} агентов",
                    "status": "running",
                    "task": task,
                    "session_id": session_id,
                    "start_time": datetime.now(),
                    "step_count": 0,
                    "pid": proc.pid,
                }

            def _watchdog(_rid: str):
                with _GLOBAL_PROCESSES_LOCK:
                    p = _GLOBAL_AGENT_PROCESSES.get(_rid)
                if not p:
                    return
                p.join()
                try:
                    # exit_code читаем вне лока (как в watchdog одиночного агента):
                    # runs-lock защищает только active_runs. Терминальный статус
                    # (включая cancelled) перепроверяем под локом перед записью,
                    # чтобы не затереть отмену/иной финальный статус от другого потока.
                    exit_code = p.exitcode
                    with _GLOBAL_RUNS_LOCK:
                        run_data = self.active_runs.get(_rid)
                        if not run_data:
                            return
                        if run_data.get("status") in ["completed", "failed", "cancelled"]:
                            return
                        run_data.update({
                            "end_time": datetime.now(),
                            "status": "completed" if exit_code == 0 else "failed",
                            "error": None if exit_code == 0 else f"Процесс завершился с кодом {exit_code}",
                        })
                        _notify_status = run_data["status"]
                        _notify_error = run_data.get("error")
                    self._notify_callback(_rid, _notify_status, {"error": _notify_error})
                except Exception:
                    pass

            with _GLOBAL_PROCESSES_LOCK:
                _GLOBAL_AGENT_PROCESSES[run_id] = proc
            watcher = threading.Thread(target=_watchdog, args=(run_id,), daemon=True)
            watcher.start()

        except Exception as e:
            logger.warning(f"⚠️ Не удалось запустить manager team в отдельном процессе, используем поток: {e}")
            thread = threading.Thread(
                target=self._run_manager_with_team_thread,
                args=(run_id, manager_definition_or_name, team_definitions_or_names, task, session_id)
            )
            thread.daemon = True
            thread.start()

        logger.info(f"👨‍💼 Запущен менеджер с командой, run_id: {run_id}")
        return run_id

    @staticmethod
    def _run_coroutine_in_new_loop(coro):
        """Безопасно выполняет coroutine в потоке, где ещё нет event loop.

        Создаёт свежий loop, гарантирует отмену висящих тасков и корректное
        закрытие loop даже при исключении. asyncio.run() может упасть с
        RuntimeError, если в потоке уже активен другой loop.
        """
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                logger.debug("Ошибка при отмене pending-тасков в потоке менеджера", exc_info=True)
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    def _run_manager_with_team_thread(self, run_id: str, manager_def, team_defs, task: str, session_id: str):
        """Выполнение менеджера с командой в отдельном потоке"""
        telemetry_manager = None
        root_span = None
        result = None

        # Подавляем предупреждения Streamlit в контексте потока.
        # Блок работы должен быть ВНУТРИ with, иначе фильтры снимаются до выполнения.
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message='.*missing ScriptRunContext.*')
            warnings.filterwarnings('ignore', message='.*This warning can be ignored when running in bare mode.*')
            try:
                # Преобразуем определения в имена профилей
                manager_profile = manager_def if isinstance(manager_def, str) else "manager"
                team_profiles = []

                for team_def in team_defs:
                    if isinstance(team_def, str):
                        team_profiles.append(team_def)
                    else:
                        team_profiles.append(team_def.name)

                # Регистрируем начальное состояние
                with _GLOBAL_RUNS_LOCK:
                    self.active_runs[run_id] = {
                        "manager_profile": manager_profile,
                        "team_profiles": team_profiles,
                        "profile_name": f"Команда: {manager_profile} + {len(team_profiles)} агентов",
                        "status": "running",
                        "task": task,
                        "session_id": session_id,
                        "start_time": datetime.now(),
                        "step_count": 0
                    }

                self._notify_callback(run_id, "started", {
                    "manager_profile": manager_profile,
                    "team_profiles": team_profiles,
                    "task": task
                })

                # Настраиваем телеметрию
                try:
                    from telemetry import get_telemetry_manager
                    telemetry_manager = get_telemetry_manager()
                except Exception:
                    telemetry_manager = None

                # Устанавливаем run_id_context для корректной телеметрии
                try:
                    from unified_logging import run_id_context
                    with run_id_context(run_id):
                        # Создаём корневой span для команды менеджера
                        if telemetry_manager and telemetry_manager.is_enabled():
                            root_span = telemetry_manager.start_run_trace(
                                run_id=run_id,
                                agent_name=manager_profile,
                                task=task,
                                profile_type="team_manager",
                                pipeline_name=f"team_{len(team_profiles)}_agents",
                                session_id=session_id,
                            )

                        # Выполняем в контексте корневого span
                        coro_factory = lambda: self.agent_system.coordinate(
                            initial_task=task,
                            session_id=run_id,
                            show=False,
                            preload_agents=team_profiles,
                        )
                        if root_span is not None:
                            from opentelemetry import trace
                            with trace.use_span(root_span):
                                result = self._run_coroutine_in_new_loop(coro_factory())
                        else:
                            result = self._run_coroutine_in_new_loop(coro_factory())

                except ImportError:
                    # run_id_context недоступен — работаем без него
                    result = self._run_coroutine_in_new_loop(
                        self.agent_system.coordinate(
                            initial_task=task,
                            session_id=run_id,
                            show=False,
                            preload_agents=team_profiles,
                        )
                    )

                # Успешное завершение span
                if root_span is not None and telemetry_manager:
                    try:
                        if isinstance(result, (dict, list)):
                            root_span.set_attribute("output.mime_type", "application/json")
                            root_span.set_attribute("output.value", json.dumps(result, ensure_ascii=False, default=str))
                        elif isinstance(result, str):
                            root_span.set_attribute("output.mime_type", "text/plain")
                            root_span.set_attribute("output.value", result)
                        elif result is not None:
                            root_span.set_attribute("output.mime_type", "text/plain")
                            root_span.set_attribute("output.value", str(result))
                    except Exception:
                        logger.debug("Не удалось записать output-атрибуты в root_span", exc_info=True)
                    telemetry_manager.finish_run_trace(root_span, success=True)

                # Сохраняем результат
                with _GLOBAL_RUNS_LOCK:
                    self.active_runs[run_id].update({
                        "status": "completed",
                        "end_time": datetime.now(),
                        "result": result
                    })

                self._notify_callback(run_id, "completed", {
                    "result": str(result) if result else None
                })

            except Exception as e:
                # Завершение span с ошибкой
                if root_span is not None and telemetry_manager:
                    telemetry_manager.finish_run_trace(root_span, success=False, error_message=str(e))

                # Сохраняем ошибку
                with _GLOBAL_RUNS_LOCK:
                    self.active_runs[run_id].update({
                        "status": "failed",
                        "end_time": datetime.now(),
                        "error": str(e)
                    })

                self._notify_callback(run_id, "failed", {
                    "error": str(e)
                })

                logger.error(f"❌ Ошибка выполнения менеджера с командой {run_id}: {e}")

    def cancel_agent_run(self, run_id: str) -> bool:
        """
        Отменить выполнение агента
        
        Args:
            run_id: Идентификатор запуска
            
        Returns:
            True если отмена успешна
        """
        with _GLOBAL_RUNS_LOCK:
            if run_id not in self.active_runs:
                return False
            run_data = self.active_runs[run_id]
            if run_data["status"] in ["completed", "failed", "cancelled"]:
                return False
            pid = run_data.get("pid")

        # Пытаемся завершить дочерний процесс, если он запущен
        with _GLOBAL_PROCESSES_LOCK:
            proc = _GLOBAL_AGENT_PROCESSES.get(run_id)
        killed = False
        if proc is not None:
            try:
                # Сначала мягкое завершение всей группы процессов
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
                proc.join(timeout=5.0)
                if proc.is_alive():
                    # Пытаемся завершить всё дерево процессов (если есть)
                    try:
                        import psutil  # type: ignore
                        p = psutil.Process(proc.pid)
                        children = p.children(recursive=True)
                        for ch in children:
                            try:
                                ch.terminate()
                            except Exception:
                                pass
                        _, alive = psutil.wait_procs(children, timeout=3)
                        for ch in alive:
                            try:
                                ch.kill()
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # Жёсткое завершение самого процесса
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    proc.join(timeout=3.0)
                killed = not proc.is_alive()
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при завершении процесса агента {pid}: {e}")
        elif pid:
            # Фолбэк на прямые сигналы по PID
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        killed = True
                        break
                if not killed:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
                    else:
                        # Дадим ОС время применить SIGKILL
                        time.sleep(0.2)
                        try:
                            os.kill(pid, 0)
                        except OSError:
                            killed = True
            except ProcessLookupError:
                killed = True
            except Exception as e:
                logger.warning(f"⚠️ Не удалось послать сигнал процессу {pid}: {e}")

        # Помечаем как отмененный (повторная проверка под блокировкой, чтобы не перезаписать
        # финальный статус, выставленный watchdog-потоком между двумя окнами блокировки)
        with _GLOBAL_RUNS_LOCK:
            run_data = self.active_runs.get(run_id)
            if run_data is None or run_data.get("status") in ["completed", "failed", "cancelled"]:
                return False
            run_data.update({
                "status": "cancelled",
                "end_time": datetime.now()
            })

        # Очищаем реестр процесса
        with _GLOBAL_PROCESSES_LOCK:
            _GLOBAL_AGENT_PROCESSES.pop(run_id, None)

        self._notify_callback(run_id, "cancelled", {})
        
        logger.info(f"🛑 Агент {run_id} отменен{'' if killed else ' (процесс мог не завершиться мгновенно)'}")
        return True

    def cleanup_completed_runs(self, max_age_hours: int = 24):
        """
        Очистка завершенных запусков старше указанного времени
        
        Args:
            max_age_hours: Максимальный возраст в часах
        """
        current_time = datetime.now()
        to_remove = []

        with _GLOBAL_RUNS_LOCK:
            for run_id, run_data in list(self.active_runs.items()):
                if run_data["status"] in ["completed", "failed", "cancelled"]:
                    end_time = run_data.get("end_time", run_data.get("start_time", current_time))
                    age_hours = (current_time - end_time).total_seconds() / 3600
                    if age_hours > max_age_hours:
                        to_remove.append(run_id)

            for run_id in to_remove:
                del self.active_runs[run_id]
                self.run_callbacks.pop(run_id, None)

        with _GLOBAL_PROCESSES_LOCK:
            for run_id in to_remove:
                _GLOBAL_AGENT_PROCESSES.pop(run_id, None)

        if to_remove:
            logger.info(f"🧹 Очищено {len(to_remove)} старых запусков агентов")
