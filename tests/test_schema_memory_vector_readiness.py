"""Регрессии: SQLite-схема остаётся доступной без готового Chroma."""
from __future__ import annotations

import contextlib
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.schema_memory import SchemaCacheManager, SchemaMemoryManager
from custom_tools.text_to_sql.schema_namespace import SchemaNamespace, SchemaScope
from custom_tools.text_to_sql.successful_sql_memory import canonical_schema_table_id
from memory.database import DatabaseHandler, VectorReadiness
from memory.rebuild import (
    CollectionRebuildReport,
    RebuildReport,
    RebuildStatus,
    rebuild_chromadb_from_sqlite,
)


def _namespace() -> SchemaNamespace:
    return SchemaNamespace(
        scope=SchemaScope.from_mapping(
            {
                "serialization_version": 1,
                "tenant_id": "tenant",
                "access_scope_id": "scope",
                "connection_view_id": "view",
                "transient": False,
            }
        ),
        schema_fingerprint="a" * 64,
    )


def _save_schema_row(handler, *, session_id, agent_name, data):
    conn = handler.get_connection()
    try:
        step = conn.execute(
            "SELECT COALESCE(MAX(step), 0) + 1 FROM agent_memory "
            "WHERE session_id = ? AND agent_name = ?",
            (session_id, agent_name),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            (session_id, agent_name, step, json.dumps(data, separators=(",", ":"))),
        )
        conn.commit()
        return step
    finally:
        conn.close()


def _disabled_handler(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_CHROMA_DISABLED", "1")
    return DatabaseHandler(
        db_path=str(tmp_path / "memory.sqlite"),
        chroma_path=str(tmp_path / "chroma"),
    )


def test_sqlite_starts_when_chroma_is_explicitly_disabled(monkeypatch, tmp_path):
    handler = _disabled_handler(monkeypatch, tmp_path)

    assert handler.vector_readiness is VectorReadiness.DISABLED
    conn = handler.get_connection()
    try:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_sqlite_starts_when_chroma_constructor_fails(monkeypatch, tmp_path):
    from memory import database as database_module

    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://gateway.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "test-key")
    monkeypatch.setattr(
        database_module,
        "OpenAI",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("gateway unavailable")),
    )
    handler = database_module.DatabaseHandler(
        db_path=str(tmp_path / "memory.sqlite"),
        chroma_path=str(tmp_path / "chroma"),
    )

    assert handler.vector_readiness is VectorReadiness.UNAVAILABLE
    conn = handler.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone() == (0,)
    finally:
        conn.close()


def test_corrupt_chroma_metadata_is_stale_not_ready(monkeypatch, tmp_path):
    from memory import database as database_module

    class FakeClient:
        def get_or_create_collection(self, *_args, **_kwargs):
            return SimpleNamespace(metadata=object())

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.embeddings = SimpleNamespace(
                create=lambda **_kwargs: SimpleNamespace(
                    model="gateway-embedding-v1",
                    data=[SimpleNamespace(embedding=[0.0] * 5)],
                )
            )

    monkeypatch.setenv("OPENAI_API_BASE_DB", "http://gateway.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY_DB", "test-key")
    monkeypatch.setattr(database_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        database_module.chromadb,
        "PersistentClient",
        lambda *_args, **_kwargs: FakeClient(),
    )
    handler = database_module.DatabaseHandler(
        db_path=str(tmp_path / "memory.sqlite"),
        chroma_path=str(tmp_path / "chroma"),
    )

    assert handler.embedding_metadata_mismatch is True
    assert handler.vector_readiness is VectorReadiness.STALE


