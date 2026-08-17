"""Durable SolverState snapshots with non-replayable execution reservations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any

from custom_tools.text_to_sql.adaptive.models import (
    ResearchReentryStatus,
    ResearchState,
    SolverState,
    SolverStopReason,
)
from custom_tools.text_to_sql.adaptive.replay_inputs import (
    ReplayInputError,
    SolverMissingEvidenceReplayInput,
    SolverReentryAdmissionReplayInput,
    SolverReentryCompletedReplayInput,
    SolverSqlProposalReplayInput,
    deserialize_replay_input,
    serialize_replay_input,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_json_bytes,
    deserialize_as,
    serialize_contract,
)

from .adaptive_state_store import AdaptiveStateStore
from ._text_to_sql_solver_execution_reducer import (
    SolverExecutionReservationAuthority,
)
from ._text_to_sql_solver_terminal_evidence import (
    SolverTerminalEvidenceV1,
    decode_verified_solver_terminal_evidence,
    encode_verified_solver_terminal_evidence,
    validate_verified_solver_terminal_evidence,
)
from .text_to_sql_contract import TextToSqlTerminalResult


class AdaptiveSolverCheckpointError(RuntimeError):
    """Base error for the solver checkpoint journal."""


class AdaptiveSolverCheckpointCasError(AdaptiveSolverCheckpointError):
    """The supplied state or action revision is not the current head."""


class AdaptiveSolverCheckpointConflictError(AdaptiveSolverCheckpointError):
    """An immutable journal identity already has different content."""


class AdaptiveSolverCheckpointCorruptionError(AdaptiveSolverCheckpointError):
    """Stored bytes, linkage, or cursor data is inconsistent."""


class AdaptiveSolverCheckpointPendingExecutionError(AdaptiveSolverCheckpointError):
    """A reserved execution must be reconciled before another action."""


class AdaptiveSolverCheckpointReplayError(AdaptiveSolverCheckpointError):
    """An execution identity has already been reserved."""


@dataclass(frozen=True, slots=True)
class SolverCheckpointCursor:
    run_id: str
    run_incarnation: str
    initial_state_revision: int
    state_revision: int
    state_digest: str
    next_action_revision: int
    pending_execution_action_revision: int | None


@dataclass(frozen=True, slots=True)
class SolverExecutionReservation:
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


@dataclass(frozen=True, slots=True)
class SolverTerminalRecord:
    run_id: str
    run_incarnation: str
    state_revision: int
    state_digest: str
    next_action_revision: int
    terminal_bytes: bytes
    terminal_digest: str
    created_at_ns: int


@dataclass(frozen=True, slots=True)
class SolverCheckpoint:
    state: SolverState
    cursor: SolverCheckpointCursor
    pending_execution: SolverExecutionReservation | None
    terminal: SolverTerminalRecord | None
    verified_terminal_evidence: SolverTerminalEvidenceV1 | None = None


@dataclass(frozen=True, slots=True)
class SolverReplaySnapshot:
    state: SolverState
    state_digest: str
    source_action_revision: int | None
    created_at_ns: int


@dataclass(frozen=True, slots=True)
class SolverReplayAction:
    action_revision: int
    action_kind: str
    base_state_revision: int
    base_state_digest: str
    result_state_revision: int | None
    result_state_digest: str | None
    candidate_id: str | None
    execution_id: str | None
    normalized_ast_digest: str | None
    action: Any
    action_digest: str
    created_at_ns: int


@dataclass(frozen=True, slots=True)
class SolverReplayReconciliation:
    action_revision: int
    outcome: str
    result_state_revision: int
    result_state_digest: str
    result: Any
    result_digest: str
    created_at_ns: int


@dataclass(frozen=True, slots=True)
class SolverReplayTerminal:
    state_revision: int
    state_digest: str
    next_action_revision: int
    terminal: Any
    terminal_digest: str
    created_at_ns: int


@dataclass(frozen=True, slots=True)
class SolverReplayChain:
    run_id: str
    run_incarnation: str
    initial_state_revision: int
    state_revision: int
    state_digest: str
    next_action_revision: int
    pending_execution_action_revision: int | None
    snapshots: tuple[SolverReplaySnapshot, ...]
    actions: tuple[SolverReplayAction, ...]
    reconciliations: tuple[SolverReplayReconciliation, ...]
    terminal: SolverReplayTerminal | None


_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

_SNAPSHOT_COLUMNS = (
    "run_id",
    "run_incarnation",
    "state_revision",
    "source_action_revision",
    "state_bytes",
    "state_digest",
    "created_at_ns",
)
_ACTION_COLUMNS = (
    "run_id",
    "run_incarnation",
    "action_revision",
    "action_kind",
    "base_state_revision",
    "base_state_digest",
    "result_state_revision",
    "result_state_digest",
    "candidate_id",
    "execution_id",
    "normalized_ast_digest",
    "action_bytes",
    "action_digest",
    "created_at_ns",
)
_RECONCILIATION_COLUMNS = (
    "run_id",
    "run_incarnation",
    "action_revision",
    "outcome",
    "result_state_revision",
    "result_state_digest",
    "result_bytes",
    "result_digest",
    "created_at_ns",
)
_HEAD_COLUMNS = (
    "run_id",
    "run_incarnation",
    "initial_state_revision",
    "state_revision",
    "state_digest",
    "next_action_revision",
    "pending_execution_action_revision",
    "terminal_digest",
)
_TERMINAL_COLUMNS = (
    "run_id",
    "run_incarnation",
    "state_revision",
    "state_digest",
    "next_action_revision",
    "terminal_bytes",
    "terminal_digest",
    "created_at_ns",
)
_TRANSITION_REPLAY_INPUT_TYPES = (
    SolverSqlProposalReplayInput,
    SolverMissingEvidenceReplayInput,
    SolverReentryAdmissionReplayInput,
    SolverReentryCompletedReplayInput,
)


def _storage_select(columns: tuple[str, ...]) -> str:
    storage_types = ", ".join(
        f"typeof({column}) AS storage_type__{column}" for column in columns
    )
    return f"*, {storage_types}"


_SNAPSHOT_SELECT = _storage_select(_SNAPSHOT_COLUMNS)
_ACTION_SELECT = _storage_select(_ACTION_COLUMNS)
_RECONCILIATION_SELECT = _storage_select(_RECONCILIATION_COLUMNS)
_HEAD_SELECT = _storage_select(_HEAD_COLUMNS)
_TERMINAL_SELECT = _storage_select(_TERMINAL_COLUMNS)


class AdaptiveSolverCheckpointStore:
    """Atomic journal for SolverState transitions and external executions."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        commit_fence: object | None = None,
    ) -> None:
        self._owner = AdaptiveStateStore(db_path, commit_fence=commit_fence)
        self.db_path = self._owner.db_path

    def close(self) -> None:
        self._owner.close()

    def initialize(self, state: SolverState) -> SolverCheckpoint:
        state_bytes, state_digest = _state_bytes_and_digest(state)
        with self._owner._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = self._load_checkpoint(
                connection,
                state.run_id,
                state.run_incarnation,
            )
            if checkpoint is not None:
                initial = self._snapshot_row(
                    connection,
                    state.run_id,
                    state.run_incarnation,
                    checkpoint.cursor.initial_state_revision,
                )
                if (
                    checkpoint.cursor.initial_state_revision == state.revision
                    and initial is not None
                    and initial["state_bytes"] == state_bytes
                    and initial["state_digest"] == state_digest
                ):
                    return _historical_checkpoint(
                        state,
                        state_digest,
                        initial_state_revision=state.revision,
                        next_action_revision=0,
                    )
                raise AdaptiveSolverCheckpointConflictError(
                    "solver checkpoint incarnation is already initialized differently"
                )
            self._insert_snapshot(connection, state, source_action_revision=None)
            connection.execute(
                """
                INSERT INTO adaptive_solver_checkpoint_heads (
                    run_id, run_incarnation, initial_state_revision, state_revision,
                    state_digest, next_action_revision,
                    pending_execution_action_revision, terminal_digest
                ) VALUES (?, ?, ?, ?, ?, 0, NULL, NULL)
                """,
                (
                    state.run_id,
                    state.run_incarnation,
                    state.revision,
                    state.revision,
                    state_digest,
                ),
            )
            return _historical_checkpoint(
                state,
                state_digest,
                initial_state_revision=state.revision,
                next_action_revision=0,
            )

    def load(
        self,
        run_id: str,
        run_incarnation: str,
    ) -> SolverCheckpoint | None:
        _require_identity(run_id, "run_id")
        _require_identity(run_incarnation, "run_incarnation")
        with self._owner._connection() as connection:
            return self._load_checkpoint(connection, run_id, run_incarnation)

    def load_replay_chain(
        self,
        run_id: str,
        run_incarnation: str,
    ) -> SolverReplayChain | None:
        """Export the complete immutable chain after the normal load validation."""

        _require_identity(run_id, "run_id")
        _require_identity(run_incarnation, "run_incarnation")
        identity = (run_id, run_incarnation)
        with self._owner._connection() as connection:
            connection.execute("BEGIN")
            checkpoint = self._load_checkpoint(connection, *identity)
            if checkpoint is None:
                return None
            head = connection.execute(
                f"""
                SELECT {_HEAD_SELECT} FROM adaptive_solver_checkpoint_heads
                WHERE run_id = ? AND run_incarnation = ?
                """,
                identity,
            ).fetchone()
            snapshots = tuple(
                connection.execute(
                    f"""
                    SELECT {_SNAPSHOT_SELECT}
                    FROM adaptive_solver_checkpoint_snapshots
                    WHERE run_id = ? AND run_incarnation = ?
                    ORDER BY state_revision
                    """,
                    identity,
                )
            )
            actions = tuple(
                connection.execute(
                    f"""
                    SELECT {_ACTION_SELECT}
                    FROM adaptive_solver_checkpoint_actions
                    WHERE run_id = ? AND run_incarnation = ?
                    ORDER BY action_revision
                    """,
                    identity,
                )
            )
            reconciliations = tuple(
                connection.execute(
                    f"""
                    SELECT {_RECONCILIATION_SELECT}
                    FROM adaptive_solver_checkpoint_execution_reconciliations
                    WHERE run_id = ? AND run_incarnation = ?
                    ORDER BY action_revision
                    """,
                    identity,
                )
            )
            terminal_row = self._terminal_row(connection, *identity)
            if head is None:
                raise AdaptiveSolverCheckpointCorruptionError(
                    "solver checkpoint head disappeared during replay read"
                )
            validated = _validate_loaded_checkpoint(
                head,
                snapshots,
                actions,
                reconciliations,
                terminal_row,
            )
            if validated != checkpoint:
                raise AdaptiveSolverCheckpointCorruptionError(
                    "solver checkpoint changed during replay read"
                )
            return _build_solver_replay_chain(
                head,
                snapshots,
                actions,
                reconciliations,
                validated,
            )

    def load_transition_replay_input(
        self,
        run_id: str,
        run_incarnation: str,
        action_revision: int,
    ) -> (
        SolverSqlProposalReplayInput
        | SolverMissingEvidenceReplayInput
        | SolverReentryAdmissionReplayInput
        | SolverReentryCompletedReplayInput
        | None
    ):
        """Load a transition reducer input; legacy/non-replay rows return ``None``."""

        _require_identity(run_id, "run_id")
        _require_identity(run_incarnation, "run_incarnation")
        _require_action_revision(action_revision)
        with self._owner._connection() as connection:
            row = self._transition_replay_input_row(
                connection,
                run_id,
                run_incarnation,
                action_revision,
            )
            return None if row is None else self._decode_transition_replay_input(row)

    def commit_non_execution(
        self,
        before_state: SolverState,
        after_state: SolverState,
        *,
        action_revision: int,
        action: Any,
        replay_input: (
            SolverSqlProposalReplayInput
            | SolverMissingEvidenceReplayInput
            | SolverReentryAdmissionReplayInput
            | SolverReentryCompletedReplayInput
            | None
        ) = None,
    ) -> SolverCheckpoint:
        _require_action_revision(action_revision)
        before_bytes, before_digest = _state_bytes_and_digest(before_state)
        del before_bytes
        after_bytes, after_digest = _state_bytes_and_digest(after_state)
        _require_state_successor(before_state, after_state)
        action_bytes = _canonical_input_bytes(action, label="action")
        action_digest = _digest(action_bytes)
        checked_replay, replay_bytes, replay_digest = (
            _validated_transition_replay_input(replay_input)
        )
        identity = (before_state.run_id, before_state.run_incarnation)

        with self._owner._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = self._required_checkpoint(connection, *identity)
            self._require_unsealed(checkpoint)
            existing = self._action_row(connection, *identity, action_revision)
            if existing is not None:
                if self._matching_transition(
                    existing,
                    before_state,
                    before_digest,
                    after_state,
                    after_digest,
                    action_bytes,
                    action_digest,
                ):
                    stored_replay = self._transition_replay_input_row(
                        connection,
                        *identity,
                        action_revision,
                    )
                    if (
                        (stored_replay is None) != (checked_replay is None)
                        or stored_replay is not None
                        and self._decode_transition_replay_input(stored_replay)
                        != checked_replay
                    ):
                        raise AdaptiveSolverCheckpointConflictError(
                            "conflicting duplicate solver replay input"
                        )
                    snapshot = self._snapshot_row(
                        connection,
                        *identity,
                        after_state.revision,
                    )
                    if (
                        snapshot is None
                        or snapshot["state_bytes"] != after_bytes
                        or snapshot["state_digest"] != after_digest
                    ):
                        raise AdaptiveSolverCheckpointCorruptionError(
                            "idempotent transition snapshot is missing or different"
                        )
                    return _historical_checkpoint(
                        after_state,
                        after_digest,
                        initial_state_revision=checkpoint.cursor.initial_state_revision,
                        next_action_revision=action_revision + 1,
                    )
                raise AdaptiveSolverCheckpointConflictError(
                    "conflicting duplicate solver transition"
                )
            if checked_replay is None and _transition_requires_replay(
                before_state,
                after_state,
                action_bytes,
            ):
                raise AdaptiveSolverCheckpointConflictError(
                    "new solver semantic transition requires replay input"
                )
            if checked_replay is not None:
                _validate_transition_replay(
                    connection,
                    before_state,
                    after_state,
                    action_bytes,
                    checked_replay,
                )
            self._require_open_head(
                checkpoint,
                before_state,
                before_digest,
                action_revision,
            )
            created_at_ns = _now_ns()
            connection.execute(
                """
                INSERT INTO adaptive_solver_checkpoint_actions (
                    run_id, run_incarnation, action_revision, action_kind,
                    base_state_revision, base_state_digest,
                    result_state_revision, result_state_digest,
                    candidate_id, execution_id, normalized_ast_digest,
                    action_bytes, action_digest, created_at_ns
                ) VALUES (?, ?, ?, 'transition', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    *identity,
                    action_revision,
                    before_state.revision,
                    before_digest,
                    after_state.revision,
                    after_digest,
                    action_bytes,
                    action_digest,
                    created_at_ns,
                ),
            )
            if checked_replay is not None:
                assert replay_bytes is not None
                assert replay_digest is not None
                self._insert_transition_replay_input(
                    connection,
                    *identity,
                    action_revision,
                    checked_replay,
                    replay_bytes,
                    replay_digest,
                    created_at_ns,
                )
            self._insert_snapshot(
                connection,
                after_state,
                source_action_revision=action_revision,
            )
            updated = connection.execute(
                """
                UPDATE adaptive_solver_checkpoint_heads
                SET state_revision = ?, state_digest = ?, next_action_revision = ?
                WHERE run_id = ? AND run_incarnation = ?
                  AND state_revision = ? AND state_digest = ?
                  AND next_action_revision = ?
                  AND pending_execution_action_revision IS NULL
                  AND terminal_digest IS NULL
                """,
                (
                    after_state.revision,
                    after_digest,
                    action_revision + 1,
                    *identity,
                    before_state.revision,
                    before_digest,
                    action_revision,
                ),
            )
            if updated.rowcount != 1:
                raise AdaptiveSolverCheckpointCasError(
                    "solver transition head compare-and-swap failed"
                )
            return _historical_checkpoint(
                after_state,
                after_digest,
                initial_state_revision=checkpoint.cursor.initial_state_revision,
                next_action_revision=action_revision + 1,
            )

    def reserve_execution(
        self,
        state: SolverState,
        *,
        action_revision: int,
        candidate_id: str,
        execution_id: str,
        request: Any,
    ) -> SolverExecutionReservation:
        _require_action_revision(action_revision)
        _require_identity(candidate_id, "candidate_id")
        _require_identity(execution_id, "execution_id")
        _, state_digest = _state_bytes_and_digest(state)
        candidate = next(
            (
                item
                for item in state.sql_candidates
                if item.candidate_id == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise ValueError("candidate_id is not present in SolverState")
        normalized_ast_digest = candidate.normalized_ast_digest
        _require_digest(normalized_ast_digest, "normalized_ast_digest")
        request_bytes = _canonical_input_bytes(request, label="execution request")
        request_digest = _digest(request_bytes)
        identity = (state.run_id, state.run_incarnation)

        with self._owner._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = self._required_checkpoint(connection, *identity)
            self._require_unsealed(checkpoint)
            if self._action_row(connection, *identity, action_revision) is not None:
                raise AdaptiveSolverCheckpointReplayError(
                    "execution action revision was already reserved"
                )
            duplicate = connection.execute(
                """
                SELECT 1 FROM adaptive_solver_checkpoint_actions
                WHERE run_id = ? AND run_incarnation = ? AND action_kind = 'execution'
                  AND (candidate_id = ? OR execution_id = ? OR normalized_ast_digest = ?)
                LIMIT 1
                """,
                (*identity, candidate_id, execution_id, normalized_ast_digest),
            ).fetchone()
            if duplicate is not None:
                raise AdaptiveSolverCheckpointReplayError(
                    "execution identity was already reserved"
                )
            self._require_open_head(
                checkpoint,
                state,
                state_digest,
                action_revision,
            )
            action_value = {
                "candidate_id": candidate_id,
                "execution_id": execution_id,
                "normalized_ast_digest": normalized_ast_digest,
                "request": json.loads(request_bytes),
            }
            action_bytes = canonical_json_bytes(action_value)
            action_digest = _digest(action_bytes)
            created_at_ns = _now_ns()
            try:
                connection.execute(
                    """
                    INSERT INTO adaptive_solver_checkpoint_actions (
                        run_id, run_incarnation, action_revision, action_kind,
                        base_state_revision, base_state_digest,
                        result_state_revision, result_state_digest,
                        candidate_id, execution_id, normalized_ast_digest,
                        action_bytes, action_digest, created_at_ns
                    ) VALUES (?, ?, ?, 'execution', ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *identity,
                        action_revision,
                        state.revision,
                        state_digest,
                        candidate_id,
                        execution_id,
                        normalized_ast_digest,
                        action_bytes,
                        action_digest,
                        created_at_ns,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AdaptiveSolverCheckpointReplayError(
                    "execution identity was already reserved"
                ) from exc
            updated = connection.execute(
                """
                UPDATE adaptive_solver_checkpoint_heads
                SET next_action_revision = ?, pending_execution_action_revision = ?
                WHERE run_id = ? AND run_incarnation = ?
                  AND state_revision = ? AND state_digest = ?
                  AND next_action_revision = ?
                  AND pending_execution_action_revision IS NULL
                  AND terminal_digest IS NULL
                """,
                (
                    action_revision + 1,
                    action_revision,
                    *identity,
                    state.revision,
                    state_digest,
                    action_revision,
                ),
            )
            if updated.rowcount != 1:
                raise AdaptiveSolverCheckpointCasError(
                    "execution reservation head compare-and-swap failed"
                )
            return SolverExecutionReservation(
                run_id=state.run_id,
                run_incarnation=state.run_incarnation,
                action_revision=action_revision,
                base_state_revision=state.revision,
                base_state_digest=state_digest,
                candidate_id=candidate_id,
                execution_id=execution_id,
                normalized_ast_digest=normalized_ast_digest,
                request_bytes=request_bytes,
                request_digest=request_digest,
                created_at_ns=created_at_ns,
            )

    def reconcile_execution(
        self,
        reservation: SolverExecutionReservation,
        after_state: SolverState,
        *,
        result: Any,
    ) -> SolverCheckpoint:
        result_bytes = _canonical_input_bytes(result, label="execution result")
        return self._reconcile_execution(
            reservation,
            after_state,
            outcome="KNOWN",
            result_bytes=result_bytes,
        )

    def reconcile_unknown_execution(
        self,
        reservation: SolverExecutionReservation,
        failure_state: SolverState,
    ) -> SolverCheckpoint:
        if failure_state.stop_reason is not SolverStopReason.TOOL_FAILURE:
            raise ValueError("unknown execution requires TOOL_FAILURE SolverState")
        return self._reconcile_execution(
            reservation,
            failure_state,
            outcome="UNKNOWN",
            result_bytes=b"null",
        )

    def reconcile_execution_terminal(
        self,
        reservation: SolverExecutionReservation,
        after_state: SolverState,
        *,
        outcome: str,
        terminal_bytes: bytes,
        verified_terminal_evidence: SolverTerminalEvidenceV1 | None = None,
    ) -> SolverCheckpoint:
        if outcome not in {"KNOWN", "UNKNOWN"}:
            raise ValueError("outcome must be exactly KNOWN or UNKNOWN")
        if (
            outcome == "UNKNOWN"
            and after_state.stop_reason is not SolverStopReason.TOOL_FAILURE
        ):
            raise ValueError("unknown execution requires TOOL_FAILURE SolverState")
        terminal_bytes = _require_canonical_bytes(
            terminal_bytes,
            label="terminal_bytes",
        )
        if outcome == "KNOWN":
            if type(verified_terminal_evidence) is not SolverTerminalEvidenceV1:
                raise TypeError(
                    "known execution requires exact verified terminal evidence"
                )
            result_bytes = encode_verified_solver_terminal_evidence(
                verified_terminal_evidence
            )
        else:
            if verified_terminal_evidence is not None:
                raise ValueError("unknown execution cannot carry terminal evidence")
            result_bytes = b"null"
        _validate_reconciliation_inputs(reservation, after_state)
        after_bytes, after_digest = _state_bytes_and_digest(after_state)
        result_digest = _digest(result_bytes)
        terminal_digest = _digest(terminal_bytes)
        identity = (reservation.run_id, reservation.run_incarnation)

        with self._owner._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = self._required_checkpoint(connection, *identity)
            action = self._action_row(
                connection,
                *identity,
                reservation.action_revision,
            )
            if action is None or not _action_matches_reservation(action, reservation):
                raise AdaptiveSolverCheckpointConflictError(
                    "execution reservation does not match the journal"
                )
            base_snapshot = self._snapshot_row(
                connection,
                *identity,
                reservation.base_state_revision,
            )
            if base_snapshot is None:
                raise AdaptiveSolverCheckpointCorruptionError(
                    "execution reservation base snapshot is missing"
                )
            base_state, _ = _decode_state_row(base_snapshot)
            _validate_unknown_result_state(outcome, base_state, after_state)
            if outcome == "KNOWN":
                terminal = _terminal_result_from_bytes(terminal_bytes)
                if not validate_verified_solver_terminal_evidence(
                    verified_terminal_evidence,
                    base_state,
                    after_state,
                    _reservation_authority(reservation),
                    terminal,
                ):
                    raise ValueError(
                        "verified terminal evidence does not match reconciliation"
                    )

            existing_reconciliation = self._reconciliation_row(
                connection,
                *identity,
                reservation.action_revision,
            )
            existing_terminal = self._terminal_row(connection, *identity)
            if existing_reconciliation is not None or existing_terminal is not None:
                if (
                    existing_reconciliation is None
                    or existing_terminal is None
                    or not _reconciliation_matches(
                        existing_reconciliation,
                        after_state,
                        after_digest,
                        outcome,
                        result_bytes,
                        result_digest,
                    )
                    or existing_terminal["state_revision"] != after_state.revision
                    or existing_terminal["state_digest"] != after_digest
                    or existing_terminal["next_action_revision"]
                    != reservation.action_revision + 1
                    or existing_terminal["terminal_bytes"] != terminal_bytes
                    or existing_terminal["terminal_digest"] != terminal_digest
                ):
                    raise AdaptiveSolverCheckpointConflictError(
                        "conflicting execution terminal reconciliation"
                    )
                return self._required_checkpoint(connection, *identity)

            self._require_unsealed(checkpoint)
            self._require_pending_reconciliation(checkpoint, reservation)
            created_at_ns = _now_ns()
            connection.execute(
                """
                INSERT INTO adaptive_solver_checkpoint_execution_reconciliations (
                    run_id, run_incarnation, action_revision, outcome,
                    result_state_revision, result_state_digest,
                    result_bytes, result_digest, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *identity,
                    reservation.action_revision,
                    outcome,
                    after_state.revision,
                    after_digest,
                    result_bytes,
                    result_digest,
                    created_at_ns,
                ),
            )
            self._insert_snapshot(
                connection,
                after_state,
                source_action_revision=reservation.action_revision,
            )
            updated = connection.execute(
                """
                UPDATE adaptive_solver_checkpoint_heads
                SET state_revision = ?, state_digest = ?,
                    pending_execution_action_revision = NULL
                WHERE run_id = ? AND run_incarnation = ?
                  AND state_revision = ? AND state_digest = ?
                  AND next_action_revision = ?
                  AND pending_execution_action_revision = ?
                  AND terminal_digest IS NULL
                """,
                (
                    after_state.revision,
                    after_digest,
                    *identity,
                    reservation.base_state_revision,
                    reservation.base_state_digest,
                    reservation.action_revision + 1,
                    reservation.action_revision,
                ),
            )
            if updated.rowcount != 1:
                raise AdaptiveSolverCheckpointCasError(
                    "execution reconciliation head compare-and-swap failed"
                )
            connection.execute(
                """
                INSERT INTO adaptive_solver_checkpoint_terminals (
                    run_id, run_incarnation, state_revision, state_digest,
                    next_action_revision, terminal_bytes, terminal_digest,
                    created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *identity,
                    after_state.revision,
                    after_digest,
                    reservation.action_revision + 1,
                    terminal_bytes,
                    terminal_digest,
                    created_at_ns,
                ),
            )
            self._seal_terminal_head(
                connection,
                after_state,
                after_digest,
                reservation.action_revision + 1,
                terminal_digest,
            )
            return self._required_checkpoint(connection, *identity)

    def record_terminal(
        self,
        state: SolverState,
        *,
        expected_action_revision: int,
        terminal_bytes: bytes,
    ) -> SolverTerminalRecord:
        _require_action_revision(expected_action_revision)
        terminal_bytes = _require_canonical_bytes(
            terminal_bytes,
            label="terminal_bytes",
        )
        terminal_digest = _digest(terminal_bytes)
        _, state_digest = _state_bytes_and_digest(state)
        identity = (state.run_id, state.run_incarnation)
        with self._owner._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = self._required_checkpoint(connection, *identity)
            existing = self._terminal_row(connection, *identity)
            if existing is not None:
                if (
                    existing["state_revision"] == state.revision
                    and existing["state_digest"] == state_digest
                    and existing["next_action_revision"] == expected_action_revision
                    and existing["terminal_bytes"] == terminal_bytes
                    and existing["terminal_digest"] == terminal_digest
                ):
                    return _terminal_from_row(existing)
                raise AdaptiveSolverCheckpointConflictError(
                    "conflicting duplicate terminal record"
                )
            reconciliation = (
                self._reconciliation_row(
                    connection,
                    *identity,
                    expected_action_revision - 1,
                )
                if expected_action_revision
                else None
            )
            if (
                reconciliation is not None
                and reconciliation["outcome"] == "KNOWN"
                and not (
                    reconciliation["result_state_revision"] == state.revision
                    and reconciliation["result_state_digest"] == state_digest
                    and _known_reconciliation_links_terminal(
                        connection,
                        reconciliation,
                        state,
                        terminal_bytes,
                    )
                )
            ):
                raise AdaptiveSolverCheckpointConflictError(
                    "terminal bytes differ from known execution reconciliation"
                )
            self._require_open_head(
                checkpoint,
                state,
                state_digest,
                expected_action_revision,
            )
            created_at_ns = _now_ns()
            connection.execute(
                """
                INSERT INTO adaptive_solver_checkpoint_terminals (
                    run_id, run_incarnation, state_revision, state_digest,
                    next_action_revision, terminal_bytes, terminal_digest,
                    created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *identity,
                    state.revision,
                    state_digest,
                    expected_action_revision,
                    terminal_bytes,
                    terminal_digest,
                    created_at_ns,
                ),
            )
            self._seal_terminal_head(
                connection,
                state,
                state_digest,
                expected_action_revision,
                terminal_digest,
            )
            return SolverTerminalRecord(
                run_id=state.run_id,
                run_incarnation=state.run_incarnation,
                state_revision=state.revision,
                state_digest=state_digest,
                next_action_revision=expected_action_revision,
                terminal_bytes=terminal_bytes,
                terminal_digest=terminal_digest,
                created_at_ns=created_at_ns,
            )

    @staticmethod
    def _seal_terminal_head(
        connection: sqlite3.Connection,
        state: SolverState,
        state_digest: str,
        expected_action_revision: int,
        terminal_digest: str,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE adaptive_solver_checkpoint_heads
            SET terminal_digest = ?
            WHERE run_id = ? AND run_incarnation = ?
              AND state_revision = ? AND state_digest = ?
              AND next_action_revision = ?
              AND pending_execution_action_revision IS NULL
              AND terminal_digest IS NULL
            """,
            (
                terminal_digest,
                state.run_id,
                state.run_incarnation,
                state.revision,
                state_digest,
                expected_action_revision,
            ),
        )
        if updated.rowcount != 1:
            raise AdaptiveSolverCheckpointCasError(
                "terminal head compare-and-swap failed"
            )

    def _reconcile_execution(
        self,
        reservation: SolverExecutionReservation,
        after_state: SolverState,
        *,
        outcome: str,
        result_bytes: bytes,
    ) -> SolverCheckpoint:
        _validate_reconciliation_inputs(reservation, after_state)
        after_bytes, after_digest = _state_bytes_and_digest(after_state)
        result_digest = _digest(result_bytes)
        identity = (reservation.run_id, reservation.run_incarnation)
        with self._owner._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = self._required_checkpoint(connection, *identity)
            self._require_unsealed(checkpoint)
            action = self._action_row(
                connection,
                *identity,
                reservation.action_revision,
            )
            if action is None or not _action_matches_reservation(action, reservation):
                raise AdaptiveSolverCheckpointConflictError(
                    "execution reservation does not match the journal"
                )
            base_snapshot = self._snapshot_row(
                connection,
                *identity,
                reservation.base_state_revision,
            )
            if base_snapshot is None:
                raise AdaptiveSolverCheckpointCorruptionError(
                    "execution reservation base snapshot is missing"
                )
            base_state, _ = _decode_state_row(base_snapshot)
            _validate_unknown_result_state(outcome, base_state, after_state)
            existing = self._reconciliation_row(
                connection,
                *identity,
                reservation.action_revision,
            )
            replayed = self._existing_reconciliation_checkpoint(
                connection,
                existing,
                reservation,
                after_state,
                after_bytes,
                after_digest,
                outcome,
                result_bytes,
                result_digest,
                checkpoint.cursor.initial_state_revision,
            )
            if replayed is not None:
                return replayed
            self._require_pending_reconciliation(checkpoint, reservation)
            created_at_ns = _now_ns()
            connection.execute(
                """
                INSERT INTO adaptive_solver_checkpoint_execution_reconciliations (
                    run_id, run_incarnation, action_revision, outcome,
                    result_state_revision, result_state_digest,
                    result_bytes, result_digest, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *identity,
                    reservation.action_revision,
                    outcome,
                    after_state.revision,
                    after_digest,
                    result_bytes,
                    result_digest,
                    created_at_ns,
                ),
            )
            self._insert_snapshot(
                connection,
                after_state,
                source_action_revision=reservation.action_revision,
            )
            updated = connection.execute(
                """
                UPDATE adaptive_solver_checkpoint_heads
                SET state_revision = ?, state_digest = ?,
                    pending_execution_action_revision = NULL
                WHERE run_id = ? AND run_incarnation = ?
                  AND state_revision = ? AND state_digest = ?
                  AND next_action_revision = ?
                  AND pending_execution_action_revision = ?
                  AND terminal_digest IS NULL
                """,
                (
                    after_state.revision,
                    after_digest,
                    *identity,
                    reservation.base_state_revision,
                    reservation.base_state_digest,
                    reservation.action_revision + 1,
                    reservation.action_revision,
                ),
            )
            if updated.rowcount != 1:
                raise AdaptiveSolverCheckpointCasError(
                    "execution reconciliation head compare-and-swap failed"
                )
            return _historical_checkpoint(
                after_state,
                after_digest,
                initial_state_revision=checkpoint.cursor.initial_state_revision,
                next_action_revision=reservation.action_revision + 1,
            )

    def _existing_reconciliation_checkpoint(
        self,
        connection: sqlite3.Connection,
        existing: sqlite3.Row | None,
        reservation: SolverExecutionReservation,
        after_state: SolverState,
        after_bytes: bytes,
        after_digest: str,
        outcome: str,
        result_bytes: bytes,
        result_digest: str,
        initial_state_revision: int,
    ) -> SolverCheckpoint | None:
        if existing is None:
            return None
        if not _reconciliation_matches(
            existing,
            after_state,
            after_digest,
            outcome,
            result_bytes,
            result_digest,
        ):
            raise AdaptiveSolverCheckpointConflictError(
                "conflicting execution reconciliation"
            )
        snapshot = self._snapshot_row(
            connection,
            reservation.run_id,
            reservation.run_incarnation,
            after_state.revision,
        )
        if (
            snapshot is None
            or snapshot["state_bytes"] != after_bytes
            or snapshot["state_digest"] != after_digest
        ):
            raise AdaptiveSolverCheckpointCorruptionError(
                "idempotent reconciliation snapshot is missing or different"
            )
        return _historical_checkpoint(
            after_state,
            after_digest,
            initial_state_revision=initial_state_revision,
            next_action_revision=reservation.action_revision + 1,
        )

    @staticmethod
    def _require_pending_reconciliation(
        checkpoint: SolverCheckpoint,
        reservation: SolverExecutionReservation,
    ) -> None:
        AdaptiveSolverCheckpointStore._require_unsealed(checkpoint)
        if (
            checkpoint.cursor.pending_execution_action_revision
            != reservation.action_revision
            or checkpoint.cursor.next_action_revision != reservation.action_revision + 1
            or checkpoint.cursor.state_revision != reservation.base_state_revision
            or checkpoint.cursor.state_digest != reservation.base_state_digest
        ):
            raise AdaptiveSolverCheckpointCasError(
                "execution reconciliation head compare-and-swap failed"
            )

    def _load_checkpoint(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
    ) -> SolverCheckpoint | None:
        try:
            return self._load_decoded_checkpoint(
                connection,
                run_id,
                run_incarnation,
            )
        except sqlite3.OperationalError as exc:
            if not str(exc).startswith("Could not decode to UTF-8 column "):
                raise
            raise AdaptiveSolverCheckpointCorruptionError(
                "stored solver checkpoint text is not valid UTF-8"
            ) from exc

    def _load_decoded_checkpoint(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
    ) -> SolverCheckpoint | None:
        identity = (run_id, run_incarnation)
        head = connection.execute(
            f"""
            SELECT {_HEAD_SELECT} FROM adaptive_solver_checkpoint_heads
            WHERE run_id = ? AND run_incarnation = ?
            """,
            identity,
        ).fetchone()
        if head is None:
            if self._has_orphan_rows(connection, *identity):
                raise AdaptiveSolverCheckpointCorruptionError(
                    "solver checkpoint rows exist without a head"
                )
            return None
        snapshots = tuple(
            connection.execute(
                f"""
                SELECT {_SNAPSHOT_SELECT} FROM adaptive_solver_checkpoint_snapshots
                WHERE run_id = ? AND run_incarnation = ?
                ORDER BY state_revision
                """,
                identity,
            )
        )
        actions = tuple(
            connection.execute(
                f"""
                SELECT {_ACTION_SELECT} FROM adaptive_solver_checkpoint_actions
                WHERE run_id = ? AND run_incarnation = ?
                ORDER BY action_revision
                """,
                identity,
            )
        )
        reconciliations = tuple(
            connection.execute(
                f"""
                SELECT {_RECONCILIATION_SELECT}
                FROM adaptive_solver_checkpoint_execution_reconciliations
                WHERE run_id = ? AND run_incarnation = ?
                ORDER BY action_revision
                """,
                identity,
            )
        )
        terminal_row = self._terminal_row(connection, *identity)
        checkpoint = _validate_loaded_checkpoint(
            head,
            snapshots,
            actions,
            reconciliations,
            terminal_row,
        )
        self._require_transition_replay_inputs(
            connection,
            run_id,
            run_incarnation,
            snapshots,
            actions,
        )
        return checkpoint

    def _require_transition_replay_inputs(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        snapshots: tuple[sqlite3.Row, ...],
        actions: tuple[sqlite3.Row, ...],
    ) -> None:
        states = {
            _stored_row_nonnegative_int(row, "state_revision"): _decode_state_row(row)[
                0
            ]
            for row in snapshots
        }
        for action_row in actions:
            if _stored_row_text(action_row, "action_kind") != "transition":
                continue
            action_bytes = _stored_row_canonical_bytes(action_row, "action_bytes")
            before_state = states[
                _stored_row_nonnegative_int(action_row, "base_state_revision")
            ]
            after_state = states[
                _stored_row_nonnegative_int(action_row, "result_state_revision")
            ]
            if not _transition_requires_replay(
                before_state,
                after_state,
                action_bytes,
            ):
                continue
            action_revision = _stored_row_nonnegative_int(
                action_row,
                "action_revision",
            )
            replay_row = self._transition_replay_input_row(
                connection,
                run_id,
                run_incarnation,
                action_revision,
            )
            if replay_row is None:
                raise AdaptiveSolverCheckpointCorruptionError(
                    "solver transition requires replay input"
                )
            self._decode_transition_replay_input(replay_row)

    def _required_checkpoint(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
    ) -> SolverCheckpoint:
        checkpoint = self._load_checkpoint(connection, run_id, run_incarnation)
        if checkpoint is None:
            raise AdaptiveSolverCheckpointCasError(
                "solver checkpoint incarnation is not initialized"
            )
        return checkpoint

    @staticmethod
    def _require_open_head(
        checkpoint: SolverCheckpoint,
        state: SolverState,
        state_digest: str,
        action_revision: int,
    ) -> None:
        AdaptiveSolverCheckpointStore._require_unsealed(checkpoint)
        if checkpoint.pending_execution is not None:
            raise AdaptiveSolverCheckpointPendingExecutionError(
                "pending execution must be reconciled before another action"
            )
        if (
            checkpoint.state != state
            or checkpoint.cursor.state_revision != state.revision
            or checkpoint.cursor.state_digest != state_digest
            or checkpoint.cursor.next_action_revision != action_revision
        ):
            raise AdaptiveSolverCheckpointCasError(
                "solver checkpoint head compare-and-swap failed"
            )

    @staticmethod
    def _require_unsealed(checkpoint: SolverCheckpoint) -> None:
        if checkpoint.terminal is not None:
            raise AdaptiveSolverCheckpointCasError(
                "terminal record closes this solver incarnation"
            )

    @staticmethod
    def _matching_transition(
        row: sqlite3.Row,
        before_state: SolverState,
        before_digest: str,
        after_state: SolverState,
        after_digest: str,
        action_bytes: bytes,
        action_digest: str,
    ) -> bool:
        return (
            row["action_kind"] == "transition"
            and row["base_state_revision"] == before_state.revision
            and row["base_state_digest"] == before_digest
            and row["result_state_revision"] == after_state.revision
            and row["result_state_digest"] == after_digest
            and row["action_bytes"] == action_bytes
            and row["action_digest"] == action_digest
        )

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection,
        state: SolverState,
        *,
        source_action_revision: int | None,
    ) -> None:
        state_bytes, state_digest = _state_bytes_and_digest(state)
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_snapshots (
                run_id, run_incarnation, state_revision, source_action_revision,
                state_bytes, state_digest, created_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.run_id,
                state.run_incarnation,
                state.revision,
                source_action_revision,
                state_bytes,
                state_digest,
                _now_ns(),
            ),
        )

    @staticmethod
    def _snapshot_row(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        state_revision: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT {_SNAPSHOT_SELECT} FROM adaptive_solver_checkpoint_snapshots
            WHERE run_id = ? AND run_incarnation = ? AND state_revision = ?
            """,
            (run_id, run_incarnation, state_revision),
        ).fetchone()

    @staticmethod
    def _action_row(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        action_revision: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT {_ACTION_SELECT} FROM adaptive_solver_checkpoint_actions
            WHERE run_id = ? AND run_incarnation = ? AND action_revision = ?
            """,
            (run_id, run_incarnation, action_revision),
        ).fetchone()

    @staticmethod
    def _transition_replay_input_row(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        action_revision: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT run_id, run_incarnation, action_revision, input_kind,
                   input_version, input_bytes, input_digest, created_at_ns
            FROM adaptive_solver_checkpoint_replay_inputs
            WHERE run_id = ? AND run_incarnation = ? AND action_revision = ?
            """,
            (run_id, run_incarnation, action_revision),
        ).fetchone()

    @staticmethod
    def _insert_transition_replay_input(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        action_revision: int,
        replay_input: (
            SolverSqlProposalReplayInput
            | SolverMissingEvidenceReplayInput
            | SolverReentryAdmissionReplayInput
            | SolverReentryCompletedReplayInput
        ),
        input_bytes: bytes,
        input_digest: str,
        created_at_ns: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_replay_inputs (
                run_id, run_incarnation, action_revision, input_kind,
                input_version, input_bytes, input_digest, created_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                run_incarnation,
                action_revision,
                replay_input.input_kind,
                replay_input.input_version,
                input_bytes,
                input_digest,
                created_at_ns,
            ),
        )

    @staticmethod
    def _decode_transition_replay_input(
        row: sqlite3.Row,
    ) -> (
        SolverSqlProposalReplayInput
        | SolverMissingEvidenceReplayInput
        | SolverReentryAdmissionReplayInput
        | SolverReentryCompletedReplayInput
    ):
        input_bytes = row["input_bytes"]
        if (
            not isinstance(input_bytes, bytes)
            or not isinstance(row["input_digest"], str)
            or type(row["action_revision"]) is not int
            or type(row["input_version"]) is not int
            or type(row["created_at_ns"]) is not int
            or row["input_digest"] != _digest(input_bytes)
        ):
            raise AdaptiveSolverCheckpointCorruptionError(
                "solver replay input storage types or digest are invalid"
            )
        try:
            decoded = deserialize_replay_input(input_bytes)
        except ReplayInputError as exc:
            raise AdaptiveSolverCheckpointCorruptionError(
                "solver replay input payload is invalid"
            ) from exc
        if (
            type(decoded) not in _TRANSITION_REPLAY_INPUT_TYPES
            or row["input_kind"] != decoded.input_kind
            or row["input_version"] != decoded.input_version
        ):
            raise AdaptiveSolverCheckpointCorruptionError(
                "solver replay input kind or version is invalid"
            )
        return decoded

    @staticmethod
    def _reconciliation_row(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        action_revision: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT {_RECONCILIATION_SELECT}
            FROM adaptive_solver_checkpoint_execution_reconciliations
            WHERE run_id = ? AND run_incarnation = ? AND action_revision = ?
            """,
            (run_id, run_incarnation, action_revision),
        ).fetchone()

    @staticmethod
    def _terminal_row(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT {_TERMINAL_SELECT} FROM adaptive_solver_checkpoint_terminals
            WHERE run_id = ? AND run_incarnation = ?
            """,
            (run_id, run_incarnation),
        ).fetchone()

    @staticmethod
    def _has_orphan_rows(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
    ) -> bool:
        for table in (
            "adaptive_solver_checkpoint_snapshots",
            "adaptive_solver_checkpoint_actions",
            "adaptive_solver_checkpoint_execution_reconciliations",
            "adaptive_solver_checkpoint_terminals",
        ):
            if (
                connection.execute(
                    f"SELECT 1 FROM {table} WHERE run_id = ? AND run_incarnation = ? LIMIT 1",
                    (run_id, run_incarnation),
                ).fetchone()
                is not None
            ):
                return True
        return False


@dataclass(frozen=True, slots=True)
class _LoadedHead:
    run_id: str
    run_incarnation: str
    initial_revision: int
    state_revision: int
    state_digest: str
    next_action_revision: int
    pending_revision: int | None
    terminal_digest: str | None


@dataclass(slots=True)
class _LoadedChain:
    current_revision: int
    current_digest: str
    used_snapshots: set[int]
    used_reconciliations: set[int]
    candidate_ids: set[str]
    execution_ids: set[str]
    normalized_ast_digests: set[str]
    pending_reservation: SolverExecutionReservation | None = None


def _validate_loaded_checkpoint(
    head: sqlite3.Row,
    snapshots: tuple[sqlite3.Row, ...],
    actions: tuple[sqlite3.Row, ...],
    reconciliations: tuple[sqlite3.Row, ...],
    terminal_row: sqlite3.Row | None,
) -> SolverCheckpoint:
    try:
        return _build_loaded_checkpoint(
            head,
            snapshots,
            actions,
            reconciliations,
            terminal_row,
        )
    except AdaptiveSolverCheckpointCorruptionError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise AdaptiveSolverCheckpointCorruptionError(
            "stored solver checkpoint chain is invalid"
        ) from exc


def _build_solver_replay_chain(
    head_row: sqlite3.Row,
    snapshot_rows: tuple[sqlite3.Row, ...],
    action_rows: tuple[sqlite3.Row, ...],
    reconciliation_rows: tuple[sqlite3.Row, ...],
    checkpoint: SolverCheckpoint,
) -> SolverReplayChain:
    head = _validated_head_row(head_row)
    snapshots = tuple(_solver_replay_snapshot(row) for row in snapshot_rows)
    actions = tuple(_solver_replay_action(row) for row in action_rows)
    reconciliations = tuple(
        _solver_replay_reconciliation(row, checkpoint.terminal)
        for row in reconciliation_rows
    )
    terminal = (
        None
        if checkpoint.terminal is None
        else SolverReplayTerminal(
            state_revision=checkpoint.terminal.state_revision,
            state_digest=checkpoint.terminal.state_digest,
            next_action_revision=checkpoint.terminal.next_action_revision,
            terminal=_frozen_json(json.loads(checkpoint.terminal.terminal_bytes)),
            terminal_digest=checkpoint.terminal.terminal_digest,
            created_at_ns=checkpoint.terminal.created_at_ns,
        )
    )
    return SolverReplayChain(
        run_id=head.run_id,
        run_incarnation=head.run_incarnation,
        initial_state_revision=head.initial_revision,
        state_revision=head.state_revision,
        state_digest=head.state_digest,
        next_action_revision=head.next_action_revision,
        pending_execution_action_revision=head.pending_revision,
        snapshots=snapshots,
        actions=actions,
        reconciliations=reconciliations,
        terminal=terminal,
    )


def _solver_replay_snapshot(row: sqlite3.Row) -> SolverReplaySnapshot:
    state, digest = _decode_state_row(row)
    return SolverReplaySnapshot(
        state=state,
        state_digest=digest,
        source_action_revision=_stored_row_optional_nonnegative_int(
            row,
            "source_action_revision",
        ),
        created_at_ns=_stored_row_nonnegative_int(row, "created_at_ns"),
    )


def _solver_replay_action(row: sqlite3.Row) -> SolverReplayAction:
    action_bytes = _stored_row_canonical_bytes(row, "action_bytes")
    return SolverReplayAction(
        action_revision=_stored_row_nonnegative_int(row, "action_revision"),
        action_kind=_stored_row_text(row, "action_kind"),
        base_state_revision=_stored_row_nonnegative_int(row, "base_state_revision"),
        base_state_digest=_stored_row_digest(row, "base_state_digest"),
        result_state_revision=row["result_state_revision"],
        result_state_digest=row["result_state_digest"],
        candidate_id=row["candidate_id"],
        execution_id=row["execution_id"],
        normalized_ast_digest=row["normalized_ast_digest"],
        action=_frozen_json(json.loads(action_bytes)),
        action_digest=_stored_row_digest(row, "action_digest"),
        created_at_ns=_stored_row_nonnegative_int(row, "created_at_ns"),
    )


def _solver_replay_reconciliation(
    row: sqlite3.Row,
    terminal: SolverTerminalRecord | None,
) -> SolverReplayReconciliation:
    result_bytes = _stored_row_canonical_bytes(row, "result_bytes")
    result_digest = _stored_row_digest(row, "result_digest")
    if (
        terminal is not None
        and row["outcome"] == "KNOWN"
        and row["action_revision"] == terminal.next_action_revision - 1
        and _is_solver_terminal_evidence_envelope(result_bytes)
    ):
        result_bytes = terminal.terminal_bytes
        result_digest = terminal.terminal_digest
    return SolverReplayReconciliation(
        action_revision=_stored_row_nonnegative_int(row, "action_revision"),
        outcome=_stored_row_text(row, "outcome"),
        result_state_revision=_stored_row_nonnegative_int(
            row,
            "result_state_revision",
        ),
        result_state_digest=_stored_row_digest(row, "result_state_digest"),
        result=_frozen_json(json.loads(result_bytes)),
        result_digest=result_digest,
        created_at_ns=_stored_row_nonnegative_int(row, "created_at_ns"),
    )


def _frozen_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _frozen_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_frozen_json(item) for item in value)
    return value


def _build_loaded_checkpoint(
    head_row: sqlite3.Row,
    snapshot_rows: tuple[sqlite3.Row, ...],
    action_rows: tuple[sqlite3.Row, ...],
    reconciliation_rows: tuple[sqlite3.Row, ...],
    terminal_row: sqlite3.Row | None,
) -> SolverCheckpoint:
    head = _validated_head_row(head_row)
    states = _validated_snapshot_rows(snapshot_rows, head)
    reconciliations = _validated_reconciliation_rows(
        reconciliation_rows,
        head,
    )
    chain = _replay_action_rows(action_rows, reconciliations, states, head)
    terminal = _validated_terminal_row(
        terminal_row,
        head,
        chain.pending_reservation,
    )
    _validate_terminal_reconciliation_link(
        terminal,
        action_rows,
        reconciliations,
    )
    verified_terminal_evidence = _verified_terminal_evidence(
        terminal,
        action_rows,
        reconciliations,
        states,
    )
    return SolverCheckpoint(
        state=states[chain.current_revision][0],
        cursor=SolverCheckpointCursor(
            run_id=head.run_id,
            run_incarnation=head.run_incarnation,
            initial_state_revision=head.initial_revision,
            state_revision=chain.current_revision,
            state_digest=chain.current_digest,
            next_action_revision=head.next_action_revision,
            pending_execution_action_revision=head.pending_revision,
        ),
        pending_execution=chain.pending_reservation,
        terminal=terminal,
        verified_terminal_evidence=verified_terminal_evidence,
    )


def _validated_head_row(row: sqlite3.Row) -> _LoadedHead:
    run_id = _stored_row_identity(row, "run_id")
    run_incarnation = _stored_row_identity(row, "run_incarnation")
    initial_revision = _stored_row_nonnegative_int(row, "initial_state_revision")
    state_revision = _stored_row_nonnegative_int(row, "state_revision")
    state_digest = _stored_row_digest(row, "state_digest")
    next_action_revision = _stored_row_nonnegative_int(
        row,
        "next_action_revision",
    )
    pending_revision = _stored_row_optional_nonnegative_int(
        row,
        "pending_execution_action_revision",
    )
    terminal_digest = _stored_row_optional_digest(row, "terminal_digest")
    if state_revision < initial_revision:
        raise ValueError("head state revision precedes its initial revision")
    if pending_revision is not None and pending_revision != next_action_revision - 1:
        raise ValueError("head pending revision does not match its action cursor")
    return _LoadedHead(
        run_id=run_id,
        run_incarnation=run_incarnation,
        initial_revision=initial_revision,
        state_revision=state_revision,
        state_digest=state_digest,
        next_action_revision=next_action_revision,
        pending_revision=pending_revision,
        terminal_digest=terminal_digest,
    )


def _validated_snapshot_rows(
    rows: tuple[sqlite3.Row, ...],
    head: _LoadedHead,
) -> dict[int, tuple[SolverState, str, sqlite3.Row]]:
    if not rows:
        raise ValueError("head has no snapshot")
    states: dict[int, tuple[SolverState, str, sqlite3.Row]] = {}
    previous_revision: int | None = None
    for index, row in enumerate(rows):
        state, digest, revision, source_revision = _validated_snapshot_row(row, head)
        if index == 0:
            if revision != head.initial_revision or source_revision is not None:
                raise ValueError("initial snapshot linkage mismatch")
        elif previous_revision is None or revision != previous_revision + 1:
            raise ValueError("snapshot revisions are not contiguous")
        if revision in states:
            raise ValueError("duplicate snapshot revision")
        states[revision] = (state, digest, row)
        previous_revision = revision
    return states


def _validated_snapshot_row(
    row: sqlite3.Row,
    head: _LoadedHead,
) -> tuple[SolverState, str, int, int | None]:
    if (
        _stored_row_identity(row, "run_id") != head.run_id
        or _stored_row_identity(row, "run_incarnation") != head.run_incarnation
    ):
        raise ValueError("snapshot identity mismatch")
    revision = _stored_row_nonnegative_int(row, "state_revision")
    source_revision = _stored_row_optional_nonnegative_int(
        row,
        "source_action_revision",
    )
    _stored_row_nonnegative_int(row, "created_at_ns")
    state, digest = _decode_state_row(row)
    if state.run_id != head.run_id or state.run_incarnation != head.run_incarnation:
        raise ValueError("snapshot state identity mismatch")
    if state.revision != revision:
        raise ValueError("snapshot state revision mismatch")
    return state, digest, revision, source_revision


def _validated_reconciliation_rows(
    rows: tuple[sqlite3.Row, ...],
    head: _LoadedHead,
) -> dict[int, sqlite3.Row]:
    reconciliations: dict[int, sqlite3.Row] = {}
    for row in rows:
        action_revision = _validated_reconciliation_row(row, head)
        if action_revision in reconciliations:
            raise ValueError("duplicate reconciliation identity")
        reconciliations[action_revision] = row
    return reconciliations


def _validated_reconciliation_row(row: sqlite3.Row, head: _LoadedHead) -> int:
    if (
        _stored_row_identity(row, "run_id") != head.run_id
        or _stored_row_identity(row, "run_incarnation") != head.run_incarnation
    ):
        raise ValueError("reconciliation identity mismatch")
    action_revision = _stored_row_nonnegative_int(row, "action_revision")
    outcome = _stored_row_text(row, "outcome")
    if outcome not in {"KNOWN", "UNKNOWN"}:
        raise ValueError("reconciliation outcome is invalid")
    _stored_row_nonnegative_int(row, "result_state_revision")
    _stored_row_digest(row, "result_state_digest")
    result_bytes = _stored_row_canonical_bytes(row, "result_bytes")
    if _stored_row_digest(row, "result_digest") != _digest(result_bytes):
        raise ValueError("reconciliation digest mismatch")
    _stored_row_nonnegative_int(row, "created_at_ns")
    return action_revision


def _replay_action_rows(
    rows: tuple[sqlite3.Row, ...],
    reconciliations: dict[int, sqlite3.Row],
    states: dict[int, tuple[SolverState, str, sqlite3.Row]],
    head: _LoadedHead,
) -> _LoadedChain:
    if len(rows) != head.next_action_revision:
        raise ValueError("action revisions are not contiguous with the head")
    chain = _LoadedChain(
        current_revision=head.initial_revision,
        current_digest=states[head.initial_revision][1],
        used_snapshots={head.initial_revision},
        used_reconciliations=set(),
        candidate_ids=set(),
        execution_ids=set(),
        normalized_ast_digests=set(),
    )
    for expected_revision, row in enumerate(rows):
        _replay_action_row(
            row,
            expected_revision,
            len(rows),
            reconciliations,
            states,
            head,
            chain,
        )
    _validate_replayed_chain(states, reconciliations, head, chain)
    return chain


def _replay_action_row(
    row: sqlite3.Row,
    expected_revision: int,
    action_count: int,
    reconciliations: dict[int, sqlite3.Row],
    states: dict[int, tuple[SolverState, str, sqlite3.Row]],
    head: _LoadedHead,
    chain: _LoadedChain,
) -> None:
    action_kind = _validated_action_row(
        row,
        expected_revision,
        head,
        chain,
    )
    reconciliation = reconciliations.get(expected_revision)
    if action_kind == "transition":
        _replay_transition_action(
            row,
            reconciliation,
            states,
            expected_revision,
            chain,
        )
        return
    _replay_execution_action(
        row,
        reconciliation,
        states,
        expected_revision,
        action_count,
        chain,
    )


def _validated_action_row(
    row: sqlite3.Row,
    expected_revision: int,
    head: _LoadedHead,
    chain: _LoadedChain,
) -> str:
    if (
        _stored_row_identity(row, "run_id") != head.run_id
        or _stored_row_identity(row, "run_incarnation") != head.run_incarnation
    ):
        raise ValueError("action identity mismatch")
    if _stored_row_nonnegative_int(row, "action_revision") != expected_revision:
        raise ValueError("action revisions contain a gap")
    if (
        _stored_row_nonnegative_int(row, "base_state_revision")
        != chain.current_revision
        or _stored_row_digest(row, "base_state_digest") != chain.current_digest
    ):
        raise ValueError("action base does not match the preceding snapshot")
    action_bytes = _stored_row_canonical_bytes(row, "action_bytes")
    if _stored_row_digest(row, "action_digest") != _digest(action_bytes):
        raise ValueError("action digest mismatch")
    _stored_row_nonnegative_int(row, "created_at_ns")
    return _validated_action_kind_shape(row)


def _validated_action_kind_shape(row: sqlite3.Row) -> str:
    action_kind = _stored_row_text(row, "action_kind")
    if action_kind == "transition":
        _stored_row_nonnegative_int(row, "result_state_revision")
        _stored_row_digest(row, "result_state_digest")
        _stored_row_none(row, "candidate_id")
        _stored_row_none(row, "execution_id")
        _stored_row_none(row, "normalized_ast_digest")
        return action_kind
    if action_kind == "execution":
        _stored_row_none(row, "result_state_revision")
        _stored_row_none(row, "result_state_digest")
        _stored_row_identity(row, "candidate_id")
        _stored_row_identity(row, "execution_id")
        _stored_row_digest(row, "normalized_ast_digest")
        return action_kind
    raise ValueError("action kind is invalid")


def _replay_transition_action(
    row: sqlite3.Row,
    reconciliation: sqlite3.Row | None,
    states: dict[int, tuple[SolverState, str, sqlite3.Row]],
    action_revision: int,
    chain: _LoadedChain,
) -> None:
    if reconciliation is not None:
        raise ValueError("transition action has an execution reconciliation")
    result_revision = row["result_state_revision"]
    if result_revision != chain.current_revision + 1:
        raise ValueError("transition state revision is not contiguous")
    chain.current_revision, chain.current_digest = _consume_result_snapshot(
        states,
        result_revision,
        row["result_state_digest"],
        action_revision,
        chain.used_snapshots,
    )


def _replay_execution_action(
    row: sqlite3.Row,
    reconciliation: sqlite3.Row | None,
    states: dict[int, tuple[SolverState, str, sqlite3.Row]],
    action_revision: int,
    action_count: int,
    chain: _LoadedChain,
) -> None:
    reservation = _reservation_from_action_row(row)
    if reservation.candidate_id in chain.candidate_ids:
        raise ValueError("execution candidate_id is repeated")
    if reservation.execution_id in chain.execution_ids:
        raise ValueError("execution execution_id is repeated")
    if reservation.normalized_ast_digest in chain.normalized_ast_digests:
        raise ValueError("execution normalized_ast_digest is repeated")
    chain.candidate_ids.add(reservation.candidate_id)
    chain.execution_ids.add(reservation.execution_id)
    chain.normalized_ast_digests.add(reservation.normalized_ast_digest)
    base_state = states[chain.current_revision][0]
    candidate = next(
        (
            item
            for item in base_state.sql_candidates
            if item.candidate_id == reservation.candidate_id
        ),
        None,
    )
    if (
        candidate is None
        or candidate.normalized_ast_digest != reservation.normalized_ast_digest
    ):
        raise ValueError("execution candidate does not match its base state")
    if reconciliation is None:
        if action_revision != action_count - 1:
            raise ValueError("only the final action may be pending")
        chain.pending_reservation = reservation
        return
    _replay_execution_reconciliation(
        row,
        reconciliation,
        states,
        action_revision,
        chain,
    )


def _replay_execution_reconciliation(
    action: sqlite3.Row,
    reconciliation: sqlite3.Row,
    states: dict[int, tuple[SolverState, str, sqlite3.Row]],
    action_revision: int,
    chain: _LoadedChain,
) -> None:
    result_revision = reconciliation["result_state_revision"]
    if result_revision != chain.current_revision + 1:
        raise ValueError("reconciled state revision is not contiguous")
    chain.current_revision, chain.current_digest = _consume_result_snapshot(
        states,
        result_revision,
        reconciliation["result_state_digest"],
        action_revision,
        chain.used_snapshots,
    )
    chain.used_reconciliations.add(action_revision)
    if reconciliation["outcome"] == "UNKNOWN":
        _validate_unknown_reconciliation(
            action,
            reconciliation,
            states,
            result_revision,
        )


def _validate_unknown_reconciliation(
    action: sqlite3.Row,
    reconciliation: sqlite3.Row,
    states: dict[int, tuple[SolverState, str, sqlite3.Row]],
    result_revision: int,
) -> None:
    before_state = states[action["base_state_revision"]][0]
    result_state = states[result_revision][0]
    if (
        reconciliation["result_bytes"] != b"null"
        or result_state.stop_reason is not SolverStopReason.TOOL_FAILURE
        or result_state.execution_results != before_state.execution_results
    ):
        raise ValueError("unknown reconciliation fabricated a result")


def _validate_replayed_chain(
    states: dict[int, tuple[SolverState, str, sqlite3.Row]],
    reconciliations: dict[int, sqlite3.Row],
    head: _LoadedHead,
    chain: _LoadedChain,
) -> None:
    if set(states) != chain.used_snapshots:
        raise ValueError("orphan solver state snapshot")
    if set(reconciliations) != chain.used_reconciliations:
        raise ValueError("orphan execution reconciliation")
    if head.pending_revision is None:
        if chain.pending_reservation is not None:
            raise ValueError("unresolved execution is not marked pending")
    elif (
        chain.pending_reservation is None
        or head.pending_revision != chain.pending_reservation.action_revision
    ):
        raise ValueError("pending execution head linkage is invalid")
    if (
        head.state_revision != chain.current_revision
        or head.state_digest != chain.current_digest
    ):
        raise ValueError("head does not match the latest committed snapshot")


def _validated_terminal_row(
    row: sqlite3.Row | None,
    head: _LoadedHead,
    pending_reservation: SolverExecutionReservation | None,
) -> SolverTerminalRecord | None:
    if row is None:
        if head.terminal_digest is not None:
            raise ValueError("terminal head marker has no terminal record")
        return None
    if head.terminal_digest is None:
        raise ValueError("terminal record has no head marker")
    terminal = _terminal_from_row(row)
    _stored_row_canonical_bytes(row, "terminal_bytes")
    if (
        terminal.terminal_digest != _digest(terminal.terminal_bytes)
        or terminal.terminal_digest != head.terminal_digest
    ):
        raise ValueError("terminal digest mismatch")
    if (
        terminal.run_id != head.run_id
        or terminal.run_incarnation != head.run_incarnation
        or terminal.state_revision != head.state_revision
        or terminal.state_digest != head.state_digest
        or terminal.next_action_revision != head.next_action_revision
        or pending_reservation is not None
    ):
        raise ValueError("terminal record does not match the exact head")
    return terminal


def _verified_terminal_evidence(
    terminal: SolverTerminalRecord | None,
    actions: tuple[sqlite3.Row, ...],
    reconciliations: dict[int, sqlite3.Row],
    states: dict[int, tuple[SolverState, str, sqlite3.Row]],
) -> SolverTerminalEvidenceV1 | None:
    if terminal is None or not actions:
        return None
    action = actions[-1]
    if action["action_kind"] != "execution":
        return None
    reconciliation = reconciliations.get(action["action_revision"])
    if reconciliation is None or reconciliation["outcome"] != "KNOWN":
        return None
    result_bytes = reconciliation["result_bytes"]
    if result_bytes == terminal.terminal_bytes:
        return None
    evidence = decode_verified_solver_terminal_evidence(result_bytes)
    if evidence is None:
        return None
    try:
        before_state = states[action["base_state_revision"]][0]
        after_state = states[reconciliation["result_state_revision"]][0]
        terminal_result = _terminal_result_from_bytes(terminal.terminal_bytes)
        reservation = _reservation_authority(_reservation_from_action_row(action))
    except (KeyError, TypeError, ValueError):
        return None
    if not validate_verified_solver_terminal_evidence(
        evidence,
        before_state,
        after_state,
        reservation,
        terminal_result,
    ):
        return None
    return evidence


def _validate_terminal_reconciliation_link(
    terminal: SolverTerminalRecord | None,
    actions: tuple[sqlite3.Row, ...],
    reconciliations: dict[int, sqlite3.Row],
) -> None:
    if terminal is None or not actions:
        return
    action = actions[-1]
    if action["action_kind"] != "execution":
        return
    reconciliation = reconciliations.get(action["action_revision"])
    if reconciliation is None:
        raise ValueError("sealed execution terminal lacks reconciliation")
    if reconciliation["outcome"] != "KNOWN":
        return
    if (
        reconciliation["result_bytes"] == terminal.terminal_bytes
        and reconciliation["result_digest"] == terminal.terminal_digest
    ):
        return
    if _is_solver_terminal_evidence_envelope(reconciliation["result_bytes"]):
        return
    raise ValueError("terminal differs from known execution reconciliation")


def _known_reconciliation_links_terminal(
    connection: sqlite3.Connection,
    reconciliation: sqlite3.Row,
    after_state: SolverState,
    terminal_bytes: bytes,
) -> bool:
    evidence = decode_verified_solver_terminal_evidence(
        reconciliation["result_bytes"]
    )
    if evidence is None:
        return False
    action = connection.execute(
        f"""
        SELECT {_ACTION_SELECT} FROM adaptive_solver_checkpoint_actions
        WHERE run_id = ? AND run_incarnation = ? AND action_revision = ?
        """,
        (
            after_state.run_id,
            after_state.run_incarnation,
            reconciliation["action_revision"],
        ),
    ).fetchone()
    if action is None:
        return False
    base_snapshot = connection.execute(
        f"""
        SELECT {_SNAPSHOT_SELECT} FROM adaptive_solver_checkpoint_snapshots
        WHERE run_id = ? AND run_incarnation = ? AND state_revision = ?
        """,
        (
            after_state.run_id,
            after_state.run_incarnation,
            action["base_state_revision"],
        ),
    ).fetchone()
    if base_snapshot is None:
        return False
    try:
        before_state, _ = _decode_state_row(base_snapshot)
        terminal = _terminal_result_from_bytes(terminal_bytes)
        reservation = _reservation_authority(_reservation_from_action_row(action))
    except (TypeError, ValueError):
        return False
    return validate_verified_solver_terminal_evidence(
        evidence,
        before_state,
        after_state,
        reservation,
        terminal,
    )


def _is_solver_terminal_evidence_envelope(value: bytes) -> bool:
    try:
        document = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False
    return bool(
        type(document) is dict
        and document.get("record_kind") == "text2sql_solver_terminal_evidence"
    )


def _terminal_result_from_bytes(value: bytes) -> TextToSqlTerminalResult:
    try:
        mapping = json.loads(value)
        return TextToSqlTerminalResult.from_mapping(mapping)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("terminal bytes are not a valid terminal result") from exc


def _reservation_authority(
    reservation: SolverExecutionReservation,
) -> SolverExecutionReservationAuthority:
    if type(reservation) is not SolverExecutionReservation:
        raise TypeError("reservation must be SolverExecutionReservation")
    return SolverExecutionReservationAuthority(
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


def _consume_result_snapshot(
    states: dict[int, tuple[SolverState, str, sqlite3.Row]],
    result_revision: int,
    result_digest: str,
    action_revision: int,
    used_snapshots: set[int],
) -> tuple[int, str]:
    entry = states.get(result_revision)
    if (
        entry is None
        or entry[1] != result_digest
        or entry[2]["source_action_revision"] != action_revision
        or result_revision in used_snapshots
    ):
        raise ValueError("action result snapshot linkage is invalid")
    used_snapshots.add(result_revision)
    return result_revision, result_digest


def _decode_state_row(row: sqlite3.Row) -> tuple[SolverState, str]:
    state_bytes = _stored_row_bytes(row, "state_bytes")
    state_digest = _stored_row_digest(row, "state_digest")
    if _digest(state_bytes) != state_digest:
        raise ValueError("state digest mismatch")
    state = deserialize_as(state_bytes, SolverState)
    if serialize_contract(state) != state_bytes:
        raise ValueError("stored SolverState is not canonical")
    return state, state_digest


def _reservation_from_action_row(row: sqlite3.Row) -> SolverExecutionReservation:
    if _validated_action_kind_shape(row) != "execution":
        raise ValueError("execution reservation requires an execution action")
    action_bytes = _stored_row_canonical_bytes(row, "action_bytes")
    if _stored_row_digest(row, "action_digest") != _digest(action_bytes):
        raise ValueError("action digest mismatch")
    value = json.loads(action_bytes)
    if type(value) is not dict or set(value) != {
        "candidate_id",
        "execution_id",
        "normalized_ast_digest",
        "request",
    }:
        raise ValueError("execution reservation payload shape is invalid")
    candidate_id = _stored_row_identity(row, "candidate_id")
    execution_id = _stored_row_identity(row, "execution_id")
    normalized_ast_digest = _stored_row_digest(row, "normalized_ast_digest")
    if (
        value["candidate_id"] != candidate_id
        or value["execution_id"] != execution_id
        or value["normalized_ast_digest"] != normalized_ast_digest
    ):
        raise ValueError("execution reservation columns do not match its payload")
    request_bytes = canonical_json_bytes(value["request"])
    return SolverExecutionReservation(
        run_id=_stored_row_identity(row, "run_id"),
        run_incarnation=_stored_row_identity(row, "run_incarnation"),
        action_revision=_stored_row_nonnegative_int(row, "action_revision"),
        base_state_revision=_stored_row_nonnegative_int(
            row,
            "base_state_revision",
        ),
        base_state_digest=_stored_row_digest(row, "base_state_digest"),
        candidate_id=candidate_id,
        execution_id=execution_id,
        normalized_ast_digest=normalized_ast_digest,
        request_bytes=request_bytes,
        request_digest=_digest(request_bytes),
        created_at_ns=_stored_row_nonnegative_int(row, "created_at_ns"),
    )


def _validate_reconciliation_inputs(
    reservation: SolverExecutionReservation,
    after_state: SolverState,
) -> None:
    if type(reservation) is not SolverExecutionReservation:
        raise TypeError("reservation must be SolverExecutionReservation")
    if (
        after_state.run_id != reservation.run_id
        or after_state.run_incarnation != reservation.run_incarnation
        or after_state.revision != reservation.base_state_revision + 1
    ):
        raise ValueError("execution reconciliation requires the exact next SolverState")


def _validate_unknown_result_state(
    outcome: str,
    before_state: SolverState,
    after_state: SolverState,
) -> None:
    if (
        outcome == "UNKNOWN"
        and after_state.execution_results != before_state.execution_results
    ):
        raise ValueError("unknown execution cannot fabricate an ExecutionResult")


def _reconciliation_matches(
    row: sqlite3.Row,
    after_state: SolverState,
    after_digest: str,
    outcome: str,
    result_bytes: bytes,
    result_digest: str,
) -> bool:
    return bool(
        row["outcome"] == outcome
        and row["result_state_revision"] == after_state.revision
        and row["result_state_digest"] == after_digest
        and row["result_bytes"] == result_bytes
        and row["result_digest"] == result_digest
    )


def _action_matches_reservation(
    row: sqlite3.Row,
    reservation: SolverExecutionReservation,
) -> bool:
    try:
        return (
            row["action_kind"] == "execution"
            and _reservation_from_action_row(row) == reservation
        )
    except (TypeError, ValueError):
        return False


def _terminal_from_row(row: sqlite3.Row) -> SolverTerminalRecord:
    return SolverTerminalRecord(
        run_id=_stored_row_identity(row, "run_id"),
        run_incarnation=_stored_row_identity(row, "run_incarnation"),
        state_revision=_stored_row_nonnegative_int(row, "state_revision"),
        state_digest=_stored_row_digest(row, "state_digest"),
        next_action_revision=_stored_row_nonnegative_int(
            row,
            "next_action_revision",
        ),
        terminal_bytes=_stored_row_bytes(row, "terminal_bytes"),
        terminal_digest=_stored_row_digest(row, "terminal_digest"),
        created_at_ns=_stored_row_nonnegative_int(row, "created_at_ns"),
    )


def _historical_checkpoint(
    state: SolverState,
    state_digest: str,
    *,
    initial_state_revision: int,
    next_action_revision: int,
) -> SolverCheckpoint:
    return SolverCheckpoint(
        state=state,
        cursor=SolverCheckpointCursor(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            initial_state_revision=initial_state_revision,
            state_revision=state.revision,
            state_digest=state_digest,
            next_action_revision=next_action_revision,
            pending_execution_action_revision=None,
        ),
        pending_execution=None,
        terminal=None,
    )


def _state_bytes_and_digest(state: SolverState) -> tuple[bytes, str]:
    if type(state) is not SolverState:
        raise TypeError("state must be exact SolverState")
    state_bytes = serialize_contract(state)
    decoded = deserialize_as(state_bytes, SolverState)
    if decoded != state:
        raise ValueError("SolverState does not round-trip canonically")
    return state_bytes, _digest(state_bytes)


def _require_state_successor(before: SolverState, after: SolverState) -> None:
    if (
        before.run_id != after.run_id
        or before.run_incarnation != after.run_incarnation
        or after.revision != before.revision + 1
    ):
        raise ValueError("after_state must be the exact next SolverState revision")


def _canonical_input_bytes(value: Any, *, label: str) -> bytes:
    try:
        return canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON") from exc


def _validated_transition_replay_input(
    replay_input: (
        SolverSqlProposalReplayInput
        | SolverMissingEvidenceReplayInput
        | SolverReentryAdmissionReplayInput
        | SolverReentryCompletedReplayInput
        | None
    ),
) -> tuple[
    SolverSqlProposalReplayInput
    | SolverMissingEvidenceReplayInput
    | SolverReentryAdmissionReplayInput
    | SolverReentryCompletedReplayInput
    | None,
    bytes | None,
    str | None,
]:
    if replay_input is None:
        return None, None, None
    if type(replay_input) not in _TRANSITION_REPLAY_INPUT_TYPES:
        raise TypeError("replay_input must be an exact solver replay input")
    raw = serialize_replay_input(replay_input)
    checked = deserialize_replay_input(raw)
    if type(checked) not in _TRANSITION_REPLAY_INPUT_TYPES:
        raise TypeError("replay_input must be an exact solver replay input")
    return checked, raw, _digest(raw)


def _validate_transition_replay(
    connection: sqlite3.Connection,
    before_state: SolverState,
    after_state: SolverState,
    action_bytes: bytes,
    replay_input: (
        SolverSqlProposalReplayInput
        | SolverMissingEvidenceReplayInput
        | SolverReentryAdmissionReplayInput
        | SolverReentryCompletedReplayInput
    ),
) -> None:
    checked = replay_input
    if type(checked) in (
        SolverSqlProposalReplayInput,
        SolverMissingEvidenceReplayInput,
    ):
        from custom_tools.text_to_sql.adaptive.solver_loop import (
            apply_solver_proposal_from_parsed,
        )

        parsed_candidate = (
            checked.parsed_candidate.to_candidate()
            if type(checked) is SolverSqlProposalReplayInput
            else None
        )
        try:
            replayed = apply_solver_proposal_from_parsed(
                before_state,
                checked.proposal,
                base_revision=before_state.revision,
                parsed_candidate=parsed_candidate,
                requirements=checked.requirements,
                generated_ids=checked.generated_ids,
            )
        except (TypeError, ValueError) as exc:
            raise AdaptiveSolverCheckpointConflictError(
                "solver replay input cannot reproduce the transition"
            ) from exc
        expected_action = canonical_json_bytes(replayed.action.model_dump(mode="json"))
        if replayed.state != after_state or expected_action != action_bytes:
            raise AdaptiveSolverCheckpointConflictError(
                "solver replay input does not reproduce the transition"
            )
    elif type(checked) is SolverReentryAdmissionReplayInput:
        _validate_reentry_admission_transition(
            connection,
            before_state,
            after_state,
            action_bytes,
            checked,
        )
    else:
        _validate_reentry_completed_transition(
            connection,
            before_state,
            after_state,
            action_bytes,
            checked,
        )


def _transition_requires_replay(
    before_state: SolverState,
    after_state: SolverState,
    action_bytes: bytes,
) -> bool:
    if len(after_state.action_history) == len(before_state.action_history) + 1:
        kind = after_state.action_history[-1].kind.value
        if kind in {"sql_candidate", "missing_evidence"}:
            return True
    if len(after_state.research_reentries) == len(before_state.research_reentries) + 1:
        if after_state.research_reentries[-1].status is ResearchReentryStatus.ADMITTED:
            return True
    if len(after_state.research_reentries) == len(before_state.research_reentries):
        before_by_id = {
            item.research_reentry_id: item for item in before_state.research_reentries
        }
        if any(
            item.status is ResearchReentryStatus.COMPLETED
            and item.research_reentry_id in before_by_id
            and before_by_id[item.research_reentry_id].status
            is ResearchReentryStatus.ADMITTED
            for item in after_state.research_reentries
        ):
            return True
    try:
        action = json.loads(action_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(action, dict):
        return False
    kind = action.get("kind")
    if kind in {"sql_candidate", "missing_evidence", "research_reentry_admitted"}:
        return True
    record = action.get("record")
    return bool(
        kind == "research_reentry_finalized"
        and isinstance(record, dict)
        and record.get("status") == ResearchReentryStatus.COMPLETED.value
    )


def _validate_reentry_admission_transition(
    connection: sqlite3.Connection,
    before_state: SolverState,
    after_state: SolverState,
    action_bytes: bytes,
    replay_input: SolverReentryAdmissionReplayInput,
) -> None:
    from custom_tools.text_to_sql.adaptive.solver_loop import admit_targeted_reentry

    research_state = _research_state_for_replay(
        connection,
        before_state,
        replay_input.research_state_revision,
        replay_input.research_state_digest,
    )
    try:
        replayed = admit_targeted_reentry(
            before_state,
            research_state,
            replay_input.missing_evidence_request_id,
            base_revision=before_state.revision,
            id_factory=lambda: replay_input.generated_reentry_id,
        )
    except (TypeError, ValueError) as exc:
        raise AdaptiveSolverCheckpointConflictError(
            "solver re-entry admission replay input cannot reproduce transition"
        ) from exc
    expected_action = canonical_json_bytes(
        {
            "kind": "research_reentry_admitted",
            "record": replayed.record.model_dump(mode="json"),
        }
    )
    if after_state != replayed.state or action_bytes != expected_action:
        raise AdaptiveSolverCheckpointConflictError(
            "solver re-entry admission replay input does not reproduce transition"
        )


def _validate_reentry_completed_transition(
    connection: sqlite3.Connection,
    before_state: SolverState,
    after_state: SolverState,
    action_bytes: bytes,
    replay_input: SolverReentryCompletedReplayInput,
) -> None:
    from custom_tools.text_to_sql.adaptive.solver_loop import finalize_targeted_reentry

    research_state = _research_state_for_replay(
        connection,
        before_state,
        replay_input.research_state_revision,
        replay_input.research_state_digest,
    )
    try:
        replayed = finalize_targeted_reentry(
            before_state,
            replay_input.research_reentry_id,
            ResearchReentryStatus.COMPLETED,
            base_revision=before_state.revision,
            research_state=research_state,
            freshness_context=replay_input.freshness_context,
            requirements=replay_input.requirements,
        )
    except (TypeError, ValueError) as exc:
        raise AdaptiveSolverCheckpointConflictError(
            "solver completed re-entry replay input cannot reproduce transition"
        ) from exc
    expected_action = canonical_json_bytes(
        {
            "kind": "research_reentry_finalized",
            "record": replayed.record.model_dump(mode="json"),
        }
    )
    if after_state != replayed.state or action_bytes != expected_action:
        raise AdaptiveSolverCheckpointConflictError(
            "solver completed re-entry replay input does not reproduce transition"
        )


def _research_state_for_replay(
    connection: sqlite3.Connection,
    solver_state: SolverState,
    revision: int,
    expected_digest: str,
) -> ResearchState:
    try:
        row = connection.execute(
            """
            SELECT payload, digest
            FROM adaptive_research_state_snapshots
            WHERE run_id = ? AND run_incarnation = ?
              AND contract_name = 'research_state' AND revision = ?
            """,
            (solver_state.run_id, solver_state.run_incarnation, revision),
        ).fetchone()
    except sqlite3.Error as exc:
        raise AdaptiveSolverCheckpointConflictError(
            "solver re-entry replay requires a durable research snapshot"
        ) from exc
    if row is None:
        raise AdaptiveSolverCheckpointConflictError(
            "solver re-entry replay requires a durable research snapshot"
        )
    payload = row["payload"]
    stored_digest = row["digest"]
    if (
        not isinstance(payload, bytes)
        or not isinstance(stored_digest, str)
        or stored_digest != expected_digest
        or _digest(payload) != expected_digest
    ):
        raise AdaptiveSolverCheckpointConflictError(
            "solver re-entry research snapshot digest does not match replay input"
        )
    try:
        research_state = deserialize_as(payload, ResearchState)
    except (TypeError, ValueError) as exc:
        raise AdaptiveSolverCheckpointConflictError(
            "solver re-entry research snapshot is invalid"
        ) from exc
    if (
        serialize_contract(research_state) != payload
        or research_state.run_id != solver_state.run_id
        or research_state.run_incarnation != solver_state.run_incarnation
        or research_state.schema_namespace_version
        != solver_state.schema_namespace_version
        or research_state.revision != revision
    ):
        raise AdaptiveSolverCheckpointConflictError(
            "solver re-entry research snapshot identity is invalid"
        )
    return research_state


def _require_canonical_bytes(value: bytes, *, label: str) -> bytes:
    if type(value) is not bytes:
        raise TypeError(f"{label} must be exact bytes")
    try:
        decoded = json.loads(value)
        if canonical_json_bytes(decoded) != value:
            raise ValueError
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain canonical JSON bytes") from exc
    return value


def _stored_row_type(row: sqlite3.Row, column: str, expected: str) -> None:
    storage_type = row[f"storage_type__{column}"]
    if type(storage_type) is not str or storage_type != expected:
        raise ValueError(f"stored {column} has invalid SQLite storage type")


def _stored_row_text(row: sqlite3.Row, column: str) -> str:
    _stored_row_type(row, column, "text")
    value = row[column]
    if type(value) is not str:
        raise ValueError(f"stored {column} is not text")
    return value


def _stored_row_identity(row: sqlite3.Row, column: str) -> str:
    return _stored_identity(_stored_row_text(row, column))


def _stored_row_digest(row: sqlite3.Row, column: str) -> str:
    return _stored_digest(_stored_row_text(row, column))


def _stored_row_nonnegative_int(row: sqlite3.Row, column: str) -> int:
    _stored_row_type(row, column, "integer")
    return _stored_nonnegative_int(row[column])


def _stored_row_optional_nonnegative_int(
    row: sqlite3.Row,
    column: str,
) -> int | None:
    if row[column] is None:
        _stored_row_type(row, column, "null")
        return None
    return _stored_row_nonnegative_int(row, column)


def _stored_row_optional_digest(row: sqlite3.Row, column: str) -> str | None:
    if row[column] is None:
        _stored_row_type(row, column, "null")
        return None
    return _stored_row_digest(row, column)


def _stored_row_bytes(row: sqlite3.Row, column: str) -> bytes:
    _stored_row_type(row, column, "blob")
    return _stored_bytes(row[column])


def _stored_row_canonical_bytes(row: sqlite3.Row, column: str) -> bytes:
    return _require_canonical_bytes(
        _stored_row_bytes(row, column),
        label=f"stored {column}",
    )


def _stored_row_none(row: sqlite3.Row, column: str) -> None:
    _stored_row_type(row, column, "null")
    if row[column] is not None:
        raise ValueError(f"stored {column} must be NULL")


def _stored_bytes(value: Any) -> bytes:
    if type(value) is not bytes:
        raise ValueError("stored value is not a BLOB")
    return value


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _require_digest(value: str, label: str) -> None:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be exact sha256 lowercase hex")


def _stored_digest(value: Any) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise ValueError("stored digest is invalid")
    return value


def _require_identity(value: str, label: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{label} is required")


def _stored_identity(value: Any) -> str:
    if type(value) is not str or not value:
        raise ValueError("stored identity is invalid")
    return value


def _require_action_revision(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("action_revision must be a non-negative integer")


def _stored_nonnegative_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("stored integer is invalid")
    return value


def _now_ns() -> int:
    value = time.time_ns()
    if type(value) is not int or value < 0:
        raise ValueError("created_at_ns must be a non-negative integer")
    return value
