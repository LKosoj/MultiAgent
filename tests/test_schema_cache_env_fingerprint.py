"""W8-T1 / W8-T7: stale schema-cache по env-fingerprint и fast schema fingerprint.

Покрывают:
  * env-fingerprint меняется при смене DB_DSN host/port/db и любого
    TEXT_TO_SQL_*_PROFILE → cache_key другой → load возвращает miss (None).
  * env-fingerprint НЕ меняется при ротации credentials в DSN (user/password).
  * fast schema fingerprint реагирует на структурную мутацию (добавление
    таблицы/колонки), но не требует полной сериализации схемы.
  * load_from_cache отдаёт hit, когда env не менялся.
"""
from __future__ import annotations

import re

import pytest

from custom_tools.text_to_sql.schema_cache import (
    SchemaCacheManager,
    _compute_env_fingerprint,
    _compute_schema_fingerprint,
    _dsn_host_port_db,
    LINKING_CACHE_ENV_PREFIXES,
)


# ---------------------------------------------------------------------------
# Хелпер: чистая env между тестами (TEXT_TO_SQL_* + SCHEMA_*).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_t2s_env(monkeypatch):
    """Изолируем тест от global env: убираем TEXT_TO_SQL_* и SCHEMA_* флаги."""
    import os

    for key in list(os.environ.keys()):
        if key.startswith("TEXT_TO_SQL_") or key.startswith(LINKING_CACHE_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    yield


def _prepare_cache_info(cache, entities, schema, dsn: str | None = None):
    import os

    return cache.prepare_cache_info(entities, schema, dsn=dsn or os.environ["DB_DSN"])


# ---------------------------------------------------------------------------
# _dsn_host_port_db: маска credentials
# ---------------------------------------------------------------------------


def test_dsn_host_port_db_strips_credentials():
    """W8-T1: user/password из DSN не должны попадать в env-fingerprint."""
    with_creds = _dsn_host_port_db("postgresql://alice:s3cret@db.example.com:5432/orders")
    no_creds = _dsn_host_port_db("postgresql://db.example.com:5432/orders")
    # Без user/password identity-строка идентична.
    assert with_creds == no_creds
    assert "alice" not in with_creds
    assert "s3cret" not in with_creds


def test_dsn_host_port_db_handles_sqlite_file_path():
    """sqlite:///path/to.db — host пуст, путь хранится в db."""
    result = _dsn_host_port_db("sqlite:///tmp/tenant_a.db")
    assert "tmp/tenant_a.db" in result


def test_dsn_host_port_db_libpq_strips_credentials_and_tracks_identity():
    """libpq keyword DSN: user/password не влияют, host/port/dbname влияют."""
    alice = _dsn_host_port_db(
        "host=db.example.com port=5432 user=alice password=one dbname=orders"
    )
    bob = _dsn_host_port_db(
        "host=db.example.com port=5432 user=bob password=two dbname=orders"
    )
    other_db = _dsn_host_port_db(
        "host=db.example.com port=5432 user=bob password=two dbname=analytics"
    )

    assert alice == bob
    assert alice != other_db
    assert "alice" not in alice
    assert "password" not in alice
    assert "db_example_com" in alice
    assert "orders" in alice


def test_dsn_host_port_db_odbc_uses_server_database_and_strips_credentials():
    """ODBC DSN: Server/Database задают identity, UID/PWD не попадают в hash input."""
    a = _dsn_host_port_db(
        "Driver={ODBC Driver 17};Server=db.example.com;Database=orders;UID=alice;PWD=one"
    )
    b = _dsn_host_port_db(
        "Driver={ODBC Driver 17};Server=db.example.com;Database=orders;UID=bob;PWD=two"
    )
    other_server = _dsn_host_port_db(
        "Driver={ODBC Driver 17};Server=replica.example.com;Database=orders;UID=bob;PWD=two"
    )

    assert a == b
    assert a != other_server
    assert "alice" not in a
    assert "pwd" not in a
    assert "db_example_com" in a
    assert "orders" in a


def test_dsn_host_port_db_pyodbc_url_uses_decoded_odbc_identity():
    """SQLAlchemy pyodbc URL без host/path должен брать identity из odbc_connect."""
    a = _dsn_host_port_db(
        "mssql+pyodbc:///?odbc_connect=Driver%3D%7BODBC+Driver+17%7D%3B"
        "Server%3Ddb1.example.com%3BDatabase%3Dorders%3BUID%3Dalice%3BPWD%3Done"
    )
    b = _dsn_host_port_db(
        "mssql+pyodbc:///?odbc_connect=Driver%3D%7BODBC+Driver+17%7D%3B"
        "Server%3Ddb1.example.com%3BDatabase%3Dorders%3BUID%3Dbob%3BPWD%3Dtwo"
    )
    other_db = _dsn_host_port_db(
        "mssql+pyodbc:///?odbc_connect=Driver%3D%7BODBC+Driver+17%7D%3B"
        "Server%3Ddb1.example.com%3BDatabase%3Danalytics%3BUID%3Dbob%3BPWD%3Dtwo"
    )

    assert a == b
    assert a != other_db
    assert "db1_example_com" in a
    assert "orders" in a
    assert "alice" not in a


# ---------------------------------------------------------------------------
# _compute_env_fingerprint: контракт
# ---------------------------------------------------------------------------


def test_env_fingerprint_stable_for_same_env(monkeypatch):
    """Идентичный env → идентичный fingerprint (детерминированность)."""
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@host:5432/db")
    monkeypatch.setenv("TEXT_TO_SQL_NLU_PROFILE", "muni_ru")
    fp1 = _compute_env_fingerprint()
    fp2 = _compute_env_fingerprint()
    assert fp1 == fp2
    # sha256 hex = 64 символа.
    assert re.fullmatch(r"[0-9a-f]{64}", fp1)


def test_env_fingerprint_invariant_to_credentials_rotation(monkeypatch):
    """W8-T1: смена user/password в DSN при том же host:port:db → fingerprint НЕ меняется."""
    monkeypatch.setenv("DB_DSN", "postgresql://alice:s3cret@db.example.com:5432/orders")
    fp_with_creds = _compute_env_fingerprint()
    monkeypatch.setenv("DB_DSN", "postgresql://bob:newpass@db.example.com:5432/orders")
    fp_other_creds = _compute_env_fingerprint()
    assert fp_with_creds == fp_other_creds


def test_env_fingerprint_invariant_to_libpq_credentials_rotation():
    """Explicit runtime libpq DSN не должен инвалидировать cache при ротации credentials."""
    alice = "host=db.example.com port=5432 user=alice password=one dbname=orders"
    bob = "host=db.example.com port=5432 user=bob password=two dbname=orders"
    other_db = "host=db.example.com port=5432 user=bob password=two dbname=analytics"

    assert _compute_env_fingerprint(dsn=alice) == _compute_env_fingerprint(dsn=bob)
    assert _compute_env_fingerprint(dsn=bob) != _compute_env_fingerprint(dsn=other_db)


def test_env_fingerprint_invariant_to_odbc_credentials_rotation():
    """Explicit runtime ODBC DSN учитывает Server/Database, но не UID/PWD."""
    alice = "Driver={ODBC Driver 17};Server=db.example.com;Database=orders;UID=alice;PWD=one"
    bob = "Driver={ODBC Driver 17};Server=db.example.com;Database=orders;UID=bob;PWD=two"
    other_server = "Driver={ODBC Driver 17};Server=replica.example.com;Database=orders;UID=bob;PWD=two"

    assert _compute_env_fingerprint(dsn=alice) == _compute_env_fingerprint(dsn=bob)
    assert _compute_env_fingerprint(dsn=bob) != _compute_env_fingerprint(dsn=other_server)


def test_env_fingerprint_changes_for_different_pyodbc_url_database():
    """mssql+pyodbc:///?odbc_connect=... не должен схлопываться в scheme:///."""
    orders = (
        "mssql+pyodbc:///?odbc_connect=Driver%3D%7BODBC+Driver+17%7D%3B"
        "Server%3Ddb1.example.com%3BDatabase%3Dorders%3BUID%3Dalice%3BPWD%3Done"
    )
    analytics = (
        "mssql+pyodbc:///?odbc_connect=Driver%3D%7BODBC+Driver+17%7D%3B"
        "Server%3Ddb1.example.com%3BDatabase%3Danalytics%3BUID%3Dalice%3BPWD%3Bone"
    )

    assert _compute_env_fingerprint(dsn=orders) != _compute_env_fingerprint(dsn=analytics)


def test_env_fingerprint_changes_on_dsn_host_change(monkeypatch):
    """W8-T1: смена DSN host → fingerprint меняется → миграция на другой кластер инвалидирует кэш."""
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@host_a:5432/db")
    fp_a = _compute_env_fingerprint()
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@host_b:5432/db")
    fp_b = _compute_env_fingerprint()
    assert fp_a != fp_b


def test_env_fingerprint_changes_on_profile_change(monkeypatch):
    """W8-T1: смена TEXT_TO_SQL_*_PROFILE → fingerprint меняется."""
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@host:5432/db")
    monkeypatch.setenv("TEXT_TO_SQL_MAIN_TABLE_SCORING_PROFILE", "default")
    fp_default = _compute_env_fingerprint()
    monkeypatch.setenv("TEXT_TO_SQL_MAIN_TABLE_SCORING_PROFILE", "muni_ru")
    fp_muni = _compute_env_fingerprint()
    assert fp_default != fp_muni


def test_env_fingerprint_changes_on_nlu_profile(monkeypatch):
    """W8-T1: TEXT_TO_SQL_NLU_PROFILE тоже инвалидирует."""
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@host:5432/db")
    fp_unset = _compute_env_fingerprint()
    monkeypatch.setenv("TEXT_TO_SQL_NLU_PROFILE", "muni_ru")
    fp_set = _compute_env_fingerprint()
    assert fp_unset != fp_set


def test_env_fingerprint_changes_on_significance_profile(monkeypatch):
    """W8-T1: TEXT_TO_SQL_SIGNIFICANCE_PROFILE тоже инвалидирует."""
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@host:5432/db")
    fp_unset = _compute_env_fingerprint()
    monkeypatch.setenv("TEXT_TO_SQL_SIGNIFICANCE_PROFILE", "muni_ru")
    fp_set = _compute_env_fingerprint()
    assert fp_unset != fp_set


# ---------------------------------------------------------------------------
# SchemaCacheManager: cache_key включает env_fingerprint
# ---------------------------------------------------------------------------


def test_prepare_cache_info_includes_env_fingerprint(monkeypatch):
    """W8-T1: prepare_cache_info возвращает env_fingerprint в результирующем dict."""
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/tenant.db")
    cache = SchemaCacheManager()
    info = _prepare_cache_info(cache, {"x": []}, {"t": {"columns": {}}})
    assert "env_fingerprint" in info
    assert re.fullmatch(r"[0-9a-f]{64}", info["env_fingerprint"])


def test_prepare_cache_info_ignores_db_dsn_env_without_explicit_or_runtime_dsn(monkeypatch):
    """DB_DSN alone must not choose the tenant namespace for schema-linking cache."""
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/tenant.db")
    cache = SchemaCacheManager()

    with pytest.raises(ValueError, match="DSN is required"):
        cache.prepare_cache_info({"x": []}, {"t": {"columns": {}}})


def test_cache_key_changes_when_profile_changes(monkeypatch):
    """W8-T1: смена TEXT_TO_SQL_*_PROFILE между двумя вызовами → разный cache_key."""
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/tenant.db")
    cache = SchemaCacheManager()
    entities = {"metrics": ["revenue"]}
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    monkeypatch.setenv("TEXT_TO_SQL_MAIN_TABLE_SCORING_PROFILE", "default")
    info_default = _prepare_cache_info(cache, entities, schema)

    monkeypatch.setenv("TEXT_TO_SQL_MAIN_TABLE_SCORING_PROFILE", "muni_ru")
    info_muni = _prepare_cache_info(cache, entities, schema)

    assert info_default["cache_key"] != info_muni["cache_key"]
    assert info_default["env_fingerprint"] != info_muni["env_fingerprint"]


def test_cache_key_changes_when_dsn_host_changes(monkeypatch):
    """W8-T1: смена DB_DSN host → разный cache_key (и разный session_id)."""
    cache = SchemaCacheManager()
    entities = {"metrics": ["revenue"]}
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    dsn_a = "postgresql://u:p@host_a:5432/orders"
    monkeypatch.setenv("DB_DSN", dsn_a)
    info_a = _prepare_cache_info(cache, entities, schema, dsn=dsn_a)

    dsn_b = "postgresql://u:p@host_b:5432/orders"
    monkeypatch.setenv("DB_DSN", dsn_b)
    info_b = _prepare_cache_info(cache, entities, schema, dsn=dsn_b)

    assert info_a["cache_key"] != info_b["cache_key"]
    assert info_a["env_fingerprint"] != info_b["env_fingerprint"]


def test_cache_key_stable_when_only_credentials_change(monkeypatch):
    """W8-T1: ротация user/password в DSN при том же host:port:db → env_fingerprint сохраняется.

    Credentials не идентифицируют данные и не входят в env_fingerprint.
    """
    cache = SchemaCacheManager()
    entities = {"metrics": ["revenue"]}
    schema = {"orders": {"columns": {"amount": {"type": "DECIMAL"}}}}

    dsn_alice = "postgresql://alice:s3cret@host:5432/orders"
    monkeypatch.setenv("DB_DSN", dsn_alice)
    info_alice = _prepare_cache_info(cache, entities, schema, dsn=dsn_alice)

    dsn_bob = "postgresql://bob:newpass@host:5432/orders"
    monkeypatch.setenv("DB_DSN", dsn_bob)
    info_bob = _prepare_cache_info(cache, entities, schema, dsn=dsn_bob)

    # env_fingerprint идентичен — credentials не входят в identity.
    assert info_alice["env_fingerprint"] == info_bob["env_fingerprint"]


# ---------------------------------------------------------------------------
# W8-T7: fast schema fingerprint
# ---------------------------------------------------------------------------


def test_schema_fingerprint_stable_for_same_schema():
    """Идентичная схема → идентичный fingerprint."""
    schema = {
        "orders": {"columns": {"id": {"type": "INT"}, "amount": {"type": "DECIMAL"}}},
        "customers": {"columns": {"id": {"type": "INT"}, "name": {"type": "TEXT"}}},
    }
    fp1 = _compute_schema_fingerprint(schema)
    fp2 = _compute_schema_fingerprint(schema)
    assert fp1 == fp2
    assert re.fullmatch(r"[0-9a-f]{64}", fp1)


def test_schema_fingerprint_changes_on_table_addition():
    """Добавили таблицу → fingerprint поменялся (tables_count + names_hash)."""
    schema_a = {"orders": {"columns": {"id": {"type": "INT"}}}}
    schema_b = {
        "orders": {"columns": {"id": {"type": "INT"}}},
        "customers": {"columns": {"id": {"type": "INT"}}},
    }
    assert _compute_schema_fingerprint(schema_a) != _compute_schema_fingerprint(schema_b)


def test_schema_fingerprint_changes_on_max_column_count():
    """Изменили max column count → fingerprint поменялся."""
    schema_a = {"t": {"columns": {"c1": {}, "c2": {}}}}
    schema_b = {"t": {"columns": {"c1": {}, "c2": {}, "c3": {}}}}
    assert _compute_schema_fingerprint(schema_a) != _compute_schema_fingerprint(schema_b)


def test_schema_fingerprint_independent_of_key_order():
    """Порядок ключей в dict не влияет на fingerprint (sorted names)."""
    schema_a = {"a_table": {"columns": {}}, "b_table": {"columns": {}}}
    # Python 3.7+: dict сохраняет порядок вставки. Создаём в обратном порядке.
    schema_b = {"b_table": {"columns": {}}, "a_table": {"columns": {}}}
    assert _compute_schema_fingerprint(schema_a) == _compute_schema_fingerprint(schema_b)


def test_schema_fingerprint_handles_legacy_schema_format():
    """Legacy: колонки прямо в корне таблицы (без вложенного 'columns')."""
    # Должна вернуть валидный fingerprint без exception.
    schema_legacy = {
        "orders": {"id": {"type": "INT"}, "amount": {"type": "DECIMAL"}},
    }
    fp = _compute_schema_fingerprint(schema_legacy)
    assert re.fullmatch(r"[0-9a-f]{64}", fp)


def test_schema_fingerprint_returned_from_prepare_cache_info(monkeypatch):
    """W8-T7: prepare_cache_info возвращает schema_fingerprint."""
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/t.db")
    cache = SchemaCacheManager()
    info = _prepare_cache_info(cache, {"x": []}, {"t": {"columns": {"c1": {}}}})
    assert "schema_fingerprint" in info
    assert re.fullmatch(r"[0-9a-f]{64}", info["schema_fingerprint"])
    # schema_hash (legacy) тоже сохранён для backward-compat.
    assert "schema_hash" in info
    assert re.fullmatch(r"[0-9a-f]{16}", info["schema_hash"])  # blake2b digest_size=8


# ---------------------------------------------------------------------------
# Интеграционный: load_from_cache → miss при смене env
# ---------------------------------------------------------------------------


def test_load_from_cache_returns_miss_when_env_fingerprint_mismatch(monkeypatch):
    """W8-T1: запись с другим env_fingerprint → load возвращает None (miss), не corrupted.

    Эмулируем backend через monkeypatch get_memory: возвращаем запись с правильным
    cache_key/schema_version (как было бы при save), но с другим env_fingerprint
    (как если бы запись сделали при другом профиле).
    """
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")
    monkeypatch.setenv("TEXT_TO_SQL_NLU_PROFILE", "muni_ru")

    cache = SchemaCacheManager()
    info = _prepare_cache_info(cache, {"x": []}, {"t": {"columns": {}}})

    fake_record = {
        "data": {
            "cache_key": info["cache_key"],
            "schema_version": info["schema_version"],
            # Разный env_fingerprint — должно быть miss.
            "env_fingerprint": "deadbeef" * 8,
            "linking_result": {"linked_tables": ["t"]},
        }
    }

    # monkeypatch get_memory + memory_manager truthy.
    import memory.tools as memory_tools_mod
    import memory.manager as memory_manager_mod

    monkeypatch.setattr(memory_tools_mod, "get_memory", lambda **kwargs: [fake_record])
    monkeypatch.setattr(memory_manager_mod, "memory_manager", object())

    result = cache.load_from_cache(info)
    assert result is None  # miss, не raise


def test_load_from_cache_returns_hit_when_env_matches(monkeypatch):
    """W8-T1: запись с тем же env_fingerprint → hit."""
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")

    cache = SchemaCacheManager()
    info = _prepare_cache_info(cache, {"x": []}, {"t": {"columns": {}}})

    fake_record = {
        "data": {
            "cache_key": info["cache_key"],
            "schema_version": info["schema_version"],
            "env_fingerprint": info["env_fingerprint"],
            "linking_result": {"linked_tables": ["t"]},
        }
    }

    import memory.tools as memory_tools_mod
    import memory.manager as memory_manager_mod

    monkeypatch.setattr(memory_tools_mod, "get_memory", lambda **kwargs: [fake_record])
    monkeypatch.setattr(memory_manager_mod, "memory_manager", object())

    result = cache.load_from_cache(info)
    assert result == {"linked_tables": ["t"]}


def test_load_from_cache_backward_compat_without_env_fingerprint(monkeypatch):
    """Запись без env_fingerprint (legacy формат до W8-T1) → если cache_key совпадает, hit.

    На практике legacy-записи не будут совпадать по cache_key (формат суффикса
    изменился), но если вдруг совпали — backward-compat: missing env_fingerprint
    в data НЕ блокирует hit (мы не делаем strict-проверку на наличие поля).
    """
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")

    cache = SchemaCacheManager()
    info = _prepare_cache_info(cache, {"x": []}, {"t": {"columns": {}}})

    legacy_record = {
        "data": {
            "cache_key": info["cache_key"],
            "schema_version": info["schema_version"],
            # env_fingerprint отсутствует → не блокируем hit.
            "linking_result": {"linked_tables": ["t"]},
        }
    }

    import memory.tools as memory_tools_mod
    import memory.manager as memory_manager_mod

    monkeypatch.setattr(memory_tools_mod, "get_memory", lambda **kwargs: [legacy_record])
    monkeypatch.setattr(memory_manager_mod, "memory_manager", object())

    result = cache.load_from_cache(info)
    assert result == {"linked_tables": ["t"]}


# ---------------------------------------------------------------------------
# #18: save_to_cache деактивирует предыдущие активные schema_linking-записи
# ---------------------------------------------------------------------------


def test_save_to_cache_deactivates_old_schema_linking_records(monkeypatch):
    """#18: save_to_cache вызывает _deactivate_conflicting_records для старых записей.

    Эмулируем backend: get_sqlite_connection() возвращает фейковый conn с
    cursor на два старых шага (10 и 20). Проверяем, что _deactivate_conflicting_records
    вызван с правильными кортежами.
    """
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")

    cache = SchemaCacheManager()
    info = _prepare_cache_info(cache, {"x": []}, {"t": {"columns": {}}})

    saved_calls = []

    # Фейковый cursor с двумя старыми записями.
    class FakeCursor:
        def execute(self, sql, params):
            pass

        def fetchall(self):
            return [(10,), (20,)]

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    class FakeDbHandler:
        def get_connection(self):
            return FakeConn()

    class FakeMemoryManager:
        db_handler = FakeDbHandler()

        # Зеркалит реальный API: memory_manager.get_sqlite_connection()
        # делегирует в db_handler.get_connection() (см. memory/manager.py:252).
        def get_sqlite_connection(self):
            return self.db_handler.get_connection()

        def _deactivate_conflicting_records(self, conflicts):
            saved_calls.append(list(conflicts))

    # save_to_cache делает `from memory.manager import memory_manager` внутри метода,
    # поэтому патчим атрибут модуля memory.manager, который importlib вернёт при вызове.
    import memory.manager as memory_manager_mod
    import memory.tools as memory_tools_mod

    fake_mm = FakeMemoryManager()
    monkeypatch.setattr(memory_manager_mod, "memory_manager", fake_mm)
    monkeypatch.setattr(memory_tools_mod, "save_memory", lambda **kwargs: None)

    cache.save_to_cache(info, {"linked_tables": ["t"]})

    assert len(saved_calls) == 1
    assert set(saved_calls[0]) == {
        (info["session_id"], "Schema-RAG-Agent", 10),
        (info["session_id"], "Schema-RAG-Agent", 20),
    }


def test_save_to_cache_deactivation_error_does_not_block_save(monkeypatch):
    """#18: ошибка деактивации логируется, но сохранение новой записи продолжается."""
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")

    cache = SchemaCacheManager()
    info = _prepare_cache_info(cache, {"x": []}, {"t": {"columns": {}}})

    class BrokenDbHandler:
        def get_connection(self):
            raise OSError("db locked")

    class FakeMemoryManager:
        db_handler = BrokenDbHandler()

        def get_sqlite_connection(self):
            return self.db_handler.get_connection()

        def _deactivate_conflicting_records(self, conflicts):
            pass

    save_called = []

    import memory.manager as memory_manager_mod
    import memory.tools as memory_tools_mod

    monkeypatch.setattr(memory_manager_mod, "memory_manager", FakeMemoryManager())
    monkeypatch.setattr(memory_tools_mod, "save_memory", lambda **kwargs: save_called.append(1))

    # Не должно бросать исключение.
    cache.save_to_cache(info, {"linked_tables": ["t"]})

    # save_memory всё равно вызван.
    assert save_called == [1]


def test_save_to_cache_no_old_records_skips_deactivation(monkeypatch):
    """#18: если старых записей нет — _deactivate_conflicting_records не вызывается."""
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")

    cache = SchemaCacheManager()
    info = _prepare_cache_info(cache, {"x": []}, {"t": {"columns": {}}})

    deactivate_called = []

    class FakeCursor:
        def execute(self, sql, params):
            pass

        def fetchall(self):
            return []  # Нет старых записей.

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    class FakeDbHandler:
        def get_connection(self):
            return FakeConn()

    class FakeMemoryManager:
        db_handler = FakeDbHandler()

        def get_sqlite_connection(self):
            return self.db_handler.get_connection()

        def _deactivate_conflicting_records(self, conflicts):
            deactivate_called.append(conflicts)

    import memory.manager as memory_manager_mod
    import memory.tools as memory_tools_mod

    monkeypatch.setattr(memory_manager_mod, "memory_manager", FakeMemoryManager())
    monkeypatch.setattr(memory_tools_mod, "save_memory", lambda **kwargs: None)

    cache.save_to_cache(info, {"linked_tables": ["t"]})

    assert deactivate_called == []


# ---------------------------------------------------------------------------
# #19: _apply_memory_filtering и get_memory обходят LLM-summary для schema_linking
# ---------------------------------------------------------------------------


def test_apply_memory_filtering_bypasses_summarization_for_schema_linking():
    """#19: schema_linking с total_length>70000 возвращается as-is без LLM."""
    from memory.tools import _apply_memory_filtering

    records = [
        {"agent_name": "Schema-RAG-Agent", "step": i, "data": "x" * 100}
        for i in range(5)
    ]
    # total_length > SUMMARIZATION_THRESHOLD (70000)
    result = _apply_memory_filtering(
        records,
        query=None,
        total_length=80000,
        cache_kind="schema_linking",
        requesting_agent=None,
    )
    assert result is records  # возвращает тот же объект (as-is)


def test_apply_memory_filtering_still_summarizes_other_cache_kinds(monkeypatch):
    """#19: для cache_kind=None (не schema_linking) фильтрация срабатывает как обычно."""
    from memory.tools import _apply_memory_filtering

    summary_called = []

    import memory.tools as memory_tools_mod

    def fake_summary(records, query, total_length):
        summary_called.append(1)
        return [{"agent_name": "memory_summarizer", "step": 0, "data": "summary"}]

    monkeypatch.setattr(memory_tools_mod, "_create_memory_summary", fake_summary)

    records = [
        {"agent_name": "SomeAgent", "step": i, "data": "x" * 100}
        for i in range(5)
    ]
    _apply_memory_filtering(
        records,
        query="test",
        total_length=80000,
        cache_kind=None,  # не schema_linking — должна вызваться суммаризация
        requesting_agent=None,
    )
    assert summary_called == [1]
