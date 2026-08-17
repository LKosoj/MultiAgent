"""Э3: тесты blockout_renderer.py -- разрешение размера (раздел 16.2),
B01(3-я форма)/B02(2-я форма)/B03-B05/B07/B11/B12/B16/B17, scope (раздел
10.2), manifest.json (раздел 10.2 п.7), ffmpeg (раздел 16.1), запись
shots.json (раздел 10.2 "Дисциплина записи"), нулевой контракт (раздел
10.3.1). Без реального Blender -- запуск подменяется через
``blockout_common.run_blender_script`` (module_callable-путь).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_tools.storybook import blockout_common as bc
from custom_tools.storybook import blockout_renderer as renderer


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(bc, "check_blender_available", lambda *a, **k: (True, "4.5.1", "ok"))
    return tmp_path


# === zero contract (раздел 10.3.1) ============================================


def test_zero_contract_returns_literal_dict_and_touches_nothing(tmp_path):
    # Ошибка 2: artifact_path must be the real resolved project dir (раздел
    # 10.2), not the hardcoded "plots/storybooks/..." literal -- this
    # fails if STORYBOOK_PROJECTS_DIR (here: tmp_path, via the autouse
    # _env fixture) is ignored.
    result = renderer.blockout_renderer_tool(session_id="s", project_id="zc1", enable=False)
    assert result == {
        "status": "skipped", "shots_rendered": 0, "frames_total": 0, "junction_checks": [],
        "artifact_path": str(tmp_path / "zc1" / "93_blockout"),
    }
    assert not (tmp_path / "zc1").exists()


def test_zero_contract_string_false(tmp_path):
    result = renderer.blockout_renderer_tool(session_id="s", project_id="zc2", enable="false")
    assert result["status"] == "skipped"
    assert not (tmp_path / "zc2").exists()


# === _parse_shots_filter: B21b garbage-token handling =========================


def test_parse_shots_filter_rejects_mixed_valid_and_garbage_token():
    with pytest.raises(ValueError, match="B21b"):
        renderer._parse_shots_filter("1,foo")


def test_parse_shots_filter_rejects_multiple_garbage_tokens_names_both():
    with pytest.raises(ValueError) as excinfo:
        renderer._parse_shots_filter("foo,1,bar")
    assert "foo" in str(excinfo.value)
    assert "bar" in str(excinfo.value)


def test_parse_shots_filter_tolerates_empty_tokens():
    assert renderer._parse_shots_filter("1,,,2") == {1, 2}


def test_parse_shots_filter_happy_path():
    assert renderer._parse_shots_filter("1,2,3") == {1, 2, 3}


# === B17 first form: scene_spec.json missing ==================================


def test_b17_scene_spec_missing_raises(tmp_path):
    with pytest.raises(RuntimeError, match="B17"):
        renderer.blockout_renderer_tool(session_id="s", project_id="nope")
    report = _read(tmp_path / "nope" / "93_blockout" / "report.json")
    assert report["blockout_renderer"]["checks"][0]["code"] == "B17"


# === B12 Blender availability =================================================


def test_b12_blender_unavailable_raises_and_records_report(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "check_blender_available", lambda *a, **k: (False, None, "not found"))
    _write(tmp_path / "b12" / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 24, "world": {}, "chains": []})
    with pytest.raises(RuntimeError, match="B12"):
        renderer.blockout_renderer_tool(session_id="s", project_id="b12")
    report = _read(tmp_path / "b12" / "93_blockout" / "report.json")
    assert report["blockout_renderer"]["checks"][0]["code"] == "B12"


# === B02 second form: blockout_fps vs scene_spec.json's fps ===================


def test_b02_second_form_fps_mismatch_raises(tmp_path):
    _write(tmp_path / "fpsmismatch" / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 24, "world": {}, "chains": []})
    with pytest.raises(RuntimeError, match="B02"):
        renderer.blockout_renderer_tool(session_id="s", project_id="fpsmismatch", fps=30)
    report = _read(tmp_path / "fpsmismatch" / "93_blockout" / "report.json")
    assert report["blockout_renderer"]["checks"][0]["code"] == "B02"


# === B01 third form: shots.json duration_s vs scene_spec.json =================


def test_b01_third_form_duration_mismatch_raises(tmp_path):
    chain = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "shots": [{"shot_number": 1, "duration_s": 5, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    _write(tmp_path / "durmismatch" / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 24, "world": {}, "chains": [chain]})
    _write(
        tmp_path / "durmismatch" / "97_shots" / "shots.json",
        {"items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 7, "width": 1280, "height": 720}]},
    )
    with pytest.raises(RuntimeError, match="B01"):
        renderer.blockout_renderer_tool(session_id="s", project_id="durmismatch", fps=24)
    report = _read(tmp_path / "durmismatch" / "93_blockout" / "report.json")
    assert report["blockout_renderer"]["checks"][0]["code"] == "B01"


def test_b01_checked_only_within_scope_not_whole_project(tmp_path, monkeypatch):
    """Ошибка 3 (раздел 12, 18.4, сценарий A12 "перерендер одной цепочки"):
    scope must be resolved BEFORE B01 is checked, and B01 must only look
    at the shots.json/scene_spec.json duration_s of the SELECTED chains --
    rerendering one healthy chain must not fail because some unrelated
    chain elsewhere in the project has a stale duration mismatch."""
    project_id = "scoped_b01"
    base = tmp_path / project_id

    good_chain = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "shots": [{"shot_number": 1, "duration_s": 5, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    bad_chain = {
        "chain_id": "sc02_ch01", "scene_number": 2, "camera_aspect": "16:9",
        "shots": [{"shot_number": 1, "duration_s": 5, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    _write(base / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 1, "chains": [good_chain, bad_chain]})
    _write(
        base / "97_shots" / "shots.json",
        {"items": [
            {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 5, "width": 1280, "height": 720},
            # sc02_ch01's shot has a duration_s mismatch vs scene_spec.json (5 != 999)
            {"scene_number": 2, "shot_number": 1, "shot_type": "start", "duration_s": 999, "width": 1280, "height": 720},
        ]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})
    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", lambda *a, **k: {"ok": False, "error": "stub"})

    # Rendering only the healthy chain must NOT raise B01 (fails later at
    # the stubbed B12 instead, proving B01 didn't short-circuit first).
    result = renderer.blockout_renderer_tool(
        session_id="s", project_id=project_id, fps=1, resolution="1280x720", scope="chain_sc01_ch01",
    )
    assert result["status"] == "partial"

    # Rendering the mismatched chain itself must still raise B01.
    with pytest.raises(RuntimeError, match="B01"):
        renderer.blockout_renderer_tool(
            session_id="s", project_id=project_id, fps=1, resolution="1280x720", scope="chain_sc02_ch01",
        )


# === resolve_render_resolution (раздел 16.2) ==================================


def test_resolve_render_resolution_tz_example_4_3():
    """раздел 16.2 example: 1280x720 request, model confirms 4:3 -> 1112x834."""
    video_caps = {"supported_resolutions": ["720p"], "supported_aspect_ratios": ["4:3"]}
    w, h, aspect, warnings = renderer.resolve_render_resolution(video_caps, 960, 720, "1280x720")
    assert (w, h) == (1112, 834)
    assert aspect == "4:3"
    assert warnings == []


def test_resolve_render_resolution_exact_size_match():
    video_caps = {"supported_sizes": ["960x720"]}
    w, h, aspect, warnings = renderer.resolve_render_resolution(video_caps, 960, 720, "1280x720")
    assert aspect == "4:3"
    assert warnings == []


def test_resolve_render_resolution_missing_width_height_p13():
    w, h, aspect, warnings = renderer.resolve_render_resolution({}, None, None, "1280x720")
    assert aspect == "16:9"
    assert (w, h) == (1280, 720)
    assert warnings[0]["code"] == "P13"
    assert warnings[0]["reason"] == "shot_size_missing"


def test_resolve_render_resolution_no_size_fields_p13():
    w, h, aspect, warnings = renderer.resolve_render_resolution({}, 1280, 720, "1280x720")
    assert aspect == "16:9"
    assert warnings[0]["reason"] == "no_size_fields"


def test_resolve_render_resolution_aspect_mismatch_p13():
    video_caps = {"supported_sizes": ["9999x9999"]}
    w, h, aspect, warnings = renderer.resolve_render_resolution(video_caps, 1280, 720, "1280x720")
    assert warnings[0]["reason"] == "aspect_mismatch"
    assert aspect == "16:9"


def test_check_chain_size_mismatch_true_when_other_shot_differs():
    """Ошибка 5 (раздел 20.2/22 A40, 4-я форма P13): render always goes by
    the first shot's size, but a later shot with a DIFFERENT size in the
    same chain must still raise a chain_size_mismatch warning."""
    items = [
        {"scene_number": 1, "shot_number": 1, "shot_type": "start", "width": 1280, "height": 720},
        {"scene_number": 1, "shot_number": 2, "shot_type": "start", "width": 640, "height": 480},
    ]
    chain_shots = [{"shot_number": 1}, {"shot_number": 2}]
    assert renderer.check_chain_size_mismatch(chain_shots, items, 1, 1280, 720) is True


def test_check_chain_size_mismatch_false_when_all_shots_match():
    items = [
        {"scene_number": 1, "shot_number": 1, "shot_type": "start", "width": 1280, "height": 720},
        {"scene_number": 1, "shot_number": 2, "shot_type": "start", "width": 1280, "height": 720},
    ]
    chain_shots = [{"shot_number": 1}, {"shot_number": 2}]
    assert renderer.check_chain_size_mismatch(chain_shots, items, 1, 1280, 720) is False


def test_full_run_emits_chain_size_mismatch_p13_warning(tmp_path, monkeypatch):
    """Integration (A40): a chain whose shots have differing width/height
    in shots.json must produce a P13/chain_size_mismatch entry in
    report.json once wired into the actual render path."""
    project_id = "size_mismatch_e2e"
    base = tmp_path / project_id
    chain = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "shots": [
            {"shot_number": 1, "duration_s": 1, "t_start": 0.0},
            {"shot_number": 2, "duration_s": 1, "t_start": 1.0},
        ],
        "objects": [], "camera": {},
    }
    _write(base / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 1, "chains": [chain]})
    _write(
        base / "97_shots" / "shots.json",
        {"items": [
            {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720},
            {"scene_number": 1, "shot_number": 2, "shot_type": "start", "duration_s": 1, "width": 640, "height": 480},
        ]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})
    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", lambda *a, **k: {"ok": False, "error": "stub"})

    renderer.blockout_renderer_tool(session_id="s", project_id=project_id, fps=1, resolution="1280x720")

    report = _read(base / "93_blockout" / "report.json")
    p13_reasons = [c.get("reason") for c in report["blockout_renderer"]["checks"] if c.get("code") == "P13"]
    assert "chain_size_mismatch" in p13_reasons


# === B16 / B17(chain present) =================================================


def test_check_b16_matches():
    assert renderer.check_b16("16:9", "16:9") is True
    assert renderer.check_b16("16:9", "4:3") is False
    assert renderer.check_b16(None, "16:9") is False


def test_check_b17_chain_present():
    chains = [{"chain_id": "sc01_ch01"}]
    assert renderer.check_b17_chain_present(chains, "sc01_ch01") is True
    assert renderer.check_b17_chain_present(chains, "sc02_ch01") is False


# === B03-B05, B07, B11 pure checks ============================================


def test_check_b03_frame_count():
    assert renderer.check_b03(["frame_0001.png", "frame_0002.png"], 2) is True
    assert renderer.check_b03(["frame_0001.png"], 2) is False


def test_check_b04_frame_numbering_gaps():
    assert renderer.check_b04(["frame_0001.png", "frame_0002.png"], 2) is True
    assert renderer.check_b04(["frame_0001.png", "frame_0003.png"], 2) is False


def test_check_b05_uniform_sizes():
    assert renderer.check_b05([(1280, 720), (1280, 720)]) is True
    assert renderer.check_b05([(1280, 720), (640, 480)]) is False
    assert renderer.check_b05([]) is False


def test_check_b07_hash_equality():
    assert renderer.check_b07("sha256:abc", "sha256:abc") is True
    assert renderer.check_b07("sha256:abc", "sha256:def") is False
    assert renderer.check_b07(None, None) is False


def test_check_b11_video_frame_count():
    assert renderer.check_b11(120, 120) is True
    assert renderer.check_b11(119, 120) is False
    assert renderer.check_b11(None, 120) is False


# === compare_junction_frames / blender_version_mismatch (раздел 15.3, B06/P07)


def _make_flat_image(path: Path, size, color) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path)


def _make_image_with_diff_pixels(path: Path, size, base_color, diff_pixels: int, delta: int) -> None:
    """``size`` = ``(w, h)``; the first ``diff_pixels`` pixels (row-major
    from the top-left) get every channel bumped by ``delta`` from
    ``base_color`` -- the rest stay flat ``base_color``."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=base_color)
    px = img.load()
    w, h = size
    n = 0
    for y in range(h):
        for x in range(w):
            if n >= diff_pixels:
                break
            px[x, y] = tuple(min(255, c + delta) for c in base_color)
            n += 1
        if n >= diff_pixels:
            break
    img.save(path)


