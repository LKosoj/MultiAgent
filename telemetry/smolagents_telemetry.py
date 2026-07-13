"""
Локальная телеметрия для smolagents без внешних сервисов
======================================================

Интегрирует OpenTelemetry instrumentation для smolagents с локальным
экспортом трасс в JSONL файлы для просмотра в Streamlit.

Основано на документации: https://huggingface.co/docs/smolagents/tutorials/inspect_runs
"""

import os
import tempfile
from contextlib import contextmanager
try:
    import fcntl
except Exception:
    fcntl = None
import hashlib
import json
import re
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import OrderedDict
from typing import Dict, Any, List, Optional, Tuple
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
_DEFAULT_MAX_TRACE_FILE_SIZE_BYTES = 10 * 1024 * 1024
_TRUNCATE_TEXT_PREFIX_BYTES = 200
_TRUNCATE_SUFFIX = "...(truncated)"


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


def _max_trace_file_size_bytes(value: Optional[int]) -> int:
    if value is None:
        value = _DEFAULT_MAX_TRACE_FILE_SIZE_BYTES
    if isinstance(value, bool) or int(value) <= 0:
        raise ValueError("max_trace_file_size_bytes must be positive")
    return int(value)


def _max_trace_file_size_mb_to_bytes(value: float) -> int:
    if isinstance(value, bool):
        raise ValueError("max_trace_file_size_mb must be positive")
    try:
        size_mb = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_trace_file_size_mb must be positive") from exc
    if not 0 < size_mb < float("inf"):
        raise ValueError("max_trace_file_size_mb must be positive")
    size_bytes = int(size_mb * 1024 * 1024)
    if size_bytes <= 0:
        raise ValueError("max_trace_file_size_mb is smaller than one byte")
    return size_bytes


@contextmanager
def _trace_run_lock(
    traces_dir: Path, run_id: str, *, exclusive: bool, ensure_dir: bool = True
):
    """Use one stable per-run flock while data files are renamed or replaced."""
    safe_run_id = _trace_file_run_id(run_id)
    if ensure_dir:
        traces_dir.mkdir(parents=True, exist_ok=True)
    lock_path = traces_dir / f".{safe_run_id}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(fd, operation)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
        os.close(fd)


