from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.fastapi_app.agui.auth import Principal
from backend.fastapi_app.agui.store import (
    AGUI_EVENT_STORE_SCHEMA_VERSION,
    EventStore,
    TextToSqlHistoryConflictError,
)
from workflow.models import TextToSqlTerminalResult, TextToSqlTerminalStatus
from workflow.result_identity import workflow_result_event_key


def _principal(subject: str, *, tenant: str = "tenant-1", admin: bool = False) -> Principal:
    roles = {"user"}
    if admin:
        roles.add("admin")
    return Principal(subject=subject, tenant_id=tenant, roles=frozenset(roles))


def _terminal(run_id: str) -> TextToSqlTerminalResult:
    return TextToSqlTerminalResult(
        run_id=run_id,
        status=TextToSqlTerminalStatus.CANCELLED,
        reason_code="CANCELLED",
        sql="",
        generated=False,
        approved=False,
        executed=False,
        dry_run=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        error=None,
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
    )


def _workflow_result_payload(run_id: str, run_incarnation: str) -> dict:
    terminal = _terminal(run_id).to_mapping()
    return {
        "run_id": run_id,
        "thread_id": f"thread-{run_id}",
        "run_incarnation": run_incarnation,
        "event_key": workflow_result_event_key(run_id, run_incarnation),
        "status": "cancelled",
        "success": False,
        "terminal_outcome": terminal,
        "result": terminal,
        "error": "run cancelled",
        "artifacts": {"terminal_outcome": terminal},
        "snapshot": {
            "run_incarnation": run_incarnation,
            "parameters": {"dialect": "postgres"},
        },
    }


def _register_terminal_run(store: EventStore, principal: Principal, run_id: str) -> None:
    store.create_run(
        run_id,
        f"thread-{run_id}",
        principal,
        run_kind="text_to_sql",
        status="cancelled",
    )


def _upsert(
    store: EventStore,
    principal: Principal,
    run_id: str,
    created_at_ms: int,
    *,
    dialect: str = "postgres",
    profile_name: str = "strict",
):
    return store.upsert_text_to_sql_history(
        principal,
        _terminal(run_id),
        created_at_ms=created_at_ms,
        dialect=dialect,
        profile_name=profile_name,
    )


