from __future__ import annotations

import logging
import multiprocessing
import signal
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from backend.fastapi_app.agui.store import (
    WorkflowAdmissionRejected as StoreWorkflowAdmissionRejected,
)
from workflow.process_supervisor import (
    DEFAULT_WORKFLOW_PROCESS_CANCEL_GRACE_SECONDS,
    DEFAULT_WORKFLOW_PROCESS_GLOBAL_LIMIT,
    DEFAULT_WORKFLOW_PROCESS_LEASE_SECONDS,
    DEFAULT_WORKFLOW_PROCESS_PER_OWNER_LIMIT,
    DEFAULT_WORKFLOW_PROCESS_PER_TENANT_LIMIT,
    DEFAULT_WORKFLOW_PROCESS_QUEUE_LIMIT,
    SupervisorConfig,
    WorkflowAdmissionRejected,
    WorkflowProcessSupervisor,
    WorkflowSupervisorConfigError,
    validate_work_spec,
)


ENV_VALUES = {
    "WORKFLOW_PROCESS_GLOBAL_LIMIT": "4",
    "WORKFLOW_PROCESS_PER_TENANT_LIMIT": "3",
    "WORKFLOW_PROCESS_PER_OWNER_LIMIT": "2",
    "WORKFLOW_PROCESS_QUEUE_LIMIT": "20",
    "WORKFLOW_PROCESS_LEASE_SECONDS": "30",
    "WORKFLOW_PROCESS_CANCEL_GRACE_SECONDS": "5",
}


def _fake_process_entrypoint(
    run_id: str,
    work_spec: Mapping[str, object],
    claim_envelope: Mapping[str, object],
) -> None:
    del run_id, work_spec, claim_envelope


def _real_spawn_entrypoint(
    run_id: str,
    work_spec: Mapping[str, object],
    claim_envelope: Mapping[str, object],
) -> None:
    del run_id, claim_envelope
    parameters = work_spec["parameters"]
    assert isinstance(parameters, dict)
    time.sleep(float(parameters["delay"]))


def _work_spec(
    *,
    deadline_at_ms: int = 60_000,
    delay: float = 0.0,
) -> dict[str, object]:
    return {
        "spec_version": 1,
        "workflow_path": "/srv/git_projects/MultiAgent/workflow_pipelines/text_to_sql_pipeline.yaml",
        "parameters": {"query": "count orders", "delay": delay},
        "session_id": "session-1",
        "client_id": "owner:tenant:alice",
        "use_enhanced": True,
        "enable_telemetry": False,
        "run_incarnation": "inc-1",
        "deadline_at_ms": deadline_at_ms,
    }


@dataclass
class FakeClock:
    value_ms: int = 1_000

    def now_ms(self) -> int:
        return self.value_ms

    def advance(self, seconds: float) -> None:
        self.value_ms += int(seconds * 1_000)


@dataclass(frozen=True)
class FakeStoredRun:
    run_id: str
    deadline_at_ms: int


@dataclass(frozen=True)
class FakeWorkflowRunSpec:
    value: Mapping[str, object]

    def to_mapping(self) -> dict[str, object]:
        return dict(self.value)


@dataclass(frozen=True)
class FakeClaimEnvelope:
    supervisor_id: str = "supervisor-test"
    attempt_generation: int = 1_000


@dataclass(frozen=True)
class FakeClaimedWorkflowRun:
    run: FakeStoredRun
    work_spec: FakeWorkflowRunSpec | None
    claim: FakeClaimEnvelope = FakeClaimEnvelope()
    invalid_work_spec: bool = False


@dataclass(frozen=True)
class FakeExpiryDecision:
    run_id: str
    run_kind: str
    action: str
    terminal_status: str | None
    terminal_reason: str | None
    worker_pid: int | None
    supervisor_id: str | None
    attempt_generation: int | None
    worker_lease_expires_at_ms: int | None
    worker_pid_started_at_ms: int | None = None


@dataclass(frozen=True)
class FakeExpiryResult:
    changed_count: int
    terminal_decisions: tuple[FakeExpiryDecision, ...]


@dataclass(frozen=True)
class FakeRunState:
    run_id: str
    status: str = "running"
    supervisor_id: str | None = "supervisor-test"
    attempt_started_at_ms: int | None = 1_000
    worker_lease_expires_at_ms: int | None = 30_000
    cancel_requested_at_ms: int | None = None