def test_compare_junction_frames_sha256_fast_path_is_exact():
    # Fast path returns before touching the filesystem -- nonexistent
    # paths still produce "exact" as long as the hashes agree.
    result = renderer.compare_junction_frames(
        "sha256:aaa", "sha256:aaa", Path("/does/not/exist/a.png"), Path("/does/not/exist/b.png"),
    )
    assert result == {"status": "exact"}


def test_compare_junction_frames_soft_pass_at_both_thresholds(tmp_path):
    """1 differing pixel out of 1000 (ratio == 0.001, exactly раздел 15.3's
    soft threshold), each channel off by exactly 1 (== раздел 15.3's
    max-channel-diff threshold) -> "soft", not "failed"."""
    size = (100, 10)  # 1000 pixels
    path_a = tmp_path / "a.png"
    path_b = tmp_path / "b.png"
    _make_flat_image(path_a, size, (100, 100, 100))
    _make_image_with_diff_pixels(path_b, size, (100, 100, 100), diff_pixels=1, delta=1)

    result = renderer.compare_junction_frames(None, None, path_a, path_b)
    assert result["status"] == "soft"
    assert result["max_channel_diff"] == 1
    assert result["diff_pixels_ratio"] == pytest.approx(0.001)


def test_compare_junction_frames_failed_just_over_ratio_threshold(tmp_path):
    """2/1000 differing pixels (ratio 0.002 > 0.001) -- fails even though
    the per-channel delta (1) is within its own threshold."""
    size = (100, 10)
    path_a = tmp_path / "a.png"
    path_b = tmp_path / "b.png"
    _make_flat_image(path_a, size, (100, 100, 100))
    _make_image_with_diff_pixels(path_b, size, (100, 100, 100), diff_pixels=2, delta=1)

    result = renderer.compare_junction_frames(None, None, path_a, path_b)
    assert result["status"] == "failed"
    assert result["max_channel_diff"] == 1
    assert result["diff_pixels_ratio"] == pytest.approx(0.002)


