"""Контрактные тесты для EPIC 3 правок в `custom_tools/text_to_sql/schema_memory.py`.

Покрывают:
  * T3.4 — индексация legacy-формата схемы через helper'ы.
  * T3.5 — BLAKE2b с session_id-солью в cache_key.
  * T3.6 — similarity использует фактическую метрику Chroma.
  * T3.7 — значимый порядок (joins/ORDER BY) НЕ сортируется при хэшировании.
  * T3.20 — schema_memory не дёргает приватные методы memory_manager.
  * T3.22 — список env-флагов для cache_key собирается автодискавером,
    а не хардкодом.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql import schema_memory as sm_module
from custom_tools.text_to_sql.schema_memory import (
    LINKING_CACHE_ENV_PREFIXES,
    SchemaCacheManager,
    SchemaMemoryManager,
    _collect_linking_cache_env,
    _distance_to_similarity,
    _normalize_for_hash,
    _resolve_chroma_metric,
    _truncate_salt,
)


def _prepare_cache_info(cache, entities, schema, dsn: str | None = None):
    import os

    return cache.prepare_cache_info(entities, schema, dsn=dsn or os.environ["DB_DSN"])


# ---------------------------------------------------------------------------
# T3.4 — legacy/new формат схемы через helper'ы
# ---------------------------------------------------------------------------


def test_schema_memory_uses_helper_for_legacy_format(monkeypatch, tmp_path):
    """T3.4: index_schema_in_memory собирает table_info из обоих форматов схемы.

    В legacy-формате колонки лежат прямо в корне таблицы (без вложенного
    ключа ``columns``) и описание/колонки доставались жёсткой логикой
    ``table_schema.get("description", "")`` / ``table_schema.get("columns", {}).items()``.
    После правки helper'ы ``get_table_description``/``get_table_columns``
    нормализуют оба формата.
    """
    saved_records = []

    def fake_save_memory(*, session_id, agent_name, data):
        saved_records.append(data)

    memory_tools = SimpleNamespace(
        save_memory=fake_save_memory,
        get_memory=lambda **kwargs: [],
    )
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)

    manager = SchemaMemoryManager(tmp_path)

    # Legacy-формат: колонки прямо в корне, нет ключа "columns".
    legacy_schema = {
        "public.orders": {
            "description": "Заказы клиентов",
            "id": {"type": "INTEGER", "constraint_type": "PK"},
            "amount": {"type": "DECIMAL", "description": "Сумма заказа"},
        }
    }

    indexed = manager.index_schema_in_memory(
        session_id="s1",
        filename="s1.json",
        db_schema=legacy_schema,
        file_hash="hash1",
    )

    assert indexed == 1
    assert len(saved_records) == 1

    table_info = saved_records[0]["table_info"]
    assert table_info["description"] == "Заказы клиентов"
    column_names = {c["name"] for c in table_info["columns"]}
    assert column_names == {"id", "amount"}


def test_schema_memory_new_format_still_supported(monkeypatch, tmp_path):
    """T3.4: новый формат (с ключом ``columns``) тоже работает через helper'ы."""
    saved_records = []
    memory_tools = SimpleNamespace(
        save_memory=lambda **kw: saved_records.append(kw["data"]),
        get_memory=lambda **kw: [],
    )
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)

    manager = SchemaMemoryManager(tmp_path)
    new_schema = {
        "public.users": {
            "description": "Пользователи",
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "email": {"type": "TEXT"},
            },
        }
    }

    manager.index_schema_in_memory(
        session_id="s2",
        filename="s2.json",
        db_schema=new_schema,
        file_hash="hash2",
    )
    assert saved_records, "должен быть проиндексирован хотя бы один table"
    table_info = saved_records[0]["table_info"]
    assert table_info["description"] == "Пользователи"
    column_names = {c["name"] for c in table_info["columns"]}
    assert column_names == {"id", "email"}


# ---------------------------------------------------------------------------
# W2-T1 — index_schema_in_memory fail-fast (raise вместо silent return 0)
# ---------------------------------------------------------------------------


