"""Generate scene sound effects through the configured LLM Gateway."""

import hashlib
import json
import os

import requests

from custom_tools.storybook.audio_subtitle import _as_bool, _read_json, _safe_project_dir, _write_json
from custom_tools.storybook.music_generator import _load_env_file


MODEL = "llmgateway/small-sfx"


def storybook_sfx_generator_tool(
    session_id: str,
    project_id: str,
    language: str = "ru",
    enable: bool = True,
) -> dict:
    """Generate scene sound effects and register their audio files.

    Args:
        session_id: Execution trace identifier.
        project_id: Storybook project identifier.
        language: Project source language code.
        enable: Whether to generate sound effects.

    Returns:
        Generation status, manifest path and track count.
    """
    try:
        base = _safe_project_dir(project_id)
    except ValueError as exc:
        return {"status": "error", "error": str(exc), "sfx_manifest_path": ""}
    audio_dir = base / "98_audio"
    manifest_path = audio_dir / "sfx_manifest.json"
    if not _as_bool(enable):
        return {"status": "skipped", "sfx_manifest_path": str(manifest_path)}

    tracks = []
    errors = []
    try:
        screenplay = _read_json(base / "91_screenplay" / "screenplay.json")
        scenes = screenplay.get("screenplay", []) if isinstance(screenplay, dict) else screenplay
        _load_env_file()
        for scene in scenes:
            effect = scene.get("sfx")
            if not effect:
                continue
            if isinstance(effect, str):
                effect = {"prompt": effect}
            prompt = str(effect.get("prompt") or "").strip()
            if not prompt:
                continue
            scene_number = scene["scene_number"]
            payload = {"model": MODEL, "input": prompt, "response_format": "mp3"}
            if effect.get("duration") is not None:
                duration = float(effect["duration"])
                if not 0 < duration < float("inf"):
                    raise ValueError(f"SFX duration must be positive (scene {scene_number})")
                payload["duration"] = duration
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
            path = audio_dir / "sfx" / f"{fingerprint}.mp3"
            if not path.is_file() or not path.stat().st_size:
                _generate_mp3(payload, path)
            track = {
                "role": "sfx", "scene_number": scene_number,
                "path": str(path.relative_to(audio_dir)), "model": MODEL, "prompt": prompt,
            }
            if "duration" in payload:
                track["duration"] = payload["duration"]
            tracks.append(track)
    except Exception as exc:
        errors.append(str(exc))

    status = "error" if errors else "success"
    manifest = {
        "session_id": session_id, "project_id": project_id, "language": language,
        "status": status, "model": MODEL, "audio_tracks": tracks, "errors": errors,
    }
    _write_json(manifest_path, manifest)
    audio_manifest_path = audio_dir / "audio_manifest.json"
    audio_manifest = _read_json(audio_manifest_path) if audio_manifest_path.exists() else {}
    previous = audio_manifest.get("audio_tracks", [])
    audio_manifest["audio_tracks"] = [t for t in previous if not isinstance(t, dict) or t.get("role") != "sfx"] + tracks
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
