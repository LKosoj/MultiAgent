import copy
import json
from pathlib import Path

import pytest

from custom_tools.storybook import continuity as ct


def _screenplay():
    descriptions = {
        1: 'Чемодан стоит на платформе станции.',
        2: 'Мечта героя о далёком городе.',
        3: 'Герой возвращается на станцию к чемодану.',
    }
    return {'screenplay': [
        {'scene_number': scene, 'storyboard': [
            {'shot_number': 1, 'description': descriptions[scene]},
        ]} for scene in (1, 2, 3)
    ]}


def _plan():
    frames = {}
    for scene in (1, 3):
        for phase in ('start', 'end'):
            frames[f'{scene}:1:{phase}'] = {
                'world_id': 'station',
                'entities': [{'id': 'suitcase', 'state_id': 'on_platform', 'visible': True}],
            }
    for phase in ('start', 'end'):
        frames[f'2:1:{phase}'] = {'world_id': 'dream', 'entities': []}
    return {
        'entities': [{
            'id': 'suitcase', 'name': 'Чемодан', 'kind': 'prop', 'world_id': 'station',
            'states': [{'id': 'on_platform', 'description': 'На платформе станции',
                        'space': 'station', 'support': 'platform', 'holder': None,
                        'motion': 'stationary'}],
            'initial_state': 'on_platform',
        }],
        'frames': frames,
    }


def test_validate_keeps_suitcase_state_through_dream_interlude():
    plan = _plan()
    assert ct.validate_continuity_plan(plan, _screenplay()) is plan


def test_frame_references_ignore_unrendered_prop_paths(tmp_path, monkeypatch):
    plan = _plan()
    plan['entities'][0]['reference_path'] = str(tmp_path / 'missing-prop.png')
    monkeypatch.setattr(ct, 'load_continuity_plan', lambda base: plan)
    frame = ct.frame_continuity(tmp_path, 1, 1, 'start')
    assert frame['landmark_reference_paths'] == []
    assert 'reference_path' not in frame['entities'][0]
    assert frame['states']['suitcase']['support'] == 'platform'


def test_identity_cleanup_survives_empty_identity_repairs(tmp_path, monkeypatch):
    from custom_tools import image_tools
    from PIL import Image
    source = tmp_path / 'actor.png'
    Image.new('RGB', (16, 16)).save(source)
    (tmp_path / '20_bible/references/characters').mkdir(parents=True)
    plan = _plan()
    plan['character_identity'] = {}
    plan['entities'][0]['states'][0]['holder'] = 'Actor'
    monkeypatch.setattr(ct, 'load_continuity_plan', lambda base: plan)
    calls = []
    def edit(**kwargs):
        calls.append(kwargs)
        Image.new('RGB', (16, 16), 'gray').save(kwargs['output_path'])
    monkeypatch.setattr(image_tools, 'edit_image_vse_tool', edit)
    actor = {'name': 'Actor', 'reference_image_path': str(source)}
    clean = ct.identity_reference_path(tmp_path, actor, 'session', 0)
    assert clean != str(source)
    assert 'Чемодан' in calls[0]['prompt']
    assert ct.identity_reference_path(tmp_path, actor, 'session', 0) == clean
    assert len(calls) == 1


def test_validate_rejects_implicit_transfer_to_character():
    plan = _plan()
    plan['entities'][0]['states'].append({
        'id': 'with_hero', 'description': 'У героя', 'space': 'station',
        'support': 'hand', 'holder': 'Герой', 'motion': 'moving'})
    plan['frames']['3:1:start']['entities'][0]['state_id'] = 'with_hero'
    with pytest.raises(ValueError, match='state changed without an event'):
        ct.validate_continuity_plan(plan, _screenplay())


def test_validate_rejects_same_id_in_another_world():
    plan = _plan()
    plan['frames']['2:1:start']['entities'] = [{'id': 'suitcase', 'state_id': 'on_platform', 'visible': True}]
    with pytest.raises(ValueError, match='crossed worlds'):
        ct.validate_continuity_plan(plan, _screenplay())


