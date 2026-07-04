"""M-25: нейтрализация доменных хардкодов в storyboard QA-промптах.

Зеркало tests/test_schema_linking_prompt_generalization.py:
  * дефолтный профиль не добавляет доменных строк (podium/anesthesiologist/
    трибуна/gloves...) — промпт универсален;
  * профиль ``stage_medical`` восстанавливает легаси-набор активного проекта;
  * env STORYBOOK_DOMAIN_PROFILE переключает профиль;
  * подмена yaml через env-путь подменяет источник профилей;
  * отсутствие конфига -> FileNotFoundError; неизвестный профиль -> KeyError;
  * в .py-файлах потока не осталось доменных строк — они переехали в yaml.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from custom_tools.storybook import domain_glossary_config as dg
from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils as su


_ENV_PATH = "STORYBOOK_DOMAIN_GLOSSARY_PATH"
_ENV_PROFILE = "STORYBOOK_DOMAIN_PROFILE"

_REPO = Path(__file__).resolve().parents[1]
_QA_PY = _REPO / "custom_tools" / "storybook" / "shots_prompt_qa.py"
_SHARED_PY = (
    _REPO / "custom_tools" / "storybook"
    / "screenplay_shots_generator_utils" / "shared_utils.py"
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    dg.reset_cache()
    yield
    monkeypatch.delenv(_ENV_PATH, raising=False)
    monkeypatch.delenv(_ENV_PROFILE, raising=False)
    dg.reset_cache()


def test_default_profile_has_no_domain_terms():
    assert dg.compose_qa_domain_prompt() == ""
    assert dg.compose_anchor_glossary_note() == ""
    assert dg.continuity_token_rules() == []


def test_stage_medical_profile_restores_legacy_terms():
    qa = dg.compose_qa_domain_prompt("stage_medical")
    for token in ("podium", "anesthesiologist", "AUDIENCE REACTION ZONE",
                  "bleachers", "трибуна", "микрофон"):
        assert token in qa, token
    note = dg.compose_anchor_glossary_note("stage_medical")
    assert "anesthesiologist" in note
    rules = dg.continuity_token_rules("stage_medical")
    assert any(r["token"] == "gloves" for r in rules)


def test_profile_via_env(monkeypatch):
    monkeypatch.setenv(_ENV_PROFILE, "stage_medical")
    dg.reset_cache()
    qa = dg.compose_qa_domain_prompt()
    assert "podium" in qa
    assert "anesthesiologist" in qa


def test_custom_yaml_via_env_path(tmp_path, monkeypatch):
    custom = tmp_path / "domain_glossary.yaml"
    custom.write_text(
        """
version: 1
profiles:
  default:
    anchor_glossary: []
  custom_demo:
    anchor_glossary:
      - {source: "звездолёт", target: "starship"}
    spatial_locks:
      - lock: "ORBIT GEOMETRY"
        rule: "Ship faces planet."
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_PATH, str(custom))
    dg.reset_cache()

    # Дефолт остаётся пустым.
    assert dg.compose_qa_domain_prompt() == ""
    # Кастомный профиль вставляет свои термины, без легаси-доменных.
    custom_qa = dg.compose_qa_domain_prompt("custom_demo")
    assert "starship" in custom_qa
    assert "ORBIT GEOMETRY" in custom_qa
    assert "podium" not in custom_qa
    assert "anesthesiologist" not in custom_qa


def test_missing_yaml_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_PATH, str(tmp_path / "nope.yaml"))
    dg.reset_cache()
    with pytest.raises(FileNotFoundError):
        dg.compose_qa_domain_prompt()


def test_unknown_profile_fails_fast():
    with pytest.raises(KeyError):
        dg.compose_qa_domain_prompt("no_such_profile")


def test_no_domain_terms_left_in_shots_prompt_qa_py():
    source = _QA_PY.read_text(encoding="utf-8")
    forbidden = re.compile(
        r"anesthesiolog|bleacher|трибуна|монитор|анестезиолог|ассистент|микрофон",
        re.IGNORECASE,
    )
    assert forbidden.findall(source) == [], (
        "Доменные строки всё ещё в shots_prompt_qa.py — должны быть в yaml"
    )


def test_no_stage_geometry_leak_in_default_qa_prompts():
    """M-25 остаток: конкретные театр-специфичные фразы (сцена/подиум/ряды) не
    должны быть захардкожены в универсальных QA/repair-промптах — иначе они
    утекают в модель под дефолтным (пустым) профилем для не-сценических проектов.
    (Открытый список примеров podium/stage/auditorium на строках-примерах —
    легитимен, поэтому проверяем именно эти конкретные фразы, а не токены.)"""
    source = _QA_PY.read_text(encoding="utf-8").lower()
    for phrase in ("seats face stage", "podium on stage side", "stage/podium/aisle"):
        assert phrase not in source, (
            f"Доменная фраза '{phrase}' осталась в shots_prompt_qa.py — "
            "должна быть нейтрализована/вынесена в активный профиль"
        )


def test_no_gloves_hardcode_left_in_shared_utils_py():
    source = _SHARED_PY.read_text(encoding="utf-8")
    assert "gloves" not in source.lower(), (
        "Токен gloves всё ещё захардкожен в shared_utils.py — должен быть в yaml"
    )


def test_negative_prompt_consistency_is_noop_by_default():
    out = su._validate_negative_prompt_consistency(
        "keep this", {"removed": ["blue gloves"], "kept": []}
    )
    assert out == "keep this"


def test_negative_prompt_consistency_applies_profile_rules(monkeypatch):
    monkeypatch.setenv(_ENV_PROFILE, "stage_medical")
    dg.reset_cache()

    added = su._validate_negative_prompt_consistency(
        "", {"removed": ["blue gloves"], "kept": []}
    )
    assert added == "no gloves on hands"

    stripped = su._validate_negative_prompt_consistency(
        "no gloves on hands", {"removed": [], "kept": ["gloves being taken off"]}
    )
    assert stripped == ""
