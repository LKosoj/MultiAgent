"""Durable asynchronous coordinator for one-action schema research turns."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import inspect
import json
import logging
import math
import re
import time
from typing import Awaitable, Literal
import uuid

from pydantic import ValidationError
from sqlglot import exp, parse
from sqlglot.errors import ParseError, TokenError

from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.adaptive_research_state_store import (
    AdaptiveResearchStateStore,
    AdaptiveResearchStateStoreError,
)
from workflow.adaptive_state_store import (
    AdaptiveCheckpointCasError,
    AdaptiveCheckpointError,
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.deadline import (
    DeadlineBudget,
    WorkflowDeadlineExceeded,
    execute_step_attempt,
)

from ..schema_loader import LoadedSchema
from ..utils import get_table_columns
from .decision_resolver import (
    DecisionExecutionError,
    DecisionResolverError,
    DuplicateResearchActionError,
    ResolvedResearchDecision,
    UnresolvableModelDecisionError,
    execute_resolved_research_decision,
    resolve_research_decision,
)
from .freshness import FreshnessContext, FreshnessStatus, evaluate_evidence_freshness
from .model_budget import ModelTokenUsage
from .models import (
    BindingStatus,
    BudgetState,
    ColumnRef,
    DiscriminatorValueBinding,
    DocumentRef,
    DerivedExpressionBinding,
    EvidenceSourceKind,
    EvidenceRecord,
    HypothesisStatus,
    JoinCandidate,
    JoinEdge,
    JoinCandidateStatus,
    LiteralValue,
    PhysicalColumnBinding,
    PredicateRef,
    PredicateOperator,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    ResearchStopReason,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    TableRef,
)
from .policy import (
    AdaptivePolicyConfig,
    BudgetAdmissionError,
    completed_model_budget_chain,
    evaluate_research_generation_authority,
    execute_model_call_with_budget_async,
    validate_state_model_budget_policy,
)
from .probes import ProbeResult, ProbeStatus, deserialize_probe_result
from .provenance import (
    MalformedProvenanceError,
    parse_probe_observation,
)
from .replay_inputs import (
    ResearchSemanticReplayInput,
    ResearchTerminalReplayInput,
)
from .research_decision import (
    BindingAssessment,
    DiscriminatorValueCandidate,
    DerivedExpressionCandidate,
    ExistingBindingRef,
    ExistingHypothesisRef,
    ExistingJoinRef,
    ExecuteResearchProbeIntent,
    HypothesisAssessment,
    JoinAssessment,
    LogicalColumnRef,
    NewBindingProposal,
    NewJoinProposal,
    PhysicalColumnCandidate,
    ProposedHypothesisRef,
    ResearchDecisionV1,
    SemanticCommitRequest,
    StopRequest,
    ToolIntent,
)
from ._decision_resolver_validation import (
    ModelDecisionReferenceError,
    expand_model_identifier_handles,
)
from ._semantic_value_certificate import (
    ExactValueCertificateError,
    evidence_observes_exact_column,
    evidence_observes_exact_value,
    predicate_has_exact_value_certificate,
)
from .semantic_reducer import (
    _categorical_in_recovery_replacement_certificate,
    _declared_join_certificate,
    _negative_hypothesis_certificate,
)
from .research_query import (
    RawResearchQuery,
    ResearchQueryAdmissionError,
    admit_research_query,
    dialect_for_plugin,
)
from .schema_research_agent import (
    SchemaResearchDecisionAdapter,
    SchemaResearchDecisionModel,
    SchemaResearchStopReviewAdapter,
    SchemaResearchValidationFeedback,
)
from .semantic_coverage import CoverageInputErrorCode
from ._semantic_coverage_boundary import evidence_has_state_authority
from ._research_terminal_authority import (
    _affected_source_ids,
    _authority_stop_reason,
    _terminal_envelope,
    _terminal_replay_is_authorized,
)
from .ambiguity import AmbiguityReport
from .semantic_reducer import (
    SemanticCommitResult,
    SemanticTurnAdmission,
    SemanticReducerError,
    commit_semantic_turn,
)
from .state import ResearchTransitionConflictError, ResearchTransitionProtocolError
from .serialization import (
    ContractDecodeError,
    canonical_digest,
    canonical_json_bytes,
    deserialize_as,
)
from .tool_registry import AdaptiveResearchToolRegistry


logger = logging.getLogger(__name__)

_MAX_MODEL_REJECTIONS_WITHOUT_PROGRESS = 5
_MAX_INVALID_STOP_REJECTIONS_WITHOUT_PROGRESS = 2
_MAX_REPEATED_MODEL_REJECTIONS_WITHOUT_PROGRESS = 2
_BINDING_ID_IN_HINT = re.compile(r"\bbinding:[A-Za-z0-9][A-Za-z0-9._:-]*")
_SOURCE_ID_IN_HINT = re.compile(r"\bsemantic:[A-Za-z0-9][A-Za-z0-9._:-]*")
_QUALIFIED_PHYSICAL_COLUMN_IN_HINT = re.compile(
    r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.){1,2}[A-Za-z_][A-Za-z0-9_]*\b"
)
_MISSING_EXACT_PREDICATE_HINT_MARKER = (
    " Create a separate new_binding for each missing trusted exact formula "
    "predicate before semantic_commit or SQL; this is an addition, not a "
    "replacement for an existing CANDIDATE: "
)


def _rejected_preflight_missing_exact_predicates(
    rejected_assessments: tuple[dict[str, object], ...],
    state: ResearchState,
    constraints: tuple[tuple[str, DocumentRef, str, str], ...],
) -> dict[str, tuple[str, ...]]:
    """Return trusted exact predicates still missing after rejected preflight."""

    constrained = {
        (source_id, column.casefold()): (column, literal)
        for source_id, _document, column, literal in constraints
    }
    missing: dict[str, set[str]] = {}
    for rejected in rejected_assessments:
        source_id = rejected.get("source_id")
        columns = rejected.get("missing_exact_predicate_columns")
        if not isinstance(source_id, str) or not isinstance(columns, list):
            continue
        for value in columns:
            if not isinstance(value, str):
                continue
            constraint = constrained.get((source_id, value.casefold()))
            if constraint is None:
                continue
            column, literal = constraint
            if any(
                isinstance(binding, DiscriminatorValueBinding)
                and binding.status in (BindingStatus.CANDIDATE, BindingStatus.SUPPORTED)
                and binding.source_id == source_id
                and binding.discriminator_column.column.casefold()
                == column.casefold()
                and binding.discriminator_predicate.operator is PredicateOperator.EQ
                and binding.discriminator_predicate.right == literal
                for binding in state.bindings
            ):
                continue
            missing.setdefault(source_id, set()).add(column)
    return {
        source_id: tuple(sorted(columns))
        for source_id, columns in sorted(missing.items())
    }


def _validated_stop_review_hint(
    hint: str,
    state: ResearchState,
    additional_assessment_binding_ids: tuple[str, ...] = (),
    loaded_schema: LoadedSchema | None = None,
    missing_exact_predicates: Mapping[str, tuple[str, ...]] | None = None,
) -> str:
    """Do not let an advisory hint override durable identifiers."""

    durable_binding_ids = {binding.binding_id for binding in state.bindings}
    durable_source_ids = {
        item.source_id for item in state.query_spec.semantic_items
    }

    def replace_unknown_binding(match: re.Match[str]) -> str:
        candidate = match.group(0)
        if candidate in durable_binding_ids:
            return candidate
        without_sentence_punctuation = candidate.rstrip(".,;!?")
        if without_sentence_punctuation in durable_binding_ids:
            return candidate
        return "the exact durable binding_id for the affected source_id"

    validated_hint = _BINDING_ID_IN_HINT.sub(
        replace_unknown_binding,
        hint,
    )

    def replace_unknown_source(match: re.Match[str]) -> str:
        candidate = match.group(0)
        if candidate in durable_source_ids:
            return candidate
        without_sentence_punctuation = candidate.rstrip(".,;!?")
        if without_sentence_punctuation in durable_source_ids:
            return candidate
        return "the exact durable source_id for the affected semantic item"

    validated_hint = _SOURCE_ID_IN_HINT.sub(replace_unknown_source, validated_hint)
    validated_hint = validated_hint.partition(
        _MISSING_EXACT_PREDICATE_HINT_MARKER
    )[0]
    if isinstance(loaded_schema, LoadedSchema):
        durable_id_spans = tuple(
            match.span()
            for identifier_pattern in (_BINDING_ID_IN_HINT, _SOURCE_ID_IN_HINT)
            for match in identifier_pattern.finditer(validated_hint)
        )
        schema_tables = {
            table_name.casefold(): {
                column_name.casefold()
                for column_name in get_table_columns(table_schema)
            }
            for table_name, table_schema in loaded_schema.schema.items()
        }

        def replace_unknown_physical_column(match: re.Match[str]) -> str:
            if any(
                start <= match.start() and match.end() <= end
                for start, end in durable_id_spans
            ):
                return match.group(0)
            reference_parts = match.group(0).split(".")
            table_reference = ".".join(reference_parts[:-1]).casefold()
            column_name = reference_parts[-1].casefold()
            matching_columns = schema_tables.get(table_reference)
            if matching_columns is None and len(reference_parts) == 2:
                matching_table_columns = [
                    columns
                    for table_name, columns in schema_tables.items()
                    if table_name.rsplit(".", 1)[-1] == table_reference
                ]
                if len(matching_table_columns) == 1:
                    matching_columns = matching_table_columns[0]
            if matching_columns is not None and column_name not in matching_columns:
                return "the verified physical column for the resolved table"
            return match.group(0)

        validated_hint = _QUALIFIED_PHYSICAL_COLUMN_IN_HINT.sub(
            replace_unknown_physical_column,
            validated_hint,
        )
    durable_evidence_ids = {record.evidence_id for record in state.evidence}
    assessment_binding_ids = {
        binding_id
        for item in state.query_spec.semantic_items
        if item.required and item.status is not SemanticItemStatus.RESOLVED
        for binding_id in item.binding_ids
    }
    assessment_binding_ids.update(additional_assessment_binding_ids)

    def lacks_exact_value_certificate(
        binding: DiscriminatorValueBinding,
    ) -> bool:
        try:
            return not predicate_has_exact_value_certificate(
                binding.discriminator_predicate,
                tuple(
                    record
                    for record in state.evidence
                    if record.evidence_id in binding.evidence_ids
                ),
            )
        except (ExactValueCertificateError, MalformedProvenanceError):
            return True

    candidate_bindings = [
        {
            "binding_id": binding.binding_id,
            "evidence_ids": sorted(
                evidence_id
                for evidence_id in binding.evidence_ids
                if evidence_id in durable_evidence_ids
            ),
        }
        for binding in state.bindings
        if binding.status is BindingStatus.CANDIDATE
        and binding.binding_id in assessment_binding_ids
        and any(
            evidence_id in durable_evidence_ids
            for evidence_id in binding.evidence_ids
        )
    ]
    missing_predicate_block = ""
    if missing_exact_predicates:
        missing_predicate_block = (
            _MISSING_EXACT_PREDICATE_HINT_MARKER
            + json.dumps(
                missing_exact_predicates,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "."
        )
    if missing_predicate_block:
        return f"{validated_hint}{missing_predicate_block}"
    if any(
        isinstance(binding, DiscriminatorValueBinding)
        and binding.status is BindingStatus.CANDIDATE
        and binding.binding_id in assessment_binding_ids
        and binding.discriminator_predicate.operator is PredicateOperator.IN
        and lacks_exact_value_certificate(binding)
        for binding in state.bindings
    ):
        return validated_hint
    if not candidate_bindings:
        return validated_hint
    auto_assessment_block = (
        " Use nonempty binding_assessment proposals, not new_binding, "
        "for these existing CANDIDATE bindings, citing only their listed durable "
        f"evidence_ids: {json.dumps(candidate_bindings, ensure_ascii=False, separators=(',', ':'))}. "
        "Then semantic_commit with those assessments; never use an empty semantic_commit. "
        "do not create a replacement binding."
    )
    if auto_assessment_block in validated_hint:
        return validated_hint
    return f"{validated_hint}{auto_assessment_block}"


def _auto_binding_assessment_hint_is_closed(
    hint: str,
    state: ResearchState,
) -> bool:
    """Return true only for a closed canonical assessment hint we generated."""

    marker = (
        " Use nonempty binding_assessment proposals, not new_binding, "
        "for these existing CANDIDATE bindings, citing only their listed durable "
        "evidence_ids: "
    )
    suffix = (
        ". Then semantic_commit with those assessments; never use an empty "
        "semantic_commit. do not create a replacement binding."
    )
    prefix, separator, serialized_candidates = hint.partition(marker)
    if not prefix or not separator or not serialized_candidates.endswith(suffix):
        return False
    try:
        candidates = json.loads(serialized_candidates[: -len(suffix)])
    except (TypeError, ValueError):
        return False
    if not isinstance(candidates, list) or not candidates:
        return False

    bindings_by_id = {binding.binding_id: binding for binding in state.bindings}
    durable_evidence_ids = {record.evidence_id for record in state.evidence}
    binding_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {
            "binding_id",
            "evidence_ids",
        }:
            return False
        binding_id = candidate["binding_id"]
        evidence_ids = candidate["evidence_ids"]
        if (
            not isinstance(binding_id, str)
            or not binding_id
            or binding_id in binding_ids
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or len(evidence_ids) != len(set(evidence_ids))
            or not all(
                isinstance(evidence_id, str) and evidence_id
                for evidence_id in evidence_ids
            )
        ):
            return False
        binding_ids.add(binding_id)
        binding = bindings_by_id.get(binding_id)
        if binding is None or binding.status is not BindingStatus.SUPPORTED:
            return False
        expected_evidence_ids = sorted(
            evidence_id
            for evidence_id in binding.evidence_ids
            if evidence_id in durable_evidence_ids
        )
        if evidence_ids != expected_evidence_ids:
            return False
        source_items = [
            item
            for item in state.query_spec.semantic_items
            if item.required and item.source_id == binding.source_id
        ]
        if not source_items or any(
            item.status is not SemanticItemStatus.RESOLVED
            for item in source_items
        ):
            return False
    return True


@dataclass(frozen=True, slots=True)
class ResearchLoopOutcome:
    """Closed, partial-safe result of schema research."""

    final_state: ResearchState
    stop_reason: ResearchStopReason
    affected_source_ids: tuple[str, ...]
    citation_evidence_ids: tuple[str, ...]
    ambiguity: AmbiguityReport | None
    rejection_signatures: tuple[tuple[str, str], ...] = ()
    freshness_context: FreshnessContext | None = None


class _ModelWaitCancelled(Exception):
    pass


class _ModelWaitDeadline(Exception):
    pass


class _ResearchLoopCoordinator:
    """Private serial coordinator; model reasoning is never checkpointed."""

    def __init__(
        self,
        *,
        initial_state: ResearchState,
        task: str,
        research_context: Callable[..., str],
        model: SchemaResearchDecisionModel,
        model_identity: str,
        adapter: SchemaResearchDecisionAdapter,
        loaded_schema: LoadedSchema,
        freshness_context: FreshnessContext,
        registry: AdaptiveResearchToolRegistry,
        state_store: AdaptiveResearchStateStore,
        checkpoint_store: AdaptiveStateStore,
        budget_ledger: AdaptiveBudgetLedger,
        policy: AdaptivePolicyConfig,
        deadline: DeadlineBudget | None,
        is_cancelled: Callable[[], bool],
        model_claim_now_ns: Callable[[], int],
        model_owner_token_factory: Callable[[], str],
        model_wait: Callable[[float], Awaitable[None]] | None,
        semantic_repair_continuation: bool = False,
        exact_formula_documents: tuple[tuple[str, DocumentRef], ...] = (),
        exact_formula_predicate_constraints: tuple[
            tuple[str, DocumentRef, str, str], ...
        ] = (),
        stop_review_model: SchemaResearchDecisionModel | None = None,
    ) -> None:
        self._initial_state = _revalidate_state(initial_state)
        self._task = _require_text(task, "task")
        self._research_context = research_context
        self._model = model
        self._stop_review_model = model if stop_review_model is None else stop_review_model
        self._model_identity = _require_text(model_identity, "model_identity")
        self._adapter = adapter
        self._loaded_schema = loaded_schema
        self._freshness_context = _revalidate_freshness(freshness_context)
        self._registry = registry
        self._state_store = state_store
        self._checkpoint_store = checkpoint_store
        self._budget_ledger = budget_ledger
        self._policy = policy
        self._deadline = deadline
        self._is_cancelled = is_cancelled
        self._model_claim_now_ns = model_claim_now_ns
        self._model_owner_token_factory = model_owner_token_factory
        self._model_wait = model_wait or self._wait_for_model_follower
        self._semantic_repair_continuation = semantic_repair_continuation
        self._exact_formula_documents = exact_formula_documents
        self._exact_formula_predicate_constraints = exact_formula_predicate_constraints
        self._latest_state = self._initial_state
        self._model_stagnation_signatures: tuple[tuple[str, str], ...] = ()
        self._pending_rejected_preflight_assessments: tuple[
            dict[str, object], ...
        ] = ()
        self._pending_stop_review_hint: str | None = None
        self._exact_physical_predicate_continuation_source_ids: tuple[str, ...] = ()
        self._last_stop_review_hint: str | None = None

    async def run(self) -> ResearchLoopOutcome:
        state, failed = self._load_or_save_initial()
        if failed is not None:
            return self._outcome(state, failed)
        try:
            validation_feedback = self._failed_probe_feedback_from_replay(state)
        except (AdaptiveResearchStateStoreError, TypeError, ValueError):
            return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
        while True:
            try:
                key = self._action_checkpoint_key(state)
                snapshot = self._checkpoint_store.get_snapshot(key)
            except AdaptiveCheckpointError:
                return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
            recover_planned = snapshot.planned is not None
            if snapshot.terminal is not None:
                try:
                    terminal = _terminal_envelope(snapshot.terminal.action, state)
                except (TypeError, ValidationError, ValueError):
                    return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
                continue_unbound_formula = (
                    self._semantic_repair_continuation
                    and terminal["reason"] == ResearchStopReason.COMPLETE.value
                    and _has_pending_required_formula_continuation(
                        state,
                        self._freshness_context,
                        self._exact_formula_documents,
                    )
                )
                if continue_unbound_formula:
                    snapshot = replace(snapshot, terminal=None)
                else:
                    try:
                        freshness_context = self._terminal_replay_freshness_context(key)
                        if freshness_context is None:
                            return self._outcome(
                                state,
                                ResearchStopReason.PROTOCOL_FAILURE,
                            )
                        state = _state_with_reconciled_model_budget(
                            state, self._budget_ledger, self._policy
                        )
                        state = self._state_with_terminal_probe_budget(state, snapshot)
                    except (
                        AdaptiveCheckpointError,
                        BudgetAdmissionError,
                        TypeError,
                        ValueError,
                    ):
                        return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
                    return self._outcome_from_terminal(
                        state,
                        snapshot.terminal.action,
                        freshness_context,
                    )

            if snapshot.observed is not None:
                aborted = _abort_reason(snapshot.observed.action)
                if aborted is not None:
                    return self._stop(state, aborted)
                if snapshot.planned is None:
                    return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
                resolved, reason = self._resolve_planned(
                    state, snapshot.planned.action, check_boundary=False
                )
                if resolved is None or reason is not None:
                    return self._stop(
                        state, reason or ResearchStopReason.PROTOCOL_FAILURE
                    )
                if _is_semantic_observed(snapshot.observed.action):
                    action = resolved.admission.action
                    if (
                        action is None
                        or action.kind is not ResearchActionKind.SEMANTIC_COMMIT
                    ):
                        return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
                    admission = resolved.admission
                    try:
                        committed = commit_semantic_turn(admission)
                    except (SemanticReducerError, ValidationError, ValueError, TypeError):
                        return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
                    reason = self._save_semantic_transition(
                        state, committed.state, resolved, admission, None
                    )
                    if reason is not None:
                        return self._stop(state, reason)
                    state = committed.state
                    continue
                probe_result = _probe_from_observed(snapshot.observed.action)
                if probe_result is None:
                    return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
                if not _probe_matches_resolution(probe_result, resolved):
                    return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
                failure_reason = _probe_failure_reason(probe_result)
                if (
                    failure_reason is not None
                    and probe_result.status is not ProbeStatus.FAILED
                ):
                    try:
                        state = self._state_with_reconciled_probe_budget(
                            state, resolved
                        )
                    except (BudgetAdmissionError, TypeError, ValueError):
                        return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
                    return self._stop(state, failure_reason)
                try:
                    admission = self._admission_with_reconciled_budget(resolved)
                    committed = commit_semantic_turn(
                        admission,
                        probe_result=probe_result,
                    )
                except (SemanticReducerError, ValidationError, ValueError, TypeError):
                    return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
                reason = self._save_semantic_transition(
                    state,
                    committed.state,
                    resolved,
                    admission,
                    probe_result,
                )
                if reason is not None:
                    return self._stop(state, reason)
                state = committed.state
                if probe_result.status is ProbeStatus.FAILED:
                    validation_feedback = "PROBE_UNAVAILABLE"
                continue

            reason = self._boundary_reason()
            if reason is not None:
                return self._stop(state, reason)
            authority = evaluate_research_generation_authority(
                state,
                self._freshness_context,
                state.run_id,
                state.run_incarnation,
            )
            authority_reason = _authority_stop_reason(authority)
            if authority_reason is ResearchStopReason.PROTOCOL_FAILURE:
                return self._stop(
                    state,
                    authority_reason,
                    affected_source_ids=authority.affected_source_ids,
                )
            terminal_freshness_context = self._freshness_context
            terminal_authority = evaluate_research_generation_authority(
                state,
                terminal_freshness_context,
                state.run_id,
                state.run_incarnation,
            )
            terminal_reason = _authority_stop_reason(terminal_authority)
            if terminal_reason is ResearchStopReason.PROTOCOL_FAILURE:
                return self._stop(
                    state,
                    terminal_reason,
                    affected_source_ids=terminal_authority.affected_source_ids,
                )
            if terminal_reason is ResearchStopReason.COMPLETE:
                pending_formula_continuation = (
                    self._semantic_repair_continuation
                    and (
                        _has_pending_required_formula_continuation(
                            state,
                            self._freshness_context,
                            self._exact_formula_documents,
                        )
                        or _has_unbound_latest_probe_evidence(state)
                    )
                )
                selected_binding_ids = {
                    binding_id
                    for item in state.query_spec.semantic_items
                    if item.required
                    for binding_id in item.binding_ids
                }
                if not pending_formula_continuation and not any(
                    binding.status is BindingStatus.CANDIDATE
                    and binding.binding_id in selected_binding_ids
                    for binding in state.bindings
                ):
                    return self._stop(
                        state,
                        terminal_reason,
                        freshness_context=terminal_freshness_context,
                    )
            if _consecutive_non_novel(self._checkpoint_store, state) >= 2:
                if self._stop_review_used_for_non_novel_streak(state):
                    if not self._stop_review_follow_up_decision_allowed(state):
                        return self._stop(state, ResearchStopReason.STAGNATED)
                    self._pending_stop_review_hint = None
                    self._last_stop_review_hint = None
                elif self._pending_stop_review_hint is None:
                    generation_authority = None
                    if (
                        not terminal_authority.allowed
                        and terminal_reason is None
                    ):
                        assert terminal_authority.reason is not None
                        generation_authority = (
                            terminal_authority.reason,
                            tuple(sorted(terminal_authority.affected_source_ids)),
                        )
                    context = self._research_context(
                        state, (), (), (), generation_authority
                    )
                    hint, _ = await self._review_stop(
                        state,
                        ResearchStopReason.STAGNATED,
                        context,
                        self._next_model_attempt(state),
                    )
                    if hint is None:
                        return self._stop(state, ResearchStopReason.STAGNATED)
                    self._pending_stop_review_hint = hint

            if snapshot.planned is None:
                decision, reason, model_stop_freshness_context = await self._model_decision(
                    state, validation_feedback
                )
                validation_feedback = None
                if reason is not None:
                    return self._stop(state, reason)
                assert decision is not None
                if isinstance(decision.next, StopRequest):
                    if decision.proposals:
                        return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
                    stop_freshness_context = self._freshness_context
                    if _model_stop_reason(decision) is ResearchStopReason.COMPLETE:
                        if model_stop_freshness_context is None:
                            return self._stop(
                                state, ResearchStopReason.PROTOCOL_FAILURE
                            )
                        stop_freshness_context = model_stop_freshness_context
                    reason = _validate_model_stop(
                        state, decision, stop_freshness_context
                    )
                    if reason is ResearchStopReason.PROTOCOL_FAILURE:
                        return self._stop(state, reason)
                    return self._stop(
                        state,
                        reason,
                        affected_source_ids=decision.next.source_ids,
                        citation_evidence_ids=decision.next.citation_evidence_ids,
                        ambiguity=decision.next.ambiguity,
                        freshness_context=(
                            stop_freshness_context
                            if reason is ResearchStopReason.COMPLETE
                            else None
                        ),
                    )
                resolved, reason = self._resolve(state, decision)
                if reason is not None:
                    return self._stop(state, reason)
                assert resolved is not None
                reason = self._record_planned(state, resolved)
                if reason is not None:
                    return self._stop(state, reason)
            else:
                resolved, reason = self._resolve_planned(state, snapshot.planned.action)
                if reason is not None:
                    return self._stop(state, reason)
                assert resolved is not None

            if (
                resolved.admission.action is not None
                and resolved.admission.action.kind is ResearchActionKind.SEMANTIC_COMMIT
            ):
                admission = resolved.admission
                try:
                    committed = commit_semantic_turn(admission)
                except (SemanticReducerError, ValidationError, ValueError, TypeError):
                    return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
                reason = self._record_observed(
                    state,
                    resolved,
                    None,
                    _is_semantically_novel_turn(state, committed),
                )
                if reason is not None:
                    return self._stop(state, reason)
                reason = self._save_semantic_transition(
                    state, committed.state, resolved, admission, None
                )
                if reason is not None:
                    return self._stop(state, reason)
                state = committed.state
                continue

            probe_result, reason = self._execute_or_recover(
                resolved, recover=recover_planned
            )
            if reason is not None:
                return self._stop(state, reason)
            assert probe_result is not None
            if not _probe_matches_resolution(probe_result, resolved):
                return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
            failure_reason = _probe_failure_reason(probe_result)
            if (
                failure_reason is not None
                and probe_result.status is not ProbeStatus.FAILED
            ):
                reason = self._record_observed(state, resolved, probe_result, False)
                if reason is not None:
                    return self._stop(state, reason)
                try:
                    state = self._state_with_reconciled_probe_budget(state, resolved)
                except (BudgetAdmissionError, TypeError, ValueError):
                    return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
                return self._stop(state, failure_reason)
            try:
                admission = self._admission_with_reconciled_budget(resolved)
                committed = commit_semantic_turn(
                    admission,
                    probe_result=probe_result,
                )
            except (SemanticReducerError, ValidationError, ValueError, TypeError):
                return self._stop(state, ResearchStopReason.PROTOCOL_FAILURE)
            reason = self._record_observed(
                state,
                resolved,
                probe_result,
                _is_semantically_novel_turn(state, committed),
            )
            if reason is not None:
                return self._stop(state, reason)
            reason = self._save_semantic_transition(
                state,
                committed.state,
                resolved,
                admission,
                probe_result,
            )
            if reason is not None:
                return self._stop(state, reason)
            state = committed.state
            if probe_result.status is ProbeStatus.FAILED:
                validation_feedback = "PROBE_UNAVAILABLE"

    def _load_or_save_initial(self) -> tuple[ResearchState, ResearchStopReason | None]:
        state = self._initial_state
        try:
            stored = self._state_store.load_latest_research_state(
                state.run_id, state.run_incarnation
            )
            if stored is None:
                self._state_store.save_research_state(
                    state, expected_previous_revision=None
                )
                self._latest_state = state
                return state, None
            self._latest_state = stored
            return stored, None
        except AdaptiveResearchStateStoreError:
            return state, ResearchStopReason.PROTOCOL_FAILURE

    async def _model_decision(
        self,
        state: ResearchState,
        validation_feedback: SchemaResearchValidationFeedback | None = None,
    ) -> tuple[
        ResearchDecisionV1 | None,
        ResearchStopReason | None,
        FreshnessContext | None,
    ]:
        if not _is_async_model(self._model):
            return None, ResearchStopReason.PROTOCOL_FAILURE, None
        reason = self._boundary_reason()
        if reason is not None:
            return None, reason, None
        limits = self._policy.model_budget
        if limits is None:
            return None, ResearchStopReason.BUDGET_EXHAUSTED, None
        decision: ResearchDecisionV1 | None = None
        terminal_freshness_context: FreshnessContext | None = None
        rejected_without_progress = 0
        rejection_signatures: set[tuple[str, str]] = set()
        rejection_counts: dict[tuple[str, str], int] = {}
        last_rejection_identity: tuple[str, str, str | None] | None = None
        consecutive_rejections = 0
        validation_feedbacks = (
            (validation_feedback,) if validation_feedback is not None else ()
        )
        rejected_duplicate_actions: tuple[dict[str, object], ...] = ()
        rejected_preflight_assessments = self._pending_rejected_preflight_assessments
        immediate_retry_hint_source_ids: tuple[str, ...] = ()
        immediate_retry_advisory_missing_operands: dict[str, tuple[str, ...]] = {}
        if rejected_preflight_assessments:
            exact_source_ids = set(
                _runtime_exact_formula_continuation_source_ids(
                    state, self._exact_formula_documents
                )
            )
            authority = evaluate_research_generation_authority(
                state, self._freshness_context, state.run_id, state.run_incarnation
            )
            self._pending_rejected_preflight_assessments = (
                _filter_pending_rejected_preflight_assessments(
                    rejected_preflight_assessments,
                    state,
                    exact_source_ids.intersection(authority.affected_source_ids),
                )
            )
        last_unresolvable_decision_digest: str | None = None
        invalid_stop_generation_authority: (
            tuple[CoverageInputErrorCode, tuple[str, ...]] | None
        ) = None
        self._model_stagnation_signatures = ()
        exact_physical_predicate_continuation_hint = (
            _unresolved_exact_physical_predicate_stop_review_hint(
                state,
                self._exact_physical_predicate_continuation_source_ids,
            )
            if self._exact_physical_predicate_continuation_source_ids
            else None
        )
        if exact_physical_predicate_continuation_hint is None:
            self._exact_physical_predicate_continuation_source_ids = ()
        repeated_exact_physical_predicate_search_value_hint = (
            _repeated_exact_physical_predicate_search_value_hint(
                state,
                self._exact_physical_predicate_continuation_source_ids,
            )
            if exact_physical_predicate_continuation_hint is not None
            else None
        )
        stop_review_hint = self._pending_stop_review_hint
        self._pending_stop_review_hint = None

        def reject_model_decision(
            feedback: SchemaResearchValidationFeedback,
            rejection_path: Literal[
                "contract_decode",
                "stop_with_proposals",
                "invalid_stop",
                "research_query_admission",
                "duplicate_action",
                "unresolvable_preflight",
            ],
            rejection_code: str | None = None,
            decision_digest: str | None = None,
        ) -> bool:
            nonlocal consecutive_rejections, last_rejection_identity
            nonlocal rejected_without_progress, validation_feedbacks
            logger.warning(
                "typed_schema_research_decision retry=true "
                "code=%s rejection_path=%s",
                rejection_code or feedback,
                rejection_path,
            )
            if feedback not in validation_feedbacks:
                validation_feedbacks += (feedback,)
            rejected_without_progress += 1
            signature = (rejection_path, rejection_code or feedback)
            rejection_signatures.add(signature)
            rejection_counts[signature] = rejection_counts.get(signature, 0) + 1
            rejection_identity = (
                *signature,
                (
                    decision_digest
                    if rejection_path == "unresolvable_preflight"
                    else None
                ),
            )
            if rejection_identity == last_rejection_identity:
                consecutive_rejections += 1
            else:
                last_rejection_identity = rejection_identity
                consecutive_rejections = 1
            repeated_invalid_stop = (
                rejection_path == "invalid_stop"
                and rejection_counts[signature]
                >= _MAX_INVALID_STOP_REJECTIONS_WITHOUT_PROGRESS
            )
            repeated_rejection = (
                consecutive_rejections
                >= _MAX_REPEATED_MODEL_REJECTIONS_WITHOUT_PROGRESS
            )
            if (
                rejected_without_progress < _MAX_MODEL_REJECTIONS_WITHOUT_PROGRESS
                and not repeated_invalid_stop
                and not repeated_rejection
            ):
                return False
            self._model_stagnation_signatures = tuple(sorted(rejection_signatures))
            return True

        attempt = self._next_model_attempt(state)
        while attempt < limits.model_calls:
            captured: ResearchDecisionV1 | None = None
            feedback_identity: object = (
                None
                if not validation_feedbacks
                else validation_feedbacks[0]
                if len(validation_feedbacks) == 1
                else validation_feedbacks
            )
            try:
                if invalid_stop_generation_authority is not None:
                    context = self._research_context(
                        state,
                        validation_feedbacks,
                        rejected_duplicate_actions,
                        rejected_preflight_assessments,
                        invalid_stop_generation_authority,
                    )
                    invalid_stop_generation_authority = None
                elif rejected_preflight_assessments:
                    context = self._research_context(
                        state,
                        validation_feedbacks,
                        rejected_duplicate_actions,
                        rejected_preflight_assessments,
                    )
                elif rejected_duplicate_actions:
                    context = self._research_context(
                        state,
                        validation_feedbacks,
                        rejected_duplicate_actions,
                    )
                else:
                    context = self._research_context(state, validation_feedbacks)
            except BudgetAdmissionError:
                return None, ResearchStopReason.BUDGET_EXHAUSTED, None
            except Exception:
                return None, ResearchStopReason.PROTOCOL_FAILURE, None
            if not isinstance(context, str):
                return None, ResearchStopReason.PROTOCOL_FAILURE, None
            if stop_review_hint is not None:
                context += (
                    "\n\nIndependent stop review allows one normal research turn. "
                    f"Its non-authoritative hint is: {stop_review_hint}"
                )
            if (
                exact_physical_predicate_continuation_hint is not None
                and exact_physical_predicate_continuation_hint not in (stop_review_hint or "")
            ):
                context += f"\n\n{exact_physical_predicate_continuation_hint}"
            if repeated_exact_physical_predicate_search_value_hint is not None:
                context += f"\n\n{repeated_exact_physical_predicate_search_value_hint}"
            if immediate_retry_hint_source_ids:
                immediate_retry_hint = _external_exact_formula_stop_review_hint(
                    state,
                    self._exact_formula_documents,
                    immediate_retry_hint_source_ids,
                    self._loaded_schema,
                    immediate_retry_advisory_missing_operands,
                    self._exact_formula_predicate_constraints,
                )
                if immediate_retry_hint is not None:
                    context += f"\n\n{immediate_retry_hint}"
            if (
                limits.model_calls >= _MAX_MODEL_REJECTIONS_WITHOUT_PROGRESS + 2
                and not self._stop_review_used_for_revision(state)
                and not self._stop_review_used_for_non_novel_streak(state)
                and not self._model_budget_supports(3)
            ):
                stop_review_hint, attempt = await self._review_stop(
                    state,
                    ResearchStopReason.BUDGET_EXHAUSTED,
                    context,
                    attempt,
                )
                if stop_review_hint is None:
                    return None, ResearchStopReason.BUDGET_EXHAUSTED, None
                continue
            if not self._model_budget_supports(1):
                return None, ResearchStopReason.BUDGET_EXHAUSTED, None
            request_identity = {
                "research_context": context,
                "state": state.model_dump(mode="json", by_alias=True),
                "task": self._task,
                "validation_feedback": feedback_identity,
            }
            if rejected_duplicate_actions:
                request_identity["rejected_duplicate_actions"] = (
                    rejected_duplicate_actions
                )
            if rejected_preflight_assessments:
                request_identity["rejected_preflight_assessments"] = (
                    rejected_preflight_assessments
                )
            request_digest = canonical_digest(request_identity)
            call_attempt = attempt
            attempt += 1

            async def _call(_: object) -> ModelTokenUsage:
                nonlocal captured

                async def invoke(
                    _context: object,
                ) -> tuple[ResearchDecisionV1, ModelTokenUsage]:
                    return await self._adapter.propose_with_usage(
                        self._model,
                        task=self._task,
                        research_context=context,
                        validation_feedback=validation_feedbacks or None,
                    )

                proposed, usage = await execute_step_attempt(
                    "schema research model",
                    invoke,
                    None,
                    attempt_timeout=None,
                    deadline=self._deadline,
                )
                captured = proposed
                return usage

            try:
                await execute_model_call_with_budget_async(
                    state.run_id,
                    state.run_incarnation,
                    _model_call_id(state, call_attempt),
                    request_digest,
                    self._model_identity,
                    limits.input_tokens_per_call,
                    limits.output_tokens_per_call,
                    _call,
                    config=self._policy,
                    ledger=self._budget_ledger,
                    claim_now_ns=self._model_claim_now_ns,
                    owner_token_factory=self._model_owner_token_factory,
                    wait=self._model_wait,
                )
                stop_review_hint = None
            except _ModelWaitCancelled:
                return None, ResearchStopReason.CANCELLED, None
            except _ModelWaitDeadline:
                return None, ResearchStopReason.DEADLINE_EXCEEDED, None
            except WorkflowDeadlineExceeded:
                return None, ResearchStopReason.DEADLINE_EXCEEDED, None
            except asyncio.CancelledError:
                return None, ResearchStopReason.CANCELLED, None
            except BudgetAdmissionError:
                return None, ResearchStopReason.BUDGET_EXHAUSTED, None
            except ContractDecodeError:
                if reject_model_decision("INVALID_DECISION", "contract_decode"):
                    stop_review_hint, attempt = await self._review_stop(
                        state,
                        ResearchStopReason.STAGNATED,
                        context,
                        attempt,
                    )
                    if stop_review_hint is not None:
                        continue
                    return None, ResearchStopReason.STAGNATED, None
                continue
            except Exception as error:
                logger.warning(
                    "typed_schema_research_decision retry=false "
                    "code=PROVIDER_OR_ADAPTER error_class=%s",
                    type(error).__name__,
                )
                return None, ResearchStopReason.PROTOCOL_FAILURE, None
            reason = self._boundary_reason()
            if reason is not None:
                return None, reason, None
            if captured is not None:
                try:
                    captured = expand_model_identifier_handles(state, captured)
                except ModelDecisionReferenceError:
                    if reject_model_decision(
                        "UNRESOLVABLE_PREFLIGHT",
                        "unresolvable_preflight",
                    ):
                        return None, ResearchStopReason.STAGNATED, None
                    continue
                pending_formula_continuation_source_ids: tuple[str, ...] = ()
                if (
                    isinstance(captured.next, StopRequest)
                    and captured.next.reason == "complete"
                    and self._semantic_repair_continuation
                ):
                    pending_formula_continuation_source_ids = (
                        _pending_required_formula_continuation_source_ids(
                            state,
                            self._freshness_context,
                            self._exact_formula_documents,
                        )
                    )
                complete_repair_pending = (
                    bool(pending_formula_continuation_source_ids)
                )
                if not complete_repair_pending:
                    captured = _normalize_complete_stop_citations(
                        state,
                        captured,
                        self._freshness_context,
                    )
                if (
                    isinstance(captured.next, ToolIntent)
                    and isinstance(
                        captured.next.intent, ExecuteResearchProbeIntent
                    )
                    and state.budget_state.remaining_rows > 0
                ):
                    runtime = self._registry.context.data_runtime
                    dsn = getattr(runtime, "dsn", None)
                    get_plugin = getattr(runtime, "get_plugin", None)
                    if get_plugin is None:
                        from db_plugins import get_plugin as default_get_plugin

                        get_plugin = default_get_plugin
                    if type(dsn) is str and callable(get_plugin):
                        try:
                            captured = _cap_execute_research_probe_limit(
                                captured,
                                maximum_row_limit=(
                                    state.budget_state.remaining_rows
                                ),
                                dialect=dialect_for_plugin(get_plugin(dsn)),
                            )
                        except ResearchQueryAdmissionError:
                            pass
                invalid_stop_generation_authority = None
                if isinstance(captured.next, StopRequest):
                    if captured.proposals:
                        if reject_model_decision(
                            "STOP_WITH_PROPOSALS", "stop_with_proposals"
                        ):
                            stop_review_hint, attempt = await self._review_stop(
                                state,
                                ResearchStopReason.STAGNATED,
                                context,
                                attempt,
                            )
                            if stop_review_hint is not None:
                                continue
                            return None, ResearchStopReason.STAGNATED, None
                        continue
                    stop_freshness_context = self._freshness_context
                    if _model_stop_reason(captured) is ResearchStopReason.COMPLETE:
                        stop_freshness_context = self._freshness_context
                    if complete_repair_pending or (
                        _validate_model_stop(
                            state, captured, stop_freshness_context
                        )
                        is ResearchStopReason.PROTOCOL_FAILURE
                    ):
                        invalid_stop_generation_authority = (
                            (
                                CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
                                pending_formula_continuation_source_ids,
                            )
                            if complete_repair_pending
                            else _invalid_complete_generation_authority(
                                state, captured, stop_freshness_context
                            )
                        )
                        if reject_model_decision("INVALID_STOP", "invalid_stop"):
                            if invalid_stop_generation_authority is not None:
                                try:
                                    context = self._research_context(
                                        state,
                                        validation_feedbacks,
                                        rejected_duplicate_actions,
                                        rejected_preflight_assessments,
                                        invalid_stop_generation_authority,
                                    )
                                except BudgetAdmissionError:
                                    return None, ResearchStopReason.BUDGET_EXHAUSTED, None
                                except Exception:
                                    return None, ResearchStopReason.PROTOCOL_FAILURE, None
                            stop_review_hint, attempt = await self._review_stop(
                                state,
                                ResearchStopReason.STAGNATED,
                                context,
                                attempt,
                            )
                            if stop_review_hint is not None:
                                continue
                            return None, ResearchStopReason.STAGNATED, None
                        continue
                    requested_stop = _model_stop_reason(captured)
                    if requested_stop in {
                        ResearchStopReason.AMBIGUOUS,
                        ResearchStopReason.UNSUPPORTED,
                    }:
                        stop_review_hint, attempt = await self._review_stop(
                            state,
                            requested_stop,
                            context,
                            attempt,
                        )
                        if stop_review_hint is not None:
                            continue
                    if _model_stop_reason(captured) is ResearchStopReason.COMPLETE:
                        terminal_freshness_context = stop_freshness_context
                try:
                    research_query_rejection = (
                        _model_research_query_admission_feedback(
                            state,
                            captured,
                            self._loaded_schema,
                            self._registry,
                        )
                    )
                except (TypeError, ValueError):
                    return None, ResearchStopReason.PROTOCOL_FAILURE, None
                if research_query_rejection is not None:
                    research_query_feedback, rejection_code = research_query_rejection
                    if reject_model_decision(
                        research_query_feedback,
                        "research_query_admission",
                        rejection_code,
                    ):
                        stop_review_hint, attempt = await self._review_stop(
                            state,
                            ResearchStopReason.STAGNATED,
                            context,
                            attempt,
                        )
                        if stop_review_hint is not None:
                            continue
                        return None, ResearchStopReason.STAGNATED, None
                    continue
                if not isinstance(captured.next, StopRequest):
                    (
                        preflight_feedback,
                        preflight_reason,
                        rejected_action,
                        rejected_assessments,
                    ) = (
                        self._preflight_model_decision(state, captured)
                    )
                    if preflight_feedback is not None:
                        decision_digest: str | None = None
                        if (
                            preflight_feedback == "DUPLICATE_ACTION"
                            and captured.proposals
                        ):
                            proposal_commit = captured.model_copy(
                                update={"next": SemanticCommitRequest()}
                            )
                            fallback_feedback, fallback_reason, _, _ = (
                                self._preflight_model_decision(
                                    state, proposal_commit
                                )
                            )
                            if fallback_feedback is None:
                                if fallback_reason is not None:
                                    return None, fallback_reason, None
                                decision = proposal_commit
                                break
                        baseline = _proposal_free_tool_baseline(captured)
                        if (
                            preflight_feedback == "UNRESOLVABLE_PREFLIGHT"
                            and baseline is not None
                        ):
                            baseline_feedback, baseline_reason, _, _ = (
                                self._preflight_model_decision(state, baseline)
                            )
                            if baseline_feedback is None:
                                if baseline_reason is not None:
                                    return None, baseline_reason, None
                                self._pending_rejected_preflight_assessments = (
                                    rejected_assessments
                                )
                                decision = baseline
                                break
                        if rejected_action is not None:
                            rejected_duplicate_actions += (rejected_action,)
                        if preflight_feedback == "UNRESOLVABLE_PREFLIGHT":
                            decision_digest = canonical_digest(
                                captured.model_dump(mode="json", by_alias=True)
                            )
                            if decision_digest == last_unresolvable_decision_digest:
                                repeated_feedback = (
                                    "REPEATED_PREFLIGHT_DECISION"
                                )
                                if repeated_feedback not in validation_feedbacks:
                                    validation_feedbacks += (repeated_feedback,)
                            last_unresolvable_decision_digest = decision_digest
                            rejected_preflight_assessments = rejected_assessments
                            immediate_retry_advisory_missing_operands = (
                                _rejected_external_exact_formula_missing_operands(
                                    rejected_assessments,
                                    state,
                                    self._exact_formula_documents,
                                    self._loaded_schema,
                                    self._exact_formula_predicate_constraints,
                                )
                            )
                            durable_hint_source_ids = tuple(
                                source_id
                                for source_id in _mismatching_external_exact_formula_candidate_source_ids(
                                    state,
                                    self._exact_formula_documents,
                                    self._loaded_schema,
                                )
                                if _filter_pending_rejected_preflight_assessments(
                                    rejected_assessments,
                                    state,
                                    {source_id},
                                )
                            )
                            immediate_retry_hint_source_ids = tuple(
                                sorted(
                                    set(durable_hint_source_ids)
                                    | set(
                                        immediate_retry_advisory_missing_operands
                                    )
                                )
                            )
                        else:
                            rejected_preflight_assessments = ()
                        if reject_model_decision(
                            preflight_feedback,
                            (
                                "duplicate_action"
                                if preflight_feedback == "DUPLICATE_ACTION"
                                else "unresolvable_preflight"
                            ),
                            decision_digest=decision_digest,
                        ):
                            try:
                                if rejected_preflight_assessments:
                                    context = self._research_context(
                                        state,
                                        validation_feedbacks,
                                        rejected_duplicate_actions,
                                        rejected_preflight_assessments,
                                    )
                                elif rejected_duplicate_actions:
                                    context = self._research_context(
                                        state,
                                        validation_feedbacks,
                                        rejected_duplicate_actions,
                                    )
                                else:
                                    context = self._research_context(
                                        state,
                                        validation_feedbacks,
                                    )
                            except BudgetAdmissionError:
                                return None, ResearchStopReason.BUDGET_EXHAUSTED, None
                            except Exception:
                                return None, ResearchStopReason.PROTOCOL_FAILURE, None
                            stop_review_hint, attempt = await self._review_stop(
                                state,
                                ResearchStopReason.STAGNATED,
                                context,
                                attempt,
                            )
                            if stop_review_hint is not None:
                                continue
                            return None, ResearchStopReason.STAGNATED, None
                        continue
                    if preflight_reason is not None:
                        return None, preflight_reason, None
                decision = captured
                break
        if decision is None:
            return None, ResearchStopReason.BUDGET_EXHAUSTED, None
        self._model_stagnation_signatures = ()
        return decision, None, terminal_freshness_context

    def _next_model_attempt(self, state: ResearchState) -> int:
        prefix = f"research-model-{state.revision}-"
        review_prefix = f"research-stop-review-{state.revision}-"
        return sum(
            record.reservation.call_id.startswith((prefix, review_prefix))
            for record in self._budget_ledger.load_model_records(
                state.run_id, state.run_incarnation
            )
        )

    def _model_budget_supports(self, calls: int) -> bool:
        limits = self._policy.model_budget
        if limits is None:
            return False
        budget = completed_model_budget_chain(
            self._budget_ledger.load_model_records(
                self._initial_state.run_id, self._initial_state.run_incarnation
            ),
            config=self._policy,
        )
        return bool(
            budget.remaining_model_calls >= calls
            and budget.remaining_input_tokens >= calls * limits.input_tokens_per_call
            and budget.remaining_output_tokens >= calls * limits.output_tokens_per_call
            and budget.remaining_total_tokens
            >= calls * (limits.input_tokens_per_call + limits.output_tokens_per_call)
        )

    async def _review_stop(
        self,
        state: ResearchState,
        reason: ResearchStopReason,
        context: str,
        attempt: int,
    ) -> tuple[str | None, int]:
        if self._stop_review_used_for_revision(
            state
        ) or not self._model_budget_supports(2):
            return None, attempt
        captured = None
        limits = self._policy.model_budget
        assert limits is not None
        reason_text = reason.value
        exact_formula_candidate_binding_ids = (
            _runtime_exact_formula_candidate_binding_ids(
                state,
                self._exact_formula_documents,
                self._loaded_schema,
            )
        )
        exact_formula_mismatching_candidate_ids = {
            binding.binding_id
            for binding in state.bindings
            if isinstance(binding, DerivedExpressionBinding)
            and binding.status is BindingStatus.CANDIDATE
            and (item := next(
                (
                    item
                    for item in state.query_spec.semantic_items
                    if item.source_id == binding.source_id
                ),
                None,
            )) is not None
            and (document := dict(self._exact_formula_documents).get(binding.source_id))
            is not None
            and binding.document == document
            and (formula := _formula_part(item.normalized_meaning)) is not None
            and _formula_part(binding.expression.expression) == formula
            and isinstance(self._loaded_schema, LoadedSchema)
            and _exact_aggregate_operand_input_mismatch(
                formula,
                tuple(
                    column
                    for table in self._loaded_schema.schema.values()
                    if isinstance(table, Mapping)
                    for column in get_table_columns(table)
                ),
                tuple(column.column for column in binding.input_columns),
            )
        }
        authority_candidate_binding_ids: tuple[str, ...] = ()
        authority_affected_source_ids: tuple[str, ...] = ()
        has_current_invalid_stop_generation_authority = False
        try:
            context_payload = json.loads(context)
        except json.JSONDecodeError:
            context_payload = None
        relationship_continuation_priority = (
            isinstance(context_payload, dict)
            and isinstance(
                required_continuation := context_payload.get("required_continuation"),
                dict,
            )
            and required_continuation.get("kind")
            == "establish_required_relationship"
        )
        rejected_preflight_items = (
            context_payload.get("rejected_preflight_assessments")
            if isinstance(context_payload, dict)
            else None
        )
        missing_exact_predicates = _rejected_preflight_missing_exact_predicates(
            tuple(
                item
                for item in rejected_preflight_items
                if isinstance(item, dict)
            )
            if isinstance(rejected_preflight_items, list)
            else (),
            state,
            self._exact_formula_predicate_constraints,
        )
        if isinstance(context_payload, dict):
            authority = context_payload.get("invalid_stop_generation_authority")
            if (
                isinstance(authority, dict)
                and authority.get("reason_code")
                == CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE.value
                and isinstance(
                    affected_source_ids := authority.get("affected_source_ids"), list
                )
                and all(isinstance(source_id, str) for source_id in affected_source_ids)
            ):
                has_current_invalid_stop_generation_authority = True
                authority_affected_source_ids = tuple(sorted(affected_source_ids))
                authority_candidate_binding_ids = tuple(
                    sorted(
                        binding.binding_id
                        for binding in state.bindings
                        if binding.status is BindingStatus.CANDIDATE
                        and binding.source_id in affected_source_ids
                        and binding.binding_id
                        not in exact_formula_mismatching_candidate_ids
                    )
                )
        additional_assessment_binding_ids = (
            ()
            if relationship_continuation_priority
            else tuple(
                sorted(
                    set(exact_formula_candidate_binding_ids)
                    | set(authority_candidate_binding_ids)
                )
            )
        )
        if self._last_stop_review_hint is not None and _auto_binding_assessment_hint_is_closed(
            self._last_stop_review_hint,
            state,
        ):
            self._last_stop_review_hint = None
        if (
            self._last_stop_review_hint is not None
            and not has_current_invalid_stop_generation_authority
        ):
            try:
                context_payload = json.loads(context)
            except json.JSONDecodeError:
                context_payload = None
            if isinstance(context_payload, dict):
                context_payload["previous_stop_review_hint"] = (
                    self._last_stop_review_hint
                )
                context = json.dumps(
                    context_payload,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
        if reason is ResearchStopReason.STAGNATED and self._model_stagnation_signatures:
            reason_text += ":" + json.dumps(
                self._model_stagnation_signatures,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        request_digest = canonical_digest(
            {
                "research_context": context,
                "review_kind": "research_stop_review",
                "stop_reason": reason_text,
                "task": self._task,
            }
        )

        async def _call(_: object) -> ModelTokenUsage:
            nonlocal captured

            async def invoke(_context: object):
                return await SchemaResearchStopReviewAdapter().review_with_usage(
                    self._stop_review_model,
                    task=self._task,
                    research_context=context,
                    stop_reason=reason_text,
                )

            captured, usage = await execute_step_attempt(
                "schema research stop review",
                invoke,
                None,
                attempt_timeout=None,
                deadline=self._deadline,
            )
            return usage

        try:
            await execute_model_call_with_budget_async(
                state.run_id,
                state.run_incarnation,
                _research_stop_review_call_id(state, attempt),
                request_digest,
                self._model_identity,
                limits.input_tokens_per_call,
                limits.output_tokens_per_call,
                _call,
                config=self._policy,
                ledger=self._budget_ledger,
                claim_now_ns=self._model_claim_now_ns,
                owner_token_factory=self._model_owner_token_factory,
                wait=self._model_wait,
            )
        except asyncio.CancelledError:
            return None, attempt + 1
        except Exception as error:
            logger.warning(
                "typed_schema_research_stop_review retry=false "
                "code=PROVIDER_OR_ADAPTER error_class=%s",
                type(error).__name__,
            )
            return None, attempt + 1
        if captured is None:
            return None, attempt + 1
        if captured.decision == "stop_confirmed":
            prior_hint = self._last_stop_review_hint
            unresolved_source_ids = {
                item.source_id
                for item in state.query_spec.semantic_items
                if item.required and item.status is not SemanticItemStatus.RESOLVED
            }
            if prior_hint is not None and (
                additional_assessment_binding_ids
                or any(
                    match.group(0) in unresolved_source_ids
                    for match in _SOURCE_ID_IN_HINT.finditer(prior_hint)
                )
            ):
                return (
                    _validated_stop_review_hint(
                        prior_hint,
                        state,
                        additional_assessment_binding_ids,
                        self._loaded_schema,
                        missing_exact_predicates,
                    ),
                    attempt + 1,
                )
            if reason is ResearchStopReason.BUDGET_EXHAUSTED and any(
                item.required
                and item.status is SemanticItemStatus.PARTIALLY_RESOLVED
                for item in state.query_spec.semantic_items
            ):
                return (
                    _validated_stop_review_hint(
                        "Complete the existing partially resolved required semantic "
                        "items with one ordinary typed decision using only durable "
                        "evidence.",
                        state,
                        loaded_schema=self._loaded_schema,
                    ),
                        attempt + 1,
                    )
            pending_exact_formula_source_ids = (
                _runtime_exact_formula_continuation_source_ids(
                    state,
                    self._exact_formula_documents,
                )
            )
            if pending_exact_formula_source_ids:
                exact_formula_hint = _external_exact_formula_stop_review_hint(
                    state,
                    self._exact_formula_documents,
                    pending_exact_formula_source_ids,
                    self._loaded_schema,
                    exact_formula_predicate_constraints=(
                        self._exact_formula_predicate_constraints
                    ),
                )
                if exact_formula_hint is not None:
                    return (
                        _validated_stop_review_hint(
                            exact_formula_hint,
                            state,
                            additional_assessment_binding_ids,
                            self._loaded_schema,
                            missing_exact_predicates,
                        ),
                        attempt + 1,
                    )
            return None, attempt + 1
        mismatch_source_ids = _mismatching_external_exact_formula_candidate_source_ids(
            state,
            self._exact_formula_documents,
            self._loaded_schema,
        )
        exact_formula_override = None
        exact_physical_predicate_override = None
        exact_physical_predicate_source_ids: tuple[str, ...] = ()
        if captured.decision == "continue":
            pending_authority_formula_sources = tuple(
                sorted(
                    set(authority_affected_source_ids).intersection(
                        _runtime_exact_formula_continuation_source_ids(
                            state,
                            self._exact_formula_documents,
                        )
                    )
                )
            )
            if (
                missing_exact_predicates
                or self._pending_rejected_preflight_assessments
            ):
                exact_formula_override = _external_exact_formula_stop_review_hint(
                    state,
                    self._exact_formula_documents,
                    tuple(missing_exact_predicates) or None,
                    self._loaded_schema,
                    exact_formula_predicate_constraints=(
                        self._exact_formula_predicate_constraints
                    ),
                )
            elif pending_authority_formula_sources:
                exact_formula_override = _external_exact_formula_stop_review_hint(
                    state,
                    self._exact_formula_documents,
                    pending_authority_formula_sources,
                    self._loaded_schema,
                )
            elif mismatch_source_ids:
                exact_formula_override = _external_exact_formula_stop_review_hint(
                    state,
                    self._exact_formula_documents,
                    mismatch_source_ids,
                )
            else:
                exact_physical_predicate_override = (
                    _unresolved_exact_physical_predicate_stop_review_hint(state)
                )
                if exact_physical_predicate_override is not None:
                    exact_physical_predicate_source_ids = tuple(
                        sorted(
                            item.source_id
                            for item in state.query_spec.semantic_items
                            if item.required
                            and item.status is not SemanticItemStatus.RESOLVED
                            and item.kind
                            in (SemanticItemKind.FILTER, SemanticItemKind.TIME)
                            and item.exact_physical_predicate
                        )
                    )
        hint = _validated_stop_review_hint(
            exact_formula_override
            or exact_physical_predicate_override
            or captured.hint,
            state,
            additional_assessment_binding_ids,
            self._loaded_schema,
            missing_exact_predicates,
        )
        if exact_physical_predicate_source_ids:
            self._exact_physical_predicate_continuation_source_ids = (
                exact_physical_predicate_source_ids
            )
        self._last_stop_review_hint = hint
        return hint, attempt + 1

    def _stop_review_used_for_revision(self, state: ResearchState) -> bool:
        prefix = f"research-stop-review-{state.revision}-"
        return any(
            record.reservation.call_id.startswith(prefix)
            for record in self._budget_ledger.load_model_records(
                state.run_id, state.run_incarnation
            )
        )

    def _stop_review_used_for_non_novel_streak(self, state: ResearchState) -> bool:
        review_revisions: set[int] = set()
        for record in self._budget_ledger.load_model_records(
            state.run_id, state.run_incarnation
        ):
            revision = _stop_review_call_revision(record.reservation.call_id)
            if revision is not None:
                review_revisions.add(revision)
        if not review_revisions:
            return False
        for revision in range(state.revision - 1, -1, -1):
            try:
                observed = self._checkpoint_store.get_snapshot(
                    AdaptiveCheckpointKey(
                        state.run_id,
                        state.run_incarnation,
                        AdaptiveLoopKind.RESEARCH,
                        revision,
                    )
                ).observed
            except AdaptiveCheckpointError:
                return False
            if observed is None or not isinstance(observed.action, dict):
                return False
            if observed.action.get("novel") is True:
                return any(review_revision > revision for review_revision in review_revisions)
        return bool(review_revisions)

    def _stop_review_follow_up_decision_allowed(self, state: ResearchState) -> bool:
        if state.revision == 0:
            return False
        review_revision = state.revision - 1
        if not any(
            _stop_review_call_revision(record.reservation.call_id) == review_revision
            for record in self._budget_ledger.load_model_records(
                state.run_id, state.run_incarnation
            )
        ):
            return False
        try:
            observed = self._checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id,
                    state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    review_revision,
                )
            ).observed
        except AdaptiveCheckpointError:
            return False
        return (
            observed is not None
            and isinstance(observed.action, dict)
            and observed.action.get("novel") is False
        )


    def _failed_probe_feedback_from_replay(
        self, state: ResearchState
    ) -> SchemaResearchValidationFeedback | None:
        if state.revision == 0:
            return None
        replay_input = self._state_store.load_research_replay_input(
            state.run_id, state.run_incarnation, state.revision
        )
        if replay_input is None or not _replay_input_has_failed_probe(replay_input):
            return None
        return "PROBE_UNAVAILABLE"

    async def _wait_for_model_follower(self, seconds: float) -> None:
        reason = self._boundary_reason()
        if reason is ResearchStopReason.CANCELLED:
            raise _ModelWaitCancelled
        if reason is ResearchStopReason.DEADLINE_EXCEEDED:
            raise _ModelWaitDeadline
        delay = seconds
        if self._deadline is not None:
            delay = min(delay, self._deadline.remaining_seconds())
        await asyncio.sleep(delay)
        reason = self._boundary_reason()
        if reason is ResearchStopReason.CANCELLED:
            raise _ModelWaitCancelled
        if reason is ResearchStopReason.DEADLINE_EXCEEDED:
            raise _ModelWaitDeadline

    def _resolve(
        self,
        state: ResearchState,
        decision: ResearchDecisionV1,
        *,
        check_boundary: bool = True,
    ) -> tuple[ResolvedResearchDecision | None, ResearchStopReason | None]:
        if check_boundary:
            reason = self._boundary_reason()
            if reason is not None:
                return None, reason
        try:
            resolved = self._resolve_current_decision(state, decision)
        except DuplicateResearchActionError:
            return None, ResearchStopReason.STAGNATED
        except (
            DecisionResolverError,
            ValidationError,
            ValueError,
            TypeError,
        ) as error:
            logger.warning(
                "typed_schema_research_decision retry=false "
                "code=DECISION_RESOLUTION_INTERNAL error_class=%s",
                type(error).__name__,
            )
            return None, ResearchStopReason.PROTOCOL_FAILURE
        return resolved, self._boundary_reason() if check_boundary else None

    def _preflight_model_decision(
        self,
        state: ResearchState,
        decision: ResearchDecisionV1,
    ) -> tuple[
        SchemaResearchValidationFeedback | None,
        ResearchStopReason | None,
        dict[str, object] | None,
        tuple[dict[str, object], ...],
    ]:
        reason = self._boundary_reason()
        if reason is not None:
            return None, reason, None, ()
        registry_context = getattr(self._registry, "context", None)
        table_namespace = getattr(
            getattr(registry_context, "schema_runtime", None),
            "table_namespace",
            None,
        )
        if type(table_namespace) is not str:
            table_namespace = None
        aggregate_operand_mismatch = _has_exact_aggregate_operand_mismatch(
            state, decision, self._exact_formula_documents, self._loaded_schema
        )
        resolved: ResolvedResearchDecision | None = None
        if aggregate_operand_mismatch:
            try:
                resolved = self._resolve_current_decision(state, decision)
            except (
                ResearchTransitionConflictError,
                ResearchTransitionProtocolError,
                UnresolvableModelDecisionError,
                DuplicateResearchActionError,
                DecisionResolverError,
                ValidationError,
                ValueError,
                TypeError,
            ):
                resolved = None
            if resolved is None or not _has_same_batch_confirmed_differently_named_aggregate_input(
                state,
                decision,
                resolved,
                self._exact_formula_documents,
                self._loaded_schema,
            ):
                return (
                    "UNRESOLVABLE_PREFLIGHT",
                    None,
                    None,
                    _rejected_preflight_assessment_context(
                        state,
                        decision,
                        self._freshness_context,
                        self._preflight_requested_action(state, decision),
                        loaded_schema=self._loaded_schema,
                        exact_formula_documents=self._exact_formula_documents,
                        table_namespace=table_namespace,
                    ),
                )
        if _has_exact_formula_predicate_mismatch(
            state, decision, self._exact_formula_predicate_constraints
        ):
            return (
                "UNRESOLVABLE_PREFLIGHT",
                None,
                None,
                _rejected_preflight_assessment_context(
                    state,
                    decision,
                    self._freshness_context,
                    self._preflight_requested_action(state, decision),
                    loaded_schema=self._loaded_schema,
                    exact_formula_documents=self._exact_formula_documents,
                    table_namespace=table_namespace,
                ),
            )
        try:
            if resolved is None:
                resolved = self._resolve_current_decision(state, decision)
            if isinstance(decision.next, SemanticCommitRequest) or decision.proposals:
                prospective = commit_semantic_turn(resolved.admission).state
                missing_predicates = (
                    _missing_resolved_exact_formula_predicates(
                        prospective, self._exact_formula_predicate_constraints
                    )
                    if isinstance(decision.next, SemanticCommitRequest)
                    else {}
                )
                if missing_predicates:
                    return (
                        "UNRESOLVABLE_PREFLIGHT",
                        None,
                        None,
                        _rejected_preflight_assessment_context(
                            state,
                            decision,
                            self._freshness_context,
                            self._preflight_requested_action(state, decision),
                            loaded_schema=self._loaded_schema,
                            missing_exact_predicates=missing_predicates,
                            exact_formula_documents=self._exact_formula_documents,
                            table_namespace=table_namespace,
                        ),
                    )
        except (ResearchTransitionConflictError, ResearchTransitionProtocolError):
            return (
                "UNRESOLVABLE_PREFLIGHT",
                None,
                None,
                _rejected_preflight_assessment_context(
                    state,
                    decision,
                    self._freshness_context,
                    self._preflight_requested_action(state, decision),
                    loaded_schema=self._loaded_schema,
                    exact_formula_documents=self._exact_formula_documents,
                    table_namespace=table_namespace,
                ),
            )
        except UnresolvableModelDecisionError as error:
            partial_selected_candidate_commit_gaps = ()
            feedback_decision = decision
            if isinstance(error.__cause__, SemanticReducerError):
                partial_selected_candidate_commit_gaps = getattr(
                    error.__cause__, "partial_selected_candidate_commit_gaps", ()
                )
            if partial_selected_candidate_commit_gaps and isinstance(
                error.normalized_decision, ResearchDecisionV1
            ):
                feedback_decision = error.normalized_decision
            rejected = _rejected_preflight_assessment_context(
                state,
                feedback_decision,
                self._freshness_context,
                self._preflight_requested_action(state, decision),
                exact_column=error.exact_column,
                loaded_schema=self._loaded_schema,
                exact_formula_documents=self._exact_formula_documents,
                table_namespace=table_namespace,
            )
            if partial_selected_candidate_commit_gaps:
                rejected = _with_partial_selected_candidate_commit_feedback(
                    rejected,
                    partial_selected_candidate_commit_gaps,
                )
            return (
                "UNRESOLVABLE_PREFLIGHT",
                None,
                None,
                rejected,
            )
        except DuplicateResearchActionError as error:
            return "DUPLICATE_ACTION", None, _duplicate_action_context(error.action), ()
        except (
            DecisionResolverError,
            ValidationError,
            ValueError,
            TypeError,
        ) as error:
            cause = error.__cause__
            logger.warning(
                "typed_schema_research_preflight retry=false "
                "code=PRECHECK_INTERNAL error_class=%s cause_class=%s",
                type(error).__name__,
                type(cause).__name__ if cause is not None else "none",
            )
            return None, ResearchStopReason.PROTOCOL_FAILURE, None, ()
        return None, self._boundary_reason(), None, ()

    def _preflight_requested_action(
        self,
        state: ResearchState,
        decision: ResearchDecisionV1,
    ) -> ResearchAction | None:
        if not isinstance(decision.next, ToolIntent):
            return None
        baseline = decision.model_copy(update={"proposals": ()})
        try:
            return self._resolve_current_decision(state, baseline).admission.action
        except (
            DecisionResolverError,
            ValidationError,
            ValueError,
            TypeError,
        ):
            return None

    def _resolve_current_decision(
        self,
        state: ResearchState,
        decision: ResearchDecisionV1,
    ) -> ResolvedResearchDecision:
        decision = expand_model_identifier_handles(state, decision)
        decision = _normalize_model_source_ids(
            state,
            decision,
            freshness_context=self._freshness_context,
        )
        decision = _normalize_exact_document_formula_candidates(
            state,
            decision,
            self._exact_formula_documents,
        )
        admitted_state = _state_with_reconciled_model_budget(
            state,
            self._budget_ledger,
            self._policy,
        )
        resolved = resolve_research_decision(
            admitted_state,
            decision,
            loaded_schema=self._loaded_schema,
            freshness_context=self._freshness_context,
            registry=self._registry,
            deadline=self._deadline,
        )
        action = resolved.admission.action
        if action is not None and any(
            action.action_digest == prior.action_digest
            for prior in state.action_history
        ):
            raise DuplicateResearchActionError(action)
        return resolved

    def _resolve_planned(
        self,
        state: ResearchState,
        action: object,
        *,
        check_boundary: bool = True,
    ) -> tuple[ResolvedResearchDecision | None, ResearchStopReason | None]:
        try:
            envelope = _planned_envelope(action)
        except (TypeError, ValidationError, ValueError):
            return None, ResearchStopReason.PROTOCOL_FAILURE
        resolved, reason = self._resolve(
            state, envelope["decision"], check_boundary=check_boundary
        )
        if resolved is None or reason is not None:
            return resolved, reason
        if _stable_planned_identity(
            _planned_action(resolved)
        ) != _stable_planned_identity(envelope):
            return None, ResearchStopReason.PROTOCOL_FAILURE
        return resolved, None

    def _record_planned(
        self, state: ResearchState, resolved: ResolvedResearchDecision
    ) -> ResearchStopReason | None:
        reason = self._boundary_reason()
        if reason is not None:
            return reason
        try:
            key = self._action_checkpoint_key(state)
            self._checkpoint_store.record_planned(
                key,
                expected_revision=None if key.revision == 0 else key.revision - 1,
                action=_planned_action(resolved),
                semantic_repair_continuation=self._semantic_repair_continuation,
            )
        except (AdaptiveCheckpointError, TypeError, ValueError) as error:
            logger.warning(
                "typed_schema_research_decision retry=false "
                "code=CHECKPOINT_PLAN_WRITE error_class=%s",
                type(error).__name__,
            )
            return ResearchStopReason.PROTOCOL_FAILURE
        return self._boundary_reason()

    def _execute_or_recover(
        self, resolved: ResolvedResearchDecision, *, recover: bool
    ) -> tuple[ProbeResult | None, ResearchStopReason | None]:
        reason = self._boundary_reason()
        if reason is not None:
            return None, reason
        try:
            result = execute_resolved_research_decision(
                resolved, self._registry, recover=recover
            )
        except DecisionExecutionError:
            return None, ResearchStopReason.TOOL_FAILURE
        if not isinstance(result, ProbeResult):
            return None, ResearchStopReason.PROTOCOL_FAILURE
        return result, None

    def _admission_with_reconciled_budget(
        self,
        resolved: ResolvedResearchDecision,
    ) -> SemanticTurnAdmission:
        """Attach the durable probe charge before persisting the next state."""

        admission = resolved.admission
        action = admission.action
        if action is None:
            raise ValueError("a probe result requires one admitted action")
        records = self._budget_ledger.load_records(
            admission.state.run_id,
            admission.state.run_incarnation,
        )
        record = _reconciled_record_for_action(records, action)
        return replace(admission, budget_state=record.reconciliation.budget_after)

    def _state_with_reconciled_probe_budget(
        self,
        state: ResearchState,
        resolved: ResolvedResearchDecision,
    ) -> ResearchState:
        """Project the one durable failed probe charge without a semantic transition."""

        action = resolved.admission.action
        if action is None:
            raise ValueError("a failed probe requires one admitted action")
        records = self._budget_ledger.load_records(state.run_id, state.run_incarnation)
        record = _reconciled_record_for_action(records, action)
        return ResearchState.model_validate(
            {
                **state.model_dump(mode="python", round_trip=True, warnings="error"),
                "budget_state": record.reconciliation.budget_after,
            }
        )

    def _state_with_terminal_probe_budget(
        self,
        state: ResearchState,
        snapshot: object,
    ) -> ResearchState:
        """Restore a failed probe charge when replay starts at its terminal key."""

        observed = getattr(snapshot, "observed", None)
        if observed is None:
            return state
        probe_result = _probe_from_observed(observed.action)
        if probe_result is None or _probe_failure_reason(probe_result) is None:
            return state
        planned = getattr(snapshot, "planned", None)
        if planned is None:
            raise ValueError("failed observed probe has no planned action")
        resolved, reason = self._resolve_planned(
            state, planned.action, check_boundary=False
        )
        if resolved is None or reason is not None:
            raise ValueError("failed observed probe cannot be replayed")
        if not _probe_matches_resolution(probe_result, resolved):
            raise ValueError("failed observed probe identity is corrupt")
        return self._state_with_reconciled_probe_budget(state, resolved)

    def _record_observed(
        self,
        state: ResearchState,
        resolved: ResolvedResearchDecision,
        result: ProbeResult | None,
        novel: bool,
    ) -> ResearchStopReason | None:
        semantic_only = (
            resolved.admission.action is not None
            and resolved.admission.action.kind is ResearchActionKind.SEMANTIC_COMMIT
        )
        if semantic_only != (result is None):
            return ResearchStopReason.PROTOCOL_FAILURE
        if type(novel) is not bool:
            return ResearchStopReason.PROTOCOL_FAILURE
        action = {
            "contract_version": 1,
            "kind": "research_observed",
            "novel": novel,
            "result": None if result is None else result.model_dump(mode="json", by_alias=True),
            "resolution_digest": resolved.resolution_digest,
        }
        try:
            key = self._action_checkpoint_key(state)
            self._checkpoint_store.record_observed(
                key, expected_revision=key.revision, action=action
            )
        except (AdaptiveCheckpointError, TypeError, ValueError):
            return ResearchStopReason.PROTOCOL_FAILURE
        return None

    def _save_semantic_transition(
        self,
        previous: ResearchState,
        state: ResearchState,
        resolved: ResolvedResearchDecision,
        admission: SemanticTurnAdmission,
        probe_result: ProbeResult | None,
    ) -> ResearchStopReason | None:
        try:
            snapshot = None
            current_key = _checkpoint_key(previous)
            action_key = self._action_checkpoint_key(previous)
            for key in dict.fromkeys((current_key, action_key)):
                candidate = self._checkpoint_store.get_snapshot(key)
                if candidate.planned is None or candidate.observed is None:
                    continue
                planned = _planned_envelope(candidate.planned.action)
                observed = candidate.observed.action
                if (
                    planned["resolution_digest"] == resolved.resolution_digest
                    and isinstance(observed, dict)
                    and observed.get("resolution_digest")
                    == resolved.resolution_digest
                ):
                    snapshot = candidate
                    break
            if snapshot is None or admission.budget_state is None:
                return ResearchStopReason.PROTOCOL_FAILURE
            replay_input = ResearchSemanticReplayInput(
                decision=resolved.decision,
                semantic_batch=resolved.semantic_batch,
                freshness_context=self._freshness_context,
                tool_claim=resolved.tool_claim,
                budget_state=admission.budget_state,
                planned_action_digest=snapshot.planned.action_digest,
                observed_action_digest=snapshot.observed.action_digest,
                probe_result=probe_result,
            )
            self._state_store.save_replayable_semantic_transition(
                previous,
                state,
                replay_input,
            )
        except (
            AdaptiveCheckpointError,
            AdaptiveResearchStateStoreError,
            TypeError,
            ValueError,
        ):
            return ResearchStopReason.PROTOCOL_FAILURE
        self._latest_state = state
        return None

    def _stop(
        self,
        state: ResearchState,
        reason: ResearchStopReason,
        *,
        affected_source_ids: tuple[str, ...] | None = None,
        citation_evidence_ids: tuple[str, ...] | None = None,
        ambiguity: AmbiguityReport | None = None,
        freshness_context: FreshnessContext | None = None,
    ) -> ResearchLoopOutcome:
        terminal_freshness_context = (
            self._freshness_context
            if freshness_context is None
            else _revalidate_freshness(freshness_context)
        )
        try:
            state = _state_with_reconciled_model_budget(
                state, self._budget_ledger, self._policy
            )
        except (BudgetAdmissionError, TypeError, ValueError):
            return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
        affected = (
            _affected_source_ids(state)
            if affected_source_ids is None
            else affected_source_ids
        )
        citations = (
            tuple(sorted(item.evidence_id for item in state.evidence))
            if citation_evidence_ids is None
            else citation_evidence_ids
        )
        if (reason is ResearchStopReason.AMBIGUOUS) != (ambiguity is not None):
            reason = ResearchStopReason.PROTOCOL_FAILURE
            ambiguity = None
        try:
            key = self._terminal_checkpoint_key(state)
        except AdaptiveCheckpointError:
            return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
        terminal = {
            "affected_source_ids": list(affected),
            "ambiguity": (
                None if ambiguity is None else ambiguity.model_dump(mode="json")
            ),
            "citation_evidence_ids": list(citations),
            "contract_version": 2,
            "kind": "research_terminal",
            "rejection_signatures": [
                list(item)
                for item in (
                    self._model_stagnation_signatures
                    if reason is ResearchStopReason.STAGNATED
                    else ()
                )
            ],
            "reason": reason.value,
        }
        try:
            snapshot = self._checkpoint_store.get_snapshot(key)
            if snapshot.terminal is not None:
                stored_terminal = _terminal_envelope(
                    snapshot.terminal.action,
                    state,
                )
                continue_unbound_formula = (
                    self._semantic_repair_continuation
                    and stored_terminal["reason"]
                    == ResearchStopReason.COMPLETE.value
                    and _has_pending_required_formula_continuation(
                        state,
                        self._freshness_context,
                        self._exact_formula_documents,
                    )
                )
                if not continue_unbound_formula:
                    stored_freshness_context = (
                        self._terminal_replay_freshness_context(key)
                    )
                    if stored_freshness_context is None:
                        return self._outcome(
                            state,
                            ResearchStopReason.PROTOCOL_FAILURE,
                        )
                    return self._outcome_from_terminal(
                        state,
                        snapshot.terminal.action,
                        stored_freshness_context,
                    )
            if snapshot.planned is not None and snapshot.observed is None:
                planned = _planned_envelope(snapshot.planned.action)
                self._checkpoint_store.record_observed(
                    key,
                    expected_revision=key.revision,
                    action={
                        "action": planned["action"],
                        "contract_version": 1,
                        "kind": "research_aborted",
                        "reason": reason.value,
                        "resolution_digest": planned["resolution_digest"],
                    },
                )
                snapshot = self._checkpoint_store.get_snapshot(key)
            self._checkpoint_store.record_replayable_terminal(
                key,
                expected_revision=(
                    key.revision
                    if snapshot.planned is not None
                    else None
                    if key.revision == 0
                    else key.revision - 1
                ),
                action=terminal,
                replay_input=ResearchTerminalReplayInput(
                    freshness_context=terminal_freshness_context,
                ),
                semantic_repair_continuation=self._semantic_repair_continuation,
            )
        except (
            AdaptiveCheckpointCasError,
            AdaptiveCheckpointError,
            TypeError,
            ValueError,
        ):
            return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
        return ResearchLoopOutcome(
            final_state=state,
            stop_reason=reason,
            affected_source_ids=tuple(affected),
            citation_evidence_ids=tuple(citations),
            ambiguity=ambiguity,
            rejection_signatures=(
                self._model_stagnation_signatures
                if reason is ResearchStopReason.STAGNATED
                else ()
            ),
            freshness_context=terminal_freshness_context,
        )

    def _action_checkpoint_key(self, state: ResearchState) -> AdaptiveCheckpointKey:
        key = _checkpoint_key(state)
        if not self._semantic_repair_continuation:
            return key
        snapshot = self._checkpoint_store.get_snapshot(key)
        if snapshot.terminal is not None:
            terminal = _terminal_envelope(snapshot.terminal.action, state)
            if not (
                terminal["reason"] == ResearchStopReason.COMPLETE.value
                and _has_pending_required_formula_continuation(
                    state,
                    self._freshness_context,
                    self._exact_formula_documents,
                )
            ):
                return key
            return replace(key, revision=key.revision + 1)
        if snapshot.planned is not None and snapshot.observed is not None:
            return replace(key, revision=key.revision + 1)
        return key

    def _terminal_checkpoint_key(
        self, state: ResearchState
    ) -> AdaptiveCheckpointKey:
        key = _checkpoint_key(state)
        if not self._semantic_repair_continuation:
            return key
        snapshot = self._checkpoint_store.get_snapshot(key)
        if snapshot.terminal is None:
            return key
        terminal = _terminal_envelope(snapshot.terminal.action, state)
        if terminal["reason"] != ResearchStopReason.COMPLETE.value or not (
            _has_pending_required_formula_continuation(
                state,
                self._freshness_context,
                self._exact_formula_documents,
            )
        ):
            return key
        return replace(key, revision=key.revision + 1)

    def _terminal_replay_freshness_context(
        self,
        key: AdaptiveCheckpointKey,
    ) -> FreshnessContext | None:
        stored = self._checkpoint_store.load_terminal_replay_input(key)
        if type(stored) is not ResearchTerminalReplayInput:
            return None
        return _revalidate_freshness(stored.freshness_context)

    def _boundary_reason(self) -> ResearchStopReason | None:
        try:
            if self._is_cancelled():
                return ResearchStopReason.CANCELLED
            if self._deadline is not None:
                self._deadline.require_remaining("schema research loop")
        except WorkflowDeadlineExceeded:
            return ResearchStopReason.DEADLINE_EXCEEDED
        except Exception:
            return ResearchStopReason.PROTOCOL_FAILURE
        return None

    @staticmethod
    def _outcome(
        state: ResearchState, reason: ResearchStopReason
    ) -> ResearchLoopOutcome:
        return ResearchLoopOutcome(
            final_state=state,
            stop_reason=reason,
            affected_source_ids=_affected_source_ids(state),
            citation_evidence_ids=tuple(
                sorted(item.evidence_id for item in state.evidence)
            ),
            ambiguity=None,
            rejection_signatures=(),
        )

    def _outcome_from_terminal(
        self,
        state: ResearchState,
        action: object,
        freshness_context: FreshnessContext,
    ) -> ResearchLoopOutcome:
        try:
            terminal = _terminal_envelope(action, state)
            reason = ResearchStopReason(terminal["reason"])
        except (TypeError, ValueError):
            return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
        if not _terminal_replay_is_authorized(
            state, freshness_context, reason, terminal
        ):
            return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
        return ResearchLoopOutcome(
            final_state=state,
            stop_reason=reason,
            affected_source_ids=tuple(terminal["affected_source_ids"]),
            citation_evidence_ids=tuple(terminal["citation_evidence_ids"]),
            ambiguity=terminal["ambiguity"],
            rejection_signatures=tuple(
                tuple(item) for item in terminal["rejection_signatures"]
            ),
            freshness_context=freshness_context,
        )


async def run_research_loop(
    *,
    initial_state: ResearchState,
    task: str,
    research_context: Callable[..., str],
    model: SchemaResearchDecisionModel,
    model_identity: str,
    adapter: SchemaResearchDecisionAdapter,
    loaded_schema: LoadedSchema,
    freshness_context: FreshnessContext,
    registry: AdaptiveResearchToolRegistry,
    state_store: AdaptiveResearchStateStore,
    checkpoint_store: AdaptiveStateStore,
    budget_ledger: AdaptiveBudgetLedger,
    policy: AdaptivePolicyConfig,
    deadline: DeadlineBudget | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    model_claim_now_ns: Callable[[], int] = time.time_ns,
    model_owner_token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    model_wait: Callable[[float], Awaitable[None]] | None = None,
    semantic_repair_continuation: bool = False,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...] = (),
    exact_formula_predicate_constraints: tuple[
        tuple[str, DocumentRef, str, str], ...
    ] = (),
    stop_review_model: SchemaResearchDecisionModel | None = None,
) -> ResearchLoopOutcome:
    """Run private schema research coordination without taking SQL authority."""

    coordinator = _ResearchLoopCoordinator(
        initial_state=initial_state,
        task=task,
        research_context=research_context,
        model=model,
        model_identity=model_identity,
        adapter=adapter,
        loaded_schema=loaded_schema,
        freshness_context=freshness_context,
        registry=registry,
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=budget_ledger,
        policy=policy,
        deadline=deadline,
        is_cancelled=is_cancelled or (lambda: False),
        model_claim_now_ns=model_claim_now_ns,
        model_owner_token_factory=model_owner_token_factory,
        model_wait=model_wait,
        semantic_repair_continuation=semantic_repair_continuation,
        exact_formula_documents=exact_formula_documents,
        exact_formula_predicate_constraints=exact_formula_predicate_constraints,
        stop_review_model=stop_review_model,
    )
    try:
        return await coordinator.run()
    except asyncio.CancelledError:
        return coordinator._stop(  # noqa: SLF001 - closed cancellation result
            coordinator._latest_state, ResearchStopReason.CANCELLED
        )


def _has_pending_required_formula_continuation(
    state: ResearchState,
    freshness_context: FreshnessContext,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...] = (),
) -> bool:
    return bool(
        _pending_required_formula_continuation_source_ids(
            state, freshness_context, exact_formula_documents
        )
    )


def _pending_required_formula_continuation_source_ids(
    state: ResearchState,
    freshness_context: FreshnessContext,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...] = (),
) -> tuple[str, ...]:
    bindings_by_id = {binding.binding_id: binding for binding in state.bindings}
    source_ids = {
        item.source_id
        for item in state.query_spec.semantic_items
        if item.required
        and item.kind is SemanticItemKind.FORMULA
        and (
            not item.binding_ids
            or any(
                bindings_by_id.get(binding_id, None) is not None
                and bindings_by_id[binding_id].status is BindingStatus.CANDIDATE
                for binding_id in item.binding_ids
            )
        )
    }
    source_ids.update(
        _exact_document_formula_continuation_source_ids(state, freshness_context)
    )
    source_ids.update(
        _runtime_exact_formula_continuation_source_ids(
            state, exact_formula_documents
        )
    )
    return tuple(sorted(source_ids))


def _runtime_exact_formula_continuation_source_ids(
    state: ResearchState,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...],
) -> tuple[str, ...]:
    """Return supplied exact formulas lacking their selected derived binding."""

    documents_by_source = dict(exact_formula_documents)
    bindings_by_id = {binding.binding_id: binding for binding in state.bindings}
    source_ids: list[str] = []
    for item in state.query_spec.semantic_items:
        if not item.required or item.kind is not SemanticItemKind.FORMULA:
            continue
        document = documents_by_source.get(item.source_id)
        formula = _formula_part(item.normalized_meaning)
        if document is None or formula is None:
            continue
        selected = _selected_exact_formula_binding_matches_document(
            item, bindings_by_id, document
        )
        if selected is not None:
            if selected:
                continue
            source_ids.append(item.source_id)
            continue
        if any(
            isinstance(binding := bindings_by_id.get(binding_id), DerivedExpressionBinding)
            and binding.status is BindingStatus.SUPPORTED
            and binding.validator_rule == "semantic-certificate:v1:derived_expression"
            and binding.document == document
            and _formula_part(binding.expression.expression) == formula
            for binding_id in item.binding_ids
        ):
            continue
        source_ids.append(item.source_id)
    return tuple(sorted(source_ids))


def _runtime_exact_formula_candidate_binding_ids(
    state: ResearchState,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...],
    loaded_schema: LoadedSchema | None = None,
) -> tuple[str, ...]:
    """Return durable candidates for pending supplied exact formulas only."""

    pending_source_ids = set(
        _runtime_exact_formula_continuation_source_ids(
            state,
            exact_formula_documents,
        )
    )
    if not pending_source_ids:
        return ()
    documents_by_source = dict(exact_formula_documents)
    formulas_by_source = {
        item.source_id: _formula_part(item.normalized_meaning)
        for item in state.query_spec.semantic_items
        if item.source_id in pending_source_ids
    }
    schema_names = (
        tuple(
            column
            for table in loaded_schema.schema.values()
            if isinstance(table, Mapping)
            for column in get_table_columns(table)
        )
        if isinstance(loaded_schema, LoadedSchema)
        else ()
    )
    return tuple(
        sorted(
            binding.binding_id
            for binding in state.bindings
            if isinstance(binding, DerivedExpressionBinding)
            and binding.status is BindingStatus.CANDIDATE
            and binding.source_id in pending_source_ids
            and binding.document == documents_by_source.get(binding.source_id)
            and _formula_part(binding.expression.expression)
            == formulas_by_source.get(binding.source_id)
            and not _exact_aggregate_operand_input_mismatch(
                formulas_by_source.get(binding.source_id) or "",
                schema_names,
                tuple(column.column for column in binding.input_columns),
            )
        )
    )


def _mismatching_external_exact_formula_candidate_source_ids(
    state: ResearchState,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...],
    loaded_schema: LoadedSchema | None,
) -> tuple[str, ...]:
    """Return exact-formula sources whose durable candidate misses a known operand."""

    if not isinstance(loaded_schema, LoadedSchema):
        return ()
    documents_by_source = dict(exact_formula_documents)
    items_by_source = {item.source_id: item for item in state.query_spec.semantic_items}
    schema_names = tuple(
        column
        for table in loaded_schema.schema.values()
        if isinstance(table, Mapping)
        for column in get_table_columns(table)
    )
    return tuple(
        sorted(
            {
                binding.source_id
                for binding in state.bindings
                if isinstance(binding, DerivedExpressionBinding)
                and binding.status is BindingStatus.CANDIDATE
                and (item := items_by_source.get(binding.source_id)) is not None
                and item.required
                and item.kind is SemanticItemKind.FORMULA
                and (document := documents_by_source.get(binding.source_id)) is not None
                and binding.document == document
                and (formula := _formula_part(item.normalized_meaning)) is not None
                and _formula_part(binding.expression.expression) == formula
                and _exact_aggregate_operand_input_mismatch(
                    formula,
                    schema_names,
                    tuple(column.column for column in binding.input_columns),
                )
            }
        )
    )


def _rejected_external_exact_formula_missing_operands(
    rejected_assessments: tuple[dict[str, object], ...],
    state: ResearchState,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...],
    loaded_schema: LoadedSchema | None,
    exact_formula_predicate_constraints: tuple[
        tuple[str, DocumentRef, str, str], ...
    ] = (),
) -> dict[str, tuple[str, ...]]:
    """Return exact aggregate or predicate names missing from rejected proposals."""

    if not isinstance(loaded_schema, LoadedSchema):
        return {}
    documents_by_source = dict(exact_formula_documents)
    items_by_source = {item.source_id: item for item in state.query_spec.semantic_items}
    schema_names = tuple(
        column
        for table in loaded_schema.schema.values()
        if isinstance(table, Mapping)
        for column in get_table_columns(table)
    )
    missing_by_source: dict[str, set[str]] = {}
    predicate_constraints: dict[str, list[tuple[str, str]]] = {}
    for source_id, _document, column, literal in exact_formula_predicate_constraints:
        predicate_constraints.setdefault(source_id, []).append((column, literal))
    bindings = {binding.binding_id: binding for binding in state.bindings}
    for rejected in rejected_assessments:
        missing_source_id = rejected.get("source_id")
        missing_predicates = rejected.get("missing_exact_predicate_columns")
        if isinstance(missing_source_id, str) and isinstance(
            missing_predicates, list
        ):
            missing_by_source.setdefault(missing_source_id, set()).update(
                column for column in missing_predicates if isinstance(column, str)
            )
        serialized = rejected.get("proposal")
        if not isinstance(serialized, dict):
            continue
        try:
            proposal = NewBindingProposal.model_validate_json(json.dumps(serialized))
        except (TypeError, ValidationError, ValueError):
            proposal = None
        if proposal is not None and isinstance(proposal.candidate, DiscriminatorValueCandidate):
            constraint = [
                column
                for column, literal in predicate_constraints.get(
                    proposal.source_id, ()
                )
                if literal == proposal.candidate.discriminator_predicate.right
            ]
            if (
                constraint
                and proposal.candidate.discriminator_predicate.operator
                is PredicateOperator.EQ
                and all(
                    proposal.candidate.discriminator_column.column.casefold()
                    != column.casefold()
                    for column in constraint
                )
            ):
                missing_by_source.setdefault(proposal.source_id, set()).update(constraint)
            continue
        if proposal is None:
            try:
                assessment = BindingAssessment.model_validate_json(json.dumps(serialized))
            except (TypeError, ValidationError, ValueError):
                continue
            binding = (
                bindings.get(assessment.subject.binding_id)
                if assessment.certificate == "consistent"
                and isinstance(assessment.subject, ExistingBindingRef)
                else None
            )
            if not isinstance(binding, DiscriminatorValueBinding):
                continue
            constraint = [
                column
                for column, literal in predicate_constraints.get(binding.source_id, ())
                if literal == binding.discriminator_predicate.right
            ]
            if (
                binding.status is BindingStatus.CANDIDATE
                and constraint
                and binding.discriminator_predicate.operator is PredicateOperator.EQ
                and all(
                    binding.discriminator_column.column.casefold()
                    != column.casefold()
                    for column in constraint
                )
            ):
                missing_by_source.setdefault(binding.source_id, set()).update(constraint)
            continue
        if not isinstance(proposal.candidate, DerivedExpressionCandidate):
            continue
        item = items_by_source.get(proposal.source_id)
        document = documents_by_source.get(proposal.source_id)
        formula = (
            _formula_part(item.normalized_meaning)
            if item is not None
            else None
        )
        if (
            item is None
            or not item.required
            or item.kind is not SemanticItemKind.FORMULA
            or document is None
            or proposal.candidate.document_id != document.document_id
            or formula is None
            or _formula_part(proposal.candidate.expression_claim) != formula
        ):
            continue
        missing = _missing_exact_aggregate_operand_names(
            formula,
            schema_names,
            tuple(column.column for column in proposal.candidate.input_columns),
        )
        if missing:
            missing_by_source.setdefault(proposal.source_id, set()).update(missing)
    return {
        source_id: tuple(sorted(missing))
        for source_id, missing in sorted(missing_by_source.items())
    }


def _pending_exact_formula_preflight_is_affected(
    state: ResearchState,
    freshness_context: FreshnessContext,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...],
) -> bool:
    """Keep corrective feedback only for a still-affected supplied exact formula."""

    source_ids = set(
        _runtime_exact_formula_continuation_source_ids(
            state, exact_formula_documents
        )
    )
    if not source_ids:
        return False
    authority = evaluate_research_generation_authority(
        state, freshness_context, state.run_id, state.run_incarnation
    )
    return bool(source_ids.intersection(authority.affected_source_ids))


def _filter_pending_rejected_preflight_assessments(
    pending: tuple[dict[str, object], ...],
    state: ResearchState,
    source_ids: set[str],
) -> tuple[dict[str, object], ...]:
    bindings = {binding.binding_id: binding.source_id for binding in state.bindings}
    retained: list[dict[str, object]] = []
    for item in pending:
        proposal = item.get("proposal")
        if not isinstance(proposal, dict):
            continue
        try:
            if proposal.get("proposal_type") == "new_binding":
                source_id = NewBindingProposal.model_validate_json(
                    json.dumps(proposal)
                ).source_id
            elif proposal.get("proposal_type") == "binding_assessment":
                assessment = BindingAssessment.model_validate_json(json.dumps(proposal))
                source_id = (
                    bindings.get(assessment.subject.binding_id)
                    if isinstance(assessment.subject, ExistingBindingRef)
                    else None
                )
            else:
                source_id = None
        except (TypeError, ValidationError, ValueError):
            source_id = None
        if source_id in source_ids:
            retained.append(item)
    return tuple(retained)


def _unresolved_exact_physical_predicate_stop_review_hint(
    state: ResearchState,
    source_ids: tuple[str, ...] | None = None,
) -> str | None:
    """Keep ordinary research aligned with required exact predicates."""

    predicates = tuple(
        item
        for item in state.query_spec.semantic_items
        if item.required
        and item.status is not SemanticItemStatus.RESOLVED
        and item.kind in (SemanticItemKind.FILTER, SemanticItemKind.TIME)
        and item.exact_physical_predicate
        and (source_ids is None or item.source_id in source_ids)
    )
    if not predicates:
        return None
    requirements = "; ".join(
        "source_id "
        f"{item.source_id} with exact QuerySpec normalized_meaning "
        f"{json.dumps(item.normalized_meaning, ensure_ascii=False)}"
        for item in sorted(predicates, key=lambda item: item.source_id)
    )
    return (
        "Continue ordinary research only for required unresolved exact physical "
        f"predicate {requirements}. Preserve QuerySpec meaning; do not replace it "
        "with a different predicate. When the profile permits an evidence-backed "
        "stored spelling for an empty trusted categorical string search, retain the "
        "same physical column and operator and obtain its exact search_value "
        "certificate. An unbound semantic phrase does not select a physical "
        "table or column; physical candidates must come from schema or evidence. "
        "If durable evidence already confirms the physical column from schema or "
        "evidence, operator, literal, and any required relationship path, create "
        "the corresponding typed binding citing only that evidence, then "
        "semantic_commit; do not repeat a probe. Otherwise acquire only the "
        "missing evidence."
    )


def _repeated_exact_physical_predicate_search_value_hint(
    state: ResearchState,
    source_ids: tuple[str, ...],
) -> str | None:
    """Warn only a guarded exact-predicate continuation about repeated value search."""

    guarded_source_ids = {
        item.source_id
        for item in state.query_spec.semantic_items
        if item.source_id in source_ids
        and item.required
        and item.status is not SemanticItemStatus.RESOLVED
        and item.kind in (SemanticItemKind.FILTER, SemanticItemKind.TIME)
        and item.exact_physical_predicate
    }
    if not guarded_source_ids:
        return None
    searches: dict[tuple[str, str | None, str, str, str], int] = {}
    for action in state.action_history:
        if (
            action.kind is not ResearchActionKind.SEARCH_VALUE
            or not isinstance(action.target, ColumnRef)
        ):
            continue
        value = next(
            (value for key, value in action.parameters if key == "value"), None
        )
        if type(value) is not str:
            continue
        column = action.target
        key = (
            column.table.namespace,
            column.table.schema_name,
            column.table.table,
            column.column,
            value.casefold(),
        )
        searches[key] = searches.get(key, 0) + 1
    if not any(count >= 2 for count in searches.values()):
        return None
    return (
        "Repeated search_value on the same physical column with a case-only value "
        "did not resolve the guarded exact predicate. Do not repeat it only with "
        "another qualification or value case; without a new fact, choose a different "
        "physical column or hypothesis."
    )


def _external_exact_formula_stop_review_hint(
    state: ResearchState,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...],
    source_ids: tuple[str, ...] | None = None,
    loaded_schema: LoadedSchema | None = None,
    advisory_missing_operands: Mapping[str, tuple[str, ...]] | None = None,
    exact_formula_predicate_constraints: tuple[
        tuple[str, DocumentRef, str, str], ...
    ] = (),
) -> str | None:
    """Build ordinary-research guidance for exact formulas or predicates."""

    pending_source_ids = (
        set(source_ids)
        if source_ids is not None
        else set(
            _runtime_exact_formula_continuation_source_ids(
                state,
                exact_formula_documents,
            )
        )
    )
    formulas = tuple(
        item.normalized_meaning.strip()
        for item in state.query_spec.semantic_items
        if item.source_id in pending_source_ids
        and item.source_id in dict(exact_formula_documents)
        and _formula_part(item.normalized_meaning) is not None
    )
    missing_by_source = {
        source_id: set(advisory_missing_operands.get(source_id, ()))
        for source_id in pending_source_ids
        if advisory_missing_operands is not None
    }
    predicate_names_by_source: dict[str, set[str]] = {}
    for source_id, _document, column, _literal in exact_formula_predicate_constraints:
        if column in missing_by_source.get(source_id, set()):
            predicate_names_by_source.setdefault(source_id, set()).add(column)
    missing_predicates = {
        column for columns in predicate_names_by_source.values() for column in columns
    }
    missing_operands_by_source = {
        source_id: columns - predicate_names_by_source.get(source_id, set())
        for source_id, columns in missing_by_source.items()
    }
    if not formulas:
        predicates = tuple(
            (column, literal)
            for source_id, _document, column, literal in exact_formula_predicate_constraints
            if source_id in pending_source_ids
        )
        if not predicates:
            return None
        names = ", ".join(sorted({column for column, _literal in predicates}))
        return (
            "Trusted document predicate requires exact column " + names
            + ". Missing exact predicate column: " + names
            + ". Verify ordinary schema evidence only; do not semantic_commit or generate SQL."
        )
    if isinstance(loaded_schema, LoadedSchema):
        documents_by_source = dict(exact_formula_documents)
        items_by_source = {item.source_id: item for item in state.query_spec.semantic_items}
        schema_names = tuple(
            column
            for table in loaded_schema.schema.values()
            if isinstance(table, Mapping)
            for column in get_table_columns(table)
        )
        for binding in state.bindings:
            item = items_by_source.get(binding.source_id)
            if (
                isinstance(binding, DerivedExpressionBinding)
                and binding.status is BindingStatus.CANDIDATE
                and binding.source_id in pending_source_ids
                and item is not None
                and binding.document == documents_by_source.get(binding.source_id)
                and (formula := _formula_part(item.normalized_meaning)) is not None
                and _formula_part(binding.expression.expression) == formula
            ):
                missing_operands_by_source.setdefault(binding.source_id, set()).update(
                    _missing_exact_aggregate_operand_names(
                        formula,
                        schema_names,
                        tuple(column.column for column in binding.input_columns),
                    )
                )
    missing_operands = {
        column
        for source_id, columns in missing_operands_by_source.items()
        for column in columns - predicate_names_by_source.get(source_id, set())
    }
    quoted_formulas = "; ".join(f'"{formula}"' for formula in formulas)
    missing_operand_text = (
        " Missing same-name aggregate operand: "
        + ", ".join(sorted(missing_operands))
        + "."
        if missing_operands
        else ""
    )
    missing_predicate_text = (
        " Missing exact predicate column: "
        + ", ".join(sorted(missing_predicates))
        + ". Use ordinary schema research to locate its loaded same-name column "
        "and declared relationship path before value search or binding; do not "
        "substitute a proxy or aggregate input."
        if missing_predicates
        else ""
    )
    return (
        f"Trusted exact formula: {quoted_formulas}.{missing_operand_text}"
        f"{missing_predicate_text} "
        "Direct ordinary research to "
        "verify and map every explicitly named aggregate operand and predicate "
        "column with durable schema and relationship evidence. Do not substitute a "
        "proxy or ID-lookalike, do not semantic_commit the formula before that proof, "
        "and do not generate SQL. When all required inputs and predicates already "
        "have durable evidence, create the document-backed derived_expression "
        "binding for the exact formula and semantic_commit it instead of repeating "
        "the probes."
    )


def _exact_document_formula_continuation_source_ids(
    state: ResearchState,
    freshness_context: FreshnessContext,
) -> tuple[str, ...]:
    """Return required exact document formulas lacking their selected derived binding."""

    bindings_by_id = {binding.binding_id: binding for binding in state.bindings}
    source_ids: list[str] = []
    for item in state.query_spec.semantic_items:
        if not item.required or item.kind is not SemanticItemKind.FORMULA:
            continue
        formula = _formula_part(item.normalized_meaning)
        if formula is None:
            continue
        documents = {
            (evidence.target.document_id, evidence.target.namespace)
            for evidence in state.evidence
            if evidence.source_kind is EvidenceSourceKind.DOCUMENT
            and isinstance(evidence.target, DocumentRef)
            and evaluate_evidence_freshness(evidence, freshness_context).status
            is FreshnessStatus.FRESH
            and (observation := parse_probe_observation(evidence.observation)) is not None
            and isinstance(observation.payload, dict)
            and isinstance(content := observation.payload.get("content"), str)
            and formula in _normalize_formula_whitespace(content)
        }
        if len(documents) != 1:
            continue
        document_id, namespace = next(iter(documents))
        selected = _selected_exact_formula_binding_matches_document(
            item,
            bindings_by_id,
            DocumentRef(document_id=document_id, namespace=namespace),
        )
        if selected is not None:
            if selected:
                continue
            source_ids.append(item.source_id)
            continue
        if any(
            isinstance(binding := bindings_by_id.get(binding_id), DerivedExpressionBinding)
            and binding.status is BindingStatus.SUPPORTED
            and binding.validator_rule == "semantic-certificate:v1:derived_expression"
            and binding.document.document_id == document_id
            and binding.document.namespace == namespace
            and _formula_part(binding.expression.expression) == formula
            for binding_id in item.binding_ids
        ):
            continue
        source_ids.append(item.source_id)
    return tuple(sorted(source_ids))


def _selected_exact_formula_binding_matches_document(
    item: SemanticItem,
    bindings_by_id: Mapping[str, object],
    document: DocumentRef,
) -> bool | None:
    """Return whether a selected exact formula binding matches its trusted document."""

    if item.exact_formula_binding_id is None:
        return None
    binding = bindings_by_id.get(item.exact_formula_binding_id)
    formula = _formula_part(item.normalized_meaning)
    return (
        isinstance(binding, DerivedExpressionBinding)
        and binding.status is BindingStatus.SUPPORTED
        and binding.source_id == item.source_id
        and binding.validator_rule == "semantic-certificate:v1:derived_expression"
        and binding.document == document
        and formula is not None
        and _formula_part(binding.expression.expression) == formula
    )


def _formula_part(value: str | None) -> str | None:
    """Return the whitespace-normalized exact formula before explanatory text."""

    if value is None:
        return None
    depth = 0
    quote: str | None = None
    index = 0
    formula_end = len(value)
    equals_index: int | None = None
    completed_top_level_expression = False
    while index < formula_end:
        character = value[index]
        if quote is not None:
            if character == quote:
                if index + 1 < formula_end and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character in "'\"":
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                completed_top_level_expression = True
        elif depth == 0 and character == ";":
            formula_end = index
            break
        elif (
            depth == 0
            and character == "="
            and (index == 0 or value[index - 1] not in "<>!=")
            and (index + 1 == formula_end or value[index + 1] != "=")
            and equals_index is None
            and not completed_top_level_expression
        ):
            equals_index = index
        index += 1
    formula = value[:formula_end].strip()
    if re.match(r"^\s*(?:ADD|SUBTRACT|MULTIPLY|DIVIDE)\s*\(", formula, re.IGNORECASE):
        depth = 0
        quote = None
        index = 0
        while index < len(formula):
            character = formula[index]
            if quote is not None:
                if character == quote:
                    if index + 1 < len(formula) and formula[index + 1] == quote:
                        index += 2
                        continue
                    quote = None
            elif character in "'\"":
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and re.match(
                    r"^\s+FROM\s+[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}\s*$",
                    formula[index + 1 :],
                    re.IGNORECASE,
                ):
                    formula = formula[: index + 1].strip()
                    break
            index += 1
    if equals_index is not None:
        left = value[:equals_index].strip()
        right = value[equals_index + 1 : formula_end].strip()
        try:
            expressions = parse(left)
        except (ParseError, TokenError, ValueError):
            expressions = ()
        if right and (len(expressions) != 1 or isinstance(expressions[0], exp.Alias)):
            formula = right
    try:
        parse(formula)
    except (ParseError, TokenError, ValueError):
        depth = 0
        quote = None
        index = 0
        longest_candidate: str | None = None
        while index < len(formula):
            character = formula[index]
            if quote is not None:
                if character == quote:
                    if index + 1 < len(formula) and formula[index + 1] == quote:
                        index += 2
                        continue
                    quote = None
                index += 1
                continue
            if character in "'\"":
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    candidate = formula[: index + 1]
                    try:
                        expressions = parse(candidate)
                    except (ParseError, TokenError, ValueError):
                        pass
                    else:
                        if len(expressions) == 1 and not isinstance(
                            expressions[0], exp.Alias
                        ):
                            longest_candidate = candidate
            index += 1
        if longest_candidate is not None:
            suffix = formula[len(longest_candidate) :].lstrip()
            if suffix and suffix[0].isalpha() and not re.match(
                r"FROM\b", suffix, re.IGNORECASE
            ):
                formula = longest_candidate
    formula = _normalize_formula_whitespace(formula)
    return formula or None


def _normalize_formula_whitespace(value: str) -> str:
    """Remove formula whitespace without changing quoted literal values."""

    normalized: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            normalized.append(character)
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    normalized.append(value[index + 1])
                    index += 2
                    continue
                quote = None
        elif character in "'\"":
            quote = character
            normalized.append(character)
        elif not character.isspace():
            normalized.append(character)
        index += 1
    return "".join(normalized)


def _has_unbound_latest_probe_evidence(state: ResearchState) -> bool:
    if not any(
        item.required and item.kind is SemanticItemKind.FORMULA
        for item in state.query_spec.semantic_items
    ) or not state.action_history:
        return False
    latest_action = state.action_history[-1]
    if latest_action.kind is ResearchActionKind.SEMANTIC_COMMIT:
        return False
    bound_evidence_ids = {
        evidence_id
        for binding in state.bindings
        for evidence_id in binding.evidence_ids
    } | {
        evidence_id
        for join in state.join_candidates
        for evidence_id in join.evidence_ids
    }
    return any(
        evidence.action_digest == latest_action.action_digest
        and evidence.evidence_id not in bound_evidence_ids
        for evidence in state.evidence
    )


def _state_with_reconciled_model_budget(
    state: ResearchState,
    ledger: AdaptiveBudgetLedger,
    policy: AdaptivePolicyConfig,
) -> ResearchState:
    """Project completed model charges into the state used for one tool turn."""

    records = ledger.load_model_records(state.run_id, state.run_incarnation)
    persisted = state.budget_state
    try:
        validate_state_model_budget_policy(persisted, config=policy)
    except BudgetAdmissionError as exc:
        raise ValueError("research budget state does not match the policy") from exc
    if not records:
        if persisted.used_model_calls != 0 or persisted.used_model_tokens != 0:
            raise ValueError("empty model ledger cannot have model usage")
        return state
    attempts_by_revision: dict[int, list[int]] = {}
    last_revision = -1
    for record in records:
        call_id = record.reservation.call_id
        matched = _MODEL_CALL_ID.match(call_id)
        if matched is None:
            if _SOLVER_MODEL_CALL_ID.match(call_id) is not None:
                # Solver proposal calls do not belong to this ResearchState's
                # revision numbering; skip them for the attempt-contiguity
                # check below. completed_model_budget_chain() below still
                # walks every record, solver included, since they all share
                # one cost chain against the same policy budget.
                continue
            raise ValueError("model ledger call ID is not a research-loop attempt")
        revision = int(matched["revision"])
        attempt = int(matched["attempt"])
        if revision > state.revision or revision < last_revision:
            raise ValueError("model ledger revision is ahead of the tool turn")
        last_revision = revision
        attempts_by_revision.setdefault(revision, []).append(attempt)
    if any(
        sorted(attempts) != list(range(len(attempts)))
        for attempts in attempts_by_revision.values()
    ) or any(
        revision not in attempts_by_revision for revision in range(state.revision)
    ):
        raise ValueError("model ledger attempts are not contiguous by state revision")
    try:
        model_budget = completed_model_budget_chain(records, config=policy)
    except BudgetAdmissionError as exc:
        raise ValueError("model ledger budget does not match the policy") from exc
    if (
        model_budget.initial_model_calls != persisted.initial_model_calls
        or model_budget.initial_total_tokens != persisted.initial_model_tokens
        or model_budget.used_model_calls < persisted.used_model_calls
        or model_budget.used_total_tokens < persisted.used_model_tokens
        or model_budget.used_model_calls > persisted.initial_model_calls
        or model_budget.used_total_tokens > persisted.initial_model_tokens
    ):
        raise ValueError("model ledger budget does not monotonically extend state")
    budget = BudgetState.model_validate(
        {
            **persisted.model_dump(mode="python", round_trip=True, warnings="error"),
            "used_model_calls": model_budget.used_model_calls,
            "remaining_model_calls": model_budget.remaining_model_calls,
            "used_model_tokens": model_budget.used_total_tokens,
            "remaining_model_tokens": model_budget.remaining_total_tokens,
        }
    )
    return ResearchState.model_validate(
        {
            **state.model_dump(mode="python", round_trip=True, warnings="error"),
            "budget_state": budget,
        }
    )


def _checkpoint_key(state: ResearchState) -> AdaptiveCheckpointKey:
    return AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, state.revision
    )


def _duplicate_action_context(action: ResearchAction) -> dict[str, object]:
    return {
        "action_digest": action.action_digest,
        "kind": action.kind,
        "target": action.target.model_dump(mode="json", by_alias=True),
        "parameters": [list(item) for item in sorted(action.parameters)],
    }


def _normalize_model_source_ids(
    state: ResearchState,
    decision: ResearchDecisionV1,
    *,
    freshness_context: FreshnessContext | None = None,
) -> ResearchDecisionV1:
    source_ids = tuple(
        item.source_id for item in state.query_spec.semantic_items
    )
    semantic_items = state.query_spec.semantic_items
    semantic_items_by_source = {
        item.source_id: item for item in semantic_items
    }
    sole_required_source_id = (
        semantic_items[0].source_id
        if len(semantic_items) == 1 and semantic_items[0].required
        else None
    )
    has_exact_source_anchor = any(
        isinstance(proposal, NewBindingProposal)
        and proposal.source_id == sole_required_source_id
        for proposal in decision.proposals
    )
    binding_ids = tuple(binding.binding_id for binding in state.bindings)
    proposals = []
    changed = False
    for proposal in decision.proposals:
        if isinstance(proposal, NewBindingProposal) and proposal.source_id not in source_ids:
            matches = tuple(
                source_id
                for source_id in source_ids
                if _edit_distance_one(source_id, proposal.source_id)
            )
            if len(matches) == 1:
                proposal = proposal.model_copy(update={"source_id": matches[0]})
                changed = True
            elif sole_required_source_id is not None and has_exact_source_anchor:
                proposal = proposal.model_copy(
                    update={"source_id": sole_required_source_id}
                )
                changed = True
        if (
            isinstance(proposal, BindingAssessment)
            and isinstance(proposal.subject, ExistingBindingRef)
            and proposal.subject.binding_id not in binding_ids
        ):
            matches = tuple(
                binding_id
                for binding_id in binding_ids
                if _edit_distance_one(binding_id, proposal.subject.binding_id)
            )
            if len(matches) == 1:
                proposal = proposal.model_copy(
                    update={"subject": ExistingBindingRef(binding_id=matches[0])}
                )
                changed = True
        source_item = semantic_items_by_source.get(
            getattr(proposal, "source_id", None)
        )
        if (
            isinstance(proposal, NewBindingProposal)
            and isinstance(proposal.candidate, DiscriminatorValueCandidate)
            and proposal.candidate.additional_predicates == ()
            and proposal.candidate.discriminator_predicate.left
            == proposal.candidate.discriminator_column
            and source_item is not None
            and source_item.kind is SemanticItemKind.DIMENSION
            and source_item.source_id
            in state.query_spec.requested_output_source_ids
        ):
            proposal = proposal.model_copy(
                update={
                    "candidate": PhysicalColumnCandidate(
                        physical_column=proposal.candidate.discriminator_column
                    )
                }
            )
            changed = True
        normalized = _normalize_new_join_citation(state, proposal)
        if normalized is not proposal:
            proposal = normalized
            changed = True
        normalized = _normalize_physical_column_citation(state, proposal)
        if normalized is not proposal:
            proposal = normalized
            changed = True
        normalized = _normalize_categorical_in_citation(
            state,
            proposal,
            freshness_context=freshness_context,
        )
        if normalized is not proposal:
            proposal = normalized
            changed = True
        normalized = _normalize_derived_expression_citation(state, proposal)
        if normalized is not proposal:
            proposal = normalized
            changed = True
        normalized = _normalize_categorical_replacement_assessment_citation(
            state,
            proposal,
            decision,
            freshness_context=freshness_context,
        )
        if normalized is not proposal:
            proposal = normalized
            changed = True
        normalized = _normalize_existing_binding_assessment_citation(
            state,
            proposal,
            freshness_context=freshness_context,
        )
        if normalized is not proposal:
            proposal = normalized
            changed = True
        normalized = _normalize_existing_hypothesis_assessment_citation(
            state,
            proposal,
            freshness_context=freshness_context,
        )
        if normalized is not proposal:
            proposal = normalized
            changed = True
        proposals.append(proposal)
    if not changed:
        return _canonicalize_unknown_binding_assessment_reference(state, decision)
    return _canonicalize_unknown_binding_assessment_reference(
        state, decision.model_copy(update={"proposals": tuple(proposals)})
    )


def _edit_distance_one(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1 or left == right:
        return False
    if len(left) == len(right):
        return sum(first != second for first, second in zip(left, right, strict=True)) == 1
    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    index = 0
    while index < len(shorter) and longer[index] == shorter[index]:
        index += 1
    return longer[index + 1 :] == shorter[index:]


def _normalize_exact_document_formula_candidates(
    state: ResearchState,
    decision: ResearchDecisionV1,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...],
) -> ResearchDecisionV1:
    documents_by_source = dict(exact_formula_documents)
    items_by_source = {item.source_id: item for item in state.query_spec.semantic_items}
    proposals = []
    changed = False
    for proposal in decision.proposals:
        document = documents_by_source.get(getattr(proposal, "source_id", None))
        if (
            isinstance(proposal, NewBindingProposal)
            and isinstance(proposal.candidate, DerivedExpressionCandidate)
            and document is not None
            and proposal.candidate.document_id == document.document_id
            and (item := items_by_source.get(proposal.source_id)) is not None
            and (formula := _formula_part(item.normalized_meaning)) is not None
        ):
            proposal = proposal.model_copy(
                update={
                    "candidate": proposal.candidate.model_copy(
                        update={
                            "expression_claim": formula,
                            "rule_excerpt": formula,
                        }
                    )
                }
            )
            changed = True
        proposals.append(proposal)
    if not changed:
        return decision
    return decision.model_copy(update={"proposals": tuple(proposals)})


def _exact_aggregate_operand_input_mismatch(
    formula: str, schema_names: tuple[str, ...], input_names: tuple[str, ...]
) -> bool:
    return bool(
        _missing_exact_aggregate_operand_names(formula, schema_names, input_names)
    )


def _missing_exact_aggregate_operand_names(
    formula: str, schema_names: tuple[str, ...], input_names: tuple[str, ...]
) -> tuple[str, ...]:
    names = {name.lower(): name for name in schema_names}
    inputs = {name.lower() for name in input_names}
    formula = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", "0", formula)
    aggregate_where_clauses: list[str] = []
    for aggregate in re.finditer(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", formula, re.I):
        depth = 1
        index = aggregate.end()
        while index < len(formula) and depth:
            if formula[index] == "(":
                depth += 1
            elif formula[index] == ")":
                depth -= 1
            index += 1
        fragment = formula[aggregate.end() : index - 1 if depth == 0 else len(formula)]
        where = re.match(
            r"^\s*(?:[A-Za-z_][A-Za-z0-9_.]*|\*)\s*WHERE(?P<clause>.*)$",
            fragment,
            re.I,
        )
        if where is not None:
            aggregate_where_clauses.append(where["clause"])
    return tuple(
        sorted(
            original_name
            for name, original_name in names.items()
            if name not in inputs
            and (
                re.search(
                    rf"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(\s*{re.escape(name)}\s*(?=\)|WHERE)",
                    formula,
                    re.I,
                )
                or any(
                    re.search(
                        rf"\b{re.escape(name)}\b(?!\s*\()",
                        clause,
                        re.I,
                    )
                    or re.search(
                        rf"(?<=[0-9)])(?:AND|OR){re.escape(name)}\b(?!\s*\()",
                        clause,
                        re.I,
                    )
                    for clause in aggregate_where_clauses
                )
            )
        )
    )


def _has_confirmed_differently_named_aggregate_input(
    state: ResearchState,
    proposal: NewBindingProposal | DerivedExpressionBinding,
    formula: str,
    schema_names: tuple[str, ...],
    *,
    confirmed_columns: tuple[ColumnRef, ...] | None = None,
) -> bool:
    if isinstance(proposal, NewBindingProposal):
        if not isinstance(proposal.candidate, DerivedExpressionCandidate):
            return False
        source_id = proposal.source_id
        input_columns = proposal.candidate.input_columns
    else:
        source_id = proposal.source_id
        input_columns = proposal.input_columns
    operand_names = {
        name.casefold()
        for name in _missing_exact_aggregate_operand_names(formula, schema_names, ())
    }
    different_inputs = tuple(
        column for column in input_columns if column.column.casefold() not in operand_names
    )
    missing_names = _missing_exact_aggregate_operand_names(
        formula,
        schema_names,
        tuple(column.column for column in input_columns),
    )
    if confirmed_columns is None:
        confirmed_columns = tuple(
            binding.physical_column
            for binding in state.bindings
            if isinstance(binding, PhysicalColumnBinding)
            and binding.source_id == source_id
            and binding.status in (BindingStatus.CANDIDATE, BindingStatus.SUPPORTED)
            and binding.evidence_ids
        )
    return (
        bool(different_inputs)
        and len(different_inputs) == len(missing_names)
        and all(
            any(
                column.column == confirmed.column
                and (
                    column.table == confirmed.table
                    or column.table
                    in {
                        confirmed.table.table,
                        _logical_table_name(confirmed.table),
                    }
                )
                for confirmed in confirmed_columns
            )
            for column in different_inputs
        )
    )


def _has_same_batch_confirmed_differently_named_aggregate_input(
    state: ResearchState,
    decision: ResearchDecisionV1,
    resolved: ResolvedResearchDecision,
    documents: tuple[tuple[str, DocumentRef], ...],
    loaded_schema: LoadedSchema,
) -> bool:
    if not documents or not isinstance(loaded_schema, LoadedSchema):
        return False
    items = {item.source_id: item for item in state.query_spec.semantic_items}
    bindings = {binding.binding_id: binding for binding in state.bindings}
    document_map = dict(documents)
    schema_names = tuple(
        column
        for table in loaded_schema.schema.values()
        if isinstance(table, Mapping)
        for column in get_table_columns(table)
    )
    saw_mismatch = False
    for proposal in decision.proposals:
        if (
            isinstance(proposal, NewBindingProposal)
            and isinstance(proposal.candidate, DerivedExpressionCandidate)
            and (item := items.get(proposal.source_id)) is not None
            and (document := document_map.get(proposal.source_id)) is not None
            and proposal.candidate.document_id == document.document_id
        ):
            candidate: NewBindingProposal | DerivedExpressionBinding = proposal
            input_columns = proposal.candidate.input_columns
        elif (
            isinstance(proposal, BindingAssessment)
            and proposal.certificate == "consistent"
            and isinstance(proposal.subject, ExistingBindingRef)
            and isinstance(
                binding := bindings.get(proposal.subject.binding_id),
                DerivedExpressionBinding,
            )
            and binding.status is BindingStatus.CANDIDATE
            and binding.evidence_ids
            and (item := items.get(binding.source_id)) is not None
            and (document := document_map.get(binding.source_id)) is not None
            and binding.document == document
            and (formula := _formula_part(item.normalized_meaning)) is not None
            and _formula_part(binding.expression.expression) == formula
        ):
            candidate = binding
            input_columns = binding.input_columns
        else:
            continue
        formula = _formula_part(item.normalized_meaning) or ""
        if not _exact_aggregate_operand_input_mismatch(
            formula,
            schema_names,
            tuple(column.column for column in input_columns),
        ):
            continue
        saw_mismatch = True
        confirmed_columns = tuple(
            binding.physical_column
            for binding in state.bindings
            if isinstance(binding, PhysicalColumnBinding)
            and binding.source_id == candidate.source_id
            and binding.status in (BindingStatus.CANDIDATE, BindingStatus.SUPPORTED)
            and binding.evidence_ids
        ) + tuple(
            binding.physical_column
            if isinstance(binding, PhysicalColumnBinding)
            else binding.discriminator_column
            for binding in resolved.admission.bindings
            if isinstance(binding, (PhysicalColumnBinding, DiscriminatorValueBinding))
            and binding.source_id == candidate.source_id
            and binding.status in (BindingStatus.CANDIDATE, BindingStatus.SUPPORTED)
            and binding.evidence_ids
        )
        if not _has_confirmed_differently_named_aggregate_input(
            state,
            candidate,
            formula,
            schema_names,
            confirmed_columns=confirmed_columns,
        ):
            return False
    return saw_mismatch


def _has_exact_aggregate_operand_mismatch(
    state: ResearchState,
    decision: ResearchDecisionV1,
    documents: tuple[tuple[str, DocumentRef], ...],
    loaded_schema: LoadedSchema,
) -> bool:
    if not documents or not isinstance(loaded_schema, LoadedSchema):
        return False
    items = {item.source_id: item for item in state.query_spec.semantic_items}
    bindings = {binding.binding_id: binding for binding in state.bindings}
    document_map = dict(documents)
    schema_names = tuple(
        column
        for table in loaded_schema.schema.values()
        if isinstance(table, Mapping)
        for column in get_table_columns(table)
    )
    for proposal in decision.proposals:
        if (
            isinstance(proposal, NewBindingProposal)
            and isinstance(proposal.candidate, DerivedExpressionCandidate)
            and (item := items.get(proposal.source_id)) is not None
            and (document := document_map.get(proposal.source_id)) is not None
            and proposal.candidate.document_id == document.document_id
        ):
            formula = _formula_part(item.normalized_meaning) or ""
            if _exact_aggregate_operand_input_mismatch(
                formula,
                schema_names,
                tuple(column.column for column in proposal.candidate.input_columns),
            ) and not _has_confirmed_differently_named_aggregate_input(
                state, proposal, formula, schema_names
            ):
                return True
        if (
            isinstance(proposal, BindingAssessment)
            and proposal.certificate == "consistent"
            and isinstance(proposal.subject, ExistingBindingRef)
            and isinstance(
                binding := bindings.get(proposal.subject.binding_id),
                DerivedExpressionBinding,
            )
            and binding.status is BindingStatus.CANDIDATE
            and (item := items.get(binding.source_id)) is not None
            and (document := document_map.get(binding.source_id)) is not None
            and binding.document == document
            and (formula := _formula_part(item.normalized_meaning)) is not None
            and _formula_part(binding.expression.expression) == formula
            and _exact_aggregate_operand_input_mismatch(
                formula,
                schema_names,
                tuple(column.column for column in binding.input_columns),
            )
        ):
            return True
    return False


def _has_exact_formula_predicate_mismatch(
    state: ResearchState,
    decision: ResearchDecisionV1,
    constraints: tuple[tuple[str, DocumentRef, str, str], ...],
) -> bool:
    """Reject a proxy discriminator for a trusted exact predicate only."""

    by_source: dict[str, list[tuple[str, str]]] = {}
    for source_id, _document, column, literal in constraints:
        by_source.setdefault(source_id, []).append((column, literal))
    bindings = {binding.binding_id: binding for binding in state.bindings}

    def differs(source_id: str, column: str, predicate: object) -> bool:
        matching = [
            name
            for name, literal in by_source.get(source_id, ())
            if literal == getattr(predicate, "right", None)
        ]
        return bool(
            getattr(predicate, "operator", None) is PredicateOperator.EQ
            and isinstance(getattr(predicate, "right", None), str)
            and matching
            and all(column.casefold() != name.casefold() for name in matching)
        )

    for proposal in decision.proposals:
        if isinstance(proposal, NewBindingProposal) and isinstance(
            proposal.candidate, DiscriminatorValueCandidate
        ):
            candidate = proposal.candidate
            if differs(
                proposal.source_id,
                candidate.discriminator_column.column,
                candidate.discriminator_predicate,
            ):
                return True
        elif (
            isinstance(proposal, BindingAssessment)
            and proposal.certificate == "consistent"
            and isinstance(proposal.subject, ExistingBindingRef)
            and isinstance(
                binding := bindings.get(proposal.subject.binding_id),
                DiscriminatorValueBinding,
            )
            and binding.status is BindingStatus.CANDIDATE
            and differs(
                binding.source_id,
                binding.discriminator_column.column,
                binding.discriminator_predicate,
            )
        ):
            return True
    return False


def _missing_resolved_exact_formula_predicates(
    state: ResearchState,
    constraints: tuple[tuple[str, DocumentRef, str, str], ...],
) -> dict[str, tuple[str, ...]]:
    """Return trusted exact predicates omitted from resolved formulas."""

    resolved_formula_sources = {
        item.source_id
        for item in state.query_spec.semantic_items
        if item.required
        and item.kind is SemanticItemKind.FORMULA
        and item.status is SemanticItemStatus.RESOLVED
    }
    supported = tuple(
        binding
        for binding in state.bindings
        if isinstance(binding, DiscriminatorValueBinding)
        and binding.status is BindingStatus.SUPPORTED
        and binding.source_id in resolved_formula_sources
    )
    missing: dict[str, set[str]] = {}
    for source_id, _document, column, literal in constraints:
        if source_id not in resolved_formula_sources:
            continue
        if any(
            binding.source_id == source_id
            and binding.discriminator_column.column.casefold() == column.casefold()
            and binding.discriminator_predicate.operator is PredicateOperator.EQ
            and binding.discriminator_predicate.right == literal
            for binding in supported
        ):
            continue
        missing.setdefault(source_id, set()).add(column)
    return {
        source_id: tuple(sorted(columns))
        for source_id, columns in sorted(missing.items())
    }


def _normalize_new_join_citation(
    state: ResearchState,
    proposal: object,
) -> object:
    if not isinstance(proposal, NewJoinProposal) or not proposal.path:
        return proposal
    durable_evidence_ids = {evidence.evidence_id for evidence in state.evidence}
    known = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id in durable_evidence_ids
    )
    unknown = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id not in durable_evidence_ids
    )
    if len(unknown) != 1:
        return proposal
    matches = tuple(
        sorted(
            evidence.evidence_id
            for evidence in state.evidence
            if evidence.evidence_id not in known
            and _new_join_has_declared_relationship_certificate(proposal, evidence)
        )
    )
    if len(matches) != 1:
        return proposal
    return proposal.model_copy(
        update={"citation_evidence_ids": tuple(sorted((*known, matches[0])))}
    )


def _new_join_has_declared_relationship_certificate(
    proposal: NewJoinProposal,
    evidence: EvidenceRecord,
) -> bool:
    if not isinstance(evidence.target, TableRef):
        return False

    def column(logical: LogicalColumnRef) -> ColumnRef:
        schema, separator, table = logical.table.rpartition(".")
        return ColumnRef(
            table=TableRef(
                namespace=evidence.target.namespace,
                schema=schema if separator else None,
                table=table if separator else logical.table,
            ),
            column=logical.column,
        )

    try:
        join = JoinCandidate(
            join_id="citation-normalization",
            left=column(proposal.left),
            right=column(proposal.right),
            join_type=proposal.join_type,
            path=tuple(
                JoinEdge(
                    left=column(edge.left),
                    right=column(edge.right),
                    join_type=edge.join_type,
                )
                for edge in proposal.path
            ),
            status=JoinCandidateStatus.CANDIDATE,
            evidence_ids=(),
        )
        return _declared_join_certificate(join, evidence)
    except (MalformedProvenanceError, SemanticReducerError, ValidationError):
        return False


def _normalize_physical_column_citation(
    state: ResearchState,
    proposal: object,
) -> object:
    if not isinstance(proposal, NewBindingProposal):
        return proposal
    candidate = proposal.candidate
    if isinstance(candidate, PhysicalColumnCandidate):
        logical = candidate.physical_column
    elif (
        isinstance(candidate, DiscriminatorValueCandidate)
        and candidate.discriminator_predicate.left
        == candidate.discriminator_column
    ):
        logical = candidate.discriminator_column
    else:
        return proposal
    durable_evidence_ids = {evidence.evidence_id for evidence in state.evidence}
    known = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id in durable_evidence_ids
    )
    unknown = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id not in durable_evidence_ids
    )
    if len(unknown) != 1:
        return proposal
    matches = tuple(
        sorted(
            evidence.evidence_id
            for evidence in state.evidence
            if evidence.evidence_id not in known
            and (
                (
                    isinstance(evidence.target, ColumnRef)
                    and evidence.target.column == logical.column
                    and logical.table
                    in {
                        evidence.target.table.table,
                        _logical_table_name(evidence.target.table),
                    }
                )
                or (
                    isinstance(evidence.target, TableRef)
                    and _evidence_observes_exact_derived_input_column(
                        evidence, logical
                    )
                )
            )
        )
    )
    if len(matches) != 1:
        return proposal
    return proposal.model_copy(
        update={"citation_evidence_ids": tuple(sorted((*known, matches[0])))}
    )


def _normalize_categorical_in_citation(
    state: ResearchState,
    proposal: object,
    *,
    freshness_context: FreshnessContext | None,
) -> object:
    if (
        not isinstance(proposal, NewBindingProposal)
        or not isinstance(proposal.candidate, DiscriminatorValueCandidate)
        or freshness_context is None
    ):
        return proposal
    candidate = proposal.candidate
    predicate = candidate.discriminator_predicate
    if (
        candidate.additional_predicates
        or predicate.left != candidate.discriminator_column
        or predicate.operator is not PredicateOperator.IN
        or not isinstance(predicate.right, tuple)
        or not predicate.right
    ):
        return proposal
    durable_evidence_ids = {evidence.evidence_id for evidence in state.evidence}
    known = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id in durable_evidence_ids
    )
    unknown = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id not in durable_evidence_ids
    )
    if len(unknown) != 1:
        return proposal
    try:
        matches = tuple(
            sorted(
                evidence.evidence_id
                for evidence in state.evidence
                if evidence.evidence_id not in known
                and evaluate_evidence_freshness(
                    evidence, freshness_context
                ).status
                is FreshnessStatus.FRESH
                and isinstance(evidence.target, ColumnRef)
                and evidence.target.column == candidate.discriminator_column.column
                and candidate.discriminator_column.table
                in {
                    evidence.target.table.table,
                    _logical_table_name(evidence.target.table),
                }
                and all(
                    evidence_observes_exact_value(
                        evidence, evidence.target, value
                    )
                    for value in predicate.right
                )
            )
        )
    except (ExactValueCertificateError, TypeError, ValueError):
        return proposal
    if len(matches) != 1:
        return proposal
    return proposal.model_copy(
        update={"citation_evidence_ids": tuple(sorted((*known, matches[0])))}
    )


def _normalize_derived_expression_citation(
    state: ResearchState,
    proposal: object,
) -> object:
    if (
        not isinstance(proposal, NewBindingProposal)
        or not isinstance(proposal.candidate, DerivedExpressionCandidate)
    ):
        return proposal
    durable_evidence_ids = {evidence.evidence_id for evidence in state.evidence}
    known = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id in durable_evidence_ids
    )
    unknown = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id not in durable_evidence_ids
    )
    if len(unknown) != 1:
        return proposal
    document_evidence = tuple(
        evidence
        for evidence in state.evidence
        if evidence.source_kind is EvidenceSourceKind.DOCUMENT
        and isinstance(evidence.target, DocumentRef)
    )
    document_candidates = tuple(
        sorted(
            evidence.evidence_id
            for evidence in document_evidence
            if evidence.evidence_id not in known
            and evidence.target.document_id == proposal.candidate.document_id
        )
    )
    if len(document_candidates) == 1:
        return proposal.model_copy(
            update={
                "citation_evidence_ids": tuple(
                    sorted((*known, document_candidates[0]))
                )
            }
        )
    known_document_citations = {
        evidence.evidence_id
        for evidence in document_evidence
        if evidence.evidence_id in known
        and evidence.target.document_id == proposal.candidate.document_id
    }
    if document_evidence and not known_document_citations:
        return proposal
    try:
        candidates = tuple(
            sorted(
                evidence.evidence_id
                for evidence in state.evidence
                if evidence.evidence_id not in known
                and any(
                    _evidence_observes_exact_derived_input_column(
                        evidence, input_column
                    )
                    for input_column in proposal.candidate.input_columns
                )
            )
        )
    except (ExactValueCertificateError, MalformedProvenanceError):
        return proposal
    if len(candidates) != 1:
        return proposal
    return proposal.model_copy(
        update={"citation_evidence_ids": tuple(sorted((*known, candidates[0])))}
    )


def _normalize_categorical_replacement_assessment_citation(
    state: ResearchState,
    proposal: object,
    decision: ResearchDecisionV1,
    *,
    freshness_context: FreshnessContext | None,
) -> object:
    if (
        not isinstance(proposal, BindingAssessment)
        or proposal.certificate != "contradicted"
        or not isinstance(proposal.subject, ExistingBindingRef)
        or freshness_context is None
    ):
        return proposal
    binding = next(
        (
            item
            for item in state.bindings
            if item.binding_id == proposal.subject.binding_id
        ),
        None,
    )
    if binding is None:
        return proposal
    durable_evidence_ids = {evidence.evidence_id for evidence in state.evidence}
    known = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id in durable_evidence_ids
    )
    unknown = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id not in durable_evidence_ids
    )
    try:
        fresh_evidence = tuple(
            evidence
            for evidence in state.evidence
            if evaluate_evidence_freshness(evidence, freshness_context).status
            is FreshnessStatus.FRESH
        )
    except (TypeError, ValueError):
        return proposal
    if not unknown:
        if not isinstance(binding, DiscriminatorValueBinding):
            return proposal
        predicate = binding.discriminator_predicate
        if not (
            predicate.operator is PredicateOperator.IN
            and isinstance(binding.discriminator_column, ColumnRef)
            and type(predicate.right) is tuple
            and predicate.right
            and all(type(literal) is str for literal in predicate.right)
        ):
            return proposal
        cited = tuple(
            evidence
            for evidence in fresh_evidence
            if evidence.evidence_id in known
        )
        missing_literals = tuple(
            literal
            for literal in predicate.right
            if not any(
                _exact_categorical_zero_search_value(
                    evidence, binding.discriminator_column, literal
                )
                for evidence in cited
            )
        )
        if not missing_literals:
            return proposal
        additions: list[str] = []
        for literal in missing_literals:
            matches = tuple(
                evidence.evidence_id
                for evidence in fresh_evidence
                if evidence.evidence_id not in {*known, *additions}
                and _exact_categorical_zero_search_value(
                    evidence, binding.discriminator_column, literal
                )
            )
            if len(matches) != 1:
                return proposal
            additions.append(matches[0])
        assessment_candidate = proposal.model_copy(
            update={"citation_evidence_ids": tuple(sorted((*known, *additions)))}
        )
        decision_candidate = decision.model_copy(
            update={
                "proposals": tuple(
                    assessment_candidate if item is proposal else item
                    for item in decision.proposals
                )
            }
        )
        if not _categorical_in_recovery_replacement_feedback_certificate(
            binding,
            assessment_candidate,
            decision_candidate,
            fresh_evidence,
        ):
            return proposal
        return assessment_candidate
    if len(unknown) != 1:
        return proposal
    matches: list[str] = []
    for evidence in fresh_evidence:
        if evidence.evidence_id in known:
            continue
        assessment_candidate = proposal.model_copy(
            update={
                "citation_evidence_ids": tuple(sorted((*known, evidence.evidence_id)))
            }
        )
        decision_candidate = decision.model_copy(
            update={
                "proposals": tuple(
                    assessment_candidate if item is proposal else item
                    for item in decision.proposals
                )
            }
        )
        if _categorical_in_recovery_replacement_feedback_certificate(
            binding,
            assessment_candidate,
            decision_candidate,
            fresh_evidence,
        ):
            matches.append(evidence.evidence_id)
    if len(matches) != 1:
        return proposal
    return proposal.model_copy(
        update={"citation_evidence_ids": tuple(sorted((*known, matches[0])))}
    )


def _exact_categorical_zero_search_value(
    evidence: EvidenceRecord,
    column: ColumnRef,
    literal: str,
) -> bool:
    observation = parse_probe_observation(evidence.observation)
    if observation is None or not isinstance(observation.payload, dict):
        return False
    payload = observation.payload
    return (
        observation.provenance.probe_kind is ResearchActionKind.SEARCH_VALUE
        and not observation.truncated
        and evidence.target == column
        and payload.get("columns") == [column.column]
        and type(payload.get("requested_value")) is str
        and payload["requested_value"] == literal
        and payload.get("rows") == []
    )


def _evidence_observes_exact_derived_input_column(
    evidence: EvidenceRecord,
    input_column: LogicalColumnRef,
) -> bool:
    if isinstance(evidence.target, ColumnRef):
        return (
            input_column.column == evidence.target.column
            and input_column.table
            in {
                evidence.target.table.table,
                _logical_table_name(evidence.target.table),
            }
            and evidence_observes_exact_column(evidence, evidence.target)
        )
    if (
        not isinstance(evidence.target, TableRef)
        or evidence.source_kind is not EvidenceSourceKind.SCHEMA
        or input_column.table
        not in {
            evidence.target.table,
            _logical_table_name(evidence.target),
        }
    ):
        return False
    observation = parse_probe_observation(evidence.observation)
    if observation is None or not isinstance(observation.payload, dict):
        return False
    columns = observation.payload.get("columns")
    return (
        observation.provenance.probe_kind is ResearchActionKind.INSPECT_TABLE
        and observation.payload.get("status") == "matched"
        and observation.payload.get("table")
        == evidence.target.model_dump(mode="json", by_alias=True)
        and isinstance(columns, list)
        and sum(
            type(column) is dict
            and type(column.get("name")) is str
            and column["name"] == input_column.column
            for column in columns
        )
        == 1
    )


def _normalize_existing_binding_assessment_citation(
    state: ResearchState,
    proposal: object,
    *,
    freshness_context: FreshnessContext | None,
) -> object:
    if (
        not isinstance(proposal, BindingAssessment)
        or not isinstance(proposal.subject, ExistingBindingRef)
    ):
        return proposal
    binding = next(
        (
            item
            for item in state.bindings
            if item.binding_id == proposal.subject.binding_id
        ),
        None,
    )
    if binding is None:
        return proposal
    durable_evidence_ids = {evidence.evidence_id for evidence in state.evidence}
    if (
        proposal.certificate == "consistent"
        and binding.status is BindingStatus.CANDIDATE
        and freshness_context is not None
    ):
        if (
            binding.evidence_ids
            and set(binding.evidence_ids) <= durable_evidence_ids
            and all(
                evaluate_evidence_freshness(evidence, freshness_context).status
                is FreshnessStatus.FRESH
                for evidence in state.evidence
                if evidence.evidence_id in binding.evidence_ids
            )
        ):
            return proposal.model_copy(
                update={"citation_evidence_ids": tuple(sorted(binding.evidence_ids))}
            )
        return proposal
    known = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id in durable_evidence_ids
    )
    unknown = tuple(
        evidence_id
        for evidence_id in proposal.citation_evidence_ids
        if evidence_id not in durable_evidence_ids
    )
    if (
        len(unknown) == 1
        and binding.evidence_ids
        and set(binding.evidence_ids) <= durable_evidence_ids
        and known == tuple(sorted(binding.evidence_ids))
    ):
        return proposal.model_copy(update={"citation_evidence_ids": known})
    candidates = tuple(
        sorted((set(binding.evidence_ids) - set(known)) & durable_evidence_ids)
    )
    if len(unknown) != 1 or len(candidates) != 1:
        return proposal
    return proposal.model_copy(
        update={"citation_evidence_ids": tuple(sorted((*known, candidates[0])))}
    )


def _normalize_existing_hypothesis_assessment_citation(
    state: ResearchState,
    proposal: object,
    *,
    freshness_context: FreshnessContext | None,
) -> object:
    if (
        not isinstance(proposal, HypothesisAssessment)
        or proposal.certificate != "consistent"
        or not isinstance(proposal.subject, ExistingHypothesisRef)
        or freshness_context is None
    ):
        return proposal
    hypothesis = next(
        (
            item
            for item in state.hypotheses
            if item.hypothesis_id == proposal.subject.hypothesis_id
        ),
        None,
    )
    if (
        hypothesis is None
        or hypothesis.status is not HypothesisStatus.PROPOSED
        or not hypothesis.evidence_ids
    ):
        return proposal
    durable_evidence_ids = {evidence.evidence_id for evidence in state.evidence}
    if not set(hypothesis.evidence_ids) <= durable_evidence_ids:
        return proposal
    if not all(
        evaluate_evidence_freshness(evidence, freshness_context).status
        is FreshnessStatus.FRESH
        for evidence in state.evidence
        if evidence.evidence_id in hypothesis.evidence_ids
    ):
        return proposal
    return proposal.model_copy(
        update={"citation_evidence_ids": tuple(sorted(hypothesis.evidence_ids))}
    )


def _canonicalize_unknown_binding_assessment_reference(
    state: ResearchState,
    decision: ResearchDecisionV1,
) -> ResearchDecisionV1:
    durable_binding_ids = {binding.binding_id for binding in state.bindings}
    binding_assessments = tuple(
        proposal
        for proposal in decision.proposals
        if isinstance(proposal, BindingAssessment)
    )
    if len(binding_assessments) != 1:
        return decision
    unknown = binding_assessments[0]
    if (
        not isinstance(unknown.subject, ExistingBindingRef)
        or unknown.subject.binding_id in durable_binding_ids
    ):
        return decision
    candidate_binding_ids = tuple(
        binding.binding_id
        for binding in state.bindings
        if binding.status is BindingStatus.CANDIDATE
        and sum(
            item.status is SemanticItemStatus.PARTIALLY_RESOLVED
            and binding.binding_id in item.binding_ids
            for item in state.query_spec.semantic_items
        )
        == 1
    )
    if len(candidate_binding_ids) != 1:
        return decision
    return decision.model_copy(
        update={
            "proposals": tuple(
                proposal.model_copy(
                    update={
                        "subject": ExistingBindingRef(
                            binding_id=candidate_binding_ids[0]
                        )
                    }
                )
                if proposal is unknown
                else proposal
                for proposal in decision.proposals
            )
        }
    )


def _rejected_preflight_assessment_context(
    state: ResearchState,
    decision: ResearchDecisionV1,
    freshness_context: FreshnessContext,
    requested_action: ResearchAction | None,
    *,
    exact_column: ColumnRef | None = None,
    loaded_schema: LoadedSchema | None = None,
    missing_exact_predicates: Mapping[str, tuple[str, ...]] | None = None,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...] = (),
    table_namespace: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Describe rejected proposals and one deterministic missing probe."""

    bindings = {binding.binding_id: binding for binding in state.bindings}
    hypotheses = {
        hypothesis.hypothesis_id: hypothesis for hypothesis in state.hypotheses
    }
    joins = {join.join_id: join for join in state.join_candidates}
    source_items = {
        item.source_id: item for item in state.query_spec.semantic_items
    }
    source_ids = set(source_items)
    durable_evidence_ids = {evidence.evidence_id for evidence in state.evidence}
    try:
        fresh_evidence = tuple(
            evidence
            for evidence in state.evidence
            if evaluate_evidence_freshness(evidence, freshness_context).status
            is FreshnessStatus.FRESH
        )
    except (TypeError, ValueError):
        return ()
    rejected_tool: dict[str, object] | None = None
    if (
        requested_action is None
        and isinstance(decision.next, ToolIntent)
        and decision.next.intent.tool_name
        in {
            "inspect_column",
            "profile_column",
            "search_value",
            "get_distinct_values",
        }
        and isinstance(loaded_schema, LoadedSchema)
    ):
        raw_intent = decision.next.intent
        raw_table = raw_intent.arguments.table
        raw_column = raw_intent.arguments.column
        loaded_tables = tuple(
            (table_name, table_schema)
            for table_name, table_schema in loaded_schema.schema.items()
            if type(table_name) is str and isinstance(table_schema, Mapping)
        )
        exact_raw_tables = tuple(
            (table_name, table_schema)
            for table_name, table_schema in loaded_tables
            if (
                table_name == raw_table
                if "." in raw_table
                else table_name.rsplit(".", 1)[-1] == raw_table
            )
        )
        casefold_raw_tables = tuple(
            (table_name, table_schema)
            for table_name, table_schema in loaded_tables
            if (
                table_name.casefold() == raw_table.casefold()
                if "." in raw_table
                else table_name.rsplit(".", 1)[-1].casefold()
                == raw_table.casefold()
            )
        )
        raw_target_is_exact_schema_column = (
            len(exact_raw_tables) == 1
            and len(casefold_raw_tables) == 1
            and raw_column in get_table_columns(exact_raw_tables[0][1])
        )
        same_name_columns = sorted(
            {
                (table_name, column_name)
                for table_name, table_schema in loaded_tables
                for column_name in get_table_columns(table_schema)
                if type(column_name) is str and column_name == raw_column
            }
        )
        if not raw_target_is_exact_schema_column:
            rejected_tool = {
                "rejected_tool": {
                    "tool_name": raw_intent.tool_name,
                    "table": raw_table,
                    "column": raw_column,
                },
                "rejection_reason": (
                    "target column is not resolvable"
                    if same_name_columns
                    else "target column is absent from captured schema"
                ),
            }
            if same_name_columns:
                rejected_tool["same_name_schema_columns"] = [
                    {"table": table_name, "column": column_name}
                    for table_name, column_name in same_name_columns
                ]
            if not decision.proposals:
                return (rejected_tool,)
    rejected: list[dict[str, object]] = []
    candidates: list[tuple[ColumnRef, dict[str, object], dict[str, object]]] = []
    join_candidates: list[tuple[TableRef, dict[str, object], dict[str, object]]] = []
    missing_calendar_year_range = _missing_calendar_year_range_derived_proposal_keys(
        state,
        decision,
        loaded_schema=loaded_schema,
        exact_formula_documents=exact_formula_documents,
    )
    for proposal in decision.proposals:
        if not isinstance(
            proposal,
            (
                BindingAssessment,
                JoinAssessment,
                HypothesisAssessment,
                NewBindingProposal,
            ),
        ):
            continue
        item: dict[str, object] = {
            "proposal": proposal.model_dump(mode="json", by_alias=True),
        }
        proposal_source_id = None
        if isinstance(proposal, NewBindingProposal):
            proposal_source_id = proposal.source_id
        elif isinstance(proposal, BindingAssessment) and isinstance(
            proposal.subject, ExistingBindingRef
        ):
            binding = bindings.get(proposal.subject.binding_id)
            if binding is not None:
                proposal_source_id = binding.source_id
        if (
            missing_exact_predicates is not None
            and proposal_source_id in missing_exact_predicates
        ):
            item["source_id"] = proposal_source_id
            item["missing_exact_predicate_columns"] = list(
                missing_exact_predicates[proposal_source_id]
            )
            item["rejection_reason"] = (
                "resolved formula omits trusted exact predicate"
            )
        if any(
            evidence_id not in durable_evidence_ids
            for evidence_id in proposal.citation_evidence_ids
        ):
            item["rejection_reason"] = "cited evidence_id does not exist"
            item["available_evidence_ids"] = sorted(durable_evidence_ids)
        if isinstance(proposal, NewBindingProposal):
            if item.get("rejection_reason") == "cited evidence_id does not exist":
                missing_probe = _missing_new_physical_column_probe(
                    proposal,
                    state,
                    loaded_schema=loaded_schema,
                    table_namespace=table_namespace,
                )
                if missing_probe is not None:
                    column, probe = missing_probe
                    candidates.append((column, item, probe))
            if (
                proposal.source_id not in source_ids
                and "rejection_reason" not in item
            ):
                item["rejection_reason"] = "source_id does not exist"
                item["available_source_ids"] = sorted(source_ids)
            if any(
                isinstance(reference, ExistingJoinRef)
                and reference.join_id not in joins
                for reference in proposal.join_references
            ) and "rejection_reason" not in item:
                item["rejection_reason"] = "referenced join_id does not exist"
            source_item = source_items.get(proposal.source_id)
            expected_column = (
                source_item.exact_physical_column_name
                if source_item is not None
                else None
            )
            if (
                "rejection_reason" not in item
                and expected_column is not None
                and isinstance(proposal.candidate, DiscriminatorValueCandidate)
                and proposal.candidate.discriminator_column.column
                != expected_column
            ):
                item["rejection_reason"] = (
                    "discriminator binding differs from exact physical column"
                )
                item["expected_exact_physical_column_name"] = expected_column
                item["required_revision"] = (
                    "replace discriminator_column.column and "
                    "discriminator_predicate.left.column with the expected exact "
                    "physical column name before resubmitting the proposal"
                )
            if (
                "rejection_reason" not in item
                and exact_column is not None
                and _new_binding_references_case_only_column(proposal, exact_column)
            ):
                item["rejection_reason"] = "logical column differs by case"
                item["exact_column"] = exact_column.model_dump(
                    mode="json", by_alias=True
                )
                if not any(
                    action.kind is ResearchActionKind.INSPECT_COLUMN
                    and action.target == exact_column
                    and action.parameters == ()
                    for action in state.action_history
                ):
                    candidates.append(
                        (
                            exact_column,
                            item,
                            {
                                "tool_name": "inspect_column",
                                "arguments": {
                                    "table": _logical_table_name(exact_column.table),
                                    "column": exact_column.column,
                                },
                            },
                        )
                    )
            if "rejection_reason" not in item and _calendar_year_equality_rejection(
                state,
                proposal,
                loaded_schema=loaded_schema,
                exact_formula_documents=exact_formula_documents,
            ):
                item["rejection_reason"] = (
                    "calendar component on full temporal column requires a range predicate"
                )
            if (
                "rejection_reason" not in item
                and proposal.proposal_key in missing_calendar_year_range
            ):
                item["rejection_reason"] = (
                    "trusted exact formula requires a calendar-year range on its only confirmed temporal input"
                )
            if "rejection_reason" not in item:
                missing_probe = _missing_new_discriminator_value_probe(
                    proposal,
                    state,
                    loaded_schema=loaded_schema,
                    table_namespace=table_namespace,
                )
                if missing_probe is not None:
                    column, probe = missing_probe
                    candidates.append((column, item, probe))
            rejected.append(item)
            continue
        if "rejection_reason" in item:
            rejected.append(item)
            continue
        if (
            isinstance(proposal, BindingAssessment)
            and isinstance(proposal.subject, ExistingBindingRef)
            and proposal.subject.binding_id not in bindings
        ):
            item["rejection_reason"] = "referenced binding_id does not exist"
            rejected.append(item)
            continue
        if (
            isinstance(proposal, BindingAssessment)
            and proposal.certificate == "contradicted"
            and not _categorical_in_recovery_replacement_feedback_certificate(
                bindings.get(proposal.subject.binding_id)
                if isinstance(proposal.subject, ExistingBindingRef)
                else None,
                proposal,
                decision,
                fresh_evidence,
            )
        ):
            item["rejection_reason"] = (
                "binding contradiction is not a permitted certificate"
            )
        if (
            isinstance(proposal, HypothesisAssessment)
            and proposal.certificate == "consistent"
            and isinstance(proposal.subject, ExistingHypothesisRef)
        ):
            hypothesis = hypotheses.get(proposal.subject.hypothesis_id)
            cited = tuple(
                record
                for record in fresh_evidence
                if record.evidence_id in proposal.citation_evidence_ids
            )
            if hypothesis is not None and not any(
                record.target in hypothesis.candidate_targets
                and (observation := parse_probe_observation(record.observation))
                is not None
                and isinstance(observation.payload, dict)
                and observation.payload.get("status") == "matched"
                for record in cited
            ):
                item["rejection_reason"] = (
                    "hypothesis consistency is not proven by cited evidence"
                )
        if (
            isinstance(proposal, HypothesisAssessment)
            and proposal.certificate == "contradicted"
            and isinstance(proposal.subject, ExistingHypothesisRef)
        ):
            hypothesis = hypotheses.get(proposal.subject.hypothesis_id)
            cited = tuple(
                record
                for record in fresh_evidence
                if record.evidence_id in proposal.citation_evidence_ids
            )
            if hypothesis is not None and not _negative_hypothesis_certificate(
                hypothesis, cited
            ):
                item["rejection_reason"] = (
                    "hypothesis contradiction is not proven by cited evidence"
                )
        if (
            proposal.certificate == "consistent"
            and isinstance(proposal.subject, ExistingBindingRef)
        ):
            binding = bindings.get(proposal.subject.binding_id)
            if isinstance(binding, PhysicalColumnBinding):
                try:
                    evidence_ids = sorted(
                        record.evidence_id
                        for record in fresh_evidence
                        if evidence_observes_exact_column(
                            record, binding.physical_column
                        )
                    )
                except ExactValueCertificateError:
                    evidence_ids = []
                if evidence_ids:
                    item["existing_evidence_id"] = evidence_ids[0]
                else:
                    missing_probe = _missing_binding_column_probe(
                        binding,
                        fresh_evidence,
                        state.action_history,
                        loaded_schema=loaded_schema,
                    )
                    if missing_probe is not None:
                        column, probe = missing_probe
                        candidates.append((column, item, probe))
            else:
                missing_probe = _missing_binding_column_probe(
                    binding,
                    fresh_evidence,
                    state.action_history,
                    loaded_schema=loaded_schema,
                )
                if missing_probe is not None:
                    column, probe = missing_probe
                    candidates.append((column, item, probe))
        elif (
            proposal.certificate == "consistent"
            and isinstance(proposal.subject, ExistingJoinRef)
        ):
            join = joins.get(proposal.subject.join_id)
            if (
                isinstance(join, JoinCandidate)
                and join.status is JoinCandidateStatus.CANDIDATE
                and len(join.path) == 1
            ):
                try:
                    evidence_ids = sorted(
                        record.evidence_id
                        for record in fresh_evidence
                        if _declared_join_certificate(join, record)
                    )
                except (TypeError, ValueError):
                    evidence_ids = []
                if evidence_ids:
                    item["existing_evidence_id"] = evidence_ids[0]
                else:
                    missing_probe = _missing_join_relationship_probe(
                        join, fresh_evidence, state.action_history
                    )
                    if missing_probe is not None:
                        table, probe = missing_probe
                        join_candidates.append((table, item, probe))
        rejected.append(item)
    if candidates:
        ordered = sorted(
            candidates,
            key=lambda item: (
                item[0].table.namespace,
                item[0].table.schema_name or "",
                item[0].table.table,
                item[0].column,
                canonical_digest(item[1]["proposal"]),
            ),
        )
        selected = ordered[0]
        if requested_action is not None:
            for column, item, probe in ordered:
                if (
                    requested_action.kind is ResearchActionKind.INSPECT_COLUMN
                    and requested_action.target == column
                    and requested_action.parameters == ()
                ):
                    selected = (column, item, probe)
                    break
        selected[1]["missing_probe"] = selected[2]
    elif join_candidates:
        selected = sorted(
            join_candidates,
            key=lambda item: (
                item[0].namespace,
                item[0].schema_name or "",
                item[0].table,
                canonical_digest(item[1]["proposal"]),
            ),
        )[0]
        selected[1]["missing_probe"] = selected[2]
    proposal_feedback = tuple(
        sorted(rejected, key=lambda item: canonical_digest(item["proposal"]))
    )
    return (
        proposal_feedback
        if rejected_tool is None
        else (*proposal_feedback, rejected_tool)
    )


