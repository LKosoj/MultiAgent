import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


tk_mock = MagicMock()
sys.modules["tkinter"] = tk_mock

ttk_mock = MagicMock()
sys.modules["tkinter.ttk"] = ttk_mock
tk_mock.ttk = ttk_mock

sys.modules["tkinter.filedialog"] = MagicMock()
sys.modules["tkinter.messagebox"] = MagicMock()
tk_mock.filedialog = MagicMock()
tk_mock.messagebox = MagicMock()
tk_mock.StringVar = MagicMock


class FakeToplevel:
    def __init__(self, *args, **kwargs):
        pass


tk_mock.Toplevel = FakeToplevel

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "StoryBookManager"))

from StoryBookManager.config.settings import AppSettings
from StoryBookManager.gui.settings_dialog import SettingsDialog


MAIN_WINDOW_PATH = project_root / "StoryBookManager" / "gui" / "main_window.py"


def get_method_body(path: Path, method_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    method_start = source.index(f"def {method_name}(")
    try:
        method_end = source.index("\n    def ", method_start + 1)
    except ValueError:
        method_end = len(source)
    return source[method_start:method_end]


class TestSettingsPersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="storybook_settings_test_"))
        self.config_file = self.temp_dir / "settings.json"

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_app_settings_save_and_reload_round_trip(self):
        settings = AppSettings()
        settings.config_file = self.config_file
        settings.settings = settings.defaults.copy()

        settings.set("projects_directory", "/tmp/storybook_projects")
        settings.set("auto_save_interval", 45)
        settings.set("max_backup_files", 7)
        settings.set("log_level", "DEBUG")

        self.assertTrue(settings.save_settings())
        self.assertTrue(self.config_file.exists())

        reloaded = AppSettings()
        reloaded.config_file = self.config_file
        reloaded.settings = reloaded.load_settings()

        self.assertEqual(reloaded.get("projects_directory"), "/tmp/storybook_projects")
        self.assertEqual(reloaded.get("auto_save_interval"), 45)
        self.assertEqual(reloaded.get("max_backup_files"), 7)
        self.assertEqual(reloaded.get("log_level"), "DEBUG")

    @patch("StoryBookManager.gui.settings_dialog.messagebox.showinfo")
    def test_settings_dialog_calls_on_save_after_successful_persist(self, mock_showinfo):
        dialog = object.__new__(SettingsDialog)
        dialog.on_save = MagicMock()
        dialog.projects_dir_var = MagicMock(get=MagicMock(return_value="/tmp/storybook_projects"))
        dialog.auto_save_var = MagicMock(get=MagicMock(return_value="45"))
        dialog.max_backup_var = MagicMock(get=MagicMock(return_value="7"))
        dialog.log_level_var = MagicMock(get=MagicMock(return_value="DEBUG"))
        dialog.destroy = MagicMock()

        with patch("StoryBookManager.gui.settings_dialog.app_settings.set") as mock_set:
            with patch("StoryBookManager.gui.settings_dialog.app_settings.save_settings", return_value=True):
                dialog.save_settings()

        self.assertEqual(mock_set.call_count, 4)
        dialog.on_save.assert_called_once_with()
        dialog.destroy.assert_called_once_with()
        mock_showinfo.assert_called_once()

    def test_main_window_apply_settings_rebinds_runtime_state(self):
        body = get_method_body(MAIN_WINDOW_PATH, "apply_settings")

        self.assertIn("self.project_manager.projects_dir = app_settings.get_projects_directory()", body)
        self.assertIn("self.project_manager.backup_dir = app_settings.get_backup_directory()", body)
        self.assertIn("logging.getLogger().setLevel", body)
        self.assertIn("self.refresh_projects()", body)
        self.assertIn('self.set_status("Настройки применены")', body)


if __name__ == "__main__":
    unittest.main()
