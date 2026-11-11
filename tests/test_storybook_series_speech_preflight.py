import json

import pytest

from custom_tools.storybook import video_plan
from custom_tools.storybook import screenplay_shots_generator as shots
from custom_tools.storybook.project_paths import series_scope
from custom_tools.storybook.studio.capabilities import invoke


@pytest.fixture
def series(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = invoke('projects_create', {'title': 'World', 'description': 'A shared world', 'genre': 'fairy', 'target_age': 'all'})['project_id']
    saved = invoke('shared_resource_save', {'project_id': pid, 'kind': 'characters', 'entity_id': 'hero', 'data': {'name': 'Hero', 'voice': 'voice'}})
    assert saved['status'] == 'success'
    sid = invoke('series_create', {'project_id': pid, 'title': 'Series', 'source_material': '', 'participant_ids': ['hero'], 'settings': {}})['series_id']
    base = tmp_path / pid / 'series' / sid
    (base / '91_screenplay').mkdir(exist_ok=True)
    (base / '97_shots').mkdir(exist_ok=True)
    script = {'screenplay': [{'scene_number': 1, 'storyboard': [{'shot_number': 1, 'timing': '00:02'}],
                               'dialogue': [{'line': 'Привет', 'speaker_id': 'hero', 'shot_number': 1}]}]}
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(script, ensure_ascii=False))
    return pid, sid, base, script


@pytest.mark.parametrize('tool', ['shots', 'video'])
@pytest.mark.parametrize('state', ['missing', 'stale', 'wrong_silent'])
def test_series_speech_preflight_blocks_before_generation(series, monkeypatch, tool, state):
    pid, sid, base, _ = series
    path = base / '98_audio/speech.json'
    if state != 'missing':
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'status': 'stale' if state == 'stale' else 'silent', 'lines': [], 'words': []}))
    monkeypatch.setattr(video_plan, 'choose_video_method', lambda *_: pytest.fail('selector must not run'))
    monkeypatch.setattr(video_plan.requests, 'post', lambda *_a, **_k: pytest.fail('network must not run'))
    with series_scope(pid, sid), pytest.raises(ValueError, match='[Рр]еч'):
        if tool == 'shots':
            shots.screenplay_shots_generator_tool('s', pid, enable=True)
        else:
            video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': []})
    assert not (base / '97_shots/video_plan.json').exists()
    assert not (base / '97_shots/shots.json').exists()


def test_new_project_root_requires_series(series):
    pid, _, _, _ = series
    with pytest.raises(ValueError, match='сери'):
        video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': []})


def test_skipped_steps_do_not_require_speech(series):
    pid, sid, _, _ = series
    with series_scope(pid, sid):
        assert video_plan.storybook_video_plan_tool('s', pid, enable=False)['status'] == 'skipped'
        assert shots.screenplay_shots_generator_tool('s', pid, enable=False)['items'] == []


def test_silent_series_with_valid_artifact_can_plan_video(series, monkeypatch):
    from custom_tools.storybook import video_generator_common, video_contract
    pid, sid, base, script = series
    script['screenplay'][0]['dialogue'] = []
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(script))
    (base / '98_audio').mkdir(exist_ok=True)
    (base / '98_audio/speech.json').write_text(json.dumps({'status': 'silent', 'lines': [], 'words': []}))
    item = dict(scene_number=1, shot_number=1, shot_type='start', duration_s=2,
                start_s=0, end_s=2, face_visibility=[], video_path=str(base / 'clip.mp4'), video_prompt='Wait')
    monkeypatch.setattr(video_contract, '_active_video_tool_name', lambda: 'video_generator_routerai_tool')
    monkeypatch.setattr(video_generator_common, '_get_routerai_video_models', lambda: {})
    monkeypatch.setattr(video_plan, 'choose_video_method', lambda state: {'method': 'keyframes', 'continuity': 'new'})
    with series_scope(pid, sid):
        result = video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': [item]})
    assert result['status'] == 'success'
    assert json.loads((base / '97_shots/video_plan.json').read_text())['decisions'][0]['method'] == 'keyframes'


