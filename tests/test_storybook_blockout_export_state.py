"""Э4: тесты ``export_state.py`` -- pure/IO-only functions (раздел 15.1,
15.2), this module's bpy-free tested surface. The bpy-touching functions
(``extract_camera_state``, ``extract_object_raw_state``,
``export_shot_state``) are only checked for their outside-Blender
``RuntimeError`` guard here -- their real behaviour against a live Blender
scene is unverifiable in this environment and is flagged for Э12 manual
acceptance.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_tools.storybook.blockout_blender import export_state


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# === resolve_clip_duration_s (раздел 9.2 "animations" list) ==================


def test_resolve_clip_duration_s_none_without_asset_id():
    assert export_state.resolve_clip_duration_s(None, "walk") is None


def test_resolve_clip_duration_s_none_without_clip_name():
    assert export_state.resolve_clip_duration_s("humanoid_adult", None) is None


def test_resolve_clip_duration_s_none_when_asset_missing_from_library(tmp_path):
    assets_root = tmp_path / "assets"
    _write(assets_root / "index.json", {"version": 1, "generated_at": None, "objects": []})
    assert export_state.resolve_clip_duration_s("missing_asset", "walk", assets_root) is None


def test_resolve_clip_duration_s_none_when_clip_not_found(tmp_path):
    assets_root = tmp_path / "assets"
    _write(assets_root / "index.json", {"version": 1, "generated_at": None, "objects": [
        {"id": "humanoid_adult", "animations": [{"name": "run", "duration_s": 2.0, "loop": True}]},
    ]})
    assert export_state.resolve_clip_duration_s("humanoid_adult", "walk", assets_root) is None


def test_resolve_clip_duration_s_none_for_non_positive_duration(tmp_path):
    assets_root = tmp_path / "assets"
    _write(assets_root / "index.json", {"version": 1, "generated_at": None, "objects": [
        {"id": "humanoid_adult", "animations": [{"name": "walk", "duration_s": 0, "loop": True}]},
    ]})
    assert export_state.resolve_clip_duration_s("humanoid_adult", "walk", assets_root) is None


def test_resolve_clip_duration_s_returns_value_when_resolvable(tmp_path):
    assets_root = tmp_path / "assets"
    _write(assets_root / "index.json", {"version": 1, "generated_at": None, "objects": [
        {"id": "humanoid_adult", "animations": [{"name": "walk", "duration_s": 2.5, "loop": True}]},
    ]})
    assert export_state.resolve_clip_duration_s("humanoid_adult", "walk", assets_root) == 2.5


# === resolve_object_animation_phase (раздел 15.2, Приложение А) ==============
# reuses blockout_common.animation_phase() -- Р3, no duplicate formula.


def test_resolve_object_animation_phase_none_without_animation_spec():
    assert export_state.resolve_object_animation_phase(None, 3.0) is None


def test_resolve_object_animation_phase_none_without_clip():
    assert export_state.resolve_object_animation_phase({}, 3.0) is None


def test_resolve_object_animation_phase_none_when_duration_unresolvable(tmp_path):
    assets_root = tmp_path / "assets"
    _write(assets_root / "index.json", {"version": 1, "generated_at": None, "objects": []})
    result = export_state.resolve_object_animation_phase(
        {"clip": "walk"}, 3.0, asset_id="missing", assets_root=assets_root,
    )
    assert result is None


def test_resolve_object_animation_phase_matches_formula_when_looping(tmp_path):
    """Приложение А: x = phase_at_t0 + (t_in_chain - t0) * speed /
    clip.duration_s, t0 всегда 0, phase = frac(x) при loop.
    x = 0.25 + 3.0*1.0/2.0 = 1.75 -> frac(x) = 0.75."""
    assets_root = tmp_path / "assets"
    _write(assets_root / "index.json", {"version": 1, "generated_at": None, "objects": [
        {"id": "humanoid_adult", "animations": [{"name": "walk", "duration_s": 2.0, "loop": True}]},
    ]})
    result = export_state.resolve_object_animation_phase(
        {"clip": "walk", "phase_at_t0": 0.25, "speed": 1.0, "loop": True},
        t_in_chain=3.0, asset_id="humanoid_adult", assets_root=assets_root,
    )
    assert result == {"clip": "walk", "phase": pytest.approx(0.75)}


def test_resolve_object_animation_phase_non_looping_clamps_to_one(tmp_path):
    assets_root = tmp_path / "assets"
    _write(assets_root / "index.json", {"version": 1, "generated_at": None, "objects": [
        {"id": "humanoid_adult", "animations": [{"name": "walk", "duration_s": 2.0, "loop": True}]},
    ]})
    result = export_state.resolve_object_animation_phase(
        {"clip": "walk", "phase_at_t0": 0.0, "speed": 1.0, "loop": False},
        t_in_chain=10.0, asset_id="humanoid_adult", assets_root=assets_root,
    )
    assert result == {"clip": "walk", "phase": 1.0}


# === build_object_state (раздел 15.2 example shape) ===========================


def test_build_object_state_without_animation():
    state = export_state.build_object_state(
        {"instance_id": "chair"}, [1.0, 2.0, 3.0], [0.0, 0.0, 90.0], [1.0, 1.0, 1.0], True, 0.0,
    )
    assert state == {
        "instance_id": "chair", "location": [1.0, 2.0, 3.0], "rotation_deg": [0.0, 0.0, 90.0],
        "scale": [1.0, 1.0, 1.0], "visible": True,
    }
    assert "animation" not in state


def test_build_object_state_with_resolvable_animation(tmp_path):
    assets_root = tmp_path / "assets"
    _write(assets_root / "index.json", {"version": 1, "generated_at": None, "objects": [
        {"id": "humanoid_adult", "animations": [{"name": "walk", "duration_s": 2.0, "loop": True}]},
    ]})
    obj_spec = {
        "instance_id": "hero", "asset_id": "humanoid_adult",
        "animation": {"clip": "walk", "phase_at_t0": 0.0, "speed": 1.0, "loop": True},
    }
    state = export_state.build_object_state(
        obj_spec, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0], True, 1.0, assets_root=assets_root,
    )
    assert state["animation"] == {"clip": "walk", "phase": pytest.approx(0.5)}


def test_build_object_state_animation_omitted_when_unresolvable(tmp_path):
    """раздел 15.2: this file is diagnostic-only and degrades silently
    rather than raising when the clip can't be resolved -- the "animation"
    key is simply absent, not present-with-null."""
    assets_root = tmp_path / "assets"
    _write(assets_root / "index.json", {"version": 1, "generated_at": None, "objects": []})
    obj_spec = {
        "instance_id": "hero", "asset_id": "missing_asset",
        "animation": {"clip": "walk", "phase_at_t0": 0.0, "speed": 1.0, "loop": True},
    }
    state = export_state.build_object_state(
        obj_spec, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0], True, 1.0, assets_root=assets_root,
    )
    assert "animation" not in state


# === build_shot_state (раздел 15.2 верхний уровень) ============================


def test_build_shot_state_assembles_top_level_shape():
    camera = {"location": [0, 0, 0], "rotation_deg": [0, 0, 0], "lens_mm": 35.0}
    objects = [{"instance_id": "chair", "location": [1, 2, 3]}]
    state = export_state.build_shot_state(
        chain_id="sc01_ch01", shot_number=2, t_in_chain=5.0, blender_version="4.5.1",
        camera=camera, objects=objects,
    )
    assert state == {
        "chain_id": "sc01_ch01", "shot_number": 2, "t_in_chain": 5.0, "blender_version": "4.5.1",
        "camera": camera, "objects": objects,
    }


# === state_output_path (раздел 8) ==============================================


def test_state_output_path_is_zero_padded_under_state_subdir():
    path = export_state.state_output_path(Path("/proj/93_blockout/sc01_ch01"), 3)
    assert path == Path("/proj/93_blockout/sc01_ch01/state/shot_03_out.json")


def test_state_output_path_pads_double_digit_shot_numbers():
    path = export_state.state_output_path(Path("/proj/93_blockout/sc01_ch01"), 12)
    assert path == Path("/proj/93_blockout/sc01_ch01/state/shot_12_out.json")


# === write_state_json (раздел 15.2 "файл пишется только на запись") ==========


def test_write_state_json_writes_atomically_no_leftover_tmp(tmp_path):
    target = tmp_path / "state" / "shot_01_out.json"
    export_state.write_state_json(target, {"chain_id": "x"})
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == {"chain_id": "x"}
    assert list((tmp_path / "state").glob("*.tmp")) == []


def test_write_state_json_overwrites_existing_file(tmp_path):
    target = tmp_path / "state" / "shot_01_out.json"
    export_state.write_state_json(target, {"chain_id": "old"})
    export_state.write_state_json(target, {"chain_id": "new"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"chain_id": "new"}


# === bpy-touching functions degrade correctly outside Blender ================


def test_bpy_touching_functions_raise_outside_blender():
    assert export_state.BPY_AVAILABLE is False
    with pytest.raises(RuntimeError):
        export_state.extract_camera_state(None)
    with pytest.raises(RuntimeError):
        export_state.extract_object_raw_state(None)
    with pytest.raises(RuntimeError):
        export_state.export_shot_state(
            chain_id="x", shot_number=1, t_in_chain=0.0, blender_version=None,
            camera_obj=None, object_specs=[], resolve_object=lambda instance_id: None,
        )
