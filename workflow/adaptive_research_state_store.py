"""Durable typed snapshots for adaptive Text-to-SQL state.

This namespace is deliberately separate from the adaptive action journal.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Iterator, TypeVar
import uuid

from custom_tools.text_to_sql.adaptive.models import (
    QuerySpec,
    ResearchActionKind,
    ResearchState,
)
from custom_tools.text_to_sql.adaptive.replay_inputs import (
    ResearchSemanticReplayInput,
    ReplayInputError,
    deserialize_replay_input,
    serialize_replay_input,
)
from custom_tools.text_to_sql.adaptive.semantic_reducer import (
    SemanticReducerError,
    SemanticTurnAdmission,
    admit_semantic_turn,
    commit_semantic_turn,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    AdaptiveSerializationError,
    SerializationLimits,
    canonical_digest,
    canonical_json_bytes,
    deserialize_as,
    serialize_contract,
)

from ._text_to_sql_reentry_recovery import PreparedTargetedReentryCommit

from .sqlite_schema_signature import (
    SQLiteSchemaSignatureError,
    owned_sqlite_schema_signature,
)
from .state_files import prepare_sqlite_file, secure_sqlite_sidecars


class AdaptiveResearchStateStoreError(RuntimeError):
    """Base error for the closed typed snapshot store."""


class AdaptiveResearchStateStoreMigrationError(AdaptiveResearchStateStoreError):
    """The typed snapshot schema cannot be safely opened."""


class AdaptiveResearchStateStoreCasError(AdaptiveResearchStateStoreError):
    """The requested ResearchState predecessor is no longer current."""


class AdaptiveResearchStateStoreConflictError(AdaptiveResearchStateStoreError):
    """An existing snapshot identity has different immutable bytes."""


class AdaptiveResearchStateStoreCorruptionError(AdaptiveResearchStateStoreError):
    """Stored bytes, digest, or identity do not form a valid snapshot."""


_Model = TypeVar("_Model", QuerySpec, ResearchState)
ADAPTIVE_RESEARCH_STATE_STORE_SCHEMA_VERSION = 3
_META_TABLE = "adaptive_research_state_meta"
_SNAPSHOT_TABLE = "adaptive_research_state_snapshots"
_PREPARED_TABLE = "adaptive_research_state_prepared_reentries"
_PREPARED_COMMIT_TABLE = "adaptive_research_state_prepared_reentry_commits"
_REPLAY_INPUT_TABLE = "adaptive_research_replay_inputs"
_OWNED_SCHEMA_PREFIX = "adaptive_research_state_"
_VERSION_KEY = "schema_version"
_PREPARED_LIMITS = SerializationLimits(
    max_state_bytes=3 * 1024 * 1024,
    max_inline_rows=0,
)

_SNAPSHOT_TABLE_SQL = f"""
    CREATE TABLE {_SNAPSHOT_TABLE} (
        run_id TEXT NOT NULL,
        run_incarnation TEXT NOT NULL,
        contract_name TEXT NOT NULL CHECK (contract_name IN ('query_spec', 'research_state')),
        revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 0),
        payload BLOB NOT NULL CHECK (typeof(payload) = 'blob'),
        digest TEXT NOT NULL,
        created_at_ns INTEGER NOT NULL CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, contract_name, revision)
    )
"""
_META_TABLE_SQL = f"""
    CREATE TABLE {_META_TABLE} (
        key TEXT PRIMARY KEY,
        value INTEGER NOT NULL
    )
"""
_PREPARED_TABLE_SQL = f"""
    CREATE TABLE {_PREPARED_TABLE} (
        run_id TEXT NOT NULL CHECK (length(run_id) > 0),
        run_incarnation TEXT NOT NULL CHECK (length(run_incarnation) > 0),
        research_reentry_id TEXT NOT NULL CHECK (length(research_reentry_id) > 0),
        missing_evidence_request_id TEXT NOT NULL CHECK (length(missing_evidence_request_id) > 0),
        source_id TEXT NOT NULL CHECK (length(source_id) > 0),
        ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal BETWEEN 1 AND 3),
        base_solver_revision INTEGER NOT NULL CHECK (typeof(base_solver_revision) = 'integer' AND base_solver_revision >= 0),
        base_contract_name TEXT NOT NULL CHECK (base_contract_name = 'research_state'),
        base_research_revision INTEGER NOT NULL CHECK (typeof(base_research_revision) = 'integer' AND base_research_revision >= 0),
        payload BLOB NOT NULL CHECK (typeof(payload) = 'blob' AND length(payload) <= 3145728),
        payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 71 AND substr(payload_digest, 1, 7) = 'sha256:'),
        plan_digest TEXT NOT NULL CHECK (length(plan_digest) = 71 AND substr(plan_digest, 1, 7) = 'sha256:'),
        created_at_ns INTEGER NOT NULL CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, research_reentry_id),
        UNIQUE (run_id, run_incarnation, missing_evidence_request_id, ordinal),
        FOREIGN KEY (run_id, run_incarnation, base_contract_name, base_research_revision)
            REFERENCES {_SNAPSHOT_TABLE} (run_id, run_incarnation, contract_name, revision)
    )
"""
_PREPARED_COMMIT_TABLE_SQL = f"""
    CREATE TABLE {_PREPARED_COMMIT_TABLE} (
        run_id TEXT NOT NULL CHECK (length(run_id) > 0),
        run_incarnation TEXT NOT NULL CHECK (length(run_incarnation) > 0),
        research_reentry_id TEXT NOT NULL CHECK (length(research_reentry_id) > 0),
        successor_contract_name TEXT NOT NULL CHECK (successor_contract_name = 'research_state'),
        successor_revision INTEGER NOT NULL CHECK (typeof(successor_revision) = 'integer' AND successor_revision >= 0),
        successor_digest TEXT NOT NULL CHECK (length(successor_digest) = 71 AND substr(successor_digest, 1, 7) = 'sha256:'),
        created_at_ns INTEGER NOT NULL CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, research_reentry_id),
        FOREIGN KEY (run_id, run_incarnation, research_reentry_id)
            REFERENCES {_PREPARED_TABLE} (run_id, run_incarnation, research_reentry_id),
        FOREIGN KEY (run_id, run_incarnation, successor_contract_name, successor_revision)
            REFERENCES {_SNAPSHOT_TABLE} (run_id, run_incarnation, contract_name, revision)
    )
"""
_PREPARED_NO_UPDATE_SQL = f"""
    CREATE TRIGGER {_PREPARED_TABLE}_no_update
    BEFORE UPDATE ON {_PREPARED_TABLE}
    BEGIN SELECT RAISE(ABORT, 'prepared re-entry rows are immutable'); END
"""
_PREPARED_NO_DELETE_SQL = f"""
    CREATE TRIGGER {_PREPARED_TABLE}_no_delete
    BEFORE DELETE ON {_PREPARED_TABLE}
    BEGIN SELECT RAISE(ABORT, 'prepared re-entry rows are append-only'); END