def test_compare_junction_frames_failed_just_over_channel_threshold(tmp_path):
    """1/1000 differing pixel (ratio within threshold) but off by 2 levels
    per channel (> раздел 15.3's max-channel-diff threshold of 1) -- fails
    even though the pixel ratio is within its own threshold."""
    size = (100, 10)
    path_a = tmp_path / "a.png"
    path_b = tmp_path / "b.png"
    _make_flat_image(path_a, size, (100, 100, 100))
    _make_image_with_diff_pixels(path_b, size, (100, 100, 100), diff_pixels=1, delta=2)

    result = renderer.compare_junction_frames(None, None, path_a, path_b)
    assert result["status"] == "failed"
    assert result["max_channel_diff"] == 2
    assert result["diff_pixels_ratio"] == pytest.approx(0.001)


def test_compare_junction_frames_pixel_identical_but_hash_mismatch_is_soft_not_exact(tmp_path):
    """Only the sha256 fast path produces "exact" -- pixel-identical frames
    reached via the pixel path (hashes missing or differing) come back as
    "soft" with zero diff, never "exact"."""
    size = (10, 10)
    path_a = tmp_path / "a.png"
    path_b = tmp_path / "b.png"
    _make_flat_image(path_a, size, (100, 100, 100))
    _make_flat_image(path_b, size, (100, 100, 100))

    result = renderer.compare_junction_frames("sha256:aaa", "sha256:bbb", path_a, path_b)
    assert result == {"status": "soft", "max_channel_diff": 0, "diff_pixels_ratio": 0.0, "warning": "static_cut"}


def test_compare_junction_frames_missing_file():
    result = renderer.compare_junction_frames(
        None, None, Path("/does/not/exist/a.png"), Path("/does/not/exist/b.png"),
    )
    assert result == {"status": "failed", "reason": "missing_file"}


def test_compare_junction_frames_size_mismatch(tmp_path):
    path_a = tmp_path / "a.png"
    path_b = tmp_path / "b.png"
    _make_flat_image(path_a, (100, 10), (1, 2, 3))
    _make_flat_image(path_b, (50, 10), (1, 2, 3))

    result = renderer.compare_junction_frames(None, None, path_a, path_b)
    assert result == {"status": "failed", "reason": "size_mismatch"}


def test_compare_junction_frames_numpy_unavailable_degrades_to_failed(tmp_path, monkeypatch):
    path_a = tmp_path / "a.png"
    path_b = tmp_path / "b.png"
    _make_flat_image(path_a, (10, 10), (1, 2, 3))
    _make_flat_image(path_b, (10, 10), (1, 2, 3))
    monkeypatch.setattr(renderer, "_NUMPY_AVAILABLE", False)

    result = renderer.compare_junction_frames(None, None, path_a, path_b)
    assert result == {"status": "failed", "reason": "numpy_unavailable"}


def test_blender_version_mismatch_true_when_both_present_and_different():
    assert renderer.blender_version_mismatch("4.5.1", "4.4.0") is True


def test_blender_version_mismatch_false_when_equal():
    assert renderer.blender_version_mismatch("4.5.1", "4.5.1") is False


def test_blender_version_mismatch_false_when_either_side_missing():
    assert renderer.blender_version_mismatch(None, "4.5.1") is False
    assert renderer.blender_version_mismatch("4.5.1", None) is False
    assert renderer.blender_version_mismatch(None, None) is False


# === Blender subprocess timeout (Предупреждение 7, раздел 13.4/13.5) =========


def test_compute_chain_render_timeout_floor_for_short_chain():
    """A short chain keeps the previous flat 900s floor."""
    assert renderer.compute_chain_render_timeout(24) == 900.0


def test_compute_chain_render_timeout_scales_for_long_chain():
    """раздел 13.5 worst case (~0.4s/frame): a long chain's total frame
    count must push the timeout well past the flat 900s a hardcoded
    timeout would have used, or a legitimate long render gets killed
    mid-way and loses already-completed shots to a false B12."""
    total_frames_ten_10s_shots_24fps = 10 * (10 * 24 + 1)  # раздел 6.4 n_render formula
    timeout = renderer.compute_chain_render_timeout(total_frames_ten_10s_shots_24fps)
    assert timeout > 900.0


def test_compute_chain_render_timeout_monotonic_in_frame_count():
    assert renderer.compute_chain_render_timeout(5000) > renderer.compute_chain_render_timeout(500)


# === scope (раздел 10.2 "Параметр scope") =====================================


def _chains_fixture():
    return [
        {"chain_id": "sc01_ch01", "scene_number": 1, "shots": [{"shot_number": 1}, {"shot_number": 2}]},
        {"chain_id": "sc01_ch02", "scene_number": 1, "shots": [{"shot_number": 3}]},
        {"chain_id": "sc02_ch01", "scene_number": 2, "shots": [{"shot_number": 1}]},
    ]


def test_resolve_scope_all():
    ids, warning = renderer.resolve_scope("all", _chains_fixture())
    assert ids == ["sc01_ch01", "sc01_ch02", "sc02_ch01"]
    assert warning is None


def test_resolve_scope_scene_nn():
    ids, warning = renderer.resolve_scope("scene_01", _chains_fixture())
    assert ids == ["sc01_ch01", "sc01_ch02"]
    assert warning is None


def test_resolve_scope_scene_nn_shot_mm():
    ids, warning = renderer.resolve_scope("scene_01_shot_03", _chains_fixture())
    assert ids == ["sc01_ch02"]
    assert warning is None


def test_resolve_scope_chain_prefixed_form():
    ids, warning = renderer.resolve_scope("chain_sc02_ch01", _chains_fixture())
    assert ids == ["sc02_ch01"]
    assert warning is None


def test_resolve_scope_unrecognized_falls_back_to_all_with_warning():
    ids, warning = renderer.resolve_scope("bogus", _chains_fixture())
    assert ids == ["sc01_ch01", "sc01_ch02", "sc02_ch01"]
    assert warning is not None
    assert warning["level"] == "warning"


# === manifest.json / freshness (раздел 10.2 п.7) ==============================


