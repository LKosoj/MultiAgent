"""Pure deterministic freshness evaluation for adaptive evidence."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import model_validator

from .models import (
    BindingBase,
    BindingStatus,
    Digest,
    DocumentRef,
    EvidenceRecord,
    EvidenceSourceKind,
    EvidenceValidityScope,
    Hypothesis,
    HypothesisStatus,
    Id,
    NonEmptyText,
    ResearchState,
    StrictModel,
)
from .provenance import (
    MalformedProvenanceError,
    ProvenanceMismatchError,
    read_evidence_provenance,
)


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    REVALIDATION_REQUIRED = "REVALIDATION_REQUIRED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class FreshnessReason(StrEnum):
    VALID = "VALID"
    LEGACY_PROVENANCE = "LEGACY_PROVENANCE"
    MALFORMED_PROVENANCE = "MALFORMED_PROVENANCE"
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
    RUN_CONTEXT_CHANGED = "RUN_CONTEXT_CHANGED"
    SCHEMA_VERSION_CHANGED = "SCHEMA_VERSION_CHANGED"
    SOURCE_VERSION_REQUIRED = "SOURCE_VERSION_REQUIRED"
    SOURCE_VERSION_CHANGED = "SOURCE_VERSION_CHANGED"
    SOURCE_REMOVED = "SOURCE_REMOVED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_EXPIRED = "SOURCE_EXPIRED"
    DATA_SNAPSHOT_CHANGED = "DATA_SNAPSHOT_CHANGED"
    DATA_SNAPSHOT_VALIDATOR_UNAVAILABLE = "DATA_SNAPSHOT_VALIDATOR_UNAVAILABLE"


class DocumentSourceAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    REMOVED = "REMOVED"
    UNAVAILABLE = "UNAVAILABLE"


class DataSnapshotStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class DocumentSourceState(StrictModel):
    document_id: Id
    availability: DocumentSourceAvailability
    source_version: NonEmptyText | None

    @model_validator(mode="after")
    def validate_source_state(self) -> DocumentSourceState:
        if (self.availability is DocumentSourceAvailability.AVAILABLE) != (
            self.source_version is not None
        ):
            raise ValueError("available document source requires source_version")
        return self


class DataSnapshotValidation(StrictModel):
    token: NonEmptyText
    status: DataSnapshotStatus


class FreshnessContext(StrictModel):
    evaluated_at: datetime
    run_id: Id
    run_incarnation: Id
    schema_namespace_version: Digest
    document_sources: tuple[DocumentSourceState, ...] = ()
    data_snapshots: tuple[DataSnapshotValidation, ...] = ()

    @model_validator(mode="after")
    def validate_context(self) -> FreshnessContext:
        if (
            self.evaluated_at.tzinfo is None
            or self.evaluated_at.utcoffset() != UTC.utcoffset(self.evaluated_at)
        ):
            raise ValueError("evaluated_at must be a UTC timestamp")
        document_ids = [item.document_id for item in self.document_sources]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("document source states must be unique")
        tokens = [item.token for item in self.data_snapshots]
        if len(tokens) != len(set(tokens)):
            raise ValueError("data snapshot validations must be unique")
        return self


class FreshnessDecision(StrictModel):
    evidence_id: Id
    status: FreshnessStatus
    reason: FreshnessReason
    evaluated_at: datetime


class FreshnessProjection(StrictModel):
    decisions: tuple[FreshnessDecision, ...]

    @model_validator(mode="after")
    def validate_projection(self) -> FreshnessProjection:
        evidence_ids = [item.evidence_id for item in self.decisions]
        if evidence_ids != sorted(evidence_ids) or len(evidence_ids) != len(
            set(evidence_ids)
        ):
            raise ValueError("freshness decisions must be unique and sorted")
        return self


class FreshnessValidationError(ValueError):
    """A supported update relies on evidence that is not fresh."""


def evaluate_evidence_freshness(
    evidence: EvidenceRecord,
    context: FreshnessContext,
) -> FreshnessDecision:
    if not isinstance(evidence, EvidenceRecord):
        raise TypeError("evidence must be EvidenceRecord")
    if not isinstance(context, FreshnessContext):
        raise TypeError("context must be FreshnessContext")
    try:
        provenance = read_evidence_provenance(evidence)
    except MalformedProvenanceError:
        return _decision(
            evidence,
            context,
            FreshnessStatus.UNAVAILABLE,
            FreshnessReason.MALFORMED_PROVENANCE,
        )
    except ProvenanceMismatchError:
        return _decision(
            evidence,
            context,
            FreshnessStatus.UNAVAILABLE,
            FreshnessReason.PROVENANCE_MISMATCH,
        )
    if provenance is None:
        return _decision(
            evidence,
            context,
            FreshnessStatus.REVALIDATION_REQUIRED,
            FreshnessReason.LEGACY_PROVENANCE,
        )
    if evidence.schema_namespace_version != context.schema_namespace_version:
        return _decision(
            evidence,
            context,
            FreshnessStatus.STALE,
            FreshnessReason.SCHEMA_VERSION_CHANGED,
        )
    if evidence.validity_scope is EvidenceValidityScope.RUN_ONLY:
        if (
            evidence.run_id != context.run_id
            or evidence.run_incarnation != context.run_incarnation
        ):
            return _decision(
                evidence,
                context,
                FreshnessStatus.REVALIDATION_REQUIRED,
                FreshnessReason.RUN_CONTEXT_CHANGED,
            )
        return _decision(
            evidence,
            context,
            FreshnessStatus.FRESH,
            FreshnessReason.VALID,
        )
    if evidence.validity_scope is EvidenceValidityScope.DATA_SNAPSHOT:
        validations = {item.token: item.status for item in context.data_snapshots}
        status = validations.get(evidence.data_snapshot_token)
        if status is None or status is DataSnapshotStatus.UNAVAILABLE:
            return _decision(
                evidence,
                context,
                FreshnessStatus.UNAVAILABLE,
                FreshnessReason.DATA_SNAPSHOT_VALIDATOR_UNAVAILABLE,
            )
        if status is DataSnapshotStatus.INVALID:
            return _decision(
                evidence,
                context,
                FreshnessStatus.STALE,
                FreshnessReason.DATA_SNAPSHOT_CHANGED,
            )
        return _decision(
            evidence,
            context,
            FreshnessStatus.FRESH,
            FreshnessReason.VALID,
        )
    if evidence.source_kind is not EvidenceSourceKind.DOCUMENT:
        return _decision(
            evidence,
            context,
            FreshnessStatus.FRESH,
            FreshnessReason.VALID,
        )
    if provenance.source_version is None or not isinstance(
        evidence.target, DocumentRef
    ):
        return _decision(
            evidence,
            context,
            FreshnessStatus.REVALIDATION_REQUIRED,
            FreshnessReason.SOURCE_VERSION_REQUIRED,
        )
    sources = {item.document_id: item for item in context.document_sources}
    source = sources.get(evidence.target.document_id)
    if source is None or source.availability is DocumentSourceAvailability.UNAVAILABLE:
        return _decision(
            evidence,
            context,
            FreshnessStatus.UNAVAILABLE,
            FreshnessReason.SOURCE_UNAVAILABLE,
        )
    if source.availability is DocumentSourceAvailability.REMOVED:
        return _decision(
            evidence,
            context,
            FreshnessStatus.STALE,
            FreshnessReason.SOURCE_REMOVED,
        )
    if source.source_version != provenance.source_version:
        return _decision(
            evidence,
            context,
            FreshnessStatus.STALE,
            FreshnessReason.SOURCE_VERSION_CHANGED,
        )
    if (
        provenance.valid_until is not None
        and context.evaluated_at >= provenance.valid_until
    ):
        return _decision(
            evidence,
            context,
            FreshnessStatus.REVALIDATION_REQUIRED,
            FreshnessReason.SOURCE_EXPIRED,
        )
    return _decision(
        evidence,
        context,
        FreshnessStatus.FRESH,
        FreshnessReason.VALID,
    )


def project_evidence_freshness(
    evidence: Iterable[EvidenceRecord],
    context: FreshnessContext,
) -> FreshnessProjection:
    records = tuple(evidence)
    if not all(isinstance(item, EvidenceRecord) for item in records):
        raise TypeError("evidence must contain EvidenceRecord values")
    decisions = tuple(
        sorted(
            (evaluate_evidence_freshness(item, context) for item in records),
            key=lambda item: item.evidence_id,
        )
    )
    return FreshnessProjection(decisions=decisions)


def validate_supported_updates(
    state: ResearchState,
    context: FreshnessContext,
    *,
    bindings: Iterable[BindingBase] = (),
    hypotheses: Iterable[Hypothesis] = (),
) -> FreshnessProjection:
    if not isinstance(state, ResearchState):
        raise TypeError("state must be ResearchState")
    if not isinstance(context, FreshnessContext):
        raise TypeError("context must be FreshnessContext")
    projection = project_evidence_freshness(state.evidence, context)
    decisions = {item.evidence_id: item for item in projection.decisions}
    existing_bindings = {item.binding_id: item for item in state.bindings}
    existing_hypotheses = {item.hypothesis_id: item for item in state.hypotheses}
    for binding in bindings:
        if not isinstance(binding, BindingBase):
            raise TypeError("bindings must contain BindingBase values")
        previous = existing_bindings.get(binding.binding_id)
        if binding.status is BindingStatus.SUPPORTED and (
            previous is None or previous != binding
        ):
            _require_fresh(binding.evidence_ids, decisions)
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, Hypothesis):
            raise TypeError("hypotheses must contain Hypothesis values")
        previous = existing_hypotheses.get(hypothesis.hypothesis_id)
        if hypothesis.status is HypothesisStatus.SUPPORTED and (
            previous is None or previous != hypothesis
        ):
            _require_fresh(hypothesis.evidence_ids, decisions)
    return projection


def _require_fresh(
    evidence_ids: tuple[str, ...],
    decisions: dict[str, FreshnessDecision],
) -> None:
    if (
        not evidence_ids
        or len(evidence_ids) != len(set(evidence_ids))
        or any(
            evidence_id not in decisions
            or decisions[evidence_id].status is not FreshnessStatus.FRESH
            for evidence_id in evidence_ids
        )
    ):
        raise FreshnessValidationError("supported update requires fresh evidence")


def _decision(
    evidence: EvidenceRecord,
    context: FreshnessContext,
    status: FreshnessStatus,
    reason: FreshnessReason,
) -> FreshnessDecision:
    return FreshnessDecision(
        evidence_id=evidence.evidence_id,
        status=status,
        reason=reason,
        evaluated_at=context.evaluated_at,
    )


__all__ = [
    "DataSnapshotStatus",
    "DataSnapshotValidation",
    "DocumentSourceAvailability",
    "DocumentSourceState",
    "FreshnessContext",
    "FreshnessDecision",
    "FreshnessProjection",
    "FreshnessReason",
    "FreshnessStatus",
    "FreshnessValidationError",
    "evaluate_evidence_freshness",
    "project_evidence_freshness",
    "validate_supported_updates",
]