def test_missing_speech_keeps_existing_plan_unchanged(series, monkeypatch):
    pid, sid, base, _ = series
    target = base / '97_shots/video_plan.json'
    target.write_text('{"previous": true}')
    monkeypatch.setattr(video_plan, 'choose_video_method', lambda *_: pytest.fail('selector must not run'))
    with series_scope(pid, sid), pytest.raises(ValueError, match='Речь'):
        video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': []})
    assert target.read_text() == '{"previous": true}'


def test_prepared_speech_series_reaches_reference_choice(series, monkeypatch):
    from custom_tools.storybook import video_generator_common, video_contract
    pid, sid, base, _ = series
    (base / '98_audio').mkdir(exist_ok=True)
    (base / '98_audio/speech.wav').write_bytes(b'original')
    (base / '98_audio/speech.json').write_text(json.dumps({'status': 'success', 'audio_path': 'speech.wav', 'duration': 2,
        'lines': [{'id': 'l1', 'scene_number': 1, 'shot_number': 1, 'speaker_id': 'hero', 'kind': 'on_screen', 'start': 0, 'end': 1}], 'words': [{'text': 'Привет', 'start': 0, 'end': 1, 'line_id': 'l1'}]}))
    item = dict(scene_number=1, shot_number=1, shot_type='start', duration_s=2, start_s=0, end_s=2,
                face_visibility=[dict(speaker_id='hero', start_s=0, end_s=2)],
                video_path=str(base / 'clip.mp4'), video_prompt='Speak')
    monkeypatch.setattr(video_contract, '_active_video_tool_name', lambda: 'video_generator_routerai_tool')
    monkeypatch.setattr(video_generator_common, '_get_routerai_video_models', lambda: {})
    monkeypatch.setattr(video_plan, 'choose_video_method', lambda *_: pytest.fail('selector must not run'))
    with series_scope(pid, sid):
        with pytest.raises(ValueError, match='does not support reference'):
            video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': [item]})


@pytest.mark.parametrize('payload', ['{', '{"status":"success","lines":[{"id":"l"}],"words":[],"audio_path":"missing.wav"}'])
def test_corrupt_or_incomplete_speech_is_rejected(series, payload):
    pid, sid, base, _ = series
    path = base / '98_audio/speech.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(payload)
    with series_scope(pid, sid), pytest.raises(ValueError, match='Речь'):
        video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': []})


@pytest.mark.parametrize('tool', ['shots', 'video'])
@pytest.mark.parametrize('payload', [{'status': 'silent'}, {'status': 'silent', 'lines': None, 'words': []},
                                     {'status': 'silent', 'lines': [], 'words': 0}])
def test_incomplete_silent_rejected_before_planning(series, monkeypatch, tool, payload):
    pid, sid, base, script = series
    script['screenplay'][0]['dialogue'] = []
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(script))
    (base / '98_audio').mkdir(exist_ok=True)
    (base / '98_audio/speech.json').write_text(json.dumps(payload))
    monkeypatch.setattr(video_plan, 'choose_video_method', lambda *_: pytest.fail('selector must not run'))
    with series_scope(pid, sid), pytest.raises(ValueError, match='Речь'):
        if tool == 'shots':
            shots.screenplay_shots_generator_tool('s', pid, enable=True)
        else:
            video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': []})


@pytest.mark.parametrize('tool', ['shots', 'video'])
def test_success_with_missing_script_and_bad_items_rejected(series, monkeypatch, tool):
    pid, sid, base, _ = series
    (base / '91_screenplay/screenplay.json').unlink()
    (base / '98_audio').mkdir(exist_ok=True)
    (base / '98_audio/speech.wav').write_bytes(b'fake')
    (base / '98_audio/speech.json').write_text(json.dumps({'status': 'success', 'audio_path': 'speech.wav',
                                                           'lines': ['bad'], 'words': ['bad']}))
    monkeypatch.setattr(video_plan, 'choose_video_method', lambda *_: pytest.fail('selector must not run'))
    with series_scope(pid, sid), pytest.raises(ValueError, match='Речь'):
        if tool == 'shots':
            shots.screenplay_shots_generator_tool('s', pid, enable=True)
        else:
            video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': []})


