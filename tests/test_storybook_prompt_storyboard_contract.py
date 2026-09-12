import json

import pytest

from custom_tools.storybook import screenplay_shots_generator as shots
from custom_tools.storybook.screenplay_shots_generator_utils import technical


def _capture(response):
    captured = []

    def fake_api(**kwargs):
        captured.append(kwargs)
        return json.dumps(response)

    return captured, fake_api


def _context(**extra):
    context = {
        "shot_description": "A hero strikes a closed door.",
        "scene_action": "The hero strikes a closed door.",
        "camera_plan": "Static medium shot",
        "available_characters": [{"name": "Hero"}],
        "available_locations": [{"name": "Hall"}],
        "shot_frame_spec": {
            "primary_subject": "Hero",
            "visible_characters": ["Hero"],
            "must_show": ["Hero", "closed door"],
            "must_not_show": [],
            "t0_mode": "frozen",
            "transition_spec": {"event_type": "impact"},
        },
    }
    context.update(extra)
    return context


def _artistic_response():
    return {
        "main_subject": "Hero",
        "camera_position": "in_front",
        "character_orientation": "three_quarter",
        "spatial_composition": "Hero center",
        "point_of_view": "objective",
        "initial_state_summary": "Hero is still.",
        "english_prompt": "Create a medium shot of Hero.",
        "negative_prompt": "blurry",
        "reference_image_paths": [],
        "reference_roles_instruction": "",
        "characters": ["Hero"],
    }


def test_pp06_start_and_end_keep_english_prompt_english_for_ru_and_en(monkeypatch):
    monkeypatch.setattr(shots, "black_screen_storyboard_shot", lambda *_args: False)
    monkeypatch.setattr(
        shots, "enrich_shot_frame_spec_environment_delta_via_llm", lambda spec, **_kwargs: spec,
    )
    monkeypatch.setattr("utils.call_openai_api", lambda **_kwargs: pytest.fail("unexpected LLM call"))
    monkeypatch.setattr(
        "custom_tools.storybook.screenplay_shots_generator_utils.shared_utils.call_openai_api",
        lambda **_kwargs: pytest.fail("unexpected shared LLM call"),
    )
    technical_params = {
        "characters": ["Hero"], "main_subject": "Hero", "shot_size": "Medium shot",
        "location_context": {}, "camera_position": "in_front",
        "character_orientation": "three_quarter", "spatial_composition": "Hero center",
    }
    end_params = {
        "final_shot_size": "Medium shot", "subject_scale_ratio": 1.0,
        "final_camera_position": "in_front", "final_character_orientation": "three_quarter",
        "final_camera_angle": "Eye-level", "final_point_of_view": "objective",
        "final_lighting_style": "High-key", "prop_continuity": {},
        "spatial_changes_from_start": "none",
    }
    for language in ("ru", "en"):
        captured, fake_api = _capture(_artistic_response())
        monkeypatch.setattr(shots, "call_openai_api", fake_api)
        assert shots._generate_shot_artistic(technical_params, _context(), language) is not None
        assert shots._generate_end_shot_artistic(
            end_params, _artistic_response(), "", _context(), language=language,
        ) is not None
        assert len(captured) == 2
        for request in captured:
            text = request["system_prompt"] + request["prompt"]
            assert "`english_prompt`: ALWAYS in English" in text or "english_prompt" in text and "English" in text
            assert "Пиши `english_prompt` и `negative_prompt` строго на" not in text
            assert "только в `negative_prompt` допустимы" not in text
            assert "Если язык `negative_prompt` не английский" in request["prompt"]
            assert "кроме стандартных camera/lens терминов и авторизованных readable texts" in request["prompt"]


@pytest.mark.parametrize(
    ("t0_mode", "event_type"),
    [
        ("frozen", "impact"),
        ("frozen", "emergence"),
        ("frozen", None),
        ("early_motion", "impact"),
        ("mid_action", "impact"),
    ],
)
def test_pp07_frozen_only_freezes_start_not_event_transition(monkeypatch, t0_mode, event_type):
    captured, fake_api = _capture({"video_prompt": "Camera locks off at eye level; Hero strikes door then door splinters; dust drifts; sharply"})
    monkeypatch.setattr(shots, "call_openai_api", fake_api)
    context = _context(shot_frame_spec={
        "primary_subject": "Hero", "visible_characters": ["Hero"],
        "must_show": ["Hero", "closed door"], "must_not_show": [],
        "t0_mode": t0_mode,
        "transition_spec": {"event_type": event_type} if event_type else {},
    })
    result = shots._generate_transition_video_prompt(
        _artistic_response(), _artistic_response(), context,
    )
    assert result is not None
    system = captured[0]["system_prompt"]
    payload = json.loads(captured[0]["prompt"].removeprefix("INPUT:\n"))
    assert "START is a frozen pre-action pose at T=0" in system
    assert "MUST NOT contain motion verbs" not in system
    assert "cause" in system.lower() and "follow-through" in system
    assert payload["t0_mode"] == t0_mode
    assert payload["transition_spec"] == ({"event_type": event_type} if event_type else {})


@pytest.mark.parametrize(
    "description",
    [
        "Camera pans slowly right.",
        "Camera tilts up.",
        "Static camera while Hero stands up.",
        "Camera dollies in toward Hero.",
        "Camera zooms out from Hero.",
    ],
)
def test_pp08_end_technical_separates_scale_change_from_pan_tilt_pose(monkeypatch, description):
    captured, fake_api = _capture({
        "characters": ["Hero"], "location": "Hall", "final_shot_size": "Medium shot",
        "final_camera_angle": "Eye-level", "final_lighting_style": "High-key",
        "final_color_palette": "cold", "final_camera_position": "in_front",
        "final_character_orientation": "three_quarter", "final_spatial_composition": "Hero center",
        "final_point_of_view": "objective", "spatial_changes_from_start": "pan right",
        "camera_movement_completed": "true", "composition_stability": "stable",
        "continuity_score": 10, "next_shot_compatibility": "", "framing_delta_percent": 0,
        "subject_scale_ratio": 1.0, "final_camera_yaw_deg": 10, "final_camera_pitch_deg": 0,
        "final_subject_yaw_deg": 0, "final_focus_target": "foreground",
        "final_depth_of_field": "normal", "main_subject": "Hero",
        "final_depth_order": [], "prop_continuity": {},
    })
    monkeypatch.setattr(technical, "call_openai_api", fake_api)
    technical._analyze_end_shot_technical(
        {"characters": ["Hero"], "location": "Hall", "shot_size": "Medium shot"},
        _context(shot_description=description),
    )
    text = captured[0]["system_prompt"] + captured[0]["prompt"]
    assert description in captured[0]["prompt"]
    assert "Pan/tilt, поза, темп или crane" in text
    assert "stands up" in text and "delta=0, ratio=1.0" in text
    assert "При ЛЮБОМ явном упоминании движения" not in text
    assert all(token in text for token in ("dolly", "zoom", "ОБЯЗАТЕЛЬНО установи числовые дельты"))


def test_pp09_start_examples_keep_frozen_environment_before_change(monkeypatch):
    captured, fake_api = _capture({"characters": ["Hero"], "location": "Hall"})
    monkeypatch.setattr(technical, "call_openai_api", fake_api)
    technical._analyze_shot_technical(_context(shot_description="A trap floor opens under Hero."))
    system = captured[0]["system_prompt"]
    assert "first hairline crack at edges" not in system
    assert "door barely ajar" not in system
    assert "Поверхность ЦЕЛОСТНАЯ и СТАБИЛЬНАЯ" in system
