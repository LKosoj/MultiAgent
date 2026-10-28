"""END prompt judge must inspect the current START/END rendered pair."""
from __future__ import annotations

import json

import pytest
from PIL import Image

from custom_tools.storybook import shots_prompt_qa as qa


def test_end_repair_uses_actual_pair_render(monkeypatch, tmp_path):
    start_path = tmp_path / "start.png"
    end_path = tmp_path / "end.png"
    Image.new("RGB", (64, 64), "gray").save(start_path)
    Image.new("RGB", (64, 64), "gray").save(end_path)
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        assert kwargs.get("image_url"), kwargs
        payload = json.loads(kwargs["prompt"].split("INPUT:\n", 1)[1])
        if "50mm" in payload.get("start_english_prompt", ""):
            return json.dumps({
                "candidate_ok": False,
                "rewritten_candidate": "Cyclist passes; window shifted right, 50mm lens",
            })
        return json.dumps({
            "candidate_ok": False,
            "rewritten_candidate": "Cyclist passes; window shifted right, 24mm lens",
        })

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    qa._END_ENGLISH_JUDGE_CACHE.clear()
    items = [
        {
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": "start",
            "english_prompt": "Cyclist passes the window on the left, 50mm lens",
            "video_prompt": "camera tracks sideways",
            "output_path": str(start_path),
        },
        {
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": "end",
            "english_prompt": "Cyclist passes the window on the left",
            "video_prompt": "",
            "output_path": str(end_path),
            "_shot_frame_spec": {
                "end_state_spec": {
                    "facial_expression": "tired without smile",
                    "world_physics": {"body_relation": "outside"},
                },
            },
            "_scene_continuity_facts": {"side": "outside"},
            "style_id": "paper",
        },
    ]
    applied, rejected = qa._apply_repairs_to_items(
        items,
        [{
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": "end",
            "set": {"english_prompt": "Cyclist passes; window shifted right relative to START"},
            "reasons": ["Keep the character outside and tired"],
        }],
        storyboard_lookup={(1, 1): {
            "description": "Cyclist passes a window",
            "camera_plan": "SIDE TRACKING",
        }},
        image_styles={"paper": "READY PAPER"},
    )

    assert applied == 1, (rejected, calls)
    assert rejected == []
    assert items[1]["english_prompt"].endswith("50mm lens")
    assert calls and calls[0].get("image_url")
    caller_payload = json.loads(calls[0]["prompt"].split("INPUT:\n", 1)[1])
    assert caller_payload["start_english_prompt"] == "Cyclist passes the window on the left, 50mm lens"
    assert caller_payload["phase_state_spec"] == items[1]["_shot_frame_spec"]["end_state_spec"]
    assert caller_payload["scene_continuity_facts"] == {"side": "outside"}
    assert caller_payload["repair_reasons"] == ["Keep the character outside and tired"]
    assert caller_payload["image_style"] == "READY PAPER"

    no_start = qa._llm_judge_end_english_prompt_candidate(
        before_english_prompt="old END, 24mm lens",
        candidate_english_prompt="new END",
        storyboard_description="Cyclist passes a window",
        storyboard_camera_plan="SIDE TRACKING",
        transition_video_prompt="camera tracks sideways",
        rendered_image_url="data:image/png;base64,no-start",
    )
    assert no_start["rewritten_candidate"].endswith("24mm lens")


def test_end_judge_cache_changes_when_render_changes(monkeypatch):
    qa._END_ENGLISH_JUDGE_CACHE.clear()
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"candidate_ok": true}'

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    common = {
        "before_english_prompt": "Cyclist passes the window on the left",
        "candidate_english_prompt": "Cyclist passes; window shifted right relative to START",
        "storyboard_description": "Cyclist passes a window",
        "storyboard_camera_plan": "SIDE TRACKING",
        "transition_video_prompt": "camera tracks sideways",
    }
    for image_url in ("data:image/png;base64,AAA", "data:image/png;base64,BBB", "data:image/png;base64,AAA"):
        qa._llm_judge_end_english_prompt_candidate(
            **common, rendered_image_url=image_url, start_english_prompt="START 50mm"
        )

    assert len(calls) == 2


