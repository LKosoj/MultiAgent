"""Closed data contract for deterministic adaptive replay artifacts."""

from __future__ import annotations

import base64
from enum import StrEnum
import hashlib
import json
import re
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, ValidationError, model_validator

from .freshness import FreshnessProjection
from .ambiguity import AmbiguityReport
from .model_budget import ModelBudgetLedgerRecord
from .models import (
    CheckResult,
    Digest,
    EvidenceValidityScope,
    Id,
    NonEmptyText,
    NonNegativeInt,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchReentryRecord,
    ResearchReentryStatus,
    ResearchState,
    ResearchStopReason,
    SolverAction,
    SolverState,
    SolverStopReason,
    StrictModel,
)
from .policy import BudgetLedgerRecord
from .probes import ProbeResult
from .replay_inputs import (
    ResearchSemanticReplayInput,
    ResearchTerminalReplayInput,
    SolverMissingEvidenceReplayInput,
    SolverReentryAdmissionReplayInput,
    SolverReentryCompletedReplayInput,
    SolverSqlProposalReplayInput,
)
from .research_decision import ResearchDecisionV1
from .serialization import (
    ArtifactReference,
    CanonicalJsonError,
    canonical_digest,
    canonical_json_bytes,
)


class ReplayContractError(ValueError):
    """The artifact is malformed, incomplete, or inconsistent."""


