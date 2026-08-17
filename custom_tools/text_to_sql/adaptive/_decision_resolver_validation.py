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
    parsed = _revalidate_decision(decision)
    _validate_freshness_context(current, freshness_context)
    _validate_registry_schema(current, loaded_schema, registry)
    _validate_state_references(current, parsed, freshness_context)
    return current, parsed


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
