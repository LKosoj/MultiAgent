"""Ф7: защита путей в meta.json, повреждённое ревью, заметки, сравнение, аргументы старта."""

import importlib
import inspect
import json

import pytest

from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio import regeneration as rg
from custom_tools.storybook.studio import shot_versions as sv
from tests.test_storybook_studio_review_core import PID, bad, call, fin, prep, project  # noqa: F401

S11 = "97_shots/scene_01_shot_01/img_final_start_01_01.png"


def make_version(project):
    res = prep(["1-1"])
    (project / S11).write_bytes(b"NEW")
    fin(res["regen_id"])
    vid = call("versions_list", key="1-1")["versions"][0]["id"]
    return vid, project / "97_shots/_versions/1-1" / vid


def tamper(vdir, part, **changes):
    meta = json.loads((vdir / "meta.json").read_text())
    meta["parts"][part].update(changes)
    (vdir / "meta.json").write_text(json.dumps(meta))


@pytest.mark.parametrize("change", [
    {"origin": "../../00_brief.json"},
    {"origin": "00_brief.json"},
    {"origin": "97_shots/_versions/1-1/x/img_final_start_01_01.png"},
    {"origin": "/etc/passwd"},
    {"origin": "97_shots/scene_01_shot_01/other.png"},   # имя не совпало с primary
    {"primary": "../img_final_start_01_01.png"},
    {"primary": "a/b.png"},
    {"files": ["../x.png"]},
])
def test_tampered_meta_rejected(project, change):
    vid, vdir = make_version(project)
    tamper(vdir, "start", **change)
    brief = project / "00_brief.json"
    before = brief.read_bytes() if brief.exists() else None
    err = bad("versions_restore", key="1-1", version_id=vid, parts=["start"])
    assert err["code"] == "conflict" and err["details"]["reason"] == "version_corrupt"
    assert (project / S11).read_bytes() == b"NEW"
    assert (brief.read_bytes() if brief.exists() else None) == before


def test_restore_order_sidecar(project):
    (project / S11).with_suffix(".continuity.json").write_text("old-side")
    res = prep(["1-1"])
    (project / S11).write_bytes(b"NEW")
    (project / S11).with_suffix(".continuity.json").write_text("new-side")
    fin(res["regen_id"])
    vid = call("versions_list", key="1-1")["versions"][0]["id"]
    call("versions_restore", key="1-1", version_id=vid, parts=["start"])
    assert (project / S11).read_bytes() == b"S11"
    assert (project / S11).with_suffix(".continuity.json").read_text() == "old-side"


def test_restore_without_stored_sidecar_removes_foreign_one(project):
    res = prep(["1-1"])
    (project / S11).write_bytes(b"NEW")
    (project / S11).with_suffix(".continuity.json").write_text("foreign")
    fin(res["regen_id"])
    vid = call("versions_list", key="1-1")["versions"][0]["id"]
    call("versions_restore", key="1-1", version_id=vid, parts=["start"])
    assert not (project / S11).with_suffix(".continuity.json").exists()


def test_restore_without_baseline_drops_current_record(project):
    fpm = importlib.import_module("custom_tools.storybook.studio._shots_fp")
    shots = importlib.import_module("custom_tools.storybook.studio.shots")
    vid, vdir = make_version(project)
    meta = json.loads((vdir / "meta.json").read_text())
    meta["baseline"] = {"start": None}
    (vdir / "meta.json").write_text(json.dumps(meta))
    shots.set_baseline_record(PID, "1-1", "start", {"fp": "x", "components": {}}, source="run")
    call("versions_restore", key="1-1", version_id=vid, parts=["start"])
    assert shots.get_baseline_record(PID, "1-1", "start") is None
    assert fpm  # модуль на месте


def test_restore_expected_token_conflict(project):
    vid, _ = make_version(project)
    err = bad("versions_restore", key="1-1", version_id=vid, parts=["start"], expected_token={"start": "bogus"})
    assert err["details"]["reason"] == "current_changed"
    cur = call("versions_list", key="1-1")["current"]["tokens"]["start"]
    assert call("versions_restore", key="1-1", version_id=vid, parts=["start"], expected_token={"start": cur})


