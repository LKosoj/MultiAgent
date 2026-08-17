"""Э3: тесты чистых функций render_shot.py и CLI-контракта main()/run()
(без Blender/bpy) -- docs/tz-blockout-reference-pipeline.md, разделы 13.4, 14.
"""
from __future__ import annotations

import json

import pytest

from custom_tools.storybook.blockout_blender import render_shot


def test_module_imports_without_bpy():
    assert render_shot.BPY_AVAILABLE is False
    assert render_shot.bpy is None


# === local frame numbering (раздел 13.4 "Нумерация файлов - локальная") =====


@pytest.mark.parametrize("i,expected", [(1, "frame_0001.png"), (241, "frame_0241.png"), (9999, "frame_9999.png")])
def test_local_frame_filename(i, expected):
    assert render_shot.local_frame_filename(i) == expected


def test_frame_filenames_for_window():
    assert render_shot.frame_filenames_for_window(3) == ["frame_0001.png", "frame_0002.png", "frame_0003.png"]


def test_frame_filenames_for_window_zero_is_empty():
    assert render_shot.frame_filenames_for_window(0) == []


# === ref_start/ref_end copy plan (раздел 14) =================================


def test_ref_copy_plan_121_frames():
    assert render_shot.ref_copy_plan(121) == {"ref_start.png": "frame_0001.png", "ref_end.png": "frame_0121.png"}


def test_ref_copy_plan_single_frame():
    """A 1-frame shot: ref_start and ref_end both point at frame_0001.png
    (still a bit-copy each, per раздел 14 -- never a symlink or shared file)."""
    assert render_shot.ref_copy_plan(1) == {"ref_start.png": "frame_0001.png", "ref_end.png": "frame_0001.png"}


# === _sampling_frames() -- shared start/mid/end sampling window ============


def test_sampling_frames_start_mid_end():
    assert render_shot._sampling_frames(0, 10) == [0, 5, 10]


def test_sampling_frames_single_frame_window():
    assert render_shot._sampling_frames(5, 5) == [5]


def test_sampling_frames_order_agnostic():
    assert render_shot._sampling_frames(10, 0) == [0, 5, 10]


def test_sampling_frames_mid_collapses_to_start():
    assert render_shot._sampling_frames(0, 1) == [0, 1]


# === chain_scene_payload() -- bridges the flat blend_path (раздел 8/13.4) ===


def test_chain_scene_payload_forwards_blend_path_to_where_build_chain_scene_reads_it():
    """правка 3 regression: blockout_renderer.py places "blend_path" at the
    TOP level of the render JSON (sibling of "chain"), but
    build_scene.build_chain_scene() only saves chain.blend when its own
    ``payload`` argument -- i.e. the nested chain payload run() hands it --
    carries that key (build_scene.py docstring). Without the bridge, the
    chain payload build_chain_scene() receives never has "blend_path" and
    chain.blend is silently never written."""
    payload = {
        "chain": {"fps": 24, "objects": [], "world": {}, "camera": {}},
        "shots": [],
        "resolution": [1280, 720],
        "fps": 24,
        "blend_path": "/tmp/93_blockout/sc01_ch01/chain.blend",
        "chain_id": "sc01_ch01",
    }

    chain_payload = render_shot.chain_scene_payload(payload)

    # This is exactly the key/level build_chain_scene() consults per its own
    # docstring ("saves chain.blend when payload["blend_path"] is set") --
    # the level the *reader* looks at, not the level the *writer* wrote to.
    assert chain_payload["blend_path"] == payload["blend_path"]
    # Original chain fields must survive untouched.
    assert chain_payload["fps"] == 24
    assert chain_payload["objects"] == []


def test_chain_scene_payload_without_blend_path_key_omits_it():
    """No top-level "blend_path" (e.g. a caller that never wants chain.blend
    saved) must not fabricate the key -- build_chain_scene()'s ``if
    blend_path:`` guard relies on its absence/falsiness to skip saving."""
    payload = {"chain": {"fps": 24, "objects": [], "world": {}, "camera": {}}, "shots": [], "resolution": [1280, 720], "fps": 24}

    chain_payload = render_shot.chain_scene_payload(payload)

    assert "blend_path" not in chain_payload


