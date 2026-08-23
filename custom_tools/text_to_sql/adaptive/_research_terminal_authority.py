"""Pure authority checks for durable research terminal replay."""

from __future__ import annotations

from ._policy_authority import (
    ResearchGenerationAuthority,
    evaluate_research_generation_authority,
)
from .ambiguity import AmbiguityReport
from ._semantic_coverage_footprint import (
    FootprintError,
    disconnected_binding_source_ids,
)
from .freshness import FreshnessContext, FreshnessStatus, evaluate_evidence_freshness
from .models import (
    BindingStatus,
    JoinCandidateStatus,
    ResearchState,
    ResearchStopReason,
    is_binding_free_semantic_item,
)
from .semantic_coverage import CoverageInputErrorCode


def _terminal_envelope(action: object, state: ResearchState) -> dict[str, object]:
    if not isinstance(action, dict) or set(action) != {
        "affected_source_ids",
        "ambiguity",
        "citation_evidence_ids",
        "contract_version",
        "kind",
        "rejection_signatures",
        "reason",
    }:
        raise ValueError("terminal envelope has an invalid shape")
    if action["contract_version"] != 2 or action["kind"] != "research_terminal":
        raise ValueError("terminal envelope has an invalid contract")
    affected = action["affected_source_ids"]
    citations = action["citation_evidence_ids"]
    rejection_signatures = action["rejection_signatures"]
    if (
        not isinstance(action["reason"], str)
        or not isinstance(affected, list)
        or not isinstance(citations, list)
        or not isinstance(rejection_signatures, list)
        or not all(type(item) is str for item in affected)
        or not all(type(item) is str for item in citations)
        or affected != sorted(set(affected))
        or citations != sorted(set(citations))
        or any(
            not isinstance(item, list)
            or len(item) != 2
            or not all(type(value) is str and value for value in item)
            for item in rejection_signatures
        )
        or rejection_signatures
        != [list(item) for item in sorted({tuple(item) for item in rejection_signatures})]
    ):
        raise ValueError("terminal envelope has invalid reason")
    ResearchStopReason(action["reason"])
    reason = ResearchStopReason(action["reason"])
    if reason is not ResearchStopReason.STAGNATED and rejection_signatures:
        raise ValueError("only stagnated terminals may have rejection signatures")
    raw_ambiguity = action["ambiguity"]
    if (reason is ResearchStopReason.AMBIGUOUS) != (raw_ambiguity is not None):
        raise ValueError("terminal envelope ambiguity must match its reason")
    ambiguity = (
        None
        if raw_ambiguity is None
        else AmbiguityReport.model_validate(raw_ambiguity)
    )
    if ambiguity is not None and ambiguity.citation_evidence_ids != tuple(citations):
        raise ValueError("terminal envelope ambiguity citations must match")
    required_source_ids = {
        item.source_id for item in state.query_spec.semantic_items if item.required
    }
    evidence_ids = {item.evidence_id for item in state.evidence}
    if not set(affected).issubset(required_source_ids) or not set(citations).issubset(
        evidence_ids
    ):
        raise ValueError("terminal envelope references unknown state entities")
    return {**action, "ambiguity": ambiguity}


def _terminal_replay_is_authorized(
    state: ResearchState,
    freshness_context: FreshnessContext,
    reason: ResearchStopReason,
    terminal: dict[str, object],
) -> bool:
    authority = evaluate_research_generation_authority(
        state,
        freshness_context,
        state.run_id,
        state.run_incarnation,
    )
    if _authority_stop_reason(authority) is ResearchStopReason.PROTOCOL_FAILURE:
        return False
    affected = tuple(terminal["affected_source_ids"])
    citations = tuple(terminal["citation_evidence_ids"])
    ambiguity = terminal["ambiguity"]
    if reason is ResearchStopReason.AMBIGUOUS:
        if type(ambiguity) is not AmbiguityReport:
            return False
        if ambiguity.citation_evidence_ids != citations:
            return False
        evidence = {item.evidence_id: item for item in state.evidence}
        if any(
            evidence_id not in evidence
            or evaluate_evidence_freshness(evidence[evidence_id], freshness_context).status
            is not FreshnessStatus.FRESH
            for evidence_id in citations
        ):
            return False
    if reason is ResearchStopReason.COMPLETE:
        return authority.allowed and affected == _canonical_outcome_affected_source_ids(
            state, reason
        )
    if reason in {ResearchStopReason.AMBIGUOUS, ResearchStopReason.UNSUPPORTED}:
        return (
            not authority.allowed
            and affected == _canonical_outcome_affected_source_ids(state, reason)
            and bool(affected)
        )
    return affected == _affected_source_ids(state) and citations == tuple(
        sorted(item.evidence_id for item in state.evidence)
    )


def _authority_stop_reason(
    authority: ResearchGenerationAuthority,
) -> ResearchStopReason | None:
    if (
        type(authority) is not ResearchGenerationAuthority
        or not authority.is_canonical()
    ):
        return ResearchStopReason.PROTOCOL_FAILURE
    if authority.allowed:
        return ResearchStopReason.COMPLETE
    if authority.reason in {
        CoverageInputErrorCode.RESEARCH_STATE_IDENTITY_MISMATCH,
        CoverageInputErrorCode.SCHEMA_NAMESPACE_MISMATCH,
    }:
        return ResearchStopReason.PROTOCOL_FAILURE
    return None


def _affected_source_ids(state: ResearchState) -> tuple[str, ...]:
    bindings = {item.binding_id: item for item in state.bindings}
    affected = []
    for item in state.query_spec.semantic_items:
        if not item.required:
            continue
        if is_binding_free_semantic_item(item, state.bindings):
            continue
        selected = [bindings.get(binding_id) for binding_id in item.binding_ids]
        if (
            item.source_id in state.unresolved_items
            or not selected
            or any(
                binding is None or binding.status is not BindingStatus.SUPPORTED
                for binding in selected
            )
        ):
            affected.append(item.source_id)
    affected.extend(_disconnected_required_source_ids(state))
    return tuple(sorted(set(affected)))


def _canonical_outcome_affected_source_ids(
    state: ResearchState,
    reason: ResearchStopReason,
) -> tuple[str, ...]:
    if reason is ResearchStopReason.COMPLETE:
        return ()
    return _affected_source_ids(state)


def _disconnected_required_source_ids(state: ResearchState) -> tuple[str, ...]:
    bindings = {binding.binding_id: binding for binding in state.bindings}
    selected = []
    for item in state.query_spec.semantic_items:
        if not item.required:
            continue
        for binding_id in item.binding_ids:
            binding = bindings.get(binding_id)
            if binding is not None and binding.status is BindingStatus.SUPPORTED:
                selected.append(binding)
    try:
        return disconnected_binding_source_ids(
            tuple(selected),
            tuple(
                candidate
                for candidate in state.join_candidates
                if candidate.status is JoinCandidateStatus.VALIDATED
            ),
        )
    except FootprintError:
        return tuple(sorted(binding.source_id for binding in selected))
