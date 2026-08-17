"""Binding footprint and join consistency checks for W5-00."""

from __future__ import annotations

from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    DerivedExpressionBinding,
    DiscriminatorValueBinding,
    DocumentRef,
    DocumentRuleBinding,
    ExpressionRef,
    JoinCandidate,
    JoinCandidateStatus,
    JoinEdge,
    JoinType,
    PredicateOperator,
    PredicateRef,
    ResearchActionKind,
    VerticalAttributeBinding,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageInputErrorCode,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_json_bytes
from tests.text_to_sql_semantic_coverage_helpers import (
    _assert_error,
    _column,
    _physical_binding,
    _schema_evidence,
    _state,
    _table,
    _validate,
)


def _predicate(table: str, column: str, value: object) -> PredicateRef:
    return PredicateRef(
        left=_column(table, column),
        operator=PredicateOperator.EQ,
        right=value,
    )


def _assert_binding_rejected(binding: object) -> None:
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        ("source-a",),
        lambda: _validate(_state(bindings=(binding,))),
    )


def test_physical_binding_base_footprint_is_exact() -> None:
    binding = _physical_binding("source-a", "binding-a", "evidence-a")
    assert _validate(_state(bindings=(binding,))).selected_bindings == (binding,)

    extra_column = _column("hidden", "secret")
    for forged in (
        binding.model_copy(update={"tables": (*binding.tables, extra_column.table)}),
        binding.model_copy(update={"columns": (*binding.columns, extra_column)}),
        binding.model_copy(update={"predicates": (_predicate("hidden", "secret", 1),)}),
        binding.model_copy(
            update={"physical_column": _column("other", "column-source-a")}
        ),
    ):
        _assert_binding_rejected(forged)


