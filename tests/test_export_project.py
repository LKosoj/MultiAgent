import sys
import unittest
from unittest.mock import MagicMock
from pathlib import Path
import os


def _tk_scope(name):
    """Модули, которые заглушка tkinter не должна оставлять в sys.modules."""
    return (
        name == "tkinter" or name.startswith("tkinter.") or name == "_tkinter"
        or name == "StoryBookManager" or name.startswith("StoryBookManager")
    )


# снимок до подмены; на выходе из импортов возвращаем как было (см. конец блока)
_saved_modules = {n: m for n, m in sys.modules.items() if _tk_scope(n)}
_saved_path = list(sys.path)

# Mock tkinter before importing anything from GUI
tk_mock = MagicMock()
sys.modules['tkinter'] = tk_mock

class FakeToplevel:
    def __init__(self, *args, **kwargs): pass
    def title(self, *args, **kwargs): pass
    def geometry(self, *args, **kwargs): pass
    def transient(self, *args, **kwargs): pass
    def grab_set(self, *args, **kwargs): pass
    def destroy(self, *args, **kwargs): pass
    def update(self, *args, **kwargs): pass

class FakeFrame:
    def __init__(self, *args, **kwargs): pass
    def pack(self, *args, **kwargs): pass
    def grid(self, *args, **kwargs): pass

tk_mock.Toplevel = FakeToplevel

ttk_mock = MagicMock()
ttk_mock.Frame = FakeFrame
sys.modules['tkinter.ttk'] = ttk_mock
tk_mock.ttk = ttk_mock

sys.modules['tkinter.filedialog'] = MagicMock()
sys.modules['tkinter.messagebox'] = MagicMock()
tk_mock.messagebox = MagicMock()
tk_mock.filedialog = MagicMock()
tk_mock.DoubleVar = MagicMock
tk_mock.StringVar = MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "StoryBookManager"))

from StoryBookManager.core.project_manager import ProjectManager

from StoryBookManager.config.settings import app_settings

# возвращаем tkinter и StoryBookManager.* как были, чтобы заглушка не протекла в другие тесты
for _name in [n for n in sys.modules if _tk_scope(n)]:
    del sys.modules[_name]
sys.modules.update(_saved_modules)
sys.path[:] = _saved_path  # иначе StoryBookManager/utils перекрывает корневой utils

class TestExportProject(unittest.TestCase):
    def setUp(self):
        # Setup temporary directories for testing
        self.test_projects_dir = project_root / "tests" / "test_projects_export"
        self.test_projects_dir.mkdir(parents=True, exist_ok=True)
        
        self.original_projects_dir = app_settings.get("projects_directory")
        self.original_backup_dir = app_settings.get("backup_directory")
        
        app_settings.set("projects_directory", str(self.test_projects_dir))
        app_settings.set("backup_directory", str(project_root / "tests" / "test_backups_export"))
        
        self.pm = ProjectManager()
        self.pm.projects_dir = self.test_projects_dir
        self.pm.backup_dir = Path(str(project_root / "tests" / "test_backups_export"))
        
        # Create a test project
        self.project_id = "test_export_project"
        project_dir = self.test_projects_dir / self.project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        
        # Add a dummy file to zip
        with open(project_dir / "dummy.txt", "w") as f:
            f.write("test content")

    def tearDown(self):
        app_settings.set("projects_directory", self.original_projects_dir)
        app_settings.set("backup_directory", self.original_backup_dir)
        
        import shutil
        if self.test_projects_dir.exists():
            shutil.rmtree(self.test_projects_dir)
        if Path(self.pm.backup_dir).exists():
            shutil.rmtree(self.pm.backup_dir)
            
        zip_path = project_root / "tests" / f"{self.project_id}.zip"
        if zip_path.exists():
            zip_path.unlink()

    def test_export_project_core(self):
        """Проверяет, что ProjectManager.export_project создает ZIP-архив"""
        output_zip = project_root / "tests" / f"{self.project_id}.zip"
        
        # Mock progress callback
        progress_calls = []
        def progress_cb(current, total):
            progress_calls.append((current, total))
            
        success = self.pm.export_project(self.project_id, str(output_zip), progress_callback=progress_cb)
        
        self.assertTrue(success)
        self.assertTrue(output_zip.exists())
        self.assertTrue(len(progress_calls) > 0)
        
        # Verify it's a valid zip
        import zipfile
        with zipfile.ZipFile(output_zip, 'r') as zf:
            files = zf.namelist()
            self.assertTrue(any("dummy.txt" in f for f in files))

if __name__ == '__main__':
    unittest.main()
