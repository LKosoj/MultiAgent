"""Exact-ID reconciliation for semantic tactical-memory records."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .chroma_text import extract_tactical_chroma_text
from .manager import memory_manager


SEMANTIC_CACHE_KINDS = frozenset({"schema_table", "successful_sql_example"})
_SEMANTIC_ID_PATTERNS = {
    "schema_table": re.compile(r"^text2sql-schema-v1-[0-9a-f]{64}$"),
    "successful_sql_example": re.compile(r"^text2sql-example-v1-[0-9a-f]{64}$"),
}
_ADVISORY_FIELDS = frozenset(
    {"needs_reindex", "reindex_error", "reindex_updated_at"}
)


class SemanticIndexStatus(str, Enum):
    INDEXED = "indexed"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True)
class SemanticIndexReceipt:
    semantic_id: str
    sqlite_step: int | None
    status: SemanticIndexStatus
    error_code: str | None = None


@dataclass(frozen=True)
class NamespaceReconciliation:
    expected_ids: tuple[str, ...]
    indexed_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    unexpected_ids: tuple[str, ...]
    failed_ids: tuple[str, ...]

    @property
    def exact(self) -> bool:
        return not (self.missing_ids or self.unexpected_ids or self.failed_ids)


@dataclass(frozen=True)
class _SourceRow:
    step: int
    run_id: str | None
    instance_step: int | None
    data: dict[str, Any]


def is_valid_semantic_id(cache_kind: str, semantic_id: object) -> bool:
    pattern = _SEMANTIC_ID_PATTERNS.get(cache_kind)
    return bool(pattern and isinstance(semantic_id, str) and pattern.fullmatch(semantic_id))


def validate_semantic_payload(data: dict[str, Any]) -> bool:
    cache_kind = data.get("cache_kind")
    semantic_id = data.get("semantic_id")
    if semantic_id is None:
        return cache_kind != "successful_sql_example"
    return isinstance(cache_kind, str) and is_valid_semantic_id(cache_kind, semantic_id)


def _source_rows(
    *,
    session_id: str,
    agent_name: str,
    cache_kind: str,
) -> tuple[list[_SourceRow], set[str]]:
    handler = memory_manager.db_handler
    conn = handler._get_connection()
    malformed: set[str] = set()
    rows: list[_SourceRow] = []
    try:
        result = conn.execute(
            """
            SELECT step, instance_step, run_id, data
            FROM agent_memory
            WHERE session_id = ? AND agent_name = ? AND valid_to IS NULL
              AND data LIKE ?
            ORDER BY step
            """,
            (
                session_id,
                agent_name,
                f'%"cache_kind":"{cache_kind}"%',
            ),
        ).fetchall()
        for step, instance_step, run_id, data_json in result:
            try:
                data = json.loads(data_json)
            except (TypeError, json.JSONDecodeError):
                malformed.add(f"source-step-{step}")
                continue
            if not isinstance(data, dict) or data.get("cache_kind") != cache_kind:
                continue
            rows.append(
                _SourceRow(
                    step=int(step),
                    instance_step=instance_step,
                    run_id=run_id,
                    data=data,
                )
            )
    finally:
        conn.close()
    return rows, malformed


def semantic_payload_fingerprint(data: dict[str, Any]) -> str:
    ignored = set(_ADVISORY_FIELDS)
    if data.get("cache_kind") == "successful_sql_example":
        ignored.update({"run_id", "saved_at"})
    stable = {key: value for key, value in data.items() if key not in ignored}
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _group_source_rows(
    rows: list[_SourceRow],
    cache_kind: str,
) -> tuple[dict[str, _SourceRow], set[str]]:
    grouped: dict[str, _SourceRow] = {}
    fingerprints: dict[str, str] = {}
    failed: set[str] = set()
    for row in rows:
        semantic_id = row.data.get("semantic_id")
        if not is_valid_semantic_id(cache_kind, semantic_id):
            failed.add(
                semantic_id if isinstance(semantic_id, str) and semantic_id else f"source-step-{row.step}"
            )
            continue
        fingerprint = semantic_payload_fingerprint(row.data)
        if semantic_id in fingerprints and fingerprints[semantic_id] != fingerprint:
            failed.add(semantic_id)
            grouped.pop(semantic_id, None)
            continue
        if semantic_id not in failed:
            fingerprints[semantic_id] = fingerprint
            grouped.setdefault(semantic_id, row)
    return grouped, failed


def source_namespace_semantic_ids(
    *,
    session_id: str,
    agent_name: str,
    cache_kind: str,
) -> tuple[set[str], set[str]]:
    """Возвращает валидные semantic ID из SQLite, не обращаясь к Chroma.

    Этот узкий helper нужен проверкам, в которых SQLite остаётся источником
    истины, а векторный индекс временно недоступен или пересобирается.
    """
    if cache_kind not in SEMANTIC_CACHE_KINDS:
        raise ValueError("cache_kind is not a supported semantic record kind")
    rows, malformed = _source_rows(
        session_id=session_id,
        agent_name=agent_name,
        cache_kind=cache_kind,
    )
    grouped, failed = _group_source_rows(rows, cache_kind)
    failed.update(malformed)
    return set(grouped), failed


def _where_filter(session_id: str, agent_name: str, cache_kind: str) -> dict[str, Any]:
    return {
        "$and": [
            {"session_id": {"$eq": session_id}},
            {"agent_name": {"$eq": agent_name}},
            {"cache_kind": {"$eq": cache_kind}},
        ]
    }


def _flatten_ids(raw_ids: object) -> set[str]:
    if not isinstance(raw_ids, list):
        return set()
    if raw_ids and isinstance(raw_ids[0], list):
        raw_ids = raw_ids[0]
    return {value for value in raw_ids if isinstance(value, str)}


def _indexed_ids(collection, where: dict[str, Any]) -> set[str]:
    result = collection.get(where=where)
    if not isinstance(result, dict):
        raise TypeError("Chroma get() must return an object")
    return _flatten_ids(result.get("ids"))


def _record_metadata(
    *,
    session_id: str,
    agent_name: str,
    row: _SourceRow,
) -> dict[str, Any]:
    semantic_id = str(row.data["semantic_id"])
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "agent_name": agent_name,
        "step": row.step,
        "tactical_id": semantic_id,
        "semantic_id": semantic_id,
    }
    for field in (
        "cache_kind",
        "cache_key",
        "cache_source",
        "schema_version",
        "filename",
        "table_fqn",
        "artifact_type",
        "saved_at",
        "dialect",
    ):
        if row.data.get(field) is not None:
            metadata[field] = str(row.data[field])
    if row.run_id:
        metadata["run_id"] = str(row.run_id)
    if row.instance_step is not None:
        metadata["instance_step"] = int(row.instance_step)
    return metadata


def _repair_row(
    *,
    collection,
    session_id: str,
    agent_name: str,
    row: _SourceRow,
) -> None:
    text = extract_tactical_chroma_text(
        row.data,
        fallback_extractor=memory_manager._extract_text_content,
    )
    if not isinstance(text, str) or not text.strip():
        raise ValueError("semantic source row has no indexable text")
    embedding = memory_manager._create_embedding(text, purpose="passage")
    if not embedding:
        raise RuntimeError("semantic source row embedding is unavailable")
    semantic_id = str(row.data["semantic_id"])
    collection.upsert(
        embeddings=[embedding],
        documents=[text],
        metadatas=[
            _record_metadata(
                session_id=session_id,
                agent_name=agent_name,
                row=row,
            )
        ],
        ids=[semantic_id],
    )


def _clear_reindex_markers(
    rows: list[_SourceRow],
    observed_ids: set[str],
    *,
    session_id: str,
    agent_name: str,
) -> set[str]:
    updates: list[tuple[str, str, str, str, int]] = []
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        semantic_id = row.data.get("semantic_id")
        if semantic_id not in observed_ids or not _ADVISORY_FIELDS.intersection(row.data):
            continue
        cleaned = {key: value for key, value in row.data.items() if key not in _ADVISORY_FIELDS}
        updates.append(
            (
                json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                now,
                session_id,
                agent_name,
                row.step,
            )
        )
    if not updates:
        return set()

    handler = memory_manager.db_handler
    conn = handler._get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for data_json, updated_at, row_session_id, row_agent_name, step in updates:
            conn.execute(
                """
                UPDATE agent_memory
                SET data = ?, updated_at = ?
                WHERE session_id = ? AND agent_name = ? AND step = ?
                  AND valid_to IS NULL
                """,
                (data_json, updated_at, row_session_id, row_agent_name, step),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        return {
            str(row.data["semantic_id"])
            for row in rows
            if row.data.get("semantic_id") in observed_ids
        }
    finally:
        conn.close()
    return set()


def reconcile_tactical_namespace(
    *,
    session_id: str,
    agent_name: str,
    cache_kind: str,
    repair_missing: bool,
) -> NamespaceReconciliation:
    """Compare active semantic source rows with exact Chroma IDs and repair gaps."""
    if cache_kind not in SEMANTIC_CACHE_KINDS:
        raise ValueError("cache_kind is not a supported semantic record kind")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    if not isinstance(agent_name, str) or not agent_name:
        raise ValueError("agent_name is required")

    rows, malformed = _source_rows(
        session_id=session_id,
        agent_name=agent_name,
        cache_kind=cache_kind,
    )
    grouped, failed = _group_source_rows(rows, cache_kind)
    failed.update(malformed)
    expected = set(grouped)

    handler = memory_manager.db_handler
    collection = getattr(handler, "tactical_collection", None)
    if collection is None:
        return NamespaceReconciliation(
            expected_ids=tuple(sorted(expected)),
            indexed_ids=(),
            missing_ids=tuple(sorted(expected)),
            unexpected_ids=(),
            failed_ids=tuple(sorted(failed)),
        )

    where = _where_filter(session_id, agent_name, cache_kind)
    try:
        actual = _indexed_ids(collection, where)
    except Exception:
        return NamespaceReconciliation(
            expected_ids=tuple(sorted(expected)),
            indexed_ids=(),
            missing_ids=tuple(sorted(expected)),
            unexpected_ids=(),
            failed_ids=tuple(sorted(failed | expected)),
        )

    missing = expected - actual
    if repair_missing:
        for semantic_id in sorted(missing):
            try:
                _repair_row(
                    collection=collection,
                    session_id=session_id,
                    agent_name=agent_name,
                    row=grouped[semantic_id],
                )
            except Exception:
                failed.add(semantic_id)
        try:
            actual = _indexed_ids(collection, where)
        except Exception:
            failed.update(expected)
            actual = set()

    marker_failures = _clear_reindex_markers(
        rows,
        expected & actual,
        session_id=session_id,
        agent_name=agent_name,
    )
    failed.update(marker_failures)
    return NamespaceReconciliation(
        expected_ids=tuple(sorted(expected)),
        indexed_ids=tuple(sorted(expected & actual)),
        missing_ids=tuple(sorted(expected - actual)),
        unexpected_ids=tuple(sorted(actual - expected)),
        failed_ids=tuple(sorted(failed)),
    )
