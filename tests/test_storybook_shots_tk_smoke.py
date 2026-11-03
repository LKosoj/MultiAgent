"""Дымовая проверка экрана «Кадры» в настоящем Tk под Xvfb (пропуск без xvfb-run)."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tests" / "helpers" / "tk_shots_smoke.py"


def _run(width: int, height: int) -> dict:
    cmd = ["xvfb-run", "-a", "-s", "-screen 0 1700x1000x24", sys.executable, str(HELPER),
           "--width", str(width), "--height", str(height)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=ROOT)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("SMOKE_REPORT ")]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    return json.loads(lines[-1][len("SMOKE_REPORT "):])


@pytest.mark.skipif(shutil.which("xvfb-run") is None, reason="нет xvfb-run")
@pytest.mark.parametrize("size", [(1280, 800), (1024, 680)])
def test_shots_screen_smoke(size):
    try:
        import tkinter  # noqa: F401
    except ImportError:
        pytest.skip("нет tkinter")
    report = _run(*size)
    checks = report["checks"]
    assert report["errors"] == []
    assert checks["loaded"] == ["ready", 100, 250]
    assert checks["selected"] == "1-1"
    assert checks["down"][:2] == ["1-1", "1-2"]
    assert checks["approved"]["status"] == "approved"
    assert checks["redo"] == "redo"
    assert checks["canvas_items"] <= 60 and checks["canvas_items_after_scroll"] <= 60
    assert checks["canvas_items_grid"] <= 60
    assert checks["scrolled_top"] > 0
    assert not any(report["clipped"].values()), report["clipped"]
