import json
import os
import shutil
from pathlib import Path

from custom_tools.storybook.project_paths import safe_storybook_project_dir


CONTRACT_VERSION = 1
STORYBOOK_WORKFLOW_NAME = "storybook_pipeline"


STORYBOOK_WORKFLOW_ACTIONS = (
    {
        "id": "project_inventory",
        "label": "List storybook projects and selected project inventory",
        "category": "project_lifecycle",
        "status": "available",
        "service_action": "workflows.storybook_project_inventory",
        "surfaces": {
            "storybook_manager": "available",
            "react": "available",
            "streamlit": "available",
        },
    },
    {
        "id": "artifact_inventory",
        "label": "Inspect storybook JSON artifact status",
        "category": "artifact_editor",
        "status": "available",
        "service_action": "workflows.storybook_project_inventory",
        "surfaces": {
            "storybook_manager": "available",
            "react": "available",
            "streamlit": "available",
        },
    },
    {
        "id": "media_inventory",
        "label": "Inspect storybook media inventory",
        "category": "media_manager",
        "status": "available",
        "service_action": "workflows.storybook_project_inventory",
        "surfaces": {
            "storybook_manager": "available",
            "react": "available",
            "streamlit": "available",
        },
    },
    {
        "id": "artifact_edit",
        "label": "Edit storybook JSON artifacts",
        "category": "artifact_editor",
        "status": "manager_only",
        "service_action": None,
        "surfaces": {
            "storybook_manager": "available",
            "react": "unsupported",
            "streamlit": "unsupported",
        },
        "reason": "Web artifact editing needs explicit write/backup UX before enabling.",
    },
    {
        "id": "media_edit",
        "label": "Edit/overwrite storybook media with backup",
        "category": "media_manager",
        "status": "manager_only",
        "service_action": None,
        "surfaces": {
            "storybook_manager": "available",
            "react": "unsupported",
            "streamlit": "unsupported",
        },
        "reason": "Web media overwrite needs explicit backup and preview flow.",
    },
    {
        "id": "project_create_backup_export_delete",
        "label": "Create, backup, export, delete storybook projects",
        "category": "project_lifecycle",
        "status": "manager_only",
        "service_action": None,
        "surfaces": {
            "storybook_manager": "available",
            "react": "unsupported",
            "streamlit": "unsupported",
        },
        "reason": "Destructive project operations need dedicated web confirmations.",
    },
    {
        "id": "full_pipeline",
        "label": "Run full pipeline",
        "category": "execution",
        "status": "available",
        "service_action": "workflows.start",
        "surfaces": {
            "storybook_manager": "available",
            "react": "available",
            "streamlit": "available",
        },
    },
    {
        "id": "video_music_readiness",
        "label": "Video/music readiness",
        "category": "preflight",
        "status": "available",
        "service_action": "workflows.storybook_readiness",
        "surfaces": {
            "storybook_manager": "available",
            "react": "available",
            "streamlit": "available",
        },
    },
    {
        "id": "validate_project",
        "label": "Validate project",
        "category": "preflight",
        "status": "available",
        "service_action": "workflows.storybook_validate",
        "surfaces": {
            "storybook_manager": "available",
            "react": "available",
            "streamlit": "available",
        },
    },
    {
        "id": "monitor_run",
        "label": "Monitor status, artifacts, result, logs",
        "category": "monitoring",
        "status": "available",
        "service_action": "workflows.status/workflows.artifacts/workflows.result/logs.run_logs",
        "surfaces": {
            "storybook_manager": "available",
            "react": "available",
            "streamlit": "available",
        },
    },
    {
        "id": "cancel_run",
        "label": "Cancel running pipeline",
        "category": "execution",
        "status": "available",
        "service_action": "workflows.cancel",
        "surfaces": {
            "storybook_manager": "available",
            "react": "available",
            "streamlit": "available",
        },
    },
    {
        "id": "run_from_step",
        "label": "Run from selected step",
        "category": "execution",
        "status": "manager_only",
        "service_action": None,
        "surfaces": {
            "storybook_manager": "available",
            "react": "unsupported",
            "streamlit": "unsupported",
        },
        "reason": "Web WorkflowManager currently starts whole YAML pipelines only.",
    },
    {
        "id": "rerun_single_step",
        "label": "Rerun one pipeline step from checkpoint",
        "category": "execution",
        "status": "manager_only",
        "service_action": None,
        "surfaces": {
            "storybook_manager": "available",
            "react": "unsupported",
            "streamlit": "unsupported",
        },
        "reason": "Requires latest StoryBookManager project checkpoint context.",
    },
    {
        "id": "pause_resume",
        "label": "Pause/resume between steps",
        "category": "execution",
        "status": "manager_only",
        "service_action": None,
        "surfaces": {
            "storybook_manager": "available",
            "react": "unsupported",
            "streamlit": "unsupported",
        },
        "reason": "Pause/resume is bound to the local PipelineRunner instance.",
    },
    {
        "id": "regenerate_image",
        "label": "Regenerate one image",
        "category": "selective_regeneration",
        "status": "not_implemented",
        "service_action": None,
        "surfaces": {
            "storybook_manager": "not_implemented",
            "react": "not_implemented",
            "streamlit": "not_implemented",
        },
    },
    {
        "id": "regenerate_video",
        "label": "Regenerate one video shot",
        "category": "selective_regeneration",
        "status": "not_implemented",
        "service_action": None,
        "surfaces": {
            "storybook_manager": "not_implemented",
            "react": "not_implemented",
            "streamlit": "not_implemented",
        },
    },
    {
        "id": "fix_project_errors",
        "label": "Auto-fix project errors",
        "category": "maintenance",
        "status": "not_implemented",
        "service_action": None,
        "surfaces": {
            "storybook_manager": "not_implemented",
            "react": "not_implemented",
            "streamlit": "not_implemented",
        },
    },
    {
        "id": "yaml_builder",
        "label": "View/edit/build workflow YAML",
        "category": "workflow_authoring",
        "status": "web_only",
        "service_action": "workflows.parse_yaml/workflows.get_yaml/workflows.generate_yaml/workflows.save_yaml",
        "surfaces": {
            "storybook_manager": "unsupported",
            "react": "available",
            "streamlit": "available",
        },
    },
)


