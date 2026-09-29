"""Strict input validation for trusted research-decision resolution."""

from __future__ import annotations

from pydantic import ValidationError

from ..schema_loader import LoadedSchema
from ..schema_namespace import SchemaNamespace, canonical_schema_fingerprint
from .freshness import FreshnessContext, FreshnessStatus, evaluate_evidence_freshness
from .models import ResearchState
from .research_decision import (
    BindingAssessment,
    ExistingBindingRef,
    ExistingHypothesisRef,
    ExistingJoinRef,
    HypothesisAssessment,
    JoinAssessment,
    NewBindingProposal,
    NewHypothesisProposal,
    ResearchDecisionV1,
    SemanticCommitRequest,
    StopRequest,
    ToolIntent,
    MAX_RESEARCH_DECISION_BYTES,
)
from .serialization import canonical_json_bytes
from .tool_registry import AdaptiveResearchToolRegistry


class ResolutionInputError(ValueError):
    """Resolution inputs do not form one trusted current context."""


class ModelDecisionReferenceError(ResolutionInputError):
    """A model-authored assessment refers to missing trusted state."""


def validate_resolution_inputs(
    state: ResearchState,
    decision: ResearchDecisionV1,
    *,
    loaded_schema: LoadedSchema,
    freshness_context: FreshnessContext,
    registry: AdaptiveResearchToolRegistry,
) -> tuple[ResearchState, ResearchDecisionV1]:
    current = _revalidate_state(state)
    if not isinstance(decision, ResearchDecisionV1):
        raise ResolutionInputError("decision must be parsed ResearchDecisionV1")
    parsed = _revalidate_decision(expand_model_identifier_handles(current, decision))
    _validate_freshness_context(current, freshness_context)
    _validate_registry_schema(current, loaded_schema, registry)
    _validate_state_references(current, parsed, freshness_context)
    return current, parsed


def expand_model_identifier_handles(
    state: ResearchState,
    decision: ResearchDecisionV1,
) -> ResearchDecisionV1:
    """Restore short model-facing identifier handles to trusted raw IDs."""

    source_ids = tuple(sorted(item.source_id for item in state.query_spec.semantic_items))
    evidence_ids = tuple(sorted(record.evidence_id for record in state.evidence))
    if len(source_ids) != len(set(source_ids)) or len(evidence_ids) != len(set(evidence_ids)):
        raise ModelDecisionReferenceError("identifier handles are ambiguous")
    source_handles = {f"s{index}": value for index, value in enumerate(source_ids, 1)}
    evidence_handles = {
        f"e{index}": value for index, value in enumerate(evidence_ids, 1)
    }

    def resolve(
        handles: tuple[str, ...] | None,
        mapping: dict[str, str],
        label: str,
    ) -> tuple[str, ...] | None:
        if handles is None:
            return None
        if len(handles) != len(set(handles)):
            raise ModelDecisionReferenceError(f"duplicate {label} handle")
        try:
            return tuple(mapping[handle] for handle in handles)
        except KeyError:
            raise ModelDecisionReferenceError(f"{label} handle does not exist") from None

    proposals = []
    changed = False
    for proposal in decision.proposals:
        updates: dict[str, object] = {}
        citation_handles = getattr(proposal, "citation_evidence_handles", None)
        citation_ids = getattr(proposal, "citation_evidence_ids", None)
        if citation_handles is not None:
            if citation_ids is not None:
                raise ModelDecisionReferenceError("mixed citation handle and raw ID")
            updates["citation_evidence_ids"] = resolve(
                citation_handles, evidence_handles, "evidence"
            )
            updates["citation_evidence_handles"] = None
        if isinstance(proposal, NewBindingProposal) and proposal.source_handle is not None:
            if proposal.source_id is not None:
                raise ModelDecisionReferenceError("mixed source handle and raw ID")
            updates["source_id"] = resolve(
                (proposal.source_handle,), source_handles, "source"
            )[0]
            updates["source_handle"] = None
        if isinstance(proposal, NewHypothesisProposal) and proposal.source_handles is not None:
            if proposal.source_ids is not None:
                raise ModelDecisionReferenceError("mixed source handle and raw ID")
            updates["source_ids"] = resolve(
                proposal.source_handles, source_handles, "source"
            )
            updates["source_handles"] = None
        if updates:
            proposal = proposal.model_copy(update=updates)
            changed = True
        proposals.append(proposal)

    next_request = decision.next
    if isinstance(next_request, StopRequest):
        updates = {}
        if next_request.source_handles is not None:
            if next_request.source_ids is not None:
                raise ModelDecisionReferenceError("mixed source handle and raw ID")
            updates["source_ids"] = resolve(
                next_request.source_handles, source_handles, "source"
            )
            updates["source_handles"] = None
        if next_request.citation_evidence_handles is not None:
            if next_request.citation_evidence_ids is not None:
                raise ModelDecisionReferenceError("mixed citation handle and raw ID")
            updates["citation_evidence_ids"] = resolve(
                next_request.citation_evidence_handles, evidence_handles, "evidence"
            )
            updates["citation_evidence_handles"] = None
            if next_request.ambiguity is not None:
                updates["ambiguity"] = next_request.ambiguity.model_copy(
                    update={
                        "citation_evidence_ids": updates["citation_evidence_ids"],
                        "citation_evidence_handles": None,
                    }
                )
        elif next_request.ambiguity is not None:
            updates["ambiguity"] = next_request.ambiguity.model_copy(
                update={"citation_evidence_handles": None}
            )
        if updates:
            next_request = next_request.model_copy(update=updates)
            changed = True
    if not changed:
        return decision
    return decision.model_copy(update={"proposals": tuple(proposals), "next": next_request})


