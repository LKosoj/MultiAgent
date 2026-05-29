"""EPIC 8.8: SQLGenerator живёт как singleton в фасаде `core/__init__.py`.

До рефакторинга `sql_generation_plugin` создавал новый `SQLGenerator()` на
каждый вызов (два валидатора и кэш — на каждый запрос). Тест фиксирует
singleton-pattern: один инстанс на весь жизненный цикл модуля.

T11-orch: дополнительные тесты наблюдаемости (logging + public API).
"""
import json
import logging
import tempfile
from pathlib import Path

from custom_tools.text_to_sql import core


def test_sql_generator_singleton_identity_stable():
    from custom_tools.text_to_sql import core as core_again
    assert core.sql_generator is core_again.sql_generator


def test_sql_generator_singleton_listed_in_all():
    assert "sql_generator" in core.__all__


def test_sql_generation_plugin_uses_singleton(monkeypatch):
    """Два вызова `sql_generation_plugin` должны делегировать в один и тот же инстанс."""
    captured: list = []
    original = core.sql_generator.generate_sql

    def stub(context, user_query, dsn=None):
        captured.append(id(core.sql_generator))
        return {"sql_query": "SELECT 1"}

    monkeypatch.setattr(core.sql_generator, "generate_sql", stub)
    try:
        core.sql_generation_plugin("ctx-1", "q-1", dsn="sqlite:///tmp/app.db")
        core.sql_generation_plugin("ctx-2", "q-2", dsn="sqlite:///tmp/app.db")
    finally:
        # monkeypatch автоматически восстановит метод, но для перестраховки.
        pass

    assert len(captured) == 2
    assert captured[0] == captured[1], "Different SQLGenerator instances per call"
    # Sanity: id матчит реальный singleton.
    assert captured[0] == id(core.sql_generator)


def test_sql_generation_plugin_passes_explicit_dsn_to_singleton(monkeypatch):
    captured = {}

    def stub(context, user_query, dsn=None):
        captured["context"] = context
        captured["user_query"] = user_query
        captured["dsn"] = dsn
        return {"sql_query": "SELECT 1"}

    monkeypatch.setattr(core.sql_generator, "generate_sql", stub)

    result = core.sql_generation_plugin("ctx", "q", dsn="sqlite:///tmp/app.db")

    assert result == {"sql_query": "SELECT 1"}
    assert captured == {
        "context": "ctx",
        "user_query": "q",
        "dsn": "sqlite:///tmp/app.db",
    }


def test_sql_generation_plugin_does_not_construct_new_instance(monkeypatch):
    """SQLGenerator() НЕ должен вызываться повторно при каждом sql_generation_plugin."""
    from custom_tools.text_to_sql import sql_generator as sg_module

    init_count = {"value": 0}
    original_init = sg_module.SQLGenerator.__init__

    def counting_init(self):
        init_count["value"] += 1
        original_init(self)

    monkeypatch.setattr(sg_module.SQLGenerator, "__init__", counting_init)
    monkeypatch.setattr(core.sql_generator, "generate_sql", lambda *a, **k: {"sql_query": ""})

    core.sql_generation_plugin("ctx", "q", dsn="sqlite:///tmp/app.db")
    core.sql_generation_plugin("ctx", "q", dsn="sqlite:///tmp/app.db")
    core.sql_generation_plugin("ctx", "q", dsn="sqlite:///tmp/app.db")

    assert init_count["value"] == 0, (
        f"SQLGenerator.__init__ called {init_count['value']} times — should be 0 (singleton)"
    )


# === T11-orch: тесты наблюдаемости ===


def test_missing_dsn_error_in_core_all():
    """MissingDSNError должна быть доступна через core.__all__ (T11-orch #8)."""
    assert "MissingDSNError" in core.__all__


