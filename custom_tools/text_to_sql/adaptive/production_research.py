"""Production assembly for one Typed schema-research run."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError

from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
from workflow.adaptive_state_store import (
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.deadline import DeadlineBudget

from ..schema_loader import LoadedSchema
from ..schema_namespace import SchemaScope
from ._semantic_value_certificate import (
    ExactValueCertificateError,
    evidence_observes_exact_column,
)
from .data_probes import DataProbeRuntime
from .freshness import (
    DocumentSourceAvailability,
    DocumentSourceState,
    FreshnessContext,
)
from .models import (
    BindingStatus,
    BudgetState,
    DerivedExpressionBinding,
    DiscriminatorValueBinding,
    DocumentRuleBinding,
    EvidenceCost,
    EvidenceRecord,
    EvidenceSourceKind,
    HypothesisStatus,
    JoinCandidateStatus,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    SemanticItemStatus,
    PhysicalColumnBinding,
    VerticalAttributeBinding,
    is_binding_free_semantic_item,
    is_structurally_resolved_limit,
)
from .policy import AdaptivePolicyConfig, BudgetAdmissionError, initial_budget_state
from .provenance import parse_probe_observation
from .research_loop import (
    ResearchLoopOutcome,
    _state_with_reconciled_model_budget,
)
from .schema_probes import (
    SchemaEvidenceDocument,
    SchemaProbeBudgetRuntime,
    SchemaProbeRuntime,
)
from .schema_research_agent import (
    SchemaResearchDecisionAdapter,
    SchemaResearchAgentProfile,
    SchemaResearchDecisionModel,
    SchemaResearchValidationFeedback,
    build_schema_research_prompt,
)
from .semantic_coverage import CoverageInputErrorCode
from .serialization import (
    InlineRowsLimitError,
    SerializationLimits,
    StateSizeLimitError,
    canonical_json_bytes,
)
from .state import _result_expectation_key
from .tool_registry import AdaptiveResearchToolContext, AdaptiveResearchToolRegistry


_MAX_SCHEMA_RESEARCH_PROMPT_BYTES = 32_768


class ProductionResearchAssemblyError(RuntimeError):
    """Trusted production inputs cannot assemble the research loop."""


class _CapturedSchemaOnlyLoader:
    """Fail if production accidentally attempts a second schema load."""

    def load_scoped_schema(self, *_args: object) -> LoadedSchema:
        raise ProductionResearchAssemblyError(
            "production adaptive research cannot reload the captured schema"
        )


@dataclass(frozen=True, slots=True)
class ProductionResearchAssembly:
    """Exact dependencies for the existing ``run_research_loop`` entrypoint."""

    initial_state: ResearchState
    task: str
    research_context: Callable[..., str]
    model: SchemaResearchDecisionModel
    model_identity: str
    adapter: SchemaResearchDecisionAdapter
    loaded_schema: LoadedSchema
    freshness_context: FreshnessContext
    registry: AdaptiveResearchToolRegistry
    state_store: AdaptiveResearchStateStore
    checkpoint_store: AdaptiveStateStore
    budget_ledger: AdaptiveBudgetLedger
    policy: AdaptivePolicyConfig
    deadline: DeadlineBudget
    is_cancelled: Callable[[], bool]
    semantic_repair_continuation: bool

    def loop_arguments(self) -> dict[str, object]:
        return {
            "initial_state": self.initial_state,
            "task": self.task,
            "research_context": self.research_context,
            "model": self.model,
            "model_identity": self.model_identity,
            "adapter": self.adapter,
            "loaded_schema": self.loaded_schema,
            "freshness_context": self.freshness_context,
            "registry": self.registry,
            "state_store": self.state_store,
            "checkpoint_store": self.checkpoint_store,
            "budget_ledger": self.budget_ledger,
            "policy": self.policy,
            "deadline": self.deadline,
            "is_cancelled": self.is_cancelled,
            "semantic_repair_continuation": self.semantic_repair_continuation,
        }


def assemble_production_research(
    *,
    initial_state: ResearchState,
    query: str,
    loaded_schema: LoadedSchema,
    semantic_table_hints: tuple[str, ...] = (),
    verified_probe_fact_hints: tuple[dict[str, object], ...] = (),
    documents: tuple[SchemaEvidenceDocument, ...] = (),
    dsn: str,
    scope: SchemaScope,
    table_namespace: str,
    model: SchemaResearchDecisionModel,
    model_identity: str,
    profile: SchemaResearchAgentProfile,
    state_store: AdaptiveResearchStateStore,
    checkpoint_store: AdaptiveStateStore,
    budget_ledger: AdaptiveBudgetLedger,
    policy: AdaptivePolicyConfig,
    deadline: DeadlineBudget,
    is_cancelled: Callable[[], bool],
    semantic_repair_continuation: bool = False,
) -> ProductionResearchAssembly:
    """Wire trusted captured inputs to the existing adaptive research loop."""

    if not isinstance(initial_state, ResearchState):
        raise TypeError("initial_state must be ResearchState")
    if not isinstance(loaded_schema, LoadedSchema):
        raise TypeError("loaded_schema must be LoadedSchema")
    if type(semantic_table_hints) is not tuple or any(
        type(table) is not str or table not in loaded_schema.schema
        for table in semantic_table_hints
    ):
        raise TypeError("semantic_table_hints must be captured schema table names")
    if type(verified_probe_fact_hints) is not tuple or any(
        type(fact) is not dict for fact in verified_probe_fact_hints
    ):
        raise TypeError("verified_probe_fact_hints must be fact dictionaries")
    if type(documents) is not tuple or any(
        type(document) is not SchemaEvidenceDocument for document in documents
    ):
        raise TypeError("documents must be exact SchemaEvidenceDocument values")
    expected_document_namespace = f"sha256:{loaded_schema.namespace.version_key}"
    if (
        len({document.document_id for document in documents}) != len(documents)
        or any(
            document.schema_namespace_version != expected_document_namespace
            for document in documents
        )
    ):
        raise ProductionResearchAssemblyError("documents differ from captured schema")
    if loaded_schema.namespace.scope != scope:
        raise ProductionResearchAssemblyError("captured schema scope changed")
    if not isinstance(query, str) or not query.strip():
        raise ProductionResearchAssemblyError("research task must be non-empty text")
    if not callable(model) or not callable(is_cancelled):
        raise TypeError("model and cancellation boundary must be callable")
    if not isinstance(profile, SchemaResearchAgentProfile):
        raise TypeError("profile must be SchemaResearchAgentProfile")
    model_identity = _require_stable_model_identity(model_identity, profile.model)

    loader = _CapturedSchemaOnlyLoader()
    schema_runtime = SchemaProbeRuntime(
        loader=loader,
        dsn=dsn,
        scope=scope,
        namespace=loaded_schema.namespace,
        table_namespace=table_namespace,
        deadline=deadline,
        loaded_schema=loaded_schema,
        documents=documents,
    )
    data_runtime = DataProbeRuntime(
        loader=loader,
        dsn=dsn,
        scope=scope,
        namespace=loaded_schema.namespace,
        table_namespace=table_namespace,
        deadline=deadline,
        loaded_schema=loaded_schema,
    )

    def budget_factory(
        kind: ResearchActionKind,
        target: object,
        parameters: tuple[tuple[str, str | int | float | bool | None], ...],
    ) -> SchemaProbeBudgetRuntime:
        current = state_store.load_latest_research_state(
            initial_state.run_id,
            initial_state.run_incarnation,
        )
        if current is None:
            raise ProductionResearchAssemblyError("research state is not durable")
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                current.run_id,
                current.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                current.revision,
            )
        )
        if snapshot.planned is None or not isinstance(snapshot.planned.action, dict):
            raise ProductionResearchAssemblyError(
                "planned research action is not durable"
            )
        envelope = snapshot.planned.action
        action_payload = envelope.get("action")
        invocation_id = envelope.get("invocation_id")
        if not isinstance(action_payload, dict) or not isinstance(invocation_id, str):
            raise ProductionResearchAssemblyError("planned research action is malformed")
        action = ResearchAction.model_validate_json(
            canonical_json_bytes(action_payload),
            strict=True,
        )
        if (
            action.kind is not kind
            or action.target != target
            or action.parameters != parameters
        ):
            raise ProductionResearchAssemblyError(
                "registry call differs from the durable planned action"
            )
        admitted = _state_with_reconciled_model_budget(
            current,
            budget_ledger,
            policy,
        )
        remaining = admitted.budget_state
        return SchemaProbeBudgetRuntime(
            state=admitted,
            action=action,
            maximum_cost=EvidenceCost(
                wall_clock_ms=remaining.remaining_wall_clock_ms,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=remaining.remaining_db_probe_ms,
                rows=(
                    min(remaining.remaining_rows, dict(action.parameters)["limit"])
                    if action.kind is ResearchActionKind.SAMPLE_ROWS
                    else remaining.remaining_rows
                ),
                bytes=remaining.remaining_bytes,
            ),
            config=policy,
            ledger=budget_ledger,
            invocation_id=invocation_id,
        )

    registry = AdaptiveResearchToolRegistry(
        AdaptiveResearchToolContext(
            schema_runtime=schema_runtime,
            data_runtime=data_runtime,
            budget_factory=budget_factory,
        )
    )

    def research_context(
        state: ResearchState,
        validation_feedback: tuple[SchemaResearchValidationFeedback, ...],
        rejected_duplicate_actions: tuple[dict[str, object], ...] = (),
        rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
        invalid_stop_generation_authority: (
            tuple[CoverageInputErrorCode, tuple[str, ...]] | None
        ) = None,
    ) -> str:
        return _bounded_research_context(
            loaded_schema,
            state,
            policy,
            profile=profile,
            task=query,
            validation_feedback=validation_feedback,
            rejected_duplicate_actions=rejected_duplicate_actions,
            rejected_preflight_assessments=rejected_preflight_assessments,
            invalid_stop_generation_authority=invalid_stop_generation_authority,
            documents=documents,
            semantic_table_hints=semantic_table_hints[:5],
            verified_probe_fact_hints=verified_probe_fact_hints[:5],
        )

    return ProductionResearchAssembly(
        initial_state=initial_state,
        task=query,
        research_context=research_context,
        model=model,
        model_identity=model_identity,
        adapter=SchemaResearchDecisionAdapter(profile),
        loaded_schema=loaded_schema,
        freshness_context=FreshnessContext(
            evaluated_at=datetime.now(UTC),
            run_id=initial_state.run_id,
            run_incarnation=initial_state.run_incarnation,
            schema_namespace_version=initial_state.schema_namespace_version,
            document_sources=tuple(
                DocumentSourceState(
                    document_id=document.document_id,
                    availability=DocumentSourceAvailability.AVAILABLE,
                    source_version=document.source_version,
                )
                for document in documents
            ),
        ),
        registry=registry,
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=budget_ledger,
        policy=policy,
        deadline=deadline,
        is_cancelled=is_cancelled,
        semantic_repair_continuation=semantic_repair_continuation,
    )


async def run_production_schema_research(
    *,
    query_spec: QuerySpec,
    loaded_schema: LoadedSchema,
    semantic_table_hints: tuple[str, ...] = (),
    verified_probe_fact_hints: tuple[dict[str, object], ...] = (),
    documents: tuple[SchemaEvidenceDocument, ...] = (),
    dsn: str,
    scope: SchemaScope,
    table_namespace: str,
    deadline: DeadlineBudget,
    policy: AdaptivePolicyConfig,
    state_store: AdaptiveResearchStateStore,
    checkpoint_store: AdaptiveStateStore,
    budget_ledger: AdaptiveBudgetLedger,
    model: SchemaResearchDecisionModel,
    model_identity: str,
    profile: SchemaResearchAgentProfile,
    is_cancelled: Callable[[], bool],
    loop_runner: Callable[..., Awaitable[ResearchLoopOutcome]],
) -> ResearchLoopOutcome:
    """Build trusted production dependencies and invoke the existing loop once."""

    if not isinstance(query_spec, QuerySpec):
        raise TypeError("query_spec must be QuerySpec")
    if not isinstance(loaded_schema, LoadedSchema):
        raise TypeError("loaded_schema must be LoadedSchema")
    if loaded_schema.namespace.scope != scope:
        raise ProductionResearchAssemblyError("captured schema scope changed")
    if not isinstance(profile, SchemaResearchAgentProfile):
        raise TypeError("profile must be SchemaResearchAgentProfile")
    namespace_version = f"sha256:{loaded_schema.namespace.version_key}"
    initial_state = _build_initial_research_state(
        query_spec,
        schema_namespace_version=namespace_version,
        budget_state=initial_budget_state(policy),
    )
    assembly = assemble_production_research(
        initial_state=initial_state,
        query=query_spec.original_text,
        loaded_schema=loaded_schema,
        semantic_table_hints=semantic_table_hints,
        verified_probe_fact_hints=verified_probe_fact_hints,
        documents=documents,
        dsn=dsn,
        scope=scope,
        table_namespace=table_namespace,
        model=model,
        model_identity=model_identity,
        profile=profile,
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=budget_ledger,
        policy=policy,
        deadline=deadline,
        is_cancelled=is_cancelled,
    )
    return await loop_runner(**assembly.loop_arguments())


def _build_initial_research_state(
    query_spec: QuerySpec,
    *,
    schema_namespace_version: str,
    budget_state: BudgetState,
) -> ResearchState:
    """Create the first native Typed research state."""

    if type(query_spec) is not QuerySpec:
        raise TypeError("query_spec must be QuerySpec")
    if type(budget_state) is not BudgetState:
        raise TypeError("budget_state must be BudgetState")
    if query_spec.revision != 0:
        raise ProductionResearchAssemblyError(
            "initial query_spec revision must be zero"
        )
    if query_spec.schema_namespace_version not in {
        None,
        schema_namespace_version,
    }:
        raise ProductionResearchAssemblyError(
            "query_spec schema namespace version does not match"
        )
    if any(item.binding_ids for item in query_spec.semantic_items):
        raise ProductionResearchAssemblyError(
            "initial query_spec items cannot have bindings"
        )

    semantic_items = tuple(
        item.model_copy(
            update={
                "status": (
                    SemanticItemStatus.RESOLVED
                    if is_structurally_resolved_limit(item)
                    else SemanticItemStatus.UNSUPPORTED
                    if item.status is SemanticItemStatus.UNSUPPORTED
                    else SemanticItemStatus.UNRESOLVED
                ),
                "binding_ids": (),
            }
        )
        for item in query_spec.semantic_items
    )
    namespaced_query = query_spec.model_copy(
        update={
            "schema_namespace_version": schema_namespace_version,
            "semantic_items": semantic_items,
        }
    )
    try:
        checked_budget = BudgetState.model_validate(
            budget_state.model_dump(mode="python", round_trip=True, warnings="error")
        )
        return ResearchState(
            run_id=namespaced_query.run_id,
            run_incarnation=namespaced_query.run_incarnation,
            revision=0,
            schema_namespace_version=schema_namespace_version,
            query_spec=namespaced_query,
            hypotheses=(),
            evidence=(),
            bindings=(),
            join_candidates=(),
            unresolved_items=tuple(
                sorted(
                    item.source_id
                    for item in namespaced_query.semantic_items
                    if item.required
                    and not is_binding_free_semantic_item(item)
                )
            ),
            action_history=(),
            result_expectations=(),
            budget_state=checked_budget,
            stop_reason=None,
        )
    except ValidationError as exc:
        raise ProductionResearchAssemblyError(
            "initial Typed research state is invalid"
        ) from exc


def stable_schema_research_model_identity(model: str) -> str:
    """Return the unique ledger identity of the dedicated research profile."""

    if not isinstance(model, str) or not model.strip():
        raise ValueError("schema-research model must be non-empty text")
    return f"schema_research_agent:{model}"


def _require_stable_model_identity(model_identity: str, profile_model: str) -> str:
    expected = stable_schema_research_model_identity(profile_model)
    if model_identity != expected:
        raise ProductionResearchAssemblyError(
            "research model identity is not profile-bound"
        )
    return expected


def _bounded_research_context(
    loaded_schema: LoadedSchema,
    state: ResearchState,
    policy: AdaptivePolicyConfig,
    *,
    profile: SchemaResearchAgentProfile,
    task: str,
    validation_feedback: tuple[SchemaResearchValidationFeedback, ...],
    rejected_duplicate_actions: tuple[dict[str, object], ...] = (),
    rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
    invalid_stop_generation_authority: (
        tuple[CoverageInputErrorCode, tuple[str, ...]] | None
    ) = None,
    documents: tuple[SchemaEvidenceDocument, ...] = (),
    semantic_table_hints: tuple[str, ...] = (),
    verified_probe_fact_hints: tuple[dict[str, object], ...] = (),
) -> str:
    """Return the bounded, deterministic model view of the durable state."""

    if policy.model_budget is None:
        raise ProductionResearchAssemblyError("research policy has no model budget")
    maximum_bytes = policy.result_volume.inline_bytes
    maximum_prompt_bytes = _MAX_SCHEMA_RESEARCH_PROMPT_BYTES

    def fits_prompt(encoded_context: bytes) -> bool:
        prompt = build_schema_research_prompt(
            profile,
            task=task,
            research_context=encoded_context.decode("utf-8"),
            validation_feedback=validation_feedback or None,
        )
        return len(prompt.encode("utf-8")) <= maximum_prompt_bytes

    if not fits_prompt(b""):
        raise BudgetAdmissionError(
            "schema-research fixed prompt exceeds the input safety envelope"
        )
    if type(state) is not ResearchState:
        raise TypeError("state must be ResearchState")
    state_payload = state.model_dump(mode="json", by_alias=True)
    document_catalog = [
        {"document_id": item.document_id, "title": item.title}
        for item in sorted(documents, key=lambda item: (item.document_id, item.title))
    ]
    assert type(state_payload) is dict
    query_payload = state_payload["query_spec"]
    assert type(query_payload) is dict
    state_view = {
        key: value
        for key, value in state_payload.items()
        if key not in _MODEL_VIEW_COLLECTIONS and key != "unresolved_items"
    }
    state_view["query_spec"] = {**query_payload, "semantic_items": []}
    state_view["unresolved_items"] = []
    for name in _MODEL_VIEW_COLLECTIONS:
        state_view[name] = []
    context = {
        "state": state_view,
        "documents": document_catalog,
        "requery_with_existing_probes": True,
        "completed_action_index": [
            {
                "kind": action.kind.value,
                "target": (
                    None
                    if action.target is None
                    else action.target.model_dump(mode="json", by_alias=True)
                ),
                "parameters": [list(item) for item in action.parameters],
                "action_digest": action.action_digest,
            }
            for action in sorted(state.action_history, key=lambda action: action.action_digest)
        ],
    }
    if rejected_duplicate_actions:
        context["rejected_duplicate_actions"] = list(rejected_duplicate_actions)
    if rejected_preflight_assessments:
        context["rejected_preflight_assessments"] = list(
            rejected_preflight_assessments
        )
    if invalid_stop_generation_authority is not None:
        reason_code, affected_source_ids = invalid_stop_generation_authority
        context["invalid_stop_generation_authority"] = {
            "reason_code": reason_code.value,
            "affected_source_ids": list(sorted(affected_source_ids)),
        }
    if semantic_table_hints:
        context["semantic_table_hints"] = list(semantic_table_hints)
    _fill_bounded_state_view(
        state,
        state_payload,
        state_view,
        context,
        loaded_schema.schema,
        policy,
        maximum_bytes,
        fits_prompt,
    )
    if verified_probe_fact_hints:
        included_hints: list[dict[str, object]] = []
        context["verified_probe_fact_hints"] = included_hints
        for hint in verified_probe_fact_hints:
            included_hints.append(hint)
            if _encode_context(
                loaded_schema.schema,
                context,
                policy,
                maximum_bytes,
                fits_prompt,
            ) is None:
                included_hints.pop()
                break
        if not included_hints:
            context.pop("verified_probe_fact_hints")
    _refresh_omitted_counts(state_payload, state_view, context)
    encoded = _encode_context(
        loaded_schema.schema,
        context,
        policy,
        maximum_bytes,
        fits_prompt,
    )
    if encoded is None:
        raise BudgetAdmissionError(
            "research state exceeds the model input safety envelope"
        )
    return encoded.decode("utf-8")


_MODEL_VIEW_COLLECTIONS = (
    "hypotheses",
    "evidence",
    "bindings",
    "join_candidates",
    "action_history",
    "result_expectations",
)


def _model_evidence_view(
    record: EvidenceRecord,
    full_payload: dict[str, object],
) -> dict[str, object]:
    """Return the trusted, decision-useful part of one inline observation."""

    observation = parse_probe_observation(record.observation)
    if observation is None or observation.storage != "inline":
        return full_payload
    return {
        "evidence_id": record.evidence_id,
        "source_kind": record.source_kind.value,
        "target": record.target.model_dump(mode="json", by_alias=True),
        "probe_kind": observation.probe_kind.value,
        "payload": observation.payload,
        "summary": observation.summary,
        "truncated": observation.truncated,
    }


def _model_binding_view(
    binding: object,
    full_payload: dict[str, object],
) -> dict[str, object]:
    """Return only the existing binding facts needed for a model decision."""

    if not isinstance(
        binding,
        (
            PhysicalColumnBinding,
            VerticalAttributeBinding,
            DiscriminatorValueBinding,
            DerivedExpressionBinding,
            DocumentRuleBinding,
        ),
    ):
        raise TypeError("active binding must be a typed binding")
    view = {
        name: full_payload[name]
        for name in ("binding_id", "source_id", "kind", "evidence_ids", "status")
    }
    if isinstance(binding, PhysicalColumnBinding):
        view["physical_column"] = full_payload["physical_column"]
    elif isinstance(binding, VerticalAttributeBinding):
        for name in (
            "entity_table",
            "entity_key",
            "attribute_catalog_table",
            "attribute_catalog_key",
            "attribute_name_predicate",
            "value_table",
            "value_entity_key",
            "value_attribute_key",
            "value_predicate",
        ):
            view[name] = full_payload[name]
    elif isinstance(binding, DiscriminatorValueBinding):
        view["discriminator_column"] = full_payload["discriminator_column"]
        view["discriminator_predicate"] = full_payload["discriminator_predicate"]
        view["predicates"] = full_payload["predicates"]
    elif isinstance(binding, DerivedExpressionBinding):
        for name in ("expression", "document", "rule_excerpt", "input_columns"):
            view[name] = full_payload[name]
    else:
        view["document"] = full_payload["document"]
        view["rule_id"] = full_payload["rule_id"]
        view["rule_text"] = full_payload["rule_text"]
    return view


def _model_semantic_item_view(full_payload: dict[str, object]) -> dict[str, object]:
    """Return the semantic item facts needed to choose the next research step."""

    return {
        name: full_payload[name]
        for name in (
            "source_id",
            "kind",
            "source_text",
            "normalized_meaning",
            "operator",
            "literal_or_reference",
            "status",
            "binding_ids",
        )
        if full_payload[name] is not None
    }


def _fill_bounded_state_view(
    state: ResearchState,
    state_payload: dict[str, object],
    view: dict[str, object],
    context: dict[str, object],
    schema: object,
    policy: AdaptivePolicyConfig,
    maximum_bytes: int,
    fits_prompt: Callable[[bytes], bool],
) -> None:
    """Pack complete facts in the deterministic order required for a decision."""

    query_payload = state_payload["query_spec"]
    assert type(query_payload) is dict
    semantic_payloads = query_payload["semantic_items"]
    assert type(semantic_payloads) is list
    semantic_by_id = {
        item.source_id: _model_semantic_item_view(payload)
        for item, payload in zip(state.query_spec.semantic_items, semantic_payloads, strict=True)
    }
    evidence_payloads = state_payload["evidence"]
    assert type(evidence_payloads) is list
    evidence_by_id = {
        item.evidence_id: _model_evidence_view(item, payload)
        for item, payload in zip(state.evidence, evidence_payloads, strict=True)
    }
    evidence_records = {item.evidence_id: item for item in state.evidence}
    def encode_current() -> bytes | None:
        _refresh_omitted_counts(state_payload, view, context)
        return _encode_context(
            schema,
            context,
            policy,
            maximum_bytes,
            fits_prompt,
        )

    included_evidence_ids: set[str] = set()

    def include(
        group: str,
        item: object,
        evidence_ids: tuple[str, ...] = (),
        unresolved_id: str | None = None,
    ) -> bool:
        if any(evidence_id not in evidence_by_id for evidence_id in evidence_ids):
            return False
        if group == "semantic_items":
            query_view = view["query_spec"]
            assert type(query_view) is dict
            values = query_view["semantic_items"]
        else:
            values = view[group]
        assert type(values) is list
        values.append(item)
        if unresolved_id is not None:
            unresolved_values = view["unresolved_items"]
            assert type(unresolved_values) is list
            unresolved_values.append(unresolved_id)
        added: list[str] = []
        evidence_values = view["evidence"]
        assert type(evidence_values) is list
        for evidence_id in sorted(
            evidence_ids,
            key=lambda value: _evidence_sort_key(evidence_records[value]),
        ):
            if evidence_id not in included_evidence_ids:
                evidence_values.append(evidence_by_id[evidence_id])
                added.append(evidence_id)
        if encode_current() is not None:
            included_evidence_ids.update(added)
            return True
        for _ in added:
            evidence_values.pop()
        values.pop()
        if unresolved_id is not None:
            unresolved_values = view["unresolved_items"]
            assert type(unresolved_values) is list
            unresolved_values.pop()
        _refresh_omitted_counts(state_payload, view, context)
        return False

    active_hypotheses = sorted(
        (
            item
            for item in state.hypotheses
            if item.status in {HypothesisStatus.PROPOSED, HypothesisStatus.TESTING}
        ),
        key=lambda item: item.hypothesis_id,
    )
    active_bindings = sorted(
        (
            item
            for item in state.bindings
            if item.status in {BindingStatus.CANDIDATE, BindingStatus.SUPPORTED}
        ),
        key=lambda item: item.binding_id,
    )
    active_joins = sorted(
        (
            item
            for item in state.join_candidates
            if item.status in {JoinCandidateStatus.CANDIDATE, JoinCandidateStatus.VALIDATED}
        ),
        key=lambda item: item.join_id,
    )
    hypothesis_payloads = state_payload["hypotheses"]
    binding_payloads = state_payload["bindings"]
    join_payloads = state_payload["join_candidates"]
    assert type(hypothesis_payloads) is list
    assert type(binding_payloads) is list
    assert type(join_payloads) is list
    hypothesis_by_id = {
        item.hypothesis_id: payload
        for item, payload in zip(state.hypotheses, hypothesis_payloads, strict=True)
    }
    binding_by_id = {
        item.binding_id: _model_binding_view(item, payload)
        for item, payload in zip(state.bindings, binding_payloads, strict=True)
    }
    join_by_id = {
        item.join_id: payload
        for item, payload in zip(state.join_candidates, join_payloads, strict=True)
    }
    active_bindings_by_source = {
        source_id: tuple(
            binding for binding in active_bindings if binding.source_id == source_id
        )
        for source_id in semantic_by_id
    }
    included_binding_ids: set[str] = set()

    def include_source(
        source_id: str,
        *,
        unresolved_id: str | None = None,
    ) -> bool:
        source = next(
            item
            for item in state.query_spec.semantic_items
            if item.source_id == source_id
        )
        bindings_for_source = active_bindings_by_source[source_id]
        if not source.binding_ids:
            return include(
                "semantic_items",
                semantic_by_id[source_id],
                unresolved_id=unresolved_id,
            )
        if any(
            evidence_id not in evidence_by_id
            for binding in bindings_for_source
            for evidence_id in binding.evidence_ids
        ):
            return False

        query_view = view["query_spec"]
        assert type(query_view) is dict
        semantic_items = query_view["semantic_items"]
        assert type(semantic_items) is list
        semantic_items.append(semantic_by_id[source_id])
        if unresolved_id is not None:
            unresolved_values = view["unresolved_items"]
            assert type(unresolved_values) is list
            unresolved_values.append(unresolved_id)
        binding_values = view["bindings"]
        assert type(binding_values) is list
        for binding in bindings_for_source:
            binding_values.append(binding_by_id[binding.binding_id])
        evidence_values = view["evidence"]
        assert type(evidence_values) is list
        added_evidence_ids = [
            evidence_id
            for binding in bindings_for_source
            for evidence_id in binding.evidence_ids
            if evidence_id not in included_evidence_ids
        ]
        added_evidence_ids = sorted(
            set(added_evidence_ids),
            key=lambda value: _evidence_sort_key(evidence_records[value]),
        )
        for evidence_id in added_evidence_ids:
            evidence_values.append(evidence_by_id[evidence_id])
        if encode_current() is not None:
            included_binding_ids.update(
                binding.binding_id for binding in bindings_for_source
            )
            included_evidence_ids.update(added_evidence_ids)
            return True
        for _ in added_evidence_ids:
            evidence_values.pop()
        for _ in bindings_for_source:
            binding_values.pop()
        semantic_items.pop()
        if unresolved_id is not None:
            unresolved_values = view["unresolved_items"]
            assert type(unresolved_values) is list
            unresolved_values.pop()
        _refresh_omitted_counts(state_payload, view, context)
        return False

    unresolved = {
        item.source_id
        for item in state.query_spec.semantic_items
        if item.required and item.source_id in state.unresolved_items
    }
    included_source_ids: set[str] = set()
    for source_id in sorted(unresolved):
        if include_source(source_id, unresolved_id=source_id):
            included_source_ids.add(source_id)

    remaining_endpoint_tables = {
        column.table
        for item in active_joins
        if item.status is JoinCandidateStatus.VALIDATED
        for column in (item.left, item.right)
    }
    for evidence in sorted(state.evidence, key=_evidence_sort_key):
        if (
            evidence.source_kind is EvidenceSourceKind.SCHEMA
            and evidence.target in remaining_endpoint_tables
        ):
            remaining_endpoint_tables.remove(evidence.target)
            if include("evidence", evidence_by_id[evidence.evidence_id]):
                included_evidence_ids.add(evidence.evidence_id)

    for item in active_joins:
        if item.status is JoinCandidateStatus.VALIDATED:
            if not include(
                "join_candidates", join_by_id[item.join_id], item.evidence_ids
            ):
                include("join_candidates", join_by_id[item.join_id])

    active_source_ids = {
        source_id
        for item in active_hypotheses
        for source_id in item.source_ids
    } | {item.source_id for item in active_bindings}
    for source_id in sorted(active_source_ids - unresolved):
        if include_source(source_id):
            included_source_ids.add(source_id)

    included_hypothesis_ids: set[str] = set()
    for item in active_hypotheses:
        if set(item.source_ids).issubset(included_source_ids):
            if include("hypotheses", hypothesis_by_id[item.hypothesis_id], item.evidence_ids):
                included_hypothesis_ids.add(item.hypothesis_id)
    for item in active_bindings:
        if (
            item.source_id in included_source_ids
            and item.binding_id not in included_binding_ids
        ):
            include("bindings", binding_by_id[item.binding_id], item.evidence_ids)
    for item in active_joins:
        if item.status is JoinCandidateStatus.CANDIDATE:
            if not include(
                "join_candidates", join_by_id[item.join_id], item.evidence_ids
            ):
                include("join_candidates", join_by_id[item.join_id])

    expectation_payloads = state_payload["result_expectations"]
    assert type(expectation_payloads) is list
    expectation_payload_by_key = {
        _result_expectation_key(item): payload
        for item, payload in zip(state.result_expectations, expectation_payloads, strict=True)
    }
    for item in sorted(
        state.result_expectations,
        key=_result_expectation_key,
    ):
        if item.source_id in included_source_ids:
            include(
                "result_expectations",
                expectation_payload_by_key[_result_expectation_key(item)],
                (item.evidence_id,),
            )

    for binding in active_bindings:
        if (
            not isinstance(binding, PhysicalColumnBinding)
            or binding.status is not BindingStatus.CANDIDATE
        ):
            continue
        for evidence in sorted(state.evidence, key=_evidence_sort_key):
            try:
                observes_column = evidence_observes_exact_column(
                    evidence,
                    binding.physical_column,
                )
            except ExactValueCertificateError:
                continue
            if (
                observes_column
                and evidence.evidence_id not in included_evidence_ids
                and evidence.evidence_id in evidence_by_id
                and include("evidence", evidence_by_id[evidence.evidence_id])
            ):
                included_evidence_ids.add(evidence.evidence_id)

    for evidence in sorted(state.evidence, key=_evidence_sort_key):
        if (
            evidence.evidence_id not in included_evidence_ids
            and evidence.evidence_id in evidence_by_id
            and include("evidence", evidence_by_id[evidence.evidence_id])
        ):
            included_evidence_ids.add(evidence.evidence_id)

    action_payloads = state_payload["action_history"]
    assert type(action_payloads) is list
    for action, payload in sorted(
        zip(state.action_history, action_payloads, strict=True),
        key=lambda pair: (-pair[0].expected_revision, pair[0].action_id),
    ):
        if (
            action.hypothesis_id is None
            or action.hypothesis_id in included_hypothesis_ids
        ):
            include("action_history", payload)


def _refresh_omitted_counts(
    state_payload: dict[str, object],
    view: dict[str, object],
    context: dict[str, object],
) -> None:
    query_payload = state_payload["query_spec"]
    query_view = view["query_spec"]
    assert type(query_payload) is dict and type(query_view) is dict
    semantic_items = query_payload["semantic_items"]
    included_items = query_view["semantic_items"]
    assert type(semantic_items) is list and type(included_items) is list
    omitted = {
        "semantic_items": len(semantic_items) - len(included_items),
        **{name: len(state_payload[name]) - len(view[name]) for name in _MODEL_VIEW_COLLECTIONS},
    }
    if any(omitted.values()):
        context["omitted"] = {
            name: count for name, count in omitted.items() if count
        }
    else:
        context.pop("omitted", None)


def _evidence_sort_key(item: object) -> tuple[object, ...]:
    """Newest evidence first, with its durable ID as a stable tie-breaker."""

    observed_at = getattr(item, "observed_at")
    return (-observed_at.timestamp(), getattr(item, "evidence_id"))


def _encode_context(
    schema: object,
    context: dict[str, object],
    policy: AdaptivePolicyConfig,
    maximum_bytes: int,
    fits_prompt: Callable[[bytes], bool],
) -> bytes | None:
    encoded = _encode_if_within_budget(
        {"schema": schema, **context}, policy, maximum_bytes
    )
    if encoded is not None and fits_prompt(encoded):
        return encoded
    if not isinstance(schema, Mapping):
        return None
    snapshot = _truncated_schema_snapshot(
        schema,
        context,
        policy,
        maximum_bytes,
        fits_prompt,
    )
    encoded = _encode_if_within_budget(
        {"schema": snapshot, **context}, policy, maximum_bytes
    )
    return encoded if encoded is not None and fits_prompt(encoded) else None


def _truncated_schema_snapshot(
    schema: Mapping[object, object],
    context: dict[str, object],
    policy: AdaptivePolicyConfig,
    maximum_bytes: int,
    fits_prompt: Callable[[bytes], bool],
) -> dict[str, object]:
    """Keep a lexical schema prefix; omitted tables remain reachable by probes."""

    table_items = sorted(
        ((name, body) for name, body in schema.items() if isinstance(name, str)),
        key=lambda item: (item[0].casefold(), item[0]),
    )
    if len(table_items) != len(schema):
        raise ProductionResearchAssemblyError("captured schema table names must be text")
    snapshot: dict[str, object] = {
        "catalog": [],
        "tables": {},
        "table_count": len(table_items),
        "truncated": True,
    }
    catalog = snapshot["catalog"]
    tables = snapshot["tables"]
    assert isinstance(catalog, list)
    assert isinstance(tables, dict)
    for table_name, body in table_items:
        candidate = {
            **snapshot,
            "catalog": [*catalog, table_name],
            "tables": {**tables, table_name: body},
            "omitted_table_count": len(table_items) - len(catalog) - 1,
            "omitted_table_details_count": len(catalog) - len(tables),
        }
        encoded_candidate = _encode_if_within_budget(
            {"schema": candidate, **context},
            policy,
            maximum_bytes,
        )
        if encoded_candidate is None or not fits_prompt(encoded_candidate):
            name_only = {
                **snapshot,
                "catalog": [*catalog, table_name],
                "tables": tables,
                "omitted_table_count": len(table_items) - len(catalog) - 1,
                "omitted_table_details_count": len(catalog) + 1 - len(tables),
            }
            encoded_name_only = _encode_if_within_budget(
                {"schema": name_only, **context},
                policy,
                maximum_bytes,
            )
            if encoded_name_only is None or not fits_prompt(encoded_name_only):
                break
            catalog.append(table_name)
            continue
        catalog.append(table_name)
        tables[table_name] = body
    snapshot["omitted_table_count"] = len(table_items) - len(catalog)
    snapshot["omitted_table_details_count"] = len(catalog) - len(tables)
    return snapshot


def _encode_if_within_budget(
    value: object,
    policy: AdaptivePolicyConfig,
    maximum_bytes: int,
) -> bytes | None:
    try:
        return canonical_json_bytes(
            value,
            limits=SerializationLimits(
                max_state_bytes=maximum_bytes,
                max_inline_rows=policy.result_volume.returned_rows,
            ),
        )
    except (InlineRowsLimitError, StateSizeLimitError):
        return None


__all__ = (
    "ProductionResearchAssembly",
    "ProductionResearchAssemblyError",
    "assemble_production_research",
    "run_production_schema_research",
    "stable_schema_research_model_identity",
)
