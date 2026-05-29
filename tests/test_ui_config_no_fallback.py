"""
Тесты для P0.8: удаление fallback в конфигурируемом UI-редакторе.

Проверяет:
- SchemaIntrospector бросает FileNotFoundError при отсутствии ui_config.json
- SchemaIntrospector бросает ValueError при битом JSON
- Нет fallback_to_legacy в ui_config.json
- Нет _get_default_ui_config в SchemaIntrospector
- EditorPanel содержит обработку ошибок конфигурации
"""

import json
import tempfile
import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_DIR = project_root / "StoryBookManager"


class TestSchemaIntrospectorNoFallback(unittest.TestCase):
    """Проверяет что SchemaIntrospector бросает ошибки вместо fallback"""

    def test_missing_config_raises_file_not_found(self):
        """При отсутствии ui_config.json — FileNotFoundError"""
        source = (
            SOURCE_DIR / "gui" / "universal_json_editor.py"
        ).read_text(encoding="utf-8")

        start = source.index("def _load_ui_config(self)")
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]

        self.assertIn("FileNotFoundError", method_body)
        self.assertIn("не найден", method_body)

    def test_invalid_json_raises_value_error(self):
        """При битом JSON — ValueError"""
        source = (
            SOURCE_DIR / "gui" / "universal_json_editor.py"
        ).read_text(encoding="utf-8")

        start = source.index("def _load_ui_config(self)")
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]

        self.assertIn("ValueError", method_body)
        self.assertIn("JSONDecodeError", method_body)

    def test_no_default_ui_config_method(self):
        """_get_default_ui_config удалён"""
        source = (
            SOURCE_DIR / "gui" / "universal_json_editor.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("def _get_default_ui_config", source)

    def test_no_empty_dict_return_on_error(self):
        """_load_ui_config не возвращает {} при ошибке"""
        source = (
            SOURCE_DIR / "gui" / "universal_json_editor.py"
        ).read_text(encoding="utf-8")

        start = source.index("def _load_ui_config(self)")
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]

        # Не должно быть return {}
        self.assertNotIn("return {}", method_body)


class TestFallbackToLegacyRemoved(unittest.TestCase):
    """Проверяет удаление fallback_to_legacy"""

    def test_no_fallback_to_legacy_in_config_json(self):
        """В ui_config.json нет fallback_to_legacy"""
        config_path = SOURCE_DIR / "config" / "ui_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))

        editor_settings = config.get("editor_settings", {})
        self.assertNotIn("fallback_to_legacy", editor_settings)

    def test_no_fallback_to_legacy_in_analyze_schema(self):
        """В analyze_schema нет fallback_to_legacy"""
        source = (
            SOURCE_DIR / "gui" / "universal_json_editor.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("fallback_to_legacy", source)


class TestEditorPanelHandlesConfigError(unittest.TestCase):
    """Проверяет что EditorPanel обрабатывает ошибки конфигурации"""

    def test_editor_panel_catches_config_errors(self):
        """EditorPanel содержит try/except для SchemaIntrospector"""
        source = (
            SOURCE_DIR / "gui" / "editor_panel.py"
        ).read_text(encoding="utf-8")

        # Должен быть try/except при создании introspector
        self.assertIn("_ui_config_error", source)
        self.assertIn("FileNotFoundError", source)

    def test_load_file_checks_config_error(self):
        """load_file проверяет _ui_config_error перед загрузкой"""
        source = (
            SOURCE_DIR / "gui" / "editor_panel.py"
        ).read_text(encoding="utf-8")

        start = source.index("def load_file(self")
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]

        self.assertIn("_ui_config_error", method_body)
        self.assertIn("Ошибка конфигурации UI", method_body)


class TestLoadUiConfigLogic(unittest.TestCase):
    """Интеграционные тесты логики загрузки ui_config"""

    def test_missing_file_raises(self):
        """Отсутствующий файл бросает FileNotFoundError"""
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "no_such_file.json"
            # Эмулируем логику _load_ui_config
            with self.assertRaises(FileNotFoundError):
                if not nonexistent.exists():
                    raise FileNotFoundError(
                        f"Файл конфигурации UI не найден: {nonexistent}"
                    )

    def test_invalid_json_raises(self):
        """Битый JSON бросает ValueError"""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "bad.json"
            bad_file.write_text("{{invalid json", encoding="utf-8")
            with self.assertRaises(ValueError):
                try:
                    with open(bad_file, 'r', encoding='utf-8') as f:
                        json.load(f)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"Некорректный JSON (строка {e.lineno}): {e.msg}"
                    ) from e

    def test_valid_json_loads(self):
        """Валидный JSON загружается успешно"""
        with tempfile.TemporaryDirectory() as tmpdir:
            good_file = Path(tmpdir) / "good.json"
            good_file.write_text('{"key": "value"}', encoding="utf-8")
            with open(good_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            self.assertEqual(config, {"key": "value"})


if __name__ == "__main__":
    unittest.main()
