"""Pure deterministic execution of a validated adaptive replay payload."""

from __future__ import annotations

import json

from .models import ResearchReentryStatus, SolverAction, SolverStopReason
from .replay_contract import (
    HistoricalReplayResult,
    HistoricalReplayStatus,
    ReplayArtifactAttachment,
    ReplayContractError,
    SolverCheckReplayAction,
    SolverExecutionReplayStep,
    SolverReentryAdmittedReplayAction,
    SolverReentryFinalizedReplayAction,
    SolverStopReplayAction,
    SolverTransitionReplayStep,
    _result_contradiction_receipt,
    sha256_digest,
)
from .replay_inputs import (
    SolverMissingEvidenceReplayInput,
    SolverReentryAdmissionReplayInput,
    SolverReentryCompletedReplayInput,
    SolverSqlProposalReplayInput,
)
from .serialization import canonical_digest, canonical_json_bytes


def replay_envelope(artifact, trusted_artifact_digest: str) -> HistoricalReplayResult:
    payload = artifact.payload
    if payload.historical_status is HistoricalReplayStatus.UNVERIFIABLE:
        return HistoricalReplayResult(
            status=HistoricalReplayStatus.UNVERIFIABLE,
            legacy_reasons=payload.legacy_reasons,
            trusted_artifact_digest=trusted_artifact_digest,
            research_state_digest=None,
            solver_state_digest=None,
            verified_research_transition_count=0,
            verified_solver_transition_count=0,
        )
    attachment_reader = _attachment_reader(payload.artifact_attachments)
    research_digest = _replay_research(payload, attachment_reader)
    solver_digest = _replay_solver(payload)
    return HistoricalReplayResult(
        status=HistoricalReplayStatus.VERIFIED,
        legacy_reasons=(),
        trusted_artifact_digest=trusted_artifact_digest,
        research_state_digest=research_digest,
        solver_state_digest=solver_digest,
        verified_research_transition_count=len(payload.research_transitions),
        verified_solver_transition_count=len(payload.solver_steps),
    )


def _replay_research(payload, read_artifact) -> str | None:
    if not payload.research_snapshots:
        return None
    from ._research_terminal_authority import (
        _terminal_envelope,
        _terminal_replay_is_authorized,
    )
    from .semantic_reducer import admit_semantic_turn, commit_semantic_turn

    current = payload.research_snapshots[0]
    for transition, successor in zip(
        payload.research_transitions,
        payload.research_snapshots[1:],
        strict=True,
    ):
        replay_input = transition.replay_input
        if replay_input is None:
            raise ReplayContractError("verified research transition lacks replay input")
        admission = admit_semantic_turn(
            current.state,
            replay_input.decision,
            batch=replay_input.semantic_batch,
            freshness_context=replay_input.freshness_context,
            tool_claim=replay_input.tool_claim,
            budget_state=replay_input.budget_state,
        )
        planned = transition.planned
        observed = transition.observed
        if (
            admission.action is None
            or planned is None
            or observed is None
            or admission.action != planned.action
            or planned.state_digest != current.digest
        ):
            raise ReplayContractError("research admission does not match artifact")
        committed = commit_semantic_turn(
            admission,
            probe_result=replay_input.probe_result,
            read_artifact=read_artifact,
        )
        if committed.state != successor.state:
            raise ReplayContractError("research reducer state does not match snapshot")
        if committed.novelty.is_novel is not observed.novel:
            raise ReplayContractError("research novelty does not match journal")
        current = successor
    abort = payload.research_abort_journal
    terminal = payload.research_terminal
    if abort is not None and (
        terminal is None
        or abort.aborted.reason is not terminal.action.reason
        or abort.planned.action != abort.aborted.action
    ):
        raise ReplayContractError("research abort journal does not match terminal")
    if terminal is not None:
        if terminal.replay_input is None:
            raise ReplayContractError("verified research terminal lacks replay input")
        action = terminal.action.model_dump(mode="json")
        envelope = _terminal_envelope(action, current.state)
        if not _terminal_replay_is_authorized(
            current.state,
            terminal.replay_input.freshness_context,
            terminal.action.reason,
            envelope,
        ):
            raise ReplayContractError("research terminal is not authorized")
    return current.digest