def _categorical_in_recovery_replacement_feedback_certificate(
    binding: object,
    assessment: BindingAssessment,
    decision: ResearchDecisionV1,
    fresh_evidence: tuple[EvidenceRecord, ...],
) -> bool:
    """Project the proposed replacement into the reducer's closed certificate."""

    if not isinstance(binding, DiscriminatorValueBinding):
        return False
    logical_column = LogicalColumnRef(
        table=_logical_table_name(binding.discriminator_column.table),
        column=binding.discriminator_column.column,
    )
    replacements: list[DiscriminatorValueBinding] = []
    for proposal in decision.proposals:
        if not (
            isinstance(proposal, NewBindingProposal)
            and isinstance(proposal.candidate, DiscriminatorValueCandidate)
            and proposal.candidate.discriminator_column == logical_column
            and proposal.candidate.discriminator_predicate.left == logical_column
        ):
            continue
        predicate = PredicateRef(
            left=binding.discriminator_column,
            operator=proposal.candidate.discriminator_predicate.operator,
            right=proposal.candidate.discriminator_predicate.right,
        )
        replacements.append(
            binding.model_copy(
                update={
                    "source_id": proposal.source_id,
                    "status": BindingStatus.CANDIDATE,
                    "evidence_ids": proposal.citation_evidence_ids,
                    "discriminator_predicate": predicate,
                    "predicates": (predicate,),
                }
            )
        )
    cited = tuple(
        record
        for record in fresh_evidence
        if record.evidence_id in assessment.citation_evidence_ids
    )
    return _categorical_in_recovery_replacement_certificate(
        binding,
        cited,
        tuple(replacements),
    )


