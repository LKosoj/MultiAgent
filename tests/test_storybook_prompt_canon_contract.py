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
    source = Path(__file__).parent / "fixtures" / "storybook" / "bible_builder_initial.py"
    baseline = types.ModuleType("custom_tools.storybook.bible_builder_baseline")
    baseline.__package__ = "custom_tools.storybook"
    baseline.__file__ = str(source)
    exec(compile(source.read_bytes(), str(source), "exec"), baseline.__dict__)

    # Исходная версия модуля берёт путь plots/storybooks/... от текущей папки
    monkeypatch.chdir(tmp_path)
    synopsis = tmp_path / "plots" / "storybooks" / "project" / "10_synopsis"
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


def test_collective_character_is_not_forced_into_one_person():
    prompt = build_canon_image_prompt('character', {
        'name': 'Футболисты', 'role': 'Группа мальчишек без индивидуальных лиц',
    }, [])
    assert 'Show ONLY this ONE character' not in prompt
    assert 'multiple characters, group shot' not in prompt
    assert 'GROUP SHEET' in prompt
    assert 'not a single representative' in prompt


def test_location_reference_does_not_copy_another_locations_weather():
    prompt = build_canon_image_prompt('location', {
        'name': 'Париж', 'historical_period': '1920-е годы',
        'environment_context': 'Осень, дождь, без снега и льда',
    }, [])
    assert '1920-е годы' in prompt
    assert 'дождь, без снега и льда' in prompt
    assert 'Target location description takes priority' in prompt


def test_individual_canon_sheet_has_face_and_full_body_views():
    prompt = build_canon_image_prompt('character', {'name': 'Actor'}, [])
    assert 'Face close-up' in prompt
    assert 'FULL-BODY FRONT view, head-to-boots' in prompt
    assert 'FULL-BODY BACK view, head-to-boots' in prompt
    assert 'neutral solid-color background' in prompt
    assert 'character, costume, proportions' in prompt


def test_location_reference_keeps_architectural_plan_and_requested_style():
    prompt = build_canon_image_prompt('location', {'name': 'Room', 'description': 'Photorealistic lived-in apartment'}, [])
    assert 'Photorealistic lived-in apartment' in prompt
    assert 'ARCHITECTURAL VISUALIZATION' in prompt
    assert '1. Top Left: Top-down spatial plan' in prompt
    assert prompt.index('Top-down spatial plan') < prompt.index('Wide establishing shot')
    assert 'derive every perspective view from that exact plan' in prompt
    assert 'never mirror, move, resize, add or remove' in prompt
    assert 'EXACT SAME location with consistent lighting and style' in prompt


def test_outdoor_location_plan_does_not_assume_an_apartment():
    prompt = build_canon_image_prompt('location', {
        'name': 'Street', 'description': 'An open street with tram tracks, trees and streetlights.',
    }, [])
    assert 'tram tracks, trees and streetlights' in prompt
    assert 'Top-down spatial plan' in prompt
    assert 'actually present in the supplied location description' in prompt
    assert not any(word in prompt.lower() for word in ('door', 'window', 'furniture', 'floor plan'))


def test_location_rules_are_shared_but_location_details_are_generation_inputs():
    from custom_tools.storybook.entity_generator_utils.prompt_templates import LOCATION_REFERENCE_RULES
    apartment = build_canon_image_prompt('location', {'name': 'Apartment', 'description': 'sofa beside a window'}, [])
    street = build_canon_image_prompt('location', {'name': 'Street', 'description': 'tram tracks and trees'}, [])
    assert apartment.split('\n\nLOCATION INPUT FOR THIS GENERATION:')[0] == LOCATION_REFERENCE_RULES
    assert street.split('\n\nLOCATION INPUT FOR THIS GENERATION:')[0] == LOCATION_REFERENCE_RULES
    assert 'sofa beside a window' in apartment and 'sofa' not in street
    assert 'tram tracks and trees' in street and 'tram' not in apartment
    assert not any(word in LOCATION_REFERENCE_RULES.lower() for word in ('sofa', 'tram', 'door', 'window', 'furniture', 'ears', 'photoreal'))


def test_character_identity_sheet_is_not_its_narrative_presentation():
    from custom_tools.storybook.entity_generator_utils.prompt_templates import build_canon_image_prompt
    prompt = build_canon_image_prompt('character', {
        'name': 'Presenter', 'role': 'A figure seen in an in-story representation',
        'environment_context': 'Narrative setting and contextual objects'}, [])
    assert 'not as its in-story presentation' in prompt
    assert 'contextual objects must not become scenery or containers' in prompt
    assert 'neutral solid-color background' in prompt
    assert 'Face close-up' in prompt and 'FULL-BODY FRONT' in prompt and 'FULL-BODY BACK' in prompt


def test_appearance_review_compares_labeled_source_and_candidate(tmp_path, monkeypatch):
    import base64
    import utils
    from custom_tools.storybook import artist_batch_edit
    source, candidate = tmp_path / 'source.png', tmp_path / 'candidate.png'
    source.write_bytes(b'approved source')
    candidate.write_bytes(b'candidate')
    calls = []
    def check(**kwargs):
        calls.append(kwargs)
        return '{"passed": true, "violations": []}'
    monkeypatch.setattr(utils, 'call_openai_api', check)
    assert artist_batch_edit._reference_requirements_met(str(candidate),
        {'name': 'Actor', 'reference_requirements': ['Apply only the declared change.']}, str(source))
    request = calls[0]
    assert request['image_url'] is None
    content = request['prompt']
    assert content[0]['text'].startswith('APPROVED SOURCE')
    assert content[1]['image_url']['url'].endswith(base64.b64encode(source.read_bytes()).decode())
    assert content[2]['text'].startswith('CANDIDATE')
    assert content[3]['image_url']['url'].endswith(base64.b64encode(candidate.read_bytes()).decode())
    assert 'do not demand more pronounced features' in request['system_prompt']
