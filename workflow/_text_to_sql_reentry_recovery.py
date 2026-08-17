"""Typed durable plan for crash-safe targeted research re-entry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from custom_tools.text_to_sql.adaptive.models import (
    Binding,
    Digest,
    Hypothesis,
    Id,
    JoinCandidate,
    ResearchReentryRecord,
    ResearchReentryStatus,
    ResearchAction,
    ResearchState,
    SolverState,
    StrictModel,
)
from custom_tools.text_to_sql.adaptive._exact_contract import exact_value
from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageRequirements,
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.semantic_reducer import (
    SemanticTurnAdmission,
    commit_semantic_turn,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
from custom_tools.text_to_sql.adaptive.solver_loop import finalize_targeted_reentry


class PreparedTargetedReentryCommit(StrictModel):
    """Complete pure write set persisted before the targeted probe may run."""

    contract_version: Literal[1] = 1
    run_id: Id
    run_incarnation: Id
    research_reentry_id: Id
    missing_evidence_request_id: Id
    source_id: Id
    ordinal: Annotated[int, Field(ge=1, le=3)]
    base_solver_revision: Annotated[int, Field(ge=0)]
    solver_admission_digest: Digest
    store_base_research_revision: Annotated[int, Field(ge=0)]
    store_base_research_digest: Digest
    projected_research: ResearchState
    projected_research_digest: Digest
    action: ResearchAction
    hypotheses: tuple[Hypothesis, ...]
    bindings: tuple[Binding, ...]
    join_candidates: tuple[JoinCandidate, ...]
    invocation_id: Id
    reservation_digest: Digest
    policy_digest: Digest
    schema_namespace_version: Digest
    plan_digest: Digest

    @model_validator(mode="after")
    def validate_identity(self) -> PreparedTargetedReentryCommit:
        projected = self.projected_research
        if (
            projected.run_id != self.run_id
            or projected.run_incarnation != self.run_incarnation
            or self.store_base_research_revision != projected.revision
            or self.action.expected_revision != projected.revision
        ):
            raise ValueError("prepared re-entry research identity is inconsistent")
        if (
            projected.schema_namespace_version != self.schema_namespace_version
        ):
            raise ValueError("prepared re-entry schema identity is inconsistent")
        if canonical_digest(projected) != self.projected_research_digest:
            raise ValueError("prepared projected research digest is inconsistent")
        values = self.model_dump(mode="python", round_trip=True)
        values.pop("plan_digest", None)
        if canonical_digest(values) != self.plan_digest:
            raise ValueError("prepared re-entry plan digest is inconsistent")
        return self


@dataclass(frozen=True, slots=True)
class RecoveredTargetedReentry:
    solver_state: SolverState
    research_state: ResearchState
    requirements: CoverageRequirements
    freshness: FreshnessContext
    record: ResearchReentryRecord


def build_prepared_targeted_reentry_commit(**values) -> PreparedTargetedReentryCommit:
    payload = {"contract_version": 1, **values}
    payload.pop("plan_digest", None)
    payload["plan_digest"] = canonical_digest(payload)
    return PreparedTargetedReentryCommit.model_validate(payload)


def recover_prepared_targeted_reentry(
    runtime: object,
    solver_state: SolverState,
    freshness: FreshnessContext,
) -> RecoveredTargetedReentry | None:
    """Recover one exact prepared probe result without replaying external work."""

    from custom_tools.text_to_sql.adaptive.research_loop import (
        _state_with_reconciled_model_budget,
    )

    admitted = tuple(
        record
        for record in solver_state.research_reentries
        if record.status is ResearchReentryStatus.ADMITTED
    )
    if len(admitted) != 1 or admitted[0] is not solver_state.research_reentries[-1]:
        return None
    record = admitted[0]
    state_store = runtime.research_state_store
    plan = state_store.load_prepared_targeted_reentry_commit(
        solver_state.run_id,
        solver_state.run_incarnation,
        record.research_reentry_id,
    )
    if plan is None:
        return None
    _validate_plan_solver_identity(plan, solver_state, record)
    store_base = state_store.load_research_state(
        plan.run_id,
        plan.run_incarnation,
        revision=plan.store_base_research_revision,
    )
    if (
        store_base is None
        or canonical_digest(store_base) != plan.store_base_research_digest
    ):
        raise ValueError("prepared re-entry store base is missing or conflicting")
    projected = _state_with_reconciled_model_budget(
        store_base,
        runtime.budget_ledger,
        runtime.verified_research_policy,
    )
    if not exact_value(projected, plan.projected_research):
        raise ValueError("prepared re-entry model projection is conflicting")

    probe_record = _exact_probe_record(runtime, plan)
    if probe_record is None:
        return None
    probe_result = probe_record.result
    reconciliation = probe_record.reconciliation
    if probe_result is None or reconciliation is None:
        return None
    if (
        probe_result.invocation_id != plan.invocation_id
        or probe_result.action_digest != plan.action.action_digest
    ):
        raise ValueError("prepared re-entry probe identity is conflicting")
    admission = SemanticTurnAdmission(
        state=projected,
        action=plan.action,
        hypotheses=plan.hypotheses,
        bindings=plan.bindings,
        join_candidates=plan.join_candidates,
        budget_state=reconciliation.budget_after,
    )
    committed = commit_semantic_turn(admission, probe_result=probe_result)
    successor = committed.state
    refreshed = FreshnessContext(
        evaluated_at=datetime.now(UTC),
        run_id=freshness.run_id,
        run_incarnation=freshness.run_incarnation,
        schema_namespace_version=freshness.schema_namespace_version,
        document_sources=freshness.document_sources,
        data_snapshots=freshness.data_snapshots,
    )
    requirements = validate_coverage_inputs(
        successor,
        refreshed,
        successor.run_id,
        successor.run_incarnation,
    )
    status = _reentry_status_for_probe(probe_result.status)
    finalized = finalize_targeted_reentry(
        solver_state,
        record.research_reentry_id,
        status,
        base_revision=solver_state.revision,
        research_state=successor if status is ResearchReentryStatus.COMPLETED else None,
        freshness_context=refreshed if status is ResearchReentryStatus.COMPLETED else None,
        requirements=requirements if status is ResearchReentryStatus.COMPLETED else None,
    )
    state_store.commit_prepared_targeted_reentry(plan, successor)
    return RecoveredTargetedReentry(
        solver_state=finalized.state,
        research_state=successor,
        requirements=requirements,
        freshness=refreshed,
        record=finalized.record,
    )


def _validate_plan_solver_identity(plan, solver_state, record) -> None:
    request = next(
        (
            item
            for item in solver_state.missing_evidence_requests
            if item.missing_evidence_request_id
            == record.missing_evidence_request_id
        ),
        None,
    )
    if (
        plan.run_id != solver_state.run_id
        or plan.run_incarnation != solver_state.run_incarnation
        or plan.research_reentry_id != record.research_reentry_id
        or plan.missing_evidence_request_id
        != record.missing_evidence_request_id
        or plan.source_id != record.source_id
        or plan.ordinal != record.ordinal
        or plan.base_solver_revision != solver_state.revision
        or plan.solver_admission_digest != canonical_digest(solver_state)
        or request is None
        or request.source_id != plan.source_id
    ):
        raise ValueError("prepared re-entry solver admission is conflicting")


def _exact_probe_record(runtime, plan):
    records = runtime.budget_ledger.load_records(plan.run_id, plan.run_incarnation)
    matches = [
        item
        for item in records
        if item.reservation.reservation_digest == plan.reservation_digest
    ]
    if len(matches) != 1:
        return None
    record = matches[0]
    reservation = record.reservation
    if (
        reservation.revision != plan.projected_research.revision
        or reservation.action_digest != plan.action.action_digest
        or reservation.probe_kind is not plan.action.kind
        or reservation.target != plan.action.target
        or reservation.policy_digest != plan.policy_digest
        or reservation.schema_namespace_version != plan.schema_namespace_version
        or reservation.budget_before != plan.projected_research.budget_state
    ):
        raise ValueError("prepared re-entry probe reservation is conflicting")
    return record


def _reentry_status_for_probe(status: ProbeStatus) -> ResearchReentryStatus:
    if status is ProbeStatus.SUCCESS:
        return ResearchReentryStatus.COMPLETED
    if status is ProbeStatus.CANCELLED:
        return ResearchReentryStatus.CANCELLED
    if status is ProbeStatus.TIMED_OUT:
        return ResearchReentryStatus.DEADLINE_EXCEEDED
    return ResearchReentryStatus.TOOL_FAILURE


__all__ = (
    "PreparedTargetedReentryCommit",
    "RecoveredTargetedReentry",
    "build_prepared_targeted_reentry_commit",
    "recover_prepared_targeted_reentry",
)