def storybook_workflow_actions(project_id: str | None = None) -> dict:
    """Return the shared UI action contract for storybook workflows."""
    return {
        "version": CONTRACT_VERSION,
        "status": "success",
        "workflow_name": STORYBOOK_WORKFLOW_NAME,
        "project_id": str(project_id).strip() if project_id else None,
        "actions": [dict(action) for action in STORYBOOK_WORKFLOW_ACTIONS],
    }


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


def storybook_video_music_readiness(
    project_id: str,
    session_id: str = "ui-readiness",
    language: str = "ru",
    enable: bool = True,
    generate_music: bool = True,
) -> dict:
    """Return a UI-safe readiness summary for storybook video, music, and render.

    The summary contains booleans, provider names, paths, and artifact statuses only.
    It intentionally never returns raw API keys or other secret values.
    """
    base = _project_dir(project_id)
    summary = _provider_summary(session_id, project_id, language, enable)
    contract_dir = base / "96_video_contract"
    shots_dir = base / "97_shots"
    audio_dir = base / "98_audio"
    final_dir = base / "99_final"
    delivery_path = contract_dir / "delivery_promise.json"
    provider_jobs_path = shots_dir / "provider_jobs.json"
    audio_manifest_path = audio_dir / "audio_manifest.json"
    music_manifest_path = audio_dir / "music_manifest.json"
    final_review_path = final_dir / "final_review.json"

    music_manifest, music_manifest_error = _read_json_artifact(music_manifest_path)
    audio_manifest, audio_manifest_error = _read_json_artifact(audio_manifest_path)
    provider_jobs, provider_jobs_error = _read_json_artifact(provider_jobs_path)
    delivery, delivery_error = _read_json_artifact(delivery_path)
    final_review, final_review_error = _read_json_artifact(final_review_path)
    music_manifest = music_manifest or {}
    audio_manifest = audio_manifest or {}
    provider_jobs = provider_jobs or {}
    delivery = delivery or {}
    final_review = final_review or {}
    artifact_errors = [
        error
        for error in (
            music_manifest_error,
            audio_manifest_error,
            provider_jobs_error,
            delivery_error,
            final_review_error,
        )
        if error
    ]

    music_path = audio_dir / "music.mp3"
    final_video_path = final_dir / "final_video.mp4"
    subtitles_path = audio_dir / "subtitles.srt"
    capabilities = dict(summary["capabilities"])
    capabilities["music"] = _has_env("SUNO_COOKIE")
    music_enabled = _as_bool(generate_music)
    music_configured = _has_env("SUNO_COOKIE")
    music_exists = music_path.exists()
    generation_enabled = _as_bool(enable)
    blocking_reasons = _readiness_blockers(summary, capabilities)
    warnings = _readiness_warnings(summary, music_enabled, music_configured, music_exists)
    errors = _readiness_errors(
        provider_jobs,
        final_review,
        music_manifest,
        artifact_errors,
        provider_jobs_path=provider_jobs_path,
    )

    payload = {
        "version": CONTRACT_VERSION,
        "status": "success",
        "ready": not blocking_reasons and not errors,
        "project_id": str(project_id),
        "project_path": str(base),
        "generation_enabled": generation_enabled,
        "generation": {
            "video_enabled": generation_enabled,
            "music_enabled": music_enabled,
            "language": language,
        },
        "capabilities": capabilities,
        "video": {
            "provider": summary["provider"],
            "providers_considered": summary["capability_details"].get("providers_considered", []),
            "expected_clip_count": summary["expected_video_count"],
            "items_total": summary["items_total"],
            "shots_path": summary["shots_path"],
            "shots_exists": summary["shots_exists"],
            "shots_error": summary["shots_error"],
        },
        "music": {
            "enabled": music_enabled,
            "provider": "suno",
            "configured": music_configured,
            "api_base_url": "https://studio-api.prod.suno.com",
            "model": (os.getenv("SUNO_MODEL") or "chirp-fenix").strip(),
            "captcha_token_configured": _has_env("SUNO_HCAPTCHA_TOKEN"),
            "status": "disabled" if not music_enabled else music_manifest.get("status") or audio_manifest.get("music_status") or "not_generated",
            "task_id": music_manifest.get("task_id"),
            "music_path": str(music_path),
            "music_exists": music_exists,
            "manifest_path": str(base / "98_audio" / "music_manifest.json"),
            "manifest_exists": bool(music_manifest),
        },
        "render": {
            "ffmpeg_path": summary["capability_details"].get("ffmpeg_path"),
            "ffprobe_path": summary["capability_details"].get("ffprobe_path"),
            "configured": bool(summary["capabilities"].get("render")),
        },
        "artifacts": {
            "provider_menu_summary": _artifact_status(contract_dir / "provider_menu_summary.json"),
            "delivery_promise": _artifact_status(delivery_path, _invalid_or_status(delivery_error, delivery.get("status"))),
            "decision_log": _artifact_status(contract_dir / "decision_log.json"),
            "provider_jobs": _provider_jobs_status(provider_jobs_path, provider_jobs, provider_jobs_error),
            "audio_manifest": _artifact_status(audio_manifest_path, _invalid_or_status(audio_manifest_error, audio_manifest.get("tts_status"))),
            "cue_sheet": _artifact_status(audio_dir / "cue_sheet.json"),
            "subtitles": _artifact_status(subtitles_path),
            "music_manifest": _artifact_status(music_manifest_path, _invalid_or_status(music_manifest_error, music_manifest.get("status"))),
            "music": _artifact_status(music_path, music_manifest.get("status") if music_path.exists() and not music_manifest_error else None),
            "final_video": _artifact_status(final_video_path),
            "timeline": _artifact_status(final_dir / "timeline.fcpxml"),
            "final_subtitles": _artifact_status(final_dir / "subtitles.srt"),
            "manifest": _artifact_status(final_dir / "manifest.json"),
            "asset_manifest": _artifact_status(final_dir / "asset_manifest.json"),
            "edit_decisions": _artifact_status(final_dir / "edit_decisions.json"),
            "render_report": _artifact_status(final_dir / "render_report.json"),
            "final_review": _artifact_status(final_review_path, _invalid_or_status(final_review_error, _final_review_status(final_review))),
        },
        "final_review": _final_review_summary(final_review),
        "workflow_actions": storybook_workflow_actions(project_id),
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "errors": errors,
    }
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


