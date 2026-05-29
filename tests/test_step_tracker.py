"""
Тесты для P1.1: виджет StepTracker.

Проверяет:
- StepTracker существует в step_tracker.py
- STATUS_ICONS содержит все необходимые статусы
- StepTracker интегрирован в create_execution_panel
- set_steps и update_step определены
- update_step использует self.after для thread-safety
"""

import ast
import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

TRACKER_PATH = project_root / "StoryBookManager" / "gui" / "step_tracker.py"
PANEL_PATH = project_root / "StoryBookManager" / "gui" / "generation_panel.py"


class TestStepTrackerExists(unittest.TestCase):
    """Проверяет наличие и структуру StepTracker"""

    def test_file_exists(self):
        self.assertTrue(TRACKER_PATH.exists(), "step_tracker.py не найден")

    def test_class_defined(self):
        source = TRACKER_PATH.read_text(encoding="utf-8")
        self.assertIn("class StepTracker", source)

    def test_status_icons_complete(self):
        source = TRACKER_PATH.read_text(encoding="utf-8")
        for status in ["pending", "running", "completed", "failed", "skipped", "cancelled"]:
            self.assertIn(f'"{status}"', source, f"Статус '{status}' отсутствует в STATUS_ICONS")

    def test_set_steps_defined(self):
        source = TRACKER_PATH.read_text(encoding="utf-8")
        self.assertIn("def set_steps(self", source)

    def test_update_step_defined(self):
        source = TRACKER_PATH.read_text(encoding="utf-8")
        self.assertIn("def update_step(self", source)

    def test_reset_defined(self):
        source = TRACKER_PATH.read_text(encoding="utf-8")
        self.assertIn("def reset(self", source)


