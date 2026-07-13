import json
import sqlite3
from types import SimpleNamespace

import pytest

from memory.rebuild import (
    CollectionRebuildReport,
    RebuildReport,
    RebuildStatus,
    _PreparedCollection,
    _extract_tactical_chroma_text,
    _resolve_rebuild_chroma_metric,
    rebuild_chromadb_from_sqlite,
)
from memory.streamlit_api import MemoryRAGManager, MemoryStatus


def _prepared(*ids):
    return _PreparedCollection(
        records=tuple(
            {
                "id": record_id,
                "embedding": [0.1],
                "document": f"document for {record_id}",
                "metadata": {"source": "test"},
            }
            for record_id in ids
        ),
        source_ids=tuple(ids),
        permanently_skipped=(),
        failed=(),
    )


class _FakeCollection:
    def __init__(self, metadata, *, on_upsert=None, on_get=None):
        self.metadata = metadata
        self.configuration = None
        self.ids = set()
        self._on_upsert = on_upsert
        self._on_get = on_get

    def upsert(self, *, ids, **_kwargs):
        if self._on_upsert:
            self._on_upsert(ids)
        self.ids.update(ids)

    def get(self):
        if self._on_get:
            self._on_get()
        return {"ids": sorted(self.ids)}


def test_rebuild_metric_prefers_existing_collection_metadata(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_CHROMA_METRIC", "cosine")
    handler = SimpleNamespace(
        tactical_collection=SimpleNamespace(
            metadata={"hnsw:space": "l2"},
            configuration=None,
        ),
        strategic_collection=None,
    )

    assert _resolve_rebuild_chroma_metric(handler) == "l2"


def test_rebuild_metric_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_CHROMA_METRIC", "ip")
    handler = SimpleNamespace(tactical_collection=None, strategic_collection=None)

    assert _resolve_rebuild_chroma_metric(handler) == "ip"


def test_rebuild_tactical_text_matches_agent_step_live_write_semantics():
    payload = {
        "cache_kind": "agent_step",
        "agent_response": "  final answer text  ",
        "agent_context": "large hidden context",
        "timestamp": "2026-07-02T00:00:00",
    }

    assert _extract_tactical_chroma_text(payload) == "final answer text"


def test_rebuild_tactical_text_matches_schema_table_live_write_semantics():
    payload = {
        "cache_kind": "schema_table",
        "table_fqn": "public.orders",
        "description": "Orders table",
        "table_info": {
            "columns": [
                {"name": "id", "description": "Identifier"},
                {"name": "amount"},
            ],
            "raw_sql": "CREATE TABLE public.orders (...)",
        },
    }

    text = _extract_tactical_chroma_text(payload)

    assert text == (
        "Таблица public.orders. Orders table\n"
        "Колонки: id: Identifier; amount"
    )
    assert "raw_sql" not in text


def test_rebuild_metric_rejects_unknown_metadata_metric():
    handler = SimpleNamespace(
        tactical_collection=SimpleNamespace(
            metadata={"hnsw:space": "manhattan"},
            configuration=None,
        ),
        strategic_collection=None,
    )

    with pytest.raises(ValueError, match="Unsupported Chroma distance metric"):
        _resolve_rebuild_chroma_metric(handler)


def test_rebuild_recreates_collections_with_embedding_metadata(monkeypatch):
    from memory import rebuild as rebuild_module

    class FakeClient:
        def __init__(self):
            self.created = {}

        def delete_collection(self, name):
            return None

        def get_or_create_collection(self, name, metadata):
            self.created[name] = metadata
            return _FakeCollection(metadata)

    fake_client = FakeClient()
    handler = SimpleNamespace(
        chroma_client=fake_client,
        embedding_model=object(),
        embedding_model_name="model-x",
        embedding_dimension=384,
        embedding_metadata_mismatch=True,
        tactical_collection=SimpleNamespace(metadata={"hnsw:space": "l2"}, configuration=None),
        strategic_collection=None,
    )
    monkeypatch.setattr(rebuild_module, "_prepare_tactical_memory", lambda _handler: _prepared())
    monkeypatch.setattr(rebuild_module, "_prepare_strategic_memory", lambda _handler: _prepared())

    result = rebuild_chromadb_from_sqlite(handler)

    assert result.status is RebuildStatus.COMPLETE
    assert fake_client.created["tactical_memory"] == {
        "description": "Detailed step-by-step agent experience",
        "hnsw:space": "l2",
        "embedding_model_name": "model-x",
        "embedding_dimension": 384,
    }
    assert fake_client.created["strategic_memory"] == {
        "description": "High-level goals and context",
        "hnsw:space": "l2",
        "embedding_model_name": "model-x",
        "embedding_dimension": 384,
    }
    assert handler.embedding_metadata_mismatch is False


def test_rebuild_recreates_and_repopulates_under_handler_lock(monkeypatch):
    from memory import rebuild as rebuild_module

    events = []

    class FakeLock:
        def __init__(self, name):
            self.name = name
            self.locked = False

        def __enter__(self):
            self.locked = True
            events.append(f"{self.name}.enter")

        def __exit__(self, exc_type, exc, tb):
            events.append(f"{self.name}.exit")
            self.locked = False

    rebuild_lock = FakeLock("rebuild_lock")
    handler_lock = FakeLock("handler_lock")

    class FakeClient:
        def delete_collection(self, name):
            assert rebuild_lock.locked
            assert handler_lock.locked
            events.append(f"delete:{name}")

        def get_or_create_collection(self, name, metadata):
            assert rebuild_lock.locked
            assert handler_lock.locked
            events.append(f"create:{name}")
            collection_kind = "tactical" if name == "tactical_memory" else "strategic"
            return _FakeCollection(
                metadata,
                on_upsert=lambda ids: events.append(f"upsert:{collection_kind}:{len(ids)}"),
                on_get=lambda: events.append(f"get:{collection_kind}"),
            )

    def fake_prepare_tactical(handler):
        assert rebuild_lock.locked
        assert handler_lock.locked
        events.append("prepare:tactical")
        return _prepared("t1")

    def fake_prepare_strategic(handler):
        assert rebuild_lock.locked
        assert handler_lock.locked
        events.append("prepare:strategic")
        return _prepared("s1")

    handler = SimpleNamespace(
        lock=handler_lock,
        chroma_client=FakeClient(),
        embedding_model=object(),
        embedding_model_name="model-x",
        embedding_dimension=384,
        tactical_collection=SimpleNamespace(metadata={"hnsw:space": "cosine"}, configuration=None),
        strategic_collection=None,
    )
    monkeypatch.setattr(rebuild_module, "_REBUILD_LOCK", rebuild_lock)
    monkeypatch.setattr(rebuild_module, "_prepare_tactical_memory", fake_prepare_tactical)
    monkeypatch.setattr(rebuild_module, "_prepare_strategic_memory", fake_prepare_strategic)

    result = rebuild_chromadb_from_sqlite(handler)

    assert result.status is RebuildStatus.COMPLETE
    assert events == [
        "rebuild_lock.enter",
        "handler_lock.enter",
        "prepare:tactical",
        "prepare:strategic",
        "delete:strategic_memory",
        "delete:tactical_memory",
        "create:strategic_memory",
        "create:tactical_memory",
        "upsert:tactical:1",
        "get:tactical",
        "upsert:strategic:1",
        "get:strategic",
        "handler_lock.exit",
        "rebuild_lock.exit",
    ]


def test_rebuild_preflight_failure_does_not_delete_collections(monkeypatch):
    from memory import rebuild as rebuild_module

    class FakeClient:
        def delete_collection(self, name):
            raise AssertionError(f"delete_collection called before successful preflight: {name}")

    handler = SimpleNamespace(
        chroma_client=FakeClient(),
        embedding_model=object(),
        embedding_model_name="model-x",
        embedding_dimension=384,
        tactical_collection=SimpleNamespace(metadata={"hnsw:space": "cosine"}, configuration=None),
        strategic_collection=None,
    )
    monkeypatch.setattr(
        rebuild_module,
        "_prepare_tactical_memory",
        lambda _handler: (_ for _ in ()).throw(RuntimeError("preflight failed")),
    )

    report = rebuild_chromadb_from_sqlite(handler)

    assert report.status is RebuildStatus.FAILED
    assert report.error_code == "REBUILD_PREFLIGHT_FAILED"


def test_rebuild_row_level_chroma_failure_is_degraded_without_metadata_mismatch(monkeypatch):
    from memory import rebuild as rebuild_module

    class FakeClient:
        def delete_collection(self, name):
            return None

        def get_or_create_collection(self, name, metadata):
            collection = _FakeCollection(metadata)
            if name == "tactical_memory":
                collection.upsert = lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("add failed")
                )
            return collection

    handler = SimpleNamespace(
        chroma_client=FakeClient(),
        embedding_model=object(),
        embedding_model_name="model-x",
        embedding_dimension=384,
        embedding_metadata_mismatch=False,
        tactical_collection=SimpleNamespace(metadata={"hnsw:space": "cosine"}, configuration=None),
        strategic_collection=None,
    )
    monkeypatch.setattr(rebuild_module, "_prepare_tactical_memory", lambda _handler: _prepared("t1"))
    monkeypatch.setattr(rebuild_module, "_prepare_strategic_memory", lambda _handler: _prepared())

    report = rebuild_chromadb_from_sqlite(handler)

    assert report.status is RebuildStatus.DEGRADED
    assert report.tactical.failed == (("t1", "CHROMA_UPSERT_FAILED"),)
    assert report.tactical.missing_ids == ("t1",)
    assert handler.embedding_metadata_mismatch is False


