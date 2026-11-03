"""Зум, окно правки и режим «Иллюстрации» в настоящем Tk (запускать под xvfb-run -a)."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "helpers"))

tk = pytest.importorskip("tkinter")
pytest.importorskip("PIL")

from imagetools_tk_env import PID, PROMPT, fake_tool, make_project, no_price, png, pump, widget_count  # noqa: E402
from tk_shell_smoke import find_clipped  # noqa: E402

SIZES = [(1280, 800), (1024, 680)]


@pytest.fixture
def root():
    if not os.environ.get("DISPLAY"):
        pytest.skip("нет дисплея: запускать под xvfb-run -a")
    win = tk.Tk()
    from StoryBookManager.gui.theme import apply_theme

    apply_theme(win)
    win.geometry("1000x700+0+0")
    win.update()
    yield win
    win.destroy()


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setenv("STORYBOOK_PROJECTS_DIR", str(tmp_path))
    no_price(monkeypatch)
    return tmp_path


def bridge_for(root):
    from StoryBookManager.core.studio_bridge import StudioBridge

    bridge = StudioBridge(root, poll_ms=20)
    return bridge


# --- холст и окно просмотра ---

def test_canvas_zoom_buttons_wheel_and_drag(root, tmp_path):
    from StoryBookManager.gui.imagetools.zoom_canvas import ZoomCanvas

    big = tmp_path / "big.png"
    big.write_bytes(png(size=(1600, 1200)))
    levels = []
    canvas = ZoomCanvas(root, on_change=levels.append)
    canvas.pack(fill="both", expand=True)
    pump(root, 0.3)
    assert canvas.load(str(big))
    pump(root, 0.3)
    fitted = canvas.state.scale
    assert canvas.state.fit_mode and fitted < 1 and levels[-1].endswith("%")
    canvas.zoom("in")
    assert canvas.state.scale > fitted and not canvas.state.fit_mode
    canvas.actual()
    assert canvas.state.scale == 1.0 and canvas.state.can_pan()
    canvas.event_generate("<ButtonPress-1>", x=100, y=100)
    canvas.event_generate("<B1-Motion>", x=140, y=120)
    root.update()
    assert (canvas.state.dx, canvas.state.dy) == (40, 20)
    canvas.event_generate("<Button-5>")      # колесо вниз (Linux)
    assert canvas.state.scale < 1.0
    canvas.fit()
    assert canvas.state.fit_mode and canvas.state.scale == fitted and canvas.state.dx == 0
    assert not canvas.load(str(tmp_path / "missing.png")) and not canvas.has_image


def test_canvas_resize_keeps_manual_zoom(root, tmp_path):
    from StoryBookManager.gui.imagetools.zoom_canvas import ZoomCanvas

    img = tmp_path / "a.png"
    img.write_bytes(png(size=(400, 300)))
    canvas = ZoomCanvas(root)
    canvas.pack(fill="both", expand=True)
    canvas.load(str(img))
    pump(root, 0.3)
    canvas.zoom("in")
    manual = canvas.state.scale
    root.geometry("700x500")
    pump(root, 0.4)
    assert canvas.state.scale == manual


def test_viewer_keys_arrows_and_escape(root, tmp_path):
    from StoryBookManager.gui.imagetools.viewer import ImageZoomViewer

    paths = []
    for name in ("a", "b"):
        path = tmp_path / f"{name}.png"
        path.write_bytes(png(size=(800, 600)))
        paths.append(str(path))
    viewer = ImageZoomViewer(root, "1-1", [("Начало", paths[0]), ("Конец", paths[1]), ("Видео", None)], 0)
    pump(root, 0.4)
    st = viewer.canvas.state
    fit = st.scale
    for seq, check in (("<plus>", lambda: st.scale > fit), ("<minus>", lambda: st.scale == fit or st.scale < fit * 1.3),
                       ("<Key-1>", lambda: st.scale == 1.0), ("<Key-0>", lambda: st.fit_mode and st.scale == fit)):
        viewer.focus_force()
        viewer.update()
        viewer.event_generate(seq)
        root.update()
        assert check(), seq
    viewer.event_generate("<Right>")
    root.update()
    assert viewer.index == 1 and st.fit_mode
    viewer.event_generate("<Right>")
    pump(root, 0.2)
    assert viewer.index == 2 and not viewer.canvas.has_image   # у третьей нет файла: подпись вместо картинки
    assert viewer.edit_btn is None
    viewer.buttons["in"].invoke()
    viewer.event_generate("<Escape>")
    root.update()
    assert not viewer.winfo_exists()


def test_viewer_edit_button_only_for_start_end(root, tmp_path):
    from StoryBookManager.gui.imagetools.viewer import ImageZoomViewer

    path = tmp_path / "a.png"
    path.write_bytes(png())
    opened, block = [], {"text": ""}
    viewer = ImageZoomViewer(root, "1-1", [("Начало", str(path)), ("Видео", str(path))], 0,
                             on_edit=lambda part, v: opened.append(part), edit_parts=["start", None],
                             is_blocked=lambda: block["text"])
    pump(root, 0.2)
    assert str(viewer.edit_btn["state"]) == "normal"
    viewer.edit_btn.invoke()
    assert opened == ["start"]
    viewer._step(1)
    assert str(viewer.edit_btn["state"]) == "disabled"
    viewer._step(-1)
    block["text"] = "идёт прогон"
    viewer.refresh_image()
    assert str(viewer.edit_btn["state"]) == "disabled"


# --- окно правки ---

class FakeEditCtl:
    def __init__(self, rows=None):
        self.previews, self.started, self.rows = [], [], rows if rows is not None else []

    def load_references(self, exclude, ok, fail, **kw):
        ok(self.rows)

    def preview(self, target, refs, ok, fail, **kw):
        self.previews.append(list(refs))
        ok({"can_start": True, "plan_digest": "d", "problems": [{"level": "warning", "message": "предупреждение"}],
            "estimate": {"cost_rub": 0.0, "unknown_cost": True}})

    def start(self, target, prompt, refs, preview, **kw):
        self.started.append((prompt, list(refs)))
        kw["on_started"]()


def test_edit_dialog_button_prompt_refs_and_cost_text(root):
    from StoryBookManager.gui.imagetools.edit_dialog import ImageEditDialog
    from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS as IT

    rows = [{"path": f"x/{n}.png", "label": f"x/{n}.png"} for n in range(5)]
    ctl = FakeEditCtl(rows)
    dialog = ImageEditDialog(root, ctl, {"path": "p.png", "name": "p.png", "shot_key": "", "part": ""}, None,
                             on_done=lambda r: None, on_status=lambda t: None)
    pump(root, 0.2)
    assert dialog.go_btn["text"] == IT["edit_go_unknown"] and str(dialog.go_btn["state"]) == "disabled"
    assert IT["edit_cost_unknown"] in dialog.cost["text"] and "предупреждение" in dialog.problems["text"]
    dialog.prompt.insert("1.0", "abcd")
    pump(root, 0.1)
    assert str(dialog.go_btn["state"]) == "disabled"
    dialog.prompt.insert("end", "e")
    pump(root, 0.1)
    assert str(dialog.go_btn["state"]) == "normal"
    for i in range(4):
        dialog.refs.selection_set(i)
        dialog._on_refs()
    assert len(dialog.selected_paths()) == 3 and dialog.refs_info["text"].startswith("Выбрано: 3")
    assert ctl.previews[-1] == ["x/0.png", "x/1.png", "x/2.png"]
    assert dialog.focus_get() is dialog.cancel_btn            # Enter по умолчанию отменяет
    dialog.go_btn.invoke()
    assert ctl.started == [("abcde", ["x/0.png", "x/1.png", "x/2.png"])] and not dialog.winfo_exists()


# --- режим «Иллюстрации» ---

def make_files_screen(root, projects, **kw):
    from StoryBookManager.gui.screens.files_screen import FilesScreen

    make_project(projects, **kw)
    screen = FilesScreen(root, bridge_for(root))
    screen.pack(fill="both", expand=True)
    screen.load_project(SimpleNamespace(project_id=PID))
    pump(root, 0.5)
    return screen


def test_illustrations_mode_250_rows_without_widget_growth(root, projects):
    screen = make_files_screen(root, projects, pages=250)
    screen.view_var.set("images")
    screen._switch_view()
    gallery = screen.gallery
    pump(root, until=lambda: gallery.state == "ready")
    base = widget_count(root)
    leaves = [i for g in gallery.tree.get_children() for i in gallery.tree.get_children(g)]
    assert len(gallery.tree.get_children()) == 250 and len(leaves) == 250
    assert gallery.count.get() == "Иллюстраций: 250"
    gallery.query.set("img_000")
    gallery.query.set("")
    pump(root, 0.2)
    assert widget_count(root) == base
    screen.view_var.set("docs")
    screen._switch_view()
    pump(root, 0.2)
    assert screen.left.winfo_ismapped() or screen.combo.winfo_ismapped()
    assert not gallery.winfo_ismapped()


def test_illustrations_empty_and_search(root, projects):
    from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS as IT

    screen = make_files_screen(root, projects, pages=3)
    screen.view_var.set("images")
    screen._switch_view()
    gallery = screen.gallery
    pump(root, until=lambda: gallery.state == "ready")
    assert [gallery.tree.item(g, "text") for g in gallery.tree.get_children()] == ["Страница 1", "Страница 2", "Страница 3"]
    gallery.query.set("zzz")
    assert gallery.tree.get_children() == ()
    gallery.query.set("")
    first = gallery.tree.get_children(gallery.tree.get_children()[0])[0]
    gallery.tree.selection_set(first)
    pump(root, 0.4)
    assert gallery.canvas.has_image and "Размеры: 8 × 6" in gallery.info.get()
    assert gallery.info.get().startswith("Файл: img_000.png")
    # пустая папка
    for path in (projects / PID / "50_images").rglob("*.png"):
        path.unlink()
    gallery.reload()
    pump(root, until=lambda: gallery.message.get() == IT["gallery_empty"])
    assert gallery.count.get() == "" and not gallery.canvas.has_image


def test_illustration_edit_end_to_end(root, projects, monkeypatch):
    from StoryBookManager.gui.imagetools.texts import IMAGE_TEXTS as IT

    state = fake_tool(monkeypatch)
    screen = make_files_screen(root, projects, pages=2)
    screen.view_var.set("images")
    screen._switch_view()
    gallery = screen.gallery
    pump(root, until=lambda: gallery.state == "ready")
    first = gallery.tree.get_children(gallery.tree.get_children()[0])[0]
    gallery.tree.selection_set(first)
    pump(root, 0.3)
    assert str(gallery.edit_btn["state"]) == "normal"
    before = (projects / PID / first).read_bytes()
    gallery.open_edit()
    dialog = next(w for w in gallery.winfo_children() if isinstance(w, tk.Toplevel))
    pump(root, until=lambda: dialog.preview is not None)
    assert dialog.go_btn["text"] == IT["edit_go_unknown"]
    dialog.prompt.insert("1.0", PROMPT)
    pump(root, 0.1)
    dialog.go_btn.invoke()
    pump(root, until=lambda: not gallery.edit_ctl.is_running() and state["calls"] and gallery.state == "ready",
         timeout=30)
    pump(root, 0.5)
    assert (projects / PID / first).read_bytes() != before
    assert state["calls"][0]["prompt"] == f"EN:{PROMPT}"
    assert gallery.status.get() == "" and "Размеры: 16 × 12" in gallery.info.get()
    backups = list((projects / PID / ".studio" / "image_edit_backups").glob("*"))
    assert len(backups) == 1
    gallery.reload()
    pump(root, until=lambda: gallery.state == "ready")
    assert gallery.count.get() == "Иллюстраций: 2"


@pytest.mark.parametrize("size", SIZES)
def test_illustrations_screen_not_clipped(root, projects, size):
    root.geometry(f"{size[0]}x{size[1]}+0+0")
    screen = make_files_screen(root, projects, pages=12, per_page=2)
    screen.view_var.set("images")
    screen._switch_view()
    pump(root, until=lambda: screen.gallery.state == "ready")
    first = screen.gallery.tree.get_children(screen.gallery.tree.get_children()[0])[0]
    screen.gallery.tree.selection_set(first)
    pump(root, 0.6)
    assert find_clipped(root) == []


# --- запуск правки: окно не закрывается до ответа, ошибка видна ---

class PendingStartCtl(FakeEditCtl):
    """Запуск не отвечает сам: тест решает, когда придёт ответ."""

    def start(self, target, prompt, refs, preview, **kw):
        self.started.append((prompt, list(refs)))
        self.kw = kw


def _ready_dialog(root, ctl):
    from StoryBookManager.gui.imagetools.edit_dialog import ImageEditDialog

    dialog = ImageEditDialog(root, ctl, {"path": "p.png", "name": "p.png", "shot_key": "", "part": ""}, None,
                             on_done=lambda r: None, on_status=lambda t: None)
    pump(root, 0.2)
    dialog.prompt.insert("1.0", "abcde")
    pump(root, 0.1)
    return dialog


def test_edit_dialog_locked_while_starting_and_shows_start_error(root):
    ctl = PendingStartCtl()
    dialog = _ready_dialog(root, ctl)
    dialog.go_btn.invoke()
    assert str(dialog.cancel_btn["state"]) == "disabled" and str(dialog.go_btn["state"]) == "disabled"
    dialog.cancel_btn.invoke()
    dialog.event_generate("<Escape>")
    dialog.close()
    root.update()
    assert dialog.winfo_exists() and not dialog.closed
    ctl.kw["on_fail"]({"kind": "failed", "text": "не вышло"})
    assert dialog.winfo_exists() and dialog.problems["text"] == "не вышло"
    assert str(dialog.cancel_btn["state"]) == "normal"
    dialog.close()
    assert not dialog.winfo_exists()


def test_edit_dialog_closes_only_after_start_answer(root):
    ctl = PendingStartCtl()
    dialog = _ready_dialog(root, ctl)
    dialog.go_btn.invoke()
    dialog.close()
    assert dialog.winfo_exists()
    ctl.kw["on_started"]()
    assert not dialog.winfo_exists()


def test_edit_dialog_pins_project_at_open(root):
    class Ctl(FakeEditCtl):
        def __init__(self):
            super().__init__()
            self.current, self.seen = "old", []

        def get_project_id(self):
            return self.current

        def preview(self, target, refs, ok, fail, **kw):
            self.seen.append(kw.get("project_id"))
            super().preview(target, refs, ok, fail)

    ctl = Ctl()
    dialog = _ready_dialog(root, ctl)
    ctl.current = "new"
    dialog._load_preview()
    assert ctl.seen == ["old", "old"]
    dialog.close()


def test_viewer_cancels_delayed_show_on_destroy(root, tmp_path):
    from StoryBookManager.gui.imagetools.viewer import ImageZoomViewer

    path = tmp_path / "a.png"
    path.write_bytes(png())
    viewer = ImageZoomViewer(root, "1-1", [("Начало", str(path))], 0)
    assert viewer._show_after is not None
    cancelled = []
    real = viewer.after_cancel
    viewer.after_cancel = lambda i: (cancelled.append(i), real(i))
    pending = viewer._show_after
    viewer.destroy()
    assert cancelled == [pending]


# --- галерея: прогон и закрытие ---

def _gallery_ready(root, projects):
    screen = make_files_screen(root, projects, pages=2)
    screen.view_var.set("images")
    screen._switch_view()
    gallery = screen.gallery
    pump(root, until=lambda: gallery.state == "ready")
    first = gallery.tree.get_children(gallery.tree.get_children()[0])[0]
    gallery.tree.selection_set(first)
    pump(root, 0.3)
    return screen, gallery


def test_gallery_edit_blocked_while_run_is_busy(root, projects):
    screen, gallery = _gallery_ready(root, projects)
    assert str(gallery.edit_btn["state"]) == "normal"
    screen.on_status({"run": {"state": "running"}})
    assert str(gallery.edit_btn["state"]) == "disabled"
    gallery.open_edit()
    assert not [w for w in gallery.winfo_children() if isinstance(w, tk.Toplevel)]
    screen.on_status({"run": {"state": "completed"}})
    assert str(gallery.edit_btn["state"]) == "normal"


def test_gallery_shutdown_closes_edit_and_runner_and_project_change_reopens(root, projects):
    screen, gallery = _gallery_ready(root, projects)
    closed_runner = gallery.runner
    gallery.shutdown()
    assert gallery.edit_ctl.closed and closed_runner._closed
    screen.load_project(SimpleNamespace(project_id=PID))   # смена проекта: старый опрос закрыт, новый готов
    assert gallery.runner is not closed_runner and not gallery.runner._closed and not gallery.edit_ctl.closed


def test_files_screen_destroy_shuts_gallery_down(root, projects):
    screen, gallery = _gallery_ready(root, projects)
    runner = gallery.runner
    screen.destroy()
    assert runner._closed and gallery.edit_ctl.closed


def test_edit_dialog_inputs_locked_while_starting_and_restored_on_error(root):
    ctl = PendingStartCtl([{"path": f"x/{n}.png", "label": f"x/{n}.png"} for n in range(2)])
    dialog = _ready_dialog(root, ctl)
    dialog.go_btn.invoke()
    assert str(dialog.prompt["state"]) == "disabled" and str(dialog.refs["state"]) == "disabled"
    dialog.prompt.insert("end", "x")          # у отключённого поля вставка не работает
    dialog._on_refs()
    dialog._sync()
    assert not dialog.can_submit() and str(dialog.go_btn["state"]) == "disabled"
    assert dialog.prompt.get("1.0", "end-1c") == "abcde" and len(ctl.started) == 1
    ctl.kw["on_fail"]({"kind": "failed", "text": "не вышло"})
    assert str(dialog.prompt["state"]) == "normal" and str(dialog.refs["state"]) == "normal"
    dialog._sync()
    assert str(dialog.go_btn["state"]) == "normal"
    dialog.close()
