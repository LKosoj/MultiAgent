"""Small typed builders for W5-04 semantic-check tests."""

from __future__ import annotations

from dataclasses import dataclass

from custom_tools.text_to_sql.adaptive.checks import SemanticCheckInput
from custom_tools.text_to_sql.adaptive._semantic_coverage_footprint import (
    canonical_binding,
)
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    BudgetState,
    ColumnRef,
    DiscriminatorValueBinding,
    ExpectedResultShape,
    JoinCandidate,
    JoinCandidateStatus,
    JoinEdge,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    QuerySpec,
    ResearchState,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    SqlCandidate,
    TableRef,
    VerticalAttributeBinding,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageRequirements,
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.semantic_plan import build_semantic_ast
from custom_tools.text_to_sql.adaptive.serialization import canonical_json_bytes
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from text_to_sql_semantic_coverage_helpers import (
    INCARNATION,
    RUN_ID,
    SCHEMA,
    _action,
    _context,
    _schema_evidence,
    _value_evidence,
)


POSTGRES_DSN = "postgresql://user:password@localhost:5432/example"


@dataclass(frozen=True, slots=True)
class ItemSpec:
    source_id: str
    kind: SemanticItemKind
    table: str
    column: str
    schema: str | None = None
    operator: PredicateOperator | None = None
    literal: object = None
    join_path: tuple[JoinEdge, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticCase:
    check_input: SemanticCheckInput
    query_spec: QuerySpec
    state: ResearchState
    requirements: CoverageRequirements


@dataclass(frozen=True, slots=True)
class VerticalCaseSpec:
    entity_table: str = "customers"
    entity_key: str = "id"
    catalog_table: str = "attributes"
    catalog_key: str = "id"
    catalog_name: str = "name"
    value_table: str = "attribute_values"
    value_entity_key: str = "customer_id"
    value_attribute_key: str = "attribute_id"
    value_column: str = "value"
    catalog_literal: str = "membership_level"
    value_literal: str = "premium"


def table(name: str, *, schema: str | None = None) -> TableRef:
    return TableRef(namespace="main", schema=schema, table=name)


def column(
    table_name: str,
    column_name: str,
    *,
    schema: str | None = None,
) -> ColumnRef:
    return ColumnRef(table=table(table_name, schema=schema), column=column_name)


def inner_join(
    left_table: str,
    left_column: str,
    right_table: str,
    right_column: str,
) -> JoinEdge:
    return JoinEdge(
        left=column(left_table, left_column),
        right=column(right_table, right_column),
    )


def build_case(
    sql: str,
    items: tuple[ItemSpec, ...],
    *,
    shape: ExpectedResultShape = ExpectedResultShape.ROWS,
    candidate_id: str = "candidate-1",
    dsn: str = POSTGRES_DSN,
) -> SemanticCase:
    state = build_state(items, shape=shape)
    requirements = validate_coverage_inputs(
        state,
        _context(),
        RUN_ID,
        INCARNATION,
    )
    parsed_ast = parse_sql_candidate(sql, dsn, candidate_id)
    candidate = SqlCandidate(
        candidate_id=candidate_id,
        sql=sql,
        normalized_ast_digest=parsed_ast.candidate_digest,
        revision=state.revision,
    )
    semantic_ast = build_semantic_ast(
        candidate,
        parsed_ast,
        state.query_spec,
        requirements,
        "main",
    )
    return SemanticCase(
        check_input=SemanticCheckInput(
            semantic_ast=semantic_ast,
            query_spec=state.query_spec,
            requirements=requirements,
        ),
        query_spec=state.query_spec,
        state=state,
        requirements=requirements,
    )


def build_vertical_case(
    sql: str,
    *,
    candidate_id: str = "vertical-candidate",
    dsn: str = POSTGRES_DSN,
    spec: VerticalCaseSpec | None = None,
) -> SemanticCase:
    state = build_vertical_state(spec)
    requirements = validate_coverage_inputs(
        state,
        _context(),
        RUN_ID,
        INCARNATION,
    )
    parsed_ast = parse_sql_candidate(sql, dsn, candidate_id)
    candidate = SqlCandidate(
        candidate_id=candidate_id,
        sql=sql,
        normalized_ast_digest=parsed_ast.candidate_digest,
        revision=state.revision,
    )
    semantic_ast = build_semantic_ast(
        candidate,
        parsed_ast,
        state.query_spec,
        requirements,
        "main",
    )
    return SemanticCase(
        check_input=SemanticCheckInput(
            semantic_ast=semantic_ast,
            query_spec=state.query_spec,
            requirements=requirements,
        ),
        query_spec=state.query_spec,
        state=state,
        requirements=requirements,
    )


def build_vertical_state(spec: VerticalCaseSpec | None = None) -> ResearchState:
    spec = spec or VerticalCaseSpec()
    entity_key = column(spec.entity_table, spec.entity_key)
    catalog_key = column(spec.catalog_table, spec.catalog_key)
    catalog_name = column(spec.catalog_table, spec.catalog_name)
    value_entity_key = column(spec.value_table, spec.value_entity_key)
    value_attribute_key = column(spec.value_table, spec.value_attribute_key)
    value_column = column(spec.value_table, spec.value_column)
    catalog_predicate = PredicateRef(
        left=catalog_name,
        operator=PredicateOperator.EQ,
        right=spec.catalog_literal,
    )
    value_predicate = PredicateRef(
        left=value_column,
        operator=PredicateOperator.EQ,
        right=spec.value_literal,
    )
    entity_join = JoinEdge(left=entity_key, right=value_entity_key)
    catalog_join = JoinEdge(left=catalog_key, right=value_attribute_key)
    columns = (
        entity_key,
        catalog_key,
        catalog_name,
        value_entity_key,
        value_attribute_key,
        value_column,
    )
    schema_evidence = tuple(
        _schema_evidence(f"evidence-vertical-schema-{index}", item)
        for index, item in enumerate(columns, start=1)
    )
    catalog_evidence = _value_evidence(
        "evidence-vertical-catalog",
        catalog_name,
        spec.catalog_literal,
    )
    value_evidence = _value_evidence(
        "evidence-vertical-value",
        value_column,
        spec.value_literal,
    )
    entity_join_evidence = _schema_evidence(
        "evidence-vertical-entity-join",
        entity_key,
    )
    catalog_join_evidence = _schema_evidence(
        "evidence-vertical-catalog-join",
        catalog_key,
    )
    evidence = (
        *schema_evidence,
        catalog_evidence,
        value_evidence,
        entity_join_evidence,
        catalog_join_evidence,
    )
    binding = canonical_binding(
        VerticalAttributeBinding(
            binding_id="binding-membership",
            source_id="membership",
            tables=(entity_key.table, catalog_key.table, value_column.table),
            columns=columns,
            predicates=(catalog_predicate, value_predicate),
            join_path=(entity_join, catalog_join),
            evidence_ids=tuple(item.evidence_id for item in evidence),
            confidence=1.0,
            status=BindingStatus.SUPPORTED,
            validator_rule="semantic-check-test",
            entity_table=entity_key.table,
            entity_key=entity_key,
            attribute_catalog_table=catalog_key.table,
            attribute_catalog_key=catalog_key,
            attribute_name_predicate=catalog_predicate,
            value_table=value_column.table,
            value_entity_key=value_entity_key,
            value_attribute_key=value_attribute_key,
            value_predicate=value_predicate,
        )
    )
    assert type(binding) is VerticalAttributeBinding
    joins = (
        JoinCandidate(
            join_id="join-vertical-catalog",
            left=catalog_join.left,
            right=catalog_join.right,
            join_type=catalog_join.join_type,
            path=(catalog_join,),
            status=JoinCandidateStatus.VALIDATED,
            evidence_ids=(catalog_join_evidence.evidence_id,),
        ),
        JoinCandidate(
            join_id="join-vertical-entity",
            left=entity_join.left,
            right=entity_join.right,
            join_type=entity_join.join_type,
            path=(entity_join,),
            status=JoinCandidateStatus.VALIDATED,
            evidence_ids=(entity_join_evidence.evidence_id,),
        ),
    )
    return ResearchState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=QuerySpec(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=SCHEMA,
            query_id="vertical-semantic-query",
            original_text="membership",
            semantic_items=(
                SemanticItem(
                    source_id="membership",
                    kind=SemanticItemKind.FILTER,
                    source_text="membership",
                    normalized_meaning=f"{spec.value_literal} membership",
                    required=True,
                    operator=PredicateOperator.EQ,
                    literal_or_reference=spec.value_literal,
                    status=SemanticItemStatus.RESOLVED,
                    binding_ids=(binding.binding_id,),
                ),
            ),
            expected_result_shape=ExpectedResultShape.ROWS,
            global_constraints=(),
        ),
        hypotheses=(),
        evidence=evidence,
        bindings=(binding,),
        join_candidates=joins,
        unresolved_items=(),
        action_history=(_action((("revision", 0),), index=0),),
        result_expectations=(),
        budget_state=_budget(),
        stop_reason=None,
    )


