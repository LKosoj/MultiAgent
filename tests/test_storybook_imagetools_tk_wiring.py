"""Проводка зума и правки ИИ в экран «Кадры» (настоящий Tk, запускать под xvfb-run -a)."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "helpers"))

tk = pytest.importorskip("tkinter")
pytest.importorskip("PIL")

from imagetools_tk_env import PID, PROMPT, fake_tool, make_project, no_price, pump  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    if not os.environ.get("DISPLAY"):
        pytest.skip("нет дисплея: запускать под xvfb-run -a")
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    no_price(monkeypatch)
    make_project(tmp_path, pages=1)
    root = tk.Tk()
    from StoryBookManager.gui.theme import apply_theme

    apply_theme(root)
    root.geometry("1280x800+0+0")
    from StoryBookManager.core.studio_bridge import StudioBridge
    from StoryBookManager.gui.shots.panel import ShotsPanel

    bridge = StudioBridge(root, poll_ms=20)
    panel = ShotsPanel(root, bridge=bridge, get_project_id=lambda: PID, on_goto_generation=lambda: None)
    panel.pack(fill="both", expand=True)
    panel.ctl.on_show()
    pump(root, until=lambda: panel.ctl.load_state == "ready")
    panel.ctl.select("1-1", immediate=True)
    pump(root, until=lambda: panel.ctl.card_state == "ready")
    pump(root, 0.3)
    yield root, panel, tmp_path / PID
    panel.shutdown()
    root.destroy()


def edit_buttons(panel):
    return {name: tile.get("edit") for name, tile in panel.card_view._tiles.items()}


def test_edit_button_only_on_start_and_end_tiles(env):
    _, panel, _ = env
    buttons = edit_buttons(panel)
    assert {"start", "end", "video"} <= set(buttons)
    assert buttons["start"] is not None and buttons["end"] is not None
    assert buttons["video"] is None
    assert all(b is None for n, b in buttons.items() if n not in ("start", "end"))
    assert buttons["start"]["text"] == "Править ИИ…"


def test_edit_button_blocked_while_run_is_busy(env):
    root, panel, _ = env
    buttons = edit_buttons(panel)
    panel.ctl.run_state = "running"
    panel.refresh("busy")
    assert str(buttons["start"]["state"]) == "disabled" and panel._edit_block()
    panel._edit_part("start")            # даже прямой вызов окна не открывает
    pump(root, 0.1)
    assert not [w for w in panel.winfo_children() if isinstance(w, tk.Toplevel)]
    panel.ctl.run_state = "idle"
    panel.refresh("busy")
    assert str(buttons["start"]["state"]) == "normal" and panel._edit_block() == ""


def test_viewer_has_zoom_and_edit_for_start_only(env):
    root, panel, _ = env
    card = panel.ctl.card
    items = [("Начало", card["parts"]["start"]["path"]), ("Конец", card["parts"]["end"]["path"])]
    panel._view_images(items, 0)
    viewer = next(w for w in panel.winfo_children() if isinstance(w, tk.Toplevel))
    pump(root, 0.3)
    assert viewer.canvas.has_image and viewer.edit_btn is not None and viewer.current_part() == "start"
    assert str(viewer.edit_btn["state"]) == "normal"
    viewer.event_generate("<Right>")
    assert viewer.current_part() == "end"
    viewer.destroy()


def test_edit_from_card_replaces_picture_and_saves_version(env, monkeypatch):
    root, panel, project = env
    state = fake_tool(monkeypatch)
    path = project / "97_shots" / "scene_01_shot_01" / "img_final_start_01_01.png"
    before = path.read_bytes()
    panel._edit_part("start")
    dialog = next(w for w in panel.winfo_children() if isinstance(w, tk.Toplevel))
    pump(root, until=lambda: dialog.preview is not None)
    assert dialog.go_btn["text"] == "Править без оценки"
    dialog.prompt.insert("1.0", PROMPT)
    pump(root, 0.1)
    dialog.go_btn.invoke()
    pump(root, until=lambda: path.read_bytes() != before and not panel.edit_ctl.is_running(), timeout=30)
    pump(root, 0.8)
    assert state["calls"][0]["images"][0].endswith("img_final_start_01_01.png")
    assert panel.edit_status.get() == "" and not panel.edit_status_label.winfo_ismapped()
    versions = list((project / "97_shots" / "_versions" / "1-1").glob("*")) if (project / "97_shots" / "_versions" / "1-1").exists() else []
    assert versions, "перед правкой должна появиться версия шота"
    assert str(edit_buttons(panel)["start"]["state"]) == "normal"


def test_panel_shutdown_closes_edit_controller_and_job_runner(env):
    _, panel, _ = env
    panel.shutdown()
    assert panel.edit_ctl.closed and panel.edit_runner._closed
