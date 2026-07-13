"""Canonical successful-SQL memory writer and namespaced retriever."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from memory.index_consistency import (
    SemanticIndexReceipt,
    SemanticIndexStatus,
    reconcile_tactical_namespace,
)

logger = logging.getLogger(__name__)


SUCCESSFUL_SQL_AGENT = "Schema-RAG-Agent"
SUCCESSFUL_SQL_CACHE_KIND = "successful_sql_example"
SUCCESSFUL_SQL_TOP_K = 3
MAX_SUCCESSFUL_SQL_CONTEXT_BYTES = 8192
_MAX_CONTEXT_QUERY_CHARS = 256
_MAX_CONTEXT_SQL_CHARS = 1200
_MAX_DIALECT_CHARS = 32
_NAMESPACE_RE = re.compile(r"^[0-9a-f]{64}$")
_DIALECT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class _SemanticSearchIncomplete(RuntimeError):
    pass


@dataclass(frozen=True)
class SuccessfulSqlExample:
    serialization_version: int
    example_id: str
    namespace_version_key: str
    user_query: str
    sql: str
    dialect: str
    run_id: str
    saved_at: str
    approved: bool
    executed: bool
    execution_success: bool
    audited: bool

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SuccessfulSqlExample":
        fields = {
            "serialization_version",
            "example_id",
            "namespace_version_key",
            "user_query",
            "sql",
            "dialect",
            "run_id",
            "saved_at",
            "approved",
            "executed",
            "execution_success",
            "audited",
        }
        if not fields.issubset(payload):
            raise ValueError("malformed successful SQL memory payload")
        example = cls(**{field: payload[field] for field in fields})
        example._validate()
        if payload.get("semantic_id") != example.example_id:
            raise ValueError("successful SQL semantic_id does not match example_id")
        return example

    def _validate(self) -> None:
        if self.serialization_version != 1:
            raise ValueError("unsupported successful SQL serialization_version")
        _validate_namespace_key(self.namespace_version_key)
        normalized_dialect = _normalize_dialect(self.dialect)
        expected_id = canonical_example_id(
            namespace_version_key=self.namespace_version_key,
            user_query=self.user_query,
            sql=self.sql,
            dialect=normalized_dialect,
        )
        if self.example_id != expected_id:
            raise ValueError("successful SQL example_id is not canonical")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id is required")
        if not isinstance(self.saved_at, str) or not self.saved_at.strip():
            raise ValueError("saved_at is required")
        if not all(
            value is True
            for value in (
                self.approved,
                self.executed,
                self.execution_success,
                self.audited,
            )
        ):
            raise ValueError("terminal success evidence must be exactly true")


@dataclass(frozen=True)
class SuccessfulSqlRetrieval:
    status: Literal["READY", "EMPTY", "DEGRADED"]
    examples: tuple[SuccessfulSqlExample, ...]
    context_json: str
    failed_ids: tuple[str, ...] = ()
    error_code: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "examples": [asdict(example) for example in self.examples],
            "context_json": self.context_json,
            "failed_ids": list(self.failed_ids),
            "error_code": self.error_code,
        }


def _validate_namespace_key(value: object) -> str:
    if not isinstance(value, str) or not _NAMESPACE_RE.fullmatch(value):
        raise ValueError("namespace_version_key must be a lowercase SHA-256 hex digest")
    return value


def validate_namespace_version_key(value: object) -> str:
    return _validate_namespace_key(value)


def _normalize_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


def _normalize_dialect(value: object) -> str:
    normalized = _normalize_text(value, "dialect").lower()
    if len(normalized) > _MAX_DIALECT_CHARS or not _DIALECT_RE.fullmatch(normalized):
        raise ValueError("dialect must be a lowercase identifier")
    return normalized


def _canonical_digest(payload: dict[str, str]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_example_id(
    *,
    namespace_version_key: str,
    user_query: str,
    sql: str,
    dialect: str,
) -> str:
    namespace_version_key = _validate_namespace_key(namespace_version_key)
    payload = {
        "namespace_version_key": namespace_version_key,
        "user_query": _normalize_text(user_query, "user_query"),
        "sql": _normalize_text(sql, "sql"),
        "dialect": _normalize_dialect(dialect),
    }
    return f"text2sql-example-v1-{_canonical_digest(payload)}"


def canonical_schema_table_id(namespace_version_key: str, table_fqn: str) -> str:
    namespace_version_key = _validate_namespace_key(namespace_version_key)
    table_key = _normalize_text(table_fqn, "table_fqn")
    return "text2sql-schema-v1-" + _canonical_digest(
        {
            "namespace_version_key": namespace_version_key,
            "table_fqn": table_key,
        }
    )


class SuccessfulSqlMemoryRepository:
    def __init__(self) -> None:
        from memory.tools import save_memory

        self._save_memory = save_memory
        self._reconcile = reconcile_tactical_namespace

    def save(
        self,
        *,
        namespace_version_key: str,
        user_query: str,
        sql: str,
        dialect: str,
        run_id: str,
        approved: bool,
        executed: bool,
        execution_success: bool,
        audited: bool,
        saved_at: str | None = None,
    ) -> SemanticIndexReceipt:
        if not all(
            value is True
            for value in (approved, executed, execution_success, audited)
        ):
            raise ValueError("terminal success evidence must be exactly true")

        normalized_query = _normalize_text(user_query, "user_query")
        normalized_sql = _normalize_text(sql, "sql")
        normalized_dialect = _normalize_dialect(dialect)
        namespace_version_key = _validate_namespace_key(namespace_version_key)
        example_id = canonical_example_id(
            namespace_version_key=namespace_version_key,
            user_query=normalized_query,
            sql=normalized_sql,
            dialect=normalized_dialect,
        )
        normalized_run_id = _normalize_text(run_id, "run_id")
        example = SuccessfulSqlExample(
            serialization_version=1,
            example_id=example_id,
            namespace_version_key=namespace_version_key,
            user_query=normalized_query,
            sql=normalized_sql,
            dialect=normalized_dialect,
            run_id=normalized_run_id,
            saved_at=saved_at or datetime.now(timezone.utc).isoformat(),
            approved=True,
            executed=True,
            execution_success=True,
            audited=True,
        )
        example._validate()
        payload = {
            **asdict(example),
            "semantic_id": example_id,
            "cache_kind": SUCCESSFUL_SQL_CACHE_KIND,
            "cache_source": "text_to_sql_terminal",
            "schema_version": namespace_version_key,
        }
        try:
            step = self._save_memory(
                session_id=namespace_version_key,
                agent_name=SUCCESSFUL_SQL_AGENT,
                data=payload,
                run_id=normalized_run_id,
            )
        except Exception:
            return SemanticIndexReceipt(
                semantic_id=example_id,
                sqlite_step=None,
                status=SemanticIndexStatus.FAILED,
                error_code="SQLITE_PERSISTENCE_FAILED",
            )
        if type(step) is not int or step < 0:
            return SemanticIndexReceipt(
                semantic_id=example_id,
                sqlite_step=None,
                status=SemanticIndexStatus.FAILED,
                error_code="SQLITE_PERSISTENCE_FAILED",
            )

        try:
            reconciliation = self._reconcile(
                session_id=namespace_version_key,
                agent_name=SUCCESSFUL_SQL_AGENT,
                cache_kind=SUCCESSFUL_SQL_CACHE_KIND,
                repair_missing=True,
            )
        except Exception:
            return SemanticIndexReceipt(
                semantic_id=example_id,
                sqlite_step=step,
                status=SemanticIndexStatus.DEGRADED,
                error_code="SEMANTIC_INDEX_UNAVAILABLE",
            )
        if reconciliation.exact and example_id in reconciliation.indexed_ids:
            return SemanticIndexReceipt(
                semantic_id=example_id,
                sqlite_step=step,
                status=SemanticIndexStatus.INDEXED,
            )
        return SemanticIndexReceipt(
            semantic_id=example_id,
            sqlite_step=step,
            status=SemanticIndexStatus.DEGRADED,
            error_code="SEMANTIC_INDEX_INCOMPLETE",
        )

    def retrieve(
        self,
        *,
        namespace_version_key: str,
        user_query: str,
        top_k: int = SUCCESSFUL_SQL_TOP_K,
    ) -> SuccessfulSqlRetrieval:
        namespace_version_key = _validate_namespace_key(namespace_version_key)
        user_query = _normalize_text(user_query, "user_query")
        if type(top_k) is not int or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        limit = min(top_k, SUCCESSFUL_SQL_TOP_K)

        try:
            reconciliation = self._reconcile(
                session_id=namespace_version_key,
                agent_name=SUCCESSFUL_SQL_AGENT,
                cache_kind=SUCCESSFUL_SQL_CACHE_KIND,
                repair_missing=True,
            )
        except Exception:
            return _degraded("SEMANTIC_INDEX_UNAVAILABLE")
        if not reconciliation.exact:
            failed = tuple(
                sorted(
                    set(reconciliation.failed_ids)
                    | set(reconciliation.missing_ids)
                    | set(reconciliation.unexpected_ids)
                )
            )
            return _degraded("SEMANTIC_INDEX_INCOMPLETE", failed)
        if not reconciliation.expected_ids:
            return SuccessfulSqlRetrieval("EMPTY", (), "[]")

        try:
            payloads = self._load_active_payloads(
                namespace_version_key=namespace_version_key
            )
            examples_by_id: dict[str, SuccessfulSqlExample] = {}
            for payload in payloads:
                try:
                    example = SuccessfulSqlExample.from_payload(payload)
                except (ValueError, TypeError) as e:
                    logger.warning(
                        "Skipping malformed successful-SQL memory row (namespace=%s, semantic_id=%s): %s",
                        namespace_version_key,
                        payload.get("semantic_id"),
                        e,
                    )
                    continue
                examples_by_id[example.example_id] = example
            ranked_ids = self._query_ids(
                namespace_version_key=namespace_version_key,
                user_query=user_query,
                top_k=limit,
            )
        except _SemanticSearchIncomplete:
            return _degraded(
                "SEMANTIC_SEARCH_INCOMPLETE",
                reconciliation.expected_ids,
            )
        except Exception:
            return _degraded("SEMANTIC_SEARCH_UNAVAILABLE")

        active_ids = set(reconciliation.expected_ids)
        if not ranked_ids or any(example_id not in active_ids for example_id in ranked_ids):
            return _degraded(
                "SEMANTIC_SEARCH_INCOMPLETE",
                reconciliation.expected_ids,
            )
        selected = [
            examples_by_id[example_id]
            for example_id in ranked_ids
            if example_id in active_ids and example_id in examples_by_id
        ][:limit]
        if not selected:
            return _degraded(
                "SEMANTIC_SEARCH_INCOMPLETE",
                reconciliation.expected_ids,
            )
        context_json = _bounded_context(selected)
        return SuccessfulSqlRetrieval(
            status="READY",
            examples=tuple(selected),
            context_json=context_json,
        )

    def _load_active_payloads(
        self,
        *,
        namespace_version_key: str,
    ) -> list[dict[str, Any]]:
        from memory.manager import memory_manager

        conn = memory_manager.db_handler._get_connection()
        try:
            rows = conn.execute(
                """
                SELECT data FROM agent_memory
                WHERE session_id = ? AND agent_name = ? AND valid_to IS NULL
                ORDER BY step
                """,
                (namespace_version_key, SUCCESSFUL_SQL_AGENT),
            ).fetchall()
        finally:
            conn.close()
        payloads: list[dict[str, Any]] = []
        for (data_json,) in rows:
            try:
                payload = json.loads(data_json)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(
                    "Skipping malformed successful-SQL memory row JSON (namespace=%s): %s",
                    namespace_version_key,
                    e,
                )
                continue
            if isinstance(payload, dict) and payload.get("cache_kind") == SUCCESSFUL_SQL_CACHE_KIND:
                payloads.append(payload)
        return payloads

    def _query_ids(
        self,
        *,
        namespace_version_key: str,
        user_query: str,
        top_k: int,
    ) -> list[str]:
        from memory.manager import memory_manager

        collection = memory_manager.db_handler.tactical_collection
        if collection is None:
            raise RuntimeError("tactical collection is unavailable")
        embedding = memory_manager._create_embedding(user_query, purpose="query")
        if not embedding:
            raise RuntimeError("query embedding is unavailable")
        result = collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            where={
                "$and": [
                    {"session_id": {"$eq": namespace_version_key}},
                    {"agent_name": {"$eq": SUCCESSFUL_SQL_AGENT}},
                    {"cache_kind": {"$eq": SUCCESSFUL_SQL_CACHE_KIND}},
                ]
            },
        )
        if not isinstance(result, dict):
            raise TypeError("Chroma query() must return an object")
        if "ids" not in result or not isinstance(result["ids"], list):
            raise _SemanticSearchIncomplete("Chroma query() returned malformed ids")
        raw_ids = result["ids"]
        if raw_ids and isinstance(raw_ids[0], list):
            if len(raw_ids) != 1:
                raise _SemanticSearchIncomplete("Chroma query() returned an invalid batch")
            raw_ids = raw_ids[0]
        if not all(isinstance(value, str) for value in raw_ids):
            raise _SemanticSearchIncomplete("Chroma query() returned a non-string id")
        return raw_ids


def _bounded_context(
    examples: list[SuccessfulSqlExample],
) -> str:
    entries: list[dict[str, str]] = []
    for example in examples[:SUCCESSFUL_SQL_TOP_K]:
        entry = {
            "user_query": example.user_query[:_MAX_CONTEXT_QUERY_CHARS],
            "sql": example.sql[:_MAX_CONTEXT_SQL_CHARS],
            "dialect": example.dialect,
        }
        serialized = json.dumps(
            [*entries, entry],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(serialized.encode("utf-8")) > MAX_SUCCESSFUL_SQL_CONTEXT_BYTES:
            continue
        entries.append(entry)
    context_json = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return context_json


def _degraded(
    error_code: str,
    failed_ids: tuple[str, ...] = (),
) -> SuccessfulSqlRetrieval:
    return SuccessfulSqlRetrieval(
        status="DEGRADED",
        examples=(),
        context_json="[]",
        failed_ids=failed_ids,
        error_code=error_code,
    )


def save_successful_sql_example(**kwargs: Any) -> SemanticIndexReceipt:
    return SuccessfulSqlMemoryRepository().save(**kwargs)


def retrieve_successful_sql_examples(
    namespace_version_key: str,
    user_query: str,
) -> SuccessfulSqlRetrieval:
    return SuccessfulSqlMemoryRepository().retrieve(
        namespace_version_key=namespace_version_key,
        user_query=user_query,
    )


def successful_sql_retrieval(
    namespace_version_key: str,
    user_query: str,
) -> dict[str, Any]:
    """Retrieve bounded, exact-namespace successful SQL examples."""
    return retrieve_successful_sql_examples(
        namespace_version_key=namespace_version_key,
        user_query=user_query,
    ).to_mapping()
