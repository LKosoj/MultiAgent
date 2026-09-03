"""Trusted semantic annotations over the one immutable sqlglot AST."""

from __future__ import annotations

from dataclasses import dataclass

from ._semantic_matching import predicate_matches
from ._sql_ast_identity import semantic_candidate_digest, source_sql_digest
from ._sql_ast_models import (
    ExpressionFact,
    ParsedSqlCandidate,
    QueryRole,
)
from .models import (
    AstExpressionPathSegment,
    AstNodeAnnotation,
    AstSemanticCoverage,
    ColumnRef,
    DerivedExpressionBinding,
    DiscriminatorValueBinding,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    QuerySpec,
    SemanticItem,
    SemanticItemKind,
    SqlCandidate,
    TableRef,
    VerticalAttributeBinding,
    is_binding_free_semantic_item,
)
from .semantic_coverage import CoverageRequirements


@dataclass(frozen=True, slots=True)
class AstColumnOccurrence:
    node_id: str
    column: ColumnRef


@dataclass(frozen=True, slots=True)
class AstColumnFacts:
    occurrences: tuple[AstColumnOccurrence, ...]
    projection_columns: tuple[ColumnRef, ...]
    aggregate_columns: tuple[ColumnRef, ...]


@dataclass(frozen=True, slots=True)
class AuthenticatedSemanticAst:
    """The parsed candidate plus a small, deterministic authority overlay."""

    candidate: SqlCandidate
    parsed_ast: ParsedSqlCandidate
    table_namespace: str
    coverage: AstSemanticCoverage

    def __post_init__(self) -> None:
        if (
            type(self.candidate) is not SqlCandidate
            or type(self.parsed_ast) is not ParsedSqlCandidate
            or type(self.table_namespace) is not str
            or not self.table_namespace
            or type(self.coverage) is not AstSemanticCoverage
        ):
            raise TypeError("AuthenticatedSemanticAst requires exact contract types")


def build_semantic_ast(
    candidate: SqlCandidate,
    parsed_ast: ParsedSqlCandidate,
    query_spec: QuerySpec,
    requirements: CoverageRequirements,
    table_namespace: str,
) -> AuthenticatedSemanticAst:
    """Attach W4 authority to exact existing AST locations without rewriting SQL."""

    candidate, parsed_ast, query_spec, requirements, items = _validated_inputs(
        candidate,
        parsed_ast,
        query_spec,
        requirements,
        table_namespace,
    )
    relation_tables = _relation_tables(
        parsed_ast,
        table_namespace,
        requirements.allowed_tables,
        parsed_ast.dialect,
    )
    pending: dict[tuple[object, ...], tuple[set[str], set[str]]] = {}

    def add(
        node_id: str,
        source_id: str,
        evidence_ids: tuple[str, ...],
        *,
        expression_field: str | None = None,
        expression_index: int | None = None,
        expression_path: tuple[tuple[str, int], ...] = (),
    ) -> None:
        key = (
            node_id,
            expression_field,
            expression_index,
            expression_path,
        )
        sources, evidence = pending.setdefault(key, (set(), set()))
        sources.add(source_id)
        evidence.update(evidence_ids)

    for binding in requirements.selected_bindings:
        item = items[binding.source_id]
        targets = _binding_targets(
            item,
            binding,
            parsed_ast,
            relation_tables,
            requirements.allowed_columns,
            parsed_ast.dialect,
        )
        for node_id, field, index, path in targets:
            add(
                node_id,
                binding.source_id,
                binding.evidence_ids,
                expression_field=field,
                expression_index=index,
                expression_path=path,
            )
    annotations = tuple(
        AstNodeAnnotation(
            node_id=key[0],
            expression_field=key[1],
            expression_index=key[2],
            expression_path=tuple(
                AstExpressionPathSegment(argument=argument, ordinal=ordinal)
                for argument, ordinal in key[3]
            ),
            source_ids=tuple(sorted(source_ids)),
            evidence_ids=tuple(sorted(evidence_ids)),
        )
        for key, (source_ids, evidence_ids) in sorted(pending.items())
    )
    coverage = AstSemanticCoverage(
        requirements_digest=requirements.requirements_digest,
        required_source_ids=requirements.required_source_ids,
        evidence_ids=tuple(
            sorted(
                {
                    evidence_id
                    for annotation in annotations
                    for evidence_id in annotation.evidence_ids
                }
            )
        ),
        annotations=annotations,
    )
    return AuthenticatedSemanticAst(candidate, parsed_ast, table_namespace, coverage)


def authenticate_semantic_ast(
    value: AuthenticatedSemanticAst,
    query_spec: QuerySpec,
    requirements: CoverageRequirements,
) -> AuthenticatedSemanticAst:
    """Rebuild the trusted overlay and reject any unmatched or substituted address."""

    if type(value) is not AuthenticatedSemanticAst:
        raise TypeError("value must be AuthenticatedSemanticAst")
    expected = build_semantic_ast(
        value.candidate,
        value.parsed_ast,
        query_spec,
        requirements,
        value.table_namespace,
    )
    if value != expected:
        raise ValueError("semantic AST does not match its authenticated authority")
    return expected


