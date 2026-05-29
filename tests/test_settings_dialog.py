import sys
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

# Mock tkinter before importing anything from GUI or tkinter itself
tk_mock = MagicMock()
sys.modules['tkinter'] = tk_mock

# Create a FakeToplevel to act as base class
class FakeToplevel:
    def __init__(self, *args, **kwargs): pass
    def title(self, *args, **kwargs): pass
    def geometry(self, *args, **kwargs): pass
    def transient(self, *args, **kwargs): pass
    def grab_set(self, *args, **kwargs): pass

tk_mock.Toplevel = FakeToplevel

ttk_mock = MagicMock()
sys.modules['tkinter.ttk'] = ttk_mock
tk_mock.ttk = ttk_mock

sys.modules['tkinter.filedialog'] = MagicMock()
sys.modules['tkinter.messagebox'] = MagicMock()
tk_mock.messagebox = MagicMock()
tk_mock.filedialog = MagicMock()
tk_mock.StringVar = MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "StoryBookManager"))

from StoryBookManager.gui.settings_dialog import SettingsDialog
from StoryBookManager.config.settings import app_settings

class TestSettingsDialog(unittest.TestCase):
    def setUp(self):
        self.parent = MagicMock()
        
        # Reset app_settings to default for test
        app_settings.set("projects_directory", "/test/projects")
        app_settings.set("auto_save_interval", 30)
        app_settings.set("max_backup_files", 10)
        app_settings.set("log_level", "INFO")
        
        # We can't actually instantiate SettingsDialog cleanly due to tk dependencies, 
        # so we patch its init or just mock its UI parts
        with patch.object(SettingsDialog, '__init__', lambda x, y: None):
            self.dialog = SettingsDialog(self.parent)
            self.dialog.projects_dir_var = MagicMock()
            self.dialog.auto_save_var = MagicMock()
            self.dialog.max_backup_var = MagicMock()
            self.dialog.log_level_var = MagicMock()
            self.dialog.destroy = MagicMock()

    def test_load_current_settings(self):
        """Проверяет, что диалог загружает текущие настройки в StringVar"""
        self.dialog.load_current_settings()
        
        self.dialog.projects_dir_var.set.assert_called_with("/test/projects")
        self.dialog.auto_save_var.set.assert_called_with("30")
        self.dialog.max_backup_var.set.assert_called_with("10")
        self.dialog.log_level_var.set.assert_called_with("INFO")

    @patch.object(app_settings, 'save_settings', return_value=True)
    def test_save_settings_success(self, mock_save):
        """Проверяет, что сохранение настроек обновляет app_settings и вызывает save_settings"""
        self.dialog.projects_dir_var.get.return_value = "/new/projects"
        self.dialog.auto_save_var.get.return_value = "60"
        self.dialog.max_backup_var.get.return_value = "20"
        self.dialog.log_level_var.get.return_value = "DEBUG"
        
        self.dialog.save_settings()
        
        # Verify app_settings was updated
        self.assertEqual(app_settings.get("projects_directory"), "/new/projects")
        self.assertEqual(app_settings.get("auto_save_interval"), 60)
        self.assertEqual(app_settings.get("max_backup_files"), 20)
        self.assertEqual(app_settings.get("log_level"), "DEBUG")
        
        # Verify save_settings was called
        mock_save.assert_called_once()
        self.dialog.destroy.assert_called_once()

    @patch('StoryBookManager.gui.settings_dialog.messagebox.showerror')
    def test_save_settings_validation_error(self, mock_showerror):
        """Проверяет, что неверные типы данных не сохраняются"""
        self.dialog.auto_save_var.get.return_value = "not_a_number"
        self.dialog.max_backup_var.get.return_value = "10"
        
        self.dialog.save_settings()
        
        # Error should be shown and not destroyed
        mock_showerror.assert_called_once()
        self.dialog.destroy.assert_not_called()

if __name__ == '__main__':
    unittest.main()