class HistoricalReplayStatus(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIABLE = "UNVERIFIABLE"


class EvidenceReuseStatus(StrEnum):
    REUSABLE = "REUSABLE"
    REVALIDATION_REQUIRED = "REVALIDATION_REQUIRED"


class LegacyReplayReason(StrEnum):
    NO_TYPED_PROVENANCE = "NO_TYPED_PROVENANCE"
    RESEARCH_ABORT_INPUT = "MISSING_RESEARCH_ABORT_REDUCER_INPUT"
    RESEARCH_TRANSITION_INPUT = "MISSING_RESEARCH_TRANSITION_INPUT"
    RESEARCH_TERMINAL_INPUT = "MISSING_RESEARCH_TERMINAL_INPUT"
    SOLVER_TRANSITION_INPUT = "MISSING_SOLVER_TRANSITION_INPUT"


class ReplayArtifactAttachment(StrictModel):
    reference: ArtifactReference
    content_base64: NonEmptyText

    @model_validator(mode="after")
    def validate_content(self) -> ReplayArtifactAttachment:
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except ValueError as exc:
            raise ValueError("artifact attachment is not canonical base64") from exc
        if base64.b64encode(content).decode("ascii") != self.content_base64:
            raise ValueError("artifact attachment is not canonical base64")
        if (
            len(content) != self.reference.byte_count
            or sha256_digest(content) != self.reference.digest
        ):
            raise ValueError("artifact attachment does not match its reference")
        return self

    def content(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)


class ResearchReplaySnapshot(StrictModel):
    state: ResearchState
    digest: Digest

    @model_validator(mode="after")
    def validate_digest(self) -> ResearchReplaySnapshot:
        if canonical_digest(self.state) != self.digest:
            raise ValueError("research snapshot digest does not match state")
        return self


class SolverReplaySnapshot(StrictModel):
    state: SolverState
    digest: Digest
    source_action_revision: NonNegativeInt | None

    @model_validator(mode="after")
    def validate_digest(self) -> SolverReplaySnapshot:
        if canonical_digest(self.state) != self.digest:
            raise ValueError("solver snapshot digest does not match state")
        return self


class ResearchPlannedReplayAction(StrictModel):
    contract_version: Literal[1] = 1
    kind: Literal["research_planned"] = "research_planned"
    action: ResearchAction
    decision: ResearchDecisionV1
    invocation_id: Id | None
    resolution_digest: Digest
    state_digest: Digest

    @model_validator(mode="after")
    def validate_invocation(self) -> ResearchPlannedReplayAction:
        if (self.action.kind is ResearchActionKind.SEMANTIC_COMMIT) != (
            self.invocation_id is None
        ):
            raise ValueError("only semantic_commit may omit invocation_id")
        return self


class ResearchObservedReplayAction(StrictModel):
    contract_version: Literal[1] = 1
    kind: Literal["research_observed"] = "research_observed"
    novel: bool
    result: ProbeResult | None
    resolution_digest: Digest

class ResearchAbortedReplayAction(StrictModel):
    contract_version: Literal[1] = 1
    kind: Literal["research_aborted"] = "research_aborted"
    action: ResearchAction
    reason: ResearchStopReason
    resolution_digest: Digest


class ResearchTerminalReplayAction(StrictModel):
    contract_version: Literal[2]
    kind: Literal["research_terminal"]
    reason: ResearchStopReason
    affected_source_ids: tuple[Id, ...]
    citation_evidence_ids: tuple[Id, ...]
    ambiguity: AmbiguityReport | None
    rejection_signatures: tuple[tuple[NonEmptyText, NonEmptyText], ...]

    @model_validator(mode="after")
    def validate_ids(self) -> ResearchTerminalReplayAction:
        for values in (self.affected_source_ids, self.citation_evidence_ids):
            if values != tuple(sorted(set(values))):
                raise ValueError("research terminal IDs must be sorted and unique")
        if (self.reason is ResearchStopReason.AMBIGUOUS) != (
            self.ambiguity is not None
        ):
            raise ValueError("research terminal ambiguity must match its reason")
        if (
            self.ambiguity is not None
            and self.ambiguity.citation_evidence_ids != self.citation_evidence_ids
        ):
            raise ValueError("research terminal ambiguity citations must match")
        if self.rejection_signatures != tuple(sorted(set(self.rejection_signatures))):
            raise ValueError("research terminal rejection signatures must be sorted and unique")
        if self.reason is not ResearchStopReason.STAGNATED and self.rejection_signatures:
            raise ValueError("only stagnated terminals may have rejection signatures")
        return self


class ResearchReplayTransition(StrictModel):
    predecessor_revision: NonNegativeInt
    predecessor_digest: Digest
    successor_revision: NonNegativeInt
    successor_digest: Digest
    planned: ResearchPlannedReplayAction | None
    planned_digest: Digest | None
    observed: ResearchObservedReplayAction | None
    observed_digest: Digest | None
    replay_input: ResearchSemanticReplayInput | None

    @model_validator(mode="after")
    def validate_shape(self) -> ResearchReplayTransition:
        if self.successor_revision != self.predecessor_revision + 1:
            raise ValueError("research transition revisions are not contiguous")
        if (self.planned is None) != (self.planned_digest is None) or (
            self.observed is None
        ) != (self.observed_digest is None):
            raise ValueError("research journal action and digest must be paired")
        if (self.planned is None) != (self.observed is None):
            raise ValueError("research planned and observed actions must be paired")
        if self.planned is not None and (
            durable_action_digest(self.planned) != self.planned_digest
            or durable_action_digest(self.observed) != self.observed_digest
        ):
            raise ValueError("research journal action digest does not match")
        if self.replay_input is not None:
            if self.planned is None or self.observed is None:
                raise ValueError("replayable research transition requires its journal")
            if (
                self.replay_input.planned_action_digest != self.planned_digest
                or self.replay_input.observed_action_digest != self.observed_digest
                or self.planned.decision != self.replay_input.decision
                or self.planned.resolution_digest != self.observed.resolution_digest
            ):
                raise ValueError("research replay input does not match its journal")
            semantic_only = (
                self.planned.action.kind is ResearchActionKind.SEMANTIC_COMMIT
            )
            if semantic_only != (
                self.observed.result is None
                and self.replay_input.probe_result is None
                and self.planned.invocation_id is None
            ):
                raise ValueError("semantic replay journal has invalid probe linkage")
            if not semantic_only and (
                self.observed.result != self.replay_input.probe_result
                or self.planned.action.action_digest
                != self.replay_input.probe_result.action_digest
                or self.planned.invocation_id
                != self.replay_input.probe_result.invocation_id
            ):
                raise ValueError("research replay input does not match probe journal")
        return self


class ResearchReplayAbortJournal(StrictModel):
    state_revision: NonNegativeInt
    state_digest: Digest
    planned: ResearchPlannedReplayAction
    planned_digest: Digest
    aborted: ResearchAbortedReplayAction
    aborted_digest: Digest

    @model_validator(mode="after")
    def validate_journal(self) -> ResearchReplayAbortJournal:
        if (
            durable_action_digest(self.planned) != self.planned_digest
            or durable_action_digest(self.aborted) != self.aborted_digest
        ):
            raise ValueError("research abort journal action digest does not match")
        if (
            self.planned.action != self.aborted.action
            or self.planned.resolution_digest != self.aborted.resolution_digest
            or self.planned.action.expected_revision != self.state_revision
            or self.planned.state_digest != self.state_digest
        ):
            raise ValueError("research abort journal links are inconsistent")
        return self


class ResearchReplayTerminal(StrictModel):
    state_revision: NonNegativeInt
    state_digest: Digest
    action: ResearchTerminalReplayAction
    action_digest: Digest
    replay_input: ResearchTerminalReplayInput | None

    @model_validator(mode="after")
    def validate_action_digest(self) -> ResearchReplayTerminal:
        if durable_action_digest(self.action) != self.action_digest:
            raise ValueError("research terminal action digest does not match")
        return self


class SolverCheckReplayAction(StrictModel):
    kind: Literal["solver_check"] = "solver_check"
    check: CheckResult


class SolverReentryAdmittedReplayAction(StrictModel):
    kind: Literal["research_reentry_admitted"] = "research_reentry_admitted"
    record: ResearchReentryRecord

    @model_validator(mode="after")
    def validate_status(self) -> SolverReentryAdmittedReplayAction:
        if self.record.status is not ResearchReentryStatus.ADMITTED:
            raise ValueError("re-entry admission action requires ADMITTED status")
        return self


class SolverReentryFinalizedReplayAction(StrictModel):
    kind: Literal["research_reentry_finalized"] = "research_reentry_finalized"
    record: ResearchReentryRecord

    @model_validator(mode="after")
    def validate_status(self) -> SolverReentryFinalizedReplayAction:
        if self.record.status is ResearchReentryStatus.ADMITTED:
            raise ValueError("re-entry finalization requires terminal status")
        return self


class SolverStopReplayAction(StrictModel):
    kind: Literal["solver_stop"] = "solver_stop"
    reason: SolverStopReason


SolverTransitionReplayInput: TypeAlias = (
    SolverSqlProposalReplayInput
    | SolverMissingEvidenceReplayInput
    | SolverReentryAdmissionReplayInput
    | SolverReentryCompletedReplayInput
)


class SolverTransitionReplayStep(StrictModel):
    step_kind: Literal["transition"] = "transition"
    action_revision: NonNegativeInt
    base_state_revision: NonNegativeInt
    base_state_digest: Digest
    result_state_revision: NonNegativeInt
    result_state_digest: Digest
    action: SolverReplayAction
    action_digest: Digest
    replay_input: SolverTransitionReplayInput | None
    created_at_ns: NonNegativeInt

    @model_validator(mode="after")
    def validate_shape(self) -> SolverTransitionReplayStep:
        if self.result_state_revision != self.base_state_revision + 1:
            raise ValueError("solver transition revisions are not contiguous")
        if durable_action_digest(self.action) != self.action_digest:
            raise ValueError("solver transition action digest does not match")
        return self


class FinalizerExecutionRequest(StrictModel):
    operation: Literal["finalize_text_to_sql_run"] = "finalize_text_to_sql_run"
    sql_query: NonEmptyText
    row_limit: Annotated[int, Field(gt=0)]
    dry_run_only: bool


class CanonicalReplayBlob(StrictModel):
    digest: Digest
    byte_count: NonNegativeInt
    content_base64: NonEmptyText

    @model_validator(mode="after")
    def validate_content(self) -> CanonicalReplayBlob:
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except ValueError as exc:
            raise ValueError("replay blob is not canonical base64") from exc
        if (
            base64.b64encode(content).decode("ascii") != self.content_base64
            or len(content) != self.byte_count
            or sha256_digest(content) != self.digest
        ):
            raise ValueError("replay blob digest or byte count does not match")
        return self

    def content(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)


class SolverSemanticRepairFallbackReplayAction(StrictModel):
    kind: Literal["semantic_repair_fallback"] = "semantic_repair_fallback"
    missing_evidence_request_id: Id
    candidate_id: Id
    execution_id: Id
    normalized_ast_digest: Digest


class SolverExecutionReplayAction(StrictModel):
    candidate_id: Id
    execution_id: Id
    normalized_ast_digest: Digest
    request: FinalizerExecutionRequest


SolverReplayAction: TypeAlias = (
    SolverAction
    | SolverCheckReplayAction
    | SolverReentryAdmittedReplayAction
    | SolverReentryFinalizedReplayAction
    | SolverStopReplayAction
    | SolverSemanticRepairFallbackReplayAction
)


class SolverExecutionReconciliation(StrictModel):
    outcome: Literal["KNOWN", "UNKNOWN"]
    result_state_revision: NonNegativeInt
    result_state_digest: Digest
    result: CanonicalReplayBlob
    created_at_ns: NonNegativeInt

    @model_validator(mode="after")
    def validate_unknown_result(self) -> SolverExecutionReconciliation:
        if self.outcome == "UNKNOWN" and self.result.content() != b"null":
            raise ValueError("unknown execution result must be canonical null")
        return self


class SolverExecutionReplayStep(StrictModel):
    step_kind: Literal["execution"] = "execution"
    action_revision: NonNegativeInt
    base_state_revision: NonNegativeInt
    base_state_digest: Digest
    action: SolverExecutionReplayAction
    action_digest: Digest
    reconciliation: SolverExecutionReconciliation
    created_at_ns: NonNegativeInt

    @model_validator(mode="after")
    def validate_action_digest(self) -> SolverExecutionReplayStep:
        if durable_action_digest(self.action) != self.action_digest:
            raise ValueError("solver execution action digest does not match")
        if self.reconciliation.result_state_revision != self.base_state_revision + 1:
            raise ValueError("execution reconciliation revision is not contiguous")
        return self


SolverReplayStep: TypeAlias = Annotated[
    SolverTransitionReplayStep | SolverExecutionReplayStep,
    Field(discriminator="step_kind"),
]


class SolverReplayTerminal(StrictModel):
    state_revision: NonNegativeInt
    state_digest: Digest
    next_action_revision: NonNegativeInt
    terminal: CanonicalReplayBlob
    created_at_ns: NonNegativeInt


class AdaptiveReplayPayload(StrictModel):
    artifact_version: Literal[3] = 3
    run_id: Id
    run_incarnation: Id
    schema_namespace_version: Digest
    historical_status: HistoricalReplayStatus
    legacy_reasons: tuple[LegacyReplayReason, ...]
    query_specs: tuple[QuerySpec, ...]
    research_snapshots: tuple[ResearchReplaySnapshot, ...]
    research_transitions: tuple[ResearchReplayTransition, ...]
    research_abort_journal: ResearchReplayAbortJournal | None
    research_terminal: ResearchReplayTerminal | None
    solver_snapshots: tuple[SolverReplaySnapshot, ...]
    solver_steps: tuple[SolverReplayStep, ...]
    solver_terminal: SolverReplayTerminal | None
    budget_records: tuple[BudgetLedgerRecord, ...]
    model_budget_records: tuple[ModelBudgetLedgerRecord, ...]
    artifact_references: tuple[ArtifactReference, ...]
    artifact_attachments: tuple[ReplayArtifactAttachment, ...]

    @model_validator(mode="after")
    def validate_chain(self) -> AdaptiveReplayPayload:
        validate_payload_identity(self)
        validate_research_chain(self)
        validate_solver_chain(self)
        validate_budget_chains(self)
        validate_artifact_attachments(self)
        expected_reasons = legacy_reasons(self)
        if self.legacy_reasons != expected_reasons:
            raise ValueError("legacy replay reasons do not match durable provenance")
        expected_status = (
            HistoricalReplayStatus.UNVERIFIABLE
            if expected_reasons
            else HistoricalReplayStatus.VERIFIED
        )
        if self.historical_status is not expected_status:
            raise ValueError("historical status does not match replay completeness")
        return self


class ReplayArtifactEnvelope(StrictModel):
    artifact_version: Literal[3] = 3
    record_kind: Literal["adaptive_replay"] = "adaptive_replay"
    payload: AdaptiveReplayPayload
    payload_digest: Digest
    byte_count: NonNegativeInt


class HistoricalReplayResult(StrictModel):
    status: HistoricalReplayStatus
    legacy_reasons: tuple[LegacyReplayReason, ...]
    trusted_artifact_digest: Digest
    research_state_digest: Digest | None
    solver_state_digest: Digest | None
    verified_research_transition_count: NonNegativeInt
    verified_solver_transition_count: NonNegativeInt


class EvidenceReuseResult(StrictModel):
    status: EvidenceReuseStatus
    projection: FreshnessProjection
    historical_status: HistoricalReplayStatus
    trusted_artifact_digest: Digest


def validate_payload_identity(payload: AdaptiveReplayPayload) -> None:
    contracts = [
        *payload.query_specs,
        *(snapshot.state for snapshot in payload.research_snapshots),
        *(snapshot.state for snapshot in payload.solver_snapshots),
    ]
    if not contracts:
        raise ValueError("replay payload requires at least one durable state")
    if tuple(item.revision for item in payload.query_specs) != tuple(
        range(len(payload.query_specs))
    ):
        raise ValueError("query specification chain is not contiguous")
    if any(
        item.run_id != payload.run_id
        or item.run_incarnation != payload.run_incarnation
        or item.schema_namespace_version != payload.schema_namespace_version
        for item in contracts
    ):
        raise ValueError("replay payload durable identity is inconsistent")
    stored_queries = set(payload.query_specs)
    if any(
        state.query_spec not in stored_queries
        for state in (
            *(snapshot.state for snapshot in payload.research_snapshots),
            *(snapshot.state for snapshot in payload.solver_snapshots),
        )
    ):
        raise ValueError("embedded QuerySpec is absent from durable query chain")


def validate_research_chain(payload: AdaptiveReplayPayload) -> None:
    snapshots = payload.research_snapshots
    if any(
        current.state.revision + 1 != following.state.revision
        for current, following in zip(snapshots, snapshots[1:])
    ):
        raise ValueError("research snapshots are not contiguous")
    if len(payload.research_transitions) != max(0, len(snapshots) - 1):
        raise ValueError("research transition count does not match snapshots")
    typed_seen = False
    for transition, before, after in zip(
        payload.research_transitions,
        snapshots[:-1],
        snapshots[1:],
        strict=True,
    ):
        if transition.replay_input is None and typed_seen:
            raise ValueError("research replay input gap follows typed input")
        typed_seen = typed_seen or transition.replay_input is not None
        if (
            transition.predecessor_revision != before.state.revision
            or transition.predecessor_digest != before.digest
            or transition.successor_revision != after.state.revision
            or transition.successor_digest != after.digest
        ):
            raise ValueError("research transition does not join exact snapshots")
    abort = payload.research_abort_journal
    if abort is not None and (
        not snapshots
        or abort.state_revision != snapshots[-1].state.revision
        or abort.state_digest != snapshots[-1].digest
    ):
        raise ValueError("research abort journal does not match final snapshot")
    terminal = payload.research_terminal
    if terminal is not None and (
        not snapshots
        or terminal.state_revision != snapshots[-1].state.revision
        or terminal.state_digest != snapshots[-1].digest
    ):
        raise ValueError("research terminal does not match final snapshot")
    if abort is not None and (
        terminal is None or abort.aborted.reason is not terminal.action.reason
    ):
        raise ValueError("research abort reason does not match terminal reason")
    if typed_seen and terminal is not None and terminal.replay_input is None:
        raise ValueError("research terminal input is missing after typed input")


def validate_solver_chain(payload: AdaptiveReplayPayload) -> None:
    snapshots = payload.solver_snapshots
    if snapshots and snapshots[0].source_action_revision is not None:
        raise ValueError("initial solver snapshot has a source action")
    if any(
        current.state.revision + 1 != following.state.revision
        for current, following in zip(snapshots, snapshots[1:])
    ):
        raise ValueError("solver snapshots are not contiguous")
    if len(payload.solver_steps) != max(0, len(snapshots) - 1):
        raise ValueError("solver step count does not match snapshots")
    typed_seen = False
    for revision, (step, before, after) in enumerate(
        zip(payload.solver_steps, snapshots[:-1], snapshots[1:], strict=True)
    ):
        if type(step) is SolverTransitionReplayStep and requires_solver_input(
            step.action
        ):
            if step.replay_input is None and typed_seen:
                raise ValueError("solver replay input gap follows typed input")
            typed_seen = typed_seen or step.replay_input is not None
        result_revision = (
            step.result_state_revision
            if type(step) is SolverTransitionReplayStep
            else step.reconciliation.result_state_revision
        )
        result_digest = (
            step.result_state_digest
            if type(step) is SolverTransitionReplayStep
            else step.reconciliation.result_state_digest
        )
        if (
            step.action_revision != revision
            or step.base_state_revision != before.state.revision
            or step.base_state_digest != before.digest
            or result_revision != after.state.revision
            or result_digest != after.digest
            or after.source_action_revision != revision
        ):
            raise ValueError("solver step does not join exact snapshots")
    terminal = payload.solver_terminal
    if terminal is not None and (
        not snapshots
        or terminal.state_revision != snapshots[-1].state.revision
        or terminal.state_digest != snapshots[-1].digest
        or terminal.next_action_revision != len(payload.solver_steps)
    ):
        raise ValueError("solver terminal does not match final snapshot")
    final_execution = (
        payload.solver_steps[-1]
        if payload.solver_steps
        and type(payload.solver_steps[-1]) is SolverExecutionReplayStep
        else None
    )
    terminal_required = bool(
        snapshots and snapshots[-1].state.stop_reason is not None
    ) or (
        final_execution is not None
        and _result_contradiction_receipt(final_execution) is None
    )
    if terminal_required and terminal is None:
        raise ValueError("final solver state requires terminal authority")


def _result_contradiction_receipt(
    step: SolverExecutionReplayStep,
):
    if step.reconciliation.outcome != "KNOWN":
        return None
    try:
        value = json.loads(step.reconciliation.result.content())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if (
        type(value) is not dict
        or value.get("record_kind")
        not in {"text2sql_result_contradiction", "text2sql_result_review"}
    ):
        return None
    from .result_review import ResultReviewReceipt
    from .result_validation import ResultContradictionReceipt

    try:
        receipt_type = (
            ResultContradictionReceipt
            if value["record_kind"] == "text2sql_result_contradiction"
            else ResultReviewReceipt
        )
        receipt = receipt_type.model_validate_json(canonical_json_bytes(value))
        if (
            type(receipt) is ResultReviewReceipt
            and receipt.verdict not in {"contradicted", "ambiguous"}
        ):
            return None
        return receipt
    except (CanonicalJsonError, ValidationError):
        return None


def validate_budget_chains(payload: AdaptiveReplayPayload) -> None:
    _validate_probe_budget_records(payload)
    _validate_model_budget_records(payload)


def _validate_probe_budget_records(payload: AdaptiveReplayPayload) -> None:
    records = payload.budget_records
    seen_reservations: set[str] = set()
    seen_actions: set[str] = set()
    seen_invocations: set[str] = set()
    planned = {
        item.predecessor_revision: item.planned
        for item in payload.research_transitions
        if item.planned is not None
    }
    if payload.research_abort_journal is not None:
        planned[payload.research_abort_journal.state_revision] = (
            payload.research_abort_journal.planned
        )
    previous_after = None
    previous_revision = -1
    for index, record in enumerate(records):
        reservation = record.reservation
        if (
            reservation.run_id != payload.run_id
            or reservation.run_incarnation != payload.run_incarnation
            or reservation.schema_namespace_version != payload.schema_namespace_version
        ):
            raise ValueError("budget record identity does not match replay payload")
        if reservation.revision <= previous_revision:
            raise ValueError("budget ledger revisions must be strictly ordered")
        previous_revision = reservation.revision
        if (
            reservation.reservation_digest in seen_reservations
            or reservation.action_digest in seen_actions
        ):
            raise ValueError("budget ledger identity is duplicated")
        seen_reservations.add(reservation.reservation_digest)
        seen_actions.add(reservation.action_digest)
        if previous_after is not None and not _same_probe_budget(
            reservation.budget_before, previous_after
        ):
            raise ValueError("budget ledger has a broken cost chain")
        journal_action = planned.get(reservation.revision)
        if journal_action is None or (
            journal_action.action.action_digest != reservation.action_digest
        ):
            raise ValueError("budget ledger record is unreferenced")
        if record.result is not None:
            if record.result.invocation_id in seen_invocations:
                raise ValueError("probe invocation identity is duplicated")
            seen_invocations.add(record.result.invocation_id)
        if record.reconciliation is None:
            if index != len(records) - 1:
                raise ValueError("only the last budget reservation may be outstanding")
            previous_after = None
        else:
            previous_after = record.reconciliation.budget_after
    by_revision = {record.reservation.revision: record for record in records}
    for transition in payload.research_transitions:
        if transition.replay_input is None:
            continue
        if transition.replay_input.probe_result is None:
            if (
                transition.planned is None
                or transition.planned.action.kind
                is not ResearchActionKind.SEMANTIC_COMMIT
            ):
                raise ValueError("probe transition has no budget record")
            continue
        record = by_revision.get(transition.predecessor_revision)
        if (
            record is None
            or record.result != transition.replay_input.probe_result
            or record.reconciliation is None
            or record.reconciliation.budget_after
            != transition.replay_input.budget_state
        ):
            raise ValueError("research transition budget record is missing")
    if records and payload.research_snapshots:
        final_budget = payload.research_snapshots[-1].state.budget_state
        final_record = records[-1]
        expected_budget = (
            final_record.reservation.budget_before
            if final_record.reconciliation is None
            else final_record.reconciliation.budget_after
        )
        if not _same_probe_budget(final_budget, expected_budget):
            raise ValueError("research state omits durable probe budget records")


_MODEL_CALL_ID = re.compile(r"^research-model-(?P<revision>\d+)-(?P<attempt>\d+)$")


def _validate_model_budget_records(payload: AdaptiveReplayPayload) -> None:
    records = payload.model_budget_records
    if not records:
        if payload.research_snapshots:
            budget = payload.research_snapshots[-1].state.budget_state
            if budget.used_model_calls != 0 or budget.used_model_tokens != 0:
                raise ValueError("research state model usage has no durable ledger")
        return
    if not payload.research_snapshots:
        raise ValueError("model budget ledger has no research authority")
    final_revision = payload.research_snapshots[-1].state.revision
    seen_call_ids: set[str] = set()
    seen_reservations: set[str] = set()
    attempts_by_revision: dict[int, list[int]] = {}
    request_by_revision: dict[int, str] = {}
    previous_after = None
    last_revision = -1
    for index, record in enumerate(records):
        reservation = record.reservation
        if (
            reservation.run_id != payload.run_id
            or reservation.run_incarnation != payload.run_incarnation
        ):
            raise ValueError(
                "model budget record identity does not match replay payload"
            )
        if (
            reservation.call_id in seen_call_ids
            or reservation.reservation_digest in seen_reservations
        ):
            raise ValueError("model budget ledger identity is duplicated")
        seen_call_ids.add(reservation.call_id)
        seen_reservations.add(reservation.reservation_digest)
        matched = _MODEL_CALL_ID.fullmatch(reservation.call_id)
        if matched is None:
            raise ValueError("model budget call ID is not a research-loop attempt")
        revision = int(matched["revision"])
        attempt = int(matched["attempt"])
        if revision > final_revision or revision < last_revision:
            raise ValueError("model budget ledger revisions are reordered")
        last_revision = revision
        attempts_by_revision.setdefault(revision, []).append(attempt)
        previous_request = request_by_revision.setdefault(
            revision, reservation.request_digest
        )
        if previous_request != reservation.request_digest:
            raise ValueError("model request digest changed within one research turn")
        if previous_after is not None and reservation.budget_before != previous_after:
            raise ValueError("model budget ledger has a broken cost chain")
        if record.reconciliation is None:
            raise ValueError("closed replay requires completed model budget records")
        else:
            previous_after = record.reconciliation.budget_after
    if any(
        attempts != list(range(len(attempts)))
        for attempts in attempts_by_revision.values()
    ):
        raise ValueError("model budget attempts are not contiguous")
    if any(revision not in attempts_by_revision for revision in range(final_revision)):
        raise ValueError("model budget research revisions have a gap")
    final_budget = payload.research_snapshots[-1].state.budget_state
    assert previous_after is not None
    if (
        final_budget.initial_model_calls != previous_after.initial_model_calls
        or final_budget.used_model_calls != previous_after.used_model_calls
        or final_budget.remaining_model_calls != previous_after.remaining_model_calls
        or final_budget.initial_model_tokens != previous_after.initial_total_tokens
        or final_budget.used_model_tokens != previous_after.used_total_tokens
        or final_budget.remaining_model_tokens != previous_after.remaining_total_tokens
    ):
        raise ValueError("research state omits durable model budget records")


def validate_artifact_attachments(payload: AdaptiveReplayPayload) -> None:
    needed_values = [
        reference
        for transition in payload.research_transitions
        if transition.replay_input is not None
        and transition.replay_input.probe_result is not None
        for reference in (transition.replay_input.probe_result.artifact_reference,)
        if reference is not None
    ]
    needed_values.extend(
        reference
        for record in payload.budget_records
        if record.result is not None
        for reference in (record.result.artifact_reference,)
        if reference is not None
    )
    needed = dedupe_artifact_references(needed_values)
    references = payload.artifact_references
    attachments = payload.artifact_attachments
    reference_ids = tuple(item.artifact_id for item in references)
    attachment_ids = tuple(item.reference.artifact_id for item in attachments)
    if (
        references != needed
        or reference_ids != tuple(sorted(set(reference_ids)))
        or attachment_ids != tuple(sorted(set(attachment_ids)))
        or reference_ids != attachment_ids
        or any(
            reference != attachment.reference
            for reference, attachment in zip(references, attachments, strict=True)
        )
    ):
        raise ValueError("artifact references and attachments do not match")


def dedupe_artifact_references(
    references: list[ArtifactReference] | tuple[ArtifactReference, ...],
) -> tuple[ArtifactReference, ...]:
    exact: dict[str, ArtifactReference] = {}
    for reference in references:
        prior = exact.get(reference.artifact_id)
        if prior is not None and prior != reference:
            raise ValueError("conflicting artifact reference identity")
        exact[reference.artifact_id] = reference
    return tuple(exact[key] for key in sorted(exact))


def legacy_reasons(
    payload: AdaptiveReplayPayload,
) -> tuple[LegacyReplayReason, ...]:
    reasons: set[LegacyReplayReason] = set()
    typed = payload.solver_terminal is not None or bool(
        payload.solver_steps
        and type(payload.solver_steps[-1]) is SolverExecutionReplayStep
        and _result_contradiction_receipt(payload.solver_steps[-1]) is not None
    )
    if payload.research_abort_journal is not None:
        reasons.add(LegacyReplayReason.RESEARCH_ABORT_INPUT)
    if any(item.replay_input is None for item in payload.research_transitions):
        reasons.add(LegacyReplayReason.RESEARCH_TRANSITION_INPUT)
    if any(item.replay_input is not None for item in payload.research_transitions):
        typed = True
    if payload.research_terminal is not None:
        if payload.research_terminal.replay_input is None:
            reasons.add(LegacyReplayReason.RESEARCH_TERMINAL_INPUT)
        else:
            typed = True
    for step in payload.solver_steps:
        if type(step) is not SolverTransitionReplayStep:
            continue
        if requires_solver_input(step.action):
            if step.replay_input is None:
                reasons.add(LegacyReplayReason.SOLVER_TRANSITION_INPUT)
            else:
                typed = True
    if not typed:
        reasons.add(LegacyReplayReason.NO_TYPED_PROVENANCE)
    return tuple(sorted(reasons, key=lambda item: item.value))


def requires_solver_input(action: SolverReplayAction) -> bool:
    return type(action) in (
        SolverAction,
        SolverReentryAdmittedReplayAction,
    ) or (
        type(action) is SolverReentryFinalizedReplayAction
        and action.record.status is ResearchReentryStatus.COMPLETED
    )


def _same_probe_budget(left, right) -> bool:
    return all(
        getattr(left, field) == getattr(right, field)
        for field in (
            "initial_wall_clock_ms",
            "used_wall_clock_ms",
            "remaining_wall_clock_ms",
            "initial_db_probe_ms",
            "used_db_probe_ms",
            "remaining_db_probe_ms",
            "initial_rows",
            "used_rows",
            "remaining_rows",
            "initial_bytes",
            "used_bytes",
            "remaining_bytes",
        )
    )


def sha256_digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def durable_action_digest(value: StrictModel) -> str:
    return canonical_digest(
        value.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
            round_trip=True,
            warnings="error",
        )
    )


