"""Общая обвязка Tk-тестов зума и правки картинок: проект во временной папке, подмена платного инструмента."""

import io
import sys
import time
import types
from pathlib import Path

PID = "demo"
PROMPT = "сделай небо розовым"


def png(color=(200, 10, 10), size=(8, 6)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def make_project(base: Path, pages: int = 3, per_page: int = 1, size=(8, 6)) -> Path:
    """Проект с шотами (2 картинки + видео-заглушка) и иллюстрациями page_001.. в 50_images."""
    from tests.test_storybook_studio_shots import build

    from tests.studio_run_fakes import make_series_project

    root = make_series_project(base, PID)
    build(root)
    for scene, shot in ((1, 1), (2, 3)):
        folder = root / "97_shots" / f"scene_{scene:02d}_shot_{shot:02d}"
        for part in ("start", "end"):
            (folder / f"img_final_{part}_{scene:02d}_{shot:02d}.png").write_bytes(png((10 * scene, 50, 90), size))
    for page in range(1, pages + 1):
        folder = root / "50_images" / f"page_{page:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        for n in range(per_page):
            (folder / f"img_{n:03d}.png").write_bytes(png((page % 255, n % 255, 120), size))
    return root


def pump(root, seconds: float = 0.3, until=None, timeout: float = 15.0) -> bool:
    """Дать мосту и таймерам отработать; с until - ждать условие до timeout."""
    end = time.time() + (timeout if until else seconds)
    while time.time() < end:
        root.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until is None


def fake_tool(monkeypatch, color=(0, 200, 0), mode="ok"):
    """Подмена перевода и инструмента правки: сети и денег нет. Возвращает журнал вызовов."""
    state = {"calls": [], "mode": mode}
    fake_utils = types.ModuleType("utils")
    tools = types.ModuleType("custom_tools.image_tools")
    monkeypatch.setitem(sys.modules, "utils", fake_utils)
    monkeypatch.setitem(sys.modules, "custom_tools.image_tools", tools)
    fake_utils._translate_texts_batch = lambda texts, lang: [f"EN:{t}" for t in texts]

    def edit(prompt, image_paths, session_id, output_path=None, **kw):
        state["calls"].append({"prompt": prompt, "images": list(image_paths), "output": output_path})
        if state["mode"] == "raise":
            raise RuntimeError("secret")
        with open(output_path, "wb") as fh:
            fh.write(png(color, (16, 12)))
        return output_path

    tools.edit_image_vse_tool = edit
    return state


def no_price(monkeypatch) -> None:
    """Цена картинки неизвестна, как в боевом конфиге."""
    from custom_tools.storybook.studio import cost_estimate

    monkeypatch.setattr(cost_estimate, "_flat_price", lambda section, field: None)


def widget_count(widget) -> int:
    return 1 + sum(widget_count(child) for child in widget.winfo_children())
