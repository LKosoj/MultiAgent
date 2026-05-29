"""Контрактные тесты yaml-конфига значимости колонок (T4.3 + B2 fix).

Проверяют:
  * подмена пути через ``TEXT_TO_SQL_SIGNIFICANCE_PATH`` действительно
    подменяет источник списков;
  * отсутствие конфига приводит к ``FileNotFoundError`` (fail-fast,
    никаких пустых дефолтов);
  * доменных слов (oktmo / «значение показател» / «год отчет») в
    ``schema_metadata.py`` больше нет — они переехали в yaml;
  * дефолтный профиль НЕ содержит доменных терминов (oktmo, «значение
    показател» и т.п.);
  * активация ``muni_ru`` через env возвращает доменные термины как
    significant за счёт union с default;
  * v1 yaml (без секции profiles) корректно мигрирует и логирует
    DEPRECATION warning.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from custom_tools.text_to_sql import significance_config
from custom_tools.text_to_sql.schema_metadata import ColumnMetadataHelper


_ENV_PATH_VAR = "TEXT_TO_SQL_SIGNIFICANCE_PATH"
_ENV_PROFILE_VAR = "TEXT_TO_SQL_SIGNIFICANCE_PROFILE"
_SCHEMA_METADATA_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_tools"
    / "text_to_sql"
    / "schema_metadata.py"
)


@pytest.fixture(autouse=True)
def _reset_significance_cache(monkeypatch):
    """Гарантирует, что кэш конфига и env не протекают между тестами."""
    significance_config.reset_cache()
    yield
    monkeypatch.delenv(_ENV_PATH_VAR, raising=False)
    monkeypatch.delenv(_ENV_PROFILE_VAR, raising=False)
    significance_config.reset_cache()


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_significance_loads_from_yaml(tmp_path, monkeypatch):
    """Подменяем yaml — кастомный keyword из описания делает колонку значимой."""
    custom_yaml = tmp_path / "significance.yaml"
    _write_yaml(
        custom_yaml,
        """
version: 2
profiles:
  default:
    high_priority_exact:
      - my_metric
    high_priority_compound: []
    medium_priority_patterns: []
    critical_description_keywords:
      - "custom marker"
