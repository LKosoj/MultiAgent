import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from custom_tools.storybook.audio_subtitle import (
    _collect_logical_shots,
    _extract_items,
    _format_srt_timestamp,
    _parse_timing_range,
)
from custom_tools.storybook.project_paths import (
    safe_storybook_project_dir,
    storybook_projects_root,
)

_RENDER_TIMEOUT_S = 1800
_PROBE_TIMEOUT_S = 120
_ANALYZE_TIMEOUT_S = 300
_AUDIO_FADE_S = 1.0
_FREEZE_MAX_RATIO = 0.40
_FADE_WINDOW_S = 1.5
_DARK_TERMS = ("night", "fade", "blackout", "dark", "ночь", "затемнение", "чернота")


def montage_assembler_tool(
    session_id: str,
    project_id: str,
    language: str = "ru",
    enable: bool = True,
    allow_missing_audio: bool = False,
    music_enabled: bool = False,
) -> Dict[str, Any]:
    """Assemble storybook clips into final video artifacts and run QA checks.

    Args:
        session_id: Execution session identifier used in generated artifacts.
        project_id: Storybook project identifier under plots/storybooks.
        language: Language code stored in generated metadata.
        enable: When false, skip assembly and return planned output paths.
        allow_missing_audio: When true, allow a video-only montage if audio is missing.
        music_enabled: When true, music was requested; a missing audio track becomes a
            blocker (or warning when allow_missing_audio=True).
    """
    enable = _as_bool(enable)
    allow_missing_audio = _as_bool(allow_missing_audio)
    music_enabled = _as_bool(music_enabled)
    try:
        base_dir = _safe_project_dir(project_id)
    except ValueError as exc:
        return {
            "status": "error",
            "message": str(exc),
            "error": str(exc),
            "results": [],
        }
    final_dir = base_dir / "99_final"
    paths = {
        "final_video": final_dir / "final_video.mp4",
        "timeline": final_dir / "timeline.fcpxml",
        "subtitles": final_dir / "subtitles.srt",
        "manifest": final_dir / "manifest.json",
        "asset_manifest": final_dir / "asset_manifest.json",
        "edit_decisions": final_dir / "edit_decisions.json",
        "render_report": final_dir / "render_report.json",
        "final_review": final_dir / "final_review.json",
    }

    if not enable:
        return {
            "status": "skipped",
            "message": "Montage assembly disabled",
            "results": [],
            "final_video_path": str(paths["final_video"]),
            "timeline_path": str(paths["timeline"]),
            "subtitles_path": str(paths["subtitles"]),
            "manifest_path": str(paths["manifest"]),
            "final_review_path": str(paths["final_review"]),
        }

    shots_path = base_dir / "97_shots" / "shots.json"
    audio_dir = base_dir / "98_audio"
    final_dir.mkdir(parents=True, exist_ok=True)

    errors: List[str] = []
    warnings: List[str] = []
    try:
        shots_data = _read_json(shots_path)
    except Exception as exc:
        errors.append(f"Failed to read shots.json: {exc}")
        shots_data = None

    items = _collect_logical_shots(_extract_items(shots_data))
    clips = _build_clip_assets(items, base_dir)
    audio_artifacts = _read_audio_artifacts(audio_dir)
    _write_final_subtitles(clips, audio_artifacts, paths["subtitles"])

    missing_clips = [clip for clip in clips if not clip["exists"]]
    blocked_media_paths = [clip for clip in clips if not clip.get("path_allowed", True)]
    if not clips:
        errors.append("No video clips found in shots.json")
    if missing_clips:
        errors.append(f"Missing video clips: {len(missing_clips)}")
    if blocked_media_paths:
        errors.append(f"Blocked video paths outside project: {len(blocked_media_paths)}")

    manifest_read_error = audio_artifacts.get("audio_manifest_read_error")
    if manifest_read_error:
        message = f"audio_manifest.json unreadable: {manifest_read_error}"
        if music_enabled and not allow_missing_audio:
            errors.append(message)
        else:
            warnings.append(message)

    usable_audio_tracks = [track for track in audio_artifacts["audio_tracks"] if track["exists"]]
    audio_required = _audio_required(audio_artifacts)
    if not usable_audio_tracks and audio_required and not allow_missing_audio:
        errors.append("Audio track missing and allow_missing_audio=False")
    if music_enabled and not usable_audio_tracks:
        if not allow_missing_audio:
            errors.append("music_enabled=True but no usable audio track and allow_missing_audio=False")
        else:
            warnings.append("final_video_has_no_audio")

    ffmpeg_bin = _find_executable("ffmpeg")
    ffprobe_bin = _find_executable("ffprobe")
    if not ffmpeg_bin:
        errors.append("ffmpeg not available")
    if not ffprobe_bin:
        errors.append("ffprobe not available")

    timeline_clips = [clip for clip in clips if clip.get("path_allowed", True)]
    _write_fcpxml(paths["timeline"], str(project_id), timeline_clips)

    expected_duration = _expected_duration(clips)
    render_result: Optional[Dict[str, Any]] = None
    final_probe = None
    blackdetect_result = None
    volume_result = None
    final_review: Optional[Dict[str, Any]] = None
    try:
        _probe_clip_durations(clips, ffprobe_bin)
        fatal_errors = [
            error for error in errors
            if not error.startswith("ffprobe not available")
        ]
        if not fatal_errors:
            render_result = _render_final_video(
                ffmpeg_bin=ffmpeg_bin or "ffmpeg",
                clips=clips,
                audio_tracks=usable_audio_tracks,
                output_path=paths["final_video"],
                expected_duration=expected_duration,
            )
            if int(render_result.get("returncode", 1)) != 0:
                errors.append("ffmpeg render failed")
            elif not paths["final_video"].exists():
                errors.append("ffmpeg reported success but final_video.mp4 was not created")

        if paths["final_video"].exists() and ffprobe_bin:
            final_probe = _probe_media(paths["final_video"])
        if paths["final_video"].exists() and ffmpeg_bin:
            blackdetect_result = _run_blackdetect(ffmpeg_bin, paths["final_video"])
            volume_result = _run_volumedetect(ffmpeg_bin, paths["final_video"]) if _probe_has_audio(final_probe) else None

        final_review = _build_final_review(
            clips=clips,
            output_path=paths["final_video"],
            final_probe=final_probe,
            expected_duration=expected_duration,
            subtitles_path=paths["subtitles"],
            render_result=render_result,
            blackdetect_result=blackdetect_result,
            volume_result=volume_result,
            audio_required=audio_required and not allow_missing_audio,
            errors=errors,
            items=items,
            warnings=warnings,
            ffprobe_available=bool(ffprobe_bin),
        )
    except Exception as exc:
        errors.append(f"Montage assembly failed: {exc}")
        final_review = None

    if final_review is None:
        final_review = {
            "passed": False,
            "checks": {},
            "errors": errors,
            "warnings": warnings,
            "clip_count": len(clips),
            "expected_duration_seconds": expected_duration,
        }
    status = "success" if final_review["passed"] else "error"

    asset_manifest = {
        "project_id": str(project_id),
        "session_id": session_id,
        "language": language,
        "shots_path": str(shots_path),
        "clips": clips,
        "missing_clips": missing_clips,
        "audio": audio_artifacts,
        "audio_required": audio_required,
        "allow_missing_audio": allow_missing_audio,
    }
    edit_decisions = {
        "project_id": str(project_id),
        "session_id": session_id,
        "sequence": _build_edit_sequence(clips),
    }
    freeze_check = final_review.get("checks", {}).get("freeze", {})
    render_report = {
        "project_id": str(project_id),
        "session_id": session_id,
        "ffmpeg_available": bool(ffmpeg_bin),
        "ffprobe_available": bool(ffprobe_bin),
        "render_result": render_result,
        "blackdetect_result": blackdetect_result,
        "volume_result": volume_result,
        "expected_duration_seconds": expected_duration,
        "clips_actual_vs_planned": _clips_actual_vs_planned(clips),
        "freeze_seconds": freeze_check.get("freeze_seconds"),
        "freeze_ratio": freeze_check.get("freeze_ratio"),
        "final_probe": final_probe,
        "errors": errors,
        "warnings": warnings,
    }
    manifest = {
        "project_id": str(project_id),
        "session_id": session_id,
        "language": language,
        "status": status,
        "final_video_path": str(paths["final_video"]),
        "timeline_path": str(paths["timeline"]),
        "subtitles_path": str(paths["subtitles"]),
        "asset_manifest_path": str(paths["asset_manifest"]),
        "edit_decisions_path": str(paths["edit_decisions"]),
        "render_report_path": str(paths["render_report"]),
        "final_review_path": str(paths["final_review"]),
        "errors": errors,
        "warnings": warnings,
    }

    _write_json(paths["asset_manifest"], asset_manifest)
    _write_json(paths["edit_decisions"], edit_decisions)
    _write_json(paths["render_report"], render_report)
    _write_json(paths["final_review"], final_review)
    _write_json(paths["manifest"], manifest)

    message = "Final montage assembled" if status == "success" else ("; ".join(errors) or "Final montage QA failed")
    result = {
        "status": status,
        "message": message,
        "final_video_path": str(paths["final_video"]),
        "timeline_path": str(paths["timeline"]),
        "subtitles_path": str(paths["subtitles"]),
        "manifest_path": str(paths["manifest"]),
        "final_review_path": str(paths["final_review"]),
        "results": {
            "final_video_path": str(paths["final_video"]),
            "timeline_path": str(paths["timeline"]),
            "subtitles_path": str(paths["subtitles"]),
            "manifest_path": str(paths["manifest"]),
            "render_report_path": str(paths["render_report"]),
            "final_review_path": str(paths["final_review"]),
        },
    }
    if status == "error":
        result["error"] = message
    return result


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.tmp-{os.getpid()}"
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def _is_non_empty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _build_clip_assets(items: List[Dict[str, Any]], project_dir: Path) -> List[Dict[str, Any]]:
    clips: List[Dict[str, Any]] = []
    cursor = 0.0
    for item in items:
        video_path = str(item.get("video_path") or "").strip()
        if not video_path:
            continue
        resolved_path, path_allowed = _resolve_project_media_path(video_path, project_dir)
        timing = _parse_timing_range(item.get("timing"), cursor)
        if timing is None:
            planned_start, planned_end = cursor, cursor
            timing_mode = "unavailable"
        else:
            planned_start, planned_end, timing_mode = timing
            cursor = max(cursor, planned_end)
        duration = max(0.0, planned_end - planned_start)
        clips.append(
            {
                "scene_number": item.get("scene_number"),
                "shot_number": item.get("shot_number"),
                "path": str(resolved_path),
                "source_path": video_path,
                "path_allowed": path_allowed,
                "exists": path_allowed and resolved_path.exists(),
                "planned_start_seconds": round(planned_start, 3),
                "planned_end_seconds": round(planned_end, 3),
                "planned_duration_seconds": round(duration, 3),
                "timing": item.get("timing"),
                "timing_mode": timing_mode,
                "video_prompt": str(item.get("video_prompt") or ""),
                "camera_text": _camera_text(item),
            }
        )
    return clips


