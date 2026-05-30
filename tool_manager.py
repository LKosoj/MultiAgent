"""
Централизованный менеджер для запуска инструментов с телеметрией
Аналогичен AgentManager, но для инструментов
"""

import logging
import threading
import uuid
from typing import Dict, Any, Optional, Callable, Union, List
from datetime import datetime, timedelta
from contextlib import contextmanager
import json
import inspect
import re
from urllib.parse import unquote_plus

logger = logging.getLogger(__name__)


_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "credentials",
    "dsn",
    "key",
    "password",
    "passwd",
    "private_key",
    "pwd",
    "refresh_token",
    "secret",
    "secret_access_key",
    "secret_key",
    "token",
}
_DSN_VALUE_KEYS = {
    "connection_string",
    "database_dsn",
    "database_url",
    "db_dsn",
    "db_url",
    "dsn",
}
_DSN_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^@/\s]*)@"
)
_DSN_QUERY_SECRET_RE = re.compile(
    r"(?P<sep>[?&;])(?P<key>[^=&;\s]+)=(?P<val>\{[^}]*\}|(?:[^&;\s]|;(?![A-Za-z_][A-Za-z0-9_]*\s*=))+)",
    flags=re.IGNORECASE,
)
_KEY_VALUE_SECRET_RE = re.compile(
    r"\b(?P<key>[A-Za-z0-9_%+\-.]+)"
    r"\s*(?:=|:(?!\s*[^\s,&;]*=))\s*"
    r"(?P<val>\{[^}]*\}|(?:[^\s,&;]|;(?![A-Za-z_][A-Za-z0-9_]*\s*[:=]))+)",
    flags=re.IGNORECASE,
)
_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>(?P<key_quote>['\"])(?P<key>[A-Za-z0-9_%+\-.]+)"
    r"(?P=key_quote)\s*:\s*)"
    r"(?P<value_quote>['\"])(?P<val>(?:\\.|(?!(?P=value_quote)).)*)"
    r"(?P=value_quote)",
    flags=re.IGNORECASE,
)
_AUTHORIZATION_SECRET_RE = re.compile(
    r"(?P<prefix>\bauthorization\s*[:=]\s*)"
    r"(?P<val>(?:Bearer|Basic|Digest|Token)\s+[^\s,&;]+|[^\s,&;]+)",
    flags=re.IGNORECASE,
)


def _normalize_sensitive_key(key: str | None) -> str:
    text = unquote_plus(str(key or ""))
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_").casefold()


def _is_sensitive_name(key: str | None) -> bool:
    lowered = _normalize_sensitive_key(key)
    return (
        lowered in _SENSITIVE_KEYS
        or "credentials" in lowered
        or "private_key" in lowered
        or "secret_access_key" in lowered
        or "secret_key" in lowered
        or lowered.endswith("_token")
        or lowered.endswith("_secret")
        or lowered.endswith("_password")
    )


def _is_sensitive_key(key: str | None) -> bool:
    if not key:
        return False
    lowered = _normalize_sensitive_key(key)
    if _is_sensitive_name(key):
        return True
    return any(
        marker in lowered
        for marker in ("api_key", "apikey", "authorization", "dsn")
    )


def _is_dsn_value_key(key: str | None) -> bool:
    lowered = _normalize_sensitive_key(key)
    return lowered in _DSN_VALUE_KEYS or lowered.endswith("_dsn")


def _redact_dsn_text(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        return f"{match.group('scheme')}***:***@"

    redacted = _DSN_CREDENTIALS_RE.sub(_replace, value)
    redacted = _DSN_QUERY_SECRET_RE.sub(
        lambda match: (
            f"{match.group('sep')}{match.group('key')}=***"
            if _normalize_sensitive_key(match.group("key")) == "odbc_connect"
            or _is_sensitive_key(match.group("key"))
            else match.group(0)
        ),
        redacted,
    )
    redacted = _AUTHORIZATION_SECRET_RE.sub(r"\g<prefix>***", redacted)
    redacted = _QUOTED_SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('value_quote')}***{match.group('value_quote')}"
            if _is_sensitive_key(match.group("key"))
            else match.group(0)
        ),
        redacted,
    )
    return _KEY_VALUE_SECRET_RE.sub(
        lambda match: (
            f"{match.group('key')}{match.group(0)[len(match.group('key')):match.start('val') - match.start()]}***"
            if _is_sensitive_key(match.group("key"))
            else match.group(0)
        ),
        redacted,
    )


