import os
import pytest
from custom_tools.sql_tools import _get_schema_version


def test_schema_version_from_env(monkeypatch):
    """Тест получения schema_version из переменной окружения."""
    monkeypatch.setenv("SCHEMA_VERSION", "custom_v2.1")
    version = _get_schema_version()
    assert version == "custom_v2.1"


def test_schema_version_fallback_hash(monkeypatch):
    """Тест fallback на хэш схемы при отсутствии SCHEMA_VERSION."""
    monkeypatch.delenv("SCHEMA_VERSION", raising=False)
    
    # Мокируем schema_info для предсказуемого хэша
    test_schema = {
        "orders": {"id": {"type": "INTEGER"}, "amount": {"type": "DECIMAL"}},
        "regions": {"id": {"type": "INTEGER"}, "name": {"type": "TEXT"}}
    }
    
    version = _get_schema_version(test_schema)
    
    # Проверяем, что версия - это полный хэш (функция возвращает полный hexdigest)
    import hashlib
    import json
    expected_hash = hashlib.md5(json.dumps(test_schema, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    assert version == expected_hash


def test_schema_version_empty_schema(monkeypatch):
    """Тест с пустой схемой."""
    monkeypatch.delenv("SCHEMA_VERSION", raising=False)
    
    # Пустая схема и None возвращают "unknown"
    version = _get_schema_version({})
    assert version == "unknown"
    
    version_none = _get_schema_version(None)
    assert version_none == "unknown"


def test_schema_version_env_priority(monkeypatch):
    """Тест приоритета переменной окружения над хэшем схемы."""
    monkeypatch.setenv("SCHEMA_VERSION", "env_priority")
    
    test_schema = {"table": {"col": {"type": "TEXT"}}}
    version = _get_schema_version(test_schema)
    
    # Переменная окружения должна иметь приоритет
    assert version == "env_priority"
