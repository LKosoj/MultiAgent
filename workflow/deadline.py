"""Cooperative monotonic deadlines for workflow execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import time
from typing import Any, Awaitable, Callable, Optional


class WorkflowDeadlineExceeded(TimeoutError):
    """The workflow no longer has time for the requested boundary."""


class _StepRaisedTimeout(TimeoutError):
    """Internal sentinel distinguishing a step raising its own TimeoutError
    from asyncio.wait_for cutting the step off (see execute_step_attempt)."""


@dataclass(frozen=True, slots=True)
class DeadlineBudget:
    """Immutable wall/monotonic representation of one workflow deadline."""

    deadline_monotonic: float
    deadline_at_ms: int
    monotonic: Callable[[], float] = field(
        default=time.monotonic,
        repr=False,
        compare=False,
    )
    wall_time: Callable[[], float] = field(
        default=time.time,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if isinstance(self.deadline_monotonic, bool) or not isinstance(
            self.deadline_monotonic,
            (int, float),
        ):
            raise TypeError("deadline_monotonic must be a finite number")
        if not math.isfinite(float(self.deadline_monotonic)):
            raise ValueError("deadline_monotonic must be a finite number")
        if isinstance(self.deadline_at_ms, bool) or not isinstance(
            self.deadline_at_ms,
            int,
        ):
            raise TypeError("deadline_at_ms must be an integer")
        if self.deadline_at_ms < 0:
            raise ValueError("deadline_at_ms must be non-negative")
        if not callable(self.monotonic) or not callable(self.wall_time):
            raise TypeError("deadline clocks must be callable")

    @classmethod
    def from_duration(
        cls,
        duration_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ) -> "DeadlineBudget":
        duration = cls._positive_seconds(duration_seconds, "duration_seconds")
        monotonic_now = float(monotonic())
        wall_now = float(wall_time())
        if not math.isfinite(monotonic_now) or not math.isfinite(wall_now):
            raise ValueError("deadline clocks must return finite numbers")
        return cls(
            deadline_monotonic=monotonic_now + duration,
            deadline_at_ms=int((wall_now + duration) * 1000),
            monotonic=monotonic,
            wall_time=wall_time,
        )

    @classmethod
    def from_deadline_at_ms(
        cls,
        deadline_at_ms: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ) -> "DeadlineBudget":
        if isinstance(deadline_at_ms, bool) or not isinstance(deadline_at_ms, int):
            raise TypeError("deadline_at_ms must be an integer")
        if deadline_at_ms < 0:
            raise ValueError("deadline_at_ms must be non-negative")
        monotonic_now = float(monotonic())
        wall_now = float(wall_time())
        if not math.isfinite(monotonic_now) or not math.isfinite(wall_now):
            raise ValueError("deadline clocks must return finite numbers")
        remaining = max(0.0, deadline_at_ms / 1000 - wall_now)
        return cls(
            deadline_monotonic=monotonic_now + remaining,
            deadline_at_ms=deadline_at_ms,
            monotonic=monotonic,
            wall_time=wall_time,
        )

    def remaining_seconds(self) -> float:
        now = float(self.monotonic())
        if not math.isfinite(now):
            raise ValueError("monotonic clock must return a finite number")
        return max(0.0, float(self.deadline_monotonic) - now)

    def require_remaining(self, boundary: str = "workflow") -> float:
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise WorkflowDeadlineExceeded(
                f"Workflow deadline exceeded before {boundary}"
            )
        return remaining

    def effective_timeout(
        self,
        attempt_timeout: float | None,
        *,
        boundary: str = "step attempt",
    ) -> float:
        remaining = self.require_remaining(boundary)
        if attempt_timeout is None:
            return remaining
        timeout = self._positive_seconds(attempt_timeout, "attempt_timeout")
        return min(timeout, remaining)

    def require_retry_delay(self, delay_seconds: float) -> float:
        delay = self._non_negative_seconds(delay_seconds, "delay_seconds")
        remaining = self.require_remaining("retry backoff")
        if delay >= remaining:
            raise WorkflowDeadlineExceeded(
                "Workflow deadline would be exhausted by retry backoff"
            )
        return delay

    @staticmethod
    def _positive_seconds(value: float, field_name: str) -> float:
        seconds = DeadlineBudget._non_negative_seconds(value, field_name)
        if seconds <= 0:
            raise ValueError(f"{field_name} must be positive")
        return seconds

    @staticmethod
    def _non_negative_seconds(value: float, field_name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{field_name} must be a finite number")
        seconds = float(value)
        if not math.isfinite(seconds):
            raise ValueError(f"{field_name} must be a finite number")
        if seconds < 0:
            raise ValueError(f"{field_name} must be non-negative")
        return seconds


def validate_attempt_timeout(value: Optional[float]) -> Optional[float]:
    """Validate a per-attempt timeout (None disables the per-attempt timeout)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("attempt_timeout must be a finite number")
    timeout = float(value)
    if timeout <= 0 or timeout == float("inf") or timeout != timeout:
        raise ValueError("attempt_timeout must be a positive finite number")
    return timeout


async def execute_step_attempt(
    step_id: str,
    step_func: Callable[[Any], Awaitable[Any]],
    context: Any,
    *,
    attempt_timeout: Optional[float],
    deadline: Optional["DeadlineBudget"],
) -> Any:
    """Execute one step attempt, honoring an optional per-attempt timeout and
    an optional cooperative workflow deadline.

    Shared by AdaptiveRetryEngine.execute_with_retry
    (workflow/resilience/retry.py) and the enhanced engine's non-retryable
    path (workflow/enhanced_engine.py:_execute_non_retryable_attempt) so both
    honor identical timeout/deadline semantics.
    """
    remaining = None
    if deadline is not None:
        remaining = deadline.require_remaining(f"step '{step_id}' attempt")
        timeout = deadline.effective_timeout(
            attempt_timeout,
            boundary=f"step '{step_id}' attempt",
        )
    else:
        timeout = validate_attempt_timeout(attempt_timeout)

    if timeout is None:
        return await step_func(context)

    budget_limited = (
        deadline is not None
        and (
            attempt_timeout is None
            or remaining is not None and attempt_timeout >= remaining
        )
    )

    async def _run_attempt() -> Any:
        try:
            return await step_func(context)
        except WorkflowDeadlineExceeded:
            raise
        except asyncio.TimeoutError as exc:
            raise _StepRaisedTimeout(str(exc)) from exc

    try:
        result = await asyncio.wait_for(_run_attempt(), timeout=timeout)
    except WorkflowDeadlineExceeded:
        raise
    except _StepRaisedTimeout as exc:
        if deadline is not None and deadline.remaining_seconds() <= 0:
            raise WorkflowDeadlineExceeded(
                f"Workflow deadline exceeded during step '{step_id}'"
            ) from exc
        raise TimeoutError(
            str(exc) or f"Step '{step_id}' operation timed out"
        ) from exc
    except asyncio.TimeoutError as exc:
        if budget_limited or (
            deadline is not None and deadline.remaining_seconds() <= 0
        ):
            raise WorkflowDeadlineExceeded(
                f"Workflow deadline exceeded during step '{step_id}'"
            ) from exc
        raise TimeoutError(
            f"Step '{step_id}' attempt timed out after {timeout:.3f} seconds"
        ) from exc

    if deadline is not None:
        deadline.require_remaining(f"step '{step_id}' attempt completion")
    return result