class FakeStore:
    def __init__(self) -> None:
        self.enqueue_error: BaseException | None = None
        self.claims: list[FakeClaimedWorkflowRun] = []
        self.loaded_specs: dict[str, FakeWorkflowRunSpec] = {}
        self.renew_results: list[bool] = []
        self.fence_results: list[int | None] = []
        self.set_worker_pid_results: list[bool] = []
        self.cancel_results: dict[str, str] = {}
        self.run_states: dict[str, FakeRunState] = {}
        self.expiry_decisions: list[FakeExpiryDecision] = []
        self.enqueue_calls: list[dict[str, object]] = []
        self.claim_calls: list[dict[str, object]] = []
        self.renew_calls: list[dict[str, object]] = []
        self.fence_calls: list[dict[str, object]] = []
        self.set_worker_pid_calls: list[dict[str, object]] = []
        self.cancel_calls: list[str] = []
        self.finish_calls: list[dict[str, object]] = []
        self.expire_calls: list[dict[str, object]] = []
        self.requeue_calls: list[dict[str, object]] = []
        self.finished = threading.Event()
        self._lock = threading.Lock()

    def enqueue_run(
        self,
        run_id: str,
        work_spec: Mapping[str, object],
        deadline_at_ms: int,
        queue_limit: int,
    ) -> FakeStoredRun:
        self.enqueue_calls.append(
            {
                "run_id": run_id,
                "work_spec": dict(work_spec),
                "deadline_at_ms": deadline_at_ms,
                "queue_limit": queue_limit,
            }
        )
        if self.enqueue_error is not None:
            raise self.enqueue_error
        return FakeStoredRun(run_id, deadline_at_ms)

    def claim_next_queued(
        self,
        supervisor_id: str,
        global_limit: int,
        per_tenant_limit: int,
        per_owner_limit: int,
        lease_expires_at_ms: int,
    ) -> FakeClaimedWorkflowRun | None:
        self.claim_calls.append(
            {
                "supervisor_id": supervisor_id,
                "global_limit": global_limit,
                "per_tenant_limit": per_tenant_limit,
                "per_owner_limit": per_owner_limit,
                "lease_expires_at_ms": lease_expires_at_ms,
            }
        )
        with self._lock:
            return self.claims.pop(0) if self.claims else None

    def load_work_spec(self, run_id: str) -> FakeWorkflowRunSpec | None:
        return self.loaded_specs.get(run_id)

    def renew_worker_lease(
        self,
        run_id: str,
        supervisor_id: str,
        attempt_generation: int,
        lease_expires_at_ms: int,
    ) -> bool:
        self.renew_calls.append(
            {
                "run_id": run_id,
                "supervisor_id": supervisor_id,
                "attempt_generation": attempt_generation,
                "lease_expires_at_ms": lease_expires_at_ms,
            }
        )
        return self.renew_results.pop(0) if self.renew_results else True

    def set_worker_pid(
        self,
        run_id: str,
        worker_pid: int,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        worker_pid_started_at_ms: int | None = None,
    ) -> bool:
        self.set_worker_pid_calls.append(
            {
                "run_id": run_id,
                "worker_pid": worker_pid,
                "expected_supervisor_id": expected_supervisor_id,
                "expected_attempt_generation": expected_attempt_generation,
                "worker_pid_started_at_ms": worker_pid_started_at_ms,
            }
        )
        if self.set_worker_pid_results:
            return self.set_worker_pid_results.pop(0)
        return True

    def request_cancel(self, run_id: str) -> str:
        self.cancel_calls.append(run_id)
        return self.cancel_results.get(run_id, "running")

    def get_run(self, run_id: str) -> FakeRunState | None:
        return self.run_states.get(
            run_id,
            FakeRunState(run_id=run_id),
        )

    def finish_worker(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        terminal_outcome: Mapping[str, object],
    ) -> bool:
        self.finish_calls.append(
            {
                "run_id": run_id,
                "expected_supervisor_id": expected_supervisor_id,
                "expected_attempt_generation": expected_attempt_generation,
                "terminal_outcome": dict(terminal_outcome),
            }
        )
        self.finished.set()
        return True

    def expire_queued_and_stale_leases(
        self,
        now_ms: int,
        reaper_supervisor_id: str,
        reaper_lease_expires_at_ms: int,
    ) -> FakeExpiryResult:
        self.expire_calls.append(
            {
                "now_ms": now_ms,
                "reaper_supervisor_id": reaper_supervisor_id,
                "reaper_lease_expires_at_ms": reaper_lease_expires_at_ms,
            }
        )
        decisions = tuple(self.expiry_decisions)
        self.expiry_decisions.clear()
        return FakeExpiryResult(
            changed_count=len(decisions),
            terminal_decisions=decisions,
        )

    def requeue_stale_worker(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        expected_worker_pid: int | None,
        now_ms: int,
    ) -> bool:
        self.requeue_calls.append(
            {
                "run_id": run_id,
                "expected_supervisor_id": expected_supervisor_id,
                "expected_attempt_generation": expected_attempt_generation,
                "expected_worker_pid": expected_worker_pid,
                "now_ms": now_ms,
            }
        )
        return True

    def fence_worker_attempt(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        lease_expires_at_ms: int,
    ) -> int | None:
        self.fence_calls.append(
            {
                "run_id": run_id,
                "expected_supervisor_id": expected_supervisor_id,
                "expected_attempt_generation": expected_attempt_generation,
                "lease_expires_at_ms": lease_expires_at_ms,
            }
        )
        if self.fence_results:
            return self.fence_results.pop(0)
        return expected_attempt_generation + 1


class FakeProcess:
    next_pid = 10_000

    def __init__(self, *, target, args, name, daemon=False) -> None:
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.pid: int | None = None
        self.exitcode: int | None = None
        self.alive = False
        self.started = 0
        self.kill_calls = 0
        self.join_calls: list[float | None] = []
        self.start_error: BaseException | None = None

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started += 1
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        self.kill_calls += 1
        self.alive = False
        self.exitcode = -signal.SIGKILL


class FakeContext:
    def __init__(self) -> None:
        self.processes: list[FakeProcess] = []
        self.start_error: BaseException | None = None

    def Process(self, **kwargs) -> FakeProcess:
        process = FakeProcess(**kwargs)
        process.start_error = self.start_error
        self.processes.append(process)
        return process


class InvalidPidProcess(FakeProcess):
    def start(self) -> None:
        super().start()
        self.pid = 0


class InvalidPidContext(FakeContext):
    def Process(self, **kwargs) -> FakeProcess:
        process = InvalidPidProcess(**kwargs)
        process.start_error = self.start_error
        self.processes.append(process)
        return process


class UnkillableProcess(FakeProcess):
    """Simulates a child stuck in an uninterruptible syscall (D-state):
    it never reports as dead, even after SIGKILL."""

    def is_alive(self) -> bool:
        return True

    def kill(self) -> None:
        self.kill_calls += 1


