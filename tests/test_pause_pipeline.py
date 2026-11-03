"""
Тесты для P2.2: пауза pipeline.

Проверяет:
- pause_pipeline / resume_pipeline в PipelineRunner
- WorkflowStatus.PAUSED используется
"""

import unittest
from pathlib import Path

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

RUNNER_PATH = project_root / "StoryBookManager" / "core" / "pipeline_runner.py"


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


if __name__ == "__main__":
    unittest.main()