def _replay_solver(payload) -> str | None:
    if not payload.solver_snapshots:
        return None
    current = payload.solver_snapshots[0]
    research_states = {
        snapshot.state.revision: snapshot.state
        for snapshot in payload.research_snapshots
    }
    for step, successor in zip(
        payload.solver_steps,
        payload.solver_snapshots[1:],
        strict=True,
    ):
        if type(step) is SolverTransitionReplayStep:
            replayed = _replay_solver_transition(step, current.state, research_states)
        else:
            replayed = _replay_solver_execution(step, current.state)
        if replayed != successor.state:
            raise ReplayContractError("solver reducer state does not match snapshot")
        current = successor
    _verify_solver_terminal(payload, current.state)
    return current.digest


def _replay_solver_transition(step, state, research_states):
    from .solver_loop import (
        admit_targeted_reentry,
        apply_solver_proposal_from_parsed,
        finalize_targeted_reentry,
        stop_solver,
    )
    from .solver_results import append_solver_check_result

    action = step.action
    replay_input = step.replay_input
    if type(action) is SolverAction:
        if type(replay_input) not in (
            SolverSqlProposalReplayInput,
            SolverMissingEvidenceReplayInput,
        ):
            raise ReplayContractError("solver proposal lacks exact replay input")
        parsed = (
            replay_input.parsed_candidate.to_candidate()
            if type(replay_input) is SolverSqlProposalReplayInput
            else None
        )
        transition = apply_solver_proposal_from_parsed(
            state,
            replay_input.proposal,
            base_revision=state.revision,
            parsed_candidate=parsed,
            requirements=replay_input.requirements,
            generated_ids=replay_input.generated_ids,
        )
        if transition.action != action:
            raise ReplayContractError("solver proposal action does not match reducer")
        return transition.state
    if type(action) is SolverReentryAdmittedReplayAction:
        if type(replay_input) is not SolverReentryAdmissionReplayInput:
            raise ReplayContractError("re-entry admission lacks exact replay input")
        research = research_states.get(replay_input.research_state_revision)
        if research is None or canonical_digest(research) != (
            replay_input.research_state_digest
        ):
            raise ReplayContractError("re-entry research authority is missing")
        transition = admit_targeted_reentry(
            state,
            research,
            replay_input.missing_evidence_request_id,
            base_revision=state.revision,
            id_factory=lambda: replay_input.generated_reentry_id,
        )
        expected = SolverReentryAdmittedReplayAction(record=transition.record)
        if expected != action:
            raise ReplayContractError(
                "re-entry admission action does not match reducer"
            )
        return transition.state
    if type(action) is SolverReentryFinalizedReplayAction:
        if action.record.status is ResearchReentryStatus.COMPLETED:
            if type(replay_input) is not SolverReentryCompletedReplayInput:
                raise ReplayContractError("completed re-entry lacks exact replay input")
            research = research_states.get(replay_input.research_state_revision)
            if research is None or canonical_digest(research) != (
                replay_input.research_state_digest
            ):
                raise ReplayContractError("completed re-entry authority is missing")
            transition = finalize_targeted_reentry(
                state,
                replay_input.research_reentry_id,
                ResearchReentryStatus.COMPLETED,
                base_revision=state.revision,
                research_state=research,
                freshness_context=replay_input.freshness_context,
                requirements=replay_input.requirements,
            )
        else:
            if replay_input is not None:
                raise ReplayContractError("failed re-entry cannot carry replay input")
            transition = finalize_targeted_reentry(
                state,
                action.record.research_reentry_id,
                action.record.status,
                base_revision=state.revision,
            )
        expected = SolverReentryFinalizedReplayAction(record=transition.record)
        if expected != action:
            raise ReplayContractError(
                "re-entry finalization action does not match reducer"
            )
        return transition.state
    if type(action) is SolverCheckReplayAction:
        if replay_input is not None:
            raise ReplayContractError("solver check cannot carry replay input")
        transition = append_solver_check_result(
            state,
            action.check,
            base_revision=state.revision,
        )
        if transition.check_result != action.check:
            raise ReplayContractError("solver check action does not match reducer")
        return transition.state
    if type(action) is SolverStopReplayAction:
        if replay_input is not None:
            raise ReplayContractError("solver stop cannot carry replay input")
        return stop_solver(
            state,
            action.reason,
            base_revision=state.revision,
        )
    raise ReplayContractError("unsupported solver transition action")


