"""Trusted exact-value evidence checks shared by reducer and authority."""

from __future__ import annotations

from .models import (
    ColumnRef,
    EvidenceRecord,
    LiteralValue,
    PredicateOperator,
    PredicateRef,
    ResearchActionKind,
)
from .provenance import parse_probe_observation


class ExactValueCertificateError(ValueError):
    """An evidence observation cannot be read as a trusted value certificate."""


def predicate_has_exact_value_certificate(
    predicate: PredicateRef,
    evidence: tuple[EvidenceRecord, ...],
) -> bool:
    """Require exact evidence for discrete predicates.

    Ordered bounds are validated as literals, not observed row values.
    """

    if type(predicate.left) is not ColumnRef:
        return False
    if predicate.operator is PredicateOperator.IS_NULL:
        return predicate.right is None and any(
            evidence_observes_exact_value(record, predicate.left, None)
            for record in evidence
        )
    if predicate.operator in {
        PredicateOperator.EQ,
    }:
        if predicate.right is None or type(predicate.right) is tuple:
            return False
        values = (predicate.right,)
    elif predicate.operator in {
        PredicateOperator.GT,
        PredicateOperator.GTE,
        PredicateOperator.LT,
        PredicateOperator.LTE,
    }:
        return type(predicate.right) in {LiteralValue, str, int, float, bool} and (
            type(predicate.right) is not LiteralValue or predicate.right.value is not None
        )
    elif predicate.operator is PredicateOperator.IN:
        if type(predicate.right) is not tuple or not predicate.right:
            return False
        values = predicate.right
    elif predicate.operator is PredicateOperator.BETWEEN:
        return (
            type(predicate.right) is tuple
            and len(predicate.right) == 2
            and all(
                type(value) in {LiteralValue, str, int, float, bool}
                and (type(value) is not LiteralValue or value.value is not None)
                for value in predicate.right
            )
        )
    else:
        return False
    return all(
        any(
            evidence_observes_exact_value(record, predicate.left, value)
            for record in evidence
        )
        for value in values
    )


def evidence_observes_exact_column(
    record: EvidenceRecord,
    column: ColumnRef,
) -> bool:
    """Match one inspected physical column in a trusted probe observation."""

    observation = parse_probe_observation(record.observation)
    if observation is None:
        raise ExactValueCertificateError(
            "semantic certificate requires v1 probe observation"
        )
    payload = observation.payload
    return (
        observation.provenance.probe_kind is ResearchActionKind.INSPECT_COLUMN
        and record.target == column
        and isinstance(payload, dict)
        and payload.get("status") == "matched"
        and payload.get("column") == column.model_dump(mode="json", by_alias=True)
    )


def evidence_observes_exact_value(
    record: EvidenceRecord,
    column: ColumnRef,
    value: object,
) -> bool:
    """Match one observed value without coercing its SQL/Python literal type."""

    observation = parse_probe_observation(record.observation)
    if observation is None:
        raise ExactValueCertificateError(
            "semantic certificate requires v1 probe observation"
        )
    payload = observation.payload
    if (
        observation.provenance.probe_kind
        not in {ResearchActionKind.SEARCH_VALUE, ResearchActionKind.DISTINCT_VALUES}
        or record.target != column
        or not isinstance(payload, dict)
        or payload.get("columns") != [column.column]
        or not isinstance(payload.get("rows"), list)
    ):
        return False
    expected = value.value if type(value) is LiteralValue else value
    return any(
        isinstance(row, list)
        and len(row) == 1
        and type(row[0]) is type(expected)
        and row[0] == expected
        for row in payload["rows"]
    )


__all__ = [
    "ExactValueCertificateError",
    "evidence_observes_exact_column",
    "evidence_observes_exact_value",
    "predicate_has_exact_value_certificate",
]
