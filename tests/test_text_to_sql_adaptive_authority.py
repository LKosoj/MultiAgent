"""Evidence-backed W4-01 authority decisions."""

from __future__ import annotations

import pytest

from custom_tools.text_to_sql.adaptive.policy import (
    ResearchGenerationAuthority,
    ResearchGenerationAuthorityStatus,
    evaluate_research_generation_authority,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import CoverageInputErrorCode
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    DiscriminatorValueBinding,
    JoinEdge,
    JoinType,
    LiteralValue,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    ResearchActionKind,
    ResearchState,
    ResearchStopReason,
    SemanticItemKind,
    SemanticItemStatus,
    VerticalAttributeBinding,
)
from tests.text_to_sql_semantic_coverage_helpers import (
    INCARNATION,
    OTHER_SCHEMA,
    RUN_ID,
    _context,
    _column,
    _join_candidate,
    _physical_binding,
    _schema_evidence,
    _state,
    _value_evidence,
)


def _with_required_filter(
    state: ResearchState,
    literal: object,
    operator: PredicateOperator | None = PredicateOperator.EQ,
) -> ResearchState:
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FILTER,
            "operator": operator,
            "literal_or_reference": literal,
        }
    )
    return state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            )
        }
    )


def _discriminator_binding(
    column,
    predicate: PredicateRef,
    evidence_ids: tuple[str, ...],
) -> DiscriminatorValueBinding:
    return DiscriminatorValueBinding(
        binding_id="value-binding",
        source_id="source-a",
        tables=(column.table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=evidence_ids,
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="forged-supported-status",
        discriminator_column=column,
        discriminator_predicate=predicate,
    )


def _required_filter_state(
    binding,
    evidence,
    literal: object,
    operator: PredicateOperator | None = PredicateOperator.EQ,
):
    return _with_required_filter(
        _state(
            item_specs=(
                ("source-a", True, SemanticItemStatus.RESOLVED, (binding.binding_id,)),
            ),
            bindings=(binding,),
            evidence=tuple(evidence),
        ),
        literal,
        operator,
    )


def _vertical_filter_state(
    *,
    value_operator: PredicateOperator = PredicateOperator.EQ,
    query_right: str | int | None = "premium",
    observed_value: str | int | None = "premium",
    include_value_evidence: bool = True,
    omit_schema_index: int | None = None,
    stale_value: bool = False,
    include_attribute_join: bool = True,
):
    entity_key = _column("customers", "id")
    catalog_key = _column("property_types", "id")
    catalog_name = _column("property_types", "name")
    value_entity_key = _column("property_values", "customer_id")
    value_attribute_key = _column("property_values", "property_type_id")
    value = _column("property_values", "value")
    entity_join = JoinEdge(
        left=entity_key, right=value_entity_key, join_type=JoinType.INNER
    )
    attribute_join = JoinEdge(
        left=catalog_key, right=value_attribute_key, join_type=JoinType.INNER
    )
    name_predicate = PredicateRef(
        left=catalog_name, operator=PredicateOperator.EQ, right="tariff"
    )
    value_predicate = PredicateRef(
        left=value,
        operator=value_operator,
        right=None if value_operator is PredicateOperator.IS_NULL else "premium",
    )
    schema_evidence = tuple(
        _schema_evidence(f"vertical-schema-{index}", column)
        for index, column in enumerate(
            (
                entity_key,
                catalog_key,
                catalog_name,
                value_entity_key,
                value_attribute_key,
                value,
            ),
            start=1,
        )
        if index != omit_schema_index
    )
    catalog_evidence = _value_evidence("vertical-catalog", catalog_name, "tariff")
    entity_join_evidence = _schema_evidence(
        "vertical-entity-join",
        entity_key.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    attribute_join_evidence = _schema_evidence(
        "vertical-attribute-join",
        catalog_key.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    evidence = [
        *schema_evidence,
        catalog_evidence,
        entity_join_evidence,
        attribute_join_evidence,
    ]
    if include_value_evidence:
        value_evidence = _value_evidence("vertical-value", value, observed_value)
        if stale_value:
            value_evidence = value_evidence.model_copy(
                update={"observation": "legacy value evidence"}
            )
        evidence.append(value_evidence)
    binding = VerticalAttributeBinding(
        binding_id="vertical-value-binding",
        source_id="source-a",
        tables=(entity_key.table, catalog_key.table, value.table),
        columns=(
            entity_key,
            catalog_key,
            catalog_name,
            value_entity_key,
            value_attribute_key,
            value,
        ),
        predicates=(name_predicate, value_predicate),
        join_path=(entity_join, attribute_join),
        evidence_ids=tuple(item.evidence_id for item in evidence),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="forged-supported-status",
        entity_table=entity_key.table,
        entity_key=entity_key,
        attribute_catalog_table=catalog_key.table,
        attribute_catalog_key=catalog_key,
        attribute_name_predicate=name_predicate,
        value_table=value.table,
        value_entity_key=value_entity_key,
        value_attribute_key=value_attribute_key,
        value_predicate=value_predicate,
    )
    joins = [
        _join_candidate(
            "vertical-entity",
            (entity_join,),
            entity_join_evidence.evidence_id,
        ),
    ]
    if include_attribute_join:
        joins.append(
            _join_candidate(
                "vertical-attribute",
                (attribute_join,),
                attribute_join_evidence.evidence_id,
            )
        )
    state = _state(
        item_specs=(
            (
                "source-a",
                True,
                SemanticItemStatus.RESOLVED,
                (binding.binding_id,),
            ),
        ),
        bindings=(binding,),
        evidence=tuple(evidence),
        joins=tuple(joins),
    )
    return (
        _with_required_filter(state, query_right, value_operator),
        name_predicate,
        value_predicate,
    )


def test_value_filter_with_only_physical_column_evidence_is_deferred() -> None:
    binding = _physical_binding("source-a", "binding-a", "evidence-a")
    state = _with_required_filter(
        _state(bindings=(binding,)),
        "premium",
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
    assert decision.affected_source_ids == ("source-a",)


@pytest.mark.parametrize(
    ("operator", "literal"),
    (
        (None, "premium"),
        (PredicateOperator.EQ, None),
        (PredicateOperator.IS_NULL, None),
    ),
)
def test_physical_column_binding_never_authorizes_a_required_filter(
    operator: PredicateOperator | None,
    literal: object,
) -> None:
    binding = _physical_binding("source-a", "binding-a", "evidence-a")
    state = _with_required_filter(
        _state(bindings=(binding,)),
        literal,
        operator,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
    assert decision.affected_source_ids == ("source-a",)


def test_physical_column_with_exact_value_evidence_is_deferred() -> None:
    binding = _physical_binding("source-a", "binding-a", "physical-schema")
    column = binding.physical_column
    schema_evidence = _schema_evidence("physical-schema", column)
    value_evidence = _value_evidence("physical-value", column, "paid")
    binding = binding.model_copy(
        update={
            "evidence_ids": (schema_evidence.evidence_id, value_evidence.evidence_id)
        }
    )
    state = _required_filter_state(
        binding,
        (schema_evidence, value_evidence),
        "paid",
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
    assert decision.affected_source_ids == ("source-a",)


def test_discriminator_value_filter_with_exact_current_evidence_is_allowed() -> None:
    column = _column("orders", "tier")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right="premium",
    )
    schema_evidence = _schema_evidence("tier-schema", column)
    value_evidence = _value_evidence("tier-value", column, "premium")
    binding = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id, value_evidence.evidence_id),
    )
    state = _required_filter_state(
        binding,
        (schema_evidence, value_evidence),
        "premium",
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (predicate,)


def test_discriminator_null_filter_requires_schema_and_exact_null_value() -> None:
    column = _column("orders", "cancelled_at")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.IS_NULL,
        right=None,
    )
    schema_evidence = _schema_evidence("cancelled-schema", column)
    null_evidence = _value_evidence("cancelled-null", column, None)
    binding = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id, null_evidence.evidence_id),
    )
    state = _required_filter_state(
        binding,
        (schema_evidence, null_evidence),
        None,
        PredicateOperator.IS_NULL,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (predicate,)


def test_filter_allows_supporting_physical_column_alongside_predicate_binding() -> None:
    column = _column("orders", "completed_at")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.IS_NOT_NULL,
        right=None,
    )
    schema_evidence = _schema_evidence("completed-schema", column)
    value_evidence = _value_evidence("completed-value", column, "09:30")
    discriminator = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id, value_evidence.evidence_id),
    )
    physical = PhysicalColumnBinding(
        binding_id="physical-binding",
        source_id="source-a",
        tables=(column.table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(schema_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=column,
    )
    state = _with_required_filter(
        _state(
            item_specs=(
                (
                    "source-a",
                    True,
                    SemanticItemStatus.RESOLVED,
                    tuple(sorted((discriminator.binding_id, physical.binding_id))),
                ),
            ),
            bindings=(discriminator, physical),
            evidence=(schema_evidence, value_evidence),
        ),
        None,
        PredicateOperator.IS_NOT_NULL,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (predicate,)


@pytest.mark.parametrize(
    "operator", (PredicateOperator.IS_NULL, PredicateOperator.IS_NOT_NULL)
)
def test_supported_null_predicate_is_authoritative_despite_stale_query_literal(
    operator: PredicateOperator,
) -> None:
    column = _column("orders", "cancelled_at")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.IS_NULL,
        right=None,
    )
    schema_evidence = _schema_evidence("cancelled-schema", column)
    null_evidence = _value_evidence("cancelled-null", column, None)
    binding = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id, null_evidence.evidence_id),
    )
    state = _required_filter_state(
        binding,
        (schema_evidence, null_evidence),
        "not-null",
        operator,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (predicate,)


def test_is_not_null_filter_does_not_require_an_observed_matching_row() -> None:
    column = _column("orders", "cancelled_at")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.IS_NOT_NULL,
        right=None,
    )
    schema_evidence = _schema_evidence("cancelled-schema", column)
    binding = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id,),
    )
    state = _required_filter_state(
        binding,
        (schema_evidence,),
        None,
        PredicateOperator.IS_NOT_NULL,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (predicate,)


def test_mixed_type_in_filter_requires_exact_evidence_for_every_value() -> None:
    column = _column("orders", "tier")
    values = ("premium", 1, True)
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.IN,
        right=values,
    )
    schema_evidence = _schema_evidence("tier-schema", column)
    value_evidence = tuple(
        _value_evidence(f"tier-value-{index}", column, value)
        for index, value in enumerate(values, start=1)
    )
    binding = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id, *(item.evidence_id for item in value_evidence)),
    )
    state = _required_filter_state(
        binding,
        (schema_evidence, *value_evidence),
        values,
        PredicateOperator.IN,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (predicate,)


@pytest.mark.parametrize("right", ("premium", ()))
def test_in_filter_with_scalar_or_empty_right_is_deferred(right: object) -> None:
    column = _column("orders", "tier")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.IN,
        right=right,
    )
    schema_evidence = _schema_evidence("tier-schema", column)
    binding = _discriminator_binding(column, predicate, (schema_evidence.evidence_id,))
    state = _required_filter_state(
        binding,
        (schema_evidence,),
        right,
        PredicateOperator.IN,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
    assert decision.affected_source_ids == ("source-a",)


def test_literal_value_filter_preserves_exact_underlying_type() -> None:
    column = _column("orders", "priority")
    literal = LiteralValue(value=1)
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right=literal,
    )
    schema_evidence = _schema_evidence("priority-schema", column)
    value_evidence = _value_evidence("priority-value", column, 1)
    binding = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id, value_evidence.evidence_id),
    )
    state = _required_filter_state(
        binding,
        (schema_evidence, value_evidence),
        literal,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True


@pytest.mark.parametrize(
    ("operator", "right"),
    (
        (PredicateOperator.GT, 1),
        (PredicateOperator.GTE, 1),
        (PredicateOperator.LT, 1),
        (PredicateOperator.LTE, 1),
        (PredicateOperator.BETWEEN, (1, 2)),
    ),
)
def test_discriminator_ordered_bounds_do_not_require_observed_row_values(
    operator: PredicateOperator,
    right: object,
) -> None:
    column = _column("orders", "priority")
    predicate = PredicateRef(left=column, operator=operator, right=right)
    schema_evidence = _schema_evidence("priority-schema", column)
    binding = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id,),
    )
    state = _required_filter_state(
        binding,
        (schema_evidence,),
        right,
        operator,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (predicate,)


@pytest.mark.parametrize(
    "observed_values",
    ((1,), (1, "2")),
    ids=("missing-upper-endpoint", "wrong-upper-endpoint-type"),
)
def test_discriminator_between_filter_accepts_without_exact_typed_endpoints(
    observed_values: tuple[object, ...],
) -> None:
    column = _column("orders", "priority")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.BETWEEN,
        right=(1, 2),
    )
    schema_evidence = _schema_evidence("priority-schema", column)
    value_evidence = tuple(
        _value_evidence(f"priority-value-{index}", column, value)
        for index, value in enumerate(observed_values, start=1)
    )
    binding = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id, *(item.evidence_id for item in value_evidence)),
    )
    state = _required_filter_state(
        binding,
        (schema_evidence, *value_evidence),
        (1, 2),
        PredicateOperator.BETWEEN,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (predicate,)


@pytest.mark.parametrize(
    ("operator", "right"),
    (
        (PredicateOperator.NEQ, "premium"),
        (PredicateOperator.NOT_IN, ("premium",)),
        (PredicateOperator.LIKE, "premium%"),
    ),
)
def test_filter_operator_does_not_require_an_observed_matching_row(
    operator: PredicateOperator,
    right: object,
) -> None:
    column = _column("orders", "tier")
    predicate = PredicateRef(left=column, operator=operator, right=right)
    schema_evidence = _schema_evidence("tier-schema", column)
    binding = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id,),
    )
    state = _required_filter_state(binding, (schema_evidence,), right, operator)

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (predicate,)