def _replay_solver_execution(step, state):
    from workflow._text_to_sql_solver_execution_reducer import (
        SolverExecutionReservationAuthority,
        SolverExecutionRequestError,
        reserved_candidate,
        state_after_known_finalizer,
        state_after_result_contradiction,
    )

    request_bytes = canonical_json_bytes(step.action.request)
    reservation = SolverExecutionReservationAuthority(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        action_revision=step.action_revision,
        base_state_revision=step.base_state_revision,
        base_state_digest=step.base_state_digest,
        candidate_id=step.action.candidate_id,
        execution_id=step.action.execution_id,
        normalized_ast_digest=step.action.normalized_ast_digest,
        request_bytes=request_bytes,
        request_digest=sha256_digest(request_bytes),
        created_at_ns=step.created_at_ns,
    )
    try:
        candidate = reserved_candidate(state, reservation)
    except SolverExecutionRequestError as exc:
        raise ReplayContractError(
            "execution request does not match reserved candidate"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise ReplayContractError("execution reservation does not match state") from exc
    if step.action.request.sql_query != candidate.sql:
        raise ReplayContractError("execution request does not match reserved candidate")
    if step.reconciliation.outcome == "UNKNOWN":
        from .solver_loop import stop_solver

        return stop_solver(
            state,
            SolverStopReason.TOOL_FAILURE,
            base_revision=state.revision,
        )
    receipt = _result_contradiction_receipt(step)
    if receipt is not None:
        return state_after_result_contradiction(state, reservation, receipt)
    from workflow.text_to_sql_contract import TextToSqlTerminalResult

    result = json.loads(step.reconciliation.result.content())
    if not isinstance(result, dict):
        raise ReplayContractError("known finalizer result must be an object")
    terminal = TextToSqlTerminalResult.from_mapping(result)
    return state_after_known_finalizer(state, reservation, terminal)


def _verify_solver_terminal(payload, state) -> None:
    terminal = payload.solver_terminal
    if terminal is None:
        return
    if terminal.state_revision != state.revision or terminal.state_digest != (
        canonical_digest(state)
    ):
        raise ReplayContractError("solver terminal does not match final state")
    if payload.solver_steps and type(payload.solver_steps[-1]) is (
        SolverExecutionReplayStep
    ):
        last = payload.solver_steps[-1]
        if last.reconciliation.outcome == "KNOWN":
            expected = last.reconciliation.result.content()
        else:
            from workflow._text_to_sql_solver_execution_reducer import (
                execution_unknown_terminal_result,
            )

            try:
                candidate = next(
                    item
                    for item in payload.solver_snapshots[-2].state.sql_candidates
                    if item.candidate_id == last.action.candidate_id
                )
            except (IndexError, StopIteration) as exc:
                raise ReplayContractError(
                    "unknown execution terminal has no reserved candidate"
                ) from exc
            expected = canonical_json_bytes(
                execution_unknown_terminal_result(
                    state.run_id, candidate.sql
                ).to_mapping()
            )
    else:
        from .terminal import solver_stop_terminal_result

        projected = solver_stop_terminal_result(state.run_id, state)
        if projected is None:
            raise ReplayContractError("solver terminal has no pure projection")
        expected = canonical_json_bytes(projected.to_mapping())
    if expected != terminal.terminal.content():
        raise ReplayContractError("solver terminal bytes do not match pure projection")


def _attachment_reader(
    attachments: tuple[ReplayArtifactAttachment, ...],
):
    content = {item.reference.artifact_id: item for item in attachments}

    def read(reference):
        attachment = content.get(reference.artifact_id)
        if attachment is None or attachment.reference != reference:
            raise ReplayContractError("artifact attachment is missing")
        return attachment.content()

    return read


__all__ = ["replay_envelope"]
