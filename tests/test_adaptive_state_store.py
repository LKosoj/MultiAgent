from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
from custom_tools.text_to_sql.adaptive.replay_inputs import (
    ResearchTerminalReplayInput,
)
from workflow.adaptive_state_store import (
    AdaptiveActionPhase,
    AdaptiveArtifactDigestMismatchError,
    AdaptiveCheckpointCasError,
    AdaptiveCheckpointConflictError,
    AdaptiveCheckpointError,
    AdaptiveCheckpointIntegrityError,
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.state_manager import SQLiteWorkflowStore


def _key(revision: int = 0, incarnation: str = "inc-1") -> AdaptiveCheckpointKey:
    return AdaptiveCheckpointKey(
        "run-1", incarnation, AdaptiveLoopKind.RESEARCH, revision
    )


def _digest(marker: str) -> str:
    return "sha256:" + marker * 64


def _record_terminal(
    store: AdaptiveStateStore,
    key: AdaptiveCheckpointKey,
    *,
    expected_revision: int | None,
    action: object,
    semantic_repair_continuation: bool = False,
):
    return store.record_replayable_terminal(
        key,
        expected_revision=expected_revision,
        action=action,
        replay_input=ResearchTerminalReplayInput(
            freshness_context=FreshnessContext(
                evaluated_at=datetime(2026, 8, 2, tzinfo=UTC),
                run_id=key.run_id,
                run_incarnation=key.run_incarnation,
                schema_namespace_version="schema:0123456789abcdef",
            )
        ),
        semantic_repair_continuation=semantic_repair_continuation,
    )


class _StringSubclass(str):
    pass


class _IntegerSubclass(int):
    pass


class _CustomKey:
    pass


def test_planned_observed_terminal_are_append_only_and_resume_after_crash(
    tmp_path,
) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    key = _key()
    planned = store.record_planned(
        key, expected_revision=None, action={"tool": "inspect"}
    )
    assert store.get_snapshot(key).planned == planned
    assert store.get_snapshot(key).observed is None
    assert AdaptiveStateStore(store.db_path).get_snapshot(key).planned == planned

    observed = store.record_observed(key, expected_revision=0, action={"rows": 1})
    assert AdaptiveStateStore(store.db_path).get_snapshot(key).observed == observed
    terminal = _record_terminal(
        store, key, expected_revision=0, action={"reason": "complete"}
    )
    snapshot = store.get_snapshot(key)
    assert snapshot.observed == observed
    assert snapshot.terminal == terminal
    assert [event.phase for event in store.list_events(key)] == [
        AdaptiveActionPhase.PLANNED,
        AdaptiveActionPhase.OBSERVED,
        AdaptiveActionPhase.TERMINAL,
    ]

    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute("DELETE FROM adaptive_checkpoint_events")


def test_retry_is_idempotent_but_conflicting_duplicate_and_cas_fail(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    key = _key()
    first = store.record_planned(
        key, expected_revision=None, action={"tool": "inspect"}
    )
    assert (
        store.record_planned(key, expected_revision=None, action={"tool": "inspect"})
        == first
    )
    with pytest.raises(AdaptiveCheckpointConflictError):
        store.record_planned(key, expected_revision=None, action={"tool": "other"})
    with pytest.raises(AdaptiveCheckpointCasError):
        store.record_planned(_key(1), expected_revision=None, action={"tool": "next"})


@pytest.mark.parametrize(
    "invalid_key",
    [1, True, 1.5, _StringSubclass("custom"), _CustomKey()],
)
@pytest.mark.parametrize("nested", [False, True])
def test_action_rejects_non_exact_string_keys_before_any_write(
    tmp_path,
    invalid_key,
    nested,
) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    action = {invalid_key: "value"}
    if nested:
        action = {"nested": action}

    with pytest.raises(ValueError, match="canonical JSON"):
        store.record_planned(_key(), expected_revision=None, action=action)

    assert store.get_snapshot(_key()).planned is None
    with sqlite3.connect(store.db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM adaptive_checkpoint_events"
            ).fetchone()[0]
            == 0
        )


def test_action_preserves_bool_int_and_float_as_distinct_canonical_values(
    tmp_path,
) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    events = []
    for incarnation, value in (("bool", True), ("int", 1), ("float", 1.0)):
        event = store.record_planned(
            _key(incarnation=incarnation),
            expected_revision=None,
            action={"value": value},
        )
        events.append(event)
        stored = store.get_snapshot(_key(incarnation=incarnation)).planned
        assert stored is not None
        assert type(stored.action["value"]) is type(value)

    assert len({event.action_digest for event in events}) == 3


@pytest.mark.parametrize("invalid_value", [float("nan"), float("inf"), float("-inf")])
def test_action_rejects_nonfinite_numbers_before_any_write(
    tmp_path, invalid_value
) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")

    with pytest.raises(ValueError, match="canonical JSON"):
        store.record_planned(
            _key(),
            expected_revision=None,
            action={"nested": [invalid_value]},
        )

    assert store.get_snapshot(_key()).planned is None


def test_action_rejects_custom_scalar_subclasses_before_any_write(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")

    with pytest.raises(ValueError, match="canonical JSON"):
        store.record_planned(
            _key(),
            expected_revision=None,
            action={"value": _IntegerSubclass(1)},
        )

    assert store.get_snapshot(_key()).planned is None


def test_next_planned_revision_requires_observed_and_terminal_closes_loop(
    tmp_path,
) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    first = _key()
    second = _key(1)
    store.record_planned(first, expected_revision=None, action={"tool": "first"})
    with pytest.raises(AdaptiveCheckpointCasError, match="previous revision"):
        store.record_planned(second, expected_revision=0, action={"tool": "next"})
    store.record_observed(first, expected_revision=0, action={"ok": True})
    _record_terminal(
        store,
        first,
        expected_revision=0,
        action={"reason": "complete"},
    )
    with pytest.raises(AdaptiveCheckpointCasError, match="closes"):
        store.record_planned(second, expected_revision=0, action={"tool": "next"})


def test_semantic_repair_continues_in_new_segment_without_changing_terminal(
    tmp_path,
) -> None:
    path = tmp_path / "state.db"
    store = AdaptiveStateStore(path)
    first = _key()
    original_terminal = _record_terminal(
        store,
        first,
        expected_revision=None,
        action={"reason": "complete"},
    )

    continued = store.record_planned(
        _key(1),
        expected_revision=0,
        action={"tool": "inspect"},
        semantic_repair_continuation=True,
    )
    restarted = AdaptiveStateStore(path)
    assert restarted.record_planned(
        _key(1),
        expected_revision=0,
        action={"tool": "inspect"},
        semantic_repair_continuation=True,
    ) == continued
    restarted.record_observed(
        _key(1), expected_revision=1, action={"rows": 1}
    )
    _record_terminal(
        restarted,
        _key(2),
        expected_revision=1,
        action={"reason": "complete"},
    )

    assert restarted.get_snapshot(first).terminal == original_terminal
    assert [
        (event.key.revision, event.phase)
        for event in restarted.load_run_events(
            first.run_id,
            first.run_incarnation,
            first.loop_kind,
        )
    ] == [
        (0, AdaptiveActionPhase.TERMINAL),
        (1, AdaptiveActionPhase.PLANNED),
        (1, AdaptiveActionPhase.OBSERVED),
        (2, AdaptiveActionPhase.TERMINAL),
    ]


def test_semantic_repair_may_close_new_segment_without_another_probe(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    _record_terminal(
        store,
        _key(),
        expected_revision=None,
        action={"reason": "complete"},
    )

    _record_terminal(
        store,
        _key(1),
        expected_revision=0,
        action={"reason": "complete"},
        semantic_repair_continuation=True,
    )

    assert [
        event.key.revision
        for event in store.load_run_events(
            "run-1", "inc-1", AdaptiveLoopKind.RESEARCH
        )
        if event.phase is AdaptiveActionPhase.TERMINAL
    ] == [0, 1]


def test_terminal_can_close_the_next_revision_without_a_tool_call(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    first = _key()
    terminal = _record_terminal(
        store,
        first,
        expected_revision=None,
        action={"contract": "adaptive_final_answer_v1", "answer": "done"},
    )

    assert (
        _record_terminal(
            store,
            first,
            expected_revision=None,
            action={"contract": "adaptive_final_answer_v1", "answer": "done"},
        )
        == terminal
    )
    with pytest.raises(AdaptiveCheckpointConflictError):
        store.record_terminal(
            first,
            expected_revision=None,
            action={"contract": "adaptive_final_answer_v1", "answer": "other"},
        )


def test_terminal_after_observed_results_uses_the_next_revision(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    first = _key()
    terminal_key = _key(1)
    store.record_planned(first, expected_revision=None, action={"tool": "inspect"})
    store.record_observed(first, expected_revision=0, action={"ok": True})

    _record_terminal(
        store,
        terminal_key,
        expected_revision=0,
        action={"contract": "adaptive_final_answer_v1", "answer": "done"},
    )

    assert store.get_snapshot(terminal_key).terminal is not None


@pytest.mark.parametrize("phase", ["planned", "observed", "terminal"])
@pytest.mark.parametrize(
    "corrupt_digest",
    ["invalid", "sha256:" + "0" * 64],
)
def test_read_rejects_invalid_or_mismatched_action_digest(
    tmp_path,
    phase,
    corrupt_digest,
) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    key = _key()
    store.record_planned(key, expected_revision=None, action={"tool": "inspect"})
    store.record_observed(key, expected_revision=0, action={"ok": True})
    _record_terminal(
        store,
        key,
        expected_revision=0,
        action={"reason": "complete"},
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TRIGGER adaptive_checkpoint_events_no_update")
        connection.execute(
            """
            UPDATE adaptive_checkpoint_events SET action_digest = ?
            WHERE run_id = ? AND run_incarnation = ? AND loop_kind = ?
              AND revision = ? AND phase = ?
            """,
            (
                corrupt_digest,
                key.run_id,
                key.run_incarnation,
                key.loop_kind.value,
                key.revision,
                phase,
            ),
        )

    with pytest.raises(AdaptiveCheckpointIntegrityError, match="action digest"):
        store.get_snapshot(key)


def test_read_rejects_noncanonical_action_json_even_with_matching_digest(
    tmp_path,
) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    key = _key()
    store.record_planned(key, expected_revision=None, action={"tool": "inspect"})
    noncanonical = '{"tool": "inspect"}'
    matching_digest = (
        f"sha256:{hashlib.sha256(noncanonical.encode('utf-8')).hexdigest()}"
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TRIGGER adaptive_checkpoint_events_no_update")
        connection.execute(
            """
            UPDATE adaptive_checkpoint_events
            SET action_json = ?, action_digest = ?
            WHERE run_id = ? AND run_incarnation = ? AND loop_kind = ?
              AND revision = ? AND phase = 'planned'
            """,
            (
                noncanonical,
                matching_digest,
                key.run_id,
                key.run_incarnation,
                key.loop_kind.value,
                key.revision,
            ),
        )

    with pytest.raises(AdaptiveCheckpointIntegrityError, match="action digest"):
        store.get_snapshot(key)


def test_planned_expected_revision_and_created_timestamp_are_strict(
    tmp_path, monkeypatch
) -> None:
    import workflow.adaptive_state_store as state_module

    store = AdaptiveStateStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match="expected_revision"):
        store.record_planned(_key(), expected_revision=True, action={"tool": "inspect"})
    with pytest.raises(ValueError, match="expected_revision"):
        store.record_planned(_key(), expected_revision=0.0, action={"tool": "inspect"})
    monkeypatch.setattr(state_module.time, "time_ns", lambda: True)
    with pytest.raises(ValueError, match="created_at_ns"):
        store.record_planned(_key(), expected_revision=None, action={"tool": "inspect"})
    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="created_at_ns"):
            connection.execute(
                """
                INSERT INTO adaptive_checkpoint_events (
                    run_id, run_incarnation, loop_kind, revision, phase, action_json,
                    action_digest, artifact_digest, created_at_ns
                ) VALUES ('run-direct', 'inc-direct', 'research', 0, 'planned', '{}', 'sha256:00', NULL, 'bad')
                """
            )


def test_two_writers_compare_and_swap_allows_only_one(tmp_path) -> None:
    path = tmp_path / "state.db"
    store = AdaptiveStateStore(path)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def write(marker: str) -> None:
        barrier.wait(timeout=10)
        try:
            store.record_planned(
                _key(), expected_revision=None, action={"writer": marker}
            )
            outcomes.append("written")
        except (AdaptiveCheckpointCasError, AdaptiveCheckpointConflictError):
            outcomes.append("rejected")

    threads = [threading.Thread(target=write, args=(marker,)) for marker in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["rejected", "written"]


def test_artifact_digest_must_match_planned_action(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    key = _key()
    store.record_planned(
        key,
        expected_revision=None,
        action={"tool": "probe"},
        artifact_digest=_digest("a"),
    )
    with pytest.raises(AdaptiveArtifactDigestMismatchError):
        store.record_observed(
            key, expected_revision=0, action={"ok": True}, artifact_digest=_digest("b")
        )
    assert store.get_snapshot(key).observed is None


def test_incarnations_are_isolated_and_old_workflow_state_stays_readable(
    tmp_path,
) -> None:
    db_path = tmp_path / "state.db"
    legacy = SQLiteWorkflowStore(str(db_path))
    assert legacy.get_adaptive_state_store().db_path == str(db_path)
    store = AdaptiveStateStore(db_path)
    store.record_planned(
        _key(incarnation="inc-a"), expected_revision=None, action={"a": 1}
    )
    store.record_planned(
        _key(incarnation="inc-b"), expected_revision=None, action={"b": 1}
    )
    assert store.get_snapshot(_key(incarnation="inc-a")).planned is not None
    assert store.get_snapshot(_key(incarnation="inc-b")).planned is not None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'workflow_checkpoints'"
        ).fetchone()


def test_lazy_adapter_returns_one_instance_to_all_callers(tmp_path) -> None:
    workflow_store = SQLiteWorkflowStore(str(tmp_path / "state.db"))
    barrier = threading.Barrier(16)
    stores: list[AdaptiveStateStore] = []

    def get_store() -> None:
        barrier.wait(timeout=10)
        stores.append(workflow_store.get_adaptive_state_store())

    threads = [threading.Thread(target=get_store) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(store) for store in stores}) == 1


def test_memory_store_keeps_one_database_alive_until_close() -> None:
    store = AdaptiveStateStore(":memory:")
    key = _key()
    store.record_planned(key, expected_revision=None, action={"tool": "inspect"})
    assert store.get_snapshot(key).planned is not None
    anchor = store._memory_anchor
    assert anchor is not None
    store.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        anchor.execute("SELECT 1")


def test_close_is_idempotent_and_rejects_all_later_operations(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    store.close()
    store.close()

    with pytest.raises(AdaptiveCheckpointError, match="store is closed"):
        store.get_snapshot(_key())
    with pytest.raises(AdaptiveCheckpointError, match="store is closed"):
        store.record_planned(_key(), expected_revision=None, action={"tool": "inspect"})


def test_temporary_connections_commit_rollback_and_close_for_file_store(
    tmp_path, monkeypatch
) -> None:
    import workflow.adaptive_state_store as state_module

    connections: list[sqlite3.Connection] = []
    real_connect = state_module.sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    def track_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(state_module.sqlite3, "connect", track_connect)
    store = AdaptiveStateStore(tmp_path / "state.db")
    key = _key()

    with pytest.raises(RuntimeError, match="rollback"):
        with store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO adaptive_checkpoint_heads VALUES (?, ?, ?, ?)",
                (key.run_id, key.run_incarnation, key.loop_kind.value, key.revision),
            )
            raise RuntimeError("rollback")
    assert store.get_snapshot(key).planned is None

    store.record_planned(key, expected_revision=None, action={"tool": "inspect"})
    assert store.get_snapshot(key).planned is not None
    assert connections
    assert all(connection.close_calls == 1 for connection in connections)


@pytest.mark.parametrize("failing_pragma", ["foreign_keys", "journal_mode"])
def test_connect_closes_connection_when_pragma_setup_fails(
    monkeypatch, failing_pragma
) -> None:
    import workflow.adaptive_state_store as state_module

    class FakeConnection:
        row_factory = None

        def __init__(self) -> None:
            self.close_calls = 0

        def execute(self, statement: str) -> None:
            if failing_pragma in statement:
                raise RuntimeError(f"{failing_pragma} failure")

        def close(self) -> None:
            self.close_calls += 1

    connection = FakeConnection()
    monkeypatch.setattr(
        state_module.sqlite3, "connect", lambda *_args, **_kwargs: connection
    )
    store = object.__new__(AdaptiveStateStore)
    store.db_path = "state.db"
    store._memory_uri = None

    with pytest.raises(RuntimeError, match=f"{failing_pragma} failure"):
        store._connect()
    assert connection.close_calls == 1


def test_connect_leaves_successful_connection_open_for_its_context_manager(
    monkeypatch,
) -> None:
    import workflow.adaptive_state_store as state_module

    class FakeConnection:
        row_factory = None

        def __init__(self) -> None:
            self.close_calls = 0

        def execute(self, _statement: str) -> None:
            return None

        def close(self) -> None:
            self.close_calls += 1

    connection = FakeConnection()
    monkeypatch.setattr(
        state_module.sqlite3, "connect", lambda *_args, **_kwargs: connection
    )
    store = object.__new__(AdaptiveStateStore)
    store.db_path = "state.db"
    store._memory_uri = None

    assert store._connect() is connection
    assert connection.close_calls == 0


def test_terminal_requires_observed_checkpoint(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "state.db")
    key = _key()
    store.record_planned(key, expected_revision=None, action={"tool": "inspect"})
    with pytest.raises(AdaptiveCheckpointCasError, match="observed"):
        _record_terminal(
            store,
            key,
            expected_revision=0,
            action={"reason": "complete"},
        )
