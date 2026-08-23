from __future__ import annotations

import json

import pytest

from custom_tools.text_to_sql.adaptive.models import (
    BudgetState,
    EvidenceSourceKind,
    ExpectedResultShape,
    MissingEvidenceRequest,
    QuerySpec,
    ResearchState,
    ResearchStopReason,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    SolverAction,
    SolverActionKind,
    SolverState,
    SolverStopReason,
)
from custom_tools.text_to_sql.adaptive.ambiguity import AmbiguityReport
from custom_tools.text_to_sql.adaptive.research_loop import ResearchLoopOutcome
from workflow.text_to_sql_early_stop_evidence import (
    build_text_to_sql_early_stop_evidence,
    build_text_to_sql_stagnation_evidence,
)


_SCHEMA = "sha256:" + "1" * 64


def _query_spec(
    *kinds: SemanticItemKind,
    required: bool = True,
) -> QuerySpec:
    source_texts = tuple(f"private-{kind.value}" for kind in kinds)
    original_text = " ".join(source_texts)
    items = []
    for index, (kind, source_text) in enumerate(zip(kinds, source_texts), start=1):
        items.append(
            SemanticItem(
                source_id=f"source-{index}",
                kind=kind,
                source_text=source_text,
                normalized_meaning=source_text,
                required=required,
                operator=None,
                literal_or_reference=None,
                status=SemanticItemStatus.UNRESOLVED,
                binding_ids=(),
            )
        )
    return QuerySpec(
        run_id="run-1",
        run_incarnation="incarnation-1",
        revision=0,
        schema_namespace_version=_SCHEMA,
        query_id="query-1",
        original_text=original_text,
        semantic_items=tuple(items),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )


def _budget_state() -> BudgetState:
    return BudgetState(
        initial_wall_clock_ms=0,
        used_wall_clock_ms=0,
        remaining_wall_clock_ms=0,
        initial_model_calls=0,
        used_model_calls=0,
        remaining_model_calls=0,
        initial_model_tokens=0,
        used_model_tokens=0,
        remaining_model_tokens=0,
        initial_db_probe_ms=0,
        used_db_probe_ms=0,
        remaining_db_probe_ms=0,
        initial_rows=0,
        used_rows=0,
        remaining_rows=0,
        initial_bytes=0,
        used_bytes=0,
        remaining_bytes=0,
    )


def _research_outcome(
    query_spec: QuerySpec,
    *,
    stop_reason: ResearchStopReason,
    affected_source_ids: tuple[str, ...] = ("source-1",),
) -> ResearchLoopOutcome:
    state = ResearchState(
        run_id=query_spec.run_id,
        run_incarnation=query_spec.run_incarnation,
        revision=0,
        schema_namespace_version=query_spec.schema_namespace_version,
        query_spec=query_spec,
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=tuple(
            item.source_id for item in query_spec.semantic_items if item.required
        ),
        action_history=(),
        budget_state=_budget_state(),
        result_expectations=(),
        stop_reason=stop_reason,
    )
    return ResearchLoopOutcome(
        final_state=state,
        stop_reason=stop_reason,
        affected_source_ids=affected_source_ids,
        citation_evidence_ids=("evidence-1",)
        if stop_reason is ResearchStopReason.AMBIGUOUS
        else (),
        ambiguity=(
            AmbiguityReport(
                interpretations=("First reading.", "Second reading."),
                citation_evidence_ids=("evidence-1",),
                missing_distinguishing_fact="The definition is absent.",
            )
            if stop_reason is ResearchStopReason.AMBIGUOUS
            else None
        ),
    )