def collect_ast_columns(
    parsed_ast: ParsedSqlCandidate,
    relation_tables: dict[str, TableRef],
    *,
    allowed_columns: tuple[ColumnRef, ...] = (),
    dialect: str | None = None,
) -> AstColumnFacts:
    """Collect physical column references from all scopes and expression owners."""

    projection_by_id = {item.output_id: item for item in parsed_ast.projections}
    projections_by_scope: dict[str, tuple[ExpressionFact, ...]] = {
        scope.node_id: tuple(
            item.expression
            for item in parsed_ast.projections
            if item.scope_id == scope.node_id
        )
        for scope in parsed_ast.scopes
    }
    relation_projection_aliases = _relation_projection_aliases(parsed_ast)
    declared_table_aliases = {
        scan.relation_id: scan.column_aliases
        for scan in parsed_ast.table_scans
        if scan.column_aliases
    }

    def columns(expression: ExpressionFact, scope_id: str) -> tuple[ColumnRef, ...]:
        return _expression_columns(
            expression,
            relation_tables,
            projection_by_id,
            projections_by_scope,
            relation_projection_aliases,
            declared_table_aliases,
            parsed_ast.set_operations,
            scope_id,
            allowed_columns,
            dialect,
        )

    occurrences: list[AstColumnOccurrence] = []
    projection_columns: list[ColumnRef] = []
    aggregate_columns: list[ColumnRef] = []
    for fact in parsed_ast.joins:
        if fact.condition is not None:
            occurrences.extend(
                AstColumnOccurrence(fact.node_id, column)
                for column in columns(fact.condition, fact.scope_id)
            )
    for fact in parsed_ast.expression_relations:
        input_tables = {
            relation_id: relation_tables[relation_id]
            for relation_id in fact.input_relation_ids
            if relation_id in relation_tables
        }
        occurrences.extend(
            AstColumnOccurrence(fact.node_id, column)
                for column in _expression_columns(
                    fact.expression,
                    input_tables or relation_tables,
                    projection_by_id,
                    projections_by_scope,
                    relation_projection_aliases,
                    declared_table_aliases,
                    parsed_ast.set_operations,
                    fact.scope_id,
                    allowed_columns,
                    dialect,
                    declared_output_columns=frozenset(fact.column_aliases),
                )
        )
    for node_id, scope_id, options in _generic_option_owners(parsed_ast):
        occurrences.extend(
            AstColumnOccurrence(node_id, column)
            for column in columns(options, scope_id)
        )
    for fact in parsed_ast.predicates:
        for atom in fact.atoms:
            occurrences.extend(
                AstColumnOccurrence(atom.node_id, column)
                for column in columns(atom.expression, fact.scope_id)
            )
    for fact in parsed_ast.projections:
        found = columns(fact.expression, fact.scope_id)
        projection_columns.extend(found)
        occurrences.extend(AstColumnOccurrence(fact.node_id, column) for column in found)
    for fact in parsed_ast.aggregates:
        found = columns(fact.expression, fact.scope_id)
        aggregate_columns.extend(found)
        occurrences.extend(AstColumnOccurrence(fact.node_id, column) for column in found)
    for fact in parsed_ast.groupings:
        occurrences.extend(
            AstColumnOccurrence(fact.node_id, column)
            for column in columns(fact.expression, fact.scope_id)
        )
    for fact in parsed_ast.orderings:
        occurrences.extend(
            AstColumnOccurrence(fact.node_id, column)
            for column in columns(fact.expression, fact.scope_id)
        )
    return AstColumnFacts(
        occurrences=tuple(occurrences),
        projection_columns=tuple(projection_columns),
        aggregate_columns=tuple(aggregate_columns),
    )