def test_end_judge_cache_changes_when_start_core_changes(monkeypatch):
    qa._END_ENGLISH_JUDGE_CACHE.clear()
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"candidate_ok": true}'

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    common = {
        "before_english_prompt": "Cyclist passes the window on the left",
        "candidate_english_prompt": "Cyclist passes; window shifted right relative to START",
        "storyboard_description": "Cyclist passes a window",
        "storyboard_camera_plan": "SIDE TRACKING",
        "transition_video_prompt": "camera tracks sideways",
        "rendered_image_url": "data:image/png;base64,AAA",
    }
    for start_prompt in ("START 50mm", "START 24mm", "START 50mm"):
        qa._llm_judge_end_english_prompt_candidate(
            **common, start_english_prompt=start_prompt
        )

    assert len(calls) == 2


def test_end_judge_cache_changes_when_phase_context_changes(monkeypatch):
    qa._END_ENGLISH_JUDGE_CACHE.clear()
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"candidate_ok": true}'

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    common = {
        "before_english_prompt": "Cyclist passes the window on the left",
        "candidate_english_prompt": "Cyclist passes; window shifted right relative to START",
        "storyboard_description": "Cyclist passes a window",
        "storyboard_camera_plan": "SIDE TRACKING",
        "transition_video_prompt": "camera tracks sideways",
        "rendered_image_url": "data:image/png;base64,AAA",
        "start_english_prompt": "START 50mm",
        "scene_continuity_facts": {"side": "outside"},
        "repair_reasons": ["Keep the character outside"],
        "image_style": "READY PAPER",
    }
    for phase_state_spec in (
        {"world_physics": {"body_relation": "outside"}},
        {"world_physics": {"body_relation": "inside"}},
        {"world_physics": {"body_relation": "outside"}},
    ):
        qa._llm_judge_end_english_prompt_candidate(
            **common, phase_state_spec=phase_state_spec
        )

    assert len(calls) == 2


@pytest.mark.parametrize("change_positive", [False, True])
def test_end_negative_repair_is_checked_with_positive_as_one_pair(monkeypatch, change_positive):
    qa._END_ENGLISH_JUDGE_CACHE.clear()
    calls = []

    def fake_call(**kwargs):
        payload = json.loads(kwargs["prompt"].split("INPUT:\n", 1)[1])
        calls.append(payload)
        assert payload["candidate_negative_prompt"] == "closed door, watermark"
        assert "closed" in payload["candidate_english_prompt"]
        return json.dumps({
            "candidate_ok": False,
            "rewritten_candidate": "The door is fully closed, 50mm lens",
            "rewritten_negative_prompt": "open door, watermark",
        })

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    items = [{
        "scene_number": 1, "shot_number": 1, "shot_type": "end",
        "english_prompt": "The door is closed, 50mm lens",
        "negative_prompt": "watermark",
        "_shot_frame_spec": {"end_state_spec": {"door": "closed"}},
    }]
    changes = {"negative_prompt": "closed door, watermark"}
    if change_positive:
        changes["english_prompt"] = "The closed door after the movement, 50mm lens"
    applied, rejected = qa._apply_repairs_to_items(
        items, [{"scene_number": 1, "shot_number": 1, "shot_type": "end", "set": changes}],
        storyboard_lookup={(1, 1): {"description": "The door closes completely", "camera_plan": "STATIC"}},
    )
    assert applied == 1 and rejected == []
    assert len(calls) == 1
    assert items[0]["english_prompt"] == "The door is fully closed, 50mm lens"
    assert items[0]["negative_prompt"] == "open door, watermark"


def test_end_judge_cache_changes_when_negative_changes(monkeypatch):
    qa._END_ENGLISH_JUDGE_CACHE.clear()
    calls = []
    monkeypatch.setattr(qa, "call_openai_api", lambda **kw: calls.append(kw) or '{"candidate_ok": true}')
    for negative in ("open door", "closed door", "open door"):
        qa._llm_judge_end_english_prompt_candidate(
            before_english_prompt="The door closes",
            candidate_english_prompt="The door is closed",
            storyboard_description="The door closes completely",
            storyboard_camera_plan="STATIC", transition_video_prompt="The door closes",
            candidate_negative_prompt=negative,
        )
    assert len(calls) == 2
