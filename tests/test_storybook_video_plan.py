import copy
import json
from pathlib import Path

import pytest
from PIL import Image

from custom_tools.storybook import video_plan as vp


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base = tmp_path / 'plots/storybooks/test'
    (base / '97_shots').mkdir(parents=True)
    items = []
    for shot, seconds in [(1, 2), (2, 4), (3, 3)]:
        for phase in ['start', 'end']:
            items.append(dict(scene_number=1, shot_number=shot, shot_type=phase,
                              timing=f'00:{seconds:02d}', video_path=str(base / f'{shot}.mp4'),
                              video_prompt='Run', voiceover=f'line {shot}' if phase == 'start' else '',
                              width=1920, height=1088))
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    return base, items


def unit(base):
    return dict(shot_keys=['1-2', '1-3'], duration_s=7, video_path=str(base / 'continuous.mp4'),
                video_prompt='Reach the goal and stop', reference_image_paths=[str(base / 'hero.png')],
                seed=123, model='alibaba/wan-3.0', resolution='480p')


def test_video_plan_keeps_duration_dialogue_and_source_frames(project):
    base, items = project
    original = copy.deepcopy(items)
    (base / '97_shots/video_plan.json').write_text(json.dumps({'units': [unit(base)]}))
    result = vp.apply_video_plan('test', items)
    starts = [i for i in result if i['shot_type'] == 'start']
    assert len(starts) == 2
    assert starts[1]['duration_s'] == 7
    assert starts[1]['video_path'] == str(base / 'continuous.mp4')
    assert starts[1]['voiceover'] == 'line 2\nline 3'
    assert starts[1]['video_mode'] == 'references'
    assert items == original
    assert json.loads((base / '97_shots/shots.json').read_text())['items'] == original


def test_nonconsecutive_video_unit_is_rejected(project):
    base, items = project
    entry = unit(base) | {'shot_keys': ['1-1', '1-3'], 'duration_s': 5}
    (base / '97_shots/video_plan.json').write_text(json.dumps({'units': [entry]}))
    with pytest.raises(ValueError, match='contiguous'):
        vp.apply_video_plan('test', items)


def test_reference_identity_includes_all_images_mode_and_seed(project):
    base, _ = project
    ref = base / 'hero.png'
    Image.new('RGB', (32, 24), 'red').save(ref)
    second = base / 'place.png'
    Image.new('RGB', (32, 24), 'blue').save(second)
    item = dict(scene_number=1, shot_number=2, video_mode='references', duration_s=7,
                timing='00:07', video_prompt='Run', reference_image_paths=[str(ref), str(second)],
                video_seed=0, video_resolution='480p')
    first, _ = vp.reference_request(item, 'alibaba/wan-3.0', 42)
    assert first['seed'] == 0
    assert 'frame_images' not in first
    assert len(first['input_references']) == 2
    old = vp.reference_input_hash(item, 'alibaba/wan-3.0', 42)
    Image.new('RGB', (32, 24), 'green').save(second)
    assert vp.reference_input_hash(item, 'alibaba/wan-3.0', 42) != old
    item['video_model'] = 'bytedance/seedance-2.5'
    assert vp.reference_request(item, 'alibaba/wan-3.0', 42)[0]['model'] == 'bytedance/seedance-2.5'


def test_decisions_chooses_supported_method_without_full_film(monkeypatch):
    captured = {}
    monkeypatch.setenv('OPENAI_API_BASE_DB', 'http://gateway/v1')
    monkeypatch.setenv('OPENAI_API_KEY_DB', 'test')

    def post(url, **kwargs):
        captured.update(url=url, **kwargs['json'])
        return type('Response', (), {'raise_for_status': lambda s: None,
                                    'json': lambda s: {'answers': {'method': {'choice': 'references'},
                                                                 'continuity': {'choice': 'new'}}}})()

    monkeypatch.setattr(vp.requests, 'post', post)
    assert vp.choose_video_method({'duration_s': 20, 'action': 'Follow the runner'}) == {
        'method': 'references', 'continuity': 'new'}
    assert captured['url'] == 'http://gateway/v1/decisions'
    assert captured['model'] == 'llmgateway/decision'
    assert set(captured['questions']['method']['criteria']) == {'keyframes', 'references'}
    assert captured['state']['duration_s'] == 20
    assert 'inside the cabin' in captured['questions']['method']['instructions']
    assert 'inserts separately' in captured['questions']['method']['instructions']
    assert 'content, not camera labels' in captured['questions']['method']['instructions']
    assert 'collisions and destruction' in captured['questions']['method']['instructions']


