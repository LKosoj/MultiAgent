"""Возможности управления прогоном через реестр: старт, статус, пауза, остановка, журнал, история."""

import json
import threading
import time

import pytest

from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio import jobs, run_control, run_exec, run_plan, runs, status
from custom_tools.storybook.studio.capabilities import invoke
from custom_tools.storybook.studio.errors import StudioError
from tests.studio_run_fakes import FakeEngine, make_series_project
from custom_tools.storybook.project_paths import series_scope


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    for name in ("STORYBOOK_VIDEO_PROVIDER", "STORYBOOK_STYLE_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROUTERAI_API_KEY", "k")
    monkeypatch.setenv("STORYBOOK_VIDEO_PRICING_PATH", str(tmp_path / "none.yaml"))
    project = make_series_project(tmp_path, "p")
    (project / "00_brief.json").write_text(json.dumps({"title": "t"}))
    engine = FakeEngine(tmp_path)
    run_exec.set_engine(engine)
    run_plan._ENGINE_FAIL.clear()
    run_plan.clear_runs_cache()
    with series_scope("p", "main"):
        yield project, engine
    run_exec.set_engine(None)


def ok(result):
    assert result["status"] == "success", result
    return result


def err(result, code):
    assert result["status"] == "error" and result["error"]["code"] == code, result
    return result["error"]


def wait_idle(project, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not run_control.probe_lock(project) and run_control.read_active(project).get("state") not in (
                "queued", "running"):
            return
        time.sleep(0.02)
    raise AssertionError("прогон не завершился")


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("условие не наступило")


def test_registered_ids_access_and_scope():
    reg = caps.registry()
    expected = {"run_plan": "read", "run_start": "run", "run_status": "read", "run_set_pause": "run",
                "run_cancel": "run", "run_mark_interrupted": "write", "run_log_tail": "read",
                "run_history": "read", "run_steps_get": "read"}
    assert {k: reg[k].access for k in expected} == expected
    assert reg["run_steps_get"].project_scoped is False
    assert all(reg[k].project_scoped for k in expected if k != "run_steps_get")


def test_run_plan_and_steps_via_invoke(env):
    plan = ok(invoke("run_plan", {"project_id": "p", "series_id": "main", "mode": "full"}))["plan"]
    assert plan["can_start"] and len(plan["steps_to_run"]) == len(run_plan.steps_catalog())
    err(invoke("run_plan", {"project_id": "нет_такого", "series_id": "main", "mode": "full"}), "not_found")
    err(invoke("run_plan", {"project_id": "p", "series_id": "main", "mode": "бред"}), "invalid_argument")
    err(invoke("run_plan", {"project_id": "p", "series_id": "main", "mode": "full", "лишнее": 1}), "invalid_argument")
    steps = ok(invoke("run_steps_get", {}))["steps"]
    assert len(steps) == len(run_plan.steps_catalog()) and steps[0]["id"] == "brief_from_prompt"


def test_start_local_full_run_end_to_end(env):
    project, engine = env
    res = ok(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "full", "launcher": "local"}))
    assert res["run_id"].startswith("tk-") and len(res["run_id"]) == 15
    wait_idle(project)
    assert len(engine.executed) == len(run_plan.steps_catalog())
    record = run_control.read_active(project)
    assert record["state"] == "completed" and record["surface"] == "tk" and record["launcher"] == "local"
    state = ok(invoke("run_status", {"project_id": "p", "series_id": "main"}))
    assert state["state"] == "completed" and state["outcome"] == "completed" and state["locked"] is False
    assert state["run"]["run_id"] == res["run_id"] and len(state["steps"]) == len(run_plan.steps_catalog())
    assert state["steps"][0]["status"] == "completed" and state["cost"]["scope"] == "video_jobs"
    assert state["engine"] == {"available": True, "error": None}
    hist = ok(invoke("run_history", {"project_id": "p", "series_id": "main"}))["runs"]
    assert hist[0]["run_id"] == res["run_id"] and hist[0]["outcome"] == "completed"
    tail = ok(invoke("run_log_tail", {"project_id": "p", "series_id": "main"}))
    assert any("Прогон" in line for line in tail["lines"])
    assert ok(invoke("run_log_tail", {"project_id": "p", "series_id": "main", "run_id": res["run_id"]}))["offset"] == tail["offset"]


def test_run_kind_does_not_use_shared_semaphore(env, monkeypatch):
    project, _ = env
    for _ in range(jobs.MAX_PARALLEL):
        assert jobs._slots.acquire(blocking=False)
    try:
        ok(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "from_step", "start_step": "brief_from_prompt",
                                "launcher": "local"}))
        wait_idle(project, timeout=10)
        assert run_control.read_active(project)["state"] == "completed"
    finally:
        for _ in range(jobs.MAX_PARALLEL):
            jobs._slots.release()


