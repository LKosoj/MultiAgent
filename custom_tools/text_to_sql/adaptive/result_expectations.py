"""Pure derivation of result expectations from closed probe certificates."""

from __future__ import annotations

import math
from typing import TypeAlias

from custom_tools.text_to_sql.schema_metadata import is_not_null

from .models import (
    BindingStatus,
    ColumnRef,
    DiscriminatorValueBinding,
    EvidenceRecord,
    LiteralValue,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    ResultExpectation,
    ResultExpectationKind,
    SemanticItemKind,
    VerticalAttributeBinding,
)
from .policy import canonical_action_digest
from .provenance import (
    MalformedProvenanceError,
    ProvenanceError,
    parse_probe_observation,
    read_evidence_provenance,
)
from .serialization import CanonicalJsonError, canonical_json_bytes


class ResultExpectationCertificateError(ValueError):
    """A claimed result certificate is not a closed standard probe fact."""


JsonScalar: TypeAlias = str | int | float | bool | None
_MAX_STANDARD_DATA_PROBE_TOP_K = 50


def derive_result_expectations(
    state: ResearchState,
    action: ResearchAction,
    evidence: EvidenceRecord,
) -> tuple[ResultExpectation, ...]:
    """Return only result facts directly proved by one standard probe payload."""

    _validate_identity(state, action, evidence)
    try:
        provenance = read_evidence_provenance(evidence)
        observation = parse_probe_observation(evidence.observation)
    except (MalformedProvenanceError, ProvenanceError, TypeError, ValueError) as exc:
        raise ResultExpectationCertificateError(
            "result expectation evidence provenance is invalid"
        ) from exc
    if provenance is None or observation is None or observation.storage != "inline":
        return ()
    if (
        observation.probe_kind is not action.kind
        or provenance.action_digest != action.action_digest
        or provenance.target != action.target
        or provenance.schema_namespace_version != state.schema_namespace_version
    ):
        raise ResultExpectationCertificateError(
            "result expectation observation contradicts its producer"
        )
    if type(action.target) is not ColumnRef:
        return ()

    if action.kind is ResearchActionKind.SEARCH_VALUE:
        return _derive_filter_absent(state, action, evidence, observation.payload, observation)
    if action.kind is ResearchActionKind.DISTINCT_VALUES:
        return _derive_value_domain(state, action, evidence, observation.payload, observation)
    if action.kind is ResearchActionKind.INSPECT_COLUMN:
        return _derive_column_schema(state, action, evidence, observation.payload)
    return ()


def _validate_identity(
    state: ResearchState,
    action: ResearchAction,
    evidence: EvidenceRecord,
) -> None:
    if not isinstance(state, ResearchState):
        raise ResultExpectationCertificateError("research state has an invalid type")
    if not isinstance(action, ResearchAction):
        raise ResultExpectationCertificateError("research action has an invalid type")
    if not isinstance(evidence, EvidenceRecord):
        raise ResultExpectationCertificateError("evidence has an invalid type")
    try:
        checked_state = ResearchState.model_validate(
            state.model_dump(mode="python", round_trip=True)
        )
        checked_action = ResearchAction.model_validate(
            action.model_dump(mode="python", round_trip=True)
        )
        checked_evidence = EvidenceRecord.model_validate(
            evidence.model_dump(mode="python", round_trip=True)
        )
    except (TypeError, ValueError) as exc:
        raise ResultExpectationCertificateError(
            "result expectation inputs violate their strict contracts"
        ) from exc
    if checked_action.action_digest != canonical_action_digest(
        kind=checked_action.kind,
        hypothesis_id=checked_action.hypothesis_id,
        target=checked_action.target,
        parameters=checked_action.parameters,
        expected_revision=checked_action.expected_revision,
    ):
        raise ResultExpectationCertificateError("research action digest is not canonical")
    recorded = next(
        (
            item
            for item in checked_state.evidence
            if item.evidence_id == checked_evidence.evidence_id
        ),
        None,
    )
    if (
        (recorded is not None and recorded != checked_evidence)
        or (
            recorded is None
            and (
                checked_action.expected_revision != checked_state.revision
                or checked_evidence.revision != checked_state.revision + 1
            )
        )
        or checked_evidence.run_id != checked_state.run_id
        or checked_evidence.run_incarnation != checked_state.run_incarnation
        or checked_evidence.schema_namespace_version
        != checked_state.schema_namespace_version
        or checked_evidence.action_digest != checked_action.action_digest
        or checked_evidence.target != checked_action.target
    ):
        raise ResultExpectationCertificateError(
            "result expectation evidence does not match state and action"
        )


