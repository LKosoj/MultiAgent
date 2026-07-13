from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest

from backend.fastapi_app.agui.auth import Principal
from backend.fastapi_app.agui.store import (
    AGUI_EVENT_STORE_SCHEMA_VERSION,
    WORKFLOW_RUN_SPEC_MAX_BYTES,
    ClaimedWorkflowRun,
    EventStore,
    WorkflowAdmissionRejected,
    WorkflowSupervisorOwnershipConflictError,
    workflow_result_event_key,
)


def _principal(subject: str, tenant: str) -> Principal:
    return Principal(subject=subject, tenant_id=tenant, roles=frozenset({"user"}))


def _spec(*, deadline_at_ms: int, marker: str = "run") -> dict[str, object]:
    return {
        "spec_version": 1,
        "workflow_path": f"/srv/workflows/{marker}.yaml",
        "parameters": {"question": marker, "nested": [None, True, 3, 1.5]},
        "session_id": f"session-{marker}",
        "client_id": None,
        "use_enhanced": True,
        "enable_telemetry": False,
        "run_incarnation": f"incarnation-{marker}",
        "deadline_at_ms": deadline_at_ms,
    }


def _create_run(
    store: EventStore,
    run_id: str,
    *,
    subject: str = "alice",
    tenant: str = "tenant-a",
    run_kind: str = "agui",
) -> None:
    store.create_run(
        run_id,
        f"thread-{run_id}",
        _principal(subject, tenant),
        run_kind=run_kind,
    )


def _enqueue(
    store: EventStore,
    run_id: str,
    *,
    deadline_at_ms: int,
    queue_limit: int = 20,
) -> None:
    store.enqueue_run(
        run_id,
        _spec(deadline_at_ms=deadline_at_ms, marker=run_id),
        deadline_at_ms,
        queue_limit,
    )


def _terminal_payload(
    run_id: str = "run-1",
    run_incarnation: str = "incarnation-run-1",
) -> dict[str, object]:
    from workflow.models import TextToSqlTerminalResult

    terminal = TextToSqlTerminalResult.from_mapping(
        {
            "run_id": run_id,
            "status": "failed",
            "reason_code": "RESULT_RECONCILIATION_FAILED",
            "sql": "",
            "generated": False,
            "approved": False,
            "executed": False,
            "dry_run": False,
            "audited": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "error": "worker failed",
            "execution": {},
            "audit": {},
            "persistence": {
                "status": "error",
                "error": "worker failed",
            },
        }
    ).to_mapping()
    return {
        "run_id": run_id,
        "thread_id": run_id,
        "run_incarnation": run_incarnation,
        "event_key": workflow_result_event_key(run_id, run_incarnation),
        "status": "failed",
        "success": False,
        "terminal_outcome": terminal,
        "result": terminal,
        "error": "worker failed",
        "artifacts": {"terminal_outcome": terminal},
        "snapshot": {"run_incarnation": run_incarnation},
    }


def test_v3_migration_adds_queue_columns_and_private_work_specs(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    store = EventStore(str(db_path))
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX idx_agui_runs_active_leases")
        conn.execute("DROP INDEX idx_agui_runs_queue")
        conn.execute("DROP TABLE workflow_run_specs")
        for column in (
            "worker_pid_started_at_ms",
            "attempt_started_at_ms",
            "cancel_requested_at_ms",
            "worker_heartbeat_at_ms",
            "worker_lease_expires_at_ms",
            "supervisor_id",
            "deadline_at_ms",
            "queued_at_ms",
        ):
            conn.execute(f"ALTER TABLE agui_runs DROP COLUMN {column}")
        conn.execute("PRAGMA user_version=3")
        conn.commit()
    finally:
        conn.close()

    migrated = EventStore(str(db_path))
    try:
        version = migrated._conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1] for row in migrated._conn.execute("PRAGMA table_info(agui_runs)")
        }
        assert version == AGUI_EVENT_STORE_SCHEMA_VERSION == 8
        assert {
            "queued_at_ms",
            "deadline_at_ms",
            "supervisor_id",
            "worker_lease_expires_at_ms",
            "worker_heartbeat_at_ms",
            "cancel_requested_at_ms",
            "attempt_started_at_ms",
            "worker_pid_started_at_ms",
        } <= columns
        assert migrated._conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'workflow_run_specs'"
        ).fetchone() == ("workflow_run_specs",)
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    finally:
        migrated.close()


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda value: value.pop("workflow_path"), "required keys"),
        (lambda value: value.__setitem__("parameters", {"bad": object()}), "primitive"),
        (lambda value: value.__setitem__("deadline_at_ms", value["deadline_at_ms"] + 1), "deadline"),
    ],
)
def test_enqueue_rejects_malformed_work_spec(tmp_path: Path, mutate, match: str) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        _create_run(store, "run-1")
        deadline = int(time.time() * 1000) + 60_000
        value = _spec(deadline_at_ms=deadline)
        mutate(value)
        with pytest.raises(ValueError, match=match):
            store.enqueue_run("run-1", value, deadline, 1)
        assert store.get_run("run-1").status == "pending"
        assert store.load_work_spec("run-1") is None
    finally:
        store.close()


