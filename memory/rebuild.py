"""
Утилиты пересборки векторной базы данных ChromaDB
=================================================

Этот модуль содержит функции для восстановления ChromaDB из SQLite данных.
Используется для:
- Восстановления после повреждения ChromaDB
- Обновления после изменения модели эмбеддингов  
- Принудительной синхронизации данных
"""

import json
import os
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Any, Dict
from smolagents import tool

from .chroma_text import extract_tactical_chroma_text
from .database import _embedding_dimension
from .manager import memory_manager


_SUPPORTED_CHROMA_METRICS = {"cosine", "l2", "ip"}
_REBUILD_LOCK = threading.RLock()
_PERMANENTLY_SKIPPED_REASONS = frozenset(
    {
        "MALFORMED_JSON",
        "INVALID_PAYLOAD",
        "INVALID_SEMANTIC_ID",
        "CONFLICTING_SOURCE_ROWS",
        "EMBEDDING_UNAVAILABLE",
    }
)


class RebuildStatus(str, Enum):
    COMPLETE = "complete"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True)
class CollectionRebuildReport:
    source_ids: tuple[str, ...]
    expected_ids: tuple[str, ...]
    indexed_ids: tuple[str, ...]
    permanently_skipped: tuple[tuple[str, str], ...]
    failed: tuple[tuple[str, str], ...]
    missing_ids: tuple[str, ...]
    unexpected_ids: tuple[str, ...]


@dataclass(frozen=True)
class RebuildReport:
    status: RebuildStatus
    tactical: CollectionRebuildReport
    strategic: CollectionRebuildReport
    duration_ms: float
    error_code: str | None = None


@dataclass(frozen=True)
class _PreparedCollection:
    records: tuple[Dict[str, Any], ...]
    source_ids: tuple[str, ...]
    permanently_skipped: tuple[tuple[str, str], ...]
    failed: tuple[tuple[str, str], ...]


def _metric_from_collection(collection) -> str | None:
    """Return Chroma hnsw metric from an existing collection, if exposed."""
    metadata = getattr(collection, "metadata", None)
    if isinstance(metadata, dict):
        raw = metadata.get("hnsw:space") or metadata.get("metric")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()

    configuration = getattr(collection, "configuration", None)
    if isinstance(configuration, dict):
        hnsw = configuration.get("hnsw")
        if isinstance(hnsw, dict):
            raw = hnsw.get("space")
            if isinstance(raw, str) and raw.strip():
                return raw.strip().lower()

    return None


def _resolve_rebuild_chroma_metric(handler) -> str:
    """Resolve metric for recreated collections.

    Existing collection metadata wins over env: rebuild should preserve the
    actual metric already on disk when Chroma exposes it. Env remains the
    fallback for fresh collections.
    """
    for collection in (
        getattr(handler, "tactical_collection", None),
        getattr(handler, "strategic_collection", None),
    ):
        metric = _metric_from_collection(collection)
        if metric:
            if metric not in _SUPPORTED_CHROMA_METRICS:
                raise ValueError(
                    f"Unsupported Chroma distance metric '{metric}' while rebuilding"
                )
            return metric

    metric = os.getenv("TEXT_TO_SQL_CHROMA_METRIC", "cosine").strip().lower() or "cosine"
    if metric not in _SUPPORTED_CHROMA_METRICS:
        raise ValueError(
            f"Unsupported TEXT_TO_SQL_CHROMA_METRIC '{metric}' while rebuilding"
        )
    return metric


def _collection_metadata(handler, description: str, chroma_metric: str) -> Dict:
    metadata = {
        "description": description,
        "hnsw:space": chroma_metric,
    }
    embedding_model_name = getattr(handler, "embedding_model_name", "") or ""
    if embedding_model_name:
        metadata["embedding_model_name"] = embedding_model_name

    embedding_dimension = getattr(handler, "embedding_dimension", None)
    if embedding_dimension is None:
        embedding_dimension = _embedding_dimension(getattr(handler, "embedding_model", None))
    if embedding_dimension is not None:
        metadata["embedding_dimension"] = int(embedding_dimension)

    return metadata


def _extract_tactical_chroma_text(data: Dict) -> str:
    """Mirror save_memory's Chroma document text selection for tactical rows."""
    return extract_tactical_chroma_text(
        data,
        fallback_extractor=memory_manager._extract_text_content,
    )


def _handler_lock_cm(handler):
    lock = getattr(handler, "lock", None)
    if lock is None:
        return nullcontext()
    return lock


