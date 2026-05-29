"""Контрактные тесты для EPIC 8 Block C (архитектурный долг).

Покрывают:
  * 8.10 — RAGSearcher собран через КОМПОЗИЦИЮ (не multi-inheritance).
  * 8.3  — schema_memory.py разбит на 4 файла-источника + facade:
      - schema_memory.py        (facade-реэкспорт)
      - schema_memory_sqlite.py (SQLite-операции + SchemaMemoryManager)
      - schema_memory_chroma.py (Chroma-хелперы + metric resolution)
      - schema_cache.py         (cache hashing + SchemaCacheManager)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 8.10 — RAGSearcher: composition вместо multi-inheritance
# ---------------------------------------------------------------------------


def test_no_multiple_inheritance_in_ragsearcher():
    """8.10: RAGSearcher больше не наследует mixin'ы.

    До правки MRO был:
        RAGSearcher → IndexingMixin → RetrievalMixin → EmbeddingUtilsMixin → object
    То есть 5 элементов в __mro__.

    После 8.10 RAGSearcher — single-inheritance от object; сервисы
    подключены через композицию (см. __init__).
    """
    from custom_tools.text_to_sql.rag.search import RAGSearcher

    mro = RAGSearcher.__mro__
    assert len(mro) <= 3, (
        f"RAGSearcher.__mro__ имеет {len(mro)} элементов: {mro}. "
        f"После 8.10 ожидалось <=3 (RAGSearcher, [ABC?], object). "
        f"Не возвращай multi-inheritance из mixin'ов."
    )
    # Должно быть точно: (RAGSearcher, object) — composition, не наследование.
    assert mro[-1] is object


def test_ragsearcher_composes_services():
    """8.10: RAGSearcher хранит сервисы как атрибуты, а не как родителей."""
    from custom_tools.text_to_sql.rag.search import RAGSearcher
    from custom_tools.text_to_sql.rag.embedding_utils import EmbeddingUtils
    from custom_tools.text_to_sql.rag.indexing import IndexingService
    from custom_tools.text_to_sql.rag.retrieval import RetrievalService

    searcher = RAGSearcher()
    assert isinstance(searcher._embeddings, EmbeddingUtils)
    assert isinstance(searcher._indexing, IndexingService)
    assert isinstance(searcher._retrieval, RetrievalService)


def test_shared_index_state_is_process_global():
    """8.10: _index_registry/_per_session_locks остаются process-global.

    Несколько экземпляров RAGSearcher должны шарить один и тот же
    SharedIndexState (через ClassVar). Это сохраняет 4.5-инвариант:
    один RLock на (session_id, process).
    """
    from custom_tools.text_to_sql.rag.search import RAGSearcher

    s1 = RAGSearcher()
    s2 = RAGSearcher()
    # _shared_state — ClassVar; один и тот же объект на всех инстансах.
    assert s1._shared_state is s2._shared_state
    # Лок на одну сессию — один и тот же.
    lock_a = s1._get_session_lock("sess-shared")
    lock_b = s2._get_session_lock("sess-shared")
    assert lock_a is lock_b


# ---------------------------------------------------------------------------
# 8.3 — schema_memory.py: разбиение по responsibility
# ---------------------------------------------------------------------------


def test_schema_memory_facade_reexports():
    """8.3: schema_memory.py — это facade, реальные классы живут в подмодулях."""
    from custom_tools.text_to_sql.schema_memory import (
        SchemaMemoryManager,
        SchemaCacheManager,
        _resolve_chroma_metric,
        _distance_to_similarity,
        LINKING_CACHE_ENV_PREFIXES,
        _collect_linking_cache_env,
        _truncate_salt,
        _normalize_for_hash,
        ORDER_SIGNIFICANT_KEYS,
    )

    assert SchemaMemoryManager.__module__ == "custom_tools.text_to_sql.schema_memory_sqlite", (
        "SchemaMemoryManager должен быть определён в schema_memory_sqlite.py"
    )
    assert SchemaCacheManager.__module__ == "custom_tools.text_to_sql.schema_cache", (
        "SchemaCacheManager должен быть определён в schema_cache.py"
    )
    assert _resolve_chroma_metric.__module__ == "custom_tools.text_to_sql.schema_memory_chroma"
    assert _distance_to_similarity.__module__ == "custom_tools.text_to_sql.schema_memory_chroma"
    assert _collect_linking_cache_env.__module__ == "custom_tools.text_to_sql.schema_cache"
    assert _normalize_for_hash.__module__ == "custom_tools.text_to_sql.schema_cache"
    assert _truncate_salt.__module__ == "custom_tools.text_to_sql.schema_cache"


def _strip_comments_and_strings(source: str) -> str:
    """Грубая очистка для self-check тестов: удаляет inline-комменты и строковые литералы."""
    cleaned_lines = []
    for line in source.splitlines():
        if "#" in line:
            line = line.split("#", 1)[0]
        line = re.sub(r'"[^"]*"', '""', line)
        line = re.sub(r"'[^']*'", "''", line)
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def test_schema_memory_chroma_has_no_sqlite_calls():
    """8.3: schema_memory_chroma.py не должен дёргать SQLite-операции.

    Запретные паттерны (SQLite-specific):
      * BEGIN IMMEDIATE
      * agent_memory (название таблицы)
      * valid_to (служебная колонка SQLite-схемы)
      * get_sqlite_connection
    """
    from custom_tools.text_to_sql import schema_memory_chroma

    source = Path(schema_memory_chroma.__file__).read_text(encoding="utf-8")
    code = _strip_comments_and_strings(source)

    forbidden = [
        r"BEGIN\s+IMMEDIATE",
        r"agent_memory",
        r"valid_to",
        r"get_sqlite_connection",
    ]
    offenders = [p for p in forbidden if re.search(p, code)]
    assert not offenders, (
        f"8.3: schema_memory_chroma.py должен содержать только Chroma-операции; "
        f"найдены SQLite-паттерны: {offenders}"
    )


def test_schema_memory_sqlite_has_no_chroma_imports():
    """8.3: schema_memory_sqlite.py не должен напрямую дёргать private Chroma-API.

    Допустимо обращение через публичный API memory_manager
    (get_tactical_collection / search_semantic_with_scores), но НЕ через
    приватные внутренности `db_handler.tactical_collection`.
    """
    from custom_tools.text_to_sql import schema_memory_sqlite

    source = Path(schema_memory_sqlite.__file__).read_text(encoding="utf-8")
    code = _strip_comments_and_strings(source)

    forbidden = [
        r"db_handler\.tactical_collection",
        r"memory_manager\._search_semantic_with_scores",
        r"memory_manager\.db_handler\._get_connection",
    ]
    offenders = [p for p in forbidden if re.search(p, code)]
    assert not offenders, (
        f"8.3: schema_memory_sqlite.py не должен использовать private Chroma/SQLite API: "
        f"{offenders}"
    )


def test_schema_memory_facade_is_pure_reexport():
    """8.3: schema_memory.py — только реэкспорт, без бизнес-логики.

    Facade не должен содержать определения классов и сложной логики.
    """
    from custom_tools.text_to_sql import schema_memory as sm_module

    source = Path(sm_module.__file__).read_text(encoding="utf-8")
    code = _strip_comments_and_strings(source)

    # В facade не должно быть определений class SchemaMemoryManager/SchemaCacheManager
    # и тяжёлых функций. Допустимы только импорты и __all__.
    assert "class SchemaMemoryManager" not in code, (
        "8.3: класс SchemaMemoryManager должен жить в schema_memory_sqlite.py, "
        "а не в facade"
    )
    assert "class SchemaCacheManager" not in code, (
        "8.3: класс SchemaCacheManager должен жить в schema_cache.py, "
        "а не в facade"
    )
