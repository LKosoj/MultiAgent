"""
Единая система логирования с run_id и событийной шиной
====================================================

Расширяет существующую систему логирования единообразным run_id
и предоставляет событийную шину для отслеживания прогресса в Streamlit.
"""

import logging
import os
import codecs
from contextlib import contextmanager
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, asdict
import threading
from queue import Queue, Empty
import json
from pathlib import Path
import asyncio
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Типы для callbacks
ProgressCallback = Callable[[str, str, Dict[str, Any]], None]  # (run_id, event_type, data)
LogCallback = Callable[[str, str, str, str], None]  # (run_id, level, message, timestamp)
SystemMetricCallback = Callable[[Dict[str, Any]], None] # (data)

# Потокобезопасное хранилище для run_id (каждый поток имеет свой run_id)
_thread_local = threading.local()
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
_SENSITIVE_SCALAR_KEYS = _SENSITIVE_DSN_QUERY_KEYS
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


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_SCALAR_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value

@dataclass
class LogEvent:
    """Событие логирования с run_id и корреляцией с OpenTelemetry спанами"""
    run_id: str
    timestamp: datetime
    level: str
    logger_name: str
    message: str
    extra_data: Dict[str, Any] = None
    span_id: Optional[str] = None
    trace_id: Optional[str] = None

    def __post_init__(self):
        if self.extra_data is None:
            self.extra_data = {}

    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь для JSON экспорта"""
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp.isoformat(),
            "level": self.level,
            "logger_name": self.logger_name,
            "message": self.message,
            "extra_data": self.extra_data,
            "span_id": self.span_id,
            "trace_id": self.trace_id
        }

@dataclass
class ProgressEvent:
    """Событие прогресса выполнения"""
    run_id: str
    timestamp: datetime
    event_type: str  # started, step, progress, completed, failed, cancelled
    component: str   # workflow, agent, step_name, etc.
    data: Dict[str, Any] = None

    def __post_init__(self):
        if self.data is None:
            self.data = {}

    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь"""
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type,
            "component": self.component,
            "data": self.data
        }

