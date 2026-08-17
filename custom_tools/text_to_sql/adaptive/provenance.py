"""Strict nested provenance carried inside evidence observations."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any, Literal

from pydantic import ValidationError, model_validator

from .models import (
    Digest,
    DocumentRef,
    EvidenceRecord,
    EvidenceSourceKind,
    EvidenceValidityScope,
    Id,
    NonEmptyText,
    NonNegativeInt,
    ResearchActionKind,
    StrictModel,
    TargetRef,
)
from .serialization import (
    ArtifactReference,
    canonical_digest,
    canonical_json_bytes,
)


class ProvenanceError(ValueError):
    """Base error for provenance that cannot be trusted."""


class MalformedProvenanceError(ProvenanceError):
    """The observation contains a malformed provenance object."""


class ProvenanceMismatchError(ProvenanceError):
    """Provenance does not match its evidence or observation envelope."""


class ProbeProvenance(StrictModel):
    """Versioned evidence chain derived from a verified probe result."""

    provenance_version: Literal[1]
    run_id: Id
    run_incarnation: Id
    invocation_id: Id
    action_digest: Digest
    probe_kind: ResearchActionKind
    target: TargetRef
    schema_namespace_version: Digest
    payload_digest: Digest
    started_at: datetime
    completed_at: datetime
    source_version: NonEmptyText | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validate_provenance(self) -> ProbeProvenance:
        for field_name in ("started_at", "completed_at", "valid_until"):
            value = getattr(self, field_name)
            if value is not None and (
                value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
            ):
                raise ValueError(f"{field_name} must be a UTC timestamp")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.valid_until is not None and self.source_version is None:
            raise ValueError("valid_until requires source_version")
        return self


class _DocumentSourceMetadata(StrictModel):
    source_version: NonEmptyText
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validate_metadata(self) -> _DocumentSourceMetadata:
        if self.valid_until is not None and (
            self.valid_until.tzinfo is None
            or self.valid_until.utcoffset() != UTC.utcoffset(self.valid_until)
        ):
            raise ValueError("valid_until must be a UTC timestamp")
        return self


def read_document_source_metadata(payload: Any) -> _DocumentSourceMetadata:
    """Read source metadata from a verified document probe payload."""
    if not isinstance(payload, dict) or not isinstance(payload.get("document"), dict):
        raise ValueError("document payload requires source metadata")
    document = payload["document"]
    try:
        return _DocumentSourceMetadata.model_validate_json(
            canonical_json_bytes(
                {
                    "source_version": document.get("source_version"),
                    "valid_until": document.get("valid_until"),
                }
            )
        )
    except (TypeError, ValueError, ValidationError):
        raise ValueError("document payload has invalid source metadata") from None


class ProbeObservationV1(StrictModel):
    """Canonical provenance-bearing evidence observation."""

    observation_version: Literal[1]
    storage: Literal["inline", "artifact"]
    invocation_id: Id
    probe_kind: ResearchActionKind
    provenance: ProbeProvenance
    payload_digest: Digest
    payload: Any
    artifact_reference: ArtifactReference | None
    row_count: NonNegativeInt
    byte_count: NonNegativeInt
    summary: NonEmptyText
    truncated: bool

    @model_validator(mode="after")
    def validate_observation(self) -> ProbeObservationV1:
        if (
            self.invocation_id != self.provenance.invocation_id
            or self.probe_kind is not self.provenance.probe_kind
            or self.payload_digest != self.provenance.payload_digest
        ):
            raise ValueError("observation does not match provenance")
        if self.storage == "inline":
            if self.artifact_reference is not None:
                raise ValueError("inline observation requires only inline payload")
            payload_bytes = canonical_json_bytes(self.payload)
            if (
                len(payload_bytes) != self.byte_count
                or canonical_digest(self.payload) != self.payload_digest
            ):
                raise ValueError("inline payload digest or byte count does not match")
            if self.probe_kind is ResearchActionKind.READ_DOCUMENT:
                metadata = read_document_source_metadata(self.payload)
                if (
                    metadata.source_version != self.provenance.source_version
                    or metadata.valid_until != self.provenance.valid_until
                ):
                    raise ValueError(
                        "document source metadata does not match provenance"
                    )
        else:
            reference = self.artifact_reference
            if self.payload is not None or reference is None:
                raise ValueError(
                    "artifact observation requires only artifact reference"
                )
            if (
                reference.digest != self.payload_digest
                or reference.byte_count != self.byte_count
            ):
                raise ValueError("artifact reference does not match observation")
        return self


_EXPECTED_EVIDENCE_CONTRACT = {
    ResearchActionKind.INSPECT_CATALOG: (
        EvidenceSourceKind.CATALOG,
        EvidenceValidityScope.SCHEMA_VERSION,
    ),
    ResearchActionKind.INSPECT_TABLE: (
        EvidenceSourceKind.SCHEMA,
        EvidenceValidityScope.SCHEMA_VERSION,
    ),
    ResearchActionKind.INSPECT_COLUMN: (
        EvidenceSourceKind.SCHEMA,
        EvidenceValidityScope.SCHEMA_VERSION,
    ),
    ResearchActionKind.INSPECT_RELATIONSHIPS: (
        EvidenceSourceKind.SCHEMA,
        EvidenceValidityScope.SCHEMA_VERSION,
    ),
    ResearchActionKind.PROFILE_COLUMN: (
        EvidenceSourceKind.PROFILE,
        EvidenceValidityScope.RUN_ONLY,
    ),
    ResearchActionKind.SAMPLE_ROWS: (
        EvidenceSourceKind.SAMPLE,
        EvidenceValidityScope.RUN_ONLY,
    ),
    ResearchActionKind.SEARCH_VALUE: (
        EvidenceSourceKind.VALUE_SEARCH,
        EvidenceValidityScope.RUN_ONLY,
    ),
    ResearchActionKind.DISTINCT_VALUES: (
        EvidenceSourceKind.VALUE_SEARCH,
        EvidenceValidityScope.RUN_ONLY,
    ),
    ResearchActionKind.EXECUTE_PROBE: (
        EvidenceSourceKind.PROBE,
        EvidenceValidityScope.RUN_ONLY,
    ),
    ResearchActionKind.READ_DOCUMENT: (
        EvidenceSourceKind.DOCUMENT,
        EvidenceValidityScope.SCHEMA_VERSION,
    ),
}


def evidence_contract_for_probe(
    kind: ResearchActionKind,
) -> tuple[EvidenceSourceKind, EvidenceValidityScope]:
    """Return the fixed evidence kind and lifetime for one probe kind."""
    if not isinstance(kind, ResearchActionKind):
        raise TypeError("kind must be ResearchActionKind")
    return _EXPECTED_EVIDENCE_CONTRACT[kind]


def read_evidence_provenance(evidence: EvidenceRecord) -> ProbeProvenance | None:
    """Return strict provenance, or None for a legacy observation without it."""
    if not isinstance(evidence, EvidenceRecord):
        raise TypeError("evidence must be EvidenceRecord")
    observation = parse_probe_observation(evidence.observation)
    if observation is None:
        return None
    provenance = observation.provenance
    if (
        provenance.run_id != evidence.run_id
        or provenance.run_incarnation != evidence.run_incarnation
        or provenance.invocation_id != evidence.evidence_id
        or provenance.action_digest != evidence.action_digest
        or provenance.target != evidence.target
        or provenance.schema_namespace_version != evidence.schema_namespace_version
        or provenance.completed_at != evidence.observed_at
        or provenance.completed_at != evidence.created_at
        or observation.row_count != evidence.cost.rows
        or observation.byte_count != evidence.cost.bytes
    ):
        raise ProvenanceMismatchError(
            "evidence observation provenance does not match its envelope"
        )
    expected_source_kind, expected_scope = evidence_contract_for_probe(
        provenance.probe_kind
    )
    if (
        evidence.source_kind is not expected_source_kind
        or evidence.validity_scope is not expected_scope
        or (
            provenance.probe_kind is ResearchActionKind.READ_DOCUMENT
            and (
                provenance.source_version is None
                or not isinstance(evidence.target, DocumentRef)
            )
        )
        or (
            provenance.probe_kind is not ResearchActionKind.READ_DOCUMENT
            and (
                provenance.source_version is not None
                or provenance.valid_until is not None
            )
        )
    ):
        raise ProvenanceMismatchError(
            "evidence provenance is incompatible with its source or validity scope"
        )
    return provenance


def parse_probe_observation(observation_text: str) -> ProbeObservationV1 | None:
    """Parse canonical v1 observation, or return None for true legacy data."""
    if type(observation_text) is not str:
        raise TypeError("observation_text must be str")
    try:
        raw = observation_text.encode("utf-8")
    except UnicodeEncodeError:
        raise MalformedProvenanceError(
            "evidence observation is not UTF-8 encodable"
        ) from None
    try:
        observation = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        if observation_text.lstrip().startswith(("{", "[")):
            raise MalformedProvenanceError(
                "evidence observation JSON is malformed"
            ) from None
        return None
    if not isinstance(observation, dict):
        return None
    has_observation_version = "observation_version" in observation
    has_provenance = "provenance" in observation
    if not has_observation_version and not has_provenance:
        return None
    if not has_observation_version or not has_provenance:
        raise MalformedProvenanceError(
            "evidence observation modern markers are incomplete"
        )
    try:
        validated = ProbeObservationV1.model_validate_json(raw)
        if canonical_json_bytes(validated) != raw:
            raise ValueError("observation is not canonical JSON")
        return validated
    except (TypeError, ValueError, ValidationError):
        raise MalformedProvenanceError(
            "evidence observation provenance is malformed"
        ) from None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = [
    "MalformedProvenanceError",
    "ProbeObservationV1",
    "ProbeProvenance",
    "ProvenanceError",
    "ProvenanceMismatchError",
    "evidence_contract_for_probe",
    "parse_probe_observation",
    "read_evidence_provenance",
]
