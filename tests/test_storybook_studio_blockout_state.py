"""Ф11: состояние и подробности болванки (без Blender, сети и платных вызовов)."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_tools.storybook.studio import _blockout_state as S
from custom_tools.storybook.studio import capabilities as caps
from custom_tools.storybook.studio.errors import StudioError
from tests.helpers_storybook_blockout import (
    PID, FakeRuns, add_final_images, build_project, fake_tools, hide_review, manifest_for, mod, read_json, write_json)

RENDERED = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    root = build_project(tmp_path / PID)
    caps.registry()
    mod("blockout").reset_caches()
    fake_tools(monkeypatch)
    runs = FakeRuns(monkeypatch)
    yield SimpleNamespace(root=root, runs=runs, mp=monkeypatch)
    mod("blockout").reset_caches()


def state():
    result = caps.invoke("blockout_state", {"project_id": PID})
    assert result["status"] == "success", result
    return result


def rows(result):
    return {r["key"]: r for r in result["shots"]}


def manifest_path(env, scene=1, shot=1):
    return env.root / "93_blockout" / S.shot_dir_name(scene, shot) / "manifest.json"


def test_fresh_project(env):
    r = state()
    assert r["built"] and r["empty_reason"] is None and r["enabled"] is True
    assert r["summary"] == {"total": 3, "rendered": 3, "stale": 0, "missing": 0, "junction_error": 0, "p10": 0}
    assert [s["scene_number"] for s in r["scenes"]] == [1, 2]
    assert r["scenes"][0]["chains"][0]["shot_keys"] == ["1-1", "1-2"]
    assert r["scenes"][0]["chains"][0]["blend_path"] == "93_blockout/sc01_ch01/chain.blend"
    assert r["settings"]["source"] == "brief" and r["settings"]["scene_spec_fps"] == 24
    assert rows(r)["1-1"]["state_title"] == "готова"
    assert set(rows(r)["1-1"]["allowed"]) >= {"redraw_shot", "redraw_chain", "regenerate_shots"}
    assert state()["digest"] == r["digest"]


def test_state_table(env):
    def edit(scene, shot, **over):
        data = read_json(manifest_path(env, scene, shot))
        data.update(over)
        write_json(manifest_path(env, scene, shot), data)

    manifest_path(env, 1, 1).unlink()
    edit(1, 2, fps=30)
    edit(2, 1, spec_hash="sha256:other")
    got = rows(state())
    assert got["1-1"]["state"] == "missing" and got["1-1"]["state_title"] == "отсутствует"
    assert got["1-2"]["state"] == "stale"
    assert got["2-1"]["state"] == "stale"
    data = read_json(manifest_path(env, 2, 1))
    data["spec_hash"] = manifest_for("sc02_ch01")["spec_hash"]
    del data["resolution"]
    write_json(manifest_path(env, 2, 1), data)
    assert rows(state())["2-1"]["state"] == "stale"  # неизвестное разрешение -> устарела
    edit(2, 1, resolution=[1280, 720], junction_with_next={"status": "failed"})
    assert rows(state())["2-1"]["state"] == "junction_error"


def test_duration_mismatch_blocks_redraw(env):
    doc = read_json(env.root / "97_shots" / "shots.json")
    for item in doc["items"]:
        if (item["scene_number"], item["shot_number"], item["shot_type"]) == (1, 1, "start"):
            item["duration_s"] = 10
    write_json(env.root / "97_shots" / "shots.json", doc)
    row = rows(state())["1-1"]
    assert row["state"] == "stale" and row["duration_mismatch"] is True
    assert row["scene_spec_duration_s"] == 5 and row["duration_s"] == 10
    assert "redraw_shot" not in row["allowed"] and "redraw_chain" not in row["allowed"]


def test_p10(env):
    add_final_images(env.root, 1, 1, RENDERED - 100)
    add_final_images(env.root, 1, 2, RENDERED + 100)
    got = rows(state())
    assert got["1-1"]["p10"] is True and "leave_as_is" in got["1-1"]["allowed"]
    assert got["1-2"]["p10"] is False and "leave_as_is" not in got["1-2"]["allowed"]
    data = read_json(manifest_path(env))
    data["p10_acknowledged"] = "2026-11-02T00:00:00Z"
    write_json(manifest_path(env), data)
    assert rows(state())["1-1"]["p10"] is False


def test_shot_without_chain(env):
    doc = read_json(env.root / "97_shots" / "shots.json")
    doc["items"] += [{"scene_number": 3, "shot_number": 1, "shot_type": "start", "duration_s": 5}]
    write_json(env.root / "97_shots" / "shots.json", doc)
    r = state()
    row = rows(r)["3-1"]
    assert row["chain_id"] is None and row["state"] == "missing" and "redraw_shot" not in row["allowed"]
    assert r["scenes"][-1]["unchained_shot_keys"] == ["3-1"]


@pytest.mark.parametrize("rel", ["97_shots/shots.json", "93_blockout/chains.json", "93_blockout/scene_spec.json",
                                 "93_blockout/scene_01_shot_01/manifest.json", "00_brief.json"])
def test_broken_json_is_warning(env, rel):
    (env.root / rel).write_text("{not json", encoding="utf-8")
    r = state()
    assert any(w["code"] == "file_broken" and w["file"] == rel for w in r["warnings"])


def test_empty_states(env, tmp_path):
    build_project(tmp_path / "bare", built=False)
    assert caps.invoke("blockout_state", {"project_id": "bare"})["empty_reason"] == "not_built"
    build_project(tmp_path / "noshots", shots=False, built=False)
    r = caps.invoke("blockout_state", {"project_id": "noshots"})
    assert r["empty_reason"] == "no_shots" and r["shots"] == []
    assert caps.invoke("blockout_state", {"project_id": "nope"})["error"]["code"] == "not_found"


def test_duration_sources(env):
    assert state()["durations"] == {"allowed": [], "source": "none"}
    write_json(env.root / "97_shots" / "video_model_caps.json", {"supported_durations": [4, 8]})
    assert state()["durations"] == {"allowed": [4, 8], "source": "video_model_caps"}
    write_json(env.root / "00_brief.json", {"blockout_allowed_durations": [5, 10]})
    assert state()["durations"] == {"allowed": [5, 10], "source": "brief"}
    write_json(env.root / ".studio" / "run_settings.json", {"version": 1, "video": {"allowed_durations": [6]}})
    assert state()["durations"] == {"allowed": [6], "source": "run_settings"}


def blockers(r, action):
    return [b["code"] for b in r["actions"][action]["blockers"]]


def test_blockers_tools(env):
    fake_tools(env.mp, blender=False, ffmpeg=False)
    mod("blockout").reset_caches()
    r = state()
    assert r["tools"]["blender"]["available"] is False and r["tools"]["ffmpeg"]["available"] is False
    assert "blender_unavailable" in blockers(r, "redraw_shot")
    assert "blender_unavailable" in blockers(r, "rebuild_scene")
    assert blockers(r, "build_preview") == ["ffmpeg_unavailable"]
    assert "blender_unavailable" not in blockers(r, "regenerate_shots")


def test_blockers_run_and_checkpoint(env):
    env.runs.info.update(active=True, current_steps=["video_generator"])
    r = state()
    assert "run_active" in blockers(r, "redraw_shot") and "run_active" in blockers(r, "leave_as_is")
    assert r["run"]["active"] is True and r["run"]["current_steps"] == ["video_generator"]
    env.runs.info.update(active=False)
    env.runs.codes = ["no_checkpoint"]
    mod("blockout").reset_caches()
    r = state()
    assert "no_checkpoint" in blockers(r, "redraw_shot") and "no_checkpoint" in blockers(r, "set_duration")
    assert "no_checkpoint" not in blockers(r, "regenerate_shots")
    assert r["checkpoint"] == {"known": True, "present": False}


def test_checkpoint_unknown_when_runs_fail(env):
    def boom(*a, **k):
        raise RuntimeError("db")

    env.mp.setattr(mod("runs"), "run_plan_get", boom)
    mod("blockout").reset_caches()
    r = state()
    assert r["checkpoint"] == {"known": False, "present": None}
    assert "checkpoint_unknown" in blockers(r, "redraw_shot")


def test_probe_is_cached(env):
    state()
    state()
    assert len(env.runs.probe_calls) == 1


def test_fps_changed_and_nothing_rendered(env):
    write_json(env.root / "00_brief.json", {"blockout_fps": 30})
    r = state()
    assert "fps_changed" in blockers(r, "redraw_shot") and "fps_changed" not in blockers(r, "build_preview")
    assert r["summary"]["stale"] == 3
    for p in env.root.glob("93_blockout/scene_*/manifest.json"):
        p.unlink()
    assert "nothing_rendered" in blockers(state(), "build_preview")


def test_enabled_actions_when_all_ok(env):
    hide_review(env.mp)
    r = state()
    for name in ("redraw_shot", "redraw_chain", "rebuild_scene", "build_preview"):
        assert r["actions"][name]["enabled"] is True, name
    assert r["actions"]["regenerate_shots"]["enabled"] is False  # нет хука Ф7
    assert blockers(r, "regenerate_shots") == ["regeneration_unavailable"]
    assert blockers(r, "set_duration") == ["no_durations"]


def test_screenplay_step_blocks_duration(env):
    write_json(env.root / "00_brief.json", {"blockout_allowed_durations": [5, 10]})
    env.runs.info.update(active=True, current_steps=["screenplay_shots_generator"])
    assert "screenplay_running" in blockers(state(), "set_duration")
    env.runs.info.update(current_steps=None)
    assert "screenplay_running" in blockers(state(), "set_duration")


def test_cache_follows_file_change(env):
    assert rows(state())["1-1"]["state"] == "rendered"
    path = manifest_path(env)
    data = read_json(path)
    data["fps"] = 99
    write_json(path, data)
    assert rows(state())["1-1"]["state"] == "stale"


def test_defaults_match_pipeline_yaml():
    inputs = mod("status")._load_pipeline()["inputs"]
    for name, value in mod("blockout").DEFAULTS.items():
        assert inputs[name] == value, name


def test_settings_to_inputs(env):
    b = mod("blockout")
    assert b.settings_to_inputs(PID) == {"blockout_fps": 24, "blockout_resolution": "1280x720"}
    write_json(env.root / "00_brief.json", {"generate_blockout": False, "blockout_jobs": 2, "blockout_preview_burnin": False,
                                            "pages_min": 5})
    assert b.settings_to_inputs(PID) == {"generate_blockout": False, "blockout_jobs": 2, "blockout_preview_burnin": False}
    write_json(env.root / "00_brief.json", {"blockout_fps": "fast"})
    with pytest.raises(StudioError):
        b.settings_to_inputs(PID)
    r = state()  # в состоянии плохое значение - предупреждение и запасное значение
    assert r["settings"]["fps"] == 24 and any(w["code"] == "brief_value_invalid" for w in r["warnings"])


def test_settings_hook_is_used_by_run_plan(env):
    """Ф4 берёт настройки болванки из брифа через settings_to_inputs."""
    write_json(env.root / "00_brief.json", {"blockout_jobs": 3})
    layers = mod("run_plan")._inputs_layers(PID, {"blockout_jobs": 0}, {}, {}, {}, [])[0]
    assert layers["blockout_jobs"] == 3


# --- подробности ---

def detail(**payload):
    return caps.invoke("blockout_detail", {"project_id": PID, **payload})


def test_detail_shot(env):
    add_final_images(env.root, 1, 1, RENDERED + 5)
    shot_dir = env.root / "93_blockout" / "scene_01_shot_01"
    (shot_dir / "ref_start.png").write_bytes(b"x")
    (shot_dir / "blockout_ref.mp4").write_bytes(b"x")
    data = read_json(manifest_path(env))
    data["warnings"] = ["мало света"]
    write_json(manifest_path(env), data)
    write_json(env.root / "00_brief.json", {"blockout_allowed_durations": [5, 10]})
    r = detail(shot_key="1-1")
    assert r["state"] == "rendered" and r["chain_id"] == "sc01_ch01"
    assert r["images"]["blockout_start"]["path"] == "93_blockout/scene_01_shot_01/ref_start.png"
    assert r["images"]["blockout_start"]["servable"] is True
    assert r["images"]["blockout_end"] is None
    assert r["images"]["image_start"]["path"] == "97_shots/scene_01_shot_01/img_final_start_01_01.png"
    assert r["videos"]["blockout_ref"]["path"].endswith("blockout_ref.mp4") and r["videos"]["final"] is None
    assert r["manifest"]["fps"] == 24 and "spec_hash" not in r["manifest"]
    assert [n["message"] for n in r["notes"]] == ["мало света"]
    assert r["duration"]["current_s"] == 5 and r["duration"]["allowed"] == [5, 10]
    assert r["duration"]["editable"] is True
    assert r["digest"]
    assert "ref_start.png" not in str(r["manifest"])


def test_detail_notes_from_report_and_chain(env):
    write_json(env.root / "93_blockout" / "report.json", {"scene_builder": {"status": "ok", "checks": [
        {"code": "B99", "level": "warning", "message": "камера дрожит", "scene_number": 1, "shot_number": 2},
        {"code": "B98", "level": "warning", "message": "цепочка длинная", "chain_id": "sc01_ch01"},
        {"code": "B97", "level": "warning", "message": "чужая", "scene_number": 2, "shot_number": 1}]}})
    mod("blockout").reset_caches()
    shot = detail(shot_key="1-2")
    assert "камера дрожит" in [n["message"] for n in shot["notes"]]
    assert "чужая" not in [n["message"] for n in shot["notes"]]
    chain = detail(chain_id="sc01_ch01")
    assert chain["shot_keys"] == ["1-1", "1-2"] and chain["blend_exists"] is False
    texts = [n["message"] for n in chain["notes"]]
    assert "камера дрожит" in texts and "цепочка длинная" in texts and "чужая" not in texts


def test_detail_errors(env):
    assert detail()["error"]["code"] == "invalid_argument"
    assert detail(shot_key="1-1", chain_id="sc01_ch01")["error"]["code"] == "invalid_argument"
    assert detail(shot_key="9-9")["error"]["code"] == "not_found"
    assert detail(shot_key="abc")["error"]["code"] == "invalid_argument"
    assert detail(chain_id="zzz")["error"]["code"] == "not_found"


def test_detail_duration_not_editable_without_checkpoint(env):
    write_json(env.root / "00_brief.json", {"blockout_allowed_durations": [5]})
    env.runs.codes = ["no_checkpoint"]
    mod("blockout").reset_caches()
    d = detail(shot_key="1-1")["duration"]
    assert d["editable"] is False and d["reason"] == "no_checkpoint"


def test_read_json_same_size_rewrites_are_seen(tmp_path):
    path = tmp_path / "a.json"
    for text in ('{"v": 1}', '{"v": 2}', '{"v": 3}'):
        path.write_text(text, encoding="utf-8")
        assert S.read_json(path) == ({"v": int(text[-2])}, "ok")


def test_read_json_caches_only_old_files(tmp_path, monkeypatch):
    import os
    path = tmp_path / "a.json"
    path.write_text('{"v": 1}', encoding="utf-8")
    assert S.read_json(path)[0] == {"v": 1}
    assert str(path) not in S._CACHE  # свежий файл не кэшируется
    old = path.stat().st_mtime - 60
    os.utime(path, (old, old))
    S.read_json(path)
    assert str(path) in S._CACHE
    sig = S._CACHE[str(path)][0]
    assert len(sig) == 3 and sig[2] == path.stat().st_ino  # inode входит в подпись


def test_chains_json_wrong_shape_is_skipped_with_warning(env):
    chains = read_json(env.root / "93_blockout" / "chains.json")
    good = chains["chains"][0]
    bad = [
        {"chain_id": ["x"], "scene_number": 1, "shots": []},
        {"chain_id": "../evil", "scene_number": 1, "shots": []},
        {"chain_id": "ok", "scene_number": 1, "shots": "nope"},
        {"chain_id": "ok2", "scene_number": 1, "shots": [{"shot_number": [1]}]},
        "junk",
    ]
    write_json(env.root / "93_blockout" / "chains.json", {**chains, "chains": [good] + bad})
    r = state()
    assert any(w["code"] == "file_broken" and w["file"] == "93_blockout/chains.json" for w in r["warnings"])
    ids = [c["chain_id"] for s in r["scenes"] for c in s["chains"]]
    assert "../evil" not in ids and good["chain_id"] in ids
    write_json(env.root / "93_blockout" / "chains.json", {**chains, "chains": "nope"})
    r = state()
    assert any(w["code"] == "file_broken" for w in r["warnings"])
    write_json(env.root / "93_blockout" / "scene_spec.json", {"chains": [{"chain_id": ["x"]}, 5]})
    assert state()["status"] == "success"
    write_json(env.root / "93_blockout" / "scene_spec.json", {"chains": "nope"})
    assert any(w["code"] == "file_broken" and w["file"] == "93_blockout/scene_spec.json" for w in state()["warnings"])


def test_parse_key_rejects_non_ascii_digits(env):
    assert S.parse_key("²-1") is None and S.parse_key("١-1") is None
    assert S.parse_key("2-3") == (2, 3) and S.parse_key("02-03") == (2, 3)
    r = caps.invoke("blockout_detail", {"project_id": PID, "shot_key": "²-1"})
    assert r["error"]["code"] == "invalid_argument"


def test_symlink_outside_project_gives_none(env, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    link = env.root / "93_blockout" / "scene_01_shot_01" / "ref_start.png"
    link.symlink_to(outside)
    assert detail(shot_key="1-1")["images"]["blockout_start"] is None


def test_unsafe_chain_id_filter():
    for bad in ("..", "a/b", "a..b", "", "²", "a b", None, 5):
        assert S.safe_chain_id(bad) is False, bad
    assert S.safe_chain_id("sc01_ch01") and S.safe_chain_id("a.b-c")


def test_resolution_zero_and_negative_rejected(env):
    for bad in ("0x720", "1280x0", "-5x10"):
        write_json(env.root / "00_brief.json", {"blockout_resolution": bad})
        assert any(w["code"] == "brief_value_invalid" for w in state()["warnings"]), bad
        with pytest.raises(Exception):
            mod("blockout").settings_to_inputs(PID)


def test_p10_mask_needs_underscore(env):
    shot_dir = env.root / "97_shots" / "scene_01_shot_01"
    shot_dir.mkdir(parents=True, exist_ok=True)
    (shot_dir / "img_final_start.png").write_bytes(b"x" * 10)
    assert S.final_image_mtimes(env.root, 1, 1) == []
    (shot_dir / "img_final_start_02.png").write_bytes(b"x" * 10)
    assert len(S.final_image_mtimes(env.root, 1, 1)) == 1
