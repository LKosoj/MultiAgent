"""Э3: тесты чистых функций build_scene.py (без Blender/bpy).

docs/tz-blockout-reference-pipeline.md, раздел 12 (sun_direction, interp,
proxy/asset resolution), раздел 9.4 (proxy body plans).
"""
from __future__ import annotations

import math

import pytest

from custom_tools.storybook import blockout_assets
from custom_tools.storybook.blockout_blender import build_scene


def test_module_imports_without_bpy():
    assert build_scene.BPY_AVAILABLE is False
    assert build_scene.bpy is None


# === sun_direction (раздел 12) ===============================================


def test_sun_direction_tz_default_constants():
    x, y, z = build_scene.sun_direction(45.0, 225.0)
    el = math.radians(45.0)
    az = math.radians(225.0)
    assert x == pytest.approx(math.cos(el) * math.cos(az))
    assert y == pytest.approx(math.cos(el) * math.sin(az))
    assert z == pytest.approx(math.sin(el))
    assert z == pytest.approx(0.7071067811865475)


def test_sun_direction_straight_up():
    x, y, z = build_scene.sun_direction(90.0, 0.0)
    assert x == pytest.approx(0.0, abs=1e-9)
    assert y == pytest.approx(0.0, abs=1e-9)
    assert z == pytest.approx(1.0)


# === interp mapping (раздел 12 "Требования к трекам") ========================


@pytest.mark.parametrize(
    "interp,expected_interpolation,expected_easing",
    [
        ("linear", "LINEAR", "AUTO"),
        ("constant", "CONSTANT", "AUTO"),
        ("bezier", "BEZIER", "AUTO"),
        ("ease_in", "SINE", "EASE_IN"),
        ("ease_out", "SINE", "EASE_OUT"),
        ("ease_in_out", "SINE", "EASE_IN_OUT"),
    ],
)
def test_blender_interp_for_known_values(interp, expected_interpolation, expected_easing):
    interpolation, easing, warning = build_scene.blender_interp_for(interp)
    assert interpolation == expected_interpolation
    assert easing == expected_easing
    assert warning is None


def test_blender_interp_for_none_defaults_to_linear():
    interpolation, easing, warning = build_scene.blender_interp_for(None)
    assert (interpolation, easing) == ("LINEAR", "AUTO")
    assert warning is None


def test_blender_interp_for_unrecognized_falls_back_with_warning():
    interpolation, easing, warning = build_scene.blender_interp_for("bounce")
    assert (interpolation, easing) == ("LINEAR", "AUTO")
    assert warning is not None and "bounce" in warning


# === proxy detection / body plan (раздел 9.4) ================================


def test_is_proxy_object_true_via_proxy_flag():
    assert build_scene.is_proxy_object({"proxy": True, "asset_id": "whatever"}) is True


def test_is_proxy_object_true_via_reserved_asset_id():
    assert build_scene.is_proxy_object({"asset_id": blockout_assets.PROXY_BIPED}) is True


def test_is_proxy_object_false_for_real_asset():
    assert build_scene.is_proxy_object({"asset_id": "humanoid_adult"}) is False


def test_is_proxy_object_false_when_no_fields():
    assert build_scene.is_proxy_object({"instance_id": "foo"}) is False


@pytest.mark.parametrize(
    "asset_id,expected_plan",
    [
        (blockout_assets.PROXY_BIPED, "biped"),
        (blockout_assets.PROXY_QUADRUPED, "quadruped"),
        (blockout_assets.PROXY_BOX, "none"),
    ],
)
def test_proxy_body_plan_for_asset_id(asset_id, expected_plan):
    assert build_scene.proxy_body_plan_for_asset_id(asset_id) == expected_plan


def test_proxy_body_plan_for_asset_id_raises_for_non_reserved():
    with pytest.raises(ValueError):
        build_scene.proxy_body_plan_for_asset_id("humanoid_adult")


# === resolve_object_geometry: the Э2 schema-gap defensive fallback ===========


