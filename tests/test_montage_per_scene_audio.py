"""Tests for the per-scene leitmotif audio filter_complex path in montage_assembler.

See docs/plans/2026-08-18-4a-music-per-scene-leitmotif-design.md for the ffmpeg
templates (atrim/aloop/afade/acrossfade) and fallback guards being verified here.

No real ffmpeg/ffprobe is invoked: `_run_command` and `_probe_media` are
monkeypatched, matching the style of tests/test_montage_assembler_tool.py.
"""

import json
import re
from pathlib import Path

from custom_tools.storybook import montage_assembler


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _probe_payload(duration: float, with_audio: bool = False):
    streams = [{"codec_type": "video"}]
    if with_audio:
        streams.append({"codec_type": "audio"})
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": str(duration)},
        "streams": streams,
    }


def _make_clip(base: Path, scene: int, shot: int = 1) -> Path:
    clip = (
        base
        / "97_shots"
        / f"scene_{scene:02d}_shot_{shot:02d}"
        / f"video_final_{scene:02d}_{shot:02d}.mp4"
    )
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"media")
    return clip


def _make_mp3(base: Path, name: str) -> Path:
    path = base / "98_audio" / "music" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"id3-fake-mp3-bytes")
    return path


def _scaffold_shots(base: Path, items) -> None:
    _write_json(base / "97_shots" / "shots.json", {"items": items})
    (base / "98_audio").mkdir(parents=True, exist_ok=True)
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:04,000\nCue\n", encoding="utf-8"
    )


def _capture_run(commands):
    def fake_run(command, timeout=None):
        commands.append(command)
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    return fake_run


def _make_probe(duration_by_path, final_duration, final_with_audio=False):
    """Fake `_probe_media` that answers by resolved path, plus a special case
    for final_video.mp4 (probed once after render to build the final review)."""

    def fake_probe(path):
        if Path(path).name == "final_video.mp4":
            return _probe_payload(final_duration, with_audio=final_with_audio)
        return _probe_payload(duration_by_path[str(Path(path).resolve())])

    return fake_probe


def _render_command(commands):
    return next(c for c in commands if "-filter_complex" in c)


def _filter_complex_of(command):
    return command[command.index("-filter_complex") + 1]


