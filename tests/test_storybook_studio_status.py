"""Состояние проекта (studio/status.py): шаги, источники, шоты, расходы, прогон, подсказки."""

from __future__ import annotations

import builtins
import json
import sys
import time
import types
from datetime import datetime, timedelta

import pytest

from custom_tools.storybook.project_paths import (
    acquire_storybook_project_lock,
    release_storybook_project_lock,
)
from custom_tools.storybook.studio import capabilities as caps
from tests._storybook_status_fixture import (  # noqa: F401  (autouse-фикстура сброса кэша)
    PROJECT_ID, _fresh_status_cache, status, db_step, hold_lock, list_tree, make_db, make_env, make_final,
    make_provider_jobs, make_shots, touch, write_run_file,
)

HOOK_MODULES = ("reports", "shots", "review", "runs")


@pytest.fixture
def project(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


@pytest.fixture
def no_hooks(monkeypatch):
    """Модулей других задач нет (None в sys.modules -> ModuleNotFoundError)."""
    for name in HOOK_MODULES:
        monkeypatch.setitem(sys.modules, f"custom_tools.storybook.studio.{name}", None)


def call(**kw):
    return caps.invoke("project_status", {"project_id": PROJECT_ID, **kw})


def steps_of(result):
    return {s["id"]: s for s in result["steps"]}


def fake_hooks(monkeypatch, **funcs):
    """funcs: имя модуля -> {функция: вызываемое}."""
    for name in HOOK_MODULES:
        module = types.ModuleType(f"custom_tools.storybook.studio.{name}")
        for func_name, func in funcs.get(name, {}).items():
            setattr(module, func_name, func)
        monkeypatch.setitem(sys.modules, f"custom_tools.storybook.studio.{name}", module)


# ---------------------------------------------------------------------------
# Шаги и источники
# ---------------------------------------------------------------------------

def test_empty_project(project, no_hooks):
    before = list_tree(project)
    result = call()
    assert result["status"] == "success"
    assert [s["order"] for s in result["steps"]] == list(range(1, 37))
    assert all(s["state"] in ("not_run", "skipped") for s in result["steps"])
    assert result["next_action"]["code"] == "empty_project"
    assert result["sources"]["runs_db"] == "no_file"
    assert list_tree(project) == before  # ни .studio/, ни .pipeline.lock не появились


def test_run_file_done_with_local_time(project, no_hooks, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        write_run_file(project, "story_planner", start=datetime(2026, 10, 24, 1, 18, 20, 801258),
                       end=datetime(2026, 10, 24, 1, 18, 21, 229258))
        row = steps_of(call())["story_planner"]
    finally:
        monkeypatch.undo()
        time.tzset()
    assert row["state"] == "done" and row["source"] == "run_file"
    assert row["started_at"] == "2026-10-23T22:18:20Z"
    assert row["finished_at"] == "2026-10-23T22:18:21Z"
    assert row["duration_seconds"] == pytest.approx(0.428)


def test_run_file_failed_error_is_trimmed_and_path_replaced(project, no_hooks):
    write_run_file(project, "story_planner", ok=False, error=f"{project}/x " + "я" * 1000)
    row = steps_of(call())["story_planner"]
    assert row["state"] == "failed"
    assert len(row["error"]) <= 300 and str(project) not in row["error"]
    assert row["error"].startswith("./x")


def test_big_run_file_reads_only_head_and_tail(project, no_hooks, monkeypatch):
    write_run_file(project, "story_planner", big_kb=3 * 1024)
    assert (project / "run_story_planner.json").stat().st_size > 3 * 1024 * 1024
    read_bytes = []
    real_open = builtins.open

    class Counting:
        def __init__(self, handle):
            self._handle = handle

        def read(self, *args):
            data = self._handle.read(*args)
            read_bytes.append(len(data))
            return data

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()

    monkeypatch.setattr(status, "open", lambda *a, **k: Counting(real_open(*a, **k)), raising=False)
    row = steps_of(call())["story_planner"]
    assert row["state"] == "done"
    assert sum(read_bytes) <= 16 * 1024


def test_broken_run_files_do_not_crash(project, no_hooks):
    (project / "run_story_planner.json").write_text("не json вообще", encoding="utf-8")
    (project / "run_init_project.json").write_text("", encoding="utf-8")
    good = write_run_file(project, "brief_from_prompt")
    (project / "run_bible_builder.json").write_bytes(good.read_bytes()[:50])
    result = call()
    rows = steps_of(result)
    assert rows["story_planner"]["state"] == "not_run" and rows["init_project"]["state"] == "not_run"
    assert rows["brief_from_prompt"]["state"] == "done"
    assert result["sources"]["run_files"] == 1 and result["sources"]["run_files_unparsed"] == 3


def test_db_and_run_file_conflict_latest_finished_wins(project, no_hooks, tmp_path):
    base = datetime(2026, 10, 24, 10, 0, 0)
    write_run_file(project, "story_planner", start=base, end=base + timedelta(seconds=30))
    make_db(tmp_path / "workflow_state.db", [{
        "workflow_id": "sbm_demo_ab12cd34", "status": "failed", "current_step": "story_planner",
        "step_results": {"story_planner": db_step("failed", base + timedelta(hours=1),
                                                   base + timedelta(hours=1, seconds=20), "сбой БД")},
    }])
    row = steps_of(call())["story_planner"]
    assert row["state"] == "failed" and row["source"] == "db" and row["error"] == "сбой БД"


def test_recovered_after_failure(project, no_hooks):
    keys = make_shots(project, 1, 2)
    touch(project, "97_shots/provider_jobs.json", b"{}")
    write_run_file(project, "video_generator", ok=False, output_conflict=True,
                   start=datetime(2026, 10, 27, 10, 28), end=datetime(2026, 10, 27, 10, 32))
    row = steps_of(call())["video_generator"]
    assert keys and row["state"] == "done" and row["note_code"] == "recovered_after_failure"
    assert row["confidence"] == "low"


def test_not_recovered_if_one_video_missing(project, no_hooks):
    make_shots(project, 1, 2, with_videos=1)
    write_run_file(project, "video_generator", ok=False, output_conflict=True,
                   start=datetime(2026, 10, 27, 10, 28), end=datetime(2026, 10, 27, 10, 32))
    row = steps_of(call())["video_generator"]
    assert row["state"] == "failed" and row["note_code"] == "run_file_conflict"


def test_marker_without_record_is_done_by_artifacts(project, no_hooks):
    touch(project, "20_bible/continuity_plan.json", b"{}")
    row = steps_of(call())["storybook_continuity_plan"]
    assert row["state"] == "done" and row["source"] == "artifacts"
    assert row["note_code"] == "artifacts_only" and row["confidence"] == "low"


def test_shared_marker_does_not_prove_a_step(project, no_hooks):
    make_shots(project, 1, 1)
    rows = steps_of(call())
    for sid in ("screenplay_shots_generator", "shots_prompt_qa", "artist_batch_shots"):
        assert rows[sid]["state"] == "not_run", sid


# ---------------------------------------------------------------------------
# Условия и флаги
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expr,flags,expected", [
    ("{a}", {"a": True}, True),
    ("{a} and {b}", {"a": True, "b": None}, None),
    ("{a} and {b}", {"a": False, "b": None}, False),
    ("{a} or {b}", {"a": True, "b": None}, True),
    ("{a} or {b}", {"a": False, "b": None}, None),
    ("not {a}", {"a": True}, False),
    ("not {a}", {"a": None}, None),
    ("({a} or {b}) and not {c}", {"a": False, "b": True, "c": False}, True),
    ("true and false", {}, False),
    ("{x.regenerated}", {}, None),
    ("{a} &&", {"a": True}, None),
    ("{a} {b}", {"a": True, "b": True}, None),
    ("(", {}, None),
    ("", {}, True),
])
def test_eval_condition(expr, flags, expected):
    assert status._eval_condition(expr, flags) is expected


