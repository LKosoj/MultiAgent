"""W6-03 dormant candidate gates and pure durable-result transitions."""

from __future__ import annotations

import asyncio

import pytest
from text_to_sql_semantic_checks_helpers import POSTGRES_DSN, ItemSpec, build_case
from text_to_sql_semantic_coverage_helpers import SCHEMA, _context

from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    CheckRepair,
    CheckResult,
    CheckStatus,
    EvidenceSourceKind,
    PredicateOperator,
    RepairKind,
    ResearchState,
    SemanticItemKind,
    SolverState,
    SolverStopReason,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal
from custom_tools.text_to_sql.adaptive.solver_protocol import (
    MissingEvidenceProposal,
    SolverProposalV1,
    SqlCandidateProposal,
)
from custom_tools.text_to_sql.adaptive.solver_results import (
    SolverGateTransitionResult,
    append_solver_check_result,
)
from custom_tools.text_to_sql.adaptive.solver_runner import (
    SolverCommitError,
    SolverRunnerValidationError,
    run_solver_candidate_gates,
    run_solver_candidate_pre_execution_gates,
)
from custom_tools.text_to_sql.core._db_exec import (
    QueryExecutionResult,
    QueryPurpose,
)
from custom_tools.text_to_sql.schema_loader import LoadedSchema
from custom_tools.text_to_sql.schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from custom_tools.text_to_sql.validators import load_startup_safety_policy
from workflow.deadline import DeadlineBudget
from workflow.deadline import WorkflowDeadlineExceeded


def _replace_schema(value: object, old: str, new: str) -> object:
    if type(value) is dict:
        return {key: _replace_schema(item, old, new) for key, item in value.items()}
    if type(value) is list:
        return [_replace_schema(item, old, new) for item in value]
    if type(value) is tuple:
        return tuple(_replace_schema(item, old, new) for item in value)
    if type(value) is str:
        return value.replace(old, new)
    return value


def _runtime(*, scope_serialization_version: int = 1):
    seed = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        (
            ItemSpec(
                source_id="status",
                kind=SemanticItemKind.FILTER,
                table="orders",
                column="status",
                operator=PredicateOperator.EQ,
                literal="active",
            ),
        ),
    )
    schema = {"orders": {"columns": {"status": {"type": "TEXT"}}}}
    scope = SchemaScope(1, "tenant", "scope", "connection", True)
    object.__setattr__(scope, "serialization_version", scope_serialization_version)
    namespace = SchemaNamespace(
        scope,
        canonical_schema_fingerprint(schema),
    )
    loaded_schema = LoadedSchema(schema, namespace, "test")
    schema_version = f"sha256:{namespace.version_key}"
    research_state = ResearchState.model_validate(
        _replace_schema(seed.state.model_dump(mode="python"), SCHEMA, schema_version)
    )
    context = _context(schema=schema_version)
    assert type(context) is FreshnessContext
    requirements = validate_coverage_inputs(
        research_state,
        context,
        research_state.run_id,
        research_state.run_incarnation,
    )
    state = SolverState(
        run_id=research_state.run_id,
        run_incarnation=research_state.run_incarnation,
        revision=research_state.revision,
        schema_namespace_version=schema_version,
        query_spec=research_state.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    ids = iter(("candidate-1", "plan-1", "action-1"))
    state = apply_solver_proposal(
        state,
        SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT o.status FROM orders o WHERE o.status = 'active'",
            ),
        ),
        base_revision=state.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=requirements,
        id_factory=lambda: next(ids),
    ).state
    return state, research_state, requirements, loaded_schema


def _check(candidate_id: str, kind: CheckKind, status=CheckStatus.PASSED):
    if status is CheckStatus.PASSED:
        return CheckResult(
            check_id=f"{kind.value}:{candidate_id}:passed",
            candidate_id=candidate_id,
            check_kind=kind,
            status=status,
            failure_code=None,
            affected_source_ids=(),
            affected_ast_node_ids=(),
            observed_error=None,
        )
    return CheckResult(
        check_id=f"{kind.value}:{candidate_id}:check_malformed",
        candidate_id=candidate_id,
        check_kind=kind,
        status=status,
        failure_code=CheckFailureCode.CHECK_MALFORMED,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error="invalid checker result",
        repair=CheckRepair(kind=RepairKind.REVISE_SQL),
    )


