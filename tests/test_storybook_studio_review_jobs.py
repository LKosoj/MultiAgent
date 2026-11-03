"""Ф7: оплаченные задачи провайдера (paid_job_pending, stale-пометка и откат)."""

import json

import pytest

from custom_tools.storybook.studio import regeneration as rg
from tests.test_storybook_studio_review_core import PID, call, bad, prep, fin, project, under_lock  # noqa: F401
from tests import storybook_report_samples as S

JOBS = "97_shots/provider_jobs.json"
ALL = ["submitting", "submitted", "pending", "in_progress", "completed", "downloading", "poll_timeout",
       "download_failed"]
DONE = ["downloaded", "failed", "stale", "cancelled"]


def write_jobs(root, jobs):
    (root / JOBS).write_text(json.dumps(S.provider_jobs(jobs)), encoding="utf-8")


def read_jobs(root):
    return json.loads((root / JOBS).read_text(encoding="utf-8"))["jobs"]


@pytest.mark.parametrize("status", ALL)
def test_known_status_is_pending(status):
    assert rg.is_job_pending({"status": status})


@pytest.mark.parametrize("status", DONE)
def test_terminal_status_not_pending_even_with_task(status):
    assert not rg.is_job_pending({"status": status, "task_id": "t", "video_url": "u"})


@pytest.mark.parametrize("status", ["queued", "weird", None])
def test_unknown_status_with_task_or_url_is_pending(status):
    assert rg.is_job_pending({"status": status, "task_id": "t"})
    assert rg.is_job_pending({"status": status, "video_url": "u"})
    assert not rg.is_job_pending({"status": status})


@pytest.mark.parametrize("status", ALL)
def test_prepare_video_blocked_for_each_pending_status(project, status):
    write_jobs(project, [S.provider_job("1-1", status)])
    res = prep(["1-1"], "video")
    assert res["status"] == "error" and res["error"]["details"]["reason"] == "paid_job_pending"
    assert (project / "97_shots/scene_01_shot_01/video_final_01_01.mp4").exists()  # ничего не тронуто
    assert rg.read_journal("demo") is None


def test_preview_reports_pending_and_start_blocked(project):
    write_jobs(project, [S.provider_job("1-1", "in_progress", task_id="t1")])
    assert rg.pending_paid_jobs(project, ["1-1"])[0]["status"] == "in_progress"
    assert rg.pending_paid_jobs(project, ["2-3"]) == []


@pytest.mark.parametrize("status", ALL)
def test_mark_stale_and_restore_roundtrip(project, status):
    write_jobs(project, [S.provider_job("1-1", status, task_id="t1", input_hash="h", error="e0")])
    with pytest.raises(Exception) as e:
        rg.mark_jobs_stale(project, ["1-1"], "rg-1", abandon=False)
    assert e.value.details["reason"] == "paid_job_pending"
    changed = rg.mark_jobs_stale(project, ["1-1"], "rg-1", abandon=True)
    assert changed[0]["abandoned"] and changed[0]["prior_status"] == status
    job = read_jobs(project)[0]
    assert job["status"] == "stale" and "rg-1" in job["error"]
    rg.restore_jobs(project, changed, "rg-1")
    job = read_jobs(project)[0]
    assert job["status"] == status and job["error"] == "e0"


def test_mark_stale_skips_done_and_other_shots(project):
    write_jobs(project, [S.provider_job("1-1", "downloaded"), S.provider_job("2-3", "submitted")])
    changed = rg.mark_jobs_stale(project, ["1-1"], "rg-2", abandon=False)
    assert [c["prior_status"] for c in changed] == ["downloaded"] and not changed[0]["abandoned"]
    assert read_jobs(project)[1]["status"] == "submitted"


def test_prepare_with_abandon_marks_and_finish_rolls_back_restores_job(project):
    write_jobs(project, [S.provider_job("1-1", "submitted", task_id="t1")])
    from custom_tools.storybook.studio import review as rv
    res = under_lock(rv.prepare_regeneration, "demo", ["1-1"], "video", abandon_pending_jobs=True)
    assert res["status"] == "success" and read_jobs(project)[0]["status"] == "stale"
    assert any(w["code"] == "pending_jobs_abandoned" for w in res["warnings"])
    done = fin(res["regen_id"])  # видео не появилось: откат файлов и возврат задачи
    assert done["rolled_back"] and read_jobs(project)[0]["status"] == "submitted"


def test_new_paid_job_forbids_rollback(project):
    res = prep(["1-1"], "video")
    write_jobs(project, [S.provider_job("1-1", "submitted", task_id="t9",
                                        timestamps={"created_at": "2999-01-01T00:00:00Z",
                                                    "updated_at": "2999-01-01T00:00:00Z"})])
    done = fin(res["regen_id"])
    assert done["awaiting_paid_job"] == ["1-1"] and done["rolled_back"] == []
