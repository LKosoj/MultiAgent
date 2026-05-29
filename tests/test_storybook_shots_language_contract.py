import json
from pathlib import Path
import sys
import types
import pytest

agent_command_stub = types.ModuleType("agent_command")
agent_command_stub.model_hard = "test-model-hard"
agent_command_stub.model_code = "test-model-code"
agent_command_stub.model_ultimate = "test-model-ultimate"
agent_command_stub.model_lite = "test-model-lite"
sys.modules.setdefault("agent_command", agent_command_stub)

utils_stub = types.ModuleType("utils")
utils_stub.call_openai_api = lambda *args, **kwargs: '{"is_black_screen": false}'
utils_stub.extract_json_from_markdown = lambda text: text
utils_stub.parse_llm_json = lambda text: json.loads(text)
utils_stub.translate_prompts_in_items = lambda *args, **kwargs: args[0]
sys.modules.setdefault("utils", utils_stub)

import utils

from custom_tools.storybook import screenplay_shots_generator as shots_generator
from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils
from custom_tools.storybook.screenplay_shots_generator_utils.technical import _create_shot_item

shared_utils.call_openai_api = lambda *args, **kwargs: '{"is_black_screen": false}'


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_create_shot_item_uses_generation_language_and_drops_canonical_fields():
    item = _create_shot_item(
        project_id="proj",
        scene_number=1,
        shot_number=1,
        shot_type="start",
        page_number=1,
        item_number=1,
        camera_plan="Close-up",
        timing="1s",
        llm_result={
            "english_prompt": "крупный план героя у окна",
            "negative_prompt": "низкое качество",
            "characters": ["Герой"],
            "camera_position": "спереди",
            "character_orientation": "three_quarter",
            "spatial_composition": "герой в центре кадра",
            "point_of_view": "objective",
            "initial_state_summary": "герой замер у окна",
            "reference_image_paths": [],
            "reference_roles_instruction": "",
            "video_prompt": "",
        },
        characters_data=[{"name": "Герой"}],
        locations_data=[],
        scene_action="Герой смотрит в окно",
        shot_description="Герой смотрит в окно",
        shot_frame_spec={"primary_subject": "Герой", "must_show": ["Герой у окна"]},
        language="ru",
    )

    assert item["english_prompt"].startswith("Создай ")
    assert "_canonical_english_prompt_en" not in item
    assert "_canonical_negative_prompt_en" not in item
    assert "_canonical_video_prompt_en" not in item


def test_create_shot_item_orders_character_ref_before_location_for_single_subject_start():
    item = _create_shot_item(
        project_id="proj",
        scene_number=5,
        shot_number=2,
        shot_type="start",
        page_number=1,
        item_number=1,
        camera_plan="Close-up",
        timing="1s",
        llm_result={
            "english_prompt": "крупный план героя",
            "negative_prompt": "низкое качество",
            "characters": ["Герой"],
            "location": "Зал",
            "camera_position": "спереди",
            "character_orientation": "three_quarter",
            "spatial_composition": "герой в центре кадра",
            "point_of_view": "objective",
            "initial_state_summary": "герой в кадре",
            "reference_image_paths": [],
            "reference_roles_instruction": "",
            "video_prompt": "",
        },
        characters_data=[
            {
                "name": "Герой",
                "reference_image_path": "/references/characters/hero.png",
            }
        ],
        locations_data=[
            {
                "name": "Зал",
                "reference_image_path": "/references/locations/hall.png",
            }
        ],
        location_time="",
        location_canon_name="Зал",
        scene_action="Герой в зале",
        shot_description="Герой в зале",
        shot_frame_spec={
            "scene_mode": "single_subject",
            "primary_subject": "Герой",
            "camera_anchor": "лицо в центре",
            "must_show": ["Герой"],
            "must_not_show": [],
        },
        language="ru",
    )

    refs = item.get("reference_image_paths") or []
    assert len(refs) >= 2
    assert refs[0].replace("\\", "/").endswith("/references/characters/hero.png")
    assert refs[1].replace("\\", "/").endswith("/references/locations/hall.png")
    assert "image 1 as character" in (item.get("reference_roles_instruction") or "")