def predicate_from_expression(
    expression: ExpressionFact,
    relation_tables: dict[str, TableRef],
    *,
    scope_id: str,
    projections: tuple[object, ...] = (),
    allowed_columns: tuple[ColumnRef, ...] = (),
    dialect: str | None = None,
) -> PredicateRef | None:
    """Decode concrete comparisons, IN lists, and ordinary BETWEEN predicates."""

    operator = {
        "eq": PredicateOperator.EQ,
        "neq": PredicateOperator.NEQ,
        "gt": PredicateOperator.GT,
        "gte": PredicateOperator.GTE,
        "lt": PredicateOperator.LT,
        "lte": PredicateOperator.LTE,
        "like": PredicateOperator.LIKE,
    }.get(expression.kind)
    children = _named_children(expression)
    if operator is not None and set(children) == {"this", "expression"}:
        left = _column_ref(children["this"], relation_tables, allowed_columns, dialect)
        right = _predicate_operand(
            children["expression"], relation_tables, allowed_columns, dialect
        )
        return (
            PredicateRef(left=left, operator=operator, right=right)
            if left is not None and right is not _UNRESOLVED
            else None
        )
    if expression.kind == "in":
        left = _only_child(expression, "this")
        values = _indexed_children(expression, "expressions")
        column = (
            _column_ref(left, relation_tables, allowed_columns, dialect)
            if left is not None
            else None
        )
        parsed = tuple(_literal_value(value) for value in values)
        if column is not None and values and all(value is not _UNRESOLVED for value in parsed):
            return PredicateRef(left=column, operator=PredicateOperator.IN, right=parsed)
    if expression.kind == "between" and expression.attributes in {
        (),
        (("symmetric", True),),
    }:
        children = _named_children(expression)
        if set(children) == {"this", "low", "high"}:
            column = _column_ref(
                children["this"], relation_tables, allowed_columns, dialect
            )
            low = _literal_value(children["low"])
            high = _literal_value(children["high"])
            if column is not None and low is not _UNRESOLVED and high is not _UNRESOLVED:
                return PredicateRef(
                    left=column,
                    operator=PredicateOperator.BETWEEN,
                    right=(low, high),
                )
    return None


def _is_symmetric_between(expression: ExpressionFact) -> bool:
    return (
        expression.kind == "between"
        and expression.attributes == (("symmetric", True),)
    )


def _conditional_aggregate_conditions(
    parsed_ast: ParsedSqlCandidate,
) -> tuple[tuple[str, str, tuple[tuple[str, int], ...], ExpressionFact], ...]:
    result: list[tuple[str, str, tuple[tuple[str, int], ...], ExpressionFact]] = []
    for aggregate in parsed_ast.aggregates:
        aggregate_children = _named_children(aggregate.expression)
        case = aggregate_children.get("this")
        if set(aggregate_children) != {"this"} or case is None or case.kind != "case":
            continue
        alternatives = _indexed_children(case, "ifs")
        if len(case.children) != 1 or len(alternatives) != 1:
            continue
        alternative_children = _named_children(alternatives[0])
        condition = alternative_children.get("this")
        selected = alternative_children.get("true")
        if (
            set(alternative_children) != {"this", "true"}
            or condition is None
            or selected is None
            or selected.kind == "null"
        ):
            continue
        result.append(
            (
                aggregate.node_id,
                aggregate.scope_id,
                (("this", 0), ("ifs", 0), ("this", 0)),
                condition,
            )
        )
    return tuple(result)


