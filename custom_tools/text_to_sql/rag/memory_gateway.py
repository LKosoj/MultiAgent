"""Memory access gateway for RAG services.

The default implementation keeps the historical late lookup through the
``custom_tools.text_to_sql.rag`` facade so tests and callers that monkeypatch
``rag.memory_manager`` / ``rag.get_memory`` / ``rag.save_memory`` keep working.
"""
from typing import Any


class RAGMemoryGateway:
    """Small injectable boundary around the tactical memory facade."""

    @property
    def memory_manager(self) -> Any:
        from custom_tools.text_to_sql import rag as _facade

        return _facade.memory_manager

    def get_memory(self, **kwargs: Any) -> Any:
        from custom_tools.text_to_sql import rag as _facade

        return _facade.get_memory(**kwargs)

    def save_memory(self, **kwargs: Any) -> Any:
        from custom_tools.text_to_sql import rag as _facade

        return _facade.save_memory(**kwargs)