def test_build_manifest_schema_matches_tz_example_shape():
    manifest = renderer.build_manifest(
        scene_number=1, shot_number=2, chain_id="sc01_ch01", duration_s=5, fps=24,
        frames_rendered=121, spec_hash="sha256:9f2c", resolution=[1280, 720], t_start_in_chain=5.0,
        junction_with_prev={"status": "exact"}, junction_with_next=None,
        assets_used=[{"asset_id": "humanoid_adult", "asset_version": 1}],
        warnings=["P01: заглушка вместо объекта «Стажёр Кеша»"], blender_version="4.5.1",
    )
    assert set(manifest.keys()) == {
        "scene_number", "shot_number", "chain_id", "duration_s", "fps", "frames_rendered", "spec_hash",
        "resolution", "t_start_in_chain", "junction_with_prev", "junction_with_next", "assets_used",
        "warnings", "rendered_at", "blender_version", "view_transform", "render_aa", "p10_acknowledged",
    }
    assert manifest["junction_with_prev"] == {"status": "exact"}
    assert manifest["junction_with_next"] is None
    assert manifest["view_transform"] == "Standard"
    assert manifest["render_aa"] == "8"
    assert manifest["p10_acknowledged"] is None
    assert manifest["rendered_at"].endswith("Z")


def test_is_manifest_current_true_when_all_fields_match():
    manifest = {"duration_s": 5, "fps": 24, "resolution": [1280, 720], "blender_version": "4.5.1", "spec_hash": "sha256:x"}
    assert renderer.is_manifest_current(
        manifest, duration_s=5, fps=24, resolution=[1280, 720], blender_version="4.5.1", spec_hash="sha256:x"
    ) is True


def test_is_manifest_current_false_when_spec_hash_differs():
    manifest = {"duration_s": 5, "fps": 24, "resolution": [1280, 720], "blender_version": "4.5.1", "spec_hash": "sha256:x"}
    assert renderer.is_manifest_current(
        manifest, duration_s=5, fps=24, resolution=[1280, 720], blender_version="4.5.1", spec_hash="sha256:DIFFERENT"
    ) is False


def test_is_manifest_current_false_when_none():
    assert renderer.is_manifest_current(None, duration_s=5, fps=24, resolution=[1280, 720], blender_version="4.5.1", spec_hash="x") is False


def test_compute_spec_hash_stable_and_order_independent():
    a = renderer.compute_spec_hash({"chain_id": "x", "objects": []})
    b = renderer.compute_spec_hash({"objects": [], "chain_id": "x"})
    assert a == b
    assert a.startswith("sha256:")


def test_compute_spec_hash_changes_with_content():
    a = renderer.compute_spec_hash({"chain_id": "x"})
    b = renderer.compute_spec_hash({"chain_id": "y"})
    assert a != b


# === assets_used (Предупреждение 9, раздел 10.2 п.7, Приложение Б п.8) =======


def test_collect_chain_assets_used_dedupes_and_reads_current_version(tmp_path):
    """assets_used must be populated from the chain's real library objects
    (asset_id + CURRENT asset_version from the shared index), deduplicated
    by asset_id, excluding proxies and asset_ids missing from the library
    -- reusing blockout_assets.read_index/find_asset_by_id rather than
    re-deriving asset resolution here."""
    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    _write(assets_root / "index.json", {
        "version": 1, "generated_at": None,
        "objects": [
            {"id": "humanoid_adult", "asset_version": 3, "path": "humanoid_adult"},
            {"id": "chair_01", "asset_version": 1, "path": "chair_01"},
        ],
    })

    chain_objects = [
        {"instance_id": "hero", "asset_id": "humanoid_adult"},
        {"instance_id": "hero_copy", "asset_id": "humanoid_adult"},  # duplicate -> deduped
        {"instance_id": "chair", "asset_id": "chair_01"},
        {"instance_id": "generic_prop", "asset_id": "__proxy_box__", "proxy": True},  # proxy -> excluded
        {"instance_id": "unresolved", "asset_id": "not_in_library"},  # not in library -> excluded
        {"instance_id": "no_asset"},  # no asset_id at all -> excluded
    ]

    result = renderer.collect_chain_assets_used(chain_objects, assets_root=assets_root)
    assert result == [
        {"asset_id": "humanoid_adult", "asset_version": 3},
        {"asset_id": "chair_01", "asset_version": 1},
    ]


def test_collect_chain_assets_used_empty_for_no_objects():
    assert renderer.collect_chain_assets_used([], assets_root=Path("/does/not/matter")) == []


# === ffmpeg (раздел 16.1) =====================================================


def test_build_ffmpeg_args_matches_tz_command_verbatim():
    args = renderer.build_ffmpeg_args(Path("/proj/shot/frames"), 24, 120, Path("/proj/shot/blockout_ref.mp4"))
    assert args == [
        "ffmpeg", "-y",
        "-framerate", "24",
        "-start_number", "1",
        "-i", "/proj/shot/frames/frame_%04d.png",
        "-frames:v", "120",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "16",
        "-preset", "slow",
        "-movflags", "+faststart",
        "/proj/shot/blockout_ref.mp4",
    ]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed in this environment")
