"""QA не должен считать сцену проверенной без списка repairs в ответе модели."""
from __future__ import annotations

import json

import pytest

import custom_tools.storybook.shots_prompt_qa as qa


@pytest.mark.parametrize("scene_count", [1, 2])
@pytest.mark.parametrize(
    "invalid_response",
    [
        {"type": "json_object", "note": "placeholder"},
        {"repairs": "bad"},
        {"repairs": None},
        {"repairs": ["malformed entry"]},
        {"repairs": [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "set": {}}]},
        {"repairs": [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "set": {"unexpected_field": "mutation"}}]},
        {"repairs": [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "set": {"english_prompt": "Worker"}, "video_prompt": "Misplaced mutation"}]},
    ],
)
def test_invalid_qa_response_does_not_mark_requested_scene_complete(
    tmp_path, monkeypatch, scene_count, invalid_response
):
    project_id = "p"
    screenplay_dir = tmp_path / project_id / "91_screenplay"
    screenplay_dir.mkdir(parents=True)
    scenes = [
        {
            "scene_number": scene_number,
            "characters": ["Cyclist"],
            "action": "",
            "location_time": "",
            "storyboard": [{
                "shot_number": 1,
                "camera_plan": "MEDIUM SHOT",
                "description": "Cyclist waits",
                "timing": "0-1",
            }],
        }
        for scene_number in range(1, scene_count + 1)
    ]
    (screenplay_dir / "screenplay.json").write_text(
        json.dumps({"screenplay": scenes}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        qa,
        "_extract_shot_frame_spec_llm",
        lambda **kwargs: {"primary_subject": "cyclist", "must_show": ["cyclist"]},
    )

    invalid_calls = []

    def fake_call_openai_api(**kwargs):
        payload = json.loads(str(kwargs["prompt"]).split("INPUT:\n", 1)[1])
        if payload["scene_number"] == scene_count:
            invalid_calls.append(kwargs["prompt"])
            if len(invalid_calls) > 1 and "unexpected_field" in json.dumps(invalid_response):
                assert 'Unsupported set fields: ["unexpected_field"]' in kwargs["prompt"]
            return json.dumps(invalid_response)
        return json.dumps({"repairs": []})

    monkeypatch.setattr(qa, "call_openai_api", fake_call_openai_api)
    items = [
        {
            "scene_number": scene_number,
            "shot_number": 1,
            "shot_type": "start",
            "english_prompt": "Cyclist",
            "video_prompt": "",
            "negative_prompt": "blur",
            "_shot_frame_spec": {"primary_subject": "cyclist", "must_show": ["cyclist"]},
        }
        for scene_number in range(1, scene_count + 1)
    ]

    with pytest.raises(RuntimeError, match="not completed|не заверш|QA"):
        qa.shots_prompt_qa_tool(
            session_id="s",
            project_id=project_id,
            shots_data={"items": items, "consistency_rules": []},
            force=True,
            dry_run=True,
            global_max_repairs=0,
        )


def test_detected_video_violations_require_repairs_for_each_affected_shot(tmp_path, monkeypatch):
    project_id = "video_coverage"
    screenplay_dir = tmp_path / project_id / "91_screenplay"
    screenplay_dir.mkdir(parents=True)
    (screenplay_dir / "screenplay.json").write_text(
        json.dumps({"screenplay": [{
            "scene_number": 1,
            "characters": ["Worker"],
            "action": "Worker crosses the room",
            "storyboard": [
                {"shot_number": 1, "camera_plan": "MEDIUM SHOT", "description": "First phase", "timing": "0-1"},
                {"shot_number": 2, "camera_plan": "MEDIUM SHOT", "description": "Second phase", "timing": "1-2"},
            ],
        }]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        qa,
        "_extract_shot_frame_spec_llm",
        lambda **kwargs: {"primary_subject": "Worker", "must_show": ["Worker"]},
    )
    monkeypatch.setattr(qa, "_llm_judge_video_prompt_lite", lambda **kwargs: {"contradiction_before": False})
    monkeypatch.setattr(
        qa,
        "_detect_closeup_video_prompt_violations",
        lambda video_prompt, camera_plan, storyboard_description: ["required"]
        if storyboard_description in {"First phase", "Second phase"} else [],
    )

    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs["prompt"])
        if len(calls) > 1:
            assert 'Unresolved required video repairs: [[2, "start"]]' in kwargs["prompt"]
        return json.dumps({
            "repairs": [{
                "scene_number": 1,
                "shot_number": 1,
                "shot_type": "start",
                "set": {"video_prompt": "Worker crosses the room."},
                "reasons": ["first violation only"],
            }],
        })

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    items = [
        {
            "scene_number": 1,
            "shot_number": shot_number,
            "shot_type": "start",
            "english_prompt": "Worker",
            "video_prompt": "existing motion",
            "negative_prompt": "blur",
            "_shot_frame_spec": {"primary_subject": "Worker", "must_show": ["Worker"]},
        }
        for shot_number in (1, 2)
    ]

    with pytest.raises(RuntimeError, match="not completed|не заверш|QA"):
        qa.shots_prompt_qa_tool(
            session_id="s",
            project_id=project_id,
            shots_data={"items": items, "consistency_rules": []},
            force=True,
            dry_run=True,
            global_max_repairs=0,
        )
