"""Прогон из Tk (PipelineRunner) виден студии: active_run.json, control.json, step_runs.json, журнал."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from custom_tools.storybook.studio import run_control
from StoryBookManager.core.pipeline_runner import PipelineRunner
from tests.studio_run_fakes import FakeEngine
from workflow.models import StepResult, StepStatus, WorkflowResult, WorkflowStatus


class TkEngine(FakeEngine):
    """Движок, у которого Tk-хуки (`_execute_workflow_step`) стоят в том же порядке, что в настоящем."""

    async def _execute_workflow_step(self, step, context, workflow_def):
        self.executed.append(step.id)
        await asyncio.sleep(self.step_delay)
        return StepResult(step_id=step.id, status=StepStatus.COMPLETED, duration_seconds=0.1)

    async def execute_workflow(self, definition, context):
        results = {}
        status = WorkflowStatus.COMPLETED
        for step in definition.steps:
            if context.workflow_id in self.cancelled:
                status = WorkflowStatus.CANCELLED
                break
            result = await self._execute_workflow_step(step, context, definition)
            results[step.id] = result
            await self._on_step_completed(context.workflow_id, step, result, context, results)
        from datetime import datetime
        return WorkflowResult(workflow_id=context.workflow_id, status=status, start_time=datetime.now(),
                              step_results=results)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    project = tmp_path / "p"
    project.mkdir()
    monkeypatch.setattr(PipelineRunner, "_initialize_engine", lambda self: None)
    obj = PipelineRunner()
    obj.engine = TkEngine(tmp_path)
    return obj, project


def _definition(*ids):
    return SimpleNamespace(steps=[SimpleNamespace(id=i) for i in ids])


def _run(obj, steps, workflow_id="sbm_p_abcdef12"):
    handle = obj._acquire_project_lock("p")
    assert handle is not None

    async def go():
        obj.current_workflow_id = workflow_id
        obj._install_step_hook(lambda **kw: None, len(steps), list(steps))
        try:
            context = SimpleNamespace(workflow_id=workflow_id, variables={"project_id": "p"})
            result = await obj.engine.execute_workflow(_definition(*steps), context)
            obj._studio_result = result
            return result
        finally:
            await obj._studio_finish()
            obj._release_project_lock(handle)
            obj._uninstall_step_hook()

    return asyncio.run(go())


def test_tk_run_is_visible_to_studio(runner):
    obj, project = runner
    _run(obj, ["brief_from_prompt", "init_project"])
    active = run_control.read_active(project)
    assert active["surface"] == "tk" and active["outcome"] == "completed" and active["run_id"].startswith("tk-")
    assert active["step_total"] == 2 and active["mode"] == "full"
    steps = json.loads((project / ".studio" / "step_runs.json").read_text())
    assert "brief_from_prompt" in json.dumps(steps)
    assert (project / ".studio" / "logs" / f"{active['run_id']}.log").is_file()
    assert run_control.read_control(project) == {}  # управляющий файл снят
    assert run_control.read_history(project)[-1]["surface"] == "tk"


def test_tk_pause_and_cancel_go_through_control_file(runner):
    obj, project = runner
    steps = ["brief_from_prompt", "init_project", "story_planner"]
    obj.engine.step_delay = 0.1
    seen = {}

    async def scenario():
        handle = obj._acquire_project_lock("p")
        obj.current_workflow_id = "sbm_partial_p_abcdef12"
        obj._install_step_hook(lambda **kw: None, 3, steps)
        context = SimpleNamespace(workflow_id=obj.current_workflow_id, variables={})
        task = asyncio.ensure_future(obj.engine.execute_workflow(_definition(*steps), context))
        while obj._studio_observer is None:
            await asyncio.sleep(0.01)
        await obj.pause_pipeline()
        seen["pause"] = run_control.read_control(project).get("pause_requested")
        await obj.resume_pipeline()
        seen["resume"] = run_control.read_control(project).get("pause_requested")
        await obj.cancel_pipeline()
        seen["cancel"] = run_control.read_control(project).get("cancel_requested")
        result = await task
        obj._studio_result = result
        await obj._studio_finish()
        obj._release_project_lock(handle)
        obj._uninstall_step_hook()

    asyncio.run(scenario())
    assert seen == {"pause": True, "resume": False, "cancel": True}
    active = run_control.read_active(project)
    assert active["outcome"] == "cancelled" and active["mode"] == "from_step"


def test_observer_failure_does_not_break_tk(runner, monkeypatch):
    obj, project = runner
    from custom_tools.storybook.studio import run_observer

    def boom(*a, **k):
        raise RuntimeError("нет доступа")

    monkeypatch.setattr(run_observer, "TkRunObserver", boom)
    result = _run(obj, ["brief_from_prompt"])
    assert result.status == WorkflowStatus.COMPLETED
    assert run_control.read_active(project) == {}


def test_step_started_failure_does_not_break_tk(runner, monkeypatch):
    obj, project = runner
    from custom_tools.storybook.studio import run_exec

    def boom(self, *a, **k):
        raise RuntimeError("диск недоступен")

    monkeypatch.setattr(run_exec.RunSession, "step_started", boom)
    result = _run(obj, ["brief_from_prompt", "init_project"])
    assert result.status == WorkflowStatus.COMPLETED
    assert obj.engine.executed == ["brief_from_prompt", "init_project"]
