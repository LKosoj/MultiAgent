"""Удаление серии не открывает окно повторного захвата блокировки."""
import fcntl
import json
from pathlib import Path

import pytest

from custom_tools.storybook.project_paths import (
    acquire_storybook_project_lock, release_storybook_project_lock, series_scope,
    _require_current_lock,
)
from custom_tools.storybook.studio import series
from custom_tools.storybook.studio.errors import StudioError
from custom_tools.storybook.studio.run_settings import SETTINGS_RELPATH


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    root = tmp_path / 'world'
    for sid in ('a', 'b'):
        path = root / 'series' / sid
        path.mkdir(parents=True)
        (path / 'series.json').write_text(json.dumps({'series_id': sid, 'snapshot': {}}))
    (root / 'project.json').write_text(json.dumps({'format': 'storybook-series-v1', 'project_id': 'world',
                                                    'resources': {}, 'defaults': {}, 'language': 'ru'}))
    (root / 'shared').mkdir()
    (root / 'shared/canon.txt').write_text('shared')
    (root / 'series/b/other.txt').write_text('other')
    return root


def test_delete_hides_series_before_lock_is_unlinked_and_keeps_recreated_series(project, monkeypatch):
    old = project / 'series/a'
    with series_scope('world', 'b'):
        other_lock = acquire_storybook_project_lock('world')
    assert other_lock is not None
    original = series.shutil.rmtree
    observation = {}
    def during_delete(path, *args, **kwargs):
        target = Path(path)
        assert (target / 'series.json').exists()
        (target / '.pipeline.lock').unlink()
        observation['old_absent'] = not old.exists()
        try:
            with series_scope('world', 'a'):
                stolen = acquire_storybook_project_lock('world')
            observation['new_acquire'] = stolen is not None
            release_storybook_project_lock(stolen)
        except StudioError:
            observation['new_acquire'] = False
        if observation['old_absent']:
            result = series.series_create('world', 'A', 'replacement', [], {})
            observation['replacement_id'] = result['series_id']
        return original(path, *args, **kwargs)
    monkeypatch.setattr(series.shutil, 'rmtree', during_delete)
    try:
        assert series.series_delete('world', 'a', True)['status'] == 'success'
    finally:
        release_storybook_project_lock(other_lock)
    assert observation == {'old_absent': True, 'new_acquire': False, 'replacement_id': 'a'}
    assert json.loads((old / 'series.json').read_text())['source_material'] == 'replacement'
    assert (project / 'series/b/other.txt').read_text() == 'other'
    assert (project / 'shared/canon.txt').read_text() == 'shared'


def test_delete_respects_active_series_and_settings_locks(project):
    with series_scope('world', 'a'):
        handle = acquire_storybook_project_lock('world')
    try:
        with pytest.raises(StudioError, match='заняты'):
            series.series_delete('world', 'a', True)
    finally:
        release_storybook_project_lock(handle)
    lock_path = project / 'series/a' / (SETTINGS_RELPATH + '.lock')
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(StudioError, match='настройки'):
            series.series_delete('world', 'a', True)
    assert (project / 'series/a/series.json').exists()


def test_failed_removal_does_not_publish_partial_series(project, monkeypatch):
    def fail(path, *args, **kwargs):
        target = Path(path)
        assert target.parent == project and target.name.startswith('.delete_series_')
        (target / 'series.json').unlink()
        raise OSError('simulated delete failure')
    monkeypatch.setattr(series.shutil, 'rmtree', fail)
    with pytest.raises(OSError, match='simulated'):
        series.series_delete('world', 'a', True)
    assert not (project / 'series/a').exists()
    assert list(project.glob('.delete_series_a_*'))
    assert [item['series_id'] for item in series.series_list('world')['series']] == ['b']


def test_preopened_old_lock_is_rejected_after_same_id_recreated(project, monkeypatch):
    folder = project / 'series/a'
    old_fd = (folder / '.pipeline.lock').open('a+')
    original = series.shutil.rmtree
    def during_delete(path, *args, **kwargs):
        result = series.series_create('world', 'A', 'replacement', [], {})
        assert result['series_id'] == 'a'
        (folder / '.pipeline.lock').touch()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(series.shutil, 'rmtree', during_delete)
    try:
        series.series_delete('world', 'a', True)
        fcntl.flock(old_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(StudioError, match='заменены'):
            _require_current_lock(old_fd)
    finally:
        old_fd.close()
