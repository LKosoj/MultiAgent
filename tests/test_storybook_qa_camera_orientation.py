"""QA must carry camera/viewpoint fields together with prompt repairs."""
from __future__ import annotations

import json

import custom_tools.storybook.shots_prompt_qa as qa


ORIENTATION_FIELDS = {
    "camera_position": "side",
    "character_orientation": "profile",
    "final_camera_position": "behind",
    "final_character_orientation": "facing_away",
}


def test_apply_repairs_accepts_camera_orientation_fields():
    item = {
        "scene_number": 1,
        "shot_number": 1,
        "shot_type": "start",
        "english_prompt": "A worker at a table",
    }

    applied, rejected = qa._apply_repairs_to_items(
        [item],
        [{
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": "start",
            "set": dict(ORIENTATION_FIELDS),
        }],
    )

    assert rejected == []
    assert applied == 1
    for field, value in ORIENTATION_FIELDS.items():
        assert item[field] == value


def test_qa_persists_camera_orientation_repairs(tmp_path, monkeypatch):
    project_id = "camera_orientation"
    project_dir = tmp_path / project_id
    (project_dir / "91_screenplay").mkdir(parents=True)
    (project_dir / "91_screenplay" / "screenplay.json").write_text(
        json.dumps({
            "screenplay": [{
                "scene_number": 1,
                "characters": ["Worker"],
                "action": "A worker waits at a table",
                "location_time": "workshop",
                "storyboard": [{
                    "shot_number": 1,
                    "camera_plan": "MEDIUM SHOT",
                    "description": "A worker waits at a table",
                    "timing": "0-1",
                }],
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        qa,
        "_extract_shot_frame_spec_llm",
        lambda **kwargs: {"primary_subject": "Worker", "must_show": ["Worker"]},
    )

    def fake_call(**kwargs):
        payload = json.loads(str(kwargs["prompt"]).split("INPUT:\n", 1)[1])
        assert payload["generated_shots"]
        assert payload["generated_shots"][0]["shot_type"] == "start"
        for field in ORIENTATION_FIELDS:
            assert payload["generated_shots"][0][field] == "contradictory old value"
        return json.dumps({
            "repairs": [{
                "scene_number": 1,
                "shot_number": 1,
                "shot_type": "start",
                "set": dict(ORIENTATION_FIELDS),
                "reasons": ["camera orientation correction"],
            }],
            "notes": "applied",
        })

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    result = qa.shots_prompt_qa_tool(
        session_id="s",
        project_id=project_id,
        shots_data={"items": [{
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": "start",
            "english_prompt": "A worker at a table",
            "video_prompt": "",
            "negative_prompt": "blur",
            **{field: "contradictory old value" for field in ORIENTATION_FIELDS},
        }]},
        force=True,
        global_max_repairs=0,
        dry_run=False,
    )

    saved = json.loads(
        (project_dir / "97_shots" / "shots.json").read_text(encoding="utf-8")
    )
    assert result["items"][0]
    for field, value in ORIENTATION_FIELDS.items():
        assert saved["items"][0][field] == value
