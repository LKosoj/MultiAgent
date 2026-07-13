from __future__ import annotations

import json
import os
import sqlite3

import pytest

from backend.fastapi_app.agui.store import EventStore
from workflow.result_identity import workflow_result_event_key
from workflow.result_outbox import WorkflowResultOutbox
from workflow.result_repository import WorkflowResultCollisionError
from test_text_to_sql_agui_workflow_contract import (
    _WorkflowManagerStub,
    _load_service_with_stubs,
)


def _payload(run_id: str, incarnation: str, *, result: str) -> dict:
    return {
        "run_id": run_id,
        "run_incarnation": incarnation,
        "event_key": workflow_result_event_key(run_id, incarnation),
        "status": "completed",
        "success": True,
        "result": result,
        "artifacts": {"final_output": result},
        "snapshot": {"workflow_name": "generic_pipeline"},
    }


def _legacy_payload(run_id: str, incarnation: str, *, result: str) -> dict:
    return {
        **_payload(run_id, incarnation, result=result),
        "event_key": f"workflow-result:{run_id}:{incarnation}",
    }


def _write_legacy_outbox(path, payload: dict) -> None:
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE workflow_result_outbox (
                event_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO workflow_result_outbox
                (event_key, run_id, run_incarnation, payload,
                 payload_bytes, created_at_ms)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                payload["event_key"],
                payload["run_id"],
                payload["run_incarnation"],
                payload_json,
                len(payload_json.encode("utf-8")),
            ),
        )
    os.chmod(path, 0o600)


def _service(monkeypatch, store, outbox_path):
    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())
    monkeypatch.setattr(service, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(
        service,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
    )
    return service


def test_service_result_reader_returns_outbox_only_terminal_result(
    monkeypatch,
    tmp_path,
):
    run_id = "outbox-only-run"
    incarnation = "outbox-only-inc"
    store = EventStore(str(tmp_path / "events.db"))
    assert store.reserve_workflow_run(
        run_id,
        incarnation,
        "session-1",
        "generic_pipeline",
    ) is True
    outbox_path = tmp_path / "outbox.db"
    outbox = WorkflowResultOutbox(str(outbox_path))
    expected = _payload(run_id, incarnation, result="outbox")
    try:
        outbox.enqueue(expected)
    finally:
        outbox.close()
    service = _service(monkeypatch, store, outbox_path)

    assert service._workflow_result_from_store(run_id) == expected


def test_service_result_reader_propagates_primary_outbox_collision(
    monkeypatch,
    tmp_path,
):
    run_id = "collision-run"
    incarnation = "collision-inc"
    store = EventStore(str(tmp_path / "events.db"))
    assert store.reserve_workflow_run(
        run_id,
        incarnation,
        "session-1",
        "generic_pipeline",
    ) is True
    primary = _payload(run_id, incarnation, result="primary")
    store.append(run_id, "WORKFLOW_RESULT", primary)
    outbox_path = tmp_path / "outbox.db"
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue({**primary, "result": "conflict"})
    finally:
        outbox.close()
    service = _service(monkeypatch, store, outbox_path)

    with pytest.raises(WorkflowResultCollisionError, match="collision|mismatch"):
        service._workflow_result_from_store(run_id)


def test_service_result_reader_propagates_primary_schema_error(
    monkeypatch,
    tmp_path,
):
    class BrokenStore:
        def list_after(self, *_args, **_kwargs):
            raise RuntimeError("malformed primary schema")

    service = _service(monkeypatch, BrokenStore(), tmp_path / "missing-outbox.db")

    with pytest.raises(RuntimeError, match="malformed primary schema"):
        service._workflow_result_from_store("broken-run")


def test_service_result_reader_uses_legacy_run_finished_only_when_result_absent(
    monkeypatch,
    tmp_path,
):
    store = EventStore(str(tmp_path / "events.db"))
    run_id = "legacy-fallback-run"
    expected = {
        "type": "workflow_outputs",
        "result": "legacy",
        "artifacts": {"final_output": "legacy"},
    }
    store.append(run_id, "RUN_FINISHED", {"result": expected})
    service = _service(monkeypatch, store, tmp_path / "missing-outbox.db")

    assert service._workflow_result_from_store(run_id) == expected


def test_service_result_reader_returns_existing_v1_primary_as_raw_legacy(
    monkeypatch,
    tmp_path,
):
    run_id = "legacy-primary-run"
    incarnation = "legacy-primary-inc"
    db_path = tmp_path / "events.db"
    EventStore(str(db_path)).close()
    expected = _legacy_payload(run_id, incarnation, result="legacy-primary")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agui_events
                (run_id, seq, event_type, payload, created_at_ms,
                 run_incarnation, event_key)
            VALUES (?, 1, 'WORKFLOW_RESULT', ?, 1, ?, ?)
            """,
            (
                run_id,
                json.dumps(expected, sort_keys=True, separators=(",", ":")),
                incarnation,
                expected["event_key"],
            ),
        )
    store = EventStore(str(db_path))
    service = _service(monkeypatch, store, tmp_path / "missing-outbox.db")

    assert service._workflow_result_from_store(run_id) == expected


def test_service_result_reader_reconciles_v1_outbox_with_translated_v2_primary(
    monkeypatch,
    tmp_path,
):
    run_id = "legacy-crash-window-run"
    incarnation = "legacy-crash-window-inc"
    store = EventStore(str(tmp_path / "events.db"))
    assert store.reserve_workflow_run(
        run_id,
        incarnation,
        "session-1",
        "generic_pipeline",
    )
    legacy = _legacy_payload(run_id, incarnation, result="same-result")
    translated = {
        **legacy,
        "event_key": workflow_result_event_key(run_id, incarnation),
    }
    store.append(run_id, "WORKFLOW_RESULT", translated)
    outbox_path = tmp_path / "legacy-outbox.db"
    _write_legacy_outbox(outbox_path, legacy)
    service = _service(monkeypatch, store, outbox_path)

    assert service._workflow_result_from_store(run_id) == translated


def test_service_result_reader_returns_existing_v1_outbox_as_raw_legacy(
    monkeypatch,
    tmp_path,
):
    run_id = "legacy-outbox-only-run"
    incarnation = "legacy-outbox-only-inc"
    store = EventStore(str(tmp_path / "events.db"))
    outbox_path = tmp_path / "legacy-outbox.db"
    expected = _legacy_payload(run_id, incarnation, result="legacy-outbox")
    _write_legacy_outbox(outbox_path, expected)
    service = _service(monkeypatch, store, outbox_path)

    assert service._workflow_result_from_store(run_id) == expected
