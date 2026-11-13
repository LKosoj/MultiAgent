import json
import threading
from pathlib import Path

import pytest
from PIL import Image

from custom_tools.storybook import background_references as bg


def _run_reference_tools(session_id, project_id, shots_data=None):
    planned = bg.storybook_background_plan_tool(session_id, project_id, shots_data)
    return bg.storybook_background_references_tool(session_id, project_id, planned)


def test_location_plans_and_images_run_in_parallel_without_splitting_returns(tmp_path, monkeypatch):
    base, route, _ = _project(tmp_path, monkeypatch)
    rooms = [{"name": f"Room {n}", "reference_image_path": f"/references/locations/room_{n}.png"}
             for n in range(3)]
    for room in rooms:
        Image.new("RGB", (16, 16), "red").save(base / "20_bible" / room["reference_image_path"].lstrip("/"))
    locations = json.loads((base / "20_bible/locations.json").read_text())
    (base / "20_bible/locations.json").write_text(json.dumps(locations + rooms))
    room_frames = [dict(route[0], scene_number=2, shot_number=n + 1, locations=[room])
                   for n, room in enumerate(rooms)]
    selected = [route[0], *room_frames, route[-1]]
    (base / "97_shots/shots.json").write_text(json.dumps({"items": selected}))
    planning_barrier = threading.Barrier(4)
    image_barrier = threading.Barrier(4)
    inputs = []

    def planner(**kwargs):
        payload = json.loads(kwargs["prompt"])
        inputs.append(payload)
        planning_barrier.wait(timeout=5)
        return json.dumps({"spaces": [{"name": next(iter(payload["locations"])), "layout": "One place",
            "cards": [{"frame_key": key, "description": "One unchanged view"} for key in payload["frames"]]}], "skipped": []})

    def edit(**kwargs):
        assert kwargs["seed"] == 0
        image_barrier.wait(timeout=5)
        Image.new("RGB", (16, 16), "gray").save(kwargs["output_path"])

    monkeypatch.setattr(bg, "call_openai_api", planner)
    monkeypatch.setattr(bg, "edit_image_vse_tool", edit)
    result = _run_reference_tools("s", "project", {"items": selected, "seed": 0})
    yard = next(payload for payload in inputs if "Yard" in payload["locations"])
    assert list(yard["frames"]) == [bg._frame_key(route[0]), bg._frame_key(route[-1])]
    assert [scene["scene_number"] for scene in yard["screenplay"]] == [1, 3]
    assert len(yard["locations"]) == 1
    assert len(inputs) == 4
    assert result["items"][0]["background_reference"] == result["items"][-1]["background_reference"]
    assert result["items"][1]["background_reference"]["sheet_path"] != result["items"][0]["background_reference"]["sheet_path"]


def test_overlapping_location_references_keep_one_planning_group():
    frames = {"1:1:start": {"locations": ["A"]}, "2:1:start": {"locations": ["B"]},
              "3:1:start": {"locations": ["A", "B"]}}
    inputs = bg._location_planning_inputs({"frames": frames, "locations": {"A": {}, "B": {}}, "screenplay": [], "language": "ru"})
    assert len(inputs) == 1 and set(inputs[0]["frames"]) == set(frames)


def test_explicit_spatial_layout_is_inherited_without_model_additions():
    plan = {'spaces': [{'layout': 'Invented arrangement from a photograph',
                        'cards': [{'frame_key': '1:1:start'}]}]}
    payload = {'frames': {'1:1:start': {'locations': ['Place']}},
               'locations': {'Place': {'spatial_layout': 'Canonical arrangement'}}}
    bg._inherit_spatial_layout(plan, payload)
    assert plan['spaces'][0]['layout'] == 'Place: Canonical arrangement'


def test_inferred_spatial_layout_remains_when_no_explicit_layout_exists():
    plan = {'spaces': [{'layout': 'Inferred arrangement', 'cards': [{'frame_key': '1:1:start'}]}]}
    bg._inherit_spatial_layout(plan, {'frames': {'1:1:start': {'locations': ['Place']}},
                                    'locations': {'Place': {}}})
    assert plan['spaces'][0]['layout'] == 'Inferred arrangement'


