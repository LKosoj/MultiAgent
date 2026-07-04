import json
from pathlib import Path

from custom_tools.storybook import montage_assembler


def _write_json(path: Path, payload):
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


def test_montage_assembler_writes_final_artifacts_with_monkeypatched_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_montage"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip_one = base / "97_shots" / "scene_01_shot_01" / "video_final_01_01.mp4"
    clip_two = base / "97_shots" / "scene_01_shot_02" / "video_final_01_02.mp4"
    audio = base / "98_audio" / "narration.wav"
    for path in [clip_one, clip_two, audio]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "shot_type": "start",
                    "timing": "00:00 - 00:04",
                    "video_path": str(clip_one),
                    "video_prompt": "Camera pans across the room",
                    "camera_plan": "Wide",
                },
                {
                    "scene_number": 1,
                    "shot_number": 2,
                    "shot_type": "start",
                    "timing": "4s",
                    "video_path": str(clip_two),
                    "video_prompt": "Hero turns toward the window",
                    "camera_plan": "Close-up",
                },
            ]
        },
    )
    _write_json(
        base / "98_audio" / "audio_manifest.json",
        {"tts_status": "ready", "audio_tracks": [{"path": str(audio), "role": "narration"}]},
    )
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:04,000\nOne\n\n"
        "2\n00:00:04,000 --> 00:00:08,000\nTwo\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    commands = []

    def fake_run(command, timeout=None):
        commands.append(command)
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
        stderr = ""
        if "volumedetect" in command:
            stderr = "mean_volume: -18.0 dB\nmax_volume: -3.0 dB\n"
        return {
            "command": command,
            "returncode": 0,
            "stdout": "",
            "stderr": stderr,
        }

    def fake_probe(path):
        if Path(path).name == "final_video.mp4":
            return _probe_payload(8.0, with_audio=True)
        return _probe_payload(4.0)

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", fake_probe)

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    final_dir = base / "99_final"
    assert (final_dir / "final_video.mp4").exists()
    assert (final_dir / "timeline.fcpxml").exists()
    assert (final_dir / "subtitles.srt").read_text(encoding="utf-8").count("-->") == 2
    manifest = json.loads((final_dir / "manifest.json").read_text(encoding="utf-8"))
    review = json.loads((final_dir / "final_review.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert review["passed"] is True
    assert review["checks"]["audio"]["has_audio"] is True
    render_command = next(command for command in commands if "-filter_complex" in command)
    assert "-safe" not in render_command
    assert str(audio) in render_command


def test_montage_assembler_writes_error_artifacts_when_ffmpeg_or_clips_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_montage_error"
    base = tmp_path / "plots" / "storybooks" / project_id
    missing_clip = base / "97_shots" / "scene_01_shot_01" / "missing.mp4"
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "timing": "5s",
                    "video_path": str(missing_clip),
                    "video_prompt": "Static camera watches the room",
                }
            ]
        },
    )
    (base / "98_audio").mkdir(parents=True, exist_ok=True)
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:05,000\nOnly cue\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: None)

    result = montage_assembler.montage_assembler_tool(
        "sess",
        project_id,
        allow_missing_audio=True,
    )

    assert result["status"] == "error"
    final_dir = base / "99_final"
    assert (final_dir / "manifest.json").exists()
    assert (final_dir / "asset_manifest.json").exists()
    assert (final_dir / "render_report.json").exists()
    assert (final_dir / "final_review.json").exists()
    asset_manifest = json.loads((final_dir / "asset_manifest.json").read_text(encoding="utf-8"))
    render_report = json.loads((final_dir / "render_report.json").read_text(encoding="utf-8"))
    assert asset_manifest["missing_clips"][0]["path"] == str(missing_clip)
    assert "ffmpeg not available" in render_report["errors"]


def test_montage_assembler_allows_tts_unavailable_without_audio_track(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_video_only"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = base / "97_shots" / "scene_01_shot_01" / "video_final_01_01.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"media")
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "timing": "4s",
                    "video_path": str(clip),
                    "video_prompt": "Camera moves through the room",
                }
            ]
        },
    )
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:04,000\nCue\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0, with_audio=False))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    assert review["checks"]["audio"]["status"] == "not_available"