def evidence_is_data_snapshot(scope: EvidenceValidityScope) -> bool:
    return scope is EvidenceValidityScope.DATA_SNAPSHOT


__all__ = [
    "AdaptiveReplayPayload",
    "CanonicalReplayBlob",
    "EvidenceReuseResult",
    "EvidenceReuseStatus",
    "FinalizerExecutionRequest",
    "HistoricalReplayResult",
    "HistoricalReplayStatus",
    "LegacyReplayReason",
    "ReplayArtifactAttachment",
    "ReplayArtifactEnvelope",
    "ReplayContractError",
    "ResearchAbortedReplayAction",
    "ResearchObservedReplayAction",
    "ResearchPlannedReplayAction",
    "ResearchReplayAbortJournal",
    "ResearchReplaySnapshot",
    "ResearchReplayTerminal",
    "ResearchReplayTransition",
    "ResearchTerminalReplayAction",
    "SolverCheckReplayAction",
    "SolverExecutionReconciliation",
    "SolverExecutionReplayAction",
    "SolverExecutionReplayStep",
    "SolverReentryAdmittedReplayAction",
    "SolverReentryFinalizedReplayAction",
    "SolverReplaySnapshot",
    "SolverReplayStep",
    "SolverReplayTerminal",
    "SolverSemanticRepairFallbackReplayAction",
    "SolverStopReplayAction",
    "SolverTransitionReplayStep",
    "dedupe_artifact_references",
    "durable_action_digest",
    "evidence_is_data_snapshot",
    "legacy_reasons",
    "requires_solver_input",
    "sha256_digest",
]
