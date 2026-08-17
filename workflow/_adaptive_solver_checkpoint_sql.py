"""Owned SQLite schema for durable solver checkpoints."""

from __future__ import annotations

from functools import lru_cache
import sqlite3

from .sqlite_schema_signature import (
    SchemaSignature,
    owned_sqlite_schema_signature,
)


SOLVER_CHECKPOINT_PREFIX = "adaptive_solver_checkpoint_"
SOLVER_CHECKPOINT_V2_TABLES = (
    "adaptive_solver_checkpoint_snapshots",
    "adaptive_solver_checkpoint_actions",
    "adaptive_solver_checkpoint_execution_reconciliations",
    "adaptive_solver_checkpoint_heads",
    "adaptive_solver_checkpoint_terminals",
)

SOLVER_CHECKPOINT_V2_SCHEMA_SQL = (
    """
    CREATE TABLE adaptive_solver_checkpoint_snapshots (
        run_id TEXT NOT NULL CHECK (length(run_id) > 0),
        run_incarnation TEXT NOT NULL CHECK (length(run_incarnation) > 0),
        state_revision INTEGER NOT NULL
            CHECK (typeof(state_revision) = 'integer' AND state_revision >= 0),
        source_action_revision INTEGER
            CHECK (source_action_revision IS NULL OR
                   (typeof(source_action_revision) = 'integer' AND source_action_revision >= 0)),
        state_bytes BLOB NOT NULL CHECK (typeof(state_bytes) = 'blob'),
        state_digest TEXT NOT NULL
            CHECK (length(state_digest) = 71 AND substr(state_digest, 1, 7) = 'sha256:' AND
                   substr(state_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        created_at_ns INTEGER NOT NULL
            CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, state_revision),
        UNIQUE (run_id, run_incarnation, source_action_revision)
    )
    """,
    """
    CREATE TABLE adaptive_solver_checkpoint_actions (
        run_id TEXT NOT NULL CHECK (length(run_id) > 0),
        run_incarnation TEXT NOT NULL CHECK (length(run_incarnation) > 0),
        action_revision INTEGER NOT NULL
            CHECK (typeof(action_revision) = 'integer' AND action_revision >= 0),
        action_kind TEXT NOT NULL CHECK (action_kind IN ('transition', 'execution')),
        base_state_revision INTEGER NOT NULL
            CHECK (typeof(base_state_revision) = 'integer' AND base_state_revision >= 0),
        base_state_digest TEXT NOT NULL
            CHECK (length(base_state_digest) = 71 AND substr(base_state_digest, 1, 7) = 'sha256:' AND
                   substr(base_state_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        result_state_revision INTEGER
            CHECK (result_state_revision IS NULL OR
                   (typeof(result_state_revision) = 'integer' AND result_state_revision >= 0)),
        result_state_digest TEXT
            CHECK (result_state_digest IS NULL OR
                   (length(result_state_digest) = 71 AND substr(result_state_digest, 1, 7) = 'sha256:' AND
                    substr(result_state_digest, 8) NOT GLOB '*[^0-9a-f]*')),
        candidate_id TEXT CHECK (candidate_id IS NULL OR length(candidate_id) > 0),
        execution_id TEXT CHECK (execution_id IS NULL OR length(execution_id) > 0),
        normalized_ast_digest TEXT
            CHECK (normalized_ast_digest IS NULL OR
                   (length(normalized_ast_digest) = 71 AND substr(normalized_ast_digest, 1, 7) = 'sha256:' AND
                    substr(normalized_ast_digest, 8) NOT GLOB '*[^0-9a-f]*')),
        action_bytes BLOB NOT NULL CHECK (typeof(action_bytes) = 'blob'),
        action_digest TEXT NOT NULL
            CHECK (length(action_digest) = 71 AND substr(action_digest, 1, 7) = 'sha256:' AND
                   substr(action_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        created_at_ns INTEGER NOT NULL
            CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, action_revision),
        CHECK (
            (action_kind = 'transition' AND
             result_state_revision = base_state_revision + 1 AND result_state_digest IS NOT NULL AND
             candidate_id IS NULL AND execution_id IS NULL AND normalized_ast_digest IS NULL)
            OR
            (action_kind = 'execution' AND
             result_state_revision IS NULL AND result_state_digest IS NULL AND
             candidate_id IS NOT NULL AND execution_id IS NOT NULL AND normalized_ast_digest IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE adaptive_solver_checkpoint_execution_reconciliations (
        run_id TEXT NOT NULL CHECK (length(run_id) > 0),
        run_incarnation TEXT NOT NULL CHECK (length(run_incarnation) > 0),
        action_revision INTEGER NOT NULL
            CHECK (typeof(action_revision) = 'integer' AND action_revision >= 0),
        outcome TEXT NOT NULL CHECK (outcome IN ('KNOWN', 'UNKNOWN')),
        result_state_revision INTEGER NOT NULL
            CHECK (typeof(result_state_revision) = 'integer' AND result_state_revision >= 0),
        result_state_digest TEXT NOT NULL
            CHECK (length(result_state_digest) = 71 AND substr(result_state_digest, 1, 7) = 'sha256:' AND
                   substr(result_state_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        result_bytes BLOB NOT NULL CHECK (typeof(result_bytes) = 'blob'),
        result_digest TEXT NOT NULL
            CHECK (length(result_digest) = 71 AND substr(result_digest, 1, 7) = 'sha256:' AND
                   substr(result_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        created_at_ns INTEGER NOT NULL
            CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, action_revision),
        FOREIGN KEY (run_id, run_incarnation, action_revision)
            REFERENCES adaptive_solver_checkpoint_actions
                (run_id, run_incarnation, action_revision)
    )
    """,
    """
    CREATE TABLE adaptive_solver_checkpoint_heads (
        run_id TEXT NOT NULL CHECK (length(run_id) > 0),
        run_incarnation TEXT NOT NULL CHECK (length(run_incarnation) > 0),
        initial_state_revision INTEGER NOT NULL
            CHECK (typeof(initial_state_revision) = 'integer' AND initial_state_revision >= 0),
        state_revision INTEGER NOT NULL
            CHECK (typeof(state_revision) = 'integer' AND state_revision >= initial_state_revision),
        state_digest TEXT NOT NULL
            CHECK (length(state_digest) = 71 AND substr(state_digest, 1, 7) = 'sha256:' AND
                   substr(state_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        next_action_revision INTEGER NOT NULL
            CHECK (typeof(next_action_revision) = 'integer' AND next_action_revision >= 0),
        pending_execution_action_revision INTEGER
            CHECK (pending_execution_action_revision IS NULL OR
                   (typeof(pending_execution_action_revision) = 'integer' AND
                    pending_execution_action_revision >= 0 AND
                    pending_execution_action_revision = next_action_revision - 1)),
        terminal_digest TEXT
            CHECK (terminal_digest IS NULL OR
                   (length(terminal_digest) = 71 AND
                    substr(terminal_digest, 1, 7) = 'sha256:' AND
                    substr(terminal_digest, 8) NOT GLOB '*[^0-9a-f]*')),
        CHECK (terminal_digest IS NULL OR
               pending_execution_action_revision IS NULL),
        PRIMARY KEY (run_id, run_incarnation),
        FOREIGN KEY (run_id, run_incarnation, state_revision)
            REFERENCES adaptive_solver_checkpoint_snapshots
                (run_id, run_incarnation, state_revision)
    )
    """,
    """
    CREATE TABLE adaptive_solver_checkpoint_terminals (
        run_id TEXT NOT NULL CHECK (length(run_id) > 0),
        run_incarnation TEXT NOT NULL CHECK (length(run_incarnation) > 0),
        state_revision INTEGER NOT NULL
            CHECK (typeof(state_revision) = 'integer' AND state_revision >= 0),
        state_digest TEXT NOT NULL
            CHECK (length(state_digest) = 71 AND substr(state_digest, 1, 7) = 'sha256:' AND
                   substr(state_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        next_action_revision INTEGER NOT NULL
            CHECK (typeof(next_action_revision) = 'integer' AND next_action_revision >= 0),
        terminal_bytes BLOB NOT NULL CHECK (typeof(terminal_bytes) = 'blob'),
        terminal_digest TEXT NOT NULL
            CHECK (length(terminal_digest) = 71 AND substr(terminal_digest, 1, 7) = 'sha256:' AND
                   substr(terminal_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        created_at_ns INTEGER NOT NULL
            CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation),
        FOREIGN KEY (run_id, run_incarnation, state_revision)
            REFERENCES adaptive_solver_checkpoint_snapshots
                (run_id, run_incarnation, state_revision)
    )
    """,
    """
    CREATE UNIQUE INDEX adaptive_solver_checkpoint_execution_candidate_unique
    ON adaptive_solver_checkpoint_actions (run_id, run_incarnation, candidate_id)
    WHERE action_kind = 'execution'
    """,
    """
    CREATE UNIQUE INDEX adaptive_solver_checkpoint_execution_id_unique
    ON adaptive_solver_checkpoint_actions (run_id, run_incarnation, execution_id)
    WHERE action_kind = 'execution'
    """,
    """
    CREATE UNIQUE INDEX adaptive_solver_checkpoint_execution_ast_unique
    ON adaptive_solver_checkpoint_actions (run_id, run_incarnation, normalized_ast_digest)
    WHERE action_kind = 'execution'
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_snapshots_no_update
    BEFORE UPDATE ON adaptive_solver_checkpoint_snapshots
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint snapshots are immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_snapshots_no_delete
    BEFORE DELETE ON adaptive_solver_checkpoint_snapshots
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint snapshots are immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_actions_no_update
    BEFORE UPDATE ON adaptive_solver_checkpoint_actions
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint actions are immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_actions_no_delete
    BEFORE DELETE ON adaptive_solver_checkpoint_actions
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint actions are immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_execution_reconciliations_no_update
    BEFORE UPDATE ON adaptive_solver_checkpoint_execution_reconciliations
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint reconciliations are immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_execution_reconciliations_no_delete
    BEFORE DELETE ON adaptive_solver_checkpoint_execution_reconciliations
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint reconciliations are immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_execution_reconciliations_execution_only
    BEFORE INSERT ON adaptive_solver_checkpoint_execution_reconciliations
    WHEN NOT EXISTS (
        SELECT 1 FROM adaptive_solver_checkpoint_actions
        WHERE run_id = NEW.run_id AND run_incarnation = NEW.run_incarnation
          AND action_revision = NEW.action_revision AND action_kind = 'execution'
    )
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint reconciliation requires execution action'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_terminals_no_update
    BEFORE UPDATE ON adaptive_solver_checkpoint_terminals
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint terminals are immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_terminals_no_delete
    BEFORE DELETE ON adaptive_solver_checkpoint_terminals
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint terminals are immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_heads_terminal_digest_immutable
    BEFORE UPDATE OF terminal_digest ON adaptive_solver_checkpoint_heads
    WHEN OLD.terminal_digest IS NOT NULL
         AND NEW.terminal_digest IS NOT OLD.terminal_digest
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint terminal digest is immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_heads_no_delete
    BEFORE DELETE ON adaptive_solver_checkpoint_heads
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint heads cannot be deleted'); END
    """,
)