def _binding_targets(
    item: SemanticItem,
    binding: object,
    parsed_ast: ParsedSqlCandidate,
    relation_tables: dict[str, TableRef],
    allowed_columns: tuple[ColumnRef, ...],
    dialect: str,
) -> set[tuple[str, str | None, int | None, tuple[tuple[str, int], ...]]]:
    targets: set[
        tuple[str, str | None, int | None, tuple[tuple[str, int], ...]]
    ] = set()
    generic_predicates = tuple(
        (node_id, scope_id, "options", path, expression)
        for node_id, scope_id, options in _generic_option_owners(parsed_ast)
        for path, expression in _expression_paths(options)
    ) + tuple(
        (fact.node_id, fact.scope_id, "expression", path, expression)
        for fact in parsed_ast.expression_relations
        for path, expression in _expression_paths(fact.expression)
    )
    if type(binding) is DiscriminatorValueBinding:
        targets.update(
            (atom.node_id, "expression", 0, ())
            for predicate in parsed_ast.predicates
            for atom in predicate.atoms
            if (actual := predicate_from_expression(
                atom.expression,
                relation_tables,
                scope_id=predicate.scope_id,
                allowed_columns=allowed_columns,
                dialect=dialect,
            )) is not None
            and any(
                predicate_matches(
                    required,
                    actual,
                    unordered_between=_is_symmetric_between(atom.expression),
                )
                for required in binding.predicates
            )
        )
        targets.update(
            (node_id, field, 0, path)
            for node_id, scope_id, field, path, expression in generic_predicates
            if (actual := predicate_from_expression(
                expression, relation_tables, scope_id=scope_id,
                allowed_columns=allowed_columns, dialect=dialect,
            )) is not None
            and any(
                predicate_matches(required, actual)
                for required in binding.predicates
            )
        )
        targets.update(
            (node_id, "expression", 0, path)
            for node_id, scope_id, path, condition in _conditional_aggregate_conditions(
                parsed_ast
            )
            if (actual := predicate_from_expression(
                condition,
                relation_tables,
                scope_id=scope_id,
                allowed_columns=allowed_columns,
                dialect=dialect,
            )) is not None
            and any(
                predicate_matches(
                    required,
                    actual,
                    unordered_between=_is_symmetric_between(condition),
                )
                for required in binding.predicates
            )
        )
    elif type(binding) is VerticalAttributeBinding:
        targets.update(
            (atom.node_id, "expression", 0, ())
            for predicate in parsed_ast.predicates
            for atom in predicate.atoms
            if (actual := predicate_from_expression(
                atom.expression,
                relation_tables,
                scope_id=predicate.scope_id,
                allowed_columns=allowed_columns,
                dialect=dialect,
            )) is not None
            and any(
                predicate_matches(
                    required,
                    actual,
                    unordered_between=_is_symmetric_between(atom.expression),
                )
                for required in (
                    binding.attribute_name_predicate,
                    binding.value_predicate,
                )
            )
        )
        targets.update(
            (node_id, "expression", 0, path)
            for node_id, scope_id, path, condition in _conditional_aggregate_conditions(
                parsed_ast
            )
            if (
                actual := predicate_from_expression(
                    condition,
                    relation_tables,
                    scope_id=scope_id,
                    allowed_columns=allowed_columns,
                    dialect=dialect,
                )
            ) is not None
            and any(
                predicate_matches(
                    required,
                    actual,
                    unordered_between=_is_symmetric_between(condition),
                )
                for required in (
                    binding.attribute_name_predicate,
                    binding.value_predicate,
                )
            )
        )
        targets.update(
            (node_id, field, 0, path)
            for node_id, scope_id, field, path, expression in generic_predicates
            if (actual := predicate_from_expression(
                expression, relation_tables, scope_id=scope_id,
                allowed_columns=allowed_columns, dialect=dialect,
            )) is not None
            and any(
                predicate_matches(required, actual)
                for required in (
                    binding.attribute_name_predicate,
                    binding.value_predicate,
                )
            )
        )
    elif type(binding) is DerivedExpressionBinding:
        input_columns = set(binding.input_columns)
        for predicate in (
            parsed_ast.predicates
            if item.kind
            in {
                SemanticItemKind.FILTER,
                SemanticItemKind.FORMULA,
                SemanticItemKind.METRIC,
            }
            else ()
        ):
            atom_columns = tuple(
                (
                    atom,
                    _formula_candidate_columns(
                        atom.expression,
                        parsed_ast,
                        relation_tables,
                        allowed_columns,
                        dialect,
                    ),
                )
                for atom in predicate.atoms
            )
            targets.update(
                (
                    predicate.node_id,
                    "expression",
                    0,
                    next(
                        path
                        for path, expression in _expression_paths(
                            predicate.expression
                        )
                        if expression == atom.expression
                    ),
                )
                for atom, columns in atom_columns
                if columns and columns.issubset(input_columns)
            )
        if item.kind is SemanticItemKind.ORDERING:
            targets.update(
                (ordering.node_id, "expression", 0, ())
                for ordering in parsed_ast.orderings
                if set(binding.input_columns).issubset(
                    _formula_candidate_columns(
                        ordering.expression,
                        parsed_ast,
                        relation_tables,
                        allowed_columns,
                        dialect,
                    )
                )
            )
            return targets
        root_scopes = {
            scope.scope_id for scope in parsed_ast.scopes if scope.parent_scope_id is None
        }
        candidates = tuple(
            projection
            for projection in parsed_ast.projections
            if projection.scope_id in root_scopes
            and set(binding.input_columns).issubset(
                _formula_candidate_columns(
                    projection.expression,
                    parsed_ast,
                    relation_tables,
                    allowed_columns,
                    dialect,
                )
            )
        )
        for projection in candidates:
            targets.add((projection.node_id, "expression", 0, ()))
        candidate_expressions = {projection.expression for projection in candidates}
        for grouping in parsed_ast.groupings:
            for path, expression in _expression_paths(grouping.expression):
                if expression in candidate_expressions:
                    targets.add((grouping.node_id, "expression", 0, path))
        for ordering in parsed_ast.orderings:
            if ordering.expression in candidate_expressions:
                targets.add((ordering.node_id, "expression", 0, ()))
    elif type(binding) is PhysicalColumnBinding:
        if item.kind is SemanticItemKind.FORMULA:
            for predicate in parsed_ast.predicates:
                for atom in predicate.atoms:
                    if _formula_candidate_columns(
                        atom.expression,
                        parsed_ast,
                        relation_tables,
                        allowed_columns,
                        dialect,
                    ) != {binding.physical_column}:
                        continue
                    targets.add(
                        (
                            predicate.node_id,
                            "expression",
                            0,
                            next(
                                path
                                for path, expression in _expression_paths(
                                    predicate.expression
                                )
                                if expression == atom.expression
                            ),
                        )
                    )
        elif item.kind is SemanticItemKind.LIMIT:
            targets.update((limit.node_id, None, None, ()) for limit in parsed_ast.limits)
        else:
            for node_id, field, index, expression in _physical_expression_owners(
                item,
                parsed_ast,
            ):
                targets.update(
                    (node_id, field, index, path)
                    for path in _column_paths(
                        expression,
                        binding.physical_column,
                        relation_tables,
                        allowed_columns,
                        dialect,
                    )
                )
    else:
        raise ValueError("semantic binding subtype is unsupported")
    return targets


