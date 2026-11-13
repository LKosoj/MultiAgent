import json
from pathlib import Path

import pytest
from PIL import Image

from custom_tools.storybook import character_state_references as cr


def _project(tmp_path, monkeypatch, sid=None):
    monkeypatch.setattr(cr, "translate_prompts_in_items", lambda item, language: item)
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    base = tmp_path / "project" / "series" / sid if sid else tmp_path / "project"
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
    # Ends of scenes 2 and 3 keep each planned state visible in two frames.
    items += [dict(items[number - 1], shot_type="end") for number in (2, 3)]
    shots = {"items": items, "outside": {"keep": True}}
    (base / "97_shots/shots.json").write_text(json.dumps(shots), encoding="utf-8")
    (base / "91_screenplay/screenplay.json").write_text(json.dumps({"screenplay": [{"scene_number": n, "action": "context"} for n in range(1, 6)]}), encoding="utf-8")
    return base, items


def _plan():
    return {"characters": [{"name": "Actor", "base_frame_keys": ["1:1:start", "4:1:start", "5:1:start"], "states": [
        {"id": "coat_off", "parent_id": "base", "changes": "coat removed", "appearance": "Actor in shirt", "frame_keys": ["2:1:start", "2:1:end"]},
        {"id": "wounded", "parent_id": "coat_off", "changes": "small wound", "appearance": "Actor in shirt with a small wound", "frame_keys": ["3:1:start", "3:1:end"]},
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
    assert all("exact original number of views and their layout" in call["prompt"] for call in calls)
    assert all("move or rotate the existing item while preserving its shape" in call["prompt"] for call in calls)
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


def test_invalid_planner_binding_is_repaired_once_with_original_payload(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    requests = []
    valid = _plan()
    invalid = {"characters": [{"name": "Actor", "base_frame_keys": ["unknown:frame"], "states": []}]}

    def planner(**kwargs):
        requests.append(kwargs)
        return json.dumps(invalid if len(requests) == 1 else valid)

    monkeypatch.setattr(cr, "call_openai_api", planner)
    monkeypatch.setattr(cr, "edit_image_vse_tool", lambda **kwargs: Image.new("RGB", (16, 16)).save(kwargs["output_path"]))
    result = cr.storybook_character_state_references_tool("s", "project", {"items": items, "seed": 0})
    assert result["items"]
    assert len(requests) == 2
    original_payload = json.loads(requests[0]["prompt"])
    repaired_payload = json.loads(requests[1]["prompt"])
    assert all(repaired_payload[key] == original_payload[key] for key in ("screenplay", "frames", "characters", "language"))
    assert repaired_payload["candidate"] == invalid
    assert "validation_error" in repaired_payload


def test_two_invalid_plans_abort_before_manifest_or_image_edit(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    shots_path = base / "97_shots/shots.json"
    before = shots_path.read_bytes()
    calls = []
    invalid = {"characters": [{"name": "Actor", "base_frame_keys": ["unknown:frame"], "states": []}]}
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: calls.append(kwargs) or json.dumps(invalid))
    monkeypatch.setattr(cr, "edit_image_vse_tool", lambda **kwargs: pytest.fail("Invalid plan must not edit images"))
    with pytest.raises(ValueError, match="Invalid or duplicate character frame binding"):
        cr.storybook_character_state_references_tool("s", "project", {"items": items, "seed": 0})
    assert len(calls) == 2
    assert shots_path.read_bytes() == before
    assert not (base / "97_shots/character_reference_states.json").exists()


def test_source_extension_reuses_existing_states_and_retries_bad_parent_plan(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    old_plan = _plan()
    requests = []
    responses = [old_plan]
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: requests.append(json.loads(kwargs["prompt"])) or json.dumps(responses.pop(0)))
    edits = []

    def edit(**kwargs):
        edits.append(kwargs)
        Image.new("RGB", (16, 16), "blue").save(kwargs["output_path"])

    monkeypatch.setattr(cr, "edit_image_vse_tool", edit)
    cr.storybook_character_state_references_tool("s", "project", {"items": items, "seed": 0})
    assert len(edits) == 2
    old_manifest = json.loads((base / "97_shots/character_reference_states.json").read_text())
    old_paths = {key: value["reference_image_path"] for key, value in old_manifest["states"].items()}
    screenplay = json.loads((base / "91_screenplay/screenplay.json").read_text())
    screenplay["screenplay"].append({"scene_number": 6, "action": "context"})
    (base / "91_screenplay/screenplay.json").write_text(json.dumps(screenplay))
    extra = dict(items[0], scene_number=6)
    items = items + [extra]
    (base / "97_shots/shots.json").write_text(json.dumps({"items": items}))
    bad = json.loads(json.dumps(old_plan))
    bad["characters"][0]["states"][0]["parent_id"] = "missing_parent"
    good = json.loads(json.dumps(old_plan))
    good["characters"][0]["base_frame_keys"].append("6:1:start")
    responses[:] = [bad, good]
    requests.clear()
    result = cr.storybook_character_state_references_tool("s", "project", {"items": items, "seed": 0})
    assert len(requests) == 2
    for request in requests:
        assert request["existing_plan"] == {"characters": old_manifest["characters"]}
        assert request["missing_pairs"] == [["6:1:start", "Actor"]]
        assert request["stale_pairs"] == []
    assert "coat_off" in requests[1]["validation_error"] and "parent" in requests[1]["validation_error"]
    assert len(edits) == 2
    manifest = json.loads((base / "97_shots/character_reference_states.json").read_text())
    assert manifest["input_hash"]
    assert set(manifest["states"]) == set(old_paths) and all(Path(path).read_bytes() for path in old_paths.values())
    assert result["items"][-1]["character_reference_states"]["Actor"]["state_id"] == "base"
    assert (base / "97_shots/character_reference_states.invalid.json").is_file()


def test_changed_parent_asset_reuses_it_and_rerenders_only_child(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    planner_calls = []
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: planner_calls.append(kwargs) or json.dumps(_plan()))
    edits = []

    def edit(**kwargs):
        edits.append(kwargs)
        Image.new("RGB", (16, 16), "blue").save(kwargs["output_path"])

    monkeypatch.setattr(cr, "edit_image_vse_tool", edit)
    cr.storybook_character_state_references_tool("s", "project", {"items": items, "seed": 0})
    manifest = json.loads((base / "97_shots/character_reference_states.json").read_text())
    parent_path = Path(manifest["states"]["Actor:coat_off"]["reference_image_path"])
    child_path = Path(manifest["states"]["Actor:wounded"]["reference_image_path"])
    Image.new("RGB", (16, 16), "green").save(parent_path)
    changed_parent = parent_path.read_bytes()
    edits_before = len(edits)
    cr.storybook_character_state_references_tool("s", "project", {"items": items, "seed": 0})
    assert len(planner_calls) == 1
    assert len(edits) == edits_before + 1
    assert parent_path.read_bytes() == changed_parent
    assert edits[-1]["output_path"] == str(child_path)
    assert edits[-1]["image_paths"] == [str(parent_path)]


def test_prop_geometry_update_does_not_replan_character_appearance(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: json.dumps(_plan()))
    monkeypatch.setattr(cr, "edit_image_vse_tool", lambda **kwargs: Image.new("RGB", (16, 16)).save(kwargs["output_path"]))
    cr.storybook_character_state_references_tool("s", "project", {"items": items})
    path = base / "91_screenplay/screenplay.json"
    screenplay = json.loads(path.read_text())
    screenplay["screenplay"][0]["scene_continuity_facts"] = {"continuity": {"geometry": "changed doorway"}}
    path.write_text(json.dumps(screenplay), encoding="utf-8")
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: pytest.fail("Appearance has not changed"))
    cr.storybook_character_state_references_tool("s", "project", {"items": items})
    assert json.loads(path.read_text()) == screenplay


def test_replaces_retired_reference_without_resolving_it_as_an_input(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    retired = str(base / 'shared/references/characters/retired.png')
    for item in items:
        item['characters'][0]['reference_image_path'] = retired
        item['reference_image_paths'][0] = retired
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    resolve = cr._reference_path
    def reject_retired(base, value):
        if value == retired:
            raise ValueError('Reference is not bound to series')
        return resolve(base, value)
    monkeypatch.setattr(cr, '_reference_path', reject_retired)
    monkeypatch.setattr(cr, 'call_openai_api', lambda **kwargs: json.dumps(_plan()))
    monkeypatch.setattr(cr, 'edit_image_vse_tool', lambda **kwargs: Image.new('RGB', (16, 16)).save(kwargs['output_path']))
    result = cr.storybook_character_state_references_tool('s', 'project', {'items': items})
    assert all(retired not in item['reference_image_paths'] for item in result['items'])
    assert all(item['characters'][0]['reference_image_path'] != retired for item in result['items'])


def test_bad_appearance_does_not_publish_shots_or_replace_approved_source(tmp_path, monkeypatch):
    from custom_tools.storybook import artist_batch_edit
    base, items = _project(tmp_path, monkeypatch)
    card = json.loads((base / '20_bible/characters.json').read_text())
    card[0]['reference_requirements'] = ['Preserve the original artistic technique and sheet layout.']
    (base / '20_bible/characters.json').write_text(json.dumps(card))
    before = (base / '97_shots/shots.json').read_bytes()
    source = (base / '20_bible/references/characters/actor.png').read_bytes()
    calls, reviews = [], []
    monkeypatch.setattr(cr, 'call_openai_api', lambda **kwargs: json.dumps(_plan()))
    def edit(**kwargs):
        calls.append(kwargs)
        Image.new('RGB', (16, 16)).save(kwargs['output_path'])
    def review(path, data, source_image_path=None):
        reviews.append(data)
        assert source_image_path == str(base / '20_bible/references/characters/actor.png')
        return False
    monkeypatch.setattr(cr, 'edit_image_vse_tool', edit)
    monkeypatch.setattr(artist_batch_edit, '_reference_requirements_met', review)
    with pytest.raises(RuntimeError, match='Character appearance reference failed requirements'):
        cr.storybook_character_state_references_tool('s', 'project', {'items': items})
    assert 'Do not add panels' in calls[0]['prompt']
    assert 'coat removed' in reviews[0]['reference_requirements'][0]
    assert 'source is already approved' in reviews[0]['reference_requirements'][1]
    assert 'original view count and composition' in reviews[0]['reference_requirements'][1]
    assert not Path(calls[0]['output_path']).exists()
    assert (base / '97_shots/shots.json').read_bytes() == before
    assert (base / '20_bible/references/characters/actor.png').read_bytes() == source


def test_appearance_canvas_inherits_parent_reference_not_scene_frame(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    Image.new('RGB', (80, 48)).save(base / '20_bible/references/characters/actor.png')
    monkeypatch.setattr(cr, 'call_openai_api', lambda **kwargs: json.dumps(_plan()))
    calls = []
    def edit(**kwargs):
        with Image.open(kwargs['image_paths'][0]) as parent:
            assert (kwargs['width'], kwargs['height']) == parent.size
        calls.append(kwargs)
        Image.new('RGB', (kwargs['width'], kwargs['height'])).save(kwargs['output_path'])
    monkeypatch.setattr(cr, 'edit_image_vse_tool', edit)
    cr.storybook_character_state_references_tool('s', 'project', {'items': items})
    assert [(call['width'], call['height']) for call in calls] == [(80, 48), (80, 48)]
    assert all((item['width'], item['height']) == (1920, 1088) for item in items)


def test_single_frame_state_is_text_over_parent_image(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    plan = {"characters": [{"name": "Actor", "base_frame_keys": ["1:1:start", "4:1:start", "5:1:start", "3:1:end"], "states": [
        {"id": "dusty_glasses", "parent_id": "base", "changes": "dusty glasses", "appearance": "Actor with dusty glasses", "frame_keys": ["2:1:start"]},
        {"id": "dusty", "parent_id": "dusty_glasses", "changes": "dust on coat", "appearance": "Actor dusty all over", "frame_keys": ["2:1:end", "3:1:start"]},
    ]}]}
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: json.dumps(plan))
    calls = []

    def edit(**kwargs):
        calls.append(kwargs)
        Image.new("RGB", (16, 16), "blue").save(kwargs["output_path"])

    monkeypatch.setattr(cr, "edit_image_vse_tool", edit)
    result = cr.storybook_character_state_references_tool("s", "project", {"items": items, "seed": 0})
    portrait = str(base / "20_bible/references/characters/actor.png")
    assert len(calls) == 1
    assert calls[0]["image_paths"] == [portrait]
    assert "dusty glasses" in calls[0]["prompt"] and "dust on coat" in calls[0]["prompt"]
    single = result["items"][1]["characters"][0]
    assert result["items"][1]["character_reference_states"]["Actor"]["state_id"] == "dusty_glasses"
    assert single["reference_image_path"] == portrait
    assert single["appearance_state"] == "Actor with dusty glasses"
    dusty = result["items"][5]["character_reference_states"]["Actor"]
    assert dusty["state_id"] == "dusty" and dusty["reference_image_path"] != portrait


def test_manifest_with_old_paths_is_reusable_after_files_moved(tmp_path, monkeypatch):
    from custom_tools.storybook import reference_catalog as catalog
    from custom_tools.storybook.project_paths import series_scope
    from custom_tools.storybook.studio.capabilities import invoke
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    pid = invoke("projects_create", {"title": "project", "description": "Shared world", "genre": "comedy",
                                     "target_age": "all"})["project_id"]
    sid = invoke("series_create", {"project_id": pid, "title": "One", "source_material": "",
                                   "participant_ids": [], "settings": {}})["series_id"]
    base, items = _project(tmp_path, monkeypatch, sid)
    old_portrait = str(base / "20_bible/references/characters/actor.png")
    catalog.store_reference(base, old_portrait, "character", "actor")
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: json.dumps(_plan()))
    outputs = []
    monkeypatch.setattr(cr, "edit_image_vse_tool", lambda **kwargs: (
        outputs.append(kwargs["output_path"]), Image.new("RGB", (16, 16), "blue").save(kwargs["output_path"])))
    with series_scope(pid, sid):
        cr.storybook_character_state_references_tool("s", pid, {"items": items})
        assert len(outputs) == 2
        # Манифест прежней версии хранит старые пути, файлы уже перенесены.
        manifest_path = base / "97_shots/character_reference_states.json"
        manifest = json.loads(manifest_path.read_text())
        old_by_parent = {"coat_off": old_portrait, "wounded": outputs[0]}
        for record, old_output in zip(manifest["states"].values(), outputs):
            record["reference_image_path"] = old_output
            record["reference_image_paths"] = [old_by_parent[record["state_id"]]]
        manifest_path.write_text(json.dumps(manifest))
        monkeypatch.setattr(cr, "reuse_reference", lambda *args, **kwargs: None)
        cr.storybook_character_state_references_tool("s", pid, {"items": items})
    assert len(outputs) == 2


def test_alias_character_path_is_replaced_by_variant_in_frame_refs(tmp_path, monkeypatch):
    from custom_tools.storybook import reference_catalog as catalog
    from custom_tools.storybook.project_paths import series_scope
    from custom_tools.storybook.studio.capabilities import invoke
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    pid = invoke("projects_create", {"title": "project", "description": "Shared world", "genre": "comedy",
                                     "target_age": "all"})["project_id"]
    sid = invoke("series_create", {"project_id": pid, "title": "One", "source_material": "",
                                   "participant_ids": [], "settings": {}})["series_id"]
    base, items = _project(tmp_path, monkeypatch, sid)
    moved = catalog.store_reference(base, base / "20_bible/references/characters/actor.png", "character", "actor")
    # В кадре лежит уже разрешённый путь, у персонажа остался алиас /references/...
    for item in items:
        item["reference_image_paths"] = [moved, "/locations/keep.png"]
    (base / "97_shots/shots.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: json.dumps(_plan()))
    monkeypatch.setattr(cr, "edit_image_vse_tool", lambda **kwargs: Image.new("RGB", (16, 16), "blue").save(kwargs["output_path"]))
    with series_scope(pid, sid):
        result = cr.storybook_character_state_references_tool("s", pid, {"items": items})
    refs = result["items"][1]["reference_image_paths"]
    assert moved not in refs and len(refs) == 2


def test_moving_portrait_to_shared_does_not_replan(tmp_path, monkeypatch):
    from custom_tools.storybook import background_references as backgrounds
    base, items = _project(tmp_path, monkeypatch)
    old = base / "20_bible/references/characters/actor.png"
    new = tmp_path / "shared/actor.png"
    moved = {"value": False}
    # Путь-кандидат остаётся прежним, разрешённый путь переезжает в общий каталог.
    resolve = lambda root, value: (old, new if moved["value"] else old)
    monkeypatch.setattr(backgrounds, "_reference_candidate", resolve)
    monkeypatch.setattr(cr, "_reference_candidate", resolve)
    count = {"planner": 0}
    monkeypatch.setattr(cr, "call_openai_api", lambda **kwargs: (count.__setitem__("planner", count["planner"] + 1) or json.dumps(_plan())))
    monkeypatch.setattr(cr, "edit_image_vse_tool", lambda **kwargs: Image.new("RGB", (16, 16)).save(kwargs["output_path"]))
    cr.storybook_character_state_references_tool("s", "project", {"items": items})
    new.parent.mkdir(parents=True)
    old.rename(new)
    moved["value"] = True
    cr.storybook_character_state_references_tool("s", "project", {"items": items})
    assert count["planner"] == 1
