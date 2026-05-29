"""
``custom_tools.text_to_sql.schema_linking`` — decomposed package version
of the legacy ``schema_linking_core`` module (Phase 7, T7.3, EPIC 8.2).

Public surface:

  * :class:`SchemaLinkingCore` — orchestrator
    (see :mod:`.linking_orchestrator`).
  * :class:`HeuristicLinker` — heuristic pipeline
    (see :mod:`.heuristic_linker`).
  * :class:`LLMLinker` — LLM pipeline (see :mod:`.llm_linker`).
  * :class:`JoinValidator` — join-building / -validation utilities
    (see :mod:`.join_validation`).
  * Top-level resolution helpers in :mod:`.resolution`:
    ``_resolve_table_name``, ``_resolve_column_name``,
    ``_table_exists_in_schema``, ``_column_exists_in_table``,
    ``_get_column_meta``.

EPIC 8.6: legacy shim ``schema_linking_core.py`` удалён —
импортируйте напрямую из ``custom_tools.text_to_sql.schema_linking``.
``llm_caller`` теперь обязан передаваться через конструктор
``SchemaLinkingCore`` (DI), late-binding lookup через shim удалён.
"""
from .linking_orchestrator import SchemaLinkingCore
from .heuristic_linker import HeuristicLinker
from .llm_linker import LLMLinker
from .join_validation import JoinValidator

__all__ = [
    "SchemaLinkingCore",
    "HeuristicLinker",
    "LLMLinker",
    "JoinValidator",
]
