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

    def fake_run(command):
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

    def fake_run(command):
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

    def forbidden_run(command):
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

    def fake_run(command):
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

    def forbidden_run(command):
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
    assert "escapes storybook root" in result["message"]


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

    def fake_run(command):
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

    def fake_run(command):
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

    def fake_run(command):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"final")
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(montage_assembler, "_run_command", fake_run)
    monkeypatch.setattr(montage_assembler, "_probe_media", lambda path: _probe_payload(4.0, with_audio=False))

    result = montage_assembler.montage_assembler_tool("sess", project_id)

    assert result["status"] == "success"
    asset_manifest = json.loads((base / "99_final" / "asset_manifest.json").read_text(encoding="utf-8"))
    assert asset_manifest["audio"]["tts_status"] == "unavailable"
