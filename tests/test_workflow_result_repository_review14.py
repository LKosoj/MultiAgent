from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.fastapi_app.agui.store import StoredEvent
from workflow.result_identity import workflow_result_event_key
from workflow.result_outbox import WorkflowResultOutbox
from workflow.result_repository import (
    WorkflowResultCollisionError,
    load_reconciled_workflow_result,
)


def _payload(run_id: str, incarnation: str, *, status: str) -> dict:
    return {
        "run_id": run_id,
        "run_incarnation": incarnation,
        "event_key": workflow_result_event_key(run_id, incarnation),
        "status": status,
    }


def _event(seq: int, payload: dict) -> StoredEvent:
    return StoredEvent(
        seq=seq,
        run_id=payload["run_id"],
        event_type="WORKFLOW_RESULT",
        payload=payload,
        created_at_ms=seq,
        run_incarnation=payload.get("run_incarnation"),
        event_key=payload.get("event_key"),
    )


class _PrimaryStore:
    def __init__(self, events, *, reservation=...):
        self._events = list(events)
        self._reservation = reservation

    def list_after(self, _run_id, _after_seq):
        return list(self._events)

    def get_workflow_run_invocation(self, _run_id):
        if self._reservation is ...:
            raise AssertionError("reservation lookup was not configured")
        return self._reservation


class _PrimaryStoreWithoutReservationAPI:
    def __init__(self, events):
        self._events = list(events)

    def list_after(self, _run_id, _after_seq):
        return list(self._events)


def test_strict_repository_rejects_incompatible_v2_primary_candidates():
    run_id = "multi-primary-run"
    first = _payload(run_id, "inc-1", status="failed")
    second = _payload(run_id, "inc-2", status="completed")

    with pytest.raises(WorkflowResultCollisionError, match="candidate|identity|collision"):
        load_reconciled_workflow_result(
            run_id,
            primary_store=_PrimaryStoreWithoutReservationAPI(
                [_event(1, first), _event(2, second)]
            ),
            strict=True,
        )


def test_strict_repository_rejects_malformed_candidate_before_valid_candidate():
    run_id = "malformed-primary-run"
    malformed = StoredEvent(
        seq=1,
        run_id=run_id,
        event_type="WORKFLOW_RESULT",
        payload={
            "run_id": run_id,
            "event_key": workflow_result_event_key(run_id, "inc-1"),
            "status": "failed",
        },
        created_at_ms=1,
    )
    valid = _payload(run_id, "inc-1", status="failed")

    with pytest.raises(WorkflowResultCollisionError, match="candidate|envelope|identity"):
        load_reconciled_workflow_result(
            run_id,
            primary_store=_PrimaryStoreWithoutReservationAPI(
                [malformed, _event(2, valid)]
            ),
            strict=True,
        )


def test_strict_repository_checks_extra_primary_candidate_when_outbox_matches_one(
    tmp_path,
):
    run_id = "outbox-selector-run"
    conflicting = _payload(run_id, "inc-1", status="failed")
    matching = _payload(run_id, "inc-2", status="completed")
    outbox_path = tmp_path / "outbox.db"
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(matching)
    finally:
        outbox.close()

    with pytest.raises(WorkflowResultCollisionError, match="candidate|identity|collision"):
        load_reconciled_workflow_result(
            run_id,
            primary_store=_PrimaryStoreWithoutReservationAPI(
                [_event(1, conflicting), _event(2, matching)]
            ),
            outbox_path=outbox_path,
            strict=True,
        )


def test_strict_repository_binds_v2_candidate_to_primary_reservation():
    run_id = "reservation-bound-run"
    payload = _payload(run_id, "inc-result", status="completed")
    reservation = SimpleNamespace(run_incarnation="inc-reserved")

    with pytest.raises(WorkflowResultCollisionError, match="reservation|incarnation"):
        load_reconciled_workflow_result(
            run_id,
            primary_store=_PrimaryStore(
                [_event(1, payload)],
                reservation=reservation,
            ),
            strict=True,
        )


def test_strict_repository_keeps_isolated_unkeyed_legacy_latest_policy():
    run_id = "legacy-unkeyed-run"
    first = StoredEvent(
        seq=1,
        run_id=run_id,
        event_type="WORKFLOW_RESULT",
        payload={"run_id": run_id, "status": "failed"},
        created_at_ms=1,
    )
    latest = StoredEvent(
        seq=2,
        run_id=run_id,
        event_type="WORKFLOW_RESULT",
        payload={"run_id": run_id, "status": "completed"},
        created_at_ms=2,
    )

    reconciled = load_reconciled_workflow_result(
        run_id,
        primary_store=_PrimaryStoreWithoutReservationAPI([first, latest]),
        strict=True,
    )

    assert reconciled is not None
    assert reconciled.payload == latest.payload