@pytest.mark.parametrize('mutate, message', [
    (lambda p: p['frames']['1:1:start']['entities'][0].update(id='unknown'), 'Invalid continuity frame entity'),
    (lambda p: p['frames']['1:1:start']['entities'].append(
        {'id': 'suitcase', 'state_id': 'on_platform', 'visible': True}), 'Invalid continuity frame entity'),
])
def test_validate_rejects_unknown_or_duplicate_entity(mutate, message):
    plan = _plan()
    mutate(plan)
    with pytest.raises(ValueError, match=message):
        ct.validate_continuity_plan(plan, _screenplay())


def test_transition_evidence_must_be_from_that_shot_description():
    plan = _plan()
    plan['entities'][0]['states'].append({
        'id': 'moved', 'description': 'У героя', 'space': 'station',
        'support': 'hand', 'holder': 'Герой', 'motion': 'moving'})
    plan['entities'][0]['transitions'] = [{
        'at': '3:1:start', 'from': 'on_platform', 'to': 'moved', 'evidence': 'Чемодан стоит на платформе',
    }]
    with pytest.raises(ValueError, match='no source event'):
        ct.validate_continuity_plan(plan, _screenplay())


def test_explicit_transfer_changes_state_and_preserves_authored_fields():
    screenplay = _screenplay()
    screenplay['screenplay'][2]['storyboard'][0].update(voiceover='Авторский текст', timing='00:10 - 00:15')
    source = {'screenplay': screenplay, 'characters': []}
    plan = _plan()
    plan['repairs'] = [{'scene_number': 3, 'shot_number': 1, 'reason': 'Explicit physical transfer',
                        'set': {'description': 'Герой поднимает чемодан с платформы и держит его в руке.'}}]
    plan['entities'][0]['states'].append({'id': 'held', 'description': 'В руке героя', 'space': 'station',
                                        'support': 'hand', 'holder': 'Герой', 'motion': 'moving'})
    plan['entities'][0]['transitions'] = [{'at': '3:1:end', 'from': 'on_platform', 'to': 'held',
                                         'evidence': 'Герой поднимает чемодан с платформы'}]
    plan['frames']['3:1:end']['entities'][0]['state_id'] = 'held'
    corrected = ct._apply_repairs(source, plan)
    assert ct.validate_continuity_plan(plan, corrected['screenplay']) is plan
    shot = corrected['screenplay']['screenplay'][2]['storyboard'][0]
    assert shot['voiceover'] == 'Авторский текст' and shot['timing'] == '00:10 - 00:15'
    assert source['screenplay']['screenplay'][2]['storyboard'][0]['description'] != shot['description']


def test_validate_requires_all_frames_and_landmark_geometry():
    plan = _plan()
    del plan['frames']['2:1:end']
    with pytest.raises(ValueError, match='frame coverage'):
        ct.validate_continuity_plan(plan, _screenplay())

    plan = _plan()
    plan['entities'][0]['kind'] = 'landmark'
    plan['entities'][0].pop('geometry', None)
    with pytest.raises(ValueError, match='no fixed geometry'):
        ct.validate_continuity_plan(plan, _screenplay())


def _managed_project(tmp_path, plan):
    base = tmp_path / 'project'
    (base / '20_bible').mkdir(parents=True)
    (base / '20_bible/continuity_plan.json').write_text(json.dumps(plan), encoding='utf-8')
    return base


def test_bindings_share_landmark_geometry_and_hash_tracks_it(tmp_path):
    plan = _plan()
    plan['entities'][0].update(kind='landmark', geometry='две колонны и часы между ними')
    base = _managed_project(tmp_path, plan)
    items = [{'scene_number': 1, 'shot_number': 1, 'shot_type': 'start'}]
    bound = ct.bind_continuity_to_items(base, items)
    assert bound[0]['_continuity']['start']['entities'][0]['geometry'] == \
        bound[0]['_continuity']['end']['entities'][0]['geometry']
    first_hash = ct.unit_continuity_hash(base, ['1-1'])

    changed = copy.deepcopy(plan)
    changed['entities'][0]['geometry'] = 'только одна колонна'
    (base / '20_bible/continuity_plan.json').write_text(json.dumps(changed), encoding='utf-8')
    assert ct.unit_continuity_hash(base, ['1-1']) != first_hash


