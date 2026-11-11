import asyncio
import json
from types import SimpleNamespace

import pytest

from custom_tools.storybook.project_paths import series_scope
from custom_tools.storybook.studio.capabilities import invoke
from tests.test_storybook_series_core import call


def create(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    sid = call('series_create', project_id=pid, title='One', source_material='Story', participant_ids=[], settings={})['series_id']
    root = tmp_path / pid / 'series' / sid
    (root / '98_audio').mkdir(exist_ok=True)
    (root / '98_audio/speech.json').write_text(json.dumps({'status': 'success', 'lines': []}))
    return pid, sid, root


def test_shot_step_rejects_stale_speech(tmp_path, monkeypatch):
    from custom_tools.storybook.screenplay_shots_generator import screenplay_shots_generator_tool
    pid, sid, root = create(tmp_path, monkeypatch)
    script = root / '91_screenplay/screenplay.json'
    script.parent.mkdir(exist_ok=True)
    script.write_text(json.dumps({'screenplay': [{'dialogue': [{'id': 'l1', 'line': 'old'}]}]}))
    call('speech_line_update', project_id=pid, series_id=sid, line_id='l1', text='new', delivery='', pause_before=0, pause_after=0)
    with series_scope(pid, sid), pytest.raises(ValueError, match='реч'):
        screenplay_shots_generator_tool('s', pid)


def test_video_plan_rejects_stale_speech(tmp_path, monkeypatch):
    from custom_tools.storybook.video_plan import storybook_video_plan_tool
    pid, sid, root = create(tmp_path, monkeypatch)
    script = root / '91_screenplay/screenplay.json'
    script.parent.mkdir(exist_ok=True)
    script.write_text(json.dumps({'screenplay': [{'dialogue': [{'id': 'l1', 'line': 'old'}]}]}))
    call('speech_line_update', project_id=pid, series_id=sid, line_id='l1', text='new', delivery='', pause_before=0, pause_after=0)
    with series_scope(pid, sid), pytest.raises(ValueError, match='реч'):
        storybook_video_plan_tool('s', pid, shots_data={'items': []})


def test_tool_executor_carries_both_series(tmp_path, monkeypatch):
    from workflow.engine import WorkflowEngine
    from workflow.models import WorkflowStep, WorkflowContext
    from custom_tools.storybook.project_paths import current_series_scope
    import tool_manager
    pid, sid, root = create(tmp_path, monkeypatch)
    second = call('series_create', project_id=pid, title='Two', source_material='', participant_ids=[], settings={})['series_id']
    engine = object.__new__(WorkflowEngine)
    engine.factory = SimpleNamespace(_create_tool=lambda name: current_series_scope)
    engine.resource_manager = SimpleNamespace(record_api_call=lambda wid: None)
    monkeypatch.setattr(tool_manager, 'get_tool_manager', lambda: SimpleNamespace(run_tool=lambda **kw: kw['tool_function']()))
    step = WorkflowStep(id='scope_test', tool_name='scope_test', task='test')
    async def execute(series_id):
        with series_scope(pid, series_id):
            return await engine._execute_tool_step(step, WorkflowContext(workflow_id=series_id, session_id='s', variables={}), 'test')
    async def both():
        return await asyncio.gather(execute(sid), execute(second))
    assert asyncio.run(both()) == [(pid, sid), (pid, second)]


def test_material_change_marks_existing_outputs_in_only_one_series(tmp_path, monkeypatch):
    pid, sid, root = create(tmp_path, monkeypatch)
    other = call('series_create', project_id=pid, title='Two', source_material='Story', participant_ids=[], settings={})['series_id']
    for rel in ['10_synopsis/synopsis.json', '20_story/story.json', '99_final/final_video.mp4']:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('old')
    result = call('series_update', project_id=pid, series_id=sid, title='One', source_material='Changed', settings={})
    assert '10_synopsis' in result['series']['stale_results']
    assert '99_final' in result['series']['stale_results']
    assert not call('series_get', project_id=pid, series_id=other)['series'].get('stale_results')


def test_stale_plan_and_successful_step_preserve_old_output(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    (root / '10_synopsis').mkdir(exist_ok=True)
    (root / '10_synopsis/synopsis.json').write_text('{"old":true}')
    (root / '10_synopsis/beats.json').write_text('[]')
    data = json.loads((root / 'series.json').read_text())
    data['stale_results'] = ['10_synopsis']
    (root / 'series.json').write_text(json.dumps(data))
    def generate(step, ctx):
        (root / '10_synopsis/synopsis.json').write_text('{"new":true}')
        (root / '10_synopsis/beats.json').write_text('[]')
    engine = FakeEngine(tmp_path, on_step=generate)
    plan = make_plan(pid, ['story_planner'], mode='single_step', series_id=sid)
    with series_scope(pid, sid):
        from custom_tools.storybook.studio.run_plan import stale_results_for_plan
        preview = stale_results_for_plan(pid, ['story_planner'])
    assert '10_synopsis' in preview['affected_results']
    plan.update(preview)
    result = asyncio.run(run_exec.execute_plan(engine, plan, run_id='s13'))
    assert result['status'] == 'success'
    assert not json.loads((root / 'series.json').read_text())['stale_results']
    assert list((root / '.studio/artifact_history/synopsis').glob('*.json'))


def test_shot_intervals_include_narrator_and_offscreen():
    from custom_tools.storybook.video_plan import _shot_intervals
    shots = [{'scene_number': 1, 'shot_number': 1, 'shot_type': 'start', 'start_s': 2., 'end_s': 5.}]
    speech = {'lines': [{'id': 'a', 'speaker_id': 'narrator', 'kind': 'narrator', 'start': 1., 'end': 3.},
                        {'id': 'b', 'speaker_id': 'fox', 'kind': 'off_screen', 'start': 4., 'end': 6.},
                        {'id': 'c', 'speaker_id': 'owl', 'kind': 'on_screen', 'start': 6., 'end': 7.}]}
    result = _shot_intervals(shots, speech)
    assert len(result) == 1 and result[0]['start_s'] == 2 and result[0]['end_s'] == 5
    assert [line['line_id'] for line in result[0]['speaker_schedule']] == ['a', 'b']
    assert result[0]['speaker_schedule'][0]['start'] == 0 and result[0]['speaker_schedule'][0]['end'] == 1


@pytest.mark.parametrize('outcome', ['failed', 'cancelled'])
def test_failed_or_cancelled_rerun_keeps_stale_and_archived_result(tmp_path, monkeypatch, outcome):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    (root / '10_synopsis').mkdir(exist_ok=True)
    (root / '10_synopsis/synopsis.json').write_text('{"old":true}')
    (root / '10_synopsis/beats.json').write_text('[]')
    data = json.loads((root / 'series.json').read_text())
    data['stale_results'] = ['10_synopsis']
    (root / 'series.json').write_text(json.dumps(data))
    engine = FakeEngine(tmp_path, fail_steps={'story_planner'} if outcome == 'failed' else ())
    plan = make_plan(pid, ['story_planner'], mode='single_step', series_id=sid)
    if outcome == 'cancelled':
        engine.cancelled.add(plan['workflow_id'])
    with series_scope(pid, sid):
        plan.update(stale_results_for_plan(pid, plan['steps_to_run']))
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='s13-failed'))
    assert '10_synopsis' in json.loads((root / 'series.json').read_text())['stale_results']
    if outcome == 'failed':
        assert list((root / '.studio/artifact_history/synopsis').glob('*.json'))