def _native_execution(sql: str, *, success: bool = True):
    return QueryExecutionResult(
        QueryPurpose.FINAL,
        {
            "success": success,
            "data": [["active"]] if success else [],
            "columns": ["status"] if success else [],
            "rows_affected": 1 if success else 0,
            "execution_time_ms": 3,
            "error_message": None if success else "database rejected query",
            "dry_run_only": False,
            "skipped_execution": False,
            "sql_query": sql,
            "applied_row_limit": 10,
        },
    )


def _passed_through(state: SolverState, *kinds: CheckKind) -> SolverState:
    for kind in kinds:
        state = append_solver_check_result(
            state,
            _check(state.sql_candidates[-1].candidate_id, kind),
            base_revision=state.revision,
        ).state
    return state


def _runner_call(
    state,
    research_state,
    requirements,
    loaded_schema,
    **updates,
):
    values = {
        "candidate_id": state.sql_candidates[-1].candidate_id,
        "research_state": research_state,
        "requirements": requirements,
        "loaded_schema": loaded_schema,
        "dsn": POSTGRES_DSN,
        "safety_policy": load_startup_safety_policy(),
        "row_limit": 10,
        "dry_run_only": False,
        "deadline": DeadlineBudget.from_duration(60),
        "is_cancelled": lambda: False,
        "id_factory": lambda: "execution-1",
        "commit_transition": lambda transition: transition.state,
        "executor": None,
    }
    values.update(updates)
    return run_solver_candidate_gates(state, **values)


def test_result_reducer_enforces_prefix_revision_and_blocking_stop():
    state, *_ = _runtime()
    candidate_id = state.sql_candidates[-1].candidate_id

    first = append_solver_check_result(
        state,
        _check(candidate_id, CheckKind.SAFETY),
        base_revision=state.revision,
    )
    assert first.base_revision == state.revision
    assert first.state.revision == state.revision + 1
    assert first.state.action_history == state.action_history

    with pytest.raises(ValueError):
        append_solver_check_result(
            first.state,
            _check(candidate_id, CheckKind.SEMANTIC),
            base_revision=first.state.revision,
        )

    blocked = append_solver_check_result(
        first.state,
        _check(candidate_id, CheckKind.SCHEMA, CheckStatus.INCONCLUSIVE),
        base_revision=first.state.revision,
    )
    with pytest.raises(ValueError):
        append_solver_check_result(
            blocked.state,
            _check(candidate_id, CheckKind.SEMANTIC),
            base_revision=blocked.state.revision,
        )


def test_runner_orders_gates_commits_each_result_and_executes_once(monkeypatch):
    state, research_state, requirements, loaded_schema = _runtime()
    calls: list[str] = []
    commits: list[SolverGateTransitionResult] = []

    def safety(*_args, **_kwargs):
        calls.append("safety")
        return {
            "is_safe": True,
            "issues": [],
            "advisory_issues": [],
            "safety_status": "safe",
            "llm_audit": "skipped_static_only",
            "layer": "static",
            "profile_name": "default",
            "policy_version": "v1",
        }

    class SchemaValidator:
        def validate_sql_against_schema(self, *_args, **_kwargs):
            calls.append("schema")
            return {"is_valid": True, "issues": []}

    def semantic(*args, **kwargs):
        calls.append("semantic")
        from custom_tools.text_to_sql.adaptive.semantic_checks import (
            evaluate_semantic_authority_checks,
        )

        return evaluate_semantic_authority_checks(*args, **kwargs)

    def explain(*_args, **_kwargs):
        calls.append("explain")
        return {
            "plan": "SCAN orders",
            "estimated_cost": 1,
            "rows_to_scan": 1,
            "issues": [],
            "profile_name": "default",
            "policy_version": "v1",
        }

    class Executor:
        def execute(self, request):
            calls.append("execution")
            assert request.deadline is deadline
            assert request.purpose is QueryPurpose.FINAL
            return _native_execution(request.sql_query)

    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.core.sql_safety_check",
        safety,
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.SQLSchemaValidator",
        SchemaValidator,
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.evaluate_semantic_authority_checks",
        semantic,
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.core.sql_explain",
        explain,
    )
    deadline = DeadlineBudget.from_duration(60)

    def commit(transition):
        commits.append(transition)
        return transition.state

    result = run_solver_candidate_gates(
        state,
        candidate_id="candidate-1",
        research_state=research_state,
        requirements=requirements,
        loaded_schema=loaded_schema,
        dsn=POSTGRES_DSN,
        safety_policy=load_startup_safety_policy(),
        row_limit=10,
        dry_run_only=False,
        deadline=deadline,
        is_cancelled=lambda: False,
        id_factory=lambda: "execution-1",
        commit_transition=commit,
        executor=Executor(),
    )

    assert calls == ["safety", "schema", "semantic", "explain", "execution"]
    assert len(commits) == 5
    assert tuple(item.base_revision for item in commits) == tuple(
        range(state.revision, state.revision + 5)
    )
    assert result.revision == state.revision + 5
    assert result.stop_reason is SolverStopReason.SOLVED
    assert result.selected_candidate_id == "candidate-1"
    assert len(result.execution_results) == 1