@pytest.mark.parametrize('field,value', [('lines', ['bad']), ('words', ['bad']),
                                        ('lines', [{'id': 'l1', 'scene_number': 1, 'shot_number': 1,
                                                    'speaker_id': 'hero', 'kind': 'on_screen', 'start': '0', 'end': 1}]),
                                        ('words', [{'text': 'x', 'line_id': 'l1', 'start': 0, 'end': None}])])
def test_success_with_malformed_measured_data_rejected(series, field, value):
    pid, sid, base, _ = series
    (base / '98_audio').mkdir(exist_ok=True)
    (base / '98_audio/speech.wav').write_bytes(b'fake')
    payload = {'status': 'success', 'duration': 2, 'audio_path': 'speech.wav',
               'lines': [{'id': 'l1', 'scene_number': 1, 'shot_number': 1,
                          'speaker_id': 'hero', 'kind': 'on_screen', 'start': 0, 'end': 1}],
               'words': [{'text': 'x', 'line_id': 'l1', 'start': 0, 'end': 1}]}
    payload[field] = value
    (base / '98_audio/speech.json').write_text(json.dumps(payload))
    with series_scope(pid, sid), pytest.raises(ValueError, match='Речь'):
        video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': []})


@pytest.mark.parametrize('payload', ['null', '{}', '{"screenplay": {}}', '{"screenplay": ["bad"]}'])
def test_success_with_malformed_screenplay_rejected(series, payload):
    pid, sid, base, _ = series
    (base / '91_screenplay/screenplay.json').write_text(payload)
    (base / '98_audio').mkdir(exist_ok=True)
    (base / '98_audio/speech.wav').write_bytes(b'fake')
    (base / '98_audio/speech.json').write_text(json.dumps({'status': 'success', 'duration': 1,
        'audio_path': 'speech.wav', 'lines': [{'id': 'l', 'scene_number': 1, 'shot_number': 1,
        'speaker_id': 'hero', 'kind': 'on_screen', 'start': 0, 'end': 1}],
        'words': [{'text': 'x', 'line_id': 'l', 'start': 0, 'end': 1}]}))
    with series_scope(pid, sid), pytest.raises(ValueError, match='Речь'):
        video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': []})


def test_success_with_missing_snapshot_is_rejected(series):
    pid, sid, base, _ = series
    (base / '98_audio').mkdir(exist_ok=True)
    (base / '98_audio/speech.wav').write_bytes(b'fake')
    (base / '98_audio/speech.json').write_text(json.dumps({'status': 'success', 'duration': 1,
        'audio_path': 'speech.wav', 'lines': [{'id': 'l', 'scene_number': 1, 'shot_number': 1,
        'speaker_id': 'hero', 'kind': 'on_screen', 'start': 0, 'end': 1}],
        'words': [{'text': 'x', 'line_id': 'l', 'start': 0, 'end': 1}]}))
    path = base / 'series.json'
    data = json.loads(path.read_text())
    data.pop('snapshot')
    path.write_text(json.dumps(data))
    with series_scope(pid, sid), pytest.raises(ValueError, match='Речь'):
        video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': []})


