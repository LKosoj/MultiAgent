"""Ф4 + Ф7: хуки перегенерации вызываются с настоящим studio.review, движок поддельный."""

import asyncio
import importlib

import pytest

from custom_tools.storybook.studio import run_exec
from custom_tools.storybook.studio.errors import StudioError
from tests.studio_run_fakes import FakeEngine, make_plan
from tests.test_storybook_studio_shots import PID, build


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = tmp_path / PID
    root.mkdir()
    build(root)
    importlib.import_module("custom_tools.storybook.studio.shots")._CACHE.clear()
    return root


def rg():
    return importlib.import_module("custom_tools.storybook.studio.regeneration")


def test_regenerate_shots_end_to_end(project, tmp_path):
    start = project / "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    plan = make_plan(PID, ["brief_from_prompt"], mode="regenerate_shots",
                     scope={"shot_keys": ["1-1"], "kinds": ["images"]})
    engine = FakeEngine(tmp_path, on_step=lambda step, ctx: start.write_bytes(b"NEW"))
    resp = asyncio.run(run_exec.execute_plan(engine, plan, run_id="r-f7"))
    assert resp["status"] == "success", resp
    assert start.read_bytes() == b"NEW"
    assert list((project / "97_shots/_versions/1-1").iterdir())
    assert not rg().has_live_journal(PID)


def test_regenerate_shots_hook_error_is_clear(project, monkeypatch):
    review = importlib.import_module("custom_tools.storybook.studio.review")
    monkeypatch.setattr(review, "prepare_regeneration",
                        lambda *a, **k: {"status": "error", "code": "locked", "message": "занято"})
    plan = make_plan(PID, ["brief_from_prompt"], mode="regenerate_shots",
                     scope={"shot_keys": ["1-1"], "kinds": ["images"]})
    with pytest.raises(StudioError) as exc:
        run_exec._prepare(plan, PID)
    assert exc.value.code == "locked"


def test_protect_keeps_regen_id_and_hook_exception_is_warning(project, monkeypatch):
    review = importlib.import_module("custom_tools.storybook.studio.review")
    monkeypatch.setattr(review, "protect_before_run", lambda pid, steps, **k: {"status": "success", "regen_id": "R1"})
    step = sorted(run_exec.PROTECT_STEPS)[0]
    assert run_exec._prepare(make_plan(PID, [step]), PID) == {"regen_id": "R1"}

    def boom(pid, steps, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(review, "protect_before_run", boom)
    assert run_exec._prepare(make_plan(PID, [step]), PID) == {}


def test_single_step_music_force_regenerate_and_no_hooks(project, monkeypatch):
    review = importlib.import_module("custom_tools.storybook.studio.review")
    calls = []
    monkeypatch.setattr(review, "prepare_regeneration", lambda *a, **k: calls.append(a) or {"status": "success"})
    plan = make_plan(PID, ["storybook_music_generator"], mode="single_step", scope={"shot_keys": [], "kinds": [],
                                                                                    "force_regenerate": True})
    assert run_exec._prepare(plan, PID, "react") == {} and calls == []
    assert plan["step_params"]["storybook_music_generator"]["force_regenerate"] is True
    plan = make_plan(PID, ["storybook_music_generator"], mode="single_step")
    run_exec._prepare(plan, PID, "react")
    assert not plan["step_params"]
    plan = make_plan(PID, ["montage_assembler"], mode="single_step")
    assert run_exec._prepare(plan, PID, "tk") == {} and calls == []


def test_surface_reaches_hooks(project, monkeypatch):
    review = importlib.import_module("custom_tools.storybook.studio.review")
    seen = {}
    monkeypatch.setattr(review, "prepare_regeneration",
                        lambda pid, keys, what, **k: seen.update(prep=k.get("surface")) or {"status": "success"})
    monkeypatch.setattr(review, "protect_before_run",
                        lambda pid, steps, **k: seen.update(prot=k.get("surface")) or {"status": "success"})
    run_exec._prepare(make_plan(PID, ["x"], mode="regenerate_shots",
                                scope={"shot_keys": ["1-1"], "kinds": ["images"]}), PID, "tk")
    run_exec._prepare(make_plan(PID, ["video_generator"]), PID, "tk")
    assert seen == {"prep": "tk", "prot": "tk"}


def test_regenerate_passes_abandon_and_parts(project, monkeypatch):
    review = importlib.import_module("custom_tools.storybook.studio.review")
    seen = {}
    monkeypatch.setattr(review, "prepare_regeneration",
                        lambda pid, keys, what, **k: seen.update(k) or {"status": "success", "regen_id": "R3"})
    plan = make_plan(PID, ["brief_from_prompt"], mode="regenerate_shots",
                     scope={"shot_keys": ["1-1"], "kinds": ["images"], "abandon_pending_jobs": True,
                            "image_parts": ["start"]})
    run_exec._prepare(plan, PID)
    assert seen["abandon_pending_jobs"] is True and seen["image_parts"] == ("start",)


def test_run_start_signature():
    import inspect
    from custom_tools.storybook.studio import runs
    params = inspect.signature(runs.run_start).parameters
    assert params["abandon_pending_jobs"].default is False and params["image_parts"].default is None
    assert params["force_regenerate"].default is False