def _with_partial_selected_candidate_commit_feedback(
    rejected: tuple[dict[str, object], ...],
    gaps: tuple[object, ...],
) -> tuple[dict[str, object], ...]:
    """Explain the selected candidate assessments required for a commit."""

    gap_by_binding_id: dict[str, object] = {}
    for gap in gaps:
        required_ids = getattr(gap, "required_binding_assessment_ids", ())
        if not isinstance(required_ids, tuple) or not all(
            isinstance(binding_id, str) for binding_id in required_ids
        ):
            continue
        for binding_id in required_ids:
            gap_by_binding_id[binding_id] = gap

    feedback: list[dict[str, object]] = []
    for item in rejected:
        proposal = item.get("proposal")
        subject = proposal.get("subject") if isinstance(proposal, dict) else None
        binding_id = (
            subject.get("binding_id") if isinstance(subject, dict) else None
        )
        if (
            not isinstance(proposal, dict)
            or proposal.get("proposal_type") != "binding_assessment"
            or proposal.get("certificate") != "consistent"
            or not isinstance(binding_id, str)
            or (gap := gap_by_binding_id.get(binding_id)) is None
        ):
            feedback.append(item)
            continue
        feedback.append(
            {
                **item,
                "rejection_reason": (
                    "semantic commit leaves selected candidate bindings unassessed"
                ),
                "source_id": getattr(gap, "source_id"),
                "required_binding_assessment_ids": list(
                    getattr(gap, "required_binding_assessment_ids")
                ),
                "unassessed_binding_ids": list(
                    getattr(gap, "unassessed_binding_ids")
                ),
            }
        )
    return tuple(feedback)