def _empty_collection_report(
    *,
    failed: tuple[tuple[str, str], ...] = (),
) -> CollectionRebuildReport:
    return CollectionRebuildReport((), (), (), (), failed, (), ())


def _failed_report(started_at: float, error_code: str) -> RebuildReport:
    failed = (("__rebuild__", error_code),)
    return RebuildReport(
        status=RebuildStatus.FAILED,
        tactical=_empty_collection_report(failed=failed),
        strategic=_empty_collection_report(failed=failed),
        duration_ms=round((monotonic() - started_at) * 1000, 3),
        error_code=error_code,
    )


def _unwritten_collection_report(
    prepared: _PreparedCollection,
    error_code: str,
) -> CollectionRebuildReport:
    expected = tuple(sorted(record["id"] for record in prepared.records))
    failed = set(prepared.failed)
    failed.update((record_id, error_code) for record_id in expected)
    return CollectionRebuildReport(
        source_ids=prepared.source_ids,
        expected_ids=expected,
        indexed_ids=(),
        permanently_skipped=prepared.permanently_skipped,
        failed=tuple(sorted(failed)),
        missing_ids=expected,
        unexpected_ids=(),
    )


def rebuild_chromadb_from_sqlite(db_handler=None) -> RebuildReport:
    """Recreate Chroma from SQLite and return exact typed coverage."""
    started_at = monotonic()
    handler = db_handler or memory_manager.db_handler
    if not handler.chroma_client or not handler.embedding_model:
        return _failed_report(started_at, "CHROMA_BACKEND_UNAVAILABLE")

    with _REBUILD_LOCK, _handler_lock_cm(handler):
        return _rebuild_chromadb_from_sqlite_locked(handler, started_at)


def _rebuild_chromadb_from_sqlite_locked(handler, started_at: float) -> RebuildReport:
    try:
        chroma_metric = _resolve_rebuild_chroma_metric(handler)
        tactical_prepared = _prepare_tactical_memory(handler)
        strategic_prepared = _prepare_strategic_memory(handler)
    except Exception:
        return _failed_report(started_at, "REBUILD_PREFLIGHT_FAILED")

    try:
        for collection_name in ("strategic_memory", "tactical_memory"):
            try:
                handler.chroma_client.delete_collection(collection_name)
            except Exception:
                pass
        handler.strategic_collection = handler.chroma_client.get_or_create_collection(
            name="strategic_memory",
            metadata=_collection_metadata(
                handler,
                "High-level goals and context",
                chroma_metric,
            ),
        )
        handler.tactical_collection = handler.chroma_client.get_or_create_collection(
            name="tactical_memory",
            metadata=_collection_metadata(
                handler,
                "Detailed step-by-step agent experience",
                chroma_metric,
            ),
        )
    except Exception:
        if hasattr(handler, "embedding_metadata_mismatch"):
            handler.embedding_metadata_mismatch = True
        return RebuildReport(
            status=RebuildStatus.FAILED,
            tactical=_unwritten_collection_report(
                tactical_prepared,
                "COLLECTION_RECREATE_FAILED",
            ),
            strategic=_unwritten_collection_report(
                strategic_prepared,
                "COLLECTION_RECREATE_FAILED",
            ),
            duration_ms=round((monotonic() - started_at) * 1000, 3),
            error_code="COLLECTION_RECREATE_FAILED",
        )

    # Collections were recreated successfully against the current embedding
    # model/dimension, so the mismatch flag is cleared now regardless of any
    # row-level write failures handled below.
    if hasattr(handler, "embedding_metadata_mismatch"):
        handler.embedding_metadata_mismatch = False

    tactical = _rebuild_collection(
        handler.tactical_collection,
        tactical_prepared,
    )
    strategic = _rebuild_collection(
        handler.strategic_collection,
        strategic_prepared,
    )
    has_technical_failure = any(
        report.failed or report.missing_ids or report.unexpected_ids
        for report in (tactical, strategic)
    )
    status = RebuildStatus.DEGRADED if has_technical_failure else RebuildStatus.COMPLETE
    return RebuildReport(
        status=status,
        tactical=tactical,
        strategic=strategic,
        duration_ms=round((monotonic() - started_at) * 1000, 3),
        error_code=None,
    )


