"""Э2: тесты чистых функций blockout_common.py — временная шкала, цепочки,
камера, кеш-хеш, доступность Blender (без Blender/LLM/сети).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from custom_tools.storybook import blockout_common as bc


# === timeline / frame formulas ==============================================


def test_n_render_and_n_video_table():
    # раздел 6.4
    assert bc.n_render(5, 24) == 121
    assert bc.n_video(5, 24) == 120
    assert bc.n_render(7, 24) == 169
    assert bc.n_video(7, 24) == 168
    assert bc.n_render(10, 24) == 241
    assert bc.n_video(10, 24) == 240


def test_frame_local_time_and_next_chain_t_start():
    assert bc.frame_local_time(5.0, 1, 24) == 5.0
    assert bc.frame_local_time(5.0, 121, 24) == pytest.approx(10.0)
    assert bc.next_chain_t_start(5.0, 5) == 10.0
    assert bc.next_chain_t_start(0.0, 5) == 5.0


def test_frame_blender_and_shot_window_frames():
    assert bc.frame_blender(0.0, 24) == 1
    assert bc.frame_blender(5.0, 24) == 121
    assert bc.shot_window_frames(5.0, 5, 24) == (121, 241)


def test_is_on_frame_grid_tolerance_examples():
    # раздел 20.1 B09: 7/25*25 == 7.000000000000001; 31/30*30 == 31.000000000000004
    t25 = 7 / 25
    assert bc.is_on_frame_grid(t25, 25)
    t30 = 31 / 30
    assert bc.is_on_frame_grid(t30, 30)
    assert not bc.is_on_frame_grid(0.33, 24)


def test_snap_to_frame_grid():
    assert bc.snap_to_frame_grid(0.33, 24) == pytest.approx(round(0.33 * 24) / 24)
    assert bc.snap_to_frame_grid(0.34, 24) == pytest.approx(round(0.34 * 24) / 24)
    # both land on the same 8th frame at 24fps
    assert bc.snap_to_frame_grid(0.33, 24) == bc.snap_to_frame_grid(0.34, 24)


# === chains (раздел 7) =======================================================


def _el(scene, shot, shot_type, duration_s=None, link_type=None):
    d = {"scene_number": scene, "shot_number": shot, "shot_type": shot_type}
    if duration_s is not None:
        d["duration_s"] = duration_s
    if link_type is not None:
        d["link_type"] = link_type
    return d


def test_effective_link_type_reads_only_start_element():
    # end-элемент всегда independent в данных, но мы читаем именно start
    elements = [
        _el(1, 1, "end", link_type="independent"),
        _el(1, 1, "start", link_type="full_copy"),
    ]
    link_type, used_fallback = bc.effective_link_type(elements)
    assert link_type == "full_copy"
    assert used_fallback is False


def test_effective_link_type_missing_start_falls_back():
    elements = [_el(1, 1, "end", link_type="independent")]
    link_type, used_fallback = bc.effective_link_type(elements)
    assert link_type == "independent"
    assert used_fallback is True


def test_effective_link_type_unknown_value_falls_back():
    elements = [_el(1, 1, "start", link_type="weird")]
    link_type, used_fallback = bc.effective_link_type(elements)
    assert link_type == "independent"
    assert used_fallback is True


def test_compute_chains_three_shot_chain_all_link_types():
    # scene 01: shot01 independent, shot02 full_copy, shot03 full_copy | shot04 reference
    # (раздел 7.1 диаграмма)
    elements = []
    for shot, lt, dur in [(1, "independent", 5), (2, "full_copy", 5), (3, "full_copy", 5), (4, "reference", 10)]:
        elements.append(_el(1, shot, "start", duration_s=dur, link_type=lt))
        elements.append(_el(1, shot, "end", duration_s=dur, link_type="independent"))

    chains, warnings = bc.compute_chains(1, elements)

    assert [c.chain_id for c in chains] == ["sc01_ch01", "sc01_ch02"]
    ch1, ch2 = chains
    assert ch1.scene_number == 1
    assert [s.shot_number for s in ch1.shots] == [1, 2, 3]
    assert [s.t_start for s in ch1.shots] == [0.0, 5.0, 10.0]
    assert ch1.total_duration_s == 15
    assert [s.shot_number for s in ch2.shots] == [4]
    assert ch2.shots[0].t_start == 0.0
    assert ch2.total_duration_s == 10
    # first shot of the scene is independent here -> no "first shot full_copy" warning
    assert not any("first shot of scene" in w for w in warnings)


def test_compute_chains_first_shot_full_copy_warns_but_still_starts_chain():
    elements = [
        _el(1, 1, "start", duration_s=5, link_type="full_copy"),
        _el(1, 1, "end", duration_s=5, link_type="independent"),
    ]
    chains, warnings = bc.compute_chains(1, elements)
    assert len(chains) == 1
    assert chains[0].chain_id == "sc01_ch01"
    assert any("first shot of scene" in w for w in warnings)


def test_compute_chains_write_chains_json_roundtrip(tmp_path: Path):
    elements = [
        _el(2, 1, "start", duration_s=5, link_type="independent"),
        _el(2, 1, "end", duration_s=5, link_type="independent"),
        _el(2, 2, "start", duration_s=7, link_type="full_copy"),
        _el(2, 2, "end", duration_s=7, link_type="independent"),
    ]
    chains, _ = bc.compute_chains(2, elements)
    out_path = tmp_path / "93_blockout" / "chains.json"
    bc.write_chains_json(out_path, chains)

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data == {
        "chains": [
            {
                "chain_id": "sc02_ch01",
                "scene_number": 2,
                "shots": [
                    {"shot_number": 1, "duration_s": 5, "t_start": 0.0},
                    {"shot_number": 2, "duration_s": 7, "t_start": 5.0},
                ],
                "total_duration_s": 12,
            }
        ]
    }
    # no leftover tmp files
    assert list(out_path.parent.glob("*.tmp")) == []


# === camera geometry (раздел 10.1, 15.2 — worked MEDIUM SHOT example) ======


def test_camera_distance_medium_shot_16_9():
    h_sensor = bc.sensor_h_mm(36.0, 720, 1280)
    assert h_sensor == pytest.approx(20.25)
    d = bc.camera_distance_m(50.0, 0.90, 0.55, h_sensor)
    assert d == pytest.approx(4.04, abs=0.01)


def test_camera_distance_close_up_16_9():
    h_sensor = bc.sensor_h_mm(36.0, 720, 1280)
    d = bc.camera_distance_m(85.0, 0.30, 0.85, h_sensor)
    assert d == pytest.approx(1.48, abs=0.01)


def test_camera_rot_deg_worked_example():
    # раздел 15.2: location=[-0.72,-1.92,1.6], look_at=[2.13,0.93,1.5]
    rot_x, rot_z = bc.camera_rot_deg([-0.72, -1.92, 1.6], [2.13, 0.93, 1.5])
    assert rot_x == pytest.approx(88.58, abs=0.01)
    assert rot_z == pytest.approx(-45.00, abs=0.01)


def test_animation_phase_loop_true_fraction():
    # раздел 15.2: frac(10.0 / 1.2) = 0.3333
    phase = bc.animation_phase(0.0, 10.0, 0.0, 1.0, 1.2, loop=True)
    assert phase == pytest.approx(0.3333, abs=1e-3)


def test_animation_phase_loop_false_clamps_to_one():
    phase = bc.animation_phase(0.0, 10.0, 0.0, 1.0, 1.2, loop=False)
    assert phase == 1.0


def test_animation_phase_loop_false_below_one_passthrough():
    phase = bc.animation_phase(0.0, 0.5, 0.0, 1.0, 1.2, loop=False)
    assert phase == pytest.approx(0.5 / 1.2)


# === resolution matching (Приложение А) =====================================


def test_resolution_match_preserves_area_and_aspect():
    w_render, h_render = bc.resolution_match(1280, 720, 16, 9)
    assert w_render % 2 == 0 and h_render % 2 == 0
    assert w_render * 9 == h_render * 16  # ratio exact
    # area roughly preserved
    assert abs(w_render * h_render - 1280 * 720) / (1280 * 720) < 0.05


def test_resolution_match_minimum_k_is_one():
    w_render, h_render = bc.resolution_match(2, 2, 16, 9)
    assert (w_render, h_render) == (32, 18)


# === track evaluation / B08 / B09 / B10 ====================================


def test_track_covers_range_single_constant_key_shorthand():
    keys = [{"t": 0.0, "v": 1.0, "interp": "constant"}]
    assert bc.track_covers_range(keys, 15.0)


def test_track_covers_range_requires_full_span():
    keys = [{"t": 0.0, "v": 1.0, "interp": "linear"}, {"t": 10.0, "v": 2.0, "interp": "linear"}]
    assert not bc.track_covers_range(keys, 15.0)
    keys2 = [{"t": 0.0, "v": 1.0, "interp": "linear"}, {"t": 15.0, "v": 2.0, "interp": "linear"}]
    assert bc.track_covers_range(keys2, 15.0)


def test_track_keys_on_frame_grid():
    keys = [{"t": 0.0, "v": 1.0, "interp": "linear"}, {"t": 5.0, "v": 2.0, "interp": "linear"}]
    assert bc.track_keys_on_frame_grid(keys, 24)
    keys_bad = [{"t": 0.33, "v": 1.0, "interp": "linear"}]
    assert not bc.track_keys_on_frame_grid(keys_bad, 24)


def test_camera_continuity_ok_flat_lens_track():
    lens_keys = [
        {"t": 0.0, "v": 50.0, "interp": "linear"},
        {"t": 5.0, "v": 50.0, "interp": "linear"},
        {"t": 10.0, "v": 50.0, "interp": "linear"},
    ]
    loc_keys = [
        {"t": 0.0, "v": [0.0, 0.0, 1.6], "interp": "bezier"},
        {"t": 5.0, "v": [1.0, 1.0, 1.6], "interp": "bezier"},
        {"t": 10.0, "v": [2.0, 2.0, 1.6], "interp": "bezier"},
    ]
    look_keys = [
        {"t": 0.0, "v": [0.0, 0.0, 1.5], "interp": "linear"},
        {"t": 10.0, "v": [2.0, 2.0, 1.5], "interp": "linear"},
    ]
    assert bc.camera_continuity_ok(loc_keys, look_keys, lens_keys, 5.0, 24)


def test_camera_continuity_detects_lens_snap_zoom_break_at_boundary():
    # 50 -> 85mm over 7 frames ending exactly at the junction: derivative
    # jumps from ~120mm/s to 0 right at t_junction -> should fail.
    fps = 24
    lens_keys = [
        {"t": 0.0, "v": 50.0, "interp": "linear"},
        {"t": 5.0 - 7 / fps, "v": 50.0, "interp": "linear"},
        {"t": 5.0, "v": 85.0, "interp": "linear"},
        {"t": 10.0, "v": 85.0, "interp": "linear"},
    ]
    loc_keys = [{"t": 0.0, "v": [0.0, 0.0, 0.0], "interp": "constant"}]
    look_keys = [{"t": 0.0, "v": [0.0, 0.0, 0.0], "interp": "constant"}]
    assert not bc.camera_continuity_ok(loc_keys, look_keys, lens_keys, 5.0, fps)


# === cache hashing (раздел 10.1 «кеш») ======================================


def test_chain_input_hash_deterministic_and_order_independent():
    a = {"shots": [{"t_start": 0.0}], "objects": ["x"]}
    b = {"objects": ["x"], "shots": [{"t_start": 0.0}]}
    assert bc.chain_input_hash(a) == bc.chain_input_hash(b)


def test_chain_input_hash_changes_with_content():
    a = {"shots": [{"t_start": 0.0}]}
    b = {"shots": [{"t_start": 1.0}]}
    assert bc.chain_input_hash(a) != bc.chain_input_hash(b)


def test_chain_input_hash_format():
    h = bc.chain_input_hash({"a": 1})
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


# === Blender availability (B12) =============================================


def test_check_blender_available_binary_not_found(monkeypatch):
    monkeypatch.delenv("BLOCKOUT_BLENDER_BIN", raising=False)
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "binary")
    monkeypatch.setattr(bc.shutil, "which", lambda name: None)
    available, version, message = bc.check_blender_available()
    assert available is False
    assert version is None
    assert "not found" in message


def test_check_blender_available_binary_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "binary")
    monkeypatch.setenv("BLOCKOUT_BLENDER_BIN", "/usr/bin/fake-blender")

    class _Proc:
        returncode = 0
        stdout = "Blender 4.5.1\n\tbuild date: ...\n"
        stderr = ""

    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: _Proc())
    available, version, message = bc.check_blender_available()
    assert available is True
    assert version == "4.5"
    assert message == "ok"


def test_check_blender_available_binary_below_minimum(monkeypatch):
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "binary")
    monkeypatch.setenv("BLOCKOUT_BLENDER_BIN", "/usr/bin/fake-blender")

    class _Proc:
        returncode = 0
        stdout = "Blender 3.6.0\n"
        stderr = ""

    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: _Proc())
    available, version, message = bc.check_blender_available()
    assert available is False
    assert version == "3.6"
    assert "below minimum" in message


# === Blender launch contract (раздел 13.4) ==================================


def test_run_blender_script_binary_mode_missing_output(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "binary")
    monkeypatch.setenv("BLOCKOUT_BLENDER_BIN", "/usr/bin/fake-blender")

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: _Proc())
    output_path = tmp_path / "out" / "result.json"
    result = bc.run_blender_script(
        Path("/fake/script.py"), {"output_path": str(output_path)}, output_path, timeout=5
    )
    assert result["ok"] is False
    assert "exited with code" in result["error"]


def test_run_blender_script_binary_mode_reads_output(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "binary")
    monkeypatch.setenv("BLOCKOUT_BLENDER_BIN", "/usr/bin/fake-blender")
    output_path = tmp_path / "out" / "result.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _fake_run(*args, **kwargs):
        output_path.write_text(json.dumps({"ok": True, "value": 42}), encoding="utf-8")

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(bc.subprocess, "run", _fake_run)
    result = bc.run_blender_script(
        Path("/fake/script.py"), {"output_path": str(output_path)}, output_path, timeout=5
    )
    assert result == {"ok": True, "value": 42}


def test_run_blender_script_module_mode_calls_callable(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "module")
    output_path = tmp_path / "result.json"
    result = bc.run_blender_script(
        Path("/fake/script.py"),
        {"output_path": str(output_path)},
        output_path,
        timeout=5,
        module_callable=lambda payload: {"ok": True, "seen": payload["output_path"]},
    )
    assert result == {"ok": True, "seen": str(output_path)}


def test_run_blender_script_module_mode_without_callable_fails_gracefully(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "module")
    output_path = tmp_path / "result.json"
    result = bc.run_blender_script(Path("/fake/script.py"), {}, output_path, timeout=5)
    assert result["ok"] is False
    assert "module_callable" in result["error"]


def test_run_blender_script_binary_mode_nonzero_exit_preserves_clean_error_json(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "binary")
    monkeypatch.setenv("BLOCKOUT_BLENDER_BIN", "/usr/bin/fake-blender")
    output_path = tmp_path / "out" / "result.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _fake_run(*args, **kwargs):
        output_path.write_text(
            json.dumps({"ok": False, "error": "CAM_INSIDE: shot=x"}), encoding="utf-8"
        )

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "Blender error traceback"

        return _Proc()

    monkeypatch.setattr(bc.subprocess, "run", _fake_run)
    result = bc.run_blender_script(
        Path("/fake/script.py"), {"output_path": str(output_path)}, output_path, timeout=5
    )
    assert result == {"ok": False, "error": "CAM_INSIDE: shot=x"}


def test_run_blender_script_binary_mode_nonzero_exit_no_output_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "binary")
    monkeypatch.setenv("BLOCKOUT_BLENDER_BIN", "/usr/bin/fake-blender")

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "segfault"

    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: _Proc())
    output_path = tmp_path / "out" / "result.json"
    result = bc.run_blender_script(
        Path("/fake/script.py"), {"output_path": str(output_path)}, output_path, timeout=5
    )
    assert result["ok"] is False
    assert "exited with code 1" in result["error"]
    assert "segfault" in result["error"]


def test_run_blender_script_binary_mode_output_json_not_dict_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "binary")
    monkeypatch.setenv("BLOCKOUT_BLENDER_BIN", "/usr/bin/fake-blender")
    output_path = tmp_path / "out" / "result.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _fake_run(*args, **kwargs):
        output_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "real blender error"

        return _Proc()

    monkeypatch.setattr(bc.subprocess, "run", _fake_run)
    result = bc.run_blender_script(
        Path("/fake/script.py"), {"output_path": str(output_path)}, output_path, timeout=5
    )
    assert result["ok"] is False
    assert "real blender error" in result["error"]
    assert "not an object" in result["error"]


def test_run_blender_script_never_raises_on_exception(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOCKOUT_BLENDER_MODE", "binary")
    monkeypatch.setenv("BLOCKOUT_BLENDER_BIN", "/usr/bin/fake-blender")

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(bc.subprocess, "run", _boom)
    output_path = tmp_path / "out" / "result.json"
    result = bc.run_blender_script(
        Path("/fake/script.py"), {"output_path": str(output_path)}, output_path, timeout=5
    )
    assert result == {"ok": False, "error": "boom"}
