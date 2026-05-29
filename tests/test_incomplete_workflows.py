"""
Тесты для P3.1: обнаружение незавершённых workflow при загрузке проекта.

Проверяет:
- get_incomplete_workflows определён в PipelineRunner
- _check_incomplete_workflows вызывается в load_project
- Информация о незавершённых workflow сохраняется в _incomplete_workflows
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

RUNNER_PATH = project_root / "StoryBookManager" / "core" / "pipeline_runner.py"
PANEL_PATH = project_root / "StoryBookManager" / "gui" / "generation_panel.py"


class TestGetIncompleteWorkflows(unittest.TestCase):

    def test_method_exists(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("async def get_incomplete_workflows(self", source)

    def test_queries_sqlite(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def get_incomplete_workflows(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("workflow_checkpoints", body)
        self.assertIn("NOT IN ('completed', 'cancelled')", body)

    def test_returns_list(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def get_incomplete_workflows(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("return results", body)
        self.assertIn("return []", body)

    def test_filters_by_project_id(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def get_incomplete_workflows(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("project_id", body)


class TestCheckIncompleteWorkflowsIntegration(unittest.TestCase):

    def test_check_method_exists(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("def _check_incomplete_workflows(self", source)

    def test_called_in_load_project(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def load_project(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_check_incomplete_workflows", body)

    def test_stores_incomplete_workflows(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _check_incomplete_workflows(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_incomplete_workflows", body)

    def test_logs_warning_on_incomplete(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("незавершённый pipeline", body)
        self.assertIn("warning", body)


class TestRecoveryDialog(unittest.TestCase):
    """Проверяет UI диалога восстановления"""

    def test_show_recovery_dialog_exists(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("def _show_recovery_dialog(self", source)

    def test_dialog_shows_resume_option(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("Возобновить", body)

    def test_dialog_shows_start_over_option(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("начать сначала", body.lower())

    def test_dialog_shows_last_step(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("current_step", body)
        self.assertIn("Последний шаг", body)

    def test_dialog_shows_timestamp(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("timestamp", body)
        self.assertIn("Время", body)

    def test_check_calls_dialog(self):
        """_check_incomplete_workflows вызывает _show_recovery_dialog"""
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _check_incomplete_workflows(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_show_recovery_dialog", body)


class TestResumeFromCheckpoint(unittest.TestCase):
    """Проверяет логику восстановления pipeline с чекпоинта"""

    def test_resume_method_exists(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("def _resume_from_checkpoint(self", source)

    def test_resume_uses_run_from_step(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _resume_from_checkpoint(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_run_from_step_thread", body)

    def test_resume_determines_step_from_completed(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _resume_from_checkpoint(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("completed_steps", body)
        self.assertIn("current_step", body)

    def test_dialog_calls_resume(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _show_recovery_dialog(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_resume_from_checkpoint", body)

    def test_skipped_steps_not_restarted(self):
        """Используется run_from_step, а не run_full_pipeline"""
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _resume_from_checkpoint(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertNotIn("run_full_pipeline", body)
        self.assertIn("_run_from_step_thread", body)


if __name__ == "__main__":
    unittest.main()
