import os
import json
from custom_tools.sql_tools import schema_linking


def test_schema_linking_generates_join_sqlite(tmp_path, monkeypatch):
    # Устанавливаем SQLite диалект
    monkeypatch.setenv("DB_DSN", "sqlite:///tmp/test.db")
    # Отключаем LLM для стабильности теста
    monkeypatch.setenv("SCHEMA_LINKING_USE_LLM", "0")
    monkeypatch.setenv("SCHEMA_LINKING_ALLOW_FALLBACKS", "1")
    # После T4.2 «revenue → amount», «region → region_id» — не дефолтная
    # эвристика, а доменный профиль. Пишем свой тестовый профиль (домен
    # больше не хранится в этом yaml — он переехал в DSN-профиль) —
    # регрессионный safety-net для алиас-логики best_column_for.
    from custom_tools.text_to_sql import column_aliases_config
    custom_yaml = tmp_path / "column_aliases.yaml"
    custom_yaml.write_text(
        """
version: 2
policy:
  type_hint_categories: ["numeric", "temporal", "identifier"]
  required_profiles: ["default"]
  default_profile_must_be_empty: true
profiles:
  default:
    aliases: {}
  alt:
    aliases:
      revenue: [revenue, amount, total]
      region: [region, region_name, region_id]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_COLUMN_ALIASES_PATH", str(custom_yaml))
    monkeypatch.setenv("TEXT_TO_SQL_COLUMN_ALIASES_PROFILE", "alt")
    column_aliases_config.reset_cache()
    # Schema-linking RAG-кэш (постоянная память) не учитывает содержимое
    # TEXT_TO_SQL_COLUMN_ALIASES_PATH в своём ключе — только имя профиля.
    # Чистим кэш для этого DSN, чтобы тест не зависел от результатов чужих
    # прогонов с тем же DSN/профилем.
    from custom_tools.sql_tools import purge_schema_linking_rag_cache
    from custom_tools.text_to_sql.utils import dsn_to_sanitized_name
    purge_schema_linking_rag_cache(session_id=dsn_to_sanitized_name("sqlite:///tmp/test.db"))
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
