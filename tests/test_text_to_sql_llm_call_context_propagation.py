"""Verifies llm_call_context (run_id/step_name) is set at the three provider call sites:

- workflow/text_to_sql_typed_research.py::_typed_schema_model (schema-research decision + stop-review)
- workflow/text_to_sql_adaptive_solver.py::_production_json_model
- custom_tools/text_to_sql/adaptive/result_review_runtime.py::build_result_review_runtime

Each test monkeypatches the provider boundary and reads back llm_call_context.get_llm_call_context()
from inside it, without exercising the surrounding engine/workflow machinery.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from smolagents import ChatMessage, MessageRole

from llm_call_context import get_llm_call_context


def test_schema_research_model_sets_llm_call_context(monkeypatch):
    import agent_command
    from workflow.text_to_sql_typed_research import _research_model

    captured: dict[str, object] = {}

    class Provider:
        def __call__(self, messages, **kwargs):
            captured.update(get_llm_call_context())
            return ChatMessage(role=MessageRole.ASSISTANT, content='{"ok": true}')

    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: Provider()
    )

    model = _research_model("profile-model", 1024, "run-schema-research")
    asyncio.run(model("research prompt"))

    assert captured == {"run_id": "run-schema-research", "step_name": "schema-research"}


def test_schema_research_stop_review_model_sets_distinct_step_name(monkeypatch):
    import agent_command
    from workflow.text_to_sql_typed_research import _research_stop_review_model

    captured: dict[str, object] = {}

    class Provider:
        def __call__(self, messages, **kwargs):
            captured.update(get_llm_call_context())
            return ChatMessage(role=MessageRole.ASSISTANT, content='{"ok": true}')

    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: Provider()
    )

    model = _research_stop_review_model("profile-model", 1024, "run-stop-review")
    asyncio.run(model("stop review prompt"))

    assert captured == {
        "run_id": "run-stop-review",
        "step_name": "schema-research-stop-review",
    }


def test_adaptive_solver_production_json_model_sets_llm_call_context(monkeypatch):
    import agent_command
    import utils
    from workflow.text_to_sql_adaptive_solver import _production_json_model

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: object()
    )

    def fake_call_openai_api(**kwargs):
        captured.update(get_llm_call_context())
        return "{}"

    monkeypatch.setattr(utils, "call_openai_api", fake_call_openai_api)

    runtime = SimpleNamespace(
        run_id="run-solver",
        verified_research_policy=SimpleNamespace(
            model_budget=SimpleNamespace(output_tokens_per_call=512)
        ),
    )
    model = _production_json_model(runtime, "profile-model", "SQL-solver", "system prompt")
    asyncio.run(model("solver prompt"))

    assert captured == {"run_id": "run-solver", "step_name": "SQL-solver"}


def test_result_review_runtime_sets_llm_call_context(monkeypatch, tmp_path):
    import agent_command
    import utils
    from dataclasses import replace

    from custom_tools.text_to_sql.adaptive._policy_config import (
        load_adaptive_policy_config,
    )
    from custom_tools.text_to_sql.adaptive.result_review import RESULT_REVIEW_RUNTIME_KEY
    from custom_tools.text_to_sql.adaptive.result_review_runtime import (
        INVALID_RESULT_REVIEW_RUNTIME,
        build_result_review_runtime,
    )
    from test_text_to_sql_pre_execution_gate import VALID_SQL
    from test_text_to_sql_result_validation_runtime import (
        _runtime_with_persisted_candidate,
    )
    from workflow.deadline import DeadlineBudget

    clock = {"now": 100.0}
    deadline = DeadlineBudget.from_duration(
        5, monotonic=lambda: clock["now"], wall_time=lambda: clock["now"]
    )
    runtime, _ = _runtime_with_persisted_candidate(tmp_path, deadline=deadline)
    runtime.verified_research_policy = load_adaptive_policy_config()
    runtime.loaded_schema = replace(
        runtime.loaded_schema,
        schema={
            "main.orders": {
                "description": "Event-specific order records.",
                "columns": {
                    "status": {"type": "TEXT", "description": "Order status."},
                    "account_id": {
                        "type": "INTEGER",
                        "constraint_type": "FK",
                        "references": "accounts.id",
                    },
                },
            },
            "main.accounts": {
                "description": "Permanent account attributes.",
                "columns": {
                    "id": {"type": "INTEGER", "constraint_type": "PK"},
                    "status": {
                        "type": "TEXT",
                        "description": "Permanent account status.",
                    },
                },
            },
        },
    )

    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda name, **kwargs: object()
    )

    captured: dict[str, object] = {}

    def fake_call_openai_api(**kwargs):
        captured.update(get_llm_call_context())
        return '{"status":"consistent","reason":"matches"}'

    monkeypatch.setattr(utils, "call_openai_api", fake_call_openai_api)

    capability = build_result_review_runtime(runtime, sql_query=VALID_SQL)
    assert capability is not INVALID_RESULT_REVIEW_RUNTIME

    response = capability.model("review this candidate")

    assert response == '{"status":"consistent","reason":"matches"}'
    assert captured == {"run_id": runtime.run_id, "step_name": RESULT_REVIEW_RUNTIME_KEY}
