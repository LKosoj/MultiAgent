from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

from backend.fastapi_app.agui.store import EventStore
import workflow.result_identity as identity_module
from workflow.result_outbox import WorkflowResultOutbox
from test_text_to_sql_agui_workflow_contract import (
    _LIGHT_WORKFLOW_MODULES,
    _load_light_workflow_streamlit_api,
)

_MISSING_MODULE = object()


@pytest.fixture(autouse=True)
def _restore_light_workflow_modules():
    # ``_load_light_workflow_streamlit_api`` (заимствована из
    # test_text_to_sql_agui_workflow_contract) подменяет sys.modules для
    # workflow.streamlit_api/workflow.result_delivery/... на облегчённые
    # копии. В файле-источнике за откат отвечает одноимённая autouse-фикстура,
    # но она не распространяется на этот файл — без локальной копии подмена
    # остаётся в sys.modules навсегда и течёт в другие тестовые модули,
    # которые резолвят workflow.streamlit_api заново через sys.modules
    # (см. workflow/result_delivery.py::_streamlit_api).
    saved = {
        name: sys.modules.get(name, _MISSING_MODULE)
        for name in _LIGHT_WORKFLOW_MODULES
    }
    yield
    for name, module in saved.items():
        if module is _MISSING_MODULE:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _legacy_payload() -> dict[str, str]:
    return {
        "run_id": "legacy:run",
        "run_incarnation": "legacy:inc",
        "event_key": "workflow-result:legacy:run:legacy:inc",
        "status": "failed",
    }


def _create_v1_outbox(path) -> dict[str, str]:
    payload = _legacy_payload()
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
    return payload


def test_v1_outbox_with_trigger_is_rejected_without_migrating(tmp_path):
    outbox_path = tmp_path / "legacy-outbox-trigger.db"
    payload = _create_v1_outbox(outbox_path)
    with sqlite3.connect(outbox_path) as connection:
        connection.execute(
            "CREATE TRIGGER injected_trigger AFTER INSERT ON "
            "workflow_result_outbox BEGIN SELECT 1; END"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="trigger"):
        WorkflowResultOutbox(str(outbox_path))

    with sqlite3.connect(outbox_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(workflow_result_outbox)"
            )
        }
        trigger_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'trigger' AND name = 'injected_trigger'
            """
        ).fetchone()
        stored_payload = connection.execute(
            "SELECT payload FROM workflow_result_outbox WHERE event_key = ?",
            (payload["event_key"],),
        ).fetchone()
    assert "enqueue_seq" not in columns
    assert trigger_exists is not None
    assert stored_payload is not None
    assert json.loads(stored_payload[0]) == payload


def test_new_outbox_enqueue_rejects_v1_identity(tmp_path):
    outbox = WorkflowResultOutbox(str(tmp_path / "new-outbox.db"))
    try:
        with pytest.raises(ValueError, match="v2|event_key"):
            outbox.enqueue(_legacy_payload())
        assert outbox.count() == 0
    finally:
        outbox.close()


def test_claimed_current_outbox_accepts_equivalent_schema_whitespace(tmp_path):
    db_path = tmp_path / "outbox-schema-whitespace.db"
    WorkflowResultOutbox(str(db_path)).close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DROP INDEX idx_workflow_result_outbox_dlq_created"
        )
        connection.execute(
            """
            CREATE INDEX idx_workflow_result_outbox_dlq_created
            ON workflow_result_outbox_dead_letter (
                quarantined_at_ms,
                dead_letter_id
            )
            """
        )
        connection.commit()

    WorkflowResultOutbox(str(db_path)).close()


def test_claimed_current_outbox_does_not_repair_missing_schema_object(tmp_path):
    db_path = tmp_path / "missing-index.db"
    WorkflowResultOutbox(str(db_path)).close()
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX idx_workflow_result_outbox_run")

    with pytest.raises(RuntimeError, match="schema|object|index"):
        WorkflowResultOutbox(str(db_path))

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_workflow_result_outbox_run'
            """
        ).fetchone() is None


def test_existing_v1_pending_outbox_replays_as_v2_then_cas_acks_original(
    tmp_path,
):
    outbox_path = tmp_path / "legacy-outbox.db"
    original_payload = _create_v1_outbox(outbox_path)
    outbox = WorkflowResultOutbox(str(outbox_path))
    store = EventStore(str(tmp_path / "primary.db"))
    assert store.reserve_workflow_run(
        original_payload["run_id"],
        original_payload["run_incarnation"],
        "session-1",
        "text_to_sql_pipeline",
    )
    try:
        pending = outbox.list_pending()
        assert len(pending) == 1
        legacy = pending[0]
        assert legacy.event_key == original_payload["event_key"]
        assert legacy.payload == original_payload

        upgraded = identity_module.upgrade_legacy_workflow_result_payload(
            legacy.payload
        )
        assert store.append(
            legacy.run_id,
            "WORKFLOW_RESULT",
            upgraded,
        ) == 1
        assert outbox.delete(
            legacy.event_key,
            enqueue_seq=legacy.enqueue_seq,
            payload=legacy.payload,
        )

        assert outbox.list_pending() == []
        stored = list(store.list_after(legacy.run_id))[0]
        assert stored.event_key == upgraded["event_key"]
        assert stored.payload == upgraded
    finally:
        store.close()
        outbox.close()


def test_production_drain_translates_v1_then_cas_acks_original(
    monkeypatch,
    tmp_path,
):
    streamlit_api = _load_light_workflow_streamlit_api()
    outbox_path = tmp_path / "legacy-outbox.db"
    primary_path = tmp_path / "primary.db"
    original = _create_v1_outbox(outbox_path)
    reservation = EventStore(str(primary_path))
    try:
        assert reservation.reserve_workflow_run(
            original["run_id"],
            original["run_incarnation"],
            "session-1",
            "text_to_sql_pipeline",
        )
    finally:
        reservation.close()
    monkeypatch.setattr(
        streamlit_api,
        "_agui_event_store_path",
        lambda: primary_path,
    )

    assert streamlit_api._drain_workflow_result_outbox_batch(
        limit=1,
        path=outbox_path,
    ) == (1, True)

    outbox = WorkflowResultOutbox(str(outbox_path))
    primary = EventStore(str(primary_path))
    try:
        assert outbox.list_pending() == []
        [stored] = list(primary.list_after(original["run_id"]))
        assert stored.event_key == identity_module.workflow_result_event_key(
            original["run_id"],
            original["run_incarnation"],
        )
        assert stored.payload == {
            **original,
            "event_key": stored.event_key,
        }
    finally:
        primary.close()
        outbox.close()
