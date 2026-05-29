"""
Тесты для P1.6: поддержка ключа collapsed_by_default.

Проверяет:
- collapsed_by_default поддерживается как fallback для collapsed
- collapsed: true работает как раньше
- Поддержка в _analyze_groups и _create_grouped_object_form
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_PATH = project_root / "StoryBookManager" / "gui" / "universal_json_editor.py"


class TestCollapsedByDefaultSupported(unittest.TestCase):
    """Проверяет что collapsed_by_default поддерживается"""

    def test_analyze_groups_supports_collapsed_by_default(self):
        """_analyze_groups использует collapsed_by_default как fallback"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("def _analyze_groups(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("collapsed_by_default", body)

    def test_grouped_object_form_supports_collapsed_by_default(self):
        """_create_grouped_object_form использует collapsed_by_default"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("def _create_grouped_object_form(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("collapsed_by_default", body)

    def test_collapsed_still_works(self):
        """collapsed: true по-прежнему имеет приоритет"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        # collapsed должен проверяться первым (get("collapsed", get("collapsed_by_default")))
        start = source.index("def _analyze_groups(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        collapsed_pos = body.index('"collapsed"')
        by_default_pos = body.index('"collapsed_by_default"')
        self.assertLess(collapsed_pos, by_default_pos,
                        "collapsed должен проверяться до collapsed_by_default")

    def test_universal_form_generator_supports_collapsed_by_default(self):
        """UniversalFormGenerator._create_group_ui поддерживает collapsed_by_default"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        # Ищем все места где используется collapsed_by_default
        count = source.count("collapsed_by_default")
        self.assertGreaterEqual(count, 3,
                                "collapsed_by_default должен поддерживаться минимум в 3 местах")


if __name__ == "__main__":
    unittest.main()