@pytest.mark.parametrize(
    "case", ("wrong-value", "wrong-column", "wrong-type", "missing")
)
def test_value_filter_ignores_unrelated_observed_values(case: str) -> None:
    column = _column("orders", "tier")
    literal: str | int = 1 if case == "wrong-type" else "premium"
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right=literal,
    )
    schema_evidence = _schema_evidence("tier-schema", column)
    evidence = [schema_evidence]
    if case != "missing":
        observed_column = (
            _column("orders", "other_tier") if case == "wrong-column" else column
        )
        observed_value: str | int
        if case == "wrong-value":
            observed_value = "standard"
        elif case == "wrong-type":
            observed_value = "1"
        else:
            observed_value = "premium"
        evidence.append(_value_evidence("tier-value", observed_column, observed_value))
    binding = _discriminator_binding(
        column,
        predicate,
        tuple(item.evidence_id for item in evidence),
    )
    state = _required_filter_state(binding, evidence, literal)

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (predicate,)


def test_value_filter_with_stale_value_evidence_is_deferred() -> None:
    column = _column("orders", "tier")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right="premium",
    )
    schema_evidence = _schema_evidence("tier-schema", column)
    stale_value = _value_evidence("tier-value", column, "premium").model_copy(
        update={"observation": "legacy value evidence"}
    )
    binding = _discriminator_binding(
        column,
        predicate,
        (schema_evidence.evidence_id, stale_value.evidence_id),
    )
    state = _required_filter_state(
        binding,
        (schema_evidence, stale_value),
        "premium",
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.STALE_BINDING_EVIDENCE
    assert decision.affected_source_ids == ("source-a",)


def test_vertical_value_filter_requires_exact_catalog_and_value_evidence() -> None:
    state, _, _ = _vertical_filter_state(include_value_evidence=False)

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
    assert decision.affected_source_ids == ("source-a",)


def test_vertical_value_filter_with_exact_current_evidence_and_joins_is_allowed() -> (
    None
):
    state, name_predicate, value_predicate = _vertical_filter_state()

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert name_predicate in decision.requirements.allowed_predicates
    assert value_predicate in decision.requirements.allowed_predicates


def test_vertical_null_filter_with_exact_current_evidence_and_joins_is_allowed() -> (
    None
):
    state, name_predicate, value_predicate = _vertical_filter_state(
        value_operator=PredicateOperator.IS_NULL,
        query_right=None,
        observed_value=None,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_predicates == (
        name_predicate,
        value_predicate,
    )


def test_vertical_null_filter_requires_exact_null_value_evidence() -> None:
    state, _, _ = _vertical_filter_state(
        value_operator=PredicateOperator.IS_NULL,
        query_right=None,
        include_value_evidence=False,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
    assert decision.affected_source_ids == ("source-a",)


@pytest.mark.parametrize("observed_value", ("not-null", 0))
def test_vertical_null_filter_rejects_other_value_or_type(
    observed_value: str | int,
) -> None:
    state, _, _ = _vertical_filter_state(
        value_operator=PredicateOperator.IS_NULL,
        query_right=None,
        observed_value=observed_value,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
    assert decision.affected_source_ids == ("source-a",)


def test_vertical_null_filter_uses_the_supported_binding_footprint() -> None:
    state, _, _ = _vertical_filter_state(
        value_operator=PredicateOperator.IS_NULL,
        query_right=None,
        observed_value=None,
        omit_schema_index=6,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None


def test_vertical_null_filter_requires_both_validated_joins() -> None:
    state, _, _ = _vertical_filter_state(
        value_operator=PredicateOperator.IS_NULL,
        query_right=None,
        observed_value=None,
        include_attribute_join=False,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
    assert decision.affected_source_ids == ("source-a",)


def test_vertical_null_filter_with_stale_value_evidence_is_deferred() -> None:
    state, _, _ = _vertical_filter_state(
        value_operator=PredicateOperator.IS_NULL,
        query_right=None,
        observed_value=None,
        stale_value=True,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.STALE_BINDING_EVIDENCE
    assert decision.affected_source_ids == ("source-a",)


@pytest.mark.parametrize(
    ("observed_value", "stale_value", "include_attribute_join", "reason"),
    (
        ("standard", False, True, CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE),
        ("premium", True, True, CoverageInputErrorCode.STALE_BINDING_EVIDENCE),
        ("premium", False, False, CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE),
    ),
)
def test_vertical_value_filter_rejects_bad_value_stale_evidence_or_missing_join(
    observed_value: str,
    stale_value: bool,
    include_attribute_join: bool,
    reason: CoverageInputErrorCode,
) -> None:
    state, _, _ = _vertical_filter_state(
        observed_value=observed_value,
        stale_value=stale_value,
        include_attribute_join=include_attribute_join,
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is reason
    assert decision.affected_source_ids == ("source-a",)


def test_non_value_dimension_and_metric_bindings_remain_allowed() -> None:
    dimension = evaluate_research_generation_authority(
        _state(), _context(), RUN_ID, INCARNATION
    )
    metric_item = (
        _state()
        .query_spec.semantic_items[0]
        .model_copy(update={"kind": SemanticItemKind.METRIC})
    )
    metric_state = _state().model_copy(
        update={
            "query_spec": _state().query_spec.model_copy(
                update={"semantic_items": (metric_item,)}
            )
        }
    )
    metric = evaluate_research_generation_authority(
        metric_state, _context(), RUN_ID, INCARNATION
    )

    assert dimension.allowed is True
    assert metric.allowed is True


def test_supported_current_physical_binding_allows_generation() -> None:
    decision = evaluate_research_generation_authority(
        _state(), _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.status is ResearchGenerationAuthorityStatus.ALLOWED
    assert decision.reason is None
    assert decision.affected_source_ids == ()
    assert decision.requirements is not None
    assert decision.requirements.required_source_ids == ("source-a",)


def test_supported_current_vertical_eav_binding_with_validated_joins_allows_generation() -> (
    None
):
    entity_key = _column("entities", "id")
    catalog_key = _column("attributes", "id")
    catalog_name = _column("attributes", "name")
    value_entity_key = _column("attribute_values", "entity_id")
    value_attribute_key = _column("attribute_values", "attribute_id")
    value = _column("attribute_values", "value")
    entity_join = JoinEdge(
        left=entity_key, right=value_entity_key, join_type=JoinType.INNER
    )
    attribute_join = JoinEdge(
        left=catalog_key, right=value_attribute_key, join_type=JoinType.INNER
    )
    evidence = _schema_evidence("vertical-evidence", catalog_name)
    binding = VerticalAttributeBinding(
        binding_id="vertical-binding",
        source_id="source-a",
        tables=(entity_key.table, catalog_key.table, value.table),
        columns=(
            entity_key,
            catalog_key,
            catalog_name,
            value_entity_key,
            value_attribute_key,
            value,
        ),
        predicates=(
            PredicateRef(
                left=catalog_name, operator=PredicateOperator.EQ, right="premium"
            ),
            PredicateRef(left=value, operator=PredicateOperator.EQ, right="yes"),
        ),
        join_path=(entity_join, attribute_join),
        evidence_ids=(evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        entity_table=entity_key.table,
        entity_key=entity_key,
        attribute_catalog_table=catalog_key.table,
        attribute_catalog_key=catalog_key,
        attribute_name_predicate=PredicateRef(
            left=catalog_name, operator=PredicateOperator.EQ, right="premium"
        ),
        value_table=value.table,
        value_entity_key=value_entity_key,
        value_attribute_key=value_attribute_key,
        value_predicate=PredicateRef(
            left=value, operator=PredicateOperator.EQ, right="yes"
        ),
    )
    state = _state(
        item_specs=(
            ("source-a", True, SemanticItemStatus.RESOLVED, ("vertical-binding",)),
        ),
        bindings=(binding,),
        evidence=(evidence,),
        joins=(
            _join_candidate("entity-join", (entity_join,), evidence.evidence_id),
            _join_candidate("attribute-join", (attribute_join,), evidence.evidence_id),
        ),
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert decision.requirements.allowed_join_paths == ((entity_join, attribute_join),)


def test_unresolved_required_source_is_deferred_with_exact_source_id() -> None:
    decision = evaluate_research_generation_authority(
        _state(
            item_specs=(("source-a", True, SemanticItemStatus.UNRESOLVED, ()),),
            bindings=(),
            unresolved_items=("source-a",),
        ),
        _context(),
        RUN_ID,
        INCARNATION,
    )

    assert decision.allowed is False
    assert decision.status is ResearchGenerationAuthorityStatus.DEFERRED
    assert decision.reason is CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE
    assert decision.affected_source_ids == ("source-a",)
    assert decision.requirements is None


def test_multiple_supported_required_bindings_are_authorized_as_one_union() -> None:
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
            _physical_binding("source-a", "binding-b", "evidence-a"),
        ),
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.reason is None
    assert decision.requirements is not None
    assert tuple(binding.binding_id for binding in decision.requirements.selected_bindings) == (
        "binding-a",
        "binding-b",
    )


def test_namespace_mismatch_fails_closed_with_exact_required_sources() -> None:
    decision = evaluate_research_generation_authority(
        _state(), _context(schema=OTHER_SCHEMA), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.status is ResearchGenerationAuthorityStatus.DEFERRED
    assert decision.reason is CoverageInputErrorCode.SCHEMA_NAMESPACE_MISMATCH
    assert decision.affected_source_ids == ("source-a",)


def test_unsupported_required_source_is_deferred() -> None:
    decision = evaluate_research_generation_authority(
        _state(
            item_specs=(("source-a", True, SemanticItemStatus.UNSUPPORTED, ()),),
            bindings=(),
            unresolved_items=("source-a",),
            stop_reason=ResearchStopReason.UNSUPPORTED,
        ),
        _context(),
        RUN_ID,
        INCARNATION,
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE
    assert decision.affected_source_ids == ("source-a",)


def test_missing_or_disconnected_validated_join_is_deferred() -> None:
    edge = JoinEdge(
        left=_column("table-source-a", "column-source-a"),
        right=_column("other", "source_id"),
        join_type=JoinType.INNER,
    )
    base = _state()
    join_evidence = _schema_evidence("join-evidence", edge.left)
    binding = _physical_binding(
        "source-a", "binding-a", "evidence-a", join_path=(edge,)
    )
    missing = _state(bindings=(binding,), evidence=(*base.evidence, join_evidence))

    missing_decision = evaluate_research_generation_authority(
        missing, _context(), RUN_ID, INCARNATION
    )
    assert (
        missing_decision.reason is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
    )
    assert missing_decision.affected_source_ids == ("source-a",)

    supported = _state(
        bindings=(binding,),
        evidence=(*base.evidence, join_evidence),
        joins=(_join_candidate("join-a", (edge,), join_evidence.evidence_id),),
    )
    supported_decision = evaluate_research_generation_authority(
        supported, _context(), RUN_ID, INCARNATION
    )
    assert supported_decision.allowed is True

    evidence_b = _schema_evidence(
        "evidence-b", _column("table-source-b", "column-source-b")
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
        evidence=(*base.evidence, evidence_b),
    )
    disconnected_decision = evaluate_research_generation_authority(
        disconnected, _context(), RUN_ID, INCARNATION
    )
    assert (
        disconnected_decision.reason
        is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
    )
    assert disconnected_decision.affected_source_ids == ("source-a", "source-b")


def test_validated_joins_through_bridge_table_allow_generation() -> None:
    left_column = _column("table-source-a", "column-source-a")
    right_column = _column("table-source-b", "column-source-b")
    left_edge = JoinEdge(
        left=_column("table-source-a", "id"),
        right=_column("bridge_records", "left_id"),
        join_type=JoinType.INNER,
    )
    right_edge = JoinEdge(
        left=_column("table-source-b", "id"),
        right=_column("bridge_records", "right_id"),
        join_type=JoinType.INNER,
    )
    unrelated_edge = JoinEdge(
        left=_column("table-source-a", "id"),
        right=_column("unrelated_records", "left_id"),
        join_type=JoinType.INNER,
    )
    evidence = (
        _schema_evidence("left-evidence", left_column),
        _schema_evidence("right-evidence", right_column),
        _schema_evidence(
            "left-join-evidence",
            left_edge.left.table,
            kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        ),
        _schema_evidence(
            "right-join-evidence",
            right_edge.left.table,
            kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        ),
        _schema_evidence(
            "unrelated-join-evidence",
            unrelated_edge.left.table,
            kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        ),
    )
    state = _state(
        item_specs=(
            ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-a",)),
            ("source-b", True, SemanticItemStatus.RESOLVED, ("binding-b",)),
        ),
        bindings=(
            _physical_binding("source-a", "binding-a", "left-evidence"),
            _physical_binding("source-b", "binding-b", "right-evidence"),
        ),
        evidence=evidence,
        joins=(
            _join_candidate("left-join", (left_edge,), "left-join-evidence"),
            _join_candidate("right-join", (right_edge,), "right-join-evidence"),
            _join_candidate(
                "unrelated-join", (unrelated_edge,), "unrelated-join-evidence"
            ),
        ),
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.reason is None
    assert decision.requirements is not None
    assert tuple(
        join.join_id for join in decision.requirements.eligible_validated_joins
    ) == ("left-join", "right-join")


def test_direct_validated_join_is_preferred_over_bridge_route() -> None:
    left_column = _column("table-source-a", "column-source-a")
    right_column = _column("table-source-b", "column-source-b")
    direct_edge = JoinEdge(
        left=_column("table-source-a", "id"),
        right=_column("table-source-b", "id"),
        join_type=JoinType.INNER,
    )
    left_bridge_edge = JoinEdge(
        left=_column("table-source-a", "id"),
        right=_column("bridge_records", "left_id"),
        join_type=JoinType.INNER,
    )
    right_bridge_edge = JoinEdge(
        left=_column("table-source-b", "id"),
        right=_column("bridge_records", "right_id"),
        join_type=JoinType.INNER,
    )
    evidence = (
        _schema_evidence("left-evidence", left_column),
        _schema_evidence("right-evidence", right_column),
        _schema_evidence(
            "direct-evidence",
            direct_edge.left.table,
            kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        ),
        _schema_evidence(
            "left-bridge-evidence",
            left_bridge_edge.left.table,
            kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        ),
        _schema_evidence(
            "right-bridge-evidence",
            right_bridge_edge.left.table,
            kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        ),
    )
    state = _state(
        item_specs=(
            ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-a",)),
            ("source-b", True, SemanticItemStatus.RESOLVED, ("binding-b",)),
        ),
        bindings=(
            _physical_binding("source-a", "binding-a", "left-evidence"),
            _physical_binding("source-b", "binding-b", "right-evidence"),
        ),
        evidence=evidence,
        joins=(
            _join_candidate("z-direct", (direct_edge,), "direct-evidence"),
            _join_candidate(
                "a-bridge-left", (left_bridge_edge,), "left-bridge-evidence"
            ),
            _join_candidate(
                "b-bridge-right", (right_bridge_edge,), "right-bridge-evidence"
            ),
        ),
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert tuple(
        join.join_id for join in decision.requirements.eligible_validated_joins
    ) == ("z-direct",)


def test_shortest_validated_bridge_route_limits_join_authority() -> None:
    left_column = _column("table-source-a", "column-source-a")
    right_column = _column("table-source-b", "column-source-b")
    edges = {
        "a-z": JoinEdge(
            left=_column("table-source-a", "id"),
            right=_column("z_records", "a_id"),
            join_type=JoinType.INNER,
        ),
        "b-y": JoinEdge(
            left=_column("table-source-b", "id"),
            right=_column("y_records", "b_id"),
            join_type=JoinType.INNER,
        ),
        "x-y": JoinEdge(
            left=_column("x_records", "y_id"),
            right=_column("y_records", "id"),
            join_type=JoinType.INNER,
        ),
        "x-z": JoinEdge(
            left=_column("x_records", "z_id"),
            right=_column("z_records", "id"),
            join_type=JoinType.INNER,
        ),
        "y-z": JoinEdge(
            left=_column("y_records", "z_id"),
            right=_column("z_records", "id"),
            join_type=JoinType.INNER,
        ),
    }
    evidence = [
        _schema_evidence("left-evidence", left_column),
        _schema_evidence("right-evidence", right_column),
    ]
    joins = []
    for join_id, edge in edges.items():
        evidence_id = f"{join_id}-evidence"
        evidence.append(
            _schema_evidence(
                evidence_id,
                edge.left.table,
                kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
            )
        )
        joins.append(_join_candidate(join_id, (edge,), evidence_id))
    state = _state(
        item_specs=(
            ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-a",)),
            ("source-b", True, SemanticItemStatus.RESOLVED, ("binding-b",)),
        ),
        bindings=(
            _physical_binding("source-a", "binding-a", "left-evidence"),
            _physical_binding("source-b", "binding-b", "right-evidence"),
        ),
        evidence=tuple(evidence),
        joins=tuple(joins),
    )

    decision = evaluate_research_generation_authority(
        state, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is True
    assert decision.requirements is not None
    assert {
        join.join_id for join in decision.requirements.eligible_validated_joins
    } == {"a-z", "b-y", "y-z"}


def test_stale_data_evidence_is_deferred() -> None:
    from custom_tools.text_to_sql.adaptive.freshness import (
        DocumentSourceAvailability,
        DocumentSourceState,
    )
    from tests.text_to_sql_semantic_coverage_helpers import (
        _document_binding,
        _document_evidence,
    )

    evidence = _document_evidence("document-evidence")
    decision = evaluate_research_generation_authority(
        _state(
            bindings=(
                _document_binding("source-a", "binding-a", evidence.evidence_id),
            ),
            evidence=(evidence,),
        ),
        _context(
            documents=(
                DocumentSourceState(
                    document_id="coverage-document",
                    availability=DocumentSourceAvailability.REMOVED,
                    source_version=None,
                ),
            )
        ),
        RUN_ID,
        INCARNATION,
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.STALE_BINDING_EVIDENCE
    assert decision.affected_source_ids == ("source-a",)


def test_state_query_run_and_incarnation_mismatches_fail_closed() -> None:
    state = _state()
    foreign_query = state.query_spec.model_copy(update={"run_id": "other-run"})
    mismatched_query = state.model_copy(update={"query_spec": foreign_query})
    checks = (
        (mismatched_query, _context(), RUN_ID, INCARNATION),
        (state, _context(), "other-run", INCARNATION),
        (state, _context(), RUN_ID, "other-incarnation"),
    )
    for candidate, context, run_id, incarnation in checks:
        decision = evaluate_research_generation_authority(
            candidate, context, run_id, incarnation
        )
        assert decision.allowed is False
        assert (
            decision.reason is CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH
        )
        assert decision.affected_source_ids == ("source-a",)


def test_malformed_state_fails_closed_without_fabricating_requirements() -> None:
    malformed = _state().model_copy(update={"query_spec": "invalid"})

    decision = evaluate_research_generation_authority(
        malformed, _context(), RUN_ID, INCARNATION
    )

    assert decision.allowed is False
    assert decision.reason is CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE
    assert decision.requirements is None


def test_authority_result_and_coverage_digest_are_deterministic() -> None:
    first = evaluate_research_generation_authority(
        _state(), _context(), RUN_ID, INCARNATION
    )
    second = evaluate_research_generation_authority(
        _state(), _context(), RUN_ID, INCARNATION
    )

    assert first == second
    assert first.requirements is not None
    assert (
        first.requirements.requirements_digest
        == second.requirements.requirements_digest
    )


def test_authority_result_rejects_noncanonical_runtime_values() -> None:
    invalid_fields = (
        {"allowed": 1},
        {"status": "DEFERRED"},
        {"reason": "UNKNOWN"},
        {"affected_source_ids": ("source-b", "source-a")},
        {"affected_source_ids": (1,)},
        {"requirements": object()},
    )
    for updates in invalid_fields:
        values = {
            "allowed": False,
            "status": ResearchGenerationAuthorityStatus.DEFERRED,
            "reason": CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE,
            "affected_source_ids": (),
            "requirements": None,
            **updates,
        }
        with pytest.raises((TypeError, ValueError)):
            ResearchGenerationAuthority(**values)

    allowed = evaluate_research_generation_authority(
        _state(), _context(), RUN_ID, INCARNATION
    )
    assert allowed.requirements is not None
    with pytest.raises(ValueError):
        ResearchGenerationAuthority(
            allowed=True,
            status=ResearchGenerationAuthorityStatus.ALLOWED,
            reason=None,
            affected_source_ids=("source-a",),
            requirements=allowed.requirements,
        )

    forged_requirements = object.__new__(type(allowed.requirements))
    with pytest.raises((TypeError, ValueError)):
        ResearchGenerationAuthority(
            allowed=True,
            status=ResearchGenerationAuthorityStatus.ALLOWED,
            reason=None,
            affected_source_ids=(),
            requirements=forged_requirements,
        )
