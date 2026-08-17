"""Durable probe-budget operations for adaptive research."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import CancelledError as FuturesCancelledError
from datetime import UTC, datetime
import time
from typing import Literal, Protocol
import uuid

from pydantic import field_validator, model_validator


from .models import (
    BudgetState,
    Digest,
    EvidenceCost,
    Id,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    StrictModel,
    TargetRef,
)
from .probes import ProbeResult, ProbeStatus, build_probe_result
from .serialization import canonical_digest

from ._policy_action_identity import canonical_digest_for_action
from ._policy_common import (
    ActionIdentityError,
    BudgetAdmissionError,
    BudgetConflictError,
    BudgetExhaustedError,
    BudgetReconciliationError,
    FOLLOWER_POLL_SECONDS,
    NonNegativeInt,
    POLICY_VERSION,
    ProbeExecutionFailure,
    _charge_cost_capped,
    _cost_with_elapsed,
    _elapsed_ms,
    _elapsed_cost,
    _has_cost,
    _monotonic_timestamp,
    _revalidate,
    _require_available_cost,
    _require_probe_only_cost,
    _reservation_overrun,
    _SHA256_DIGEST_RE,
    _utc_timestamp,
    _with_measured_wall_cost,
)
from ._policy_config import AdaptivePolicyConfig, initial_budget_state


class BudgetReservation(StrictModel):
    contract_version: Literal[1] = POLICY_VERSION
    run_id: Id
    run_incarnation: Id
    revision: NonNegativeInt
    schema_namespace_version: Digest
    action_digest: Digest
    probe_kind: ResearchActionKind
    target: TargetRef
    policy_digest: Digest
    budget_before: BudgetState
    maximum_cost: EvidenceCost
    reservation_digest: Digest

    @field_validator(
        "action_digest",
        "policy_digest",
        "reservation_digest",
    )
    @classmethod
    def validate_sha256_digest(cls, value: str) -> str:
        if not _SHA256_DIGEST_RE.fullmatch(value):
            raise ValueError("digest must be exact sha256 lowercase hex")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> BudgetReservation:
        _require_probe_only_cost(self.maximum_cost)
        if self.reservation_digest != _reservation_digest(self):
            raise ValueError("reservation_digest is not canonical")
        return self


class BudgetReconciliation(StrictModel):
    contract_version: Literal[1] = POLICY_VERSION
    reservation: BudgetReservation
    invocation_id: Id
    result_status: ProbeStatus
    actual_cost: EvidenceCost
    charged_cost: EvidenceCost
    overrun_cost: EvidenceCost
    budget_after: BudgetState
    budget_exhausted: bool
    reconciliation_digest: Digest

    @field_validator("reconciliation_digest")
    @classmethod
    def validate_sha256_digest(cls, value: str) -> str:
        if not _SHA256_DIGEST_RE.fullmatch(value):
            raise ValueError("digest must be exact sha256 lowercase hex")
        return value

    @model_validator(mode="after")
    def validate_transition(self) -> BudgetReconciliation:
        _require_probe_only_cost(self.reservation.maximum_cost)
        _require_probe_only_cost(self.actual_cost)
        _require_probe_only_cost(self.charged_cost)
        _require_probe_only_cost(self.overrun_cost)
        expected_budget, expected_charged = _charge_cost_capped(
            self.reservation.budget_before,
            self.actual_cost,
        )
        expected_overrun = _reservation_overrun(
            self.reservation.maximum_cost,
            self.actual_cost,
        )
        if self.charged_cost != expected_charged:
            raise ValueError("charged_cost must equal the available actual cost")
        if self.overrun_cost != expected_overrun:
            raise ValueError("overrun_cost must record reservation excess")
        if self.budget_after != expected_budget:
            raise ValueError("budget_after must charge actual_cost exactly once")
        if self.budget_exhausted != _has_cost(expected_overrun):
            raise ValueError("budget_exhausted must expose reservation overrun")
        if self.reconciliation_digest != _reconciliation_digest(self):
            raise ValueError("reconciliation_digest is not canonical")
        return self


class BudgetLedgerRecord(StrictModel):
    """One durable reservation and its optional atomic result reconciliation."""

    reservation: BudgetReservation
    result: ProbeResult | None
    reconciliation: BudgetReconciliation | None

    @model_validator(mode="after")
    def validate_record(self) -> BudgetLedgerRecord:
        if self.reconciliation is not None and self.result is None:
            raise ValueError("reconciliation requires a recorded probe result")
        if self.result is not None:
            _validate_result_matches_reservation(self.reservation, self.result)
        if self.reconciliation is not None:
            expected = _build_reconciliation(self.reservation, self.result)
            if self.reconciliation != expected:
                raise ValueError("recorded reconciliation is not canonical")
        return self


class BudgetLedger(Protocol):
    """Append-only durable source of budget reservation truth."""

    def load_records(
        self,
        run_id: str,
        run_incarnation: str,
    ) -> tuple[BudgetLedgerRecord, ...]: ...

    def record_reservation(
        self,
        reservation: BudgetReservation,
    ) -> BudgetReservation: ...

    def claim_execution(
        self,
        reservation: BudgetReservation,
        owner_token: str,
        *,
        now_ns: int,
    ) -> bool: ...

    def record_result(
        self,
        reservation: BudgetReservation,
        result: ProbeResult,
        *,
        owner_token: str,
    ) -> ProbeResult: ...

    def record_reconciliation(
        self,
        reconciliation: BudgetReconciliation,
        result: ProbeResult,
    ) -> BudgetReconciliation: ...


def reserve_probe_budget(
    state: ResearchState,
    action: ResearchAction,
    maximum_cost: EvidenceCost,
    *,
    config: AdaptivePolicyConfig,
    ledger: BudgetLedger,
) -> BudgetReservation:
    """Atomically admit one probe against the complete durable ledger."""

    reservation, records, current_record = _prepare_probe_reservation(
        state,
        action,
        maximum_cost,
        config=config,
        ledger=ledger,
    )
    if current_record is not None:
        if current_record.reservation == reservation:
            return reservation
        raise BudgetConflictError(
            "outstanding or completed reservation conflicts with the proposed action"
        )
    if len(records) >= config.operation_counts.db_probes:
        raise BudgetExhaustedError("DB probe count budget is exhausted")
    return ledger.record_reservation(reservation)


def _prepare_probe_reservation(
    state: ResearchState,
    action: ResearchAction,
    maximum_cost: EvidenceCost,
    *,
    config: AdaptivePolicyConfig,
    ledger: BudgetLedger,
) -> tuple[
    BudgetReservation,
    tuple[BudgetLedgerRecord, ...],
    BudgetLedgerRecord | None,
]:
    """Derive one canonical reservation without writing or claiming it."""

    checked_config = _revalidate(config, AdaptivePolicyConfig, "policy config")
    checked_state = _revalidate(state, ResearchState, "research state")
    checked_action = _revalidate(action, ResearchAction, "research action")
    checked_cost = _revalidate(maximum_cost, EvidenceCost, "maximum cost")
    _validate_state_for_admission(checked_state, checked_config)
    _validate_action_for_admission(
        checked_state, checked_action, checked_config, checked_cost
    )

    policy_digest = canonical_digest(checked_config)
    records = _load_ledger_records(
        ledger,
        checked_state.run_id,
        checked_state.run_incarnation,
    )
    current_record = _validate_ledger_for_admission(
        checked_state,
        checked_config,
        policy_digest,
        records,
    )

    _require_available_cost(checked_state.budget_state, checked_cost)
    values = {
        "run_id": checked_state.run_id,
        "run_incarnation": checked_state.run_incarnation,
        "revision": checked_state.revision,
        "schema_namespace_version": checked_state.schema_namespace_version,
        "action_digest": checked_action.action_digest,
        "probe_kind": checked_action.kind,
        "target": checked_action.target,
        "policy_digest": policy_digest,
        "budget_before": checked_state.budget_state,
        "maximum_cost": checked_cost,
    }
    reservation = BudgetReservation(
        **values,
        reservation_digest=canonical_digest(values),
    )
    return reservation, records, current_record


def reconcile_probe_cost(
    reservation: BudgetReservation,
    result: ProbeResult,
) -> BudgetReconciliation:
    """Derive the canonical charge for one reserved result."""

    checked_reservation = _revalidate(
        reservation,
        BudgetReservation,
        "budget reservation",
    )
    checked_result = _revalidate(result, ProbeResult, "probe result")
    return _build_reconciliation(checked_reservation, checked_result)


def recover_probe_with_budget(
    state: ResearchState,
    action: ResearchAction,
    maximum_cost: EvidenceCost,
    *,
    expected_invocation_id: str,
    config: AdaptivePolicyConfig,
    ledger: BudgetLedger,
) -> ProbeResult | None:
    """Read one exact durable result and finish only its missing reconciliation."""

    if type(expected_invocation_id) is not str or not expected_invocation_id:
        raise TypeError("expected_invocation_id must be non-empty text")

    reservation, _, current_record = _prepare_probe_reservation(
        state,
        action,
        maximum_cost,
        config=config,
        ledger=ledger,
    )
    if current_record is None:
        return None
    if current_record.reservation != reservation:
        raise BudgetConflictError(
            "durable reservation conflicts with the proposed recovery action"
        )
    if current_record.result is None:
        return None
    if current_record.result.invocation_id != expected_invocation_id:
        raise BudgetReconciliationError(
            "durable probe result invocation does not match recovery"
        )
    if current_record.reconciliation is None:
        reconciliation = reconcile_probe_cost(reservation, current_record.result)
        ledger.record_reconciliation(reconciliation, current_record.result)
    return current_record.result


def execute_probe_with_budget(
    state: ResearchState,
    action: ResearchAction,
    maximum_cost: EvidenceCost,
    execute: Callable[[BudgetReservation], ProbeResult],
    *,
    config: AdaptivePolicyConfig,
    ledger: BudgetLedger,
    invocation_id: str | None = None,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    claim_now_ns: Callable[[], int] = time.time_ns,
    owner_token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    wait: Callable[[float], None] = time.sleep,
) -> tuple[ProbeResult, BudgetReconciliation]:
    """Execute once and durably reconcile timeout, failure, or cancellation."""

    for callback, name in (
        (execute, "execute"),
        (monotonic_ns, "monotonic_ns"),
        (utc_now, "utc_now"),
        (claim_now_ns, "claim_now_ns"),
        (owner_token_factory, "owner_token_factory"),
        (wait, "wait"),
    ):
        if not callable(callback):
            raise TypeError(f"{name} must be callable")
    if invocation_id is not None and (
        type(invocation_id) is not str or not invocation_id
    ):
        raise TypeError("invocation_id must be non-empty text when provided")
    reservation = reserve_probe_budget(
        state,
        action,
        maximum_cost,
        config=config,
        ledger=ledger,
    )
    owner_token = owner_token_factory()
    if not isinstance(owner_token, str) or not owner_token:
        raise TypeError("owner_token_factory must return a non-empty string")
    while True:
        existing = _record_for_reservation(
            _load_ledger_records(
                ledger,
                reservation.run_id,
                reservation.run_incarnation,
            ),
            reservation,
        )
        if existing.reconciliation is not None:
            if existing.result is None:  # pragma: no cover - model invariant
                raise BudgetReconciliationError("reconciliation has no result")
            return existing.result, existing.reconciliation
        if existing.result is not None:
            reconciliation = reconcile_probe_cost(reservation, existing.result)
            stored = ledger.record_reconciliation(reconciliation, existing.result)
            return existing.result, stored
        if ledger.claim_execution(
            reservation,
            owner_token,
            now_ns=_monotonic_timestamp(claim_now_ns(), "claim_now_ns result"),
        ):
            break
        wait(FOLLOWER_POLL_SECONDS)

    started_at = _utc_timestamp(utc_now(), "utc_now result")
    started_ns = _monotonic_timestamp(monotonic_ns(), "monotonic_ns result")
    try:
        result = _revalidate(execute(reservation), ProbeResult, "probe result")
        result = _with_measured_wall_cost(
            result,
            _elapsed_ms(started_ns, monotonic_ns()),
        )
    except asyncio.CancelledError:
        result = _caught_failure_result(
            reservation,
            invocation_id,
            ProbeStatus.CANCELLED,
            _elapsed_cost(started_ns, monotonic_ns()),
            "cancelled",
            "probe execution was cancelled",
            started_at,
            utc_now(),
        )
    except FuturesCancelledError:
        result = _caught_failure_result(
            reservation,
            invocation_id,
            ProbeStatus.CANCELLED,
            _elapsed_cost(started_ns, monotonic_ns()),
            "cancelled",
            "probe execution was cancelled",
            started_at,
            utc_now(),
        )
    except TimeoutError:
        result = _caught_failure_result(
            reservation,
            invocation_id,
            ProbeStatus.TIMED_OUT,
            _elapsed_cost(started_ns, monotonic_ns()),
            "timed_out",
            "probe execution timed out",
            started_at,
            utc_now(),
        )
    except ProbeExecutionFailure as exc:
        result = _caught_failure_result(
            reservation,
            invocation_id,
            exc.status,
            _cost_with_elapsed(
                exc.actual_cost,
                _elapsed_ms(started_ns, monotonic_ns()),
            ),
            exc.failure_code,
            exc.summary,
            started_at,
            utc_now(),
        )
    except Exception:
        result = _caught_failure_result(
            reservation,
            invocation_id,
            ProbeStatus.FAILED,
            _elapsed_cost(started_ns, monotonic_ns()),
            "execution_failed",
            "probe execution failed",
            started_at,
            utc_now(),
        )
    result = ledger.record_result(
        reservation,
        result,
        owner_token=owner_token,
    )
    reconciliation = reconcile_probe_cost(reservation, result)
    stored = ledger.record_reconciliation(reconciliation, result)
    return result, stored


def _validate_state_for_admission(
    state: ResearchState,
    config: AdaptivePolicyConfig,
) -> None:
    if state.stop_reason is not None:
        raise BudgetAdmissionError("stopped research state cannot reserve work")
    if len(state.action_history) != state.revision:
        raise BudgetAdmissionError("action history length must equal state revision")
    if [item.expected_revision for item in state.action_history] != list(
        range(state.revision)
    ):
        raise BudgetAdmissionError("action history revisions must be contiguous")
    for historical_action in state.action_history:
        if historical_action.action_digest != canonical_digest_for_action(
            historical_action
        ):
            raise ActionIdentityError("historical action digest is not canonical")
    if len(state.action_history) >= config.operation_counts.actions:
        raise BudgetExhaustedError("research action count budget is exhausted")

    limits = {
        "wall_clock_ms": config.wall_clock_ms,
        "model_calls": config.operation_counts.model_decisions,
        "model_tokens": config.resource_limits.model_tokens,
        "db_probe_ms": config.resource_limits.db_probe_ms,
        "rows": config.result_volume.returned_rows,
        "bytes": config.result_volume.inline_bytes,
    }
    for field_name, limit in limits.items():
        if getattr(state.budget_state, f"initial_{field_name}") != limit:
            raise BudgetAdmissionError(
                f"initial {field_name} must equal the versioned policy"
            )


def _validate_action_for_admission(
    state: ResearchState,
    action: ResearchAction,
    config: AdaptivePolicyConfig,
    maximum_cost: EvidenceCost,
) -> None:
    if action.expected_revision != state.revision:
        raise BudgetAdmissionError("action expected_revision must equal state revision")
    if action.action_digest != canonical_digest_for_action(action):
        raise ActionIdentityError("action_digest is not canonical")
    if action.action_digest in {item.action_digest for item in state.action_history}:
        raise BudgetConflictError("action already exists in immutable history")
    _require_probe_only_cost(maximum_cost)
    if action.kind is ResearchActionKind.SAMPLE_ROWS:
        parameters = dict(action.parameters)
        limit = parameters.get("limit")
        if type(limit) is not int or limit <= 0:
            raise BudgetAdmissionError("sample_rows requires a positive integer limit")
        if limit > config.per_action.sample_rows or maximum_cost.rows > limit:
            raise BudgetExhaustedError("sample_rows per-action budget is exceeded")


def _load_ledger_records(
    ledger: BudgetLedger,
    run_id: str,
    run_incarnation: str,
) -> tuple[BudgetLedgerRecord, ...]:
    load_records = getattr(ledger, "load_records", None)
    if not callable(load_records):
        raise TypeError("ledger must provide load_records")
    records = load_records(run_id, run_incarnation)
    if type(records) is not tuple:
        raise TypeError("ledger load_records must return a tuple")
    return tuple(
        _revalidate(item, BudgetLedgerRecord, "budget ledger record")
        for item in records
    )


def _validate_ledger_for_admission(
    state: ResearchState,
    config: AdaptivePolicyConfig,
    policy_digest: str,
    records: tuple[BudgetLedgerRecord, ...],
) -> BudgetLedgerRecord | None:
    expected_budget = initial_budget_state(config)
    outstanding: BudgetLedgerRecord | None = None
    invocation_ids: set[str] = set()
    completed_count = 0
    previous_revision = -1
    for index, record in enumerate(records):
        reservation = record.reservation
        if (
            reservation.run_id != state.run_id
            or reservation.run_incarnation != state.run_incarnation
        ):
            raise BudgetAdmissionError("ledger belongs to another run incarnation")
        if reservation.revision <= previous_revision:
            raise BudgetAdmissionError("budget ledger revisions must be strictly ordered")
        previous_revision = reservation.revision
        if reservation.schema_namespace_version != state.schema_namespace_version:
            raise BudgetAdmissionError("budget ledger uses another schema namespace")
        if reservation.policy_digest != policy_digest:
            raise BudgetAdmissionError("budget ledger uses another policy version")
        if not _same_probe_budget(reservation.budget_before, expected_budget):
            raise BudgetAdmissionError("budget ledger has a broken cost chain")
        if record.result is not None:
            if record.result.invocation_id in invocation_ids:
                raise BudgetAdmissionError("probe invocation identity is duplicated")
            invocation_ids.add(record.result.invocation_id)
        if record.reconciliation is None:
            if outstanding is not None or index != len(records) - 1:
                raise BudgetAdmissionError(
                    "only the last budget reservation may be outstanding"
                )
            outstanding = record
            continue
        expected_budget = record.reconciliation.budget_after
        completed_count += 1

    historical_probe_actions = tuple(
        action
        for action in state.action_history
        if action.kind is not ResearchActionKind.SEMANTIC_COMMIT
    )
    completed_retry = (
        completed_count == len(historical_probe_actions) + 1
        and bool(records)
        and records[-1].reservation.revision == state.revision
        and records[-1].reconciliation is not None
    )
    if completed_count != len(historical_probe_actions) and not completed_retry:
        raise BudgetAdmissionError("research state omits durable budget ledger entries")
    expected_state_budget = (
        records[-1].reservation.budget_before if completed_retry else expected_budget
    )
    if not _same_probe_budget(state.budget_state, expected_state_budget):
        raise BudgetAdmissionError(
            "research state budget does not match durable ledger"
        )
    if not completed_retry and any(
        record.reconciliation is not None and record.reconciliation.budget_exhausted
        for record in records
    ):
        raise BudgetExhaustedError("reservation overrun exhausted the research budget")
    if outstanding is not None and outstanding.reservation.revision != state.revision:
        raise BudgetAdmissionError("outstanding reservation revision is stale")
    completed_records = (
        records[:-1] if completed_retry or outstanding is not None else records
    )
    if len(completed_records) != len(historical_probe_actions):
        raise BudgetAdmissionError("research state omits durable budget ledger entries")
    for action, record in zip(historical_probe_actions, completed_records, strict=True):
        reservation = record.reservation
        if (
            reservation.revision != action.expected_revision
            or reservation.action_digest != action.action_digest
            or reservation.probe_kind is not action.kind
            or reservation.target != action.target
        ):
            raise BudgetAdmissionError("action history does not match durable ledger")
    return records[-1] if completed_retry else outstanding


def _same_probe_budget(left: BudgetState, right: BudgetState) -> bool:
    """Compare the resources owned by the probe ledger, not model usage."""

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


def _record_for_reservation(
    records: tuple[BudgetLedgerRecord, ...],
    reservation: BudgetReservation,
) -> BudgetLedgerRecord:
    matches = [
        record
        for record in records
        if record.reservation.reservation_digest == reservation.reservation_digest
    ]
    if len(matches) != 1 or matches[0].reservation != reservation:
        raise BudgetReconciliationError("durable reservation is missing or conflicting")
    return matches[0]


def _validate_result_matches_reservation(
    reservation: BudgetReservation,
    result: ProbeResult,
) -> None:
    _require_probe_only_cost(reservation.maximum_cost)
    _require_probe_only_cost(result.cost)
    if (
        result.run_id != reservation.run_id
        or result.run_incarnation != reservation.run_incarnation
    ):
        raise BudgetReconciliationError(
            "probe result belongs to another run incarnation"
        )
    if result.revision != reservation.revision:
        raise BudgetReconciliationError(
            "probe result revision does not match reservation"
        )
    if result.schema_namespace_version != reservation.schema_namespace_version:
        raise BudgetReconciliationError(
            "probe result schema does not match reservation"
        )
    if result.action_digest != reservation.action_digest:
        raise BudgetReconciliationError(
            "probe result action does not match reservation"
        )
    if (
        result.probe_kind is not reservation.probe_kind
        or result.target != reservation.target
    ):
        raise BudgetReconciliationError(
            "probe result semantics do not match reservation"
        )


def _build_reconciliation(
    reservation: BudgetReservation,
    result: ProbeResult | None,
) -> BudgetReconciliation:
    if result is None:
        raise BudgetReconciliationError("probe result is required")
    _validate_result_matches_reservation(reservation, result)
    budget_after, charged_cost = _charge_cost_capped(
        reservation.budget_before,
        result.cost,
    )
    overrun_cost = _reservation_overrun(reservation.maximum_cost, result.cost)
    values = {
        "reservation": reservation,
        "invocation_id": result.invocation_id,
        "result_status": result.status,
        "actual_cost": result.cost,
        "charged_cost": charged_cost,
        "overrun_cost": overrun_cost,
        "budget_after": budget_after,
        "budget_exhausted": _has_cost(overrun_cost),
    }
    return BudgetReconciliation(
        **values,
        reconciliation_digest=canonical_digest(values),
    )


def _caught_failure_result(
    reservation: BudgetReservation,
    invocation_id: str | None,
    status: ProbeStatus,
    cost: EvidenceCost,
    failure_code: str,
    summary: str,
    started_at: datetime,
    completed_at: datetime,
) -> ProbeResult:
    return build_probe_result(
        run_id=reservation.run_id,
        run_incarnation=reservation.run_incarnation,
        revision=reservation.revision,
        schema_namespace_version=reservation.schema_namespace_version,
        invocation_id=invocation_id
        or f"probe-{reservation.reservation_digest.removeprefix('sha256:')}",
        action_digest=reservation.action_digest,
        probe_kind=reservation.probe_kind,
        status=status,
        target=reservation.target,
        started_at=started_at,
        completed_at=_utc_timestamp(completed_at, "utc_now result"),
        summary=summary,
        cost=cost,
        row_count=cost.rows,
        failure_code=failure_code,
    )


def _reservation_digest(reservation: BudgetReservation) -> str:
    return canonical_digest(
        {
            "run_id": reservation.run_id,
            "run_incarnation": reservation.run_incarnation,
            "revision": reservation.revision,
            "schema_namespace_version": reservation.schema_namespace_version,
            "action_digest": reservation.action_digest,
            "probe_kind": reservation.probe_kind,
            "target": reservation.target,
            "policy_digest": reservation.policy_digest,
            "budget_before": reservation.budget_before,
            "maximum_cost": reservation.maximum_cost,
        }
    )


def _reconciliation_digest(reconciliation: BudgetReconciliation) -> str:
    return canonical_digest(
        {
            "reservation": reconciliation.reservation,
            "invocation_id": reconciliation.invocation_id,
            "result_status": reconciliation.result_status,
            "actual_cost": reconciliation.actual_cost,
            "charged_cost": reconciliation.charged_cost,
            "overrun_cost": reconciliation.overrun_cost,
            "budget_after": reconciliation.budget_after,
            "budget_exhausted": reconciliation.budget_exhausted,
        }
    )
