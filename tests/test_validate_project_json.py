"""
Тесты для P2.4: валидация JSON-файлов проекта.

Проверяет:
- validate_project_for_pipeline проверяет все JSON файлы
- Невалидный JSON показывает ошибку с именем файла и строкой
- Валидные JSON проходят проверку
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

RUNNER_PATH = project_root / "StoryBookManager" / "core" / "pipeline_runner.py"


class TestValidateProjectJsonSyntax(unittest.TestCase):
    """Проверяет валидацию JSON-файлов в validate_project_for_pipeline"""

    def _make_runner(self):
        with patch(
            "StoryBookManager.core.pipeline_runner.PipelineRunner._initialize_engine"
        ):
            from StoryBookManager.core.pipeline_runner import PipelineRunner
            runner = PipelineRunner()
            runner.engine = MagicMock()
            return runner

    def _make_project(self, tmpdir, files):
        """Создаёт проект с заданными файлами. files = {rel_path: content}"""
        proj_dir = Path(tmpdir) / "test_project"
        proj_dir.mkdir()
        for rel_path, content in files.items():
            file_path = proj_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
        return proj_dir

    def _run_validate(self, runner, tmpdir, project_name="test_project"):
        mock_settings = MagicMock()
        mock_settings.get_projects_directory.return_value = Path(tmpdir)
        mock_module = MagicMock()
        mock_module.app_settings = mock_settings
        with patch.dict("sys.modules", {"config.settings": mock_module}):
            return runner.validate_project_for_pipeline(project_name)

    def test_valid_project_passes(self):
        """Проект с валидными JSON проходит валидацию"""
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_project(tmpdir, {
                "00_brief.json": '{"title": "test"}',
                "20_story/story.json": '{"pages": []}',
            })
            result = self._run_validate(runner, tmpdir)
            self.assertTrue(result["valid"])

    def test_invalid_json_detected(self):
        """Невалидный JSON обнаруживается"""
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_project(tmpdir, {
                "00_brief.json": '{"title": "test"}',
                "20_story/story.json": '{invalid json!!!}',
            })
            result = self._run_validate(runner, tmpdir)
            self.assertFalse(result["valid"])
            self.assertIn("story.json", result["message"])

    def test_error_includes_line_number(self):
        """Ошибка содержит номер строки"""
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_project(tmpdir, {
                "00_brief.json": '{"title": "test"}',
                "bad.json": '{\n  "a": 1,\n  bad\n}',
            })
            result = self._run_validate(runner, tmpdir)
            self.assertFalse(result["valid"])
            self.assertIn("строка", result["message"])

    def test_multiple_invalid_files_all_reported(self):
        """Все невалидные файлы перечислены в ошибке"""
        runner = self._make_runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_project(tmpdir, {
                "00_brief.json": '{"title": "test"}',
                "a.json": '{bad}',
                "sub/b.json": '{also bad}',
            })
            result = self._run_validate(runner, tmpdir)
            self.assertFalse(result["valid"])
            self.assertIn("a.json", result["message"])
            self.assertIn("b.json", result["message"])


class TestValidateProjectSourceCode(unittest.TestCase):
    """Проверяет исходный код validate_project_for_pipeline"""

    def _get_method_body(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")
        start = source.index("def validate_project_for_pipeline(self")
        next_def = source.index("\ndef ", start + 1)
        return source[start:next_def]

    def test_uses_rglob_json(self):
        """Рекурсивно ищет все *.json файлы"""
        body = self._get_method_body()
        self.assertIn('rglob("*.json")', body)

    def test_catches_json_decode_error(self):
        """Перехватывает JSONDecodeError"""
        body = self._get_method_body()
        self.assertIn("JSONDecodeError", body)

    def test_reports_line_number(self):
        """Отчёт включает номер строки"""
        body = self._get_method_body()
        self.assertIn("e.lineno", body)


if __name__ == "__main__":
    unittest.main()
