"""Журнал прогона `.studio/logs/<run_id>.log`: запись на время прогона и чтение хвоста."""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from .errors import StudioError

logger = logging.getLogger(__name__)

LOGS_DIRNAME = "logs"
MAX_LOG_BYTES = 10 * 1024 * 1024
KEEP_LOGS = 30
MAX_TAIL_LINES = 2000
MAX_TAIL_BYTES = 512 * 1024
TRUNCATED_LINE = "журнал усечён: достигнут предел размера"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_ENTRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_FALLBACK_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|secret|password|passwd|token|authorization|cookie)\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|(?:Bearer\s+)?\S+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")


def check_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id) or ".." in run_id:
        raise StudioError("invalid_argument", "Недопустимый идентификатор прогона")
    return run_id


def redact(text: str) -> str:
    """Сначала свой regex (он знает про «Bearer <токен>»), затем общая функция доставки результатов."""
    text = _FALLBACK_SECRET_RE.sub(lambda m: m.group(1) + "***", text)
    text = _BEARER_RE.sub(r"\1 ***", text)
    try:
        from workflow.result_delivery import _redact_text  # type: ignore

        return _redact_text(text)
    except Exception:  # noqa: BLE001 - запасной вариант не должен падать
        return text


def logs_dir(root: Path) -> Path:
    return Path(root) / ".studio" / LOGS_DIRNAME


def log_path(root: Path, run_id: str) -> Path:
    return logs_dir(root) / f"{check_run_id(run_id)}.log"


class _Handler(logging.Handler):
    def __init__(self, path: Path, limit: int) -> None:
        super().__init__(level=logging.INFO)
        self.path = path
        self.limit = limit
        self.size = path.stat().st_size if path.exists() else 0
        self.truncated = False
        self._io_lock = threading.Lock()

    def write_text(self, level: str, text: str) -> None:
        stamp = datetime.now().isoformat(timespec="seconds")
        body = redact(text).replace("\r", "")
        lines = body.split("\n")
        block = f"{stamp} {level} {lines[0]}\n" + "".join(f"  {line}\n" for line in lines[1:])
        data = block.encode("utf-8", "replace")
        with self._io_lock:
            if self.truncated:
                return
            if self.size + len(data) > self.limit:
                data = f"{stamp} WARNING {TRUNCATED_LINE}\n".encode("utf-8")
                self.truncated = True
            try:
                with open(self.path, "ab") as handle:
                    handle.write(data)
                self.size += len(data)
            except OSError:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.write_text(record.levelname, record.getMessage())
            if record.exc_info:
                self.write_text(record.levelname, self.formatException(record.exc_info))
        except Exception:  # noqa: BLE001
            self.handleError(record)


class RunLog:
    """Обработчик на корневом логгере на время одного прогона (один воркер - один прогон)."""

    def __init__(self, root: Path, run_id: str, *, limit: int = MAX_LOG_BYTES, keep: int = KEEP_LOGS) -> None:
        path = log_path(root, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        prune_logs(root, keep=keep, protect=path.name)
        self.handler = _Handler(path, limit)
        logging.getLogger().addHandler(self.handler)

    def event(self, text: str, level: str = "INFO") -> None:
        self.handler.write_text(level, text)

    def close(self) -> None:
        logging.getLogger().removeHandler(self.handler)


def prune_logs(root: Path, keep: int = KEEP_LOGS, protect: str | None = None) -> None:
    """Оставить ~keep новых журналов; текущий не трогаем."""
    folder = logs_dir(root)
    if not folder.is_dir():
        return
    files = sorted((p for p in folder.glob("*.log") if p.name != protect),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[max(keep - 1, 0):]:
        try:
            old.unlink()
        except OSError:
            continue


def tail(root: Path, run_id: str, lines: int = 500, after_offset: int | None = None) -> dict:
    """{lines, offset, truncated}: до 2000 записей / 512 КБ; продолжение строк клеится к записи."""
    path = log_path(root, run_id)
    if not path.is_file():
        raise StudioError("not_found", "Журнал прогона не найден")
    lines = max(1, min(int(lines), MAX_TAIL_LINES))
    size = path.stat().st_size
    start = 0 if after_offset is None else max(0, min(int(after_offset), size))
    truncated = False
    if size - start > MAX_TAIL_BYTES:
        start = size - MAX_TAIL_BYTES
        truncated = True
    with open(path, "rb") as handle:
        handle.seek(start)
        raw = handle.read(MAX_TAIL_BYTES)
    text = raw.decode("utf-8", "replace")
    entries: list[str] = []
    for line in (text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")) if text else []:
        if _ENTRY_RE.match(line) or not entries:
            entries.append(line)
        else:
            entries[-1] += "\n" + line.lstrip(" ")
    if truncated and entries and not _ENTRY_RE.match(entries[0]):
        entries = entries[1:]  # обрезанная с начала запись
    if len(entries) > lines:
        entries = entries[-lines:]
        truncated = True
    return {"lines": entries, "offset": start + len(raw), "truncated": truncated}
