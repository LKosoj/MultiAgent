import json
import types
from pathlib import Path

import pytest

from custom_tools.storybook import bible_builder
from custom_tools.storybook.entity_generator_utils.prompt_templates import (
    build_canon_image_prompt,
    get_character_nature,
)


def test_pp10_negative_rules_do_not_classify_human_as_robot():
    data = {
        "name": "Dr. Mira",
        "role": "human doctor",
        "no_go_rules": ["no robot parts", "no animal traits"],
    }
    assert get_character_nature(data) == "human"
    assert "HUMAN-ONLY CONSTRAINT" in build_canon_image_prompt("character", data, [])


@pytest.mark.parametrize("nature", ["human", "animal", "anthropomorphic_animal", "robot"])
def test_pp10_explicit_nature_still_has_priority(nature):
    assert get_character_nature({"entity_nature": nature, "no_go_rules": ["no robot parts"]}) == nature


def test_pp10_positive_robot_signal_still_classifies_robot():
    assert get_character_nature({"role": "robot mechanic", "no_go_rules": ["no animal traits"]}) == "robot"


def test_pp11_anthropomorphic_bird_keeps_own_anatomy_without_mammal_requirements():
    prompt = build_canon_image_prompt("character", {
        "name": "Raven", "entity_nature": "anthropomorphic_animal", "species": "raven",
        "immutable_attributes": {"unique_features": ["beak", "feathers", "wings"]},
    }, [])
    constraint = prompt.split("Character 'Raven' must have", 1)[0]
    assert "humanoid posture" in constraint
    assert "canonical species traits" in constraint
    assert "fur + muzzle" not in constraint


def test_pp11_mammal_features_remain_available_from_its_canon_data():
    prompt = build_canon_image_prompt("character", {
        "name": "Fox", "entity_nature": "anthropomorphic_animal",
        "immutable_attributes": {"unique_features": ["fur", "muzzle"]},
    }, [])
    assert "fur, muzzle" in prompt


@pytest.mark.parametrize("language", ["ru", "en", "fr"])
def test_pp13_bible_request_carries_requested_language(tmp_path, monkeypatch, language):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    source = tmp_path / "project" / "10_synopsis"
    source.mkdir(parents=True)
    (source / "synopsis.json").write_text("{}", encoding="utf-8")
    (source / "beats.json").write_text("[]", encoding="utf-8")
    captured = []

    def fake_api(**kwargs):
        captured.append(kwargs)
        return json.dumps({"characters": [], "locations": [], "consistency_rules": []})

    monkeypatch.setattr(bible_builder, "call_openai_api", fake_api)
    bible_builder.bible_builder_tool("session", "project", language=language)

    assert len(captured) == 1
    request = captured[0]
    assert json.loads(request["prompt"])["language"] == language
    assert "characters" in request["system_prompt"]
    assert "locations" in request["system_prompt"]
    assert f"descriptive text value in the requested language `{language}`" in request["system_prompt"]
    assert "Keep JSON keys unchanged and preserve proper names unchanged" in request["system_prompt"]


def test_pp13_baseline_request_omits_language(tmp_path, monkeypatch):
    source = Path("/tmp/multiagent-prompt-remediation.Ghskbe/custom_tools/storybook/bible_builder.py")
    baseline = types.ModuleType("custom_tools.storybook.bible_builder_baseline")
    baseline.__package__ = "custom_tools.storybook"
    baseline.__file__ = str(source)
    exec(compile(source.read_bytes(), str(source), "exec"), baseline.__dict__)

    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    synopsis = tmp_path / "project" / "10_synopsis"
    synopsis.mkdir(parents=True)
    (synopsis / "synopsis.json").write_text("{}", encoding="utf-8")
    (synopsis / "beats.json").write_text("[]", encoding="utf-8")
    captured = []

    def fake_api(**kwargs):
        captured.append(kwargs)
        return json.dumps({"characters": [], "locations": [], "consistency_rules": []})

    baseline.call_openai_api = fake_api
    baseline.bible_builder_tool("session", "project", language="fr")

    assert "language" not in json.loads(captured[0]["prompt"])
