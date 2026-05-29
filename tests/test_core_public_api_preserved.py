"""Contract-pinning тест для package custom_tools.text_to_sql.core (Phase 7).

Проверяет, что после декомпозиции core.py в package:
- все публичные функции остаются доступны на module-level фасада;
- singletons (nlu_processor, rag_searcher, sql_validator, schema_limiter)
  остаются доступны как атрибуты модуля;
- прямые импорты `from custom_tools.text_to_sql.core import <name>` работают
  и указывают на ту же функцию, что и атрибут фасада.
"""
from custom_tools.text_to_sql import core


PUBLIC_FUNCTIONS = [
    "natural_language_processing",
    "intent_extraction",
    "vector_db_search",
    "schema_linking",
    "sql_generation_plugin",
    "code_formatter",
    "sql_safety_check",
    "sql_explain",
    "secure_db_executor",
    "pii_masking",
    "audit_logger",
    "save_successful_sql",
    "purge_schema_linking_rag_cache",
]

SINGLETONS = ["nlu_processor", "rag_searcher", "sql_validator", "schema_limiter", "sql_generator"]


def test_core_public_functions_preserved():
    for name in PUBLIC_FUNCTIONS:
        assert callable(getattr(core, name)), f"core.{name} missing or not callable"


def test_core_singletons_preserved():
    for name in SINGLETONS:
        obj = getattr(core, name, None)
        assert obj is not None, f"core.{name} singleton missing"


def test_core_direct_imports_preserved():
    from custom_tools.text_to_sql.core import sql_explain, secure_db_executor, audit_logger
    assert sql_explain is core.sql_explain
    assert secure_db_executor is core.secure_db_executor
    assert audit_logger is core.audit_logger


def test_core_monkeypatch_targets_preserved():
    """Тесты в репозитории monkeypatch'ят core.call_openai_api и core.get_plugin.

    Эти атрибуты обязаны существовать на module level фасада.
    """
    assert hasattr(core, "call_openai_api")
    assert hasattr(core, "get_plugin")


def test_core_describe_helpers_preserved():
    """test_sqlglot_integration.py импортирует приватные хелперы DESCRIBE из core."""
    from custom_tools.text_to_sql.core import (
        _extract_schema_and_table_from_describe,
        _parse_table_name_from_describe_sqlglot,
    )
    assert callable(_extract_schema_and_table_from_describe)
    assert callable(_parse_table_name_from_describe_sqlglot)