def test_schema_rows_remain_authoritative_for_every_non_ready_vector_state(
    monkeypatch, tmp_path
):
    import memory.index_consistency as consistency_module
    import memory.manager as manager_module

    handler = _disabled_handler(monkeypatch, tmp_path)
    chroma_probe_calls = 0
    reject_chroma_probe = False

    def broken_chroma_probe():
        nonlocal chroma_probe_calls
        chroma_probe_calls += 1
        if not reject_chroma_probe:
            return None
        raise AssertionError("non-ready schema checks must not touch Chroma")

    fake_memory_manager = SimpleNamespace(
        db_handler=handler,
        get_sqlite_connection=handler.get_connection,
        get_tactical_collection=broken_chroma_probe,
        _extract_text_content=lambda value: str(value),
    )
    monkeypatch.setattr(manager_module, "memory_manager", fake_memory_manager)
    monkeypatch.setattr(consistency_module, "memory_manager", fake_memory_manager)
    monkeypatch.setitem(
        sys.modules,
        "memory.tools",
        SimpleNamespace(
            save_memory=lambda **kwargs: _save_schema_row(handler, **kwargs),
            get_memory=lambda **_kwargs: [],
            memory_requester_context=lambda _name: contextlib.nullcontext(),
        ),
    )

    namespace = _namespace()
    schema = {"orders": {"columns": {"id": {"type": "INTEGER"}}}}
    manager = SchemaMemoryManager(tmp_path)

    assert manager.ensure_schema_indexed_in_memory(namespace, schema) is True
    conn = handler.get_connection()
    try:
        stored = json.loads(
            conn.execute(
                "SELECT data FROM agent_memory WHERE session_id = ?",
                (namespace.version_key,),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    file_hash = stored["file_hash"]
    reject_chroma_probe = True
    chroma_probe_calls = 0
    for readiness in (
        VectorReadiness.DISABLED,
        VectorReadiness.UNAVAILABLE,
        VectorReadiness.STALE,
        VectorReadiness.REBUILDING,
    ):
        handler.set_vector_readiness(readiness, "test")
        assert manager.is_schema_indexed(
            namespace,
            file_hash,
            expected_count=1,
            expected_table_keys=("orders",),
        ) is True
        assert manager.last_vector_readiness == readiness.value
    assert chroma_probe_calls == 0

    assert manager.find_semantic_relevant_tables(["orders"], namespace=namespace) == []
    assert manager.last_search_status == "sqlite_lexical_no_hits"
    assert chroma_probe_calls == 0


def test_non_ready_vector_uses_sqlite_lexical_schema_search(monkeypatch, tmp_path):
    import memory.manager as manager_module

    handler = _disabled_handler(monkeypatch, tmp_path)
    namespace = _namespace()
    _save_schema_row(
        handler,
        session_id=namespace.version_key,
        agent_name="Schema-RAG-Agent",
        data={
            "cache_kind": "schema_table",
            "table_fqn": "sales.orders",
            "table_name": "orders",
            "description": "Customer purchase orders",
            "table_info": {
                "table_name": "sales.orders",
                "description": "Customer purchase orders",
                "columns": [
                    {"name": "order_id", "description": "Order identifier"},
                ],
            },
        },
    )
    monkeypatch.setattr(
        manager_module,
        "memory_manager",
        SimpleNamespace(
            db_handler=handler,
            get_sqlite_connection=handler.get_connection,
            get_tactical_collection=lambda: (_ for _ in ()).throw(
                AssertionError("non-ready lexical search must not touch Chroma")
            ),
        ),
    )

    def get_memory(*, session_id, query=None, **_kwargs):
        assert session_id == namespace.version_key
        assert query is None
        conn = handler.get_connection()
        try:
            rows = conn.execute(
                "SELECT agent_name, step, data FROM agent_memory "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"agent_name": agent_name, "step": step, "data": json.loads(data)}
            for agent_name, step, data in rows
        ]

    monkeypatch.setitem(
        sys.modules,
        "memory.tools",
        SimpleNamespace(
            get_memory=get_memory,
            memory_requester_context=lambda _name: contextlib.nullcontext(),
        ),
    )

    manager = SchemaMemoryManager(tmp_path)

    assert manager.find_semantic_relevant_tables(["orders"], namespace=namespace) == [
        "sales.orders"
    ]
    assert manager.last_search_status == "sqlite_lexical_ok"


def test_vector_readiness_probe_errors_fall_back_to_sqlite(monkeypatch, tmp_path):
    import memory.index_consistency as consistency_module
    import memory.manager as manager_module

    handler = _disabled_handler(monkeypatch, tmp_path)
    namespace = _namespace()
    _save_schema_row(
        handler,
        session_id=namespace.version_key,
        agent_name="Schema-RAG-Agent",
        data={
            "cache_kind": "schema_table",
            "semantic_id": canonical_schema_table_id(namespace.version_key, "orders"),
            "file_hash": "content-hash",
        },
    )

    class BrokenReadinessHandler:
        @property
        def vector_readiness(self):
            raise RuntimeError("readiness getter failed")

    manager = SchemaMemoryManager(tmp_path)
    broken_readiness_manager = SimpleNamespace(
        db_handler=BrokenReadinessHandler(),
        get_sqlite_connection=handler.get_connection,
        get_tactical_collection=lambda: (_ for _ in ()).throw(RuntimeError("must not run")),
    )
    monkeypatch.setattr(manager_module, "memory_manager", broken_readiness_manager)
    monkeypatch.setattr(consistency_module, "memory_manager", broken_readiness_manager)

    assert manager._observe_vector_readiness(broken_readiness_manager) == "unavailable"
    assert manager.last_vector_reason == "vector_readiness_probe_failed"

    class NoStateHandler:
        embedding_metadata_mismatch = False

        def _get_connection(self):
            return handler._get_connection()

    broken_collection_manager = SimpleNamespace(
        db_handler=NoStateHandler(),
        get_sqlite_connection=handler.get_connection,
        get_tactical_collection=lambda: (_ for _ in ()).throw(RuntimeError("collection failed")),
    )
    monkeypatch.setattr(manager_module, "memory_manager", broken_collection_manager)
    monkeypatch.setattr(consistency_module, "memory_manager", broken_collection_manager)

    assert manager._observe_vector_readiness(broken_collection_manager) == "stale"
    assert manager.last_vector_reason == "tactical_collection_probe_failed"
    assert manager.is_schema_indexed(
        namespace,
        "content-hash",
        expected_count=1,
        expected_table_keys=("orders",),
    ) is True


def test_search_failure_marks_vector_stale_then_sqlite_check_skips_chroma(
    monkeypatch, tmp_path
):
    import memory.index_consistency as consistency_module
    import memory.manager as manager_module

    handler = _disabled_handler(monkeypatch, tmp_path)
    handler.set_vector_readiness(VectorReadiness.READY, "test")
    namespace = _namespace()
    semantic_id = canonical_schema_table_id(namespace.version_key, "orders")
    _save_schema_row(
        handler,
        session_id=namespace.version_key,
        agent_name="Schema-RAG-Agent",
        data={
            "cache_kind": "schema_table",
            "semantic_id": semantic_id,
            "file_hash": "content-hash",
        },
    )

    class BrokenCollection:
        metadata = {"hnsw:space": "l2"}
        configuration = None

        def get(self, **_kwargs):
            raise AssertionError("stale SQLite check must not read Chroma")

    collection = BrokenCollection()
    fake_memory_manager = SimpleNamespace(
        db_handler=handler,
        get_sqlite_connection=handler.get_connection,
        get_tactical_collection=lambda: collection,
        search_semantic_with_scores=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Chroma query failed")
        ),
    )
    monkeypatch.setattr(manager_module, "memory_manager", fake_memory_manager)
    monkeypatch.setattr(consistency_module, "memory_manager", fake_memory_manager)

    manager = SchemaMemoryManager(tmp_path)
    assert manager.find_semantic_relevant_tables(["orders"], dsn="sqlite:///tmp/t.db") == []
    assert handler.vector_readiness is VectorReadiness.STALE
    assert manager.last_search_status == "vector_stale"
    assert manager.is_schema_indexed(
        namespace,
        "content-hash",
        expected_count=1,
        expected_table_keys=("orders",),
    ) is True