def _revalidate_state(state: ResearchState) -> ResearchState:
    if not isinstance(state, ResearchState):
        raise ResolutionInputError("state must satisfy ResearchState")
    try:
        current = ResearchState.model_validate(
            state.model_dump(mode="python", round_trip=True, warnings="error")
        )
    except (TypeError, ValueError, ValidationError):
        raise ResolutionInputError("state must satisfy ResearchState") from None
    if len(current.action_history) != current.revision:
        raise ResolutionInputError("state revision must equal action history length")
    if current.stop_reason is not None:
        raise ResolutionInputError("stopped research state cannot accept a decision")
    return current


def _revalidate_decision(decision: ResearchDecisionV1) -> ResearchDecisionV1:
    if not isinstance(decision, ResearchDecisionV1):
        raise ResolutionInputError("decision must be parsed ResearchDecisionV1")
    try:
        parsed = ResearchDecisionV1.model_validate(
            decision.model_dump(mode="python", round_trip=True, warnings="error")
        )
    except (TypeError, ValueError, ValidationError):
        raise ResolutionInputError("decision must satisfy ResearchDecisionV1") from None
    encoded = canonical_json_bytes(
        parsed.model_dump(mode="json", by_alias=True, warnings="error")
    )
    if len(encoded) > MAX_RESEARCH_DECISION_BYTES:
        raise ResolutionInputError("research decision exceeds its byte bound")
    return parsed


def _validate_freshness_context(
    state: ResearchState,
    context: FreshnessContext,
) -> None:
    if not isinstance(context, FreshnessContext):
        raise ResolutionInputError("freshness_context must satisfy its contract")
    if (
        context.run_id != state.run_id
        or context.run_incarnation != state.run_incarnation
        or context.schema_namespace_version != state.schema_namespace_version
    ):
        raise ResolutionInputError("freshness context does not match current state")


def _validate_registry_schema(
    state: ResearchState,
    loaded_schema: LoadedSchema,
    registry: AdaptiveResearchToolRegistry,
) -> None:
    if not isinstance(loaded_schema, LoadedSchema):
        raise ResolutionInputError("loaded_schema must be a trusted LoadedSchema")
    if not isinstance(registry, AdaptiveResearchToolRegistry):
        raise ResolutionInputError("registry must be AdaptiveResearchToolRegistry")
    if not isinstance(loaded_schema.namespace, SchemaNamespace):
        raise ResolutionInputError("loaded schema lacks a trusted namespace")
    try:
        fingerprint = canonical_schema_fingerprint(loaded_schema.schema)
    except (TypeError, ValueError) as exc:
        raise ResolutionInputError("loaded scoped schema is invalid") from exc
    if fingerprint != loaded_schema.namespace.schema_fingerprint:
        raise ResolutionInputError("loaded scoped schema fingerprint is stale")
    expected_version = f"sha256:{loaded_schema.namespace.version_key}"
    if expected_version != state.schema_namespace_version:
        raise ResolutionInputError("loaded schema version does not match state")

    context = registry.context
    runtimes = (context.schema_runtime, context.data_runtime)
    namespaces: list[str] = []
    for runtime in runtimes:
        if (
            getattr(runtime, "namespace", None) != loaded_schema.namespace
            or getattr(runtime, "scope", None) != loaded_schema.namespace.scope
        ):
            raise ResolutionInputError(
                "adaptive registry runtime scope or namespace does not match schema"
            )
        namespace = getattr(runtime, "table_namespace", None)
        if type(namespace) is not str or not namespace:
            raise ResolutionInputError("adaptive registry runtime lacks namespace")
        namespaces.append(namespace)
    if len(set(namespaces)) != 1:
        raise ResolutionInputError(
            "adaptive registry runtimes use different namespaces"
        )
    documents = getattr(context.schema_runtime, "documents", None)
    if type(documents) is not tuple:
        raise ResolutionInputError("schema runtime lacks trusted documents")


