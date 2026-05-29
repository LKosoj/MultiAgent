"""
Локальная телеметрия для smolagents без внешних сервисов
======================================================

Интегрирует OpenTelemetry instrumentation для smolagents с локальным
экспортом трасс в JSONL файлы для просмотра в Streamlit.

Основано на документации: https://huggingface.co/docs/smolagents/tutorials/inspect_runs
"""

import os
try:
    import fcntl
except Exception:
    fcntl = None
import hashlib
import json
import re
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
import threading
from queue import Queue
from urllib.parse import unquote_plus

from backend.fastapi_app.agui.redaction import (
    _redact_payload as _agui_redact_payload,
    redact_pii_in_payload,
)

logger = logging.getLogger(__name__)

_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})")
_MAX_URL_DECODE_DEPTH = 5
_RUN_ID_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _decode_unicode_escapes(value: str) -> str:
    def _replace(match: re.Match) -> str:
        hex_value = match.group(1) or match.group(2)
        return chr(int(hex_value, 16))

    return _UNICODE_ESCAPE_RE.sub(_replace, value)


def _redact_with_agui(value: Any) -> Any:
    return redact_pii_in_payload(
        _agui_redact_payload(
            value,
            _path=("telemetry",),
            _redact_dsn_namespace_scalar_keys=True,
        )
    )