def test_schema_cache_hit_does_not_depend_on_vector_readiness(monkeypatch):
    import memory.manager as manager_module

    cache = SchemaCacheManager()
    info = cache.prepare_cache_info(
        {"entities": ["orders"]},
        {"orders": {"columns": {"id": {"type": "INTEGER"}}}},
        session_id="sqlite-authoritative",
    )
    cached = {"linked_tables": ["orders"]}
    monkeypatch.setattr(manager_module, "memory_manager", object())
    monkeypatch.setitem(
        sys.modules,
        "memory.tools",
        SimpleNamespace(
            get_memory=lambda **_kwargs: [
                {
                    "data": {
                        "cache_key": info["cache_key"],
                        "schema_version": info["schema_version"],
                        "env_fingerprint": info["env_fingerprint"],
                        "linking_result": cached,
                    }
                }
            ],
            memory_requester_context=lambda _name: contextlib.nullcontext(),
        ),
    )

    assert cache.load_from_cache(info) == cached


def test_scoped_schema_memory_rejects_changed_file_content(
    monkeypatch,
    tmp_path,
) -> None:
    import memory.index_consistency as consistency_module
    import memory.manager as manager_module

    namespace = _namespace()
    handler = _disabled_handler(monkeypatch, tmp_path)
    _save_schema_row(
        handler,
        session_id=namespace.version_key,
        agent_name="Schema-RAG-Agent",
        data={
            "cache_kind": "schema_table",
            "semantic_id": canonical_schema_table_id(
                namespace.version_key,
                "orders",
            ),
            "file_hash": "old-content-hash",
        },
    )
    fake_memory_manager = SimpleNamespace(db_handler=handler)
    monkeypatch.setattr(manager_module, "memory_manager", fake_memory_manager)
    monkeypatch.setattr(consistency_module, "memory_manager", fake_memory_manager)

    manager = SchemaMemoryManager(tmp_path)

    assert manager.is_schema_indexed(
        namespace,
        "new-content-hash",
        expected_count=1,
        expected_table_keys=("orders",),
    ) is False


