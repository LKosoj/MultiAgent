"""
Backward-compat shim for ``schema_linking.strategies`` (EPIC 8.2).

Расщеплено на:

  * :mod:`linking_orchestrator` — :class:`SchemaLinkingCore` фасад.
  * :mod:`heuristic_linker` — heuristic pipeline.
  * :mod:`llm_linker` — LLM pipeline.

Этот модуль остаётся как тонкий re-export для существующих импортов
``from custom_tools.text_to_sql.schema_linking.strategies import SchemaLinkingCore``.
"""
from .linking_orchestrator import SchemaLinkingCore

__all__ = ["SchemaLinkingCore"]
