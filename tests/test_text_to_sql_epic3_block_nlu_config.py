"""Тесты EPIC 3, блок NLU/Config (3.12 — 3.23).

Покрывает:
* 3.12: ``schema_filtering`` использует morphemes из nlu yaml.
* 3.13: словарь регионов берётся из yaml, без ``title()``.
* 3.14: ``date_range`` с одной датой — открытый интервал, не ``IS NULL``.
* 3.15: feature-flag ``enabled`` обязателен, по умолчанию false.
* 3.23: сброс / изоляция кэша morphemes через DI/scope, не глобальный ``reset_cache``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from custom_tools.text_to_sql import nlu, nlu_config, schema_filtering
from custom_tools.text_to_sql.nlu import NLUProcessor
from custom_tools.text_to_sql.nlu_config import (
    NLUMorphemesRegistry,
    load_nlu_morphemes,
    nlu_morphemes_scope,
)
from custom_tools.text_to_sql.schema_filtering import (
    SchemaRelevanceFilter,
    _expand_entity_tokens,
)


@pytest.fixture(autouse=True)
def _reset_morphemes_cache():
    nlu_config.reset_cache()
    yield
    nlu_config.reset_cache()


def _write_yaml(path: Path, *, enabled: bool = True, regions_normalize: dict | None = None) -> None:
    regions_block = regions_normalize or {}
    regions_yaml = "regions:\n  normalize: {}\n" if not regions_block else (
        "regions:\n  normalize:\n"
        + "".join(f"    \"{k}\": \"{v}\"\n" for k, v in regions_block.items())
    )
    path.write_text(
        f"""
version: 1
language: ru
enabled: {str(enabled).lower()}
intents:
  - canonical: revenue
    morphemes: ["выруч", "revenue", "сумм"]
dimensions:
  - canonical: region
    morphemes: ["регион", "облас", "край"]
relative_date:
  triggers: []
  periods: []
  days_pattern: '(\\d+)\\s*(?:дн|day)'
patterns:
  date_iso:
    - '(20\\d{{2}}-\\d{{2}}-\\d{{2}})'
  region:
    - 'регион(?:е|а)?\\s+([a-zA-Zа-яА-Я\\-]+)'
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
{regions_yaml}
""",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# 3.15: явный feature-flag
# --------------------------------------------------------------------------


def test_morphemes_disabled_by_default_explicit_flag(tmp_path, monkeypatch):
    """Без ``enabled: true`` heuristic NLU fallback должен явно отказывать."""
    cfg_path = tmp_path / "nlu_morphemes_disabled.yaml"
    _write_yaml(cfg_path, enabled=False)

    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(cfg_path))
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    with pytest.raises(RuntimeError, match="disabled by config"):
        processor.extract_intent("покажи выручку")


def test_morphemes_enabled_flag_required_yaml_key(tmp_path, monkeypatch):
    """Если ключ ``enabled`` отсутствует — default false, fallback закрыт."""
    cfg_path = tmp_path / "nlu_morphemes_no_flag.yaml"
    cfg_path.write_text(
        """
version: 1
language: ru
intents: []
dimensions: []
relative_date:
  triggers: []
  periods: []
  days_pattern: '(\\d+)\\s*(?:dd)'
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
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(cfg_path))
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    with pytest.raises(RuntimeError, match="disabled by config"):
        processor.extract_intent("любой текст")


# --------------------------------------------------------------------------
# 3.23: DI / scoped cache вместо global reset_cache
# --------------------------------------------------------------------------


def test_nlu_cache_reset_via_di_not_global(tmp_path, monkeypatch):
    """``nlu_morphemes_scope`` изолирует кэш — без глобального ``reset_cache``."""
    cfg_path = tmp_path / "nlu_morphemes.yaml"
    _write_yaml(cfg_path, enabled=True)
    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(cfg_path))

    # Внутри scope — отдельный пустой registry; первый load прогревает его.
    with nlu_morphemes_scope() as scoped_registry:
        cfg_inner = load_nlu_morphemes()
        assert isinstance(scoped_registry, NLUMorphemesRegistry)
        assert cfg_inner.enabled is True
        # registry должен содержать тот же объект
        cfg_inner_again = load_nlu_morphemes()
        assert cfg_inner is cfg_inner_again

    # После выхода из scope — кэш не утёк наружу: новый scope получает свежий
    # объект (а не закэшированный из предыдущего).
    with nlu_morphemes_scope() as another_registry:
        cfg_outer = load_nlu_morphemes()
        assert cfg_outer is not cfg_inner
        assert another_registry is not scoped_registry


def test_di_processor_uses_injected_registry(tmp_path, monkeypatch):
    """``NLUProcessor`` принимает явный registry (DI), не трогая глобальный."""
    cfg_path = tmp_path / "nlu_morphemes.yaml"
    _write_yaml(cfg_path, enabled=True)
    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(cfg_path))
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    registry = NLUMorphemesRegistry()
    processor = NLUProcessor(morphemes_registry=registry)
    result = processor.extract_intent("покажи выручку")

    assert result["entities"]["metrics"] == ["revenue"]
    # DI-registry должен быть заполнен, а глобальный — нет.
    assert registry._cache  # type: ignore[attr-defined]
    assert not nlu_config._DEFAULT_REGISTRY._cache  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 3.13: словарь регионов из yaml вместо title()
# --------------------------------------------------------------------------


