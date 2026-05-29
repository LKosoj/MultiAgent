"""
Тесты кэша vector_db_search для RAGSearcher.search_examples().

Проверяют корректность `_load_from_cache` / `_save_to_cache` в RetrievalMixin:
- cache miss → save → hit (без повторного обращения к ChromaDB);
- инвалидация при изменении schema_version;
- fail-fast при ошибках memory layer (без silent fallback);
- стабильность cache_key при близких эмбеддингах (квантование).
"""
import pytest

from custom_tools.text_to_sql import rag
from custom_tools.text_to_sql.rag import RAGSearcher


def _make_fake_memory_store():
    """Создаёт fake-хранилище memory с get_memory/save_memory."""
    store = {"items": []}

    def fake_save_memory(*, session_id, agent_name, data, **kwargs):
        store["items"].append({
            "session_id": session_id,
            "agent_name": agent_name,
            "data": dict(data),
        })
        return len(store["items"])

    def fake_get_memory(*, session_id=None, agent_name=None, cache_kind=None,
                       include_historical=False, **kwargs):
        out = []
        for it in store["items"]:
            if session_id is not None and it["session_id"] != session_id:
                continue
            if agent_name is not None and it["agent_name"] != agent_name:
                continue
            if cache_kind is not None and it["data"].get("cache_kind") != cache_kind:
                continue
            out.append({"data": it["data"]})
        return out

    return store, fake_save_memory, fake_get_memory


def _setup_searcher(monkeypatch, *, chroma_results):
    """Создаёт RAGSearcher с отключёнными побочными источниками (chroma, sqlrag, doc)."""
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test_rag_cache.db")
    # Отключаем opt-in file fallbacks
    monkeypatch.delenv("VECTOR_DB_SEARCH_ALLOW_FILE_FALLBACKS", raising=False)

    searcher = RAGSearcher()

    chroma_calls = {"count": 0}

    def fake_search_chroma(query_embedding, top_k):
        chroma_calls["count"] += 1
        return list(chroma_results)

    monkeypatch.setattr(searcher, "_search_chroma", fake_search_chroma)
    monkeypatch.setattr(searcher, "_load_sqlrag_files", lambda session_id: [])
    monkeypatch.setattr(searcher, "_search_doc_files", lambda top_k: [])
    # Чтобы избежать обращения к memory_manager._create_embedding при пересчёте.
    # 4.3: signature совместима с query_text=… (опциональный keyword).
    monkeypatch.setattr(
        searcher, "_rescore_missing_with_embedding",
        lambda query_embedding, items, **kwargs: items,
    )
    return searcher, chroma_calls


def test_search_examples_cache_miss_then_hit(monkeypatch):
    store, fake_save, fake_get = _make_fake_memory_store()
    monkeypatch.setattr(rag, "get_memory", fake_get)
    monkeypatch.setattr(rag, "save_memory", fake_save)

    chroma_payload = [{"sql_example": "SELECT 1;", "similarity_score": 0.8}]
    searcher, chroma_calls = _setup_searcher(monkeypatch, chroma_results=chroma_payload)

    embedding = [0.1] * 8

    # Первый вызов: miss → ChromaDB query + save.
    first = searcher.search_examples(embedding, top_k=1, query_text="select one")
    assert first == [{"sql_example": "SELECT 1;", "similarity_score": 0.8}]
    assert chroma_calls["count"] == 1
    # Должна быть ровно одна запись с cache_source=vector_db_search.
    saved = [it for it in store["items"]
             if it["data"].get("cache_source") == "vector_db_search"]
    assert len(saved) == 1
    assert saved[0]["data"]["result"] == first
    assert saved[0]["data"]["cache_kind"] == "vector_db_search"

    # Второй вызов с тем же embedding: hit без обращения к ChromaDB.
    second = searcher.search_examples(embedding, top_k=1, query_text="select one")
    assert second == first
    assert chroma_calls["count"] == 1, "Cache hit должен предотвратить повторный ChromaDB-запрос"