def test_pre_execution_runner_stops_after_explain_without_execution(monkeypatch):
    state, research_state, requirements, loaded_schema = _runtime()
    calls: list[str] = []

    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.core.sql_safety_check",
        lambda *_a, **_kw: {
            "is_safe": True,
            "issues": [],
            "advisory_issues": [],
            "safety_status": "safe",
            "llm_audit": "skipped_static_only",
        },
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.SQLSchemaValidator.validate_sql_against_schema",
        lambda *_a, **_kw: {"is_valid": True, "issues": []},
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.core.sql_explain",
        lambda *_a, **_kw: {
            "plan": "SCAN orders",
            "estimated_cost": 1,
            "rows_to_scan": 1,
            "issues": [],
            "profile_name": "default",
            "policy_version": "v1",
        },
    )

    def commit(transition):
        calls.append(transition.check_result.check_kind.value)
        return transition.state

    result = run_solver_candidate_pre_execution_gates(
        state,
        candidate_id="candidate-1",
        research_state=research_state,
        requirements=requirements,
        loaded_schema=loaded_schema,
        dsn=POSTGRES_DSN,
        safety_policy=load_startup_safety_policy(),
        row_limit=10,
        dry_run_only=False,
        deadline=DeadlineBudget.from_duration(60),
        is_cancelled=lambda: False,
        commit_transition=commit,
    )

    assert calls == ["safety", "schema", "semantic", "explain"]
    assert result.revision == state.revision + 4
    assert result.stop_reason is None
    assert result.execution_results == ()


def test_runner_resume_blocker_and_commit_mismatch_do_not_recall_prior_gate(
    monkeypatch,
):
    state, research_state, requirements, loaded_schema = _runtime()
    candidate_id = state.sql_candidates[-1].candidate_id
    state = append_solver_check_result(
        state,
        _check(candidate_id, CheckKind.SAFETY),
        base_revision=state.revision,
    ).state
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.core.sql_safety_check",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("recalled safety")),
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.SQLSchemaValidator.validate_sql_against_schema",
        lambda *_a, **_kw: {"is_valid": True, "issues": []},
    )

    with pytest.raises(SolverCommitError):
        run_solver_candidate_gates(
            state,
            candidate_id=candidate_id,
            research_state=research_state,
            requirements=requirements,
            loaded_schema=loaded_schema,
            dsn=POSTGRES_DSN,
            safety_policy=load_startup_safety_policy(),
            row_limit=10,
            dry_run_only=False,
            deadline=DeadlineBudget.from_duration(60),
            is_cancelled=lambda: False,
            id_factory=lambda: "execution-1",
            commit_transition=lambda _transition: state,
            executor=None,
        )


