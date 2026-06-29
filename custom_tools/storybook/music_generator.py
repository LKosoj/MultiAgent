import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from custom_tools.storybook.audio_subtitle import _safe_project_dir


STUDIO_API_BASE_URL = "https://studio-api.prod.suno.com"
CLERK_BASE_URL = "https://auth.suno.com"
CLERK_API_VERSION = "2025-11-10"
CLERK_JS_VERSION = "5.117.0"
DEFAULT_MODEL = "chirp-fenix"  # Suno v5.5 (latest as of 2026-06); override via SUNO_MODEL
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


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
        instrumental: Request instrumental music from Suno.
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
    cookie_raw = _env("SUNO_COOKIE")
    if not cookie_raw:
        payload = _manifest_payload(
            session_id=session_id,
            project_id=project_id,
            language=language,
            provider=provider,
            status="skipped",
            message="SUNO_COOKIE is not configured",
            music_path=music_path,
        )
        _write_json(manifest_path, payload)
        _merge_music_status(audio_manifest_path, payload, track=None)
        return _tool_result(payload, manifest_path, music_path)

    music_prompt = _clip_prompt(prompt or _build_music_prompt(base_dir, language))
    task_id: Optional[str] = None
    try:
        auth = _authenticate(cookie_raw)

        captcha_required = _captcha_required(auth)
        manual_token = _env("SUNO_HCAPTCHA_TOKEN") or None
        if captcha_required and not manual_token:
            message = "Suno requires a captcha for this request; set SUNO_HCAPTCHA_TOKEN to provide one"
            payload = _manifest_payload(
                session_id=session_id,
                project_id=project_id,
                language=language,
                provider=provider,
                status="skipped",
                message=message,
                music_path=music_path,
                prompt=music_prompt,
            )
            _write_json(manifest_path, payload)
            _merge_music_status(audio_manifest_path, payload, track=None)
            return _tool_result(payload, manifest_path, music_path)
        token = manual_token if captcha_required else None

        request_payload = _build_suno_payload(music_prompt, instrumental, base_dir, token)
        submit_response = _post_suno_generate(auth, request_payload)
        clip_ids = _extract_clip_ids(submit_response)
        if not clip_ids:
            message = "Suno response did not include any clip ids"
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
            task_id = clip_ids[0]
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

        record_payload, audio_url, task_id = _wait_for_suno_audio_url(
            auth=auth,
            clip_ids=clip_ids,
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
            prompt=music_prompt,
            task_id=task_id,
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


def _build_suno_payload(prompt: str, instrumental: bool, base_dir: Path, token: Optional[str]) -> Dict[str, Any]:
    custom_mode = _truthy_env("SUNO_CUSTOM_MODE")
    payload: Dict[str, Any] = {
        "make_instrumental": instrumental,
        "mv": _env("SUNO_MODEL", DEFAULT_MODEL),
        "prompt": "",
        "generation_type": "TEXT",
        "token": token,
    }
    if custom_mode:
        payload["tags"] = _clip_text(_env("SUNO_MUSIC_STYLE") or _derive_style(base_dir), 200)
        payload["title"] = _clip_text(_derive_title(base_dir), 80)
        payload["prompt"] = prompt
    else:
        payload["gpt_description_prompt"] = prompt
    return payload


def _authenticate(cookie_raw: str) -> Dict[str, Any]:
    cookies = _parse_cookies(cookie_raw)
    client_token = cookies.get("__client")
    if not client_token:
        raise RuntimeError("SUNO_COOKIE is missing the __client value")
    device_id = cookies.get("ajs_anonymous_id") or str(uuid.uuid4())
    auth = {"cookies": cookies, "client_token": client_token, "device_id": device_id}

    url = _url(CLERK_BASE_URL, "/v1/client") + _clerk_query()
    response = requests.get(url, headers=_clerk_headers(auth), timeout=_request_timeout())
    data = _json_response(response)
    inner = data.get("response")
    sid = inner.get("last_active_session_id") if isinstance(inner, dict) else None
    if not sid:
        raise RuntimeError("Suno auth did not return an active session id")
    auth["sid"] = str(sid)
    return auth


def _mint_jwt(auth: Dict[str, Any]) -> str:
    url = _url(CLERK_BASE_URL, f"/v1/client/sessions/{auth['sid']}/tokens") + _clerk_query()
    response = requests.post(url, headers=_clerk_headers(auth), timeout=_request_timeout())
    data = _json_response(response)
    jwt = data.get("jwt")
    if not jwt:
        raise RuntimeError("Suno auth did not return a session token")
    return str(jwt)


def _captcha_required(auth: Dict[str, Any]) -> bool:
    url = _url(STUDIO_API_BASE_URL, "/api/c/check")
    response = requests.post(
        url, headers=_studio_headers(auth), json={"ctype": "generation"}, timeout=_request_timeout()
    )
    data = _json_response(response)
    return bool(data.get("required"))


def _post_suno_generate(auth: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    url = _url(STUDIO_API_BASE_URL, "/api/generate/v2/")
    response = requests.post(url, headers=_studio_headers(auth), json=payload, timeout=_request_timeout())
    return _json_response(response)


def _get_suno_feed(auth: Dict[str, Any], clip_ids: List[str]) -> Dict[str, Any]:
    url = _url(STUDIO_API_BASE_URL, "/api/feed/v2")
    response = requests.get(
        url, headers=_studio_headers(auth), params={"ids": ",".join(clip_ids)}, timeout=_request_timeout()
    )
    return _json_response(response)


def _wait_for_suno_audio_url(
    auth: Dict[str, Any],
    clip_ids: List[str],
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> Tuple[Dict[str, Any], Optional[str], Optional[str]]:
    deadline = time.monotonic() + max(1, int(timeout_seconds or 1))
    poll_interval = max(1, int(poll_interval_seconds or 1))
    last_payload: Dict[str, Any] = {}

    while True:
        last_payload = _get_suno_feed(auth, clip_ids)
        clips = _feed_clips(last_payload)
        all_failed = bool(clips)
        for clip in clips:
            status = str(clip.get("status") or "").strip().lower()
            audio_url = clip.get("audio_url")
            if status == "complete" and isinstance(audio_url, str) and audio_url.startswith(("http://", "https://")):
                return last_payload, audio_url, str(clip.get("id") or "") or None
            if status != "error":
                all_failed = False
        if all_failed:
            raise RuntimeError("Suno task failed for all clips")
        if time.monotonic() >= deadline:
            raise TimeoutError("Suno task timed out before audio was ready")
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
        raise RuntimeError(f"Suno response is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Suno response must be a JSON object")
    return payload


def _parse_cookies(raw: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in str(raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            cookies[key] = value.strip()
    return cookies


def _cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def _clerk_query() -> str:
    return f"?__clerk_api_version={CLERK_API_VERSION}&_clerk_js_version={CLERK_JS_VERSION}"


def _static_headers(device_id: str) -> Dict[str, str]:
    return {
        "Affiliate-Id": "undefined",
        "Device-Id": f'"{device_id}"',
        "x-suno-client": "Android prerelease-4nt180t 1.0.42",
        "X-Requested-With": "com.suno.android",
        "User-Agent": _USER_AGENT,
    }


def _clerk_headers(auth: Dict[str, Any]) -> Dict[str, str]:
    headers = _static_headers(auth["device_id"])
    headers["Authorization"] = auth["client_token"]
    headers["Cookie"] = _cookie_header(auth["cookies"])
    return headers


def _studio_headers(auth: Dict[str, Any]) -> Dict[str, str]:
    jwt = _mint_jwt(auth)
    headers = _static_headers(auth["device_id"])
    headers["Authorization"] = f"Bearer {jwt}"
    headers["Cookie"] = _cookie_header(auth["cookies"])
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json"
    return headers


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


def _extract_clip_ids(payload: Dict[str, Any]) -> List[str]:
    clips = _feed_clips(payload)
    ids: List[str] = []
    for clip in clips:
        value = clip.get("id")
        if value not in (None, ""):
            ids.append(str(value))
    return ids


def _feed_clips(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    clips = payload.get("clips")
    if not isinstance(clips, list):
        data = payload.get("data")
        if isinstance(data, dict):
            clips = data.get("clips")
    if not isinstance(clips, list):
        return []
    return [clip for clip in clips if isinstance(clip, dict)]


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