def test_flag_in_run_settings_skips_blockout(project, no_hooks):
    touch(project, ".studio/run_settings.json", json.dumps({"generate_blockout": False}).encode())
    result = call()
    rows = steps_of(result)
    for sid in ("blockout_scene_builder", "blockout_renderer", "blockout_preview"):
        assert rows[sid]["state"] == "skipped" and rows[sid]["note_code"] == "flag_off", sid
    assert rows[sid]["confidence"] == "high"
    assert result["flags"]["generate_blockout"] == {"value": False, "source": "run_settings"}


def test_flag_priority_settings_db_brief_default(project, no_hooks, tmp_path):
    assert call()["flags"]["generate_blockout"] == {"value": True, "source": "default"}
    status._reset_cache_for_tests()
    touch(project, "00_brief.json", json.dumps({"generate_blockout": False, "title": "T"}).encode())
    assert call()["flags"]["generate_blockout"] == {"value": False, "source": "brief"}
    status._reset_cache_for_tests()
    make_db(tmp_path / "workflow_state.db", [{
        "workflow_id": "sbm_demo_ab12cd34", "status": "completed",
        "context": {"variables": {"project_id": "demo", "generate_blockout": True}}}])
    assert call()["flags"]["generate_blockout"] == {"value": True, "source": "db"}
    status._reset_cache_for_tests()
    touch(project, ".studio/run_settings.json", json.dumps({"generate_blockout": False}).encode())
    assert call()["flags"]["generate_blockout"] == {"value": False, "source": "run_settings"}


