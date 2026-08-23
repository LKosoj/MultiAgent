"""Closed post-execution findings derived from certified research evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import model_validator

from ._semantic_coverage_boundary import evidence_has_state_authority
from ._semantic_matching import predicate_matches
from ._sql_ast_identity import semantic_candidate_digest, source_sql_digest
from ._sql_ast_models import ParsedSqlCandidate, PredicateLocation, QueryRole
from .freshness import FreshnessContext, FreshnessStatus, evaluate_evidence_freshness
from .models import (
    Binding,
    ColumnRef,
    DiscriminatorValueBinding,
    Digest,
    EvidenceRecord,
    Id,
    NonNegativeInt,
    PhysicalColumnBinding,
    PredicateRef,
    ResearchAction,
    ResearchState,
    ResultExpectation,
    ResultExpectationKind,
    SqlCandidate,
    StrictModel,
    VerticalAttributeBinding,
)
from .result_expectations import (
    ResultExpectationCertificateError,
    derive_result_expectations,
)
from .semantic_plan import predicate_from_expression
from ._semantic_coverage_footprint import normalized_freshness_digest
from .semantic_coverage import CoverageRequirements
from .serialization import CanonicalJsonError, canonical_json_bytes


RESULT_VALIDATION_RUNTIME_KEY = "text_to_sql_result_validation"
_RESULT_VALIDATION_CAPABILITY_MARKER = object()


class ResultContradictionFinding(StrictModel):
    expectation: ResultExpectation
    ast_node_id: Id
    output_index: NonNegativeInt | None

    @model_validator(mode="after")
    def validate_output_address(self) -> ResultContradictionFinding:
        is_filter = self.expectation.kind is ResultExpectationKind.FILTER_MATCH_ABSENT
        if (self.output_index is None) != is_filter:
            raise ValueError("result finding output address contradicts expectation")
        return self


class ResultContradictionReceipt(StrictModel):
    record_kind: Literal["text2sql_result_contradiction"] = (
        "text2sql_result_contradiction"
    )
    run_id: Id
    run_incarnation: Id
    research_state_revision: NonNegativeInt
    candidate_id: Id
    normalized_ast_digest: Digest
    requirements_digest: Digest
    finding: ResultContradictionFinding
    execution: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ResultValidationCapability:
    _marker: object = field(repr=False, compare=False)
    state: ResearchState
    requirements: CoverageRequirements
    freshness_context: FreshnessContext
    candidate: SqlCandidate
    parsed_ast: ParsedSqlCandidate


def create_result_validation_capability(
    *,
    state: ResearchState,
    requirements: CoverageRequirements,
    freshness_context: FreshnessContext,
    candidate: SqlCandidate,
    parsed_ast: ParsedSqlCandidate,
) -> object:
    """Create the opaque post-execution validator for one checked candidate."""

    state, requirements, freshness_context, candidate, parsed_ast = (
        _validated_result_validation_inputs(
            state=state,
            requirements=requirements,
            freshness_context=freshness_context,
            candidate=candidate,
            parsed_ast=parsed_ast,
        )
    )
    return _ResultValidationCapability(
        _marker=_RESULT_VALIDATION_CAPABILITY_MARKER,
        state=state,
        requirements=requirements,
        freshness_context=freshness_context,
        candidate=candidate,
        parsed_ast=parsed_ast,
    )


def evaluate_result_validation_capability(
    value: object,
    *,
    expected_run_id: str,
    expected_sql: str,
    execution: object,
) -> ResultContradictionReceipt | None:
    """Return one authenticated contradiction, or fail closed for bad input."""

    if (
        type(value) is not _ResultValidationCapability
        or value._marker is not _RESULT_VALIDATION_CAPABILITY_MARKER
        or type(expected_run_id) is not str
        or not expected_run_id
        or type(expected_sql) is not str
        or not expected_sql.strip()
    ):
        raise TypeError("result validation capability inputs are invalid")
    state, requirements, freshness_context, candidate, parsed_ast = (
        _validated_result_validation_inputs(
            state=value.state,
            requirements=value.requirements,
            freshness_context=value.freshness_context,
            candidate=value.candidate,
            parsed_ast=value.parsed_ast,
        )
    )
    if state.run_id != expected_run_id or candidate.sql != expected_sql:
        raise ValueError("result validation capability identity does not match finalizer")
    canonical_execution = _validated_executed_result(execution)
    finding = validate_execution_result_expectations(
        state=state,
        requirements=requirements,
        freshness_context=freshness_context,
        candidate=candidate,
        parsed_ast=parsed_ast,
        columns=canonical_execution["columns"],
        data=canonical_execution["data"],
    )
    if finding is None:
        return None
    return ResultContradictionReceipt(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        research_state_revision=state.revision,
        candidate_id=candidate.candidate_id,
        normalized_ast_digest=candidate.normalized_ast_digest,
        requirements_digest=requirements.requirements_digest,
        finding=finding,
        execution=canonical_execution,
    )


def _validated_executed_result(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError("execution must be an exact dictionary")
    try:
        normalized = json.loads(canonical_json_bytes(value))
    except (CanonicalJsonError, TypeError, ValueError) as exc:
        raise ValueError("execution must be canonical JSON") from exc
    if (
        type(normalized) is not dict
        or normalized != value
        or value.get("success") is not True
        or value.get("dry_run_only") is not False
        or value.get("skipped_execution") is not False
        or type(value.get("columns")) is not list
        or not all(type(column) is str for column in value["columns"])
        or type(value.get("data")) is not list
    ):
        raise ValueError("execution is not a successful executed result")
    return normalized


def _validated_result_validation_inputs(
    *,
    state: object,
    requirements: object,
    freshness_context: object,
    candidate: object,
    parsed_ast: object,
) -> tuple[
    ResearchState,
    CoverageRequirements,
    FreshnessContext,
    SqlCandidate,
    ParsedSqlCandidate,
]:
    state = _revalidate(state, ResearchState, "state")
    requirements = _revalidate(requirements, CoverageRequirements, "requirements")
    freshness_context = _revalidate(
        freshness_context, FreshnessContext, "freshness_context"
    )
    candidate = _revalidate(candidate, SqlCandidate, "candidate")
    if type(parsed_ast) is not ParsedSqlCandidate:
        raise TypeError("parsed_ast must be ParsedSqlCandidate")
    if not _requirements_match_persisted_freshness(requirements, freshness_context):
        raise ValueError("requirements do not match persisted research authority")
    if (
        candidate.revision != requirements.state_revision
        or parsed_ast.candidate_id != candidate.candidate_id
        or parsed_ast.source_sql_digest != source_sql_digest(candidate.sql)
        or parsed_ast.candidate_digest != semantic_candidate_digest(parsed_ast)
        or candidate.normalized_ast_digest != parsed_ast.candidate_digest
    ):
        raise ValueError("candidate and parsed AST identities contradict each other")
    return state, requirements, freshness_context, candidate, parsed_ast


def _requirements_match_persisted_freshness(
    requirements: CoverageRequirements,
    freshness_context: FreshnessContext,
) -> bool:
    """Bind post-execution checks to their persisted research authority."""

    return requirements.freshness_digest == normalized_freshness_digest(
        freshness_context
    )


def validate_execution_result_expectations(
    *,
    state: ResearchState,
    requirements: CoverageRequirements,
    freshness_context: FreshnessContext,
    candidate: SqlCandidate,
    parsed_ast: ParsedSqlCandidate,
    columns: list[str],
    data: list[object],
) -> ResultContradictionFinding | None:
    """Return one proven post-execution contradiction, if present."""

    state, requirements, freshness_context, candidate, parsed_ast = (
        _validated_result_validation_inputs(
            state=state,
            requirements=requirements,
            freshness_context=freshness_context,
            candidate=candidate,
            parsed_ast=parsed_ast,
        )
    )
    if type(columns) is not list or not all(type(column) is str for column in columns):
        raise TypeError("columns must be a list of strings")
    if type(data) is not list:
        raise TypeError("data must be a list")

    for expectation in state.result_expectations:
        applicable = _applicable_expectation(
            state,
            requirements,
            freshness_context,
            expectation,
        )
        if not applicable:
            continue
        for binding, _, _ in applicable:
            if expectation.kind is ResultExpectationKind.FILTER_MATCH_ABSENT:
                finding = _filter_absence_finding(
                    expectation,
                    binding,
                    parsed_ast,
                    columns,
                    data,
                )
            else:
                finding = _direct_output_finding(
                    expectation,
                    binding,
                    parsed_ast,
                    columns,
                    data,
                )
            if finding is not None:
                return finding
    return None


def _revalidate(value: object, model: type[StrictModel], label: str):
    if type(value) is not model:
        raise TypeError(f"{label} must be exact {model.__name__}")
    return model.model_validate(value.model_dump(mode="python", round_trip=True))


def _root_projections(parsed_ast: ParsedSqlCandidate, columns: list[str]):
    if (
        parsed_ast.joins
        or parsed_ast.ctes
        or parsed_ast.derived_relations
        or parsed_ast.expression_relations
        or parsed_ast.subquery_refs
        or parsed_ast.set_operations
        or parsed_ast.aggregates
        or parsed_ast.groupings
    ):
        return None
    roots = tuple(scope for scope in parsed_ast.scopes if scope.query_role is QueryRole.ROOT)
    if len(roots) != 1 or len(parsed_ast.scopes) != 1:
        return None
    projections = tuple(
        item for item in parsed_ast.projections if item.scope_id == roots[0].scope_id
    )
    return projections if len(projections) == len(columns) else None


def _applicable_expectation(
    state: ResearchState,
    requirements: CoverageRequirements,
    freshness_context: FreshnessContext,
    expectation: ResultExpectation,
) -> tuple[tuple[Binding, EvidenceRecord, ResearchAction], ...]:
    matches = tuple(
        item
        for item in requirements.selected_bindings
        if item.source_id == expectation.source_id
    )
    evidence = _evidence(state, expectation.evidence_id)
    action = _action(state, evidence.action_digest) if evidence is not None else None
    if (
        evidence is None
        or action is None
        or not evidence_has_state_authority(evidence, state)
        or evaluate_evidence_freshness(evidence, freshness_context).status
        is not FreshnessStatus.FRESH
        or _is_superseded(state, evidence, action)
    ):
        return ()
    try:
        derived = derive_result_expectations(state, action, evidence)
    except ResultExpectationCertificateError:
        return ()
    if expectation not in derived:
        return ()
    return tuple((binding, evidence, action) for binding in matches)


def _is_superseded(
    state: ResearchState,
    evidence: EvidenceRecord,
    action: ResearchAction,
) -> bool:
    for newer_evidence in state.evidence:
        newer_action = _action(state, newer_evidence.action_digest)
        if (
            newer_action is not None
            and newer_evidence.revision > evidence.revision
            and newer_action.expected_revision > action.expected_revision
            and newer_evidence.target == newer_action.target
            and evidence_has_state_authority(newer_evidence, state)
            and _same_probe_signature(newer_action, action)
        ):
            try:
                derive_result_expectations(state, newer_action, newer_evidence)
            except ResultExpectationCertificateError:
                continue
            return True
    return False


def _same_probe_signature(left: ResearchAction, right: ResearchAction) -> bool:
    return (
        left.kind == right.kind
        and left.target == right.target
        and left.parameters == right.parameters
    )


def _evidence(state: ResearchState, evidence_id: str):
    matches = tuple(item for item in state.evidence if item.evidence_id == evidence_id)
    if len(matches) > 1:
        raise ValueError("result expectation evidence is not unique")
    return matches[0] if matches else None


def _action(state: ResearchState, action_digest: str):
    matches = tuple(item for item in state.action_history if item.action_digest == action_digest)
    if len(matches) > 1:
        raise ValueError("result expectation action is not unique")
    return matches[0] if matches else None


def _direct_projection_column(expression, parsed_ast: ParsedSqlCandidate, expected: ColumnRef):
    if expression.kind != "column" or expression.children:
        return None
    attributes = dict(expression.attributes)
    if len(attributes) != len(expression.attributes) or attributes.get("name") != expected.column:
        return None
    relation_id = attributes.get("relation_id")
    scans = tuple(parsed_ast.table_scans)
    if relation_id is None and len(scans) == 1:
        scan = scans[0]
    else:
        scan = next((item for item in scans if item.relation_id == relation_id), None)
    if scan is None or (
        scan.table.schema != expected.table.schema_name
        or scan.table.name != expected.table.table
    ):
        return None
    return expected


def _filter_absence_finding(
    expectation: ResultExpectation,
    binding: object,
    parsed_ast: ParsedSqlCandidate,
    columns: list[str],
    data: list[object],
) -> ResultContradictionFinding | None:
    predicates = _filter_binding_predicates(binding, expectation.column)
    shape = _filter_shape(parsed_ast, columns, binding)
    if predicates is None or shape is None or not columns:
        return None
    required_predicates, result_predicate = predicates
    where_fact, relation_tables = shape
    typed_atoms = tuple(
        (atom, actual)
        for atom in where_fact.atoms
        if (
            actual := predicate_from_expression(
                atom.expression, relation_tables, scope_id=where_fact.scope_id
            )
        )
        is not None
    )
    if any(
        not any(predicate_matches(required, actual) for _, actual in typed_atoms)
        for required in required_predicates
    ):
        return None
    matching_atoms = tuple(
        atom
        for atom, actual in typed_atoms
        if predicate_matches(result_predicate, actual)
    )
    if len(matching_atoms) != 1:
        return None
    values = _output_values(data, columns, 0)
    if values is None or not values:
        return None
    return ResultContradictionFinding(
        expectation=expectation,
        ast_node_id=matching_atoms[0].node_id,
        output_index=None,
    )


def _filter_binding_predicates(
    binding: object,
    column: ColumnRef,
) -> tuple[tuple[PredicateRef, ...], PredicateRef] | None:
    if type(binding) is DiscriminatorValueBinding:
        predicate = binding.discriminator_predicate
        required = (predicate,)
    elif type(binding) is VerticalAttributeBinding:
        predicate = binding.value_predicate
        required = (binding.attribute_name_predicate, predicate)
    else:
        return None
    return (required, predicate) if predicate.left == column else None


def _filter_shape(
    parsed_ast: ParsedSqlCandidate,
    columns: list[str],
    binding: object,
):
    if (
        parsed_ast.ctes
        or parsed_ast.derived_relations
        or parsed_ast.expression_relations
        or parsed_ast.subquery_refs
        or parsed_ast.set_operations
        or parsed_ast.aggregates
        or parsed_ast.groupings
    ):
        return None
    roots = tuple(scope for scope in parsed_ast.scopes if scope.query_role is QueryRole.ROOT)
    if len(roots) != 1 or len(parsed_ast.scopes) != 1:
        return None
    projections = tuple(
        projection
        for projection in parsed_ast.projections
        if projection.scope_id == roots[0].scope_id
    )
    if len(projections) != len(columns):
        return None
    relation_tables = _binding_relation_tables(parsed_ast, binding)
    if relation_tables is None:
        return None
    where_facts = tuple(
        fact
        for fact in parsed_ast.predicates
        if fact.scope_id == roots[0].scope_id and fact.location is PredicateLocation.WHERE
    )
    if len(where_facts) != 1:
        return None
    if type(binding) is DiscriminatorValueBinding:
        if len(binding.tables) != 1 or parsed_ast.joins:
            return None
    elif type(binding) is VerticalAttributeBinding:
        if not _vertical_join_path_matches(parsed_ast, relation_tables, binding):
            return None
    else:
        return None
    join_facts = tuple(
        fact
        for fact in parsed_ast.predicates
        if fact.scope_id == roots[0].scope_id and fact.location is PredicateLocation.JOIN_ON
    )
    if (
        len(parsed_ast.predicates) != len(join_facts) + 1
        or len(join_facts) != len(parsed_ast.joins)
        or {fact.owner_node_id for fact in join_facts}
        != {join.node_id for join in parsed_ast.joins}
        or any(
            fact.expression
            != next(
                join.condition
                for join in parsed_ast.joins
                if join.node_id == fact.owner_node_id
            )
            for fact in join_facts
        )
    ):
        return None
    return where_facts[0], relation_tables


def _binding_relation_tables(
    parsed_ast: ParsedSqlCandidate,
    binding: object,
):
    if type(binding) not in {DiscriminatorValueBinding, VerticalAttributeBinding}:
        return None
    if len(parsed_ast.table_scans) != len(binding.tables):
        return None
    matches: list[object] = []
    relation_tables = {}
    for scan in parsed_ast.table_scans:
        candidates = tuple(
            table
            for table in binding.tables
            if table.schema_name == scan.table.schema and table.table == scan.table.name
        )
        if len(candidates) != 1:
            return None
        matches.append(candidates[0])
        relation_tables[scan.relation_id] = candidates[0]
    if any(matches.count(table) != 1 for table in binding.tables):
        return None
    return relation_tables


def _vertical_join_path_matches(
    parsed_ast: ParsedSqlCandidate,
    relation_tables,
    binding: VerticalAttributeBinding,
) -> bool:
    if len(parsed_ast.joins) != len(binding.join_path):
        return False
    actual_endpoints: list[tuple[ColumnRef, ColumnRef]] = []
    for join in parsed_ast.joins:
        predicate = predicate_from_expression(
            join.condition,
            relation_tables,
            scope_id=join.scope_id,
        )
        if (
            predicate is None
            or type(predicate.left) is not ColumnRef
            or type(predicate.right) is not ColumnRef
        ):
            return False
        actual_endpoints.append((predicate.left, predicate.right))
    required = list(binding.join_path)
    for left, right in actual_endpoints:
        for index, edge in enumerate(required):
            if (edge.left == left and edge.right == right) or (
                edge.left == right and edge.right == left
            ):
                required.pop(index)
                break
        else:
            return False
    return not required


def _direct_output_finding(
    expectation: ResultExpectation,
    binding: object,
    parsed_ast: ParsedSqlCandidate,
    columns: list[str],
    data: list[object],
) -> ResultContradictionFinding | None:
    if type(binding) is not PhysicalColumnBinding or binding.physical_column != expectation.column:
        return None
    root_projections = _root_projections(parsed_ast, columns)
    if root_projections is None:
        return None
    matched = tuple(
        (index, projection)
        for index, projection in enumerate(root_projections)
        if _direct_projection_column(projection.expression, parsed_ast, expectation.column)
        == expectation.column
    )
    if len(matched) != 1:
        return None
    index, projection = matched[0]
    values = _output_values(data, columns, index)
    if values is None:
        return None
    if expectation.kind is ResultExpectationKind.DIRECT_OUTPUT_NOT_NULL:
        contradiction = any(value is None for value in values)
    elif expectation.kind is ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN:
        contradiction = _has_outside_domain(values, expectation.allowed_values)
    elif expectation.kind is ResultExpectationKind.DIRECT_OUTPUT_PRIMARY_KEY_UNIQUE:
        if len(parsed_ast.table_scans) != 1:
            return None
        contradiction = _has_duplicate_non_null_value(values)
    else:
        return None
    if not contradiction:
        return None
    return ResultContradictionFinding(
        expectation=expectation,
        ast_node_id=projection.node_id,
        output_index=index,
    )


def _output_values(
    data: list[object],
    columns: list[str],
    index: int,
) -> tuple[object, ...] | None:
    if index >= len(columns):
        return None
    name = columns[index]
    if any(column == name for column in columns[:index] + columns[index + 1 :]):
        return None
    values: list[object] = []
    for row in data:
        if type(row) is list:
            if index >= len(row):
                raise ValueError("result row is shorter than columns")
            values.append(row[index])
        elif type(row) is dict:
            if name not in row:
                raise ValueError("result row does not contain output column")
            values.append(row[name])
        else:
            raise TypeError("result row must be a list or dict")
    return tuple(values)


def _has_outside_domain(
    values: tuple[object, ...],
    allowed_values: tuple[object, ...],
) -> bool:
    allowed = {canonical_json_bytes(value) for value in allowed_values}
    for value in values:
        encoded = _canonical_non_null_scalar(value)
        if encoded is not None and encoded not in allowed:
            return True
    return False


def _canonical_non_null_scalar(value: object) -> bytes | None:
    if value is None or type(value) not in {str, int, float, bool}:
        return None
    try:
        return canonical_json_bytes(value)
    except CanonicalJsonError:
        return None


def _has_duplicate_non_null_value(values: tuple[object, ...]) -> bool:
    seen: set[bytes] = set()
    for value in values:
        if value is None:
            continue
        try:
            encoded = canonical_json_bytes(value)
        except CanonicalJsonError:
            continue
        if encoded in seen:
            return True
        seen.add(encoded)
    return False


__all__ = [
    "RESULT_VALIDATION_RUNTIME_KEY",
    "ResultContradictionFinding",
    "ResultContradictionReceipt",
    "create_result_validation_capability",
    "evaluate_result_validation_capability",
    "validate_execution_result_expectations",
]
