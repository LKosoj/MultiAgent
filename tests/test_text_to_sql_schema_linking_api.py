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
    assert out["sql_generation_allowed"] is bool(out.get("join_success"))


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


def test_schema_linking_forwards_value_grounding_kwarg(monkeypatch):
    captured = {}

    def fake_link(self, entities, schema_info, dsn=None, session_id=None, value_grounding=None):
        captured["value_grounding"] = value_grounding
        return {
            "linked_entities": {"metrics": [], "dimensions": [], "filters": {}},
            "joins": [],
            "join_success": True,
            "schema_info": _schema(),
        }

    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_linker.SchemaLinker.link_entities_to_schema",
        fake_link,
    )

    out = schema_linking(
        _entities(),
        schema_info=_schema(),
        dsn="sqlite:///tmp/test.db",
        value_grounding=True,
    )

    assert captured["value_grounding"] is True
    assert out["decision"] == "ABSTAIN"
    assert out["sql_generation_allowed"] is False
    assert out["confidence"] == 0.75
    assert out["ambiguity"]["requires_clarification"] is False


def test_partial_linking_requires_clarification_and_blocks_generation(monkeypatch):
    def fake_link(self, entities, schema_info, **kwargs):
        return {
            "linked_entities": {
                "metrics": [
                    {"name": "revenue", "table": "orders", "column": "amount"}
                ],
                "dimensions": [],
                "filters": {},
            },
            "joins": [],
            "join_success": True,
            "schema_info": _schema(),
            "ambiguous_bindings": [],
        }

    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_linker.SchemaLinker.link_entities_to_schema",
        fake_link,
    )

    out = schema_linking(_entities(), schema_info=_schema())

    assert out["decision"] == "CLARIFY"
    assert out["decision_reasons"] == ["UNRESOLVED_ENTITIES"]
    assert out["unresolved_entities"] == [
        {"entity_type": "dimension", "name": "region"}
    ]
    assert out["ambiguity"]["requires_clarification"] is True
    assert out["abstain"] is False
    assert out["sql_generation_allowed"] is False
    assert out["terminal_reason_code"] == "SCHEMA_CLARIFICATION_REQUIRED"


def test_complete_single_table_linking_proceeds(monkeypatch):
    def fake_link(self, entities, schema_info, **kwargs):
        return {
            "linked_entities": {
                "metrics": [
                    {"name": "revenue", "table": "orders", "column": "amount"}
                ],
                "dimensions": [
                    {"name": "region", "table": "orders", "column": "region_id"}
                ],
                "filters": {},
            },
            "joins": [],
            "join_success": True,
            "schema_info": _schema(),
            "ambiguous_bindings": [],
        }

    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_linker.SchemaLinker.link_entities_to_schema",
        fake_link,
    )

    out = schema_linking(_entities(), schema_info=_schema())

    assert out["decision"] == "PROCEED"
    assert out["decision_reasons"] == []
    assert out["unresolved_entities"] == []
    assert out["abstain"] is False
    assert out["sql_generation_allowed"] is True
    assert out["terminal_reason_code"] == ""


def test_complete_linking_accepts_entity_key_from_llm_strategy(monkeypatch):
    def fake_link(self, entities, schema_info, **kwargs):
        return {
            "linked_entities": {
                "metrics": [
                    {
                        "entity": "revenue",
                        "table": "orders",
                        "column": "amount",
                    }
                ],
                "dimensions": [
                    {
                        "entity": "region",
                        "table": "orders",
                        "column": "region_id",
                    }
                ],
                "filters": {},
            },
            "joins": [],
            "join_success": True,
            "schema_info": _schema(),
            "ambiguous_bindings": [],
        }

    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_linker.SchemaLinker.link_entities_to_schema",
        fake_link,
    )

    out = schema_linking(_entities(), schema_info=_schema())

    assert out["unresolved_entities"] == []
    assert out["decision"] == "PROCEED"
    assert out["sql_generation_allowed"] is True


def test_equal_best_diagnostics_require_clarification(monkeypatch):
    candidates = [
        {"table": "orders", "column": "region", "score": 10},
        {"table": "regions", "column": "region", "score": 10},
    ]

    def fake_link(self, entities, schema_info, **kwargs):
        return {
            "linked_entities": {
                "metrics": [
                    {"name": "revenue", "table": "orders", "column": "amount"}
                ],
                "dimensions": [],
                "filters": {},
            },
            "joins": [],
            "join_success": True,
            "schema_info": _schema(),
            "ambiguous_bindings": [
                {
                    "entity_type": "dimension",
                    "name": "region",
                    "candidates": candidates,
                }
            ],
        }

    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_linker.SchemaLinker.link_entities_to_schema",
        fake_link,
    )

    out = schema_linking(_entities(), schema_info=_schema())

    assert out["decision"] == "CLARIFY"
    assert out["decision_reasons"] == [
        "UNRESOLVED_ENTITIES",
        "AMBIGUOUS_BINDINGS",
    ]
    assert out["ambiguous_bindings"][0]["candidates"] == candidates
    assert out["sql_generation_allowed"] is False


def test_schema_linking_confidence_threshold_can_abstain(monkeypatch):
    def fake_link(self, entities, schema_info, dsn=None, session_id=None, value_grounding=None):
        return {
            "linked_entities": {
                "metrics": [
                    {"name": "revenue", "table": "orders", "column": "amount"}
                ],
                "dimensions": [],
                "filters": {},
            },
            "joins": [],
            "join_success": True,
            "schema_info": _schema(),
        }

    monkeypatch.setenv("TEXT_TO_SQL_MIN_CONFIDENCE_TO_GENERATE", "0.95")
    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_linker.SchemaLinker.link_entities_to_schema",
        fake_link,
    )

    out = schema_linking(
        _entities(),
        schema_info=_schema(),
        dsn="sqlite:///tmp/test.db",
    )

    assert out["confidence"] == 0.9
    assert out["decision"] == "ABSTAIN"
    assert out["abstain"] is True
    assert out["sql_generation_allowed"] is False


@pytest.mark.parametrize("value", ["bad", "nan", "-0.1", "1.1"])
def test_schema_linking_confidence_threshold_rejects_invalid_override(
    monkeypatch,
    value,
):
    monkeypatch.setenv("TEXT_TO_SQL_MIN_CONFIDENCE_TO_GENERATE", value)

    with pytest.raises(ValueError, match="TEXT_TO_SQL_MIN_CONFIDENCE_TO_GENERATE"):
        schema_linking(_entities(), schema_info=_schema())
