"""Контрактные тесты yaml-конфига PII-категорий (T4.6).

Проверяют:
  * для каждого уровня sensitivity {low, medium, high} промпт содержит
    ТОЧНО тот же набор PII-категорий, что и до рефакторинга
    (regression-критерий);
  * выбор юрисдикции через env ``PII_JURISDICTION``;
  * подмену пути конфига через env ``PII_CATEGORIES_PATH``;
  * fail-fast при отсутствии yaml (``FileNotFoundError``);
  * fail-fast при неизвестном sensitivity (``ValueError``);
  * отсутствие хардкода PII-категорий в ``prompts.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_tools.text_to_sql import pii_categories_config
from custom_tools.text_to_sql.prompts import build_pii_detection_prompt


_ENV_PATH_VAR = "PII_CATEGORIES_PATH"
_ENV_JURISDICTION_VAR = "PII_JURISDICTION"
_PROMPTS_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_tools"
    / "text_to_sql"
    / "prompts.py"
)

# Эталонные строки PII-описания ровно как в коде ДО T4.6.
# Любое изменение здесь — изменение поведения, которое надо обсуждать.
_LEGACY_PII_DESCRIPTIONS = {
    "high": (
        "любые данные связанные с личностью: имя, email, телефон, адрес, "
        "возраст, зарплата, должность, паспорт, SSN, IP"
    ),
    "low": (
        "только критичные PII: полное имя, email, телефон, паспорт, SSN, "
        "номера карт"
    ),
    "medium": (
        "персональные данные: имя, email, телефон, адрес, паспорт, SSN, "
        "номера карт, но НЕ города, возраст или зарплаты"
    ),
}


@pytest.fixture(autouse=True)
def _reset_pii_cache(monkeypatch):
    """Гарантирует, что кэш конфига не протекает между тестами."""
    pii_categories_config.reset_cache()
    yield
    monkeypatch.delenv(_ENV_PATH_VAR, raising=False)
    monkeypatch.delenv(_ENV_JURISDICTION_VAR, raising=False)
    pii_categories_config.reset_cache()


def _expected_prompt(columns, descr):
    return (
        f"Определи, какие из колонок содержат PII ({descr}). "
        'Верни ТОЛЬКО JSON {"columns": ["col1", "col2", ...]} '
        "с названиями колонок для маскирования.\n"
        f"Колонки: {json.dumps(columns, ensure_ascii=False)}"
    )


@pytest.mark.parametrize("sensitivity", ["low", "medium", "high"])
def test_pii_prompt_default_matches_legacy(sensitivity):
    """Дефолтный yaml (юрисдикция ru) → промпт идентичен старому."""
    columns = ["name", "email", "city"]
    prompt = build_pii_detection_prompt(columns, sensitivity)
    expected = _expected_prompt(columns, _LEGACY_PII_DESCRIPTIONS[sensitivity])
    assert prompt == expected, (
        f"Регрессия для sensitivity={sensitivity}:\n"
        f"actual:   {prompt!r}\nexpected: {expected!r}"
    )


def test_pii_prompt_jurisdiction_via_env(monkeypatch):
    """``PII_JURISDICTION=eu`` → берётся eu-секция (другой набор категорий)."""
    monkeypatch.setenv(_ENV_JURISDICTION_VAR, "eu")
    pii_categories_config.reset_cache()

    # На low в eu-юрисдикции адрес уже PII, поэтому промпт отличается
    # от ru-low. Проверяем содержательное различие.
    prompt_low = build_pii_detection_prompt(["a"], "low")
    assert "адрес" in prompt_low, (
        "eu/low должна включать 'адрес' (отличие от ru/low)"
    )

    # На medium в eu добавляются возраст и зарплата
    # (отличие от ru/medium, где они в негативном хвосте).
    prompt_medium = build_pii_detection_prompt(["a"], "medium")
    assert "возраст" in prompt_medium
    assert "зарплата" in prompt_medium
    # И не должно быть негативного хвоста "но НЕ ..." для eu.
    assert "но НЕ" not in prompt_medium


def test_pii_prompt_loads_from_yaml(tmp_path, monkeypatch):
    """Подмена пути даёт кастомные категории."""
    custom_yaml = tmp_path / "categories.yaml"
    custom_yaml.write_text(
        """
version: 2
policy:
  sensitivity_levels: ["low", "medium", "high"]
