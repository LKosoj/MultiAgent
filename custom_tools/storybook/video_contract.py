import json
import os
import shutil
from pathlib import Path


CONTRACT_VERSION = 1


def _load_env_file() -> None:
    """Load repo-level .env so preflight sees the same provider config as generators."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return

    with env_path.open("r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


_load_env_file()


def storybook_video_preflight_tool(
    session_id: str,
    project_id: str,
    language: str = "ru",
    enable: bool = True,
) -> dict:
    """Write a provider capability preflight summary for storybook video.

    Args:
        session_id: Execution session identifier used in generated artifacts.
        project_id: Storybook project identifier under plots/storybooks.
        language: Language code stored in generated metadata.
        enable: Whether video generation is requested for this run.
    """
    summary = _provider_summary(session_id, project_id, language, enable)
    _atomic_write_json(Path(summary["artifact_path"]), summary)
    return summary


def storybook_video_delivery_promise_tool(
    session_id: str,
    project_id: str,
    language: str = "ru",
    enable: bool = True,
) -> dict:
    """Write the explicit delivery promise for motion-led storybook video.

    Args:
        session_id: Execution session identifier used in generated artifacts.
        project_id: Storybook project identifier under plots/storybooks.
        language: Language code stored in generated metadata.
        enable: Whether video generation is requested for this run.
    """
    enabled = _as_bool(enable)
    summary = _provider_summary(session_id, project_id, language, enabled)
    expected_outputs = [item["video_path"] for item in summary["expected_video_items"]]

    blocking_reasons = []
    if enabled and summary["shots_error"]:
        blocking_reasons.append("shots_json_invalid")
    if enabled and not summary["capabilities"]["video"]:
        blocking_reasons.append("video_provider_unavailable")
    if enabled and not summary["capabilities"]["render"]:
        blocking_reasons.append("render_capability_unavailable")
    if enabled and not expected_outputs:
        blocking_reasons.append("no_expected_video_clips")

    artifact_path = str(_contract_dir(project_id) / "delivery_promise.json")
    will_generate = enabled and not blocking_reasons
    status = "success" if will_generate else "skipped"
    if enabled and blocking_reasons:
        status = "error"

    promise = {
        "version": CONTRACT_VERSION,
        "session_id": session_id,
        "project_id": project_id,
        "language": language,
        "status": status,
        "provider": summary["provider"],
        "generation_enabled": enabled,
        "capabilities": summary["capabilities"],
        "will_generate_video": will_generate,
        "expected_video_count": len(expected_outputs),
        "expected_outputs": expected_outputs,
        "blocking_reasons": blocking_reasons,
        "artifact_path": artifact_path,
        "provider_menu_artifact_path": summary["artifact_path"],
        "delivery_promise": _delivery_message(enabled, will_generate, blocking_reasons, len(expected_outputs)),
    }
    if status == "error":
        promise["error"] = promise["delivery_promise"]
    _atomic_write_json(Path(artifact_path), promise)
    return promise


def storybook_video_decision_log_tool(
    session_id: str,
    project_id: str,
    language: str = "ru",
    approved: bool = False,
) -> dict:
    """Write provider, delivery, and render decisions for storybook video.

    Args:
        session_id: Execution session identifier used in generated artifacts.
        project_id: Storybook project identifier under plots/storybooks.
        language: Language code stored in generated metadata.
        approved: Whether the recorded decisions were explicitly approved.
    """
    approved = _as_bool(approved)
    base = _project_dir(project_id)
    artifact_path = str(_contract_dir(project_id) / "decision_log.json")
    preflight = _read_json(_contract_dir(project_id) / "provider_menu_summary.json")
    if not preflight:
        preflight = _provider_summary(session_id, project_id, language, True)
    delivery = _read_json(_contract_dir(project_id) / "delivery_promise.json") or {}

    final_video_path = base / "99_final" / "final_video.mp4"
    final_video_status = "present" if final_video_path.exists() else "missing"
    decisions = _decision_entries(preflight, delivery, approved)
    payload = {
        "version": CONTRACT_VERSION,
        "session_id": session_id,
        "project_id": project_id,
        "language": language,
        "status": "success",
        "approved": approved,
        "decisions": decisions,
        "final_video_status": final_video_status,
        "artifact_path": artifact_path,
    }
    _atomic_write_json(Path(artifact_path), payload)
    return payload


def _provider_summary(session_id, project_id, language, enable):
    enabled = _as_bool(enable)
    shots_path, items, shots_error = _load_shots(project_id)
    provider, providers = _select_provider()
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    capabilities = {
        "image": _image_capability(),
        "video": provider != "none",
        "audio": _audio_capability(),
        "render": bool(ffmpeg_path and ffprobe_path),
    }
    expected_video_items = _expected_video_items(items)
    summary = {
        "version": CONTRACT_VERSION,
        "session_id": session_id,
        "project_id": project_id,
        "language": language,
        "status": "success",
        "provider": provider,
        "generation_enabled": enabled,
        "items_total": len(items),
        "expected_video_count": len(expected_video_items),
        "artifact_path": str(_contract_dir(project_id) / "provider_menu_summary.json"),
        "shots_path": str(shots_path),
        "shots_exists": shots_path.exists(),
        "shots_error": shots_error,
        "capabilities": capabilities,
        "capability_details": {
            "providers_considered": providers,
            "ffmpeg_path": ffmpeg_path,
            "ffprobe_path": ffprobe_path,
            "image_env": _env_state(
                "OPENAI_API_BASE_DB",
                "OPENAI_API_KEY_DB",
                "VSEGPT_API_KEY",
                "TEXT2IMAGE_MODEL",
                "IMG2IMG_MODEL",
                "VSEGPT_IMG2IMG_MODEL",
            ),
            "audio_env": _env_state(
                "ELEVENLABS_API_KEY",
                "OPENAI_AUDIO_API_KEY",
                "TTS_API_KEY",
                "STORYBOOK_AUDIO_ENABLED",
            ),
        },
        "expected_video_items": expected_video_items,
    }
    return summary


def _project_dir(project_id):
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


def _contract_dir(project_id):
    return _project_dir(project_id) / "96_video_contract"


def _load_shots(project_id):
    shots_path = _project_dir(project_id) / "97_shots" / "shots.json"
    if not shots_path.exists():
        return shots_path, [], None
    try:
        data = json.loads(shots_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return shots_path, [], f"{exc.msg} (line {exc.lineno}, column {exc.colno})"

    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return shots_path, data["items"], None
    if isinstance(data, list):
        return shots_path, data, None
    return shots_path, [], "shots.json must be a list or an object with an items list"


def _expected_video_items(items):
    expected = []
    for item in items:
        if not isinstance(item, dict):
            continue
        video_prompt = str(item.get("video_prompt") or "").strip()
        video_path = str(item.get("video_path") or "").strip()
        if video_prompt and video_path:
            expected.append(
                {
                    "scene_number": item.get("scene_number"),
                    "shot_number": item.get("shot_number"),
                    "video_path": video_path,
                }
            )
    return expected


def _select_provider():
    providers = [
        {
            "name": "aitunnel",
            "configured": _has_env("AITUNNEL_API_KEY") and _has_env("AITUNNEL_VIDEO_MODEL"),
            "required_env": ["AITUNNEL_API_KEY", "AITUNNEL_VIDEO_MODEL"],
        },
        {
            "name": "minimax",
            "configured": _has_env("MINIMAX_API_KEY"),
            "required_env": ["MINIMAX_API_KEY"],
        },
        {
            "name": "kling",
            "configured": _has_env("KLING_API_KEY") and _has_env("KLING_API_SECRET_KEY"),
            "required_env": ["KLING_API_KEY", "KLING_API_SECRET_KEY"],
        },
        {
            "name": "veo",
            "configured": _has_env("GOOGLE_CLOUD_PROJECT") or _has_env("GEMINI_API_KEY"),
            "required_env": ["GOOGLE_CLOUD_PROJECT or GEMINI_API_KEY"],
        },
    ]
    requested = (os.getenv("STORYBOOK_VIDEO_PROVIDER") or "").strip().lower()
    if requested:
        for provider in providers:
            if provider["name"] == requested:
                return (requested if provider["configured"] else "none"), providers
    for provider in providers:
        if provider["configured"]:
            return provider["name"], providers
    return "none", providers


def _image_capability():
    has_key = _has_env("OPENAI_API_KEY_DB") or _has_env("VSEGPT_API_KEY")
    has_model = (
        _has_env("TEXT2IMAGE_MODEL")
        or _has_env("IMG2IMG_MODEL")
        or _has_env("VSEGPT_IMG2IMG_MODEL")
    )
    return _has_env("OPENAI_API_BASE_DB") and has_key and has_model


def _audio_capability():
    return (
        _has_env("ELEVENLABS_API_KEY")
        or _has_env("OPENAI_AUDIO_API_KEY")
        or _has_env("TTS_API_KEY")
        or _truthy_env("STORYBOOK_AUDIO_ENABLED")
    )


def _decision_entries(preflight, delivery, approved):
    provider = preflight.get("provider") or "none"
    capabilities = preflight.get("capabilities") or {}
    delivery_status = delivery.get("status") or "unknown"
    will_generate = bool(delivery.get("will_generate_video"))
    return [
        {
            "name": "video_provider",
            "options_considered": [p["name"] for p in preflight.get("capability_details", {}).get("providers_considered", [])],
            "selected": provider,
            "reason": "Selected configured provider" if provider != "none" else "No configured video provider was available",
            "confidence": 0.9 if provider != "none" else 0.4,
            "cost": _known_provider_cost(provider),
            "approved": approved,
        },
        {
            "name": "delivery_promise",
            "options_considered": ["generate_motion_led_video", "block_on_missing_capability", "disabled"],
            "selected": "generate_motion_led_video" if will_generate else delivery_status,
            "reason": delivery.get("delivery_promise") or "Delivery promise artifact was not available",
            "confidence": 0.9 if delivery else 0.5,
            "cost": None,
            "approved": approved,
        },
        {
            "name": "render_runtime",
            "options_considered": ["ffmpeg_ffprobe", "none"],
            "selected": "ffmpeg_ffprobe" if capabilities.get("render") else "none",
            "reason": "ffmpeg and ffprobe are available" if capabilities.get("render") else "ffmpeg and ffprobe are not both available",
            "confidence": 0.95,
            "cost": {"amount": 0, "currency": "local"},
            "approved": approved,
        },
    ]


def _known_provider_cost(provider):
    if provider == "none":
        return None
    env_name = f"{provider.upper()}_VIDEO_COST_RUB"
    value = (os.getenv(env_name) or os.getenv("STORYBOOK_VIDEO_COST_RUB") or "").strip()
    if not value:
        return None
    try:
        return {"amount": float(value), "currency": "RUB", "source": env_name}
    except ValueError:
        return None


def _delivery_message(enabled, will_generate, blocking_reasons, expected_count):
    if will_generate:
        return f"Motion-led video generation is promised for {expected_count} expected clip(s)."
    if not enabled:
        return "Video generation is explicitly disabled for this run."
    return "Motion-led video generation is blocked: " + ", ".join(blocking_reasons)


def _atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _has_env(name):
    return bool((os.getenv(name) or "").strip())


def _truthy_env(name):
    return _as_bool(os.getenv(name))


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_state(*names):
    return {name: _has_env(name) for name in names}
