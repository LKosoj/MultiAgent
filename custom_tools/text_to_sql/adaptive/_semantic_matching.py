"""Canonical equality helpers for the narrow W5-04 semantic subset."""

from __future__ import annotations

import math
from typing import TypeAlias

from .models import (
    ColumnRef,
    ExpressionRef,
    JoinEdge,
    JoinType,
    LiteralValue,
    PredicateOperator,
    PredicateRef,
    TableRef,
)


CanonicalKey: TypeAlias = tuple[object, ...]


def canonical_literal_key(value: object) -> CanonicalKey:
    """Preserve SQL literal types without Python bool/int equality leaks."""

    if type(value) is LiteralValue:
        value = value.value
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("bool", value)
    if type(value) is int:
        return ("int", str(value))
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("literal float must be finite")
        return ("float", value.hex())
    if type(value) is str:
        return ("str", value)
    raise TypeError("unsupported literal type")


def canonical_predicate_key(predicate: PredicateRef) -> CanonicalKey:
    """Return a stable exact key; only IN-like lists ignore order/duplicates."""

    if type(predicate) is not PredicateRef:
        raise TypeError("predicate must be PredicateRef")
    right = predicate.right
    if type(right) is tuple:
        literal_keys = tuple(canonical_literal_key(value) for value in right)
        if predicate.operator in {PredicateOperator.IN, PredicateOperator.NOT_IN}:
            literal_keys = tuple(sorted(set(literal_keys)))
        right_key: CanonicalKey = ("literals", literal_keys)
    else:
        right_key = _operand_key(right)
    return (
        "predicate",
        _operand_key(predicate.left),
        predicate.operator.value,
        right_key,
    )


def predicate_matches(
    required: PredicateRef,
    actual: PredicateRef,
    *,
    unordered_between: bool = False,
) -> bool:
    """Compare predicates without aliases, formatting, or lossy coercion."""

    try:
        if canonical_predicate_key(required) == canonical_predicate_key(actual):
            return True
        if (
            unordered_between
            and required.operator is PredicateOperator.BETWEEN
            and actual.operator is PredicateOperator.BETWEEN
            and required.left == actual.left
            and type(required.right) is tuple
            and type(actual.right) is tuple
            and len(required.right) == len(actual.right) == 2
        ):
            return (
                canonical_literal_key(required.right[0])
                == canonical_literal_key(actual.right[1])
                and canonical_literal_key(required.right[1])
                == canonical_literal_key(actual.right[0])
            )
        if (
            required.operator not in {PredicateOperator.EQ, PredicateOperator.NEQ}
            or actual.operator is not required.operator
            or type(required.right) is not ColumnRef
            or type(actual.right) is not ColumnRef
            or type(required.left) is not ColumnRef
            or type(actual.left) is not ColumnRef
        ):
            return False
        return required.left == actual.right and required.right == actual.left
    except (TypeError, ValueError):
        return False


def join_path_matches(
    required_path: tuple[JoinEdge, ...],
    actual_path: tuple[JoinEdge, ...],
) -> bool:
    """Match inner graphs by edge multiset; preserve outer-join sequence."""

    if (
        not required_path
        or len(required_path) != len(actual_path)
        or any(type(edge) is not JoinEdge for edge in (*required_path, *actual_path))
    ):
        return False
    if all(edge.join_type is JoinType.INNER for edge in (*required_path, *actual_path)):
        return sorted(_inner_edge_key(edge) for edge in required_path) == sorted(
            _inner_edge_key(edge) for edge in actual_path
        )
    return required_path == actual_path


def _operand_key(value: object) -> CanonicalKey:
    if value is None:
        return ("none",)
    if type(value) is ColumnRef:
        return ("column", *_column_key(value))
    if type(value) is ExpressionRef:
        return ("expression", value.expression_id, value.expression)
    return ("literal", *canonical_literal_key(value))


def _table_key(table: TableRef) -> CanonicalKey:
    return (
        table.namespace,
        table.schema_name is not None,
        table.schema_name or "",
        table.table,
    )


def _column_key(column: ColumnRef) -> CanonicalKey:
    return (*_table_key(column.table), column.column)


def _inner_edge_key(edge: JoinEdge) -> CanonicalKey:
    endpoints = tuple(
        sorted(
            (
                _column_key(edge.left),
                _column_key(edge.right),
            )
        )
    )
    return (edge.operator.value, edge.join_type.value, endpoints)


__all__ = [
    "canonical_literal_key",
    "canonical_predicate_key",
    "join_path_matches",
    "predicate_matches",
]