def _prepare_tactical_memory(handler) -> _PreparedCollection:
    from .index_consistency import (
        SEMANTIC_CACHE_KINDS,
        is_valid_semantic_id,
        semantic_payload_fingerprint,
    )

    conn = handler._get_connection()
    try:
        records = conn.execute(
            "SELECT session_id, agent_name, step, instance_step, run_id, data "
            "FROM agent_memory WHERE valid_to IS NULL ORDER BY session_id, agent_name, step"
        ).fetchall()
    finally:
        conn.close()

    prepared: dict[str, Dict[str, Any]] = {}
    fingerprints: dict[str, str] = {}
    source_ids: set[str] = set()
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for session_id, agent_name, step, instance_step, run_id, data_json in records:
        legacy_id = f"{session_id}-{agent_name}-{step}"
        try:
            data_dict = json.loads(data_json)
        except (TypeError, json.JSONDecodeError):
            source_ids.add(legacy_id)
            failed.append((legacy_id, "MALFORMED_JSON"))
            continue
        if not isinstance(data_dict, dict):
            source_ids.add(legacy_id)
            failed.append((legacy_id, "INVALID_PAYLOAD"))
            continue
        cache_kind = data_dict.get("cache_kind")
        semantic_source = data_dict.get("semantic_id") is not None
        if semantic_source:
            semantic_id = data_dict.get("semantic_id")
            if cache_kind not in SEMANTIC_CACHE_KINDS or not is_valid_semantic_id(
                cache_kind,
                semantic_id,
            ):
                source_ids.add(legacy_id)
                failed.append((legacy_id, "INVALID_SEMANTIC_ID"))
                continue
            tactical_id = semantic_id
        elif cache_kind == "successful_sql_example":
            source_ids.add(legacy_id)
            failed.append((legacy_id, "INVALID_SEMANTIC_ID"))
            continue
        else:
            tactical_id = legacy_id
        source_ids.add(tactical_id)

        text_content = _extract_tactical_chroma_text(data_dict)
        if not text_content or len(text_content.strip()) < 10:
            skipped.append((tactical_id, "CONTENT_TOO_SHORT"))
            continue
        try:
            embedding = memory_manager._create_embedding(
                text_content,
                purpose="passage",
                allow_metadata_mismatch=True,
            )
        except Exception:
            failed.append((tactical_id, "EMBEDDING_FAILED"))
            continue
        if not embedding:
            failed.append((tactical_id, "EMBEDDING_UNAVAILABLE"))
            continue

        metadata = {
            "session_id": session_id,
            "agent_name": agent_name,
            "step": step,
            "tactical_id": tactical_id,
        }
        for field in (
            "cache_kind", "cache_key", "cache_source", "schema_version",
            "filename", "table_fqn", "auto_loaded", "source",
            "artifact_type", "file_hash", "topic", "category", "tags",
            "is_global", "saved_by", "saved_at", "memory_source", "semantic_id",
            "dialect",
        ):
            if data_dict.get(field) is not None:
                metadata[field] = str(data_dict[field])
        if run_id:
            metadata["run_id"] = str(run_id)
        if instance_step is not None:
            metadata["instance_step"] = int(instance_step)
        record = {
            "embedding": embedding,
            "document": text_content,
            "metadata": metadata,
            "id": tactical_id,
        }
        fingerprint = (
            semantic_payload_fingerprint(data_dict)
            if semantic_source
            else json.dumps(
                {"document": text_content, "metadata": metadata},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if tactical_id in fingerprints and fingerprints[tactical_id] != fingerprint:
            prepared.pop(tactical_id, None)
            failed.append((tactical_id, "CONFLICTING_SOURCE_ROWS"))
            continue
        if not any(item_id == tactical_id for item_id, _ in failed):
            fingerprints[tactical_id] = fingerprint
            prepared.setdefault(tactical_id, record)
    permanently_skipped = tuple(
        sorted(
            set(skipped)
            | {item for item in failed if item[1] in _PERMANENTLY_SKIPPED_REASONS}
        )
    )
    failed = tuple(
        sorted({item for item in failed if item[1] not in _PERMANENTLY_SKIPPED_REASONS})
    )
    return _PreparedCollection(
        records=tuple(prepared[key] for key in sorted(prepared)),
        source_ids=tuple(sorted(source_ids)),
        permanently_skipped=permanently_skipped,
        failed=failed,
    )


def _prepare_strategic_memory(handler) -> _PreparedCollection:
    conn = handler._get_connection()
    try:
        records = conn.execute(
            "SELECT memory_id, session_id, type, content FROM strategic_memory "
            "WHERE valid_to IS NULL ORDER BY memory_id"
        ).fetchall()
    finally:
        conn.close()

    prepared: list[Dict[str, Any]] = []
    source_ids: list[str] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for memory_id, session_id, memory_type, content in records:
        record_id = str(memory_id)
        source_ids.append(record_id)
        if not isinstance(content, str) or len(content.strip()) < 10:
            skipped.append((record_id, "CONTENT_TOO_SHORT"))
            continue
        try:
            embedding = memory_manager._create_embedding(
                content,
                purpose="passage",
                allow_metadata_mismatch=True,
            )
        except Exception:
            failed.append((record_id, "EMBEDDING_FAILED"))
            continue
        if not embedding:
            failed.append((record_id, "EMBEDDING_UNAVAILABLE"))
            continue
        prepared.append(
            {
                "embedding": embedding,
                "document": content,
                "metadata": {
                    "session_id": session_id,
                    "type": memory_type,
                    "memory_id": memory_id,
                },
                "id": record_id,
            }
        )
    return _PreparedCollection(
        records=tuple(prepared),
        source_ids=tuple(sorted(set(source_ids))),
        permanently_skipped=tuple(sorted(set(skipped))),
        failed=tuple(sorted(set(failed))),
    )


def _collection_ids(collection) -> set[str]:
    result = collection.get()
    if not isinstance(result, dict):
        raise TypeError("Chroma get() must return an object")
    raw_ids = result.get("ids") or []
    if raw_ids and isinstance(raw_ids[0], list):
        raw_ids = raw_ids[0]
    return {value for value in raw_ids if isinstance(value, str)}


def _rebuild_collection(
    collection,
    prepared: _PreparedCollection,
) -> CollectionRebuildReport:
    expected = {record["id"] for record in prepared.records}
    failed = list(prepared.failed)
    for record in prepared.records:
        try:
            collection.upsert(
                embeddings=[record["embedding"]],
                documents=[record["document"]],
                metadatas=[record["metadata"]],
                ids=[record["id"]],
            )
        except Exception:
            failed.append((record["id"], "CHROMA_UPSERT_FAILED"))
    try:
        actual = _collection_ids(collection)
    except Exception:
        actual = set()
        failed.append(("__reconcile__", "CHROMA_ID_READ_FAILED"))
    return CollectionRebuildReport(
        source_ids=prepared.source_ids,
        expected_ids=tuple(sorted(expected)),
        indexed_ids=tuple(sorted(expected & actual)),
        permanently_skipped=tuple(sorted(set(prepared.permanently_skipped))),
        failed=tuple(sorted(set(failed))),
        missing_ids=tuple(sorted(expected - actual)),
        unexpected_ids=tuple(sorted(actual - expected)),
    )


@tool
def rebuild_chromadb_tool() -> str:
    """Инструмент для пересборки ChromaDB из SQLite данных.
    
    Административный инструмент для восстановления векторной базы данных.
    Полностью очищает ChromaDB и восстанавливает все данные из SQLite.
    
    Returns:
        str: Статус операции и статистика восстановления
    """
    try:
        print("🔄 Начинаем пересоздание ChromaDB из SQLite...")
        report = rebuild_chromadb_from_sqlite()
        return format_rebuild_report(report)
        
    except Exception as e:
        error_msg = f"❌ Ошибка пересоздания ChromaDB: {str(e)}"
        print(error_msg)
        return error_msg


def format_rebuild_report(report: RebuildReport) -> str:
    """Format a typed report only for human-facing compatibility adapters."""
    return (
        f"status={report.status.value}; "
        f"tactical={len(report.tactical.indexed_ids)}/{len(report.tactical.expected_ids)}; "
        f"strategic={len(report.strategic.indexed_ids)}/{len(report.strategic.expected_ids)}; "
        f"skipped={len(report.tactical.permanently_skipped) + len(report.strategic.permanently_skipped)}; "
        f"failed={len(report.tactical.failed) + len(report.strategic.failed)}; "
        f"duration_ms={report.duration_ms}"
    )


# Обновляем метод в memory_manager для использования новой логики
def patch_memory_manager():
    """Обновляет методы memory_manager для использования новой логики пересборки"""
    def new_rebuild_method(self):
        """Новый метод пересборки для MemoryManager"""
        return rebuild_chromadb_from_sqlite(self.db_handler)
    
    # Заменяем метод
    memory_manager.rebuild_chromadb_from_sqlite = new_rebuild_method.__get__(memory_manager, memory_manager.__class__)


# Применяем патч при импорте
patch_memory_manager()