def _camera_text(item: Dict[str, Any]) -> str:
    return " ".join(
        str(item.get(field) or "").strip()
        for field in ["camera_plan", "camera_position", "spatial_composition"]
        if str(item.get(field) or "").strip()
    )


def _probe_clip_durations(clips: List[Dict[str, Any]], ffprobe_bin: Optional[str]) -> None:
    if not ffprobe_bin:
        return
    for clip in clips:
        if not clip.get("exists"):
            continue
        probe = _probe_media(Path(clip["path"]))
        clip["actual_duration_seconds"] = _probe_duration(probe)


def _clips_actual_vs_planned(clips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "scene_number": clip.get("scene_number"),
            "shot_number": clip.get("shot_number"),
            "planned_duration_seconds": round(_clip_duration(clip), 3),
            "actual_duration_seconds": clip.get("actual_duration_seconds"),
        }
        for clip in clips
    ]


def _read_audio_artifacts(audio_dir: Path) -> Dict[str, Any]:
    subtitles_path = audio_dir / "subtitles.srt"
    manifest_path = audio_dir / "audio_manifest.json"
    cue_sheet_path = audio_dir / "cue_sheet.json"
    manifest: Dict[str, Any] = {}
    read_error: Optional[str] = None
    if manifest_path.exists():
        try:
            loaded = _read_json(manifest_path)
        except Exception as exc:
            read_error = str(exc)
        else:
            if isinstance(loaded, dict):
                manifest = loaded
            else:
                read_error = "audio_manifest.json is not a JSON object"

    tracks = _extract_audio_tracks(manifest, audio_dir)
    return {
        "subtitles_path": str(subtitles_path),
        "subtitles_exists": subtitles_path.exists(),
        "subtitles_non_empty": subtitles_path.exists() and bool(subtitles_path.read_text(encoding="utf-8").strip()),
        "audio_manifest_path": str(manifest_path),
        "audio_manifest_exists": manifest_path.exists(),
        "audio_manifest_read_error": read_error,
        "cue_sheet_path": str(cue_sheet_path),
        "cue_sheet_exists": cue_sheet_path.exists(),
        "tts_status": manifest.get("tts_status") or "unavailable",
        "audio_tracks": tracks,
    }


