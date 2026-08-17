"""Pure durable transitions for deterministic solver gate results."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    CheckKind,
    CheckResult,
    CheckStatus,
    ExecutionResult,
    SolverState,
    SolverStopReason,
)
from .solver_loop import (
    SolverConflictError,
    SolverRevisionError,
    SolverValidationError,
    _revalidate_exact,
)

_GATE_ORDER = (
    CheckKind.SAFETY,
    CheckKind.SCHEMA,
    CheckKind.SEMANTIC,
    CheckKind.EXPLAIN,
    CheckKind.EXECUTION,
)


@dataclass(frozen=True, slots=True)
class SolverGateTransitionResult:
    base_revision: int
    state: SolverState
    check_result: CheckResult
    execution_result: ExecutionResult | None = None

    def __post_init__(self) -> None:
        if (
            type(self.base_revision) is not int
            or type(self.state) is not SolverState
            or type(self.check_result) is not CheckResult
            or (
                self.execution_result is not None
                and type(self.execution_result) is not ExecutionResult
            )
        ):
            raise TypeError("SolverGateTransitionResult has invalid field types")


def append_solver_check_result(
    state: SolverState,
    check_result: CheckResult,
    *,
    base_revision: int,
) -> SolverGateTransitionResult:
    """Append one blocking or non-execution gate result with CAS semantics."""

    current = _validated_inputs(state, check_result, base_revision)
    _require_next_gate(current, check_result)
    if (
        check_result.check_kind is CheckKind.EXECUTION
        and check_result.status is CheckStatus.PASSED
    ):
        raise SolverConflictError(
            "passed execution check requires an atomic ExecutionResult"
        )
    next_state = _next_state(
        current,
        check_result=check_result,
        execution_result=None,
    )
    return SolverGateTransitionResult(
        base_revision=base_revision,
        state=next_state,
        check_result=check_result,
        execution_result=None,
    )


def append_solver_execution_result(
    state: SolverState,
    check_result: CheckResult,
    execution_result: ExecutionResult,
    *,
    base_revision: int,
) -> SolverGateTransitionResult:
    """Atomically append the final execution check and its typed evidence."""

    current = _validated_inputs(state, check_result, base_revision)
    execution = _revalidate_exact(
        execution_result,
        ExecutionResult,
        SolverValidationError,
        "execution_result",
    )
    _require_next_gate(current, check_result)
    if check_result.check_kind is not CheckKind.EXECUTION:
        raise SolverConflictError("ExecutionResult requires an execution check")
    if execution.candidate_id != check_result.candidate_id:
        raise SolverConflictError("execution candidate does not match check candidate")
    if any(
        item.execution_id == execution.execution_id
        for item in current.execution_results
    ) or any(
        item.candidate_id == execution.candidate_id
        for item in current.execution_results
    ):
        raise SolverConflictError("execution result already exists")
    if execution.success != (check_result.status is CheckStatus.PASSED):
        raise SolverConflictError("execution success contradicts execution check")

    next_state = _next_state(
        current,
        check_result=check_result,
        execution_result=execution,
    )
    return SolverGateTransitionResult(
        base_revision=base_revision,
        state=next_state,
        check_result=check_result,
        execution_result=execution,
    )


def _validated_inputs(
    state: SolverState,
    check_result: CheckResult,
    base_revision: int,
) -> SolverState:
    current = _revalidate_exact(
        state,
        SolverState,
        SolverValidationError,
        "state",
    )
    checked = _revalidate_exact(
        check_result,
        CheckResult,
        SolverValidationError,
        "check_result",
    )
    if type(base_revision) is not int or base_revision < 0:
        raise SolverRevisionError("base_revision must be a non-negative integer")
    if base_revision != current.revision:
        raise SolverRevisionError("base_revision does not match SolverState revision")
    if current.stop_reason is not None:
        raise SolverConflictError("SolverState is already stopped")
    if checked.candidate_id not in {
        candidate.candidate_id for candidate in current.sql_candidates
    }:
        raise SolverConflictError("check candidate is not in SolverState")
    if any(item.check_id == checked.check_id for item in current.check_results):
        raise SolverConflictError("check_id already exists")
    _validate_all_gate_histories(current)
    return current


def _require_next_gate(state: SolverState, check_result: CheckResult) -> None:
    candidate_checks = tuple(
        item
        for item in state.check_results
        if item.candidate_id == check_result.candidate_id
    )
    if len(candidate_checks) >= len(_GATE_ORDER):
        raise SolverConflictError("candidate gate sequence is complete")
    for index, item in enumerate(candidate_checks):
        if item.check_kind is not _GATE_ORDER[index]:
            raise SolverConflictError("candidate gate history is not a prefix")
        if item.status is not CheckStatus.PASSED:
            raise SolverConflictError("blocking gate prevents later checks")
    expected = _GATE_ORDER[len(candidate_checks)]
    if check_result.check_kind is not expected:
        raise SolverConflictError("check is not the next candidate gate")


def _validate_all_gate_histories(state: SolverState) -> None:
    executions = {item.candidate_id: item for item in state.execution_results}
    for candidate in state.sql_candidates:
        checks = tuple(
            item
            for item in state.check_results
            if item.candidate_id == candidate.candidate_id
        )
        if len(checks) > len(_GATE_ORDER):
            raise SolverConflictError("candidate gate history is too long")
        for index, check in enumerate(checks):
            if check.check_kind is not _GATE_ORDER[index]:
                raise SolverConflictError("candidate gate history is not a prefix")
            if check.status is not CheckStatus.PASSED and index != len(checks) - 1:
                raise SolverConflictError("checks follow a blocking gate")
        execution = executions.get(candidate.candidate_id)
        has_execution_check = bool(
            checks and checks[-1].check_kind is CheckKind.EXECUTION
        )
        if execution is not None:
            if not has_execution_check or execution.success != (
                checks[-1].status is CheckStatus.PASSED
            ):
                raise SolverConflictError(
                    "execution evidence contradicts candidate gate history"
                )
        elif has_execution_check and checks[-1].status is CheckStatus.PASSED:
            raise SolverConflictError(
                "passed execution check lacks atomic execution evidence"
            )
    if state.stop_reason is SolverStopReason.SOLVED:
        selected_execution = executions.get(state.selected_candidate_id)
        if selected_execution is None or not selected_execution.success:
            raise SolverConflictError("SOLVED state lacks successful execution")
    elif state.selected_candidate_id is not None:
        raise SolverConflictError("open state cannot select a candidate")


def _next_state(
    state: SolverState,
    *,
    check_result: CheckResult,
    execution_result: ExecutionResult | None,
) -> SolverState:
    updates: dict[str, object] = {
        **state.model_dump(mode="python"),
        "revision": state.revision + 1,
        "check_results": (*state.check_results, check_result),
    }
    if execution_result is not None:
        updates["execution_results"] = (
            *state.execution_results,
            execution_result,
        )
        if execution_result.success:
            updates["selected_candidate_id"] = execution_result.candidate_id
            updates["stop_reason"] = SolverStopReason.SOLVED
    try:
        next_state = SolverState.model_validate(updates)
    except Exception as exc:
        raise SolverConflictError("gate result conflicts with SolverState") from exc
    return _revalidate_exact(
        next_state,
        SolverState,
        SolverValidationError,
        "next_state",
    )


__all__ = [
    "SolverGateTransitionResult",
    "append_solver_check_result",
    "append_solver_execution_result",
]