def test_enqueue_rejects_oversized_and_digest_mismatched_specs(tmp_path: Path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        deadline = int(time.time() * 1000) + 60_000
        _create_run(store, "oversized")
        oversized = _spec(deadline_at_ms=deadline)
        oversized["parameters"] = {"value": "x" * WORKFLOW_RUN_SPEC_MAX_BYTES}
        with pytest.raises(ValueError, match="byte limit"):
            store.enqueue_run("oversized", oversized, deadline, 2)

        _create_run(store, "digest")
        _enqueue(store, "digest", deadline_at_ms=deadline)
        store._conn.execute(
            "UPDATE workflow_run_specs SET spec_sha256 = ? WHERE run_id = ?",
            ("0" * 64, "digest"),
        )
        store._conn.commit()
        with pytest.raises(ValueError, match="digest"):
            store.load_work_spec("digest")
    finally:
        store.close()


def test_enqueue_rejects_cyclic_work_spec_before_serialization(tmp_path: Path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        deadline = int(time.time() * 1000) + 60_000
        _create_run(store, "cyclic")
        value = _spec(deadline_at_ms=deadline)
        cycle: list[object] = []
        cycle.append(cycle)
        value["parameters"] = {"cycle": cycle}
        with pytest.raises(ValueError, match="cycles"):
            store.enqueue_run("cyclic", value, deadline, 1)
    finally:
        store.close()


def test_queue_limit_rejects_without_changing_pending_run(tmp_path: Path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        deadline = int(time.time() * 1000) + 60_000
        _create_run(store, "queued")
        _create_run(store, "rejected")
        _enqueue(store, "queued", deadline_at_ms=deadline, queue_limit=1)

        with pytest.raises(WorkflowAdmissionRejected):
            _enqueue(store, "rejected", deadline_at_ms=deadline, queue_limit=1)

        assert store.get_run("queued").status == "queued"
        assert store.get_run("rejected").status == "pending"
        assert store.load_work_spec("rejected") is None
    finally:
        store.close()


def test_two_connections_enforce_caps_and_skip_saturated_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    first = EventStore(str(db_path))
    second = EventStore(str(db_path))
    try:
        deadline = int(time.time() * 1000) + 60_000
        for run_id, subject, tenant in (
            ("a-1", "alice", "tenant-a"),
            ("a-2", "alice", "tenant-a"),
            ("b-1", "bob", "tenant-b"),
        ):
            _create_run(first, run_id, subject=subject, tenant=tenant)
            _enqueue(first, run_id, deadline_at_ms=deadline)

        claim_a = first.claim_next_queued("supervisor-a", 2, 2, 1, deadline)
        claim_b = second.claim_next_queued("supervisor-b", 2, 2, 1, deadline)
        blocked = first.claim_next_queued("supervisor-c", 2, 2, 1, deadline)

        assert isinstance(claim_a, ClaimedWorkflowRun)
        assert claim_a.run.run_id == "a-1"
        assert isinstance(claim_b, ClaimedWorkflowRun)
        assert claim_b.run.run_id == "b-1"
        assert blocked is None
        assert first.get_run("a-2").status == "queued"
    finally:
        second.close()
        first.close()


def test_two_connections_cannot_race_past_global_cap(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    first = EventStore(str(db_path))
    second = EventStore(str(db_path))
    barrier = threading.Barrier(3)
    claims: list[ClaimedWorkflowRun | None] = []
    failures: list[BaseException] = []
    claims_lock = threading.Lock()
    try:
        deadline = int(time.time() * 1000) + 60_000
        for run_id, tenant in (("run-a", "tenant-a"), ("run-b", "tenant-b")):
            _create_run(first, run_id, tenant=tenant)
            _enqueue(first, run_id, deadline_at_ms=deadline)

        def claim(store: EventStore, supervisor_id: str) -> None:
            try:
                barrier.wait()
                result = store.claim_next_queued(
                    supervisor_id, 1, 1, 1, deadline
                )
                with claims_lock:
                    claims.append(result)
            except BaseException as exc:
                with claims_lock:
                    failures.append(exc)

        threads = [
            threading.Thread(target=claim, args=(first, "supervisor-a")),
            threading.Thread(target=claim, args=(second, "supervisor-b")),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        assert not failures
        assert all(not thread.is_alive() for thread in threads)
        assert sum(item is not None for item in claims) == 1
        assert sum(
            first.get_run(run_id).status == "running"
            for run_id in ("run-a", "run-b")
        ) == 1
    finally:
        second.close()
        first.close()


def test_lease_renewal_and_stale_reclaim_from_fresh_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    first = EventStore(str(db_path))
    try:
        now = int(time.time() * 1000)
        _create_run(first, "run-1")
        _enqueue(first, "run-1", deadline_at_ms=now + 120_000)
        claimed = first.claim_next_queued("supervisor-a", 1, 1, 1, now + 10_000)
        assert claimed is not None
        generation = claimed.claim.attempt_generation
        assert first.set_worker_pid("run-1", 4321, "supervisor-a", generation)
        assert first.renew_worker_lease(
            "run-1", "supervisor-a", generation, now + 20_000
        )
        assert not first.renew_worker_lease(
            "run-1", "supervisor-b", generation, now + 30_000
        )
        expiry = first.expire_queued_and_stale_leases(
            now + 20_001,
            "supervisor-reaper",
            now + 30_000,
        )
        assert expiry.changed_count == 1
        assert len(expiry.terminal_decisions) == 1
        decision = expiry.terminal_decisions[0]
        assert decision.action == "reap_then_requeue"
        assert decision.worker_pid == 4321
        assert decision.supervisor_id == "supervisor-reaper"
        assert first.get_run("run-1").status == "running"
        assert first.get_run("run-1").worker_pid == 4321
        assert first.requeue_stale_worker(
            "run-1",
            "supervisor-reaper",
            decision.attempt_generation,
            4321,
            now + 20_001,
        )
        assert not first.requeue_stale_worker(
            "run-1",
            "supervisor-reaper",
            decision.attempt_generation,
            4321,
            now + 20_001,
        )
    finally:
        first.close()

    second = EventStore(str(db_path))
    try:
        reclaimed = second.claim_next_queued(
            "supervisor-b", 1, 1, 1, int(time.time() * 1000) + 30_000
        )
        assert reclaimed is not None
        assert reclaimed.run.run_id == "run-1"
        assert reclaimed.work_spec.to_mapping()["run_incarnation"] == (
            "incarnation-run-1"
        )
    finally:
        second.close()


def test_claim_generation_is_monotonic_and_late_renewal_is_rejected(
    tmp_path: Path,
) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        now = int(time.time() * 1000)
        _create_run(store, "run-1")
        _enqueue(store, "run-1", deadline_at_ms=now + 120_000)

        claimed = store.claim_next_queued(
            "supervisor-a", 1, 1, 1, now + 10_000
        )
        assert claimed is not None
        first_generation = claimed.claim.attempt_generation
        assert claimed.claim.supervisor_id == "supervisor-a"
        assert first_generation == claimed.run.attempt_started_at_ms
        assert store.set_worker_pid(
            "run-1", 4321, "supervisor-a", first_generation
        )

        store._conn.execute(
            "UPDATE agui_runs SET worker_lease_expires_at_ms = ? WHERE run_id = ?",
            (now - 1, "run-1"),
        )
        store._conn.commit()
        assert not store.renew_worker_lease(
            "run-1", "supervisor-a", first_generation, now + 20_000
        )

        expiry = store.expire_queued_and_stale_leases(
            now,
            "supervisor-reaper",
            now + 10_000,
        )
        decision = expiry.terminal_decisions[0]
        assert decision.supervisor_id == "supervisor-reaper"
        assert decision.attempt_generation > first_generation
        assert store.requeue_stale_worker(
            "run-1",
            "supervisor-reaper",
            decision.attempt_generation,
            4321,
            now,
        )

        reclaimed = store.claim_next_queued(
            "supervisor-b", 1, 1, 1, now + 30_000
        )
        assert reclaimed is not None
        assert reclaimed.claim.attempt_generation > decision.attempt_generation
    finally:
        store.close()


def test_only_one_reaper_claims_an_expired_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    first = EventStore(str(db_path))
    second = EventStore(str(db_path))
    try:
        now = int(time.time() * 1000)
        _create_run(first, "run-1")
        _enqueue(first, "run-1", deadline_at_ms=now + 120_000)
        claimed = first.claim_next_queued(
            "supervisor-a", 1, 1, 1, now + 10_000
        )
        assert claimed is not None
        generation = claimed.claim.attempt_generation
        assert first.set_worker_pid("run-1", 4321, "supervisor-a", generation)
        first._conn.execute(
            "UPDATE agui_runs SET worker_lease_expires_at_ms = ? WHERE run_id = ?",
            (now - 1, "run-1"),
        )
        first._conn.commit()

        barrier = threading.Barrier(3)
        results = []

        def reap(store: EventStore, reaper_id: str) -> None:
            barrier.wait()
            results.append(
                store.expire_queued_and_stale_leases(
                    now,
                    reaper_id,
                    now + 10_000,
                )
            )

        threads = [
            threading.Thread(target=reap, args=(first, "reaper-a")),
            threading.Thread(target=reap, args=(second, "reaper-b")),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        decisions = [
            decision
            for result in results
            for decision in result.terminal_decisions
        ]
        assert len(decisions) == 1
        assert decisions[0].supervisor_id in {"reaper-a", "reaper-b"}
    finally:
        second.close()
        first.close()


def test_result_pending_is_not_expired_or_claimed_again(tmp_path: Path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        now = int(time.time() * 1000)
        _create_run(store, "run-1", run_kind="text_to_sql")
        _enqueue(store, "run-1", deadline_at_ms=now + 120_000)
        claimed = store.claim_next_queued(
            "supervisor-a", 1, 1, 1, now + 10_000
        )
        assert claimed is not None
        generation = claimed.claim.attempt_generation

        assert store.mark_worker_result_pending(
            "run-1", "supervisor-a", generation
        )
        pending = store.get_run("run-1")
        assert pending is not None
        assert pending.status == "result_pending"
        assert pending.worker_lease_expires_at_ms is None
        assert (
            store.expire_queued_and_stale_leases(
                now + 60_000,
                "supervisor-reaper",
                now + 70_000,
            ).terminal_decisions
            == ()
        )
        assert (
            store.claim_next_queued(
                "supervisor-b", 1, 1, 1, now + 70_000
            )
            is None
        )
    finally:
        store.close()


def test_corrupt_spec_claim_is_fenced_decision_and_next_run_continues(
    tmp_path: Path,
) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        now = int(time.time() * 1000)
        for run_id in ("run-a", "run-b"):
            _create_run(store, run_id)
            _enqueue(store, run_id, deadline_at_ms=now + 120_000)
        store._conn.execute(
            "UPDATE workflow_run_specs SET spec_sha256 = ? WHERE run_id = ?",
            ("0" * 64, "run-a"),
        )
        store._conn.commit()

        assert store.has_work_spec("run-a")
        with pytest.raises(ValueError, match="digest"):
            store.load_work_spec("run-a")
        invalid = store.claim_next_queued(
            "supervisor-a", 1, 1, 1, now + 10_000
        )
        assert invalid is not None
        assert invalid.run.run_id == "run-a"
        assert invalid.invalid_work_spec
        assert invalid.work_spec is None
        assert store.finish_worker(
            "run-a",
            "supervisor-a",
            invalid.claim.attempt_generation,
            {"status": "failed", "reason": "INVALID_WORK_SPEC"},
        )

        valid = store.claim_next_queued(
            "supervisor-a", 1, 1, 1, now + 20_000
        )
        assert valid is not None
        assert valid.run.run_id == "run-b"
        assert not valid.invalid_work_spec
    finally:
        store.close()


def test_old_worker_result_loses_after_fence_and_requeued_generation(
    tmp_path: Path,
) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        now = int(time.time() * 1000)
        _create_run(store, "run-1", run_kind="text_to_sql")
        assert store.reserve_workflow_run(
            "run-1",
            "incarnation-run-1",
            "session-run-1",
            "text_to_sql",
        )
        _enqueue(store, "run-1", deadline_at_ms=now + 120_000)
        old_claim = store.claim_next_queued(
            "supervisor-old", 1, 1, 1, now + 10_000
        )
        assert old_claim is not None
        old_generation = old_claim.claim.attempt_generation
        assert store.set_worker_pid(
            "run-1", 4321, "supervisor-old", old_generation
        )
        fenced_generation = store.fence_worker_attempt(
            "run-1",
            "supervisor-old",
            old_generation,
            now + 20_000,
        )
        assert fenced_generation is not None
        assert store.requeue_stale_worker(
            "run-1",
            "supervisor-old",
            fenced_generation,
            4321,
            now,
        )
        new_claim = store.claim_next_queued(
            "supervisor-new", 1, 1, 1, now + 30_000
        )
        assert new_claim is not None

        with pytest.raises(WorkflowSupervisorOwnershipConflictError):
            store.finalize_run_with_event(
                "run-1",
                _terminal_payload(),
                expected_supervisor_id="supervisor-old",
                expected_attempt_generation=old_generation,
            )
        seq = store.finalize_run_with_event(
            "run-1",
            _terminal_payload(),
            expected_supervisor_id="supervisor-new",
            expected_attempt_generation=new_claim.claim.attempt_generation,
        )
        assert seq > 0
    finally:
        store.close()


def test_startup_durable_scan_uses_raw_spec_existence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import importlib
    import sys

    from workflow import state_files

    main_db_path = tmp_path / "main-events.db"
    monkeypatch.setattr(
        state_files,
        "default_state_database_path",
        lambda *_args, **_kwargs: main_db_path,
    )
    sys.modules.pop("backend.fastapi_app.main", None)
    main = importlib.import_module("backend.fastapi_app.main")
    main.store.close()

    store = EventStore(str(tmp_path / "events.db"))
    try:
        now = int(time.time() * 1000)
        _create_run(store, "run-corrupt")
        _enqueue(store, "run-corrupt", deadline_at_ms=now + 120_000)
        store._conn.execute(
            "UPDATE workflow_run_specs SET spec_sha256 = ? WHERE run_id = ?",
            ("0" * 64, "run-corrupt"),
        )
        store._conn.commit()
        monkeypatch.setattr(main, "store", store)

        assert main._durable_supervised_run_ids() == frozenset({"run-corrupt"})
    finally:
        store.close()
        sys.modules.pop("backend.fastapi_app.main", None)


def test_queued_cancelled_run_never_claims_and_clears_spec(tmp_path: Path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        deadline = int(time.time() * 1000) + 60_000
        _create_run(store, "run-1")
        _enqueue(store, "run-1", deadline_at_ms=deadline)

        assert store.request_cancel("run-1") == "cancelled"
        assert store.claim_next_queued("supervisor", 1, 1, 1, deadline) is None
        assert store.load_work_spec("run-1") is None
    finally:
        store.close()


@pytest.mark.parametrize(
    ("cancel", "expected_status", "expected_reason"),
    [
        (False, "timed_out", "DEADLINE_EXCEEDED"),
        (True, "cancelled", "CANCEL_REQUESTED"),
    ],
)
def test_text_to_sql_queued_terminal_requires_external_event_decision(
    tmp_path: Path,
    cancel: bool,
    expected_status: str,
    expected_reason: str,
) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        now = int(time.time() * 1000)
        _create_run(store, "run-1", run_kind="text_to_sql")
        _enqueue(store, "run-1", deadline_at_ms=now + 60_000)
        if cancel:
            assert store.request_cancel("run-1") == "queued"
            expiry_at = now + 1
        else:
            expiry_at = now + 60_001

        expiry = store.expire_queued_and_stale_leases(
            expiry_at,
            "supervisor-reaper",
            expiry_at + 10_000,
        )

        assert [
            (item.run_id, item.terminal_status, item.terminal_reason)
            for item in expiry.terminal_decisions
        ] == [("run-1", expected_status, expected_reason)]
        assert store.get_run("run-1").status == "queued"
        assert store.load_work_spec("run-1") is not None
    finally:
        store.close()


@pytest.mark.parametrize("method", ["transition", "reconcile", "finish", "finalize"])
def test_every_terminal_store_path_clears_private_spec(
    tmp_path: Path,
    method: str,
) -> None:
    store = EventStore(str(tmp_path / f"{method}.db"))
    try:
        deadline = int(time.time() * 1000) + 60_000
        run_kind = "text_to_sql" if method == "finalize" else "agui"
        _create_run(store, "run-1", run_kind=run_kind)
        if run_kind == "text_to_sql":
            store.reserve_workflow_run(
                "run-1", "incarnation-run-1", "session-run-1", "text_to_sql"
            )
        _enqueue(store, "run-1", deadline_at_ms=deadline)

        if method == "transition":
            assert store.transition_run("run-1", {"queued"}, "failed", reason="x")
        elif method == "reconcile":
            assert store.reconcile_non_terminal_runs() == 1
        elif method == "finish":
            claimed = store.claim_next_queued("supervisor", 1, 1, 1, deadline)
            assert claimed is not None
            assert store.finish_worker(
                "run-1",
                "supervisor",
                claimed.claim.attempt_generation,
                {"status": "failed", "reason": "crashed"},
            )
        else:
            from workflow.models import TextToSqlTerminalResult

            terminal = TextToSqlTerminalResult.from_mapping(
                {
                    "run_id": "run-1",
                    "status": "failed",
                    "reason_code": "RESULT_RECONCILIATION_FAILED",
                    "sql": "",
                    "generated": False,
                    "approved": False,
                    "executed": False,
                    "dry_run": False,
                    "audited": False,
                    "data": [],
                    "columns": [],
                    "rows_affected": 0,
                    "error": "worker failed",
                    "execution": {},
                    "audit": {},
                    "persistence": {
                        "status": "error",
                        "error": "worker failed",
                    },
                }
            ).to_mapping()
            payload = {
                "run_id": "run-1",
                "thread_id": "run-1",
                "run_incarnation": "incarnation-run-1",
                "event_key": workflow_result_event_key(
                    "run-1", "incarnation-run-1"
                ),
                "status": "failed",
                "success": False,
                "terminal_outcome": terminal,
                "result": terminal,
                "error": "worker failed",
                "artifacts": {"terminal_outcome": terminal},
                "snapshot": {"run_incarnation": "incarnation-run-1"},
            }
            store.finalize_run_with_event("run-1", payload)

        assert store.load_work_spec("run-1") is None
    finally:
        store.close()


def test_work_spec_never_appears_in_run_or_event_projection(tmp_path: Path) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        deadline = int(time.time() * 1000) + 60_000
        _create_run(store, "run-1")
        _enqueue(store, "run-1", deadline_at_ms=deadline)
        store.append("run-1", "CUSTOM", {"visible": True})

        run_projection = json.dumps(store.get_run("run-1").__dict__, default=list)
        event_projection = json.dumps([event.payload for event in store.list_after("run-1")])
        assert "workflow_path" not in run_projection
        assert "question" not in run_projection
        assert "workflow_path" not in event_projection
        assert "question" not in event_projection

        row = store._conn.execute(
            "SELECT spec_json, spec_sha256 FROM workflow_run_specs WHERE run_id = ?",
            ("run-1",),
        ).fetchone()
        assert row is not None
        assert hashlib.sha256(row[0].encode("utf-8")).hexdigest() == row[1]
    finally:
        store.close()


def test_supervisor_finalize_precondition_rejects_changed_running_owner(
    tmp_path: Path,
) -> None:
    store = EventStore(str(tmp_path / "events.db"))
    try:
        now = int(time.time() * 1_000)
        _create_run(store, "run-1", run_kind="text_to_sql")
        assert store.reserve_workflow_run(
            "run-1",
            "incarnation-run-1",
            "session-run-1",
            "text_to_sql",
        )
        _enqueue(store, "run-1", deadline_at_ms=now + 60_000)
        claimed = store.claim_next_queued(
            "supervisor-current",
            1,
            1,
            1,
            now + 10_000,
        )
        assert claimed is not None

        with pytest.raises(WorkflowSupervisorOwnershipConflictError):
            store.finalize_run_with_event(
                "run-1",
                _terminal_payload(),
                expected_supervisor_id="supervisor-stale",
                expected_attempt_generation=claimed.claim.attempt_generation,
            )

        stored = store.get_run("run-1")
        assert stored is not None
        assert stored.status == "running"
        assert stored.supervisor_id == "supervisor-current"
        assert list(store.list_after("run-1")) == []
        assert store.load_work_spec("run-1") is not None
    finally:
        store.close()


def test_supervisor_finalize_holds_write_cas_against_stale_requeue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "events.db"
    finalizer = EventStore(str(db_path))
    recoverer = EventStore(str(db_path))
    entered_insert = threading.Event()
    release_insert = threading.Event()
    finalize_results: list[int] = []
    requeue_results: list[bool] = []
    failures: list[BaseException] = []
    try:
        now = int(time.time() * 1_000)
        _create_run(finalizer, "run-1", run_kind="text_to_sql")
        assert finalizer.reserve_workflow_run(
            "run-1",
            "incarnation-run-1",
            "session-run-1",
            "text_to_sql",
        )
        _enqueue(finalizer, "run-1", deadline_at_ms=now + 60_000)
        claimed = finalizer.claim_next_queued(
            "supervisor-current",
            1,
            1,
            1,
            now + 5_000,
        )
        assert claimed is not None
        expiry = finalizer.expire_queued_and_stale_leases(
            now + 5_001,
            "supervisor-reaper",
            now + 15_000,
        )
        assert len(expiry.terminal_decisions) == 1
        reaper_generation = expiry.terminal_decisions[0].attempt_generation
        assert reaper_generation is not None
        original_insert = finalizer._insert_workflow_result_locked

        def blocked_insert(**kwargs):
            entered_insert.set()
            assert release_insert.wait(timeout=5)
            return original_insert(**kwargs)

        monkeypatch.setattr(
            finalizer,
            "_insert_workflow_result_locked",
            blocked_insert,
        )

        def finalize() -> None:
            try:
                finalize_results.append(
                    finalizer.finalize_run_with_event(
                        "run-1",
                        _terminal_payload(),
                        expected_supervisor_id="supervisor-reaper",
                        expected_attempt_generation=reaper_generation,
                    )
                )
            except BaseException as exc:
                failures.append(exc)

        def requeue() -> None:
            try:
                requeue_results.append(
                    recoverer.requeue_stale_worker(
                        "run-1",
                        "supervisor-reaper",
                        reaper_generation,
                        None,
                        now + 5_001,
                    )
                )
            except BaseException as exc:
                failures.append(exc)

        finalize_thread = threading.Thread(target=finalize)
        finalize_thread.start()
        assert entered_insert.wait(timeout=5)
        requeue_thread = threading.Thread(target=requeue)
        requeue_thread.start()
        release_insert.set()
        finalize_thread.join(timeout=5)
        requeue_thread.join(timeout=5)

        assert not failures
        assert not finalize_thread.is_alive()
        assert not requeue_thread.is_alive()
        assert finalize_results == [1]
        assert requeue_results == [False]
        stored = finalizer.get_run("run-1")
        assert stored is not None
        assert stored.status == "failed"
        assert len(list(finalizer.list_after("run-1"))) == 1
    finally:
        release_insert.set()
        recoverer.close()
        finalizer.close()
