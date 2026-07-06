from __future__ import annotations

import importlib
import unittest
from pathlib import Path

import yaml

from tests.workflow_test_utils import (
    load_light_parallel_executor,
    load_light_workflow_models,
)


ROOT = Path(__file__).resolve().parents[1]

workflow_models = load_light_workflow_models()
parallel_executor_module = load_light_parallel_executor()

ParallelWorkflowExecutor = parallel_executor_module.ParallelWorkflowExecutor
DependencyGraph = parallel_executor_module.DependencyGraph
WorkflowContext = workflow_models.WorkflowContext
WorkflowDefinition = workflow_models.WorkflowDefinition
WorkflowStep = workflow_models.WorkflowStep


class TestStorybookPipelineDag(unittest.TestCase):
    def _load_storybook(self):
        return WorkflowDefinition.from_yaml(ROOT / "workflow_pipelines" / "storybook_pipeline.yaml")

    def test_storybook_pipeline_yaml_is_acyclic(self):
        workflow_def = self._load_storybook()

        self.assertTrue(workflow_def.parallel_execution)
        self.assertTrue(workflow_def.requires_enhanced_engine)
        self.assertFalse(DependencyGraph(workflow_def.steps).has_cycles())

    def test_story_writer_editor_order_is_pinned(self):
        steps = {step.id: step for step in self._load_storybook().steps}

        self.assertEqual(steps["story_writer"].depends_on, ["style_keeper"])
        self.assertEqual(steps["story_editor"].depends_on, ["story_writer"])
        self.assertIn("story_editor", steps["prompt_engineer"].depends_on)

    def test_all_storybook_dependencies_exist(self):
        workflow_def = self._load_storybook()
        step_ids = {step.id for step in workflow_def.steps}
        missing = {
            dep_id
            for step in workflow_def.steps
            for dep_id in step.depends_on
            if dep_id not in step_ids
        }

        self.assertEqual(missing, set())

    def test_video_tail_steps_have_required_order(self):
        steps = {step.id: step for step in self._load_storybook().steps}

        self.assertIn("storybook_video_preflight", steps)
        self.assertIn("storybook_video_delivery_promise", steps)
        self.assertIn("video_generator", steps)
        self.assertIn("storybook_audio_subtitle", steps)
        self.assertIn("storybook_music_generator", steps)
        self.assertIn("montage_assembler", steps)
        self.assertIn("storybook_video_decision_log", steps)
        self.assertIn("storybook_video_preflight", steps["storybook_video_delivery_promise"].depends_on)
        self.assertIn("storybook_video_delivery_promise", steps["video_generator"].depends_on)
        self.assertEqual(steps["storybook_audio_subtitle"].depends_on, ["video_generator"])
        self.assertEqual(steps["storybook_music_generator"].depends_on, ["storybook_audio_subtitle"])
        self.assertEqual(steps["storybook_music_generator"].condition, "{generate_screenplay}")
        self.assertEqual(steps["montage_assembler"].depends_on, ["storybook_music_generator"])
        self.assertEqual(steps["storybook_video_decision_log"].depends_on, ["montage_assembler"])

    def test_montage_assembler_receives_audio_policy_flags(self):
        steps = {step.id: step for step in self._load_storybook().steps}

        params = steps["montage_assembler"].tool_params

        self.assertEqual(params["allow_missing_audio"], "{final_allow_missing_audio}")
        self.assertEqual(params["music_enabled"], "{generate_music}")

    def test_pipeline_runner_knows_video_tail_artifacts(self):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        artifacts = PipelineRunner._step_output_artifacts()

        self.assertIn("96_video_contract/provider_menu_summary.json", artifacts["storybook_video_preflight"])
        self.assertIn("96_video_contract/delivery_promise.json", artifacts["storybook_video_delivery_promise"])
        self.assertIn("98_audio/subtitles.srt", artifacts["storybook_audio_subtitle"])
        self.assertIn("98_audio/music_manifest.json", artifacts["storybook_music_generator"])
        self.assertIn("99_final/final_video.mp4", artifacts["montage_assembler"])
        self.assertIn("96_video_contract/decision_log.json", artifacts["storybook_video_decision_log"])

    def test_video_tail_tool_definitions_resolve_to_callables(self):
        workflow_def = self._load_storybook()
        tail_step_ids = {
            "storybook_video_preflight",
            "storybook_video_delivery_promise",
            "storybook_audio_subtitle",
            "storybook_music_generator",
            "montage_assembler",
            "storybook_video_decision_log",
        }
        tool_names = {
            step.tool_name
            for step in workflow_def.steps
            if step.id in tail_step_ids
        }

        definitions = {}
        for path in (ROOT / "tool_definitions").glob("*.yaml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("name") in tool_names:
                definitions[data["name"]] = (path, data)

        self.assertEqual(set(definitions), tool_names)
        for tool_name, (path, data) in definitions.items():
            source = data.get("implementation_source")
            self.assertIsInstance(source, str, path)
            module_name, function_name = source.rsplit(".", 1)
            module = importlib.import_module(module_name)
            self.assertTrue(callable(getattr(module, function_name, None)), tool_name)


class TestParallelExecutorCycleGate(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_executor_raises_on_cycle_before_running_steps(self):
        steps = [
            WorkflowStep(id="story_writer", task="write", depends_on=["story_editor"]),
            WorkflowStep(id="story_editor", task="edit", depends_on=["story_writer"]),
        ]

        async def step_executor(_step, _context):
            raise AssertionError("cyclic workflow must fail before executing any step")

        executor = ParallelWorkflowExecutor(max_concurrent=1)

        with self.assertRaisesRegex(ValueError, "циклические зависимости"):
            await executor.execute_steps_parallel(
                steps,
                WorkflowContext(workflow_id="wf_cycle", session_id="wf_cycle"),
                step_executor=step_executor,
                dependency_checker=lambda step, results: True,
                condition_checker=lambda step, context: False,
            )
