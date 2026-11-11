"""Возможности самого каркаса: токен медиа."""

from __future__ import annotations

from .capabilities import capability
from .errors import success
from .media_tokens import issue_media_token
from .paths import project_dir


@capability("media_token", title="Токен для медиа проекта", access="read")
def media_token(project_id: str, series_id: str | None = None) -> dict:
    project_dir(project_id)  # нет проекта -> not_found
    if series_id is not None:
        from .series import series_dir
        series_dir(project_id, series_id)
    issued = issue_media_token(project_id, series_id=series_id)
    # поле называется "t", как параметр ссылки: ключ "token" маскирует редактор секретов
    return success(t=issued["token"], expires_at=issued["expires_at"], ttl_seconds=issued["ttl_seconds"])