def test_create_shot_item_black_screen_start_has_no_refs_and_merges_style_negative(monkeypatch):
    import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su
    with su._BLACK_SCREEN_DETECTION_LOCK:
        su._BLACK_SCREEN_DETECTION_CACHE.clear()
    monkeypatch.setattr(
        su, "call_openai_api",
        lambda *a, **kw: json.dumps({"is_black_screen": True}),
    )

    item = _create_shot_item(
        project_id="proj",
        scene_number=4,
        shot_number=16,
        shot_type="start",
        page_number=1,
        item_number=1,
        camera_plan="BLACK SCREEN",
        timing="1s",
        llm_result={
            "english_prompt": "Extreme close-up of hero in tomb",
            "negative_prompt": "blurry",
            "characters": ["Герой"],
            "camera_position": "спереди",
            "character_orientation": "three_quarter",
            "spatial_composition": "центр",
            "point_of_view": "objective",
            "initial_state_summary": "герой",
            "reference_image_paths": ["/references/locations/tomb.png"],
            "reference_roles_instruction": "",
            "video_prompt": "",
        },
        characters_data=[
            {
                "name": "Герой",
                "reference_image_path": "/references/characters/hero.png",
            }
        ],
        locations_data=[
            {
                "name": "Гробница",
                "reference_image_path": "/references/locations/tomb.png",
            }
        ],
        location_time="",
        location_canon_name="Гробница",
        scene_action="",
        shot_description="",
        shot_frame_spec={
            "scene_mode": "object_focus",
            "primary_subject": "звук",
            "must_show": ["тьма"],
            "must_not_show": [],
        },
        language="ru",
        visual_style="Европейский графический роман.",
        style_do_not_include="фотореализм",
    )
    assert item.get("reference_image_paths") == []
    assert item.get("characters") == []
    assert item.get("locations") == []
    neg = item.get("negative_prompt") or ""
    assert "фотореализм" in neg
    assert "свет" in neg.lower() or "light" in neg.lower()
    ep = item.get("english_prompt") or ""
    assert "#000000" in ep
    assert "Black screen" in ep or "чёрн" in ep.lower()


def test_create_shot_item_keeps_location_before_character_for_environment_start():
    item = _create_shot_item(
        project_id="proj",
        scene_number=1,
        shot_number=1,
        shot_type="start",
        page_number=1,
        item_number=1,
        camera_plan="Wide shot",
        timing="1s",
        llm_result={
            "english_prompt": "общий план зала",
            "negative_prompt": "низкое качество",
            "characters": ["Герой"],
            "location": "Зал",
            "camera_position": "спереди",
            "character_orientation": "three_quarter",
            "spatial_composition": "зал целиком",
            "point_of_view": "objective",
            "initial_state_summary": "пустой зал",
            "reference_image_paths": [],
            "reference_roles_instruction": "",
            "video_prompt": "",
        },
        characters_data=[
            {
                "name": "Герой",
                "reference_image_path": "/references/characters/hero.png",
            }
        ],
        locations_data=[
            {
                "name": "Зал",
                "reference_image_path": "/references/locations/hall.png",
            }
        ],
        location_time="",
        location_canon_name="Зал",
        scene_action="Зал",
        shot_description="Establishing shot зала",
        shot_frame_spec={
            "scene_mode": "environment",
            "primary_subject": "Зал",
            "camera_anchor": "центр зала",
            "must_show": ["зал"],
            "must_not_show": [],
        },
        language="ru",
    )

    refs = item.get("reference_image_paths") or []
    assert len(refs) >= 2
    assert refs[0].replace("\\", "/").endswith("/references/locations/hall.png")
    assert refs[1].replace("\\", "/").endswith("/references/characters/hero.png")
    assert "image 1 as location" in (item.get("reference_roles_instruction") or "")


def test_allow_end_location_change_via_llm_defaults_to_false_without_explicit_allow(monkeypatch):
    monkeypatch.setattr(shots_generator, "call_openai_api", lambda **kwargs: "{}", raising=True)
    allowed = shots_generator._allow_end_location_change_via_llm(
        start_location_name="Служебный коридор гробницы",
        candidate_end_location_name="Вход в гробницу",
        shot_description="Камера слегка приближается к персонажу",
        scene_action="Герой оборачивается",
        shot_frame_spec={"transition_spec": {"environment_delta": ["параллакс стен"]}},
    )
    assert allowed is False


