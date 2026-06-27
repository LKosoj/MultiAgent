import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def storybook_audio_subtitle_tool(
    session_id: str,
    project_id: str,
    language: str = "ru",
    enable: bool = True,
) -> Dict[str, Any]:
    """Create subtitle and audio planning artifacts for a storybook project.

    Args:
        session_id: Execution session identifier used in generated artifacts.
        project_id: Storybook project identifier under plots/storybooks.
        language: Language code stored in generated metadata.
        enable: When false, skip artifact generation and return planned output paths.
    """
    enable = _as_bool(enable)
    try:
        base_dir = _safe_project_dir(project_id)
    except ValueError as exc:
        return {
            "status": "error",
            "message": str(exc),
            "error": str(exc),
            "results": [],
        }
    audio_dir = base_dir / "98_audio"
    subtitles_path = audio_dir / "subtitles.srt"
    manifest_path = audio_dir / "audio_manifest.json"
    cue_sheet_path = audio_dir / "cue_sheet.json"

    if not enable:
        return {
            "status": "skipped",
            "message": "Audio/subtitle generation disabled",
            "results": [],
            "subtitles_path": str(subtitles_path),
            "audio_manifest_path": str(manifest_path),
            "cue_sheet_path": str(cue_sheet_path),
        }

    shots_path = base_dir / "97_shots" / "shots.json"
    screenplay_path = base_dir / "91_screenplay" / "screenplay.json"
    audio_dir.mkdir(parents=True, exist_ok=True)

    errors: List[str] = []
    shots_data: Any = None
    if shots_path.exists():
        try:
            shots_data = _read_json(shots_path)
        except Exception as exc:
            errors.append(f"Failed to read shots.json: {exc}")
            shots_data = None
    else:
        errors.append(f"shots.json not found: {shots_path}")

    screenplay_data: Any = None
    if screenplay_path.exists():
        try:
            screenplay_data = _read_json(screenplay_path)
        except Exception as exc:
            errors.append(f"Failed to read screenplay.json: {exc}")

    items = _collect_logical_shots(_extract_items(shots_data))
    screenplay_lookup = _build_screenplay_lookup(screenplay_data)
    cues, skipped = _build_cues(items, screenplay_lookup)

    subtitles_path.write_text(_format_srt(cues), encoding="utf-8")

    cue_sheet = {
        "project_id": str(project_id),
        "session_id": session_id,
        "language": language,
        "source_shots_path": str(shots_path),
        "source_screenplay_path": str(screenplay_path) if screenplay_path.exists() else None,
        "cue_count": len(cues),
        "cues": cues,
        "skipped": skipped,
    }
    _write_json(cue_sheet_path, cue_sheet)

    audio_manifest = {
        "project_id": str(project_id),
        "session_id": session_id,
        "language": language,
        "tts_status": "unavailable",
        "tts_reason": "No external TTS backend is called by this tool",
        "audio_tracks": [],
        "subtitles_path": str(subtitles_path),
        "cue_sheet_path": str(cue_sheet_path),
        "cue_count": len(cues),
        "sources": {
            "shots_json": str(shots_path) if shots_path.exists() else None,
            "screenplay_json": str(screenplay_path) if screenplay_path.exists() else None,
        },
        "errors": errors,
    }
    _write_json(manifest_path, audio_manifest)

    status = "error" if errors and not cues else "success"
    message = f"Generated {len(cues)} subtitle cues; TTS unavailable"
    if errors and not cues:
        message = "; ".join(errors)

    result = {
        "status": status,
        "message": message,
        "subtitles_path": str(subtitles_path),
        "audio_manifest_path": str(manifest_path),
        "cue_sheet_path": str(cue_sheet_path),
        "cue_count": len(cues),
        "tts_status": "unavailable",
        "results": {
            "subtitles_path": str(subtitles_path),
            "audio_manifest_path": str(manifest_path),
            "cue_sheet_path": str(cue_sheet_path),
            "cue_count": len(cues),
            "tts_status": "unavailable",
        },
    }
    if status == "error":
        result["error"] = message
    return result


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_items(shots_data: Any) -> List[Dict[str, Any]]:
    if isinstance(shots_data, dict) and isinstance(shots_data.get("items"), list):
        return [item for item in shots_data["items"] if isinstance(item, dict)]
    if isinstance(shots_data, list):
        return [item for item in shots_data if isinstance(item, dict)]
    return []


