"""Read-only assembly of deterministic adaptive Text-to-SQL replay artifacts."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass

from custom_tools.text_to_sql.adaptive.model_budget import ModelBudgetLedgerRecord
from custom_tools.text_to_sql.adaptive.models import (
    QuerySpec,
    ResearchActionKind,
    ResearchReentryStatus,
    ResearchState,
    SolverAction,
)
from custom_tools.text_to_sql.adaptive.policy import BudgetLedgerRecord
from custom_tools.text_to_sql.adaptive.probes import ArtifactReader
from custom_tools.text_to_sql.adaptive.replay import (
    AdaptiveReplayPayload,
    CanonicalReplayBlob,
    HistoricalReplayStatus,
    LegacyReplayReason,
    ReplayArtifactAttachment,
    ReplayContractError,
    ResearchAbortedReplayAction,
    ResearchObservedReplayAction,
    ResearchPlannedReplayAction,
    ResearchReplayAbortJournal,
    ResearchReplaySnapshot,
    ResearchReplayTerminal,
    ResearchReplayTransition,
    ResearchTerminalReplayAction,
    SolverCheckReplayAction,
    SolverExecutionReconciliation,
    SolverExecutionReplayAction,
    SolverExecutionReplayStep,
    SolverReentryAdmittedReplayAction,
    SolverReentryFinalizedReplayAction,
    SolverSemanticRepairFallbackReplayAction,
    SolverReplaySnapshot,
    SolverReplayTerminal,
    SolverStopReplayAction,
    SolverTransitionReplayStep,
    encode_replay_artifact,
)
from custom_tools.text_to_sql.adaptive.replay_inputs import (
    ResearchSemanticReplayInput,
    ResearchTerminalReplayInput,
    SolverMissingEvidenceReplayInput,
    SolverReentryAdmissionReplayInput,
    SolverReentryCompletedReplayInput,
    SolverSqlProposalReplayInput,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    DEFAULT_LIMITS,
    ArtifactReference,
    StateSizeLimitError,
    canonical_json_bytes,
    verify_artifact_reference,
)
from custom_tools.text_to_sql.adaptive.replay_contract import (
    _result_contradiction_receipt,
    dedupe_artifact_references,
    sha256_digest,
)

from .adaptive_budget_ledger import AdaptiveBudgetLedger
from .adaptive_research_state_store import AdaptiveResearchStateStore
from .adaptive_solver_checkpoint import (
    AdaptiveSolverCheckpointStore,
    SolverReplayAction,
    SolverReplayChain,
    SolverReplayReconciliation,
)
from .adaptive_state_store import (
    AdaptiveActionPhase,
    AdaptiveCheckpointEvent,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)


SolverInput = (
    SolverSqlProposalReplayInput
    | SolverMissingEvidenceReplayInput
    | SolverReentryAdmissionReplayInput
    | SolverReentryCompletedReplayInput
)

MAX_REPLAY_ATTACHMENT_BYTES = 48 * 1024
MAX_REPLAY_ATTACHMENT_COUNT = 32


@dataclass(frozen=True, slots=True)
class _ReplayAuthorities:
    query_specs: tuple[QuerySpec, ...]
    research_states: tuple[ResearchState, ...]
    research_inputs: tuple[ResearchSemanticReplayInput | None, ...]
    research_events: tuple[AdaptiveCheckpointEvent, ...]
    research_terminal_input: ResearchTerminalReplayInput | None
    solver_chain: SolverReplayChain | None
    solver_inputs: tuple[SolverInput | None, ...]
    budget_records: tuple[BudgetLedgerRecord, ...]
    model_budget_records: tuple[ModelBudgetLedgerRecord, ...]


def build_adaptive_replay_artifact(
    run_id: str,
    run_incarnation: str,
    *,
    checkpoint_store: AdaptiveStateStore,
    research_store: AdaptiveResearchStateStore,
    solver_store: AdaptiveSolverCheckpointStore,
    budget_ledger: AdaptiveBudgetLedger,
    read_artifact: ArtifactReader | None = None,
) -> bytes:
    """Build canonical replay bytes from validated durable read authorities."""

    _require_store(checkpoint_store, AdaptiveStateStore, "checkpoint_store")
    _require_store(research_store, AdaptiveResearchStateStore, "research_store")
    _require_store(solver_store, AdaptiveSolverCheckpointStore, "solver_store")
    _require_store(budget_ledger, AdaptiveBudgetLedger, "budget_ledger")
    first = _load_authorities(
        run_id,
        run_incarnation,
        checkpoint_store,
        research_store,
        solver_store,
        budget_ledger,
    )
    second = _load_authorities(
        run_id,
        run_incarnation,
        checkpoint_store,
        research_store,
        solver_store,
        budget_ledger,
    )
    if first != second:
        raise ReplayContractError("durable replay authorities changed during export")

    schema = _validate_identity(run_id, run_incarnation, first)
    research_snapshots = tuple(
        ResearchReplaySnapshot(state=state, digest=_state_digest(state))
        for state in first.research_states
    )
    research_events = _research_event_index(first)
    research_transitions = _research_transitions(first, research_events)
    research_abort_journal = _research_abort_journal(
        first,
        research_snapshots,
        research_events,
    )
    research_terminal = _research_terminal(
        first,
        research_snapshots,
        research_events,
    )
    solver_snapshots, solver_steps, solver_terminal = _solver_chain(first)
    _validate_budget_links(first, research_transitions)
    references = _artifact_references(
        research_transitions,
        first.budget_records,
    )
    legacy_reasons = _legacy_reasons(
        research_transitions,
        research_terminal,
        solver_steps,
        solver_terminal,
        research_abort_journal,
    )
    payload_values = {
        "artifact_version": 3,
        "run_id": run_id,
        "run_incarnation": run_incarnation,
        "schema_namespace_version": schema,
        "historical_status": (
            HistoricalReplayStatus.UNVERIFIABLE
            if legacy_reasons
            else HistoricalReplayStatus.VERIFIED
        ),
        "legacy_reasons": legacy_reasons,
        "query_specs": first.query_specs,
        "research_snapshots": research_snapshots,
        "research_transitions": research_transitions,
        "research_abort_journal": research_abort_journal,
        "research_terminal": research_terminal,
        "solver_snapshots": solver_snapshots,
        "solver_steps": solver_steps,
        "solver_terminal": solver_terminal,
        "budget_records": first.budget_records,
        "model_budget_records": first.model_budget_records,
        "artifact_references": references,
    }
    attachments = _artifact_attachments(
        references,
        read_artifact,
        payload_projection=payload_values,
    )
    return encode_replay_artifact(
        AdaptiveReplayPayload(
            **payload_values,
            artifact_attachments=attachments,
        )
    )


def _load_authorities(
    run_id,
    run_incarnation,
    checkpoint_store,
    research_store,
    solver_store,
    budget_ledger,
) -> _ReplayAuthorities:
    research_states = research_store.load_research_state_chain(
        run_id,
        run_incarnation,
    )
    research_inputs = tuple(
        research_store.load_research_replay_input(
            run_id,
            run_incarnation,
            state.revision,
        )
        for state in research_states[1:]
    )
    events = checkpoint_store.load_run_events(
        run_id,
        run_incarnation,
        AdaptiveLoopKind.RESEARCH,
    )
    terminal_event = next(
        (
            event
            for event in reversed(events)
            if event.phase is AdaptiveActionPhase.TERMINAL
        ),
        None,
    )
    terminal_input = (
        None
        if terminal_event is None
        else checkpoint_store.load_terminal_replay_input(terminal_event.key)
    )
    solver_chain = solver_store.load_replay_chain(run_id, run_incarnation)
    solver_inputs = (
        ()
        if solver_chain is None
        else tuple(
            solver_store.load_transition_replay_input(
                run_id,
                run_incarnation,
                action.action_revision,
            )
            if action.action_kind == "transition"
            else None
            for action in solver_chain.actions
        )
    )
    return _ReplayAuthorities(
        query_specs=research_store.load_query_spec_chain(run_id, run_incarnation),
        research_states=research_states,
        research_inputs=research_inputs,
        research_events=events,
        research_terminal_input=terminal_input,
        solver_chain=solver_chain,
        solver_inputs=solver_inputs,
        budget_records=budget_ledger.load_records(run_id, run_incarnation),
        model_budget_records=budget_ledger.load_model_records(
            run_id,
            run_incarnation,
        ),
    )


def _validate_identity(run_id, run_incarnation, authorities) -> str:
    contracts = [*authorities.query_specs, *authorities.research_states]
    if authorities.solver_chain is not None:
        chain = authorities.solver_chain
        if chain.run_id != run_id or chain.run_incarnation != run_incarnation:
            raise ReplayContractError("solver replay identity does not match request")
        contracts.extend(snapshot.state for snapshot in chain.snapshots)
    if not contracts:
        raise ReplayContractError("no durable adaptive state exists for replay")
    if any(
        contract.run_id != run_id or contract.run_incarnation != run_incarnation
        for contract in contracts
    ):
        raise ReplayContractError("durable replay identity does not match request")
    schemas = {contract.schema_namespace_version for contract in contracts}
    if len(schemas) != 1:
        raise ReplayContractError("durable replay schema namespace is inconsistent")
    stored_queries = set(authorities.query_specs)
    embedded = [state.query_spec for state in authorities.research_states]
    if authorities.solver_chain is not None:
        embedded.extend(
            snapshot.state.query_spec for snapshot in authorities.solver_chain.snapshots
        )
    if any(query not in stored_queries for query in embedded):
        raise ReplayContractError("embedded QuerySpec is absent from durable chain")
    return schemas.pop()


def _research_transitions(
    authorities: _ReplayAuthorities,
    events: dict[tuple[int, AdaptiveActionPhase], AdaptiveCheckpointEvent],
) -> tuple[ResearchReplayTransition, ...]:
    transitions = []
    typed_seen = False
    for before, after, replay_input in zip(
        authorities.research_states[:-1],
        authorities.research_states[1:],
        authorities.research_inputs,
        strict=True,
    ):
        planned_event = events.get((before.revision, AdaptiveActionPhase.PLANNED))
        observed_event = events.get((before.revision, AdaptiveActionPhase.OBSERVED))
        if replay_input is None:
            if typed_seen:
                raise ReplayContractError("research replay input gap follows v3 input")
            planned = (
                None
                if planned_event is None
                else _exact_action(planned_event, ResearchPlannedReplayAction)
            )
            observed = (
                None
                if observed_event is None
                else _exact_action(observed_event, ResearchObservedReplayAction)
            )
        else:
            typed_seen = True
            if planned_event is None or observed_event is None:
                raise ReplayContractError(
                    "replayable research transition lacks journal actions"
                )
            planned = _exact_action(planned_event, ResearchPlannedReplayAction)
            observed = _exact_action(observed_event, ResearchObservedReplayAction)
        transitions.append(
            ResearchReplayTransition(
                predecessor_revision=before.revision,
                predecessor_digest=_state_digest(before),
                successor_revision=after.revision,
                successor_digest=_state_digest(after),
                planned=planned,
                planned_digest=(
                    None if planned_event is None else planned_event.action_digest
                ),
                observed=observed,
                observed_digest=(
                    None if observed_event is None else observed_event.action_digest
                ),
                replay_input=replay_input,
            )
        )
    return tuple(transitions)


def _research_event_index(
    authorities: _ReplayAuthorities,
) -> dict[tuple[int, AdaptiveActionPhase], AdaptiveCheckpointEvent]:
    events: dict[tuple[int, AdaptiveActionPhase], AdaptiveCheckpointEvent] = {}
    terminal_revisions = tuple(
        event.key.revision
        for event in authorities.research_events
        if event.phase is AdaptiveActionPhase.TERMINAL
    )
    final_terminal_revision = terminal_revisions[-1] if terminal_revisions else None
    for event in authorities.research_events:
        if (
            event.phase is AdaptiveActionPhase.TERMINAL
            and event.key.revision != final_terminal_revision
        ):
            continue
        key = (event.key.revision, event.phase)
        if key in events:
            raise ReplayContractError("research journal event identity is duplicated")
        events[key] = event
    return events


def _research_abort_journal(
    authorities: _ReplayAuthorities,
    snapshots: tuple[ResearchReplaySnapshot, ...],
    events: dict[tuple[int, AdaptiveActionPhase], AdaptiveCheckpointEvent],
) -> ResearchReplayAbortJournal | None:
    if not snapshots:
        return None
    revision = snapshots[-1].state.revision
    planned_event = events.get((revision, AdaptiveActionPhase.PLANNED))
    observed_event = events.get((revision, AdaptiveActionPhase.OBSERVED))
    if planned_event is None and observed_event is None:
        return None
    if planned_event is None or observed_event is None:
        raise ReplayContractError("final research journal pair is incomplete")
    observed_action = observed_event.action
    if not isinstance(observed_action, Mapping) or observed_action.get("kind") != (
        "research_aborted"
    ):
        raise ReplayContractError("final research journal has no successor state")
    return ResearchReplayAbortJournal(
        state_revision=revision,
        state_digest=snapshots[-1].digest,
        planned=_exact_action(planned_event, ResearchPlannedReplayAction),
        planned_digest=planned_event.action_digest,
        aborted=_exact_action(observed_event, ResearchAbortedReplayAction),
        aborted_digest=observed_event.action_digest,
    )


def _research_terminal(
    authorities: _ReplayAuthorities,
    snapshots: tuple[ResearchReplaySnapshot, ...],
    indexed_events: dict[tuple[int, AdaptiveActionPhase], AdaptiveCheckpointEvent],
) -> ResearchReplayTerminal | None:
    events = tuple(
        event
        for event in authorities.research_events
        if event.phase is AdaptiveActionPhase.TERMINAL
    )
    if not events:
        return None
    if not snapshots:
        raise ReplayContractError("research terminal journal is inconsistent")
    event = events[-1]
    if event.key.revision != snapshots[-1].state.revision:
        raise ReplayContractError("research terminal revision is not final")
    if authorities.research_terminal_input is None and any(
        replay_input is not None for replay_input in authorities.research_inputs
    ):
        raise ReplayContractError("research terminal input is missing after v3 input")
    expected_keys = {
        (revision, phase)
        for revision in range(max(0, len(snapshots) - 1))
        for phase in (AdaptiveActionPhase.PLANNED, AdaptiveActionPhase.OBSERVED)
    }
    final_revision = snapshots[-1].state.revision
    if (final_revision, AdaptiveActionPhase.PLANNED) in indexed_events:
        expected_keys.update(
            {
                (final_revision, AdaptiveActionPhase.PLANNED),
                (final_revision, AdaptiveActionPhase.OBSERVED),
            }
        )
    expected_keys.add((final_revision, AdaptiveActionPhase.TERMINAL))
    if set(indexed_events) != expected_keys:
        raise ReplayContractError("research journal contains unreferenced events")
    return ResearchReplayTerminal(
        state_revision=event.key.revision,
        state_digest=snapshots[-1].digest,
        action=_exact_action(event, ResearchTerminalReplayAction),
        action_digest=event.action_digest,
        replay_input=authorities.research_terminal_input,
    )


def _solver_chain(
    authorities: _ReplayAuthorities,
) -> tuple[
    tuple[SolverReplaySnapshot, ...],
    tuple[SolverTransitionReplayStep | SolverExecutionReplayStep, ...],
    SolverReplayTerminal | None,
]:
    chain = authorities.solver_chain
    if chain is None:
        return (), (), None
    snapshots = tuple(
        SolverReplaySnapshot(
            state=snapshot.state,
            digest=snapshot.state_digest,
            source_action_revision=snapshot.source_action_revision,
        )
        for snapshot in chain.snapshots
    )
    reconciliations = {item.action_revision: item for item in chain.reconciliations}
    steps = []
    typed_seen = False
    for action, replay_input in zip(
        chain.actions,
        authorities.solver_inputs,
        strict=True,
    ):
        if action.action_kind == "transition":
            parsed = _solver_transition_action(action)
            requires = type(parsed) in (
                SolverAction,
                SolverReentryAdmittedReplayAction,
            ) or (
                type(parsed) is SolverReentryFinalizedReplayAction
                and parsed.record.status is ResearchReentryStatus.COMPLETED
            )
            if requires and replay_input is None:
                if typed_seen:
                    raise ReplayContractError(
                        "solver replay input gap follows v3 input"
                    )
            elif replay_input is not None:
                typed_seen = True
            steps.append(
                SolverTransitionReplayStep(
                    action_revision=action.action_revision,
                    base_state_revision=action.base_state_revision,
                    base_state_digest=action.base_state_digest,
                    result_state_revision=_required_int(
                        action.result_state_revision,
                        "solver transition result revision",
                    ),
                    result_state_digest=_required_text(
                        action.result_state_digest,
                        "solver transition result digest",
                    ),
                    action=parsed,
                    action_digest=action.action_digest,
                    replay_input=replay_input,
                    created_at_ns=action.created_at_ns,
                )
            )
            continue
        reconciliation = reconciliations.get(action.action_revision)
        if reconciliation is None:
            raise ReplayContractError("execution action lacks reconciliation")
        steps.append(_execution_step(action, reconciliation))
    if set(reconciliations) != {
        step.action_revision
        for step in steps
        if type(step) is SolverExecutionReplayStep
    }:
        raise ReplayContractError("solver reconciliation is duplicated or unreferenced")
    terminal = (
        None
        if chain.terminal is None
        else SolverReplayTerminal(
            state_revision=chain.terminal.state_revision,
            state_digest=chain.terminal.state_digest,
            next_action_revision=chain.terminal.next_action_revision,
            terminal=_blob(
                canonical_json_bytes(chain.terminal.terminal),
                chain.terminal.terminal_digest,
            ),
            created_at_ns=chain.terminal.created_at_ns,
        )
    )
    return snapshots, tuple(steps), terminal


def _solver_transition_action(action: SolverReplayAction):
    raw = action.action
    if not isinstance(raw, Mapping):
        raise ReplayContractError("solver transition action must be an object")
    raw = dict(raw)
    kind = raw.get("kind")
    model = {
        "sql_candidate": SolverAction,
        "missing_evidence": SolverAction,
        "solver_check": SolverCheckReplayAction,
        "research_reentry_admitted": SolverReentryAdmittedReplayAction,
        "research_reentry_finalized": SolverReentryFinalizedReplayAction,
        "semantic_repair_fallback": SolverSemanticRepairFallbackReplayAction,
        "solver_stop": SolverStopReplayAction,
    }.get(kind)
    if model is None:
        raise ReplayContractError("solver transition action kind is unsupported")
    return _exact_value(raw, model, "solver transition action")


def _execution_step(
    action: SolverReplayAction,
    reconciliation: SolverReplayReconciliation,
) -> SolverExecutionReplayStep:
    parsed = _exact_value(
        dict(action.action) if isinstance(action.action, Mapping) else action.action,
        SolverExecutionReplayAction,
        "solver execution action",
    )
    if (
        parsed.candidate_id != action.candidate_id
        or parsed.execution_id != action.execution_id
        or parsed.normalized_ast_digest != action.normalized_ast_digest
    ):
        raise ReplayContractError("execution action columns do not match payload")
    result_bytes = canonical_json_bytes(reconciliation.result)
    return SolverExecutionReplayStep(
        action_revision=action.action_revision,
        base_state_revision=action.base_state_revision,
        base_state_digest=action.base_state_digest,
        action=parsed,
        action_digest=action.action_digest,
        reconciliation=SolverExecutionReconciliation(
            outcome=reconciliation.outcome,
            result_state_revision=reconciliation.result_state_revision,
            result_state_digest=reconciliation.result_state_digest,
            result=_blob(result_bytes, reconciliation.result_digest),
            created_at_ns=reconciliation.created_at_ns,
        ),
        created_at_ns=action.created_at_ns,
    )


def _validate_budget_links(
    authorities: _ReplayAuthorities,
    transitions: tuple[ResearchReplayTransition, ...],
) -> None:
    identities: set[tuple[int, str]] = set()
    for record in authorities.budget_records:
        identity = (
            record.reservation.revision,
            record.reservation.action_digest,
        )
        if identity in identities:
            raise ReplayContractError("research budget record is duplicated")
        identities.add(identity)
    for transition in transitions:
        replay_input = transition.replay_input
        if replay_input is None:
            continue
        planned = transition.planned
        if planned is None:
            raise ReplayContractError("research transition has no planned action")
        if replay_input.probe_result is None:
            if planned.action.kind is not ResearchActionKind.SEMANTIC_COMMIT:
                raise ReplayContractError("probe transition has no budget record")
            continue
        matches = tuple(
            record
            for record in authorities.budget_records
            if record.reservation.revision == transition.predecessor_revision
            and record.reservation.action_digest == planned.action.action_digest
        )
        record = matches[0] if len(matches) == 1 else None
        if (
            record is None
            or record.result != replay_input.probe_result
            or record.reconciliation is None
            or record.reconciliation.budget_after != replay_input.budget_state
        ):
            raise ReplayContractError("research transition budget record is missing")


def _artifact_references(
    transitions: tuple[ResearchReplayTransition, ...],
    budget_records: tuple[BudgetLedgerRecord, ...] = (),
) -> tuple[ArtifactReference, ...]:
    references = [
        reference
        for transition in transitions
        if transition.replay_input is not None
        and transition.replay_input.probe_result is not None
        for reference in (transition.replay_input.probe_result.artifact_reference,)
        if reference is not None
    ]
    references.extend(
        reference
        for record in budget_records
        if record.result is not None
        for reference in (record.result.artifact_reference,)
        if reference is not None
    )
    try:
        return dedupe_artifact_references(references)
    except ValueError as exc:
        raise ReplayContractError("conflicting artifact reference identity") from exc


def _artifact_attachments(
    references: tuple[ArtifactReference, ...],
    read_artifact: ArtifactReader | None,
    *,
    payload_projection: Mapping[str, object] | None = None,
) -> tuple[ReplayArtifactAttachment, ...]:
    if references and read_artifact is None:
        raise ReplayContractError("artifact-backed replay requires artifact reader")
    if read_artifact is not None and not callable(read_artifact):
        raise TypeError("read_artifact must be callable")
    if any(
        reference.byte_count > MAX_REPLAY_ATTACHMENT_BYTES for reference in references
    ):
        raise ReplayContractError("attachment byte_count exceeds replay bound")
    if len(references) > MAX_REPLAY_ATTACHMENT_COUNT:
        raise ReplayContractError("attachment count exceeds replay bound")
    encoded_lengths = tuple(
        4 * ((reference.byte_count + 2) // 3) for reference in references
    )
    if sum(encoded_lengths) > DEFAULT_LIMITS.max_state_bytes:
        raise ReplayContractError("attachment base64 aggregate exceeds replay state bound")
    if payload_projection is not None:
        _preflight_replay_artifact_size(
            payload_projection,
            references,
            encoded_lengths,
        )
    return tuple(
        ReplayArtifactAttachment(
            reference=reference,
            content_base64=base64.b64encode(
                verify_artifact_reference(reference, read_artifact)
            ).decode("ascii"),
        )
        for reference in references
    )


def _preflight_replay_artifact_size(
    payload_projection: Mapping[str, object],
    references: tuple[ArtifactReference, ...],
    encoded_lengths: tuple[int, ...],
) -> None:
    projected_payload = {
        **payload_projection,
        "artifact_attachments": tuple(
            {
                "reference": reference,
                "content_base64": "A" * encoded_length,
            }
            for reference, encoded_length in zip(
                references,
                encoded_lengths,
                strict=True,
            )
        ),
    }
    try:
        payload_bytes = canonical_json_bytes(
            projected_payload,
            limits=DEFAULT_LIMITS,
        )
        canonical_json_bytes(
            {
                "artifact_version": 3,
                "record_kind": "adaptive_replay",
                "payload": projected_payload,
                "payload_digest": sha256_digest(payload_bytes),
                "byte_count": len(payload_bytes),
            },
            limits=DEFAULT_LIMITS,
        )
    except StateSizeLimitError as exc:
        raise ReplayContractError(
            "projected replay artifact exceeds max_state_bytes"
        ) from exc


def _legacy_reasons(
    transitions,
    terminal,
    solver_steps,
    solver_terminal,
    research_abort_journal,
):
    reasons = set()
    typed = solver_terminal is not None or bool(
        solver_steps
        and type(solver_steps[-1]) is SolverExecutionReplayStep
        and _result_contradiction_receipt(solver_steps[-1]) is not None
    )
    if research_abort_journal is not None:
        reasons.add(LegacyReplayReason.RESEARCH_ABORT_INPUT)
    if any(item.replay_input is None for item in transitions):
        reasons.add(LegacyReplayReason.RESEARCH_TRANSITION_INPUT)
    if any(item.replay_input is not None for item in transitions):
        typed = True
    if terminal is not None and terminal.replay_input is None:
        reasons.add(LegacyReplayReason.RESEARCH_TERMINAL_INPUT)
    elif terminal is not None:
        typed = True
    for step in solver_steps:
        if type(step) is not SolverTransitionReplayStep:
            continue
        action = step.action
        requires = type(action) in (
            SolverAction,
            SolverReentryAdmittedReplayAction,
        ) or (
            type(action) is SolverReentryFinalizedReplayAction
            and action.record.status is ResearchReentryStatus.COMPLETED
        )
        if requires and step.replay_input is None:
            reasons.add(LegacyReplayReason.SOLVER_TRANSITION_INPUT)
        elif requires:
            typed = True
    if not typed:
        reasons.add(LegacyReplayReason.NO_TYPED_PROVENANCE)
    return tuple(sorted(reasons, key=lambda item: item.value))


def _exact_action(event, model):
    return _exact_value(event.action, model, "research journal action")


def _exact_value(value, model, label):
    try:
        checked = model.model_validate_json(canonical_json_bytes(value))
    except (TypeError, ValueError) as exc:
        raise ReplayContractError(f"{label} is invalid") from exc
    if canonical_json_bytes(
        checked.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
            round_trip=True,
            warnings="error",
        )
    ) != canonical_json_bytes(value):
        raise ReplayContractError(f"{label} is not exact")
    return checked


def _blob(content: bytes, digest: str) -> CanonicalReplayBlob:
    return CanonicalReplayBlob(
        digest=digest,
        byte_count=len(content),
        content_base64=base64.b64encode(content).decode("ascii"),
    )


def _state_digest(state) -> str:
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest

    return canonical_digest(state)


def _required_int(value: int | None, label: str) -> int:
    if type(value) is not int:
        raise ReplayContractError(f"{label} is missing")
    return value


def _required_text(value: str | None, label: str) -> str:
    if type(value) is not str:
        raise ReplayContractError(f"{label} is missing")
    return value


def _require_store(value: object, expected_type: type, label: str) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(f"{label} must be {expected_type.__name__}")


__all__ = ["build_adaptive_replay_artifact"]
