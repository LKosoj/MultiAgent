import json
from datetime import datetime
from pathlib import Path
from typing import Any

from custom_tools.storybook import project_paths
from custom_tools.storybook.studio.project_store import is_service_dir, is_series_project


def _has_shot_video(project_path: Path) -> bool:
    """Есть ли видео шотов; копии в 97_shots/_versions не считаются."""
    shots_dir = project_path / "97_shots"
    if not shots_dir.exists():
        return False
    return any("_versions" not in f.relative_to(shots_dir).parts for f in shots_dir.rglob("*.mp4"))


CONTRACT_VERSION = 1

JSON_ARTIFACTS = {
    "brief": "00_brief.json",
    "synopsis": "10_synopsis/synopsis.json",
    "beats": "10_synopsis/beats.json",
    "characters": "20_bible/characters.json",
    "locations": "20_bible/locations.json",
    "consistency_rules": "20_bible/consistency_rules.json",
    "story": "20_story/story.json",
    "style_text": "30_style/style_text.json",
    "style_images": "30_style/style_images.json",
    "screenplay": "91_screenplay/screenplay.json",
    "shots": "97_shots/shots.json",
    "video_plan": "97_shots/video_plan.json",
    "provider_jobs": "97_shots/provider_jobs.json",
    "chains": "93_blockout/chains.json",
    "scene_spec": "93_blockout/scene_spec.json",
    "asset_map": "93_blockout/asset_map.json",
    "audio_manifest": "98_audio/audio_manifest.json",
    "music_manifest": "98_audio/music_manifest.json",
    "final_manifest": "99_final/manifest.json",
    "asset_manifest": "99_final/asset_manifest.json",
    "edit_decisions": "99_final/edit_decisions.json",
    "render_report": "99_final/render_report.json",
    "final_review": "99_final/final_review.json",
}

MEDIA_DIRS = (
    "20_bible/references",
    "50_images",
    "97_shots",
    "98_audio",
    "99_final",
    # НЕ корень 93_blockout: обход там рекурсивный и ничем не ограничен
    # (frames/ шота — тысячи файлов), см. раздел 18.9 ТЗ.
    "93_blockout/preview",
)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}


def storybook_project_inventory(project_id: str | None = None, max_items: int = 20,
                                series_id: str | None = None) -> dict[str, Any]:
    """Return a read-only inventory shared by StoryBookManager, React, and Streamlit."""
    projects_dir = project_paths.storybook_projects_root()
    projects = [_project_preview(path, include_size=False) for path in _iter_projects(projects_dir)]
    payload: dict[str, Any] = {
        "version": CONTRACT_VERSION,
        "status": "success",
        "projects_dir": str(projects_dir),
        "projects_count": len(projects),
        "projects": projects,
        "selected_project": None,
    }

    selected_path = None
    if project_id:
        project_id = project_paths.require_safe_storybook_project_id(project_id)
        from custom_tools.storybook.studio.project_store import _project_path
        selected_path = _project_path(projects_dir, project_id)
        if series_id:
            from custom_tools.storybook.studio.series import series_dir
            selected_path = series_dir(project_id, series_id)
    if selected_path is not None:
        payload["selected_project"] = {
            "preview": _project_preview(selected_path, include_size=True, project_id=project_id),
            "artifacts": _artifact_inventory(selected_path),
            "media": _media_inventory(selected_path, max_items=max_items),
        }
    return payload


def _iter_projects(projects_dir: Path) -> list[Path]:
    if not projects_dir.exists():
        return []
    projects = [path for path in projects_dir.iterdir() if path.is_dir() and not is_service_dir(path.name)
                and is_series_project(path)]
    projects.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    return projects


def _project_preview(project_path: Path, include_size: bool, project_id: str | None = None) -> dict[str, Any]:
    brief = _read_json(project_path / ("project.json" if (project_path / "project.json").is_file() else "00_brief.json")) or {}
    story = _read_json(project_path / "20_story" / "story.json") or {}
    images_dir = project_path / "50_images"
    thumbnail = None
    if images_dir.exists():
        first_image = next((path for path in images_dir.rglob("*") if path.suffix.lower() in IMAGE_EXTENSIONS), None)
        if first_image is not None:
            thumbnail = _relative_path(project_path, first_image)

    return {
        "project_id": project_id or project_path.name,
        "name": str(brief.get("title") or project_path.name),
        "description": str(brief.get("description") or "")[:200],
        "language": brief.get("language"),
        "characters_count": len(brief.get("main_characters") or []) if isinstance(brief.get("main_characters"), list) else 0,
        "pages_count": len(story.get("pages") or []) if isinstance(story.get("pages"), list) else 0,
        "has_pdf": (project_path / "95_pdf" / "book.pdf").exists(),
        "has_video": _has_shot_video(project_path),
        "thumbnail": thumbnail,
        "size_bytes": _dir_size(project_path) if include_size else None,
        "created": _iso_mtime(project_path, ctime=True),
        "modified": _iso_mtime(project_path),
    }


def _artifact_inventory(project_path: Path) -> dict[str, Any]:
    artifacts = {}
    for name, relative_path in JSON_ARTIFACTS.items():
        path = project_path / relative_path
        entry: dict[str, Any] = {
            "path": relative_path,
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else 0,
            "modified": _iso_mtime(path) if path.exists() else None,
            "status": "missing",
        }
        if path.exists():
            error = _json_error(path)
            entry["status"] = "invalid" if error else "present"
            if error:
                entry["error"] = error
        artifacts[name] = entry
    return artifacts


def _media_inventory(project_path: Path, max_items: int) -> dict[str, Any]:
    items = []
    counts = {"image": 0, "video": 0, "audio": 0, "other": 0}
    # общие референсы серийного проекта лежат в корне проекта, а не в серии
    root = project_paths.reference_project_dir(project_path)
    dirs = [(project_path, d) for d in MEDIA_DIRS]
    bound: list[Path] = []
    if root != project_path:
        # серия видит только привязанные к ней референсы, как галерея
        from custom_tools.storybook.reference_catalog import bound_references
        try:
            records = bound_references(project_path, None)
        except (OSError, ValueError):
            records = []
        bound = [root / r["path"] for r in records if str(r["path"]).startswith("shared/references/")]
    elif (root / "shared" / "references").is_dir():
        dirs.append((root, "shared/references"))
    files = []
    for base, relative_dir in dirs:
        media_dir = base / relative_dir
        if media_dir.exists():
            files.extend((base, path) for path in media_dir.rglob("*")
                         if path.is_file() and "_versions" not in path.relative_to(media_dir).parts)
    files.extend((root, path) for path in bound if path.is_file())
    for base, path in files:
        kind = _media_kind(path)
        if kind is None:
            continue
        counts[kind] += 1
        if len(items) >= max_items:
            continue
        items.append(
            {
                "type": kind,
                "path": _relative_path(base, path),
                "name": path.name,
                "size": path.stat().st_size,
                "modified": _iso_mtime(path),
            }
        )
    return {"counts": counts, "items": items, "items_limit": max_items}


def _media_kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _json_error(path: Path) -> str | None:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
    except Exception as exc:
        return str(exc)
    return None


def _relative_path(project_path: Path, path: Path) -> str:
    try:
        return str(path.relative_to(project_path))
    except ValueError:
        return str(path)


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _iso_mtime(path: Path, ctime: bool = False) -> str | None:
    if not path.exists():
        return None
    stat = path.stat()
    value = stat.st_ctime if ctime else stat.st_mtime
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")