def _extract_audio_tracks(manifest: Dict[str, Any], audio_dir: Path) -> List[Dict[str, Any]]:
    raw_tracks = manifest.get("audio_tracks")
    if not isinstance(raw_tracks, list):
        raw_tracks = []
    if manifest.get("audio_path"):
        raw_tracks.append({"path": manifest.get("audio_path"), "role": "main"})

    tracks: List[Dict[str, Any]] = []
    for index, track in enumerate(raw_tracks):
        if isinstance(track, str):
            path = track
            role = "audio"
        elif isinstance(track, dict):
            path = track.get("path") or track.get("audio_path") or track.get("file") or track.get("source")
            role = track.get("role") or "audio"
        else:
            continue
        if not path:
            continue
        track_path, path_allowed = _resolve_audio_path(str(path), audio_dir)
        tracks.append(
            {
                "index": index,
                "role": role,
                "path": str(track_path),
                "source_path": str(path),
                "path_allowed": path_allowed,
                "exists": path_allowed and track_path.exists(),
            }
        )
    return tracks


def _write_final_subtitles(
    clips: List[Dict[str, Any]],
    audio_artifacts: Dict[str, Any],
    target_path: Path,
) -> None:
    cue_texts = _load_cue_texts(Path(audio_artifacts.get("cue_sheet_path") or "."))
    if cue_texts is not None:
        sequence = _build_edit_sequence(clips)
        _atomic_write_text(target_path, _render_timeline_srt(sequence, cue_texts))
        return
    source = Path(audio_artifacts.get("subtitles_path") or ".")
    text = source.read_text(encoding="utf-8") if source.is_file() else ""
    _atomic_write_text(target_path, text)


