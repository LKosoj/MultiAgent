"""Ф11: действия болванки превращаются в запросы к Ф4 (подмена runs, без запуска и удалений)."""

import sys
import types
from types import SimpleNamespace

import pytest

from custom_tools.storybook.project_paths import series_scope

from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio.errors import StudioError
from tests.helpers_storybook_blockout import (
    PID, PKG, FakeRuns, build_project, fake_tools, hide_review, mod, read_json, snapshot_files, write_json)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = build_project(tmp_path / PID)
    caps.registry()
    mod("blockout").reset_caches()
    fake_tools(monkeypatch)
    runs = FakeRuns(monkeypatch)
    with series_scope(PID, "main"):
        yield SimpleNamespace(root=root, runs=runs, mp=monkeypatch)
    mod("blockout").reset_caches()


def add_review_hook(env):
    review = types.ModuleType(f"{PKG}.review")
    review.prepare_regeneration = lambda project_id, shot_keys, what: {"status": "success"}
    env.mp.setitem(sys.modules, f"{PKG}.review", review)
    mod("blockout").reset_caches()


def plan(action, **payload):
    return caps.invoke("blockout_plan", {"project_id": PID, "series_id": "main", "action": action, **payload})


def run(action, **payload):
    return caps.invoke("blockout_run", {"project_id": PID, "series_id": "main", "action": action, **payload})


def last_plan(env):
    return [c for c in env.runs.plan_calls if c.get("inputs") is not None or c["mode"] != "single_step"][-1]


def codes(result):
    return {p["code"] for p in result["problems"]}


def test_redraw_shot(env):
    r = plan("redraw_shot", shot_keys=["1-2"])
    assert r["status"] == "success" and r["can_start"] is True and r["plan_digest"] == "d1"
    call = last_plan(env)
    assert call["mode"] == "single_step" and call["step_id"] == "blockout_renderer"
    assert call["inputs"] == {"generate_blockout": True, "blockout_fps": 24, "blockout_resolution": "1280x720",
                              "blockout_scope": "scene_01_shot_02", "blockout_jobs": 0}
    assert "blockout_allowed_durations" not in call["inputs"]
    assert call["shot_keys"] is None and call["kinds"] is None
    assert r["confirm"]["required"] is False
    assert r["notes"]


def test_redraw_shot_needs_exactly_one(env):
    assert plan("redraw_shot", shot_keys=["1-1", "1-2"])["error"]["code"] == "invalid_argument"
    assert plan("redraw_shot")["error"]["code"] == "invalid_argument"
    assert plan("redraw_shot", shot_keys=["9-9"])["error"]["code"] == "not_found"
    assert plan("nope")["error"]["code"] == "invalid_argument"


def test_redraw_chain_by_id_and_by_shot(env):
    by_id = plan("redraw_chain", chain_id="sc01_ch01")
    assert last_plan(env)["inputs"]["blockout_scope"] == "chain_sc01_ch01"
    assert by_id["confirm"]["required"] is True and "sc01_ch01" in by_id["confirm"]["text"]
    plan("redraw_chain", shot_keys=["2-1"])
    assert last_plan(env)["inputs"]["blockout_scope"] == "chain_sc02_ch01"
    assert plan("redraw_chain", chain_id="zzz")["error"]["code"] == "not_found"
    assert plan("redraw_chain")["error"]["code"] == "invalid_argument"


def test_rebuild_scene(env):
    r = plan("rebuild_scene")
    call = last_plan(env)
    assert call["step_id"] == "blockout_scene_builder"
    assert call["inputs"] == {"generate_blockout": True, "blockout_fps": 24, "blockout_resolution": "1280x720"}
    assert {"rebuild_makes_stale", "rebuild_overwrites"} <= codes(r)
    assert r["confirm"]["required"] is True and r["can_start"] is True


def test_build_preview(env):
    write_json(env.root / "00_brief.json", {"blockout_preview_burnin": False})
    r = plan("build_preview")
    call = last_plan(env)
    assert call["step_id"] == "blockout_preview"
    assert call["inputs"] == {"generate_blockout": True, "blockout_preview_burnin": False}
    assert r["confirm"]["required"] is False


def test_regenerate_without_hook_is_unavailable_and_deletes_nothing(env):
    hide_review(env.mp)
    before = snapshot_files(env.root)
    r = plan("regenerate_shots", shot_keys=["1-1"])
    assert r["can_start"] is False and "regeneration_unavailable" in codes(r)
    err = run("regenerate_shots", shot_keys=["1-1"], expected_plan_digest="d1")
    assert err["error"]["code"] == "conflict" and env.runs.start_calls == []
    assert snapshot_files(env.root) == before


