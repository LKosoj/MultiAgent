"""W8-T9: валидация пустого/None query_embedding в RAG search.

Раньше при пустом эмбеддинге `_search_chroma` / `search_examples` возвращали
пустой список — это silent fallback (выглядит как штатный cache miss).
Теперь fail-fast с ValueError("query_embedding is empty; cannot search").
"""
from __future__ import annotations

import pytest

from custom_tools.text_to_sql.rag import RAGSearcher
from custom_tools.text_to_sql.rag.retrieval import RetrievalService
from custom_tools.text_to_sql.rag.embedding_utils import EmbeddingUtils


@pytest.fixture
def searcher(monkeypatch):
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test_w8_rag.db")
    return RAGSearcher()


@pytest.mark.parametrize("bad_emb", [None, []])
def test_search_examples_raises_on_empty_embedding(searcher, bad_emb):
    with pytest.raises(ValueError, match="query_embedding is empty"):
        searcher.search_examples(query_embedding=bad_emb, top_k=3)


@pytest.mark.parametrize("bad_emb", [None, []])
def test_search_chroma_raises_on_empty_embedding(bad_emb):
    """Прямой вызов RetrievalService._search_chroma — без RAGSearcher."""
    embeddings = EmbeddingUtils()
    rs = RetrievalService(repo_root=None, embeddings=embeddings)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="query_embedding is empty"):
        rs._search_chroma(query_embedding=bad_emb, top_k=3)


def test_non_empty_embedding_does_not_raise(searcher, monkeypatch):
    """Sanity: ненулевой эмбеддинг не должен валиться нашей валидацией.

    Используем монkeypatch на `_search_chroma`, чтобы не зависеть от Chroma.
    """
    monkeypatch.setattr(searcher, "_search_chroma", lambda emb, k: [])
    monkeypatch.setattr(searcher, "_load_sqlrag_files", lambda sid: [])
    monkeypatch.setattr(searcher, "_search_doc_files", lambda k: [])
    # `_prepare_cache_info` ходит в memory_manager — заглушаем минимумом.
    monkeypatch.setattr(
        searcher, "_prepare_cache_info",
        lambda *args, **kwargs: {
            "session_id": "x", "cache_kind": "vector_db_search",
            "cache_key": "k", "schema_version": "v", "dsn": "", "cacheable": False,
        },
    )
    monkeypatch.setattr(searcher, "_load_from_cache", lambda info: None)

    # Не должно бросать ValueError.
    out = searcher.search_examples(query_embedding=[0.1, 0.2, 0.3], top_k=2, query_text="sanity query")
    assert isinstance(out, list)
