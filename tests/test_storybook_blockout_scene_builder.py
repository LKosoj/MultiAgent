"""Э2: тесты blockout_scene_builder.py — camera_plan, asset_map, chains,
scene_spec, report.json, нулевой контракт, B01/B02/B12/B14/B15 (без
реального Blender/LLM/сети — LLM-точки входа замоканы).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from custom_tools.storybook import blockout_common as bc
from custom_tools.storybook import blockout_scene_builder as sb


# === fixtures =================================================================


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _setup_project(
    tmp_path: Path,
    project_id: str,
    *,
    items,
    characters=None,
    locations=None,
    supported_durations=(5, 7, 10),
    caps_warnings=None,
):
    base = tmp_path / project_id
    _write(base / "20_bible" / "characters.json", characters if characters is not None else [{"name": "Герой"}])
    _write(base / "20_bible" / "locations.json", locations if locations is not None else [])
    _write(base / "91_screenplay" / "screenplay.json", {"screenplay": [{"scene_number": 1, "action": "walk"}]})
    _write(base / "97_shots" / "shots.json", {"items": items})
    _write(
        base / "97_shots" / "video_model_caps.json",
        {"supported_durations": list(supported_durations), "warnings": list(caps_warnings or [])},
    )
    return base


def _shot(scene, shot, *, shot_type="start", duration_s=5, camera_plan="MEDIUM SHOT", link_type="independent",
          characters=None, main_subject="Герой", width=1280, height=720, **extra):
    el = {
        "scene_number": scene, "shot_number": shot, "shot_type": shot_type, "duration_s": duration_s,
        "camera_plan": camera_plan, "link_type": link_type, "characters": characters or ["Герой"],
        "main_subject": main_subject, "width": width, "height": height,
    }
    el.update(extra)
    return el


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("BLOCKOUT_ASSETS_DIR", str(tmp_path / "_assets"))
    monkeypatch.setattr(bc, "check_blender_available", lambda *a, **k: (True, "4.2", "ok"))
    monkeypatch.setattr(sb, "llm_resolve_asset", lambda *a, **k: None)
    monkeypatch.setattr(sb, "llm_parse_chain_spatial", lambda *a, **k: None)
    return tmp_path


# === zero contract (раздел 10.3.1) ===========================================


def test_zero_contract_returns_literal_dict_and_touches_nothing(tmp_path):
    # Правка 2: artifact_path must be the real resolved project dir (раздел
    # 10.2), not the hardcoded "plots/storybooks/..." literal -- this
    # fails if STORYBOOK_PROJECTS_DIR (here: tmp_path, via the autouse
    # _env fixture) is ignored.
    result = sb.blockout_scene_builder_tool(session_id="s", project_id="zc1", enable=False)
    assert result == {
        "status": "skipped", "chains_total": 0, "shots_total": 0, "unmapped_assets": [],
        "artifact_path": str(tmp_path / "zc1" / "93_blockout" / "chains.json"),
    }
    assert not (tmp_path / "zc1").exists()


def test_zero_contract_string_false(tmp_path):
    result = sb.blockout_scene_builder_tool(session_id="s", project_id="zc2", enable="false")
    assert result["status"] == "skipped"
    assert not (tmp_path / "zc2").exists()


@pytest.mark.parametrize("project_id", ["../evil", "", None])
def test_zero_contract_degrades_instead_of_raising_on_invalid_project_id(project_id):
    """Правка 2 тонкость: raздел 10.3.1's zero contract requirement is
    unconditional -- switching _zero_contract() from the hardcoded literal
    to safe_storybook_project_dir() means it can now raise ValueError on an
    invalid project_id, which would propagate out of blockout_scene_builder_tool
    and, under on_failure: stop, fail the whole pipeline. Must degrade to
    status="skipped" instead, same as blockout_preview_tool does."""
    result = sb.blockout_scene_builder_tool(session_id="s", project_id=project_id, enable=False)
    assert result["status"] == "skipped"
    assert result["artifact_path"] == ""


# === B02 fractional fps =======================================================


def test_b02_fractional_fps_raises_before_any_file_read(tmp_path):
    # report.json is created first in pipeline order (раздел 10.3.1), so its
    # directory legitimately exists afterwards — but no OTHER project file
    # (shots.json etc.) is ever read: B02 aborts before those reads happen.
    with pytest.raises(ValueError, match="blockout_fps must be an integer"):
        sb.blockout_scene_builder_tool(session_id="s", project_id="nope", fps=23.5)

    report_path = tmp_path / "nope" / "93_blockout" / "report.json"
    assert report_path.is_file()
    report = _read(report_path)
    assert report["blockout_scene_builder"]["checks"][0]["code"] == "B02"
    assert not (tmp_path / "nope" / "97_shots").exists()


def test_b02_accepts_integer_float_and_numeric_string(tmp_path):
    _setup_project(tmp_path, "b02ok", items=[_shot(1, 1)])
    for fps in (24, 24.0, "24"):
        result = sb.blockout_scene_builder_tool(session_id="s", project_id="b02ok", fps=fps)
        assert result["status"] in ("success", "warning")


# === B12 Blender availability =================================================


def test_b12_blender_unavailable_raises_and_records_report(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "check_blender_available", lambda *a, **k: (False, None, "not found"))
    base = _setup_project(tmp_path, "b12", items=[_shot(1, 1)])
    with pytest.raises(RuntimeError, match="B12"):
        sb.blockout_scene_builder_tool(session_id="s", project_id="b12")
    report = _read(base / "93_blockout" / "report.json")
    checks = report["blockout_scene_builder"]["checks"]
    assert checks[0]["code"] == "B12"
    assert checks[0]["level"] == "error"


# === B15 empty supported_durations ============================================


def test_b15_empty_supported_durations_raises(tmp_path):
    base = _setup_project(tmp_path, "b15", items=[_shot(1, 1)], supported_durations=[])
    with pytest.raises(RuntimeError, match="B15"):
        sb.blockout_scene_builder_tool(session_id="s", project_id="b15")
    report = _read(base / "93_blockout" / "report.json")
    assert report["blockout_scene_builder"]["checks"][0]["code"] == "B15"


# === B01a duplicate shot_number within a scene ================================


def test_b01a_duplicate_shot_number_across_chains_raises(tmp_path):
    # two independent "start" elements sharing scene=1/shot=1 each begin
    # their own chain (раздел 7) -- compute_chains would silently collapse
    # them by shot_number, so this must be caught first and raise loudly.
    items = [
        _shot(1, 1, link_type="independent"),
        _shot(1, 1, link_type="independent"),
    ]
    base = _setup_project(tmp_path, "b01a", items=items)
    with pytest.raises(ValueError, match="B01a"):
        sb.blockout_scene_builder_tool(session_id="s", project_id="b01a")
    report = _read(base / "93_blockout" / "report.json")
    checks = report["blockout_scene_builder"]["checks"]
    assert checks[0]["code"] == "B01a"
    assert checks[0]["level"] == "error"


# === B01 duration_s validation =================================================


def test_b01_duration_not_in_effective_set_raises(tmp_path):
    base = _setup_project(tmp_path, "b01a", items=[_shot(1, 1, duration_s=6)], supported_durations=[5, 7, 10])
    with pytest.raises(RuntimeError, match="B01"):
        sb.blockout_scene_builder_tool(session_id="s", project_id="b01a")
    report = _read(base / "93_blockout" / "report.json")
    entry = report["blockout_scene_builder"]["checks"][0]
    assert entry["code"] == "B01"
    assert entry["details"]["invalid"][0]["duration_s"] == 6


def test_b01_duration_missing_raises(tmp_path):
    el = _shot(1, 1)
    del el["duration_s"]
    _setup_project(tmp_path, "b01b", items=[el])
    with pytest.raises(RuntimeError, match="B01"):
        sb.blockout_scene_builder_tool(session_id="s", project_id="b01b")


def test_b01_allowed_durations_narrowing_is_folded_into_report_not_caps_file(tmp_path):
    base = _setup_project(tmp_path, "b01c", items=[_shot(1, 1, duration_s=7)], supported_durations=[5, 7, 10])
    result = sb.blockout_scene_builder_tool(session_id="s", project_id="b01c", allowed_durations=[7, 10, 99])
    assert result["status"] == "warning"
    report = _read(base / "93_blockout" / "report.json")
    codes = [c.get("code") for c in report["blockout_scene_builder"]["checks"]]
    assert "DURATION_ALLOWED_NARROWED" in codes
    caps = _read(base / "97_shots" / "video_model_caps.json")
    assert caps["warnings"] == []  # not written back into video_model_caps.json


# === B14 chain integrity =======================================================


def test_b14_full_copy_without_source_end_path_raises(tmp_path):
    items = [
        _shot(1, 1, link_type="independent"),
        _shot(1, 2, link_type="full_copy"),  # missing source_end_path/copy_from_previous_end
    ]
    _setup_project(tmp_path, "b14", items=items)
    with pytest.raises(RuntimeError, match="B14"):
        sb.blockout_scene_builder_tool(session_id="s", project_id="b14")


# === timeline / window math + 3-shot chain covering all 3 link_type values ====


def test_three_shot_chain_all_link_types_and_timeline_math(tmp_path):
    items = [
        _shot(1, 1, link_type="independent", duration_s=5),
        _shot(1, 2, link_type="full_copy", duration_s=7, source_end_path="a/end.png", copy_from_previous_end=True),
        _shot(1, 3, link_type="reference", duration_s=10),
    ]
    base = _setup_project(tmp_path, "chain3", items=items)
    result = sb.blockout_scene_builder_tool(session_id="s", project_id="chain3")
    assert result["shots_total"] == 3

    chains = _read(base / "93_blockout" / "chains.json")["chains"]
    # independent (shot1) and reference (shot3) each start a new chain;
    # full_copy (shot2) continues shot1's chain (раздел 7).
    assert len(chains) == 2
    chain_a, chain_b = chains
    assert chain_a["chain_id"] == "sc01_ch01"
    assert [s["shot_number"] for s in chain_a["shots"]] == [1, 2]
    assert chain_a["shots"][0]["t_start"] == 0.0
    assert chain_a["shots"][1]["t_start"] == 5.0
    assert chain_a["total_duration_s"] == 12

    assert chain_b["chain_id"] == "sc01_ch02"
    assert [s["shot_number"] for s in chain_b["shots"]] == [3]
    assert chain_b["shots"][0]["t_start"] == 0.0
    assert chain_b["total_duration_s"] == 10


# === camera_plan parsing =======================================================


def test_parse_camera_plan_recognized_base_no_modifier():
    result = sb.parse_camera_plan("MEDIUM SHOT")
    assert result["base"] == "MEDIUM SHOT"
    assert result["base_recognized"] is True
    assert result["lens_mm"] == 50.0
    assert result["warnings"] == []


def test_parse_camera_plan_unrecognized_base_falls_back_to_medium_shot_with_p11():
    result = sb.parse_camera_plan("SOME NONSENSE PLAN")
    assert result["base"] == "MEDIUM SHOT"
    assert result["base_recognized"] is False
    codes = [w["code"] for w in result["warnings"]]
    assert "P11" in codes


def test_parse_camera_plan_position_modifier_conflict_no_crash():
    result = sb.parse_camera_plan("CLOSE-UP — MECHANISM FROM WALL")
    assert result["base"] == "CLOSE-UP"
    assert result["position_override"] == "MECHANISM FROM WALL"
    assert result["fraction"] is None


def test_parse_camera_plan_pov_with_subject_name():
    result = sb.parse_camera_plan("POV — ДОЛБОЯЩЕР")
    assert result["is_pov"] is True
    assert result["pov_subject_name"] == "ДОЛБОЯЩЕР"


def test_parse_camera_plan_unrecognized_modifier_gets_p11_but_keeps_base():
    result = sb.parse_camera_plan("MEDIUM SHOT — SOMETHING WEIRD")
    assert result["base"] == "MEDIUM SHOT"
    assert result["base_recognized"] is True
    codes = [w["code"] for w in result["warnings"]]
    assert "P11" in codes


def test_parse_camera_plan_empty_string_no_crash():
    result = sb.parse_camera_plan("")
    assert result["base"] == "MEDIUM SHOT"
    assert result["base_recognized"] is False


def test_parse_camera_plan_none_no_crash():
    result = sb.parse_camera_plan(None)
    assert result["base"] == "MEDIUM SHOT"


# === object matching + placeholder substitution (P01) =========================


def test_asset_matching_llm_fails_substitutes_proxy_with_p01(tmp_path):
    items = [_shot(1, 1, characters=["Герой"])]
    base = _setup_project(
        tmp_path, "assets1", items=items,
        characters=[{"name": "Герой"}],
        locations=[{"name": "Лес", "key_objects": ["Дерево"], "description": "лес"}],
    )
    result = sb.blockout_scene_builder_tool(session_id="s", project_id="assets1")
    assert set(result["unmapped_assets"]) == {"Герой", "Лес / Дерево"}

    asset_map = _read(base / "93_blockout" / "asset_map.json")
    hero = next(c for c in asset_map["characters"] if c["name"] == "Герой")
    assert hero["asset_id"] == "__proxy_biped__"
    assert hero["scale"] == 1.0
    tree = next(l for l in asset_map["locations"] if l["key_object"] == "Дерево")
    assert tree["asset_id"] == "__proxy_box__"

    report = _read(base / "93_blockout" / "report.json")
    codes = [c["code"] for c in report["blockout_scene_builder"]["checks"]]
    assert codes.count("P01") >= 2


def test_asset_matching_exact_name_match_uses_index_without_llm(tmp_path, monkeypatch):
    assets_root = tmp_path / "_assets"
    (assets_root / "character" / "hero").mkdir(parents=True)
    (assets_root / "character" / "hero" / "meta.json").write_text(json.dumps({
        "id": "hero_01", "name": "Герой", "category": "character", "path": "character/hero",
        "file": "model.glb", "dimensions_m": [0.5, 0.5, 1.8], "has_armature": True,
    }), encoding="utf-8")
    (assets_root / "index.json").write_text(json.dumps({
        "version": 1, "generated_at": None,
        "objects": [{
            "id": "hero_01", "name": "Герой", "category": "character", "path": "character/hero",
            "file": "model.glb", "dimensions_m": [0.5, 0.5, 1.8], "has_armature": True,
        }],
    }), encoding="utf-8")

    llm_calls = []
    monkeypatch.setattr(sb, "llm_resolve_asset", lambda *a, **k: llm_calls.append(1) or None)

    items = [_shot(1, 1, characters=["Герой"])]
    base = _setup_project(tmp_path, "assets2", items=items, characters=[{"name": "Герой"}], locations=[])
    sb.blockout_scene_builder_tool(session_id="s", project_id="assets2")

    assert llm_calls == []  # exact-name match short-circuits before any LLM call
    asset_map = _read(base / "93_blockout" / "asset_map.json")
    hero = asset_map["characters"][0]
    assert hero["asset_id"] == "hero_01"
    assert hero["height_m"] == 1.8


def test_asset_matching_fetch_disabled_falls_back_to_proxy_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOCKOUT_ASSET_FETCH", "off")

    def fake_llm(display_name, description, language):
        return {"asset_id": None, "name": "Hero", "search_query": "knight", "category": "character",
                "height_m": 1.9, "body_plan": "biped"}

    monkeypatch.setattr(sb, "llm_resolve_asset", fake_llm)

    items = [_shot(1, 1, characters=["Герой"])]
    base = _setup_project(tmp_path, "assets3", items=items, characters=[{"name": "Герой"}], locations=[])
    result = sb.blockout_scene_builder_tool(session_id="s", project_id="assets3")
    assert "Герой" in result["unmapped_assets"]
    asset_map = _read(base / "93_blockout" / "asset_map.json")
    assert asset_map["characters"][0]["asset_id"] == "__proxy_biped__"
    assert asset_map["characters"][0]["height_m"] == 1.9


# === asset_map.json entry immutability (раздел 9.4) ============================


def test_asset_map_existing_proxy_entry_untouched_on_rerun(tmp_path, monkeypatch):
    items = [_shot(1, 1, characters=["Герой"])]
    base = _setup_project(tmp_path, "immut1", items=items, characters=[{"name": "Герой"}], locations=[])

    _write(base / "93_blockout" / "asset_map.json", {
        "characters": [{"name": "Герой", "asset_id": "__proxy_biped__", "scale": 1.0, "height_m": 1.42, "body_plan": "biped"}],
        "locations": [], "unmapped": ["Герой"],
    })

    def fail_llm(*a, **k):
        raise AssertionError("LLM must not be called for an existing proxy record")

    monkeypatch.setattr(sb, "llm_resolve_asset", fail_llm)
    sb.blockout_scene_builder_tool(session_id="s", project_id="immut1")
    asset_map = _read(base / "93_blockout" / "asset_map.json")
    hero = asset_map["characters"][0]
    assert hero["height_m"] == 1.42  # untouched
    assert hero["scale"] == 1.0
    assert hero["asset_id"] == "__proxy_biped__"


def test_asset_map_real_asset_scale_recomputed_from_height_m(tmp_path):
    assets_root = tmp_path / "_assets"
    (assets_root / "character" / "hero").mkdir(parents=True)
    (assets_root / "index.json").write_text(json.dumps({
        "version": 1, "generated_at": None,
        "objects": [{
            "id": "hero_01", "name": "Герой", "category": "character", "path": "character/hero",
            "file": "model.glb", "dimensions_m": [0.5, 0.5, 1.8], "has_armature": True,
        }],
    }), encoding="utf-8")

    items = [_shot(1, 1, characters=["Герой"])]
    base = _setup_project(tmp_path, "immut2", items=items, characters=[{"name": "Герой"}], locations=[])
    _write(base / "93_blockout" / "asset_map.json", {
        "characters": [{"name": "Герой", "asset_id": "hero_01", "scale": 1.0, "height_m": 0.9}],
        "locations": [], "unmapped": [],
    })

    sb.blockout_scene_builder_tool(session_id="s", project_id="immut2")
    asset_map = _read(base / "93_blockout" / "asset_map.json")
    hero = asset_map["characters"][0]
    # exception 1: scale recomputed from the (untouched) height_m field
    assert hero["height_m"] == 0.9
    assert hero["scale"] == pytest.approx(0.9 / 1.8)


def test_asset_map_orphaned_asset_id_replaced_with_proxy_and_p01(tmp_path):
    items = [_shot(1, 1, characters=["Герой"])]
    base = _setup_project(tmp_path, "immut3", items=items, characters=[{"name": "Герой"}], locations=[])
    _write(base / "93_blockout" / "asset_map.json", {
        "characters": [{"name": "Герой", "asset_id": "vanished_01", "scale": 1.0, "height_m": 1.7, "body_plan": "biped"}],
        "locations": [], "unmapped": [],
    })
    # no index.json at all -> asset_id can never resolve -> exception 3
    sb.blockout_scene_builder_tool(session_id="s", project_id="immut3")
    asset_map = _read(base / "93_blockout" / "asset_map.json")
    hero = asset_map["characters"][0]
    assert hero["asset_id"] == "__proxy_biped__"
    report = _read(base / "93_blockout" / "report.json")
    codes = [c["code"] for c in report["blockout_scene_builder"]["checks"]]
    assert "P01" in codes


# === POV camera positioning ====================================================


def test_pov_camera_placed_at_subject_eye_height(tmp_path):
    items = [_shot(1, 1, camera_plan="POV — Герой", duration_s=5)]
    base = _setup_project(tmp_path, "pov1", items=items, characters=[{"name": "Герой"}], locations=[])
    sb.blockout_scene_builder_tool(session_id="s", project_id="pov1")
    scene_spec = _read(base / "93_blockout" / "scene_spec.json")
    chain = scene_spec["chains"][0]
    location_keys = chain["camera"]["tracks"]["location"]
    # subject is static at [0,0,0] in the deep-fallback path -> eye height = 0.94*1.70
    matching = [k for k in location_keys if abs(k["t"]) <= 1e-6 or abs(k["t"] - 5.0) <= 1e-6]
    assert matching
    for key in matching:
        assert key["v"][2] == pytest.approx(0.94 * 1.70)


# === report.json write discipline ==============================================


def test_report_write_discipline_preserves_foreign_section_and_no_leftover_tmp(tmp_path):
    items = [_shot(1, 1)]
    base = _setup_project(tmp_path, "report1", items=items)
    report_path = base / "93_blockout" / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({"some_other_step": {"checks": [{"code": "X", "level": "info"}]}}), encoding="utf-8")

    sb.blockout_scene_builder_tool(session_id="s", project_id="report1")

    report = _read(report_path)
    assert report["some_other_step"] == {"checks": [{"code": "X", "level": "info"}]}
    assert "blockout_scene_builder" in report

    leftovers = list(report_path.parent.glob("*.tmp"))
    assert leftovers == []
    assert (report_path.parent / (report_path.name + ".lock")).is_file()


def test_report_created_before_b01_failure_and_foreign_section_preserved(tmp_path):
    el = _shot(1, 1, duration_s=6)  # not in supported_durations -> B01
    base = _setup_project(tmp_path, "report2", items=[el], supported_durations=[5, 7, 10])
    report_path = base / "93_blockout" / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({"foreign": {"ok": True}}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="B01"):
        sb.blockout_scene_builder_tool(session_id="s", project_id="report2")

    report = _read(report_path)
    assert report["foreign"] == {"ok": True}
    assert report["blockout_scene_builder"]["checks"][0]["code"] == "B01"


# === merge_write_report low-level ==============================================


def test_merge_write_report_sidecar_lock_and_unique_tmp(tmp_path):
    report_path = tmp_path / "r.json"
    sb.merge_write_report(report_path, "step_a", lambda s: {"x": 1})
    sb.merge_write_report(report_path, "step_b", lambda s: {"y": 2})
    report = _read(report_path)
    assert report == {"step_a": {"x": 1}, "step_b": {"y": 2}}
    assert (tmp_path / "r.json.lock").is_file()
    assert list(tmp_path.glob("*.tmp")) == []


# === scene_spec.json world/ground/sun schema (раздел 12) ======================


def test_scene_spec_root_schema_matches_section_12(tmp_path):
    items = [_shot(1, 1)]
    base = _setup_project(tmp_path, "specroot", items=items)
    sb.blockout_scene_builder_tool(session_id="s", project_id="specroot", fps=24, resolution="1280x720")
    scene_spec = _read(base / "93_blockout" / "scene_spec.json")

    assert scene_spec["project_id"] == "specroot"
    assert scene_spec["fps"] == 24
    assert scene_spec["resolution"] == [1280, 720]
    assert scene_spec["spec_version"] == 1
    assert "generated_at" in scene_spec and scene_spec["generated_at"]
    assert "world" not in scene_spec  # world lives inside each chain, not at the root (раздел 12)
    assert isinstance(scene_spec["chains"], list) and scene_spec["chains"]


def test_scene_spec_chain_world_ground_and_constant_sun(tmp_path):
    items = [_shot(1, 1)]
    base = _setup_project(tmp_path, "specworld", items=items)
    sb.blockout_scene_builder_tool(session_id="s", project_id="specworld")
    scene_spec = _read(base / "93_blockout" / "scene_spec.json")
    chain = scene_spec["chains"][0]
    world = chain["world"]
    assert world["ground"]["enabled"] is True
    assert world["ground"]["z"] == 0.0
    assert world["ground"]["size_m"] >= 400.0
    # sun direction is a constant project-wide value, not computed (раздел 12, около строки 961)
    assert world["sun"]["elevation_deg"] == 45.0
    assert world["sun"]["azimuth_deg"] == 225.0


# === ground clamp — единственная жёсткая граница по высоте (раздел 10.1) ======


def test_ground_clamp_raises_camera_and_preserves_distance():
    plan = sb.parse_camera_plan("MEDIUM SHOT")
    w_render, h_render = sb.camera_aspect_to_render_hw("16:9")
    h_sensor_mm = bc.sensor_h_mm(sb.DEFAULT_SENSOR_MM, h_render, w_render)
    distance = bc.camera_distance_m(plan["lens_mm"], plan["s_m_override"], plan["fraction"], h_sensor_mm)

    look_at_point = (0.0, 0.0, 1.5)
    # nearly straight down from look_at -> without the clamp z goes well below ground.z
    prior_location = (0.0, -0.01, 0.0)
    boundary_points = [(0.0, plan, None, None)]

    # sanity check: without the clamp the very same inputs really do go below ground
    unclamped_tracks = {
        "location": [{"t": 0.0, "v": list(prior_location), "interp": "constant"}],
        "look_at": [{"t": 0.0, "v": list(look_at_point), "interp": "constant"}],
        "lens_mm": [{"t": 0.0, "v": 50.0, "interp": "constant"}],
    }
    sb._apply_camera_boundary_overrides(unclamped_tracks, boundary_points, h_sensor_mm, {"enabled": False, "z": 0.0})
    assert unclamped_tracks["location"][0]["v"][2] < 0.0

    camera_tracks = {
        "location": [{"t": 0.0, "v": list(prior_location), "interp": "constant"}],
        "look_at": [{"t": 0.0, "v": list(look_at_point), "interp": "constant"}],
        "lens_mm": [{"t": 0.0, "v": 50.0, "interp": "constant"}],
    }
    sb._apply_camera_boundary_overrides(camera_tracks, boundary_points, h_sensor_mm, {"enabled": True, "z": 0.0})
    new_location = camera_tracks["location"][0]["v"]
    assert new_location[2] == pytest.approx(0.0, abs=1e-9)
    achieved_distance = math.dist(new_location, look_at_point)
    assert achieved_distance == pytest.approx(distance, rel=1e-9)


def test_ground_clamp_wired_through_full_tool_for_low_camera_shot(tmp_path):
    # Э2 must build a chain's world block BEFORE computing its camera, so the
    # clamp has ground.z/enabled to clamp against (раздел 10.1 + 12).
    items = [_shot(1, 1, camera_plan="MEDIUM SHOT", duration_s=5)]
    base = _setup_project(tmp_path, "groundclamp1", items=items, characters=[{"name": "Герой"}], locations=[])
    sb.blockout_scene_builder_tool(session_id="s", project_id="groundclamp1")
    scene_spec = _read(base / "93_blockout" / "scene_spec.json")
    chain = scene_spec["chains"][0]
    ground = chain["world"]["ground"]
    assert ground["enabled"] is True
    assert ground["z"] == 0.0

    location_keys = chain["camera"]["tracks"]["location"]
    look_at_keys = chain["camera"]["tracks"]["look_at"]
    key0 = next(k for k in location_keys if abs(k["t"]) <= 1e-6)
    assert key0["v"][2] == pytest.approx(ground["z"], abs=1e-6)

    look_at0 = next(k for k in look_at_keys if abs(k["t"]) <= 1e-6)["v"]
    achieved_distance = math.dist(key0["v"], look_at0)

    w_render, h_render = sb.camera_aspect_to_render_hw(chain["camera_aspect"])
    h_sensor_mm = bc.sensor_h_mm(sb.DEFAULT_SENSOR_MM, h_render, w_render)
    plan = sb.parse_camera_plan("MEDIUM SHOT")
    expected_distance = bc.camera_distance_m(plan["lens_mm"], plan["s_m_override"], plan["fraction"], h_sensor_mm)
    assert achieved_distance == pytest.approx(expected_distance, rel=1e-6)


# === position modifiers: FROM WALL / ВНУТРИ .../ FROM TRAP (раздел 10.1) ======


_POSITION_MODIFIER_EXAMPLES = [
    "WIDE SHOT — POV ДОЛБОЯЩЕРА",
    "MEDIUM SHOT — ВНУТРИ САРКОФАГА",
    "LOW ANGLE — FROM TRAP",
    "CLOSE-UP — MECHANISM FROM WALL",
]


@pytest.mark.parametrize("camera_plan_text", _POSITION_MODIFIER_EXAMPLES)
def test_position_modifier_llm_payload_carries_raw_text_and_required_fields(tmp_path, monkeypatch, camera_plan_text):
    captured = {}

    def fake_llm(chain_payload):
        captured["payload"] = chain_payload
        return None  # force static fallback; only the payload passed in matters here

    monkeypatch.setattr(sb, "llm_parse_chain_spatial", fake_llm)

    items = [
        _shot(1, 1, shot_type="start", camera_plan=camera_plan_text, duration_s=5,
              english_prompt="An english prompt", video_prompt="A video prompt"),
        _shot(1, 1, shot_type="end", camera_plan=camera_plan_text, duration_s=5,
              spatial_changes_from_start="camera pulls back"),
    ]
    _setup_project(tmp_path, f"posmod_payload_{_POSITION_MODIFIER_EXAMPLES.index(camera_plan_text)}",
                   items=items, characters=[{"name": "Герой"}], locations=[])
    sb.blockout_scene_builder_tool(
        session_id="s", project_id=f"posmod_payload_{_POSITION_MODIFIER_EXAMPLES.index(camera_plan_text)}"
    )

    assert "payload" in captured
    shot_payload = captured["payload"]["shots"][0]
    # раздел 10.1, "Контракт LLM-разбора": raw camera_plan text (not just the
    # parsed base) plus english_prompt/video_prompt/spatial_changes_from_start.
    assert shot_payload["camera_plan"] == camera_plan_text
    assert shot_payload["english_prompt"] == "An english prompt"
    assert shot_payload["video_prompt"] == "A video prompt"
    assert shot_payload["spatial_changes_from_start"] == "camera pulls back"


@pytest.mark.parametrize("camera_plan_text, base_lens_mm", [
    ("MEDIUM SHOT — ВНУТРИ САРКОФАГА", 50.0),
    ("LOW ANGLE — FROM TRAP", 35.0),
    ("CLOSE-UP — MECHANISM FROM WALL", 85.0),
])
def test_position_modifier_forces_base_lens_and_keeps_llm_authored_position(tmp_path, monkeypatch, camera_plan_text, base_lens_mm):
    llm_location = [1.0, -2.0, 1.4]

    def fake_llm(chain_payload):
        iids = [o["instance_id"] for o in chain_payload["objects"]]
        return {
            "objects": [
                {"instance_id": iid, "tracks": {"location": [{"t": 0.0, "v": [0.0, 0.0, 0.0], "interp": "constant"}]}}
                for iid in iids
            ],
            "camera": {"tracks": {
                "location": [{"t": 0.0, "v": list(llm_location), "interp": "constant"}],
                "look_at": [{"t": 0.0, "v": [0.0, 0.0, 1.5], "interp": "constant"}],
                # deliberately NOT the base's lens, to prove it gets overridden
                "lens_mm": [{"t": 0.0, "v": 999.0, "interp": "constant"}],
            }},
        }

    monkeypatch.setattr(sb, "llm_parse_chain_spatial", fake_llm)
    items = [_shot(1, 1, camera_plan=camera_plan_text, duration_s=5)]
    base = _setup_project(tmp_path, "posmodlens", items=items, characters=[{"name": "Герой"}], locations=[])
    sb.blockout_scene_builder_tool(session_id="s", project_id="posmodlens")
    scene_spec = _read(base / "93_blockout" / "scene_spec.json")
    chain = scene_spec["chains"][0]

    # (б) "от базы остаётся только объектив" — lens forced to the base's value
    lens_keys = chain["camera"]["tracks"]["lens_mm"]
    assert lens_keys
    assert all(k["v"] == pytest.approx(base_lens_mm) for k in lens_keys)

    # position modifiers are excluded from the table-driven recompute (раздел
    # 10.1) — the LLM-authored location must survive untouched.
    location_keys = chain["camera"]["tracks"]["location"]
    key0 = next(k for k in location_keys if abs(k["t"]) <= 1e-6)
    assert key0["v"] == pytest.approx(llm_location)


def test_position_modifier_example_wide_shot_pov_keeps_base_lens(tmp_path):
    # Fourth ТЗ example: POV as a MODIFIER (not the base) — base's own lens
    # (WIDE SHOT = 28mm) survives, not the POV table row's 35mm.
    items = [_shot(1, 1, camera_plan="WIDE SHOT — POV ДОЛБОЯЩЕРА", duration_s=5)]
    base = _setup_project(tmp_path, "posmod_wide_pov", items=items, characters=[{"name": "Герой"}], locations=[])
    sb.blockout_scene_builder_tool(session_id="s", project_id="posmod_wide_pov")
    chain = _read(base / "93_blockout" / "scene_spec.json")["chains"][0]

    lens_keys = chain["camera"]["tracks"]["lens_mm"]
    assert lens_keys
    assert all(k["v"] == pytest.approx(28.0) for k in lens_keys)

    location_keys = chain["camera"]["tracks"]["location"]
    matching = [k for k in location_keys if abs(k["t"]) <= 1e-6 or abs(k["t"] - 5.0) <= 1e-6]
    assert matching
    for key in matching:
        assert key["v"][2] == pytest.approx(0.94 * 1.70)


# === cache re-snap without LLM on fps/camera_aspect mismatch (раздел 10.1) ====


def test_cache_reuse_skips_llm_on_fps_and_aspect_change_but_recomputes_distance(tmp_path, monkeypatch):
    call_count = {"n": 0}

    def fake_llm_ok(chain_payload):
        call_count["n"] += 1
        iids = [o["instance_id"] for o in chain_payload["objects"]]
        return {
            "objects": [
                {"instance_id": iid, "tracks": {"location": [{"t": 0.0, "v": [0.0, 0.0, 0.0], "interp": "constant"}]}}
                for iid in iids
            ],
            "camera": {"tracks": {
                "location": [{"t": 0.0, "v": [0.0, -4.0404, 1.5], "interp": "constant"}],
                "look_at": [{"t": 0.0, "v": [0.0, 0.0, 1.5], "interp": "constant"}],
                "lens_mm": [{"t": 0.0, "v": 50.0, "interp": "constant"}],
            }},
        }

    monkeypatch.setattr(sb, "llm_parse_chain_spatial", fake_llm_ok)
    items = [_shot(1, 1, camera_plan="MEDIUM SHOT", duration_s=5)]
    base = _setup_project(tmp_path, "cachehit1", items=items, characters=[{"name": "Герой"}], locations=[])

    sb.blockout_scene_builder_tool(session_id="s", project_id="cachehit1", fps=24, resolution="1280x720")
    assert call_count["n"] == 1
    scene_spec_1 = _read(base / "93_blockout" / "scene_spec.json")
    chain_1 = scene_spec_1["chains"][0]
    assert chain_1["fps"] == 24
    assert chain_1["camera_aspect"] == "16:9"

    def fail_llm(*a, **k):
        raise AssertionError("LLM must not be called on a cache hit with only fps/camera_aspect changed")

    monkeypatch.setattr(sb, "llm_parse_chain_spatial", fail_llm)
    # blockout_common.chain_input_hash deliberately excludes fps/camera_aspect
    # (раздел 10.1, «кеш») -> this must be the cheap re-snap-without-LLM path.
    sb.blockout_scene_builder_tool(session_id="s", project_id="cachehit1", fps=30, resolution="720x1280")

    scene_spec_2 = _read(base / "93_blockout" / "scene_spec.json")
    chain_2 = scene_spec_2["chains"][0]
    assert chain_2["fps"] == 30
    assert chain_2["camera_aspect"] == "9:16"
    assert chain_2["source"] == "cache"
    assert chain_2["input_hash"] == chain_1["input_hash"]

    look_at = [0.0, 0.0, 1.5]
    loc1 = next(k for k in chain_1["camera"]["tracks"]["location"] if abs(k["t"]) <= 1e-6)["v"]
    loc2 = next(k for k in chain_2["camera"]["tracks"]["location"] if abs(k["t"]) <= 1e-6)["v"]
    dist1 = math.dist(loc1, look_at)
    dist2 = math.dist(loc2, look_at)
    # u = norm(location - look_at) is reused, only d changes with the new
    # h_sensor_mm (raздел 10.1: "меняется только d").
    assert dist1 == pytest.approx(4.0404, abs=1e-3)
    assert dist2 == pytest.approx(1.2784, abs=1e-3)
    assert dist2 != pytest.approx(dist1, rel=1e-2)


# === B10 camera continuity at internal chain junctions (раздел 15.4, 20.1) ====


def test_b10_camera_jump_at_chain_junction_caught_and_reported(tmp_path):
    # раздел 10.1's boundary-override rule places each shot's own camera_plan
    # target at its own window end -- with a very different camera_plan on
    # each side of a short junction (LONG SHOT -> EXTREME CLOSE-UP, 1s each),
    # the lens_mm track jumps 28mm -> 100mm over one second, well past the
    # 60mm/s threshold of раздел 15.4. Nothing must raise (раздел 10.3.1:
    # B08/B09/B10 degrade to a warning, they never raise) but the jump must
    # be caught and land in report.json as B10 before this fix existed at all.
    items = [
        _shot(1, 1, camera_plan="LONG SHOT", duration_s=1, link_type="independent"),
        _shot(1, 2, camera_plan="EXTREME CLOSE-UP", duration_s=1, link_type="full_copy",
              source_end_path="a/end.png", copy_from_previous_end=True),
    ]
    base = _setup_project(tmp_path, "b10jump", items=items, characters=[{"name": "Герой"}], locations=[],
                           supported_durations=[1])
    result = sb.blockout_scene_builder_tool(session_id="s", project_id="b10jump")
    assert result["status"] == "warning"  # degraded, not failed (раздел 10.3.1)

    report = _read(base / "93_blockout" / "report.json")
    checks = report["blockout_scene_builder"]["checks"]
    b10_checks = [c for c in checks if c.get("code") == "B10"]
    assert len(b10_checks) == 1
    assert b10_checks[0]["level"] == "warning"
    assert "sc01_ch01" in b10_checks[0]["message"]
    assert "t=1.000s" in b10_checks[0]["message"]


def test_b10_smooth_junction_is_not_a_false_positive(tmp_path):
    # Same 2-shot chain shape as above, but a normal 5s shot duration and a
    # mild camera_plan change either side of the junction -- must NOT trip
    # B10, or every ordinary multi-shot chain would warn on every run.
    items = [
        _shot(1, 1, camera_plan="MEDIUM SHOT", duration_s=5, link_type="independent"),
        _shot(1, 2, camera_plan="MEDIUM CLOSE-UP", duration_s=5, link_type="full_copy",
              source_end_path="a/end.png", copy_from_previous_end=True),
    ]
    base = _setup_project(tmp_path, "b10smooth", items=items, characters=[{"name": "Герой"}], locations=[])
    sb.blockout_scene_builder_tool(session_id="s", project_id="b10smooth")

    report = _read(base / "93_blockout" / "report.json")
    checks = report["blockout_scene_builder"]["checks"]
    assert [c for c in checks if c.get("code") == "B10"] == []


# === B08/B09 distinguishable codes (раздел 20.3) ===============================


def test_validate_chain_tracks_distinguishes_b08_from_b09():
    valid_camera = {"tracks": {
        "location": [{"t": 0.0, "v": [0, 0, 0]}, {"t": 5.0, "v": [1, 1, 1]}],
        "look_at": [{"t": 0.0, "v": [0, 0, 0]}, {"t": 5.0, "v": [1, 1, 1]}],
        "lens_mm": [{"t": 0.0, "v": 50.0}, {"t": 5.0, "v": 50.0}],
    }}
    assert sb._validate_chain_tracks([], valid_camera, 5.0, 24) is None

    # B08: location track does not cover [0, total_duration_s] -- ends at t=3
    short_camera = json.loads(json.dumps(valid_camera))
    short_camera["tracks"]["location"][-1]["t"] = 3.0
    assert sb._validate_chain_tracks([], short_camera, 5.0, 24) == "B08"

    # B09: full coverage (first/last keys at 0/5), but a middle key does not
    # land on the 24fps frame grid
    off_grid_camera = json.loads(json.dumps(valid_camera))
    off_grid_camera["tracks"]["location"].insert(1, {"t": 0.013, "v": [0.3, 0.3, 0.3]})
    assert sb._validate_chain_tracks([], off_grid_camera, 5.0, 24) == "B09"


def test_b08_and_b09_surface_distinct_codes_in_report(tmp_path, monkeypatch):
    def make_camera(location_keys):
        return {"objects": [], "camera": {"tracks": {
            "location": location_keys,
            "look_at": [{"t": 0.0, "v": [0, 0, 0], "interp": "constant"}],
            "lens_mm": [{"t": 0.0, "v": 50.0, "interp": "constant"}],
        }}}

    # B08: track stops short of total_duration_s (5s) on every attempt
    b08_response = make_camera([{"t": 0.0, "v": [0, 0, 0], "interp": "linear"},
                                 {"t": 3.0, "v": [1, 1, 1], "interp": "linear"}])
    monkeypatch.setattr(sb, "llm_parse_chain_spatial", lambda *a, **k: b08_response)
    items = [_shot(1, 1, camera_plan="MEDIUM SHOT", duration_s=5)]
    base = _setup_project(tmp_path, "b08rep", items=items, characters=[{"name": "Герой"}], locations=[])
    sb.blockout_scene_builder_tool(session_id="s", project_id="b08rep")
    checks = _read(base / "93_blockout" / "report.json")["blockout_scene_builder"]["checks"]
    assert [c["code"] for c in checks if c.get("code") in ("B08", "B09")] == ["B08"]

    # B09: full coverage, but a key sits off the frame grid
    b09_response = make_camera([{"t": 0.0, "v": [0, 0, 0], "interp": "linear"},
                                 {"t": 0.013, "v": [0.5, 0.5, 0.5], "interp": "linear"},
                                 {"t": 5.0, "v": [1, 1, 1], "interp": "linear"}])
    monkeypatch.setattr(sb, "llm_parse_chain_spatial", lambda *a, **k: b09_response)
    base2 = _setup_project(tmp_path, "b09rep", items=items, characters=[{"name": "Герой"}], locations=[])
    sb.blockout_scene_builder_tool(session_id="s", project_id="b09rep")
    checks2 = _read(base2 / "93_blockout" / "report.json")["blockout_scene_builder"]["checks"]
    assert [c["code"] for c in checks2 if c.get("code") in ("B08", "B09")] == ["B09"]


# === subject_focus validation (P2.14) ==========================================


def test_subject_focus_valid_id_resolves_and_dedupes(tmp_path):
    items = [_shot(1, 1, characters=["Hero"], main_subject="Hero", subject_focus=["hero", "hero"])]
    base = _setup_project(tmp_path, "subjfocus_ok", items=items, characters=[{"name": "Hero"}], locations=[])
    sb.blockout_scene_builder_tool(session_id="s", project_id="subjfocus_ok")
    chain = _read(base / "93_blockout" / "scene_spec.json")["chains"][0]
    assert chain["shots"][0]["subject_focus"] == ["hero"]


def test_subject_focus_valid_id_from_second_shots_own_location(tmp_path):
    # R2-SPEC repro: compute_chains() (blockout_common.py) splits chains
    # purely on link_type, never on location -- a "full_copy" shot can
    # legitimately continue the chain in a DIFFERENT location than the
    # chain's first shot. subject_focus naming a prop that only exists in
    # that second shot's own location must resolve, not raise SUBJ_INVALID
    # just because it isn't among the FIRST shot's location's props.
    items = [
        _shot(1, 1, characters=["Hero"], main_subject="Hero", location="Forest", link_type="independent"),
        _shot(1, 2, characters=["Hero"], main_subject="Hero", location="Cave", link_type="full_copy",
              source_end_path="a/end.png", copy_from_previous_end=True, subject_focus=["rock"]),
    ]
    base = _setup_project(
        tmp_path, "subjfocus_xloc", items=items,
        characters=[{"name": "Hero"}],
        locations=[
            {"name": "Forest", "key_objects": ["tree"], "description": "forest"},
            {"name": "Cave", "key_objects": ["rock"], "description": "cave"},
        ],
    )
    sb.blockout_scene_builder_tool(session_id="s", project_id="subjfocus_xloc")
    chain = _read(base / "93_blockout" / "scene_spec.json")["chains"][0]
    assert chain["shots"][1]["subject_focus"] == ["rock"]


def test_subject_focus_unknown_id_raises_subj_invalid(tmp_path):
    items = [_shot(1, 1, characters=["Hero"], main_subject="Hero", subject_focus=["not_a_real_instance"])]
    _setup_project(tmp_path, "subjfocus_bad", items=items, characters=[{"name": "Hero"}], locations=[])
    with pytest.raises(ValueError, match="SUBJ_INVALID"):
        sb.blockout_scene_builder_tool(session_id="s", project_id="subjfocus_bad")


# === visible track validation (раздел 12) ======================================


def test_visible_track_non_constant_interp_raises_vis_invalid():
    keys = [{"t": 0.0, "v": True, "interp": "linear"}]
    with pytest.raises(ValueError, match="VIS_INVALID"):
        sb._validate_visible_track(keys, 5.0, "sc01_ch01", "hero")


# === scene_spec chain shots carry t_start/duration_s (BUG-A regression guard) =


def test_chain_scene_spec_shots_carry_t_start_and_duration_s(tmp_path):
    # blockout_renderer.py reads shot["t_start"]/shot["duration_s"] off this
    # exact list; dropping them here breaks every real render with a KeyError.
    items = [
        _shot(1, 1, link_type="independent", duration_s=5),
        _shot(1, 2, link_type="reference", duration_s=7),
    ]
    base = _setup_project(tmp_path, "shotcontract", items=items)
    sb.blockout_scene_builder_tool(session_id="s", project_id="shotcontract")
    scene_spec = _read(base / "93_blockout" / "scene_spec.json")
    for chain in scene_spec["chains"]:
        for shot_entry in chain["shots"]:
            assert {"shot_number", "t_start", "duration_s"} <= set(shot_entry.keys())
