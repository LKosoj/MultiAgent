"""Binding, join, and determinism checks for W5-00."""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    JoinCandidate,
    JoinCandidateStatus,
    JoinEdge,
    JoinType,
    PredicateOperator,
    PredicateRef,
    PhysicalColumnBinding,
    ResearchActionKind,
    ResearchState,
    SemanticItemKind,
    SemanticItemStatus,
)
from custom_tools.text_to_sql.adaptive.policy import (
    evaluate_research_generation_authority,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageInputErrorCode,
    CoverageRequirements,
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)
from tests.text_to_sql_semantic_coverage_helpers import (
    _action,
    _assert_error,
    _column,
    _context,
    _join_candidate,
    _physical_binding,
    _schema_evidence,
    _state,
    _table,
    _validate,
)


def _assert_forged_requirements_rejected(
    requirements: CoverageRequirements,
    **updates: object,
) -> None:
    payload = requirements.model_dump(
        mode="python",
        by_alias=True,
        exclude_none=False,
        round_trip=True,
        warnings="error",
    )
    payload.update(updates)
    payload_without_digest = {
        key: value for key, value in payload.items() if key != "requirements_digest"
    }
    payload["requirements_digest"] = canonical_digest(payload_without_digest)
    with pytest.raises(ValidationError):
        CoverageRequirements.model_validate(payload)


@pytest.mark.parametrize(
    "join_status",
    (None, JoinCandidateStatus.CANDIDATE, JoinCandidateStatus.REJECTED),
)
def test_required_join_must_have_an_exact_validated_candidate(
    join_status: JoinCandidateStatus | None,
) -> None:
    edge = JoinEdge(
        left=_column("orders", "customer_id"),
        right=_column("customers", "id"),
        join_type=JoinType.INNER,
    )
    base = _state()
    binding = _physical_binding(
        "source-a",
        "binding-a",
        "evidence-a",
        join_path=(edge,),
    )
    joins = (
        ()
        if join_status is None
        else (
            _join_candidate(
                "join-a",
                (edge,),
                "join-evidence",
                status=join_status,
            ),
        )
    )
    state = _state(bindings=(binding,), evidence=base.evidence, joins=joins)
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        ("source-a",),
        lambda: _validate(state),
    )


def test_validated_join_is_eligible_without_legacy_binding_paths() -> None:
    aggregate_column = _column("table-aggregate", "column-aggregate")
    lower_column = _column("table-lower-bound", "column-lower-bound")
    upper_column = _column("table-lower-bound", "column-upper-bound")
    edge = JoinEdge(
        left=_column("table-aggregate", "lower_bound_id"),
        right=_column("table-lower-bound", "id"),
        join_type=JoinType.INNER,
    )
    join_evidence = _schema_evidence(
        "aggregate-bounds-join",
        edge.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    state = _state(
        item_specs=(
            ("aggregate", True, SemanticItemStatus.RESOLVED, ("aggregate-binding",)),
            ("lower-bound", True, SemanticItemStatus.RESOLVED, ("lower-binding",)),
            ("upper-bound", True, SemanticItemStatus.RESOLVED, ("upper-binding",)),
        ),
        bindings=(
            _physical_binding("aggregate", "aggregate-binding", "aggregate-evidence"),
            _physical_binding("lower-bound", "lower-binding", "lower-evidence"),
            PhysicalColumnBinding(
                binding_id="upper-binding",
                source_id="upper-bound",
                tables=(upper_column.table,),
                columns=(upper_column,),
                predicates=(),
                join_path=(),
                evidence_ids=("upper-evidence",),
                confidence=1.0,
                status=BindingStatus.SUPPORTED,
                validator_rule="coverage",
                physical_column=upper_column,
            ),
        ),
        evidence=(
            _schema_evidence("aggregate-evidence", aggregate_column),
            _schema_evidence("lower-evidence", lower_column),
            _schema_evidence("upper-evidence", upper_column),
            join_evidence,
        ),
        joins=(
            _join_candidate(
                "aggregate-bounds",
                (edge,),
                join_evidence.evidence_id,
            ),
        ),
    )
    items = state.query_spec.semantic_items
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        items[0].model_copy(update={"kind": SemanticItemKind.METRIC}),
                        items[1].model_copy(update={"kind": SemanticItemKind.FORMULA}),
                        items[2].model_copy(update={"kind": SemanticItemKind.FORMULA}),
                    )
                }
            )
        }
    )

    authority = evaluate_research_generation_authority(
        state, _context(), state.run_id, state.run_incarnation
    )

    assert authority.allowed is True
    assert authority.requirements is not None
    assert tuple(join.join_id for join in authority.requirements.eligible_validated_joins) == (
        "aggregate-bounds",
    )


