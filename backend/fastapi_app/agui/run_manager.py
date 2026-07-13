"""Run registry and event publishing for AG-UI streams."""

from __future__ import annotations

import asyncio
import enum
import functools
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from pydantic import TypeAdapter, ValidationError

from .auth import Principal, current_principal, principal_context
from .encoder import EventEncoder
from .events import (
    BaseEvent,
    CustomEvent,
    Event,
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
)
from .models import RunAgentInput
from .redaction import _redact_payload, redact_pii_in_payload
from .runner import run_agent
from .store import EventStore, StoredRun, WorkflowRunInvocation

logger = logging.getLogger(__name__)


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    ERRORED = "errored"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.FINISHED, RunStatus.ERRORED, RunStatus.CANCELLED}
)
_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value}
)
_WORKFLOW_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)
_DEFAULT_MAX_CONCURRENT_RUNS = 128
_RUN_CANCEL_WAIT_SECONDS = 10.0
_TEXT_TO_SQL_SERVICE_ACTION = "presets.text_to_sql.generate"
_TEXT_TO_SQL_DURABLE_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "abstained", "failed", "cancelled", "timed_out"}
)
_DURABLE_FOLLOW_POLL_SECONDS = 0.05
_DURABLE_FOLLOW_MAX_RETRY_SECONDS = 1.0
_EVENT_ADAPTER = TypeAdapter(Event)


def _max_concurrent_runs() -> int:
    raw = os.getenv("AG_UI_MAX_CONCURRENT_RUNS")
    if raw is None or raw == "":
        return _DEFAULT_MAX_CONCURRENT_RUNS
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"AG_UI_MAX_CONCURRENT_RUNS must be a positive integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"AG_UI_MAX_CONCURRENT_RUNS must be >= 1, got {value}")
    return value


def is_terminal_event_payload(event_type: str, payload: dict) -> bool:
    if event_type in _TERMINAL_EVENT_TYPES:
        return True
    if event_type == "WORKFLOW_RESULT":
        return str(payload.get("status") or "").lower() in _WORKFLOW_TERMINAL_STATUSES
    if event_type != EventType.CUSTOM.value:
        return False
    name = payload.get("name")
    value = payload.get("value")
    if name == "service.result":
        if (
            isinstance(value, dict)
            and value.get("action") == _TEXT_TO_SQL_SERVICE_ACTION
            and value.get("ok") is True
            and isinstance(value.get("data"), dict)
            and value["data"].get("run_id")
        ):
            return False
        return True
    if name == "workflow.result":
        return True
    if name == "workflow.progress" and isinstance(value, dict):
        return str(value.get("status") or "").lower() in _WORKFLOW_TERMINAL_STATUSES
    return False


def is_terminal_event(event: BaseEvent) -> bool:
    payload = event.model_dump(by_alias=True, exclude_none=True)
    return is_terminal_event_payload(event.type.value, payload)


@dataclass
class RunInfo:
    run_id: str
    thread_id: str
    principal: Principal
    status: RunStatus
    started_at_ms: int
    finished_at_ms: Optional[int] = None
    task: Optional[asyncio.Task] = None
    events: list[tuple[int, BaseEvent]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[Optional[tuple[int, BaseEvent]]]] = field(
        default_factory=set
    )
    lifecycle_started: asyncio.Event = field(default_factory=asyncio.Event)
    startup_error: Optional[BaseException] = None
    cancel_requested: bool = False
    durable_follower: bool = False
    durable_cancel_attempted: bool = False
    finalization_task: Optional[asyncio.Task[None]] = None


@dataclass(frozen=True)
class _TextToSqlAdmission:
    idempotency_key: Optional[str]
    request_fingerprint: str
    workflow_name: str


@dataclass(frozen=True)
class RequestReplayView:
    run_id: str
    request_id: Optional[str]
    after: int


@dataclass(frozen=True)
class _DurableCancelOutcome:
    cancelled: bool
    retryable: bool = False


@dataclass(frozen=True)
class _UncachedCancelDispatchResult:
    explicit_false: bool
    failed: bool = False


def _is_nonterminal_text_to_sql(stored: Optional[StoredRun]) -> bool:
    return (
        stored is not None
        and stored.run_kind == "text_to_sql"
        and stored.status not in _TEXT_TO_SQL_DURABLE_TERMINAL_STATUSES
    )


def _text_to_sql_admission(
    input_data: RunAgentInput,
) -> Optional[_TextToSqlAdmission]:
    forwarded = input_data.forwarded_props
    if not isinstance(forwarded, dict):
        return None
    if forwarded.get("service_action") != _TEXT_TO_SQL_SERVICE_ACTION:
        return None
    payload = forwarded.get("service_payload")
    if not isinstance(payload, dict):
        raise ValueError("service_payload must be an object")
    from ._t2s_requests import (
        canonical_text_to_sql_start_fingerprint,
        parse_text_to_sql_start,
    )

    request = parse_text_to_sql_start(payload)
    return _TextToSqlAdmission(
        idempotency_key=request.idempotency_key,
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        workflow_name=request.workflow_name,
    )


def _text_to_sql_transport_terminal(
    store: EventStore,
    info: RunInfo,
    *,
    workflow_name: str,
    status: str,
    error: str,
    run_incarnation: Optional[str] = None,
) -> bool:
    from workflow.streamlit_api import (
        _build_text_to_sql_no_runtime_terminal,
        _build_workflow_result_event_payload,
    )

    public_error = str(redact_pii_in_payload(_redact_payload(error)))
    reason_code = "CANCELLED" if status == "cancelled" else (
        "MANDATORY_STEP_NOT_COMPLETED"
    )
    terminal = _build_text_to_sql_no_runtime_terminal(
        run_id=info.run_id,
        status=status,
        reason_code=reason_code,
        error=public_error,
    )
    if run_incarnation is None:
        run_incarnation = f"agui-{uuid.uuid4().hex}"
        if not store.reserve_workflow_run(
            info.run_id,
            run_incarnation,
            info.thread_id,
            workflow_name,
        ):
            return False
    payload = _build_workflow_result_event_payload(
        info.run_id,
        terminal,
        "cancelled" if status == "cancelled" else "failed",
        error=public_error,
        artifacts={
            "final_output": terminal,
            "terminal_outcome": terminal,
        },
        snapshot={
            "workflow_name": workflow_name,
            "session_id": info.thread_id,
        },
        terminal_outcome=terminal,
        run_incarnation=run_incarnation,
    )
    store.finalize_run_with_event(info.run_id, payload)
    return True


def _compatibility_status(status: str) -> RunStatus:
    if status in {"pending", "legacy"}:
        return RunStatus.PENDING
    if status == "queued":
        return RunStatus.QUEUED
    if status in {"running", "result_pending"}:
        return RunStatus.RUNNING
    if status in {"succeeded", "abstained"}:
        return RunStatus.FINISHED
    if status == "cancelled":
        return RunStatus.CANCELLED
    return RunStatus.ERRORED


_EVICT_TTL_SECONDS: int = 30 * 60  # 30 minutes


