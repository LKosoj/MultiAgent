"""Единый формат результатов и ошибок studio."""

from __future__ import annotations

CODES = (
    "invalid_argument",
    "not_found",
    "conflict",
    "locked",
    "forbidden_path",
    "unsupported",
    "unavailable",
    "unknown_capability",
    "invalid_result",
    "internal",
)


class StudioError(Exception):
    """Ожидаемая ошибка возможности: код + короткое русское сообщение."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def success(**data) -> dict:
    return {"status": "success", **data}


def error_result(code: str, message: str, **details) -> dict:
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"status": "error", "error": error}


def from_exception(exc: BaseException) -> dict:
    if isinstance(exc, StudioError):
        return error_result(exc.code, exc.message, **exc.details)
    if isinstance(exc, ValueError):
        return error_result("invalid_argument", str(exc))
    return error_result("internal", "Внутренняя ошибка")


def is_error(result: object) -> bool:
    return isinstance(result, dict) and result.get("status") == "error"
