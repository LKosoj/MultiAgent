import json

from custom_tools.storybook.studio.capabilities import invoke


def call(name, **kwargs):
    result = invoke(name, kwargs)
    assert result['status'] == 'success', result
    return result


def test_new_project_series_and_old_directory_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    old = tmp_path / 'old'
    old.mkdir()
    (old / '00_brief.json').write_text('{"title":"old"}')
    project = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    assert json.loads((tmp_path / project / 'project.json').read_text())['format'] == 'storybook-series-v1'
    assert [p['project_id'] for p in call('projects_list')['projects']] == [project]
    a = call('series_create', project_id=project, title='One', source_material='First', participant_ids=[], settings={})['series_id']
    b = call('series_create', project_id=project, title='Two', source_material='Second', participant_ids=[], settings={})['series_id']
    for sid in (a, b):
        path = tmp_path / project / 'series' / sid / '97_shots'
        path.mkdir(exist_ok=True)
        (path / 'shot.json').write_text(sid)
    call('series_delete', project_id=project, series_id=a, confirm=True)
    assert (tmp_path / project / 'series' / b / '97_shots/shot.json').read_text() == b
    assert (old / '00_brief.json').read_text() == '{"title":"old"}'
    assert invoke('projects_get', {'project_id': 'old'})['status'] == 'error'


def test_versions_are_pinned_and_applied_only_to_selected_series(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    first = call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'voice': 'v1', 'language': 'ru', 'speech_style': 'soft'})['version_id']
    a = call('series_create', project_id=pid, title='One', source_material='', participant_ids=['fox'], settings={})['series_id']
    b = call('series_create', project_id=pid, title='Two', source_material='', participant_ids=['fox'], settings={})['series_id']
    second = call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'voice': 'v2', 'language': 'ru', 'speech_style': 'soft'})['version_id']
    def voice(sid):
        return call('series_get', project_id=pid, series_id=sid)['resources']['characters']['fox']
    assert voice(a)['version_id'] == voice(b)['version_id'] == first
    preview = call('series_resource_update_preview', project_id=pid, series_id=a, kind='characters', entity_id='fox', version_id=second)
    assert 'affected_results' in preview and voice(a)['data']['voice'] == 'v1'
    call('series_resource_update_apply', project_id=pid, series_id=a, kind='characters', entity_id='fox', version_id=second)
    assert voice(a)['data']['voice'] == 'v2' and voice(b)['data']['voice'] == 'v1'
    (tmp_path / pid / 'shared/characters/fox' / first / 'data.json').unlink()
    assert invoke('series_get', {'project_id': pid, 'series_id': b})['status'] == 'error'


def test_scope_is_nested_and_project_specific(tmp_path, monkeypatch):
    from custom_tools.storybook.project_paths import safe_storybook_project_dir, series_scope
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    sid = call('series_create', project_id=pid, title='One', source_material='', participant_ids=[], settings={})['series_id']
    with series_scope(pid, sid):
        assert safe_storybook_project_dir(pid) == tmp_path / pid / 'series' / sid
        assert safe_storybook_project_dir('other') == tmp_path / 'other'
    assert safe_storybook_project_dir(pid) == tmp_path / pid


