"""Дымовой прогон studio/status на настоящем проекте (только чтение). Нет проекта - пропуск."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from custom_tools.storybook.studio import capabilities as caps
from tests._storybook_status_fixture import _fresh_status_cache  # noqa: F401

ROOT = Path(__file__).resolve().parents[1] / "plots" / "storybooks"
PROJECT = "platonov_20261024"

pytestmark = pytest.mark.skipif(not (ROOT / PROJECT).is_dir(), reason="нет настоящего проекта")


@pytest.fixture
def result(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(ROOT))
    monkeypatch.setenv("STORYBOOK_WORKFLOW_DB", str(tmp_path / "нет.db"))
    for name in ("reports", "shots", "review", "runs"):
        import sys
        monkeypatch.setitem(sys.modules, f"custom_tools.storybook.studio.{name}", None)
    started = time.perf_counter()
    data = caps.invoke("project_status", {"project_id": PROJECT})
    data["_cold"] = time.perf_counter() - started
    return data


def test_real_project_status(result):
    assert result["status"] == "success"
    steps = {s["id"]: s for s in result["steps"]}
    assert len(steps) == 36
    shots = result["shots"]
    assert shots["total"] == 50 and shots["with_videos"] == 50
    assert result["spend"]["total"] == pytest.approx(4062.73, rel=0.01)
    assert steps["video_generator"]["state"] == "done"
    assert steps["video_generator"]["note_code"] in (None, "recovered_after_failure")
    for sid, row in steps.items():
        if sid.startswith("blockout_"):
            assert row["state"] in ("done", "skipped"), sid
    assert result["active_run"]["state"] == "idle"
    assert result["sources"]["run_files_unparsed"] == 0


def test_real_project_speed(result):
    assert result["_cold"] < 2.0
    started = time.perf_counter()
    caps.invoke("project_status", {"project_id": PROJECT})
    assert time.perf_counter() - started < 0.1