def _formula_candidate_columns(
    expression: ExpressionFact,
    parsed_ast: ParsedSqlCandidate,
    relation_tables: dict[str, TableRef],
    allowed_columns: tuple[ColumnRef, ...],
    dialect: str,
) -> set[ColumnRef]:
    """Resolve confirmed columns from the immutable candidate AST only."""

    if (
        column := _formula_column_ref(
            expression,
            parsed_ast,
            relation_tables,
            allowed_columns,
            dialect,
        )
    ) is not None:
        return {column}
    attributes = dict(expression.attributes)
    if expression.kind == "subquery_ref":
        scope_id = attributes.get("scope_id")
        if type(scope_id) is str:
            return {
                column
                for projection in parsed_ast.projections
                if projection.scope_id == scope_id
                for column in _formula_candidate_columns(
                    projection.expression,
                    parsed_ast,
                    relation_tables,
                    allowed_columns,
                    dialect,
                )
            }
    return {
        column
        for _, _, child in expression.children
        for column in _formula_candidate_columns(
            child,
            parsed_ast,
            relation_tables,
            allowed_columns,
            dialect,
        )
    }


def _physical_expression_owners(
    item: SemanticItem,
    parsed_ast: ParsedSqlCandidate,
) -> tuple[tuple[str, str, int, ExpressionFact], ...]:
    if item.kind is SemanticItemKind.METRIC:
        return tuple(
            (fact.node_id, "expression", 0, fact.expression)
            for fact in (
                *parsed_ast.projections,
                *parsed_ast.aggregates,
                *parsed_ast.expression_relations,
            )
        )
    if item.kind is SemanticItemKind.DIMENSION:
        return (
            *(
                (fact.node_id, "expression", 0, fact.expression)
                for fact in (*parsed_ast.projections, *parsed_ast.expression_relations)
            ),
            *(
                (fact.node_id, "expression", 0, fact.expression)
                for fact in parsed_ast.groupings
            ),
        )
    if item.kind is SemanticItemKind.ORDERING:
        return tuple(
            (fact.node_id, "expression", 0, fact.expression)
            for fact in parsed_ast.orderings
        )
    return ()


def _generic_option_owners(parsed_ast: ParsedSqlCandidate):
    for facts in (
        parsed_ast.scopes,
        parsed_ast.table_scans,
        parsed_ast.set_operations,
        parsed_ast.orderings,
        parsed_ast.joins,
    ):
        for fact in facts:
            if fact.options is not None:
                yield fact.node_id, fact.scope_id, fact.options


def _column_paths(
    expression: ExpressionFact,
    expected: ColumnRef,
    relation_tables: dict[str, TableRef],
    allowed_columns: tuple[ColumnRef, ...],
    dialect: str,
) -> tuple[tuple[tuple[str, int], ...], ...]:
    return tuple(
        path
        for path, candidate in _expression_paths(expression)
        if _column_ref(candidate, relation_tables, allowed_columns, dialect) == expected
    )


def _expression_paths(
    expression: ExpressionFact,
    path: tuple[tuple[str, int], ...] = (),
):
    yield path, expression
    for argument, ordinal, child in expression.children:
        yield from _expression_paths(child, (*path, (argument, ordinal)))



