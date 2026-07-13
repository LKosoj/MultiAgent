"""Lease-backed operational retention for Text-to-SQL history and traces."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .auth import Principal
from .store import (
    EventStore,
    OperationalRetentionLease,
    OperationalRetentionState,
)


logger = logging.getLogger(__name__)
OPERATIONAL_RETENTION_SCOPE = "t14-operational-retention-v1"
DAY_MS = 24 * 60 * 60 * 1000


class _TelemetryRetention(Protocol):
    def cleanup_old_traces_report(self, *, max_age_days: int) -> dict[str, Any]: ...

    def enforce_trace_size_limit(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class OperationalRetentionPolicy:
    history_max_age_days: int
    trace_retention_days: int
    cleanup_interval_ms: int
    lease_duration_ms: int

    def __post_init__(self) -> None:
        for field_name in (
            "history_max_age_days",
            "trace_retention_days",
            "cleanup_interval_ms",
            "lease_duration_ms",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


def _non_negative_report_integer(
    report: dict[str, Any],
    field_name: str,
) -> int:
    value = report.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"retention report {field_name} must be non-negative")
    return value


def _report_failures(report: dict[str, Any]) -> list[Any]:
    failures = report.get("failures")
    if not isinstance(failures, list):
        raise ValueError("retention report failures must be a list")
    return failures


class _OperationalRetentionLeaseHeartbeat:
    def __init__(
        self,
        store: EventStore,
        lease: OperationalRetentionLease,
        *,
        lease_duration_ms: int,
        clock_ms: Callable[[], int],
    ) -> None:
        self._store = store
        self._lease = lease
        self._lease_duration_ms = lease_duration_ms
        self._clock_ms = clock_ms
        self._renew_lock = threading.Lock()
        self._failure_lock = threading.Lock()
        self._failure: Optional[Exception] = None
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=(
                "operational-retention-heartbeat-"
                f"{lease.owner_id}-{lease.generation}"
            ),
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _raise_failure(self) -> None:
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            raise failure

    def renew(self) -> None:
        with self._renew_lock:
            self._raise_failure()
            self._lease = self._store.renew_operational_retention(
                self._lease,
                now_ms=self._clock_ms(),
                lease_duration_ms=self._lease_duration_ms,
            )

    def _run(self) -> None:
        interval_seconds = max(1, self._lease_duration_ms // 3) / 1000
        while not self._stop_event.wait(interval_seconds):
            try:
                self.renew()
            except Exception as exc:
                with self._failure_lock:
                    self._failure = exc
                return

    def stop(self, *, raise_failure: bool = True) -> None:
        self._stop_event.set()
        self._thread.join()
        if raise_failure:
            self._raise_failure()


class OperationalRetentionCoordinator:
    def __init__(
        self,
        store: EventStore,
        policy: OperationalRetentionPolicy,
        *,
        telemetry_manager_factory: Callable[[], _TelemetryRetention],
        coordinator_id: Optional[str] = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        executor: Optional[ThreadPoolExecutor] = None,
    ) -> None:
        if not isinstance(store, EventStore):
            raise TypeError("store must be an EventStore")
        if not isinstance(policy, OperationalRetentionPolicy):
            raise TypeError("policy must be an OperationalRetentionPolicy")
        if not callable(telemetry_manager_factory) or not callable(clock_ms):
            raise TypeError("retention factories and clocks must be callable")
        coordinator_id = coordinator_id or (
            f"pid-{os.getpid()}-{uuid.uuid4().hex}"
        )
        if not isinstance(coordinator_id, str) or not coordinator_id.strip():
            raise ValueError("coordinator_id must be non-blank text")
        self._store = store
        self.policy = policy
        self.coordinator_id = coordinator_id
        self._telemetry_manager_factory = telemetry_manager_factory
        self._clock_ms = clock_ms
        self._executor = executor
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._admin = Principal(
            subject="operational-retention",
            tenant_id="system",
            roles=frozenset({"admin"}),
        )

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("retention clock must return non-negative integer ms")
        return value

    def run_once_if_due(self) -> Optional[OperationalRetentionState]:
        started_at_ms = self._now_ms()
        lease = self._store.claim_operational_retention(
            OPERATIONAL_RETENTION_SCOPE,
            self.coordinator_id,
            now_ms=started_at_ms,
            lease_duration_ms=self.policy.lease_duration_ms,
        )
        if lease is None:
            return None

        heartbeat = _OperationalRetentionLeaseHeartbeat(
            self._store,
            lease,
            lease_duration_ms=self.policy.lease_duration_ms,
            clock_ms=self._now_ms,
        )
        heartbeat.start()
        counters = {
            "history_deleted_rows": 0,
            "history_failures": 0,
            "trace_deleted_files": 0,
            "trace_deleted_bytes": 0,
            "trace_rotations": 0,
            "trace_bytes_before": 0,
            "trace_bytes_after": 0,
            "trace_cleanup_failures": 0,
            "trace_size_failures": 0,
        }
        failures: list[str] = []
        history_cutoff = (
            started_at_ms - self.policy.history_max_age_days * DAY_MS
        )
        try:
            heartbeat.renew()
            try:
                counters["history_deleted_rows"] = (
                    self._store.admin_prune_text_to_sql_history(
                        self._admin,
                        before_created_at_ms=max(0, history_cutoff),
                    )
                )
            except Exception as exc:
                counters["history_failures"] = 1
                failures.append(f"history pruning failed: {exc}")
            heartbeat.renew()

            telemetry: Optional[_TelemetryRetention]
            try:
                telemetry = self._telemetry_manager_factory()
            except Exception as exc:
                telemetry = None
                counters["trace_cleanup_failures"] = 1
                counters["trace_size_failures"] = 1
                failures.append(f"telemetry manager failed: {exc}")

            if telemetry is not None:
                heartbeat.renew()
                try:
                    cleanup = telemetry.cleanup_old_traces_report(
                        max_age_days=self.policy.trace_retention_days
                    )
                    if not isinstance(cleanup, dict):
                        raise ValueError("trace cleanup report must be an object")
                    counters["trace_deleted_files"] = (
                        _non_negative_report_integer(cleanup, "deleted_count")
                    )
                    counters["trace_deleted_bytes"] = (
                        _non_negative_report_integer(cleanup, "deleted_bytes")
                    )
                    cleanup_failures = _report_failures(cleanup)
                    counters["trace_cleanup_failures"] = len(cleanup_failures)
                    if cleanup_failures:
                        failures.append(
                            "trace cleanup reported "
                            f"{len(cleanup_failures)} failures"
                        )
                except Exception as exc:
                    counters["trace_cleanup_failures"] = max(
                        1,
                        counters["trace_cleanup_failures"],
                    )
                    failures.append(f"trace cleanup failed: {exc}")
                heartbeat.renew()

                try:
                    size_report = telemetry.enforce_trace_size_limit()
                    if not isinstance(size_report, dict):
                        raise ValueError("trace size report must be an object")
                    counters["trace_rotations"] = (
                        _non_negative_report_integer(size_report, "rotations")
                    )
                    counters["trace_bytes_before"] = (
                        _non_negative_report_integer(size_report, "bytes_before")
                    )
                    counters["trace_bytes_after"] = (
                        _non_negative_report_integer(size_report, "bytes_after")
                    )
                    size_failures = _report_failures(size_report)
                    counters["trace_size_failures"] = len(size_failures)
                    if size_failures:
                        failures.append(
                            "trace size enforcement reported "
                            f"{len(size_failures)} failures"
                        )
                except Exception as exc:
                    counters["trace_size_failures"] = max(
                        1,
                        counters["trace_size_failures"],
                    )
                    failures.append(f"trace size enforcement failed: {exc}")
                heartbeat.renew()

            completed_at_ms = self._now_ms()
            heartbeat.renew()
            heartbeat.stop()
            success = not failures
            error = None if success else "; ".join(failures)
            return self._store.complete_operational_retention(
                lease,
                now_ms=completed_at_ms,
                next_due_at_ms=(
                    completed_at_ms + self.policy.cleanup_interval_ms
                ),
                success=success,
                counters=counters,
                error=error,
            )
        finally:
            heartbeat.stop(raise_failure=False)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        await asyncio.get_running_loop().run_in_executor(
            self._executor, self.run_once_if_due
        )
        self._task = asyncio.create_task(
            self._run_loop(),
            name="operational-retention-coordinator",
        )

    async def _run_loop(self) -> None:
        interval_seconds = self.policy.cleanup_interval_ms / 1000
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=interval_seconds,
                )
            except asyncio.TimeoutError:
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        self._executor, self.run_once_if_due
                    )
                except Exception:
                    logger.exception("Operational retention iteration failed")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        await task
        self._task = None


def create_operational_retention_coordinator(
    store: EventStore,
    *,
    executor: Optional[ThreadPoolExecutor] = None,
) -> OperationalRetentionCoordinator:
    from configuration_api import get_configuration_manager
    from telemetry import get_telemetry_manager

    config = get_configuration_manager().get_config()
    interval_ms = int(config.system.cleanup_interval_hours) * 60 * 60 * 1000
    policy = OperationalRetentionPolicy(
        history_max_age_days=int(config.logging.max_age_days),
        trace_retention_days=int(config.telemetry.trace_retention_days),
        cleanup_interval_ms=interval_ms,
        lease_duration_ms=15 * 60 * 1000,
    )

    def telemetry_factory():
        return get_telemetry_manager(
            traces_dir=config.telemetry.traces_dir,
            enabled=config.telemetry.enabled,
            max_trace_file_size_mb=config.telemetry.max_trace_file_size_mb,
        )

    return OperationalRetentionCoordinator(
        store,
        policy,
        telemetry_manager_factory=telemetry_factory,
        executor=executor,
    )


__all__ = [
    "OPERATIONAL_RETENTION_SCOPE",
    "OperationalRetentionCoordinator",
    "OperationalRetentionPolicy",
    "create_operational_retention_coordinator",
]
