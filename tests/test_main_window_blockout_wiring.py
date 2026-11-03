"""Проводка оболочки в StoryBookManager/gui/main_window.py (раздел 18.4 ТЗ
docs/tz-blockout-reference-pipeline.md): регистрация болванки, закрытие окна
с отменой прогона, смена проекта, горячие клавиши.

Headless: настоящий tkinter подменяется в sys.modules лёгкими фейками.

Проверяет:
- on_project_selected() догружает blockout_panel вместе с остальными
  панелями;
- run_from_step() открывает раздел «Генерация» по id и передаёт режим
  в новый экран.
"""

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def _import_main_window():
    sys.modules.pop("StoryBookManager.gui.main_window", None)

    tk_module = types.ModuleType("tkinter")
    ttk_module = types.ModuleType("tkinter.ttk")
    messagebox_module = types.ModuleType("tkinter.messagebox")

    tk_module.Tk = MagicMock
    tk_module.Menu = MagicMock
    tk_module.StringVar = MagicMock
    tk_module.DoubleVar = MagicMock
    tk_module.WORD = "word"
    tk_module.END = "end"
    tk_module.Toplevel = MagicMock
    tk_module.ttk = ttk_module
    tk_module.messagebox = messagebox_module
    tk_module.scrolledtext = types.ModuleType("tkinter.scrolledtext")
    tk_module.scrolledtext.ScrolledText = MagicMock

    for name in ("Frame", "Label", "Button", "Notebook", "Progressbar", "Separator"):
        setattr(ttk_module, name, MagicMock)

    messagebox_module.showerror = lambda *args, **kwargs: None
    messagebox_module.showwarning = lambda *args, **kwargs: None
    messagebox_module.showinfo = lambda *args, **kwargs: None
    messagebox_module.askyesno = lambda *args, **kwargs: True

    settings_module = types.ModuleType("StoryBookManager.config.settings")
    settings_module.app_settings = MagicMock()

    def _fake_panel_module(name, class_name):
        mod = types.ModuleType(name)
        setattr(mod, class_name, MagicMock)
        return mod

    fake_modules = {
        "tkinter": tk_module,
        "tkinter.ttk": ttk_module,
        "tkinter.messagebox": messagebox_module,
        "tkinter.scrolledtext": tk_module.scrolledtext,
        "StoryBookManager.config.settings": settings_module,
        "StoryBookManager.core.project_manager": _fake_panel_module(
            "StoryBookManager.core.project_manager", "ProjectManager"
        ),
        # экран «Кадры» подключается лениво в create_panels; здесь он не нужен
        "StoryBookManager.gui.shots.adapter": _fake_panel_module(
            "StoryBookManager.gui.shots.adapter", "ShotsAdapter"
        ),
    }

    with patch.dict(sys.modules, fake_modules):
        return importlib.import_module("StoryBookManager.gui.main_window")