def _formula_column_ref(
    expression: ExpressionFact,
    parsed_ast: ParsedSqlCandidate,
    relation_tables: dict[str, TableRef],
    allowed_columns: tuple[ColumnRef, ...],
    dialect: str,
    resolving_relation_ids: frozenset[str] = frozenset(),
) -> ColumnRef | None:
    attributes = dict(expression.attributes)
    if len(attributes) != len(expression.attributes):
        return None
    relation_id = attributes.get("relation_id")
    name = attributes.get("name")
    if (
        type(relation_id) is str
        and type(name) is str
        and name
        in next(
            (
                scan.column_aliases
                for scan in parsed_ast.table_scans
                if scan.relation_id == relation_id
            ),
            (),
        )
    ):
        return None
    direct = _column_ref(expression, relation_tables, allowed_columns, dialect)
    if direct is not None:
        return direct
    if type(relation_id) is not str or type(name) is not str:
        return None
    if relation_id in resolving_relation_ids:
        return None
    relation_aliases = _relation_projection_aliases(parsed_ast).get(relation_id)
    if relation_aliases is not None:
        child_scope_id, aliases = relation_aliases
        if name not in aliases:
            return None
        projections_by_scope = {
            scope.node_id: tuple(
                item.expression
                for item in parsed_ast.projections
                if item.scope_id == scope.node_id
            )
            for scope in parsed_ast.scopes
        }
        projection = _scope_projection(
            child_scope_id,
            aliases.index(name),
            projections_by_scope,
            parsed_ast.set_operations,
        )
        if projection is None:
            return None
        return _formula_column_ref(
            projection,
            parsed_ast,
            relation_tables,
            allowed_columns,
            dialect,
            resolving_relation_ids | {relation_id},
        )
    child_scope_ids = tuple(
        item.query_scope_id
        for item in parsed_ast.derived_relations
        if item.relation_id == relation_id
    )
    if not child_scope_ids:
        cte_ids = tuple(
            item.cte_id
            for item in parsed_ast.cte_references
            if item.relation_id == relation_id
        )
        if len(cte_ids) != 1:
            return None
        child_scope_ids = tuple(
            item.query_scope_id for item in parsed_ast.ctes if item.cte_id == cte_ids[0]
        )
    if len(child_scope_ids) != 1:
        return None
    columns = tuple(
        column
        for projection in parsed_ast.projections
        if projection.scope_id == child_scope_ids[0]
        if (
            column := _formula_column_ref(
                projection.expression,
                parsed_ast,
                relation_tables,
                allowed_columns,
                dialect,
                resolving_relation_ids | {relation_id},
            )
        ) is not None
        and _resolve_column_ref(
            ColumnRef(table=column.table, column=name),
            allowed_columns,
            dialect,
        ) == column
    )
    if len(columns) != 1:
        return None
    return columns[0]



def _validated_inputs(
    candidate: SqlCandidate,
    parsed_ast: ParsedSqlCandidate,
    query_spec: QuerySpec,
    requirements: CoverageRequirements,
    table_namespace: str,
) -> tuple[SqlCandidate, ParsedSqlCandidate, QuerySpec, CoverageRequirements, dict[str, SemanticItem]]:
    if (
        type(candidate) is not SqlCandidate
        or type(parsed_ast) is not ParsedSqlCandidate
        or type(query_spec) is not QuerySpec
        or type(requirements) is not CoverageRequirements
        or type(table_namespace) is not str
        or not table_namespace
    ):
        raise TypeError("semantic AST inputs require exact contract types")
    checked_candidate = SqlCandidate.model_validate(candidate.model_dump(mode="python"))
    checked_query = QuerySpec.model_validate(query_spec.model_dump(mode="python"))
    checked_requirements = CoverageRequirements.model_validate(
        requirements.model_dump(mode="python")
    )
    if (
        checked_candidate != candidate
        or checked_query != query_spec
        or checked_requirements != requirements
        or parsed_ast.candidate_id != candidate.candidate_id
        or parsed_ast.source_sql_digest != source_sql_digest(candidate.sql)
        or parsed_ast.candidate_digest != semantic_candidate_digest(parsed_ast)
        or candidate.normalized_ast_digest != parsed_ast.candidate_digest
        or candidate.revision != requirements.state_revision
        or requirements.run_id != query_spec.run_id
        or requirements.run_incarnation != query_spec.run_incarnation
        or requirements.schema_namespace_version != query_spec.schema_namespace_version
        or requirements.expected_result_shape is not query_spec.expected_result_shape
    ):
        raise ValueError("semantic AST identities contradict each other")
    required = tuple(
        sorted(
            (item for item in query_spec.semantic_items if item.required),
            key=lambda item: item.source_id,
        )
    )
    if requirements.required_source_ids != tuple(item.source_id for item in required):
        raise ValueError("semantic coverage sources contradict the query")
    deferred_limits = tuple(
        item
        for item in required
        if item.kind is SemanticItemKind.LIMIT
        and item.literal_or_reference is None
    )
    root_scope_ids = {
        scope.scope_id
        for scope in parsed_ast.scopes
        if scope.parent_scope_id is None and scope.query_role is QueryRole.ROOT
    }
    root_limits = tuple(
        limit for limit in parsed_ast.limits if limit.scope_id in root_scope_ids
    )
    if deferred_limits and (
        len(deferred_limits) != 1
        or len(root_limits) != 1
        or type(root_limits[0].count) is not int
        or root_limits[0].count <= 0
    ):
        raise ValueError("unknown limit requires one positive outer literal LIMIT")
    items = {item.source_id: item for item in required}
    binding_required_source_ids = tuple(
        item.source_id for item in required if not is_binding_free_semantic_item(item)
    )
    bindings_by_source = {
        source_id: tuple(
            binding
            for binding in requirements.selected_bindings
            if binding.source_id == source_id
        )
        for source_id in binding_required_source_ids
    }
    if (
        tuple(bindings_by_source) != binding_required_source_ids
        or any(not bindings_by_source[source_id] for source_id in binding_required_source_ids)
    ):
        raise ValueError("selected bindings do not exactly cover required items")
    return candidate, parsed_ast, query_spec, requirements, items