def test_unrelated_validated_join_cannot_satisfy_required_path() -> None:
    required = JoinEdge(
        left=_column("orders", "customer_id"),
        right=_column("customers", "id"),
        join_type=JoinType.INNER,
    )
    unrelated = JoinEdge(
        left=_column("orders", "seller_id"),
        right=_column("sellers", "id"),
        join_type=JoinType.INNER,
    )
    base = _state()
    join_evidence = _schema_evidence(
        "join-evidence",
        unrelated.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    state = _state(
        bindings=(
            _physical_binding(
                "source-a",
                "binding-a",
                "evidence-a",
                join_path=(required,),
            ),
        ),
        evidence=(*base.evidence, join_evidence),
        joins=(_join_candidate("join-unrelated", (unrelated,), "join-evidence"),),
    )
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        ("source-a",),
        lambda: _validate(state),
    )


def test_validated_join_also_requires_fresh_evidence() -> None:
    edge = JoinEdge(
        left=_column("orders", "customer_id"),
        right=_column("customers", "id"),
        join_type=JoinType.INNER,
    )
    base = _state()
    stale = _schema_evidence(
        "join-evidence",
        edge.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    ).model_copy(update={"observation": "legacy evidence"})
    state = _state(
        bindings=(
            _physical_binding(
                "source-a",
                "binding-a",
                "evidence-a",
                join_path=(edge,),
            ),
        ),
        evidence=(*base.evidence, stale),
        joins=(_join_candidate("join-a", (edge,), "join-evidence"),),
    )
    _assert_error(
        CoverageInputErrorCode.STALE_BINDING_EVIDENCE,
        ("source-a",),
        lambda: _validate(state),
    )


def test_inner_join_symmetry_is_allowed_but_composite_order_is_preserved() -> None:
    first = JoinEdge(
        left=_column("orders", "tenant_id"),
        right=_column("customers", "tenant_id"),
        join_type=JoinType.INNER,
    )
    second = JoinEdge(
        left=_column("orders", "customer_id"),
        right=_column("customers", "id"),
        join_type=JoinType.INNER,
    )
    symmetric = tuple(
        JoinEdge(
            left=edge.right,
            right=edge.left,
            join_type=JoinType.INNER,
        )
        for edge in (first, second)
    )
    base = _state()
    evidence = _schema_evidence(
        "join-evidence",
        first.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    binding = _physical_binding(
        "source-a",
        "binding-a",
        "evidence-a",
        join_path=(first, second),
    )
    ready = _state(
        bindings=(binding,),
        evidence=(*base.evidence, evidence),
        joins=(_join_candidate("join-a", symmetric, "join-evidence"),),
    )
    requirements = _validate(ready)
    assert tuple(join.join_id for join in requirements.eligible_validated_joins) == (
        "join-a",
    )
    assert requirements.allowed_join_paths == ((first, second),)

    reordered = _state(
        bindings=(binding,),
        evidence=(*base.evidence, evidence),
        joins=(
            _join_candidate(
                "join-a",
                tuple(reversed(symmetric)),
                "join-evidence",
            ),
        ),
    )
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        ("source-a",),
        lambda: _validate(reordered),
    )