def test_search_examples_cache_key_schema_version_invalidation(monkeypatch):
    store, fake_save, fake_get = _make_fake_memory_store()
    monkeypatch.setattr(rag, "get_memory", fake_get)
    monkeypatch.setattr(rag, "save_memory", fake_save)

    chroma_payload = [{"sql_example": "SELECT 2;", "similarity_score": 0.7}]
    searcher, chroma_calls = _setup_searcher(monkeypatch, chroma_results=chroma_payload)

    embedding = [0.2] * 8

    # get_schema_version(None) для smoke-теста подменяем через monkeypatch на utils.
    from custom_tools.text_to_sql import utils as t2s_utils

    monkeypatch.setattr(t2s_utils, "get_schema_version", lambda schema: "v1")
    first = searcher.search_examples(embedding, top_k=1, query_text="select two")
    assert chroma_calls["count"] == 1
    assert first == chroma_payload

    # Меняем schema_version → должен быть miss даже при тех же остальных параметрах.
    monkeypatch.setattr(t2s_utils, "get_schema_version", lambda schema: "v2")
    second = searcher.search_examples(embedding, top_k=1, query_text="select two")
    assert chroma_calls["count"] == 2, "Смена schema_version должна инвалидировать кэш"
    assert second == chroma_payload


def test_search_examples_cache_fail_fast_on_memory_error(monkeypatch):
    """Ошибки memory layer пробрасываются наверх (без silent fallback)."""

    class BoomError(RuntimeError):
        pass

    def fake_get_memory(**kwargs):
        raise BoomError("memory backend down")

    monkeypatch.setattr(rag, "get_memory", fake_get_memory)
    # save_memory не должен быть вызван при падении get_memory.
    monkeypatch.setattr(
        rag, "save_memory",
        lambda **kwargs: pytest.fail("save_memory не должен вызываться при ошибке get_memory"),
    )

    searcher, _ = _setup_searcher(monkeypatch, chroma_results=[])

    with pytest.raises(BoomError):
        searcher.search_examples([0.3] * 8, top_k=1, query_text="memory failure")


def test_search_examples_cache_key_stable_for_identical_embedding(monkeypatch):
    """W5-T2: битово равные эмбеддинги дают один cache_key (precise hash).

    Раньше тест проверял квантование до 3 знаков (round(x, 3)) — это давало
    коллизии для близких, но семантически различных запросов. Квантование
    убрано (см. embedding_utils.py:W5-T2); теперь cache hit требует точного
    битового совпадения вектора.
    """
    searcher, _ = _setup_searcher(monkeypatch, chroma_results=[])

    base_embedding = [0.1232] * 8
    same_embedding = [0.1232] * 8  # битово равен

    first = searcher._prepare_cache_info(base_embedding, top_k=1)["cache_key"]
    second = searcher._prepare_cache_info(same_embedding, top_k=1)["cache_key"]

    assert first == second, "Битово равные эмбеддинги должны давать одинаковый cache_key"


def test_search_examples_cache_key_differs_for_close_embeddings(monkeypatch):
    """W5-T2: близкие, но не равные эмбеддинги дают РАЗНЫЕ cache_key.

    После удаления `round(x, 3)` (см. embedding_utils.py) даже малое
    отличие вектора → cache miss. Это сознательное ужесточение: при
    реальных embedding-моделях «близкие» векторы могут соответствовать
    семантически различным запросам, кэшировать их под одним ключом
    нельзя.
    """
    searcher, _ = _setup_searcher(monkeypatch, chroma_results=[])

    base_embedding = [0.1232] * 8
    near_embedding = [v + 5e-5 for v in base_embedding]  # разница < 1e-4

    base_key = searcher._prepare_cache_info(base_embedding, top_k=1)["cache_key"]
    near_key = searcher._prepare_cache_info(near_embedding, top_k=1)["cache_key"]

    assert base_key != near_key, "Близкие, но не битово равные эмбеддинги должны давать разные cache_key"
