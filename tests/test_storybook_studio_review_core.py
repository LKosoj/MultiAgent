"""Ф7: ревью шотов, версии, перегенерация, счётчики видео."""

import importlib
import json
from pathlib import Path

import pytest

from custom_tools.storybook.studio import capabilities as caps
from tests.test_storybook_studio_shots import PID, build

REV = "custom_tools.storybook.studio.review"


def rv():
    return importlib.import_module(REV)


def rg():
    return importlib.import_module("custom_tools.storybook.studio.regeneration")


def call(cap, **payload):
    return caps.invoke(cap, {"project_id": PID, **payload})


def bad(cap, **payload):
    r = call(cap, **payload)
    assert r["status"] == "error", r
    return r["error"]


def under_lock(fn, *a, **kw):
    h = rg().acquire_lock(PID)
    try:
        return fn(*a, **kw)
    finally:
        rg().release_lock(h)


def prep(keys, what="images"):
    return under_lock(rv().prepare_regeneration, PID, keys, what)


def fin(regen_id):
    return under_lock(rv().finish_regeneration, PID, regen_id)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = tmp_path / PID
    root.mkdir()
    build(root)
    importlib.import_module("custom_tools.storybook.studio.shots")._CACHE.clear()
    return root


def test_registered_admin_only():
    reg = caps.registry()
    want = {"review_get": "read", "review_set_status": "write", "review_set_note": "write", "versions_list": "read",
            "versions_compare": "read", "versions_restore": "write", "regenerate_preview": "read",
            "regenerate_start": "run"}
    for cap, access in want.items():
        assert reg[cap].access == access and reg[cap].category == "review"


def test_status_flow_and_conflict(project):
    r = call("review_get")
    assert r["counts"]["pending"] == 2 and r["shots"]["1-1"]["entry_digest"] == "j1:none"
    s = call("review_set_status", keys=["1-1"], status="approved", expected_digests={"1-1": "j1:none"})
    assert s["updated"] == ["1-1"] and s["counts"]["approved"] == 1
    e = bad("review_set_status", keys=["1-1"], status="redo", expected_digests={"1-1": "j1:none"})
    assert e["details"]["reason"] == "review_changed"
    assert call("review_set_status", keys=["1-1"], status="approved")["unchanged"] == ["1-1"]
    e = bad("review_set_status", keys=["9-9"], status="approved")
    assert e["code"] == "not_found"
    note = call("review_set_note", key="1-1", note="свет", expected_digest=call("review_get", key="1-1")["shots"]["1-1"]["entry_digest"])
    assert note["entry"]["note"] == "свет"


def test_hooks(project):
    assert rv().review_counts(PID) == {"approved": 0, "pending": 2, "redo": 0, "rejected": 0}
    assert rv().shot_review_map(PID) is None
    call("review_set_status", keys=["2-3"], status="redo")
    assert rv().shot_review_map(PID)["2-3"]["status_title"] == "Переделать"
    assert rv().status_title("approved") == "Одобрен"


def test_prepare_snapshots_and_rollback(project):
    start = project / "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    call("review_set_status", keys=["1-1"], status="approved")
    res = prep(["1-1"])
    assert res["status"] == "success" and not start.exists()
    assert list((project / "97_shots/_versions/1-1").iterdir())
    start.write_bytes(b"NEW")
    done = fin(res["regen_id"])
    assert "1-1" in done["review_reset"] or done["replaced"]
    assert call("review_get")["shots"]["1-1"]["status"] == "pending"
    vl = call("versions_list", key="1-1")
    assert len(vl["versions"]) == 1


def test_prepare_failed_run_restores(project):
    start = project / "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    res = prep(["1-1"])
    done = fin(res["regen_id"])
    assert start.read_bytes() == b"S11"
    assert done["rolled_back"]


def test_restore_version(project):
    start = project / "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    res = prep(["1-1"])
    start.write_bytes(b"NEW")
    fin(res["regen_id"])
    vid = call("versions_list", key="1-1")["versions"][0]["id"]
    cmp = call("versions_compare", key="1-1", left=vid, right="current")
    assert cmp["left"]["id"] == vid
    out = call("versions_restore", key="1-1", version_id=vid, parts=["start"])
    assert out["restored"] == ["start"] and start.read_bytes() == b"S11"
    assert len(call("versions_list", key="1-1")["versions"]) == 2  # снимок «NEW»
    bad("versions_restore", key="1-1", version_id="current")


def test_preview_requires_run_module(project, monkeypatch):
    runs = importlib.import_module("custom_tools.storybook.studio.runs")
    monkeypatch.delattr(runs, "run_plan_get", raising=False)
    e = bad("regenerate_preview", what="images", shot_keys=["1-1"])
    assert e["code"] == "unavailable"


def test_start_digest_and_cost_checks(project, monkeypatch):
    runs = importlib.import_module("custom_tools.storybook.studio.runs")
    started = {}
    monkeypatch.setattr(runs, "run_plan_get", lambda pid, **kw: {"plan": {
        "can_start": True, "problems": [], "steps_to_run": ["artist_batch_shots"], "plan_digest": "d1", "mode": kw["mode"]}},
        raising=False)
    monkeypatch.setattr(runs, "run_start", lambda pid, **kw: started.update(kw) or {"run_id": "r1", "workflow_id": "w"},
                        raising=False)
    settings = importlib.import_module("custom_tools.storybook.studio.run_settings")
    monkeypatch.setattr(settings, "cost_estimate_get", lambda pid, **kw: {
        "status": "success", "total_known_rub": 12.0, "unknown_components": []})
    pv = call("regenerate_preview", what="images", shot_keys=["1-1"])
    assert pv["confirm_required"]["cost_rub"] == 12.0 and pv["expanded_keys"] == ["1-1"]
    e = bad("regenerate_start", what="images", shot_keys=["1-1"], launcher="backend", expected_plan_digest="bad")
    assert e["details"]["reason"] == "plan_changed"
    e = bad("regenerate_start", what="images", shot_keys=["1-1"], launcher="backend",
             expected_plan_digest=pv["plan_digest"])
    assert e["details"]["reason"] == "cost_confirmation_required"
    out = call("regenerate_start", what="images", shot_keys=["1-1"], launcher="backend",
               expected_plan_digest=pv["plan_digest"], confirm_cost_rub=12.0)
    assert out["run_id"] == "r1" and started["expected_plan_digest"] == "d1"


def test_video_counters_skip_versions(tmp_path):
    from custom_tools.storybook import storybook_surface as ss
    shots_dir = tmp_path / "97_shots"
    (shots_dir / "_versions/1-1/x").mkdir(parents=True)
    (shots_dir / "_versions/1-1/x/v.mp4").write_bytes(b"v")
    assert ss._has_shot_video(tmp_path) is False
    (shots_dir / "scene_01_shot_01").mkdir()
    (shots_dir / "scene_01_shot_01/v.mp4").write_bytes(b"v")
    assert ss._has_shot_video(tmp_path) is True