def _collect_logical_shots(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[Any, Any, int], Dict[str, Any]] = {}
    scores: Dict[Tuple[Any, Any, int], int] = {}

    for index, item in enumerate(items):
        scene_number = item.get("scene_number")
        shot_number = item.get("shot_number")
        key = (
            scene_number if scene_number is not None else f"scene_{index}",
            shot_number if shot_number is not None else f"shot_{index}",
            index if scene_number is None or shot_number is None else 0,
        )
        score = _shot_type_score(item.get("shot_type"))
        if key not in grouped or score < scores[key]:
            grouped[key] = item
            scores[key] = score

    return sorted(grouped.values(), key=_shot_sort_key)


def _shot_type_score(shot_type: Any) -> int:
    value = str(shot_type or "").strip().lower()
    if value == "start" or not value:
        return 0
    if value == "end":
        return 2
    return 1


def _shot_sort_key(item: Dict[str, Any]) -> Tuple[int, int, int]:
    return (
        _to_int(item.get("scene_number"), 999999),
        _to_int(item.get("shot_number"), 999999),
        _to_int(item.get("number"), 999999),
    )


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_screenplay_lookup(screenplay_data: Any) -> Dict[str, Dict[Tuple[Any, Any], Any]]:
    scenes = []
    if isinstance(screenplay_data, dict) and isinstance(screenplay_data.get("screenplay"), list):
        scenes = screenplay_data["screenplay"]
    elif isinstance(screenplay_data, list):
        scenes = screenplay_data

    shot_text: Dict[Tuple[Any, Any], str] = {}
    scene_text: Dict[Tuple[Any, Any], str] = {}
    shot_timing: Dict[Tuple[Any, Any], Any] = {}
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        scene_number = scene.get("scene_number")
        scene_value = _first_text(scene, ["subtitle", "voiceover", "narration", "dialogue", "action"])
        if scene_value:
            scene_text[(scene_number, None)] = scene_value
        storyboard = scene.get("storyboard")
        if not isinstance(storyboard, list):
            continue
        for shot in storyboard:
            if not isinstance(shot, dict):
                continue
            text = _first_text(
                shot,
                ["subtitle", "subtitle_text", "caption", "voiceover", "narration", "dialogue", "description"],
            )
            if text:
                shot_text[(scene_number, shot.get("shot_number"))] = text
            if shot.get("timing") not in (None, ""):
                shot_timing[(scene_number, shot.get("shot_number"))] = shot.get("timing")

    return {"shots": shot_text, "scenes": scene_text, "timings": shot_timing}


