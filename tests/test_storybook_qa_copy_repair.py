"""START prompt repairs must stop stale full-copy reuse."""
from __future__ import annotations

import copy
import json

import pytest

from custom_tools.storybook import shots_prompt_qa as qa


@pytest.mark.parametrize("candidate", ["START crop: hand visible", "OLD START PROMPT"])
def test_start_english_repair_disables_stale_copy(candidate):
    source_end = "/plots/storybooks/p/97_shots/scene_05_shot_04/img_final_end_05_04.png"
    items = [
        {
            "scene_number": 5,
            "shot_number": 4,
            "shot_type": "start",
            "english_prompt": "OLD START PROMPT",
            "video_prompt": "",
            "copy_from_previous_end": True,
            "source_end_path": source_end,
            "reference_image_paths": [source_end, "/references/characters/hero.png"],
            "image_path": None,
            "link_type": "full_copy",
            "should_link_as_next_start": True,
            "should_use_prev_end_as_reference": False,
        },
        {
            "scene_number": 5,
            "shot_number": 4,
            "shot_type": "end",
            "english_prompt": "UNCHANGED END",
            "reference_image_paths": [source_end],
            "copy_from_previous_end": False,
        },
    ]
    before_other = copy.deepcopy(items[1])

    applied, rejected = qa._apply_repairs_to_items(
        items,
        [{
            "scene_number": 5,
            "shot_number": 4,
            "shot_type": "start",
            "set": {"english_prompt": candidate},
        }],
        storyboard_lookup={(5, 4): {"description": "A new crop", "camera_plan": "MEDIUM SHOT"}},
    )

    assert applied == 1
    assert rejected == []
    assert items[0]["english_prompt"] == candidate
    assert items[0]["copy_from_previous_end"] is False
    assert items[0]["link_type"] == "reference"
    assert items[0]["should_link_as_next_start"] is False
    assert items[0]["should_use_prev_end_as_reference"] is True
    assert items[0]["reference_image_paths"] == [source_end, "/references/characters/hero.png"]
    assert items[1] == before_other


def test_start_copy_flags_are_persisted_to_existing_shots(tmp_path, monkeypatch):
    base = tmp_path / "persist_copy"
    (base / "91_screenplay").mkdir(parents=True)
    (base / "97_shots").mkdir(parents=True)
    (base / "91_screenplay" / "screenplay.json").write_text(
        json.dumps({"screenplay": [{
            "scene_number": 1,
            "characters": [],
            "storyboard": [{"shot_number": 1, "camera_plan": "MEDIUM SHOT", "description": "A new crop"}],
        }]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    source_end = "/plots/storybooks/persist_copy/97_shots/end.png"
    item = {
        "scene_number": 1,
        "shot_number": 1,
        "shot_type": "start",
        "english_prompt": "OLD START",
        "video_prompt": "",
        "reference_image_paths": [source_end, "/references/characters/hero.png"],
        "copy_from_previous_end": True,
        "source_end_path": source_end,
        "link_type": "full_copy",
        "should_link_as_next_start": True,
        "should_use_prev_end_as_reference": False,
        "_shot_frame_spec": {"primary_subject": "hero", "must_show": ["hero"]},
    }
    payload = {"items": [item], "consistency_rules": []}
    (base / "97_shots" / "shots.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    monkeypatch.setattr(
        qa,
        "_extract_shot_frame_spec_llm",
        lambda **kwargs: {"primary_subject": "hero", "must_show": ["hero"]},
    )
    def fake_call(**kwargs):
        audit = json.loads(kwargs["prompt"].split("INPUT:\n", 1)[1])
        frame = audit["generated_shots"][0]
        assert frame["copy_from_previous_end"] is True
        assert frame["source_end_path"] == source_end
        return json.dumps({"repairs": [{
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": "start",
            "set": {"english_prompt": "START crop: hand visible"},
        }]})

    monkeypatch.setattr(qa, "call_openai_api", fake_call)

    qa.shots_prompt_qa_tool(
        "s", "persist_copy", shots_data=payload, force=True, dry_run=False, global_max_repairs=0
    )

    saved = json.loads((base / "97_shots" / "shots.json").read_text(encoding="utf-8"))
    saved_item = saved["items"][0]
    assert saved_item["copy_from_previous_end"] is False
    assert saved_item["link_type"] == "reference"
    assert saved_item["should_link_as_next_start"] is False
    assert saved_item["should_use_prev_end_as_reference"] is True