def _load_cue_texts(cue_sheet_path: Path) -> Optional[Dict[Any, str]]:
    if not cue_sheet_path.is_file():
        return None
    try:
        data = _read_json(cue_sheet_path)
    except Exception:
        return None
    cues = data.get("cues") if isinstance(data, dict) else None
    if not isinstance(cues, list):
        return None
    texts: Dict[Any, str] = {}
    for cue in cues:
        if not isinstance(cue, dict):
            continue
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        texts[(cue.get("scene_number"), cue.get("shot_number"))] = text
    return texts


def _render_timeline_srt(sequence: List[Dict[str, Any]], cue_texts: Dict[Any, str]) -> str:
    blocks = []
    index = 0
    for entry in sequence:
        text = cue_texts.get((entry.get("scene_number"), entry.get("shot_number")))
        if not text:
            continue
        index += 1
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{entry['start_timecode']} --> {entry['end_timecode']}",
                    text,
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _write_fcpxml(path: Path, project_id: str, clips: List[Dict[str, Any]]) -> None:
    fcpxml = ET.Element("fcpxml", version="1.11")
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(resources, "format", id="r0", name="FFVideoFormat1080p25", frameDuration="1/25s", width="1920", height="1080")
    for index, clip in enumerate(clips, start=1):
        asset = ET.SubElement(
            resources,
            "asset",
            id=f"r{index}",
            name=Path(clip["path"]).name,
            src=_path_uri(clip["path"]),
            hasVideo="1",
        )
        duration = max(float(clip.get("planned_duration_seconds") or 0), 1.0)
        asset.set("duration", _fcpx_duration(duration))

    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", name=f"storybook-{project_id}")
    project = ET.SubElement(event, "project", name=f"{project_id}-final")
    sequence = ET.SubElement(project, "sequence", format="r0", duration=_fcpx_duration(_expected_duration(clips)))
    spine = ET.SubElement(sequence, "spine")
    for index, clip in enumerate(clips, start=1):
        duration = max(float(clip.get("planned_duration_seconds") or 0), 1.0)
        ET.SubElement(
            spine,
            "asset-clip",
            name=Path(clip["path"]).name,
            ref=f"r{index}",
            offset=_fcpx_duration(_clip_start(clip)),
            duration=_fcpx_duration(duration),
            start="0s",
        )

    ET.indent(fcpxml, space="  ")
    tree = ET.ElementTree(fcpxml)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.tmp-{os.getpid()}"
    with tmp_path.open("wb") as output:
        output.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        output.write(b"<!DOCTYPE fcpxml>\n")
        tree.write(output, encoding="utf-8", xml_declaration=False)
    os.replace(tmp_path, path)


def _path_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _fcpx_duration(seconds: float) -> str:
    milliseconds = max(1, int(round(seconds * 1000)))
    return f"{milliseconds}/1000s"


def _expected_duration(clips: List[Dict[str, Any]]) -> float:
    if not clips:
        return 0.0
    return round(max(_clip_end(clip) for clip in clips), 3)


