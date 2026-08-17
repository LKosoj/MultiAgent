"""Versioned migration tests for the adaptive checkpoint journal."""

from __future__ import annotations

import sqlite3

import pytest
import workflow.adaptive_state_store as state_module

from workflow._adaptive_solver_checkpoint_sql import SOLVER_CHECKPOINT_TABLES
from workflow.adaptive_state_store import (
    ADAPTIVE_STATE_STORE_SCHEMA_VERSION,
    AdaptiveCheckpointKey,
    AdaptiveCheckpointMigrationError,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)


def _key() -> AdaptiveCheckpointKey:
    return AdaptiveCheckpointKey("run-1", "inc-1", AdaptiveLoopKind.RESEARCH, 0)


def _drop_solver_checkpoint_schema(connection: sqlite3.Connection) -> None:
    for table_name in reversed(SOLVER_CHECKPOINT_TABLES):
        connection.execute(f"DROP TABLE {table_name}")


def _make_v1_store(path, *, drop_triggers: tuple[str, ...] = ()) -> None:
    store = AdaptiveStateStore(path)
    store.record_planned(_key(), expected_revision=None, action={"tool": "inspect"})
    store.close()
    with sqlite3.connect(path) as connection:
        _drop_solver_checkpoint_schema(connection)
        connection.execute("DROP TABLE adaptive_checkpoint_replay_inputs")
        connection.execute(
            "UPDATE adaptive_checkpoint_meta SET value = 1 WHERE key = 'schema_version'"
        )
        for trigger_name in drop_triggers:
            connection.execute(f"DROP TRIGGER {trigger_name}")


def _make_legacy_store(path, *, drop_triggers: tuple[str, ...] = ()) -> None:
    _make_v1_store(path, drop_triggers=drop_triggers)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE adaptive_checkpoint_meta")


def _schema_version(path) -> int | None:
    with sqlite3.connect(path) as connection:
        meta_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'adaptive_checkpoint_meta'"
        ).fetchone()
        if meta_exists is None:
            return None
        return connection.execute(
            "SELECT value FROM adaptive_checkpoint_meta WHERE key = 'schema_version'"
        ).fetchone()[0]


def _trigger_names(path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }


@pytest.mark.parametrize(
    "missing_triggers",
    [
        (
            "adaptive_checkpoint_events_no_update",
            "adaptive_checkpoint_events_no_delete",
            "adaptive_checkpoint_events_created_at_integer",
        ),
        ("adaptive_checkpoint_events_no_update",),
    ],
)
def test_v1_migration_restores_legacy_triggers_preserves_rows_and_restarts(
    tmp_path,
    missing_triggers,
) -> None:
    path = tmp_path / "state.db"
    _make_legacy_store(path, drop_triggers=missing_triggers)

    migrated = AdaptiveStateStore(path)
    assert _schema_version(path) == ADAPTIVE_STATE_STORE_SCHEMA_VERSION
    assert migrated.get_snapshot(_key()).planned is not None
    migrated.close()

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE adaptive_checkpoint_events SET action_json = '{}' "
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute("DELETE FROM adaptive_checkpoint_events")
        for invalid_timestamp in ("bad", -1):
            with pytest.raises(sqlite3.DatabaseError, match="created_at_ns"):
                connection.execute(
                    """
                    INSERT INTO adaptive_checkpoint_events (
                        run_id, run_incarnation, loop_kind, revision, phase, action_json,
                        action_digest, artifact_digest, created_at_ns
                    ) VALUES ('invalid', 'inc-1', 'research', 0, 'planned', '{}', 'sha256:00', NULL, ?)
                    """,
                    (invalid_timestamp,),
                )

    resumed = AdaptiveStateStore(path)
    assert resumed.get_snapshot(_key()).planned is not None
    resumed.close()


def test_legacy_migration_replaces_same_name_corrupted_trigger_without_losing_rows(
    tmp_path,
) -> None:
    path = tmp_path / "state.db"
    _make_legacy_store(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER adaptive_checkpoint_events_no_update")
        connection.execute(
            """
            CREATE TRIGGER adaptive_checkpoint_events_no_update
            BEFORE UPDATE ON adaptive_checkpoint_events
            BEGIN SELECT 1; END
            """
        )

    migrated = AdaptiveStateStore(path)
    assert migrated.get_snapshot(_key()).planned is not None
    migrated.close()
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE adaptive_checkpoint_events SET action_json = '{}' "
            )


