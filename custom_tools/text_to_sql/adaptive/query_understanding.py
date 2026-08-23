"""Pure, fail-closed conversion of typed NLU output into ``QuerySpec``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from .models import (
    ExpectedResultShape,
    PredicateOperator,
    QuerySpec,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
)
from .serialization import canonical_digest


class QueryUnderstandingError(ValueError):
    """Base error for invalid typed query-understanding output."""


class QueryUnderstandingDecodeError(QueryUnderstandingError):
    """The response has an unknown, missing, or wrongly typed field."""


class QueryUnderstandingSemanticError(QueryUnderstandingError):
    """A semantic item would make an unsupported schema assertion."""


_RESPONSE_FIELDS = frozenset({"expected_result_shape", "semantic_items"})
_ITEM_FIELDS = frozenset(
    {
        "kind",
        "source_text",
        "normalized_meaning",
        "required",
        "requested_output",
        "exact_physical_predicate",
        "operator",
        "literal_or_reference",
        "status",
    }
)
_NLU_STATUSES = frozenset(
    {
        SemanticItemStatus.UNRESOLVED,
        SemanticItemStatus.AMBIGUOUS,
        SemanticItemStatus.UNSUPPORTED,
    }
)


def understand_query(
    text: str,
    *,
    run_id: str,
    run_incarnation: str,
    response: Mapping[str, object],
) -> QuerySpec:
    """Build a complete partial ``QuerySpec`` without schema or runtime access."""

    if type(text) is not str or not text:
        raise QueryUnderstandingDecodeError("text must be a non-empty string")
    if not isinstance(response, Mapping):
        raise QueryUnderstandingDecodeError("response must be an object")
    _require_exact_keys(response, _RESPONSE_FIELDS, "response")
    query_id = _stable_id("query", {"contract_version": 1, "original_text": text})
    shape = _enum_value(
        ExpectedResultShape, response["expected_result_shape"], "expected_result_shape"
    )
    raw_items = response["semantic_items"]
    if not isinstance(raw_items, Sequence) or isinstance(
        raw_items, (str, bytes, bytearray)
    ):
        raise QueryUnderstandingDecodeError("semantic_items must be an array")

    decoded_items = tuple(
        _decode_item(query_id, raw_item, raw_ordinal)
        for raw_ordinal, raw_item in enumerate(raw_items)
    )
    items = tuple(item for item, _ in decoded_items)
    requested_output_source_ids = tuple(
        sorted(
            item.source_id
            for item, requested_output in decoded_items
            if requested_output
        )
    )
    ordered_items = tuple(
        sorted(
            items,
            key=lambda item: (
                item.kind.value,
                item.source_text,
                item.normalized_meaning or "",
                item.source_id,
            ),
        )
    )
    try:
        return QuerySpec(
            run_id=run_id,
            run_incarnation=run_incarnation,
            revision=0,
            schema_namespace_version=None,
            query_id=query_id,
            original_text=text,
            semantic_items=ordered_items,
            requested_output_source_ids=requested_output_source_ids,
            expected_result_shape=shape,
            global_constraints=(),
        )
    except ValueError as exc:
        raise QueryUnderstandingDecodeError(
            "QuerySpec contract validation failed"
        ) from exc


def _decode_item(
    query_id: str, raw_item: object, raw_ordinal: int
) -> tuple[SemanticItem, bool]:
    if not isinstance(raw_item, Mapping):
        raise QueryUnderstandingDecodeError("semantic item must be an object")
    _require_exact_keys(raw_item, _ITEM_FIELDS, "semantic item")
    kind = _enum_value(SemanticItemKind, raw_item["kind"], "semantic item kind")
    status = _enum_value(SemanticItemStatus, raw_item["status"], "semantic item status")
    if status not in _NLU_STATUSES:
        raise QueryUnderstandingSemanticError(
            "NLU semantic items cannot claim resolved schema bindings"
        )
    source_text = raw_item["source_text"]
    if type(source_text) is not str or not source_text:
        raise QueryUnderstandingDecodeError(
            "semantic item source_text must be non-empty text"
        )
    normalized = raw_item["normalized_meaning"]
    if normalized is not None and (type(normalized) is not str or not normalized):
        raise QueryUnderstandingDecodeError(
            "normalized_meaning must be non-empty text or null"
        )
    required = raw_item["required"]
    if type(required) is not bool:
        raise QueryUnderstandingDecodeError("required must be a boolean")
    requested_output = raw_item["requested_output"]
    if type(requested_output) is not bool:
        raise QueryUnderstandingDecodeError("requested_output must be a boolean")
    if requested_output and not required:
        raise QueryUnderstandingSemanticError(
            "requested_output semantic items must be required"
        )
    exact_physical_predicate = raw_item["exact_physical_predicate"]
    if type(exact_physical_predicate) is not bool:
        raise QueryUnderstandingDecodeError(
            "exact_physical_predicate must be a boolean"
        )
    operator_value = raw_item["operator"]
    operator = (
        None
        if operator_value is None
        else _enum_value(
            PredicateOperator,
            operator_value,
            "semantic item operator",
        )
    )
    if exact_physical_predicate and (
        kind not in {SemanticItemKind.FILTER, SemanticItemKind.TIME}
        or operator is None
    ):
        raise QueryUnderstandingSemanticError(
            "exact_physical_predicate requires FILTER or TIME with an operator"
        )
    literal = _decode_literal(raw_item["literal_or_reference"])
    source_id = _stable_id(
        "semantic",
        {
            "query_id": query_id,
            "kind": kind.value,
            "source_text": source_text,
            "normalized_meaning": normalized,
            "raw_ordinal": raw_ordinal,
        },
    )
    return (
        SemanticItem(
            source_id=source_id,
            kind=kind,
            source_text=source_text,
            normalized_meaning=normalized,
            required=required,
            exact_physical_predicate=exact_physical_predicate,
            operator=operator,
            literal_or_reference=literal,
            status=status,
            binding_ids=(),
        ),
        requested_output,
    )


def _decode_literal(value: object) -> object:
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise QueryUnderstandingSemanticError(
                "literal_or_reference floats must be finite"
            )
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = tuple(value)
        if any(type(item) is float and not math.isfinite(item) for item in values):
            raise QueryUnderstandingSemanticError(
                "literal_or_reference floats must be finite"
            )
        if all(_is_finite_json_scalar(item) for item in values):
            return values
    raise QueryUnderstandingSemanticError(
        "literal_or_reference must be a JSON scalar or scalar array"
    )


def _is_finite_json_scalar(value: object) -> bool:
    return type(value) in {str, int, bool} or (
        type(value) is float and math.isfinite(value)
    )


def _enum_value(enum_type: type[Any], value: object, label: str) -> Any:
    if type(value) is not str:
        raise QueryUnderstandingDecodeError(f"{label} must be text")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise QueryUnderstandingDecodeError(f"{label} is not supported") from exc


def _require_exact_keys(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    if set(value) != expected:
        raise QueryUnderstandingDecodeError(
            f"{label} fields must match the typed contract exactly"
        )


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}:{canonical_digest(value).split(':', 1)[1]}"
