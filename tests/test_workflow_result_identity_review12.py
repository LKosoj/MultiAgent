"""Adversarial contracts for durable workflow-result identity."""

from __future__ import annotations

import json
import sqlite3

import pytest

from backend.fastapi_app.agui.store import EventStore, workflow_result_event_key
from workflow.result_repository import (
    WorkflowResultCollisionError,
    load_reconciled_workflow_result,
)
from workflow.result_outbox import WorkflowResultOutbox


@pytest.mark.parametrize(
    ("run_id", "run_incarnation"),
    [
        (" run-1", "inc-1"),
        ("run-1 ", "inc-1"),
        ("run-1", " inc-1"),
        ("run-1", "inc-1 "),
        (" ", "inc-1"),
        ("run-1", " "),
    ],
)
def test_workflow_result_event_key_rejects_noncanonical_identity(
    run_id,
    run_incarnation,
):
    with pytest.raises(ValueError, match="canonical|whitespace|non-empty"):
        workflow_result_event_key(run_id, run_incarnation)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", " run-1"),
        ("run_incarnation", "inc-1 "),
        ("session_id", " session-1"),
        ("workflow_name", "text_to_sql_pipeline "),
    ],
)
def test_workflow_run_reservation_rejects_surrounding_whitespace(
    tmp_path,
    field,
    value,
):
    store = EventStore(str(tmp_path / f"reservation-{field}.db"))
    values = {
        "run_id": "run-1",
        "run_incarnation": "inc-1",
        "session_id": "session-1",
        "workflow_name": "text_to_sql_pipeline",
    }
    values[field] = value
    try:
        with pytest.raises(ValueError, match="canonical|whitespace|non-empty"):
            store.reserve_workflow_run(**values)
        assert store.get_workflow_run_invocation("run-1") is None
    finally:
        store.close()


def test_outbox_enqueue_rejects_event_key_identity_mismatch_without_write(tmp_path):
    outbox = WorkflowResultOutbox(str(tmp_path / "enqueue-mismatch.db"))
    payload = {
        "run_id": "run-1",
        "run_incarnation": "inc-1",
        "event_key": workflow_result_event_key("run-1", "wrong-incarnation"),
        "status": "failed",
    }
    try:
        with pytest.raises(ValueError, match="event_key|identity"):
            outbox.enqueue(payload)
        assert outbox.count() == 0
    finally:
        outbox.close()


def test_outbox_decode_quarantines_event_key_identity_mismatch(tmp_path):
    db_path = tmp_path / "decode-mismatch.db"
    outbox = WorkflowResultOutbox(str(db_path))
    payload = {
        "run_id": "run-1",
        "run_incarnation": "inc-1",
        "event_key": workflow_result_event_key("run-1", "inc-1"),
        "status": "failed",
    }
    try:
        outbox.enqueue(payload)
    finally:
        outbox.close()

    corrupt = {
        **payload,
        "event_key": workflow_result_event_key("run-1", "wrong-incarnation"),
    }
    encoded = json.dumps(
        corrupt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE workflow_result_outbox
            SET event_key = ?, payload = ?, payload_bytes = ?
            """,
            (
                corrupt["event_key"],
                encoded,
                len(encoded.encode("utf-8")),
            ),
        )

    outbox = WorkflowResultOutbox(str(db_path))
    try:
        assert outbox.list_pending() == []
        assert outbox.count() == 0
    finally:
        outbox.close()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_result_outbox_dead_letter"
        ).fetchone() == (1,)


@pytest.mark.parametrize("reservation", ["missing", "different_incarnation"])
def test_event_store_keyed_result_requires_exact_reserved_invocation(
    tmp_path,
    reservation,
):
    store = EventStore(str(tmp_path / f"reservation-{reservation}.db"))
    run_id = "run-1"
    result_incarnation = "inc-result"
    if reservation == "different_incarnation":
        assert store.reserve_workflow_run(
            run_id,
            "inc-reserved",
            "session-1",
            "text_to_sql_pipeline",
        ) is True
    payload = {
        "run_id": run_id,
        "run_incarnation": result_incarnation,
        "event_key": workflow_result_event_key(run_id, result_incarnation),
        "status": "failed",
    }
    try:
        with pytest.raises(ValueError, match="reserved|reservation|incarnation"):
            store.append(run_id, "WORKFLOW_RESULT", payload)
        assert list(store.list_after(run_id, 0)) == []
    finally:
        store.close()


def test_event_store_keyed_result_requires_payload_run_id(tmp_path):
    store = EventStore(str(tmp_path / "missing-payload-run-id.db"))
    run_id = "run-1"
    incarnation = "inc-1"
    assert store.reserve_workflow_run(
        run_id,
        incarnation,
        "session-1",
        "text_to_sql_pipeline",
    ) is True
    payload = {
        "run_incarnation": incarnation,
        "event_key": workflow_result_event_key(run_id, incarnation),
        "status": "failed",
    }
    try:
        with pytest.raises(ValueError, match="run_id|identity"):
            store.append(run_id, "WORKFLOW_RESULT", payload)
        assert list(store.list_after(run_id, 0)) == []
    finally:
        store.close()


def test_reconciler_rejects_canonical_payload_in_wrong_event_envelope(tmp_path):
    store = EventStore(str(tmp_path / "wrong-envelope.db"))
    run_id = "run-1"
    incarnation = "inc-1"
    payload = {
        "run_id": run_id,
        "run_incarnation": incarnation,
        "event_key": workflow_result_event_key(run_id, incarnation),
        "status": "failed",
    }
    try:
        with store._lock:
            store._conn.execute(
                """
                INSERT INTO agui_events
                    (run_id, seq, event_type, payload, created_at_ms,
                     run_incarnation, event_key)
                VALUES (?, 1, 'OTHER_EVENT', ?, 1, ?, ?)
                """,
                (
                    run_id,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    incarnation,
                    payload["event_key"],
                ),
            )
            store._conn.commit()
        with pytest.raises(WorkflowResultCollisionError, match="event_type|envelope"):
            load_reconciled_workflow_result(
                run_id,
                primary_store=store,
                strict=True,
            )
    finally:
        store.close()