def test_current_schema_rejects_same_name_corrupted_trigger(tmp_path) -> None:
    path = tmp_path / "state.db"
    AdaptiveStateStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER adaptive_checkpoint_events_no_delete")
        connection.execute(
            """
            CREATE TRIGGER adaptive_checkpoint_events_no_delete
            BEFORE DELETE ON adaptive_checkpoint_events
            BEGIN SELECT 1; END
            """
        )

    with pytest.raises(AdaptiveCheckpointMigrationError, match="trigger"):
        AdaptiveStateStore(path)


@pytest.mark.parametrize("phase", ["planned", "observed", "terminal"])
def test_current_schema_open_rejects_corrupt_action_digest_for_every_phase(
    tmp_path,
    phase,
) -> None:
    path = tmp_path / "state.db"
    key = AdaptiveCheckpointKey("run-1", "inc-1", AdaptiveLoopKind.SOLVER, 0)
    store = AdaptiveStateStore(path)
    store.record_planned(key, expected_revision=None, action={"tool": "inspect"})
    store.record_observed(key, expected_revision=0, action={"ok": True})
    store.record_terminal(key, expected_revision=0, action={"reason": "complete"})
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER adaptive_checkpoint_events_no_update")
        connection.execute(
            """
            UPDATE adaptive_checkpoint_events SET action_digest = ?
            WHERE phase = ?
            """,
            ("sha256:" + "0" * 64, phase),
        )
        connection.execute(
            state_module._CHECKPOINT_TRIGGER_SQL["adaptive_checkpoint_events_no_update"]
        )

    with pytest.raises(AdaptiveCheckpointMigrationError, match="action digest"):
        AdaptiveStateStore(path)


def test_legacy_migration_rejects_corrupt_action_digest(tmp_path) -> None:
    path = tmp_path / "state.db"
    _make_legacy_store(
        path,
        drop_triggers=("adaptive_checkpoint_events_no_update",),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE adaptive_checkpoint_events SET action_digest = 'invalid'"
        )

    with pytest.raises(AdaptiveCheckpointMigrationError, match="action digest"):
        AdaptiveStateStore(path)


def test_unknown_future_or_invalid_schema_version_fails_closed(tmp_path) -> None:
    path = tmp_path / "state.db"
    AdaptiveStateStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE adaptive_checkpoint_meta SET value = ? WHERE key = 'schema_version'",
            (ADAPTIVE_STATE_STORE_SCHEMA_VERSION + 1,),
        )
    with pytest.raises(AdaptiveCheckpointMigrationError, match="newer"):
        AdaptiveStateStore(path)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE adaptive_checkpoint_meta SET value = -1 WHERE key = 'schema_version'"
        )
    with pytest.raises(AdaptiveCheckpointMigrationError, match="not supported"):
        AdaptiveStateStore(path)


def test_current_schema_version_is_v3() -> None:
    assert ADAPTIVE_STATE_STORE_SCHEMA_VERSION == 3


def test_v1_to_v2_migration_preserves_generic_rows_and_creates_namespace(
    tmp_path,
) -> None:
    path = tmp_path / "state.db"
    _make_v1_store(path)

    migrated = AdaptiveStateStore(path)
    assert migrated.get_snapshot(_key()).planned is not None
    assert _schema_version(path) == ADAPTIVE_STATE_STORE_SCHEMA_VERSION
    migrated.close()

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        head_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(adaptive_solver_checkpoint_heads)"
            )
        }
    assert set(SOLVER_CHECKPOINT_TABLES) <= tables
    assert "terminal_digest" in head_columns
    assert (
        "adaptive_solver_checkpoint_heads_terminal_digest_immutable"
        in _trigger_names(path)
    )


