import unittest
from pathlib import Path


project_root = Path(__file__).parent.parent
PROJECT_PANEL_PATH = project_root / "StoryBookManager" / "gui" / "project_panel.py"
MAIN_WINDOW_PATH = project_root / "StoryBookManager" / "gui" / "main_window.py"


def get_class_method_body(path: Path, class_name: str, method_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    class_start = source.index(f"class {class_name}")
    class_source = source[class_start:]
    method_start = class_source.index(f"def {method_name}(")
    try:
        method_end = class_source.index("\n    def ", method_start + 1)
    except ValueError:
        method_end = len(class_source)
    return class_source[method_start:method_end]


def get_method_body(path: Path, method_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    method_start = source.index(f"def {method_name}(")
    try:
        method_end = source.index("\n    def ", method_start + 1)
    except ValueError:
        method_end = len(source)
    return source[method_start:method_end]


class TestNewProjectDialogStructure(unittest.TestCase):
    def test_dialog_contains_required_fields_and_buttons(self):
        body = get_class_method_body(PROJECT_PANEL_PATH, "NewProjectDialog", "create_ui")

        self.assertIn('text="Название:"', body)
        self.assertIn('text="Описание:"', body)
        self.assertIn('text="Жанр:"', body)
        self.assertIn('text="Возраст:"', body)
        self.assertIn("self.title_var", body)
        self.assertIn("self.description_text", body)
        self.assertIn("self.genre_var", body)
        self.assertIn("self.target_age_var", body)
        self.assertIn('text="Создать"', body)
        self.assertIn('text="Отмена"', body)

    def test_dialog_uses_comboboxes_for_genre_and_age(self):
        body = get_class_method_body(PROJECT_PANEL_PATH, "NewProjectDialog", "create_ui")

        self.assertIn("genre_combo = ttk.Combobox", body)
        self.assertIn("target_age_combo = ttk.Combobox", body)
        self.assertIn('values=self.field_config["genre_values"]', body)
        self.assertIn('values=self.field_config["target_age_values"]', body)

    def test_dialog_loads_predefined_values_from_ui_config(self):
        body = get_class_method_body(PROJECT_PANEL_PATH, "NewProjectDialog", "_load_field_config")

        self.assertIn("ui_config.json", body)
        self.assertIn('ui_config["brief"]["field_config"]', body)
        self.assertIn('field_config["genre"]["values"]', body)
        self.assertIn('field_config["target_age"]["values"]', body)


class TestNewProjectDialogBehavior(unittest.TestCase):
    def test_create_generates_project_id_and_returns_brief_fields(self):
        body = get_class_method_body(PROJECT_PANEL_PATH, "NewProjectDialog", "create")

        self.assertIn("self.project_manager.generate_project_id", body)
        self.assertIn('"title": title', body)
        self.assertIn('"description": description', body)
        self.assertIn('"genre": genre', body)
        self.assertIn('"target_age": target_age', body)
        self.assertIn('"project_id": project_id', body)

    def test_project_panel_delegates_creation_to_project_manager(self):
        body = get_method_body(PROJECT_PANEL_PATH, "create_new_project")

        self.assertIn("dialog = NewProjectDialog(self, self.project_manager)", body)
        self.assertIn("self.project_manager.create_project(", body)
        self.assertIn('project_id_hint=dialog.result["project_id"]', body)
        self.assertIn("self.refresh_projects()", body)


class TestNewProjectDialogIntegration(unittest.TestCase):
    def test_main_window_new_project_uses_project_panel_dialog(self):
        body = get_method_body(MAIN_WINDOW_PATH, "new_project")

        self.assertIn("self.notebook.select(0)", body)
        self.assertIn("self.project_panel.create_new_project()", body)
        self.assertNotIn("будет реализована позже", body)


if __name__ == "__main__":
    unittest.main()