def test_chain_scene_payload_does_not_mutate_input_chain_dict():
    """chain_scene_payload() must not mutate payload["chain"] in place --
    callers (e.g. run()) still pass the original "chain" sub-dict around
    (_write_shot_state() reads chain_payload["objects"] from its return
    value, not from payload["chain"] directly, but the input must stay
    pristine)."""
    original_chain = {"fps": 24, "objects": [], "world": {}, "camera": {}}
    payload = {"chain": original_chain, "shots": [], "resolution": [1280, 720], "fps": 24, "blend_path": "/tmp/x.blend"}

    render_shot.chain_scene_payload(payload)

    assert "blend_path" not in original_chain


# === run() requires bpy =======================================================


def test_run_raises_without_bpy():
    with pytest.raises(RuntimeError, match="bpy"):
        render_shot.run({"chain": {}, "shots": [], "resolution": [1280, 720], "fps": 24})


# === main() CLI contract (mirrors normalize_asset.py's own tests) ============


def test_main_writes_success_output_with_mocked_run(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "result.json"
    payload = {
        "chain": {"fps": 24, "objects": [], "world": {}, "camera": {}},
        "shots": [],
        "resolution": [1280, 720],
        "fps": 24,
        "output_path": str(output_path),
    }
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    fake_result = {"ok": True, "chain_warnings": [], "shots": {}}
    monkeypatch.setattr(render_shot, "run", lambda payload: fake_result)

    exit_code = render_shot.main(["blender", "-b", "-P", "render_shot.py", "--", str(input_path)])

    assert exit_code == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result == fake_result
    assert list(tmp_path.glob("*.tmp")) == []


def test_main_writes_failure_output_and_returns_nonzero(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "result.json"
    payload = {"chain": {}, "shots": [], "resolution": [1280, 720], "fps": 24, "output_path": str(output_path)}
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    def _boom(payload):
        raise RuntimeError("blender exploded")

    monkeypatch.setattr(render_shot, "run", _boom)

    exit_code = render_shot.main(["--", str(input_path)])

    assert exit_code == 1
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert "blender exploded" in result["error"]


def test_main_missing_separator_returns_error_without_crashing():
    assert render_shot.main(["no-separator-here"]) == 1


def test_main_missing_input_path_after_separator_returns_error():
    assert render_shot.main(["--"]) == 1


# === _aabb_contains() -- pure margin-shrunk AABB containment (раздел P1.4) ===


@pytest.mark.parametrize(
    "point,expected",
    [
        ((0.0, 0.0, 0.0), True),
        ((0.95, 0.0, 0.0), False),  # exactly on the margin-shrunk boundary: strict "<" excludes it
        ((10.0, 10.0, 10.0), False),
        ((0.0, 0.0, 5.0), False),  # inside on x/y but outside on z
    ],
)
def test_aabb_contains(point, expected):
    aabb_min = (-1.0, -1.0, -1.0)
    aabb_max = (1.0, 1.0, 1.0)
    assert render_shot._aabb_contains(point, aabb_min, aabb_max, margin=0.05) == expected


# === _category_for_instance() -- P0.2 material classification ================


def test_scan_asset_categories_default_delegates_to_blockout_assets_root(tmp_path, monkeypatch):
    """assets_root=None (the production default) resolves via
    blockout_assets.blockout_assets_root(), which reads BLOCKOUT_ASSETS_DIR --
    the pre-existing tests only exercised the explicit assets_root= path."""
    monkeypatch.setenv("BLOCKOUT_ASSETS_DIR", str(tmp_path))
    (tmp_path / "character" / "hero").mkdir(parents=True)
    (tmp_path / "character" / "hero" / "meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "prop" / "chair_01").mkdir(parents=True)
    (tmp_path / "prop" / "chair_01" / "meta.json").write_text("{}", encoding="utf-8")

    mapping = render_shot._scan_asset_categories(assets_root=None)

    assert mapping == {"hero": "character", "chair_01": "prop"}


def test_category_for_instance_unresolved_ancestor_returns_none():
    """The ground plane (and anything else with no chain object entry) must
    stay untinted, not default to "prop"."""
    assert render_shot._category_for_instance({"a": "prop"}, None) is None


def test_category_for_instance_resolved_without_category_defaults_to_prop():
    assert render_shot._category_for_instance({}, "some_instance") == "prop"


def test_category_for_instance_resolved_with_category():
    assert render_shot._category_for_instance({"some_instance": "vehicle"}, "some_instance") == "vehicle"
