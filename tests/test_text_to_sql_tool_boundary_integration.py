from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


class _Span:
    def set_attributes(self, _attributes):
        pass

    def set_attribute(self, _key, _value):
        pass

    def end(self):
        pass


class _Telemetry:
    def __init__(self, calls):
        self.calls = calls

    def start_run_trace(self, **kwargs):
        self.calls.append(("start", kwargs))
        return _Span()

    def finish_run_trace(self, _span, **kwargs):
        self.calls.append(("finish", kwargs))


@pytest.mark.parametrize(
    (
        "mode",
        "executor_success",
        "audit_status",
        "expected_status",
        "expected_executed",
        "expected_audited",
    ),
    [
        ("success", True, "logged", "succeeded", True, True),
        ("execution_failure", False, "logged", "failed", True, True),
        ("audit_failure", True, "error", "failed", True, False),
    ],
)
def test_finalizer_crosses_real_tool_loader_manager_and_engine_boundary(
    monkeypatch,
    tmp_path,
    mode,
    executor_success,
    audit_status,
    expected_status,
    expected_executed,
    expected_audited,
):
    mcp_config = tmp_path / "mcp_servers.json"
    mcp_config.write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(mcp_config))

    from agent_factory import AgentFactory
    from custom_tools.text_to_sql import core
    import telemetry
    from tool_manager import get_tool_manager
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import (
        StepResult,
        StepStatus,
        WorkflowContext,
        WorkflowDefinition,
    )

    telemetry_calls = []
    monkeypatch.setattr(
        telemetry,
        "get_telemetry_manager",
        lambda: _Telemetry(telemetry_calls),
    )

    factory = AgentFactory()
    registered_tool = factory._create_tool("finalize_text_to_sql_run")
    assert registered_tool is factory.tool_mapping["finalize_text_to_sql_run"]
    assert registered_tool.name == "finalize_text_to_sql_run"

    calls = {}

    def execute(sql_query, row_limit=100, dsn=None, *, dry_run_only=None):
        calls["execute"] = {
            "sql_query": sql_query,
            "row_limit": row_limit,
            "dsn": dsn,
            "dry_run_only": dry_run_only,
        }
        return {
            "success": executor_success,
            "data": [{"order_count": 3}] if executor_success else [],
            "columns": ["order_count"] if executor_success else [],
            "rows_affected": 0,
            "execution_time_ms": 3,
                "error_message": None if executor_success else "database unavailable",
                "dry_run_only": False,
                "skipped_execution": False,
                "sql_query": sql_query,
                "applied_row_limit": row_limit,
        }

    def audit(entry):
        calls["audit"] = entry
        if audit_status == "logged":
            return {"status": "logged", "log_id": "audit-boundary-9"}
        return {"status": "error", "error": "audit unavailable"}

    def persist(**kwargs):
        assert mode == "success"
        calls["persist"] = kwargs
        return {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
        }

    monkeypatch.setattr(core, "secure_db_executor", execute)
    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(core, "save_successful_sql", persist)

    workflow = WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    finalizer_step = next(step for step in workflow.steps if step.id == "db_audit")

    engine = object.__new__(EnhancedWorkflowEngine)
    engine.factory = factory
    engine.policy_engine = SimpleNamespace(get_budget=lambda *args: None)
    resource_calls = []
    engine.resource_manager = SimpleNamespace(
        record_api_call=lambda workflow_id: resource_calls.append(workflow_id)
    )

    generated_sql = "SELECT COUNT(*) AS order_count FROM orders"
    generation_output = {
        "sql": generated_sql,
        "description": "Count all orders",
    }
    verification_output = {
        "verification_status": "Approved",
        "safety_check": "ok",
        "performance_check": "ok",
        "recommendations": [],
    }
    namespace_version_key = "a" * 64
    context = WorkflowContext(
        workflow_id=f"workflow-boundary-9-{mode}",
        session_id=f"session-boundary-9-{mode}",
        variables={
            **workflow.inputs,
            "query": "How many orders are there?",
            "dsn": "sqlite:///boundary.db",
            "max_rows": 37,
            "dry_run_only": False,
            "run_id": f"run-boundary-9-{mode}",
            "session_id": f"session-boundary-9-{mode}",
        },
        step_outputs={
            "schema_linking_step": {
                "namespace_version_key": namespace_version_key,
            },
            "schema_linking_step.namespace_version_key": namespace_version_key,
            "sql_generation": generation_output,
            "sql_generation.sql": generated_sql,
            "sql_verification": verification_output,
            "sql_verification.verification_status": "Approved",
        },
    )

    finalizer_result = asyncio.run(
        engine._execute_step_with_policy(
            finalizer_step,
            context,
            plan=None,
            attempt=1,
        )
    )
    assert finalizer_result.status.value == StepStatus.COMPLETED.value

    assert calls["execute"] == {
        "sql_query": generated_sql,
        "row_limit": 37,
        "dsn": "sqlite:///boundary.db",
        "dry_run_only": False,
    }
    assert calls["audit"]["session_id"] == f"session-boundary-9-{mode}"
    assert calls["audit"]["run_id"] == f"run-boundary-9-{mode}"
    assert "sql_query" not in calls["audit"]
    assert "user_query" not in calls["audit"]
    execution_summary = calls["audit"]["execution"]
    assert execution_summary["sql_sha256"] == hashlib.sha256(
        generated_sql.encode("utf-8")
    ).hexdigest()
    assert execution_summary["query_sha256"] == hashlib.sha256(
        b"How many orders are there?"
    ).hexdigest()
    if mode == "success":
        assert json.loads(calls["persist"]["execution_result"]) == execution_summary
        assert calls["persist"]["sql_query"] == generated_sql
        assert calls["persist"]["user_query"] == "How many orders are there?"
        assert execution_summary["execution_time_ms"] == 3
        assert generated_sql not in calls["persist"]["execution_result"]
        assert "How many orders are there?" not in calls["persist"]["execution_result"]
    else:
        assert "persist" not in calls
    assert resource_calls == [f"workflow-boundary-9-{mode}"]

    start_trace = next(call for call in telemetry_calls if call[0] == "start")
    assert start_trace[1]["session_id"] == f"session-boundary-9-{mode}"
    tool_invocation_id = start_trace[1]["run_id"]
    assert tool_invocation_id.startswith("tool-")
    assert tool_invocation_id != f"run-boundary-9-{mode}"

    tool_run = get_tool_manager().get_tool_status(f"run-boundary-9-{mode}")
    assert tool_run is not None
    assert tool_run["tool_invocation_id"] == tool_invocation_id
    assert tool_run["workflow_run_id"] == f"run-boundary-9-{mode}"
    assert tool_run["tool_name"] == "finalize_text_to_sql_run"
    assert tool_run["status"] == "completed"

    now = datetime.now()
    step_results = {
        "sql_generation": StepResult(
            step_id="sql_generation",
            status=StepStatus.COMPLETED,
            output=generation_output,
            start_time=now,
            end_time=now,
        ),
        "sql_verification": StepResult(
            step_id="sql_verification",
            status=StepStatus.COMPLETED,
            output=verification_output,
            start_time=now,
            end_time=now,
        ),
        "db_audit": finalizer_result,
    }

    outcome = engine._derive_text_to_sql_terminal_outcome(
        workflow,
        context,
        step_results,
    )

    assert outcome.status.value == expected_status
    assert outcome.run_id == f"run-boundary-9-{mode}"
    assert outcome.sql == generated_sql
    assert outcome.generated is True
    assert outcome.approved is True
    assert outcome.executed is expected_executed
    assert outcome.dry_run is False
    assert outcome.audited is expected_audited
    assert outcome.data == (
        [{"order_count": 3}] if executor_success else []
    )
    assert outcome.columns == (["order_count"] if executor_success else [])
    if mode == "execution_failure":
        assert outcome.reason_code == "EXECUTION_FAILED"
        assert outcome.execution["success"] is False
        assert outcome.persistence == {"status": "not_attempted"}
    elif mode == "audit_failure":
        assert outcome.reason_code == "AUDIT_FAILED"
    if mode == "success":
        assert outcome.persistence == {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
        }
    else:
        assert outcome.persistence == {"status": "not_attempted"}