def test_start_rejects_stale_digest_blocking_errors_and_busy(env):
    project, _ = env
    plan = ok(invoke("run_plan", {"project_id": "p", "series_id": "main", "mode": "full"}))["plan"]
    stale = err(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "full", "launcher": "local",
                                     "expected_plan_digest": "0" * 64}), "conflict")
    assert stale["details"]["reason"] == "plan_changed"
    err(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "single_step", "step_id": "story_writer",
                             "launcher": "local"}), "conflict")
    err(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "full", "launcher": "ftp"}), "invalid_argument")
    err(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "full", "inputs": {"video_provider": "x"},
                             "launcher": "local"}), "invalid_argument")
    handle = run_control.acquire_lock_with_retries("p")
    try:
        run_control.write_active(project, {"run_id": "live", "state": "running", "surface": "react"})
        busy = err(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "full", "launcher": "local",
                                        "expected_plan_digest": plan["plan_digest"]}), "locked")
        assert busy["details"]["surface"] == "react" and "React" in busy["message"]
    finally:
        run_control.release_lock(handle)


def test_failed_launch_releases_reservation(env, monkeypatch):
    project, _ = env
    run_control.write_active(project, {"run_id": "old", "state": "completed"})

    def boom(*args, **kwargs):
        raise RuntimeError("не запустилось")

    monkeypatch.setattr(runs.run_launchers, "start", boom)
    result = invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "full", "launcher": "backend"})
    assert result["status"] == "error"
    assert run_control.read_active(project)["run_id"] == "old"


def test_second_start_during_reservation_is_locked(env, monkeypatch):
    project, _ = env
    monkeypatch.setattr(runs.run_launchers, "start", lambda *a, **k: None)  # бронь остаётся
    ok(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "full", "launcher": "backend"}))
    err(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "full", "launcher": "backend"}), "locked")
    assert ok(invoke("run_status", {"project_id": "p", "series_id": "main"}))["state"] == "running"  # queued наружу как running


def test_pause_resume_and_cancel_local_run(env, monkeypatch):
    project, engine = env
    engine.step_delay = 0.15
    monkeypatch.setattr(run_exec, "POLL_S", 0.03)
    started = ok(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "from_step", "start_step": "brief_from_prompt",
                                      "launcher": "local"}))
    run_id = started["run_id"]
    wait_for(lambda: run_control.compute_state(project)["state"] == "running" and engine.executed)
    msg = ok(invoke("run_set_pause", {"project_id": "p", "series_id": "main", "paused": True, "run_id": run_id}))
    assert "после текущего шага" in msg["message"]
    wait_for(lambda: run_control.compute_state(project)["state"] == "paused", timeout=8)
    status_paused = ok(invoke("run_status", {"project_id": "p", "series_id": "main"}))
    assert status_paused["state"] == "paused" and status_paused["progress"]["paused"] is True
    assert status_paused["other_surface"]["surface"] == "tk"
    err(invoke("run_set_pause", {"project_id": "p", "series_id": "main", "paused": False, "run_id": "foreign"}), "conflict")
    ok(invoke("run_set_pause", {"project_id": "p", "series_id": "main", "paused": False}))
    wait_for(lambda: run_control.compute_state(project)["state"] == "running", timeout=8)
    cancelled = ok(invoke("run_cancel", {"project_id": "p", "series_id": "main", "run_id": run_id}))
    assert cancelled["state"] in ("stopping", "running")
    wait_idle(project, timeout=15)

    assert run_control.read_active(project)["outcome"] == "cancelled"
    assert len(engine.cancel_calls) == 1
    err(invoke("run_cancel", {"project_id": "p", "series_id": "main"}), "conflict")
    err(invoke("run_set_pause", {"project_id": "p", "series_id": "main", "paused": True}), "conflict")


def test_force_cancel_only_for_backend_runs(env, monkeypatch):
    project, engine = env
    engine.step_delay = 0.1
    monkeypatch.setattr(run_exec, "POLL_S", 0.03)
    ok(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "from_step", "start_step": "brief_from_prompt",
                            "launcher": "local"}))
    wait_for(lambda: run_control.compute_state(project)["locked"])
    err(invoke("run_cancel", {"project_id": "p", "series_id": "main", "force": True}), "unsupported")
    invoke("run_cancel", {"project_id": "p", "series_id": "main"})
    wait_idle(project, timeout=15)
    run_control.write_active(project, {"run_id": "bk", "state": "running", "launcher": "backend"})
    handle = run_control.acquire_lock_with_retries("p")
    killed = []
    monkeypatch.setattr(runs.run_launchers, "cancel_backend", lambda rid: killed.append(rid) or True)
    try:
        ok(invoke("run_cancel", {"project_id": "p", "series_id": "main", "force": True}))
        assert killed == ["bk"]
    finally:
        run_control.release_lock(handle)