def test_adopted_result_is_reused_without_post(project, monkeypatch):
    from custom_tools.storybook.video_generator_aitunnel_jobs import _ProviderJobStore
    base, items = project
    ref = base / 'hero.png'
    Image.new('RGB', (32, 24), 'red').save(ref)
    movie = base / 'approved.mp4'
    movie.write_bytes(b'existing-video')
    test = base / 'trial'
    test.mkdir()
    import hashlib
    context = dict(model='alibaba/wan-3.0', duration_seconds=7, resolution='480p',
                   aspect_ratio='16:9', seed=123, references=[{
                       'path': str(ref), 'sha256': hashlib.sha256(ref.read_bytes()).hexdigest()}])
    (test / 'context.json').write_text(json.dumps(context))
    (test / 'prompt.json').write_text(json.dumps({'video_prompt': 'Run and stop'}))
    (test / 'provider_jobs.json').write_text(json.dumps({'jobs': [{
        'status': 'downloaded', 'task_id': 'paid-test-id', 'output_path': str(movie)}]}))
    monkeypatch.setattr(vp.requests, 'post', lambda *a, **k: pytest.fail('Paid test must be reused'))
    prior = base / '97_shots/video_units/1-2_1-3.mp4'
    prior.parent.mkdir()
    prior.write_bytes(b'previous-test-video')
    vp.adopt_reference_test('test', test, ['1-2', '1-3'])
    assert prior.read_bytes() == b'previous-test-video'
    item = vp.apply_video_plan('test', items)[2]
    store = _ProviderJobStore(str(base / '97_shots/provider_jobs.json'), provider_name='routerai')
    assert vp.trusted_reference_output(item, store, 'alibaba/wan-3.0', None)
    result = vp.generate_reference_video(item, 'secret', 'alibaba/wan-3.0', None, store, {
        'alibaba/wan-3.0': {'supports_image_references': True, 'max_image_references': 10,
                          'supported_durations': list(range(2, 31))}})
    assert result['success']
    assert movie.read_bytes() == b'existing-video'
    assert Path(item['video_path']).read_bytes() == movie.read_bytes()
    from custom_tools.storybook import video_generator_aitunnel_tool as generator
    from custom_tools.storybook.video_generator_routerai_tool import video_generator_routerai_tool
    monkeypatch.setenv('ROUTERAI_API_KEY', 'test')
    monkeypatch.setenv('ROUTERAI_VIDEO_MODEL', 'alibaba/wan-3.0')
    monkeypatch.setattr(generator, '_get_routerai_video_models', lambda: {
        'alibaba/wan-3.0': {'supports_image_references': True, 'max_image_references': 10,
                          'supported_durations': list(range(2, 31))}})
    reused = video_generator_routerai_tool('test', 'test', enable=True, skip_prompt_enhancement=True,
                                          sample_before_batch=True, sample_shot_key='1-2')
    assert reused['stats'] == {'total': 1, 'successful': 1, 'failed': 0, 'remaining': 0}