@dataclass
class SystemMetricEvent:
    """Событие системной метрики"""
    timestamp: datetime
    metric_type: str # cpu_usage, memory_usage, disk_usage, etc.
    data: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь"""
        return {
            "timestamp": self.timestamp.isoformat(),
            "metric_type": self.metric_type,
            "data": self.data
        }


class RunIdLoggerAdapter(logging.LoggerAdapter):
    """
    Адаптер логгера, автоматически добавляющий run_id в сообщения
    """
    
    def __init__(self, logger: logging.Logger, run_id: str):
        super().__init__(logger, {"run_id": run_id})
        self.run_id = run_id

    def process(self, msg, kwargs):
        """Добавляем run_id в extra"""
        extra = kwargs.get('extra', {})
        extra['run_id'] = self.run_id
        kwargs['extra'] = extra
        return f"[{self.run_id[:8]}] {msg}", kwargs


def _run_async_callback_in_thread(coro_func, *args, **kwargs):
    """
    Выполняет async callback в основном event loop через run_coroutine_threadsafe.
    Если основного event loop нет, создает новый в отдельном потоке.
    """
    try:
        # Try to get the main event loop (FastAPI's loop)
        main_loop = None
        try:
            # Try to get any running loop
            main_loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, try to get the default loop
            try:
                main_loop = asyncio.get_event_loop()
                if main_loop.is_closed():
                    main_loop = None
            except RuntimeError:
                main_loop = None
        
        if main_loop is not None and main_loop.is_running():
            # We have a running loop, schedule the coroutine in it
            coro = coro_func(*args, **kwargs)
            asyncio.run_coroutine_threadsafe(coro, main_loop)
        else:
            # No running loop, create a new one in a separate thread
            def run_in_new_loop():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        # coro_func is a coroutine function, so we need to call it to get a coroutine
                        coro = coro_func(*args, **kwargs)
                        loop.run_until_complete(coro)
                    finally:
                        loop.close()
                except Exception as e:
                    logging.getLogger(__name__).error(f"Ошибка выполнения async callback в отдельном потоке: {e}", exc_info=True)
            
            thread = threading.Thread(target=run_in_new_loop, daemon=True)
            thread.start()
    except Exception as e:
        logging.getLogger(__name__).error(f"Ошибка при попытке выполнить async callback: {e}", exc_info=True)


class EventBus:
    """
    Событийная шина для отслеживания прогресса и логов, а также системных метрик.
    """
    
    def __init__(self):
        self.log_subscribers: Dict[str, List[LogCallback]] = {}
        self.progress_subscribers: Dict[str, List[ProgressCallback]] = {}
        self.system_metric_subscribers: List[SystemMetricCallback] = [] # Global subscribers for system metrics

        self.log_queue = Queue()
        self.progress_queue = Queue()
        self.system_metric_queue = Queue()

        self._running = True
        self._lock = threading.Lock()
        # Удерживаем ссылки на создаваемые asyncio.Task, чтобы GC не убил их до завершения
        # и чтобы исключения в callback'ах корректно логировались.
        self._background_tasks: "set[asyncio.Task]" = set()

        # Запускаем обработчики в отдельных потоках
        self._start_processors()

    def _schedule_async_callback(self, loop: "asyncio.AbstractEventLoop", coro) -> None:
        task = loop.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _start_processors(self):
        """Запуск обработчиков событий"""
        log_thread = threading.Thread(target=self._process_log_events, daemon=True)
        log_thread.start()
        
        progress_thread = threading.Thread(target=self._process_progress_events, daemon=True)
        progress_thread.start()

        system_metric_thread = threading.Thread(target=self._process_system_metric_events, daemon=True)
        system_metric_thread.start()


    def _process_log_events(self):
        """Обработка событий логирования"""
        while self._running:
            try:
                event = self.log_queue.get(timeout=1.0)
                self._dispatch_log_event(event)
            except Empty:
                continue
            except Exception as e:
                logging.getLogger(__name__).error(f"Ошибка обработки log события: {e}")

    def _process_progress_events(self):
        """Обработка событий прогресса"""
        while self._running:
            try:
                event = self.progress_queue.get(timeout=1.0)
                self._dispatch_progress_event(event)
            except Empty:
                continue
            except Exception as e:
                logging.getLogger(__name__).error(f"Ошибка обработки progress события: {e}")

    def _process_system_metric_events(self):
        """Обработка событий системных метрик"""
        while self._running:
            try:
                event = self.system_metric_queue.get(timeout=1.0)
                self._dispatch_system_metric_event(event)
            except Empty:
                continue
            except Exception as e:
                logging.getLogger(__name__).error(f"Ошибка обработки system metric события: {e}")

    def _dispatch_log_event(self, event: LogEvent):
        """Рассылка события логирования подписчикам"""
        with self._lock:
            # Отправляем всем подписчикам на конкретный run_id
            if event.run_id in self.log_subscribers:
                for callback in self.log_subscribers[event.run_id]:
                    try:
                        # Check if callback is a coroutine function
                        if asyncio.iscoroutinefunction(callback):
                            # Try to get the current event loop
                            try:
                                loop = asyncio.get_running_loop()
                                # If we have a running loop, schedule the coroutine
                                self._schedule_async_callback(loop, callback(event.run_id, event.level, event.message, event.timestamp.isoformat()))
                            except RuntimeError:
                                # No running event loop, execute in a separate thread with new event loop
                                _run_async_callback_in_thread(callback, event.run_id, event.level, event.message, event.timestamp.isoformat())
                        else:
                            callback(event.run_id, event.level, event.message, event.timestamp.isoformat())
                    except Exception as e:
                        logging.getLogger(__name__).error(f"Ошибка в log callback: {e}", exc_info=True)

            # Отправляем всем "глобальным" подписчикам (run_id = "*")
            if "*" in self.log_subscribers:
                for callback in self.log_subscribers["*"]:
                    try:
                        # Check if callback is a coroutine function
                        if asyncio.iscoroutinefunction(callback):
                            # Try to get the current event loop
                            try:
                                loop = asyncio.get_running_loop()
                                # If we have a running loop, schedule the coroutine
                                self._schedule_async_callback(loop, callback(event.run_id, event.level, event.message, event.timestamp.isoformat()))
                            except RuntimeError:
                                # No running event loop, execute in a separate thread with new event loop
                                _run_async_callback_in_thread(callback, event.run_id, event.level, event.message, event.timestamp.isoformat())
                        else:
                            callback(event.run_id, event.level, event.message, event.timestamp.isoformat())
                    except Exception as e:
                        logging.getLogger(__name__).error(f"Ошибка в global log callback: {e}", exc_info=True)

    def _dispatch_progress_event(self, event: ProgressEvent):
        """Рассылка события прогресса подписчикам"""
        with self._lock:
            # Отправляем всем подписчикам на конкретный run_id
            if event.run_id in self.progress_subscribers:
                for callback in self.progress_subscribers[event.run_id]:
                    try:
                        # Check if callback is a coroutine function
                        if asyncio.iscoroutinefunction(callback):
                            # Try to get the current event loop
                            try:
                                loop = asyncio.get_running_loop()
                                # If we have a running loop, schedule the coroutine
                                self._schedule_async_callback(loop, callback(event.run_id, event.event_type, event.data))
                            except RuntimeError:
                                # No running event loop, execute in a separate thread with new event loop
                                _run_async_callback_in_thread(callback, event.run_id, event.event_type, event.data)
                        else:
                            callback(event.run_id, event.event_type, event.data)
                    except Exception as e:
                        logging.getLogger(__name__).error(f"Ошибка в progress callback: {e}", exc_info=True)

            # Отправляем всем "глобальным" подписчикам (run_id = "*")
            if "*" in self.progress_subscribers:
                for callback in self.progress_subscribers["*"]:
                    try:
                        # Check if callback is a coroutine function
                        if asyncio.iscoroutinefunction(callback):
                            # Try to get the current event loop
                            try:
                                loop = asyncio.get_running_loop()
                                # If we have a running loop, schedule the coroutine
                                self._schedule_async_callback(loop, callback(event.run_id, event.event_type, event.data))
                            except RuntimeError:
                                # No running event loop, execute in a separate thread with new event loop
                                _run_async_callback_in_thread(callback, event.run_id, event.event_type, event.data)
                        else:
                            callback(event.run_id, event.event_type, event.data)
                    except Exception as e:
                        logging.getLogger(__name__).error(f"Ошибка в global progress callback: {e}", exc_info=True)

    def _dispatch_system_metric_event(self, event: SystemMetricEvent):
        """Рассылка события системных метрик подписчикам"""
        with self._lock:
            for callback in self.system_metric_subscribers:
                try:
                    # Check if callback is a coroutine function
                    if asyncio.iscoroutinefunction(callback):
                        # Try to get the current event loop
                        try:
                            loop = asyncio.get_running_loop()
                            # If we have a running loop, schedule the coroutine
                            self._schedule_async_callback(loop, callback(event.to_dict()))
                        except RuntimeError:
                            # No running event loop, execute in a separate thread with new event loop
                            _run_async_callback_in_thread(callback, event.to_dict())
                    else:
                        callback(event.to_dict())
                except Exception as e:
                    logging.getLogger(__name__).error(f"Ошибка в system metric callback: {e}", exc_info=True)


    def subscribe_logs(self, run_id: str, callback: LogCallback):
        """
        Подписаться на события логирования
        
        Args:
            run_id: ID запуска ("*" для всех запусков)
            callback: Функция-обработчик
        """
        with self._lock:
            if run_id not in self.log_subscribers:
                self.log_subscribers[run_id] = []
            self.log_subscribers[run_id].append(callback)

    def subscribe_progress(self, run_id: str, callback: ProgressCallback):
        """
        Подписаться на события прогресса
        
        Args:
            run_id: ID запуска ("*" для всех запусков)
            callback: Функция-обработчик
        """
        with self._lock:
            if run_id not in self.progress_subscribers:
                self.progress_subscribers[run_id] = []
            self.progress_subscribers[run_id].append(callback)

    def subscribe_system_metrics(self, callback: SystemMetricCallback):
        """
        Подписаться на события системных метрик
        """
        with self._lock:
            self.system_metric_subscribers.append(callback)

    def unsubscribe_logs(self, run_id: str, callback: LogCallback):
        """Отписаться от событий логирования"""
        with self._lock:
            if run_id in self.log_subscribers:
                try:
                    self.log_subscribers[run_id].remove(callback)
                    if not self.log_subscribers[run_id]:
                        del self.log_subscribers[run_id]
                except ValueError:
                    pass

    def unsubscribe_progress(self, run_id: str, callback: ProgressCallback):
        """Отписаться от событий прогресса"""
        with self._lock:
            if run_id in self.progress_subscribers:
                try:
                    self.progress_subscribers[run_id].remove(callback)
                    if not self.progress_subscribers[run_id]:
                        del self.progress_subscribers[run_id]
                except ValueError:
                    pass

    def unsubscribe_system_metrics(self, callback: SystemMetricCallback):
        """Отписаться от событий системных метрик"""
        with self._lock:
            try:
                self.system_metric_subscribers.remove(callback)
            except ValueError:
                pass


    def emit_log(self, run_id: str, level: str, message: str, logger_name: str = "",
                 extra_data: Dict[str, Any] = None, span_id: str = None, trace_id: str = None):
        """Отправить событие логирования с корреляционными данными"""
        event = LogEvent(
            run_id=run_id,
            timestamp=datetime.now(timezone.utc),
            level=level,
            logger_name=logger_name,
            message=message,
            extra_data=extra_data or {},
            span_id=span_id,
            trace_id=trace_id
        )
        self.log_queue.put(event)

    def emit_progress(self, run_id: str, event_type: str, component: str, data: Dict[str, Any] = None):
        """Отправить событие прогресса"""
        event = ProgressEvent(
            run_id=run_id,
            timestamp=datetime.now(timezone.utc),
            event_type=event_type,
            component=component,
            data=data or {}
        )
        self.progress_queue.put(event)

    def emit_system_metric(self, metric_type: str, data: Dict[str, Any]):
        """Отправить событие системной метрики"""
        event = SystemMetricEvent(
            timestamp=datetime.now(timezone.utc),
            metric_type=metric_type,
            data=data
        )
        self.system_metric_queue.put(event)

    def shutdown(self):
        """Завершение работы события шины"""
        self._running = False




class RunIdLogHandler(logging.Handler):
    """
    Handler для перехвата логов с run_id и отправки в событийную шину
    с автоматической корреляцией OpenTelemetry спанов
    """
    
    def __init__(self, event_bus: EventBus, logs_dir: Path):
        super().__init__()
        self.event_bus = event_bus
        self.logs_dir = Path(logs_dir)

    def emit(self, record):
        """Обработка log record с автоматической корреляцией спанов"""
        try:
            # Извлекаем run_id из extra данных или из потоковой локальной переменной
            run_id = getattr(record, 'run_id', None)
            if not run_id:
                # Потокобезопасный способ получения run_id
                run_id = get_current_run_id()
            if not run_id:
                # Fallback на os.environ для обратной совместимости
                run_id = os.environ.get('RUN_ID')
            if not run_id:
                return  # Пропускаем логи без run_id вообще
            
            # Автоматически извлекаем информацию о текущем спане
            span_id = None
            trace_id = None
            
            try:
                # Попытка импортировать OpenTelemetry (может быть недоступна)
                from opentelemetry import trace
                
                current_span = trace.get_current_span()
                if current_span and current_span.is_recording():
                    span_context = current_span.get_span_context()
                    span_id = format(span_context.span_id, '016x')
                    trace_id = format(span_context.trace_id, '032x')
                    
            except ImportError:
                # OpenTelemetry не установлена - продолжаем без корреляции
                pass
            except Exception:
                # Любая ошибка при получении span контекста - игнорируем
                pass
            
            # Также проверяем, переданы ли span_id/trace_id явно в extra
            if hasattr(record, 'span_id') and record.span_id:
                span_id = record.span_id
            if hasattr(record, 'trace_id') and record.trace_id:
                trace_id = record.trace_id
            
            def _decode_text(value: str) -> str:
                if not isinstance(value, str):
                    return value
                if any(token in value for token in ("\\u", "\\U", "\\x")):
                    try:
                        return codecs.decode(value, "unicode_escape")
                    except Exception:
                        return value
                return value

            def _normalize_json_string(value: str) -> str:
                if not isinstance(value, str):
                    return value
                stripped = value.strip()
                if not stripped:
                    return value
                candidate = stripped
                if candidate[0] not in ("{", "["):
                    if candidate.startswith("\\{") or candidate.startswith("\\["):
                        candidate = _decode_text(candidate)
                    else:
                        candidate = _decode_text(candidate)
                        if candidate and candidate[0] not in ("{", "["):
                            return value
                if "\\u" not in stripped and "\\U" not in stripped:
                    return value
                try:
                    parsed = json.loads(candidate)
                except Exception:
                    return value
                try:
                    return json.dumps(parsed, ensure_ascii=False)
                except Exception:
                    return value

            def _normalize_value(value):
                if isinstance(value, dict):
                    return {k: _normalize_value(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [_normalize_value(item) for item in value]
                if isinstance(value, str):
                    normalized = _normalize_json_string(value)
                    return _decode_text(normalized)
                return value

            # Форматируем сообщение
            message = self.format(record)
            message = _redact_text(_decode_text(message))

            extra_data = dict(getattr(record, '__dict__', {}))
            extra_data = _redact_payload(_normalize_value(extra_data))
            if isinstance(extra_data.get("msg"), str):
                extra_data["msg"] = message
            if isinstance(extra_data.get("message"), str):
                extra_data["message"] = message
            
            # Отправляем в событийную шину с корреляционными данными
            self.event_bus.emit_log(
                run_id=run_id,
                level=record.levelname,
                message=message,
                logger_name=record.name,
                extra_data=extra_data,
                span_id=span_id,
                trace_id=trace_id
            )

            # Дополнительно сохраняем событие в per-run JSONL для последующей корреляции
            try:
                self.logs_dir.mkdir(parents=True, exist_ok=True)
                log_file = self.logs_dir / f"{run_id}_logs.jsonl"
                log_dict = {
                    "run_id": run_id,
                    "timestamp": datetime.now().isoformat(),
                    "level": record.levelname,
                    "logger_name": record.name,
                    "message": message,
                    "extra_data": extra_data,
                    "span_id": span_id,
                    "trace_id": trace_id,
                }
                with open(log_file, 'a', encoding='utf-8') as f:
                    json.dump(log_dict, f, ensure_ascii=False, default=str)
                    f.write('\n')
            except Exception:
                # Никогда не роняем основной поток из-за проблемы при записи логов
                pass
            
        except Exception:
            self.handleError(record)


class UnifiedLoggingManager:
    """
    Менеджер единой системы логирования с run_id
    """
    
    def __init__(self, logs_dir: str = "logs"):
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        
        self.event_bus = EventBus()
        self.run_loggers: Dict[str, RunIdLoggerAdapter] = {}
        
        # Настраиваем обработчик для перехвата логов
        self._setup_log_handler()
        
        logging.getLogger(__name__).info("📝 UnifiedLoggingManager инициализирован")

    def _setup_log_handler(self):
        """Настройка обработчика логов"""
        # Создаем обработчик для событийной шины
        event_handler = RunIdLogHandler(self.event_bus, self.logs_dir)
        event_handler.setLevel(logging.DEBUG)
        
        # Добавляем к root logger
        root_logger = logging.getLogger()
        base_level = root_logger.level if root_logger.level != logging.NOTSET else logging.INFO
        console_handler = next(
            (
                handler
                for handler in root_logger.handlers
                if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
            ),
            None,
        )
        if console_handler is None:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(base_level)
            console_handler.setFormatter(
                logging.Formatter(
                    "[%(asctime)s] %(name)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            root_logger.addHandler(console_handler)
        if root_logger.level == logging.NOTSET:
            root_logger.setLevel(base_level)
        root_logger.addHandler(event_handler)

        # Добавляем обработчик к логгерам, которые не прокидывают события вверх
        for logger in logging.Logger.manager.loggerDict.values():
            if not isinstance(logger, logging.Logger):
                continue
            if logger.propagate is False and event_handler not in logger.handlers:
                logger.addHandler(event_handler)
            if logger.propagate is False and console_handler and not any(
                isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
                for handler in logger.handlers
            ):
                logger.addHandler(console_handler)

        if not getattr(logging, "_unified_logging_patched", False):
            def _patched_get_logger(name: Optional[str] = None) -> logging.Logger:
                logger = _original_get_logger(name)
                if isinstance(logger, logging.Logger) and logger.propagate is False and event_handler not in logger.handlers:
                    logger.addHandler(event_handler)
                if (
                    isinstance(logger, logging.Logger)
                    and logger.propagate is False
                    and console_handler
                    and not any(
                        isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
                        for handler in logger.handlers
                    )
                ):
                    logger.addHandler(console_handler)
                return logger

            logging.getLogger = _patched_get_logger  # type: ignore[assignment]
            logging._unified_logging_patched = True

    def get_logger(self, run_id: str, logger_name: str = "multiagent") -> RunIdLoggerAdapter:
        """
        Получить логгер с привязанным run_id
        
        Args:
            run_id: Идентификатор запуска
            logger_name: Имя логгера
            
        Returns:
            RunIdLoggerAdapter с автоматическим run_id
        """
        key = f"{run_id}:{logger_name}"
        
        if key not in self.run_loggers:
            base_logger = logging.getLogger(logger_name)
            self.run_loggers[key] = RunIdLoggerAdapter(base_logger, run_id)
            
        return self.run_loggers[key]

    def emit_progress(self, run_id: str, event_type: str, component: str, data: Dict[str, Any] = None):
        """
        Отправить событие прогресса
        
        Args:
            run_id: Идентификатор запуска
            event_type: Тип события (started, step, progress, completed, failed, cancelled)
            component: Компонент (workflow, agent, step_name)
            data: Дополнительные данные
        """
        self.event_bus.emit_progress(run_id, event_type, component, data)

    def subscribe_run_logs(self, run_id: str, callback: LogCallback):
        """Подписаться на логи конкретного запуска"""
        self.event_bus.subscribe_logs(run_id, callback)

    def subscribe_run_progress(self, run_id: str, callback: ProgressCallback):
        """Подписаться на прогресс конкретного запуска"""
        self.event_bus.subscribe_progress(run_id, callback)

    def subscribe_all_logs(self, callback: LogCallback):
        """Подписаться на все логи"""
        self.event_bus.subscribe_logs("*", callback)

    def subscribe_all_progress(self, callback: ProgressCallback):
        """Подписаться на весь прогресс"""
        self.event_bus.subscribe_progress("*", callback)

    def unsubscribe_run_logs(self, run_id: str, callback: LogCallback):
        """Отписаться от логов запуска"""
        self.event_bus.unsubscribe_logs(run_id, callback)

    def unsubscribe_run_progress(self, run_id: str, callback: ProgressCallback):
        """Отписаться от прогресса запуска"""
        self.event_bus.unsubscribe_progress(run_id, callback)

    def unsubscribe_all_logs(self, callback: LogCallback):
        """Симметрично subscribe_all_logs — отписка от глобального канала логов."""
        self.event_bus.unsubscribe_logs("*", callback)

    def unsubscribe_all_progress(self, callback: ProgressCallback):
        """Симметрично subscribe_all_progress — отписка от глобального канала прогресса."""
        self.event_bus.unsubscribe_progress("*", callback)

    def get_run_logs(self, run_id: str, limit: int = 1000) -> List[LogEvent]:
        """
        Получить сохраненные логи для конкретного запуска
        
        Args:
            run_id: Идентификатор запуска
            limit: Максимальное количество логов
            
        Returns:
            Список объектов LogEvent
        """
        try:
            log_file = self.logs_dir / f"{run_id}_logs.jsonl"
            if not log_file.exists():
                return []
            events: List[LogEvent] = []
            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        events.append(
                            LogEvent(
                                run_id=data.get("run_id", run_id),
                                timestamp=datetime.fromisoformat(data.get("timestamp")),
                                level=data.get("level", "INFO"),
                                logger_name=data.get("logger_name", ""),
                                message=data.get("message", ""),
                                extra_data=data.get("extra_data", {}),
                                span_id=data.get("span_id"),
                                trace_id=data.get("trace_id"),
                            )
                        )
                    except Exception:
                        continue
            # Возвращаем последние limit событий
            return events[-limit:]
        except Exception as e:
            logging.getLogger(__name__).error(f"Ошибка чтения логов {run_id}: {e}")
            return []

    def search_all_logs(self, 
                        query: str = "",
                        level: Optional[str] = None,
                        start_time: Optional[datetime] = None,
                        end_time: Optional[datetime] = None,
                        limit: int = 100,
                        use_regex: bool = False,
                        case_sensitive: bool = False) -> List[LogEvent]:
        """
        Ищет логи по всем файлам, соответствующим критериям.
        
        Args:
            query: Текстовый запрос для поиска.
            level: Уровень логирования для фильтрации (INFO, WARNING, ERROR, DEBUG).
            start_time: Начальное время для фильтрации.
            end_time: Конечное время для фильтрации.
            limit: Максимальное количество возвращаемых логов.
            use_regex: Использовать ли `query` как регулярное выражение.
            case_sensitive: Чувствителен ли поиск к регистру.
            
        Returns:
            Список объектов LogEvent, соответствующих критериям.
        """
        matched_logs: List[LogEvent] = []
        log_files = sorted(self.logs_dir.glob("*_logs.jsonl"), reverse=True) # Search newer logs first
        
        compiled_query = None
        if query:
            try:
                import re
                if use_regex:
                    compiled_query = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
                else:
                    # Escape special characters for literal search if not regex
                    compiled_query = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
            except Exception as e:
                logging.getLogger(__name__).warning(f"Ошибка компиляции регулярного выражения: {e}")
                compiled_query = None # Fallback to no query search
        
        for log_file in log_files:
            if len(matched_logs) >= limit:
                break
            
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if len(matched_logs) >= limit:
                            break
                        try:
                            data = json.loads(line)
                            log_event = LogEvent(
                                run_id=data.get("run_id"),
                                timestamp=datetime.fromisoformat(data.get("timestamp")),
                                level=data.get("level", "INFO"),
                                logger_name=data.get("logger_name", ""),
                                message=data.get("message", ""),
                                extra_data=data.get("extra_data", {}),
                                span_id=data.get("span_id"),
                                trace_id=data.get("trace_id"),
                            )

                            # Apply filters
                            if level and log_event.level != level:
                                continue
                            if start_time and log_event.timestamp < start_time:
                                continue
                            if end_time and log_event.timestamp > end_time:
                                continue
                            if compiled_query and not compiled_query.search(log_event.message):
                                continue

                            matched_logs.append(log_event)
                        except Exception:
                            continue
            except Exception as e:
                logging.getLogger(__name__).warning(f"Ошибка чтения файла логов {log_file}: {e}")
        
        # Сортируем по времени, так как читали файлы в обратном порядке
        matched_logs.sort(key=lambda x: x.timestamp)
        return matched_logs[-limit:] # Return up to limit, ordered chronologically


    def get_logs_for_span(self, run_id: str, span_id: str) -> List[LogEvent]:
        """
        Получить все логи для конкретного спана
        
        Args:
            run_id: Идентификатор запуска
            span_id: Идентификатор спана
            
        Returns:
            Список логов связанных с данным спаном
        """
        try:
            if not span_id:
                return []
            # Читаем все логи запуска и фильтруем по span_id
            run_logs = self.get_run_logs(run_id)
            return [log for log in run_logs if getattr(log, "span_id", None) == span_id]
        except Exception as e:
            logging.getLogger(__name__).error(
                f"Ошибка получения логов для span {span_id} (run {run_id}): {e}"
            )
            return []

    def get_correlated_logs_and_spans(self, run_id: str) -> Dict[str, Any]:
        """
        Получить коррелированные логи и спаны для запуска
        
        Args:
            run_id: Идентификатор запуска
            
        Returns:
            Словарь с логами и спанами, сгруппированными по корреляции
        """
        try:
            from telemetry.smolagents_telemetry import get_telemetry_manager
            
            # Получаем спаны
            telemetry_manager = get_telemetry_manager()
            spans = telemetry_manager.read_trace_events(run_id)
            
            # Получаем логи (заглушка - в реальности нужно читать из сохраненных файлов)
            logs = self.get_run_logs(run_id)
            
            # Группируем логи по span_id
            logs_by_span = {}
            logs_without_span = []
            
            for log in logs:
                if log.span_id:
                    if log.span_id not in logs_by_span:
                        logs_by_span[log.span_id] = []
                    logs_by_span[log.span_id].append(log)
                else:
                    logs_without_span.append(log)
            
            # Создаем коррелированную структуру
            correlated_data = {
                "run_id": run_id,
                "spans": [],
                "uncorrelated_logs": logs_without_span
            }
            
            for span in spans:
                span_data = {
                    "span": span,
                    "logs": logs_by_span.get(span.span_id, [])
                }
                correlated_data["spans"].append(span_data)
            
            return correlated_data
            
        except Exception as e:
            logging.getLogger(__name__).error(f"Ошибка получения коррелированных данных для {run_id}: {e}")
            return {"run_id": run_id, "spans": [], "uncorrelated_logs": []}

    def cleanup_old_logs(self, max_age_days: int = 7):
        """
        Очистка старых файлов логов
        
        Args:
            max_age_days: Максимальный возраст в днях
        """
        if not self.logs_dir.exists():
            return
            
        current_time = datetime.now()
        removed_count = 0
        
        for log_file in self.logs_dir.glob("*_logs.jsonl"):
            try:
                file_age = current_time - datetime.fromtimestamp(log_file.stat().st_mtime)
                
                if file_age.days > max_age_days:
                    log_file.unlink()
                    removed_count += 1
                    
            except Exception as e:
                logging.getLogger(__name__).warning(f"Ошибка при удалении {log_file}: {e}")
        
        if removed_count > 0:
            logging.getLogger(__name__).info(f"🧹 Удалено {removed_count} старых файлов логов")

    def shutdown(self):
        """Завершение работы системы логирования"""
        self.event_bus.shutdown()


# Глобальный экземпляр менеджера
_logging_manager: Optional[UnifiedLoggingManager] = None
_original_get_logger = logging.getLogger

def get_logging_manager(logs_dir: str = "logs") -> UnifiedLoggingManager:
    """
    Получить глобальный экземпляр менеджера логирования
    
    Args:
        logs_dir: Директория для логов
        
    Returns:
        Экземпляр UnifiedLoggingManager
    """
    global _logging_manager
    
    if _logging_manager is None:
        _logging_manager = UnifiedLoggingManager(logs_dir)
    
    return _logging_manager

def get_run_logger(run_id: str, logger_name: str = "multiagent") -> RunIdLoggerAdapter:
    """
    Получить логгер с привязанным run_id
    
    Args:
        run_id: Идентификатор запуска
        logger_name: Имя логгера
        
    Returns:
        RunIdLoggerAdapter
    """
    manager = get_logging_manager()
    return manager.get_logger(run_id, logger_name)

def get_current_run_id() -> Optional[str]:
    """
    Получить текущий run_id из потоковой локальной переменной.
    Потокобезопасно - каждый поток имеет свой run_id.
    
    Returns:
        Текущий run_id или None если не установлен
    """
    return getattr(_thread_local, 'run_id', None)


def set_current_run_id(run_id: Optional[str]) -> None:
    """
    Установить текущий run_id в потоковую локальную переменную.
    Потокобезопасно - не влияет на другие потоки.
    
    Args:
        run_id: Идентификатор запуска или None для очистки
    """
    if run_id is None:
        if hasattr(_thread_local, 'run_id'):
            delattr(_thread_local, 'run_id')
    else:
        _thread_local.run_id = run_id


@contextmanager
def run_id_context(run_id: str):
    """
    Контекстный менеджер для установки run_id в потоковой локальной переменной.
    
    ПОТОКОБЕЗОПАСНО: Каждый поток имеет свой независимый run_id.
    Используйте для локальной корреляции логов и трассировки.
    
    Args:
        run_id: Идентификатор запуска
        
    Example:
        with run_id_context("run-123"):
            # Все логи и спаны в этом контексте получат run_id="run-123"
            agent.run(task)
    """
    previous = get_current_run_id()
    try:
        set_current_run_id(run_id)
        # Также устанавливаем в os.environ для обратной совместимости
        # (некоторые старые части кода могут всё ещё использовать это)
        os.environ["RUN_ID"] = run_id
        yield
    finally:
        try:
            set_current_run_id(previous)
            # Восстанавливаем или удаляем переменную окружения
            if previous is not None:
                os.environ["RUN_ID"] = previous
            else:
                os.environ.pop("RUN_ID", None)
        except Exception:
            pass

def emit_progress(run_id: str, event_type: str, component: str, data: Dict[str, Any] = None):
    """
    Отправить событие прогресса
    
    Args:
        run_id: Идентификатор запуска
        event_type: Тип события
        component: Компонент
        data: Дополнительные данные
    """
    manager = get_logging_manager()
    manager.emit_progress(run_id, event_type, component, data)
