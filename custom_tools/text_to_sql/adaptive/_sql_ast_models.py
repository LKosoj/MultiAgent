"""Неизменяемые модели результата W5-02 без зависимостей от парсера."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias


MAX_AST_NODES = 10_000
MAX_AST_DEPTH = 128

CanonicalScalar: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class AstLimits:
    max_nodes: int
    max_depth: int


class SqlAstErrorCode(StrEnum):
    """Закрытые причины отказа W5-02."""

    DIALECT_UNSUPPORTED = "DIALECT_UNSUPPORTED"
    PARSE_TIMEOUT = "PARSE_TIMEOUT"
    PARSE_FAILED = "PARSE_FAILED"
    MULTI_STATEMENT = "MULTI_STATEMENT"
    SHAPE_UNSUPPORTED = "SHAPE_UNSUPPORTED"


class SqlAstError(ValueError):
    """Типизированный fail-closed отказ разбора SQL."""

    def __init__(self, code: SqlAstErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PredicateLocation(StrEnum):
    JOIN_ON = "join_on"
    WHERE = "where"
    HAVING = "having"


class QueryRole(StrEnum):
    ROOT = "root"
    CTE = "cte"
    DERIVED = "derived"
    SET_LEFT = "set_left"
    SET_RIGHT = "set_right"
    SCALAR_SUBQUERY = "scalar_subquery"
    EXISTS_SUBQUERY = "exists_subquery"
    IN_SUBQUERY = "in_subquery"
    QUANTIFIED_SUBQUERY = "quantified_subquery"


class SetOperationKind(StrEnum):
    UNION = "union"
    INTERSECT = "intersect"
    EXCEPT = "except"


@dataclass(frozen=True, slots=True)
class QualifiedTableName:
    catalog: str | None
    schema: str | None
    name: str


@dataclass(frozen=True, slots=True)
class ExpressionFact:
    """Структурный узел выражения без исходного SQL-текста."""

    kind: str
    attributes: tuple[tuple[str, CanonicalScalar], ...] = ()
    children: tuple[tuple[str, int, ExpressionFact], ...] = ()


@dataclass(frozen=True, slots=True)
class _ScopedFact:
    node_id: str
    scope_id: str


@dataclass(frozen=True, slots=True)
class SelectScopeFact(_ScopedFact):
    parent_scope_id: str | None
    query_role: QueryRole
    distinct: ExpressionFact | None
    options: ExpressionFact | None


@dataclass(frozen=True, slots=True)
class CteFact:
    node_id: str
    declaring_scope_id: str
    cte_id: str
    query_scope_id: str
    recursive: bool
    column_aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TableScanFact(_ScopedFact):
    relation_id: str
    source_alias: str
    table: QualifiedTableName
    column_aliases: tuple[str, ...]
    options: ExpressionFact | None


@dataclass(frozen=True, slots=True)
class CteReferenceFact(_ScopedFact):
    relation_id: str
    source_alias: str
    cte_id: str
    column_aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DerivedRelationFact(_ScopedFact):
    relation_id: str
    source_alias: str
    query_scope_id: str
    lateral: bool
    column_aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExpressionRelationFact(_ScopedFact):
    relation_id: str
    source_alias: str
    expression: ExpressionFact
    input_relation_ids: tuple[str, ...]
    column_aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SetOperationFact(_ScopedFact):
    parent_scope_id: str | None
    query_role: QueryRole
    operation: SetOperationKind
    left_scope_id: str
    right_scope_id: str
    distinct: bool
    options: ExpressionFact | None


@dataclass(frozen=True, slots=True)
class SubqueryRefFact(_ScopedFact):
    child_scope_id: str
    query_role: QueryRole


@dataclass(frozen=True, slots=True)
class JoinFact(_ScopedFact):
    relation_id: str
    left_relation_ids: tuple[str, ...]
    output_visible: bool
    condition: ExpressionFact | None
    using_columns: tuple[str, ...]
    options: ExpressionFact | None


@dataclass(frozen=True, slots=True)
class ProjectionFact(_ScopedFact):
    output_id: str
    expression: ExpressionFact


@dataclass(frozen=True, slots=True)
class PredicateAtomFact:
    node_id: str
    path: tuple[str, ...]
    expression: ExpressionFact


@dataclass(frozen=True, slots=True)
class PredicateFact(_ScopedFact):
    location: PredicateLocation
    owner_node_id: str | None
    expression: ExpressionFact
    atoms: tuple[PredicateAtomFact, ...]


@dataclass(frozen=True, slots=True)
class AggregateFact(_ScopedFact):
    function: str
    distinct: bool
    expression: ExpressionFact


@dataclass(frozen=True, slots=True)
class GroupingFact(_ScopedFact):
    expression: ExpressionFact


@dataclass(frozen=True, slots=True)
class OrderingFact(_ScopedFact):
    expression: ExpressionFact
    descending: bool
    nulls_first: bool | None
    options: ExpressionFact | None


@dataclass(frozen=True, slots=True)
class LimitFact(_ScopedFact):
    count: int | None
    offset: int


@dataclass(frozen=True, slots=True)
class ParsedSqlCandidate:
    candidate_id: str
    dialect: str
    candidate_digest: str
    source_sql_digest: str
    scopes: tuple[SelectScopeFact, ...]
    ctes: tuple[CteFact, ...]
    table_scans: tuple[TableScanFact, ...]
    cte_references: tuple[CteReferenceFact, ...]
    derived_relations: tuple[DerivedRelationFact, ...]
    expression_relations: tuple[ExpressionRelationFact, ...]
    set_operations: tuple[SetOperationFact, ...]
    subquery_refs: tuple[SubqueryRefFact, ...]
    joins: tuple[JoinFact, ...]
    projections: tuple[ProjectionFact, ...]
    predicates: tuple[PredicateFact, ...]
    aggregates: tuple[AggregateFact, ...]
    groupings: tuple[GroupingFact, ...]
    orderings: tuple[OrderingFact, ...]
    limits: tuple[LimitFact, ...]