_SOLVER_REPLAY_INPUT_TABLE_SQL = """
    CREATE TABLE adaptive_solver_checkpoint_replay_inputs (
        run_id TEXT NOT NULL CHECK (length(run_id) > 0),
        run_incarnation TEXT NOT NULL CHECK (length(run_incarnation) > 0),
        action_revision INTEGER NOT NULL
            CHECK (typeof(action_revision) = 'integer' AND action_revision >= 0),
        input_kind TEXT NOT NULL CHECK (input_kind IN (
            'solver_sql_proposal',
            'solver_missing_evidence',
            'solver_reentry_admission',
            'solver_reentry_completed'
        )),
        input_version INTEGER NOT NULL
            CHECK (typeof(input_version) = 'integer' AND input_version = 1),
        input_bytes BLOB NOT NULL CHECK (typeof(input_bytes) = 'blob'),
        input_digest TEXT NOT NULL
            CHECK (length(input_digest) = 71 AND substr(input_digest, 1, 7) = 'sha256:' AND
                   substr(input_digest, 8) NOT GLOB '*[^0-9a-f]*'),
        created_at_ns INTEGER NOT NULL
            CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, action_revision),
        FOREIGN KEY (run_id, run_incarnation, action_revision)
            REFERENCES adaptive_solver_checkpoint_actions
                (run_id, run_incarnation, action_revision)
    )
"""
SOLVER_CHECKPOINT_V3_ADDITIONAL_SQL = (
    _SOLVER_REPLAY_INPUT_TABLE_SQL,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_replay_inputs_no_update
    BEFORE UPDATE ON adaptive_solver_checkpoint_replay_inputs
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint replay inputs are immutable'); END
    """,
    """
    CREATE TRIGGER adaptive_solver_checkpoint_replay_inputs_no_delete
    BEFORE DELETE ON adaptive_solver_checkpoint_replay_inputs
    BEGIN SELECT RAISE(ABORT, 'solver checkpoint replay inputs are immutable'); END
    """,
)
SOLVER_CHECKPOINT_TABLES = (
    *SOLVER_CHECKPOINT_V2_TABLES,
    "adaptive_solver_checkpoint_replay_inputs",
)
SOLVER_CHECKPOINT_SCHEMA_SQL = (
    *SOLVER_CHECKPOINT_V2_SCHEMA_SQL,
    *SOLVER_CHECKPOINT_V3_ADDITIONAL_SQL,
)


def create_solver_checkpoint_v2_schema(connection: sqlite3.Connection) -> None:
    for statement in SOLVER_CHECKPOINT_V2_SCHEMA_SQL:
        connection.execute(statement)


def create_solver_checkpoint_v3_additions(connection: sqlite3.Connection) -> None:
    for statement in SOLVER_CHECKPOINT_V3_ADDITIONAL_SQL:
        connection.execute(statement)


def create_solver_checkpoint_schema(connection: sqlite3.Connection) -> None:
    for statement in SOLVER_CHECKPOINT_SCHEMA_SQL:
        connection.execute(statement)


def solver_checkpoint_v2_schema_signature(
    connection: sqlite3.Connection,
) -> SchemaSignature:
    return owned_sqlite_schema_signature(
        connection,
        prefix=SOLVER_CHECKPOINT_PREFIX,
        table_names=SOLVER_CHECKPOINT_V2_TABLES,
    )


def solver_checkpoint_schema_signature(
    connection: sqlite3.Connection,
) -> SchemaSignature:
    return owned_sqlite_schema_signature(
        connection,
        prefix=SOLVER_CHECKPOINT_PREFIX,
        table_names=SOLVER_CHECKPOINT_TABLES,
    )


@lru_cache(maxsize=1)
def canonical_solver_checkpoint_v2_schema_signature() -> SchemaSignature:
    connection = sqlite3.connect(":memory:")
    try:
        create_solver_checkpoint_v2_schema(connection)
        return solver_checkpoint_v2_schema_signature(connection)
    finally:
        connection.close()


@lru_cache(maxsize=1)
def canonical_solver_checkpoint_schema_signature() -> SchemaSignature:
    connection = sqlite3.connect(":memory:")
    try:
        create_solver_checkpoint_schema(connection)
        return solver_checkpoint_schema_signature(connection)
    finally:
        connection.close()
