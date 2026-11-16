"""Узкие контракты QA для фазовых specs и close-up source context."""
from __future__ import annotations

import json

import pytest

import custom_tools.storybook.shots_prompt_qa as sq
from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils


def _phase_spec(name: str, end_state: str = "end"):
    return {
        "primary_subject": name,
        "visible_readable_texts": [],
        "start_state_spec": {"visible_readable_texts": ["ТОЧНАЯ НАДПИСЬ"], "state": "start",
                             "t0_mode": "frozen", "holder": None, "visible": False,
                             "character_pose_signatures": {name: "inside screen"}},
        "end_state_spec": {"visible_readable_texts": [], "state": end_state},
    }


def _current_key(scene_number: int, character: str):
    return shared_utils._build_shot_frame_spec_cache_key(
        scene_number=scene_number,
        shot_number=1,
        shot_description="",
        camera_plan="MEDIUM SHOT",
        scene_characters=[character],
        location_time="",
        location_canon_name="",
    )


@pytest.mark.parametrize("start_duration,end_duration,has_duration", [
    (None, None, False), (0, None, False), (-1, None, False), ("", None, False),
    ("invalid", None, False), ("2.75", None, False), (float("nan"), None, False),
    (float("inf"), None, False), (float("-inf"), None, False),
    (True, None, False), (False, None, False),
    (2.75, None, True), (None, 2.75, True),
])
def test_qa_payload_keeps_scene_characters_and_phase_specs(tmp_path, monkeypatch, start_duration, end_duration, has_duration):
    project_id = "qa_phase_payload"
    screenplay_dir = tmp_path / project_id / "91_screenplay"
    screenplay_dir.mkdir(parents=True)
    scenes = [
        {
            "scene_number": 1,
            "characters": ["Alpha"],
            "location_time": "",
            "action": "",
            "storyboard": [{"shot_number": 1, "camera_plan": "MEDIUM SHOT", "description": "", "timing": "0-1"}],
        },
        {
            "scene_number": 2,
            "characters": ["Beta"],
            "location_time": "",
            "action": "",
            "storyboard": [{"shot_number": 1, "camera_plan": "MEDIUM SHOT", "description": "", "timing": "0-1"}],
        },
    ]
    (screenplay_dir / "screenplay.json").write_text(
        json.dumps({"screenplay": scenes}), encoding="utf-8"
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(sq, "video_prompt_violations", lambda *_args: [])
    captured = []
    systems = []

    def fake_call_openai_api(**kwargs):
        prompt = str(kwargs.get("prompt") or "")
        if "INPUT:" in prompt:
            try:
                payload = json.loads(prompt.split("INPUT:\n", 1)[1])
                if "generated_shots" in payload:
                    captured.append(payload)
                    systems.append(kwargs["system_prompt"])
            except (ValueError, json.JSONDecodeError):
                pass
        return json.dumps({"repairs": [], "notes": "ok"})

    monkeypatch.setattr(sq, "call_openai_api", fake_call_openai_api)
    result = sq.shots_prompt_qa_tool(
        session_id="s",
        project_id=project_id,
        shots_data={
            "items": [
                {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": start_duration, "timing": "0-8", "camera_plan": "STALE CLOSE UP", "english_prompt": "alpha", "video_prompt": "", "_shot_frame_spec": _phase_spec("Alpha"), "_shot_frame_spec_cache_key": _current_key(1, "Alpha")},
                {"scene_number": 1, "shot_number": 1, "shot_type": "end", "duration_s": end_duration, "timing": "0-8", "camera_plan": "MEDIUM SHOT", "english_prompt": "alpha end", "video_prompt": "", "_shot_frame_spec": _phase_spec("Alpha", "alpha-end"), "_shot_frame_spec_cache_key": _current_key(1, "Alpha")},
                {"scene_number": 2, "shot_number": 1, "shot_type": "start", "camera_plan": "MEDIUM SHOT", "timing": "1-2", "english_prompt": "beta", "video_prompt": "", "_shot_frame_spec": _phase_spec("Beta"), "_shot_frame_spec_cache_key": _current_key(2, "Beta")},
                {"scene_number": 2, "shot_number": 1, "shot_type": "end", "camera_plan": "MEDIUM SHOT", "english_prompt": "beta end", "video_prompt": "unexpected existing END video", "_shot_frame_spec": _phase_spec("Beta", "beta-end"), "_shot_frame_spec_cache_key": _current_key(2, "Beta")},
            ],
            "consistency_rules": [],
        },
        force=True,
        global_max_repairs=0,
        dry_run=True,
    )

    assert len(captured) == 2
    assert all("SOURCE PRIORITY (highest to lowest)" in system for system in systems)
    assert all("visible=false does not forbid" in system for system in systems)
    assert all("If phase_state_spec is omitted" in system for system in systems)
    assert all("not an exhaustive visible cast" in system for system in systems)
    assert all(sq.VIDEO_PROMPT_RULES in system for system in systems)
    assert all(system.index("First derive each phase") < system.index("Inspect EVERY START") for system in systems)
    by_scene = {payload["scene_number"]: payload for payload in captured}
    assert by_scene[1]["characters"] == ["Alpha"]
    assert by_scene[2]["characters"] == ["Beta"]
    for scene_number, character in ((1, "Alpha"), (2, "Beta")):
        payload = by_scene[scene_number]
        assert payload["characters"] == [character]
        assert "characters" not in payload["storyboard"][0]
        shots_by_type = {shot["shot_type"]: shot for shot in payload["generated_shots"]}
        assert payload["storyboard"][0]["camera_plan"] == "MEDIUM SHOT"
        assert all("camera_plan" not in shot for shot in shots_by_type.values())
        if scene_number == 1:
            if has_duration:
                assert "timing" not in payload["storyboard"][0]
                assert all("timing" not in shot for shot in shots_by_type.values())
            else:
                assert payload["storyboard"][0]["timing"] == "0-1"
                assert all(shot["timing"] == "0-8" for shot in shots_by_type.values())
            assert "video_prompt" not in shots_by_type["end"]
        else:
            assert payload["storyboard"][0]["timing"] == "0-1"
            assert shots_by_type["start"]["timing"] == "1-2"
            assert shots_by_type["end"]["video_prompt"] == "unexpected existing END video"
        full_spec = payload["shot_frame_specs"].get("1", payload["shot_frame_specs"].get(1))
        assert "phase_state_spec" not in shots_by_type["start"]
        assert full_spec["start_state_spec"]["state"] == "start"
        assert full_spec["start_state_spec"]["holder"] is None
        assert full_spec["start_state_spec"]["visible"] is False
        assert full_spec["start_state_spec"]["t0_mode"] == "frozen"
        assert full_spec["start_state_spec"]["visible_readable_texts"] == ["ТОЧНАЯ НАДПИСЬ"]
        assert full_spec["start_state_spec"]["character_pose_signatures"][character] == "inside screen"
        assert all("storyboard_description" not in shot and "storyboard_camera_plan" not in shot
                   and "transition_video_prompt" not in shot for shot in shots_by_type.values())
        assert shots_by_type["end"]["phase_state_spec"]["state"] == f"{'alpha' if scene_number == 1 else 'beta'}-end"
        assert shots_by_type["end"]["phase_state_spec"] != full_spec["end_state_spec"]
        assert "shot_frame_spec" not in shots_by_type["start"]
        assert "shot_frame_spec" not in shots_by_type["end"]
    assert result["items"]


def test_closeup_cache_key_includes_storyboard_description(monkeypatch):
    sq._CLOSEUP_ROOM_CHECK_CACHE.clear()
    calls = []

    def fake_call_openai_api(**kwargs):
        payload = json.loads(kwargs["prompt"])
        calls.append(payload)
        violations = ["valve"] if payload["storyboard_description"] == "SOURCE B" else []
        return json.dumps({"violations": violations})

    monkeypatch.setattr(sq, "call_openai_api", fake_call_openai_api)
    assert sq._llm_check_closeup_room_details("close-up valve", "CLOSE UP", "SOURCE A") == []
    assert sq._llm_check_closeup_room_details("close-up valve", "CLOSE UP", "SOURCE B") == ["valve"]
    assert len(calls) == 2
    assert {call["storyboard_description"] for call in calls} == {"SOURCE A", "SOURCE B"}


def test_video_prompt_repair_passes_storyboard_description_to_closeup_checks(monkeypatch):
    seen = []

    monkeypatch.setattr(sq, "_detect_closeup_video_prompt_violations", lambda *args, **kwargs: [])

    def fake_judge(**kwargs):
        return {"contradiction_before": kwargs["candidate_video_prompt"] is None, "candidate_ok": True, "rewritten_candidate": None}

    def fake_closeup(*args, **kwargs):
        description = kwargs.get("storyboard_description")
        if description is None and len(args) > 2:
            description = args[2]
        seen.append(description)
        return False

    monkeypatch.setattr(sq, "_llm_judge_video_prompt_lite", fake_judge)
    monkeypatch.setattr(sq, "_has_any_closeup_room_markers", fake_closeup)
    items = [{
        "scene_number": 1,
        "shot_number": 1,
        "shot_type": "start",
        "video_prompt": "close-up old subject",
        "reference_image_paths": ["ref.png"],
    }]
    applied, rejected = sq._apply_repairs_to_items(
        items,
        [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "set": {"video_prompt": "close-up valve turning"}}],
        storyboard_lookup={(1, 1): {"description": "Main subject is a valve", "camera_plan": "CLOSE UP"}},
    )

    assert applied == 1
    assert rejected == []
    assert items[0]["video_prompt"] == "close-up valve turning"
    assert seen and all(description == "Main subject is a valve" for description in seen)