class RunManager:
    def __init__(self, store: EventStore, evict_ttl_seconds: int = _EVICT_TTL_SECONDS) -> None:
        self._store = store
        self._runs: dict[str, RunInfo] = {}
        self._lock = asyncio.Lock()
        self._evict_ttl_seconds = evict_ttl_seconds
        self._uncached_cancel_dispatches: dict[
            str,
            asyncio.Task[_UncachedCancelDispatchResult],
        ] = {}
        self._store_executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="agui-store"
        )

    def close(self) -> None:
        """Shut down the dedicated store executor. Idempotent.

        Uses ``cancel_futures=False`` so in-flight (e.g. shielded) store
        calls started before shutdown are allowed to finish rather than
        being cancelled out from under callers still awaiting them.
        """
        self._store_executor.shutdown(wait=False, cancel_futures=False)

    @property
    def store_executor(self) -> ThreadPoolExecutor:
        """Dedicated thread pool for offloading blocking EventStore calls.

        Exposed so callers that share the same SQLite-backed store (e.g. the
        operational retention coordinator) can dispatch their own blocking
        store calls onto the same pool instead of the asyncio default
        executor.
        """
        return self._store_executor

    async def _call_store(self, func, *args, **kwargs):
        """Run a blocking EventStore call off the event loop thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._store_executor, functools.partial(func, *args, **kwargs)
        )

    @staticmethod
    def _service_request_id(input_data: RunAgentInput) -> Optional[str]:
        forwarded = input_data.forwarded_props
        payload = (
            forwarded.get("service_payload")
            if isinstance(forwarded, dict)
            else None
        )
        request_id = payload.get("__request_id") if isinstance(payload, dict) else None
        return request_id if isinstance(request_id, str) else None

    def _info_from_stored(self, stored: StoredRun) -> RunInfo:
        events: list[tuple[int, BaseEvent]] = []
        for stored_event in self._store.list_after(stored.run_id, 0):
            if stored_event.event_type not in {member.value for member in EventType}:
                continue
            try:
                event = _EVENT_ADAPTER.validate_python(stored_event.payload)
            except ValidationError:
                continue
            events.append((stored_event.seq, event))
        return RunInfo(
            run_id=stored.run_id,
            thread_id=stored.thread_id,
            principal=stored.principal,
            status=_compatibility_status(stored.status),
            started_at_ms=stored.created_at_ms,
            finished_at_ms=stored.finished_at_ms,
            events=events,
        )

    def _append_terminal_retry_projection(
        self,
        info: RunInfo,
        stored: StoredRun,
        input_data: RunAgentInput,
        admission: _TextToSqlAdmission,
        *,
        include_start: bool = True,
    ) -> None:
        forwarded = input_data.forwarded_props
        service_payload = (
            forwarded.get("service_payload")
            if isinstance(forwarded, dict)
            else {}
        )
        request_id = (
            service_payload.get("__request_id")
            if isinstance(service_payload, dict)
            else None
        )
        now_ms = int(time.time() * 1000)
        invocation = self._store.get_workflow_run_invocation(stored.run_id)
        session_id = (
            invocation.session_id if invocation is not None else stored.thread_id
        )
        start_event = CustomEvent(
            type=EventType.CUSTOM,
            name="service.result",
            value={
                "action": _TEXT_TO_SQL_SERVICE_ACTION,
                "ok": True,
                "data": {
                    "run_id": stored.run_id,
                    "workflow_name": admission.workflow_name,
                    "session_id": session_id,
                },
                "__request_id": request_id,
            },
            timestamp=now_ms,
        )
        existing_events = [event for _seq, event in info.events]
        events: list[BaseEvent] = [start_event] if include_start else []
        primary = (
            self._store.get_event(stored.run_id, stored.result_seq)
            if stored.result_seq is not None
            else None
        )
        if primary is not None and primary.event_type == "WORKFLOW_RESULT":
            result = primary.payload
            terminal = result.get("terminal_outcome")
            terminal_status = (
                str(terminal.get("status") or "failed")
                if isinstance(terminal, dict)
                else "failed"
            )
            expected_terminal_type = (
                EventType.RUN_FINISHED
                if terminal_status in {"succeeded", "abstained"}
                else EventType.RUN_ERROR
            )
            terminal_reason = str(
                terminal.get("reason_code")
                if isinstance(terminal, dict)
                else "Text-to-SQL terminal result failed"
            )
            terminal_projections = [
                event
                for event in existing_events
                if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
            ]
            has_winner_terminal_projection = any(
                (
                    expected_terminal_type == EventType.RUN_FINISHED
                    and isinstance(event, RunFinishedEvent)
                    and event.result == result
                )
                or (
                    expected_terminal_type == EventType.RUN_ERROR
                    and isinstance(event, RunErrorEvent)
                    and event.code == f"text_to_sql_{terminal_status}"
                    and event.message == terminal_reason
                )
                for event in terminal_projections
            )
            if terminal_projections and not has_winner_terminal_projection:
                logger.error(
                    "Stored transport terminal disagrees with durable Text-to-SQL "
                    "winner for %s; the request-scoped view will use result_seq",
                    stored.run_id,
                )
            if not any(
                isinstance(event, CustomEvent)
                and event.name == "workflow.result"
                and event.value == result
                for event in existing_events
            ):
                events.append(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="workflow.result",
                        value=result,
                        timestamp=now_ms,
                    )
                )
            if has_winner_terminal_projection:
                terminal_status = ""
            if terminal_status in {"succeeded", "abstained"}:
                events.append(
                    RunFinishedEvent(
                        type=EventType.RUN_FINISHED,
                        thread_id=stored.thread_id,
                        run_id=stored.run_id,
                        result=result,
                        timestamp=now_ms,
                    )
                )
            elif terminal_status:
                events.append(
                    RunErrorEvent(
                        type=EventType.RUN_ERROR,
                        message=terminal_reason,
                        code=f"text_to_sql_{terminal_status}",
                        timestamp=now_ms,
                    )
                )
        elif stored.status in {
            "succeeded",
            "abstained",
            "failed",
            "cancelled",
            "timed_out",
        }:
            error_code = (
                "text_to_sql_server_restarted"
                if stored.terminal_reason == "SERVER_RESTARTED"
                else "text_to_sql_terminal_without_result"
            )
            error_message = (
                "Text-to-SQL run was interrupted by server restart"
                if stored.terminal_reason == "SERVER_RESTARTED"
                else str(stored.terminal_reason or "run is terminal")
            )
            if not any(
                isinstance(event, RunErrorEvent) and event.code == error_code
                for event in existing_events
            ):
                events.append(
                    RunErrorEvent(
                        type=EventType.RUN_ERROR,
                        message=error_message,
                        code=error_code,
                        timestamp=now_ms,
                    )
                )
        for event in events:
            payload = event.model_dump(by_alias=True, exclude_none=True)
            seq = self._store.append(stored.run_id, event.type.value, payload)
            info.events.append((seq, event))

    def _read_durable_follow_state(
        self,
        run_id: str,
    ) -> tuple[Optional[StoredRun], Optional[WorkflowRunInvocation]]:
        """Fetch the run row and its workflow invocation in a single hop."""
        stored = self._store.get_run(run_id)
        if stored is None:
            return None, None
        invocation = self._store.get_workflow_run_invocation(run_id)
        return stored, invocation

    async def _follow_durable_text_to_sql(
        self,
        info: RunInfo,
        input_data: RunAgentInput,
        admission: _TextToSqlAdmission,
        *,
        outer_exit_status: Optional[RunStatus] = None,
        outer_error_message: Optional[str] = None,
    ) -> None:
        retry_seconds = _DURABLE_FOLLOW_POLL_SECONDS
        try:
            while True:
                try:
                    stored, invocation = await self._call_store(
                        self._read_durable_follow_state, info.run_id
                    )
                    if stored is None:
                        error = RuntimeError(
                            f"durable run disappeared: {info.run_id}"
                        )
                        if not info.lifecycle_started.is_set():
                            info.startup_error = error
                        logger.error(str(error))
                        return
                    if stored.status in _TEXT_TO_SQL_DURABLE_TERMINAL_STATUSES:
                        async with self._lock:
                            before = len(info.events)
                            await self._call_store(
                                self._append_terminal_retry_projection,
                                info,
                                stored,
                                input_data,
                                admission,
                                include_start=False,
                            )
                            published = info.events[before:]
                            info.status = _compatibility_status(stored.status)
                            info.finished_at_ms = stored.finished_at_ms
                            info.durable_cancel_attempted = False
                            self._uncached_cancel_dispatches.pop(info.run_id, None)
                            info.lifecycle_started.set()
                            subscribers = list(info.subscribers)
                            info.subscribers.clear()
                        for queue in subscribers:
                            for item in published:
                                await queue.put(item)
                            await queue.put(None)
                        return
                    if invocation is not None:
                        if invocation.workflow_name != admission.workflow_name:
                            await self._call_store(
                                _text_to_sql_transport_terminal,
                                self._store,
                                info,
                                workflow_name=admission.workflow_name,
                                status="failed",
                                error=(
                                    "durable workflow invocation does not match "
                                    "Text-to-SQL request"
                                ),
                                run_incarnation=invocation.run_incarnation,
                            )
                            retry_seconds = _DURABLE_FOLLOW_POLL_SECONDS
                            continue
                        if not info.lifecycle_started.is_set():
                            async with self._lock:
                                await self._call_store(
                                    self._append_terminal_retry_projection,
                                    info,
                                    stored,
                                    input_data,
                                    admission,
                                    include_start=False,
                                )
                                info.lifecycle_started.set()
                    elif outer_exit_status is not None:
                        cancelled = outer_exit_status is RunStatus.CANCELLED
                        await self._call_store(
                            _text_to_sql_transport_terminal,
                            self._store,
                            info,
                            workflow_name=admission.workflow_name,
                            status="cancelled" if cancelled else "failed",
                            error=(
                                "run cancelled"
                                if cancelled
                                else outer_error_message
                                or "Text-to-SQL finished before workflow invocation"
                            ),
                        )
                        retry_seconds = _DURABLE_FOLLOW_POLL_SECONDS
                        continue
                    elif stored.worker_pid is not None:
                        await self._call_store(
                            _text_to_sql_transport_terminal,
                            self._store,
                            info,
                            workflow_name=admission.workflow_name,
                            status="failed",
                            error=(
                                "worker PID exists without a durable workflow "
                                "invocation"
                            ),
                        )
                        retry_seconds = _DURABLE_FOLLOW_POLL_SECONDS
                        continue
                    retry_seconds = _DURABLE_FOLLOW_POLL_SECONDS
                    await asyncio.sleep(_DURABLE_FOLLOW_POLL_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Durable Text-to-SQL follower read failed for %s; retrying",
                        info.run_id,
                    )
                    await asyncio.sleep(retry_seconds)
                    retry_seconds = min(
                        retry_seconds * 2,
                        _DURABLE_FOLLOW_MAX_RETRY_SECONDS,
                    )
        finally:
            info.lifecycle_started.set()
            should_close = False
            try:
                stored = await self._call_store(self._store.get_run, info.run_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Unable to verify durable Text-to-SQL state while stopping "
                    "follower %s; keeping subscribers open",
                    info.run_id,
                )
            else:
                should_close = not _is_nonterminal_text_to_sql(stored)
            if should_close:
                async with self._lock:
                    subscribers = list(info.subscribers)
                    info.subscribers.clear()
                for queue in subscribers:
                    await queue.put(None)

    async def _prepare_existing_text_to_sql(
        self,
        info: RunInfo,
        stored: StoredRun,
        input_data: RunAgentInput,
        admission: _TextToSqlAdmission,
    ) -> None:
        """Project a retry onto the durable run without starting another worker."""
        if stored.status in {
            "succeeded",
            "abstained",
            "failed",
            "cancelled",
            "timed_out",
        }:
            await self._call_store(
                self._append_terminal_retry_projection,
                info,
                stored,
                input_data,
                admission,
            )
            info.status = _compatibility_status(stored.status)
            info.finished_at_ms = stored.finished_at_ms
            info.startup_error = None
            info.lifecycle_started.set()
            return
        invocation = await self._call_store(
            self._store.get_workflow_run_invocation, stored.run_id
        )
        if invocation is not None:
            if invocation.workflow_name != admission.workflow_name:
                raise ValueError("stored workflow invocation does not match request")
        await self._call_store(
            self._append_terminal_retry_projection,
            info,
            stored,
            input_data,
            admission,
        )
        if info.task is None or info.task.done():
            info.durable_follower = True
            info.startup_error = None
            info.lifecycle_started = asyncio.Event()
            info.task = asyncio.create_task(
                self._follow_durable_text_to_sql(
                    info,
                    input_data,
                    admission,
                )
            )

    async def _dispatch_uncached_text_to_sql_cancel(
        self,
        run_id: str,
        principal: Principal,
    ) -> _UncachedCancelDispatchResult:
        from .service import handle_service_action

        try:
            response = await asyncio.to_thread(
                handle_service_action,
                "workflows.cancel",
                {"run_id": run_id},
                principal=principal,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Uncached Text-to-SQL cancel dispatch failed for %s",
                run_id,
            )
            return _UncachedCancelDispatchResult(
                explicit_false=False,
                failed=True,
            )
        return _UncachedCancelDispatchResult(
            explicit_false=(
                isinstance(response, dict)
                and response.get("cancelled") is False
            )
        )

    async def _cancel_durable_text_to_sql(
        self,
        run_id: str,
        dispatch: asyncio.Task[_UncachedCancelDispatchResult],
    ) -> _DurableCancelOutcome:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RUN_CANCEL_WAIT_SECONDS
        try:
            result = await asyncio.wait_for(
                asyncio.shield(dispatch),
                timeout=max(_RUN_CANCEL_WAIT_SECONDS, 0.001),
            )
        except asyncio.TimeoutError:
            return _DurableCancelOutcome(cancelled=False)
        if result.failed or result.explicit_false:
            return _DurableCancelOutcome(
                cancelled=False,
                retryable=True,
            )

        while True:
            stored = await self._call_store(self._store.get_run, run_id)
            if stored is None:
                return _DurableCancelOutcome(cancelled=False)
            if stored.status == "cancelled":
                return _DurableCancelOutcome(cancelled=True)
            if stored.status in {
                "succeeded",
                "abstained",
                "failed",
                "timed_out",
            }:
                return _DurableCancelOutcome(cancelled=False)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _DurableCancelOutcome(cancelled=False)
            await asyncio.sleep(min(0.05, remaining))

    async def _cancel_durable_follower(
        self,
        info: RunInfo,
        dispatch: asyncio.Task[_UncachedCancelDispatchResult],
    ) -> bool:
        outcome = await self._cancel_durable_text_to_sql(
            info.run_id,
            dispatch,
        )
        if outcome.retryable:
            async with self._lock:
                stored = await self._call_store(self._store.get_run, info.run_id)
                if (
                    self._runs.get(info.run_id) is info
                    and _is_nonterminal_text_to_sql(stored)
                ):
                    info.durable_cancel_attempted = False
                    if self._uncached_cancel_dispatches.get(info.run_id) is dispatch:
                        self._uncached_cancel_dispatches.pop(info.run_id, None)
        elif outcome.cancelled:
            async with self._lock:
                if self._uncached_cancel_dispatches.get(info.run_id) is dispatch:
                    self._uncached_cancel_dispatches.pop(info.run_id, None)
        return outcome.cancelled

    @staticmethod
    async def _await_lifecycle_start(info: RunInfo) -> None:
        if info.task is None:
            return
        await info.lifecycle_started.wait()
        if info.startup_error is not None:
            raise RuntimeError(str(info.startup_error)) from info.startup_error
        if info.task.done():
            try:
                info.task.result()
            except asyncio.CancelledError as exc:
                raise RuntimeError("run task was cancelled during startup") from exc

    def _evict_stale(self) -> int:
        """Remove terminal RunInfo entries whose finished_at_ms is older than TTL.

        Returns the number of evicted entries. MUST be called while holding
        self._lock (it mutates self._runs); does not cancel running tasks.
        """
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - self._evict_ttl_seconds * 1000
        to_delete = [
            run_id
            for run_id, info in self._runs.items()
            if info.status in _TERMINAL_STATUSES
            and info.finished_at_ms is not None
            and info.finished_at_ms < cutoff_ms
        ]
        for run_id in to_delete:
            del self._runs[run_id]
        return len(to_delete)

    async def reconcile_orphaned_runs(self) -> int:
        stored_runs = await self._call_store(self._store.list_non_terminal_runs)
        protected = [
            stored.run_id
            for stored in stored_runs
            if await self._call_store(self._store.load_work_spec, stored.run_id)
            is not None
        ]
        return await self._call_store(
            self._store.reconcile_non_terminal_runs,
            reason="SERVER_RESTARTED",
            exclude_run_ids=protected,
        )

    async def _start_run_locked(
        self,
        input_data: RunAgentInput,
        principal: Principal,
        admission: Optional[_TextToSqlAdmission],
    ) -> tuple[RunInfo, Optional[int]]:
        replay_after: Optional[int] = None
        async with self._lock:
            self._evict_stale()
            info: Optional[RunInfo] = None
            if admission is not None and admission.idempotency_key is not None:
                existing = await self._call_store(
                    self._store.get_run_by_idempotency,
                    principal=principal,
                    run_kind="text_to_sql",
                    idempotency_key=admission.idempotency_key,
                )
                if existing is not None:
                    if (
                        existing.request_fingerprint
                        != admission.request_fingerprint
                    ):
                        raise ValueError(
                            "idempotency_key was already used for a different request"
                        )
                    info = self._runs.get(existing.run_id)
                    if info is None:
                        info = await self._call_store(
                            self._info_from_stored, existing
                        )
                        self._runs[existing.run_id] = info
                    current = (
                        await self._call_store(self._store.get_run, existing.run_id)
                        or existing
                    )
                    replay_after = (
                        await self._call_store(
                            self._store.latest_seq, existing.run_id
                        )
                        or 0
                    )
                    await self._prepare_existing_text_to_sql(
                        info,
                        current,
                        input_data,
                        admission,
                    )
            if info is None:
                if input_data.run_id in self._runs:
                    raise ValueError(f"run_id already exists: {input_data.run_id}")
                if (
                    await self._call_store(self._store.get_run, input_data.run_id)
                    is not None
                    or await self._call_store(
                        self._store.latest_seq, input_data.run_id
                    )
                    is not None
                ):
                    raise ValueError(f"run_id already exists: {input_data.run_id}")
                max_concurrent = _max_concurrent_runs()
                active_count = sum(
                    1 for active in self._runs.values()
                    if active.status not in _TERMINAL_STATUSES
                )
                if active_count >= max_concurrent:
                    raise ValueError(
                        "too many active AG-UI runs: "
                        f"{active_count} >= {max_concurrent}"
                    )
                if admission is None:
                    stored = await self._call_store(
                        self._store.create_run,
                        input_data.run_id,
                        input_data.thread_id,
                        principal,
                    )
                    created = True
                else:
                    stored, created = await self._call_store(
                        self._store.create_or_get_run,
                        principal=principal,
                        run_kind="text_to_sql",
                        idempotency_key=admission.idempotency_key,
                        request_fingerprint=admission.request_fingerprint,
                        proposed_run_id=input_data.run_id,
                        thread_id=input_data.thread_id,
                    )
                if not created:
                    assert admission is not None
                    info = self._runs.get(stored.run_id)
                    if info is None:
                        info = await self._call_store(
                            self._info_from_stored, stored
                        )
                        self._runs[stored.run_id] = info
                    current = (
                        await self._call_store(self._store.get_run, stored.run_id)
                        or stored
                    )
                    replay_after = (
                        await self._call_store(self._store.latest_seq, stored.run_id)
                        or 0
                    )
                    await self._prepare_existing_text_to_sql(
                        info,
                        current,
                        input_data,
                        admission,
                    )
                else:
                    info = await self._call_store(self._info_from_stored, stored)
                    if admission is not None:
                        replay_after = 0
                    info.task = asyncio.create_task(
                        self._execute_run(input_data, info)
                    )
                    self._runs[input_data.run_id] = info
        assert info is not None
        return info, replay_after

    async def _start_run(
        self,
        input_data: RunAgentInput,
        principal: Optional[Principal] = None,
    ) -> tuple[RunInfo, Optional[RequestReplayView]]:
        principal = principal or current_principal()
        admission = _text_to_sql_admission(input_data)
        # The locked critical section below now awaits store calls dispatched
        # to a worker thread. Shield it in its own task so that a caller being
        # cancelled (e.g. a disconnected HTTP request) cannot abandon the
        # section mid-way and leave a durable row without in-memory/task
        # bookkeeping; this mirrors the existing
        # _await_text_to_sql_finalizer shielding pattern. If cancellation is
        # observed while waiting, it is re-raised once the critical section
        # has safely completed, so callers relying on cancellation
        # propagation (e.g. task.cancel()) are not silently ignored.
        task: asyncio.Task[tuple[RunInfo, Optional[int]]] = asyncio.create_task(
            self._start_run_locked(input_data, principal, admission)
        )
        cancelled = False
        while True:
            try:
                info, replay_after = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    raise
        await self._await_lifecycle_start(info)
        view = (
            RequestReplayView(
                run_id=info.run_id,
                request_id=self._service_request_id(input_data),
                after=replay_after,
            )
            if replay_after is not None
            else None
        )
        if cancelled:
            raise asyncio.CancelledError()
        return info, view

    async def start_run(
        self,
        input_data: RunAgentInput,
        principal: Optional[Principal] = None,
    ) -> RunInfo:
        info, _view = await self._start_run(input_data, principal)
        return info

    async def start_run_request_view(
        self,
        input_data: RunAgentInput,
        principal: Optional[Principal] = None,
    ) -> tuple[RunInfo, Optional[RequestReplayView]]:
        return await self._start_run(input_data, principal)

    def _adopt_text_to_sql_follower_locked(
        self,
        input_data: RunAgentInput,
        info: RunInfo,
        admission: _TextToSqlAdmission,
        stored: Optional[StoredRun],
        outer_exit_status: RunStatus,
        outer_error_message: Optional[str],
        *,
        expected_outer_task: Optional[asyncio.Task[None]] = None,
    ) -> None:
        current_task = asyncio.current_task()
        if (
            info.task is current_task
            or info.task is expected_outer_task
            or info.task is None
            or info.task.done()
        ):
            info.task = asyncio.create_task(
                self._follow_durable_text_to_sql(
                    info,
                    input_data,
                    admission,
                    outer_exit_status=outer_exit_status,
                    outer_error_message=outer_error_message,
                )
            )
        if stored is not None:
            info.status = _compatibility_status(stored.status)
        else:
            info.status = RunStatus.RUNNING
        info.finished_at_ms = None
        info.startup_error = None
        info.cancel_requested = False
        info.durable_follower = True
        info.durable_cancel_attempted = False
        info.lifecycle_started.set()

    async def _finalize_text_to_sql_outer_run(
        self,
        input_data: RunAgentInput,
        info: RunInfo,
        admission: _TextToSqlAdmission,
        terminal_status: RunStatus,
        error_message: Optional[str],
        *,
        expected_outer_task: Optional[asyncio.Task[None]] = None,
    ) -> None:
        try:
            stored = await self._call_store(self._store.get_run, info.run_id)
            invocation = await self._call_store(
                self._store.get_workflow_run_invocation, info.run_id
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Unable to read Text-to-SQL state while finalizing outer run %s; "
                "adopting durable follower",
                info.run_id,
            )
            async with self._lock:
                self._adopt_text_to_sql_follower_locked(
                    input_data,
                    info,
                    admission,
                    None,
                    terminal_status,
                    error_message,
                    expected_outer_task=expected_outer_task,
                )
            return
        if _is_nonterminal_text_to_sql(stored) and invocation is None:
            cancelled = terminal_status is RunStatus.CANCELLED
            try:
                await self._call_store(
                    _text_to_sql_transport_terminal,
                    self._store,
                    info,
                    workflow_name=admission.workflow_name,
                    status="cancelled" if cancelled else "failed",
                    error=(
                        "run cancelled"
                        if cancelled
                        else error_message
                        or "Text-to-SQL finished before workflow invocation"
                    ),
                )
            except Exception:
                logger.exception(
                    "Unable to persist pre-invocation Text-to-SQL terminal for %s",
                    info.run_id,
                )
        elif (
            _is_nonterminal_text_to_sql(stored)
            and invocation is not None
            and invocation.workflow_name != admission.workflow_name
        ):
            try:
                await self._call_store(
                    _text_to_sql_transport_terminal,
                    self._store,
                    info,
                    workflow_name=admission.workflow_name,
                    status="failed",
                    error=(
                        "durable workflow invocation does not match Text-to-SQL "
                        "request"
                    ),
                    run_incarnation=invocation.run_incarnation,
                )
            except Exception:
                logger.exception(
                    "Unable to persist mismatched Text-to-SQL invocation terminal "
                    "for %s",
                    info.run_id,
                )

        published: list[tuple[int, BaseEvent]] = []
        subscribers: list[asyncio.Queue[Optional[tuple[int, BaseEvent]]]] = []
        try:
            async with self._lock:
                stored = await self._call_store(self._store.get_run, info.run_id)
                invocation = await self._call_store(
                    self._store.get_workflow_run_invocation, info.run_id
                )
                if _is_nonterminal_text_to_sql(stored):
                    if (
                        invocation is not None
                        and invocation.workflow_name != admission.workflow_name
                    ):
                        logger.error(
                            "Durable workflow invocation does not match Text-to-SQL "
                            "outer run %s",
                            info.run_id,
                        )
                    self._adopt_text_to_sql_follower_locked(
                        input_data,
                        info,
                        admission,
                        stored,
                        terminal_status,
                        error_message,
                        expected_outer_task=expected_outer_task,
                    )
                    return

                before = len(info.events)
                if stored is not None:
                    await self._call_store(
                        self._append_terminal_retry_projection,
                        info,
                        stored,
                        input_data,
                        admission,
                        include_start=False,
                    )
                    info.status = _compatibility_status(stored.status)
                    info.finished_at_ms = stored.finished_at_ms
                else:
                    info.status = RunStatus.ERRORED
                    info.finished_at_ms = int(time.time() * 1000)
                info.durable_follower = False
                published = info.events[before:]
                subscribers = list(info.subscribers)
                info.subscribers.clear()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Unable to reconcile Text-to-SQL outer run %s; adopting durable "
                "follower",
                info.run_id,
            )
            async with self._lock:
                self._adopt_text_to_sql_follower_locked(
                    input_data,
                    info,
                    admission,
                    None,
                    terminal_status,
                    error_message,
                    expected_outer_task=expected_outer_task,
                )
            return

        for queue in subscribers:
            for item in published:
                await queue.put(item)
            await queue.put(None)

    async def _run_text_to_sql_finalizer(
        self,
        input_data: RunAgentInput,
        info: RunInfo,
        admission: _TextToSqlAdmission,
        terminal_status: RunStatus,
        error_message: Optional[str],
        *,
        expected_outer_task: asyncio.Task[None],
    ) -> None:
        try:
            await self._finalize_text_to_sql_outer_run(
                input_data,
                info,
                admission,
                terminal_status,
                error_message,
                expected_outer_task=expected_outer_task,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Text-to-SQL outer finalizer failed for %s; adopting durable follower",
                info.run_id,
            )
            async with self._lock:
                self._adopt_text_to_sql_follower_locked(
                    input_data,
                    info,
                    admission,
                    None,
                    terminal_status,
                    error_message,
                    expected_outer_task=expected_outer_task,
                )

    async def _adopt_after_interrupted_text_to_sql_finalizer(
        self,
        input_data: RunAgentInput,
        info: RunInfo,
        admission: _TextToSqlAdmission,
        terminal_status: RunStatus,
        error_message: Optional[str],
        *,
        expected_outer_task: asyncio.Task[None],
    ) -> None:
        logger.error(
            "Text-to-SQL outer finalizer was cancelled for %s; adopting durable follower",
            info.run_id,
        )
        async with self._lock:
            self._adopt_text_to_sql_follower_locked(
                input_data,
                info,
                admission,
                None,
                terminal_status,
                error_message,
                expected_outer_task=expected_outer_task,
            )

    async def _await_text_to_sql_finalizer(
        self,
        input_data: RunAgentInput,
        info: RunInfo,
        admission: _TextToSqlAdmission,
        terminal_status: RunStatus,
        error_message: Optional[str],
    ) -> None:
        outer_task = asyncio.current_task()
        assert outer_task is not None
        finalization_task: asyncio.Task[None] = asyncio.create_task(
            self._run_text_to_sql_finalizer(
                input_data,
                info,
                admission,
                terminal_status,
                error_message,
                expected_outer_task=outer_task,
            )
        )
        info.finalization_task = finalization_task
        try:
            while True:
                try:
                    await asyncio.shield(finalization_task)
                    break
                except asyncio.CancelledError:
                    if finalization_task.cancelled():
                        finalization_task = asyncio.create_task(
                            self._adopt_after_interrupted_text_to_sql_finalizer(
                                input_data,
                                info,
                                admission,
                                terminal_status,
                                error_message,
                                expected_outer_task=outer_task,
                            )
                        )
                        info.finalization_task = finalization_task
        finally:
            if info.finalization_task is finalization_task:
                info.finalization_task = None

    async def _finalize_generic_run(
        self, info: RunInfo, terminal_status: RunStatus
    ) -> None:
        lifecycle_status = {
            RunStatus.FINISHED: "succeeded",
            RunStatus.ERRORED: "failed",
            RunStatus.CANCELLED: "cancelled",
        }[terminal_status]
        lifecycle_reason = {
            RunStatus.FINISHED: None,
            RunStatus.ERRORED: "RUN_ERROR",
            RunStatus.CANCELLED: "CANCELLED",
        }[terminal_status]
        await self._call_store(
            self._store.transition_run,
            info.run_id,
            {"pending", "queued", "running", "result_pending"},
            lifecycle_status,
            reason=lifecycle_reason,
        )
        stored = await self._call_store(self._store.get_run, info.run_id)
        if stored is not None:
            terminal_status = _compatibility_status(stored.status)
        async with self._lock:
            info.status = terminal_status
            info.finished_at_ms = int(time.time() * 1000)
            subscribers = list(info.subscribers)
            info.subscribers.clear()
        for queue in subscribers:
            await queue.put(None)

    async def _execute_run(self, input_data: RunAgentInput, info: RunInfo) -> None:
        admission = _text_to_sql_admission(input_data)
        try:
            if admission is None:
                if not await self._call_store(
                    self._store.transition_run,
                    info.run_id,
                    {"pending", "queued"},
                    "running",
                ):
                    stored = await self._call_store(self._store.get_run, info.run_id)
                    if stored is None or stored.status != "running":
                        raise RuntimeError(
                            f"run lifecycle could not enter running: {info.run_id}"
                        )
                info.status = RunStatus.RUNNING
            else:
                stored = await self._call_store(self._store.get_run, info.run_id)
                if not _is_nonterminal_text_to_sql(stored):
                    raise RuntimeError(
                        f"run lifecycle is not eligible for Text-to-SQL admission: "
                        f"{info.run_id}"
                    )
                info.status = _compatibility_status(stored.status)
        except Exception as exc:  # noqa: BLE001
            if admission is None:
                info.lifecycle_started.set()
                raise
            startup_error = RuntimeError(
                f"run lifecycle could not enter running: {info.run_id}"
            )
            await self._await_text_to_sql_finalizer(
                input_data,
                info,
                admission,
                RunStatus.ERRORED,
                str(exc),
            )
            info.startup_error = startup_error
            info.lifecycle_started.set()
            return
        info.lifecycle_started.set()
        terminal_status = RunStatus.FINISHED
        error_message: Optional[str] = None

        async def _process_run_event(event: BaseEvent) -> None:
            nonlocal terminal_status
            if admission is not None and event.type in {
                EventType.RUN_FINISHED,
                EventType.RUN_ERROR,
            }:
                gate_stored = await self._call_store(
                    self._store.get_run, info.run_id
                )
                if _is_nonterminal_text_to_sql(gate_stored):
                    if event.type == EventType.RUN_ERROR:
                        terminal_status = RunStatus.ERRORED
                    return
            await self._publish_event(info.run_id, event)
            if event.type == EventType.RUN_ERROR:
                terminal_status = RunStatus.ERRORED

        try:
            with principal_context(info.principal):
                agen = run_agent(input_data)
                # T2 перевёл запись событий на await-раунд-трипы через
                # ThreadPoolExecutor (_call_store/_publish_event), из-за чего
                # отмена может быть доставлена ВНЕ фрейма run_agent — в теле
                # обработки уже полученного события (gate-проверка выше или
                # _publish_event). Раньше запись была синхронной, и отмена
                # доставлялась только пока задача была приостановлена внутри
                # agen.__anext__() (т.е. в собственных await-точках run_agent
                # / вложенных генераторов), где успевала сработать их
                # грациозная cancel-логика (например, workflows.cancel в
                # _run_text_to_sql_service_action).
                #
                # agen.athrow(...) здесь не помогает: PEP 525 не прокидывает
                # брошенное исключение через `async for x in inner(): yield x`
                # — оно поднимается прямо во фрейме run_agent на месте yield,
                # не давая вложенному генератору шанса поймать его своим
                # `except asyncio.CancelledError` (проверено эмпирически).
                # Поэтому при отмене, пойманной в теле обработки события, мы
                # повторно отменяем свою же задачу и как ни в чём не бывало
                # продолжаем обычную итерацию agen.__anext__(): следующая
                # реальная точка приостановки внутри run_agent (или того, что
                # он делегирует) получит CancelledError на уровне Task/Future
                # — точно так же, как раньше при попадании отмены прямо в
                # agen.__anext__() — так что её собственный except сработает
                # штатно и событие после него пройдёт через тот же
                # _process_run_event. cancel_forwarded ограничивает пересылку
                # одним разом: повторная отмена после неё просто
                # пробрасывается наружу.
                cancel_forwarded = False
                try:
                    while True:
                        try:
                            event = await agen.__anext__()
                        except StopAsyncIteration:
                            break
                        try:
                            await _process_run_event(event)
                        except asyncio.CancelledError:
                            if cancel_forwarded:
                                raise
                            cancel_forwarded = True
                            current_task = asyncio.current_task()
                            assert current_task is not None
                            current_task.cancel()
                            continue
                finally:
                    await agen.aclose()
        except asyncio.CancelledError:
            terminal_status = RunStatus.CANCELLED
            error_message = "run cancelled"
            if admission is None:
                await self._publish_event(
                    info.run_id,
                    RunErrorEvent(
                        type=EventType.RUN_ERROR,
                        message="run cancelled",
                        code="cancelled",
                        timestamp=int(time.time() * 1000),
                    ),
                )
                raise
        except Exception as exc:  # noqa: BLE001
            terminal_status = RunStatus.ERRORED
            error_message = str(exc)
            if admission is None:
                await self._publish_event(
                    info.run_id,
                    RunErrorEvent(
                        type=EventType.RUN_ERROR,
                        message=redact_pii_in_payload(_redact_payload(str(exc))),
                        code="run_manager_error",
                        timestamp=int(time.time() * 1000),
                    ),
                )
        finally:
            if admission is not None:
                await self._await_text_to_sql_finalizer(
                    input_data,
                    info,
                    admission,
                    terminal_status,
                    error_message,
                )
            else:
                # Финализация generic-run (admission is None) обязана
                # завершиться даже при взведённой, но ещё не доставленной
                # отмене. Такая отмена возможна после cancel-пересылки в
                # цикле выше: если отмена была поймана на ПОСЛЕДНЕМ событии
                # генератора, следующий agen.__anext__() завершается
                # StopAsyncIteration синхронно (терминальный yield -> return
                # без await), aclose() на исчерпанном генераторе тоже не
                # приостанавливается, и _must_cancel доживает до этого
                # finally. Без shield первый же await self._call_store(...)
                # получил бы CancelledError: transition_run не выполнился бы,
                # info.status навсегда остался бы RUNNING, а подписчики
                # stream_live никогда не получили бы завершающий None.
                # Тот же паттерн, что в _start_run / _publish_event /
                # _await_text_to_sql_finalizer: shield + повторное ожидание +
                # re-raise отмены после завершения финализации.
                finalization_task: asyncio.Task[None] = asyncio.create_task(
                    self._finalize_generic_run(info, terminal_status)
                )
                cancelled = False
                while True:
                    try:
                        await asyncio.shield(finalization_task)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                        if finalization_task.done():
                            raise
                if cancelled:
                    raise asyncio.CancelledError()

    async def _publish_event_locked(
        self,
        run_id: str,
        redacted_payload: dict,
        redacted_event: BaseEvent,
    ) -> tuple[int, list[asyncio.Queue[Optional[tuple[int, BaseEvent]]]]]:
        async with self._lock:
            # The durable sequence and its in-memory/live publication are one
            # ordered operation relative to retry admission. Otherwise a retry
            # can snapshot a cursor, append a higher-sequence projection, and
            # make subscribers discard this lower sequence when it arrives.
            seq = await self._call_store(
                self._store.append, run_id, redacted_event.type.value, redacted_payload
            )
            info = self._runs.get(run_id)
            subscribers: list[asyncio.Queue[Optional[tuple[int, BaseEvent]]]] = []
            if info is not None:
                if info.status in _TERMINAL_STATUSES:
                    # Событие пришло после терминального статуса — это баг логики выше
                    # по стеку (run уже завершён). Логируем WARNING, но событие всё равно
                    # фиксируем в info.events и store: оно уже персистировано через
                    # self._store.append выше, и историческая выдача через replay должна
                    # остаться согласованной с persisted store.
                    logger.warning(
                        "AG-UI event %s published after terminal status %s for run_id=%s seq=%s",
                        redacted_event.type.value,
                        info.status.value,
                        run_id,
                        seq,
                    )
                info.events.append((seq, redacted_event))
                subscribers = list(info.subscribers)
        return seq, subscribers

    async def _publish_event(self, run_id: str, event: BaseEvent) -> None:
        payload = event.model_dump(by_alias=True, exclude_none=True)
        redacted_payload = redact_pii_in_payload(_redact_payload(payload))
        redacted_event = event.__class__.model_validate(redacted_payload)
        # Shield the locked append+fan-out bookkeeping from cancellation of the
        # calling task, same rationale/pattern as _start_run: the durable
        # sequence must not be assigned without also updating in-memory state,
        # even if whatever awaits this call gets cancelled mid-flight. If
        # cancellation is observed while waiting, it is re-raised once the
        # critical section has safely completed, so run cancellation (e.g.
        # RunManager.cancel_cached calling info.task.cancel() while an event
        # publish is in flight) is not silently swallowed.
        task: asyncio.Task[
            tuple[int, list[asyncio.Queue[Optional[tuple[int, BaseEvent]]]]]
        ] = asyncio.create_task(
            self._publish_event_locked(run_id, redacted_payload, redacted_event)
        )
        cancelled = False
        while True:
            try:
                seq, subscribers = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    raise
        for queue in subscribers:
            await queue.put((seq, redacted_event))
        if cancelled:
            raise asyncio.CancelledError()

    def _durable_terminal_view_events(self, stored: StoredRun) -> list[BaseEvent]:
        if stored.result_seq is None:
            return []
        primary = self._store.get_event(stored.run_id, stored.result_seq)
        if primary is None or primary.event_type != "WORKFLOW_RESULT":
            raise RuntimeError("run result_seq does not reference WORKFLOW_RESULT")
        result = primary.payload
        terminal = result.get("terminal_outcome")
        status = (
            str(terminal.get("status") or "failed")
            if isinstance(terminal, dict)
            else "failed"
        )
        now_ms = int(time.time() * 1000)
        workflow_event = CustomEvent(
            type=EventType.CUSTOM,
            name="workflow.result",
            value=result,
            timestamp=now_ms,
        )
        if status in {"succeeded", "abstained"}:
            terminal_event: BaseEvent = RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                thread_id=stored.thread_id,
                run_id=stored.run_id,
                result=result,
                timestamp=now_ms,
            )
        else:
            terminal_event = RunErrorEvent(
                type=EventType.RUN_ERROR,
                message=str(
                    terminal.get("reason_code")
                    if isinstance(terminal, dict)
                    else "Text-to-SQL terminal result failed"
                ),
                code=f"text_to_sql_{status}",
                timestamp=now_ms,
            )
        return [workflow_event, terminal_event]

    def _has_stored_terminal_event(self, run_id: str) -> bool:
        """Check whether a terminal event has already been persisted for run_id."""
        return any(
            is_terminal_event_payload(stored.event_type, stored.payload)
            for stored in self._store.list_after(run_id, 0)
        )

    async def durable_terminal_view(
        self,
        run_id: str,
    ) -> Optional[tuple[int, tuple[BaseEvent, BaseEvent]]]:
        """Return the authoritative transport projection for a terminal run."""
        stored = await self._call_store(self._store.get_run, run_id)
        if (
            stored is None
            or stored.run_kind != "text_to_sql"
            or stored.result_seq is None
            or stored.status
            not in {"succeeded", "abstained", "failed", "cancelled", "timed_out"}
        ):
            return None
        events = await self._call_store(self._durable_terminal_view_events, stored)
        return stored.result_seq, (events[0], events[1])

    async def stream_live(
        self,
        run_id: str,
        after: int = 0,
        *,
        request_view: Optional[RequestReplayView] = None,
    ) -> AsyncIterator[BaseEvent]:
        if request_view is not None:
            if request_view.run_id != run_id:
                raise ValueError("request replay view does not match run_id")
            after = request_view.after
        queue: asyncio.Queue[Optional[tuple[int, BaseEvent]]] = asyncio.Queue()
        # Snapshot буфера и подписка должны быть атомарными относительно
        # _publish_event и _execute_run.finally: иначе подписчик может пропустить
        # события, опубликованные после snapshot, но до добавления в subscribers,
        # либо подписаться на уже завершённый run и зависнуть в ожидании None.
        async with self._lock:
            info = self._runs.get(run_id)
            if info is None:
                return
            buffered = [(seq, event) for seq, event in info.events if seq > after]
            follow = info.status not in _TERMINAL_STATUSES
            if follow:
                info.subscribers.add(queue)

        last_seq = after

        def visible(event: BaseEvent) -> bool:
            if request_view is None:
                return True
            if not (
                isinstance(event, CustomEvent)
                and event.name == "service.result"
                and isinstance(event.value, dict)
                and event.value.get("action") == _TEXT_TO_SQL_SERVICE_ACTION
            ):
                return True
            return event.value.get("__request_id") == request_view.request_id

        def is_text_to_sql_terminal_projection(event: BaseEvent) -> bool:
            return event.type in {
                EventType.RUN_FINISHED,
                EventType.RUN_ERROR,
            } or (isinstance(event, CustomEvent) and event.name == "workflow.result")

        # try/finally охватывает и буфер, и live-цикл: если потребитель закроет
        # генератор (aclose / GeneratorExit) на любом yield, подписчик гарантированно
        # будет удалён из info.subscribers. Без этого был возможен подвисший подписчик
        # при отвале клиента до первого live-события (см. T3.6: subscriber_count
        # должен корректно отражать число активных читателей для cancel-on-disconnect).
        try:
            for seq, event in buffered:
                if seq <= last_seq:
                    continue
                last_seq = seq
                if not visible(event):
                    continue
                if request_view is not None and is_text_to_sql_terminal_projection(
                    event
                ):
                    continue
                yield event

            if follow:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    seq, event = item
                    if seq <= last_seq:
                        continue
                    last_seq = seq
                    if not visible(event):
                        continue
                    if request_view is not None and is_text_to_sql_terminal_projection(
                        event
                    ):
                        continue
                    yield event
        finally:
            async with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current.subscribers.discard(queue)
        if request_view is not None:
            stored = await self._call_store(self._store.get_run, run_id)
            if stored is not None and stored.result_seq is not None:
                for event in await self._call_store(
                    self._durable_terminal_view_events, stored
                ):
                    yield event
            elif (
                stored is not None
                and stored.status == "failed"
                and stored.terminal_reason == "SERVER_RESTARTED"
            ):
                yield RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message="Text-to-SQL run was interrupted by server restart",
                    code="text_to_sql_server_restarted",
                    timestamp=stored.finished_at_ms,
                )

    async def cancel_cached(self, run_id: str) -> Optional[bool]:
        durable_info: Optional[RunInfo] = None
        dispatch: Optional[asyncio.Task[_UncachedCancelDispatchResult]] = None
        async with self._lock:
            info = self._runs.get(run_id)
            if info is None:
                return None
            if info.task is None:
                return False
            if info.cancel_requested:
                return False
            stored_run = await self._call_store(self._store.get_run, run_id)
            durable_text_to_sql = _is_nonterminal_text_to_sql(stored_run)
            if stored_run is not None and (
                stored_run.run_kind == "text_to_sql"
                and stored_run.status in _TEXT_TO_SQL_DURABLE_TERMINAL_STATUSES
            ):
                self._uncached_cancel_dispatches.pop(run_id, None)
                return False
            dispatch = self._uncached_cancel_dispatches.get(run_id)
            if dispatch is not None:
                if not dispatch.done():
                    return False
                dispatch_result = (
                    _UncachedCancelDispatchResult(
                        explicit_false=False,
                        failed=True,
                    )
                    if dispatch.cancelled()
                    else dispatch.result()
                )
                if not (dispatch_result.failed or dispatch_result.explicit_false):
                    return False
                self._uncached_cancel_dispatches.pop(run_id, None)
                info.durable_cancel_attempted = False
                dispatch = None
            if not durable_text_to_sql:
                if info.status in _TERMINAL_STATUSES:
                    return False
                if any(is_terminal_event(event) for _seq, event in info.events):
                    return False
                if await self._call_store(self._has_stored_terminal_event, run_id):
                    return False
            if info.durable_follower:
                assert stored_run is not None
                dispatch = asyncio.create_task(
                    self._dispatch_uncached_text_to_sql_cancel(
                        run_id,
                        stored_run.principal,
                    )
                )
                self._uncached_cancel_dispatches[run_id] = dispatch
                info.durable_cancel_attempted = True
                durable_info = info
                task = None
            else:
                if durable_text_to_sql and not info.lifecycle_started.is_set():
                    return False
                info.task.cancel()
                info.cancel_requested = True
                task = info.task
        if durable_info is not None:
            assert dispatch is not None
            return await self._cancel_durable_follower(durable_info, dispatch)
        assert task is not None
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_RUN_CANCEL_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            async with self._lock:
                stored_run = await self._call_store(self._store.get_run, run_id)
                if (
                    self._runs.get(run_id) is info
                    and info.task is task
                    and not task.done()
                    and _is_nonterminal_text_to_sql(stored_run)
                ):
                    info.cancel_requested = False
            return False
        except asyncio.CancelledError:
            pass
        except Exception:
            return False
        stored = await self._call_store(self._store.get_run, run_id)
        return stored is not None and stored.status == "cancelled"

    async def cancel_uncached_text_to_sql(
        self,
        run_id: str,
        principal: Principal,
    ) -> bool:
        cached_run_appeared = False
        dispatch: Optional[asyncio.Task[_UncachedCancelDispatchResult]] = None
        async with self._lock:
            info = self._runs.get(run_id)
            if info is not None:
                if not principal.can_access(info.principal):
                    return False
                stored = await self._call_store(self._store.get_run, run_id)
            else:
                stored = await self._call_store(self._store.get_run, run_id)
                if (
                    stored is None
                    or stored.run_kind != "text_to_sql"
                    or stored.status in _TEXT_TO_SQL_DURABLE_TERMINAL_STATUSES
                ):
                    self._uncached_cancel_dispatches.pop(run_id, None)
                    return False
                if not principal.can_access(stored.principal):
                    return False
            dispatch = self._uncached_cancel_dispatches.get(run_id)
            if dispatch is not None and dispatch.done():
                dispatch_result = (
                    _UncachedCancelDispatchResult(
                        explicit_false=False,
                        failed=True,
                    )
                    if dispatch.cancelled()
                    else dispatch.result()
                )
                if dispatch_result.explicit_false:
                    self._uncached_cancel_dispatches.pop(run_id, None)
                    return False
                elif dispatch_result.failed:
                    self._uncached_cancel_dispatches.pop(run_id, None)
                    dispatch = None
                else:
                    return False
            if dispatch is None and info is not None:
                cached_run_appeared = True
            elif dispatch is None:
                dispatch = asyncio.create_task(
                    self._dispatch_uncached_text_to_sql_cancel(run_id, principal)
                )
                self._uncached_cancel_dispatches[run_id] = dispatch
        if cached_run_appeared:
            cancelled = await self.cancel_cached(run_id)
            return cancelled is True
        assert dispatch is not None
        try:
            dispatch_result = await asyncio.wait_for(
                asyncio.shield(dispatch),
                timeout=max(_RUN_CANCEL_WAIT_SECONDS, 0.001),
            )
        except asyncio.TimeoutError:
            return False

        stored = await self._call_store(self._store.get_run, run_id)
        terminal = (
            stored is None
            or stored.status in _TEXT_TO_SQL_DURABLE_TERMINAL_STATUSES
        )
        async with self._lock:
            if (
                self._uncached_cancel_dispatches.get(run_id) is dispatch
                and (
                    terminal
                    or dispatch_result.failed
                    or dispatch_result.explicit_false
                )
            ):
                self._uncached_cancel_dispatches.pop(run_id, None)
        return stored is not None and stored.status == "cancelled"

    async def cancel(self, run_id: str) -> bool:
        cancelled = await self.cancel_cached(run_id)
        return cancelled is True

    async def cancel_if_orphaned(self, run_id: str) -> bool:
        """Cancel a live run only when no subscriber and no terminal event exist."""
        async with self._lock:
            info = self._runs.get(run_id)
            if info is None or info.task is None:
                return False
            if info.subscribers:
                return False
            stored_run = await self._call_store(self._store.get_run, run_id)
            durable_text_to_sql = _is_nonterminal_text_to_sql(stored_run)
            if stored_run is not None and (
                stored_run.run_kind == "text_to_sql"
                and stored_run.status in _TEXT_TO_SQL_DURABLE_TERMINAL_STATUSES
            ):
                return False
            if not durable_text_to_sql:
                if info.status in _TERMINAL_STATUSES:
                    return False
                if any(is_terminal_event(event) for _seq, event in info.events):
                    return False
                if await self._call_store(self._has_stored_terminal_event, run_id):
                    return False
        return await self.cancel(run_id)

    def subscriber_count(self, run_id: str) -> int:
        """Возвращает число активных live-подписчиков run'а.

        Используется политикой «никого нет — отменяем» при disconnect SSE-клиента.
        Для несуществующих run'ов возвращает 0.
        """
        info = self._runs.get(run_id)
        if info is None:
            return 0
        return len(info.subscribers)

    def is_active(self, run_id: str) -> bool:
        """True, если run существует и его статус не терминальный."""
        info = self._runs.get(run_id)
        if info is None:
            return False
        return info.status not in _TERMINAL_STATUSES

    def get_info(self, run_id: str) -> Optional[RunInfo]:
        return self._runs.get(run_id)

    async def can_access(self, run_id: str, principal: Principal) -> bool:
        info = self._runs.get(run_id)
        if info is not None:
            return principal.can_access(info.principal)
        owner = await self._call_store(self._store.get_run_principal, run_id)
        if owner is not None:
            return principal.can_access(owner)
        latest = await self._call_store(self._store.latest_seq, run_id)
        return latest is not None and principal.has_role("admin")

    def encoder(self, accept: Optional[str]) -> EventEncoder:
        return EventEncoder(accept=accept)