def test_review_file_corrupt(project):
    call("review_set_status", keys=["1-1"], status="approved")
    path = project / "97_shots/review_state.json"
    path.write_text("{ не json", encoding="utf-8")
    assert call("review_get")["corrupt"] is True
    err = bad("review_set_status", keys=["1-1"], status="redo")
    assert err["details"]["reason"] == "review_file_corrupt"
    assert path.read_text(encoding="utf-8") == "{ не json"  # файл не затёрт


@pytest.mark.parametrize("note", ["a\x00b", "a\x07b", "x" * 2001, 5])
def test_note_validation(project, note):
    err = bad("review_set_note", key="1-1", note=note, expected_digest="j1:none")
    assert err["code"] in ("invalid_argument",)


def test_note_allows_newline_tab_and_normalizes_crlf(project):
    r = call("review_set_note", key="1-1", note="a\r\nb\tc", expected_digest="j1:none")
    assert r["entry"]["note"] == "a\nb\tc"


def test_compare_diff_and_errors(project):
    vid, _ = make_version(project)
    r = call("versions_compare", key="1-1", left=vid, right="current")
    assert r["right"]["id"] == "current" and "tokens" in r["right"]
    assert r["diff"]["size_delta"]["start"] == 3 - 3 or "start" in r["diff"]["size_delta"]
    assert bad("versions_compare", key="1-1", left=vid, right=vid)["code"] == "invalid_argument"
    assert bad("versions_compare", key="1-1", left="nope", right="current")["code"] == "not_found"


def test_versions_list_unit_block_and_usage(project):
    (project / "97_shots/video_plan.json").write_text(
        json.dumps({"units": [{"shot_keys": ["1-1", "2-3"]}]}), encoding="utf-8")
    res = prep(["2-3"], "video")
    assert res["expanded_keys"] == ["1-1", "2-3"]
    (project / "97_shots/scene_01_shot_01/video_final_01_01.mp4").write_bytes(b"NEWV")
    fin(res["regen_id"])
    assert len(call("versions_list", key="2-3")["versions"]) == 1  # блок виден из любого шота
    assert call("versions_list")["usage"]["bytes_total"] > 0


def test_media_inventory_skips_versions(tmp_path):
    from custom_tools.storybook import storybook_surface as ss
    (tmp_path / "97_shots/_versions/1-1/v").mkdir(parents=True)
    (tmp_path / "97_shots/_versions/1-1/v/a.png").write_bytes(b"x")
    (tmp_path / "97_shots/s").mkdir()
    (tmp_path / "97_shots/s/b.png").write_bytes(b"x")
    inv = ss._media_inventory(tmp_path, max_items=50)
    assert [i["name"] for i in inv["items"]] == ["b.png"] and inv["counts"]["image"] == 1


def test_start_args_match_run_start_signature(project, monkeypatch):
    runs = importlib.import_module("custom_tools.storybook.studio.runs")
    seen = {}

    def run_start(project_id, *, mode, launcher, expected_plan_digest, kinds=None, shot_keys=None,
                  rebuild_film=False, step_id=None, abandon_pending_jobs=False, image_parts=None):
        seen.update(locals())
        return {"run_id": "r", "workflow_id": "w"}

    monkeypatch.setattr(runs, "run_plan_get", lambda pid, **kw: {"plan": {
        "can_start": True, "problems": [], "steps_to_run": ["artist_batch_shots"], "plan_digest": "d"}}, raising=False)
    monkeypatch.setattr(runs, "run_start", run_start, raising=False)
    settings = importlib.import_module("custom_tools.storybook.studio.run_settings")
    monkeypatch.setattr(settings, "cost_estimate_get", lambda pid, **kw: {"total_known_rub": 0, "unknown_components": []})
    pv = call("regenerate_preview", what="images", shot_keys=["1-1"])
    call("regenerate_start", what="images", shot_keys=["1-1"], launcher="backend",
         expected_plan_digest=pv["plan_digest"], abandon_pending_jobs=False, image_parts=["end"])
    assert seen["image_parts"] == ["end"] and seen["abandon_pending_jobs"] is False
    # без поддержки image_parts полный набор кадров стартует, частичный отклоняется
    def old_start(project_id, *, mode, launcher, expected_plan_digest, kinds=None, shot_keys=None, rebuild_film=False,
                  step_id=None):
        return {"run_id": "r2", "workflow_id": "w"}
    monkeypatch.setattr(runs, "run_start", old_start, raising=False)
    out = call("regenerate_start", what="images", shot_keys=["1-1"], launcher="local",
               expected_plan_digest=pv["plan_digest"])
    assert out["run_id"] == "r2"
    err = bad("regenerate_start", what="images", shot_keys=["1-1"], launcher="local",
              expected_plan_digest=pv["plan_digest"], image_parts=["end"])
    assert err["code"] == "unavailable"