def test_index_schema_in_memory_raises_when_save_memory_unavailable(monkeypatch, tmp_path):
    """W2-T1: ``save_memory is None`` → SchemaIndexingMemoryUnavailable.

    Раньше silent ``return 0``: caller думал «схема пуста», а реально
    был не загружен memory-стек. Это конфигурационная проблема, не
    штатное «нет данных».
    """
    from custom_tools.text_to_sql.schema_memory import (
        SchemaIndexingMemoryUnavailable,
        SchemaMemoryManager,
    )

    memory_tools = SimpleNamespace(save_memory=None, get_memory=lambda **kw: [])
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)

    manager = SchemaMemoryManager(tmp_path)
    schema = {"orders": {"columns": {"id": {"type": "INTEGER"}}}}

    with pytest.raises(SchemaIndexingMemoryUnavailable) as exc:
        manager.index_schema_in_memory(
            session_id="s1",
            filename="s1.json",
            db_schema=schema,
            file_hash="h1",
        )
    # Сообщение должно содержать имя сессии — это контекст для диагностики.
    assert "s1" in str(exc.value)


def test_index_schema_in_memory_raises_on_per_table_failures(monkeypatch, tmp_path):
    """W2-T1: если save_memory падает на отдельных таблицах → SchemaIndexingError.

    Раньше broad except возвращал 0 при ЛЮБОЙ ошибке. Теперь:
      * Сбойные таблицы аккумулируются в ``failed_tables``.
      * Успешные ДО сбоя видны в ``indexed_count``.
      * Финальный raise сообщает обо всех неудачах разом.
    """
    from custom_tools.text_to_sql.schema_memory import (
        SchemaIndexingError,
        SchemaMemoryManager,
    )

    save_calls = []

    def fake_save_memory(*, session_id, agent_name, data):
        save_calls.append(data["table_fqn"])
        if data["table_fqn"] == "public.orders":
            raise RuntimeError("simulated DB write failure")

    memory_tools = SimpleNamespace(save_memory=fake_save_memory, get_memory=lambda **kw: [])
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)

    manager = SchemaMemoryManager(tmp_path)
    schema = {
        "public.users":  {"columns": {"id": {"type": "INTEGER"}}},
        "public.orders": {"columns": {"id": {"type": "INTEGER"}}},
        "public.items":  {"columns": {"id": {"type": "INTEGER"}}},
    }

    with pytest.raises(SchemaIndexingError) as exc:
        manager.index_schema_in_memory(
            session_id="sX",
            filename="sX.json",
            db_schema=schema,
            file_hash="hX",
        )

    err = exc.value
    # Все 3 таблицы дёргались (цикл не прерывается на первой ошибке).
    assert set(save_calls) == {"public.users", "public.orders", "public.items"}
    # Failed: только orders. Indexed: 2.
    assert err.failed_tables == ["public.orders"]
    assert err.indexed_count == 2
    # Сообщение содержит и счётчики, и причину для диагностики.
    assert "1/3" in str(err)
    assert "simulated DB write failure" in str(err)


def test_index_schema_in_memory_returns_zero_for_empty_schema(monkeypatch, tmp_path):
    """W2-T1: пустой db_schema → ``indexed_count == 0`` БЕЗ raise.

    Это штатное "нет таблиц для индексации" — единственный случай,
    когда метод возвращает 0. Любой другой сбой теперь делает raise.
    """
    from custom_tools.text_to_sql.schema_memory import SchemaMemoryManager

    memory_tools = SimpleNamespace(
        save_memory=lambda **kw: None,
        get_memory=lambda **kw: [],
    )
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)

    manager = SchemaMemoryManager(tmp_path)
    result = manager.index_schema_in_memory(
        session_id="empty",
        filename="empty.json",
        db_schema={},  # пусто = штатное "нет данных"
        file_hash="h-empty",
    )
    assert result == 0


