"""Tests for the per-scene leitmotif audio filter_complex path in montage_assembler.

See docs/plans/2026-08-18-4a-music-per-scene-leitmotif-design.md for the ffmpeg
templates (atrim/aloop/afade/acrossfade) and fallback guards being verified here.

Most checks monkeypatch `_run_command` and `_probe_media`, matching the style
of tests/test_montage_assembler_tool.py. The dedicated temporary-file
regression invokes local `ffmpeg`/`ffprobe` to verify the real MP4 path.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_tools.storybook import montage_assembler


_HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


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
    # First input is extended by its 1s outgoing crossfade, preserving the
    # second scene's absolute start after acrossfade subtracts that second.
    assert "atrim=0:5.000" in filter_complex
    assert "atrim=0:5.000" in filter_complex
    assert filter_complex.count("afade=t=in:st=0:d=0.5") == 2
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
    assert "afade=t=in:st=0:d=0.5" in filter_complex
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


def test_group_scenes_preserves_absolute_timeline_and_gap():
    scenes = montage_assembler._group_scenes_from_clips([
        {"scene_number": 1, "planned_start_seconds": 0, "planned_end_seconds": 4, "planned_duration_seconds": 4},
        {"scene_number": 2, "planned_start_seconds": 6, "planned_end_seconds": 10, "planned_duration_seconds": 4},
    ])

    assert scenes == [
        {"scene_id": "1", "start_seconds": 0.0, "end_seconds": 4.0, "duration_seconds": 4.0},
        {"scene_id": "2", "start_seconds": 6.0, "end_seconds": 10.0, "duration_seconds": 4.0},
    ]


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg and ffprobe are required")
def test_real_ffmpeg_renders_mp4_through_temporary_file_in_both_audio_paths(tmp_path):
    video_path = tmp_path / "clip.mp4"
    audio_path = tmp_path / "theme.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video_path),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(audio_path)],
        check=True,
        capture_output=True,
    )
    clips = [{
        "path": str(video_path),
        "planned_start_seconds": 0,
        "planned_end_seconds": 1,
        "planned_duration_seconds": 1,
    }]

    legacy_output = tmp_path / "legacy.mp4"
    legacy_result = montage_assembler._render_legacy_single_track(
        "ffmpeg", clips, [{"path": str(audio_path)}], legacy_output, expected_duration=1,
    )
    scene_output = tmp_path / "per_scene.mp4"
    scene_result = montage_assembler._render_with_per_scene_audio(
        "ffmpeg",
        clips,
        [{"scene_id": "1", "duration_seconds": 1}],
        {"scene_mapping": {"1": "neutral"}},
        {"neutral": audio_path.name},
        tmp_path,
        [{"path": str(audio_path)}],
        scene_output,
        expected_duration=1,
    )

    assert legacy_result["returncode"] == 0, legacy_result
    assert scene_result["returncode"] == 0, scene_result
    for output_path in (legacy_output, scene_output):
        probe = montage_assembler._probe_media(output_path)
        assert "mp4" in probe["format"]["format_name"]
        assert any(stream["codec_type"] == "video" for stream in probe["streams"])
        assert any(stream["codec_type"] == "audio" for stream in probe["streams"])


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg and ffprobe are required")
def test_real_ffmpeg_crossfades_keep_thirteen_second_audio_timeline(tmp_path):
    clips = []
    scenes = []
    manifest = {}
    for index, duration in enumerate((4, 5, 4), start=1):
        clip_path = tmp_path / f"clip_{index}.mp4"
        audio_path = tmp_path / f"theme_{index}.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=blue:s=320x180:d={duration}", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=15", str(audio_path)],
            check=True, capture_output=True,
        )
        start = sum((4, 5, 4)[:index - 1])
        clips.append({"path": str(clip_path), "planned_start_seconds": start, "planned_end_seconds": start + duration, "planned_duration_seconds": duration})
        scenes.append({"scene_id": str(index), "duration_seconds": duration})
        manifest[str(index)] = audio_path.name

    output_path = tmp_path / "final.mp4"
    result = montage_assembler._render_with_per_scene_audio(
        "ffmpeg", clips, scenes, {"scene_mapping": {"1": "1", "2": "2", "3": "3"}}, manifest,
        tmp_path, [], output_path, expected_duration=13,
    )

    assert result["returncode"] == 0, result
    probe = montage_assembler._probe_media(output_path)
    audio_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert abs(float(audio_stream["duration"]) - 13.0) <= 0.15


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg and ffprobe are required")
def test_real_ffmpeg_short_scene_keeps_next_theme_at_absolute_start(tmp_path):
    """A <=0.1s music scene must not remove or delay the following theme."""
    clips = []
    scenes = []
    manifest = {}
    for index, (duration, audio_source) in enumerate(
        ((0.1, "anullsrc=r=44100:cl=stereo:d=5"), (4, "sine=frequency=880:duration=5")),
        start=1,
    ):
        clip_path = tmp_path / f"short_clip_{index}.mp4"
        audio_path = tmp_path / f"short_theme_{index}.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=blue:s=320x180:d={duration}", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", audio_source, str(audio_path)],
            check=True, capture_output=True,
        )
        start = 0 if index == 1 else 0.1
        clips.append({"path": str(clip_path), "planned_start_seconds": start, "planned_end_seconds": start + duration, "planned_duration_seconds": duration})
        scenes.append({"scene_id": str(index), "start_seconds": start, "duration_seconds": duration})
        manifest[str(index)] = audio_path.name

    output_path = tmp_path / "short_scene_final.mp4"
    result = montage_assembler._render_with_per_scene_audio(
        "ffmpeg", clips, scenes, {"scene_mapping": {"1": "1", "2": "2"}}, manifest,
        tmp_path, [], output_path, expected_duration=4.1,
    )

    assert result["returncode"] == 0, result
    probe = montage_assembler._probe_media(output_path)
    audio_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert abs(float(audio_stream["duration"]) - 4.1) <= 0.15
    detected = subprocess.run(
        ["ffmpeg", "-i", str(output_path), "-af", "silencedetect=n=-50dB:d=0.03", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    matches = re.findall(r"silence_start: ([0-9.]+).*?silence_end: ([0-9.]+)", detected.stderr, re.S)
    assert any(abs(float(start)) <= 0.03 and abs(float(end) - 0.1) <= 0.05 for start, end in matches), detected.stderr


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg and ffprobe are required")
@pytest.mark.parametrize("starts, expected_silence", [((0, 6), (4, 6)), ((2, 6), (0, 2))])
def test_real_ffmpeg_preserves_music_timeline_gaps(tmp_path, starts, expected_silence):
    clips = []
    manifest = {}
    for index, start in enumerate(starts, start=1):
        clip_path = tmp_path / f"clip_{index}.mp4"
        audio_path = tmp_path / f"theme_{index}.mp3"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip_path)], check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={index * 440}:duration=10", str(audio_path)], check=True, capture_output=True)
        clips.append({"path": str(clip_path), "planned_start_seconds": start, "planned_end_seconds": start + 4, "planned_duration_seconds": 4})
        manifest[str(index)] = audio_path.name
    output_path = tmp_path / "final.mp4"
    result = montage_assembler._render_with_per_scene_audio(
        "ffmpeg", clips,
        [{"scene_id": "1", "start_seconds": starts[0], "duration_seconds": 4}, {"scene_id": "2", "start_seconds": starts[1], "duration_seconds": 4}],
        {"scene_mapping": {"1": "1", "2": "2"}}, manifest, tmp_path, [], output_path, expected_duration=10,
    )
    assert result["returncode"] == 0, result
    detected = subprocess.run(["ffmpeg", "-i", str(output_path), "-af", "silencedetect=n=-50dB:d=1", "-f", "null", "-"], capture_output=True, text=True, check=True)
    matches = re.findall(r"silence_start: ([0-9.]+).*?silence_end: ([0-9.]+)", detected.stderr, re.S)
    assert any(abs(float(start) - expected_silence[0]) <= 0.2 and abs(float(end) - expected_silence[1]) <= 0.2 for start, end in matches), detected.stderr


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg and ffprobe are required")
def test_real_ffmpeg_single_scene_initial_gap_and_same_scene_gap(tmp_path):
    audio_path = tmp_path / "theme.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=12", str(audio_path)], check=True, capture_output=True)
    clips = []
    for index, start in enumerate((0, 6), start=1):
        clip_path = tmp_path / f"clip_{index}.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip_path)], check=True, capture_output=True)
        clips.append({"path": str(clip_path), "scene_number": 1, "planned_start_seconds": start, "planned_end_seconds": start + 4, "planned_duration_seconds": 4})
    scenes = montage_assembler._group_scenes_from_clips(clips)
    assert len(scenes) == 2
    output_path = tmp_path / "same_scene_gap.mp4"
    result = montage_assembler._render_with_per_scene_audio("ffmpeg", clips, scenes, {"scene_mapping": {"1": "theme"}}, {"theme": audio_path.name}, tmp_path, [], output_path, 10)
    assert result["returncode"] == 0, result
    probe = montage_assembler._probe_media(output_path)
    assert abs(float(next(s for s in probe["streams"] if s["codec_type"] == "audio")["duration"]) - 10) <= 0.15
    detected = subprocess.run(["ffmpeg", "-i", str(output_path), "-af", "silencedetect=n=-50dB:d=1", "-f", "null", "-"], capture_output=True, text=True, check=True)
    matches = re.findall(r"silence_start: ([0-9.]+).*?silence_end: ([0-9.]+)", detected.stderr, re.S)
    assert any(abs(float(start) - 4) <= 0.2 and abs(float(end) - 6) <= 0.2 for start, end in matches), detected.stderr

    initial_clip = [{**clips[0], "scene_number": 2, "planned_start_seconds": 2, "planned_end_seconds": 6}]
    initial_output = tmp_path / "initial_gap.mp4"
    initial_result = montage_assembler._render_with_per_scene_audio("ffmpeg", initial_clip, [{"scene_id": "2", "start_seconds": 2, "duration_seconds": 4}], {"scene_mapping": {"2": "theme"}}, {"theme": audio_path.name}, tmp_path, [], initial_output, 6)
    assert initial_result["returncode"] == 0, initial_result
    initial_probe = montage_assembler._probe_media(initial_output)
    assert abs(float(next(s for s in initial_probe["streams"] if s["codec_type"] == "audio")["duration"]) - 6) <= 0.15
    initial_detected = subprocess.run(["ffmpeg", "-i", str(initial_output), "-af", "silencedetect=n=-50dB:d=1", "-f", "null", "-"], capture_output=True, text=True, check=True)
    assert re.search(r"silence_start: 0(?:\.\d+)?", initial_detected.stderr)
