"""
Тесты для P2.4: валидация зависимостей шагов при частичном запуске.

Проверяет:
- validate_step_dependencies определён и работает
- Запуск с шага без зависимостей (первый шаг) не даёт ошибок
- Запуск с зависимого шага при пропуске зависимости — ошибка
- run_from_step вызывает validate_step_dependencies
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

RUNNER_PATH = project_root / "StoryBookManager" / "core" / "pipeline_runner.py"


def _make_steps(specs):
    """specs = [(id, [depends_on]), ...]"""
    steps = []
    for step_id, deps in specs:
        s = MagicMock()
        s.id = step_id
        s.depends_on = deps
        steps.append(s)
    return steps


def _make_workflow(specs):
    wf = MagicMock()
    wf.steps = _make_steps(specs)
    return wf


class TestValidateStepDependencies(unittest.TestCase):

    def _validate(self, specs, start_step):
        from StoryBookManager.core.pipeline_runner import PipelineRunner
        wf = _make_workflow(specs)
        return PipelineRunner.validate_step_dependencies(wf, start_step)

    def test_first_step_no_errors(self):
        """Запуск с первого шага — нет неудовлетворённых зависимостей"""
        errors = self._validate([
            ("a", []),
            ("b", ["a"]),
            ("c", ["b"]),
        ], "a")
        self.assertEqual(errors, [])

    def test_start_from_dependent_step_with_skipped_dep(self):
        """Запуск со 2-го шага, зависящего от 1-го — ошибка"""
        errors = self._validate([
            ("a", []),
            ("b", ["a"]),
            ("c", ["b"]),
        ], "b")
        self.assertTrue(len(errors) > 0)
        self.assertIn("a", errors[0])

    def test_start_from_step_without_deps(self):
        """Шаг без зависимостей — нет ошибок"""
        errors = self._validate([
            ("a", []),
            ("b", []),
            ("c", ["b"]),
        ], "b")
        self.assertEqual(errors, [])

    def test_transitive_deps_reported(self):
        """c зависит от a (пропущенного) — ошибка"""
        errors = self._validate([
            ("a", []),
            ("b", ["a"]),
            ("c", ["a"]),
        ], "b")
        # b зависит от a, c зависит от a — обе ошибки
        dep_texts = " ".join(errors)
        self.assertIn("a", dep_texts)

    def test_nonexistent_step_returns_empty(self):
        """Несуществующий шаг — пустой список"""
        errors = self._validate([("a", [])], "z")
        self.assertEqual(errors, [])


class TestRunFromStepChecksDeps(unittest.TestCase):
    """Проверяет интеграцию validate_step_dependencies в run_from_step"""

    def test_run_from_step_calls_validate(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def run_from_step(self")
        next_def = source.index("\n    async def ", start + 1)
        body = source[start:next_def]
        self.assertIn("validate_step_dependencies", body)

    def test_run_from_step_returns_error_on_dep_failure(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("async def run_from_step(self")
        next_def = source.index("\n    async def ", start + 1)
        body = source[start:next_def]
        self.assertIn("Неудовлетворённые зависимости", body)

    def test_validate_method_uses_depends_on(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("def validate_step_dependencies(")
        next_def = source.index("\n    async def ", start + 1)
        body = source[start:next_def]
        self.assertIn("depends_on", body)


if __name__ == "__main__":
    unittest.main()