def _new_binding_references_case_only_column(
    proposal: NewBindingProposal,
    exact_column: ColumnRef,
) -> bool:
    """Match the rejected typed column reference without rewriting it."""

    table = exact_column.table
    names = {table.table}
    if table.schema_name is not None:
        names.add(f"{table.schema_name}.{table.table}")

    def contains(value: object) -> bool:
        if isinstance(value, dict):
            logical_table = value.get("table")
            logical_column = value.get("column")
            if (
                logical_table in names
                and type(logical_column) is str
                and logical_column != exact_column.column
                and logical_column.casefold() == exact_column.column.casefold()
            ):
                return True
            return any(contains(item) for item in value.values())
        if isinstance(value, tuple):
            return any(contains(item) for item in value)
        return False

    return contains(proposal.candidate.model_dump(mode="python"))


def _logical_table_name(table: TableRef) -> str:
    return f"{table.schema_name}.{table.table}" if table.schema_name else table.table


def _missing_new_physical_column_probe(
    proposal: NewBindingProposal,
    state: ResearchState,
    *,
    loaded_schema: LoadedSchema | None,
    table_namespace: str | None,
) -> tuple[ColumnRef, dict[str, object]] | None:
    if (
        not isinstance(proposal.candidate, PhysicalColumnCandidate)
    ):
        return None
    column = _exact_loaded_schema_column(
        proposal.candidate.physical_column,
        state,
        loaded_schema=loaded_schema,
        table_namespace=table_namespace,
    )
    if column is None:
        return None
    try:
        if any(
            evidence_observes_exact_column(evidence, column)
            for evidence in state.evidence
        ):
            return None
    except ExactValueCertificateError:
        return None
    if any(
        action.kind is ResearchActionKind.INSPECT_COLUMN
        and action.target == column
        and action.parameters == ()
        for action in state.action_history
    ):
        return None
    return (
        column,
        {
            "tool_name": "inspect_column",
            "arguments": {
                "table": _logical_table_name(column.table),
                "column": column.column,
            },
        },
    )


