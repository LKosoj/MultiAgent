import json
import sys
import types
from pathlib import Path

import pytest



agent_command_stub = types.ModuleType("agent_command")
agent_command_stub.model_hard = "test-model-hard"
agent_command_stub.model_code = "test-model-code"
agent_command_stub.model_ultimate = "test-model-ultimate"
agent_command_stub.model_lite = "test-model-lite"
agent_command_stub.model_mapping = {}
sys.modules.setdefault("agent_command", agent_command_stub)

utils_stub = types.ModuleType("utils")
utils_stub.call_openai_api = lambda *args, **kwargs: '{"is_black_screen": false}'
utils_stub.extract_json_from_markdown = lambda text: text
utils_stub.parse_llm_json = lambda text: json.loads(text)
utils_stub.translate_prompts_in_items = lambda *args, **kwargs: args[0]
sys.modules.setdefault("utils", utils_stub)

from custom_tools.storybook import screenplay_shots_generator as shots_generator


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    "previous_position,next_position,expected_link",
    [
        ("in_front", "in_front; камера на уровне глаз", True),
        ("behind", "behind — камера за плечом персонажа", True),
        ("front", "in_front, eye level", True),
        ("behind", "in_front; камера на уровне глаз", False),
        ("in_front", "камера перед двумя сотрудниками на складе", True),
        ("неопределённый ракурс", "behind", True),
        ("in_front", "сзади", False),
        ("behind", "сзади", True),
    ],
)
def test_camera_position_explanation_preserves_actual_viewpoint(
    tmp_path, monkeypatch, previous_position, next_position, expected_link
):
    from custom_tools.storybook import style_keeper

    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(style_keeper, "ensure_image_style_catalog", lambda *args: {})
    contexts = []
    generated_contexts = []

    def build_context(**kwargs):
        contexts.append(kwargs)
        return {}

    monkeypatch.setattr(shots_generator, "_build_extended_context", build_context)
    def generate(**kwargs):
        generated_contexts.append(kwargs["extended_context"])
        return {"camera_position": next_position, "english_prompt": "START"}

    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", generate)
    monkeypatch.setattr(shots_generator, "_create_shot_item", lambda **kwargs: kwargs)
    result = shots_generator._convert_end_to_start_shot_item(
        previous_end_shot_item={"output_path": "previous_end.png", "camera_plan": "Medium shot"},
        previous_end_llm_result={"final_camera_position": previous_position},
        project_id="viewpoint_explanation", scene_number=1, shot_number=2,
        page_number=1, item_number=3, camera_plan="Medium shot", timing="2s",
        characters_data=[], locations_data=[], scene_action="Герой ждёт",
        shot_description="Герой стоит у стола", scene_characters=[], language="ru",
    )
    assert result["is_linked_start"] is expected_link
    assert result["should_use_prev_end_as_reference"] is expected_link
    assert len(contexts) == (1 if expected_link else 2)
    assert generated_contexts[0]["previous_shot"]["final_camera_position"] == previous_position


@pytest.mark.parametrize(
    "previous_position,next_position",
    [("side", "in_front"), ("in_front", "side"), ("side", "behind"), ("behind", "side")],
)
def test_adjacent_camera_positions_keep_reference_link_without_full_copy(
    tmp_path, monkeypatch, previous_position, next_position
):
    from custom_tools.storybook import style_keeper

    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(style_keeper, "ensure_image_style_catalog", lambda *args: {})
    contexts = []
    generated = []

    def build_context(**kwargs):
        contexts.append(kwargs)
        return {}

    monkeypatch.setattr(shots_generator, "_build_extended_context", build_context)
    monkeypatch.setattr(
        shots_generator,
        "_generate_shot_prompt",
        lambda **kwargs: generated.append(kwargs) or {
            "camera_position": next_position,
            "english_prompt": "START",
        },
    )
    monkeypatch.setattr(shots_generator, "_create_shot_item", lambda **kwargs: kwargs)

    result = shots_generator._convert_end_to_start_shot_item(
        previous_end_shot_item={
            "output_path": "previous_end.png",
            "camera_plan": "Medium shot",
        },
        previous_end_llm_result={
            "final_camera_position": previous_position,
            "final_shot_size": "Medium shot",
            "should_link_as_next_start": "true",
        },
        project_id="adjacent_camera_positions",
        scene_number=1,
        shot_number=2,
        page_number=1,
        item_number=3,
        camera_plan="Medium shot",
        timing="2s",
        characters_data=[],
        locations_data=[],
        scene_action="A worker crosses a warehouse",
        shot_description="A worker near a doorway",
        scene_characters=[],
        language="en",
    )

    assert len(generated) == 1
    assert len(contexts) == 1
    assert result["is_linked_start"] is True
    assert result["should_use_prev_end_as_reference"] is True
    assert result["copy_from_previous_end"] is False
    assert result["link_type"] == "reference"


