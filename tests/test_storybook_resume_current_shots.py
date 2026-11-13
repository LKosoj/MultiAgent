"""Character-reference continuation must use the saved, repaired shot tasks."""
import json
from types import SimpleNamespace

import pytest

from custom_tools.storybook.project_paths import series_scope
from custom_tools.storybook.studio import run_exec
from tests.studio_run_fakes import make_plan, make_series_project


@pytest.mark.parametrize('mode', ['single_step', 'from_step'])
def test_character_reference_continuation_reads_current_series_shots(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    series = make_series_project(tmp_path, 'p')
    path = series / '97_shots/shots.json'
    path.parent.mkdir()
    fresh = {'items': [{'english_prompt': 'repaired current prompt', 'reference_image_paths': ['/current.png']}]}
    path.write_text(json.dumps(fresh))
    old = {'shots_prompt_qa': {'items': [{'english_prompt': 'obsolete checkpoint'}]},
           'story_planner': {'preserve': True}}
    checkpoint = SimpleNamespace(context=SimpleNamespace(step_outputs=old, metadata={}, variables={}))
    monkeypatch.setattr(run_exec.run_plan, 'load_checkpoint', lambda *_: checkpoint)
    plan = make_plan('p', ['character_reference_states'], mode=mode, source_workflow_id='old')
    with series_scope('p', 'main'):
        context = run_exec._build_context(object(), plan, {'step_outputs': {'other_step': 'prepared'}})
    assert context.step_outputs['shots_prompt_qa'] == fresh
    assert context.step_outputs['story_planner'] == {'preserve': True}
    assert context.step_outputs['other_step'] == 'prepared'
    assert old['shots_prompt_qa']['items'][0]['english_prompt'] == 'obsolete checkpoint'


def test_running_prompt_qa_keeps_checkpoint_output_until_that_step_executes(tmp_path, monkeypatch):
    monkeypatch.setenv('STORYBOOK_PROJECTS_DIR', str(tmp_path))
    series = make_series_project(tmp_path, 'p')
    path = series / '97_shots/shots.json'
    path.parent.mkdir()
    path.write_text('{"items":[{"english_prompt":"saved"}]}')
    original = {'items': [{'english_prompt': 'checkpoint'}]}
    checkpoint = SimpleNamespace(context=SimpleNamespace(step_outputs={'shots_prompt_qa': original}, metadata={}, variables={}))
    monkeypatch.setattr(run_exec.run_plan, 'load_checkpoint', lambda *_: checkpoint)
    plan = make_plan('p', ['shots_prompt_qa', 'character_reference_states'], mode='full', source_workflow_id='old')
    with series_scope('p', 'main'):
        context = run_exec._build_context(object(), plan, {})
    assert context.step_outputs['shots_prompt_qa'] == original