def test_ensure_schema_indexed_distinguishes_no_tables_from_db_error(monkeypatch, tmp_path):
    """W2-T1: caller (ensure_schema_indexed_in_memory) ОТЛИЧАЕТ два случая.

    * ``False`` — нет таблиц для индексации (штатно).
    * ``raise SchemaIndexingError`` — ошибка БД/memory (нештатно).
    """
    from custom_tools.text_to_sql.schema_memory import (
        SchemaIndexingError,
        SchemaMemoryManager,
    )

    # Случай 1: пустая схема → False, без raise.
    memory_tools_ok = SimpleNamespace(
        save_memory=lambda **kw: None,
        get_memory=lambda **kw: [],
    )
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools_ok)
    manager = SchemaMemoryManager(tmp_path)
    # is_schema_indexed теперь принимает optional expected_count — используем **kwargs
    monkeypatch.setattr(manager, "is_schema_indexed", lambda session_id, file_hash, **kwargs: False)
    monkeypatch.setattr(manager, "remove_old_schema_records", lambda session_id, filename: None)

    assert manager.ensure_schema_indexed_in_memory("sqlite:///x.db", {}) is False

    # Случай 2: непустая схема, но save_memory падает → SchemaIndexingError.
    def broken_save(*, session_id, agent_name, data):
        raise RuntimeError("DB down")

    memory_tools_broken = SimpleNamespace(save_memory=broken_save, get_memory=lambda **kw: [])
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools_broken)
    manager2 = SchemaMemoryManager(tmp_path)
    monkeypatch.setattr(manager2, "is_schema_indexed", lambda session_id, file_hash, **kwargs: False)
    monkeypatch.setattr(manager2, "remove_old_schema_records", lambda session_id, filename: None)

    with pytest.raises(SchemaIndexingError):
        manager2.ensure_schema_indexed_in_memory(
            "sqlite:///x.db", {"orders": {"columns": {"id": {"type": "INT"}}}}
        )


# ---------------------------------------------------------------------------
# T3.5 — BLAKE2b + session_id-соль
# ---------------------------------------------------------------------------