def _first_event_run_id(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8") as trace_file:
            for line in trace_file:
                if not line.strip():
                    continue
                value = json.loads(line)
                run_id = value.get("run_id") if isinstance(value, dict) else None
                return _trace_file_run_id(run_id) if run_id else None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return None


def _segment_number(path: Path, run_id: str) -> Optional[int]:
    prefix = f"{run_id}."
    stem = path.stem
    if not stem.startswith(prefix):
        return None
    suffix = stem[len(prefix):]
    if not suffix.isdigit() or int(suffix) < 1:
        return None
    return int(suffix)


def _ordered_trace_paths(traces_dir: Path, run_id: str) -> List[Path]:
    safe_run_id = _trace_file_run_id(run_id)
    base_path = traces_dir / f"{safe_run_id}.jsonl"
    segments = []
    for candidate in traces_dir.glob(f"{safe_run_id}.*.jsonl"):
        segment = _segment_number(candidate, safe_run_id)
        if segment is None:
            continue
        event_run_id = _first_event_run_id(candidate)
        if event_run_id == safe_run_id or (event_run_id is None and base_path.exists()):
            segments.append((segment, candidate))
    paths = [path for _, path in sorted(segments)]
    if base_path.exists():
        paths.append(base_path)
    return paths


def _read_trace_lines(paths: List[Path]) -> List[bytes]:
    lines = []
    for path in paths:
        for line in path.read_bytes().splitlines(keepends=True):
            lines.append(line if line.endswith(b"\n") else line + b"\n")
    return lines


def _chunk_trace_lines(lines: List[bytes], max_bytes: int) -> List[bytes]:
    chunks: List[bytes] = []
    current = bytearray()
    for line in lines:
        if current and len(current) + len(line) > max_bytes:
            chunks.append(bytes(current))
            current.clear()
        current.extend(line)
    if current:
        chunks.append(bytes(current))
    return chunks


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _truncate_trace_event_dict(
    event_dict: Dict[str, Any], max_bytes: int
) -> Optional[Dict[str, Any]]:
    """Ensure a single trace event dict serializes within max_bytes.

    Runs at most three whole-dict size checks (fast path, attributes/events
    stripped, name/error_message capped) with no recursion or looping.
    Returns the possibly-modified dict, or None if it still does not fit
    after every stage - the caller must drop the record in that case.
    """
    if _json_size(event_dict) <= max_bytes:
        return event_dict

    # Stage 1: attributes/events are normally the bulk of the payload.
    # Scalar fields (run_id/span_id/parent_span_id/name/start_time/end_time/
    # duration_ms/status/error_message) are left untouched.
    original_attributes = event_dict.get("attributes")
    try:
        original_attributes_bytes = _json_size(original_attributes)
    except (TypeError, ValueError):
        original_attributes_bytes = -1

    stage1 = dict(event_dict)
    stage1_attributes: Dict[str, Any] = {
        "_truncated": True,
        "_original_attributes_bytes": original_attributes_bytes,
    }
    # Preserve "openinference.span.kind" (a short enum such as "LLM"/"AGENT"/
    # "TOOL") because get_trace_status()/_is_llm_span() in telemetry/helpers.py
    # key off it to recognize LLM spans for recovered-error detection. Losing
    # it here would make a truncated OK LLM span invisible to that check and
    # could flip an otherwise-recovered trace's status to "error". We
    # deliberately do NOT preserve "output.value" even though get_trace_status()
    # also inspects it (to decide whether an OK LLM span "recovers" a later
    # error) - it is typically the multi-KB attribute that triggered this
    # truncation in the first place. Limitation: a truncated OK LLM span is
    # still recognized as an LLM span, but will not count towards recovering a
    # later error, since its output.value is gone.
    if isinstance(original_attributes, dict):
        span_kind = original_attributes.get("openinference.span.kind")
        if isinstance(span_kind, str) and len(span_kind.encode("utf-8")) <= 64:
            stage1_attributes["openinference.span.kind"] = span_kind
    stage1["attributes"] = stage1_attributes
    stage1["events"] = []
    if _json_size(stage1) <= max_bytes:
        return stage1

    # Stage 2: cap name/error_message to a limited prefix.
    stage2 = dict(stage1)
    budget = max(0, _TRUNCATE_TEXT_PREFIX_BYTES - len(_TRUNCATE_SUFFIX))
    for key in ("name", "error_message"):
        value = stage2.get(key)
        if isinstance(value, str) and value:
            # budget is a byte budget (_TRUNCATE_TEXT_PREFIX_BYTES caps the
            # serialized JSONL line and _json_size counts UTF-8 bytes), but
            # slicing a Python str slices by *character*. Multi-byte text
            # (e.g. Cyrillic, 2 bytes/char) would overshoot the byte budget
            # by up to ~2-4x if sliced by character instead. Encode first,
            # slice the bytes, then decode with errors="ignore" to drop a
            # partial trailing multi-byte sequence left at the cut boundary.
            stage2[key] = (
                value.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
                + _TRUNCATE_SUFFIX
            )
    if _json_size(stage2) <= max_bytes:
        return stage2

    # Stage 3: even the minimal skeleton does not fit - drop the record.
    logger.error(
        f"❌ Событие трассы run_id={event_dict.get('run_id')} "
        f"span_id={event_dict.get('span_id')} не помещается в лимит "
        f"{max_bytes} байт даже после усечения, запись отброшена"
    )
    return None


def _truncate_oversized_lines(
    lines: List[bytes], max_bytes: int
) -> Tuple[List[bytes], int, int]:
    """Force every raw JSONL line to fit within max_bytes.

    Returns (healed_lines, truncated_count, dropped_count).
    """
    healed: List[bytes] = []
    truncated_count = 0
    dropped_count = 0
    for line in lines:
        if len(line) <= max_bytes:
            healed.append(line)
            continue
        try:
            event_dict = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error(f"❌ Не удалось разобрать строку трассы для усечения: {exc}")
            dropped_count += 1
            continue
        truncated_dict = _truncate_trace_event_dict(event_dict, max_bytes)
        if truncated_dict is None:
            dropped_count += 1
            continue
        new_line = (json.dumps(truncated_dict, ensure_ascii=False) + "\n").encode("utf-8")
        if len(new_line) > max_bytes:
            logger.error(
                f"❌ Событие трассы run_id={truncated_dict.get('run_id')} "
                f"span_id={truncated_dict.get('span_id')} всё ещё превышает лимит "
                "после усечения, запись отброшена"
            )
            dropped_count += 1
            continue
        healed.append(new_line)
        truncated_count += 1
    return healed, truncated_count, dropped_count


def _stage_bytes(target: Path, data: bytes) -> Path:
    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(fd)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)
    return temporary_path


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rewrite_trace_lines_locked(
    traces_dir: Path,
    run_id: str,
    lines: List[bytes],
    max_bytes: int,
) -> List[Path]:
    """Replace one logical trace with complete-line chunks; caller holds EX lock."""
    safe_run_id = _trace_file_run_id(run_id)
    old_paths = _ordered_trace_paths(traces_dir, safe_run_id)
    chunks = _chunk_trace_lines(lines, max_bytes)
    if not chunks:
        return old_paths

    old_path_set = set(old_paths)
    targets: List[Path] = []
    candidate_number = 1
    for _ in chunks[:-1]:
        while True:
            candidate = traces_dir / f"{safe_run_id}.{candidate_number}.jsonl"
            candidate_number += 1
            if not candidate.exists() or candidate in old_path_set:
                targets.append(candidate)
                break
    targets.append(traces_dir / f"{safe_run_id}.jsonl")

    staged = [_stage_bytes(target, data) for target, data in zip(targets, chunks)]
    try:
        for temporary_path, target in zip(staged, targets):
            os.replace(temporary_path, target)
        for obsolete_path in old_path_set.difference(targets):
            obsolete_path.unlink(missing_ok=True)
        _fsync_directory(traces_dir)
    finally:
        for temporary_path in staged:
            temporary_path.unlink(missing_ok=True)
    return targets


