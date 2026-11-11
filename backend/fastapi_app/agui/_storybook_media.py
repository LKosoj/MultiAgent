"""Медиа-маршруты storybook: доступ по токену проекта или по токену AG-UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from .auth import authenticate_request

_STATUS_BY_CODE = {
    "invalid_argument": 400,
    "forbidden_path": 403,
    "not_found": 404,
}


def authorize_media_request(request: Request, project_id: str, token: str | None, series_id: str | None = None) -> None:
    from custom_tools.storybook.studio.media_tokens import verify_media_token

    if token and verify_media_token(project_id, token, series_id=series_id):
        return
    principal = authenticate_request(request)  # 401/403 как у остальных маршрутов
    if not principal.has_role("admin"):
        raise HTTPException(status_code=403, detail="storybook media requires role 'admin'")


def _raise_http(exc) -> None:
    raise HTTPException(status_code=_STATUS_BY_CODE.get(exc.code, 400), detail=exc.message) from exc


_ZIP_MIME = "application/zip"


def _is_export_zip(project_id: str, target: Path) -> bool:
    """ZIP отдаём только из `.studio/exports/`; проверяем уже разрешённый путь (симлинки раскрыты)."""
    from custom_tools.storybook.studio.paths import project_dir

    exports = (project_dir(project_id) / ".studio" / "exports").resolve()
    return target.suffix.lower() == ".zip" and target.parent == exports


def locate_media_file(project_id: str, path: str, series_id: str | None = None) -> tuple[Path, str]:
    """Путь к файлу проекта и его MIME; расширение вне белого списка запрещено."""
    from custom_tools.storybook.studio.errors import StudioError
    from custom_tools.storybook.studio.paths import MEDIA_EXTENSIONS, resolve_in_project

    from custom_tools.storybook.studio.series import _project
    from custom_tools.storybook.project_paths import series_scope
    from contextlib import nullcontext
    try:
        _project(project_id)
        if series_id is None and not (path.startswith("shared/") or path.startswith(".studio/exports/")):
            raise StudioError("forbidden_path", "Файл не относится к общим ресурсам")
        with series_scope(project_id, series_id) if series_id is not None else nullcontext():
            target = resolve_in_project(project_id, path)
    except StudioError as exc:
        _raise_http(exc)
    mime = MEDIA_EXTENSIONS.get(target.suffix.lower())
    if mime is None and _is_export_zip(project_id, target):
        mime = _ZIP_MIME
    if mime is None:
        raise HTTPException(status_code=403, detail="file type is not allowed")
    return target, mime


def make_thumb_response_source(project_id: str, path: str, size: int, series_id: str | None = None):
    """Миниатюра в потоке-воркере: (путь к jpeg | байты заглушки, признак заглушки)."""
    from custom_tools.storybook.studio.errors import StudioError
    from custom_tools.storybook.studio.thumbs import make_thumb

    from custom_tools.storybook.project_paths import series_scope
    from contextlib import nullcontext
    try:
        locate_media_file(project_id, path, series_id)
        with series_scope(project_id, series_id) if series_id is not None else nullcontext():
            return make_thumb(project_id, path, size)
    except StudioError as exc:
        _raise_http(exc)
