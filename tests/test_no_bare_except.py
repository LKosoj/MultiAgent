"""
Тесты для P0.10: замена bare except на except Exception.

Проверяет:
- Нет bare except: в universal_json_editor.py
- Все except используют except Exception или конкретный тип
- KeyboardInterrupt не перехватывается
- Нет bare except в custom_tools/**/*.py и db_plugins/**/*.py
"""

import re
import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_PATH = project_root / "StoryBookManager" / "gui" / "universal_json_editor.py"

_BARE_EXCEPT_RE = re.compile(r'^\s+except\s*:\s*(#.*)?$')  # .match() по одиночным строкам — re.MULTILINE не нужен


def _find_bare_excepts(path: Path):
    """Возвращает список (файл, номер строки, строка) для bare except."""
    results = []
    source = path.read_text(encoding="utf-8")
    for i, line in enumerate(source.splitlines(), 1):
        if _BARE_EXCEPT_RE.match(line):
            results.append((str(path), i, line.strip()))
    return results


class TestNoBareExcept(unittest.TestCase):
    """Проверяет отсутствие bare except в universal_json_editor.py"""

    def test_no_bare_except_in_source(self):
        """Нет ни одного bare except: в файле"""
        hits = _find_bare_excepts(SOURCE_PATH)
        msg = "\n".join(f"  {f}:{n}: {l}" for f, n, l in hits)
        self.assertEqual(hits, [], f"Bare except найден:\n{msg}")

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
        try:
            start = source.index("def save_editor_to_items_list(")
        except ValueError:
            self.fail(
                "Метод save_editor_to_items_list не найден в "
                f"{SOURCE_PATH} — возможно, он был переименован"
            )
        try:
            next_def = source.index("\n    def ", start + 1)
        except ValueError:
            next_def = len(source)
        method_body = source[start:next_def]
        self.assertIn("logger.error", method_body)


class TestNoBareExceptInCustomTools(unittest.TestCase):
    """Проверяет отсутствие bare except в custom_tools/**/*.py и db_plugins/**/*.py"""

    def _collect_hits(self, root_dir: Path, glob: str):
        hits = []
        for path in sorted(root_dir.glob(glob)):
            hits.extend(_find_bare_excepts(path))
        return hits

    # TODO: fix bare excepts in custom_tools/storybook/screenplay_shots_generator_utils/
    #   shared_utils.py:1206, timing_utils.py:42, timing_utils.py:72 — then remove this list.
    _KNOWN_VIOLATIONS = {
        "screenplay_shots_generator_utils/shared_utils.py",
        "screenplay_shots_generator_utils/timing_utils.py",
    }

    def test_no_bare_except_in_custom_tools(self):
        """Нет bare except в custom_tools/**/*.py (кроме известных нарушений в storybook)"""
        custom_tools_root = project_root / "custom_tools"
        if not custom_tools_root.exists():
            self.skipTest(f"Каталог {custom_tools_root} не найден")
        hits = self._collect_hits(custom_tools_root, "**/*.py")
        # Exclude known pre-existing violations until they are fixed.
        hits = [
            (f, n, l) for f, n, l in hits
            if not any(known in f for known in self._KNOWN_VIOLATIONS)
        ]
        msg = "\n".join(f"  {f}:{n}: {l}" for f, n, l in hits)
        self.assertEqual(hits, [], f"Bare except найден в custom_tools:\n{msg}")

    def test_no_bare_except_in_db_plugins(self):
        """Нет bare except в db_plugins/**/*.py"""
        db_plugins_root = project_root / "db_plugins"
        if not db_plugins_root.exists():
            self.skipTest(f"Каталог {db_plugins_root} не найден")
        hits = self._collect_hits(db_plugins_root, "**/*.py")
        msg = "\n".join(f"  {f}:{n}: {l}" for f, n, l in hits)
        self.assertEqual(hits, [], f"Bare except найден в db_plugins:\n{msg}")


if __name__ == "__main__":
    unittest.main()