@pytest.mark.parametrize(
    "failed_retry_step",
    [None, "sql_generation", "sql_verification"],
)
def test_execution_failure_corrective_retry_crosses_real_boundary_once(
    monkeypatch,
    tmp_path,
    failed_retry_step,
):
    """The one allowed execution-feedback retry uses the real finalizer boundary."""
    mcp_config = tmp_path / "mcp_servers.json"
    mcp_config.write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(mcp_config))

    from agent_factory import AgentFactory
    from custom_tools.text_to_sql import core
    import telemetry
    from tool_manager import get_tool_manager
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import (
        StepResult,
        StepStatus,
        WorkflowContext,
        WorkflowDefinition,
    )

    telemetry_calls = []
    monkeypatch.setattr(
        telemetry,
        "get_telemetry_manager",
        lambda: _Telemetry(telemetry_calls),
    )

    factory = AgentFactory()
    registered_tool = factory._create_tool("finalize_text_to_sql_run")
    assert registered_tool is factory.tool_mapping["finalize_text_to_sql_run"]

    broken_sql = "SELECT missing_column FROM orders"
    corrected_sql = "SELECT COUNT(*) AS order_count FROM orders"
    execution_calls = []
    audit_calls = []
    persistence_calls = []

    def execute(sql_query, row_limit=100, dsn=None, *, dry_run_only=None):
        execution_calls.append(sql_query)
        succeeded = len(execution_calls) == 2
        return {
            "success": succeeded,
            "data": [{"order_count": 3}] if succeeded else [],
            "columns": ["order_count"] if succeeded else [],
            "rows_affected": 0,
            "execution_time_ms": 3,
                "error_message": None if succeeded else "no such column: missing_column",
                "dry_run_only": False,
                "skipped_execution": False,
                "sql_query": sql_query,
                "applied_row_limit": row_limit,
        }

    def audit(entry):
        audit_calls.append(entry)
        return {"status": "logged", "log_id": f"audit-retry-{len(audit_calls)}"}

    def persist(**kwargs):
        persistence_calls.append(kwargs)
        return {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
        }

    monkeypatch.setattr(core, "secure_db_executor", execute)
    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(core, "save_successful_sql", persist)

    workflow = WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    generation_step = next(step for step in workflow.steps if step.id == "sql_generation")
    verification_step = next(
        step for step in workflow.steps if step.id == "sql_verification"
    )
    finalizer_step = next(step for step in workflow.steps if step.id == "db_audit")

    engine = object.__new__(EnhancedWorkflowEngine)
    engine.factory = factory
    engine.policy_engine = SimpleNamespace(get_budget=lambda *args: None)
    engine.resource_manager = SimpleNamespace(record_api_call=lambda _workflow_id: None)

    class _StateManager:
        async def save_checkpoint(self, **_kwargs):
            return None

    engine.state_manager = _StateManager()

    run_id = f"run-boundary-retry-{failed_retry_step or 'success'}"
    session_id = f"session-boundary-retry-{failed_retry_step or 'success'}"
    generation_output = {"sql": broken_sql, "description": "initial SQL"}
    verification_output = {
        "verification_status": "Approved",
        "safety_check": "ok",
        "performance_check": "ok",
        "recommendations": [],
    }
    namespace_version_key = "a" * 64
    context = WorkflowContext(
        workflow_id=f"workflow-{run_id}",
        session_id=session_id,
        variables={
            **workflow.inputs,
            "query": "How many orders are there?",
            "dsn": "sqlite:///boundary.db",
            "max_rows": 37,
            "dry_run_only": False,
            "run_id": run_id,
            "session_id": session_id,
        },
        step_outputs={
            "schema_linking_step": {
                "namespace_version_key": namespace_version_key,
            },
            "schema_linking_step.namespace_version_key": namespace_version_key,
            "sql_generation": generation_output,
            "sql_generation.sql": broken_sql,
            "sql_verification": verification_output,
            "sql_verification.verification_status": "Approved",
        },
    )
    now = datetime.now()
    previous_results = {
        "sql_generation": StepResult(
            step_id="sql_generation",
            status=StepStatus.COMPLETED,
            output=generation_output,
            start_time=now,
            end_time=now,
        ),
        "sql_verification": StepResult(
            step_id="sql_verification",
            status=StepStatus.COMPLETED,
            output=verification_output,
            start_time=now,
            end_time=now,
        ),
    }

    initial_result = asyncio.run(
        engine._execute_step_with_policy(finalizer_step, context, plan=None, attempt=1)
    )
    assert initial_result.status.value == StepStatus.COMPLETED.value
    assert initial_result.output["reason_code"] == "EXECUTION_FAILED"
    previous_results["db_audit"] = initial_result

    rerun_calls = []

    async def execute_corrective_step(step, retry_context, _previous_results):
        rerun_calls.append(step.id)
        feedback = retry_context.variables["sql_execution_feedback"]
        assert feedback["failure_code"] == "EXECUTION_FAILED"
        assert feedback["previous_sql"] == broken_sql
        if step.id == failed_retry_step:
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                error=f"{step.id} corrective rerun failed",
            )
        if step is generation_step:
            return StepResult(
                step_id=step.id,
                status=StepStatus.COMPLETED,
                output={"sql": corrected_sql, "description": "corrected SQL"},
            )
        if step is verification_step:
            assert retry_context.step_outputs["sql_generation.sql"] == corrected_sql
            return StepResult(
                step_id=step.id,
                status=StepStatus.COMPLETED,
                output=verification_output,
            )
        assert step is finalizer_step
        return await engine._execute_step_with_policy(
            step,
            retry_context,
            plan=None,
            attempt=2,
        )

    engine._execute_enhanced_step = execute_corrective_step
    result = asyncio.run(
        engine._complete_enhanced_step_with_output_retry(
            finalizer_step,
            initial_result,
            context,
            workflow,
            previous_results,
        )
    )

    if failed_retry_step is None:
        assert rerun_calls == ["sql_generation", "sql_verification", "db_audit"]
        assert result.status.value == StepStatus.COMPLETED.value
        assert result.output["status"] == "succeeded"
        assert result.output["sql"] == corrected_sql
        assert execution_calls == [broken_sql, corrected_sql]
        assert len(audit_calls) == 2
        assert len(persistence_calls) == 1
        outcome = engine._derive_text_to_sql_terminal_outcome(
            workflow,
            context,
            previous_results,
        )
        assert outcome.status.value == "succeeded"
        assert outcome.sql == corrected_sql
    else:
        expected_calls = (
            ["sql_generation"]
            if failed_retry_step == "sql_generation"
            else ["sql_generation", "sql_verification"]
        )
        assert rerun_calls == expected_calls
        assert result.status.value == StepStatus.FAILED.value
        assert result.error_class == "output_retry_chain_failed"
        assert execution_calls == [broken_sql]
        assert not persistence_calls
        outcome = engine._derive_text_to_sql_terminal_outcome(
            workflow,
            context,
            previous_results,
        )
        assert outcome.status.value == "failed"
        assert outcome.reason_code == "OUTPUT_RETRY_CHAIN_FAILED"

    start_calls = [
        payload
        for event, payload in telemetry_calls
        if event == "start" and payload["agent_name"] == "finalize_text_to_sql_run"
    ]
    expected_tool_calls = 2 if failed_retry_step is None else 1
    assert len(start_calls) == expected_tool_calls
    invocation_ids = [payload["run_id"] for payload in start_calls]
    assert len(set(invocation_ids)) == expected_tool_calls
    assert all(payload["session_id"] == session_id for payload in start_calls)

    tool_runs = [
        run
        for run in get_tool_manager().active_runs.values()
        if run.get("workflow_run_id") == run_id
    ]
    assert len(tool_runs) == expected_tool_calls
    assert {run["tool_invocation_id"] for run in tool_runs} == set(invocation_ids)


