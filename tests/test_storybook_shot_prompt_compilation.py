import json

import pytest

from custom_tools.storybook import screenplay_shots_generator as shots_generator
from custom_tools.storybook.screenplay_shots_generator_utils import technical
from custom_tools.storybook.screenplay_shots_generator_utils.technical import (
    _create_shot_item,
)


def _patch_local_helpers(monkeypatch, location_reply="Workshop"):
    monkeypatch.setattr(
        technical,
        "_infer_accessory_visibility_via_llm",
        lambda **kwargs: {
            "must_show_additions": [],
            "must_not_show_additions": [],
            "negative_prompt_tokens": [],
        },
    )
    monkeypatch.setattr(technical, "black_screen_storyboard_shot", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        technical,
        "call_openai_api",
        lambda *args, **kwargs: json.dumps(
            {"canonical_location_name": location_reply}, ensure_ascii=False
        ),
    )


def _item_kwargs(llm_result, **overrides):
    values = {
        "project_id": "neutral_project",
        "scene_number": 3,
        "shot_number": 2,
        "shot_type": "end",
        "page_number": 1,
        "item_number": 1,
        "camera_plan": "Medium shot",
        "timing": "2s",
        "llm_result": llm_result,
        "characters_data": [],
        "locations_data": [],
        "scene_action": "Нейтральное действие",
        "shot_description": "Нейтральное описание кадра",
        "shot_frame_spec": {"primary_subject": "человек", "must_show": ["человек"]},
        "language": "ru",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "shot_type,raw_characters,shot_frame_spec",
    [
        ("start", [], {"visible_characters": []}),
        ("start", ["Witness"], {"visible_characters": []}),
        ("end", ["Witness"], {"visible_characters": ["Witness"], "end_state_spec": {"visible_characters": []}}),
    ],
)
def test_shot_frame_visible_characters_block_llm_and_text_fallback_refs(
    monkeypatch, shot_type, raw_characters, shot_frame_spec
):
    _patch_local_helpers(monkeypatch)
    witness = {"name": "Witness", "reference_image_path": "/references/characters/witness.png"}
    location = {"name": "Workshop", "reference_image_path": "/references/locations/workshop.png"}
    continuity = "plots/storybooks/neutral_project/97_shots/scene_03_shot_02/img_final_start_03_02.png"
    item = _create_shot_item(
        **_item_kwargs(
            {
                "english_prompt": "Insert of the hatch after Witness leaves",
                "negative_prompt": "blur",
                "characters": raw_characters,
                "location": "Workshop",
                "reference_image_paths": [continuity, location["reference_image_path"], witness["reference_image_path"]],
            },
            shot_type=shot_type,
            characters_data=[witness],
            locations_data=[location],
            location_canon_name="Workshop",
            shot_description="Insert of the hatch after Witness leaves",
            shot_frame_spec=shot_frame_spec,
            scene_continuity_facts={"hatch": "fixed"},
        )
    )

    assert item["characters"] == []
    assert witness["reference_image_path"] not in item["reference_image_paths"]
    assert location["reference_image_path"] in item["reference_image_paths"]
    assert item["_scene_continuity_facts"] == {"hatch": "fixed"}
    assert continuity in item["reference_image_paths"]


def test_end_prompt_keeps_single_line_content_and_useful_technical_details(monkeypatch):
    _patch_local_helpers(monkeypatch)
    prompt = (
        "Редактируй image 1: человек бежит во дворе, опорная стопа касается земли; "
        "50 мм f/2.0, свет 6500 K; единый кадр без панелей."
    )
    item = _create_shot_item(
        **_item_kwargs(
            {
                "english_prompt": prompt,
                "negative_prompt": "LLM NEGATIVE",
                "characters": [],
            }
        )
    )

    assert prompt in item["english_prompt"]
    assert "50 мм f/2.0" in item["english_prompt"]
    assert "6500 K" in item["english_prompt"]