def test_reference_generation_sends_one_request_and_reuses_result(project, monkeypatch):
    from custom_tools.storybook import video_generator_aitunnel_tool as generator
    from custom_tools.storybook.video_generator_aitunnel_jobs import _ProviderJobStore
    base, items = project
    Image.new('RGB', (32, 24), 'red').save(base / 'hero.png')
    (base / '97_shots/video_plan.json').write_text(json.dumps({'units': [unit(base)]}))
    item = vp.apply_video_plan('test', items)[2]
    # Existing storyboard anchors must not leak into reference-only requests.
    item['start_image'] = item['end_image'] = str(base / 'hero.png')
    store = _ProviderJobStore(str(base / '97_shots/provider_jobs.json'), provider_name='routerai')
    posts = []

    def post(url, **kwargs):
        posts.append(kwargs['json'])
        assert kwargs['timeout'] == 1200
        return type('Response', (), {'raise_for_status': lambda s: None,
                                    'json': lambda s: {'id': 'one-paid-task'}})()

    monkeypatch.setattr(vp.requests, 'post', post)
    monkeypatch.setattr(generator, '_wait_for_video_completion_aitunnel',
                        lambda *a, **k: {'unsigned_urls': ['https://cdn.example/result.mp4']})
    monkeypatch.setattr(generator, '_download_video_aitunnel', lambda url, path, headers: Path(path).write_bytes(b'video'))
    catalog = {'alibaba/wan-3.0': {'supports_image_references': True, 'max_image_references': None,
                                 'supported_durations': list(range(2, 31))}}
    assert vp.generate_reference_video(item, 'test', 'alibaba/wan-3.0', None, store, catalog)['success']
    assert vp.generate_reference_video(item, 'test', 'alibaba/wan-3.0', None, store, catalog)['success']
    assert len(posts) == 1
    assert 'frame_images' not in posts[0]
    assert len(posts[0]['input_references']) == 1
    assert posts[0]['duration'] == 7 and posts[0]['seed'] == 123


def test_video_groups_ignore_character_list_order():
    items = [dict(scene_number=1, shot_number=n, shot_type='start', timing='00:04',
                  _shot_frame_spec={'start_state_spec': {'visible_characters': cast}})
             for n, cast in [(1, ['A', 'B']), (2, ['B', 'A'])]]
    choices = {vp._key(i): {'method': 'references', 'continuity': 'continue'} for i in items}
    assert len(vp._action_groups(items, choices)) == 1


def test_reference_prompt_binds_final_image_and_saved_style(project, monkeypatch):
    import utils
    base, items = project
    (base / '30_style').mkdir()
    (base / '30_style/image_styles.json').write_text(json.dumps({
        'default_style_id': 'other', 'styles': {'other': {'prompt': 'WRONG'}, 'ink': {'prompt': 'SAVED INK STYLE'}},
        'frame_bindings': {'1:2:start': 'ink'}}))
    final = base / 'ending.png'
    final.write_bytes(b'final')
    style_image = base / 'style.png'
    style_image.write_bytes(b'dedicated style')
    captured = {}

    def generate(**kwargs):
        captured.update(kwargs)
        return '{"video_prompt":"Use Image 2 for style; travel, then finish on Image 3."}'

    monkeypatch.setattr(utils, 'call_openai_api', generate)
    group = vp._starts(items)[1:]
    group[0]['_continuity'] = {'start': {'entities': [{
        'id': 'arch', 'name': 'Station arch', 'kind': 'landmark', 'reference_path': 'hero.png'}]}}
    prompt, refs = vp.prepare_reference_prompt(base, group, {}, ['hero.png'], final_image=final, style_image=style_image)
    assert refs == ['hero.png', str(style_image), str(final)]
    assert json.loads(captured['prompt'])['final_image_number'] == 3
    assert json.loads(captured['prompt'])['style_image_number'] == 2
    assert json.loads(captured['prompt'])['landmark_image_roles'] == [
        {'entity_id': 'arch', 'name': 'Station arch', 'image_number': 1}]
    assert prompt == 'SAVED INK STYLE\n\nUse Image 2 for style; travel, then finish on Image 3.'
    assert 'not the choice of style' in captured['system_prompt']
    assert 'WRONG' not in prompt
    assert 'ordinary image reference' in captured['system_prompt']
    assert 'Reserve the last two seconds' not in captured['system_prompt']
    assert 'Do not invent fixed-duration holds' in captured['system_prompt']
    assert 'Snapshot limb positions are not timed instructions' in captured['system_prompt']


