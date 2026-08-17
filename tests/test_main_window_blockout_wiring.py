"""Раздел 18.4 ТЗ (docs/tz-blockout-reference-pipeline.md): регистрация
вкладки «Болванка» в StoryBookManager/gui/main_window.py.

Headless — tkinter не установлен в этом окружении (образец подмены
sys.modules["tkinter"]: tests/test_generation_panel_pipeline_config.py).
main_window.py импортирует остальные панели напрямую по имени, поэтому они
тоже подменяются лёгкими фейковыми модулями — реальные project_panel.py и
т.п. не участвуют.

Проверяет:
- on_project_selected() догружает blockout_panel вместе с остальными
  панелями;
- on_generation_complete() обновляет blockout_panel ДО проверки
  несохранённых изменений в редакторе (иначе «Нет» в диалоге о
  несохранённых изменениях в редакторе обрезал бы обновление вкладки
  «Болванка» тоже);
- run_from_step() переключает вкладки по ссылке на виджет
  (self.generation_panel), а не по магическому индексу (критерий A19 —
  ручная проверка по ТЗ, но регрессия неверного индекса ловится и здесь).
"""

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class FakeNotebook:
    """Записывает вызовы select() — чтобы отличить select(3) (магический
    индекс) от select(виджет)."""

    def __init__(self):
        self.select_calls = []
        self.tabs = []

    def add(self, widget, **kwargs):
        self.tabs.append(widget)

    def select(self, *args):
        self.select_calls.append(args[0] if args else None)

    def index(self, _end):
        return len(self.tabs)

    def bind(self, *args, **kwargs):
        return None

    def tab(self, *args, **kwargs):
        return None


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
        "StoryBookManager.gui.project_panel": _fake_panel_module(
            "StoryBookManager.gui.project_panel", "ProjectPanel"
        ),
        "StoryBookManager.gui.editor_panel": _fake_panel_module(
            "StoryBookManager.gui.editor_panel", "EditorPanel"
        ),
        "StoryBookManager.gui.media_panel": _fake_panel_module(
            "StoryBookManager.gui.media_panel", "MediaPanel"
        ),
        "StoryBookManager.gui.generation_panel": _fake_panel_module(
            "StoryBookManager.gui.generation_panel", "GenerationPanel"
        ),
        "StoryBookManager.gui.blockout_panel": _fake_panel_module(
            "StoryBookManager.gui.blockout_panel", "BlockoutPanel"
        ),
        "StoryBookManager.gui.settings_dialog": _fake_panel_module(
            "StoryBookManager.gui.settings_dialog", "SettingsDialog"
        ),
    }

    with patch.dict(sys.modules, fake_modules):
        return importlib.import_module("StoryBookManager.gui.main_window")


class TestBlockoutPanelWiring(unittest.TestCase):
    def _make_window(self, module):
        MainWindow = module.MainWindow
        win = MainWindow.__new__(MainWindow)
        win.notebook = FakeNotebook()
        win.current_project = types.SimpleNamespace(project_id="proj1", name="Проект 1")
        win.project_info_label = MagicMock()
        win.editor_panel = MagicMock()
        win.media_panel = MagicMock()
        win.generation_panel = MagicMock()
        win.blockout_panel = MagicMock()
        win.set_status = MagicMock()
        return win, MainWindow

    def test_on_project_selected_loads_blockout_panel(self):
        module = _import_main_window()
        win, MainWindow = self._make_window(module)
        project = types.SimpleNamespace(project_id="proj1", name="Проект 1")

        MainWindow.on_project_selected(win, project)

        win.blockout_panel.load_project.assert_called_once_with(project)

    def test_on_generation_complete_refreshes_blockout_panel_before_unsaved_changes_check(self):
        module = _import_main_window()
        win, MainWindow = self._make_window(module)

        call_order = []
        win.media_panel.refresh_media = MagicMock(side_effect=lambda: call_order.append("media"))
        win.blockout_panel.refresh = MagicMock(side_effect=lambda: call_order.append("blockout"))
        win.editor_panel.has_unsaved_changes = MagicMock(
            side_effect=lambda: (call_order.append("unsaved_check"), False)[1]
        )
        win.editor_panel.load_project = MagicMock()

        MainWindow.on_generation_complete(win)

        self.assertEqual(call_order, ["media", "blockout", "unsaved_check"])
        win.blockout_panel.refresh.assert_called_once()

    def test_on_generation_complete_refreshes_blockout_panel_even_if_editor_reload_declined(self):
        """Раздел 18.4: обновление вкладки «Болванка» не должно зависеть от
        ответа человека в диалоге «есть несохранённые изменения» редактора —
        оно вызывается раньше этого диалога."""
        module = _import_main_window()
        win, MainWindow = self._make_window(module)
        win.media_panel.refresh_media = MagicMock()
        win.blockout_panel.refresh = MagicMock()
        win.editor_panel.has_unsaved_changes = MagicMock(return_value=True)
        win.editor_panel.load_project = MagicMock()

        with patch.object(module.messagebox, "askyesno", return_value=False):
            MainWindow.on_generation_complete(win)

        win.blockout_panel.refresh.assert_called_once()
        win.editor_panel.load_project.assert_not_called()

    def test_run_from_step_selects_generation_panel_by_widget_not_magic_index(self):
        module = _import_main_window()
        win, MainWindow = self._make_window(module)

        MainWindow.run_from_step(win)

        self.assertEqual(win.notebook.select_calls, [win.generation_panel])
        win.generation_panel.run_from_step.assert_called_once()

    def test_run_from_step_warns_and_skips_when_no_project(self):
        module = _import_main_window()
        win, MainWindow = self._make_window(module)
        win.current_project = None

        with patch.object(module.messagebox, "showwarning") as mock_warn:
            MainWindow.run_from_step(win)

        mock_warn.assert_called_once()
        self.assertEqual(win.notebook.select_calls, [])


if __name__ == "__main__":
    unittest.main()