def test_real_prepared_speech_accepted_in_public_series(series, monkeypatch):
    from custom_tools.storybook import speech, video_generator_common, video_contract
    from custom_tools.storybook.studio.series import resolve_snapshot
    pid, sid, base, script = series
    lines = speech.screenplay_lines(script, resolve_snapshot(pid, sid))
    monkeypatch.setattr(speech, '_synthesize', lambda _: b'original-audio')
    monkeypatch.setattr(speech, '_duration', lambda _: 2.0)
    monkeypatch.setattr(speech, '_transcribe', lambda *_: [{'text': 'Привет', 'start': .2, 'end': 1.2}])
    speech.prepare_speech(base, pid, sid, lines, 'ru')
    item = dict(scene_number=1, shot_number=1, shot_type='start', duration_s=2,
                start_s=0, end_s=2, face_visibility=[], video_path=str(base / 'clip.mp4'), video_prompt='Wait')
    monkeypatch.setattr(video_contract, '_active_video_tool_name', lambda: 'video_generator_routerai_tool')
    monkeypatch.setattr(video_generator_common, '_get_routerai_video_models', lambda: {})
    monkeypatch.setattr(video_plan, 'choose_video_method', lambda state: {'method': 'keyframes', 'continuity': 'new'})
    with series_scope(pid, sid):
        result = video_plan.storybook_video_plan_tool('s', pid, shots_data={'items': [item]})
    assert result['status'] == 'success'


def _dispatch_video_fixture(series, monkeypatch, state, reference):
    import io
    import wave
    from PIL import Image
    from custom_tools.storybook import speech, video_generator_aitunnel_tool as wan, continuity_review
    from custom_tools.storybook.studio.series import resolve_snapshot
    pid, sid, base, script = series
    start, end = base / 'start.png', base / 'end.png'
    Image.new('RGB', (32, 32), 'red').save(start)
    Image.new('RGB', (32, 32), 'blue').save(end)
    item = dict(scene_number=1, shot_number=1, shot_type='start', duration_s=2, timing='00:02',
                start_s=0, end_s=2, face_visibility=[dict(speaker_id='hero', start_s=0, end_s=2)],
                start_image=str(start), end_image=str(end), video_path=str(base / 'clip.mp4'),
                video_prompt='Speak', width=32, height=32)
    (base / '97_shots/shots.json').write_text(json.dumps({'items': [item]}))
    measured = None
    if state == 'success':
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\0\0' * 16000)
        monkeypatch.setattr(speech, '_synthesize', lambda _: output.getvalue())
        monkeypatch.setattr(speech, '_duration', lambda _: 2.)
        monkeypatch.setattr(speech, '_transcribe', lambda *_: [{'text':'Привет', 'start':.2, 'end':1.2}])
        measured = speech.prepare_speech(base, pid, sid, speech.screenplay_lines(script, resolve_snapshot(pid, sid)), 'ru')
    elif state == 'stale':
        (base / '98_audio').mkdir(exist_ok=True)
        (base / '98_audio/speech.json').write_text(json.dumps({'status':'stale'}))
    plan = {'units': [], 'decisions': [{'shot_keys':['1-1'], 'method':'keyframes'}]}
    if measured:
        plan['shot_intervals'] = video_plan._shot_intervals([item], measured)
    if reference:
        schedule = plan['shot_intervals'][0]['speaker_schedule']
        plan['decisions'] = [{'shot_keys':['1-1'], 'method':'references', 'reason':'visible_speech', 'line_ids':[schedule[0]['line_id']]}]
        plan['units'] = [dict(shot_keys=['1-1'], duration_s=2, video_path=str(base/'clip.mp4'), video_prompt='Speak',
            reference_image_paths=[str(start), str(end)], resolution='480p', start_s=0, end_s=2,
            speech_source_path=str(base/'98_audio'/measured['audio_path']), speaker_schedule=schedule)]
    (base/'97_shots/video_plan.json').write_text(json.dumps(plan))
    monkeypatch.setenv('AITUNNEL_API_KEY', 'fake')
    monkeypatch.setenv('AITUNNEL_VIDEO_MODEL', 'fake-model')
    monkeypatch.setattr(continuity_review, 'require_continuity_acceptance', lambda *_: None)
    monkeypatch.setattr(wan, '_get_aitunnel_video_models', lambda: {'fake-model': {'supports_image_references': True,
        'supported_durations':[2], 'max_image_references':10}})
    monkeypatch.setattr(wan, '_resolve_model_and_size', lambda **_: ('fake-model', {'resolution':'480p', 'aspect_ratio':'1:1'}, 2))
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs['json'])
        raise RuntimeError('intercepted request')
    monkeypatch.setattr(wan.requests, 'post', post)
    return item, measured, calls


