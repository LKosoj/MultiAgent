"""Public terminal results for Typed research and SQL solving."""

from __future__ import annotations

from .models import (
    ResearchReentryStatus,
    ResearchStopReason,
    SolverState,
    SolverStopReason,
)
from .ambiguity import AmbiguityReport
from workflow.text_to_sql_contract import (
    TextToSqlTerminalReasonCode,
    TextToSqlTerminalResult,
    TextToSqlTerminalStatus,
    text_to_sql_research_terminal_result,
)


_RESEARCH_STOP_REASON_CODES = {
    ResearchStopReason.COMPLETE: None,
    ResearchStopReason.AMBIGUOUS: TextToSqlTerminalReasonCode.RESEARCH_AMBIGUOUS,
    ResearchStopReason.UNSUPPORTED: TextToSqlTerminalReasonCode.RESEARCH_UNSUPPORTED,
    ResearchStopReason.STAGNATED: TextToSqlTerminalReasonCode.RESEARCH_STAGNATED,
    ResearchStopReason.BUDGET_EXHAUSTED: (
        TextToSqlTerminalReasonCode.RESEARCH_BUDGET_EXHAUSTED
    ),
    ResearchStopReason.DEADLINE_EXCEEDED: TextToSqlTerminalReasonCode.TIMED_OUT,
    ResearchStopReason.CANCELLED: TextToSqlTerminalReasonCode.CANCELLED,
    ResearchStopReason.TOOL_FAILURE: TextToSqlTerminalReasonCode.RESEARCH_TOOL_FAILURE,
    ResearchStopReason.PROTOCOL_FAILURE: (
        TextToSqlTerminalReasonCode.RESEARCH_PROTOCOL_FAILURE
    ),
}

_SOLVER_STOP_REASON_CODES = {
    SolverStopReason.SOLVED: None,
    SolverStopReason.MISSING_EVIDENCE: (
        TextToSqlTerminalReasonCode.SCHEMA_CLARIFICATION_REQUIRED
    ),
    SolverStopReason.NO_SAFE_CANDIDATE: (
        TextToSqlTerminalReasonCode.DETERMINISTIC_CHECK_REJECTED
    ),
    SolverStopReason.STAGNATED: TextToSqlTerminalReasonCode.RESEARCH_STAGNATED,
    SolverStopReason.BUDGET_EXHAUSTED: (
        TextToSqlTerminalReasonCode.RESEARCH_BUDGET_EXHAUSTED
    ),
    SolverStopReason.DEADLINE_EXCEEDED: TextToSqlTerminalReasonCode.TIMED_OUT,
    SolverStopReason.CANCELLED: TextToSqlTerminalReasonCode.CANCELLED,
    SolverStopReason.TOOL_FAILURE: TextToSqlTerminalReasonCode.RESEARCH_TOOL_FAILURE,
    SolverStopReason.PROTOCOL_FAILURE: (
        TextToSqlTerminalReasonCode.RESEARCH_PROTOCOL_FAILURE
    ),
}


def research_stop_terminal_result(
    run_id: object,
    stop_reason: object,
    ambiguity: object | None = None,
) -> TextToSqlTerminalResult | None:
    """Return the public terminal for one research stop, or None for COMPLETE."""

    if type(stop_reason) is not ResearchStopReason:
        return text_to_sql_research_terminal_result(
            run_id,
            TextToSqlTerminalReasonCode.RESEARCH_PROTOCOL_FAILURE.value,
            None,
        )
    if stop_reason is ResearchStopReason.AMBIGUOUS:
        if type(ambiguity) is not AmbiguityReport:
            return text_to_sql_research_terminal_result(
                run_id,
                TextToSqlTerminalReasonCode.RESEARCH_PROTOCOL_FAILURE.value,
                None,
            )
    elif ambiguity is not None:
        return text_to_sql_research_terminal_result(
            run_id,
            TextToSqlTerminalReasonCode.RESEARCH_PROTOCOL_FAILURE.value,
            None,
        )
    reason_code = _RESEARCH_STOP_REASON_CODES.get(stop_reason)
    if reason_code is None:
        return None
    return text_to_sql_research_terminal_result(run_id, reason_code.value, ambiguity)


