"""
Schema Memory Manager — facade-реэкспорт.

EPIC 8.3: исходный 868-строчный модуль разбит на 3 файла-источника по
responsibility:

  * `schema_memory_sqlite.py` — ``SchemaMemoryManager`` (orchestrator;
    SQLite-операции + Chroma cleanup managed soft-fail; держит
    ``_schema_write_lock``).
  * `schema_memory_chroma.py` — Chroma-хелперы: ``_resolve_chroma_metric``,
    ``_distance_to_similarity`` + поддерживаемые метрики.
  * `schema_cache.py`         — ``SchemaCacheManager`` + cache-hashing
    утилиты (``LINKING_CACHE_ENV_PREFIXES``, ``_collect_linking_cache_env``,
    ``_truncate_salt``, ``_normalize_for_hash``, ``ORDER_SIGNIFICANT_KEYS``).

Этот модуль — pure re-export. Никаких определений классов и функций
здесь больше нет (см. ``test_schema_memory_facade_is_pure_reexport``).
Внешний контракт (импорты ``from custom_tools.text_to_sql.schema_memory import ...``)
сохранён.
"""
from __future__ import annotations

from .schema_cache import (
    LINKING_CACHE_ENV_PREFIXES,
    ORDER_SIGNIFICANT_KEYS,
    SchemaCacheCorrupted,
    SchemaCacheManager,
    _collect_linking_cache_env,
    _compute_env_fingerprint,
    _compute_schema_fingerprint,
    _normalize_for_hash,
    _truncate_salt,
)
from .schema_memory_chroma import (
    _distance_to_similarity,
    _resolve_chroma_metric,
)
from .schema_memory_sqlite import (
    SchemaIndexingError,
    SchemaIndexingMemoryUnavailable,
    SchemaMemoryManager,
)

__all__ = [
    # Классы (внешний контракт)
    "SchemaMemoryManager",
    "SchemaCacheManager",
    "SchemaCacheCorrupted",
    # W2-T1: индексация схемы — кастомные исключения fail-fast
    "SchemaIndexingError",
    "SchemaIndexingMemoryUnavailable",
    # Chroma-хелперы (используются rag/retrieval.py)
    "_resolve_chroma_metric",
    "_distance_to_similarity",
    # Cache-hashing утилиты (используются тестами + schema_cache)
    "LINKING_CACHE_ENV_PREFIXES",
    "_collect_linking_cache_env",
    "_compute_env_fingerprint",
    "_compute_schema_fingerprint",
    "_truncate_salt",
    "_normalize_for_hash",
    "ORDER_SIGNIFICANT_KEYS",
]