def test_streamlit_maps_typed_rebuild_status_without_parsing_text(monkeypatch):
    from memory import streamlit_api as streamlit_module

    collection = CollectionRebuildReport(
        source_ids=("one", "two"),
        expected_ids=("one",),
        indexed_ids=("one",),
        permanently_skipped=(("two", "CONTENT_TOO_SHORT"),),
        failed=(),
        missing_ids=(),
        unexpected_ids=(),
    )
    report = RebuildReport(
        status=RebuildStatus.DEGRADED,
        tactical=collection,
        strategic=CollectionRebuildReport((), (), (), (), (), (), ()),
        duration_ms=12.5,
    )
    monkeypatch.setattr(
        streamlit_module,
        "rebuild_chromadb_from_sqlite",
        lambda _handler: report,
    )
    manager = MemoryRAGManager.__new__(MemoryRAGManager)
    manager.memory_manager = SimpleNamespace(db_handler=object())
    monkeypatch.setattr(manager, "_require_memory_archivist", lambda *_args: None)
    monkeypatch.setattr(
        manager,
        "get_memory_status",
        lambda: MemoryStatus(
            sqlite_available=True,
            chromadb_available=True,
            embedding_model_available=True,
            sqlite_path="memory.sqlite",
            chromadb_path="chroma",
            embedding_model_name="model",
        ),
    )

    result = manager.rebuild_memory(force=True)

    assert result.success is False
    assert result.tactical_count == 1
    assert result.tactical_errors == 0
    assert result.warnings == ["two: CONTENT_TOO_SHORT"]
    assert result.permanently_skipped_count == 1
    assert result.error_message == "rebuild status: degraded"


