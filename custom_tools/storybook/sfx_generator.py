"""Generate scene sound effects through the configured LLM Gateway."""

import hashlib
import json
import os

import requests

from custom_tools.storybook.audio_subtitle import _as_bool, _read_json, _write_json
from custom_tools.storybook.music_generator import _load_env_file


MODEL = "llmgateway/small-sfx"


def _event_start(effect, scene_number, speech, shots):
    """Translate a line/word reference to seconds from the measured speech."""
    line_id = effect.get("line_id")
    if line_id:
        lines = [line for line in speech.get("lines", []) if line.get("id") == line_id and line.get("scene_number") == scene_number]
        if len(lines) != 1:
            raise ValueError(f"Реплика звукового события не найдена: {line_id}")
        line = lines[0]
        if "word_index" in effect:
            index = effect["word_index"]
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < line["word_end"] - line["word_start"]:
                raise ValueError(f"Неверный номер слова звукового события: {line_id}")
            anchor = speech["words"][line["word_start"] + index]["start"]
        else:
            anchor = line["end"] if effect.get("anchor") == "end" else line["start"]
        return float(anchor) + float(effect.get("offset", 0))
    if "start" in effect:
        return float(effect["start"])
    scene_shots = [shot for shot in shots if shot.get("scene_number") == scene_number and
                   "start_s" in shot and (effect.get("shot_number") is None or shot.get("shot_number") == effect["shot_number"])]
    if scene_shots:
        return min(float(shot["start_s"]) for shot in scene_shots)
    lines = [line for line in speech.get("lines", []) if line.get("scene_number") == scene_number]
    if lines:
        return float(lines[0]["start"])
    raise ValueError(f"Для звука сцены {scene_number} нужна привязка по времени")