def test_dotted_condition_is_unresolved(project, no_hooks):
    row = steps_of(call())["story_editor"]
    assert row["state"] == "not_run" and row["note_code"] == "condition_unresolved"


# ---------------------------------------------------------------------------
# «Обойдён» и «устарел»
# ---------------------------------------------------------------------------

def test_bypassed_chain(project, no_hooks):
    write_run_file(project, "artist_batch_shots")
    rows = steps_of(call())
    for sid in ("blockout_scene_builder", "blockout_renderer", "blockout_preview"):
        assert rows[sid]["state"] == "skipped" and rows[sid]["note_code"] == "bypassed", sid
        assert rows[sid]["source"] == "downstream" and rows[sid]["confidence"] == "low"


def test_not_bypassed_without_done_descendants(project, no_hooks):
    rows = steps_of(call())
    assert rows["blockout_renderer"]["state"] == "not_run"


@pytest.mark.parametrize("gap,expected", [(61, True), (59, False)])
def test_outdated_hint_threshold(project, no_hooks, gap, expected):
    base = datetime(2026, 10, 24, 10, 0, 0)
    write_run_file(project, "story_planner", start=base, end=base)
    write_run_file(project, "init_project", start=base, end=base + timedelta(seconds=gap))
    row = steps_of(call())["story_planner"]
    assert row["state"] == "done"  # подсказка состояние не меняет
    assert bool(row["outdated_hint"]) is expected
    assert row["display_state"] == ("outdated" if expected else "done")
    if expected:
        assert row["outdated_hint"] == {"since_step": "init_project", "reliability": "low"}


def test_outdated_hint_ignores_artifacts_only_dependency(project, no_hooks):
    base = datetime(2026, 10, 24, 10, 0, 0)
    write_run_file(project, "storybook_video_plan", start=base, end=base)
    touch(project, "97_shots/character_reference_states.json", b"{}", mtime=time.time())
    row = steps_of(call())["storybook_video_plan"]
    assert row["outdated_hint"] is None


# ---------------------------------------------------------------------------
# Этапы
# ---------------------------------------------------------------------------

def test_every_step_in_exactly_one_stage():
    ids = [s["id"] for s in status._load_pipeline()["steps"]]
    members = [m for _sid, _title, group in status.STAGES for m in group]
    assert sorted(members) == sorted(ids) and len(members) == len(set(members)) == 36


def test_step_markers_cover_runner_artifacts():
    try:
        from StoryBookManager.core.pipeline_runner import PipelineRunner
    except Exception as exc:  # pragma: no cover  (нет Tk/движка в окружении)
        pytest.skip(f"PipelineRunner не импортируется: {exc}")
    for step_id, paths in PipelineRunner._step_output_artifacts().items():
        assert set(paths) <= set(status.STEP_MARKERS.get(step_id, ())), step_id


def _write_steps(project, ids, ok=True):
    for sid in ids:
        write_run_file(project, sid, ok=ok)


def stage_states(result):
    return {i["id"]: i["state"] for i in result["stage"]["items"]}


def test_stage_states(project, no_hooks):
    story = status.STAGES[0][2]
    _write_steps(project, story)
    result = call()
    assert stage_states(result)["story"] == "done"
    assert stage_states(result)["book"] == "not_started"
    assert result["stage"]["current"] == "book"
    status._reset_cache_for_tests()
    write_run_file(project, "items_builder")
    assert stage_states(call())["book"] == "partial"
    status._reset_cache_for_tests()
    write_run_file(project, "artist_batch", ok=False)
    assert stage_states(call())["book"] == "problems"


def test_optional_not_run_does_not_block_stage(project, no_hooks):
    _write_steps(project, ("items_builder", "artist_batch"))  # assemble_md, md_to_pdf необязательны
    assert stage_states(call())["book"] == "done"


def test_running_stage_with_active_progress(project, no_hooks, monkeypatch):
    fake_hooks(monkeypatch, runs={"run_progress": lambda pid: {
        "run_id": "r1", "mode": "full", "surface": "tk", "step_id": "story_planner",
        "step_index": 3, "step_total": 36, "percent": 41, "message": ""}})
    with hold_lock(project):
        result = call(surface="tk")
    assert stage_states(result)["story"] == "running"
    assert steps_of(result)["story_planner"]["state"] == "running"


# ---------------------------------------------------------------------------
# Шоты, финал, расходы, артефакты
# ---------------------------------------------------------------------------

