"""Append-only SQLite journal for adaptive Text-to-SQL loop checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterator
import uuid

from custom_tools.text_to_sql.adaptive.replay_inputs import (
    ReplayInputError,
    ResearchTerminalReplayInput,
    SolverMissingEvidenceReplayInput,
    SolverReentryAdmissionReplayInput,
    SolverReentryCompletedReplayInput,
    SolverSqlProposalReplayInput,
    deserialize_replay_input,
    serialize_replay_input,
)

from ._adaptive_solver_checkpoint_sql import (
    canonical_solver_checkpoint_schema_signature,
    canonical_solver_checkpoint_v2_schema_signature,
    create_solver_checkpoint_v2_schema,
    create_solver_checkpoint_v3_additions,
    solver_checkpoint_schema_signature,
    solver_checkpoint_v2_schema_signature,
)
from .sqlite_schema_signature import owned_sqlite_schema_signature
from .state_files import prepare_sqlite_file, secure_sqlite_sidecars


class AdaptiveLoopKind(StrEnum):
    RESEARCH = "research"
    SOLVER = "solver"


class AdaptiveActionPhase(StrEnum):
    PLANNED = "planned"
    OBSERVED = "observed"
    TERMINAL = "terminal"


class AdaptiveCheckpointError(RuntimeError):
    """Base error for the closed adaptive checkpoint journal."""


class AdaptiveCheckpointMigrationError(AdaptiveCheckpointError):
    """The adaptive checkpoint journal schema cannot be safely opened."""


class AdaptiveCheckpointIntegrityError(AdaptiveCheckpointError):
    """A stored checkpoint event failed its canonical digest check."""


class AdaptiveCheckpointCasError(AdaptiveCheckpointError):
    """The caller's expected revision is no longer current."""


class AdaptiveCheckpointConflictError(AdaptiveCheckpointError):
    """A duplicate checkpoint event has different immutable content."""


class AdaptiveArtifactDigestMismatchError(AdaptiveCheckpointError):
    """Observed artifact does not match the artifact planned for this revision."""


@dataclass(frozen=True, slots=True)
class AdaptiveCheckpointKey:
    run_id: str
    run_incarnation: str
    loop_kind: AdaptiveLoopKind
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id is required")
        if not isinstance(self.run_incarnation, str) or not self.run_incarnation:
            raise ValueError("run_incarnation is required")
        if not isinstance(self.loop_kind, AdaptiveLoopKind):
            raise TypeError("loop_kind must be AdaptiveLoopKind")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class AdaptiveCheckpointEvent:
    key: AdaptiveCheckpointKey
    phase: AdaptiveActionPhase
    action: Any
    action_digest: str
    artifact_digest: str | None
    created_at_ns: int


@dataclass(frozen=True, slots=True)
class AdaptiveCheckpointSnapshot:
    key: AdaptiveCheckpointKey
    planned: AdaptiveCheckpointEvent | None
    observed: AdaptiveCheckpointEvent | None
    terminal: AdaptiveCheckpointEvent | None


_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
ADAPTIVE_STATE_STORE_SCHEMA_VERSION = 3
_SCHEMA_META_TABLE = "adaptive_checkpoint_meta"
_SCHEMA_VERSION_KEY = "schema_version"
_SOLVER_REPLAY_INPUT_TYPES = (
    SolverSqlProposalReplayInput,
    SolverMissingEvidenceReplayInput,
    SolverReentryAdmissionReplayInput,
    SolverReentryCompletedReplayInput,
)
_CHECKPOINT_REPLAY_INPUT_TABLE = "adaptive_checkpoint_replay_inputs"
_CHECKPOINT_REPLAY_INPUT_SQL = (
    """
    CREATE TABLE adaptive_checkpoint_replay_inputs (
        run_id TEXT NOT NULL CHECK (length(run_id) > 0),
        run_incarnation TEXT NOT NULL CHECK (length(run_incarnation) > 0),
        loop_kind TEXT NOT NULL CHECK (loop_kind = 'research'),
        revision INTEGER NOT NULL
            CHECK (typeof(revision) = 'integer' AND revision >= 0),
        phase TEXT NOT NULL CHECK (phase = 'terminal'),
        input_kind TEXT NOT NULL CHECK (input_kind = 'research_terminal'),
        input_version INTEGER NOT NULL
            CHECK (typeof(input_version) = 'integer' AND input_version = 1),
        input_bytes BLOB NOT NULL CHECK (typeof(input_bytes) = 'blob'),
        input_digest TEXT NOT NULL
            CHECK (length(input_digest) = 71 AND substr(input_digest, 1, 7) = 'sha256:' AND
                   substr(input_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        created_at_ns INTEGER NOT NULL
            CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, loop_kind, revision, phase),
        FOREIGN KEY (run_id, run_incarnation, loop_kind, revision, phase)
            REFERENCES adaptive_checkpoint_events
                (run_id, run_incarnation, loop_kind, revision, phase)
    )
    """,
    """
    CREATE TRIGGER adaptive_checkpoint_replay_inputs_no_update
    BEFORE UPDATE ON adaptive_checkpoint_replay_inputs
    BEGIN SELECT RAISE(ABORT, 'adaptive checkpoint replay inputs are immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_checkpoint_replay_inputs_no_delete
    BEFORE DELETE ON adaptive_checkpoint_replay_inputs
    BEGIN SELECT RAISE(ABORT, 'adaptive checkpoint replay inputs are immutable'); END
    """,
)
_CHECKPOINT_TRIGGER_SQL = {
    "adaptive_checkpoint_events_no_update": """
        CREATE TRIGGER adaptive_checkpoint_events_no_update
        BEFORE UPDATE ON adaptive_checkpoint_events
        BEGIN SELECT RAISE(ABORT, 'adaptive checkpoint events are immutable'); END
    """,
    "adaptive_checkpoint_events_no_delete": """
        CREATE TRIGGER adaptive_checkpoint_events_no_delete
        BEFORE DELETE ON adaptive_checkpoint_events
        BEGIN SELECT RAISE(ABORT, 'adaptive checkpoint events are immutable'); END
    """,
    "adaptive_checkpoint_events_created_at_integer": """
        CREATE TRIGGER adaptive_checkpoint_events_created_at_integer
        BEFORE INSERT ON adaptive_checkpoint_events
        WHEN typeof(NEW.created_at_ns) != 'integer' OR NEW.created_at_ns < 0
        BEGIN SELECT RAISE(ABORT, 'adaptive checkpoint created_at_ns must be an integer'); END
    """,
}


