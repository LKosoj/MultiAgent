"""Production coordination for the Typed SQL solver."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import cast

from llm_call_context import llm_call_context

from custom_tools.text_to_sql.adaptive.models import (
    CheckKind,
    CheckStatus,
    EvidenceSourceKind,
    PhysicalColumnBinding,
    PredicateOperator,
    ResearchReentryStatus,
    ResearchState,
    SemanticItemKind,
    SolverState,
    SolverStopReason,
)
from custom_tools.text_to_sql.adaptive._semantic_coverage_boundary import (
    evidence_has_state_authority,
)
from custom_tools.text_to_sql.adaptive._semantic_coverage_footprint import (
    normalized_state_digest,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageRequirements,
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    ContractDecodeError,
    canonical_json_bytes,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
from custom_tools.text_to_sql.adaptive.replay_inputs import (
    SolverReentryAdmissionReplayInput,
    SolverReentryCompletedReplayInput,
    SolverSqlProposalReplayInput,
)
from custom_tools.text_to_sql.adaptive.result_validation import (
    ResultContradictionReceipt,
)
from custom_tools.text_to_sql.adaptive.result_review import ResultReviewReceipt
from custom_tools.text_to_sql.adaptive.solver_loop import (
    SolverCandidateLimitError,
    SolverConflictError,
    SolverProtocolError,
    apply_solver_proposal,
    finalize_targeted_reentry,
    stop_solver,
)
from custom_tools.text_to_sql.adaptive.solver_protocol import SolverProposalV1
from custom_tools.text_to_sql.adaptive.solver_protocol import MissingEvidenceProposal
from custom_tools.text_to_sql.adaptive.solver_protocol import SqlCandidateProposal
from custom_tools.text_to_sql.adaptive.solver_runner import (
    run_solver_candidate_pre_execution_gates,
)
from custom_tools.text_to_sql.adaptive.sql_ast import SqlAstError, SqlAstErrorCode
from custom_tools.text_to_sql.adaptive.terminal import solver_stop_terminal_result
from .adaptive_solver_checkpoint import (
    AdaptiveSolverCheckpointStore,
    SolverCheckpoint,
    SolverExecutionReservation,
)
from .deadline import WorkflowDeadlineExceeded
from ._text_to_sql_solver_execution_reducer import (
    SolverExecutionReservationAuthority,
    execution_unknown_terminal_result,
    reserved_candidate as _pure_reserved_candidate,
    state_after_known_finalizer as _pure_state_after_known_finalizer,
    state_after_result_contradiction as _pure_state_after_result_contradiction,
)
from ._text_to_sql_solver_terminal_evidence import (
    build_verified_solver_terminal_evidence,
)
from ._text_to_sql_solver_reentry import settle_incomplete_reentry_model_call
from ._text_to_sql_reentry_recovery import recover_prepared_targeted_reentry
from .text_to_sql_contract import TextToSqlTerminalResult


logger = logging.getLogger(__name__)


SolverProposalBoundary = Callable[
    [SolverState, CoverageRequirements],
    Awaitable[SolverProposalV1],
]
SolverRepairProposalBoundary = Callable[
    [SolverState, CoverageRequirements, ResultReviewReceipt],
    Awaitable[SolverProposalV1],
]


@dataclass(frozen=True, slots=True)
class _SolverRetryFeedback:
    repair_receipt: ResultReviewReceipt
    sql_parse_feedback: Mapping[str, str]


SolverFeedbackProposalBoundary = Callable[
    [
        SolverState,
        CoverageRequirements,
        Mapping[str, str] | _SolverRetryFeedback,
    ],
    Awaitable[SolverProposalV1],
]
SolverReentryBoundary = Callable[..., Awaitable[object]]


def _result_reentry_receipt(value: object) -> ResultContradictionReceipt | ResultReviewReceipt:
    if not isinstance(value, Mapping):
        raise ValueError("result reentry receipt must be a mapping")
    canonical = canonical_json_bytes(value)
    if value.get("record_kind") == "text2sql_result_contradiction":
        return ResultContradictionReceipt.model_validate_json(canonical)
    if value.get("record_kind") == "text2sql_result_review":
        receipt = ResultReviewReceipt.model_validate_json(canonical)
        if receipt.verdict not in {"contradicted", "ambiguous"}:
            raise ValueError("result review receipt is not actionable")
        return receipt
    raise ValueError("result reentry receipt kind is invalid")


def _result_reentry_source_evidence(
    receipt: ResultContradictionReceipt | ResultReviewReceipt,
) -> tuple[str, str]:
    if type(receipt) is ResultContradictionReceipt:
        return receipt.finding.expectation.source_id, receipt.finding.expectation.evidence_id
    if receipt.source_id is None or receipt.evidence_id is None:
        raise ValueError("non-consistent review receipt has no repair target")
    return receipt.source_id, receipt.evidence_id


@dataclass(frozen=True, slots=True)
class SolverFinalizerPreparation:
    state: SolverState
    reservation: SolverExecutionReservation | None
    terminal: TextToSqlTerminalResult | None


async def run_production_adaptive_sql_generation(
    runtime: object,
    *,
    safety_policy: object,
    row_limit: int,
    dry_run_only: bool,
    table_namespace: str,
    model: Callable[[str], Awaitable[str | bytes]] | None = None,
    research_model: Callable[[str], Awaitable[str | bytes]] | None = None,
    reenter: SolverReentryBoundary | None = None,
) -> dict[str, object]:
    """Build the model boundary after exact Typed runtime validation."""

    runtime, _, _ = _validated_generation_runtime(runtime)
    from custom_tools.text_to_sql.adaptive.sql_solver_agent import (
        SqlSolverProposalAdapter,
        load_sql_solver_agent_profile,
    )

    profile = load_sql_solver_agent_profile()
    provider = model or _production_solver_model(
        runtime,
        profile.model,
        profile.instructions,
    )
    adapter = SqlSolverProposalAdapter(profile, provider)
    if reenter is None:
        from custom_tools.text_to_sql.adaptive.schema_research_agent import (
            load_schema_research_agent_profile,
        )
        from ._text_to_sql_solver_reentry import (
            build_production_reentry_boundary,
        )

        research_profile = load_schema_research_agent_profile()
        research_provider = research_model or _production_json_model(
            runtime,
            research_profile.model,
            "schema-research",
            research_profile.instructions,
        )
        reenter = build_production_reentry_boundary(
            runtime,
            table_namespace=table_namespace,
            model=research_provider,
        )

    async def propose(
        state: SolverState,
        requirements: CoverageRequirements,
        feedback: ResultReviewReceipt
        | Mapping[str, str]
        | _SolverRetryFeedback
        | None = None,
    ) -> SolverProposalV1:
        if type(feedback) is _SolverRetryFeedback:
            repair_receipt = feedback.repair_receipt
            sql_parse_feedback = feedback.sql_parse_feedback
        else:
            repair_receipt = feedback if type(feedback) is ResultReviewReceipt else None
            sql_parse_feedback = feedback if isinstance(feedback, Mapping) else None
        solver_context = _solver_context(
            runtime,
            state,
            requirements,
            repair_receipt,
            sql_parse_feedback,
        )
        return await adapter.propose(
            task=runtime.query,
            solver_context=solver_context,
            deadline=runtime.deadline,
        )

    return await run_adaptive_sql_generation(
        runtime,
        propose=propose,
        safety_policy=safety_policy,
        row_limit=row_limit,
        dry_run_only=dry_run_only,
        table_namespace=table_namespace,
        reenter=reenter,
    )


async def run_adaptive_sql_generation(
    runtime: object,
    *,
    propose: SolverProposalBoundary,
    safety_policy: object,
    row_limit: int,
    dry_run_only: bool,
    table_namespace: str,
    reenter: SolverReentryBoundary | None = None,
    id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> dict[str, object]:
    runtime, research, store = _validated_generation_runtime(runtime)
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    if checkpoint is not None:
        resumed = _resume_generation(runtime, store, checkpoint)
        if resumed is not None:
            return resumed
    from ._text_to_sql_document_authority import (
        DocumentAuthorityError,
        live_solver_document_freshness_context,
        solver_document_freshness_reference,
    )

    try:
        current_freshness = live_solver_document_freshness_context(runtime, research)
    except DocumentAuthorityError:
        recovery_base = _recoverable_reentry_base_state(runtime, checkpoint, research)
        if recovery_base is None:
            raise
        research = recovery_base
        current_freshness = live_solver_document_freshness_context(runtime, research)
    validate_coverage_inputs(
        research,
        current_freshness,
        runtime.run_id,
        runtime.run_incarnation,
    )
    freshness = solver_document_freshness_reference(runtime, research)
    requirements = validate_coverage_inputs(
        research,
        freshness,
        runtime.run_id,
        runtime.run_incarnation,
    )
    repair_receipt = None
    if checkpoint is not None:
        persisted_reentry = _result_contradiction_requirements(
            store,
            checkpoint,
            research,
        )
        if persisted_reentry is not None:
            persisted_requirements, receipt = persisted_reentry
            if (
                type(receipt) is ResultReviewReceipt
                and receipt.verdict == "contradicted"
                and receipt.repair_kind is None
            ):
                repair_receipt = receipt
            else:
                checkpoint = _commit_result_contradiction_missing_evidence(
                    runtime,
                    store,
                    checkpoint,
                    research,
                    persisted_requirements,
                    table_namespace=table_namespace,
                )
    if checkpoint is None:
        initial = _initial_solver_state(research)
        store.initialize(initial)
        checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
        if checkpoint is None:
            raise RuntimeError("solver checkpoint initialization was not durable")
    if repair_receipt is None:
        (
            checkpoint,
            research,
            requirements,
            freshness,
            resumed,
        ) = await _resume_open_generation(
            runtime,
            store,
            checkpoint,
            research,
            requirements,
            freshness,
            reenter,
            id_factory,
            safety_policy,
            row_limit,
            dry_run_only,
            table_namespace,
        )
    else:
        resumed = None
    if resumed is not None:
        return resumed

    model_turns = len(checkpoint.state.action_history)
    proposal_feedback = None
    while model_turns < 8:
        boundary = await _generation_boundary(runtime)
        if boundary is not None:
            return _stop_and_project(runtime, store, checkpoint, boundary)
        (
            checkpoint,
            proposal_failure,
            proposal_feedback,
        ) = await _commit_next_solver_proposal(
            runtime,
            store,
            checkpoint,
            requirements,
            table_namespace,
            propose,
            id_factory,
            repair_receipt,
            proposal_feedback,
        )
        if proposal_feedback is None:
            repair_receipt = None
        model_turns += 1
        if proposal_failure is not None:
            return _stop_and_project(
                runtime,
                store,
                checkpoint,
                proposal_failure,
            )
        if proposal_feedback is not None:
            continue

        if checkpoint.state.stop_reason is SolverStopReason.MISSING_EVIDENCE:
            (
                checkpoint,
                research,
                requirements,
                freshness,
                terminal,
            ) = await _continue_after_missing_evidence(
                runtime,
                store,
                checkpoint,
                research,
                requirements,
                freshness,
                reenter,
                id_factory,
            )
            if terminal is not None:
                return terminal
            continue

        parsed_candidate = _durable_parsed_candidate(store, checkpoint, requirements)
        checkpoint = await asyncio.to_thread(
            _run_pre_execution,
            runtime,
            store,
            checkpoint,
            research,
            requirements,
            safety_policy,
            row_limit,
            dry_run_only,
            parsed_candidate,
        )
        ready = _ready_candidate(checkpoint.state)
        if ready is not None:
            return _ready_generation_output(runtime, checkpoint.state, ready)

    return _stop_and_project(
        runtime,
        store,
        checkpoint,
        SolverStopReason.BUDGET_EXHAUSTED,
    )


async def _resume_open_generation(
    runtime,
    store,
    checkpoint,
    research,
    requirements,
    freshness,
    reenter,
    id_factory,
    safety_policy,
    row_limit,
    dry_run_only,
    table_namespace,
):
    before_reentry_recovery = checkpoint
    try:
        recovered = recover_prepared_targeted_reentry(
            runtime,
            checkpoint.state,
            freshness,
        )
    except Exception:
        recovered = None
    if recovered is not None:
        if recovered.record.status is ResearchReentryStatus.ADMITTED:
            runtime.verified_research_state = recovered.research_state
            resumed = await _run_reentry(
                runtime,
                store,
                checkpoint,
                recovered.research_state,
                requirements,
                recovered.freshness,
                reenter,
                id_factory,
                resume_admitted=True,
            )
            if resumed[0].state.research_reentries[-1].status is not ResearchReentryStatus.COMPLETED:
                terminal = _seal_stopped_generation(runtime, store, resumed[0])
                return (*resumed, terminal)
            return (*resumed, None)
        replay_input = (
                _completed_reentry_replay_input(
                    recovered.record,
                    recovered.research_state,
                    recovered.freshness,
                    recovered.requirements,
                    research_state_store=runtime.research_state_store,
                )
            if recovered.record.status is ResearchReentryStatus.COMPLETED
            else None
        )
        checkpoint = store.commit_non_execution(
            checkpoint.state,
            recovered.solver_state,
            action_revision=checkpoint.cursor.next_action_revision,
            action={
                "kind": "research_reentry_finalized",
                "record": recovered.record.model_dump(mode="json"),
            },
            replay_input=replay_input,
        )
        research = recovered.research_state
        requirements = recovered.requirements
        freshness = recovered.freshness
        runtime.verified_research_state = research
        if recovered.record.status is not ResearchReentryStatus.COMPLETED:
            terminal = _seal_stopped_generation(runtime, store, checkpoint)
            return checkpoint, research, requirements, freshness, terminal
        from ._text_to_sql_document_authority import (
            live_solver_document_freshness_context,
            solver_document_freshness_reference,
        )

        current_freshness = live_solver_document_freshness_context(runtime, research)
        validate_coverage_inputs(
            research,
            current_freshness,
            research.run_id,
            research.run_incarnation,
        )
        freshness = solver_document_freshness_reference(runtime, research)
        requirements = validate_coverage_inputs(
            research,
            freshness,
            research.run_id,
            research.run_incarnation,
        )
        before_reentry_recovery = checkpoint
    else:
        research = await settle_incomplete_reentry_model_call(
            runtime,
            checkpoint.state,
            research,
            requirements,
        )

    boundary = await _generation_boundary(runtime)
    if boundary is not None:
        if checkpoint.state.stop_reason is SolverStopReason.MISSING_EVIDENCE:
            status = (
                ResearchReentryStatus.CANCELLED
                if boundary is SolverStopReason.CANCELLED
                else ResearchReentryStatus.DEADLINE_EXCEEDED
            )
            checkpoint = _finalize_incomplete_reentry(
                store,
                checkpoint,
                status=status,
            )
            terminal = _seal_stopped_generation(runtime, store, checkpoint)
        else:
            terminal = _stop_and_project(runtime, store, checkpoint, boundary)
        return checkpoint, research, requirements, freshness, terminal
    checkpoint = _finalize_incomplete_reentry(store, checkpoint)
    if checkpoint != before_reentry_recovery:
        terminal = _seal_stopped_generation(runtime, store, checkpoint)
        return checkpoint, research, requirements, freshness, terminal
    if checkpoint.state.stop_reason is SolverStopReason.MISSING_EVIDENCE:
        return await _continue_after_missing_evidence(
            runtime,
            store,
            checkpoint,
            research,
            requirements,
            freshness,
            reenter,
            id_factory,
        )
    if checkpoint.state.stop_reason is not None:
        terminal = _seal_stopped_generation(runtime, store, checkpoint)
        return checkpoint, research, requirements, freshness, terminal
    latest_successful_execution = next(
        (
            item
            for item in reversed(checkpoint.state.execution_results)
            if item.success
        ),
        None,
    )
    if (
        latest_successful_execution is not None
        and checkpoint.state.sql_candidates
        and checkpoint.state.sql_candidates[-1].candidate_id
        == latest_successful_execution.candidate_id
        and checkpoint.state.research_reentries
        and checkpoint.state.research_reentries[-1].status
        is ResearchReentryStatus.COMPLETED
    ):
        return checkpoint, research, requirements, freshness, None
    ready = _ready_candidate(checkpoint.state)
    if ready is not None:
        return (
            checkpoint,
            research,
            requirements,
            freshness,
            _ready_generation_output(runtime, checkpoint.state, ready),
        )
    if checkpoint.state.sql_candidates:
        checkpoint = await asyncio.to_thread(
            _run_pre_execution,
            runtime,
            store,
            checkpoint,
            research,
            requirements,
            safety_policy,
            row_limit,
            dry_run_only,
            None,
        )
        ready = _ready_candidate(checkpoint.state)
        if ready is not None:
            return (
                checkpoint,
                research,
                requirements,
                freshness,
                _ready_generation_output(runtime, checkpoint.state, ready),
            )
    return checkpoint, research, requirements, freshness, None


async def _continue_after_missing_evidence(
    runtime,
    store,
    checkpoint,
    research,
    requirements,
    freshness,
    reenter,
    id_factory,
):
    if reenter is None:
        terminal = _seal_stopped_generation(runtime, store, checkpoint)
        return checkpoint, research, requirements, freshness, terminal
    try:
        checkpoint, research, requirements, freshness = await _run_reentry(
            runtime,
            store,
            checkpoint,
            research,
            requirements,
            freshness,
            reenter,
            id_factory,
        )
    except Exception:
        logger.exception("Adaptive solver research re-entry failed")
        checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
        if checkpoint is None:
            raise RuntimeError("solver checkpoint disappeared during re-entry")
        checkpoint = _finalize_incomplete_reentry(store, checkpoint)
        terminal = _seal_stopped_generation(runtime, store, checkpoint)
        return checkpoint, research, requirements, freshness, terminal
    if checkpoint.state.stop_reason is not None:
        terminal = _seal_stopped_generation(runtime, store, checkpoint)
        return checkpoint, research, requirements, freshness, terminal
    return checkpoint, research, requirements, freshness, None


async def _commit_next_solver_proposal(
    runtime,
    store,
    checkpoint,
    requirements,
    table_namespace,
    propose,
    id_factory,
    repair_receipt,
    proposal_feedback,
):
    proposal = None
    try:
        if proposal_feedback is not None:
            feedback = proposal_feedback
            if repair_receipt is not None:
                feedback = _SolverRetryFeedback(
                    repair_receipt=repair_receipt,
                    sql_parse_feedback=proposal_feedback,
                )
            proposal = await cast(SolverFeedbackProposalBoundary, propose)(
                checkpoint.state,
                requirements,
                feedback,
            )
        elif repair_receipt is None:
            proposal = await propose(checkpoint.state, requirements)
        else:
            proposal = await cast(SolverRepairProposalBoundary, propose)(
                checkpoint.state,
                requirements,
                repair_receipt,
            )
        proposal = _normalize_solver_proposal_source_id(checkpoint.state, proposal)
        if (
            repair_receipt is not None
            and type(proposal.proposal) is not SqlCandidateProposal
        ):
            raise SolverProtocolError(
                "proven SQL repair requires a SQL candidate"
            )
        transition = apply_solver_proposal(
            checkpoint.state,
            proposal,
            base_revision=checkpoint.state.revision,
            dsn=runtime.dsn,
            table_namespace=table_namespace,
            requirements=requirements,
            id_factory=id_factory,
        )
        committed = store.commit_non_execution(
            checkpoint.state,
            transition.state,
            action_revision=checkpoint.cursor.next_action_revision,
            action=transition.action.model_dump(mode="json"),
            replay_input=transition.replay_input,
        )
        return committed, None, None
    except WorkflowDeadlineExceeded:
        return checkpoint, SolverStopReason.DEADLINE_EXCEEDED, None
    except SqlAstError as exc:
        if (
            exc.code is SqlAstErrorCode.PARSE_FAILED
            and
            type(proposal) is SolverProposalV1
            and type(proposal.proposal) is SqlCandidateProposal
        ):
            return (
                checkpoint,
                None,
                {
                    "failure_code": "SQL_PARSE_REJECTED",
                    "reason": "SQL parser rejected the prior candidate",
                    "rejected_sql": proposal.proposal.sql,
                },
            )
        return checkpoint, SolverStopReason.TOOL_FAILURE, None
    except Exception as exc:
        return checkpoint, _proposal_failure_reason(exc), None


def _normalize_solver_proposal_source_id(
    state: SolverState,
    proposal: SolverProposalV1,
) -> SolverProposalV1:
    item = proposal.proposal
    if type(item) is not MissingEvidenceProposal:
        return proposal
    source_ids = tuple(
        semantic_item.source_id for semantic_item in state.query_spec.semantic_items
    )
    if item.source_id in source_ids:
        return proposal
    matches = tuple(
        source_id
        for source_id in source_ids
        if len(source_id) == len(item.source_id)
        and sum(
            left != right
            for left, right in zip(source_id, item.source_id, strict=True)
        )
        == 1
    )
    if len(matches) != 1:
        return proposal
    normalized = item.model_copy(update={"source_id": matches[0]})
    return proposal.model_copy(update={"proposal": normalized})


def _proposal_failure_reason(exc):
    if isinstance(exc, SolverCandidateLimitError):
        return SolverStopReason.NO_SAFE_CANDIDATE
    if isinstance(exc, SolverConflictError):
        return SolverStopReason.STAGNATED
    if isinstance(exc, (ContractDecodeError, SolverProtocolError)):
        return SolverStopReason.PROTOCOL_FAILURE
    return SolverStopReason.TOOL_FAILURE


def _production_solver_model(runtime, profile_model, system_prompt):
    return _production_json_model(
        runtime,
        profile_model,
        "SQL-solver",
        system_prompt,
    )


def _production_json_model(runtime, profile_model, label, system_prompt):
    policy = runtime.verified_research_policy
    limits = getattr(policy, "model_budget", None)
    output_tokens = getattr(limits, "output_tokens_per_call", None)
    if type(output_tokens) is not int or output_tokens <= 0:
        raise TypeError("adaptive solver requires configured model limits")

    from agent_command import create_text_to_sql_model
    from utils import call_openai_api

    provider = create_text_to_sql_model(
        profile_model,
        max_tokens=output_tokens,
        temperature=0.3,
    )

    async def model(prompt: str) -> str:
        with llm_call_context(run_id=runtime.run_id, step_name=label):
            response = await asyncio.to_thread(
                call_openai_api,
                prompt=prompt,
                model=provider,
                max_tokens=output_tokens,
                temperature=0.3,
                max_retries=0,
                response_format={"type": "json_object"},
                system_prompt=system_prompt,
            )
        if type(response) is bytes:
            response = response.decode("utf-8", errors="strict")
        if type(response) is not str:
            raise TypeError(f"{label} model response is not text")
        if len(response.encode("utf-8")) > max(1_024, output_tokens * 8):
            raise ValueError(f"{label} model response exceeds its safety envelope")
        return response

    return model


def _solver_context(
    runtime,
    state,
    requirements,
    repair_receipt=None,
    sql_parse_feedback=None,
) -> str:
    policy = runtime.verified_research_policy
    limits = getattr(policy, "model_budget", None)
    input_tokens = getattr(limits, "input_tokens_per_call", None)
    if type(input_tokens) is not int or input_tokens <= 0:
        raise TypeError("adaptive solver requires configured model limits")
    if repair_receipt is not None and type(repair_receipt) is not ResultReviewReceipt:
        raise TypeError("solver repair receipt must be an exact ResultReviewReceipt")
    payload_data = {
        "coverage_requirements": requirements.model_dump(mode="json"),
        "solver_state": state.model_dump(mode="json"),
        "trusted_documents": [
            {"content": document.content, "document_id": document.document_id}
            for document in runtime.document_snapshot
        ],
    }
    if repair_receipt is not None:
        payload_data["deterministic_sql_repair_receipt"] = repair_receipt.model_dump(
            mode="json"
        )
    if sql_parse_feedback is not None:
        payload_data["sql_parse_feedback"] = dict(sql_parse_feedback)
    payload = canonical_json_bytes(payload_data)
    if len(payload) > input_tokens * 4:
        raise ValueError("adaptive solver context exceeds configured model input")
    return payload.decode("utf-8")


def _validated_generation_runtime(runtime: object):
    from .text_to_sql_typed_runtime import (
        TextToSqlTypedRuntime,
        _ADMISSION_CAPABILITY,
    )

    if (
        type(runtime) is not TextToSqlTypedRuntime
        or runtime._capability is not _ADMISSION_CAPABILITY
        or type(runtime.verified_research_state) is not ResearchState
        or type(runtime.solver_checkpoint_store) is not AdaptiveSolverCheckpointStore
        or not isinstance(runtime.dsn, str)
        or not runtime.dsn
        or runtime.loaded_schema is None
    ):
        raise TypeError("adaptive solver runtime is incomplete")
    return (
        runtime,
        runtime.verified_research_state,
        runtime.solver_checkpoint_store,
    )


def _initial_solver_state(research: ResearchState) -> SolverState:
    return SolverState(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        query_spec=research.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )


def _resume_generation(runtime, store, checkpoint):
    if checkpoint.terminal is not None:
        terminal = TextToSqlTerminalResult.from_mapping(
            json.loads(checkpoint.terminal.terminal_bytes)
        )
        runtime.verified_solver_terminal = terminal
        return _terminal_generation_output(terminal)
    if checkpoint.pending_execution is not None:
        checkpoint = reconcile_pending_finalizer_unknown(store, checkpoint)
        terminal = TextToSqlTerminalResult.from_mapping(
            json.loads(checkpoint.terminal.terminal_bytes)
        )
        runtime.verified_solver_state = checkpoint.state
        runtime.verified_solver_terminal = terminal
        return _terminal_generation_output(terminal)
    return None


def _ready_generation_output(runtime, state, candidate):
    runtime.verified_solver_state = state
    runtime.verified_solver_candidate_id = candidate.candidate_id
    return {
        "sql": candidate.sql,
        "description": "Adaptive solver candidate passed deterministic gates",
    }


async def _generation_boundary(runtime) -> SolverStopReason | None:
    if runtime.is_cancelled():
        return SolverStopReason.CANCELLED
    checker = runtime._cancellation_checker
    if checker is not None and await checker():
        runtime.mark_cancelled()
        return SolverStopReason.CANCELLED
    if runtime.deadline.remaining_seconds() <= 0:
        return SolverStopReason.DEADLINE_EXCEEDED
    return None


def _run_pre_execution(
    runtime,
    store,
    checkpoint,
    research,
    requirements,
    safety_policy,
    row_limit,
    dry_run_only,
    parsed_candidate=None,
):
    def commit(transition):
        nonlocal checkpoint
        checkpoint = store.commit_non_execution(
            checkpoint.state,
            transition.state,
            action_revision=checkpoint.cursor.next_action_revision,
            action={
                "kind": "solver_check",
                "check": transition.check_result.model_dump(mode="json"),
            },
        )
        return checkpoint.state

    candidate = checkpoint.state.sql_candidates[-1]
    run_solver_candidate_pre_execution_gates(
        checkpoint.state,
        candidate.candidate_id,
        research_state=research,
        requirements=requirements,
        loaded_schema=runtime.loaded_schema,
        dsn=runtime.dsn,
        safety_policy=safety_policy,
        row_limit=row_limit,
        dry_run_only=dry_run_only,
        deadline=runtime.deadline,
        is_cancelled=runtime.is_cancelled,
        commit_transition=commit,
        parsed_candidate=parsed_candidate,
    )
    return checkpoint


def _durable_parsed_candidate(store, checkpoint, requirements):
    """Load the one parser result committed with this candidate proposal."""

    candidate = checkpoint.state.sql_candidates[-1]
    run_id = checkpoint.state.run_id
    run_incarnation = checkpoint.state.run_incarnation
    chain = store.load_replay_chain(run_id, run_incarnation)
    if chain is None:
        raise ValueError("candidate replay chain is missing")
    matches = []
    for action in chain.actions:
        replay = store.load_transition_replay_input(
            run_id, run_incarnation, action.action_revision
        )
        if type(replay) is not SolverSqlProposalReplayInput:
            continue
        parsed = replay.parsed_candidate.to_candidate()
        proposal = replay.proposal.proposal
        if (
            parsed.candidate_id == candidate.candidate_id
            and parsed.candidate_digest == candidate.normalized_ast_digest
            and type(proposal) is SqlCandidateProposal
            and proposal.sql == candidate.sql
            and replay.requirements == requirements
        ):
            matches.append(parsed)
    if len(matches) != 1:
        raise ValueError("candidate replay parser authority is invalid")
    return matches[0]


async def _run_reentry(
    runtime,
    store,
    checkpoint,
    research,
    requirements,
    freshness,
    reenter,
    id_factory,
    resume_admitted=False,
):
    request = checkpoint.state.missing_evidence_requests[-1]
    durable_research = runtime.research_state_store.load_research_state(
        research.run_id,
        research.run_incarnation,
        revision=research.revision,
    )
    if durable_research is None:
        raise RuntimeError("re-entry requires a durable research snapshot")

    def commit_admission(transition):
        nonlocal checkpoint
        replay_input = SolverReentryAdmissionReplayInput(
            research_state_revision=research.revision,
            research_state_digest=canonical_digest(durable_research),
            missing_evidence_request_id=request.missing_evidence_request_id,
            generated_reentry_id=transition.record.research_reentry_id,
        )
        checkpoint = store.commit_non_execution(
            checkpoint.state,
            transition.state,
            action_revision=checkpoint.cursor.next_action_revision,
            action={
                "kind": "research_reentry_admitted",
                "record": transition.record.model_dump(mode="json"),
            },
            replay_input=replay_input,
        )
        return checkpoint.state

    reentry_kwargs = {
        "requirements": requirements,
        "freshness_context": freshness,
        "commit_solver_admission": commit_admission,
        "id_factory": id_factory,
    }
    if resume_admitted:
        reentry_kwargs["resume_admitted"] = True
    outcome = await reenter(
        checkpoint.state,
        research,
        request.missing_evidence_request_id,
        **reentry_kwargs,
    )
    before = checkpoint.state
    replay_input = (
        _completed_reentry_replay_input(
            outcome.record,
            outcome.research_state,
            outcome.freshness_context,
            outcome.requirements,
            research_state_store=runtime.research_state_store,
        )
        if outcome.record.status is ResearchReentryStatus.COMPLETED
        else None
    )
    checkpoint = store.commit_non_execution(
        before,
        outcome.solver_state,
        action_revision=checkpoint.cursor.next_action_revision,
        action={
            "kind": "research_reentry_finalized",
            "record": outcome.record.model_dump(mode="json"),
        },
        replay_input=replay_input,
    )
    if outcome.record.status is not ResearchReentryStatus.COMPLETED:
        return checkpoint, outcome.research_state, requirements, freshness
    runtime.verified_research_state = outcome.research_state
    refreshed = outcome.freshness_context
    rebuilt = outcome.requirements
    if refreshed is None or rebuilt is None:
        raise RuntimeError("completed re-entry omitted replay authority")
    from ._text_to_sql_document_authority import (
        live_solver_document_freshness_context,
        solver_document_freshness_reference,
    )

    current_freshness = live_solver_document_freshness_context(
        runtime,
        outcome.research_state,
    )
    validate_coverage_inputs(
        outcome.research_state,
        current_freshness,
        outcome.research_state.run_id,
        outcome.research_state.run_incarnation,
    )
    refreshed = solver_document_freshness_reference(
        runtime,
        outcome.research_state,
    )
    rebuilt = validate_coverage_inputs(
        outcome.research_state,
        refreshed,
        outcome.research_state.run_id,
        outcome.research_state.run_incarnation,
    )
    return checkpoint, outcome.research_state, rebuilt, refreshed


def _completed_reentry_replay_input(
    record,
    research_state,
    freshness_context,
    requirements,
    *,
    research_state_store,
):
    if freshness_context is None or requirements is None:
        raise RuntimeError("completed re-entry omitted replay authority")
    durable_research_state = research_state_store.load_research_state(
        research_state.run_id,
        research_state.run_incarnation,
        revision=research_state.revision,
    )
    if durable_research_state is None:
        raise RuntimeError("completed re-entry omitted durable research state")
    durable_requirements = validate_coverage_inputs(
        durable_research_state,
        freshness_context,
        durable_research_state.run_id,
        durable_research_state.run_incarnation,
    )
    return SolverReentryCompletedReplayInput(
        research_reentry_id=record.research_reentry_id,
        research_state_revision=research_state.revision,
        research_state_digest=canonical_digest(durable_research_state),
        freshness_context=freshness_context,
        requirements=durable_requirements,
    )


def _recoverable_reentry_base_state(
    runtime,
    checkpoint: SolverCheckpoint | None,
    research: ResearchState,
) -> ResearchState | None:
    """Return only the durable base needed to finish one interrupted re-entry."""

    if checkpoint is None:
        return None
    admitted = tuple(
        record
        for record in checkpoint.state.research_reentries
        if record.status is ResearchReentryStatus.ADMITTED
    )
    if len(admitted) != 1 or admitted[0] is not checkpoint.state.research_reentries[-1]:
        return None
    base = getattr(runtime, "research_state_store", None)
    load = getattr(base, "load_research_state", None)
    if not callable(load):
        return None
    state = load(
        research.run_id,
        research.run_incarnation,
        revision=admitted[0].research_base_revision,
    )
    if (
        type(state) is not ResearchState
        or state.revision != admitted[0].research_base_revision
        or research.revision <= state.revision
        or state.run_id != research.run_id
        or state.run_incarnation != research.run_incarnation
        or state.schema_namespace_version != research.schema_namespace_version
    ):
        return None
    return state


def _ready_candidate(state: SolverState):
    if not state.sql_candidates or state.stop_reason is not None:
        return None
    candidate = state.sql_candidates[-1]
    checks = tuple(
        item
        for item in state.check_results
        if item.candidate_id == candidate.candidate_id
    )
    expected = (
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )
    if len(checks) != len(expected) or any(
        check.check_kind is not kind or check.status is not CheckStatus.PASSED
        for check, kind in zip(checks, expected, strict=True)
    ):
        return None
    return candidate


def _finalize_incomplete_reentry(
    store,
    checkpoint,
    *,
    status=ResearchReentryStatus.PROTOCOL_FAILURE,
):
    admitted = tuple(
        record
        for record in checkpoint.state.research_reentries
        if record.status is ResearchReentryStatus.ADMITTED
    )
    if not admitted:
        return checkpoint
    if len(admitted) != 1 or admitted[0] is not checkpoint.state.research_reentries[-1]:
        raise ValueError("solver has conflicting admitted research re-entry")
    finalized = finalize_targeted_reentry(
        checkpoint.state,
        admitted[0].research_reentry_id,
        status,
        base_revision=checkpoint.state.revision,
    )
    return store.commit_non_execution(
        checkpoint.state,
        finalized.state,
        action_revision=checkpoint.cursor.next_action_revision,
        action={
            "kind": "research_reentry_finalized",
            "record": finalized.record.model_dump(mode="json"),
        },
    )


def _stop_and_project(runtime, store, checkpoint, reason):
    stopped = stop_solver(
        checkpoint.state,
        reason,
        base_revision=checkpoint.state.revision,
    )
    checkpoint = store.commit_non_execution(
        checkpoint.state,
        stopped,
        action_revision=checkpoint.cursor.next_action_revision,
        action={"kind": "solver_stop", "reason": reason.value},
    )
    return _seal_stopped_generation(runtime, store, checkpoint)


def _seal_stopped_generation(runtime, store, checkpoint):
    terminal = solver_stop_terminal_result(runtime.run_id, checkpoint.state)
    if terminal is None:
        raise ValueError("stopped solver did not produce a public terminal")
    terminal_bytes = canonical_json_bytes(terminal.to_mapping())
    store.record_terminal(
        checkpoint.state,
        expected_action_revision=checkpoint.cursor.next_action_revision,
        terminal_bytes=terminal_bytes,
    )
    runtime.verified_solver_state = checkpoint.state
    runtime.verified_solver_terminal = terminal
    return _terminal_generation_output(terminal)


def _terminal_generation_output(terminal):
    return {
        "sql": "",
        "description": f"Adaptive solver stopped: {terminal.reason_code or 'terminal'}",
    }


def prepare_finalizer_execution(
    runtime: object,
    request: Mapping[str, object],
    *,
    id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> SolverFinalizerPreparation:
    runtime, _, store = _validated_generation_runtime(runtime)
    checkpoint = store.load(runtime.run_id, runtime.run_incarnation)
    if checkpoint is None:
        raise RuntimeError("solver checkpoint is missing before finalizer")
    if checkpoint.terminal is not None:
        return _prepared_terminal(runtime, checkpoint)
    if checkpoint.pending_execution is not None:
        recovered = reconcile_pending_finalizer_unknown(store, checkpoint)
        return _prepared_terminal(runtime, recovered)
    candidate = _ready_candidate(checkpoint.state)
    if candidate is None or (
        runtime.verified_solver_candidate_id != candidate.candidate_id
    ):
        raise ValueError("solver has no exact ready candidate")
    _validate_finalizer_request(request, candidate.sql)
    execution_id = id_factory()
    if type(execution_id) is not str or not execution_id:
        raise ValueError("execution_id factory returned invalid text")
    reservation = store.reserve_execution(
        checkpoint.state,
        action_revision=checkpoint.cursor.next_action_revision,
        candidate_id=candidate.candidate_id,
        execution_id=execution_id,
        request=request,
    )
    runtime.verified_solver_state = checkpoint.state
    return SolverFinalizerPreparation(checkpoint.state, reservation, None)


def apply_finalizer_checkpoint(runtime: object, checkpoint: SolverCheckpoint):
    runtime, _, _ = _validated_generation_runtime(runtime)
    if checkpoint.terminal is None:
        raise ValueError("finalizer checkpoint must be terminal")
    terminal = TextToSqlTerminalResult.from_mapping(
        json.loads(checkpoint.terminal.terminal_bytes)
    )
    runtime.verified_solver_state = checkpoint.state
    runtime.verified_solver_candidate_id = None
    runtime.verified_solver_terminal = terminal
    return terminal.to_mapping()


def _result_contradiction_requirements(
    store: AdaptiveSolverCheckpointStore,
    checkpoint: SolverCheckpoint,
    research: ResearchState,
) -> tuple[CoverageRequirements, ResultContradictionReceipt | ResultReviewReceipt] | None:
    if type(store) is not AdaptiveSolverCheckpointStore:
        raise TypeError("store must be an exact AdaptiveSolverCheckpointStore")
    if type(checkpoint) is not SolverCheckpoint:
        raise TypeError("checkpoint must be an exact SolverCheckpoint")
    if type(research) is not ResearchState:
        raise TypeError("research must be an exact ResearchState")
    if checkpoint.terminal is not None or checkpoint.pending_execution is not None:
        return None
    if (
        checkpoint.state.run_id != research.run_id
        or checkpoint.state.run_incarnation != research.run_incarnation
        or checkpoint.state.schema_namespace_version
        != research.schema_namespace_version
        or checkpoint.cursor.run_id != checkpoint.state.run_id
        or checkpoint.cursor.run_incarnation != checkpoint.state.run_incarnation
        or checkpoint.cursor.state_revision != checkpoint.state.revision
    ):
        raise ValueError("result-contradiction checkpoint identity is invalid")
    chain = store.load_replay_chain(research.run_id, research.run_incarnation)
    if chain is None:
        raise ValueError("result-contradiction replay chain is missing")
    if (
        chain.run_id != checkpoint.state.run_id
        or chain.run_incarnation != checkpoint.state.run_incarnation
        or chain.state_revision != checkpoint.state.revision
        or chain.state_digest != checkpoint.cursor.state_digest
        or chain.next_action_revision != checkpoint.cursor.next_action_revision
        or chain.pending_execution_action_revision is not None
        or chain.terminal is not None
    ):
        raise ValueError("result-contradiction checkpoint is not the replay head")
    if not chain.actions or chain.actions[-1].action_kind != "execution":
        return None
    execution = chain.actions[-1]
    reconciliations = tuple(
        item
        for item in chain.reconciliations
        if item.action_revision == execution.action_revision
        and item.outcome == "KNOWN"
        and item.result_state_revision == checkpoint.state.revision
        and item.result_state_digest == checkpoint.cursor.state_digest
    )
    if len(reconciliations) != 1:
        raise ValueError("result-contradiction known reconciliation is invalid")
    receipt = _result_reentry_receipt(reconciliations[0].result)
    if (
        receipt.run_id != research.run_id
        or receipt.run_incarnation != research.run_incarnation
        or receipt.research_state_revision != research.revision
    ):
        raise ValueError("result-contradiction receipt authority is invalid")
    candidates = tuple(
        item
        for item in checkpoint.state.sql_candidates
        if item.candidate_id == receipt.candidate_id
    )
    if (
        len(candidates) != 1
        or execution.candidate_id != receipt.candidate_id
        or execution.normalized_ast_digest != receipt.normalized_ast_digest
        or candidates[0].revision != receipt.research_state_revision
        or candidates[0].normalized_ast_digest != receipt.normalized_ast_digest
    ):
        raise ValueError("result-contradiction receipt candidate is invalid")
    matches: list[SolverSqlProposalReplayInput] = []
    for action in chain.actions:
        if action.action_revision >= execution.action_revision:
            continue
        replay = store.load_transition_replay_input(
            research.run_id,
            research.run_incarnation,
            action.action_revision,
        )
        if type(replay) is not SolverSqlProposalReplayInput:
            continue
        parsed = replay.parsed_candidate.to_candidate()
        proposal = replay.proposal.proposal
        requirements = replay.requirements
        if (
            parsed.candidate_id == receipt.candidate_id
            and parsed.candidate_digest == receipt.normalized_ast_digest
            and replay.generated_ids[0] == receipt.candidate_id
            and type(proposal) is SqlCandidateProposal
            and proposal.sql == candidates[0].sql
            and requirements.run_id == research.run_id
            and requirements.run_incarnation == research.run_incarnation
            and requirements.schema_namespace_version
            == research.schema_namespace_version
            and requirements.state_revision == candidates[0].revision
            and requirements.state_digest == normalized_state_digest(research)
            and requirements.requirements_digest == receipt.requirements_digest
        ):
            matches.append(replay)
    if len(matches) != 1:
        raise ValueError("result-contradiction proposal replay is invalid")
    return matches[0].requirements, receipt


def _commit_result_contradiction_missing_evidence(
    runtime: object,
    store: AdaptiveSolverCheckpointStore,
    checkpoint: SolverCheckpoint,
    research: ResearchState,
    requirements: CoverageRequirements,
    *,
    table_namespace: str,
) -> SolverCheckpoint:
    runtime, runtime_research, runtime_store = _validated_generation_runtime(runtime)
    if type(store) is not AdaptiveSolverCheckpointStore or store is not runtime_store:
        raise TypeError("store must be the exact runtime solver checkpoint store")
    if type(checkpoint) is not SolverCheckpoint:
        raise TypeError("checkpoint must be an exact SolverCheckpoint")
    if type(research) is not ResearchState:
        raise TypeError("research must be an exact ResearchState")
    if type(requirements) is not CoverageRequirements:
        raise TypeError("requirements must be exact CoverageRequirements")
    research = ResearchState.model_validate(
        research.model_dump(mode="python", round_trip=True)
    )
    requirements = CoverageRequirements.model_validate(
        requirements.model_dump(mode="python", round_trip=True)
    )
    if research != runtime_research:
        raise ValueError("research does not match the runtime research state")
    if (
        requirements.run_id != research.run_id
        or requirements.run_incarnation != research.run_incarnation
        or requirements.state_revision != research.revision
        or requirements.schema_namespace_version != research.schema_namespace_version
        or requirements.state_digest != normalized_state_digest(research)
    ):
        raise ValueError("requirements do not match the current research state")
    if (
        checkpoint.state.run_id != research.run_id
        or checkpoint.state.run_incarnation != research.run_incarnation
        or checkpoint.state.schema_namespace_version
        != research.schema_namespace_version
        or checkpoint.pending_execution is not None
        or checkpoint.terminal is not None
        or checkpoint.cursor.run_id != checkpoint.state.run_id
        or checkpoint.cursor.run_incarnation != checkpoint.state.run_incarnation
        or checkpoint.cursor.state_revision != checkpoint.state.revision
    ):
        raise ValueError("checkpoint is not an open result-contradiction state")

    chain = store.load_replay_chain(research.run_id, research.run_incarnation)
    if chain is None:
        raise ValueError("result-contradiction replay chain is missing")
    known = tuple(
        item for item in chain.reconciliations if item.outcome == "KNOWN"
    )
    if not known:
        raise ValueError("result-contradiction known reconciliation is missing")
    reconciliation = known[-1]
    if (
        reconciliation.result_state_revision != checkpoint.state.revision
        or reconciliation.result_state_digest != checkpoint.cursor.state_digest
        or checkpoint.cursor.next_action_revision != reconciliation.action_revision + 1
    ):
        raise ValueError("checkpoint is not the reconciled result-contradiction state")
    snapshots = tuple(
        item
        for item in chain.snapshots
        if item.state.revision == reconciliation.result_state_revision
        and item.state_digest == reconciliation.result_state_digest
    )
    if len(snapshots) != 1 or snapshots[0].state != checkpoint.state:
        raise ValueError("checkpoint does not match the reconciled result state")
    receipt = _result_reentry_receipt(reconciliation.result)
    if (
        receipt.run_id != research.run_id
        or receipt.run_incarnation != research.run_incarnation
        or receipt.research_state_revision != research.revision
        or receipt.requirements_digest != requirements.requirements_digest
    ):
        raise ValueError("result-contradiction receipt authority does not match")
    candidate = next(
        (
            item
            for item in checkpoint.state.sql_candidates
            if item.candidate_id == receipt.candidate_id
        ),
        None,
    )
    if (
        candidate is None
        or candidate.revision != receipt.research_state_revision
        or candidate.normalized_ast_digest != receipt.normalized_ast_digest
    ):
        raise ValueError("result-contradiction receipt candidate does not match")
    source_id, evidence_id = _result_reentry_source_evidence(receipt)
    if (
        type(receipt) is ResultContradictionReceipt
        and receipt.finding.expectation not in research.result_expectations
    ):
        raise ValueError("result-contradiction expectation is not current research")
    if source_id not in {item.source_id for item in research.query_spec.semantic_items}:
        raise ValueError("result reentry source is not current research")
    evidence = tuple(
        item
        for item in research.evidence
        if item.evidence_id == evidence_id
    )
    if len(evidence) != 1 or not evidence_has_state_authority(evidence[0], research):
        raise ValueError("result-contradiction evidence is not current authority")

    execution_actions = tuple(
        item
        for item in chain.actions
        if item.action_revision == reconciliation.action_revision
        and item.action_kind == "execution"
    )
    if len(execution_actions) != 1:
        raise ValueError("result-contradiction execution reservation is missing")
    execution_action = execution_actions[0]
    action = json.loads(canonical_json_bytes(execution_action.action))
    if type(action) is not dict or set(action) != {
        "candidate_id",
        "execution_id",
        "normalized_ast_digest",
        "request",
    }:
        raise ValueError("result-contradiction execution action is invalid")
    if (
        action["candidate_id"] != execution_action.candidate_id
        or action["execution_id"] != execution_action.execution_id
        or action["normalized_ast_digest"] != execution_action.normalized_ast_digest
        or action["candidate_id"] != receipt.candidate_id
        or action["normalized_ast_digest"] != receipt.normalized_ast_digest
    ):
        raise ValueError("result-contradiction execution identity does not match")
    request_digest = "sha256:" + hashlib.sha256(
        canonical_json_bytes(action["request"])
    ).hexdigest()
    reservation_identity = {
        "run_id": chain.run_id,
        "run_incarnation": chain.run_incarnation,
        "action_revision": execution_action.action_revision,
        "base_state_revision": execution_action.base_state_revision,
        "base_state_digest": execution_action.base_state_digest,
        "candidate_id": execution_action.candidate_id,
        "execution_id": execution_action.execution_id,
        "normalized_ast_digest": execution_action.normalized_ast_digest,
        "request_digest": request_digest,
    }
    generated_digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "receipt": receipt.model_dump(mode="json"),
                "reservation": reservation_identity,
            }
        )
    ).hexdigest()
    generated_ids = (
        f"result-contradiction-request-{generated_digest}",
        f"result-contradiction-action-{generated_digest}",
    )
    question = (
        receipt.reason
        if type(receipt) is ResultReviewReceipt
        else "Recheck the trusted evidence for this result review."
    )
    reason = (
        receipt.reason
        if type(receipt) is ResultReviewReceipt
        else "The executed result needs another targeted evidence check."
    )
    repair_binding_id = receipt.repair_binding_id if type(receipt) is ResultReviewReceipt else None
    repair_kind = receipt.repair_kind if type(receipt) is ResultReviewReceipt else None
    predicate_authority = (
        receipt.predicate_authority if type(receipt) is ResultReviewReceipt else None
    )
    if repair_kind is not None:
        repair_bindings = tuple(
            binding
            for binding in requirements.selected_bindings
            if binding.source_id == source_id
            and evidence_id in binding.evidence_ids
            and (
                repair_binding_id is None
                or binding.binding_id == repair_binding_id
            )
        )
        if len(repair_bindings) != 1:
            raise ValueError("semantic repair target is not one selected binding")
        repair_binding_id = repair_bindings[0].binding_id
    if predicate_authority is not None:
        source_item = next(
            item for item in research.query_spec.semantic_items if item.source_id == source_id
        )
        predicate_bindings = tuple(
            binding
            for binding in requirements.selected_bindings
            if binding.source_id == source_id
            and isinstance(binding, PhysicalColumnBinding)
            and binding.physical_column == predicate_authority.left
        )
        if (
            source_item.kind is not SemanticItemKind.DIMENSION
            or predicate_authority.operator is not PredicateOperator.EQ
            or len(predicate_bindings) != 1
        ):
            raise ValueError("predicate authority is not one selected dimension binding")
    proposal = SolverProposalV1(
        proposal_version=1,
        proposal=MissingEvidenceProposal(
            proposal_kind="missing_evidence",
            source_id=source_id,
            question=question,
            required_evidence_kind=(
                EvidenceSourceKind.VALUE_SEARCH
                if predicate_authority is not None
                else evidence[0].source_kind
            ),
            reason=reason,
            repair_kind=repair_kind,
            repair_binding_id=repair_binding_id,
            predicate_authority=predicate_authority,
        ),
    )
    transition = apply_solver_proposal(
        checkpoint.state,
        proposal,
        base_revision=checkpoint.state.revision,
        dsn=runtime.dsn,
        table_namespace=table_namespace,
        requirements=requirements,
        id_factory=iter(generated_ids).__next__,
        trusted_semantic_repair=repair_kind is not None,
        trusted_predicate_authority=predicate_authority is not None,
    )
    return store.commit_non_execution(
        checkpoint.state,
        transition.state,
        action_revision=checkpoint.cursor.next_action_revision,
        action=transition.action.model_dump(mode="json"),
        replay_input=transition.replay_input,
    )


def _prepared_terminal(runtime, checkpoint):
    terminal_mapping = apply_finalizer_checkpoint(runtime, checkpoint)
    terminal = TextToSqlTerminalResult.from_mapping(terminal_mapping)
    return SolverFinalizerPreparation(checkpoint.state, None, terminal)


def _validate_finalizer_request(request, expected_sql):
    if not isinstance(request, Mapping) or set(request) != {
        "operation",
        "sql_query",
        "row_limit",
        "dry_run_only",
    }:
        raise ValueError("finalizer request has an invalid shape")
    if (
        request["operation"] != "finalize_text_to_sql_run"
        or request["sql_query"] != expected_sql
        or type(request["row_limit"]) is not int
        or request["row_limit"] <= 0
        or type(request["dry_run_only"]) is not bool
    ):
        raise ValueError("finalizer request does not match the ready candidate")


def reconcile_known_finalizer(
    store: AdaptiveSolverCheckpointStore,
    reservation: SolverExecutionReservation,
    state: SolverState,
    terminal_mapping: Mapping[str, object],
) -> SolverCheckpoint:
    if terminal_mapping.get("record_kind") in {
        "text2sql_result_contradiction",
        "text2sql_result_review",
    }:
        receipt = _result_reentry_receipt(dict(terminal_mapping))
        after_state = _pure_state_after_result_contradiction(
            state,
            _reservation_authority(reservation),
            receipt,
        )
        return store.reconcile_execution(
            reservation,
            after_state,
            result=receipt.model_dump(mode="json"),
        )
    terminal = TextToSqlTerminalResult.from_mapping(terminal_mapping)
    after_state = _state_after_known_finalizer(state, reservation, terminal)
    authority = _reservation_authority(reservation)
    evidence = build_verified_solver_terminal_evidence(
        state,
        after_state,
        authority,
        terminal,
    )
    return store.reconcile_execution_terminal(
        reservation,
        after_state,
        outcome="KNOWN",
        terminal_bytes=canonical_json_bytes(terminal.to_mapping()),
        verified_terminal_evidence=evidence,
    )


def reconcile_pending_finalizer_unknown(
    store: AdaptiveSolverCheckpointStore,
    checkpoint: SolverCheckpoint,
) -> SolverCheckpoint:
    if type(checkpoint) is not SolverCheckpoint:
        raise TypeError("checkpoint must be SolverCheckpoint")
    reservation = checkpoint.pending_execution
    if reservation is None:
        raise ValueError("checkpoint has no pending finalizer reservation")
    return reconcile_reserved_finalizer_unknown(
        store,
        reservation,
        checkpoint.state,
    )


def reconcile_reserved_finalizer_unknown(
    store: AdaptiveSolverCheckpointStore,
    reservation: SolverExecutionReservation,
    state: SolverState,
) -> SolverCheckpoint:
    candidate = _reserved_candidate(state, reservation)
    failure_state = stop_solver(
        state,
        SolverStopReason.TOOL_FAILURE,
        base_revision=state.revision,
    )
    terminal = execution_unknown_terminal_result(state.run_id, candidate.sql)
    return store.reconcile_execution_terminal(
        reservation,
        failure_state,
        outcome="UNKNOWN",
        terminal_bytes=canonical_json_bytes(terminal.to_mapping()),
    )


def _state_after_known_finalizer(
    state: SolverState,
    reservation: SolverExecutionReservation,
    terminal: TextToSqlTerminalResult,
) -> SolverState:
    return _pure_state_after_known_finalizer(
        state,
        _reservation_authority(reservation),
        terminal,
    )


def _reserved_candidate(
    state: SolverState,
    reservation: SolverExecutionReservation,
):
    return _pure_reserved_candidate(state, _reservation_authority(reservation))


def _reservation_authority(
    reservation: SolverExecutionReservation,
) -> SolverExecutionReservationAuthority:
    if type(reservation) is not SolverExecutionReservation:
        raise TypeError("reservation must be SolverExecutionReservation")
    return SolverExecutionReservationAuthority(
        run_id=reservation.run_id,
        run_incarnation=reservation.run_incarnation,
        action_revision=reservation.action_revision,
        base_state_revision=reservation.base_state_revision,
        base_state_digest=reservation.base_state_digest,
        candidate_id=reservation.candidate_id,
        execution_id=reservation.execution_id,
        normalized_ast_digest=reservation.normalized_ast_digest,
        request_bytes=reservation.request_bytes,
        request_digest=reservation.request_digest,
        created_at_ns=reservation.created_at_ns,
    )


__all__ = (
    "SolverFinalizerPreparation",
    "apply_finalizer_checkpoint",
    "prepare_finalizer_execution",
    "reconcile_known_finalizer",
    "reconcile_pending_finalizer_unknown",
    "reconcile_reserved_finalizer_unknown",
    "run_adaptive_sql_generation",
    "run_production_adaptive_sql_generation",
)
