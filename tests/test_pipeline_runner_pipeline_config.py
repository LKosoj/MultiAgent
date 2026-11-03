"""
Тесты передачи настроек pipeline (input_overrides) в движок через PipelineRunner.

Проверяет:
- run_full_pipeline отдаёт параметры в execute_workflow_from_yaml и в контекст
- run_from_step сливает входы из yaml с переопределениями из интерфейса
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class TestPipelineRunnerPipelineConfig(unittest.TestCase):
    """Проверяет передачу настроек в runtime pipeline."""

    def setUp(self):
        # проекты во временной папке: иначе .pipeline.lock ложится в настоящий plots/storybooks
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"STORYBOOK_PROJECTS_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _make_runner(self):
        with patch(
            "StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"
        ):
            from StoryBookManager.core.pipeline_runner import PipelineRunner

            runner = PipelineRunner()
            runner.engine = MagicMock()
            runner.engine.execute_workflow_from_yaml = AsyncMock(return_value=MagicMock())
            runner.engine.execute_workflow = AsyncMock(return_value=MagicMock())
            return runner

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_run_full_pipeline_passes_input_overrides_to_engine(self):
        runner = self._make_runner()
        overrides = {
            "pages_min": 9,
            "pages_max": 12,
            "words_per_page_min": 130,
            "words_per_page_max": 220,
            "language": "en",
            "screenplay_time": 180,
            "generate_screenplay": False,
            "generate_end_shots": False,
            "force_update_prompts": True,
            "skip_prompt_enhancement": False,
            "sample_before_batch": True,
            "sample_shot_key": "1-2",
            "generate_music": True,
            "final_allow_missing_audio": True,
        }
        mock_ctx_cls = MagicMock(return_value=MagicMock())

        with patch.dict(
            "sys.modules",
            {"workflow.models": MagicMock(WorkflowContext=mock_ctx_cls, WorkflowDefinition=MagicMock())},
        ):
            result = asyncio.run(
                runner.run_full_pipeline("proj1", "custom task", input_overrides=overrides)
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(mock_ctx_cls.call_args.kwargs["variables"]["language"], "en")
        self.assertTrue(mock_ctx_cls.call_args.kwargs["variables"]["force_update_prompts"])

        call_kwargs = runner.engine.execute_workflow_from_yaml.call_args.kwargs
        self.assertEqual(call_kwargs["pages_min"], 9)
        self.assertEqual(call_kwargs["pages_max"], 12)
        self.assertEqual(call_kwargs["words_per_page_min"], 130)
        self.assertEqual(call_kwargs["words_per_page_max"], 220)
        self.assertEqual(call_kwargs["language"], "en")
        self.assertEqual(call_kwargs["screenplay_time"], 180)
        self.assertFalse(call_kwargs["generate_screenplay"])
        self.assertFalse(call_kwargs["generate_end_shots"])
        self.assertTrue(call_kwargs["force_update_prompts"])
        self.assertFalse(call_kwargs["skip_prompt_enhancement"])
        self.assertTrue(call_kwargs["sample_before_batch"])
        self.assertEqual(call_kwargs["sample_shot_key"], "1-2")
        self.assertTrue(call_kwargs["generate_music"])
        self.assertTrue(call_kwargs["final_allow_missing_audio"])

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_run_from_step_merges_yaml_inputs_with_ui_overrides(self):
        runner = self._make_runner()

        mock_workflow_def = MagicMock()
        step_a = MagicMock()
        step_a.id = "brief_from_prompt"
        step_a.depends_on = []
        step_a.condition = None
        step_b = MagicMock()
        step_b.id = "story_writer"
        step_b.depends_on = []
        step_b.condition = None
        mock_workflow_def.steps = [step_a, step_b]
        mock_workflow_def.inputs = {
            "task": "yaml task",
            "pages_min": 1,
            "pages_max": 2,
            "words_per_page_min": 100,
            "words_per_page_max": 200,
            "language": "ru",
            "screenplay_time": 120,
            "generate_screenplay": True,
            "generate_end_shots": True,
            "force_update_prompts": False,
            "skip_prompt_enhancement": True,
            "sample_before_batch": False,
            "sample_shot_key": "",
            "generate_music": True,
            "final_allow_missing_audio": False,
        }

        mock_ctx = MagicMock()
        mock_wf_def_cls = MagicMock(from_yaml=MagicMock(return_value=mock_workflow_def))
        mock_ctx_cls = MagicMock(return_value=mock_ctx)

        with patch.dict(
            "sys.modules",
            {
                "workflow.models": MagicMock(
                    WorkflowDefinition=mock_wf_def_cls,
                    WorkflowContext=mock_ctx_cls,
                ),
            },
        ):
            result = asyncio.run(
                runner.run_from_step(
                    "proj42",
                    "story_writer",
                    task="ui task",
                    input_overrides={
                        "pages_min": 7,
                        "words_per_page_max": 260,
                        "language": "de",
                        "screenplay_time": 240,
                        "force_update_prompts": True,
                        "skip_prompt_enhancement": False,
                        "sample_before_batch": True,
                        "sample_shot_key": "1-2",
                        "generate_music": True,
                        "final_allow_missing_audio": True,
                    },
                )
            )

        self.assertEqual(result["status"], "success")
        variables = mock_ctx_cls.call_args.kwargs["variables"]
        self.assertEqual(variables["project_id"], "proj42")
        self.assertEqual(variables["task"], "ui task")
        self.assertEqual(variables["pages_min"], 7)
        self.assertEqual(variables["pages_max"], 2)
        self.assertEqual(variables["words_per_page_min"], 100)
        self.assertEqual(variables["words_per_page_max"], 260)
        self.assertEqual(variables["language"], "de")
        self.assertEqual(variables["screenplay_time"], 240)
        self.assertTrue(variables["force_update_prompts"])
        self.assertFalse(variables["skip_prompt_enhancement"])
        self.assertTrue(variables["sample_before_batch"])
        self.assertEqual(variables["sample_shot_key"], "1-2")
        self.assertTrue(variables["generate_music"])
        self.assertTrue(variables["final_allow_missing_audio"])


if __name__ == "__main__":
    unittest.main()