def test_unrelated_entity_does_not_change_unit_hash(tmp_path):
    plan = _plan()
    plan['entities'][0].update(kind='landmark', geometry='фиксированная арка')
    base = _managed_project(tmp_path, plan)
    first_hash = ct.unit_continuity_hash(base, ['1-1'])
    changed = copy.deepcopy(plan)
    changed['entities'].append({
        'id': 'unrelated', 'name': 'Лампа', 'kind': 'prop', 'world_id': 'station',
        'states': [{'id': 'off', 'description': 'Выключена', 'space': 'station',
                    'support': 'wall', 'holder': None, 'motion': 'stationary'}],
        'initial_state': 'off',
    })
    (base / '20_bible/continuity_plan.json').write_text(json.dumps(changed), encoding='utf-8')
    assert ct.unit_continuity_hash(base, ['1-1']) == first_hash


def test_repair_candidate_merges_partial_frame_update_without_dropping_bindings(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    (base / '91_screenplay').mkdir(parents=True)
    source = {'screenplay': _screenplay(), 'characters': [], 'locations': [],
              'author_materials': {'revision.txt': 'Чемодан должен оставаться на платформе.'}}
    plan = _plan()
    plan['entities'].append({'id': 'arch', 'name': 'Арка', 'kind': 'prop', 'world_id': 'station',
        'states': [{'id': 'fixed', 'description': 'У станции', 'space': 'station',
                    'support': 'ground', 'holder': None, 'motion': 'stationary'}],
        'initial_state': 'fixed'})
    plan['frames']['1:1:start']['entities'].append({'id': 'arch', 'state_id': 'fixed', 'visible': False})
    plan['frames']['1:1:end']['entities'].append({'id': 'arch', 'state_id': 'fixed', 'visible': False})
    plan['frames']['1:1:start']['entities'][0]['state_id'] = 'wrong'
    fingerprint = ct._digest([ct._PLAN_VERSION, source])
    plan['_input_hash'] = fingerprint

    def repair(**kwargs):
        payload = json.loads(kwargs['prompt'])
        assert len(payload['candidate']['entities']) == 1
        assert payload['source']['author_materials'] == source['author_materials']
        return json.dumps({'frame_updates': {
            '1:1:start': {'entities': [{'id': 'suitcase', 'state_id': 'on_platform'}]}}})

    monkeypatch.setattr(ct, 'call_openai_api', repair)
    repaired = ct._repair_candidate(base, source, plan, fingerprint)
    bindings = {item['id']: item for item in repaired['frames']['1:1:start']['entities']}
    assert bindings['suitcase']['visible'] is True
    assert bindings['arch']['visible'] is False


def test_publish_records_publication_sources_before_interrupted_write(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    (base / '91_screenplay').mkdir(parents=True)
    (base / '20_bible').mkdir(parents=True)
    source = {'screenplay': _screenplay(), 'characters': [], 'locations': [],
              'author_materials': {}}
    plan = _plan()
    original_write = ct._write
    writes = []

    def interrupted(path, value):
        writes.append(Path(path).name)
        if Path(path).name == 'screenplay.json':
            raise RuntimeError('simulated interruption')
        return original_write(path, value)

    monkeypatch.setattr(ct, '_write', interrupted)
    with pytest.raises(RuntimeError, match='interruption'):
        ct._publish_plan(base, source, plan)
    candidate_path = base / '91_screenplay/continuity_candidate.json'
    candidate = json.loads(candidate_path.read_text())
    fingerprint = ct._digest([ct._PLAN_VERSION, source])
    assert 'continuity_candidate.json' in writes
    assert candidate['_publication_sources']
    assert ct._repair_candidate(base, source, candidate, fingerprint) is candidate


def test_grouped_scene_repair_is_normalized_and_preserves_shot_fields():
    screenplay = _screenplay()
    screenplay['screenplay'][0].update(voiceover='Голос автора', timing='00:03')
    screenplay['screenplay'][0]['storyboard'][0].update(voiceover='Шот', timing='00:01', camera='wide')
    source = {'screenplay': screenplay, 'characters': [], 'locations': [], 'author_materials': {}}
    plan = _plan() | {'repairs': [{
        'scene_number': 1, 'action': 'Герой оставляет чемодан у арки.',
        'storyboard': [{'shot_number': 1, 'description': 'Чемодан стоит у арки.'}],
    }]}
    corrected = ct._apply_repairs(source, plan)
    scene = corrected['screenplay']['screenplay'][0]
    assert scene['action'] == 'Герой оставляет чемодан у арки.'
    assert scene['storyboard'][0]['description'] == 'Чемодан стоит у арки.'
    assert scene['voiceover'] == 'Голос автора' and scene['timing'] == '00:03'
    assert scene['storyboard'][0]['voiceover'] == 'Шот'
    assert {repair['shot_number'] for repair in plan['repairs']} == {None, 1}
    invalid = _plan() | {'repairs': [{
        'scene_number': 1, 'storyboard': [{'shot_number': 1, 'description': 'x', 'camera': 'close'}],
    }]}
    with pytest.raises(ValueError, match='staging descriptions'):
        ct._apply_repairs(source, invalid)


def test_plan_tool_resumes_current_candidate_before_stale_existing(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'project'
    for directory in ('91_screenplay', '20_bible', 'inputs'):
        (base / directory).mkdir(parents=True)
    screenplay = _screenplay()
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(screenplay), encoding='utf-8')
    (base / '20_bible/characters.json').write_text('[]', encoding='utf-8')
    (base / '20_bible/locations.json').write_text('[]', encoding='utf-8')
    source = {'screenplay': screenplay, 'characters': [], 'locations': [], 'author_materials': {}}
    plan = _plan()
    fingerprint = ct._digest([ct._PLAN_VERSION, source])
    (base / '20_bible/continuity_plan.json').write_text(json.dumps(plan | {'input_hash': 'stale'}), encoding='utf-8')
    (base / '91_screenplay/continuity_candidate.json').write_text(
        json.dumps(plan | {'_input_hash': fingerprint}), encoding='utf-8')
    monkeypatch.setattr(ct, 'call_openai_api', lambda **kwargs: pytest.fail('Stale existing plan must not be repaired'))
    result = ct.storybook_continuity_plan_tool('session', 'project')
    assert result['status'] == 'success'
    assert json.loads((base / '20_bible/continuity_plan.json').read_text())['input_hash']


def test_repair_focuses_all_entities_with_evidence_stale_after_one_description_repair(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    (base / '91_screenplay').mkdir(parents=True)
    screenplay = _screenplay()
    screenplay['screenplay'][0]['storyboard'][0]['description'] = (
        'Чемодан стоит на платформе; фонарь стоит у стены; затем чемодан достигает арки '
        'и фонарь переходит к арке.')
    source = {'screenplay': screenplay, 'characters': [], 'locations': [], 'author_materials': {}}
    plan = _plan()
    second = {'id': 'lamp', 'name': 'Фонарь', 'kind': 'prop', 'world_id': 'station',
              'states': [{'id': 'at_wall', 'description': 'У стены', 'space': 'station',
                          'support': 'ground', 'holder': None, 'motion': 'stationary'},
                         {'id': 'by_arch', 'description': 'У арки', 'space': 'station',
                          'support': 'ground', 'holder': None, 'motion': 'stationary'}],
              'initial_state': 'at_wall',
              'transitions': [{'at': '1:1:end', 'from': 'at_wall', 'to': 'by_arch',
                               'evidence': 'фонарь переходит к арке'}]}
    plan['entities'][0]['states'].append({'id': 'by_arch', 'description': 'У арки', 'space': 'station',
                                          'support': 'ground', 'holder': None, 'motion': 'stationary'})
    plan['entities'][0]['transitions'] = [{'at': '1:1:end', 'from': 'on_platform', 'to': 'by_arch',
                                           'evidence': 'чемодан достигает арки'}]
    plan['entities'].append(second)
    for key, frame in plan['frames'].items():
        if key == '1:1:end':
            frame['entities'][0]['state_id'] = 'by_arch'
        elif key.startswith('3:1:'):
            frame['entities'][0]['state_id'] = 'by_arch'
        if key.startswith('1:1:'):
            frame['entities'].append({'id': 'lamp', 'state_id': 'at_wall' if key.endswith('start') else 'by_arch',
                                      'visible': True})
        elif key.startswith('3:1:'):
            frame['entities'].append({'id': 'lamp', 'state_id': 'by_arch', 'visible': True})
    plan['repairs'] = [{'scene_number': 1, 'shot_number': 1,
                        'reason': 'Авторское уточнение staging',
                        'set': {'description': 'Чемодан перемещён к арке; фонарь перемещён к арке.'}}]
    fingerprint = ct._digest([ct._PLAN_VERSION, source])
    plan['_input_hash'] = fingerprint
    calls = []

    def repair(**kwargs):
        payload = json.loads(kwargs['prompt'])
        calls.append(payload)
        assert {entity['id'] for entity in payload['candidate']['entities']} == {'suitcase', 'lamp'}
        assert payload['source']['screenplay']['screenplay'][0]['storyboard'][0]['description'] == \
            'Чемодан перемещён к арке; фонарь перемещён к арке.'
        fixed = copy.deepcopy(plan['entities'])
        fixed[0]['transitions'][0]['evidence'] = 'Чемодан перемещён к арке'
        fixed[1]['transitions'][0]['evidence'] = 'фонарь перемещён к арке'
        return json.dumps({'entity_replacements': fixed})

    monkeypatch.setattr(ct, 'call_openai_api', repair)
    monkeypatch.setattr(ct, '_review_staging_repairs', lambda base, source, plan: None)
    result = ct._repair_candidate(base, source, plan, fingerprint)
    assert len(calls) == 1
    assert {entity['id'] for entity in result['entities']} == {'suitcase', 'lamp'}


def test_staging_review_is_cached_and_repeated_for_changed_text(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    (base / '91_screenplay').mkdir(parents=True)
    source = {'screenplay': _screenplay(), 'characters': [], 'locations': [], 'author_materials': {}}
    source['screenplay']['screenplay'][0]['storyboard'][0].update(
        camera_plan='CLOSE-UP', location_canon_name='station')
    plan = _plan() | {'repairs': [{'scene_number': 1, 'shot_number': 1,
        'reason': 'staging', 'set': {'description': 'Чемодан перемещён к арке.'}}]}
    calls = []
    monkeypatch.setattr(ct, 'call_openai_api', lambda **kwargs: calls.append(kwargs) or
                        json.dumps({'passed': True, 'issues': []}))
    original = copy.deepcopy(source)
    ct._review_staging_repairs(base, source, plan)
    ct._review_staging_repairs(base, source, plan)
    assert len(calls) == 1
    projected = json.loads(calls[0]['prompt'])['screenplay']['screenplay'][0]['storyboard'][0]
    assert projected['camera_plan'] == 'CLOSE-UP' and projected['location_canon_name'] == 'station'
    plan['repairs'][0]['set']['description'] = 'Чемодан остаётся у платформы.'
    ct._review_staging_repairs(base, source, plan)
    assert len(calls) == 2
    assert source == original


def test_staging_review_rejects_nonempty_issues_without_publishing(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    (base / '91_screenplay').mkdir(parents=True)
    source = {'screenplay': _screenplay(), 'characters': [], 'locations': [], 'author_materials': {}}
    plan = _plan() | {'repairs': [{'scene_number': 1, 'shot_number': 1,
        'reason': 'staging', 'set': {'description': 'Чемодан перемещён к арке.'}}]}
    monkeypatch.setattr(ct, 'call_openai_api', lambda **kwargs: json.dumps({'passed': True, 'issues': ['unneeded change']}))
    with pytest.raises(ValueError, match='Staging review rejected'):
        ct._review_staging_repairs(base, source, plan)
    assert not (base / '91_screenplay/screenplay.json').exists()
    assert not (base / '20_bible/continuity_plan.json').exists()


def test_repair_candidate_reverts_rejected_staging_with_one_corrective_patch(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    (base / '91_screenplay').mkdir(parents=True)
    source = {'screenplay': _screenplay(), 'characters': [], 'locations': [], 'author_materials': {}}
    original = copy.deepcopy(source)
    plan = _plan() | {'repairs': [{'scene_number': 1, 'shot_number': 1,
        'reason': 'staging', 'set': {'description': 'Чемодан перемещён к арке.'}}]}
    fingerprint = ct._digest([ct._PLAN_VERSION, source])
    plan['_input_hash'] = fingerprint
    calls = []

    def responses(**kwargs):
        calls.append(json.loads(kwargs['prompt']))
        if len(calls) == 1:
            return json.dumps({'passed': False, 'issues': ['не нужно менять постановку']})
        return json.dumps({'repairs': []})

    monkeypatch.setattr(ct, 'call_openai_api', responses)
    result = ct._repair_candidate(base, source, plan, fingerprint)
    assert len(calls) == 2
    assert calls[1]['source']['screenplay']['screenplay'][0]['storyboard'][0]['description'] == \
        source['screenplay']['screenplay'][0]['storyboard'][0]['description']
    assert result['repairs'] == []
    assert source == original


def test_normalize_fills_missing_states_across_declared_transition_and_preserves_explicit_values():
    plan = _plan()
    plan['entities'][0]['states'].append({'id': 'held', 'description': 'В руке', 'space': 'station',
                                          'support': 'hand', 'holder': 'Герой', 'motion': 'moving'})
    plan['entities'][0]['transitions'] = [{'at': '1:1:end', 'from': 'on_platform', 'to': 'held',
                                           'evidence': 'Герой берёт чемодан с платформы'}]
    plan['frames']['1:1:end']['entities'][0].pop('state_id')
    plan['frames']['3:1:start']['entities'][0].pop('state_id')
    plan['frames']['3:1:end']['entities'][0].pop('state_id')
    plan['frames']['1:1:start']['entities'][0].pop('state_id')
    plan['frames']['1:1:start']['entities'][0]['visible'] = False
    screenplay = _screenplay()
    screenplay['screenplay'][0]['storyboard'][0]['description'] = 'Герой берёт чемодан с платформы.'
    normalized = ct._normalize_frame_keys(plan)
    states = {key: normalized['frames'][key]['entities'][0]['state_id']
              for key in ('1:1:start', '1:1:end', '3:1:start', '3:1:end')}
    assert states == {'1:1:start': 'on_platform', '1:1:end': 'held',
                      '3:1:start': 'held', '3:1:end': 'held'}
    assert normalized['frames']['1:1:start']['entities'][0]['visible'] is False
    assert ct.validate_continuity_plan(normalized, screenplay) is normalized
    normalized['frames']['3:1:end']['entities'][0]['state_id'] = 'on_platform'
    ct._normalize_frame_keys(normalized)
    with pytest.raises(ValueError, match='state changed without an event'):
        ct.validate_continuity_plan(normalized, screenplay)


@pytest.mark.parametrize('source_current', [False, True])
def test_linked_image_requires_current_source_even_if_target_is_newer(tmp_path, monkeypatch, source_current):
    from custom_tools.storybook import artist_batch_edit as artist, video_plan

    (tmp_path / '97_shots').mkdir()
    source = tmp_path / 'source.png'
    target = tmp_path / 'target.png'
    source.write_bytes(b'current source')
    target.write_bytes(b'stale target')
    source_item = {'output_path': str(source)}
    (tmp_path / '97_shots/shots.json').write_text(json.dumps({'items': [source_item]}))
    item = {'project_id': 'project', 'source_end_path': str(source), 'output_path': str(target)}
    monkeypatch.setattr(artist, 'safe_storybook_project_dir', lambda project: tmp_path)
    monkeypatch.setattr(video_plan, 'select_image_items', lambda project, items: items)
    monkeypatch.setattr(ct, 'image_continuity_hash', lambda item: 'managed')
    monkeypatch.setattr(ct, 'current_continuity_image', lambda frame:
                        source_current and frame['output_path'] == str(source))

    assert artist._handle_linked_shot(item) is source_current
    assert target.read_bytes() == (b'current source' if source_current else b'stale target')


def test_stale_plan_repaired_once_to_final_boundary_and_cached(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'project'
    for directory in ('91_screenplay', '20_bible', 'inputs'):
        (base / directory).mkdir(parents=True)
    screenplay = _screenplay()
    shot = screenplay['screenplay'][0]['storyboard'][0]
    shot['description'] = 'Чемодан открывается и затем закрывается.'
    screenplay_path = base / '91_screenplay/screenplay.json'
    screenplay_path.write_text(json.dumps(screenplay), encoding='utf-8')
    (base / '20_bible/characters.json').write_text('[]')
    (base / '20_bible/locations.json').write_text('[]')
    plan = _plan()
    plan['entities'][0]['states'].append(dict(plan['entities'][0]['states'][0], id='closed', description='Закрыт'))
    anchor = {'id': 'anchor', 'name': 'Anchor', 'kind': 'prop', 'world_id': 'station',
              'geometry': 'fixed construction', 'reference_path': '/reference/anchor.png',
              'states': [{'id': 'fixed', 'description': 'Fixed', 'space': 'station', 'support': 'ground',
                          'holder': None, 'motion': 'stationary'}], 'initial_state': 'fixed'}
    plan['entities'].append(anchor)
    plan['frames']['1:1:start']['entities'].append({'id': 'anchor', 'state_id': 'fixed', 'visible': False})
    plan['frames']['1:1:end']['entities'].append({'id': 'anchor', 'state_id': 'fixed', 'visible': False})
    source = ct._source(base)
    plan.update(version=ct._PLAN_VERSION - 1, input_hash=ct._digest([ct._PLAN_VERSION - 1, source]))
    (base / '20_bible/continuity_plan.json').write_text(json.dumps(plan))
    calls = []
    def repair(**kwargs):
        calls.append(kwargs)
        entity = copy.deepcopy(plan['entities'][0])
        entity['transitions'] = [{'at': '1:1:end', 'from': 'on_platform', 'to': 'closed',
                                  'evidence': shot['description']}]
        return json.dumps({'entity_replacements': [entity], 'frame_updates': {
            '1:1:end': {'entities': [{'id': 'suitcase', 'state_id': 'closed'}]},
            '3:1:start': {'entities': [{'id': 'suitcase', 'state_id': 'closed'}]},
            '3:1:end': {'entities': [{'id': 'suitcase', 'state_id': 'closed'}]}}})
    monkeypatch.setattr(ct, 'call_openai_api', repair)
    result = ct.storybook_continuity_plan_tool('session', 'project')
    assert result['status'] == 'success' and len(calls) == 1
    saved = json.loads((base / '20_bible/continuity_plan.json').read_text())
    assert saved['entities'][1] == anchor
    assert saved['entities'][0]['transitions'] == [{'at': '1:1:end', 'from': 'on_platform', 'to': 'closed', 'evidence': shot['description']}]
    assert saved['frames']['1:1:end']['entities'][0]['state_id'] == 'closed'
    assert json.loads(screenplay_path.read_text())['screenplay'][0]['storyboard'][0] == shot
    assert ct.storybook_continuity_plan_tool('session', 'project')['cached'] is True
    assert len(calls) == 1


@pytest.mark.parametrize('invalid', [False, True])
def test_current_plan_feedback_uses_existing_focused_repair(tmp_path, monkeypatch, invalid):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'project'
    for directory in ('91_screenplay', '20_bible', 'inputs'):
        (base / directory).mkdir(parents=True)
    screenplay = _screenplay()
    screenplay['screenplay'][0]['storyboard'][0]['description'] = 'Чемодан открывается, затем закрывается.'
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(screenplay))
    (base / '20_bible/characters.json').write_text('[]')
    (base / '20_bible/locations.json').write_text('[]')
    plan = _plan()
    other = {'id': 'anchor', 'name': 'Anchor', 'kind': 'prop', 'world_id': 'station',
             'geometry': 'fixed geometry', 'reference_path': '/reference/anchor.png',
             'states': [{'id': 'fixed', 'description': 'Fixed', 'space': 'station', 'support': 'ground',
                         'holder': None, 'motion': 'stationary'}], 'initial_state': 'fixed'}
    plan['entities'].append(other)
    plan['frames']['1:1:start']['entities'].append({'id': 'anchor', 'state_id': 'fixed', 'visible': False})
    if invalid:
        plan['frames']['1:1:end']['entities'][0]['state_id'] = 'unknown'
    source = ct._source(base)
    fingerprint = ct._digest([ct._PLAN_VERSION, source])
    plan.update(version=ct._PLAN_VERSION, input_hash=fingerprint, _input_hash=fingerprint, affected_shot_keys=['1-1'])
    (base / '20_bible/continuity_plan.json').write_text(json.dumps(plan))
    (base / '91_screenplay/continuity_candidate.json').write_text(json.dumps({'_input_hash': fingerprint, 'entities': [], 'frames': {}}))
    calls = []
    def model(**kwargs):
        payload = json.loads(kwargs['prompt'])
        calls.append(payload)
        assert [entity['id'] for entity in payload['candidate']['entities']] == ['suitcase']
        assert set(payload['candidate']['frames']) == {'1:1:start', '1:1:end', '3:1:start', '3:1:end'}
        assert 'suitcase' in payload['validation_error'] and 'hypotheses' in payload['validation_error']
        if invalid:
            assert 'Continuity state changed without an event' in payload['validation_error']
        entity = copy.deepcopy(plan['entities'][0])
        entity['states'].append(dict(entity['states'][0], id='closed', description='Closed'))
        entity['transitions'] = [{'at': '1:1:end', 'from': 'on_platform', 'to': 'closed',
                                  'evidence': screenplay['screenplay'][0]['storyboard'][0]['description']}]
        return json.dumps({'entity_replacements': [entity], 'frame_updates': {
            key: {'entities': [{'id': 'suitcase', 'state_id': 'closed'}]}
            for key in ('1:1:end', '3:1:start', '3:1:end')}})
    monkeypatch.setattr(ct, 'call_openai_api', model)
    monkeypatch.setattr(ct, '_review_staging_repairs', lambda *args: None)
    result = ct.storybook_continuity_plan_tool('session', 'project', review_findings=[{'entity_id': 'suitcase', 'observed': 'incorrect final state'}])
    assert result['status'] == 'success' and len(calls) == 1
    updated = json.loads((base / '20_bible/continuity_plan.json').read_text())
    assert updated['entities'][1] == other
    assert updated['frames']['1:1:end']['entities'][0]['state_id'] == 'closed'
    actual_source = ct._source(base)
    for scene in actual_source['screenplay']['screenplay']:
        if scene.get('scene_continuity_facts') == {}:
            scene.pop('scene_continuity_facts')
    assert actual_source == source
    assert ct.storybook_continuity_plan_tool('session', 'project')['cached'] is True
    assert ct.storybook_continuity_plan_tool('session', 'project', review_findings=[])['cached'] is True
    assert len(calls) == 1


def test_invalid_motion_repair_reports_enum_and_publishes_complete_entity(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'project'
    for directory in ('91_screenplay', '20_bible', 'inputs'):
        (base / directory).mkdir(parents=True)
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(_screenplay()))
    (base / '20_bible/characters.json').write_text('[]')
    (base / '20_bible/locations.json').write_text('[]')
    source = ct._source(base)
    fingerprint = ct._digest([ct._PLAN_VERSION, source])
    plan = _plan()
    plan['entities'][0]['states'][0]['motion'] = 'sometimes moving'
    other = {'id': 'anchor', 'name': 'Anchor', 'kind': 'prop', 'world_id': 'station',
             'geometry': 'fixed', 'reference_path': '/reference/anchor.png', 'initial_state': 'fixed',
             'states': [{'id': 'fixed', 'description': 'Fixed', 'space': 'station', 'support': 'ground',
                         'holder': None, 'motion': 'stationary'}]}
    plan['entities'].append(other)
    plan.update(version=ct._PLAN_VERSION, input_hash=fingerprint, _input_hash=fingerprint)
    (base / '20_bible/continuity_plan.json').write_text(json.dumps(plan))
    calls = []
    def repair(**kwargs):
        payload = json.loads(kwargs['prompt'])
        calls.append(payload)
        error = payload['validation_error']
        assert all(term in error for term in ('suitcase/on_platform', 'motion', 'sometimes moving', 'stationary', 'moving'))
        assert 'states.motion' in kwargs['system_prompt']
        fixed = copy.deepcopy(plan['entities'][0])
        fixed['states'][0]['motion'] = 'stationary'
        return json.dumps({'entity_replacements': [fixed]})
    monkeypatch.setattr(ct, 'call_openai_api', repair)
    result = ct.storybook_continuity_plan_tool('s', 'project', review_findings=[{'entity_id': 'suitcase', 'observed': 'check motion'}])
    assert result['status'] == 'success' and len(calls) == 1
    saved = json.loads((base / '20_bible/continuity_plan.json').read_text())
    assert saved['entities'][0]['states'][0]['motion'] == 'stationary'
    assert not saved['entities'][0].get('transitions')
    assert saved['entities'][1] == other
    current = ct._source(base)
    for scene in current['screenplay']['screenplay']:
        if scene.get('scene_continuity_facts') == {}:
            scene.pop('scene_continuity_facts')
    assert current == source
