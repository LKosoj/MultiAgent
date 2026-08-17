"""Публичные модели и входная функция безопасного разбора SQL-кандидата."""

from __future__ import annotations

from ..dialects import get_sqlglot_dialect
from . import _sql_ast_models as _models
from ._sql_ast_models import (
    AggregateFact,
    CteFact,
    CteReferenceFact,
    DerivedRelationFact,
    ExpressionRelationFact,
    ExpressionFact,
    GroupingFact,
    JoinFact,
    LimitFact,
    OrderingFact,
    ParsedSqlCandidate,
    PredicateAtomFact,
    PredicateFact,
    PredicateLocation,
    ProjectionFact,
    QueryRole,
    QualifiedTableName,
    SelectScopeFact,
    SetOperationFact,
    SetOperationKind,
    SqlAstError,
    SqlAstErrorCode,
    SubqueryRefFact,
    TableScanFact,
)
from ._sql_ast_process import parse_candidate_isolated as _parse_candidate_isolated


MAX_AST_NODES = _models.MAX_AST_NODES
MAX_AST_DEPTH = _models.MAX_AST_DEPTH


def parse_sql_candidate(
    sql: str,
    dsn: str,
    candidate_id: str,
) -> ParsedSqlCandidate:
    """Разобрать ровно один SELECT-кандидат, ничего не выполняя."""

    if not isinstance(sql, str):
        raise TypeError("sql must be a string")
    if not isinstance(dsn, str) or not dsn.strip():
        raise SqlAstError(
            SqlAstErrorCode.DIALECT_UNSUPPORTED,
            "explicit non-empty DSN is required",
        )
    if not isinstance(candidate_id, str) or not candidate_id or len(candidate_id) > 256:
        raise ValueError("candidate_id must be non-empty text up to 256 characters")

    try:
        dialect = get_sqlglot_dialect(dsn, strict=True)
    except Exception as exc:
        raise SqlAstError(
            SqlAstErrorCode.DIALECT_UNSUPPORTED,
            "SQL dialect is not configured for the explicit DSN",
        ) from exc

    max_ast_nodes, max_ast_depth = MAX_AST_NODES, MAX_AST_DEPTH
    return _parse_candidate_isolated(
        sql,
        dialect,
        candidate_id,
        max_ast_nodes=max_ast_nodes,
        max_ast_depth=max_ast_depth,
    )


__all__ = [
    "AggregateFact",
    "CteFact",
    "CteReferenceFact",
    "DerivedRelationFact",
    "ExpressionRelationFact",
    "ExpressionFact",
    "GroupingFact",
    "JoinFact",
    "LimitFact",
    "MAX_AST_DEPTH",
    "MAX_AST_NODES",
    "OrderingFact",
    "ParsedSqlCandidate",
    "PredicateAtomFact",
    "PredicateFact",
    "PredicateLocation",
    "ProjectionFact",
    "QueryRole",
    "QualifiedTableName",
    "SelectScopeFact",
    "SetOperationFact",
    "SetOperationKind",
    "SqlAstError",
    "SqlAstErrorCode",
    "SubqueryRefFact",
    "TableScanFact",
    "parse_sql_candidate",
]