@pytest.mark.parametrize(
    "camera_plan,expected_copy",
    [("SLOW PUSH-IN EXTREME CLOSE-UP", False), ("EXTREME WIDE SHOT", True), ("FULL SHOT", False)],
)
def test_extreme_closeup_does_not_full_copy_closeup_end(monkeypatch, tmp_path, camera_plan, expected_copy):
    from custom_tools.storybook import style_keeper

    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(style_keeper, "ensure_image_style_catalog", lambda *args: {})
    monkeypatch.setattr(shots_generator, "_build_extended_context", lambda **kwargs: {})
    monkeypatch.setattr(
        shots_generator,
        "_generate_shot_prompt",
        lambda **kwargs: {
            "camera_position": "in_front",
            "english_prompt": "START",
            "final_shot_size": "Extreme close-up",
        },
    )
    monkeypatch.setattr(shots_generator, "_create_shot_item", lambda **kwargs: kwargs)

    result = shots_generator._convert_end_to_start_shot_item(
        previous_end_shot_item={
            "output_path": "previous_end.png",
            "camera_plan": "Wide shot" if expected_copy else "Close-up",
        },
        previous_end_llm_result={
            "final_camera_position": "in_front",
            "final_shot_size": "Wide shot" if expected_copy else "Close-up",
            "should_link_as_next_start": "true",
        },
        project_id="extreme_closeup", scene_number=1, shot_number=2,
        page_number=1, item_number=3,
        camera_plan=camera_plan, timing="2s",
        characters_data=[], locations_data=[], scene_action="Neutral action",
        shot_description="A neutral close-up", scene_characters=[], language="en",
    )

    assert result["copy_from_previous_end"] is expected_copy
    assert result["link_type"] == ("reference" if not expected_copy else "full_copy")