def _build_cues(
    items: List[Dict[str, Any]],
    screenplay_lookup: Dict[str, Dict[Tuple[Any, Any], Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cues: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    cursor = 0.0

    for item in items:
        timing_value = item.get("timing")
        if timing_value in (None, ""):
            timing_value = screenplay_lookup.get("timings", {}).get((item.get("scene_number"), item.get("shot_number")))
        timing = _parse_timing_range(timing_value, cursor)
        if timing is None:
            skipped.append(_skip_record(item, "timing_unavailable"))
            continue

        start_seconds, end_seconds, timing_mode = timing
        text, source = _resolve_cue_text(item, screenplay_lookup)
        if not text:
            skipped.append(_skip_record(item, "text_unavailable"))
            cursor = max(cursor, end_seconds)
            continue

        cue = {
            "index": len(cues) + 1,
            "scene_number": item.get("scene_number"),
            "shot_number": item.get("shot_number"),
            "start_seconds": round(start_seconds, 3),
            "end_seconds": round(end_seconds, 3),
            "start_timecode": _format_srt_timestamp(start_seconds),
            "end_timecode": _format_srt_timestamp(end_seconds),
            "duration_seconds": round(end_seconds - start_seconds, 3),
            "text": text,
            "text_source": source,
            "timing": timing_value,
            "timing_mode": timing_mode,
        }
        cues.append(cue)
        cursor = max(cursor, end_seconds)

    return cues, skipped


def _skip_record(item: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "scene_number": item.get("scene_number"),
        "shot_number": item.get("shot_number"),
        "reason": reason,
    }


def _resolve_cue_text(
    item: Dict[str, Any],
    screenplay_lookup: Dict[str, Dict[Tuple[Any, Any], Any]],
) -> Tuple[str, str]:
    key = (item.get("scene_number"), item.get("shot_number"))
    shot_text = screenplay_lookup.get("shots", {}).get(key)
    if shot_text:
        return shot_text, "screenplay.storyboard"

    scene_text = screenplay_lookup.get("scenes", {}).get((item.get("scene_number"), None))
    if scene_text:
        return scene_text, "screenplay.scene"

    item_text = _first_text(
        item,
        [
            "subtitle",
            "subtitle_text",
            "caption",
            "voiceover",
            "narration",
            "dialogue",
            "description",
            "shot_description",
            "initial_state_summary",
            "video_prompt",
        ],
    )
    if item_text:
        return item_text, "shots.item"
    return "", ""


def _first_text(payload: Dict[str, Any], fields: List[str]) -> str:
    for field in fields:
        text = _stringify_text(payload.get(field))
        if text:
            return text
    return ""


def _stringify_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, (int, float)):
        return _clean_text(str(value))
    if isinstance(value, list):
        parts = [_stringify_text(item) for item in value]
        return _clean_text(" ".join(part for part in parts if part))
    if isinstance(value, dict):
        text = _first_text(value, ["text", "line", "dialogue", "caption", "description", "content"])
        character = _clean_text(str(value.get("character") or value.get("speaker") or ""))
        if character and text:
            return f"{character}: {text}"
        return text
    return ""


def _clean_text(value: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]+", " ", str(value))
    return re.sub(r"\s+", " ", value).strip()


def _parse_timing_range(value: Any, cursor: float = 0.0) -> Optional[Tuple[float, float, str]]:
    if isinstance(value, dict):
        start = _parse_time_value(value.get("start"))
        end = _parse_time_value(value.get("end"))
        if start is not None and end is not None and end > start:
            return start, end, "absolute"
        duration = _parse_duration_value(value.get("duration"))
        if duration is not None:
            return cursor, cursor + duration, "duration"
        return None

    if isinstance(value, (int, float)):
        duration = float(value)
        if duration > 0:
            return cursor, cursor + duration, "duration"
        return None

    text = str(value or "").strip()
    if not text:
        return None

    if "-" in text:
        left, right = re.split(r"\s*-\s*", text, maxsplit=1)
        start = _parse_time_value(left)
        end = _parse_time_value(right)
        if start is not None and end is not None and end > start:
            return start, end, "absolute"
        return None

    duration = _parse_duration_value(text)
    if duration is not None:
        return cursor, cursor + duration, "duration"
    return None


def _parse_duration_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().lower().replace(",", ".")
    if text.endswith("sec"):
        text = text[:-3].strip()
    elif text.endswith("s"):
        text = text[:-1].strip()
    if ":" in text:
        return _parse_time_value(text)
    try:
        duration = float(text)
    except ValueError:
        return None
    return duration if duration > 0 else None


def _parse_time_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    if text.endswith("s") and ":" not in text:
        return _parse_duration_value(text)

    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
        return float(text)
    except ValueError:
        return None


def _format_srt(cues: List[Dict[str, Any]]) -> str:
    blocks = []
    for cue in cues:
        blocks.append(
            "\n".join(
                [
                    str(cue["index"]),
                    f"{cue['start_timecode']} --> {cue['end_timecode']}",
                    cue["text"],
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _format_srt_timestamp(seconds: float) -> str:
    milliseconds_total = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds_total, 3600 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _safe_project_dir(project_id: str) -> Path:
    value = str(project_id or "").strip()
    if not value:
        raise ValueError("project_id is required")

    root = (Path("plots") / "storybooks").resolve()
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"project_id escapes storybook root: {project_id}") from exc
    if candidate == root:
        raise ValueError("project_id must identify a project directory")
    return candidate


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
