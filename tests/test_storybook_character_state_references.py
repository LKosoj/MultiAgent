import json
from pathlib import Path

import pytest
from PIL import Image

from custom_tools.storybook import character_state_references as cr


def _project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    base = tmp_path / "project"
    for name in ["97_shots", "91_screenplay", "20_bible/references/characters"]:
        (base / name).mkdir(parents=True, exist_ok=True)
    portrait = base / "20_bible/references/characters/actor.png"
    Image.new("RGB", (16, 16), "gray").save(portrait)
    actor = {"name": "Actor", "immutable_attributes": {"face_shape": "angular"},
             "variable_attributes": {"base_clothing": "coat"}, "reference_image_path": "/references/characters/actor.png"}
    (base / "20_bible/characters.json").write_text(json.dumps([actor]), encoding="utf-8")
    items = []
    for number, phase in enumerate(["base", "removed", "wounded", "fantasy", "base"], 1):
        items.append({"scene_number": number, "shot_number": 1, "shot_type": "start", "width": 1920, "height": 1088,
                      "reference_image_paths": ["/references/characters/actor.png", "/locations/keep.png"],
                      "characters": [{"name": "Actor", "reference_image_path": "/references/characters/actor.png", "outside": phase}],
                      "_shot_frame_spec": {"start_state_spec": {"must_show": [phase], "must_not_show": []}}})
    shots = {"items": items, "outside": {"keep": True}}
    (base / "97_shots/shots.json").write_text(json.dumps(shots), encoding="utf-8")
    (base / "91_screenplay/screenplay.json").write_text(json.dumps({"screenplay": [{"scene_number": n, "action": "context"} for n in range(1, 6)]}), encoding="utf-8")
    return base, items


def _plan():
    return {"characters": [{"name": "Actor", "base_frame_keys": ["1:1:start", "4:1:start", "5:1:start"], "states": [
        {"id": "coat_off", "parent_id": "base", "changes": "coat removed", "appearance": "Actor in shirt", "frame_keys": ["2:1:start"]},
        {"id": "wounded", "parent_id": "coat_off", "changes": "small wound", "appearance": "Actor in shirt with a small wound", "frame_keys": ["3:1:start"]},
    ]}]}


def test_variants_use_parent_and_explicit_return_to_base(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    base_bytes = (base / "20_bible/references/characters/actor.png").read_bytes()
    calls = []
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: json.dumps(_plan()))

    def edit(**kwargs):
        calls.append(kwargs)
        Image.new("RGB", (16, 16), "blue").save(kwargs["output_path"])

    monkeypatch.setattr(cr, "edit_image_vse_tool", edit)
    result = cr.storybook_character_state_references_tool("s", "project", {"items": items, "seed": 0, "outside": {"keep": True}})
    assert len(calls) == 2
    assert all(call["seed"] == 0 for call in calls)
    assert calls[1]["image_paths"] == [calls[0]["output_path"]]
    assert result["outside"] == {"keep": True}
    assert result["items"][0]["character_reference_states"]["Actor"]["state_id"] == "base"
    assert result["items"][3]["character_reference_states"]["Actor"]["state_id"] == "base"
    assert result["items"][2]["character_reference_states"]["Actor"]["state_id"] == "wounded"
    assert (base / "20_bible/references/characters/actor.png").read_bytes() == base_bytes
    assert "/locations/keep.png" in result["items"][0]["reference_image_paths"]


def test_cache_and_version_change(tmp_path, monkeypatch):
    _, items = _project(tmp_path, monkeypatch)
    count = {"planner": 0}
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: (count.__setitem__("planner", count["planner"] + 1) or json.dumps(_plan())))
    monkeypatch.setattr(cr, "edit_image_vse_tool", lambda **kwargs: Image.new("RGB", (16, 16)).save(kwargs["output_path"]))
    cr.storybook_character_state_references_tool("s", "project", {"items": items})
    cr.storybook_character_state_references_tool("s", "project", {"items": items})
    assert count["planner"] == 1
    monkeypatch.setattr(cr, "_CHARACTER_REFERENCE_PLAN_VERSION", cr._CHARACTER_REFERENCE_PLAN_VERSION + 1)
    cr.storybook_character_state_references_tool("s", "project", {"items": items})
    assert count["planner"] == 2


def test_failed_child_does_not_publish_and_resume_uses_cache(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: json.dumps(_plan()))
    calls = []

    def fail_second(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            Image.new("RGB", (16, 16)).save(kwargs["output_path"])

    monkeypatch.setattr(cr, "edit_image_vse_tool", fail_second)
    original = (base / "97_shots/shots.json").read_bytes()
    with pytest.raises(RuntimeError):
        cr.storybook_character_state_references_tool("s", "project", {"items": items})
    assert (base / "97_shots/shots.json").read_bytes() == original
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: pytest.fail("cached plan expected"))
    monkeypatch.setattr(cr, "edit_image_vse_tool", lambda **kwargs: Image.new("RGB", (16, 16)).save(kwargs["output_path"]))
    result = cr.storybook_character_state_references_tool("s", "project", {"items": items})
    assert result["items"][2]["character_reference_states"]["Actor"]["state_id"] == "wounded"


def test_state_ids_are_local_to_each_character():
    plan = {"characters": [
        {"name": name, "states": [{"id": "coat_off", "parent_id": "base", "changes": "coat removed",
          "appearance": "shirt", "frame_keys": ["1:1:end"]}]}
        for name in ["Actor", "Other"]
    ]}
    cr._validate_plan(plan, {"1:1:end": {}}, {("1:1:end", "Actor"), ("1:1:end", "Other")}, {"Actor", "Other"})


def test_artist_keeps_existing_project_appearance_reference(tmp_path, monkeypatch):
    from custom_tools.storybook import artist_batch_edit as artist

    base, items = _project(tmp_path, monkeypatch)
    variant = base / "20_bible/scene_character_references/version/characters/actor_shirt.png"
    variant.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), "blue").save(variant)
    before = variant.read_bytes()
    for item in items[:2]:
        item["project_id"] = "project"
        item["reference_image_paths"] = [str(variant)]
        item["characters"] = [{"name": "Actor", "reference_image_path": str(variant), "appearance_state": "shirt"}]
    calls = []
    monkeypatch.setattr(artist, "_smart_select_references_for_generation", lambda entity_type, entity, paths, max_count: paths)
    monkeypatch.setattr(artist, "_create_canon_reference", lambda **kwargs: calls.append(kwargs) or True)
    result = artist._preprocess_canon_references(items[:2], [], "s", 1)
    assert calls == [] and result == []
    assert variant.read_bytes() == before