def test_runner_dry_run_stops_after_inconclusive_explain_without_final(monkeypatch):
    state, research_state, requirements, loaded_schema = _runtime()

    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.core.sql_safety_check",
        lambda *_a, **_kw: {
            "is_safe": True,
            "issues": [],
            "advisory_issues": [],
            "safety_status": "safe",
            "llm_audit": "skipped_static_only",
        },
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.SQLSchemaValidator.validate_sql_against_schema",
        lambda *_a, **_kw: {"is_valid": True, "issues": []},
    )
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.core.sql_explain",
        lambda sql, **_kw: {
            "plan": None,
            "estimated_cost": None,
            "rows_to_scan": None,
            "issues": [],
            "profile_name": "default",
            "policy_version": "v1",
            "dry_run_only": True,
            "skipped_execution": True,
            "sql_query": sql,
        },
    )

    result = run_solver_candidate_gates(
        state,
        candidate_id="candidate-1",
        research_state=research_state,
        requirements=requirements,
        loaded_schema=loaded_schema,
        dsn=POSTGRES_DSN,
        safety_policy=load_startup_safety_policy(),
        row_limit=10,
        dry_run_only=True,
        deadline=DeadlineBudget.from_duration(60),
        is_cancelled=lambda: False,
        id_factory=lambda: (_ for _ in ()).throw(AssertionError("no final ID")),
        commit_transition=lambda transition: transition.state,
        executor=None,
    )

    assert result.check_results[-1].check_kind is CheckKind.EXPLAIN
    assert result.check_results[-1].status is CheckStatus.INCONCLUSIVE
    assert result.execution_results == ()


def test_runner_propagates_cancellation_before_any_external_call():
    state, research_state, requirements, loaded_schema = _runtime()

    with pytest.raises(asyncio.CancelledError):
        run_solver_candidate_gates(
            state,
            candidate_id="candidate-1",
            research_state=research_state,
            requirements=requirements,
            loaded_schema=loaded_schema,
            dsn=POSTGRES_DSN,
            safety_policy=load_startup_safety_policy(),
            row_limit=10,
            dry_run_only=False,
            deadline=DeadlineBudget.from_duration(60),
            is_cancelled=lambda: True,
            id_factory=lambda: "execution-1",
            commit_transition=lambda transition: transition.state,
            executor=None,
        )


@pytest.mark.parametrize("checker_raises", (False, True))
def test_runner_rechecks_cancellation_after_checker_before_commit(
    monkeypatch,
    checker_raises,
):
    state, research_state, requirements, loaded_schema = _runtime()
    commits = []
    cancellation = iter((False, True))

    def safety(*_args, **_kwargs):
        if checker_raises:
            raise RuntimeError("checker failed")
        return {
            "is_safe": False,
            "issues": [{"issue_type": "FORBIDDEN_COMMAND", "description": "blocked"}],
            "advisory_issues": [],
            "safety_status": "unsafe",
            "llm_audit": "skipped_static_unsafe",
        }

    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.core.sql_safety_check",
        safety,
    )

    with pytest.raises(asyncio.CancelledError):
        _runner_call(
            state,
            research_state,
            requirements,
            loaded_schema,
            is_cancelled=lambda: next(cancellation),
            commit_transition=lambda transition: commits.append(transition),
        )

    assert commits == []


@pytest.mark.parametrize("executor_raises", (False, True))
def test_runner_rechecks_cancellation_after_executor_before_commit(
    executor_raises,
):
    state, research_state, requirements, loaded_schema = _runtime()
    state = _passed_through(
        state,
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )
    commits = []
    cancellation = iter((False, True))

    class Executor:
        def execute(self, request):
            if executor_raises:
                raise RuntimeError("executor failed")
            return _native_execution(request.sql_query)

    with pytest.raises(asyncio.CancelledError):
        _runner_call(
            state,
            research_state,
            requirements,
            loaded_schema,
            is_cancelled=lambda: next(cancellation),
            commit_transition=lambda transition: commits.append(transition),
            executor=Executor(),
        )

    assert commits == []


@pytest.mark.parametrize("adapter_raises", (False, True))
def test_runner_checks_cancellation_immediately_before_final_transition(
    monkeypatch,
    adapter_raises,
):
    state, research_state, requirements, loaded_schema = _runtime()
    state = _passed_through(
        state,
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )
    commits = []
    cancellation = iter((False, False, True))

    class Executor:
        def execute(self, request):
            return _native_execution(request.sql_query)

    if adapter_raises:
        monkeypatch.setattr(
            "custom_tools.text_to_sql.adaptive.solver_runner.adapt_execution_check_result",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("adapter failed")
            ),
        )

    def commit(transition):
        commits.append(transition)
        return transition.state

    with pytest.raises(asyncio.CancelledError):
        _runner_call(
            state,
            research_state,
            requirements,
            loaded_schema,
            is_cancelled=lambda: next(cancellation),
            commit_transition=commit,
            executor=Executor(),
        )

    assert commits == []