def test_single_step_snapshot_keeps_versions_without_journal(project):
    (project / "98_audio").mkdir()
    (project / "98_audio/music_manifest.json").write_text("{}")
    review = importlib.import_module("custom_tools.storybook.studio.review")
    review._snapshot_single_step(PID, "music", "react")
    assert rg.read_journal(PID) is None
    assert len(call("versions_list", kind="music")["versions"]) == 1


def test_helpers_share_one_implementation():
    review = importlib.import_module("custom_tools.storybook.studio.review")
    state = importlib.import_module("custom_tools.storybook.studio.review_state")
    assert review._mod is sv._mod and rg._optional is sv._optional and state.now_iso is sv.now_iso


def _music_setup(project, monkeypatch, run_start):
    (project / "98_audio").mkdir(exist_ok=True)
    (project / "98_audio/music_manifest.json").write_text("{}")
    runs = importlib.import_module("custom_tools.storybook.studio.runs")
    monkeypatch.setattr(runs, "run_plan_get", lambda pid, **kw: {"plan": {
        "can_start": True, "problems": [], "steps_to_run": ["storybook_music_generator"], "plan_digest": "d"}},
        raising=False)
    monkeypatch.setattr(runs, "run_start", run_start, raising=False)
    settings = importlib.import_module("custom_tools.storybook.studio.run_settings")
    monkeypatch.setattr(settings, "cost_estimate_get", lambda pid, **kw: {"total_known_rub": 0, "unknown_components": []})


def test_music_force_regenerate_passed_and_no_journal(project, monkeypatch):
    seen = {}

    def run_start(project_id, *, mode, launcher, expected_plan_digest, step_id=None, force_regenerate=False):
        seen.update(mode=mode, force=force_regenerate)
        assert rg.read_journal(PID) is None  # Ф4 не должен ждать журнала
        return {"run_id": "r", "workflow_id": "w"}

    _music_setup(project, monkeypatch, run_start)
    pv = call("regenerate_preview", what="music")
    assert not any(p["code"] == "force_regenerate_unavailable" for p in pv["problems"])
    call("regenerate_start", what="music", launcher="backend", expected_plan_digest=pv["plan_digest"])
    assert seen["force"] is True
    assert len(call("versions_list", kind="music")["versions"]) == 1


def test_music_warning_when_no_force_param(project, monkeypatch):
    def run_start(project_id, *, mode, launcher, expected_plan_digest, step_id=None):
        return {"run_id": "r", "workflow_id": "w"}

    _music_setup(project, monkeypatch, run_start)
    pv = call("regenerate_preview", what="music")
    assert any(p["code"] == "force_regenerate_unavailable" for p in pv["problems"])
    call("regenerate_start", what="music", launcher="backend", expected_plan_digest=pv["plan_digest"])
    assert rg.read_journal(PID) is None


def _submitted_job(project):
    (project / "97_shots/provider_jobs.json").write_text(json.dumps({"version": 2, "jobs": [{
        "provider": "aitunnel", "shot_key": "1-1", "status": "submitted", "task_id": "T", "input_hash": "h",
        "output_path": "97_shots/scene_01_shot_01/video_final_01_01.mp4"}]}), encoding="utf-8")


