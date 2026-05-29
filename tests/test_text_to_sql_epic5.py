"""Контрактные тесты EPIC 5 — Hardcoded business-logic → yaml.

Один тестовый модуль на весь блок (8 задач). Для каждой задачи —
небольшая группа тестов, привязанных к конкретному yaml-полю / loader'у.

См. AGENTS.md:
  * никакого silent fallback (отсутствие/невалидный yaml → raise);
  * никакого хардкода в Python — всё в yaml;
  * сохранение публичных сигнатур.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# 5.4 — pii_categories_config.py: sensitivity-уровни из yaml
# ---------------------------------------------------------------------------


def _pii_reset_env(monkeypatch) -> None:
    from custom_tools.text_to_sql import pii_categories_config

    monkeypatch.delenv("PII_CATEGORIES_PATH", raising=False)
    monkeypatch.delenv("PII_JURISDICTION", raising=False)
    pii_categories_config.reset_cache()


def test_pii_sensitivity_levels_from_yaml(tmp_path, monkeypatch):
    """policy.sensitivity_levels — source of truth для допустимых уровней."""
    from custom_tools.text_to_sql import pii_categories_config

    yaml_path = tmp_path / "custom_pii.yaml"
    yaml_path.write_text(
        """
version: 2
policy:
  sensitivity_levels: ["low", "medium", "high", "critical"]
default_jurisdiction: ru
jurisdictions:
  ru:
    description: "test"
    prefixes:
      low: "lo"
      medium: "md"
      high: "hi"
      critical: "crit"
    negatives: {}
    sync_masking:
      rules:
        - id: email
          pattern: '[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}'
          replacement: '[EMAIL]'
    categories:
      - id: passport
        label_ru: "паспорт"
        sensitivities: [low, medium, high, critical]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PII_CATEGORIES_PATH", str(yaml_path))
    pii_categories_config.reset_cache()

    cfg = pii_categories_config.load_pii_categories_config()
    assert cfg.sensitivity_levels == ("low", "medium", "high", "critical")

    # compose_pii_description должен принимать новый уровень из yaml
    desc = pii_categories_config.compose_pii_description("critical")
    assert "паспорт" in desc
    assert desc.startswith("crit:")

    _pii_reset_env(monkeypatch)


