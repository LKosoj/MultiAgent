"""Проверка передачи контекста в generate_run_summary."""
from datetime import datetime, timezone
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from memory.rag_memory import MemoryPolicy, RagMemory


class _CaptureModel:
    def __init__(self):
        self.last_messages = None

    def __call__(self, messages, **kwargs):
        self.last_messages = messages
        return SimpleNamespace(content="Тестовое суммари на основе контекста")


ActionStep = type("ActionStep", (), {})
FinalAnswerStep = type("FinalAnswerStep", (), {})


class TestRunSummaryContext(unittest.TestCase):
    def setUp(self):
        self.memory = RagMemory(
            session_id="test-session",
            agent_name="researcher",
            policy=MemoryPolicy(enable_logging=False, last_k_steps=5),
        )
        self.memory.start_new_run(task="Найти статьи про AI и Data Engineering")
        self.capture_model = _CaptureModel()
        self.memory.set_summary_model(self.capture_model)

    def _add_action_step(self, observations: str):
        step = ActionStep()
        step.error = None
        step.model_output = "вызов web_search"
        step.action_output = None
        step.observations = observations
        step.code_action = None
        self.memory.steps.append(step)

    def _add_final_answer(self, output: str):
        step = FinalAnswerStep()
        step.output = output
        self.memory.steps.append(step)

    @patch("memory.rag_memory.save_memory", return_value=1)
    def test_context_includes_steps_and_final_answer(self, _save):
        self._add_action_step("Результаты поиска: статья 1, статья 2")
        self._add_final_answer("Итог: найдены 2 статьи про AI и DE")

        context = self.memory.collect_run_context_for_summary()
        self.assertTrue(context["successful_steps"])
        joined = "\n".join(context["successful_steps"])
        self.assertIn("статья 1", joined)
        self.assertIn("найдены 2 статьи", joined)

        summary = self.memory.generate_run_summary(model=self.capture_model)
        self.assertEqual(summary, "Тестовое суммари на основе контекста")
        self.assertIsNotNone(self.capture_model.last_messages)

        user_content = self._extract_user_content(self.capture_model.last_messages)
        self.assertIn("статья 1", user_content)
        self.assertIn("найдены 2 статьи", user_content)
        self.assertIn("AI и Data Engineering", user_content)
        self.assertGreater(len(user_content), 100)

    @patch("memory.rag_memory.save_memory", return_value=1)
    @patch("memory.tools.get_memory")
    def test_rag_records_included_with_requester_context(self, mock_get_memory, _save):
        mock_get_memory.return_value = [
            {
                "agent_name": "researcher",
                "step": 1,
                "data": {
                    "agent_response": "Длинный ответ исследователя с ссылками",
                    "cache_kind": "agent_step",
                },
            }
        ]
        self._add_final_answer("краткий итог")

        summary = self.memory.generate_run_summary(model=self.capture_model)
        self.assertIsNotNone(summary)
        user_content = self._extract_user_content(self.capture_model.last_messages)
        self.assertIn("Длинный ответ исследователя", user_content)
        mock_get_memory.assert_called_once()
        self.assertEqual(mock_get_memory.call_args.kwargs.get("requesting_agent"), "researcher")

    @patch("memory.rag_memory.save_memory", return_value=1)
    def test_uses_passed_model_not_global(self, _save):
        self._add_final_answer("результат работы")
        passed = _CaptureModel()
        self.memory.generate_run_summary(model=passed)
        self.assertIsNotNone(passed.last_messages)
        self.assertIsNone(self.capture_model.last_messages)

    def test_get_context_converts_token_budget_to_char_budget(self):
        self.memory.policy.strategic_read = True
        self.memory.policy.search_enabled = False
        self.memory.get_full_steps = MagicMock(return_value=[])
        self.memory._get_strategic_context = MagicMock(return_value="x" * 200)

        context = self.memory.get_context(max_tokens=40)

        self.assertTrue(context.endswith("..."))
        self.assertGreater(len(context), 40)
        self.assertLessEqual(len(context), 160 + len("\n..."))

    def test_get_context_filters_semantic_duplicate_from_recent_steps(self):
        duplicate_step = {
            "agent_name": "researcher",
            "step": 2,
            "data": {"agent_response": "duplicate latest context text"},
        }
        recent_steps = [
            {
                "agent_name": "researcher",
                "step": 1,
                "data": {"agent_response": "recent context for semantic query"},
            },
            duplicate_step,
        ]
        semantic_only_step = {
            "agent_name": "researcher",
            "step": 99,
            "data": {"agent_response": "older semantic context text"},
        }
        self.memory.policy.strategic_read = False
        self.memory.policy.search_enabled = True
        self.memory.get_full_steps = MagicMock(return_value=recent_steps)
        self.memory.search_memory = MagicMock(
            return_value=[duplicate_step, semantic_only_step]
        )

        context = self.memory.get_context()

        self.assertIn("older semantic context text", context)
        self.assertEqual(context.count("duplicate latest context text"), 1)

    @patch("memory.rag_memory.save_memory", return_value=1)
    def test_add_step_writes_utc_aware_timestamp(self, mock_save):
        self.memory.add_step({"agent_response": "результат"})

        timestamp = mock_save.call_args.kwargs["data"]["timestamp"]
        parsed = datetime.fromisoformat(timestamp)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(None))

    @staticmethod
    def _extract_user_content(messages):
        for msg in messages:
            role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
            content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
            if str(role).endswith("user") or role == "user":
                return str(content)
        return ""


if __name__ == "__main__":
    unittest.main()
