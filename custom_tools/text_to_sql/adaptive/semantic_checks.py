"""Pure semantic checks over every fact in the parsed sqlglot AST."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from ..dialects import get_sqlglot_dialect
from ._semantic_matching import predicate_matches
from ._semantic_value_certificate import predicate_has_exact_value_certificate
from ._semantic_coverage_footprint import (
    canonical_binding,
    canonical_join,
    model_payload,
    normalized_state_digest,
)
from ._sql_ast_models import PredicateLocation
from .checks import SemanticCheckInput, require_authenticated_semantic_input
from .models import (
    BindingStatus,
    CheckFailureCode,
    CheckKind,
    CheckRepair,
    CheckResult,
    CheckStatus,
    ColumnRef,
    DerivedExpressionBinding,
    EvidenceSourceKind,
    JoinCandidateStatus,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    QuerySpec,
    RepairKind,
    ResearchState,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    TableRef,
    VerticalAttributeBinding,
    is_binding_free_semantic_item,
)
from .semantic_coverage import CoverageRequirements
from .semantic_plan import (
    AstColumnOccurrence,
    _relation_tables,
    _resolve_column_ref,
    collect_ast_columns,
    predicate_from_expression,
)


_AUTHORITY_FAILURE_ORDER = (
    CheckFailureCode.UNAUTHORIZED_TABLE,
    CheckFailureCode.UNAUTHORIZED_COLUMN,
    CheckFailureCode.UNAUTHORIZED_JOIN,
    CheckFailureCode.UNAUTHORIZED_LITERAL,
)


@dataclass(frozen=True, slots=True)
class _PredicateOccurrence:
    node_id: str
    scope_id: str
    relation_scope_ids: tuple[str, ...]
    expression_field: str
    expression_index: int
    expression_path: tuple[tuple[str, int], ...]
    unordered_between: bool
    predicate: PredicateRef


@dataclass(frozen=True, slots=True)
class _SemanticContext:
    check_input: SemanticCheckInput
    query_spec: QuerySpec
    state: ResearchState
    requirements: CoverageRequirements
    bindings_by_source: dict[str, tuple[object, ...]]
    items_by_kind: dict[SemanticItemKind, tuple[SemanticItem, ...]]
    tables: tuple[tuple[str, TableRef], ...]
    columns: tuple[AstColumnOccurrence, ...]
    relation_tables: dict[str, TableRef]
    joins: tuple[object, ...]
    root_scope_ids: frozenset[str]
    authority_predicates: tuple[_PredicateOccurrence, ...]
    expression_relation_node_ids: frozenset[str]


class _SemanticInputError(ValueError):
    def __init__(self, ast_node_ids: tuple[str, ...] = ()) -> None:
        super().__init__(CheckFailureCode.CHECK_INPUT_INVALID.value)
        self.ast_node_ids = tuple(sorted(set(ast_node_ids)))


def evaluate_semantic_authority_checks(
    check_input: SemanticCheckInput,
    research_state: ResearchState,
    dsn: str,
) -> CheckResult:
    """Block only SQL facts outside the authenticated research authority."""

    candidate_id = _safe_candidate_id(check_input)
    try:
        context = _validated_context(check_input, research_state, dsn)
        checks = {
            CheckFailureCode.UNAUTHORIZED_TABLE: _unauthorized_table,
            CheckFailureCode.UNAUTHORIZED_COLUMN: _unauthorized_column,
            CheckFailureCode.UNAUTHORIZED_JOIN: _unauthorized_join,
            CheckFailureCode.UNAUTHORIZED_LITERAL: _unauthorized_authority_literal,
        }
        for code in _AUTHORITY_FAILURE_ORDER:
            if (result := checks[code](context)) is not None:
                return result
        return _passed(candidate_id)
    except _SemanticInputError as exc:
        return _inconclusive(candidate_id, exc.ast_node_ids)
    except Exception:
        return _inconclusive(candidate_id)


def _validated_context(
    check_input: SemanticCheckInput,
    state: ResearchState,
    dsn: str,
) -> _SemanticContext:
    if type(check_input) is not SemanticCheckInput or type(state) is not ResearchState:
        raise TypeError("semantic check inputs require exact contract types")
    check_input = require_authenticated_semantic_input(check_input)
    if type(dsn) is not str or not dsn:
        raise TypeError("semantic checks require an explicit DSN")
    try:
        if get_sqlglot_dialect(dsn, strict=True) != check_input.parsed_ast.dialect:
            raise _SemanticInputError()
    except _SemanticInputError:
        raise
    except Exception as exc:
        raise _SemanticInputError() from exc
    try:
        state = ResearchState.model_validate(model_payload(state))
    except (ValidationError, TypeError, ValueError) as exc:
        raise _SemanticInputError() from exc
    query_spec = check_input.query_spec
    requirements = check_input.requirements
    ast = check_input.semantic_ast
    required_items = tuple(item for item in query_spec.semantic_items if item.required)
    required_source_ids = tuple(sorted(item.source_id for item in required_items))
    if (
        query_spec != state.query_spec
        or requirements.required_source_ids != required_source_ids
        or requirements.state_revision != state.revision
        or requirements.schema_namespace_version != state.schema_namespace_version
        or requirements.expected_result_shape is not query_spec.expected_result_shape
        or requirements.state_digest != normalized_state_digest(state)
        or ast.coverage.requirements_digest != requirements.requirements_digest
        or ast.coverage.required_source_ids != requirements.required_source_ids
    ):
        raise _SemanticInputError()
    _validate_requirements_state_membership(required_items, requirements, state)
    binding_required_source_ids = {
        item.source_id
        for item in required_items
        if not is_binding_free_semantic_item(item)
    }
    bindings = {
        source_id: tuple(
            binding
            for binding in requirements.selected_bindings
            if binding.source_id == source_id
        )
        for source_id in binding_required_source_ids
    }
    if set(bindings) != binding_required_source_ids:
        raise _SemanticInputError()
    reachable_scope_ids = _reachable_scope_ids(ast.parsed_ast)
    authority_tables = tuple(
        dict.fromkeys(
            (
                *requirements.allowed_tables,
                *(
                    table
                    for path in requirements.allowed_join_paths
                    for edge in path
                    for table in (edge.left.table, edge.right.table)
                ),
            )
        )
    )
    resolved_tables = _relation_tables(
        ast.parsed_ast,
        ast.table_namespace,
        authority_tables,
        ast.parsed_ast.dialect,
    )
    relation_tables = {
        scan.relation_id: resolved_tables[scan.relation_id]
        for scan in ast.parsed_ast.table_scans
        if scan.scope_id in reachable_scope_ids
    }
    active_ast = _active_ast(ast.parsed_ast, reachable_scope_ids)
    authority_columns = tuple(
        dict.fromkeys(
            (
                *requirements.allowed_columns,
                *(
                    column
                    for join in requirements.eligible_validated_joins
                    for edge in join.path
                    for column in (edge.left, edge.right)
                ),
            )
        )
    )
    authority_predicates = tuple(
        occurrence
        for node_id, scope_id, field, index, expression in _all_active_expressions(active_ast)
        for occurrence in _comparison_occurrences(
            node_id,
            scope_id,
            field,
            index,
            expression,
            relation_tables,
            authority_columns,
            ast.parsed_ast.dialect,
            {
                scan.relation_id: scan.scope_id
                for scan in active_ast.table_scans
            },
        )
    )
    columns = collect_ast_columns(
        active_ast,
        relation_tables,
        allowed_columns=authority_columns,
        dialect=ast.parsed_ast.dialect,
    )
    return _SemanticContext(
        check_input=check_input,
        query_spec=query_spec,
        state=state,
        requirements=requirements,
        bindings_by_source=bindings,
        items_by_kind={
            kind: tuple(item for item in required_items if item.kind is kind)
            for kind in SemanticItemKind
        },
        tables=tuple(
            (fact.node_id, relation_tables[fact.relation_id])
            for fact in active_ast.table_scans
        ),
        columns=columns.occurrences,
        relation_tables=relation_tables,
        joins=tuple(active_ast.joins),
        root_scope_ids=frozenset(
            scope.scope_id
            for scope in active_ast.scopes
            if scope.parent_scope_id is None and scope.query_role.value == "root"
        ),
        authority_predicates=authority_predicates,
        expression_relation_node_ids=frozenset(
            fact.node_id for fact in active_ast.expression_relations
        ),
    )


def _validate_requirements_state_membership(
    items: tuple[SemanticItem, ...],
    requirements: CoverageRequirements,
    state: ResearchState,
) -> None:
    canonical_state_bindings = tuple(
        canonical_binding(binding) for binding in state.bindings
    )
    selected = {
        item.source_id: tuple(
            binding
            for binding in requirements.selected_bindings
            if binding.source_id == item.source_id
        )
        for item in items
        if not is_binding_free_semantic_item(item)
    }
    for item in items:
        if is_binding_free_semantic_item(item):
            continue
        supported = tuple(
            sorted(
                (
                    binding
                    for binding in canonical_state_bindings
                    if binding.source_id == item.source_id
                    and binding.status is BindingStatus.SUPPORTED
                ),
                key=lambda binding: binding.binding_id,
            )
        )
        if (
            tuple(binding.binding_id for binding in supported) != item.binding_ids
            or selected.get(item.source_id) != supported
        ):
            raise _SemanticInputError()
    known_joins = {
        join.join_id: canonical_join(join)
        for join in state.join_candidates
        if join.status is JoinCandidateStatus.VALIDATED
    }
    if any(known_joins.get(item.join_id) != item for item in requirements.eligible_validated_joins):
        raise _SemanticInputError()
    from .semantic_coverage import _derive_row_preservation_requirements

    expected_row_requirements = _derive_row_preservation_requirements(
        state.query_spec,
        items,
        requirements.selected_bindings,
        requirements.eligible_validated_joins,
    )
    if requirements.row_preservation_requirements != expected_row_requirements:
        raise _SemanticInputError()


def _unauthorized_table(context: _SemanticContext) -> CheckResult | None:
    allowed = set(context.requirements.allowed_tables)
    for path in context.requirements.allowed_join_paths:
        for edge in path:
            allowed.update((edge.left.table, edge.right.table))
    nodes = tuple(node_id for node_id, table in context.tables if table not in allowed)
    return _failure(context, CheckFailureCode.UNAUTHORIZED_TABLE, nodes=nodes) if nodes else None


def _unauthorized_column(context: _SemanticContext) -> CheckResult | None:
    # Scoped to columns smuggled through a derived (expression) relation, e.g.
    # ``UNNEST(ARRAY[o.secret]) AS u(value)`` re-exposing ``o.secret`` as
    # ``u.value``. Plain physical-column references elsewhere in the AST
    # (aggregates, projections, ...) are intentionally not blanket-checked
    # here: schema validation owns unselected-but-existing column references,
    # and requested-output/discriminator/vertical checks already cover their
    # own authority-sensitive column usages precisely.
    allowed = set(context.requirements.allowed_columns)
    for path in context.requirements.allowed_join_paths:
        for edge in path:
            allowed.update((edge.left, edge.right))
    nodes = tuple(
        item.node_id
        for item in context.columns
        if item.node_id in context.expression_relation_node_ids
        and item.column not in allowed
    )
    return _failure(context, CheckFailureCode.UNAUTHORIZED_COLUMN, nodes=nodes) if nodes else None


def _unauthorized_join(context: _SemanticContext) -> CheckResult | None:
    source_ids: list[str] = []
    node_ids: list[str] = []
    for requirement in context.requirements.row_preservation_requirements:
        mismatches = _row_preservation_join_mismatches(requirement, context)
        if mismatches is None:
            continue
        source_ids.extend(requirement.related_source_ids)
        node_ids.extend(mismatches)
    for binding in context.requirements.selected_bindings:
        # Only vertical/EAV bindings get their join_path validated as a gate
        # here: swapped entity/catalog columns can silently attribute a value
        # to the wrong entity, which is a genuine authorization concern. For
        # every other binding kind, a "wrong" INNER join_path is intentionally
        # not treated as a pre-execution gate (see
        # test_reversed_and_different_inner_join_edges_are_not_pre_execution_gates
        # and test_cross_join_with_authorized_endpoints_is_not_blocked_by_join_path);
        # LEFT-preserving joins are already covered above via
        # row_preservation_requirements.
        if type(binding) is not VerticalAttributeBinding or not binding.join_path:
            continue
        mismatches = _join_path_edge_mismatches(binding.join_path, context)
        if mismatches:
            source_ids.append(binding.source_id)
            node_ids.extend(mismatches)
    return (
        _failure(
            context,
            CheckFailureCode.UNAUTHORIZED_JOIN,
            sources=tuple(source_ids),
            nodes=tuple(node_ids),
        )
        if source_ids
        else None
    )


def _row_preservation_join_mismatches(requirement, context: _SemanticContext):
    base_relation_ids = {
        relation_id
        for join in context.joins
        if join.scope_id in context.root_scope_ids
        for relation_id in (join.relation_id, *join.left_relation_ids)
        if context.relation_tables.get(relation_id) == requirement.base_table
    }
    if len(base_relation_ids) != 1:
        return ()
    node_ids: list[str] = []
    used_node_ids: set[str] = set()
    for edge in requirement.effective_join_path:
        candidates = [
            join
            for join in context.joins
            if join.scope_id in context.root_scope_ids
            and join.node_id not in used_node_ids
            and _join_condition_has_edge(join, edge, context)
        ]
        if len(candidates) != 1:
            return tuple(node_ids)
        join = candidates[0]
        used_node_ids.add(join.node_id)
        node_ids.append(join.node_id)
        options = dict(join.options.attributes) if join.options is not None else {}
        if (
            context.relation_tables.get(join.relation_id) != edge.right.table
            or not any(
                context.relation_tables.get(relation_id) == edge.left.table
                for relation_id in join.left_relation_ids
            )
            or options.get("side") != "LEFT"
        ):
            return tuple(node_ids)
    return None


def _join_path_edge_mismatches(join_path, context: _SemanticContext):
    """Node IDs of joins whose ON-condition does not encode a required edge.

    Unlike ``_row_preservation_join_mismatches`` (LEFT-join-only), this
    inspects every binding's ``join_path`` regardless of join type, catching
    e.g. a vertical/EAV join whose ON-condition swaps physical columns
    between two otherwise-correctly-joined tables.
    """
    node_ids: list[str] = []
    used_node_ids: set[str] = set()
    for edge in join_path:
        candidates = [
            join
            for join in context.joins
            if join.scope_id in context.root_scope_ids
            and join.node_id not in used_node_ids
            and _join_connects_edge_tables(join, edge, context)
        ]
        if len(candidates) != 1:
            # Accepted limitation (mirrors _row_preservation_join_mismatches):
            # when several joins connect the same table pair (e.g. two EAV
            # attribute pivots over one entity table) the edge cannot be
            # attributed to a single join by topology alone, so it is not
            # gated here rather than risk a false rejection.
            continue
        join = candidates[0]
        used_node_ids.add(join.node_id)
        if not _join_condition_has_edge(join, edge, context):
            node_ids.append(join.node_id)
            node_ids.extend(
                predicate.node_id
                for predicate in context.check_input.parsed_ast.predicates
                if predicate.location is PredicateLocation.JOIN_ON
                and predicate.owner_node_id == join.node_id
            )
    return tuple(node_ids)


def _join_connects_edge_tables(join, edge, context: _SemanticContext) -> bool:
    # ``left_relation_ids`` accumulates every relation already in scope, not
    # just the join's immediate left operand (chained joins keep growing this
    # set). Matching purely by table membership in the combined set is
    # therefore ambiguous for edges whose tables happen to be a subset of a
    # later join's cumulative left side. Anchor the match on the table this
    # join actually introduces (``relation_id``) and require the edge's other
    # table to already be in scope.
    own_table = context.relation_tables.get(join.relation_id)
    left_tables = {
        context.relation_tables.get(relation_id) for relation_id in join.left_relation_ids
    }
    if own_table == edge.left.table:
        return edge.right.table in left_tables
    if own_table == edge.right.table:
        return edge.left.table in left_tables
    return False


def _join_condition_has_edge(join, edge, context: _SemanticContext) -> bool:
    if join.condition is None:
        return False
    expected_forward = PredicateRef(
        left=edge.left,
        operator=edge.operator,
        right=edge.right,
    )
    expected_reversed = PredicateRef(
        left=edge.right,
        operator=edge.operator,
        right=edge.left,
    )
    pending = [join.condition]
    while pending:
        expression = pending.pop()
        try:
            actual = predicate_from_expression(
                expression,
                context.relation_tables,
                scope_id=join.scope_id,
                allowed_columns=(edge.left, edge.right),
                dialect=context.check_input.parsed_ast.dialect,
            )
        except (TypeError, ValueError):
            actual = None
        if actual is not None and (
            predicate_matches(expected_forward, actual)
            or predicate_matches(expected_reversed, actual)
        ):
            return True
        pending.extend(child for _, _, child in getattr(expression, "children", ()))
    return False


def _all_active_expressions(parsed_ast: object):
    for facts in (
        getattr(parsed_ast, "scopes", ()),
        getattr(parsed_ast, "table_scans", ()),
        getattr(parsed_ast, "set_operations", ()),
        getattr(parsed_ast, "orderings", ()),
        getattr(parsed_ast, "joins", ()),
    ):
        for fact in facts:
            if fact.options is not None:
                yield fact.node_id, fact.scope_id, "options", 0, fact.options
    for fact in getattr(parsed_ast, "expression_relations", ()):
        yield fact.node_id, fact.scope_id, "expression", 0, fact.expression
    for fact in getattr(parsed_ast, "joins", ()):
        if fact.condition is not None:
            yield fact.node_id, fact.scope_id, "condition", 0, fact.condition
    for fact in getattr(parsed_ast, "projections", ()):
        yield fact.node_id, fact.scope_id, "expression", 0, fact.expression
    for fact in getattr(parsed_ast, "predicates", ()):
        yield fact.node_id, fact.scope_id, "expression", 0, fact.expression
    for fact in getattr(parsed_ast, "aggregates", ()):
        yield fact.node_id, fact.scope_id, "expression", 0, fact.expression
    for fact in getattr(parsed_ast, "groupings", ()):
        yield fact.node_id, fact.scope_id, "expression", 0, fact.expression
    for fact in getattr(parsed_ast, "orderings", ()):
        yield fact.node_id, fact.scope_id, "expression", 0, fact.expression


def _comparison_occurrences(
    node_id: str,
    scope_id: str,
    expression_field: str,
    expression_index: int,
    expression: object,
    relations: dict[str, TableRef],
    allowed_columns: tuple[ColumnRef, ...],
    dialect: str,
    relation_scopes: dict[str, str],
) -> tuple[_PredicateOccurrence, ...]:
    result: list[_PredicateOccurrence] = []
    pending = [((), expression)]
    while pending:
        path, current = pending.pop()
        predicate = predicate_from_expression(
            current,
            relations,
            scope_id=scope_id,
            allowed_columns=allowed_columns,
            dialect=dialect,
        )
        if predicate is not None:
            result.append(
                _PredicateOccurrence(
                    node_id=node_id,
                    scope_id=scope_id,
                    relation_scope_ids=_predicate_relation_scope_ids(
                        current,
                        relation_scopes,
                    ),
                    expression_field=expression_field,
                    expression_index=expression_index,
                    expression_path=path,
                    unordered_between=(
                        getattr(current, "kind", None) == "between"
                        and getattr(current, "attributes", ()) == (("symmetric", True),)
                    ),
                    predicate=predicate,
                )
            )
        pending.extend(
            ((path + ((argument, ordinal),)), child)
            for argument, ordinal, child in getattr(current, "children", ())
        )
    return tuple(result)


def _predicate_relation_scope_ids(
    expression: object,
    relation_scopes: dict[str, str],
) -> tuple[str, ...]:
    children = {
        argument: child
        for argument, ordinal, child in getattr(expression, "children", ())
        if ordinal == 0
    }
    if len(children) != len(getattr(expression, "children", ())) or set(children) != {
        "this",
        "expression",
    }:
        return ()
    relation_ids = tuple(
        dict(getattr(child, "attributes", ())).get("relation_id")
        for child in (children["this"], children["expression"])
    )
    if not all(type(relation_id) is str for relation_id in relation_ids):
        return ()
    scopes = tuple(relation_scopes.get(relation_id) for relation_id in relation_ids)
    return scopes if all(type(scope) is str for scope in scopes) else ()


def _required_predicates(context: _SemanticContext) -> tuple[tuple[str, PredicateRef], ...]:
    result: list[tuple[str, PredicateRef]] = []
    for item in (
        *context.items_by_kind[SemanticItemKind.FILTER],
        *context.items_by_kind[SemanticItemKind.TIME],
    ):
        for binding in context.bindings_by_source[item.source_id]:
            result.extend((item.source_id, predicate) for predicate in binding.predicates)
    return tuple(result)


def _unauthorized_authority_literal(context: _SemanticContext) -> CheckResult | None:
    allowed = tuple(predicate for _, predicate in _required_predicates(context)) + tuple(context.requirements.allowed_predicates)
    eligible_evidence_ids = set(context.requirements.eligible_evidence_ids)
    exact_value_evidence = tuple(
        record
        for record in context.state.evidence
        if record.evidence_id in eligible_evidence_ids
        and record.source_kind is EvidenceSourceKind.VALUE_SEARCH
    )
    nodes = tuple(
        item.node_id
        for item in context.authority_predicates
        if _predicate_has_literal(item.predicate)
        and item.predicate.operator is not PredicateOperator.LIKE
        and not _binding_free_formula_authorizes_literal(context, item)
        and not _derived_formula_annotation_authorizes_literal(context, item)
        and not _requested_output_authorizes_literal(context, item)
        and not (
            item.predicate.left in context.requirements.allowed_columns
            and item.predicate.operator is PredicateOperator.EQ
            and predicate_has_exact_value_certificate(
                item.predicate,
                exact_value_evidence,
            )
        )
        and not any(
            _predicate_is_authorized(expected, item)
            for expected in allowed
        )
    )
    return _failure(context, CheckFailureCode.UNAUTHORIZED_LITERAL, nodes=nodes) if nodes else None


def _requested_output_authorizes_literal(
    context: _SemanticContext,
    occurrence: _PredicateOccurrence,
) -> bool:
    if occurrence.node_id not in {
        projection.node_id
        for projection in context.check_input.parsed_ast.projections
    }:
        return False
    requested_source_ids = set(context.query_spec.requested_output_source_ids)
    for item in context.query_spec.semantic_items:
        if (
            item.source_id not in requested_source_ids
            or item.operator is None
            or item.literal_or_reference is None
        ):
            continue
        for binding in context.bindings_by_source.get(item.source_id, ()):
            if type(binding) is not PhysicalColumnBinding:
                continue
            try:
                expected = PredicateRef(
                    left=binding.physical_column,
                    operator=item.operator,
                    right=item.literal_or_reference,
                )
            except (ValidationError, TypeError, ValueError):
                continue
            if _predicate_is_authorized(expected, occurrence):
                return True
    return False


def _predicate_has_literal(predicate: PredicateRef) -> bool:
    return type(predicate.right) is not ColumnRef


def _predicate_is_authorized(
    expected: PredicateRef,
    occurrence: _PredicateOccurrence,
) -> bool:
    return predicate_matches(
        expected,
        occurrence.predicate,
        unordered_between=occurrence.unordered_between,
    )


def _binding_free_formula_authorizes_literal(
    context: _SemanticContext,
    occurrence: _PredicateOccurrence,
) -> bool:
    if not any(
        item.required
        and item.status
        in {SemanticItemStatus.UNRESOLVED, SemanticItemStatus.RESOLVED}
        for item in context.items_by_kind[SemanticItemKind.FORMULA]
    ):
        return False
    formula_node_ids = {
        item.node_id
        for item in (
            *context.check_input.parsed_ast.projections,
            *context.check_input.parsed_ast.aggregates,
        )
    }
    return occurrence.node_id in formula_node_ids


def _derived_formula_annotation_authorizes_literal(
    context: _SemanticContext,
    occurrence: _PredicateOccurrence,
) -> bool:
    physical_formula_sources = {
        item.source_id
        for item in context.items_by_kind[SemanticItemKind.FORMULA]
    }
    formula_sources = {
        source_id
        for source_id, bindings in context.bindings_by_source.items()
        if any(type(binding) is DerivedExpressionBinding for binding in bindings)
        or (
            source_id in physical_formula_sources
            and any(type(binding) is PhysicalColumnBinding for binding in bindings)
        )
    }
    return any(
        formula_sources.intersection(annotation.source_ids)
        and annotation.node_id == occurrence.node_id
        and annotation.expression_field == occurrence.expression_field
        and annotation.expression_index == occurrence.expression_index
        and tuple(
            (segment.argument, segment.ordinal)
            for segment in annotation.expression_path
        )
        == occurrence.expression_path[: len(annotation.expression_path)]
        for annotation in context.check_input.semantic_ast.coverage.annotations
    )


def _failure(
    context: _SemanticContext,
    code: CheckFailureCode,
    *,
    sources: tuple[str, ...] = (),
    nodes: tuple[str, ...] = (),
) -> CheckResult:
    source_ids = tuple(sorted(set(sources)))
    ast_node_ids = tuple(sorted(set(nodes)))
    return CheckResult(
        check_id=f"semantic:{context.check_input.candidate.candidate_id}:{code.value.lower()}",
        candidate_id=context.check_input.candidate.candidate_id,
        check_kind=CheckKind.SEMANTIC,
        status=CheckStatus.FAILED,
        failure_code=code,
        affected_source_ids=source_ids,
        affected_ast_node_ids=ast_node_ids,
        observed_error=None,
        repair=CheckRepair(
            kind=RepairKind.REVISE_SQL,
            source_ids=source_ids,
            ast_node_ids=ast_node_ids,
        ),
    )


def _passed(candidate_id: str) -> CheckResult:
    return CheckResult(
        check_id=f"semantic:{candidate_id}:passed",
        candidate_id=candidate_id,
        check_kind=CheckKind.SEMANTIC,
        status=CheckStatus.PASSED,
        failure_code=None,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=None,
    )


def _inconclusive(candidate_id: str, nodes: tuple[str, ...] = ()) -> CheckResult:
    ast_node_ids = tuple(sorted(set(nodes)))
    return CheckResult(
        check_id=f"semantic:{candidate_id}:check_input_invalid",
        candidate_id=candidate_id,
        check_kind=CheckKind.SEMANTIC,
        status=CheckStatus.INCONCLUSIVE,
        failure_code=CheckFailureCode.CHECK_INPUT_INVALID,
        affected_source_ids=(),
        affected_ast_node_ids=ast_node_ids,
        observed_error=CheckFailureCode.CHECK_INPUT_INVALID.value,
        repair=CheckRepair(kind=RepairKind.REVISE_SQL, ast_node_ids=ast_node_ids),
    )


def _safe_candidate_id(value: object) -> str:
    candidate = getattr(value, "candidate", None)
    candidate_id = getattr(candidate, "candidate_id", None)
    return candidate_id if type(candidate_id) is str and candidate_id else "invalid-candidate"


def _cte_passthrough_column(
    expression: object,
    parsed_ast: object,
    relation_tables: dict[str, TableRef],
    allowed_columns: tuple[ColumnRef, ...] = (),
    dialect: str | None = None,
) -> ColumnRef | None:
    if (
        column := _column_ref(expression, relation_tables, allowed_columns, dialect)
    ) is not None:
        return column
    if getattr(expression, "kind", None) not in {"column", "outer_column"}:
        return None
    attributes = dict(getattr(expression, "attributes", ()))
    name = attributes.get("name")
    relation_id = attributes.get("relation_id")
    references = [
        item
        for item in getattr(parsed_ast, "cte_references", ())
        if item.relation_id == relation_id
    ]
    if type(name) is not str or len(references) != 1:
        return None
    cte_scopes = [
        item.query_scope_id
        for item in getattr(parsed_ast, "ctes", ())
        if item.cte_id == references[0].cte_id
    ]
    if len(cte_scopes) != 1:
        return None
    matches = [
        column
        for fact in getattr(parsed_ast, "projections", ())
        if fact.scope_id == cte_scopes[0]
        and dict(getattr(fact.expression, "attributes", ())).get("name") == name
        if (
            column := _column_ref(
                fact.expression,
                relation_tables,
                allowed_columns,
                dialect,
            )
        ) is not None
    ]
    return matches[0] if len(matches) == 1 else None


def _column_ref(
    expression: object,
    relations: dict[str, TableRef],
    allowed_columns: tuple[ColumnRef, ...] = (),
    dialect: str | None = None,
) -> ColumnRef | None:
    attributes = dict(getattr(expression, "attributes", ()))
    if getattr(expression, "kind", None) not in {"column", "outer_column"}:
        return None
    name = attributes.get("name")
    relation_id = attributes.get("relation_id")
    if type(name) is not str:
        return None
    if relation_id is None and len(relations) == 1:
        return _resolve_column_ref(
            ColumnRef(table=next(iter(relations.values())), column=name),
            allowed_columns,
            dialect,
        )
    if type(relation_id) is str and relation_id in relations:
        return _resolve_column_ref(
            ColumnRef(table=relations[relation_id], column=name),
            allowed_columns,
            dialect,
        )
    return None


def _reachable_scope_ids(parsed_ast: object) -> frozenset[str]:
    scopes = tuple(getattr(parsed_ast, "scopes", ()))
    reachable = {
        scope.scope_id
        for scope in scopes
        if scope.parent_scope_id is None and scope.query_role.value == "root"
    }
    ctes = {cte.cte_id: cte for cte in getattr(parsed_ast, "ctes", ())}
    while True:
        before = len(reachable)
        reachable.update(
            item.child_scope_id
            for item in getattr(parsed_ast, "subquery_refs", ())
            if item.scope_id in reachable
        )
        reachable.update(
            item.query_scope_id
            for item in getattr(parsed_ast, "derived_relations", ())
            if item.scope_id in reachable
        )
        for operation in getattr(parsed_ast, "set_operations", ()):
            if operation.parent_scope_id is None or operation.parent_scope_id in reachable:
                reachable.update((operation.left_scope_id, operation.right_scope_id))
        reachable.update(
            ctes[item.cte_id].query_scope_id
            for item in getattr(parsed_ast, "cte_references", ())
            if item.scope_id in reachable and item.cte_id in ctes
        )
        if len(reachable) == before:
            return frozenset(reachable)


def _active_ast(parsed_ast: object, reachable: frozenset[str]):
    from dataclasses import replace

    return replace(
        parsed_ast,
        table_scans=tuple(item for item in parsed_ast.table_scans if item.scope_id in reachable),
        expression_relations=tuple(
            item for item in parsed_ast.expression_relations if item.scope_id in reachable
        ),
        joins=tuple(item for item in parsed_ast.joins if item.scope_id in reachable),
        projections=tuple(item for item in parsed_ast.projections if item.scope_id in reachable),
        predicates=tuple(item for item in parsed_ast.predicates if item.scope_id in reachable),
        aggregates=tuple(item for item in parsed_ast.aggregates if item.scope_id in reachable),
        groupings=tuple(item for item in parsed_ast.groupings if item.scope_id in reachable),
        orderings=tuple(item for item in parsed_ast.orderings if item.scope_id in reachable),
        limits=tuple(item for item in parsed_ast.limits if item.scope_id in reachable),
    )


__all__ = ["evaluate_semantic_authority_checks"]