def test_shots_counters_mixed(project, no_hooks):
    make_shots(project, 2, 2, with_images=3, with_videos=2)
    make_provider_jobs(project, [{"shot_key": "2-2", "status": "failed"}])
    shots = call()["shots"]
    assert (shots["total"], shots["with_images"], shots["with_videos"]) == (4, 3, 2)
    assert shots["video_expected"] == 4 and shots["errors"] == 1 and shots["ready"] == 2
    assert shots["stale"] is None and shots["approved"] is None and shots["ready_basis"] == "files"


def test_shot_paths_do_not_depend_on_cwd(project, no_hooks, tmp_path, monkeypatch):
    make_shots(project, 1, 2)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert call()["shots"]["with_videos"] == 2


def test_shots_missing_gives_nulls(project, no_hooks):
    shots = call()["shots"]
    assert shots["source"] == "missing"
    assert shots["total"] is None and shots["with_images"] is None and shots["ready"] is None


def test_broken_shots_json_keeps_previous_value(project, no_hooks):
    make_shots(project, 1, 2)
    assert call()["shots"]["total"] == 2
    path = project / "97_shots" / "shots.json"
    path.write_text("{ обрыв", encoding="utf-8")
    shots = call()["shots"]
    assert shots["partial"] is True and shots["total"] == 2
    status._reset_cache_for_tests()
    shots = call()["shots"]
    assert shots["source"] == "invalid" and shots["total"] is None
    assert next(a for a in call()["artifacts"] if a["id"] == "shots")["state"] == "invalid"


def test_zero_size_file_is_not_counted(project, no_hooks):
    make_shots(project, 1, 2)
    (project / "97_shots/scene_01_shot_01/video_final_01_01.mp4").write_bytes(b"")
    assert call()["shots"]["with_videos"] == 1


def test_spend_blocks(project, no_hooks):
    make_provider_jobs(project, [
        {"status": "downloaded", "cost": 10.5, "provider": "routerai"},
        {"status": "stale", "cost": 5, "provider": "aitunnel"},
        {"status": "failed", "cost": 0},
        {"status": "failed", "cost": None},
        {"status": "submitting", "cost": None},
        {"status": "downloaded", "cost": 3, "currency": "USD"},
        {"status": "downloaded", "cost": 2, "currency": None},
    ])
    touch(project, ".studio/run_settings.json", json.dumps({"budget_rub": 100}).encode())
    spend = call()["spend"]
    assert spend["scope"] == "video_jobs"
    assert spend["total"] == pytest.approx(17.5) and spend["wasted"] == pytest.approx(5)
    assert spend["by_status"]["downloaded"] == pytest.approx(12.5)
    assert spend["by_provider"] == {"routerai": pytest.approx(12.5), "aitunnel": pytest.approx(5)}
    assert spend["jobs_total"] == 7 and spend["jobs_failed"] == 2 and spend["jobs_unknown_cost"] == 1
    assert spend["other_currencies"] == {"USD": 3} and spend["assumed_rub"] == pytest.approx(2)
    assert spend["budget_rub"] == 100 and spend["budget_left_rub"] == pytest.approx(82.5)


def test_final_video_block(project, no_hooks):
    make_final(project, passed=True, duration=12.0, width=1280, height=720)
    final = call()["final_video"]
    assert final["exists"] and final["duration_seconds"] == 12.0
    assert (final["width"], final["height"], final["review_passed"]) == (1280, 720, True)
    status._reset_cache_for_tests()
    (project / "99_final/render_report.json").unlink()
    assert call()["final_video"]["width"] is None


def test_final_older_than_shots(project, no_hooks):
    make_shots(project, 1, 1)
    make_final(project)
    import os
    os.utime(project / "99_final/final_video.mp4", (1000, 1000))
    assert call()["final_video"]["older_than_shots"] is True


def test_artifacts_states(project, no_hooks):
    make_provider_jobs(project, [{"status": "failed"}])
    touch(project, "00_brief.json", b"{}")
    rows = {a["id"]: a for a in call()["artifacts"]}
    assert len(rows) == 22
    assert rows["provider_jobs"]["state"] == "present"  # старые неудачные попытки - не «с ошибкой»
    assert rows["brief"]["state"] == "present" and rows["story"]["state"] == "missing"
    status._reset_cache_for_tests()
    (project / "97_shots/provider_jobs.json").write_text("{", encoding="utf-8")
    assert next(a for a in call()["artifacts"] if a["id"] == "provider_jobs")["state"] == "invalid"


# ---------------------------------------------------------------------------
# Прогон, замок, база
# ---------------------------------------------------------------------------

def test_locked_project_is_running(project, no_hooks):
    with hold_lock(project):
        result = call()
    run = result["active_run"]
    assert run["locked"] and run["state"] == "running"
    assert run["text"] == "Идёт: прогон запущен, шаг уточняется"
    assert result["next_action"]["code"] == "run_active"