def test_linked_start_state_is_used_for_next_end(tmp_path, monkeypatch):
    project_id = "linked_start_state"
    base = tmp_path / project_id
    _write_json(
        base / "91_screenplay" / "screenplay.json",
        {
            "screenplay": [
                {
                    "scene_number": 1,
                    "action": "Герой идёт по комнате",
                    "characters": ["Герой"],
                    "storyboard": [
                        {
                            "shot_number": 1,
                            "description": "Герой входит",
                            "camera_plan": "Close-up",
                            "timing": "1s",
                        },
                        {
                            "shot_number": 2,
                            "description": "Герой останавливается",
                            "camera_plan": "Medium shot",
                            "timing": "1s",
                        },
                    ],
                }
            ]
        },
    )
    _write_json(base / "20_bible" / "characters.json", [{"name": "Герой"}])
    _write_json(base / "20_bible" / "locations.json", [])
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))

    class FakeFuture:
        def __init__(self, fn, *args, **kwargs):
            self._result = fn(*args, **kwargs)

        def result(self):
            return self._result

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, *args, **kwargs):
            return FakeFuture(fn, *args, **kwargs)

    captured_end_start = {}

    def fake_build_extended_context(**kwargs):
        shot_number = kwargs["shot_number"]
        return {
            "shot_number": shot_number,
            "shot_frame_spec": {"primary_subject": "Герой", "must_show": ["Герой"]},
            "full_shot_frame_spec": {"primary_subject": "Герой", "must_show": ["Герой"]},
            "shot_frame_spec_cache_key": f"scene_1_shot_{shot_number}",
            "scene_continuity_facts": {},
            "location_time": "",
            "location_canon_name": "",
        }

    def fake_generate_shot_prompt(
        extended_context,
        shot_type,
        video_prompt="",
        start_llm_result=None,
        language="en",
    ):
        shot_number = extended_context["shot_number"]
        if shot_type == "end" and shot_number == 2:
            captured_end_start.update(start_llm_result or {})
        if shot_type == "start":
            return {
                "english_prompt": "INITIAL START",
                "negative_prompt": "negative",
                "characters": ["Герой"],
                "camera_position": "front",
                "initial_state_summary": "INITIAL STATE",
                "add_end_shot": "true",
                "should_link_as_next_start": "true",
                "should_use_prev_end_as_reference": "false",
            }
        if shot_number == 1:
            return {
                "english_prompt": "OLD PREVIOUS END",
                "negative_prompt": "negative",
                "characters": ["Герой"],
                "camera_position": "front",
                "initial_state_summary": "OLD STATE",
                "final_shot_size": "Wide shot",
                "should_link_as_next_start": "true",
                "should_use_prev_end_as_reference": "false",
            }
        return {
            "english_prompt": "SECOND END",
            "negative_prompt": "negative",
            "characters": ["Герой"],
            "camera_position": "side",
            "initial_state_summary": "SECOND END STATE",
            "should_link_as_next_start": "false",
            "should_use_prev_end_as_reference": "false",
        }

    def fake_convert_end_to_start_shot_item(**kwargs):
        return {
            "shot_type": "start",
            "shot_number": 2,
            "english_prompt": "CURRENT START",
            "camera_plan": "Medium shot",
            "camera_position": "side",
            "initial_state_summary": "CURRENT STATE",
            "final_shot_size": "Medium shot",
            "shot_size": "Medium shot",
            "lighting_style": "soft window light",
            "characters": [{"name": "Герой"}],
            "locations": [],
            "output_path": str(base / "current-start.png"),
            "video_prompt": "",
            "_start_llm_result": {
                "english_prompt": "CURRENT START",
                "shot_size": "Medium shot",
                "camera_position": "side",
                "initial_state_summary": "CURRENT STATE",
                "lighting_style": "soft window light",
            },
        }

    monkeypatch.setattr(shots_generator, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(shots_generator, "as_completed", lambda futures: list(futures))
    monkeypatch.setattr(shots_generator, "_build_extended_context", fake_build_extended_context)
    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", fake_generate_shot_prompt)
    monkeypatch.setattr(
        shots_generator, "_convert_end_to_start_shot_item", fake_convert_end_to_start_shot_item
    )
    monkeypatch.setattr(
        shots_generator,
        "_generate_transition_video_prompt",
        lambda **kwargs: "Camera moves smoothly through the room",
    )
    monkeypatch.setattr(shots_generator, "_generate_fcpxml", lambda *args, **kwargs: None)
    monkeypatch.setattr(shots_generator, "_generate_photo_fcpxml", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        shots_generator,
        "resolve_video_model_capabilities",
        lambda *args, **kwargs: {"supported_durations": [1, 5, 10], "warnings": []},
    )

    result = shots_generator.screenplay_shots_generator_tool(
        session_id="sess",
        project_id=project_id,
        generate_end_shots=True,
        language="ru",
        generate_blockout=False,
    )

    assert captured_end_start["english_prompt"] == "CURRENT START"
    assert captured_end_start["initial_state_summary"] == "CURRENT STATE"
    assert captured_end_start["camera_position"] == "side"
    assert captured_end_start["shot_size"] == "Medium shot"
    assert captured_end_start["lighting_style"] == "soft window light"
    assert all("_start_llm_result" not in item for item in result["items"])


def test_artistic_start_and_end_receive_full_style_context_and_common_core(monkeypatch):
    calls = []

    def fake_call_openai_api(**kwargs):
        calls.append(kwargs)
        return json.dumps(
            {
                "english_prompt": "Создай кадр в применимой акварельной манере",
                "negative_prompt": "negative",
                "add_end_shot": "false",
                "should_link_as_next_start": "false",
                "should_use_prev_end_as_reference": "false",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(shots_generator, "call_openai_api", fake_call_openai_api)
    style_marker = "STYLE_MATERIAL_PALETTE_ANCHOR"
    style_images = {"material_signature": "paper grain and ink bleed"}
    context = {
        "shot_description": "Герой стоит у окна",
        "scene_action": "Герой ждёт",
        "camera_plan": "Medium shot",
        "shot_frame_spec": {
            "primary_subject": "Герой",
            "must_show": ["Герой у окна"],
        },
        "visual_style": style_marker,
        "style_images": style_images,
        "style_do_not_include": ["GLOBAL_STORY_RULE"],
        "location_context": {},
    }
    technical_params = {
        "shot_size": "Medium shot",
        "main_subject": "Герой",
        "camera_position": "side",
        "character_orientation": "profile",
        "spatial_composition": "у окна",
        "initial_state_summary": "Герой стоит",
        "final_shot_size": "Medium shot",
        "final_camera_position": "side",
        "final_character_orientation": "profile",
        "final_spatial_composition": "у окна",
        "final_lighting_style": "мягкий свет",
        "characters": ["Герой"],
        "location_context": {},
    }

    shots_generator._generate_shot_artistic(technical_params, context, language="ru")
    shots_generator._generate_end_shot_artistic(
        technical_params,
        {"english_prompt": "START", "camera_position": "side"},
        "",
        context,
        final_shot_size_label="Medium shot",
        language="ru",
    )

    assert len(calls) >= 2
    for call in calls[:2]:
        combined = f"{call['prompt']}\n{call['system_prompt']}"
        lower = combined.lower()
        assert style_marker in combined
        assert "style_images" in combined
        assert "style_do_not_include" in combined
        assert "не генерируй" in lower or "do not generate" in lower
        assert "не выбирай" in lower or "do not choose" in lower