def test_late_completion_cannot_clear_newer_revision(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    (root / '10_synopsis').mkdir(exist_ok=True)
    for name in ('synopsis.json', 'beats.json'):
        (root / '10_synopsis' / name).write_text('{}')
    data = json.loads((root / 'series.json').read_text())
    data['stale_results'] = ['10_synopsis']
    (root / 'series.json').write_text(json.dumps(data))
    def generate(step, ctx):
        for name in ('synopsis.json', 'beats.json'):
            (root / '10_synopsis' / name).write_text('{}')
        data = json.loads((root / 'series.json').read_text())
        data['input_revision'] = 1
        (root / 'series.json').write_text(json.dumps(data))
    engine = FakeEngine(tmp_path, on_step=generate)
    plan = make_plan(pid, ['story_planner'], mode='single_step', series_id=sid)
    with series_scope(pid, sid):
        plan.update(stale_results_for_plan(pid, plan['steps_to_run']))
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='late'))
    assert '10_synopsis' in json.loads((root / 'series.json').read_text())['stale_results']


def test_line_edit_marks_speech_video_and_final_stale(tmp_path, monkeypatch):
    pid, sid, root = create(tmp_path, monkeypatch)
    script = root / '91_screenplay/screenplay.json'
    script.parent.mkdir(exist_ok=True)
    script.write_text(json.dumps({'screenplay': [{'dialogue': [{'id': 'l1', 'line': 'old'}]}]}))
    (root / '97_shots').mkdir(exist_ok=True)
    (root / '97_shots/shots.json').write_text('{}')
    (root / '99_final').mkdir(exist_ok=True)
    (root / '99_final/final_video.mp4').write_bytes(b'video')
    call('speech_line_update', project_id=pid, series_id=sid, line_id='l1', text='new', delivery='', pause_before=0, pause_after=0)
    series = json.loads((root / 'series.json').read_text())
    assert set(series['stale_results']) >= {'98_audio/speech.json', '97_shots', '99_final'}
    assert series['input_revision'] == 1


def test_sound_edit_marks_only_sound_preview_and_final(tmp_path, monkeypatch):
    pid, sid, root = create(tmp_path, monkeypatch)
    speech = root / '98_audio/speech.json'
    original = speech.read_bytes()
    (root / '98_audio/audio_manifest.json').write_text(json.dumps({'audio_tracks': [{'role': 'sfx', 'id': 'rain', 'duration_seconds': 1}]}))
    (root / '98_audio/previews').mkdir(exist_ok=True)
    (root / '99_final').mkdir(exist_ok=True)
    call('sound_event_update', project_id=pid, series_id=sid, event_id='rain', start=0, level=.2, enabled=True)
    data = json.loads((root / 'series.json').read_text())
    assert set(data['stale_results']) == {'98_audio/previews', '99_final'}
    assert speech.read_bytes() == original


def test_video_plan_artifact_is_read_only(tmp_path, monkeypatch):
    pid, sid, root = create(tmp_path, monkeypatch)
    (root / '97_shots').mkdir(exist_ok=True)
    (root / '97_shots/video_plan.json').write_text(json.dumps({'shot_intervals': []}))
    result = call('artifact_read', project_id=pid, series_id=sid, key='video_plan')
    assert result['content']['shot_intervals'] == [] and result['editable'] is False
    result = invoke('artifact_save', {'project_id': pid, 'series_id': sid, 'key': 'video_plan', 'base_digest': None, 'content': {}})
    assert result['status'] == 'error' and result['error']['code'] == 'unsupported'


def test_voice_update_marks_speech_stale_but_visual_update_keeps_it(tmp_path, monkeypatch):
    pid, sid, root = create(tmp_path, monkeypatch)
    first = call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox', 'voice': 'v1'})['version_id']
    call('series_update', project_id=pid, series_id=sid, title='One', source_material='Story', settings={}, participant_ids=['fox'])
    speech = root / '98_audio/speech.json'
    speech.write_text(json.dumps({'status': 'success', 'lines': []}))
    visual = call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox', 'voice': 'v1', 'appearance': 'red'})['version_id']
    call('series_resource_update_apply', project_id=pid, series_id=sid, kind='characters', entity_id='fox', version_id=visual)
    assert json.loads(speech.read_text())['status'] == 'success'
    voice = call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox', 'voice': 'v2', 'appearance': 'red'})['version_id']
    call('series_resource_update_apply', project_id=pid, series_id=sid, kind='characters', entity_id='fox', version_id=voice)
    assert json.loads(speech.read_text())['status'] == 'stale'


