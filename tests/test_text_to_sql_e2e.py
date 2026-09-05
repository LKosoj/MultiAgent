"""End-to-end regression test for the Typed Text-to-SQL YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from workflow.deadline import DeadlineBudget
from workflow.enhanced_engine import EnhancedWorkflowEngine
from workflow.models import (
    StepStatus,
    TextToSqlTerminalStatus,
    WorkflowContext,
    WorkflowStatus,
)
from workflow.text_to_sql_typed_runtime import (
    TextToSqlTypedAdmission,
    TextToSqlTypedRuntime,
    _ADMISSION_CAPABILITY,
)


PIPELINE_YAML = Path("workflow_pipelines/text_to_sql_pipeline.yaml")
SQL = 'SELECT SUM("amount") AS total FROM "orders" LIMIT 10'


@pytest.fixture
def engine_with_stubs(monkeypatch):
    engine = EnhancedWorkflowEngine()
    calls: list[dict[str, Any]] = []

    def install_runtime(context: WorkflowContext) -> None:
        deadline = context._deadline_budget
        admission = TextToSqlTypedAdmission(
            run_id=context.variables["run_id"],
            run_incarnation="e2e-incarnation",
            deadline=deadline,
            query=context.variables["query"],
            dsn=context.variables["dsn"],
            schema_scope={},
            _capability=_ADMISSION_CAPABILITY,
        )
        context._text_to_sql_typed_runtime = TextToSqlTypedRuntime(
            run_id=admission.run_id,
            run_incarnation=admission.run_incarnation,
            deadline=deadline,
            query=admission.query,
            dsn=admission.dsn,
            schema_scope=admission.schema_scope,
            research_state_store=None,
            checkpoint_store=None,
            budget_ledger=None,
            solver_checkpoint_store=object(),
            _capability=_ADMISSION_CAPABILITY,
            _admission=admission,
        )

    async def fake_tool(step, context, _task):
        calls.append({"step_id": step.id, "outputs": dict(context.step_outputs)})
        if step.id == "schema_research":
            return {
                "stop_reason": "COMPLETE",
                "ready_for_sql": True,
                "terminal_reason_code": None,
                "schema_namespace_version": "sha256:" + "a" * 64,
                "namespace_version_key": "a" * 64,
            }
        if step.id == "db_audit":
            assert context.step_outputs["sql_solving"]["sql"] == SQL
            return {
                "run_id": context.variables["run_id"],
                "status": "succeeded",
                "reason_code": "",
                "sql": SQL,
                "generated": True,
                "approved": True,
                "executed": True,
                "dry_run": False,
                "audited": True,
                "data": [[383.8]],
                "columns": ["total"],
                "rows_affected": 1,
                "error": None,
                "ambiguity": None,
                "execution": {
                    "success": True,
                    "sql_query": SQL,
                    "data": [[383.8]],
                    "columns": ["total"],
                    "rows_affected": 1,
                    "execution_time_ms": 1,
                    "dry_run_only": False,
                    "skipped_execution": False,
                    "applied_row_limit": 10,
                },
                "audit": {"status": "logged", "log_id": "audit-e2e"},
                "persistence": {
                    "status": "saved",
                    "filename": "query.md",
                    "path": "/tmp/query.md",
                },
                "result_review": {},
                "provenance": {},
            }
        raise AssertionError(f"unexpected tool step: {step.id}")

    async def fake_agent(step, context, _task, plan=None, budget=None):
        del plan, budget
        assert step.id == "sql_solving"
        assert context.step_outputs["schema_research"]["ready_for_sql"] is True
        return {"sql": SQL, "description": "sum orders"}

    async def noop_checkpoint(*_args, **_kwargs):
        return None

    monkeypatch.setattr(engine, "_install_text_to_sql_typed_runtime", install_runtime)
    monkeypatch.setattr(engine, "_execute_tool_step", fake_tool)
    monkeypatch.setattr(engine, "_execute_enhanced_agent_step", fake_agent)
    monkeypatch.setattr(engine.state_manager, "save_checkpoint", noop_checkpoint)
    engine._e2e_calls = calls
    return engine


@pytest.mark.asyncio
async def test_typed_pipeline_executes_its_three_steps(engine_with_stubs) -> None:
    context = WorkflowContext(session_id="e2e-session")
    context._deadline_budget = DeadlineBudget.from_duration(30)

    result = await engine_with_stubs.execute_workflow_from_yaml(
        PIPELINE_YAML,
        context=context,
        query="Покажи сумму заказов",
        dsn="sqlite:///:memory:",
        max_rows=10,
        session_id="e2e-session",
        run_id="e2e-run",
        schema_scope={},
        dry_run_only=False,
    )

    assert result.status is WorkflowStatus.COMPLETED
    assert list(result.step_results) == [
        "schema_research",
        "sql_solving",
        "db_audit",
    ]
    assert all(
        step.status is StepStatus.COMPLETED for step in result.step_results.values()
    )
    assert result.terminal_outcome is not None
    assert result.terminal_outcome.status is TextToSqlTerminalStatus.SUCCEEDED
    assert result.terminal_outcome.sql == SQL
    assert result.final_output["final"] == result.terminal_outcome.to_mapping()
    assert [call["step_id"] for call in engine_with_stubs._e2e_calls] == [
        "schema_research",
        "db_audit",
    ]