def test_build_ffmpeg_args_actually_runs_against_real_frames(tmp_path):
    """Real (non-mocked) integration check: two tiny PNG frames really get
    encoded by the real ffmpeg binary into an mp4 with exactly N_video
    frames -- validates the exact раздел 16.1 command, not just its
    argv shape."""
    from PIL import Image

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    for i in (1, 2):
        Image.new("RGB", (16, 12), color=(10 * i, 20 * i, 30 * i)).save(frames_dir / f"frame_{i:04d}.png")

    output_path = tmp_path / "blockout_ref.mp4"
    args = renderer.build_ffmpeg_args(frames_dir, 1, 1, output_path)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert output_path.is_file()
    assert output_path.stat().st_size > 0

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(output_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert probe.stdout.strip() == "1"


# === shots.json per-field merge (раздел 10.2 "Дисциплина записи") ============


def test_merge_write_shots_blockout_fields_preserves_other_fields(tmp_path):
    shots_path = tmp_path / "shots.json"
    _write(shots_path, {"items": [
        {"scene_number": 1, "shot_number": 1, "shot_type": "start", "output_path": "orig.png", "video_path": "orig.mp4", "timing": {"a": 1}},
        {"scene_number": 1, "shot_number": 1, "shot_type": "end", "output_path": "orig_end.png"},
    ]})
    renderer._merge_write_shots_blockout_fields(shots_path, {
        (1, 1, "start"): {"blockout_ref_image": "ref_start.png", "blockout_video": "v.mp4", "blockout_rendered_at": "2026-08-16T00:00:00Z"},
        (1, 1, "end"): {"blockout_ref_image": "ref_end.png", "blockout_video": "v.mp4", "blockout_rendered_at": "2026-08-16T00:00:00Z"},
    })
    data = _read(shots_path)
    start_item = next(i for i in data["items"] if i["shot_type"] == "start")
    end_item = next(i for i in data["items"] if i["shot_type"] == "end")
    assert start_item["output_path"] == "orig.png"
    assert start_item["video_path"] == "orig.mp4"
    assert start_item["timing"] == {"a": 1}
    assert start_item["blockout_ref_image"] == "ref_start.png"
    assert end_item["blockout_ref_image"] == "ref_end.png"
    assert end_item["output_path"] == "orig_end.png"
    assert list(tmp_path.glob("*.tmp")) == []


def test_merge_write_shots_blockout_fields_noop_on_empty_patches(tmp_path):
    shots_path = tmp_path / "shots.json"
    renderer._merge_write_shots_blockout_fields(shots_path, {})
    assert not shots_path.exists()


# === full orchestration happy path (module_callable mocked, real ffmpeg) =====


def test_full_run_success_writes_manifest_shots_and_report(tmp_path, monkeypatch):
    project_id = "e2e"
    base = tmp_path / project_id

    chain = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "shots": [{"shot_number": 1, "duration_s": 1, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    _write(base / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 1, "world": {}, "chains": [chain]})
    _write(
        base / "97_shots" / "shots.json",
        {"items": [{
            "scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 1,
            "width": 1280, "height": 720, "output_path": "orig.png", "video_path": "orig.mp4",
        }]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})

    shot_dir = base / "93_blockout" / "scene_01_shot_01"
    frames_dir = shot_dir / "frames"
    frames_dir.mkdir(parents=True)
    from PIL import Image

    Image.new("RGB", (16, 12), color=(1, 2, 3)).save(frames_dir / "frame_0001.png")
    Image.new("RGB", (16, 12), color=(4, 5, 6)).save(frames_dir / "frame_0002.png")

    def _fake_run_blender_script(script_path, payload, output_json_path, *, timeout, module_callable=None):
        return {
            "ok": True,
            "chain_warnings": ["P01: object 'hero' has no asset_id in scene_spec.json; rendered as generic box proxy"],
            "shots": {
                "scene_01_shot_01": {
                    "frames_written": 2,
                    "frame_files": ["frame_0001.png", "frame_0002.png"],
                    "resolution": [1280, 720],
                    "frame_0001_sha256": "sha256:aaa",
                    "frame_last_sha256": "sha256:bbb",
                    "ref_start_sha256": "sha256:aaa",
                    "ref_end_sha256": "sha256:bbb",
                }
            },
        }

    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", _fake_run_blender_script)

    result = renderer.blockout_renderer_tool(session_id="s", project_id=project_id, fps=1, resolution="1280x720", scope="all")

    assert result == {
        "status": "success", "shots_rendered": 1, "frames_total": 2, "junction_checks": [],
        "artifact_path": str(base / "93_blockout"),
    }

    manifest = _read(shot_dir / "manifest.json")
    assert manifest["scene_number"] == 1
    assert manifest["shot_number"] == 1
    assert manifest["chain_id"] == "sc01_ch01"
    assert manifest["duration_s"] == 1
    assert manifest["fps"] == 1
    assert manifest["frames_rendered"] == 2
    assert manifest["resolution"] == [1280, 720]
    assert manifest["junction_with_prev"] is None  # single-shot chain: both edges are chain boundaries
    assert manifest["junction_with_next"] is None
    assert manifest["blender_version"] == "4.5.1"
    assert any("P01" in w for w in manifest["warnings"])

    video_path = shot_dir / "blockout_ref.mp4"
    assert video_path.is_file()
    assert video_path.stat().st_size > 0

    shots_data = _read(base / "97_shots" / "shots.json")
    item = shots_data["items"][0]
    assert item["output_path"] == "orig.png"  # untouched, per-field merge
    assert item["blockout_ref_image"] == str(shot_dir / "ref_start.png")
    assert item["blockout_video"] == str(video_path)
    assert item["blockout_rendered_at"].endswith("Z")

    report = _read(base / "93_blockout" / "report.json")
    section = report["blockout_renderer"]
    assert section["status"] == "success"
    assert section["shots_rendered"] == 1
    assert section["frames_total"] == 2
    # A04 (раздел 22): single-shot chain has no internal junction to check
    # this run, so the soft-junction ratio is undefined (None), not 0.
    assert section["soft_junction_ratio"] is None
    codes = [c.get("code") for c in section["checks"]]
    assert "P13" in codes  # no_size_fields (video_model_caps.json is {})
    assert "P01" in codes
    assert "B03" not in codes and "B04" not in codes and "B07" not in codes and "B11" not in codes


def test_full_run_chain_launch_failure_is_partial_not_raised(tmp_path, monkeypatch):
    project_id = "launchfail"
    base = tmp_path / project_id
    chain = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "shots": [{"shot_number": 1, "duration_s": 1, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    _write(base / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 1, "world": {}, "chains": [chain]})
    _write(
        base / "97_shots" / "shots.json",
        {"items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720}]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})

    monkeypatch.setattr(
        renderer.blockout_common, "run_blender_script",
        lambda *a, **k: {"ok": False, "error": "blender crashed"},
    )

    result = renderer.blockout_renderer_tool(session_id="s", project_id=project_id, fps=1, resolution="1280x720")
    assert result["status"] == "partial"
    assert result["shots_rendered"] == 0

    report = _read(base / "93_blockout" / "report.json")
    codes = [c.get("code") for c in report["blockout_renderer"]["checks"]]
    assert "B12" in codes


# === world per chain (Ошибка 1, раздел 12) ====================================


def test_world_is_read_per_chain_not_from_scene_spec_root(tmp_path, monkeypatch):
    """scene_spec.json's root never has a "world" key (раздел 12) -- each
    chain carries its OWN world (ground/sun). Each chain sent to Blender
    must get its own chain's world, and a stray root-level "world" (if
    ever present) must be ignored entirely."""
    project_id = "world_per_chain"
    base = tmp_path / project_id

    chain1 = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "world": {"ground": {"size_m": 10}, "sun": {}},
        "shots": [{"shot_number": 1, "duration_s": 1, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    chain2 = {
        "chain_id": "sc02_ch01", "scene_number": 2, "camera_aspect": "16:9",
        "world": {"ground": {"size_m": 20}, "sun": {}},
        "shots": [{"shot_number": 1, "duration_s": 1, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    _write(base / "93_blockout" / "scene_spec.json", {
        "version": 1, "fps": 1,
        "world": {"ground": {"size_m": 999}, "sun": {}},  # root-level -- must be ignored
        "chains": [chain1, chain2],
    })
    _write(
        base / "97_shots" / "shots.json",
        {"items": [
            {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720},
            {"scene_number": 2, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720},
        ]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})

    seen_worlds = {}

    def _fake_run_blender_script(script_path, payload, output_json_path, *, timeout, module_callable=None):
        shot_key = payload["shots"][0]["shot_key"]
        seen_worlds[shot_key] = payload["chain"]["world"]
        return {"ok": False, "error": "not rendering in this test"}

    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", _fake_run_blender_script)

    renderer.blockout_renderer_tool(session_id="s", project_id=project_id, fps=1, resolution="1280x720", scope="all")

    assert seen_worlds["scene_01_shot_01"] == {"ground": {"size_m": 10}, "sun": {}}
    assert seen_worlds["scene_02_shot_01"] == {"ground": {"size_m": 20}, "sun": {}}


# === Предупреждение 6: stale report checks cleared on rerun ==================


def test_report_stale_checks_cleared_for_rerendered_chain_only(tmp_path, monkeypatch):
    """A chain that failed once (B16) and is fixed and rerun with a
    narrower scope must not keep its stale error in report.json forever;
    another chain's checks (outside the current scope) must survive."""
    project_id = "clear_scope"
    base = tmp_path / project_id

    broken_chain = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "4:3",  # mismatches computed aspect -> B16
        "shots": [{"shot_number": 1, "duration_s": 1, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    other_chain = {
        "chain_id": "sc02_ch01", "scene_number": 2, "camera_aspect": "16:9",
        "shots": [{"shot_number": 1, "duration_s": 1, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    scene_spec_path = base / "93_blockout" / "scene_spec.json"
    _write(scene_spec_path, {"version": 1, "fps": 1, "chains": [broken_chain, other_chain]})
    _write(
        base / "97_shots" / "shots.json",
        {"items": [
            {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720},
            {"scene_number": 2, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720},
        ]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})
    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", lambda *a, **k: {"ok": False, "error": "stub"})

    # First run over the whole project: sc01_ch01 fails B16 (partial, not
    # raised), sc02_ch01 fails the stubbed B12.
    result1 = renderer.blockout_renderer_tool(session_id="s", project_id=project_id, fps=1, resolution="1280x720", scope="all")
    assert result1["status"] == "partial"
    report1 = _read(base / "93_blockout" / "report.json")
    codes1 = {(c.get("code"), c.get("chain_id")) for c in report1["blockout_renderer"]["checks"]}
    assert ("B16", "sc01_ch01") in codes1
    assert ("B12", "sc02_ch01") in codes1

    # "Fix" sc01_ch01's camera_aspect and rerun ONLY that chain.
    broken_chain["camera_aspect"] = "16:9"
    _write(scene_spec_path, {"version": 1, "fps": 1, "chains": [broken_chain, other_chain]})
    result2 = renderer.blockout_renderer_tool(
        session_id="s", project_id=project_id, fps=1, resolution="1280x720", scope="chain_sc01_ch01",
    )
    assert result2["status"] == "partial"  # now fails later, at the stubbed B12

    report2 = _read(base / "93_blockout" / "report.json")
    codes2 = {(c.get("code"), c.get("chain_id")) for c in report2["blockout_renderer"]["checks"]}
    assert ("B16", "sc01_ch01") not in codes2  # stale entry cleared by the rerun
    assert ("B12", "sc01_ch01") in codes2  # fresh entry from this run
    assert ("B12", "sc02_ch01") in codes2  # untouched -- outside this run's scope


# === Ошибка 4: B05 wired into the actual render path ==========================


def test_b05_mismatched_frame_sizes_on_disk_raises_and_records_report(tmp_path, monkeypatch):
    """check_b05() exists and is unit-tested, but A03 requires it to
    actually run in the render path -- two rendered frames with different
    pixel sizes on disk must fail the step with a B05 report entry."""
    project_id = "b05fail"
    base = tmp_path / project_id
    chain = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "shots": [{"shot_number": 1, "duration_s": 1, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    _write(base / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 1, "chains": [chain]})
    _write(
        base / "97_shots" / "shots.json",
        {"items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720}]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})

    shot_dir = base / "93_blockout" / "scene_01_shot_01"
    frames_dir = shot_dir / "frames"
    frames_dir.mkdir(parents=True)
    from PIL import Image

    Image.new("RGB", (16, 12), color=(1, 2, 3)).save(frames_dir / "frame_0001.png")
    Image.new("RGB", (8, 6), color=(4, 5, 6)).save(frames_dir / "frame_0002.png")  # mismatched size

    def _fake_run_blender_script(script_path, payload, output_json_path, *, timeout, module_callable=None):
        return {
            "ok": True, "chain_warnings": [],
            "shots": {"scene_01_shot_01": {
                "frames_written": 2,
                "frame_files": ["frame_0001.png", "frame_0002.png"],
                "resolution": [1280, 720],
                "frame_0001_sha256": "sha256:aaa", "frame_last_sha256": "sha256:bbb",
                "ref_start_sha256": "sha256:aaa", "ref_end_sha256": "sha256:bbb",
            }},
        }

    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", _fake_run_blender_script)

    with pytest.raises(RuntimeError, match="B05"):
        renderer.blockout_renderer_tool(session_id="s", project_id=project_id, fps=1, resolution="1280x720")

    report = _read(base / "93_blockout" / "report.json")
    codes = [c.get("code") for c in report["blockout_renderer"]["checks"]]
    assert "B05" in codes


# === Э4 code review, "ОШИБКА (главная)": shots.json patches must not be
# lost when a LATER chain in the same run fails a blocking check ==============


def test_shots_json_patches_survive_a_later_chain_b03_failure(tmp_path, monkeypatch):
    """раздел 13.4 ("провал цепочки... остальные цепочки продолжают
    рендериться") + раздел 10.3.1 (B03 raises) + раздел 13.4's promise that
    the rest of the run keeps its result: two chains, scope="all". The
    first chain renders successfully; the second fails B03 and raises.
    The first chain's shots.json fields (blockout_ref_image/
    blockout_video/blockout_rendered_at) must already be on disk when the
    exception propagates -- they are written per chain (Pass 2b), not
    batched for the whole run. Without the fix, this shot's fields are
    lost entirely because the run never reaches the end-of-run batch
    write."""
    project_id = "partial_chain_persist"
    base = tmp_path / project_id

    chain_ok = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "shots": [{"shot_number": 1, "duration_s": 1, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    chain_bad = {
        "chain_id": "sc02_ch01", "scene_number": 2, "camera_aspect": "16:9",
        "shots": [{"shot_number": 1, "duration_s": 1, "t_start": 0.0}],
        "objects": [], "camera": {},
    }
    _write(base / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 1, "chains": [chain_ok, chain_bad]})
    _write(
        base / "97_shots" / "shots.json",
        {"items": [
            {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720},
            {"scene_number": 2, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720},
        ]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})

    shot1_dir = base / "93_blockout" / "scene_01_shot_01"
    frames_dir = shot1_dir / "frames"
    frames_dir.mkdir(parents=True)
    from PIL import Image

    Image.new("RGB", (16, 12), color=(1, 2, 3)).save(frames_dir / "frame_0001.png")
    Image.new("RGB", (16, 12), color=(4, 5, 6)).save(frames_dir / "frame_0002.png")

    def _fake_run_blender_script(script_path, payload, output_json_path, *, timeout, module_callable=None):
        if payload["chain_id"] == "sc01_ch01":
            return {
                "ok": True, "chain_warnings": [],
                "shots": {"scene_01_shot_01": {
                    "frames_written": 2, "frame_files": ["frame_0001.png", "frame_0002.png"],
                    "resolution": [1280, 720],
                    "frame_0001_sha256": "sha:aaa", "frame_last_sha256": "sha:bbb",
                    "ref_start_sha256": "sha:aaa", "ref_end_sha256": "sha:bbb",
                }},
            }
        # sc02_ch01: Blender returns fewer frames than N_render (2 expected) -> B03.
        return {
            "ok": True, "chain_warnings": [],
            "shots": {"scene_02_shot_01": {
                "frames_written": 1, "frame_files": ["frame_0001.png"],
                "resolution": [1280, 720],
                "frame_0001_sha256": "sha:x", "frame_last_sha256": "sha:y",
                "ref_start_sha256": "sha:x", "ref_end_sha256": "sha:y",
            }},
        }

    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", _fake_run_blender_script)

    with pytest.raises(RuntimeError, match="B03"):
        renderer.blockout_renderer_tool(
            session_id="s", project_id=project_id, fps=1, resolution="1280x720", scope="all",
        )

    shots_data = _read(base / "97_shots" / "shots.json")
    by_key = {(i["scene_number"], i["shot_number"], i["shot_type"]): i for i in shots_data["items"]}
    ok_item = by_key[(1, 1, "start")]
    assert ok_item["blockout_ref_image"] == str(shot1_dir / "ref_start.png")
    assert ok_item["blockout_video"] == str(shot1_dir / "blockout_ref.mp4")
    assert ok_item["blockout_rendered_at"].endswith("Z")

    # The failed chain's shot must NOT have been patched.
    bad_item = by_key[(2, 1, "start")]
    assert "blockout_ref_image" not in bad_item

    # manifest.json for the successful chain was written too (Pass 2b ran
    # to completion for it before sc02_ch01 was even started).
    assert (shot1_dir / "manifest.json").is_file()


# === Предупреждение 9: assets_used integration ================================


def test_full_run_populates_assets_used_in_manifest(tmp_path, monkeypatch):
    """assets_used in manifest.json must be populated from the chain's
    real library objects, not left as [] regardless of scene_spec.json
    content (раздел 10.2 п.7, Приложение Б п.8)."""
    project_id = "assets_used_e2e"
    base = tmp_path / project_id

    assets_root = tmp_path / "assets_lib"
    assets_root.mkdir()
    _write(assets_root / "index.json", {
        "version": 1, "generated_at": None,
        "objects": [{"id": "humanoid_adult", "asset_version": 2, "path": "humanoid_adult"}],
    })
    monkeypatch.setenv("BLOCKOUT_ASSETS_DIR", str(assets_root))

    chain = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "shots": [{"shot_number": 1, "duration_s": 1, "t_start": 0.0}],
        "objects": [{"instance_id": "hero", "asset_id": "humanoid_adult"}], "camera": {},
    }
    _write(base / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 1, "chains": [chain]})
    _write(
        base / "97_shots" / "shots.json",
        {"items": [{"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720}]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})

    shot_dir = base / "93_blockout" / "scene_01_shot_01"
    frames_dir = shot_dir / "frames"
    frames_dir.mkdir(parents=True)
    from PIL import Image

    Image.new("RGB", (16, 12), color=(1, 2, 3)).save(frames_dir / "frame_0001.png")
    Image.new("RGB", (16, 12), color=(4, 5, 6)).save(frames_dir / "frame_0002.png")

    def _fake_run_blender_script(script_path, payload, output_json_path, *, timeout, module_callable=None):
        return {
            "ok": True, "chain_warnings": [],
            "shots": {"scene_01_shot_01": {
                "frames_written": 2,
                "frame_files": ["frame_0001.png", "frame_0002.png"],
                "resolution": [1280, 720],
                "frame_0001_sha256": "sha256:aaa", "frame_last_sha256": "sha256:bbb",
                "ref_start_sha256": "sha256:aaa", "ref_end_sha256": "sha256:bbb",
            }},
        }

    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", _fake_run_blender_script)

    renderer.blockout_renderer_tool(session_id="s", project_id=project_id, fps=1, resolution="1280x720")

    manifest = _read(shot_dir / "manifest.json")
    assert manifest["assets_used"] == [{"asset_id": "humanoid_adult", "asset_version": 2}]


# === Э4 full orchestration: B06/P07 junction check on a real chain ===========


def test_full_run_three_shot_chain_junction_checks_exact_and_soft(tmp_path, monkeypatch):
    """A04 / раздел 21 Э4 acceptance bar: "B06 проходит на цепочке из трёх
    шотов" -- exercises both non-failing outcomes (sha256 fast path
    "exact" at the 1<->2 junction, soft pixel-threshold pass at the 2<->3
    junction) across a real three-shot chain, and checks that a soft P07
    is duplicated into BOTH neighbouring shots' manifest.json (раздел
    20.3, "Пошотные предупреждения дублируются в manifest.json шота")."""
    project_id = "three_shot_chain"
    base = tmp_path / project_id
    chain = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "shots": [
            {"shot_number": 1, "duration_s": 1, "t_start": 0.0},
            {"shot_number": 2, "duration_s": 1, "t_start": 1.0},
            {"shot_number": 3, "duration_s": 1, "t_start": 2.0},
        ],
        "objects": [], "camera": {},
    }
    _write(base / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 1, "chains": [chain]})
    _write(
        base / "97_shots" / "shots.json",
        {"items": [
            {"scene_number": 1, "shot_number": n, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720}
            for n in (1, 2, 3)
        ]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})

    dirs = {n: base / "93_blockout" / f"scene_01_shot_{n:02d}" for n in (1, 2, 3)}

    _make_flat_image(dirs[1] / "frames" / "frame_0001.png", (100, 10), (50, 50, 50))
    _make_flat_image(dirs[1] / "frames" / "frame_0002.png", (100, 10), (50, 50, 50))
    _make_flat_image(dirs[2] / "frames" / "frame_0001.png", (100, 10), (50, 50, 50))
    _make_flat_image(dirs[2] / "frames" / "frame_0002.png", (100, 10), (100, 100, 100))
    _make_image_with_diff_pixels(dirs[3] / "frames" / "frame_0001.png", (100, 10), (100, 100, 100), diff_pixels=1, delta=1)
    _make_flat_image(dirs[3] / "frames" / "frame_0002.png", (100, 10), (100, 100, 100))

    def _shot_result(sha_first, sha_last):
        return {
            "frames_written": 2, "frame_files": ["frame_0001.png", "frame_0002.png"],
            "resolution": [1280, 720],
            "frame_0001_sha256": sha_first, "frame_last_sha256": sha_last,
            "ref_start_sha256": sha_first, "ref_end_sha256": sha_last,
        }

    def _fake_run_blender_script(script_path, payload, output_json_path, *, timeout, module_callable=None):
        return {"ok": True, "chain_warnings": [], "shots": {
            # shot1 <-> shot2: identical sha256 -> "exact" fast path.
            "scene_01_shot_01": _shot_result("sha:s1a", "sha:junction12"),
            "scene_01_shot_02": _shot_result("sha:junction12", "sha:s2b"),
            # shot2 <-> shot3: differing sha256 -> falls to the real pixel
            # diff prepared above (1/1000 px, delta 1 -> soft).
            "scene_01_shot_03": _shot_result("sha:s3a", "sha:s3b"),
        }}

    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", _fake_run_blender_script)

    result = renderer.blockout_renderer_tool(session_id="s", project_id=project_id, fps=1, resolution="1280x720", scope="all")

    assert result["status"] == "success"
    assert result["shots_rendered"] == 3
    assert result["junction_checks"] == [
        {"chain_id": "sc01_ch01", "scene_number": 1, "shot_number": 2, "prev_shot_number": 1, "status": "exact"},
        {
            "chain_id": "sc01_ch01", "scene_number": 1, "shot_number": 3, "prev_shot_number": 2,
            "status": "soft", "max_channel_diff": 1, "diff_pixels_ratio": pytest.approx(0.001),
        },
    ]

    manifest1 = _read(dirs[1] / "manifest.json")
    manifest2 = _read(dirs[2] / "manifest.json")
    manifest3 = _read(dirs[3] / "manifest.json")

    assert manifest1["junction_with_prev"] is None  # chain start boundary
    assert manifest1["junction_with_next"] == {"status": "exact"}

    assert manifest2["junction_with_prev"] == {"status": "exact"}
    assert manifest2["junction_with_next"]["status"] == "soft"
    assert any("P07" in w and "next" in w for w in manifest2["warnings"])

    assert manifest3["junction_with_prev"]["status"] == "soft"
    assert manifest3["junction_with_next"] is None  # chain end boundary
    assert any("P07" in w and "previous" in w for w in manifest3["warnings"])

    report = _read(base / "93_blockout" / "report.json")
    codes = [c.get("code") for c in report["blockout_renderer"]["checks"]]
    assert "P07" in codes
    assert "B06" not in codes
    # A04 (раздел 22): "доля стыков, прошедших только по мягкому порогу
    # (P07), фиксируется в отчёте" -- 1 soft out of 2 internal junctions.
    assert report["blockout_renderer"]["soft_junction_ratio"] == pytest.approx(0.5)

    shots_data = _read(base / "97_shots" / "shots.json")
    for item in shots_data["items"]:
        assert "blockout_junction_failed" not in item


def test_junction_check_sets_blockout_junction_failed_and_clears_on_rerun(tmp_path, monkeypatch):
    """раздел 15.3: a failed junction sets ``blockout_junction_failed`` on
    the RECEIVING shot's ``start`` AND ``end`` shots.json elements (never
    on the previous shot's elements), records B06 in report.json, and
    marks the run "partial" without raising (B06 is one of the three
    raise-exempt checks, раздел 10.3.1). Rerunning the SAME chain once the
    junction compares sha256-equal (e.g. the scene got fixed and re-baked)
    clears both the flag and the stale B06 report entry."""
    project_id = "junction_e2e"
    base = tmp_path / project_id
    chain = {
        "chain_id": "sc01_ch01", "scene_number": 1, "camera_aspect": "16:9",
        "shots": [
            {"shot_number": 1, "duration_s": 1, "t_start": 0.0},
            {"shot_number": 2, "duration_s": 1, "t_start": 1.0},
        ],
        "objects": [], "camera": {},
    }
    _write(base / "93_blockout" / "scene_spec.json", {"version": 1, "fps": 1, "chains": [chain]})
    _write(
        base / "97_shots" / "shots.json",
        {"items": [
            {"scene_number": 1, "shot_number": 1, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720},
            {"scene_number": 1, "shot_number": 1, "shot_type": "end", "duration_s": 1},
            {"scene_number": 1, "shot_number": 2, "shot_type": "start", "duration_s": 1, "width": 1280, "height": 720},
            {"scene_number": 1, "shot_number": 2, "shot_type": "end", "duration_s": 1},
        ]},
    )
    _write(base / "97_shots" / "video_model_caps.json", {})

    shot1_dir = base / "93_blockout" / "scene_01_shot_01"
    shot2_dir = base / "93_blockout" / "scene_01_shot_02"
    _make_flat_image(shot1_dir / "frames" / "frame_0001.png", (100, 10), (100, 100, 100))
    _make_flat_image(shot1_dir / "frames" / "frame_0002.png", (100, 10), (100, 100, 100))
    _make_flat_image(shot2_dir / "frames" / "frame_0001.png", (100, 10), (200, 200, 200))  # far from shot1's last frame
    _make_flat_image(shot2_dir / "frames" / "frame_0002.png", (100, 10), (200, 200, 200))

    def _shot_result(sha_first, sha_last):
        return {
            "frames_written": 2, "frame_files": ["frame_0001.png", "frame_0002.png"],
            "resolution": [1280, 720],
            "frame_0001_sha256": sha_first, "frame_last_sha256": sha_last,
            "ref_start_sha256": sha_first, "ref_end_sha256": sha_last,
        }

    def _fake_run_blender_script_fail(script_path, payload, output_json_path, *, timeout, module_callable=None):
        return {"ok": True, "chain_warnings": [], "shots": {
            "scene_01_shot_01": _shot_result("sha:a1", "sha:a2"),
            "scene_01_shot_02": _shot_result("sha:b1", "sha:b2"),
        }}

    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", _fake_run_blender_script_fail)

    result1 = renderer.blockout_renderer_tool(session_id="s", project_id=project_id, fps=1, resolution="1280x720", scope="all")
    assert result1["status"] == "partial"
    assert result1["junction_checks"] == [{
        "chain_id": "sc01_ch01", "scene_number": 1, "shot_number": 2, "prev_shot_number": 1,
        "status": "failed", "max_channel_diff": 100, "diff_pixels_ratio": pytest.approx(1.0),
    }]

    report1 = _read(base / "93_blockout" / "report.json")
    codes1 = [c.get("code") for c in report1["blockout_renderer"]["checks"]]
    assert "B06" in codes1

    shots1 = _read(base / "97_shots" / "shots.json")
    by_key1 = {(i["shot_number"], i["shot_type"]): i for i in shots1["items"]}
    assert by_key1[(2, "start")]["blockout_junction_failed"] is True
    assert by_key1[(2, "end")]["blockout_junction_failed"] is True
    assert "blockout_junction_failed" not in by_key1[(1, "start")]
    assert "blockout_junction_failed" not in by_key1[(1, "end")]

    # Rerun the same chain with the junction now sha256-identical -- both
    # the flag and the stale B06 entry must clear.
    def _fake_run_blender_script_pass(script_path, payload, output_json_path, *, timeout, module_callable=None):
        return {"ok": True, "chain_warnings": [], "shots": {
            "scene_01_shot_01": _shot_result("sha:a1", "sha:same"),
            "scene_01_shot_02": _shot_result("sha:same", "sha:b2"),
        }}

    monkeypatch.setattr(renderer.blockout_common, "run_blender_script", _fake_run_blender_script_pass)
    result2 = renderer.blockout_renderer_tool(
        session_id="s", project_id=project_id, fps=1, resolution="1280x720", scope="chain_sc01_ch01",
    )
    assert result2["status"] == "success"
    assert result2["junction_checks"] == [{
        "chain_id": "sc01_ch01", "scene_number": 1, "shot_number": 2, "prev_shot_number": 1, "status": "exact",
    }]

    report2 = _read(base / "93_blockout" / "report.json")
    codes2 = [c.get("code") for c in report2["blockout_renderer"]["checks"]]
    assert "B06" not in codes2

    shots2 = _read(base / "97_shots" / "shots.json")
    by_key2 = {(i["shot_number"], i["shot_type"]): i for i in shots2["items"]}
    assert "blockout_junction_failed" not in by_key2[(2, "start")]
    assert "blockout_junction_failed" not in by_key2[(2, "end")]
