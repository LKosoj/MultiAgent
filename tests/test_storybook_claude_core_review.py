import copy
import fcntl
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from custom_tools.storybook.project_paths import (
    acquire_storybook_project_lock, release_storybook_project_lock, series_scope,
)
from custom_tools.storybook.studio import artifacts, project_store
from custom_tools.storybook.studio.errors import StudioError
from custom_tools.storybook.studio.capabilities import invoke
from tests.studio_run_fakes import make_series_project


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    return make_series_project(tmp_path)


def test_project_busy_and_destructive_operations_see_series_run(world, tmp_path):
    with series_scope('demo', 'main'):
        handle = acquire_storybook_project_lock('demo')
    try:
        assert project_store.is_busy(tmp_path / 'demo')
        result = invoke('projects_delete', {'project_id': 'demo', 'confirm': True})
        assert result['error']['code'] == 'locked', result
        with pytest.raises(StudioError, match='другое приложение|прогон'):
            project_store._lock_or_raise(tmp_path / 'demo')
    finally:
        release_storybook_project_lock(handle)
    assert not project_store.is_busy(tmp_path / 'demo')


def test_project_operation_blocks_new_series_runs(world, tmp_path):
    handle = project_store._lock_or_raise(tmp_path / 'demo')
    try:
        with series_scope('demo', 'main'):
            attempt = acquire_storybook_project_lock('demo')
        try:
            assert attempt is None
        finally:
            release_storybook_project_lock(attempt)
    finally:
        project_store.unlock(handle)


def test_project_operation_blocks_deleting_a_series(world, tmp_path):
    handle = project_store._lock_or_raise(tmp_path / 'demo')
    try:
        result = invoke('series_delete', {'project_id': 'demo', 'series_id': 'main', 'confirm': True})
        assert result['status'] == 'error' and result['error']['code'] == 'locked', result
        assert world.is_dir()
    finally:
        project_store.unlock(handle)


def test_deleted_series_is_not_recreated_by_a_previously_selected_run(world):
    with series_scope('demo', 'main'):
        result = invoke('series_delete', {'project_id': 'demo', 'series_id': 'main', 'confirm': True})
        assert result['status'] == 'success', result
        handle = None
        try:
            with pytest.raises(StudioError) as failure:
                handle = acquire_storybook_project_lock('demo')
            assert failure.value.code == 'not_found'
        finally:
            release_storybook_project_lock(handle)
    assert not world.exists()
    project_store.unlock(project_store._lock_or_raise(world.parent.parent))
    repeated = invoke('series_delete', {'project_id': 'demo', 'series_id': 'main', 'confirm': True})
    assert repeated['error']['code'] == 'not_found'
    assert not world.exists()


def test_series_lock_rejects_deleted_inode_after_same_id_is_recreated(world, monkeypatch):
    flock = fcntl.flock
    changed = False

    def replace_before_lock(fd, operation):
        nonlocal changed
        if not changed and operation & fcntl.LOCK_EX and os.readlink(f'/proc/self/fd/{fd}') == str(world / '.pipeline.lock'):
            changed = True
            assert invoke('series_delete', {'project_id': 'demo', 'series_id': 'main', 'confirm': True})['status'] == 'success'
            created = invoke('series_create', {'project_id': 'demo', 'title': 'Main', 'source_material': 'New series', 'participant_ids': [], 'settings': {}})
            assert created['series_id'] == 'main'
            (world / '.pipeline.lock').touch()
        return flock(fd, operation)

    monkeypatch.setattr(fcntl, 'flock', replace_before_lock)
    handle = None
    with series_scope('demo', 'main'):
        try:
            with pytest.raises(StudioError) as failure:
                handle = acquire_storybook_project_lock('demo')
            assert failure.value.code == 'not_found'
        finally:
            release_storybook_project_lock(handle)
        current = acquire_storybook_project_lock('demo')
        try:
            assert current is not None
            assert acquire_storybook_project_lock('demo') is None
        finally:
            release_storybook_project_lock(current)