@pytest.mark.parametrize("phase_context", [True, False])
def test_end_artistic_receives_final_phase_and_scene_facts(monkeypatch, phase_context):
    end_state = {
        "primary_subject": "hatch",
        "pose_signature": "lever lowered against its stop",
        "world_physics": {"contact_constraints": ["bar touches the stop"]},
    }
    full_spec = {
        "primary_subject": "hatch",
        "start_state_spec": {"pose_signature": "lever raised"},
        "end_state_spec": end_state,
    }
    facts = {"boundary_parts": ["moving leaf", "fixed glazed section"]}
    captured = []
    monkeypatch.setattr(shots_generator, "enrich_shot_frame_spec_environment_delta_via_llm", lambda spec, **kwargs: spec)
    monkeypatch.setattr(shots_generator, "black_screen_storyboard_shot", lambda *args, **kwargs: False)

    def fake_call(**kwargs):
        captured.append(kwargs)
        return json.dumps({"english_prompt": "Final hatch", "negative_prompt": "blur"})

    monkeypatch.setattr(shots_generator, "call_openai_api", fake_call)
    shots_generator._generate_end_shot_artistic(
        {}, {"english_prompt": "Initial hatch"}, "",
        {"shot_frame_spec": end_state if phase_context else full_spec,
         "full_shot_frame_spec": full_spec, "scene_continuity_facts": facts},
    )

    prompt = captured[0]["prompt"]
    spec_text = prompt.split("SHOT FRAME SPEC (", 1)[1].split("\n", 1)[1]
    payload, _ = json.JSONDecoder().raw_decode(spec_text)
    assert payload["end_state_spec"] == end_state
    assert json.dumps(facts, ensure_ascii=False, indent=2) in prompt


def test_visual_style_does_not_append_global_story_context(monkeypatch):
    _patch_local_helpers(monkeypatch)
    item = _create_shot_item(
        **_item_kwargs(
            {
                "english_prompt": "Собственный акварельный стиль, 50mm f/2.0, свет 6500 K",
                "negative_prompt": "LLM NEGATIVE",
                "characters": [],
            },
            visual_style="GLOBAL_OTHER_SCENE_PALETTE_AND_FUTURE_EVENTS_MODEL_ID",
            style_do_not_include=["GLOBAL_STORY_MORAL_AND_DIALOGUE_RESTRICTION"],
        )
    )

    assert "GLOBAL_OTHER_SCENE_PALETTE_AND_FUTURE_EVENTS_MODEL_ID" not in item["english_prompt"]
    assert "Собственный акварельный стиль" in item["english_prompt"]
    assert "50mm f/2.0" in item["english_prompt"]
    assert "6500 K" in item["english_prompt"]
    assert "LLM NEGATIVE" in item["negative_prompt"]
    assert "GLOBAL_STORY_MORAL_AND_DIALOGUE_RESTRICTION" not in item["negative_prompt"]


@pytest.mark.parametrize(("language", "frozen_anchor"), [
    ("ru", "без переноса фоновых якорей"),
    ("en", "do not move background anchors"),
    ("es", "sin mover anclas del fondo"),
    ("fr", "sans deplacer les ancrages de fond"),
    ("de", "keine verschiebung der hintergrundanker"),
])
def test_tracking_end_does_not_forbid_screen_space_background_motion(monkeypatch, language, frozen_anchor):
    _patch_local_helpers(monkeypatch)
    prompt = "Edit image 1: cyclist stays centered; street facades have shifted left as the camera follows."
    item = _create_shot_item(**_item_kwargs({
        "english_prompt": prompt, "negative_prompt": "blur", "characters": [],
        "reference_image_paths": ["/97_shots/scene_03_shot_02/img_final_start_03_02.png"],
    }, language=language, camera_plan="SIDE TRACKING SHOT",
        shot_description="The camera follows a cyclist along a street."))
    assert prompt in item["english_prompt"]
    assert frozen_anchor not in item["negative_prompt"]


