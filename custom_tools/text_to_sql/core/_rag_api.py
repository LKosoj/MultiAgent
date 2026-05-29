"""RAG API подмодуль core (Phase 7 декомпозиция).

Реализация vector_db_search.
"""
from typing import Dict, List


def vector_db_search(
    query: str,
    top_k: int = 3,
    *,
    rag_searcher,
) -> List[Dict[str, object]]:
    """Поиск релевантных SQL-примеров через get_memory по текстовому запросу.

    Args:
        query: Текст запроса пользователя
        top_k: Количество наиболее релевантных примеров для возврата (>0)

    Returns:
        Список словарей с найденными примерами

    Raises:
        ValueError: если ``top_k`` не положительный int (fail-fast,
            см. AGENTS.md — никаких silent fallback'ов).
    """
    # Fail-fast валидация: bool — подкласс int, явно отсекаем.
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ValueError(
            f"top_k must be a positive int, got {top_k!r} ({type(top_k).__name__})"
        )
    # Новый путь: используем семантику get_memory внутри RAGSearcher
    return rag_searcher.search_examples_by_query(query, top_k)
