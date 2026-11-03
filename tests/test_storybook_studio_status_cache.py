"""Кэш и скорость studio/status: без обхода дерева, повторный вызов не перечитывает файлы."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import threading
import time

import pytest

from custom_tools.storybook.studio import capabilities as caps
from tests._storybook_status_fixture import (  # noqa: F401
    PROJECT_ID, _fresh_status_cache, status, hold_lock, make_db, make_env, make_final, make_provider_jobs,
    make_shots, write_run_file,
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    for name in ("reports", "shots", "review", "runs"):
        monkeypatch.setitem(sys.modules, f"custom_tools.storybook.studio.{name}", None)
    project = make_env(tmp_path, monkeypatch)
    make_shots(project, 2, 3)
    make_provider_jobs(project, [{"status": "downloaded", "cost": 5}])
    make_final(project)
    for step in ("story_planner", "init_project", "artist_batch_shots"):
        write_run_file(project, step)
    return project


def call(name="project_status", **kw):
    return caps.invoke(name, {"project_id": PROJECT_ID, **kw})


def count(monkeypatch, name):
    counter = {"n": 0}
    real = getattr(status, name)

    def wrapper(*a, **k):
        counter["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(status, name, wrapper)
    return counter


def test_warm_call_does_not_reread_files(project, monkeypatch):
    first = call()
    assert first["cache"]["static_hit"] is False
    loads, tails = count(monkeypatch, "_load_json"), count(monkeypatch, "_load_tail")
    second = call()
    assert second["cache"]["static_hit"] is True
    assert loads["n"] == 0 and tails["n"] == 0
    assert second["shots"] == first["shots"] and second["steps"] == first["steps"]


def test_changed_file_is_reparsed_only_for_that_file(project, monkeypatch):
    call()
    time.sleep(0.02)
    write_run_file(project, "story_planner", ok=False)
    os.utime(project, None)
    loads, tails = count(monkeypatch, "_load_json"), count(monkeypatch, "_load_tail")
    row = {s["id"]: s for s in call()["steps"]}["story_planner"]
    assert row["state"] == "failed"
    assert loads["n"] + tails["n"] <= 6


def test_refresh_drops_caches(project, monkeypatch):
    call()
    loads = count(monkeypatch, "_load_json")
    result = call(refresh=True)
    assert result["cache"]["static_hit"] is False and loads["n"] > 0


def test_no_tree_walks_or_disk_usage(project, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("обход дерева запрещён")

    for mod, name in ((os, "walk"), (shutil, "disk_usage")):
        monkeypatch.setattr(mod, name, forbidden)
    import glob
    import pathlib
    monkeypatch.setattr(glob, "glob", forbidden)
    monkeypatch.setattr(pathlib.Path, "rglob", forbidden)
    monkeypatch.setattr(pathlib.Path, "glob", forbidden)
    assert call()["status"] == "success"
    assert call(refresh=True)["status"] == "success"


def test_warm_call_stat_ceiling(project, monkeypatch):
    call()
    calls = {"n": 0}
    real = os.stat

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(os, "stat", counting)
    call()
    assert calls["n"] <= 200


def test_project_cache_is_lru_16(tmp_path, monkeypatch):
    root = make_env(tmp_path, monkeypatch).parent
    for number in range(20):
        (root / f"p{number}").mkdir()
        caps.invoke("project_status", {"project_id": f"p{number}"})
    assert len(status._PROJECT_CACHE) <= 16
    assert len(status._FILE_CACHE) <= 512


def test_threads_get_consistent_results(project):
    expected = call()["shots"]["total"]
    errors = []

    def work():
        try:
            for _ in range(50):
                assert call()["shots"]["total"] == expected
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_brief_free_lock_skips_db_and_hooks(project, monkeypatch):
    connects = {"n": 0}
    real = sqlite3.connect

    def counting(*a, **k):
        connects["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", counting)
    hooks = count(monkeypatch, "_hooks_data")
    brief = call("project_status_brief")
    assert brief["run"]["state"] == "idle"
    assert connects["n"] == 0 and hooks["n"] == 0


def test_db_result_is_cached_by_ttl(project, tmp_path, monkeypatch):
    make_db(tmp_path / "workflow_state.db", [
        {"workflow_id": "sbm_demo_ab12cd34", "status": "failed", "current_step": "story_planner"}])
    call()
    connects = {"n": 0}
    real = sqlite3.connect

    def counting(*a, **k):
        connects["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", counting)
    call()
    assert connects["n"] == 0
    with hold_lock(project):
        call()  # при занятом замке TTL короче, но свежий кэш всё ещё годится
    assert connects["n"] <= 1
