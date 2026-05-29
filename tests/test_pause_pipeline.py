"""
Тесты для P2.2: пауза pipeline.

Проверяет:
- Кнопка Пауза в UI
- pause_pipeline / resume_pipeline в PipelineRunner
- WorkflowStatus.PAUSED используется
- toggle_pause в GenerationPanel
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

RUNNER_PATH = project_root / "StoryBookManager" / "core" / "pipeline_runner.py"
PANEL_PATH = project_root / "StoryBookManager" / "gui" / "generation_panel.py"


class TestPauseButtonInUI(unittest.TestCase):

    def test_pause_button_created(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("self.pause_button", source)
        self.assertIn("Пауза", source)

    def test_pause_button_in_status_frame(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def create_ui(self)")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("pause_button", body)

    def test_toggle_pause_defined(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        self.assertIn("def toggle_pause(self)", source)


class TestPausePipelineMethod(unittest.TestCase):

    def test_pause_pipeline_defined(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("async def pause_pipeline(self)", source)

    def test_resume_pipeline_defined(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("def resume_pipeline(self)", source)

    def test_pause_event_exists(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("_pause_event", source)

    def test_pause_uses_workflow_status_paused(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def pause_pipeline(self)")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("WorkflowStatus.PAUSED", body)


class TestPauseResumeLogic(unittest.TestCase):

    def test_pause_clears_event(self):
        """pause_pipeline вызывает _pause_event.clear()"""
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def pause_pipeline(self)")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_pause_event.clear()", body)

    def test_resume_sets_event(self):
        """resume_pipeline вызывает _pause_event.set()"""
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def resume_pipeline(self)")
        next_def = source.index("\n    async def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_pause_event.set()", body)

    def test_step_hook_checks_pause(self):
        """Step hook проверяет _pause_event после каждого шага"""
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("def _install_step_hook(self")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_pause_event.is_set()", body)
        self.assertIn("_pause_event.wait()", body)


class TestResumeEngineIntegration(unittest.TestCase):
    """Проверяет интеграцию resume_pipeline с engine"""

    def _get_resume_body(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def resume_pipeline(self)")
        next_def = source.index("\n    async def ", start + 1)
        return source[start:next_def]

    def test_resume_is_async(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("async def resume_pipeline(self)", source)

    def test_resume_saves_running_checkpoint(self):
        body = self._get_resume_body()
        self.assertIn("WorkflowStatus.RUNNING", body)
        self.assertIn("save_checkpoint", body)

    def test_resume_calls_in_thread_from_toggle(self):
        """toggle_pause вызывает resume_pipeline через thread"""
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def toggle_pause(self)")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_resume_in_thread", body)
        self.assertIn("run_until_complete", body)


class TestTogglePause(unittest.TestCase):

    def _get_toggle_body(self):
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def toggle_pause(self)")
        next_def = source.index("\n    def ", start + 1)
        return source[start:next_def]

    def test_toggle_calls_pause_pipeline(self):
        body = self._get_toggle_body()
        self.assertIn("pause_pipeline", body)

    def test_toggle_calls_resume_pipeline(self):
        body = self._get_toggle_body()
        self.assertIn("resume_pipeline", body)

    def test_toggle_changes_button_text(self):
        body = self._get_toggle_body()
        self.assertIn("Продолжить", body)
        self.assertIn("Пауза", body)

    def test_stop_unblocks_pause_before_cancel(self):
        """stop_generation разблокирует _pause_event перед отменой"""
        source = PANEL_PATH.read_text(encoding="utf-8")
        start = source.index("def stop_generation(self)")
        next_def = source.index("\n    def ", start + 1)
        body = source[start:next_def]
        self.assertIn("_pause_event.set()", body)


if __name__ == "__main__":
    unittest.main()