def _build_edit_sequence(clips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sequence = []
    for index, clip in enumerate(clips, start=1):
        start = _clip_start(clip)
        end = _clip_end(clip)
        sequence.append(
            {
                "index": index,
                "scene_number": clip.get("scene_number"),
                "shot_number": clip.get("shot_number"),
                "source_path": clip.get("path"),
                "timeline_start_seconds": round(start, 3),
                "timeline_end_seconds": round(end, 3),
                "start_timecode": _format_srt_timestamp(start),
                "end_timecode": _format_srt_timestamp(end),
            }
        )
    return sequence


def _render_final_video(
    ffmpeg_bin: str,
    clips: List[Dict[str, Any]],
    audio_tracks: List[Dict[str, Any]],
    output_path: Path,
    expected_duration: float,
    loop_audio: bool = False,
) -> Dict[str, Any]:
    tmp_path = output_path.parent / f"{output_path.name}.tmp-{os.getpid()}"
    command = [
        ffmpeg_bin,
        "-y",
    ]
    for clip in clips:
        command.extend(["-i", clip["path"]])
    if audio_tracks:
        command.extend(["-i", audio_tracks[0]["path"]])

    filter_complex = _build_video_filter(clips)
    if audio_tracks:
        filter_complex = f"{filter_complex};{_build_audio_filter(len(clips), expected_duration, loop_audio)}"
    command.extend(["-filter_complex", filter_complex, "-map", "[vout]"])
    if audio_tracks:
        command.extend(["-map", "[aout]", "-c:a", "aac"])
    command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    if expected_duration > 0:
        command.extend(["-t", f"{expected_duration:.3f}"])
    command.append(str(tmp_path))

    result = _run_command(command, timeout=_RENDER_TIMEOUT_S)
    if int(result.get("returncode", 1)) == 0 and _is_non_empty_file(tmp_path):
        os.replace(tmp_path, output_path)
    else:
        _safe_unlink(tmp_path)
    return result


def _build_audio_filter(audio_input_index: int, duration: float, loop_audio: bool) -> str:
    duration = max(0.0, duration)
    fade_start = max(0.0, duration - _AUDIO_FADE_S)
    base = "aloop=loop=-1:size=2147483647" if loop_audio else "apad"
    return (
        f"[{audio_input_index}:a:0]{base},atrim=0:{duration:.3f},"
        f"afade=t=out:st={fade_start:.3f}:d={_AUDIO_FADE_S:.3f}[aout]"
    )


def _build_video_filter(clips: List[Dict[str, Any]]) -> str:
    parts = []
    labels = []
    cursor = 0.0
    for index, clip in enumerate(clips):
        start = max(_clip_start(clip), cursor)
        if start > cursor:
            gap_label = f"gap{index}"
            gap_duration = start - cursor
            parts.append(
                f"[{index}:v:0]"
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
                "fps=24,setsar=1,"
                "trim=duration=0.001,setpts=PTS-STARTPTS,"
                f"tpad=stop_mode=clone:stop_duration={gap_duration:.3f},"
                f"trim=duration={gap_duration:.3f},setpts=PTS-STARTPTS"
                f"[{gap_label}]"
            )
            labels.append(f"[{gap_label}]")
            cursor = start
        duration = max(_clip_duration(clip), 1.0)
        label = f"v{index}"
        labels.append(f"[{label}]")
        parts.append(
            f"[{index}:v:0]"
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
            "fps=24,setsar=1,"
            f"trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={duration:.3f},"
            f"trim=duration={duration:.3f},setpts=PTS-STARTPTS"
            f"[{label}]"
        )
        cursor += duration
    parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[vout]")
    return ";".join(parts)


def _find_executable(name: str) -> Optional[str]:
    return shutil.which(name)


def _run_command(command: List[str], timeout: Optional[float] = None) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": 124,
            "timed_out": True,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + f"\nsubprocess timed out after {timeout}s",
        }
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _probe_media(path: Path) -> Dict[str, Any]:
    ffprobe_bin = _find_executable("ffprobe") or "ffprobe"
    result = _run_command(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=_PROBE_TIMEOUT_S,
    )
    if int(result.get("returncode", 1)) != 0:
        return {"error": result.get("stderr") or "ffprobe failed", "probe_result": result}
    try:
        return json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError as exc:
        return {"error": f"Invalid ffprobe JSON: {exc}", "probe_result": result}


def _run_blackdetect(ffmpeg_bin: str, path: Path) -> Dict[str, Any]:
    result = _run_command(
        [
            ffmpeg_bin,
            "-i",
            str(path),
            "-vf",
            "blackdetect=d=0.5:pic_th=0.98",
            "-an",
            "-f",
            "null",
            "-",
        ],
        timeout=_ANALYZE_TIMEOUT_S,
    )
    stderr = str(result.get("stderr") or "")
    intervals = _parse_black_intervals(stderr)
    return {
        "command": result.get("command"),
        "returncode": result.get("returncode"),
        "black_frames_detected": bool(intervals),
        "black_intervals": intervals,
        "stderr": stderr,
    }


