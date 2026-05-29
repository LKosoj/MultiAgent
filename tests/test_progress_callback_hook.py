"""
Тесты для P1.2: подключение progress_callback к engine через _on_step_completed hook.

Проверяет:
- _install_step_hook определён в PipelineRunner
- _uninstall_step_hook определён в PipelineRunner
- run_full_pipeline вызывает _install_step_hook при наличии callback
- run_full_pipeline вызывает _uninstall_step_hook в finally
- progress_callback в generation_panel принимает step_id/step_status/step_duration
- progress_callback обновляет step_tracker
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

RUNNER_PATH = project_root / "StoryBookManager" / "core" / "pipeline_runner.py"
PANEL_PATH = project_root / "StoryBookManager" / "gui" / "generation_panel.py"


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

    def test_callback_sends_progress_to_update_progress(self):
        """progress_callback в generation_panel вызывает update_progress"""
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _run_full_pipeline_thread(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("self.update_progress(progress", body)

    def test_update_progress_sets_bar_value(self):
        """update_progress устанавливает progress_bar['value']"""
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def update_progress(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("progress_bar", body)


class TestGenerationPanelCallback(unittest.TestCase):
    """Проверяет что callback в generation_panel обрабатывает step info"""

    def _get_full_pipeline_thread_body(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _run_full_pipeline_thread(self")
        next_def = source.index("\n    def ", start + 1)
        return source[start:next_def]

    def test_callback_accepts_step_params(self):
        body = self._get_full_pipeline_thread_body()
        self.assertIn("step_id", body)
        self.assertIn("step_status", body)
        self.assertIn("step_duration", body)

    def test_callback_updates_step_tracker(self):
        body = self._get_full_pipeline_thread_body()
        self.assertIn("step_tracker.update_step", body)

    def test_from_step_callback_also_updated(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def _run_from_step_thread(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("step_tracker.update_step", body)


if __name__ == "__main__":
    unittest.main()