def test_montage_assembler_blocks_required_missing_audio_before_render(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_missing_audio"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = base / "97_shots" / "scene_01_shot_01" / "video_final_01_01.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"media")
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "timing": "4s",
                    "video_path": str(clip),
                    "video_prompt": "Camera moves",
                }
            ]
        },
    )
    _write_json(
        base / "98_audio" / "audio_manifest.json",
        {"tts_status": "ready", "audio_tracks": [{"path": "missing.wav", "role": "narration"}]},
    )
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:04,000\nCue\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def forbidden_run(command, timeout=None):
        raise AssertionError(f"required missing audio must block render: {command}")

    monkeypatch.setattr(montage_assembler, "_run_command", forbidden_run)

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "error"
    render_report = json.loads((base / "99_final" / "render_report.json").read_text(encoding="utf-8"))
    assert "Audio track missing and allow_missing_audio=False" in render_report["errors"]


def test_montage_assembler_resolves_relative_audio_paths_from_audio_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_relative_audio"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = base / "97_shots" / "scene_01_shot_01" / "video_final_01_01.mp4"
    audio = base / "98_audio" / "narration.wav"
    clip.parent.mkdir(parents=True, exist_ok=True)
    audio.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"media")
    audio.write_bytes(b"audio")
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "timing": "4s",
                    "video_path": str(clip),
                    "video_prompt": "Camera moves",
                }
            ]
        },
    )
    _write_json(
        base / "98_audio" / "audio_manifest.json",
        {"tts_status": "ready", "audio_tracks": [{"path": "narration.wav", "role": "narration"}]},
    )
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:04,000\nCue\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    commands = []

    def fake_run(command, timeout=None):
        commands.append(command)
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
        stderr = "mean_volume: -14.0 dB\nmax_volume: -2.0 dB\n" if "volumedetect" in command else ""
        return {"command": command, "returncode": 0, "stdout": "", "stderr": stderr}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0, with_audio=True))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    render_command = next(command for command in commands if "-filter_complex" in command)
    assert str(audio.resolve()) in render_command


def test_montage_assembler_blocks_video_paths_outside_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_path_block"
    base = tmp_path / "plots" / "storybooks" / project_id
    outside_clip = tmp_path / "outside.mp4"
    outside_clip.write_bytes(b"media")
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "timing": "4s",
                    "video_path": str(outside_clip),
                    "video_prompt": "Camera moves",
                }
            ]
        },
    )
    (base / "98_audio").mkdir(parents=True, exist_ok=True)
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:04,000\nCue\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def forbidden_run(command, timeout=None):
        raise AssertionError(f"outside media path must block render: {command}")

    monkeypatch.setattr(montage_assembler, "_run_command", forbidden_run)

    result = montage_assembler.montage_assembler_tool("sess", project_id, allow_missing_audio=True)

    assert result["status"] == "error"
    render_report = json.loads((base / "99_final" / "render_report.json").read_text(encoding="utf-8"))
    assert "Blocked video paths outside project: 1" in render_report["errors"]
    timeline_text = (base / "99_final" / "timeline.fcpxml").read_text(encoding="utf-8")
    assert "outside.mp4" not in timeline_text


def test_montage_assembler_rejects_project_id_path_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = montage_assembler.montage_assembler_tool("sess", "../outside")

    assert result["status"] == "error"
    assert "project_id must be a safe path segment" in result["message"]


def test_montage_assembler_failed_blackdetect_fails_final_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_blackdetect_fail"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = base / "97_shots" / "scene_01_shot_01" / "video_final_01_01.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"media")
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "timing": "4s",
                    "video_path": str(clip),
                    "video_prompt": "Camera moves",
                }
            ]
        },
    )
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:04,000\nCue\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        if "blackdetect=d=0.5:pic_th=0.98" in command:
            return {"command": command, "returncode": 1, "stdout": "", "stderr": "blackdetect failed"}
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0, with_audio=False))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "error"
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    assert review["checks"]["black_frame"]["analyzer_passed"] is False


