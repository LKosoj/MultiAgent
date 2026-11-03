import contextlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "StoryBookManager"))

AppSettings = None  # заполняет фикстура setUpModule


_patcher = None


@contextlib.contextmanager
def _tk_stubbed(stubs):
    """На время блока подменяет tkinter заглушками и даёт свежий StoryBookManager.*.

    На выходе возвращает только эти модули как были: заглушки и
    StoryBookManager.*, импортированные под ними, не протекают в другие тесты
    (сторонние библиотеки, импортированные внутри, не трогаем).
    """
    def _own(name):
        return name in stubs or name == "StoryBookManager" or name.startswith("StoryBookManager.")

    saved = {n: m for n, m in sys.modules.items() if _own(n)}
    for name in saved:
        del sys.modules[name]
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for name in [n for n in sys.modules if _own(n)]:
            del sys.modules[name]
        sys.modules.update(saved)


def setUpModule():
    """Подменяет tkinter заглушками только на время модуля (см. tearDownModule)."""
    global AppSettings, _patcher
    tk_mock = MagicMock()
    ttk_mock = MagicMock()
    tk_mock.ttk = ttk_mock
    tk_mock.filedialog = MagicMock()
    tk_mock.messagebox = MagicMock()
    tk_mock.StringVar = MagicMock
    _patcher = _tk_stubbed(
        {
            "tkinter": tk_mock,
            "tkinter.ttk": ttk_mock,
            "tkinter.filedialog": tk_mock.filedialog,
            "tkinter.messagebox": tk_mock.messagebox,
        }
    )
    _patcher.__enter__()
    from StoryBookManager.config.settings import AppSettings as _AppSettings
    AppSettings = _AppSettings


def tearDownModule():
    global AppSettings
    _patcher.__exit__(None, None, None)  # возвращает tkinter и StoryBookManager.* как были
    AppSettings = None


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
        # путь должен существовать, иначе загрузка подменит его значением по умолчанию
        settings = AppSettings()
        settings.config_file = self.config_file
        settings.settings = settings.defaults.copy()

        settings.set("projects_directory", str(self.temp_dir))
        settings.set("auto_save_interval", 45)
        settings.set("max_backup_files", 7)
        settings.set("log_level", "DEBUG")

        self.assertTrue(settings.save_settings())
        self.assertTrue(self.config_file.exists())

        reloaded = AppSettings()
        reloaded.config_file = self.config_file
        reloaded.settings = reloaded.load_settings()

        self.assertEqual(reloaded.get("projects_directory"), str(self.temp_dir))
        self.assertEqual(reloaded.get("auto_save_interval"), 45)
        self.assertEqual(reloaded.get("max_backup_files"), 7)
        self.assertEqual(reloaded.get("log_level"), "DEBUG")

    def test_main_window_apply_settings_rebinds_runtime_state(self):
        body = get_method_body(MAIN_WINDOW_PATH, "apply_settings")

        env_line = 'os.environ["STORYBOOK_PROJECTS_DIR"] = str(app_settings.get_projects_directory())'
        rebind_line = "self.project_manager.projects_dir = storybook_projects_root()"
        self.assertIn(env_line, body)
        self.assertIn(rebind_line, body)
        self.assertLess(
            body.index(env_line),
            body.index(rebind_line),
            "STORYBOOK_PROJECTS_DIR должна выставляться ДО storybook_projects_root(), "
            "иначе резолвер прочитает старое значение (раздел 18.0 ТЗ)",
        )
        self.assertIn("self.project_manager.backup_dir = app_settings.get_backup_directory()", body)
        self.assertIn("logging.getLogger().setLevel", body)
        self.assertIn("self.refresh_projects()", body)
        self.assertIn('self.set_status("Настройки применены")', body)


if __name__ == "__main__":
    unittest.main()
