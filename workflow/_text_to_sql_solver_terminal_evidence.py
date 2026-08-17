"""Privacy-safe durable evidence for one verified solver finalizer terminal."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import ValidationError, field_validator

from custom_tools.text_to_sql.adaptive._exact_contract import (
    exact_value,
    revalidate_exact_model,
)
from custom_tools.text_to_sql.adaptive._model_primitives import (
    Digest,
    Id,
    NonNegativeInt,
    StrictModel,
)
from custom_tools.text_to_sql.adaptive.models import SolverState
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)

from ._text_to_sql_solver_execution_reducer import (
    SolverExecutionReservationAuthority,
    state_after_known_finalizer,
)
from .text_to_sql_contract import (
    TextToSqlTerminalResult,
    TextToSqlTerminalStatus,
)


_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _require_sha256(value: str) -> str:
    if _SHA256_DIGEST.fullmatch(value) is None:
        raise ValueError("evidence digest must be exact lowercase sha256")
    return value


class SolverTerminalEvidenceQueryV1(StrictModel):
    query_id: Id
    digest: Digest

    _validate_digest = field_validator("digest")(_require_sha256)


class SolverTerminalEvidenceStateV1(StrictModel):
    revision: NonNegativeInt
    digest: Digest

    _validate_digest = field_validator("digest")(_require_sha256)


class SolverTerminalEvidenceReservationV1(StrictModel):
    action_revision: NonNegativeInt
    base_state_revision: NonNegativeInt
    base_state_digest: Digest
    candidate_id: Id
    execution_id: Id
    normalized_ast_digest: Digest
    request_digest: Digest

    _validate_digests = field_validator(
        "base_state_digest",
        "normalized_ast_digest",
        "request_digest",
    )(_require_sha256)


class SolverTerminalProjectionV1(StrictModel):
    digest: Digest
    status: TextToSqlTerminalStatus
    reason_code: str
    generated: bool
    approved: bool
    executed: bool
    dry_run: bool
    audited: bool
    result_review_digest: Digest | None
    result_review_verdict: Literal["consistent"] | None

    _validate_digest = field_validator("digest")(_require_sha256)


class SolverTerminalEvidenceV1(StrictModel):
    schema_version: Literal[1] = 1
    record_kind: Literal["text2sql_solver_terminal_evidence"] = (
        "text2sql_solver_terminal_evidence"
    )
    run_id: Id
    run_incarnation: Id
    schema_namespace_version: Digest
    query: SolverTerminalEvidenceQueryV1
    solver_state: SolverTerminalEvidenceStateV1
    reservation: SolverTerminalEvidenceReservationV1
    terminal: SolverTerminalProjectionV1


def build_verified_solver_terminal_evidence(
    before_state: SolverState,
    after_state: SolverState,
    reservation: SolverExecutionReservationAuthority,
    terminal: TextToSqlTerminalResult,
) -> SolverTerminalEvidenceV1:
    """Build evidence only after reconstructing the exact finalizer successor."""

    before = revalidate_exact_model(
        before_state,
        SolverState,
        ValueError,
        "solver evidence base state",
    )
    after = revalidate_exact_model(
        after_state,
        SolverState,
        ValueError,
        "solver evidence result state",
    )
    authority = _canonical_reservation(reservation)
    checked_terminal = _canonical_terminal(terminal)
    if (
        checked_terminal.status is TextToSqlTerminalStatus.SUCCEEDED
        and checked_terminal.executed
    ):
        from custom_tools.text_to_sql.adaptive.result_review import ResultReviewReceipt

        receipt = ResultReviewReceipt.model_validate(
            checked_terminal.to_mapping()["result_review"]
        )
        if (
            receipt.verdict != "consistent"
            or receipt.run_id != authority.run_id
            or receipt.run_incarnation != authority.run_incarnation
            or receipt.candidate_id != authority.candidate_id
            or receipt.normalized_ast_digest != authority.normalized_ast_digest
            or receipt.execution != checked_terminal.execution
        ):
            raise ValueError("successful Typed terminal lacks exact result review")
    expected_after = state_after_known_finalizer(
        before,
        authority,
        checked_terminal,
    )
    if not exact_value(after, expected_after):
        raise ValueError("solver terminal evidence result state is not exact")

    return SolverTerminalEvidenceV1(
        run_id=after.run_id,
        run_incarnation=after.run_incarnation,
        schema_namespace_version=after.schema_namespace_version,
        query=SolverTerminalEvidenceQueryV1(
            query_id=after.query_spec.query_id,
            digest=canonical_digest(after.query_spec),
        ),
        solver_state=SolverTerminalEvidenceStateV1(
            revision=after.revision,
            digest=canonical_digest(after),
        ),
        reservation=SolverTerminalEvidenceReservationV1(
            action_revision=authority.action_revision,
            base_state_revision=authority.base_state_revision,
            base_state_digest=authority.base_state_digest,
            candidate_id=authority.candidate_id,
            execution_id=authority.execution_id,
            normalized_ast_digest=authority.normalized_ast_digest,
            request_digest=authority.request_digest,
        ),
        terminal=SolverTerminalProjectionV1(
            digest=canonical_digest(checked_terminal.to_mapping()),
            status=checked_terminal.status,
            reason_code=checked_terminal.reason_code,
            generated=checked_terminal.generated,
            approved=checked_terminal.approved,
            executed=checked_terminal.executed,
            dry_run=checked_terminal.dry_run,
            audited=checked_terminal.audited,
            result_review_digest=(
                canonical_digest(checked_terminal.result_review)
                if checked_terminal.result_review
                else None
            ),
            result_review_verdict=(
                checked_terminal.result_review.get("verdict")
                if checked_terminal.result_review
                else None
            ),
        ),
    )


def encode_verified_solver_terminal_evidence(
    evidence: SolverTerminalEvidenceV1,
) -> bytes:
    """Return the one canonical JSON representation of exact v1 evidence."""

    if type(evidence) is not SolverTerminalEvidenceV1:
        raise TypeError("solver terminal evidence must be exact v1 evidence")
    checked = SolverTerminalEvidenceV1.model_validate(
        evidence.model_dump(mode="python")
    )
    if not exact_value(evidence, checked):
        raise ValueError("solver terminal evidence is not an exact canonical value")
    return canonical_json_bytes(checked.model_dump(mode="json"))


def decode_verified_solver_terminal_evidence(
    value: bytes,
) -> SolverTerminalEvidenceV1 | None:
    """Decode known v1 evidence; unrecognized envelope values are untrusted."""

    if type(value) is not bytes:
        return None
    try:
        document = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        type(document) is not dict
        or document.get("record_kind") != "text2sql_solver_terminal_evidence"
        or document.get("schema_version") != 1
    ):
        return None
    try:
        evidence = SolverTerminalEvidenceV1.model_validate_json(value)
        if encode_verified_solver_terminal_evidence(evidence) != value:
            return None
    except (TypeError, ValueError, ValidationError):
        return None
    return evidence


def validate_verified_solver_terminal_evidence(
    evidence: SolverTerminalEvidenceV1,
    before_state: SolverState,
    after_state: SolverState,
    reservation: SolverExecutionReservationAuthority,
    terminal: TextToSqlTerminalResult,
) -> bool:
    """Return whether stored evidence exactly matches trusted durable inputs."""

    if type(evidence) is not SolverTerminalEvidenceV1:
        return False
    try:
        expected = build_verified_solver_terminal_evidence(
            before_state,
            after_state,
            reservation,
            terminal,
        )
    except (TypeError, ValueError):
        return False
    return exact_value(evidence, expected)


def _canonical_reservation(
    reservation: SolverExecutionReservationAuthority,
) -> SolverExecutionReservationAuthority:
    if type(reservation) is not SolverExecutionReservationAuthority:
        raise TypeError("solver terminal evidence reservation is invalid")
    checked = SolverExecutionReservationAuthority(
        run_id=reservation.run_id,
        run_incarnation=reservation.run_incarnation,
        action_revision=reservation.action_revision,
        base_state_revision=reservation.base_state_revision,
        base_state_digest=reservation.base_state_digest,
        candidate_id=reservation.candidate_id,
        execution_id=reservation.execution_id,
        normalized_ast_digest=reservation.normalized_ast_digest,
        request_bytes=reservation.request_bytes,
        request_digest=reservation.request_digest,
        created_at_ns=reservation.created_at_ns,
    )
    if not exact_value(reservation, checked):
        raise ValueError("solver terminal evidence reservation is not exact")
    return checked


def _canonical_terminal(terminal: TextToSqlTerminalResult) -> TextToSqlTerminalResult:
    if type(terminal) is not TextToSqlTerminalResult:
        raise TypeError("solver terminal evidence terminal is invalid")
    checked = TextToSqlTerminalResult.from_mapping(terminal.to_mapping())
    if not exact_value(terminal, checked):
        raise ValueError("solver terminal evidence terminal is not exact")
    return checked


__all__ = [
    "SolverTerminalEvidenceV1",
    "SolverTerminalProjectionV1",
    "build_verified_solver_terminal_evidence",
    "decode_verified_solver_terminal_evidence",
    "encode_verified_solver_terminal_evidence",
    "validate_verified_solver_terminal_evidence",
]