def test_each_required_join_edge_can_be_covered_by_its_own_validated_candidate() -> (
    None
):
    first = JoinEdge(
        left=_column("orders", "customer_id"),
        right=_column("customers", "id"),
        join_type=JoinType.INNER,
    )
    second = JoinEdge(
        left=_column("customers", "region_id"),
        right=_column("regions", "id"),
        join_type=JoinType.INNER,
    )
    base = _state()
    evidence_one = _schema_evidence(
        "join-evidence-one",
        first.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    evidence_two = _schema_evidence(
        "join-evidence-two",
        second.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    state = _state(
        bindings=(
            _physical_binding(
                "source-a",
                "binding-a",
                "evidence-a",
                join_path=(first, second),
            ),
        ),
        evidence=(*base.evidence, evidence_one, evidence_two),
        joins=(
            _join_candidate("join-one", (first,), evidence_one.evidence_id),
            _join_candidate("join-two", (second,), evidence_two.evidence_id),
        ),
    )

    requirements = _validate(state)

    assert tuple(join.join_id for join in requirements.eligible_validated_joins) == (
        "join-one",
        "join-two",
    )


def test_left_join_direction_is_not_symmetric() -> None:
    edge = JoinEdge(
        left=_column("orders", "customer_id"),
        right=_column("customers", "id"),
        join_type=JoinType.LEFT,
    )
    reversed_edge = JoinEdge(
        left=edge.right,
        right=edge.left,
        join_type=JoinType.LEFT,
    )
    base = _state()
    evidence = _schema_evidence(
        "join-evidence",
        edge.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    binding = _physical_binding(
        "source-a",
        "binding-a",
        "evidence-a",
        join_path=(edge,),
    )
    ready = _state(
        bindings=(binding,),
        evidence=(*base.evidence, evidence),
        joins=(_join_candidate("join-a", (edge,), "join-evidence"),),
    )
    assert _validate(ready).eligible_validated_joins[0].join_id == "join-a"

    wrong_direction = _state(
        bindings=(binding,),
        evidence=(*base.evidence, evidence),
        joins=(
            _join_candidate(
                "join-a",
                (reversed_edge,),
                "join-evidence",
            ),
        ),
    )
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        ("source-a",),
        lambda: _validate(wrong_direction),
    )


def test_requirements_and_digest_are_stable_under_state_collection_reordering() -> None:
    evidence_a = _schema_evidence(
        "evidence-a",
        _column("table-source-a", "column-source-a"),
    )
    evidence_b = _schema_evidence(
        "evidence-b",
        _column("table-source-b", "column-source-b"),
    )
    edge = JoinEdge(
        left=_column("table-source-a", "source_b_id"),
        right=_column("table-source-b", "id"),
        join_type=JoinType.INNER,
    )
    join_evidence = _schema_evidence(
        "join-evidence",
        edge.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    state = _state(
        item_specs=(
            ("source-b", True, SemanticItemStatus.RESOLVED, ("binding-b",)),
            ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-a",)),
        ),
        bindings=(
            _physical_binding("source-b", "binding-b", "evidence-b", join_path=(edge,)),
            _physical_binding("source-a", "binding-a", "evidence-a", join_path=(edge,)),
        ),
        evidence=(evidence_b, evidence_a, join_evidence),
        joins=(_join_candidate("join-a-b", (edge,), "join-evidence"),),
    )
    reordered_query = state.query_spec.model_copy(
        update={"semantic_items": tuple(reversed(state.query_spec.semantic_items))}
    )
    reordered = state.model_copy(
        update={
            "query_spec": reordered_query,
            "bindings": tuple(reversed(state.bindings)),
            "evidence": tuple(reversed(state.evidence)),
        }
    )

    assert _validate(state) == _validate(reordered)
    requirements = _validate(state)
    assert requirements.required_source_ids == ("source-a", "source-b")
    assert tuple(selected.source_id for selected in requirements.selected_bindings) == (
        "source-a",
        "source-b",
    )


def test_required_binding_tables_must_form_one_validated_join_component() -> None:
    evidence_a = _schema_evidence(
        "evidence-a",
        _column("table-source-a", "column-source-a"),
    )
    evidence_b = _schema_evidence(
        "evidence-b",
        _column("table-source-b", "column-source-b"),
    )
    disconnected = _state(
        item_specs=(
            ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-a",)),
            ("source-b", True, SemanticItemStatus.RESOLVED, ("binding-b",)),
        ),
        bindings=(
            _physical_binding("source-a", "binding-a", "evidence-a"),
            _physical_binding("source-b", "binding-b", "evidence-b"),
        ),
        evidence=(evidence_a, evidence_b),
    )
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        ("source-a", "source-b"),
        lambda: _validate(disconnected),
    )

    edge = JoinEdge(
        left=_column("table-source-a", "source_b_id"),
        right=_column("table-source-b", "id"),
        join_type=JoinType.INNER,
    )
    join_evidence = _schema_evidence(
        "join-evidence",
        edge.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    connected = _state(
        item_specs=(
            ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-a",)),
            ("source-b", True, SemanticItemStatus.RESOLVED, ("binding-b",)),
        ),
        bindings=(
            _physical_binding("source-a", "binding-a", "evidence-a", join_path=(edge,)),
            _physical_binding("source-b", "binding-b", "evidence-b", join_path=(edge,)),
        ),
        evidence=(evidence_a, evidence_b, join_evidence),
        joins=(_join_candidate("join-a-b", (edge,), "join-evidence"),),
    )
    assert _validate(connected).required_source_ids == ("source-a", "source-b")


def test_four_required_tables_in_two_join_components_fail_closed() -> None:
    sources = ("source-a", "source-b", "source-c", "source-d")
    evidence = tuple(
        _schema_evidence(
            f"evidence-{source[-1]}",
            _column(f"table-{source}", f"column-{source}"),
        )
        for source in sources
    )
    edge_a_b = JoinEdge(
        left=_column("table-source-a", "source_b_id"),
        right=_column("table-source-b", "id"),
        join_type=JoinType.INNER,
    )
    edge_b_c = JoinEdge(
        left=_column("table-source-b", "source_c_id"),
        right=_column("table-source-c", "id"),
        join_type=JoinType.INNER,
    )
    edge_c_d = JoinEdge(
        left=_column("table-source-c", "source_d_id"),
        right=_column("table-source-d", "id"),
        join_type=JoinType.INNER,
    )
    join_evidence = tuple(
        _schema_evidence(
            f"join-evidence-{label}",
            edge.left.table,
            kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        )
        for label, edge in (("a-b", edge_a_b), ("b-c", edge_b_c), ("c-d", edge_c_d))
    )

    disconnected = _state(
        item_specs=tuple(
            (source, True, SemanticItemStatus.RESOLVED, (f"binding-{source[-1]}",))
            for source in sources
        ),
        bindings=(
            _physical_binding(
                "source-a", "binding-a", "evidence-a", join_path=(edge_a_b,)
            ),
            _physical_binding(
                "source-b", "binding-b", "evidence-b", join_path=(edge_a_b,)
            ),
            _physical_binding(
                "source-c", "binding-c", "evidence-c", join_path=(edge_c_d,)
            ),
            _physical_binding(
                "source-d", "binding-d", "evidence-d", join_path=(edge_c_d,)
            ),
        ),
        evidence=(*evidence, join_evidence[0], join_evidence[2]),
        joins=(
            _join_candidate("join-a-b", (edge_a_b,), join_evidence[0].evidence_id),
            _join_candidate("join-c-d", (edge_c_d,), join_evidence[2].evidence_id),
        ),
    )
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        sources,
        lambda: _validate(disconnected),
    )

    connected = _state(
        item_specs=tuple(
            (source, True, SemanticItemStatus.RESOLVED, (f"binding-{source[-1]}",))
            for source in sources
        ),
        bindings=(
            _physical_binding(
                "source-a", "binding-a", "evidence-a", join_path=(edge_a_b,)
            ),
            _physical_binding(
                "source-b",
                "binding-b",
                "evidence-b",
                join_path=(edge_a_b, edge_b_c),
            ),
            _physical_binding(
                "source-c",
                "binding-c",
                "evidence-c",
                join_path=(edge_b_c, edge_c_d),
            ),
            _physical_binding(
                "source-d", "binding-d", "evidence-d", join_path=(edge_c_d,)
            ),
        ),
        evidence=(*evidence, *join_evidence),
        joins=(
            _join_candidate("join-a-b", (edge_a_b,), join_evidence[0].evidence_id),
            _join_candidate("join-b-c", (edge_b_c,), join_evidence[1].evidence_id),
            _join_candidate("join-c-d", (edge_c_d,), join_evidence[2].evidence_id),
        ),
    )
    assert _validate(connected).required_source_ids == sources


def test_action_parameters_and_evidence_ids_are_canonically_unordered() -> None:
    action_left = _action((("z", 2), ("a", 1)))
    action_right = _action((("a", 1), ("z", 2)))
    left = _state(action_history=(action_left,))
    right = _state(action_history=(action_right,))
    assert _validate(left) == _validate(right)

    evidence_a = _schema_evidence(
        "evidence-a",
        _column("table-source-a", "column-source-a"),
    )
    evidence_b = _schema_evidence(
        "evidence-b",
        _column("table-source-a", "column-source-a"),
    )
    binding = _physical_binding("source-a", "binding-a", "evidence-a")
    binding_left = binding.model_copy(
        update={"evidence_ids": ("evidence-b", "evidence-a")}
    )
    binding_right = binding.model_copy(
        update={"evidence_ids": ("evidence-a", "evidence-b")}
    )
    left = _state(bindings=(binding_left,), evidence=(evidence_b, evidence_a))
    right = _state(bindings=(binding_right,), evidence=(evidence_a, evidence_b))
    assert _validate(left) == _validate(right)


def test_meaningful_composite_join_order_changes_authorization_digest() -> None:
    first = JoinEdge(
        left=_column("orders", "tenant_id"),
        right=_column("customers", "tenant_id"),
        join_type=JoinType.INNER,
    )
    second = JoinEdge(
        left=_column("orders", "customer_id"),
        right=_column("customers", "id"),
        join_type=JoinType.INNER,
    )
    base = _state()
    join_evidence = _schema_evidence(
        "join-order-evidence",
        first.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )

    def state_with_path(path: tuple[JoinEdge, ...]) -> ResearchState:
        return _state(
            bindings=(
                _physical_binding(
                    "source-a",
                    "binding-a",
                    "evidence-a",
                    join_path=path,
                ),
            ),
            evidence=(*base.evidence, join_evidence),
            joins=(_join_candidate("join-order", path, join_evidence.evidence_id),),
        )

    ordered = _validate(state_with_path((first, second)))
    reversed_order = _validate(state_with_path((second, first)))
    assert ordered.state_digest != reversed_order.state_digest
    assert ordered.requirements_digest != reversed_order.requirements_digest


def test_recomputed_digest_cannot_authorize_forged_derived_footprint() -> None:
    requirements = _validate(_state())
    extra_predicate = PredicateRef(
        left=_column("hidden", "secret"),
        operator=PredicateOperator.EQ,
        right=1,
    )
    extra_path = (
        JoinEdge(
            left=_column("hidden", "left_id"),
            right=_column("other", "right_id"),
            join_type=JoinType.INNER,
        ),
    )
    forged_fields = (
        {"eligible_evidence_ids": ()},
        {
            "allowed_tables": tuple(
                sorted(
                    (*requirements.allowed_tables, _table("hidden")),
                    key=canonical_json_bytes,
                )
            )
        },
        {"allowed_columns": ()},
        {"allowed_predicates": (extra_predicate,)},
        {"allowed_join_paths": (extra_path,)},
    )
    for updates in forged_fields:
        _assert_forged_requirements_rejected(requirements, **updates)


def test_recomputed_digest_rejects_extra_and_missing_eligible_joins() -> None:
    no_join_requirements = _validate(_state())
    extra_edge = JoinEdge(
        left=_column("hidden", "left_id"),
        right=_column("other", "right_id"),
        join_type=JoinType.INNER,
    )
    extra_join = JoinCandidate(
        join_id="forged-extra-join",
        left=extra_edge.left,
        right=extra_edge.right,
        join_type=JoinType.INNER,
        path=(extra_edge,),
        status=JoinCandidateStatus.VALIDATED,
        evidence_ids=("forged-join-evidence",),
    )
    _assert_forged_requirements_rejected(
        no_join_requirements,
        eligible_validated_joins=(extra_join,),
        eligible_evidence_ids=(
            *no_join_requirements.eligible_evidence_ids,
            "forged-join-evidence",
        ),
    )

    required_edge = JoinEdge(
        left=_column("orders", "customer_id"),
        right=_column("customers", "id"),
        join_type=JoinType.INNER,
    )
    base = _state()
    join_evidence = _schema_evidence(
        "required-join-evidence",
        required_edge.left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    binding = _physical_binding(
        "source-a",
        "binding-a",
        "evidence-a",
        join_path=(required_edge,),
    )
    join = _join_candidate(
        "required-join",
        (required_edge,),
        join_evidence.evidence_id,
    )
    with_join = _validate(
        _state(
            bindings=(binding,),
            evidence=(*base.evidence, join_evidence),
            joins=(join,),
        )
    )
    _assert_forged_requirements_rejected(
        with_join,
        eligible_validated_joins=(),
        eligible_evidence_ids=("evidence-a",),
    )


def test_requirements_are_immutable_and_selection_is_not_caller_controlled() -> None:
    requirements = _validate(_state())
    with pytest.raises(ValidationError):
        requirements.state_revision = 99  # type: ignore[misc]
    assert tuple(inspect.signature(validate_coverage_inputs).parameters) == (
        "state",
        "freshness_context",
        "run_id",
        "run_incarnation",
    )


def test_readiness_module_has_no_runtime_or_persistence_imports() -> None:
    module = inspect.getmodule(validate_coverage_inputs)
    assert module is not None
    source = inspect.getsource(module)
    for forbidden in (
        "sqlite3",
        "workflow.",
        ".policy",
        ".probes",
        ".tool_registry",
        ".sql_ast",
        "model_call",
    ):
        assert forbidden not in source
