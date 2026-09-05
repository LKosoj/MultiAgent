"""
EPIC 8.3: SchemaMemoryManager — SQLite-side оркестратор schema-memory.

Вынесен из единого ``schema_memory.py`` (868 строк) после разбиения по
responsibility. Этот модуль:
  * Содержит ``SchemaMemoryManager`` — оркестратор индексации/удаления
    схем в тактической памяти (SQLite + Chroma).
  * Использует Chroma-хелперы из ``schema_memory_chroma`` (``_resolve_chroma_metric``,
    ``_distance_to_similarity``) — но не дублирует их.
  * Хранит ``_schema_write_lock`` — process-level lock на запись схем.

КРИТИЧНО: при удалении старых записей сохраняется порядок:
    ``_schema_write_lock`` → SQLite BEGIN IMMEDIATE → SQLite commit →
    Chroma cleanup (managed soft-fail).
SQLite — source of truth, Chroma — eventually consistent.
"""
from __future__ import annotations

import os
import sys
import json
import time
import math
import hashlib
import logging
import sqlite3
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .schema_memory_chroma import _resolve_chroma_metric, _distance_to_similarity
from .schema_metadata import is_pk, is_fk
from .schema_namespace import SchemaNamespace, SchemaScope
from .successful_sql_memory import canonical_schema_table_id
from .utils import (
    dsn_to_sanitized_name,
    get_schema_version,
    get_table_columns,
    get_table_description,
)

logger = logging.getLogger(__name__)

_VECTOR_READY = "ready"
_VECTOR_NOT_READY = frozenset({"disabled", "stale", "unavailable", "rebuilding"})


def _redact_schema_memory_value(value: Any) -> Any:
    try:
        from custom_tools.text_to_sql.redaction import _redact_payload, redact_pii_in_payload

        if isinstance(value, BaseException):
            value = str(value)
        return redact_pii_in_payload(_redact_payload(value))
    except Exception:
        return "<redacted>"


_PERSISTED_DATA_PROBE_KINDS = frozenset(
    {"profile_column", "sample_rows", "search_value", "distinct_values"}
)