def test_from_step_rebuilds_only_stale_selected_output(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    story = root / '20_story/story.json'
    story.parent.mkdir(exist_ok=True)
    story.write_text('{"old":true}')
    unrelated = root / '10_synopsis/synopsis.json'
    unrelated.parent.mkdir(exist_ok=True)
    unrelated.write_text('{"unchanged":true}')
    data = json.loads((root / 'series.json').read_text())
    data['stale_results'] = ['20_story']
    (root / 'series.json').write_text(json.dumps(data))
    def generate(step, context):
        if step.id == 'story_writer':
            assert not story.exists()
            story.write_text('{"new":true}')
    engine = FakeEngine(tmp_path, on_step=generate)
    plan = make_plan(pid, ['story_writer', 'story_editor'], mode='from_step', series_id=sid)
    with series_scope(pid, sid):
        plan.update(stale_results_for_plan(pid, plan['steps_to_run']))
    result = asyncio.run(run_exec.execute_plan(engine, plan, run_id='from-step'))
    assert result['status'] == 'success' and unrelated.read_text() == '{"unchanged":true}'
    assert story.read_text() == '{"new":true}'
    assert list((root / '.studio/artifact_history/story').glob('*.json'))
    assert not json.loads((root / 'series.json').read_text())['stale_results']


def test_stale_bible_keeps_shared_and_archives_previous_local_characters(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox'})
    call('series_update', project_id=pid, series_id=sid, title='One', source_material='Story', settings={}, participant_ids=['fox'])
    bible = root / '20_bible'
    bible.mkdir(exist_ok=True)
    (bible / 'characters.json').write_text(json.dumps([{'name': 'Fox', '_shared_id': 'fox'}, {'name': 'Old visitor'}]))
    (bible / 'locations.json').write_text('[]')
    data = json.loads((root / 'series.json').read_text())
    data['stale_results'] = ['20_bible']
    (root / 'series.json').write_text(json.dumps(data))
    def generate(step, ctx):
        characters = json.loads((bible / 'characters.json').read_text())
        assert [c['name'] for c in characters] == ['Fox']
        characters.append({'name': 'New visitor'})
        (bible / 'characters.json').write_text(json.dumps(characters))
        (bible / 'locations.json').write_text('[]')
        (bible / 'consistency_rules.json').write_text('[]')
    engine = FakeEngine(tmp_path, on_step=generate)
    plan = make_plan(pid, ['bible_builder'], mode='single_step', series_id=sid)
    with series_scope(pid, sid):
        plan.update(stale_results_for_plan(pid, plan['steps_to_run']))
    result = asyncio.run(run_exec.execute_plan(engine, plan, run_id='bible'))
    assert result['status'] == 'success', result
    assert list((root / '.studio/artifact_history/characters').glob('*.json'))


def test_late_speech_result_remains_stale_after_input_revision_changes(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    series_path = root / 'series.json'
    data = json.loads(series_path.read_text())
    data['stale_results'] = ['98_audio/speech.json']
    series_path.write_text(json.dumps(data))
    speech = root / '98_audio/speech.json'
    def generate(step, ctx):
        data = json.loads(series_path.read_text())
        data['input_revision'] = 1
        series_path.write_text(json.dumps(data))
        speech.write_text(json.dumps({'status': 'success', 'lines': []}))
    engine = FakeEngine(tmp_path, on_step=generate)
    plan = make_plan(pid, ['storybook_speech'], mode='single_step', series_id=sid)
    with series_scope(pid, sid):
        plan.update(stale_results_for_plan(pid, plan['steps_to_run']))
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='late-speech'))
    assert json.loads(speech.read_text())['status'] == 'stale'
    assert '98_audio/speech.json' in json.loads(series_path.read_text())['stale_results']


def test_stale_style_reuses_pinned_project_style(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    style = call('shared_resource_save', project_id=pid, kind='style', entity_id='main', data={'style_text': {'voice': 'ink'}, 'style_images': {'style': 'ink'}})['version_id']
    data = json.loads((root / 'series.json').read_text())
    data['snapshot']['style'] = {'main': style}
    data['stale_results'] = ['30_style']
    (root / 'series.json').write_text(json.dumps(data))
    def check(step, context):
        assert json.loads((root / '30_style/style_images.json').read_text()) == {'style': 'ink'}
    engine = FakeEngine(tmp_path, on_step=check)
    plan = make_plan(pid, ['style_keeper'], mode='single_step', series_id=sid)
    with series_scope(pid, sid):
        plan.update(stale_results_for_plan(pid, plan['steps_to_run']))
    result = asyncio.run(run_exec.execute_plan(engine, plan, run_id='style'))
    assert result['status'] == 'success', result


def test_visual_settings_change_preserves_speech(tmp_path, monkeypatch):
    pid, sid, root = create(tmp_path, monkeypatch)
    speech = root / '98_audio/speech.json'
    speech.write_text(json.dumps({'status': 'success', 'audio_path': 'voice.wav'}))
    original = speech.read_bytes()
    result = call('series_update', project_id=pid, series_id=sid, title='One', source_material='Story', settings={'style_preset': 'ink'})
    assert speech.read_bytes() == original
    assert '98_audio' not in result['series']['stale_results']


def test_speech_in_screenplay_stage_before_shots():
    from custom_tools.storybook.studio.status import STAGES
    stages = {key: (title, steps) for key, title, steps in STAGES}
    title, steps = stages['screenplay']
    assert title == 'Сценарий, речь и кадры'
    assert steps.index('storybook_speech') < steps.index('screenplay_shots_generator')
    assert stages['sound'][0] == 'Музыка и субтитры'


def test_sound_edit_during_speech_does_not_stale_new_speech(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    series_path = root / 'series.json'
    data = json.loads(series_path.read_text())
    data['stale_results'] = ['98_audio/speech.json']
    series_path.write_text(json.dumps(data))
    speech = root / '98_audio/speech.json'
    (root / '98_audio/audio_manifest.json').write_text(json.dumps({'audio_tracks': [{'role': 'sfx', 'id': 'rain', 'duration_seconds': 1}]}))
    def generate(step, ctx):
        call('sound_event_update', project_id=pid, series_id=sid, event_id='rain', start=0, level=.2, enabled=True)
        speech.write_text(json.dumps({'status': 'success', 'lines': []}))
    engine = FakeEngine(tmp_path, on_step=generate)
    plan = make_plan(pid, ['storybook_speech'], mode='single_step', series_id=sid)
    with series_scope(pid, sid):
        plan.update(stale_results_for_plan(pid, plan['steps_to_run']))
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='late-sound'))
    assert json.loads(speech.read_text())['status'] == 'success'
    assert '98_audio/speech.json' not in json.loads(series_path.read_text())['stale_results']


def test_empty_series_has_neutral_speech_and_sound_state(tmp_path, monkeypatch):
    pid, sid, root = create(tmp_path, monkeypatch)
    (root / '98_audio/speech.json').unlink()
    speech = call('speech_get', project_id=pid, series_id=sid)
    assert speech['screenplay'] == {'screenplay': []} and speech['speech'] is None and speech['lines'] == []
    assert call('sound_events_get', project_id=pid, series_id=sid)['events'] == []
    script = root / '91_screenplay/screenplay.json'
    script.parent.mkdir(exist_ok=True)
    script.write_text('{bad')
    assert invoke('speech_get', {'project_id': pid, 'series_id': sid})['status'] == 'error'


def test_disabled_and_conditional_producers_do_not_hold_markers(tmp_path, monkeypatch):
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    data = json.loads((root / 'series.json').read_text())
    data['stale_results'] = ['20_story', '98_audio', '98_audio/previews', '97_shots']
    (root / 'series.json').write_text(json.dumps(data))
    for rel in ('20_story/story.json', '98_audio/sfx_manifest.json', '97_shots/shots.json'):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{}')
    with series_scope(pid, sid):
        plan = stale_results_for_plan(pid, ['story_writer', 'storybook_sfx_generator', 'shots_prompt_qa'],
                                      inputs={'generate_screenplay': True, 'generate_music': False, 'generate_sfx': True})
    assert 'story_editor' not in plan['stale_requirements']['20_story']
    assert 'storybook_music_generator' not in plan['stale_requirements']['98_audio']
    assert 'storybook_sfx_generator' in plan['stale_requirements']['98_audio']
    assert 'shots_prompt_qa' in plan['stale_requirements']['97_shots']


def test_selected_book_images_are_archived_before_rerender(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    images = root / '50_images/page_01'
    images.mkdir(parents=True)
    image = images / 'img_final.png'
    image.write_bytes(b'old')
    neighbour = images / 'untouched.png'
    neighbour.write_bytes(b'keep')
    (root / '50_items').mkdir(exist_ok=True)
    (root / '50_items/items.json').write_text(json.dumps({'items': [{'output_path': str(image)}]}))
    data = json.loads((root / 'series.json').read_text()); data['stale_results'] = ['50_images']
    (root / 'series.json').write_text(json.dumps(data))
    def generate(step, ctx):
        assert not image.exists()
        image.write_bytes(b'new')
    engine = FakeEngine(tmp_path, on_step=generate)
    plan = make_plan(pid, ['artist_batch'], mode='single_step', series_id=sid,
                     stale_steps={'artist_batch': ['50_images']}, affected_results=['50_images'],
                     stale_requirements={'50_images': ['artist_batch']})
    result = asyncio.run(run_exec.execute_plan(engine, plan, run_id='images'))
    assert result['status'] == 'success', result
    assert image.read_bytes() == b'new' and neighbour.read_bytes() == b'keep'
    assert any(p.read_bytes() == b'old' for p in (root / '.studio/image_edit_backups').glob('*'))


def test_montage_cannot_clear_final_while_shots_are_stale(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    final = root / '99_final/final_video.mp4'
    final.parent.mkdir(exist_ok=True); final.write_bytes(b'old')
    data = json.loads((root / 'series.json').read_text()); data['stale_results'] = ['97_shots', '99_final']
    (root / 'series.json').write_text(json.dumps(data))
    engine = FakeEngine(tmp_path, on_step=lambda step, ctx: final.write_bytes(b'new'))
    plan = make_plan(pid, ['montage_assembler'], mode='single_step', series_id=sid,
                     stale_steps={'montage_assembler': ['99_final']}, affected_results=['99_final'],
                     stale_requirements={'99_final': ['montage_assembler']})
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='montage'))
    assert '99_final' in json.loads((root / 'series.json').read_text())['stale_results']


def test_single_steps_accumulate_confirmation_until_all_done(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    story = root / '20_story/story.json'
    story.parent.mkdir(exist_ok=True); story.write_text('{}')
    data = json.loads((root / 'series.json').read_text()); data['stale_results'] = ['20_story']
    (root / 'series.json').write_text(json.dumps(data))
    requirements = {'20_story': ['story_writer', 'story_editor']}
    for index, step in enumerate(('story_writer', 'story_editor')):
        engine = FakeEngine(tmp_path, on_step=lambda _, ctx: story.write_text('{"new":true}'))
        plan = make_plan(pid, [step], mode='single_step', series_id=sid, stale_steps={step: ['20_story']},
                         affected_results=['20_story'], stale_requirements=requirements)
        result = asyncio.run(run_exec.execute_plan(engine, plan, run_id=f'part-{index}'))
        assert result['status'] == 'success'
        stale = json.loads((root / 'series.json').read_text())['stale_results']
        assert ('20_story' in stale) == (index == 0)


def test_sound_preview_clears_only_its_marker_on_success(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import speech as studio_speech
    pid, sid, root = create(tmp_path, monkeypatch)
    audio = root / '98_audio'
    (audio / 'effect.wav').write_bytes(b'effect')
    (audio / 'speech.json').write_text(json.dumps({'status': 'silent', 'lines': []}))
    (audio / 'audio_manifest.json').write_text(json.dumps({'audio_tracks': [{'id': 'rain', 'role': 'sfx', 'path': 'effect.wav', 'duration_seconds': 1}]}))
    data = json.loads((root / 'series.json').read_text()); data['stale_results'] = ['98_audio/previews', '99_final']
    (root / 'series.json').write_text(json.dumps(data))
    monkeypatch.setattr(studio_speech.subprocess, 'run', lambda command, **kwargs: __import__('pathlib').Path(command[-1]).write_bytes(b'preview'))
    call('sound_preview', project_id=pid, series_id=sid, event_id='rain')
    assert json.loads((root / 'series.json').read_text())['stale_results'] == ['99_final']


def test_quality_check_is_confirmed_without_removing_its_input(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    shot = root / '97_shots/shots.json'; shot.parent.mkdir(exist_ok=True); shot.write_text('{"items": []}')
    data = json.loads((root / 'series.json').read_text()); data['stale_results'] = ['97_shots/shots.json']
    (root / 'series.json').write_text(json.dumps(data))
    def qa(step, ctx):
        assert shot.exists()
        shot.write_text('{"items": [], "checked": true}')
    engine = FakeEngine(tmp_path, on_step=qa)
    plan = make_plan(pid, ['shots_prompt_qa'], mode='single_step', series_id=sid,
                     stale_steps={'shots_prompt_qa': ['97_shots/shots.json']}, affected_results=['97_shots/shots.json'],
                     stale_requirements={'97_shots/shots.json': ['shots_prompt_qa']})
    assert asyncio.run(run_exec.execute_plan(engine, plan, run_id='qa'))['status'] == 'success'
    assert not json.loads((root / 'series.json').read_text())['stale_results']


def test_sfx_step_confirms_manifest_without_deleting_existing_audio(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    manifest = root / '98_audio/sfx_manifest.json'; manifest.write_text('{"old": true}')
    audio = root / '98_audio/audio_manifest.json'; audio.write_text('{"audio_tracks": []}')
    data = json.loads((root / 'series.json').read_text()); data['stale_results'] = ['98_audio/sfx_manifest.json']
    (root / 'series.json').write_text(json.dumps(data))
    def generate(step, ctx):
        assert audio.exists()
        manifest.write_text('{"new":true}')
    engine = FakeEngine(tmp_path, on_step=generate)
    plan = make_plan(pid, ['storybook_sfx_generator'], mode='single_step', series_id=sid,
                     stale_steps={'storybook_sfx_generator': ['98_audio/sfx_manifest.json']},
                     affected_results=['98_audio/sfx_manifest.json'],
                     stale_requirements={'98_audio/sfx_manifest.json': ['storybook_sfx_generator']})
    assert asyncio.run(run_exec.execute_plan(engine, plan, run_id='sfx'))['status'] == 'success'
    assert not json.loads((root / 'series.json').read_text())['stale_results']


def test_actual_selected_shot_steps_accumulate_across_runs(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    shot = root / '97_shots/shots.json'; shot.parent.mkdir(exist_ok=True); shot.write_text('{"items":[]}')
    data = json.loads((root / 'series.json').read_text()); data['stale_results'] = ['97_shots']
    (root / 'series.json').write_text(json.dumps(data))
    steps = ('screenplay_shots_generator', 'shots_prompt_qa', 'artist_batch_shots')
    for index, step in enumerate(steps):
        def write(step_obj, context):
            shot.write_text(json.dumps({'items': [], 'step': step_obj.id}))
        engine = FakeEngine(tmp_path, on_step=write)
        plan = make_plan(pid, [step], mode='single_step', series_id=sid)
        with series_scope(pid, sid):
            plan.update(stale_results_for_plan(pid, [step], inputs={'generate_screenplay': True}))
        result = asyncio.run(run_exec.execute_plan(engine, plan, run_id=f'actual-{index}'))
        assert result['status'] == 'success', result
        stale = json.loads((root / 'series.json').read_text())['stale_results']
        assert ('97_shots' in stale) == (index < len(steps) - 1)


def test_full_rerun_clears_final_only_after_upstream_is_rebuilt(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    story = root / '20_story/story.json'; story.parent.mkdir(exist_ok=True); story.write_text('{}')
    final = root / '99_final/final_video.mp4'; final.parent.mkdir(exist_ok=True); final.write_bytes(b'old')
    data = json.loads((root / 'series.json').read_text()); data['stale_results'] = ['20_story', '99_final']
    (root / 'series.json').write_text(json.dumps(data))
    def generate(step, ctx):
        if step.id == 'story_writer': story.write_text('{"new":true}')
        if step.id == 'montage_assembler': final.write_bytes(b'new')
    engine = FakeEngine(tmp_path, on_step=generate)
    plan = make_plan(pid, ['story_writer', 'montage_assembler'], mode='full', series_id=sid)
    with series_scope(pid, sid):
        plan.update(stale_results_for_plan(pid, plan['steps_to_run'], inputs={'generate_screenplay': True}))
    result = asyncio.run(run_exec.execute_plan(engine, plan, run_id='full'))
    assert result['status'] == 'success', result
    assert not json.loads((root / 'series.json').read_text())['stale_results']


def test_failed_missing_output_remains_required_for_later_run(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    shots = root / '97_shots/shots.json'; shots.parent.mkdir(exist_ok=True); shots.write_text('{}')
    video = root / '97_shots/video_plan.json'; video.write_text('{}')
    data = json.loads((root / 'series.json').read_text()); data['stale_results'] = ['97_shots']
    (root / 'series.json').write_text(json.dumps(data))
    with series_scope(pid, sid):
        preview = stale_results_for_plan(pid, ['screenplay_shots_generator'], inputs={'generate_screenplay': True})
    plan = make_plan(pid, ['screenplay_shots_generator'], mode='single_step', series_id=sid, **preview)
    engine = FakeEngine(tmp_path, fail_steps={'screenplay_shots_generator'})
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='failed-shots'))
    assert not shots.exists()
    with series_scope(pid, sid):
        preview = stale_results_for_plan(pid, ['storybook_video_plan'], inputs={'generate_screenplay': True})
    assert 'screenplay_shots_generator' in preview['stale_requirements']['97_shots']
    plan = make_plan(pid, ['storybook_video_plan'], mode='single_step', series_id=sid, **preview)
    engine = FakeEngine(tmp_path, on_step=lambda step, ctx: video.write_text('{"new":true}'))
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='video-only'))
    assert '97_shots' in json.loads((root / 'series.json').read_text())['stale_results']


def test_retry_confirms_output_missing_after_previous_failed_attempt(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    story = root / '20_story/story.json'; story.parent.mkdir(exist_ok=True); story.write_text('{}')
    series = root / 'series.json'; data = json.loads(series.read_text()); data['stale_results'] = ['20_story']; series.write_text(json.dumps(data))
    for index, failing in enumerate((True, False)):
        engine = FakeEngine(tmp_path, fail_steps={'story_writer'} if failing else (),
                            on_step=None if failing else lambda step, ctx: story.write_text('{"new":true}'))
        plan = make_plan(pid, ['story_writer'], mode='single_step', series_id=sid,
                         stale_steps={'story_writer': ['20_story']}, affected_results=['20_story'],
                         stale_requirements={'20_story': ['story_writer']})
        asyncio.run(run_exec.execute_plan(engine, plan, run_id=f'retry-{index}'))
    assert not json.loads(series.read_text())['stale_results']


def test_edit_after_step_completion_cannot_be_cleared_at_run_end(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    story = root / '20_story/story.json'; story.parent.mkdir(exist_ok=True); story.write_text('{}')
    series = root / 'series.json'; data = json.loads(series.read_text()); data['stale_results'] = ['20_story']; series.write_text(json.dumps(data))
    def generate(step, ctx):
        if step.id == 'story_writer': story.write_text('{"new":true}')
        else:
            data = json.loads(series.read_text()); data['input_revision'] = 1; series.write_text(json.dumps(data))
    engine = FakeEngine(tmp_path, on_step=generate)
    plan = make_plan(pid, ['story_writer', 'story_editor'], mode='from_step', series_id=sid,
                     stale_steps={'story_writer': ['20_story']}, affected_results=['20_story'],
                     stale_requirements={'20_story': ['story_writer']})
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='edit-after-step'))
    assert '20_story' in json.loads(series.read_text())['stale_results']


def test_book_image_retry_confirms_previously_archived_file(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    image = root / '50_images/page_01/img_final.png'; image.parent.mkdir(parents=True); image.write_bytes(b'old')
    (root / '50_items').mkdir(exist_ok=True)
    (root / '50_items/items.json').write_text(json.dumps({'items': [{'output_path': str(image)}]}))
    series = root / 'series.json'; data = json.loads(series.read_text()); data['stale_results'] = ['50_images']; series.write_text(json.dumps(data))
    for index, failing in enumerate((True, False)):
        engine = FakeEngine(tmp_path, fail_steps={'artist_batch'} if failing else (),
                            on_step=None if failing else lambda step, ctx: image.write_bytes(b'new'))
        plan = make_plan(pid, ['artist_batch'], mode='single_step', series_id=sid,
                         stale_steps={'artist_batch': ['50_images']}, affected_results=['50_images'],
                         stale_requirements={'50_images': ['artist_batch']})
        asyncio.run(run_exec.execute_plan(engine, plan, run_id=f'image-retry-{index}'))
    assert image.read_bytes() == b'new'
    assert not json.loads(series.read_text())['stale_results']


def test_final_folder_with_photo_and_film_accumulates_both_producers(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    folder = root / '99_final'; folder.mkdir(exist_ok=True)
    (folder / 'photo_video.mp4').write_bytes(b'old photo')
    (folder / 'final_video.mp4').write_bytes(b'old final')
    data = json.loads((root / 'series.json').read_text()); data['stale_results'] = ['99_final']
    (root / 'series.json').write_text(json.dumps(data))
    for index, step in enumerate(('storybook_photo_montage', 'montage_assembler')):
        def write(step_obj, ctx):
            name = 'photo_video.mp4' if step_obj.id == 'storybook_photo_montage' else 'final_video.mp4'
            (folder / name).write_bytes(b'new')
        engine = FakeEngine(tmp_path, on_step=write)
        plan = make_plan(pid, [step], mode='single_step', series_id=sid)
        with series_scope(pid, sid):
            plan.update(stale_results_for_plan(pid, [step], inputs={'generate_screenplay': True}))
        result = asyncio.run(run_exec.execute_plan(engine, plan, run_id=f'final-{index}'))
        assert result['status'] == 'success', result
        stale = json.loads((root / 'series.json').read_text())['stale_results']
        assert ('99_final' in stale) == (index == 0)


def _s13_story_setup(tmp_path, monkeypatch):
    pid, sid, root = create(tmp_path, monkeypatch)
    synopsis = root / '10_synopsis/synopsis.json'; synopsis.parent.mkdir(exist_ok=True)
    synopsis.write_text('{"text":"old"}')
    (synopsis.parent / 'beats.json').write_text('[]')
    story = root / '20_story/story.json'; story.parent.mkdir(exist_ok=True); story.write_text('{"text":"old"}')
    series = root / 'series.json'; data = json.loads(series.read_text())
    data['stale_results'] = ['10_synopsis', '20_story']; series.write_text(json.dumps(data))
    return pid, sid, root, synopsis, story, series


def _run_s13_step(tmp_path, pid, sid, step, on_step=None, fail=False, cancel=False):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    engine = FakeEngine(tmp_path, on_step=on_step, fail_steps={step} if fail else ())
    plan = make_plan(pid, [step], mode='single_step', series_id=sid)
    with series_scope(pid, sid):
        plan.update(stale_results_for_plan(pid, [step], inputs={'generate_screenplay': True}, explicit_step=step))
    if cancel:
        engine.cancelled.add(plan['workflow_id'])
    return asyncio.run(run_exec.execute_plan(engine, plan, run_id=f'{step}-{__import__("uuid").uuid4().hex[:6]}'))


def test_stale_upstream_at_start_prevents_later_editor_clearing(tmp_path, monkeypatch):
    pid, sid, root, synopsis, story, series = _s13_story_setup(tmp_path, monkeypatch)
    def writer(step, ctx): story.write_text(synopsis.read_text())
    def planner(step, ctx):
        synopsis.write_text('{"text":"new"}')
        (synopsis.parent / 'beats.json').write_text('[]')
    def editor(step, ctx): story.write_text(story.read_text() + ' ')
    _run_s13_step(tmp_path, pid, sid, 'story_writer', writer)
    assert '20_story' in json.loads(series.read_text())['stale_results']
    _run_s13_step(tmp_path, pid, sid, 'story_planner', planner)
    _run_s13_step(tmp_path, pid, sid, 'story_editor', editor)
    assert '20_story' in json.loads(series.read_text())['stale_results']
    _run_s13_step(tmp_path, pid, sid, 'story_writer', writer)
    assert '20_story' in json.loads(series.read_text())['stale_results']
    _run_s13_step(tmp_path, pid, sid, 'story_editor', editor)
    assert not json.loads(series.read_text())['stale_results']
    assert json.loads(story.read_text())['text'] == 'new'


def test_planner_before_writer_confirms_fresh_result(tmp_path, monkeypatch):
    pid, sid, root, synopsis, story, series = _s13_story_setup(tmp_path, monkeypatch)
    def planner(step, ctx):
        synopsis.write_text('{"text":"new"}')
        (synopsis.parent / 'beats.json').write_text('[]')
    _run_s13_step(tmp_path, pid, sid, 'story_planner', planner)
    _run_s13_step(tmp_path, pid, sid, 'story_writer', lambda step, ctx: story.write_text(synopsis.read_text()))
    assert not json.loads(series.read_text())['stale_results']
    assert json.loads(story.read_text())['text'] == 'new'


def test_failed_repeat_revokes_previous_confirmation_but_cancel_before_start_does_not(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    story = root / '20_story/story.json'; story.parent.mkdir(exist_ok=True); story.write_text('{}')
    series = root / 'series.json'; data = json.loads(series.read_text()); data['stale_results'] = ['20_story']; series.write_text(json.dumps(data))
    requirements = {'20_story': ['story_writer', 'story_editor']}
    def run(step, *, fail=False, cancel=False):
        engine = FakeEngine(tmp_path, fail_steps={step} if fail else (), on_step=lambda s, c: story.write_text('{}'))
        plan = make_plan(pid, [step], mode='single_step', series_id=sid, stale_steps={step: ['20_story']}, affected_results=['20_story'], stale_requirements=requirements)
        if cancel: engine.cancelled.add(plan['workflow_id'])
        asyncio.run(run_exec.execute_plan(engine, plan, run_id=__import__('uuid').uuid4().hex))
    run('story_writer')
    run('story_writer', cancel=True)
    assert 'story_writer' in json.loads(series.read_text())['stale_confirmations']['20_story']['steps']
    run('story_writer', fail=True)
    assert 'story_writer' not in json.loads(series.read_text())['stale_confirmations']['20_story']['steps']
    run('story_editor')
    assert '20_story' in json.loads(series.read_text())['stale_results']
    run('story_writer')
    assert '20_story' in json.loads(series.read_text())['stale_results']
    run('story_editor')
    assert '20_story' not in json.loads(series.read_text())['stale_results']


def test_full_and_from_planner_refresh_inputs_before_writer(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    for index, mode in enumerate(('full', 'from_step')):
        base = tmp_path / str(index); base.mkdir()
        pid, sid, root, synopsis, story, series = _s13_story_setup(base, monkeypatch)
        def produce(step, ctx):
            if step.id == 'story_planner':
                synopsis.write_text('{"text":"new"}')
                (synopsis.parent / 'beats.json').write_text('[]')
            if step.id == 'story_writer': story.write_text(synopsis.read_text())
        engine = FakeEngine(base, on_step=produce)
        plan = make_plan(pid, ['story_planner', 'story_writer'], mode=mode, series_id=sid)
        with series_scope(pid, sid):
            plan.update(stale_results_for_plan(pid, plan['steps_to_run'], inputs={'generate_screenplay': True}))
        result = asyncio.run(run_exec.execute_plan(engine, plan, run_id=f'{mode}-ordered'))
        assert result['status'] == 'success', result
        assert not json.loads(series.read_text())['stale_results']
        assert json.loads(story.read_text())['text'] == 'new'


def test_from_writer_does_not_confirm_stale_synopsis(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root, synopsis, story, series = _s13_story_setup(tmp_path, monkeypatch)
    engine = FakeEngine(tmp_path, on_step=lambda step, ctx: story.write_text(synopsis.read_text()))
    plan = make_plan(pid, ['story_writer'], mode='from_step', series_id=sid)
    with series_scope(pid, sid):
        plan.update(stale_results_for_plan(pid, plan['steps_to_run'], inputs={'generate_screenplay': True}))
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='from-writer'))
    assert set(json.loads(series.read_text())['stale_results']) == {'10_synopsis', '20_story'}


def test_shared_audio_marker_needs_only_ancestor_producers(tmp_path, monkeypatch):
    from custom_tools.storybook.studio.run_exec import RunSession
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    for name in ('speech.json', 'audio_manifest.json', 'music_plan.json', 'music_manifest.json', 'sfx_manifest.json'):
        (root / '98_audio' / name).write_text('{"status":"success"}')
    series_path = root / 'series.json'
    data = json.loads(series_path.read_text()); data['stale_results'] = ['98_audio']
    revision = {key: data.get(key) for key in ('source_material', 'participant_ids', 'settings', 'snapshot', 'input_revision')}
    data['stale_confirmations'] = {'98_audio': {'revision': revision, 'steps': ['storybook_speech', 'storybook_audio_subtitle'],
                                                'requirements': ['storybook_speech', 'storybook_audio_subtitle', 'music_planner', 'storybook_music_generator', 'storybook_sfx_generator']}}
    series_path.write_text(json.dumps(data))
    plan = make_plan(pid, ['music_planner'], mode='single_step', series_id=sid,
                     inputs={'generate_screenplay': True, 'generate_music': True, 'generate_sfx': True})
    session = RunSession(FakeEngine(tmp_path), plan, root, run_id='audio', surface='tk', launcher='local')
    session._series_revision = {**revision, 'sound_revision': None}
    with series_scope(pid, sid):
        assert session._stale_inputs_ready('music_planner', ['98_audio'], data) is True
        data['stale_confirmations']['98_audio']['steps'].remove('storybook_audio_subtitle')
        assert session._stale_inputs_ready('music_planner', ['98_audio'], data) is False


def test_cancel_after_step_start_revokes_confirmation(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    story = root / '20_story/story.json'; story.parent.mkdir(exist_ok=True); story.write_text('{}')
    series_path = root / 'series.json'; data = json.loads(series_path.read_text()); data['stale_results'] = ['20_story']; series_path.write_text(json.dumps(data))
    def make(engine):
        return make_plan(pid, ['story_writer'], mode='single_step', series_id=sid,
                         stale_steps={'story_writer': ['20_story']}, affected_results=['20_story'],
                         stale_requirements={'20_story': ['story_writer', 'story_editor']})
    engine = FakeEngine(tmp_path, on_step=lambda step, ctx: story.write_text('{}'))
    asyncio.run(run_exec.execute_plan(engine, make(engine), run_id='ok-first'))
    async def cancel(step, context, previous):
        raise asyncio.CancelledError()
    engine2 = FakeEngine(tmp_path); engine2._execute_enhanced_step = cancel
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_exec.execute_plan(engine2, make(engine2), run_id='cancel-after'))
    assert 'story_writer' not in json.loads(series_path.read_text())['stale_confirmations']['20_story']['steps']


def test_repeat_shot_generator_revokes_previous_qa_for_same_result(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from custom_tools.storybook.studio.run_plan import stale_results_for_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    shot = root / '97_shots/shots.json'; shot.parent.mkdir(exist_ok=True); shot.write_text('{"items":[]}')
    series = root / 'series.json'; data = json.loads(series.read_text()); data['stale_results'] = ['97_shots']; series.write_text(json.dumps(data))
    def run(step, index):
        engine = FakeEngine(tmp_path, on_step=lambda item, ctx: shot.write_text(json.dumps({'items': [], 'iteration': index})))
        plan = make_plan(pid, [step], mode='single_step', series_id=sid)
        with series_scope(pid, sid):
            plan.update(stale_results_for_plan(pid, [step], inputs={'generate_screenplay': True}, explicit_step=step))
        result = asyncio.run(run_exec.execute_plan(engine, plan, run_id=f'repeat-{index}'))
        assert result['status'] == 'success', result
    run('screenplay_shots_generator', 1)
    run('shots_prompt_qa', 2)
    run('screenplay_shots_generator', 3)
    confirmed = json.loads(series.read_text())['stale_confirmations']['97_shots']['steps']
    assert 'shots_prompt_qa' not in confirmed
    run('artist_batch_shots', 4)
    assert '97_shots' in json.loads(series.read_text())['stale_results']
    run('shots_prompt_qa', 5)
    run('artist_batch_shots', 6)
    assert '97_shots' not in json.loads(series.read_text())['stale_results']


def test_repeat_writer_revokes_editor_confirmation_of_same_result(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    story = root / '20_story/story.json'; story.parent.mkdir(exist_ok=True); story.write_text('{}')
    series = root / 'series.json'; data = json.loads(series.read_text()); data['stale_results'] = ['20_story']
    revision = {key: data.get(key) for key in ('source_material', 'participant_ids', 'settings', 'snapshot', 'input_revision')}
    data['stale_confirmations'] = {'20_story': {'revision': revision, 'steps': ['story_writer', 'story_editor'], 'requirements': ['story_writer', 'story_editor']}}
    series.write_text(json.dumps(data))
    engine = FakeEngine(tmp_path, on_step=lambda step, ctx: story.write_text('{"rewritten": true}'))
    plan = make_plan(pid, ['story_writer'], mode='single_step', series_id=sid,
                     stale_steps={'story_writer': ['20_story']}, affected_results=['20_story'],
                     stale_requirements={'20_story': ['story_writer', 'story_editor']})
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='writer-repeat'))
    data = json.loads(series.read_text())
    assert '20_story' in data['stale_results']
    assert data['stale_confirmations']['20_story']['steps'] == ['story_writer']


def test_repeat_revokes_only_confirmation_with_same_marker(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    story = root / '20_story/story.json'; story.parent.mkdir(exist_ok=True); story.write_text('{}')
    series = root / 'series.json'; data = json.loads(series.read_text()); data['stale_results'] = ['20_story', '99_final']
    revision = {key: data.get(key) for key in ('source_material', 'participant_ids', 'settings', 'snapshot', 'input_revision')}
    data['stale_confirmations'] = {
        '20_story': {'revision': revision, 'steps': ['story_writer', 'story_editor'], 'requirements': ['story_writer', 'story_editor']},
        '99_final': {'revision': {**revision, 'sound_revision': None}, 'steps': ['montage_assembler'], 'requirements': ['montage_assembler']},
    }
    series.write_text(json.dumps(data))
    engine = FakeEngine(tmp_path, on_step=lambda step, ctx: story.write_text('{"new": true}'))
    plan = make_plan(pid, ['story_writer'], mode='single_step', series_id=sid,
                     stale_steps={'story_writer': ['20_story']}, affected_results=['20_story'],
                     stale_requirements={'20_story': ['story_writer', 'story_editor']})
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='same-marker'))
    data = json.loads(series.read_text())
    assert data['stale_confirmations']['20_story']['steps'] == ['story_writer']
    assert data['stale_confirmations']['99_final']['steps'] == ['montage_assembler']


def test_cancel_before_repeat_preserves_downstream_confirmation(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine, make_plan
    pid, sid, root = create(tmp_path, monkeypatch)
    story = root / '20_story/story.json'; story.parent.mkdir(exist_ok=True); story.write_text('{}')
    series = root / 'series.json'; data = json.loads(series.read_text()); data['stale_results'] = ['20_story']
    revision = {key: data.get(key) for key in ('source_material', 'participant_ids', 'settings', 'snapshot', 'input_revision')}
    data['stale_confirmations'] = {'20_story': {'revision': revision, 'steps': ['story_writer', 'story_editor'], 'requirements': ['story_writer', 'story_editor']}}
    series.write_text(json.dumps(data))
    engine = FakeEngine(tmp_path)
    plan = make_plan(pid, ['story_writer'], mode='single_step', series_id=sid,
                     stale_steps={'story_writer': ['20_story']}, affected_results=['20_story'],
                     stale_requirements={'20_story': ['story_writer', 'story_editor']})
    engine.cancelled.add(plan['workflow_id'])
    asyncio.run(run_exec.execute_plan(engine, plan, run_id='cancel-before-repeat'))
    assert json.loads(series.read_text())['stale_confirmations']['20_story']['steps'] == ['story_writer', 'story_editor']