def test_multi_track_per_scene_filter_complex_correct_for_two_scenes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_two_scenes"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip1 = _make_clip(base, scene=1, shot=1)
    clip2 = _make_clip(base, scene=2, shot=1)
    _scaffold_shots(
        base,
        [
            {"scene_number": 1, "shot_number": 1, "timing": "4s",
             "video_path": str(clip1), "video_prompt": "Hero walks through the alley"},
            {"scene_number": 2, "shot_number": 1, "timing": "5s",
             "video_path": str(clip2), "video_prompt": "Neutral establishing shot of the street"},
        ],
    )
    hero_mp3 = _make_mp3(base, "hero.mp3")
    neutral_mp3 = _make_mp3(base, "neutral.mp3")
    _write_json(
        base / "98_audio" / "music_plan.json",
        {"scene_mapping": {"1": "hero", "2": "neutral"}},
    )
    _write_json(
        base / "98_audio" / "music_manifest.json",
        {"hero": "music/hero.mp3", "neutral": "music/neutral.mp3"},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    commands = []
    monkeypatch.setattr(montage_assembler, "_run_command", _capture_run(commands))
    duration_by_path = {
        str(clip1.resolve()): 4.0,
        str(clip2.resolve()): 5.0,
        str(hero_mp3.resolve()): 10.0,
        str(neutral_mp3.resolve()): 10.0,
    }
    monkeypatch.setattr(
        montage_assembler, "_probe_media", _make_probe(duration_by_path, final_duration=9.0)
    )

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    render_command = _render_command(commands)
    assert render_command.count("-i") == 4  # 2 video + 2 unique audio inputs
    filter_complex = _filter_complex_of(render_command)
    assert "atrim=0:4.000" in filter_complex
    assert "atrim=0:5.000" in filter_complex
    assert filter_complex.count("afade=in:0:0.5") == 2
    # crossfade duration is clamped (min(1.0, min(a,b)/2 - 0.05)); relax the exact-digits
    # match but still confirm 4-5s scenes land on the un-clamped 1.000s value.
    match = re.search(
        r"\[a_scene_0\]\[a_scene_1\]acrossfade=d=(\d+\.?\d*)\[a_final\]", filter_complex
    )
    assert match, filter_complex
    assert match.group(1) == "1.000"


def test_multi_track_aloop_used_for_scene_longer_than_track(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_aloop"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base, scene=1, shot=1)
    _scaffold_shots(
        base,
        [
            {"scene_number": 1, "shot_number": 1, "timing": "60s",
             "video_path": str(clip), "video_prompt": "Long chase scene through the city"},
        ],
    )
    neutral_mp3 = _make_mp3(base, "neutral.mp3")
    _write_json(base / "98_audio" / "music_plan.json", {"scene_mapping": {"1": "neutral"}})
    _write_json(base / "98_audio" / "music_manifest.json", {"neutral": "music/neutral.mp3"})
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    commands = []
    monkeypatch.setattr(montage_assembler, "_run_command", _capture_run(commands))
    duration_by_path = {
        str(clip.resolve()): 60.0,
        str(neutral_mp3.resolve()): 30.0,
    }
    monkeypatch.setattr(
        montage_assembler, "_probe_media", _make_probe(duration_by_path, final_duration=60.0)
    )

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    filter_complex = _filter_complex_of(_render_command(commands))
    # track (30s) probed shorter than scene (60s) -> aloop must precede atrim.
    # size=2147483647 (max int32) rather than a sample-rate-derived size, since Suno
    # mp3s aren't guaranteed to be 44.1kHz.
    assert "aloop=loop=-1:size=2147483647" in filter_complex
    assert "atrim=0:60.000" in filter_complex


def test_multi_track_single_scene_skips_crossfade(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_single_scene"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base, scene=1, shot=1)
    _scaffold_shots(
        base,
        [
            {"scene_number": 1, "shot_number": 1, "timing": "4s",
             "video_path": str(clip), "video_prompt": "Quiet moment by the window"},
        ],
    )
    neutral_mp3 = _make_mp3(base, "neutral.mp3")
    _write_json(base / "98_audio" / "music_plan.json", {"scene_mapping": {"1": "neutral"}})
    _write_json(base / "98_audio" / "music_manifest.json", {"neutral": "music/neutral.mp3"})
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    commands = []
    monkeypatch.setattr(montage_assembler, "_run_command", _capture_run(commands))
    duration_by_path = {
        str(clip.resolve()): 4.0,
        str(neutral_mp3.resolve()): 10.0,
    }
    monkeypatch.setattr(
        montage_assembler, "_probe_media", _make_probe(duration_by_path, final_duration=4.0)
    )

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    filter_complex = _filter_complex_of(_render_command(commands))
    assert "acrossfade" not in filter_complex
    assert "atrim=0:4.000" in filter_complex
    assert "afade=in:0:0.5" in filter_complex
    assert "[a_final]" in filter_complex


def test_multi_track_missing_manifest_falls_back_to_legacy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_missing_manifest"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base, scene=1, shot=1)
    _scaffold_shots(
        base,
        [
            {"scene_number": 1, "shot_number": 1, "timing": "4s",
             "video_path": str(clip), "video_prompt": "Quiet moment by the window"},
        ],
    )
    # music_plan.json present, but music_manifest.json intentionally absent:
    # the dispatcher must skip the per-scene path entirely and use the legacy renderer.
    _write_json(base / "98_audio" / "music_plan.json", {"scene_mapping": {"1": "neutral"}})

    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    commands = []
    monkeypatch.setattr(montage_assembler, "_run_command", _capture_run(commands))
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0))

    legacy_calls = []
    original_legacy = montage_assembler._render_legacy_single_track

    def spy_legacy(*args, **kwargs):
        legacy_calls.append((args, kwargs))
        return original_legacy(*args, **kwargs)

    monkeypatch.setattr(montage_assembler, "_render_legacy_single_track", spy_legacy)

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    assert len(legacy_calls) == 1
    filter_complex = _filter_complex_of(_render_command(commands))
    assert "a_scene_0" not in filter_complex
    assert "acrossfade" not in filter_complex


def test_multi_track_dedups_repeated_leitmotif_track_across_scenes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_dedup"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip1 = _make_clip(base, scene=1, shot=1)
    clip2 = _make_clip(base, scene=2, shot=1)
    clip3 = _make_clip(base, scene=3, shot=1)
    _scaffold_shots(
        base,
        [
            {"scene_number": 1, "shot_number": 1, "timing": "4s",
             "video_path": str(clip1), "video_prompt": "Hero enters the tomb"},
            {"scene_number": 2, "shot_number": 1, "timing": "3s",
             "video_path": str(clip2), "video_prompt": "Neutral establishing shot of the street"},
            {"scene_number": 3, "shot_number": 1, "timing": "4s",
             "video_path": str(clip3), "video_prompt": "Hero returns to the tomb"},
        ],
    )
    hero_mp3 = _make_mp3(base, "hero.mp3")
    neutral_mp3 = _make_mp3(base, "neutral.mp3")
    _write_json(
        base / "98_audio" / "music_plan.json",
        {"scene_mapping": {"1": "hero", "2": "neutral", "3": "hero"}},
    )
    _write_json(
        base / "98_audio" / "music_manifest.json",
        {"hero": "music/hero.mp3", "neutral": "music/neutral.mp3"},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    commands = []
    monkeypatch.setattr(montage_assembler, "_run_command", _capture_run(commands))
    duration_by_path = {
        str(clip1.resolve()): 4.0,
        str(clip2.resolve()): 3.0,
        str(clip3.resolve()): 4.0,
        str(hero_mp3.resolve()): 10.0,
        str(neutral_mp3.resolve()): 10.0,
    }
    monkeypatch.setattr(
        montage_assembler, "_probe_media", _make_probe(duration_by_path, final_duration=11.0)
    )

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    render_command = _render_command(commands)
    assert render_command.count("-i") == 5  # 3 video + 2 unique audio inputs (dedup)
    assert render_command.count(str(hero_mp3.resolve())) == 1
    assert render_command.count(str(neutral_mp3.resolve())) == 1
    filter_complex = _filter_complex_of(render_command)
    # scenes 1 and 3 both reference hero.mp3 -> same ffmpeg input index reused.
    assert filter_complex.count("[3:a]") == 2
    assert "[4:a]" in filter_complex
