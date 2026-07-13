from __future__ import annotations

import json
import os
import sqlite3

import pytest

from workflow.result_outbox import WorkflowResultOutbox


def _create_legacy_outbox(path, timestamps: list[int]) -> list[str]:
    event_keys = [
        f"workflow-result:legacy-run-{index}:legacy-incarnation-{index}"
        for index in range(len(timestamps))
    ]
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
        for index, (event_key, created_at_ms) in enumerate(
            zip(event_keys, timestamps, strict=True)
        ):
            payload = {
                "event_key": event_key,
                "run_id": f"legacy-run-{index}",
                "run_incarnation": f"legacy-incarnation-{index}",
            }
            payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            connection.execute(
                """
                INSERT INTO workflow_result_outbox
                    (event_key, run_id, run_incarnation, payload,
                     payload_bytes, created_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    payload["run_id"],
                    payload["run_incarnation"],
                    payload_json,
                    len(payload_json.encode("utf-8")),
                    created_at_ms,
                ),
            )
        connection.commit()
    os.chmod(path, 0o600)
    return event_keys


@pytest.mark.parametrize(
    "timestamps",
    [
        [300, 100, 200],
        [100, 100, 100],
    ],
    ids=["clock-rollback", "equal-timestamps"],
)
def test_legacy_backfill_preserves_rowid_fifo_independent_of_wall_clock(
    tmp_path,
    timestamps,
):
    db_path = tmp_path / "legacy-outbox.db"
    expected_event_keys = _create_legacy_outbox(db_path, timestamps)

    outbox = WorkflowResultOutbox(str(db_path))
    try:
        pending = outbox.list_pending()
    finally:
        outbox.close()

    assert [entry.event_key for entry in pending] == expected_event_keys
    assert [entry.enqueue_seq for entry in pending] == [1, 2, 3]
