from __future__ import annotations

import json
import os
import sqlite3

import pytest

from workflow.result_identity import workflow_result_event_key
from workflow.result_outbox import (
    WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION,
    WorkflowResultOutbox,
)


def _payload(run_id: str = "run-1", incarnation: str = "inc-1") -> dict[str, str]:
    return {
        "run_id": run_id,
        "run_incarnation": incarnation,
        "event_key": workflow_result_event_key(run_id, incarnation),
        "status": "failed",
    }


def _create_exact_v2_outbox(path, *, malformed: bool = False) -> dict[str, str]:
    payload = _payload("run-v2", "inc-v2")
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE workflow_result_outbox (
                enqueue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL
            );
            CREATE TABLE workflow_result_outbox_dead_letter (
                dead_letter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                quarantined_at_ms INTEGER NOT NULL
            );
            CREATE INDEX idx_workflow_result_outbox_run
                ON workflow_result_outbox(run_id, run_incarnation, enqueue_seq);
            CREATE UNIQUE INDEX idx_workflow_result_outbox_incarnation
                ON workflow_result_outbox(run_id, run_incarnation);
            CREATE INDEX idx_workflow_result_outbox_dlq_created
                ON workflow_result_outbox_dead_letter(
                    quarantined_at_ms, dead_letter_id
                );
            PRAGMA user_version=2;
            """
        )
        connection.execute(
            """
            INSERT INTO workflow_result_outbox
                (enqueue_seq, event_key, run_id, run_incarnation, payload,
                 payload_bytes, created_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                7,
                payload["event_key"],
                payload["run_id"],
                payload["run_incarnation"],
                payload_json,
                len(payload_json.encode("utf-8")),
                123,
            ),
        )
        if malformed:
            connection.execute("DROP INDEX idx_workflow_result_outbox_run")
        connection.commit()
    os.chmod(path, 0o600)
    return payload


def test_v3_claim_metadata_round_trips_outside_payload(tmp_path) -> None:
    path = tmp_path / "outbox.db"
    payload = _payload()
    outbox = WorkflowResultOutbox(str(path))
    try:
        outbox.enqueue(
            payload,
            supervisor_id="supervisor-a",
            attempt_generation=17,
        )
        [entry] = outbox.list_pending()
        assert entry.supervisor_id == "supervisor-a"
        assert entry.attempt_generation == 17
        assert entry.payload == payload
        assert "supervisor_id" not in entry.payload
        assert "attempt_generation" not in entry.payload
        assert outbox.pending_for_claim(
            "run-1",
            supervisor_id="supervisor-a",
            attempt_generation=17,
        )
    finally:
        outbox.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        row = connection.execute(
            """
            SELECT supervisor_id, attempt_generation, payload
            FROM workflow_result_outbox
            """
        ).fetchone()
    assert row[:2] == ("supervisor-a", 17)
    assert json.loads(row[2]) == payload
    assert WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION == 3


@pytest.mark.parametrize(
    ("supervisor_id", "attempt_generation"),
    [
        ("supervisor-a", None),
        (None, 1),
        ("", 1),
        (" supervisor-a", 1),
        ("supervisor-a", True),
        ("supervisor-a", 0),
    ],
)
def test_enqueue_rejects_invalid_private_claim_pair(
    tmp_path,
    supervisor_id,
    attempt_generation,
) -> None:
    outbox = WorkflowResultOutbox(str(tmp_path / "outbox.db"))
    try:
        with pytest.raises((TypeError, ValueError)):
            outbox.enqueue(
                _payload(),
                supervisor_id=supervisor_id,
                attempt_generation=attempt_generation,
            )
        assert outbox.count() == 0
    finally:
        outbox.close()


