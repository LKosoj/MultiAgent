from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tests.workflow_test_utils import load_light_workflow_models

workflow_models = load_light_workflow_models()
WorkflowCheckpoint = workflow_models.WorkflowCheckpoint
WorkflowContext = workflow_models.WorkflowContext
WorkflowDefinition = workflow_models.WorkflowDefinition
WorkflowStatus = workflow_models.WorkflowStatus
WorkflowStep = workflow_models.WorkflowStep


class TestResumeCheckpointContext(unittest.TestCase):
    @patch("StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine")
    def test_resume_from_checkpoint_uses_saved_context_and_remaining_steps(self, _mock_init):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner()
        runner.engine = MagicMock()
        runner.engine.execute_workflow = AsyncMock(
            return_value=MagicMock(status=WorkflowStatus.COMPLETED)
        )
        runner.engine.state_manager = MagicMock()
        runner.engine.state_manager.store = MagicMock()

        checkpoint_context = WorkflowContext(
            workflow_id="wf_storybook",
            session_id="proj_storybook",
            variables={"project_id": "proj_storybook", "language": "ru"},
            step_outputs={"story_writer": {"pages": 5}},
            current_step="story_writer",
        )
        checkpoint = WorkflowCheckpoint(
            workflow_id="wf_storybook",
            timestamp=datetime.now(),
            status=WorkflowStatus.RUNNING,
            current_step="story_writer",
            completed_steps=["brief_from_prompt", "story_writer"],
            context=checkpoint_context,
            step_results={},
            resumable=False,
            metadata={},
        )
        runner.engine.state_manager.store.get_latest_checkpoint = AsyncMock(
            return_value=checkpoint
        )

        workflow_definition = WorkflowDefinition(
            name="storybook_pipeline",
            steps=[
                WorkflowStep(id="brief_from_prompt", task="brief"),
                WorkflowStep(id="story_writer", task="story"),
                WorkflowStep(id="artist_batch", task="images"),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root_path = Path(tmp_dir)
            pipeline_dir = root_path / "workflow_pipelines"
            pipeline_dir.mkdir(parents=True, exist_ok=True)
            (pipeline_dir / "storybook_pipeline.yaml").write_text("name: stub\nsteps: []\n")

            with patch("StoryBookManager.core.pipeline_runner.project_root", root_path):
                with patch(
                    "workflow.models.WorkflowDefinition.from_yaml",
                    return_value=workflow_definition,
                ):
                    result = asyncio.run(
                        runner.resume_workflow_from_checkpoint("wf_storybook")
                    )

        self.assertEqual(result["status"], "success")
        runner.engine.execute_workflow.assert_awaited_once()

        executed_definition = runner.engine.execute_workflow.await_args.args[0]
        executed_context = runner.engine.execute_workflow.await_args.args[1]

        self.assertEqual(
            [step.id for step in executed_definition.steps],
            ["artist_batch"],
        )
        self.assertEqual(executed_context.workflow_id, "wf_storybook")
        self.assertEqual(executed_context.session_id, "proj_storybook")
        self.assertEqual(
            executed_context.step_outputs["story_writer"]["pages"],
            5,
        )
