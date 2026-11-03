"""Ф0 п.6: ошибка инициализации движка сохраняется в engine_error."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def test_engine_error_recorded():
    from StoryBookManager.core.pipeline_runner import PipelineRunner
    fake = MagicMock()
    fake.EnhancedWorkflowEngine.side_effect = RuntimeError("boom")
    with patch.dict(sys.modules, {"workflow.enhanced_engine": fake}):
        runner = PipelineRunner()
    assert runner.engine is None
    assert "boom" in runner.engine_error


def test_engine_ok_clears_error():
    from StoryBookManager.core.pipeline_runner import PipelineRunner
    fake = MagicMock()
    with patch.dict(sys.modules, {"workflow.enhanced_engine": fake}):
        runner = PipelineRunner()
    assert runner.engine is not None
    assert runner.engine_error is None
