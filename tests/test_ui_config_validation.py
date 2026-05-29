"""
Тесты для P1.5: валидация виджетов, interface и layout в ui_config.json.

Проверяет:
- VALID_WIDGETS, VALID_INTERFACES, VALID_LAYOUTS определены
- _validate_ui_config проверяет все три типа полей
- Неизвестный виджет/interface/layout вызывает logger.warning
- Все значения в текущем ui_config.json валидны
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_PATH = project_root / "StoryBookManager" / "gui" / "universal_json_editor.py"
CONFIG_PATH = project_root / "StoryBookManager" / "config" / "ui_config.json"


class TestValidWidgetsDefined(unittest.TestCase):

    def test_valid_widgets_constant_exists(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn("VALID_WIDGETS", source)

    def test_valid_widgets_contains_core_types(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("VALID_WIDGETS")
        end = source.index("}", start) + 1
        block = source[start:end]
        for widget in ["entry", "text_area", "combobox", "spinbox",
                       "checkbox", "list_editor", "nested_group",
                       "universal_array_editor", "dropdown_selector"]:
            self.assertIn(f'"{widget}"', block, f"'{widget}' отсутствует в VALID_WIDGETS")


class TestValidateUiConfigMethod(unittest.TestCase):

    def test_validate_method_exists(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn("def _validate_ui_config(self", source)

    def test_called_in_init(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("def __init__(self):")
        next_def = source.index("\n    def ", start + 1)
        init_body = source[start:next_def]
        self.assertIn("_validate_ui_config", init_body)

    def test_logs_warning_for_unknown_widget(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("def _validate_ui_config(self")
        # Find the end of validation methods
        next_method = source.index("\n    def analyze_schema", start)
        validation_code = source[start:next_method]
        self.assertIn("logger.warning", validation_code)
        self.assertIn("Неизвестный виджет", validation_code)
        self.assertIn("VALID_WIDGETS", validation_code)


class TestCurrentConfigIsValid(unittest.TestCase):
    """Проверяет что текущий ui_config.json не содержит невалидных виджетов"""

    def test_all_widgets_in_config_are_valid(self):
        import json
        import re
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("VALID_WIDGETS = {")
        end = source.index("}", start) + 1
        block = source[start:end]
        valid = set(re.findall(r'"(\w+)"', block))

        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        skip_keys = {"translations", "editor_settings", "field_labels"}
        invalid = []

        for section_name, section in config.items():
            if section_name in skip_keys or not isinstance(section, dict):
                continue
            field_config = section.get("field_config", {})
            for field_name, field_cfg in field_config.items():
                if not isinstance(field_cfg, dict):
                    continue
                widget = field_cfg.get("widget")
                if widget and widget not in valid:
                    invalid.append(f"[{section_name}] {field_name}: {widget}")

        msg = "\n".join(invalid)
        self.assertEqual(invalid, [], f"Невалидные виджеты:\n{msg}")


class TestValidInterfacesDefined(unittest.TestCase):

    def test_valid_interfaces_constant_exists(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn("VALID_INTERFACES", source)

    def test_valid_interfaces_contains_core_types(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("VALID_INTERFACES")
        end = source.index("}", start) + 1
        block = source[start:end]
        for iface in ["tabs", "dropdown_selector", "list", "accordion"]:
            self.assertIn(f'"{iface}"', block)


class TestValidLayoutsDefined(unittest.TestCase):

    def test_valid_layouts_constant_exists(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn("VALID_LAYOUTS", source)

    def test_valid_layouts_contains_core_types(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("VALID_LAYOUTS")
        end = source.index("}", start) + 1
        block = source[start:end]
        for layout in ["vertical", "grid", "horizontal"]:
            self.assertIn(f'"{layout}"', block)


class TestValidationCoversAllThreeTypes(unittest.TestCase):
    """Проверяет что _validate проверяет widget, interface и layout"""

    def _get_validation_code(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("def _validate_ui_config(self")
        end = source.index("\n    def analyze_schema", start)
        return source[start:end]

    def test_validates_interface(self):
        code = self._get_validation_code()
        self.assertIn("VALID_INTERFACES", code)
        self.assertIn("Неизвестный interface", code)

    def test_validates_layout(self):
        code = self._get_validation_code()
        self.assertIn("VALID_LAYOUTS", code)
        self.assertIn("Неизвестный layout", code)

    def test_validates_widgets(self):
        code = self._get_validation_code()
        self.assertIn("VALID_WIDGETS", code)
        self.assertIn("Неизвестный виджет", code)


if __name__ == "__main__":
    unittest.main()
