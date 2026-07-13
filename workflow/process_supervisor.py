"""Bounded durable admission and spawn-safe workflow process supervision."""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backend.fastapi_app.agui.store import WorkflowAdmissionRejected


logger = logging.getLogger(__name__)

_WORK_SPEC_KEYS = frozenset(
    {
        "spec_version",
        "workflow_path",
        "parameters",
        "session_id",
        "client_id",
        "use_enhanced",
        "enable_telemetry",
        "run_incarnation",
        "deadline_at_ms",
    }
)
_MAX_WORK_SPEC_BYTES = 256 * 1024
_TERMINAL_STATES = frozenset(
    {"abstained", "cancelled", "failed", "succeeded", "timed_out"}
)
# Conservative public defaults preserve startup compatibility when the new
# environment variables are absent.
DEFAULT_WORKFLOW_PROCESS_GLOBAL_LIMIT = 4
DEFAULT_WORKFLOW_PROCESS_PER_TENANT_LIMIT = 2
DEFAULT_WORKFLOW_PROCESS_PER_OWNER_LIMIT = 1
DEFAULT_WORKFLOW_PROCESS_QUEUE_LIMIT = 100
DEFAULT_WORKFLOW_PROCESS_LEASE_SECONDS = 30
DEFAULT_WORKFLOW_PROCESS_CANCEL_GRACE_SECONDS = 5

_ENV_DEFAULTS = {
    "WORKFLOW_PROCESS_GLOBAL_LIMIT": DEFAULT_WORKFLOW_PROCESS_GLOBAL_LIMIT,
    "WORKFLOW_PROCESS_PER_TENANT_LIMIT": (
        DEFAULT_WORKFLOW_PROCESS_PER_TENANT_LIMIT
    ),
    "WORKFLOW_PROCESS_PER_OWNER_LIMIT": DEFAULT_WORKFLOW_PROCESS_PER_OWNER_LIMIT,
    "WORKFLOW_PROCESS_QUEUE_LIMIT": DEFAULT_WORKFLOW_PROCESS_QUEUE_LIMIT,
    "WORKFLOW_PROCESS_LEASE_SECONDS": DEFAULT_WORKFLOW_PROCESS_LEASE_SECONDS,
    "WORKFLOW_PROCESS_CANCEL_GRACE_SECONDS": (
        DEFAULT_WORKFLOW_PROCESS_CANCEL_GRACE_SECONDS
    ),
}


class WorkflowSupervisorConfigError(ValueError):
    """Supervisor environment configuration is invalid."""


class SupervisorStore(Protocol):
    def enqueue_run(
        self,
        run_id: str,
        work_spec: Mapping[str, object],
        deadline_at_ms: int,
        queue_limit: int,
    ) -> object: ...

    def claim_next_queued(
        self,
        supervisor_id: str,
        global_limit: int,
        per_tenant_limit: int,
        per_owner_limit: int,
        lease_expires_at_ms: int,
    ) -> object | None: ...

    def renew_worker_lease(
        self,
        run_id: str,
        supervisor_id: str,
        attempt_generation: int,
        lease_expires_at_ms: int,
    ) -> bool: ...

    def set_worker_pid(
        self,
        run_id: str,
        worker_pid: int,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        worker_pid_started_at_ms: int | None = None,
    ) -> bool: ...

    def request_cancel(self, run_id: str) -> str: ...

    def get_run(self, run_id: str) -> object | None: ...

    def finish_worker(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        terminal_outcome: Mapping[str, object],
    ) -> bool: ...

    def expire_queued_and_stale_leases(
        self,
        now_ms: int,
        reaper_supervisor_id: str,
        reaper_lease_expires_at_ms: int,
    ) -> object: ...

    def requeue_stale_worker(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        expected_worker_pid: int | None,
        now_ms: int,
    ) -> bool: ...

    def fence_worker_attempt(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        lease_expires_at_ms: int,
    ) -> int | None: ...


