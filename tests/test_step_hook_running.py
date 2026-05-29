from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.workflow_test_utils import load_light_workflow_models

workflow_models = load_light_workflow_models()
StepStatus = workflow_models.StepStatus
WorkflowStep = workflow_models.WorkflowStep


class TestStepHookRunning(unittest.TestCase):
    @patch("StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine")
    def test_install_step_hook_reports_running_and_completed(self, _mock_init):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner()
        runner.engine = MagicMock()
        runner.engine._on_step_completed = AsyncMock(return_value=None)

        step_result = MagicMock()
        step_result.status = StepStatus.COMPLETED
        step_result.duration_seconds = 1.25
        runner.engine._execute_workflow_step = AsyncMock(return_value=step_result)

        events = []

        def progress_callback(**kwargs):
            events.append(kwargs)

        runner._install_step_hook(progress_callback, total_steps=2)

        step = WorkflowStep(id="story_writer", task="story")
        asyncio.run(runner.engine._execute_workflow_step(step, MagicMock(), MagicMock()))
        asyncio.run(
            runner.engine._on_step_completed(
                "wf1",
                step,
                step_result,
                MagicMock(),
                {},
            )
        )
        runner._uninstall_step_hook()

        self.assertEqual(events[0]["step_status"], "running")
        self.assertEqual(events[0]["step_id"], "story_writer")
        self.assertEqual(events[1]["step_status"], "completed")
        self.assertEqual(events[1]["step_duration"], 1.25)
        self.assertEqual(events[1]["progress"], 50.0)