@pytest.mark.parametrize('state', ['missing', 'stale', 'success'])
def test_dispatch_blocks_unready_or_stale_keyframes_before_post(series, monkeypatch, state):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    pid, sid, _, _ = series
    _, _, calls = _dispatch_video_fixture(series, monkeypatch, state, False)
    from custom_tools.storybook import video_generator_aitunnel_tool as wan
    monkeypatch.setattr(wan, '_get_aitunnel_video_models', lambda: pytest.fail('catalog must not run'))
    with series_scope(pid, sid):
        try:
            video_generator_dispatch_tool('s', pid, enable=True, video_provider='aitunnel',
                                          skip_prompt_enhancement=True, retry_cycles=0)
        except ValueError:
            pass
    assert calls == []


def test_dispatch_current_speech_reference_reaches_intercepted_post(series, monkeypatch):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    pid, sid, _, _ = series
    _, _, calls = _dispatch_video_fixture(series, monkeypatch, 'success', True)
    with series_scope(pid, sid):
        video_generator_dispatch_tool('s', pid, enable=True, video_provider='aitunnel',
                                      skip_prompt_enhancement=True, retry_cycles=0)
    assert len(calls) == 1
    assert 'frame_images' not in calls[0]
    assert any(ref['type'] == 'audio_url' for ref in calls[0]['input_references'])


@pytest.mark.parametrize('change', ['speaker', 'timing', 'audio', 'schedule', 'visibility'])
def test_dispatch_rejects_reference_plan_not_matching_current_speech(series, monkeypatch, change):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    pid, sid, base, _ = series
    _, _, calls = _dispatch_video_fixture(series, monkeypatch, 'success', True)
    plan_path = base / '97_shots/video_plan.json'
    plan = json.loads(plan_path.read_text())
    if change == 'audio':
        plan['units'][0]['speech_source_path'] = str(base / 'old.wav')
    elif change == 'schedule':
        plan['units'][0]['speaker_schedule'][0]['end'] += .1
    elif change == 'speaker':
        plan['shot_intervals'][0]['speaker_schedule'][0]['speaker_id'] = 'someone-else'
    elif change == 'timing':
        plan['shot_intervals'][0]['end_s'] += .1
    else:
        shots_path = base / '97_shots/shots.json'
        shots = json.loads(shots_path.read_text())
        del shots['items'][0]['face_visibility']
        shots_path.write_text(json.dumps(shots))
    plan_path.write_text(json.dumps(plan))
    with series_scope(pid, sid), pytest.raises(ValueError, match='перепланируйте'):
        video_generator_dispatch_tool('s', pid, enable=True, video_provider='aitunnel',
                                      skip_prompt_enhancement=True, retry_cycles=0)
    assert not calls


def test_dispatch_silent_series_keeps_normal_keyframe_path(series, monkeypatch):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    pid, sid, base, script = series
    _, _, calls = _dispatch_video_fixture(series, monkeypatch, 'missing', False)
    script['screenplay'][0]['dialogue'] = []
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(script))
    (base / '98_audio').mkdir(exist_ok=True)
    (base / '98_audio/speech.json').write_text(json.dumps({'status':'silent', 'lines':[], 'words':[]}))
    with series_scope(pid, sid):
        video_generator_dispatch_tool('s', pid, enable=True, video_provider='aitunnel',
                                      skip_prompt_enhancement=True, retry_cycles=0)
    assert len(calls) == 1
    assert [frame['frame_type'] for frame in calls[0]['frame_images']] == ['first_frame', 'last_frame']


@pytest.mark.parametrize('state', ['missing', 'stale', 'success'])
def test_direct_aitunnel_executor_blocks_bad_speech_plan(series, monkeypatch, state):
    from custom_tools.storybook.video_generator_aitunnel_tool import video_generator_aitunnel_tool
    pid, sid, _, _ = series
    _, _, calls = _dispatch_video_fixture(series, monkeypatch, state, False)
    from custom_tools.storybook import video_generator_aitunnel_tool as wan
    monkeypatch.setattr(wan, '_get_aitunnel_video_models', lambda: pytest.fail('catalog must not run'))
    with series_scope(pid, sid), pytest.raises(ValueError):
        video_generator_aitunnel_tool('s', pid, enable=True, skip_prompt_enhancement=True, retry_cycles=0)
    assert not calls