def test_montage_assembler_preserves_absolute_timing_gap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_timing_gap"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip_one = base / "97_shots" / "scene_01_shot_01" / "video_final_01_01.mp4"
    clip_two = base / "97_shots" / "scene_01_shot_02" / "video_final_01_02.mp4"
    for clip in [clip_one, clip_two]:
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"media")
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "timing": "00:00 - 00:02",
                    "video_path": str(clip_one),
                    "video_prompt": "Camera moves one",
                },
                {
                    "scene_number": 1,
                    "shot_number": 2,
                    "timing": "00:05 - 00:07",
                    "video_path": str(clip_two),
                    "video_prompt": "Camera moves two",
                },
            ]
        },
    )
    _write_json(base / "98_audio" / "audio_manifest.json", {"tts_status": "unavailable", "audio_tracks": []})
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nOne\n\n"
        "2\n00:00:05,000 --> 00:00:07,000\nTwo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    render_commands = []

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            render_commands.append(command)
            Path(command[-1]).write_bytes(b"final")
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(7.0, with_audio=False))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    render_filter = render_commands[0][render_commands[0].index("-filter_complex") + 1]
    assert "color=c=black" not in render_filter
    assert "stop_duration=3.000" in render_filter
    edit_decisions = json.loads((base / "99_final" / "edit_decisions.json").read_text(encoding="utf-8"))
    assert edit_decisions["sequence"][1]["timeline_start_seconds"] == 5.0
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    assert review["expected_duration_seconds"] == 7.0


def test_montage_assembler_missing_audio_manifest_records_unavailable_tts_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_missing_audio_manifest"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = base / "97_shots" / "scene_01_shot_01" / "video_final_01_01.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"media")
    _write_json(
        base / "97_shots" / "shots.json",
        {
            "items": [
                {
                    "scene_number": 1,
                    "shot_number": 1,
                    "timing": "4s",
                    "video_path": str(clip),
                    "video_prompt": "Camera moves",
                }
            ]
        },
    )
    (base / "98_audio").mkdir(parents=True, exist_ok=True)
    (base / "98_audio" / "subtitles.srt").write_text(
        "1\n00:00:00,000 --> 00:00:04,000\nCue\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0, with_audio=False))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    asset_manifest = json.loads((base / "99_final" / "asset_manifest.json").read_text(encoding="utf-8"))
    assert asset_manifest["audio"]["tts_status"] == "unavailable"


def _make_clip(base: Path, scene: int = 1, shot: int = 1) -> Path:
    clip = (
        base
        / "97_shots"
        / f"scene_{scene:02d}_shot_{shot:02d}"
        / f"video_final_{scene:02d}_{shot:02d}.mp4"
    )
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"media")
    return clip


def _scaffold(base, items, *, audio_manifest=None, cue_sheet=None,
              subtitles="1\n00:00:00,000 --> 00:00:04,000\nCue\n"):
    _write_json(base / "97_shots" / "shots.json", {"items": items})
    (base / "98_audio").mkdir(parents=True, exist_ok=True)
    if audio_manifest is not None:
        _write_json(base / "98_audio" / "audio_manifest.json", audio_manifest)
    if cue_sheet is not None:
        _write_json(base / "98_audio" / "cue_sheet.json", cue_sheet)
    if subtitles is not None:
        (base / "98_audio" / "subtitles.srt").write_text(subtitles, encoding="utf-8")


def _video_only_run(command, timeout=None):
    if "-filter_complex" in command:
        Path(command[-1]).write_bytes(b"final")
    return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}


def test_montage_assembler_removes_shortest_and_pads_audio(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_audio_pad"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    audio = base / "98_audio" / "narration.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": str(clip), "video_prompt": "Camera moves"}],
        audio_manifest={"tts_status": "ready",
                        "audio_tracks": [{"path": str(audio), "role": "narration"}]},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    commands = []

    def fake_run(command, timeout=None):
        commands.append(command)
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
        stderr = "mean_volume: -16.0 dB\nmax_volume: -2.0 dB\n" if "volumedetect" in command else ""
        return {"command": command, "returncode": 0, "stdout": "", "stderr": stderr}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0, with_audio=True))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    render_command = next(c for c in commands if "-filter_complex" in c)
    assert "-shortest" not in render_command
    assert "-t" in render_command
    render_filter = render_command[render_command.index("-filter_complex") + 1]
    assert "apad" in render_filter
    assert "afade=t=out" in render_filter