ProcessEntrypoint = Callable[
    [str, Mapping[str, object], Mapping[str, object]],
    None,
]
Terminalizer = Callable[
    [str, str | None, int | None, Mapping[str, object]],
    bool,
]
ThreadFactory = Callable[..., object]
ProcessGroupSignal = Callable[[object, int], None]
ProcessIdentityProbe = Callable[[int], int | None]
StaleProcessReaper = Callable[[int, int | None, float], None]


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    global_limit: int = _ENV_DEFAULTS["WORKFLOW_PROCESS_GLOBAL_LIMIT"]
    per_tenant_limit: int = _ENV_DEFAULTS["WORKFLOW_PROCESS_PER_TENANT_LIMIT"]
    per_owner_limit: int = _ENV_DEFAULTS["WORKFLOW_PROCESS_PER_OWNER_LIMIT"]
    queue_limit: int = _ENV_DEFAULTS["WORKFLOW_PROCESS_QUEUE_LIMIT"]
    lease_seconds: int = _ENV_DEFAULTS["WORKFLOW_PROCESS_LEASE_SECONDS"]
    cancel_grace_seconds: int = _ENV_DEFAULTS[
        "WORKFLOW_PROCESS_CANCEL_GRACE_SECONDS"
    ]

    def __post_init__(self) -> None:
        for name, value in (
            ("global_limit", self.global_limit),
            ("per_tenant_limit", self.per_tenant_limit),
            ("per_owner_limit", self.per_owner_limit),
            ("queue_limit", self.queue_limit),
            ("lease_seconds", self.lease_seconds),
            ("cancel_grace_seconds", self.cancel_grace_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise WorkflowSupervisorConfigError(
                    f"{name} must be a positive integer"
                )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> SupervisorConfig:
        values = os.environ if environ is None else environ

        def positive(name: str) -> int:
            raw = values.get(name, str(_ENV_DEFAULTS[name]))
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise WorkflowSupervisorConfigError(
                    f"{name} must be a positive integer"
                ) from exc
            if value <= 0 or str(value) != str(raw).strip():
                raise WorkflowSupervisorConfigError(
                    f"{name} must be a positive integer"
                )
            return value

        return cls(
            global_limit=positive("WORKFLOW_PROCESS_GLOBAL_LIMIT"),
            per_tenant_limit=positive("WORKFLOW_PROCESS_PER_TENANT_LIMIT"),
            per_owner_limit=positive("WORKFLOW_PROCESS_PER_OWNER_LIMIT"),
            queue_limit=positive("WORKFLOW_PROCESS_QUEUE_LIMIT"),
            lease_seconds=positive("WORKFLOW_PROCESS_LEASE_SECONDS"),
            cancel_grace_seconds=positive(
                "WORKFLOW_PROCESS_CANCEL_GRACE_SECONDS"
            ),
        )


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    run_id: str
    state: str
    deadline_at_ms: int


@dataclass(frozen=True, slots=True)
class CancellationResult:
    run_id: str
    state: str
    accepted: bool
    local: bool


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    run_id: str
    pid: int | None
    state: str
    deadline_at_ms: int
    started_at_ms: int
    exitcode: int | None
    alive: bool


@dataclass(slots=True)
class _ProcessHandle:
    run_id: str
    process: object
    deadline_at_ms: int
    started_at_ms: int
    supervisor_id: str
    attempt_generation: int
    state: str = "running"
    requested_outcome: dict[str, object] | None = None
    finalized: bool = False
    termination_fenced: bool = False
    watcher: object | None = None


@dataclass(frozen=True, slots=True)
class _Claim:
    run_id: str
    deadline_at_ms: int
    supervisor_id: str
    attempt_generation: int
    work_spec: dict[str, object] | None
    invalid_work_spec: bool


def _required_canonical_text(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or not all(character.isprintable() for character in value)
    ):
        raise ValueError(f"work_spec {field_name} must be canonical text")
    return value


def validate_work_spec(work_spec: object) -> dict[str, object]:
    """Validate and detach the primitive work specification used by spawn."""
    if not isinstance(work_spec, Mapping) or set(work_spec) != _WORK_SPEC_KEYS:
        raise ValueError("work_spec must contain the exact persisted fields")
    if work_spec.get("spec_version") != 1:
        raise ValueError("work_spec spec_version must be 1")

    workflow_path = _required_canonical_text(
        work_spec.get("workflow_path"),
        "workflow_path",
    )
    path = Path(workflow_path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("work_spec workflow_path must be canonical and absolute")

    _required_canonical_text(work_spec.get("session_id"), "session_id")
    _required_canonical_text(
        work_spec.get("run_incarnation"),
        "run_incarnation",
    )
    client_id = work_spec.get("client_id")
    if client_id is not None:
        _required_canonical_text(client_id, "client_id")
    for field_name in ("use_enhanced", "enable_telemetry"):
        if type(work_spec.get(field_name)) is not bool:
            raise ValueError(f"work_spec {field_name} must be a boolean")
    deadline_at_ms = work_spec.get("deadline_at_ms")
    if (
        isinstance(deadline_at_ms, bool)
        or not isinstance(deadline_at_ms, int)
        or deadline_at_ms <= 0
    ):
        raise ValueError("work_spec deadline_at_ms must be a positive integer")
    if not isinstance(work_spec.get("parameters"), Mapping):
        raise ValueError("work_spec parameters must be an object")

    try:
        encoded = json.dumps(
            dict(work_spec),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("work_spec must contain only JSON primitives") from exc
    if len(encoded) > _MAX_WORK_SPEC_BYTES:
        raise ValueError("work_spec exceeds the supervisor byte limit")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError("work_spec must serialize to an object")
    return decoded


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _to_mapping(value: object) -> object:
    converter = getattr(value, "to_mapping", None)
    return converter() if callable(converter) else value


def _terminal_outcome(status: str, reason: str | None = None) -> dict[str, object]:
    if status not in _TERMINAL_STATES:
        raise ValueError(f"unsupported terminal status: {status}")
    outcome: dict[str, object] = {"status": status}
    if reason:
        outcome["reason"] = reason
    return outcome


class WorkflowProcessSupervisor:
    """Own durable claims and all local workflow process handles."""

    def __init__(
        self,
        store: SupervisorStore,
        *,
        process_entrypoint: ProcessEntrypoint,
        config: SupervisorConfig | None = None,
        supervisor_id: str | None = None,
        process_context: object | None = None,
        now_ms: Callable[[], int] | None = None,
        wait: Callable[[float], bool] | None = None,
        thread_factory: ThreadFactory = threading.Thread,
        process_group_signal: ProcessGroupSignal | None = None,
        process_identity_probe: ProcessIdentityProbe | None = None,
        stale_process_reaper: StaleProcessReaper | None = None,
        terminalizer: Terminalizer | None = None,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        if not callable(process_entrypoint) or "<locals>" in str(
            getattr(process_entrypoint, "__qualname__", "")
        ):
            raise ValueError("process_entrypoint must be a module-level callable")
        if poll_interval_seconds <= 0 or not math.isfinite(poll_interval_seconds):
            raise ValueError("poll_interval_seconds must be positive")
        identifier = supervisor_id or f"supervisor-{uuid.uuid4().hex}"
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier != identifier.strip()
        ):
            raise ValueError("supervisor_id must be canonical text")

        self._store = store
        self._process_entrypoint = process_entrypoint
        self._config = config or SupervisorConfig.from_environment()
        self._supervisor_id = identifier
        self._process_context = process_context or multiprocessing.get_context("spawn")
        self._now_ms = now_ms or (lambda: int(time.time() * 1_000))
        self._thread_factory = thread_factory
        self._process_group_signal = process_group_signal or self._default_process_signal
        self._process_identity_probe = (
            process_identity_probe or self._default_process_identity_probe
        )
        self._stale_process_reaper = (
            stale_process_reaper or self._default_stale_process_reaper
        )
        self._terminalizer = terminalizer or self._finish_worker
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._stop_event = threading.Event()
        self._wait = wait or self._stop_event.wait
        self._handles: dict[str, _ProcessHandle] = {}
        self._handles_lock = threading.RLock()
        self._schedule_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._scheduler_thread: object | None = None
        self._running = False

    @property
    def supervisor_id(self) -> str:
        return self._supervisor_id

    def is_accepting(self) -> bool:
        """Return whether this supervisor can currently accept scheduled work."""
        with self._lifecycle_lock:
            scheduler = self._scheduler_thread
            if (
                not self._running
                or self._stop_event.is_set()
                or scheduler is None
            ):
                return False
            is_alive = getattr(scheduler, "is_alive", None)
            if not callable(is_alive):
                return False
            try:
                return bool(is_alive())
            except Exception:
                return False

    def submit(
        self,
        run_id: str,
        work_spec: Mapping[str, object],
        *,
        deadline_at_ms: int,
    ) -> SubmissionResult:
        run_identifier = self._validate_run_id(run_id)
        spec = validate_work_spec(work_spec)
        if spec["deadline_at_ms"] != deadline_at_ms:
            raise ValueError("work_spec deadline does not match admission deadline")
        self._store.enqueue_run(
            run_identifier,
            spec,
            deadline_at_ms,
            self._config.queue_limit,
        )
        if self._running:
            self._schedule_once()
        return SubmissionResult(run_identifier, "queued", deadline_at_ms)

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._running:
                return False
            self._running = True
            self._stop_event.clear()
            thread = self._thread_factory(
                target=self._scheduler_loop,
                name=f"workflow-supervisor-{self._supervisor_id}",
                daemon=True,
            )
            self._scheduler_thread = thread
            thread.start()
            return True

    def stop(self) -> bool:
        with self._schedule_lock:
            with self._lifecycle_lock:
                if not self._running:
                    return False
                self._running = False
                self._stop_event.set()
                scheduler = self._scheduler_thread
                self._scheduler_thread = None
        if scheduler is not None:
            scheduler.join(timeout=max(1.0, self._poll_interval_seconds * 2))
        with self._handles_lock:
            run_ids = tuple(self._handles)
        for run_id in run_ids:
            try:
                self._store.request_cancel(run_id)
            except Exception:
                logger.exception("Failed to request cancellation during supervisor stop")
            self._cancel_local(run_id, reason="SUPERVISOR_STOPPED")
        return True

    def cancel(self, run_id: str) -> CancellationResult:
        run_identifier = self._validate_run_id(run_id)
        state = self._store.request_cancel(run_identifier)
        if not isinstance(state, str) or not state:
            raise RuntimeError("request_cancel returned an invalid state")
        normalized = state.lower()
        if normalized == "cancelled":
            return CancellationResult(run_identifier, normalized, True, False)
        if normalized in _TERMINAL_STATES or normalized == "result_pending":
            return CancellationResult(run_identifier, normalized, False, False)
        if normalized in {"missing", "not_found"}:
            return CancellationResult(run_identifier, normalized, False, False)
        with self._handles_lock:
            local = run_identifier in self._handles
        if local:
            if self._cancel_local(run_identifier, reason="CANCEL_REQUESTED"):
                return CancellationResult(run_identifier, "cancelled", True, True)
            winner = self._store.get_run(run_identifier)
            if winner is None:
                return CancellationResult(run_identifier, "missing", False, False)
            winner_status = str(_field(winner, "status", "")).lower()
            accepted = not (
                winner_status in _TERMINAL_STATES
                or winner_status == "result_pending"
            )
            return CancellationResult(
                run_identifier,
                winner_status,
                accepted,
                False,
            )
        if normalized == "queued":
            self._terminalize(
                run_identifier,
                _terminal_outcome("cancelled", "CANCEL_REQUESTED"),
                expected_supervisor_id=None,
                expected_attempt_generation=None,
            )
            return CancellationResult(run_identifier, "cancelled", True, False)
        return CancellationResult(run_identifier, normalized, True, False)

    def get_process_snapshot(self, run_id: str) -> ProcessSnapshot | None:
        with self._handles_lock:
            handle = self._handles.get(run_id)
            if handle is None:
                return None
            process = handle.process
            return ProcessSnapshot(
                run_id=run_id,
                pid=getattr(process, "pid", None),
                state=handle.state,
                deadline_at_ms=handle.deadline_at_ms,
                started_at_ms=handle.started_at_ms,
                exitcode=getattr(process, "exitcode", None),
                alive=self._is_alive(process),
            )

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._schedule_once()
            except Exception:
                logger.exception("Workflow supervisor scheduling pass failed")
            if self._wait(self._poll_interval_seconds):
                break

    def _schedule_once(self) -> int:
        with self._schedule_lock:
            if not self._running:
                return 0
            now_ms = self._now_ms()
            self._retry_pending_finalizations(now_ms)
            reaper_id = f"{self._supervisor_id}:reaper:{uuid.uuid4().hex}"
            expiry_result = self._store.expire_queued_and_stale_leases(
                now_ms,
                reaper_id,
                now_ms + self._config.lease_seconds * 1_000,
            )
            self._process_expiry_decisions(expiry_result, now_ms)
            started = 0
            for _ in range(self._config.global_limit):
                if not self._running:
                    break
                claim_value = self._store.claim_next_queued(
                    self._supervisor_id,
                    self._config.global_limit,
                    self._config.per_tenant_limit,
                    self._config.per_owner_limit,
                    now_ms + self._config.lease_seconds * 1_000,
                )
                if claim_value is None:
                    break
                run_id = self._claim_run_id(claim_value)
                try:
                    claim = self._normalize_claim(claim_value)
                except (TypeError, ValueError):
                    logger.exception("Invalid durable claim for %s", run_id)
                    claim_envelope = _field(claim_value, "claim")
                    claim_supervisor_id = _field(
                        claim_envelope,
                        "supervisor_id",
                    )
                    claim_generation = _field(
                        claim_envelope,
                        "attempt_generation",
                    )
                    if (
                        claim_supervisor_id == self._supervisor_id
                        and isinstance(claim_generation, int)
                        and not isinstance(claim_generation, bool)
                        and claim_generation > 0
                    ):
                        self._terminalize(
                            run_id,
                            _terminal_outcome("failed", "INVALID_WORK_SPEC"),
                            expected_supervisor_id=claim_supervisor_id,
                            expected_attempt_generation=claim_generation,
                        )
                    continue
                if claim.invalid_work_spec:
                    self._terminalize(
                        claim.run_id,
                        _terminal_outcome("failed", "INVALID_WORK_SPEC"),
                        expected_supervisor_id=claim.supervisor_id,
                        expected_attempt_generation=claim.attempt_generation,
                    )
                    continue
                if claim.deadline_at_ms <= now_ms:
                    self._terminalize(
                        claim.run_id,
                        _terminal_outcome(
                            "timed_out",
                            "QUEUE_DEADLINE_EXCEEDED",
                        ),
                        expected_supervisor_id=claim.supervisor_id,
                        expected_attempt_generation=claim.attempt_generation,
                    )
                    continue
                if self._spawn_claimed(claim):
                    started += 1
            return started

    def _process_expiry_decisions(self, expiry_result: object, now_ms: int) -> None:
        decision_values = _field(expiry_result, "terminal_decisions")
        if (
            isinstance(decision_values, (str, bytes, Mapping))
            or not isinstance(decision_values, Iterable)
        ):
            raise ValueError("expiry result terminal_decisions must be iterable")
        for decision in decision_values:
            action = _field(decision, "action")
            run_id = self._validate_run_id(_field(decision, "run_id"))
            worker_pid = _field(decision, "worker_pid")
            supervisor_id = _field(decision, "supervisor_id")
            attempt_generation = _field(decision, "attempt_generation")
            lease_expires_at_ms = _field(
                decision,
                "worker_lease_expires_at_ms",
            )
            worker_pid_started_at_ms = _field(
                decision,
                "worker_pid_started_at_ms",
            )
            if worker_pid is not None and (
                isinstance(worker_pid, bool)
                or not isinstance(worker_pid, int)
                or worker_pid <= 0
            ):
                raise ValueError("expiry decision worker_pid must be positive or null")
            if supervisor_id is not None and (
                not isinstance(supervisor_id, str)
                or not supervisor_id
                or supervisor_id != supervisor_id.strip()
            ):
                raise ValueError("expiry decision supervisor_id must be canonical")
            if attempt_generation is not None and (
                isinstance(attempt_generation, bool)
                or not isinstance(attempt_generation, int)
                or attempt_generation <= 0
            ):
                raise ValueError(
                    "expiry decision attempt_generation must be positive or null"
                )
            if lease_expires_at_ms is not None and (
                isinstance(lease_expires_at_ms, bool)
                or not isinstance(lease_expires_at_ms, int)
                or lease_expires_at_ms <= 0
            ):
                raise ValueError(
                    "expiry decision worker_lease_expires_at_ms must be positive or null"
                )
            if worker_pid_started_at_ms is not None and (
                isinstance(worker_pid_started_at_ms, bool)
                or not isinstance(worker_pid_started_at_ms, int)
                or worker_pid_started_at_ms <= 0
            ):
                raise ValueError(
                    "expiry decision worker_pid_started_at_ms must be positive or null"
                )
            if worker_pid is not None:
                self._stale_process_reaper(
                    worker_pid,
                    worker_pid_started_at_ms,
                    float(self._config.cancel_grace_seconds),
                )
            if action == "terminalize":
                status = _field(decision, "terminal_status")
                reason = _field(decision, "terminal_reason")
                if not isinstance(status, str):
                    raise ValueError("expiry decision terminal_status is required")
                if reason is not None and not isinstance(reason, str):
                    raise ValueError("expiry decision terminal_reason must be text")
                self._terminalize(
                    run_id,
                    _terminal_outcome(status, reason),
                    expected_supervisor_id=supervisor_id,
                    expected_attempt_generation=attempt_generation,
                )
            elif action == "reap_then_requeue":
                if supervisor_id is None or attempt_generation is None:
                    raise ValueError("reaper claim envelope is required")
                self._store.requeue_stale_worker(
                    run_id,
                    supervisor_id,
                    attempt_generation,
                    worker_pid,
                    now_ms,
                )
            else:
                raise ValueError(f"unsupported expiry action: {action}")

    def _normalize_claim(self, claim_value: object) -> _Claim:
        run = _field(claim_value, "run", claim_value)
        run_id = self._validate_run_id(_field(run, "run_id"))
        deadline_at_ms = _field(run, "deadline_at_ms")
        if (
            isinstance(deadline_at_ms, bool)
            or not isinstance(deadline_at_ms, int)
            or deadline_at_ms <= 0
        ):
            raise ValueError("claimed run deadline_at_ms must be positive")
        claim_envelope = _field(claim_value, "claim")
        supervisor_id = _field(claim_envelope, "supervisor_id")
        attempt_generation = _field(claim_envelope, "attempt_generation")
        if (
            not isinstance(supervisor_id, str)
            or not supervisor_id
            or supervisor_id != supervisor_id.strip()
            or supervisor_id != self._supervisor_id
        ):
            raise ValueError("claimed supervisor_id is invalid")
        if (
            isinstance(attempt_generation, bool)
            or not isinstance(attempt_generation, int)
            or attempt_generation <= 0
        ):
            raise ValueError("claimed attempt_generation must be positive")
        invalid_work_spec = _field(claim_value, "invalid_work_spec", False)
        if type(invalid_work_spec) is not bool:
            raise ValueError("invalid_work_spec must be boolean")
        spec = None
        if not invalid_work_spec:
            raw_spec = _field(claim_value, "work_spec")
            spec = validate_work_spec(_to_mapping(raw_spec))
            if spec["deadline_at_ms"] != deadline_at_ms:
                raise ValueError("claimed work_spec deadline does not match run")
        return _Claim(
            run_id,
            deadline_at_ms,
            supervisor_id,
            attempt_generation,
            spec,
            invalid_work_spec,
        )

    def _claim_run_id(self, claim_value: object) -> str:
        run = _field(claim_value, "run", claim_value)
        return self._validate_run_id(_field(run, "run_id"))

    def _spawn_claimed(self, claim: _Claim) -> bool:
        if claim.work_spec is None:
            raise ValueError("invalid work spec cannot be spawned")
        claim_envelope = {
            "supervisor_id": claim.supervisor_id,
            "attempt_generation": claim.attempt_generation,
        }
        try:
            process = self._process_context.Process(
                target=self._process_entrypoint,
                args=(claim.run_id, claim.work_spec, claim_envelope),
                name=f"workflow-{claim.run_id[:48]}",
                daemon=False,
            )
            process.start()
        except Exception:
            self._terminalize(
                claim.run_id,
                _terminal_outcome("failed", "WORKER_SPAWN_FAILED"),
                expected_supervisor_id=claim.supervisor_id,
                expected_attempt_generation=claim.attempt_generation,
            )
            return False

        worker_pid = getattr(process, "pid", None)
        registered = False
        if (
            not isinstance(worker_pid, bool)
            and isinstance(worker_pid, int)
            and worker_pid > 0
        ):
            try:
                worker_started_at_ms = self._process_identity_probe(worker_pid)
            except Exception:
                worker_started_at_ms = None
            try:
                registered = self._store.set_worker_pid(
                    claim.run_id,
                    worker_pid,
                    claim.supervisor_id,
                    claim.attempt_generation,
                    worker_started_at_ms,
                )
            except Exception:
                logger.exception(
                    "Failed to register workflow worker PID for %s",
                    claim.run_id,
                )
        if not registered:
            self._terminate_process(process)
            process.join()
            self._terminalize(
                claim.run_id,
                _terminal_outcome(
                    "failed",
                    "WORKER_PID_REGISTRATION_FAILED",
                ),
                expected_supervisor_id=claim.supervisor_id,
                expected_attempt_generation=claim.attempt_generation,
            )
            return False

        handle = _ProcessHandle(
            run_id=claim.run_id,
            process=process,
            deadline_at_ms=claim.deadline_at_ms,
            started_at_ms=self._now_ms(),
            supervisor_id=claim.supervisor_id,
            attempt_generation=claim.attempt_generation,
        )
        with self._handles_lock:
            if claim.run_id in self._handles:
                self._terminate_process(process)
                self._terminalize(
                    claim.run_id,
                    _terminal_outcome("failed", "DUPLICATE_LOCAL_HANDLE"),
                    expected_supervisor_id=claim.supervisor_id,
                    expected_attempt_generation=claim.attempt_generation,
                )
                return False
            self._handles[claim.run_id] = handle
        try:
            watcher = self._thread_factory(
                target=lambda: self._watch_process(claim.run_id),
                name=f"workflow-watch-{claim.run_id[:48]}",
                daemon=True,
            )
            handle.watcher = watcher
            watcher.start()
        except Exception:
            handle.requested_outcome = _terminal_outcome(
                "failed",
                "WATCHER_START_FAILED",
            )
            should_terminalize = self._fence_and_terminate(handle)
            self._release_handle(
                claim.run_id,
                handle.requested_outcome if should_terminalize else None,
            )
            return False
        return True

    def _watch_process(self, run_id: str) -> None:
        outcome: dict[str, object] | None = None
        should_terminalize = True
        try:
            next_heartbeat_ms = self._now_ms()
            while True:
                with self._handles_lock:
                    handle = self._handles.get(run_id)
                if handle is None:
                    return
                process = handle.process
                if not self._is_alive(process):
                    outcome = handle.requested_outcome or self._exit_outcome(process)
                    break
                now_ms = self._now_ms()
                if handle.requested_outcome is not None:
                    outcome = handle.requested_outcome
                    should_terminalize = self._fence_and_terminate(handle)
                    break
                if now_ms >= handle.deadline_at_ms:
                    outcome = _terminal_outcome(
                        "timed_out",
                        "WORKFLOW_DEADLINE_EXCEEDED",
                    )
                    should_terminalize = self._fence_and_terminate(handle)
                    break
                if now_ms >= next_heartbeat_ms:
                    renewed = self._store.renew_worker_lease(
                        run_id,
                        handle.supervisor_id,
                        handle.attempt_generation,
                        now_ms + self._config.lease_seconds * 1_000,
                    )
                    if not renewed:
                        outcome, should_terminalize = self._lease_failure(handle)
                        if should_terminalize:
                            should_terminalize = self._fence_and_terminate(handle)
                        break
                    next_heartbeat_ms = (
                        now_ms + max(1, self._config.lease_seconds * 1_000 // 3)
                    )
                remaining_seconds = max(
                    0.001,
                    (handle.deadline_at_ms - now_ms) / 1_000,
                )
                if self._wait(min(self._poll_interval_seconds, remaining_seconds)):
                    outcome = _terminal_outcome(
                        "cancelled",
                        "SUPERVISOR_STOPPED",
                    )
                    should_terminalize = self._fence_and_terminate(handle)
                    break
        except Exception:
            logger.exception("Workflow process watcher failed for %s", run_id)
            outcome = _terminal_outcome("failed", "SUPERVISOR_WATCH_FAILED")
            with self._handles_lock:
                failed_handle = self._handles.get(run_id)
            should_terminalize = (
                failed_handle is not None
                and self._fence_and_terminate(failed_handle)
            )
        finally:
            with self._handles_lock:
                handle = self._handles.get(run_id)
            if handle is not None:
                if self._is_alive(handle.process) and should_terminalize:
                    should_terminalize = self._fence_and_terminate(handle)
                if self._is_alive(handle.process):
                    # handle.process — наш собственный дочерний процесс (child
                    # делает os.setsid), killpg его группы безопасен независимо
                    # от исхода fence
                    self._terminate_process(handle.process)
                if self._is_alive(handle.process):
                    handle.process.join(timeout=0)
                else:
                    handle.process.join()
                self._release_handle(
                    run_id,
                    outcome if should_terminalize else None,
                )

    def _lease_failure(
        self,
        handle: _ProcessHandle,
    ) -> tuple[dict[str, object] | None, bool]:
        run = self._store.get_run(handle.run_id)
        if run is None:
            return None, False
        if _field(run, "cancel_requested_at_ms") is not None:
            return _terminal_outcome("cancelled", "CANCEL_REQUESTED"), True
        status = str(_field(run, "status", "")).lower()
        owner = _field(run, "supervisor_id")
        generation = _field(run, "attempt_started_at_ms")
        if (
            status in _TERMINAL_STATES
            or status == "result_pending"
            or owner != handle.supervisor_id
            or generation != handle.attempt_generation
        ):
            return None, False
        return _terminal_outcome("failed", "WORKER_LEASE_LOST"), True

    def _cancel_local(self, run_id: str, *, reason: str) -> bool:
        with self._handles_lock:
            handle = self._handles.get(run_id)
            if handle is None or handle.finalized:
                return False
            handle.state = "cancelling"
            handle.requested_outcome = _terminal_outcome("cancelled", reason)
        should_terminalize = self._fence_and_terminate(handle)
        if not should_terminalize:
            with self._handles_lock:
                current = self._handles.get(run_id)
                if current is handle and not handle.finalized:
                    handle.state = "running"
                    handle.requested_outcome = None
            return False
        if self._is_alive(handle.process):
            handle.process.join(timeout=0)
        else:
            handle.process.join()
        self._release_handle(
            run_id,
            handle.requested_outcome,
        )
        return True

    def _fence_and_terminate(self, handle: _ProcessHandle) -> bool:
        with self._handles_lock:
            current = self._handles.get(handle.run_id)
            if current is not handle or handle.finalized:
                return False
            if not handle.termination_fenced:
                next_generation = self._store.fence_worker_attempt(
                    handle.run_id,
                    handle.supervisor_id,
                    handle.attempt_generation,
                    self._now_ms() + self._config.lease_seconds * 1_000,
                )
                if next_generation is None:
                    return False
                handle.attempt_generation = next_generation
                handle.termination_fenced = True
            process = handle.process
        self._terminate_process(process)
        return True

    def _release_handle(
        self,
        run_id: str,
        outcome: Mapping[str, object] | None,
    ) -> None:
        with self._handles_lock:
            handle = self._handles.get(run_id)
            if handle is None or handle.finalized:
                return
        if outcome is not None and not self._terminalize(
            run_id,
            outcome,
            expected_supervisor_id=handle.supervisor_id,
            expected_attempt_generation=handle.attempt_generation,
        ):
            run = self._store.get_run(run_id)
            if self._same_running_claim(run, handle):
                handle.state = "finalizing"
                handle.requested_outcome = dict(outcome)
                renewed = self._store.renew_worker_lease(
                    run_id,
                    handle.supervisor_id,
                    handle.attempt_generation,
                    self._now_ms() + self._config.lease_seconds * 1_000,
                )
                if renewed:
                    return
        self._drop_handle(run_id, handle)
        if self._running:
            self._schedule_once()

    def _retry_pending_finalizations(self, now_ms: int) -> None:
        with self._handles_lock:
            pending = tuple(
                handle
                for handle in self._handles.values()
                if handle.state == "finalizing"
                and handle.requested_outcome is not None
                and not handle.finalized
            )
        for handle in pending:
            assert handle.requested_outcome is not None
            if self._terminalize(
                handle.run_id,
                handle.requested_outcome,
                expected_supervisor_id=handle.supervisor_id,
                expected_attempt_generation=handle.attempt_generation,
            ):
                self._drop_handle(handle.run_id, handle)
                continue
            run = self._store.get_run(handle.run_id)
            if not self._same_running_claim(run, handle) or not (
                self._store.renew_worker_lease(
                    handle.run_id,
                    handle.supervisor_id,
                    handle.attempt_generation,
                    now_ms + self._config.lease_seconds * 1_000,
                )
            ):
                self._drop_handle(handle.run_id, handle)

    @staticmethod
    def _same_running_claim(run: object | None, handle: _ProcessHandle) -> bool:
        return (
            run is not None
            and str(_field(run, "status", "")).lower() == "running"
            and _field(run, "supervisor_id") == handle.supervisor_id
            and _field(run, "attempt_started_at_ms")
            == handle.attempt_generation
        )

    def _drop_handle(self, run_id: str, handle: _ProcessHandle) -> None:
        with self._handles_lock:
            if self._handles.get(run_id) is handle:
                handle.finalized = True
                self._handles.pop(run_id, None)

    def _terminalize(
        self,
        run_id: str,
        outcome: Mapping[str, object],
        *,
        expected_supervisor_id: str | None,
        expected_attempt_generation: int | None,
    ) -> bool:
        normalized = self._normalize_terminal_outcome(outcome)
        if expected_supervisor_id is None or isinstance(
            expected_supervisor_id,
            str,
        ):
            supervisor_id = expected_supervisor_id
        else:
            raise TypeError("expected_supervisor_id must be text or null")
        if expected_attempt_generation is None or (
            isinstance(expected_attempt_generation, int)
            and not isinstance(expected_attempt_generation, bool)
            and expected_attempt_generation > 0
        ):
            attempt_generation = expected_attempt_generation
        else:
            raise TypeError("expected_attempt_generation must be positive or null")
        return bool(
            self._terminalizer(
                run_id,
                supervisor_id,
                attempt_generation,
                normalized,
            )
        )

    def _finish_worker(
        self,
        run_id: str,
        supervisor_id: str | None,
        attempt_generation: int | None,
        outcome: Mapping[str, object],
    ) -> bool:
        if supervisor_id is None or attempt_generation is None:
            return False
        return self._store.finish_worker(
            run_id,
            supervisor_id,
            attempt_generation,
            outcome,
        )

    @staticmethod
    def _normalize_terminal_outcome(value: object) -> dict[str, object]:
        mapping = _to_mapping(value)
        if not isinstance(mapping, Mapping):
            raise ValueError("terminal outcome must be an object")
        status = mapping.get("status")
        reason = mapping.get("reason")
        if not isinstance(status, str):
            raise ValueError("terminal outcome status is required")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("terminal outcome reason must be text")
        return _terminal_outcome(status, reason)

    @staticmethod
    def _exit_outcome(process: object) -> dict[str, object]:
        exitcode = getattr(process, "exitcode", None)
        if exitcode == 0:
            return _terminal_outcome("succeeded", "WORKER_EXITED")
        return _terminal_outcome("failed", "WORKER_CRASHED")

    def _terminate_process(self, process: object) -> None:
        if not self._is_alive(process):
            return
        self._process_group_signal(process, signal.SIGTERM)
        process.join(timeout=float(self._config.cancel_grace_seconds))
        if self._is_alive(process):
            self._process_group_signal(process, signal.SIGKILL)
            killer = getattr(process, "kill", None)
            if callable(killer) and self._is_alive(process):
                killer()
            process.join(timeout=float(self._config.cancel_grace_seconds))
            if self._is_alive(process):
                logger.error(
                    "Workflow worker process %s did not exit after SIGKILL; "
                    "abandoning join (process likely stuck in an "
                    "uninterruptible syscall)",
                    getattr(process, "pid", None),
                )

    @staticmethod
    def _is_alive(process: object) -> bool:
        checker = getattr(process, "is_alive", None)
        return bool(checker()) if callable(checker) else False

    @staticmethod
    def _default_process_signal(process: object, sig: int) -> None:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0 and os.name == "posix":
            try:
                process_group = os.getpgid(pid)
                if process_group != os.getpgrp():
                    os.killpg(process_group, sig)
                    return
            except (OSError, ProcessLookupError):
                pass
        method_name = "terminate" if sig == signal.SIGTERM else "kill"
        method = getattr(process, method_name, None)
        if callable(method):
            method()

    @staticmethod
    def _default_process_identity_probe(pid: int) -> int | None:
        import psutil

        try:
            return int(psutil.Process(pid).create_time() * 1000)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ImportError, OSError, ValueError):
            return None

    def _default_stale_process_reaper(
        self,
        pid: int,
        expected_started_at_ms: int | None,
        grace_seconds: float,
    ) -> None:
        if pid <= 0:
            return
        if expected_started_at_ms is None:
            return
        current_started_at_ms = self._process_identity_probe(pid)
        if (
            current_started_at_ms is None
            or abs(current_started_at_ms - expected_started_at_ms) > 2
        ):
            return

        def signal_pid(sig: int) -> None:
            if os.name == "posix":
                process_group = os.getpgid(pid)
                if process_group == os.getpgrp():
                    raise RuntimeError("refusing to signal the supervisor process group")
                os.killpg(process_group, sig)
            else:
                os.kill(pid, sig)

        try:
            signal_pid(signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            signal_pid(signal.SIGKILL)
        except ProcessLookupError:
            return

    @staticmethod
    def _validate_run_id(value: object) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 256
            or not all(character.isprintable() for character in value)
        ):
            raise ValueError("run_id must be canonical text")
        return value


__all__ = [
    "CancellationResult",
    "DEFAULT_WORKFLOW_PROCESS_CANCEL_GRACE_SECONDS",
    "DEFAULT_WORKFLOW_PROCESS_GLOBAL_LIMIT",
    "DEFAULT_WORKFLOW_PROCESS_LEASE_SECONDS",
    "DEFAULT_WORKFLOW_PROCESS_PER_OWNER_LIMIT",
    "DEFAULT_WORKFLOW_PROCESS_PER_TENANT_LIMIT",
    "DEFAULT_WORKFLOW_PROCESS_QUEUE_LIMIT",
    "ProcessSnapshot",
    "SubmissionResult",
    "SupervisorConfig",
    "SupervisorStore",
    "WorkflowAdmissionRejected",
    "WorkflowProcessSupervisor",
    "WorkflowSupervisorConfigError",
    "validate_work_spec",
]
