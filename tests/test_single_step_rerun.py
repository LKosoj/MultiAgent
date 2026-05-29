"""
Тесты для P2.3: перезапуск одного конкретного шага.

Проверяет:
- run_single_step исполняет только выбранный шаг
- depends_on/condition не тянут остальные шаги при explicit rerun
- context.step_outputs и variables от других шагов сохраняются
- rerun_single_step требует сохранённый checkpoint, а не использует fallback
"""

import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@dataclass
class WorkflowContext:
    workflow_id: str = None
    session_id: str = None
    client_id: str = None
    variables: dict = field(default_factory=dict)
    step_outputs: dict = field(default_factory=dict)
    current_step: str = None
    metadata: dict = field(default_factory=dict)


def _make_runner_with_mock_engine():
    with patch(
        "StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"
    ):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner()
        runner.engine = MagicMock()
        runner.engine.execute_workflow = AsyncMock(return_value=MagicMock())
        runner.engine.state_manager = MagicMock()
        runner.engine.state_manager.store = MagicMock()
        return runner


def _make_mock_workflow_def(step_ids):
    mock_def = MagicMock()
    steps = []
    for sid in step_ids:
        step = MagicMock()
        step.id = sid
        step.condition = "some_condition" if sid == "artist_batch" else None
        step.depends_on = ["items_builder"] if sid == "artist_batch" else []
        steps.append(step)
    mock_def.steps = steps
    mock_def.inputs = {"language": "ru", "generate_screenplay": True}
    return mock_def


class TestSingleStepRerun(unittest.TestCase):
    PIPELINE_STEP_IDS = [
        "brief_from_prompt",
        "init_project",
        "items_builder",
        "artist_batch",
        "assemble_md",
    ]

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_run_single_step_executes_only_selected_step(self):
        runner = _make_runner_with_mock_engine()
        mock_def = _make_mock_workflow_def(self.PIPELINE_STEP_IDS)
        base_context = WorkflowContext(
            workflow_id="wf_prev",
            session_id="proj1",
            variables={"project_id": "proj1", "task": "storybook task"},
            step_outputs={"items_builder": [{"page": 1}]},
        )

        mock_wf_def_cls = MagicMock(from_yaml=MagicMock(return_value=mock_def))
        with patch.dict(
            "sys.modules",
            {"workflow.models": MagicMock(WorkflowDefinition=mock_wf_def_cls, WorkflowContext=WorkflowContext)},
        ):
            result = asyncio.get_event_loop().run_until_complete(
                runner.run_single_step(
                    "proj1",
                    "artist_batch",
                    base_context=base_context,
                )
            )

        self.assertEqual(result["status"], "success")
        call_args = runner.engine.execute_workflow.call_args
        passed_def = call_args[0][0]
        self.assertEqual([step.id for step in passed_def.steps], ["artist_batch"])
        self.assertEqual(passed_def.steps[0].depends_on, [])
        self.assertIsNone(passed_def.steps[0].condition)

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_run_single_step_preserves_context_from_other_steps(self):
        runner = _make_runner_with_mock_engine()
        mock_def = _make_mock_workflow_def(self.PIPELINE_STEP_IDS)
        base_context = WorkflowContext(
            workflow_id="wf_prev",
            session_id="proj1",
            variables={
                "project_id": "proj1",
                "task": "storybook task",
                "language": "ru",
                "pages_min": 8,
            },
            step_outputs={
                "brief_from_prompt": {"title": "Кот-рыцарь"},
                "brief_from_prompt.title": "Кот-рыцарь",
                "items_builder": [{"page": 1, "prompt": "draw"}],
            },
            metadata={"previous_run": True},
        )

        mock_wf_def_cls = MagicMock(from_yaml=MagicMock(return_value=mock_def))
        with patch.dict(
            "sys.modules",
            {"workflow.models": MagicMock(WorkflowDefinition=mock_wf_def_cls, WorkflowContext=WorkflowContext)},
        ):
            result = asyncio.get_event_loop().run_until_complete(
                runner.run_single_step(
                    "proj1",
                    "artist_batch",
                    task="updated task",
                    input_overrides={"language": "en"},
                    base_context=base_context,
                    source_workflow_id="wf_prev",
                )
            )

        self.assertEqual(result["status"], "success")
        _, passed_context = runner.engine.execute_workflow.call_args[0]
        self.assertEqual(passed_context.step_outputs["items_builder"][0]["page"], 1)
        self.assertEqual(passed_context.step_outputs["brief_from_prompt.title"], "Кот-рыцарь")
        self.assertEqual(passed_context.variables["language"], "en")
        self.assertEqual(passed_context.variables["task"], "updated task")
        self.assertEqual(passed_context.metadata["source_workflow_id"], "wf_prev")
        self.assertEqual(passed_context.metadata["single_step_rerun"], "artist_batch")

    def test_rerun_single_step_requires_checkpoint(self):
        runner = _make_runner_with_mock_engine()
        runner._get_latest_project_checkpoint = AsyncMock(return_value=None)

        result = asyncio.get_event_loop().run_until_complete(
            runner.rerun_single_step("proj1", "artist_batch")
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("checkpoint", result["message"])
        runner.engine.execute_workflow.assert_not_awaited()

    def test_rerun_single_step_uses_latest_checkpoint_context(self):
        runner = _make_runner_with_mock_engine()
        checkpoint = MagicMock()
        checkpoint.workflow_id = "wf_prev"
        checkpoint.context = WorkflowContext(
            workflow_id="wf_prev",
            session_id="proj1",
            variables={"project_id": "proj1"},
            step_outputs={"items_builder": [{"page": 1}]},
        )
        runner._get_latest_project_checkpoint = AsyncMock(return_value=checkpoint)
        runner.run_single_step = AsyncMock(return_value={"status": "success"})

        result = asyncio.get_event_loop().run_until_complete(
            runner.rerun_single_step("proj1", "artist_batch", task="updated")
        )

        self.assertEqual(result["status"], "success")
        runner.run_single_step.assert_awaited_once()
        kwargs = runner.run_single_step.await_args.kwargs
        self.assertEqual(kwargs["base_context"].workflow_id, "wf_prev")
        self.assertEqual(kwargs["source_workflow_id"], "wf_prev")

    def test_get_latest_project_checkpoint_uses_most_recent_workflow(self):
        runner = _make_runner_with_mock_engine()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "workflow_state.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE workflow_checkpoints (
                        workflow_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO workflow_checkpoints (workflow_id, timestamp) VALUES (?, ?)",
                    ("sbm_proj1_old", "2026-03-21T10:00:00"),
                )
                conn.execute(
                    "INSERT INTO workflow_checkpoints (workflow_id, timestamp) VALUES (?, ?)",
                    ("sbm_proj1_new", "2026-03-22T10:00:00"),
                )
                conn.execute(
                    "INSERT INTO workflow_checkpoints (workflow_id, timestamp) VALUES (?, ?)",
                    ("sbm_other_project", "2026-03-23T10:00:00"),
                )

            checkpoint = MagicMock()
            runner.engine.state_manager.store.db_path = str(db_path)
            runner.engine.state_manager.store.get_latest_checkpoint = AsyncMock(
                return_value=checkpoint
            )

            result = asyncio.get_event_loop().run_until_complete(
                runner._get_latest_project_checkpoint("proj1")
            )

        self.assertIs(result, checkpoint)
        runner.engine.state_manager.store.get_latest_checkpoint.assert_awaited_once_with(
            "sbm_proj1_new"
        )


if __name__ == "__main__":
    unittest.main()