def _redact_runtime_value(
    value: Any,
    key: str | None = None,
    _memo: dict[int, Any] | None = None,
    _active: set[int] | None = None,
) -> Any:
    if _is_sensitive_key(key):
        if _is_dsn_value_key(key) and isinstance(value, str):
            return _redact_dsn_text(value)
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact_dsn_text(value)
    if not isinstance(value, (dict, list, tuple)):
        return value
    if _memo is None:
        _memo = {}
    if _active is None:
        _active = set()
    obj_id = id(value)
    if obj_id in _active:
        return "[Circular]"
    if obj_id in _memo:
        return _memo[obj_id]
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        _memo[obj_id] = redacted
        _active.add(obj_id)
        try:
            for item_key, item_value in value.items():
                safe_key = _redact_dsn_text(item_key) if isinstance(item_key, str) else item_key
                redacted[safe_key] = _redact_runtime_value(item_value, str(item_key), _memo, _active)
        finally:
            _active.discard(obj_id)
        return redacted
    if isinstance(value, list):
        redacted_list: list[Any] = []
        _memo[obj_id] = redacted_list
        _active.add(obj_id)
        try:
            redacted_list.extend(_redact_runtime_value(item, _memo=_memo, _active=_active) for item in value)
        finally:
            _active.discard(obj_id)
        return redacted_list
    _active.add(obj_id)
    redacted_tuple = tuple(_redact_runtime_value(item, _memo=_memo, _active=_active) for item in value)
    _active.discard(obj_id)
    _memo[obj_id] = redacted_tuple
    return redacted_tuple


