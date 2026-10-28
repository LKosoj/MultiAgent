"""Rendered-pixel findings must be fed back into the ordinary QA repair loop."""
from __future__ import annotations

import json

import pytest
from PIL import Image

from custom_tools.storybook import shots_prompt_qa as qa

@pytest.mark.parametrize("effective_repair", [True, False])
def test_rendered_visual_finding_blocks_unrelated_success_until_repaired(tmp_path, monkeypatch, effective_repair):
    base = tmp_path / "cyclist"
    (base / "91_screenplay").mkdir(parents=True)
    (base / "00_brief.json").write_text(
        json.dumps({"storybook_prompt": "A neutral cyclist scene."}), encoding="utf-8"
    )
    (base / "91_screenplay" / "screenplay.json").write_text(
        json.dumps({"screenplay": [{
            "scene_number": 1,
            "characters": ["Cyclist"],
            "action": "",
            "storyboard": [
                {"shot_number": 1, "camera_plan": "SIDE TRACKING", "description": "Cyclist passes a window", "timing": "0-1"},
                {"shot_number": 2, "camera_plan": "MEDIUM SHOT", "description": "Cyclist pauses", "timing": "1-2"},
            ],
        }]}), encoding="utf-8"
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        qa,
        "_extract_shot_frame_spec_llm",
        lambda **kwargs: {"primary_subject": "cyclist", "must_show": ["cyclist"]},
    )
    monkeypatch.setattr(
        qa,
        "_llm_judge_end_english_prompt_candidate",
        lambda **kwargs: {"candidate_ok": True},
    )

    output_paths = []
    for name in ("start1", "end1", "start2"):
        path = base / f"{name}.png"
        Image.new("RGB", (800, 450), "#6b7280").save(path)
        output_paths.append(str(path))

    items = [
        {
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": "start",
            "english_prompt": "Cyclist passes the window on the left",
            "video_prompt": "",
            "negative_prompt": "blur",
            "output_path": output_paths[0],
            "_shot_frame_spec": {"primary_subject": "cyclist", "must_show": ["cyclist"]},
        },
        {
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": "end",
            "english_prompt": "Cyclist passes the window on the left",
            "video_prompt": "",
            "negative_prompt": "blur",
            "output_path": output_paths[1],
            "_shot_frame_spec": {"primary_subject": "cyclist", "must_show": ["cyclist"]},
        },
        {
            "scene_number": 1,
            "shot_number": 2,
            "shot_type": "start",
            "english_prompt": "Cyclist pauses",
            "video_prompt": "",
            "negative_prompt": "blur",
            "output_path": output_paths[2],
            "_shot_frame_spec": {"primary_subject": "cyclist", "must_show": ["cyclist"]},
        },
        {
            "scene_number": 1,
            "shot_number": 2,
            "shot_type": "end",
            "english_prompt": "Cyclist pauses",
            "video_prompt": "",
            "negative_prompt": "blur",
            "output_path": output_paths[2],
            "_shot_frame_spec": {"primary_subject": "cyclist", "must_show": ["cyclist"]},
        },
    ]
    items.append({**items[0], "scene_number": 2, "_shot_frame_spec": {"primary_subject": "unrelated statue"}})
    audit_calls = []
    qa_calls = []
    main_payloads = []

    def fake_call(**kwargs):
        system_prompt = str(kwargs.get("system_prompt") or "").lower()
        if "rendered-frame visual audit" in system_prompt:
            audit_payload = json.loads(str(kwargs["prompt"]).split("AUDIT_INPUT:\n", 1)[1])
            audit_calls.append((kwargs, audit_payload))
            audited_shot = audit_payload["generated_shots"][0]["shot_number"]
            assert {shot["shot_number"] for shot in audit_payload["generated_shots"]} == {audited_shot}
            assert {shot["shot_type"] for shot in audit_payload["generated_shots"]} == {"start", "end"}
            if audited_shot == 2:
                return json.dumps({"violations": []})
            return json.dumps({"violations": [{
                "shot_number": 1,
                "shot_type": "end",
                "observed": "window remains on the wrong side of the cyclist",
                "required": "window shifts right relative to START while cyclist remains in side tracking",
            }]})
        payload = json.loads(str(kwargs["prompt"]).split("INPUT:\n", 1)[1])
        qa_calls.append(kwargs)
        main_payloads.append(payload)
        if len(qa_calls) == 1:
            return json.dumps({"repairs": [{
                "scene_number": 1,
                "shot_number": 2,
                "shot_type": "start",
                "set": {"english_prompt": "Cyclist pauses with a clear neutral framing"},
            }]})
        end_prompt = (
            "Cyclist passes; window shifted right relative to START"
            if effective_repair
            else "Cyclist passes the window on the left"
        )
        return json.dumps({"repairs": [
            {
                "scene_number": 1,
                "shot_number": 1,
                "shot_type": "end",
                "set": {"english_prompt": end_prompt},
            },
            {
                "scene_number": 1,
                "shot_number": 2,
                "shot_type": "start",
                "set": {"english_prompt": "Cyclist pauses with a clear neutral framing"},
            },
        ]})

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    if not effective_repair:
        result = qa.shots_prompt_qa_tool(
            "s", "cyclist", shots_data={"items": items, "consistency_rules": []},
            force=True, dry_run=True, global_max_repairs=0,
        )
        end_item = next(item for item in result["items"] if item["shot_type"] == "end")
        assert end_item["english_prompt"] == "Cyclist passes the window on the left"
        assert result["_qa_report"]["rendered_image_findings"][0]["shot_number"] == 1
        return

    result = qa.shots_prompt_qa_tool(
        "s", "cyclist", shots_data={"items": items, "consistency_rules": []},
        force=True, dry_run=True, global_max_repairs=0,
    )

    assert audit_calls, "rendered-frame visual audit was not requested"
    assert [payload["generated_shots"][0]["shot_number"] for _kwargs, payload in audit_calls] == [1, 2]
    assert all(kwargs.get("image_url") for kwargs, _payload in audit_calls), "actual rendered image was not attached to the audit"
    assert len(qa_calls) == 2
    assert all(call.get("image_url") for call in qa_calls), "actual rendered image was not attached to text QA"
    assert main_payloads[0]["rendered_image_findings"][0]["shot_number"] == 1
    end_item = next(item for item in result["items"] if item["shot_type"] == "end")
    assert "window shifted right" in end_item["english_prompt"]
