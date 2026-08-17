"""Single trusted conversion from successful probe results to evidence."""

from __future__ import annotations

from pydantic import ValidationError

from .models import (
    EvidenceRecord,
    EvidenceSourceKind,
    ResearchAction,
    ResearchActionKind,
)
from .probes import ArtifactReader, ProbeResult, ProbeStatus, probe_observation_json
from .provenance import evidence_contract_for_probe


class ProbeEvidenceError(ValueError):
    """A probe result cannot be trusted as evidence for the producer action."""


def evidence_source_kind_for_action(kind: ResearchActionKind) -> EvidenceSourceKind:
    """Return the only evidence source compatible with an action kind."""

    if not isinstance(kind, ResearchActionKind):
        raise TypeError("kind must be ResearchActionKind")
    return evidence_contract_for_probe(kind)[0]


def probe_result_to_evidence(
    result: ProbeResult,
    producer_action: ResearchAction,
    *,
    read_artifact: ArtifactReader | None = None,
) -> EvidenceRecord | None:
    """Create evidence from one valid success; all other statuses return None."""

    checked_result = _revalidate_result(result)
    if checked_result.status is not ProbeStatus.SUCCESS:
        return None
    checked_action = _revalidate_action(producer_action)
    if checked_result.action_digest != checked_action.action_digest:
        raise ProbeEvidenceError(
            "probe result action_digest does not match producer action"
        )
    if checked_result.revision != checked_action.expected_revision:
        raise ProbeEvidenceError("probe result revision does not match producer action")
    if checked_result.probe_kind is not checked_action.kind:
        raise ProbeEvidenceError("probe kind does not match producer action kind")
    if checked_result.target != checked_action.target:
        raise ProbeEvidenceError("probe target does not match producer action target")

    observation = probe_observation_json(checked_result, read_artifact=read_artifact)
    source_kind, validity_scope = evidence_contract_for_probe(checked_result.probe_kind)
    try:
        return EvidenceRecord(
            run_id=checked_result.run_id,
            run_incarnation=checked_result.run_incarnation,
            revision=checked_result.revision + 1,
            schema_namespace_version=checked_result.schema_namespace_version,
            evidence_id=checked_result.invocation_id,
            source_kind=source_kind,
            target=checked_result.target,
            action_digest=checked_result.action_digest,
            observation=observation,
            validity_scope=validity_scope,
            data_snapshot_token=None,
            observed_at=checked_result.completed_at,
            strength=1.0,
            created_at=checked_result.completed_at,
            cost=checked_result.cost,
        )
    except ValidationError as exc:
        raise ProbeEvidenceError("probe result cannot form valid evidence") from exc


def _revalidate_result(result: ProbeResult) -> ProbeResult:
    if not isinstance(result, ProbeResult):
        raise TypeError("result must be ProbeResult")
    try:
        return ProbeResult.model_validate(
            result.model_dump(mode="python", round_trip=True)
        )
    except ValidationError:
        raise ProbeEvidenceError("probe result violates its strict contract") from None


def _revalidate_action(action: ResearchAction) -> ResearchAction:
    if not isinstance(action, ResearchAction):
        raise TypeError("producer_action must be ResearchAction")
    try:
        return ResearchAction.model_validate(
            action.model_dump(mode="python", round_trip=True)
        )
    except ValidationError:
        raise ProbeEvidenceError(
            "producer action violates its strict contract"
        ) from None