def _relation_tables(
    parsed_ast: ParsedSqlCandidate,
    table_namespace: str,
    allowed_tables: tuple[TableRef, ...],
    dialect: str | None = None,
) -> dict[str, TableRef]:
    return {
        scan.relation_id: _table_ref(
            scan.table,
            table_namespace,
            allowed_tables,
            dialect,
        )
        for scan in parsed_ast.table_scans
    }


def _table_ref(
    table: object,
    namespace: str,
    allowed_tables: tuple[TableRef, ...],
    dialect: str | None = None,
) -> TableRef:
    raw = TableRef(
        namespace=namespace,
        schema=getattr(table, "schema"),
        table=getattr(table, "name"),
    )
    if raw.schema_name is not None:
        return raw
    matches = tuple(
        item
        for item in allowed_tables
        if item.namespace == raw.namespace and item.table == raw.table
    )
    if len(matches) == 1:
        return matches[0]
    if dialect != "sqlite":
        return raw
    matches = tuple(
        item
        for item in allowed_tables
        if item.namespace == raw.namespace
        and item.table.casefold() == raw.table.casefold()
    )
    return matches[0] if len(matches) == 1 else raw


def _expression_columns(
    expression: ExpressionFact,
    relation_tables: dict[str, TableRef],
    projections_by_id: dict[str, object],
    projections_by_scope: dict[str, tuple[ExpressionFact, ...]],
    relation_projection_aliases: dict[str, tuple[str, tuple[str, ...]]],
    declared_table_aliases: dict[str, tuple[str, ...]],
    set_operations: tuple[object, ...],
    scope_id: str,
    allowed_columns: tuple[ColumnRef, ...],
    dialect: str | None,
    resolving_relation_ids: frozenset[str] = frozenset(),
    declared_output_columns: frozenset[str] = frozenset(),
    in_declared_output: bool = False,
) -> tuple[ColumnRef, ...]:
    attributes = dict(expression.attributes)
    if len(attributes) != len(expression.attributes):
        raise ValueError("AST expression attributes are contradictory")
    relation_id = attributes.get("relation_id")
    name = attributes.get("name")
    if (
        type(relation_id) is str
        and type(name) is str
        and relation_id in relation_projection_aliases
        and relation_id not in resolving_relation_ids
    ):
        child_scope_id, aliases = relation_projection_aliases[relation_id]
        if name in aliases:
            child = _scope_projection(
                child_scope_id,
                aliases.index(name),
                projections_by_scope,
                set_operations,
            )
            if child is not None:
                return _expression_columns(
                    child,
                    relation_tables,
                    projections_by_id,
                    projections_by_scope,
                    relation_projection_aliases,
                    declared_table_aliases,
                    set_operations,
                    child_scope_id,
                    allowed_columns,
                    dialect,
                    resolving_relation_ids | {relation_id},
                )
    if (
        type(relation_id) is str
        and type(name) is str
        and name in declared_table_aliases.get(relation_id, ())
    ):
        return ()
    column = _column_ref(expression, relation_tables, allowed_columns, dialect)
    if column is not None:
        if in_declared_output and type(name) is str and name in declared_output_columns:
            return ()
        return (column,)
    if expression.kind == "output_ref":
        projection = projections_by_id.get(attributes.get("output_id"))
        if projection is None or getattr(projection, "scope_id", None) != scope_id:
            return ()
        return _expression_columns(
            projection.expression,
            relation_tables,
            projections_by_id,
            projections_by_scope,
            relation_projection_aliases,
            declared_table_aliases,
            set_operations,
            scope_id,
            allowed_columns,
            dialect,
            resolving_relation_ids,
            declared_output_columns,
            in_declared_output,
        )
    if expression.kind == "ordinal_ref":
        ordinal = attributes.get("ordinal")
        values = projections_by_scope.get(scope_id, ())
        if type(ordinal) is not int or not 1 <= ordinal <= len(values):
            return ()
        return _expression_columns(
            values[ordinal - 1],
            relation_tables,
            projections_by_id,
            projections_by_scope,
            relation_projection_aliases,
            declared_table_aliases,
            set_operations,
            scope_id,
            allowed_columns,
            dialect,
            resolving_relation_ids,
            declared_output_columns,
            in_declared_output,
        )
    result: list[ColumnRef] = []
    for argument, _, child in expression.children:
        result.extend(
            _expression_columns(
                child,
                relation_tables,
                projections_by_id,
                projections_by_scope,
                relation_projection_aliases,
                declared_table_aliases,
                set_operations,
                scope_id,
                allowed_columns,
                dialect,
                resolving_relation_ids,
                declared_output_columns,
                in_declared_output or argument == "into",
            )
        )
    return tuple(result)