class TestStepTrackerThreadSafety(unittest.TestCase):
    """Проверяет thread-safety через self.after"""

    def test_update_step_uses_self_after(self):
        tree = ast.parse(TRACKER_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "update_step":
                source_lines = ast.get_source_segment(
                    TRACKER_PATH.read_text(encoding="utf-8"), node
                )
                self.assertIn("self.after", source_lines)
                return
        self.fail("update_step не найден")


class TestStepTrackerIntegration(unittest.TestCase):
    """Проверяет интеграцию StepTracker в GenerationPanel"""

    def test_import_step_tracker(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("from StoryBookManager.gui.step_tracker import StepTracker", source)

    def test_step_tracker_in_create_execution_panel(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def create_execution_panel(")
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]
        self.assertIn("StepTracker", method_body)
        self.assertIn("self.step_tracker", method_body)
        self.assertIn("on_restart_requested=self._restart_pipeline_step_from_tracker", method_body)

    def test_step_tracker_set_steps_called(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def create_execution_panel(")
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]
        self.assertIn("set_steps", method_body)

    def test_refresh_updates_step_tracker(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def refresh_pipeline_steps(")
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]
        self.assertIn("step_tracker", method_body)


class TestStepTrackerDefaultState(unittest.TestCase):
    """Проверяет начальное состояние шагов"""

    def test_set_steps_uses_pending_icon(self):
        source = TRACKER_PATH.read_text(encoding="utf-8")
        start = source.index("def set_steps(self")
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]
        self.assertIn('STATUS_ICONS["pending"]', method_body)


class TestStepTrackerContextMenu(unittest.TestCase):
    """Проверяет контекстное меню перезапуска шага."""

    def _get_method_body(self, path: Path, method_name: str) -> str:
        source = path.read_text(encoding="utf-8")
        start = source.index(f"def {method_name}(")
        try:
            next_def = source.index("\n    def ", start + 1)
        except ValueError:
            next_def = len(source)
        return source[start:next_def]

    def test_context_menu_label_contains_restart_action(self):
        body = self._get_method_body(TRACKER_PATH, "_show_context_menu")
        self.assertIn("tk.Menu", body)
        self.assertIn('label=f"Перезапустить шаг {step_id}"', body)
        self.assertIn("self._restart_step", body)
        self.assertIn("tk_popup", body)
        self.assertIn("grab_release", body)

    def test_restart_step_calls_callback(self):
        body = self._get_method_body(TRACKER_PATH, "_restart_step")
        self.assertIn("if self._on_restart_requested", body)
        self.assertIn("self._on_restart_requested(step_id)", body)


class TestStepTrackerRestartIntegration(unittest.TestCase):
    """Проверяет интеграцию контекстного меню с GenerationPanel."""

    def test_generation_panel_reuses_single_step_rerun(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _restart_pipeline_step_from_tracker(")
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]

        self.assertIn("self.start_generation", method_body)
        self.assertIn("_run_single_step_thread", method_body)
        self.assertIn("Перезапуск шага", method_body)


class TestStepTrackerStyling(unittest.TestCase):
    """Проверяет визуальное выделение шагов по статусу"""

    def _get_update_step_body(self):
        source = TRACKER_PATH.read_text(encoding="utf-8")
        start = source.index("def update_step(self")
        next_def = source.index("\n    def ", start + 1)
        return source[start:next_def]

    def test_status_colors_defined(self):
        """STATUS_COLORS определён для всех статусов"""
        source = TRACKER_PATH.read_text(encoding="utf-8")
        self.assertIn("STATUS_COLORS", source)
        for status in ["pending", "running", "completed", "failed", "skipped", "cancelled"]:
            self.assertIn(f'"{status}"', source)

    def test_running_uses_bold_font(self):
        """Выполняемый шаг использует жирный шрифт"""
        source = TRACKER_PATH.read_text(encoding="utf-8")
        self.assertIn("FONT_RUNNING", source)
        self.assertIn('"bold"', source)

    def test_update_step_applies_color(self):
        """update_step применяет цвет из STATUS_COLORS"""
        body = self._get_update_step_body()
        self.assertIn("STATUS_COLORS", body)
        self.assertIn("fg=color", body)

    def test_update_step_applies_font(self):
        """update_step применяет шрифт (bold для running, normal для остальных)"""
        body = self._get_update_step_body()
        self.assertIn("FONT_RUNNING", body)
        self.assertIn("FONT_NORMAL", body)
        self.assertIn("font=font", body)

    def test_running_status_distinguishable(self):
        """running использует отличный от pending цвет"""
        source = TRACKER_PATH.read_text(encoding="utf-8")
        # Оба определены в STATUS_COLORS с разными значениями
        self.assertIn('"running"', source)
        self.assertIn('"pending"', source)
        # running и pending не могут иметь одинаковый цвет
        start = source.index("STATUS_COLORS")
        end = source.index("}", start) + 1
        colors_block = source[start:end]
        self.assertIn("#0066CC", colors_block)  # running color
        self.assertIn("#666666", colors_block)  # pending color


class TestStepTrackerDuration(unittest.TestCase):
    """Проверяет форматирование и отображение duration_sec"""

    def _get_update_step_body(self):
        source = TRACKER_PATH.read_text(encoding="utf-8")
        start = source.index("def update_step(self")
        next_def = source.index("\n    def ", start + 1)
        return source[start:next_def]

    def test_update_step_displays_duration_seconds(self):
        """duration_sec < 60 форматируется как '12.3s'"""
        body = self._get_update_step_body()
        self.assertIn("duration_sec", body)
        self.assertIn('f"{duration_sec:.1f}s"', body)

    def test_update_step_displays_duration_minutes(self):
        """duration_sec >= 60 форматируется как '2m 15s'"""
        body = self._get_update_step_body()
        self.assertIn("divmod", body)
        self.assertIn('f"{m}m {s}s"', body)

    def test_running_status_shows_dots(self):
        """status='running' показывает '...' в time_label"""
        body = self._get_update_step_body()
        self.assertIn('"running"', body)
        self.assertIn('"..."', body)

    def test_duration_none_no_update(self):
        """duration_sec=None не обновляет time_label (кроме running)"""
        body = self._get_update_step_body()
        self.assertIn("if duration_sec is not None", body)


if __name__ == "__main__":
    unittest.main()