def _redact_text_leaf(value: str) -> str:
    raw_redacted = _redact_with_agui(value)
    raw_text = raw_redacted if isinstance(raw_redacted, str) else str(raw_redacted)

    decoded = value
    for _ in range(_MAX_URL_DECODE_DEPTH):
        next_decoded = unquote_plus(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
        decoded_redacted = _redact_with_agui(decoded)
        decoded_text = (
            decoded_redacted if isinstance(decoded_redacted, str) else str(decoded_redacted)
        )
        if decoded_text != decoded:
            return decoded_text
    if unquote_plus(decoded) != decoded:
        return "<redacted>"
    return raw_text


def _redact_encoded_leaf_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            (_redact_text_leaf(key) if isinstance(key, str) else key): _redact_encoded_leaf_values(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_encoded_leaf_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_encoded_leaf_values(item) for item in value)
    if isinstance(value, str):
        return _redact_text_leaf(value)
    return value


def _redact_payload(value: Any) -> Any:
    return _redact_encoded_leaf_values(_redact_with_agui(value))


def _redact_text(value: str) -> str:
    redacted = _redact_payload(value)
    return redacted if isinstance(redacted, str) else str(redacted)


def _safe_run_id(value: Any) -> str:
    text = str(value or "unknown")
    redacted = _redact_text(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if redacted != text:
        return f"redacted-run-{digest}"
    if (
        text in {".", ".."}
        or not _RUN_ID_FILENAME_RE.fullmatch(text)
        or "/" in text
        or "\\" in text
    ):
        return f"safe-run-{digest}"
    return text


def _trace_file_run_id(value: Any) -> str:
    text = str(value or "")
    if text in {".", ".."} or not _RUN_ID_FILENAME_RE.fullmatch(text):
        raise ValueError("invalid trace run_id")
    return text

# Опциональные импорты для телеметрии
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter, SpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import Span, Status, StatusCode
    from opentelemetry.sdk.trace.export import ReadableSpan
    from openinference.instrumentation.smolagents import SmolagentsInstrumentor
    TELEMETRY_AVAILABLE = True
except ImportError as e:
    logger.warning(f"⚠️ Телеметрия недоступна: {e}")
    logger.warning("Установите: pip install 'smolagents[telemetry]' openinference-instrumentation-smolagents")
    TELEMETRY_AVAILABLE = False

    class SpanExporter:
        def export(self, spans):
            return None

        def shutdown(self):
            return None

    class SpanProcessor:
        def on_start(self, span, parent_context=None):
            return None

        def on_end(self, span):
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

        def shutdown(self):
            return None

    class StatusCode:
        ERROR = "ERROR"
        OK = "OK"

    class _TraceStub:
        @staticmethod
        def get_current_span():
            return None

    trace = _TraceStub()
    Span = Any
    ReadableSpan = Any

@dataclass
class TraceEvent:
    """Событие трассировки"""
    run_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    start_time: datetime
    end_time: Optional[datetime]
    duration_ms: Optional[float]
    status: str
    attributes: Dict[str, Any]
    events: List[Dict[str, Any]]
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь для JSON экспорта"""
        return {
            "run_id": self.run_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
            "events": self.events,
            "error_message": self.error_message
        }


class LocalJSONLExporter(SpanExporter):
    """
    Локальный экспортер трасс в JSONL файлы
    """
    
    def __init__(self, traces_dir: str = "logs/traces"):
        """
        Args:
            traces_dir: Директория для сохранения файлов трасс
        """
        self.traces_dir = Path(traces_dir)
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self._file_handles: Dict[str, Any] = {}
        self._lock = threading.Lock()
        # Кэш соответствий trace_id -> run_id, чтобы наследники без явного run_id
        # попадали в тот же файл трассы, что и корневой span
        self._trace_to_run: Dict[int, str] = {}
        
        logger.info(f"📊 LocalJSONLExporter инициализирован: {self.traces_dir}")

    def export(self, spans: List[ReadableSpan]) -> None:
        """
        Экспорт spans в JSONL файлы
        """
        try:
            for span in spans:
                trace_event = self._convert_span_to_event(span)
                if trace_event:
                    self._write_trace_event(trace_event)
        except Exception as e:
            logger.error(f"❌ Ошибка экспорта трасс: {e}")

    def _convert_span_to_event(self, span: ReadableSpan) -> Optional[TraceEvent]:
        """Конвертация OpenTelemetry span в TraceEvent"""
        try:
            def _decode_text(value: str) -> str:
                if not isinstance(value, str):
                    return value
                if any(token in value for token in ("\\u", "\\U")):
                    try:
                        return _decode_unicode_escapes(value)
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

            # Извлекаем run_id из атрибутов
            attributes = dict(span.attributes) if span.attributes else {}
            trace_id = span.context.trace_id if span and span.context else None

            run_id = attributes.get("run_id") or attributes.get("session.id") or attributes.get("session_id")
            if run_id:
                run_id = _safe_run_id(run_id)
                # Запоминаем соответствие для всех дочерних спанов в этом trace
                if trace_id is not None:
                    with self._lock:
                        self._trace_to_run[trace_id] = run_id
            else:
                # Пробуем восстановить по trace_id, иначе используем unknown
                if trace_id is not None:
                    with self._lock:
                        mapped = self._trace_to_run.get(trace_id)
                    run_id = mapped or "unknown"
                else:
                    run_id = "unknown"
            
            # Вычисляем длительность
            duration_ms = None
            if span.start_time and span.end_time:
                duration_ns = span.end_time - span.start_time
                duration_ms = duration_ns / 1_000_000  # ns -> ms
            
            # Преобразуем статус
            status = "ok"
            error_message = None
            if span.status:
                if span.status.status_code == StatusCode.ERROR:
                    status = "error"
                    error_message = span.status.description
                elif span.status.status_code == StatusCode.OK:
                    status = "ok"
            
            # Извлекаем события
            events = []
            if span.events:
                for event in span.events:
                    events.append({
                        "name": event.name,
                        "timestamp": datetime.fromtimestamp(event.timestamp / 1_000_000_000, tz=timezone.utc).isoformat(),
                        "attributes": dict(event.attributes) if event.attributes else {}
                    })

            attributes = _redact_payload(_normalize_value(attributes))
            events = _redact_payload(_normalize_value(events))
            if isinstance(error_message, str):
                error_message = _redact_text(_decode_text(error_message))
            span_name = _redact_text(_decode_text(span.name))
            
            return TraceEvent(
                run_id=run_id,
                span_id=format(span.context.span_id, '016x'),
                parent_span_id=format(span.parent.span_id, '016x') if span.parent else None,
                name=span_name,
                start_time=datetime.fromtimestamp(span.start_time / 1_000_000_000, tz=timezone.utc) if span.start_time else datetime.now(timezone.utc),
                end_time=datetime.fromtimestamp(span.end_time / 1_000_000_000, tz=timezone.utc) if span.end_time else None,
                duration_ms=duration_ms,
                status=status,
                attributes=attributes,
                events=events,
                error_message=error_message
            )
            
        except Exception as e:
            logger.error(f"❌ Ошибка конвертации span: {e}")
            return None

    def _write_trace_event(self, trace_event: TraceEvent):
        """Запись события в соответствующий JSONL файл"""
        try:
            safe_run_id = _trace_file_run_id(trace_event.run_id)
            with self._lock:
                file_path = self.traces_dir / f"{safe_run_id}.jsonl"
                line = json.dumps(trace_event.to_dict(), ensure_ascii=False) + "\n"
                data = line.encode("utf-8")
                fd = os.open(file_path, os.O_WRONLY | os.O_CREAT)
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                    os.lseek(fd, 0, os.SEEK_END)
                    size_before = os.lseek(fd, 0, os.SEEK_CUR)
                    written = os.write(fd, data)
                    if written != len(data):
                        os.ftruncate(fd, size_before)
                        raise OSError("short write")
                    os.fsync(fd)
                finally:
                    if fcntl is not None:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        except Exception:
                            pass
                    os.close(fd)
                    
        except Exception as e:
            logger.error(f"❌ Ошибка записи трассы для {_redact_text(str(trace_event.run_id))}: {e}")

    def shutdown(self) -> None:
        """Закрытие экспортера"""
        with self._lock:
            for file_handle in self._file_handles.values():
                try:
                    file_handle.close()
                except:
                    pass
            self._file_handles.clear()

class RunIdPropagatingSpanProcessor(SpanProcessor):
    """
    SpanProcessor, который проставляет атрибут run_id для КАЖДОГО создаваемого спана.
    Это гарантирует корректную корреляцию даже если библиотека начинает новый trace.
    
    ПОТОКОБЕЗОПАСНО: Использует threading.local для хранения run_id, что позволяет
    корректно работать при параллельном выполнении нескольких агентов.
    
    Источники run_id (в порядке приоритета):
    1. Потоковая локальная переменная (через unified_logging.get_current_run_id)
    2. Переменная окружения RUN_ID (для обратной совместимости)
    3. Родительский спан
    """

    def on_start(self, span: "Span", parent_context) -> None:  # type: ignore[name-defined]
        try:
            run_id = None
            
            # 1. Приоритет: потокобезопасная локальная переменная
            try:
                from unified_logging import get_current_run_id
                run_id = get_current_run_id()
            except ImportError:
                pass
            
            # 2. Fallback: переменная окружения (для обратной совместимости)
            if not run_id:
                run_id = os.environ.get("RUN_ID")
            
            # 3. Fallback: извлекаем из родительского спана
            if not run_id:
                try:
                    # Получаем текущий активный спан (родитель)
                    current_span = trace.get_current_span()
                    if current_span and current_span.is_recording():
                        # Пробуем получить run_id из атрибутов родителя
                        if hasattr(current_span, 'attributes') and current_span.attributes:
                            run_id = current_span.attributes.get("run_id")
                except Exception:
                    pass
            
            if run_id:
                # Проставляем run_id на текущий спан
                try:
                    span.set_attribute("run_id", run_id)
                except Exception:
                    pass
        except Exception:
            # Никогда не падаем из процессора
            pass

    def on_end(self, span: "Span") -> None:  # type: ignore[name-defined]
        return

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
    
    def shutdown(self) -> None:
        return


class SmolagentsTelemetryManager:
    """
    Менеджер телеметрии для smolagents
    """
    
    def __init__(self, 
                 traces_dir: str = "logs/traces",
                 service_name: str = "multiagent-system",
                 enabled: bool = True):
        """
        Args:
            traces_dir: Директория для сохранения трасс
            service_name: Имя сервиса для OpenTelemetry
            enabled: Включена ли телеметрия
        """
        self.traces_dir = traces_dir
        self.service_name = service_name
        self.enabled = enabled and TELEMETRY_AVAILABLE
        self.instrumentor = None
        self.tracer_provider = None
        
        if self.enabled:
            self._setup_telemetry()
        else:
            logger.warning("⚠️ Телеметрия smolagents отключена или недоступна")

    def _setup_telemetry(self):
        """Настройка OpenTelemetry и SmolagentsInstrumentor"""
        try:
            # Создаем resource
            resource = Resource.create({
                "service.name": self.service_name,
                "service.version": "1.0.0"
            })
            
            # Создаем TracerProvider
            self.tracer_provider = TracerProvider(resource=resource)
            
            # Создаем и добавляем наш локальный экспортер
            exporter = LocalJSONLExporter(self.traces_dir)
            
            # Создаем простой SpanProcessor
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
            processor = SimpleSpanProcessor(exporter)
            self.tracer_provider.add_span_processor(processor)

            # Добавляем процессор, который проставляет run_id на каждом спане
            self.tracer_provider.add_span_processor(RunIdPropagatingSpanProcessor())
            
            # Устанавливаем глобальный tracer provider только если еще не установлен
            try:
                current_provider = trace.get_tracer_provider()
                # Проверяем, является ли текущий провайдер дефолтным (не настроенным)
                if not hasattr(current_provider, 'add_span_processor'):
                    trace.set_tracer_provider(self.tracer_provider)
            except Exception:
                # Если не удалось получить провайдер или возникла ошибка - устанавливаем наш
                try:
                    trace.set_tracer_provider(self.tracer_provider)
                except Exception as set_err:
                    # Игнорируем ошибку "Overriding of current TracerProvider is not allowed"
                    logger.debug(f"TracerProvider уже установлен: {set_err}")
            
            # Инициализируем SmolagentsInstrumentor
            self.instrumentor = SmolagentsInstrumentor()
            self.instrumentor.instrument()
            
            logger.info(f"✅ Телеметрия smolagents настроена: {self.traces_dir}")
            
        except Exception as e:
            logger.error(f"❌ Ошибка настройки телеметрии: {e}")
            self.enabled = False

    def start_run_trace(self, run_id: str, agent_name: str, task: str,
                       profile_type: str = "", pipeline_name: str = "",
                       session_id: str = "") -> Optional[Any]:
        """
        Начать трассировку запуска агента/пайплайна
        
        Args:
            run_id: Идентификатор запуска
            agent_name: Имя агента
            task: Задача для выполнения
            profile_type: Тип профиля агента
            pipeline_name: Имя пайплайна (если применимо)
            
        Returns:
            Span объект или None если телеметрия отключена
        """
        if not self.enabled:
            return None
            
        try:
            tracer = trace.get_tracer(__name__)
            span = tracer.start_span(f"agent_run_{agent_name}")
            
            # Добавляем атрибуты
            span.set_attributes({
                "run_id": run_id,
                "agent_name": agent_name,
                "profile_type": profile_type,
                "pipeline_name": pipeline_name,
                "task": task[:200],  # Ограничиваем длину
                "dynamic_profile": profile_type.startswith("temp_"),
                "start_time": datetime.now().isoformat(),
                "session_id": session_id
            })
            
            return span
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания span для {run_id}: {e}")
            return None

    def add_step_event(self, span, step_name: str, step_data: Dict[str, Any]):
        """Добавить событие шага в трассировку"""
        if not self.enabled or not span:
            return
            
        try:
            span.add_event(f"step_{step_name}", {
                "step_name": step_name,
                "step_data": json.dumps(step_data, default=str)[:500]  # Ограничиваем размер
            })
        except Exception as e:
            logger.error(f"❌ Ошибка добавления события шага: {e}")

    def finish_run_trace(self, span, success: bool = True, error_message: str = None):
        """Завершить трассировку запуска"""
        if not self.enabled or not span:
            return
            
        try:
            if success:
                span.set_status(Status(StatusCode.OK))
            else:
                span.set_status(Status(StatusCode.ERROR, error_message or "Unknown error"))
                
            span.set_attribute("end_time", datetime.now().isoformat())
            span.end()
            # Добавляем служебный маркер о завершении run_id, чтобы UI не считал трассу активной
            try:
                # Пишем минимальное событие в файл, чтобы обновился mtime и счётчик
                exporter = LocalJSONLExporter(self.traces_dir)
                exporter.export_event(span.get_span_context(), {
                    "event": "run_finished",
                    "success": success,
                    "error": error_message or ""
                })
            except Exception:
                pass
            
        except Exception as e:
            logger.error(f"❌ Ошибка завершения span: {e}")

    def get_trace_files(self) -> List[Dict[str, Any]]:
        """
        Получить список файлов трасс
        
        Returns:
            Список файлов с метаданными
        """
        trace_files = []
        traces_path = Path(self.traces_dir)
        
        if not traces_path.exists():
            return trace_files
            
        for jsonl_file in traces_path.glob("*.jsonl"):
            try:
                stat = jsonl_file.stat()
                trace_files.append({
                    "file_path": str(jsonl_file),
                    "run_id": jsonl_file.stem,
                    "size_bytes": stat.st_size,
                    "modified_time": datetime.fromtimestamp(stat.st_mtime),
                    "events_count": self._count_lines(jsonl_file)
                })
            except Exception as e:
                logger.warning(f"⚠️ Ошибка чтения метаданных {jsonl_file}: {e}")
        
        # Сортируем по времени модификации (новые сверху)
        trace_files.sort(key=lambda x: x["modified_time"], reverse=True)
        return trace_files

    def _count_lines(self, file_path: Path) -> int:
        """Подсчет количества строк в файле"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return sum(1 for _ in f)
        except:
            return 0

    def read_trace_events(self, run_id: str) -> List[TraceEvent]:
        """
        Прочитать события трассировки для конкретного запуска
        
        Args:
            run_id: Идентификатор запуска
            
        Returns:
            Список объектов TraceEvent
        """
        events = []
        safe_run_id = _trace_file_run_id(run_id)
        trace_file = Path(self.traces_dir) / f"{safe_run_id}.jsonl"
        
        if not trace_file.exists():
            return events
            
        try:
            with open(trace_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        event_data = json.loads(line)
                        
                        # Конвертируем обратно в TraceEvent
                        event = TraceEvent(
                            run_id=event_data["run_id"],
                            span_id=event_data["span_id"],
                            parent_span_id=event_data.get("parent_span_id"),
                            name=event_data["name"],
                            start_time=datetime.fromisoformat(event_data["start_time"]) if event_data["start_time"] else None,
                            end_time=datetime.fromisoformat(event_data["end_time"]) if event_data["end_time"] else None,
                            duration_ms=event_data.get("duration_ms"),
                            status=event_data["status"],
                            attributes=event_data["attributes"],
                            events=event_data["events"],
                            error_message=event_data.get("error_message")
                        )
                        events.append(event)
                        
        except Exception as e:
            logger.error(f"❌ Ошибка чтения трассы {run_id}: {e}")
        
        # Сортируем по времени старта
        events.sort(key=lambda x: x.start_time if x.start_time else datetime.min)
        return events

    def load_trace_file(self, run_id: str) -> Dict[str, Any]:
        """
        Загрузить содержимое файла трассировки для конкретного запуска
        
        Args:
            run_id: Идентификатор запуска
            
        Returns:
            Словарь с данными трассировки включая spans
        """
        safe_run_id = _trace_file_run_id(run_id)
        trace_events = self.read_trace_events(safe_run_id)
        
        # Преобразуем TraceEvent объекты в словари для совместимости со Streamlit
        spans = []
        for event in trace_events:
            # Преобразуем status из строки в ожидаемую структуру
            status_dict = {
                "status_code": "ERROR" if event.status in ["error", "ERROR"] else "OK"
            }
            
            # Конвертируем время в наносекунды для совместимости
            start_time_nano = None
            end_time_nano = None
            if event.start_time:
                start_time_nano = int(event.start_time.timestamp() * 1_000_000_000)
            if event.end_time:
                end_time_nano = int(event.end_time.timestamp() * 1_000_000_000)
            
            span_dict = {
                "run_id": event.run_id,
                "span_id": event.span_id,
                "parent_span_id": event.parent_span_id,
                "name": event.name,
                "start_time": event.start_time.isoformat() if event.start_time else None,
                "end_time": event.end_time.isoformat() if event.end_time else None,
                "start_time_unix_nano": start_time_nano,
                "end_time_unix_nano": end_time_nano,
                "duration_ms": event.duration_ms,
                "status": status_dict,
                "attributes": event.attributes,
                "events": event.events,
                "error_message": event.error_message
            }
            spans.append(span_dict)
        
        return {
            "run_id": safe_run_id,
            "spans": spans,
            "total_spans": len(spans)
        }

    def cleanup_old_traces(self, max_age_days: int = 7):
        """
        Очистка старых файлов трасс
        
        Args:
            max_age_days: Максимальный возраст файлов в днях
        """
        traces_path = Path(self.traces_dir)
        
        if not traces_path.exists():
            return
            
        current_time = datetime.now()
        removed_count = 0
        
        for jsonl_file in traces_path.glob("*.jsonl"):
            try:
                file_age = current_time - datetime.fromtimestamp(jsonl_file.stat().st_mtime)
                
                if file_age.days > max_age_days:
                    jsonl_file.unlink()
                    removed_count += 1
                    
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при удалении {jsonl_file}: {e}")
        
        if removed_count > 0:
            logger.info(f"🧹 Удалено {removed_count} старых файлов трасс")
        return removed_count

    def is_enabled(self) -> bool:
        """Проверить, включена ли телеметрия"""
        return self.enabled

    def disable(self):
        """Отключить телеметрию"""
        if self.instrumentor:
            try:
                self.instrumentor.uninstrument()
            except:
                pass
        self.enabled = False
        logger.info("🔇 Телеметрия smolagents отключена")

    def enable(self):
        """Включить телеметрию"""
        if TELEMETRY_AVAILABLE and not self.enabled:
            self._setup_telemetry()

    def check_and_mark_incomplete_traces(self) -> Dict[str, Any]:
        """
        Проверяет все трассы и помечает незавершенные как содержащие ошибки
        
        Returns:
            Словарь с результатами проверки
        """
        from .helpers import is_trace_completed
        
        result = {
            "total_traces": 0,
            "incomplete_traces": 0,
            "marked_traces": [],
            "errors": []
        }
        
        try:
            trace_files = self.get_trace_files()
            result["total_traces"] = len(trace_files)
            
            for trace_file_meta in trace_files:
                try:
                    run_id = trace_file_meta["run_id"]
                    # Загружаем "сырые" события, а не обработанные спаны
                    events = self._read_raw_trace_events(run_id)
                    
                    if not events:
                        continue
                    
                    # Конвертируем сырые события в спаны для проверки
                    spans_for_check = self._convert_events_to_spans_for_check(events)

                    # Проверяем завершенность трассы
                    if not is_trace_completed(spans_for_check):
                        result["incomplete_traces"] += 1
                        
                        # Помечаем незавершенную трассу как содержащую ошибку, работая с сырыми событиями
                        marked = self._mark_trace_as_error(run_id, "Трасса не завершена - прерван процесс")
                        if marked:
                            result["marked_traces"].append({
                                "run_id": run_id,
                                "reason": "incomplete",
                                "timestamp": datetime.now().isoformat()
                            })
                        
                except Exception as e:
                    error_msg = f"Ошибка обработки трассы {trace_file_meta.get('run_id', 'unknown')}: {e}"
                    result["errors"].append(error_msg)
                    logger.warning(error_msg)
            
            if result["marked_traces"]:
                logger.info(f"🔍 Проверка трасс: помечено {len(result['marked_traces'])} из {result['total_traces']} незавершенных трасс")
            else:
                logger.info(f"✅ Проверка трасс: все {result['total_traces']} трасс завершены корректно")
                
        except Exception as e:
            error_msg = f"Критическая ошибка при проверке трасс: {e}"
            result["errors"].append(error_msg)
            logger.error(error_msg)
        
        return result

    def _mark_trace_as_error(self, run_id: str, error_reason: str) -> bool:
        """
        Помечает трассу как содержащую ошибку путем изменения файла трассировки.
        
        Args:
            run_id: Идентификатор трассы
            error_reason: Причина ошибки
            
        Returns:
            True если трасса была помечена, False в случае ошибки
        """
        try:
            safe_run_id = _trace_file_run_id(run_id)
            trace_file = Path(self.traces_dir) / f"{safe_run_id}.jsonl"
            
            if not trace_file.exists():
                return False
            
            # Читаем оригинальные события как есть
            original_events = self._read_raw_trace_events(safe_run_id)
            
            if not original_events:
                return False
            
            redacted_reason = _redact_text(error_reason)

            # Стратегии пометки незавершенных трасс:
            # 1. Корневые спаны агентов (agent_run_*)
            # 2. Любые корневые спаны  
            # 3. Если нет корневых спанов - помечаем все спаны или добавляем новый error спан
            
            modified = False
            
            # Стратегия 1: Корневые спаны агентов
            agent_root_spans = [e for e in original_events 
                              if not e.get("parent_span_id") and 
                              e.get("name", "").lower().startswith("agent_run_")]
            
            if agent_root_spans:
                for event in agent_root_spans:
                    if event.get("status") != "error":
                        event["status"] = "error"
                        event["error_message"] = redacted_reason
                        # Устанавливаем время окончания, если его нет
                        if not event.get("end_time"):
                            event["end_time"] = datetime.now().isoformat()
                        self._add_error_event(event, redacted_reason)
                        modified = True
            
            # Стратегия 2: Любые корневые спаны если нет agent_run_
            else:
                root_spans = [e for e in original_events if not e.get("parent_span_id")]
                
                if root_spans:
                    # Помечаем все корневые спаны без времени окончания
                    for event in root_spans:
                        if not event.get("end_time"):
                            if event.get("status") != "error":
                                event["status"] = "error"
                                event["error_message"] = redacted_reason
                                event["end_time"] = datetime.now().isoformat()
                                self._add_error_event(event, redacted_reason)
                                modified = True
                
                # Стратегия 3: Нет корневых спанов - добавляем новый error спан
                else:
                    # Находим последнее событие, чтобы сохранить хронологию
                    last_start_time = datetime.min
                    if original_events:
                        last_start_time_str = original_events[-1].get("start_time")
                        if last_start_time_str:
                            try:
                                last_start_time = datetime.fromisoformat(last_start_time_str)
                            except ValueError:
                                last_start_time = datetime.now()
                        else:
                            last_start_time = datetime.now()
                    else:
                        last_start_time = datetime.now()

                    error_span = {
                        "run_id": safe_run_id,
                        "span_id": f"error_{int(datetime.now().timestamp())}",
                        "parent_span_id": None,
                        "name": "trace_incomplete_error",
                        "start_time": last_start_time.isoformat(),
                        "end_time": last_start_time.isoformat(),
                        "duration_ms": 0,
                        "status": "error",
                        "attributes": {
                            "run_id": safe_run_id,
                            "artificial_span": True,
                            "marked_at_startup": True
                        },
                        "events": [],
                        "error_message": redacted_reason
                    }
                    self._add_error_event(error_span, redacted_reason)
                    original_events.append(error_span)
                    modified = True
            
            # Перезаписываем файл с обновленными событиями
            if modified:
                original_events = _redact_payload(original_events)
                with open(trace_file, 'w', encoding='utf-8') as f:
                    for event in original_events:
                        json.dump(event, f, ensure_ascii=False)
                        f.write('\n')
                
                logger.debug(
                    f"Трасса {safe_run_id} помечена как содержащая ошибку: {redacted_reason}"
                )
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Ошибка при пометке трассы {_redact_text(str(run_id))}: {e}")
            return False

    def _add_error_event(self, span_event: Dict, error_reason: str):
        """Добавляет событие об ошибке к спану"""
        error_event = {
            "name": "trace_marked_incomplete",
            "timestamp": datetime.now().isoformat(),
            "attributes": {
                "reason": error_reason,
                "marked_at_startup": True
            }
        }
        
        if "events" not in span_event:
            span_event["events"] = []
        span_event["events"].append(error_event)

    def _read_raw_trace_events(self, run_id: str) -> List[Dict]:
        """Читает 'сырые' события из файла трассировки"""
        events = []
        safe_run_id = _trace_file_run_id(run_id)
        trace_file = Path(self.traces_dir) / f"{safe_run_id}.jsonl"
        if not trace_file.exists():
            return events
        
        try:
            with open(trace_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Ошибка чтения raw-трассы {run_id}: {e}")
            
        return events

    def _convert_events_to_spans_for_check(self, events: List[Dict]) -> List[Dict]:
        """Упрощенный конвертер для функции is_trace_completed"""
        spans = []
        for event in events:
            # is_trace_completed нужна только эта информация
            spans.append({
                "name": event.get("name"),
                "parent_span_id": event.get("parent_span_id"),
                "end_time_unix_nano": int(datetime.fromisoformat(event["end_time"]).timestamp() * 1_000_000_000) if event.get("end_time") else None
            })
        return spans


# Глобальный экземпляр менеджера телеметрии
_telemetry_manager: Optional[SmolagentsTelemetryManager] = None

def get_telemetry_manager(traces_dir: str = "logs/traces", 
                         service_name: str = "multiagent-system",
                         enabled: bool = True) -> SmolagentsTelemetryManager:
    """
    Получить глобальный экземпляр менеджера телеметрии
    
    Args:
        traces_dir: Директория для сохранения трасс
        service_name: Имя сервиса
        enabled: Включена ли телеметрия
        
    Returns:
        Экземпляр SmolagentsTelemetryManager
    """
    global _telemetry_manager
    
    if _telemetry_manager is None:
        _telemetry_manager = SmolagentsTelemetryManager(
            traces_dir=traces_dir,
            service_name=service_name,
            enabled=enabled
        )
    
    return _telemetry_manager

def configure_telemetry(enabled: bool = True, traces_dir: str = "logs/traces"):
    """
    Настроить телеметрию smolagents
    
    Args:
        enabled: Включить/отключить телеметрию
        traces_dir: Директория для трасс
    """
    manager = get_telemetry_manager(traces_dir=traces_dir, enabled=enabled)
    
    if enabled and not manager.is_enabled():
        manager.enable()
    elif not enabled and manager.is_enabled():
        manager.disable()
        
    return manager