def _solver_state(
    query_spec: QuerySpec,
    *,
    source_id: str = "source-1",
) -> SolverState:
    request = MissingEvidenceRequest(
        run_id=query_spec.run_id,
        run_incarnation=query_spec.run_incarnation,
        revision=1,
        schema_namespace_version=query_spec.schema_namespace_version,
        missing_evidence_request_id="request-1",
        source_id=source_id,
        question="private diagnostic question",
        candidate_targets=(),
        required_evidence_kind=EvidenceSourceKind.SCHEMA,
        reason="private missing evidence reason",
    )
    action = SolverAction(
        action_id="action-1",
        kind=SolverActionKind.MISSING_EVIDENCE,
        base_revision=0,
        candidate_id=None,
        missing_evidence_request_id=request.missing_evidence_request_id,
    )
    return SolverState(
        run_id=query_spec.run_id,
        run_incarnation=query_spec.run_incarnation,
        revision=1,
        schema_namespace_version=query_spec.schema_namespace_version,
        query_spec=query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(request,),
        action_history=(action,),
        selected_candidate_id=None,
        stop_reason=SolverStopReason.MISSING_EVIDENCE,
    )


@pytest.mark.parametrize(
    ("kind", "expected_requirement"),
    [
        (SemanticItemKind.METRIC, "required_metric"),
        (SemanticItemKind.DIMENSION, "required_dimension"),
        (SemanticItemKind.FILTER, "required_filter"),
        (SemanticItemKind.ORDERING, "required_ordering"),
        (SemanticItemKind.LIMIT, "required_limit"),
        (SemanticItemKind.TIME, "required_time"),
        (SemanticItemKind.FORMULA, "required_formula"),
    ],
)
def test_research_evidence_preserves_typed_requirement(
    kind: SemanticItemKind,
    expected_requirement: str,
) -> None:
    outcome = _research_outcome(
        _query_spec(kind),
        stop_reason=ResearchStopReason.AMBIGUOUS,
    )

    receipt = build_text_to_sql_early_stop_evidence(
        research_outcome=outcome,
        solver_state=None,
        terminal_status="abstained",
        terminal_reason_code="RESEARCH_AMBIGUOUS",
    )

    assert receipt is not None
    assert receipt["violated_typed_requirement"] == expected_requirement


@pytest.mark.parametrize(
    ("stop_reason", "reason_code", "root_mechanism", "error_class"),
    [
        (
            ResearchStopReason.AMBIGUOUS,
            "RESEARCH_AMBIGUOUS",
            "ambiguous",
            "ambiguous_requirement",
        ),
        (
            ResearchStopReason.UNSUPPORTED,
            "RESEARCH_UNSUPPORTED",
            "unsupported",
            "unsupported_requirement",
        ),
    ],
)
def test_research_root_mechanisms_stay_distinct(
    stop_reason: ResearchStopReason,
    reason_code: str,
    root_mechanism: str,
    error_class: str,
) -> None:
    receipt = build_text_to_sql_early_stop_evidence(
        research_outcome=_research_outcome(
            _query_spec(SemanticItemKind.FILTER),
            stop_reason=stop_reason,
        ),
        solver_state=None,
        terminal_status="abstained",
        terminal_reason_code=reason_code,
    )

    assert receipt is not None
    assert receipt["root_mechanism"] == root_mechanism
    assert receipt["error_class"] == error_class
    assert receipt["terminal_source"] == "research"


def test_solver_missing_evidence_has_its_own_authority() -> None:
    receipt = build_text_to_sql_early_stop_evidence(
        research_outcome=None,
        solver_state=_solver_state(_query_spec(SemanticItemKind.FORMULA)),
        terminal_status="abstained",
        terminal_reason_code="SCHEMA_CLARIFICATION_REQUIRED",
    )

    assert receipt is not None
    assert receipt["terminal_source"] == "solver"
    assert receipt["root_mechanism"] == "missing_evidence"
    assert receipt["error_class"] == "missing_evidence"
    assert receipt["violated_typed_requirement"] == "required_formula"
    assert receipt["pipeline_component"] == "adaptive_sql_solver"