def test_resolve_object_geometry_documented_proxy_shape():
    """раздел 9.4/12 documented shape: proxy=True + height_m + a reserved asset_id."""
    obj = {"instance_id": "npc_1", "proxy": True, "asset_id": blockout_assets.PROXY_BIPED, "height_m": 1.8}
    geometry = build_scene.resolve_object_geometry(obj)
    assert geometry == {"mode": "proxy", "body_plan": "biped", "height_m": 1.8, "warning": None}


def test_resolve_object_geometry_e2_actual_output_no_asset_id():
    """Э2's real current output: only instance_id + tracks, no asset_id/proxy
    at all (confirmed via build_chain_scene_spec()'s three code paths).
    Must degrade to a generic box proxy with a P01 warning, not crash."""
    obj = {"instance_id": "hero", "tracks": {}}
    geometry = build_scene.resolve_object_geometry(obj)
    assert geometry["mode"] == "proxy"
    assert geometry["body_plan"] == "none"
    assert geometry["height_m"] == pytest.approx(blockout_assets.typical_height_m("character"))
    assert geometry["warning"] is not None
    assert "P01" in geometry["warning"]
    assert "hero" in geometry["warning"]


def test_resolve_object_geometry_real_asset_id_not_found_in_library(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOCKOUT_ASSETS_DIR", str(tmp_path / "_assets"))
    obj = {"instance_id": "baz", "asset_id": "humanoid_adult"}
    geometry = build_scene.resolve_object_geometry(obj, assets_root=tmp_path / "_assets")
    assert geometry["mode"] == "proxy"
    assert geometry["body_plan"] == "none"
    assert geometry["warning"] is not None
    assert "humanoid_adult" in geometry["warning"]
    assert "not found in library" in geometry["warning"]


def test_resolve_object_geometry_real_asset_id_found_in_library(tmp_path):
    assets_root = tmp_path / "_assets"
    asset_dir = assets_root / "props" / "humanoid_adult"
    asset_dir.mkdir(parents=True)
    (asset_dir / "asset.glb").write_bytes(b"glb-bytes")
    index = {
        "version": 1,
        "generated_at": None,
        "objects": [
            {
                "id": "humanoid_adult",
                "category": "character",
                "path": "props/humanoid_adult",
                "file": "asset.glb",
            }
        ],
    }
    import json

    (assets_root / "index.json").write_text(json.dumps(index), encoding="utf-8")

    obj = {"instance_id": "hero2", "asset_id": "humanoid_adult", "scale": 1.5}
    geometry = build_scene.resolve_object_geometry(obj, assets_root=assets_root)
    assert geometry["mode"] == "asset"
    assert geometry["scale"] == pytest.approx(1.5)
    assert geometry["warning"] is None
    assert geometry["asset_path"] == asset_dir / "asset.glb"


# === PROXY_PART_BUILDERS dispatch table ======================================


def test_proxy_part_builders_covers_every_proxy_part_kind():
    assert set(build_scene.PROXY_PART_BUILDERS.keys()) == {"sphere", "box", "capsule"}


@pytest.mark.parametrize("body_plan", ["biped", "quadruped", "none"])
def test_proxy_part_specs_kinds_all_have_builders(body_plan):
    parts = blockout_assets.proxy_part_specs(body_plan, 1.7)
    for part in parts:
        assert part.kind in build_scene.PROXY_PART_BUILDERS


# === bpy-required functions raise outside Blender ============================


@pytest.mark.parametrize(
    "func,args",
    [
        (build_scene.build_proxy_object, ("x", "biped", 1.7)),
        (build_scene.apply_world, ({},)),
        (build_scene.build_camera, ({}, 24)),
    ],
)
def test_bpy_functions_raise_without_bpy(func, args):
    with pytest.raises(RuntimeError, match="bpy"):
        func(*args)


def test_build_chain_scene_raises_without_bpy():
    with pytest.raises(RuntimeError, match="bpy"):
        build_scene.build_chain_scene({"fps": 24, "objects": [], "world": {}, "camera": {}})
