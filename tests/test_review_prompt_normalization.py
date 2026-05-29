"""Регрессии для review prompt/normalizer manager-а."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parent.parent
PROMPTS_PATH = ROOT / ".cli-proxy" / ".manager" / "prompt" / "prompts.yaml"


class TestReviewPromptNormalization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = yaml.safe_load(PROMPTS_PATH.read_text(encoding="utf-8"))
        cls.prompts = data["prompts"]

    def test_review_instruction_requires_final_json_before_iteration_limit(self):
        instruction = self.prompts["review_instruction_template"]

        self.assertIn("лимита инструментов/итераций", instruction)
        self.assertIn("approved=false", instruction)
        self.assertIn('{"path": "...", "pattern": "..."}', instruction)
        self.assertIn('{"command": "..."}', instruction)

    def test_review_normalizer_rejects_tool_trace_payloads(self):
        normalizer = self.prompts["review_normalize_system"]

        self.assertIn("Поля `approved`, `summary`, `comments` обязательны всегда.", normalizer)
        self.assertIn("Достигнут лимит итераций", normalizer)
        self.assertIn("Последние вызовы инструментов", normalizer)
        self.assertIn("search_text", normalizer)
        self.assertIn("read_file", normalizer)
        self.assertIn("run_command", normalizer)
        self.assertIn('{"pattern": "unfinished markers", "path": "file.py"}', normalizer)
        self.assertIn('{"command": ".venv/bin/pytest -q ..."}', normalizer)


if __name__ == "__main__":
    unittest.main()
