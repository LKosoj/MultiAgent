from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.fastapi_app.agui.store import StoredEvent
from workflow.result_identity import workflow_result_event_key
from workflow.result_repository import (
    WorkflowResultCollisionError,
    load_reconciled_workflow_result,
)


def _v2_payload(run_id: str, incarnation: str) -> dict:
    return {
        "run_id": run_id,
        "run_incarnation": incarnation,
        "event_key": workflow_result_event_key(run_id, incarnation),
        "status": "completed",
    }


def _v1_payload(run_id: str, incarnation: str) -> dict:
    return {
        **_v2_payload(run_id, incarnation),
        "event_key": f"workflow-result:{run_id}:{incarnation}",
    }


def _event(payload: dict) -> StoredEvent:
    return StoredEvent(
        seq=1,
        run_id=payload["run_id"],
        event_type="WORKFLOW_RESULT",
        payload=payload,
        created_at_ms=1,
        run_incarnation=payload.get("run_incarnation"),
        event_key=payload.get("event_key"),
    )


class _PrimaryStore:
    def __init__(self, events, *, reservation):
        self._events = list(events)
        self._reservation = reservation

    def list_after(self, _run_id, _after_seq):
        return list(self._events)

    def get_workflow_run_invocation(self, _run_id):
        return self._reservation


class _PrimaryStoreWithoutReservationAPI:
    def list_after(self, _run_id, _after_seq):
        return []


class _Outbox:
    def __init__(self, payload):
        self._payload = payload

    def latest_payload(self, _run_id):
        return self._payload

    def close(self):
        return None


def _load_with_outbox(
    tmp_path,
    *,
    run_id: str,
    payload: dict,
    primary_store,
    strict: bool,
):
    outbox_path = tmp_path / "outbox.db"
    outbox_path.write_bytes(b"repository-test-placeholder")
    return load_reconciled_workflow_result(
        run_id,
        primary_store=primary_store,
        outbox_path=outbox_path,
        outbox_factory=lambda _path: _Outbox(payload),
        strict=strict,
    )


def test_strict_v2_outbox_only_requires_existing_reservation(tmp_path):
    run_id = "outbox-only-missing-reservation"
    payload = _v2_payload(run_id, "inc-result")

    with pytest.raises(WorkflowResultCollisionError, match="reservation"):
        _load_with_outbox(
            tmp_path,
            run_id=run_id,
            payload=payload,
            primary_store=_PrimaryStore([], reservation=None),
            strict=True,
        )


def test_strict_v2_outbox_only_requires_reservation_api(tmp_path):
    run_id = "outbox-only-missing-reservation-api"
    payload = _v2_payload(run_id, "inc-result")

    with pytest.raises(WorkflowResultCollisionError, match="reservation"):
        _load_with_outbox(
            tmp_path,
            run_id=run_id,
            payload=payload,
            primary_store=_PrimaryStoreWithoutReservationAPI(),
            strict=True,
        )


def test_strict_v2_outbox_only_rejects_mismatched_reservation(tmp_path):
    run_id = "outbox-only-mismatched-reservation"
    payload = _v2_payload(run_id, "inc-result")
    reservation = SimpleNamespace(
        run_id=run_id,
        run_incarnation="inc-reserved",
    )

    with pytest.raises(WorkflowResultCollisionError, match="reservation|incarnation"):
        _load_with_outbox(
            tmp_path,
            run_id=run_id,
            payload=payload,
            primary_store=_PrimaryStore([], reservation=reservation),
            strict=True,
        )


def test_strict_v2_outbox_only_rejects_reservation_for_other_run(tmp_path):
    run_id = "outbox-only-wrong-run-reservation"
    payload = _v2_payload(run_id, "inc-result")
    reservation = SimpleNamespace(
        run_id="another-run",
        run_incarnation="inc-result",
    )

    with pytest.raises(WorkflowResultCollisionError, match="reservation|run_id"):
        _load_with_outbox(
            tmp_path,
            run_id=run_id,
            payload=payload,
            primary_store=_PrimaryStore([], reservation=reservation),
            strict=True,
        )


def test_strict_v1_primary_and_v2_outbox_bind_to_reservation(tmp_path):
    run_id = "v1-primary-v2-outbox"
    incarnation = "inc-result"
    primary = _v1_payload(run_id, incarnation)
    outbox = _v2_payload(run_id, incarnation)
    reservation = SimpleNamespace(
        run_id=run_id,
        run_incarnation="inc-reserved",
    )

    with pytest.raises(WorkflowResultCollisionError, match="reservation|incarnation"):
        _load_with_outbox(
            tmp_path,
            run_id=run_id,
            payload=outbox,
            primary_store=_PrimaryStore(
                [_event(primary)],
                reservation=reservation,
            ),
            strict=True,
        )


def test_strict_v2_outbox_accepts_exact_reservation(tmp_path):
    run_id = "outbox-only-matching-reservation"
    incarnation = "inc-result"
    payload = _v2_payload(run_id, incarnation)
    reservation = SimpleNamespace(
        run_id=run_id,
        run_incarnation=incarnation,
    )

    reconciled = _load_with_outbox(
        tmp_path,
        run_id=run_id,
        payload=payload,
        primary_store=_PrimaryStore([], reservation=reservation),
        strict=True,
    )

    assert reconciled is not None
    assert reconciled.payload == payload


def test_strict_v1_outbox_remains_readable_without_reservation(tmp_path):
    run_id = "legacy-outbox-without-reservation"
    payload = _v1_payload(run_id, "legacy-inc")

    reconciled = _load_with_outbox(
        tmp_path,
        run_id=run_id,
        payload=payload,
        primary_store=_PrimaryStore([], reservation=None),
        strict=True,
    )

    assert reconciled is not None
    assert reconciled.payload == payload


def test_non_strict_v2_outbox_remains_readable_without_reservation(tmp_path):
    run_id = "non-strict-outbox-without-reservation"
    payload = _v2_payload(run_id, "inc-result")

    reconciled = _load_with_outbox(
        tmp_path,
        run_id=run_id,
        payload=payload,
        primary_store=_PrimaryStore([], reservation=None),
        strict=False,
    )

    assert reconciled is not None
    assert reconciled.payload == payload