def build_state(
    items: tuple[ItemSpec, ...],
    *,
    shape: ExpectedResultShape = ExpectedResultShape.ROWS,
) -> ResearchState:
    original_text = " ".join(item.source_id for item in items) or "query"
    semantic_items: list[SemanticItem] = []
    bindings: list[PhysicalColumnBinding | DiscriminatorValueBinding] = []
    evidence = []
    for item in items:
        binding_id = f"binding-{item.source_id}"
        schema_evidence_id = f"evidence-{item.source_id}-schema"
        physical_column = column(item.table, item.column, schema=item.schema)
        semantic_items.append(
            SemanticItem(
                source_id=item.source_id,
                kind=item.kind,
                source_text=item.source_id,
                normalized_meaning=item.source_id,
                required=True,
                operator=item.operator,
                literal_or_reference=item.literal,
                status=SemanticItemStatus.RESOLVED,
                binding_ids=(binding_id,),
            )
        )
        if item.kind is SemanticItemKind.FILTER:
            if item.operator is None:
                raise ValueError("filter ItemSpec requires an operator")
            value_evidence_id = f"evidence-{item.source_id}-value"
            predicate = PredicateRef(
                left=physical_column,
                operator=item.operator,
                right=item.literal,
            )
            bindings.append(
                DiscriminatorValueBinding(
                    binding_id=binding_id,
                    source_id=item.source_id,
                    tables=(physical_column.table,),
                    columns=(physical_column,),
                    predicates=(predicate,),
                    join_path=item.join_path,
                    evidence_ids=tuple(sorted((schema_evidence_id, value_evidence_id))),
                    confidence=1.0,
                    status=BindingStatus.SUPPORTED,
                    validator_rule="semantic-check-test",
                    discriminator_column=physical_column,
                    discriminator_predicate=predicate,
                )
            )
            evidence.append(
                _value_evidence(value_evidence_id, physical_column, item.literal)
            )
        else:
            bindings.append(
                PhysicalColumnBinding(
                    binding_id=binding_id,
                    source_id=item.source_id,
                    tables=(physical_column.table,),
                    columns=(physical_column,),
                    predicates=(),
                    join_path=item.join_path,
                    evidence_ids=(schema_evidence_id,),
                    confidence=1.0,
                    status=BindingStatus.SUPPORTED,
                    validator_rule="semantic-check-test",
                    physical_column=physical_column,
                )
            )
        evidence.append(_schema_evidence(schema_evidence_id, physical_column))

    unique_paths = {
        canonical_json_bytes(item.join_path): item.join_path
        for item in items
        if item.join_path
    }
    joins: list[JoinCandidate] = []
    for ordinal, key in enumerate(sorted(unique_paths), start=1):
        path = unique_paths[key]
        evidence_id = f"evidence-join-{ordinal}"
        evidence.append(_schema_evidence(evidence_id, path[0].left))
        joins.append(
            JoinCandidate(
                join_id=f"join-{ordinal}",
                left=path[0].left,
                right=path[-1].right,
                join_type=path[0].join_type,
                path=path,
                status=JoinCandidateStatus.VALIDATED,
                evidence_ids=(evidence_id,),
            )
        )

    return ResearchState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=QuerySpec(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=SCHEMA,
            query_id="semantic-query",
            original_text=original_text,
            semantic_items=tuple(semantic_items),
            expected_result_shape=shape,
            global_constraints=(),
        ),
        hypotheses=(),
        evidence=tuple(evidence),
        bindings=tuple(bindings),
        join_candidates=tuple(joins),
        unresolved_items=(),
        action_history=(_action((("revision", 0),), index=0),),
        result_expectations=(),
        budget_state=_budget(),
        stop_reason=None,
    )


def _budget() -> BudgetState:
    values: dict[str, int] = {}
    for name in (
        "wall_clock_ms",
        "model_calls",
        "model_tokens",
        "db_probe_ms",
        "rows",
        "bytes",
    ):
        values[f"initial_{name}"] = 1
        values[f"used_{name}"] = 0
        values[f"remaining_{name}"] = 1
    return BudgetState(**values)