def test_history_schema_is_additive_and_owner_keyed(tmp_path) -> None:
    path = tmp_path / "events.db"
    store = EventStore(str(path))
    try:
        with sqlite3.connect(path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            columns = connection.execute(
                "PRAGMA table_info(text_to_sql_history)"
            ).fetchall()
            indexes = {
                row[1]
                for row in connection.execute(
                    "PRAGMA index_list(text_to_sql_history)"
                )
            }
            import_columns = connection.execute(
                "PRAGMA table_info(text_to_sql_history_imports)"
            ).fetchall()
        assert version == AGUI_EVENT_STORE_SCHEMA_VERSION
        assert [(row[1], row[5]) for row in columns] == [
            ("tenant_id", 1),
            ("owner_subject", 2),
            ("run_id", 3),
            ("created_at_ms", 0),
            ("status", 0),
            ("dialect", 0),
            ("profile_name", 0),
            ("terminal_snapshot_json", 0),
        ]
        assert [(row[1], row[5]) for row in import_columns] == [
            ("source_identity", 1),
            ("source_path", 0),
            ("content_sha256", 0),
            ("completed_at_ms", 0),
            ("imported", 0),
            ("duplicates", 0),
            ("quarantined_malformed", 0),
            ("quarantined_unowned", 0),
            ("quarantined_invalid", 0),
        ]
        assert "idx_text_to_sql_history_owner_created" in indexes
    finally:
        store.close()


def test_v4_store_migrates_history_tables_additively(tmp_path) -> None:
    path = tmp_path / "events.db"
    initial = EventStore(str(path))
    initial.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_text_to_sql_history_owner_created")
        connection.execute("DROP TABLE text_to_sql_history")
        connection.execute("DROP TABLE text_to_sql_history_quarantine")
        connection.execute("PRAGMA user_version=4")

    migrated = EventStore(str(path))
    try:
        with sqlite3.connect(path) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        assert version == AGUI_EVENT_STORE_SCHEMA_VERSION
        assert {
            "text_to_sql_history",
            "text_to_sql_history_quarantine",
        } <= names
    finally:
        migrated.close()


def test_v6_store_migrates_history_import_journal_additively(tmp_path) -> None:
    path = tmp_path / "events.db"
    initial = EventStore(str(path))
    initial.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS text_to_sql_history_imports")
        connection.execute("PRAGMA user_version=6")

    migrated = EventStore(str(path))
    try:
        with sqlite3.connect(path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            table = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'text_to_sql_history_imports'
                """
            ).fetchone()
        assert version == AGUI_EVENT_STORE_SCHEMA_VERSION == 8
        assert table == ("text_to_sql_history_imports",)
    finally:
        migrated.close()


def test_history_upsert_is_typed_idempotent_and_owner_scoped(tmp_path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    alice = _principal("alice")
    bob = _principal("bob")
    try:
        _register_terminal_run(store, alice, "run-1")
        first = _upsert(store, alice, "run-1", 1000)
        replay = _upsert(store, alice, "run-1", 1000)

        assert replay == first
        assert first.tenant_id == "tenant-1"
        assert first.owner_subject == "alice"
        assert first.status is TextToSqlTerminalStatus.CANCELLED
        assert first.terminal_result == _terminal("run-1")
        assert store.list_text_to_sql_history(alice) == [first]
        assert store.list_text_to_sql_history(bob) == []

        with pytest.raises(PermissionError, match="owner"):
            _upsert(store, bob, "run-1", 1000)
        with pytest.raises(TextToSqlHistoryConflictError):
            _upsert(store, alice, "run-1", 1000, dialect="mysql")
    finally:
        store.close()


def test_terminal_finalize_atomically_writes_and_replays_history(tmp_path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    alice = _principal("alice")
    payload = _workflow_result_payload("run-1", "inc-1")
    try:
        store.create_run(
            "run-1",
            "thread-run-1",
            alice,
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1",
            "inc-1",
            "thread-run-1",
            "text_to_sql",
        )

        first_seq = store.finalize_run_with_event("run-1", payload)
        replay_seq = store.finalize_run_with_event("run-1", payload)

        assert replay_seq == first_seq
        entries = store.list_text_to_sql_history(alice)
        assert len(entries) == 1
        assert entries[0].run_id == "run-1"
        assert entries[0].dialect == "postgres"
        assert entries[0].profile_name == "unknown"
        assert entries[0].terminal_result == _terminal("run-1")
    finally:
        store.close()


def test_terminal_finalize_rolls_back_when_history_insert_fails(tmp_path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    alice = _principal("alice")
    payload = _workflow_result_payload("run-1", "inc-1")
    try:
        store.create_run(
            "run-1",
            "thread-run-1",
            alice,
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1",
            "inc-1",
            "thread-run-1",
            "text_to_sql",
        )
        store._conn.execute(
            """
            CREATE TEMP TRIGGER fail_history_insert
            BEFORE INSERT ON text_to_sql_history
            BEGIN
                SELECT RAISE(ABORT, 'injected history failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="history failure"):
            store.finalize_run_with_event("run-1", payload)

        assert store.get_run("run-1").status == "pending"
        assert store.latest_seq("run-1") is None
        assert store.list_text_to_sql_history(alice) == []
    finally:
        store.close()


def test_history_order_pagination_clear_analytics_and_prune_are_scoped(tmp_path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    alice = _principal("alice")
    bob = _principal("bob")
    try:
        for run_id, created_at_ms, dialect in (
            ("run-a", 1000, "postgres"),
            ("run-b", 2000, "sqlite"),
            ("run-c", 2000, "postgres"),
        ):
            _register_terminal_run(store, alice, run_id)
            _upsert(store, alice, run_id, created_at_ms, dialect=dialect)
        _register_terminal_run(store, bob, "run-bob")
        _upsert(store, bob, "run-bob", 500, dialect="mysql")

        page = store.list_text_to_sql_history(alice, limit=2, offset=1)
        assert [entry.run_id for entry in page] == ["run-b", "run-a"]
        assert store.text_to_sql_history_analytics(alice) == {
            "total": 3,
            "statuses": {"cancelled": 3},
            "dialects": {"postgres": 2, "sqlite": 1},
            "profiles": {"strict": 3},
        }
        assert store.prune_text_to_sql_history(alice, before_created_at_ms=1500) == 1
        assert [entry.run_id for entry in store.list_text_to_sql_history(alice)] == [
            "run-c",
            "run-b",
        ]
        assert store.clear_text_to_sql_history(alice) == 2
        assert [entry.run_id for entry in store.list_text_to_sql_history(bob)] == [
            "run-bob"
        ]
    finally:
        store.close()


def test_admin_cross_owner_access_is_explicit_and_role_guarded(tmp_path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    alice = _principal("alice")
    bob = _principal("bob", tenant="tenant-2")
    admin = _principal("operator", tenant="ops", admin=True)
    try:
        _register_terminal_run(store, alice, "run-a")
        _register_terminal_run(store, bob, "run-b")
        _upsert(store, alice, "run-a", 1000)
        _upsert(store, bob, "run-b", 2000)

        with pytest.raises(PermissionError, match="admin"):
            store.admin_list_text_to_sql_history(alice)
        assert [
            (entry.tenant_id, entry.owner_subject, entry.run_id)
            for entry in store.admin_list_text_to_sql_history(admin)
        ] == [
            ("tenant-2", "bob", "run-b"),
            ("tenant-1", "alice", "run-a"),
        ]
        assert store.admin_text_to_sql_history_analytics(
            admin,
            tenant_id="tenant-1",
            owner_subject="alice",
        )["total"] == 1
        assert store.admin_clear_text_to_sql_history(
            admin,
            tenant_id="tenant-2",
            owner_subject="bob",
        ) == 1
        assert store.list_text_to_sql_history(alice)[0].run_id == "run-a"
    finally:
        store.close()


def test_concurrent_same_run_upserts_produce_one_row(tmp_path) -> None:
    path = tmp_path / "events.db"
    store = EventStore(str(path))
    second_store = EventStore(str(path))
    alice = _principal("alice")
    try:
        _register_terminal_run(store, alice, "run-1")
        with ThreadPoolExecutor(max_workers=8) as executor:
            entries = list(
                executor.map(
                    lambda index: _upsert(
                        store if index % 2 == 0 else second_store,
                        alice,
                        "run-1",
                        1000,
                    ),
                    range(24),
                )
            )
        assert len({entry.run_id for entry in entries}) == 1
        assert len(store.list_text_to_sql_history(alice)) == 1
    finally:
        second_store.close()
        store.close()


def test_legacy_import_uses_authoritative_owner_and_quarantines_bad_rows(
    tmp_path,
) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    alice = _principal("alice")
    admin = _principal("operator", tenant="ops", admin=True)
    try:
        _register_terminal_run(store, alice, "run-valid")
        valid = json.dumps(
            {
                "run_id": "run-valid",
                "created_at_ms": 1000,
                "dialect": "postgres",
                "profile_name": "strict",
                "terminal_snapshot": _terminal("run-valid").to_mapping(),
                "owner_subject": "attacker-controlled",
                "tenant_id": "attacker-controlled",
            }
        )
        unknown = json.dumps(
            {
                "run_id": "run-unknown",
                "created_at_ms": 1000,
                "dialect": "postgres",
                "profile_name": "strict",
                "terminal_snapshot": _terminal("run-unknown").to_mapping(),
            }
        )

        result = store.import_legacy_text_to_sql_history(
            [valid, "{broken-json", unknown]
        )

        assert result.imported == 1
        assert result.duplicates == 0
        assert result.quarantined_malformed == 1
        assert result.quarantined_unowned == 1
        entry = store.list_text_to_sql_history(alice)[0]
        assert (entry.owner_subject, entry.tenant_id) == ("alice", "tenant-1")
        assert store.admin_text_to_sql_history_quarantine_counts(admin) == {
            "MALFORMED_JSON": 1,
            "UNOWNED_RUN": 1,
        }
    finally:
        store.close()


def test_legacy_import_once_does_not_duplicate_history_or_quarantine(
    tmp_path,
) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    alice = _principal("alice")
    admin = _principal("operator", tenant="ops", admin=True)
    try:
        _register_terminal_run(store, alice, "run-valid")
        valid = json.dumps(
            {
                "run_id": "run-valid",
                "created_at_ms": 1000,
                "dialect": "postgres",
                "profile_name": "strict",
                "terminal_snapshot": _terminal("run-valid").to_mapping(),
            }
        )
        lines = [valid, "{broken-json"]
        arguments = {
            "source_identity": "a" * 64,
            "source_path": "/srv/app/logs/sql_history.jsonl",
            "content_sha256": "b" * 64,
        }

        first = store.import_legacy_text_to_sql_history_once(lines, **arguments)
        replay = store.import_legacy_text_to_sql_history_once(lines, **arguments)

        assert first.imported == 1
        assert first.quarantined_malformed == 1
        assert first.already_completed is False
        assert replay == type(first)(
            imported=1,
            duplicates=0,
            quarantined_malformed=1,
            quarantined_unowned=0,
            quarantined_invalid=0,
            already_completed=True,
        )
        assert len(store.list_text_to_sql_history(alice)) == 1
        assert store.admin_text_to_sql_history_quarantine_counts(admin) == {
            "MALFORMED_JSON": 1,
        }
        marker_count = store._conn.execute(
            "SELECT COUNT(*) FROM text_to_sql_history_imports"
        ).fetchone()[0]
        assert marker_count == 1
    finally:
        store.close()