def _missing_new_discriminator_value_probe(
    proposal: NewBindingProposal,
    state: ResearchState,
    *,
    loaded_schema: LoadedSchema | None,
    table_namespace: str | None,
) -> tuple[ColumnRef, dict[str, object]] | None:
    """Return the missing exact value search for one known new discriminator."""

    candidate = proposal.candidate
    if (
        not isinstance(candidate, DiscriminatorValueCandidate)
        or candidate.discriminator_predicate.left != candidate.discriminator_column
    ):
        return None
    column = _exact_loaded_schema_column(
        candidate.discriminator_column,
        state,
        loaded_schema=loaded_schema,
        table_namespace=table_namespace,
    )
    if column is None:
        return None
    predicate = {
        "left": column,
        "operator": candidate.discriminator_predicate.operator,
        "right": candidate.discriminator_predicate.right,
    }
    binding = DiscriminatorValueBinding(
        binding_id=proposal.proposal_key,
        source_id=proposal.source_id,
        tables=(column.table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=(),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=column,
        discriminator_predicate=predicate,
    )
    return _missing_binding_column_probe(
        binding,
        state.evidence,
        state.action_history,
        loaded_schema=loaded_schema,
    )


def _exact_loaded_schema_column(
    logical: LogicalColumnRef,
    state: ResearchState,
    *,
    loaded_schema: LoadedSchema | None,
    table_namespace: str | None,
) -> ColumnRef | None:
    """Resolve one exact logical column from the captured schema."""

    if not isinstance(loaded_schema, LoadedSchema):
        return None
    schema = loaded_schema.schema
    table_names = tuple(name for name in schema if type(name) is str)
    if "." in logical.table:
        exact_tables = tuple(name for name in table_names if name == logical.table)
        folded_tables = tuple(
            name for name in table_names if name.casefold() == logical.table.casefold()
        )
    else:
        exact_tables = tuple(
            name for name in table_names if name.rsplit(".", 1)[-1] == logical.table
        )
        folded_tables = tuple(
            name
            for name in table_names
            if name.rsplit(".", 1)[-1].casefold() == logical.table.casefold()
        )
    if len(exact_tables) != 1 or len(folded_tables) != 1:
        return None
    table_name = exact_tables[0]
    table_body = schema.get(table_name)
    if not isinstance(table_body, Mapping):
        return None
    try:
        columns = get_table_columns(table_body)
    except (TypeError, ValueError):
        return None
    exact_columns = tuple(name for name in columns if name == logical.column)
    folded_columns = tuple(
        name
        for name in columns
        if type(name) is str and name.casefold() == logical.column.casefold()
    )
    if len(exact_columns) != 1 or len(folded_columns) != 1:
        return None
    if table_namespace is None:
        namespaces = {
            target.table.namespace if isinstance(target, ColumnRef) else target.namespace
            for evidence in state.evidence
            if isinstance((target := evidence.target), (ColumnRef, TableRef))
        }
        if len(namespaces) != 1:
            return None
        table_namespace = next(iter(namespaces))
    schema_name, has_schema, physical_table = table_name.rpartition(".")
    column = ColumnRef(
        table=TableRef(
            namespace=table_namespace,
            schema=schema_name if has_schema else None,
            table=physical_table if has_schema else table_name,
        ),
        column=exact_columns[0],
    )
    return column


def _missing_binding_column_probe(
    binding: object,
    evidence: tuple[EvidenceRecord, ...],
    action_history: tuple[ResearchAction, ...],
    *,
    loaded_schema: LoadedSchema | None = None,
) -> tuple[ColumnRef, dict[str, object]] | None:
    """Return one exact inspection only when it is provably the missing fact."""

    if isinstance(binding, PhysicalColumnBinding):
        columns = (binding.physical_column,)
    elif isinstance(binding, DiscriminatorValueBinding):
        columns = binding.columns
    elif isinstance(binding, DerivedExpressionBinding):
        columns = binding.input_columns
    else:
        return None
    def column_is_known(column: ColumnRef) -> bool:
        return any(
            evidence_observes_exact_column(record, column) for record in evidence
        ) or _loaded_schema_observes_exact_column(loaded_schema, column)

    try:
        missing = next(
            (
                column
                for column in sorted(
                    columns,
                    key=lambda item: (
                        item.table.namespace,
                        item.table.schema_name or "",
                        item.table.table,
                        item.column,
                    ),
                )
                if not column_is_known(column)
                and not any(
                    action.kind is ResearchActionKind.INSPECT_COLUMN
                    and action.target == column
                    and action.parameters == ()
                    for action in action_history
                )
            ),
            None,
        )
    except ExactValueCertificateError:
        return None
    if missing is not None:
        table = missing.table
        logical_table = (
            f"{table.schema_name}.{table.table}"
            if table.schema_name is not None
            else table.table
        )
        return (
            missing,
            {
                "tool_name": "inspect_column",
                "arguments": {"table": logical_table, "column": missing.column},
            },
        )
    if not isinstance(binding, DiscriminatorValueBinding):
        return None
    try:
        for predicate in binding.predicates:
            column = predicate.left
            if not column_is_known(column):
                return None
            if predicate.operator is PredicateOperator.EQ:
                values = (predicate.right,)
            elif predicate.operator is PredicateOperator.IN and isinstance(
                predicate.right, tuple
            ):
                values = predicate.right
            elif predicate.operator is PredicateOperator.IS_NULL:
                values = (None,)
            else:
                continue
            for value in values:
                exact_value = value.value if type(value) is LiteralValue else value
                if type(exact_value) not in {str, int, float, bool, type(None)}:
                    return None
                if type(exact_value) is float and not math.isfinite(exact_value):
                    return None
                if not any(
                    evidence_observes_exact_value(record, column, exact_value)
                    for record in evidence
                ) and not any(
                    action.kind is ResearchActionKind.SEARCH_VALUE
                    and action.target == column
                    and any(
                        name == "value"
                        and type(attempted_value) is type(exact_value)
                        and attempted_value == exact_value
                        for name, attempted_value in action.parameters
                    )
                    for action in action_history
                ):
                    table = column.table
                    logical_table = (
                        f"{table.schema_name}.{table.table}"
                        if table.schema_name is not None
                        else table.table
                    )
                    return (
                        column,
                        {
                            "tool_name": "search_value",
                            "arguments": {
                                "table": logical_table,
                                "column": column.column,
                                "value": exact_value,
                                "top_k": 1,
                            },
                        },
                    )
    except ExactValueCertificateError:
        return None
    return None


def _loaded_schema_observes_exact_column(
    loaded_schema: LoadedSchema | None,
    column: ColumnRef,
) -> bool:
    """Return whether the captured schema proves this exact physical column."""

    if not isinstance(loaded_schema, LoadedSchema):
        return False
    table_body = loaded_schema.schema.get(_logical_table_name(column.table))
    if not isinstance(table_body, Mapping):
        return False
    try:
        columns = get_table_columns(table_body)
    except (TypeError, ValueError):
        return False
    return column.column in columns


def _schema_table_name(column: object) -> str | None:
    table = getattr(column, "table", None)
    if isinstance(table, str):
        return table
    return _logical_table_name(table) if isinstance(table, TableRef) else None


def _calendar_year_equality_rejection(
    state: ResearchState,
    proposal: NewBindingProposal,
    *,
    loaded_schema: LoadedSchema | None,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...],
) -> bool:
    """Recognize a rejected year component used as a full temporal value."""

    if (
        not isinstance(proposal.candidate, DiscriminatorValueCandidate)
        or proposal.candidate.discriminator_predicate.operator is not PredicateOperator.EQ
        or proposal.candidate.discriminator_predicate.left
        != proposal.candidate.discriminator_column
        or type(proposal.candidate.discriminator_predicate.right) is not int
        or proposal.source_id not in dict(exact_formula_documents)
    ):
        return False
    column = proposal.candidate.discriminator_column
    if not _loaded_schema_declares_full_temporal_column(loaded_schema, column):
        return False
    item = next(
        (item for item in state.query_spec.semantic_items if item.source_id == proposal.source_id),
        None,
    )
    formula = item.normalized_meaning if item is not None else None
    if not isinstance(formula, str):
        return False
    return any(
        match.group(1).casefold() == column.column.casefold()
        and int(match.group(2)) == proposal.candidate.discriminator_predicate.right
        for match in re.finditer(
            r"\bYEAR\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*=\s*(-?\d+)(?=[^A-Za-z0-9_]|$)",
            formula,
            re.IGNORECASE,
        )
    )