def _relation_projection_aliases(
    parsed_ast: ParsedSqlCandidate,
) -> dict[str, tuple[str, tuple[str, ...]]]:
    ctes = {item.cte_id: item for item in parsed_ast.ctes}
    aliases: dict[str, tuple[str, tuple[str, ...]]] = {}
    for reference in parsed_ast.cte_references:
        cte = ctes.get(reference.cte_id)
        column_aliases = reference.column_aliases or (
            cte.column_aliases if cte is not None else ()
        )
        if cte is not None and column_aliases:
            aliases[reference.relation_id] = (cte.query_scope_id, column_aliases)
    for relation in parsed_ast.derived_relations:
        if relation.column_aliases:
            aliases[relation.relation_id] = (
                relation.query_scope_id,
                relation.column_aliases,
            )
    return aliases


def _scope_projection(
    scope_id: str,
    ordinal: int,
    projections_by_scope: dict[str, tuple[ExpressionFact, ...]],
    set_operations: tuple[object, ...],
) -> ExpressionFact | None:
    projections = projections_by_scope.get(scope_id, ())
    if ordinal < len(projections):
        return projections[ordinal]
    operations = tuple(item for item in set_operations if item.scope_id == scope_id)
    if len(operations) != 1:
        return None
    return _scope_projection(
        operations[0].left_scope_id,
        ordinal,
        projections_by_scope,
        set_operations,
    )


def _column_ref(
    expression: ExpressionFact | None,
    relation_tables: dict[str, TableRef],
    allowed_columns: tuple[ColumnRef, ...] = (),
    dialect: str | None = None,
) -> ColumnRef | None:
    if expression is None or expression.kind not in {"column", "outer_column"}:
        return None
    attributes = dict(expression.attributes)
    if len(attributes) != len(expression.attributes):
        return None
    name = attributes.get("name")
    relation_id = attributes.get("relation_id")
    if type(name) is not str:
        return None
    if relation_id is None and len(relation_tables) == 1:
        return _resolve_column_ref(
            ColumnRef(table=next(iter(relation_tables.values())), column=name),
            allowed_columns,
            dialect,
        )
    if type(relation_id) is str and relation_id in relation_tables:
        return _resolve_column_ref(
            ColumnRef(table=relation_tables[relation_id], column=name),
            allowed_columns,
            dialect,
        )
    return None


def _resolve_column_ref(
    column: ColumnRef,
    allowed_columns: tuple[ColumnRef, ...],
    dialect: str | None,
) -> ColumnRef:
    if dialect != "sqlite":
        return column
    exact = tuple(
        item
        for item in allowed_columns
        if item.table == column.table and item.column == column.column
    )
    if len(exact) == 1:
        return exact[0]
    matches = tuple(
        item
        for item in allowed_columns
        if item.table == column.table and item.column.casefold() == column.column.casefold()
    )
    return matches[0] if len(matches) == 1 else column


_UNRESOLVED = object()


def _predicate_operand(
    expression: ExpressionFact,
    tables: dict[str, TableRef],
    allowed_columns: tuple[ColumnRef, ...] = (),
    dialect: str | None = None,
) -> object:
    column = _column_ref(expression, tables, allowed_columns, dialect)
    return column if column is not None else _literal_value(expression)


def _literal_value(expression: ExpressionFact) -> object:
    attributes = dict(expression.attributes)
    if len(attributes) != len(expression.attributes) or expression.children:
        return _UNRESOLVED
    if expression.kind == "literal":
        value = attributes.get("value")
        is_string = attributes.get("is_string")
        if type(value) is not str or type(is_string) is not bool:
            return _UNRESOLVED
        if is_string:
            return value
        try:
            parsed = int(value)
        except ValueError:
            try:
                parsed = float(value)
            except ValueError:
                return _UNRESOLVED
        return parsed if str(parsed) == value else _UNRESOLVED
    if expression.kind == "boolean" and type(attributes.get("value")) is bool:
        return attributes["value"]
    if expression.kind == "null" and not attributes:
        return None
    return _UNRESOLVED


def _named_children(expression: ExpressionFact) -> dict[str, ExpressionFact]:
    values = {
        argument: child
        for argument, ordinal, child in expression.children
        if ordinal == 0
    }
    return values if len(values) == len(expression.children) else {}


def _only_child(expression: ExpressionFact, argument: str) -> ExpressionFact | None:
    values = _indexed_children(expression, argument)
    return values[0] if len(values) == 1 else None


def _indexed_children(expression: ExpressionFact, argument: str) -> tuple[ExpressionFact, ...]:
    values = tuple(
        (ordinal, child)
        for child_argument, ordinal, child in expression.children
        if child_argument == argument
    )
    if tuple(ordinal for ordinal, _ in values) != tuple(range(len(values))):
        return ()
    return tuple(child for _, child in values)


__all__ = [
    "AstColumnFacts",
    "AstColumnOccurrence",
    "AuthenticatedSemanticAst",
    "authenticate_semantic_ast",
    "build_semantic_ast",
    "collect_ast_columns",
    "predicate_from_expression",
]
