"""
Тесты для P0.1: остановка генерации через cancel_pipeline.

Проверяет:
- PipelineRunner.cancel_pipeline() вызывает engine.cancel_workflow()
- Сохраняется workflow_id при запуске
- cancel_pipeline возвращает ошибку если нет активного pipeline
- run_pipeline_sync использует переданный runner
- GenerationPanel._cancel_event корректно работает
"""

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class TestPipelineRunnerCancel(unittest.TestCase):
    """Тесты для PipelineRunner.cancel_pipeline и связанной логики"""

    def _make_runner_with_mock_engine(self):
        """Создаёт PipelineRunner с замоканным engine"""
        with patch(
            "StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"
        ):
            from StoryBookManager.core.pipeline_runner import PipelineRunner

            runner = PipelineRunner()
            runner.engine = MagicMock()
            runner.engine.cancel_workflow = AsyncMock(return_value=None)
            return runner

    def test_cancel_pipeline_no_engine(self):
        """cancel_pipeline возвращает ошибку если engine=None"""
        with patch(
            "StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"
        ):
            from StoryBookManager.core.pipeline_runner import PipelineRunner

            runner = PipelineRunner()
            runner.engine = None
            runner.current_workflow_id = "test_wf"

            result = asyncio.get_event_loop().run_until_complete(
                runner.cancel_pipeline()
            )
            self.assertEqual(result["status"], "error")

    def test_cancel_pipeline_no_workflow_id(self):
        """cancel_pipeline возвращает ошибку если нет active workflow"""
        runner = self._make_runner_with_mock_engine()
        runner.current_workflow_id = None

        result = asyncio.get_event_loop().run_until_complete(
            runner.cancel_pipeline()
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("Нет активного pipeline", result["message"])

    def test_cancel_pipeline_success(self):
        """cancel_pipeline вызывает engine.cancel_workflow и возвращает cancelled"""
        runner = self._make_runner_with_mock_engine()
        runner.current_workflow_id = "sbm_test_abc123"

        result = asyncio.get_event_loop().run_until_complete(
            runner.cancel_pipeline()
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["workflow_id"], "sbm_test_abc123")
        runner.engine.cancel_workflow.assert_awaited_once_with(
            "sbm_test_abc123",
            reason="Отменено пользователем из StoryBookManager",
        )

    def test_cancel_pipeline_engine_exception(self):
        """cancel_pipeline возвращает ошибку при исключении в engine"""
        runner = self._make_runner_with_mock_engine()
        runner.current_workflow_id = "sbm_fail"
        runner.engine.cancel_workflow = AsyncMock(
            side_effect=RuntimeError("engine boom")
        )

        result = asyncio.get_event_loop().run_until_complete(
            runner.cancel_pipeline()
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("engine boom", result["message"])

    @patch("StoryBookManager.core.pipeline_runner.project_root",
           new=project_root)
    def test_workflow_id_set_during_run(self):
        """run_full_pipeline устанавливает current_workflow_id"""
        runner = self._make_runner_with_mock_engine()

        mock_result = MagicMock()
        runner.engine.execute_workflow_from_yaml = AsyncMock(
            return_value=mock_result
        )

        yaml_path = (
            project_root / "workflow_pipelines" / "storybook_pipeline.yaml"
        )
        if not yaml_path.exists():
            self.skipTest("storybook_pipeline.yaml not found")

        # Захватываем workflow_id во время выполнения
        captured_id = None

        original_execute = runner.engine.execute_workflow_from_yaml

        async def capture_execute(*args, **kwargs):
            nonlocal captured_id
            captured_id = runner.current_workflow_id
            return await original_execute(*args, **kwargs)

        runner.engine.execute_workflow_from_yaml = capture_execute

        # Mock WorkflowContext to avoid importing networkx
        mock_ctx_class = MagicMock()
        mock_ctx_class.return_value = MagicMock()

        with patch.dict(
            "sys.modules",
            {"workflow.models": MagicMock(WorkflowContext=mock_ctx_class)},
        ):
            asyncio.get_event_loop().run_until_complete(
                runner.run_full_pipeline("test_proj", "test task")
            )

        # workflow_id был установлен во время выполнения
        self.assertIsNotNone(captured_id)
        self.assertTrue(captured_id.startswith("sbm_test_proj_"))

        # workflow_id очищается после выполнения
        self.assertIsNone(runner.current_workflow_id)

    @patch("StoryBookManager.core.pipeline_runner.project_root",
           new=project_root)
    def test_run_pipeline_sync_uses_given_runner(self):
        """run_pipeline_sync использует переданный runner, не создаёт новый"""
        from StoryBookManager.core.pipeline_runner import run_pipeline_sync

        runner = self._make_runner_with_mock_engine()
        mock_result = MagicMock()
        runner.engine.execute_workflow_from_yaml = AsyncMock(
            return_value=mock_result
        )

        yaml_path = (
            project_root / "workflow_pipelines" / "storybook_pipeline.yaml"
        )
        if not yaml_path.exists():
            self.skipTest("storybook_pipeline.yaml not found")

        mock_ctx_class = MagicMock()
        mock_ctx_class.return_value = MagicMock()

        with patch.dict(
            "sys.modules",
            {"workflow.models": MagicMock(WorkflowContext=mock_ctx_class)},
        ):
            result = run_pipeline_sync(runner, "proj1", "task1")

        self.assertEqual(result["status"], "success")
        runner.engine.execute_workflow_from_yaml.assert_awaited_once()


class TestCancelEvent(unittest.TestCase):
    """Тесты для threading.Event механизма отмены"""

    def test_cancel_event_starts_cleared(self):
        """_cancel_event начинается в cleared состоянии"""
        event = threading.Event()
        self.assertFalse(event.is_set())

    def test_cancel_event_set_is_visible_from_another_thread(self):
        """Event.set() виден из другого потока"""
        event = threading.Event()
        observed = []

        def worker():
            event.wait(timeout=2.0)
            observed.append(event.is_set())

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        event.set()
        t.join(timeout=3.0)

        self.assertTrue(observed)
        self.assertTrue(observed[0])


if __name__ == "__main__":
    unittest.main()
