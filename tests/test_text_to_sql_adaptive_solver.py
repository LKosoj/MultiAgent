"""Production ON solver finalization and crash-recovery contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    ColumnRef,
    EvidenceCost,
    EvidenceSourceKind,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    ResearchState,
    ResearchAction,
    ResearchActionKind,
    ResearchReentryStatus,
    ResearchStopReason,
    SemanticItemKind,
    ResultExpectation,
    ResultExpectationKind,
    SolverActionKind,
    SolverState,
    SolverStopReason,
    TableRef,
)
from custom_tools.text_to_sql.adaptive._policy_model_budget import _model_started
from custom_tools.text_to_sql.adaptive.production_research import (
    stable_schema_research_model_identity,
)
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, build_probe_result
from custom_tools.text_to_sql.adaptive.research_loop import _model_call_id
from custom_tools.text_to_sql.adaptive.research_reentry import (
    _research_context,
    _trusted_targets,
)
from custom_tools.text_to_sql.adaptive.replay_inputs import (
    ResearchTerminalReplayInput,
    SolverMissingEvidenceReplayInput,
    SolverReentryAdmissionReplayInput,
    SolverReentryCompletedReplayInput,
    SolverSqlProposalReplayInput,
)
from custom_tools.text_to_sql.adaptive.freshness import (
    FreshnessContext,
)
from custom_tools.text_to_sql.adaptive.schema_research_agent import (
    load_schema_research_agent_profile,
)
from custom_tools.text_to_sql.adaptive.schema_probes import SchemaEvidenceDocument
from custom_tools.text_to_sql.adaptive.policy import (
    canonical_action_digest,
    reconcile_probe_cost,
    reserve_model_call_budget,
    reserve_probe_budget,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
from custom_tools.text_to_sql.adaptive.serialization import canonical_json_bytes
from custom_tools.text_to_sql.adaptive.serialization import serialize_contract
from custom_tools.text_to_sql.adaptive.semantic_coverage import validate_coverage_inputs
from custom_tools.text_to_sql.adaptive.replay_inputs import serialize_replay_input
from custom_tools.text_to_sql.adaptive.result_validation import (
    ResultContradictionFinding,
    ResultContradictionReceipt,
)
from custom_tools.text_to_sql.adaptive.result_review import ResultReviewReceipt
from custom_tools.text_to_sql.adaptive.solver_loop import (
    admit_targeted_reentry,
    apply_solver_proposal,
    finalize_targeted_reentry,
    SolverProtocolError,
)
from custom_tools.text_to_sql.adaptive.solver_protocol import (
    MissingEvidenceProposal,
    SolverProposalV1,
    SqlCandidateProposal,
    parse_solver_proposal,
)
from custom_tools.text_to_sql.adaptive.solver_results import (
    append_solver_check_result,
)
from custom_tools.text_to_sql.adaptive.sql_ast import SqlAstError, SqlAstErrorCode
from test_text_to_sql_solver_runner import (
    _check,
    _passed_through,
    _runtime,
)
from test_text_to_sql_adaptive_solver_reentry_runtime import (
    _one_remaining_reentry_case,
    _seed_honest_v2_research_history,
)
from text_to_sql_semantic_checks_helpers import POSTGRES_DSN
from text_to_sql_semantic_coverage_helpers import _context
from workflow.text_to_sql_typed_runtime import (
    TextToSqlTypedAdmission,
    TextToSqlTypedRuntime,
    _ADMISSION_CAPABILITY,
)
from workflow._text_to_sql_document_authority import (
    DocumentAuthorityError,
    empty_schema_document_registry,
    live_solver_document_freshness_context,
    solver_document_freshness_reference,
)
from workflow._text_to_sql_solver_terminal_evidence import (
    build_verified_solver_terminal_evidence,
    decode_verified_solver_terminal_evidence,
    encode_verified_solver_terminal_evidence,
)
from workflow._text_to_sql_solver_reentry import build_production_reentry_boundary
from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
from workflow.adaptive_state_store import (
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.adaptive_solver_checkpoint import (
    AdaptiveSolverCheckpointConflictError,
    AdaptiveSolverCheckpointCorruptionError,
    AdaptiveSolverCheckpointStore,
)
from workflow.deadline import DeadlineBudget
from workflow.text_to_sql_adaptive_solver import (
    _reservation_authority,
    _solver_context,
    _state_after_known_finalizer,
    reconcile_known_finalizer,
    reconcile_pending_finalizer_unknown,
    run_adaptive_sql_generation,
)
from workflow.text_to_sql_contract import TextToSqlTerminalResult


def _ready_state():
    state, *_ = _runtime()
    return _passed_through(
        state,
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )


def test_solver_context_includes_trusted_document_content() -> None:
    state, research, requirements, _ = _runtime()
    document = SchemaEvidenceDocument(
        document_id="document-formula",
        namespace="main",
        schema_namespace_version=research.schema_namespace_version,
        source_version="sha256:document-formula",
        title="Metric definition",
        content="required metric = SUM(value) / COUNT(record_id)",
        target=None,
    )
    runtime = SimpleNamespace(
        verified_research_policy=SimpleNamespace(
            model_budget=SimpleNamespace(input_tokens_per_call=16_000)
        ),
        document_snapshot=(document,),
    )

    payload = json.loads(_solver_context(runtime, state, requirements))

    assert payload["trusted_documents"] == [
        {
            "content": document.content,
            "document_id": document.document_id,
        }
    ]


def test_solver_context_includes_row_preservation_requirements() -> None:
    state, _, requirements, _ = _runtime()
    runtime = SimpleNamespace(
        verified_research_policy=SimpleNamespace(
            model_budget=SimpleNamespace(input_tokens_per_call=16_000)
        ),
        document_snapshot=(),
    )
    payload = json.loads(_solver_context(runtime, state, requirements))

    assert payload["coverage_requirements"]["row_preservation_requirements"] == [
        item.model_dump(mode="json")
        for item in requirements.row_preservation_requirements
    ]


def _reservation(store, state):
    candidate = state.sql_candidates[-1]
    return store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="finalizer-1",
        request={
            "operation": "finalize_text_to_sql_run",
            "row_limit": 10,
            "dry_run_only": False,
        },
    )


def _successful_terminal(state):
    sql = state.sql_candidates[-1].sql
    execution = {
        "success": True,
        "data": [["active"]],
        "columns": ["status"],
        "rows_affected": 1,
        "execution_time_ms": 3,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": sql,
        "applied_row_limit": 10,
    }
    return {
        "run_id": state.run_id,
        "status": "succeeded",
        "reason_code": "",
        "sql": sql,
        "generated": True,
        "approved": True,
        "executed": True,
        "dry_run": False,
        "audited": True,
        "data": [["active"]],
        "columns": ["status"],
        "rows_affected": 1,
        "error": None,
        "execution": execution,
        "audit": {"status": "logged", "log_id": "audit-1"},
        "persistence": {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
        },
        "ambiguity": None,
        "result_review": {
            "record_kind": "text2sql_result_review",
            "run_id": state.run_id,
            "run_incarnation": state.run_incarnation,
            "research_state_revision": state.revision,
            "candidate_id": state.sql_candidates[-1].candidate_id,
            "normalized_ast_digest": state.sql_candidates[-1].normalized_ast_digest,
            "requirements_digest": "sha256:" + "0" * 64,
            "source_id": None,
            "evidence_id": None,
            "verdict": "consistent",
            "reason": "matches trusted evidence",
            "execution": execution,
            "deterministic_failure_code": None,
        },
    }


def _result_contradiction_receipt(state):
    candidate = state.sql_candidates[-1]
    return ResultContradictionReceipt(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        research_state_revision=candidate.revision,
        candidate_id=candidate.candidate_id,
        normalized_ast_digest=candidate.normalized_ast_digest,
        requirements_digest="sha256:" + "0" * 64,
        finding=ResultContradictionFinding(
            expectation=ResultExpectation(
                source_id=state.query_spec.semantic_items[0].source_id,
                evidence_id="evidence-status-value",
                kind=ResultExpectationKind.FILTER_MATCH_ABSENT,
                column=ColumnRef(
                    table=TableRef(namespace="main", schema=None, table="orders"),
                    column="status",
                ),
            ),
            ast_node_id="root-projection-1",
            output_index=None,
        ),
        execution=_successful_terminal(state)["execution"],
    )


def _persisted_result_contradiction_checkpoint(
    tmp_path,
    *,
    include_exact_expectation: bool = True,
    receipt_requirements_digest: str | None = None,
    result_review_reason: str | None = None,
    result_review_verdict: str = "contradicted",
    deterministic_failure_code: CheckFailureCode | None = None,
    repair_kind: str | None = None,
    predicate_authority: PredicateRef | None = None,
):
    _, research, _, _ = _runtime()
    evidence = next(
        item
        for item in research.evidence
        if item.evidence_id == "evidence-status-value"
    )
    expectation = ResultExpectation(
        source_id=research.query_spec.semantic_items[0].source_id,
        evidence_id=evidence.evidence_id,
        kind=ResultExpectationKind.FILTER_MATCH_ABSENT,
        column=evidence.target,
    )
    if predicate_authority is not None:
        selected = next(
            binding
            for binding in research.bindings
            if binding.source_id == expectation.source_id
        )
        physical = PhysicalColumnBinding(
            binding_id="physical-status",
            source_id=selected.source_id,
            tables=selected.tables,
            columns=selected.columns,
            predicates=(),
            join_path=selected.join_path,
            evidence_ids=selected.evidence_ids,
            confidence=selected.confidence,
            status=selected.status,
            validator_rule=selected.validator_rule,
            physical_column=predicate_authority.left,
        )
        query_spec = research.query_spec.model_copy(
            update={
                "semantic_items": tuple(
                    item.model_copy(
                        update={
                            "kind": SemanticItemKind.DIMENSION,
                            "operator": None,
                            "literal_or_reference": None,
                            "binding_ids": (physical.binding_id,),
                        }
                    )
                    if item.source_id == expectation.source_id
                    else item
                    for item in research.query_spec.semantic_items
                )
            }
        )
        research = ResearchState.model_validate(
            {
                **research.model_dump(mode="python"),
                "query_spec": query_spec,
                "bindings": (physical,),
                "result_expectations": (expectation,)
                if include_exact_expectation
                else (),
            }
        )
    else:
        research = research.model_copy(
            update={
                "result_expectations": (expectation,)
                if include_exact_expectation
                else (),
            }
        )
    runtime, store = _generation_runtime(tmp_path, research_state=research)
    runtime.verified_research_state = research
    requirements = validate_coverage_inputs(
        research,
        live_solver_document_freshness_context(runtime, research),
        research.run_id,
        research.run_incarnation,
    )
    initial = SolverState(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        query_spec=research.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    proposal = apply_solver_proposal(
        initial,
        SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql=(
                    "SELECT o.status FROM orders o WHERE o.status = 'active' "
                    "ORDER BY o.status"
                ),
            ),
        ),
        base_revision=initial.revision,
        dsn=runtime.dsn,
        table_namespace="main",
        requirements=requirements,
        id_factory=iter(("candidate-1", "plan-1", "action-1")).__next__,
    )
    store.initialize(initial)
    checkpoint = store.load(research.run_id, research.run_incarnation)
    assert checkpoint is not None
    checkpoint = store.commit_non_execution(
        initial,
        proposal.state,
        action_revision=checkpoint.cursor.next_action_revision,
        action=proposal.action.model_dump(mode="json"),
        replay_input=proposal.replay_input,
    )
    for kind in (
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    ):
        transition = append_solver_check_result(
            checkpoint.state,
            _check(checkpoint.state.sql_candidates[-1].candidate_id, kind),
            base_revision=checkpoint.state.revision,
        )
        checkpoint = store.commit_non_execution(
            checkpoint.state,
            transition.state,
            action_revision=checkpoint.cursor.next_action_revision,
            action={
                "kind": "solver_check",
                "check": transition.check_result.model_dump(mode="json"),
            },
        )
    state = checkpoint.state
    if result_review_reason is None:
        receipt = _result_contradiction_receipt(state).model_copy(
            update={
                "requirements_digest": receipt_requirements_digest
                or requirements.requirements_digest,
                "finding": ResultContradictionFinding(
                    expectation=expectation,
                    ast_node_id="root-projection-1",
                    output_index=None,
                ),
            }
        )
    else:
        receipt = ResultReviewReceipt(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            research_state_revision=state.sql_candidates[-1].revision,
            candidate_id=state.sql_candidates[-1].candidate_id,
            normalized_ast_digest=state.sql_candidates[-1].normalized_ast_digest,
            requirements_digest=receipt_requirements_digest
            or requirements.requirements_digest,
            source_id=expectation.source_id,
            evidence_id=expectation.evidence_id,
            verdict=result_review_verdict,
            reason=result_review_reason,
            execution=_successful_terminal(state)["execution"],
            deterministic_failure_code=deterministic_failure_code,
            repair_kind=repair_kind,
            predicate_authority=predicate_authority,
        )
    replay = store.load_transition_replay_input(
        research.run_id,
        research.run_incarnation,
        0,
    )
    assert type(replay) is SolverSqlProposalReplayInput
    assert replay.requirements == requirements
    current_requirements = validate_coverage_inputs(
        research,
        live_solver_document_freshness_context(runtime, research),
        research.run_id,
        research.run_incarnation,
    )
    assert current_requirements.requirements_digest != requirements.requirements_digest
    reservation = store.reserve_execution(
        state,
        action_revision=checkpoint.cursor.next_action_revision,
        candidate_id=state.sql_candidates[-1].candidate_id,
        execution_id="finalizer-1",
        request={
            "operation": "finalize_text_to_sql_run",
            "row_limit": 10,
            "dry_run_only": False,
        },
    )
    checkpoint = reconcile_known_finalizer(
        store,
        reservation,
        state,
        receipt.model_dump(mode="json"),
    )
    chain = store.load_replay_chain(state.run_id, state.run_incarnation)

    assert chain is not None
    assert canonical_json_bytes(chain.reconciliations[-1].result) == canonical_json_bytes(
        receipt.model_dump(mode="json")
    )
    assert checkpoint.terminal is None
    return runtime, store, checkpoint, research, requirements, receipt, reservation, evidence


def _result_contradiction_ids(receipt, reservation) -> tuple[str, str]:
    reservation_identity = {
        "run_id": reservation.run_id,
        "run_incarnation": reservation.run_incarnation,
        "action_revision": reservation.action_revision,
        "base_state_revision": reservation.base_state_revision,
        "base_state_digest": reservation.base_state_digest,
        "candidate_id": reservation.candidate_id,
        "execution_id": reservation.execution_id,
        "normalized_ast_digest": reservation.normalized_ast_digest,
        "request_digest": reservation.request_digest,
    }
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "receipt": receipt.model_dump(mode="json"),
                "reservation": reservation_identity,
            }
        )
    ).hexdigest()
    return (
        f"result-contradiction-request-{digest}",
        f"result-contradiction-action-{digest}",
    )


def test_verified_solver_terminal_evidence_is_canonical_closed_and_private(
    tmp_path,
) -> None:
    state = _ready_state()
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(state)
    reservation = _reservation(store, state)
    terminal = TextToSqlTerminalResult.from_mapping(_successful_terminal(state))
    after_state = _state_after_known_finalizer(state, reservation, terminal)

    evidence = build_verified_solver_terminal_evidence(
        state,
        after_state,
        _reservation_authority(reservation),
        terminal,
    )
    encoded = encode_verified_solver_terminal_evidence(evidence)

    assert decode_verified_solver_terminal_evidence(encoded) == evidence
    assert set(evidence.model_dump(mode="json")) == {
        "schema_version",
        "record_kind",
        "run_id",
        "run_incarnation",
        "schema_namespace_version",
        "query",
        "solver_state",
        "reservation",
        "terminal",
    }
    for forbidden in (
        state.sql_candidates[-1].sql,
        "active",
        "audit-1",
        "/tmp/query.md",
    ):
        assert forbidden.encode() not in encoded

    document = evidence.model_dump(mode="json")
    document["future"] = True
    assert (
        decode_verified_solver_terminal_evidence(canonical_json_bytes(document)) is None
    )
    document = evidence.model_dump(mode="json")
    document["schema_version"] = 2
    assert (
        decode_verified_solver_terminal_evidence(canonical_json_bytes(document)) is None
    )


def test_known_finalizer_persists_only_privacy_safe_terminal_evidence(
    tmp_path,
) -> None:
    question_marker = "Q7vX9!"
    data_marker = "row-private-marker-71"
    error_marker = "error-private-marker-72"
    audit_marker = "audit-private-marker-73"
    path_marker = "/tmp/private-path-marker-74.md"
    state = _ready_state()
    semantic_item = state.query_spec.semantic_items[0].model_copy(
        update={"source_text": question_marker}
    )
    query_spec = state.query_spec.model_copy(
        update={
            "original_text": question_marker,
            "semantic_items": (semantic_item,),
        }
    )
    state = state.model_copy(update={"query_spec": query_spec})
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(state)
    terminal = _successful_terminal(state)
    terminal.update(
        status="failed",
        reason_code="RESULT_AGGREGATION_FAILED",
        data=[[data_marker]],
        error=error_marker,
        audit={"status": "logged", "log_id": audit_marker},
        persistence={
            "status": "saved",
            "filename": "query.md",
            "path": path_marker,
        },
        result_review={},
    )
    terminal["execution"] = {
        **terminal["execution"],
        "data": [[data_marker]],
    }

    checkpoint = reconcile_known_finalizer(
        store,
        _reservation(store, state),
        state,
        terminal,
    )

    assert checkpoint.verified_terminal_evidence is not None
    with sqlite3.connect(store.db_path) as connection:
        result_bytes = connection.execute(
            """
            SELECT result_bytes
            FROM adaptive_solver_checkpoint_execution_reconciliations
            """
        ).fetchone()[0]
    for forbidden in (
        question_marker,
        state.sql_candidates[-1].sql,
        data_marker,
        error_marker,
        audit_marker,
        path_marker,
    ):
        assert forbidden.encode() not in result_bytes
    assert json.loads(result_bytes)["terminal"] == {
        "approved": True,
        "audited": True,
        "digest": checkpoint.verified_terminal_evidence.terminal.digest,
        "dry_run": False,
        "executed": True,
        "generated": True,
            "reason_code": "RESULT_AGGREGATION_FAILED",
            "result_review_digest": None,
            "result_review_verdict": None,
            "status": "failed",
    }


def test_known_finalizer_uses_existing_execution_adapter_and_seals_bytes(
    tmp_path,
) -> None:
    state = _ready_state()
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(state)
    reservation = _reservation(store, state)
    terminal = _successful_terminal(state)

    checkpoint = reconcile_known_finalizer(
        store,
        reservation,
        state,
        terminal,
    )

    assert checkpoint.state.stop_reason is SolverStopReason.SOLVED
    assert checkpoint.state.execution_results[0].execution_id == "finalizer-1"
    assert checkpoint.terminal is not None
    assert checkpoint.verified_terminal_evidence is not None
    assert checkpoint.terminal.terminal_bytes == canonical_json_bytes(terminal)
    assert reconcile_known_finalizer(store, reservation, state, terminal) == checkpoint
    changed = {
        **terminal,
        "status": "failed",
        "reason_code": "RESULT_AGGREGATION_FAILED",
        "error": "aggregation failed",
    }
    with pytest.raises(AdaptiveSolverCheckpointConflictError):
        reconcile_known_finalizer(store, reservation, state, changed)


def test_result_contradiction_reducer_keeps_successful_execution_open(tmp_path) -> None:
    from workflow._text_to_sql_solver_execution_reducer import (
        state_after_result_contradiction,
    )

    state = _ready_state()
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(state)
    receipt = _result_contradiction_receipt(state)

    after = state_after_result_contradiction(
        state,
        _reservation_authority(_reservation(store, state)),
        receipt,
    )

    assert after.revision == state.revision + 1
    assert after.check_results[-1].check_kind is CheckKind.EXECUTION
    assert after.check_results[-1].status.value == "passed"
    assert after.execution_results[-1].success is True
    assert after.selected_candidate_id is None
    assert after.stop_reason is None


def test_result_contradiction_reconciles_as_open_execution_evidence(tmp_path) -> None:
    state = _ready_state()
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(state)
    reservation = _reservation(store, state)
    receipt = _result_contradiction_receipt(state)

    checkpoint = reconcile_known_finalizer(
        store,
        reservation,
        state,
        receipt.model_dump(mode="json"),
    )

    assert checkpoint.state.revision == state.revision + 1
    assert checkpoint.pending_execution is None
    assert checkpoint.terminal is None
    with sqlite3.connect(store.db_path) as connection:
        result_bytes = connection.execute(
            """
            SELECT result_bytes
            FROM adaptive_solver_checkpoint_execution_reconciliations
            """
        ).fetchone()[0]
    assert result_bytes == canonical_json_bytes(receipt.model_dump(mode="json"))


def test_result_contradiction_rejects_tampered_candidate_without_settlement(
    tmp_path,
) -> None:
    state = _ready_state()
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(state)
    reservation = _reservation(store, state)
    receipt = _result_contradiction_receipt(state).model_copy(
        update={"candidate_id": "foreign-candidate"}
    )

    with pytest.raises(ValueError):
        reconcile_known_finalizer(
            store,
            reservation,
            state,
            receipt.model_dump(mode="json"),
        )

    checkpoint = store.load(state.run_id, state.run_incarnation)
    assert checkpoint is not None
    assert checkpoint.state == state
    assert checkpoint.pending_execution == reservation
    assert checkpoint.terminal is None


def test_result_contradiction_commits_missing_evidence_from_persisted_receipt(
    tmp_path,
) -> None:
    (
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        _,
        _,
        evidence,
    ) = _persisted_result_contradiction_checkpoint(tmp_path)
    from workflow.text_to_sql_adaptive_solver import (
        _commit_result_contradiction_missing_evidence,
    )

    _commit_result_contradiction_missing_evidence(
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        table_namespace="main",
    )

    committed = store.load(checkpoint.state.run_id, checkpoint.state.run_incarnation)
    assert committed is not None
    assert committed.state.revision == checkpoint.state.revision + 1
    assert len(committed.state.missing_evidence_requests) == 1
    assert committed.state.stop_reason is SolverStopReason.MISSING_EVIDENCE
    request = committed.state.missing_evidence_requests[0]
    action = committed.state.action_history[-1]
    assert request.question
    assert request.reason
    assert request.required_evidence_kind is evidence.source_kind
    assert action.kind is SolverActionKind.MISSING_EVIDENCE
    assert action.missing_evidence_request_id == request.missing_evidence_request_id
    replay = store.load_transition_replay_input(
        committed.state.run_id,
        committed.state.run_incarnation,
        committed.cursor.next_action_revision - 1,
    )
    assert type(replay) is SolverMissingEvidenceReplayInput
    assert type(replay.proposal) is SolverProposalV1
    assert type(replay.proposal.proposal) is MissingEvidenceProposal
    assert replay.proposal.proposal.required_evidence_kind is evidence.source_kind
    assert replay.requirements == requirements
    assert replay.generated_ids == (request.missing_evidence_request_id, action.action_id)


def test_semantic_binding_mismatch_persists_exact_selected_binding_for_research(
    tmp_path,
) -> None:
    (
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        _,
        _,
        _,
    ) = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="selected attribute is only a related proxy",
        result_review_verdict="ambiguous",
        repair_kind="semantic_binding_mismatch",
    )
    from workflow.text_to_sql_adaptive_solver import (
        _commit_result_contradiction_missing_evidence,
    )

    _commit_result_contradiction_missing_evidence(
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        table_namespace="main",
    )

    committed = store.load(checkpoint.state.run_id, checkpoint.state.run_incarnation)
    assert committed is not None
    request = committed.state.missing_evidence_requests[-1]
    selected = tuple(
        binding
        for binding in requirements.selected_bindings
        if binding.source_id == request.source_id
    )
    assert len(selected) == 1
    assert request.repair_kind == "semantic_binding_mismatch"
    assert request.repair_binding_id == selected[0].binding_id


def test_result_review_predicate_authority_routes_exact_value_search_reentry(
    tmp_path,
) -> None:
    _, research, _, _ = _runtime()
    evidence = next(
        item for item in research.evidence if item.evidence_id == "evidence-status-value"
    )
    predicate = PredicateRef(
        left=evidence.target,
        operator=PredicateOperator.EQ,
        right="active",
    )
    (
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        _,
        _,
        _,
    ) = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="verify the exact status value",
        predicate_authority=predicate,
    )
    from workflow.text_to_sql_adaptive_solver import (
        _commit_result_contradiction_missing_evidence,
    )

    _commit_result_contradiction_missing_evidence(
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        table_namespace="main",
    )

    committed = store.load(research.run_id, research.run_incarnation)
    assert committed is not None
    request = committed.state.missing_evidence_requests[-1]
    assert request.required_evidence_kind is EvidenceSourceKind.VALUE_SEARCH
    assert request.predicate_authority == predicate
    replay = store.load_transition_replay_input(
        research.run_id,
        research.run_incarnation,
        committed.cursor.next_action_revision - 1,
    )
    assert type(replay) is SolverMissingEvidenceReplayInput
    assert replay.proposal.proposal.predicate_authority == predicate


def test_result_contradiction_missing_evidence_ids_are_stable_on_resume(
    tmp_path,
) -> None:
    (
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        receipt,
        reservation,
        _,
    ) = _persisted_result_contradiction_checkpoint(tmp_path)
    from workflow.text_to_sql_adaptive_solver import (
        _commit_result_contradiction_missing_evidence,
    )

    expected_request_id, expected_action_id = _result_contradiction_ids(
        receipt,
        reservation,
    )
    _commit_result_contradiction_missing_evidence(
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        table_namespace="main",
    )
    committed = store.load(checkpoint.state.run_id, checkpoint.state.run_incarnation)
    assert committed is not None
    request = committed.state.missing_evidence_requests[0]
    action = committed.state.action_history[-1]
    assert request.missing_evidence_request_id == expected_request_id
    assert action.action_id == expected_action_id

    _commit_result_contradiction_missing_evidence(
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        table_namespace="main",
    )
    resumed = store.load(checkpoint.state.run_id, checkpoint.state.run_incarnation)
    assert resumed is not None
    assert resumed.state.missing_evidence_requests == (request,)
    assert resumed.state.action_history == committed.state.action_history


@pytest.mark.parametrize(
    ("include_exact_expectation", "receipt_requirements_digest"),
    (
        (False, None),
        (True, "sha256:" + "f" * 64),
    ),
)
def test_result_contradiction_rejects_invalid_receipt_before_missing_evidence(
    tmp_path,
    include_exact_expectation,
    receipt_requirements_digest,
) -> None:
    (
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        _,
        _,
        _,
    ) = _persisted_result_contradiction_checkpoint(
        tmp_path,
        include_exact_expectation=include_exact_expectation,
        receipt_requirements_digest=receipt_requirements_digest,
    )
    from workflow.text_to_sql_adaptive_solver import (
        _commit_result_contradiction_missing_evidence,
    )

    with pytest.raises(ValueError):
        _commit_result_contradiction_missing_evidence(
            runtime,
            store,
            checkpoint,
            research,
            requirements,
            table_namespace="main",
        )

    unchanged = store.load(checkpoint.state.run_id, checkpoint.state.run_incarnation)
    assert unchanged == checkpoint


@pytest.mark.parametrize(
    ("result_review_reason", "result_review_verdict", "repair_kind"),
    (
        (None, "contradicted", None),
        (
            "The returned rows do not establish the requested entity-time computation.",
            "ambiguous",
            None,
        ),
        (
            "The selected binding contradicts the requested semantic attribute.",
            "contradicted",
            "semantic_binding_mismatch",
        ),
    ),
)
def test_resume_result_contradiction_commits_missing_evidence_before_reentry(
    monkeypatch,
    tmp_path,
    result_review_reason,
    result_review_verdict,
    repair_kind,
) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator

    (
        runtime,
        store,
        _,
        research,
        _,
        receipt,
        reservation,
        _,
    ) = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason=result_review_reason,
        result_review_verdict=result_review_verdict,
        repair_kind=repair_kind,
    )
    expected_request_id, expected_action_id = _result_contradiction_ids(
        receipt,
        reservation,
    )
    calls = {"propose": 0, "reenter": 0}
    reentry_request = {}

    async def forbidden_propose(*_args):
        calls["propose"] += 1
        raise AssertionError("persisted result contradiction must enter re-entry first")

    async def reenter(
        solver_state,
        research_state,
        request_id,
        *,
        commit_solver_admission,
        id_factory,
        **_kwargs,
    ):
        calls["reenter"] += 1
        request = solver_state.missing_evidence_requests[-1]
        action = solver_state.action_history[-1]
        reentry_request.update(question=request.question, reason=request.reason)
        assert request_id == expected_request_id
        assert request.missing_evidence_request_id == expected_request_id
        assert action.action_id == expected_action_id
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

    def forbidden_pre_execution(*_args, **_kwargs):
        raise AssertionError("persisted result contradiction must commit S+2 first")

    monkeypatch.setattr(
        coordinator,
        "run_solver_candidate_pre_execution_gates",
        forbidden_pre_execution,
    )

    asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden_propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=reenter,
            id_factory=iter(("result-contradiction-reentry-1",)).__next__,
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert calls == {"propose": 0, "reenter": 1}
    assert checkpoint is not None
    assert checkpoint.state.missing_evidence_requests[0].missing_evidence_request_id == (
        expected_request_id
    )
    if result_review_reason is not None:
        assert reentry_request == {
            "question": result_review_reason,
            "reason": result_review_reason,
        }


def test_reentry_admission_replays_durable_snapshot_after_budget_projection(
    tmp_path,
) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator

    runtime, store, checkpoint, research, requirements, *_ = (
        _persisted_result_contradiction_checkpoint(tmp_path)
    )
    checkpoint = coordinator._commit_result_contradiction_missing_evidence(
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        table_namespace="main",
    )
    budget = research.budget_state.model_copy(
        update={
            "used_model_calls": research.budget_state.used_model_calls + 1,
            "remaining_model_calls": research.budget_state.remaining_model_calls - 1,
            "used_model_tokens": research.budget_state.used_model_tokens + 1,
            "remaining_model_tokens": research.budget_state.remaining_model_tokens - 1,
        }
    )
    projected = research.model_copy(update={"budget_state": budget})
    calls = {"admission": 0}

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
        calls["admission"] += 1
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

    asyncio.run(
        coordinator._run_reentry(
            runtime,
            store,
            checkpoint,
            projected,
            requirements,
            _context(
                run_id=projected.run_id,
                incarnation=projected.run_incarnation,
                schema=projected.schema_namespace_version,
            ),
            reenter,
            iter(("result-contradiction-reentry-1",)).__next__,
        )
    )

    assert calls == {"admission": 1}


def test_resume_deterministic_result_shape_revises_sql_without_reentry(
    monkeypatch,
    tmp_path,
) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator

    (
        runtime,
        store,
        _,
        _,
        _,
        receipt,
        _,
        _,
    ) = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="an auxiliary output is not requested",
        deterministic_failure_code=CheckFailureCode.RESULT_SHAPE_MISMATCH,
    )
    calls = {"propose": 0, "reenter": 0}

    class DirectRepairObserved(BaseException):
        pass

    async def propose(_state, _requirements, repair_receipt):
        calls["propose"] += 1
        assert repair_receipt == receipt
        return SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT o.status FROM orders o WHERE o.status = 'active'",
            ),
        )

    async def forbidden_reenter(*_args, **_kwargs):
        calls["reenter"] += 1
        raise AssertionError("deterministic SQL shape repair must not research")

    async def forbidden_resume(*_args, **_kwargs):
        raise AssertionError("deterministic SQL shape repair must call solver directly")

    def stop_after_direct_commit(*_args, **_kwargs):
        raise DirectRepairObserved()

    monkeypatch.setattr(coordinator, "_resume_open_generation", forbidden_resume)
    monkeypatch.setattr(coordinator, "_run_pre_execution", stop_after_direct_commit)

    with pytest.raises(DirectRepairObserved):
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=propose,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
                reenter=forbidden_reenter,
            )
        )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert calls == {"propose": 1, "reenter": 0}
    assert checkpoint is not None
    assert checkpoint.state.missing_evidence_requests == ()
    assert len(checkpoint.state.sql_candidates) == 2


def test_resume_proven_result_review_contradiction_revises_sql_without_reentry(
    monkeypatch,
    tmp_path,
) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator

    (
        runtime,
        store,
        _,
        _,
        _,
        receipt,
        _,
        _,
    ) = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="the SQL adds an undocumented intermediate aggregation",
        result_review_verdict="contradicted",
    )
    calls = {"propose": 0, "reenter": 0}

    class DirectRepairObserved(BaseException):
        pass

    async def propose(_state, _requirements, repair_receipt):
        calls["propose"] += 1
        assert repair_receipt == receipt
        return SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT MAX(o.amount) FROM orders o",
            ),
        )

    async def forbidden_reenter(*_args, **_kwargs):
        calls["reenter"] += 1
        raise AssertionError("proven SQL contradiction must not research")

    async def forbidden_resume(*_args, **_kwargs):
        raise AssertionError("proven SQL contradiction must call solver directly")

    def stop_after_direct_commit(*_args, **_kwargs):
        raise DirectRepairObserved()

    monkeypatch.setattr(coordinator, "_resume_open_generation", forbidden_resume)
    monkeypatch.setattr(coordinator, "_run_pre_execution", stop_after_direct_commit)

    with pytest.raises(DirectRepairObserved):
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=propose,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
                reenter=forbidden_reenter,
            )
        )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert calls == {"propose": 1, "reenter": 0}
    assert checkpoint is not None
    assert checkpoint.state.missing_evidence_requests == ()
    assert len(checkpoint.state.sql_candidates) == 2


def test_deterministic_result_shape_rejects_missing_evidence_proposal(
    tmp_path,
) -> None:
    (
        runtime,
        store,
        _,
        _,
        _,
        receipt,
        _,
        _,
    ) = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="an auxiliary output is not requested",
        deterministic_failure_code=CheckFailureCode.RESULT_SHAPE_MISMATCH,
    )
    calls = {"propose": 0, "reenter": 0}

    async def propose(_state, _requirements, repair_receipt):
        calls["propose"] += 1
        assert repair_receipt == receipt
        return SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id=receipt.source_id,
                question="unneeded",
                required_evidence_kind=EvidenceSourceKind.VALUE_SEARCH,
                reason="unneeded",
            ),
        )

    async def forbidden_reenter(*_args, **_kwargs):
        calls["reenter"] += 1
        raise AssertionError("deterministic SQL shape repair must not research")

    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=forbidden_reenter,
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert calls == {"propose": 1, "reenter": 0}
    assert output["sql"] == ""
    assert checkpoint is not None
    assert checkpoint.state.missing_evidence_requests == ()
    assert checkpoint.state.stop_reason is SolverStopReason.PROTOCOL_FAILURE


def test_deterministic_result_shape_reconstructs_receipt_after_crash_before_commit(
    monkeypatch,
    tmp_path,
) -> None:
    (
        runtime,
        store,
        _,
        _,
        _,
        receipt,
        _,
        _,
    ) = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="an auxiliary output is not requested",
        deterministic_failure_code=CheckFailureCode.RESULT_SHAPE_MISMATCH,
    )
    received = []

    class CrashBeforeCommit(BaseException):
        pass

    async def crash_before_commit(_state, _requirements, repair_receipt):
        received.append(repair_receipt)
        raise CrashBeforeCommit()

    with pytest.raises(CrashBeforeCommit):
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=crash_before_commit,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
            )
        )

    class CrashAfterProposalCommit(BaseException):
        pass

    async def repair_after_restart(_state, _requirements, repair_receipt):
        received.append(repair_receipt)
        return SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql=(
                    "SELECT o.status FROM orders o WHERE o.status = 'active' "
                    "ORDER BY o.status DESC"
                ),
            ),
        )

    import workflow.text_to_sql_adaptive_solver as coordinator

    original_commit = store.commit_non_execution

    def crash_after_proposal_commit(before_state, after_state, **kwargs):
        committed = original_commit(before_state, after_state, **kwargs)
        if len(after_state.sql_candidates) == 2:
            raise CrashAfterProposalCommit()
        return committed

    monkeypatch.setattr(store, "commit_non_execution", crash_after_proposal_commit)
    try:
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=repair_after_restart,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
            )
        )
    except CrashAfterProposalCommit:
        pass
    else:
        checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
        raise AssertionError(
            f"proposal commit did not crash: received={received!r}, checkpoint={checkpoint!r}"
        )

    monkeypatch.setattr(store, "commit_non_execution", original_commit)

    async def forbidden_solver(*_args, **_kwargs):
        raise AssertionError("committed repair must not resend result-review feedback")

    calls = []

    def pass_committed_candidate_gates(state, candidate_id, *, commit_transition, **_kwargs):
        calls.append(candidate_id)
        for kind in (
            CheckKind.SAFETY,
            CheckKind.SCHEMA,
            CheckKind.SEMANTIC,
            CheckKind.EXPLAIN,
        ):
            transition = append_solver_check_result(
                state,
                _check(candidate_id, kind),
                base_revision=state.revision,
            )
            state = commit_transition(transition)
        return state

    monkeypatch.setattr(
        coordinator,
        "run_solver_candidate_pre_execution_gates",
        pass_committed_candidate_gates,
    )
    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden_solver,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert received == [receipt, receipt]
    assert output["sql"].endswith("ORDER BY o.status DESC")
    assert checkpoint is not None
    assert len(checkpoint.state.sql_candidates) == 2
    assert calls == [checkpoint.state.sql_candidates[-1].candidate_id]


@pytest.mark.parametrize(
    ("verdict", "open_repair"),
    (
        ("contradicted", True),
        ("ambiguous", True),
        ("consistent", False),
        ("malformed", False),
        ("timeout", False),
    ),
)
def test_result_reentry_receipt_opens_only_actionable_review(tmp_path, verdict, open_repair) -> None:
    from workflow.text_to_sql_adaptive_solver import _result_reentry_receipt

    _, _, _, _, _, receipt, _, _ = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="synthetic result review",
    )
    source_id, evidence_id = (
        (receipt.source_id, receipt.evidence_id)
        if verdict in {"contradicted", "ambiguous"}
        else (None, None)
    )
    review = ResultReviewReceipt(
        run_id=receipt.run_id,
        run_incarnation=receipt.run_incarnation,
        research_state_revision=receipt.research_state_revision,
        candidate_id=receipt.candidate_id,
        normalized_ast_digest=receipt.normalized_ast_digest,
        requirements_digest=receipt.requirements_digest,
        source_id=source_id,
        evidence_id=evidence_id,
        verdict=verdict,
        reason=receipt.reason,
        execution=receipt.execution,
        deterministic_failure_code=None,
    )

    if open_repair:
        assert _result_reentry_receipt(review.model_dump(mode="json")) == review
    else:
        with pytest.raises(ValueError, match="not actionable"):
            _result_reentry_receipt(review.model_dump(mode="json"))


def test_resume_completed_result_contradiction_reentry_proposes_new_candidate(
    monkeypatch,
    tmp_path,
) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator
    from workflow.text_to_sql_adaptive_solver import (
        _commit_result_contradiction_missing_evidence,
    )

    (
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        _,
        _,
        evidence,
    ) = _persisted_result_contradiction_checkpoint(tmp_path)
    checkpoint = _commit_result_contradiction_missing_evidence(
        runtime,
        store,
        checkpoint,
        research,
        requirements,
        table_namespace="main",
    )
    request = checkpoint.state.missing_evidence_requests[-1]
    action = ResearchAction(
        action_id="result-contradiction-refresh-action",
        kind=ResearchActionKind.SEARCH_VALUE,
        hypothesis_id=None,
        target=evidence.target,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.SEARCH_VALUE,
            hypothesis_id=None,
            target=evidence.target,
            parameters=(),
            expected_revision=research.revision,
        ),
        expected_revision=research.revision,
    )
    refreshed_observation = json.loads(evidence.observation)
    refreshed_observation["invocation_id"] = "evidence-status-value-refresh"
    refreshed_observation["provenance"]["action_digest"] = action.action_digest
    refreshed_observation["provenance"]["invocation_id"] = (
        "evidence-status-value-refresh"
    )
    refreshed_evidence = evidence.model_copy(
        update={
            "evidence_id": "evidence-status-value-refresh",
            "revision": research.revision + 1,
            "action_digest": action.action_digest,
            "observation": canonical_json_bytes(refreshed_observation).decode("utf-8"),
        }
    )
    completed_research = type(research).model_validate(
        {
            **research.model_dump(mode="python"),
            "revision": research.revision + 1,
            "evidence": (*research.evidence, refreshed_evidence),
            "action_history": (*research.action_history, action),
        }
    )
    freshness = _context(
        run_id=completed_research.run_id,
        incarnation=completed_research.run_incarnation,
        schema=completed_research.schema_namespace_version,
    )
    completed_requirements = validate_coverage_inputs(
        completed_research,
        freshness,
        completed_research.run_id,
        completed_research.run_incarnation,
    )
    with sqlite3.connect(runtime.research_state_store.db_path) as connection:
        connection.execute(
            """
            INSERT INTO adaptive_research_state_snapshots (
                run_id, run_incarnation, contract_name, revision,
                payload, digest, created_at_ns
            ) VALUES (?, ?, 'research_state', ?, ?, ?, ?)
            """,
            (
                completed_research.run_id,
                completed_research.run_incarnation,
                completed_research.revision,
                serialize_contract(completed_research),
                canonical_digest(completed_research),
                completed_research.revision + 1,
            ),
        )
    admitted = admit_targeted_reentry(
        checkpoint.state,
        research,
        request.missing_evidence_request_id,
        base_revision=checkpoint.state.revision,
        id_factory=iter(("result-contradiction-reentry-1",)).__next__,
    )
    checkpoint = store.commit_non_execution(
        checkpoint.state,
        admitted.state,
        action_revision=checkpoint.cursor.next_action_revision,
        action={
            "kind": "research_reentry_admitted",
            "record": admitted.record.model_dump(mode="json"),
        },
        replay_input=SolverReentryAdmissionReplayInput(
            research_state_revision=research.revision,
            research_state_digest=canonical_digest(research),
            missing_evidence_request_id=request.missing_evidence_request_id,
            generated_reentry_id=admitted.record.research_reentry_id,
        ),
    )
    finalized = finalize_targeted_reentry(
        checkpoint.state,
        admitted.record.research_reentry_id,
        ResearchReentryStatus.COMPLETED,
        base_revision=checkpoint.state.revision,
        research_state=completed_research,
        freshness_context=freshness,
        requirements=completed_requirements,
    )
    checkpoint = store.commit_non_execution(
        checkpoint.state,
        finalized.state,
        action_revision=checkpoint.cursor.next_action_revision,
        action={
            "kind": "research_reentry_finalized",
            "record": finalized.record.model_dump(mode="json"),
        },
        replay_input=SolverReentryCompletedReplayInput(
            research_reentry_id=finalized.record.research_reentry_id,
            research_state_revision=completed_research.revision,
            research_state_digest=canonical_digest(completed_research),
            freshness_context=freshness,
            requirements=completed_requirements,
        ),
    )
    runtime.verified_research_state = completed_research
    calls = {"propose": 0}

    async def propose(_state, _requirements):
        calls["propose"] += 1
        return SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT o.status FROM orders o WHERE o.status = 'inactive'",
            ),
        )

    def pass_new_candidate_gates(state, candidate_id, *, commit_transition, **_kwargs):
        assert candidate_id != "candidate-1", (
            "completed result contradiction reentry must not rerun old candidate"
        )
        for kind in (
            CheckKind.SAFETY,
            CheckKind.SCHEMA,
            CheckKind.SEMANTIC,
            CheckKind.EXPLAIN,
        ):
            transition = append_solver_check_result(
                state,
                _check(candidate_id, kind),
                base_revision=state.revision,
            )
            state = commit_transition(transition)
        return state

    monkeypatch.setattr(
        coordinator,
        "run_solver_candidate_pre_execution_gates",
        pass_new_candidate_gates,
    )
    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    assert calls == {"propose": 1}
    assert output["sql"].endswith("'inactive'")


def test_deterministic_rejection_seals_without_execution_result(tmp_path) -> None:
    state = _ready_state()
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(state)
    reservation = _reservation(store, state)
    sql = state.sql_candidates[-1].sql
    terminal = {
        "run_id": state.run_id,
        "status": "abstained",
        "reason_code": "DETERMINISTIC_CHECK_REJECTED",
        "sql": sql,
        "generated": True,
        "approved": False,
        "executed": False,
        "dry_run": False,
        "audited": False,
        "data": [],
        "columns": [],
        "rows_affected": 0,
        "error": None,
        "execution": {},
            "audit": {},
                "persistence": {"status": "not_attempted"},
                "ambiguity": None,
                "result_review": {},
            }

    checkpoint = reconcile_known_finalizer(
        store,
        reservation,
        state,
        terminal,
    )

    assert checkpoint.state.stop_reason is SolverStopReason.NO_SAFE_CANDIDATE
    assert checkpoint.state.execution_results == ()
    assert checkpoint.terminal is not None
    assert checkpoint.verified_terminal_evidence is not None


@pytest.mark.parametrize(
    ("reason_code", "overrides"),
    (
        (
            "AUDIT_FAILED",
            {
                "audited": False,
                "error": "audit unavailable",
                "audit": {"status": "error", "error": "audit unavailable"},
                "persistence": {"status": "not_attempted"},
            },
        ),
        (
            "AUDIT_CONTRACT_INVALID",
            {
                "audited": False,
                "error": "invalid audit result",
                "audit": {"status": "error", "error": "invalid audit result"},
                "persistence": {"status": "not_attempted"},
            },
        ),
        (
            "PERSISTENCE_CONTRACT_INVALID",
            {
                "error": "invalid persistence result",
                "persistence": {
                    "status": "error",
                    "error": "invalid persistence result",
                },
            },
        ),
        (
            "RESULT_AGGREGATION_FAILED",
            {"error": "aggregation failed"},
        ),
    ),
)
def test_successful_execution_with_later_terminal_failure_is_not_solved(
    tmp_path,
    reason_code,
    overrides,
) -> None:
    state = _ready_state()
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(state)
    reservation = _reservation(store, state)
    terminal = {
        **_successful_terminal(state),
        "status": "failed",
        "reason_code": reason_code,
        **overrides,
    }

    checkpoint = reconcile_known_finalizer(store, reservation, state, terminal)

    assert checkpoint.state.revision == state.revision + 1
    assert checkpoint.state.stop_reason is SolverStopReason.TOOL_FAILURE
    assert checkpoint.state.selected_candidate_id is None
    assert len(checkpoint.state.execution_results) == 1
    assert checkpoint.state.execution_results[0].success is True
    assert checkpoint.terminal is not None
    assert checkpoint.verified_terminal_evidence is not None
    assert checkpoint.terminal.terminal_bytes == canonical_json_bytes(terminal)


def test_pending_reservation_becomes_atomic_unknown_without_finalizer(tmp_path) -> None:
    state = _ready_state()
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(state)
    reservation = _reservation(store, state)
    pending = store.load(state.run_id, state.run_incarnation)
    assert pending is not None
    assert pending.pending_execution == reservation

    checkpoint = reconcile_pending_finalizer_unknown(store, pending)

    assert checkpoint.state.stop_reason is SolverStopReason.TOOL_FAILURE
    assert checkpoint.state.execution_results == ()
    assert checkpoint.terminal is not None
    assert checkpoint.verified_terminal_evidence is None
    assert b'"reason_code":"EXECUTION_UNKNOWN"' in (checkpoint.terminal.terminal_bytes)


def test_sealed_known_terminal_resume_wins_over_later_cancellation(tmp_path) -> None:
    runtime, store = _generation_runtime(tmp_path)
    state = _ready_state()
    store.initialize(state)
    terminal = _successful_terminal(state)
    checkpoint = reconcile_known_finalizer(
        store,
        _reservation(store, state),
        state,
        terminal,
    )
    assert checkpoint.verified_terminal_evidence is not None
    runtime.mark_cancelled()

    async def forbidden(*_args):
        raise AssertionError("sealed terminal resume must not call the model")

    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    assert output["sql"] == ""
    assert runtime.verified_solver_terminal is not None
    assert runtime.verified_solver_terminal.to_mapping() == terminal
    assert (
        store.load(state.run_id, state.run_incarnation).verified_terminal_evidence
        == checkpoint.verified_terminal_evidence
    )


def _generation_runtime(tmp_path, *, research_state=None):
    _, original_research, _, loaded_schema = _runtime()
    research = original_research if research_state is None else research_state
    database = tmp_path / "generation.sqlite"
    _seed_honest_v2_research_history(database, (research,))
    research_store = AdaptiveResearchStateStore(database)
    store = AdaptiveSolverCheckpointStore(database)
    deadline = DeadlineBudget.from_duration(30)
    scope = loaded_schema.namespace.scope.to_mapping()
    admission = TextToSqlTypedAdmission(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        deadline=deadline,
        query=research.query_spec.original_text,
        dsn=POSTGRES_DSN,
        schema_scope=scope,
        _capability=_ADMISSION_CAPABILITY,
    )
    runtime = TextToSqlTypedRuntime(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        deadline=deadline,
        query=research.query_spec.original_text,
        dsn=POSTGRES_DSN,
        schema_scope=scope,
        research_state_store=research_store,
        checkpoint_store=None,
        budget_ledger=None,
        solver_checkpoint_store=store,
        _capability=_ADMISSION_CAPABILITY,
        _admission=admission,
    )
    runtime.loaded_schema = loaded_schema
    runtime.document_registry = empty_schema_document_registry(
        loaded_schema.namespace.scope,
        loaded_schema.namespace,
    )
    checkpoint_store = AdaptiveStateStore(database)
    for revision in range(research.revision):
        key = AdaptiveCheckpointKey(
            research.run_id,
            research.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            revision,
        )
        checkpoint_store.record_planned(
            key,
            expected_revision=None if revision == 0 else revision - 1,
            action={"kind": "historical_planned", "revision": revision},
        )
        checkpoint_store.record_observed(
            key,
            expected_revision=revision,
            action={"kind": "historical_observed", "revision": revision},
        )
    checkpoint_store.record_replayable_terminal(
        AdaptiveCheckpointKey(
            research.run_id,
            research.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            research.revision,
        ),
        expected_revision=research.revision - 1,
        action={
            "affected_source_ids": [],
            "citation_evidence_ids": [],
            "contract_version": 2,
            "kind": "research_terminal",
            "rejection_signatures": [],
            "reason": "COMPLETE",
            "ambiguity": None,
        },
        replay_input=ResearchTerminalReplayInput(
            freshness_context=FreshnessContext(
                evaluated_at=datetime.now(UTC),
                run_id=research.run_id,
                run_incarnation=research.run_incarnation,
                schema_namespace_version=research.schema_namespace_version,
            )
        ),
    )
    runtime.checkpoint_store = checkpoint_store
    runtime.verified_research_state = research
    return runtime, store


def _durable_admitted_reentry_runtime(tmp_path, *, admitted: bool = True):
    runtime, proposed, research, requirements, freshness = _one_remaining_reentry_case(
        tmp_path
    )
    runtime.verified_research_state = research
    store = AdaptiveSolverCheckpointStore(tmp_path / "adaptive.sqlite")
    runtime.solver_checkpoint_store = store
    initial = SolverState(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        query_spec=research.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    store.initialize(initial)
    checkpoint = store.commit_non_execution(
        initial,
        proposed,
        action_revision=0,
        action=proposed.action_history[-1].model_dump(mode="json"),
        replay_input=SolverMissingEvidenceReplayInput(
            proposal=SolverProposalV1(
                proposal_version=1,
                proposal=MissingEvidenceProposal(
                    proposal_kind="missing_evidence",
                    source_id="status",
                    question="Refresh status column evidence",
                    required_evidence_kind=EvidenceSourceKind.SCHEMA,
                    reason="One exact schema observation is required",
                ),
            ),
            requirements=requirements,
            generated_ids=("request-1", "action-1"),
        ),
    )
    if not admitted:
        return runtime, store, checkpoint, research, requirements, freshness
    admitted = admit_targeted_reentry(
        proposed,
        research,
        "request-1",
        base_revision=proposed.revision,
        id_factory=iter(("reentry-1",)).__next__,
    )
    checkpoint = store.commit_non_execution(
        checkpoint.state,
        admitted.state,
        action_revision=checkpoint.cursor.next_action_revision,
        action={
            "kind": "research_reentry_admitted",
            "record": admitted.record.model_dump(mode="json"),
        },
        replay_input=SolverReentryAdmissionReplayInput(
            research_state_revision=research.revision,
            research_state_digest=canonical_digest(research),
            missing_evidence_request_id="request-1",
            generated_reentry_id="reentry-1",
        ),
    )
    return runtime, store, checkpoint, research, requirements, freshness


@pytest.mark.parametrize(
    ("boundary", "admitted", "reason_code"),
    (
        ("cancelled", False, "SCHEMA_CLARIFICATION_REQUIRED"),
        ("deadline", False, "SCHEMA_CLARIFICATION_REQUIRED"),
        ("cancelled", True, "CANCELLED"),
        ("deadline", True, "TIMED_OUT"),
    ),
)
def test_missing_evidence_resume_handles_control_boundary_without_second_stop(
    tmp_path,
    boundary,
    admitted,
    reason_code,
) -> None:
    runtime, store, before, _, _, _ = _durable_admitted_reentry_runtime(
        tmp_path,
        admitted=admitted,
    )
    if boundary == "cancelled":
        runtime.mark_cancelled()
    else:
        runtime.deadline = DeadlineBudget(
            deadline_monotonic=0.0,
            deadline_at_ms=0,
            monotonic=lambda: 1.0,
        )
    calls = {"proposal": 0, "reentry": 0, "id": 0}

    async def forbidden_proposal(*_args):
        calls["proposal"] += 1
        raise AssertionError("control-boundary resume must not call the model")

    async def forbidden_reentry(*_args, **_kwargs):
        calls["reentry"] += 1
        raise AssertionError("control-boundary resume must not call re-entry")

    def forbidden_id():
        calls["id"] += 1
        raise AssertionError("control-boundary resume must not allocate IDs")

    asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden_proposal,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=forbidden_reentry,
            id_factory=forbidden_id,
        )
    )

    after = store.load(runtime.run_id, runtime.run_incarnation)
    assert calls == {"proposal": 0, "reentry": 0, "id": 0}
    assert after is not None and after.terminal is not None
    assert after.cursor.next_action_revision == before.cursor.next_action_revision + (
        1 if admitted else 0
    )
    assert runtime.verified_solver_terminal.reason_code == reason_code


@pytest.mark.parametrize(("durable_turns", "expected_calls"), ((7, 1), (8, 0)))
def test_restart_preserves_durable_solver_model_turn_cap(
    monkeypatch,
    tmp_path,
    durable_turns,
    expected_calls,
) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator

    _, research, requirements, _ = _runtime()
    runtime, store = _generation_runtime(tmp_path)
    state = SolverState(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        query_spec=research.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    store.initialize(state)
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert checkpoint is not None
    ids = iter(
        f"turn-{turn}-{kind}"
        for turn in range(8)
        for kind in ("candidate", "plan", "action")
    )
    for turn in range(durable_turns):
        transition = apply_solver_proposal(
            checkpoint.state,
            SolverProposalV1(
                proposal_version=1,
                proposal=SqlCandidateProposal(
                    proposal_kind="sql_candidate",
                    sql=(
                        "SELECT o.status FROM orders o "
                        f"WHERE o.status = 'active-{turn}'"
                    ),
                ),
            ),
            base_revision=checkpoint.state.revision,
            dsn=runtime.dsn,
            table_namespace="main",
            requirements=requirements,
            id_factory=ids.__next__,
        )
        checkpoint = store.commit_non_execution(
            checkpoint.state,
            transition.state,
            action_revision=checkpoint.cursor.next_action_revision,
            action=transition.action.model_dump(mode="json"),
            replay_input=transition.replay_input,
        )
    calls = 0

    async def proposal(*_args):
        nonlocal calls
        calls += 1
        raise SolverProtocolError("stop after the one remaining durable turn")

    monkeypatch.setattr(
        coordinator,
        "run_solver_candidate_pre_execution_gates",
        lambda state, *_args, **_kwargs: state,
    )

    asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=proposal,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    assert calls == expected_calls
    if durable_turns == 8:
        assert (
            runtime.verified_solver_terminal.reason_code == "RESEARCH_BUDGET_EXHAUSTED"
        )


def test_restart_settles_exact_started_reentry_model_before_protocol_terminal(
    tmp_path,
) -> None:
    runtime, store, admitted, research, requirements, _ = (
        _durable_admitted_reentry_runtime(tmp_path)
    )
    request = admitted.state.missing_evidence_requests[-1]
    context = _research_context(
        request,
        _trusted_targets(request.source_id, research, requirements),
        research,
    )
    profile = load_schema_research_agent_profile()
    limits = runtime.verified_research_policy.model_budget
    assert limits is not None
    reservation = reserve_model_call_budget(
        research.run_id,
        research.run_incarnation,
        _model_call_id(research, 0),
        canonical_digest(
            {
                "research_context": context,
                "state": research.model_dump(mode="json", by_alias=True),
                "task": request.question,
            }
        ),
        stable_schema_research_model_identity(profile.model),
        limits.input_tokens_per_call,
        limits.output_tokens_per_call,
        config=runtime.verified_research_policy,
        ledger=runtime.budget_ledger,
    )
    owner = "crashed-owner"
    claim = runtime.budget_ledger.claim_model_execution(
        reservation,
        owner,
        now_ns=1,
    )
    assert claim.acquired is True
    runtime.budget_ledger.record_model_started(
        _model_started(reservation, "crashed-invocation", claim.generation, 2),
        owner_token=owner,
    )
    calls = {"propose": 0, "reenter": 0}

    async def forbidden_proposal(*_args):
        calls["propose"] += 1
        raise AssertionError("restart must not call the solver model")

    async def forbidden_reentry(*_args, **_kwargs):
        calls["reenter"] += 1
        raise AssertionError("restart must not replay targeted re-entry")

    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden_proposal,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=forbidden_reentry,
        )
    )

    after = store.load(runtime.run_id, runtime.run_incarnation)
    records = runtime.budget_ledger.load_model_records(
        runtime.run_id,
        runtime.run_incarnation,
    )
    assert output["sql"] == ""
    assert calls == {"propose": 0, "reenter": 0}
    assert after is not None and after.terminal is not None
    assert after.cursor.next_action_revision == admitted.cursor.next_action_revision + 1
    assert runtime.verified_solver_terminal.reason_code == "RESEARCH_PROTOCOL_FAILURE"
    assert records[-1].started is not None
    assert records[-1].result is not None
    assert records[-1].reconciliation is not None


def test_restart_does_not_settle_nonmatching_started_model_call(tmp_path) -> None:
    runtime, store, admitted, research, _, _ = _durable_admitted_reentry_runtime(
        tmp_path
    )
    limits = runtime.verified_research_policy.model_budget
    assert limits is not None
    reservation = reserve_model_call_budget(
        research.run_id,
        research.run_incarnation,
        _model_call_id(research, 0),
        canonical_digest({"not": "the targeted request"}),
        stable_schema_research_model_identity("different-model"),
        limits.input_tokens_per_call,
        limits.output_tokens_per_call,
        config=runtime.verified_research_policy,
        ledger=runtime.budget_ledger,
    )
    owner = "foreign-owner"
    claim = runtime.budget_ledger.claim_model_execution(
        reservation,
        owner,
        now_ns=1,
    )
    assert claim.acquired is True
    runtime.budget_ledger.record_model_started(
        _model_started(reservation, "foreign-invocation", claim.generation, 2),
        owner_token=owner,
    )
    before = runtime.budget_ledger.load_model_records(
        runtime.run_id,
        runtime.run_incarnation,
    )[-1]

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("restart must not invoke callbacks")

    asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=forbidden,
        )
    )

    after = runtime.budget_ledger.load_model_records(
        runtime.run_id,
        runtime.run_incarnation,
    )[-1]
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert after == before
    assert checkpoint is not None and checkpoint.terminal is not None
    assert (
        checkpoint.cursor.next_action_revision
        == admitted.cursor.next_action_revision + 1
    )
    assert runtime.verified_solver_terminal.reason_code == "RESEARCH_PROTOCOL_FAILURE"


def test_restart_leaves_unprepared_probe_reservation_as_durable_unknown(
    tmp_path,
) -> None:
    runtime, store, admitted, research, _, _ = _durable_admitted_reentry_runtime(
        tmp_path
    )
    target = TableRef(namespace="main", schema=None, table="orders")
    digest = canonical_action_digest(
        kind=ResearchActionKind.INSPECT_TABLE,
        hypothesis_id=None,
        target=target,
        parameters=(),
        expected_revision=research.revision,
    )
    action = ResearchAction(
        action_id="unprepared-action",
        kind=ResearchActionKind.INSPECT_TABLE,
        hypothesis_id=None,
        target=target,
        parameters=(),
        action_digest=digest,
        expected_revision=research.revision,
    )
    maximum = EvidenceCost(
        wall_clock_ms=1,
        model_calls=0,
        model_tokens=0,
        db_probe_ms=1,
        rows=1,
        bytes=1,
    )
    reservation = reserve_probe_budget(
        research,
        action,
        maximum,
        config=runtime.verified_research_policy,
        ledger=runtime.budget_ledger,
    )
    assert runtime.budget_ledger.claim_execution(
        reservation,
        "crashed-owner",
        now_ns=1,
    )
    before = runtime.budget_ledger.load_records(
        runtime.run_id,
        runtime.run_incarnation,
    )[-1]

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("restart must not invoke callbacks")

    asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=forbidden,
        )
    )

    after = runtime.budget_ledger.load_records(
        runtime.run_id,
        runtime.run_incarnation,
    )[-1]
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert after == before
    assert after.result is None and after.reconciliation is None
    assert checkpoint is not None and checkpoint.terminal is not None
    assert (
        checkpoint.cursor.next_action_revision
        == admitted.cursor.next_action_revision + 1
    )
    assert runtime.verified_solver_terminal.reason_code == "RESEARCH_PROTOCOL_FAILURE"


def test_restart_fails_closed_for_reconciled_probe_without_prepared_plan(
    tmp_path,
) -> None:
    runtime, store, _, research, _, _ = _durable_admitted_reentry_runtime(tmp_path)
    target = TableRef(namespace="main", schema=None, table="orders")
    digest = canonical_action_digest(
        kind=ResearchActionKind.INSPECT_TABLE,
        hypothesis_id=None,
        target=target,
        parameters=(),
        expected_revision=research.revision,
    )
    action = ResearchAction(
        action_id="unprepared-action",
        kind=ResearchActionKind.INSPECT_TABLE,
        hypothesis_id=None,
        target=target,
        parameters=(),
        action_digest=digest,
        expected_revision=research.revision,
    )
    maximum = EvidenceCost(
        wall_clock_ms=1,
        model_calls=0,
        model_tokens=0,
        db_probe_ms=1,
        rows=0,
        bytes=2,
    )
    reservation = reserve_probe_budget(
        research,
        action,
        maximum,
        config=runtime.verified_research_policy,
        ledger=runtime.budget_ledger,
    )
    owner = "crashed-owner"
    assert runtime.budget_ledger.claim_execution(reservation, owner, now_ns=1)
    now = datetime.now(UTC)
    result = build_probe_result(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        invocation_id="unprepared-invocation",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=action.target,
        started_at=now,
        completed_at=now,
        summary="durable result without prepared plan",
        cost=maximum,
        row_count=0,
        payload={},
    )
    stored = runtime.budget_ledger.record_result(
        reservation,
        result,
        owner_token=owner,
    )
    runtime.budget_ledger.record_reconciliation(
        reconcile_probe_cost(reservation, stored),
        stored,
    )
    before_state = runtime.research_state_store.load_latest_research_state(
        runtime.run_id,
        runtime.run_incarnation,
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("restart must not invoke callbacks")

    asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=forbidden,
        )
    )

    after_state = runtime.research_state_store.load_latest_research_state(
        runtime.run_id,
        runtime.run_incarnation,
    )
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert after_state == before_state == research
    assert checkpoint is not None and checkpoint.terminal is not None
    assert runtime.verified_solver_terminal.reason_code == "RESEARCH_PROTOCOL_FAILURE"


@pytest.mark.parametrize(
    "crash_window",
    (
        "before_research_save",
        "after_research_save",
        "tampered_plan",
        "mismatched_replay",
    ),
)
def test_restart_recovers_prepared_successor_and_same_reentry_record(
    monkeypatch,
    tmp_path,
    crash_window,
) -> None:
    from custom_tools.text_to_sql.adaptive.research_decision import (
        InspectTableIntent,
        ResearchDecisionV1,
        ToolIntent,
    )
    from custom_tools.text_to_sql.adaptive.tool_registry import InspectTableArguments

    runtime, _, research, _, _ = _one_remaining_reentry_case(tmp_path)
    runtime.verified_research_state = research
    store = AdaptiveSolverCheckpointStore(tmp_path / "adaptive.sqlite")
    runtime.solver_checkpoint_store = store
    calls = {"proposal": 0, "provider": 0}

    async def propose(_state, _requirements):
        calls["proposal"] += 1
        return SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id="status",
                question="Refresh status column evidence",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="One exact schema observation is required",
            ),
        )

    async def provider(_prompt: str) -> str:
        calls["provider"] += 1
        return ResearchDecisionV1.model_validate(
            {
                "proposals": (),
                "next": ToolIntent(
                    hypothesis_ref=None,
                    intent=InspectTableIntent(
                        arguments=InspectTableArguments(table="orders")
                    ),
                ),
            }
        ).model_dump_json()

    boundary = build_production_reentry_boundary(
        runtime,
        table_namespace="main",
        model=provider,
    )
    original_commit = store.commit_non_execution
    original_prepared_commit = (
        runtime.research_state_store.commit_prepared_targeted_reentry
    )

    def crash_before_solver_finalization(
        before,
        after,
        *,
        action_revision,
        action,
        replay_input=None,
    ):
        if action.get("kind") == "research_reentry_finalized":
            raise SystemExit("simulated hard crash")
        return original_commit(
            before,
            after,
            action_revision=action_revision,
            action=action,
            replay_input=replay_input,
        )

    def crash_before_research_save(_plan, _successor):
        raise SystemExit("simulated hard crash")

    if crash_window in {
        "before_research_save",
        "tampered_plan",
        "mismatched_replay",
    }:
        monkeypatch.setattr(
            runtime.research_state_store,
            "commit_prepared_targeted_reentry",
            crash_before_research_save,
        )
    else:
        monkeypatch.setattr(
            store,
            "commit_non_execution",
            crash_before_solver_finalization,
        )
    with pytest.raises(SystemExit, match="simulated hard crash"):
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=propose,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
                reenter=boundary,
                id_factory=iter(
                    ("request-1", "proposal-action-1", "reentry-1")
                ).__next__,
            )
        )

    crashed = store.load(runtime.run_id, runtime.run_incarnation)
    latest = runtime.research_state_store.load_latest_research_state(
        runtime.run_id,
        runtime.run_incarnation,
    )
    assert crashed is not None and crashed.terminal is None
    assert len(crashed.state.research_reentries) == 1
    crashed_record = crashed.state.research_reentries[0]
    assert crashed_record.status is ResearchReentryStatus.ADMITTED
    assert crashed_record.research_reentry_id == "reentry-1"
    assert crashed_record.missing_evidence_request_id == "request-1"
    assert crashed_record.source_id == "status"
    assert crashed_record.ordinal == 1
    crashed_action_revision = crashed.cursor.next_action_revision
    crashed_state_revision = crashed.state.revision
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        SolverReentryAdmissionReplayInput,
    )

    assert isinstance(
        store.load_transition_replay_input(
            runtime.run_id,
            runtime.run_incarnation,
            crashed_action_revision - 1,
        ),
        SolverReentryAdmissionReplayInput,
    )
    assert latest is not None
    assert latest.revision == research.revision + (
        0
        if crash_window in {"before_research_save", "mismatched_replay"}
        else 1
        if crash_window == "after_research_save"
        else 0
    )
    plan = runtime.research_state_store.load_prepared_targeted_reentry_commit(
        runtime.run_id,
        runtime.run_incarnation,
        "reentry-1",
    )
    assert plan is not None
    assert runtime.research_state_store.is_prepared_targeted_reentry_committed(
        plan
    ) is (crash_window == "after_research_save")
    before_probe_records = runtime.budget_ledger.load_records(
        runtime.run_id,
        runtime.run_incarnation,
    )
    before_model_records = runtime.budget_ledger.load_model_records(
        runtime.run_id,
        runtime.run_incarnation,
    )
    if crash_window == "tampered_plan":
        import sqlite3

        with sqlite3.connect(runtime.research_state_store.db_path) as connection:
            connection.execute(
                "DROP TRIGGER adaptive_research_state_prepared_reentries_no_update"
            )
            connection.execute(
                """
                UPDATE adaptive_research_state_prepared_reentries
                SET payload = ?
                WHERE run_id = ? AND run_incarnation = ?
                  AND research_reentry_id = ?
                """,
                (b"{}", runtime.run_id, runtime.run_incarnation, "reentry-1"),
            )

    monkeypatch.setattr(store, "commit_non_execution", original_commit)
    monkeypatch.setattr(
        runtime.research_state_store,
        "commit_prepared_targeted_reentry",
        original_prepared_commit,
    )
    runtime.verified_research_state = latest
    runtime.deadline = DeadlineBudget.from_duration(30)
    if crash_window != "tampered_plan":
        runtime.mark_cancelled()
    replay_calls = {"proposal": 0, "reentry": 0, "id": 0}

    async def forbidden_proposal(*_args):
        replay_calls["proposal"] += 1
        raise AssertionError("recovery must not call the solver model")

    async def forbidden_reentry(*_args, **_kwargs):
        replay_calls["reentry"] += 1
        raise AssertionError("recovery must not replay research")

    def forbidden_id():
        replay_calls["id"] += 1
        raise AssertionError("recovery must not allocate new identities")

    asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden_proposal,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=forbidden_reentry,
            id_factory=forbidden_id,
        )
    )

    recovered = store.load(runtime.run_id, runtime.run_incarnation)
    assert calls == {"proposal": 1, "provider": 1}
    assert replay_calls == {"proposal": 0, "reentry": 0, "id": 0}
    assert recovered is not None and recovered.terminal is not None
    if crash_window == "tampered_plan":
        after_research = runtime.research_state_store.load_latest_research_state(
            runtime.run_id,
            runtime.run_incarnation,
        )
        assert after_research == latest == research
        assert (
            runtime.budget_ledger.load_records(
                runtime.run_id,
                runtime.run_incarnation,
            )
            == before_probe_records
        )
        assert (
            runtime.budget_ledger.load_model_records(
                runtime.run_id,
                runtime.run_incarnation,
            )
            == before_model_records
        )
        assert recovered.cursor.next_action_revision == crashed_action_revision + 1
        assert recovered.state.revision == crashed_state_revision + 1
        assert len(recovered.state.research_reentries) == 1
        assert recovered.state.research_reentries[0].status is (
            ResearchReentryStatus.PROTOCOL_FAILURE
        )
        assert runtime.verified_solver_terminal.reason_code == (
            "RESEARCH_PROTOCOL_FAILURE"
        )
        return
    assert recovered.cursor.next_action_revision == crashed_action_revision + 2
    assert recovered.state.revision == crashed_state_revision + 2
    assert len(recovered.state.research_reentries) == 1
    recovered_record = recovered.state.research_reentries[0]
    assert recovered_record.status is ResearchReentryStatus.COMPLETED
    assert recovered_record.research_reentry_id == crashed_record.research_reentry_id
    assert (
        recovered_record.missing_evidence_request_id
        == crashed_record.missing_evidence_request_id
    )
    assert recovered_record.source_id == crashed_record.source_id
    assert recovered_record.ordinal == crashed_record.ordinal
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        SolverReentryCompletedReplayInput,
    )

    assert isinstance(
        store.load_transition_replay_input(
            runtime.run_id,
            runtime.run_incarnation,
            crashed_action_revision,
        ),
        SolverReentryCompletedReplayInput,
    )
    latest = runtime.research_state_store.load_latest_research_state(
        runtime.run_id,
        runtime.run_incarnation,
    )
    assert latest is not None and latest.revision == research.revision + 1
    assert runtime.research_state_store.is_prepared_targeted_reentry_committed(plan)

    from workflow._text_to_sql_document_authority import (
        DocumentAuthorityError,
        live_solver_document_freshness_context,
    )

    assert live_solver_document_freshness_context(runtime, latest).document_sources == ()
    import sqlite3

    with sqlite3.connect(store.db_path) as connection:
        if crash_window == "before_research_save":
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_replay_inputs_no_delete"
            )
            connection.execute(
                """
                DELETE FROM adaptive_solver_checkpoint_replay_inputs
                WHERE run_id = ? AND run_incarnation = ?
                  AND action_revision = ?
                """,
                (
                    runtime.run_id,
                    runtime.run_incarnation,
                    crashed_action_revision,
                ),
            )
        elif crash_window == "mismatched_replay":
            completed_input = store.load_transition_replay_input(
                runtime.run_id,
                runtime.run_incarnation,
                crashed_action_revision,
            )
            assert isinstance(completed_input, SolverReentryCompletedReplayInput)
            replacement = serialize_replay_input(
                completed_input.model_copy(
                    update={"research_state_digest": "sha256:" + "0" * 64}
                )
            )
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_replay_inputs_no_update"
            )
            connection.execute(
                """
                UPDATE adaptive_solver_checkpoint_replay_inputs
                SET input_bytes = ?, input_digest = ?
                WHERE run_id = ? AND run_incarnation = ?
                  AND action_revision = ?
                """,
                (
                    replacement,
                    "sha256:" + hashlib.sha256(replacement).hexdigest(),
                    runtime.run_id,
                    runtime.run_incarnation,
                    crashed_action_revision,
                ),
            )
        else:
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_replay_inputs_no_update"
            )
            connection.execute(
                """
                UPDATE adaptive_solver_checkpoint_replay_inputs
                SET input_bytes = ?
                WHERE run_id = ? AND run_incarnation = ?
                  AND action_revision = ?
                """,
                (
                    b"{}",
                    runtime.run_id,
                    runtime.run_incarnation,
                    crashed_action_revision,
                ),
            )

    expected_error = (
        "solver re-entry replay input is invalid"
        if crash_window in {"before_research_save", "after_research_save"}
        else "completed re-entry replay input"
    )
    with pytest.raises(DocumentAuthorityError, match=expected_error):
        live_solver_document_freshness_context(runtime, latest)


def test_semantic_binding_repair_restart_resumes_research_after_durable_probe(
    monkeypatch,
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.adaptive.models import BindingStatus
    from custom_tools.text_to_sql.adaptive.research_decision import (
        ExecuteResearchProbeIntent,
        ResearchDecisionV1,
        ToolIntent,
    )
    from custom_tools.text_to_sql.adaptive.research_tool_contracts import (
        ExecuteResearchProbeArguments,
    )
    import custom_tools.text_to_sql.adaptive.data_probes as data_probes
    import workflow._text_to_sql_solver_reentry as production_reentry

    runtime, _, research, requirements, _ = _one_remaining_reentry_case(tmp_path)
    runtime.verified_research_state = research
    store = AdaptiveSolverCheckpointStore(tmp_path / "adaptive.sqlite")
    runtime.solver_checkpoint_store = store
    selected = tuple(
        binding
        for binding in requirements.selected_bindings
        if binding.source_id == "status"
    )
    assert len(selected) == 1
    calls = {"provider": 0, "continuation": 0}

    class SuccessfulQueryExecutor:
        def __init__(self, **_kwargs):
            pass

        def execute(self, request):
            assert request.sql_query == (
                "SELECT status, COUNT(*) AS row_count "
                "FROM orders GROUP BY status LIMIT 20"
            )
            return SimpleNamespace(
                success=True,
                outcome={},
                columns=["status", "row_count"],
                data=[["active", 1]],
            )

    monkeypatch.setattr(data_probes, "QueryExecutor", SuccessfulQueryExecutor)

    repair_proposal = SolverProposalV1(
        proposal_version=1,
        proposal=MissingEvidenceProposal(
            proposal_kind="missing_evidence",
            source_id="status",
            question="Find the physical column for the requested account attribute",
            required_evidence_kind=EvidenceSourceKind.SCHEMA,
            reason="The selected attribute is only a related proxy",
            repair_kind="semantic_binding_mismatch",
            repair_binding_id=selected[0].binding_id,
        ),
    )
    initial = SolverState(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        query_spec=research.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    with pytest.raises(SolverProtocolError, match="trusted result review"):
        apply_solver_proposal(
            initial,
            repair_proposal,
            base_revision=initial.revision,
            dsn=runtime.dsn,
            table_namespace="main",
            requirements=requirements,
            id_factory=iter(("forged-request", "forged-action")).__next__,
        )
    transition = apply_solver_proposal(
        initial,
        repair_proposal,
        base_revision=initial.revision,
        dsn=runtime.dsn,
        table_namespace="main",
        requirements=requirements,
        id_factory=iter(("request-1", "proposal-action-1")).__next__,
        trusted_semantic_repair=True,
    )
    store.initialize(initial)
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert checkpoint is not None
    store.commit_non_execution(
        initial,
        transition.state,
        action_revision=checkpoint.cursor.next_action_revision,
        action=transition.action.model_dump(mode="json"),
        replay_input=transition.replay_input,
    )

    async def forbidden_proposal(*_args):
        raise AssertionError("semantic repair must not call the solver model")

    async def provider(_prompt: str) -> str:
        calls["provider"] += 1
        return ResearchDecisionV1.model_validate(
            {
                "proposals": (),
                "next": ToolIntent(
                    hypothesis_ref=None,
                    intent=ExecuteResearchProbeIntent(
                        arguments=ExecuteResearchProbeArguments(
                            sql=(
                                "SELECT status, COUNT(*) AS row_count "
                                "FROM orders GROUP BY status LIMIT 20"
                            ),
                        )
                    ),
                ),
            }
        ).model_dump_json()

    async def crash_in_continuation(
        _runtime,
        _namespace,
        _model,
        _profile,
        state,
        request,
    ):
        calls["continuation"] += 1
        stale = tuple(
            binding
            for binding in state.bindings
            if binding.binding_id == request.repair_binding_id
            and binding.status is BindingStatus.STALE
        )
        assert len(stale) == 1
        raise SystemExit("simulated crash in continued research")

    monkeypatch.setattr(
        production_reentry,
        "_continue_production_research",
        crash_in_continuation,
    )
    boundary = build_production_reentry_boundary(
        runtime,
        table_namespace="main",
        model=provider,
    )

    with pytest.raises(SystemExit, match="simulated crash"):
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=forbidden_proposal,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
                reenter=boundary,
                id_factory=iter(("reentry-1",)).__next__,
            )
        )

    latest = runtime.research_state_store.load_latest_research_state(
        runtime.run_id,
        runtime.run_incarnation,
    )
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert latest is not None and latest.revision == research.revision + 1
    assert checkpoint is not None
    assert checkpoint.state.research_reentries[-1].status is ResearchReentryStatus.ADMITTED

    def forbidden_id():
        raise AssertionError("recovery must not allocate a new re-entry identity")

    runtime.verified_research_state = latest
    runtime.deadline = DeadlineBudget.from_duration(30)
    with pytest.raises(SystemExit, match="simulated crash"):
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=forbidden_proposal,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
                reenter=boundary,
                id_factory=forbidden_id,
            )
        )

    assert calls == {"provider": 1, "continuation": 2}


def _durable_candidate_runtime(tmp_path, passed_prefix: int):
    candidate, research, requirements, _ = _runtime()
    runtime, store = _generation_runtime(tmp_path)
    initial = SolverState(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        query_spec=research.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    proposal_transition = apply_solver_proposal(
        initial,
        SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT o.status FROM orders o WHERE o.status = 'active'",
            ),
        ),
        base_revision=initial.revision,
        dsn=runtime.dsn,
        table_namespace="main",
        requirements=requirements,
        id_factory=iter(("candidate-1", "plan-1", "action-1")).__next__,
    )
    assert proposal_transition.state == candidate
    store.initialize(initial)
    checkpoint = store.commit_non_execution(
        initial,
        candidate,
        action_revision=0,
        action=proposal_transition.action.model_dump(mode="json"),
        replay_input=proposal_transition.replay_input,
    )
    for kind in (
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )[:passed_prefix]:
        transition = append_solver_check_result(
            checkpoint.state,
            _check(candidate.sql_candidates[-1].candidate_id, kind),
            base_revision=checkpoint.state.revision,
        )
        checkpoint = store.commit_non_execution(
            checkpoint.state,
            transition.state,
            action_revision=checkpoint.cursor.next_action_revision,
            action={"kind": "solver_check", "check_kind": kind.value},
        )
    return runtime, store, checkpoint


@pytest.mark.parametrize("passed_prefix", range(5))
def test_resume_partial_pre_execution_prefix_before_calling_model(
    monkeypatch,
    tmp_path,
    passed_prefix,
) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator

    runtime, store, before = _durable_candidate_runtime(tmp_path, passed_prefix)
    proposal_calls = 0

    async def forbidden_proposal(*_args):
        nonlocal proposal_calls
        proposal_calls += 1
        raise AssertionError("durable candidate must resume before another model turn")

    def remaining_gates(state, candidate_id, *, commit_transition, **_kwargs):
        existing = tuple(
            check for check in state.check_results if check.candidate_id == candidate_id
        )
        for kind in (
            CheckKind.SAFETY,
            CheckKind.SCHEMA,
            CheckKind.SEMANTIC,
            CheckKind.EXPLAIN,
        )[len(existing) :]:
            transition = append_solver_check_result(
                state,
                _check(candidate_id, kind),
                base_revision=state.revision,
            )
            state = commit_transition(transition)
        return state

    monkeypatch.setattr(
        coordinator,
        "run_solver_candidate_pre_execution_gates",
        remaining_gates,
    )
    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden_proposal,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    after = store.load(runtime.run_id, runtime.run_incarnation)
    assert output["sql"] == before.state.sql_candidates[-1].sql
    assert proposal_calls == 0
    assert after is not None
    assert after.cursor.next_action_revision == 5
    assert len(after.state.check_results) == 4


def test_ready_legacy_candidate_is_rejected_before_runtime_resume(tmp_path) -> None:
    runtime, store, _ = _durable_candidate_runtime(tmp_path, 4)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "DROP TRIGGER adaptive_solver_checkpoint_replay_inputs_no_delete"
        )
        connection.execute(
            """
            DELETE FROM adaptive_solver_checkpoint_replay_inputs
            WHERE run_id = ? AND run_incarnation = ? AND action_revision = 0
            """,
            (runtime.run_id, runtime.run_incarnation),
        )
    proposal_calls = 0

    async def forbidden_proposal(*_args):
        nonlocal proposal_calls
        proposal_calls += 1
        raise AssertionError("incompatible checkpoint must not call the model")

    with pytest.raises(
        AdaptiveSolverCheckpointCorruptionError,
        match="requires replay input",
    ):
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=forbidden_proposal,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
            )
        )

    assert proposal_calls == 0
    assert runtime.verified_solver_candidate_id is None


@pytest.mark.parametrize("boundary", ("cancelled", "deadline"))
def test_ready_resume_checks_control_boundary_before_returning_sql(
    tmp_path,
    boundary,
) -> None:
    runtime, store, _ = _durable_candidate_runtime(tmp_path, 4)
    if boundary == "cancelled":
        runtime.mark_cancelled()
    else:
        runtime.deadline = DeadlineBudget(
            deadline_monotonic=0.0,
            deadline_at_ms=0,
            monotonic=lambda: 1.0,
        )
    proposal_calls = 0

    async def forbidden_proposal(*_args):
        nonlocal proposal_calls
        proposal_calls += 1
        raise AssertionError("control boundary must stop before the model")

    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden_proposal,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert output["sql"] == ""
    assert proposal_calls == 0
    assert checkpoint is not None and checkpoint.terminal is not None
    assert checkpoint.state.stop_reason is (
        SolverStopReason.CANCELLED
        if boundary == "cancelled"
        else SolverStopReason.DEADLINE_EXCEEDED
    )


def test_in_flight_proposal_deadline_seals_timed_out_and_replays(tmp_path) -> None:
    runtime, store = _generation_runtime(tmp_path)
    ticks = iter((0.0, 0.0, 1.0))
    runtime.deadline = DeadlineBudget(
        deadline_monotonic=0.5,
        deadline_at_ms=500,
        monotonic=ticks.__next__,
    )
    proposal_calls = 0

    async def propose(*_args):
        nonlocal proposal_calls
        proposal_calls += 1
        runtime.deadline.require_remaining("in-flight solver proposal")
        raise AssertionError("expired proposal must not return")

    first = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert first["sql"] == ""
    assert proposal_calls == 1
    assert checkpoint is not None and checkpoint.terminal is not None
    assert checkpoint.state.stop_reason is SolverStopReason.DEADLINE_EXCEEDED
    durable_terminal = json.loads(checkpoint.terminal.terminal_bytes)
    assert durable_terminal["status"] == "timed_out"
    assert durable_terminal["reason_code"] == "TIMED_OUT"
    assert durable_terminal["sql"] == ""

    replay = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    replayed = store.load(runtime.run_id, runtime.run_incarnation)
    assert replay == first
    assert proposal_calls == 1
    assert replayed is not None and replayed.terminal == checkpoint.terminal


def test_generation_commits_candidate_and_four_gates_then_replays(
    monkeypatch,
    tmp_path,
) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator

    runtime, store = _generation_runtime(tmp_path)
    calls = {"proposal": 0}

    async def propose(_state, _requirements):
        calls["proposal"] += 1
        return SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT o.status FROM orders o WHERE o.status = 'active'",
            ),
        )

    def gates(state, candidate_id, *, commit_transition, **_kwargs):
        for kind in (
            CheckKind.SAFETY,
            CheckKind.SCHEMA,
            CheckKind.SEMANTIC,
            CheckKind.EXPLAIN,
        ):
            transition = append_solver_check_result(
                state,
                _check(candidate_id, kind),
                base_revision=state.revision,
            )
            state = commit_transition(transition)
        return state

    monkeypatch.setattr(
        coordinator,
        "run_solver_candidate_pre_execution_gates",
        gates,
    )
    ids = iter(("candidate-1", "plan-1", "action-1"))
    first = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            id_factory=ids.__next__,
        )
    )

    assert first["sql"].startswith("SELECT o.status")
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert checkpoint is not None
    assert checkpoint.cursor.next_action_revision == 5
    assert len(checkpoint.state.check_results) == 4
    proposal_replay = store.load_transition_replay_input(
        runtime.run_id,
        runtime.run_incarnation,
        0,
    )
    assert type(proposal_replay) is SolverSqlProposalReplayInput
    persisted_reference = solver_document_freshness_reference(
        runtime,
        runtime.verified_research_state,
    )
    assert proposal_replay.requirements == validate_coverage_inputs(
        runtime.verified_research_state,
        persisted_reference,
        runtime.run_id,
        runtime.run_incarnation,
    )

    from custom_tools.text_to_sql.adaptive._policy_config import (
        load_adaptive_policy_config,
    )
    from custom_tools.text_to_sql.adaptive.result_review_runtime import (
        INVALID_RESULT_REVIEW_RUNTIME,
        build_result_review_runtime,
    )
    from custom_tools.text_to_sql.adaptive.result_validation_runtime import (
        INVALID_RESULT_VALIDATION_RUNTIME,
        build_result_validation_runtime,
    )
    import workflow._text_to_sql_document_authority as authority

    runtime.verified_research_outcome = SimpleNamespace(
        stop_reason=ResearchStopReason.COMPLETE
    )
    runtime.verified_research_policy = load_adaptive_policy_config()
    with monkeypatch.context() as final_runtime_patch:
        def live_revalidation_must_not_run(*_args):
            raise AssertionError("final is persisted")

        final_runtime_patch.setattr(
            authority,
            "live_solver_document_freshness_context",
            live_revalidation_must_not_run,
        )
        assert (
            build_result_validation_runtime(runtime, sql_query=first["sql"])
            is not INVALID_RESULT_VALIDATION_RUNTIME
        )
        assert (
            build_result_review_runtime(runtime, sql_query=first["sql"])
            is not INVALID_RESULT_REVIEW_RUNTIME
        )

    async def forbidden(*_args):
        raise AssertionError("ready checkpoint must not call the model")

    replay = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )
    assert replay == first
    assert calls == {"proposal": 1}


def test_generation_rejects_stale_live_authority_before_solver_proposal(
    monkeypatch,
    tmp_path,
) -> None:
    import workflow._text_to_sql_document_authority as authority

    runtime, _ = _generation_runtime(tmp_path)
    calls = {"proposal": 0}

    async def propose(*_args):
        calls["proposal"] += 1
        raise AssertionError("stale authority must stop before the proposal")

    def stale(*_args):
        raise DocumentAuthorityError("live authority is stale")

    monkeypatch.setattr(authority, "live_solver_document_freshness_context", stale)

    with pytest.raises(DocumentAuthorityError, match="live authority is stale"):
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=propose,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
            )
        )

    assert calls == {"proposal": 0}


def test_malformed_solver_proposal_is_protocol_failure(tmp_path) -> None:
    runtime, store = _generation_runtime(tmp_path)

    async def propose(*_args):
        return parse_solver_proposal(b"{")

    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert output == {
        "sql": "",
        "description": "Adaptive solver stopped: RESEARCH_PROTOCOL_FAILURE",
    }
    assert checkpoint is not None
    assert checkpoint.state.stop_reason is SolverStopReason.PROTOCOL_FAILURE


def test_unparseable_sql_proposal_is_returned_for_bounded_repair(
    monkeypatch,
    tmp_path,
) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator

    runtime, store = _generation_runtime(tmp_path)
    feedbacks = []

    class RepairedCandidateCommitted(BaseException):
        pass

    async def propose(_state, _requirements, feedback=None):
        feedbacks.append(feedback)
        sql = (
            "SELECT o.status FROM orders o WHERE o.status = 'active'"
            if feedback is not None
            else "SELECT 'Women's Soccer'"
        )
        return SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql=sql,
            ),
        )

    def stop_after_repaired_commit(*_args, **_kwargs):
        raise RepairedCandidateCommitted()

    monkeypatch.setattr(coordinator, "_run_pre_execution", stop_after_repaired_commit)

    with pytest.raises(RepairedCandidateCommitted):
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=propose,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
            )
        )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert feedbacks == [
        None,
        {
            "failure_code": "SQL_PARSE_REJECTED",
            "reason": "SQL parser rejected the prior candidate",
            "rejected_sql": "SELECT 'Women's Soccer'",
        },
    ]
    assert checkpoint is not None
    assert len(checkpoint.state.sql_candidates) == 1
    assert checkpoint.state.stop_reason is None


def test_sql_parser_timeout_remains_tool_failure(monkeypatch, tmp_path) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator

    runtime, store = _generation_runtime(tmp_path)
    proposal_calls = 0

    async def propose(*_args):
        nonlocal proposal_calls
        proposal_calls += 1
        return SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT o.status FROM orders o",
            ),
        )

    def parser_timeout(*_args, **_kwargs):
        raise SqlAstError(
            SqlAstErrorCode.PARSE_TIMEOUT,
            "SQL parsing exceeded its wall-time budget",
        )

    monkeypatch.setattr(coordinator, "apply_solver_proposal", parser_timeout)

    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert proposal_calls == 1
    assert output == {
        "sql": "",
        "description": "Adaptive solver stopped: RESEARCH_TOOL_FAILURE",
    }
    assert checkpoint is not None
    assert checkpoint.state.stop_reason is SolverStopReason.TOOL_FAILURE


def test_parse_retry_preserves_deterministic_result_shape_receipt(
    monkeypatch,
    tmp_path,
) -> None:
    import workflow.text_to_sql_adaptive_solver as coordinator

    (
        runtime,
        store,
        _,
        _,
        _,
        receipt,
        _,
        _,
    ) = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="an auxiliary output is not requested",
        deterministic_failure_code=CheckFailureCode.RESULT_SHAPE_MISMATCH,
    )
    feedbacks = []

    class RepairedCandidateCommitted(BaseException):
        pass

    async def propose(_state, _requirements, feedback):
        feedbacks.append(feedback)
        if len(feedbacks) == 1:
            assert feedback == receipt
            sql = "SELECT 'unclosed"
        else:
            assert feedback.repair_receipt == receipt
            assert feedback.sql_parse_feedback == {
                "failure_code": "SQL_PARSE_REJECTED",
                "reason": "SQL parser rejected the prior candidate",
                "rejected_sql": "SELECT 'unclosed",
            }
            sql = "SELECT o.status FROM orders o"
        return SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql=sql,
            ),
        )

    async def forbidden_resume(*_args, **_kwargs):
        raise AssertionError("deterministic SQL shape repair must call solver directly")

    def stop_after_repaired_commit(*_args, **_kwargs):
        raise RepairedCandidateCommitted()

    monkeypatch.setattr(coordinator, "_resume_open_generation", forbidden_resume)
    monkeypatch.setattr(coordinator, "_run_pre_execution", stop_after_repaired_commit)

    with pytest.raises(RepairedCandidateCommitted):
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=propose,
                safety_policy=object(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
            )
        )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert len(feedbacks) == 2
    assert checkpoint is not None
    assert len(checkpoint.state.sql_candidates) == 2
    assert checkpoint.state.stop_reason is None


def test_parse_retry_rejects_missing_evidence_during_result_shape_repair(
    tmp_path,
) -> None:
    (
        runtime,
        store,
        _,
        _,
        _,
        receipt,
        _,
        _,
    ) = _persisted_result_contradiction_checkpoint(
        tmp_path,
        result_review_reason="an auxiliary output is not requested",
        deterministic_failure_code=CheckFailureCode.RESULT_SHAPE_MISMATCH,
    )
    proposal_calls = 0
    reentry_calls = 0

    async def propose(_state, _requirements, _feedback):
        nonlocal proposal_calls
        proposal_calls += 1
        proposal = (
            SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT 'unclosed",
            )
            if proposal_calls == 1
            else MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id=receipt.source_id,
                question="Which additional evidence is needed?",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="Requesting evidence is invalid during direct SQL repair",
            )
        )
        return SolverProposalV1(proposal_version=1, proposal=proposal)

    async def forbidden_reenter(*_args, **_kwargs):
        nonlocal reentry_calls
        reentry_calls += 1
        raise AssertionError("deterministic SQL shape repair must not research")

    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=forbidden_reenter,
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert proposal_calls == 2
    assert reentry_calls == 0
    assert output == {
        "sql": "",
        "description": "Adaptive solver stopped: RESEARCH_PROTOCOL_FAILURE",
    }
    assert checkpoint is not None
    assert checkpoint.state.missing_evidence_requests == ()
    assert checkpoint.state.stop_reason is SolverStopReason.PROTOCOL_FAILURE


def test_solver_runtime_error_is_tool_failure(tmp_path) -> None:
    runtime, store = _generation_runtime(tmp_path)

    async def propose(*_args):
        raise RuntimeError("provider failed")

    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert output == {
        "sql": "",
        "description": "Adaptive solver stopped: RESEARCH_TOOL_FAILURE",
    }
    assert checkpoint is not None
    assert checkpoint.state.stop_reason is SolverStopReason.TOOL_FAILURE


def test_missing_evidence_without_reentry_seals_and_replays(tmp_path) -> None:
    runtime, store = _generation_runtime(tmp_path)

    async def propose(_state, _requirements):
        return SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id="status",
                question="Which status evidence is authoritative?",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="One targeted observation is required",
            ),
        )

    ids = iter(("request-1", "action-1"))
    first = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            id_factory=ids.__next__,
        )
    )

    assert first["sql"] == ""
    assert runtime.verified_solver_terminal.reason_code == (
        "SCHEMA_CLARIFICATION_REQUIRED"
    )
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert checkpoint is not None and checkpoint.terminal is not None

    async def forbidden(*_args):
        raise AssertionError("terminal checkpoint must not call the model")

    replay = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
        )
    )
    assert replay == first


def test_solver_normalizes_unique_one_character_source_id_typo() -> None:
    from workflow import text_to_sql_adaptive_solver as coordinator

    state, _, _, _ = _runtime()
    proposal = SolverProposalV1(
        proposal_version=1,
        proposal=MissingEvidenceProposal(
            proposal_kind="missing_evidence",
            source_id="statuX",
            question="Which status evidence is authoritative?",
            required_evidence_kind=EvidenceSourceKind.SCHEMA,
            reason="One targeted observation is required",
        ),
    )

    normalized = coordinator._normalize_solver_proposal_source_id(state, proposal)

    assert normalized.proposal.source_id == "status"


@pytest.mark.parametrize("source_id", ("statXX", "statusX"))
def test_solver_does_not_normalize_distant_source_id(source_id) -> None:
    from workflow import text_to_sql_adaptive_solver as coordinator

    state, _, _, _ = _runtime()
    proposal = SolverProposalV1(
        proposal_version=1,
        proposal=MissingEvidenceProposal(
            proposal_kind="missing_evidence",
            source_id=source_id,
            question="Which status evidence is authoritative?",
            required_evidence_kind=EvidenceSourceKind.SCHEMA,
            reason="One targeted observation is required",
        ),
    )

    normalized = coordinator._normalize_solver_proposal_source_id(state, proposal)

    assert normalized is proposal


def test_solver_does_not_normalize_ambiguous_source_id() -> None:
    from workflow import text_to_sql_adaptive_solver as coordinator

    state, _, _, _ = _runtime()
    semantic_item = state.query_spec.semantic_items[0]
    query_spec = state.query_spec.model_copy(
        update={
            "semantic_items": (
                semantic_item,
                semantic_item.model_copy(update={"source_id": "statuY"}),
            )
        }
    )
    state = state.model_copy(update={"query_spec": query_spec})
    proposal = SolverProposalV1(
        proposal_version=1,
        proposal=MissingEvidenceProposal(
            proposal_kind="missing_evidence",
            source_id="statuX",
            question="Which status evidence is authoritative?",
            required_evidence_kind=EvidenceSourceKind.SCHEMA,
            reason="One targeted observation is required",
        ),
    )

    normalized = coordinator._normalize_solver_proposal_source_id(state, proposal)

    assert normalized is proposal


@pytest.mark.parametrize(
    ("reentry_status", "terminal_status", "reason_code"),
    (
        (ResearchReentryStatus.CANCELLED, "cancelled", "CANCELLED"),
        (ResearchReentryStatus.DEADLINE_EXCEEDED, "timed_out", "TIMED_OUT"),
        (
            ResearchReentryStatus.BUDGET_EXHAUSTED,
            "abstained",
            "RESEARCH_BUDGET_EXHAUSTED",
        ),
        (ResearchReentryStatus.TOOL_FAILURE, "failed", "RESEARCH_TOOL_FAILURE"),
        (
            ResearchReentryStatus.PROTOCOL_FAILURE,
            "failed",
            "RESEARCH_PROTOCOL_FAILURE",
        ),
    ),
)
def test_resume_durable_missing_evidence_calls_reentry_before_model(
    tmp_path,
    reentry_status,
    terminal_status,
    reason_code,
) -> None:
    _, research, requirements, _ = _runtime()
    runtime, store = _generation_runtime(tmp_path)
    initial = SolverState(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        query_spec=research.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    proposal_transition = apply_solver_proposal(
        initial,
        SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id="status",
                question="Refresh status evidence",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="One targeted observation is required",
            ),
        ),
        base_revision=initial.revision,
        dsn=runtime.dsn,
        table_namespace="main",
        requirements=requirements,
        id_factory=iter(("request-1", "proposal-action-1")).__next__,
    )
    proposed = proposal_transition.state
    store.initialize(initial)
    store.commit_non_execution(
        initial,
        proposed,
        action_revision=0,
        action=proposal_transition.action.model_dump(mode="json"),
        replay_input=proposal_transition.replay_input,
    )
    calls = {"proposal": 0, "reentry": 0}

    async def forbidden_proposal(*_args):
        calls["proposal"] += 1
        raise AssertionError("durable missing evidence must not call the model")

    async def reenter(
        solver_state,
        research_state,
        request_id,
        *,
        commit_solver_admission,
        id_factory,
        **_kwargs,
    ):
        calls["reentry"] += 1
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
            reentry_status,
            base_revision=committed.revision,
        )
        return SimpleNamespace(
            solver_state=finalized.state,
            research_state=research_state,
            record=finalized.record,
        )

    output = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden_proposal,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=reenter,
            id_factory=iter(("reentry-1",)).__next__,
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert output["sql"] == ""
    assert calls == {"proposal": 0, "reentry": 1}
    assert checkpoint is not None and checkpoint.terminal is not None
    assert checkpoint.state.research_reentries[0].status is reentry_status
    assert runtime.verified_solver_terminal.status.value == terminal_status
    assert runtime.verified_solver_terminal.reason_code == reason_code


def test_reentry_admission_and_finalization_are_separate_durable_revisions(
    tmp_path,
) -> None:
    runtime, store = _generation_runtime(tmp_path)

    async def propose(_state, _requirements):
        return SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id="status",
                question="Refresh status evidence",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="One targeted observation is required",
            ),
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
        committed_state = commit_solver_admission(admitted)
        assert committed_state == admitted.state
        finalized = finalize_targeted_reentry(
            committed_state,
            admitted.record.research_reentry_id,
            ResearchReentryStatus.PROTOCOL_FAILURE,
            base_revision=committed_state.revision,
        )
        return SimpleNamespace(
            solver_state=finalized.state,
            research_state=research_state,
            record=finalized.record,
        )

    ids = iter(("request-1", "proposal-action-1", "reentry-1"))
    result = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=reenter,
            id_factory=ids.__next__,
        )
    )

    assert result["sql"] == ""
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert checkpoint is not None
    assert checkpoint.cursor.next_action_revision == 3
    assert len(checkpoint.state.research_reentries) == 1
    assert checkpoint.state.research_reentries[0].status is (
        ResearchReentryStatus.PROTOCOL_FAILURE
    )


def test_reentry_failure_recovers_admitted_record_and_replays_terminal(
    tmp_path,
    caplog,
) -> None:
    runtime, store = _generation_runtime(tmp_path)

    async def propose(_state, _requirements):
        return SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id="status",
                question="Refresh status evidence",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="One targeted observation is required",
            ),
        )

    calls = {"proposal": 0, "reentry": 0}

    async def counting_propose(state, requirements):
        calls["proposal"] += 1
        return await propose(state, requirements)

    async def failing_reentry(
        solver_state,
        research_state,
        request_id,
        *,
        commit_solver_admission,
        id_factory,
        **_kwargs,
    ):
        calls["reentry"] += 1
        admitted = admit_targeted_reentry(
            solver_state,
            research_state,
            request_id,
            base_revision=solver_state.revision,
            id_factory=id_factory,
        )
        assert commit_solver_admission(admitted) == admitted.state
        raise RuntimeError("external research boundary failed")

    ids = iter(("request-1", "proposal-action-1", "reentry-1"))
    first = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=counting_propose,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=failing_reentry,
            id_factory=ids.__next__,
        )
    )

    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    assert first["sql"] == ""
    assert checkpoint is not None and checkpoint.terminal is not None
    assert checkpoint.cursor.next_action_revision == 3
    assert len(checkpoint.state.research_reentries) == 1
    assert checkpoint.state.research_reentries[0].status is (
        ResearchReentryStatus.PROTOCOL_FAILURE
    )
    assert calls == {"proposal": 1, "reentry": 1}
    assert "external research boundary failed" in caplog.text

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("sealed re-entry failure must replay exactly")

    replay = asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=forbidden,
            safety_policy=object(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=forbidden,
        )
    )
    assert replay == first
