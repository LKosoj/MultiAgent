from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

from tests.workflow_test_utils import load_light_workflow_models

load_light_workflow_models()


class TestProjectValidationDependencies(unittest.TestCase):
    @patch("StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine")
    def test_validate_project_checks_required_artifacts_for_partial_run(self, _mock_init):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner()

        with tempfile.TemporaryDirectory() as tmp_dir:
            projects_root = Path(tmp_dir)
            project_path = projects_root / "proj_validation"
            (project_path / "10_synopsis").mkdir(parents=True, exist_ok=True)
            (project_path / "20_bible").mkdir(parents=True, exist_ok=True)
            (project_path / "30_style").mkdir(parents=True, exist_ok=True)

            (project_path / "00_brief.json").write_text(
                json.dumps({"title": "Story", "storybook_prompt": "Prompt"}),
                encoding="utf-8",
            )
            (project_path / "10_synopsis" / "synopsis.json").write_text("{}", encoding="utf-8")
            (project_path / "10_synopsis" / "beats.json").write_text("{}", encoding="utf-8")
            (project_path / "20_bible" / "characters.json").write_text("{}", encoding="utf-8")
            (project_path / "20_bible" / "locations.json").write_text("{}", encoding="utf-8")
            (project_path / "20_bible" / "consistency_rules.json").write_text(
                "[]",
                encoding="utf-8",
            )

            config_package = types.ModuleType("config")
            config_settings_module = types.ModuleType("config.settings")
            config_settings_module.app_settings = types.SimpleNamespace(
                get_projects_directory=lambda: projects_root
            )
            config_package.settings = config_settings_module

            with patch.dict(
                sys.modules,
                {
                    "config": config_package,
                    "config.settings": config_settings_module,
                },
            ):
                result = runner.validate_project_for_pipeline(
                    "proj_validation",
                    start_step="prompt_engineer",
                )

        self.assertFalse(result["valid"])
        self.assertIn("30_style/style_text.json", result["message"])
        self.assertIn("30_style/style_images.json", result["message"])

    def test_required_artifacts_follow_dependency_graph_not_previous_order(self):
        """screenplay branch не должна требовать image/pdf артефакты предыдущей ветки."""
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        models = load_light_workflow_models()
        workflow_def = models.WorkflowDefinition.from_yaml(
            Path(__file__).resolve().parents[1] / "workflow_pipelines" / "storybook_pipeline.yaml"
        )

        required = PipelineRunner._collect_required_artifacts(
            workflow_def,
            start_step="screenplay_generator",
        )

        self.assertIn("20_story/story.json", required)
        self.assertNotIn("50_images", required)
        self.assertNotIn("90_md/book.md", required)
        self.assertNotIn("95_pdf/book.pdf", required)
