"""Короткоживущие подписанные токены на чтение медиа одного проекта."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 3600
_ENV_KEY = "STORYBOOK_MEDIA_SIGNING_KEY"
_process_key: bytes | None = None


def _signing_key() -> bytes:
    configured = os.getenv(_ENV_KEY)
    if configured:
        return configured.encode("utf-8")
    global _process_key
    if _process_key is None:
        _process_key = secrets.token_bytes(32)
        logger.warning(
            "%s не задан: ключ подписи медиа случаен на процесс, "
            "после перезапуска старые ссылки перестанут работать; "
            "при нескольких воркерах ключ обязателен, иначе воркеры не примут токены друг друга",
            _ENV_KEY,
        )
    return _process_key


def _sign(project_id: str, exp: str) -> str:
    message = f"sbm1|{project_id}|{exp}".encode("utf-8")
    return hmac.new(_signing_key(), message, hashlib.sha256).hexdigest()


def issue_media_token(project_id: str, ttl: int = DEFAULT_TTL_SECONDS, now: float | None = None) -> dict:
    current = time.time() if now is None else now
    exp = str(int(current) + int(ttl))
    return {
        "token": f"{exp}.{_sign(project_id, exp)}",
        "expires_at": int(exp),
        "ttl_seconds": int(ttl),
    }


def verify_media_token(project_id: str, token: str | None, now: float | None = None) -> bool:
    if not isinstance(token, str) or "." not in token:
        return False
    exp, _, signature = token.partition(".")
    # isascii: str.isdigit() пропускает «²» и подобные, на которых int() падает;
    # длина: int() отказывается разбирать строки длиннее 4300 цифр
    if not (exp.isascii() and exp.isdigit() and len(exp) <= 12):
        return False
    current = time.time() if now is None else now
    if int(exp) < current:
        return False
    # compare_digest не принимает не-ASCII str, поэтому сравниваем байты
    return hmac.compare_digest(signature.encode("utf-8"), _sign(project_id, exp).encode("ascii"))
