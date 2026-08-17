"""Total identity inspection for untrusted semantic coverage inputs."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from ._semantic_coverage_footprint import (
    BINDING_TYPES,
    matching_join_offsets,
    model_payload,
)
from .freshness import FreshnessContext
from .models import (
    Binding,
    BindingStatus,
    Digest,
    EvidenceRecord,
    Id,
    JoinCandidate,
    JoinCandidateStatus,
    NonNegativeInt,
    QuerySpec,
    ResearchState,
    SemanticItem,
    StrictModel,
)
from .provenance import ProvenanceError, read_evidence_provenance


class _RunIdentity(StrictModel):
    run_id: Id
    run_incarnation: Id


class _AuthorityIdentity(_RunIdentity):
    revision: NonNegativeInt
    schema_namespace_version: Digest


class _EvidenceIdentity(_AuthorityIdentity):
    evidence_id: Id
    action_digest: Digest


@dataclass(frozen=True, slots=True)
class IdentityInspection:
    invalid: bool
    affected_source_ids: tuple[str, ...]


def safe_required_source_ids(value: object) -> tuple[str, ...]:
    try:
        if type(value) is not ResearchState or type(value.query_spec) is not QuerySpec:
            return ()
        if type(value.query_spec.semantic_items) is not tuple:
            return ()
        source_ids = []
        for raw_item in value.query_spec.semantic_items:
            if type(raw_item) is not SemanticItem:
                continue
            try:
                item = SemanticItem.model_validate(model_payload(raw_item))
            except (ValidationError, ValueError, TypeError):
                source_id = raw_item.source_id
                required = raw_item.required
                if type(source_id) is str and source_id and required is True:
                    source_ids.append(source_id)
                continue
            if item.required:
                source_ids.append(item.source_id)
        return tuple(sorted(set(source_ids)))
    except Exception:
        return ()


def inspect_identity(
    state: object,
    context: object,
    run_id: object,
    run_incarnation: object,
    safe_source_ids: tuple[str, ...],
) -> IdentityInspection:
    invalid = IdentityInspection(True, safe_source_ids)
    if not _run_identity_is_valid(run_id, run_incarnation):
        return invalid
    if type(state) is ResearchState:
        if not _authority_identity_is_valid(state):
            return invalid
        if state.run_id != run_id or state.run_incarnation != run_incarnation:
            return invalid
        query = state.query_spec
        if type(query) is QuerySpec:
            if not _authority_identity_is_valid(query):
                return invalid
            if (
                query.run_id != state.run_id
                or query.run_incarnation != state.run_incarnation
                or query.revision > state.revision
            ):
                return invalid
    if type(context) is FreshnessContext:
        if not _context_identity_is_valid(context):
            return invalid
        if context.run_id != run_id or context.run_incarnation != run_incarnation:
            return invalid
        if type(state) is ResearchState and (
            context.run_id != state.run_id
            or context.run_incarnation != state.run_incarnation
        ):
            return invalid
    if type(state) is ResearchState:
        evidence_invalid, affected = _evidence_identity_mismatch_sources(state)
        if evidence_invalid:
            return IdentityInspection(True, affected)
    return IdentityInspection(False, ())


def evidence_has_state_authority(
    evidence: EvidenceRecord,
    state: ResearchState,
) -> bool:
    if (
        evidence.run_id != state.run_id
        or evidence.run_incarnation != state.run_incarnation
        or evidence.revision > state.revision
        or evidence.schema_namespace_version != state.schema_namespace_version
    ):
        return False
    try:
        provenance = read_evidence_provenance(evidence)
    except (ProvenanceError, TypeError, ValueError):
        return False
    if provenance is None:
        return True
    return (
        provenance.run_id == state.run_id
        and provenance.run_incarnation == state.run_incarnation
        and provenance.schema_namespace_version == state.schema_namespace_version
    )


def _run_identity_is_valid(run_id: object, run_incarnation: object) -> bool:
    try:
        _RunIdentity(run_id=run_id, run_incarnation=run_incarnation)
    except (ValidationError, ValueError, TypeError):
        return False
    return True


def _authority_identity_is_valid(value: object) -> bool:
    try:
        _AuthorityIdentity(
            run_id=value.run_id,
            run_incarnation=value.run_incarnation,
            revision=value.revision,
            schema_namespace_version=value.schema_namespace_version,
        )
    except (AttributeError, ValidationError, ValueError, TypeError):
        return False
    return True


def _context_identity_is_valid(context: FreshnessContext) -> bool:
    try:
        _AuthorityIdentity(
            run_id=context.run_id,
            run_incarnation=context.run_incarnation,
            revision=0,
            schema_namespace_version=context.schema_namespace_version,
        )
    except (AttributeError, ValidationError, ValueError, TypeError):
        return False
    return True


def _evidence_identity_mismatch_sources(
    state: ResearchState,
) -> tuple[bool, tuple[str, ...]]:
    selected = _safe_selected_bindings(state)
    if type(state.evidence) is not tuple:
        return False, ()
    authority_by_id: dict[str, bool | None] = {}
    identity_invalid = False
    for evidence in state.evidence:
        if type(evidence) is not EvidenceRecord:
            continue
        if not _evidence_identity_is_valid(evidence):
            identity_invalid = True
        authority = _raw_evidence_has_state_authority(evidence, state)
        if type(evidence.evidence_id) is str:
            authority_by_id[evidence.evidence_id] = authority
    mismatch_sources = {
        source_id
        for source_id, binding in selected
        for evidence_id in binding.evidence_ids
        if authority_by_id.get(evidence_id) is False
    }
    if mismatch_sources:
        identity_invalid = True
    if type(state.join_candidates) is tuple:
        for raw_candidate in state.join_candidates:
            candidate = _strict_join_shape(raw_candidate)
            if (
                candidate is None
                or candidate.status is not JoinCandidateStatus.VALIDATED
            ):
                continue
            affected = tuple(
                source_id
                for source_id, binding in selected
                if matching_join_offsets(candidate, binding.join_path)
            )
            if affected and any(
                authority_by_id.get(evidence_id) is False
                for evidence_id in candidate.evidence_ids
            ):
                mismatch_sources.update(affected)
                identity_invalid = True
    return identity_invalid, tuple(sorted(mismatch_sources))


def _safe_selected_bindings(
    state: ResearchState,
) -> tuple[tuple[str, Binding], ...]:
    try:
        query = state.query_spec
        if (
            type(query) is not QuerySpec
            or type(query.semantic_items) is not tuple
            or type(state.bindings) is not tuple
        ):
            return ()
        bindings = {}
        for raw_binding in state.bindings:
            binding = _strict_binding_shape(raw_binding)
            if binding is not None:
                bindings[binding.binding_id] = binding
        selected = []
        for raw_item in query.semantic_items:
            if type(raw_item) is not SemanticItem:
                continue
            item = SemanticItem.model_validate(model_payload(raw_item))
            if not item.required:
                continue
            for binding_id in item.binding_ids:
                binding = bindings.get(binding_id)
                if (
                    binding is not None
                    and binding.source_id == item.source_id
                    and binding.status is BindingStatus.SUPPORTED
                ):
                    selected.append((item.source_id, binding))
        return tuple(selected)
    except Exception:
        return ()


def _strict_binding_shape(value: object) -> Binding | None:
    if type(value) not in BINDING_TYPES:
        return None
    try:
        return type(value).model_validate(model_payload(value))
    except (ValidationError, ValueError, TypeError):
        return None


def _strict_join_shape(value: object) -> JoinCandidate | None:
    if type(value) is not JoinCandidate:
        return None
    try:
        return JoinCandidate.model_validate(model_payload(value))
    except (ValidationError, ValueError, TypeError):
        return None


def _raw_evidence_has_state_authority(
    evidence: EvidenceRecord,
    state: ResearchState,
) -> bool | None:
    if not _evidence_identity_is_valid(evidence):
        return False
    try:
        checked = EvidenceRecord.model_validate(model_payload(evidence))
    except (ValidationError, ValueError, TypeError):
        return None
    return evidence_has_state_authority(checked, state)


def _evidence_identity_is_valid(evidence: EvidenceRecord) -> bool:
    try:
        _EvidenceIdentity(
            run_id=evidence.run_id,
            run_incarnation=evidence.run_incarnation,
            revision=evidence.revision,
            schema_namespace_version=evidence.schema_namespace_version,
            evidence_id=evidence.evidence_id,
            action_digest=evidence.action_digest,
        )
    except (AttributeError, ValidationError, ValueError, TypeError):
        return False
    return True


__all__ = [
    "IdentityInspection",
    "evidence_has_state_authority",
    "inspect_identity",
    "safe_required_source_ids",
]
