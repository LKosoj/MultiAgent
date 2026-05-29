"""
Contract-pinning тест для package custom_tools.text_to_sql.rag (Phase 7).

Проверяет, что после декомпозиции rag.py в package:
- namespace `from custom_tools.text_to_sql import rag` работает;
- прямой импорт `from custom_tools.text_to_sql.rag import RAGSearcher` работает
  и указывает на тот же класс, что и атрибут модуля;
- публичные методы RAGSearcher (search_examples, search_examples_by_query)
  доступны на классе.
"""
from custom_tools.text_to_sql import rag


def test_rag_searcher_class_preserved():
    assert hasattr(rag, "RAGSearcher")
    assert callable(rag.RAGSearcher)


def test_rag_searcher_direct_import_preserved():
    from custom_tools.text_to_sql.rag import RAGSearcher
    assert RAGSearcher is rag.RAGSearcher


def test_rag_searcher_public_methods_preserved():
    # минимум: search_examples и search_examples_by_query доступны как методы
    assert hasattr(rag.RAGSearcher, "search_examples")
    assert hasattr(rag.RAGSearcher, "search_examples_by_query")