def _state_with_binding_joins(binding: object):
    path = binding.join_path
    base = _state()
    join_evidence = _schema_evidence(
        "join-evidence",
        path[0].left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    joins = tuple(
        JoinCandidate(
            join_id=f"join-{index}",
            left=edge.left,
            right=edge.right,
            join_type=edge.join_type,
            path=(edge,),
            status=JoinCandidateStatus.VALIDATED,
            evidence_ids=(join_evidence.evidence_id,),
        )
        for index, edge in enumerate(path)
    )
    return _state(
        bindings=(binding,),
        evidence=(*base.evidence, join_evidence),
        joins=joins,
    )


def _vertical_binding() -> VerticalAttributeBinding:
    entity_key = _column("entities", "id")
    catalog_key = _column("attributes", "id")
    name_predicate = _predicate("attributes", "name", "priority")
    value_entity_key = _column("values", "entity_id")
    value_attribute_key = _column("values", "attribute_id")
    value_predicate = _predicate("values", "number_value", 10)
    entity_join = JoinEdge(
        left=entity_key,
        right=value_entity_key,
        join_type=JoinType.INNER,
    )
    attribute_join = JoinEdge(
        left=catalog_key,
        right=value_attribute_key,
        join_type=JoinType.INNER,
    )
    return VerticalAttributeBinding(
        binding_id="binding-a",
        source_id="source-a",
        tables=(entity_key.table, catalog_key.table, value_entity_key.table),
        columns=(
            entity_key,
            catalog_key,
            name_predicate.left,
            value_entity_key,
            value_attribute_key,
            value_predicate.left,
        ),
        predicates=(name_predicate, value_predicate),
        join_path=(entity_join, attribute_join),
        evidence_ids=("evidence-a",),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        entity_table=entity_key.table,
        entity_key=entity_key,
        attribute_catalog_table=catalog_key.table,
        attribute_catalog_key=catalog_key,
        attribute_name_predicate=name_predicate,
        value_table=value_entity_key.table,
        value_entity_key=value_entity_key,
        value_attribute_key=value_attribute_key,
        value_predicate=value_predicate,
    )


def test_vertical_binding_cannot_hide_base_footprint() -> None:
    binding = _vertical_binding()
    assert _validate(_state_with_binding_joins(binding)).selected_bindings == (binding,)
    hidden_column = _column("hidden", "secret")
    mismatching_predicate = _predicate("attributes", "other_name", "priority")
    for forged in (
        binding.model_copy(update={"tables": (*binding.tables, hidden_column.table)}),
        binding.model_copy(update={"columns": (*binding.columns, hidden_column)}),
        binding.model_copy(
            update={
                "predicates": (*binding.predicates, _predicate("hidden", "secret", 1))
            }
        ),
        binding.model_copy(update={"attribute_name_predicate": mismatching_predicate}),
        binding.model_copy(update={"entity_key": _column("wrong-entities", "id")}),
    ):
        _assert_binding_rejected(forged)


def _discriminator_binding() -> DiscriminatorValueBinding:
    column = _column("orders", "kind")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right="retail",
    )
    return DiscriminatorValueBinding(
        binding_id="binding-a",
        source_id="source-a",
        tables=(column.table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=("evidence-a",),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        discriminator_column=column,
        discriminator_predicate=predicate,
    )


def test_discriminator_binding_footprint_matches_typed_fields() -> None:
    binding = _discriminator_binding()
    assert _validate(_state(bindings=(binding,))).selected_bindings == (binding,)
    other_predicate = _predicate("orders", "other_kind", "retail")
    for forged in (
        binding.model_copy(update={"tables": (*binding.tables, _table("hidden"))}),
        binding.model_copy(
            update={"columns": (*binding.columns, _column("hidden", "secret"))}
        ),
        binding.model_copy(
            update={"predicates": (*binding.predicates, other_predicate)}
        ),
        binding.model_copy(update={"discriminator_predicate": other_predicate}),
    ):
        _assert_binding_rejected(forged)


def _derived_binding() -> DerivedExpressionBinding:
    inputs = (_column("orders", "gross"), _column("customers", "discount"))
    tables = tuple(sorted({item.table for item in inputs}, key=canonical_json_bytes))
    return DerivedExpressionBinding(
        binding_id="binding-a",
        source_id="source-a",
        tables=tables,
        columns=inputs,
        predicates=(),
        join_path=(
            JoinEdge(
                left=_column("orders", "customer_id"),
                right=_column("customers", "id"),
                join_type=JoinType.INNER,
            ),
        ),
        evidence_ids=("evidence-a",),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        expression=ExpressionRef(
            expression_id="net-expression",
            expression="gross - discount",
        ),
        input_columns=inputs,
    )


def test_derived_binding_footprint_is_derived_only_from_inputs() -> None:
    binding = _derived_binding()
    requirements = _validate(_state_with_binding_joins(binding))
    selected = requirements.selected_bindings[0]
    canonical_inputs = tuple(
        sorted(set(binding.input_columns), key=canonical_json_bytes)
    )
    assert selected.columns == canonical_inputs
    assert selected.input_columns == binding.input_columns
    for forged in (
        binding.model_copy(update={"tables": (*binding.tables, _table("hidden"))}),
        binding.model_copy(
            update={"columns": (*binding.columns, _column("hidden", "secret"))}
        ),
        binding.model_copy(update={"predicates": (_predicate("hidden", "secret", 1),)}),
    ):
        _assert_binding_rejected(forged)


def test_derived_input_order_is_preserved_while_base_columns_are_canonical() -> None:
    binding = _derived_binding()
    reordered = binding.model_copy(
        update={
            "columns": tuple(reversed(binding.columns)),
            "input_columns": tuple(reversed(binding.input_columns)),
        }
    )

    baseline = _validate(_state_with_binding_joins(binding)).selected_bindings[0]
    reordered_selected = _validate(
        _state_with_binding_joins(reordered)
    ).selected_bindings[0]

    assert baseline.columns == reordered_selected.columns
    assert baseline.input_columns == binding.input_columns
    assert reordered_selected.input_columns == tuple(reversed(binding.input_columns))


def test_derived_duplicate_inputs_are_rejected() -> None:
    binding = _derived_binding()
    duplicated_inputs = (
        binding.input_columns[1],
        binding.input_columns[0],
        binding.input_columns[1],
    )
    duplicated = binding.model_copy(
        update={
            "columns": duplicated_inputs,
            "input_columns": duplicated_inputs,
        }
    )

    _assert_error(
        CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE,
        ("source-a",),
        lambda: _validate(_state(bindings=(duplicated,))),
    )


def _document_binding() -> DocumentRuleBinding:
    return DocumentRuleBinding(
        binding_id="binding-a",
        source_id="source-a",
        tables=(),
        columns=(),
        predicates=(),
        join_path=(),
        evidence_ids=("evidence-a",),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        document=DocumentRef(document_id="rules", namespace="main"),
        rule_id="priority-rule",
        rule_text="Priority comes from the approved rule.",
    )


def test_document_binding_has_no_relational_footprint() -> None:
    binding = _document_binding()
    assert _validate(_state(bindings=(binding,))).selected_bindings == (binding,)
    for forged in (
        binding.model_copy(update={"tables": (_table("hidden"),)}),
        binding.model_copy(update={"columns": (_column("hidden", "secret"),)}),
        binding.model_copy(update={"predicates": (_predicate("hidden", "secret", 1),)}),
    ):
        _assert_binding_rejected(forged)


def _join_state(
    required_path: tuple[JoinEdge, ...],
    candidate: JoinCandidate,
):
    base = _state()
    join_evidence = _schema_evidence(
        "join-evidence",
        required_path[0].left.table,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    binding = _physical_binding(
        "source-a",
        "binding-a",
        "evidence-a",
        join_path=required_path,
    )
    return _state(
        bindings=(binding,),
        evidence=(*base.evidence, join_evidence),
        joins=(candidate,),
    )


def _validated_join(
    join_id: str,
    path: tuple[JoinEdge, ...],
    *,
    left=None,
    right=None,
) -> JoinCandidate:
    return JoinCandidate(
        join_id=join_id,
        left=left or path[0].left,
        right=right or path[0].right,
        join_type=path[0].join_type,
        path=path,
        status=JoinCandidateStatus.VALIDATED,
        evidence_ids=("join-evidence",),
    )


def test_join_top_level_pair_and_type_must_match_direct_path() -> None:
    edge = JoinEdge(
        left=_column("orders", "customer_id"),
        right=_column("customers", "id"),
        join_type=JoinType.INNER,
    )
    valid = _validated_join("join-direct", (edge,))
    assert _validate(_join_state((edge,), valid)).eligible_validated_joins == (valid,)

    for forged in (
        valid.model_copy(update={"left": _column("orders", "id")}),
        valid.model_copy(update={"right": _column("customers", "other_id")}),
        valid.model_copy(update={"join_type": JoinType.LEFT}),
    ):
        _assert_error(
            CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
            ("source-a",),
            lambda forged=forged: _validate(_join_state((edge,), forged)),
        )


def test_composite_join_keeps_first_declared_pair_and_order() -> None:
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
    valid = _validated_join("join-composite", (first, second))
    assert _validate(_join_state((first, second), valid)).eligible_validated_joins == (
        valid,
    )
    forged = valid.model_copy(update={"left": second.left, "right": second.right})
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        ("source-a",),
        lambda: _validate(_join_state((first, second), forged)),
    )

    reversed_second = JoinEdge(
        left=second.right,
        right=second.left,
        join_type=JoinType.INNER,
    )
    inconsistent_path = (first, reversed_second)
    inconsistent = _validated_join(
        "join-inconsistent-orientation",
        inconsistent_path,
        left=first.left,
        right=reversed_second.right,
    )
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        ("source-a",),
        lambda: _validate(_join_state(inconsistent_path, inconsistent)),
    )


def test_multi_hop_join_requires_connected_chain_and_outer_endpoints() -> None:
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
    path = (first, second)
    valid = _validated_join(
        "join-multi-hop",
        path,
        left=first.left,
        right=second.right,
    )
    assert _validate(_join_state(path, valid)).eligible_validated_joins == (valid,)
    wrong_endpoint = valid.model_copy(update={"right": first.right})
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        ("source-a",),
        lambda: _validate(_join_state(path, wrong_endpoint)),
    )

    disconnected_second = JoinEdge(
        left=_column("sellers", "region_id"),
        right=_column("regions", "id"),
        join_type=JoinType.INNER,
    )
    disconnected_path = (first, disconnected_second)
    disconnected = _validated_join(
        "join-disconnected",
        disconnected_path,
        left=first.left,
        right=disconnected_second.right,
    )
    _assert_error(
        CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        ("source-a",),
        lambda: _validate(_join_state(disconnected_path, disconnected)),
    )
