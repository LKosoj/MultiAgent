import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


tk_mock = MagicMock()
sys.modules["tkinter"] = tk_mock


class FakeToplevel:
    def __init__(self, *args, **kwargs):
        pass

    def title(self, *args, **kwargs):
        pass

    def geometry(self, *args, **kwargs):
        pass

    def transient(self, *args, **kwargs):
        pass

    def grab_set(self, *args, **kwargs):
        pass

    def destroy(self, *args, **kwargs):
        pass

    def update(self, *args, **kwargs):
        pass


class FakeFrame:
    def __init__(self, *args, **kwargs):
        pass

    def pack(self, *args, **kwargs):
        pass

    def grid(self, *args, **kwargs):
        pass


tk_mock.Toplevel = FakeToplevel

ttk_mock = MagicMock()
ttk_mock.Frame = FakeFrame
sys.modules["tkinter.ttk"] = ttk_mock
tk_mock.ttk = ttk_mock

sys.modules["tkinter.filedialog"] = MagicMock()
sys.modules["tkinter.messagebox"] = MagicMock()
tk_mock.messagebox = MagicMock()
tk_mock.filedialog = MagicMock()
tk_mock.DoubleVar = MagicMock
tk_mock.StringVar = MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "StoryBookManager"))

from StoryBookManager.config.settings import app_settings
from StoryBookManager.core.project_manager import ProjectManager
from StoryBookManager.gui.project_panel import ProjectPanel


class TestNewProjectCreation(unittest.TestCase):
    def setUp(self):
        self.test_projects_dir = project_root / "tests" / "test_projects_create"
        self.test_backup_dir = project_root / "tests" / "test_backups_create"
        self.test_projects_dir.mkdir(parents=True, exist_ok=True)
        self.test_backup_dir.mkdir(parents=True, exist_ok=True)

        self.original_projects_dir = app_settings.get("projects_directory")
        self.original_backup_dir = app_settings.get("backup_directory")

        app_settings.set("projects_directory", str(self.test_projects_dir))
        app_settings.set("backup_directory", str(self.test_backup_dir))

        self.pm = ProjectManager()
        self.pm.projects_dir = self.test_projects_dir
        self.pm.backup_dir = self.test_backup_dir

    def tearDown(self):
        app_settings.set("projects_directory", self.original_projects_dir)
        app_settings.set("backup_directory", self.original_backup_dir)

        if self.test_projects_dir.exists():
            shutil.rmtree(self.test_projects_dir)
        if self.test_backup_dir.exists():
            shutil.rmtree(self.test_backup_dir)

    def test_create_project_writes_brief_and_lists_project(self):
        project = self.pm.create_project(
            title="Лисенок и фонарь",
            description="Добрая история о лисенке, который учится не бояться темноты.",
            genre="сказка",
            target_age="3-5 лет",
            language="ru",
            pages_min=8,
            pages_max=10,
            words_per_page_min=120,
            words_per_page_max=180,
            project_id_hint="",
        )

        brief_path = self.test_projects_dir / project.project_id / "00_brief.json"
        self.assertTrue(brief_path.exists())

        with open(brief_path, "r", encoding="utf-8") as f:
            brief = json.load(f)

        self.assertEqual(brief["title"], "Лисенок и фонарь")
        self.assertEqual(brief["description"], "Добрая история о лисенке, который учится не бояться темноты.")
        self.assertEqual(brief["genre"], "сказка")
        self.assertEqual(brief["target_age"], "3-5 лет")
        self.assertEqual(brief["language"], "ru")
        self.assertIsInstance(brief["seed"], int)

        project_ids = [item.project_id for item in self.pm.list_projects()]
        self.assertIn(project.project_id, project_ids)

    def test_generate_project_id_is_unique(self):
        first = self.pm.create_project(
            title="Ночной город",
            description="Короткая история о путешествии по огням большого города.",
            genre="рассказ",
            target_age="9-12 лет",
            project_id_hint="storybook",
        )
        second = self.pm.create_project(
            title="Ночной город",
            description="Другая история про тот же город, но с новым героем.",
            genre="рассказ",
            target_age="9-12 лет",
            project_id_hint="storybook",
        )

        self.assertNotEqual(first.project_id, second.project_id)
        self.assertTrue(second.project_id.startswith("storybook"))

    def test_project_panel_cancel_does_not_create_project(self):
        with patch.object(ProjectPanel, "__init__", lambda self, parent, project_manager, on_project_selected: None):
            panel = ProjectPanel(MagicMock(), self.pm, MagicMock())
            panel.project_manager = self.pm

            with patch("StoryBookManager.gui.project_panel.NewProjectDialog") as dialog_cls:
                dialog_cls.return_value.result = None
                panel.create_new_project()

        self.assertEqual(self.pm.list_projects(), [])

    @patch("StoryBookManager.gui.project_panel.messagebox.showinfo")
    def test_project_panel_creates_project_and_refreshes_list(self, mock_showinfo):
        dialog_result = {
            "title": "Тайна маяка",
            "project_id": "",
            "description": "История о девочке, которая нашла старый маяк и помогла кораблям вернуться домой.",
            "genre": "сказка",
            "target_age": "6-8 лет",
            "pages_min": 9,
            "pages_max": 12,
            "words_per_page_min": 150,
            "words_per_page_max": 220,
            "language": "ru",
        }

        with patch.object(ProjectPanel, "__init__", lambda self, parent, project_manager, on_project_selected: None):
            panel = ProjectPanel(MagicMock(), self.pm, MagicMock())
            panel.project_manager = self.pm
            panel.refresh_projects = MagicMock()
            panel.projects_tree = MagicMock()
            panel.projects_tree.get_children.return_value = []
            panel.on_project_select = MagicMock()

            with patch("StoryBookManager.gui.project_panel.NewProjectDialog") as dialog_cls:
                dialog_cls.return_value.result = dialog_result
                panel.create_new_project()

        self.assertEqual(len(self.pm.list_projects()), 1)
        panel.refresh_projects.assert_called_once()
        mock_showinfo.assert_called_once()


if __name__ == "__main__":
    unittest.main()
