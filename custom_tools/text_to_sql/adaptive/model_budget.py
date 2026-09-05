"""Closed DTOs for durable model-call budget accounting.

This module intentionally has no coordinator or model-provider integration.
It describes only the immutable facts that the budget ledger records.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .models import Digest, Id, NonNegativeInt, StrictModel
from .serialization import _register_internal_decode_models, canonical_digest


_SHA256_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
SQLITE_SIGNED_INTEGER_MAX = 2**63 - 1
# Conservative estimate for sizing an LLM prompt from its byte length without
# tokenizing it; both model-input-size guards below use the same constant.
APPROXIMATE_BYTES_PER_TOKEN = 4
ModelIdentity = Annotated[str, Field(min_length=1, max_length=512)]
ClaimGeneration = Annotated[
    int,
    Field(ge=0, le=SQLITE_SIGNED_INTEGER_MAX),
]


class ModelUsageBudgetError(ValueError):
    """Observed model usage cannot be reconciled against its reservation."""


class ModelBudgetLimits(StrictModel):
    """Configured v2 limits for model calls and their token usage."""

    model_calls: NonNegativeInt
    input_tokens_per_call: NonNegativeInt
    output_tokens_per_call: NonNegativeInt
    total_tokens: NonNegativeInt

    @model_validator(mode="after")
    def validate_nonempty_budget(self) -> ModelBudgetLimits:
        if self.model_calls == 0 or self.total_tokens == 0:
            raise ValueError("model budget must allow calls and total tokens")
        return self


class ModelBudgetState(StrictModel):
    """One independent, replayable accounting chain for model resources."""

    initial_model_calls: NonNegativeInt
    used_model_calls: NonNegativeInt
    remaining_model_calls: NonNegativeInt
    initial_input_tokens: NonNegativeInt
    used_input_tokens: NonNegativeInt
    remaining_input_tokens: NonNegativeInt
    initial_output_tokens: NonNegativeInt
    used_output_tokens: NonNegativeInt
    remaining_output_tokens: NonNegativeInt
    initial_total_tokens: NonNegativeInt
    used_total_tokens: NonNegativeInt
    remaining_total_tokens: NonNegativeInt

    @model_validator(mode="after")
    def validate_remaining(self) -> ModelBudgetState:
        for name in ("model_calls", "input_tokens", "output_tokens", "total_tokens"):
            if getattr(self, f"initial_{name}") - getattr(
                self, f"used_{name}"
            ) != getattr(self, f"remaining_{name}"):
                raise ValueError(f"remaining_{name} must equal initial minus used")
        if self.used_total_tokens != self.used_input_tokens + self.used_output_tokens:
            raise ValueError("used_total_tokens must equal input plus output tokens")
        return self


class ModelTokenUsage(StrictModel):
    """Provider-reported token counts; ``None`` means that count was unavailable."""

    input_tokens: NonNegativeInt | None
    output_tokens: NonNegativeInt | None


class ModelCallReservation(StrictModel):
    contract_version: Literal[1] = 1
    run_id: Id
    run_incarnation: Id
    call_id: Id
    request_digest: Digest
    model_identity: ModelIdentity
    policy_digest: Digest
    budget_before: ModelBudgetState
    maximum_input_tokens: NonNegativeInt
    maximum_output_tokens: NonNegativeInt
    reservation_digest: Digest

    @field_validator("request_digest", "policy_digest", "reservation_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256_DIGEST_RE.fullmatch(value):
            raise ValueError("digest must be exact sha256 lowercase hex")
        return value

    @field_validator("model_identity")
    @classmethod
    def validate_model_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_identity must contain non-whitespace text")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> ModelCallReservation:
        if self.reservation_digest != model_reservation_digest(self):
            raise ValueError("reservation_digest is not canonical")
        return self


class ModelCallStarted(StrictModel):
    contract_version: Literal[1] = 1
    reservation: ModelCallReservation
    invocation_id: Id
    claim_generation: ClaimGeneration
    started_at_ns: NonNegativeInt
    started_digest: Digest

    @field_validator("started_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256_DIGEST_RE.fullmatch(value):
            raise ValueError("digest must be exact sha256 lowercase hex")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> ModelCallStarted:
        if self.started_digest != model_started_digest(self):
            raise ValueError("started_digest is not canonical")
        return self


class ModelCallResult(StrictModel):
    """One provider response, including explicitly unavailable usage fields."""

    contract_version: Literal[1] = 1
    reservation: ModelCallReservation
    invocation_id: Id
    started_claim_generation: ClaimGeneration
    claim_generation: ClaimGeneration
    usage: ModelTokenUsage
    result_digest: Digest

    @field_validator("result_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256_DIGEST_RE.fullmatch(value):
            raise ValueError("digest must be exact sha256 lowercase hex")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> ModelCallResult:
        if self.claim_generation < self.started_claim_generation:
            raise ValueError("result claim generation predates started generation")
        if self.claim_generation > self.started_claim_generation and (
            self.usage.input_tokens is not None or self.usage.output_tokens is not None
        ):
            raise ValueError("takeover result usage must be conservative")
        if self.result_digest != model_result_digest(self):
            raise ValueError("result_digest is not canonical")
        return self


class ModelCallReconciliation(StrictModel):
    contract_version: Literal[1] = 1
    reservation: ModelCallReservation
    invocation_id: Id
    actual_usage: ModelTokenUsage
    charged_input_tokens: NonNegativeInt
    charged_output_tokens: NonNegativeInt
    charged_total_tokens: NonNegativeInt
    budget_after: ModelBudgetState
    usage_was_conservative: bool
    reconciliation_digest: Digest

    @field_validator("reconciliation_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256_DIGEST_RE.fullmatch(value):
            raise ValueError("digest must be exact sha256 lowercase hex")
        return value

    @model_validator(mode="after")
    def validate_transition(self) -> ModelCallReconciliation:
        budget_after, charged_input, charged_output = model_charge(
            self.reservation,
            self.actual_usage,
        )
        if (
            self.charged_input_tokens != charged_input
            or self.charged_output_tokens != charged_output
            or self.charged_total_tokens != charged_input + charged_output
            or self.budget_after != budget_after
        ):
            raise ValueError(
                "model reconciliation does not match the budget transition"
            )
        if self.usage_was_conservative != (
            self.actual_usage.input_tokens is None
            or self.actual_usage.output_tokens is None
        ):
            raise ValueError("usage_was_conservative must report unavailable usage")
        if self.reconciliation_digest != model_reconciliation_digest(self):
            raise ValueError("reconciliation_digest is not canonical")
        return self


class ModelBudgetLedgerRecord(StrictModel):
    reservation: ModelCallReservation
    started: ModelCallStarted | None
    result: ModelCallResult | None
    reconciliation: ModelCallReconciliation | None

    @model_validator(mode="after")
    def validate_sequence(self) -> ModelBudgetLedgerRecord:
        if self.result is not None:
            if self.started is None or self.result.reservation != self.reservation:
                raise ValueError("model result requires matching started reservation")
            if self.result.invocation_id != self.started.invocation_id:
                raise ValueError("model result invocation does not match started event")
            if self.result.started_claim_generation != self.started.claim_generation:
                raise ValueError("model result started generation does not match")
            if self.result.claim_generation > self.started.claim_generation and (
                self.result.usage.input_tokens is not None
                or self.result.usage.output_tokens is not None
            ):
                raise ValueError("takeover model result usage is not conservative")
        if self.reconciliation is not None:
            if (
                self.result is None
                or self.reconciliation.reservation != self.reservation
            ):
                raise ValueError("model reconciliation requires matching result")
            if self.reconciliation.invocation_id != self.result.invocation_id:
                raise ValueError(
                    "model reconciliation invocation does not match result"
                )
        return self


def model_reservation_digest(reservation: ModelCallReservation) -> str:
    return canonical_digest(
        {
            "run_id": reservation.run_id,
            "run_incarnation": reservation.run_incarnation,
            "call_id": reservation.call_id,
            "request_digest": reservation.request_digest,
            "model_identity": reservation.model_identity,
            "policy_digest": reservation.policy_digest,
            "budget_before": reservation.budget_before,
            "maximum_input_tokens": reservation.maximum_input_tokens,
            "maximum_output_tokens": reservation.maximum_output_tokens,
        }
    )


def model_started_digest(started: ModelCallStarted) -> str:
    return canonical_digest(
        {
            "reservation": started.reservation,
            "invocation_id": started.invocation_id,
            "claim_generation": started.claim_generation,
            "started_at_ns": started.started_at_ns,
        }
    )


def model_result_digest(result: ModelCallResult) -> str:
    return canonical_digest(
        {
            "reservation": result.reservation,
            "invocation_id": result.invocation_id,
            "started_claim_generation": result.started_claim_generation,
            "claim_generation": result.claim_generation,
            "usage": result.usage,
        }
    )


def model_reconciliation_digest(reconciliation: ModelCallReconciliation) -> str:
    return canonical_digest(
        {
            "reservation": reconciliation.reservation,
            "invocation_id": reconciliation.invocation_id,
            "actual_usage": reconciliation.actual_usage,
            "charged_input_tokens": reconciliation.charged_input_tokens,
            "charged_output_tokens": reconciliation.charged_output_tokens,
            "charged_total_tokens": reconciliation.charged_total_tokens,
            "budget_after": reconciliation.budget_after,
            "usage_was_conservative": reconciliation.usage_was_conservative,
        }
    )


def model_charge(
    reservation: ModelCallReservation,
    usage: ModelTokenUsage,
) -> tuple[ModelBudgetState, int, int]:
    input_tokens = (
        reservation.maximum_input_tokens
        if usage.input_tokens is None
        else usage.input_tokens
    )
    output_tokens = (
        reservation.maximum_output_tokens
        if usage.output_tokens is None
        else usage.output_tokens
    )
    before = reservation.budget_before
    if (
        usage.input_tokens is not None
        and input_tokens > reservation.maximum_input_tokens
    ):
        raise ModelUsageBudgetError("model input usage exceeds the reserved maximum")
    if (
        usage.output_tokens is not None
        and output_tokens > reservation.maximum_output_tokens
    ):
        raise ModelUsageBudgetError("model output usage exceeds the reserved maximum")
    if before.remaining_model_calls < 1:
        raise ModelUsageBudgetError("model call budget is already exhausted")
    if input_tokens > before.remaining_input_tokens:
        raise ModelUsageBudgetError("model input usage exceeds the remaining budget")
    if output_tokens > before.remaining_output_tokens:
        raise ModelUsageBudgetError("model output usage exceeds the remaining budget")
    if input_tokens + output_tokens > before.remaining_total_tokens:
        raise ModelUsageBudgetError("model total usage exceeds the remaining budget")
    return (
        ModelBudgetState(
            initial_model_calls=before.initial_model_calls,
            used_model_calls=before.used_model_calls + 1,
            remaining_model_calls=before.remaining_model_calls - 1,
            initial_input_tokens=before.initial_input_tokens,
            used_input_tokens=before.used_input_tokens + input_tokens,
            remaining_input_tokens=before.remaining_input_tokens - input_tokens,
            initial_output_tokens=before.initial_output_tokens,
            used_output_tokens=before.used_output_tokens + output_tokens,
            remaining_output_tokens=before.remaining_output_tokens - output_tokens,
            initial_total_tokens=before.initial_total_tokens,
            used_total_tokens=before.used_total_tokens + input_tokens + output_tokens,
            remaining_total_tokens=before.remaining_total_tokens
            - input_tokens
            - output_tokens,
        ),
        input_tokens,
        output_tokens,
    )


_register_internal_decode_models(
    ModelBudgetLimits,
    ModelBudgetState,
    ModelTokenUsage,
    ModelCallReservation,
    ModelCallStarted,
    ModelCallResult,
    ModelCallReconciliation,
    ModelBudgetLedgerRecord,
)