def test_missing_dsn_error_importable_from_core():
    """MissingDSNError импортируется из публичного пакета core (T11-orch #8)."""
    from custom_tools.text_to_sql.core import MissingDSNError
    assert issubclass(MissingDSNError, RuntimeError)


def test_get_history_logs_warning_on_corrupt_line(tmp_path, caplog):
    """get_history логирует warning при встрече с битой JSONL-строкой (T11-orch #1)."""
    from custom_tools.text_to_sql.tool import SQLHistoryManager

    history_file = tmp_path / "sql_history.jsonl"
    good_entry = {"id": "1", "timestamp": "2026-01-01T00:00:00", "natural_query": "q", "generated_sql": "SELECT 1", "status": "ok", "connection_id": "c", "execution_result": None}
    history_file.write_text(
        json.dumps(good_entry) + "\n"
        + "not valid json{{{\n"
        + json.dumps({"id": "2", "timestamp": "2026-01-02T00:00:00", "natural_query": "q2", "generated_sql": "SELECT 2", "status": "ok", "connection_id": "c", "execution_result": None}) + "\n",
        encoding="utf-8",
    )

    manager = SQLHistoryManager(history_file)
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.tool"):
        result = manager.get_history()

    # Контракт: возвращает список, пропуская битые строки
    assert len(result) == 2
    assert result[0]["id"] == "1"
    assert result[1]["id"] == "2"
    # Наблюдаемость: warning присутствует в логе
    assert any("пропущена битая строка" in r.message for r in caplog.records)


def test_get_history_returns_empty_list_on_missing_file(tmp_path):
    """get_history возвращает [] без исключений для несуществующего файла (T11-orch #1)."""
    from custom_tools.text_to_sql.tool import SQLHistoryManager

    manager = SQLHistoryManager(tmp_path / "nonexistent.jsonl")
    result = manager.get_history()
    assert result == []


def test_get_schema_version_logs_warning_for_unknown(monkeypatch, caplog):
    """get_schema_version логирует warning при fallback к 'unknown' (T11-orch #5)."""
    from custom_tools.text_to_sql import utils as utils_module

    monkeypatch.delenv("SCHEMA_VERSION", raising=False)
    monkeypatch.delenv("DB_DSN", raising=False)
    utils_module.clear_schema_version_cache()

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.utils"):
        version = utils_module.get_schema_version(db_schema=None)

    assert version == "unknown"
    assert any("unknown" in r.message for r in caplog.records)


def test_get_schema_version_with_explicit_schema_no_dsn_warning(monkeypatch, caplog):
    """get_schema_version с явной схемой не выдаёт предупреждение про DSN (T11-orch #3)."""
    from custom_tools.text_to_sql import utils as utils_module

    monkeypatch.delenv("SCHEMA_VERSION", raising=False)
    utils_module.clear_schema_version_cache()

    schema = {"table_a": {"columns": [{"name": "id", "type": "integer"}]}}
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.utils"):
        version = utils_module.get_schema_version(db_schema=schema)

    assert version != "unknown"
    dsn_warnings = [r for r in caplog.records if "DB_DSN" in r.message]
    assert dsn_warnings == [], f"Неожиданные предупреждения про DSN: {dsn_warnings}"


def test_dsn_to_sanitized_name_logs_warning_on_error(monkeypatch, caplog):
    """dsn_to_sanitized_name логирует warning при исключении и возвращает hash-имя (T11-orch #6)."""
    from custom_tools.text_to_sql import utils as utils_module

    original_urlparse = utils_module.urlparse

    def broken_urlparse(dsn):
        raise RuntimeError("simulated parse error")

    monkeypatch.setattr(utils_module, "urlparse", broken_urlparse)

    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.utils"):
        result = utils_module.dsn_to_sanitized_name("postgresql://user:pw@host/db")

    # Контракт: всегда возвращает строку
    assert isinstance(result, str)
    assert result.startswith("db_")
    # Наблюдаемость: warning в логе
    assert any("hash-имя" in r.message for r in caplog.records)