def test_generated_continuity_frame_keeps_location_reference_as_support(monkeypatch):
    _patch_local_helpers(monkeypatch, location_reply="Street")
    location = {"name": "Street", "reference_image_path": "/references/locations/street.png"}
    item = _create_shot_item(**_item_kwargs({
        "english_prompt": "Edit image 1: cyclist centered, street facades shifted left.",
        "negative_prompt": "blur", "characters": [], "location": "Street",
        "reference_image_paths": [
            "/97_shots/scene_03_shot_02/img_final_start_03_02.png",
            location["reference_image_path"],
        ],
    }, locations_data=[location], location_canon_name="Street"))
    assert "as location support" in item["reference_roles_instruction"]
    assert "anchor screen positions projected by the current camera" in item["reference_roles_instruction"]


def test_non_black_shot_requires_meaningful_image_prompt(monkeypatch):
    _patch_local_helpers(monkeypatch)
    for prompt in (None, "", "   "):
        try:
            _create_shot_item(
                **_item_kwargs(
                    {"english_prompt": prompt, "negative_prompt": "negative", "characters": []}
                )
            )
        except ValueError:
            continue
        raise AssertionError(f"prompt {prompt!r} was accepted")


def test_black_screen_allows_empty_image_prompt(monkeypatch):
    _patch_local_helpers(monkeypatch)
    monkeypatch.setattr(technical, "black_screen_storyboard_shot", lambda *args, **kwargs: True)
    item = _create_shot_item(
        **_item_kwargs(
            {"english_prompt": "", "negative_prompt": "negative", "characters": []},
            camera_plan="BLACK SCREEN",
        )
    )
    assert "чёрн" in item["english_prompt"].lower()


def test_shot_location_prefers_llm_courtyard_over_scene_canonical(monkeypatch):
    _patch_local_helpers(monkeypatch)
    workshop = {
        "name": "Workshop",
        "reference_image_path": "/references/locations/workshop.png",
    }
    courtyard = {
        "name": "Courtyard",
        "reference_image_path": "/references/locations/courtyard.png",
    }
    item = _create_shot_item(
        **_item_kwargs(
            {
                "english_prompt": "Человек бежит по двору",
                "negative_prompt": "negative",
                "characters": [],
                "location": "Courtyard",
            },
            locations_data=[workshop, courtyard],
            location_canon_name="Workshop",
            shot_description="Человек бежит по Courtyard",
        )
    )

    assert item["locations"] == [courtyard]
    assert "/references/locations/courtyard.png" in item["reference_image_paths"]


def test_unknown_shot_location_falls_back_to_canonical(monkeypatch):
    _patch_local_helpers(monkeypatch, location_reply="Workshop")
    workshop = {
        "name": "Workshop",
        "reference_image_path": "/references/locations/workshop.png",
    }
    courtyard = {
        "name": "Courtyard",
        "reference_image_path": "/references/locations/courtyard.png",
    }
    item = _create_shot_item(
        **_item_kwargs(
            {
                "english_prompt": "Человек идёт",
                "negative_prompt": "negative",
                "characters": [],
                "location": "Unknown place",
            },
            locations_data=[workshop, courtyard],
            location_canon_name="Workshop",
        )
    )
    assert item["locations"] == [workshop]


def test_xerox_collage_does_not_add_bare_collage_negative(monkeypatch):
    _patch_local_helpers(monkeypatch)
    item = _create_shot_item(
        **_item_kwargs(
            {
                "english_prompt": "Сцена на бумаге",
                "negative_prompt": "LLM NEGATIVE",
                "characters": [],
            },
            visual_style="xerox collage / ксероксный коллаж",
        )
    )
    negative_tokens = {token.strip().casefold() for token in item["negative_prompt"].split(",")}
    assert "collage" not in negative_tokens
    assert "коллаж" not in negative_tokens
    assert "панели" in negative_tokens