def test_pii_v1_without_policy_fails(tmp_path, monkeypatch):
    """v1 yaml без policy.sensitivity_levels → fail-fast (migration required)."""
    from custom_tools.text_to_sql import pii_categories_config

    yaml_path = tmp_path / "legacy_v1.yaml"
    yaml_path.write_text(
        """
version: 1
default_jurisdiction: ru
jurisdictions:
  ru:
    description: "legacy"
    prefixes:
      low: "lo"
      medium: "md"
      high: "hi"
    negatives: {}
    categories:
      - id: passport
        label_ru: "паспорт"
        sensitivities: [low, medium, high]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PII_CATEGORIES_PATH", str(yaml_path))
    pii_categories_config.reset_cache()

    with pytest.raises(ValueError, match="migration required"):
        pii_categories_config.load_pii_categories_config()

    _pii_reset_env(monkeypatch)


def test_pii_sync_masking_rules_from_yaml(tmp_path, monkeypatch):
    """sync_masking.rules — source of truth для regex-only audit/RAG masker."""
    from custom_tools.text_to_sql import pii_categories_config
    from custom_tools.text_to_sql.core import _pii

    yaml_path = tmp_path / "sync_pii.yaml"
    yaml_path.write_text(
        """
version: 2
policy:
  sensitivity_levels: ["low", "medium", "high"]
default_jurisdiction: ru
jurisdictions:
  ru:
    description: "test"
    prefixes:
      low: "lo"
      medium: "md"
      high: "hi"
    negatives: {}
    sync_masking:
      rules:
        - id: ticket
          pattern: 'TICKET-\\d+'
          replacement: '[TICKET]'
    categories:
      - id: email
        label_ru: "email"
        sensitivities: [low, medium, high]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PII_CATEGORIES_PATH", str(yaml_path))
    pii_categories_config.reset_cache()

    cfg = pii_categories_config.load_pii_categories_config()
    rules = cfg.get_jurisdiction("ru").sync_masking_rules
    assert len(rules) == 1
    assert rules[0].id == "ticket"
    assert _pii.pii_mask_sync("incident TICKET-123") == "incident [TICKET]"

    _pii_reset_env(monkeypatch)


def test_pii_sync_masking_eu_masks_configured_runtime_categories(monkeypatch):
    """EU jurisdiction must not degrade to email-only sync masking."""
    from custom_tools.text_to_sql import pii_categories_config
    from custom_tools.text_to_sql.core import _pii

    monkeypatch.setenv("PII_JURISDICTION", "eu")
    monkeypatch.delenv("PII_CATEGORIES_PATH", raising=False)
    pii_categories_config.reset_cache()

    masked = _pii.pii_mask_sync(
        "email user@example.com phone +49 30 123456 "
        "passport C01X00T47 ssn 123-45-6789 "
        "card 4111 1111 1111 1111 ip 192.168.1.10"
    )

    assert "user@example.com" not in masked
    assert "+49 30 123456" not in masked
    assert "C01X00T47" not in masked
    assert "123-45-6789" not in masked
    assert "4111 1111 1111 1111" not in masked
    assert "192.168.1.10" not in masked
    for replacement in ("[EMAIL]", "[PHONE]", "[PASSPORT]", "[SSN]", "[CARD]", "[IP]"):
        assert replacement in masked

    _pii_reset_env(monkeypatch)


def test_pii_sync_masking_invalid_regex_fails_fast(tmp_path, monkeypatch):
    from custom_tools.text_to_sql import pii_categories_config

    yaml_path = tmp_path / "bad_sync_pii.yaml"
    yaml_path.write_text(
        """
version: 2
policy:
  sensitivity_levels: ["low", "medium", "high"]
default_jurisdiction: ru
jurisdictions:
  ru:
    description: "test"
    prefixes:
      low: "lo"
      medium: "md"
      high: "hi"
    negatives: {}
    sync_masking:
      rules:
        - id: broken
          pattern: '['
          replacement: '[BROKEN]'
    categories:
      - id: email
        label_ru: "email"
        sensitivities: [low, medium, high]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PII_CATEGORIES_PATH", str(yaml_path))
    pii_categories_config.reset_cache()

    with pytest.raises(ValueError, match="invalid regex"):
        pii_categories_config.load_pii_categories_config()

    _pii_reset_env(monkeypatch)


def test_pii_sync_masking_missing_policy_fails_fast(tmp_path, monkeypatch):
    """sync_masking is mandatory: audit/RAG masking must not become raw passthrough."""
    from custom_tools.text_to_sql import pii_categories_config

    yaml_path = tmp_path / "missing_sync_pii.yaml"
    yaml_path.write_text(
        """
version: 2
policy:
  sensitivity_levels: ["low", "medium", "high"]
default_jurisdiction: ru
jurisdictions:
  ru:
    description: "test"
    prefixes:
      low: "lo"
      medium: "md"
      high: "hi"
    negatives: {}
    categories:
      - id: email
        label_ru: "email"
        sensitivities: [low, medium, high]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PII_CATEGORIES_PATH", str(yaml_path))
    pii_categories_config.reset_cache()

    with pytest.raises(ValueError, match="sync_masking"):
        pii_categories_config.load_pii_categories_config()

    _pii_reset_env(monkeypatch)


# ---------------------------------------------------------------------------
# 5.5 — column_aliases_config.py: type_hint_categories из yaml
# ---------------------------------------------------------------------------


def _column_aliases_reset(monkeypatch) -> None:
    from custom_tools.text_to_sql import column_aliases_config

    monkeypatch.delenv("TEXT_TO_SQL_COLUMN_ALIASES_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_COLUMN_ALIASES_PROFILE", raising=False)
    column_aliases_config.reset_cache()


def test_column_aliases_type_hint_categories_from_yaml(tmp_path, monkeypatch):
    """policy.type_hint_categories — source of truth для категорий type_hints."""
    from custom_tools.text_to_sql import column_aliases_config

    yaml_path = tmp_path / "custom_aliases.yaml"
    yaml_path.write_text(
        """
version: 2
policy:
  type_hint_categories: ["numeric", "temporal", "identifier", "geo"]
profiles:
  default:
    aliases: {}
    type_hints:
      numeric: []
      temporal: []
      identifier: []
      geo: []
  custom_profile:
    aliases:
      region:
        - region
        - location
    type_hints:
      geo:
        - region
        - location
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_COLUMN_ALIASES_PATH", str(yaml_path))
    column_aliases_config.reset_cache()

    cfg = column_aliases_config.load_column_aliases_config()
    # Новая категория "geo" должна быть валидной (динамически из yaml)
    assert "geo" in cfg.profiles["custom_profile"].type_hints
    assert cfg.profiles["custom_profile"].type_hints["geo"] == ["region", "location"]

    _column_aliases_reset(monkeypatch)


def test_column_aliases_type_hint_unknown_category_rejected(tmp_path, monkeypatch):
    """Категория вне policy.type_hint_categories → fail-fast."""
    from custom_tools.text_to_sql import column_aliases_config

    yaml_path = tmp_path / "bad_aliases.yaml"
    yaml_path.write_text(
        """
version: 2
policy:
  type_hint_categories: ["numeric", "temporal", "identifier"]
profiles:
  default:
    aliases: {}
    type_hints:
      numeric: []
      temporal: []
      identifier: []
  bad:
    aliases: {}
    type_hints:
      numerc: []
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_COLUMN_ALIASES_PATH", str(yaml_path))
    column_aliases_config.reset_cache()

    with pytest.raises(ValueError, match="numerc"):
        column_aliases_config.load_column_aliases_config()

    _column_aliases_reset(monkeypatch)


# ---------------------------------------------------------------------------
# 5.6 — column_aliases: default-must-be-empty как policy
# ---------------------------------------------------------------------------


def test_column_aliases_default_must_be_empty_policy(tmp_path, monkeypatch):
    """policy.default_profile_must_be_empty=true → default с aliases → fail-fast."""
    from custom_tools.text_to_sql import column_aliases_config

    yaml_path = tmp_path / "bad_default.yaml"
    yaml_path.write_text(
        """
version: 2
policy:
  type_hint_categories: ["numeric", "temporal", "identifier"]
  required_profiles: ["default"]
  default_profile_must_be_empty: true
profiles:
  default:
    aliases:
      foo: [bar]
    type_hints:
      numeric: []
      temporal: []
      identifier: []
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_COLUMN_ALIASES_PATH", str(yaml_path))
    column_aliases_config.reset_cache()

    with pytest.raises(ValueError, match="default.*empty"):
        column_aliases_config.load_column_aliases_config()

    _column_aliases_reset(monkeypatch)


def test_column_aliases_default_can_be_non_empty_when_policy_off(tmp_path, monkeypatch):
    """Если policy.default_profile_must_be_empty=false — default может быть не пуст."""
    from custom_tools.text_to_sql import column_aliases_config

    yaml_path = tmp_path / "ok_default.yaml"
    yaml_path.write_text(
        """
version: 2
policy:
  type_hint_categories: ["numeric", "temporal", "identifier"]
  required_profiles: ["default"]
  default_profile_must_be_empty: false
profiles:
  default:
    aliases:
      foo: [bar]
    type_hints:
      numeric: []
      temporal: []
      identifier: []
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_COLUMN_ALIASES_PATH", str(yaml_path))
    column_aliases_config.reset_cache()

    cfg = column_aliases_config.load_column_aliases_config()
    assert cfg.profiles["default"].aliases == {"foo": ["bar"]}

    _column_aliases_reset(monkeypatch)


# ---------------------------------------------------------------------------
# 5.7 — main_table_scoring: унифицированный coerce_weight с min_value per-field
# ---------------------------------------------------------------------------


def test_main_table_scoring_negative_weight_rejected(tmp_path, monkeypatch):
    """Отрицательный вес → fail-fast с явным сообщением о поле."""
    from custom_tools.text_to_sql import main_table_scoring_config

    yaml_path = tmp_path / "bad_scoring.yaml"
    yaml_path.write_text(
        """
version: 2
profiles:
  default:
    semantic_match_weight: 100
    pk_weight: -5
    fk_weight: 5
    numeric_weight: 3
    columns_count_weight: 2
    min_score_for_pick: 1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_MAIN_TABLE_SCORING_PATH", str(yaml_path))
    main_table_scoring_config.reset_cache()

    with pytest.raises(ValueError, match="pk_weight"):
        main_table_scoring_config.load_main_table_scoring_config()

    monkeypatch.delenv("TEXT_TO_SQL_MAIN_TABLE_SCORING_PATH", raising=False)
    main_table_scoring_config.reset_cache()


def test_main_table_scoring_min_score_zero_rejected(tmp_path, monkeypatch):
    """min_score_for_pick=0 по-прежнему отвергается (>=1)."""
    from custom_tools.text_to_sql import main_table_scoring_config

    yaml_path = tmp_path / "bad_min.yaml"
    yaml_path.write_text(
        """
version: 2
profiles:
  default:
    semantic_match_weight: 100
    pk_weight: 10
    fk_weight: 5
    numeric_weight: 3
    columns_count_weight: 2
    min_score_for_pick: 0
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_MAIN_TABLE_SCORING_PATH", str(yaml_path))
    main_table_scoring_config.reset_cache()

    with pytest.raises(ValueError, match="min_score_for_pick"):
        main_table_scoring_config.load_main_table_scoring_config()

    monkeypatch.delenv("TEXT_TO_SQL_MAIN_TABLE_SCORING_PATH", raising=False)
    main_table_scoring_config.reset_cache()


# ---------------------------------------------------------------------------
# 5.3 — schema_memory.py / important_column_name_substrings из significance.yaml
# ---------------------------------------------------------------------------


def test_schema_memory_important_substrings_from_yaml(tmp_path, monkeypatch):
    """important_column_name_substrings из значения yaml-профиля."""
    from custom_tools.text_to_sql import significance_config

    yaml_path = tmp_path / "sig.yaml"
    yaml_path.write_text(
        """
version: 2
profiles:
  default:
    high_priority_exact: []
    high_priority_compound: []
    medium_priority_patterns: []
    critical_description_keywords: []
    important_column_name_substrings: ["foo", "bar"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_SIGNIFICANCE_PATH", str(yaml_path))
    monkeypatch.delenv("TEXT_TO_SQL_SIGNIFICANCE_PROFILE", raising=False)
    significance_config.reset_cache()

    prof = significance_config.load_significance_config()
    assert prof.important_column_name_substrings == frozenset({"foo", "bar"})

    # Проверим, что schema_memory читает именно эти подстроки.
    from custom_tools.text_to_sql.schema_memory import SchemaMemoryManager

    mgr = SchemaMemoryManager(repo_root=tmp_path)
    # «foo_col» с типом text должен попасть в important_columns;
    # колонка вне списка — нет.
    table_schema = {
        "columns": {
            "foo_col": {"type": "text", "description": "x"},
            "irrelevant": {"type": "text", "description": "y"},
        }
    }
    desc = mgr.create_table_description("public.t", table_schema)
    assert "foo_col" in desc
    assert "irrelevant" not in desc

    monkeypatch.delenv("TEXT_TO_SQL_SIGNIFICANCE_PATH", raising=False)
    significance_config.reset_cache()


# ---------------------------------------------------------------------------
# 5.1 — JoinBuilder: английская плюрализация из yaml
# ---------------------------------------------------------------------------


def test_join_builder_inflections_from_yaml(tmp_path, monkeypatch):
    """JoinBuilder использует pluralizers из nlu_morphemes.yaml.

    Дефолтный yaml содержит ``["", "s"]`` и ``["y", "ies"]``, поэтому
    user_id ↔ users должно совпасть.
    """
    from custom_tools.text_to_sql.join_builder import JoinBuilder

    schema = {
        "public.users": {"columns": {"id": {"type": "int"}}},
        "public.orders": {
            "columns": {
                "id": {"type": "int"},
                "user_id": {"type": "int"},
            }
        },
    }
    builder = JoinBuilder(db_schema=schema)
    joins = builder.infer_joins_by_convention({"public.users", "public.orders"})
    assert any(
        j["from_table"] == "public.orders"
        and j["from_column"] == "user_id"
        and j["to_table"] == "public.users"
        for j in joins
    ), f"ожидался join user_id → users, получил {joins!r}"


def test_join_builder_inflections_custom_pluralizer(tmp_path, monkeypatch):
    """Кастомный pluralizer (y → ies) из переопределённого yaml."""
    from custom_tools.text_to_sql import nlu_config
    from custom_tools.text_to_sql.join_builder import JoinBuilder

    yaml_path = tmp_path / "nlu_inflect.yaml"
    yaml_path.write_text(
        """
version: 1
language: ru
enabled: true
intents: []
dimensions: []
relative_date:
  triggers: []
  periods: []
  days_pattern: '(\\d+)'
patterns:
  date_iso: []
  region: []
  amount_greater: []
  amount_less: []
  amount_between: []
  top_n: []
order:
  triggers: []
  desc_triggers: []
intent_rules: []
default_intent: query
top_n_intent: top_n
tokenizer:
  adpositions: []
regions:
  normalize: {}
table_name_inflections:
  enabled: true
  pluralizers:
    - ["y", "ies"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(yaml_path))
    nlu_config.reset_cache()

    schema = {
        "public.categories": {"columns": {"id": {"type": "int"}}},
        "public.products": {
            "columns": {
                "id": {"type": "int"},
                "category_id": {"type": "int"},
            }
        },
    }
    builder = JoinBuilder(db_schema=schema)
    joins = builder.infer_joins_by_convention({"public.categories", "public.products"})
    assert any(
        j["from_column"] == "category_id" and j["to_table"] == "public.categories"
        for j in joins
    ), f"ожидался join category_id → categories через y→ies, получил {joins!r}"

    monkeypatch.delenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", raising=False)
    nlu_config.reset_cache()


# ---------------------------------------------------------------------------
# 5.2 — type_categories: loader + plugin protocol
# ---------------------------------------------------------------------------


def test_type_categories_yaml_loader(tmp_path, monkeypatch):
    """Loader читает категории из yaml; подмена пути работает."""
    from custom_tools.text_to_sql import type_categories_config

    yaml_path = tmp_path / "type_cats.yaml"
    yaml_path.write_text(
        """
version: 1
categories:
  integer: ["int", "bigint"]
  numeric: ["float", "decimal"]
  string: ["varchar", "text"]
  temporal: ["date"]
compatibility:
  - [integer, numeric]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_TYPE_CATEGORIES_PATH", str(yaml_path))
    type_categories_config.reset_cache()

    cfg = type_categories_config.load_type_categories_config()
    assert cfg.get_category("bigint") == "integer"
    assert cfg.get_category("decimal(10,2)") == "numeric"
    # Не классифицированный тип → "other".
    assert cfg.get_category("uuid") == "other"

    monkeypatch.delenv("TEXT_TO_SQL_TYPE_CATEGORIES_PATH", raising=False)
    type_categories_config.reset_cache()


def test_type_categories_compatibility(tmp_path, monkeypatch):
    """compatibility-пары делают типы совместимыми в обе стороны."""
    from custom_tools.text_to_sql import type_categories_config

    yaml_path = tmp_path / "compat.yaml"
    yaml_path.write_text(
        """
version: 1
categories:
  integer: ["int"]
  numeric: ["float"]
  string: ["varchar"]
compatibility:
  - [integer, numeric]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_TYPE_CATEGORIES_PATH", str(yaml_path))
    type_categories_config.reset_cache()

    cfg = type_categories_config.load_type_categories_config()
    # Одинаковая группа всегда совместима.
    assert cfg.is_compatible("int", "int") is True
    # integer ↔ numeric (двусторонне).
    assert cfg.is_compatible("int", "float") is True
    assert cfg.is_compatible("float", "int") is True
    # string vs integer — нет.
    assert cfg.is_compatible("int", "varchar") is False

    monkeypatch.delenv("TEXT_TO_SQL_TYPE_CATEGORIES_PATH", raising=False)
    type_categories_config.reset_cache()


def test_db_plugin_get_type_category():
    """BaseDBPlugin.get_type_category читает категорию из yaml-конфига."""
    from db_plugins.base import BaseDBPlugin

    plugin = BaseDBPlugin()
    # Дефолтный yaml включает integer/numeric/string/temporal.
    assert plugin.get_type_category("bigint") == "integer"
    assert plugin.get_type_category("varchar(255)") == "string"
    assert plugin.get_type_category("timestamp") == "temporal"


# ---------------------------------------------------------------------------
# 5.8 — significance.yaml: legacy v1 fallback удалён, fail-fast
# ---------------------------------------------------------------------------


def test_significance_v1_layout_rejected(tmp_path, monkeypatch):
    """yaml без секции 'profiles' → fail-fast (v1 fallback удалён)."""
    from custom_tools.text_to_sql import significance_config

    yaml_path = tmp_path / "legacy_v1.yaml"
    yaml_path.write_text(
        """
version: 1
high_priority_exact:
  - legacy_marker
high_priority_compound: []
medium_priority_patterns: []
critical_description_keywords: []
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_SIGNIFICANCE_PATH", str(yaml_path))
    significance_config.reset_cache()

    with pytest.raises(ValueError, match="profiles"):
        significance_config.load_significance_config()

    monkeypatch.delenv("TEXT_TO_SQL_SIGNIFICANCE_PATH", raising=False)
    significance_config.reset_cache()
