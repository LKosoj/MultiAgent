"""Журнал шагов .studio/step_runs.json: запись, сверка, медианы, хэш входов, расход видео."""

import json
from types import SimpleNamespace

import pytest

from custom_tools.storybook.studio import step_runs as sr


def result(status, duration=2.0, error=None):
    return SimpleNamespace(status=SimpleNamespace(value=status), duration_seconds=duration, error=error, end_time=None)


def test_started_finished_and_durations(tmp_path):
    log = sr.StepRunLog(tmp_path, "r1", "wf")
    log.mark_started("a", "h", 100.0)
    data = sr.read_step_runs(tmp_path)["steps"]["a"]
    assert data["status"] == "running" and data["input_hash"] == "h" and data["run_id"] == "r1"
    log.mark_finished("a", "completed", 105.0)
    data = sr.read_step_runs(tmp_path)["steps"]["a"]
    assert data["status"] == "completed" and data["duration_s"] == 5.0 and data["durations"] == [5.0]
    for i in range(7):
        log.mark_started("a", "h", 0.0)
        log.mark_finished("a", "completed", float(i + 1))
    assert len(sr.read_step_runs(tmp_path)["steps"]["a"]["durations"]) == sr.KEEP_DURATIONS
    assert sr.median_duration(tmp_path, "a") == 5.0
    assert sr.median_duration(tmp_path, "нет") is None


def test_failed_keeps_old_durations_and_truncates_error(tmp_path):
    log = sr.StepRunLog(tmp_path, "r1", "wf")
    log.mark_started("a", None, 0.0)
    log.mark_finished("a", "completed", 3.0)
    log.mark_started("a", None, 10.0)
    log.mark_finished("a", "failed", 11.0, error="x" * 900)
    rec = sr.read_step_runs(tmp_path)["steps"]["a"]
    assert rec["status"] == "failed" and rec["durations"] == [3.0] and len(rec["error"]) == sr.MAX_ERROR_CHARS


def test_reconcile_adds_failed_and_skipped(tmp_path):
    log = sr.StepRunLog(tmp_path, "r1", "wf")
    log.mark_started("done", None, 0.0)
    log.mark_finished("done", "completed", 1.0)
    log.reconcile({"done": result("completed"), "bad": result("failed", error="E"), "skip": result("skipped"),
                   "weird": result("pending")})
    steps = sr.read_step_runs(tmp_path)["steps"]
    assert steps["bad"]["status"] == "failed" and steps["bad"]["error"] == "E"
    assert steps["skip"]["status"] == "skipped" and "weird" not in steps
    assert steps["done"]["durations"] == [1.0]  # завершённый шаг не дублируется
    log.reconcile("не словарь")


def test_video_cost_taken_from_jobs(tmp_path):
    jobs = tmp_path / "97_shots"
    jobs.mkdir()
    (jobs / "provider_jobs.json").write_text(json.dumps({"jobs": [
        {"cost": 10.5, "timestamps": {"created_at": 150.0}},
        {"cost": 4, "timestamps": {"created_at": "1970-01-01T00:02:40+00:00"}},
        {"cost": 99, "timestamps": {"created_at": 10.0}},
        {"cost": "x", "timestamps": {"created_at": 150.0}},
        {"timestamps": {"created_at": 150.0}},
    ]}))
    assert sr.jobs_cost_since(tmp_path, 100.0) == 14.5
    assert sr.jobs_cost_since(tmp_path, 100.0, until_ts=155.0) == 10.5
    log = sr.StepRunLog(tmp_path, "r", "wf")
    log.mark_started("video_generator", None, 100.0)
    log.mark_finished("video_generator", "completed", 200.0)
    assert sr.read_step_runs(tmp_path)["steps"]["video_generator"]["cost_rub"] == 14.5
    assert sr.jobs_cost_since(tmp_path / "нет", 0) == 0.0


def test_input_hash_changes_with_params_and_files(tmp_path):
    (tmp_path / "20_story").mkdir()
    (tmp_path / "20_story" / "story.json").write_text("a")
    step = SimpleNamespace(tool_name="t", tool_params={"p": "{lang}", "session_id": "s1"}, depends_on=["w"])
    arts = {"w": ["20_story/story.json"]}
    base = sr.compute_step_input_hash(step, {"lang": "ru"}, tmp_path, arts)
    same = sr.compute_step_input_hash(SimpleNamespace(tool_name="t", tool_params={"p": "{lang}", "session_id": "s2"},
                                                      depends_on=["w"]), {"lang": "ru"}, tmp_path, arts)
    assert base == same  # session_id не влияет
    assert sr.compute_step_input_hash(step, {"lang": "en"}, tmp_path, arts) != base
    (tmp_path / "20_story" / "story.json").write_text("changed!")
    assert sr.compute_step_input_hash(step, {"lang": "ru"}, tmp_path, arts) != base
    assert len(base) == 64


def test_broken_file_reads_as_empty(tmp_path):
    (tmp_path / ".studio").mkdir()
    (tmp_path / ".studio" / "step_runs.json").write_text("мусор")
    assert sr.read_step_runs(tmp_path) == {"version": 1, "steps": {}}