def test_dispatch_other_provider_blocks_missing_speech_before_loading_provider(series, monkeypatch):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    pid, sid, _, _ = series
    _, _, calls = _dispatch_video_fixture(series, monkeypatch, 'missing', False)
    with series_scope(pid, sid), pytest.raises(ValueError, match='Речь'):
        video_generator_dispatch_tool('s', pid, enable=True, video_provider='minimax')
    assert not calls


@pytest.mark.parametrize('leftover', ['both', 'audio_only', 'schedule_only'])
def test_dispatch_rejects_speech_plan_after_series_becomes_silent(series, monkeypatch, leftover):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    from custom_tools.storybook import video_generator_aitunnel_tool as wan
    pid, sid, base, script = series
    _, _, calls = _dispatch_video_fixture(series, monkeypatch, 'success', True)
    plan_path = base / '97_shots/video_plan.json'
    plan = json.loads(plan_path.read_text())
    if leftover == 'audio_only':
        plan['units'][0].pop('speaker_schedule')
    elif leftover == 'schedule_only':
        plan['units'][0].pop('speech_source_path')
    plan_path.write_text(json.dumps(plan))
    script['screenplay'][0]['dialogue'] = []
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(script))
    (base / '98_audio/speech.json').write_text(json.dumps({'status': 'silent', 'lines': [], 'words': []}))
    monkeypatch.setattr(wan, '_get_aitunnel_video_models', lambda: pytest.fail('catalog must not run'))
    with series_scope(pid, sid), pytest.raises(ValueError, match='перепланируйте'):
        video_generator_dispatch_tool('s', pid, enable=True, video_provider='aitunnel',
                                      skip_prompt_enhancement=True, retry_cycles=0)
    assert calls == []


@pytest.mark.parametrize('change', ['off_screen', 'no_visible_face'])
def test_dispatch_rejects_old_speech_unit_when_speaker_is_not_visible(series, monkeypatch, change):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    from custom_tools.storybook import video_generator_aitunnel_tool as wan
    pid, sid, base, script = series
    item, measured, calls = _dispatch_video_fixture(series, monkeypatch, 'success', True)
    if change == 'off_screen':
        script['screenplay'][0]['dialogue'][0]['kind'] = 'off_screen'
        (base / '91_screenplay/screenplay.json').write_text(json.dumps(script))
        measured['lines'][0]['kind'] = 'off_screen'
        (base / '98_audio/speech.json').write_text(json.dumps(measured))
        plan_path = base / '97_shots/video_plan.json'
        plan = json.loads(plan_path.read_text())
        plan['shot_intervals'] = video_plan._shot_intervals([item], measured)
        plan_path.write_text(json.dumps(plan))
    else:
        item['face_visibility'] = []
        (base / '97_shots/shots.json').write_text(json.dumps({'items': [item]}))
    monkeypatch.setattr(wan, '_get_aitunnel_video_models', lambda: pytest.fail('catalog must not run'))
    with series_scope(pid, sid), pytest.raises(ValueError, match='перепланируйте'):
        video_generator_dispatch_tool('s', pid, enable=True, video_provider='aitunnel',
                                      skip_prompt_enhancement=True, retry_cycles=0)
    assert calls == []


