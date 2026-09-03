"""Durable v2 model-budget operations for adaptive research."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import inspect
import sqlite3
import time
from typing import Awaitable, Protocol
import uuid

from pydantic import ValidationError


from .models import (
    BudgetState,
)
from .model_budget import (
    ModelBudgetLedgerRecord,
    ModelBudgetState,
    ModelCallReconciliation,
    ModelCallReservation,
    ModelCallResult,
    ModelCallStarted,
    ModelTokenUsage,
    ModelUsageBudgetError,
    model_charge,
)
from .serialization import canonical_digest

from ._policy_common import (
    BudgetAdmissionError,
    BudgetConflictError,
    BudgetExhaustedError,
    BudgetReconciliationError,
    FOLLOWER_POLL_SECONDS,
    _SHA256_DIGEST_RE,
    _monotonic_timestamp,
    _revalidate,
)
from ._policy_config import (
    AdaptivePolicyConfig,
    _require_model_budget_v2,
    initial_model_budget_state,
)


class _ModelExecutionClaim(Protocol):
    acquired: bool
    generation: int


class ModelBudgetLedger(Protocol):
    """The model-event namespace of the same durable budget ledger."""

    def load_model_records(
        self,
        run_id: str,
        run_incarnation: str,
    ) -> tuple[ModelBudgetLedgerRecord, ...]: ...

    def record_model_reservation(
        self,
        reservation: ModelCallReservation,
    ) -> ModelCallReservation: ...

    def claim_model_execution(
        self,
        reservation: ModelCallReservation,
        owner_token: str,
        *,
        now_ns: int,
    ) -> _ModelExecutionClaim: ...

    def record_model_started(
        self,
        started: ModelCallStarted,
        *,
        owner_token: str,
    ) -> tuple[ModelCallStarted, bool]: ...

    def record_model_result(
        self,
        result: ModelCallResult,
        *,
        owner_token: str,
    ) -> ModelCallResult: ...

    def record_model_reconciliation(
        self,
        reconciliation: ModelCallReconciliation,
        result: ModelCallResult,
    ) -> ModelCallReconciliation: ...


def completed_model_budget_chain(
    records: tuple[ModelBudgetLedgerRecord, ...],
    *,
    config: AdaptivePolicyConfig,
) -> ModelBudgetState:
    """Return the policy-anchored budget after only completed model calls."""

    checked_config = _revalidate(config, AdaptivePolicyConfig, "policy config")
    checked_records = tuple(
        _revalidate(record, ModelBudgetLedgerRecord, "model budget ledger record")
        for record in records
    )
    expected_policy_digest = canonical_digest(checked_config)
    if any(
        record.reservation.policy_digest != expected_policy_digest
        for record in checked_records
    ):
        raise BudgetAdmissionError("model budget ledger policy does not match")
    budget, outstanding = _model_budget_chain(
        checked_records,
        initial_model_budget_state(checked_config),
    )
    if outstanding is not None:
        raise BudgetAdmissionError("model budget ledger has an outstanding decision")
    return budget


def validate_state_model_budget_policy(
    budget: BudgetState,
    *,
    config: AdaptivePolicyConfig,
) -> None:
    """Reject persisted model totals that do not come from the active policy."""

    checked_budget = _revalidate(budget, BudgetState, "research budget state")
    expected = initial_model_budget_state(config)
    if (
        checked_budget.initial_model_calls != expected.initial_model_calls
        or checked_budget.initial_model_tokens != expected.initial_total_tokens
    ):
        raise BudgetAdmissionError(
            "research budget state model totals do not match the policy"
        )


def reserve_model_call_budget(
    run_id: str,
    run_incarnation: str,
    call_id: str,
    request_digest: str,
    model_identity: str,
    maximum_input_tokens: int,
    maximum_output_tokens: int,
    *,
    config: AdaptivePolicyConfig,
    ledger: ModelBudgetLedger,
) -> ModelCallReservation:
    """Durably admit one named model invocation under the v2 policy."""

    checked_config = _revalidate(config, AdaptivePolicyConfig, "policy config")
    limits = _require_model_budget_v2(checked_config)
    if not isinstance(request_digest, str) or not _SHA256_DIGEST_RE.fullmatch(
        request_digest
    ):
        raise BudgetAdmissionError("request_digest must be exact sha256 lowercase hex")
    if (
        not isinstance(model_identity, str)
        or not model_identity.strip()
        or len(model_identity) > 512
    ):
        raise BudgetAdmissionError("model_identity must be non-empty bounded text")
    if type(maximum_input_tokens) is not int or maximum_input_tokens < 0:
        raise ValueError("maximum_input_tokens must be a non-negative integer")
    if type(maximum_output_tokens) is not int or maximum_output_tokens < 0:
        raise ValueError("maximum_output_tokens must be a non-negative integer")
    records = _load_model_ledger_records(ledger, run_id, run_incarnation)
    policy_digest = canonical_digest(checked_config)
    matching = [record for record in records if record.reservation.call_id == call_id]
    if matching:
        stored = matching[0].reservation
        if len(matching) == 1 and (
            stored.run_id == run_id
            and stored.run_incarnation == run_incarnation
            and stored.request_digest == request_digest
            and stored.model_identity == model_identity
            and stored.policy_digest == policy_digest
            and stored.maximum_input_tokens == maximum_input_tokens
            and stored.maximum_output_tokens == maximum_output_tokens
        ):
            return stored
        raise BudgetConflictError("model call identity already has a reservation")
    if any(record.reservation.policy_digest != policy_digest for record in records):
        raise BudgetAdmissionError("model budget ledger policy does not match")
    if maximum_input_tokens > limits.input_tokens_per_call:
        raise BudgetExhaustedError("model input token per-call budget is exhausted")
    if maximum_output_tokens > limits.output_tokens_per_call:
        raise BudgetExhaustedError("model output token per-call budget is exhausted")
    budget, outstanding = _model_budget_chain(
        records, initial_model_budget_state(checked_config)
    )
    values = {
        "run_id": run_id,
        "run_incarnation": run_incarnation,
        "call_id": call_id,
        "request_digest": request_digest,
        "model_identity": model_identity,
        "policy_digest": policy_digest,
        "budget_before": budget,
        "maximum_input_tokens": maximum_input_tokens,
        "maximum_output_tokens": maximum_output_tokens,
    }
    try:
        reservation = ModelCallReservation(
            **values,
            reservation_digest=canonical_digest(values),
        )
    except ValidationError as exc:
        raise BudgetAdmissionError("model reservation has invalid identity") from exc
    if outstanding is not None:
        raise BudgetConflictError("outstanding model reservation blocks the next call")
    _require_model_budget_available(budget, maximum_input_tokens, maximum_output_tokens)
    return ledger.record_model_reservation(reservation)


def reconcile_model_call_usage(
    result: ModelCallResult,
) -> ModelCallReconciliation:
    """Calculate the one canonical charge, reserving maxima for unknown usage."""

    checked = _revalidate(result, ModelCallResult, "model result")
    try:
        budget_after, charged_input, charged_output = model_charge(
            checked.reservation,
            checked.usage,
        )
    except ModelUsageBudgetError as exc:
        raise BudgetReconciliationError(str(exc)) from exc
    reconciliation_values = {
        "reservation": checked.reservation,
        "invocation_id": checked.invocation_id,
        "actual_usage": checked.usage,
        "charged_input_tokens": charged_input,
        "charged_output_tokens": charged_output,
        "charged_total_tokens": charged_input + charged_output,
        "budget_after": budget_after,
        "usage_was_conservative": (
            checked.usage.input_tokens is None or checked.usage.output_tokens is None
        ),
    }
    return ModelCallReconciliation(
        **reconciliation_values,
        reconciliation_digest=canonical_digest(reconciliation_values),
    )


@dataclass(frozen=True, slots=True)
class _ModelExecutionReady:
    started: ModelCallStarted
    claim_generation: int


def _prepare_model_execution(
    run_id: str,
    run_incarnation: str,
    call_id: str,
    request_digest: str,
    model_identity: str,
    maximum_input_tokens: int,
    maximum_output_tokens: int,
    *,
    config: AdaptivePolicyConfig,
    ledger: ModelBudgetLedger,
    owner_token_factory: Callable[[], str],
) -> tuple[ModelCallReservation, str]:
    reservation = reserve_model_call_budget(
        run_id,
        run_incarnation,
        call_id,
        request_digest,
        model_identity,
        maximum_input_tokens,
        maximum_output_tokens,
        config=config,
        ledger=ledger,
    )
    owner_token = owner_token_factory()
    if not isinstance(owner_token, str) or not owner_token:
        raise TypeError("owner_token_factory must return a non-empty string")
    return reservation, owner_token


def _settle_model_result(
    ledger: ModelBudgetLedger,
    started: ModelCallStarted,
    claim_generation: int,
    owner_token: str,
    usage: ModelTokenUsage,
) -> ModelCallReconciliation:
    result = _model_result(started, claim_generation, usage)
    for attempt in range(2):
        try:
            stored = ledger.record_model_result(result, owner_token=owner_token)
            return ledger.record_model_reconciliation(
                reconcile_model_call_usage(stored), stored
            )
        except sqlite3.OperationalError as error:
            error_code = getattr(error, "sqlite_errorcode", None)
            is_io_error = (
                isinstance(error_code, int)
                and error_code & 0xFF == sqlite3.SQLITE_IOERR
            ) or str(error).casefold() == "disk i/o error"
            if attempt or not is_io_error:
                raise
    raise AssertionError("unreachable")


def _claim_model_execution_step(
    ledger: ModelBudgetLedger,
    reservation: ModelCallReservation,
    owner_token: str,
    *,
    claim_now_ns: Callable[[], int],
) -> ModelCallReconciliation | _ModelExecutionReady | None:
    record = _model_record_for_reservation(
        _load_model_ledger_records(
            ledger, reservation.run_id, reservation.run_incarnation
        ),
        reservation,
    )
    if record.reconciliation is not None:
        return record.reconciliation
    if record.result is not None:
        return ledger.record_model_reconciliation(
            reconcile_model_call_usage(record.result), record.result
        )
    claim = ledger.claim_model_execution(
        reservation,
        owner_token,
        now_ns=_monotonic_timestamp(claim_now_ns(), "claim_now_ns result"),
    )
    if not claim.acquired:
        return None
    record = _model_record_for_reservation(
        _load_model_ledger_records(
            ledger, reservation.run_id, reservation.run_incarnation
        ),
        reservation,
    )
    if record.reconciliation is not None:
        return record.reconciliation
    if record.result is not None:
        return ledger.record_model_reconciliation(
            reconcile_model_call_usage(record.result), record.result
        )
    if record.started is not None:
        if record.started.claim_generation >= claim.generation:
            return None
        return _settle_model_result(
            ledger,
            record.started,
            claim.generation,
            owner_token,
            ModelTokenUsage(input_tokens=None, output_tokens=None),
        )
    started = _model_started(
        reservation,
        f"model-{reservation.reservation_digest.removeprefix('sha256:')}",
        claim.generation,
        _monotonic_timestamp(claim_now_ns(), "claim_now_ns started"),
    )
    started, created = ledger.record_model_started(started, owner_token=owner_token)
    if not created:
        if started.claim_generation >= claim.generation:
            return None
        return _settle_model_result(
            ledger,
            started,
            claim.generation,
            owner_token,
            ModelTokenUsage(input_tokens=None, output_tokens=None),
        )
    return _ModelExecutionReady(started=started, claim_generation=claim.generation)


def execute_model_call_with_budget(
    run_id: str,
    run_incarnation: str,
    call_id: str,
    request_digest: str,
    model_identity: str,
    maximum_input_tokens: int,
    maximum_output_tokens: int,
    execute: Callable[[ModelCallReservation], ModelTokenUsage],
    *,
    config: AdaptivePolicyConfig,
    ledger: ModelBudgetLedger,
    claim_now_ns: Callable[[], int] = time.time_ns,
    owner_token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    wait: Callable[[float], None] = time.sleep,
) -> ModelCallReconciliation:
    """Execute once after STARTED; replay conservatively settles an unknown call."""

    for callback, name in (
        (execute, "execute"),
        (claim_now_ns, "claim_now_ns"),
        (owner_token_factory, "owner_token_factory"),
        (wait, "wait"),
    ):
        if not callable(callback):
            raise TypeError(f"{name} must be callable")
    reservation, owner_token = _prepare_model_execution(
        run_id,
        run_incarnation,
        call_id,
        request_digest,
        model_identity,
        maximum_input_tokens,
        maximum_output_tokens,
        config=config,
        ledger=ledger,
        owner_token_factory=owner_token_factory,
    )
    while True:
        step = _claim_model_execution_step(
            ledger, reservation, owner_token, claim_now_ns=claim_now_ns
        )
        if step is None:
            wait(FOLLOWER_POLL_SECONDS)
            continue
        if isinstance(step, ModelCallReconciliation):
            return step
        try:
            usage = _revalidate(execute(reservation), ModelTokenUsage, "model usage")
        except BaseException as error:
            _settle_model_failure(
                ledger,
                step.started,
                step.claim_generation,
                owner_token,
                getattr(error, "model_usage", None)
                if type(getattr(error, "model_usage", None)) is ModelTokenUsage
                else ModelTokenUsage(input_tokens=None, output_tokens=None),
            )
            raise
        return _settle_model_result(
            ledger, step.started, step.claim_generation, owner_token, usage
        )


async def execute_model_call_with_budget_async(
    run_id: str,
    run_incarnation: str,
    call_id: str,
    request_digest: str,
    model_identity: str,
    maximum_input_tokens: int,
    maximum_output_tokens: int,
    execute: Callable[[ModelCallReservation], Awaitable[ModelTokenUsage]],
    *,
    config: AdaptivePolicyConfig,
    ledger: ModelBudgetLedger,
    claim_now_ns: Callable[[], int] = time.time_ns,
    owner_token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ModelCallReconciliation:
    """Async counterpart of :func:`execute_model_call_with_budget`.

    The provider is invoked only after the durable ``started`` event.  A
    cancellation after that boundary has unknown provider usage, so it is
    settled with the reservation maxima before cancellation is re-raised.
    """

    for callback, name in (
        (execute, "execute"),
        (claim_now_ns, "claim_now_ns"),
        (owner_token_factory, "owner_token_factory"),
        (wait, "wait"),
    ):
        if not callable(callback):
            raise TypeError(f"{name} must be callable")
    reservation, owner_token = _prepare_model_execution(
        run_id,
        run_incarnation,
        call_id,
        request_digest,
        model_identity,
        maximum_input_tokens,
        maximum_output_tokens,
        config=config,
        ledger=ledger,
        owner_token_factory=owner_token_factory,
    )
    while True:
        step = _claim_model_execution_step(
            ledger, reservation, owner_token, claim_now_ns=claim_now_ns
        )
        if step is None:
            waiting = wait(FOLLOWER_POLL_SECONDS)
            if not inspect.isawaitable(waiting):
                raise TypeError("wait must return an awaitable")
            await waiting
            continue
        if isinstance(step, ModelCallReconciliation):
            return step
        try:
            usage = _revalidate(
                await execute(reservation), ModelTokenUsage, "model usage"
            )
        except BaseException as error:
            _settle_model_failure(
                ledger,
                step.started,
                step.claim_generation,
                owner_token,
                getattr(error, "model_usage", None)
                if type(getattr(error, "model_usage", None)) is ModelTokenUsage
                else ModelTokenUsage(input_tokens=None, output_tokens=None),
            )
            raise
        return _settle_model_result(
            ledger, step.started, step.claim_generation, owner_token, usage
        )


def _load_model_ledger_records(
    ledger: ModelBudgetLedger,
    run_id: str,
    run_incarnation: str,
) -> tuple[ModelBudgetLedgerRecord, ...]:
    load_records = getattr(ledger, "load_model_records", None)
    if not callable(load_records):
        raise TypeError("ledger must provide load_model_records")
    records = load_records(run_id, run_incarnation)
    if type(records) is not tuple:
        raise TypeError("ledger load_model_records must return a tuple")
    return tuple(
        _revalidate(item, ModelBudgetLedgerRecord, "model budget ledger record")
        for item in records
    )


def _model_budget_chain(
    records: tuple[ModelBudgetLedgerRecord, ...],
    initial_budget: ModelBudgetState,
) -> tuple[ModelBudgetState, ModelBudgetLedgerRecord | None]:
    expected = initial_budget
    outstanding: ModelBudgetLedgerRecord | None = None
    seen_call_ids: set[str] = set()
    for record in records:
        reservation = record.reservation
        if reservation.call_id in seen_call_ids:
            raise BudgetAdmissionError("model call identity is duplicated")
        seen_call_ids.add(reservation.call_id)
        if reservation.budget_before != expected:
            raise BudgetAdmissionError("model budget ledger has a broken cost chain")
        if record.reconciliation is None:
            if outstanding is not None:
                raise BudgetAdmissionError(
                    "only one model reservation may be outstanding"
                )
            outstanding = record
            continue
        expected_reconciliation = (
            reconcile_model_call_usage(record.result) if record.result else None
        )
        if (
            expected_reconciliation is None
            or record.reconciliation != expected_reconciliation
        ):
            raise BudgetAdmissionError("model budget reconciliation is not canonical")
        expected = record.reconciliation.budget_after
    return expected, outstanding


def _require_model_budget_available(
    budget: ModelBudgetState,
    maximum_input_tokens: int,
    maximum_output_tokens: int,
) -> None:
    if budget.remaining_model_calls < 1:
        raise BudgetExhaustedError("model call budget is exhausted")
    if maximum_input_tokens > budget.remaining_input_tokens:
        raise BudgetExhaustedError("model input token budget is exhausted")
    if maximum_output_tokens > budget.remaining_output_tokens:
        raise BudgetExhaustedError("model output token budget is exhausted")
    if maximum_input_tokens + maximum_output_tokens > budget.remaining_total_tokens:
        raise BudgetExhaustedError("model total token budget is exhausted")


def _model_record_for_reservation(
    records: tuple[ModelBudgetLedgerRecord, ...],
    reservation: ModelCallReservation,
) -> ModelBudgetLedgerRecord:
    matches = [record for record in records if record.reservation == reservation]
    if len(matches) != 1:
        raise BudgetReconciliationError(
            "durable model reservation is missing or conflicting"
        )
    return matches[0]


def _model_started(
    reservation: ModelCallReservation,
    invocation_id: str,
    claim_generation: int,
    started_at_ns: int,
) -> ModelCallStarted:
    values = {
        "reservation": reservation,
        "invocation_id": invocation_id,
        "claim_generation": claim_generation,
        "started_at_ns": started_at_ns,
    }
    return ModelCallStarted(**values, started_digest=canonical_digest(values))


def _model_result(
    started: ModelCallStarted,
    claim_generation: int,
    usage: ModelTokenUsage,
) -> ModelCallResult:
    values = {
        "reservation": started.reservation,
        "invocation_id": started.invocation_id,
        "started_claim_generation": started.claim_generation,
        "claim_generation": claim_generation,
        "usage": usage,
    }
    return ModelCallResult(**values, result_digest=canonical_digest(values))


def _settle_model_failure(
    ledger: ModelBudgetLedger,
    started: ModelCallStarted,
    claim_generation: int,
    owner_token: str,
    usage: ModelTokenUsage,
) -> None:
    """Best-effort settlement before the original error escapes.

    A failed write intentionally remains visible as an outstanding STARTED call.
    The loop then refuses to publish a clean terminal checkpoint.
    """

    try:
        _settle_model_result(
            ledger,
            started,
            claim_generation,
            owner_token,
            usage,
        )
    except BaseException:
        pass