def _loaded_schema_declares_full_temporal_column(
    loaded_schema: LoadedSchema | None,
    column: object,
) -> bool:
    table_name = _schema_table_name(column)
    column_name = getattr(column, "column", None)
    if (
        not isinstance(loaded_schema, LoadedSchema)
        or table_name is None
        or not isinstance(column_name, str)
    ):
        return False
    table = loaded_schema.schema.get(table_name)
    columns = table.get("columns") if isinstance(table, Mapping) else None
    metadata = columns.get(column_name) if isinstance(columns, Mapping) else None
    return (
        isinstance(metadata, Mapping)
        and isinstance(declared_type := metadata.get("type"), str)
        and declared_type.strip().upper() in {"DATE", "DATETIME", "TIMESTAMP"}
    )


def _missing_calendar_year_range_derived_proposal_keys(
    state: ResearchState,
    decision: ResearchDecisionV1,
    *,
    loaded_schema: LoadedSchema | None,
    exact_formula_documents: tuple[tuple[str, DocumentRef], ...],
) -> frozenset[str]:
    """Find one exact formula proposal that omitted its only temporal range."""

    documents = dict(exact_formula_documents)
    source_items = {
        item.source_id: item for item in state.query_spec.semantic_items
    }
    derived_by_source: dict[str, list[NewBindingProposal]] = {}
    for proposal in decision.proposals:
        if (
            isinstance(proposal, NewBindingProposal)
            and isinstance(proposal.candidate, DerivedExpressionCandidate)
            and proposal.source_id in documents
            and proposal.candidate.document_id == documents[proposal.source_id].document_id
        ):
            derived_by_source.setdefault(proposal.source_id, []).append(proposal)

    keys: set[str] = set()
    for source_id, proposals in derived_by_source.items():
        if len(proposals) != 1:
            continue
        item = source_items.get(source_id)
        if (
            item is None
            or item.kind is not SemanticItemKind.FORMULA
            or not item.required
        ):
            continue
        formula = _formula_part(item.normalized_meaning)
        derived = proposals[0].candidate
        if (
            formula is None
            or _formula_part(derived.expression_claim) != formula
            or len(
                year_matches := tuple(
                    re.finditer(
                        r"YEAR\([A-Za-z_][A-Za-z0-9_]*\)=(-?\d+)(?=(?:AND|OR)(?=[A-Za-z_])|[^A-Za-z0-9_]|$)",
                        formula,
                        re.IGNORECASE,
                    )
                )
            )
            != 1
        ):
            continue
        temporal_inputs = tuple(
            column
            for column in derived.input_columns
            if _loaded_schema_declares_full_temporal_column(loaded_schema, column)
        )
        if len(temporal_inputs) != 1:
            continue
        temporal_column = temporal_inputs[0]
        year = int(year_matches[0].group(1))
        if _has_required_time_calendar_year_range(
            state,
            source_id,
            temporal_column,
            year,
        ):
            continue
        if any(
            isinstance(other, NewBindingProposal)
            and other.source_id == source_id
            and isinstance(other.candidate, DiscriminatorValueCandidate)
            and _same_schema_column(
                other.candidate.discriminator_column, temporal_column
            )
            and _has_calendar_year_range(
                (
                    other.candidate.discriminator_predicate,
                    *other.candidate.additional_predicates,
                ),
                temporal_column,
                year,
            )
            for other in decision.proposals
        ):
            continue
        keys.add(proposals[0].proposal_key)
    return frozenset(keys)


