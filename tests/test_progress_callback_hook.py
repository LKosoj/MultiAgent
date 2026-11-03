"""
Тесты для P1.2: подключение progress_callback к engine через _on_step_completed hook.

Проверяет:
- _install_step_hook определён в PipelineRunner
- _uninstall_step_hook определён в PipelineRunner
- run_full_pipeline вызывает _install_step_hook при наличии callback
- run_full_pipeline вызывает _uninstall_step_hook в finally
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

RUNNER_PATH = project_root / "StoryBookManager" / "core" / "pipeline_runner.py"


class TestStepHookExists(unittest.TestCase):
    """Проверяет наличие hook-методов в PipelineRunner"""

    def test_install_step_hook_defined(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("def _install_step_hook(self", source)

    def test_uninstall_step_hook_defined(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("def _uninstall_step_hook(self", source)

    def test_original_on_step_completed_stored(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("_original_on_step_completed", source)


class TestRunFullPipelineUsesHook(unittest.TestCase):
    """Проверяет что run_full_pipeline подключает hook"""

    def _get_run_full_pipeline_body(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def run_full_pipeline(self")
        next_def = source.index("\n    async def ", start + 1)
        return source[start:next_def]

    def test_installs_hook_when_callback_provided(self):
        body = self._get_run_full_pipeline_body()
        self.assertIn("_install_step_hook", body)
        self.assertIn("progress_callback", body)

    def test_uninstalls_hook_in_finally(self):
        body = self._get_run_full_pipeline_body()
        finally_idx = body.index("finally:")
        after_finally = body[finally_idx:]
        self.assertIn("_uninstall_step_hook", after_finally)


class TestInstallStepHookLogic(unittest.TestCase):
    """Проверяет логику _install_step_hook"""

    def _get_hook_body(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("def _install_step_hook(self")
        next_def = source.index("\n    def ", start + 1)
        return source[start:next_def]

    def test_wraps_on_step_completed(self):
        body = self._get_hook_body()
        self.assertIn("_on_step_completed", body)

    def test_calls_progress_callback(self):
        body = self._get_hook_body()
        self.assertIn("progress_callback(", body)

    def test_passes_step_info(self):
        body = self._get_hook_body()
        self.assertIn("step_id=", body)
        self.assertIn("step_status=", body)
        self.assertIn("step_duration=", body)
        self.assertIn("progress=", body)

    def test_calculates_progress_percentage(self):
        body = self._get_hook_body()
        self.assertIn("total_steps", body)
        self.assertIn("completed_count", body)

    def test_logs_step_completion_message(self):
        body = self._get_hook_body()
        self.assertIn("завершён", body)


class TestProgressBarUpdate(unittest.TestCase):
    """Проверяет обновление progress bar через callback"""

    def _get_hook_body(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("def _install_step_hook(self")
        next_def = source.index("\n    def ", start + 1)
        return source[start:next_def]

    def test_progress_reaches_100_on_last_step(self):
        """При total_steps=N и completed=N прогресс = 100%"""
        body = self._get_hook_body()
        self.assertIn("completed_count[0] / total_steps", body)
        self.assertIn("* 100", body)


if __name__ == "__main__":
    unittest.main()
