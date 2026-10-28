"""Регрессии актуальности кэша shot_frame_spec в генераторе и QA."""
from __future__ import annotations

import json

import custom_tools.storybook.shots_prompt_qa as qa
from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils


def _scene(scene_number: int, character: str):
    return {
        "scene_number": scene_number,
        "characters": [character],
        "location_time": "",
        "location_canon_name": "Workshop",
        "action": "",
        "storyboard": [{
            "shot_number": 1,
            "camera_plan": "MEDIUM SHOT",
            "description": f"{character} waits by the workbench",
            "timing": "0-1",
        }],
    }


def _item(scene_number: int, character: str, key: str = "old-key"):
    return {
        "scene_number": scene_number,
        "shot_number": 1,
        "shot_type": "start",
        "camera_plan": "MEDIUM SHOT",
        "english_prompt": character,
        "video_prompt": "",
        "_shot_frame_spec": {"primary_subject": character, "must_show": [character]},
        "_shot_frame_spec_cache_key": key,
    }


def test_frame_spec_cache_key_changes_with_version(monkeypatch):
    monkeypatch.setattr(shared_utils, "_SHOT_FRAME_SPEC_VERSION", 1, raising=False)
    old_key = shared_utils._build_shot_frame_spec_cache_key(
        scene_number=1, shot_number=1, shot_description="Hero waits", camera_plan="MEDIUM SHOT",
        scene_characters=["Hero"], location_time="", location_canon_name="Workshop",
    )
    monkeypatch.setattr(shared_utils, "_SHOT_FRAME_SPEC_VERSION", 2, raising=False)
    new_key = shared_utils._build_shot_frame_spec_cache_key(
        scene_number=1, shot_number=1, shot_description="Hero waits", camera_plan="MEDIUM SHOT",
        scene_characters=["Hero"], location_time="", location_canon_name="Workshop",
    )
    assert old_key != new_key


def test_frame_spec_cache_key_includes_scene_continuity_context():
    common = dict(
        scene_number=1, shot_number=1, shot_description="Hero waits",
        camera_plan="MEDIUM SHOT", scene_characters=["Hero"],
        location_time="", location_canon_name="Workshop",
    )
    first = shared_utils._build_shot_frame_spec_cache_key(
        **common, scene_action="Hero stands by the door", scene_continuity_facts={"side": "inside"}
    )
    second = shared_utils._build_shot_frame_spec_cache_key(
        **common, scene_action="Hero crosses the room", scene_continuity_facts={"side": "outside"}
    )
    default = shared_utils._build_shot_frame_spec_cache_key(**common)
    explicit_empty = shared_utils._build_shot_frame_spec_cache_key(
        **common, scene_action="", scene_continuity_facts={}
    )
    assert first != second
    assert default == explicit_empty


def test_frame_spec_extractor_passes_scene_context_to_model_and_cache(monkeypatch):
    monkeypatch.setattr(shared_utils, "_SHOT_FRAME_SPEC_CACHE", {})
    monkeypatch.setattr(shared_utils, "_load_shot_frame_spec_from_disk_cache", lambda **kwargs: {})
    monkeypatch.setattr(shared_utils, "_load_shot_frame_spec_from_existing_shots", lambda **kwargs: {})
    monkeypatch.setattr(shared_utils, "_store_shot_frame_spec_to_disk_cache", lambda **kwargs: None)
    monkeypatch.setattr(
        shared_utils, "_normalize_shot_frame_spec",
        lambda raw_spec, scene_characters: dict(raw_spec),
    )
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"primary_subject":"Hero"}'

    monkeypatch.setattr(shared_utils, "call_openai_api", fake_call)
    first = shared_utils._extract_shot_frame_spec_llm(
        project_id="context_payload", scene_number=1, shot_number=1,
        shot_description="Hero waits", camera_plan="MEDIUM SHOT",
        scene_action="Hero stands by the door", scene_characters=["Hero"],
        location_time="", location_canon_name="Workshop",
        scene_continuity_facts={"side": "inside"},
    )
    second = shared_utils._extract_shot_frame_spec_llm(
        project_id="context_payload", scene_number=1, shot_number=1,
        shot_description="Hero waits", camera_plan="MEDIUM SHOT",
        scene_action="Hero crosses the room", scene_characters=["Hero"],
        location_time="", location_canon_name="Workshop",
        scene_continuity_facts={"side": "outside"},
    )

    assert first and second and len(calls) == 2
    payloads = [json.loads(call["prompt"].split("INPUT:\n", 1)[1]) for call in calls]
    assert payloads[0]["scene_action"] == "Hero stands by the door"
    assert payloads[0]["scene_continuity_facts"] == {"side": "inside"}
    assert payloads[1]["scene_action"] == "Hero crosses the room"
    assert payloads[1]["scene_continuity_facts"] == {"side": "outside"}


def test_frame_spec_exclusions_do_not_expand_partial_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shared_utils, '_SHOT_FRAME_SPEC_CACHE', {})
    monkeypatch.setattr(shared_utils, '_normalize_shot_frame_spec', lambda raw, characters: raw)
    monkeypatch.setattr(shared_utils, '_store_shot_frame_spec_to_disk_cache', lambda **kwargs: None)
    calls = []
    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"scene_mode":"object_focus","primary_subject":"cabinet","must_show":["latch"]}'
    monkeypatch.setattr(shared_utils, 'call_openai_api', fake_call)
    result = shared_utils._extract_shot_frame_spec_llm(
        scene_number=1, shot_number=1, project_id='partial_state',
        shot_description='The latch turns on the damaged left panel.',
        camera_plan='MEDIUM SHOT', scene_characters=[], location_time='', location_canon_name='Workshop',
        scene_action='The left panel is damaged; the right panel remains intact.',
        scene_continuity_facts={},
    )
    assert result['primary_subject'] == 'cabinet'
    assert 'partial change does not imply complete absence' in calls[0]['system_prompt']


