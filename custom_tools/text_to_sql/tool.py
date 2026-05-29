"""Registry and history managers for Text-to-SQL (FastAPI/backend use).

Glue-модуль: держит process-global реестры job/connection и thread-safe
``SQLHistoryManager`` поверх JSONL-файла. Не содержит бизнес-логики
text-to-sql (модели/промпты/SQL-генерация живут в pipeline/конфигах) —
только storage-слой и in-process синхронизация.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

from .models import ConnectionRegistryEntry, JobRegistryEntry

logger = logging.getLogger(__name__)


# Process-global registries (singleton-like, no Streamlit)
_job_registry: Dict[str, JobRegistryEntry] = {}
_connection_registry: Dict[str, ConnectionRegistryEntry] = {}


def get_job_registry() -> Dict[str, JobRegistryEntry]:
    """Return the global job registry."""
    return _job_registry


def get_connection_registry() -> Dict[str, ConnectionRegistryEntry]:
    """Return the global connection registry."""
    return _connection_registry


# === EPIC 7.21: thread-safe SQLHistoryManager ===
#
# Локи делим на per-path в пределах процесса: разные истории независимы,
# но 2 instance, указывающие на один файл, должны разделять mutex. Это
# гарантирует, что concurrent append/read/clear не повредят JSONL и
# не приведут к partial-line чтениям.
#
# ВНИМАНИЕ: межпроцессную синхронизацию (например, backend + Streamlit
# на одну и ту же `sql_history.jsonl`) этот лок НЕ обеспечивает. Если
# понадобится, переходить на fcntl/portalocker — но в рамках 7.21
# scope только in-process safety.
#
# Рост _HISTORY_LOCKS: словарь накапливает по одному Lock на уникальный resolved-путь.
# В типичном процессе используется 1-2 файла истории, поэтому утечка не критична.
# Если понадобится сотни путей — рассмотреть weakref.WeakValueDictionary или явный LRU.
_HISTORY_LOCKS: Dict[Path, threading.Lock] = {}
_HISTORY_LOCKS_LOCK = threading.Lock()


def _get_history_lock(path: Path) -> threading.Lock:
    """Возвращает (или создаёт) per-path lock."""
    resolved = path.resolve()
    with _HISTORY_LOCKS_LOCK:
        lock = _HISTORY_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _HISTORY_LOCKS[resolved] = lock
        return lock


class SQLHistoryManager:
    """Manages persistent SQL generation history (JSONL file).

    Thread-safe: append/read защищены per-path `threading.Lock`
    (EPIC 7.21). Append выполняется через append-mode + ``fsync`` под
    mutex: concurrent reader (под тем же lock) либо видит полный набор
    предыдущих строк, либо полный набор после новой строки, но не partial.
    Старый вариант с rewrite-all+os.replace был O(N²) по числу записей и
    деградировал на больших историях.
    """

    def __init__(self, history_file: Path):
        self._path = Path(history_file)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _get_history_lock(self._path)

    def get_history(self, max_entries: int = 100) -> List[Dict[str, Any]]:
        """Load history from disk. Returns list of dicts with id, timestamp,
        natural_query, generated_sql, status, connection_id, execution_result.
        """
        with self._lock:
            if not self._path.exists():
                return []
            entries: List[Dict[str, Any]] = []
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            ts = rec.get("timestamp")
                            if isinstance(ts, str):
                                rec["timestamp"] = ts
                            elif hasattr(ts, "isoformat"):
                                rec["timestamp"] = ts.isoformat()
                            else:
                                rec["timestamp"] = str(ts) if ts else ""
                            entries.append(rec)
                        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
                            logger.warning("get_history: пропущена битая строка в %s: %s", self._path, exc)
                            continue
                return entries[-max_entries:]
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("get_history: не удалось прочитать %s: %s", self._path, exc)
                return []

    def append(self, entry: Dict[str, Any]) -> None:
        """Append a single history entry to the JSONL file.

        Реализация: open("ab") + fsync под per-path mutex. Так как
        get_history тоже берёт этот mutex, partial-line ситуации в пределах
        процесса исключены. Запись одной строки в POSIX-режиме append
        атомарна на уровне ядра (write до PIPE_BUF), но fsync гарантирует
        durability на случай краха.
        """
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "ab") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
