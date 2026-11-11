from types import SimpleNamespace
import pytest

from StoryBookManager.core.pipeline_runner import PipelineRunner
from custom_tools.storybook.project_paths import (
    acquire_storybook_project_lock,
    release_storybook_project_lock,
)
from workflow import streamlit_api
from workflow.models import WorkflowStatus


def _new_series(project_id):
    from custom_tools.storybook.studio.projects import projects_create
    from custom_tools.storybook.studio.series import series_create
    project = projects_create(title=project_id, description='A shared story world', genre='Story', target_age='all', id_hint=project_id)
    pid = project['project_id']
    series = series_create(pid, 'First', 'A story', [], {})
    return pid, series['series_id']


def test_same_project_lock_rejects_second_web_or_desktop_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))

    web_lock = acquire_storybook_project_lock("same-project")
    desktop_lock = PipelineRunner._acquire_project_lock(object(), "same-project")

    assert web_lock is not None
    assert desktop_lock is None
    release_storybook_project_lock(web_lock)


def test_different_projects_and_release_after_exception_are_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))

    first_lock = acquire_storybook_project_lock("first")
    second_lock = acquire_storybook_project_lock("second")

    assert first_lock is not None
    assert second_lock is not None
    try:
        raise RuntimeError("workflow failed")
    except RuntimeError:
        release_storybook_project_lock(first_lock)
    finally:
        release_storybook_project_lock(second_lock)

    released_lock = acquire_storybook_project_lock("first")
    assert released_lock is not None
    release_storybook_project_lock(released_lock)


def test_web_worker_holds_lock_through_result_write_and_releases_afterward(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id, series_id = _new_series('web_project')
    from custom_tools.storybook.studio.series import series_create
    other_series_id = series_create(project_id, 'Second', 'Another story', [], {})['series_id']
    manager = streamlit_api.WorkflowManager(use_enhanced=False)
    workflow_def = SimpleNamespace(name="storybook_pipeline", metadata={}, steps=[])
    monkeypatch.setattr(streamlit_api.WorkflowDefinition, "from_yaml", lambda _path: workflow_def)
    monkeypatch.setattr(
        streamlit_api,
        "_persist_workflow_result",
        lambda *_args, **_kwargs: SimpleNamespace(
            persistence_succeeded=True, resolved_payload={}, candidate_won=True,
        ),
    )

    class _Engine:
        async def execute_workflow_from_yaml(self, *_args, **_kwargs):
            from custom_tools.storybook.project_paths import safe_storybook_project_dir
            assert acquire_storybook_project_lock(project_id) is None
            assert safe_storybook_project_dir(project_id) == tmp_path / project_id / 'series' / series_id
            from custom_tools.storybook.project_paths import series_scope
            with series_scope(project_id, other_series_id):
                other_lock = acquire_storybook_project_lock(project_id)
                assert other_lock is not None
                release_storybook_project_lock(other_lock)
            assert safe_storybook_project_dir(project_id) == tmp_path / project_id / 'series' / series_id
            return SimpleNamespace(
                status=WorkflowStatus.COMPLETED,
                workflow_id="workflow-id",
                final_output=None,
                step_results={},
                terminal_outcome=None,
            )

    manager.engine = _Engine()
    manager._execute_workflow_in_context(
        "run-id", tmp_path / "storybook.yaml", {"project_id": project_id, "series_id": series_id}, "session-id",
    )

    from custom_tools.storybook.project_paths import series_scope
    with series_scope(project_id, series_id):
        released_lock = acquire_storybook_project_lock(project_id)
    assert released_lock is not None
    release_storybook_project_lock(released_lock)


def test_web_worker_rejects_missing_project_id_even_with_yaml_default(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project_id, series_id = _new_series('storybook_project')
    manager = streamlit_api.WorkflowManager(use_enhanced=False)
    workflow_def = SimpleNamespace(
        name="storybook_pipeline", metadata={}, steps=[], inputs={"project_id": "storybook_project"},
    )
    monkeypatch.setattr(streamlit_api.WorkflowDefinition, "from_yaml", lambda _path: workflow_def)
    monkeypatch.setattr(
        streamlit_api,
        "_persist_workflow_result",
        lambda *_args, **_kwargs: SimpleNamespace(
            persistence_succeeded=True, resolved_payload={}, candidate_won=True,
        ),
    )

    class _Engine:
        async def execute_workflow_from_yaml(self, *_args, **_kwargs):
            pytest.fail("Workflow must not start without an explicit project_id")

    manager.engine = _Engine()
    with pytest.raises(ValueError, match="project_id is required"):
        manager._execute_workflow_in_context("run-id", tmp_path / "storybook.yaml", {"series_id": series_id}, "session-id")

    released_lock = acquire_storybook_project_lock("storybook_project")
    assert released_lock is not None
    release_storybook_project_lock(released_lock)