def test_dispatch_silent_series_keeps_mute_reference_action(series, monkeypatch):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    pid, sid, base, script = series
    _, _, calls = _dispatch_video_fixture(series, monkeypatch, 'success', True)
    script['screenplay'][0]['dialogue'] = []
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(script))
    (base / '98_audio/speech.json').write_text(json.dumps({'status': 'silent', 'lines': [], 'words': []}))
    plan_path = base / '97_shots/video_plan.json'
    plan = json.loads(plan_path.read_text())
    plan['units'][0].pop('speech_source_path')
    plan['units'][0].pop('speaker_schedule')
    plan_path.write_text(json.dumps(plan))
    with series_scope(pid, sid):
        video_generator_dispatch_tool('s', pid, enable=True, video_provider='aitunnel',
                                      skip_prompt_enhancement=True, retry_cycles=0)
    assert len(calls) == 1
    assert all(ref['type'] != 'audio_url' for ref in calls[0]['input_references'])


def test_dispatch_rejects_speech_audio_override_in_current_shot(series, monkeypatch):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    from custom_tools.storybook import video_generator_aitunnel_tool as wan
    pid, sid, base, _ = series
    item, _, calls = _dispatch_video_fixture(series, monkeypatch, 'success', True)
    item['speech_audio_path'] = str(base / 'old.wav')
    (base / '97_shots/shots.json').write_text(json.dumps({'items': [item]}))
    monkeypatch.setattr(wan, '_get_aitunnel_video_models', lambda: pytest.fail('catalog must not run'))
    with series_scope(pid, sid), pytest.raises(ValueError, match='перепланируйте'):
        video_generator_dispatch_tool('s', pid, enable=True, video_provider='aitunnel',
                                      skip_prompt_enhancement=True, retry_cycles=0)
    assert not calls


def test_dispatch_offscreen_speech_allows_ordinary_keyframe_clip(series, monkeypatch):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    pid, sid, base, script = series
    item, measured, calls = _dispatch_video_fixture(series, monkeypatch, 'success', False)
    script['screenplay'][0]['dialogue'][0]['kind'] = 'off_screen'
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(script))
    measured['lines'][0]['kind'] = 'off_screen'
    (base / '98_audio/speech.json').write_text(json.dumps(measured))
    plan_path = base / '97_shots/video_plan.json'
    plan = json.loads(plan_path.read_text())
    plan['shot_intervals'] = video_plan._shot_intervals([item], measured)
    plan_path.write_text(json.dumps(plan))
    with series_scope(pid, sid):
        video_generator_dispatch_tool('s', pid, enable=True, video_provider='aitunnel',
                                      skip_prompt_enhancement=True, retry_cycles=0)
    assert len(calls) == 1
    assert [frame['frame_type'] for frame in calls[0]['frame_images']] == ['first_frame', 'last_frame']


def test_dispatch_narration_allows_ordinary_keyframe_clip(series, monkeypatch):
    from custom_tools.storybook.video_generator_dispatch_tool import video_generator_dispatch_tool
    pid, sid, base, script = series
    item, measured, calls = _dispatch_video_fixture(series, monkeypatch, 'success', False)
    saved = invoke('shared_resource_save', {'project_id': pid, 'kind': 'narrators', 'entity_id': 'reader',
                                             'data': {'name': 'Reader', 'voice': 'voice'}})
    snapshot_path = base / 'series.json'
    data = json.loads(snapshot_path.read_text())
    data['snapshot']['narrators'] = {'reader': saved['version_id']}
    snapshot_path.write_text(json.dumps(data))
    script['screenplay'][0]['dialogue'] = []
    script['screenplay'][0]['voiceover'] = 'Привет'
    (base / '91_screenplay/screenplay.json').write_text(json.dumps(script))
    measured['lines'][0].update(kind='narrator', speaker_id='reader')
    (base / '98_audio/speech.json').write_text(json.dumps(measured))
    plan_path = base / '97_shots/video_plan.json'
    plan = json.loads(plan_path.read_text())
    plan['shot_intervals'] = video_plan._shot_intervals([item], measured)
    plan_path.write_text(json.dumps(plan))
    with series_scope(pid, sid):
        video_generator_dispatch_tool('s', pid, enable=True, video_provider='aitunnel',
                                      skip_prompt_enhancement=True, retry_cycles=0)
    assert len(calls) == 1
    assert [frame['frame_type'] for frame in calls[0]['frame_images']] == ['first_frame', 'last_frame']