def _parse_black_intervals(stderr: str) -> List[Dict[str, float]]:
    intervals: List[Dict[str, float]] = []
    for match in re.finditer(
        r"black_start:(\d+(?:\.\d+)?)\s+black_end:(\d+(?:\.\d+)?)", stderr
    ):
        intervals.append({"start": float(match.group(1)), "end": float(match.group(2))})
    return intervals


def _run_volumedetect(ffmpeg_bin: str, path: Path) -> Dict[str, Any]:
    result = _run_command(
        [
            ffmpeg_bin,
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        timeout=_ANALYZE_TIMEOUT_S,
    )
    stderr = str(result.get("stderr") or "")
    return {
        "command": result.get("command"),
        "returncode": result.get("returncode"),
        "mean_volume_db": _parse_volume(stderr, "mean_volume"),
        "max_volume_db": _parse_volume(stderr, "max_volume"),
        "stderr": stderr,
    }


def _build_final_review(
    clips: List[Dict[str, Any]],
    output_path: Path,
    final_probe: Optional[Dict[str, Any]],
    expected_duration: float,
    subtitles_path: Path,
    render_result: Optional[Dict[str, Any]],
    blackdetect_result: Optional[Dict[str, Any]],
    volume_result: Optional[Dict[str, Any]],
    audio_required: bool,
    errors: List[str],
    items: List[Dict[str, Any]],
    warnings: Optional[List[str]] = None,
    ffprobe_available: bool = True,
) -> Dict[str, Any]:
    container = _container_check(output_path, final_probe)
    duration = _duration_check(expected_duration, final_probe)
    black_frame = _black_frame_check(blackdetect_result, clips, expected_duration)
    audio = _audio_check(final_probe, volume_result, audio_required)
    subtitles = _subtitle_check(subtitles_path)
    monotony = _monotony_check(items)
    freeze = _freeze_check(clips, expected_duration, ffprobe_available)

    checks = {
        "container": container,
        "duration": duration,
        "black_frame": black_frame,
        "audio": audio,
        "subtitles": subtitles,
        "monotony": monotony,
        "freeze": freeze,
    }
    review_warnings = list(warnings or [])
    if black_frame.get("allowlisted_intervals"):
        review_warnings.append(
            f"black_frame allowlisted transitions: {len(black_frame['allowlisted_intervals'])}"
        )
    passed = not errors and all(check.get("passed", False) for check in checks.values())
    return {
        "passed": passed,
        "checks": checks,
        "errors": errors,
        "warnings": review_warnings,
        "clip_count": len(clips),
        "expected_duration_seconds": expected_duration,
    }


def _freeze_check(
    clips: List[Dict[str, Any]],
    expected_duration: float,
    ffprobe_available: bool,
) -> Dict[str, Any]:
    probed = [clip for clip in clips if clip.get("actual_duration_seconds") is not None]
    if not ffprobe_available or not probed:
        return {
            "passed": True,
            "status": "unavailable",
            "freeze_seconds": None,
            "freeze_ratio": None,
            "threshold": _FREEZE_MAX_RATIO,
        }
    freeze_seconds = sum(
        max(0.0, _clip_duration(clip) - float(clip["actual_duration_seconds"]))
        for clip in probed
    )
    ratio = freeze_seconds / expected_duration if expected_duration > 0 else 0.0
    passed = ratio <= _FREEZE_MAX_RATIO
    return {
        "passed": passed,
        "status": "ok" if passed else "excessive_freeze",
        "freeze_seconds": round(freeze_seconds, 3),
        "freeze_ratio": round(ratio, 3),
        "threshold": _FREEZE_MAX_RATIO,
    }


def _container_check(output_path: Path, final_probe: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    streams = final_probe.get("streams", []) if isinstance(final_probe, dict) else []
    format_name = ((final_probe or {}).get("format") or {}).get("format_name", "")
    has_video = any(stream.get("codec_type") == "video" for stream in streams if isinstance(stream, dict))
    probe_error = (final_probe or {}).get("error") if isinstance(final_probe, dict) else None
    probe_available = isinstance(final_probe, dict) and not probe_error and bool(final_probe.get("format") or streams)
    if probe_available:
        passed = output_path.exists() and has_video and "mp4" in str(format_name)
    else:
        passed = output_path.exists() and output_path.suffix == ".mp4"
    return {
        "passed": passed,
        "output_exists": output_path.exists(),
        "format_name": format_name,
        "has_video": has_video,
        "probe_available": probe_available,
        "probe_error": probe_error,
    }


def _duration_check(expected_duration: float, final_probe: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    actual_duration = _probe_duration(final_probe)
    if expected_duration <= 0 or actual_duration is None:
        return {
            "passed": False,
            "expected_duration_seconds": expected_duration,
            "actual_duration_seconds": actual_duration,
            "drift_seconds": None,
        }
    drift = abs(actual_duration - expected_duration)
    tolerance = max(1.0, expected_duration * 0.08)
    return {
        "passed": drift <= tolerance,
        "expected_duration_seconds": expected_duration,
        "actual_duration_seconds": actual_duration,
        "drift_seconds": round(drift, 3),
        "tolerance_seconds": round(tolerance, 3),
    }


def _probe_duration(final_probe: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(final_probe, dict):
        return None
    try:
        return float((final_probe.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        return None


def _black_frame_check(
    blackdetect_result: Optional[Dict[str, Any]],
    clips: List[Dict[str, Any]],
    expected_duration: float,
) -> Dict[str, Any]:
    analyzer_passed = isinstance(blackdetect_result, dict) and int(blackdetect_result.get("returncode", 1)) == 0
    intervals = blackdetect_result.get("black_intervals") if isinstance(blackdetect_result, dict) else None
    if not isinstance(intervals, list):
        intervals = []
    signal = bool(intervals) or (
        isinstance(blackdetect_result, dict) and bool(blackdetect_result.get("black_frames_detected"))
    )
    allowlisted: List[Dict[str, Any]] = []
    unexplained: List[Dict[str, float]] = []
    for interval in intervals:
        reason = _classify_black_interval(interval, clips, expected_duration)
        if reason:
            allowlisted.append({**interval, "reason": reason})
        else:
            unexplained.append(interval)
    return {
        "passed": analyzer_passed and not unexplained,
        "signal_reported": signal,
        "analyzer_passed": analyzer_passed,
        "black_intervals": intervals,
        "allowlisted_intervals": allowlisted,
        "unexplained_intervals": unexplained,
    }


def _classify_black_interval(
    interval: Dict[str, float],
    clips: List[Dict[str, Any]],
    expected_duration: float,
) -> Optional[str]:
    try:
        start = float(interval.get("start"))
        end = float(interval.get("end"))
    except (TypeError, ValueError):
        return None
    # Легитимный fade — КОРОТКИЙ чёрный сегмент у края. Разрешаем по позиции
    # ТОЛЬКО вместе с ограничением длительности (<= окна fade): иначе целиком
    # чёрный рендер [0, expected] или длинная чёрная голова/хвост попадёт под
    # start<=window / end>=expected-window и замаскирует дефект как «fade».
    interval_len = end - start
    if start <= _FADE_WINDOW_S and interval_len <= _FADE_WINDOW_S:
        return "fade_in"
    if (
        expected_duration > 0
        and end >= expected_duration - _FADE_WINDOW_S
        and interval_len <= _FADE_WINDOW_S
    ):
        return "fade_out"
    for clip in clips:
        clip_start = _clip_start(clip)
        clip_end = _clip_end(clip)
        if clip_start <= end and clip_end >= start:
            # WS-F: тёмная сцена (ночь/blackout по промпту) — легитимный АТМОСФЕРНЫЙ чёрный,
            # но НЕ когда чёрное покрывает практически весь клип: тогда это чисто чёрный
            # рендер (сбой генерации/склейки), и его нельзя маскировать под «dark_scene»
            # (иначе переоткрывается ровно та дыра честности, что закрыл fade-гейт выше).
            clip_len = max(0.0, clip_end - clip_start)
            overlap = max(0.0, min(end, clip_end) - max(start, clip_start))
            if clip_len > 0 and overlap >= 0.9 * clip_len:
                continue
            haystack = f"{clip.get('video_prompt') or ''} {clip.get('camera_text') or ''}".lower()
            if any(term in haystack for term in _DARK_TERMS):
                return "dark_scene"
    return None


def _audio_check(
    final_probe: Optional[Dict[str, Any]],
    volume_result: Optional[Dict[str, Any]],
    audio_required: bool,
) -> Dict[str, Any]:
    has_audio = _probe_has_audio(final_probe)
    volume = _extract_volume(volume_result)
    if not audio_required and not has_audio:
        return {
            "passed": True,
            "has_audio": False,
            "volume_db": volume,
            "status": "not_available",
        }
    passed = has_audio and volume is not None and volume > -55.0
    return {
        "passed": passed,
        "has_audio": has_audio,
        "volume_db": volume,
        "status": "ok" if passed else "missing_or_silent",
    }


def _extract_volume(render_result: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(render_result, dict):
        return None
    for key in ["audio_volume_db", "mean_volume_db", "max_volume_db"]:
        try:
            return float(render_result[key])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _subtitle_check(subtitles_path: Path) -> Dict[str, Any]:
    text = subtitles_path.read_text(encoding="utf-8") if subtitles_path.exists() else ""
    cue_count = text.count("-->")
    return {
        "passed": cue_count > 0,
        "path": str(subtitles_path),
        "exists": subtitles_path.exists(),
        "cue_count": cue_count,
    }


def _monotony_check(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    prompts = [_normalize_text(str(item.get("video_prompt") or item.get("english_prompt") or "")) for item in items]
    cameras = [_normalize_text(_camera_text(item)) for item in items]
    prompt_values = [value for value in prompts if value]
    camera_values = [value for value in cameras if value]
    prompt_repeat = _max_repeat_ratio(prompt_values)
    camera_repeat = _max_repeat_ratio(camera_values)
    static_ratio = _static_prompt_ratio(prompts)
    risk = "low"
    has_prompt_sample = len(prompt_values) >= 3
    has_camera_sample = len(camera_values) >= 3
    if len(items) >= 3 and (
        (has_prompt_sample and prompt_repeat >= 0.6)
        or (has_camera_sample and camera_repeat >= 0.75)
        or static_ratio >= 0.7
    ):
        risk = "high"
    elif len(items) >= 3 and (
        (has_prompt_sample and prompt_repeat >= 0.4)
        or (has_camera_sample and camera_repeat >= 0.55)
        or static_ratio >= 0.5
    ):
        risk = "medium"
    return {
        "passed": risk != "high",
        "risk": risk,
        "prompt_repeat_ratio": round(prompt_repeat, 3),
        "camera_repeat_ratio": round(camera_repeat, 3),
        "static_prompt_ratio": round(static_ratio, 3),
    }


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def _max_repeat_ratio(values: List[str]) -> float:
    if not values:
        return 0.0
    counts: Dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts.values()) / len(values)


def _static_prompt_ratio(prompts: List[str]) -> float:
    prompts = [prompt for prompt in prompts if prompt]
    if not prompts:
        return 0.0
    static_terms = ("static", "lock off", "locked off", "still", "no movement", "статич")
    static_count = sum(1 for prompt in prompts if any(term in prompt for term in static_terms))
    return static_count / len(prompts)


def _safe_project_dir(project_id: str) -> Path:
    return safe_storybook_project_dir(project_id)


def _resolve_project_media_path(raw_path: str, project_dir: Path) -> tuple[Path, bool]:
    value = str(raw_path or "").strip()
    if not value:
        return project_dir, False
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        return resolved, _is_relative_to(resolved, project_dir)
    primary = (project_dir / candidate).resolve()
    if primary.exists():
        return primary, _is_relative_to(primary, project_dir)
    fallback = (storybook_projects_root() / candidate).resolve()
    if fallback.exists() and _is_relative_to(fallback, project_dir):
        return fallback, True
    return primary, _is_relative_to(primary, project_dir)


def _resolve_audio_path(raw_path: str, audio_dir: Path) -> tuple[Path, bool]:
    value = str(raw_path or "").strip()
    if not value:
        return audio_dir, False
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (audio_dir / candidate).resolve()
    return resolved, _is_relative_to(resolved, audio_dir.parent)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _clip_duration(clip: Dict[str, Any]) -> float:
    try:
        return max(0.0, float(clip.get("planned_duration_seconds") or 0))
    except (TypeError, ValueError):
        return 0.0


def _clip_start(clip: Dict[str, Any]) -> float:
    try:
        return max(0.0, float(clip.get("planned_start_seconds") or 0))
    except (TypeError, ValueError):
        return 0.0


def _clip_end(clip: Dict[str, Any]) -> float:
    explicit_end = clip.get("planned_end_seconds")
    try:
        if explicit_end not in (None, ""):
            return max(_clip_start(clip), float(explicit_end))
    except (TypeError, ValueError):
        pass
    return _clip_start(clip) + _clip_duration(clip)


def _probe_has_audio(final_probe: Optional[Dict[str, Any]]) -> bool:
    streams = final_probe.get("streams", []) if isinstance(final_probe, dict) else []
    return any(stream.get("codec_type") == "audio" for stream in streams if isinstance(stream, dict))


def _parse_volume(stderr: str, key: str) -> Optional[float]:
    marker = f"{key}:"
    for line in stderr.splitlines():
        if marker not in line:
            continue
        try:
            value = line.split(marker, 1)[1].strip().split(" ", 1)[0]
            return float(value)
        except (IndexError, ValueError):
            return None
    return None


def _audio_required(audio_artifacts: Dict[str, Any]) -> bool:
    if audio_artifacts.get("audio_tracks"):
        return True
    tts_status = str(audio_artifacts.get("tts_status") or "").strip().lower()
    return tts_status not in {"", "none", "skipped", "unavailable"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