def test_non_execution_terminal_failure_does_not_cross_corrective_boundary(
    monkeypatch,
    tmp_path,
):
    """An audit failure is terminal and must not trigger generation or execution again."""
    mcp_config = tmp_path / "mcp_servers.json"
    mcp_config.write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(mcp_config))

    from agent_factory import AgentFactory
    from custom_tools.text_to_sql import core
    from workflow.enhanced_engine import EnhancedWorkflowEngine
    from workflow.models import StepResult, StepStatus, WorkflowContext, WorkflowDefinition

    factory = AgentFactory()
    factory._create_tool("finalize_text_to_sql_run")
    execution_calls = []

    def execute(sql_query, row_limit=100, dsn=None, *, dry_run_only=None):
        execution_calls.append(sql_query)
        return {
            "success": True,
            "data": [{"value": 1}],
            "columns": ["value"],
            "rows_affected": 0,
            "execution_time_ms": 1,
                "error_message": None,
                "dry_run_only": False,
                "skipped_execution": False,
                "sql_query": sql_query,
                "applied_row_limit": row_limit,
        }

    monkeypatch.setattr(core, "secure_db_executor", execute)
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda _entry: {"status": "error", "error": "audit unavailable"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda **_kwargs: pytest.fail("audit failure must not persist SQL"),
    )

    workflow = WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    finalizer_step = next(step for step in workflow.steps if step.id == "db_audit")
    engine = object.__new__(EnhancedWorkflowEngine)
    engine.factory = factory
    engine.policy_engine = SimpleNamespace(get_budget=lambda *args: None)
    engine.resource_manager = SimpleNamespace(record_api_call=lambda _workflow_id: None)

    class _StateManager:
        async def save_checkpoint(self, **_kwargs):
            return None

    engine.state_manager = _StateManager()
    namespace_version_key = "a" * 64
    context = WorkflowContext(
        workflow_id="workflow-audit-no-retry",
        session_id="session-audit-no-retry",
        variables={
            **workflow.inputs,
            "query": "Return one",
            "dsn": "sqlite:///boundary.db",
            "max_rows": 10,
            "dry_run_only": False,
            "run_id": "run-audit-no-retry",
            "session_id": "session-audit-no-retry",
        },
        step_outputs={
            "schema_linking_step": {
                "namespace_version_key": namespace_version_key,
            },
            "schema_linking_step.namespace_version_key": namespace_version_key,
            "sql_generation": {"sql": "SELECT 1", "description": "one"},
            "sql_generation.sql": "SELECT 1",
            "sql_verification": {
                "verification_status": "Approved",
                "safety_check": "ok",
                "performance_check": "ok",
                "recommendations": [],
            },
            "sql_verification.verification_status": "Approved",
        },
    )
    now = datetime.now()
    previous_results = {
        "sql_generation": StepResult(
            step_id="sql_generation",
            status=StepStatus.COMPLETED,
            output=context.step_outputs["sql_generation"],
            start_time=now,
            end_time=now,
        ),
        "sql_verification": StepResult(
            step_id="sql_verification",
            status=StepStatus.COMPLETED,
            output=context.step_outputs["sql_verification"],
            start_time=now,
            end_time=now,
        ),
    }
    initial_result = asyncio.run(
        engine._execute_step_with_policy(finalizer_step, context, plan=None, attempt=1)
    )
    assert initial_result.output["reason_code"] == "AUDIT_FAILED"
    previous_results["db_audit"] = initial_result

    async def forbidden_corrective_step(*_args, **_kwargs):
        pytest.fail("non-execution failure must not enter corrective retry chain")

    engine._execute_enhanced_step = forbidden_corrective_step
    result = asyncio.run(
        engine._complete_enhanced_step_with_output_retry(
            finalizer_step,
            initial_result,
            context,
            workflow,
            previous_results,
        )
    )

    assert result is initial_result
    assert execution_calls == ["SELECT 1"]