def test_allow_end_location_change_via_llm_honors_explicit_allow_from_model(monkeypatch):
    monkeypatch.setattr(
        shots_generator,
        "call_openai_api",
        lambda **kwargs: json.dumps({"allow_location_change": True, "reason": "explicit move"}),
        raising=True,
    )
    allowed = shots_generator._allow_end_location_change_via_llm(
        start_location_name="Служебный коридор гробницы",
        candidate_end_location_name="Вход в гробницу",
        shot_description="Персонаж выбегает из коридора ко входу",
        scene_action="Явный переход в другую локацию",
        shot_frame_spec={"transition_spec": {"environment_delta": ["выход к входной зоне"]}},
    )
    assert allowed is True


def test_create_shot_item_uses_parent_location_reference_when_child_missing_ref():
    item = _create_shot_item(
        project_id="proj",
        scene_number=7,
        shot_number=1,
        shot_type="start",
        page_number=1,
        item_number=1,
        camera_plan="Medium shot",
        timing="1s",
        llm_result={
            "english_prompt": "герой в дочерней зоне локации",
            "negative_prompt": "низкое качество",
            "characters": ["Герой"],
            "location": "Служебная зона",
            "camera_position": "спереди",
            "character_orientation": "three_quarter",
            "spatial_composition": "герой в центре",
            "point_of_view": "objective",
            "initial_state_summary": "герой стоит",
            "reference_image_paths": [],
            "reference_roles_instruction": "",
            "video_prompt": "",
        },
        characters_data=[
            {
                "name": "Герой",
                "reference_image_path": "/references/characters/hero.png",
            }
        ],
        locations_data=[
            {
                "name": "Базовая зона",
                "reference_image_path": "/references/locations/base_zone.png",
            },
            {
                "name": "Служебная зона",
                "reference_image_path": "",
                "parent_location_name": "Базовая зона",
            },
        ],
        location_time="",
        location_canon_name="Служебная зона",
        scene_action="Герой в служебной зоне",
        shot_description="Герой в служебной зоне",
        shot_frame_spec={
            "scene_mode": "single_subject",
            "primary_subject": "Герой",
            "camera_anchor": "центр",
            "must_show": ["герой"],
            "must_not_show": [],
        },
        language="ru",
    )

    refs = item.get("reference_image_paths") or []
    assert any(str(r).endswith("/references/locations/base_zone.png") for r in refs)
    assert item.get("locations")[0].get("name") == "Служебная зона"
    assert "as location — Служебная зона" in (item.get("reference_roles_instruction") or "")