def test_lock_probe_does_not_create_file_or_disturb_holder(project, no_hooks):
    call()
    assert not (project / ".pipeline.lock").exists()
    with hold_lock(project):
        call()
        assert acquire_storybook_project_lock(PROJECT_ID) is None  # замок всё ещё держат
    handle = acquire_storybook_project_lock(PROJECT_ID)
    assert handle is not None
    release_storybook_project_lock(handle)


def _run_row(wid, run_status, step, steps=None, **extra):
    return {"workflow_id": wid, "status": run_status, "current_step": step,
            "step_results": steps or {}, **extra}


def test_interrupted_run(project, no_hooks, tmp_path):
    make_db(tmp_path / "workflow_state.db", [_run_row("sbm_demo_ab12cd34", "running", "video_generator")])
    result = call()
    assert result["active_run"]["state"] == "interrupted"
    assert steps_of(result)["video_generator"]["state"] == "failed"
    assert steps_of(result)["video_generator"]["note_code"] == "interrupted"
    assert result["next_action"]["code"] == "resume_interrupted"
    assert result["next_action"]["target"]["mode"] == "resume"
    assert "Генерация видео" in result["next_action"]["title"]


def test_failed_run_text(project, no_hooks, tmp_path):
    base = datetime(2026, 11, 1, 10, 0, 0)
    make_db(tmp_path / "workflow_state.db", [_run_row(
        "sbm_demo_ab12cd34", "failed", "story_planner",
        {"story_planner": db_step("failed", base, base + timedelta(seconds=5), "упал")})])
    result = call()
    assert result["active_run"]["state"] == "failed"
    assert result["active_run"]["text"] == "Прогон остановился с ошибкой на шаге «План истории»"
    assert result["next_action"]["code"] == "fix_failed_step"
    assert result["last_run"]["steps_failed"] == 1


def test_completed_run_text_and_last_run(project, no_hooks, tmp_path):
    started = (datetime.now() - timedelta(minutes=10)).isoformat()
    make_db(tmp_path / "workflow_state.db", [
        {**_run_row("sbm_demo_ab12cd34", "running", "x"), "timestamp": started},
        {**_run_row("sbm_demo_ab12cd34", "completed", None,
                    {"story_planner": db_step("completed", datetime.now(), datetime.now())}),
         "timestamp": datetime.now().isoformat(), "resumable": 0},
    ])
    result = call()
    assert result["active_run"]["state"] == "completed"
    assert result["active_run"]["text"] == "Прогон завершён, 10 мин"
    assert result["last_run"]["steps_done"] == 1 and result["last_run"]["status"] == "completed"
    assert result["last_run"]["duration_seconds"] == pytest.approx(600, abs=5)


def test_control_json_without_lock_is_stale(project, no_hooks):
    touch(project, ".studio/control.json", json.dumps({"pause_requested": True}).encode())
    run = call()["active_run"]
    assert run["stale_control"] is True and run["pause_requested"] is False
    assert run["state"] == "idle"


def test_pause_cancel_requested_with_lock(project, no_hooks):
    touch(project, ".studio/control.json", json.dumps({"pause_requested": True}).encode())
    with hold_lock(project):
        run = call()["active_run"]
    assert run["state"] == "pause_requested" and run["text"] == "Пауза после текущего шага"
    touch(project, ".studio/control.json", json.dumps({"cancel_requested": True,
                                                        "pause_requested": True}).encode())
    with hold_lock(project):
        run = call()["active_run"]
    assert run["state"] == "stopping" and run["text"] == "Остановка…"


def test_run_text_surface_variants(project, no_hooks, monkeypatch):
    fake_hooks(monkeypatch, runs={"run_progress": lambda pid: {
        "run_id": "r", "mode": "full", "surface": "tk", "step_id": "story_planner",
        "step_index": 3, "step_total": 36, "percent": 41, "message": ""}})
    with hold_lock(project):
        same = call(surface="tk")["active_run"]["text"]
        other = call(surface="react")["active_run"]["text"]
        unknown = call()["active_run"]["text"]
    assert same == "Идёт: шаг 3 из 36 «План истории», 41 %"
    assert other == "Идёт в другом приложении (Tk), шаг 3 из 36"
    assert unknown == same