def test_end_location_lock_passes_start_location_to_factory(tmp_path, monkeypatch):
    project_id = "location_lock_project"
    base = tmp_path / project_id
    (base / "91_screenplay").mkdir(parents=True)
    (base / "20_bible").mkdir(parents=True)
    (base / "91_screenplay" / "screenplay.json").write_text(
        json.dumps(
            {
                "screenplay": [
                    {
                        "scene_number": 1,
                        "action": "Человек идёт",
                        "characters": [],
                        "storyboard": [
                            {
                                "shot_number": 1,
                                "description": "Человек идёт",
                                "camera_plan": "Medium shot",
                                "timing": "1s",
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (base / "20_bible" / "characters.json").write_text("[]", encoding="utf-8")
    (base / "20_bible" / "locations.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))

    class _Future:
        def __init__(self, fn, *args, **kwargs):
            self.value = fn(*args, **kwargs)

        def result(self):
            return self.value

    class _Executor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, *args, **kwargs):
            return _Future(fn, *args, **kwargs)

    courtyard = {
        "name": "Courtyard",
        "reference_image_path": "/references/locations/courtyard.png",
    }
    factory_calls = []

    monkeypatch.setattr(shots_generator, "ThreadPoolExecutor", _Executor)
    monkeypatch.setattr(shots_generator, "as_completed", lambda futures: list(futures))
    monkeypatch.setattr(
        shots_generator,
        "_build_extended_context",
        lambda **kwargs: {
            "shot_number": 1,
            "shot_frame_spec": {"primary_subject": "человек", "must_show": ["человек"]},
            "full_shot_frame_spec": {"primary_subject": "человек", "must_show": ["человек"]},
            "shot_frame_spec_cache_key": "scene_1_shot_1",
            "scene_continuity_facts": {},
            "location_time": "",
            "location_canon_name": "",
        },
    )

    def fake_generate(extended_context, shot_type, **kwargs):
        result = {
            "english_prompt": "START" if shot_type == "start" else "END",
            "negative_prompt": "negative",
            "characters": [],
            "camera_position": "front",
            "initial_state_summary": "state",
            "add_end_shot": "true",
            "should_link_as_next_start": "true",
            "should_use_prev_end_as_reference": "false",
        }
        result["location"] = "Courtyard" if shot_type == "start" else "Workshop"
        return result

    def fake_create(**kwargs):
        factory_calls.append(kwargs)
        if kwargs["shot_type"] == "start":
            return {"locations": [courtyard], "output_path": "/tmp/start.png", "video_prompt": ""}
        return {"locations": [courtyard], "output_path": "/tmp/end.png", "video_prompt": ""}

    monkeypatch.setattr(shots_generator, "_generate_shot_prompt", fake_generate)
    monkeypatch.setattr(shots_generator, "_create_shot_item", fake_create)
    monkeypatch.setattr(shots_generator, "_allow_end_location_change_via_llm", lambda **kwargs: False)
    monkeypatch.setattr(
        shots_generator,
        "_generate_transition_video_prompt",
        lambda **kwargs: "Camera holds; subject walks; room remains stable; steady pace",
    )
    monkeypatch.setattr(shots_generator, "_generate_fcpxml", lambda *args, **kwargs: None)
    monkeypatch.setattr(shots_generator, "_generate_photo_fcpxml", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        shots_generator,
        "resolve_video_model_capabilities",
        lambda *args, **kwargs: {"supported_durations": [1, 5, 10], "warnings": []},
    )

    shots_generator.screenplay_shots_generator_tool(
        session_id="s",
        project_id=project_id,
        generate_end_shots=True,
        language="ru",
        generate_blockout=False,
    )

    assert len(factory_calls) == 2
    end_call = next(call for call in factory_calls if call["shot_type"] == "end")
    assert end_call["llm_result"]["location"] == "Courtyard"
    assert end_call["location_canon_name"] == "Courtyard"


def test_image_style_catalog_reuses_default_and_honors_frame_bindings(monkeypatch):
    _patch_local_helpers(monkeypatch)
    catalog = {
        "default_style_id": "main",
        "styles": {
            "main": {"prompt": "MAIN IMAGE STYLE"},
            "other": {"prompt": "OTHER IMAGE STYLE"},
            "unrelated": {"prompt": "UNRELATED IMAGE STYLE"},
        },
        "frame_bindings": {
            "3:2:start": "other",
            "3:2:end": "main",
        },
    }

    default_start = _create_shot_item(
        **_item_kwargs(
            {"english_prompt": "START FRAME", "negative_prompt": "negative", "characters": []},
            shot_type="start",
            shot_number=1,
            image_style_catalog=catalog,
        )
    )
    default_end = _create_shot_item(
        **_item_kwargs(
            {"english_prompt": "END FRAME", "negative_prompt": "negative", "characters": []},
            shot_number=1,
            image_style_catalog=catalog,
        )
    )
    bound_start = _create_shot_item(
        **_item_kwargs(
            {"english_prompt": "BOUND START", "negative_prompt": "negative", "characters": []},
            shot_type="start",
            image_style_catalog=catalog,
        )
    )
    bound_end = _create_shot_item(
        **_item_kwargs(
            {"english_prompt": "BOUND END", "negative_prompt": "negative", "characters": []},
            image_style_catalog={**catalog, "frame_bindings": {"3:2:end": "other"}},
        )
    )

    assert default_start["style_id"] == "main"
    assert default_end["style_id"] == "main"
    assert "MAIN IMAGE STYLE" in default_start["english_prompt"]
    assert "MAIN IMAGE STYLE" in default_end["english_prompt"]
    assert bound_start["style_id"] == "other"
    assert "OTHER IMAGE STYLE" in bound_start["english_prompt"]
    assert bound_end["style_id"] == "other"
    assert "OTHER IMAGE STYLE" in bound_end["english_prompt"]
    for item in (default_start, default_end, bound_start, bound_end):
        assert "UNRELATED IMAGE STYLE" not in item["english_prompt"]


def test_image_style_catalog_rejects_unknown_frame_binding(monkeypatch):
    _patch_local_helpers(monkeypatch)
    catalog = {
        "default_style_id": "main",
        "styles": {"main": {"prompt": "MAIN IMAGE STYLE"}},
        "frame_bindings": {"3:2:end": "missing"},
    }
    with pytest.raises(ValueError):
        _create_shot_item(
            **_item_kwargs(
                {"english_prompt": "END FRAME", "negative_prompt": "negative", "characters": []},
                image_style_catalog=catalog,
            )
        )


def test_merge_write_shots_persists_image_style_bindings(tmp_path):
    shots_path = tmp_path / "project" / "97_shots" / "shots.json"
    catalog = {
        "default_style_id": "main",
        "styles": {
            "main": {"prompt": "MAIN IMAGE STYLE"},
            "other": {"prompt": "OTHER IMAGE STYLE"},
        },
        "frame_bindings": {},
    }
    shots_data = {
        "items": [
            {"scene_number": 3, "shot_number": 2, "shot_type": "start", "style_id": "other"},
            {"scene_number": 3, "shot_number": 2, "shot_type": "end", "style_id": "main"},
        ]
    }

    shots_generator._merge_write_shots(
        str(shots_path),
        shots_data=shots_data,
        partial=False,
        image_style_catalog=catalog,
    )

    saved = json.loads(
        (tmp_path / "project" / "30_style" / "image_styles.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["styles"] == catalog["styles"]
    assert saved["frame_bindings"] == {
        "3:2:start": "other",
        "3:2:end": "main",
    }