def storybook_sfx_generator_tool(
    session_id: str,
    project_id: str,
    language: str = "ru",
    enable: bool = True,
    series_id: str = "",
) -> dict:
    """Generate scene sound effects and register their audio files.

    Args:
        session_id: Execution trace identifier.
        project_id: Storybook project identifier.
        language: Project source language code.
        enable: Whether to generate sound effects.
        series_id: Identifier of the series.

    Returns:
        Generation status, manifest path and track count.
    """
    try:
        from custom_tools.storybook.studio.series import series_dir
        base = series_dir(project_id, series_id)
    except Exception as exc:
        return {"status": "error", "error": str(exc), "sfx_manifest_path": ""}
    audio_dir = base / "98_audio"
    manifest_path = audio_dir / "sfx_manifest.json"
    if not _as_bool(enable):
        return {"status": "skipped", "sfx_manifest_path": str(manifest_path)}

    tracks = []
    errors = []
    event_ids = set()
    try:
        screenplay = _read_json(base / "91_screenplay" / "screenplay.json")
        speech_path = audio_dir / "speech.json"
        speech = _read_json(speech_path)
        if speech.get("status") not in ("success", "silent"):
            raise ValueError("Речь ещё не измерена")
        shots_path = base / "97_shots" / "shots.json"
        shots_data = _read_json(shots_path) if shots_path.exists() else {}
        shots = shots_data.get("items", []) if isinstance(shots_data, dict) else []
        scenes = screenplay.get("screenplay", []) if isinstance(screenplay, dict) else screenplay
        _load_env_file()
        for scene in scenes:
            effects = scene.get("sfx")
            if not effects:
                continue
            if not isinstance(effects, list):
                effects = [effects]
            for index, effect in enumerate(effects):
                if isinstance(effect, str):
                    effect = {"prompt": effect}
                prompt = str(effect.get("prompt") or "").strip()
                if not prompt:
                    continue
                scene_number = scene["scene_number"]
                event_id = effect.get("id", f"{scene_number}-{index + 1}")
                if event_id in event_ids:
                    raise ValueError(f"Повторный идентификатор звука: {event_id}")
                event_ids.add(event_id)
                payload = {"model": MODEL, "input": prompt, "response_format": "mp3"}
                if effect.get("duration") is not None:
                    duration = float(effect["duration"])
                    if not 0 < duration < float("inf"):
                        raise ValueError(f"SFX duration must be positive (scene {scene_number})")
                    payload["duration"] = duration
                level = float(effect.get("level", 1))
                start = _event_start(effect, scene_number, speech, shots)
                if not 0 <= level <= 1 or not 0 <= start < float("inf"):
                    raise ValueError(f"Некорректное время или громкость SFX (сцена {scene_number})")
                bounds = [shot for shot in shots if shot.get("scene_number") == scene_number and "start_s" in shot and "end_s" in shot]
                if bounds:
                    lower = min(float(shot["start_s"]) for shot in bounds)
                    upper = max(float(shot["end_s"]) for shot in bounds)
                    if start < lower or start >= upper or ("duration" in payload and start + payload["duration"] > upper):
                        raise ValueError(f"Звуковое событие выходит за пределы сцены {scene_number}")
                fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
                path = audio_dir / "sfx" / f"{fingerprint}.mp3"
                if not path.is_file() or not path.stat().st_size:
                    _generate_mp3(payload, path)
                track = {"role": "sfx", "scene_number": scene_number, "id": event_id,
                         "path": str(path.relative_to(audio_dir)), "model": MODEL, "prompt": prompt,
                         "offset_seconds": start, "level": level, "enabled": effect.get("enabled", True),
                         "speech_hash": speech.get("input_hash")}
                for key in ("action_id", "kind", "line_id", "word_index", "anchor", "shot_number"):
                    if key in effect:
                        track[key] = effect[key]
                if "duration" in payload:
                    track["duration"] = payload["duration"]
                    track["duration_seconds"] = payload["duration"]
                tracks.append(track)
    except Exception as exc:
        errors.append(str(exc))

    status = "error" if errors else "success"
    manifest = {
        "session_id": session_id, "project_id": project_id, "language": language,
        "status": status, "model": MODEL, "audio_tracks": tracks, "errors": errors,
    }
    audio_manifest_path = audio_dir / "audio_manifest.json"
    audio_manifest = _read_json(audio_manifest_path) if audio_manifest_path.exists() else {}
    previous = audio_manifest.get("audio_tracks", [])
    edits = {t.get("id"): t for t in previous if isinstance(t, dict) and t.get("role") == "sfx" and t.get("edited")}
    for track in tracks:
        old = edits.get(track.get("id"))
        if old and old.get("path") == track.get("path") and old.get("speech_hash") == track.get("speech_hash"):
            for field in ("offset_seconds", "level", "enabled", "edited"):
                if field in old:
                    track[field] = old[field]
    if errors:
        if manifest_path.exists():
            old_manifest = _read_json(manifest_path)
            manifest["audio_tracks"] = old_manifest.get("audio_tracks", [])
        else:
            manifest["audio_tracks"] = [t for t in previous if isinstance(t, dict) and t.get("role") == "sfx"]
        audio_manifest["audio_tracks"] = previous
    else:
        audio_manifest["audio_tracks"] = [t for t in previous if not isinstance(t, dict) or t.get("role") != "sfx"] + tracks
    _write_json(manifest_path, manifest)
    audio_manifest["sfx_status"] = status
    audio_manifest["sfx_errors"] = errors
    _write_json(audio_manifest_path, audio_manifest)
    result = {"status": status, "sfx_manifest_path": str(manifest_path), "track_count": len(tracks)}
    if errors:
        result["error"] = "; ".join(errors)
    return result


def _generate_mp3(payload, path):
    base_url = os.getenv("OPENAI_API_BASE_DB", "").rstrip("/")
    api_key = os.getenv("OPENAI_API_KEY_DB", "")
    if not base_url or not api_key:
        raise RuntimeError("SFX requires OPENAI_API_BASE_DB and OPENAI_API_KEY_DB")
    response = requests.post(
        f"{base_url}/audio/speech", json=payload,
        headers={"Authorization": f"Bearer {api_key}", "X-Title": "MAgent"}, timeout=1200,
    )
    response.raise_for_status()
    if not response.content or not response.headers.get("Content-Type", "").lower().startswith("audio/"):
        raise RuntimeError("SFX gateway did not return an audio file")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(response.content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
