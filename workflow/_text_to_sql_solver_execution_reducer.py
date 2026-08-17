"""Pure reservation validation and finalizer-to-solver reduction."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from custom_tools.text_to_sql.adaptive.checks import adapt_final_execution_mapping
from custom_tools.text_to_sql.adaptive.models import (
    CheckKind,
    CheckStatus,
    SolverState,
    SolverStopReason,
    SqlCandidate,
)
from custom_tools.text_to_sql.adaptive.result_validation import (
    ResultContradictionReceipt,
)
from custom_tools.text_to_sql.adaptive.result_review import ResultReviewReceipt
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)
from custom_tools.text_to_sql.adaptive.solver_loop import stop_solver
from custom_tools.text_to_sql.adaptive.solver_results import (
    append_solver_execution_result,
)

from .text_to_sql_contract import (
    TextToSqlTerminalReasonCode,
    TextToSqlTerminalResult,
    TextToSqlTerminalStatus,
)


_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FINALIZER_REQUEST_REQUIRED_FIELDS = frozenset(
    {
        "operation",
        "row_limit",
        "dry_run_only",
    }
)
_FINALIZER_REQUEST_OPTIONAL_FIELDS = frozenset({"sql_query"})


class SolverExecutionRequestError(ValueError):
    """The finalizer request does not match its reserved candidate."""


@dataclass(frozen=True, slots=True)
class SolverExecutionReservationAuthority:
    run_id: str
    run_incarnation: str
    action_revision: int
    base_state_revision: int
    base_state_digest: str
    candidate_id: str
    execution_id: str
    normalized_ast_digest: str
    request_bytes: bytes
    request_digest: str
    created_at_ns: int

    def __post_init__(self) -> None:
        for value in (
            self.run_id,
            self.run_incarnation,
            self.candidate_id,
            self.execution_id,
        ):
            if type(value) is not str or _ID_RE.fullmatch(value) is None:
                raise TypeError("reservation identity is invalid")
        for value in (
            self.base_state_digest,
            self.normalized_ast_digest,
            self.request_digest,
        ):
            if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
                raise TypeError("reservation digest is invalid")
        for value in (
            self.action_revision,
            self.base_state_revision,
            self.created_at_ns,
        ):
            if type(value) is not int or value < 0:
                raise TypeError("reservation revision is invalid")
        if type(self.request_bytes) is not bytes:
            raise TypeError("reservation request_bytes must be exact bytes")
        if _sha256_digest(self.request_bytes) != self.request_digest:
            raise ValueError("reservation request digest does not match bytes")


def reserved_candidate(
    state: SolverState,
    reservation: SolverExecutionReservationAuthority,
) -> SqlCandidate:
    candidate = _reservation_candidate(state, reservation)
    _validated_request(reservation, candidate)
    return candidate


def _reservation_candidate(
    state: SolverState,
    reservation: SolverExecutionReservationAuthority,
) -> SqlCandidate:
    if type(state) is not SolverState:
        raise TypeError("state must be exact SolverState")
    if type(reservation) is not SolverExecutionReservationAuthority:
        raise TypeError("reservation must be exact authority")
    if (
        state.run_id != reservation.run_id
        or state.run_incarnation != reservation.run_incarnation
        or state.revision != reservation.base_state_revision
        or canonical_digest(state) != reservation.base_state_digest
    ):
        raise ValueError("reservation does not match SolverState")
    candidate = next(
        (
            item
            for item in state.sql_candidates
            if item.candidate_id == reservation.candidate_id
        ),
        None,
    )
    if candidate is None or (
        candidate.normalized_ast_digest != reservation.normalized_ast_digest
    ):
        raise ValueError("reserved candidate does not match SolverState")
    return candidate


def validate_finalizer_identity(
    state: SolverState,
    reservation: SolverExecutionReservationAuthority,
    terminal: TextToSqlTerminalResult,
) -> None:
    _validated_finalizer(state, reservation, terminal)


def state_after_known_finalizer(
    state: SolverState,
    reservation: SolverExecutionReservationAuthority,
    terminal: TextToSqlTerminalResult,
) -> SolverState:
    candidate, request = _validated_finalizer(state, reservation, terminal)
    execution = terminal.to_mapping()["execution"]
    if execution and terminal.reason_code != (
        TextToSqlTerminalReasonCode.EXECUTOR_CONTRACT_INVALID.value
    ):
        evidence = adapt_final_execution_mapping(
            candidate,
            execution,
            execution_id=reservation.execution_id,
            expected_row_limit=request["row_limit"],
            expected_dry_run_only=request["dry_run_only"],
        )
        if evidence.execution_result is not None:
            transition = append_solver_execution_result(
                state,
                evidence.check_result,
                evidence.execution_result,
                base_revision=state.revision,
            )
            if terminal.status is TextToSqlTerminalStatus.SUCCEEDED:
                return transition.state
            return SolverState.model_validate(
                {
                    **transition.state.model_dump(mode="python"),
                    "selected_candidate_id": None,
                    "stop_reason": SolverStopReason.TOOL_FAILURE,
                }
            )
    reason = (
        SolverStopReason.NO_SAFE_CANDIDATE
        if terminal.reason_code
        in {
            TextToSqlTerminalReasonCode.DETERMINISTIC_CHECK_REJECTED.value,
            TextToSqlTerminalReasonCode.VERIFIER_REJECTED.value,
            TextToSqlTerminalReasonCode.STALE_REQUIRED_EVIDENCE.value,
        }
        else SolverStopReason.PROTOCOL_FAILURE
    )
    return stop_solver(state, reason, base_revision=state.revision)


def state_after_result_contradiction(
    state: SolverState,
    reservation: SolverExecutionReservationAuthority,
    receipt: ResultContradictionReceipt,
) -> SolverState:
    """Record one proven contradictory execution without sealing the solver."""

    if type(receipt) not in {ResultContradictionReceipt, ResultReviewReceipt}:
        raise TypeError("receipt must be an exact result reentry receipt")
    source_id = (
        receipt.finding.expectation.source_id
        if type(receipt) is ResultContradictionReceipt
        else receipt.source_id
    )
    candidate = _reservation_candidate(state, reservation)
    request = _validated_request(reservation, candidate)
    if (
        receipt.run_id != state.run_id
        or receipt.run_incarnation != state.run_incarnation
        or receipt.candidate_id != candidate.candidate_id
        or receipt.normalized_ast_digest != candidate.normalized_ast_digest
        or receipt.research_state_revision != candidate.revision
        or source_id
        not in {item.source_id for item in state.query_spec.semantic_items}
    ):
        raise ValueError("result contradiction does not match finalizer reservation")
    evidence = adapt_final_execution_mapping(
        candidate,
        receipt.execution,
        execution_id=reservation.execution_id,
        expected_row_limit=request["row_limit"],
        expected_dry_run_only=request["dry_run_only"],
    )
    if (
        evidence.check_result.check_kind is not CheckKind.EXECUTION
        or evidence.check_result.status is not CheckStatus.PASSED
        or evidence.execution_result is None
        or not evidence.execution_result.success
    ):
        raise ValueError("result contradiction execution is not technically successful")
    transition = append_solver_execution_result(
        state,
        evidence.check_result,
        evidence.execution_result,
        base_revision=state.revision,
    )
    return SolverState.model_validate(
        {
            **transition.state.model_dump(mode="python"),
            "selected_candidate_id": None,
            "stop_reason": None,
        }
    )


def _validated_finalizer(
    state: SolverState,
    reservation: SolverExecutionReservationAuthority,
    terminal: TextToSqlTerminalResult,
) -> tuple[SqlCandidate, dict[str, object]]:
    if type(terminal) is not TextToSqlTerminalResult:
        raise TypeError("terminal must be exact TextToSqlTerminalResult")
    candidate = _reservation_candidate(state, reservation)
    request = _validated_request(reservation, candidate)
    if terminal.run_id != state.run_id or terminal.sql != candidate.sql:
        raise ValueError("finalizer terminal does not match its reservation")
    return candidate, request


def execution_unknown_terminal_result(
    run_id: str,
    sql: str,
) -> TextToSqlTerminalResult:
    """Project the terminal for a finalizer whose durable outcome is unknown."""

    return TextToSqlTerminalResult(
        run_id=run_id,
        status=TextToSqlTerminalStatus.FAILED,
        reason_code=TextToSqlTerminalReasonCode.EXECUTION_UNKNOWN.value,
        sql=sql,
        generated=bool(sql.strip()),
        approved=True,
        executed=False,
        dry_run=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        error="finalizer outcome is unknown",
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
    )


def _validated_request(
    reservation: SolverExecutionReservationAuthority,
    candidate: SqlCandidate,
) -> dict[str, object]:
    try:
        request = json.loads(reservation.request_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SolverExecutionRequestError(
            "reservation request is not valid JSON"
        ) from None
    if (
        type(request) is not dict
        or not _FINALIZER_REQUEST_REQUIRED_FIELDS.issubset(request)
        or not set(request).issubset(
            _FINALIZER_REQUEST_REQUIRED_FIELDS | _FINALIZER_REQUEST_OPTIONAL_FIELDS
        )
        or canonical_json_bytes(request) != reservation.request_bytes
        or request["operation"] != "finalize_text_to_sql_run"
        or ("sql_query" in request and request["sql_query"] != candidate.sql)
        or type(request["row_limit"]) is not int
        or request["row_limit"] <= 0
        or type(request["dry_run_only"]) is not bool
    ):
        raise SolverExecutionRequestError(
            "reservation request does not match reserved candidate"
        )
    return request


def _sha256_digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


__all__ = [
    "SolverExecutionReservationAuthority",
    "SolverExecutionRequestError",
    "execution_unknown_terminal_result",
    "reserved_candidate",
    "state_after_known_finalizer",
    "state_after_result_contradiction",
    "validate_finalizer_identity",
]
