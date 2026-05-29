"""
Тесты для P0.10: замена bare except на except Exception.

Проверяет:
- Нет bare except: в universal_json_editor.py
- Все except используют except Exception или конкретный тип
- KeyboardInterrupt не перехватывается
"""

import re
import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_PATH = project_root / "StoryBookManager" / "gui" / "universal_json_editor.py"


class TestNoBareExcept(unittest.TestCase):
    """Проверяет отсутствие bare except в universal_json_editor.py"""

    def test_no_bare_except_in_source(self):
        """Нет ни одного bare except: в файле"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        bare_except_pattern = re.compile(r'^\s+except\s*:\s*(#.*)?$', re.MULTILINE)
        lines = []
        for i, line in enumerate(source.splitlines(), 1):
            if bare_except_pattern.match(line):
                lines.append((i, line.strip()))
        msg = "\n".join(f"  строка {n}: {l}" for n, l in lines)
        self.assertEqual(lines, [], f"Bare except найден:\n{msg}")

    def test_except_exception_used(self):
        """В файле используется except Exception"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn("except Exception", source)

    def test_keyboard_interrupt_not_caught(self):
        """KeyboardInterrupt не перехватывается bare except"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        # Не должно быть except без типа (который ловит BaseException)
        bare_pattern = re.compile(r'^\s+except\s*:\s*$', re.MULTILINE)
        self.assertIsNone(
            bare_pattern.search(source),
            "Bare except: найден — это перехватывает KeyboardInterrupt"
        )

    def test_specific_exception_types_used(self):
        """Используются конкретные типы исключений где возможно"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        # Должны быть конкретные типы: JSONDecodeError, FileNotFoundError, ValueError
        has_specific = (
            "json.JSONDecodeError" in source
            or "FileNotFoundError" in source
            or "ValueError" in source
        )
        self.assertTrue(
            has_specific,
            "Ожидаются конкретные типы исключений (JSONDecodeError, FileNotFoundError, ValueError)"
        )

    def test_save_editor_uses_logger_error(self):
        """save_editor_to_items_list использует logger.error для ошибок"""
        source = SOURCE_PATH.read_text(encoding="utf-8")
        start = source.index("def save_editor_to_items_list(")
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]
        self.assertIn("logger.error", method_body)


if __name__ == "__main__":
    unittest.main()
