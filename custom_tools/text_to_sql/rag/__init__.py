"""
RAG (Retrieval Augmented Generation) для поиска SQL примеров.

Декомпозиция rag.py в package (Phase 7). Внешние импорты:
    from custom_tools.text_to_sql.rag import RAGSearcher
    from custom_tools.text_to_sql import rag
сохранены через фасад.

Реэкспорт `get_memory`, `save_memory`, `memory_manager` на уровне фасада
сохраняет совместимость с monkeypatch-сценариями вида
`monkeypatch.setattr(rag, "get_memory", fake)` (см. test_text_to_sql_core_contracts.py).
"""
from memory.manager import memory_manager
from memory.tools import get_memory, save_memory

from .search import RAGSearcher

__all__ = ["RAGSearcher", "get_memory", "save_memory", "memory_manager"]
