"""Ф11: прямые записи болванки (длительность шота и «оставить как есть»)."""

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_tools.storybook.project_paths import series_scope

from custom_tools.storybook import project_paths
from custom_tools.storybook.studio import _blockout_state as S
from custom_tools.storybook.studio import capabilities as caps
from tests.helpers_storybook_blockout import (
    PID, FakeRuns, add_final_images, build_project, fake_tools, mod, read_json)

RENDERED = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = build_project(tmp_path / PID, brief={"blockout_fps": 24, "blockout_resolution": "1280x720",
                                                "blockout_allowed_durations": [5, 10]})
    caps.registry()
    mod("blockout").reset_caches()
    mod("shots")._CACHE.clear()
    fake_tools(monkeypatch)
    runs = FakeRuns(monkeypatch)
    with series_scope(PID, "main"):
        yield SimpleNamespace(root=root, runs=runs, mp=monkeypatch)
    mod("blockout").reset_caches()


def digest_of(key="1-1"):
    return caps.invoke("blockout_detail", {"project_id": PID, "series_id": "main", "shot_key": key})["digest"]


def set_duration(seconds, digest=None, key="1-1"):
    return caps.invoke("blockout_set_duration", {"project_id": PID, "series_id": "main", "shot_key": key, "seconds": seconds,
                                                 "base_digest": digest or digest_of(key)})


def pair(env, key=(1, 1)):
    items = read_json(env.root / "97_shots" / "shots.json")["items"]
    return {i["shot_type"]: i for i in items if (i["scene_number"], i["shot_number"]) == key}


def test_set_duration_writes_both_items_through_shots(env):
    r = set_duration(10)
    assert r["status"] == "success", r
    assert r["duration_s"] == 10 and r["rebuild_needed"] is True and r["state"] == "stale"
    assert r["shot"]["duration_mismatch"] is True and r["digest"]
    for phase, item in pair(env).items():
        assert item["duration_s"] == 10 and item["duration_source"] == "manual" and item["timing"] == "00:10", phase
    assert pair(env)["start"]["duration_requested_s"] == 5
    other = pair(env, (1, 2))["start"]
    assert other["duration_s"] == 5 and "duration_source" not in other
    # исходный запрос фиксируется один раз
    back = set_duration(5)
    assert back["rebuild_needed"] is False and back["state"] == "rendered"
    assert pair(env)["start"]["duration_requested_s"] == 5


def test_set_duration_checks(env):
    assert set_duration(7)["error"]["code"] == "invalid_argument"
    assert set_duration(5, digest="stale")["error"]["code"] == "conflict"
    assert set_duration(5, key="9-9", digest="x")["error"]["code"] == "not_found"
    bad = caps.invoke("blockout_set_duration", {"project_id": PID, "series_id": "main", "shot_key": "1-1", "seconds": True, "base_digest": "x"})
    assert bad["error"]["code"] == "invalid_argument"
    bad = caps.invoke("blockout_set_duration", {"project_id": PID, "series_id": "main", "shot_key": "1-1", "seconds": 5, "base_digest": ""})
    assert bad["error"]["code"] == "invalid_argument"
    assert pair(env)["start"]["duration_s"] == 5


@pytest.mark.parametrize("steps", [["screenplay_shots_generator"], None])
def test_set_duration_locked_while_screenplay_runs(env, steps):
    digest = digest_of()
    env.runs.info.update(active=True, current_steps=steps)
    r = set_duration(10, digest)
    assert r["error"]["code"] == "locked"
    assert pair(env)["start"]["duration_s"] == 5


def test_set_duration_respects_shown_blockers(env):
    digest = digest_of()
    env.runs.codes = ["no_checkpoint"]
    mod("blockout").reset_caches()
    r = set_duration(10, digest)
    assert r["error"]["code"] == "locked"
    env.runs.codes = []
    write_brief_no_durations(env)
    assert set_duration(10, digest)["error"]["code"] == "locked"
    assert pair(env)["start"]["duration_s"] == 5


def write_brief_no_durations(env):
    from tests.helpers_storybook_blockout import write_json
    write_json(env.root / "00_brief.json", {"blockout_fps": 24})
    mod("blockout").reset_caches()


