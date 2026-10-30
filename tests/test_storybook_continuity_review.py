import json
import hashlib
from pathlib import Path

from PIL import Image
import pytest

from custom_tools.storybook import continuity as ct
from custom_tools.storybook import continuity_review as review

_REAL_REPAIR_MEDIA = review._repair_media


def _project(tmp_path, monkeypatch):
    monkeypatch.setattr(review, '_repair_media', lambda *args: None)
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    base = tmp_path / 'project'
    for directory in ('20_bible', '20_bible/references', '91_screenplay', '97_shots', 'inputs'):
        (base / directory).mkdir(parents=True)
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
    assert blocks[1]['text'].startswith('LANDMARK.')
    assert blocks[3]['text'] == 'VIDEO.0 t=0.00s'
    for block, size, channel in [(blocks[2], (40, 24), 0), (blocks[4], (48, 32), 2)]:
        with Image.open(io.BytesIO(base64.b64decode(block['image_url']['url'].split(',', 1)[1]))) as image:
            assert image.format == 'JPEG' and image.size == size
            assert image.getpixel((10, 10))[channel] > 240
    assert frame.read_bytes() == before


def test_failed_or_uncertain_audit_is_not_accepted(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(review, '_audit', lambda *args: {
        'passed': False, 'violations': [], 'uncertain': ['unclear object state']})
    result = review.storybook_continuity_images_review_tool('session', 'project')
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


def test_failed_review_repairs_once_and_stays_unaccepted(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(review, '_audit', lambda *args: {'passed': False,
        'violations': [{'frame_key': '1:1:start'}], 'uncertain': []})
    repairs = []
    monkeypatch.setattr(review, '_repair_media', lambda *args: repairs.append(args))
    result = review.storybook_continuity_images_review_tool('session', 'project')
    assert result['status'] == 'error'
    assert len(repairs) == 1
    assert review.continuity_acceptance(base, 'images')['status'] == 'review_required'


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
    assert {(context['shot_number'], context['phase']) for context in tasks[0]['contexts']} == {
        (1, 'start'), (2, 'end')}


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


def test_repair_scope_uses_only_localized_frame_violation_but_both_phases(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch)
    report = {'tasks': {'group': {'id': '1-1|1-2|1-3', 'verdicts': [
        {'violations': [{'frame_key': '1:2:end'}], 'uncertain': []}]}}}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report))
    task = {'id': '1-1|1-2|1-3', 'input_hash': 'group', 'group': items}
    _REAL_REPAIR_MEDIA(base, [task], 'images', 'session')
    assert calls['qa'][0]['shot_keys'] == ['1-2']
    artist_items = calls['artist'][0]['items']['items']
    assert {item['shot_number'] for item in artist_items} == {2}
    assert {item['shot_type'] for item in artist_items} == {'start', 'end'}


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
        {'violations': [{'frame_key': '1:2:end'}], 'uncertain': []}]}}}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-2', 'input_hash': 'one', 'group': [by_key[(2, 'end')]]}], 'images', 'session')
    assert calls['qa'][0]['shot_keys'] == ['1-2', '1-3', '1-4']
    assert {item['shot_number'] for item in calls['artist'][0]['items']['items']} == {2, 3, 4}


def test_uncertain_unlocalized_review_repairs_whole_group(tmp_path, monkeypatch):
    base, items, calls = _repair_scope_fixture(tmp_path, monkeypatch)
    report = {'tasks': {'group': {'id': '1-1|1-2|1-3', 'verdicts': [
        {'violations': [], 'uncertain': ['critical unresolved identity']}]}}}
    (base / '97_shots/continuity_images_review.json').write_text(json.dumps(report))
    _REAL_REPAIR_MEDIA(base, [{'id': '1-1|1-2|1-3', 'input_hash': 'group', 'group': items}], 'images', 'session')
    assert calls['qa'][0]['shot_keys'] == ['1-1', '1-2', '1-3']


def test_event_frame_indices_scale_deduplicate_and_bound():
    assert review._video_sample_indices([4, 4], 4, 2, 30, 60)[-2:] == [58, 59]
    assert review._video_sample_indices([0], 4, 2, 60, 120)[:2] == [0, 1]
    assert review._video_sample_indices([4], 4, 2, 60, 120)[-2:] == [118, 119]
    assert review._video_sample_indices([4], 4, 2.002, 30000 / 1001, 60)[-2:] == [58, 59]
    indices = review._video_sample_indices([2, 2], 4, 2, 10, 20)
    assert len(indices) == len(set(indices)) and {9, 10, 11} <= set(indices)


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
    assert 't=0.96s' in labels and 't=1.00s' in labels and 't=1.04s' in labels
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
    assert no_events['input_hash'] == ct._digest([review._REVIEW_VERSION, 'videos', no_events['contexts'], hashes, [None], 'video_sampling_v2'])