def test_screenplay_shots_generator_keeps_generation_language_and_skips_post_translate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_lang"
    captured_languages = []

    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/91_screenplay/screenplay.json",
        {
            "screenplay": [
                {
                    "scene_number": 1,
                    "action": "Герой смотрит в окно",
                    "characters": ["Герой"],
                    "storyboard": [
                        {
                            "shot_number": 1,
                            "description": "Герой смотрит в окно",
                            "camera_plan": "Close-up",
                            "timing": "1s",
                        }
                    ],
                }
            ]
        },
    )
    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/20_bible/characters.json",
        [{"name": "Герой"}],
    )
    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/20_bible/locations.json",
        [],
    )

    def fake_build_extended_context(**kwargs):
        return {
            "shot_frame_spec": {
                "primary_subject": "Герой",
                "must_show": ["Герой у окна"],
            },
            "full_shot_frame_spec": {
                "primary_subject": "Герой",
                "must_show": ["Герой у окна"],
            },
            "shot_frame_spec_cache_key": "scene_1_shot_1",
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
        captured_languages.append(language)
        assert shot_type == "start"
        return {
            "english_prompt": "крупный план героя у окна",
            "negative_prompt": "низкое качество",
            "characters": ["Герой"],
            "main_subject": "Герой",
            "camera_position": "спереди",
            "character_orientation": "three_quarter",
            "spatial_composition": "герой в центре кадра",
            "point_of_view": "objective",
            "initial_state_summary": "герой замер у окна",
            "reference_image_paths": [],
            "reference_roles_instruction": "",
            "video_prompt": "",
            "add_end_shot": "false",
        }

    def fail_translate(*args, **kwargs):
        raise AssertionError("translate_prompts_in_items must not be called by screenplay_shots_generator_tool")

    monkeypatch.setattr(shots_generator, "_build_extended_context", fake_build_extended_context)
    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", fake_generate_shot_prompt)
    monkeypatch.setattr(shots_generator, "_generate_fcpxml", lambda *args, **kwargs: None)
    monkeypatch.setattr(shots_generator, "_generate_photo_fcpxml", lambda *args, **kwargs: None)
    monkeypatch.setattr(utils, "translate_prompts_in_items", fail_translate, raising=True)

    result = shots_generator.screenplay_shots_generator_tool(
        session_id="sess",
        project_id=project_id,
        generate_end_shots=False,
        language="ru",
    )

    assert captured_languages == ["ru"]
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["english_prompt"].startswith("Создай ")
    assert "героя" in item["english_prompt"].lower()
    assert not any(key.startswith("_canonical_") for key in item)

    saved = json.loads(
        (tmp_path / f"plots/storybooks/{project_id}/97_shots/shots.json").read_text(encoding="utf-8")
    )
    assert saved["items"][0]["english_prompt"] == item["english_prompt"]


def test_screenplay_shots_generator_writes_checkpoint_before_late_scene_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_checkpoint"

    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/91_screenplay/screenplay.json",
        {
            "screenplay": [
                {
                    "scene_number": 1,
                    "action": "Первая сцена",
                    "characters": ["Герой"],
                    "storyboard": [
                        {
                            "shot_number": 1,
                            "description": "Герой смотрит в окно",
                            "camera_plan": "Close-up",
                            "timing": "1s",
                        }
                    ],
                },
                {
                    "scene_number": 2,
                    "action": "Вторая сцена",
                    "characters": ["Герой"],
                    "storyboard": [
                        {
                            "shot_number": 1,
                            "description": "Герой поворачивается",
                            "camera_plan": "Medium shot",
                            "timing": "1s",
                        }
                    ],
                },
            ]
        },
    )
    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/20_bible/characters.json",
        [{"name": "Герой"}],
    )
    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/20_bible/locations.json",
        [],
    )

    class FakeFuture:
        def __init__(self, fn, *args, **kwargs):
            self._exc = None
            self._result = None
            try:
                self._result = fn(*args, **kwargs)
            except Exception as exc:
                self._exc = exc

        def result(self):
            if self._exc is not None:
                raise self._exc
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

    def fake_as_completed(futures):
        return list(futures)

    def fake_build_extended_context(**kwargs):
        scene_number = kwargs["scene"]["scene_number"]
        return {
            "scene_number": scene_number,
            "shot_frame_spec": {
                "primary_subject": "Герой",
                "must_show": ["Герой у окна"],
            },
            "full_shot_frame_spec": {
                "primary_subject": "Герой",
                "must_show": ["Герой у окна"],
            },
            "shot_frame_spec_cache_key": f"scene_{scene_number}_shot_1",
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
        return {
            "english_prompt": "Создай крупный план героя у окна",
            "negative_prompt": "низкое качество",
            "characters": ["Герой"],
            "main_subject": "Герой",
            "camera_position": "спереди",
            "character_orientation": "three_quarter",
            "spatial_composition": "герой в центре кадра",
            "point_of_view": "objective",
            "initial_state_summary": "герой замер у окна",
            "reference_image_paths": [],
            "reference_roles_instruction": "",
            "video_prompt": "",
            "add_end_shot": "true",
        }

    def fake_generate_transition_video_prompt(
        *,
        start_llm_result,
        end_llm_result,
        extended_context,
    ):
        if extended_context["scene_number"] == 2:
            return None
        return "Camera locks off; subject moves; environment shimmers; steadily"

    monkeypatch.setattr(shots_generator, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(shots_generator, "as_completed", fake_as_completed)
    monkeypatch.setattr(shots_generator, "_build_extended_context", fake_build_extended_context)
    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", fake_generate_shot_prompt)
    monkeypatch.setattr(
        shots_generator,
        "_generate_transition_video_prompt",
        fake_generate_transition_video_prompt,
    )
    monkeypatch.setattr(shots_generator, "_generate_fcpxml", lambda *args, **kwargs: None)
    monkeypatch.setattr(shots_generator, "_generate_photo_fcpxml", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="Ошибка обработки сцены 2"):
        shots_generator.screenplay_shots_generator_tool(
            session_id="sess",
            project_id=project_id,
            generate_end_shots=True,
            language="ru",
        )

    saved = json.loads(
        (tmp_path / f"plots/storybooks/{project_id}/97_shots/shots.json").read_text(encoding="utf-8")
    )
    assert len(saved["items"]) == 2
    assert {item["scene_number"] for item in saved["items"]} == {1}
    assert saved["generated_session_id"] == "sess"


def test_screenplay_shots_generator_writes_checkpoint_before_late_shot_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_shot_checkpoint"

    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/91_screenplay/screenplay.json",
        {
            "screenplay": [
                {
                    "scene_number": 1,
                    "action": "Одна сцена с поздним падением второго шота",
                    "characters": ["Герой"],
                    "storyboard": [
                        {
                            "shot_number": 1,
                            "description": "Герой смотрит в окно",
                            "camera_plan": "Close-up",
                            "timing": "1s",
                        },
                        {
                            "shot_number": 2,
                            "description": "Герой оборачивается",
                            "camera_plan": "Medium shot",
                            "timing": "1s",
                        },
                    ],
                }
            ]
        },
    )
    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/20_bible/characters.json",
        [{"name": "Герой"}],
    )
    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/20_bible/locations.json",
        [],
    )

    class FakeFuture:
        def __init__(self, fn, *args, **kwargs):
            self._exc = None
            self._result = None
            try:
                self._result = fn(*args, **kwargs)
            except Exception as exc:
                self._exc = exc

        def result(self):
            if self._exc is not None:
                raise self._exc
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

    def fake_as_completed(futures):
        return list(futures)

    def fake_build_extended_context(**kwargs):
        shot_number = kwargs["shot_number"]
        return {
            "shot_number": shot_number,
            "shot_frame_spec": {
                "primary_subject": "Герой",
                "must_show": ["Герой у окна"],
            },
            "full_shot_frame_spec": {
                "primary_subject": "Герой",
                "must_show": ["Герой у окна"],
            },
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
        if shot_number == 2 and shot_type == "end":
            raise RuntimeError("late end failure")
        return {
            "english_prompt": f"prompt shot {shot_number} {shot_type}",
            "negative_prompt": "negative",
            "characters": ["Герой"],
            "main_subject": "Герой",
            "camera_position": "front",
            "character_orientation": "front",
            "spatial_composition": f"shot {shot_number}",
            "point_of_view": "objective",
            "initial_state_summary": f"state {shot_number}",
            "reference_image_paths": [],
            "reference_roles_instruction": "",
            "video_prompt": "",
            "add_end_shot": "true",
            "should_link_as_next_start": "false",
            "should_use_prev_end_as_reference": "false",
        }

    monkeypatch.setattr(shots_generator, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(shots_generator, "as_completed", fake_as_completed)
    monkeypatch.setattr(shots_generator, "_build_extended_context", fake_build_extended_context)
    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", fake_generate_shot_prompt)
    monkeypatch.setattr(shots_generator, "_generate_transition_video_prompt", lambda **kwargs: "video prompt")
    monkeypatch.setattr(shots_generator, "_generate_fcpxml", lambda *args, **kwargs: None)
    monkeypatch.setattr(shots_generator, "_generate_photo_fcpxml", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="Ошибка обработки сцены 1"):
        shots_generator.screenplay_shots_generator_tool(
            session_id="sess",
            project_id=project_id,
            generate_end_shots=True,
            language="ru",
        )

    saved = json.loads(
        (tmp_path / f"plots/storybooks/{project_id}/97_shots/shots.json").read_text(encoding="utf-8")
    )
    assert [(item["scene_number"], item["shot_number"], item["shot_type"]) for item in saved["items"]] == [
        (1, 1, "start"),
        (1, 1, "end"),
    ]


def test_screenplay_shots_generator_writes_checkpoint_before_transition_video_prompt_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_transition_checkpoint"

    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/91_screenplay/screenplay.json",
        {
            "screenplay": [
                {
                    "scene_number": 1,
                    "action": "Одна сцена с падением на final transition video prompt",
                    "characters": ["Герой"],
                    "storyboard": [
                        {
                            "shot_number": 1,
                            "description": "Герой смотрит в окно",
                            "camera_plan": "Close-up",
                            "timing": "1s",
                        }
                    ],
                }
            ]
        },
    )
    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/20_bible/characters.json",
        [{"name": "Герой"}],
    )
    _write_json(
        tmp_path / f"plots/storybooks/{project_id}/20_bible/locations.json",
        [],
    )

    class FakeFuture:
        def __init__(self, fn, *args, **kwargs):
            self._exc = None
            self._result = None
            try:
                self._result = fn(*args, **kwargs)
            except Exception as exc:
                self._exc = exc

        def result(self):
            if self._exc is not None:
                raise self._exc
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

    def fake_as_completed(futures):
        return list(futures)

    def fake_build_extended_context(**kwargs):
        return {
            "shot_number": kwargs["shot_number"],
            "shot_frame_spec": {
                "primary_subject": "Герой",
                "must_show": ["Герой у окна"],
            },
            "full_shot_frame_spec": {
                "primary_subject": "Герой",
                "must_show": ["Герой у окна"],
            },
            "shot_frame_spec_cache_key": "scene_1_shot_1",
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
        return {
            "english_prompt": f"prompt shot 1 {shot_type}",
            "negative_prompt": "negative",
            "characters": ["Герой"],
            "main_subject": "Герой",
            "camera_position": "front",
            "character_orientation": "front",
            "spatial_composition": f"shot 1 {shot_type}",
            "point_of_view": "objective",
            "initial_state_summary": "state 1",
            "reference_image_paths": [],
            "reference_roles_instruction": "",
            "video_prompt": "",
            "add_end_shot": "true",
            "should_link_as_next_start": "false",
            "should_use_prev_end_as_reference": "false",
        }

    monkeypatch.setattr(shots_generator, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(shots_generator, "as_completed", fake_as_completed)
    monkeypatch.setattr(shots_generator, "_build_extended_context", fake_build_extended_context)
    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", fake_generate_shot_prompt)
    monkeypatch.setattr(shots_generator, "_generate_transition_video_prompt", lambda **kwargs: None)
    monkeypatch.setattr(shots_generator, "_generate_fcpxml", lambda *args, **kwargs: None)
    monkeypatch.setattr(shots_generator, "_generate_photo_fcpxml", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="Ошибка обработки сцены 1"):
        shots_generator.screenplay_shots_generator_tool(
            session_id="sess",
            project_id=project_id,
            generate_end_shots=True,
            language="ru",
        )

    saved = json.loads(
        (tmp_path / f"plots/storybooks/{project_id}/97_shots/shots.json").read_text(encoding="utf-8")
    )
    assert [(item["scene_number"], item["shot_number"], item["shot_type"]) for item in saved["items"]] == [
        (1, 1, "start"),
        (1, 1, "end"),
    ]
    assert saved["items"][0]["video_prompt"] == ""


def test_enrich_environment_delta_via_llm_fills_when_model_says_yes(monkeypatch):
    import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su

    def fake_call_openai_api(*args, **kwargs):
        return json.dumps(
            {
                "needs_environment_delta": True,
                "environment_delta": ["видимое окружение меняется относительно START при том же зале"],
            }
        )

    monkeypatch.setattr(su, "call_openai_api", fake_call_openai_api)

    spec = {
        "transition_spec": {
            "camera_delta": "tracking",
            "environment_delta": [],
        },
    }
    out = su.enrich_shot_frame_spec_environment_delta_via_llm(spec, video_prompt="Camera tracks forward")
    ed = (out.get("transition_spec") or {}).get("environment_delta") or []
    assert len(ed) == 1
    assert "START" in ed[0]


def test_enrich_environment_delta_via_llm_skips_when_already_present(monkeypatch):
    import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su

    calls = []

    def fake_call_openai_api(*args, **kwargs):
        calls.append(1)
        return json.dumps({"needs_environment_delta": False, "environment_delta": []})

    monkeypatch.setattr(su, "call_openai_api", fake_call_openai_api)

    spec = {
        "transition_spec": {
            "environment_delta": ["уже задано извлечением"],
        }
    }
    out = su.enrich_shot_frame_spec_environment_delta_via_llm(spec, video_prompt="any")
    assert not calls
    assert (out.get("transition_spec") or {}).get("environment_delta") == ["уже задано извлечением"]


def test_enrich_environment_delta_via_llm_respects_model_no(monkeypatch):
    import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su

    def fake_call_openai_api(*args, **kwargs):
        return json.dumps({"needs_environment_delta": False, "environment_delta": []})

    monkeypatch.setattr(su, "call_openai_api", fake_call_openai_api)

    out = su.enrich_shot_frame_spec_environment_delta_via_llm(
        {"transition_spec": {"environment_delta": []}}, video_prompt="static"
    )
    assert not (out.get("transition_spec") or {}).get("environment_delta")


def _reset_black_screen_cache():
    import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su
    with su._BLACK_SCREEN_DETECTION_LOCK:
        su._BLACK_SCREEN_DETECTION_CACHE.clear()


def test_black_screen_storyboard_shot_empty_inputs_skip_llm(monkeypatch):
    import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su
    _reset_black_screen_cache()

    calls = []

    def fake_call_openai_api(*args, **kwargs):
        calls.append(1)
        return json.dumps({"is_black_screen": True})

    monkeypatch.setattr(su, "call_openai_api", fake_call_openai_api)

    assert su.black_screen_storyboard_shot("", None) is False
    assert su.black_screen_storyboard_shot("   ", {"camera_anchor": "", "must_show": []}) is False
    assert calls == []


def test_black_screen_storyboard_shot_llm_true(monkeypatch):
    import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su
    _reset_black_screen_cache()

    monkeypatch.setattr(
        su, "call_openai_api",
        lambda *a, **kw: json.dumps({"is_black_screen": True}),
    )

    assert su.black_screen_storyboard_shot(
        "BLACK SCREEN — total darkness",
        {"camera_anchor": "чёрный экран", "must_show": ["абсолютная тьма"]},
    ) is True


def test_black_screen_storyboard_shot_llm_false(monkeypatch):
    import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su
    _reset_black_screen_cache()

    monkeypatch.setattr(
        su, "call_openai_api",
        lambda *a, **kw: json.dumps({"is_black_screen": False}),
    )

    assert su.black_screen_storyboard_shot(
        "ночная сцена с факелами",
        {"camera_anchor": "тёмный коридор", "must_show": ["герой с факелом", "стены гробницы"]},
    ) is False


def test_black_screen_storyboard_shot_caches_per_input(monkeypatch):
    import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su
    _reset_black_screen_cache()

    calls = []

    def fake_call_openai_api(*args, **kwargs):
        calls.append(1)
        return json.dumps({"is_black_screen": True})

    monkeypatch.setattr(su, "call_openai_api", fake_call_openai_api)

    spec = {"camera_anchor": "чёрный экран", "must_show": ["абсолютная тьма"]}
    assert su.black_screen_storyboard_shot("BLACK SCREEN", spec) is True
    assert su.black_screen_storyboard_shot("BLACK SCREEN", spec) is True
    assert su.black_screen_storyboard_shot("BLACK SCREEN", dict(spec)) is True
    assert len(calls) == 1


def test_black_screen_storyboard_shot_llm_failure_propagates(monkeypatch):
    import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su
    _reset_black_screen_cache()

    def boom(*a, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(su, "call_openai_api", boom)

    import pytest
    with pytest.raises(RuntimeError, match="llm down"):
        su.black_screen_storyboard_shot(
            "BLACK SCREEN", {"camera_anchor": "чёрный экран", "must_show": ["абсолютная тьма"]}
        )
