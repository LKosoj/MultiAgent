"""
End-to-end regression test for text_to_sql_pipeline.yaml.

Tool- и agent-шаги мокаем на уровне `WorkflowEngine._execute_tool_step` /
`_execute_manager_with_preloaded_agents`, поэтому проверяется:
- успешная загрузка YAML;
- разрешение зависимостей между шагами (DAG);
- проброс outputs предыдущих шагов в последующие;
- финальная агрегация по секции `outputs`.

Реальные tool-функции и LLM-вызовы НЕ затрагиваются — это unit-test зоны.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from workflow.engine import WorkflowEngine
from workflow.models import StepStatus, WorkflowStatus


PIPELINE_YAML = Path("workflow_pipelines/text_to_sql_pipeline.yaml")


_TOOL_OUTPUTS: dict[str, dict[str, Any]] = {
    "nlu_processing": {
        "tokens": ["показать", "сумму", "заказов"],
        "pos_tags": ["VERB", "NOUN", "NOUN"],
    },
    "intent_extraction_step": {
        "intent": "aggregate",
        "entities": {
            "metrics": ["amount"],
            "dimensions": [],
            "filters": {},
        },
    },
    "schema_linking_step": {
        "linked_entities": {
            "metrics": [
                {"name": "amount", "table": "orders", "column": "amount"}
            ],
            "dimensions": [],
            "filters": {},
        },
        "joins": [],
        "join_success": True,
        "unlinked_entities": [],
        "schema_info": {
            "orders": {
                "description": "Orders fact table",
                "columns": {
                    "id": {"type": "INTEGER"},
                    "amount": {"type": "REAL"},
                },
            }
        },
    },
}


_MANAGER_OUTPUT: dict[str, Any] = {
    "sql_query": 'SELECT SUM("amount") AS total FROM "orders" LIMIT 10',
    "execution": {
        "success": True,
        "executed": True,
        "rows_affected": 1,
        "data": [{"total": 383.8}],
    },
    "summary": "E2E stub executed",
}


# EPIC 6.3: god-manager sql_pipeline декомпозирован на sql_generation /
# sql_verification / db_audit. Каждый шаг — отдельный agent_type, поэтому
# теперь мокаем _execute_agent_step, а не _execute_manager_with_preloaded_agents.
_AGENT_OUTPUTS: dict[str, dict[str, Any]] = {
    "sql_generation": {
        "sql": 'SELECT SUM("amount") AS total FROM "orders" LIMIT 10',
        "description": "Сумма по полю amount таблицы orders",
    },
    "sql_verification": {
        "verification_status": "Approved",
        "safety_check": {"is_safe": True, "issues": []},
        "performance_check": {"plan": "seq scan orders", "estimated_cost": 1.0, "issues": []},
        "recommendations": [],
    },
    "db_audit": dict(_MANAGER_OUTPUT),
}


@pytest.fixture
def engine_with_stubs(monkeypatch):
    """Engine с мокированными tool/agent шагами и in-memory state."""

    engine = WorkflowEngine()

    async def _fake_tool_step(step, context, task):
        if step.id not in _TOOL_OUTPUTS:
            raise AssertionError(f"unexpected tool step: {step.id}")
        return _TOOL_OUTPUTS[step.id]

    async def _fake_agent_step(step, context, task):
        if step.id not in _AGENT_OUTPUTS:
            raise AssertionError(f"unexpected agent step: {step.id}")
        return dict(_AGENT_OUTPUTS[step.id])

    monkeypatch.setattr(engine, "_execute_tool_step", _fake_tool_step)
    monkeypatch.setattr(engine, "_execute_agent_step", _fake_agent_step)

    async def _noop_checkpoint(*args, **kwargs):
        return None

    monkeypatch.setattr(
        engine.state_manager, "save_checkpoint", _noop_checkpoint
    )

    return engine


@pytest.mark.asyncio
async def test_text_to_sql_pipeline_executes_all_steps(engine_with_stubs):
    """Pipeline проходит все 4 шага и возвращает final.sql_query."""
    result = await engine_with_stubs.execute_workflow_from_yaml(
        PIPELINE_YAML,
        query="Покажи сумму заказов",
        dsn="sqlite:///:memory:",
        max_rows=10,
        session_id="e2e-sess",
        run_id="e2e-run",
        use_schema_suggestions=True,
        dry_run_only=False,
    )

    assert result.status == WorkflowStatus.COMPLETED, (
        f"workflow failed: status={result.status}, error={result.error_message}"
    )
    assert result.failed_steps == 0

    expected_steps = {
        "nlu_processing",
        "intent_extraction_step",
        "schema_linking_step",
        # EPIC 6.3: god-manager sql_pipeline -> три декомпозированных шага
        "sql_generation",
        "sql_verification",
        "db_audit",
    }
    assert expected_steps.issubset(result.step_results.keys()), (
        f"missing steps: {expected_steps - result.step_results.keys()}"
    )
    for step_id in expected_steps:
        status = result.step_results[step_id].status
        assert status in {StepStatus.COMPLETED, StepStatus.SKIPPED}, (
            f"step {step_id} ended with {status}"
        )

    final = result.final_output
    assert isinstance(final, dict)
    sql_query = final.get("final", {}).get("sql_query", "")
    assert "SELECT" in sql_query.upper(), f"final.sql_query missing: {final!r}"
    execution = final["final"]["execution"]
    assert execution["success"] is True
    assert execution["executed"] is True


@pytest.mark.asyncio
async def test_text_to_sql_pipeline_propagates_step_outputs(engine_with_stubs):
    """Каждый шаг получает outputs предыдущих шагов через WorkflowContext."""
    result = await engine_with_stubs.execute_workflow_from_yaml(
        PIPELINE_YAML,
        query="Покажи сумму заказов",
        dsn="sqlite:///:memory:",
        max_rows=10,
        session_id="e2e-sess-2",
        run_id="e2e-run-2",
        use_schema_suggestions=True,
        dry_run_only=False,
    )

    assert result.status == WorkflowStatus.COMPLETED

    final = result.final_output
    nlu = final.get("nlu") or final.get("outputs", {}).get("nlu")
    assert nlu and nlu["tokens"] == ["показать", "сумму", "заказов"]

    intent = final.get("intent") or final.get("outputs", {}).get("intent")
    assert intent and intent["entities"]["metrics"] == ["amount"]
