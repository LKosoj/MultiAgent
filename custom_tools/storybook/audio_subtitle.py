import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from custom_tools.storybook.project_paths import safe_storybook_project_dir


def storybook_audio_subtitle_tool(
    session_id: str,
    project_id: str,
    language: str = "ru",
    enable: bool = True,
    generate_subtitles: bool = True,
    series_id: str = "",
) -> Dict[str, Any]:
    """Write subtitles and register audio from the series' measured speech.

    Args:
        session_id: Execution trace identifier.
        project_id: Project identifier.
        language: Language code.
        enable: Whether to prepare subtitles and audio.
        generate_subtitles: Whether to write subtitles.
        series_id: Series identifier.
    """
    from custom_tools.storybook.studio.series import series_dir
    try:
        base = series_dir(project_id, series_id)
    except Exception as exc:
        return {"status": "error", "error": str(exc), "subtitles_path": "", "audio_manifest_path": "", "cue_sheet_path": ""}
    audio_dir = base / "98_audio"
    subtitles = audio_dir / "subtitles.srt"
    cue_path = audio_dir / "cue_sheet.json"
    manifest_path = audio_dir / "audio_manifest.json"
    if not _as_bool(enable):
        return {"status": "skipped", "subtitles_path": str(subtitles),
                "cue_sheet_path": str(cue_path), "audio_manifest_path": str(manifest_path)}
    try:
        speech = _read_json(audio_dir / "speech.json")
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc), "subtitles_path": str(subtitles),
                "audio_manifest_path": str(manifest_path), "cue_sheet_path": str(cue_path)}
    if speech.get("status") not in ("success", "silent"):
        return {"status": "error", "error": "Речь ещё не измерена", "subtitles_path": str(subtitles),
                "audio_manifest_path": str(manifest_path), "cue_sheet_path": str(cue_path)}
    cues = [{"index": i, "scene_number": line.get("scene_number"), "shot_number": line.get("shot_number"),
             "text": line["text"], "start": line["start"], "end": line["end"],
             "start_timecode": _format_srt_timestamp(line["start"]),
             "end_timecode": _format_srt_timestamp(line["end"])}
            for i, line in enumerate(speech["lines"], 1)]
    if _as_bool(generate_subtitles):
        _atomic_write_text(subtitles, _format_srt(cues))
    _write_json(cue_path, {"timeline": "measured", "cues": cues, "cue_count": len(cues)})
    manifest = _read_json(manifest_path) if manifest_path.exists() else {}
    tracks = [track for track in manifest.get("audio_tracks", []) if track.get("role") != "narration"]
    if speech.get("audio_path"):
        tracks.insert(0, {"role": "narration", "path": speech["audio_path"], "offset_seconds": 0,
                          "duration_seconds": speech["duration"]})
    manifest.update(audio_tracks=tracks, tts_status=speech["status"], subtitles_path=str(subtitles),
                    cue_sheet_path=str(cue_path), subtitles_enabled=_as_bool(generate_subtitles))
    _write_json(manifest_path, manifest)
    return {"status": "success", "speech_path": str(audio_dir / "speech.json"),
            "subtitles_path": str(subtitles), "cue_sheet_path": str(cue_path),
            "audio_manifest_path": str(manifest_path), "cue_count": len(cues)}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


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
        if "voiceover" in scene:
            scene_value = _stringify_text(scene["voiceover"])
        if scene_value or "voiceover" in scene:
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
            if "voiceover" in shot:
                text = _stringify_text(shot["voiceover"])
            if text or "voiceover" in shot:
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
    shot_texts = screenplay_lookup.get("shots", {})
    if key in shot_texts:
        return shot_texts[key], "screenplay.storyboard"

    scene_key = (item.get("scene_number"), None)
    scene_texts = screenplay_lookup.get("scenes", {})
    if scene_key in scene_texts:
        return scene_texts[scene_key], "screenplay.scene"

    # Явный текст для зрителя главнее voiceover; пустое явное поле = «нет поля».
    viewer_text = _first_text(item, ["subtitle", "subtitle_text", "caption"])
    if viewer_text:
        return viewer_text, "shots.item"

    # Пустая речь не должна подменяться описанием кадра.
    if "voiceover" in item:
        return _stringify_text(item["voiceover"]), "shots.item"

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
    return safe_storybook_project_dir(project_id)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