def test_location_planning_keeps_only_its_bound_styles():
    payload = {'frames': {'1:1:start': {'locations': ['Place'], 'style_id': 'current'}},
               'screenplay': [], 'locations': {'Place': {}}, 'language': 'en',
               'styles': {'current': {'prompt': 'Ink on paper'}, 'other': {'prompt': 'Another style'}}}
    result = bg._location_planning_inputs(payload)[0]
    assert result['styles'] == {'current': {'prompt': 'Ink on paper'}}


def _project(tmp_path, monkeypatch):
    monkeypatch.setattr(bg, '_review_background_plan', lambda *args: None)
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    base = tmp_path / "project"
    for name in ["97_shots", "91_screenplay", "20_bible/references/locations", "30_style"]:
        (base / name).mkdir(parents=True, exist_ok=True)
    canonical = base / "20_bible/references/locations/yard.png"
    Image.new("RGB", (16, 16), "gray").save(canonical)
    location = {"name": "Yard", "reference_image_path": "/references/locations/yard.png"}
    (base / "20_bible/locations.json").write_text(json.dumps([location]))
    (base / "30_style/image_styles.json").write_text(json.dumps({
        "default_style_id": "ink", "styles": {"ink": {"prompt": "Ink on paper"}},
    }))
    route = [{
        "project_id": "project", "scene_number": 1 if n < 7 else 3,
        "shot_number": n + 1 if n < 7 else n - 6, "shot_type": "start",
        "camera_plan": "TRACKING", "locations": [location], "style_id": "ink",
        "english_prompt": f"Frame {n}", "width": 1920, "height": 1088,
    } for n in range(13)]
    other = {"scene_number": 2, "shot_number": 1, "shot_type": "start", "english_prompt": "Untouched"}
    (base / "97_shots/shots.json").write_text(json.dumps({"items": route + [other]}))
    (base / "91_screenplay/screenplay.json").write_text(json.dumps({"screenplay": [
        {"scene_number": 1, "action": "Travel through the yard", "storyboard": []},
        {"scene_number": 2, "action": "An unrelated memory elsewhere", "storyboard": []},
        {"scene_number": 3, "action": "Continue the same travel", "storyboard": []},
    ]}))
    plan = {"spaces": [{"name": "Yard", "layout": "Facade and gateway", "cards": [
        {"frame_key": bg._frame_key(item), "description": f"View {n}"}
        for n, item in reversed(list(enumerate(route)))
    ]}], "skipped": []}
    return base, route, plan


def test_planning_finishes_without_rendering_or_publishing(tmp_path, monkeypatch):
    base, route, plan = _project(tmp_path, monkeypatch)
    before = (base / "97_shots/shots.json").read_bytes()
    monkeypatch.setattr(bg, "call_openai_api", lambda **kwargs: json.dumps(plan))
    monkeypatch.setattr(bg, "edit_image_vse_tool", lambda **kwargs: pytest.fail("Planning must not render"))
    monkeypatch.setattr(bg, "generate_image_tool", lambda **kwargs: pytest.fail("Planning must not render"))
    result = bg.storybook_background_plan_tool("s", "project", {"items": route})
    manifest = json.loads((base / "20_bible/references/background_references.json").read_text())
    assert all(Path(sheet["path"]).is_relative_to(base / "20_bible/references/background_references") for sheet in manifest["sheets"])
    assert result["_background_plan_hash"] == manifest["input_hash"]
    assert (base / "97_shots/shots.json").read_bytes() == before
    assert not any(Path(sheet["path"]).exists() for sheet in manifest["sheets"])


def test_planner_sees_each_location_reference_image(tmp_path, monkeypatch):
    import base64

    base, route, plan = _project(tmp_path, monkeypatch)
    second = base / "20_bible/references/locations/room.png"
    Image.new("RGB", (16, 16), "blue").save(second)
    locations_path = base / "20_bible/locations.json"
    locations = json.loads(locations_path.read_text())
    locations.append({"name": "Room", "reference_image_path": "/references/locations/room.png"})
    locations_path.write_text(json.dumps(locations))
    route[0]["locations"] = locations
    requests = []

    def capture(**kwargs):
        requests.append(kwargs)
        return json.dumps(plan)

    monkeypatch.setattr(bg, "call_openai_api", capture)
    bg.storybook_background_plan_tool("s", "project", {"items": route})
    assert len(requests) == 1
    assert requests[0].get("image_url") is None
    content = requests[0]['prompt']
    assert 'Yard' in content[1]['text'] and 'Room' in content[3]['text']
    assert base64.b64decode(content[2]['image_url']['url'].split(',', 1)[1]) == (base / '20_bible/references/locations/yard.png').read_bytes()
    assert base64.b64decode(content[4]['image_url']['url'].split(',', 1)[1]) == second.read_bytes()


