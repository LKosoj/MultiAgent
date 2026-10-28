"""Saved reference instructions must use the current deterministic compiler."""
import json
import pytest
from custom_tools.storybook import shots_prompt_qa as qa


@pytest.mark.parametrize("named_metadata", [False, True])
def test_qa_recompiles_saved_reference_roles_without_rewriting_scene(tmp_path, monkeypatch, named_metadata):
    base = tmp_path / 'p'
    (base / '91_screenplay').mkdir(parents=True)
    (base / '30_style').mkdir()
    (base / '91_screenplay/screenplay.json').write_text(json.dumps({'screenplay': [{
        'scene_number': 1, 'characters': [], 'storyboard': [{
            'shot_number': 1, 'camera_plan': 'MEDIUM SHOT', 'description': 'A cyclist travels past a brick wall.',
        }],
    }]}))
    (base / '30_style/image_styles.json').write_text(json.dumps({
        'default_style_id': 'ink', 'styles': {'ink': {'prompt': 'READY INK'}},
        'frame_bindings': {'1:1:end': 'ink'},
    }))
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    monkeypatch.setattr(qa, '_extract_shot_frame_spec_llm', lambda **kwargs: {'primary_subject': 'cyclist'})
    calls = []
    def fake_call(**kwargs):
        calls.append(kwargs)
        return '{"repairs": []}'
    monkeypatch.setattr(qa, 'call_openai_api', fake_call)
    core = 'Medium shot: cyclist travels to the right; brick wall passes left.'
    legacy = 'Опорные образы: image 1 as continuity reference (pose+composition+lighting); image 2 as location (layout+lighting+palette). Сохранить перспективу.'
    refs = ['/project/97_shots/previous.png', '/references/locations/street.png', '/references/characters/cyclist.png']
    location = {'name': 'Street', 'reference_image_path': refs[1]}
    character = {'name': 'Cyclist', 'reference_image_path': refs[2]}
    (base / '20_bible').mkdir()
    (base / '20_bible/characters.json').write_text(json.dumps([character]))
    (base / '20_bible/locations.json').write_text(json.dumps([location]))
    item = {'scene_number': 1, 'shot_number': 1, 'shot_type': 'end',
            'camera_plan': 'MEDIUM SHOT', 'english_prompt': core + '\n\nImage style: OLD\n\n' + legacy,
            'reference_roles_instruction': legacy, 'reference_image_paths': refs.copy(),
            'characters': ['Cyclist'] if named_metadata else [character],
            'locations': ['Street'] if named_metadata else [location],
            'negative_prompt': 'blur', 'video_prompt': ''}
    output = qa.shots_prompt_qa_tool('s', 'p', shots_data={'items': [item]}, force=True, dry_run=True)
    result = output['items'][0]
    assert '(pose+composition+lighting)' not in result['english_prompt']
    assert result['reference_roles_instruction'] != legacy
    assert 'image 2 as location support' in result['reference_roles_instruction']
    assert 'Экранные координаты' in result['reference_roles_instruction']
    assert result['english_prompt'].startswith(core + '\n\nImage style: READY INK')
    assert result['english_prompt'].endswith(result['reference_roles_instruction'])
    assert result['english_prompt'].count('READY INK') == 1
    assert result['reference_image_paths'] == refs
    assert 'image 3 as character — Cyclist' in result['reference_roles_instruction']
    assert calls and all('(pose+composition+lighting)' not in c['prompt'] for c in calls)


def test_reference_role_refresh_preserves_parent_location_chain():
    parent = {'name': 'District', 'reference_image_path': '/references/district.png'}
    middle = {'name': 'Lane', 'parent_location_name': 'District'}
    child = {'name': 'Corner', 'parent_location_name': 'Lane'}
    item = {'camera_plan': 'MEDIUM SHOT', 'english_prompt': 'A cyclist at the corner.',
            'reference_image_paths': ['/references/district.png'], 'locations': [child], 'characters': []}
    qa._refresh_item_reference_roles(item, {x['name'].lower(): x for x in (parent, middle, child)})
    assert 'image 1 as location — Corner' in item['reference_roles_instruction']
    assert item['english_prompt'].startswith('A cyclist at the corner.\n\n')