"""
_PREPARED_COMMIT_NO_UPDATE_SQL = f"""
    CREATE TRIGGER {_PREPARED_COMMIT_TABLE}_no_update
    BEFORE UPDATE ON {_PREPARED_COMMIT_TABLE}
    BEGIN SELECT RAISE(ABORT, 'prepared re-entry commits are immutable'); END
"""
_PREPARED_COMMIT_NO_DELETE_SQL = f"""
    CREATE TRIGGER {_PREPARED_COMMIT_TABLE}_no_delete
    BEFORE DELETE ON {_PREPARED_COMMIT_TABLE}
    BEGIN SELECT RAISE(ABORT, 'prepared re-entry commits are append-only'); END
"""
_REPLAY_INPUT_TABLE_SQL = f"""
    CREATE TABLE {_REPLAY_INPUT_TABLE} (
        run_id TEXT NOT NULL CHECK (length(run_id) > 0),
        run_incarnation TEXT NOT NULL CHECK (length(run_incarnation) > 0),
        successor_contract_name TEXT NOT NULL
            CHECK (successor_contract_name = 'research_state'),
        successor_revision INTEGER NOT NULL
            CHECK (typeof(successor_revision) = 'integer' AND successor_revision >= 0),
        input_kind TEXT NOT NULL CHECK (input_kind = 'research_semantic'),
        input_version INTEGER NOT NULL
            CHECK (typeof(input_version) = 'integer' AND input_version = 1),
        input_bytes BLOB NOT NULL CHECK (typeof(input_bytes) = 'blob'),
        input_digest TEXT NOT NULL
            CHECK (length(input_digest) = 71 AND substr(input_digest, 1, 7) = 'sha256:' AND
                   substr(input_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        created_at_ns INTEGER NOT NULL
            CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, successor_revision),
        FOREIGN KEY (
            run_id,
            run_incarnation,
            successor_contract_name,
            successor_revision
        ) REFERENCES {_SNAPSHOT_TABLE} (
            run_id,
            run_incarnation,
            contract_name,
            revision
        )
    )
"""
_REPLAY_INPUT_NO_UPDATE_SQL = f"""
    CREATE TRIGGER {_REPLAY_INPUT_TABLE}_no_update
    BEFORE UPDATE ON {_REPLAY_INPUT_TABLE}
    BEGIN SELECT RAISE(ABORT, 'research replay inputs are immutable'); END
"""
_REPLAY_INPUT_NO_DELETE_SQL = f"""
    CREATE TRIGGER {_REPLAY_INPUT_TABLE}_no_delete
    BEFORE DELETE ON {_REPLAY_INPUT_TABLE}
    BEGIN SELECT RAISE(ABORT, 'research replay inputs are immutable'); END
"""
_V1_OWNED_TABLE_SQL = (_SNAPSHOT_TABLE_SQL, _META_TABLE_SQL)
_V2_ADDITIONAL_SQL = (
    _PREPARED_TABLE_SQL,
    _PREPARED_COMMIT_TABLE_SQL,
    _PREPARED_NO_UPDATE_SQL,
    _PREPARED_NO_DELETE_SQL,
    _PREPARED_COMMIT_NO_UPDATE_SQL,
    _PREPARED_COMMIT_NO_DELETE_SQL,
)
_V2_OWNED_TABLE_SQL = (*_V1_OWNED_TABLE_SQL, *_V2_ADDITIONAL_SQL)
_V3_ADDITIONAL_SQL = (
    _REPLAY_INPUT_TABLE_SQL,
    _REPLAY_INPUT_NO_UPDATE_SQL,
    _REPLAY_INPUT_NO_DELETE_SQL,
)
_OWNED_TABLE_SQL = (*_V2_OWNED_TABLE_SQL, *_V3_ADDITIONAL_SQL)


def _owned_schema_signature(
    connection: sqlite3.Connection,
    table_names: tuple[str, ...] = (
        _SNAPSHOT_TABLE,
        _META_TABLE,
        _PREPARED_TABLE,
        _PREPARED_COMMIT_TABLE,
        _REPLAY_INPUT_TABLE,
    ),
) -> tuple[tuple[str, str, str, tuple[tuple[str, str], ...] | None], ...]:
    try:
        return owned_sqlite_schema_signature(
            connection,
            prefix=_OWNED_SCHEMA_PREFIX,
            table_names=table_names,
        )
    except SQLiteSchemaSignatureError as exc:
        raise AdaptiveResearchStateStoreMigrationError(
            "typed snapshot schema definition is invalid"
        ) from exc


def _canonical_owned_schema_signature(
    statements: tuple[str, ...] = _OWNED_TABLE_SQL,
    table_names: tuple[str, ...] = (
        _SNAPSHOT_TABLE,
        _META_TABLE,
        _PREPARED_TABLE,
        _PREPARED_COMMIT_TABLE,
        _REPLAY_INPUT_TABLE,
    ),
) -> tuple[tuple[str, str, str, tuple[tuple[str, str], ...] | None], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            connection.execute(statement)
        return _owned_schema_signature(connection, table_names)
    finally:
        connection.close()


def _require_commit_guard(commit_guard: object | None) -> None:
    if commit_guard is not None:
        commit_guard.require_active()


def _commit_with_guard(
    connection: sqlite3.Connection,
    commit_guard: object | None,
    *,
    contract_name: str,
) -> None:
    if commit_guard is None:
        return
    commit_guard.commit(connection, contract_name=contract_name)


def _validated_research_state(value: ResearchState, label: str) -> ResearchState:
    if type(value) is not ResearchState:
        raise TypeError(f"{label} must be ResearchState")
    return ResearchState.model_validate(
        value.model_dump(mode="python", round_trip=True, warnings="error")
    )


def _require_exact_semantic_reduction(
    previous: ResearchState,
    successor: ResearchState,
    replay_input: ResearchSemanticReplayInput,
) -> SemanticTurnAdmission:
    try:
        admission = admit_semantic_turn(
            previous,
            replay_input.decision,
            batch=replay_input.semantic_batch,
            freshness_context=replay_input.freshness_context,
            tool_claim=replay_input.tool_claim,
            budget_state=replay_input.budget_state,
        )
        replayed = commit_semantic_turn(
            admission,
            probe_result=replay_input.probe_result,
        )
    except (SemanticReducerError, TypeError, ValueError) as exc:
        raise AdaptiveResearchStateStoreConflictError(
            "research replay input cannot reproduce the successor"
        ) from exc
    if replayed.state != successor:
        raise AdaptiveResearchStateStoreConflictError(
            "research replay input does not reproduce the successor"
        )
    return admission


def _bytes_digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _require_linked_semantic_journal(
    connection: sqlite3.Connection,
    previous: ResearchState,
    replay_input: ResearchSemanticReplayInput,
    admission: SemanticTurnAdmission,
) -> None:
    try:
        rows = connection.execute(
            """
            SELECT phase, action_json, action_digest
            FROM adaptive_checkpoint_events
            WHERE run_id = ? AND run_incarnation = ? AND loop_kind = 'research'
              AND revision = ? AND phase IN ('planned', 'observed')
            """,
            (previous.run_id, previous.run_incarnation, previous.revision),
        ).fetchall()
    except sqlite3.Error as exc:
        raise AdaptiveResearchStateStoreConflictError(
            "research semantic transition requires its durable journal"
        ) from exc
    by_phase = {row["phase"]: row for row in rows}
    if len(rows) != 2 or set(by_phase) != {"planned", "observed"}:
        raise AdaptiveResearchStateStoreConflictError(
            "research semantic transition requires planned and observed journal events"
        )

    actions: dict[str, dict[str, object]] = {}
    for phase, expected_digest in (
        ("planned", replay_input.planned_action_digest),
        ("observed", replay_input.observed_action_digest),
    ):
        row = by_phase[phase]
        action_json = row["action_json"]
        action_digest = row["action_digest"]
        if (
            not isinstance(action_json, str)
            or not isinstance(action_digest, str)
            or _bytes_digest(action_json.encode("utf-8")) != action_digest
            or action_digest != expected_digest
        ):
            raise AdaptiveResearchStateStoreConflictError(
                "research replay input does not match durable journal digests"
            )
        try:
            action = json.loads(action_json)
        except json.JSONDecodeError as exc:
            raise AdaptiveResearchStateStoreConflictError(
                "research semantic journal action is invalid"
            ) from exc
        if (
            not isinstance(action, dict)
            or canonical_json_bytes(action).decode("utf-8") != action_json
        ):
            raise AdaptiveResearchStateStoreConflictError(
                "research semantic journal action is not canonical"
            )
        actions[phase] = action

    planned = actions["planned"]
    observed = actions["observed"]
    action = admission.action
    probe = replay_input.probe_result
    resolution_digest = planned.get("resolution_digest")
    state_digest = planned.get("state_digest")
    novel = observed.get("novel")
    if (
        action is None
        or not isinstance(resolution_digest, str)
        or not isinstance(state_digest, str)
        or type(novel) is not bool
    ):
        raise AdaptiveResearchStateStoreConflictError(
            "research semantic journal has no admitted action"
        )
    semantic_only = action.kind is ResearchActionKind.SEMANTIC_COMMIT
    if semantic_only != (probe is None and replay_input.tool_claim is None):
        raise AdaptiveResearchStateStoreConflictError(
            "research semantic journal has invalid semantic-only linkage"
        )
    expected_planned = {
        "action": action.model_dump(mode="json", by_alias=True),
        "contract_version": 1,
        "decision": replay_input.decision.model_dump(mode="json", by_alias=True),
        "invocation_id": None if probe is None else probe.invocation_id,
        "kind": "research_planned",
        "resolution_digest": resolution_digest,
        "state_digest": state_digest,
    }
    expected_observed = {
        "contract_version": 1,
        "kind": "research_observed",
        "novel": novel,
        "result": None if probe is None else probe.model_dump(mode="json", by_alias=True),
        "resolution_digest": resolution_digest,
    }
    if planned != expected_planned or observed != expected_observed:
        raise AdaptiveResearchStateStoreConflictError(
            "research replay input does not match durable journal actions"
        )
    if probe is not None and (
        probe.run_id != previous.run_id
        or probe.run_incarnation != previous.run_incarnation
        or probe.revision != previous.revision
        or probe.schema_namespace_version != previous.schema_namespace_version
        or probe.action_digest != action.action_digest
        or probe.probe_kind is not action.kind
        or probe.target != action.target
    ):
        raise AdaptiveResearchStateStoreConflictError(
            "research replay probe identity does not match durable journal action"
        )


class AdaptiveResearchStateStore:
    """SQLite-backed canonical QuerySpec and ResearchState snapshots."""

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
            self._memory_uri = f"file:adaptive-research-state-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._memory_anchor = sqlite3.connect(self._memory_uri, uri=True)
        else:
            prepare_sqlite_file(self.db_path, tighten_existing=True)
        try:
            self._migrate()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release the shared in-memory database anchor once."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            anchor = self._memory_anchor
            self._memory_anchor = None
        if anchor is not None:
            anchor.close()

    def save_query_spec(
        self,
        query_spec: QuerySpec,
        *,
        commit_guard: object | None = None,
    ) -> QuerySpec:
        """Save one immutable QuerySpec revision, accepting an identical retry."""
        return self._save(
            query_spec,
            expected_previous_revision=None,
            commit_guard=commit_guard,
        )

    def load_query_spec(
        self,
        run_id: str,
        run_incarnation: str,
        *,
        revision: int | None = None,
    ) -> QuerySpec | None:
        """Load the requested QuerySpec revision, or the latest when omitted."""
        return self._load(
            QuerySpec,
            run_id,
            run_incarnation,
            revision=revision,
        )

    def load_query_spec_chain(
        self,
        run_id: str,
        run_incarnation: str,
    ) -> tuple[QuerySpec, ...]:
        """Load every validated QuerySpec revision for one exact incarnation."""

        return self._load_chain(QuerySpec, run_id, run_incarnation)

    def save_research_state(
        self,
        state: ResearchState,
        *,
        expected_previous_revision: int | None,
        commit_guard: object | None = None,
    ) -> ResearchState:
        """Save one ResearchState revision with compare-and-swap semantics."""
        return self._save(
            state,
            expected_previous_revision=expected_previous_revision,
            commit_guard=commit_guard,
        )

    def save_replayable_semantic_transition(
        self,
        previous: ResearchState,
        successor: ResearchState,
        replay_input: ResearchSemanticReplayInput,
    ) -> ResearchState:
        """Atomically save one verified semantic successor and its replay input."""

        previous = _validated_research_state(previous, "previous")
        successor = _validated_research_state(successor, "successor")
        if type(replay_input) is not ResearchSemanticReplayInput:
            raise TypeError("replay_input must be ResearchSemanticReplayInput")
        replay_input = ResearchSemanticReplayInput.model_validate(
            replay_input.model_dump(
                mode="python",
                round_trip=True,
                warnings="error",
            )
        )
        if (
            successor.run_id != previous.run_id
            or successor.run_incarnation != previous.run_incarnation
            or successor.revision != previous.revision + 1
            or successor.schema_namespace_version != previous.schema_namespace_version
        ):
            raise AdaptiveResearchStateStoreCasError(
                "semantic successor identity is invalid"
            )
        admission = _require_exact_semantic_reduction(
            previous,
            successor,
            replay_input,
        )
        previous_payload = serialize_contract(previous)
        previous_digest = canonical_digest(previous)
        successor_payload = serialize_contract(successor)
        successor_digest = canonical_digest(successor)
        input_bytes = serialize_replay_input(replay_input)
        input_digest = _bytes_digest(input_bytes)
        identity = (successor.run_id, successor.run_incarnation)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            durable_previous = self._select_snapshot(
                connection,
                *identity,
                "research_state",
                previous.revision,
            )
            if (
                durable_previous is None
                or durable_previous["payload"] != previous_payload
                or durable_previous["digest"] != previous_digest
            ):
                raise AdaptiveResearchStateStoreCasError(
                    "semantic predecessor is not the exact durable snapshot"
                )
            existing_successor = self._select_snapshot(
                connection,
                *identity,
                "research_state",
                successor.revision,
            )
            existing_input = self._select_replay_input(
                connection,
                *identity,
                successor.revision,
            )
            if existing_successor is not None or existing_input is not None:
                if existing_successor is None or existing_input is None:
                    raise AdaptiveResearchStateStoreConflictError(
                        "semantic successor and replay input are incomplete"
                    )
                loaded_input = self._decode_replay_input(existing_input)
                if (
                    existing_successor["payload"] == successor_payload
                    and existing_successor["digest"] == successor_digest
                    and loaded_input == replay_input
                ):
                    return successor
                raise AdaptiveResearchStateStoreConflictError(
                    "conflicting duplicate semantic transition"
                )
            _require_linked_semantic_journal(
                connection,
                previous,
                replay_input,
                admission,
            )
            if (
                self._latest_revision(
                    connection,
                    *identity,
                    "research_state",
                )
                != previous.revision
            ):
                raise AdaptiveResearchStateStoreCasError(
                    "semantic successor compare-and-swap failed"
                )
            self._insert_research_snapshot(
                connection,
                successor,
                successor_payload,
                successor_digest,
            )
            self._insert_replay_input(
                connection,
                successor,
                replay_input,
                input_bytes,
                input_digest,
            )
        return successor

    def load_research_replay_input(
        self,
        run_id: str,
        run_incarnation: str,
        revision: int,
    ) -> ResearchSemanticReplayInput | None:
        """Load a replay input; legacy snapshots explicitly return ``None``."""

        self._validate_lookup_identity(run_id, run_incarnation, revision)
        assert revision is not None
        with self._connection() as connection:
            row = self._select_replay_input(
                connection,
                run_id,
                run_incarnation,
                revision,
            )
            return None if row is None else self._decode_replay_input(row)

    def load_latest_research_state(
        self,
        run_id: str,
        run_incarnation: str,
        *,
        busy_timeout: float | None = None,
        deadline_monotonic: float | None = None,
    ) -> ResearchState | None:
        """Load the latest valid ResearchState for exactly one run incarnation."""
        return self._load(
            ResearchState,
            run_id,
            run_incarnation,
            revision=None,
            busy_timeout=busy_timeout,
            deadline_monotonic=deadline_monotonic,
        )

    def load_research_state(
        self,
        run_id: str,
        run_incarnation: str,
        *,
        revision: int,
    ) -> ResearchState | None:
        """Load one exact immutable ResearchState revision."""
        return self._load(ResearchState, run_id, run_incarnation, revision=revision)

    def load_research_state_chain(
        self,
        run_id: str,
        run_incarnation: str,
    ) -> tuple[ResearchState, ...]:
        """Load every validated ResearchState revision for one exact incarnation."""

        return self._load_chain(ResearchState, run_id, run_incarnation)

    def load_verified_reentry_successor_chain(
        self,
        terminal_state: ResearchState,
        *,
        busy_timeout: float | None = None,
        deadline_monotonic: float | None = None,
    ) -> ResearchState:
        """Verify committed prepared re-entries from one terminal base to latest."""

        terminal_state = ResearchState.model_validate(
            terminal_state.model_dump(
                mode="python",
                round_trip=True,
                warnings="error",
            )
        )
        identity = (terminal_state.run_id, terminal_state.run_incarnation)
        with self._connection(
            busy_timeout=busy_timeout,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            base_row = self._select_snapshot(
                connection,
                *identity,
                "research_state",
                terminal_state.revision,
            )
            if (
                base_row is None
                or self._decode_snapshot(
                    base_row,
                    ResearchState,
                )
                != terminal_state
            ):
                raise AdaptiveResearchStateStoreCorruptionError(
                    "terminal research snapshot is not exact"
                )
            prepared_rows = connection.execute(
                f"""
                SELECT * FROM {_PREPARED_TABLE}
                WHERE run_id = ? AND run_incarnation = ?
                ORDER BY base_research_revision, research_reentry_id
                """,
                identity,
            ).fetchall()
            plans = {}
            for row in prepared_rows:
                plan = self._decode_prepared(row)
                plan_base = self._select_snapshot(
                    connection,
                    *identity,
                    "research_state",
                    plan.store_base_research_revision,
                )
                if (
                    plan_base is None
                    or canonical_digest(self._decode_snapshot(plan_base, ResearchState))
                    != plan.store_base_research_digest
                    or plan.schema_namespace_version
                    != terminal_state.schema_namespace_version
                ):
                    raise AdaptiveResearchStateStoreCorruptionError(
                        "prepared re-entry base authority is invalid"
                    )
                plans[plan.research_reentry_id] = plan
            commit_rows = connection.execute(
                f"""
                SELECT * FROM {_PREPARED_COMMIT_TABLE}
                WHERE run_id = ? AND run_incarnation = ?
                ORDER BY successor_revision, research_reentry_id
                """,
                identity,
            ).fetchall()
            current = terminal_state
            for row in commit_rows:
                plan = plans.get(row["research_reentry_id"])
                successor = self._select_snapshot(
                    connection,
                    *identity,
                    "research_state",
                    row["successor_revision"],
                )
                if plan is None or successor is None:
                    raise AdaptiveResearchStateStoreCorruptionError(
                        "committed re-entry authority is incomplete"
                    )
                decoded = self._decode_snapshot(successor, ResearchState)
                if (
                    plan.store_base_research_revision != current.revision
                    or plan.store_base_research_digest != canonical_digest(current)
                    or row["successor_revision"] != current.revision + 1
                    or row["successor_digest"] != canonical_digest(decoded)
                    or decoded.schema_namespace_version
                    != terminal_state.schema_namespace_version
                ):
                    raise AdaptiveResearchStateStoreCorruptionError(
                        "committed re-entry chain has a gap or conflict"
                    )
                current = decoded
            if (
                self._latest_revision(
                    connection,
                    *identity,
                    "research_state",
                )
                != current.revision
            ):
                raise AdaptiveResearchStateStoreCorruptionError(
                    "latest research snapshot is outside committed re-entry chain"
                )
            return current

    def prepare_targeted_reentry_commit(
        self,
        plan: PreparedTargetedReentryCommit,
    ) -> PreparedTargetedReentryCommit:
        checked, payload, payload_digest = _validated_prepared_plan(plan)
        identity = (
            checked.run_id,
            checked.run_incarnation,
            checked.research_reentry_id,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            base = self._select_snapshot(
                connection,
                checked.run_id,
                checked.run_incarnation,
                "research_state",
                checked.store_base_research_revision,
            )
            if (
                base is None
                or canonical_digest(self._decode_snapshot(base, ResearchState))
                != checked.store_base_research_digest
            ):
                raise AdaptiveResearchStateStoreCasError(
                    "prepared re-entry base snapshot is not durable"
                )
            if (
                self._latest_revision(
                    connection,
                    checked.run_id,
                    checked.run_incarnation,
                    "research_state",
                )
                != checked.store_base_research_revision
            ):
                raise AdaptiveResearchStateStoreCasError(
                    "prepared re-entry base snapshot is not current"
                )
            existing = self._select_prepared(connection, *identity)
            if existing is not None:
                loaded = self._decode_prepared(existing)
                if loaded == checked:
                    return loaded
                raise AdaptiveResearchStateStoreConflictError(
                    "conflicting prepared re-entry plan"
                )
            try:
                connection.execute(
                    f"""
                    INSERT INTO {_PREPARED_TABLE} (
                        run_id, run_incarnation, research_reentry_id,
                        missing_evidence_request_id, source_id, ordinal,
                        base_solver_revision, base_contract_name,
                        base_research_revision, payload, payload_digest,
                        plan_digest, created_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'research_state', ?, ?, ?, ?, ?)
                    """,
                    (
                        *identity,
                        checked.missing_evidence_request_id,
                        checked.source_id,
                        checked.ordinal,
                        checked.base_solver_revision,
                        checked.store_base_research_revision,
                        payload,
                        payload_digest,
                        checked.plan_digest,
                        time.time_ns(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AdaptiveResearchStateStoreConflictError(
                    "prepared re-entry identity already exists"
                ) from exc
        return checked

    def load_prepared_targeted_reentry_commit(
        self,
        run_id: str,
        run_incarnation: str,
        research_reentry_id: str,
    ) -> PreparedTargetedReentryCommit | None:
        self._validate_lookup_identity(run_id, run_incarnation, None)
        if not isinstance(research_reentry_id, str) or not research_reentry_id:
            raise ValueError("research_reentry_id is required")
        with self._connection() as connection:
            row = self._select_prepared(
                connection,
                run_id,
                run_incarnation,
                research_reentry_id,
            )
            return None if row is None else self._decode_prepared(row)

    def commit_prepared_targeted_reentry(
        self,
        plan: PreparedTargetedReentryCommit,
        successor: ResearchState,
    ) -> ResearchState:
        checked, _, _ = _validated_prepared_plan(plan)
        successor = ResearchState.model_validate(
            successor.model_dump(mode="python", round_trip=True, warnings="error")
        )
        if (
            successor.run_id != checked.run_id
            or successor.run_incarnation != checked.run_incarnation
            or successor.revision != checked.store_base_research_revision + 1
            or successor.schema_namespace_version != checked.schema_namespace_version
        ):
            raise AdaptiveResearchStateStoreCasError(
                "prepared re-entry successor identity is invalid"
            )
        successor_payload = serialize_contract(successor)
        successor_digest = canonical_digest(successor)
        identity = (
            checked.run_id,
            checked.run_incarnation,
            checked.research_reentry_id,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prepared = self._select_prepared(connection, *identity)
            if prepared is None or self._decode_prepared(prepared) != checked:
                raise AdaptiveResearchStateStoreConflictError(
                    "prepared re-entry plan is missing or conflicting"
                )
            committed = self._select_prepared_commit(connection, *identity)
            latest_revision = self._latest_revision(
                connection,
                successor.run_id,
                successor.run_incarnation,
                "research_state",
            )
            if committed is not None:
                if latest_revision != successor.revision:
                    raise AdaptiveResearchStateStoreConflictError(
                        "prepared re-entry successor is no longer current"
                    )
                return self._confirmed_prepared_successor(
                    connection,
                    committed,
                    successor,
                    successor_payload,
                    successor_digest,
                )
            existing = self._select_snapshot(
                connection,
                successor.run_id,
                successor.run_incarnation,
                "research_state",
                successor.revision,
            )
            if existing is None:
                if latest_revision != checked.store_base_research_revision:
                    raise AdaptiveResearchStateStoreCasError(
                        "prepared re-entry successor compare-and-swap failed"
                    )
                self._insert_research_snapshot(
                    connection,
                    successor,
                    successor_payload,
                    successor_digest,
                )
            else:
                if latest_revision != successor.revision:
                    raise AdaptiveResearchStateStoreConflictError(
                        "prepared re-entry successor is no longer current"
                    )
                if (
                    existing["payload"] != successor_payload
                    or existing["digest"] != successor_digest
                ):
                    raise AdaptiveResearchStateStoreConflictError(
                        "prepared re-entry successor snapshot is conflicting"
                    )
            connection.execute(
                f"""
                INSERT INTO {_PREPARED_COMMIT_TABLE} (
                    run_id, run_incarnation, research_reentry_id,
                    successor_contract_name, successor_revision,
                    successor_digest, created_at_ns
                ) VALUES (?, ?, ?, 'research_state', ?, ?, ?)
                """,
                (*identity, successor.revision, successor_digest, time.time_ns()),
            )
        return successor

    def is_prepared_targeted_reentry_committed(
        self,
        plan: PreparedTargetedReentryCommit,
    ) -> bool:
        checked, _, _ = _validated_prepared_plan(plan)
        with self._connection() as connection:
            row = self._select_prepared_commit(
                connection,
                checked.run_id,
                checked.run_incarnation,
                checked.research_reentry_id,
            )
            return row is not None

    def _save(
        self,
        contract: _Model,
        *,
        expected_previous_revision: int | None,
        commit_guard: object | None,
    ) -> _Model:
        model_type = type(contract)
        if model_type not in (QuerySpec, ResearchState):
            raise TypeError("contract must be QuerySpec or ResearchState")
        contract = model_type.model_validate(
            contract.model_dump(mode="python", round_trip=True, warnings="error")
        )
        if (
            type(expected_previous_revision) is not int
            and expected_previous_revision is not None
        ):
            raise ValueError("expected_previous_revision must be an integer or None")
        payload = serialize_contract(contract)
        digest = canonical_digest(contract)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_commit_guard(commit_guard)
            existing = self._select_snapshot(
                connection,
                contract.run_id,
                contract.run_incarnation,
                contract.contract_name,
                contract.revision,
            )
            if existing is not None:
                loaded = self._decode_snapshot(existing, model_type)
                if existing["payload"] == payload and existing["digest"] == digest:
                    if (
                        model_type is ResearchState
                        and contract.revision > 0
                        and self._select_replay_input(
                            connection,
                            contract.run_id,
                            contract.run_incarnation,
                            contract.revision,
                        )
                        is not None
                    ):
                        raise AdaptiveResearchStateStoreConflictError(
                            "conflicting duplicate research replay input"
                        )
                    _commit_with_guard(
                        connection,
                        commit_guard,
                        contract_name=contract.contract_name,
                    )
                    return loaded
                raise AdaptiveResearchStateStoreConflictError(
                    "conflicting duplicate typed snapshot"
                )

            if model_type is ResearchState and contract.revision > 0:
                raise AdaptiveResearchStateStoreConflictError(
                    "new ResearchState successor requires replay input"
                )

            current_revision = self._latest_revision(
                connection,
                contract.run_id,
                contract.run_incarnation,
                contract.contract_name,
            )
            if model_type is QuerySpec:
                if expected_previous_revision is not None:
                    raise ValueError(
                        "QuerySpec does not accept expected_previous_revision"
                    )
                if contract.revision != current_revision + 1:
                    raise AdaptiveResearchStateStoreCasError(
                        "QuerySpec revision must be monotonic"
                    )
            elif (
                -1 if expected_previous_revision is None else expected_previous_revision
            ) != current_revision or contract.revision != current_revision + 1:
                raise AdaptiveResearchStateStoreCasError(
                    "ResearchState revision compare-and-swap failed"
                )

            created_at_ns = time.time_ns()
            if type(created_at_ns) is not int or created_at_ns < 0:
                raise ValueError("created_at_ns must be a non-negative integer")
            connection.execute(
                f"""
                INSERT INTO {_SNAPSHOT_TABLE} (
                    run_id, run_incarnation, contract_name, revision, payload, digest, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract.run_id,
                    contract.run_incarnation,
                    contract.contract_name,
                    contract.revision,
                    payload,
                    digest,
                    created_at_ns,
                ),
            )
            _commit_with_guard(
                connection,
                commit_guard,
                contract_name=contract.contract_name,
            )
        return contract

    def _load(
        self,
        model_type: type[_Model],
        run_id: str,
        run_incarnation: str,
        *,
        revision: int | None,
        busy_timeout: float | None = None,
        deadline_monotonic: float | None = None,
    ) -> _Model | None:
        self._validate_lookup_identity(run_id, run_incarnation, revision)
        contract_name = model_type.model_fields["contract_name"].default
        with self._connection(
            busy_timeout=busy_timeout,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            if revision is None:
                row = connection.execute(
                    f"""
                    SELECT run_id, run_incarnation, contract_name, revision, payload, digest, created_at_ns
                    FROM {_SNAPSHOT_TABLE}
                    WHERE run_id = ? AND run_incarnation = ? AND contract_name = ?
                    ORDER BY revision DESC LIMIT 1
                    """,
                    (run_id, run_incarnation, contract_name),
                ).fetchone()
            else:
                row = self._select_snapshot(
                    connection, run_id, run_incarnation, contract_name, revision
                )
            return None if row is None else self._decode_snapshot(row, model_type)

    def _load_chain(
        self,
        model_type: type[_Model],
        run_id: str,
        run_incarnation: str,
    ) -> tuple[_Model, ...]:
        self._validate_lookup_identity(run_id, run_incarnation, None)
        contract_name = model_type.model_fields["contract_name"].default
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, run_incarnation, contract_name, revision,
                       payload, digest, created_at_ns
                FROM {_SNAPSHOT_TABLE}
                WHERE run_id = ? AND run_incarnation = ? AND contract_name = ?
                ORDER BY revision
                """,
                (run_id, run_incarnation, contract_name),
            ).fetchall()
        decoded: list[_Model] = []
        for expected_revision, row in enumerate(rows):
            if (
                type(row["revision"]) is not int
                or row["revision"] != expected_revision
                or type(row["created_at_ns"]) is not int
                or row["created_at_ns"] < 0
            ):
                raise AdaptiveResearchStateStoreCorruptionError(
                    "typed snapshot chain is not contiguous and canonical"
                )
            decoded.append(self._decode_snapshot(row, model_type))
        return tuple(decoded)

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = self._read_schema_version(connection)
            if version is None:
                self._create_schema(connection)
                version = ADAPTIVE_RESEARCH_STATE_STORE_SCHEMA_VERSION
            elif version > ADAPTIVE_RESEARCH_STATE_STORE_SCHEMA_VERSION:
                raise AdaptiveResearchStateStoreMigrationError(
                    "typed snapshot schema is newer than this application"
                )
            if version == 1:
                self._migrate_v1_to_v2(connection)
                version = 2
            if version == 2:
                self._migrate_v2_to_v3(connection)
                version = 3
            elif version != ADAPTIVE_RESEARCH_STATE_STORE_SCHEMA_VERSION:
                raise AdaptiveResearchStateStoreMigrationError(
                    "typed snapshot schema version is not supported"
                )
            self._validate_schema(connection)
        if self.db_path != ":memory:":
            secure_sqlite_sidecars(self.db_path, tighten_existing=True)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        found = names.intersection(
            {
                _META_TABLE,
                _SNAPSHOT_TABLE,
                _PREPARED_TABLE,
                _PREPARED_COMMIT_TABLE,
                _REPLAY_INPUT_TABLE,
            }
        )
        if found:
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot schema is incomplete"
            )
        for statement in _OWNED_TABLE_SQL:
            connection.execute(statement)
        connection.execute(
            f"INSERT INTO {_META_TABLE} (key, value) VALUES (?, ?)",
            (_VERSION_KEY, ADAPTIVE_RESEARCH_STATE_STORE_SCHEMA_VERSION),
        )

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        v1_tables = (_SNAPSHOT_TABLE, _META_TABLE)
        if _owned_schema_signature(
            connection, v1_tables
        ) != _canonical_owned_schema_signature(_V1_OWNED_TABLE_SQL, v1_tables):
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot v1 schema definition or constraints are not canonical"
            )
        meta_rows = connection.execute(
            f"SELECT key, value FROM {_META_TABLE} ORDER BY key"
        ).fetchall()
        if len(meta_rows) != 1 or (meta_rows[0]["key"], meta_rows[0]["value"]) != (
            _VERSION_KEY,
            1,
        ):
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot v1 meta rows are incompatible"
            )
        for statement in _V2_ADDITIONAL_SQL:
            connection.execute(statement)
        updated = connection.execute(
            f"UPDATE {_META_TABLE} SET value = 2 WHERE key = ? AND value = 1",
            (_VERSION_KEY,),
        )
        if updated.rowcount != 1:
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot v1 migration compare-and-swap failed"
            )

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        v2_tables = (
            _SNAPSHOT_TABLE,
            _META_TABLE,
            _PREPARED_TABLE,
            _PREPARED_COMMIT_TABLE,
        )
        if _owned_schema_signature(
            connection, v2_tables
        ) != _canonical_owned_schema_signature(_V2_OWNED_TABLE_SQL, v2_tables):
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot v2 schema definition or constraints are not canonical"
            )
        meta_rows = connection.execute(
            f"SELECT key, value FROM {_META_TABLE} ORDER BY key"
        ).fetchall()
        if len(meta_rows) != 1 or (meta_rows[0]["key"], meta_rows[0]["value"]) != (
            _VERSION_KEY,
            2,
        ):
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot v2 meta rows are incompatible"
            )
        for statement in _V3_ADDITIONAL_SQL:
            connection.execute(statement)
        updated = connection.execute(
            f"UPDATE {_META_TABLE} SET value = 3 WHERE key = ? AND value = 2",
            (_VERSION_KEY,),
        )
        if updated.rowcount != 1:
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot v2 migration compare-and-swap failed"
            )

    @staticmethod
    def _read_schema_version(connection: sqlite3.Connection) -> int | None:
        meta_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_META_TABLE,),
        ).fetchone()
        if meta_exists is None:
            return None
        row = connection.execute(
            f"SELECT value FROM {_META_TABLE} WHERE key = ?", (_VERSION_KEY,)
        ).fetchone()
        if row is None or type(row["value"]) is not int:
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot schema version is invalid"
            )
        return int(row["value"])

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        expected_columns = {
            "run_id": ("TEXT", 1, 1),
            "run_incarnation": ("TEXT", 1, 2),
            "contract_name": ("TEXT", 1, 3),
            "revision": ("INTEGER", 1, 4),
            "payload": ("BLOB", 1, 0),
            "digest": ("TEXT", 1, 0),
            "created_at_ns": ("INTEGER", 1, 0),
        }
        snapshot_columns = {
            row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
            for row in connection.execute(f"PRAGMA table_info({_SNAPSHOT_TABLE})")
        }
        if snapshot_columns != expected_columns:
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot table is incompatible"
            )
        meta_columns = {
            row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
            for row in connection.execute(f"PRAGMA table_info({_META_TABLE})")
        }
        if meta_columns != {
            "key": ("TEXT", 0, 1),
            "value": ("INTEGER", 1, 0),
        }:
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot meta table is incompatible"
            )
        meta_rows = connection.execute(
            f"SELECT key, value FROM {_META_TABLE} ORDER BY key"
        ).fetchall()
        if len(meta_rows) != 1 or (
            meta_rows[0]["key"],
            meta_rows[0]["value"],
        ) != (_VERSION_KEY, ADAPTIVE_RESEARCH_STATE_STORE_SCHEMA_VERSION):
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot meta rows are incompatible"
            )
        if _owned_schema_signature(connection) != _canonical_owned_schema_signature():
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot schema definition or constraints are incompatible"
            )
        for row in connection.execute(
            f"""
            SELECT run_id, run_incarnation, successor_contract_name,
                   successor_revision, input_kind, input_version,
                   input_bytes, input_digest, created_at_ns
            FROM {_REPLAY_INPUT_TABLE}
            """
        ):
            try:
                AdaptiveResearchStateStore._decode_replay_input(row)
            except AdaptiveResearchStateStoreCorruptionError as exc:
                raise AdaptiveResearchStateStoreMigrationError(
                    "research replay input rows are incompatible"
                ) from exc
        if (
            AdaptiveResearchStateStore._read_schema_version(connection)
            != ADAPTIVE_RESEARCH_STATE_STORE_SCHEMA_VERSION
        ):
            raise AdaptiveResearchStateStoreMigrationError(
                "typed snapshot schema migration did not reach the current version"
            )

    def _connect(
        self,
        *,
        busy_timeout: float | None = None,
        deadline_monotonic: float | None = None,
    ) -> sqlite3.Connection:
        timeout = _bounded_busy_timeout(busy_timeout, deadline_monotonic)
        connection = sqlite3.connect(
            self._memory_uri or self.db_path,
            timeout=timeout,
            isolation_level=None,
            uri=self._memory_uri is not None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(
        self,
        *,
        busy_timeout: float | None = None,
        deadline_monotonic: float | None = None,
    ) -> Iterator[sqlite3.Connection]:
        with self._lifecycle_lock:
            if self._closed:
                raise AdaptiveResearchStateStoreError("typed snapshot store is closed")
            connection = self._connect(
                busy_timeout=busy_timeout,
                deadline_monotonic=deadline_monotonic,
            )
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

    @staticmethod
    def _select_snapshot(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        contract_name: str,
        revision: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT run_id, run_incarnation, contract_name, revision, payload, digest, created_at_ns
            FROM {_SNAPSHOT_TABLE}
            WHERE run_id = ? AND run_incarnation = ? AND contract_name = ? AND revision = ?
            """,
            (run_id, run_incarnation, contract_name, revision),
        ).fetchone()

    @staticmethod
    def _select_prepared(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        research_reentry_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT * FROM {_PREPARED_TABLE}
            WHERE run_id = ? AND run_incarnation = ? AND research_reentry_id = ?
            """,
            (run_id, run_incarnation, research_reentry_id),
        ).fetchone()

    @staticmethod
    def _select_prepared_commit(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        research_reentry_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT * FROM {_PREPARED_COMMIT_TABLE}
            WHERE run_id = ? AND run_incarnation = ? AND research_reentry_id = ?
            """,
            (run_id, run_incarnation, research_reentry_id),
        ).fetchone()

    @staticmethod
    def _select_replay_input(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        successor_revision: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"""
            SELECT run_id, run_incarnation, successor_contract_name,
                   successor_revision, input_kind, input_version,
                   input_bytes, input_digest, created_at_ns
            FROM {_REPLAY_INPUT_TABLE}
            WHERE run_id = ? AND run_incarnation = ? AND successor_revision = ?
            """,
            (run_id, run_incarnation, successor_revision),
        ).fetchone()

    @staticmethod
    def _insert_replay_input(
        connection: sqlite3.Connection,
        successor: ResearchState,
        replay_input: ResearchSemanticReplayInput,
        input_bytes: bytes,
        input_digest: str,
    ) -> None:
        connection.execute(
            f"""
            INSERT INTO {_REPLAY_INPUT_TABLE} (
                run_id, run_incarnation, successor_contract_name,
                successor_revision, input_kind, input_version,
                input_bytes, input_digest, created_at_ns
            ) VALUES (?, ?, 'research_state', ?, ?, ?, ?, ?, ?)
            """,
            (
                successor.run_id,
                successor.run_incarnation,
                successor.revision,
                replay_input.input_kind,
                replay_input.input_version,
                input_bytes,
                input_digest,
                time.time_ns(),
            ),
        )

    @staticmethod
    def _decode_replay_input(row: sqlite3.Row) -> ResearchSemanticReplayInput:
        input_bytes = row["input_bytes"]
        input_digest = row["input_digest"]
        if (
            not isinstance(input_bytes, bytes)
            or not isinstance(input_digest, str)
            or type(row["successor_revision"]) is not int
            or type(row["input_version"]) is not int
            or type(row["created_at_ns"]) is not int
        ):
            raise AdaptiveResearchStateStoreCorruptionError(
                "research replay input storage types are invalid"
            )
        if not hmac.compare_digest(input_digest, _bytes_digest(input_bytes)):
            raise AdaptiveResearchStateStoreCorruptionError(
                "research replay input digest does not match"
            )
        try:
            decoded = deserialize_replay_input(input_bytes)
        except ReplayInputError as exc:
            raise AdaptiveResearchStateStoreCorruptionError(
                "research replay input payload is invalid"
            ) from exc
        if (
            type(decoded) is not ResearchSemanticReplayInput
            or row["successor_contract_name"] != "research_state"
            or row["input_kind"] != decoded.input_kind
            or row["input_version"] != decoded.input_version
            or decoded.freshness_context.run_id != row["run_id"]
            or decoded.freshness_context.run_incarnation != row["run_incarnation"]
            or (
                decoded.probe_result is not None
                and (
                    decoded.probe_result.run_id != row["run_id"]
                    or decoded.probe_result.run_incarnation != row["run_incarnation"]
                    or decoded.probe_result.revision
                    != row["successor_revision"] - 1
                    or decoded.probe_result.schema_namespace_version
                    != decoded.freshness_context.schema_namespace_version
                )
            )
        ):
            raise AdaptiveResearchStateStoreCorruptionError(
                "research replay input identity is invalid"
            )
        return decoded

    @staticmethod
    def _decode_prepared(row: sqlite3.Row) -> PreparedTargetedReentryCommit:
        payload = row["payload"]
        if not isinstance(payload, bytes) or not isinstance(row["payload_digest"], str):
            raise AdaptiveResearchStateStoreCorruptionError(
                "prepared re-entry storage types are invalid"
            )
        try:
            decoded = PreparedTargetedReentryCommit.model_validate_json(payload)
            canonical = canonical_json_bytes(decoded, limits=_PREPARED_LIMITS)
        except (ValueError, TypeError) as exc:
            raise AdaptiveResearchStateStoreCorruptionError(
                "prepared re-entry payload is invalid"
            ) from exc
        if (
            canonical != payload
            or canonical_digest(decoded) != row["payload_digest"]
            or decoded.plan_digest != row["plan_digest"]
            or decoded.run_id != row["run_id"]
            or decoded.run_incarnation != row["run_incarnation"]
            or decoded.research_reentry_id != row["research_reentry_id"]
            or decoded.missing_evidence_request_id != row["missing_evidence_request_id"]
            or decoded.source_id != row["source_id"]
            or decoded.ordinal != row["ordinal"]
            or decoded.base_solver_revision != row["base_solver_revision"]
            or decoded.store_base_research_revision != row["base_research_revision"]
        ):
            raise AdaptiveResearchStateStoreCorruptionError(
                "prepared re-entry identity or digest is invalid"
            )
        return decoded

    @staticmethod
    def _insert_research_snapshot(
        connection: sqlite3.Connection,
        state: ResearchState,
        payload: bytes,
        digest: str,
    ) -> None:
        connection.execute(
            f"""
            INSERT INTO {_SNAPSHOT_TABLE} (
                run_id, run_incarnation, contract_name, revision,
                payload, digest, created_at_ns
            ) VALUES (?, ?, 'research_state', ?, ?, ?, ?)
            """,
            (
                state.run_id,
                state.run_incarnation,
                state.revision,
                payload,
                digest,
                time.time_ns(),
            ),
        )

    @staticmethod
    def _confirmed_prepared_successor(
        connection: sqlite3.Connection,
        committed: sqlite3.Row,
        successor: ResearchState,
        payload: bytes,
        digest: str,
    ) -> ResearchState:
        row = AdaptiveResearchStateStore._select_snapshot(
            connection,
            successor.run_id,
            successor.run_incarnation,
            "research_state",
            successor.revision,
        )
        if (
            committed["successor_revision"] != successor.revision
            or committed["successor_digest"] != digest
            or row is None
            or row["payload"] != payload
            or row["digest"] != digest
        ):
            raise AdaptiveResearchStateStoreConflictError(
                "prepared re-entry commit is conflicting"
            )
        return successor

    @staticmethod
    def _latest_revision(
        connection: sqlite3.Connection,
        run_id: str,
        run_incarnation: str,
        contract_name: str,
    ) -> int:
        row = connection.execute(
            f"""
            SELECT MAX(revision) AS revision FROM {_SNAPSHOT_TABLE}
            WHERE run_id = ? AND run_incarnation = ? AND contract_name = ?
            """,
            (run_id, run_incarnation, contract_name),
        ).fetchone()
        return -1 if row is None or row["revision"] is None else int(row["revision"])

    @staticmethod
    def _decode_snapshot(row: sqlite3.Row, model_type: type[_Model]) -> _Model:
        payload = row["payload"]
        if not isinstance(payload, bytes) or not isinstance(row["digest"], str):
            raise AdaptiveResearchStateStoreCorruptionError(
                "typed snapshot storage types are invalid"
            )
        try:
            decoded = deserialize_as(payload, model_type)
            canonical_payload = serialize_contract(decoded)
            digest = canonical_digest(decoded)
        except AdaptiveSerializationError as exc:
            raise AdaptiveResearchStateStoreCorruptionError(
                "typed snapshot payload is invalid"
            ) from exc
        if payload != canonical_payload or row["digest"] != digest:
            raise AdaptiveResearchStateStoreCorruptionError(
                "typed snapshot payload digest does not match"
            )
        if (
            decoded.run_id != row["run_id"]
            or decoded.run_incarnation != row["run_incarnation"]
            or decoded.contract_name != row["contract_name"]
            or decoded.revision != row["revision"]
        ):
            raise AdaptiveResearchStateStoreCorruptionError(
                "typed snapshot identity does not match its payload"
            )
        return decoded

    @staticmethod
    def _validate_lookup_identity(
        run_id: str,
        run_incarnation: str,
        revision: int | None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id is required")
        if not isinstance(run_incarnation, str) or not run_incarnation:
            raise ValueError("run_incarnation is required")
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("revision must be a non-negative integer or None")


def _validated_prepared_plan(
    plan: PreparedTargetedReentryCommit,
) -> tuple[PreparedTargetedReentryCommit, bytes, str]:
    if type(plan) is not PreparedTargetedReentryCommit:
        raise TypeError("plan must be exact PreparedTargetedReentryCommit")
    checked = PreparedTargetedReentryCommit.model_validate(
        plan.model_dump(mode="python", round_trip=True, warnings="error")
    )
    payload = canonical_json_bytes(checked, limits=_PREPARED_LIMITS)
    return checked, payload, canonical_digest(checked)


def _bounded_busy_timeout(
    busy_timeout: float | None,
    deadline_monotonic: float | None,
) -> float:
    if busy_timeout is None:
        timeout = 30.0
    elif (
        isinstance(busy_timeout, bool)
        or not isinstance(busy_timeout, (int, float))
        or busy_timeout < 0
        or busy_timeout != busy_timeout
        or busy_timeout == float("inf")
    ):
        raise ValueError("busy_timeout must be a non-negative finite number")
    else:
        timeout = float(busy_timeout)
    if deadline_monotonic is None:
        return timeout
    if (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or deadline_monotonic != deadline_monotonic
        or deadline_monotonic == float("inf")
    ):
        raise ValueError("deadline_monotonic must be a finite number")
    remaining = float(deadline_monotonic) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("typed snapshot store deadline expired")
    return min(timeout, remaining)