def planning_setup(project, monkeypatch):
    from custom_tools.storybook import video_contract, video_generator_common
    base, items = project
    from custom_tools.storybook import style_keeper
    (base / '30_style').mkdir(exist_ok=True)
    (base / '30_style/image_styles.json').write_text(json.dumps({
        'version': 2, 'default_style_id': 'ink', 'styles': {'ink': {'prompt': 'Ink on paper'}}, 'frame_bindings': {}}))
    def style_refs(base, catalog, style_ids, session_id, seed, items):
        result = {}
        for style_id in style_ids:
            path = base / f'style_{style_id}.png'
            path.write_bytes(b'dedicated style')
            result[style_id] = str(path.resolve())
        return result
    monkeypatch.setattr(style_keeper, 'ensure_video_style_references', style_refs)
    hero = base / 'hero.png'
    hero.write_bytes(b'identity')
    for i in items:
        i['characters'] = [{'name': 'Hero', 'reference_image_path': str(hero)}]
        i['locations'] = [{'name': 'Courtyard'}]
        i['output_path'] = vp._frame_path(i)
        if i['shot_type'] == 'end':
            i['reference_image_paths'] = [vp._frame_path(i, 'start'), str(hero)]
    monkeypatch.setenv('ROUTERAI_VIDEO_MODEL', 'alibaba/wan-3.0')
    monkeypatch.setenv('ROUTERAI_RESOLUTION', '480p')
    monkeypatch.setattr(video_contract, '_active_video_tool_name', lambda: 'video_generator_routerai_tool')
    monkeypatch.setattr(video_generator_common, '_get_routerai_video_models', lambda: {
        'alibaba/wan-3.0': {'supports_image_references': True, 'supported_durations': list(range(2, 31)),
                          'max_image_references': 10}})
    calls = []

    def compile_prompt(base, group, state, refs, language, final_image=None, initial_image=None, continuity_image=None, style_image=None):
        calls.append((group, state, initial_image, final_image, continuity_image))
        return 'Prepared action', list(dict.fromkeys(refs + [p for p in [style_image, initial_image, final_image, continuity_image] if p]))

    monkeypatch.setattr(vp, 'prepare_reference_prompt', compile_prompt)
    return base, items, calls


def test_plan_selects_mixed_action_ranges_before_images(project, monkeypatch):
    base, items, calls = planning_setup(project, monkeypatch)
    source = {'items': items, 'seed': 123}
    raw = json.dumps(source)
    (base / '97_shots/shots.json').write_text(raw)
    decisions = []

    def choose(state):
        decisions.append(state)
        return {'method': 'keyframes' if state['current']['shot_key'] == '1-1' else 'references',
                'continuity': 'continue'}

    monkeypatch.setattr(vp, 'choose_video_method', choose)
    result = vp.storybook_video_plan_tool('s', 'test', shots_data=source)
    plan = json.loads((base / '97_shots/video_plan.json').read_text())
    assert plan['units'][0]['shot_keys'] == ['1-2', '1-3']
    assert plan['units'][0]['duration_s'] == 7
    assert [(i['shot_number'], i['shot_type']) for i in result['items']] == [(1, 'start'), (1, 'end'), (2, 'start'), (3, 'end')]
    assert not Path(plan['units'][0]['start_reference_path']).exists()
    end = result['items'][-1]
    assert end['reference_image_paths'][0] == plan['units'][0]['start_reference_path']
    assert (base / '97_shots/shots.json').read_text() == raw
    assert len(decisions) == 3 and all(set(s) == {'previous', 'current', 'next'} for s in decisions)
    assert [s['shot_key'] for s in calls[0][1]['shots']] == ['1-2', '1-3']
    items[0]['image_path'] = 'new rendered image metadata'
    assert vp.storybook_video_plan_tool('s', 'test', shots_data=source)['cached']
    assert len(decisions) == 3


