"""Typed engine dispatch and exactly-once finalizer integration."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql import core
from custom_tools.text_to_sql.adaptive.models import CheckKind
from test_text_to_sql_adaptive_solver import (
    _result_contradiction_receipt,
    _successful_terminal,
)
from test_text_to_sql_solver_runner import _passed_through, _runtime
from workflow.adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore
from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded
from workflow.enhanced_engine import EnhancedWorkflowEngine
from workflow.models import ResourceLimits, WorkflowContext, WorkflowDefinition, WorkflowStep
from workflow.text_to_sql_typed_runtime import (
    TextToSqlTypedAdmission,
    TextToSqlTypedRuntime,
    _ADMISSION_CAPABILITY,
)


def _context(runtime: TextToSqlTypedRuntime) -> WorkflowContext:
    context = WorkflowContext(
        workflow_id=runtime.run_id,
        session_id="session",
        variables={"run_id": runtime.run_id, "dry_run_only": False},
    )
    context._text_to_sql_typed_runtime = runtime
    return context


def _typed_runtime(*, solver_store: object) -> TextToSqlTypedRuntime:
    deadline = DeadlineBudget.from_duration(30)
    admission = TextToSqlTypedAdmission(
        run_id="run",
        run_incarnation="incarnation",
        deadline=deadline,
        query="count orders",
        dsn="sqlite:///unused.db",
        schema_scope={},
        _capability=_ADMISSION_CAPABILITY,
    )
    return TextToSqlTypedRuntime(
        run_id=admission.run_id,
        run_incarnation=admission.run_incarnation,
        deadline=deadline,
        query=admission.query,
        dsn=admission.dsn,
        schema_scope=admission.schema_scope,
        research_state_store=None,
        checkpoint_store=None,
        budget_ledger=None,
        solver_checkpoint_store=solver_store,
        _capability=_ADMISSION_CAPABILITY,
        _admission=admission,
    )


def test_pipeline_contains_only_typed_steps() -> None:
    workflow = WorkflowDefinition.from_yaml(
        "workflow_pipelines/text_to_sql_pipeline.yaml"
    )

    assert [step.id for step in workflow.steps] == [
        "schema_research",
        "sql_solving",
        "db_audit",
    ]


def test_sql_solving_dispatches_to_typed_solver(monkeypatch) -> None:
    engine = object.__new__(EnhancedWorkflowEngine)
    runtime = _typed_runtime(solver_store=object())
    calls: list[str] = []

    async def typed(*_args):
        calls.append("typed")
        return {"sql": "SELECT 1", "description": "ok"}

    monkeypatch.setattr(engine, "_execute_typed_sql_solving", typed)
    result = asyncio.run(
        engine._execute_enhanced_agent_step(
            WorkflowStep(id="sql_solving", task="solve", agent_type="sql_solver_agent"),
            _context(runtime),
            "solve",
            None,
            ResourceLimits(),
        )
    )

    assert result["sql"] == "SELECT 1"
    assert calls == ["typed"]


def _finalizer_runtime(tmp_path):
    state, research, _, loaded_schema = _runtime()
    state = _passed_through(
        state,
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(state)
    deadline = DeadlineBudget.from_duration(30)
    scope = loaded_schema.namespace.scope.to_mapping()
    admission = TextToSqlTypedAdmission(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        deadline=deadline,
        query=research.query_spec.original_text,
        dsn="postgresql://unused",
        schema_scope=scope,
        _capability=_ADMISSION_CAPABILITY,
    )
    runtime = TextToSqlTypedRuntime(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        deadline=deadline,
        query=admission.query,
        dsn=admission.dsn,
        schema_scope=scope,
        research_state_store=None,
        checkpoint_store=None,
        budget_ledger=None,
        solver_checkpoint_store=store,
        _capability=_ADMISSION_CAPABILITY,
        _admission=admission,
    )
    runtime.loaded_schema = loaded_schema
    runtime.verified_research_state = research
    runtime.verified_solver_state = state
    runtime.verified_solver_candidate_id = state.sql_candidates[-1].candidate_id
    return runtime, state, store


def _finalizer_step(state) -> WorkflowStep:
    return WorkflowStep(
        id="db_audit",
        task="finalize",
        step_type="tool",
        tool_name="finalize_text_to_sql_run",
        tool_params={
            "sql_query": state.sql_candidates[-1].sql,
            "row_limit": 10,
            "dry_run_only": False,
        },
    )


def test_finalizer_is_reserved_once_and_terminal_replay_does_not_call_it(
    monkeypatch,
    tmp_path,
) -> None:
    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, store = _finalizer_runtime(tmp_path)
    context = _context(runtime)
    calls: list[str] = []

    async def finalizer(*_args):
        calls.append("finalizer")
        return _successful_terminal(state)

    monkeypatch.setattr(engine, "_execute_on_db_audit_tool_once", finalizer)
    step = _finalizer_step(state)
    first = asyncio.run(
        engine._execute_reserved_text_to_sql_finalizer(step, context, "finalize")
    )
    second = asyncio.run(
        engine._execute_reserved_text_to_sql_finalizer(step, context, "finalize")
    )

    assert first == second
    assert calls == ["finalizer"]
    assert store.load(state.run_id, state.run_incarnation).terminal is not None


@pytest.mark.parametrize("mismatch", ("sql_query", "candidate_id"))
def test_reserved_finalizer_rejects_unverified_candidate_before_db_audit(
    monkeypatch,
    tmp_path,
    mismatch,
) -> None:
    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, _ = _finalizer_runtime(tmp_path)
    step = _finalizer_step(state)
    if mismatch == "sql_query":
        step.tool_params["sql_query"] = "SELECT 0"
    else:
        runtime.verified_solver_candidate_id = "foreign-candidate"
    calls: list[str] = []

    async def forbidden(*_args):
        calls.append("db_audit")

    monkeypatch.setattr(engine, "_execute_on_db_audit_tool_once", forbidden)

    with pytest.raises(RuntimeError, match="no durable settlement"):
        asyncio.run(
            engine._execute_reserved_text_to_sql_finalizer(step, _context(runtime), "finalize")
        )

    assert calls == []


def test_executor_dsn_mismatch_fails_closed_before_executor(
    monkeypatch,
    tmp_path,
) -> None:
    import tool_manager

    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, _ = _finalizer_runtime(tmp_path)
    step = _finalizer_step(state)
    step.tool_params.update(
        {
            "user_query": runtime.query,
            "dsn": "postgresql://different",
            "run_id": runtime.run_id,
        }
    )
    executor_calls: list[str] = []

    def forbidden_executor(*_args, **_kwargs):
        executor_calls.append("executor")

    class DirectToolManager:
        @staticmethod
        def run_tool(*, tool_function, session_id, **kwargs):
            kwargs.pop("tool_name")
            kwargs.pop("task_description")
            kwargs.pop("workflow_run_id")
            return tool_function(session_id=session_id, **kwargs)

    engine.factory = SimpleNamespace(
        _create_tool=lambda _name: core.finalize_text_to_sql_run
    )
    engine.resource_manager = SimpleNamespace(record_api_call=lambda _run_id: None)
    monkeypatch.setattr(tool_manager, "get_tool_manager", lambda: DirectToolManager())
    monkeypatch.setattr(core, "secure_db_executor", forbidden_executor)

    result = asyncio.run(
        engine._execute_reserved_text_to_sql_finalizer(
            step,
            _context(runtime),
            "finalize",
        )
    )

    assert result["reason_code"] == "DETERMINISTIC_CHECK_REJECTED"
    assert result["executed"] is False
    assert executor_calls == []


def test_pending_finalizer_resume_returns_unknown_without_call(
    monkeypatch,
    tmp_path,
) -> None:
    from workflow.text_to_sql_adaptive_solver import prepare_finalizer_execution

    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, _ = _finalizer_runtime(tmp_path)
    context = _context(runtime)
    prepare_finalizer_execution(
        runtime,
        {
            "operation": "finalize_text_to_sql_run",
            **_finalizer_step(state).tool_params,
        },
        id_factory=lambda: "reserved-before-crash",
    )

    async def forbidden(*_args):
        raise AssertionError("pending finalizer must not run again")

    monkeypatch.setattr(engine, "_execute_on_db_audit_tool_once", forbidden)
    terminal = asyncio.run(
        engine._execute_reserved_text_to_sql_finalizer(
            _finalizer_step(state), context, "finalize"
        )
    )

    assert terminal["reason_code"] == "EXECUTION_UNKNOWN"


@pytest.mark.parametrize(
    ("commit_phase", "cancel_twice"),
    (("before", False), ("after", False), ("before", True)),
)
def test_cancellation_waits_for_known_reconciliation_and_reraises(
    monkeypatch,
    tmp_path,
    commit_phase,
    cancel_twice,
) -> None:
    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, store = _finalizer_runtime(tmp_path)
    context = _context(runtime)
    entered = threading.Event()
    release = threading.Event()
    known_committed = threading.Event()
    original = store.reconcile_execution_terminal

    def blocked_reconciliation(*args, outcome, **kwargs):
        if outcome == "KNOWN":
            if commit_phase == "before":
                entered.set()
                assert release.wait(timeout=10)
            checkpoint = original(*args, outcome=outcome, **kwargs)
            known_committed.set()
            if commit_phase == "after":
                entered.set()
                assert release.wait(timeout=10)
            return checkpoint
        assert known_committed.wait(timeout=10)
        return original(*args, outcome=outcome, **kwargs)

    async def finalizer(*_args):
        return _successful_terminal(state)

    monkeypatch.setattr(store, "reconcile_execution_terminal", blocked_reconciliation)
    monkeypatch.setattr(engine, "_execute_on_db_audit_tool_once", finalizer)

    async def run_case():
        task = asyncio.create_task(
            engine._execute_reserved_text_to_sql_finalizer(
                _finalizer_step(state), context, "finalize"
            )
        )
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel("cancel-finalizer")
        if cancel_twice:
            task.cancel("cancel-finalizer-again")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_case())
    checkpoint = store.load(state.run_id, state.run_incarnation)
    assert checkpoint is not None and checkpoint.terminal is not None


def test_deadline_from_finalizer_records_unknown_then_reraises(
    monkeypatch,
    tmp_path,
) -> None:
    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, store = _finalizer_runtime(tmp_path)
    context = _context(runtime)

    async def finalizer(*_args):
        raise WorkflowDeadlineExceeded("deadline crossed inside finalizer")

    monkeypatch.setattr(engine, "_execute_on_db_audit_tool_once", finalizer)

    with pytest.raises(WorkflowDeadlineExceeded, match="inside finalizer"):
        asyncio.run(
            engine._execute_reserved_text_to_sql_finalizer(
                _finalizer_step(state), context, "finalize"
            )
        )

    checkpoint = store.load(state.run_id, state.run_incarnation)
    assert checkpoint is not None and checkpoint.terminal is not None
    assert b'"reason_code":"EXECUTION_UNKNOWN"' in checkpoint.terminal.terminal_bytes


def test_result_contradiction_dispatches_open_checkpoint_without_terminal_apply(
    monkeypatch,
    tmp_path,
) -> None:
    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, _ = _finalizer_runtime(tmp_path)
    context = _context(runtime)
    step = _finalizer_step(state)
    terminal = _successful_terminal(state)
    captured = []

    async def finalizer(*_args):
        return _result_contradiction_receipt(state).model_dump(mode="json")

    async def continuation(
        continuation_step,
        continuation_context,
        continuation_task,
    ):
        captured.append(
            (continuation_step, continuation_context, continuation_task)
        )
        return terminal

    monkeypatch.setattr(engine, "_execute_on_db_audit_tool_once", finalizer)
    monkeypatch.setattr(
        engine,
        "_continue_result_contradiction_finalizer",
        continuation,
        raising=False,
    )

    result = asyncio.run(
        engine._execute_reserved_text_to_sql_finalizer(step, context, "finalize")
    )

    assert result == terminal
    assert captured == [(step, context, "finalize")]


def test_fresh_runtime_dispatches_durable_result_contradiction_without_gate(
    monkeypatch,
    tmp_path,
) -> None:
    from workflow.text_to_sql_adaptive_solver import (
        prepare_finalizer_execution,
        reconcile_known_finalizer,
    )

    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, store = _finalizer_runtime(tmp_path)
    step = _finalizer_step(state)
    prepared = prepare_finalizer_execution(
        runtime,
        {"operation": "finalize_text_to_sql_run", **step.tool_params},
        id_factory=lambda: "result-contradiction-finalizer",
    )
    assert prepared.reservation is not None
    checkpoint = reconcile_known_finalizer(
        store,
        prepared.reservation,
        prepared.state,
        _result_contradiction_receipt(state).model_dump(mode="json"),
    )
    assert checkpoint.terminal is None
    runtime.loaded_schema = None
    runtime.verified_research_state = None
    runtime.verified_research_outcome = None
    runtime.verified_research_policy = None
    runtime.verified_solver_state = None
    runtime.verified_solver_candidate_id = None
    runtime.verified_solver_terminal = None
    context = _context(runtime)
    terminal = _successful_terminal(state)
    captured = []

    async def continuation(
        continuation_step,
        continuation_context,
        continuation_task,
    ):
        captured.append(
            (continuation_step, continuation_context, continuation_task)
        )
        assert runtime.loaded_schema is None
        assert runtime.verified_research_state is None
        assert runtime.verified_solver_state is None
        assert runtime.verified_solver_candidate_id is None
        return terminal

    monkeypatch.setattr(
        engine,
        "_continue_result_contradiction_finalizer",
        continuation,
        raising=False,
    )

    result = asyncio.run(engine._execute_tool_step(step, context, "finalize"))

    assert result == terminal
    assert captured == [(step, context, "finalize")]


def test_result_contradiction_hydration_returns_research_terminal_before_solver(
    monkeypatch,
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.adaptive.models import ResearchStopReason
    from custom_tools.text_to_sql.adaptive.research_loop import ResearchLoopOutcome
    from custom_tools.text_to_sql.adaptive.terminal import research_stop_terminal_result
    import workflow.text_to_sql_typed_research as typed_research

    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, _ = _finalizer_runtime(tmp_path)
    research = runtime.verified_research_state
    loaded_schema = runtime.loaded_schema
    runtime.loaded_schema = None
    runtime.verified_research_state = None
    runtime.verified_research_outcome = None
    runtime.verified_research_policy = None
    context = _context(runtime)
    calls: list[str] = []

    async def hydrate(hydrated_runtime):
        assert hydrated_runtime is runtime
        runtime.loaded_schema = loaded_schema
        runtime.verified_research_state = research
        runtime.verified_research_policy = object()
        runtime.verified_research_outcome = ResearchLoopOutcome(
            final_state=research,
            stop_reason=ResearchStopReason.BUDGET_EXHAUSTED,
            affected_source_ids=(),
            citation_evidence_ids=(),
            ambiguity=None,
        )
        return {"ready_for_sql": False}

    async def forbidden_solver(*_args):
        calls.append("solver")
        raise AssertionError("non-ready research must not enter the SQL solver")

    monkeypatch.setattr(typed_research, "run_typed_schema_research", hydrate)
    monkeypatch.setattr(engine, "_execute_typed_sql_solving", forbidden_solver)

    result = asyncio.run(
        engine._continue_result_contradiction_finalizer(
            _finalizer_step(state),
            context,
            "finalize",
        )
    )

    expected = research_stop_terminal_result(
        runtime.run_id,
        ResearchStopReason.BUDGET_EXHAUSTED,
        None,
    )
    assert result == expected.to_mapping()
    assert calls == []
