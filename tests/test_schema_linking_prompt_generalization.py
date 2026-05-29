"""Контрактные тесты генерализации schema-linking промпта (T4.4).

Проверяют:
  * дефолтный профиль не добавляет доменных подсказок в промпт
    (универсальный шаблон, никаких oktmo/territory_id/...);
  * профиль ``muni_ru`` восстанавливает легаси-поведение для
    пользовательского муниципального датасета;
  * подмена пути конфига через env подменяет источник профилей;
  * отсутствие конфига приводит к ``FileNotFoundError`` (fail-fast);
  * в ``custom_tools/text_to_sql/prompts.py`` не осталось доменных
    имён колонок — они переехали в yaml.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from custom_tools.text_to_sql import schema_linking_examples_config
from custom_tools.text_to_sql.prompts import build_schema_linking_prompt


_ENV_PATH_VAR = "TEXT_TO_SQL_SCHEMA_LINKING_EXAMPLES_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_SCHEMA_LINKING_PROFILE"
_PROMPTS_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_tools"
    / "text_to_sql"
    / "prompts.py"
)

_LEGACY_TOKENS = (
    "oktmo",
    "okato",
    "municipal_district_name",
    "territory_id",
    "district_id",
    "area_id",
    "zone_id",
    "region_id",
    "region_name",
)


@pytest.fixture(autouse=True)
def _reset_examples_cache(monkeypatch):
    """Гарантирует, что кэш конфига не протекает между тестами."""
    schema_linking_examples_config.reset_cache()
    yield
    monkeypatch.delenv(_ENV_PATH_VAR, raising=False)
    monkeypatch.delenv(_ENV_PROFILE_VAR, raising=False)
    schema_linking_examples_config.reset_cache()


def _sample_inputs():
    entities = {"metrics": ["revenue"], "dimensions": ["date"], "filters": {}}
    schema_str = "schema.t1(id INT, ts DATE)"
    return entities, schema_str


def test_schema_linking_prompt_default_profile_no_domain_terms():
    """Дефолтный профиль (пустой) → промпт не содержит доменных терминов."""
    entities, schema_str = _sample_inputs()
    prompt = build_schema_linking_prompt(entities, schema_str)

    lower = prompt.lower()
    for token in _LEGACY_TOKENS:
        assert token not in lower, (
            f"Дефолтный профиль не должен содержать доменный токен '{token}' в промпте"
        )

    # И заголовок доменного блока тоже не должен появляться.
    assert "ДОМЕННЫЕ ПРИМЕРЫ" not in prompt


def test_schema_linking_prompt_muni_profile_includes_legacy_terms():
    """Профиль ``muni_ru`` → промпт содержит исторические доменные имена."""
    entities, schema_str = _sample_inputs()
    prompt = build_schema_linking_prompt(entities, schema_str, profile="muni_ru")

    assert "ДОМЕННЫЕ ПРИМЕРЫ" in prompt
    # Регрессионная проверка для пользовательского датасета.
    for token in ("oktmo", "territory_id", "municipal_district_name"):
        assert token in prompt, (
            f"Профиль muni_ru должен содержать легаси-токен '{token}'"
        )


def test_schema_linking_prompt_profile_via_env(monkeypatch):
    """Env ``TEXT_TO_SQL_SCHEMA_LINKING_PROFILE`` переключает профиль."""
    entities, schema_str = _sample_inputs()
    monkeypatch.setenv(_ENV_PROFILE_VAR, "muni_ru")
    schema_linking_examples_config.reset_cache()

    prompt = build_schema_linking_prompt(entities, schema_str)
    assert "territory_id" in prompt
    assert "oktmo" in prompt


def test_schema_linking_prompt_loads_examples_from_yaml(tmp_path, monkeypatch):
    """Подменяем yaml — кастомный токен попадает в промпт через свой профиль."""
    custom_yaml = tmp_path / "schema_linking_examples.yaml"
    custom_yaml.write_text(
        """
version: 1
profiles:
  default:
    priority_id_columns: []
    low_priority_name_columns: []
    prefer_id_over_name_rules: []
  custom_demo:
    priority_id_columns:
      - tenant_uuid
      - product_sku
    low_priority_name_columns:
      - product_display_name
    prefer_id_over_name_rules:
      - id_column: tenant_uuid
        ignore_column: tenant_label
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_PATH_VAR, str(custom_yaml))
    schema_linking_examples_config.reset_cache()

    entities, schema_str = _sample_inputs()

    # Дефолт остаётся пустым.
    default_prompt = build_schema_linking_prompt(entities, schema_str)
    assert "tenant_uuid" not in default_prompt
    assert "ДОМЕННЫЕ ПРИМЕРЫ" not in default_prompt

    # Кастомный профиль вставляет свои термины.
    custom_prompt = build_schema_linking_prompt(
        entities, schema_str, profile="custom_demo"
    )
    assert "tenant_uuid" in custom_prompt
    assert "product_sku" in custom_prompt
    assert "product_display_name" in custom_prompt
    assert "tenant_label" in custom_prompt
    # Легаси-токенов в кастомном yaml нет — их не должно быть и в промпте.
    assert "oktmo" not in custom_prompt
    assert "territory_id" not in custom_prompt


def test_schema_linking_prompt_fails_fast_when_yaml_missing(tmp_path, monkeypatch):
    """Несуществующий путь → ``FileNotFoundError``, без молчаливых дефолтов."""
    missing = tmp_path / "does_not_exist.yaml"
    monkeypatch.setenv(_ENV_PATH_VAR, str(missing))
    schema_linking_examples_config.reset_cache()

    entities, schema_str = _sample_inputs()
    with pytest.raises(FileNotFoundError):
        build_schema_linking_prompt(entities, schema_str)


def test_schema_linking_prompt_unknown_profile_fails_fast(tmp_path, monkeypatch):
    """Неизвестное имя профиля → ``KeyError`` (fail-fast)."""
    entities, schema_str = _sample_inputs()
    with pytest.raises(KeyError):
        build_schema_linking_prompt(entities, schema_str, profile="no_such_profile")


def test_schema_linking_prompt_no_domain_terms_in_python():
    """Доменные имена колонок не должны больше жить в prompts.py."""
    source = _PROMPTS_PATH.read_text(encoding="utf-8")
    forbidden = re.compile(
        r"oktmo|okato|municipal_district_name|territory_id|district_id|area_id|zone_id|region_id|region_name",
        re.IGNORECASE,
    )
    matches = forbidden.findall(source)
    assert matches == [], (
        "Доменные имена колонок всё ещё в prompts.py — должны быть в yaml: "
        f"{matches}"
    )
