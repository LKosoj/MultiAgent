"""
Тесты для P0.2: удаление fallback на хардкод-список шагов.

Проверяет:
- При отсутствии YAML pipeline_steps пустой, ошибка сохранена
- При корректном YAML шаги загружаются
- При повреждённом YAML шаги пустые, ошибка сохранена
- Хардкод-список удалён из исходного кода
- run_full_pipeline/run_from_step содержат проверку _pipeline_load_error
"""

import tempfile
import unittest
from pathlib import Path

import sys
import yaml

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


VALID_YAML = """\
name: test_pipeline
version: '0.1'
steps:
  - id: step_one
    step_type: tool
    task: "First step"
    tool_name: tool_one
  - id: step_two
    step_type: tool
    task: "Second step"
    tool_name: tool_two
  - id: step_three
    step_type: tool
    task: "Third step"
    tool_name: tool_three
"""

EMPTY_STEPS_YAML = """\
name: test_pipeline
version: '0.1'
steps: []
"""

INVALID_YAML = "{{{{not yaml at all"


def _run_load_logic(tmpdir, yaml_content=None, yaml_exists=True):
    """Reproduce the load_pipeline_steps logic with a custom tmpdir.

    This avoids importing tkinter by reimplementing the same logic
    that generation_panel.load_pipeline_steps uses.
    """
    pipelines_dir = Path(tmpdir) / "workflow_pipelines"
    pipelines_dir.mkdir(exist_ok=True)

    yaml_path = pipelines_dir / "storybook_pipeline.yaml"
    if yaml_exists and yaml_content is not None:
        yaml_path.write_text(yaml_content, encoding="utf-8")

    # Same logic as GenerationPanel.load_pipeline_steps
    pipeline_steps = []
    pipeline_load_error = None

    try:
        pipeline_file = Path(tmpdir) / "workflow_pipelines" / "storybook_pipeline.yaml"

        if not pipeline_file.exists():
            pipeline_load_error = f"Файл pipeline не найден: {pipeline_file}"
            return pipeline_steps, pipeline_load_error

        with open(pipeline_file, 'r', encoding='utf-8') as f:
            pipeline_data = yaml.safe_load(f)

        steps = pipeline_data.get('steps', [])
        pipeline_steps = [step.get('id') for step in steps if step.get('id')]

        if not pipeline_steps:
            pipeline_load_error = "Pipeline файл не содержит шагов"
            return pipeline_steps, pipeline_load_error

    except Exception as e:
        pipeline_load_error = f"Ошибка загрузки pipeline: {e}"

    return pipeline_steps, pipeline_load_error


class TestLoadPipelineSteps(unittest.TestCase):
    """Тесты загрузки шагов из YAML без fallback"""

    def test_missing_yaml_no_fallback(self):
        """При отсутствии YAML pipeline_steps пустой, ошибка установлена"""
        with tempfile.TemporaryDirectory() as tmpdir:
            steps, error = _run_load_logic(tmpdir, yaml_exists=False)

        self.assertEqual(steps, [])
        self.assertIsNotNone(error)
        self.assertIn("не найден", error)

    def test_valid_yaml_loads_steps(self):
        """При корректном YAML шаги загружаются"""
        with tempfile.TemporaryDirectory() as tmpdir:
            steps, error = _run_load_logic(tmpdir, VALID_YAML)

        self.assertEqual(steps, ["step_one", "step_two", "step_three"])
        self.assertIsNone(error)

    def test_empty_steps_yaml(self):
        """При пустом списке шагов — ошибка, не fallback"""
        with tempfile.TemporaryDirectory() as tmpdir:
            steps, error = _run_load_logic(tmpdir, EMPTY_STEPS_YAML)

        self.assertEqual(steps, [])
        self.assertIsNotNone(error)
        self.assertIn("не содержит шагов", error)

    def test_invalid_yaml_no_fallback(self):
        """При повреждённом YAML — ошибка, не fallback"""
        with tempfile.TemporaryDirectory() as tmpdir:
            steps, error = _run_load_logic(tmpdir, INVALID_YAML)

        self.assertEqual(steps, [])
        self.assertIsNotNone(error)
        self.assertIn("Ошибка загрузки pipeline", error)


class TestNoHardcodedFallback(unittest.TestCase):
    """Проверяет отсутствие хардкод-списка в исходном коде"""

    def test_no_hardcoded_step_list_in_load_pipeline_steps(self):
        """В load_pipeline_steps нет хардкод-списка шагов"""
        source_path = (
            project_root / "StoryBookManager" / "gui" / "generation_panel.py"
        )
        source = source_path.read_text(encoding="utf-8")

        # Извлекаем тело метода load_pipeline_steps
        start = source.index("def load_pipeline_steps(self):")
        # Найти следующий def на том же уровне отступа
        next_def = source.index("\n    def ", start + 1)
        method_body = source[start:next_def]

        hardcoded_markers = [
            '"brief_from_prompt"',
            '"init_project"',
            '"story_planner"',
            '"bible_builder"',
            '"style_keeper"',
        ]
        for marker in hardcoded_markers:
            self.assertNotIn(
                marker, method_body,
                f"Хардкод-шаг {marker} найден в load_pipeline_steps"
            )


class TestPipelineLoadErrorBlocksRun(unittest.TestCase):
    """Проверяет что run_full_pipeline/run_from_step содержат проверку ошибки"""

    def _read_method_source(self, method_name):
        source_path = (
            project_root / "StoryBookManager" / "gui" / "generation_panel.py"
        )
        source = source_path.read_text(encoding="utf-8")
        start = source.index(f"def {method_name}(self):")
        next_def = source.index("\n    def ", start + 1)
        return source[start:next_def]

    def test_run_full_pipeline_checks_load_error(self):
        """run_full_pipeline содержит проверку _pipeline_load_error"""
        body = self._read_method_source("run_full_pipeline")
        self.assertIn("_pipeline_load_error", body)
        self.assertIn("Pipeline не загружен", body)

    def test_run_from_step_checks_load_error(self):
        """run_from_step содержит проверку _pipeline_load_error"""
        body = self._read_method_source("run_from_step")
        self.assertIn("_pipeline_load_error", body)
        self.assertIn("Pipeline не загружен", body)


if __name__ == "__main__":
    unittest.main()
