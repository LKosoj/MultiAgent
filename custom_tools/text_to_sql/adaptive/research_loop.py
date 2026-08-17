"""Durable asynchronous coordinator for one-action schema research turns."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import inspect
import json
import logging
import math
import re
import time
from typing import Awaitable, Literal
import uuid

from pydantic import ValidationError

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
    BudgetState,
    ColumnRef,
    DiscriminatorValueBinding,
    DerivedExpressionBinding,
    EvidenceRecord,
    JoinCandidate,
    JoinCandidateStatus,
    LiteralValue,
    PhysicalColumnBinding,
    PredicateOperator,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    ResearchStopReason,
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
from .replay_inputs import (
    ResearchSemanticReplayInput,
    ResearchTerminalReplayInput,
)
from .research_decision import (
    BindingAssessment,
    ExistingBindingRef,
    ExistingJoinRef,
    ExecuteResearchProbeIntent,
    HypothesisAssessment,
    JoinAssessment,
    ResearchDecisionV1,
    SemanticCommitRequest,
    StopRequest,
    ToolIntent,
)
from ._semantic_value_certificate import (
    ExactValueCertificateError,
    evidence_observes_exact_column,
    evidence_observes_exact_value,
)
from .semantic_reducer import _declared_join_certificate
from .research_query import (
    RawResearchQuery,
    ResearchQueryAdmissionError,
    admit_research_query,
    dialect_for_plugin,
)
from .schema_research_agent import (
    SchemaResearchDecisionAdapter,
    SchemaResearchDecisionModel,
    SchemaResearchValidationFeedback,
)
from .semantic_coverage import CoverageInputErrorCode
from ._research_terminal_authority import (
    _affected_source_ids,
    _authority_stop_reason,
    _disconnected_required_source_ids as _disconnected_required_source_ids,
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


@dataclass(frozen=True, slots=True)
class ResearchLoopOutcome:
    """Closed, partial-safe result of schema research."""

    final_state: ResearchState
    stop_reason: ResearchStopReason
    affected_source_ids: tuple[str, ...]
    citation_evidence_ids: tuple[str, ...]
    ambiguity: AmbiguityReport | None
    rejection_signatures: tuple[tuple[str, str], ...] = ()


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
    ) -> None:
        self._initial_state = _revalidate_state(initial_state)
        self._task = _require_text(task, "task")
        self._research_context = research_context
        self._model = model
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
        self._latest_state = self._initial_state
        self._model_stagnation_signatures: tuple[tuple[str, str], ...] = ()

    async def run(self) -> ResearchLoopOutcome:
        state, failed = self._load_or_save_initial()
        if failed is not None:
            return self._outcome(state, failed)
        try:
            validation_feedback = self._failed_probe_feedback_from_replay(state)
        except (AdaptiveResearchStateStoreError, TypeError, ValueError):
            return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
        while True:
            key = _checkpoint_key(state)
            try:
                snapshot = self._checkpoint_store.get_snapshot(key)
            except AdaptiveCheckpointError:
                return self._outcome(state, ResearchStopReason.PROTOCOL_FAILURE)
            recover_planned = snapshot.planned is not None
            if snapshot.terminal is not None:
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
            terminal_freshness_context = _terminal_freshness_context(
                self._freshness_context
            )
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
                return self._stop(
                    state,
                    terminal_reason,
                    freshness_context=terminal_freshness_context,
                )
            if _consecutive_non_novel(self._checkpoint_store, state) >= 3:
                return self._stop(state, ResearchStopReason.STAGNATED)

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
        validation_feedbacks = (
            (validation_feedback,) if validation_feedback is not None else ()
        )
        rejected_duplicate_actions: tuple[dict[str, object], ...] = ()
        rejected_preflight_assessments: tuple[dict[str, object], ...] = ()
        invalid_stop_generation_authority: (
            tuple[CoverageInputErrorCode, tuple[str, ...]] | None
        ) = None
        self._model_stagnation_signatures = ()

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
        ) -> bool:
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
            rejection_signatures.add((rejection_path, rejection_code or feedback))
            if rejected_without_progress < _MAX_MODEL_REJECTIONS_WITHOUT_PROGRESS:
                return False
            self._model_stagnation_signatures = tuple(sorted(rejection_signatures))
            return True

        for attempt in range(limits.model_calls):
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
                    _model_call_id(state, attempt),
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
                invalid_stop_generation_authority = None
                if isinstance(captured.next, StopRequest):
                    if captured.proposals:
                        if reject_model_decision(
                            "STOP_WITH_PROPOSALS", "stop_with_proposals"
                        ):
                            return None, ResearchStopReason.STAGNATED, None
                        continue
                    stop_freshness_context = self._freshness_context
                    if _model_stop_reason(captured) is ResearchStopReason.COMPLETE:
                        stop_freshness_context = _terminal_freshness_context(
                            self._freshness_context
                        )
                    if (
                        _validate_model_stop(
                            state, captured, stop_freshness_context
                        )
                        is ResearchStopReason.PROTOCOL_FAILURE
                    ):
                        invalid_stop_generation_authority = (
                            _invalid_complete_generation_authority(
                                state, captured, stop_freshness_context
                            )
                        )
                        if reject_model_decision("INVALID_STOP", "invalid_stop"):
                            return None, ResearchStopReason.STAGNATED, None
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
                        baseline = _assessment_only_tool_baseline(captured)
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
                                decision = baseline
                                break
                        if rejected_action is not None:
                            rejected_duplicate_actions += (rejected_action,)
                        if preflight_feedback == "UNRESOLVABLE_PREFLIGHT":
                            rejected_preflight_assessments = rejected_assessments
                        else:
                            rejected_preflight_assessments = ()
                        if reject_model_decision(
                            preflight_feedback,
                            (
                                "duplicate_action"
                                if preflight_feedback == "DUPLICATE_ACTION"
                                else "unresolvable_preflight"
                            ),
                        ):
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
        try:
            resolved = self._resolve_current_decision(state, decision)
            if isinstance(decision.next, SemanticCommitRequest) or decision.proposals:
                commit_semantic_turn(resolved.admission)
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
                ),
            )
        except UnresolvableModelDecisionError:
            return (
                "UNRESOLVABLE_PREFLIGHT",
                None,
                None,
                _rejected_preflight_assessment_context(
                    state,
                    decision,
                    self._freshness_context,
                    self._preflight_requested_action(state, decision),
                ),
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
            self._checkpoint_store.record_planned(
                _checkpoint_key(state),
                expected_revision=None if state.revision == 0 else state.revision - 1,
                action=_planned_action(resolved),
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
            self._checkpoint_store.record_observed(
                _checkpoint_key(state), expected_revision=state.revision, action=action
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
            snapshot = self._checkpoint_store.get_snapshot(
                _checkpoint_key(previous)
            )
            if (
                snapshot.planned is None
                or snapshot.observed is None
                or admission.budget_state is None
            ):
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
        key = _checkpoint_key(state)
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
                stored_freshness_context = self._terminal_replay_freshness_context(key)
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
                    expected_revision=state.revision,
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
                    state.revision
                    if snapshot.planned is not None
                    else None
                    if state.revision == 0
                    else state.revision - 1
                ),
                action=terminal,
                replay_input=ResearchTerminalReplayInput(
                    freshness_context=terminal_freshness_context,
                ),
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
        )

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
    )
    try:
        return await coordinator.run()
    except asyncio.CancelledError:
        return coordinator._stop(  # noqa: SLF001 - closed cancellation result
            coordinator._latest_state, ResearchStopReason.CANCELLED
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
        matched = _MODEL_CALL_ID.match(record.reservation.call_id)
        if matched is None:
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


def _rejected_preflight_assessment_context(
    state: ResearchState,
    decision: ResearchDecisionV1,
    freshness_context: FreshnessContext,
    requested_action: ResearchAction | None,
) -> tuple[dict[str, object], ...]:
    """Describe rejected assessments and one deterministic missing probe."""

    bindings = {binding.binding_id: binding for binding in state.bindings}
    joins = {join.join_id: join for join in state.join_candidates}
    try:
        fresh_evidence = tuple(
            evidence
            for evidence in state.evidence
            if evaluate_evidence_freshness(evidence, freshness_context).status
            is FreshnessStatus.FRESH
        )
    except (TypeError, ValueError):
        return ()
    rejected: list[dict[str, object]] = []
    candidates: list[tuple[ColumnRef, dict[str, object], dict[str, object]]] = []
    join_candidates: list[tuple[TableRef, dict[str, object], dict[str, object]]] = []
    for proposal in decision.proposals:
        if not isinstance(
            proposal, (BindingAssessment, JoinAssessment, HypothesisAssessment)
        ):
            continue
        item: dict[str, object] = {
            "proposal": proposal.model_dump(mode="json", by_alias=True),
        }
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
                        binding, fresh_evidence, state.action_history
                    )
                    if missing_probe is not None:
                        column, probe = missing_probe
                        candidates.append((column, item, probe))
            else:
                missing_probe = _missing_binding_column_probe(
                    binding, fresh_evidence, state.action_history
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
    return tuple(
        sorted(rejected, key=lambda item: canonical_digest(item["proposal"]))
    )


def _missing_binding_column_probe(
    binding: object,
    evidence: tuple[EvidenceRecord, ...],
    action_history: tuple[ResearchAction, ...],
) -> tuple[ColumnRef, dict[str, object]] | None:
    """Return one exact inspection only when it is provably the missing fact."""

    if isinstance(binding, PhysicalColumnBinding):
        columns = (binding.physical_column,)
    elif isinstance(binding, DiscriminatorValueBinding):
        columns = (binding.discriminator_column,)
    elif isinstance(binding, DerivedExpressionBinding):
        columns = binding.input_columns
    else:
        return None
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
                if not any(
                    evidence_observes_exact_column(record, column)
                    for record in evidence
                )
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
    column = binding.discriminator_column
    try:
        if not any(
            evidence_observes_exact_column(record, column) for record in evidence
        ):
            return None
        predicate = binding.discriminator_predicate
        if predicate.operator is PredicateOperator.EQ:
            values = (predicate.right,)
        elif predicate.operator is PredicateOperator.IN and isinstance(
            predicate.right, tuple
        ):
            values = predicate.right
        elif predicate.operator is PredicateOperator.IS_NULL:
            values = (None,)
        else:
            return None
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
    if any(
        (
            novelty.added_hypothesis_ids,
            novelty.updated_hypothesis_ids,
            novelty.added_binding_ids,
            novelty.updated_binding_ids,
            novelty.added_join_ids,
            novelty.updated_join_ids,
        )
    ):
        return True
    if (
        novelty.unresolved_items != state.unresolved_items
        or novelty.stop_reason != state.stop_reason
    ):
        return True
    previous_ids = {record.evidence_id for record in state.evidence}
    previous = {_semantic_evidence_fingerprint(item) for item in state.evidence}
    added = {
        _semantic_evidence_fingerprint(item)
        for item in next_state.evidence
        if item.evidence_id not in previous_ids
    }
    return bool(added - previous)


def _semantic_evidence_fingerprint(evidence: EvidenceRecord) -> str:
    observation = json.loads(evidence.observation)
    if isinstance(observation, dict):
        observation.pop("invocation_id", None)
        provenance = observation.get("provenance")
        if isinstance(provenance, dict):
            for field_name in (
                "action_digest",
                "completed_at",
                "invocation_id",
                "started_at",
            ):
                provenance.pop(field_name, None)
    return canonical_digest(
        {
            "schema_namespace_version": evidence.schema_namespace_version,
            "source_kind": evidence.source_kind.value,
            "target": evidence.target.model_dump(mode="json", by_alias=True),
            "observation": observation,
            "validity_scope": evidence.validity_scope.value,
            "data_snapshot_token": evidence.data_snapshot_token,
            "strength": evidence.strength,
        }
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


def _assessment_only_tool_baseline(
    decision: ResearchDecisionV1,
) -> ResearchDecisionV1 | None:
    if not isinstance(decision.next, ToolIntent) or not decision.proposals:
        return None
    if not all(
        isinstance(item, (HypothesisAssessment, BindingAssessment, JoinAssessment))
        and item.subject.reference_kind == "existing"
        for item in decision.proposals
    ):
        return None
    return decision.model_copy(update={"proposals": ()})


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
    elif decision.next.source_ids != affected:
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


_MODEL_CALL_ID = re.compile(r"^research-model-(?P<revision>\d+)-(?P<attempt>\d+)$")


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


def _terminal_freshness_context(context: FreshnessContext) -> FreshnessContext:
    return FreshnessContext(
        evaluated_at=datetime.now(UTC),
        run_id=context.run_id,
        run_incarnation=context.run_incarnation,
        schema_namespace_version=context.schema_namespace_version,
        document_sources=context.document_sources,
        data_snapshots=context.data_snapshots,
    )


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
