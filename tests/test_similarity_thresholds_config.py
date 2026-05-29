"""
Тесты для similarity_thresholds_config (W9-A7).

Покрывают:
  * fail-fast при отсутствии файла;
  * fail-fast при невалидном top-level;
  * fail-fast при отсутствии профиля default;
  * fail-fast при пропуске поля внутри профиля;
  * fail-fast при значении вне [0.0, 1.0];
  * выбор активного профиля через env TEXT_TO_SQL_SIMILARITY_PROFILE;
  * env-override через resolve_threshold (env > yaml);
  * сохранение текущих дефолтов (no behavioral regression).
"""

import os
from pathlib import Path

import pytest

from custom_tools.text_to_sql import similarity_thresholds_config as stc


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Каждый тест начинается с пустого кэша и дефолтного профиля."""
    monkeypatch.delenv("TEXT_TO_SQL_SIMILARITY_THRESHOLDS_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SIMILARITY_PROFILE", raising=False)
    monkeypatch.delenv("RAG_VECTOR_THRESHOLD", raising=False)
    monkeypatch.delenv("RAG_RERANK_THRESHOLD", raising=False)
    monkeypatch.delenv("SCHEMA_TABLE_MIN_SCORE", raising=False)
    stc.reset_cache()
    yield
    stc.reset_cache()


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "similarity_thresholds.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_default_profile_from_repo_yaml():
    """Конфиг из репозитория грузится и default-профиль доступен."""
    profile = stc.load_similarity_thresholds()
    assert profile.name == "default"
    # Значения должны совпадать с историческими дефолтами (W9-A7 не меняет
    # их, иначе ломаются существующие тесты на env-defaults).
    assert profile.rag_examples_min_score == pytest.approx(0.7)
    assert profile.strategic_memory_min_score == pytest.approx(0.75)
    assert profile.tactical_memory_min_score == pytest.approx(0.5)
    assert profile.schema_linking_min_score == pytest.approx(0.2)


def test_profile_switch_via_env(monkeypatch):
    """Активный профиль управляется env TEXT_TO_SQL_SIMILARITY_PROFILE."""
    monkeypatch.setenv("TEXT_TO_SQL_SIMILARITY_PROFILE", "muni_ru")
    stc.reset_cache()
    assert stc.resolve_active_profile() == "muni_ru"
    profile = stc.load_similarity_thresholds()
    assert profile.name == "muni_ru"


def test_missing_file_raises(tmp_path, monkeypatch):
    """Отсутствие файла → FileNotFoundError (no silent fallback)."""
    monkeypatch.setenv(
        "TEXT_TO_SQL_SIMILARITY_THRESHOLDS_PATH",
        str(tmp_path / "no-such-file.yaml"),
    )
    stc.reset_cache()
    with pytest.raises(FileNotFoundError):
        stc.load_similarity_thresholds()


def test_top_level_must_be_mapping(tmp_path, monkeypatch):
    yaml_path = _write_yaml(tmp_path, "- a\n- b\n")
    monkeypatch.setenv("TEXT_TO_SQL_SIMILARITY_THRESHOLDS_PATH", str(yaml_path))
    stc.reset_cache()
    with pytest.raises(ValueError, match="mapping"):
        stc.load_similarity_thresholds()


def test_profiles_must_contain_default(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path,
        """
profiles:
  muni_ru:
    rag_examples_min_score: 0.7
    strategic_memory_min_score: 0.75
    tactical_memory_min_score: 0.5
    schema_linking_min_score: 0.2
""",
    )
    monkeypatch.setenv("TEXT_TO_SQL_SIMILARITY_THRESHOLDS_PATH", str(yaml_path))
    stc.reset_cache()
    with pytest.raises(ValueError, match="default"):
        stc.load_similarity_thresholds()


def test_missing_required_field(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path,
        """
profiles:
  default:
    rag_examples_min_score: 0.7
    strategic_memory_min_score: 0.75
    tactical_memory_min_score: 0.5
    # schema_linking_min_score пропущено
""",
    )
    monkeypatch.setenv("TEXT_TO_SQL_SIMILARITY_THRESHOLDS_PATH", str(yaml_path))
    stc.reset_cache()
    with pytest.raises(ValueError, match="schema_linking_min_score"):
        stc.load_similarity_thresholds()


def test_value_out_of_range(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path,
        """
profiles:
  default:
    rag_examples_min_score: 1.5
    strategic_memory_min_score: 0.75
    tactical_memory_min_score: 0.5
    schema_linking_min_score: 0.2
""",
    )
    monkeypatch.setenv("TEXT_TO_SQL_SIMILARITY_THRESHOLDS_PATH", str(yaml_path))
    stc.reset_cache()
    with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
        stc.load_similarity_thresholds()


def test_resolve_threshold_yaml_only():
    """Без env-override — значение из активного профиля."""
    assert stc.resolve_threshold("strategic_memory_min_score") == pytest.approx(0.75)
    assert stc.resolve_threshold("tactical_memory_min_score") == pytest.approx(0.5)
    assert stc.resolve_threshold("schema_linking_min_score") == pytest.approx(0.2)


def test_resolve_threshold_env_wins(monkeypatch):
    """Если env_override задан и значение env есть — env приоритет над yaml."""
    monkeypatch.setenv("RAG_VECTOR_THRESHOLD", "0.42")
    value = stc.resolve_threshold(
        "strategic_memory_min_score", env_override="RAG_VECTOR_THRESHOLD"
    )
    assert value == pytest.approx(0.42)


def test_resolve_threshold_env_empty_falls_back_to_yaml(monkeypatch):
    """Пустая env-переменная не считается «задана» — берётся yaml."""
    monkeypatch.setenv("RAG_VECTOR_THRESHOLD", "")
    value = stc.resolve_threshold(
        "strategic_memory_min_score", env_override="RAG_VECTOR_THRESHOLD"
    )
    assert value == pytest.approx(0.75)


def test_resolve_threshold_env_invalid_float(monkeypatch):
    """Невалидный float в env → ValueError (fail-fast, не silent fallback)."""
    monkeypatch.setenv("RAG_VECTOR_THRESHOLD", "not-a-number")
    with pytest.raises(ValueError, match="RAG_VECTOR_THRESHOLD"):
        stc.resolve_threshold(
            "strategic_memory_min_score", env_override="RAG_VECTOR_THRESHOLD"
        )


def test_resolve_threshold_unknown_field():
    with pytest.raises(KeyError, match="unknown field"):
        stc.resolve_threshold("no_such_field")
