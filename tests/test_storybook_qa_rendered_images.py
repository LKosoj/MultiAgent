"""Existing shot QA must receive available renders, not just their text prompts."""
import base64
import io
import json

import pytest
from PIL import Image

from custom_tools.storybook import shots_prompt_qa as qa


@pytest.mark.parametrize("with_images", [True, False])
@pytest.mark.parametrize("reverse_items", [True, False])
def test_qa_receives_phase_images_and_original_brief(tmp_path, monkeypatch, with_images, reverse_items):
    base = tmp_path / "gate"
    (base / "91_screenplay").mkdir(parents=True)
    source = "The gate must close completely."
    continuity_facts = {"boundary_parts": ["moving leaf", "fixed glazed section"]}
    location = {"name": "Gate", "reference_image_path": "/references/locations/gate.png"}
    (base / "20_bible/references/locations").mkdir(parents=True)
    (base / "20_bible/locations.json").write_text(json.dumps([location]))
    Image.new("RGB", (800, 450), "blue").save(base / "20_bible/references/locations/gate.png")
    (base / "00_brief.json").write_text(json.dumps({"language": "ru", "storybook_prompt": source}))
    (base / "91_screenplay/screenplay.json").write_text(json.dumps({"screenplay": [{
        "scene_number": 1, "characters": [], "scene_continuity_facts": continuity_facts, "storyboard": [{
            "shot_number": 1, "camera_plan": "MEDIUM SHOT", "description": "Ворота закрываются",
        }],
    }]}))
    items = []
    for phase, color in [("start", "red"), ("end", "green")]:
        path = base / f"{phase}.png"
        if with_images:
            Image.new("RGB", (800, 450), color).save(path)
        items.append({"scene_number": 1, "shot_number": 1, "shot_type": phase,
                      "english_prompt": "Закрытые ворота", "negative_prompt": "blur, do not move background anchors, без замены фоновой архитектуры, deformed hands",
                      "video_prompt": "", "output_path": str(path),
                      "locations": [location], "reference_image_paths": [location["reference_image_path"]]})
    if reverse_items:
        items.reverse()
    outside = {"scene_number": 2, "shot_number": 1, "shot_type": "start",
               "english_prompt": "Unrelated scene", "negative_prompt": "без другого задника"}
    items.append(dict(outside))
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(qa, "_extract_shot_frame_spec_llm", lambda **kwargs: {"primary_subject": "gate"})
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        if "rendered-frame visual audit" in str(kwargs.get("system_prompt") or "").lower():
            return '{"violations": []}'
        return '{"repairs": []}'

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    result = qa.shots_prompt_qa_tool("s", "gate", shots_data={"items": items}, scene_numbers=[1], force=True, dry_run=True)
    main_calls = [call for call in calls if "rendered-frame visual audit" not in str(call.get("system_prompt") or "").lower()]
    assert len(calls) == (2 if with_images else 1)
    assert len(main_calls) == 1
    payload = json.loads(main_calls[0]["prompt"].split("INPUT:\n", 1)[1])
    assert payload["source_brief"] == source
    assert [item["shot_type"] for item in payload["generated_shots"]] == ["start", "end"]
    assert payload["generated_shots"][0]["negative_prompt"] == "blur, deformed hands"
    assert all(item["negative_prompt"] == "blur, deformed hands" for item in result["items"] if item["scene_number"] == 1)
    assert next(item for item in result["items"] if item["scene_number"] == 2) == outside
    if with_images:
        audit_payload = json.loads(calls[0]["prompt"].split("AUDIT_INPUT:\n", 1)[1])
        assert audit_payload["scene_continuity_facts"] == continuity_facts
        assert all(shot["negative_prompt"] == "blur, deformed hands" for shot in audit_payload["generated_shots"])
        url = calls[0]["image_url"]
        with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
            assert image.height == 936
            left = image.getpixel((image.width // 4, 234))
            right = image.getpixel((image.width * 3 // 4, 234))
            canonical = image.getpixel((image.width // 4, 702))
            assert left[0] > 200 and left[1] < 50
            assert right[1] > 80 and right[0] < 50
            assert canonical[2] > 200 and canonical[0] < 50
    else:
        assert not main_calls[0].get("image_url")


def test_visual_audit_receives_bound_styles_for_each_phase(tmp_path, monkeypatch):
    base = tmp_path / "style_audit"
    (base / "91_screenplay").mkdir(parents=True)
    (base / "30_style").mkdir()
    (base / "00_brief.json").write_text(json.dumps({"storybook_prompt": "A neutral hatch."}))
    (base / "91_screenplay/screenplay.json").write_text(json.dumps({"screenplay": [{
        "scene_number": 1,
        "characters": [],
        "storyboard": [{"shot_number": 1, "camera_plan": "MEDIUM SHOT", "description": "A hatch"}],
    }]}))
    (base / "30_style/image_styles.json").write_text(json.dumps({
        "default_style_id": "ink",
        "styles": {
            "ink": {"prompt": "INK READY STYLE"},
            "paper": {"prompt": "PAPER READY STYLE"},
            "unused": {"prompt": "UNUSED STYLE"},
        },
        "frame_bindings": {"1:1:start": "ink", "1:1:end": "paper"},
    }))
    items = []
    for phase, color in [("start", "red"), ("end", "green")]:
        path = base / f"{phase}.png"
        Image.new("RGB", (80, 45), color).save(path)
        items.append({
            "scene_number": 1,
            "shot_number": 1,
            "shot_type": phase,
            "style_id": "unused",
            "english_prompt": "A hatch, no appended style",
            "negative_prompt": "blur",
            "video_prompt": "",
            "output_path": str(path),
        })
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(qa, "_extract_shot_frame_spec_llm", lambda **kwargs: {"primary_subject": "hatch"})
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        if "rendered-frame visual audit" in str(kwargs.get("system_prompt") or "").lower():
            return '{"violations": []}'
        return '{"repairs": []}'

    monkeypatch.setattr(qa, "call_openai_api", fake_call)
    qa.shots_prompt_qa_tool("s", "style_audit", shots_data={"items": items}, force=True, dry_run=True)

    audit_call = next(call for call in calls if "rendered-frame visual audit" in str(call.get("system_prompt") or "").lower())
    main_call = next(call for call in calls if "rendered-frame visual audit" not in str(call.get("system_prompt") or "").lower())
    audit_payload = json.loads(audit_call["prompt"].split("AUDIT_INPUT:\n", 1)[1])
    main_payload = json.loads(main_call["prompt"].split("INPUT:\n", 1)[1])
    assert [shot["style_id"] for shot in audit_payload["generated_shots"]] == ["ink", "paper"]
    assert [shot["style_id"] for shot in main_payload["generated_shots"]] == ["ink", "paper"]
    assert audit_payload["image_styles"] == {"ink": "INK READY STYLE", "paper": "PAPER READY STYLE"}
    assert main_payload["image_styles"] == {"ink": "INK READY STYLE", "paper": "PAPER READY STYLE"}
    assert "UNUSED STYLE" not in json.dumps(audit_payload, ensure_ascii=False)
    assert "UNUSED STYLE" not in json.dumps(main_payload, ensure_ascii=False)
    assert all("READY STYLE" not in shot["english_prompt"] for shot in main_payload["generated_shots"])
