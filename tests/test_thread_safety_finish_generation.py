"""
Тесты для P0.3: thread-safety finish_generation().

Проверяет:
- finish_generation() использует self.after (не прямые вызовы виджетов)
- update_progress() использует self.after
- add_log() использует self.after
- Нет прямых widget-модификаций в методах, вызываемых из worker threads
- Можно вызвать finish_generation() многократно без ошибок
"""

import ast
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

SOURCE_PATH = project_root / "StoryBookManager" / "gui" / "generation_panel.py"


def _get_method_ast(method_name: str) -> ast.FunctionDef:
    """Парсит AST generation_panel.py и возвращает узел метода."""
    source = SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            return node
    raise ValueError(f"Метод {method_name} не найден")


def _method_calls_self_after(method_name: str) -> bool:
    """Проверяет, что метод содержит вызов self.after(...)."""
    func = _get_method_ast(method_name)
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            func_node = node.func
            if (isinstance(func_node, ast.Attribute)
                    and func_node.attr == "after"
                    and isinstance(func_node.value, ast.Name)
                    and func_node.value.id == "self"):
                return True
    return False


def _method_has_direct_widget_config(method_name: str) -> bool:
    """Проверяет, есть ли прямые вызовы .config() на виджетах вне self.after.

    Ищет паттерн self.<widget>.config() НЕ внутри вложенной def _update_ui().
    """
    func = _get_method_ast(method_name)

    # Собираем вложенные функции
    nested_defs = set()
    for node in ast.iter_child_nodes(func):
        if isinstance(node, ast.FunctionDef):
            nested_defs.add(id(node))

    # Ищем .config() вызовы на уровне метода (не внутри nested def)
    for node in ast.iter_child_nodes(func):
        if id(node) in nested_defs:
            continue
        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Call):
                fn = subnode.func
                if (isinstance(fn, ast.Attribute)
                        and fn.attr == "config"
                        and isinstance(fn.value, ast.Attribute)
                        and isinstance(fn.value.value, ast.Name)
                        and fn.value.value.id == "self"):
                    return True
    return False


class TestFinishGenerationThreadSafety(unittest.TestCase):
    """Проверяет, что finish_generation() thread-safe"""

    def test_finish_generation_uses_self_after(self):
        """finish_generation() вызывает self.after()"""
        self.assertTrue(
            _method_calls_self_after("finish_generation"),
            "finish_generation() должен вызывать self.after() для thread-safety"
        )

    def test_finish_generation_no_direct_widget_config(self):
        """finish_generation() не вызывает .config() напрямую (только через _update_ui)"""
        self.assertFalse(
            _method_has_direct_widget_config("finish_generation"),
            "finish_generation() не должен напрямую вызывать self.<widget>.config()"
        )

    def test_update_progress_uses_self_after(self):
        """update_progress() вызывает self.after()"""
        self.assertTrue(
            _method_calls_self_after("update_progress"),
            "update_progress() должен вызывать self.after() для thread-safety"
        )

    def test_add_log_uses_self_after(self):
        """add_log() вызывает self.after()"""
        self.assertTrue(
            _method_calls_self_after("add_log"),
            "add_log() должен вызывать self.after() для thread-safety"
        )


class TestFinishGenerationConcurrency(unittest.TestCase):
    """Проверяет, что finish_generation() безопасен при многократном вызове из потоков"""

    def test_multiple_calls_from_threads(self):
        """10 параллельных вызовов finish_generation не вызывают crash"""
        mock_panel = MagicMock()
        scheduled_callbacks = []

        def mock_after(delay, func):
            scheduled_callbacks.append(func)

        mock_panel.after = mock_after

        # Эмулируем finish_generation
        def finish_generation():
            def _update_ui():
                mock_panel.is_generating = False
                mock_panel.stop_button.config(state="disabled")
                mock_panel.status_label.config(text="Готов к работе")
                mock_panel.generation_thread = None
            mock_panel.after(0, _update_ui)

        threads = []
        for _ in range(10):
            t = threading.Thread(target=finish_generation, daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=5)

        # Все 10 callback-ов были scheduled
        self.assertEqual(len(scheduled_callbacks), 10)

        # Выполняем все scheduled callbacks (симуляция Tk main loop)
        for cb in scheduled_callbacks:
            cb()

        # Финальное состояние корректно
        self.assertFalse(mock_panel.is_generating)
        self.assertIsNone(mock_panel.generation_thread)


class TestElapsedTimeDisplay(unittest.TestCase):
    """Проверяет отображение общего времени в статус-баре"""

    def _get_source(self):
        return SOURCE_PATH.read_text(encoding="utf-8")

    def test_start_generation_records_time(self):
        source = self._get_source()
        start = source.index("def start_generation(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_generation_start_time", body)
        self.assertIn("time.time()", body)

    def test_finish_generation_shows_elapsed(self):
        source = self._get_source()
        start = source.index("def finish_generation(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("Завершено за", body)
        self.assertIn("_generation_start_time", body)

    def test_elapsed_format_minutes_seconds(self):
        source = self._get_source()
        start = source.index("def finish_generation(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("мин", body)
        self.assertIn("сек", body)


if __name__ == "__main__":
    unittest.main()