def test_db_finds_both_id_kinds_and_exact_match_only(project, no_hooks, tmp_path):
    ctx = {"variables": {"project_id": "demo"}}
    rows = [
        _run_row("sbm_demo_ab12cd34", "failed", "a"),
        _run_row("sbm_partial_demo_ab12cd34", "failed", "a"),
        _run_row("workflow_20261101_154134_359ed893", "failed", "a", context=ctx),
        _run_row("sbm_demo_2_ab12cd34", "failed", "a"),
        _run_row("sbm_de_o_ab12cd34", "failed", "a"),
        _run_row("sbm_de%_ab12cd34", "failed", "a"),
        _run_row("workflow_other", "failed", "a", context={"variables": {"project_id": "demo_2"}}),
    ]
    make_db(tmp_path / "workflow_state.db", rows)
    runs = call()["incomplete_runs"]
    assert sorted(runs) == sorted(["sbm_demo_ab12cd34", "sbm_partial_demo_ab12cd34",
                                   "workflow_20261101_154134_359ed893"])


def test_db_missing_file_and_timeout(project, no_hooks, tmp_path, monkeypatch):
    assert call()["sources"]["runs_db"] == "no_file"
    make_db(tmp_path / "workflow_state.db", [_run_row("sbm_demo_ab12cd34", "failed", "a")])
    monkeypatch.setattr(status, "_DB_BUDGET_SEC", -1.0)
    monkeypatch.setattr(status, "_DB_PROGRESS_OPS", 1)
    status._reset_cache_for_tests()
    result = call()
    assert result["sources"]["runs_db"] == "timeout"
    assert result["status"] == "success"


def test_response_has_no_context_or_outputs(project, no_hooks, tmp_path):
    base = datetime(2026, 11, 1, 10, 0, 0)
    make_db(tmp_path / "workflow_state.db", [_run_row(
        "sbm_demo_ab12cd34", "failed", "story_planner",
        {"story_planner": db_step("failed", base, base + timedelta(seconds=5), "упал")},
        context={"variables": {"project_id": "demo"}, "api_key": "SECRET-TOKEN-123"})])
    text = json.dumps(call(), ensure_ascii=False)
    assert "SECRET-TOKEN-123" not in text and "не должен попасть" not in text


# ---------------------------------------------------------------------------
# Хуки и подсказки
# ---------------------------------------------------------------------------

def test_no_hooks_gives_nulls_and_silent_rules(project, no_hooks):
    make_shots(project, 1, 2)
    result = call()
    assert result["shots"]["stale"] is None and result["warnings"] == {"count": None, "source": None}
    assert result["hook_errors"] == []
    assert result["next_action"]["code"] not in ("regenerate_stale", "redo_shots", "review_shots",
                                                  "look_at_warnings")


def test_hooks_values_flow(project, no_hooks, monkeypatch):
    make_shots(project, 1, 2)
    write_run_file(project, "story_planner")  # не «пустой проект»
    fake_hooks(
        monkeypatch,
        reports={"warnings_count": lambda pid: 5},
        shots={"stale_shots_summary": lambda pid: {"count": 1, "keys": ["1-1"]}},
        review={"review_counts": lambda pid: {"approved": 1, "pending": 0, "redo": 0, "rejected": 0}},
    )
    result = call()
    shots = result["shots"]
    assert shots["stale"] == 1 and shots["ready"] == 1 and shots["ready_basis"] == "files+stale"
    assert shots["approved"] == 1
    assert result["warnings"] == {"count": 5, "source": "reports"}
    assert result["next_action"]["code"] == "regenerate_stale"
    assert result["next_action"]["target"] == {"screen": "shots", "filter": "stale",
                                               "step_id": None, "mode": None}


def test_failing_hook_is_reported_not_raised(project, no_hooks, monkeypatch):
    def boom(pid):
        raise RuntimeError("секрет не должен утечь")

    fake_hooks(monkeypatch, reports={"warnings_count": boom})
    result = call()
    assert result["status"] == "success" and result["warnings"]["count"] is None
    assert result["hook_errors"] == ["reports.warnings_count: RuntimeError"]


def test_broken_foreign_import_is_reported(project, no_hooks, monkeypatch):
    real = status.importlib.import_module

    def fake(name, *a, **k):
        if name.endswith(".reports"):
            raise ModuleNotFoundError("No module named 'nonexistent_dep'", name="nonexistent_dep")
        return real(name, *a, **k)

    monkeypatch.setattr(status.importlib, "import_module", fake)
    result = call()
    assert any(e.startswith("reports.warnings_count") for e in result["hook_errors"])


def _step(sid, state, deps=(), optional=False):
    return {"id": sid, "title": sid.upper(), "state": state, "optional": optional,
            "depends_on": list(deps)}


def _shots(**kw):
    base = {"total": 10, "with_images": 10, "with_videos": 10, "video_expected": 10, "errors": 0,
            "stale": None, "approved": None, "review_pending": None, "redo": None}
    return {**base, **kw}


def _run(**kw):
    return {"locked": False, "state": "idle", "step_index": None, "step_total": 36, "_step": None,
            **kw}


