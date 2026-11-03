"""Внешние программы: плеер, проводник, браузер. Запуск без ожидания (интерфейс не блокируется)."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)


def _command(target: str, platform: str) -> list:
    if platform == "darwin":
        return ["open", target]
    return ["explorer", target] if platform.startswith("win") else ["xdg-open", target]


def open_external(path_or_url: Union[str, Path], platform: str = "") -> bool:
    """Открыть файл, папку или ссылку в программе по умолчанию; False при сбое запуска."""
    target = str(path_or_url)
    platform = platform or sys.platform
    try:
        if platform.startswith("win") and hasattr(os, "startfile"):
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(_command(target, platform), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        logger.exception("Не удалось открыть %s", target)
        return False


def play_video(path: Union[str, Path]) -> bool:
    """Фильм во внешнем плеере (со звуком)."""
    if not Path(path).is_file():
        return False
    return open_external(path)


def reveal(path: Union[str, Path]) -> bool:
    """Показать файл в проводнике: открывается папка, где он лежит."""
    target = Path(path)
    return open_external(target.parent if target.suffix else target)