def _readiness_blockers(summary, capabilities):
    blockers = []
    if not summary.get("generation_enabled"):
        return blockers
    if summary.get("shots_error"):
        blockers.append("shots_json_invalid")
    if not capabilities.get("video"):
        blockers.append("video_provider_unavailable")
    if not capabilities.get("render"):
        blockers.append("render_capability_unavailable")
    if summary.get("shots_exists") and summary.get("expected_video_count", 0) == 0:
        blockers.append("no_expected_video_clips")
    return blockers


def _readiness_warnings(summary, music_enabled, music_configured, music_exists):
    warnings = []
    if music_enabled and not music_configured and not music_exists:
        warnings.append("music_provider_unavailable")
    if not summary.get("shots_exists"):
        warnings.append("shots_json_missing_before_run")
    return warnings


def _readiness_errors(provider_jobs, final_review, music_manifest, artifact_errors=None, provider_jobs_path=None):
    errors = list(artifact_errors or [])
    jobs = provider_jobs.get("jobs") if isinstance(provider_jobs, dict) else None
    if isinstance(jobs, list):
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if str(job.get("status")) in {"failed", "download_failed"}:
                shot_key = job.get("shot_key") or job.get("output_path") or "unknown"
                errors.append(f"provider_job_failed:{shot_key}")
    elif provider_jobs:
        name = provider_jobs_path.name if provider_jobs_path else "provider_jobs.json"
        errors.append(f"invalid_json_shape:{name}")

    music_status = str(music_manifest.get("status") or "").lower() if isinstance(music_manifest, dict) else ""
    if music_status in {"error", "failed"}:
        errors.append("music_generation_failed")

    if isinstance(final_review, dict) and final_review:
        if final_review.get("passed") is False:
            errors.append("final_review_failed")
        review_errors = final_review.get("errors")
        if isinstance(review_errors, list):
            errors.extend(str(error) for error in review_errors if error)

    return errors


