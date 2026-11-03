"""Только Tk: открыть .blend в Blender и превью болванки во внешнем плеере."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


def blender_binary_path() -> Optional[str]:
    """`BLOCKOUT_BLENDER_BIN`, иначе `blender` в `PATH`; None - кнопка неактивна."""
    configured = os.environ.get("BLOCKOUT_BLENDER_BIN")
    if configured and Path(configured).is_file() and os.access(configured, os.X_OK):
        return configured
    return shutil.which("blender")


def open_blend(project_dir, blend_path) -> bool:
    binary = blender_binary_path()
    full = Path(project_dir) / blend_path
    if not binary or not full.is_file():
        return False
    try:
        subprocess.Popen([binary, str(full)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False
