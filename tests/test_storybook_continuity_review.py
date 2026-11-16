import json
import hashlib
from pathlib import Path

from PIL import Image
import pytest

from custom_tools.storybook import continuity as ct
from custom_tools.storybook import continuity_review as review

_REAL_REPAIR_MEDIA = review._repair_media


def _project(tmp_path, monkeypatch, sid=None):
    monkeypatch.setattr(review, '_repair_media', lambda *args: None)
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'project' / 'series' / sid if sid else tmp_path / 'project'
    for directory in ('20_bible', '20_bible/references', '91_screenplay', '97_shots', 'inputs'):
        (base / directory).mkdir(parents=True, exist_ok=True)
    screenplay = {'screenplay': [{'scene_number': 1, 'storyboard': [
        {'shot_number': 1, 'description': 'Чемодан стоит у вокзальной арки.'}
    ]}]}
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(screenplay), encoding='utf-8')
    (base / '20_bible/characters.json').write_text('[]', encoding='utf-8')
    (base / '20_bible/locations.json').write_text('[]', encoding='utf-8')
    frames = {
        f'1:1:{phase}': {'world_id': 'station', 'entities': [
            {'id': 'suitcase', 'state_id': 'at_arch', 'visible': True}
        ]} for phase in ('start', 'end')
    }
    plan = {'version': 1, 'entities': [{
        'id': 'suitcase', 'name': 'Чемодан', 'kind': 'prop', 'world_id': 'station',
        'states': [{'id': 'at_arch', 'description': 'У арки', 'space': 'station',
                    'support': 'ground', 'holder': None, 'motion': 'stationary'}],
        'initial_state': 'at_arch',
    }], 'frames': frames, 'repairs': [], 'character_identity': {}}
    plan['input_hash'] = ct._digest([ct._PLAN_VERSION, ct._source(base)])
    (base / '20_bible/continuity_plan.json').write_text(json.dumps(plan), encoding='utf-8')
    items = []
    for phase in ('start', 'end'):
        path = base / f'97_shots/{phase}.png'
        Image.new('RGB', (16, 16), 'gray').save(path)
        items.append({'project_id': 'project', 'scene_number': 1, 'shot_number': 1,
                      'shot_type': phase, 'output_path': str(path),
                      '_shot_frame_spec': {f'{phase}_state_spec': {}}})
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}), encoding='utf-8')
    for item in items:
        ct.record_continuity_image(item)
    return base, items


def test_image_review_keeps_boundary_timing_without_incidental_checklists(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    phase = {'t0_mode': 'frozen', 'pose_signature': 'The suitcase is closed before the handle is lifted.',
             'character_pose_signatures': {'Traveler': 'Hand has not touched the handle.'},
             'must_show': ['Incidental stitching on the suitcase.']}
    items[0]['_shot_frame_spec']['start_state_spec'] = phase
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    task = review._tasks(base, 'images')[0]
    context = next(c for c in task['contexts'] if c['phase'] == 'start')
    assert context['phase_state_spec'] == {'t0_mode': 'frozen'}
    assert next(c for c in task['contexts'] if c['phase'] == 'end')['phase_state_spec'] == {}
    requests = []
    monkeypatch.setattr(review, 'call_openai_api', lambda **kwargs: requests.append(kwargs) or json.dumps({
        'passed': True, 'violations': [], 'uncertain': []}))
    review._audit(task, task['group'], 0)
    assert 'Do not require later source events or reactions in START' in requests[0]['system_prompt']
    assert 'A still image shows one instant' in requests[0]['system_prompt']
    assert 'If the observation matches the required state, omit it' in requests[0]['system_prompt']
    payload = requests[0]['prompt'][0]['text']
    assert phase['pose_signature'] not in payload
    assert phase['character_pose_signatures']['Traveler'] not in payload


def test_images_review_accepts_and_then_uses_cache(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    assert review.continuity_acceptance(base, 'images')['status'] == 'review_required'
    calls = []
    monkeypatch.setattr(review, '_audit', lambda *args: calls.append(args) or {
        'passed': True, 'violations': [], 'uncertain': []})
    first = review.storybook_continuity_images_review_tool('session', 'project')
    assert first['status'] == 'success'
    assert calls
    count = len(calls)
    second = review.storybook_continuity_images_review_tool('session', 'project')
    assert second['status'] == 'success'
    assert len(calls) == count
    assert review.continuity_acceptance(base, 'images')['status'] == 'accepted'


def test_audit_requests_separate_image_and_video_directions(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    items[0]['video_prompt'] = 'Original movement'
    shots_path = base / '97_shots/shots.json'
    shots_path.write_text(json.dumps({'items': items}))
    requests = []
    monkeypatch.setattr('custom_tools.storybook.shots_prompt_qa._scene_render_review_image', lambda _: 'image')
    monkeypatch.setattr(review, 'call_openai_api', lambda **kwargs: requests.append(kwargs) or json.dumps({
        'passed': True, 'violations': [], 'uncertain': []}))
    task = review._tasks(base, 'images')[0]
    review._audit(task, task['group'], 0)
    items[0]['video_prompt'] = 'New movement'
    shots_path.write_text(json.dumps({'items': items}))
    changed = review._tasks(base, 'images')[0]
    review._audit(changed, changed['group'], 0)
    assert requests[0]['prompt'] == requests[1]['prompt']
    assert task['input_hash'] == changed['input_hash']
    instruction = requests[0]['system_prompt']
    assert 'SOURCE PRIORITY (highest to lowest)' in instruction
    assert 'visible=false does not forbid' in instruction
    assert 'continuity overrides incompatible legacy phase descriptions or prompts' not in instruction
    assert 'part counts and arrangement must be supported by registered continuity.geometry or primary source' in instruction
    assert 'incidental reference details and decor are not new fixed constraints' in instruction
    assert 'Strictly preserve registered parts' in instruction
    assert 'Opposite viewpoints reverse their projected left/right order' in instruction
    assert 'reference-sheet overlays' in instruction
    image_payload = json.loads(requests[0]['prompt'][0]['text'])
    assert 'video_prompt' not in image_payload and 'video_mode' not in image_payload
    assert all('current_video_prompt' not in frame for frame in image_payload['expected_frames'])
    video = dict(changed, kind='videos', group=[dict(changed['group'][0], video_prompt='Combined motion', video_mode='references')],
                 contexts=[dict(context, current_video_prompt='New movement') for context in changed['contexts']])
    review._audit(video, video['group'], 0)
    video_payload = json.loads(requests[2]['prompt'][0]['text'])
    assert video_payload['video_prompt'] == 'Combined motion'
    assert video_payload['video_mode'] == 'references'
    assert all(frame['current_video_prompt'] == 'New movement' for frame in video_payload['expected_frames'])
    assert 'multiple independent camera views or pose studies' in requests[2]['system_prompt']
    assert 'Do not report uncertainty because an opening, ending or event lies outside this chunk' in requests[2]['system_prompt']


def test_changed_png_bytes_require_new_review(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(review, '_audit', lambda *args: {'passed': True, 'violations': [], 'uncertain': []})
    review.storybook_continuity_images_review_tool('session', 'project')
    with open(items[0]['output_path'], 'ab') as output:
        output.write(b'changed')
    ct.record_continuity_image(items[0])
    assert review.continuity_acceptance(base, 'images')['status'] == 'review_required'


@pytest.mark.parametrize('kind', ['images', 'videos'])
def test_audit_sends_separate_labeled_images(tmp_path, monkeypatch, kind):
    import base64
    import io
    base, _ = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    master = base / 'master.png'
    frame = base / 'frame.jpg'
    Image.new('RGB', (40, 24), 'red').save(master)
    Image.new('RGB', (48, 32), 'blue').save(frame)
    task.update(kind=kind, masters=[str(master)], video_duration_s=2, final_sample_time_s=1.96)
    pictures = [dict(scene_number='VIDEO', shot_number=0, shot_type='t=0.00s', output_path=str(frame))]
    before = frame.read_bytes()
    calls = []
    monkeypatch.setattr('custom_tools.storybook.shots_prompt_qa._scene_render_review_image',
                        lambda _: pytest.fail('Video frames must not be combined into a review sheet'))
    monkeypatch.setattr(review, 'call_openai_api', lambda **kwargs: calls.append(kwargs) or json.dumps({
        'passed': True, 'violations': [], 'uncertain': []}))
    review._audit(task, pictures, 0)
    request = calls[0]
    assert 'image_url' not in request
    blocks = request['prompt']
    assert [block['type'] for block in blocks] == ['text', 'text', 'image_url', 'text', 'image_url']
    if kind == 'videos':
        assert json.loads(blocks[0]['text'])['final_sample_time_s'] == 1.96
    assert blocks[1]['text'] == 'VIDEO.0 t=0.00s'
    assert blocks[3]['text'].startswith('LANDMARK.')
    for block, size, channel in [(blocks[2], (48, 32), 2), (blocks[4], (40, 24), 0)]:
        with Image.open(io.BytesIO(base64.b64decode(block['image_url']['url'].split(',', 1)[1]))) as image:
            assert image.format == 'JPEG' and image.size == size
            assert image.getpixel((10, 10))[channel] > 240
    assert frame.read_bytes() == before


def test_failed_or_uncertain_audit_is_not_accepted(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(review, '_audit', lambda *args: {
        'passed': False, 'violations': [], 'uncertain': ['unclear object state']})
    result = review._review('session', 'project', 'images', True, repair=False)
    assert result['status'] == 'error'
    assert review.continuity_acceptance(base, 'images')['status'] == 'review_required'


def test_legacy_image_requires_visual_review_before_becoming_current(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    (base / '97_shots/start.continuity.json').unlink()
    result = review.continuity_acceptance(base, 'images')
    assert result['status'] == 'review_required'
    assert not ct.current_continuity_image(items[0])
    monkeypatch.setattr(review, '_audit', lambda *args: {'passed': True, 'violations': [], 'uncertain': []})
    assert review.storybook_continuity_images_review_tool('session', 'project')['status'] == 'success'
    assert ct.current_continuity_image(items[0])
    assert json.loads((base / '97_shots/start.continuity.json').read_text())['basis'] == 'visual_review'


def test_source_change_invalidates_continuity_plan(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    screenplay = json.loads((base / '91_screenplay/screenplay.json').read_text())
    screenplay['screenplay'][0]['storyboard'][0]['description'] = 'Чемодан исчезает.'
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(screenplay), encoding='utf-8')
    result = review.continuity_acceptance(base, 'images')
    assert result['status'] == 'invalid_inputs'
    assert 'stale' in result['error']


def test_r2v_selects_only_existing_boundary_images_and_rebases_dependencies(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    start = base / '97_shots/a-start.png'
    end = base / '97_shots/b-end.png'
    start.write_bytes(b'start')
    end.write_bytes(b'end')
    items = []
    for shot in (1, 2):
        for phase in ('start', 'end'):
            path = base / f'97_shots/{shot}-{phase}.png'
            if (shot, phase) == (1, 'start'):
                path = start
            elif (shot, phase) == (2, 'end'):
                path = end
            item = {'scene_number': 1, 'shot_number': shot, 'shot_type': phase,
                    'output_path': str(path), 'image_paths': [str(path)]}
            if (shot, phase) == (2, 'end'):
                item['image_paths'] = [str(base / '97_shots/2-start.png')]
            items.append(item)
    (base / '97_shots/video_plan.json').write_text(json.dumps({'units': [{
        'shot_keys': ['1-1', '1-2'], 'start_reference_path': str(start),
        'end_reference_path': str(end),
    }]}), encoding='utf-8')
    from custom_tools.storybook.video_plan import select_image_items
    selected = select_image_items('project', items)
    assert {(item['shot_number'], item['shot_type']) for item in selected} == {(1, 'start'), (2, 'end')}
    bend = next(item for item in selected if item['shot_number'] == 2)
    assert bend['image_paths'] == [str(start)]


def test_video_tasks_hash_clip_and_existing_anchors_only(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    clip = base / '97_shots/unit.mp4'
    clip.write_bytes(b'mock movie')
    (base / '97_shots/video_plan.json').write_text(json.dumps({'units': [{
        'shot_keys': ['1-1'], 'duration_s': 5, 'video_path': str(clip), 'continuity_hash': 'state',
        'video_prompt': 'run', 'reference_image_paths': [], 'resolution': '480p',
    }]}), encoding='utf-8')
    monkeypatch.setattr(review, 'unit_continuity_hash', lambda base, keys: 'state')
    monkeypatch.setattr(ct, 'unit_continuity_hash', lambda base, keys: 'state')
    monkeypatch.setattr(review, 'validate_current_continuity', lambda base: {'frames': {}})
    tasks = review._tasks(base, 'videos')
    assert tasks and tasks[0]['media'] == [str(clip.resolve())]
    assert tasks[0]['media'] and tasks[0]['media'][0].endswith('unit.mp4')


def test_normalize_frame_keys_only_removes_scene_prefix_and_rejects_collision():
    plan = {'frames': {'scene:1:2:start': {'world_id': 'x'}, '1:2:end': {'world_id': 'x'}},
            'entities': [{'id': 'x', 'initial_state': 'initial',
                          'transitions': [{'at': 'scene:1:2:start', 'to': 'changed'}]}]}
    normalized = ct._normalize_frame_keys(plan)
    assert set(normalized['frames']) == {'1:2:start', '1:2:end'}
    assert normalized['frames']['1:2:start']['world_id'] == 'x'
    assert normalized['entities'][0]['transitions'][0]['at'] == '1:2:start'
    with pytest.raises(ValueError, match='Duplicate normalized'):
        ct._normalize_frame_keys({'frames': {'scene:1:2:start': {}, '1:2:start': {}}, 'entities': []})


def test_reference_locations_reject_unknown_and_missing_landmark_reference():
    source = {'locations': [{'name': 'Station', 'reference_image_path': '/station.png'}]}
    unknown = {'entities': [{'id': 'arch', 'kind': 'landmark', 'locations': ['Unknown'],
                            'reference_location': 'Station'}]}
    with pytest.raises(ValueError, match='Unknown continuity location'):
        ct._validate_reference_locations(unknown, source)
    missing = {'entities': [{'id': 'arch', 'kind': 'landmark', 'locations': ['Station'],
                             'reference_location': 'Station'}]}
    source['locations'][0].pop('reference_image_path')
    with pytest.raises(ValueError, match='canonical reference'):
        ct._validate_reference_locations(missing, source)


def test_pending_image_findings_filters_current_failed_media_and_state(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    media = {item['output_path']: hashlib.sha256(open(item['output_path'], 'rb').read()).hexdigest()
             for item in items}
    state_hash = ct.unit_continuity_hash(base, ['1-1'])
    report = {'tasks': {'task': {'id': '1-1', 'passed': False, 'state_hash': state_hash,
        'review_version': review._REVIEW_VERSION,
        'media_hashes': media, 'verdicts': [{'violations': [
            {'frame_key': '1:1:start', 'observed': 'wrong', 'required': 'right'},
            {'frame_key': '1:1:middle', 'observed': 'bad', 'required': 'good'}]}]}}}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report), encoding='utf-8')
    assert review.pending_image_findings(base, {'1-1'}) == [{
        'scene_number': 1, 'shot_number': 1, 'shot_type': 'start',
        'observed': 'wrong', 'required': 'right'}]
    report['tasks']['task']['review_version'] = review._REVIEW_VERSION - 1
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report), encoding='utf-8')
    assert review.pending_image_findings(base, {'1-1'}) == []
    report['tasks']['task']['review_version'] = review._REVIEW_VERSION
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report), encoding='utf-8')
    with open(items[0]['output_path'], 'ab') as output:
        output.write(b'changed')
    assert review.pending_image_findings(base, {'1-1'}) == []


def test_image_tasks_include_only_phases_with_attached_current_images(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    (base / '91_screenplay').mkdir()
    (base / '20_bible').mkdir()
    (base / '91_screenplay/screenplay.json').write_text(json.dumps({'screenplay': [{'scene_number': 1, 'storyboard': [{'shot_number': 1}, {'shot_number': 2}]}]}))
    (base / '20_bible/characters.json').write_text('[]')
    (base / '20_bible/locations.json').write_text('[]')
    attached = []
    items = []
    for shot in (1, 2):
        for phase in ('start', 'end'):
            path = base / f'97_shots/{shot}-{phase}.png'
            if (shot, phase) in {(1, 'start'), (2, 'end')}:
                path.write_bytes(b'image')
                attached.append((shot, phase, path))
            items.append({'scene_number': 1, 'shot_number': shot, 'shot_type': phase,
                          'output_path': str(path)})
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}), encoding='utf-8')
    contexts = {(shot, phase): {'world_id': 'station', 'entities': []}
                for shot, phase, _ in attached}

    def bind(base, values):
        return [dict(item, _continuity={phase: contexts.get((item['shot_number'], phase), {})
                                        for phase in ('start', 'end')}) for item in values]

    monkeypatch.setattr(review, 'validate_current_continuity', lambda base: {'frames': {}})
    monkeypatch.setattr(review, 'bind_continuity_to_items', bind)
    from custom_tools.storybook import video_plan
    monkeypatch.setattr(video_plan, 'select_image_items', lambda project_id, values:
                        [item for item in values if (item['shot_number'], item['shot_type']) in {
                            (1, 'start'), (2, 'end')}])
    monkeypatch.setattr(review, 'unit_continuity_hash', lambda base, keys: 'state')
    tasks = review._tasks(base, 'images')
    assert tasks
    assert {(context['shot_number'], context['phase']) for task in tasks for context in task['contexts']} == {
        (1, 'start'), (2, 'end')}
    assert len(tasks) == 2


def _repair_scope_fixture(tmp_path, monkeypatch, count=3):
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    items = []
    for shot in range(1, count + 1):
        for phase in ('start', 'end'):
            path = base / f'97_shots/{shot}-{phase}.png'
            path.write_bytes(f'{shot}-{phase}'.encode())
            items.append({'scene_number': 1, 'shot_number': shot, 'shot_type': phase,
                          'output_path': str(path)})
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}), encoding='utf-8')
    calls = {'qa': [], 'background_plan': [], 'background_refs': [], 'artist': []}
    monkeypatch.setattr(review, 'safe_storybook_project_dir', lambda project: base)
    monkeypatch.setattr(review, 'bind_continuity_to_items', lambda base, values: values)
    monkeypatch.setattr('custom_tools.storybook.video_plan.select_image_items', lambda project, values: values)
    monkeypatch.setattr('custom_tools.storybook.shots_prompt_qa.shots_prompt_qa_tool',
                        lambda **kwargs: calls['qa'].append(kwargs) or {'items': items})
    monkeypatch.setattr('custom_tools.storybook.background_references.storybook_background_plan_tool',
                        lambda *args, **kwargs: calls['background_plan'].append((args, kwargs)) or args[2])
    monkeypatch.setattr('custom_tools.storybook.background_references.storybook_background_references_tool',
                        lambda *args, **kwargs: calls['background_refs'].append((args, kwargs)) or args[2])
    monkeypatch.setattr('custom_tools.storybook.artist_batch_edit.artist_agent_batch_edit_tool',
                        lambda **kwargs: calls['artist'].append(kwargs) or json.dumps([{'ok': True} for _ in kwargs['items']['items']]))
    return base, items, calls


