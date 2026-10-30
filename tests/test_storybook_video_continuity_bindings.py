import copy
import json

import pytest

from custom_tools.storybook import video_plan as vp


def _items(base):
    return [
        dict(scene_number=1, shot_number=1, shot_type=phase, timing='00:02',
             video_path=str(base / '1.mp4'), video_prompt='Run', width=1920, height=1088)
        for phase in ('start', 'end')
    ]


def test_phase_and_planning_shot_keep_continuity_fields():
    spec = {'camera_anchor': 'wide', 'must_not_show': ['ball'],
            'world_physics': ['glass stays broken'], 'visible_characters': ['boy']}
    item = dict(scene_number=1, shot_number=1, shot_type='start', timing='2s',
                _shot_frame_spec={'start_state_spec': spec},
                _continuity={'start': {'world_id': 'yard', 'states': {'ball': {'state_id': 'snow'}}}})
    planned = vp._planning_shot(item)
    assert planned['opening']['must_not_show'] == ['ball']
    assert planned['opening']['world_physics'] == ['glass stays broken']
    assert planned['opening']['visible_characters'] == ['boy']
    assert planned['continuity']['start']['states']['ball']['state_id'] == 'snow'


def test_fingerprint_changes_when_continuity_state_changes(tmp_path):
    first = _items(tmp_path)
    first[0]['_continuity'] = {'start': {'world_id': 'yard', 'states': {'ball': {'state_id': 'on_snow'}}}}
    second = copy.deepcopy(first)
    second[0]['_continuity']['start']['states']['ball']['state_id'] = 'in_window'
    assert vp._fingerprint(first) != vp._fingerprint(second)


def test_current_video_direction_survives_source_description_and_invalidates_plan(tmp_path):
    first = _items(tmp_path)
    first[0].update(shot_description='The actor throws the case.', video_prompt='The actor carries the case carefully.')
    planned = vp._planning_shot(first[0])
    assert planned['current_video_prompt'] == 'The actor carries the case carefully.'
    second = copy.deepcopy(first)
    second[0]['video_prompt'] = 'The actor sets the case down gently.'
    assert vp._fingerprint(first) != vp._fingerprint(second)


def test_managed_stale_unit_rejected_but_unmanaged_keeps_legacy(monkeypatch, tmp_path):
    base = tmp_path / 'plots/storybooks/test'
    (base / '97_shots').mkdir(parents=True)
    items = _items(base)
    unit = {'shot_keys': ['1-1'], 'duration_s': 2, 'video_path': str(base / 'unit.mp4'),
            'video_prompt': 'Run', 'reference_image_paths': [], 'resolution': '480p'}
    (base / '97_shots/video_plan.json').write_text(json.dumps({'units': [unit]}))
    monkeypatch.chdir(tmp_path)

    from custom_tools.storybook import continuity

    monkeypatch.setattr(continuity, 'bind_continuity_to_items', lambda base, values: [
        dict(value, _continuity={'start': {'world_id': 'yard'}}) for value in values])
    monkeypatch.setattr(continuity, 'unit_continuity_hash', lambda base, keys: 'current')
    with pytest.raises(ValueError, match='Video unit continuity is stale'):
        vp.apply_video_plan('test', items)

    monkeypatch.setattr(continuity, 'bind_continuity_to_items', lambda base, values: list(values))
    monkeypatch.setattr(continuity, 'unit_continuity_hash', lambda base, keys: None)
    result = vp.apply_video_plan('test', items)
    assert result[0]['video_path'] == str(base / 'unit.mp4')