def test_claim_is_part_of_enqueue_idempotency_and_delete_cas(tmp_path) -> None:
    outbox = WorkflowResultOutbox(str(tmp_path / "outbox.db"))
    payload = _payload()
    try:
        event_key = outbox.enqueue(
            payload,
            supervisor_id="supervisor-a",
            attempt_generation=17,
        )
        assert outbox.enqueue(
            payload,
            supervisor_id="supervisor-a",
            attempt_generation=17,
        ) == event_key
        with pytest.raises(ValueError, match="claim"):
            outbox.enqueue(
                payload,
                supervisor_id="supervisor-a",
                attempt_generation=18,
            )
        [entry] = outbox.list_pending()
        assert not outbox.delete(event_key)
        assert not outbox.delete(
            event_key,
            enqueue_seq=entry.enqueue_seq,
            payload=payload,
            supervisor_id="supervisor-a",
            attempt_generation=18,
        )
        assert outbox.delete(
            event_key,
            enqueue_seq=entry.enqueue_seq,
            payload=payload,
            supervisor_id="supervisor-a",
            attempt_generation=17,
        )
    finally:
        outbox.close()


def test_exact_v2_migrates_to_v3_preserving_sequence_as_unclaimed(tmp_path) -> None:
    path = tmp_path / "v2.db"
    payload = _create_exact_v2_outbox(path)

    outbox = WorkflowResultOutbox(str(path))
    try:
        [entry] = outbox.list_pending()
        assert entry.enqueue_seq == 7
        assert entry.payload == payload
        assert entry.supervisor_id is None
        assert entry.attempt_generation is None
    finally:
        outbox.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?",
            ("workflow_result_outbox",),
        ).fetchone()[0] == 7


def test_v2_to_v3_preserves_deleted_sequence_high_water(tmp_path) -> None:
    path = tmp_path / "v2-high-water.db"
    _create_exact_v2_outbox(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE sqlite_sequence SET seq = 41 WHERE name = ?",
            ("workflow_result_outbox",),
        )
        connection.commit()

    outbox = WorkflowResultOutbox(str(path))
    try:
        payload = _payload("run-after-migration", "inc-after-migration")
        outbox.enqueue(payload)
        entry = next(
            item
            for item in outbox.list_pending()
            if item.event_key == payload["event_key"]
        )
        assert entry.enqueue_seq == 42
    finally:
        outbox.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?",
            ("workflow_result_outbox",),
        ).fetchone()[0] == 42


def test_v2_migration_quarantines_duplicate_run_id_keeping_max_enqueue_seq(
    tmp_path,
) -> None:
    path = tmp_path / "v2-duplicate-run.db"
    loser_payload = _payload("run-dup", "inc-old")
    winner_payload = _payload("run-dup", "inc-new")
    loser_json = json.dumps(loser_payload, sort_keys=True, separators=(",", ":"))
    winner_json = json.dumps(winner_payload, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE workflow_result_outbox (
                enqueue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL
            );
            CREATE TABLE workflow_result_outbox_dead_letter (
                dead_letter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                quarantined_at_ms INTEGER NOT NULL
            );
            CREATE INDEX idx_workflow_result_outbox_run
                ON workflow_result_outbox(run_id, run_incarnation, enqueue_seq);
            CREATE UNIQUE INDEX idx_workflow_result_outbox_incarnation
                ON workflow_result_outbox(run_id, run_incarnation);
            CREATE INDEX idx_workflow_result_outbox_dlq_created
                ON workflow_result_outbox_dead_letter(
                    quarantined_at_ms, dead_letter_id
                );
            PRAGMA user_version=2;
            """
        )
        connection.execute(
            """
            INSERT INTO workflow_result_outbox
                (enqueue_seq, event_key, run_id, run_incarnation, payload,
                 payload_bytes, created_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                5,
                loser_payload["event_key"],
                loser_payload["run_id"],
                loser_payload["run_incarnation"],
                loser_json,
                len(loser_json.encode("utf-8")),
                100,
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_result_outbox
                (enqueue_seq, event_key, run_id, run_incarnation, payload,
                 payload_bytes, created_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                9,
                winner_payload["event_key"],
                winner_payload["run_id"],
                winner_payload["run_incarnation"],
                winner_json,
                len(winner_json.encode("utf-8")),
                200,
            ),
        )
        connection.commit()
    os.chmod(path, 0o600)

    outbox = WorkflowResultOutbox(str(path))
    try:
        pending = outbox.list_pending()
    finally:
        outbox.close()

    assert [entry.payload for entry in pending] == [winner_payload]

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        dead_letters = connection.execute(
            "SELECT event_key, reason_code FROM workflow_result_outbox_dead_letter"
        ).fetchall()
    assert dead_letters == [
        (loser_payload["event_key"], "legacy_duplicate_incarnation"),
    ]


def test_malformed_claimed_v2_is_rejected_without_repair(tmp_path) -> None:
    path = tmp_path / "malformed-v2.db"
    _create_exact_v2_outbox(path, malformed=True)

    with pytest.raises(RuntimeError, match="schema|index|object"):
        WorkflowResultOutbox(str(path))

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(workflow_result_outbox)"
            )
        }
    assert "supervisor_id" not in columns
    assert "attempt_generation" not in columns


