"""Возможности самого каркаса: токен медиа."""

from __future__ import annotations

from .capabilities import capability
from .errors import success
from .media_tokens import issue_media_token
from .paths import project_dir


@capability("media_token", title="Токен для медиа проекта", access="read")
def media_token(project_id: str) -> dict:
    project_dir(project_id)  # нет проекта -> not_found
    issued = issue_media_token(project_id)
    # поле называется "t", как параметр ссылки: ключ "token" маскирует редактор секретов
    return success(t=issued["token"], expires_at=issued["expires_at"], ttl_seconds=issued["ttl_seconds"])
