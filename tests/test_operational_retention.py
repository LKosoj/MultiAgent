from __future__ import annotations

import importlib
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from backend.fastapi_app.agui.auth import Principal
from backend.fastapi_app.agui.retention import (
    OPERATIONAL_RETENTION_SCOPE,
    OperationalRetentionCoordinator,
    OperationalRetentionPolicy,
    create_operational_retention_coordinator,
)
from backend.fastapi_app.agui.store import (
    EventStore,
    OperationalRetentionLeaseConflictError,
)
from workflow.models import TextToSqlTerminalResult, TextToSqlTerminalStatus


DAY_MS = 24 * 60 * 60 * 1000


def test_v5_store_migrates_retention_state_additively(tmp_path) -> None:
    path = tmp_path / "events.db"
    initial = EventStore(str(path))
    initial.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE operational_retention_state")
        connection.execute("PRAGMA user_version=5")

    migrated = EventStore(str(path))
    try:
        with sqlite3.connect(path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            table = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'operational_retention_state'
                """
            ).fetchone()
        assert version == 8
        assert table == ("operational_retention_state",)
    finally:
        migrated.close()


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


def _history(store: EventStore, run_id: str, created_at_ms: int) -> None:
    owner = Principal("alice", "tenant-1", frozenset({"user"}))
    store.create_run(
        run_id,
        f"thread-{run_id}",
        owner,
        run_kind="text_to_sql",
        status="cancelled",
    )
    store.upsert_text_to_sql_history(
        owner,
        _terminal(run_id),
        created_at_ms=created_at_ms,
        dialect="postgres",
        profile_name="strict",
    )


def test_retention_lease_has_one_winner_and_persists_completion(tmp_path) -> None:
    path = tmp_path / "events.db"
    first_store = EventStore(str(path))
    second_store = EventStore(str(path))
    try:
        first = first_store.claim_operational_retention(
            "t14",
            "worker-a",
            now_ms=1000,
            lease_duration_ms=500,
        )
        second = second_store.claim_operational_retention(
            "t14",
            "worker-b",
            now_ms=1000,
            lease_duration_ms=500,
        )

        assert first is not None
        assert second is None
        completed = first_store.complete_operational_retention(
            first,
            now_ms=1100,
            next_due_at_ms=2100,
            success=True,
            counters={"history_deleted_rows": 3},
        )
        assert completed.status == "succeeded"
        assert completed.last_attempt_at_ms == 1000
        assert completed.last_success_at_ms == 1100
        assert completed.next_due_at_ms == 2100
        assert completed.counters == {"history_deleted_rows": 3}
        assert second_store.claim_operational_retention(
            "t14",
            "worker-b",
            now_ms=2000,
            lease_duration_ms=500,
        ) is None
    finally:
        second_store.close()
        first_store.close()

    reopened = EventStore(str(path))
    try:
        assert reopened.get_operational_retention_state("t14") == completed
    finally:
        reopened.close()


def test_expired_lease_is_fenced_from_late_completion(tmp_path) -> None:
    path = tmp_path / "events.db"
    first_store = EventStore(str(path))
    second_store = EventStore(str(path))
    try:
        stale = first_store.claim_operational_retention(
            "t14",
            "worker-a",
            now_ms=1000,
            lease_duration_ms=100,
        )
        winner = second_store.claim_operational_retention(
            "t14",
            "worker-b",
            now_ms=1100,
            lease_duration_ms=100,
        )
        assert stale is not None
        assert winner is not None
        assert winner.generation == stale.generation + 1

        with pytest.raises(OperationalRetentionLeaseConflictError):
            first_store.complete_operational_retention(
                stale,
                now_ms=1150,
                next_due_at_ms=2000,
                success=True,
                counters={},
            )
    finally:
        second_store.close()
        first_store.close()


def test_retention_lease_renewal_is_generation_fenced(tmp_path) -> None:
    path = tmp_path / "events.db"
    first_store = EventStore(str(path))
    second_store = EventStore(str(path))
    try:
        claimed = first_store.claim_operational_retention(
            "t14",
            "worker-a",
            now_ms=1000,
            lease_duration_ms=100,
        )
        assert claimed is not None

        renewed = first_store.renew_operational_retention(
            claimed,
            now_ms=1050,
            lease_duration_ms=100,
        )

        assert renewed.generation == claimed.generation
        assert renewed.expires_at_ms == 1150
        assert second_store.claim_operational_retention(
            "t14",
            "worker-b",
            now_ms=1100,
            lease_duration_ms=100,
        ) is None
        takeover = second_store.claim_operational_retention(
            "t14",
            "worker-b",
            now_ms=1150,
            lease_duration_ms=100,
        )
        assert takeover is not None
        assert takeover.generation == claimed.generation + 1
        with pytest.raises(OperationalRetentionLeaseConflictError):
            first_store.renew_operational_retention(
                renewed,
                now_ms=1150,
                lease_duration_ms=100,
            )
    finally:
        second_store.close()
        first_store.close()


class _Telemetry:
    def __init__(self, *, cleanup_failures=None, size_failures=None):
        self.cleanup_failures = cleanup_failures or []
        self.size_failures = size_failures or []
        self.cleanup_calls = []
        self.size_calls = 0

    def cleanup_old_traces_report(self, *, max_age_days):
        self.cleanup_calls.append(max_age_days)
        return {
            "deleted_files": ["old.jsonl"],
            "deleted_count": 1,
            "deleted_bytes": 40,
            "failures": list(self.cleanup_failures),
        }

    def enforce_trace_size_limit(self):
        self.size_calls += 1
        return {
            "rotations": 2,
            "files": ["run.1.jsonl", "run.jsonl"],
            "bytes_before": 100,
            "bytes_after": 100,
            "failures": list(self.size_failures),
        }


def _policy() -> OperationalRetentionPolicy:
    return OperationalRetentionPolicy(
        history_max_age_days=7,
        trace_retention_days=9,
        cleanup_interval_ms=1000,
        lease_duration_ms=500,
    )


def test_coordinator_runs_due_maintenance_and_records_exact_metrics(tmp_path) -> None:
    now_ms = 20 * DAY_MS
    store = EventStore(str(tmp_path / "events.db"))
    telemetry = _Telemetry()
    try:
        _history(store, "old-run", now_ms - 8 * DAY_MS)
        _history(store, "recent-run", now_ms - DAY_MS)
        coordinator = OperationalRetentionCoordinator(
            store,
            _policy(),
            telemetry_manager_factory=lambda: telemetry,
            coordinator_id="worker-a",
            clock_ms=lambda: now_ms,
        )

        state = coordinator.run_once_if_due()

        assert state is not None
        assert state.status == "succeeded"
        assert state.counters == {
            "history_deleted_rows": 1,
            "history_failures": 0,
            "trace_deleted_files": 1,
            "trace_deleted_bytes": 40,
            "trace_rotations": 2,
            "trace_bytes_before": 100,
            "trace_bytes_after": 100,
            "trace_cleanup_failures": 0,
            "trace_size_failures": 0,
        }
        assert state.next_due_at_ms == now_ms + 1000
        assert telemetry.cleanup_calls == [9]
        assert telemetry.size_calls == 1
        assert coordinator.run_once_if_due() is None
        admin = Principal("ops", "ops", frozenset({"admin"}))
        assert [
            entry.run_id
            for entry in store.admin_list_text_to_sql_history(admin)
        ] == ["recent-run"]
    finally:
        store.close()


def test_coordinator_persists_failed_result_for_retry(tmp_path) -> None:
    now_ms = 20 * DAY_MS
    store = EventStore(str(tmp_path / "events.db"))
    telemetry = _Telemetry(cleanup_failures=[{"error": "cannot delete"}])
    try:
        coordinator = OperationalRetentionCoordinator(
            store,
            _policy(),
            telemetry_manager_factory=lambda: telemetry,
            coordinator_id="worker-a",
            clock_ms=lambda: now_ms,
        )

        state = coordinator.run_once_if_due()

        assert state is not None
        assert state.status == "failed"
        assert state.last_success_at_ms is None
        assert "trace cleanup" in state.last_error
        assert state.next_due_at_ms == now_ms + 1000
        assert state.counters["trace_cleanup_failures"] == 1
    finally:
        store.close()


def test_coordinator_aborts_remaining_phases_after_lease_takeover(tmp_path) -> None:
    now_ms = [20 * DAY_MS]
    lease_duration_ms = 1_000_000
    path = tmp_path / "events.db"
    first_store = EventStore(str(path))
    second_store = EventStore(str(path))

    class TakeoverTelemetry(_Telemetry):
        def cleanup_old_traces_report(self, *, max_age_days):
            now_ms[0] += lease_duration_ms
            self.takeover = second_store.claim_operational_retention(
                OPERATIONAL_RETENTION_SCOPE,
                "worker-b",
                now_ms=now_ms[0],
                lease_duration_ms=lease_duration_ms,
            )
            assert self.takeover is not None
            return super().cleanup_old_traces_report(max_age_days=max_age_days)

    telemetry = TakeoverTelemetry()
    try:
        coordinator = OperationalRetentionCoordinator(
            first_store,
            OperationalRetentionPolicy(
                history_max_age_days=7,
                trace_retention_days=9,
                cleanup_interval_ms=1000,
                lease_duration_ms=lease_duration_ms,
            ),
            telemetry_manager_factory=lambda: telemetry,
            coordinator_id="worker-a",
            clock_ms=lambda: now_ms[0],
        )

        with pytest.raises(OperationalRetentionLeaseConflictError):
            coordinator.run_once_if_due()

        assert telemetry.cleanup_calls == [9]
        assert telemetry.size_calls == 0
        state = first_store.get_operational_retention_state(
            OPERATIONAL_RETENTION_SCOPE
        )
        assert state is not None
        assert state.status == "running"
        assert state.lease_owner == "worker-b"
        assert state.lease_generation == telemetry.takeover.generation
    finally:
        second_store.close()
        first_store.close()


def test_retention_factory_passes_configured_trace_size(monkeypatch, tmp_path) -> None:
    import configuration_api
    import telemetry

    config = SimpleNamespace(
        logging=SimpleNamespace(max_age_days=11),
        system=SimpleNamespace(cleanup_interval_hours=2),
        telemetry=SimpleNamespace(
            enabled=True,
            traces_dir="custom-traces",
            trace_retention_days=13,
            max_trace_file_size_mb=3.25,
        ),
    )
    manager = SimpleNamespace()
    calls = []
    monkeypatch.setattr(
        configuration_api,
        "get_configuration_manager",
        lambda: SimpleNamespace(get_config=lambda: config),
    )
    monkeypatch.setattr(
        telemetry,
        "get_telemetry_manager",
        lambda **kwargs: calls.append(kwargs) or manager,
    )
    store = EventStore(str(tmp_path / "events.db"))
    try:
        coordinator = create_operational_retention_coordinator(store)

        assert coordinator._telemetry_manager_factory() is manager
        assert calls == [
            {
                "traces_dir": "custom-traces",
                "enabled": True,
                "max_trace_file_size_mb": 3.25,
            }
        ]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_fastapi_lifespan_starts_and_stops_retention_once(
    monkeypatch,
    tmp_path,
) -> None:
    from workflow import state_files

    monkeypatch.delitem(sys.modules, "backend.fastapi_app.main", raising=False)
    monkeypatch.setattr(
        state_files,
        "default_state_database_path",
        lambda *_args, **_kwargs: tmp_path / "events.db",
    )
    main = importlib.import_module("backend.fastapi_app.main")

    events = []

    class Supervisor:
        def start(self):
            events.append("supervisor.start")

        def stop(self):
            events.append("supervisor.stop")

    class Retention:
        async def start(self):
            events.append("retention.start")

        async def stop(self):
            events.append("retention.stop")

    monkeypatch.setattr(main, "validate_auth_configuration", lambda: None)
    monkeypatch.setattr(
        main,
        "_import_legacy_text_to_sql_history_once",
        lambda: events.append("history.import"),
        raising=False,
    )
    monkeypatch.setattr(
        main,
        "configure_workflow_process_supervisor",
        lambda _store: Supervisor(),
    )
    monkeypatch.setattr(
        main,
        "create_operational_retention_coordinator",
        lambda _store, **_kwargs: Retention(),
    )
    monkeypatch.setattr(
        main,
        "_drain_workflow_results_before_reconcile",
        lambda: frozenset(),
    )
    monkeypatch.setattr(main, "_durable_supervised_run_ids", lambda: frozenset())
    monkeypatch.setattr(main.store, "reconcile_non_terminal_runs", lambda **_kw: 0)

    try:
        async with main._lifespan(main.app):
            events.append("running")
    finally:
        main.store.close()

    assert events == [
        "history.import",
        "supervisor.start",
        "retention.start",
        "running",
        "retention.stop",
        "supervisor.stop",
    ]
