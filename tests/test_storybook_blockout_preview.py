"""Э6: тесты blockout_preview.py -- сплошная склейка отрендеренных болванок
(раздел 17), burn-in подписи (раздел 16.1/17), contact sheet (раздел 22,
A11), нулевой контракт (раздел 10.3.1), report.json (раздел 20.3).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from custom_tools.storybook import blockout_preview as bp


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _chain(chain_id, scene_number, shots):
    return {
        "chain_id": chain_id,
        "scene_number": scene_number,
        "shots": shots,
        "total_duration_s": sum(s["duration_s"] for s in shots),
    }


def _shot(shot_number, duration_s, t_start=0.0):
    return {"shot_number": shot_number, "duration_s": duration_s, "t_start": t_start}


def _chains_payload(chains):
    return {"chains": chains}


def _make_video(path: Path, *, duration_s: int, fps: int = 24, w: int = 64, h: int = 36, color: str = "red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c={color}:s={w}x{h}:d={duration_s}:r={fps}",
        "-pix_fmt", "yuv420p", str(path),
    ]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    return tmp_path


# === chains.json -> общий порядок шотов (раздел 17.2) ==========================


def test_shots_from_chains_payload_orders_by_scene_then_shot_regardless_of_file_order():
    payload = _chains_payload([
        _chain("sc02_ch01", 2, [_shot(1, 5)]),
        _chain("sc01_ch02", 1, [_shot(3, 7)]),
        _chain("sc01_ch01", 1, [_shot(1, 5), _shot(2, 5)]),
    ])
    shots = bp.shots_from_chains_payload(payload)
    assert [(s["scene_number"], s["shot_number"]) for s in shots] == [(1, 1), (1, 2), (1, 3), (2, 1)]
    assert shots[0]["is_first_in_chain"] is True
    assert shots[1]["is_first_in_chain"] is False
    assert shots[2]["chain_id"] == "sc01_ch02"


def test_shots_from_chains_payload_sorts_double_digit_shot_numbers_numerically():
    """правка 4б regression: shot 10 must sort AFTER shot 2 within the same
    scene. A naive string sort would put "10" before "2" (lexical order) --
    the implementation casts shot_number to int before sorting, so this
    only guards against that regressing."""
    payload = _chains_payload([
        _chain("sc01_ch01", 1, [_shot(1, 5), _shot(2, 5)]),
        _chain("sc01_ch02", 1, [_shot(10, 5)]),
    ])
    shots = bp.shots_from_chains_payload(payload)
    assert [s["shot_number"] for s in shots] == [1, 2, 10]


def test_shots_from_chains_payload_handles_missing_or_malformed():
    assert bp.shots_from_chains_payload(None) == []
    assert bp.shots_from_chains_payload({}) == []
    assert bp.shots_from_chains_payload({"chains": "not-a-list"}) == []
    assert bp.shots_from_chains_payload({"chains": [{"chain_id": "c", "scene_number": "bad", "shots": [_shot(1, 5)]}]}) == []


# === fps/resolution сверка с первым включённым (раздел 17.2) ==================


def test_plan_segment_reencodes_all_matching_first_segment_no_reencode():
    segments = [{"fps": 24, "resolution": [1280, 720]}, {"fps": 24, "resolution": [1280, 720]}]
    planned = bp.plan_segment_reencodes(segments)
    assert [p["needs_reencode"] for p in planned] == [False, False]


def test_plan_segment_reencodes_mismatch_flagged_against_first_known():
    segments = [
        {"fps": 24, "resolution": [1280, 720]},
        {"fps": 30, "resolution": [1920, 1080]},
        {"fps": 24, "resolution": [1280, 720]},
    ]
    planned = bp.plan_segment_reencodes(segments)
    assert planned[0]["needs_reencode"] is False
    assert planned[1]["needs_reencode"] is True
    assert planned[1]["target_fps"] == 24
    assert planned[1]["target_resolution"] == [1280, 720]
    assert planned[2]["needs_reencode"] is False


def test_plan_segment_reencodes_unknown_manifest_included_as_is():
    segments = [{"fps": None, "resolution": None}, {"fps": 24, "resolution": [1280, 720]}]
    planned = bp.plan_segment_reencodes(segments)
    assert planned[0]["needs_reencode"] is False
    assert planned[1]["needs_reencode"] is False


# === drawtext burn-in (экранирование проверено эмпирически реальным ffmpeg) ===


def test_escape_drawtext_text_escapes_colon_and_middle_dot():
    assert bp.escape_drawtext_text("00:05 · x") == "00\\:05 \\· x"


def test_escape_drawtext_text_escapes_single_quote_and_backslash():
    """правка 4а: text is interpolated into ``text='...'`` inside the
    drawtext filter, so an unescaped ``'`` would terminate the quoted
    string early and an unescaped ``\\`` would be consumed by ffmpeg's own
    escaping -- both must be backslash-escaped like ':' and '·' already are."""
    assert bp.escape_drawtext_text("it's") == "it\\'s"
    assert bp.escape_drawtext_text("a\\b") == "a\\\\b"


# === per-shot two-line burn-in (P2.11) =========================================


def test_shot_burnin_summary_text():
    assert bp.shot_burnin_summary_text(3, 6, 5.0) == "SC 03·SH 06·5s"


def test_build_shot_burnin_line2_filter_uses_eif_not_expr():
    """R-PREV: ffmpeg's %{expr\\:...} macro always prints 6 decimals, so the
    line2 filter must use %{eif\\:...\\:d} instead, parametrized by fps."""
    filt = bp.build_shot_burnin_line2_filter("/font.ttf", fps=8, frame_count=40)
    assert "n/8" in filt
    assert "eif" in filt


def test_build_shot_burnin_filter_chain_joins_two_drawtext_lines_with_font():
    chain = bp.build_shot_burnin_filter_chain(1, 2, 5, 24, 120, "/font.ttf")
    parts = chain.split(",drawtext=")
    assert len(parts) == 2
    assert chain.count("fontfile='/font.ttf'") == 2


# === ffmpeg argv builders (раздел 16.1 -- только формат команд планов Б/В) ====


def test_build_concat_list_lines_escapes_single_quote():
    lines = bp.build_concat_list_lines([Path("/a/b.mp4"), Path("/a/it's.mp4")])
    assert lines[0] == "file '/a/b.mp4'"
    assert lines[1] == "file '/a/it'\\''s.mp4'"


def test_build_concat_ffmpeg_args():
    args = bp.build_concat_ffmpeg_args(Path("/tmp/list.txt"), Path("/tmp/out.mp4"))
    assert args == ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "/tmp/list.txt", "-c", "copy", "/tmp/out.mp4"]


def test_build_reencode_ffmpeg_args_scales_and_sets_fps():
    args = bp.build_reencode_ffmpeg_args(Path("/a.mp4"), Path("/b.mp4"), 24, 1280, 720)
    assert "-vf" in args and "scale=1280:720" in args
    assert "-r" in args and "24" in args
    assert args[-1] == "/b.mp4"


def test_build_burnin_ffmpeg_args_matches_plan_tokens():
    args = bp.build_burnin_ffmpeg_args(Path("/in.mp4"), Path("/out.mp4"), "drawtext=...")
    assert "-vf" in args
    assert "drawtext=..." in args
    assert "-c:v" in args
    assert "libx264" in args
    assert "-crf" in args
    assert "18" in args
    assert args[-1] == "/out.mp4"


# === contact sheet pagination (раздел 22, A11) =================================


def test_paginate_shots_24_per_page():
    shots = [{"n": i} for i in range(50)]
    pages = bp.paginate_shots(shots)
    assert [len(p) for p in pages] == [24, 24, 2]


def test_paginate_shots_empty():
    assert bp.paginate_shots([]) == []


def test_contact_sheet_page_filename():
    assert bp.contact_sheet_page_filename(0) == "contact_sheet.png"
    assert bp.contact_sheet_page_filename(1) == "contact_sheet_02.png"
    assert bp.contact_sheet_page_filename(2) == "contact_sheet_03.png"


def test_render_contact_sheet_page_grey_cell_for_shot_without_ref_frames(tmp_path):
    """A11: шот без ref_start.png/ref_end.png занимает своё место серой
    ячейкой с подписью "не отрендерен"; рядом стоящий отрендеренный шот
    показывает реальный кадр."""
    rendered_dir = tmp_path / "scene_01_shot_01"
    rendered_dir.mkdir()
    Image.new("RGB", (32, 18), color=(255, 0, 0)).save(rendered_dir / "ref_start.png")
    Image.new("RGB", (32, 18), color=(0, 255, 0)).save(rendered_dir / "ref_end.png")

    missing_dir = tmp_path / "scene_01_shot_02"
    missing_dir.mkdir()  # ref_start.png/ref_end.png отсутствуют

    shots = [
        {"scene_number": 1, "shot_number": 1, "duration_s": 5},
        {"scene_number": 1, "shot_number": 2, "duration_s": 5},
    ]

    def resolver(s):
        return tmp_path / bp.shot_dir_name(s["scene_number"], s["shot_number"])

    image = bp.render_contact_sheet_page(shots, resolver, font_path=None)
    assert image.size == (bp._SHEET_PAGE_W, bp._SHEET_PAGE_H)
    assert image.getpixel((10, 10)) == (255, 0, 0)
    assert image.getpixel((bp._SHEET_CELL_W + 10, 10)) == bp._SHEET_GREY


# === файлово-присутственная логика (_gather_segments) ==========================


def test_gather_segments_skips_missing_video_with_warning(tmp_path):
    blockout_dir = tmp_path / "93_blockout"
    shot1_dir = blockout_dir / "scene_01_shot_01"
    shot1_dir.mkdir(parents=True)
    (shot1_dir / "blockout_ref.mp4").write_bytes(b"fake")
    _write_json(shot1_dir / "manifest.json", {"fps": 24, "resolution": [1280, 720]})

    ordered_shots = [
        {"scene_number": 1, "shot_number": 1, "chain_id": "c1", "duration_s": 5, "is_first_in_chain": True},
        {"scene_number": 1, "shot_number": 2, "chain_id": "c1", "duration_s": 5, "is_first_in_chain": False},
    ]
    included, warnings = bp._gather_segments(blockout_dir, ordered_shots)
    assert [s["shot_number"] for s in included] == [1]
    assert len(warnings) == 1
    assert "missing" in warnings[0]["message"]


def test_gather_segments_warns_on_missing_manifest_but_still_includes_segment(tmp_path):
    blockout_dir = tmp_path / "93_blockout"
    shot_dir = blockout_dir / "scene_01_shot_01"
    shot_dir.mkdir(parents=True)
    (shot_dir / "blockout_ref.mp4").write_bytes(b"fake")

    ordered_shots = [{"scene_number": 1, "shot_number": 1, "chain_id": "c1", "duration_s": 5, "is_first_in_chain": True}]
    included, warnings = bp._gather_segments(blockout_dir, ordered_shots)
    assert len(included) == 1
    assert included[0]["fps"] is None
    assert len(warnings) == 1
    assert "manifest" in warnings[0]["message"]


# === нулевой контракт (раздел 10.3.1) ==========================================


def test_zero_contract_returns_literal_dict_and_touches_nothing(tmp_path):
    result = bp.blockout_preview_tool(session_id="s", project_id="zc1", enable=False)
    assert result == {
        "status": "skipped",
        "segments_total": 0,
        "segments_included": 0,
        "total_duration_s": 0,
        "blockout_all_path": None,
        "blockout_all_burnin_path": None,
        "contact_sheet_paths": [],
        "artifact_path": str(tmp_path / "zc1" / "93_blockout" / "preview" / "blockout_all.mp4"),
    }
    assert not (tmp_path / "zc1").exists()


def test_zero_contract_string_false(tmp_path):
    result = bp.blockout_preview_tool(session_id="s", project_id="zc2", enable="false")
    assert result["status"] == "skipped"
    assert not (tmp_path / "zc2").exists()


def test_invalid_project_id_never_raises_degrades_to_warning():
    result = bp.blockout_preview_tool(session_id="s", project_id="../evil")
    assert result["status"] == "warning"
    assert result["segments_total"] == 0


@pytest.mark.parametrize("project_id", ["../evil", "", None])
def test_invalid_project_id_with_enable_false_never_raises_degrades_to_skipped(project_id):
    """Правка 1: _zero_contract() (enable=False branch) calls
    safe_storybook_project_dir(project_id) internally and used to be called
    OUTSIDE any try/except in blockout_preview_tool -- an invalid
    project_id raised ValueError straight out of the tool, which under
    on_failure: stop would abort artist_batch_shots/video_generator/
    montage_assembler (раздел 10.3). Must degrade to status="skipped"
    instead, symmetric with _crash_fallback() for the enable=True path."""
    result = bp.blockout_preview_tool(session_id="s", project_id=project_id, enable=False)
    assert result["status"] == "skipped"
    assert result["artifact_path"] == ""


# === "ни одного сегмента" (раздел 17.2) ========================================


def test_no_segments_found_returns_warning_and_creates_nothing_in_preview(tmp_path):
    project_id = "empty1"
    blockout_dir = tmp_path / project_id / "93_blockout"
    # раздел 20.3: report.json's sole creator is blockout_scene_builder --
    # pre-create it here to mirror a real pipeline run, where scene_builder
    # always creates it first.
    _write_json(blockout_dir / "report.json", {})
    _write_json(blockout_dir / "chains.json", _chains_payload([_chain("c1", 1, [_shot(1, 5)])]))
    # scene_01_shot_01 dir отсутствует -> blockout_ref.mp4 отсутствует

    result = bp.blockout_preview_tool(session_id="s", project_id=project_id)
    assert result["status"] == "warning"
    assert result["segments_total"] == 1
    assert result["segments_included"] == 0
    assert result["blockout_all_path"] is None
    assert not (blockout_dir / "preview").exists()

    report = _read_json(blockout_dir / "report.json")
    assert report["blockout_preview"]["status"] == "warning"
    assert report["blockout_preview"]["segments_included"] == 0


def test_chains_json_missing_returns_warning(tmp_path):
    result = bp.blockout_preview_tool(session_id="s", project_id="nochains")
    assert result["status"] == "warning"
    assert result["segments_total"] == 0


def test_report_json_not_created_when_blockout_scene_builder_never_ran(tmp_path):
    """правка 4в: раздел 20.3 names blockout_scene_builder the sole creator
    of report.json ("создаёт его blockout_scene_builder (первый по
    порядку)"). If the "Собрать превью" button (раздел 18.4) runs this step
    standalone before scene_builder has ever created the file, this step
    must not create it either -- same rule раздел 20.3 gives for
    artist_batch_shots/video_generator (only append when it already
    exists)."""
    project_id = "nofile1"
    blockout_dir = tmp_path / project_id / "93_blockout"
    _write_json(blockout_dir / "chains.json", _chains_payload([_chain("c1", 1, [_shot(1, 5)])]))
    # report.json deliberately absent -- blockout_scene_builder never ran

    result = bp.blockout_preview_tool(session_id="s", project_id=project_id)

    assert result["status"] == "warning"
    assert not (blockout_dir / "report.json").exists()


# === report.json: чужая секция сохраняется, sidecar-lock, без утечек tmp ======


def test_report_write_preserves_foreign_section_and_no_leftover_tmp(tmp_path):
    project_id = "report1"
    blockout_dir = tmp_path / project_id / "93_blockout"
    _write_json(blockout_dir / "chains.json", _chains_payload([_chain("c1", 1, [_shot(1, 5)])]))
    shot_dir = blockout_dir / "scene_01_shot_01"
    shot_dir.mkdir(parents=True)
    (shot_dir / "blockout_ref.mp4").write_bytes(b"fake")  # не настоящее видео -- ffmpeg concat не пройдёт, ок
    _write_json(shot_dir / "manifest.json", {"fps": 24, "resolution": [1280, 720]})

    report_path = blockout_dir / "report.json"
    _write_json(report_path, {"some_other_step": {"checks": [{"code": "X", "level": "info"}]}})

    bp.blockout_preview_tool(session_id="s", project_id=project_id)

    report = _read_json(report_path)
    assert report["some_other_step"] == {"checks": [{"code": "X", "level": "info"}]}
    assert "blockout_preview" in report
    assert list(blockout_dir.glob("*.tmp")) == []
    assert (blockout_dir / "report.json.lock").is_file()


# === status success/partial (детерминировано, ffmpeg замокан) =================


def test_success_status_when_all_segments_present(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "_find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(bp, "_run_ffmpeg", lambda *a, **k: (True, ""))
    project_id = "success1"
    blockout_dir = tmp_path / project_id / "93_blockout"
    _write_json(blockout_dir / "chains.json", _chains_payload([_chain("c1", 1, [_shot(1, 5)])]))
    shot_dir = blockout_dir / "scene_01_shot_01"
    shot_dir.mkdir(parents=True)
    (shot_dir / "blockout_ref.mp4").write_bytes(b"fake")
    _write_json(shot_dir / "manifest.json", {"fps": 24, "resolution": [1280, 720]})

    result = bp.blockout_preview_tool(session_id="s", project_id=project_id, burnin=False)
    assert result["status"] == "success"
    assert result["segments_included"] == 1 == result["segments_total"]
    assert result["blockout_all_path"] is not None


def test_burnin_degrades_to_no_burnin_file_when_font_unavailable(tmp_path, monkeypatch):
    """правка 4б: no font found (monkeypatched, rather than relying on the
    environment actually lacking one, which today is only covered by
    pytest.skip in test_real_burnin_pass_creates_file_when_font_available)
    must degrade -- blockout_all.mp4 still gets built, but
    blockout_all_burnin.mp4 is skipped with a report warning, not an
    exception."""
    monkeypatch.setattr(bp, "_find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(bp, "_run_ffmpeg", lambda *a, **k: (True, ""))
    monkeypatch.setattr(bp, "_find_font", lambda: None)
    project_id = "nofont1"
    blockout_dir = tmp_path / project_id / "93_blockout"
    _write_json(blockout_dir / "report.json", {})  # раздел 20.3: pre-created by scene_builder in real runs
    _write_json(blockout_dir / "chains.json", _chains_payload([_chain("c1", 1, [_shot(1, 5)])]))
    shot_dir = blockout_dir / "scene_01_shot_01"
    shot_dir.mkdir(parents=True)
    (shot_dir / "blockout_ref.mp4").write_bytes(b"fake")
    _write_json(shot_dir / "manifest.json", {"fps": 24, "resolution": [1280, 720]})

    result = bp.blockout_preview_tool(session_id="s", project_id=project_id, burnin=True)

    assert result["status"] == "success"
    assert result["blockout_all_path"] is not None
    assert result["blockout_all_burnin_path"] is None
    assert not (blockout_dir / "preview" / "blockout_all_burnin.mp4").exists()

    report = _read_json(blockout_dir / "report.json")
    checks = report["blockout_preview"]["checks"]
    assert any("burn-in font not found" in c.get("message", "") for c in checks)


def test_partial_status_when_some_segments_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "_find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(bp, "_run_ffmpeg", lambda *a, **k: (True, ""))
    project_id = "partial1"
    blockout_dir = tmp_path / project_id / "93_blockout"
    _write_json(blockout_dir / "chains.json", _chains_payload([_chain("c1", 1, [_shot(1, 5), _shot(2, 5)])]))
    shot1_dir = blockout_dir / "scene_01_shot_01"
    shot1_dir.mkdir(parents=True)
    (shot1_dir / "blockout_ref.mp4").write_bytes(b"fake")
    _write_json(shot1_dir / "manifest.json", {"fps": 24, "resolution": [1280, 720]})
    # scene_01_shot_02 отсутствует целиком

    result = bp.blockout_preview_tool(session_id="s", project_id=project_id, burnin=False)
    assert result["status"] == "partial"
    assert result["segments_total"] == 2
    assert result["segments_included"] == 1


# === очистка устаревших артефактов (раздел 8) ==================================


def test_burnin_false_removes_stale_burnin_file_and_rebuilds_contact_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "_find_ffmpeg", lambda: None)  # не требует реального ffmpeg
    project_id = "stale1"
    blockout_dir = tmp_path / project_id / "93_blockout"
    _write_json(blockout_dir / "chains.json", _chains_payload([_chain("c1", 1, [_shot(1, 5)])]))
    shot_dir = blockout_dir / "scene_01_shot_01"
    shot_dir.mkdir(parents=True)
    (shot_dir / "blockout_ref.mp4").write_bytes(b"fake")

    preview_dir = blockout_dir / "preview"
    preview_dir.mkdir(parents=True)
    (preview_dir / "blockout_all_burnin.mp4").write_bytes(b"stale")
    (preview_dir / "contact_sheet.png").write_bytes(b"stale-png")

    bp.blockout_preview_tool(session_id="s", project_id=project_id, burnin=False)

    assert not (preview_dir / "blockout_all_burnin.mp4").exists()
    assert (preview_dir / "contact_sheet.png").is_file()
    assert (preview_dir / "contact_sheet.png").read_bytes() != b"stale-png"


# === реальный ffmpeg (skipif) ==================================================


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed in this environment")
def test_real_concat_produces_video_with_correct_total_duration(tmp_path):
    project_id = "realconcat"
    blockout_dir = tmp_path / project_id / "93_blockout"
    _write_json(blockout_dir / "chains.json", _chains_payload([_chain("c1", 1, [_shot(1, 1), _shot(2, 2)])]))
    for shot_number, duration in ((1, 1), (2, 2)):
        shot_dir = blockout_dir / f"scene_01_shot_{shot_number:02d}"
        _make_video(shot_dir / "blockout_ref.mp4", duration_s=duration, fps=24, w=64, h=36)
        _write_json(shot_dir / "manifest.json", {"fps": 24, "resolution": [64, 36]})
        Image.new("RGB", (64, 36), color=(1, 2, 3)).save(shot_dir / "ref_start.png")
        Image.new("RGB", (64, 36), color=(4, 5, 6)).save(shot_dir / "ref_end.png")

    result = bp.blockout_preview_tool(session_id="s", project_id=project_id, burnin=False)
    assert result["status"] == "success"
    assert result["segments_included"] == 2
    assert result["total_duration_s"] == 3

    out_path = Path(result["blockout_all_path"])
    assert out_path.is_file()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert float(probe.stdout.strip()) == pytest.approx(3.0, abs=0.2)

    assert len(result["contact_sheet_paths"]) == 1
    assert Path(result["contact_sheet_paths"][0]).is_file()
    assert result["blockout_all_burnin_path"] is None
    assert not (blockout_dir / "preview" / "blockout_all_burnin.mp4").exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed in this environment")
def test_real_burnin_pass_creates_file_when_font_available(tmp_path):
    if bp._find_font() is None:
        pytest.skip("no drawtext font available in this environment")
    project_id = "realburnin"
    blockout_dir = tmp_path / project_id / "93_blockout"
    _write_json(blockout_dir / "chains.json", _chains_payload([_chain("c1", 1, [_shot(1, 1)])]))
    shot_dir = blockout_dir / "scene_01_shot_01"
    _make_video(shot_dir / "blockout_ref.mp4", duration_s=1, fps=24, w=64, h=36)
    _write_json(shot_dir / "manifest.json", {"fps": 24, "resolution": [64, 36]})

    result = bp.blockout_preview_tool(session_id="s", project_id=project_id, burnin=True)
    assert result["blockout_all_burnin_path"] is not None
    assert Path(result["blockout_all_burnin_path"]).is_file()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed in this environment")
def test_real_mismatched_segment_gets_reencoded_to_first_segments_params(tmp_path):
    project_id = "reencode1"
    blockout_dir = tmp_path / project_id / "93_blockout"
    _write_json(blockout_dir / "report.json", {})  # раздел 20.3: pre-created by scene_builder in real runs
    _write_json(blockout_dir / "chains.json", _chains_payload([_chain("c1", 1, [_shot(1, 1), _shot(2, 1)])]))

    shot1_dir = blockout_dir / "scene_01_shot_01"
    _make_video(shot1_dir / "blockout_ref.mp4", duration_s=1, fps=24, w=64, h=36)
    _write_json(shot1_dir / "manifest.json", {"fps": 24, "resolution": [64, 36]})

    shot2_dir = blockout_dir / "scene_01_shot_02"
    _make_video(shot2_dir / "blockout_ref.mp4", duration_s=1, fps=30, w=128, h=72)
    _write_json(shot2_dir / "manifest.json", {"fps": 30, "resolution": [128, 72]})

    result = bp.blockout_preview_tool(session_id="s", project_id=project_id, burnin=False)
    assert result["status"] == "success"

    out_path = Path(result["blockout_all_path"])
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(out_path)],
        capture_output=True, text=True, timeout=60,
    )
    width, height, rate = probe.stdout.strip().split(",")
    assert (int(width), int(height)) == (64, 36)
    assert rate in ("24/1", "24")

    report = _read_json(blockout_dir / "report.json")
    checks = report["blockout_preview"]["checks"]
    assert any("re-encoded" in c["message"] for c in checks)