def test_multiple_affected_sources_of_one_kind_share_one_requirement() -> None:
    receipt = build_text_to_sql_early_stop_evidence(
        research_outcome=_research_outcome(
            _query_spec(SemanticItemKind.FILTER, SemanticItemKind.FILTER),
            stop_reason=ResearchStopReason.AMBIGUOUS,
            affected_source_ids=("source-1", "source-2"),
        ),
        solver_state=None,
        terminal_status="abstained",
        terminal_reason_code="RESEARCH_AMBIGUOUS",
    )

    assert receipt is not None
    assert receipt["violated_typed_requirement"] == "required_filter"


@pytest.mark.parametrize(
    "case",
    ["empty", "mixed", "unknown", "optional", "mismatch"],
)
def test_unprovable_evidence_is_unavailable(case: str) -> None:
    query_spec = _query_spec(
        SemanticItemKind.FILTER,
        SemanticItemKind.FORMULA,
        required=case != "optional",
    )
    affected_source_ids = {
        "empty": (),
        "mixed": ("source-1", "source-2"),
        "unknown": ("unknown-source",),
    }.get(case, ("source-1",))
    outcome = _research_outcome(
        query_spec,
        stop_reason=ResearchStopReason.AMBIGUOUS,
        affected_source_ids=affected_source_ids,
    )

    receipt = build_text_to_sql_early_stop_evidence(
        research_outcome=outcome,
        solver_state=None,
        terminal_status="abstained",
        terminal_reason_code=(
            "RESEARCH_UNSUPPORTED" if case == "mismatch" else "RESEARCH_AMBIGUOUS"
        ),
    )

    assert receipt is None


def test_receipt_contains_no_source_text_literals_sql_or_source_ids() -> None:
    query_spec = _query_spec(SemanticItemKind.FILTER)
    outcome = _research_outcome(
        query_spec,
        stop_reason=ResearchStopReason.UNSUPPORTED,
    )

    receipt = build_text_to_sql_early_stop_evidence(
        research_outcome=outcome,
        solver_state=None,
        terminal_status="abstained",
        terminal_reason_code="RESEARCH_UNSUPPORTED",
    )

    assert receipt is not None
    encoded = json.dumps(receipt, sort_keys=True)
    assert set(receipt) == {
        "schema_version",
        "record_kind",
        "terminal_source",
        "root_mechanism",
        "error_class",
        "violated_typed_requirement",
        "pipeline_component",
        "state_sha256",
    }
    assert query_spec.original_text not in encoded
    assert "source-1" not in encoded
    assert "request-1" not in encoded
    assert "SELECT" not in encoded


def test_stagnation_receipt_has_only_sorted_unique_rejection_signatures() -> None:
    outcome = _research_outcome(
        _query_spec(SemanticItemKind.FILTER),
        stop_reason=ResearchStopReason.STAGNATED,
    )
    outcome = ResearchLoopOutcome(
        final_state=outcome.final_state,
        stop_reason=outcome.stop_reason,
        affected_source_ids=outcome.affected_source_ids,
        citation_evidence_ids=outcome.citation_evidence_ids,
        ambiguity=outcome.ambiguity,
        rejection_signatures=(
            ("invalid_stop", "INVALID_STOP"),
            ("research_query_admission", "research_query_limit"),
        ),
    )

    receipt = build_text_to_sql_stagnation_evidence(
        research_outcome=outcome,
        terminal_status="abstained",
        terminal_reason_code="RESEARCH_STAGNATED",
    )

    assert receipt == {
        "schema_version": 1,
        "record_kind": "text2sql_research_stagnation_evidence",
        "terminal_source": "research",
        "terminal_reason_code": "RESEARCH_STAGNATED",
        "rejection_signatures": [
            ["invalid_stop", "INVALID_STOP"],
            ["research_query_admission", "research_query_limit"],
        ],
        "state_sha256": receipt["state_sha256"],
    }