def _create_permanently_skipped_only_db(db_path):
    """Seed a SQLite DB with only permanently-skipped tactical rows.

    One row has malformed JSON (MALFORMED_JSON) and one row has content that
    is too short to embed (CONTENT_TOO_SHORT). Neither reaches the embedding
    step, and neither is a transient/technical failure.
    """

    def get_connection():
        return sqlite3.connect(db_path)

    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE agent_memory (
                session_id TEXT, agent_name TEXT, step INTEGER,
                instance_step INTEGER, run_id TEXT, data TEXT,
                valid_to TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE strategic_memory ("
            "memory_id TEXT, session_id TEXT, type TEXT, content TEXT, valid_to TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO agent_memory VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
            ("s1", "Agent", 1, "{not json"),
        )
        conn.execute(
            "INSERT INTO agent_memory VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
            ("s1", "Agent", 2, json.dumps({"x": "a"})),
        )
        conn.commit()
    finally:
        conn.close()
    return get_connection


class _FakeRecreateClient:
    """Chroma client stub that always succeeds at collection recreation."""

    def delete_collection(self, name):
        return None

    def get_or_create_collection(self, name, metadata):
        return _FakeCollection(metadata)


def test_rebuild_permanently_skipped_rows_leave_status_complete_and_metadata_usable(tmp_path):
    from memory.manager import MemoryManager

    db_path = tmp_path / "memory.sqlite"
    get_connection = _create_permanently_skipped_only_db(db_path)

    handler = SimpleNamespace(
        _get_connection=get_connection,
        chroma_client=_FakeRecreateClient(),
        embedding_model=object(),
        embedding_model_name="model-x",
        embedding_dimension=384,
        embedding_metadata_mismatch=True,
        tactical_collection=SimpleNamespace(metadata={"hnsw:space": "cosine"}, configuration=None),
        strategic_collection=None,
    )

    report = rebuild_chromadb_from_sqlite(handler)

    assert report.status is RebuildStatus.COMPLETE
    assert handler.embedding_metadata_mismatch is False
    assert report.tactical.failed == ()
    assert len(report.tactical.permanently_skipped) == 2
    assert {reason for _id, reason in report.tactical.permanently_skipped} == {
        "MALFORMED_JSON",
        "CONTENT_TOO_SHORT",
    }

    # The metadata-mismatch flag is now False, so subsequent embedding calls
    # (without the rebuild-only allow_metadata_mismatch escape hatch) must not
    # be blocked by a stale mismatch left over from row-level skips.
    handler.embedding_model = SimpleNamespace(
        encode=lambda text, convert_to_tensor=False: SimpleNamespace(tolist=lambda: [0.1]),
    )
    manager = MemoryManager.__new__(MemoryManager)
    manager.db_handler = handler
    embedding = manager._create_embedding("some passage text", purpose="passage")
    assert embedding == [0.1]


def test_rebuild_embedding_unavailable_from_nul_only_content_is_permanently_skipped(
    monkeypatch, tmp_path
):
    """T6 regression: NUL-only content is data-deterministic, not transient.

    ``agent_response`` of length >= 10 made only of NUL bytes passes the
    ``len(text.strip()) < 10`` guard in ``_prepare_tactical_memory`` (NUL is
    not whitespace), but ``MemoryManager._normalize_text_for_embedding``
    strips NUL bytes, leaving an empty string, so ``_create_embedding``
    returns ``None`` and rebuild records ``EMBEDDING_UNAVAILABLE``. This must
    be permanently skipped, not retried, since a retry can never change the
    row's content.
    """
    from memory import rebuild as rebuild_module
    from memory.manager import MemoryManager

    db_path = tmp_path / "memory.sqlite"

    def get_connection():
        return sqlite3.connect(db_path)

    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE agent_memory (
                session_id TEXT, agent_name TEXT, step INTEGER,
                instance_step INTEGER, run_id TEXT, data TEXT,
                valid_to TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE strategic_memory ("
            "memory_id TEXT, session_id TEXT, type TEXT, content TEXT, valid_to TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO agent_memory VALUES (?, ?, ?, NULL, NULL, ?, NULL)",
            (
                "s1",
                "Agent",
                1,
                json.dumps({"cache_kind": "agent_step", "agent_response": "\x00" * 20}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Real MemoryManager instance (not a stub return value) so the actual
    # _normalize_text_for_embedding -> _create_embedding path runs; only the
    # underlying encode() call is stubbed, and it must never be reached
    # because the NUL-only text normalizes to empty before encoding.
    embedding_db_handler = SimpleNamespace(
        embedding_model=SimpleNamespace(
            encode=lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("encode must not be called for NUL-only content")
            )
        ),
        embedding_model_name="model-x",
        embedding_metadata_mismatch=False,
    )
    real_manager = MemoryManager.__new__(MemoryManager)
    real_manager.db_handler = embedding_db_handler
    monkeypatch.setattr(rebuild_module, "memory_manager", real_manager)

    handler = SimpleNamespace(
        _get_connection=get_connection,
        chroma_client=_FakeRecreateClient(),
        embedding_model=object(),
        embedding_model_name="model-x",
        embedding_dimension=384,
        embedding_metadata_mismatch=True,
        tactical_collection=SimpleNamespace(metadata={"hnsw:space": "cosine"}, configuration=None),
        strategic_collection=None,
    )

    report = rebuild_chromadb_from_sqlite(handler)

    assert report.status is RebuildStatus.COMPLETE
    assert report.tactical.failed == ()
    assert report.tactical.permanently_skipped == (("s1-Agent-1", "EMBEDDING_UNAVAILABLE"),)
    assert report.tactical.missing_ids == ()
    assert report.tactical.unexpected_ids == ()
    assert handler.embedding_metadata_mismatch is False


def test_streamlit_rebuild_memory_succeeds_when_only_permanently_skipped_rows_exist(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "memory.sqlite"
    get_connection = _create_permanently_skipped_only_db(db_path)

    handler = SimpleNamespace(
        _get_connection=get_connection,
        chroma_client=_FakeRecreateClient(),
        embedding_model=object(),
        embedding_model_name="model-x",
        embedding_dimension=384,
        embedding_metadata_mismatch=True,
        tactical_collection=SimpleNamespace(metadata={"hnsw:space": "cosine"}, configuration=None),
        strategic_collection=None,
    )

    manager = MemoryRAGManager.__new__(MemoryRAGManager)
    manager.memory_manager = SimpleNamespace(db_handler=handler)
    monkeypatch.setattr(manager, "_require_memory_archivist", lambda *_args: None)
    monkeypatch.setattr(
        manager,
        "get_memory_status",
        lambda: MemoryStatus(
            sqlite_available=True,
            chromadb_available=True,
            embedding_model_available=True,
            sqlite_path=str(db_path),
            chromadb_path="chroma",
            embedding_model_name="model-x",
        ),
    )

    result = manager.rebuild_memory(force=True)

    assert result.success is True
    assert result.permanently_skipped_count == 2
    assert handler.embedding_metadata_mismatch is False


def test_rebuild_collection_recreate_failure_still_marks_metadata_mismatch(monkeypatch):
    from memory import rebuild as rebuild_module

    class FakeClient:
        def delete_collection(self, name):
            return None

        def get_or_create_collection(self, name, metadata):
            raise RuntimeError("chroma unavailable")

    handler = SimpleNamespace(
        chroma_client=FakeClient(),
        embedding_model=object(),
        embedding_model_name="model-x",
        embedding_dimension=384,
        embedding_metadata_mismatch=False,
        tactical_collection=SimpleNamespace(metadata={"hnsw:space": "cosine"}, configuration=None),
        strategic_collection=None,
    )
    monkeypatch.setattr(rebuild_module, "_prepare_tactical_memory", lambda _handler: _prepared("t1"))
    monkeypatch.setattr(rebuild_module, "_prepare_strategic_memory", lambda _handler: _prepared("s1"))

    report = rebuild_chromadb_from_sqlite(handler)

    assert report.status is RebuildStatus.FAILED
    assert report.error_code == "COLLECTION_RECREATE_FAILED"
    assert handler.embedding_metadata_mismatch is True


def test_rebuild_groups_duplicate_canonical_semantic_rows(monkeypatch, tmp_path):
    from custom_tools.text_to_sql.successful_sql_memory import canonical_example_id
    from memory import rebuild as rebuild_module

    namespace_key = "a" * 64
    example_id = canonical_example_id(
        namespace_version_key=namespace_key,
        user_query="show orders",
        sql="SELECT * FROM orders",
        dialect="sqlite",
    )
    db_path = tmp_path / "memory.sqlite"

    def get_connection():
        return sqlite3.connect(db_path)

    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE agent_memory (
                session_id TEXT, agent_name TEXT, step INTEGER,
                instance_step INTEGER, run_id TEXT, data TEXT,
                valid_to TEXT
            )
            """
        )
        for step in (1, 2):
            payload = {
                "serialization_version": 1,
                "example_id": example_id,
                "semantic_id": example_id,
                "namespace_version_key": namespace_key,
                "user_query": "show orders",
                "sql": "SELECT * FROM orders",
                "dialect": "sqlite",
                "run_id": f"run-{step}",
                "saved_at": f"2026-07-12T00:00:0{step}+00:00",
                "approved": True,
                "executed": True,
                "execution_success": True,
                "audited": True,
                "cache_kind": "successful_sql_example",
            }
            conn.execute(
                "INSERT INTO agent_memory VALUES (?, ?, ?, NULL, ?, ?, NULL)",
                (
                    namespace_key,
                    "Schema-RAG-Agent",
                    step,
                    f"run-{step}",
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    handler = SimpleNamespace(_get_connection=get_connection)
    monkeypatch.setattr(
        rebuild_module,
        "memory_manager",
        SimpleNamespace(
            _extract_text_content=lambda data: json.dumps(data, sort_keys=True),
            _create_embedding=lambda *_args, **_kwargs: [0.1],
        ),
    )

    prepared = rebuild_module._prepare_tactical_memory(handler)

    assert prepared.source_ids == (example_id,)
    assert tuple(record["id"] for record in prepared.records) == (example_id,)
    assert prepared.failed == ()