def test_repair_does_not_repeat_identical_edits_with_reworded_observations(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch, count=1)
    finding = {'frame_key': '1:1:end', 'entity_id': 'prop',
               'observed': 'wrong support', 'required': 'Restore ground support'}
    findings = [finding, dict(finding, observed='still on the wrong support'),
                dict(finding, required='Restore the blue color')]
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps({'tasks': {'hash': {
        'verdicts': [{'passed': False, 'violations': findings, 'uncertain': []}]}}}))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-1', 'input_hash': 'hash', 'group': items}], 'images', 's')
    assert [call['items']['items'][0]['_continuity_edit_findings'][0]['required']
            for call in calls['artist']] == ['Restore ground support', 'Restore the blue color']


def test_repair_receives_registered_physical_context(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    finding = {'frame_key': '1:1:end', 'entity_id': 'suitcase', 'observed': 'wrong support', 'required': 'Restore ground support'}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps({'tasks': {task['input_hash']: {
        'verdicts': [{'passed': False, 'violations': [finding], 'uncertain': []}]}}}))
    calls = []
    monkeypatch.setattr('custom_tools.storybook.artist_batch_edit.artist_agent_batch_edit_tool',
                        lambda **kwargs: calls.append(kwargs) or json.dumps([{'ok': True}]))
    _REAL_REPAIR_MEDIA(base, [task], 'images', 's')
    selected = calls[0]['items']['items'][0]
    assert selected['_continuity']['end']['states']['suitcase']['support'] == 'ground'
    assert selected['_continuity_edit_findings'][0]['entity_id'] == 'suitcase'


def test_repair_scope_uses_only_localized_frame_violation_but_both_phases(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch)
    report = {'tasks': {'group': {'id': '1-1|1-2|1-3', 'verdicts': [
        {'violations': [{'frame_key': '1:2:end', 'observed': 'wrong', 'required': 'right'}], 'uncertain': []}]}}}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report))
    task = {'id': '1-1|1-2|1-3', 'input_hash': 'group', 'group': items}
    _REAL_REPAIR_MEDIA(base, [task], 'images', 'session')
    artist_items = calls['artist'][0]['items']['items']
    assert {(item['shot_number'], item['shot_type']) for item in artist_items} == {(2, 'end')}


@pytest.mark.parametrize('both_bad', [False, True])
def test_local_edit_uses_reviewed_good_companion_instead_of_unrelated_refs(tmp_path, monkeypatch, both_bad):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch, count=1)
    items[0]['reference_image_paths'] = ['unrelated-location']
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    findings = [{'frame_key': '1:1:start', 'entity_id': 'document',
                 'observed': 'Document print changed', 'required': 'Preserve the same print as END'}]
    if both_bad:
        findings.append(dict(findings[0], frame_key='1:1:end'))
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps({'tasks': {'group': {
        'verdicts': [{'passed': False, 'violations': findings, 'uncertain': []}]}}}))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-1', 'input_hash': 'group', 'group': items}], 'images', 's')
    refs = calls['artist'][0]['items']['items'][0]['reference_image_paths']
    assert (items[1]['output_path'] in refs) is not both_bad
    if not both_bad:
        assert refs == [items[0]['output_path'], items[1]['output_path']]


def test_repair_restores_unaffected_phase_after_artist_writes_group(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch)
    report = {'tasks': {'group': {'id': '1-1|1-2|1-3', 'verdicts': [
        {'violations': [{'frame_key': '1:2:end', 'observed': 'wrong', 'required': 'right'}], 'uncertain': []}]}}}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report))

    def artist(**kwargs):
        for item in kwargs['items']['items']:
            Path(item['output_path']).write_bytes(b'artist-output')
        return json.dumps([{'ok': True} for _ in kwargs['items']['items']])

    monkeypatch.setattr('custom_tools.storybook.artist_batch_edit.artist_agent_batch_edit_tool', artist)
    task = {'id': '1-1|1-2|1-3', 'input_hash': 'group', 'group': items}
    _REAL_REPAIR_MEDIA(base, [task], 'images', 'session')
    assert (base / '97_shots/2-start.png').read_bytes() == b'2-start'
    assert (base / '97_shots/2-end.png').read_bytes() == b'artist-output'


def test_repair_scope_expands_only_transitive_copy_dependencies(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch, count=5)
    by_key = {(item['shot_number'], item['shot_type']): item for item in items}
    by_key[(3, 'start')]['copy_from_previous_end'] = True
    by_key[(3, 'start')]['source_end_path'] = by_key[(2, 'end')]['output_path']
    by_key[(4, 'start')]['copy_from_previous_end'] = True
    by_key[(4, 'start')]['source_end_path'] = by_key[(3, 'start')]['output_path']
    by_key[(5, 'start')]['reference_image_paths'] = [by_key[(2, 'end')]['output_path']]
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}), encoding='utf-8')
    report = {'tasks': {'one': {'id': '1-2', 'verdicts': [
        {'violations': [{'frame_key': '1:2:end', 'observed': 'wrong', 'required': 'right'}], 'uncertain': []}]}}}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-2', 'input_hash': 'one', 'group': [by_key[(2, 'end')]]}], 'images', 'session')
    assert {(item['shot_number'], item['shot_type']) for item in calls['artist'][0]['items']['items']} == {(2, 'end')}


def test_uncertain_unlocalized_review_does_not_repair_group(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch)
    report = {'tasks': {'group': {'id': '1-1|1-2|1-3', 'verdicts': [
        {'violations': [], 'uncertain': ['critical unresolved identity']}]}}}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-1|1-2|1-3', 'input_hash': 'group', 'group': items}], 'images', 'session')
    assert not calls['qa'] and not calls['artist']
    assert not calls['background_plan'] and not calls['background_refs']


def test_event_frame_indices_scale_deduplicate_and_bound():
    assert review._video_sample_indices([4, 4], 4, 2, 30, 60) == [0, 15, 30, 45, 59]
    assert review._video_sample_indices([0], 4, 1, 60, 120) == [0, 30, 119]
    assert review._video_sample_indices([4], 4, 4, 60, 240) == [0, 30, 60, 90, 120, 150, 180, 210, 239]
    indices = review._video_sample_indices([2, 2], 4, 2, 10, 20)
    assert len(indices) == 5 and len(indices) == len(set(indices)) and indices[0] == 0 and indices[-1] == 19


