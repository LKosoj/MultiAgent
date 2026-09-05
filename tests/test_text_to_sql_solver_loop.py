"""W6-02 pure reducer for authenticated solver proposals."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    CheckResult,
    CheckStatus,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExecutionResult,
    ResearchState,
    ResearchReentryStatus,
    SolverState,
    SolverStopReason,
    SemanticItemStatus,
)
from custom_tools.text_to_sql.adaptive.freshness import (
    DataSnapshotStatus,
    DataSnapshotValidation,
)
from custom_tools.text_to_sql.adaptive.solver_loop import (
    SolverCandidateLimitError,
    SolverConflictError,
    SolverProtocolError,
    SolverReferenceError,
    SolverRevisionError,
    SolverValidationError,
    admit_targeted_reentry,
    apply_solver_proposal,
    apply_solver_proposal_from_parsed,
    finalize_targeted_reentry,
    stop_solver,
)
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from custom_tools.text_to_sql.adaptive.solver_protocol import (
    MissingEvidenceProposal,
    SolverProposalV1,
    SqlCandidateProposal,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageRequirements,
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive._semantic_coverage_footprint import (
    derive_coverage_footprint,
    requirements_digest,
)
from custom_tools.text_to_sql.adaptive.terminal import solver_stop_terminal_result
from text_to_sql_semantic_checks_helpers import (
    POSTGRES_DSN,
    ItemSpec,
    build_case,
    build_state,
)
from custom_tools.text_to_sql.adaptive.models import (
    PredicateOperator,
    SemanticItemKind,
)
from text_to_sql_semantic_coverage_helpers import (
    _action as research_action,
    _context as coverage_context,
    _schema_evidence,
)


def _case():
    return build_case(
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


def _two_source_case():
    return build_case(
        "SELECT o.id FROM orders o WHERE o.status = 'active'",
        (
            ItemSpec(
                source_id="order_id",
                kind=SemanticItemKind.DIMENSION,
                table="orders",
                column="id",
            ),
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


def _state(case, *, revision: int = 1, **updates: object) -> SolverState:
    values: dict[str, object] = {
        "run_id": case.state.run_id,
        "run_incarnation": case.state.run_incarnation,
        "revision": revision,
        "schema_namespace_version": case.state.schema_namespace_version,
        "query_spec": case.query_spec,
        "sql_candidates": (),
        "check_results": (),
        "execution_results": (),
        "missing_evidence_requests": (),
        "action_history": (),
        "selected_candidate_id": None,
        "stop_reason": None,
    }
    values.update(updates)
    return SolverState.model_validate(values)


def _ids(*values: str):
    pending = iter(values)
    return lambda: next(pending)


def _sql(sql: str) -> SolverProposalV1:
    return SolverProposalV1(
        proposal_version=1,
        proposal=SqlCandidateProposal(proposal_kind="sql_candidate", sql=sql),
    )


def _missing(source_id: str = "status") -> SolverProposalV1:
    return SolverProposalV1(
        proposal_version=1,
        proposal=MissingEvidenceProposal(
            proposal_kind="missing_evidence",
            source_id=source_id,
            question="Which source is authoritative?",
            required_evidence_kind=EvidenceSourceKind.SCHEMA,
            reason="No authenticated evidence is available.",
        ),
    )


def _apply(state: SolverState, proposal: SolverProposalV1, case, ids):
    return apply_solver_proposal(
        state,
        proposal,
        base_revision=state.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=case.requirements,
        id_factory=ids,
    )


def _missing_state(case) -> SolverState:
    return _apply(
        _state(case),
        _missing(),
        case,
        _ids("request-1", "action-1"),
    ).state


def test_solver_transition_rejects_canonical_requirements_missing_filter_binding() -> None:
    case = _two_source_case()
    selected = tuple(
        binding
        for binding in case.requirements.selected_bindings
        if binding.source_id != "status"
    )
    footprint = derive_coverage_footprint(
        selected,
        case.requirements.eligible_validated_joins,
    )
    values = case.requirements.model_dump(mode="python")
    values.update(
        {
            "selected_bindings": selected,
            "eligible_validated_joins": footprint.eligible_validated_joins,
            "eligible_evidence_ids": footprint.eligible_evidence_ids,
            "allowed_tables": footprint.allowed_tables,
            "allowed_columns": footprint.allowed_columns,
            "allowed_predicates": footprint.allowed_predicates,
            "allowed_join_paths": footprint.allowed_join_paths,
        }
    )
    values["requirements_digest"] = requirements_digest(
        CoverageRequirements.model_construct(**values)
    )
    forged = CoverageRequirements.model_validate(values)

    with pytest.raises(SolverProtocolError):
        apply_solver_proposal(
            _state(case),
            _missing(),
            base_revision=1,
            dsn=POSTGRES_DSN,
            table_namespace="main",
            requirements=forged,
            id_factory=_ids("request-forged", "action-forged"),
        )


@pytest.mark.parametrize("proposal_kind", ["sql", "missing"])
def test_live_and_replay_share_exact_pure_proposal_reducer(proposal_kind) -> None:
    case = _case()
    state = _state(case)
    if proposal_kind == "sql":
        proposal = _sql("SELECT o.status FROM orders o WHERE o.status = 'active'")
        generated_ids = ("candidate-replay", "action-replay")
        parsed = parse_sql_candidate(proposal.proposal.sql, POSTGRES_DSN, generated_ids[0])
    else:
        proposal = _missing()
        generated_ids = ("request-replay", "action-replay")
        parsed = None

    live = apply_solver_proposal(
        state,
        proposal,
        base_revision=state.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=case.requirements,
        id_factory=_ids(*generated_ids),
    )
    replayed = apply_solver_proposal_from_parsed(
        state,
        proposal,
        base_revision=state.revision,
        parsed_candidate=parsed,
        requirements=case.requirements,
        generated_ids=generated_ids,
    )

    assert replayed.state == live.state
    assert replayed.action == live.action


def _fresh_research(case, *, revision: int = 2) -> ResearchState:
    target = case.requirements.allowed_columns[0]
    evidence = _schema_evidence("fresh-evidence", target, revision=revision)
    return ResearchState.model_validate(
        {
            **case.state.model_dump(mode="python"),
            "revision": revision,
            "evidence": (*case.state.evidence, evidence),
            "action_history": (
                *case.state.action_history,
                *(
                    research_action((("revision", index),), index=index)
                    for index in range(1, revision)
                ),
            ),
        }
    )


@pytest.mark.parametrize(
    "reason",
    (
        SolverStopReason.NO_SAFE_CANDIDATE,
        SolverStopReason.STAGNATED,
        SolverStopReason.BUDGET_EXHAUSTED,
        SolverStopReason.DEADLINE_EXCEEDED,
        SolverStopReason.CANCELLED,
        SolverStopReason.TOOL_FAILURE,
        SolverStopReason.PROTOCOL_FAILURE,
    ),
)
def test_stop_solver_advances_one_revision_with_existing_terminal_reason(
    reason,
) -> None:
    state = _state(_case())

    stopped = stop_solver(state, reason, base_revision=state.revision)

    assert stopped.revision == state.revision + 1
    assert stopped.stop_reason is reason
    assert stopped.selected_candidate_id is None
    assert stopped.execution_results == state.execution_results


def test_stop_solver_rejects_success_missing_evidence_and_stopped_state() -> None:
    state = _state(_case())
    for reason in (SolverStopReason.SOLVED, SolverStopReason.MISSING_EVIDENCE):
        with pytest.raises(SolverProtocolError):
            stop_solver(state, reason, base_revision=state.revision)

    stopped = stop_solver(
        state,
        SolverStopReason.TOOL_FAILURE,
        base_revision=state.revision,
    )
    with pytest.raises(SolverConflictError):
        stop_solver(
            stopped,
            SolverStopReason.PROTOCOL_FAILURE,
            base_revision=stopped.revision,
        )


def test_targeted_reentry_admission_is_revision_only_without_solver_action() -> None:
    case = _case()
    stopped = _missing_state(case)

    admitted = admit_targeted_reentry(
        stopped,
        case.state,
        "request-1",
        base_revision=stopped.revision,
        id_factory=_ids("reentry-1"),
    )

    assert admitted.state.revision == stopped.revision + 1
    assert admitted.state.action_history == stopped.action_history
    assert admitted.record.status is ResearchReentryStatus.ADMITTED
    assert admitted.record.ordinal == 1
    assert admitted.record.missing_evidence_request_id == "request-1"
    assert admitted.record.source_id == "status"


def test_targeted_reentry_terminal_failure_retains_missing_evidence() -> None:
    case = _case()
    stopped = _missing_state(case)
    admitted = admit_targeted_reentry(
        stopped,
        case.state,
        "request-1",
        base_revision=stopped.revision,
        id_factory=_ids("reentry-1"),
    ).state

    finalized = finalize_targeted_reentry(
        admitted,
        "reentry-1",
        ResearchReentryStatus.TOOL_FAILURE,
        base_revision=admitted.revision,
    )

    assert finalized.state.revision == admitted.revision + 1
    assert finalized.state.stop_reason is SolverStopReason.MISSING_EVIDENCE
    assert finalized.state.action_history == stopped.action_history
    assert finalized.record.status is ResearchReentryStatus.TOOL_FAILURE
    with pytest.raises(SolverConflictError):
        finalize_targeted_reentry(
            finalized.state,
            "reentry-1",
            ResearchReentryStatus.PROTOCOL_FAILURE,
            base_revision=finalized.state.revision,
        )


def test_targeted_reentry_completed_requires_fresh_canonical_w4_authority() -> None:
    case = _case()
    stopped = _missing_state(case)
    admitted = admit_targeted_reentry(
        stopped,
        case.state,
        "request-1",
        base_revision=stopped.revision,
        id_factory=_ids("reentry-1"),
    ).state
    fresh = _fresh_research(case)
    fresh_requirements = validate_coverage_inputs(
        fresh,
        coverage_context(),
        fresh.run_id,
        fresh.run_incarnation,
    )

    finalized = finalize_targeted_reentry(
        admitted,
        "reentry-1",
        ResearchReentryStatus.COMPLETED,
        base_revision=admitted.revision,
        research_state=fresh,
        freshness_context=coverage_context(),
        requirements=fresh_requirements,
    )

    assert finalized.state.stop_reason is None
    assert finalized.record.research_result_revision == fresh.revision
    assert finalized.record.evidence_ids == ("fresh-evidence",)


def test_formula_reentry_refreshes_query_spec_before_sql_proposal() -> None:
    case = build_case(
        "SELECT AVG(o.amount) FROM orders o",
        (
            ItemSpec(
                source_id="formula",
                kind=SemanticItemKind.FORMULA,
                table="orders",
                column="amount",
            ),
        ),
    )
    requested = _apply(
        _state(case, revision=0),
        _missing("formula"),
        case,
        _ids("request-1", "action-1"),
    ).state
    stale_query_spec = case.query_spec.model_copy(
        update={
            "semantic_items": (
                case.query_spec.semantic_items[0].model_copy(
                    update={
                        "binding_ids": (),
                        "status": SemanticItemStatus.UNRESOLVED,
                    }
                ),
            )
        }
    )
    stale_research = case.state.model_copy(
        update={
            "query_spec": stale_query_spec,
            "bindings": (),
            "unresolved_items": (),
        }
    )
    requested = requested.model_copy(update={"query_spec": stale_query_spec})
    admitted = admit_targeted_reentry(
        requested,
        stale_research,
        "request-1",
        base_revision=requested.revision,
        id_factory=_ids("reentry-1"),
    ).state
    fresh = _fresh_research(case, revision=5)
    fresh_requirements = validate_coverage_inputs(
        fresh,
        coverage_context(),
        fresh.run_id,
        fresh.run_incarnation,
    )
    resumed = finalize_targeted_reentry(
        admitted,
        "reentry-1",
        ResearchReentryStatus.COMPLETED,
        base_revision=admitted.revision,
        research_state=fresh,
        freshness_context=coverage_context(),
        requirements=fresh_requirements,
    ).state

    transition = apply_solver_proposal(
        resumed,
        _sql("SELECT AVG(o.amount) FROM orders o"),
        base_revision=resumed.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=fresh_requirements,
        id_factory=_ids("candidate-1", "action-2"),
    )

    assert transition.state.sql_candidates[-1].sql == (
        "SELECT AVG(o.amount) FROM orders o"
    )


def test_targeted_reentry_rejects_new_unavailable_evidence() -> None:
    case = _case()
    stopped = _missing_state(case)
    admitted = admit_targeted_reentry(
        stopped,
        case.state,
        "request-1",
        base_revision=stopped.revision,
        id_factory=_ids("reentry-1"),
    ).state
    fresh = _fresh_research(case)
    unavailable_evidence = fresh.evidence[-1].model_copy(
        update={
            "validity_scope": EvidenceValidityScope.DATA_SNAPSHOT,
            "data_snapshot_token": "unavailable-snapshot",
        }
    )
    unavailable = ResearchState.model_validate(
        {
            **fresh.model_dump(mode="python"),
            "evidence": (*fresh.evidence[:-1], unavailable_evidence),
        }
    )
    requirements = validate_coverage_inputs(
        unavailable,
        coverage_context(),
        unavailable.run_id,
        unavailable.run_incarnation,
    )

    with pytest.raises(SolverValidationError):
        finalize_targeted_reentry(
            admitted,
            "reentry-1",
            ResearchReentryStatus.COMPLETED,
            base_revision=admitted.revision,
            research_state=unavailable,
            freshness_context=coverage_context(),
            requirements=requirements,
        )


def test_targeted_reentry_rejects_new_stale_evidence() -> None:
    case = _case()
    stopped = _missing_state(case)
    admitted = admit_targeted_reentry(
        stopped,
        case.state,
        "request-1",
        base_revision=stopped.revision,
        id_factory=_ids("reentry-1"),
    ).state
    fresh = _fresh_research(case)
    stale_evidence = fresh.evidence[-1].model_copy(
        update={
            "validity_scope": EvidenceValidityScope.DATA_SNAPSHOT,
            "data_snapshot_token": "stale-snapshot",
        }
    )
    stale = ResearchState.model_validate(
        {
            **fresh.model_dump(mode="python"),
            "evidence": (*fresh.evidence[:-1], stale_evidence),
        }
    )
    context = coverage_context(
        snapshots=(
            DataSnapshotValidation(
                token="stale-snapshot",
                status=DataSnapshotStatus.INVALID,
            ),
        )
    )
    requirements = validate_coverage_inputs(
        stale,
        context,
        stale.run_id,
        stale.run_incarnation,
    )

    with pytest.raises(SolverValidationError):
        finalize_targeted_reentry(
            admitted,
            "reentry-1",
            ResearchReentryStatus.COMPLETED,
            base_revision=admitted.revision,
            research_state=stale,
            freshness_context=context,
            requirements=requirements,
        )


def test_targeted_reentry_rejects_fresh_evidence_for_another_source() -> None:
    case = _two_source_case()
    stopped = _missing_state(case)
    admitted = admit_targeted_reentry(
        stopped,
        case.state,
        "request-1",
        base_revision=stopped.revision,
        id_factory=_ids("reentry-1"),
    ).state
    other_binding = next(
        binding
        for binding in case.requirements.selected_bindings
        if binding.source_id == "order_id"
    )
    unrelated_evidence = _schema_evidence(
        "fresh-unrelated-evidence",
        other_binding.columns[0],
        revision=2,
    )
    unrelated = ResearchState.model_validate(
        {
            **case.state.model_dump(mode="python"),
            "revision": 2,
            "evidence": (*case.state.evidence, unrelated_evidence),
            "action_history": (
                *case.state.action_history,
                research_action((("revision", 1),), index=1),
            ),
        }
    )
    requirements = validate_coverage_inputs(
        unrelated,
        coverage_context(),
        unrelated.run_id,
        unrelated.run_incarnation,
    )

    with pytest.raises(SolverValidationError):
        finalize_targeted_reentry(
            admitted,
            "reentry-1",
            ResearchReentryStatus.COMPLETED,
            base_revision=admitted.revision,
            research_state=unrelated,
            freshness_context=coverage_context(),
            requirements=requirements,
        )


def test_targeted_reentry_rejects_stale_research_and_forged_authority() -> None:
    case = _case()
    stopped = _missing_state(case)
    admitted = admit_targeted_reentry(
        stopped,
        case.state,
        "request-1",
        base_revision=stopped.revision,
        id_factory=_ids("reentry-1"),
    ).state

    with pytest.raises(SolverValidationError):
        finalize_targeted_reentry(
            admitted,
            "reentry-1",
            ResearchReentryStatus.COMPLETED,
            base_revision=admitted.revision,
            research_state=case.state,
            freshness_context=coverage_context(),
            requirements=case.requirements,
        )

    fresh = _fresh_research(case)
    forged = case.requirements.model_copy(update={"state_revision": 2})
    with pytest.raises(SolverProtocolError):
        finalize_targeted_reentry(
            admitted,
            "reentry-1",
            ResearchReentryStatus.COMPLETED,
            base_revision=admitted.revision,
            research_state=fresh,
            freshness_context=coverage_context(),
            requirements=forged,
        )


def test_targeted_reentry_has_three_attempt_cap_and_contiguous_ordinals() -> None:
    case = _case()
    current = _missing_state(case)
    for ordinal in range(1, 4):
        admitted = admit_targeted_reentry(
            current,
            case.state,
            "request-1",
            base_revision=current.revision,
            id_factory=_ids(f"reentry-{ordinal}"),
        )
        assert admitted.record.ordinal == ordinal
        current = finalize_targeted_reentry(
            admitted.state,
            admitted.record.research_reentry_id,
            ResearchReentryStatus.PROTOCOL_FAILURE,
            base_revision=admitted.state.revision,
        ).state

    with pytest.raises(SolverCandidateLimitError):
        admit_targeted_reentry(
            current,
            case.state,
            "request-1",
            base_revision=current.revision,
            id_factory=_ids("reentry-4"),
        )


def test_historical_completed_reentry_does_not_classify_new_missing_request() -> None:
    case = _case()
    stopped = _missing_state(case)
    admitted = admit_targeted_reentry(
        stopped,
        case.state,
        "request-1",
        base_revision=stopped.revision,
        id_factory=_ids("reentry-1"),
    ).state
    fresh = _fresh_research(case)
    fresh_requirements = validate_coverage_inputs(
        fresh,
        coverage_context(),
        fresh.run_id,
        fresh.run_incarnation,
    )
    resumed = finalize_targeted_reentry(
        admitted,
        "reentry-1",
        ResearchReentryStatus.COMPLETED,
        base_revision=admitted.revision,
        research_state=fresh,
        freshness_context=coverage_context(),
        requirements=fresh_requirements,
    ).state
    missing_again = apply_solver_proposal(
        resumed,
        SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id="status",
                question="Which newer status evidence is authoritative?",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="A newer targeted observation is required",
            ),
        ),
        base_revision=resumed.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=fresh_requirements,
        id_factory=_ids("request-2", "action-2"),
    ).state

    terminal = solver_stop_terminal_result(missing_again.run_id, missing_again)

    assert terminal is not None
    assert terminal.reason_code == "SCHEMA_CLARIFICATION_REQUIRED"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("run_id", "other-run"),
        ("run_incarnation", "00112233445566778899aabbccddeeff"),
        ("schema_namespace_version", "schema:fedcba9876543210"),
    ),
)
def test_targeted_reentry_rejects_cross_identity_research_state(
    field: str,
    value: str,
) -> None:
    case = _case()
    stopped = _missing_state(case)
    forged = case.state.model_copy(update={field: value})

    with pytest.raises(SolverValidationError):
        admit_targeted_reentry(
            stopped,
            forged,
            "request-1",
            base_revision=stopped.revision,
            id_factory=_ids("reentry-1"),
        )


def test_targeted_reentry_rejects_stale_solver_revision_and_id_collision() -> None:
    case = _case()
    stopped = _missing_state(case)

    with pytest.raises(SolverRevisionError):
        admit_targeted_reentry(
            stopped,
            case.state,
            "request-1",
            base_revision=stopped.revision - 1,
            id_factory=_ids("reentry-1"),
        )
    with pytest.raises(SolverConflictError):
        admit_targeted_reentry(
            stopped,
            case.state,
            "request-1",
            base_revision=stopped.revision,
            id_factory=_ids("request-1"),
        )


def test_sql_proposal_appends_host_owned_candidate_and_action() -> None:
    case = _case()
    state = _state(case)

    result = _apply(
        state,
        _sql("SELECT o.status FROM orders o WHERE o.status = 'active'"),
        case,
        _ids("candidate-1", "action-1"),
    )

    assert state.sql_candidates == state.action_history == ()
    assert result.state.revision == 2
    assert result.state.sql_candidates[0].candidate_id == "candidate-1"
    assert result.state.action_history[0].action_id == "action-1"
    assert result.state.action_history[0].base_revision == 1


def test_candidate_keeps_research_revision_while_action_uses_solver_cas() -> None:
    case = _case()
    state = _state(case, revision=4)

    result = _apply(
        state,
        _sql("SELECT o.status FROM orders o WHERE o.status = 'active'"),
        case,
        _ids("candidate-1", "action-1"),
    )

    assert case.requirements.state_revision == 1
    assert result.state.revision == 5
    assert result.state.sql_candidates[0].revision == 1
    assert result.state.action_history[0].base_revision == 4


def test_zero_research_revision_requirements_are_accepted_by_sql_branch() -> None:
    seeded = build_state(())
    research_state = ResearchState.model_validate(
        {
            **seeded.model_dump(mode="python"),
            "revision": 0,
            "action_history": (),
        }
    )
    requirements = validate_coverage_inputs(
        research_state,
        coverage_context(),
        research_state.run_id,
        research_state.run_incarnation,
    )
    case = SimpleNamespace(
        state=research_state,
        query_spec=research_state.query_spec,
        requirements=requirements,
    )

    result = _apply(
        _state(case, revision=0),
        _sql("SELECT id FROM orders"),
        case,
        _ids("candidate-1", "action-1"),
    )

    assert requirements.state_revision == research_state.query_spec.revision == 0
    assert result.state.sql_candidates[0].revision == 0
    assert result.state.revision == 1


def test_equivalent_sql_dedupes_by_existing_w5_digest_without_mutation() -> None:
    case = _case()
    first = _apply(
        _state(case),
        _sql("SELECT status FROM orders WHERE status = 'active'"),
        case,
        _ids("candidate-1", "action-1"),
    ).state
    before = first.model_dump(mode="python")

    with pytest.raises(SolverConflictError):
        _apply(
            first,
            _sql("select \"status\" from \"orders\" where \"status\" = 'active'"),
            case,
            _ids("candidate-2", "action-2"),
        )

    assert first.model_dump(mode="python") == before


def test_failed_candidate_allows_same_semantics_with_corrected_sql_text() -> None:
    case = _case()
    first = _apply(
        _state(case),
        _sql("SELECT status FROM orders WHERE status = 'active'"),
        case,
        _ids("candidate-1", "action-1"),
    ).state
    failed_schema_check = CheckResult(
        check_id="check-1",
        candidate_id="candidate-1",
        check_kind=CheckKind.SCHEMA,
        status=CheckStatus.FAILED,
        failure_code=CheckFailureCode.SCHEMA_REJECTED,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error="Column reference is ambiguous.",
        required_change="Qualify the column reference.",
    )
    first = SolverState.model_validate(
        {
            **first.model_dump(mode="python"),
            "check_results": (failed_schema_check,),
        }
    )

    second = _apply(
        first,
        _sql("SELECT orders.status FROM orders WHERE orders.status = 'active'"),
        case,
        _ids("candidate-2", "action-2"),
    ).state

    assert tuple(item.candidate_id for item in second.sql_candidates) == (
        "candidate-1",
        "candidate-2",
    )
    second_failed_schema_check = failed_schema_check.model_copy(
        update={"check_id": "check-2", "candidate_id": "candidate-2"}
    )
    second = SolverState.model_validate(
        {
            **second.model_dump(mode="python"),
            "check_results": (*second.check_results, second_failed_schema_check),
        }
    )

    with pytest.raises(SolverConflictError):
        _apply(
            second,
            _sql("SELECT status FROM orders WHERE status = 'active'"),
            case,
            _ids("candidate-3", "action-3"),
        )


def test_failed_candidate_allows_exact_retry_after_completed_research_reentry() -> None:
    case = _case()
    sql = "SELECT status FROM orders WHERE status = 'active'"
    first = _apply(
        _state(case),
        _sql(sql),
        case,
        _ids("candidate-1", "action-1"),
    ).state
    failed_check = CheckResult(
        check_id="check-1",
        candidate_id="candidate-1",
        check_kind=CheckKind.SEMANTIC,
        status=CheckStatus.FAILED,
        failure_code=CheckFailureCode.UNAUTHORIZED_LITERAL,
        affected_source_ids=(),
        affected_ast_node_ids=("ast:projection:literal",),
        observed_error="The literal lacks evidence.",
        required_change="Research the literal value.",
    )
    checked = SolverState.model_validate(
        {
            **first.model_dump(mode="python"),
            "check_results": (failed_check,),
        }
    )
    requested = _apply(
        checked,
        _missing(),
        case,
        _ids("request-1", "action-2"),
    ).state
    admitted = admit_targeted_reentry(
        requested,
        case.state,
        "request-1",
        base_revision=requested.revision,
        id_factory=_ids("reentry-1"),
    ).state
    fresh = _fresh_research(case)
    fresh_requirements = validate_coverage_inputs(
        fresh,
        coverage_context(),
        fresh.run_id,
        fresh.run_incarnation,
    )
    resumed = finalize_targeted_reentry(
        admitted,
        "reentry-1",
        ResearchReentryStatus.COMPLETED,
        base_revision=admitted.revision,
        research_state=fresh,
        freshness_context=coverage_context(),
        requirements=fresh_requirements,
    ).state

    retried = _apply(
        resumed,
        _sql(sql),
        SimpleNamespace(requirements=fresh_requirements),
        _ids("candidate-2", "action-3"),
    ).state

    assert tuple(item.candidate_id for item in retried.sql_candidates) == (
        "candidate-1",
        "candidate-2",
    )


def test_structural_sql_change_appends_and_preserves_prior_checks_and_results() -> None:
    case = _case()
    first = _apply(
        _state(case),
        _sql("SELECT o.status FROM orders o WHERE o.status = 'active'"),
        case,
        _ids("candidate-1", "action-1"),
    ).state
    check = CheckResult(
        check_id="check-1",
        candidate_id="candidate-1",
        check_kind=CheckKind.SAFETY,
        status=CheckStatus.PASSED,
        failure_code=None,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=None,
        repair=None,
        required_change=None,
    )
    execution = ExecutionResult(
        execution_id="execution-1",
        candidate_id="candidate-1",
        success=True,
        row_count=1,
        elapsed_ms=1,
        error_code=None,
    )
    first = SolverState.model_validate(
        {
            **first.model_dump(mode="python"),
            "check_results": (check,),
            "execution_results": (execution,),
        }
    )

    second = _apply(
        first,
        _sql("SELECT o.status FROM orders o WHERE o.status = 'inactive'"),
        case,
        _ids("candidate-2", "action-2"),
    ).state

    assert tuple(item.candidate_id for item in second.sql_candidates) == (
        "candidate-1",
        "candidate-2",
    )
    assert second.check_results == first.check_results
    assert second.execution_results == first.execution_results


def test_sql_action_history_allows_a_gate_revision_gap() -> None:
    case = _case()
    first = _apply(
        _state(case),
        _sql("SELECT o.status FROM orders o WHERE o.status = 'active'"),
        case,
        _ids("candidate-1", "action-1"),
    ).state
    after_gate = SolverState.model_validate(
        {**first.model_dump(mode="python"), "revision": 3}
    )

    result = _apply(
        after_gate,
        _sql("SELECT o.status FROM orders o WHERE o.status = 'inactive'"),
        case,
        _ids("candidate-2", "action-2"),
    )

    assert tuple(action.base_revision for action in result.state.action_history) == (
        1,
        3,
    )
    assert result.state.revision == 4


def test_ninth_candidate_is_rejected_before_mapping_or_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case()
    state = _state(case)
    for index in range(8):
        state = _apply(
            state,
            _sql(f"SELECT o.status FROM orders o WHERE o.status = 'value-{index}'"),
            case,
            _ids(f"candidate-{index}", f"action-{index}"),
        ).state
    before = state.model_dump(mode="python")
    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_loop.map_sql_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("mapped")),
    )

    with pytest.raises(SolverCandidateLimitError):
        _apply(state, _sql("SELECT status FROM orders"), case, _ids("unused"))

    assert state.model_dump(mode="python") == before


def test_known_source_missing_evidence_stops_with_host_request() -> None:
    case = _case()
    result = _apply(_state(case), _missing(), case, _ids("request-1", "action-1"))

    request = result.state.missing_evidence_requests[0]
    assert request.missing_evidence_request_id == "request-1"
    assert request.candidate_targets == ()
    assert request.revision == 2
    assert result.state.action_history[0].missing_evidence_request_id == "request-1"
    assert result.state.stop_reason is SolverStopReason.MISSING_EVIDENCE


def test_unknown_source_rejects_without_mutation() -> None:
    case = _case()
    state = _state(case)
    before = state.model_dump(mode="python")

    with pytest.raises(SolverReferenceError):
        _apply(state, _missing("unknown"), case, _ids("request-1", "action-1"))

    assert state.model_dump(mode="python") == before


@pytest.mark.parametrize("update", ({"stop_reason": SolverStopReason.STAGNATED},))
def test_stopped_state_rejects(update: dict[str, object]) -> None:
    case = _case()
    state = _state(case, **update)
    with pytest.raises(SolverConflictError):
        _apply(state, _sql("SELECT status FROM orders"), case, _ids("unused"))


def test_stale_base_revision_rejects() -> None:
    case = _case()
    state = _state(case)
    with pytest.raises(SolverRevisionError):
        apply_solver_proposal(
            state,
            _sql("SELECT status FROM orders"),
            base_revision=0,
            dsn=POSTGRES_DSN,
            table_namespace="main",
            requirements=case.requirements,
            id_factory=_ids("unused"),
        )


@pytest.mark.parametrize(
    "field",
    ("run_id", "run_incarnation", "schema_namespace_version", "state_revision"),
)
def test_wrong_requirements_identity_rejects(field: str) -> None:
    case = _case()
    requirements = case.requirements.model_copy(
        update={field: "wrong" if field != "state_revision" else 0}
    )
    with pytest.raises(SolverProtocolError):
        apply_solver_proposal(
            _state(case),
            _sql("SELECT status FROM orders"),
            base_revision=1,
            dsn=POSTGRES_DSN,
            table_namespace="main",
            requirements=requirements,
            id_factory=_ids("unused"),
        )


@pytest.mark.parametrize("proposal_kind", ("sql", "missing"))
def test_requirements_required_sources_must_match_exact_query_spec(
    proposal_kind: str,
) -> None:
    case = _case()
    foreign = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        (
            ItemSpec(
                source_id="foreign-source",
                kind=SemanticItemKind.FILTER,
                table="orders",
                column="status",
                operator=PredicateOperator.EQ,
                literal="active",
            ),
        ),
    )
    id_calls = 0

    def id_factory() -> str:
        nonlocal id_calls
        id_calls += 1
        return f"unused-{id_calls}"

    proposal = (
        _sql("SELECT status FROM orders")
        if proposal_kind == "sql"
        else _missing("status")
    )
    with pytest.raises(SolverProtocolError, match="requirements"):
        apply_solver_proposal(
            _state(case),
            proposal,
            base_revision=1,
            dsn=POSTGRES_DSN,
            table_namespace="main",
            requirements=foreign.requirements,
            id_factory=id_factory,
        )

    assert id_calls == 0


def test_reducer_has_no_check_or_execution_boundary_dependency() -> None:
    import custom_tools.text_to_sql.adaptive.solver_loop as module

    source = inspect.getsource(module)
    assert ".checks" not in source
    assert ".executor" not in source
    assert "from .execution import" not in source
    assert "from . import execution" not in source


@pytest.mark.parametrize("forged", ("state", "proposal", "requirements"))
def test_model_copy_forgery_is_rejected(forged: str) -> None:
    case = _case()
    state = _state(case)
    proposal = _sql("SELECT status FROM orders")
    requirements = case.requirements
    if forged == "state":
        state = state.model_copy(update={"revision": "not-an-int"})
    elif forged == "proposal":
        proposal = proposal.model_copy(update={"proposal_version": 2})
    else:
        requirements = requirements.model_copy(update={"state_revision": "not-an-int"})

    with pytest.raises((SolverProtocolError, SolverValidationError, TypeError)):
        apply_solver_proposal(
            state,
            proposal,
            base_revision=1,
            dsn=POSTGRES_DSN,
            table_namespace="main",
            requirements=requirements,
            id_factory=_ids("candidate-1", "action-1"),
        )


@pytest.mark.parametrize(
    ("root", "nested"),
    (
        ("state", False),
        ("state", True),
        ("proposal", False),
        ("proposal", True),
        ("requirements", False),
        ("requirements", True),
    ),
)
def test_model_copy_forged_declared_field_extras_are_rejected(
    root: str,
    nested: bool,
) -> None:
    case = _case()
    state = _state(case).model_copy(deep=True)
    proposal = _sql("SELECT status FROM orders").model_copy(deep=True)
    requirements = case.requirements.model_copy(deep=True)
    value = {"state": state, "proposal": proposal, "requirements": requirements}[root]
    target = value
    if nested:
        if root == "state":
            target = state.query_spec
        elif root == "proposal":
            target = proposal.proposal
        else:
            target = requirements.selected_bindings[0]
    target.__dict__["forged_extra"] = "hidden"

    with pytest.raises((SolverValidationError, SolverProtocolError)):
        apply_solver_proposal(
            state,
            proposal,
            base_revision=1,
            dsn=POSTGRES_DSN,
            table_namespace="main",
            requirements=requirements,
            id_factory=_ids("candidate-1", "action-1"),
        )


def _adversarial_inputs(root: str, nested: bool):
    case = _case()
    state = _state(case).model_copy(deep=True)
    proposal = _sql("SELECT status FROM orders").model_copy(deep=True)
    requirements = case.requirements.model_copy(deep=True)
    target = {"state": state, "proposal": proposal, "requirements": requirements}[root]
    if nested:
        if root == "state":
            target = state.query_spec
        elif root == "proposal":
            target = proposal.proposal
        else:
            target = requirements.selected_bindings[0]
    return state, proposal, requirements, target


@pytest.mark.parametrize(
    ("root", "nested"),
    (
        ("state", False),
        ("state", True),
        ("proposal", False),
        ("proposal", True),
        ("requirements", False),
        ("requirements", True),
    ),
)
def test_forged_pydantic_private_is_rejected_recursively(
    root: str,
    nested: bool,
) -> None:
    state, proposal, requirements, target = _adversarial_inputs(root, nested)
    object.__setattr__(target, "__pydantic_private__", {"forged": "hidden"})

    with pytest.raises((SolverValidationError, SolverProtocolError)):
        apply_solver_proposal(
            state,
            proposal,
            base_revision=1,
            dsn=POSTGRES_DSN,
            table_namespace="main",
            requirements=requirements,
            id_factory=_ids("candidate-1", "action-1"),
        )


@pytest.mark.parametrize(
    ("root", "nested"),
    (
        ("state", False),
        ("state", True),
        ("proposal", False),
        ("proposal", True),
        ("requirements", False),
        ("requirements", True),
    ),
)
def test_forged_pydantic_extra_equality_is_never_called(
    root: str,
    nested: bool,
) -> None:
    callbacks = 0

    class EqualToEmpty(dict):
        def __eq__(self, _other: object) -> bool:
            nonlocal callbacks
            callbacks += 1
            return True

        def __ne__(self, _other: object) -> bool:
            nonlocal callbacks
            callbacks += 1
            return False

    state, proposal, requirements, target = _adversarial_inputs(root, nested)
    object.__setattr__(target, "__pydantic_extra__", EqualToEmpty(forged="hidden"))

    with pytest.raises((SolverValidationError, SolverProtocolError)):
        apply_solver_proposal(
            state,
            proposal,
            base_revision=1,
            dsn=POSTGRES_DSN,
            table_namespace="main",
            requirements=requirements,
            id_factory=_ids("candidate-1", "action-1"),
        )
    assert callbacks == 0


@pytest.mark.parametrize(
    ("root", "nested"),
    (
        ("state", False),
        ("state", True),
        ("proposal", False),
        ("proposal", True),
        ("requirements", False),
        ("requirements", True),
    ),
)
def test_forged_dict_subclass_iterator_is_never_called(
    root: str,
    nested: bool,
) -> None:
    callbacks = 0

    class HidingDict(dict):
        def __iter__(self):
            nonlocal callbacks
            callbacks += 1
            return iter(key for key in dict.keys(self) if key != "forged_hidden_field")

    state, proposal, requirements, target = _adversarial_inputs(root, nested)
    forged_dict = HidingDict(target.__dict__)
    forged_dict["forged_hidden_field"] = "hidden"
    object.__setattr__(target, "__dict__", forged_dict)

    with pytest.raises((SolverValidationError, SolverProtocolError)):
        apply_solver_proposal(
            state,
            proposal,
            base_revision=1,
            dsn=POSTGRES_DSN,
            table_namespace="main",
            requirements=requirements,
            id_factory=_ids("candidate-1", "action-1"),
        )
    assert callbacks == 0


@pytest.mark.parametrize("root", ("state", "proposal", "requirements"))
def test_nested_string_subclasses_are_rejected(root: str) -> None:
    class TextSubclass(str):
        pass

    case = _case()
    state = _state(case).model_copy(deep=True)
    proposal = _sql("SELECT status FROM orders").model_copy(deep=True)
    requirements = case.requirements.model_copy(deep=True)
    if root == "state":
        state.query_spec.__dict__["original_text"] = TextSubclass(
            state.query_spec.original_text
        )
    elif root == "proposal":
        proposal.proposal.__dict__["sql"] = TextSubclass(proposal.proposal.sql)
    else:
        binding = requirements.selected_bindings[0]
        binding.__dict__["validator_rule"] = TextSubclass(binding.validator_rule)

    with pytest.raises((SolverValidationError, SolverProtocolError)):
        apply_solver_proposal(
            state,
            proposal,
            base_revision=1,
            dsn=POSTGRES_DSN,
            table_namespace="main",
            requirements=requirements,
            id_factory=_ids("candidate-1", "action-1"),
        )


@pytest.mark.parametrize(
    "ids", (("candidate-1", "action-1"), ("bad id", "action-1"))
)
def test_generated_ids_are_valid_and_do_not_collide(ids: tuple[str, ...]) -> None:
    case = _case()
    state = _state(case)
    if ids[0] == "candidate-1":
        state = _apply(state, _sql("SELECT status FROM orders"), case, _ids(*ids)).state
        with pytest.raises(SolverConflictError):
            _apply(
                state,
                _sql("SELECT status FROM orders WHERE status = 'x'"),
                case,
                _ids(*ids),
            )
    else:
        with pytest.raises(SolverProtocolError):
            _apply(state, _sql("SELECT status FROM orders"), case, _ids(*ids))


def test_generated_id_string_subclass_rejects_before_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IdSubclass(str):
        pass

    case = _case()
    state = _state(case)
    raw_ids = iter(("candidate-1", IdSubclass("action-1")))
    id_calls = 0
    map_calls = 0

    def id_factory() -> str:
        nonlocal id_calls
        id_calls += 1
        return next(raw_ids)

    def mapping(*_args, **_kwargs):
        nonlocal map_calls
        map_calls += 1
        raise AssertionError("mapping must not run")

    monkeypatch.setattr(
        "custom_tools.text_to_sql.adaptive.solver_loop.map_sql_candidate", mapping
    )

    with pytest.raises(SolverProtocolError, match="invalid Id"):
        _apply(state, _sql("SELECT status FROM orders"), case, id_factory)

    assert id_calls == 2
    assert map_calls == 0
    assert state.sql_candidates == state.action_history == ()