def test_reference_role_refresh_preserves_black_screen_instruction():
    text = 'Black screen.\n\nReference anchors: image 1 as continuity — reduce to black.'
    item = {'camera_plan': 'BLACK SCREEN', 'english_prompt': text,
            'reference_image_paths': ['/project/97_shots/previous.png'],
            'reference_roles_instruction': 'black-only reference'}
    qa._refresh_item_reference_roles(item)
    assert item['english_prompt'] == text
    assert item['reference_roles_instruction'] == 'black-only reference'


def test_qa_filters_character_not_visible_in_current_phase_only(tmp_path, monkeypatch):
    base = tmp_path / "phase_scope"
    (base / "91_screenplay").mkdir(parents=True)
    (base / "20_bible").mkdir()
    scenes = [
        {
            "scene_number": 1,
            "characters": ["Witness"],
            "action": "An insert of a hatch after Witness leaves",
            "storyboard": [{"shot_number": 1, "camera_plan": "MEDIUM SHOT", "description": "An insert of the hatch after Witness leaves"}],
        },
        {
            "scene_number": 2,
            "characters": ["Witness"],
            "action": "Witness waits by the hatch",
            "storyboard": [{"shot_number": 1, "camera_plan": "MEDIUM SHOT", "description": "Witness waits by the hatch"}],
        },
    ]
    (base / "91_screenplay/screenplay.json").write_text(json.dumps({"screenplay": scenes}), encoding="utf-8")
    witness = {"name": "Witness", "reference_image_path": "/references/characters/witness.png"}
    location = {"name": "Workshop", "reference_image_path": "/references/locations/workshop.png"}
    (base / "20_bible/characters.json").write_text(json.dumps([witness]), encoding="utf-8")
    (base / "20_bible/locations.json").write_text(json.dumps([location]), encoding="utf-8")
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(qa, "_extract_shot_frame_spec_llm", lambda **kwargs: {"primary_subject": "hatch", "visible_characters": []})
    monkeypatch.setattr(qa, "call_openai_api", lambda **kwargs: '{"repairs": []}')

    common_item = {
        "shot_number": 1, "shot_type": "start", "camera_plan": "MEDIUM SHOT",
        "english_prompt": "A hatch in the workshop", "video_prompt": "", "negative_prompt": "",
        "reference_image_paths": ["/97_shots/phase_scope/scene_01_shot_01/img_final_start_01_01.png", location["reference_image_path"], witness["reference_image_path"]],
        "characters": [witness], "locations": [location], "reference_roles_instruction": "",
        "scene_continuity_facts": {"hatch": "fixed"}, "_shot_frame_spec": {"visible_characters": []},
    }
    scene_one = dict(common_item, scene_number=1)
    scene_two = dict(common_item, scene_number=2)
    location_index = {location["name"].lower(): location}
    character_index = {witness["name"].lower(): witness}
    qa._refresh_item_reference_roles(scene_one, location_index, character_index)
    qa._refresh_item_reference_roles(scene_two, location_index, character_index)
    before_scene_two = json.loads(json.dumps(scene_two))
    result = qa.shots_prompt_qa_tool("s", "phase_scope", shots_data={"items": [scene_one, scene_two]}, scene_numbers=[1], force=True, dry_run=True, global_max_repairs=0)

    processed = next(item for item in result["items"] if item["scene_number"] == 1)
    untouched = next(item for item in result["items"] if item["scene_number"] == 2)
    assert processed["characters"] == []
    assert witness["reference_image_path"] not in processed["reference_image_paths"]
    assert location["reference_image_path"] in processed["reference_image_paths"]
    assert "/97_shots/phase_scope/scene_01_shot_01/img_final_start_01_01.png" in processed["reference_image_paths"]
    assert processed["scene_continuity_facts"] == {"hatch": "fixed"}
    assert "/97_shots/phase_scope/scene_01_shot_01/img_final_start_01_01.png" in untouched["reference_image_paths"]
    assert untouched == before_scene_two