def test_single_location_reference_reaches_planner_without_downsampling(tmp_path, monkeypatch):
    import base64

    base, route, plan = _project(tmp_path, monkeypatch)
    reference = base / "20_bible/references/locations/yard.png"
    Image.new("RGB", (1920, 1088), "gray").save(reference)
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        return json.dumps(plan)

    monkeypatch.setattr(bg, "call_openai_api", capture)
    bg.storybook_background_plan_tool("s", "project", {"items": route})
    assert base64.b64decode(calls[0]["image_url"].split(",", 1)[1]) == reference.read_bytes()


def test_only_identical_views_inside_same_space_are_reused():
    spaces = [{"name": "Yard", "cards": [
        {"frame_key": "1:1:start", "description": "Gateway at right edge"},
        {"frame_key": "1:1:end", "description": "Gateway at left edge after camera travel"},
        {"frame_key": "3:1:start", "description": "Gateway at right edge"}]},
        {"name": "Other yard", "cards": [{"frame_key": "2:1:start", "description": "Gateway at right edge"}]}]
    frames = {key: {"style_id": "ink"} for key in ["1:1:start", "1:1:end", "3:1:start", "2:1:start"]}
    result = bg._group_identical_background_views(spaces, frames)
    assert [card["frame_keys"] for card in result[0]["cards"]] == [["1:1:start", "3:1:start"], ["1:1:end"]]
    assert result[1]["cards"][0]["frame_keys"] == ["2:1:start"]
    frames["3:1:start"]["style_id"] = "watercolor"
    result = bg._group_identical_background_views(spaces, frames)
    assert len(result[0]["cards"]) == 3


def test_interrupted_route_splits_at_twelve_and_continues_from_previous_sheet(tmp_path, monkeypatch):
    base, route, plan = _project(tmp_path, monkeypatch)
    planner_calls, image_calls = [], []

    def planner(**kwargs):
        payload = json.loads(kwargs["prompt"])
        assert [s["scene_number"] for s in payload["screenplay"]] == [1, 3]
        planner_calls.append(kwargs)
        return json.dumps(plan)

    def edit(**kwargs):
        image_calls.append(kwargs)
        expected = (1920, 816) if kwargs['output_path'].endswith('sheet_01.png') else (1920, 1088)
        assert (kwargs["width"], kwargs["height"]) == expected
        assert all(Path(path).is_file() for path in kwargs["image_paths"])
        Image.new("RGB", (16, 16), "blue").save(kwargs["output_path"])
        # A concurrent change outside the requested frames must survive persistence.
        path = base / "97_shots/shots.json"
        current = json.loads(path.read_text())
        current["items"][-1]["english_prompt"] = "Edited during generation"
        path.write_text(json.dumps(current))

    monkeypatch.setattr(bg, "call_openai_api", planner)
    monkeypatch.setattr(bg, "edit_image_vse_tool", edit)
    result = _run_reference_tools("s", "project", {"items": route})
    manifest = json.loads((base / "20_bible/references/background_references.json").read_text())
    assert [sheet["card_count"] for sheet in manifest["sheets"]] == [12, 1]
    assert image_calls[1]["image_paths"][0] == image_calls[0]["output_path"]
    assert len(result["items"]) == 14
    first = result["items"][0]["background_reference"]
    last = result["items"][12]["background_reference"]
    assert manifest["sheets"][0]["space_id"] == manifest["sheets"][1]["space_id"]
    assert first["sheet_path"] != last["sheet_path"]
    assert first["card_number"] == 1
    assert first["sheet_path"] == image_calls[1]["output_path"]
    assert last["sheet_path"] == image_calls[0]["output_path"]
    assert result["items"][-1] == {
        "scene_number": 2, "shot_number": 1, "shot_type": "start",
        "english_prompt": "Edited during generation",
    }
    _run_reference_tools("s", "project", {"items": route})
    assert len(planner_calls) == 1 and len(image_calls) == 2