class UnkillableContext(FakeContext):
    def Process(self, **kwargs) -> FakeProcess:
        process = UnkillableProcess(**kwargs)
        process.start_error = self.start_error
        self.processes.append(process)
        return process


class ManualThread:
    instances: list["ManualThread"] = []

    def __init__(self, *, target, name, daemon) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.joined = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True

    def is_alive(self) -> bool:
        return self.started and not self.joined


@pytest.fixture
def config() -> SupervisorConfig:
    return SupervisorConfig.from_environment(ENV_VALUES)


def _claim(
    run_id: str = "run-1",
    *,
    deadline_at_ms: int = 60_000,
    work_spec: Mapping[str, object] | None = None,
    supervisor_id: str = "supervisor-test",
    attempt_generation: int = 1_000,
    invalid_work_spec: bool = False,
) -> FakeClaimedWorkflowRun:
    return FakeClaimedWorkflowRun(
        run=FakeStoredRun(run_id, deadline_at_ms),
        work_spec=(
            None
            if invalid_work_spec
            else FakeWorkflowRunSpec(
                dict(work_spec or _work_spec(deadline_at_ms=deadline_at_ms))
            )
        ),
        claim=FakeClaimEnvelope(supervisor_id, attempt_generation),
        invalid_work_spec=invalid_work_spec,
    )


def _supervisor(
    store: FakeStore,
    config: SupervisorConfig,
    *,
    clock: FakeClock | None = None,
    context: object | None = None,
    group_signals: list[int] | None = None,
    terminalizer=None,
    stale_reaps: list[tuple[int, int | None]] | None = None,
    process_identity_probe=None,
    started: bool = True,
) -> WorkflowProcessSupervisor:
    clock = clock or FakeClock()
    group_signals = group_signals if group_signals is not None else []
    stale_reaps = stale_reaps if stale_reaps is not None else []
    supervisor = WorkflowProcessSupervisor(
        store,
        process_entrypoint=_fake_process_entrypoint,
        config=config,
        supervisor_id="supervisor-test",
        process_context=context or FakeContext(),
        now_ms=clock.now_ms,
        wait=lambda seconds: clock.advance(seconds) or False,
        thread_factory=ManualThread,
        process_group_signal=lambda _process, sig: group_signals.append(sig),
        process_identity_probe=process_identity_probe or (lambda _pid: None),
        terminalizer=terminalizer,
        stale_process_reaper=(
            lambda pid, expected_started_at_ms, _grace: stale_reaps.append(
                (pid, expected_started_at_ms)
            )
        ),
    )
    if started:
        assert supervisor.start() is True
    return supervisor


def test_config_parses_positive_environment_values_and_rejects_invalid() -> None:
    config = SupervisorConfig.from_environment(ENV_VALUES)

    assert (
        config.global_limit,
        config.per_tenant_limit,
        config.per_owner_limit,
        config.queue_limit,
        config.lease_seconds,
        config.cancel_grace_seconds,
    ) == (4, 3, 2, 20, 30, 5)

    for key, value in (
        ("WORKFLOW_PROCESS_GLOBAL_LIMIT", "0"),
        ("WORKFLOW_PROCESS_QUEUE_LIMIT", "-1"),
        ("WORKFLOW_PROCESS_LEASE_SECONDS", "1.5"),
        ("WORKFLOW_PROCESS_CANCEL_GRACE_SECONDS", ""),
    ):
        environment = dict(ENV_VALUES)
        environment[key] = value
        with pytest.raises(WorkflowSupervisorConfigError, match=key):
            SupervisorConfig.from_environment(environment)


def test_config_uses_documented_defaults_when_environment_is_absent() -> None:
    config = SupervisorConfig.from_environment({})

    assert (
        config.global_limit,
        config.per_tenant_limit,
        config.per_owner_limit,
        config.queue_limit,
        config.lease_seconds,
        config.cancel_grace_seconds,
    ) == (
        DEFAULT_WORKFLOW_PROCESS_GLOBAL_LIMIT,
        DEFAULT_WORKFLOW_PROCESS_PER_TENANT_LIMIT,
        DEFAULT_WORKFLOW_PROCESS_PER_OWNER_LIMIT,
        DEFAULT_WORKFLOW_PROCESS_QUEUE_LIMIT,
        DEFAULT_WORKFLOW_PROCESS_LEASE_SECONDS,
        DEFAULT_WORKFLOW_PROCESS_CANCEL_GRACE_SECONDS,
    ) == (4, 2, 1, 100, 30, 5)


def test_admission_rejection_is_the_store_exception() -> None:
    assert WorkflowAdmissionRejected is StoreWorkflowAdmissionRejected


def test_work_spec_requires_exact_primitive_persisted_shape() -> None:
    assert validate_work_spec(_work_spec()) == _work_spec()

    invalid_specs = (
        {},
        {**_work_spec(), "entrypoint": "time:sleep"},
        {**_work_spec(), "workflow_path": "relative.yaml"},
        {**_work_spec(), "parameters": {"bad": object()}},
        {**_work_spec(), "use_enhanced": "true"},
    )
    for invalid in invalid_specs:
        with pytest.raises(ValueError, match="work_spec"):
            validate_work_spec(invalid)


