"""Исполнитель прогона: замок, управляющие файлы, хуки движка, пауза и отмена (движок поддельный)."""

import asyncio
import json
import os

import pytest

from custom_tools.storybook import project_paths
from custom_tools.storybook.studio import run_control, run_exec
from custom_tools.storybook.studio.errors import StudioError
from tests.studio_run_fakes import FakeEngine, make_plan

STEPS = ["brief_from_prompt", "init_project", "story_planner"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    base = tmp_path / "p"
    base.mkdir()
    return base


def run(coro):
    return asyncio.run(coro)


def active(project):
    return json.loads((project / ".studio" / "active_run.json").read_text())


def test_full_run_writes_state_step_runs_and_log(project, tmp_path):
    engine = FakeEngine(tmp_path)
    seen = []
    resp = run(run_exec.execute_plan(engine, make_plan("p", STEPS), run_id="run-1", surface="react",
                                     progress_callback=lambda **kw: seen.append(kw)))
    assert resp["status"] == "success"
    assert engine.executed == STEPS
    record = active(project)
    assert record["state"] == "completed" and record["outcome"] == "completed" and record["percent"] == 100.0
    steps = json.loads((project / ".studio" / "step_runs.json").read_text())["steps"]
    assert {k: v["status"] for k, v in steps.items()} == {s: "completed" for s in STEPS}
    assert steps["init_project"]["run_id"] == "run-1" and steps["init_project"]["input_hash"]
    log = (project / ".studio" / "logs" / "run-1.log").read_text()
    assert "Шаг «init_project» завершён" in log
    assert [s["step_status"] for s in seen if s["step_id"] == "brief_from_prompt"] == ["running", "completed"]
    assert not (project / ".studio" / "control.json").exists()
    history = json.loads((project / ".studio" / "run_history.json").read_text())["runs"]
    assert history[-1]["run_id"] == "run-1" and history[-1]["outcome"] == "completed"
    assert run_control.probe_lock(project) is False


def test_lock_released_and_busy_project_rejected(project, tmp_path):
    handle = project_paths.acquire_storybook_project_lock("p")
    try:
        resp = run(run_exec.execute_plan(FakeEngine(tmp_path), make_plan("p", STEPS), run_id="run-2"))
        assert resp["status"] == "error" and resp["code"] == "locked"
    finally:
        project_paths.release_storybook_project_lock(handle)


def test_failed_step_is_reported_by_reconcile(project, tmp_path):
    engine = FakeEngine(tmp_path, fail_steps={"init_project"})
    seen = []
    resp = run(run_exec.execute_plan(engine, make_plan("p", STEPS), run_id="run-3",
                                     progress_callback=lambda **kw: seen.append(kw)))
    assert resp["status"] == "error"
    assert active(project)["state"] == "failed" and active(project)["outcome"] == "failed"
    steps = json.loads((project / ".studio" / "step_runs.json").read_text())["steps"]
    assert steps["init_project"]["status"] == "failed" and steps["init_project"]["error"] == "сломан"
    assert any(s.get("step_status") == "failed" and s["step_id"] == "init_project" for s in seen)


def test_exception_in_engine_marks_failed_and_releases_lock(project, tmp_path):
    engine = FakeEngine(tmp_path)

    async def boom(definition, context):
        raise RuntimeError("упал движок")

    engine.execute_workflow = boom
    resp = run(run_exec.execute_plan(engine, make_plan("p", STEPS), run_id="run-4"))
    assert resp["status"] == "error" and "упал движок" in resp["message"]
    assert active(project)["state"] == "failed"
    assert run_control.probe_lock(project) is False


def test_lock_held_by_caller_is_not_released(project, tmp_path):
    handle = run_control.acquire_lock_with_retries("p")
    try:
        resp = run(run_exec.execute_plan(FakeEngine(tmp_path), make_plan("p", STEPS), run_id="run-5",
                                         lock_held=True))
        assert resp["status"] == "success"
        assert run_control.probe_lock(project) is True
    finally:
        run_control.release_lock(handle)


def test_cancel_request_calls_engine_once_and_stops(project, tmp_path):
    engine = FakeEngine(tmp_path, step_delay=0.15)

    async def scenario():
        task = asyncio.ensure_future(run_exec.execute_plan(engine, make_plan("p", STEPS), run_id="run-6",
                                                           poll_s=0.02))
        await asyncio.sleep(0.1)
        run_control.control_request(project, "run-6", cancel=True, by="tk")
        run_control.control_request(project, "run-6", cancel=True, by="tk")
        return await task

    resp = run(scenario())
    assert resp["status"] == "cancelled"
    assert len(engine.cancel_calls) == 1
    assert len(engine.executed) < len(STEPS)
    assert active(project)["outcome"] == "cancelled"


def test_pause_blocks_next_step_then_resume(project, tmp_path):
    engine = FakeEngine(tmp_path, step_delay=0.1)
    states = []

    async def scenario():
        task = asyncio.ensure_future(run_exec.execute_plan(engine, make_plan("p", STEPS), run_id="run-7",
                                                           poll_s=0.02))
        await asyncio.sleep(0.05)
        run_control.control_request(project, "run-7", pause=True, by="react")
        await asyncio.sleep(0.2)
        states.append(run_control.compute_state(project)["state"])
        done_while_paused = len(engine.executed)
        await asyncio.sleep(0.2)
        assert len(engine.executed) == done_while_paused  # новые шаги не стартуют
        run_control.control_request(project, "run-7", pause=False, by="react")
        return await task

    resp = run(scenario())
    assert states == ["paused"] and resp["status"] == "success" and engine.executed == STEPS
    statuses = [c["status"].name for c in engine.state_manager.saved]
    assert statuses == ["PAUSED", "RUNNING"]


def test_early_cancel_request_before_start_is_kept(project, tmp_path):
    run_control.control_request(project, "run-8", cancel=True, by="react")
    resp = run(run_exec.execute_plan(FakeEngine(tmp_path), make_plan("p", STEPS), run_id="run-8", poll_s=0.02))
    assert resp["status"] in ("cancelled", "success")


def test_env_set_and_restored(project, tmp_path, monkeypatch):
    monkeypatch.delenv("STORYBOOK_VIDEO_PROVIDER", raising=False)
    seen = []
    engine = FakeEngine(tmp_path, on_step=lambda s, c: seen.append(os.environ.get("STORYBOOK_VIDEO_PROVIDER")))
    plan = make_plan("p", ["brief_from_prompt"], env={"STORYBOOK_VIDEO_PROVIDER": "aitunnel"})
    run(run_exec.execute_plan(engine, plan, run_id="run-9"))
    assert seen == ["aitunnel"] and "STORYBOOK_VIDEO_PROVIDER" not in os.environ


def test_step_params_context_and_checkpoint_outputs(project, tmp_path):
    engine = FakeEngine(tmp_path)
    engine.state_manager.store.add("sbm_old_aaaaaaaa", "failed", variables={"language": "en"},
                                   outputs={"story_planner": {"x": 1}})
    plan = make_plan("p", ["video_generator"], mode="single_step", source_workflow_id="sbm_old_aaaaaaaa",
                     inputs={"language": "ru"}, step_params={"video_generator": {"shot_keys": ["s01_sh01"]}})
    resp = run(run_exec.execute_plan(engine, plan, run_id="run-10"))
    assert resp["status"] == "success"
    ctx = engine.contexts[0]
    assert ctx.step_outputs == {"story_planner": {"x": 1}} and ctx.variables["project_id"] == "p"
    assert ctx.variables["language"] == "ru" and ctx.metadata["single_step_rerun"] == "video_generator"
    step = engine.definitions[0].steps[0]
    assert step.depends_on == [] and step.condition is None
    assert step.tool_params["shot_keys"] == ["s01_sh01"]


def test_hooks_pass_through_without_session(project, tmp_path):
    engine = FakeEngine(tmp_path)
    run_exec._install_hooks(engine)
    run_exec._install_hooks(engine)  # повторная установка безопасна
    assert engine._studio_hooked is True


def test_empty_plan_rejected(project, tmp_path):
    resp = run(run_exec.execute_plan(FakeEngine(tmp_path), make_plan("p", []), run_id="run-11"))
    assert resp["status"] == "error" and resp["code"] == "invalid_argument"


def test_failed_step_is_closed_so_pause_reaches_paused(project, tmp_path):
    # движок не зовёт хук завершения для упавшего шага: шаг не должен висеть в running
    engine = FakeEngine(tmp_path, fail_steps={"init_project"})
    seen = []
    run(run_exec.execute_plan(engine, make_plan("p", STEPS), run_id="run-f", progress_callback=lambda **kw: seen.append(kw)))
    failed = [s for s in seen if s.get("step_id") == "init_project" and s.get("step_status") == "failed"]
    assert len(failed) == 1  # итоговая сверка не дублирует уведомление
    steps = json.loads((project / ".studio" / "step_runs.json").read_text())["steps"]
    assert steps["init_project"]["status"] == "failed" and steps["init_project"]["error"]


def test_failed_step_does_not_stay_in_running(project, tmp_path):
    # параллельный пайплайн идёт дальше после сбоя одного шага: упавший не должен висеть «выполняется»,
    # иначе пауза не дойдёт до paused (она ждёт пустой список running)
    class Continuing(FakeEngine):
        async def execute_workflow(self, definition, context):
            results = {}
            for step in definition.steps:
                results[step.id] = await self._execute_enhanced_step(step, context, dict(results))
                if results[step.id].status.value != "failed":
                    await self._on_step_completed(context.workflow_id, step, results[step.id], context, results)
            from datetime import datetime
            from workflow.models import WorkflowResult, WorkflowStatus
            return WorkflowResult(workflow_id=context.workflow_id, status=WorkflowStatus.FAILED,
                                  start_time=datetime.now(), step_results=results, error="сбой")

    seen = {}
    engine = Continuing(tmp_path, fail_steps={"init_project"})

    def on_step(step, context):
        if step.id == "story_planner":
            seen["running"] = list(run_exec._SESSIONS[context.workflow_id].running)

    engine.on_step = on_step
    run(run_exec.execute_plan(engine, make_plan("p", STEPS), run_id="run-p"))
    assert seen["running"] == ["story_planner"]


def test_step_state_write_failure_does_not_fail_step(project, tmp_path, monkeypatch):
    def boom(self, *a, **k):
        raise OSError("диск полон")

    monkeypatch.setattr(run_exec.RunSession, "step_started", boom)
    monkeypatch.setattr(run_exec.RunSession, "step_finished", boom)
    engine = FakeEngine(tmp_path)
    resp = run(run_exec.execute_plan(engine, make_plan("p", STEPS), run_id="run-w"))
    assert resp["status"] == "success" and engine.executed == STEPS


def test_session_end_failure_still_releases_lock_and_log_handler(project, tmp_path, monkeypatch):
    import logging

    def boom(self, *a, **k):
        raise OSError("нет места")

    monkeypatch.setattr(run_exec.RunSession, "end", boom)
    before = list(logging.getLogger().handlers)
    with pytest.raises(OSError):
        run(run_exec.execute_plan_raw(FakeEngine(tmp_path), make_plan("p", STEPS), run_id="run-e"))
    assert run_control.probe_lock(project) is False
    assert list(logging.getLogger().handlers) == before


def test_reconcile_failure_does_not_skip_finish_regeneration(project, monkeypatch):
    calls = []

    class Shots:
        @staticmethod
        def reconcile_after_run(*a, **k):
            raise RuntimeError("сверка упала")

    class Review:
        @staticmethod
        def finish_regeneration(project_id, regen_id, **k):
            calls.append((regen_id, k["outcome"]))

    monkeypatch.setattr(run_exec, "_hook", lambda module, name: getattr(Shots if module == "shots" else Review, name, None))
    run_exec._finish({}, "p", {"regen_id": "rg-1"}, 0.0, "failed")
    assert calls == [("rg-1", "failed")]


def test_overwritten_unfinished_run_goes_to_history(project, tmp_path):
    (project / ".studio").mkdir()
    (project / ".studio" / "active_run.json").write_text(json.dumps({
        "run_id": "old-1", "workflow_id": "sbm_p_aaaaaaaa", "mode": "full", "surface": "tk",
        "state": "running", "outcome": None, "started_at": "2026-11-01T10:00:00"}))
    run(run_exec.execute_plan(FakeEngine(tmp_path), make_plan("p", STEPS), run_id="run-n"))
    history = json.loads((project / ".studio" / "run_history.json").read_text())["runs"]
    assert [(r["run_id"], r["outcome"]) for r in history] == [("old-1", "interrupted"), ("run-n", "completed")]
