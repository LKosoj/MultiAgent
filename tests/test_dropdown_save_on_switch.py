"""
Тесты для P0.9: сохранение данных при переключении элементов в dropdown-selector.

Проверяет:
- on_selection_change вызывает _save_current_item перед переключением
- _save_current_item определён в обоих dropdown-селекторах
- get_form_data / get_data вызывает _save_current_item
- add_item вызывает _save_current_item перед добавлением
- Логика сохранения вынесена в WidgetFactory.save_editor_to_items_list (без дублирования)
- Merge с оригиналом сохраняет hidden-поля
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_PATH = project_root / "StoryBookManager" / "gui" / "universal_json_editor.py"


def _get_method_source(method_name):
    """Извлекает исходный код метода из universal_json_editor.py"""
    source = SOURCE_PATH.read_text(encoding="utf-8")
    start = source.index(f"def {method_name}(self")
    next_def_pos = len(source)
    search_start = start + 1
    for marker in ["\n    def ", "\nclass "]:
        try:
            pos = source.index(marker, search_start)
            if pos < next_def_pos:
                next_def_pos = pos
        except ValueError:
            pass
    return source[start:next_def_pos]


class TestSaveCurrentItemExists(unittest.TestCase):
    """Проверяет что _save_current_item определён в обоих dropdown-селекторах"""

    def test_save_current_item_in_dropdown_selector_with_subfields(self):
        body = _get_method_source("_create_dropdown_selector_with_subfields")
        self.assertIn("def _save_current_item():", body)

    def test_save_current_item_in_dropdown_selector(self):
        body = _get_method_source("_create_dropdown_selector")
        self.assertIn("def _save_current_item():", body)


class TestOnSelectionChangeSaves(unittest.TestCase):
    """Проверяет что on_selection_change вызывает _save_current_item"""

    def _extract_on_selection_change(self, method_name):
        body = _get_method_source(method_name)
        start = body.index("def on_selection_change(")
        next_def = body.index("\n        def ", start + 1)
        return body[start:next_def]

    def test_on_selection_saves_in_dropdown_with_subfields(self):
        func_body = self._extract_on_selection_change(
            "_create_dropdown_selector_with_subfields"
        )
        self.assertIn("_save_current_item()", func_body)

    def test_on_selection_saves_in_dropdown_selector(self):
        func_body = self._extract_on_selection_change(
            "_create_dropdown_selector"
        )
        self.assertIn("_save_current_item()", func_body)


class TestGetFormDataSaves(unittest.TestCase):
    """Проверяет что get_form_data/get_data вызывает _save_current_item"""

    def test_get_form_data_calls_save(self):
        body = _get_method_source("_create_dropdown_selector_with_subfields")
        start = body.index("def get_form_data():")
        next_def = body.index("\n        def ", start + 1)
        func_body = body[start:next_def]
        self.assertIn("_save_current_item()", func_body)

    def test_get_data_calls_save(self):
        body = _get_method_source("_create_dropdown_selector")
        start = body.index("def get_data():")
        next_def = body.index("\n", body.index("return items_list", start))
        func_body = body[start:next_def]
        self.assertIn("_save_current_item()", func_body)


class TestAddItemSaves(unittest.TestCase):
    """Проверяет что add_item вызывает _save_current_item перед добавлением"""

    def test_add_item_saves_in_dropdown_with_subfields(self):
        body = _get_method_source("_create_dropdown_selector_with_subfields")
        start = body.index("def add_item():")
        next_def = body.index("\n        def ", start + 1)
        func_body = body[start:next_def]
        self.assertIn("_save_current_item()", func_body)

    def test_add_item_saves_in_dropdown_selector(self):
        body = _get_method_source("_create_dropdown_selector")
        start = body.index("def add_item():")
        next_def = body.index("\n        def ", start + 1)
        func_body = body[start:next_def]
        self.assertIn("_save_current_item()", func_body)


class TestNoDuplication(unittest.TestCase):
    """Проверяет что логика сохранения не дублируется"""

    def test_save_logic_in_static_method(self):
        """Общая логика вынесена в WidgetFactory.save_editor_to_items_list"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn("def save_editor_to_items_list(", source)

    def test_closures_delegate_to_static_method(self):
        """Оба _save_current_item делегируют в save_editor_to_items_list"""
        body1 = _get_method_source("_create_dropdown_selector_with_subfields")
        start1 = body1.index("def _save_current_item():")
        next1 = body1.index("\n        def ", start1 + 1)
        closure1 = body1[start1:next1]

        body2 = _get_method_source("_create_dropdown_selector")
        start2 = body2.index("def _save_current_item():")
        next2 = body2.index("\n        def ", start2 + 1)
        closure2 = body2[start2:next2]

        self.assertIn("save_editor_to_items_list", closure1)
        self.assertIn("save_editor_to_items_list", closure2)

    def test_no_inline_form_data_collection(self):
        """В closures нет дублированной логики сбора form_data"""
        body1 = _get_method_source("_create_dropdown_selector_with_subfields")
        start1 = body1.index("def _save_current_item():")
        next1 = body1.index("\n        def ", start1 + 1)
        closure1 = body1[start1:next1]

        body2 = _get_method_source("_create_dropdown_selector")
        start2 = body2.index("def _save_current_item():")
        next2 = body2.index("\n        def ", start2 + 1)
        closure2 = body2[start2:next2]

        # Не должно быть inline-логики сбора данных
        self.assertNotIn("form_data", closure1)
        self.assertNotIn("form_data", closure2)


class TestSaveEditorToItemsListSource(unittest.TestCase):
    """Проверяет содержимое save_editor_to_items_list через анализ кода"""

    def _get_static_method_body(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("def save_editor_to_items_list(")
        next_def = source.index("\n    def ", start + 1)
        return source[start:next_def]

    def test_merge_preserves_hidden_fields(self):
        """Merge через {**original, **form_data}"""
        body = self._get_static_method_body()
        self.assertIn("**original", body)
        self.assertIn("**form_data", body)

    def test_handles_none_editor(self):
        """Проверяет current_editor is None"""
        body = self._get_static_method_body()
        self.assertIn("current_editor is None", body)

    def test_handles_out_of_range(self):
        """Проверяет current_index за пределами"""
        body = self._get_static_method_body()
        self.assertIn("0 <= current_index < len(items_list)", body)

    def test_exception_handling(self):
        """Обрабатывает исключения"""
        body = self._get_static_method_body()
        self.assertIn("except Exception", body)


if __name__ == "__main__":
    unittest.main()
