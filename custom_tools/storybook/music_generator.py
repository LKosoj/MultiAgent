import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from custom_tools.storybook.audio_subtitle import _safe_project_dir


DEFAULT_SUNO_BASE_URL = "https://api.sunoapi.org"
DEFAULT_GENERATE_ENDPOINT = "/api/v1/generate"
DEFAULT_RECORD_INFO_ENDPOINT = "/api/v1/generate/record-info"


def storybook_music_generator_tool(
    session_id: str,
    project_id: str,
    language: str = "ru",
    enable: bool = True,
    provider: str = "suno",
    prompt: Optional[str] = None,
    instrumental: bool = True,
    wait_for_completion: bool = True,
    poll_interval_seconds: int = 10,
    timeout_seconds: int = 600,
    force_regenerate: bool = False,
) -> Dict[str, Any]:
    """Generate or reuse a background music track and register it in audio_manifest.json.

    Args:
        session_id: Execution session identifier used in generated artifacts.
        project_id: Storybook project identifier under plots/storybooks.
        language: Language code stored in generated metadata.
        enable: If false, skip music generation and write a skipped manifest.
        provider: Music provider name. Currently only "suno" is implemented.
        prompt: Optional explicit music prompt. If empty, it is derived from screenplay/brief data.
        instrumental: Request instrumental music from Suno-compatible providers.
        wait_for_completion: If true, poll until an audio URL is available and download it.
        poll_interval_seconds: Polling interval for async provider jobs.
        timeout_seconds: Max polling time for async provider jobs.
        force_regenerate: If false, reuse 98_audio/music.mp3 when it already exists.
    """
    enable = _as_bool(enable)
    instrumental = _as_bool(instrumental)
    wait_for_completion = _as_bool(wait_for_completion)
    force_regenerate = _as_bool(force_regenerate)
    provider = str(provider or "suno").strip().lower()

    try:
        base_dir = _safe_project_dir(project_id)
    except ValueError as exc:
        return _error_result(str(exc))

    audio_dir = base_dir / "98_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    music_path = audio_dir / "music.mp3"
    manifest_path = audio_dir / "music_manifest.json"
    audio_manifest_path = audio_dir / "audio_manifest.json"

    if provider != "suno":
        payload = _manifest_payload(
            session_id=session_id,
            project_id=project_id,
            language=language,
            provider=provider,
            status="error",
            message=f"Unsupported music provider: {provider}",
            music_path=music_path,
        )
        _write_json(manifest_path, payload)
        _merge_music_status(audio_manifest_path, payload, track=None)
        return _error_result(payload["message"], manifest_path=manifest_path, music_path=music_path)

    if not enable:
        payload = _manifest_payload(
            session_id=session_id,
            project_id=project_id,
            language=language,
            provider=provider,
            status="skipped",
            message="Music generation disabled",
            music_path=music_path,
        )
        _write_json(manifest_path, payload)
        _merge_music_status(audio_manifest_path, payload, track=None)
        return _tool_result(payload, manifest_path, music_path)

    if music_path.exists() and not force_regenerate:
        track = _music_track(music_path, provider, task_id=None, reused=True)
        payload = _manifest_payload(
            session_id=session_id,
            project_id=project_id,
            language=language,
            provider=provider,
            status="success",
            message="Reused existing music track",
            music_path=music_path,
            track=track,
            reused_existing=True,
        )
        _write_json(manifest_path, payload)
        _merge_music_status(audio_manifest_path, payload, track=track)
        return _tool_result(payload, manifest_path, music_path)

    _load_env_file()
    api_key = _env("SUNO_API_KEY")
    if not api_key:
        payload = _manifest_payload(
            session_id=session_id,
            project_id=project_id,
            language=language,
            provider=provider,
            status="skipped",
            message="SUNO_API_KEY is not configured",
            music_path=music_path,
        )
        _write_json(manifest_path, payload)
        _merge_music_status(audio_manifest_path, payload, track=None)
        return _tool_result(payload, manifest_path, music_path)

    music_prompt = _clip_prompt(prompt or _build_music_prompt(base_dir, language))
    request_payload = _build_suno_payload(music_prompt, instrumental, base_dir)
    try:
        submit_response = _post_suno_generate(api_key, request_payload)
        task_id = _extract_task_id(submit_response)
        if not task_id:
            message = "Suno response did not include taskId"
            payload = _manifest_payload(
                session_id=session_id,
                project_id=project_id,
                language=language,
                provider=provider,
                status="error",
                message=message,
                music_path=music_path,
                prompt=music_prompt,
                provider_response=submit_response,
            )
            _write_json(manifest_path, payload)
            _merge_music_status(audio_manifest_path, payload, track=None)
            return _error_result(message, manifest_path=manifest_path, music_path=music_path)

        if not wait_for_completion:
            payload = _manifest_payload(
                session_id=session_id,
                project_id=project_id,
                language=language,
                provider=provider,
                status="submitted",
                message="Suno music generation submitted",
                music_path=music_path,
                prompt=music_prompt,
                task_id=task_id,
                provider_response=submit_response,
            )
            _write_json(manifest_path, payload)
            _merge_music_status(audio_manifest_path, payload, track=None)
            return _tool_result(payload, manifest_path, music_path)

        record_payload, audio_url = _wait_for_suno_audio_url(
            api_key=api_key,
            task_id=task_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if not audio_url:
            message = "Suno task completed without an audio URL"
            payload = _manifest_payload(
                session_id=session_id,
                project_id=project_id,
                language=language,
                provider=provider,
                status="error",
                message=message,
                music_path=music_path,
                prompt=music_prompt,
                task_id=task_id,
                provider_response=record_payload,
            )
            _write_json(manifest_path, payload)
            _merge_music_status(audio_manifest_path, payload, track=None)
            return _error_result(message, manifest_path=manifest_path, music_path=music_path)

        _download_audio(audio_url, music_path)
    except Exception as exc:
        message = f"Suno music generation failed: {exc}"
        payload = _manifest_payload(
            session_id=session_id,
            project_id=project_id,
            language=language,
            provider=provider,
            status="error",
            message=message,
            music_path=music_path,
            prompt=music_prompt if "music_prompt" in locals() else None,
            task_id=task_id if "task_id" in locals() else None,
        )
        _write_json(manifest_path, payload)
        _merge_music_status(audio_manifest_path, payload, track=None)
        return _error_result(message, manifest_path=manifest_path, music_path=music_path)

    track = _music_track(music_path, provider, task_id=task_id, reused=False)
    payload = _manifest_payload(
        session_id=session_id,
        project_id=project_id,
        language=language,
        provider=provider,
        status="success",
        message="Generated music track",
        music_path=music_path,
        prompt=music_prompt,
        task_id=task_id,
        track=track,
        provider_response=record_payload,
    )
    _write_json(manifest_path, payload)
    _merge_music_status(audio_manifest_path, payload, track=track)
    return _tool_result(payload, manifest_path, music_path)


def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    with env_path.open("r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


def _build_suno_payload(prompt: str, instrumental: bool, base_dir: Path) -> Dict[str, Any]:
    custom_mode = _truthy_env("SUNO_CUSTOM_MODE")
    payload: Dict[str, Any] = {
        "prompt": prompt,
        "customMode": custom_mode,
        "instrumental": instrumental,
        "model": _env("SUNO_MODEL", "V4_5"),
    }
    callback_url = _env("SUNO_CALLBACK_URL")
    if callback_url:
        payload["callBackUrl"] = callback_url
    if custom_mode:
        payload["style"] = _clip_text(_env("SUNO_MUSIC_STYLE") or _derive_style(base_dir), 200)
        payload["title"] = _clip_text(_derive_title(base_dir), 80)
    return payload


def _post_suno_generate(api_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = _url(_env("SUNO_API_BASE_URL", DEFAULT_SUNO_BASE_URL), _env("SUNO_GENERATE_ENDPOINT", DEFAULT_GENERATE_ENDPOINT))
    response = requests.post(url, headers=_headers(api_key), json=payload, timeout=_request_timeout())
    return _json_response(response)


def _get_suno_record_info(api_key: str, task_id: str) -> Dict[str, Any]:
    url = _url(
        _env("SUNO_API_BASE_URL", DEFAULT_SUNO_BASE_URL),
        _env("SUNO_RECORD_INFO_ENDPOINT", DEFAULT_RECORD_INFO_ENDPOINT),
    )
    response = requests.get(url, headers=_headers(api_key), params={"taskId": task_id}, timeout=_request_timeout())
    return _json_response(response)


def _wait_for_suno_audio_url(
    api_key: str,
    task_id: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> Tuple[Dict[str, Any], Optional[str]]:
    deadline = time.monotonic() + max(1, int(timeout_seconds or 1))
    poll_interval = max(1, int(poll_interval_seconds or 1))
    last_payload: Dict[str, Any] = {}
    failure_statuses = {"CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED", "SENSITIVE_WORD_ERROR", "CALLBACK_EXCEPTION", "FAILED", "ERROR"}

    while True:
        last_payload = _get_suno_record_info(api_key, task_id)
        status = _extract_status(last_payload)
        audio_url = _extract_audio_url(last_payload)
        if audio_url and status in {"SUCCESS", "FIRST_SUCCESS", "COMPLETE", "COMPLETED", "READY"}:
            return last_payload, audio_url
        if audio_url and not status:
            return last_payload, audio_url
        if status in failure_statuses:
            raise RuntimeError(f"Suno task failed with status {status}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Suno task timed out with status {status or 'unknown'}")
        time.sleep(poll_interval)


def _download_audio(audio_url: str, output_path: Path) -> None:
    response = requests.get(audio_url, timeout=max(30, _request_timeout()))
    if getattr(response, "status_code", 200) >= 400:
        raise RuntimeError(f"Audio download failed with HTTP {response.status_code}")
    tmp_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    with tmp_path.open("wb") as handle:
        if hasattr(response, "iter_content"):
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if chunk:
                    handle.write(chunk)
        else:
            handle.write(getattr(response, "content", b""))
    if tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded audio file is empty")
    os.replace(tmp_path, output_path)


def _json_response(response: Any) -> Dict[str, Any]:
    status_code = getattr(response, "status_code", 200)
    if status_code >= 400:
        text = getattr(response, "text", "")
        raise RuntimeError(f"HTTP {status_code}: {text[:300]}")
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"Provider response is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Provider response must be a JSON object")
    code = payload.get("code")
    if code not in (None, 0, 200, "0", "200"):
        message = payload.get("message") or payload.get("msg") or "provider returned non-success code"
        raise RuntimeError(str(message))
    return payload


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _build_music_prompt(base_dir: Path, language: str) -> str:
    screenplay = _read_json(base_dir / "91_screenplay" / "screenplay.json")
    brief = _read_json(base_dir / "00_brief.json")
    parts: List[str] = []

    concept = screenplay.get("concept") if isinstance(screenplay, dict) else None
    if isinstance(concept, dict):
        for key in ("music_concept", "music_sound", "tone", "mood"):
            if concept.get(key):
                parts.append(str(concept[key]))

    for scene in _screenplay_scenes(screenplay)[:8]:
        if isinstance(scene, dict) and scene.get("sound"):
            parts.append(str(scene["sound"]))

    if isinstance(brief, dict):
        for key in ("title", "task", "summary", "description"):
            if brief.get(key):
                parts.append(str(brief[key]))

    if not parts:
        parts.append("Warm instrumental cinematic storybook soundtrack for children, gentle orchestral textures, clear melody, no vocals.")

    language_note = "Instrumental background music, no vocals."
    return " ".join([*parts, language_note])


def _screenplay_scenes(screenplay: Any) -> List[Dict[str, Any]]:
    if isinstance(screenplay, dict) and isinstance(screenplay.get("screenplay"), list):
        return [scene for scene in screenplay["screenplay"] if isinstance(scene, dict)]
    if isinstance(screenplay, dict) and isinstance(screenplay.get("scenes"), list):
        return [scene for scene in screenplay["scenes"] if isinstance(scene, dict)]
    if isinstance(screenplay, list):
        return [scene for scene in screenplay if isinstance(scene, dict)]
    return []


def _derive_style(base_dir: Path) -> str:
    prompt = _build_music_prompt(base_dir, "en")
    return _clip_text(prompt, 200) or "cinematic storybook orchestral"


def _derive_title(base_dir: Path) -> str:
    brief = _read_json(base_dir / "00_brief.json")
    if isinstance(brief, dict) and brief.get("title"):
        return str(brief["title"])
    return "storybook soundtrack"


def _extract_task_id(payload: Dict[str, Any]) -> Optional[str]:
    values = [
        payload.get("taskId"),
        payload.get("task_id"),
        payload.get("id"),
    ]
    data = payload.get("data")
    if isinstance(data, dict):
        values.extend([data.get("taskId"), data.get("task_id"), data.get("id")])
    for value in values:
        if value not in (None, ""):
            return str(value)
    return None


def _extract_status(payload: Dict[str, Any]) -> str:
    candidates = [payload.get("status"), payload.get("state")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("status"), data.get("state")])
        response = data.get("response")
        if isinstance(response, dict):
            candidates.extend([response.get("status"), response.get("state")])
    for value in candidates:
        if value not in (None, ""):
            return str(value).strip().upper()
    return ""


def _extract_audio_url(payload: Dict[str, Any]) -> Optional[str]:
    for item in _walk_dicts(payload):
        for key in ("audioUrl", "audio_url", "audio", "sourceAudioUrl", "streamAudioUrl"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
    return None


def _walk_dicts(value: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for nested in value.values():
            found.extend(_walk_dicts(nested))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_dicts(item))
    return found


def _merge_music_status(audio_manifest_path: Path, music_manifest: Dict[str, Any], track: Optional[Dict[str, Any]]) -> None:
    audio_manifest = _read_json(audio_manifest_path)
    if not isinstance(audio_manifest, dict):
        audio_manifest = {
            "tts_status": "unavailable",
            "audio_tracks": [],
        }
    tracks = audio_manifest.get("audio_tracks")
    if not isinstance(tracks, list):
        tracks = []
    tracks = [
        existing
        for existing in tracks
        if not (isinstance(existing, dict) and existing.get("role") == "music" and existing.get("provider") == music_manifest.get("provider"))
    ]
    if track:
        tracks.append(track)
    audio_manifest["audio_tracks"] = tracks
    audio_manifest["music_status"] = music_manifest.get("status")
    audio_manifest["music_provider"] = music_manifest.get("provider")
    audio_manifest["music_manifest_path"] = music_manifest.get("manifest_path")
    if music_manifest.get("music_path"):
        audio_manifest["music_path"] = music_manifest.get("music_path")
    _write_json(audio_manifest_path, audio_manifest)


def _music_track(music_path: Path, provider: str, task_id: Optional[str], reused: bool) -> Dict[str, Any]:
    track: Dict[str, Any] = {
        "role": "music",
        "path": music_path.name,
        "provider": provider,
        "source": "storybook_music_generator_tool",
        "reused_existing": reused,
    }
    if task_id:
        track["task_id"] = task_id
    return track


def _manifest_payload(
    session_id: str,
    project_id: str,
    language: str,
    provider: str,
    status: str,
    message: str,
    music_path: Path,
    prompt: Optional[str] = None,
    task_id: Optional[str] = None,
    track: Optional[Dict[str, Any]] = None,
    reused_existing: bool = False,
    provider_response: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "session_id": session_id,
        "project_id": str(project_id),
        "language": language,
        "provider": provider,
        "status": status,
        "message": message,
        "manifest_path": str(music_path.parent / "music_manifest.json"),
        "music_path": str(music_path),
        "audio_tracks": [track] if track else [],
        "reused_existing": reused_existing,
    }
    if prompt:
        payload["prompt"] = prompt
    if task_id:
        payload["task_id"] = task_id
    if provider_response is not None:
        payload["provider_response"] = provider_response
    return payload


def _tool_result(payload: Dict[str, Any], manifest_path: Path, music_path: Path) -> Dict[str, Any]:
    status = str(payload.get("status") or "unknown")
    result = {
        "status": status,
        "message": payload.get("message", ""),
        "music_manifest_path": str(manifest_path),
        "music_path": str(music_path),
        "provider": payload.get("provider"),
        "task_id": payload.get("task_id"),
        "results": {
            "music_manifest_path": str(manifest_path),
            "music_path": str(music_path),
            "provider": payload.get("provider"),
            "task_id": payload.get("task_id"),
            "music_status": status,
        },
    }
    if status == "error":
        result["error"] = payload.get("message", "")
    return result


def _error_result(
    message: str,
    manifest_path: Optional[Path] = None,
    music_path: Optional[Path] = None,
) -> Dict[str, Any]:
    manifest_path_text = str(manifest_path) if manifest_path else ""
    music_path_text = str(music_path) if music_path else ""
    result = {
        "status": "error",
        "message": message,
        "error": message,
        "music_manifest_path": manifest_path_text,
        "music_path": music_path_text,
        "results": {
            "music_manifest_path": manifest_path_text,
            "music_path": music_path_text,
            "music_status": "error",
        },
    }
    return result


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _url(base_url: str, endpoint: str) -> str:
    return base_url.rstrip("/") + "/" + endpoint.lstrip("/")


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name) or default).strip()


def _truthy_env(name: str) -> bool:
    return _as_bool(os.getenv(name))


def _request_timeout() -> int:
    try:
        return max(5, int(_env("SUNO_REQUEST_TIMEOUT_SECONDS", "60")))
    except ValueError:
        return 60


def _clip_prompt(prompt: str) -> str:
    return _clip_text(prompt, 500)


def _clip_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