class SemanticFact(BaseModel):
    """A durable, schema-scoped semantic fact."""

    model_config = ConfigDict(frozen=True)

    subject: Literal["table", "column"]
    table_fqn: str
    column: str | None = None
    fact_kind: Literal["description", "example", "glossary_term"]
    value: str | int | float | bool | None
    source: Literal["file", "typed_probe", "dsn_glossary"]
    status: Literal["approved", "candidate", "rejected"]
    kind: Literal["dimension", "measure", "filter_value", "entity"] | None = None
    note: str | None = None

    @field_validator("table_fqn")
    @classmethod
    def _require_table_fqn(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("table_fqn must be non-empty")
        return value

    @field_validator("column")
    @classmethod
    def _normalize_column(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("column must be non-empty when provided")
        return value

    @field_validator("value")
    @classmethod
    def _require_finite_float(cls, value: str | int | float | bool | None) -> str | int | float | bool | None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("semantic fact values must be finite")
        return value

    @model_validator(mode="after")
    def _validate_subject(self) -> "SemanticFact":
        if self.subject == "column" and self.column is None:
            raise ValueError("column facts require column")
        if self.subject == "table" and self.column is not None:
            raise ValueError("table facts must not include column")
        if self.fact_kind == "description" and (
            not isinstance(self.value, str) or not self.value.strip()
        ):
            raise ValueError("description facts require a non-empty text value")
        if self.fact_kind != "glossary_term" and (
            self.kind is not None or self.note is not None
        ):
            raise ValueError("kind/note are only valid for glossary_term facts")
        return self


# W2-2.2: ``kind``/``note`` were added later, exclusively for
# ``dsn_glossary`` facts. Every ``file``/``typed_probe`` fact ever persisted
# always has both at their ``None`` default, so including them (even as
# ``null``) in a fact's canonical dump would change the JSON shape of every
# already-stored fact and break its ``cache_key``/identity match. Facts that
# never populate them keep the exact pre-existing dump; only glossary facts
# that set them see the new keys.
_OPTIONAL_GLOSSARY_FACT_FIELDS = ("kind", "note")


def _semantic_fact_dump(fact: SemanticFact) -> dict[str, Any]:
    """Canonical dump of one fact used for hashing/identity/storage."""

    exclude = {
        name for name in _OPTIONAL_GLOSSARY_FACT_FIELDS if getattr(fact, name) is None
    }
    return fact.model_dump(mode="json", exclude=exclude)


def _semantic_fact_key(fact: SemanticFact) -> str:
    from .adaptive.serialization import canonical_digest

    return "text2sql-semantic-fact-v1-" + canonical_digest(
        _semantic_fact_dump(fact)
    ).removeprefix("sha256:")


def _semantic_fact_source_digest(facts: Sequence[SemanticFact]) -> str:
    from .adaptive.serialization import canonical_digest

    return canonical_digest(
        sorted(
            (_semantic_fact_dump(fact) for fact in facts),
            key=lambda fact: json.dumps(fact, ensure_ascii=False, sort_keys=True),
        )
    )


def _semantic_file_snapshot_key(source_digest: str, *, source: str = "file") -> str:
    """Return the snapshot marker key for one fact-set source.

    ``source="file"`` (the default) reproduces the original, pre-existing
    key byte-for-byte so already-persisted file snapshots keep matching.
    ``source="dsn_glossary"`` gives the DSN glossary its own, non-colliding
    snapshot namespace (EPIC W2-2.2).
    """
    if source not in ("file", "dsn_glossary"):
        raise ValueError("source must be 'file' or 'dsn_glossary'")
    return f"text2sql-semantic-{source}-snapshot-v1-" + source_digest.removeprefix(
        "sha256:"
    )


def _semantic_fact_status_override_key(fact_key: str) -> str:
    """Cache key for a status-override record targeting ``fact_key``."""
    return f"text2sql-semantic-fact-status-v1-{fact_key}"


@dataclass(frozen=True)
class _FactCandidate:
    """One semantic fact record that survived source-snapshot replacement.

    Shared result type between ``_scan_semantic_fact_candidates`` and its two
    callers (``find_approved_semantic_facts``, ``list_semantic_facts``).
    """

    fact: SemanticFact
    key: str
    step: int
    source_digest: str


def _probe_fact_key(fact: dict[str, Any]) -> str:
    from .adaptive.serialization import canonical_digest

    return "text2sql-schema-probe-fact-v1-" + canonical_digest(fact).removeprefix(
        "sha256:"
    )


def _verified_probe_facts(
    namespace: SchemaNamespace,
    state: object,
) -> list[dict[str, Any]]:
    """Normalize only evidence whose exact producer action is still available."""
    from .adaptive.models import ResearchState
    from .adaptive.provenance import parse_probe_observation, read_evidence_provenance

    if not isinstance(state, ResearchState):
        raise TypeError("state must be ResearchState")
    namespace_version = f"sha256:{namespace.version_key}"
    if state.schema_namespace_version != namespace_version:
        return []
    facts: list[dict[str, Any]] = []
    for evidence in state.evidence:
        try:
            provenance = read_evidence_provenance(evidence)
            observation = parse_probe_observation(evidence.observation)
        except ValueError:
            continue
        if (
            provenance is None
            or observation is None
            or observation.storage != "inline"
            or observation.truncated
            or provenance.probe_kind.value not in _PERSISTED_DATA_PROBE_KINDS
            or evidence.schema_namespace_version != namespace_version
        ):
            continue
        actions = [
            action
            for action in state.action_history
            if action.action_digest == evidence.action_digest
        ]
        if len(actions) != 1:
            continue
        action = actions[0]
        if (
            action.kind is not provenance.probe_kind
            or action.target != evidence.target
            or action.target != provenance.target
            or action.expected_revision + 1 != evidence.revision
        ):
            continue
        facts.append(
            {
                "probe_kind": action.kind.value,
                "target": action.target.model_dump(mode="json", by_alias=True),
                "parameters": [list(item) for item in action.parameters],
                "payload": observation.payload,
            }
        )
    return facts


def _approved_semantic_facts_from_probe_fact(
    fact: dict[str, Any],
) -> tuple[SemanticFact, ...]:
    """Promote direct, complete column-value probes into approved examples."""
    if fact.get("probe_kind") not in {"search_value", "distinct_values"}:
        return ()
    target = fact.get("target")
    payload = fact.get("payload")
    if not isinstance(target, dict) or not isinstance(payload, dict):
        return ()
    table_ref = target.get("table")
    column = target.get("column")
    values = payload.get("values")
    if not isinstance(table_ref, dict) or not isinstance(column, str):
        return ()
    table_parts = [
        table_ref.get(name)
        for name in ("namespace", "schema", "table")
        if isinstance(table_ref.get(name), str) and table_ref[name]
    ]
    if not table_parts or not isinstance(values, list):
        return ()
    return tuple(
        SemanticFact(
            subject="column",
            table_fqn=".".join(table_parts),
            column=column,
            fact_kind="example",
            value=value,
            source="typed_probe",
            status="approved",
        )
        for value in values
        if value is None
        or isinstance(value, (str, int, bool))
        or (isinstance(value, float) and math.isfinite(value))
    )


# === W2-T1: Кастомные исключения для fail-fast индексации схемы ===
# AGENTS.md запрещает silent fallback. Раньше broad ``except Exception →
# return 0`` в ``index_schema_in_memory`` маскировал реальные сбои:
# вызывающий код не мог отличить "схема пуста" от "БД упала". Эти
# исключения вводят явный сигнал об ошибке индексации с контекстом.
class SchemaIndexingError(RuntimeError):
    """Не удалось проиндексировать схему в тактической памяти.

    Используется как общий тип для ошибок ``index_schema_in_memory``:
    под этим зонтиком оба варианта — невосстановимый сбой и частичный
    успех (см. ``failed_tables``). Caller'ы могут ловить именно его и
    решать retry/skip явно, без перехвата широкого ``Exception``.
    """

    def __init__(
        self,
        message: str,
        *,
        failed_tables: Optional[List[str]] = None,
        indexed_count: int = 0,
    ) -> None:
        super().__init__(message)
        # Список таблиц, на которых упала индексация (в порядке встречи).
        self.failed_tables: List[str] = list(failed_tables or [])
        # Сколько таблиц успели сохранить ДО ошибки — для частичного успеха.
        self.indexed_count: int = indexed_count


class SchemaIndexingMemoryUnavailable(SchemaIndexingError):
    """``memory.tools.save_memory`` недоступен (модуль не импортирован
    или хук возвращает None). Это конфигурационная проблема, не
    транзиент — caller обычно должен делать fail-fast наружу, а не
    тихо «нет данных»."""


# Process-level lock не спасает от межпроцессных гонок: при многопроцессном
# FastAPI/uvicorn два worker'а могут одновременно деактивировать схему.
# File-based lock через fcntl.flock — Unix-only; на Windows fallback на
# threading.Lock (это НЕ silent fallback на ослабленную семантику для Unix —
# Windows-сценарий тут редкий, а нативного аналога flock у него нет).
if sys.platform != "win32":
    import fcntl

    class _FileLock:
        """Inter-process exclusive lock через ``fcntl.flock``."""

        def __init__(self, path: str) -> None:
            self.path = path
            self._fd: Optional[int] = None

        def acquire(self, timeout: float = 30.0) -> None:
            """Захватывает exclusive flock с явным timeout.

            Бесконечная блокировка опасна: если соседний процесс/воркер
            подвис под write-локом, мы зависнем навсегда. timeout по
            умолчанию 30s — достаточно длинный, чтобы покрыть
            нормальный pipeline-write, но не «навсегда».
            """
            self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
            start = time.monotonic()
            try:
                while True:
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return
                    except BlockingIOError:
                        if time.monotonic() - start > timeout:
                            # null-first: обнуляем self._fd ДО close. Если
                            # os.close сам бросит OSError (EBADF/EINTR), внешний
                            # except увидит None и не закроет fd повторно —
                            # двойной close мог бы закрыть чужой дескриптор,
                            # переоткрытый другим потоком (TOCTOU).
                            fd = self._fd
                            self._fd = None
                            try:
                                os.close(fd)
                            except OSError as close_exc:
                                # os.close может бросить EBADF/EINTR. Логируем, но
                                # НЕ даём ему подменить первопричину: иначе внешний
                                # except BaseException ре-пробросит OSError вместо
                                # ожидаемого caller'ом TimeoutError.
                                logger.warning(
                                    "os.close при timeout flock дал ошибку: %r",
                                    close_exc,
                                )
                            raise TimeoutError(
                                f"Could not acquire file lock within {timeout}s"
                            )
                        time.sleep(0.1)
            except BaseException:
                # Закрываем fd при любом исключении, не только BlockingIOError,
                # чтобы не утекал дескриптор (KeyboardInterrupt, OSError и др.).
                # null-first по той же причине, что и в timeout-ветке.
                fd = self._fd
                if fd is not None:
                    self._fd = None
                    try:
                        os.close(fd)
                    except OSError as close_exc:
                        # Симметрично timeout-ветке: ошибка os.close не должна
                        # подменять оригинальное исключение (raise ниже без
                        # аргумента ре-пробрасывает именно его).
                        logger.warning(
                            "os.close в BaseException-cleanup дал ошибку: %r",
                            close_exc,
                        )
                raise

        def release(self) -> None:
            if self._fd is not None:
                fd = self._fd
                self._fd = None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

        def __enter__(self) -> "_FileLock":
            self.acquire()
            return self

        def __exit__(self, *exc_info: Any) -> None:
            self.release()
else:
    class _FileLock:  # type: ignore[no-redef]
        """Windows-fallback: process-local threading.Lock (нет flock)."""

        def __init__(self, path: str) -> None:
            self.path = path
            self._lock = threading.Lock()

        def __enter__(self) -> "_FileLock":
            self._lock.acquire()
            return self

        def __exit__(self, *exc_info: Any) -> None:
            self._lock.release()


class SchemaMemoryManager:
    """Менеджер памяти для схем базы данных."""

    # Process-level lock сохранён ради backward-compat (внешний код мог брать
    # ``SchemaMemoryManager._schema_write_lock``). Реальная защита теперь
    # через file-based lock (см. ``__init__._write_lock_path``), который
    # работает И между потоками, И между процессами (multi-worker FastAPI).
    _schema_write_lock = threading.Lock()

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.last_search_status = "not_started"
        self.last_search_error = None
        # Это состояние относится только к дополнительному Chroma-индексу.
        # Наличие SQLite-строк схемы проверяется отдельно и остаётся
        # авторитетным результатом для pipeline.
        self.last_vector_readiness = "unavailable"
        self.last_vector_reason: Optional[str] = "not_initialized"
        # File-based lock: путь рядом с repo_root, чтобы все воркеры одного
        # деплоя видели один и тот же файл-маркер. Каталог гарантированно
        # существует (repo_root).
        self._write_lock_path: str = str(
            Path(self.repo_root) / ".schema_write.lock"
        )
        # На read-only ФС (Docker с RO-mount, тесты) даже touch файла
        # упадёт. По правилам проекта (AGENTS.md) silent fallback на
        # ослабленную семантику запрещён — по умолчанию fail-fast.
        # Явный opt-in: SCHEMA_ALLOW_THREAD_ONLY_LOCK=1 (например, для
        # single-process тестов и read-only FS dev-окружений).
        self._use_file_lock: bool = True
        if sys.platform == "win32":
            # Windows: нет нативного flock; thread-only — единственный
            # доступный режим. Требуем явный opt-in так же, как для
            # read-only FS на Unix.
            if os.getenv("SCHEMA_ALLOW_THREAD_ONLY_LOCK", "0") != "1":
                raise RuntimeError(
                    "Inter-process file lock (fcntl.flock) is unavailable on Windows. "
                    "Set SCHEMA_ALLOW_THREAD_ONLY_LOCK=1 to explicitly opt-in to "
                    "thread-only locking (single-process only)."
                )
            self._use_file_lock = False
        else:
            try:
                Path(self._write_lock_path).touch(exist_ok=True)
            except (PermissionError, OSError) as e:
                if os.getenv("SCHEMA_ALLOW_THREAD_ONLY_LOCK", "0") != "1":
                    raise RuntimeError(
                        f"Schema write-lock path not writable: {e}. "
                        f"Inter-process protection (fcntl.flock) requires a writable "
                        f"file at {self._write_lock_path!r}. Set "
                        f"SCHEMA_ALLOW_THREAD_ONLY_LOCK=1 to explicitly opt-in to "
                        f"thread-only locking (single-process only)."
                    ) from e
                logger.warning(
                    "File lock path not writable: %s; SCHEMA_ALLOW_THREAD_ONLY_LOCK=1 "
                    "in env — falling back to thread-only lock (single-process only)",
                    e,
                )
                self._use_file_lock = False
        # Lazy integrity-check: один раз на инстанс, ВНЕ write_lock
        # (см. _ensure_sqlite_integrity_checked). PRAGMA integrity_check
        # на больших БД занимает минуты, держать write-lock на это время
        # нельзя — это DoS для всех writers.
        self._sqlite_integrity_checked: bool = False
        self._integrity_check_lock = threading.Lock()
        # W5-T3: per-thread depth-счётчик для re-entrant write-lock.
        # `ensure_schema_indexed_in_memory` берёт write_lock сверху, потом
        # вызывает `remove_old_schema_records`, который раньше брал лок
        # повторно (deadlock на fcntl.flock с новым fd). Считаем глубину:
        # если > 0, внутренний вызов не делает повторный acquire.
        self._lock_depth = threading.local()

    def _write_lock_cm(self) -> Any:
        """Context manager для write-lock с re-entrant поддержкой.

        W5-T3: позволяет верхнему уровню (`ensure_schema_indexed_in_memory`)
        захватить лок один раз и вызывать внутренние операции
        (`remove_old_schema_records`, `index_schema_in_memory`), которые
        раньше брали лок самостоятельно.

        Реализация через per-thread depth-счётчик: первый acquire берёт
        реальный (file-based или thread-based) лок, повторные acquire'ы
        в том же потоке — no-op. release вызывается зеркально.

        Это решает гонку «check-then-act» в `ensure_schema_indexed_in_memory`:
        раньше is_schema_indexed → remove → index выполнялись без общего
        лока, два worker'а могли проскочить check одновременно и оба
        запустить переиндексацию.
        """
        manager = self

        class _ReentrantCM:
            def __enter__(self_inner):
                depth = getattr(manager._lock_depth, "value", 0)
                if depth == 0:
                    inner = (
                        _FileLock(manager._write_lock_path)
                        if manager._use_file_lock
                        else manager._schema_write_lock
                    )
                    inner.__enter__()
                    self_inner._inner = inner
                else:
                    self_inner._inner = None
                manager._lock_depth.value = depth + 1
                return self_inner

            def __exit__(self_inner, *exc_info: Any) -> None:
                depth = getattr(manager._lock_depth, "value", 1)
                manager._lock_depth.value = depth - 1
                if self_inner._inner is not None:
                    self_inner._inner.__exit__(*exc_info)

        return _ReentrantCM()

    def _ensure_sqlite_integrity_checked(self, memory_manager: Any) -> None:
        """Однократный PRAGMA integrity_check на инстанс, БЕЗ write_lock.

        Запускается на ОТДЕЛЬНОМ коннекте (memory_manager.get_sqlite_connection
        возвращает new per-call connection), чтобы не держать write-lock
        на время проверки — на больших БД integrity_check занимает минуты
        и блокировал бы всех writers.

        Если БД повреждена — поднимаем RuntimeError (fail-fast вместо
        silent corruption данных). ``sqlite3.DatabaseError`` тоже переводим
        в RuntimeError с явной причиной. Параллельные вызовы защищены
        отдельным ``_integrity_check_lock`` (но НЕ write_lock).
        """
        if self._sqlite_integrity_checked:
            return
        with self._integrity_check_lock:
            if self._sqlite_integrity_checked:
                return
            conn = memory_manager.get_sqlite_connection()
            try:
                try:
                    cursor = conn.execute("PRAGMA integrity_check")
                    result = cursor.fetchone()
                except sqlite3.DatabaseError as e:
                    raise RuntimeError(f"SQLite database corrupt: {e}") from e
                if result and result[0] != "ok":
                    raise RuntimeError(f"SQLite integrity check failed: {result[0]}")
                self._sqlite_integrity_checked = True
            finally:
                conn.close()

    def _set_search_status(self, status: str, error: Optional[str] = None) -> None:
        self.last_search_status = status
        self.last_search_error = (
            str(_redact_schema_memory_value(error)) if error is not None else None
        )

    @staticmethod
    def _sqlite_lexical_schema_tables(
        entities: List[str],
        namespace: SchemaNamespace,
    ) -> List[str]:
        terms = tuple(
            dict.fromkeys(
                entity.strip().casefold()
                for entity in entities
                if isinstance(entity, str) and len(entity.strip()) > 2
            )
        )[:5]
        if not terms:
            return []
        from memory.tools import get_memory, memory_requester_context

        with memory_requester_context("Schema-RAG-Agent"):
            records = get_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_table",
                include_historical=False,
                requesting_agent="Schema-RAG-Agent",
            )
        ranked_tables: list[tuple[int, str]] = []
        seen_tables: set[str] = set()
        for record in records:
            data = record.get("data") if isinstance(record, dict) else None
            if not isinstance(data, dict):
                continue
            table_fqn = data.get("table_fqn")
            if not isinstance(table_fqn, str) or not table_fqn or table_fqn in seen_tables:
                continue
            table_info = data.get("table_info")
            columns = table_info.get("columns") if isinstance(table_info, dict) else ()
            searchable = [
                data.get("table_fqn"),
                data.get("table_name"),
                data.get("description"),
                table_info.get("table_name") if isinstance(table_info, dict) else None,
                table_info.get("description") if isinstance(table_info, dict) else None,
            ]
            if isinstance(columns, list):
                for column in columns:
                    if isinstance(column, dict):
                        searchable.extend((column.get("name"), column.get("description")))
            text = " ".join(value for value in searchable if isinstance(value, str)).casefold()
            matched_terms = sum(term in text for term in terms)
            if matched_terms:
                ranked_tables.append((matched_terms, table_fqn))
                seen_tables.add(table_fqn)
        ranked_tables.sort(key=lambda item: (-item[0], item[1]))
        return [table_fqn for _matched_terms, table_fqn in ranked_tables]

    def restore_descriptions_from_memory(
        self,
        namespace: SchemaNamespace,
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> bool:
        """Fill missing descriptions from the exact persisted schema namespace."""
        from memory.tools import get_memory, memory_requester_context

        with memory_requester_context("Schema-RAG-Agent"):
            records = get_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_table",
                include_historical=False,
                requesting_agent="Schema-RAG-Agent",
            )

        changed = False
        for record in records:
            data = record.get("data") if isinstance(record, dict) else None
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != namespace.version_key
            ):
                continue
            table_fqn = data.get("table_fqn")
            table_info = data.get("table_info")
            if (
                not isinstance(table_fqn, str)
                or table_fqn not in db_schema
                or not isinstance(table_info, dict)
            ):
                continue
            table_schema = db_schema[table_fqn]
            table_description = table_info.get("description")
            if (
                not get_table_description(table_schema)
                and isinstance(table_description, str)
                and table_description.strip()
            ):
                table_schema["description"] = table_description.strip()
                changed = True

            memory_columns = table_info.get("columns")
            if not isinstance(memory_columns, list):
                continue
            live_columns = get_table_columns(table_schema)
            for memory_column in memory_columns:
                if not isinstance(memory_column, dict):
                    continue
                name = memory_column.get("name")
                description = memory_column.get("description")
                live_column = live_columns.get(name) if isinstance(name, str) else None
                if (
                    isinstance(live_column, dict)
                    and not str(live_column.get("description", "")).strip()
                    and isinstance(description, str)
                    and description.strip()
                ):
                    live_column["description"] = description.strip()
                    changed = True
        return changed

    def _observe_vector_readiness(self, memory_manager: Any) -> str:
        """Возвращает явное состояние Chroma, не подменяя им SQLite-готовность."""
        try:
            handler = getattr(memory_manager, "db_handler", None)
            raw_state = getattr(handler, "vector_readiness", None)
            state = getattr(raw_state, "value", raw_state)
        except Exception:
            self.last_vector_readiness = "unavailable"
            self.last_vector_reason = "vector_readiness_probe_failed"
            return self.last_vector_readiness

        if not isinstance(state, str) or state not in _VECTOR_NOT_READY | {_VECTOR_READY}:
            try:
                metadata_mismatch = getattr(handler, "embedding_metadata_mismatch", False)
            except Exception:
                self.last_vector_readiness = "unavailable"
                self.last_vector_reason = "vector_readiness_probe_failed"
                return self.last_vector_readiness
            if metadata_mismatch:
                state = "stale"
            else:
                try:
                    get_collection = getattr(memory_manager, "get_tactical_collection", None)
                    collection = get_collection() if callable(get_collection) else None
                except Exception:
                    state = "stale" if handler is not None else "unavailable"
                    self.last_vector_readiness = state
                    self.last_vector_reason = "tactical_collection_probe_failed"
                    return state
                state = _VECTOR_READY if collection is not None else "unavailable"
        self.last_vector_readiness = state
        try:
            reason = getattr(handler, "vector_readiness_reason", None)
        except Exception:
            reason = "vector_readiness_probe_failed"
        self.last_vector_reason = str(reason) if reason else None
        return state

    @staticmethod
    def _mark_vector_stale(memory_manager: Any, reason: str) -> None:
        """Не даёт ошибке Chroma выглядеть как готовому векторному индексу."""
        try:
            handler = getattr(memory_manager, "db_handler", None)
            setter = getattr(handler, "set_vector_readiness", None)
            if callable(setter):
                setter("stale", reason)
            elif handler is not None:
                handler.vector_readiness = "stale"
                handler.vector_readiness_reason = reason
        except Exception:
            logger.warning("Could not mark the vector index stale")

    def ensure_schema_indexed_in_memory(
        self,
        dsn: str | SchemaNamespace,
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        session_id: Optional[str] = None,
    ) -> bool:
        """Обеспечивает индексацию схемы в тактической памяти.

        Fail-fast: ошибки memory layer / JSON-парса schema-файла пробрасываются
        наверх (Phase 6-Extended). Раньше любое исключение глушилось в `return
        False`, что давало silent fallback на "схема не проиндексирована".

        Возвращаемое значение чётко различает три случая:

        * ``True`` — индекс актуален или успешно построен.
        * ``False`` — нет таблиц для индексации (пустой ``db_schema`` или
          schema-файл с ``enable: false``). Это штатное "нет данных", НЕ
          ошибка.
        * ``raise`` (W2-T1) — ошибка БД/memory layer: пробрасывается
          :class:`SchemaIndexingError` / :class:`SchemaIndexingMemoryUnavailable`
          с контекстом, чтобы caller мог решить retry/skip явно.
        """
        from memory.tools import save_memory

        if isinstance(dsn, SchemaScope):
            raise TypeError(
                "SchemaScope is unversioned; schema memory requires SchemaNamespace"
            )
        namespace = dsn if isinstance(dsn, SchemaNamespace) else None

        if not save_memory:
            # Раньше тут было ``return False`` — caller не отличал
            # «memory-стек не загружен» от «таблиц нет». Теперь явно
            # сигнализируем конфигурационную проблему наверх.
            identity = (
                f"namespace={namespace.version_key!r}"
                if namespace is not None
                else f"dsn={dsn_to_sanitized_name(dsn)!r}"
            )
            raise SchemaIndexingMemoryUnavailable(
                "memory.tools.save_memory is unavailable; "
                "cannot ensure schema indexed for "
                f"{identity}"
            )

        if namespace is not None:
            session_id = namespace.version_key
        else:
            session_id = session_id or dsn_to_sanitized_name(dsn)
        sqlrag_dir = self.repo_root / "sqlrag"
        filename = (
            f"schema-v1-{namespace.version_key}.json"
            if namespace is not None
            else f"{session_id}.json"
        )
        json_path = sqlrag_dir / filename

        if namespace is not None:
            schema_data = {
                "enable": True,
                "schema_info": db_schema,
                "source": "validated_live_schema",
            }
        elif json_path.exists():
            # Corrupt schema-файл — это критично; раньше json.JSONDecodeError
            # глушился в `return False` и схема оказывалась "не индексирована"
            # без видимой причины. Теперь — fail-fast.
            content = json_path.read_text(encoding="utf-8")
            schema_data = json.loads(content)
            if not schema_data.get("enable", False):
                # Если enable: false, удаляем существующие записи.
                # W5-T3: оборачиваем в write_lock_cm — `remove_old_schema_records`
                # сам берёт лок, но при последовательных вызовах разных enable
                # toggle'ов между процессами без верхнего лока возможна гонка
                # «один читает, другой удалил часть, третий частично вставил».
                # Внутренний acquire — no-op (re-entrant).
                logger.info(f"Schema file {filename} has enable: false, removing existing records")
                with self._write_lock_cm():
                    self.remove_old_schema_records(session_id, filename)
                return False
        else:
            # Файл отсутствует — всегда строим schema_data из переданного
            # db_schema и индексируем в память. Флаг SCHEMA_AUTOSAVE
            # управляет ТОЛЬКО записью JSON на диск (это делает
            # schema_loader._save_schema_to_file), а индексация в
            # тактической памяти должна работать в любом случае.
            schema_data = {
                "enable": True,
                "schema_info": db_schema,
                "source": "introspection",
            }

        # Вычисляем хэш source-of-truth данных схемы.
        # Исключаем служебные поля (enable/source/metadata), чтобы переключение
        # флага enable или смена источника не приводили к лишней переиндексации.
        schema_for_hash = {
            k: v for k, v in schema_data.items()
            if k not in {"enable", "source", "metadata"}
        }
        normalized_content = json.dumps(schema_for_hash, ensure_ascii=False, sort_keys=True)
        # BLAKE2b быстрее MD5 и не считается криптографически сломанным.
        # digest_size=16 → 32 hex-символа, совместимо по длине со старым MD5.
        # Меняем формат хэша — старые записи `file_hash` инвалидируются и
        # переиндексируются при следующем вызове (это OK: индекс
        # самовосстанавливается из источника).
        file_hash = hashlib.blake2b(
            normalized_content.encode("utf-8"), digest_size=16
        ).hexdigest()

        # W5-T3: double-checked locking. Без лока ниже сразу два процесса/
        # worker'а могут проскочить is_schema_indexed (оба видят "нет"),
        # оба вызовут remove + index — это race condition с дублирующими
        # вставками и битым кэшем. Берём write-lock и проверяем повторно.
        # Раньше остальные операции (remove_old_schema_records) брали лок
        # сами; теперь `_write_lock_cm` re-entrant, внутренние acquire'ы
        # становятся no-op.
        # Быстрый путь без лока: если индекс уже актуален — выходим без
        # дорогостоящего захвата flock.
        expected_count = len(db_schema)
        index_identity: str | SchemaNamespace = namespace or session_id
        if self.is_schema_indexed(
            index_identity,
            file_hash,
            expected_count=expected_count,
            expected_table_keys=tuple(db_schema),
        ):
            return True

        with self._write_lock_cm():
            # Double-check внутри лока: пока ждали, другой worker мог
            # уже проиндексировать схему.
            if self.is_schema_indexed(
                index_identity,
                file_hash,
                expected_count=expected_count,
                expected_table_keys=tuple(db_schema),
            ):
                return True

            # Удаляем старые записи для этого файла (внутренний acquire
            # лока — no-op благодаря re-entrance).
            self.remove_old_schema_records(session_id, filename)

            # Индексируем схему по таблицам.
            # W2-T1: ошибки БД/memory layer пробрасываются как
            # ``SchemaIndexingError`` / ``SchemaIndexingMemoryUnavailable`` —
            # сюда возвращается ТОЛЬКО успешный путь. ``indexed_count == 0``
            # значит ровно одно: ``db_schema`` пуст (нет таблиц для
            # индексации) — это штатное "нет данных", caller получит ``False``
            # и сможет отличить его от ``raise``.
            if namespace is None:
                indexed_count = self.index_schema_in_memory(
                    session_id,
                    filename,
                    db_schema,
                    file_hash,
                )
            else:
                indexed_count = self.index_schema_in_memory(
                    session_id,
                    filename,
                    db_schema,
                    file_hash,
                    schema_version=namespace.version_key,
                    semantic_namespace_key=namespace.version_key,
                )
            if namespace is None:
                return bool(indexed_count)
            if indexed_count == 0:
                return False
            if not self.is_schema_indexed(
                namespace,
                file_hash,
                expected_count=expected_count,
                expected_table_keys=tuple(db_schema),
            ):
                raise SchemaIndexingError(
                    "Schema source rows were saved but exact semantic IDs did not reconcile",
                    indexed_count=indexed_count,
                )
            return True

    def is_schema_indexed(
        self,
        session_id: str | SchemaNamespace,
        file_hash: str,
        expected_count: Optional[int] = None,
        expected_table_keys: Optional[tuple[str, ...]] = None,
    ) -> bool:
        """Проверяет, проиндексирована ли схема с данным хэшем.

        Идемпотентный check. `return False` означает "записи с таким хэшем
        нет" — штатное "нет данных", это НЕ silent fallback.
        Ошибки memory layer пробрасываются наверх (fail-fast).

        Args:
            session_id: идентификатор сессии.
            file_hash: хэш файла схемы.
            expected_count: ожидаемое число активных записей с данным хэшем
                (соответствует числу таблиц db_schema). Если передан и > 0,
                метод возвращает True только при active_records_with_hash >= expected_count,
                иначе детектирует частичную индексацию и возвращает False.
                При expected_count=None (по умолчанию) — поведение прежнее: True при > 0.
        """
        if isinstance(session_id, SchemaScope):
            raise TypeError(
                "SchemaScope is unversioned; schema memory requires SchemaNamespace"
            )
        namespace = session_id if isinstance(session_id, SchemaNamespace) else None
        if namespace is not None:
            from memory.index_consistency import (
                reconcile_tactical_namespace,
                source_namespace_semantic_ids,
            )
            from memory.manager import memory_manager

            vector_readiness = self._observe_vector_readiness(memory_manager)
            expected_ids = (
                {
                    canonical_schema_table_id(namespace.version_key, table_fqn)
                    for table_fqn in expected_table_keys
                }
                if expected_table_keys is not None
                else None
            )
            source_ids, failed_ids = source_namespace_semantic_ids(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_table",
                expected_file_hash=file_hash,
            )
            if vector_readiness != _VECTOR_READY:
                expected_ids = source_ids if expected_ids is None else expected_ids
                return (
                    bool(expected_ids)
                    and not failed_ids
                    and expected_ids == source_ids
                    and (
                        expected_count is None
                        or expected_count <= 0
                        or len(expected_ids) == expected_count
                    )
                )
            with self._write_lock_cm():
                reconciliation = reconcile_tactical_namespace(
                    session_id=namespace.version_key,
                    agent_name="Schema-RAG-Agent",
                    cache_kind="schema_table",
                    repair_missing=True,
                )
            expected_ids = (
                set(reconciliation.expected_ids)
                if expected_ids is None
                else expected_ids
            )
            actual_ids = set(reconciliation.indexed_ids)
            sqlite_source_is_complete = (
                bool(expected_ids)
                and not failed_ids
                and expected_ids == source_ids
                and not reconciliation.failed_ids
                and expected_ids == set(reconciliation.expected_ids)
                and (
                    expected_count is None
                    or expected_count <= 0
                    or len(expected_ids) == expected_count
                )
            )
            return (
                sqlite_source_is_complete
                and not reconciliation.missing_ids
                and not reconciliation.unexpected_ids
                and expected_ids == actual_ids
            )

        from memory.tools import get_memory
        try:
            from memory.tools import memory_requester_context
        except ImportError:
            def memory_requester_context(_agent_name: str) -> Any:
                return nullcontext()

        # Получаем ТОЛЬКО активные записи (include_historical=False по умолчанию)
        with memory_requester_context("Schema-RAG-Agent"):
            results = get_memory(
                session_id=session_id,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_table",
                include_historical=False,  # Явно указываем, что нужны только активные записи
                requesting_agent="Schema-RAG-Agent",
            )

        # Проверяем, есть ли записи с нужным хэшем
        active_records_with_hash = 0
        for result in results:
            if isinstance(result, dict):
                data = result.get("data", {})
                if data.get("file_hash") == file_hash:
                    active_records_with_hash += 1

        # Если передан expected_count > 0 — сверяем с реальным числом таблиц
        if expected_count is not None and expected_count > 0:
            if active_records_with_hash > 0 and active_records_with_hash < expected_count:
                logger.warning(
                    "Partial schema index detected: %d records vs %d expected — will re-index",
                    active_records_with_hash,
                    expected_count,
                )
            is_indexed = active_records_with_hash >= expected_count
        else:
            # Backward-compatible: схема считается проиндексированной при любой записи
            is_indexed = active_records_with_hash > 0

        logger.debug(
            "Schema index check: %d active records with hash %s... -> indexed: %s",
            active_records_with_hash,
            file_hash[:8],
            is_indexed,
        )

        return is_indexed

    def remove_old_schema_records(self, session_id: str, filename: str) -> None:
        """Удаляет старые записи схемы для данного файла.

        Защищён process-level lock и SQLite-транзакцией: SQLite фиксирует
        факт деактивации записей до того, как мы трогаем ChromaDB
        (eventually consistent). При ошибке SQLite — rollback и ранний выход,
        ChromaDB не трогается (иначе получим расхождение).

        SQLite-ошибки и общие ошибки memory layer теперь пробрасываются наверх
        (Phase 6-Extended). ChromaDB cleanup после успешного SQLite commit:
        по умолчанию ошибка громко логируется (logger.error) и НЕ ломает
        вызов — это managed soft-fail, потому что SQLite уже source of truth.
        Чтобы сделать его strict, выставьте `TEXT_TO_SQL_STRICT_CHROMA_CLEANUP=1`.
        """
        # Получаем менеджер памяти
        from memory.manager import memory_manager

        if not memory_manager:
            return

        # Эскейпим спец-символы LIKE в имени файла, чтобы '%'/'_' в имени
        # не давали ложных совпадений и не деактивировали чужие записи.
        # Обратный слэш сам должен быть удвоен, '%' и '_' — экранированы.
        escaped_filename = (
            filename.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
        )

        # КРИТИЧНО (EPIC 8.3): порядок операций МЕНЯТЬ НЕЛЬЗЯ.
        # file-based lock → SQLite BEGIN IMMEDIATE → SQLite commit → Chroma cleanup.
        # SQLite — source of truth, Chroma — eventually consistent.
        # File-based lock (через fcntl.flock на Unix) защищает И между
        # потоками, И между worker-процессами FastAPI/uvicorn. На read-only
        # ФС (см. __init__) выпадаем в thread-only защиту.
        # Integrity-check ВНЕ write_lock: PRAGMA integrity_check на больших
        # БД может занимать минуты; держать write-lock на это время —
        # DoS для всех writers. Делаем один раз на инстанс на отдельном
        # коннекте.
        self._ensure_sqlite_integrity_checked(memory_manager)

        # W5-T3: используем _write_lock_cm — re-entrant обёртка над
        # _FileLock/_schema_write_lock. Если caller (например,
        # `ensure_schema_indexed_in_memory`) уже взял лок сверху, внутренний
        # acquire становится no-op (без deadlock на fcntl.flock с новым fd).
        with self._write_lock_cm():
            # T3.20: используем публичный API вместо `db_handler._get_connection()`.
            # Контракт `memory_manager.get_sqlite_connection()` подтверждён
            # (memory/database.py:get_connection → sqlite3.connect): возвращает
            # НОВОЕ per-call соединение, поэтому `conn.close()` в finally
            # корректен и не выдёргивает shared-хэндл.
            conn = memory_manager.get_sqlite_connection()
            try:
                # Явная транзакция: BEGIN IMMEDIATE — захватываем write-lock
                # сразу, чтобы избежать гонки SELECT/UPDATE между сессиями.
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except Exception as _begin_exc:
                    # Не все драйверы поддерживают BEGIN IMMEDIATE;
                    # допустим обычный неявный режим. Это НЕ silent fallback —
                    # отсутствие IMMEDIATE-mode не нарушает корректность,
                    # только ослабляет concurrency-гарантии до драйверного дефолта.
                    logger.warning("BEGIN IMMEDIATE failed, falling back to default transaction mode: %s", _begin_exc)

                cursor = conn.cursor()

                # Деактивируем записи в SQLite.
                # 4.6: компактный формат `"key":"value"` (без пробела) соответствует
                # save_memory(separators=(",",":"), sort_keys=True).
                current_time = datetime.now().isoformat()
                cursor.execute("""
                    UPDATE agent_memory
                    SET valid_to = ?, updated_at = ?
                    WHERE session_id = ? AND agent_name = ? AND valid_to IS NULL
                    AND data LIKE '%"filename":"' || ? || '"%' ESCAPE '\\'
                    AND data LIKE '%"cache_kind":"schema_table"%' ESCAPE '\\'
                """, (current_time, current_time, session_id, "Schema-RAG-Agent", escaped_filename))

                updated_rows = cursor.rowcount
                conn.commit()

                if updated_rows > 0:
                    logger.info(f"Deactivated {updated_rows} old schema records for {filename}")
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    # rollback внутри cleanup-finally; если падает — это не
                    # silent fallback, мы всё равно raise оригинал ниже.
                    pass
                raise
            finally:
                conn.close()

            # ChromaDB чистим после успешного SQLite-коммита.
            # Порядок важен: SQLite — source of truth, Chroma — eventually consistent.
            strict_chroma_cleanup = os.getenv("TEXT_TO_SQL_STRICT_CHROMA_CLEANUP", "0") == "1"
            try:
                # T3.20: публичный API вместо прямого обращения к `db_handler.tactical_collection`.
                tactical = memory_manager.get_tactical_collection()
                if tactical:
                    results = tactical.get(where={"$and": [
                        {"session_id": {"$eq": session_id}},
                        {"filename": {"$eq": filename}},
                        {"cache_kind": {"$eq": "schema_table"}}
                    ]})

                    if results and results.get("ids"):
                        ids_to_delete = results["ids"]
                        if ids_to_delete:
                            tactical.delete(ids=ids_to_delete)
                            logger.info(f"Deleted {len(ids_to_delete)} schema records from ChromaDB for {filename}")

            except Exception as e:
                self._mark_vector_stale(memory_manager, "schema_cleanup_failed")
                if strict_chroma_cleanup:
                    raise
                # EVENTUAL CONSISTENCY WINDOW: если процесс упадёт между SQLite commit и Chroma cleanup,
                # записи в SQLite будут деактивированы (source of truth), но Chroma может содержать
                # устаревшие векторы. При следующем ensure_schema_indexed_in_memory они будут
                # переписаны — это допустимая кратковременная рассинхронизация.
                # Для строгого режима: TEXT_TO_SQL_STRICT_CHROMA_CLEANUP=1.
                # Managed soft-fail: SQLite (source of truth) уже коммитнут,
                # Chroma догонится при следующем proceed. Логируем громко.
                logger.error(
                    f"Failed to delete schema records from ChromaDB for {filename}: {e}. "
                    f"SQLite committed; set TEXT_TO_SQL_STRICT_CHROMA_CLEANUP=1 to fail-fast."
                )

    def index_schema_in_memory(
        self,
        session_id: str,
        filename: str,
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        file_hash: str,
        *,
        schema_version: Optional[str] = None,
        semantic_namespace_key: Optional[str] = None,
    ) -> int:
        """Индексирует схему в тактической памяти по таблицам.

        W2-T1: fail-fast вместо silent ``return 0``.

        * Если ``save_memory`` недоступен → :class:`SchemaIndexingMemoryUnavailable`.
          Это конфиг-проблема (memory.tools не подгрузился), а не штатное
          "нет данных"; caller должен явно отличать её от пустой схемы.
        * Если хоть одна таблица упала на save_memory → собираем все
          неудачи и в конце поднимаем :class:`SchemaIndexingError` с полем
          ``failed_tables`` и ``indexed_count`` (частичный успех виден).
        * Возврат ``0`` теперь возможен ТОЛЬКО при пустом ``db_schema``
          (нет таблиц для индексации) — это штатное "нет данных".
        """
        from memory.tools import save_memory

        if not save_memory:
            # Сигнализируем отсутствие memory-стека явно — раньше это
            # маскировалось в `return 0`, и caller думал "схема пуста".
            raise SchemaIndexingMemoryUnavailable(
                "memory.tools.save_memory is unavailable; "
                f"cannot index schema for session {session_id!r} "
                f"(filename={filename!r})"
            )

        schema_version = schema_version or get_schema_version(db_schema)
        indexed_count = 0
        # Аккумулируем неудачи по таблицам, чтобы дать caller'у видеть,
        # какие именно записи не были сохранены (частичный успех).
        failed_tables: List[tuple[str, str]] = []

        for table_fqn, table_schema in db_schema.items():
            # Формируем описание таблицы для индексации
            table_description = self.create_table_description(table_fqn, table_schema)

            # Получаем колонки в новом формате
            table_columns = get_table_columns(table_schema)

            # Подсчитываем статистику
            pk_count = sum(1 for col_info in table_columns.values()
                         if isinstance(col_info, dict) and is_pk(col_info))
            fk_count = sum(1 for col_info in table_columns.values()
                         if isinstance(col_info, dict) and is_fk(col_info))

            # Метаданные для записи
            metadata = {
                "cache_source": "schema_vector",
                "cache_kind": "schema_table",
                "source": "schema_file",
                "filename": filename,
                "file_hash": file_hash,
                "schema_version": schema_version,
                "table_fqn": table_fqn,
                "table_name": table_fqn.split(".")[-1],  # Короткое имя
                "column_count": len(table_columns),
                "pk_count": pk_count,
                "fk_count": fk_count,
                "auto_loaded": True,
                "description": table_description,
                # Добавляем полную информацию о таблице для восстановления схемы.
                # Раньше тут была жёсткая логика legacy/new формата
                # (`table_schema.get("description", "")` + `table_schema.get("columns", {}).items()`),
                # которая разваливалась на legacy-схемах, где колонки лежат прямо в корне
                # таблицы (без ключа "columns"). Helper'ы из utils.py нормализуют оба формата.
                "table_info": {
                    "table_name": table_fqn,
                    "description": get_table_description(table_schema),
                    "columns": [
                        {
                            "name": col_name,
                            "type": col_info.get("type", "") if isinstance(col_info, dict) else str(col_info),
                            "description": col_info.get("description", "") if isinstance(col_info, dict) else "",
                            "not_null": col_info.get("not_null", "") if isinstance(col_info, dict) else "",
                            "default_value": col_info.get("default_value", "") if isinstance(col_info, dict) else "",
                            "constraint_type": col_info.get("constraint_type", "") if isinstance(col_info, dict) else "",
                            "references": col_info.get("references", "") if isinstance(col_info, dict) else ""
                        }
                        for col_name, col_info in table_columns.items()
                    ]
                }
            }
            if semantic_namespace_key is not None:
                metadata["semantic_id"] = canonical_schema_table_id(
                    semantic_namespace_key,
                    table_fqn,
                )

            # Per-table save: ловим узко вокруг save_memory, чтобы не
            # маскировать ошибки построения metadata (это — баги).
            # Раньше единый broad-except поверх всего цикла глушил И сбои
            # БД, И ошибки сборки metadata, скрывая реальную причину.
            try:
                saved_step = save_memory(
                    session_id=session_id,
                    agent_name="Schema-RAG-Agent",
                    data=metadata,
                )
                if semantic_namespace_key is not None and (
                    type(saved_step) is not int or saved_step < 0
                ):
                    raise RuntimeError("save_memory did not persist the schema row")
            except Exception as exc:  # noqa: BLE001 — собираем все per-table сбои
                # Память может быть SQLite/Chroma; разные backends → разные
                # исключения. Контекст (таблица + причина) логируем сразу,
                # а финальный raise — после цикла, чтобы видеть масштаб.
                logger.error(
                    "Failed to index table %r for session %s: %s",
                    table_fqn, session_id, exc,
                )
                failed_tables.append((table_fqn, str(exc)))
                continue

            indexed_count += 1

        if failed_tables:
            tables_msg = ", ".join(f"{name}: {err}" for name, err in failed_tables)
            raise SchemaIndexingError(
                f"Failed to index {len(failed_tables)}/{len(db_schema)} tables "
                f"for session {session_id!r} (filename={filename!r}); "
                f"successfully indexed {indexed_count}. Failures: {tables_msg}",
                failed_tables=[name for name, _ in failed_tables],
                indexed_count=indexed_count,
            )

        if indexed_count > 0:
            logger.info(f"✅ Schema indexed in memory: {indexed_count} tables for session {session_id}")
        return indexed_count

    def create_table_description(self, table_fqn: str, table_schema: Dict[str, Dict[str, Any]]) -> str:
        """Создает описание таблицы для семантического поиска на русском языке."""
        try:
            # Начинаем с имени таблицы
            parts = [f"Таблица {table_fqn}"]

            # Добавляем описание таблицы если есть
            table_description = get_table_description(table_schema)
            if table_description:
                parts.append(f"Описание: {table_description}")

            # Получаем колонки в новом формате
            table_columns = get_table_columns(table_schema)

            # Собираем информацию о ключевых колонках
            pk_columns = []
            fk_columns = []
            important_columns = []

            for col_name, col_info in table_columns.items():
                if isinstance(col_info, dict):
                    col_type = col_info.get("type", "")
                    col_desc = col_info.get("description", "")

                    if is_pk(col_info):
                        pk_columns.append(f"{col_name} ({col_type})")
                    elif is_fk(col_info):
                        refs = col_info.get("references", "")
                        fk_columns.append(f"{col_name} -> {refs}" if refs else col_name)
                    elif col_desc and len(col_desc) > 10:  # Значимые описания
                        important_columns.append(f"{col_name}: {col_desc}")
                    elif col_type:
                        # EPIC 5.3: список substring-маркеров живёт в
                        # significance.yaml (important_column_name_substrings),
                        # а не в коде. Никаких хардкодных доменных слов.
                        from .significance_config import load_significance_config
                        substrings = load_significance_config().important_column_name_substrings
                        col_name_lower = col_name.lower()
                        if any(keyword in col_name_lower for keyword in substrings):
                            important_columns.append(f"{col_name} ({col_type})")

            # Добавляем информацию о ключах
            if pk_columns:
                parts.append(f"Первичные ключи: {', '.join(pk_columns)}")

            if fk_columns:
                parts.append(f"Внешние ключи: {', '.join(fk_columns[:3])}")  # Ограничиваем количество

            # Добавляем важные колонки
            if important_columns:
                parts.append(f"Важные колонки: {', '.join(important_columns[:5])}")  # Ограничиваем количество

            # Добавляем общую информацию
            total_columns = len(table_columns)
            parts.append(f"Всего колонок: {total_columns}")

            return ". ".join(parts) + "."

        except Exception as e:
            logger.warning(f"Failed to create table description for {table_fqn}: {e}")
            return f"Таблица {table_fqn}"

    def find_semantic_relevant_tables(
        self,
        entities: List[str],
        namespace: Optional[SchemaNamespace] = None,
        dsn: Optional[str] = None,
    ) -> List[str]:
        """Находит семантически релевантные таблицы через поиск в памяти с фильтрацией по скору."""
        if isinstance(namespace, SchemaScope):
            raise TypeError(
                "SchemaScope is unversioned; semantic search requires SchemaNamespace"
            )
        if isinstance(namespace, str):
            if dsn is not None:
                raise TypeError("DSN was provided twice")
            dsn = namespace
            namespace = None
        if namespace is not None and not isinstance(namespace, SchemaNamespace):
            raise TypeError("namespace must be a SchemaNamespace or null")
        try:
            from memory.manager import memory_manager

            vector_readiness = self._observe_vector_readiness(memory_manager)
            if vector_readiness != _VECTOR_READY:
                if namespace is not None:
                    relevant_tables = self._sqlite_lexical_schema_tables(
                        entities, namespace
                    )
                    self._set_search_status(
                        "sqlite_lexical_ok" if relevant_tables else "sqlite_lexical_no_hits",
                        self.last_vector_reason
                        or "semantic vector index is not ready",
                    )
                    return relevant_tables
                self._set_search_status(
                    f"vector_{vector_readiness}",
                    self.last_vector_reason or "semantic vector index is not ready",
                )
                return []
            # T3.20: публичный API; `get_tactical_collection()` возвращает None,
            # если Chroma не инициализирована.
            tactical_collection = memory_manager.get_tactical_collection() if memory_manager else None
            if not memory_manager or not tactical_collection:
                self._set_search_status("memory_unavailable", "memory manager or tactical_collection is unavailable")
                return []

            if namespace is not None:
                session_id = namespace.version_key
                from memory.index_consistency import reconcile_tactical_namespace

                reconciliation = reconcile_tactical_namespace(
                    session_id=session_id,
                    agent_name="Schema-RAG-Agent",
                    cache_kind="schema_table",
                    repair_missing=True,
                )
                if not reconciliation.exact:
                    self._set_search_status(
                        "memory_unavailable",
                        "schema semantic IDs are not exactly reconciled",
                    )
                    return []
                active_semantic_ids = set(reconciliation.expected_ids)
            else:
                # Legacy outer adapter: direct library calls still accept DSN.
                from .utils import resolve_dsn

                effective_dsn = resolve_dsn(dsn) or ""
                if not effective_dsn:
                    self._set_search_status("memory_unavailable", "runtime DSN is required for schema memory search")
                    return []
                session_id = dsn_to_sanitized_name(effective_dsn)
                active_semantic_ids = None

            # Формируем поисковый запрос из сущностей
            search_terms = []
            for entity in entities:
                if isinstance(entity, str) and len(entity.strip()) > 2:
                    search_terms.append(entity.strip())

            if not search_terms:
                self._set_search_status("empty_query", "No searchable schema-linking terms provided")
                return []

            # Ограничиваем количество терминов для эффективности
            search_query = " ".join(search_terms[:5])

            logger.debug(
                "Searching for relevant tables with query: %r",
                _redact_schema_memory_value(search_query),
            )

            # Параметры фильтрации.
            # W9-A7: env SCHEMA_TABLE_MIN_SCORE сохраняет приоритет (legacy
            # контракт); при отсутствии env читаем из yaml-профиля
            # similarity_thresholds.schema_linking_min_score.
            from .similarity_thresholds_config import resolve_threshold

            min_score = resolve_threshold(
                "schema_linking_min_score", env_override="SCHEMA_TABLE_MIN_SCORE"
            )

            # Выполняем семантический поиск с получением скоров
            where_filter = {
                "$and": [
                    {"session_id": session_id},
                    {"cache_kind": "schema_table"}
                ]
            }

            # T3.20: публичный API вместо `_search_semantic_with_scores`.
            semantic_search_results = memory_manager.search_semantic_with_scores(
                tactical_collection,
                search_query,
                n_results=50,  # Получаем больше результатов для фильтрации
                where=where_filter
            )

            if not semantic_search_results or 'ids' not in semantic_search_results:
                logger.info(
                    "No semantic search results for entities: %s",
                    _redact_schema_memory_value(entities),
                )
                self._set_search_status("no_hits", "Semantic search returned no schema_table records")
                return []

            # ChromaDB query() возвращает list-of-lists: одна вложенная коллекция
            # на каждый исходный запрос. У нас query batch size = 1, поэтому
            # распаковываем первый (и единственный) уровень ПЕРЕД итерацией,
            # чтобы zip(ids, distances, metadatas) точно сопоставлял элементы.
            raw_ids = semantic_search_results.get('ids') or []
            ids = raw_ids[0] if raw_ids and isinstance(raw_ids[0], list) else raw_ids

            raw_distances = semantic_search_results.get('distances') or []
            if raw_distances and isinstance(raw_distances[0], list):
                distances = raw_distances[0]
            elif raw_distances:
                distances = raw_distances
            else:
                distances = [0.0] * len(ids)

            raw_metadatas = semantic_search_results.get('metadatas') or []
            if raw_metadatas and isinstance(raw_metadatas[0], list):
                metadatas = raw_metadatas[0]
            elif raw_metadatas:
                metadatas = raw_metadatas
            else:
                metadatas = [{}] * len(ids)

            # Выравниваем длины на случай частичных ответов от Chroma.
            min_len = min(len(ids), len(distances), len(metadatas))
            ids = ids[:min_len]
            distances = distances[:min_len]
            metadatas = metadatas[:min_len]

            # T3.6: similarity ВЫЧИСЛЯЕТСЯ ИЗ ФАКТИЧЕСКОЙ метрики коллекции
            # Chroma, а не предполагается cosine. Дефолтная metric у Chroma —
            # `l2`, а формула `1.0 - distance/2.0` валидна только для cosine
            # distance в [0, 2]. На l2-коллекции это давало "similarity",
            # которое могло уходить в большой минус для любых расстояний > 2
            # и обрезалось `max(0, ...)`, делая ranking бесполезным.
            metric = _resolve_chroma_metric(tactical_collection)
            scored_results = []
            for i, (id_val, distance, metadata) in enumerate(zip(ids, distances, metadatas)):
                if active_semantic_ids is not None and id_val not in active_semantic_ids:
                    continue
                similarity = _distance_to_similarity(distance, metric)
                table_fqn = metadata.get("table_fqn")

                if table_fqn and similarity >= min_score:
                    scored_results.append((table_fqn, similarity))
                elif table_fqn:
                    logger.debug(
                        f"Filtered table {table_fqn} due to low score: "
                        f"{similarity:.3f} < {min_score} (metric={metric})"
                    )

            # Сортируем по скору (лучшие первыми)
            scored_results.sort(key=lambda x: x[1], reverse=True)

            # Извлекаем только имена таблиц
            relevant_tables = []
            seen_tables = set()

            for table_fqn, score in scored_results:
                if table_fqn not in seen_tables:
                    relevant_tables.append(table_fqn)
                    seen_tables.add(table_fqn)
                    logger.debug(f"Selected table {table_fqn} with relevance score: {score:.3f}")

            # Логирование результатов
            if relevant_tables:
                self._set_search_status("ok")
                logger.info(f"Found {len(relevant_tables)} semantically relevant tables: {relevant_tables[:10]}")
                if scored_results:
                    best_score = scored_results[0][1]
                    worst_score = scored_results[-1][1] if len(scored_results) > 1 else best_score
                    logger.debug(f"Relevance scores range: {worst_score:.3f} - {best_score:.3f} (min threshold: {min_score})")
            else:
                logger.info(
                    "No semantically relevant tables found for entities: %s (min_score: %s)",
                    _redact_schema_memory_value(entities),
                    min_score,
                )
                self._set_search_status("no_hits", f"No schema_table records matched min_score {min_score}")

            return relevant_tables

        except Exception as e:
            # Конфиг-ошибка эмбеддингов: пробрасываем, чтобы caller видел
            # реальную причину (OPENAI_API_KEY_DB / embedding config), а не []
            try:
                from memory.manager import EmbeddingUnavailableError, EmbeddingFailedError
                if isinstance(e, (EmbeddingUnavailableError, EmbeddingFailedError)):
                    logger.error(
                        "Embedding model unavailable in find_semantic_relevant_tables"
                        " — check OPENAI_API_KEY_DB / embedding config: %s",
                        _redact_schema_memory_value(e),
                    )
                    self._set_search_status("embedding_unavailable", str(e))
                    raise
            except ImportError:
                # memory.manager недоступен — не можем классифицировать e как
                # embedding-ошибку. НЕ молчим: логируем, что тип неизвестен, и
                # падаем в общий warning-путь ниже (исходный e сохранён).
                logger.warning(
                    "Не удалось импортировать EmbeddingUnavailableError — тип "
                    "ошибки embedding неизвестен, трактуем как generic: %s",
                    _redact_schema_memory_value(e),
                )
            logger.warning(
                "Failed to find semantic relevant tables: %s",
                _redact_schema_memory_value(e),
            )
            self._mark_vector_stale(memory_manager, "semantic_search_failed")
            self.last_vector_readiness = "stale"
            self.last_vector_reason = "semantic_search_failed"
            self._set_search_status("vector_stale", "semantic_search_failed")
            return []

    def find_verified_probe_facts(
        self,
        entities: List[str],
        namespace: SchemaNamespace,
    ) -> List[dict[str, Any]]:
        """Return a bounded set of matching facts from one exact namespace."""
        if not isinstance(namespace, SchemaNamespace):
            raise TypeError("namespace must be SchemaNamespace")
        terms = tuple(
            dict.fromkeys(
                item.casefold()
                for item in entities
                if isinstance(item, str) and item.strip()
            )
        )
        if not terms:
            return []
        from memory.tools import get_memory, memory_requester_context

        with memory_requester_context("Schema-RAG-Agent"):
            records = get_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_probe_fact",
                requesting_agent="Schema-RAG-Agent",
            )
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for record in records:
            data = record.get("data") if isinstance(record, dict) else None
            if not isinstance(data, dict) or data.get("schema_version") != namespace.version_key:
                continue
            fact = data.get("fact")
            key = data.get("cache_key")
            if not isinstance(fact, dict) or key != _probe_fact_key(fact):
                continue
            text = json.dumps(fact, ensure_ascii=False, sort_keys=True).casefold()
            score = sum(term in text for term in terms)
            if score:
                scored.append((score, key, fact))
        return [
            fact
            for _, _, fact in sorted(scored, key=lambda item: (-item[0], item[1]))[:5]
        ]

    def save_verified_probe_facts(
        self,
        namespace: SchemaNamespace,
        state: object,
    ) -> None:
        """Save normalized facts once per namespace and fact key."""
        if not isinstance(namespace, SchemaNamespace):
            raise TypeError("namespace must be SchemaNamespace")
        from memory.tools import get_memory, memory_requester_context, save_memory

        for fact in _verified_probe_facts(namespace, state):
            fact_key = _probe_fact_key(fact)
            with memory_requester_context("Schema-RAG-Agent"):
                existing = get_memory(
                    session_id=namespace.version_key,
                    agent_name="Schema-RAG-Agent",
                    cache_kind="schema_probe_fact",
                    cache_key=fact_key,
                    requesting_agent="Schema-RAG-Agent",
                )
            if any(
                isinstance(record.get("data"), dict)
                and record["data"].get("fact") == fact
                for record in existing
                if isinstance(record, dict)
            ):
                continue
            saved_step = save_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                data={
                    "cache_kind": "schema_probe_fact",
                    "cache_key": fact_key,
                    "cache_source": "typed_db_probe",
                    "schema_version": namespace.version_key,
                    "fact": fact,
                },
            )
            if type(saved_step) is not int or saved_step < 0:
                raise SchemaIndexingError("verified Typed probe fact was not persisted")
            self.save_approved_semantic_facts(
                namespace,
                _approved_semantic_facts_from_probe_fact(fact),
            )

    @staticmethod
    def _scan_semantic_fact_candidates(
        namespace: SchemaNamespace,
    ) -> List["_FactCandidate"]:
        """Read+filter every currently-live approved fact record of a namespace.

        Shared "record -> surviving approved fact" plumbing between
        ``find_approved_semantic_facts`` (which additionally scores by
        search terms) and ``list_semantic_facts`` (which returns every fact
        of given sources with no term filter). Neither the search-term score
        nor status-override application belongs here — both are applied by
        the two callers on top of this shared candidate set.
        """
        if not isinstance(namespace, SchemaNamespace):
            raise TypeError("namespace must be SchemaNamespace")
        from memory.tools import get_memory, memory_requester_context

        with memory_requester_context("Schema-RAG-Agent"):
            records = get_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_semantic_fact",
                requesting_agent="Schema-RAG-Agent",
            )
        # W2-2.2: ``file`` and ``dsn_glossary`` are both "replaceable"
        # fact-set sources — each write replaces the whole set, and a
        # snapshot marker record (``is_file_snapshot`` /
        # ``is_dsn_glossary_snapshot``) tells us which source_digest is
        # current so stale facts from an older replace call drop out.
        # ``typed_probe`` facts have no such marker and are never filtered
        # by digest below.
        snapshot_marker_fields = {
            "file": "is_file_snapshot",
            "dsn_glossary": "is_dsn_glossary_snapshot",
        }
        latest_snapshot: dict[str, tuple[int, str] | None] = {
            source: None for source in snapshot_marker_fields
        }
        candidates: list[tuple[int, str, str, SemanticFact]] = []
        for sequence, record in enumerate(records):
            data = record.get("data") if isinstance(record, dict) else None
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != namespace.version_key
            ):
                continue
            step = record.get("step") if isinstance(record, dict) else None
            sequence_or_step = step if isinstance(step, int) else sequence
            matched_snapshot_marker = False
            for source, marker_field in snapshot_marker_fields.items():
                if data.get(marker_field) is not True:
                    continue
                matched_snapshot_marker = True
                source_digest = data.get("source_digest")
                if (
                    isinstance(source_digest, str)
                    and data.get("cache_key")
                    == _semantic_file_snapshot_key(source_digest, source=source)
                    and (
                        latest_snapshot[source] is None
                        or sequence_or_step > latest_snapshot[source][0]
                    )
                ):
                    latest_snapshot[source] = (sequence_or_step, source_digest)
                break
            if matched_snapshot_marker:
                continue
            try:
                fact = SemanticFact.model_validate(data.get("fact"))
            except (TypeError, ValueError):
                continue
            if fact.status != "approved":
                continue
            key = data.get("cache_key")
            if not isinstance(key, str) or key != _semantic_fact_key(fact):
                continue
            candidates.append(
                (
                    sequence_or_step,
                    key,
                    data.get("source_digest") if isinstance(data.get("source_digest"), str) else key,
                    fact,
                )
            )
        latest_fact_source: dict[str, dict[tuple[str, str, str | None, str], tuple[int, str]]] = {
            source: {} for source in snapshot_marker_fields
        }
        for step, _key, source_digest, fact in candidates:
            if fact.source not in latest_fact_source:
                continue
            identity = (fact.subject, fact.table_fqn, fact.column, fact.fact_kind)
            previous = latest_fact_source[fact.source].get(identity)
            if previous is None or step > previous[0]:
                latest_fact_source[fact.source][identity] = (step, source_digest)

        def _survives_replacement(fact: SemanticFact, source_digest: str) -> bool:
            if fact.source not in snapshot_marker_fields:
                return True
            snapshot = latest_snapshot[fact.source]
            if snapshot is not None:
                return source_digest == snapshot[1]
            identity = (fact.subject, fact.table_fqn, fact.column, fact.fact_kind)
            return source_digest == latest_fact_source[fact.source][identity][1]

        return [
            _FactCandidate(fact=fact, key=key, step=step, source_digest=source_digest)
            for step, key, source_digest, fact in candidates
            if _survives_replacement(fact, source_digest)
        ]

    @staticmethod
    def _latest_semantic_fact_status_overrides(
        namespace: SchemaNamespace,
    ) -> dict[str, str]:
        """Latest override status per overridden ``fact_key`` in one namespace.

        Overrides are append-only records under
        ``cache_kind="schema_semantic_fact_status_override"`` (written by
        ``set_semantic_fact_status_override``); the most recent one (by
        ``sequence_or_step``) wins for a given ``fact_key``.
        """
        if not isinstance(namespace, SchemaNamespace):
            raise TypeError("namespace must be SchemaNamespace")
        from memory.tools import get_memory, memory_requester_context

        with memory_requester_context("Schema-RAG-Agent"):
            records = get_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_semantic_fact_status_override",
                requesting_agent="Schema-RAG-Agent",
            )
        latest: dict[str, tuple[int, str]] = {}
        for sequence, record in enumerate(records):
            data = record.get("data") if isinstance(record, dict) else None
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != namespace.version_key
            ):
                continue
            overridden_fact_key = data.get("overridden_fact_key")
            status = data.get("status")
            if not isinstance(overridden_fact_key, str) or status not in (
                "approved",
                "rejected",
            ):
                continue
            step = record.get("step") if isinstance(record, dict) else None
            sequence_or_step = step if isinstance(step, int) else sequence
            previous = latest.get(overridden_fact_key)
            if previous is None or sequence_or_step > previous[0]:
                latest[overridden_fact_key] = (sequence_or_step, status)
        return {key: status for key, (_step, status) in latest.items()}

    def find_approved_semantic_facts(
        self,
        terms: Sequence[str],
        namespace: SchemaNamespace,
    ) -> List[SemanticFact]:
        """Read matching, non-rejected approved facts from one schema namespace."""
        normalized_terms = tuple(
            dict.fromkeys(
                term.casefold()
                for term in terms
                if isinstance(term, str) and term.strip()
            )
        )
        if not normalized_terms:
            return []
        candidates = self._scan_semantic_fact_candidates(namespace)
        overrides = self._latest_semantic_fact_status_overrides(namespace)
        scored: list[tuple[int, str, SemanticFact]] = []
        for candidate in candidates:
            if overrides.get(candidate.key) == "rejected":
                continue
            rendered = json.dumps(
                _semantic_fact_dump(candidate.fact), ensure_ascii=False, sort_keys=True
            ).casefold()
            score = sum(term in rendered for term in normalized_terms)
            if score:
                scored.append((score, candidate.key, candidate.fact))
        return [
            fact
            for _score, _key, fact in sorted(scored, key=lambda item: (-item[0], item[1]))
        ]

    def list_semantic_facts(
        self,
        namespace: SchemaNamespace,
        *,
        sources: frozenset[str] = frozenset({"typed_probe"}),
    ) -> list[tuple[SemanticFact, Literal["approved", "rejected"]]]:
        """All current facts of ``sources`` in one namespace, with effective status.

        Unlike ``find_approved_semantic_facts`` there is no search-term
        filter — every surviving fact of the requested sources is returned,
        paired with its effective status after applying any
        ``set_semantic_fact_status_override`` record.
        """
        candidates = self._scan_semantic_fact_candidates(namespace)
        overrides = self._latest_semantic_fact_status_overrides(namespace)
        result: list[tuple[SemanticFact, Literal["approved", "rejected"]]] = []
        for candidate in candidates:
            if candidate.fact.source not in sources:
                continue
            effective_status = overrides.get(candidate.key, "approved")
            if effective_status not in ("approved", "rejected"):
                effective_status = "approved"
            result.append((candidate.fact, effective_status))
        return result

    def set_semantic_fact_status_override(
        self,
        namespace: SchemaNamespace,
        fact_key: str,
        status: Literal["approved", "rejected"],
    ) -> None:
        """Record an explicit approved/rejected override for ``fact_key``.

        Writes a small, standalone override record (not a ``SemanticFact``)
        rather than mutating the original fact record — the override targets
        the original fact's identity (``fact_key``, computed the same way
        ``save_approved_semantic_facts`` computed it) so the original
        append-only audit record is never touched, and a repeated call with
        the same status is a no-op (manual existence check before insert,
        mirroring ``save_approved_semantic_facts``'s own dedup idiom —
        ``save_memory``'s built-in dedup only covers
        ``SEMANTIC_CACHE_KINDS``, which this cache_kind is not part of).
        """
        if not isinstance(namespace, SchemaNamespace):
            raise TypeError("namespace must be SchemaNamespace")
        if not isinstance(fact_key, str) or not fact_key.strip():
            raise ValueError("fact_key must be a non-empty string")
        if status not in ("approved", "rejected"):
            raise ValueError("status must be 'approved' or 'rejected'")
        from memory.tools import get_memory, memory_requester_context, save_memory

        override_key = _semantic_fact_status_override_key(fact_key)
        with memory_requester_context("Schema-RAG-Agent"):
            existing = get_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_semantic_fact_status_override",
                cache_key=override_key,
                requesting_agent="Schema-RAG-Agent",
            )
        if any(
            isinstance(record, dict)
            and isinstance(record.get("data"), dict)
            and record["data"].get("overridden_fact_key") == fact_key
            and record["data"].get("status") == status
            for record in existing
        ):
            return
        saved_step = save_memory(
            session_id=namespace.version_key,
            agent_name="Schema-RAG-Agent",
            data={
                "cache_kind": "schema_semantic_fact_status_override",
                "cache_key": override_key,
                "cache_source": "metadata_editor",
                "schema_version": namespace.version_key,
                "overridden_fact_key": fact_key,
                "status": status,
            },
        )
        if type(saved_step) is not int or saved_step < 0:
            raise SchemaIndexingError("semantic fact status override was not persisted")

    def save_approved_semantic_facts(
        self,
        namespace: SchemaNamespace,
        facts: Sequence[SemanticFact],
    ) -> None:
        """Persist each approved fact once in the existing schema memory."""
        if not isinstance(namespace, SchemaNamespace):
            raise TypeError("namespace must be SchemaNamespace")
        from memory.tools import get_memory, memory_requester_context, save_memory

        facts = tuple(facts)
        for fact in facts:
            if not isinstance(fact, SemanticFact):
                raise TypeError("facts must contain SemanticFact values")
            if fact.status != "approved":
                raise ValueError("only approved semantic facts may be persisted")
        file_facts = tuple(fact for fact in facts if fact.source == "file")
        if file_facts:
            self.replace_file_semantic_facts(namespace, file_facts)
            facts = tuple(fact for fact in facts if fact.source != "file")

        for fact in facts:
            fact_key = _semantic_fact_key(fact)
            with memory_requester_context("Schema-RAG-Agent"):
                existing = get_memory(
                    session_id=namespace.version_key,
                    agent_name="Schema-RAG-Agent",
                    cache_kind="schema_semantic_fact",
                    cache_key=fact_key,
                    requesting_agent="Schema-RAG-Agent",
                )
            if any(
                isinstance(record, dict)
                and isinstance(record.get("data"), dict)
                and record["data"].get("fact") == _semantic_fact_dump(fact)
                for record in existing
            ):
                continue
            saved_step = save_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                data={
                    "cache_kind": "schema_semantic_fact",
                    "cache_key": fact_key,
                    "cache_source": "typed_semantic_fact",
                    "schema_version": namespace.version_key,
                    "source_digest": None,
                    "fact": _semantic_fact_dump(fact),
                },
            )
            if type(saved_step) is not int or saved_step < 0:
                raise SchemaIndexingError("approved semantic fact was not persisted")

    def _replace_semantic_fact_source(
        self,
        namespace: SchemaNamespace,
        facts: tuple[SemanticFact, ...],
        *,
        source: str,
        marker_field: str,
    ) -> None:
        """Shared "replace the whole fact-set" plumbing for one source.

        Used by both ``replace_file_semantic_facts`` (``source="file"``) and
        ``replace_dsn_glossary_facts`` (``source="dsn_glossary"``, EPIC
        W2-2.2). Writes a snapshot marker record carrying the current
        ``source_digest`` so ``find_approved_semantic_facts`` can drop facts
        left over from an earlier replace call, then persists each fact once.
        """
        from memory.tools import get_memory, memory_requester_context, save_memory

        source_digest = _semantic_fact_source_digest(facts)
        snapshot_key = _semantic_file_snapshot_key(source_digest, source=source)
        with memory_requester_context("Schema-RAG-Agent"):
            existing = get_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                cache_kind="schema_semantic_fact",
                cache_key=snapshot_key,
                requesting_agent="Schema-RAG-Agent",
            )
        if not any(
            isinstance(record, dict)
            and isinstance(record.get("data"), dict)
            and record["data"].get(marker_field) is True
            and record["data"].get("source_digest") == source_digest
            for record in existing
        ):
            saved_step = save_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                data={
                    "cache_kind": "schema_semantic_fact",
                    "cache_key": snapshot_key,
                    "cache_source": "typed_semantic_fact",
                    "schema_version": namespace.version_key,
                    marker_field: True,
                    "source_digest": source_digest,
                },
            )
            if type(saved_step) is not int or saved_step < 0:
                raise SchemaIndexingError(
                    f"semantic {source} snapshot was not persisted"
                )

        for fact in facts:
            fact_key = _semantic_fact_key(fact)
            with memory_requester_context("Schema-RAG-Agent"):
                existing = get_memory(
                    session_id=namespace.version_key,
                    agent_name="Schema-RAG-Agent",
                    cache_kind="schema_semantic_fact",
                    cache_key=fact_key,
                    requesting_agent="Schema-RAG-Agent",
                )
            if any(
                isinstance(record, dict)
                and isinstance(record.get("data"), dict)
                and record["data"].get("fact") == _semantic_fact_dump(fact)
                # A fact carried over unchanged from an earlier replace call
                # still needs a fresh row stamped with *this* source_digest,
                # or it would keep the stale digest forever and get dropped
                # by find_approved_semantic_facts' snapshot filter the
                # moment any other fact in the set changes.
                and record["data"].get("source_digest") == source_digest
                for record in existing
            ):
                continue
            saved_step = save_memory(
                session_id=namespace.version_key,
                agent_name="Schema-RAG-Agent",
                data={
                    "cache_kind": "schema_semantic_fact",
                    "cache_key": fact_key,
                    "cache_source": "typed_semantic_fact",
                    "schema_version": namespace.version_key,
                    "source_digest": source_digest,
                    "fact": _semantic_fact_dump(fact),
                },
            )
            if type(saved_step) is not int or saved_step < 0:
                raise SchemaIndexingError("approved semantic fact was not persisted")

    def replace_file_semantic_facts(
        self,
        namespace: SchemaNamespace,
        facts: Sequence[SemanticFact],
    ) -> None:
        """Replace the current file-owned fact snapshot, including an empty one."""
        if not isinstance(namespace, SchemaNamespace):
            raise TypeError("namespace must be SchemaNamespace")
        facts = tuple(facts)
        for fact in facts:
            if not isinstance(fact, SemanticFact) or fact.source != "file":
                raise TypeError("file snapshots must contain file SemanticFact values")
            if fact.status != "approved":
                raise ValueError("only approved semantic facts may be persisted")
        self._replace_semantic_fact_source(
            namespace, facts, source="file", marker_field="is_file_snapshot"
        )

    def replace_dsn_glossary_facts(
        self,
        namespace: SchemaNamespace,
        facts: Sequence[SemanticFact],
    ) -> None:
        """Replace the current DSN-glossary fact snapshot, including an empty one.

        EPIC W2-2.2: a term removed from the DSN glossary profile must
        disappear from ``find_approved_semantic_facts`` on the next call —
        mirrors ``replace_file_semantic_facts`` but keeps its own snapshot
        namespace/marker so it never interacts with file- or
        typed_probe-owned facts.
        """
        if not isinstance(namespace, SchemaNamespace):
            raise TypeError("namespace must be SchemaNamespace")
        facts = tuple(facts)
        for fact in facts:
            if not isinstance(fact, SemanticFact) or fact.source != "dsn_glossary":
                raise TypeError(
                    "dsn glossary snapshots must contain dsn_glossary SemanticFact values"
                )
            if fact.status != "approved":
                raise ValueError("only approved semantic facts may be persisted")
        self._replace_semantic_fact_source(
            namespace,
            facts,
            source="dsn_glossary",
            marker_field="is_dsn_glossary_snapshot",
        )

    def set_schema_ready_marker(
        self,
        session_id: str | SchemaNamespace,
        schema_version: Optional[str] = None,
    ) -> None:
        """Устанавливает маркер готовности схемы."""
        if isinstance(session_id, SchemaScope):
            raise TypeError(
                "SchemaScope is unversioned; ready marker requires SchemaNamespace"
            )
        if isinstance(session_id, SchemaNamespace):
            namespace = session_id
            if not self.is_schema_indexed(
                namespace,
                namespace.version_key,
            ):
                raise SchemaIndexingError(
                    "schema ready marker rejected: semantic IDs are not reconciled"
                )
            return
        if not isinstance(schema_version, str) or not schema_version:
            raise ValueError("schema_version is required")
        try:
            from memory.tools import save_memory

            if not save_memory:
                return

            # Сохраняем маркер готовности схемы
            marker_data = {
                "cache_source": "schema_metadata",
                "cache_kind": "schema_ready",
                "schema_version": schema_version,
                "status": "ready",
                "session_id": session_id
            }

            save_memory(
                session_id=session_id,
                agent_name="Schema-RAG-Agent",
                data=marker_data
            )

            logger.debug(f"Schema ready marker set for session {session_id}, version {schema_version}")

        except Exception as e:
            logger.error(f"Failed to set schema ready marker: {e}")
