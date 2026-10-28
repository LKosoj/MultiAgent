"""Узкие контракты QA для фазовых specs и close-up source context."""
from __future__ import annotations

import json

import custom_tools.storybook.shots_prompt_qa as sq
from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils


def _phase_spec(name: str, end_state: str = "end"):
    return {
        "primary_subject": name,
        "visible_readable_texts": [],
        "start_state_spec": {"visible_readable_texts": [], "state": "start"},
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


def test_qa_payload_keeps_scene_characters_and_phase_specs(tmp_path, monkeypatch):
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
    captured = []

    def fake_call_openai_api(**kwargs):
        prompt = str(kwargs.get("prompt") or "")
        if "INPUT:" in prompt:
            try:
                payload = json.loads(prompt.split("INPUT:\n", 1)[1])
                if "generated_shots" in payload:
                    captured.append(payload)
            except (ValueError, json.JSONDecodeError):
                pass
        return json.dumps({"repairs": [], "notes": "ok"})

    monkeypatch.setattr(sq, "call_openai_api", fake_call_openai_api)
    result = sq.shots_prompt_qa_tool(
        session_id="s",
        project_id=project_id,
        shots_data={
            "items": [
                {"scene_number": 1, "shot_number": 1, "shot_type": "start", "camera_plan": "MEDIUM SHOT", "english_prompt": "alpha", "video_prompt": "", "_shot_frame_spec": _phase_spec("Alpha"), "_shot_frame_spec_cache_key": _current_key(1, "Alpha")},
                {"scene_number": 1, "shot_number": 1, "shot_type": "end", "camera_plan": "MEDIUM SHOT", "english_prompt": "alpha end", "video_prompt": "", "_shot_frame_spec": _phase_spec("Alpha", "alpha-end"), "_shot_frame_spec_cache_key": _current_key(1, "Alpha")},
                {"scene_number": 2, "shot_number": 1, "shot_type": "start", "camera_plan": "MEDIUM SHOT", "english_prompt": "beta", "video_prompt": "", "_shot_frame_spec": _phase_spec("Beta"), "_shot_frame_spec_cache_key": _current_key(2, "Beta")},
                {"scene_number": 2, "shot_number": 1, "shot_type": "end", "camera_plan": "MEDIUM SHOT", "english_prompt": "beta end", "video_prompt": "", "_shot_frame_spec": _phase_spec("Beta", "beta-end"), "_shot_frame_spec_cache_key": _current_key(2, "Beta")},
            ],
            "consistency_rules": [],
        },
        force=True,
        global_max_repairs=0,
        dry_run=True,
    )

    assert len(captured) == 2
    by_scene = {payload["scene_number"]: payload for payload in captured}
    assert by_scene[1]["storyboard"][0]["characters"] == ["Alpha"]
    assert by_scene[2]["storyboard"][0]["characters"] == ["Beta"]
    for scene_number, character in ((1, "Alpha"), (2, "Beta")):
        payload = by_scene[scene_number]
        assert payload["storyboard"][0]["characters"] == [character]
        shots_by_type = {shot["shot_type"]: shot for shot in payload["generated_shots"]}
        full_spec = payload["shot_frame_specs"].get("1", payload["shot_frame_specs"].get(1))
        assert shots_by_type["start"]["phase_state_spec"] == full_spec["start_state_spec"]
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