class TestBlockoutPanelWiring(unittest.TestCase):
    def _make_window(self, module):
        MainWindow = module.MainWindow
        win = MainWindow.__new__(MainWindow)
        win.show_section = MagicMock()
        win.topbar = MagicMock()
        win.navigation = MagicMock()
        win.current_project = types.SimpleNamespace(project_id="proj1", name="Проект 1")
        win.editor_panel = MagicMock()
        win.shots = MagicMock()
        win.generation = MagicMock()
        win.blockout_panel = MagicMock()
        win.overview = MagicMock()
        win.sections = {}
        win.poller = MagicMock()
        win.set_status = MagicMock()
        return win, MainWindow

    def _closing_window(self, module, owner="Tk"):
        win, MainWindow = self._make_window(module)
        win.root = MagicMock()
        win._run_view = types.SimpleNamespace(owner_name=owner, stop_enabled=True)
        win._shutdown_shell = MagicMock()
        win.editor_panel.has_unsaved_changes.return_value = True

        win.bridge = MagicMock()
        return win, MainWindow

    def test_close_sends_run_cancel_directly_even_if_bridge_pool_is_busy(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        module = _import_main_window()
        win, MainWindow = self._closing_window(module)
        # пул моста целиком занят: задача через него не выполнилась бы
        gate = threading.Event()
        pool = ThreadPoolExecutor(max_workers=1)
        pool.submit(gate.wait)
        calls = []
        try:
            with patch("StoryBookManager.gui.shell.dialogs.ConfirmDialog.ask", return_value=True), \
                    patch.object(module.messagebox, "askyesnocancel", create=True, return_value=False), \
                    patch("custom_tools.storybook.studio.capabilities.invoke",
                          side_effect=lambda cap, payload: calls.append((cap, payload)) or {"status": "success"}):
                MainWindow.on_closing(win)
        finally:
            gate.set()
            pool.shutdown(wait=True)
        self.assertEqual(calls, [("run_cancel", {"project_id": "proj1"})])
        win.bridge.call.assert_not_called()
        win._shutdown_shell.assert_called_once()

    def test_run_cancel_sent_even_if_closing_raises_after_confirmation(self):
        module = _import_main_window()
        win, MainWindow = self._closing_window(module)
        win.editor_panel.has_unsaved_changes.side_effect = RuntimeError("сбой")
        calls = []
        with patch("StoryBookManager.gui.shell.dialogs.ConfirmDialog.ask", return_value=True), \
                patch("custom_tools.storybook.studio.capabilities.invoke",
                      side_effect=lambda cap, payload: calls.append((cap, payload)) or {"status": "success"}):
            MainWindow.on_closing(win)
        self.assertEqual(calls, [("run_cancel", {"project_id": "proj1"})])   # ровно один раз
        win._shutdown_shell.assert_called_once()
        win.root.destroy.assert_called_once()

    def test_cancel_in_unsaved_dialog_does_not_touch_run(self):
        module = _import_main_window()
        win, MainWindow = self._closing_window(module)
        with patch("StoryBookManager.gui.shell.dialogs.ConfirmDialog.ask", return_value=True), \
                patch.object(module.messagebox, "askyesnocancel", create=True, return_value=None), \
                patch("custom_tools.storybook.studio.capabilities.invoke") as invoke:
            MainWindow.on_closing(win)
        invoke.assert_not_called()
        win._shutdown_shell.assert_not_called()

    def test_on_project_selected_loads_blockout_panel(self):
        module = _import_main_window()
        win, MainWindow = self._make_window(module)
        project = types.SimpleNamespace(project_id="proj1", name="Проект 1")

        MainWindow.on_project_selected(win, project)

        win.blockout_panel.load_project.assert_called_once_with(project)

    def test_run_from_step_opens_generation_section_by_id(self):
        module = _import_main_window()
        win, MainWindow = self._make_window(module)

        MainWindow.run_from_step(win)

        win.show_section.assert_called_once_with("generation")
        win.generation.apply_intent.assert_called_once_with(mode="from_step")

    def test_run_from_step_warns_and_skips_when_no_project(self):
        module = _import_main_window()
        win, MainWindow = self._make_window(module)
        win.current_project = None

        with patch.object(module.messagebox, "showwarning") as mock_warn:
            MainWindow.run_from_step(win)

        mock_warn.assert_called_once()
        win.show_section.assert_not_called()


class TestShellWiring(unittest.TestCase):
    """Проводка оболочки на фейках: разделы, F5, Ctrl+S, смена проекта, пауза в верхней строке."""

    def _win(self, project=None):
        module = _import_main_window()
        win = module.MainWindow.__new__(module.MainWindow)
        win.current_project = project
        win.host = types.SimpleNamespace(current="projects", show=MagicMock(
            side_effect=lambda sid: setattr(win.host, "current", sid)))
        win.navigation = MagicMock()
        win.topbar = MagicMock()
        win.overview = MagicMock()
        win.poller = MagicMock()
        win.set_status = MagicMock()
        win.editor_panel = MagicMock()
        win.shots = MagicMock()
        win.blockout_panel = MagicMock()
        win.sections = {sid: MagicMock(**{"can_leave.return_value": True})
                        for sid in ("projects", "overview", "files", "shots")}
        return module, win

    def test_section_without_project_is_refused(self):
        module, win = self._win()
        with patch.object(module.notify, "info") as info:
            self.assertFalse(module.MainWindow.show_section(win, "shots"))
        info.assert_called_once()
        win.host.show.assert_not_called()

    def test_opening_project_lands_on_overview_and_resets_topbar(self):
        module, win = self._win()
        project = types.SimpleNamespace(project_id="p2", name="Второй")
        module.MainWindow.show_section = module.MainWindow.show_section
        win.show_section = lambda sid, **kw: module.MainWindow.show_section(win, sid, **kw)
        win._update_topbar_visibility = MagicMock()
        module.MainWindow.on_project_selected(win, project)
        self.assertEqual(win.host.current, "overview")
        win.topbar.set_stage.assert_called_with("")
        win.overview.update_view.assert_called()
        win.poller.start.assert_called_once_with("p2")
        win.navigation.set_project_available.assert_called_with(True)

    def test_refresh_and_save_go_to_current_section(self):
        module, win = self._win(types.SimpleNamespace(project_id="p", name="П"))
        win.host.current = "files"
        module.MainWindow.refresh_section(win)
        win.sections["files"].on_refresh.assert_called_once()
        module.MainWindow.save_current(win)
        win.sections["files"].on_save.assert_called_once()
        win.sections["shots"].on_save.assert_not_called()

    def test_new_project_opens_projects_and_creates(self):
        module, win = self._win()
        win.show_section = MagicMock()
        win.project_panel = MagicMock()
        module.MainWindow.new_project(win)
        win.show_section.assert_called_once_with("projects")
        win.project_panel.create_new_project.assert_called_once()

    def test_unavailable_resets_overview(self):
        module, win = self._win()
        module.MainWindow._on_status_unavailable(win)
        win.topbar.show_unavailable.assert_called_once()
        win.overview.update_view.assert_called_once()

    # --- финальное ревью: Ctrl+K, Ctrl+S, закрытие, фильтр обзора ---------------------
    def test_ctrl_k_asks_current_section_before_project_change(self):
        module, win = self._win(types.SimpleNamespace(project_id="p", name="П"))
        win.host.current = "shots"
        win.sections["shots"].can_leave.return_value = False
        win.project_manager = MagicMock()
        win.on_project_selected = MagicMock()
        win._current_section_can_leave = lambda: module.MainWindow._current_section_can_leave(win)
        module.MainWindow._open_project_by_id(win, "p2")
        win.sections["shots"].can_leave.assert_called_once()
        win.on_project_selected.assert_not_called()
        win.sections["shots"].can_leave.return_value = True
        module.MainWindow._open_project_by_id(win, "p2")
        win.on_project_selected.assert_called_once()

    def test_ctrl_k_without_project_asks_settings_section(self):
        module, win = self._win()
        win.host.current = "settings"
        win.sections["settings"] = MagicMock(**{"can_leave.return_value": False})
        win.project_manager = MagicMock()
        win.on_project_selected = MagicMock()
        win._current_section_can_leave = lambda: module.MainWindow._current_section_can_leave(win)
        module.MainWindow._open_project_by_id(win, "p2")
        win.sections["settings"].can_leave.assert_called_once()
        win.on_project_selected.assert_not_called()
        win.editor_panel.can_leave.assert_not_called()

    def test_opening_project_does_not_ask_again_about_unsaved(self):
        module, win = self._win()
        win.sections["projects"].can_leave.return_value = False  # повторный вопрос сорвал бы переход
        win.show_section = lambda sid, **kw: module.MainWindow.show_section(win, sid, **kw)
        win._update_topbar_visibility = MagicMock()
        module.MainWindow.on_project_selected(win, types.SimpleNamespace(project_id="p2", name="Второй"))
        self.assertEqual(win.host.current, "overview")
        win.sections["projects"].can_leave.assert_not_called()

    def test_ctrl_s_works_in_settings_without_project(self):
        module, win = self._win()
        win.host.current = "settings"
        win.sections["settings"] = MagicMock()
        module.MainWindow.save_current(win)
        win.sections["settings"].on_save.assert_called_once()
        win.host.current = "files"
        module.MainWindow.save_current(win)
        win.sections["files"].on_save.assert_not_called()

    def test_goto_filtered_applies_intent_only_after_successful_switch(self):
        module, win = self._win()
        win.show_section = MagicMock(return_value=False)
        module.MainWindow._goto_filtered(win, "shots", "failed")
        win.shots.apply_intent.assert_not_called()
        win.show_section.return_value = True
        module.MainWindow._goto_filtered(win, "shots", "failed")
        win.shots.apply_intent.assert_called_once_with(filter="failed")

    def test_shutdown_destroys_project_panel_before_bridge_close(self):
        module, win = self._win()
        order = MagicMock()
        win.project_panel, win.bridge = order.project_panel, order.bridge
        win.movie = win.blockout_panel = win.editor_panel = win.settings_screen = MagicMock()
        win.generation = win.toasts = MagicMock()
        module.MainWindow._shutdown_shell(win)
        names = [c[0] for c in order.mock_calls]
        self.assertLess(names.index("project_panel.destroy"), names.index("bridge.close"))

    def test_closing_asks_current_section_after_run_dialog_and_stays_if_refused(self):
        module, win = self._win(types.SimpleNamespace(project_id="p", name="П"))
        win.root = MagicMock()
        win._run_view = types.SimpleNamespace(owner_name="Tk", stop_enabled=True)
        win._shutdown_shell = MagicMock()
        win.bridge = MagicMock()
        win.editor_panel.has_unsaved_changes.return_value = False
        win.host.current = "shots"
        win.sections["shots"].can_leave.return_value = False
        win._current_section_can_leave = lambda: module.MainWindow._current_section_can_leave(win)
        win._confirm_close_with_run = lambda: module.MainWindow._confirm_close_with_run(win)
        with patch("StoryBookManager.gui.shell.dialogs.ConfirmDialog.ask", return_value=True), \
                patch("custom_tools.storybook.studio.capabilities.invoke") as invoke:
            module.MainWindow.on_closing(win)
        win.sections["shots"].can_leave.assert_called_once()
        invoke.assert_not_called()
        win.root.destroy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
