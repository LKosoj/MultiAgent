"""Typed engine dispatch and exactly-once finalizer integration."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql import core
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    ResearchReentryStatus,
)
from custom_tools.text_to_sql.adaptive.result_review import ResultReviewReceipt
from test_text_to_sql_adaptive_solver import (
    _persisted_result_contradiction_checkpoint,
    _result_contradiction_receipt,
    _successful_terminal,
)
from test_text_to_sql_solver_runner import _passed_through, _runtime
from custom_tools.text_to_sql.adaptive.solver_loop import (
    admit_targeted_reentry,
    finalize_targeted_reentry,
)
from workflow.adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore
from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded
from workflow.enhanced_engine import EnhancedWorkflowEngine
from workflow.models import ResourceLimits, WorkflowContext, WorkflowDefinition, WorkflowStep
from workflow.text_to_sql_adaptive_solver import run_adaptive_sql_generation
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


def test_finalizer_ordinary_success_path_threads_namespace_version_key(
    monkeypatch,
    tmp_path,
) -> None:
    """W2-2.3 gap fix: the *ordinary* (non-fallback) success path — reservation
    followed by ``_execute_on_db_audit_tool_once`` — must also thread the run's
    schema namespace version key into the ``finalize_text_to_sql_run`` tool
    call. Previously only the semantic-repair fallback branch
    (``prepared.verified_execution``) passed it, so ``successful_sql`` memory
    was never populated on normal runs.
    """
    import tool_manager

    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, _store = _finalizer_runtime(tmp_path)
    step = _finalizer_step(state)
    original_tool_params = dict(step.tool_params)
    step.tool_params.update(
        {
            "user_query": runtime.query,
            "dsn": runtime.dsn,
            "run_id": runtime.run_id,
        }
    )
    captured: dict = {}

    def fake_finalize_text_to_sql_run(*_args, **kwargs):
        captured.update(kwargs)
        return _successful_terminal(state)

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
    monkeypatch.setattr(core, "finalize_text_to_sql_run", fake_finalize_text_to_sql_run)

    result = asyncio.run(
        engine._execute_reserved_text_to_sql_finalizer(
            step, _context(runtime), "finalize"
        )
    )

    expected_key = runtime.verified_research_state.schema_namespace_version.split(
        ":", 1
    )[-1]
    assert expected_key == runtime.loaded_schema.namespace.version_key
    assert captured["namespace_version_key"] == expected_key
    assert result["status"] == "succeeded"
    # The reservation/CAS ``request`` dict (built by ``_adaptive_finalizer_request``)
    # keeps its pinned 4-key shape after the call — namespace_version_key must
    # not leak into ``step.tool_params`` on the original step object, since a
    # replay/retry re-reads it from there and an extra key would change the
    # persisted ``request_digest`` used for durable resume.
    assert dict(step.tool_params) == {
        **original_tool_params,
        "user_query": runtime.query,
        "dsn": runtime.dsn,
        "run_id": runtime.run_id,
    }


def test_finalizer_ordinary_success_path_still_threads_key_when_memory_disabled(
    monkeypatch,
    tmp_path,
) -> None:
    """Even in the canonical public-benchmark environment (memory writes
    disabled), the engine must still thread a valid namespace key through —
    ``successful_sql_memory``/``_audit.save_successful_sql`` own the
    ``TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED`` gate and no-op the write
    themselves. The engine has no business re-checking that flag; duplicating
    the gate at this layer would just create a second place to get it wrong.
    """
    import tool_manager

    monkeypatch.setenv("TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED", "0")
    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, _store = _finalizer_runtime(tmp_path)
    step = _finalizer_step(state)
    step.tool_params.update(
        {
            "user_query": runtime.query,
            "dsn": runtime.dsn,
            "run_id": runtime.run_id,
        }
    )
    captured: dict = {}

    def fake_finalize_text_to_sql_run(*_args, **kwargs):
        captured.update(kwargs)
        return _successful_terminal(state)

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
    monkeypatch.setattr(core, "finalize_text_to_sql_run", fake_finalize_text_to_sql_run)

    asyncio.run(
        engine._execute_reserved_text_to_sql_finalizer(
            step, _context(runtime), "finalize"
        )
    )

    expected_key = runtime.verified_research_state.schema_namespace_version.split(
        ":", 1
    )[-1]
    assert captured["namespace_version_key"] == expected_key


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


@pytest.mark.parametrize("crash_before_terminal", (False, True))
def test_verified_semantic_repair_execution_is_durable_and_never_reexecutes(
    monkeypatch,
    tmp_path,
    crash_before_terminal,
) -> None:
    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, store, _, research, _, _, _, _ = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="selected binding has no verified replacement",
        result_review_verdict="contradicted",
        repair_kind="semantic_binding_mismatch",
    )

    async def reenter(
        solver_state,
        research_state,
        request_id,
        *,
        commit_solver_admission,
        id_factory,
        **_kwargs,
    ):
        admitted = admit_targeted_reentry(
            solver_state,
            research_state,
            request_id,
            base_revision=solver_state.revision,
            id_factory=id_factory,
        )
        committed = commit_solver_admission(admitted)
        finalized = finalize_targeted_reentry(
            committed,
            admitted.record.research_reentry_id,
            ResearchReentryStatus.PROTOCOL_FAILURE,
            base_revision=committed.revision,
        )
        return SimpleNamespace(
            solver_state=finalized.state,
            research_state=research_state,
            record=finalized.record,
        )

    async def forbidden_proposal(*_args, **_kwargs):
        raise AssertionError("semantic repair must exhaust its durable re-entry first")

    generated = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden_proposal,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=reenter,
            id_factory=iter(("semantic-repair-reentry-1",)).__next__,
        )
    )
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert checkpoint is not None
    assert checkpoint.state.research_reentries[-1].status is (
        ResearchReentryStatus.PROTOCOL_FAILURE
    )
    assert generated["sql"] == checkpoint.state.sql_candidates[-1].sql

    def forbidden_executor(*_args, **_kwargs):
        raise AssertionError("verified fallback must not execute SQL again")

    def forbidden_result_review(*_args, **_kwargs):
        raise AssertionError("verified fallback must not review SQL again")

    async def forbidden_db_audit(*_args, **_kwargs):
        raise AssertionError("verified fallback must not use the db_audit tool")

    side_effects = {"audit": 0, "persistence": 0}
    persistence_calls = []

    def audit_logger(_entry):
        side_effects["audit"] += 1
        return {"status": "logged", "log_id": "audit"}

    def save_successful_sql(**kwargs):
        side_effects["persistence"] += 1
        persistence_calls.append(kwargs)
        return {"status": "saved", "filename": "query.md", "path": "/tmp/query.md"}

    monkeypatch.setattr(core, "secure_db_executor", forbidden_executor)
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.result_review.evaluate_result_review_capability",
        forbidden_result_review,
    )
    monkeypatch.setattr(core, "audit_logger", audit_logger)
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        save_successful_sql,
    )
    monkeypatch.setattr(engine, "_execute_on_db_audit_tool_once", forbidden_db_audit)
    step = _finalizer_step(checkpoint.state)

    if crash_before_terminal:
        from workflow.text_to_sql_adaptive_solver import prepare_finalizer_execution

        prepared = prepare_finalizer_execution(
            runtime,
            {"operation": "finalize_text_to_sql_run", **step.tool_params},
        )
        assert prepared.verified_execution is not None
        marker = store.load(runtime.run_id, runtime.run_incarnation)
        assert marker is not None and marker.terminal is None
        assert marker.state.stop_reason.name == "SOLVED"
        recovered = asyncio.run(
            engine._execute_reserved_text_to_sql_finalizer(
                step, _context(runtime), "finalize"
            )
        )
        assert recovered["reason_code"] == "EXECUTION_UNKNOWN"
        assert side_effects == {"audit": 0, "persistence": 0}
        return

    result = asyncio.run(
        engine._execute_reserved_text_to_sql_finalizer(
            step, _context(runtime), "finalize"
        )
    )
    replay = asyncio.run(
        engine._execute_reserved_text_to_sql_finalizer(
            step, _context(runtime), "finalize"
        )
    )

    assert result["status"] == "succeeded"
    assert replay == result
    assert side_effects == {"audit": 1, "persistence": 1}
    # W2-2.3: the finalizer must thread the run's own schema namespace
    # version (hex-only, "sha256:" prefix stripped) through to the
    # successful-SQL memory write, not merely call it.
    assert len(persistence_calls) == 1
    assert persistence_calls[0]["namespace_version_key"] == (
        research.schema_namespace_version.split(":", 1)[-1]
    )


def test_semantic_repair_fallback_cancellation_settles_before_terminal(
    monkeypatch,
    tmp_path,
) -> None:
    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, store, _, research, _, _, _, _ = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="selected binding has no verified replacement",
        result_review_verdict="contradicted",
        repair_kind="semantic_binding_mismatch",
    )

    async def reenter(
        solver_state,
        research_state,
        request_id,
        *,
        commit_solver_admission,
        id_factory,
        **_kwargs,
    ):
        admitted = admit_targeted_reentry(
            solver_state,
            research_state,
            request_id,
            base_revision=solver_state.revision,
            id_factory=id_factory,
        )
        committed = commit_solver_admission(admitted)
        finalized = finalize_targeted_reentry(
            committed,
            admitted.record.research_reentry_id,
            ResearchReentryStatus.PROTOCOL_FAILURE,
            base_revision=committed.revision,
        )
        return SimpleNamespace(
            solver_state=finalized.state,
            research_state=research_state,
            record=finalized.record,
        )

    async def forbidden_proposal(*_args, **_kwargs):
        raise AssertionError("semantic repair must exhaust its durable re-entry first")

    asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden_proposal,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=reenter,
            id_factory=iter(("semantic-repair-reentry-1",)).__next__,
        )
    )
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert checkpoint is not None

    audit_started = threading.Event()
    release_audit = threading.Event()
    side_effects = {"audit": 0, "persistence": 0}

    def audit_logger(_entry):
        audit_started.set()
        assert release_audit.wait(timeout=10)
        side_effects["audit"] += 1
        return {"status": "logged", "log_id": "audit"}

    persistence_calls = []

    def save_successful_sql(**kwargs):
        side_effects["persistence"] += 1
        persistence_calls.append(kwargs)
        return {"status": "saved", "filename": "query.md", "path": "/tmp/query.md"}

    def forbidden_executor(*_args, **_kwargs):
        raise AssertionError("verified fallback must not execute SQL again")

    async def forbidden_db_audit(*_args, **_kwargs):
        raise AssertionError("verified fallback must not use the db_audit tool")

    monkeypatch.setattr(core, "secure_db_executor", forbidden_executor)
    monkeypatch.setattr(core, "audit_logger", audit_logger)
    monkeypatch.setattr(core, "save_successful_sql", save_successful_sql)
    monkeypatch.setattr(engine, "_execute_on_db_audit_tool_once", forbidden_db_audit)
    step = _finalizer_step(checkpoint.state)
    context = _context(runtime)

    async def cancel_during_audit():
        task = asyncio.create_task(
            engine._execute_reserved_text_to_sql_finalizer(step, context, "finalize")
        )
        assert await asyncio.to_thread(audit_started.wait, 10)
        task.cancel("cancel-during-semantic-repair-audit")
        release_audit.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_audit())

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert checkpoint is not None and checkpoint.terminal is not None
    assert side_effects == {"audit": 1, "persistence": 1}
    replay = asyncio.run(
        engine._execute_reserved_text_to_sql_finalizer(step, context, "finalize")
    )
    assert replay["status"] == "succeeded"
    assert side_effects == {"audit": 1, "persistence": 1}
    # W2-2.3: same namespace-version-key propagation as the non-cancellation
    # variant above, exercised through the cancel-during-audit path instead.
    assert len(persistence_calls) == 1
    assert persistence_calls[0]["namespace_version_key"] == (
        research.schema_namespace_version.split(":", 1)[-1]
    )


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


@pytest.mark.parametrize(
    ("verdict", "deterministic_failure_code", "open_repair"),
    (
        ("contradicted", CheckFailureCode.RESULT_SHAPE_MISMATCH, True),
        ("ambiguous", None, True),
        ("consistent", None, False),
        ("malformed", None, False),
        ("timeout", None, False),
    ),
)
def test_open_result_contradiction_checkpoint_requires_actionable_review(
    monkeypatch,
    tmp_path,
    verdict,
    deterministic_failure_code,
    open_repair,
) -> None:
    from workflow.text_to_sql_adaptive_solver import reconcile_known_finalizer

    engine = object.__new__(EnhancedWorkflowEngine)
    runtime, state, store = _finalizer_runtime(tmp_path)
    candidate = state.sql_candidates[-1]
    source_id, evidence_id = (
        ("status", "evidence-status-value")
        if verdict in {"contradicted", "ambiguous"}
        else (None, None)
    )
    actionable_receipt = ResultReviewReceipt(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        research_state_revision=candidate.revision,
        candidate_id=candidate.candidate_id,
        normalized_ast_digest=candidate.normalized_ast_digest,
        requirements_digest="sha256:" + "a" * 64,
        source_id="status",
        evidence_id="evidence-status-value",
        verdict="contradicted",
        reason="synthetic result review",
        execution=_successful_terminal(state)["execution"],
        deterministic_failure_code=CheckFailureCode.RESULT_SHAPE_MISMATCH,
    )
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="review-replay",
        request={
            "operation": "finalize_text_to_sql_run",
            "sql_query": candidate.sql,
            "row_limit": 10,
            "dry_run_only": False,
        },
    )
    checkpoint = reconcile_known_finalizer(
        store,
        reservation,
        state,
        actionable_receipt.model_dump(mode="json"),
    )
    receipt = ResultReviewReceipt(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        research_state_revision=candidate.revision,
        candidate_id=candidate.candidate_id,
        normalized_ast_digest=candidate.normalized_ast_digest,
        requirements_digest="sha256:" + "a" * 64,
        source_id=source_id,
        evidence_id=evidence_id,
        verdict=verdict,
        reason="synthetic result review",
        execution=_successful_terminal(state)["execution"],
        deterministic_failure_code=deterministic_failure_code,
    )
    monkeypatch.setattr(
        store,
        "load_replay_chain",
        lambda *_args: SimpleNamespace(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            state_revision=checkpoint.state.revision,
            state_digest=checkpoint.cursor.state_digest,
            next_action_revision=checkpoint.cursor.next_action_revision,
            pending_execution_action_revision=None,
            terminal=None,
            actions=(
                SimpleNamespace(
                    action_kind="execution",
                    action_revision=reservation.action_revision,
                    candidate_id=candidate.candidate_id,
                    normalized_ast_digest=candidate.normalized_ast_digest,
                ),
            ),
            reconciliations=(
                SimpleNamespace(
                    action_revision=reservation.action_revision,
                    outcome="KNOWN",
                    result_state_revision=checkpoint.state.revision,
                    result_state_digest=checkpoint.cursor.state_digest,
                    result=receipt.model_dump(mode="json"),
                ),
            ),
        ),
    )

    assert (
        engine._open_result_contradiction_checkpoint(runtime) == checkpoint
        if open_repair
        else engine._open_result_contradiction_checkpoint(runtime) is None
    )


@pytest.mark.parametrize("verdict", ("consistent", "malformed", "timeout"))
def test_result_contradiction_reducer_rejects_nonactionable_review(
    tmp_path,
    verdict,
) -> None:
    from workflow._text_to_sql_solver_execution_reducer import (
        state_after_result_contradiction,
    )

    _, state, store = _finalizer_runtime(tmp_path)
    candidate = state.sql_candidates[-1]
    receipt = ResultReviewReceipt(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        research_state_revision=candidate.revision,
        candidate_id=candidate.candidate_id,
        normalized_ast_digest=candidate.normalized_ast_digest,
        requirements_digest="sha256:" + "a" * 64,
        source_id=None,
        evidence_id=None,
        verdict=verdict,
        reason="synthetic result review",
        execution=_successful_terminal(state)["execution"],
        deterministic_failure_code=None,
    )
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="review-reducer",
        request={
            "operation": "finalize_text_to_sql_run",
            "sql_query": candidate.sql,
            "row_limit": 10,
            "dry_run_only": False,
        },
    )

    with pytest.raises(ValueError, match="not actionable"):
        state_after_result_contradiction(state, reservation, receipt)


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