def test_long_action_uses_shared_boundary_and_30_second_limit(project, monkeypatch):
    base, items, calls = planning_setup(project, monkeypatch)
    for i in items:
        i['duration_s'] = 15
    monkeypatch.setattr(vp, 'choose_video_method', lambda state: {'method': 'references', 'continuity': 'continue'})
    result = vp.storybook_video_plan_tool('s', 'test', shots_data={'items': items})
    units = json.loads((base / '97_shots/video_plan.json').read_text())['units']
    assert [u['duration_s'] for u in units] == [30, 15]
    assert units[0]['end_reference_path'] == units[1]['start_reference_path']
    assert units[1]['continues_from_shot'] == '1-2'
    assert len(result['items']) == 3  # opening, shared boundary, ending
    kept = {i['output_path'] for i in result['items']}
    planned_paths = {i['output_path'] for i in items}
    assert all(p not in planned_paths or p in kept
               for i in result['items'] for p in i.get('reference_image_paths', []))
    assert vp._split_action(vp._starts(items), {'supported_durations': [15, 30, 45]}) == [vp._starts(items)[:2], vp._starts(items)[2:]]


def test_action_resumes_after_other_scenes_without_merging_them(project, monkeypatch):
    base, items, calls = planning_setup(project, monkeypatch)
    for i in items:
        i['scene_number'] = i['shot_number']
        i['shot_number'] = 1
        i['output_path'] = vp._frame_path(dict(i, output_path=None))
        i.pop('reference_image_paths', None)
        if i['scene_number'] == 2:
            i['locations'] = [{'name': 'Another world'}]
    monkeypatch.setattr(vp, 'choose_video_method', lambda state: {
        'method': 'keyframes' if state['current']['shot_key'] == '2-1' else 'references', 'continuity': 'continue'})
    resumes = []

    def resume(state, questions):
        resumes.append(state)
        assert set(questions['resume']['criteria']) == {'new', '1-1'}
        return {'resume': '1-1'}

    monkeypatch.setattr(vp, '_decide', resume)
    vp.storybook_video_plan_tool('s', 'test', shots_data={'items': items})
    units = json.loads((base / '97_shots/video_plan.json').read_text())['units']
    assert [u['shot_keys'] for u in units] == [['1-1'], ['3-1']]
    assert units[1]['continues_from_shot'] == '1-1'
    assert units[0]['end_reference_path'] in units[1]['reference_image_paths']
    assert resumes[0]['current'][0]['shot_key'] == '3-1'
    resumed_call = next(call for call in calls if vp._key(call[0][0]) == '3-1')
    assert resumed_call[4] == units[0]['end_reference_path']


def test_new_action_and_appearance_change_prevent_merging():
    items = [dict(scene_number=1, shot_number=i, duration_s=4, style_id='ink', locations=[{'name': 'Yard'}],
                  character_reference_states={'Hero': {'state_id': state}})
             for i, state in [(1, 'coat'), (2, 'coat'), (3, 'no-coat')]]
    choices = {vp._key(i): {'method': 'references', 'continuity': 'continue'} for i in items}
    choices['1-2']['continuity'] = 'new'
    assert [[vp._key(i) for i in g] for g in vp._action_groups(items, choices)] == [['1-1'], ['1-2'], ['1-3']]
    with pytest.raises(ValueError, match='cannot fit'):
        vp._split_action([dict(items[0], duration_s=31)], {'supported_durations': list(range(2, 31))})


def test_keyframe_only_plan_never_generates_style_images(project, monkeypatch):
    from custom_tools.storybook import style_keeper
    base, items, _ = planning_setup(project, monkeypatch)
    monkeypatch.setattr(vp, 'choose_video_method', lambda state: {'method': 'keyframes', 'continuity': 'new'})
    monkeypatch.setattr(style_keeper, 'ensure_video_style_references', lambda *a, **k: pytest.fail('No reference videos'))
    vp.storybook_video_plan_tool('s', 'test', shots_data={'items': items})
    assert json.loads((base / '97_shots/video_plan.json').read_text())['units'] == []


def test_style_reference_instruction_change_invalidates_video_plan(project, monkeypatch):
    from custom_tools.storybook import style_keeper
    _, items, _ = planning_setup(project, monkeypatch)
    before = vp._fingerprint(items)
    monkeypatch.setattr(style_keeper, '_STYLE_REFERENCE_INSTRUCTION', 'Changed style showcase requirements')
    assert vp._fingerprint(items) != before