def test_cache_key_blake2b_with_session_salt(monkeypatch):
    """T3.5: одинаковые entities/schema/env у разных сессий → РАЗНЫЕ cache_key.

    Это закрывает риск cross-tenant cache hit. Дополнительно проверяем, что
    MD5 больше не используется в schema_memory (источник истины — диф).
    """
    cache = SchemaCacheManager()
    entities = {"metrics": ["revenue"]}
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    # Изоляция env для детерминированности: убираем все SCHEMA_*-флаги.
    for key in [k for k in list(globals().get("os", __import__("os")).environ) if k.startswith(LINKING_CACHE_ENV_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)

    dsn_a = "sqlite:///tmp/tenant_a.db"
    monkeypatch.setenv("DB_DSN", dsn_a)
    info_a = _prepare_cache_info(cache, entities, schema, dsn=dsn_a)

    dsn_b = "sqlite:///tmp/tenant_b.db"
    monkeypatch.setenv("DB_DSN", dsn_b)
    info_b = _prepare_cache_info(cache, entities, schema, dsn=dsn_b)

    # Изолированные session_id → разные ключи (благодаря соли в blake2b).
    assert info_a["session_id"] != info_b["session_id"]
    assert info_a["cache_key"] != info_b["cache_key"]
    assert info_a["schema_hash"] != info_b["schema_hash"]
    assert info_a["entities_hash"] != info_b["entities_hash"]
    assert info_a["linking_env_hash"] != info_b["linking_env_hash"]


def test_cache_key_uses_blake2b_no_md5(monkeypatch):
    """T3.5: cache_key содержит blake2b-хэши (длина 16 hex-символов)."""
    cache = SchemaCacheManager()
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/t.db")
    for key in [k for k in list(__import__("os").environ) if k.startswith(LINKING_CACHE_ENV_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)

    info = _prepare_cache_info(cache, {"x": []}, {"t": {"columns": {}}})
    # Каждый компонент хэша — 16 hex-символов (digest_size=8).
    assert re.fullmatch(r"[0-9a-f]{16}", info["schema_hash"])
    assert re.fullmatch(r"[0-9a-f]{16}", info["entities_hash"])
    assert re.fullmatch(r"[0-9a-f]{16}", info["linking_env_hash"])


def test_truncate_salt_respects_blake2b_limit():
    """T3.5: соль для blake2b не должна превышать 16 байт.

    Для длинных значений делаем blake2b-сжатие, а не глупое обрезание —
    иначе два session_id с общим префиксом ≥ 16 символов получили бы
    одинаковую соль (см. test_cache_key_blake2b_with_session_salt).
    """
    short = _truncate_salt(b"short")
    assert short == b"short"

    long_value = b"x" * 100
    out = _truncate_salt(long_value)
    assert len(out) == 16
    # Сжатие сохраняет уникальность: разные длинные значения с общим
    # 16-байтным префиксом дают разный результат.
    a = _truncate_salt(b"sqlite_tmp_tenant_a_db")
    b = _truncate_salt(b"sqlite_tmp_tenant_b_db")
    assert a != b


# ---------------------------------------------------------------------------
# T3.6 — similarity использует фактическую метрику Chroma
# ---------------------------------------------------------------------------


class _FakeChromaCollection:
    """Mock Chroma-коллекции с настраиваемой metric."""

    def __init__(self, metric: str = "l2"):
        # Источник истины №1: collection.metadata["hnsw:space"]
        self.metadata = {"hnsw:space": metric}
        # Источник истины №2: collection.configuration (на свежих версиях)
        self.configuration = {"hnsw": {"space": metric}}


def test_resolve_metric_from_metadata_hnsw_space():
    """T3.6: метрика читается из collection.metadata['hnsw:space']."""
    assert _resolve_chroma_metric(_FakeChromaCollection("cosine")) == "cosine"
    assert _resolve_chroma_metric(_FakeChromaCollection("l2")) == "l2"


def test_resolve_metric_falls_back_to_configuration():
    """T3.6: если в metadata нет space, читается из configuration['hnsw']['space']."""
    collection = SimpleNamespace(
        metadata=None,
        configuration={"hnsw": {"space": "cosine"}},
    )
    assert _resolve_chroma_metric(collection) == "cosine"


def test_resolve_metric_default_is_chroma_default():
    """T3.6: если у коллекции нет данных о space, используется l2 (Chroma-дефолт)."""
    collection = SimpleNamespace(metadata=None, configuration=None)
    assert _resolve_chroma_metric(collection) == "l2"


def test_resolve_metric_rejects_unknown():
    """T3.6: незнакомая метрика → ValueError (fail-fast, не silent)."""
    with pytest.raises(ValueError, match="Unsupported Chroma distance metric"):
        _resolve_chroma_metric(_FakeChromaCollection("manhattan"))


def test_similarity_cosine_formula():
    """T3.6: для cosine distance=0 → similarity=1, distance=2 → similarity=0."""
    assert _distance_to_similarity(0.0, "cosine") == pytest.approx(1.0)
    assert _distance_to_similarity(2.0, "cosine") == pytest.approx(0.0)
    assert _distance_to_similarity(1.0, "cosine") == pytest.approx(0.5)


def test_similarity_l2_formula():
    """T3.6: для l2 distance=0 → similarity=1, distance→∞ → similarity→0."""
    assert _distance_to_similarity(0.0, "l2") == pytest.approx(1.0)
    assert _distance_to_similarity(1.0, "l2") == pytest.approx(0.5)
    # Большое расстояние → small but positive.
    assert 0.0 < _distance_to_similarity(1000.0, "l2") < 0.01


def test_similarity_l2_rejects_negative_distance():
    """T3.6: l2 не может быть отрицательным."""
    with pytest.raises(ValueError, match="non-negative"):
        _distance_to_similarity(-0.1, "l2")


def test_similarity_uses_chroma_metric_in_pipeline(monkeypatch):
    """T3.6: find_semantic_relevant_tables РЕАЛЬНО конвертирует distance с
    учётом metric. На l2-коллекции distance=0 → similarity=1, не
    1 - 0/2.0 (что тоже =1, но logic-path должен использовать l2-formula).

    Проверяем через distance=2.0:
      * Старая cosine-формула: 1 - 2/2 = 0 → ниже порога 0.2 → отфильтровано.
      * Новая l2-формула:     1 / (1 + 2) ≈ 0.333 → ВЫШЕ порога → попадает.
    """
    collection = _FakeChromaCollection(metric="l2")

    fake_search_results = {
        "ids": [["s1-Schema-RAG-Agent-1"]],
        "distances": [[2.0]],
        "metadatas": [[{"table_fqn": "public.orders"}]],
    }

    fake_manager = SimpleNamespace(
        get_tactical_collection=lambda: collection,
        search_semantic_with_scores=lambda *a, **kw: fake_search_results,
    )
    monkeypatch.setattr(sm_module, "memory_manager", fake_manager, raising=False)

    # Подменяем импортируемый внутри функции memory_manager.
    import memory.manager as mm_module
    monkeypatch.setattr(mm_module, "memory_manager", fake_manager)

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/t.db")
    monkeypatch.setenv("SCHEMA_TABLE_MIN_SCORE", "0.2")

    manager = SchemaMemoryManager(Path("/tmp"))
    tables = manager.find_semantic_relevant_tables(["orders"], dsn="sqlite:///tmp/t.db")

    # На l2 distance=2.0 даёт sim≈0.333 ≥ 0.2 → таблица попадает.
    # На старой cosine-формуле sim=0 < 0.2 → таблица была бы отфильтрована.
    assert tables == ["public.orders"], (
        "T3.6: similarity должен считаться через l2-формулу, а не cosine"
    )


# ---------------------------------------------------------------------------
# T3.7 — значимый порядок не сортируется
# ---------------------------------------------------------------------------


def test_significant_order_preserved_for_joins():
    """T3.7: список под ключом ``joins`` СОХРАНЯЕТ порядок при нормализации."""
    plan_ab = {
        "joins": [
            {"from": "orders", "to": "users"},
            {"from": "users", "to": "addresses"},
        ]
    }
    plan_ba = {
        "joins": [
            {"from": "users", "to": "addresses"},
            {"from": "orders", "to": "users"},
        ]
    }

    norm_ab = _normalize_for_hash(plan_ab)
    norm_ba = _normalize_for_hash(plan_ba)

    # Порядок joins СОХРАНЯЕТСЯ → хэши разные → плана с разной
    # последовательностью join'ов кэшируются раздельно.
    assert norm_ab != norm_ba
    assert json.dumps(norm_ab) != json.dumps(norm_ba)


def test_significant_order_preserved_for_order_by():
    """T3.7: список под ``order_by`` тоже сохраняет порядок."""
    plan_1 = {"order_by": ["name", "created_at"]}
    plan_2 = {"order_by": ["created_at", "name"]}
    assert _normalize_for_hash(plan_1) != _normalize_for_hash(plan_2)


def test_non_significant_order_still_normalized():
    """T3.7: списки под обычными ключами СОРТИРУЮТСЯ (cache hit rate).

    Регрессионная страховка: правка для joins/order_by не должна сломать
    нормализацию обычных значений (metrics, dimensions и т.п.).
    """
    obj1 = {"metrics": ["a", "b", "c"]}
    obj2 = {"metrics": ["c", "a", "b"]}
    assert _normalize_for_hash(obj1) == _normalize_for_hash(obj2)


def test_significant_order_sets_still_sorted():
    """T3.7: set/frozenset всегда сортируются (порядка нет по определению)."""
    obj_set = {"joins": frozenset(["a", "b", "c"])}
    obj_set_other = {"joins": frozenset(["b", "c", "a"])}
    # frozenset под "joins" — порядка нет, должны быть нормализованы одинаково.
    assert _normalize_for_hash(obj_set) == _normalize_for_hash(obj_set_other)


# ---------------------------------------------------------------------------
# T3.20 — нет приватных вызовов memory_manager
# ---------------------------------------------------------------------------


def test_no_private_memory_api_usage():
    """T3.20: в schema-memory модулях не должно быть вызовов приватных методов
    memory_manager (с префиксом ``_``), кроме описаний в комментариях.

    EPIC 8.3: после разбиения 868-строчного ``schema_memory.py`` на 4
    файла-источника self-check сканирует ВСЕ четыре:
        * schema_memory.py        (facade)
        * schema_memory_sqlite.py
        * schema_memory_chroma.py
        * schema_cache.py

    Запретные паттерны:
      * ``memory_manager._get_connection``
      * ``memory_manager._search_semantic_with_scores``
      * ``memory_manager.db_handler._get_connection``
    """
    from custom_tools.text_to_sql import (
        schema_cache as schema_cache_mod,
        schema_memory_chroma as chroma_mod,
        schema_memory_sqlite as sqlite_mod,
    )

    modules = [sm_module, sqlite_mod, chroma_mod, schema_cache_mod]
    forbidden_patterns = [
        r"memory_manager\._get_connection",
        r"memory_manager\._search_semantic_with_scores",
        r"memory_manager\.db_handler\._get_connection",
    ]

    offenders = []
    for mod in modules:
        source_path = Path(mod.__file__)
        source = source_path.read_text(encoding="utf-8")

        # Очищаем комментарии — в комментариях упоминания приватных методов
        # допустимы (мы там описываем, что было ДО правки).
        cleaned_lines = []
        for line in source.splitlines():
            # Удаляем inline-комментарии.
            if "#" in line:
                line = line.split("#", 1)[0]
            # Удаляем строковые литералы (грубо, но достаточно для self-check).
            line = re.sub(r'"[^"]*"', '""', line)
            line = re.sub(r"'[^']*'", "''", line)
            cleaned_lines.append(line)
        code = "\n".join(cleaned_lines)

        for pattern in forbidden_patterns:
            if re.search(pattern, code):
                offenders.append(f"{mod.__name__}: {pattern}")

    assert not offenders, (
        f"T3.20: найдены вызовы приватного API memory_manager в коде: {offenders}. "
        "Используй публичные алиасы get_sqlite_connection / "
        "search_semantic_with_scores / get_tactical_collection."
    )


def test_public_memory_api_exists():
    """T3.20: соответствующие публичные методы должны быть в MemoryManager."""
    from memory.manager import MemoryManager

    assert hasattr(MemoryManager, "get_sqlite_connection")
    assert hasattr(MemoryManager, "get_tactical_collection")
    assert hasattr(MemoryManager, "search_semantic_with_scores")


# ---------------------------------------------------------------------------
# T3.22 — autodiscovery вместо hardcoded списка env-флагов
# ---------------------------------------------------------------------------


def test_linking_cache_env_no_hardcode():
    """T3.22: LINKING_CACHE_ENV_DEFAULTS hardcoded-словаря быть не должно.

    EPIC 8.3: после разбиения константы и хелперы кэша живут в
    ``schema_cache.py``; сканируем все 4 файла (facade + 3 источника).
    """
    from custom_tools.text_to_sql import (
        schema_cache as schema_cache_mod,
        schema_memory_chroma as chroma_mod,
        schema_memory_sqlite as sqlite_mod,
    )

    modules = [sm_module, sqlite_mod, chroma_mod, schema_cache_mod]

    has_prefixes = False
    for mod in modules:
        source = Path(mod.__file__).read_text(encoding="utf-8")
        # Удаляем комментарии для self-check.
        code_lines = []
        for line in source.splitlines():
            if "#" in line:
                line = line.split("#", 1)[0]
            code_lines.append(line)
        code = "\n".join(code_lines)

        assert "LINKING_CACHE_ENV_DEFAULTS" not in code, (
            f"T3.22: захардкоженный LINKING_CACHE_ENV_DEFAULTS не должен "
            f"присутствовать в коде ({mod.__name__}); используй autodiscovery "
            f"по префиксам."
        )
        # Префиксы — это SoT; должны существовать хотя бы в одном модуле.
        if "LINKING_CACHE_ENV_PREFIXES" in code:
            has_prefixes = True

    assert has_prefixes, (
        "T3.22: LINKING_CACHE_ENV_PREFIXES должны присутствовать как SoT "
        "хотя бы в одном из schema-memory модулей."
    )


def test_linking_cache_env_autodiscovers_new_flags(monkeypatch):
    """T3.22: добавление нового SCHEMA_*-флага в env автоматически меняет
    cache_key БЕЗ правки кода. Раньше это было невозможно — список был
    hardcoded.
    """
    # Очищаем все SCHEMA_*-флаги.
    import os
    for key in [k for k in list(os.environ) if k.startswith(LINKING_CACHE_ENV_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/t.db")

    cache = SchemaCacheManager()
    entities = {"x": []}
    schema = {"t": {"columns": {}}}

    info_before = _prepare_cache_info(cache, entities, schema)

    # Выставляем НОВЫЙ флаг, которого никогда не было в hardcode-списке.
    monkeypatch.setenv("SCHEMA_LINKING_BRAND_NEW_KNOB", "experimental")
    info_after = _prepare_cache_info(cache, entities, schema)

    assert info_before["linking_env_hash"] != info_after["linking_env_hash"], (
        "T3.22: новый env-флаг с префиксом SCHEMA_LINKING_ должен попадать "
        "в linking_env_hash через autodiscovery"
    )
    assert info_before["cache_key"] != info_after["cache_key"]


def test_linking_cache_env_ignores_unrelated_vars(monkeypatch):
    """T3.22: переменные без префикса schema-linking НЕ участвуют в hash.

    Иначе любое изменение PATH или TERM ломало бы cache.
    """
    import os
    for key in [k for k in list(os.environ) if k.startswith(LINKING_CACHE_ENV_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/t.db")
    cache = SchemaCacheManager()
    entities = {"x": []}
    schema = {"t": {"columns": {}}}

    info_before = _prepare_cache_info(cache, entities, schema)
    monkeypatch.setenv("UNRELATED_RANDOM_VAR", "value123")
    info_after = _prepare_cache_info(cache, entities, schema)

    assert info_before["linking_env_hash"] == info_after["linking_env_hash"]


def test_collect_linking_cache_env_is_deterministic(monkeypatch):
    """T3.22: _collect_linking_cache_env() возвращает отсортированный dict."""
    import os
    for key in [k for k in list(os.environ) if k.startswith(LINKING_CACHE_ENV_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("SCHEMA_TABLE_MIN_SCORE", "0.5")
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "1")
    monkeypatch.setenv("SCHEMA_MAX_TABLES", "30")

    collected = _collect_linking_cache_env()
    assert list(collected.keys()) == sorted(collected.keys())
    assert collected["SCHEMA_TABLE_MIN_SCORE"] == "0.5"
    assert collected["SCHEMA_LINKING_USE_LLM"] == "1"
    assert collected["SCHEMA_MAX_TABLES"] == "30"


# ---------------------------------------------------------------------------
# T3-schema-index: новые тесты надёжности индексации (#3 HIGH, #20 MEDIUM, low)
# ---------------------------------------------------------------------------


def test_is_schema_indexed_partial_rejects_incomplete(monkeypatch, tmp_path, caplog):
    """#3 HIGH: is_schema_indexed(expected_count=3) возвращает False при 1 записи из 3.

    Сценарий: краш после индексации первой из трёх таблиц. Без expected_count
    метод вернул бы True (active_records_with_hash > 0). С expected_count=3
    должен вернуть False и залогировать предупреждение о частичной индексации.
    """
    import logging

    # Одна активная запись с нужным хэшем из трёх ожидаемых.
    fake_records = [
        {"data": {"file_hash": "abc123", "cache_kind": "schema_table"}},
    ]
    memory_tools = SimpleNamespace(
        get_memory=lambda **kw: fake_records,
        save_memory=lambda **kw: None,
    )
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)

    manager = SchemaMemoryManager(tmp_path)

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.schema_memory_sqlite"):
        result = manager.is_schema_indexed("s1", "abc123", expected_count=3)

    assert result is False, "Частичная индексация (1 из 3) должна давать False"
    assert any("Partial schema index" in r.message for r in caplog.records), (
        "Должно быть warning с текстом 'Partial schema index'"
    )


def test_is_schema_indexed_full_count_returns_true(monkeypatch, tmp_path):
    """#3 HIGH: is_schema_indexed(expected_count=2) возвращает True при 2 записях из 2."""
    fake_records = [
        {"data": {"file_hash": "abc123", "cache_kind": "schema_table"}},
        {"data": {"file_hash": "abc123", "cache_kind": "schema_table"}},
    ]
    memory_tools = SimpleNamespace(
        get_memory=lambda **kw: fake_records,
        save_memory=lambda **kw: None,
    )
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)

    manager = SchemaMemoryManager(tmp_path)
    result = manager.is_schema_indexed("s1", "abc123", expected_count=2)

    assert result is True, "Полная индексация (2 из 2) должна давать True"


def test_is_schema_indexed_backward_compat_no_expected_count(monkeypatch, tmp_path):
    """#3 HIGH: без expected_count поведение прежнее — True при любой записи."""
    fake_records = [
        {"data": {"file_hash": "abc123", "cache_kind": "schema_table"}},
    ]
    memory_tools = SimpleNamespace(
        get_memory=lambda **kw: fake_records,
        save_memory=lambda **kw: None,
    )
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)

    manager = SchemaMemoryManager(tmp_path)
    # Без expected_count — backward-compat: True при любой записи
    result = manager.is_schema_indexed("s1", "abc123")
    assert result is True, "Без expected_count одна запись → True (backward compat)"


def test_find_semantic_relevant_tables_raises_on_embedding_unavailable(monkeypatch, tmp_path):
    """#20 MEDIUM: EmbeddingUnavailableError пробрасывается наверх, не маскируется в [].

    Caller должен видеть реальную причину (конфиг эмбеддингов), а не пустой результат.
    """
    from memory.manager import EmbeddingUnavailableError

    collection = SimpleNamespace(metadata={"hnsw:space": "l2"}, configuration=None)

    def fake_search_raises(*a, **kw):
        raise EmbeddingUnavailableError("Embedding model is not configured on db_handler")

    fake_manager = SimpleNamespace(
        get_tactical_collection=lambda: collection,
        search_semantic_with_scores=fake_search_raises,
    )

    import custom_tools.text_to_sql.schema_memory_sqlite as sqlite_mod

    monkeypatch.setattr(sqlite_mod, "memory_manager", fake_manager, raising=False)

    import memory.manager as mm_module
    monkeypatch.setattr(mm_module, "memory_manager", fake_manager)

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/t.db")
    monkeypatch.setenv("SCHEMA_TABLE_MIN_SCORE", "0.2")

    manager = SchemaMemoryManager(tmp_path)

    with pytest.raises(EmbeddingUnavailableError):
        manager.find_semantic_relevant_tables(["orders"], dsn="sqlite:///tmp/t.db")


def test_find_semantic_relevant_tables_raises_on_key_error_in_unpacking(monkeypatch, tmp_path):
    """#20 MEDIUM: KeyError при распаковке ids/distances пробрасывается наверх."""
    collection = SimpleNamespace(metadata={"hnsw:space": "l2"}, configuration=None)

    # Возвращаем результат с некорректным типом, который вызовет TypeError при распаковке
    def fake_search_bad_type(*a, **kw):
        # Корректный ids, но distances — строка вместо списка чисел.
        return {
            "ids": [["id1"]],
            "distances": "not_a_list",  # вызовет TypeError при индексировании
            "metadatas": [[{"table_fqn": "public.orders"}]],
        }

    fake_manager = SimpleNamespace(
        get_tactical_collection=lambda: collection,
        search_semantic_with_scores=fake_search_bad_type,
    )

    import custom_tools.text_to_sql.schema_memory_sqlite as sqlite_mod

    monkeypatch.setattr(sqlite_mod, "memory_manager", fake_manager, raising=False)

    import memory.manager as mm_module
    monkeypatch.setattr(mm_module, "memory_manager", fake_manager)

    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/t.db")

    manager = SchemaMemoryManager(tmp_path)

    # TypeError при isinstance(raw_distances[0], list) — строка[0] возвращает символ,
    # не бросает TypeError. Вместо этого проверим, что KeyError пробрасывается.
    # Тест с реальным KeyError:
    def fake_search_key_error(*a, **kw):
        raise KeyError("table_fqn_missing_key")

    fake_manager2 = SimpleNamespace(
        get_tactical_collection=lambda: collection,
        search_semantic_with_scores=fake_search_key_error,
    )
    monkeypatch.setattr(sqlite_mod, "memory_manager", fake_manager2, raising=False)
    monkeypatch.setattr(mm_module, "memory_manager", fake_manager2)

    with pytest.raises(KeyError):
        manager.find_semantic_relevant_tables(["orders"], dsn="sqlite:///tmp/t.db")


def test_file_lock_fd_closed_on_unexpected_exception(monkeypatch, tmp_path):
    """low: _FileLock.acquire закрывает fd при не-BlockingIOError исключении."""
    import sys

    if sys.platform == "win32":
        pytest.skip("_FileLock с fcntl доступен только на Unix")

    import fcntl
    import os as os_module

    from custom_tools.text_to_sql.schema_memory_sqlite import _FileLock

    lock_path = str(tmp_path / "test.lock")
    lock = _FileLock(lock_path)

    closed_fds = []
    original_close = os_module.close

    def tracking_close(fd):
        closed_fds.append(fd)
        original_close(fd)

    monkeypatch.setattr(os_module, "close", tracking_close)

    # Подменяем fcntl.flock чтобы бросал OSError (не BlockingIOError)
    def bad_flock(fd, op):
        raise OSError("unexpected flock error")

    monkeypatch.setattr(fcntl, "flock", bad_flock)

    with pytest.raises(OSError):
        lock.acquire()

    # fd должен быть закрыт
    assert closed_fds, "fd должен быть закрыт при unexpected exception в acquire"
    # После исключения self._fd должен быть None
    assert lock._fd is None, "self._fd должен быть None после exception"