def test_mark_interrupted_requires_free_lock(env):
    project, _ = env
    handle = run_control.acquire_lock_with_retries("p")
    try:
        run_control.write_active(project, {"run_id": "r1", "state": "running", "workflow_id": "wf"})
        locked = err(invoke("run_mark_interrupted", {"project_id": "p", "series_id": "main", "run_id": "r1"}), "locked")
        assert "pids" in locked["details"]
    finally:
        run_control.release_lock(handle)
    assert ok(invoke("run_status", {"project_id": "p", "series_id": "main"}))["state"] == "interrupted"
    err(invoke("run_mark_interrupted", {"project_id": "p", "series_id": "main", "run_id": "other"}), "not_found")
    ok(invoke("run_mark_interrupted", {"project_id": "p", "series_id": "main", "run_id": "r1"}))
    state = ok(invoke("run_status", {"project_id": "p", "series_id": "main"}))
    assert state["state"] == "interrupted" and state["outcome"] == "interrupted" and state["resumable"] is True


def test_status_rejects_foreign_run_id_and_unsafe_ids(env):
    err(invoke("run_status", {"project_id": "p", "series_id": "main", "run_id": "none"}), "not_found")
    err(invoke("run_status", {"project_id": "p", "series_id": "main", "run_id": "../x"}), "invalid_argument")
    err(invoke("run_log_tail", {"project_id": "p", "series_id": "main"}), "not_found")
    err(invoke("run_log_tail", {"project_id": "p", "series_id": "main", "run_id": "../../etc"}), "invalid_argument")
    err(invoke("run_log_tail", {"project_id": "p", "series_id": "main", "run_id": "ok", "lines": 0}), "invalid_argument")
    err(invoke("run_history", {"project_id": "p", "series_id": "main", "limit": 0}), "invalid_argument")


def test_status_failure_and_video_progress(env):
    project, engine = env
    engine.fail_steps = {"init_project"}
    ok(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "full", "launcher": "local"}))
    wait_idle(project)
    state = ok(invoke("run_status", {"project_id": "p", "series_id": "main"}))
    assert state["state"] == "failed" and state["failure"]["step_id"] == "init_project"
    assert state["failure"]["message"] == "сломан" and state["resumable"] is True
    shots = project / "97_shots"
    shots.mkdir()
    (shots / "provider_jobs.json").write_text(json.dumps({"jobs": [
        {"shot_key": "a", "status": "downloaded", "cost": 5, "timestamps": {"created_at": time.time()}},
        {"shot_key": "a", "status": "failed"}, {"shot_key": "b", "status": "failed"},
        {"shot_key": "c", "status": "running"}]}))
    assert runs.video_progress(project) == {"done": 1, "total": 3, "failed": 1}
    state = ok(invoke("run_status", {"project_id": "p", "series_id": "main"}))
    assert state["video"]["done"] == 1 and state["cost"]["spent_rub"] == 5.0


def test_eta_critical_path_and_no_history(env):
    project, _ = env
    steps = ["brief_from_prompt", "init_project", "story_planner", "bible_builder"]
    assert runs.compute_eta(project, steps, set(), [], {})["basis"] == "no_history"
    durations = {"brief_from_prompt": 10, "init_project": 5, "story_planner": 20, "bible_builder": 30}
    (project / ".studio").mkdir()
    (project / ".studio" / "step_runs.json").write_text(json.dumps(
        {"steps": {k: {"durations": [v, v, v]} for k, v in durations.items()}}))
    eta = runs.compute_eta(project, steps, set(), [], {})
    assert eta["basis"] == "history" and eta["seconds"] > 0
    assert runs.compute_eta(project, steps, set(steps), [], {}) == {"seconds": 0, "basis": "history"}
    partial = runs.compute_eta(project, steps, {"brief_from_prompt"}, [], {})
    assert partial["seconds"] <= eta["seconds"]


def test_run_progress_and_active_run_info_hooks(env):
    project, engine = env
    assert runs.run_progress("p") is None
    assert runs.active_run_info("p") == {"active": False, "current_steps": None}
    handle = run_control.acquire_lock_with_retries("p")
    try:
        run_control.write_active(project, {"run_id": "r", "state": "running", "surface": "tk", "mode": "full",
                                           "step_id": "story_planner", "step_index": 3, "step_total": 36,
                                           "percent": 8.3, "message": "m", "running": ["story_planner"]})
        progress = runs.run_progress("p")
        assert progress["run_id"] == "r" and progress["surface"] == "tk" and progress["percent"] == 8.3
        assert runs.active_run_info("p") == {"active": True, "current_steps": ["story_planner"]}
    finally:
        run_control.release_lock(handle)


