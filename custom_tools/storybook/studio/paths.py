"""Безопасные пути внутри проекта storybook для studio."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from custom_tools.storybook.project_paths import safe_storybook_project_dir

from .errors import StudioError

# расширение -> MIME для отдачи через HTTP-маршрут медиа
MEDIA_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
    ".pdf": "application/pdf",
}

_STUDIO_DIRNAME = ".studio"
_FORBIDDEN_NAMES = {".pipeline.lock"}


def project_dir(project_id: str, *, must_exist: bool = True) -> Path:
    try:
        return safe_storybook_project_dir(project_id, must_exist=must_exist)
    except ValueError as exc:
        text = str(exc)
        code = "not_found" if text.startswith("project not found") else "invalid_argument"
        raise StudioError(code, text) from exc


def resolve_in_project(project_id: str, rel_path: str, *, must_exist: bool = True) -> Path:
    """Путь внутри проекта; всё, что выходит наружу (включая симлинки), запрещено."""
    if not isinstance(rel_path, str) or not rel_path.strip() or "\0" in rel_path:
        raise StudioError("invalid_argument", "Не указан путь к файлу")
    pure = PurePosixPath(rel_path)
    if (
        "\\" in rel_path
        or pure.is_absolute()
        or (len(rel_path) > 1 and rel_path[1] == ":")
        or any(part in {"", ".", ".."} for part in rel_path.split("/"))
    ):
        raise StudioError("forbidden_path", "Недопустимый путь")
    for part in pure.parts:
        if part in _FORBIDDEN_NAMES or (part.startswith(".") and part != _STUDIO_DIRNAME):
            raise StudioError("forbidden_path", "Недопустимый путь")
    root = project_dir(project_id, must_exist=must_exist).resolve()
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise StudioError("forbidden_path", "Путь выходит за пределы проекта") from exc
    if must_exist and not target.is_file():
        raise StudioError("not_found", "Файл не найден")
    return target


def project_relpath(project_id: str, absolute: Path) -> str:
    root = project_dir(project_id).resolve()
    try:
        return absolute.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise StudioError("forbidden_path", "Путь выходит за пределы проекта") from exc


def studio_dir(project_id: str, *, create: bool = False) -> Path:
    path = project_dir(project_id) / _STUDIO_DIRNAME
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def thumbs_dir(project_id: str, *, create: bool = False) -> Path:
    path = studio_dir(project_id, create=create) / "thumbs"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path
