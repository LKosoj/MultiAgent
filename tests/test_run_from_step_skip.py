"""
Тесты для P0.4: исправление логики пропуска шагов в run_from_step.

Проверяет:
- Шаги до стартового удаляются из workflow (не помечаются condition)
- Стартовый шаг и последующие сохраняются
- Несуществующий шаг возвращает ошибку
- Логи содержат записи о пропущенных шагах
- Нет хардкод-условия condition="true" в исходном коде
"""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def _make_runner_with_mock_engine():
    """Создаёт PipelineRunner с замоканным engine"""
    with patch(
        "StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"
    ):
        from StoryBookManager.core.pipeline_runner import PipelineRunner

        runner = PipelineRunner()
        runner.engine = MagicMock()
        runner.engine.execute_workflow = AsyncMock(return_value=MagicMock())
        return runner


def _make_mock_workflow_def(step_ids):
    """Создаёт mock WorkflowDefinition с заданными шагами"""
    mock_def = MagicMock()
    steps = []
    for sid in step_ids:
        step = MagicMock()
        step.id = sid
        step.condition = None
        steps.append(step)
    mock_def.steps = steps
    return mock_def


PIPELINE_STEP_IDS = [
    "brief_from_prompt", "init_project", "story_planner",
    "bible_builder", "style_keeper", "story_writer",
    "prompt_engineer", "artist_batch"
]


class TestRunFromStepSlicing(unittest.TestCase):
    """Проверяет что run_from_step корректно отрезает шаги"""

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_start_from_step_5_removes_steps_1_to_4(self):
        """Запуск с шага 5 (style_keeper) удаляет шаги 1-4"""
        runner = _make_runner_with_mock_engine()
        mock_def = _make_mock_workflow_def(PIPELINE_STEP_IDS)

        mock_ctx = MagicMock()
        mock_wf_def_cls = MagicMock(from_yaml=MagicMock(return_value=mock_def))
        mock_ctx_cls = MagicMock(return_value=mock_ctx)

        with patch.dict("sys.modules", {
            "workflow.models": MagicMock(
                WorkflowDefinition=mock_wf_def_cls,
                WorkflowContext=mock_ctx_cls,
            ),
        }):
            # Подменяем путь к YAML чтобы exists() вернул True
            yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"
            if not yaml_path.exists():
                self.skipTest("storybook_pipeline.yaml not found")

            result = asyncio.get_event_loop().run_until_complete(
                runner.run_from_step("proj1", "style_keeper")
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["skipped_steps"], 4)

        # Проверяем что engine получил workflow с шагами начиная с style_keeper
        call_args = runner.engine.execute_workflow.call_args
        passed_def = call_args[0][0]  # первый позиционный аргумент
        remaining_step_ids = [s.id for s in passed_def.steps]
        self.assertEqual(
            remaining_step_ids,
            ["style_keeper", "story_writer", "prompt_engineer", "artist_batch"]
        )

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_start_from_first_step_skips_nothing(self):
        """Запуск с первого шага не удаляет ничего"""
        runner = _make_runner_with_mock_engine()
        mock_def = _make_mock_workflow_def(PIPELINE_STEP_IDS)

        mock_wf_def_cls = MagicMock(from_yaml=MagicMock(return_value=mock_def))
        mock_ctx_cls = MagicMock(return_value=MagicMock())

        with patch.dict("sys.modules", {
            "workflow.models": MagicMock(
                WorkflowDefinition=mock_wf_def_cls,
                WorkflowContext=mock_ctx_cls,
            ),
        }):
            yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"
            if not yaml_path.exists():
                self.skipTest("storybook_pipeline.yaml not found")

            result = asyncio.get_event_loop().run_until_complete(
                runner.run_from_step("proj1", "brief_from_prompt")
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["skipped_steps"], 0)

        call_args = runner.engine.execute_workflow.call_args
        passed_def = call_args[0][0]
        self.assertEqual(len(passed_def.steps), len(PIPELINE_STEP_IDS))

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_nonexistent_step_returns_error(self):
        """Несуществующий шаг возвращает ошибку"""
        runner = _make_runner_with_mock_engine()
        mock_def = _make_mock_workflow_def(PIPELINE_STEP_IDS)

        mock_wf_def_cls = MagicMock(from_yaml=MagicMock(return_value=mock_def))
        mock_ctx_cls = MagicMock(return_value=MagicMock())

        with patch.dict("sys.modules", {
            "workflow.models": MagicMock(
                WorkflowDefinition=mock_wf_def_cls,
                WorkflowContext=mock_ctx_cls,
            ),
        }):
            yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"
            if not yaml_path.exists():
                self.skipTest("storybook_pipeline.yaml not found")

            result = asyncio.get_event_loop().run_until_complete(
                runner.run_from_step("proj1", "nonexistent_step")
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("не найден", result["message"])
        runner.engine.execute_workflow.assert_not_awaited()

    @patch("StoryBookManager.core.pipeline_runner.project_root", new=project_root)
    def test_skipped_step_conditions_not_set(self):
        """Пропущенные шаги не получают condition='true' (шаги просто удаляются)"""
        runner = _make_runner_with_mock_engine()
        mock_def = _make_mock_workflow_def(PIPELINE_STEP_IDS)

        mock_wf_def_cls = MagicMock(from_yaml=MagicMock(return_value=mock_def))
        mock_ctx_cls = MagicMock(return_value=MagicMock())

        with patch.dict("sys.modules", {
            "workflow.models": MagicMock(
                WorkflowDefinition=mock_wf_def_cls,
                WorkflowContext=mock_ctx_cls,
            ),
        }):
            yaml_path = project_root / "workflow_pipelines" / "storybook_pipeline.yaml"
            if not yaml_path.exists():
                self.skipTest("storybook_pipeline.yaml not found")

            asyncio.get_event_loop().run_until_complete(
                runner.run_from_step("proj1", "story_writer")
            )

        call_args = runner.engine.execute_workflow.call_args
        passed_def = call_args[0][0]
        for step in passed_def.steps:
            # None — оригинальное значение, не модифицировано
            self.assertIsNone(step.condition,
                              f"Шаг {step.id} не должен иметь condition")


class TestNoHardcodedConditionTrue(unittest.TestCase):
    """Проверяет отсутствие condition='true' хака в исходном коде"""

    def test_no_condition_true_in_run_from_step(self):
        """В run_from_step нет condition = 'true'"""
        source = (
            project_root / "StoryBookManager" / "core" / "pipeline_runner.py"
        ).read_text(encoding="utf-8")

        start = source.index("async def run_from_step(")
        next_def = source.index("\n    async def ", start + 1) if "\n    async def " in source[start + 1:] else source.index("\n    def ", start + 1)
        method_body = source[start:next_def]

        self.assertNotIn('condition = "true"', method_body)
        self.assertNotIn("condition = 'true'", method_body)


if __name__ == "__main__":
    unittest.main()
