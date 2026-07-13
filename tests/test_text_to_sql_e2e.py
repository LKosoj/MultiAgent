"""
End-to-end regression test for text_to_sql_pipeline.yaml.

Tool- и agent-шаги мокаем на уровне `EnhancedWorkflowEngine._execute_tool_step` /
`_execute_agent_step`, поэтому проверяется:
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

from workflow.enhanced_engine import EnhancedWorkflowEngine
from workflow.models import (
    StepStatus,
    TextToSqlTerminalStatus,
    WorkflowStatus,
)


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
        "namespace_version_key": "a" * 64,
        "linked_entities": {
            "metrics": [
                {"name": "amount", "table": "orders", "column": "amount"}
            ],
            "dimensions": [],
            "filters": {},
        },
        "joins": [],
        "join_success": True,
        "sql_generation_allowed": True,
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
    "successful_sql_retrieval": {
        "status": "EMPTY",
        "examples": [],
        "context_json": "[]",
        "failed_ids": [],
        "error_code": None,
    },
}


# EPIC 6.3: god-manager sql_pipeline декомпозирован на два agent-шага
# и детерминированный db_audit tool-шаг.
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
}


@pytest.fixture
def engine_with_stubs(monkeypatch):
    """Engine с мокированными tool/agent шагами и in-memory state."""

    engine = EnhancedWorkflowEngine()
    tool_calls: list[dict[str, Any]] = []

    async def _fake_tool_step(step, context, task):
        tool_calls.append({
            "step_id": step.id,
            "tool_name": step.tool_name,
            "run_id": context.variables.get("run_id"),
            "sql_generation": context.step_outputs.get("sql_generation"),
            "sql_verification": context.step_outputs.get("sql_verification"),
        })
        if step.id == "db_audit":
            assert step.tool_name == "finalize_text_to_sql_run"
            sql = context.step_outputs["sql_generation"]["sql"]
            assert (
                context.step_outputs["sql_verification"]["verification_status"]
                == "Approved"
            )
            return {
                "run_id": context.variables["run_id"],
                "status": "succeeded",
                "reason_code": "",
                "sql": sql,
                "generated": True,
                "approved": True,
                "executed": True,
                "dry_run": False,
                "audited": True,
                "data": [{"total": 383.8}],
                "columns": ["total"],
                "rows_affected": 1,
                "error": None,
                "execution": {
                    "success": True,
                    "data": [{"total": 383.8}],
                    "columns": ["total"],
                    "rows_affected": 1,
                    "execution_time_ms": 1,
                    "error_message": None,
                    "dry_run_only": False,
                    "skipped_execution": False,
                    "sql_query": sql,
                    "applied_row_limit": 10,
                },
                "audit": {
                    "status": "logged",
                    "log_id": f"audit-{context.variables['run_id']}",
                },
                "persistence": {
                    "status": "saved",
                    "filename": "query.md",
                    "path": "/tmp/query.md",
                },
            }
        if step.id not in _TOOL_OUTPUTS:
            raise AssertionError(f"unexpected tool step: {step.id}")
        return _TOOL_OUTPUTS[step.id]

    async def _fake_agent_step(step, context, task):
        if step.id not in _AGENT_OUTPUTS:
            raise AssertionError(f"unexpected agent step: {step.id}")
        return dict(_AGENT_OUTPUTS[step.id])

    monkeypatch.setattr(engine, "_execute_tool_step", _fake_tool_step)
    monkeypatch.setattr(engine, "_execute_agent_step", _fake_agent_step)

    async def _fake_enhanced_agent_step(step, context, task, plan=None, budget=None):
        del plan, budget
        return await _fake_agent_step(step, context, task)

    monkeypatch.setattr(engine, "_execute_enhanced_agent_step", _fake_enhanced_agent_step)

    async def _noop_checkpoint(*args, **kwargs):
        return None

    monkeypatch.setattr(
        engine.state_manager, "save_checkpoint", _noop_checkpoint
    )
    engine._e2e_tool_calls = tool_calls

    return engine


@pytest.mark.asyncio
async def test_text_to_sql_pipeline_executes_all_steps(engine_with_stubs):
    """Pipeline проходит все шаги и возвращает terminal outcome."""
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
        f"workflow failed: status={result.status}, error={result.error}"
    )
    assert result.failed_steps == 0

    expected_steps = {
        "nlu_processing",
        "intent_extraction_step",
        "schema_linking_step",
        "successful_sql_retrieval",
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
    terminal = result.terminal_outcome
    assert terminal is not None
    assert terminal.status is TextToSqlTerminalStatus.SUCCEEDED
    assert terminal.sql == _AGENT_OUTPUTS["sql_generation"]["sql"]
    assert terminal.generated is True
    assert terminal.approved is True
    assert terminal.executed is True
    assert terminal.dry_run is False
    assert terminal.audited is True
    assert terminal.error is None
    assert terminal.execution["success"] is True
    assert terminal.audit["status"] == "logged"
    assert terminal.persistence["status"] == "saved"
    assert final["final"] == terminal.to_mapping()
    assert result.step_results["db_audit"].output == terminal.to_mapping()
    assert any(
        call["step_id"] == "db_audit"
        and call["tool_name"] == "finalize_text_to_sql_run"
        for call in engine_with_stubs._e2e_tool_calls
    )


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

    db_audit_call = next(
        call
        for call in engine_with_stubs._e2e_tool_calls
        if call["step_id"] == "db_audit"
    )
    assert db_audit_call["run_id"] == "e2e-run-2"
    assert db_audit_call["sql_generation"] == _AGENT_OUTPUTS["sql_generation"]
    assert db_audit_call["sql_verification"] == _AGENT_OUTPUTS["sql_verification"]
    terminal = result.terminal_outcome
    assert terminal is not None
    assert final["final"] == terminal.to_mapping()