def test_series_scoped_capability_requires_series_and_isolates_results(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    a = call('series_create', project_id=pid, title='One', source_material='', participant_ids=[], settings={})['series_id']
    b = call('series_create', project_id=pid, title='Two', source_material='', participant_ids=[], settings={})['series_id']
    assert invoke('artifacts_list', {'project_id': pid})['status'] == 'error'
    left = call('artifacts_list', project_id=pid, series_id=a)
    right = call('artifacts_list', project_id=pid, series_id=b)
    assert left['series_id'] == a and right['series_id'] == b


def test_update_project_and_series_persists_input(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    call('projects_update', project_id=pid, title='New world', description='Changed world', language='ru', defaults={'pages_min': 2})
    sid = call('series_create', project_id=pid, title='One', source_material='Old', participant_ids=[], settings={})['series_id']
    call('series_update', project_id=pid, series_id=sid, title='Revised', source_material='New source', settings={'pages_min': 3})
    series = call('series_get', project_id=pid, series_id=sid)['series']
    brief = json.loads((tmp_path / pid / 'series' / sid / '00_brief.json').read_text())
    assert series['source_material'] == brief['description'] == 'New source'
    assert brief['title'] == 'Revised' and brief['pages_min'] == 3


def test_materialize_pinned_canon_for_bible_and_style(tmp_path, monkeypatch):
    from custom_tools.storybook.studio.series import materialize_snapshot
    from custom_tools.storybook.project_paths import series_scope
    from custom_tools.storybook.bible_builder import bible_builder_tool
    from custom_tools.storybook.style_keeper import style_keeper_tool
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox', 'voice': 'v1'})
    call('shared_resource_save', project_id=pid, kind='style', entity_id='main', data={'style_text': {'narrative_voice': 'warm'}, 'style_images': {'style': 'ink'}, 'negative_list': 'modern'})
    sid = call('series_create', project_id=pid, title='One', source_material='', participant_ids=['fox'], settings={})['series_id']
    call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Changed', 'voice': 'v2'})
    materialize_snapshot(pid, sid)
    with series_scope(pid, sid):
        style_keeper_tool('test', pid)
    root = tmp_path / pid / 'series' / sid
    assert json.loads((root / '20_bible/characters.json').read_text())[0]['name'] == 'Fox'
    assert json.loads((root / '30_style/style_text.json').read_text())['narrative_voice'] == 'warm'


def test_materialize_character_without_uploaded_reference_has_generation_path(tmp_path, monkeypatch):
    from custom_tools.storybook.studio.series import materialize_snapshot
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='comedy', target_age='18+')['project_id']
    call('shared_resource_save', project_id=pid, kind='characters', entity_id='hero',
         data={'name': 'Герой', 'immutable_attributes': {'face_shape': 'вытянутое лицо'}})
    sid = call('series_create', project_id=pid, title='Pilot', source_material='', participant_ids=['hero'], settings={})['series_id']
    materialize_snapshot(pid, sid)
    character = json.loads((tmp_path / pid / 'series' / sid / '20_bible/characters.json').read_text())[0]
    assert character['reference_image_path'] == '/references/characters/hero.png'
    assert character['immutable_attributes']['face_shape'] == 'вытянутое лицо'


def test_series_tokens_do_not_allow_other_series(tmp_path, monkeypatch):
    from custom_tools.storybook.studio.media_tokens import issue_media_token, verify_media_token
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    a = call('series_create', project_id=pid, title='One', source_material='', participant_ids=[], settings={})['series_id']
    b = call('series_create', project_id=pid, title='Two', source_material='', participant_ids=[], settings={})['series_id']
    token = issue_media_token(pid, series_id=a)['token']
    assert verify_media_token(pid, token, series_id=a)
    assert not verify_media_token(pid, token, series_id=b)
    assert not verify_media_token(pid, token)


def test_reference_is_stored_with_version_and_reused(tmp_path, monkeypatch):
    import base64
    from custom_tools.storybook.studio.series import materialize_snapshot
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    saved = call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox'}, reference_base64=base64.b64encode(b'image bytes').decode(), reference_filename='fox.png')
    sid = call('series_create', project_id=pid, title='One', source_material='', participant_ids=['fox'], settings={})['series_id']
    materialize_snapshot(pid, sid)
    entity = json.loads((tmp_path / pid / 'series' / sid / '20_bible/characters.json').read_text())[0]
    ref = __import__('pathlib').Path(entity['reference_image_path'])
    assert ref.read_bytes() == b'image bytes' and saved['version_id'] in str(ref)


def test_apply_marks_existing_results_for_selected_series(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox', 'voice': 'v1'})
    a = call('series_create', project_id=pid, title='One', source_material='', participant_ids=['fox'], settings={})['series_id']
    b = call('series_create', project_id=pid, title='Two', source_material='', participant_ids=['fox'], settings={})['series_id']
    output = tmp_path / pid / 'series' / a / '99_final/final_video.mp4'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b'video')
    version = call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox', 'voice': 'v2'})['version_id']
    call('series_resource_update_apply', project_id=pid, series_id=a, kind='characters', entity_id='fox', version_id=version)
    assert '99_final' in call('series_get', project_id=pid, series_id=a)['series']['stale_results']
    assert not call('series_get', project_id=pid, series_id=b)['series'].get('stale_results')


def test_series_media_paths_do_not_escape(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException
    from backend.fastapi_app.agui._storybook_media import locate_media_file
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    a = call('series_create', project_id=pid, title='One', source_material='', participant_ids=[], settings={})['series_id']
    b = call('series_create', project_id=pid, title='Two', source_material='', participant_ids=[], settings={})['series_id']
    for sid in (a, b):
        (tmp_path / pid / 'series' / sid / 'image.png').write_bytes(sid.encode())
    assert locate_media_file(pid, 'image.png', a)[0].read_bytes() == a.encode()
    with pytest.raises(HTTPException):
        locate_media_file(pid, '../two/image.png', a)
    with pytest.raises(HTTPException):
        locate_media_file(pid, f'series/{b}/image.png')


def test_add_participant_pins_current_version_without_updating_existing(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    old = call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox'})['version_id']
    sid = call('series_create', project_id=pid, title='One', source_material='', participant_ids=['fox'], settings={})['series_id']
    call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'New Fox'})
    owl = call('shared_resource_save', project_id=pid, kind='characters', entity_id='owl', data={'name': 'Owl'})['version_id']
    call('series_update', project_id=pid, series_id=sid, title='One', source_material='', settings={}, participant_ids=['fox', 'owl'])
    snapshot = call('series_get', project_id=pid, series_id=sid)['series']['snapshot']['characters']
    assert snapshot == {'fox': old, 'owl': owl}


def test_background_job_keeps_series_scope(tmp_path, monkeypatch):
    import time
    from custom_tools.storybook.project_paths import series_scope, safe_storybook_project_dir
    from custom_tools.storybook.studio.jobs import start_job, read_job
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    sid = call('series_create', project_id=pid, title='One', source_material='', participant_ids=[], settings={})['series_id']
    with series_scope(pid, sid):
        job = start_job(tmp_path, 'test', pid, lambda progress: {'path': str(safe_storybook_project_dir(pid))})
    for _ in range(100):
        result = read_job(tmp_path, job)
        if result['state'] != 'running':
            break
        time.sleep(.01)
    assert result['series_id'] == sid
    assert result['result']['path'] == str(tmp_path / pid / 'series' / sid)


def test_run_history_separates_series_in_cache(tmp_path, monkeypatch):
    from custom_tools.storybook.project_paths import series_scope
    from custom_tools.storybook.studio.run_plan import find_runs
    from tests.studio_run_fakes import FakeEngine
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    a = call('series_create', project_id=pid, title='One', source_material='', participant_ids=[], settings={})['series_id']
    b = call('series_create', project_id=pid, title='Two', source_material='', participant_ids=[], settings={})['series_id']
    engine = FakeEngine(tmp_path)
    engine.state_manager.store.add(f'sbm_{pid}_aaaaaaaa', 'failed', variables={'project_id': pid, 'series_id': a})
    engine.state_manager.store.add(f'sbm_{pid}_bbbbbbbb', 'failed', variables={'project_id': pid, 'series_id': b})
    with series_scope(pid, a):
        first = find_runs(engine, pid, cached=True)
    with series_scope(pid, b):
        second = find_runs(engine, pid, cached=True)
    assert [x['workflow_id'] for x in first] == [f'sbm_{pid}_aaaaaaaa']
    assert [x['workflow_id'] for x in second] == [f'sbm_{pid}_bbbbbbbb']


def test_brief_is_valid_and_tracks_source_material(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    sid = call('series_create', project_id=pid, title='One', source_material='A blue fox', participant_ids=[], settings={})['series_id']
    result = call('artifact_validate', project_id=pid, series_id=sid, key='brief')
    assert result['valid'] is True
    call('series_update', project_id=pid, series_id=sid, title='One', source_material='An orange fox', settings={})
    brief = json.loads((tmp_path / pid / 'series' / sid / '00_brief.json').read_text())
    assert brief['storybook_prompt'] == 'An orange fox'


def test_missing_absolute_reference_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    result = invoke('shared_resource_save', {'project_id': pid, 'kind': 'characters', 'entity_id': 'fox', 'data': {'name': 'Fox', 'reference_image_path': '/nonexistent/fox.png'}})
    assert result['status'] == 'error'


def test_preview_includes_existing_bible_and_style(tmp_path, monkeypatch):
    from custom_tools.storybook.studio.series import materialize_snapshot
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox'})
    sid = call('series_create', project_id=pid, title='One', source_material='', participant_ids=['fox'], settings={})['series_id']
    materialize_snapshot(pid, sid)
    style = tmp_path / pid / 'series' / sid / '30_style/style_images.json'
    style.parent.mkdir(exist_ok=True)
    style.write_text('{}')
    version = call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox 2'})['version_id']
    preview = call('series_resource_update_preview', project_id=pid, series_id=sid, kind='characters', entity_id='fox', version_id=version)
    assert '20_bible/characters.json' in preview['affected_results']
    assert '30_style' in preview['affected_results']


def test_bible_adds_episode_characters_while_preserving_shared(tmp_path, monkeypatch):
    from custom_tools.storybook.studio.series import materialize_snapshot
    from custom_tools.storybook.project_paths import series_scope
    from custom_tools.storybook import bible_builder
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    call('shared_resource_save', project_id=pid, kind='characters', entity_id='fox', data={'name': 'Fox', 'appearance': 'original'})
    sid = call('series_create', project_id=pid, title='One', source_material='Fox meets Owl', participant_ids=['fox'], settings={})['series_id']
    path = tmp_path / pid / 'series' / sid
    (path / '10_synopsis').mkdir(exist_ok=True)
    (path / '10_synopsis/synopsis.json').write_text('{}')
    (path / '10_synopsis/beats.json').write_text('[]')
    materialize_snapshot(pid, sid)
    assert not (path / '20_bible/locations.json').exists()
    generated = {'characters': [{'name': 'Fox', 'appearance': 'changed'}, {'name': 'Owl'}], 'locations': [], 'consistency_rules': []}
    monkeypatch.setattr(bible_builder, 'call_openai_api', lambda **kwargs: json.dumps(generated))
    with series_scope(pid, sid):
        bible_builder.bible_builder_tool('test', pid)
    characters = json.loads((path / '20_bible/characters.json').read_text())
    assert characters[0]['name'] == 'Fox' and characters[0]['appearance'] == 'original'
    assert [x['name'] for x in characters] == ['Fox', 'Owl']


def test_empty_snapshot_does_not_disable_bible_builder(tmp_path, monkeypatch):
    from custom_tools.storybook.studio.series import materialize_snapshot
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    sid = call('series_create', project_id=pid, title='One', source_material='', participant_ids=[], settings={})['series_id']
    materialize_snapshot(pid, sid)
    path = tmp_path / pid / 'series' / sid / '20_bible'
    assert not any((path / f'{name}.json').exists() for name in ('characters', 'locations', 'consistency_rules'))


def test_full_and_single_runs_keep_series_for_artifacts_and_locks(tmp_path, monkeypatch):
    import asyncio
    from custom_tools.storybook.project_paths import safe_storybook_project_dir
    from custom_tools.storybook.studio.run_exec import execute_plan
    from tests.studio_run_fakes import FakeEngine, make_plan
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    a = call('series_create', project_id=pid, title='One', source_material='', participant_ids=[], settings={})['series_id']
    b = call('series_create', project_id=pid, title='Two', source_material='', participant_ids=[], settings={})['series_id']
    def on_step(step, context):
        root = safe_storybook_project_dir(pid)
        (root / 'result.txt').write_text(context.variables['series_id'])
    engine = FakeEngine(tmp_path, on_step=on_step)
    for index, (sid, mode) in enumerate(((a, 'full'), (b, 'single_step'))):
        plan = make_plan(pid, ['story_planner'], mode=mode, series_id=sid,
                         workflow_id=f'sbm_{pid}_aaaaaaa{index}')
        result = asyncio.run(execute_plan(engine, plan, run_id=f'run-{index}'))
        assert result['status'] == 'success', result
    for sid in (a, b):
        root = tmp_path / pid / 'series' / sid
        assert (root / 'result.txt').read_text() == sid
        assert (root / '.studio/run_history.json').is_file()


def test_public_run_plan_contains_series_id(tmp_path, monkeypatch):
    from custom_tools.storybook.studio import run_exec
    from tests.studio_run_fakes import FakeEngine
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='A shared world', genre='fairy', target_age='all')['project_id']
    sid = call('series_create', project_id=pid, title='One', source_material='Story', participant_ids=[], settings={})['series_id']
    monkeypatch.setattr(run_exec, 'get_engine', lambda: FakeEngine(tmp_path))
    result = call('run_plan', project_id=pid, series_id=sid, mode='full', task='Story')
    assert result['plan']['project_id'] == pid and result['plan']['series_id'] == sid


def test_materialize_same_character_version_preserves_pipeline_identity_cleanup(tmp_path, monkeypatch):
    from custom_tools.storybook.studio.series import materialize_snapshot
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    pid = call('projects_create', title='World', description='Shared world', genre='comedy', target_age='18+')['project_id']
    call('shared_resource_save', project_id=pid, kind='characters', entity_id='hero', data={
        'name':'Hero', 'voice':'chosen-voice', 'immutable_attributes':{'unique_features':['face','temporary prop']},
        'variable_attributes':{'accessories':['temporary prop']}})
    sid = call('series_create', project_id=pid, title='Pilot', source_material='', participant_ids=['hero'], settings={})['series_id']
    materialize_snapshot(pid, sid)
    path = tmp_path / pid / 'series' / sid / '20_bible/characters.json'
    cleaned = json.loads(path.read_text())
    cleaned[0]['immutable_attributes']['unique_features'] = ['face']
    cleaned[0]['variable_attributes']['accessories'] = []
    path.write_text(json.dumps(cleaned))
    materialize_snapshot(pid, sid)
    assert json.loads(path.read_text()) == cleaned