def test_regenerate_with_hook(env):
    add_review_hook(env)
    before = snapshot_files(env.root)
    env.runs.estimate = {"total_known_rub": 12.5, "complete": True}
    r = plan("regenerate_shots", shot_keys=["1-1", "1-2", "1-1"])
    assert r["can_start"] is True
    call = last_plan(env)
    assert call["mode"] == "regenerate_shots" and call["shot_keys"] == ["1-1", "1-2"]
    assert call["kinds"] == ["images"] and call["rebuild_film"] is False and call["inputs"] is None
    assert r["confirm"]["required"] is True and r["confirm"]["cost_rub"] == 12.5
    assert "12.50" in r["confirm"]["text"] and "версии шота" in r["confirm"]["text"]
    assert snapshot_files(env.root) == before
    assert plan("regenerate_shots")["error"]["code"] == "invalid_argument"
    assert plan("regenerate_shots", shot_keys=["7-7"])["error"]["code"] == "not_found"


def test_regenerate_unknown_price_and_warnings(env):
    add_review_hook(env)
    write_json(env.root / "00_brief.json", {"blockout_fps": 30, "blockout_use_as_image_reference": False})
    r = plan("regenerate_shots", shot_keys=["1-1"])
    assert "цена неизвестна" in r["confirm"]["text"].lower()
    assert {"blockout_not_current", "no_image_reference"} <= codes(r)
    assert r["can_start"] is True  # предупреждения не блокируют


def test_run_passes_request_and_digest(env):
    r = run("redraw_shot", shot_keys=["1-1"], launcher="local", expected_plan_digest="d1", discard_unfinished=True)
    assert r["status"] == "success" and r["run_id"] == "r1"
    call = env.runs.start_calls[0]
    assert call["mode"] == "single_step" and call["step_id"] == "blockout_renderer"
    assert call["launcher"] == "local" and call["expected_plan_digest"] == "d1" and call["discard_unfinished"] is True
    assert call["inputs"]["blockout_scope"] == "scene_01_shot_01"
    assert "blockout_allowed_durations" not in call["inputs"]


def test_run_propagates_runs_errors(env):
    def locked(project_id, mode, **kw):
        raise StudioError("locked", "занято")

    env.mp.setattr(mod("runs"), "run_start", locked)
    assert run("rebuild_scene", expected_plan_digest="d1")["error"]["code"] == "locked"
    env.mp.setattr(mod("runs"), "run_start", lambda *a, **k: (_ for _ in ()).throw(StudioError("conflict", "план изменился")))
    assert run("rebuild_scene", expected_plan_digest="old")["error"]["code"] == "conflict"


def test_blockers_stop_before_runs(env):
    fake_tools(env.mp, blender=False)
    mod("blockout").reset_caches()
    r = plan("redraw_shot", shot_keys=["1-1"])
    assert r["can_start"] is False and "blender_unavailable" in codes(r)
    err = run("redraw_shot", shot_keys=["1-1"])
    assert err["error"]["code"] == "conflict" and env.runs.start_calls == []


def test_duration_mismatch_and_fps_changed(env):
    doc = read_json(env.root / "97_shots" / "shots.json")
    for item in doc["items"]:
        if (item["scene_number"], item["shot_number"], item["shot_type"]) == (1, 1, "start"):
            item["duration_s"] = 10
    write_json(env.root / "97_shots" / "shots.json", doc)
    r = plan("redraw_shot", shot_keys=["1-1"])
    assert "duration_mismatch" in codes(r) and r["can_start"] is False
    assert "duration_mismatch" in codes(plan("redraw_chain", chain_id="sc01_ch01"))
    assert plan("redraw_shot", shot_keys=["2-1"])["can_start"] is True
    write_json(env.root / "00_brief.json", {"blockout_fps": 30})
    r = plan("redraw_shot", shot_keys=["2-1"])
    assert "fps_changed" in codes(r) and r["can_start"] is False


def test_runs_problems_are_merged(env):
    env.runs.codes = ["no_checkpoint"]
    r = plan("rebuild_scene")
    assert r["can_start"] is False and "no_checkpoint" in codes(r)


def test_blockout_disabled_gets_notice(env):
    write_json(env.root / "00_brief.json", {"generate_blockout": False})
    r = plan("redraw_shot", shot_keys=["1-1"])
    assert "blockout_disabled" in codes(r) and last_plan(env)["inputs"]["generate_blockout"] is True


@pytest.mark.parametrize("action,extra", [("redraw_chain", {"chain_id": "sc01_ch01"}), ("rebuild_scene", {}),
                                          ("regenerate_shots", {"shot_keys": ["1-1"]})])
@pytest.mark.parametrize("digest", [None, "", "  "])
def test_confirm_actions_need_plan_digest(env, action, extra, digest):
    add_review_hook(env)
    r = run(action, expected_plan_digest=digest, **extra)
    assert r["error"]["code"] == "invalid_argument" and env.runs.start_calls == []
    assert run(action, expected_plan_digest="d1", **extra)["status"] == "success"


def test_plain_actions_do_not_need_digest(env):
    assert run("redraw_shot", shot_keys=["1-1"])["status"] == "success"
    assert run("build_preview")["status"] == "success"