def test_failed_continuation_does_not_publish_missing_reference(tmp_path, monkeypatch):
    base, route, plan = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(bg, "call_openai_api", lambda **kw: json.dumps(plan))
    original = (base / "97_shots/shots.json").read_bytes()
    calls = []

    def edit(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            Image.new("RGB", (16, 16), "blue").save(kwargs["output_path"])

    monkeypatch.setattr(bg, "edit_image_vse_tool", edit)
    with pytest.raises(RuntimeError, match="not generated"):
        _run_reference_tools("s", "project", {"items": route})
    assert (base / "97_shots/shots.json").read_bytes() == original
    monkeypatch.setattr(bg, "call_openai_api", lambda **kw: pytest.fail("Existing plan should resume"))
    monkeypatch.setattr(bg, "edit_image_vse_tool", lambda **kw: Image.new("RGB", (16, 16), "blue").save(kw["output_path"]))
    result = _run_reference_tools("s", "project", {"items": route})
    assert len([item for item in result["items"] if item.get("background_reference")]) == 13


def test_explicitly_skipped_environment_creates_no_sheets(tmp_path, monkeypatch):
    base, route, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(bg, "call_openai_api", lambda **kw: json.dumps({
        "spaces": [], "skipped": [{"frame_key": bg._frame_key(item), "reason": "Object macro without environment"} for item in route],
    }))
    monkeypatch.setattr(bg, "edit_image_vse_tool", lambda **kw: pytest.fail("Static episode needs no image"))
    result = _run_reference_tools("s", "project", {"items": route})
    assert not any(item.get("background_reference") for item in result["items"])


def test_larger_sequence_continues_each_sheet_not_only_the_first():
    frames = {f"1:{n}:start": {"style_id": "ink", "locations": [], "width": 1920, "height": 1088}
              for n in range(1, 26)}
    sequence = {"cards": [{"frame_keys": [key], "description": key} for key in frames]}
    sheets, bindings = bg._build_background_sheets([sequence], frames, {"ink": {"prompt": "Ink"}}, Path("out"))
    assert [sheet["card_count"] for sheet in sheets] == [12, 12, 1]
    assert sheets[2]["previous_sheet"] == sheets[1]["path"]
    assert bindings["1:25:start"]["card_number"] == 1


def test_portrait_background_cards_keep_their_aspect_ratio_in_sheet():
    frames = {f'1:{n}:start': {'style_id': 'main', 'locations': [], 'width': 1080, 'height': 1920}
              for n in (1, 2)}
    space = {'cards': [{'frame_keys': [key], 'description': key} for key in frames]}
    sheets, _ = bg._build_background_sheets([space], frames, {'main': {'prompt': 'Photorealism'}}, Path('out'))
    sheet = sheets[0]
    assert sheet['width'] / sheet['height'] == pytest.approx(2 * 9 / 16, abs=0.002)
    assert 'вид 9:16' in sheet['prompt']


@pytest.mark.parametrize("phase, expected", [("start", "intact"), ("end", "broken")])
def test_planner_receives_current_phase_not_shared_final_state(tmp_path, monkeypatch, phase, expected):
    base, route, _ = _project(tmp_path, monkeypatch)
    route[0]["shot_type"] = phase
    route[0]["spatial_composition"] = "irrelevant detailed actor pose " * 100
    route[0]["final_spatial_composition"] = "irrelevant detailed actor pose " * 100
    route[0]["_shot_frame_spec"] = {
        "transition_spec": {"camera_delta": "Lateral camera travel with unchanged subject scale",
                            "environment_delta": ["The gateway projects farther left"],
                            "subject_delta": ["irrelevant detailed actor pose"]},
        "world_physics": {"surface_state": "broken"},
        "start_state_spec": {"world_physics": {"surface_state": "intact", "support_state": "subject stands outside on the ground", "occlusion_constraints": ["interior visible"]}, "must_show": ["irrelevant detailed actor pose"]},
        "end_state_spec": {"world_physics": {"surface_state": "broken", "support_state": "subject stands outside on the ground", "occlusion_constraints": ["interior hidden"]}, "must_not_show": ["irrelevant detailed actor pose"]},
    }

    class InputCaptured(Exception):
        pass

    def capture(**kwargs):
        frame = json.loads(kwargs["prompt"])["frames"][bg._frame_key(route[0])]
        assert frame["environment_state"] == expected
        assert frame["support"] == "subject stands outside on the ground"
        assert frame["visibility_constraints"] == ["interior visible" if phase == "start" else "interior hidden"]
        assert frame["camera_delta"] == "Lateral camera travel with unchanged subject scale"
        assert frame["environment_delta"] == ["The gateway projects farther left"]
        assert "irrelevant detailed actor pose" not in kwargs["prompt"]
        raise InputCaptured

    monkeypatch.setattr(bg, "call_openai_api", capture)
    with pytest.raises(InputCaptured):
        _run_reference_tools("s", "project", {"items": route})


def test_small_spaces_are_not_combined_to_fill_twelve():
    frames = {f"1:{n}:start": {"style_id": "ink", "locations": [], "width": 1920, "height": 1088}
              for n in range(1, 7)}
    keys = list(frames)
    spaces = [{"name": "Room", "cards": [{"frame_keys": keys[:2], "description": "One static view"}]},
              {"name": "Street", "cards": [{"frame_keys": [key], "description": key} for key in keys[2:]]}]
    sheets, bindings = bg._build_background_sheets(spaces, frames, {"ink": {"prompt": "Ink"}}, Path("out"))
    assert [sheet["card_count"] for sheet in sheets] == [1, 4]
    assert all(sheet["previous_sheet"] is None for sheet in sheets)
    assert bindings[keys[0]] == bindings[keys[1]]
    assert bindings[keys[1]]["sheet_path"] != bindings[keys[2]]["sheet_path"]


def test_missing_frame_needs_explicit_skip():
    frames = {"1:1:start": {"style_id": "ink", "locations": [], "width": 1920, "height": 1088}}
    with pytest.raises(ValueError, match="coverage"):
        bg._build_background_sheets([], frames, {"ink": {"prompt": "Ink"}}, Path("out"))


def test_landmark_states_separate_views_and_sheets_without_fragmenting_props(tmp_path):
    def frame(state, visible=True, prop='a'):
        return {'style_id': 'ink', 'width': 100, 'height': 100, 'locations': [],
                'continuity': {'entities': [{'id': 'window', 'kind': 'landmark', 'geometry': 'window'},
                                            {'id': 'prop', 'kind': 'prop'}],
                               'states': {'window': {'id': state, 'visible': visible}, 'prop': {'id': prop}}}}
    frames = {'a': frame('intact'), 'b': frame('broken'), 'c': frame('intact', False, 'b')}
    views = [{'name': 'place', 'cards': [{'frame_key': key, 'description': 'same view'} for key in frames]}]
    grouped = bg._group_identical_background_views(views, frames)
    assert [card['frame_keys'] for card in grouped[0]['cards']] == [['a', 'c'], ['b']]
    mixed = [{'name': 'place', 'cards': [{'frame_keys': ['a', 'b', 'c'], 'description': 'same view'}]}]
    sheets, bindings = bg._build_background_sheets(mixed, frames, {'ink': {'prompt': 'ink'}}, tmp_path)
    assert len(sheets) == 2
    assert bindings['a'] == bindings['c']
    assert bindings['a']['sheet_path'] != bindings['b']['sheet_path']
    assert all(sheet['previous_sheet'] is None for sheet in sheets)


def test_sheet_chain_stays_within_state_and_missing_continuity_is_unchanged(tmp_path):
    frames = {}
    cards = []
    for number in range(27):
        key = str(number)
        frame = {'style_id': 'ink', 'width': 100, 'height': 100, 'locations': []}
        if number < 14:
            frame['continuity'] = {'entities': [{'id': 'landmark', 'kind': 'landmark', 'geometry': 'shape'}],
                                   'states': {'landmark': {'id': 'one' if number < 13 else 'two'}}}
        frames[key] = frame
        cards.append({'frame_keys': [key], 'description': key})
    sheets, bindings = bg._build_background_sheets([{'cards': cards}], frames, {'ink': {'prompt': 'ink'}}, tmp_path)
    assert [sheet['card_count'] for sheet in sheets] == [12, 1, 1, 12, 1]
    assert sheets[1]['previous_sheet'] == sheets[0]['path']
    assert sheets[2]['previous_sheet'] is None
    assert sheets[3]['previous_sheet'] is None
    assert sheets[4]['previous_sheet'] == sheets[3]['path']
    assert len({sheet['path'] for sheet in sheets}) == 5
    assert len(bindings) == 27


def test_background_render_reports_provider_failure(tmp_path, monkeypatch):
    _, route, plan = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(bg,'call_openai_api',lambda **_:json.dumps(plan))
    planned=bg.storybook_background_plan_tool('s','project',{'items':route})
    monkeypatch.setattr(bg,'edit_image_vse_tool',lambda **_:'image provider rejected the requested size')
    with pytest.raises(RuntimeError,match='image provider rejected the requested size'):
        bg.storybook_background_references_tool('s','project',planned)


def test_background_prop_error_is_repaired_before_plan_is_saved(tmp_path, monkeypatch):
    import copy
    from custom_tools.storybook import continuity
    base, route, plan = _project(tmp_path, monkeypatch)
    prop = {'id': 'portable-id', 'kind': 'prop', 'name': 'Portable object'}
    monkeypatch.setattr(continuity, 'frame_continuity', lambda *args: {'entities': [prop], 'states': {}})
    bad = copy.deepcopy(plan)
    bad['spaces'][0]['cards'][0]['description'] = 'Portable object (portable-id) fills the foreground'
    repaired = copy.deepcopy(plan)
    skipped_key = repaired['spaces'][0]['cards'].pop(0)['frame_key']
    repaired['skipped'] = [{'frame_key': skipped_key, 'reason': 'Object macro without a separate background'}]
    requests = []
    def planner(**kwargs):
        payload = json.loads(kwargs['prompt'])
        requests.append(payload)
        return json.dumps(bad if len(requests) == 1 else repaired)
    monkeypatch.setattr(bg, 'call_openai_api', planner)
    bg.storybook_background_plan_tool('s', 'project', {'items': route})
    assert len(requests) == 2
    assert requests[1]['candidate'] == bad
    assert 'portable-id' in requests[1]['validation_error']
    saved = json.loads((base / '20_bible/references/background_references.json').read_text())
    assert saved['skipped'] == repaired['skipped']
    assert skipped_key not in saved['frame_bindings']
    assert 'portable-id' not in json.dumps(saved['spaces'])


def test_persistent_background_prop_error_does_not_save_a_plan(tmp_path, monkeypatch):
    from custom_tools.storybook import continuity
    base, route, plan = _project(tmp_path, monkeypatch)
    prop = {'id': 'portable-id', 'kind': 'prop', 'name': 'Portable object'}
    monkeypatch.setattr(continuity, 'frame_continuity', lambda *args: {'entities': [prop], 'states': {}})
    plan['spaces'][0]['layout'] = 'A portable-id as part of the environment'
    requests = []
    monkeypatch.setattr(bg, 'call_openai_api', lambda **kwargs: requests.append(kwargs) or json.dumps(plan))
    with pytest.raises(ValueError, match='portable prop portable-id'):
        bg.storybook_background_plan_tool('s', 'project', {'items': route})
    assert len(requests) == 3
    assert not (base / '20_bible/references/background_references.json').exists()
    assert not list((base / '20_bible/references/background_references').rglob('location_*.json'))


def test_background_semantic_review_rejects_foreground_even_without_entity_id(monkeypatch):
    received = []
    monkeypatch.setattr(bg, 'call_openai_api', lambda **kwargs: received.append(kwargs) or
                        json.dumps({'issues': ['1:1:start: clothing belongs to the actor, not the background']}))
    plan = {'spaces': [{'cards': [{'frame_key': '1:1:start', 'description': 'Clothing fills the background'}]}]}
    payload = {'locations': {'Place': {'spatial_layout': 'One continuous physical place'}}, 'frames': {}}
    with pytest.raises(ValueError, match='clothing belongs to the actor'):
        bg._review_background_plan(plan, payload)
    assert json.loads(received[0]['prompt']) == {'source': payload, 'candidate': plan}


def test_current_project_style_reaches_planner_and_reviewer(tmp_path, monkeypatch):
    _, route, plan = _project(tmp_path, monkeypatch)
    requests, reviews = [], []
    monkeypatch.setattr(bg, 'call_openai_api', lambda **kwargs: requests.append(json.loads(kwargs['prompt'])) or json.dumps(plan))
    monkeypatch.setattr(bg, '_review_background_plan', lambda candidate, payload: reviews.append(payload))
    bg.storybook_background_plan_tool('s', 'project', {'items': route})
    assert requests[0]['styles'] == reviews[0]['styles'] == {'ink': {'prompt': 'Ink on paper'}}


def test_background_semantic_review_accepts_consistent_plan(monkeypatch):
    monkeypatch.setattr(bg, 'call_openai_api', lambda **kwargs: json.dumps({'issues': []}))
    bg._review_background_plan({'spaces': []}, {'locations': {}, 'frames': {}})


def test_semantic_review_failure_returns_to_existing_plan_repair(tmp_path, monkeypatch):
    import copy
    base, route, plan = _project(tmp_path, monkeypatch)
    bad = copy.deepcopy(plan)
    bad['spaces'][0]['cards'][0]['description'] = 'Clothing fills the background'
    requests = []
    def planner(**kwargs):
        requests.append(json.loads(kwargs['prompt']))
        return json.dumps(bad if len(requests) == 1 else plan)
    def review(candidate, payload):
        if candidate == bad:
            raise ValueError('Clothing belongs to the actor')
    monkeypatch.setattr(bg, 'call_openai_api', planner)
    monkeypatch.setattr(bg, '_review_background_plan', review)
    bg.storybook_background_plan_tool('s', 'project', {'items': route})
    assert len(requests) == 2
    assert requests[1]['candidate'] == bad
    assert requests[1]['validation_error'] == 'Clothing belongs to the actor'
    saved = json.loads((base / '20_bible/references/background_references.json').read_text())
    assert 'Clothing' not in json.dumps(saved['spaces'])


@pytest.mark.parametrize('first_wrong_schema', [False, True])
def test_schema_or_new_review_findings_leave_another_repair_pass(tmp_path, monkeypatch, first_wrong_schema):
    import copy
    base, route, plan = _project(tmp_path, monkeypatch)
    first = copy.deepcopy(plan)
    first['spaces'][0]['cards'][0]['description'] = 'First defect'
    second = copy.deepcopy(plan)
    second['spaces'][0]['cards'][0]['description'] = 'Second defect'
    replies = [{'wrong_schema': True} if first_wrong_schema else first, second, plan]
    requests = []
    def planner(**kwargs):
        requests.append(json.loads(kwargs['prompt']))
        return json.dumps(replies[len(requests) - 1])
    def review(candidate, payload):
        description = candidate['spaces'][0]['cards'][0]['description']
        if description in ('First defect', 'Second defect'):
            raise ValueError(description)
    monkeypatch.setattr(bg, 'call_openai_api', planner)
    monkeypatch.setattr(bg, '_review_background_plan', review)
    bg.storybook_background_plan_tool('s', 'project', {'items': route})
    assert len(requests) == 3
    assert requests[2]['validation_error'] == 'Second defect'
    # Полный план пишется заново: прошлые замечания не должны вернуться.
    first_error = requests[1]['validation_error']
    assert requests[2]['previous_validation_errors'] == [first_error]
    saved = json.loads((base / '20_bible/references/background_references.json').read_text())
    assert 'defect' not in json.dumps(saved['spaces'])


@pytest.mark.parametrize('field', ['previous_sheet', 'landmark_reference_paths'])
def test_old_manifest_reference_resolves_moved_file(tmp_path, monkeypatch, field):
    from custom_tools.storybook import reference_catalog as catalog
    from custom_tools.storybook.project_paths import series_scope
    from custom_tools.storybook.studio.capabilities import invoke
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = invoke('projects_create', {'title': 'World', 'description': 'Shared world', 'genre': 'comedy',
                                     'target_age': 'all'})['project_id']
    sid = invoke('series_create', {'project_id': pid, 'title': 'One', 'source_material': '',
                                   'participant_ids': [], 'settings': {}})['series_id']
    base = tmp_path / pid / 'series' / sid
    folder = base / '20_bible/references'
    folder.mkdir(parents=True, exist_ok=True)
    # Предыдущий лист уже перенесён в shared, а манифест ссылается на старый путь.
    old = folder / 'backgrounds/previous.png'
    old.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (16, 16), 'gray').save(old)
    moved = catalog.store_reference(base, old, 'background', 'previous')
    assert not old.exists()
    sheet = {'space_id': 'yard', 'path': str(folder / 'backgrounds/sheet_02.png'), 'previous_sheet': None,
             'locations': [], 'prompt': 'Sheet', 'negative_prompt': '', 'width': 16, 'height': 16, 'sheet_number': 2}
    sheet[field] = str(old) if field == 'previous_sheet' else [str(old)]
    item = {'scene_number': 1, 'shot_number': 1, 'shot_type': 'start'}
    (folder / 'background_references.json').write_text(json.dumps(
        {'input_hash': 'h', 'sheets': [sheet], 'frame_bindings': {}}))
    (base / '20_bible/locations.json').write_text('[]')
    (base / '97_shots').mkdir(exist_ok=True)
    (base / '97_shots/shots.json').write_text(json.dumps({'items': [item]}))
    calls = []
    monkeypatch.setattr(bg, 'edit_image_vse_tool', lambda **kw: calls.append(kw) or Image.new(
        'RGB', (16, 16), 'blue').save(kw['output_path']))
    with series_scope(pid, sid):
        bg.storybook_background_references_tool('s', pid, {'items': [item], '_background_plan_hash': 'h'})
    assert calls[0]['image_paths'] == [moved]