def test_submit_is_bounded_and_returns_explicit_queued_result(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    supervisor = _supervisor(store, config)
    spec = _work_spec(deadline_at_ms=20_000)

    result = supervisor.submit("run-1", spec, deadline_at_ms=20_000)

    assert result.run_id == "run-1"
    assert result.state == "queued"
    assert store.enqueue_calls == [
        {
            "run_id": "run-1",
            "work_spec": spec,
            "deadline_at_ms": 20_000,
            "queue_limit": config.queue_limit,
        }
    ]

    store.enqueue_error = WorkflowAdmissionRejected("queue full")
    with pytest.raises(WorkflowAdmissionRejected, match="queue"):
        supervisor.submit(
            "run-2",
            _work_spec(deadline_at_ms=20_000),
            deadline_at_ms=20_000,
        )


def test_scheduler_passes_caps_and_spawns_with_run_id_and_persisted_spec(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    context = FakeContext()
    clock = FakeClock()
    supervisor = _supervisor(store, config, clock=clock, context=context)

    assert supervisor._schedule_once() == 1

    assert store.expire_calls[0]["now_ms"] == clock.value_ms
    assert str(store.expire_calls[0]["reaper_supervisor_id"]).startswith(
        "supervisor-test:reaper:"
    )
    assert store.expire_calls[0]["reaper_lease_expires_at_ms"] == (
        clock.value_ms + config.lease_seconds * 1_000
    )
    assert store.claim_calls[0] == {
        "supervisor_id": "supervisor-test",
        "global_limit": config.global_limit,
        "per_tenant_limit": config.per_tenant_limit,
        "per_owner_limit": config.per_owner_limit,
        "lease_expires_at_ms": clock.value_ms + config.lease_seconds * 1_000,
    }
    process = context.processes[0]
    assert process.target is _fake_process_entrypoint
    assert process.args == (
        "run-1",
        _work_spec(),
        {"supervisor_id": "supervisor-test", "attempt_generation": 1_000},
    )
    assert store.set_worker_pid_calls == [
        {
            "run_id": "run-1",
            "worker_pid": process.pid,
            "expected_supervisor_id": "supervisor-test",
            "expected_attempt_generation": 1_000,
            "worker_pid_started_at_ms": None,
        }
    ]
    snapshot = supervisor.get_process_snapshot("run-1")
    assert snapshot is not None
    assert snapshot.pid == process.pid
    assert snapshot.state == "running"


def test_pid_registration_rejection_reaps_process_without_handle(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    store.set_worker_pid_results.append(False)
    context = FakeContext()
    signals: list[int] = []
    supervisor = _supervisor(
        store,
        config,
        context=context,
        group_signals=signals,
    )

    assert supervisor._schedule_once() == 0

    process = context.processes[0]
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.join_calls
    assert supervisor.get_process_snapshot("run-1") is None
    assert store.finish_calls == [
        {
            "run_id": "run-1",
            "expected_supervisor_id": "supervisor-test",
            "expected_attempt_generation": 1_000,
            "terminal_outcome": {
                "status": "failed",
                "reason": "WORKER_PID_REGISTRATION_FAILED",
            },
        }
    ]


def test_invalid_started_pid_reaps_process_without_store_registration(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    context = InvalidPidContext()
    supervisor = _supervisor(store, config, context=context)

    assert supervisor._schedule_once() == 0

    assert store.set_worker_pid_calls == []
    assert context.processes[0].join_calls
    assert supervisor.get_process_snapshot("run-1") is None
    assert store.finish_calls[0]["terminal_outcome"] == {
        "status": "failed",
        "reason": "WORKER_PID_REGISTRATION_FAILED",
    }


def test_expired_claim_finishes_without_process_creation(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim(deadline_at_ms=1_000))
    context = FakeContext()
    supervisor = _supervisor(store, config, clock=FakeClock(1_000), context=context)

    assert supervisor._schedule_once() == 0
    assert context.processes == []
    assert store.finish_calls[0]["terminal_outcome"] == {
        "status": "timed_out",
        "reason": "QUEUE_DEADLINE_EXCEEDED",
    }


def test_expiry_decisions_reap_before_terminalize_or_requeue(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.expiry_decisions.extend(
        [
            FakeExpiryDecision(
                run_id="run-expired",
                run_kind="agui",
                action="terminalize",
                terminal_status="timed_out",
                terminal_reason="DEADLINE_EXCEEDED",
                worker_pid=111,
                supervisor_id="supervisor-reaper-a",
                attempt_generation=101,
                worker_lease_expires_at_ms=900,
                worker_pid_started_at_ms=111_000,
            ),
            FakeExpiryDecision(
                run_id="run-recover",
                run_kind="agui",
                action="reap_then_requeue",
                terminal_status=None,
                terminal_reason=None,
                worker_pid=222,
                supervisor_id="supervisor-reaper-b",
                attempt_generation=102,
                worker_lease_expires_at_ms=800,
                worker_pid_started_at_ms=222_000,
            ),
            FakeExpiryDecision(
                run_id="run-queued",
                run_kind="text_to_sql",
                action="terminalize",
                terminal_status="cancelled",
                terminal_reason="CANCEL_REQUESTED",
                worker_pid=None,
                supervisor_id=None,
                attempt_generation=None,
                worker_lease_expires_at_ms=None,
                worker_pid_started_at_ms=None,
            ),
        ]
    )
    stale_reaps: list[tuple[int, int | None]] = []
    terminal_calls: list[
        tuple[str, str | None, int | None, dict[str, object]]
    ] = []

    def terminalize(
        run_id: str,
        supervisor_id: str | None,
        attempt_generation: int | None,
        outcome: Mapping[str, object],
    ) -> bool:
        terminal_calls.append(
            (run_id, supervisor_id, attempt_generation, dict(outcome))
        )
        if supervisor_id is None or attempt_generation is None:
            return True
        return store.finish_worker(
            run_id,
            supervisor_id,
            attempt_generation,
            outcome,
        )

    clock = FakeClock(1_000)
    supervisor = _supervisor(
        store,
        config,
        clock=clock,
        stale_reaps=stale_reaps,
        terminalizer=terminalize,
    )

    supervisor._schedule_once()

    assert stale_reaps == [(111, 111_000), (222, 222_000)]
    assert terminal_calls == [
        (
            "run-expired",
            "supervisor-reaper-a",
            101,
            {"status": "timed_out", "reason": "DEADLINE_EXCEEDED"},
        ),
        (
            "run-queued",
            None,
            None,
            {"status": "cancelled", "reason": "CANCEL_REQUESTED"},
        ),
    ]
    assert store.finish_calls == [
        {
            "run_id": "run-expired",
            "expected_supervisor_id": "supervisor-reaper-a",
            "expected_attempt_generation": 101,
            "terminal_outcome": {
                "status": "timed_out",
                "reason": "DEADLINE_EXCEEDED",
            },
        }
    ]
    assert store.requeue_calls == [
        {
            "run_id": "run-recover",
            "expected_supervisor_id": "supervisor-reaper-b",
            "expected_attempt_generation": 102,
            "expected_worker_pid": 222,
            "now_ms": clock.value_ms,
        }
    ]


def test_stale_reaper_signals_sigterm_when_identity_matches_within_tolerance(
    config: SupervisorConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workflow.process_supervisor as process_supervisor_module

    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_supervisor_module.os, "getpgid", lambda _pid: 555)
    monkeypatch.setattr(process_supervisor_module.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(
        process_supervisor_module.os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr(
        process_supervisor_module.os,
        "kill",
        lambda _pid, _sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    supervisor = WorkflowProcessSupervisor(
        FakeStore(),
        process_entrypoint=_fake_process_entrypoint,
        config=config,
        supervisor_id="supervisor-test",
        process_identity_probe=lambda _pid: 555_002,
    )

    # expected=555_000, probe=555_002: |diff|==2, exactly at the documented
    # +/-2ms tolerance boundary, so it must still be treated as a match.
    supervisor._default_stale_process_reaper(4321, 555_000, 5.0)

    assert killpg_calls == [(555, signal.SIGTERM)]


@pytest.mark.parametrize(
    ("worker_pid_started_at_ms", "probe_return"),
    [
        pytest.param(None, None, id="null_identity"),
        pytest.param(555_000, 999_000, id="mismatched_identity"),
    ],
)
def test_stale_reaper_skips_signal_on_identity_mismatch_but_store_action_still_proceeds(
    config: SupervisorConfig,
    monkeypatch: pytest.MonkeyPatch,
    worker_pid_started_at_ms: int | None,
    probe_return: int | None,
) -> None:
    import workflow.process_supervisor as process_supervisor_module

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not signal on identity mismatch/None")

    monkeypatch.setattr(process_supervisor_module.os, "getpgid", _forbidden)
    monkeypatch.setattr(process_supervisor_module.os, "killpg", _forbidden)
    monkeypatch.setattr(process_supervisor_module.os, "kill", _forbidden)

    def _probe(_pid: int) -> int | None:
        if worker_pid_started_at_ms is None:
            raise AssertionError("probe must not be consulted for null identity")
        return probe_return

    store = FakeStore()
    supervisor = WorkflowProcessSupervisor(
        store,
        process_entrypoint=_fake_process_entrypoint,
        config=config,
        supervisor_id="supervisor-test",
        process_identity_probe=_probe,
    )
    expiry_result = FakeExpiryResult(
        changed_count=1,
        terminal_decisions=(
            FakeExpiryDecision(
                run_id="run-mismatch",
                run_kind="agui",
                action="reap_then_requeue",
                terminal_status=None,
                terminal_reason=None,
                worker_pid=4321,
                supervisor_id="supervisor-reaper",
                attempt_generation=7,
                worker_lease_expires_at_ms=9_000,
                worker_pid_started_at_ms=worker_pid_started_at_ms,
            ),
        ),
    )

    supervisor._process_expiry_decisions(expiry_result, 1_000)

    assert store.requeue_calls == [
        {
            "run_id": "run-mismatch",
            "expected_supervisor_id": "supervisor-reaper",
            "expected_attempt_generation": 7,
            "expected_worker_pid": 4321,
            "now_ms": 1_000,
        }
    ]


@pytest.mark.parametrize(
    ("exitcode", "status", "reason"),
    [(0, "succeeded", "WORKER_EXITED"), (7, "failed", "WORKER_CRASHED")],
)
def test_watcher_reaps_and_releases_capacity_once(
    config: SupervisorConfig,
    exitcode: int,
    status: str,
    reason: str,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    context = FakeContext()
    supervisor = _supervisor(store, config, context=context)
    supervisor._schedule_once()
    process = context.processes[0]
    process.alive = False
    process.exitcode = exitcode

    supervisor._watch_process("run-1")

    assert process.join_calls
    assert store.finish_calls == [
        {
            "run_id": "run-1",
            "expected_supervisor_id": "supervisor-test",
            "expected_attempt_generation": 1_000,
            "terminal_outcome": {"status": status, "reason": reason},
        }
    ]
    assert supervisor.get_process_snapshot("run-1") is None


def test_deadline_sends_term_then_kill_and_finishes_timed_out(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim(deadline_at_ms=1_500))
    context = FakeContext()
    clock = FakeClock(1_000)
    signals: list[int] = []
    supervisor = _supervisor(
        store,
        config,
        clock=clock,
        context=context,
        group_signals=signals,
    )
    supervisor._schedule_once()
    clock.value_ms = 1_500

    supervisor._watch_process("run-1")

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert context.processes[0].join_calls[0] == config.cancel_grace_seconds
    assert store.finish_calls[0]["terminal_outcome"] == {
        "status": "timed_out",
        "reason": "WORKFLOW_DEADLINE_EXCEEDED",
    }


def test_deadline_kill_abandons_join_and_still_releases_handle_when_process_survives_sigkill(
    config: SupervisorConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore()
    store.claims.append(_claim(deadline_at_ms=1_500))
    context = UnkillableContext()
    clock = FakeClock(1_000)
    signals: list[int] = []
    supervisor = _supervisor(
        store,
        config,
        clock=clock,
        context=context,
        group_signals=signals,
    )
    supervisor._schedule_once()
    clock.value_ms = 1_500

    with caplog.at_level(logging.ERROR):
        supervisor._watch_process("run-1")

    process = context.processes[0]
    assert signal.SIGTERM in signals
    assert signal.SIGKILL in signals
    # Every join call must be bounded: a process stuck in an uninterruptible
    # syscall after SIGKILL must never be awaited forever.
    assert None not in process.join_calls
    assert "did not exit after SIGKILL" in caplog.text
    # The watcher must still reach _release_handle so the run_id doesn't stay
    # stuck on this supervisor forever.
    assert supervisor.get_process_snapshot("run-1") is None
    assert store.finish_calls[0]["terminal_outcome"] == {
        "status": "timed_out",
        "reason": "WORKFLOW_DEADLINE_EXCEEDED",
    }


def test_lost_lease_terminates_worker_and_releases_handle(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    store.renew_results.append(False)
    context = FakeContext()
    signals: list[int] = []
    supervisor = _supervisor(store, config, context=context, group_signals=signals)
    supervisor._schedule_once()

    supervisor._watch_process("run-1")

    assert signals[0] == signal.SIGTERM
    assert store.finish_calls[0]["terminal_outcome"]["reason"] == "WORKER_LEASE_LOST"
    assert supervisor.get_process_snapshot("run-1") is None


def test_failed_heartbeat_observes_durable_cancel(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    store.renew_results.append(False)
    store.run_states["run-1"] = FakeRunState(
        run_id="run-1",
        cancel_requested_at_ms=1_001,
    )
    context = FakeContext()
    supervisor = _supervisor(store, config, context=context)
    supervisor._schedule_once()

    supervisor._watch_process("run-1")

    assert store.finish_calls[0]["terminal_outcome"] == {
        "status": "cancelled",
        "reason": "CANCEL_REQUESTED",
    }


def test_failed_heartbeat_does_not_overwrite_new_owner_or_terminal_winner(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    store.renew_results.append(False)
    store.run_states["run-1"] = FakeRunState(
        run_id="run-1",
        supervisor_id="supervisor-new-owner",
    )
    context = FakeContext()
    signals: list[int] = []
    supervisor = _supervisor(store, config, context=context, group_signals=signals)
    supervisor._schedule_once()

    supervisor._watch_process("run-1")

    assert store.finish_calls == []
    # We lost the lease race to a new owner, so should_terminalize stays
    # False (no store write) -- but our own local child is still alive and
    # must be killed regardless, so it cannot outlive the fence loss.
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert supervisor.get_process_snapshot("run-1") is None


def test_deadline_fence_loss_still_kills_local_orphan_but_leaves_store_untouched(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim(deadline_at_ms=1_500))
    store.fence_results.append(None)
    context = FakeContext()
    clock = FakeClock(1_000)
    signals: list[int] = []
    supervisor = _supervisor(
        store,
        config,
        clock=clock,
        context=context,
        group_signals=signals,
    )
    supervisor._schedule_once()
    clock.value_ms = 1_500

    supervisor._watch_process("run-1")

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert store.finish_calls == []
    assert supervisor.get_process_snapshot("run-1") is None


def test_cancel_lease_failure_fence_loss_still_kills_local_orphan_but_leaves_store_untouched(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    store.renew_results.append(False)
    store.run_states["run-1"] = FakeRunState(
        run_id="run-1",
        cancel_requested_at_ms=1_001,
    )
    store.fence_results.append(None)
    context = FakeContext()
    signals: list[int] = []
    supervisor = _supervisor(store, config, context=context, group_signals=signals)
    supervisor._schedule_once()

    supervisor._watch_process("run-1")

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert store.finish_calls == []
    assert supervisor.get_process_snapshot("run-1") is None


def test_queued_cancel_never_spawns(config: SupervisorConfig) -> None:
    store = FakeStore()
    store.cancel_results["run-queued"] = "cancelled"
    context = FakeContext()
    supervisor = _supervisor(store, config, context=context)

    result = supervisor.cancel("run-queued")

    assert (result.accepted, result.state, result.local) == (True, "cancelled", False)
    assert context.processes == []
    assert store.finish_calls == []


def test_queued_cancel_terminalizer_receives_no_supervisor_owner(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.cancel_results["run-queued"] = "queued"
    calls: list[tuple[str, str | None, int | None, dict[str, object]]] = []
    supervisor = _supervisor(
        store,
        config,
        terminalizer=lambda run_id, supervisor_id, generation, outcome: calls.append(
            (run_id, supervisor_id, generation, dict(outcome))
        )
        or True,
    )

    result = supervisor.cancel("run-queued")

    assert (result.accepted, result.state, result.local) == (
        True,
        "cancelled",
        False,
    )
    assert calls == [
        (
            "run-queued",
            None,
            None,
            {"status": "cancelled", "reason": "CANCEL_REQUESTED"},
        )
    ]


def test_running_cancel_terminates_local_process_and_finishes_once(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    context = FakeContext()
    signals: list[int] = []
    supervisor = _supervisor(store, config, context=context, group_signals=signals)
    supervisor._schedule_once()

    result = supervisor.cancel("run-1")

    assert (result.accepted, result.local) == (True, True)
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert store.finish_calls[0]["terminal_outcome"] == {
        "status": "cancelled",
        "reason": "CANCEL_REQUESTED",
    }


def test_remote_running_cancel_is_durably_accepted(config: SupervisorConfig) -> None:
    store = FakeStore()
    store.cancel_results["run-remote"] = "running"
    supervisor = _supervisor(store, config)

    result = supervisor.cancel("run-remote")

    assert (result.accepted, result.state, result.local) == (True, "running", False)
    assert store.finish_calls == []


def test_spawn_failure_uses_terminalizer_and_releases_handle(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    context = FakeContext()
    context.start_error = RuntimeError("spawn failed")
    supervisor = _supervisor(store, config, context=context)

    assert supervisor._schedule_once() == 0
    assert store.finish_calls[0]["terminal_outcome"] == {
        "status": "failed",
        "reason": "WORKER_SPAWN_FAILED",
    }
    assert supervisor.get_process_snapshot("run-1") is None


def test_claimed_work_spec_is_revalidated_before_spawn(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    invalid = _work_spec()
    invalid["parameters"] = {"bad": object()}
    store.claims.append(_claim(work_spec=invalid))
    context = FakeContext()
    supervisor = _supervisor(store, config, context=context)

    assert supervisor._schedule_once() == 0
    assert context.processes == []
    assert store.finish_calls[0]["terminal_outcome"]["reason"] == "INVALID_WORK_SPEC"


def test_injected_terminalizer_replaces_generic_finish_worker(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    context = FakeContext()
    calls: list[tuple[str, str, int, dict[str, object]]] = []
    supervisor = _supervisor(
        store,
        config,
        context=context,
        terminalizer=lambda run_id, supervisor_id, generation, outcome: calls.append(
            (run_id, supervisor_id, generation, dict(outcome))
        ) or True,
    )
    supervisor._schedule_once()
    context.processes[0].alive = False
    context.processes[0].exitcode = 0

    supervisor._watch_process("run-1")

    assert calls == [
        (
            "run-1",
            "supervisor-test",
            1_000,
            {"status": "succeeded", "reason": "WORKER_EXITED"},
        )
    ]
    assert store.finish_calls == []


def test_spawn_receives_separate_claim_envelope_and_uses_exact_pid_cas(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim(attempt_generation=41))
    context = FakeContext()
    supervisor = _supervisor(store, config, context=context)

    assert supervisor._schedule_once() == 1

    assert context.processes[0].args[2] == {
        "supervisor_id": "supervisor-test",
        "attempt_generation": 41,
    }
    assert "supervisor_id" not in context.processes[0].args[1]
    assert store.set_worker_pid_calls == [
        {
            "run_id": "run-1",
            "worker_pid": context.processes[0].pid,
            "expected_supervisor_id": "supervisor-test",
            "expected_attempt_generation": 41,
            "worker_pid_started_at_ms": None,
        }
    ]


def test_cancel_rotates_generation_before_signalling(
    config: SupervisorConfig,
) -> None:
    order: list[str] = []

    class OrderedStore(FakeStore):
        def fence_worker_attempt(self, *args, **kwargs):
            order.append("fence")
            return super().fence_worker_attempt(*args, **kwargs)

    store = OrderedStore()
    store.claims.append(_claim(attempt_generation=73))
    context = FakeContext()
    supervisor = WorkflowProcessSupervisor(
        store,
        process_entrypoint=_fake_process_entrypoint,
        config=config,
        supervisor_id="supervisor-test",
        process_context=context,
        now_ms=FakeClock().now_ms,
        wait=lambda _seconds: False,
        thread_factory=ManualThread,
        process_group_signal=lambda _process, _sig: order.append("signal"),
    )
    assert supervisor.start()
    supervisor._schedule_once()

    supervisor.cancel("run-1")

    assert order[0] == "fence"
    assert store.finish_calls[0]["expected_attempt_generation"] == 74


def test_cancel_returns_durable_terminal_before_touching_local_handle(
    config: SupervisorConfig,
) -> None:
    signals: list[int] = []
    store = FakeStore()
    store.claims.append(_claim())
    store.cancel_results["run-1"] = "succeeded"
    context = FakeContext()
    supervisor = WorkflowProcessSupervisor(
        store,
        process_entrypoint=_fake_process_entrypoint,
        config=config,
        supervisor_id="supervisor-test",
        process_context=context,
        now_ms=FakeClock().now_ms,
        wait=lambda _seconds: False,
        thread_factory=ManualThread,
        process_group_signal=lambda _process, sig: signals.append(sig),
    )
    assert supervisor.start()
    supervisor._schedule_once()

    result = supervisor.cancel("run-1")

    assert result.state == "succeeded"
    assert result.accepted is False
    assert result.local is False
    assert store.fence_calls == []
    assert store.finish_calls == []
    assert signals == []


@pytest.mark.parametrize("winner", ["succeeded", "result_pending"])
def test_cancel_rereads_durable_winner_when_local_fence_loses(
    config: SupervisorConfig,
    winner: str,
) -> None:
    signals: list[int] = []
    store = FakeStore()
    store.claims.append(_claim())
    store.fence_results.append(None)
    store.run_states["run-1"] = FakeRunState(
        run_id="run-1",
        status=winner,
    )
    context = FakeContext()
    supervisor = WorkflowProcessSupervisor(
        store,
        process_entrypoint=_fake_process_entrypoint,
        config=config,
        supervisor_id="supervisor-test",
        process_context=context,
        now_ms=FakeClock().now_ms,
        wait=lambda _seconds: False,
        thread_factory=ManualThread,
        process_group_signal=lambda _process, sig: signals.append(sig),
    )
    assert supervisor.start()
    supervisor._schedule_once()

    result = supervisor.cancel("run-1")

    assert result.state == winner
    assert result.accepted is False
    assert result.local is False
    assert len(store.fence_calls) == 1
    assert store.finish_calls == []
    assert signals == []


def test_invalid_claim_terminalizes_exact_generation_and_continues(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.extend(
        [
            _claim("run-invalid", attempt_generation=9, invalid_work_spec=True),
            _claim("run-valid", attempt_generation=10),
        ]
    )
    context = FakeContext()
    supervisor = _supervisor(store, config, context=context)

    assert supervisor._schedule_once() == 1
    assert store.finish_calls[0] == {
        "run_id": "run-invalid",
        "expected_supervisor_id": "supervisor-test",
        "expected_attempt_generation": 9,
        "terminal_outcome": {
            "status": "failed",
            "reason": "INVALID_WORK_SPEC",
        },
    }
    assert len(context.processes) == 1
    assert context.processes[0].args[0] == "run-valid"


def test_terminalizer_false_releases_result_pending_claim(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    context = FakeContext()

    def terminalize(run_id, supervisor_id, generation, _outcome):
        store.run_states[run_id] = FakeRunState(
            run_id=run_id,
            status="result_pending",
            supervisor_id=supervisor_id,
            attempt_started_at_ms=generation,
            worker_lease_expires_at_ms=None,
        )
        return False

    supervisor = _supervisor(
        store,
        config,
        context=context,
        terminalizer=terminalize,
    )
    supervisor._schedule_once()
    context.processes[0].alive = False
    context.processes[0].exitcode = 0

    supervisor._watch_process("run-1")

    assert supervisor.get_process_snapshot("run-1") is None
    assert store.renew_calls == []


def test_terminalizer_false_retains_lease_and_retries_without_respawn(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    store.claims.append(_claim())
    context = FakeContext()
    results = iter((False, True))

    def terminalize(_run_id, _supervisor_id, _generation, _outcome):
        return next(results)

    supervisor = _supervisor(
        store,
        config,
        context=context,
        terminalizer=terminalize,
    )
    supervisor._schedule_once()
    context.processes[0].alive = False
    context.processes[0].exitcode = 0

    supervisor._watch_process("run-1")

    snapshot = supervisor.get_process_snapshot("run-1")
    assert snapshot is not None
    assert snapshot.state == "finalizing"
    assert store.renew_calls[-1]["attempt_generation"] == 1_000

    supervisor._schedule_once()

    assert supervisor.get_process_snapshot("run-1") is None
    assert len(context.processes) == 1


def test_start_stop_are_idempotent_and_supervisor_id_is_stable(
    config: SupervisorConfig,
) -> None:
    ManualThread.instances.clear()
    supervisor = _supervisor(FakeStore(), config, started=False)

    assert supervisor.supervisor_id == "supervisor-test"
    assert supervisor.start() is True
    assert supervisor.start() is False
    assert supervisor.supervisor_id == "supervisor-test"
    assert ManualThread.instances[-1].started is True
    assert supervisor.stop() is True
    assert supervisor.stop() is False


def test_stopped_supervisor_does_not_claim_or_spawn(
    config: SupervisorConfig,
) -> None:
    store = FakeStore()
    context = FakeContext()
    supervisor = _supervisor(store, config, context=context, started=False)
    assert supervisor.start() is True
    assert supervisor.stop() is True
    store.claims.append(_claim())

    assert supervisor._schedule_once() == 0

    assert store.claim_calls == []
    assert context.processes == []


def test_default_context_is_spawn(monkeypatch, config: SupervisorConfig) -> None:
    calls: list[str] = []
    context = FakeContext()
    monkeypatch.setattr(
        multiprocessing,
        "get_context",
        lambda method: calls.append(method) or context,
    )

    WorkflowProcessSupervisor(
        FakeStore(),
        process_entrypoint=_fake_process_entrypoint,
        config=config,
    )

    assert calls == ["spawn"]


def test_real_spawn_finishes_while_parent_lock_is_held(config: SupervisorConfig) -> None:
    now_ms = int(time.time() * 1_000)
    deadline_at_ms = now_ms + 10_000
    store = FakeStore()
    store.claims.append(
        _claim(
            deadline_at_ms=deadline_at_ms,
            work_spec=_work_spec(deadline_at_ms=deadline_at_ms, delay=0.01),
            supervisor_id="supervisor-real-spawn",
        )
    )
    parent_lock = threading.Lock()
    parent_lock.acquire()
    supervisor = WorkflowProcessSupervisor(
        store,
        process_entrypoint=_real_spawn_entrypoint,
        config=config,
        supervisor_id="supervisor-real-spawn",
        process_context=multiprocessing.get_context("spawn"),
        poll_interval_seconds=0.01,
    )

    try:
        assert supervisor.start() is True
        assert store.finished.wait(timeout=5)
    finally:
        parent_lock.release()
        supervisor.stop()

    assert store.finish_calls[0]["terminal_outcome"]["status"] == "succeeded"
    assert supervisor.get_process_snapshot("run-1") is None
