import os
import json
from custom_tools.sql_tools import schema_linking


def test_schema_linking_generates_join_sqlite(monkeypatch):
    # Устанавливаем SQLite диалект
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")
    # Отключаем LLM для стабильности теста
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "0")
    monkeypatch.setenv("SCHEMA_LINKING_ALLOW_FALLBACKS", "1")
    # После T4.2 «revenue → amount», «region → region_id» — не дефолтная
    # эвристика, а доменный профиль. Включаем muni_ru — регрессионный
    # safety-net для пользовательского датасета.
    monkeypatch.setenv("TEXT_TO_SQL_COLUMN_ALIASES_PROFILE", "muni_ru")
    from custom_tools.text_to_sql import column_aliases_config
    column_aliases_config.reset_cache()
    entities = {
        "metrics": ["revenue"],
        "dimensions": ["region"],
        "filters": {}
    }
    # Минимальная схема, без описаний
    schema_info = {
        "orders": {
            "id": {"type": "INTEGER", "description": ""},
            "region_id": {"type": "INTEGER", "description": ""},
            "amount": {"type": "DECIMAL", "description": ""}
        },
        "regions": {
            "id": {"type": "INTEGER", "description": ""},
            "region_name": {"type": "TEXT", "description": ""}
        }
    }
    out = schema_linking(entities, schema_info=schema_info, dsn="sqlite:///tmp/test.db")
    assert isinstance(out, dict)
    assert "linked_entities" in out
    joins = out.get("joins", [])
    # Должна быть связь между orders и regions по region_id/id в любом направлении
    def _ok(j):
        ft, fc, tt, tc = j.get("from_table"), j.get("from_column"), j.get("to_table"), j.get("to_column")
        # Принимаем любые префиксы схем для SQLite (main., db., или без префикса)
        orders_variants = {"orders", "main.orders", "db.orders"}
        regions_variants = {"regions", "main.regions", "db.regions"}
        return (
            (ft in orders_variants and fc == "region_id" and tt in regions_variants and tc == "id") or
            (ft in regions_variants and fc == "id" and tt in orders_variants and tc == "region_id")
        )
    assert any(_ok(j) for j in joins)
