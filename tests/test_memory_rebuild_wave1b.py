from types import SimpleNamespace

import pytest

from memory.rebuild import (
    _extract_tactical_chroma_text,
    _resolve_rebuild_chroma_metric,
    rebuild_chromadb_from_sqlite,
)


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
            return SimpleNamespace(metadata=metadata, configuration=None)

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
    monkeypatch.setattr(rebuild_module, "_prepare_tactical_memory", lambda _handler: ([], 0))
    monkeypatch.setattr(rebuild_module, "_prepare_strategic_memory", lambda _handler: ([], 0))

    result = rebuild_chromadb_from_sqlite(handler)

    assert "успешно" in result
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
            return SimpleNamespace(metadata=metadata, configuration=None)

    def fake_prepare_tactical(handler):
        assert rebuild_lock.locked
        assert handler_lock.locked
        events.append("prepare:tactical")
        return ([{"id": "t1"}], 0)

    def fake_prepare_strategic(handler):
        assert rebuild_lock.locked
        assert handler_lock.locked
        events.append("prepare:strategic")
        return ([{"id": "s1"}], 0)

    def fake_add_tactical(handler, records):
        assert rebuild_lock.locked
        assert handler_lock.locked
        events.append(f"add:tactical:{len(records)}")
        return len(records)

    def fake_add_strategic(handler, records):
        assert rebuild_lock.locked
        assert handler_lock.locked
        events.append(f"add:strategic:{len(records)}")
        return len(records)

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
    monkeypatch.setattr(rebuild_module, "_add_tactical_memory", fake_add_tactical)
    monkeypatch.setattr(rebuild_module, "_add_strategic_memory", fake_add_strategic)

    result = rebuild_chromadb_from_sqlite(handler)

    assert "успешно" in result
    assert events == [
        "rebuild_lock.enter",
        "handler_lock.enter",
        "prepare:tactical",
        "prepare:strategic",
        "delete:strategic_memory",
        "delete:tactical_memory",
        "create:strategic_memory",
        "create:tactical_memory",
        "add:tactical:1",
        "add:strategic:1",
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

    with pytest.raises(RuntimeError, match="preflight failed"):
        rebuild_chromadb_from_sqlite(handler)


def test_rebuild_add_failure_marks_embedding_metadata_mismatch(monkeypatch):
    from memory import rebuild as rebuild_module

    class FakeClient:
        def delete_collection(self, name):
            return None

        def get_or_create_collection(self, name, metadata):
            return SimpleNamespace(metadata=metadata, configuration=None)

    handler = SimpleNamespace(
        chroma_client=FakeClient(),
        embedding_model=object(),
        embedding_model_name="model-x",
        embedding_dimension=384,
        embedding_metadata_mismatch=False,
        tactical_collection=SimpleNamespace(metadata={"hnsw:space": "cosine"}, configuration=None),
        strategic_collection=None,
    )
    monkeypatch.setattr(rebuild_module, "_prepare_tactical_memory", lambda _handler: ([{"id": "t1"}], 0))
    monkeypatch.setattr(rebuild_module, "_prepare_strategic_memory", lambda _handler: ([], 0))
    monkeypatch.setattr(
        rebuild_module,
        "_add_tactical_memory",
        lambda _handler, _records: (_ for _ in ()).throw(RuntimeError("add failed")),
    )

    with pytest.raises(RuntimeError, match="add failed"):
        rebuild_chromadb_from_sqlite(handler)

    assert handler.embedding_metadata_mismatch is True