def solver_stop_terminal_result(
    run_id: object,
    state: object,
) -> TextToSqlTerminalResult | None:
    if type(state) is not SolverState or type(run_id) is not str or not run_id:
        return text_to_sql_research_terminal_result(
            run_id,
            TextToSqlTerminalReasonCode.RESEARCH_PROTOCOL_FAILURE.value,
        )
    reason = _solver_terminal_reason(state)
    if reason is None:
        return None
    if reason in {
        TextToSqlTerminalReasonCode.RESEARCH_STAGNATED,
        TextToSqlTerminalReasonCode.RESEARCH_BUDGET_EXHAUSTED,
        TextToSqlTerminalReasonCode.RESEARCH_TOOL_FAILURE,
        TextToSqlTerminalReasonCode.RESEARCH_PROTOCOL_FAILURE,
        TextToSqlTerminalReasonCode.CANCELLED,
        TextToSqlTerminalReasonCode.TIMED_OUT,
    }:
        return text_to_sql_research_terminal_result(run_id, reason.value)
    sql = (
        state.sql_candidates[-1].sql
        if reason is TextToSqlTerminalReasonCode.DETERMINISTIC_CHECK_REJECTED
        and state.sql_candidates
        else ""
    )
    return _solver_terminal(
        run_id,
        TextToSqlTerminalStatus.ABSTAINED,
        reason,
        sql=sql,
        approved=False,
        error=None,
    )


def _solver_terminal_reason(
    state: SolverState,
) -> TextToSqlTerminalReasonCode | None:
    if state.stop_reason is not SolverStopReason.MISSING_EVIDENCE:
        return _SOLVER_STOP_REASON_CODES.get(state.stop_reason)
    if not state.missing_evidence_requests:
        return TextToSqlTerminalReasonCode.SCHEMA_CLARIFICATION_REQUIRED
    request_id = state.missing_evidence_requests[-1].missing_evidence_request_id
    record = next(
        (
            item
            for item in reversed(state.research_reentries)
            if item.missing_evidence_request_id == request_id
        ),
        None,
    )
    if record is None:
        return TextToSqlTerminalReasonCode.SCHEMA_CLARIFICATION_REQUIRED
    return {
        ResearchReentryStatus.CANCELLED: TextToSqlTerminalReasonCode.CANCELLED,
        ResearchReentryStatus.DEADLINE_EXCEEDED: TextToSqlTerminalReasonCode.TIMED_OUT,
        ResearchReentryStatus.BUDGET_EXHAUSTED: (
            TextToSqlTerminalReasonCode.RESEARCH_BUDGET_EXHAUSTED
        ),
        ResearchReentryStatus.TOOL_FAILURE: (
            TextToSqlTerminalReasonCode.RESEARCH_TOOL_FAILURE
        ),
        ResearchReentryStatus.PROTOCOL_FAILURE: (
            TextToSqlTerminalReasonCode.RESEARCH_PROTOCOL_FAILURE
        ),
    }.get(record.status, TextToSqlTerminalReasonCode.SCHEMA_CLARIFICATION_REQUIRED)


def execution_unknown_terminal_result(
    run_id: str,
    sql: str,
) -> TextToSqlTerminalResult:
    return _solver_terminal(
        run_id,
        TextToSqlTerminalStatus.FAILED,
        TextToSqlTerminalReasonCode.EXECUTION_UNKNOWN,
        sql=sql,
        approved=True,
        error="finalizer outcome is unknown",
    )


def _solver_terminal(
    run_id: str,
    status: TextToSqlTerminalStatus,
    reason: TextToSqlTerminalReasonCode,
    *,
    sql: str,
    approved: bool,
    error: str | None,
) -> TextToSqlTerminalResult:
    return TextToSqlTerminalResult(
        run_id=run_id,
        status=status,
        reason_code=reason.value,
        sql=sql,
        generated=bool(sql.strip()),
        approved=approved,
        executed=False,
        dry_run=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        error=error,
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
        ambiguity=None,
    )