def _expires_after_first_guard():
    monotonic = iter((0.0, 2.0))
    return DeadlineBudget(
        deadline_monotonic=1.0,
        deadline_at_ms=1,
        monotonic=lambda: next(monotonic),
        wall_time=lambda: 0.0,
    )


def test_runner_rechecks_deadline_after_checker_success_before_commit(monkeypatch):
    state, research_state, requirements, loaded_schema = _runtime()
    commits = []
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_runner.core.sql_safety_check",
        lambda *_a, **_kw: {
            "is_safe": False,
            "issues": [{"issue_type": "FORBIDDEN_COMMAND", "description": "blocked"}],
            "advisory_issues": [],
            "safety_status": "unsafe",
            "llm_audit": "skipped_static_unsafe",
        },
    )

    with pytest.raises(WorkflowDeadlineExceeded):
        _runner_call(
            state,
            research_state,
            requirements,
            loaded_schema,
            deadline=_expires_after_first_guard(),
            commit_transition=lambda transition: commits.append(transition),
        )

    assert commits == []


def test_runner_rechecks_deadline_after_executor_exception_before_commit():
    state, research_state, requirements, loaded_schema = _runtime()
    state = _passed_through(
        state,
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )
    commits = []

    class Executor:
        def execute(self, _request):
            raise RuntimeError("executor failed")

    with pytest.raises(WorkflowDeadlineExceeded):
        _runner_call(
            state,
            research_state,
            requirements,
            loaded_schema,
            deadline=_expires_after_first_guard(),
            commit_transition=lambda transition: commits.append(transition),
            executor=Executor(),
        )

    assert commits == []


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    (
        ("deadline_monotonic", float("inf")),
        ("deadline_at_ms", True),
    ),
)
def test_runner_rejects_noncanonical_deadline_before_parsing(
    monkeypatch,
    field_name,
    forged_value,
):
    state, research_state, requirements, loaded_schema = _runtime()
    deadline = DeadlineBudget.from_duration(60)
    object.__setattr__(deadline, field_name, forged_value)
    parsings = []
    from custom_tools.text_to_sql.adaptive import solver_runner

    real_parser = solver_runner.parse_sql_candidate

    def tracked_parser(*args, **kwargs):
        parsings.append(True)
        return real_parser(*args, **kwargs)

    monkeypatch.setattr(solver_runner, "parse_sql_candidate", tracked_parser)
    monkeypatch.setattr(
        solver_runner.core,
        "sql_safety_check",
        lambda *_args, **_kwargs: {
            "is_safe": False,
            "issues": [{"issue_type": "FORBIDDEN_COMMAND", "description": "blocked"}],
            "advisory_issues": [],
            "safety_status": "unsafe",
            "llm_audit": "skipped_static_unsafe",
        },
    )

    with pytest.raises(SolverRunnerValidationError):
        _runner_call(
            state,
            research_state,
            requirements,
            loaded_schema,
            deadline=deadline,
        )

    assert parsings == []


def test_runner_rebuilds_schema_scope_before_parsing(monkeypatch):
    state, research_state, requirements, loaded_schema = _runtime(
        scope_serialization_version=2
    )
    parsings = []
    from custom_tools.text_to_sql.adaptive import solver_runner

    real_parser = solver_runner.parse_sql_candidate

    def tracked_parser(*args, **kwargs):
        parsings.append(True)
        return real_parser(*args, **kwargs)

    monkeypatch.setattr(solver_runner, "parse_sql_candidate", tracked_parser)
    monkeypatch.setattr(
        solver_runner.core,
        "sql_safety_check",
        lambda *_args, **_kwargs: {
            "is_safe": False,
            "issues": [{"issue_type": "FORBIDDEN_COMMAND", "description": "blocked"}],
            "advisory_issues": [],
            "safety_status": "unsafe",
            "llm_audit": "skipped_static_unsafe",
        },
    )

    with pytest.raises(SolverRunnerValidationError):
        _runner_call(state, research_state, requirements, loaded_schema)

    assert parsings == []