def test_montage_assembler_run_command_receives_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_timeout_kw"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": str(clip), "video_prompt": "Camera moves"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    seen = []

    def fake_run(command, timeout=None):
        seen.append((command, timeout))
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0))

    montage_assembler.montage_assembler_tool("sess", project_id)

    render_timeout = next(t for c, t in seen if "-filter_complex" in c)
    assert render_timeout == montage_assembler._RENDER_TIMEOUT_S


def test_montage_assembler_render_timeout_marks_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_render_timeout"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": str(clip), "video_prompt": "Camera moves"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            return {"command": command, "returncode": 124, "timed_out": True, "stdout": "", "stderr": "timed out"}
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "error"
    render_report = json.loads((base / "99_final" / "render_report.json").read_text(encoding="utf-8"))
    assert "ffmpeg render failed" in render_report["errors"]
    assert not (base / "99_final" / "final_video.mp4").exists()


def test_montage_assembler_final_video_promoted_via_os_replace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_os_replace"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": str(clip), "video_prompt": "Camera moves"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    monkeypatch.setattr(montage_assembler, "_run_command", _video_only_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    final_dir = base / "99_final"
    assert result["status"] == "success"
    assert (final_dir / "final_video.mp4").exists()
    assert not list(final_dir.glob("final_video.mp4.tmp-*"))


def test_montage_assembler_partial_render_not_promoted_on_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_partial_render"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": str(clip), "video_prompt": "Camera moves"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"partial")
            return {"command": command, "returncode": 1, "stdout": "", "stderr": "boom"}
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    final_dir = base / "99_final"
    assert result["status"] == "error"
    assert not (final_dir / "final_video.mp4").exists()
    assert not list(final_dir.glob("final_video.mp4.tmp-*"))


def test_montage_assembler_flags_freeze_when_clips_shorter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_freeze"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip_one = _make_clip(base, 1, 1)
    clip_two = _make_clip(base, 1, 2)
    _scaffold(
        base,
        [
            {"scene_number": 1, "shot_number": 1, "timing": "00:00 - 00:04",
             "video_path": str(clip_one), "video_prompt": "Camera moves one"},
            {"scene_number": 1, "shot_number": 2, "timing": "00:04 - 00:08",
             "video_path": str(clip_two), "video_prompt": "Camera moves two"},
        ],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    monkeypatch.setattr(montage_assembler, "_run_command", _video_only_run)

    def fake_probe(path):
        if Path(path).name == "final_video.mp4":
            return _probe_payload(8.0)
        return _probe_payload(1.0)

    monkeypatch.setattr(montage_assembler, "_probe_media", fake_probe)

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "error"
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    assert review["checks"]["freeze"]["passed"] is False
    assert review["checks"]["freeze"]["freeze_ratio"] > 0.4


def test_montage_assembler_render_report_records_actual_vs_planned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_actual_vs_planned"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": str(clip), "video_prompt": "Camera moves"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    monkeypatch.setattr(montage_assembler, "_run_command", _video_only_run)

    def fake_probe(path):
        if Path(path).name == "final_video.mp4":
            return _probe_payload(4.0)
        return _probe_payload(3.5)

    monkeypatch.setattr(montage_assembler, "_probe_media", fake_probe)

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    render_report = json.loads((base / "99_final" / "render_report.json").read_text(encoding="utf-8"))
    entry = render_report["clips_actual_vs_planned"][0]
    assert entry["planned_duration_seconds"] == 4.0
    assert entry["actual_duration_seconds"] == 3.5
    assert render_report["freeze_seconds"] is not None


def test_montage_assembler_music_enabled_missing_audio_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_music_block"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": str(clip), "video_prompt": "Camera moves"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def forbidden_run(command, timeout=None):
        raise AssertionError(f"music_enabled without audio must block render: {command}")

    monkeypatch.setattr(montage_assembler, "_run_command", forbidden_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id, music_enabled=True)

    assert result["status"] == "error"
    render_report = json.loads((base / "99_final" / "render_report.json").read_text(encoding="utf-8"))
    assert "music_enabled=True but no usable audio track and allow_missing_audio=False" in render_report["errors"]


def test_montage_assembler_music_enabled_missing_audio_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_music_warn"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": str(clip), "video_prompt": "Camera moves"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    monkeypatch.setattr(montage_assembler, "_run_command", _video_only_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0))

    result = montage_assembler.montage_assembler_tool(
        "sess", project_id, allow_missing_audio=True, music_enabled=True
    )

    assert result["status"] == "success"
    render_report = json.loads((base / "99_final" / "render_report.json").read_text(encoding="utf-8"))
    assert "final_video_has_no_audio" in render_report["warnings"]