def test_video_event_sampling_and_cache(tmp_path, monkeypatch):
    import shutil
    import subprocess
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg unavailable')
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    clip = base / '97_shots/test.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=32x32:r=25:d=1',
                    '-f', 'lavfi', '-i', 'color=blue:s=32x32:r=25:d=1',
                    '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0', '-c:v', 'mpeg4', str(clip)], check=True)
    task = {'id': '1-1', 'input_hash': 'event', 'state_hash': 'state', 'media': [str(clip)],
            'events': [2], 'planned_duration': 4, 'contexts': [{'time_s': 2}], 'group': [{}]}
    monkeypatch.setattr(review, 'safe_storybook_project_dir', lambda _: base)
    monkeypatch.setattr(review, '_tasks', lambda *_: [task])
    monkeypatch.setattr(review, 'continuity_acceptance', lambda *_: {'passed': True})
    calls = []
    def audit(current, pictures, chunk):
        calls.append((current, [p['shot_type'] for p in pictures]))
        for picture in pictures:
            with Image.open(picture['output_path']) as frame:
                red, _, blue = frame.convert('RGB').getpixel((16, 16))
            if picture['shot_type'] == 't=0.96s':
                assert red > blue + 100
            if picture['shot_type'] == 't=1.04s':
                assert blue > red + 100
        return {'passed': True, 'violations': [], 'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    assert review._review('s', 'project', 'videos', True)['status'] == 'success'
    labels = [label for _, pictures in calls for label in pictures]
    assert labels == ['t=0.00s', 't=0.48s', 't=1.00s', 't=1.52s', 't=1.96s']
    assert all(current['contexts'][0]['time_s'] == 1 for current, _ in calls)
    assert all(current['video_duration_s'] == 2 and current['final_sample_time_s'] == 1.96 for current, _ in calls)
    assert task['contexts'][0]['time_s'] == 2
    count = len(calls)
    review._review('s', 'project', 'videos', True)
    assert len(calls) == count
    path = base / '97_shots/continuity_videos_review.json'
    report = json.loads(path.read_text())
    report['tasks']['event']['passed'] = False
    path.write_text(json.dumps(report))
    monkeypatch.setattr(review, 'continuity_acceptance', lambda *_: {'passed': False})
    assert review._review('s', 'project', 'videos', True, repair=False)['status'] == 'error'
    assert len(calls) == count
    task['input_hash'] = 'changed-event'
    review._review('s', 'project', 'videos', True, repair=False)
    assert len(calls) > count


def test_video_tasks_event_times_and_fingerprint(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    items += [dict(item, shot_number=2) for item in items]
    screenplay_path = base / '91_screenplay/screenplay.json'
    screenplay = json.loads(screenplay_path.read_text())
    screenplay['screenplay'][0]['storyboard'].append({'shot_number': 2, 'description': 'second shot'})
    screenplay_path.write_text(json.dumps(screenplay))
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    clip = base / '97_shots/unit.mp4'
    clip.write_bytes(b'video')
    monkeypatch.setattr(review, 'unit_continuity_hash', lambda *_: 'state')
    monkeypatch.setattr(review, 'bind_continuity_to_items', lambda _, rows: [dict(row, _continuity={}) for row in rows])
    from custom_tools.storybook import video_plan
    monkeypatch.setattr(video_plan, 'apply_video_plan', lambda _, starts: [dict(starts[0], covered_shot_keys=['1-1', '1-2'], video_path=str(clip))])
    monkeypatch.setattr(video_plan, '_duration', lambda _: 2)
    plan = {'entities': [{'transitions': [{'at': '1:1:end'}, {'at': '1:2:start'}, {'at': '9:9:end'}]}]}
    monkeypatch.setattr(review, 'validate_current_continuity', lambda _: plan)
    task = review._tasks(base, 'videos')[0]
    assert task['events'] == [2]
    plan['entities'][0]['transitions'] = [{'at': '1:2:end'}]
    boundary = review._tasks(base, 'videos')[0]
    assert boundary['events'] == [4]
    plan['entities'][0]['transitions'] = []
    no_events = review._tasks(base, 'videos')[0]
    assert no_events['events'] == []
    assert task['input_hash'] != no_events['input_hash']
    from pathlib import Path
    hashes = {path: review._file_hash(path) for path in set(no_events['media']) | {str(Path(item['output_path']).resolve()) for item in items}}
    assert no_events['input_hash'] == ct._digest([review._REVIEW_VERSION, 'videos', no_events['contexts'], hashes, [None], 'video_sampling_v3'])


def test_video_finding_checks_only_named_source_boundary(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    anchors = [str(Path(item['output_path']).resolve()) for item in items]
    task = {'id': '1-1', 'input_hash': 'video', 'anchors': anchors, 'group': [items[0]]}
    finding = {'frame_key': 'VIDEO.0 t=0.00s', 'source_frame_key': '1:1:start',
               'observed': 'extra object', 'required': 'remove it'}
    report = {'tasks': {'video': {'passed': False, 'verdicts': [
        {'passed': False, 'violations': [finding], 'uncertain': []}]}}}
    image_task = {'id': '1-1', 'group': items}
    monkeypatch.setattr(review, '_tasks', lambda _, kind: [image_task] if kind == 'images' else [task])
    calls = []
    def fake_review(session, project, kind, enable, **kwargs):
        calls.append((kind, kwargs))
        return {'status': 'success', 'acceptance': {'passed': True}}
    monkeypatch.setattr(review, '_review', fake_review)
    def fake_repair(base, selected, kind, session, on_attempt):
        calls.append(('repair', kind))
        on_attempt({'1-1'})
    monkeypatch.setattr(review, '_repair_media', fake_repair)
    result = review._repair_round('s', 'project', 'videos', True, base, [task], report, {})
    assert result['status'] == 'success'
    assert calls[0][0] == 'images' and calls[0][1]['repair'] is False
    assert calls[0][1]['shot_keys'] == ['1-1']
    assert [f['frame_key'] for f in calls[0][1]['review_findings']] == ['1:1:start']
    assert calls[1] == ('repair', 'videos')


def test_video_temporal_finding_never_checks_images(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    task = {'id': '1-1', 'input_hash': 'video', 'anchors': [i['output_path'] for i in items], 'group': [items[0]]}
    report = {'tasks': {'video': {'passed': False, 'verdicts': [{'passed': False,
        'violations': [{'frame_key': 'VIDEO.0', 'observed': 'motion stops', 'required': 'continue moving'}],
        'uncertain': []}]}}}
    monkeypatch.setattr(review, '_tasks', lambda _, kind: [{'group': items}] if kind == 'images' else [task])
    calls = []
    monkeypatch.setattr(review, '_review', lambda s, p, kind, e, **kw: calls.append(kind) or
                        {'status': 'success', 'acceptance': {'passed': True}})
    monkeypatch.setattr(review, '_repair_media', lambda b, t, k, s, cb: cb({'1-1'}))
    review._repair_round('s', 'project', 'videos', True, base, [task], report, {})
    assert calls == ['videos']


@pytest.mark.parametrize('mode', ['keyframes', 'references'])
@pytest.mark.parametrize('changed_dependency', [False, True])
def test_video_repair_rewrites_compactly_and_generates_once(tmp_path, monkeypatch, mode, changed_dependency):
    import sys
    import types
    from custom_tools.storybook import video_contract
    base, items = _project(tmp_path, monkeypatch)
    items[0]['video_prompt'] = 'Original action with Image 1 and Image 2'
    if changed_dependency:
        items.append(dict(items[0], shot_number=2, video_prompt='Keep dependent action'))
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    (base / '97_shots/continuity_videos_review.json').write_text(json.dumps({
        'tasks': {'hash': {'verdicts': [{'violations': ['object moved'], 'uncertain': []}]},
                  'dependent': {'passed': True, 'verdicts': []}}}))
    calls, generated = [], []
    monkeypatch.setattr(review, 'call_openai_api', lambda **kwargs: calls.append(kwargs) or json.dumps({'video_prompt': 'Fixed action'}))
    monkeypatch.setattr(video_contract, '_active_video_tool_name', lambda: 'test_video_generator')
    module = types.ModuleType('custom_tools.storybook.test_video_generator')
    def generate(session_id, project_id, shot_keys, **kwargs):
        generated.append((session_id, project_id, shot_keys, kwargs))
    module.test_video_generator = generate
    monkeypatch.setitem(sys.modules, module.__name__, module)
    snapshot = dict(items[0], video_prompt='Stale prompt from before the source repair')
    if mode == 'references':
        snapshot.update(video_mode='references', covered_shot_keys=['1-1'])
        (base / '97_shots/video_plan.json').write_text(json.dumps({'units': [
            {'shot_keys': ['1-1'], 'video_prompt': items[0]['video_prompt']}]}))
    task = {'id': '1-1', 'input_hash': 'hash', 'group': [snapshot], 'contexts': []}
    tasks = [task]
    if changed_dependency:
        tasks.append({'id': '1-2', 'input_hash': 'dependent', 'group': [items[-1]], 'contexts': []})
    _REAL_REPAIR_MEDIA(base, tasks, 'videos', 'session')
    assert len(calls) == 0 and len(generated) == 1
    assert generated[0][2] == (['1-1', '1-2'] if changed_dependency else ['1-1'])
    assert generated[0][3]['skip_prompt_enhancement'] is True
    if mode == 'references':
        assert 'object moved' in json.loads((base / '97_shots/video_plan.json').read_text())['units'][0]['video_prompt']
    else:
        assert 'object moved' in json.loads((base / '97_shots/shots.json').read_text())['items'][0]['video_prompt']
    if changed_dependency:
        assert json.loads((base / '97_shots/shots.json').read_text())['items'][-1]['video_prompt'] == 'Keep dependent action'


def test_video_repair_preserves_image_acceptance(tmp_path, monkeypatch):
    import sys
    import types
    from custom_tools.storybook import video_contract
    base, items = _project(tmp_path, monkeypatch)
    items[0]['video_prompt'] = 'Original direction'
    shots_path = base / '97_shots/shots.json'
    shots_path.write_text(json.dumps({'items': items}))
    monkeypatch.setattr(review, '_audit', lambda *args: {'passed': True, 'violations': [], 'uncertain': []})
    assert review.storybook_continuity_images_review_tool('session', 'project')['status'] == 'success'
    (base / '97_shots/continuity_videos_review.json').write_text(json.dumps({
        'tasks': {'hash': {'verdicts': [{'violations': ['object moved'], 'uncertain': []}]}}}))
    monkeypatch.setattr(review, 'call_openai_api', lambda **kwargs: json.dumps({'video_prompt': 'Revised direction'}))
    monkeypatch.setattr(video_contract, '_active_video_tool_name', lambda: 'test_video_acceptance')
    module = types.ModuleType('custom_tools.storybook.test_video_acceptance')
    calls = []
    def generate(session_id, project_id, shot_keys, **kwargs):
        review.require_continuity_acceptance(base, 'images')
        calls.append(shot_keys)
    module.test_video_acceptance = generate
    monkeypatch.setitem(sys.modules, module.__name__, module)
    task = {'id': '1-1', 'input_hash': 'hash', 'group': [items[0]], 'contexts': []}
    _REAL_REPAIR_MEDIA(base, [task], 'videos', 'session')
    assert calls == [['1-1']]
    assert review.continuity_acceptance(base, 'images')['status'] == 'accepted'
    assert 'current_video_prompt' not in review._tasks(base, 'images')[0]['contexts'][0]


def test_source_shot_supersedes_derived_checklists(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    items[0]['_shot_frame_spec']['start_state_spec'] = {
        'visible_characters': ['Actor'], 'must_show': ['derived'], 'must_not_show': ['derived'],
        'world_physics': 'derived', 'camera_anchor': 'derived'}
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    context = review._tasks(base, 'images')[0]['contexts'][0]
    assert context['source_shot']['description'] == 'Чемодан стоит у вокзальной арки.'
    assert 'camera_plan' in context['source_shot']
    assert context['visible_characters'] == ['Actor']
    assert context['continuity']['entities']
    assert 'phase_state' not in context
    assert all(key not in context for key in ('must_show', 'must_not_show', 'world_physics', 'camera_anchor'))


def test_prior_findings_rechecked_with_explicit_dismissal_and_cache(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    finding = {'frame_key': '1:1:end', 'entity_id': 'suitcase', 'observed': 'crop', 'required': 'whole body'}
    media_hashes = {p: review._file_hash(p) for p in task['media']}
    old = {'id': task['id'], 'state_hash': task['state_hash'], 'media_hashes': media_hashes,
           'review_version': review._REVIEW_VERSION, 'verdicts': [{'violations': [finding, finding]}]}
    stale_state = dict(old, state_hash='other', verdicts=[{'violations': [{'frame_key': 'stale-state'}]}])
    stale_media = dict(old, media_hashes={}, verdicts=[{'violations': [{'frame_key': 'stale-media'}]}])
    report_path = base / '97_shots/continuity_images_review.json'
    report_path.write_text(json.dumps({'tasks': {'old': old, 'other-state': stale_state, 'other-media': stale_media}}))
    requests = []
    monkeypatch.setattr('custom_tools.storybook.shots_prompt_qa._scene_render_review_image', lambda _: 'image')
    responses = [{'passed': True, 'violations': [], 'uncertain': []},
                 {'passed': True, 'violations': [], 'uncertain': [], 'dismissed': [{'index': 0, 'reason': 'Only a discarded crop requirement'}]}]
    monkeypatch.setattr(review, 'call_openai_api', lambda **kwargs: requests.append(json.loads(kwargs['prompt'][0]['text'])) or json.dumps(responses.pop(0)))
    result = review._review('s', 'project', 'images', True, repair=False)
    assert result['status'] == 'error'
    record = json.loads(report_path.read_text())['tasks'][task['input_hash']]
    assert record['verdicts'][0]['violations'] == [finding]
    assert requests[0]['prior_findings'] == [{'index': 0, 'finding': finding}]
    assert review._review('s', 'project', 'images', True, repair=False)['status'] == 'success'
    assert requests[1]['prior_findings'] == [{'index': 0, 'finding': finding}]
    assert review._review('s', 'project', 'images', True, repair=False)['status'] == 'success'
    assert len(requests) == 2


def test_repair_attaches_only_current_frame_findings_transiently(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch)
    finding = {'frame_key': '1:2:end', 'observed': 'wrong position', 'required': 'correct position'}
    report = {'tasks': {'group': {'id': '1-1|1-2|1-3', 'verdicts': [{'violations': [finding], 'uncertain': []}]}}}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report))
    before = (base / '97_shots/shots.json').read_bytes()
    _REAL_REPAIR_MEDIA(base, [{'id': '1-1|1-2|1-3', 'input_hash': 'group', 'group': items}], 'images', 's')
    assert not calls['qa'] and not calls['background_plan'] and not calls['background_refs']
    assert (base / '97_shots/shots.json').read_bytes() == before
    artist_items = calls['artist'][0]['items']['items']
    assert len(artist_items) == 1
    targeted = [item for item in artist_items if item.get('_continuity_edit_findings')]
    assert len(targeted) == 1 and targeted[0]['shot_type'] == 'end'
    assert targeted[0]['_continuity_edit_findings'] == [{'observed': 'wrong position', 'required': 'correct position'}]
    assert '_continuity_edit_findings' not in (base / '97_shots/shots.json').read_text()


def test_direct_edit_selects_only_target_and_actual_copy_dependencies(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch, count=4)
    by_key = {(item['shot_number'], item['shot_type']): item for item in items}
    target = by_key[(2, 'end')]
    untouched = by_key[(2, 'start')]
    before = Path(untouched['output_path']).read_bytes()
    by_key[(3, 'start')]['copy_from_previous_end'] = True
    by_key[(3, 'start')]['source_end_path'] = target['output_path']
    by_key[(4, 'start')]['copy_from_previous_end'] = True
    by_key[(4, 'start')]['source_end_path'] = by_key[(3, 'start')]['output_path']
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    finding = {'frame_key': '1:2:end', 'observed': 'wrong', 'required': 'correct'}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps({'tasks': {'hash': {
        'verdicts': [{'violations': [finding], 'uncertain': []}]}}}))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-2', 'input_hash': 'hash', 'group': [target]}], 'images', 's')
    selected = calls['artist'][0]['items']['items']
    assert {(item['shot_number'], item['shot_type']) for item in selected} == {(2, 'end')}
    assert Path(untouched['output_path']).read_bytes() == before
    assert not Path(untouched['output_path']).with_suffix('.continuity.json').exists()


def test_failed_image_cache_refreshes_and_rewrites_prior_finding(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    old = {'frame_key': '1:1:end', 'entity_id': 'suitcase', 'observed': 'misplaced', 'required': 'correct arrangement'}
    new = dict(old, required='Move the object right of the landmark; preserve the landmark.')
    record = {'id': task['id'], 'passed': False, 'state_hash': task['state_hash'],
              'review_version': review._REVIEW_VERSION,
              'media_hashes': {p: review._file_hash(p) for p in task['media']},
              'verdicts': [{'violations': [old], 'uncertain': []}]}
    path = base / '97_shots/continuity_images_review.json'
    path.write_text(json.dumps({'tasks': {task['input_hash']: record}}))
    requests = []
    monkeypatch.setattr('custom_tools.storybook.shots_prompt_qa._scene_render_review_image', lambda _: 'image')
    def api(**kwargs):
        requests.append(kwargs)
        return json.dumps({'passed': False, 'violations': [new], 'uncertain': [],
                           'dismissed': [{'index': 0, 'reason': 'Replaced with actionable wording'}]})
    monkeypatch.setattr(review, 'call_openai_api', api)
    assert review._review('s', 'project', 'images', True, repair=False)['status'] == 'error'
    verdict = json.loads(path.read_text())['tasks'][task['input_hash']]['verdicts'][0]
    assert verdict['violations'] == [new]
    assert json.loads(requests[0]['prompt'][0]['text'])['prior_findings'] == [{'index': 0, 'finding': old}]
    assert 'do not correct a target by changing an unrelated object' in requests[0]['system_prompt']
    assert 'do not guess an ambiguous direction' in requests[0]['system_prompt']


def test_direct_edit_excludes_other_rejected_frames_but_keeps_good_refs(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch, count=3)
    by_key = {(item['shot_number'], item['shot_type']): item for item in items}
    first, second = by_key[(1, 'end')], by_key[(2, 'end')]
    good = by_key[(3, 'start')]['output_path']
    second['reference_image_paths'] = [first['output_path'], good]
    second['references'] = [good, first['output_path']]
    shots_path = base / '97_shots/shots.json'
    shots_path.write_text(json.dumps({'items': items}))
    before = shots_path.read_bytes()
    findings = [{'frame_key': f'1:{n}:end', 'observed': 'wrong', 'required': 'correct'} for n in (1, 2)]
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps({'tasks': {'hash': {
        'verdicts': [{'violations': findings, 'uncertain': []}]}}}))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-1|1-2', 'input_hash': 'hash', 'group': [first, second]}], 'images', 's')
    selected = [item for call in calls['artist'] for item in call['items']['items']]
    repaired = next(item for item in selected if item['shot_number'] == 2)
    assert repaired['reference_image_paths'] == [good]
    assert repaired['references'] == [good]
    assert shots_path.read_bytes() == before


def test_operator_finding_rechecks_only_affected_cached_group_and_not_after_repair(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    other = dict(task, id='2-1', input_hash='other', group=[dict(items[0], scene_number=2)], media=task['media'])
    monkeypatch.setattr(review, '_tasks', lambda *_: [task, other])
    monkeypatch.setattr(ct, 'record_continuity_image', lambda *args, **kwargs: None)
    monkeypatch.setattr(review, 'continuity_acceptance', lambda *_: {'passed': True})
    report_path = base / '97_shots/continuity_images_review.json'
    records = {t['input_hash']: {'id': t['id'], 'passed': True, 'state_hash': t['state_hash'],
                'media_hashes': {p: review._file_hash(p) for p in t['media']}, 'verdicts': []} for t in (task, other)}
    report_path.write_text(json.dumps({'tasks': records}))
    finding = {'frame_key': '1:1:start', 'entity_id': 'suitcase', 'observed': 'wrong side', 'required': 'check geometry'}
    calls = []
    monkeypatch.setattr(review, '_audit', lambda current, *_: calls.append(current) or {'passed': True, 'violations': [], 'uncertain': []})
    review.storybook_continuity_images_review_tool('s', 'project', review_findings=[finding])
    assert len(calls) == 1 and calls[0]['id'] == task['id']
    assert calls[0]['prior_findings'] == [finding]
    assert json.loads(report_path.read_text())['tasks']['other'] == records['other']
    with pytest.raises(ValueError, match='Unknown image review frame'):
        review.storybook_continuity_images_review_tool('s', 'project', review_findings=[dict(finding, frame_key='9:9:end')])

    # A second pass after the repair must not receive the observation about old pixels.
    calls.clear()
    monkeypatch.setattr(review, 'continuity_acceptance', lambda *_: {'passed': False} if not calls else {'passed': len(calls) > 1})
    def audit(current, *_):
        calls.append(current)
        return {'passed': len(calls) > 1, 'violations': [] if len(calls) > 1 else [finding], 'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    monkeypatch.setattr(review, '_repair_media', lambda *_: Path(task['media'][0]).write_bytes(b'changed'))
    review._review('s', 'project', 'images', True, review_findings=[finding])
    assert len(calls) == 2 and calls[0]['prior_findings'] == [finding]
    assert calls[1]['prior_findings'] == []


def test_pair_audits_isolate_findings_masters_and_current_pair_images(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    frames = []
    contexts = []
    for shot in (1, 2, 3):
        for item in items:
            phase = item['shot_type']
            frame = dict(item, shot_number=shot, _continuity={phase: {'world_id': 'station', 'entities': [
                {'id': f'landmark{shot}', 'reference_path': f'/master{shot}'}]}})
            frames.append(frame)
            contexts.append({'scene_number': 1, 'shot_number': shot, 'phase': phase,
                             'continuity': frame['_continuity'][phase]})
    task.update(group=frames, frames=frames, contexts=contexts, masters=['/master1', '/master2', '/master3'])
    findings = [{'frame_key': f'1:{shot}:end', 'entity_id': 'object', 'observed': 'wrong', 'required': 'correct'}
                for shot in (1, 2, 3)]
    monkeypatch.setattr(review, '_tasks', lambda *_: [task])
    monkeypatch.setattr(review, 'continuity_acceptance', lambda *_: {'passed': False})
    calls = []
    def audit(current, pictures, number):
        calls.append((current, pictures))
        return {'passed': number != 1, 'violations': [], 'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    result = review._review('s', 'project', 'images', True, repair=False, review_findings=findings)
    assert result['status'] == 'error' and len(calls) == 3
    for index, (current, pictures) in enumerate(calls, 1):
        assert {context['shot_number'] for context in current['contexts']} == {index}
        assert current['prior_findings'] == [findings[index - 1]]
        assert current['masters'] == [f'/master{index}']
        assert {item['shot_number'] for item in pictures} == {index}
        assert [item['shot_type'] for item in pictures] == ['start', 'end']
    assert items[1]['shot_type'] == 'end'
    record = json.loads((base / '97_shots/continuity_images_review.json').read_text())['tasks'][task['input_hash']]
    assert not record['passed'] and len(record['verdicts']) == 3


def test_audit_rejects_foreign_image_frame_key(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    monkeypatch.setattr('custom_tools.storybook.shots_prompt_qa._scene_render_review_image', lambda _: 'image')
    monkeypatch.setattr(review, 'call_openai_api', lambda **kwargs: json.dumps({
        'passed': False, 'violations': [{'frame_key': '9:9:end'}], 'uncertain': []}))
    with pytest.raises(ValueError, match='unexpected frame_key'):
        review._audit(task, task['group'], 0)


def test_character_edit_excludes_previous_scene_and_unaffected_cast(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch, count=1)
    identity = base / 'traveler.png'
    other = base / 'guard.png'
    identity.write_bytes(b'identity')
    other.write_bytes(b'other identity')
    target = items[0]
    target['characters'] = [{'name': 'Traveler', 'reference_image_path': str(identity)},
                            {'name': 'Guard', 'reference_image_path': str(other)}]
    target['reference_image_paths'] = [items[1]['output_path'], str(identity), str(other)]
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    finding = {'frame_key': '1:1:start', 'entity_id': 'Traveler',
               'observed': 'Duplicated body', 'required': 'Remove the extra body; preserve the hand.'}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps({'tasks': {'hash': {
        'verdicts': [{'passed': False, 'violations': [finding], 'uncertain': []}]}}}))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-1', 'input_hash': 'hash', 'group': [target]}], 'images', 's')
    assert calls['artist'][0]['items']['items'][0]['reference_image_paths'] == [target['output_path'], str(identity)]


def test_direct_edit_adds_only_target_entity_master_without_writing_shots(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch)
    master = base / 'master.png'
    unrelated = base / 'other-master.png'
    master.write_bytes(b'master')
    unrelated.write_bytes(b'other')
    (base / '20_bible').mkdir()
    (base / '20_bible/continuity_plan.json').write_text(json.dumps({'entities': [
        {'id': 'target', 'reference_path': str(master)}, {'id': 'other', 'reference_path': str(unrelated)}]}))
    target = items[0]
    target['reference_image_paths'] = ['existing-character']
    shots_path = base / '97_shots/shots.json'
    shots_path.write_text(json.dumps({'items': items}))
    before = shots_path.read_bytes()
    finding = {'frame_key': '1:1:start', 'entity_id': 'target', 'observed': 'wrong', 'required': 'correct'}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps({'tasks': {'hash': {
        'verdicts': [{'passed': True, 'violations': [], 'uncertain': []},
                     {'passed': False, 'violations': [finding], 'uncertain': []}]}}}))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-1', 'input_hash': 'hash', 'group': [target]}], 'images', 's')
    assert not calls['qa'] and not calls['background_plan'] and not calls['background_refs']
    selected = calls['artist'][0]['items']['items'][0]
    assert selected['reference_image_paths'] == [str(master), 'existing-character']
    assert str(unrelated) not in selected['reference_image_paths']
    assert shots_path.read_bytes() == before


def test_direct_edit_uses_master_moved_to_shared_references(tmp_path, monkeypatch):
    from custom_tools.storybook import reference_catalog as catalog
    from custom_tools.storybook.studio.capabilities import invoke
    _, items, calls = _repair_scope_fixture(tmp_path / 'plain', monkeypatch)
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = invoke('projects_create', {'title': 'World', 'description': 'Shared world', 'genre': 'comedy',
                                     'target_age': 'all'})['project_id']
    sid = invoke('series_create', {'project_id': pid, 'title': 'One', 'source_material': '',
                                   'participant_ids': [], 'settings': {}})['series_id']
    base = tmp_path / pid / 'series' / sid
    old = base / '20_bible/references/locations/master.png'
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b'master')
    new = catalog.store_reference(base, old, 'location', 'master')
    assert not old.exists()
    (base / '20_bible/continuity_plan.json').write_text(json.dumps({'entities': [
        {'id': 'target', 'reference_path': str(old)}]}))
    target = items[0]
    (base / '97_shots').mkdir(exist_ok=True)
    finding = {'frame_key': '1:1:start', 'entity_id': 'target', 'observed': 'wrong', 'required': 'correct'}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps({'tasks': {'hash': {
        'verdicts': [{'passed': True, 'violations': [], 'uncertain': []},
                     {'passed': False, 'violations': [finding], 'uncertain': []}]}}}))
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-1', 'input_hash': 'hash', 'group': [target]}], 'images', 's')
    assert calls['artist'][0]['items']['items'][0]['reference_image_paths'] == [new]


def test_review_tasks_and_audit_read_landmark_master_moved_to_shared(tmp_path, monkeypatch):
    from custom_tools.storybook import reference_catalog as catalog
    from custom_tools.storybook.project_paths import series_scope
    from custom_tools.storybook.studio.capabilities import invoke
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = invoke('projects_create', {'title': 'project', 'description': 'Shared world', 'genre': 'comedy',
                                     'target_age': 'all'})['project_id']
    sid = invoke('series_create', {'project_id': pid, 'title': 'One', 'source_material': '',
                                   'participant_ids': [], 'settings': {}})['series_id']
    base, _ = _project(tmp_path, monkeypatch, sid)
    old = base / '20_bible/references/locations/tower.png'
    old.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (16, 16), 'white').save(old)
    plan = json.loads((base / '20_bible/continuity_plan.json').read_text())
    plan['entities'][0].update(kind='landmark', geometry='башня', reference_path=str(old))
    (base / '20_bible/continuity_plan.json').write_text(json.dumps(plan))
    with series_scope(pid, sid):
        before = review._tasks(base, 'images')[0]['input_hash']
        catalog.store_reference(base, old, 'location', 'tower')
        assert not old.exists()
        task = review._tasks(base, 'images')[0]
        assert task['masters'] == [str(old)] and task['input_hash'] == before
        requests = []
        monkeypatch.setattr(review, 'call_openai_api', lambda **kw: requests.append(kw) or '{}')
        monkeypatch.setattr(review, 'parse_llm_json', lambda _: {'passed': True, 'violations': [], 'uncertain': []})
        review._audit(task, [], 1)
    assert any('LANDMARK' in part.get('text', '') for part in requests[0]['prompt'])


def test_failed_artist_round_is_rolled_back_before_audit(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    original = [Path(item['output_path']).read_bytes() for item in items]
    calls = []
    monkeypatch.setattr(review, '_audit', lambda *args: calls.append(args) or {
        'passed': False, 'violations': [{'frame_key': '1:1:end', 'observed': 'wrong', 'required': 'correct'}], 'uncertain': []})
    monkeypatch.setattr('custom_tools.storybook.artist_batch_edit.artist_agent_batch_edit_tool',
                        lambda **kwargs: json.dumps([{'ok': False, 'error': 'render failed'}]))
    monkeypatch.setattr(review, '_repair_media', _REAL_REPAIR_MEDIA)
    with pytest.raises(RuntimeError, match='render failed'):
        review.storybook_continuity_images_review_tool('s', 'project')
    assert len(calls) == 1
    assert [Path(item['output_path']).read_bytes() for item in items] == original




@pytest.mark.parametrize('fps', [30, 60])
def test_video_end_event_extracts_actual_last_and_previous_frames(tmp_path, monkeypatch, fps):
    import shutil
    import subprocess
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg unavailable')
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    clip = base / '97_shots/clip.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=red:s=32x32:r={fps}:d=2',
                    '-vf', f"drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='eq(n,{2*fps-1})'",
                    '-c:v', 'mpeg4', str(clip)], check=True)
    task = {'id': '1-1', 'input_hash': 'event', 'state_hash': 'state', 'media': [str(clip)],
            'events': [2], 'planned_duration': 2, 'contexts': [{'time_s': 2}], 'group': [{}]}
    monkeypatch.setattr(review, 'safe_storybook_project_dir', lambda _: base)
    monkeypatch.setattr(review, '_tasks', lambda *_: [task])
    monkeypatch.setattr(review, 'continuity_acceptance', lambda *_: {'passed': True})
    pixels = {}
    def audit(_, pictures, chunk):
        for picture in pictures:
            with Image.open(picture['output_path']) as frame:
                pixels[picture['shot_type']] = frame.convert('RGB').getpixel((16, 16))
        return {'passed': True, 'violations': [], 'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    assert review._review('s', 'project', 'videos', True)['status'] == 'success'
    last = pixels[f't={(2 * fps - 1) / fps:.2f}s']
    middle = pixels['t=1.00s']
    assert last[2] > last[0] + 100 and middle[0] > middle[2] + 100


def test_video_review_can_run_without_automatic_repair(monkeypatch):
    calls = {}

    def fake_review(*args, **kwargs):
        calls['repair'] = kwargs.get('repair')
        return {'status': 'success'}

    monkeypatch.setattr(review, '_review', fake_review)
    assert review.storybook_continuity_videos_review_tool('s', 'p', repair=False) == {'status': 'success'}
    assert calls['repair'] is False


def test_visual_audit_receives_character_canon_as_separate_reference(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    canon = base / '20_bible/references/hero.png'
    Image.new('RGB', (16, 16), 'red').save(canon)
    for item in items:
        item['characters'] = [{'name': 'Hero', 'reference_image_path': str(canon)}]
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    requests = []
    monkeypatch.setattr(review, 'call_openai_api', lambda **kwargs: requests.append(kwargs) or json.dumps({
        'passed': True, 'violations': [], 'uncertain': []}))
    task = review._tasks(base, 'images')[0]
    review._audit(task, task['group'], 0)
    content = requests[0]['prompt']
    assert any('CHARACTER.Hero' in item.get('text', '') for item in content)
    assert 'character identity' in requests[0]['system_prompt'].lower()


def test_continuity_tasks_keep_project_id_inside_series_scope(tmp_path, monkeypatch):
    import shutil
    from custom_tools.storybook.project_paths import series_scope
    from custom_tools.storybook.studio.capabilities import invoke
    from custom_tools.storybook import video_plan
    old, items = _project(tmp_path, monkeypatch)
    pid = invoke('projects_create', {'title': 'World', 'description': 'A shared world', 'genre': 'fairy', 'target_age': 'all'})['project_id']
    sid = invoke('series_create', {'project_id': pid, 'title': 'Series', 'source_material': '', 'participant_ids': [], 'settings': {}})['series_id']
    base = tmp_path / pid / 'series' / sid
    shutil.copytree(old, base, dirs_exist_ok=True)
    for item in items:
        item['output_path'] = str(base / '97_shots' / (item['shot_type'] + '.png'))
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    called = []
    original = video_plan.select_image_items
    def select(project_id, rows):
        called.append(project_id)
        return original(project_id, rows)
    monkeypatch.setattr(video_plan, 'select_image_items', select)
    with series_scope(pid, sid):
        assert review._tasks(base, 'images')
    assert called == [pid]


def test_image_audit_preserves_readable_small_props_without_changing_source(tmp_path, monkeypatch):
    import base64
    import io
    base, _ = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    frame = base / 'portrait.png'
    Image.new('RGB', (1080,1920), 'gray').save(frame)
    before = frame.read_bytes()
    calls = []
    monkeypatch.setattr(review, 'call_openai_api', lambda **kwargs: calls.append(kwargs) or json.dumps({
        'passed':True, 'violations':[], 'uncertain':[]}))
    review._audit(task,[dict(scene_number=2,shot_number=1,shot_type='end',output_path=str(frame))],0)
    blocks = calls[0]['prompt']
    target_index = next(i for i, block in enumerate(blocks) if block.get('text') == '2.1 end')
    image_block = blocks[target_index + 1]
    with Image.open(io.BytesIO(base64.b64decode(image_block['image_url']['url'].split(',',1)[1]))) as sent:
        assert sent.size == (720,1280)
    assert frame.read_bytes() == before


def test_new_review_version_does_not_reuse_old_visual_findings(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    task = review._tasks(base,'images')[0]
    old = {'id':task['id'], 'review_version':review._REVIEW_VERSION-1, 'passed':False,
           'state_hash':task['state_hash'],'media_hashes':{p:review._file_hash(p) for p in task['media']},
           'verdicts':[{'violations':[{'frame_key':'1:1:start','entity_id':'old','observed':'old hint','required':'old edit'}], 'uncertain':[]}]}
    (base/'97_shots/continuity_images_review.json').write_text(json.dumps({'tasks':{'old':old}}))
    seen=[]
    def audit(current,*_):
        seen.extend(current.get('prior_findings',[]))
        return {'passed':True,'violations':[],'uncertain':[]}
    monkeypatch.setattr(review,'_audit',audit)
    review._review('test','project','images',True,repair=False)
    assert seen == []


def test_pair_audit_receives_other_cast_reference_and_tracks_its_bytes(tmp_path, monkeypatch):
    import copy
    base, pair = _project(tmp_path, monkeypatch)
    refs = {}
    for name,color in [('First','red'),('Second','blue')]:
        path=base/f'20_bible/references/{name}.png'
        Image.new('RGB',(16,16),color).save(path);refs[name]=str(path)
    screenplay=json.loads((base/'91_screenplay/screenplay.json').read_text())
    screenplay['screenplay'][0]['storyboard']*=4
    for number,shot in enumerate(screenplay['screenplay'][0]['storyboard'],1):
        screenplay['screenplay'][0]['storyboard'][number-1]=dict(shot,shot_number=number)
    (base/'91_screenplay/screenplay.json').write_text(json.dumps(screenplay))
    plan=json.loads((base/'20_bible/continuity_plan.json').read_text())
    template=copy.deepcopy(plan['frames']);plan['frames']={f'1:{number}:{phase}':copy.deepcopy(template[f'1:1:{phase}']) for number in range(1,5) for phase in ['start','end']}
    plan['input_hash']=ct._digest([ct._PLAN_VERSION,ct._source(base)])
    (base/'20_bible/continuity_plan.json').write_text(json.dumps(plan))
    items=[]
    for number in range(1,5):
        for original in pair:
            item=copy.deepcopy(original);item['shot_number']=number
            name='First' if number==1 else 'Second'
            item['characters']=[{'name':name,'reference_image_path':refs[name]}]
            items.append(item)
    (base/'97_shots/shots.json').write_text(json.dumps({'items':items}))
    task=next(t for t in review._tasks(base,'images') if t['id']=='1-4')
    requests=[]
    monkeypatch.setattr(review,'call_openai_api',lambda **kwargs:requests.append(kwargs) or json.dumps({'passed':True,'violations':[],'uncertain':[]}))
    review._audit(task,task['group'],0)
    labels=[block.get('text','') for block in requests[0]['prompt']]
    assert sum(label.startswith('CHARACTER.First ') for label in labels)==1
    assert sum(label.startswith('CHARACTER.Second ') for label in labels)==1
    Image.new('RGB',(16,16),'green').save(refs['First'])
    changed=next(t for t in review._tasks(base,'images') if t['id']=='1-4')
    assert changed['input_hash'] != task['input_hash']


def test_deleted_review_images_are_ignored_before_reading_old_reference_hashes(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    path = base / '97_shots/continuity_images_review.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'tasks': {'old': {
        'review_version': review._REVIEW_VERSION, 'id': '1-1', 'passed': False,
        'state_hash': 'old', 'media_hashes': {str(base / 'deleted.png'): 'old'}, 'verdicts': []}}}))
    def forbidden(*args, **kwargs):
        raise AssertionError('Deleted image review must not read its old references')
    monkeypatch.setattr(review, 'unit_continuity_hash', forbidden)
    assert review.pending_image_findings(base, {'1-1'}) == []


def test_failed_repair_rolls_back_only_groups_still_rejected(tmp_path, monkeypatch):
    # Откат всех групп терял успешный ремонт: каждый запуск начинал с тех же проблем.
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    groups = {}
    for group in ('fixed', 'broken'):
        path = base / f'97_shots/{group}.png'
        path.write_bytes(b'bad')
        groups[group] = {'scene_number': 1, 'shot_number': len(groups) + 1, 'shot_type': 'start',
                         'output_path': str(path), '_continuity': {'start': {}}}
    (base / '97_shots/shots.json').write_text(json.dumps({'items': list(groups.values())}))
    monkeypatch.setattr('custom_tools.storybook.video_plan.select_image_items', lambda _, items: items)

    def tasks(base, kind):
        return [{'id': name, 'input_hash': name + Path(item['output_path']).read_text(), 'group': [item],
                 'state_hash': 'state', 'media': [item['output_path']], 'contexts': []}
                for name, item in groups.items()]

    def repair(base, rejected, kind, session_id, on_attempt):
        on_attempt({task['id'] for task in rejected})
        for task in rejected:
            Path(task['group'][0]['output_path']).write_bytes(b'good' if task['id'] == 'fixed' else b'worse')

    monkeypatch.setattr(review, 'safe_storybook_project_dir', lambda project: base)
    monkeypatch.setattr(review, '_tasks', tasks)
    monkeypatch.setattr(review, '_audit', lambda task, pictures, number: {
        'passed': Path(pictures[0]['output_path']).read_bytes() == b'good',
        'violations': [] if Path(pictures[0]['output_path']).read_bytes() == b'good' else
        [{'frame_key': f"1:{pictures[0]['shot_number']}:start", 'observed': 'bad', 'required': 'fix'}], 'uncertain': []})
    monkeypatch.setattr(review, '_repair_media', repair)
    monkeypatch.setattr(ct, 'record_continuity_image', lambda item, basis: None)
    result = review.storybook_continuity_images_review_tool('session', 'project')
    assert result['auto_accepted'] == ['broken']
    assert (base / '97_shots/fixed.png').read_bytes() == b'good'
    assert (base / '97_shots/broken.png').read_bytes() == b'bad'


def test_group_failing_two_repair_rounds_is_auto_accepted_with_findings(tmp_path, monkeypatch):
    # Аудитор, которого ремонт не убеждает, не должен навсегда блокировать видео.
    base, _ = _project(tmp_path, monkeypatch)
    finding = {'frame_key': '1:1:start', 'observed': 'wrong', 'required': 'correct'}
    monkeypatch.setattr(review, '_audit', lambda *args: {'passed': False, 'violations': [finding], 'uncertain': []})
    repairs = []
    def repair(*args):
        repairs.append(args)
        args[-1]({'1-1'})
    monkeypatch.setattr(review, '_repair_media', repair)
    result = review.storybook_continuity_images_review_tool('session', 'project')
    assert result['status'] == 'success'
    assert len(repairs) == 2
    assert review.continuity_acceptance(base, 'images')['passed']
    report = json.loads((base / '97_shots/continuity_images_review.json').read_text())
    record = next(iter(report['tasks'].values()))
    assert record['auto_accepted']['failed_rounds'] == 2
    assert record['verdicts'][0]['violations'] == [finding]


def test_failed_round_counter_survives_restart(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(review, '_audit', lambda *args: {'passed': False, 'violations': [
        {'frame_key': '1:1:start', 'observed': 'wrong', 'required': 'correct'}], 'uncertain': []})
    repairs = []
    def repair(*args):
        repairs.append(args)
        args[-1]({'1-1'})
    monkeypatch.setattr(review, '_repair_media', repair)
    path = base / '97_shots/continuity_images_review.json'
    review._review('session', 'project', 'images', True, repair=False)
    report = json.loads(path.read_text())
    report['failed_rounds'] = {'1-1': 1}
    path.write_text(json.dumps(report))
    assert review.storybook_continuity_images_review_tool('session', 'project')['status'] == 'success'
    assert len(repairs) == 1


def test_repair_exception_is_failed_round_with_rollback(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    original = Path(items[0]['output_path']).read_bytes()
    monkeypatch.setattr(review, '_audit', lambda *args: {'passed': False,
        'violations': [{'frame_key': '1:1:start', 'observed': 'wrong', 'required': 'correct'}], 'uncertain': []})

    def repair(*args):
        args[-1]({'1-1'})
        Path(items[0]['output_path']).write_bytes(b'broken')
        raise RuntimeError('Background plan review: rejected')

    monkeypatch.setattr(review, '_repair_media', repair)
    with pytest.raises(RuntimeError, match='Background plan review: rejected'):
        review.storybook_continuity_images_review_tool('session', 'project')
    assert Path(items[0]['output_path']).read_bytes() == original


def test_video_group_failing_two_repair_rounds_is_auto_accepted(tmp_path, monkeypatch):
    import shutil
    import subprocess
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg unavailable')
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    clip = base / '97_shots/test.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=32x32:r=5:d=1',
                    '-c:v', 'mpeg4', str(clip)], check=True)
    task = {'id': '1-1', 'input_hash': 'video', 'state_hash': 'state', 'media': [str(clip)], 'anchors': [],
            'planned_duration': 1, 'contexts': [{'time_s': 0}], 'group': [{'scene_number': 1, 'shot_number': 1}]}
    monkeypatch.setattr(review, 'safe_storybook_project_dir', lambda _: base)
    monkeypatch.setattr(review, '_tasks', lambda _, kind: [task] if kind == 'videos' else [])
    monkeypatch.setattr(review, '_audit', lambda *args: {'passed': False, 'violations': [
        {'frame_key': 'VIDEO.0', 'observed': 'wrong', 'required': 'correct'}], 'uncertain': []})
    repairs = []
    def repair(*args):
        repairs.append(args)
        args[-1]({'1-1'})
    monkeypatch.setattr(review, '_repair_media', repair)
    assert review.storybook_continuity_videos_review_tool('s', 'project')['status'] == 'success'
    assert len(repairs) == 2
    assert review.continuity_acceptance(base, 'videos')['passed']


def _two_pair_project(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    screenplay_path = base / '91_screenplay/screenplay.json'
    screenplay = json.loads(screenplay_path.read_text())
    screenplay['screenplay'][0]['storyboard'].append({'shot_number': 2, 'description': 'Другой кадр'})
    screenplay_path.write_text(json.dumps(screenplay))
    plan_path = base / '20_bible/continuity_plan.json'
    plan = json.loads(plan_path.read_text())
    for phase in ('start', 'end'):
        plan['frames'][f'1:2:{phase}'] = plan['frames'][f'1:1:{phase}']
        path = base / f'97_shots/2-{phase}.png'
        Image.new('RGB', (16, 16), 'blue').save(path)
        items.append(dict(items[0], shot_number=2, shot_type=phase, output_path=str(path)))
    plan['input_hash'] = ct._digest([ct._PLAN_VERSION, ct._source(base)])
    plan_path.write_text(json.dumps(plan))
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    return base, items


def test_pair_acceptance_cache_is_independent_and_scoped(tmp_path, monkeypatch):
    base, items = _two_pair_project(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(review, '_audit', lambda task, *_: calls.append(task['id']) or
                        {'passed': True, 'violations': [], 'uncertain': []})
    assert review._review('s', 'project', 'images', True, repair=False)['status'] == 'success'
    assert sorted(calls) == ['1-1', '1-2']
    Path(items[0]['output_path']).write_bytes(b'changed')
    assert review.continuity_acceptance(base, 'images', shot_keys=['1-2'])['passed']
    assert not review.continuity_acceptance(base, 'images')['passed']
    calls.clear()
    assert review._review('s', 'project', 'images', True, repair=False)['status'] == 'success'
    assert calls == ['1-1']


def test_unlocalized_uncertainty_does_not_attempt_repair(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(review, '_audit', lambda *args: {'passed': False, 'violations': [], 'uncertain': ['unclear']})
    calls = []
    monkeypatch.setattr(review, '_repair_media', lambda *args: calls.append(args))
    result = review._review('s', 'project', 'images', True)
    assert result['status'] == 'error'
    assert not calls
    report = json.loads((base / '97_shots/continuity_images_review.json').read_text())
    assert not report.get('failed_rounds')


def test_copy_source_change_invalidates_only_dependent_pair(tmp_path, monkeypatch):
    base, items = _two_pair_project(tmp_path, monkeypatch)
    items[2].update(copy_from_previous_end=True, source_end_path=items[1]['output_path'])
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    initial = {task['id']: task['input_hash'] for task in review._tasks(base, 'images')}
    Path(items[1]['output_path']).write_bytes(b'changed source')
    updated = {task['id']: task['input_hash'] for task in review._tasks(base, 'images')}
    assert {key for key in initial if initial[key] != updated[key]} == {'1-1', '1-2'}
    Path(items[0]['output_path']).write_bytes(b'changed unrelated start')
    after_start = {task['id']: task['input_hash'] for task in review._tasks(base, 'images')}
    assert after_start['1-2'] == updated['1-2']


def test_character_master_hash_matches_actual_audit_references(tmp_path, monkeypatch):
    base, items = _two_pair_project(tmp_path, monkeypatch)
    master = base / '20_bible/references/hero.png'
    Image.new('RGB', (16, 16), 'red').save(master)
    items[0]['characters'] = [{'name': 'Hero', 'reference_image_path': str(master)}]
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    before = {task['id']: task for task in review._tasks(base, 'images')}
    assert all(str(master) in task['character_masters'] for task in before.values())
    Image.new('RGB', (16, 16), 'green').save(master)
    after = {task['id']: task for task in review._tasks(base, 'images')}
    assert all(before[key]['input_hash'] != after[key]['input_hash'] for key in before)


def test_scoped_acceptance_requires_copy_source_and_rejects_unknown(tmp_path, monkeypatch):
    base, items = _two_pair_project(tmp_path, monkeypatch)
    items[2].update(copy_from_previous_end=True, source_end_path=items[1]['output_path'])
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    monkeypatch.setattr(review, '_audit', lambda *_: {'passed': True, 'violations': [], 'uncertain': []})
    review._review('s', 'project', 'images', True, repair=False)
    Path(items[0]['output_path']).write_bytes(b'changed upstream pair')
    assert review.continuity_acceptance(base, 'images', ['1-2'])['unaccepted'] == ['1-1']
    assert not review.continuity_acceptance(base, 'images', [])['passed']
    assert not review.continuity_acceptance(base, 'images', ['9-9'])['passed']


def test_new_finding_rechecks_only_accepted_pair(tmp_path, monkeypatch):
    base, _ = _two_pair_project(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(review, '_audit', lambda task, *_: calls.append(task['id']) or
                        {'passed': True, 'violations': [], 'uncertain': []})
    review._review('s', 'project', 'images', True, repair=False)
    calls.clear()
    finding = {'frame_key': '1:2:start', 'observed': 'wrong', 'required': 'check'}
    review._review('s', 'project', 'images', True, repair=False, review_findings=[finding])
    assert calls == ['1-2']


def test_old_group_acceptance_is_not_fabricated_for_pairs(tmp_path, monkeypatch):
    base, _ = _two_pair_project(tmp_path, monkeypatch)
    old = {'id': '1-1|1-2', 'passed': True, 'review_version': review._REVIEW_VERSION,
           'auto_accepted': {'failed_rounds': 2}, 'verdicts': [
               {'passed': False, 'violations': [{'frame_key': '1:1:start'}], 'uncertain': []}]}
    path = base / '97_shots/continuity_images_review.json'
    path.write_text(json.dumps({'tasks': {'old-hash': old}}))
    assert not review.continuity_acceptance(base, 'images')['passed']
    assert json.loads(path.read_text())['tasks']['old-hash'] == old


@pytest.mark.parametrize('source_confirmed', [True, False])
def test_video_source_hypothesis_requires_readonly_confirmation(tmp_path, monkeypatch, source_confirmed):
    base, items = _project(tmp_path, monkeypatch)
    video = {'id': '1-1', 'input_hash': 'video', 'anchors': [i['output_path'] for i in items], 'group': [items[0]]}
    finding = {'frame_key': 'VIDEO.0', 'source_frame_key': '1:1:start',
               'entity_id': 'suitcase', 'observed': 'wrong side', 'required': 'move it'}
    report = {'tasks': {'video': {'passed': False, 'verdicts': [
        {'passed': False, 'violations': [finding], 'uncertain': []}]}}}
    original_tasks = review._tasks
    monkeypatch.setattr(review, '_tasks', lambda path, kind, shot_keys=None:
                        original_tasks(path, kind, shot_keys) if kind == 'images' else [video])
    state = {'edited': False}
    calls = []
    def audit(task, *_):
        calls.append(('audit', task['kind']))
        if state['edited']:
            return {'passed': True, 'violations': [], 'uncertain': []}
        if not source_confirmed:
            return {'passed': False, 'violations': [], 'uncertain': ['cannot locate']}
        return {'passed': False, 'violations': [{'frame_key': '1:1:start', 'entity_id': 'suitcase',
                                                'observed': 'wrong side', 'required': 'move it'}], 'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    def repair(_, tasks, kind, session, on_attempt):
        calls.append(('repair', kind))
        on_attempt({task['id'] for task in tasks})
        if kind == 'images':
            state['edited'] = True
    monkeypatch.setattr(review, '_repair_media', repair)
    actual_review = review._review
    def dispatch(session, project, kind, enable, **kwargs):
        if kind == 'videos':
            return {'status': 'success' if source_confirmed else 'error',
                    'acceptance': {'passed': source_confirmed}}
        return actual_review(session, project, kind, enable, **kwargs)
    monkeypatch.setattr(review, '_review', dispatch)
    result = review._repair_round('s', 'project', 'videos', True, base, [video], report, {})
    assert calls[0] == ('audit', 'images')
    if source_confirmed:
        assert calls[1:3] == [('repair', 'images'), ('audit', 'images')]
        assert ('repair', 'videos') in calls and result['status'] == 'success'
    else:
        assert not any(call[0] == 'repair' for call in calls)
        assert result['status'] == 'error'
        assert not result['attempted_repairs']


def test_explicit_source_repairs_count_only_actual_attempts_and_keep_verdict(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    finding = {'frame_key': '1:1:start', 'entity_id': 'suitcase', 'observed': 'wrong', 'required': 'fix'}
    monkeypatch.setattr(review, '_audit', lambda *_: {'passed': False, 'violations': [finding], 'uncertain': []})
    def attempt(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1'})
    monkeypatch.setattr(review, '_repair_media', attempt)
    first = review._review('s', 'project', 'images', True, review_findings=[finding])
    path = base / '97_shots/continuity_images_review.json'
    assert first['status'] == 'error'
    assert json.loads(path.read_text())['failed_rounds']['1-1'] == 1
    second = review._review('s', 'project', 'images', True, review_findings=[finding])
    assert second['status'] == 'success'
    record = next(r for r in json.loads(path.read_text())['tasks'].values() if r.get('auto_accepted'))
    assert record['auto_accepted']['failed_rounds'] == 2
    assert record['verdicts'][0]['passed'] is False
    assert record['verdicts'][0]['violations'] == [finding]


def test_video_hypothesis_needs_explicit_source_confirmation(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    hypothesis = {'frame_key': '1:1:start', 'source': 'video', 'entity_id': 'suitcase',
                  'observed': 'wrong side', 'required': 'move it'}
    monkeypatch.setattr(review, 'call_openai_api', lambda **_: json.dumps({
        'passed': True, 'violations': [], 'uncertain': [], 'dismissed': []}))
    verdict = review._audit(dict(task, prior_findings=[hypothesis]), task['group'], 0)
    assert not verdict['passed'] and not verdict['violations'] and verdict['uncertain']


@pytest.mark.parametrize('explicit', [False, True])
def test_repair_exception_rolls_back_and_records_real_attempt(tmp_path, monkeypatch, explicit):
    base, items = _project(tmp_path, monkeypatch)
    original = Path(items[0]['output_path']).read_bytes()
    metadata = Path(items[0]['output_path']).with_suffix('.continuity.json')
    original_metadata = metadata.read_bytes()
    finding = {'frame_key': '1:1:start', 'observed': 'wrong', 'required': 'fix'}
    monkeypatch.setattr(review, '_audit', lambda *_: {'passed': False, 'violations': [finding], 'uncertain': []})
    def repair(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1'})
        Path(items[0]['output_path']).write_bytes(b'unaccepted')
        metadata.unlink()
        raise RuntimeError('editor failed')
    monkeypatch.setattr(review, '_repair_media', repair)
    for count in (1, 2):
        with pytest.raises(RuntimeError, match='editor failed'):
            review._review('s', 'project', 'images', True, review_findings=[finding] if explicit else None)
        assert Path(items[0]['output_path']).read_bytes() == original
        assert metadata.read_bytes() == original_metadata
        report = json.loads((base / '97_shots/continuity_images_review.json').read_text())
        assert report['failed_rounds']['1-1'] == count
        assert not any(record.get('auto_accepted') for record in report['tasks'].values())


@pytest.mark.parametrize('explicit', [False, True])
def test_post_repair_audit_exception_rolls_back_and_records_attempt(tmp_path, monkeypatch, explicit):
    base, items = _project(tmp_path, monkeypatch)
    original = Path(items[0]['output_path']).read_bytes()
    finding = {'frame_key': '1:1:start', 'observed': 'wrong', 'required': 'fix'}
    calls = []
    def audit(*_):
        calls.append(True)
        if len(calls) > 1:
            raise ValueError('post audit failed')
        return {'passed': False, 'violations': [finding], 'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    def repair(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1'})
        Path(items[0]['output_path']).write_bytes(b'unaccepted')
    monkeypatch.setattr(review, '_repair_media', repair)
    with pytest.raises(ValueError, match='post audit failed'):
        review._review('s', 'project', 'images', True, review_findings=[finding] if explicit else None)
    assert Path(items[0]['output_path']).read_bytes() == original
    report = json.loads((base / '97_shots/continuity_images_review.json').read_text())
    assert report['failed_rounds']['1-1'] == 1
    assert not any(record.get('auto_accepted') for record in report['tasks'].values())


@pytest.mark.parametrize('shot_keys,passed', [([], False), (['9-9'], False), (['1-1'], True)])
def test_scoped_acceptance_validates_keys_without_continuity_plan(tmp_path, monkeypatch, shot_keys, passed):
    base, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(review, 'validate_current_continuity', lambda _: None)
    result = review.continuity_acceptance(base, 'images', shot_keys)
    assert result['passed'] is passed
    assert result['status'] == ('not_required' if passed else 'invalid_inputs')


def test_failed_post_audit_discards_partial_results_for_rolled_back_bytes(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    finding = {'frame_key': '1:1:start', 'observed': 'wrong', 'required': 'fix'}
    monkeypatch.setattr(review, '_audit', lambda *_: {'passed': False, 'violations': [finding], 'uncertain': []})
    review._review('s', 'project', 'images', True, repair=False)
    path = base / '97_shots/continuity_images_review.json'
    before = json.loads(path.read_text())['tasks']
    task = review._tasks(base, 'images')[0]
    def edit(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1'})
        Path(items[0]['output_path']).write_bytes(b'unaccepted')
    def fail_after_partial(*args, **kwargs):
        report = json.loads(path.read_text())
        report['tasks']['partial'] = {'id': '1-1', 'passed': True, 'verdicts': []}
        path.write_text(json.dumps(report))
        raise ValueError('post audit failed')
    monkeypatch.setattr(review, '_repair_media', edit)
    monkeypatch.setattr(review, '_review', fail_after_partial)
    with pytest.raises(ValueError, match='post audit failed'):
        review._repair_round('s', 'project', 'images', True, base, [task], {'tasks': before}, {})
    after = json.loads(path.read_text())
    assert after['tasks'] == before
    assert after['failed_rounds']['1-1'] == 1


@pytest.mark.parametrize('had_metadata', [False, True])
def test_normal_rejected_repair_restores_image_and_sidecar(tmp_path, monkeypatch, had_metadata):
    base, items = _project(tmp_path, monkeypatch)
    image = Path(items[0]['output_path'])
    metadata = image.with_suffix('.continuity.json')
    original = image.read_bytes()
    if not had_metadata:
        metadata.unlink()
    original_metadata = metadata.read_bytes() if had_metadata else None
    finding = {'frame_key': '1:1:start', 'observed': 'wrong', 'required': 'fix'}
    monkeypatch.setattr(review, '_audit', lambda *_: {'passed': False, 'violations': [finding], 'uncertain': []})
    review._review('s', 'project', 'images', True, repair=False)
    task = review._tasks(base, 'images')[0]
    report = json.loads((base / '97_shots/continuity_images_review.json').read_text())
    def edit(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1'})
        image.write_bytes(b'bad new image')
        metadata.write_bytes(b'bad new metadata')
    monkeypatch.setattr(review, '_repair_media', edit)
    result = review._repair_round('s', 'project', 'images', True, base, [task], report, {})
    assert not result['acceptance']['passed']
    assert image.read_bytes() == original
    assert (metadata.read_bytes() if metadata.exists() else None) == original_metadata


def test_confirmed_manual_repair_resets_counter_before_new_finding(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    finding = {'frame_key': '1:1:start', 'observed': 'wrong', 'required': 'fix'}
    state = {'passed': False}
    monkeypatch.setattr(review, '_audit', lambda *_: {'passed': state['passed'],
        'violations': [] if state['passed'] else [finding], 'uncertain': []})
    monkeypatch.setattr(review, '_repair_media', lambda b, t, k, s, cb: cb({'1-1'}))
    review._review('s', 'project', 'images', True, review_findings=[finding])
    path = base / '97_shots/continuity_images_review.json'
    assert json.loads(path.read_text())['failed_rounds']['1-1'] == 1
    Path(items[0]['output_path']).write_bytes(b'manually fixed')
    state['passed'] = True
    assert review._review('s', 'project', 'images', True, repair=False)['status'] == 'success'
    assert '1-1' not in json.loads(path.read_text()).get('failed_rounds', {})
    state['passed'] = False
    result = review._review('s', 'project', 'images', True, review_findings=[finding])
    assert result['status'] == 'error'
    report = json.loads(path.read_text())
    assert report['failed_rounds']['1-1'] == 1
    assert not any(record.get('auto_accepted') for record in report['tasks'].values())


def test_scoped_review_repairs_only_selected_pair_through_two_rounds(tmp_path, monkeypatch):
    base, _ = _two_pair_project(tmp_path, monkeypatch)
    calls = []
    finding = {'frame_key': '1:1:start', 'observed': 'wrong', 'required': 'fix'}
    def audit(task, *_):
        calls.append(('audit', task['id']))
        assert task['id'] == '1-1'
        return {'passed': False, 'violations': [finding], 'uncertain': []}
    def repair(base, tasks, kind, session, on_attempt):
        calls.append(('repair', [task['id'] for task in tasks]))
        on_attempt({'1-1'})
    monkeypatch.setattr(review, '_audit', audit)
    monkeypatch.setattr(review, '_repair_media', repair)
    result = review._review('s', 'project', 'images', True, shot_keys=['1-1'])
    assert result['status'] == 'success'
    assert [entry for entry in calls if entry[0] == 'repair'] == [('repair', ['1-1'])] * 2


def test_scoped_review_includes_actual_copy_dependency(tmp_path, monkeypatch):
    base, items = _two_pair_project(tmp_path, monkeypatch)
    items[2].update(copy_from_previous_end=True, source_end_path=items[1]['output_path'])
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    calls = []
    monkeypatch.setattr(review, '_audit', lambda task, *_: calls.append(task['id']) or
                        {'passed': True, 'violations': [], 'uncertain': []})
    assert review._review('s', 'project', 'images', True, repair=False, shot_keys=['1-2'])['status'] == 'success'
    assert sorted(calls) == ['1-1', '1-2']


def test_rollback_keeps_accepted_pair_and_restores_rejected_sidecar(tmp_path, monkeypatch):
    base, items = _two_pair_project(tmp_path, monkeypatch)
    for item in items:
        ct.record_continuity_image(item)
    first = Path(items[0]['output_path'])
    second = Path(items[2]['output_path'])
    original_first, original_second = first.read_bytes(), second.read_bytes()
    second_meta = second.with_suffix('.continuity.json')
    original_meta = second_meta.read_bytes()
    def audit(task, *_):
        passed = task['id'] == '1-1' and first.read_bytes() != original_first
        frame = f"{task['id'].replace('-', ':')}:start"
        return {'passed': passed, 'violations': [] if passed else [
            {'frame_key': frame, 'observed': 'wrong', 'required': 'fix'}], 'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    review._review('s', 'project', 'images', True, repair=False)
    tasks = review._tasks(base, 'images')
    report_path = base / '97_shots/continuity_images_review.json'
    report = json.loads(report_path.read_text())
    report['failed_rounds'] = {'1-1': 1, '1-2': 1}
    report_path.write_text(json.dumps(report))
    def edit(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1', '1-2'})
        first.write_bytes(b'accepted')
        second.write_bytes(b'rejected')
        second_meta.write_bytes(b'new metadata')
    monkeypatch.setattr(review, '_repair_media', edit)
    result = review._repair_round('s', 'project', 'images', True, base, tasks, report, {})
    assert result['acceptance']['unaccepted'] == ['1-2']
    assert first.read_bytes() == b'accepted'
    assert json.loads(first.with_suffix('.continuity.json').read_text())['basis'] == 'visual_review'
    assert second.read_bytes() == original_second
    assert second_meta.read_bytes() == original_meta
    assert json.loads(report_path.read_text())['failed_rounds'] == {'1-2': 1}


def test_cached_auto_accept_does_not_clear_failed_counter(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(review, '_audit', lambda *_: {'passed': True, 'violations': [], 'uncertain': []})
    review._review('s', 'project', 'images', True, repair=False)
    path = base / '97_shots/continuity_images_review.json'
    report = json.loads(path.read_text())
    record = next(iter(report['tasks'].values()))
    record['auto_accepted'] = {'failed_rounds': 2}
    record['verdicts'] = [{'passed': False, 'violations': [{'frame_key': '1:1:start'}], 'uncertain': []}]
    report['failed_rounds'] = {'1-1': 1}
    path.write_text(json.dumps(report))
    review._review('s', 'project', 'images', True, repair=False)
    report = json.loads(path.read_text())
    assert report['failed_rounds']['1-1'] == 1
    assert next(iter(report['tasks'].values()))['verdicts'][0]['passed'] is False


def test_normal_rollback_restores_dependent_copy_and_its_sidecar(tmp_path, monkeypatch):
    base, items = _two_pair_project(tmp_path, monkeypatch)
    items[2].update(copy_from_previous_end=True, source_end_path=items[1]['output_path'])
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    source, dependent = Path(items[1]['output_path']), Path(items[2]['output_path'])
    sidecar = dependent.with_suffix('.continuity.json')
    sidecar.write_bytes(b'original metadata')
    source_before, dependent_before = source.read_bytes(), dependent.read_bytes()
    finding = {'frame_key': '1:1:end', 'observed': 'wrong', 'required': 'fix'}
    monkeypatch.setattr(review, '_audit', lambda *_: {'passed': False, 'violations': [finding], 'uncertain': []})
    review._review('s', 'project', 'images', True, repair=False, shot_keys=['1-1'])
    task = review._tasks(base, 'images', ['1-1'])[0]
    report = json.loads((base / '97_shots/continuity_images_review.json').read_text())
    def edit(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1'})
        source.write_bytes(b'bad source')
        dependent.write_bytes(b'bad copy')
        sidecar.write_bytes(b'bad metadata')
    monkeypatch.setattr(review, '_repair_media', edit)
    result = review._repair_round('s', 'project', 'images', True, base, [task], report, {}, ['1-1'])
    assert not result['acceptance']['passed']
    assert source.read_bytes() == source_before
    assert dependent.read_bytes() == dependent_before
    assert sidecar.read_bytes() == b'original metadata'


def test_exception_after_partial_pass_restores_previous_failed_rounds(tmp_path, monkeypatch):
    base, items = _two_pair_project(tmp_path, monkeypatch)
    def audit(task, *_):
        frame = f"{task['id'].replace('-', ':')}:start"
        return {'passed': False, 'violations': [{'frame_key': frame, 'observed': 'wrong', 'required': 'fix'}],
                'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    review._review('s', 'project', 'images', True, repair=False)
    path = base / '97_shots/continuity_images_review.json'
    report = json.loads(path.read_text())
    report['failed_rounds'] = {'1-1': 1, 'unrelated': 7}
    path.write_text(json.dumps(report))
    tasks = review._tasks(base, 'images')
    before = [Path(item['output_path']).read_bytes() for item in items]
    def edit(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1', '1-2'})
        for item in items:
            Path(item['output_path']).write_bytes(b'unaccepted')
    def partial_pass_then_fail(*args, **kwargs):
        current = json.loads(path.read_text())
        current['failed_rounds'].pop('1-1')
        path.write_text(json.dumps(current))
        raise ValueError('second pair audit failed')
    monkeypatch.setattr(review, '_repair_media', edit)
    monkeypatch.setattr(review, '_review', partial_pass_then_fail)
    with pytest.raises(ValueError, match='second pair audit failed'):
        review._repair_round('s', 'project', 'images', True, base, tasks, report, {})
    assert [Path(item['output_path']).read_bytes() for item in items] == before
    assert json.loads(path.read_text())['failed_rounds'] == {'1-1': 2, '1-2': 1, 'unrelated': 7}


def test_exception_restores_counter_of_rolled_back_copy_consumer(tmp_path, monkeypatch):
    base, items = _two_pair_project(tmp_path, monkeypatch)
    items[2].update(copy_from_previous_end=True, source_end_path=items[1]['output_path'])
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    def audit(task, *_):
        passed = task['id'] == '1-2'
        return {'passed': passed, 'violations': [] if passed else [
            {'frame_key': '1:1:end', 'observed': 'wrong', 'required': 'fix'}], 'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    review._review('s', 'project', 'images', True, repair=False)
    path = base / '97_shots/continuity_images_review.json'
    report = json.loads(path.read_text())
    report['failed_rounds'] = {'1-1': 1, '1-2': 3}
    path.write_text(json.dumps(report))
    tasks = review._tasks(base, 'images')
    def edit(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1'})
        Path(items[1]['output_path']).write_bytes(b'unaccepted source')
        Path(items[2]['output_path']).write_bytes(b'unaccepted copy')
    def partial_pass_then_fail(*args, **kwargs):
        current = json.loads(path.read_text())
        current['failed_rounds'] = {}
        path.write_text(json.dumps(current))
        raise ValueError('post audit failed')
    monkeypatch.setattr(review, '_repair_media', edit)
    monkeypatch.setattr(review, '_review', partial_pass_then_fail)
    with pytest.raises(ValueError, match='post audit failed'):
        review._repair_round('s', 'project', 'images', True, base, tasks, report, {})
    assert json.loads(path.read_text())['failed_rounds'] == {'1-1': 2, '1-2': 3}


def test_normal_rollback_preserves_failed_counter_of_transiently_passed_copy(tmp_path, monkeypatch):
    base, items = _two_pair_project(tmp_path, monkeypatch)
    items[2].update(copy_from_previous_end=True, source_end_path=items[1]['output_path'])
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    source, copied = Path(items[1]['output_path']), Path(items[2]['output_path'])
    source_before, copied_before = source.read_bytes(), copied.read_bytes()
    def audit(task, *_):
        passed = task['id'] == '1-2' and copied.read_bytes() != copied_before
        frame = '1:1:end' if task['id'] == '1-1' else '1:2:start'
        return {'passed': passed, 'violations': [] if passed else [
            {'frame_key': frame, 'observed': 'wrong', 'required': 'fix'}], 'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    review._review('s', 'project', 'images', True, repair=False)
    path = base / '97_shots/continuity_images_review.json'
    report = json.loads(path.read_text())
    report['failed_rounds'] = {'1-2': 1}
    path.write_text(json.dumps(report))
    tasks = review._tasks(base, 'images')
    def edit(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1'})
        source.write_bytes(b'bad source')
        copied.write_bytes(b'transient copy')
    monkeypatch.setattr(review, '_repair_media', edit)
    result = review._repair_round('s', 'project', 'images', True, base, tasks, report, {})
    assert not result['acceptance']['passed']
    assert source.read_bytes() == source_before and copied.read_bytes() == copied_before
    assert json.loads(path.read_text())['failed_rounds']['1-2'] == 1


def test_known_scoped_pair_without_continuity_entities_is_not_required(tmp_path, monkeypatch):
    base, _ = _two_pair_project(tmp_path, monkeypatch)
    path = base / '20_bible/continuity_plan.json'
    plan = json.loads(path.read_text())
    for phase in ('start', 'end'):
        plan['frames'][f'1:2:{phase}']['entities'] = []
    path.write_text(json.dumps(plan))
    result = review.continuity_acceptance(base, 'images', ['1-2'])
    assert result['passed'] and result['status'] == 'not_required'
    assert review._review('s', 'project', 'images', True, shot_keys=['1-2'])['acceptance']['status'] == 'not_required'
    assert review.continuity_acceptance(base, 'images', [])['status'] == 'invalid_inputs'
    assert review.continuity_acceptance(base, 'images', ['9-9'])['status'] == 'invalid_inputs'


def test_image_cache_key_covers_actual_landmark_caption_from_both_phases(tmp_path, monkeypatch):
    base, items = _project(tmp_path, monkeypatch)
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items[:1]}))
    master = base / '20_bible/references/shared.png'
    Image.new('RGB', (16, 16), 'red').save(master)
    path = base / '20_bible/continuity_plan.json'
    plan = json.loads(path.read_text())
    original = plan['entities'][0]
    suitcase = dict(original, id='suitcase', kind='landmark', geometry='fixed', reference_path=str(master))
    station = dict(original, id='station', kind='landmark', geometry='fixed', reference_path=str(master))
    plan['entities'] = [suitcase, station]
    plan['frames']['1:1:start']['entities'] = [{'id': 'suitcase', 'state_id': 'at_arch', 'visible': True}]
    plan['frames']['1:1:end']['entities'] = [{'id': 'station', 'state_id': 'at_arch', 'visible': True}]
    path.write_text(json.dumps(plan))
    requests = []
    monkeypatch.setattr(review, 'call_openai_api', lambda **kwargs: requests.append(kwargs) or json.dumps({
        'passed': True, 'violations': [], 'uncertain': []}))
    first = review._tasks(base, 'images')[0]
    review._audit(first, first['group'], 0)
    labels1 = [part['text'] for part in requests[-1]['prompt'] if part.get('type') == 'text']
    plan['frames']['1:1:end']['entities'][0]['id'] = 'suitcase'
    path.write_text(json.dumps(plan))
    second = review._tasks(base, 'images')[0]
    review._audit(second, second['group'], 0)
    labels2 = [part['text'] for part in requests[-1]['prompt'] if part.get('type') == 'text']
    assert any('LANDMARK.station' in label for label in labels1)
    assert any('LANDMARK.suitcase' in label for label in labels2)
    assert first['input_hash'] != second['input_hash']


def test_video_exception_preserves_actual_confirmed_pass_and_counts_unconfirmed(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    files = {key: base / f'97_shots/{key}.mp4' for key in ('1-1', '1-2')}
    for path in files.values():
        path.write_bytes(b'old')
    def tasks(base, kind):
        return [{'id': key, 'input_hash': key + ':' + path.read_text(), 'media': [str(path)],
                 'group': [{'scene_number': 1, 'shot_number': int(key[-1])}], 'anchors': []}
                for key, path in files.items()]
    initial = tasks(base, 'videos')
    report = {'tasks': {task['input_hash']: {'id': task['id'], 'passed': False,
        'verdicts': [{'passed': False, 'violations': [{'observed': 'wrong', 'required': 'fix'}],
                      'uncertain': []}]} for task in initial}, 'failed_rounds': {'1-1': 1, '1-2': 1}}
    report_path = base / '97_shots/continuity_videos_review.json'
    report_path.write_text(json.dumps(report))
    monkeypatch.setattr(review, '_tasks', lambda base, kind: tasks(base, kind) if kind == 'videos' else [])
    def repair(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1', '1-2'})
        for path in files.values():
            path.write_bytes(b'new')
    def partial_pass_then_fail(*args, **kwargs):
        current = json.loads(report_path.read_text())
        current['tasks']['1-1:new'] = {'id': '1-1', 'passed': True,
            'verdicts': [{'passed': True, 'violations': [], 'uncertain': []}]}
        current['failed_rounds'].pop('1-1')
        report_path.write_text(json.dumps(current))
        raise ValueError('second video audit failed')
    monkeypatch.setattr(review, '_repair_media', repair)
    monkeypatch.setattr(review, '_review', partial_pass_then_fail)
    with pytest.raises(ValueError, match='second video audit failed'):
        review._repair_round('s', 'project', 'videos', True, base, initial, report, {})
    assert all(path.read_bytes() == b'new' for path in files.values())
    after = json.loads(report_path.read_text())
    assert after['tasks']['1-1:new']['passed'] is True
    assert after['failed_rounds'] == {'1-2': 2}


def test_video_exception_does_not_treat_auto_accepted_as_model_pass(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    (base / '97_shots').mkdir(parents=True)
    clip = base / '97_shots/clip.mp4'
    clip.write_bytes(b'old')
    def tasks(base, kind):
        return [{'id': '1-1', 'input_hash': clip.read_text(), 'media': [str(clip)],
                 'group': [{'scene_number': 1, 'shot_number': 1}], 'anchors': []}]
    initial = tasks(base, 'videos')
    report = {'tasks': {'old': {'id': '1-1', 'passed': False, 'verdicts': [
        {'passed': False, 'violations': [{'observed': 'wrong', 'required': 'fix'}], 'uncertain': []}]}},
        'failed_rounds': {'1-1': 1}}
    path = base / '97_shots/continuity_videos_review.json'
    path.write_text(json.dumps(report))
    monkeypatch.setattr(review, '_tasks', lambda base, kind: tasks(base, kind) if kind == 'videos' else [])
    def repair(base, tasks, kind, session, on_attempt):
        on_attempt({'1-1'})
        clip.write_bytes(b'new')
    def fail(*args, **kwargs):
        current = json.loads(path.read_text())
        current['tasks']['new'] = {'id': '1-1', 'passed': True, 'auto_accepted': {'failed_rounds': 2},
            'verdicts': [{'passed': False, 'violations': [{'observed': 'wrong'}], 'uncertain': []}]}
        path.write_text(json.dumps(current))
        raise ValueError('post audit failed')
    monkeypatch.setattr(review, '_repair_media', repair)
    monkeypatch.setattr(review, '_review', fail)
    with pytest.raises(ValueError, match='post audit failed'):
        review._repair_round('s', 'project', 'videos', True, base, initial, report, {})
    result = json.loads(path.read_text())
    assert result['failed_rounds']['1-1'] == 2
    assert 'new' not in result['tasks']


def test_pair_without_landmark_roles_keeps_previous_cache_key(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    hashes = {path: review._file_hash(path) for path in set(task['media']) | set(task['anchors'])}
    expected = ct._digest([review._REVIEW_VERSION, 'images', task['contexts'], hashes,
        [item.get('english_prompt') for item in task['group']], 'image_pair_v1', {},
        [(item.get('copy_from_previous_end'), item.get('source_end_path')) for item in task['group']]])
    assert task['input_hash'] == expected
