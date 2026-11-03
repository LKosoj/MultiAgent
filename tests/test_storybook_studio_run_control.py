"""Общий слой управления прогоном: замок, control.json, active_run.json, бронь, состояния."""

import json
import time

import pytest

from custom_tools.storybook.studio import run_control as rc
from custom_tools.storybook.studio.errors import StudioError


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    base = tmp_path / "p"
    base.mkdir()
    return base


def test_control_create_request_patch_clear(root):
    data = rc.control_create(root, "r1", "tk", "wf1")
    assert data["pause_requested"] is False and data["surface"] == "tk"
    assert rc.control_request(root, "r1", pause=True, by="react")["pause_requested"] is True
    assert rc.control_request(root, "other", pause=True) is None  # чужой прогон игнорируется
    assert rc.control_request(root, "r1", cancel=True)["cancel_requested"] is True
    assert rc.control_patch(root, "r1", ack={"cancel": True}, paused=True)
    data = rc.read_control(root)
    assert data["ack"] == {"pause": False, "cancel": True} and data["paused"] is True
    assert rc.control_patch(root, "other", paused=False) is False
    rc.control_clear(root, "other")
    assert rc.read_control(root)
    rc.control_clear(root, "r1")
    assert rc.read_control(root) == {}


def test_early_request_survives_control_create(root):
    rc.control_request(root, "r2", cancel=True, by="react")
    assert rc.read_control(root)["run_id"] == "r2"
    data = rc.control_create(root, "r2", "react", "wf")
    assert data["cancel_requested"] is True
    fresh = rc.control_create(root, "r3", "react", "wf")
    assert fresh["cancel_requested"] is False


def test_unpause_clears_ack(root):
    rc.control_create(root, "r1", "tk", None)
    rc.control_request(root, "r1", pause=True)
    rc.control_patch(root, "r1", ack={"pause": True})
    assert rc.control_request(root, "r1", pause=False)["ack"]["pause"] is False


def test_reservation_blocks_second_and_expires(root, monkeypatch):
    rec = {"run_id": "a", "surface": "react", "mode": "full", "started_at": "t"}
    rc.reserve(root, rec)
    assert rc.read_active(root)["state"] == "queued"
    with pytest.raises(StudioError) as busy:
        rc.reserve(root, {**rec, "run_id": "b"})
    assert busy.value.code == "locked" and busy.value.details["run_id"] == "a"
    assert "React" in busy.value.message
    now = time.time()
    monkeypatch.setattr(rc, "_now", lambda: now + rc.RESERVE_TTL_S + 1)
    assert rc.effective_record(rc.read_active(root)) == {}
    rc.reserve(root, {**rec, "run_id": "c"})
    assert rc.read_active(root)["run_id"] == "c"


def test_release_reservation_restores_previous(root):
    rc.write_active(root, {"run_id": "old", "state": "completed"})
    rc.reserve(root, {"run_id": "new", "surface": "tk"})
    rc.release_reservation(root, "other")
    assert rc.read_active(root)["run_id"] == "new"
    rc.release_reservation(root, "new")
    assert rc.read_active(root)["run_id"] == "old"
    rc.write_active(root, {"run_id": "x", "state": "queued", "reserved_ts": time.time()})
    rc.release_reservation(root, "x")
    assert rc.read_active(root) == {}


def test_reserve_rejected_when_lock_held(root):
    handle = rc.acquire_lock_with_retries("p")
    try:
        rc.write_active(root, {"run_id": "live", "state": "running", "surface": "tk", "step_id": "s",
                               "started_at": "t"})
        with pytest.raises(StudioError) as exc:
            rc.reserve(root, {"run_id": "n", "surface": "react"})
        assert exc.value.code == "locked" and exc.value.details["surface"] == "tk"
        assert exc.value.details["step"] == "s" and "Tk" in exc.value.message
    finally:
        rc.release_lock(handle)


def test_acquire_retries_past_short_probe(root, monkeypatch):
    calls = []
    real = rc.project_paths.acquire_storybook_project_lock

    def flaky(project_id):
        calls.append(1)
        return None if len(calls) < 3 else real(project_id)

    monkeypatch.setattr(rc.project_paths, "acquire_storybook_project_lock", flaky)
    monkeypatch.setattr(rc, "LOCK_RETRY_DELAY_S", 0)
    handle = rc.acquire_lock_with_retries("p")
    assert handle is not None and len(calls) == 3
    rc.release_lock(handle)
    monkeypatch.setattr(rc.project_paths, "acquire_storybook_project_lock", lambda pid: None)
    assert rc.acquire_lock_with_retries("p") is None


def state(root):
    return rc.compute_state(root)["state"]


def test_state_matrix(root):
    assert state(root) == "idle"
    handle = rc.acquire_lock_with_retries("p")
    try:
        rc.write_active(root, {"run_id": "r", "state": "running", "heartbeat_ts": time.time()})
        rc.control_create(root, "r", "tk", None)
        assert state(root) == "running"
        rc.control_request(root, "r", pause=True)
        assert state(root) == "pause_requested"
        rc.control_patch(root, "r", paused=True)
        assert state(root) == "paused"
        rc.control_request(root, "r", cancel=True)
        assert state(root) == "stopping"
    finally:
        rc.release_lock(handle)
    info = rc.compute_state(root)
    assert info["state"] == "interrupted" and info["stale_control"] is True and info["control"] == {}


def test_unresponsive_and_final_states(root):
    handle = rc.acquire_lock_with_retries("p")
    try:
        rc.write_active(root, {"run_id": "r", "state": "running", "heartbeat_ts": time.time() - 120})
        info = rc.compute_state(root)
        assert info["unresponsive"] is True and info["heartbeat_age_s"] > 100
    finally:
        rc.release_lock(handle)
    for recorded, expected in (("completed", "completed"), ("failed", "failed"), ("cancelled", "idle"),
                               ("interrupted", "interrupted"), ("running", "interrupted")):
        rc.write_active(root, {"run_id": "r", "state": recorded})
        assert state(root) == expected


def test_queued_counts_as_running_until_it_expires(root, monkeypatch):
    rc.reserve(root, {"run_id": "q", "surface": "react"})
    info = rc.compute_state(root)
    assert info["state"] == "running" and info["queued"] is True
    now = time.time()
    monkeypatch.setattr(rc, "_now", lambda: now + 1000)
    assert state(root) == "idle"


def test_history_keeps_last_records(root):
    for i in range(rc.HISTORY_KEEP + 5):
        rc.append_history(root, {"run_id": f"r{i}"})
    rc.append_history(root, {"run_id": "r10", "outcome": "x"})
    runs = rc.read_history(root)
    assert len(runs) == rc.HISTORY_KEEP and runs[-1]["run_id"] == "r10"


def test_broken_json_files_are_ignored(root):
    (root / ".studio").mkdir()
    (root / ".studio" / "control.json").write_text("{не json")
    (root / ".studio" / "active_run.json").write_text("[1]")
    assert rc.read_control(root) == {} and rc.read_active(root) == {}