DONE_STEPS = [_step("a", "done"), _step("b", "done", ["a"])]
FINAL_OK = {"exists": True, "older_than_shots": False}


@pytest.mark.parametrize("kwargs,code", [
    (dict(run=_run(locked=True, step_index=28)), "run_active"),
    (dict(run=_run(state="interrupted", _step={"id": "b", "title": "B"})), "resume_interrupted"),
    (dict(steps=[_step("a", "not_run"), _step("b", "not_run", ["a"])]), "empty_project"),
    (dict(steps=[_step("a", "done"), _step("b", "failed", ["a"])]), "fix_failed_step"),
    (dict(shots=_shots(stale=3)), "regenerate_stale"),
    (dict(shots=_shots(errors=2)), "fix_shot_errors"),
    (dict(shots=_shots(with_images=6)), "generate_images"),
    (dict(shots=_shots(with_videos=4)), "generate_videos"),
    (dict(shots=_shots(redo=2)), "redo_shots"),
    (dict(shots=_shots(review_pending=12)), "review_shots"),
    (dict(steps=[_step("a", "done"), _step("b", "not_run", ["a"])]), "run_next_step"),
    (dict(final={"exists": False, "older_than_shots": False}), "build_film"),
    (dict(final={"exists": True, "older_than_shots": True}), "build_film"),
    (dict(warnings=5), "look_at_warnings"),
    (dict(), "done"),
])
def test_next_action_rules(kwargs, code):
    args = dict(steps=DONE_STEPS, shots=_shots(), final=FINAL_OK, warnings=None, run=_run())
    args.update(kwargs)
    assert status._next_action(**args)["code"] == code


def test_next_action_priority_pairs():
    args = dict(steps=DONE_STEPS, shots=_shots(), final=FINAL_OK, warnings=None, run=_run())
    assert status._next_action(**{**args, "shots": _shots(stale=1, errors=1)})["code"] == "regenerate_stale"
    assert status._next_action(**{**args, "shots": _shots(errors=1, with_images=5)})["code"] == "fix_shot_errors"
    assert status._next_action(**{**args, "shots": _shots(redo=1, review_pending=1)})["code"] == "redo_shots"
    failed = [_step("a", "failed"), _step("b", "not_run", ["a"])]
    assert status._next_action(**{**args, "steps": failed, "shots": _shots(stale=1)})["code"] == "fix_failed_step"
    assert status._next_action(**{**args, "run": _run(locked=True), "steps": failed})["code"] == "run_active"


def test_next_action_ignores_optional_and_blocked_steps():
    steps = [_step("a", "done"), _step("opt", "not_run", ["a"], optional=True),
             _step("blocked", "not_run", ["opt"])]
    args = dict(steps=steps, shots=_shots(), final=FINAL_OK, warnings=None, run=_run())
    assert status._next_action(**args)["code"] == "done"


def test_build_film_title_depends_on_review():
    args = dict(steps=DONE_STEPS, final={"exists": False, "older_than_shots": False},
                warnings=None, run=_run())
    assert status._next_action(shots=_shots(), **args)["title"] == "Собрать фильм"
    approved = status._next_action(shots=_shots(approved=10), **args)
    assert approved["title"] == "Все кадры одобрены — собрать фильм"
    assert approved["target"]["step_id"] == "montage_assembler"


def test_outdated_hint_does_not_change_next_action(project, no_hooks):
    base = datetime(2026, 10, 24, 10, 0, 0)
    write_run_file(project, "story_planner", start=base, end=base)
    write_run_file(project, "init_project", start=base, end=base + timedelta(seconds=300))
    result = call()
    assert steps_of(result)["story_planner"]["outdated_hint"]
    assert result["next_action"]["code"] == "run_next_step"
    assert result["next_action"]["target"]["step_id"] != "story_planner"


# ---------------------------------------------------------------------------
# Контракт
# ---------------------------------------------------------------------------

def test_contract(project, no_hooks):
    reg = caps.registry()
    assert reg["project_status"].access == "read" and reg["project_status_brief"].access == "read"
    make_shots(project, 1, 1)
    json.dumps(call())
    json.dumps(caps.invoke("project_status_brief", {"project_id": PROJECT_ID}))
    assert caps.invoke("project_status", {"project_id": ".."})["error"]["code"] == "invalid_argument"
    assert caps.invoke("project_status", {"project_id": "ghost"})["error"]["code"] == "not_found"
    assert call(surface="web")["error"]["code"] == "invalid_argument"
    assert caps.invoke("project_status_brief", {"project_id": PROJECT_ID,
                                                "surface": 1})["error"]["code"] == "invalid_argument"


