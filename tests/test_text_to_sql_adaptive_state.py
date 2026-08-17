"""Immutable reducer tests for adaptive Text-to-SQL research state."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    BudgetState,
    ColumnRef,
    EvidenceCost,
    EvidenceRecord,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExpectedResultShape,
    Hypothesis,
    HypothesisStatus,
    JoinCandidate,
    JoinCandidateStatus,
    JoinType,
    PhysicalColumnBinding,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    ResearchStopReason,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
from custom_tools.text_to_sql.adaptive.semantic_coverage import CoverageInputError, validate_coverage_inputs
from custom_tools.text_to_sql.adaptive.state import (
    ResearchTransitionConflictError,
    ResearchTransitionProtocolError,
    ResearchTransitionReferenceError,
    ResearchTransitionResult,
    ResearchTransitionRevisionError,
    ResearchTransitionValidationError,
    apply_research_transition,
)


RUN_ID = "run-1"
INCARNATION = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SCHEMA = "sha256:" + "a" * 64
NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _table() -> TableRef:
    return TableRef(namespace="main", schema=None, table="orders")


def _column() -> ColumnRef:
    return ColumnRef(table=_table(), column="status")


def _item(source_id: str = "source-1") -> SemanticItem:
    return SemanticItem(
        source_id=source_id,
        kind=SemanticItemKind.FILTER,
        source_text="orders",
        normalized_meaning="orders",
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )


def _budget() -> BudgetState:
    return BudgetState(
        initial_wall_clock_ms=10,
        used_wall_clock_ms=0,
        remaining_wall_clock_ms=10,
        initial_model_calls=1,
        used_model_calls=0,
        remaining_model_calls=1,
        initial_model_tokens=10,
        used_model_tokens=0,
        remaining_model_tokens=10,
        initial_db_probe_ms=10,
        used_db_probe_ms=0,
        remaining_db_probe_ms=10,
        initial_rows=10,
        used_rows=0,
        remaining_rows=10,
        initial_bytes=10,
        used_bytes=0,
        remaining_bytes=10,
    )


def _state(**changes) -> ResearchState:
    values = {
        "run_id": RUN_ID,
        "run_incarnation": INCARNATION,
        "revision": 0,
        "schema_namespace_version": SCHEMA,
        "query_spec": QuerySpec(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=None,
            query_id="query-1",
            original_text="orders",
            semantic_items=(_item(),),
            expected_result_shape=ExpectedResultShape.ROWS,
            global_constraints=(),
        ),
        "hypotheses": (),
        "evidence": (),
        "bindings": (),
        "join_candidates": (),
        "unresolved_items": ("source-1",),
        "action_history": (),
        "result_expectations": (),
        "budget_state": _budget(),
        "stop_reason": None,
    }
    values.update(changes)
    return ResearchState(**values)


def test_required_positive_integer_limit_is_resolved_without_binding() -> None:
    limit = SemanticItem(
        source_id="limit-1",
        kind=SemanticItemKind.LIMIT,
        source_text="highest",
        normalized_meaning="limit",
        required=True,
        operator=None,
        literal_or_reference=1,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=(),
    )
    state = _state(
        query_spec=QuerySpec(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=None,
            query_id="query-limit",
            original_text="highest",
            semantic_items=(limit,),
            expected_result_shape=ExpectedResultShape.ROWS,
            global_constraints=(),
        ),
        unresolved_items=(),
    )

    assert state.query_spec.semantic_items == (limit,)
    assert state.unresolved_items == ()


@pytest.mark.parametrize("literal", (0, -1, True, "1"))
def test_only_positive_integer_limit_is_resolved_without_binding(
    literal: object,
) -> None:
    with pytest.raises(ValidationError, match="requires binding_ids"):
        SemanticItem(
            source_id="limit-1",
            kind=SemanticItemKind.LIMIT,
            source_text="highest",
            normalized_meaning="limit",
            required=True,
            operator=None,
            literal_or_reference=literal,
            status=SemanticItemStatus.RESOLVED,
            binding_ids=(),
        )


def test_bound_positive_integer_limit_with_candidate_binding_stays_unresolved() -> None:
    binding = _binding(
        binding_id="binding-limit",
        source_id="limit-1",
        evidence_ids=("evidence-1",),
    )
    limit = SemanticItem(
        source_id="limit-1",
        kind=SemanticItemKind.LIMIT,
        source_text="highest",
        normalized_meaning="limit",
        required=True,
        operator=None,
        literal_or_reference=1,
        status=SemanticItemStatus.PARTIALLY_RESOLVED,
        binding_ids=(binding.binding_id,),
    )
    state = _state(
        query_spec=QuerySpec(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=None,
            query_id="query-limit",
            original_text="highest",
            semantic_items=(limit,),
            expected_result_shape=ExpectedResultShape.ROWS,
            global_constraints=(),
        ),
        bindings=(binding,),
        evidence=(_evidence(_action(), evidence_id="evidence-1"),),
        unresolved_items=("limit-1",),
    )

    assert state.unresolved_items == ("limit-1",)


def test_stale_positive_integer_limit_binding_stays_unresolved_and_is_rejected() -> None:
    binding = _binding(
        binding_id="binding-limit",
        source_id="limit-1",
        status=BindingStatus.STALE,
        evidence_ids=("evidence-1",),
    )
    limit = SemanticItem(
        source_id="limit-1",
        kind=SemanticItemKind.LIMIT,
        source_text="highest",
        normalized_meaning="limit",
        required=True,
        operator=None,
        literal_or_reference=1,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    state = _state(
        query_spec=QuerySpec(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=SCHEMA,
            query_id="query-limit-stale",
            original_text="highest",
            semantic_items=(limit,),
            expected_result_shape=ExpectedResultShape.ROWS,
            global_constraints=(),
        ),
        bindings=(binding,),
        evidence=(_evidence(_action(), evidence_id="evidence-1"),),
        unresolved_items=("limit-1",),
    )
    context = FreshnessContext(
        evaluated_at=NOW,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        schema_namespace_version=SCHEMA,
    )

    assert state.unresolved_items == ("limit-1",)
    with pytest.raises(CoverageInputError) as error:
        validate_coverage_inputs(state, context, RUN_ID, INCARNATION)
    assert error.value.affected_source_ids == ("limit-1",)


def _action(
    *,
    action_id: str = "action-1",
    revision: int = 0,
    hypothesis_id: str | None = None,
    detail: str = "full",
    parameters: tuple[tuple[str, str | int], ...] | None = None,
) -> ResearchAction:
    action_parameters = parameters if parameters is not None else (("detail", detail),)
    return ResearchAction(
        action_id=action_id,
        kind=ResearchActionKind.INSPECT_TABLE,
        hypothesis_id=hypothesis_id,
        target=_table(),
        parameters=action_parameters,
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_TABLE,
            hypothesis_id=hypothesis_id,
            target=_table(),
            parameters=action_parameters,
            expected_revision=revision,
        ),
        expected_revision=revision,
    )


def _evidence(
    action: ResearchAction,
    *,
    evidence_id: str = "evidence-1",
    revision: int = 1,
    schema: str = SCHEMA,
    run_id: str = RUN_ID,
    incarnation: str = INCARNATION,
) -> EvidenceRecord:
    return EvidenceRecord(
        run_id=run_id,
        run_incarnation=incarnation,
        revision=revision,
        schema_namespace_version=schema,
        evidence_id=evidence_id,
        source_kind=EvidenceSourceKind.SCHEMA,
        target=_table(),
        action_digest=action.action_digest,
        observation="orders exists",
        validity_scope=EvidenceValidityScope.SCHEMA_VERSION,
        data_snapshot_token=None,
        observed_at=NOW,
        strength=1.0,
        created_at=NOW,
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=0,
            bytes=0,
        ),
    )


def _hypothesis(
    *,
    status: HypothesisStatus = HypothesisStatus.TESTING,
    evidence_ids: tuple[str, ...] = (),
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id="hypothesis-1",
        source_ids=("source-1",),
        claim="orders maps to the requested data",
        candidate_targets=(_table(),),
        status=status,
        evidence_ids=evidence_ids,
    )


def _binding(
    *,
    binding_id: str = "binding-1",
    source_id: str = "source-1",
    status: BindingStatus = BindingStatus.CANDIDATE,
    evidence_ids: tuple[str, ...] = (),
) -> PhysicalColumnBinding:
    return PhysicalColumnBinding(
        binding_id=binding_id,
        source_id=source_id,
        tables=(_table(),),
        columns=(_column(),),
        predicates=(),
        join_path=(),
        evidence_ids=evidence_ids,
        confidence=1.0,
        status=status,
        validator_rule="schema-check",
        physical_column=_column(),
    )


def test_transition_is_immutable_append_only_and_deterministic() -> None:
    state = _state()
    action = _action()
    result = apply_research_transition(
        state,
        action,
        evidence=(
            _evidence(action, evidence_id="evidence-b"),
            _evidence(action, evidence_id="evidence-a"),
        ),
        hypotheses=(
            _hypothesis(status=HypothesisStatus.PROPOSED, evidence_ids=("evidence-a",)),
        ),
        bindings=(_binding(evidence_ids=("evidence-a",)),),
    )

    assert isinstance(result, ResearchTransitionResult)
    assert state.revision == 0
    assert result.state.revision == 1
    assert [item.evidence_id for item in result.state.evidence] == [
        "evidence-a",
        "evidence-b",
    ]
    assert result.state.unresolved_items == ("source-1",)
    assert result.novelty.is_novel is True
    assert result.novelty.added_evidence_ids == ("evidence-a", "evidence-b")
    assert result.novelty.added_hypothesis_ids == ("hypothesis-1",)
    assert result.novelty.added_binding_ids == ("binding-1",)

    resolved = apply_research_transition(
        result.state,
        _action(action_id="action-2", revision=1, detail="columns"),
        hypotheses=(
            _hypothesis(
                status=HypothesisStatus.SUPPORTED,
                evidence_ids=("evidence-a",),
            ),
        ),
        bindings=(
            _binding(
                status=BindingStatus.SUPPORTED,
                evidence_ids=("evidence-a",),
            ),
        ),
    )
    assert resolved.state.unresolved_items == ()
    assert resolved.novelty.updated_hypothesis_ids == ("hypothesis-1",)
    assert resolved.novelty.updated_binding_ids == ("binding-1",)


def test_transition_merges_join_candidates_atomically_with_the_action() -> None:
    action = _action()
    join = JoinCandidate(
        join_id="join-1",
        left=_column(),
        right=ColumnRef(table=_table(), column="customer_id"),
        join_type=JoinType.INNER,
        path=(),
        status=JoinCandidateStatus.CANDIDATE,
        evidence_ids=(),
    )

    result = apply_research_transition(_state(), action, join_candidates=(join,))

    assert result.state.revision == 1
    assert result.state.action_history == (action,)
    assert result.state.join_candidates == (join,)
    assert result.novelty.added_join_ids == ("join-1",)


def test_transition_rejects_stale_revision_and_duplicate_action_id_or_digest() -> None:
    action = _action()
    with pytest.raises(ResearchTransitionRevisionError, match="expected_revision"):
        apply_research_transition(_state(), _action(revision=1))

    first = apply_research_transition(_state(), action).state
    assert apply_research_transition(_state(), action).novelty.is_novel is False
    with pytest.raises(ResearchTransitionConflictError, match="duplicate"):
        apply_research_transition(first, _action(action_id="action-1", revision=1))


def test_new_action_id_and_revision_do_not_bypass_semantic_duplicate_check() -> None:
    first = apply_research_transition(_state(), _action()).state
    with pytest.raises(ResearchTransitionConflictError, match="duplicate"):
        apply_research_transition(
            first,
            _action(action_id="action-2", revision=1),
        )

    next_state = apply_research_transition(
        first,
        _action(action_id="action-3", revision=1, detail="columns"),
    ).state
    assert next_state.revision == 2


def test_transition_rejects_noncanonical_action_digest_and_model_copy_bypass() -> None:
    bad_action = _action().model_copy(update={"action_digest": "sha256:" + "b" * 64})
    with pytest.raises(ResearchTransitionValidationError, match="action_digest"):
        apply_research_transition(_state(), bad_action)

    bad_budget = _budget().model_copy(update={"remaining_rows": -1})
    bypassed = _state().model_copy(update={"budget_state": bad_budget})
    with pytest.raises(ResearchTransitionValidationError, match="research state"):
        apply_research_transition(bypassed, _action())
    mismatched_budget = _budget().model_copy(update={"remaining_rows": 9})
    bypassed = _state().model_copy(update={"budget_state": mismatched_budget})
    with pytest.raises(ResearchTransitionValidationError, match="research state"):
        apply_research_transition(bypassed, _action())


def test_transition_requires_evidence_to_match_run_schema_revision_and_action() -> None:
    action = _action()
    with pytest.raises(ResearchTransitionRevisionError, match="evidence revision"):
        apply_research_transition(
            _state(), action, evidence=(_evidence(action, revision=0),)
        )
    with pytest.raises(ResearchTransitionReferenceError, match="schema namespace"):
        apply_research_transition(
            _state(), action, evidence=(_evidence(action, schema="sha256:" + "b" * 64),)
        )
    with pytest.raises(ResearchTransitionReferenceError, match="run incarnation"):
        apply_research_transition(
            _state(),
            action,
            evidence=(_evidence(action, run_id="run-2"),),
        )
    with pytest.raises(ResearchTransitionReferenceError, match="run incarnation"):
        apply_research_transition(
            _state(),
            action,
            evidence=(_evidence(action, incarnation="other-incarnation"),),
        )
    other = _action(action_id="action-2", detail="columns")
    with pytest.raises(ResearchTransitionReferenceError, match="transition action"):
        apply_research_transition(_state(), action, evidence=(_evidence(other),))


def test_transition_validates_references_and_status_updates() -> None:
    action = _action(hypothesis_id="hypothesis-1")
    with pytest.raises(ResearchTransitionReferenceError, match="hypothesis_id"):
        apply_research_transition(_state(), action)
    with pytest.raises(ResearchTransitionReferenceError, match="source_ids"):
        apply_research_transition(
            _state(),
            _action(),
            hypotheses=(
                _hypothesis(status=HypothesisStatus.PROPOSED).model_copy(
                    update={"source_ids": ("missing",)}
                ),
            ),
        )
    with pytest.raises(ResearchTransitionReferenceError, match="evidence_ids"):
        apply_research_transition(
            _state(),
            _action(),
            bindings=(_binding(evidence_ids=("missing-evidence",)),),
        )

    first_action = _action()
    first = apply_research_transition(
        _state(),
        first_action,
        evidence=(_evidence(first_action),),
        hypotheses=(_hypothesis(status=HypothesisStatus.PROPOSED),),
        bindings=(_binding(evidence_ids=("evidence-1",)),),
    ).state
    second_action = _action(action_id="action-2", revision=1, detail="columns")
    supported = apply_research_transition(
        first,
        second_action,
        hypotheses=(
            _hypothesis(
                status=HypothesisStatus.SUPPORTED,
                evidence_ids=("evidence-1",),
            ),
        ),
        bindings=(
            _binding(
                status=BindingStatus.SUPPORTED,
                evidence_ids=("evidence-1",),
            ),
        ),
    ).state
    assert supported.hypotheses[0].status is HypothesisStatus.SUPPORTED
    with pytest.raises(ResearchTransitionConflictError, match="immutable"):
        apply_research_transition(
            first,
            second_action,
            bindings=(_binding().model_copy(update={"confidence": 0.5}),),
        )
    third_action = _action(action_id="action-3", revision=1, detail="rows")
    with pytest.raises(ResearchTransitionConflictError, match="evidence_ids"):
        apply_research_transition(
            first,
            third_action,
            bindings=(_binding(),),
        )
    with pytest.raises(ResearchTransitionProtocolError, match="hypothesis status"):
        apply_research_transition(
            supported,
            _action(action_id="action-4", revision=2, detail="status"),
            hypotheses=(
                _hypothesis(
                    status=HypothesisStatus.TESTING,
                    evidence_ids=("evidence-1",),
                ),
            ),
        )


def test_transition_rejects_new_noninitial_entities_and_evidence_reordering() -> None:
    action = _action()
    with pytest.raises(ResearchTransitionProtocolError, match="new hypothesis"):
        apply_research_transition(_state(), action, hypotheses=(_hypothesis(),))
    with pytest.raises(ResearchTransitionProtocolError, match="new binding"):
        apply_research_transition(
            _state(),
            action,
            evidence=(_evidence(action),),
            bindings=(
                _binding(
                    status=BindingStatus.SUPPORTED,
                    evidence_ids=("evidence-1",),
                ),
            ),
        )

    first = apply_research_transition(
        _state(),
        action,
        evidence=(
            _evidence(action, evidence_id="evidence-a"),
            _evidence(action, evidence_id="evidence-b"),
        ),
        bindings=(_binding(evidence_ids=("evidence-a", "evidence-b")),),
    ).state
    with pytest.raises(ResearchTransitionConflictError, match="evidence_ids"):
        apply_research_transition(
            first,
            _action(action_id="action-2", revision=1, detail="reorder"),
            bindings=(_binding(evidence_ids=("evidence-b", "evidence-a")),),
        )


def test_transition_rejects_duplicate_evidence_and_bypassed_history_gaps() -> None:
    action = _action()
    with pytest.raises(ResearchTransitionConflictError, match="duplicate evidence"):
        apply_research_transition(
            _state(),
            action,
            evidence=(_evidence(action), _evidence(action)),
        )

    first = apply_research_transition(
        _state(), action, evidence=(_evidence(action),)
    ).state
    gap_action = _action(action_id="action-3", revision=2, detail="gap")
    gapped = first.model_copy(
        update={"revision": 3, "action_history": (first.action_history[0], gap_action)}
    )
    with pytest.raises(ResearchTransitionValidationError, match="strict contract"):
        apply_research_transition(
            gapped, _action(action_id="action-4", revision=3, detail="next")
        )

    bad_evidence = first.model_copy(
        update={"evidence": (_evidence(action, revision=2),)}
    )
    with pytest.raises(ResearchTransitionRevisionError, match="producer action"):
        apply_research_transition(
            bad_evidence,
            _action(action_id="action-5", revision=1, detail="bad-evidence"),
        )


def test_semantic_action_digest_ignores_parameter_order() -> None:
    left = _action(parameters=(("z", 2), ("a", 1)))
    right = _action(action_id="action-2", parameters=(("a", 1), ("z", 2)))
    assert left.action_digest == right.action_digest


def test_transition_derives_unresolved_items_and_stops_future_actions() -> None:
    first_action = _action()
    first = apply_research_transition(
        _state(),
        first_action,
        evidence=(_evidence(first_action),),
        bindings=(_binding(evidence_ids=("evidence-1",)),),
    ).state
    completed = apply_research_transition(
        first,
        _action(action_id="action-2", revision=1, detail="complete"),
        bindings=(
            _binding(
                status=BindingStatus.SUPPORTED,
                evidence_ids=("evidence-1",),
            ),
        ),
        stop_reason=ResearchStopReason.COMPLETE,
    )
    assert completed.state.unresolved_items == ()
    assert completed.novelty.stop_reason is ResearchStopReason.COMPLETE
    assert completed.novelty.is_novel is True
    with pytest.raises(ResearchTransitionProtocolError, match="stopped"):
        apply_research_transition(
            completed.state, _action(action_id="action-3", revision=2)
        )


def test_transition_atomically_derives_query_spec_resolution_from_bindings() -> None:
    first_action = _action()
    candidate = apply_research_transition(
        _state(),
        first_action,
        evidence=(_evidence(first_action),),
        bindings=(_binding(evidence_ids=("evidence-1",)),),
    ).state

    candidate_item = candidate.query_spec.semantic_items[0]
    assert candidate_item.status is SemanticItemStatus.PARTIALLY_RESOLVED
    assert candidate_item.binding_ids == ("binding-1",)
    assert candidate.query_spec.revision == candidate.revision == 1
    assert candidate.unresolved_items == ("source-1",)

    resolved = apply_research_transition(
        candidate,
        _action(action_id="action-2", revision=1, detail="support"),
        bindings=(
            _binding(
                status=BindingStatus.SUPPORTED,
                evidence_ids=("evidence-1",),
            ),
        ),
    ).state

    resolved_item = resolved.query_spec.semantic_items[0]
    assert resolved_item.status is SemanticItemStatus.RESOLVED
    assert resolved_item.binding_ids == ("binding-1",)
    assert resolved.query_spec.revision == resolved.revision == 2
    assert resolved.unresolved_items == ()


def test_transition_rolls_back_stale_and_resolves_all_supported_bindings() -> None:
    first_action = _action()
    candidates = apply_research_transition(
        _state(),
        first_action,
        evidence=(
            _evidence(first_action, evidence_id="evidence-a"),
            _evidence(first_action, evidence_id="evidence-b"),
        ),
        bindings=(
            _binding(binding_id="binding-b", evidence_ids=("evidence-b",)),
            _binding(binding_id="binding-a", evidence_ids=("evidence-a",)),
        ),
    ).state
    assert candidates.query_spec.semantic_items[0].binding_ids == (
        "binding-a",
        "binding-b",
    )

    ambiguous = apply_research_transition(
        candidates,
        _action(action_id="action-2", revision=1, detail="support"),
        bindings=(
            _binding(
                binding_id="binding-b",
                status=BindingStatus.SUPPORTED,
                evidence_ids=("evidence-b",),
            ),
            _binding(
                binding_id="binding-a",
                status=BindingStatus.SUPPORTED,
                evidence_ids=("evidence-a",),
            ),
        ),
    ).state
    resolved_item = ambiguous.query_spec.semantic_items[0]
    assert resolved_item.status is SemanticItemStatus.RESOLVED
    assert resolved_item.binding_ids == ("binding-a", "binding-b")
    assert ambiguous.unresolved_items == ()

    single = apply_research_transition(
        _state(),
        first_action,
        evidence=(_evidence(first_action),),
        bindings=(_binding(evidence_ids=("evidence-1",)),),
    ).state
    stale = apply_research_transition(
        single,
        _action(action_id="action-2", revision=1, detail="stale"),
        bindings=(
            _binding(
                status=BindingStatus.STALE,
                evidence_ids=("evidence-1",),
            ),
        ),
    ).state
    stale_item = stale.query_spec.semantic_items[0]
    assert stale_item.status is SemanticItemStatus.UNRESOLVED
    assert stale_item.binding_ids == ()
    assert stale.unresolved_items == ("source-1",)


@pytest.mark.parametrize(
    ("status", "binding_ids"),
    (
        (SemanticItemStatus.UNRESOLVED, ()),
        (SemanticItemStatus.PARTIALLY_RESOLVED, ()),
        (SemanticItemStatus.RESOLVED, ("binding-1",)),
        (SemanticItemStatus.AMBIGUOUS, ("binding-1",)),
        (SemanticItemStatus.UNSUPPORTED, ()),
    ),
)
def test_research_state_rejects_each_forged_semantic_resolution_status(
    status: SemanticItemStatus,
    binding_ids: tuple[str, ...],
) -> None:
    action = _action()
    candidate = apply_research_transition(
        _state(),
        action,
        evidence=(_evidence(action),),
        bindings=(_binding(evidence_ids=("evidence-1",)),),
    ).state
    payload = candidate.model_dump(mode="python", round_trip=True)
    payload["query_spec"]["semantic_items"][0].update(
        {"status": status, "binding_ids": binding_ids}
    )

    with pytest.raises(ValueError, match="binding resolution"):
        ResearchState.model_validate(payload)


@pytest.mark.parametrize(
    ("revision", "actions"),
    (
        (1, ()),
        (2, (_action(),)),
        (2, (_action(), _action(action_id="action-2", revision=2, detail="gap"))),
        (1, (_action(revision=1),)),
    ),
)
def test_research_state_rejects_missing_extra_or_gapped_action_history(
    revision: int,
    actions: tuple[ResearchAction, ...],
) -> None:
    payload = _state().model_dump(mode="python", round_trip=True)
    payload.update({"revision": revision, "action_history": actions})

    with pytest.raises(ValueError, match="every revision from zero"):
        ResearchState.model_validate(payload)


def test_semantic_commit_is_one_targetless_action_history_revision() -> None:
    action = ResearchAction(
        action_id="semantic-action-1",
        kind=ResearchActionKind.SEMANTIC_COMMIT,
        hypothesis_id=None,
        target=None,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.SEMANTIC_COMMIT,
            hypothesis_id=None,
            target=None,
            parameters=(),
            expected_revision=0,
        ),
        expected_revision=0,
    )

    successor = apply_research_transition(_state(), action).state

    assert successor.revision == 1
    assert successor.action_history == (action,)


def test_sequential_semantic_commits_have_distinct_canonical_digests() -> None:
    first = canonical_action_digest(
        kind=ResearchActionKind.SEMANTIC_COMMIT,
        hypothesis_id=None,
        target=None,
        parameters=(),
        expected_revision=0,
    )
    second = canonical_action_digest(
        kind=ResearchActionKind.SEMANTIC_COMMIT,
        hypothesis_id=None,
        target=None,
        parameters=(),
        expected_revision=1,
    )

    assert first != second