def test_reference_units_receive_only_their_own_style_image(project, monkeypatch):
    from custom_tools.storybook import style_keeper
    base, items, _ = planning_setup(project, monkeypatch)
    (base / '30_style/image_styles.json').write_text(json.dumps({
        'version': 2, 'default_style_id': 'ink', 'frame_bindings': {},
        'styles': {'ink': {'prompt': 'Ink'}, 'wash': {'prompt': 'Wash'}, 'pencil': {'prompt': 'Pencil'}},
        'location_bindings': {'Courtyard': 'ink', 'Terrace': 'wash'}}))
    for item in items:
        if item['shot_number'] == 1:
            item['style_id'] = 'pencil'
        elif item['shot_number'] == 2:
            item['locations'] = [{'name': 'Terrace'}]
    requested = []

    def references(base, catalog, style_ids, session_id, seed, items):
        requested.append(set(style_ids))
        result = {}
        for style_id in style_ids:
            path = base / f'{style_id}.png'
            path.write_bytes(b'dedicated style')
            result[style_id] = str(path.resolve())
        return result

    monkeypatch.setattr(style_keeper, 'ensure_video_style_references', references)
    monkeypatch.setattr(vp, 'choose_video_method', lambda state: {
        'method': 'keyframes' if state['current']['shot_key'] == '1-1' else 'references', 'continuity': 'continue'})
    vp.storybook_video_plan_tool('s', 'test', shots_data={'items': items, 'seed': 123})
    assert requested == [{'ink', 'wash'}]
    bindings = json.loads((base / '30_style/image_styles.json').read_text())['frame_bindings']
    assert bindings['1:1:start'] == 'pencil' and bindings['1:1:end'] == 'pencil'
    assert bindings['1:2:start'] == 'wash' and bindings['1:2:end'] == 'wash'
    assert bindings['1:3:start'] == 'ink' and bindings['1:3:end'] == 'ink'
    units = json.loads((base / '97_shots/video_plan.json').read_text())['units']
    assert [unit['style_id'] for unit in units] == ['wash', 'ink']
    for unit in units:
        assert unit['style_reference_path'] in unit['reference_image_paths']
        other = 'ink' if unit['style_id'] == 'wash' else 'wash'
        assert str((base / f'{other}.png').resolve()) not in unit['reference_image_paths']


def test_photo_export_uses_only_selected_boundaries_and_preserves_duration(project, monkeypatch):
    from custom_tools.storybook.screenplay_shots_generator_utils.fcpxml_generator import _generate_photo_fcpxml
    from custom_tools.storybook.timeline_media import _seconds
    import xml.etree.ElementTree as ET
    base, items, _ = planning_setup(project, monkeypatch)
    monkeypatch.setattr(vp, 'choose_video_method', lambda state: {
        'method': 'keyframes' if state['current']['shot_key'] == '1-1' else 'references', 'continuity': 'continue'})
    result = vp.storybook_video_plan_tool('s', 'test', shots_data={'items': items})
    from PIL import Image
    for item in result['items']:
        p = Path(item['output_path'])
        p.parent.mkdir(exist_ok=True)
        Image.new('RGB', (32, 24), (100, 100, 100)).save(p)
    output = base / '97_shots/photo.fcpxml'
    assert _generate_photo_fcpxml('test', items, str(output))
    root = ET.parse(output).getroot()
    assert _seconds(root.find('.//sequence').get('duration')) == 9
    assert len(root.findall('.//spine/video')) == 4
    from urllib.parse import unquote, urlparse
    sources = [Path(unquote(urlparse(r.get('src')).path)) for r in root.findall('.//asset/media-rep')]
    assert len(sources) == 4 and all(p.is_file() for p in sources)