def test_planner_keeps_listed_location_zones_in_one_space(tmp_path, monkeypatch):
    _, route, plan = _project(tmp_path, monkeypatch)
    prompts = []
    monkeypatch.setattr(bg, 'call_openai_api', lambda **kwargs: prompts.append(kwargs['system_prompt']) or json.dumps(plan))
    monkeypatch.setattr(bg, '_review_background_plan', lambda candidate, payload: None)
    bg.storybook_background_plan_tool('s', 'project', {'items': route})
    assert 'Зоны и участки из описания одной локации' in prompts[0]
    assert 'previous_validation_errors' in prompts[0]


def test_repair_keeps_accepted_cards_and_rewrites_only_flagged_frames(tmp_path, monkeypatch):
    import copy
    base, route, plan = _project(tmp_path, monkeypatch)
    first = copy.deepcopy(plan)
    first['spaces'][0]['cards'][0]['description'] = 'Defect'
    # Исправляя отмеченный кадр, модель портит уже принятый соседний.
    second = copy.deepcopy(plan)
    second['spaces'][0]['cards'][1]['description'] = 'Broken neighbour'
    replies = [first, second, second]
    requests = []
    def planner(**kwargs):
        requests.append(kwargs['prompt'])
        return json.dumps(replies[len(requests) - 1])
    def review(candidate, payload):
        bad = [card['frame_key'] for card in candidate['spaces'][0]['cards']
               if card['description'] in ('Defect', 'Broken neighbour')]
        if bad:
            raise ValueError(', '.join(bad) + ': фон описывает персонажа')
    monkeypatch.setattr(bg, 'call_openai_api', planner)
    monkeypatch.setattr(bg, '_review_background_plan', review)
    bg.storybook_background_plan_tool('s', 'project', {'items': route})
    assert len(requests) == 2
    saved = json.loads((base / '20_bible/references/background_references.json').read_text())
    text = json.dumps(saved['spaces'], ensure_ascii=False)
    assert 'Defect' not in text and 'Broken' not in text


def test_binding_error_names_unknown_and_repeated_frames(tmp_path):
    # Без ключей в сообщении планировщик исправляет привязку вслепую и повторяет ошибку.
    frames = {'1:1:start': {}, '1:2:start': {}}
    spaces = [{'cards': [{'frame_keys': ['1:1:start']}, {'frame_keys': ['1:1:start', '9:9:start']}]}]
    with pytest.raises(ValueError) as error:
        bg._build_background_sheets(spaces, frames, {}, tmp_path)
    assert '9:9:start' in str(error.value) and '1:1:start' in str(error.value)