@pytest.mark.parametrize("invalid_history", ("wrong_prefix", "unbound_execution"))
def test_runner_validates_every_candidate_history_before_parsing_or_calls(
    monkeypatch,
    invalid_history,
):
    state, research_state, requirements, loaded_schema = _runtime()
    ids = iter(("candidate-2", "plan-2", "action-2"))
    state = apply_solver_proposal(
        state,
        SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT o.status FROM orders o WHERE o.status = 'inactive'",
            ),
        ),
        base_revision=state.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=requirements,
        id_factory=lambda: next(ids),
    ).state
    old_candidate_id = state.sql_candidates[0].candidate_id
    if invalid_history == "wrong_prefix":
        checks = (_check(old_candidate_id, CheckKind.SEMANTIC),)
    else:
        checks = tuple(_check(old_candidate_id, kind) for kind in CheckKind)
    state = SolverState.model_validate(
        {**state.model_dump(mode="python"), "check_results": checks}
    )
    side_effects = []
    from custom_tools.text_to_sql.adaptive import solver_runner

    real_parser = solver_runner.parse_sql_candidate

    def tracked_parser(*args, **kwargs):
        side_effects.append("parsing")
        return real_parser(*args, **kwargs)

    monkeypatch.setattr(solver_runner, "parse_sql_candidate", tracked_parser)
    monkeypatch.setattr(
        solver_runner.core,
        "sql_safety_check",
        lambda *_a, **_kw: side_effects.append("safety"),
    )

    with pytest.raises(ValueError):
        _runner_call(state, research_state, requirements, loaded_schema)

    assert side_effects == []


def test_execution_id_cannot_collide_with_missing_evidence_request():
    state, research_state, requirements, loaded_schema = _runtime()
    ids = iter(("occupied-execution-id", "missing-action"))
    state = apply_solver_proposal(
        state,
        SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id="status",
                question="Which source is authoritative?",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="Need an authoritative source.",
            ),
        ),
        base_revision=state.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=requirements,
        id_factory=lambda: next(ids),
    ).state
    state = SolverState.model_validate(
        {**state.model_dump(mode="python"), "stop_reason": None}
    )
    state = _passed_through(
        state,
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )
    calls = []

    class Executor:
        def execute(self, request):
            calls.append(request)
            return _native_execution(request.sql_query)

    with pytest.raises(ValueError):
        _runner_call(
            state,
            research_state,
            requirements,
            loaded_schema,
            id_factory=lambda: "occupied-execution-id",
            executor=Executor(),
        )

    assert calls == []


class _SneakyStr(str):
    pass


@pytest.mark.parametrize(
    "target",
    (
        "loaded_hidden",
        "namespace_hidden",
        "schema_scalar",
        "policy_hidden",
        "policy_scalar",
    ),
)
def test_runner_rejects_forged_dataclass_boundaries_before_parsing(
    monkeypatch,
    target,
):
    state, research_state, requirements, loaded_schema = _runtime()
    policy = type(load_startup_safety_policy()).from_mapping(
        load_startup_safety_policy().to_mapping()
    )
    if target == "loaded_hidden":
        object.__setattr__(loaded_schema, "hidden", "forged")
    elif target == "namespace_hidden":
        object.__setattr__(loaded_schema.namespace, "hidden", "forged")
    elif target == "schema_scalar":
        loaded_schema.schema["orders"]["columns"]["status"]["type"] = _SneakyStr("TEXT")
    elif target == "policy_hidden":
        object.__setattr__(policy, "hidden", "forged")
    else:
        object.__setattr__(policy, "profile_name", _SneakyStr(policy.profile_name))
    parsings = []
    from custom_tools.text_to_sql.adaptive import solver_runner

    real_parser = solver_runner.parse_sql_candidate

    def tracked_parser(*args, **kwargs):
        parsings.append(True)
        return real_parser(*args, **kwargs)

    monkeypatch.setattr(solver_runner, "parse_sql_candidate", tracked_parser)

    with pytest.raises(SolverRunnerValidationError):
        _runner_call(
            state,
            research_state,
            requirements,
            loaded_schema,
            safety_policy=policy,
        )

    assert parsings == []