""",
    )
    monkeypatch.setenv(_ENV_PATH_VAR, str(custom_yaml))
    significance_config.reset_cache()

    # exact match из подменённого yaml
    assert ColumnMetadataHelper.is_semantic_significant_column("my_metric", {}) is True

    # keyword в описании из подменённого yaml
    assert (
        ColumnMetadataHelper.is_semantic_significant_column(
            "arbitrary_col", {"description": "This is a CUSTOM marker column"}
        )
        is True
    )

    # отсутствующий в подменённом yaml легаси-токен значимым не считается
    assert ColumnMetadataHelper.is_semantic_significant_column("oktmo", {}) is False


def test_significance_fails_fast_when_yaml_missing(tmp_path, monkeypatch):
    """Несуществующий путь → FileNotFoundError, без молчаливых дефолтов."""
    missing = tmp_path / "does_not_exist.yaml"
    monkeypatch.setenv(_ENV_PATH_VAR, str(missing))
    significance_config.reset_cache()

    with pytest.raises(FileNotFoundError):
        ColumnMetadataHelper.is_semantic_significant_column("id", {})


def test_significance_no_domain_words_in_python():
    """Доменные строки не должны больше жить в schema_metadata.py."""
    source = _SCHEMA_METADATA_PATH.read_text(encoding="utf-8")
    forbidden = re.compile(r"oktmo|значение показател|год отчет", re.IGNORECASE)
    matches = forbidden.findall(source)
    assert matches == [], (
        "Доменные слова всё ещё в schema_metadata.py — должны быть в yaml: "
        f"{matches}"
    )


def test_significance_default_profile_excludes_oktmo(monkeypatch):
    """Default profile НЕ содержит oktmo и доменных русских терминов.

    Это контракт B2 fix: default — общеотраслевой, без доменных добавок.
    """
    # env не задаём — активный профиль = "default".
    monkeypatch.delenv(_ENV_PROFILE_VAR, raising=False)
    significance_config.reset_cache()

    # доменный exact-term: oktmo НЕ значим в default.
    assert ColumnMetadataHelper.is_semantic_significant_column("oktmo", {}) is False
    # доменный русский keyword: «значение показател» НЕ значим в default.
    assert (
        ColumnMetadataHelper.is_semantic_significant_column(
            "indicator_value", {"description": "Значение показателя за период"}
        )
        is False
    )
    # «год отчет» — тоже доменный, НЕ значим в default.
    assert (
        ColumnMetadataHelper.is_semantic_significant_column(
            "year_col", {"description": "год отчета"}
        )
        is False
    )

    # Общеотраслевые признаки в default ОСТАЮТСЯ значимыми.
    assert ColumnMetadataHelper.is_semantic_significant_column("id", {}) is True
    assert ColumnMetadataHelper.is_semantic_significant_column("year", {}) is True
    # структурный паттерн _id$
    assert ColumnMetadataHelper.is_semantic_significant_column("user_id", {}) is True
    # общеотраслевой keyword в описании
    assert (
        ColumnMetadataHelper.is_semantic_significant_column(
            "pk_col", {"description": "Первичный ключ таблицы"}
        )
        is True
    )
    # бесполезное имя — не значимо
    assert ColumnMetadataHelper.is_semantic_significant_column("foobar", {}) is False


def test_significance_muni_profile_includes_oktmo_via_merge(monkeypatch):
    """Активация ``muni_ru`` через env возвращает oktmo и доменные термины.

    Регрес-сейф: union с default — default-признаки остаются значимыми.
    """
    monkeypatch.setenv(_ENV_PROFILE_VAR, "muni_ru")
    significance_config.reset_cache()

    # доменные добавки из muni_ru
    assert ColumnMetadataHelper.is_semantic_significant_column("oktmo", {}) is True
    assert (
        ColumnMetadataHelper.is_semantic_significant_column(
            "indicator_value", {"description": "Значение показателя за период"}
        )
        is True
    )
    assert (
        ColumnMetadataHelper.is_semantic_significant_column(
            "year_col", {"description": "год отчета"}
        )
        is True
    )

    # default-признаки остаются значимыми после merge
    assert ColumnMetadataHelper.is_semantic_significant_column("id", {}) is True
    assert ColumnMetadataHelper.is_semantic_significant_column("user_id", {}) is True
    assert (
        ColumnMetadataHelper.is_semantic_significant_column(
            "pk_col", {"description": "Первичный ключ таблицы"}
        )
        is True
    )


def test_significance_unknown_profile_raises_key_error(monkeypatch):
    """Несуществующий профиль → KeyError (fail-fast)."""
    monkeypatch.setenv(_ENV_PROFILE_VAR, "no_such_profile")
    significance_config.reset_cache()

    with pytest.raises(KeyError):
        ColumnMetadataHelper.is_semantic_significant_column("id", {})


def test_significance_yaml_v1_legacy_rejected(tmp_path, monkeypatch):
    """v1 yaml (без profiles) отвергается с ValueError (EPIC 5.8).

    Раньше был silent fallback с deprecation warning. Теперь fail-fast.
    """
    legacy_yaml = tmp_path / "significance_v1.yaml"
    _write_yaml(
        legacy_yaml,
        """
version: 1
high_priority_exact:
  - legacy_marker
high_priority_compound: []
medium_priority_patterns:
  - pattern: '_id$'
    description: идентификатор
critical_description_keywords:
  - "legacy keyword"
""",
    )
    monkeypatch.setenv(_ENV_PATH_VAR, str(legacy_yaml))
    significance_config.reset_cache()

    with pytest.raises(ValueError, match="profiles"):
        ColumnMetadataHelper.is_semantic_significant_column("legacy_marker", {})
