"""Ф7: журнал, recover_pending, protect_before_run, область картинок, намерение старта."""

import json

from custom_tools.storybook.studio import regeneration as rg
from custom_tools.storybook.studio import shot_versions as sv
from tests.test_storybook_studio_review_core import PID, call, prep, fin, project, under_lock  # noqa: F401
from custom_tools.storybook.studio import review as rv

S11 = "97_shots/scene_01_shot_01/img_final_start_01_01.png"
S23 = "97_shots/scene_02_shot_03/img_final_start_02_03.png"


def frame(scene, shot, phase, out, **extra):
    return {"scene_number": scene, "shot_number": shot, "shot_type": phase, "output_path": out, **extra}


def test_recover_after_crash_between_two_ops(project):
    res = prep(["1-1", "2-3"])
    journal = rg.read_journal(PID)
    assert len(journal["ops"]) == 2
    op2 = journal["ops"][1]
    sv.rollback_op(PID, op2)  # перенос второго шота не успел: файлы на месте
    (project / op2["version_rel"]).mkdir(parents=True, exist_ok=True)  # и остался пустой каталог версии
    (project / S23).write_bytes(b"S23")
    out = rg.recover_pending(PID)
    assert out["recovered"]
    assert (project / S11).read_bytes() == b"S11"  # первый откатился (новой картинки нет)
    assert (project / S23).read_bytes() == b"S23"  # второй не тронут и не объявлен «заменённым»
    assert out["replaced"] == []
    assert not (project / op2["version_rel"]).exists()
    assert rg.read_journal(PID) is None
    assert res["regen_id"] == journal["regen_id"]


def test_old_file_in_place_is_not_new(project):
    res = prep(["1-1"])
    op = rg.read_journal(PID)["ops"][0]
    sv.rollback_op(PID, op)  # файл вернулся, версия пуста
    assert not rg._new_file_ready(project, op, "start")
    done = fin(res["regen_id"])
    assert done["replaced"] == [] and done["rolled_back"] == []


def test_rollback_stub_and_honest_restored(project):
    prep(["1-1"])
    op = rg.read_journal(PID)["ops"][0]
    (project / S11).write_bytes(b"")  # нулевая заглушка на месте старого файла
    back = sv.rollback_op(PID, op)
    assert back == ["start", "end"] or set(back) == {"start", "end"}
    assert (project / S11).read_bytes() == b"S11"


def test_recover_skipped_when_project_busy(project):
    prep(["1-1"])
    handle = rg.acquire_lock(PID)
    try:
        out = rg.recover_pending(PID)
    finally:
        rg.release_lock(handle)
    assert out["recovered"] is False and rg.read_journal(PID) is not None


def test_protect_before_run_copies_stale_and_finish(project, monkeypatch):
    shots = rv._mod("shots")
    monkeypatch.setattr(shots, "stale_shots_summary", lambda pid: {"keys": ["1-1"]}, raising=False)
    monkeypatch.setattr(shots, "shot_get", lambda pid, k: {"shot": {"parts": {"start": {"freshness": "stale"}}}})
    res = under_lock(rv.protect_before_run, PID, ["artist_batch_shots"], surface="react")
    assert res["status"] == "success" and res["snapshots"][0]["part"] == "start"
    assert (project / S11).read_bytes() == b"S11"  # копия: файл на месте
    vdir = project / "97_shots/_versions/1-1" / res["snapshots"][0]["version_id"]
    assert (vdir / "img_final_start_01_01.png").exists()
    (project / S11).write_bytes(b"CHANGED")  # прогон перерисовал
    done = fin(res["regen_id"])
    assert vdir.exists() and {"key": "1-1", "part": "start"} in done["replaced"]


def test_protect_without_stale_does_nothing(project, monkeypatch):
    shots = rv._mod("shots")
    monkeypatch.setattr(shots, "stale_shots_summary", lambda pid: {"keys": []}, raising=False)
    assert rv.protect_before_run(PID)["snapshots"] == []
    assert not (project / "97_shots/_versions").exists()


def test_protect_separates_video_block_from_images(project, monkeypatch):
    plan = {"units": [{"shot_keys": ["1-1", "2-3"]}]}
    (project / "97_shots/video_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    res = sv.snapshot_copy(PID, "2-3", None, "manual", dedupe=False)
    assert len(res["version_ids"]) == 2
    assert (project / "97_shots/_versions/1-1").exists() and (project / "97_shots/_versions/2-3").exists()


def test_linked_closure_and_build_items():
    frames = [frame(1, 1, "start", "/p/a_s.png"), frame(1, 1, "end", "/p/a_e.png"),
              frame(2, 3, "start", "/p/b_s.png", copy_from_previous_end=True, source_end_path="/p/a_e.png"),
              frame(2, 3, "end", "/p/b_e.png"),
              frame(3, 1, "start", "/p/c_s.png", copy_from_previous_end=True, source_end_path="/p/b_e.png"),
              frame(3, 1, "end", "/p/c_e.png")]
    both = ("start", "end")
    assert rg.linked_closure(frames, ["1-1"], both) == ["2-3"]  # end шота 2-3 не меняется, цепочка дальше не идёт
    assert rg.linked_closure(frames, ["1-1"], ("end",)) == ["2-3"]
    assert rg.linked_closure(frames, ["1-1"], ("start",)) == []
    assert rg.linked_closure(frames, ["2-3"], both) == ["3-1"]  # выбран сам 2-3: его end перерисуют
    doc = {"seed": 1, "items": []}
    built = rg.build_items(doc, frames, ["1-1"], ["2-3"], ("end",))
    assert [(f["scene_number"], f["shot_type"]) for f in built["items"]] == [(1, "end"), (2, "start")]
    assert built["scope_shot_keys"] == ["1-1", "2-3"] and built["seed"] == 1


def test_prepare_surface_comes_from_argument_only(project):
    res = under_lock(rv.prepare_regeneration, PID, ["1-1"], "images", surface="tk")
    assert rg.read_journal(PID)["surface"] == "tk"
    fin(res["regen_id"])
    prep(["2-3"])
    assert rg.read_journal(PID)["surface"] is None
    assert not hasattr(rg, "write_intent") and not (project / ".studio" / "regeneration_intent.json").exists()


def test_prepare_failure_leaves_no_orphan_dirs(project, monkeypatch):
    calls = {"n": 0}
    real = sv.plan_shot_op

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(*a, **kw)

    monkeypatch.setattr(sv, "plan_shot_op", flaky)
    try:
        prep(["1-1", "2-3"])
    except RuntimeError:
        pass
    vroot = project / "97_shots/_versions"
    assert not vroot.exists() or not any(p.is_dir() for k in vroot.iterdir() for p in k.iterdir())