class ToolManager:
    """
    Менеджер для централизованного запуска инструментов с телеметрией
    Автоматически создает корневой спан agent_run_<tool_name> для каждого инструмента
    """
    
    _CLEANUP_INTERVAL = 100  # вызывать cleanup каждые N завершений

    def __init__(self):
        self.active_runs: Dict[str, Dict[str, Any]] = {}
        self._runs_lock = threading.Lock()
        self._completion_counter = 0

    def _sanitize_active_run(self, run_id: str) -> None:
        """Редактирует чувствительные поля записи active_runs «на месте».

        ИНВАРИАНТ: вызывается ТОЛЬКО при уже захваченном self._runs_lock и сам лок
        НЕ берёт (threading.Lock нереентерабелен — повторный захват = deadlock).
        Все 4 call-site (success/except в run_tool и tool_context) держат лок.
        """
        run_data = self.active_runs.get(run_id)
        if not isinstance(run_data, dict):
            return
        safe_run_data = _redact_runtime_value(run_data)
        run_data.clear()
        if isinstance(safe_run_data, dict):
            run_data.update(safe_run_data)
        
    def run_tool(self, 
                 tool_name: str,
                 tool_function: Callable,
                 task_description: str,
                 session_id: Optional[str] = None,
                 **kwargs) -> Any:
        """
        Централизованный запуск инструмента с телеметрией
        
        Args:
            tool_name: Имя инструмента (например, "image_generation", "image_analysis")
            tool_function: Функция инструмента для вызова
            task_description: Описание задачи
            session_id: Идентификатор сессии
            **kwargs: Аргументы для передачи в функцию инструмента
            
        Returns:
            Результат выполнения инструмента
        """
        
        # Используем session_id как run_id для единой системы идентификаторов
        if not session_id:
            session_id = f"run-{uuid.uuid4().hex[:16]}"
        run_id = session_id
        
        span = None
        result = None
        try:
            # Настраиваем телеметрию
            from telemetry import get_telemetry_manager
            telemetry_manager = get_telemetry_manager()
            safe_kwargs = _redact_runtime_value(kwargs)
            safe_task_description = _redact_runtime_value(task_description)
            
            # Регистрируем запуск
            with self._runs_lock:
                self.active_runs[run_id] = {
                    "tool_name": tool_name,
                    "status": "running",
                    "task": safe_task_description,
                    "session_id": session_id,
                    "start_time": datetime.now(),
                    "kwargs": safe_kwargs
                }
            
            logger.info(f"🔧 Запуск инструмента {tool_name} с run_id: {run_id}")
            
            # Создаем корневой span с именем agent_run_<tool_name>
            # Используем start_run_trace() для совместимости с SmolagentsTelemetryManager
            span = telemetry_manager.start_run_trace(
                run_id=run_id,
                agent_name=tool_name,
                task=safe_task_description,
                profile_type="streamlit_tool",
                session_id=session_id
            )
            
            tool_completed = False
            try:
                if span:
                    # Добавляем дополнительные атрибуты для инструментов
                    additional_attrs = {k: str(v)[:100] for k, v in safe_kwargs.items()
                                       if isinstance(v, (str, int, float, bool))}
                    if additional_attrs:
                        span.set_attributes(additional_attrs)
                
                # Добавляем session_id в kwargs для функции инструмента
                if session_id and 'session_id' not in kwargs:
                    kwargs['session_id'] = session_id

                # Вызываем функцию инструмента, фильтруя неизвестные параметры
                call_kwargs = self._filter_kwargs_for_callable(tool_function, kwargs)
                safe_call_kwargs = _redact_runtime_value(call_kwargs)
                logger.debug(f"Отфильтрованные параметры для {tool_name}: {safe_call_kwargs}")
                
                # Специальная обработка для DuckDuckGoSearchTool и подобных инструментов
                # которые принимают позиционный аргумент (строку запроса) через __call__
                # Проверяем сигнатуру метода forward() или __call__() для определения способа вызова
                use_positional_query = False
                if not inspect.isfunction(tool_function) and not inspect.ismethod(tool_function):
                    try:
                        # Для экземпляров классов проверяем метод forward() или __call__()
                        if hasattr(tool_function, 'forward'):
                            sig = inspect.signature(tool_function.forward)
                            logger.debug(f"Сигнатура forward() для {tool_name}: {sig}")
                        elif hasattr(tool_function, '__call__'):
                            sig = inspect.signature(tool_function.__call__)
                            logger.debug(f"Сигнатура __call__() для {tool_name}: {sig}")
                        else:
                            sig = inspect.signature(tool_function)
                            logger.debug(f"Сигнатура для {tool_name}: {sig}")
                        
                        # Если метод принимает только один позиционный параметр (кроме self)
                        # и у нас есть параметр 'query', передаем его как позиционный аргумент
                        params = list(sig.parameters.values())
                        # Исключаем self/cls параметры
                        non_self_params = [p for p in params if p.name not in ('self', 'cls')]
                        
                        if len(non_self_params) == 1 and 'query' in call_kwargs:
                            use_positional_query = True
                    except Exception as e:
                        logger.warning(f"Ошибка при определении способа вызова инструмента {tool_name}: {e}, используем стандартный вызов")

                if use_positional_query:
                    safe_query = safe_call_kwargs.get("query") if isinstance(safe_call_kwargs, dict) else "[REDACTED]"
                    logger.info(f"Вызываем {tool_name} с позиционным аргументом: query='{safe_query}'")
                    result = tool_function(call_kwargs['query'])
                else:
                    logger.debug(f"Вызываем {tool_name} с именованными параметрами: {safe_call_kwargs}")
                    result = tool_function(**call_kwargs)
                
                tool_completed = True

                # Обновляем статус
                do_cleanup = False  # инициализация до lock: иначе исключение внутри
                                    # критической секции даст UnboundLocalError ниже.
                with self._runs_lock:
                    self.active_runs[run_id]["status"] = "completed"
                    self.active_runs[run_id]["end_time"] = datetime.now()
                    safe_result = _redact_runtime_value(result)
                    self.active_runs[run_id]["result"] = str(safe_result)[:200] if safe_result else "No result"
                    self._sanitize_active_run(run_id)
                    # Периодическая автоочистка завершённых записей
                    self._completion_counter += 1
                    do_cleanup = self._completion_counter % self._CLEANUP_INTERVAL == 0
                if do_cleanup:
                    self.cleanup_completed()

                logger.info(f"✅ Инструмент {tool_name} завершен с run_id: {run_id}")
                
                return result
                
            finally:
                # Закрываем span через finish_run_trace с записью результата
                if span and tool_completed:
                    try:
                        if isinstance(result, (dict, list)):
                            span.set_attribute("output.mime_type", "application/json")
                            span.set_attribute("output.value", json.dumps(_redact_runtime_value(result), ensure_ascii=False, default=str))
                        elif isinstance(result, str):
                            span.set_attribute("output.mime_type", "text/plain")
                            span.set_attribute("output.value", _redact_runtime_value(result))
                        elif result is not None:
                            span.set_attribute("output.mime_type", "text/plain")
                            span.set_attribute("output.value", str(_redact_runtime_value(result)))
                    except Exception as attr_err:
                        logger.debug("Telemetry: не удалось записать output-атрибуты span'а: %s", attr_err)
                    try:
                        telemetry_manager.finish_run_trace(span, success=True)
                    except Exception as finish_err:
                        logger.debug("Telemetry: finish_run_trace не сработал (%s), закрываем span напрямую", finish_err)
                        try:
                            span.end()
                        except Exception as end_err:
                            logger.debug("Telemetry: span.end() также не сработал: %s", end_err)

        except Exception as e:
            # Обновляем статус при ошибке
            do_cleanup = False  # инициализация до lock (см. success-путь)
            with self._runs_lock:
                if run_id in self.active_runs:
                    self.active_runs[run_id]["status"] = "failed"
                    self.active_runs[run_id]["error"] = str(_redact_runtime_value(str(e)))
                    self.active_runs[run_id]["end_time"] = datetime.now()
                    self._sanitize_active_run(run_id)
                # Периодическая автоочистка: учитываем и сбойные завершения, иначе
                # failed-записи копятся в active_runs бесконечно (cleanup был только в success-пути).
                self._completion_counter += 1
                do_cleanup = self._completion_counter % self._CLEANUP_INTERVAL == 0
            if do_cleanup:
                self.cleanup_completed()

            # Закрываем span при ошибке
            if span:
                try:
                    telemetry_manager.finish_run_trace(
                        span,
                        success=False,
                        error_message=str(_redact_runtime_value(str(e))),
                    )
                except Exception as finish_err:
                    logger.debug("Telemetry: finish_run_trace (error path) не сработал: %s", finish_err)
                    try:
                        span.end()
                    except Exception as end_err:
                        logger.debug("Telemetry: span.end() (error path) не сработал: %s", end_err)

            safe_error = str(_redact_runtime_value(str(e)))
            logger.error(f"❌ Ошибка выполнения инструмента {tool_name} с run_id {run_id}: {safe_error}")
            raise

    @staticmethod
    def _filter_kwargs_for_callable(func: Callable, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Фильтрует kwargs, оставляя только те параметры, которые принимает функция.
        Для экземпляров классов проверяет метод forward() или __call__().
        """
        try:
            # Если это экземпляр класса, проверяем метод forward() или __call__()
            if not inspect.isfunction(func) and not inspect.ismethod(func):
                # Проверяем наличие метода forward() (используется в smolagents)
                if hasattr(func, 'forward'):
                    try:
                        signature = inspect.signature(func.forward)
                        logger.debug(f"Используем метод forward() для {type(func).__name__}, сигнатура: {signature}")
                    except Exception as e:
                        logger.warning(f"Не удалось получить сигнатуру forward() для {type(func).__name__}: {e}")
                        # Пробуем __call__()
                        if hasattr(func, '__call__'):
                            signature = inspect.signature(func.__call__)
                        else:
                            signature = inspect.signature(func)
                # Иначе проверяем __call__()
                elif hasattr(func, '__call__'):
                    signature = inspect.signature(func.__call__)
                else:
                    # Если нет специальных методов, проверяем сам объект
                    signature = inspect.signature(func)
            else:
                signature = inspect.signature(func)
        except Exception as e:
            # Если не удалось получить сигнатуру, возвращаем все kwargs
            # (на случай, если функция принимает **kwargs)
            logger.warning(f"Не удалось получить сигнатуру для {type(func).__name__}: {e}, передаем все kwargs")
            return kwargs
        
        # Если функция принимает **kwargs, возвращаем все параметры
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
            logger.debug(f"Функция {type(func).__name__} принимает **kwargs, передаем все параметры")
            return kwargs
        
        # Фильтруем kwargs, оставляя только разрешенные параметры
        allowed = set(signature.parameters.keys())
        filtered = {key: value for key, value in kwargs.items() if key in allowed}
        if filtered != kwargs:
            removed = set(kwargs.keys()) - set(filtered.keys())
            logger.debug(f"Отфильтрованы параметры {removed} для {type(func).__name__}, разрешенные: {allowed}")
        return filtered
    
    @contextmanager
    def tool_context(self, tool_name: str, task_description: str, session_id: Optional[str] = None, **kwargs):
        """
        Контекстный менеджер для запуска инструментов
        Использование:
        
        with tool_manager.tool_context("image_generation", "Generate image", session_id="123") as ctx:
            result = some_tool_function()
            ctx.add_metadata("images_generated", 3)
        """
        
        # Используем session_id как run_id для единой системы идентификаторов
        if not session_id:
            session_id = f"run-{uuid.uuid4().hex[:16]}"
        run_id = session_id
        
        class ToolContext:
            def __init__(self, span, run_data):
                self.span = span
                self.run_data = run_data
                
            def add_metadata(self, key: str, value: Any):
                """Добавить метаданные в спан"""
                if self.span:
                    self.span.set_attribute(key, str(_redact_runtime_value(value, key)))
                    
            def set_status(self, status: str):
                """Установить статус выполнения"""
                self.run_data["status"] = status
        
        span = None
        try:
            # Настраиваем телеметрию
            from telemetry import get_telemetry_manager
            telemetry_manager = get_telemetry_manager()
            
            safe_kwargs = _redact_runtime_value(kwargs)
            safe_task_description = _redact_runtime_value(task_description)

            # Регистрируем запуск
            run_data = {
                "tool_name": tool_name,
                "status": "running",
                "task": safe_task_description,
                "session_id": session_id,
                "start_time": datetime.now(),
                "kwargs": safe_kwargs
            }
            with self._runs_lock:
                self.active_runs[run_id] = run_data
            
            logger.info(f"🔧 Начало контекста инструмента {tool_name} с run_id: {run_id}")
            
            # Создаем корневой span
            span = telemetry_manager.start_run_trace(
                run_id=run_id,
                agent_name=tool_name,
                task=safe_task_description,
                profile_type="streamlit_tool",
                session_id=session_id
            )
            
            try:
                if span:
                    # Добавляем дополнительные атрибуты для инструментов
                    additional_attrs = {k: str(v)[:100] for k, v in safe_kwargs.items()
                                       if isinstance(v, (str, int, float, bool))}
                    if additional_attrs:
                        span.set_attributes(additional_attrs)
                
                ctx = ToolContext(span, run_data)
                yield ctx

                # Обновляем статус при успешном завершении под локом — иначе
                # list_active_tools/cleanup_completed увидят частично обновлённый run_data.
                do_cleanup = False  # инициализация до lock: иначе исключение внутри
                                    # критической секции даст UnboundLocalError ниже.
                with self._runs_lock:
                    if run_data["status"] == "running":
                        run_data["status"] = "completed"
                    if "result" in run_data:
                        run_data["result"] = _redact_runtime_value(run_data["result"])
                    run_data["end_time"] = datetime.now()
                    self._sanitize_active_run(run_id)
                    # Периодическая автоочистка (как в run_tool): tool_context пишет в тот
                    # же active_runs, без этого его записи копились бы бесконечно.
                    self._completion_counter += 1
                    do_cleanup = self._completion_counter % self._CLEANUP_INTERVAL == 0
                if do_cleanup:
                    self.cleanup_completed()

                logger.info(f"✅ Контекст инструмента {tool_name} завершен с run_id: {run_id}")

            finally:
                # Закрываем span через finish_run_trace
                if span:
                    try:
                        # Итог по контексту может быть зафиксирован пользователем в run_data["result"], если он есть.
                        # Читаем под локом — иначе гонка с list_active_tools/cleanup_completed и другими писателями.
                        with self._runs_lock:
                            result_value = self.active_runs.get(run_id, {}).get("result")
                        if result_value:
                            safe_result = _redact_runtime_value(result_value)
                            if isinstance(result_value, (dict, list)):
                                span.set_attribute("output.mime_type", "application/json")
                                span.set_attribute("output.value", json.dumps(safe_result, ensure_ascii=False, default=str))
                            else:
                                span.set_attribute("output.mime_type", "text/plain")
                                span.set_attribute("output.value", str(safe_result))
                    except Exception:
                        pass
                    try:
                        telemetry_manager.finish_run_trace(span, success=True)
                    except Exception:
                        try:
                            span.end()
                        except Exception:
                            pass
                
        except Exception as e:
            # Обновляем статус при ошибке
            do_cleanup = False  # инициализация до lock (см. success-путь)
            with self._runs_lock:
                if run_id in self.active_runs:
                    self.active_runs[run_id]["status"] = "failed"
                    self.active_runs[run_id]["error"] = str(_redact_runtime_value(str(e)))
                    self.active_runs[run_id]["end_time"] = datetime.now()
                    self._sanitize_active_run(run_id)
                # Периодическая автоочистка: учитываем и сбойные завершения tool_context
                # (как в run_tool), иначе failed-записи копятся в active_runs.
                self._completion_counter += 1
                do_cleanup = self._completion_counter % self._CLEANUP_INTERVAL == 0
            if do_cleanup:
                self.cleanup_completed()

            # Закрываем span при ошибке
            if span:
                try:
                    telemetry_manager.finish_run_trace(
                        span,
                        success=False,
                        error_message=str(_redact_runtime_value(str(e))),
                    )
                except Exception:
                    try:
                        span.end()
                    except Exception:
                        pass

            safe_error = str(_redact_runtime_value(str(e)))
            logger.error(f"❌ Ошибка в контексте инструмента {tool_name} с run_id {run_id}: {safe_error}")
            raise
    
    def get_tool_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Получить статус выполнения инструмента"""
        with self._runs_lock:
            return self.active_runs.get(run_id)
    
    def list_active_tools(self) -> Dict[str, Dict[str, Any]]:
        """Получить список активных инструментов"""
        with self._runs_lock:
            return {k: v for k, v in self.active_runs.items() if v.get("status") == "running"}

    def list_run_snapshots(self) -> Dict[str, Dict[str, Any]]:
        """Получить снэпшот всех запусков инструментов."""
        with self._runs_lock:
            return {
                run_id: dict(run_data)
                for run_id, run_data in self.active_runs.items()
                if isinstance(run_data, dict)
            }
    
    def cleanup_completed(self, max_age_minutes: int = 60):
        """Очистка завершенных запусков старше указанного времени"""
        cutoff_time = datetime.now() - timedelta(minutes=max_age_minutes)

        with self._runs_lock:
            to_remove = [
                run_id for run_id, run_data in self.active_runs.items()
                if (run_data.get("end_time") and run_data["end_time"] < cutoff_time
                    and run_data.get("status") in ["completed", "failed"])
            ]
            for run_id in to_remove:
                del self.active_runs[run_id]

        if to_remove:
            logger.info(f"🧹 Очищено {len(to_remove)} завершенных запусков инструментов")


# Глобальный экземпляр менеджера инструментов
_tool_manager = None
_tool_manager_lock = threading.Lock()

def get_tool_manager() -> ToolManager:
    """Получить глобальный экземпляр менеджера инструментов (потокобезопасно)"""
    global _tool_manager
    if _tool_manager is None:
        with _tool_manager_lock:
            if _tool_manager is None:
                _tool_manager = ToolManager()
    return _tool_manager


# Декоратор для автоматического добавления телеметрии к функциям инструментов
def with_telemetry(tool_name: str, task_description: str = None):
    """
    Декоратор для автоматического добавления телеметрии к функциям инструментов
    
    Использование:
    @with_telemetry("image_generation", "Generate image from prompt")
    def my_tool_function(prompt, session_id, **kwargs):
        # Ваш код инструмента
        return result
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            # Формируем описание задачи
            description = task_description or f"Execute {tool_name}"
            if args:
                description += f" with {len(args)} args"
            if kwargs:
                description += f" and {len(kwargs)} kwargs"

            # Преобразуем positional args в именованные по сигнатуре функции
            merged_kwargs = dict(kwargs)
            if args:
                try:
                    sig = inspect.signature(func)
                    bound = sig.bind_partial(*args, **kwargs)
                    parameters = sig.parameters
                    var_positional_name = next(
                        (
                            name for name, param in parameters.items()
                            if param.kind == inspect.Parameter.VAR_POSITIONAL
                        ),
                        None,
                    )
                    if var_positional_name and bound.arguments.get(var_positional_name):
                        # Функция объявляет *args: позиционный «хвост» нельзя пробросить
                        # через run_tool (он вызывает tool_function(**kwargs)). Телеметрия
                        # для таких вызовов неприменима — вызываем напрямую, сохраняя ВСЕ
                        # аргументы. Это штатный путь, а не аномалия (потому debug, не warning).
                        logger.debug(
                            "with_telemetry: %s объявляет *args — прямой вызов без телеметрии",
                            func.__name__,
                        )
                        return func(*args, **kwargs)

                    has_bound_positional_only = any(
                        param.kind == inspect.Parameter.POSITIONAL_ONLY and name in bound.arguments
                        for name, param in parameters.items()
                    )
                    if has_bound_positional_only:
                        logger.debug(
                            "with_telemetry: %s получил positional-only аргументы — прямой вызов без телеметрии",
                            func.__name__,
                        )
                        return func(*args, **kwargs)

                    for name, value in bound.arguments.items():
                        param = parameters.get(name)
                        if param is None:
                            continue
                        if param.kind in (
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            inspect.Parameter.KEYWORD_ONLY,
                        ):
                            merged_kwargs[name] = value
                        elif param.kind == inspect.Parameter.VAR_KEYWORD and isinstance(value, dict):
                            merged_kwargs.update(value)
                except TypeError:
                    # bind_partial повторяет ошибки Python (например duplicate values);
                    # прямой вызов сохранит исходную семантику исключения.
                    return func(*args, **kwargs)
                except ValueError:
                    return func(*args, **kwargs)
                except Exception:
                    # Если сигнатуру получить не удалось — передаём функцию напрямую с исходными аргументами
                    logger.warning(
                        "with_telemetry: не удалось получить сигнатуру %s, вызов без телеметрии",
                        func.__name__,
                    )
                    return func(*args, **kwargs)

            # session_id должен извлекаться ПОСЛЕ преобразования positional args:
            # для функций вида f(session_id, x) вызов f("sid", 1) обязан сохранить "sid".
            session_id = merged_kwargs.get('session_id') or str(uuid.uuid4())[:8]

            # session_id уже захвачен и передаётся явно — убираем из merged_kwargs.
            # Безопасно: run_tool переинъектирует session_id в kwargs функции инструмента
            # (см. "if session_id and 'session_id' not in kwargs"), поэтому инструменты,
            # читающие session_id из своих kwargs, всё равно его получат.
            merged_kwargs.pop('session_id', None)

            # Используем менеджер инструментов
            tool_manager = get_tool_manager()
            return tool_manager.run_tool(
                tool_name=tool_name,
                tool_function=func,
                task_description=description,
                session_id=session_id,
                **merged_kwargs
            )
        return wrapper
    return decorator