def _validate_state_references(
    state: ResearchState,
    decision: ResearchDecisionV1,
    freshness_context: FreshnessContext,
) -> None:
    source_ids = {item.source_id for item in state.query_spec.semantic_items}
    hypotheses = {item.hypothesis_id for item in state.hypotheses}
    bindings = {item.binding_id for item in state.bindings}
    joins = {item.join_id for item in state.join_candidates}
    evidence = {item.evidence_id: item for item in state.evidence}

    cited: set[str] = set()
    for proposal in decision.proposals:
        proposal_citations = getattr(proposal, "citation_evidence_ids", ())
        for evidence_id in proposal_citations:
            if evidence_id not in evidence:
                raise ModelDecisionReferenceError(
                    "proposal citation does not exist in current state"
                )
        cited.update(proposal_citations)
        if isinstance(proposal, NewHypothesisProposal):
            if not set(proposal.source_ids).issubset(source_ids):
                raise ModelDecisionReferenceError(
                    "hypothesis source reference does not exist"
                )
        elif isinstance(proposal, NewBindingProposal):
            if proposal.source_id not in source_ids:
                raise ModelDecisionReferenceError(
                    "binding source reference does not exist"
                )
            for reference in proposal.join_references:
                if (
                    isinstance(reference, ExistingJoinRef)
                    and reference.join_id not in joins
                ):
                    raise ModelDecisionReferenceError(
                        "binding references an unknown join"
                    )
        elif isinstance(proposal, HypothesisAssessment):
            if (
                isinstance(proposal.subject, ExistingHypothesisRef)
                and proposal.subject.hypothesis_id not in hypotheses
            ):
                raise ModelDecisionReferenceError(
                    "assessment references an unknown hypothesis"
                )
        elif isinstance(proposal, BindingAssessment):
            if (
                isinstance(proposal.subject, ExistingBindingRef)
                and proposal.subject.binding_id not in bindings
            ):
                raise ModelDecisionReferenceError(
                    "assessment references an unknown binding"
                )
        elif isinstance(proposal, JoinAssessment):
            if (
                isinstance(proposal.subject, ExistingJoinRef)
                and proposal.subject.join_id not in joins
            ):
                raise ModelDecisionReferenceError(
                    "assessment references an unknown join"
                )

    if isinstance(decision.next, ToolIntent):
        reference = decision.next.hypothesis_ref
        if (
            isinstance(reference, ExistingHypothesisRef)
            and reference.hypothesis_id not in hypotheses
        ):
            raise ModelDecisionReferenceError(
                "tool references an unknown hypothesis"
            )
    elif not isinstance(decision.next, SemanticCommitRequest):
        _require_subset(decision.next.source_ids, source_ids, "stop source")
        cited.update(decision.next.citation_evidence_ids)
        if decision.next.reason == "complete":
            items = {item.source_id: item for item in state.query_spec.semantic_items}
            hidden = [
                source_id
                for source_id in state.unresolved_items
                if items[source_id].required
            ]
            if hidden:
                raise ResolutionInputError(
                    "complete stop cannot hide unresolved required items"
                )

    for evidence_id in cited:
        record = evidence.get(evidence_id)
        if record is None:
            raise ResolutionInputError("citation does not exist in current state")
        if (
            record.run_id != state.run_id
            or record.run_incarnation != state.run_incarnation
            or record.schema_namespace_version != state.schema_namespace_version
            or record.revision > state.revision
        ):
            raise ResolutionInputError("citation has foreign or future identity")
        freshness = evaluate_evidence_freshness(record, freshness_context)
        if freshness.status is not FreshnessStatus.FRESH:
            raise ResolutionInputError("citation is not fresh")


def _require_subset(values: tuple[str, ...], allowed: set[str], label: str) -> None:
    if not set(values).issubset(allowed):
        raise ResolutionInputError(f"{label} reference does not exist")
