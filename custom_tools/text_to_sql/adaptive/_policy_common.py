"""Shared definitions for adaptive research policy modules."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import re
from typing import Annotated

from pydantic import Field, ValidationError


from .models import (
    BudgetState,
    EvidenceCost,
)
from .probes import ProbeResult, ProbeStatus


class ResearchPolicyError(ValueError):
    """Base error for rejected research policy input or transition."""


class ResearchPolicyConfigError(ResearchPolicyError):
    """Adaptive policy YAML does not satisfy the closed v1 schema."""


class ActionIdentityError(ResearchPolicyError):
    """Research action identity is malformed or non-canonical."""


class BudgetAdmissionError(ResearchPolicyError):
    """Research work cannot be admitted under the current immutable state."""


class BudgetExhaustedError(BudgetAdmissionError):
    """At least one configured resource limit has been reached."""


class BudgetConflictError(BudgetAdmissionError):
    """The proposed action or revision has already been reserved or observed."""


class BudgetReconciliationError(ResearchPolicyError):
    """Observed probe cost cannot be charged exactly once to its reservation."""


class ProbeExecutionFailure(Exception):
    """Typed callback failure carrying costs not visible from elapsed time alone."""

    def __init__(
        self,
        *,
        status: ProbeStatus,
        actual_cost: EvidenceCost,
        failure_code: str,
        summary: str,
    ) -> None:
        if status is ProbeStatus.SUCCESS:
            raise ValueError("ProbeExecutionFailure status cannot be success")
        self.status = status
        self.actual_cost = _revalidate(
            actual_cost,
            EvidenceCost,
            "probe execution failure cost",
        )
        self.failure_code = failure_code
        self.summary = summary
        super().__init__(summary)


PositiveInt = Annotated[int, Field(gt=0)]


NonNegativeInt = Annotated[int, Field(ge=0)]


POLICY_VERSION = 1


MODEL_BUDGET_POLICY_VERSION = 2


MAX_WALL_CLOCK_SECONDS = 14_400


MAX_MODEL_TOKENS = 0


MAX_MODEL_CALLS_V2 = MAX_MODEL_DECISIONS = 256


MAX_MODEL_INPUT_TOKENS_PER_CALL = 32_768


MAX_MODEL_OUTPUT_TOKENS_PER_CALL = 32_000


MAX_MODEL_TOTAL_TOKENS = 1_048_576


MAX_DB_PROBE_MS = MAX_WALL_CLOCK_SECONDS * 1_000


MAX_ACTIONS = 512


MAX_DB_PROBES = 384


MAX_RETURNED_ROWS = 5_000


MAX_INLINE_BYTES = 2 * 1024 * 1024


MAX_SAMPLE_ROWS = 50


FOLLOWER_POLL_SECONDS = 0.01


_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config/text_to_sql/adaptive.yaml"
)


_CONFIG_ENV_VAR = "TEXT_TO_SQL_ADAPTIVE_CONFIG_PATH"


_SHA256_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


_COST_FIELDS = (
    "wall_clock_ms",
    "model_calls",
    "model_tokens",
    "db_probe_ms",
    "rows",
    "bytes",
)


def _require_probe_only_cost(cost: EvidenceCost) -> None:
    """Keep model accounting exclusively in the model ledger."""

    if cost.model_calls != 0 or cost.model_tokens != 0:
        raise BudgetAdmissionError("probe costs cannot charge model resources")


def _monotonic_timestamp(value: int, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _elapsed_ms(started_ns: int, completed_ns: int) -> int:
    completed = _monotonic_timestamp(completed_ns, "monotonic_ns result")
    if completed < started_ns:
        raise ValueError("monotonic clock moved backwards")
    return (completed - started_ns + 999_999) // 1_000_000


def _utc_timestamp(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must be a UTC timestamp")
    return value


def _cost_with_elapsed(cost: EvidenceCost, elapsed_ms: int) -> EvidenceCost:
    values = cost.model_dump(mode="python")
    values["wall_clock_ms"] = max(cost.wall_clock_ms, elapsed_ms)
    return EvidenceCost(**values)


def _elapsed_cost(started_ns: int, completed_ns: int) -> EvidenceCost:
    return EvidenceCost(
        wall_clock_ms=_elapsed_ms(started_ns, completed_ns),
        model_calls=0,
        model_tokens=0,
        db_probe_ms=0,
        rows=0,
        bytes=0,
    )


def _with_measured_wall_cost(result: ProbeResult, elapsed_ms: int) -> ProbeResult:
    values = result.model_dump(mode="python", round_trip=True)
    values["cost"] = _cost_with_elapsed(result.cost, elapsed_ms)
    return ProbeResult.model_validate(values)


def _require_available_cost(budget: BudgetState, cost: EvidenceCost) -> None:
    for field_name in _COST_FIELDS:
        if getattr(cost, field_name) > getattr(budget, f"remaining_{field_name}"):
            raise BudgetExhaustedError(f"{field_name} budget is exhausted")


def _charge_cost_capped(
    budget: BudgetState,
    cost: EvidenceCost,
) -> tuple[BudgetState, EvidenceCost]:
    charged_values: dict[str, int] = {}
    budget_values: dict[str, int] = {}
    for field_name in _COST_FIELDS:
        initial = getattr(budget, f"initial_{field_name}")
        charged = min(
            getattr(cost, field_name),
            getattr(budget, f"remaining_{field_name}"),
        )
        used = getattr(budget, f"used_{field_name}") + charged
        charged_values[field_name] = charged
        budget_values[f"initial_{field_name}"] = initial
        budget_values[f"used_{field_name}"] = used
        budget_values[f"remaining_{field_name}"] = initial - used
    return BudgetState(**budget_values), EvidenceCost(**charged_values)


def _reservation_overrun(
    maximum_cost: EvidenceCost,
    actual_cost: EvidenceCost,
) -> EvidenceCost:
    return EvidenceCost(
        **{
            field_name: max(
                0,
                getattr(actual_cost, field_name) - getattr(maximum_cost, field_name),
            )
            for field_name in _COST_FIELDS
        }
    )


def _has_cost(cost: EvidenceCost) -> bool:
    return any(getattr(cost, field_name) > 0 for field_name in _COST_FIELDS)


def _revalidate(value, model_type, label: str):
    if not isinstance(value, model_type):
        raise TypeError(f"{label} has an invalid type")
    try:
        return model_type.model_validate(
            value.model_dump(mode="python", round_trip=True)
        )
    except ValidationError as exc:
        raise ResearchPolicyError(f"{label} violates its strict contract") from exc
