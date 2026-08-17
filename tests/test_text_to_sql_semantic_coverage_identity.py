"""Identity, readiness, and freshness checks for W5-00."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_tools.text_to_sql.adaptive.freshness import (
    DataSnapshotStatus,
    DataSnapshotValidation,
    DocumentSourceAvailability,
    DocumentSourceState,
    FreshnessContext,
)
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    EvidenceRecord,
    JoinCandidateStatus,
    JoinEdge,
    JoinType,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    ResearchActionKind,
    ResearchStopReason,
    SemanticItemStatus,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageInputError,
    CoverageInputErrorCode,
    _validate_coverage_inputs,
    validate_coverage_inputs,
)
from tests.text_to_sql_semantic_coverage_helpers import (
    INCARNATION,
    NOW,
    OTHER_SCHEMA,
    RUN_ID,
    _assert_error,
    _column,
    _context,
    _document_binding,
    _document_evidence,
    _join_candidate,
    _physical_binding,
    _schema_evidence,
    _state,
    _table,
    _validate,
)


@pytest.mark.parametrize(
    ("context", "run_id", "incarnation"),
    [
        (_context(), "other-run", INCARNATION),
        (_context(), RUN_ID, "other-incarnation"),
        (_context(run_id="other-run"), RUN_ID, INCARNATION),
        (_context(incarnation="other-incarnation"), RUN_ID, INCARNATION),
    ],
)
def test_run_and_incarnation_must_match_exactly(
    context: FreshnessContext,
    run_id: str,
    incarnation: str,
) -> None:
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
        ("source-a",),
        lambda: validate_coverage_inputs(_state(), context, run_id, incarnation),
    )


def test_state_context_and_query_schema_must_match_exactly() -> None:
    _assert_error(
        CoverageInputErrorCode.SCHEMA_NAMESPACE_MISMATCH,
        ("source-a",),
        lambda: _validate(_state(), _context(schema=OTHER_SCHEMA)),
    )
    state = _state()
    mismatched_query = state.query_spec.model_copy(
        update={"schema_namespace_version": OTHER_SCHEMA}
    )
    mismatched = state.model_copy(update={"query_spec": mismatched_query})
    _assert_error(
        CoverageInputErrorCode.SCHEMA_NAMESPACE_MISMATCH,
        ("source-a",),
        lambda: _validate(mismatched),
    )


def test_forged_models_are_strictly_revalidated() -> None:
    state = _state()
    forged_query = state.query_spec.model_copy(update={"run_id": "other-run"})
    forged_state = state.model_copy(update={"query_spec": forged_query})
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
        ("source-a",),
        lambda: _validate(forged_state),
    )
    forged_context = _context().model_copy(
        update={"evaluated_at": datetime(2026, 7, 31, 12, 0)}
    )
    _assert_error(
        CoverageInputErrorCode.STALE_BINDING_EVIDENCE,
        ("source-a",),
        lambda: _validate(state, forged_context),
    )


@pytest.mark.parametrize(
    "corruption",
    (
        "state-query",
        "query-items-string",
        "query-items-list",
        "state-binding",
        "binding-field",
        "state-evidence",
        "evidence-field",
        "state-join",
        "join-field",
        "state-object-setattr",
        "semantic-object-setattr",
        "binding-object-setattr",
        "evidence-object-setattr",
        "join-object-setattr",
    ),
)
def test_any_corrupted_nested_input_fails_with_closed_error(
    corruption: str,
) -> None:
    state = _state()
    if corruption == "state-query":
        forged = state.model_copy(update={"query_spec": "not-a-query"})
    elif corruption == "query-items-string":
        query = state.query_spec.model_copy(update={"semantic_items": ("bad",)})
        forged = state.model_copy(update={"query_spec": query})
    elif corruption == "query-items-list":
        query = state.query_spec.model_copy(
            update={"semantic_items": list(state.query_spec.semantic_items)}
        )
        forged = state.model_copy(update={"query_spec": query})
    elif corruption == "state-binding":
        forged = state.model_copy(update={"bindings": ("bad",)})
    elif corruption == "binding-field":
        binding = state.bindings[0].model_copy(update={"tables": ("bad",)})
        forged = state.model_copy(update={"bindings": (binding,)})
    elif corruption == "state-evidence":
        forged = state.model_copy(update={"evidence": ("bad",)})
    elif corruption == "evidence-field":
        evidence = state.evidence[0].model_copy(update={"target": "bad"})
        forged = state.model_copy(update={"evidence": (evidence,)})
    elif corruption == "state-join":
        forged = state.model_copy(update={"join_candidates": ("bad",)})
    elif corruption == "join-field":
        edge = JoinEdge(
            left=_column("orders", "customer_id"),
            right=_column("customers", "id"),
            join_type=JoinType.INNER,
        )
        join = _join_candidate(
            "join-corrupted",
            (edge,),
            "unused",
            status=JoinCandidateStatus.CANDIDATE,
        ).model_copy(update={"path": ("bad",)})
        forged = state.model_copy(update={"join_candidates": (join,)})
    elif corruption == "state-object-setattr":
        forged = state.model_copy(deep=True)
        object.__setattr__(forged, "query_spec", "not-a-query")
    elif corruption == "semantic-object-setattr":
        item = state.query_spec.semantic_items[0].model_copy(deep=True)
        object.__setattr__(item, "binding_ids", "bad")
        query = state.query_spec.model_copy(update={"semantic_items": (item,)})
        forged = state.model_copy(update={"query_spec": query})
    elif corruption == "binding-object-setattr":
        binding = state.bindings[0].model_copy(deep=True)
        object.__setattr__(binding, "columns", ("bad",))
        forged = state.model_copy(update={"bindings": (binding,)})
    elif corruption == "evidence-object-setattr":
        evidence = state.evidence[0].model_copy(deep=True)
        object.__setattr__(evidence, "target", "bad")
        forged = state.model_copy(update={"evidence": (evidence,)})
    else:
        edge = JoinEdge(
            left=_column("orders", "customer_id"),
            right=_column("customers", "id"),
            join_type=JoinType.INNER,
        )
        join = _join_candidate(
            "join-corrupted",
            (edge,),
            "unused",
            status=JoinCandidateStatus.CANDIDATE,
        ).model_copy(deep=True)
        object.__setattr__(join, "path", ("bad",))
        forged = state.model_copy(update={"join_candidates": (join,)})

    with pytest.raises(CoverageInputError):
        _validate(forged)


@pytest.mark.parametrize(
    "update",
    (
        {"run_id": 7},
        {"run_incarnation": 7},
        {"revision": "future"},
        {"schema_namespace_version": 7},
        {"action_digest": 7},
        {"observation": '{"observation_version":1,"provenance":'},
        {"action_digest": "sha256:" + "f" * 64},
    ),
    ids=(
        "run-id-type",
        "incarnation-type",
        "revision-type",
        "schema-type",
        "action-digest-type",
        "malformed-modern-provenance",
        "mismatching-modern-provenance",
    ),
)
def test_malformed_evidence_identity_is_never_downgraded_to_stale(
    update: dict[str, object],
) -> None:
    state = _state()
    forged_evidence = state.evidence[0].model_copy(update=update)
    forged_state = state.model_copy(update={"evidence": (forged_evidence,)})
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
        ("source-a",),
        lambda: _validate(forged_state),
    )


def test_object_setattr_evidence_identity_corruption_is_closed() -> None:
    state = _state()
    forged_evidence = state.evidence[0].model_copy(deep=True)
    object.__setattr__(forged_evidence, "run_id", 7)
    forged_state = state.model_copy(update={"evidence": (forged_evidence,)})
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
        ("source-a",),
        lambda: _validate(forged_state),
    )


def test_malformed_evidence_id_is_identity_error_without_guessed_source() -> None:
    state = _state()
    forged_evidence = state.evidence[0].model_copy(update={"evidence_id": 7})
    forged_state = state.model_copy(update={"evidence": (forged_evidence,)})
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
        (),
        lambda: _validate(forged_state),
    )


def test_malformed_state_query_context_and_caller_identities_are_closed() -> None:
    state = _state()
    forged_state = state.model_copy(update={"run_id": 7})
    forged_query = state.query_spec.model_copy(update={"run_id": 7})
    state_with_forged_query = state.model_copy(update={"query_spec": forged_query})
    forged_context = _context().model_copy(update={"run_incarnation": 7})
    callbacks = (
        lambda: _validate(forged_state),
        lambda: _validate(state_with_forged_query),
        lambda: _validate(state, forged_context),
        lambda: validate_coverage_inputs(
            state,
            _context(),
            7,  # type: ignore[arg-type]
            INCARNATION,
        ),
    )
    for callback in callbacks:
        _assert_error(
            CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
            ("source-a",),
            callback,
        )


@pytest.mark.parametrize(
    ("status", "binding_ids", "unresolved", "stop_reason"),
    [
        (SemanticItemStatus.UNRESOLVED, (), ("source-a",), None),
        (SemanticItemStatus.PARTIALLY_RESOLVED, (), (), None),
        (SemanticItemStatus.AMBIGUOUS, (), (), None),
        (SemanticItemStatus.UNSUPPORTED, (), (), None),
        (
            SemanticItemStatus.UNRESOLVED,
            (),
            ("source-a",),
            ResearchStopReason.COMPLETE,
        ),
        (
            SemanticItemStatus.RESOLVED,
            ("binding-a",),
            ("source-a",),
            None,
        ),
    ],
)
def test_required_items_must_be_completely_resolved(
    status: SemanticItemStatus,
    binding_ids: tuple[str, ...],
    unresolved: tuple[str, ...],
    stop_reason: ResearchStopReason | None,
) -> None:
    state = _state(
        item_specs=(("source-a", True, status, binding_ids),),
        unresolved_items=unresolved,
        stop_reason=stop_reason,
    )
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE,
        ("source-a",),
        lambda: _validate(state),
    )


def test_forged_resolved_item_without_binding_is_rejected() -> None:
    state = _state()
    forged_item = state.query_spec.semantic_items[0].model_copy(
        update={"binding_ids": ()}
    )
    forged_query = state.query_spec.model_copy(
        update={"semantic_items": (forged_item,)}
    )
    forged_state = state.model_copy(update={"query_spec": forged_query})
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE,
        ("source-a",),
        lambda: _validate(forged_state),
    )


@pytest.mark.parametrize(
    "status",
    (BindingStatus.CANDIDATE, BindingStatus.REJECTED),
)
def test_required_binding_must_be_supported(status: BindingStatus) -> None:
    state = _state(
        bindings=(
            _physical_binding(
                "source-a",
                "binding-a",
                "evidence-a",
                status=status,
            ),
        )
    )
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE,
        ("source-a",),
        lambda: _validate(state),
    )


def test_stale_binding_status_is_not_ready() -> None:
    state = _state(
        bindings=(
            _physical_binding(
                "source-a",
                "binding-a",
                "evidence-a",
                status=BindingStatus.STALE,
            ),
        )
    )
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE,
        ("source-a",),
        lambda: _validate(state),
    )


def test_missing_and_cross_source_bindings_fail_closed() -> None:
    state = _state()
    missing_item = state.query_spec.semantic_items[0].model_copy(
        update={"binding_ids": ("missing-binding",)}
    )
    missing_query = state.query_spec.model_copy(
        update={"semantic_items": (missing_item,)}
    )
    missing_state = state.model_copy(update={"query_spec": missing_query})
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE,
        ("source-a",),
        lambda: _validate(missing_state),
    )

    evidence_b = _schema_evidence(
        "evidence-b",
        _column("table-source-b", "column-source-b"),
    )
    cross_source = _state(
        item_specs=(
            ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-b",)),
            ("source-b", False, SemanticItemStatus.UNRESOLVED, ()),
        ),
        bindings=(_physical_binding("source-b", "binding-b", "evidence-b"),),
        evidence=(evidence_b,),
        unresolved_items=("source-b",),
    )
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE,
        ("source-a",),
        lambda: _validate(cross_source),
    )


def test_multiple_supported_required_bindings_are_selected_as_one_union() -> None:
    alternative_column = _column("table-source-a", "column-source-a-alternative")
    evidence_b = _schema_evidence(
        "evidence-b",
        alternative_column,
    )
    alternative_binding = _physical_binding(
        "source-a", "binding-b", "evidence-b"
    ).model_copy(
        update={
            "tables": (alternative_column.table,),
            "columns": (alternative_column,),
            "physical_column": alternative_column,
        }
    )
    state = _state(
        item_specs=(
            (
                "source-a",
                True,
                SemanticItemStatus.RESOLVED,
                ("binding-a", "binding-b"),
            ),
        ),
        bindings=(
            _physical_binding("source-a", "binding-a", "evidence-a"),
            alternative_binding,
        ),
        evidence=(_state().evidence[0], evidence_b),
    )
    requirements = _validate(state)

    assert tuple(binding.binding_id for binding in requirements.selected_bindings) == (
        "binding-a",
        "binding-b",
    )
    assert alternative_column in requirements.allowed_columns


def test_selected_bindings_follow_canonical_source_and_binding_order() -> None:
    source_a = _physical_binding("source-a", "binding-z", "evidence-a")
    source_b = _physical_binding("source-b", "binding-a", "evidence-b").model_copy(
        update={
            "tables": source_a.tables,
            "columns": source_a.columns,
            "physical_column": source_a.physical_column,
        }
    )
    state = _state(
        item_specs=(
            ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-z",)),
            ("source-b", True, SemanticItemStatus.RESOLVED, ("binding-a",)),
        ),
        bindings=(source_b, source_a),
        evidence=(
            _schema_evidence("evidence-a", source_a.physical_column),
            _schema_evidence("evidence-b", source_a.physical_column),
        ),
    )

    requirements = _validate_coverage_inputs(state, _context(), RUN_ID, INCARNATION)

    assert tuple(binding.binding_id for binding in requirements.selected_bindings) == (
        "binding-z",
        "binding-a",
    )


@pytest.mark.parametrize("freshness", ("stale", "unavailable", "revalidate", "future"))
def test_supported_binding_requires_current_fresh_evidence(freshness: str) -> None:
    completed_at = NOW + timedelta(seconds=1) if freshness == "future" else NOW
    valid_until = NOW if freshness == "revalidate" else None
    evidence = _document_evidence(
        "document-evidence",
        completed_at=completed_at,
        valid_until=valid_until,
    )
    state = _state(
        bindings=(_document_binding("source-a", "binding-a", evidence.evidence_id),),
        evidence=(evidence,),
    )
    availability = (
        DocumentSourceAvailability.REMOVED
        if freshness == "stale"
        else DocumentSourceAvailability.AVAILABLE
    )
    documents = (
        ()
        if freshness == "unavailable"
        else (
            DocumentSourceState(
                document_id="coverage-document",
                availability=availability,
                source_version=None
                if availability is DocumentSourceAvailability.REMOVED
                else "v1",
            ),
        )
    )
    _assert_error(
        CoverageInputErrorCode.STALE_BINDING_EVIDENCE,
        ("source-a",),
        lambda: _validate(state, _context(documents=documents)),
    )


@pytest.mark.parametrize(
    "evidence",
    (
        _schema_evidence(
            "foreign-run",
            _column("table-source-a", "column-source-a"),
            run_id="other-run",
        ),
        _schema_evidence(
            "foreign-incarnation",
            _column("table-source-a", "column-source-a"),
            run_incarnation="other-incarnation",
        ),
        _schema_evidence(
            "future-revision",
            _column("table-source-a", "column-source-a"),
            revision=2,
        ),
        _schema_evidence(
            "foreign-provenance-run",
            _column("table-source-a", "column-source-a"),
            provenance_run_id="other-run",
        ),
        _schema_evidence(
            "foreign-provenance-incarnation",
            _column("table-source-a", "column-source-a"),
            provenance_run_incarnation="other-incarnation",
        ),
        _schema_evidence(
            "foreign-provenance-schema",
            _column("table-source-a", "column-source-a"),
            provenance_schema=OTHER_SCHEMA,
        ),
    ),
    ids=(
        "foreign-run",
        "foreign-incarnation",
        "future-revision",
        "foreign-provenance-run",
        "foreign-provenance-incarnation",
        "foreign-provenance-schema",
    ),
)
def test_binding_evidence_authority_is_checked_before_freshness(
    evidence: EvidenceRecord,
) -> None:
    state = _state(
        bindings=(_physical_binding("source-a", "binding-a", evidence.evidence_id),),
        evidence=(evidence,),
    )
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
        ("source-a",),
        lambda: _validate(state),
    )


def test_forged_binding_evidence_envelope_cannot_bypass_identity() -> None:
    evidence = _schema_evidence(
        "forged-envelope",
        _column("table-source-a", "column-source-a"),
    ).model_copy(update={"run_id": "other-run"})
    state = _state(
        bindings=(_physical_binding("source-a", "binding-a", evidence.evidence_id),),
        evidence=(evidence,),
    )
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
        ("source-a",),
        lambda: _validate(state),
    )


def test_forged_binding_evidence_schema_is_an_identity_mismatch() -> None:
    state = _state()
    forged_evidence = state.evidence[0].model_copy(
        update={"schema_namespace_version": OTHER_SCHEMA}
    )
    forged_state = state.model_copy(update={"evidence": (forged_evidence,)})
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
        ("source-a",),
        lambda: _validate(forged_state),
    )


def test_foreign_join_evidence_reports_every_affected_required_source() -> None:
    edge = JoinEdge(
        left=_column("orders", "customer_id"),
        right=_column("customers", "id"),
        join_type=JoinType.INNER,
    )
    binding_a_evidence = _schema_evidence(
        "binding-evidence-a",
        _column("table-source-a", "column-source-a"),
    )
    binding_b_evidence = _schema_evidence(
        "binding-evidence-b",
        _column("table-source-b", "column-source-b"),
    )
    join_evidence = _schema_evidence(
        "foreign-join-evidence",
        edge.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        run_id="other-run",
    )
    state = _state(
        item_specs=(
            ("source-b", True, SemanticItemStatus.RESOLVED, ("binding-b",)),
            ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-a",)),
        ),
        bindings=(
            _physical_binding(
                "source-b",
                "binding-b",
                binding_b_evidence.evidence_id,
                join_path=(edge,),
            ),
            _physical_binding(
                "source-a",
                "binding-a",
                binding_a_evidence.evidence_id,
                join_path=(edge,),
            ),
        ),
        evidence=(binding_b_evidence, join_evidence, binding_a_evidence),
        joins=(_join_candidate("shared-join", (edge,), join_evidence.evidence_id),),
    )
    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
        ("source-a", "source-b"),
        lambda: _validate(state),
    )


def test_freshness_and_requirements_digests_bind_context() -> None:
    documents = (
        DocumentSourceState(
            document_id="document-b",
            availability=DocumentSourceAvailability.AVAILABLE,
            source_version="v1",
        ),
        DocumentSourceState(
            document_id="document-a",
            availability=DocumentSourceAvailability.AVAILABLE,
            source_version="v1",
        ),
    )
    snapshots = (
        DataSnapshotValidation(token="snapshot-b", status=DataSnapshotStatus.VALID),
        DataSnapshotValidation(token="snapshot-a", status=DataSnapshotStatus.VALID),
    )
    state = _state()
    first = _validate(
        state,
        _context(documents=documents, snapshots=snapshots),
    )
    reordered = _validate(
        state,
        _context(
            documents=tuple(reversed(documents)),
            snapshots=tuple(reversed(snapshots)),
        ),
    )
    assert first == reordered
    assert first.freshness_digest == reordered.freshness_digest
    assert first.requirements_digest == reordered.requirements_digest

    changed_contexts = (
        _context(
            evaluated_at=NOW + timedelta(seconds=1),
            documents=documents,
            snapshots=snapshots,
        ),
        _context(
            documents=(
                documents[0].model_copy(update={"source_version": "v2"}),
                documents[1],
            ),
            snapshots=snapshots,
        ),
        _context(
            documents=(
                DocumentSourceState(
                    document_id="document-b",
                    availability=DocumentSourceAvailability.REMOVED,
                    source_version=None,
                ),
                documents[1],
            ),
            snapshots=snapshots,
        ),
        _context(
            documents=documents,
            snapshots=(
                snapshots[0].model_copy(update={"status": DataSnapshotStatus.INVALID}),
                snapshots[1],
            ),
        ),
        _context(
            documents=documents,
            snapshots=(
                snapshots[0].model_copy(update={"token": "snapshot-c"}),
                snapshots[1],
            ),
        ),
    )
    for context in changed_contexts:
        changed = _validate(state, context)
        assert changed.state_digest == first.state_digest
        assert changed.freshness_digest != first.freshness_digest
        assert changed.requirements_digest != first.requirements_digest


def test_optional_items_do_not_expand_footprint() -> None:
    base = _state()
    extra_evidence = _schema_evidence(
        "evidence-extra",
        _column("unselected-table", "unselected-column"),
        run_id="unselected-foreign-run",
    )
    extra_binding = PhysicalColumnBinding(
        binding_id="binding-extra",
        source_id="source-b",
        tables=(_table("unselected-table"),),
        columns=(_column("unselected-table", "unselected-column"),),
        predicates=(),
        join_path=(),
        evidence_ids=(extra_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=_column("unselected-table", "unselected-column"),
    )
    state = _state(
        item_specs=(
            ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-a",)),
            ("source-b", False, SemanticItemStatus.RESOLVED, ("binding-extra",)),
        ),
        bindings=(*base.bindings, extra_binding),
        evidence=(*base.evidence, extra_evidence),
        unresolved_items=(),
    )

    requirements = _validate(state)

    assert requirements.required_source_ids == ("source-a",)
    assert tuple(item.binding_id for item in requirements.selected_bindings) == (
        "binding-a",
    )
    assert requirements.eligible_evidence_ids == ("evidence-a",)
    assert _table("unselected-table") not in requirements.allowed_tables
    assert _column("unselected-table", "unselected-column") not in (
        requirements.allowed_columns
    )


def test_global_constraints_fail_until_they_have_source_identity() -> None:
    predicate = PredicateRef(
        left=_column("orders", "status"),
        operator=PredicateOperator.EQ,
        right="paid",
    )
    state = _state(global_constraints=(predicate,))
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        (),
        lambda: _validate(state),
    )