def _normalize_schema_sql(sql: str) -> str:
    return " ".join(sql.strip().removesuffix(";").split()).lower()


def _checkpoint_replay_schema_signature(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, tuple[tuple[str, str], ...] | None], ...]:
    return owned_sqlite_schema_signature(
        connection,
        prefix=_CHECKPOINT_REPLAY_INPUT_TABLE,
        table_names=(_CHECKPOINT_REPLAY_INPUT_TABLE,),
    )


def _canonical_checkpoint_replay_schema_signature() -> tuple[
    tuple[str, str, str, tuple[tuple[str, str], ...] | None], ...
]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE adaptive_checkpoint_events (
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                loop_kind TEXT NOT NULL,
                revision INTEGER NOT NULL,
                phase TEXT NOT NULL,
                PRIMARY KEY (run_id, run_incarnation, loop_kind, revision, phase)
            )
            """
        )
        for statement in _CHECKPOINT_REPLAY_INPUT_SQL:
            connection.execute(statement)
        return _checkpoint_replay_schema_signature(connection)
    finally:
        connection.close()


class AdaptiveStateStore:
    """Durable CAS journal; it has no vector-memory or callback dependency."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        commit_fence: object | None = None,
    ) -> None:
        if commit_fence is not None and not callable(
            getattr(commit_fence, "require_open_in_transaction", None)
        ):
            raise TypeError("commit_fence must provide require_open_in_transaction")
        self.db_path = str(db_path)
        self._commit_fence = commit_fence
        self._memory_uri: str | None = None
        self._memory_anchor: sqlite3.Connection | None = None
        self._closed = False
        self._lifecycle_lock = threading.Lock()
        if self.db_path == ":memory:":
            self._memory_uri = (
                f"file:adaptive-state-{uuid.uuid4().hex}?mode=memory&cache=shared"
            )
            self._memory_anchor = sqlite3.connect(self._memory_uri, uri=True)
        if self.db_path != ":memory:":
            prepare_sqlite_file(self.db_path, tighten_existing=True)
        try:
            self._migrate()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Close the store and release the in-memory journal anchor once."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            memory_anchor = self._memory_anchor
            self._memory_anchor = None
        if memory_anchor is not None:
            memory_anchor.close()

    def record_planned(
        self,
        key: AdaptiveCheckpointKey,
        *,
        expected_revision: int | None,
        action: Any,
        artifact_digest: str | None = None,
    ) -> AdaptiveCheckpointEvent:
        return self._append(
            key,
            AdaptiveActionPhase.PLANNED,
            expected_revision=expected_revision,
            action=action,
            artifact_digest=artifact_digest,
        )

    def record_observed(
        self,
        key: AdaptiveCheckpointKey,
        *,
        expected_revision: int,
        action: Any,
        artifact_digest: str | None = None,
    ) -> AdaptiveCheckpointEvent:
        return self._append(
            key,
            AdaptiveActionPhase.OBSERVED,
            expected_revision=expected_revision,
            action=action,
            artifact_digest=artifact_digest,
        )

    def record_terminal(
        self,
        key: AdaptiveCheckpointKey,
        *,
        expected_revision: int | None,
        action: Any,
        artifact_digest: str | None = None,
    ) -> AdaptiveCheckpointEvent:
        return self._append(
            key,
            AdaptiveActionPhase.TERMINAL,
            expected_revision=expected_revision,
            action=action,
            artifact_digest=artifact_digest,
        )

    def record_replayable_terminal(
        self,
        key: AdaptiveCheckpointKey,
        *,
        expected_revision: int | None,
        action: Any,
        replay_input: ResearchTerminalReplayInput,
    ) -> AdaptiveCheckpointEvent:
        """Atomically record one research terminal and its freshness input."""

        if key.loop_kind is not AdaptiveLoopKind.RESEARCH:
            raise ValueError("replayable terminal requires the research loop")
        if type(replay_input) is not ResearchTerminalReplayInput:
            raise TypeError("replay_input must be ResearchTerminalReplayInput")
        checked = deserialize_replay_input(serialize_replay_input(replay_input))
        if type(checked) is not ResearchTerminalReplayInput:
            raise TypeError("replay_input must be ResearchTerminalReplayInput")
        if (
            checked.freshness_context.run_id != key.run_id
            or checked.freshness_context.run_incarnation != key.run_incarnation
        ):
            raise ValueError("terminal replay input does not match checkpoint identity")
        return self._append(
            key,
            AdaptiveActionPhase.TERMINAL,
            expected_revision=expected_revision,
            action=action,
            artifact_digest=None,
            replay_input=checked,
        )

    def load_terminal_replay_input(
        self,
        key: AdaptiveCheckpointKey,
    ) -> ResearchTerminalReplayInput | None:
        """Load terminal freshness input; legacy terminals return ``None``."""

        if key.loop_kind is not AdaptiveLoopKind.RESEARCH:
            raise ValueError("terminal replay input requires the research loop")
        with self._connection() as connection:
            row = self._read_checkpoint_replay_input(connection, key)
            return None if row is None else self._decode_checkpoint_replay_input(row)

    def on_state_transition(
        self,
        key: AdaptiveCheckpointKey,
        phase: AdaptiveActionPhase,
        *,
        expected_revision: int | None,
        action: Any,
        artifact_digest: str | None = None,
    ) -> AdaptiveCheckpointEvent:
        """Durably append one accepted adaptive-state transition.

        The result becomes visible only after the transaction commits. An
        identical retry is idempotent; different content at the same
        checkpoint identity is rejected by the existing CAS rules.
        """
        if not isinstance(phase, AdaptiveActionPhase):
            raise ValueError("phase must be AdaptiveActionPhase")
        return self._append(
            key,
            phase,
            expected_revision=expected_revision,
            action=action,
            artifact_digest=artifact_digest,
        )

    def get_snapshot(self, key: AdaptiveCheckpointKey) -> AdaptiveCheckpointSnapshot:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT phase, action_json, action_digest, artifact_digest, created_at_ns
                FROM adaptive_checkpoint_events
                WHERE run_id = ? AND run_incarnation = ? AND loop_kind = ? AND revision = ?
                ORDER BY created_at_ns
                """,
                (key.run_id, key.run_incarnation, key.loop_kind.value, key.revision),
            ).fetchall()
        events = {
            AdaptiveActionPhase(row["phase"]): self._event_from_row(
                key,
                AdaptiveActionPhase(row["phase"]),
                row,
            )
            for row in rows
        }
        return AdaptiveCheckpointSnapshot(
            key=key,
            planned=events.get(AdaptiveActionPhase.PLANNED),
            observed=events.get(AdaptiveActionPhase.OBSERVED),
            terminal=events.get(AdaptiveActionPhase.TERMINAL),
        )

    def list_events(
        self, key: AdaptiveCheckpointKey
    ) -> tuple[AdaptiveCheckpointEvent, ...]:
        snapshot = self.get_snapshot(key)
        return tuple(
            event
            for event in (snapshot.planned, snapshot.observed, snapshot.terminal)
            if event
        )

    def load_run_events(
        self,
        run_id: str,
        run_incarnation: str,
        loop_kind: AdaptiveLoopKind,
    ) -> tuple[AdaptiveCheckpointEvent, ...]:
        """Load and validate the complete event chain for one loop incarnation."""

        identity = AdaptiveCheckpointKey(run_id, run_incarnation, loop_kind, 0)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT revision, phase, action_json, action_digest,
                       artifact_digest, created_at_ns
                FROM adaptive_checkpoint_events
                WHERE run_id = ? AND run_incarnation = ? AND loop_kind = ?
                ORDER BY revision,
                    CASE phase
                        WHEN 'planned' THEN 0
                        WHEN 'observed' THEN 1
                        WHEN 'terminal' THEN 2
                        ELSE 3
                    END
                """,
                (identity.run_id, identity.run_incarnation, identity.loop_kind.value),
            ).fetchall()

        events: list[AdaptiveCheckpointEvent] = []
        phases_by_revision: dict[int, list[AdaptiveActionPhase]] = {}
        try:
            for row in rows:
                revision = row["revision"]
                if type(revision) is not int or revision < 0:
                    raise ValueError
                phase = AdaptiveActionPhase(row["phase"])
                _validate_artifact_digest(row["artifact_digest"])
                if type(row["created_at_ns"]) is not int or row["created_at_ns"] < 0:
                    raise ValueError
                key = AdaptiveCheckpointKey(
                    identity.run_id,
                    identity.run_incarnation,
                    identity.loop_kind,
                    revision,
                )
                events.append(self._event_from_row(key, phase, row))
                phases_by_revision.setdefault(revision, []).append(phase)
            _validate_complete_event_chain(phases_by_revision)
        except (TypeError, ValueError) as exc:
            raise AdaptiveCheckpointIntegrityError(
                "adaptive checkpoint event chain is invalid"
            ) from exc
        return tuple(events)

    def _append(
        self,
        key: AdaptiveCheckpointKey,
        phase: AdaptiveActionPhase,
        *,
        expected_revision: int | None,
        action: Any,
        artifact_digest: str | None,
        replay_input: ResearchTerminalReplayInput | None = None,
    ) -> AdaptiveCheckpointEvent:
        if replay_input is not None and (
            phase is not AdaptiveActionPhase.TERMINAL
            or key.loop_kind is not AdaptiveLoopKind.RESEARCH
        ):
            raise ValueError("replay input is valid only for a research terminal")
        replay_bytes = (
            None if replay_input is None else serialize_replay_input(replay_input)
        )
        replay_digest = (
            None
            if replay_bytes is None
            else f"sha256:{hashlib.sha256(replay_bytes).hexdigest()}"
        )
        action_json = _canonical_action_json(action)
        action_digest = (
            f"sha256:{hashlib.sha256(action_json.encode('utf-8')).hexdigest()}"
        )
        _validate_artifact_digest(artifact_digest)
        if phase is AdaptiveActionPhase.PLANNED and (
            expected_revision is not None and type(expected_revision) is not int
        ):
            raise ValueError("planned expected_revision must be an integer or None")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._read_event(connection, key, phase)
            if existing is not None:
                if (
                    existing["action_json"] == action_json
                    and existing["artifact_digest"] == artifact_digest
                ):
                    if (
                        phase is AdaptiveActionPhase.TERMINAL
                        and key.loop_kind is AdaptiveLoopKind.RESEARCH
                    ):
                        stored_input = self._read_checkpoint_replay_input(
                            connection,
                            key,
                        )
                        if (
                            (stored_input is None) != (replay_input is None)
                            or stored_input is not None
                            and self._decode_checkpoint_replay_input(stored_input)
                            != replay_input
                        ):
                            raise AdaptiveCheckpointConflictError(
                                "conflicting duplicate terminal replay input"
                            )
                    return self._event_from_row(key, phase, existing)
                raise AdaptiveCheckpointConflictError(
                    "conflicting duplicate checkpoint event"
                )

            if (
                phase is AdaptiveActionPhase.TERMINAL
                and key.loop_kind is AdaptiveLoopKind.RESEARCH
                and replay_input is None
            ):
                raise AdaptiveCheckpointConflictError(
                    "new research terminal requires replay input"
                )

            if self._terminal_revision(connection, key) is not None:
                raise AdaptiveCheckpointCasError(
                    "terminal checkpoint closes this loop incarnation"
                )

            current_revision = self._current_revision(connection, key)
            if phase is AdaptiveActionPhase.PLANNED:
                expected_current = (
                    -1 if expected_revision is None else expected_revision
                )
                if (
                    current_revision != expected_current
                    or key.revision != expected_current + 1
                ):
                    raise AdaptiveCheckpointCasError(
                        "planned checkpoint revision compare-and-swap failed"
                    )
                if (
                    key.revision > 0
                    and self._read_event(
                        connection,
                        AdaptiveCheckpointKey(
                            key.run_id,
                            key.run_incarnation,
                            key.loop_kind,
                            key.revision - 1,
                        ),
                        AdaptiveActionPhase.OBSERVED,
                    )
                    is None
                ):
                    raise AdaptiveCheckpointCasError(
                        "previous revision must be observed before planning next revision"
                    )
            elif (
                phase is AdaptiveActionPhase.TERMINAL
                and self._read_event(connection, key, AdaptiveActionPhase.PLANNED)
                is None
            ):
                expected_current = (
                    -1 if expected_revision is None else expected_revision
                )
                if type(expected_current) is not int:
                    raise AdaptiveCheckpointCasError(
                        "terminal expected_revision must be an integer or None"
                    )
                if (
                    current_revision != expected_current
                    or key.revision != expected_current + 1
                ):
                    raise AdaptiveCheckpointCasError(
                        "terminal checkpoint revision compare-and-swap failed"
                    )
                if (
                    key.revision > 0
                    and self._read_event(
                        connection,
                        AdaptiveCheckpointKey(
                            key.run_id,
                            key.run_incarnation,
                            key.loop_kind,
                            key.revision - 1,
                        ),
                        AdaptiveActionPhase.OBSERVED,
                    )
                    is None
                ):
                    raise AdaptiveCheckpointCasError(
                        "previous revision must be observed before terminal checkpoint"
                    )
            else:
                if (
                    type(expected_revision) is not int
                    or expected_revision != key.revision
                ):
                    raise AdaptiveCheckpointCasError(
                        "checkpoint event expected_revision must equal its revision"
                    )
                if current_revision != key.revision:
                    raise AdaptiveCheckpointCasError(
                        "checkpoint event revision compare-and-swap failed"
                    )
                planned = self._read_event(connection, key, AdaptiveActionPhase.PLANNED)
                if planned is None:
                    raise AdaptiveCheckpointCasError(
                        "planned checkpoint is required before observation"
                    )
                if (
                    phase is AdaptiveActionPhase.TERMINAL
                    and self._read_event(connection, key, AdaptiveActionPhase.OBSERVED)
                    is None
                ):
                    raise AdaptiveCheckpointCasError(
                        "observed checkpoint is required before terminal checkpoint"
                    )
                planned_artifact = planned["artifact_digest"]
                if planned_artifact is not None and artifact_digest != planned_artifact:
                    raise AdaptiveArtifactDigestMismatchError(
                        "observed artifact digest does not match planned artifact"
                    )

            created_at_ns = time.time_ns()
            if type(created_at_ns) is not int or created_at_ns < 0:
                raise ValueError("created_at_ns must be a non-negative integer")
            connection.execute(
                """
                INSERT INTO adaptive_checkpoint_events (
                    run_id, run_incarnation, loop_kind, revision, phase, action_json,
                    action_digest, artifact_digest, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key.run_id,
                    key.run_incarnation,
                    key.loop_kind.value,
                    key.revision,
                    phase.value,
                    action_json,
                    action_digest,
                    artifact_digest,
                    created_at_ns,
                ),
            )
            if replay_input is not None:
                assert replay_bytes is not None
                assert replay_digest is not None
                self._insert_checkpoint_replay_input(
                    connection,
                    key,
                    replay_input,
                    replay_bytes,
                    replay_digest,
                    created_at_ns,
                )
            if phase is AdaptiveActionPhase.PLANNED:
                connection.execute(
                    """
                    INSERT INTO adaptive_checkpoint_heads (run_id, run_incarnation, loop_kind, revision)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(run_id, run_incarnation, loop_kind) DO UPDATE SET revision = excluded.revision
                    """,
                    (
                        key.run_id,
                        key.run_incarnation,
                        key.loop_kind.value,
                        key.revision,
                    ),
                )
            return AdaptiveCheckpointEvent(
                key=key,
                phase=phase,
                action=json.loads(action_json),
                action_digest=action_digest,
                artifact_digest=artifact_digest,
                created_at_ns=created_at_ns,
            )

    @staticmethod
    def _read_checkpoint_replay_input(
        connection: sqlite3.Connection,
        key: AdaptiveCheckpointKey,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT run_id, run_incarnation, loop_kind, revision, phase,
                   input_kind, input_version, input_bytes, input_digest,
                   created_at_ns
            FROM adaptive_checkpoint_replay_inputs
            WHERE run_id = ? AND run_incarnation = ? AND loop_kind = ?
              AND revision = ? AND phase = 'terminal'
            """,
            (
                key.run_id,
                key.run_incarnation,
                key.loop_kind.value,
                key.revision,
            ),
        ).fetchone()

    @staticmethod
    def _insert_checkpoint_replay_input(
        connection: sqlite3.Connection,
        key: AdaptiveCheckpointKey,
        replay_input: ResearchTerminalReplayInput,
        input_bytes: bytes,
        input_digest: str,
        created_at_ns: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO adaptive_checkpoint_replay_inputs (
                run_id, run_incarnation, loop_kind, revision, phase,
                input_kind, input_version, input_bytes, input_digest,
                created_at_ns
            ) VALUES (?, ?, 'research', ?, 'terminal', ?, ?, ?, ?, ?)
            """,
            (
                key.run_id,
                key.run_incarnation,
                key.revision,
                replay_input.input_kind,
                replay_input.input_version,
                input_bytes,
                input_digest,
                created_at_ns,
            ),
        )

    @staticmethod
    def _decode_checkpoint_replay_input(
        row: sqlite3.Row,
    ) -> ResearchTerminalReplayInput:
        input_bytes = row["input_bytes"]
        input_digest = row["input_digest"]
        if (
            not isinstance(input_bytes, bytes)
            or not isinstance(input_digest, str)
            or type(row["revision"]) is not int
            or type(row["input_version"]) is not int
            or type(row["created_at_ns"]) is not int
        ):
            raise AdaptiveCheckpointIntegrityError(
                "terminal replay input storage types are invalid"
            )
        expected_digest = f"sha256:{hashlib.sha256(input_bytes).hexdigest()}"
        if not hmac.compare_digest(input_digest, expected_digest):
            raise AdaptiveCheckpointIntegrityError(
                "terminal replay input digest does not match"
            )
        try:
            decoded = deserialize_replay_input(input_bytes)
        except ReplayInputError as exc:
            raise AdaptiveCheckpointIntegrityError(
                "terminal replay input payload is invalid"
            ) from exc
        if (
            type(decoded) is not ResearchTerminalReplayInput
            or row["loop_kind"] != "research"
            or row["phase"] != "terminal"
            or row["input_kind"] != decoded.input_kind
            or row["input_version"] != decoded.input_version
            or decoded.freshness_context.run_id != row["run_id"]
            or decoded.freshness_context.run_incarnation != row["run_incarnation"]
        ):
            raise AdaptiveCheckpointIntegrityError(
                "terminal replay input identity is invalid"
            )
        return decoded

    @staticmethod
    def _decode_solver_checkpoint_replay_input(row: sqlite3.Row) -> object:
        input_bytes = row["input_bytes"]
        input_digest = row["input_digest"]
        if (
            not isinstance(input_bytes, bytes)
            or not isinstance(input_digest, str)
            or type(row["action_revision"]) is not int
            or type(row["input_version"]) is not int
            or type(row["created_at_ns"]) is not int
        ):
            raise AdaptiveCheckpointIntegrityError(
                "solver replay input storage types are invalid"
            )
        expected_digest = f"sha256:{hashlib.sha256(input_bytes).hexdigest()}"
        if not hmac.compare_digest(input_digest, expected_digest):
            raise AdaptiveCheckpointIntegrityError(
                "solver replay input digest does not match"
            )
        try:
            decoded = deserialize_replay_input(input_bytes)
        except ReplayInputError as exc:
            raise AdaptiveCheckpointIntegrityError(
                "solver replay input payload is invalid"
            ) from exc
        if (
            type(decoded) not in _SOLVER_REPLAY_INPUT_TYPES
            or row["input_kind"] != decoded.input_kind
            or row["input_version"] != decoded.input_version
        ):
            raise AdaptiveCheckpointIntegrityError(
                "solver replay input kind or version is invalid"
            )
        if type(decoded) is SolverSqlProposalReplayInput and (
            decoded.requirements.run_id != row["run_id"]
            or decoded.requirements.run_incarnation != row["run_incarnation"]
        ):
            raise AdaptiveCheckpointIntegrityError(
                "solver replay input identity is invalid"
            )
        return decoded

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = self._read_schema_version(connection)
            if version is None:
                self._prepare_legacy_schema(connection)
                self._migrate_v0_to_v1(connection)
                version = 1
            elif version > ADAPTIVE_STATE_STORE_SCHEMA_VERSION:
                raise AdaptiveCheckpointMigrationError(
                    "adaptive checkpoint schema is newer than this application"
                )
            elif version == 0:
                self._validate_checkpoint_tables(connection)
                self._migrate_v0_to_v1(connection)
                version = 1
            elif version < 0:
                raise AdaptiveCheckpointMigrationError(
                    "adaptive checkpoint schema version is not supported"
                )
            if version == 1:
                self._validate_v1_schema(connection)
                self._migrate_v1_to_v2(connection)
                version = 2
            if version == 2:
                self._migrate_v2_to_v3(connection)
                version = 3
            elif version != ADAPTIVE_STATE_STORE_SCHEMA_VERSION:
                raise AdaptiveCheckpointMigrationError(
                    "adaptive checkpoint schema version is not supported"
                )
            self._validate_current_schema(connection)
        self._secure_sidecars()

    @staticmethod
    def _create_checkpoint_tables(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS adaptive_checkpoint_events (
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                loop_kind TEXT NOT NULL CHECK (loop_kind IN ('research', 'solver')),
                revision INTEGER NOT NULL CHECK (revision >= 0),
                phase TEXT NOT NULL CHECK (phase IN ('planned', 'observed', 'terminal')),
                action_json TEXT NOT NULL,
                action_digest TEXT NOT NULL,
                artifact_digest TEXT,
                created_at_ns INTEGER NOT NULL CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
                PRIMARY KEY (run_id, run_incarnation, loop_kind, revision, phase)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS adaptive_checkpoint_heads (
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                loop_kind TEXT NOT NULL CHECK (loop_kind IN ('research', 'solver')),
                revision INTEGER NOT NULL CHECK (revision >= 0),
                PRIMARY KEY (run_id, run_incarnation, loop_kind)
            )
            """
        )
        AdaptiveStateStore._restore_canonical_triggers(connection)

    def _prepare_legacy_schema(self, connection: sqlite3.Connection) -> None:
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        legacy_tables = {
            "adaptive_checkpoint_events",
            "adaptive_checkpoint_heads",
        }
        found = names.intersection(legacy_tables)
        if found and found != legacy_tables:
            raise AdaptiveCheckpointMigrationError(
                "adaptive checkpoint legacy schema is incomplete"
            )
        if not found:
            self._create_checkpoint_tables(connection)
        else:
            self._validate_checkpoint_tables(connection)

    @staticmethod
    def _read_schema_version(connection: sqlite3.Connection) -> int | None:
        meta_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_SCHEMA_META_TABLE,),
        ).fetchone()
        if meta_exists is None:
            return None
        row = connection.execute(
            f"SELECT value FROM {_SCHEMA_META_TABLE} WHERE key = ?",
            (_SCHEMA_VERSION_KEY,),
        ).fetchone()
        if row is None or type(row["value"]) is not int:
            raise AdaptiveCheckpointMigrationError(
                "adaptive checkpoint schema version is invalid"
            )
        return int(row["value"])

    @staticmethod
    def _validate_checkpoint_tables(connection: sqlite3.Connection) -> None:
        expected_columns = {
            "adaptive_checkpoint_events": {
                "run_id",
                "run_incarnation",
                "loop_kind",
                "revision",
                "phase",
                "action_json",
                "action_digest",
                "artifact_digest",
                "created_at_ns",
            },
            "adaptive_checkpoint_heads": {
                "run_id",
                "run_incarnation",
                "loop_kind",
                "revision",
            },
        }
        for table_name, expected in expected_columns.items():
            columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            if columns != expected:
                raise AdaptiveCheckpointMigrationError(
                    f"adaptive checkpoint table {table_name} is incompatible"
                )

    @staticmethod
    def _restore_canonical_triggers(connection: sqlite3.Connection) -> None:
        for trigger_name in _CHECKPOINT_TRIGGER_SQL:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        for trigger_sql in _CHECKPOINT_TRIGGER_SQL.values():
            connection.execute(trigger_sql)

    @staticmethod
    def _validate_checkpoint_triggers(connection: sqlite3.Connection) -> None:
        rows = tuple(
            connection.execute(
                """
                SELECT name, tbl_name, sql FROM sqlite_master
                WHERE type = 'trigger' AND tbl_name = 'adaptive_checkpoint_events'
                """
            )
        )
        by_name = {row["name"]: row for row in rows}
        if set(by_name) != set(_CHECKPOINT_TRIGGER_SQL):
            raise AdaptiveCheckpointMigrationError(
                "adaptive checkpoint triggers are incomplete or incompatible"
            )
        for trigger_name, trigger_sql in _CHECKPOINT_TRIGGER_SQL.items():
            row = by_name[trigger_name]
            if row["tbl_name"] != "adaptive_checkpoint_events" or (
                _normalize_schema_sql(row["sql"]) != _normalize_schema_sql(trigger_sql)
            ):
                raise AdaptiveCheckpointMigrationError(
                    f"adaptive checkpoint trigger {trigger_name} is incompatible"
                )

    @staticmethod
    def _migrate_v0_to_v1(connection: sqlite3.Connection) -> None:
        AdaptiveStateStore._restore_canonical_triggers(connection)
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_SCHEMA_META_TABLE} (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            INSERT INTO {_SCHEMA_META_TABLE} (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_SCHEMA_VERSION_KEY, 1),
        )

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        if solver_checkpoint_v2_schema_signature(connection):
            raise AdaptiveCheckpointMigrationError(
                "adaptive solver checkpoint v2 schema is partial or conflicting"
            )
        create_solver_checkpoint_v2_schema(connection)
        cursor = connection.execute(
            f"UPDATE {_SCHEMA_META_TABLE} SET value = ? WHERE key = ? AND value = 1",
            (2, _SCHEMA_VERSION_KEY),
        )
        if cursor.rowcount != 1:
            raise AdaptiveCheckpointMigrationError(
                "adaptive checkpoint v1 schema version changed during migration"
            )

    def _migrate_v2_to_v3(self, connection: sqlite3.Connection) -> None:
        self._validate_generic_schema(connection, expected_version=2)
        if (
            solver_checkpoint_v2_schema_signature(connection)
            != canonical_solver_checkpoint_v2_schema_signature()
        ):
            raise AdaptiveCheckpointMigrationError(
                "adaptive solver checkpoint v2 schema is incomplete or incompatible"
            )
        if _checkpoint_replay_schema_signature(connection):
            raise AdaptiveCheckpointMigrationError(
                "adaptive checkpoint replay input schema is partial or conflicting"
            )
        for statement in _CHECKPOINT_REPLAY_INPUT_SQL:
            connection.execute(statement)
        create_solver_checkpoint_v3_additions(connection)
        cursor = connection.execute(
            f"UPDATE {_SCHEMA_META_TABLE} SET value = 3 WHERE key = ? AND value = 2",
            (_SCHEMA_VERSION_KEY,),
        )
        if cursor.rowcount != 1:
            raise AdaptiveCheckpointMigrationError(
                "adaptive checkpoint v2 schema version changed during migration"
            )

    def _validate_generic_schema(
        self,
        connection: sqlite3.Connection,
        *,
        expected_version: int,
    ) -> None:
        self._validate_checkpoint_tables(connection)
        self._validate_checkpoint_triggers(connection)
        version = self._read_schema_version(connection)
        if version != expected_version:
            raise AdaptiveCheckpointMigrationError(
                "adaptive checkpoint schema version does not match its structure"
            )
        for row in connection.execute(
            "SELECT action_json, action_digest FROM adaptive_checkpoint_events"
        ):
            _validated_stored_action(
                row,
                error_type=AdaptiveCheckpointMigrationError,
            )

    def _validate_v1_schema(self, connection: sqlite3.Connection) -> None:
        self._validate_generic_schema(connection, expected_version=1)

    def _validate_current_schema(self, connection: sqlite3.Connection) -> None:
        self._validate_generic_schema(
            connection,
            expected_version=ADAPTIVE_STATE_STORE_SCHEMA_VERSION,
        )
        if (
            solver_checkpoint_schema_signature(connection)
            != canonical_solver_checkpoint_schema_signature()
        ):
            raise AdaptiveCheckpointMigrationError(
                "adaptive solver checkpoint v3 schema is incomplete or incompatible"
            )
        if (
            _checkpoint_replay_schema_signature(connection)
            != _canonical_checkpoint_replay_schema_signature()
        ):
            raise AdaptiveCheckpointMigrationError(
                "adaptive checkpoint replay input schema is incomplete or incompatible"
            )
        for row in connection.execute(
            """
            SELECT run_id, run_incarnation, loop_kind, revision, phase,
                   input_kind, input_version, input_bytes, input_digest,
                   created_at_ns
            FROM adaptive_checkpoint_replay_inputs
            """
        ):
            try:
                self._decode_checkpoint_replay_input(row)
            except AdaptiveCheckpointIntegrityError as exc:
                raise AdaptiveCheckpointMigrationError(
                    "adaptive checkpoint replay input rows are incompatible"
                ) from exc
        for row in connection.execute(
            """
            SELECT run_id, run_incarnation, action_revision, input_kind,
                   input_version, input_bytes, input_digest, created_at_ns
            FROM adaptive_solver_checkpoint_replay_inputs
            """
        ):
            try:
                self._decode_solver_checkpoint_replay_input(row)
            except AdaptiveCheckpointIntegrityError as exc:
                raise AdaptiveCheckpointMigrationError(
                    "adaptive solver replay input rows are incompatible"
                ) from exc

    def _connect(self) -> sqlite3.Connection:
        target = self._memory_uri or self.db_path
        connection = sqlite3.connect(
            target,
            timeout=30,
            isolation_level=None,
            uri=self._memory_uri is not None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
        except BaseException:
            try:
                connection.close()
            except BaseException:
                pass
            raise
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open one transaction-scoped connection and close it deterministically."""
        with self._lifecycle_lock:
            if self._closed:
                raise AdaptiveCheckpointError("adaptive checkpoint store is closed")
            connection = self._connect()
        changes_before = connection.total_changes
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            try:
                if (
                    connection.total_changes != changes_before
                    and self._commit_fence is not None
                ):
                    self._commit_fence.require_open_in_transaction(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()

    def _secure_sidecars(self) -> None:
        if self.db_path != ":memory:":
            secure_sqlite_sidecars(self.db_path, tighten_existing=True)

    @staticmethod
    def _read_event(
        connection: sqlite3.Connection,
        key: AdaptiveCheckpointKey,
        phase: AdaptiveActionPhase,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            """
            SELECT action_json, action_digest, artifact_digest, created_at_ns
            FROM adaptive_checkpoint_events
            WHERE run_id = ? AND run_incarnation = ? AND loop_kind = ? AND revision = ? AND phase = ?
            """,
            (
                key.run_id,
                key.run_incarnation,
                key.loop_kind.value,
                key.revision,
                phase.value,
            ),
        ).fetchone()
        if row is not None:
            _validated_stored_action(row)
        return row

    @staticmethod
    def _current_revision(
        connection: sqlite3.Connection, key: AdaptiveCheckpointKey
    ) -> int:
        row = connection.execute(
            """
            SELECT revision FROM adaptive_checkpoint_heads
            WHERE run_id = ? AND run_incarnation = ? AND loop_kind = ?
            """,
            (key.run_id, key.run_incarnation, key.loop_kind.value),
        ).fetchone()
        return -1 if row is None else int(row["revision"])

    @staticmethod
    def _terminal_revision(
        connection: sqlite3.Connection, key: AdaptiveCheckpointKey
    ) -> int | None:
        row = connection.execute(
            """
            SELECT revision, action_json, action_digest FROM adaptive_checkpoint_events
            WHERE run_id = ? AND run_incarnation = ? AND loop_kind = ? AND phase = 'terminal'
            LIMIT 1
            """,
            (key.run_id, key.run_incarnation, key.loop_kind.value),
        ).fetchone()
        if row is None:
            return None
        _validated_stored_action(row)
        return int(row["revision"])

    @staticmethod
    def _event_from_row(
        key: AdaptiveCheckpointKey,
        phase: AdaptiveActionPhase,
        row: sqlite3.Row,
    ) -> AdaptiveCheckpointEvent:
        action = _validated_stored_action(row)
        return AdaptiveCheckpointEvent(
            key=key,
            phase=phase,
            action=action,
            action_digest=row["action_digest"],
            artifact_digest=row["artifact_digest"],
            created_at_ns=row["created_at_ns"],
        )


def _canonical_action_json(action: Any) -> str:
    try:
        normalized = _strict_json_value(action)
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("action must be canonical JSON") from exc


def _validate_complete_event_chain(
    phases_by_revision: Mapping[int, list[AdaptiveActionPhase]],
) -> None:
    if not phases_by_revision:
        return
    revisions = tuple(phases_by_revision)
    if revisions != tuple(range(len(revisions))):
        raise ValueError("checkpoint revisions must be contiguous from zero")
    for revision, phases in phases_by_revision.items():
        is_last = revision == revisions[-1]
        if phases == [AdaptiveActionPhase.TERMINAL]:
            if not is_last:
                raise ValueError("terminal checkpoint must be final")
            continue
        allowed = [AdaptiveActionPhase.PLANNED]
        if not is_last:
            allowed.append(AdaptiveActionPhase.OBSERVED)
        if phases == allowed:
            continue
        if is_last and phases in (
            [AdaptiveActionPhase.PLANNED, AdaptiveActionPhase.OBSERVED],
            [
                AdaptiveActionPhase.PLANNED,
                AdaptiveActionPhase.OBSERVED,
                AdaptiveActionPhase.TERMINAL,
            ],
        ):
            continue
        raise ValueError("checkpoint phases are incomplete or out of order")


def _strict_json_value(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be exact strings")
            normalized[key] = _strict_json_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_strict_json_value(item) for item in value]
    raise ValueError("unsupported JSON value type")


def _validated_stored_action(
    row: sqlite3.Row,
    *,
    error_type: type[AdaptiveCheckpointError] = AdaptiveCheckpointIntegrityError,
) -> Any:
    action_json = row["action_json"]
    action_digest = row["action_digest"]
    try:
        if type(action_json) is not str or type(action_digest) is not str:
            raise ValueError
        if not _SHA256_RE.fullmatch(action_digest):
            raise ValueError
        action = json.loads(action_json)
        if _canonical_action_json(action) != action_json:
            raise ValueError
        expected_digest = (
            f"sha256:{hashlib.sha256(action_json.encode('utf-8')).hexdigest()}"
        )
        if action_digest != expected_digest:
            raise ValueError
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise error_type("adaptive checkpoint event action digest is invalid") from exc
    return action


def _validate_artifact_digest(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
    ):
        raise ValueError("artifact_digest must be exact sha256 lowercase hex")