def _has_calendar_year_range(
    predicates: tuple[object, ...],
    column: object,
    year: int,
) -> bool:
    """Return whether one candidate contains the exact half-open year range."""

    return any(
        _same_schema_column(getattr(predicate, "left", None), column)
        and getattr(predicate, "operator", None) is PredicateOperator.GTE
        and getattr(predicate, "right", None) == f"{year:04d}-01-01"
        for predicate in predicates
    ) and any(
        _same_schema_column(getattr(predicate, "left", None), column)
        and getattr(predicate, "operator", None) is PredicateOperator.LT
        and getattr(predicate, "right", None) == f"{year + 1:04d}-01-01"
        for predicate in predicates
    )


def _has_required_time_calendar_year_range(
    state: ResearchState,
    formula_source_id: str,
    column: object,
    year: int,
) -> bool:
    """Return whether a separate required time item already owns this range."""

    bindings = {binding.binding_id: binding for binding in state.bindings}
    return any(
        item.source_id != formula_source_id
        and item.required
        and item.kind is SemanticItemKind.TIME
        and isinstance(binding := bindings.get(binding_id), DiscriminatorValueBinding)
        and binding.source_id == item.source_id
        and binding.status in {BindingStatus.CANDIDATE, BindingStatus.SUPPORTED}
        and _same_schema_column(binding.discriminator_column, column)
        and _has_calendar_year_range(binding.predicates, column, year)
        for item in state.query_spec.semantic_items
        for binding_id in item.binding_ids
    )


def _same_schema_column(left: object, right: object) -> bool:
    return (
        getattr(left, "column", None) == getattr(right, "column", None)
        and _schema_table_name(left) == _schema_table_name(right)
    )


def _missing_join_relationship_probe(
    join: object,
    evidence: tuple[EvidenceRecord, ...],
    action_history: tuple[ResearchAction, ...],
) -> tuple[TableRef, dict[str, object]] | None:
    """Return one direct relationship inspection only when it is missing."""

    if (
        not isinstance(join, JoinCandidate)
        or join.status is not JoinCandidateStatus.CANDIDATE
        or len(join.path) != 1
    ):
        return None
    table = min(
        (join.left.table, join.right.table),
        key=lambda item: (item.namespace, item.schema_name or "", item.table),
    )
    parameters = (("depth", 1), ("top_k", 50))
    try:
        if any(_declared_join_certificate(join, record) for record in evidence):
            return None
    except (TypeError, ValueError):
        return None
    if any(
        action.kind is ResearchActionKind.INSPECT_RELATIONSHIPS
        and action.target == table
        and action.parameters == parameters
        for action in action_history
    ):
        return None
    logical_table = (
        f"{table.schema_name}.{table.table}"
        if table.schema_name is not None
        else table.table
    )
    return (
        table,
        {
            "tool_name": "inspect_relationships",
            "arguments": {"table": logical_table, "top_k": 50, "depth": 1},
        },
    )