def test_unkeyed_existing_shot_is_not_reused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        shared_utils,
        "_normalize_shot_frame_spec",
        lambda raw_spec, scene_characters: dict(raw_spec),
    )
    path = tmp_path / "plots" / "storybooks" / "p" / "97_shots"
    path.mkdir(parents=True)
    (path / "shots.json").write_text(json.dumps({"items": [{
        "scene_number": 1,
        "shot_number": 1,
        "camera_plan": "MEDIUM SHOT",
        "_shot_frame_spec": {"primary_subject": "Hero", "must_show": ["Hero"]},
    }]}), encoding="utf-8")

    assert shared_utils._load_shot_frame_spec_from_existing_shots(
        project_id="p",
        scene_number=1,
        shot_number=1,
        cache_key="current-key",
        scene_characters=["Hero"],
    ) == {}


def test_stale_embedded_specs_are_refreshed_for_both_phases_and_then_skipped(tmp_path, monkeypatch):
    project_id = "stale_specs"
    base = tmp_path / project_id
    screenplay_dir = base / "91_screenplay"
    screenplay_dir.mkdir(parents=True)
    (screenplay_dir / "screenplay.json").write_text(
        json.dumps({"screenplay": [_scene(1, "Hero")]}), encoding="utf-8"
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    fresh_spec = {"primary_subject": "Hero", "must_show": ["Hero", "new gate"]}
    extract_calls = []

    def fake_extract(**kwargs):
        extract_calls.append(kwargs)
        return dict(fresh_spec)

    monkeypatch.setattr(qa, "_extract_shot_frame_spec_llm", fake_extract)
    monkeypatch.setattr(qa, "call_openai_api", lambda **kwargs: '{"repairs": [], "notes": "ok"}')
    data = {
        "items": [_item(1, "Hero"), dict(_item(1, "Hero"), shot_type="end")],
        "consistency_rules": [],
        "prompts_validated": True,
        "prompts_validated_spec_version": 1,
    }

    result = qa.shots_prompt_qa_tool(
        session_id="s", project_id=project_id, shots_data=data,
        force=False, global_max_repairs=0, dry_run=True,
    )
    assert len(extract_calls) == 1
    keys = {item.get("_shot_frame_spec_cache_key") for item in result["items"]}
    assert len(keys) == 1 and "old-key" not in keys
    assert all(item["_shot_frame_spec"] == fresh_spec for item in result["items"])
    assert result["prompts_validated_spec_version"] == shared_utils._SHOT_FRAME_SPEC_VERSION

    qa.shots_prompt_qa_tool(
        session_id="s", project_id=project_id, shots_data=result,
        force=False, global_max_repairs=0, dry_run=True,
    )
    assert len(extract_calls) == 1


def test_partial_current_coverage_does_not_merge_old_disk_coverage(tmp_path, monkeypatch):
    project_id = "coverage_version"
    base = tmp_path / project_id
    (base / "91_screenplay").mkdir(parents=True)
    (base / "97_shots").mkdir(parents=True)
    (base / "91_screenplay" / "screenplay.json").write_text(
        json.dumps({"screenplay": [_scene(1, "Hero"), _scene(2, "Observer")]}), encoding="utf-8"
    )
    (base / "97_shots" / "shots.json").write_text(
        json.dumps({"items": [], "prompts_validated_scenes": [2], "prompts_validated_spec_version": 1}),
        encoding="utf-8",
    )
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        qa,
        "_extract_shot_frame_spec_llm",
        lambda **kwargs: {"primary_subject": "Hero", "must_show": ["Hero"]},
    )
    monkeypatch.setattr(qa, "call_openai_api", lambda **kwargs: '{"repairs": [], "notes": "ok"}')
    data = {
        "items": [_item(1, "Hero"), _item(2, "Observer")],
        "consistency_rules": [],
        "prompts_validated_spec_version": getattr(shared_utils, "_SHOT_FRAME_SPEC_VERSION", 2),
        "prompts_validated_scenes": [],
    }

    result = qa.shots_prompt_qa_tool(
        session_id="s", project_id=project_id, shots_data=data,
        scene_numbers=[1], force=True, global_max_repairs=0, dry_run=False,
    )
    assert result["prompts_validated_scenes"] == [1]


def test_extended_context_excludes_future_scene_actions(monkeypatch):
    captured = {}
    def extract(**kwargs):
        captured.update(kwargs)
        return {'primary_subject':'latch','visible_characters':[]}
    monkeypatch.setattr(shared_utils, '_extract_shot_frame_spec_llm', extract)
    storyboard=[{'shot_number':1,'description':'The panel breaks.'},
                {'shot_number':2,'description':'Observe the latch.'},
                {'shot_number':3,'description':'FUTURE_EXIT_MARKER'}]
    context=shared_utils._build_extended_context(
        project_id='scope',scene={'scene_number':1},storyboard=storyboard,shot_number=2,
        scene_action='The panel breaks. Observe the latch. FUTURE_EXIT_MARKER',
        shot_description='Observe the latch.',camera_plan='CLOSE-UP',scene_characters=[],
        screenplay_data={},characters_data=[],locations_data=[])
    assert 'The panel breaks.' in captured['scene_action']
    assert 'FUTURE_EXIT_MARKER' not in captured['scene_action']
    assert 'FUTURE_EXIT_MARKER' not in context['scene_action']