@pytest.mark.parametrize('source_passes,changed_dependency', [(True, False), (True, True), (False, False)])
def test_video_repair_checks_used_source_frames_first(tmp_path, monkeypatch, source_passes, changed_dependency):
    import subprocess
    base, items = _project(tmp_path, monkeypatch)
    clip = base / '97_shots/test.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=32x32:r=5:d=1',
                    '-c:v', 'mpeg4', str(clip)], check=True)
    start, end = [str(Path(item['output_path']).resolve()) for item in items]
    internal = dict(items[0], shot_number=2, output_path=str(base / 'unused_internal.png'))
    catalog = [dict(items[0]), dict(items[1], shot_number=2), internal]
    primary = {'id': '1-1|1-2', 'input_hash': 'primary', 'state_hash': 'state', 'media': [str(clip)],
               'anchors': [start, end], 'planned_duration': 1, 'contexts': [{'time_s': 0}, {'time_s': 1}],
               'group': [{'scene_number': 1, 'shot_number': 1, 'video_mode': 'references'}]}
    dependent = dict(primary, id='2-1', input_hash='dependent',
                     group=[{'scene_number': 2, 'shot_number': 1}])
    state = {'source_done': False}
    order, forwarded, regenerated = [], [], []
    def tasks(_, kind):
        if kind == 'images':
            return [{'group': catalog}]
        dependency_hash = 'changed' if state['source_done'] and changed_dependency else 'dependent'
        return [dict(primary, input_hash='generated-primary' if primary['id'] in regenerated else 'primary'),
                dict(dependent, input_hash='generated-dependent' if dependent['id'] in regenerated else dependency_hash)]
    monkeypatch.setattr(review, '_tasks', tasks)
    monkeypatch.setattr(review, 'continuity_acceptance', lambda *_: {'passed': bool(regenerated)})
    observation = {'frame_key': 'VIDEO.0 t=0.00s', 'entity_id': 'suitcase',
                   'observed': 'An extra object is visible', 'required': 'Preserve the registered object count'}
    def audit(task, *_):
        passed = task['id'] == '2-1' or bool(regenerated)
        return {'passed': passed, 'violations': [] if passed else [observation], 'uncertain': []}
    monkeypatch.setattr(review, '_audit', audit)
    real_review = review._review
    def dispatch(session, project, kind, enable, **kwargs):
        if kind == 'images':
            order.append('images')
            forwarded.extend(kwargs['review_findings'])
            state['source_done'] = source_passes
            return {'status': 'success' if source_passes else 'error'}
        return real_review(session, project, kind, enable, **kwargs)
    monkeypatch.setattr(review, '_review', dispatch)
    def repair(_, selected, kind, session):
        order.append(kind)
        regenerated.extend(task['id'] for task in selected)
    monkeypatch.setattr(review, '_repair_media', repair)
    if source_passes:
        assert real_review('s', 'project', 'videos', True)['status'] == 'success'
        assert order == ['images', 'videos']
        assert regenerated == (['1-1|1-2', '2-1'] if changed_dependency else ['1-1|1-2'])
    else:
        with pytest.raises(RuntimeError, match='video generation was not started'):
            real_review('s', 'project', 'videos', True)
        assert order == ['images'] and not regenerated
    assert {finding['frame_key'] for finding in forwarded} == {'1:1:start', '1:2:end'}
    assert all(finding['source'] == 'video' and finding['video_frame_key'] == observation['frame_key']
               and finding['observed'] == observation['observed'] for finding in forwarded)


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
    assert len(calls) == 1 and len(generated) == 1
    assert json.loads(calls[0]['prompt'])['existing_prompt'] == items[0]['video_prompt']
    instruction = calls[0]['system_prompt']
    for text in ('Перепиши коротко', 'повторы', 'противоречия', 'микродвижения', 'оптику',
                 'физику', 'Image N и их роли', 'физические условия', 'START/END'):
        assert text in instruction
    assert generated[0][2] == (['1-1', '1-2'] if changed_dependency else ['1-1'])
    assert generated[0][3]['skip_prompt_enhancement'] is True
    if mode == 'references':
        assert json.loads((base / '97_shots/video_plan.json').read_text())['units'][0]['video_prompt'] == 'Fixed action'
    else:
        assert json.loads((base / '97_shots/shots.json').read_text())['items'][0]['video_prompt'] == 'Fixed action'
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
           'review_version': 5, 'verdicts': [{'violations': [finding, finding]}]}
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
    assert {(item['shot_number'], item['shot_type']) for item in selected} == {(2, 'end'), (3, 'start'), (4, 'start')}
    assert Path(untouched['output_path']).read_bytes() == before
    assert not Path(untouched['output_path']).with_suffix('.continuity.json').exists()


def test_failed_image_cache_refreshes_and_rewrites_prior_finding(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    task = review._tasks(base, 'images')[0]
    old = {'frame_key': '1:1:end', 'entity_id': 'suitcase', 'observed': 'misplaced', 'required': 'correct arrangement'}
    new = dict(old, required='Move the object right of the landmark; preserve the landmark.')
    record = {'id': task['id'], 'passed': False, 'state_hash': task['state_hash'],
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
    selected = calls['artist'][0]['items']['items']
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


def test_pair_audits_isolate_findings_masters_and_only_use_accepted_previous(tmp_path, monkeypatch):
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
    assert [item['shot_type'] for item in calls[1][1]].count('CONTINUITY_REFERENCE') == 1
    assert all(item['shot_type'] != 'CONTINUITY_REFERENCE' for item in calls[2][1])
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


def test_failed_artist_stops_before_postrepair_audit(tmp_path, monkeypatch):
    base, _ = _project(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(review, '_audit', lambda *args: calls.append(args) or {
        'passed': False, 'violations': [{'frame_key': '1:1:end', 'observed': 'wrong', 'required': 'correct'}], 'uncertain': []})
    monkeypatch.setattr('custom_tools.storybook.artist_batch_edit.artist_agent_batch_edit_tool',
                        lambda **kwargs: json.dumps([{'ok': False, 'error': 'render failed'}]))
    monkeypatch.setattr(review, '_repair_media', _REAL_REPAIR_MEDIA)
    with pytest.raises(RuntimeError, match='Continuity image repair failed'):
        review.storybook_continuity_images_review_tool('s', 'project')
    assert len(calls) == 1




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
    previous = pixels[f't={(2 * fps - 2) / fps:.2f}s']
    assert last[2] > last[0] + 100 and previous[0] > previous[2] + 100