def test_stale_intent_file_has_no_effect(project):
    from tests.test_storybook_studio_review_core import under_lock
    review = importlib.import_module("custom_tools.storybook.studio.review")
    _submitted_job(project)
    studio = project / ".studio"
    studio.mkdir(exist_ok=True)
    (studio / "regeneration_intent.json").write_text(json.dumps({
        "what": "video", "keys": ["1-1"], "abandon_pending_jobs": True, "image_parts": ["end"], "nonce": "n",
        "at": 9999999999}), encoding="utf-8")
    res = under_lock(review.prepare_regeneration, PID, ["1-1"], "video", abandon_pending_jobs=False)
    assert res["status"] == "error" and res["error"]["details"]["reason"] == "paid_job_pending"
    job = json.loads((project / "97_shots/provider_jobs.json").read_text())["jobs"][0]
    assert job["status"] == "submitted"
    res = under_lock(review.prepare_regeneration, PID, ["1-1"], "images")  # область картинок из файла не берётся
    assert res["status"] == "success"
    assert len(res["snapshots"]) == 2  # start и end, а не только end


def test_start_abandon_requires_run_start_support(project, monkeypatch):
    runs = importlib.import_module("custom_tools.storybook.studio.runs")
    _submitted_job(project)
    monkeypatch.setattr(runs, "run_plan_get", lambda pid, **kw: {"plan": {
        "can_start": True, "problems": [], "steps_to_run": ["video_generator"], "plan_digest": "d"}}, raising=False)
    monkeypatch.setattr(runs, "run_start", lambda project_id, *, mode, launcher, expected_plan_digest, kinds=None,
                        shot_keys=None, rebuild_film=False: {"run_id": "r"}, raising=False)
    settings = importlib.import_module("custom_tools.storybook.studio.run_settings")
    monkeypatch.setattr(settings, "cost_estimate_get", lambda pid, **kw: {"total_known_rub": 0, "unknown_components": []})
    pv = call("regenerate_preview", what="video", shot_keys=["1-1"])
    err = bad("regenerate_start", what="video", shot_keys=["1-1"], launcher="backend",
              expected_plan_digest=pv["plan_digest"], abandon_pending_jobs=True)
    assert err["code"] == "unavailable"


def test_restore_midfail_rolls_back_without_keyerror(project, monkeypatch):
    d = project / "97_shots/scene_01_shot_01"
    res = prep(["1-1"])
    for n in ("img_final_start_01_01.png", "img_final_end_01_01.png"):
        (d / n).write_bytes(b"NEW")
    fin(res["regen_id"])
    vid = call("versions_list", key="1-1")["versions"][0]["id"]
    (d / "img_final_end_01_01.png").unlink()  # у текущего шота нет end: в снимке этой части нет
    orig = sv._copy_atomic

    def boom(src, dst):
        if dst.name == "img_final_end_01_01.png":
            raise OSError("disk")
        return orig(src, dst)

    monkeypatch.setattr(sv, "_copy_atomic", boom)
    err = bad("versions_restore", key="1-1", version_id=vid)
    assert err["code"] == "unavailable" and err["details"]["part"] == "end" and "end" in err["message"]
    assert (d / "img_final_start_01_01.png").read_bytes() == b"NEW"  # start возвращён из снимка


@pytest.mark.parametrize("mode", ["raise", "error_dict"])
def test_music_snapshot_removed_when_run_start_fails(project, monkeypatch, mode):
    from custom_tools.storybook.studio.errors import StudioError

    def run_start(project_id, *, mode_, launcher, expected_plan_digest, step_id=None, **kw):
        raise AssertionError

    def failing(project_id, **kw):
        if mode == "raise":
            raise StudioError("locked", "Проект занят прогоном")
        return {"status": "error", "error": {"code": "conflict", "message": "plan_blocked"}}

    _music_setup(project, monkeypatch, failing)
    pv = call("regenerate_preview", what="music")
    err = bad("regenerate_start", what="music", launcher="backend", expected_plan_digest=pv["plan_digest"])
    assert err["code"] in ("locked", "conflict")
    assert call("versions_list", kind="music")["versions"] == []
    assert (project / "98_audio/music_manifest.json").exists()  # оригинал на месте
    assert rg.read_journal(PID) is None


def test_music_success_keeps_snapshot_and_no_regen_pending(project, monkeypatch):
    _music_setup(project, monkeypatch, lambda project_id, **kw: {"run_id": "r", "workflow_id": "w"})
    pv = call("regenerate_preview", what="music")
    out = call("regenerate_start", what="music", launcher="backend", expected_plan_digest=pv["plan_digest"])
    assert out["regen_pending"] is False
    assert len(call("versions_list", kind="music")["versions"]) == 1