def test_brief_shape(project, no_hooks):
    touch(project, "00_brief.json", json.dumps({"title": "Снег"}, ensure_ascii=False).encode())
    brief = caps.invoke("project_status_brief", {"project_id": PROJECT_ID})
    assert brief["project"] == {"id": "demo", "title": "Снег"}
    assert brief["run"]["state"] == "idle" and brief["spend"]["total"] == 0
    assert brief["final_video_exists"] is False and brief["failed_step"] is None
    assert brief["stage"]["id"] == "story"


def test_shot_key_helpers():
    assert status.shot_key(2, 3) == "2-3" and status.shot_dir_name(2, 3) == "scene_02_shot_03"


# ---------------------------------------------------------------------------
# Поиск прогонов: неполный контекст и «partial_<id>»
# ---------------------------------------------------------------------------

def test_context_query_timeout_keeps_first_query_results(project, no_hooks, tmp_path, monkeypatch):
    import sqlite3
    make_db(tmp_path / "workflow_state.db", [
        _run_row("sbm_demo_ab12cd34", "failed", "story_planner"),
        _run_row("workflow_x", "failed", "a", context={"variables": {"project_id": "demo"}}),
    ])
    real_connect = sqlite3.connect

    class Conn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            if "LIMIT ?" in sql:
                raise sqlite3.OperationalError("interrupted")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Conn(real_connect(*a, **k)))
    result = call()
    assert result["sources"]["runs_db"] == "partial"
    assert result["incomplete_runs"] == ["sbm_demo_ab12cd34"]


def test_context_query_looks_only_at_latest_rows(project, no_hooks, tmp_path, monkeypatch):
    ctx = {"variables": {"project_id": "demo"}}
    make_db(tmp_path / "workflow_state.db", [
        _run_row("workflow_old", "failed", "a", context=ctx, timestamp="2026-11-01T10:00:00"),
        _run_row("workflow_new", "failed", "a", context=ctx, timestamp="2026-11-02T10:00:00"),
    ])
    monkeypatch.setattr(status, "_DB_CONTEXT_ROWS", 1)
    monkeypatch.setattr(status, "_DB_DAYS", 10000)
    result = call()
    assert result["incomplete_runs"] == ["workflow_new"]
    # окно заполнено — история помечена неполной
    assert result["sources"]["runs_db"] == "partial"


def test_context_window_not_full_is_complete(project, no_hooks, tmp_path, monkeypatch):
    ctx = {"variables": {"project_id": "demo"}}
    make_db(tmp_path / "workflow_state.db", [
        _run_row("workflow_new", "failed", "a", context=ctx, timestamp="2026-11-02T10:00:00")])
    monkeypatch.setattr(status, "_DB_CONTEXT_ROWS", 5)
    monkeypatch.setattr(status, "_DB_DAYS", 10000)
    assert call()["sources"]["runs_db"] != "partial"


def test_context_window_exactly_full_is_complete(project, no_hooks, tmp_path, monkeypatch):
    ctx = {"variables": {"project_id": "demo"}}
    make_db(tmp_path / "workflow_state.db", [
        _run_row("workflow_new", "failed", "a", context=ctx, timestamp="2026-11-02T10:00:00")])
    monkeypatch.setattr(status, "_DB_CONTEXT_ROWS", 1)
    monkeypatch.setattr(status, "_DB_DAYS", 10000)
    # строк ровно столько, сколько вмещает окно: за окном ничего не осталось
    assert call()["sources"]["runs_db"] != "partial"


@pytest.mark.parametrize("ctx_project,owner", [("demo", "demo"), ("partial_demo", "partial_demo"),
                                               (None, None)])
def test_partial_prefix_ambiguity(project, no_hooks, tmp_path, ctx_project, owner):
    (project.parent / "partial_demo").mkdir()
    context = {"variables": {"project_id": ctx_project}} if ctx_project else {}
    make_db(tmp_path / "workflow_state.db", [
        _run_row("sbm_partial_demo_ab12cd34", "failed", "a", context=context)])
    for pid in ("demo", "partial_demo"):
        status._reset_cache_for_tests()
        runs = caps.invoke("project_status", {"project_id": pid})["incomplete_runs"]
        assert (runs == ["sbm_partial_demo_ab12cd34"]) is (pid == owner), pid


def test_partial_prefix_without_twin_project_counts(project, no_hooks, tmp_path):
    make_db(tmp_path / "workflow_state.db", [_run_row("sbm_partial_demo_ab12cd34", "failed", "a")])
    assert call()["incomplete_runs"] == ["sbm_partial_demo_ab12cd34"]


def test_pipeline_lock_held_public(project):
    assert status.pipeline_lock_held(PROJECT_ID) is False
    with hold_lock(project):
        assert status.pipeline_lock_held(PROJECT_ID) is True