def test_regions_dictionary_from_yaml(tmp_path, monkeypatch):
    """Канонизация region — через ``regions.normalize``, без ``.title()``."""
    cfg_path = tmp_path / "nlu_morphemes.yaml"
    _write_yaml(
        cfg_path,
        enabled=True,
        regions_normalize={"москв": "Москва", "свердлов": "Свердловская область"},
    )
    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(cfg_path))
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    result = processor.extract_intent("покажи выручку в регионе москва")

    region_value = result["entities"]["filters"].get("region")
    assert region_value == "Москва", (
        f"Должно быть из yaml-словаря, а не title(); получено: {region_value!r}"
    )


def test_regions_unknown_passes_through_without_title(tmp_path, monkeypatch):
    """Неизвестный регион не должен модифицироваться через ``title()``."""
    cfg_path = tmp_path / "nlu_morphemes.yaml"
    _write_yaml(cfg_path, enabled=True, regions_normalize={})
    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(cfg_path))
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    result = processor.extract_intent("покажи в регионе нижний-новгород")

    region_value = result["entities"]["filters"].get("region")
    # title() превратил бы в "Нижний-Новгород". Контракт 3.13 — отсутствие
    # title() в коде: возвращается оригинальный токен (lower-case strip).
    assert region_value is not None
    assert region_value == "нижний-новгород"


# --------------------------------------------------------------------------
# 3.14: end:None → открытый интервал, не IS NULL
# --------------------------------------------------------------------------


def test_end_none_means_open_interval_not_is_null(tmp_path, monkeypatch):
    """Одиночная дата должна давать ``{"start": ...}`` без ``end`` ключа."""
    cfg_path = tmp_path / "nlu_morphemes.yaml"
    _write_yaml(cfg_path, enabled=True)
    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(cfg_path))
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    result = processor.extract_intent("после 2024-01-15 покажи всё")

    date_range = result["entities"]["filters"].get("date_range")
    assert date_range is not None
    assert date_range.get("start") == "2024-01-15"
    # Ключ "end" не должен присутствовать (иначе downstream трактует как IS NULL).
    assert "end" not in date_range, (
        f"Ожидался открытый интервал без 'end', получено: {date_range!r}"
    )


def test_two_dates_still_produce_closed_range(tmp_path, monkeypatch):
    """Регресс: два совпавших даты дают закрытый интервал."""
    cfg_path = tmp_path / "nlu_morphemes.yaml"
    _write_yaml(cfg_path, enabled=True)
    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(cfg_path))
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    result = processor.extract_intent("между 2024-01-01 и 2024-12-31")

    date_range = result["entities"]["filters"].get("date_range")
    assert date_range == {"start": "2024-01-01", "end": "2024-12-31"}


# --------------------------------------------------------------------------
# 3.12: schema_filtering использует morphemes из yaml
# --------------------------------------------------------------------------


def test_schema_filtering_uses_morphemes_from_yaml():
    """Entity 'revenue' должна цеплять колонку через морфему-substring из yaml."""
    morphemes_index = {
        "revenue": ["выруч", "amount", "сумм"],
        "region": ["регион", "облас"],
    }
    db_schema = {
        "sales": {
            "columns": {
                # 'amount_rub' ↔ морфема 'amount' (а не сама entity 'revenue').
                "amount_rub": {"type": "DECIMAL"},
                "id": {"type": "INTEGER"},
            }
        },
        "unrelated": {
            "columns": {"foo": {"type": "TEXT"}},
        },
    }

    # Substring-only — ничего не найдёт ('revenue' не в 'amount_rub').
    plain = SchemaRelevanceFilter.find_relevant_tables_by_entities(
        ["revenue"], db_schema, morphemes_index={}
    )
    assert "sales" not in plain

    # С morphemes_index — должен найти sales через морфему 'amount'.
    with_morphemes = SchemaRelevanceFilter.find_relevant_tables_by_entities(
        ["revenue"], db_schema, morphemes_index=morphemes_index
    )
    assert "sales" in with_morphemes
    assert "unrelated" not in with_morphemes


def test_schema_filtering_score_uses_morphemes():
    """Score-функция тоже расширяется морфемами (см. 3.12, строки 280/300)."""
    morphemes_index = {"revenue": ["выруч", "доход"]}

    table_schema = {
        "columns": {
            "vyruchka": {"type": "DECIMAL", "description": "месячная выручка"},
        }
    }

    score_plain = SchemaRelevanceFilter.score_table_relevance(
        "sales", table_schema, ["revenue"], morphemes_index={}
    )
    score_morph = SchemaRelevanceFilter.score_table_relevance(
        "sales", table_schema, ["revenue"], morphemes_index=morphemes_index
    )

    # Без morphemes колонка не цепляется (substring не совпадёт).
    # С morphemes — совпадает по 'выруч' и в имени, и в описании.
    assert score_morph > score_plain


def test_expand_entity_tokens_backward_compat():
    """Без morphemes_index расширение не происходит (back-compat)."""
    assert _expand_entity_tokens("revenue", None) == ["revenue"]
    assert _expand_entity_tokens("revenue", {}) == ["revenue"]


def test_expand_entity_tokens_canonical_match():
    """Entity = canonical → возвращаются все морфемы группы."""
    idx = {"revenue": ["выруч", "сумм"]}
    tokens = _expand_entity_tokens("revenue", idx)
    assert "revenue" in tokens
    assert "выруч" in tokens
    assert "сумм" in tokens