def _derive_filter_absent(
    state: ResearchState,
    action: ResearchAction,
    evidence: EvidenceRecord,
    payload: object,
    observation: object,
) -> tuple[ResultExpectation, ...]:
    target = action.target
    if (
        len(action.parameters) != 2
        or action.parameters[0][0] != "top_k"
        or action.parameters[1][0] != "value"
    ):
        raise ResultExpectationCertificateError("search-value action parameters are invalid")
    top_k, value = action.parameters[0][1], action.parameters[1][1]
    if (
        type(top_k) is not int
        or not 1 <= top_k <= _MAX_STANDARD_DATA_PROBE_TOP_K
        or not _is_json_scalar(value)
    ):
        raise ResultExpectationCertificateError("search-value action parameters are invalid")
    rows = _standard_data_rows(payload, action, target, state.schema_namespace_version)
    if rows is None or getattr(observation, "truncated", None) is not False:
        return ()
    _validate_one_column_rows(rows)
    if getattr(observation, "row_count", None) != len(rows):
        raise ResultExpectationCertificateError("search-value row count is invalid")
    if rows:
        return ()
    source_ids = _filter_source_ids(state, target, value)
    return _expectations(
        source_ids,
        evidence.evidence_id,
        ResultExpectationKind.FILTER_MATCH_ABSENT,
        target,
    )


def _derive_value_domain(
    state: ResearchState,
    action: ResearchAction,
    evidence: EvidenceRecord,
    payload: object,
    observation: object,
) -> tuple[ResultExpectation, ...]:
    target = action.target
    if not _has_exact_parameters(action, (("top_k", int),)):
        raise ResultExpectationCertificateError("distinct-values action parameters are invalid")
    top_k = action.parameters[0][1]
    if type(top_k) is not int or not 1 <= top_k <= _MAX_STANDARD_DATA_PROBE_TOP_K:
        raise ResultExpectationCertificateError("distinct-values top_k is invalid")
    rows = _standard_data_rows(payload, action, target, state.schema_namespace_version)
    if rows is None or getattr(observation, "truncated", None) is not False:
        return ()
    if getattr(observation, "row_count", None) != len(rows):
        raise ResultExpectationCertificateError("distinct-values row count is invalid")
    values = _domain_values(rows)
    if not values:
        return ()
    source_ids = _physical_source_ids(state, target)
    return _expectations(
        source_ids,
        evidence.evidence_id,
        ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN,
        target,
        allowed_values=values,
    )


def _derive_column_schema(
    state: ResearchState,
    action: ResearchAction,
    evidence: EvidenceRecord,
    payload: object,
) -> tuple[ResultExpectation, ...]:
    target = action.target
    if action.parameters:
        raise ResultExpectationCertificateError("inspect-column action parameters are invalid")
    if not isinstance(payload, dict):
        raise ResultExpectationCertificateError("inspect-column payload is invalid")
    if payload.get("schema_namespace_version") != state.schema_namespace_version:
        raise ResultExpectationCertificateError(
            "inspect-column payload schema version is invalid"
        )
    if payload.get("status") != "matched":
        return ()
    if payload.get("column") != target.model_dump(mode="json", by_alias=True):
        raise ResultExpectationCertificateError("inspect-column payload target is invalid")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ResultExpectationCertificateError("inspect-column metadata is invalid")
    source_ids = _physical_source_ids(state, target)
    expectations: list[ResultExpectation] = []
    if is_not_null({"not_null": metadata.get("not_null")}):
        expectations.extend(
            _expectations(
                source_ids,
                evidence.evidence_id,
                ResultExpectationKind.DIRECT_OUTPUT_NOT_NULL,
                target,
            )
        )
    if metadata.get("is_primary_key") is True:
        expectations.extend(
            _expectations(
                source_ids,
                evidence.evidence_id,
                ResultExpectationKind.DIRECT_OUTPUT_PRIMARY_KEY_UNIQUE,
                target,
            )
        )
    return tuple(expectations)


