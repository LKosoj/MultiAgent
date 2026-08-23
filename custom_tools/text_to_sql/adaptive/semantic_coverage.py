"""Pure input readiness checks for semantic coverage evaluation."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
import math
from typing import NoReturn

from pydantic import ValidationError, model_validator

from ._semantic_coverage_boundary import (
    evidence_has_state_authority,
    inspect_identity,
    safe_required_source_ids,
)
from ._semantic_matching import predicate_matches
from ._semantic_coverage_footprint import (
    FootprintError,
    canonical_binding,
    canonical_join,
    covered_join_positions,
    disconnected_binding_source_ids,
    derive_coverage_footprint,
    matching_join_offsets,
    model_payload,
    normalized_freshness_digest,
    normalized_state_digest,
    requirements_digest,
)
from ._semantic_value_certificate import (
    ExactValueCertificateError,
    predicate_has_exact_value_certificate,
    predicate_has_valid_literal,
)
from .freshness import (
    FreshnessContext,
    FreshnessStatus,
    evaluate_evidence_freshness,
)
from .models import (
    Binding,
    BindingStatus,
    ColumnRef,
    DerivedExpressionBinding,
    DiscriminatorValueBinding,
    Digest,
    EvidenceRecord,
    ExpectedResultShape,
    Id,
    JoinCandidate,
    JoinCandidateStatus,
    JoinEdge,
    LiteralValue,
    NonNegativeInt,
    PredicateOperator,
    PredicateRef,
    ResearchState,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    StrictModel,
    TableRef,
    VerticalAttributeBinding,
    is_binding_free_semantic_item,
)
from .serialization import canonical_digest


class CoverageInputErrorCode(StrEnum):
    RESEARCH_STATE_IDENTITY_MISMATCH = "RESEARCH_STATE_IDENTITY_MISMATCH"
    RESEARCH_STATE_INCOMPLETE = "RESEARCH_STATE_INCOMPLETE"
    SCHEMA_NAMESPACE_MISMATCH = "SCHEMA_NAMESPACE_MISMATCH"
    STALE_BINDING_EVIDENCE = "STALE_BINDING_EVIDENCE"
    QUERY_REQUIREMENT_INCOMPLETE = "QUERY_REQUIREMENT_INCOMPLETE"
    AMBIGUOUS_REQUIRED_BINDING = "AMBIGUOUS_REQUIRED_BINDING"


class CoverageInputError(ValueError):
    """A closed readiness failure with only machine-readable authority."""

    def __init__(
        self,
        code: CoverageInputErrorCode,
        affected_source_ids: Iterable[str] = (),
    ) -> None:
        if not isinstance(code, CoverageInputErrorCode):
            raise TypeError("code must be CoverageInputErrorCode")
        self.code = code
        self.affected_source_ids = tuple(sorted(set(affected_source_ids)))
        super().__init__(code.value)


class CoverageRequirements(StrictModel):
    """Immutable inputs authorized for later coverage checks.

    Consumers must bind authorization to ``requirements_digest``. The narrower
    ``state_digest`` excludes freshness context and cannot prevent replay under
    a different context.
    """

    run_id: Id
    run_incarnation: Id
    state_revision: NonNegativeInt
    schema_namespace_version: Digest
    required_source_ids: tuple[Id, ...]
    selected_bindings: tuple[Binding, ...]
    eligible_validated_joins: tuple[JoinCandidate, ...]
    eligible_evidence_ids: tuple[Id, ...]
    allowed_tables: tuple[TableRef, ...]
    allowed_columns: tuple[ColumnRef, ...]
    allowed_predicates: tuple[PredicateRef, ...]
    allowed_join_paths: tuple[tuple[JoinEdge, ...], ...]
    expected_result_shape: ExpectedResultShape
    state_digest: Digest
    freshness_digest: Digest
    requirements_digest: Digest

    @model_validator(mode="after")
    def validate_deterministic_shape(self) -> CoverageRequirements:
        _require_sorted_unique(self.required_source_ids, "required source IDs")
        selected_sources = tuple(item.source_id for item in self.selected_bindings)
        selected_binding_ids = tuple(item.binding_id for item in self.selected_bindings)
        if len(selected_binding_ids) != len(set(selected_binding_ids)):
            raise ValueError("selected binding IDs must be unique")
        if self.selected_bindings != tuple(
            sorted(self.selected_bindings, key=lambda item: (item.source_id, item.binding_id))
        ):
            raise ValueError("selected bindings must use canonical source and binding order")
        if not set(selected_sources).issubset(self.required_source_ids):
            raise ValueError("selected bindings must belong to required sources")
        if any(
            item.status is not BindingStatus.SUPPORTED
            for item in self.selected_bindings
        ):
            raise ValueError("selected bindings must be supported")
        if (
            tuple(canonical_binding(item) for item in self.selected_bindings)
            != self.selected_bindings
        ):
            raise ValueError("selected binding footprints must be canonical")
        footprint = derive_coverage_footprint(
            self.selected_bindings,
            self.eligible_validated_joins,
        )
        if disconnected_binding_source_ids(
            self.selected_bindings,
            self.eligible_validated_joins,
        ):
            raise ValueError("selected bindings must form one validated join component")
        for actual, expected, label in (
            (
                self.eligible_validated_joins,
                footprint.eligible_validated_joins,
                "eligible validated joins",
            ),
            (
                self.eligible_evidence_ids,
                footprint.eligible_evidence_ids,
                "eligible evidence IDs",
            ),
            (self.allowed_tables, footprint.allowed_tables, "allowed tables"),
            (self.allowed_columns, footprint.allowed_columns, "allowed columns"),
            (
                self.allowed_predicates,
                footprint.allowed_predicates,
                "allowed predicates",
            ),
            (
                self.allowed_join_paths,
                footprint.allowed_join_paths,
                "allowed join paths",
            ),
        ):
            if actual != expected:
                raise ValueError(f"{label} must exactly match the derived footprint")
        if self.requirements_digest != requirements_digest(self):
            raise ValueError("requirements_digest does not match requirements")
        return self


def validate_coverage_inputs(
    state: ResearchState,
    freshness_context: FreshnessContext,
    run_id: str,
    run_incarnation: str,
) -> CoverageRequirements:
    """Derive a closed coverage footprint from one fully ready research state.

    ``requirements_digest`` is the authorization identity: it binds the
    normalized research state, freshness context, and derived footprint.
    ``state_digest`` alone is intentionally insufficient for replay checks.
    """

    safe_source_ids = safe_required_source_ids(state)
    try:
        identity = inspect_identity(
            state,
            freshness_context,
            run_id,
            run_incarnation,
            safe_source_ids,
        )
        if identity.invalid:
            _fail(
                CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
                identity.affected_source_ids,
            )
        return _validate_coverage_inputs(
            state,
            freshness_context,
            run_id,
            run_incarnation,
        )
    except CoverageInputError:
        raise
    except Exception:
        raise CoverageInputError(
            CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE,
            safe_source_ids,
        ) from None


def _validate_coverage_inputs(
    state: ResearchState,
    freshness_context: FreshnessContext,
    run_id: str,
    run_incarnation: str,
) -> CoverageRequirements:
    current = _revalidate_state(state, safe_required_source_ids(state))
    context = _revalidate_freshness_context(
        freshness_context, safe_required_source_ids(current)
    )
    required_items = tuple(
        sorted(
            (item for item in current.query_spec.semantic_items if item.required),
            key=lambda item: item.source_id,
        )
    )
    required_source_ids = tuple(item.source_id for item in required_items)

    if (
        type(run_id) is not str
        or type(run_incarnation) is not str
        or current.run_id != run_id
        or current.run_incarnation != run_incarnation
        or context.run_id != run_id
        or context.run_incarnation != run_incarnation
    ):
        _fail(
            CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
            required_source_ids,
        )
    if (
        current.schema_namespace_version != context.schema_namespace_version
        or current.query_spec.schema_namespace_version
        != current.schema_namespace_version
    ):
        _fail(
            CoverageInputErrorCode.SCHEMA_NAMESPACE_MISMATCH,
            required_source_ids,
        )
    if current.query_spec.global_constraints:
        _fail(CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE)

    incomplete_sources = tuple(
        item.source_id
        for item in required_items
        if not is_binding_free_semantic_item(item, current.bindings)
        and (
            item.status is not SemanticItemStatus.RESOLVED
            or item.source_id in current.unresolved_items
            or not item.binding_ids
        )
    )
    if incomplete_sources:
        _fail(CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE, incomplete_sources)
    canonical_bindings: list[Binding] = []
    invalid_binding_sources: list[str] = []
    invalid_binding_found = False
    for binding in current.bindings:
        try:
            canonical_bindings.append(canonical_binding(binding))
        except FootprintError:
            invalid_binding_found = True
            if binding.source_id in required_source_ids:
                invalid_binding_sources.append(binding.source_id)
    if invalid_binding_found:
        _fail(
            CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
            invalid_binding_sources,
        )

    bindings_by_id = {item.binding_id: item for item in canonical_bindings}
    selected: list[Binding] = []
    stale_sources: list[str] = []
    invalid_sources: list[str] = []
    for item in required_items:
        if is_binding_free_semantic_item(item, current.bindings):
            continue
        bindings_for_source = tuple(
            bindings_by_id.get(binding_id) for binding_id in item.binding_ids
        )
        if any(
            binding is None or binding.source_id != item.source_id
            for binding in bindings_for_source
        ):
            invalid_sources.append(item.source_id)
            continue
        if any(binding.status is BindingStatus.STALE for binding in bindings_for_source):
            stale_sources.append(item.source_id)
            continue
        if any(binding.status is not BindingStatus.SUPPORTED for binding in bindings_for_source):
            invalid_sources.append(item.source_id)
            continue
        selected.extend(bindings_for_source)
    if stale_sources:
        _fail(CoverageInputErrorCode.STALE_BINDING_EVIDENCE, stale_sources)
    if invalid_sources:
        _fail(CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE, invalid_sources)

    canonical_joins: list[JoinCandidate] = []
    invalid_join_sources: list[str] = []
    invalid_join_found = False
    for candidate in current.join_candidates:
        try:
            canonical_joins.append(canonical_join(candidate))
        except FootprintError:
            invalid_join_found = True
            invalid_join_sources.extend(
                item.source_id
                for item in selected
                if matching_join_offsets(candidate, item.join_path)
            )
    if invalid_join_found:
        _fail(
            CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
            invalid_join_sources,
        )

    evidence_by_id = {item.evidence_id: item for item in current.evidence}
    binding_citations = {
        item.binding_id: _revalidate_citations(item.evidence_ids, evidence_by_id)
        for item in selected
    }
    binding_authority_mismatch_sources = tuple(
        item.source_id
        for item in selected
        if any(
            not evidence_has_state_authority(evidence, current)
            for evidence in binding_citations[item.binding_id]
        )
    )
    validated_joins_by_binding: dict[
        str, tuple[tuple[JoinCandidate, tuple[int, ...]], ...]
    ] = {}
    for item in selected:
        if not item.join_path:
            continue
        validated_joins_by_binding[item.binding_id] = tuple(
            (candidate, offsets)
            for candidate in canonical_joins
            if candidate.status is JoinCandidateStatus.VALIDATED
            and (offsets := matching_join_offsets(candidate, item.join_path))
        )

    join_citations_by_binding = {
        binding_id: tuple(
            (
                candidate,
                offsets,
                _revalidate_citations(candidate.evidence_ids, evidence_by_id),
            )
            for candidate, offsets in candidates
        )
        for binding_id, candidates in validated_joins_by_binding.items()
    }
    join_authority_mismatch_sources = tuple(
        item.source_id
        for item in selected
        for _, _, evidence_records in join_citations_by_binding.get(item.binding_id, ())
        if any(
            not evidence_has_state_authority(evidence, current)
            for evidence in evidence_records
        )
    )
    authority_mismatch_sources = (
        *binding_authority_mismatch_sources,
        *join_authority_mismatch_sources,
    )
    if authority_mismatch_sources:
        _fail(
            CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
            authority_mismatch_sources,
        )

    freshness: dict[str, bool] = {}
    stale_binding_sources = tuple(
        item.source_id
        for item in selected
        if not _citations_are_current(
            item.evidence_ids,
            binding_citations[item.binding_id],
            freshness,
            context,
        )
    )
    if stale_binding_sources:
        _fail(
            CoverageInputErrorCode.STALE_BINDING_EVIDENCE,
            stale_binding_sources,
        )

    predicate_sources = tuple(
        item.source_id
        for item in required_items
        if item.kind in {SemanticItemKind.FILTER, SemanticItemKind.TIME}
        and any(
            not _binding_proves_required_predicate(
                item,
                bindings_by_id[binding_id],
                binding_citations[binding_id],
            )
            for binding_id in item.binding_ids
        )
    )
    if predicate_sources:
        _fail(
            CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
            predicate_sources,
        )

    eligible_joins: list[JoinCandidate] = []
    incomplete_join_sources: list[str] = []
    stale_join_sources: list[str] = []
    for item in selected:
        required_path = item.join_path
        if not required_path:
            continue
        validated = validated_joins_by_binding[item.binding_id]
        fresh_validated = tuple(
            (candidate, offsets)
            for candidate, offsets, citations in join_citations_by_binding[
                item.binding_id
            ]
            if _citations_are_current(
                candidate.evidence_ids,
                citations,
                freshness,
                context,
            )
        )
        fresh_coverage = covered_join_positions(fresh_validated)
        if len(fresh_coverage) == len(required_path):
            eligible_joins.extend(candidate for candidate, _ in fresh_validated)
            continue
        validated_coverage = covered_join_positions(validated)
        if len(validated_coverage) == len(required_path):
            stale_join_sources.append(item.source_id)
            continue
        incomplete_join_sources.append(item.source_id)
    if stale_join_sources:
        _fail(CoverageInputErrorCode.STALE_BINDING_EVIDENCE, stale_join_sources)
    if incomplete_join_sources:
        _fail(
            CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
            incomplete_join_sources,
        )

    selected_table_digests = {
        canonical_digest(table)
        for binding in selected
        for table in binding.tables
    }
    for candidate in canonical_joins:
        if candidate.status is not JoinCandidateStatus.VALIDATED:
            continue
        if any(
            canonical_digest(table) not in selected_table_digests
            for edge in candidate.path
            for table in (edge.left.table, edge.right.table)
        ):
            continue
        citations = _revalidate_citations(candidate.evidence_ids, evidence_by_id)
        if any(not evidence_has_state_authority(item, current) for item in citations):
            continue
        if _citations_are_current(
            candidate.evidence_ids,
            citations,
            freshness,
            context,
        ):
            eligible_joins.append(candidate)

    selected_bindings = tuple(
        sorted(selected, key=lambda binding: (binding.source_id, binding.binding_id))
    )
    footprint = derive_coverage_footprint(
        selected_bindings,
        tuple(eligible_joins),
    )
    disconnected_sources = disconnected_binding_source_ids(
        selected_bindings,
        footprint.eligible_validated_joins,
    )
    if disconnected_sources:
        _fail(
            CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
            disconnected_sources,
        )
    requirements = {
        "run_id": current.run_id,
        "run_incarnation": current.run_incarnation,
        "state_revision": current.revision,
        "schema_namespace_version": current.schema_namespace_version,
        "required_source_ids": required_source_ids,
        "selected_bindings": selected_bindings,
        "eligible_validated_joins": footprint.eligible_validated_joins,
        "eligible_evidence_ids": footprint.eligible_evidence_ids,
        "allowed_tables": footprint.allowed_tables,
        "allowed_columns": footprint.allowed_columns,
        "allowed_predicates": footprint.allowed_predicates,
        "allowed_join_paths": footprint.allowed_join_paths,
        "expected_result_shape": current.query_spec.expected_result_shape,
        "state_digest": normalized_state_digest(current),
        "freshness_digest": normalized_freshness_digest(context),
    }
    return CoverageRequirements(
        **requirements,
        requirements_digest=canonical_digest(requirements),
    )


def _binding_proves_required_predicate(
    item: SemanticItem,
    binding: Binding,
    citations: tuple[EvidenceRecord, ...],
) -> bool:
    """Reprove concrete binding predicates through exact value evidence."""

    if isinstance(binding, DiscriminatorValueBinding):
        source_predicate = binding.discriminator_predicate
        predicates = binding.predicates
    elif isinstance(binding, VerticalAttributeBinding):
        if item.kind is not SemanticItemKind.FILTER:
            return False
        source_predicate = binding.value_predicate
        predicates = (binding.attribute_name_predicate, source_predicate)
        supported_operators = {
            PredicateOperator.EQ,
            PredicateOperator.IN,
            PredicateOperator.IS_NULL,
        }
    elif type(binding) is DerivedExpressionBinding and not binding.predicates:
        return True
    else:
        return False
    if (
        item.operator is None
        or not _filter_right_is_valid(item.operator, item.literal_or_reference)
        or not _filter_right_is_valid(
            source_predicate.operator,
            source_predicate.right,
        )
        or (
            isinstance(binding, VerticalAttributeBinding)
            and source_predicate.operator not in supported_operators
        )
    ):
        return False
    if isinstance(binding, DiscriminatorValueBinding):
        exact_query_match = False
        if item.exact_physical_predicate:
            try:
                required_predicate = PredicateRef(
                    left=binding.discriminator_column,
                    operator=item.operator,
                    right=item.literal_or_reference,
                )
            except (TypeError, ValueError):
                pass
            else:
                exact_query_match = len(predicates) == 1 and predicate_matches(
                    required_predicate,
                    source_predicate,
                )
        requirement_matches = (
            bool(predicates)
            and predicates[0] == source_predicate
            and source_predicate.left == binding.discriminator_column
            and (
                (item.exact_physical_predicate and exact_query_match)
                or (
                    not item.exact_physical_predicate
                    and item.kind is SemanticItemKind.TIME
                )
                or (
                    not item.exact_physical_predicate
                    and len(predicates) == 1
                    and source_predicate.operator is item.operator
                )
            )
        )
    else:
        try:
            required_predicate = PredicateRef(
                left=source_predicate.left,
                operator=item.operator,
                right=item.literal_or_reference,
            )
        except (TypeError, ValueError):
            return False
        requirement_matches = predicate_matches(required_predicate, source_predicate)
    if isinstance(binding, DiscriminatorValueBinding):
        return requirement_matches and all(
            predicate_has_valid_literal(predicate) for predicate in predicates
        )
    try:
        return requirement_matches and all(
            predicate_has_exact_value_certificate(predicate, citations)
            for predicate in predicates
        )
    except ExactValueCertificateError:
        return False


def _filter_right_is_valid(
    operator: PredicateOperator,
    right: object,
) -> bool:
    """Validate the query-side operand before matching a binding predicate."""

    if operator in {PredicateOperator.IS_NULL, PredicateOperator.IS_NOT_NULL}:
        return right is None
    if operator in {PredicateOperator.IN, PredicateOperator.NOT_IN}:
        return (
            type(right) is tuple
            and bool(right)
            and all(_is_non_null_literal(value) for value in right)
        )
    if operator is PredicateOperator.BETWEEN:
        return (
            type(right) is tuple
            and len(right) == 2
            and all(_is_non_null_literal(value) for value in right)
        )
    return (
        type(right) is not tuple
        and _is_non_null_literal(right)
        and (
            operator is not PredicateOperator.LIKE or type(_literal_value(right)) is str
        )
    )


def _is_non_null_literal(value: object) -> bool:
    value = _literal_value(value)
    return type(value) in {str, int, float, bool} and (
        type(value) is not float or math.isfinite(value)
    )


def _literal_value(value: object) -> object:
    return value.value if type(value) is LiteralValue else value


def _revalidate_state(
    value: ResearchState,
    affected_source_ids: tuple[str, ...],
) -> ResearchState:
    if type(value) is not ResearchState:
        _fail(CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE)
    try:
        return ResearchState.model_validate(model_payload(value))
    except (ValidationError, ValueError, TypeError):
        _fail(CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE, affected_source_ids)


def _revalidate_freshness_context(
    value: FreshnessContext,
    affected_source_ids: tuple[str, ...],
) -> FreshnessContext:
    if type(value) is not FreshnessContext:
        _fail(CoverageInputErrorCode.STALE_BINDING_EVIDENCE, affected_source_ids)
    try:
        return FreshnessContext.model_validate(model_payload(value))
    except (ValidationError, ValueError, TypeError):
        _fail(CoverageInputErrorCode.STALE_BINDING_EVIDENCE, affected_source_ids)


def _revalidate_citations(
    evidence_ids: tuple[str, ...],
    evidence_by_id: dict[str, EvidenceRecord],
) -> tuple[EvidenceRecord, ...]:
    records: list[EvidenceRecord] = []
    for evidence_id in evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            continue
        try:
            records.append(
                EvidenceRecord.model_validate(
                    evidence.model_dump(
                        mode="python",
                        by_alias=True,
                        exclude_none=False,
                        round_trip=True,
                        warnings="error",
                    )
                )
            )
        except (ValidationError, ValueError, TypeError):
            continue
    return tuple(records)


def _evidence_is_current(
    evidence: EvidenceRecord,
    context: FreshnessContext,
) -> bool:
    return (
        evidence.observed_at <= context.evaluated_at
        and evidence.created_at <= context.evaluated_at
        and evaluate_evidence_freshness(evidence, context).status
        is FreshnessStatus.FRESH
    )


def _citations_are_current(
    evidence_ids: tuple[str, ...],
    citations: tuple[EvidenceRecord, ...],
    freshness: dict[str, bool],
    context: FreshnessContext,
) -> bool:
    for evidence in citations:
        if evidence.evidence_id not in freshness:
            freshness[evidence.evidence_id] = _evidence_is_current(evidence, context)
    return bool(
        evidence_ids
        and len(evidence_ids) == len(set(evidence_ids))
        and len(citations) == len(evidence_ids)
        and all(freshness.get(evidence_id, False) for evidence_id in evidence_ids)
    )


def _require_sorted_unique(values: tuple[str, ...], label: str) -> None:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and sorted")


def _fail(
    code: CoverageInputErrorCode,
    affected_source_ids: Iterable[str] = (),
) -> NoReturn:
    raise CoverageInputError(code, affected_source_ids)


__all__ = [
    "CoverageInputError",
    "CoverageInputErrorCode",
    "CoverageRequirements",
    "validate_coverage_inputs",
]