def test_parent_lock_rejects_deleted_inode_after_project_restore(world, tmp_path, monkeypatch):
    backup_dir = tmp_path / '_backups'
    backup = project_store.create_backup(tmp_path, backup_dir, 'demo')
    flock = fcntl.flock
    changed = False

    def restore_before_lock(fd, operation):
        nonlocal changed
        if not changed and operation & fcntl.LOCK_SH and os.readlink(f'/proc/self/fd/{fd}') == str(tmp_path / 'demo/.pipeline.lock'):
            changed = True
            project_store.delete_project(tmp_path, backup_dir, 'demo')
            project_store.restore_backup(tmp_path, backup_dir, 'demo', backup['backup_id'])
            (tmp_path / 'demo/.pipeline.lock').touch()
        return flock(fd, operation)

    monkeypatch.setattr(fcntl, 'flock', restore_before_lock)
    handle = None
    with series_scope('demo', 'main'):
        try:
            with pytest.raises(StudioError) as failure:
                handle = acquire_storybook_project_lock('demo')
            assert failure.value.code == 'not_found'
        finally:
            release_storybook_project_lock(handle)
    project_store.unlock(project_store._lock_or_raise(tmp_path / 'demo'))


def test_saving_settings_cannot_recreate_series_deleted_during_validation(world, monkeypatch):
    from custom_tools.storybook.studio import run_settings
    validate = run_settings.validate

    def delete_after_validation(settings, project_id, strict):
        issues = validate(settings, project_id, strict=strict)
        assert invoke('series_delete', {'project_id': 'demo', 'series_id': 'main', 'confirm': True})['status'] == 'success'
        return issues

    monkeypatch.setattr(run_settings, 'validate', delete_after_validation)
    result = invoke('run_settings_save', {'project_id': 'demo', 'series_id': 'main', 'settings': run_settings.default_settings()})
    assert result['status'] == 'error' and result['error']['code'] == 'not_found', result
    assert not world.exists()


def test_pending_settings_do_not_write_into_a_recreated_series(world, monkeypatch):
    from custom_tools.storybook.studio import run_settings
    validate = run_settings.validate

    def replace_after_validation(settings, project_id, strict):
        issues = validate(settings, project_id, strict=strict)
        assert invoke('series_delete', {'project_id': 'demo', 'series_id': 'main', 'confirm': True})['status'] == 'success'
        created = invoke('series_create', {'project_id': 'demo', 'title': 'Main', 'source_material': 'New story', 'participant_ids': [], 'settings': {}})
        assert created['series_id'] == 'main'
        (world / '.studio').mkdir(exist_ok=True)
        return issues

    monkeypatch.setattr(run_settings, 'validate', replace_after_validation)
    result = invoke('run_settings_save', {'project_id': 'demo', 'series_id': 'main', 'settings': run_settings.default_settings()})
    assert result['status'] == 'error' and result['error']['code'] == 'not_found', result
    assert not (world / run_settings.SETTINGS_RELPATH).exists()


def test_series_cannot_be_deleted_while_settings_are_being_written(world, monkeypatch):
    from custom_tools.storybook.studio import run_settings
    run_active = run_settings._run_active
    attempts = []

    def try_delete_before_write(project_id):
        attempts.append(invoke('series_delete', {'project_id': 'demo', 'series_id': 'main', 'confirm': True}))
        return run_active(project_id)

    monkeypatch.setattr(run_settings, '_run_active', try_delete_before_write)
    result = invoke('run_settings_save', {'project_id': 'demo', 'series_id': 'main', 'settings': run_settings.default_settings()})
    assert result['status'] == 'success', result
    assert len(attempts) == 1 and attempts[0]['error']['code'] == 'locked'
    assert world.is_dir()


def test_restore_refuses_a_running_series(world, tmp_path):
    backup_dir = tmp_path / '_backups'
    backup = project_store.create_backup(tmp_path, backup_dir, 'demo')
    with series_scope('demo', 'main'):
        handle = acquire_storybook_project_lock('demo')
    try:
        with pytest.raises(StudioError, match='другое приложение|прогон'):
            project_store.restore_backup(tmp_path, backup_dir, 'demo', backup['backup_id'])
        assert world.is_dir()
    finally:
        release_storybook_project_lock(handle)