def _standard_data_rows(
    payload: object,
    action: ResearchAction,
    target: ColumnRef,
    schema_namespace_version: str,
) -> list[object] | None:
    if not isinstance(payload, dict):
        raise ResultExpectationCertificateError("data certificate payload is invalid")
    if (
        payload.get("probe_kind") != action.kind.value
        or payload.get("schema_namespace_version") != schema_namespace_version
        or payload.get("target") != target.model_dump(mode="json", by_alias=True)
        or payload.get("columns") != [target.column]
    ):
        raise ResultExpectationCertificateError("data certificate payload contradicts action")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ResultExpectationCertificateError("data certificate rows are invalid")
    return rows


def _validate_one_column_rows(rows: list[object]) -> None:
    for row in rows:
        if not isinstance(row, list) or len(row) != 1 or not _is_json_scalar(row[0]):
            raise ResultExpectationCertificateError("data certificate row is invalid")


def _domain_values(rows: list[object]) -> tuple[JsonScalar, ...]:
    _validate_one_column_rows(rows)
    values = tuple(row[0] for row in rows)
    if any(value is None for value in values):
        raise ResultExpectationCertificateError("distinct-values certificate contains null")
    try:
        encoded = tuple(canonical_json_bytes(value) for value in values)
    except CanonicalJsonError as exc:
        raise ResultExpectationCertificateError(
            "distinct-values certificate contains a non-canonical value"
        ) from exc
    if len(encoded) != len(set(encoded)):
        raise ResultExpectationCertificateError("distinct-values certificate repeats a value")
    return tuple(value for _, value in sorted(zip(encoded, values, strict=True)))


def _has_exact_parameters(
    action: ResearchAction,
    shape: tuple[tuple[str, type[object]], ...],
) -> bool:
    return len(action.parameters) == len(shape) and all(
        key == expected_key and type(value) is expected_type
        for (key, value), (expected_key, expected_type) in zip(
            action.parameters, shape, strict=True
        )
    )


def _is_json_scalar(value: object) -> bool:
    return (
        value is None
        or type(value) in {str, int, bool}
        or type(value) is float and math.isfinite(value)
    )


def _physical_source_ids(
    state: ResearchState,
    column: ColumnRef,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            binding.source_id
            for binding in state.bindings
            if type(binding) is PhysicalColumnBinding
            and binding.status is BindingStatus.SUPPORTED
            and binding.physical_column == column
        )
    )


def _filter_source_ids(
    state: ResearchState,
    column: ColumnRef,
    value: JsonScalar,
) -> tuple[str, ...]:
    items = {item.source_id: item for item in state.query_spec.semantic_items}
    source_ids: list[str] = []
    for binding in state.bindings:
        predicate = _filter_predicate(binding)
        if (
            predicate is not None
            and binding.status is BindingStatus.SUPPORTED
            and items[binding.source_id].kind is SemanticItemKind.FILTER
            and _predicate_matches(predicate, column, value)
        ):
            source_ids.append(binding.source_id)
    return tuple(sorted(set(source_ids)))


def _filter_predicate(binding: object) -> PredicateRef | None:
    if type(binding) is DiscriminatorValueBinding:
        return binding.discriminator_predicate
    if type(binding) is VerticalAttributeBinding:
        return binding.value_predicate
    return None


def _predicate_matches(
    predicate: PredicateRef,
    column: ColumnRef,
    value: JsonScalar,
) -> bool:
    if predicate.left != column:
        return False
    if predicate.operator is PredicateOperator.IS_NULL:
        return predicate.right is None and value is None
    if predicate.operator is not PredicateOperator.EQ or predicate.right is None:
        return False
    expected = predicate.right.value if type(predicate.right) is LiteralValue else predicate.right
    return _is_json_scalar(expected) and type(expected) is type(value) and expected == value


def _expectations(
    source_ids: tuple[str, ...],
    evidence_id: str,
    kind: ResultExpectationKind,
    column: ColumnRef,
    *,
    allowed_values: tuple[JsonScalar, ...] = (),
) -> tuple[ResultExpectation, ...]:
    return tuple(
        ResultExpectation(
            source_id=source_id,
            evidence_id=evidence_id,
            kind=kind,
            column=column,
            allowed_values=allowed_values,
        )
        for source_id in source_ids
    )


__all__ = [
    "ResultExpectationCertificateError",
    "derive_result_expectations",
]
