import json

import pytest

from custom_tools.storybook.blockout_blender import normalize_asset


def test_module_imports_without_bpy():
    assert normalize_asset.BPY_AVAILABLE is False
    assert normalize_asset.bpy is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Armature|Take 001", "Armature|Take 001"),
        ("walk_cycle_01", "walk"),
        ("Run_Fast", "run"),
    ],
)
def test_canonicalize_clip_name_tz_examples(raw, expected):
    assert normalize_asset.canonicalize_clip_name(raw) == expected


def test_resolve_animation_names_single_unrecognized_becomes_idle():
    assert normalize_asset.resolve_animation_names(["Mystery Clip"]) == ["idle"]


def test_resolve_animation_names_single_recognized_kept():
    assert normalize_asset.resolve_animation_names(["walk_cycle_01"]) == ["walk"]


def test_resolve_animation_names_multiple_unrecognized_kept_as_is():
    assert normalize_asset.resolve_animation_names(["Foo", "Bar"]) == ["Foo", "Bar"]


def test_resolve_animation_names_mixed():
    assert normalize_asset.resolve_animation_names(["walk_cycle_01", "Run_Fast", "Armature|Take 001"]) == [
        "walk",
        "run",
        "Armature|Take 001",
    ]


def test_compute_dimensions():
    dims = normalize_asset.compute_dimensions((-0.2, -0.1, 0.0), (0.35, 0.25, 1.78))
    assert dims == pytest.approx((0.55, 0.35, 1.78))


def test_compute_pivot_offset():
    offset = normalize_asset.compute_pivot_offset((-0.2, -0.1, 0.0), (0.35, 0.25, 1.78))
    assert offset == pytest.approx((-0.075, -0.075, 0.0))


def test_compute_out_of_range_correction_within_range_is_none():
    assert normalize_asset.compute_out_of_range_correction(1.78, "character") is None
    assert normalize_asset.compute_out_of_range_correction(0.01, "prop") is None
    assert normalize_asset.compute_out_of_range_correction(100.0, "building") is None


def test_compute_out_of_range_correction_too_small():
    correction = normalize_asset.compute_out_of_range_correction(0.005, "character")
    assert correction == pytest.approx(1.70 / 0.005)


def test_compute_out_of_range_correction_too_large():
    correction = normalize_asset.compute_out_of_range_correction(150.0, "vehicle")
    assert correction == pytest.approx(1.60 / 150.0)


def test_detect_named_forward_xy_kenney_wheels_point_minus_y():
    # Kenney Train Kit: wheels-front at y=-0.6, wheels-back at y=+0.6.
    parts = [
        {"name": "train-locomotive-a", "location": [0.0, 0.0, 0.0]},
        {"name": "wheels-front", "location": [0.0, -0.6, 0.36]},
        {"name": "wheels-back", "location": [0.0, 0.6, 0.36]},
    ]
    forward = normalize_asset.detect_named_forward_xy(parts)
    assert forward is not None
    assert forward[0] == pytest.approx(0.0)
    assert forward[1] == pytest.approx(-1.2)


def test_detect_named_forward_xy_no_named_front_returns_none():
    parts = [{"name": "cactus_tall", "location": [0.0, 0.0, 0.0]}]
    assert normalize_asset.detect_named_forward_xy(parts) is None


def test_yaw_delta_to_plus_y_from_minus_y_is_180_deg():
    import math

    delta = normalize_asset.yaw_delta_to_plus_y(0.0, -1.2)
    assert delta == pytest.approx(math.pi)


def test_run_raises_without_bpy():
    with pytest.raises(RuntimeError):
        normalize_asset.run({"source_path": "x", "category": "prop", "output_path": "y", "output_glb_path": "z"})


def test_main_writes_success_output_with_mocked_run(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "result.json"
    payload = {
        "source_path": str(tmp_path / "source.glb"),
        "category": "prop",
        "output_path": str(output_path),
        "output_glb_path": str(tmp_path / "out.glb"),
    }
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    fake_result = {
        "ok": True,
        "dimensions_m": [0.5, 0.5, 0.5],
        "has_armature": False,
        "animations": [],
        "forward_axis": "+Y",
        "up_axis": "+Z",
        "pivot": "base_center",
        "blender_version": "4.2.0",
        "warnings": [],
    }
    monkeypatch.setattr(normalize_asset, "run", lambda payload: fake_result)

    exit_code = normalize_asset.main(["blender", "-b", "-P", "normalize_asset.py", "--", str(input_path)])

    assert exit_code == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result == fake_result
    assert list(tmp_path.glob("*.tmp")) == []


def test_main_writes_failure_output_and_returns_nonzero(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "result.json"
    payload = {
        "source_path": str(tmp_path / "source.glb"),
        "category": "prop",
        "output_path": str(output_path),
        "output_glb_path": str(tmp_path / "out.glb"),
    }
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    def _boom(payload):
        raise RuntimeError("blender exploded")

    monkeypatch.setattr(normalize_asset, "run", _boom)

    exit_code = normalize_asset.main(["--", str(input_path)])

    assert exit_code == 1
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert "blender exploded" in result["error"]


def test_main_missing_separator_returns_error_without_crashing():
    assert normalize_asset.main(["no-separator-here"]) == 1


def test_main_missing_input_path_after_separator_returns_error():
    assert normalize_asset.main(["--"]) == 1
