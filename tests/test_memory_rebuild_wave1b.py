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
        tactical_collection=SimpleNamespace(metadata={"hnsw:space": "l2"}, configuration=None),
        strategic_collection=None,
    )
    monkeypatch.setattr(rebuild_module, "_rebuild_tactical_memory", lambda _handler: (0, 0))
    monkeypatch.setattr(rebuild_module, "_rebuild_strategic_memory", lambda _handler: (0, 0))

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