def test_background_renderer_returns_only_planned_image_tasks(project, monkeypatch):
    from custom_tools.storybook.background_references import storybook_background_references_tool
    base, items, _ = planning_setup(project, monkeypatch)
    monkeypatch.setattr(vp, 'choose_video_method', lambda state: {
        'method': 'keyframes' if state['current']['shot_key'] == '1-1' else 'references', 'continuity': 'continue'})
    (base / '97_shots/shots.json').write_text(json.dumps({'items': items}))
    selected = vp.storybook_video_plan_tool('s', 'test', shots_data={'items': items})
    reference_dir = base / '20_bible/references'
    reference_dir.mkdir(parents=True)
    (base / '20_bible/locations.json').write_text(json.dumps([{'name': 'Courtyard'}]))
    (reference_dir / 'background_references.json').write_text(json.dumps({
        'input_hash': 'planned', 'sheets': [], 'frame_bindings': {}}))
    result = storybook_background_references_tool('s', 'test', dict(selected, _background_plan_hash='planned'))
    assert len(result['items']) == 4
    assert len(json.loads((base / '97_shots/shots.json').read_text())['items']) == 6
    assert result['items'][-1]['reference_image_paths'][0] == result['items'][-2]['output_path']
    scoped = storybook_background_references_tool('s', 'test', dict(selected,
        _background_plan_hash='planned', scope_shot_keys=['1-1']))
    assert [vp._key(i) for i in scoped['items']] == ['1-1', '1-1']


def test_classification_runs_four_at_a_time(project, monkeypatch):
    import threading
    base, items, _ = planning_setup(project, monkeypatch)
    extra = [dict(i, shot_number=4) for i in items[:2]]
    barrier = threading.Barrier(4)
    seen = []

    def choose(state):
        seen.append(state['current']['shot_key'])
        barrier.wait(timeout=5)
        return {'method': 'keyframes', 'continuity': 'new'}

    monkeypatch.setattr(vp, 'choose_video_method', choose)
    vp.storybook_video_plan_tool('s', 'test', shots_data={'items': items + extra})
    assert set(seen) == {'1-1', '1-2', '1-3', '1-4'}


def test_action_is_not_silently_changed_to_keyframes_when_unsupported(project, monkeypatch):
    from custom_tools.storybook import video_generator_common
    _, items, _ = planning_setup(project, monkeypatch)
    monkeypatch.setattr(vp, 'choose_video_method', lambda state: {'method': 'references', 'continuity': 'continue'})
    monkeypatch.setattr(video_generator_common, '_get_routerai_video_models', lambda: {'alibaba/wan-3.0': {}})
    with pytest.raises(ValueError, match='does not support reference videos'):
        vp.storybook_video_plan_tool('s', 'test', shots_data={'items': items})
    assert [len(g) for g in vp._split_action([dict(i, duration_s=8) for i in vp._starts(items)],
                                           {'supported_durations': list(range(2, 13))})] == [1, 1, 1]


def test_scoped_planning_preserves_other_units_and_limits_render_items(project, monkeypatch):
    base, items, _ = planning_setup(project, monkeypatch)
    kept = unit(base)
    (base / '97_shots/video_plan.json').write_text(json.dumps({'units': [kept], 'decisions': [{
        'shot_keys': ['1-2', '1-3'], 'method': 'references'}]}))
    seen = []

    def choose(state):
        seen.append(state)
        return {'method': 'keyframes', 'continuity': 'new'}

    monkeypatch.setattr(vp, 'choose_video_method', choose)
    result = vp.storybook_video_plan_tool('s', 'test', shots_data={'items': items}, shot_keys=['1-1'])
    saved = json.loads((base / '97_shots/video_plan.json').read_text())
    assert saved['units'] == [kept]
    assert [s['current']['shot_key'] for s in seen] == ['1-1']
    assert seen[0]['next']['shot_key'] == '1-2'
    assert [vp._key(i) for i in result['items']] == ['1-1', '1-1']
    assert saved['source_hash'] != vp._fingerprint(items)
    with pytest.raises(ValueError, match='complete existing video unit'):
        vp.storybook_video_plan_tool('s', 'test', shots_data={'items': items}, shot_keys=['1-2'])
    choices = {vp._key(i): {'method': 'references', 'continuity': 'continue'} for i in vp._starts(items)}
    starts = vp._starts(items)
    assert len(vp._action_groups([starts[0], starts[2]], choices, {'1-1': 0, '1-2': 1, '1-3': 2})) == 2