def test_montage_assembler_final_srt_built_from_actual_timeline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_srt_timeline"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip_one = _make_clip(base, 1, 1)
    clip_two = _make_clip(base, 1, 2)
    _scaffold(
        base,
        [
            {"scene_number": 1, "shot_number": 1, "timing": "00:00 - 00:04",
             "video_path": str(clip_one), "video_prompt": "Camera moves one"},
            {"scene_number": 1, "shot_number": 2, "timing": "00:04 - 00:08",
             "video_path": str(clip_two), "video_prompt": "Camera moves two"},
        ],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
        cue_sheet={
            "cues": [
                {"scene_number": 1, "shot_number": 1, "text": "Hello", "timeline": "planned"},
                {"scene_number": 1, "shot_number": 2, "text": "World", "timeline": "planned"},
                {"scene_number": 9, "shot_number": 9, "text": "Orphan", "timeline": "planned"},
            ]
        },
        subtitles="1\n00:00:00,000 --> 00:00:99,000\nLegacy\n",
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    monkeypatch.setattr(montage_assembler, "_run_command", _video_only_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(8.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    srt = (base / "99_final" / "subtitles.srt").read_text(encoding="utf-8")
    assert "Hello" in srt and "World" in srt
    assert "Orphan" not in srt
    assert "Legacy" not in srt
    assert "00:00:00,000 --> 00:00:04,000" in srt
    assert "00:00:04,000 --> 00:00:08,000" in srt
    assert srt.count("-->") == 2


def test_montage_assembler_broken_audio_manifest_surfaces_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_broken_manifest"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": str(clip), "video_prompt": "Camera moves"}],
    )
    (base / "98_audio" / "audio_manifest.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    monkeypatch.setattr(montage_assembler, "_run_command", _video_only_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    render_report = json.loads((base / "99_final" / "render_report.json").read_text(encoding="utf-8"))
    assert any("audio_manifest.json unreadable" in warning for warning in render_report["warnings"])


def test_montage_assembler_container_check_requires_probe_format(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_container_format"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": str(clip), "video_prompt": "Camera moves"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    monkeypatch.setattr(montage_assembler, "_run_command", _video_only_run)

    def fake_probe(path):
        if Path(path).name == "final_video.mp4":
            return {"format": {"format_name": "matroska,webm", "duration": "4.0"},
                    "streams": [{"codec_type": "video"}]}
        return _probe_payload(4.0)

    monkeypatch.setattr(montage_assembler, "_probe_media", fake_probe)

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "error"
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    assert review["checks"]["container"]["passed"] is False


def test_montage_assembler_resolves_relative_clip_paths_from_project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_relative_clip"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "4s",
          "video_path": "97_shots/scene_01_shot_01/video_final_01_01.mp4",
          "video_prompt": "Camera moves"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)
    commands = []

    def fake_run(command, timeout=None):
        commands.append(command)
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    render_command = next(c for c in commands if "-filter_complex" in c)
    assert str(clip.resolve()) in render_command


def test_montage_assembler_blackdetect_allowlisted_for_night_scene(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_black_night"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "6s",
          "video_path": str(clip), "video_prompt": "Night scene inside a dark room"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        if "blackdetect=d=0.5:pic_th=0.98" in command:
            return {"command": command, "returncode": 0, "stdout": "",
                    "stderr": "black_start:2.000 black_end:3.000 black_duration:1.000"}
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(6.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    black = review["checks"]["black_frame"]
    assert black["passed"] is True
    assert black["allowlisted_intervals"]
    assert black["allowlisted_intervals"][0]["reason"] == "dark_scene"


def test_montage_assembler_blackdetect_unexplained_midvideo_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project_id = "proj_black_mid"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "6s",
          "video_path": str(clip), "video_prompt": "Camera moves through a bright room"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        if "blackdetect=d=0.5:pic_th=0.98" in command:
            return {"command": command, "returncode": 0, "stdout": "",
                    "stderr": "black_start:2.000 black_end:3.000 black_duration:1.000"}
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(6.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "error"
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    black = review["checks"]["black_frame"]
    assert black["passed"] is False
    assert black["unexplained_intervals"]


def test_montage_assembler_blackdetect_whole_black_not_masked_as_fade(tmp_path, monkeypatch):
    """Полностью чёрный рендер [0, expected] НЕ маскируется как fade_in/out.

    Ранее fade-allowlist проверял только позицию (start<=окно / end>=конец-окно),
    поэтому интервал [0, 6] попадал под fade_in и success штамповался на чёрном
    видео. Теперь fade разрешён лишь при короткой длительности (<= окна)."""
    monkeypatch.chdir(tmp_path)
    project_id = "proj_black_whole"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "6s",
          "video_path": str(clip), "video_prompt": "Camera moves through a bright room"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        if "blackdetect=d=0.5:pic_th=0.98" in command:
            return {"command": command, "returncode": 0, "stdout": "",
                    "stderr": "black_start:0.000 black_end:6.000 black_duration:6.000"}
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(6.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "error"
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    black = review["checks"]["black_frame"]
    assert black["passed"] is False
    assert black["unexplained_intervals"]


def test_montage_assembler_blackdetect_short_fade_in_still_allowlisted(tmp_path, monkeypatch):
    """Короткий чёрный сегмент у старта (<= окна fade) остаётся легитимным fade_in —
    гейт длительности не должен переусердствовать и валить нормальные fade."""
    monkeypatch.chdir(tmp_path)
    project_id = "proj_black_short_fade"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "6s",
          "video_path": str(clip), "video_prompt": "Camera moves through a bright room"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        if "blackdetect=d=0.5:pic_th=0.98" in command:
            return {"command": command, "returncode": 0, "stdout": "",
                    "stderr": "black_start:0.000 black_end:1.000 black_duration:1.000"}
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(6.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    black = review["checks"]["black_frame"]
    assert black["passed"] is True
    assert black["allowlisted_intervals"][0]["reason"] == "fade_in"


def test_montage_assembler_whole_black_dark_scene_not_masked(tmp_path, monkeypatch):
    """WS-F: целиком чёрный клип (сбой рендера) в сцене с «тёмным» промптом НЕ должен
    маскироваться под dark_scene — иначе переоткрывается дыра честности fade-гейта."""
    monkeypatch.chdir(tmp_path)
    project_id = "proj_black_dark_whole"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "6s",
          "video_path": str(clip), "video_prompt": "Total blackout, pitch dark night scene"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        if "blackdetect=d=0.5:pic_th=0.98" in command:
            return {"command": command, "returncode": 0, "stdout": "",
                    "stderr": "black_start:0.000 black_end:6.000 black_duration:6.000"}
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(6.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "error"
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    black = review["checks"]["black_frame"]
    assert black["passed"] is False
    assert black["unexplained_intervals"]


def test_montage_assembler_partial_black_in_dark_scene_still_allowlisted(tmp_path, monkeypatch):
    """WS-F: короткий чёрный внутри тёмной сцены (не покрывает весь клип) остаётся
    легитимным dark_scene — coverage-гейт не должен переусердствовать."""
    monkeypatch.chdir(tmp_path)
    project_id = "proj_black_dark_partial"
    base = tmp_path / "plots" / "storybooks" / project_id
    clip = _make_clip(base)
    _scaffold(
        base,
        [{"scene_number": 1, "shot_number": 1, "timing": "6s",
          "video_path": str(clip), "video_prompt": "Total blackout, pitch dark night scene"}],
        audio_manifest={"tts_status": "unavailable", "audio_tracks": []},
    )
    monkeypatch.setattr(montage_assembler, "_find_executable", lambda name: name)

    def fake_run(command, timeout=None):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        if "blackdetect=d=0.5:pic_th=0.98" in command:
            return {"command": command, "returncode": 0, "stdout": "",
                    "stderr": "black_start:2.500 black_end:3.500 black_duration:1.000"}
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(6.0))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    review = json.loads((base / "99_final" / "final_review.json").read_text(encoding="utf-8"))
    black = review["checks"]["black_frame"]
    assert black["passed"] is True
    assert black["allowlisted_intervals"][0]["reason"] == "dark_scene"