default_jurisdiction: test_jur
jurisdictions:
  test_jur:
    description: "test"
    prefixes:
      low: "минимальный набор"
      medium: "средний набор"
      high: "полный набор"
    negatives: {}
    sync_masking:
      rules:
        - id: email
          pattern: '[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}'
          replacement: '[EMAIL]'
    categories:
      - id: only_one
        label_ru: "только_токен_xyz"
        sensitivities: [low, medium, high]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_PATH_VAR, str(custom_yaml))
    pii_categories_config.reset_cache()

    prompt = build_pii_detection_prompt(["c1"], "medium")
    assert "только_токен_xyz" in prompt
    assert "средний набор: только_токен_xyz" in prompt
    # Стандартные категории не должны протечь.
    assert "паспорт" not in prompt
    assert "email" not in prompt


def test_pii_prompt_fails_fast_when_yaml_missing(tmp_path, monkeypatch):
    """Несуществующий путь → ``FileNotFoundError`` без молчаливых дефолтов."""
    missing = tmp_path / "absent.yaml"
    monkeypatch.setenv(_ENV_PATH_VAR, str(missing))
    pii_categories_config.reset_cache()

    with pytest.raises(FileNotFoundError):
        build_pii_detection_prompt(["a"], "medium")


def test_pii_no_categories_hardcoded_in_python():
    """В ``build_pii_detection_prompt`` не должно остаться категорий
    PII (email/SSN/паспорт/зарплата/имя/IP/...). Они должны жить в yaml.

    Проверяем тело именно этой функции, не весь файл (модуль может
    содержать «email» в других контекстах).
    """
    source = _PROMPTS_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"def build_pii_detection_prompt\(.*?\n(?=def |\Z)",
        source,
        flags=re.DOTALL,
    )
    assert match, "Не нашли тело build_pii_detection_prompt"
    body = match.group(0)

    forbidden = (
        "email",
        "SSN",
        "паспорт",
        "зарплат",
        "возраст",
        "телефон",
        "номера карт",
    )
    leaked = [tok for tok in forbidden if tok in body]
    assert leaked == [], (
        "Категории PII всё ещё хардкодятся в build_pii_detection_prompt — "
        f"должны быть в yaml: {leaked}"
    )


def test_pii_invalid_sensitivity_raises():
    """Неизвестный уровень чувствительности → ``ValueError``."""
    with pytest.raises(ValueError):
        build_pii_detection_prompt(["a"], "extreme")


def test_pii_unknown_jurisdiction_fails_fast(monkeypatch):
    """Неизвестная юрисдикция через env → ``KeyError`` (fail-fast)."""
    monkeypatch.setenv(_ENV_JURISDICTION_VAR, "no_such_jur")
    pii_categories_config.reset_cache()
    with pytest.raises(KeyError):
        build_pii_detection_prompt(["a"], "medium")


# ============================================================================
# Тесты для T2-pii-audit: card_number в RU, phone-regex ужесточение
# ============================================================================


def test_ru_card_number_rule_masks_visa():
    """card_number в RU sync_masking: типичный Visa-номер маскируется как [CARD]."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("4111 1111 1111 1111") == "[CARD]"


def test_ru_card_number_rule_masks_compact_digits():
    """Компактные 16 цифр без пробелов маскируются как [CARD]."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    result = pii_mask_sync("4111111111111111")
    assert result == "[CARD]"


def test_ru_card_number_inn_not_masked_as_card():
    """ИНН с префиксом INN: маскируется как [INN], не как [CARD]."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    masked = pii_mask_sync("INN: 7707083893")
    assert "[INN]" in masked
    assert "[CARD]" not in masked
    assert "7707083893" not in masked


def test_a3_bare_7_phone_not_matched():
    """bare 79991234567 без разделителя — не телефон (защита ОКТМО/ОКАТО 7xxx).

    Намеренное изменение поведения: bare `7XXXXXXXXXX` без разделителя
    больше не матчится как телефон, чтобы исключить ложные срабатывания
    на территориальные коды, начинающиеся с 7.
    """
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("79991234567") == "79991234567"


def test_a3_plus7_phone_still_matched():
    """+79991234567 (с явным +) — телефон, маскируется."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("+79991234567") == "[PHONE]"


def test_a3_7_with_separator_matched():
    """7 999 123-45-67 (с разделителем) — телефон, маскируется."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("7 999 123-45-67") == "[PHONE]"


def test_a3_8_prefix_phone_still_matched():
    """8 999 123-45-67 — телефон, маскируется."""
    from custom_tools.text_to_sql.core._pii import pii_mask_sync
    assert pii_mask_sync("8 999 123-45-67") == "[PHONE]"