def _artifact_status(path, status=None):
    return {
        "path": str(path),
        "exists": path.exists(),
        "status": status or ("present" if path.exists() else "missing"),
    }


def _invalid_or_status(error, status):
    return "invalid" if error else status


def _provider_jobs_status(path, provider_jobs, error=None):
    status = "missing"
    if error:
        status = "invalid"
    elif provider_jobs:
        jobs = provider_jobs.get("jobs") if isinstance(provider_jobs, dict) else None
        if isinstance(jobs, list):
            failed = any(isinstance(job, dict) and str(job.get("status")) in {"failed", "download_failed"} for job in jobs)
            status = "failed" if failed else "present"
        else:
            status = "invalid"
    payload = _artifact_status(path, status)
    jobs = provider_jobs.get("jobs") if isinstance(provider_jobs, dict) else None
    payload["job_count"] = len(jobs) if isinstance(jobs, list) else 0
    return payload


def _final_review_status(final_review):
    if not final_review:
        return None
    return "passed" if final_review.get("passed") else "failed"


def _final_review_summary(final_review):
    if not final_review:
        return {"exists": False, "passed": None, "failed_checks": [], "errors": []}
    checks = final_review.get("checks")
    failed_checks = [
        name
        for name, value in checks.items()
        if isinstance(value, dict) and not value.get("passed", False)
    ] if isinstance(checks, dict) else []
    errors = final_review.get("errors")
    return {
        "exists": True,
        "passed": final_review.get("passed"),
        "failed_checks": failed_checks,
        "errors": errors if isinstance(errors, list) else [],
    }


def _project_dir(project_id):
    return safe_storybook_project_dir(project_id)


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


def _read_json_artifact(path):
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"invalid_json:{path.name}:line_{exc.lineno}_column_{exc.colno}"
    except OSError as exc:
        return None, f"unreadable_json:{path.name}:{exc.__class__.__name__}"
    if not isinstance(data, dict):
        return None, f"invalid_json_shape:{path.name}"
    return data, None


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
