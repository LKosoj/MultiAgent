"""Public value types for canonical release progress."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


SCHEMA_VERSION = 3


class ReleaseProgressError(RuntimeError):
    """Raised when release progress cannot be authenticated or recovered."""


class ReleasePhase(str, Enum):
    BETWEEN_LEGS = "BETWEEN_LEGS"
    ACTIVE_LEG = "ACTIVE_LEG"
    AWAITING_REPAIR_DECISION = "AWAITING_REPAIR_DECISION"
    CONTINUING_ACTIVE_LEG = "CONTINUING_ACTIVE_LEG"
    AWAITING_POST_REPEAT_EVALUATION = "AWAITING_POST_REPEAT_EVALUATION"
    AWAITING_POST_REPEAT_REPAIR_DECISION = "AWAITING_POST_REPEAT_REPAIR_DECISION"
    FINALIZING_PARTIAL_STOP = "FINALIZING_PARTIAL_STOP"
    FINALIZING_POST_REPEAT_STOP = "FINALIZING_POST_REPEAT_STOP"
    DIAGNOSTIC_PARTIAL_STOP = "DIAGNOSTIC_PARTIAL_STOP"
    DIAGNOSTIC_POST_REPEAT_STOP = "DIAGNOSTIC_POST_REPEAT_STOP"
    COMPLETE = "COMPLETE"
    INVALID = "INVALID"


@dataclass(frozen=True)
class ReleaseProgress:
    phase: ReleasePhase
    active_benchmark: str | None
    active_repeat_ordinal: int | None
    active_seed: int | None
    in_flight_case_key: str | None
    candidate_sha256: str | None
    decision_sha256: str | None
    prefix_case_count: int | None
    invalid_reason: str | None


@dataclass(frozen=True)
class CompletedLeg:
    benchmark: str
    repeat_ordinal: int
    seed: int
    return_code: int
    artifact_handshake_sha256: str


@dataclass(frozen=True)
class PendingPostRepeatLeg:
    benchmark: str
    repeat_ordinal: int
    seed: int
    return_code: int


def active_leg_key(value: Mapping[str, object]) -> tuple[str, int, int]:
    benchmark = value.get("benchmark")
    repeat_ordinal = value.get("repeat_ordinal")
    seed = value.get("seed")
    if (
        not isinstance(benchmark, str)
        or not isinstance(repeat_ordinal, int)
        or isinstance(repeat_ordinal, bool)
        or not isinstance(seed, int)
        or isinstance(seed, bool)
    ):
        raise ValueError("active release leg identity is invalid")
    return benchmark, repeat_ordinal, seed


def active_leg_matches_plan(
    active_leg: Mapping[str, object], plan_item: Mapping[str, object]
) -> bool:
    return active_leg_key(active_leg) == (
        plan_item.get("benchmark"),
        plan_item.get("repeat_ordinal"),
        plan_item.get("seed"),
    )


def awaiting_repair_leg(
    plan_item: Mapping[str, object], candidate_sha256: str
) -> dict[str, object]:
    benchmark, repeat_ordinal, seed = active_leg_key(plan_item)
    return {
        "benchmark": benchmark,
        "repeat_ordinal": repeat_ordinal,
        "seed": seed,
        "candidate_sha256": candidate_sha256,
    }