def _planned_action(resolved: ResolvedResearchDecision) -> dict[str, object]:
    action = resolved.admission.action
    invocation = resolved.invocation
    if action is None:
        raise ValueError("planned schema research decision must contain one action")
    if action.kind is not ResearchActionKind.SEMANTIC_COMMIT and invocation is None:
        raise ValueError("planned probe decision must contain one invocation")
    return {
        "action": action.model_dump(mode="json", by_alias=True),
        "contract_version": 1,
        "decision": resolved.decision.model_dump(mode="json", by_alias=True),
        "invocation_id": None if invocation is None else invocation.invocation_id,
        "kind": "research_planned",
        "resolution_digest": resolved.resolution_digest,
        "state_digest": resolved.state_digest,
    }


def _planned_envelope(action: object) -> dict[str, object]:
    if not isinstance(action, dict) or set(action) != {
        "action",
        "contract_version",
        "decision",
        "invocation_id",
        "kind",
        "resolution_digest",
        "state_digest",
    }:
        raise ValueError("planned envelope has an invalid shape")
    if action["contract_version"] != 1 or action["kind"] != "research_planned":
        raise ValueError("planned envelope has an invalid contract")
    decision = deserialize_as(
        canonical_json_bytes(action["decision"]), ResearchDecisionV1
    )
    if not isinstance(action["action"], dict) or action["invocation_id"] is not None and not isinstance(action["invocation_id"], str):
        raise ValueError("planned envelope has invalid action identity")
    if not isinstance(action["resolution_digest"], str):
        raise ValueError("planned envelope has invalid resolution identity")
    if not isinstance(action["state_digest"], str):
        raise ValueError("planned envelope has invalid state identity")
    return {**action, "decision": decision}


def _stable_planned_identity(action: dict[str, object]) -> dict[str, object]:
    decision = action["decision"]
    if isinstance(decision, ResearchDecisionV1):
        decision = decision.model_dump(mode="json", by_alias=True)
    return {
        "action": action["action"],
        "decision": decision,
        "invocation_id": action["invocation_id"],
        "state_digest": action["state_digest"],
    }


def _probe_from_observed(action: object) -> ProbeResult | None:
    if not isinstance(action, dict) or set(action) != {
        "contract_version",
        "kind",
        "novel",
        "result",
        "resolution_digest",
    }:
        return None
    if action["contract_version"] != 1 or action["kind"] != "research_observed":
        return None
    if type(action["novel"]) is not bool or not isinstance(
        action["resolution_digest"], str
    ):
        return None
    if action["result"] is None:
        return None
    try:
        return deserialize_probe_result(canonical_json_bytes(action["result"]))
    except (TypeError, ValueError):
        return None


def _is_semantic_observed(action: object) -> bool:
    return bool(
        isinstance(action, dict)
        and set(action)
        == {
            "contract_version",
            "kind",
            "novel",
            "result",
            "resolution_digest",
        }
        and action["contract_version"] == 1
        and action["kind"] == "research_observed"
        and type(action["novel"]) is bool
        and action["result"] is None
        and isinstance(action["resolution_digest"], str)
    )


def _replay_input_has_failed_probe(replay_input: object) -> bool:
    probe_result = getattr(replay_input, "probe_result", None)
    return bool(
        isinstance(probe_result, ProbeResult)
        and probe_result.status is ProbeStatus.FAILED
    )


def _reconciled_record_for_action(records: tuple[object, ...], action: object):
    matches = tuple(
        record
        for record in records
        if getattr(getattr(record, "reservation", None), "revision", None)
        == getattr(action, "expected_revision", None)
        and getattr(getattr(record, "reservation", None), "action_digest", None)
        == getattr(action, "action_digest", None)
    )
    if len(matches) != 1 or getattr(matches[0], "reconciliation", None) is None:
        raise ValueError("probe ledger does not reconcile the admitted action")
    return matches[0]


def _probe_matches_resolution(
    result: ProbeResult,
    resolved: ResolvedResearchDecision,
) -> bool:
    action = resolved.admission.action
    invocation = resolved.invocation
    state = resolved.admission.state
    return bool(
        action is not None
        and invocation is not None
        and result.run_id == state.run_id
        and result.run_incarnation == state.run_incarnation
        and result.revision == action.expected_revision
        and result.schema_namespace_version == state.schema_namespace_version
        and result.invocation_id == invocation.invocation_id
        and result.action_digest == action.action_digest
        and result.probe_kind is action.kind
        and result.target == action.target
    )


def _probe_failure_reason(result: ProbeResult) -> ResearchStopReason | None:
    if result.status is ProbeStatus.SUCCESS:
        return None
    if result.status is ProbeStatus.FAILED:
        return ResearchStopReason.TOOL_FAILURE
    if result.status is ProbeStatus.TIMED_OUT:
        return ResearchStopReason.DEADLINE_EXCEEDED
    if result.status is ProbeStatus.CANCELLED:
        return ResearchStopReason.CANCELLED
    return ResearchStopReason.PROTOCOL_FAILURE


def _abort_reason(action: object) -> ResearchStopReason | None:
    if not isinstance(action, dict) or set(action) != {
        "action",
        "contract_version",
        "kind",
        "reason",
        "resolution_digest",
    }:
        return None
    if action["contract_version"] != 1 or action["kind"] != "research_aborted":
        return None
    try:
        return ResearchStopReason(action["reason"])
    except (TypeError, ValueError):
        return ResearchStopReason.PROTOCOL_FAILURE


def _consecutive_non_novel(store: AdaptiveStateStore, state: ResearchState) -> int:
    count = 0
    for revision in range(state.revision - 1, max(-1, state.revision - 4), -1):
        try:
            observed = store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id,
                    state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    revision,
                )
            ).observed
        except AdaptiveCheckpointError:
            return 3
        if observed is None or not isinstance(observed.action, dict):
            return 0
        if observed.action.get("novel") is True:
            return 0
        count += 1
    return count


def _is_semantically_novel_turn(
    state: ResearchState, committed: SemanticCommitResult
) -> bool:
    """Ignore append-only evidence IDs when judging whether research progressed."""

    novelty = committed.novelty
    next_state = committed.state
    if state.unresolved_items or next_state.unresolved_items:
        if any(
            (
                novelty.updated_hypothesis_ids,
                novelty.added_binding_ids,
                novelty.updated_binding_ids,
                novelty.added_join_ids,
                novelty.updated_join_ids,
            )
        ):
            return True
        if novelty.added_hypothesis_ids:
            previous_scopes = {
                canonical_digest(
                    {
                        "candidate_targets": hypothesis.candidate_targets,
                        "source_ids": hypothesis.source_ids,
                    }
                )
                for hypothesis in state.hypotheses
                if hypothesis.candidate_targets
            }
            added_ids = set(novelty.added_hypothesis_ids)
            if any(
                not hypothesis.candidate_targets
                or canonical_digest(
                    {
                        "candidate_targets": hypothesis.candidate_targets,
                        "source_ids": hypothesis.source_ids,
                    }
                )
                not in previous_scopes
                for hypothesis in next_state.hypotheses
                if hypothesis.hypothesis_id in added_ids
            ):
                return True
        if (
            novelty.unresolved_items != state.unresolved_items
            or novelty.stop_reason != state.stop_reason
        ):
            return True
    return _has_novel_relevant_value_evidence(state, next_state)


def _has_novel_relevant_value_evidence(
    state: ResearchState, next_state: ResearchState
) -> bool:
    existing_ids = {record.evidence_id for record in state.evidence}
    added = tuple(
        record
        for record in next_state.evidence
        if record.evidence_id not in existing_ids
    )
    selected_binding_ids = {
        binding_id
        for item in state.query_spec.semantic_items
        if item.required
        for binding_id in item.binding_ids
    }
    relevant_columns = {
        column
        for binding in state.bindings
        if (
            binding.binding_id in selected_binding_ids
            and binding.status
            in {BindingStatus.CANDIDATE, BindingStatus.SUPPORTED}
        )
        for column in binding.columns
    } | {
        constraint.left
        for constraint in state.query_spec.global_constraints
        if isinstance(constraint.left, ColumnRef)
    } | {
        constraint.right
        for constraint in state.query_spec.global_constraints
        if isinstance(constraint.right, ColumnRef)
    }
    unbound_exact_column_names = {
        item.exact_physical_column_name
        for item in state.query_spec.semantic_items
        if (
            item.required
            and item.source_id in state.unresolved_items
            and item.kind in {SemanticItemKind.FILTER, SemanticItemKind.TIME}
            and item.exact_physical_predicate
            and item.exact_physical_column_name is not None
            and not any(
                binding.source_id == item.source_id for binding in state.bindings
            )
        )
    }
    unbound_exact_predicate_values = {
        (item.exact_physical_column_name, item.literal_or_reference)
        for item in state.query_spec.semantic_items
        if (
            item.required
            and item.source_id in state.unresolved_items
            and item.kind in {SemanticItemKind.FILTER, SemanticItemKind.TIME}
            and item.exact_physical_predicate
            and item.exact_physical_column_name is not None
            and not any(
                binding.source_id == item.source_id for binding in state.bindings
            )
        )
    }
    relevant_columns |= {
        record.target
        for record in added
        if (
            isinstance(record.target, ColumnRef)
            and record.target.column in unbound_exact_column_names
        )
    }
    for column in relevant_columns:
        for record in added:
            if (
                (
                    values := _qualifying_full_distinct_values(
                        next_state, record, column, next_state.action_history
                    )
                )
                is not None
            ):
                if any(
                    not any(
                        _distinct_values_include(
                            _qualifying_full_distinct_values(
                                state, previous, column, state.action_history
                            ),
                            value,
                        )
                        for previous in state.evidence
                    )
                    for value in values
                ):
                    return True
            value = _positive_exact_search_value(
                next_state, record, column, next_state.action_history
            )
            if value is not None and (
                (
                    any(
                        _distinct_values_include(
                            _qualifying_full_distinct_values(
                                state, previous, column, state.action_history
                            ),
                            value,
                        )
                        for previous in state.evidence
                        if _qualifying_full_distinct_values(
                            state, previous, column, state.action_history
                        )
                        is not None
                    )
                    and not _has_prior_qualifying_exact_search(
                        state, state.evidence, state.action_history, column, value
                    )
                )
                or (
                    any(
                        exact_column_name == column.column
                        and type(exact_value) is type(value)
                        and exact_value == value
                        for exact_column_name, exact_value in unbound_exact_predicate_values
                    )
                    and not _has_prior_qualifying_exact_search(
                        state,
                        state.evidence,
                        state.action_history,
                        column,
                        value,
                        require_prior_distinct=False,
                    )
                )
            ):
                return True
    return False


def _has_prior_qualifying_exact_search(
    state: ResearchState,
    evidence: tuple[EvidenceRecord, ...],
    action_history: tuple[ResearchAction, ...],
    column: ColumnRef,
    value: object,
    *,
    require_prior_distinct: bool = True,
) -> bool:
    for index, record in enumerate(evidence):
        previous_value = _positive_exact_search_value(
            state, record, column, action_history
        )
        if type(previous_value) is not type(value) or previous_value != value:
            continue
        if not require_prior_distinct or any(
            _distinct_values_include(
                _qualifying_full_distinct_values(
                    state, previous, column, action_history
                ),
                value,
            )
            for previous in evidence[:index]
            if _qualifying_full_distinct_values(
                state, previous, column, action_history
            )
            is not None
        ):
            return True
    return False


def _qualifying_full_distinct_values(
    state: ResearchState,
    evidence: EvidenceRecord,
    column: ColumnRef,
    action_history: tuple[ResearchAction, ...],
) -> tuple[object, ...] | None:
    observation = _value_evidence_observation(state, evidence, column)
    if (
        observation is None
        or observation.provenance.probe_kind is not ResearchActionKind.DISTINCT_VALUES
    ):
        return None
    payload = observation.payload
    if (
        not isinstance(payload, dict)
        or payload.get("columns") != [column.column]
        or not isinstance(payload.get("rows"), list)
        or not payload["rows"]
        or observation.row_count != len(payload["rows"])
        or not all(
            isinstance(row, list)
            and len(row) == 1
            and _is_closed_value(row[0])
            for row in payload["rows"]
        )
        or not any(
            action.action_digest == evidence.action_digest
            and action.kind is ResearchActionKind.DISTINCT_VALUES
            and action.target == column
            and len(action.parameters) == 1
            and action.parameters[0][0] == "top_k"
            and type(action.parameters[0][1]) is int
            and action.parameters[0][1] > 0
            for action in action_history
        )
    ):
        return None
    return tuple(row[0] for row in payload["rows"])


def _distinct_values_include(values: tuple[object, ...] | None, value: object) -> bool:
    return values is not None and any(
        type(candidate) is type(value) and candidate == value for candidate in values
    )


def _positive_exact_search_value(
    state: ResearchState,
    evidence: EvidenceRecord,
    column: ColumnRef,
    action_history: tuple[ResearchAction, ...],
) -> object | None:
    observation = _value_evidence_observation(state, evidence, column)
    if (
        observation is None
        or observation.provenance.probe_kind is not ResearchActionKind.SEARCH_VALUE
        or not isinstance(observation.payload, dict)
    ):
        return None
    payload = observation.payload
    value = payload.get("requested_value")
    if (
        not _is_closed_value(value)
        or payload.get("columns") != [column.column]
        or not isinstance(payload.get("rows"), list)
        or not payload["rows"]
        or observation.row_count != len(payload["rows"])
        or not any(
            isinstance(row, list)
            and len(row) == 1
            and type(row[0]) is type(value)
            and row[0] == value
            for row in payload["rows"]
        )
        or not any(
            action.action_digest == evidence.action_digest
            and action.kind is ResearchActionKind.SEARCH_VALUE
            and action.target == column
            and any(
                name == "value"
                and type(action_value) is type(value)
                and action_value == value
                for name, action_value in action.parameters
            )
            for action in action_history
        )
    ):
        return None
    return value


def _value_evidence_observation(
    state: ResearchState, evidence: EvidenceRecord, column: ColumnRef
):
    if evidence.target != column or not evidence_has_state_authority(evidence, state):
        return None
    try:
        observation = parse_probe_observation(evidence.observation)
    except MalformedProvenanceError:
        return None
    if (
        observation is None
        or observation.storage != "inline"
        or observation.truncated
        or observation.provenance.action_digest != evidence.action_digest
        or observation.provenance.target != evidence.target
    ):
        return None
    return observation


def _is_closed_value(value: object) -> bool:
    return type(value) in {str, int, float, bool} and (
        type(value) is not float or math.isfinite(value)
    )


def _completeness_reason(
    state: ResearchState, context: FreshnessContext
) -> ResearchStopReason | None:
    return _authority_stop_reason(
        evaluate_research_generation_authority(
            state, context, state.run_id, state.run_incarnation
        )
    )


def _model_stop_reason(decision: ResearchDecisionV1) -> ResearchStopReason:
    reason = decision.next.reason
    return {
        "complete": ResearchStopReason.COMPLETE,
        "ambiguous": ResearchStopReason.AMBIGUOUS,
        "unsupported": ResearchStopReason.UNSUPPORTED,
    }[reason]


def _proposal_free_tool_baseline(
    decision: ResearchDecisionV1,
) -> ResearchDecisionV1 | None:
    if not isinstance(decision.next, ToolIntent) or not decision.proposals:
        return None
    next_request = decision.next
    if isinstance(decision.next.hypothesis_ref, ProposedHypothesisRef):
        next_request = decision.next.model_copy(update={"hypothesis_ref": None})
    return decision.model_copy(update={"proposals": (), "next": next_request})


def _cap_execute_research_probe_limit(
    decision: ResearchDecisionV1,
    *,
    maximum_row_limit: int,
    dialect: str,
) -> ResearchDecisionV1:
    if (
        type(maximum_row_limit) is not int
        or maximum_row_limit <= 0
        or not isinstance(decision.next, ToolIntent)
        or not isinstance(decision.next.intent, ExecuteResearchProbeIntent)
    ):
        return decision
    arguments = decision.next.intent.arguments
    try:
        statements = parse(arguments.sql, read=dialect)
    except (ParseError, TokenError, ValueError):
        return decision
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return decision
    tree = statements[0]
    selections = tuple(tree.find_all(exp.Select))
    limited_scopes: tuple[exp.Expression, ...] = selections + tuple(
        tree.find_all(exp.SetOperation)
    )
    if not selections or any(
        scope.args.get("offset") is not None for scope in limited_scopes
    ):
        return decision
    limits: list[tuple[exp.Limit, int]] = []
    for scope in limited_scopes:
        limit = scope.args.get("limit")
        expression = limit.expression if isinstance(limit, exp.Limit) else None
        if limit is None:
            if scope is tree:
                return decision
            continue
        if not isinstance(expression, exp.Literal) or expression.is_string:
            return decision
        try:
            value = int(expression.this)
        except (TypeError, ValueError):
            return decision
        if str(value) != expression.this or value <= 0:
            return decision
        limits.append((limit, value))
    if not any(value > maximum_row_limit for _, value in limits):
        return decision
    for limit, value in limits:
        if value > maximum_row_limit:
            limit.set("expression", exp.Literal.number(maximum_row_limit))
    bounded_arguments = arguments.model_copy(
        update={
            "sql": tree.sql(
                dialect=dialect,
                comments=False,
                normalize=False,
                pretty=False,
            )
        }
    )
    bounded_intent = decision.next.intent.model_copy(
        update={"arguments": bounded_arguments}
    )
    bounded_next = decision.next.model_copy(update={"intent": bounded_intent})
    return decision.model_copy(update={"next": bounded_next})


def _model_research_query_admission_feedback(
    state: ResearchState,
    decision: ResearchDecisionV1,
    loaded_schema: LoadedSchema,
    registry: AdaptiveResearchToolRegistry,
) -> tuple[SchemaResearchValidationFeedback, str] | None:
    if isinstance(decision.next, (StopRequest, SemanticCommitRequest)) or not isinstance(
        decision.next.intent, ExecuteResearchProbeIntent
    ):
        return None
    if not isinstance(loaded_schema, LoadedSchema):
        raise TypeError("loaded_schema must be LoadedSchema")
    expected_schema_version = f"sha256:{loaded_schema.namespace.version_key}"
    if state.schema_namespace_version != expected_schema_version:
        raise ValueError("research state differs from the captured schema")
    maximum_row_limit = state.budget_state.remaining_rows
    if maximum_row_limit <= 0:
        return None

    runtime = registry.context.data_runtime
    dsn = getattr(runtime, "dsn", None)
    namespace = getattr(runtime, "table_namespace", None)
    get_plugin = getattr(runtime, "get_plugin", None)
    if type(dsn) is not str or type(namespace) is not str:
        raise TypeError("data runtime lacks trusted query metadata")
    if get_plugin is None:
        from db_plugins import get_plugin as default_get_plugin

        get_plugin = default_get_plugin
    if not callable(get_plugin):
        raise TypeError("data runtime get_plugin must be callable or null")

    dialect = dialect_for_plugin(get_plugin(dsn))
    arguments = decision.next.intent.arguments
    try:
        admit_research_query(
            RawResearchQuery(sql=arguments.sql, parameters=arguments.parameters),
            schema=loaded_schema.schema,
            dialect=dialect,
            namespace=namespace,
            schema_namespace_version=state.schema_namespace_version,
            maximum_row_limit=maximum_row_limit,
        )
    except ResearchQueryAdmissionError as error:
        logger.warning(
            "typed_schema_research_query retry=true code=%s",
            error.failure_code,
        )
        return (
            (
                "RAW_RESEARCH_QUERY_LIMIT"
                if error.failure_code == "research_query_limit"
                else {
                    "research_query_column": "INVALID_RESEARCH_QUERY_COLUMN",
                    "research_query_determinism": (
                        "INVALID_RESEARCH_QUERY_DETERMINISM"
                    ),
                    "research_query_output": "INVALID_RESEARCH_QUERY_OUTPUT",
                }.get(error.failure_code, "INVALID_RESEARCH_QUERY")
            ),
            error.failure_code,
        )
    return None


def _validate_model_stop(
    state: ResearchState,
    decision: ResearchDecisionV1,
    context: FreshnessContext,
) -> ResearchStopReason:
    if not isinstance(decision.next, StopRequest):
        return ResearchStopReason.PROTOCOL_FAILURE
    requested = _model_stop_reason(decision)
    required = {
        item.source_id for item in state.query_spec.semantic_items if item.required
    }
    affected = _affected_source_ids(state)
    if not set(decision.next.source_ids).issubset(required):
        return ResearchStopReason.PROTOCOL_FAILURE
    if requested is ResearchStopReason.COMPLETE:
        if (
            decision.next.source_ids
            or _completeness_reason(state, context) is not ResearchStopReason.COMPLETE
        ):
            return ResearchStopReason.PROTOCOL_FAILURE
    elif not decision.next.source_ids or not set(decision.next.source_ids).issubset(
        affected
    ):
        return ResearchStopReason.PROTOCOL_FAILURE
    evidence = {item.evidence_id: item for item in state.evidence}
    for evidence_id in decision.next.citation_evidence_ids:
        item = evidence.get(evidence_id)
        if (
            item is None
            or evaluate_evidence_freshness(item, context).status
            is not FreshnessStatus.FRESH
        ):
            return ResearchStopReason.PROTOCOL_FAILURE
    return requested


def _normalize_complete_stop_citations(
    state: ResearchState,
    decision: ResearchDecisionV1,
    context: FreshnessContext,
) -> ResearchDecisionV1:
    if (
        not isinstance(decision.next, StopRequest)
        or decision.next.reason != "complete"
        or decision.proposals
    ):
        return decision
    authority = evaluate_research_generation_authority(
        state,
        context,
        state.run_id,
        state.run_incarnation,
    )
    if not authority.allowed or authority.requirements is None:
        return decision
    citations = set(authority.requirements.eligible_evidence_ids)
    if _has_unbound_latest_probe_evidence(state):
        latest_action_digest = state.action_history[-1].action_digest
        citations.update(
            evidence.evidence_id
            for evidence in state.evidence
            if evidence.action_digest == latest_action_digest
            and evaluate_evidence_freshness(evidence, context).status
            is FreshnessStatus.FRESH
        )
    return decision.model_copy(
        update={
            "next": decision.next.model_copy(
                update={
                    "citation_evidence_ids": tuple(sorted(citations))
                }
            )
        }
    )


def _invalid_complete_generation_authority(
    state: ResearchState,
    decision: ResearchDecisionV1,
    context: FreshnessContext,
) -> tuple[CoverageInputErrorCode, tuple[str, ...]] | None:
    if (
        not isinstance(decision.next, StopRequest)
        or _model_stop_reason(decision) is not ResearchStopReason.COMPLETE
        or decision.next.source_ids
    ):
        return None
    evidence = {item.evidence_id: item for item in state.evidence}
    if any(
        (item := evidence.get(evidence_id)) is None
        or evaluate_evidence_freshness(item, context).status is not FreshnessStatus.FRESH
        for evidence_id in decision.next.citation_evidence_ids
    ):
        return None
    authority = evaluate_research_generation_authority(
        state, context, state.run_id, state.run_incarnation
    )
    if authority.allowed or _authority_stop_reason(authority) is not None:
        return None
    assert authority.reason is not None
    return authority.reason, tuple(sorted(authority.affected_source_ids))


def _model_call_id(state: ResearchState, attempt: int) -> str:
    return f"research-model-{state.revision}-{attempt}"


def _research_stop_review_call_id(state: ResearchState, attempt: int) -> str:
    return f"research-stop-review-{state.revision}-{attempt}"


_MODEL_CALL_ID = re.compile(
    r"^research-(?P<kind>model|stop-review)-(?P<revision>\d+)-(?P<attempt>\d+)$"
)


def _stop_review_call_revision(call_id: str) -> int | None:
    matched = _MODEL_CALL_ID.fullmatch(call_id)
    if matched is None or matched.group("kind") != "stop-review":
        return None
    return int(matched.group("revision"))

# Adaptive solver proposal calls (workflow/text_to_sql_adaptive_solver.py) share
# this same model-budget ledger but are keyed by SolverState.revision, a
# counter independent of the ResearchState revision tracked in this module.
_SOLVER_MODEL_CALL_ID = re.compile(r"^solver-generate-\d+-\d+$")


def _is_async_model(model: object) -> bool:
    return inspect.iscoroutinefunction(model) or inspect.iscoroutinefunction(
        getattr(model, "__call__", None)
    )


def _revalidate_state(value: ResearchState) -> ResearchState:
    try:
        return ResearchState.model_validate(
            value.model_dump(mode="python", by_alias=True, round_trip=True)
        )
    except (AttributeError, TypeError, ValidationError, ValueError) as error:
        raise TypeError("initial_state must satisfy ResearchState") from error


def _revalidate_freshness(value: FreshnessContext) -> FreshnessContext:
    try:
        return FreshnessContext.model_validate(
            value.model_dump(mode="python", by_alias=True, round_trip=True)
        )
    except (AttributeError, TypeError, ValidationError, ValueError) as error:
        raise TypeError("freshness_context must satisfy FreshnessContext") from error


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be non-empty text")
    return value


__all__ = (
    "ResearchLoopOutcome",
    "run_research_loop",
)