def test_set_duration_without_start_item_is_locked(env):
    digest = digest_of("1-2")
    doc = read_json(env.root / "97_shots" / "shots.json")
    doc["items"] = [i for i in doc["items"] if not ((i["scene_number"], i["shot_number"]) == (1, 2) and i["shot_type"] == "start")]
    from tests.helpers_storybook_blockout import write_json
    write_json(env.root / "97_shots" / "shots.json", doc)
    mod("blockout").reset_caches()
    r = set_duration(10, digest, key="1-2")
    assert r["error"]["code"] == "locked"


def test_set_duration_delegates_to_shots(env):
    calls = []
    shots = mod("shots")

    def fake(project_id, key, seconds, base_digest=None):
        calls.append((project_id, key, seconds, base_digest))
        return {"digest": "new"}

    env.mp.setattr(shots, "set_duration", fake)
    r = set_duration(10, "dg")
    assert calls == [(PID, "1-1", 10, "dg")] and r["digest"] == "new"
    env.runs.info.update(active=True, current_steps=["video_generator"])  # другой шаг не мешает
    assert set_duration(10, "dg")["status"] == "success"


# --- «оставить как есть» ---

def leave(keys):
    return caps.invoke("blockout_leave_as_is", {"project_id": PID, "series_id": "main", "shot_keys": keys})


def manifest_file(env, scene=1, shot=1):
    return env.root / "93_blockout" / S.shot_dir_name(scene, shot) / "manifest.json"


def test_leave_as_is_writes_only_ack(env):
    add_final_images(env.root, 1, 1, RENDERED - 100)
    add_final_images(env.root, 1, 2, RENDERED + 100)
    before = read_json(manifest_file(env))
    before_other = manifest_file(env, 1, 2).read_bytes()
    r = leave(["1-1", "1-2", "2-1", "9-9"])
    assert r["changed"] == ["1-1"]
    assert {(s["key"], s["reason"]) for s in r["skipped"]} == {
        ("1-2", "no_p10"), ("2-1", "no_p10"), ("9-9", "manifest_missing")}
    after = read_json(manifest_file(env))
    stamp = after.pop("p10_acknowledged")
    before.pop("p10_acknowledged")
    assert after == before and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp)
    assert manifest_file(env, 1, 2).read_bytes() == before_other
    state = caps.invoke("blockout_state", {"project_id": PID, "series_id": "main"})
    assert {r["key"]: r["p10"] for r in state["shots"]}["1-1"] is False
    again = leave(["1-1"])
    assert again["changed"] == [] and again["skipped"] == [{"key": "1-1", "reason": "already_acknowledged"}]
    assert not list(env.root.rglob("*.tmp"))


def test_leave_as_is_invalid_args(env):
    assert leave([])["error"]["code"] == "invalid_argument"
    assert leave(["bad"])["error"]["code"] == "invalid_argument"


def test_leave_as_is_locked_while_project_busy(env):
    add_final_images(env.root, 1, 1, RENDERED - 100)
    other = project_paths.acquire_storybook_project_lock(PID)
    assert other is not None
    try:
        r = leave(["1-1"])
        assert r["error"]["code"] == "locked"
    finally:
        project_paths.release_storybook_project_lock(other)
    assert read_json(manifest_file(env))["p10_acknowledged"] is None
    assert leave(["1-1"])["changed"] == ["1-1"]  # замок после отказа не остался занятым


def test_leave_as_is_releases_lock_on_error(env):
    add_final_images(env.root, 1, 1, RENDERED - 100)

    def boom(path, data):
        raise OSError("disk")

    env.mp.setattr(mod("project_store"), "write_json_atomic", boom)
    assert leave(["1-1"])["error"]["code"] == "internal"
    handle = project_paths.acquire_storybook_project_lock(PID)
    assert handle is not None  # замок снят
    project_paths.release_storybook_project_lock(handle)
    assert read_json(manifest_file(env))["p10_acknowledged"] is None
    assert not list(env.root.rglob("*.tmp"))