def test_restore_keeps_the_same_lock_after_directory_swap(world, tmp_path, monkeypatch):
    backup_dir = tmp_path / '_backups'
    backup = project_store.create_backup(tmp_path, backup_dir, 'demo')
    rename = project_store.os.rename
    admitted = []

    def observe_swap(source, destination):
        rename(source, destination)
        if Path(destination) == tmp_path / 'demo':
            with series_scope('demo', 'main'):
                attempt = acquire_storybook_project_lock('demo')
            admitted.append(attempt is not None)
            release_storybook_project_lock(attempt)

    monkeypatch.setattr(project_store.os, 'rename', observe_swap)
    project_store.restore_backup(tmp_path, backup_dir, 'demo', backup['backup_id'])
    assert admitted == [False]
    with series_scope('demo', 'main'):
        handle = acquire_storybook_project_lock('demo')
    assert handle is not None
    release_storybook_project_lock(handle)


def test_public_pipeline_requires_explicit_project_id():
    from backend.fastapi_app.agui._pipeline_requests import StorybookPipelineRequest
    with pytest.raises(ValidationError, match='project_id'):
        StorybookPipelineRequest(task='Story', series_id='main')


def test_second_style_entity_is_rejected_but_new_version_is_allowed(world):
    payload = {'project_id': 'demo', 'kind': 'style', 'entity_id': 'main', 'data': {'style_images': {'style': 'ink'}}}
    first = invoke('shared_resource_save', payload)
    assert first['status'] == 'success', first
    second = invoke('shared_resource_save', {**payload, 'entity_id': 'other'})
    assert second['status'] == 'error', second
    revised = invoke('shared_resource_save', {**payload, 'data': {'style_images': {'style': 'pencil'}}})
    assert revised['status'] == 'success' and revised['version_id'] != first['version_id']


def test_inventory_omits_legacy_directory(world, tmp_path):
    from custom_tools.storybook.storybook_surface import storybook_project_inventory
    old = tmp_path / 'old'; old.mkdir()
    (old / '00_brief.json').write_text('{"title":"Old"}')
    assert [row['project_id'] for row in storybook_project_inventory()['projects']] == ['demo']
    assert (old / '00_brief.json').is_file()


def test_screenplay_save_and_restore_invalidate_measured_speech(world):
    screenplay = json.loads((Path(__file__).parent / 'fixtures/storybook_artifacts/screenplay.json').read_text())
    path = world / '91_screenplay/screenplay.json'; path.parent.mkdir()
    path.write_text(json.dumps(screenplay))
    audio = world / '98_audio'; audio.mkdir()
    speech = audio / 'speech.json'; speech.write_text('{"status":"success"}')
    (audio / 'audio_manifest.json').write_text('{}')
    (world / '97_shots').mkdir(); (world / '99_final').mkdir()
    changed = copy.deepcopy(screenplay); changed['screenplay'][0]['voiceover'] = 'Изменённая реплика.'
    result = invoke('artifact_save', {'project_id': 'demo', 'series_id': 'main', 'key': 'screenplay',
                                    'base_digest': artifacts.file_digest(path.read_bytes()), 'content': changed})
    assert result['status'] == 'success', result
    assert json.loads(speech.read_text())['status'] == 'stale'
    speech.write_text('{"status":"success"}')
    result = invoke('artifact_restore', {'project_id': 'demo', 'series_id': 'main', 'key': 'screenplay',
        'base_digest': artifacts.file_digest(path.read_bytes()), 'version_id': result['version_id']})
    assert result['status'] == 'success', result
    assert json.loads(speech.read_text())['status'] == 'stale'
    state = json.loads((world / 'series.json').read_text())
    assert {'98_audio/speech.json', '97_shots', '98_audio/audio_manifest.json', '99_final'} <= set(state['stale_results'])