def test_current_v2_schema_rejects_changed_terminal_marker_trigger(
    tmp_path,
) -> None:
    path = tmp_path / "state.db"
    AdaptiveStateStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DROP TRIGGER adaptive_solver_checkpoint_heads_terminal_digest_immutable"
        )
        connection.execute(
            """
            CREATE TRIGGER adaptive_solver_checkpoint_heads_terminal_digest_immutable
            BEFORE UPDATE OF terminal_digest ON adaptive_solver_checkpoint_heads
            BEGIN SELECT 1; END
            """
        )

    with pytest.raises(
        AdaptiveCheckpointMigrationError,
        match="incomplete or incompatible",
    ):
        AdaptiveStateStore(path)


def test_partial_solver_namespace_fails_closed_during_v1_migration(tmp_path) -> None:
    path = tmp_path / "state.db"
    _make_v1_store(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE adaptive_solver_checkpoint_partial (value TEXT)"
        )

    with pytest.raises(
        AdaptiveCheckpointMigrationError, match="partial or conflicting"
    ):
        AdaptiveStateStore(path)


def test_v1_migration_rejects_incompatible_generic_trigger(tmp_path) -> None:
    path = tmp_path / "state.db"
    _make_v1_store(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER adaptive_checkpoint_events_no_update")
        connection.execute(
            """
            CREATE TRIGGER adaptive_checkpoint_events_no_update
            BEFORE UPDATE ON adaptive_checkpoint_events
            BEGIN SELECT 1; END
            """
        )

    with pytest.raises(AdaptiveCheckpointMigrationError, match="trigger"):
        AdaptiveStateStore(path)


def test_current_v2_schema_rejects_changed_owned_index(tmp_path) -> None:
    path = tmp_path / "state.db"
    AdaptiveStateStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX adaptive_solver_checkpoint_execution_ast_unique")
        connection.execute(
            """
            CREATE INDEX adaptive_solver_checkpoint_execution_ast_unique
            ON adaptive_solver_checkpoint_actions (normalized_ast_digest)
            """
        )

    with pytest.raises(AdaptiveCheckpointMigrationError, match="v3 schema"):
        AdaptiveStateStore(path)


def test_interrupted_v1_to_v2_migration_rolls_back_and_resumes(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "state.db"
    _make_v1_store(path)
    real_migration = AdaptiveStateStore._migrate_v1_to_v2

    def interrupted_migration(connection) -> None:
        real_migration(connection)
        raise RuntimeError("interrupted v2 migration")

    monkeypatch.setattr(
        AdaptiveStateStore,
        "_migrate_v1_to_v2",
        staticmethod(interrupted_migration),
    )
    with pytest.raises(RuntimeError, match="interrupted v2 migration"):
        AdaptiveStateStore(path)
    assert _schema_version(path) == 1
    with sqlite3.connect(path) as connection:
        assert not {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'adaptive_solver_checkpoint_%'"
            )
        }

    monkeypatch.setattr(
        AdaptiveStateStore,
        "_migrate_v1_to_v2",
        staticmethod(real_migration),
    )
    resumed = AdaptiveStateStore(path)
    assert resumed.get_snapshot(_key()).planned is not None
    assert _schema_version(path) == ADAPTIVE_STATE_STORE_SCHEMA_VERSION
    resumed.close()


def test_interrupted_v1_migration_rolls_back_and_can_resume(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "state.db"
    _make_legacy_store(
        path,
        drop_triggers=("adaptive_checkpoint_events_no_update",),
    )
    real_migration = AdaptiveStateStore._migrate_v0_to_v1

    def interrupted_migration(connection) -> None:
        real_migration(connection)
        raise RuntimeError("interrupted migration")

    monkeypatch.setattr(
        AdaptiveStateStore,
        "_migrate_v0_to_v1",
        staticmethod(interrupted_migration),
    )
    with pytest.raises(RuntimeError, match="interrupted migration"):
        AdaptiveStateStore(path)
    assert _schema_version(path) is None
    assert "adaptive_checkpoint_events_no_update" not in _trigger_names(path)

    monkeypatch.setattr(
        AdaptiveStateStore,
        "_migrate_v0_to_v1",
        staticmethod(real_migration),
    )
    resumed = AdaptiveStateStore(path)
    assert resumed.get_snapshot(_key()).planned is not None
    assert _schema_version(path) == ADAPTIVE_STATE_STORE_SCHEMA_VERSION
    resumed.close()
    assert "adaptive_checkpoint_events_no_update" in _trigger_names(path)
