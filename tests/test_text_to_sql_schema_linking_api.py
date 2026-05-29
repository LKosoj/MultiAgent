"""Тесты разделения session_id/schema_info в schema_linking (EPIC 1.7).

Покрывают:
- новую двух-kwarg сигнатуру (schema_linking(entities, session_id=, schema_info=));
- backward-compat shim для positional dict (session_id=dict → schema_info);
- DeprecationWarning + явный TypeError при ambiguous-вызовах.
"""
import pytest

from custom_tools.text_to_sql.core import schema_linking
from custom_tools.text_to_sql.core import _schema_linking_api


def _entities():
    return {
        "metrics": ["revenue"],
        "dimensions": ["region"],
        "filters": {},
    }


def _schema():
    return {
        "orders": {
            "columns": {
                "id": {"type": "INTEGER", "description": ""},
                "region_id": {"type": "INTEGER", "description": ""},
                "amount": {"type": "DECIMAL", "description": ""},
            }
        },
        "regions": {
            "columns": {
                "id": {"type": "INTEGER", "description": ""},
                "region_name": {"type": "TEXT", "description": ""},
            }
        },
    }


def _setup_env(monkeypatch):
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "0")
    monkeypatch.setenv("SCHEMA_LINKING_ALLOW_FALLBACKS", "1")
    monkeypatch.setenv("TEXT_TO_SQL_COLUMN_ALIASES_PROFILE", "muni_ru")
    from custom_tools.text_to_sql import column_aliases_config
    column_aliases_config.reset_cache()


def _capture_schema_linking_warnings(monkeypatch):
    caught = []

    def capture_warning(message, category=None, **kwargs):
        caught.append((str(message), category or UserWarning))

    monkeypatch.setattr(_schema_linking_api._warnings, "warn", capture_warning)
    return caught


def test_new_kwarg_api_explicit_schema_info(monkeypatch):
    """Новый API: session_id и schema_info — два независимых kwarg, без warnings."""
    _setup_env(monkeypatch)
    caught = _capture_schema_linking_warnings(monkeypatch)

    out = schema_linking(
        _entities(),
        session_id="abc",
        schema_info=_schema(),
        dsn="sqlite:///tmp/test.db",
    )

    deprecation = [item for item in caught if issubclass(item[1], DeprecationWarning)]
    assert deprecation == [], f"unexpected DeprecationWarnings: {deprecation}"
    assert isinstance(out, dict)
    assert "linked_entities" in out


def test_deprecated_dict_as_session_emits_warning(monkeypatch):
    """Legacy: позиционно переданный dict вместо session_id — DeprecationWarning, но работает."""
    _setup_env(monkeypatch)
    entities = _entities()
    schema = _schema()
    caught = _capture_schema_linking_warnings(monkeypatch)

    legacy_out = schema_linking(entities, schema, dsn="sqlite:///tmp/test.db")
    assert any(
        issubclass(category, DeprecationWarning) and "schema_info" in message
        for message, category in caught
    )

    # Результат должен быть идентичен kwarg-варианту
    new_out = schema_linking(
        _entities(),
        schema_info=_schema(),
        dsn="sqlite:///tmp/test.db",
    )
    assert legacy_out.get("linked_entities") == new_out.get("linked_entities")
    assert legacy_out.get("joins") == new_out.get("joins")


def test_deprecated_only_session_id_string(monkeypatch):
    """session_id=str, schema_info=None — валидный вызов, без warnings, schema пустая."""
    _setup_env(monkeypatch)
    caught = _capture_schema_linking_warnings(monkeypatch)

    out = schema_linking(
        _entities(),
        session_id="sess-1",
        dsn="sqlite:///tmp/test.db",
    )

    deprecation = [item for item in caught if issubclass(item[1], DeprecationWarning)]
    assert deprecation == [], f"unexpected DeprecationWarnings: {deprecation}"
    assert isinstance(out, dict)
    # schema_info=None → линкер работает с пустой схемой/кэшем; контракт ответа сохраняется.
    assert "linked_entities" in out


def test_both_kwargs_when_session_id_dict_raises_typeerror(monkeypatch):
    """Ambiguous call: session_id=dict И schema_info=dict — TypeError, без silent."""
    _setup_env(monkeypatch)

    with pytest.raises(TypeError, match="ambiguous"):
        schema_linking(_entities(), session_id={"x": 1}, schema_info={"y": 2})


def test_mixed_keyword_call(monkeypatch):
    """Kwarg-only вызов: entities=, schema_info=, session_id=None — ok."""
    _setup_env(monkeypatch)
    caught = _capture_schema_linking_warnings(monkeypatch)

    out = schema_linking(
        entities=_entities(),
        schema_info=_schema(),
        session_id=None,
        dsn="sqlite:///tmp/test.db",
    )

    deprecation = [item for item in caught if issubclass(item[1], DeprecationWarning)]
    assert deprecation == [], f"unexpected DeprecationWarnings: {deprecation}"
    assert isinstance(out, dict)
    assert "linked_entities" in out


def test_schema_linking_uses_explicit_dsn_for_schema_resolution(monkeypatch):
    """Explicit dsn must win over DB_DSN when loading sqlrag/introspection schema."""
    _setup_env(monkeypatch)
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/env.db")

    selected_dsn = "postgresql://alice:secret@db.example.com/app"
    captured = {}

    def fake_load_sqlrag_schema(self, dsn):
        captured["dsn"] = dsn
        return _schema()

    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_loader.SchemaLoader._load_sqlrag_schema",
        fake_load_sqlrag_schema,
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_loader.SchemaLoader._normalize_table_names",
        lambda self, schema, dsn: schema,
    )

    out = schema_linking(_entities(), dsn=selected_dsn)

    assert captured["dsn"] == selected_dsn
    assert "linked_entities" in out
