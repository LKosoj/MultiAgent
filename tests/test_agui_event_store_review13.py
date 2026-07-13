from __future__ import annotations

import json
import sqlite3

import pytest

from backend.fastapi_app.agui.store import (
    AGUI_EVENT_STORE_SCHEMA_VERSION,
    EventStore,
)


def _v2_event_key(run_id: str, run_incarnation: str) -> str:
    return (
        f"workflow-result:v2:{len(run_id)}:{run_id}:"
        f"{len(run_incarnation)}:{run_incarnation}"
    )


def _reserved_store(tmp_path, filename: str = "events.db") -> EventStore:
    store = EventStore(str(tmp_path / filename))
    assert store.reserve_workflow_run(
        "run-1",
        "inc-1",
        "session-1",
        "text_to_sql_pipeline",
    )
    return store


def _create_historical_v2_event_store(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE agui_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                run_incarnation TEXT,
                event_key TEXT
            );
            CREATE TABLE agui_runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                owner_subject TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                roles TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            );
            CREATE TABLE workflow_run_invocations (
                run_id TEXT PRIMARY KEY,
                run_incarnation TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            );
            CREATE INDEX idx_agui_events_run_seq
            ON agui_events(run_id, seq);
            CREATE UNIQUE INDEX idx_agui_events_event_key
            ON agui_events(event_key) WHERE event_key IS NOT NULL;
            INSERT INTO agui_runs VALUES (
                'legacy-run', 'legacy-thread', 'legacy-owner', 'legacy-tenant',
                '["user"]', 11
            );
            INSERT INTO workflow_run_invocations VALUES (
                'legacy-run', 'legacy-inc', 'legacy-session',
                'text_to_sql_pipeline', 12
            );
            INSERT INTO agui_events
                (run_id, seq, event_type, payload, created_at_ms)
            VALUES ('legacy-run', 1, 'LEGACY_EVENT', '{"legacy":true}', 13);
            PRAGMA user_version=2;
            """
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "run_id": "run-1",
            "run_incarnation": "inc-1",
            "status": "failed",
        },
        {
            "run_id": "run-1",
            "run_incarnation": "inc-1",
            "event_key": "workflow-result:run-1:inc-1",
            "status": "failed",
        },
    ],
    ids=["missing-event-key", "legacy-v1-event-key"],
)
def test_public_workflow_result_append_rejects_incomplete_or_v1_identity(
    tmp_path,
    payload,
):
    store = _reserved_store(tmp_path, f"{payload.get('event_key', 'missing')}.db")
    try:
        with pytest.raises(ValueError, match="identity|event_key|v2"):
            store.append("run-1", "WORKFLOW_RESULT", payload)
        assert list(store.list_after("run-1")) == []
    finally:
        store.close()


def test_public_workflow_result_append_accepts_full_v2_identity(tmp_path):
    store = _reserved_store(tmp_path)
    payload = {
        "run_id": "run-1",
        "run_incarnation": "inc-1",
        "event_key": _v2_event_key("run-1", "inc-1"),
        "status": "failed",
    }
    try:
        assert store.append("run-1", "WORKFLOW_RESULT", payload) == 1
        event = list(store.list_after("run-1"))[0]
        assert event.event_key == payload["event_key"]
        assert event.payload == payload
    finally:
        store.close()


def test_non_result_event_cannot_claim_workflow_result_identity_namespace(
    tmp_path,
):
    store = _reserved_store(tmp_path)
    payload = {
        "run_id": "run-1",
        "run_incarnation": "inc-1",
        "event_key": _v2_event_key("run-1", "inc-1"),
        "status": "failed",
    }
    try:
        with pytest.raises(ValueError, match="WORKFLOW_RESULT|event_key|reserved"):
            store.append("run-1", "OTHER_EVENT", payload)
        assert list(store.list_after("run-1")) == []
    finally:
        store.close()


def test_historical_v2_nonunique_run_sequence_index_migrates_to_v7(tmp_path):
    db_path = tmp_path / "events-v2.db"
    _create_historical_v2_event_store(db_path)

    store = EventStore(str(db_path))
    try:
        stored = store.get_run("legacy-run")
        assert stored is not None
        assert stored.owner_subject == "legacy-owner"
        assert store.get_workflow_run_invocation("legacy-run") is not None
        assert [event.payload for event in store.list_after("legacy-run")] == [
            {"legacy": True}
        ]
    finally:
        store.close()

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == AGUI_EVENT_STORE_SCHEMA_VERSION
        )
        run_seq_index = next(
            row
            for row in connection.execute("PRAGMA index_list(agui_events)")
            if row[1] == "idx_agui_events_run_seq"
        )
        assert run_seq_index[2] == 1
        assert [
            row[2]
            for row in connection.execute(
                "PRAGMA index_xinfo(idx_agui_events_run_seq)"
            )
            if row[5]
        ] == ["run_id", "seq"]


def test_v2_migration_rejects_unknown_run_sequence_index_shape_transactionally(
    tmp_path,
):
    db_path = tmp_path / "malformed-events-v2.db"
    _create_historical_v2_event_store(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX idx_agui_events_run_seq")
        connection.execute(
            """
            CREATE INDEX idx_agui_events_run_seq
            ON agui_events(seq, run_id)
            """
        )

    with pytest.raises(RuntimeError, match="run sequence index"):
        EventStore(str(db_path))

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        index_sql = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_agui_events_run_seq'
            """
        ).fetchone()[0]
        assert "(seq, run_id)" in index_sql
        assert connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'workflow_run_specs'
            """
        ).fetchone() is None


def test_claimed_current_event_store_accepts_equivalent_schema_whitespace(
    tmp_path,
):
    db_path = tmp_path / "schema-whitespace.db"
    EventStore(str(db_path)).close()
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX idx_agui_events_run_seq")
        connection.execute(
            """
            CREATE UNIQUE INDEX idx_agui_events_run_seq
            ON agui_events (
                run_id,
                seq
            )
            """
        )
        connection.commit()

    EventStore(str(db_path)).close()


@pytest.mark.parametrize(
    "malformation_sql",
    [
        "CREATE TABLE unexpected_state (value TEXT)",
        "CREATE VIEW unexpected_view AS SELECT run_id FROM agui_events",
        """
        CREATE TRIGGER unexpected_trigger
        AFTER INSERT ON agui_events BEGIN SELECT 1; END
        """,
        "CREATE INDEX unexpected_index ON agui_events(created_at_ms)",
        """
        ALTER TABLE agui_runs RENAME TO agui_runs_old;
        CREATE TABLE agui_runs (
            run_id TEXT PRIMARY KEY CHECK (length(run_id) > 0),
            thread_id TEXT NOT NULL,
            owner_subject TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            roles TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        );
        DROP TABLE agui_runs_old;
        """,
    ],
    ids=[
        "unexpected-table",
        "unexpected-view",
        "unexpected-trigger",
        "unexpected-index",
        "altered-table-sql",
    ],
)
def test_claimed_current_event_store_rejects_unapproved_sqlite_master_semantics(
    tmp_path,
    malformation_sql,
):
    db_path = tmp_path / "schema.db"
    EventStore(str(db_path)).close()
    with sqlite3.connect(db_path) as connection:
        connection.executescript(malformation_sql)
        connection.commit()

    with pytest.raises(RuntimeError, match="schema|object|SQL|sql"):
        EventStore(str(db_path))


def test_existing_v1_primary_result_remains_raw_readable_after_upgrade(tmp_path):
    db_path = tmp_path / "legacy-primary.db"
    EventStore(str(db_path)).close()
    payload = {
        "run_id": "legacy:run",
        "run_incarnation": "legacy:inc",
        "event_key": "workflow-result:legacy:run:legacy:inc",
        "status": "failed",
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agui_events
                (run_id, seq, event_type, payload, created_at_ms,
                 run_incarnation, event_key)
            VALUES (?, 1, 'WORKFLOW_RESULT', ?, 1, ?, ?)
            """,
            (
                payload["run_id"],
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                payload["run_incarnation"],
                payload["event_key"],
            ),
        )

    store = EventStore(str(db_path))
    try:
        event = list(store.list_after(payload["run_id"]))[0]
        assert event.event_key == payload["event_key"]
        assert event.payload == payload
    finally:
        store.close()