def _next_segment_path(traces_dir: Path, run_id: str) -> Path:
    existing_numbers = [
        number
        for path in _ordered_trace_paths(traces_dir, run_id)
        if (number := _segment_number(path, run_id)) is not None
    ]
    number = max(existing_numbers, default=0) + 1
    while (candidate := traces_dir / f"{run_id}.{number}.jsonl").exists():
        number += 1
    return candidate

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


@dataclass
class _RunWriteState:
    """Cached per-run write bookkeeping to avoid a glob/stat on every span."""
    base_size: int
    next_segment_number: int
    verified_clean: bool


class LocalJSONLExporter(SpanExporter):
    """
    Локальный экспортер трасс в JSONL файлы
    """

    def __init__(
        self,
        traces_dir: str = "logs/traces",
        max_trace_file_size_bytes: Optional[int] = None,
    ):
        """
        Args:
            traces_dir: Директория для сохранения файлов трасс
        """
        self.traces_dir = Path(traces_dir)
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self._dir_created = True
        self.max_trace_file_size_bytes = _max_trace_file_size_bytes(
            max_trace_file_size_bytes
        )
        self._lock = threading.Lock()
        # Кэш соответствий trace_id -> run_id, чтобы наследники без явного run_id
        # попадали в тот же файл трассы, что и корневой span
        # Ограничен 10 000 записями для предотвращения утечки памяти при долгой работе
        self._trace_to_run: OrderedDict[int, str] = OrderedDict()
        self._trace_to_run_max = 10_000
        # Кэш write-state на run_id (base_size/next_segment_number/verified_clean),
        # чтобы не дёргать glob+stat всех сегментов на каждый span одного рана.
        self._run_states: "OrderedDict[str, _RunWriteState]" = OrderedDict()
        self._run_states_max = 10_000

        logger.info(f"📊 LocalJSONLExporter инициализирован: {self.traces_dir}")

    def invalidate_run_cache(self, run_id: str) -> None:
        """Drop the cached write-state for run_id, forcing a full recompute."""
        with self._lock:
            self._run_states.pop(run_id, None)

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
                        self._trace_to_run.move_to_end(trace_id)
                        if len(self._trace_to_run) > self._trace_to_run_max:
                            self._trace_to_run.popitem(last=False)
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
            event_dict = _truncate_trace_event_dict(
                trace_event.to_dict(), self.max_trace_file_size_bytes
            )
            if event_dict is None:
                return
            line = json.dumps(event_dict, ensure_ascii=False) + "\n"
            data = line.encode("utf-8")
            try:
                self._write_trace_line_locked(safe_run_id, data)
            except FileNotFoundError:
                # traces_dir was removed out from under a live exporter (e.g.
                # an external cleanup process deleted it). _dir_created no
                # longer reflects reality, so every future write would keep
                # hitting the same FileNotFoundError forever - reset the flag
                # and the cached write-state for this run, then retry exactly
                # once with ensure_dir=True to recreate the directory. A
                # second failure is handled like any other write error below.
                self._dir_created = False
                self.invalidate_run_cache(safe_run_id)
                self._write_trace_line_locked(safe_run_id, data)

        except Exception as e:
            logger.error(f"❌ Ошибка записи трассы для {_redact_text(str(trace_event.run_id))}: {e}")

    def _write_trace_line_locked(self, safe_run_id: str, data: bytes) -> None:
        """Append one already-encoded JSONL line for safe_run_id under its lock."""
        with self._lock:
            with _trace_run_lock(
                self.traces_dir,
                safe_run_id,
                exclusive=True,
                ensure_dir=not self._dir_created,
            ):
                file_path = self.traces_dir / f"{safe_run_id}.jsonl"
                state = self._run_states.get(safe_run_id)
                cache_hit = False
                if state is not None and state.verified_clean:
                    try:
                        actual_size = file_path.stat().st_size
                    except FileNotFoundError:
                        actual_size = 0
                    if actual_size == state.base_size:
                        cache_hit = True
                        size_before = actual_size
                        next_segment_number = state.next_segment_number

                if cache_hit:
                    # Verified-clean cache hit: one stat of the known base
                    # path, no glob/first-line/full-segment scan needed.
                    if (
                        size_before > 0
                        and size_before + len(data) > self.max_trace_file_size_bytes
                    ):
                        # Do not trust the cached next_segment_number blindly:
                        # another process (streamlit, agui service, workflow
                        # worker) writing into the same traces_dir may have
                        # already claimed that segment number since this
                        # cache entry was populated. _next_segment_path()
                        # re-checks existence on disk before returning, so
                        # os.replace() below can never silently clobber a
                        # segment file created by another writer.
                        rotated_path = _next_segment_path(
                            self.traces_dir, safe_run_id
                        )
                        os.replace(file_path, rotated_path)
                        _fsync_directory(self.traces_dir)
                        size_before = 0
                        chosen_number = _segment_number(rotated_path, safe_run_id)
                        next_segment_number = (
                            chosen_number
                            if chosen_number is not None
                            else next_segment_number
                        ) + 1
                else:
                    # Cache miss or drift: recompute the full picture from disk.
                    paths = _ordered_trace_paths(self.traces_dir, safe_run_id)
                    if any(
                        path.stat().st_size > self.max_trace_file_size_bytes
                        for path in paths
                    ):
                        healed_lines, _, _ = _truncate_oversized_lines(
                            _read_trace_lines(paths),
                            self.max_trace_file_size_bytes,
                        )
                        _rewrite_trace_lines_locked(
                            self.traces_dir,
                            safe_run_id,
                            healed_lines,
                            self.max_trace_file_size_bytes,
                        )
                        paths = _ordered_trace_paths(self.traces_dir, safe_run_id)

                    size_before = file_path.stat().st_size if file_path.exists() else 0
                    if (
                        size_before > 0
                        and size_before + len(data) > self.max_trace_file_size_bytes
                    ):
                        os.replace(
                            file_path,
                            _next_segment_path(self.traces_dir, safe_run_id),
                        )
                        _fsync_directory(self.traces_dir)
                        size_before = 0
                        paths = _ordered_trace_paths(self.traces_dir, safe_run_id)

                    existing_numbers = [
                        number
                        for path in paths
                        if (number := _segment_number(path, safe_run_id)) is not None
                    ]
                    next_segment_number = max(existing_numbers, default=0) + 1

                fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX)
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

                self._run_states[safe_run_id] = _RunWriteState(
                    base_size=size_before + len(data),
                    next_segment_number=next_segment_number,
                    verified_clean=True,
                )
                self._run_states.move_to_end(safe_run_id)
                if len(self._run_states) > self._run_states_max:
                    self._run_states.popitem(last=False)

    def shutdown(self) -> None:
        """Закрытие экспортера"""
        pass

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
                 enabled: bool = True,
                 max_trace_file_size_bytes: Optional[int] = None):
        """
        Args:
            traces_dir: Директория для сохранения трасс
            service_name: Имя сервиса для OpenTelemetry
            enabled: Включена ли телеметрия
        """
        self.traces_dir = traces_dir
        self.service_name = service_name
        self.max_trace_file_size_bytes = _max_trace_file_size_bytes(
            max_trace_file_size_bytes
        )
        self.enabled = enabled and TELEMETRY_AVAILABLE
        self.instrumentor = None
        self.tracer_provider = None
        self.exporter: Optional[LocalJSONLExporter] = None
        
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
            self.exporter = LocalJSONLExporter(
                self.traces_dir,
                max_trace_file_size_bytes=self.max_trace_file_size_bytes,
            )
            
            # Создаем простой SpanProcessor
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
            processor = SimpleSpanProcessor(self.exporter)
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
            
        except Exception as e:
            logger.error(f"❌ Ошибка завершения span: {e}")

    def get_trace_files(self, *, include_events_count: bool = True) -> List[Dict[str, Any]]:
        """
        Получить список файлов трасс

        Args:
            include_events_count: Если False, events_count не читается из файлов
                (None в результате) - экономит чтение файлов для вызовов,
                которым нужны только метаданные размера/времени.

        Returns:
            Список файлов с метаданными
        """
        trace_files = []
        traces_path = Path(self.traces_dir)
        
        if not traces_path.exists():
            return trace_files
            
        grouped_files: Dict[str, List[Path]] = {}
        for jsonl_file in traces_path.glob("*.jsonl"):
            run_id = _first_event_run_id(jsonl_file) or jsonl_file.stem
            try:
                run_id = _trace_file_run_id(run_id)
            except ValueError:
                logger.warning(f"⚠️ Некорректное имя файла трассы {jsonl_file}")
                continue
            grouped_files.setdefault(run_id, []).append(jsonl_file)

        for run_id, physical_files in grouped_files.items():
            try:
                ordered_files = sorted(
                    physical_files,
                    key=lambda path: (
                        _segment_number(path, run_id) is None,
                        _segment_number(path, run_id) or 0,
                    ),
                )
                stats = [path.stat() for path in ordered_files]
                base_path = traces_path / f"{run_id}.jsonl"
                trace_files.append({
                    "file_path": str(base_path if base_path in ordered_files else ordered_files[-1]),
                    "file_paths": [str(path) for path in ordered_files],
                    "run_id": run_id,
                    "size_bytes": sum(stat.st_size for stat in stats),
                    "modified_time": datetime.fromtimestamp(
                        max(stat.st_mtime for stat in stats)
                    ),
                    "events_count": (
                        sum(self._count_lines(path) for path in ordered_files)
                        if include_events_count
                        else None
                    ),
                    "segments_count": len(ordered_files),
                })
            except Exception as e:
                logger.warning(f"⚠️ Ошибка чтения метаданных трассы {run_id}: {e}")
        
        # Сортируем по времени модификации (новые сверху)
        trace_files.sort(key=lambda x: x["modified_time"], reverse=True)
        return trace_files

    def _count_lines(self, file_path: Path) -> int:
        """Подсчет количества строк в файле"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return sum(1 for _ in f)
        except Exception:
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
        for event_data in self._read_raw_trace_events(safe_run_id):
            try:
                events.append(TraceEvent(
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
                ))
            except (KeyError, TypeError, ValueError) as e:
                logger.error(f"❌ Ошибка чтения трассы {safe_run_id}: {e}")

        def _sort_key(event: TraceEvent):
            st = event.start_time
            if st is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            return st if st.tzinfo is not None else st.replace(tzinfo=timezone.utc)

        events.sort(key=_sort_key)
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
        return self.cleanup_old_traces_report(max_age_days=max_age_days)["deleted_count"]

    def cleanup_old_traces_report(self, max_age_days: int = 7) -> Dict[str, Any]:
        """Delete expired physical trace files and report exact outcomes."""
        result = {
            "deleted_files": [],
            "deleted_count": 0,
            "deleted_bytes": 0,
            "failures": [],
        }
        traces_path = Path(self.traces_dir)
        if not traces_path.exists():
            return result

        cutoff = datetime.now().timestamp() - timedelta(days=max_age_days).total_seconds()
        for trace_meta in self.get_trace_files(include_events_count=False):
            run_id = trace_meta["run_id"]
            attempted_deletion = False
            try:
                with _trace_run_lock(traces_path, run_id, exclusive=True):
                    trace_paths = _ordered_trace_paths(traces_path, run_id)
                    if not trace_paths:
                        continue
                    path_stats = [(path, path.stat()) for path in trace_paths]
                    if max(stat.st_mtime for _, stat in path_stats) >= cutoff:
                        continue

                    # A filesystem cannot unlink several files atomically. Holding
                    # the run lock prevents writers/readers from observing an
                    # interleaved partial deletion; every individual outcome is
                    # still reported exactly below.
                    attempted_deletion = True
                    run_had_failure = False
                    for trace_path, stat in path_stats:
                        try:
                            trace_path.unlink()
                            result["deleted_files"].append(str(trace_path))
                            result["deleted_count"] += 1
                            result["deleted_bytes"] += stat.st_size
                        except Exception as e:
                            run_had_failure = True
                            result["failures"].append({
                                "file_path": str(trace_path),
                                "error": str(e),
                            })
                            logger.warning(
                                f"⚠️ Ошибка при удалении {trace_path}: {e}"
                            )

                    if not run_had_failure:
                        safe_run_id = _trace_file_run_id(run_id)
                        (traces_path / f".{safe_run_id}.lock").unlink(missing_ok=True)
            except Exception as e:
                result["failures"].append({
                    "run_id": run_id,
                    "error": str(e),
                })
                logger.warning(f"⚠️ Ошибка при удалении трассы {run_id}: {e}")
            # Invalidate after releasing the run flock: LocalJSONLExporter's
            # own lock ordering is self._lock -> flock, so acquiring
            # self._lock while still holding the flock here would invert it.
            if attempted_deletion and self.exporter is not None:
                self.exporter.invalidate_run_cache(run_id)

        result["deleted_files"].sort()
        result["failures"].sort(
            key=lambda failure: failure.get("file_path", failure.get("run_id", ""))
        )

        if result["deleted_count"]:
            logger.info(f"🧹 Удалено {result['deleted_count']} старых файлов трасс")
        return result

    def enforce_trace_size_limit(self) -> Dict[str, Any]:
        """Split oversized logical traces without breaking JSONL records."""
        traces_path = Path(self.traces_dir)
        result = {
            "rotations": 0,
            "files": [],
            "bytes_before": 0,
            "bytes_after": 0,
            "failures": [],
        }
        if not traces_path.exists():
            return result

        result["bytes_before"] = sum(
            path.stat().st_size for path in traces_path.glob("*.jsonl")
        )
        for trace_meta in self.get_trace_files(include_events_count=False):
            run_id = trace_meta["run_id"]
            rewrote = False
            try:
                with _trace_run_lock(traces_path, run_id, exclusive=True):
                    paths = _ordered_trace_paths(traces_path, run_id)
                    if not paths or not any(
                        path.stat().st_size > self.max_trace_file_size_bytes
                        for path in paths
                    ):
                        continue
                    lines = _read_trace_lines(paths)
                    healed_lines, truncated_count, dropped_count = _truncate_oversized_lines(
                        lines, self.max_trace_file_size_bytes
                    )
                    target_paths = _rewrite_trace_lines_locked(
                        traces_path,
                        run_id,
                        healed_lines,
                        self.max_trace_file_size_bytes,
                    )
                    rewrote = True
                    result["rotations"] += max(0, len(target_paths) - 1)
                    result["files"].extend(str(path) for path in target_paths)
                    if truncated_count:
                        result["failures"].append({
                            "run_id": run_id,
                            "error": (
                                f"{truncated_count} JSONL record(s) exceeded "
                                "max_trace_file_size_bytes and were truncated"
                            ),
                        })
                    if dropped_count:
                        result["failures"].append({
                            "run_id": run_id,
                            "error": (
                                f"{dropped_count} JSONL record(s) exceeded "
                                "max_trace_file_size_bytes even after truncation "
                                "and were dropped"
                            ),
                        })
            except Exception as e:
                result["failures"].append({"run_id": run_id, "error": str(e)})
                logger.warning(f"⚠️ Ошибка ротации трассы {run_id}: {e}")
            # Invalidate after releasing the run flock: LocalJSONLExporter's
            # own lock ordering is self._lock -> flock, so acquiring
            # self._lock while still holding the flock here would invert it.
            if rewrote and self.exporter is not None:
                self.exporter.invalidate_run_cache(run_id)

        result["bytes_after"] = sum(
            path.stat().st_size for path in traces_path.glob("*.jsonl")
        )
        return result

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
        try:
            safe_run_id = _trace_file_run_id(run_id)
            with _trace_run_lock(Path(self.traces_dir), safe_run_id, exclusive=True):
                marked = self._mark_trace_as_error_locked(safe_run_id, error_reason)
            # Invalidate after releasing the run flock: LocalJSONLExporter's
            # own lock ordering is self._lock -> flock, so acquiring
            # self._lock while still holding the flock here would invert it.
            if marked and self.exporter is not None:
                self.exporter.invalidate_run_cache(safe_run_id)
            return marked
        except Exception as e:
            logger.error(f"Ошибка при пометке трассы {_redact_text(str(run_id))}: {e}")
            return False

    def _mark_trace_as_error_locked(self, run_id: str, error_reason: str) -> bool:
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
            traces_path = Path(self.traces_dir)
            trace_paths = _ordered_trace_paths(traces_path, safe_run_id)

            if not trace_paths:
                return False
            
            # Читаем оригинальные события как есть
            original_events = self._read_raw_trace_events_unlocked(safe_run_id)
            
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
                lines = [
                    (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
                    for event in original_events
                ]
                _rewrite_trace_lines_locked(
                    traces_path,
                    safe_run_id,
                    lines,
                    self.max_trace_file_size_bytes,
                )

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
        safe_run_id = _trace_file_run_id(run_id)
        with _trace_run_lock(Path(self.traces_dir), safe_run_id, exclusive=False):
            return self._read_raw_trace_events_unlocked(safe_run_id)

    def _read_raw_trace_events_unlocked(self, run_id: str) -> List[Dict]:
        events = []
        safe_run_id = _trace_file_run_id(run_id)
        trace_paths = _ordered_trace_paths(Path(self.traces_dir), safe_run_id)
        for trace_file in trace_paths:
            try:
                with trace_file.open("r", encoding="utf-8") as source:
                    for line in source:
                        if line.strip():
                            events.append(json.loads(line))
            except (OSError, UnicodeError, json.JSONDecodeError) as e:
                logger.error(f"Ошибка чтения raw-трассы {safe_run_id}: {e}")
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
_telemetry_manager_lock = threading.Lock()

def get_telemetry_manager(traces_dir: str = "logs/traces",
                         service_name: str = "multiagent-system",
                         enabled: bool = True,
                         max_trace_file_size_mb: Optional[float] = None
                         ) -> SmolagentsTelemetryManager:
    """
    Получить глобальный экземпляр менеджера телеметрии

    Args:
        traces_dir: Директория для сохранения трасс
        service_name: Имя сервиса
        enabled: Включена ли телеметрия
        max_trace_file_size_mb: Явный максимальный размер trace-файла

    Returns:
        Экземпляр SmolagentsTelemetryManager
    """
    global _telemetry_manager

    explicit_max_bytes = (
        _max_trace_file_size_mb_to_bytes(max_trace_file_size_mb)
        if max_trace_file_size_mb is not None
        else None
    )
    with _telemetry_manager_lock:
        if _telemetry_manager is None:
            _telemetry_manager = SmolagentsTelemetryManager(
                traces_dir=traces_dir,
                service_name=service_name,
                enabled=enabled,
                max_trace_file_size_bytes=explicit_max_bytes,
            )
        elif explicit_max_bytes is not None:
            _telemetry_manager.max_trace_file_size_bytes = explicit_max_bytes
            if _telemetry_manager.exporter is not None:
                _telemetry_manager.exporter.max_trace_file_size_bytes = (
                    explicit_max_bytes
                )

    return _telemetry_manager

def configure_telemetry(
    enabled: bool = True,
    traces_dir: str = "logs/traces",
    max_trace_file_size_mb: Optional[float] = None,
):
    """
    Настроить телеметрию smolagents
    
    Args:
        enabled: Включить/отключить телеметрию
        traces_dir: Директория для трасс
        max_trace_file_size_mb: Явный максимальный размер trace-файла
    """
    manager = get_telemetry_manager(
        traces_dir=traces_dir,
        enabled=enabled,
        max_trace_file_size_mb=max_trace_file_size_mb,
    )
    
    if enabled and not manager.is_enabled():
        manager.enable()
    elif not enabled and manager.is_enabled():
        manager.disable()
        
    return manager
