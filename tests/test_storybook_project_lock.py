from types import SimpleNamespace

from StoryBookManager.core.pipeline_runner import PipelineRunner
from custom_tools.storybook.project_paths import (
    acquire_storybook_project_lock,
    release_storybook_project_lock,
)
from workflow import streamlit_api
from workflow.models import WorkflowStatus


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
            assert acquire_storybook_project_lock("web-project") is None
            return SimpleNamespace(
                status=WorkflowStatus.COMPLETED,
                workflow_id="workflow-id",
                final_output=None,
                step_results={},
                terminal_outcome=None,
            )

    manager.engine = _Engine()
    manager._execute_workflow_in_context(
        "run-id", tmp_path / "storybook.yaml", {"project_id": "web-project"}, "session-id",
    )

    released_lock = acquire_storybook_project_lock("web-project")
    assert released_lock is not None
    release_storybook_project_lock(released_lock)


def test_web_worker_locks_yaml_default_project_id(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
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
            assert acquire_storybook_project_lock("storybook_project") is None
            return SimpleNamespace(
                status=WorkflowStatus.COMPLETED,
                workflow_id="workflow-id",
                final_output=None,
                step_results={},
                terminal_outcome=None,
            )

    manager.engine = _Engine()
    manager._execute_workflow_in_context("run-id", tmp_path / "storybook.yaml", {}, "session-id")

    released_lock = acquire_storybook_project_lock("storybook_project")
    assert released_lock is not None
    release_storybook_project_lock(released_lock)