def test_reference_submit_error_is_saved_and_not_repeated(project, monkeypatch):
    from custom_tools.storybook.video_generator_aitunnel_jobs import _ProviderJobStore
    base, _ = project
    ref = base / 'hero.png'
    Image.new('RGB', (32, 24), 'red').save(ref)
    item = dict(scene_number=1, shot_number=2, duration_s=4, video_prompt='Run',
                reference_image_paths=[str(ref)], video_path=str(base / 'new.mp4'))
    store = _ProviderJobStore(str(base / '97_shots/provider_jobs.json'), provider_name='routerai')
    posts = []

    def post(*args, **kwargs):
        posts.append(kwargs)
        response = vp.requests.Response()
        response.status_code = 504
        response._content = b'upstream timeout'
        response.url = 'https://routerai.ru/api/v1/videos'
        return response

    monkeypatch.setattr(vp.requests, 'post', post)
    catalog = {'wan': {'supports_image_references': True, 'supported_durations': [4], 'max_image_references': 10}}
    with pytest.raises(vp.requests.HTTPError):
        vp.generate_reference_video(item, 'test', 'wan', None, store, catalog)
    job = store.find_current_job('1-2', vp.reference_input_hash(item, 'wan', None))
    assert job['error'] == '504: upstream timeout' and job['status'] == 'submitting'
    with pytest.raises(RuntimeError, match='without ID'):
        vp.generate_reference_video(item, 'test', 'wan', None, store, catalog)
    assert len(posts) == 1


def test_reference_prompt_discovers_existing_opening_and_binds_visible_subjects(project, monkeypatch):
    import utils
    base, items = project
    group = vp._starts(items)[1:]
    opening = Path(group[0]['video_path']).parent / 'img_final_start_01_02.png'
    opening.write_bytes(b'opening')
    final = base / 'ending.png'
    final.write_bytes(b'ending')
    captured = {}

    def generate(**kwargs):
        captured.update(kwargs)
        return '{"video_prompt":"Begin in motion on Image 2; reach Image 3."}'

    monkeypatch.setattr(utils, 'call_openai_api', generate)
    _, refs = vp.prepare_reference_prompt(base, group, {}, ['hero.png'], final_image=final)
    assert refs == ['hero.png', str(opening.resolve()), str(final.resolve())]
    assert json.loads(captured['prompt'])['initial_image_number'] == 2
    assert 'visible from the first frame' in captured['system_prompt']


def test_router_reference_jpeg_transport_keeps_source_identity_and_aitunnel_png(project):
    import base64
    import io
    base, _ = project
    paths = [base / 'red.png', base / 'blue.png']
    for path, color in zip(paths, ('red', 'blue')):
        Image.new('RGB', (48, 32), color).save(path)
    originals = [path.read_bytes() for path in paths]
    item = dict(scene_number=1, shot_number=1, duration_s=5, video_prompt='Action',
                reference_image_paths=list(map(str, paths)), width=48, height=32)
    router, router_identity = vp.reference_request(item, 'model', 7, 'routerai')
    other, other_identity = vp.reference_request(item, 'model', 7, 'aitunnel')
    assert router_identity == other_identity
    assert vp.reference_input_hash(item, 'model', 7, 'routerai') == vp._digest(other_identity)
    for index, entry in enumerate(router['input_references']):
        url = entry['image_url']['url']
        assert url.startswith('data:image/jpeg;base64,')
        with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as image:
            assert image.format == 'JPEG' and image.size == (48, 32)
            red, _, blue = image.getpixel((10, 10))
            assert (red > blue) == (index == 0)
    assert all(entry['image_url']['url'].startswith('data:image/png;base64,') for entry in other['input_references'])
    assert [path.read_bytes() for path in paths] == originals
    assert router['seed'] == 7 and router['prompt'] == item['video_prompt']