def test_state_vocabulary_matches_project_status(env):
    """Словарь состояний runs совпадает со status._active_run для живого прогона."""
    project, _ = env
    handle = run_control.acquire_lock_with_retries("p")
    try:
        run_control.write_active(project, {"run_id": "r", "state": "running", "surface": "tk", "step_id": "story_planner",
                                           "step_index": 3, "percent": 5.0})
        run_control.control_create(project, "r", "tk", None)
        pipeline = status._load_pipeline()
        for action, expected in ((lambda: None, "running"),
                                 (lambda: run_control.control_request(project, "r", pause=True), "pause_requested"),
                                 (lambda: run_control.control_request(project, "r", cancel=True), "stopping")):
            action()
            mine = run_control.compute_state(project)["state"]
            control = run_control.read_control(project)
            theirs = status._active_run({"available": False}, True, control, True, runs.run_progress("p"), "",
                                        pipeline["steps"])
            assert mine == expected == theirs["state"]
    finally:
        run_control.release_lock(handle)
    assert run_control.compute_state(project)["state"] == "interrupted"
    theirs = status._active_run({"available": True, "latest": {"status": "running", "current_step": "x",
                                                                "steps": {}, "started": None, "finished": None,
                                                                "workflow_id": "w"}},
                                False, {}, False, None, "", pipeline["steps"])
    assert theirs["state"] == "interrupted"


def test_status_uses_run_progress_hook(env):
    project, _ = env
    handle = run_control.acquire_lock_with_retries("p")
    try:
        run_control.write_active(project, {"run_id": "r", "state": "running", "surface": "react",
                                           "step_id": "story_planner", "step_index": 3, "step_total": 36,
                                           "percent": 8.0, "mode": "full"})
        run_control.control_create(project, "r", "react", None)
        data = status._call_hook("run_progress", "p")[0]
        assert data["step_id"] == "story_planner" and data["surface"] == "react"
    finally:
        run_control.release_lock(handle)


def test_discard_unfinished_only_after_successful_start(env, monkeypatch):
    project, engine = env
    engine.state_manager.store.add("sbm_p_aaaaaaaa", "failed", completed=["brief_from_prompt"], variables={"project_id": "p", "series_id": "main"})
    run_plan.clear_runs_cache()
    args = {"project_id": "p", "series_id": "main", "mode": "from_step", "start_step": "brief_from_prompt", "launcher": "backend",
            "discard_unfinished": True}

    def failing(*a, **k):
        raise StudioError("unavailable", "бэкенд недоступен")

    monkeypatch.setattr(runs.run_launchers, "start", failing)
    err(invoke("run_start", args), "unavailable")
    assert engine.cancel_calls == []  # старый прогон цел
    monkeypatch.setattr(runs.run_launchers, "start", lambda *a, **k: None)
    started = ok(invoke("run_start", args))
    cancelled = [wid for wid, _ in engine.cancel_calls]
    assert cancelled == ["sbm_p_aaaaaaaa"] and started["workflow_id"] not in cancelled
    assert run_plan.read_discarded(project) == ["sbm_p_aaaaaaaa"]


def test_run_start_discard_over_cancelled_run_then_fail_then_resume(env, monkeypatch):
    project, engine = env
    engine.state_manager.store.add("sbm_p_aaaaaaaa", "cancelled", completed=["brief_from_prompt"], variables={"project_id": "p", "series_id": "main"},
                                   ts="2026-11-01T10:00:00")
    (project / ".studio").mkdir(exist_ok=True)
    (project / ".studio" / "step_runs.json").write_text(json.dumps({"steps": {
        "brief_from_prompt": {"status": "completed", "workflow_id": "sbm_p_aaaaaaaa"}}}))
    run_plan.clear_runs_cache()
    monkeypatch.setattr(runs.run_launchers, "start", lambda *a, **k: None)
    started = ok(invoke("run_start", {"project_id": "p", "series_id": "main", "mode": "from_step", "start_step": "init_project",
                                      "launcher": "backend", "discard_unfinished": True}))
    assert engine.cancel_calls == []  # уже отменённый повторно не отменяем
    assert run_plan.read_discarded(project) == ["sbm_p_aaaaaaaa"]
    engine.state_manager.store.add(started["workflow_id"], "failed", completed=[], variables={"project_id": "p", "series_id": "main"}, ts="2026-11-01T11:00:00")
    run_plan.clear_runs_cache()
    plan = run_plan.build_plan("p", "resume", engine=engine)
    assert plan["skip_steps"] == []
