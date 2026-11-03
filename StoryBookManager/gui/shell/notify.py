"""Тосты для панелей: без менеджера (тесты, импорт без окна) - запись в лог, без исключений."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_manager = None


def set_manager(manager) -> None:
    global _manager
    _manager = manager


def _emit(kind: str, text: str, details: Optional[str] = None) -> None:
    manager = _manager
    if manager is not None:
        try:
            manager.show(kind, text, details)
            return
        except Exception:
            logger.exception("Не удалось показать тост")
    level = {"error": logging.ERROR, "warning": logging.WARNING}.get(kind, logging.INFO)
    logger.log(level, "[тост %s] %s", kind, text)


def success(text: str) -> None:
    _emit("success", text)


def info(text: str) -> None:
    _emit("info", text)


def warning(text: str) -> None:
    _emit("warning", text)


def error(text: str, details: Optional[str] = None) -> None:
    _emit("error", text, details)
