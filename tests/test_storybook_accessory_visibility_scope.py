import json

from custom_tools.storybook.screenplay_shots_generator_utils import technical


def test_accessory_visibility_payload_is_limited_to_current_frame(monkeypatch):
    captured = {}

    def fake_call_openai_api(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        captured["system_prompt"] = kwargs["system_prompt"]
        return json.dumps(
            {
                "must_show_additions": [],
                "must_not_show_additions": [],
                "negative_prompt_tokens": [],
            }
        )

    monkeypatch.setattr(technical, "call_openai_api", fake_call_openai_api)
    result = technical._infer_accessory_visibility_via_llm(
        shot_type="end",
        shot_frame_spec={
            "primary_subject": "Neutral Person",
            "visible_characters": ["Neutral Person"],
            "must_show": ["person back view"],
            "must_not_show": ["GLOBAL_UNRELATED_SCENE_BAN"],
            "world_physics": {"support_state": "feet on floor"},
        },
        llm_result={
            "camera_position": "side",
            "character_orientation": "back",
            "point_of_view": "objective",
            "spatial_composition": "center frame",
            "prop_continuity": {"removed": ["coat"]},
        },
        shot_characters=[
            {
                "name": "Neutral Person",
                "variable_attributes": {
                    "base_clothing": "coat",
                    "accessories": ["badge"],
                },
            }
        ],
    )

    payload = json.loads(captured["prompt"].split("INPUT:", 1)[1])
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "GLOBAL_UNRELATED_SCENE_BAN" not in serialized
    assert payload["shot_frame_spec"]["must_show"] == ["person back view"]
    assert payload["camera_position"] == "side"
    assert payload["character_visual_items"] == [
        {"character": "Neutral Person", "items": ["coat", "badge"]}
    ]
    assert payload["prop_continuity"] == {"removed": ["coat"]}
    assert result == {
        "must_show_additions": [],
        "must_not_show_additions": [],
        "negative_prompt_tokens": [],
    }