def test_rebuild_marks_vector_stale_when_interrupted(monkeypatch):
    from memory import rebuild as rebuild_module

    handler = SimpleNamespace(
        chroma_client=object(),
        embedding_model=object(),
        lock=threading.RLock(),
        vector_readiness=VectorReadiness.READY,
    )
    monkeypatch.setattr(
        rebuild_module,
        "_rebuild_chromadb_from_sqlite_locked",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        rebuild_chromadb_from_sqlite(handler)

    assert handler.vector_readiness is VectorReadiness.STALE
    assert handler.vector_readiness_reason == "rebuild_interrupted"


def test_concurrent_rebuilds_finish_with_ready_vector_state(monkeypatch):
    from memory import rebuild as rebuild_module

    handler = SimpleNamespace(
        chroma_client=object(),
        embedding_model=object(),
        lock=threading.RLock(),
        vector_readiness=VectorReadiness.STALE,
    )
    reports = []

    def complete_report(*_args):
        empty = CollectionRebuildReport((), (), (), (), (), (), ())
        return RebuildReport(
            status=RebuildStatus.COMPLETE,
            tactical=empty,
            strategic=empty,
            duration_ms=0.0,
        )

    monkeypatch.setattr(
        rebuild_module,
        "_rebuild_chromadb_from_sqlite_locked",
        complete_report,
    )

    threads = [
        threading.Thread(target=lambda: reports.append(rebuild_chromadb_from_sqlite(handler)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [report.status for report in reports] == [RebuildStatus.COMPLETE] * 2
    assert handler.vector_readiness is VectorReadiness.READY
