import unittest
from unittest.mock import MagicMock, patch
import sys
from pathlib import Path


class FakeFrame:
    def __init__(self, *args, **kwargs):
        pass


_STUBBED_MODULES = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    "tkinter.scrolledtext",
    "PIL",
    "PIL.Image",
    "PIL.ImageTk",
]
_MISSING_MODULE = object()
_saved_modules = {name: sys.modules.get(name, _MISSING_MODULE) for name in _STUBBED_MODULES}
_saved_sys_path = list(sys.path)

try:
    project_root = Path(__file__).parent.parent
    for path in (str(project_root), str(project_root / "StoryBookManager")):
        if path not in sys.path:
            sys.path.insert(0, path)

    tk_mock = MagicMock()
    ttk_mock = MagicMock()
    ttk_mock.Frame = FakeFrame
    tk_mock.ttk = ttk_mock
    tk_mock.Frame = FakeFrame
    tk_mock.messagebox = MagicMock()
    tk_mock.scrolledtext = MagicMock()
    tk_mock.StringVar = MagicMock
    sys.modules["tkinter"] = tk_mock
    sys.modules["tkinter.ttk"] = ttk_mock
    sys.modules["tkinter.messagebox"] = tk_mock.messagebox
    sys.modules["tkinter.scrolledtext"] = tk_mock.scrolledtext
    try:
        import PIL.Image  # noqa: F401
    except Exception:
        sys.modules.setdefault("PIL", MagicMock())
        sys.modules.setdefault("PIL.Image", MagicMock())
    sys.modules["PIL.ImageTk"] = MagicMock()

    from StoryBookManager.gui.editor_panel import EditorPanel
finally:
    sys.path[:] = _saved_sys_path
    for name, module in _saved_modules.items():
        if module is _MISSING_MODULE:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class TestEditorPanelFiles(unittest.TestCase):
    def setUp(self):
        self.panel = MagicMock()
        self.panel.file_combo = {}
        self.panel.file_var = MagicMock()
        self.panel.file_var.get.return_value = ''

    @patch('os.path.exists')
    def test_only_existing_files_shown(self, mock_exists):
        """Показываются только файлы, которые реально существуют на диске"""
        self.panel.current_project = MagicMock()
        self.panel.current_project.project_id = "test_proj"

        self.panel.file_manager = MagicMock()
        self.panel.project_manager = MagicMock()
        fake_files = {
            "brief": "/fake/brief.json",
            "style_text": "/fake/style_text.json",
            "consistency_rules": "/fake/consistency_rules.json",
        }
        self.panel.project_manager.get_project_files.return_value = fake_files

        def fake_exists(path):
            return "brief" in path or "consistency_rules" in path
        mock_exists.side_effect = fake_exists

        bound_method = EditorPanel.update_file_list.__get__(self.panel, type(self.panel))
        bound_method()

        values = self.panel.file_combo['values']

        self.assertTrue(any("brief" in v for v in values))
        self.assertTrue(any("consistency_rules" in v for v in values))
        self.assertFalse(any("style_text" in v for v in values),
                         "style_text should not appear if file doesn't exist")

    @patch('os.path.exists')
    def test_new_project_shows_only_brief(self, mock_exists):
        """Для нового проекта (только 00_brief.json) показывается только brief"""
        self.panel.current_project = MagicMock()
        self.panel.current_project.project_id = "new_proj"

        self.panel.file_manager = MagicMock()
        self.panel.project_manager = MagicMock()
        fake_files = {
            "brief": "/fake/00_brief.json",
            "synopsis": "/fake/10_synopsis/synopsis.json",
            "story": "/fake/20_story/story.json",
            "characters": "/fake/20_bible/characters.json",
        }
        self.panel.project_manager.get_project_files.return_value = fake_files

        def fake_exists(path):
            return "00_brief" in path
        mock_exists.side_effect = fake_exists

        bound_method = EditorPanel.update_file_list.__get__(self.panel, type(self.panel))
        bound_method()

        values = self.panel.file_combo['values']
        self.assertEqual(len(values), 1)
        self.assertIn("brief", values[0])


if __name__ == "__main__":
    unittest.main()