def test_corrupt_private_claim_is_quarantined(tmp_path) -> None:
    path = tmp_path / "outbox.db"
    outbox = WorkflowResultOutbox(str(path))
    try:
        outbox.enqueue(
            _payload(),
            supervisor_id="supervisor-a",
            attempt_generation=17,
        )
    finally:
        outbox.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE workflow_result_outbox SET attempt_generation = 0"
        )
        connection.commit()

    outbox = WorkflowResultOutbox(str(path))
    try:
        assert outbox.list_pending() == []
        assert outbox.count() == 0
    finally:
        outbox.close()


def test_oversized_unclaimed_row_is_quarantined_and_frees_event_key(
    tmp_path,
    caplog,
) -> None:
    path = tmp_path / "outbox.db"
    payload = _payload()
    big_payload = {**payload, "detail": "x" * 2000}
    outbox = WorkflowResultOutbox(str(path))
    try:
        outbox.enqueue(big_payload)
    finally:
        outbox.close()

    # Reopen with a byte limit smaller than the already-stored payload (but
    # large enough for a smaller replacement payload below): the row is
    # structurally valid, just no longer within the configured limit.
    caplog.set_level("WARNING", logger="workflow.result_outbox")
    small_outbox = WorkflowResultOutbox(str(path), max_payload_bytes=200)
    try:
        assert small_outbox.list_pending() == []
        # Unclaimed oversized rows are quarantined to dead_letter, not left
        # behind: the main table is empty again and the event_key is free.
        assert small_outbox.count() == 0

        recovered_event_key = small_outbox.enqueue(payload)
        assert recovered_event_key == payload["event_key"]
        [entry] = small_outbox.list_pending()
        assert entry.payload == payload
    finally:
        small_outbox.close()

    assert "exceeds payload size limit" in caplog.text
    with sqlite3.connect(path) as connection:
        dead_letters = connection.execute(
            "SELECT event_key, reason_code FROM workflow_result_outbox_dead_letter"
        ).fetchall()
    assert dead_letters == [(payload["event_key"], "oversized_payload")]


def test_oversized_claimed_row_is_skipped_with_warning_and_not_quarantined(
    tmp_path,
    caplog,
) -> None:
    path = tmp_path / "outbox.db"
    payload = _payload()
    big_payload = {**payload, "detail": "x" * 2000}
    outbox = WorkflowResultOutbox(str(path))
    try:
        outbox.enqueue(
            big_payload,
            supervisor_id="supervisor-a",
            attempt_generation=1,
        )
    finally:
        outbox.close()

    # A claimed row must not be quarantined on oversize: doing so would
    # erase the only evidence pending_for_claim() has that this attempt
    # already produced a terminal result, which could make a supervisor
    # believe the run never completed and write a conflicting outcome.
    caplog.set_level("WARNING", logger="workflow.result_outbox")
    small_outbox = WorkflowResultOutbox(str(path), max_payload_bytes=200)
    try:
        assert small_outbox.list_pending() == []
        assert small_outbox.count() == 1
    finally:
        small_outbox.close()

    assert "exceeds payload size limit" in caplog.text
    assert "claim/fencing protocol" in caplog.text
    with sqlite3.connect(path) as connection:
        stored = connection.execute(
            "SELECT event_key FROM workflow_result_outbox"
        ).fetchone()
        dead_letters = connection.execute(
            "SELECT COUNT(*) FROM workflow_result_outbox_dead_letter"
        ).fetchone()
    assert stored == (payload["event_key"],)
    assert dead_letters == (0,)
