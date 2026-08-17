from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import importlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types

import pytest

from backend.fastapi_app.agui.auth import Principal
from backend.fastapi_app.agui.auth import principal_context
from backend.fastapi_app.agui.connection_registry import (
    ConnectionRegistry,
    ConnectionTargetPolicy,
)
from backend.fastapi_app.agui._t2s_requests import (
    canonical_text_to_sql_start_fingerprint,
    parse_text_to_sql_start,
)
from backend.fastapi_app.agui.store import (
    AGUI_EVENT_STORE_SCHEMA_VERSION,
    EventStore,
    WorkflowAdmissionConflictError,
    WorkflowClaimEnvelope,
)
from backend.fastapi_app.agui.events import (
    CustomEvent,
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
)
from backend.fastapi_app.agui.models import RunAgentInput
import backend.fastapi_app.agui.run_manager as run_manager_module
from workflow.result_identity import workflow_result_event_key


def _principal(subject: str = "owner", tenant: str = "tenant") -> Principal:
    return Principal(
        subject=subject,
        tenant_id=tenant,
        roles=frozenset({"user"}),
    )


def _install_service_connection(
    service_module,
    monkeypatch,
    tmp_path,
    principal: Principal,
) -> str:
    database_path = tmp_path / "example.db"
    database_path.touch()
    dsn = f"sqlite://{database_path}"
    policy = ConnectionTargetPolicy(
        allowed_schemes={"sqlite"},
        allowed_file_roots={tmp_path},
    )
    registry = ConnectionRegistry(policy)
    admin = Principal(
        subject="test-admin",
        tenant_id="test-admin",
        roles=frozenset({"admin", "user"}),
    )
    record = registry.register(
        admin,
        display_name="Lifecycle test database",
        dsn=dsn,
        owner_subject=principal.subject,
        tenant_id=principal.tenant_id,
    )
    monkeypatch.setattr(service_module, "_CONNECTION_TARGET_POLICY", policy)
    monkeypatch.setattr(service_module, "_CONNECTION_REGISTRY", registry)
    return str(record.connection_ref)


def _configure_required_auth(monkeypatch) -> None:
    for name in (
        "AG_UI_AUTH_TOKEN_MAP",
        "AG_UI_ADMIN_TOKEN",
        "AG_UI_USER_TOKEN",
        "AG_UI_MEMORY_ARCHIVIST_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv("AG_UI_AUTH_TOKEN", "lifecycle-test-token")


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_input(
    run_id: str,
    *,
    idempotency_key: str,
    query: str = "count orders",
    client_id: str = "untrusted-client",
    request_id: str | None = None,
) -> RunAgentInput:
    service_payload = {
        "query": query,
        "dsn": "sqlite:///example.db",
        "idempotency_key": idempotency_key,
        "client_id": client_id,
    }
    if request_id is not None:
        service_payload["__request_id"] = request_id
    return RunAgentInput(
        **{
            "threadId": f"thread-{run_id}",
            "runId": run_id,
            "state": {},
            "messages": [
                {"id": "msg-1", "role": "user", "content": query}
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {
                "service_action": "presets.text_to_sql.generate",
                "service_payload": service_payload,
            },
        }
    )


def _create_text_to_sql_run(
    store: EventStore,
    *,
    run_id: str = "run-1",
    principal: Principal | None = None,
    idempotency_key: str | None = None,
    fingerprint: str | None = None,
):
    return store.create_or_get_run(
        principal=principal or _principal(),
        run_kind="text_to_sql",
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        proposed_run_id=run_id,
        thread_id="thread-1",
    )


def _admit_workflow_run(
    store: EventStore,
    *,
    run_id: str,
    principal: Principal | None = None,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    run_incarnation: str = "inc-1",
    session_id: str | None = None,
    workflow_name: str = "text_to_sql_pipeline",
    parameters: dict[str, object] | None = None,
    work_spec: dict[str, object] | None = None,
    create_if_missing: bool = False,
):
    principal = principal or _principal()
    session_id = session_id or f"session-{run_id}"
    deadline_at_ms = int(time.time() * 1000) + 60_000
    work_spec = work_spec or {
        "spec_version": 1,
        "workflow_path": os.path.abspath(
            "workflow_pipelines/text_to_sql_pipeline.yaml"
        ),
        "parameters": parameters or {},
        "session_id": session_id,
        "client_id": None,
        "use_enhanced": True,
        "enable_telemetry": False,
        "run_incarnation": run_incarnation,
        "deadline_at_ms": deadline_at_ms,
    }
    deadline_at_ms = int(work_spec["deadline_at_ms"])
    return store.admit_workflow_run(
        run_id=run_id,
        thread_id=session_id,
        principal=principal,
        run_kind="text_to_sql",
        run_incarnation=run_incarnation,
        session_id=session_id,
        workflow_name=workflow_name,
        work_spec=work_spec,
        deadline_at_ms=deadline_at_ms,
        queue_limit=10,
        create_if_missing=create_if_missing,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
    )


def _claim_text_to_sql_worker(
    store: EventStore,
    *,
    run_id: str,
    run_incarnation: str,
    thread_id: str,
    workflow_name: str = "text_to_sql_pipeline",
    supervisor_id: str = "test-supervisor",
    worker_pid: int = 54_321,
    principal: Principal | None = None,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    parameters: dict[str, object] | None = None,
    work_spec: dict[str, object] | None = None,
) -> WorkflowClaimEnvelope:
    stored = store.get_run(run_id)
    if stored is None:
        _admit_workflow_run(
            store,
            run_id=run_id,
            principal=principal,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            run_incarnation=run_incarnation,
            session_id=thread_id,
            workflow_name=workflow_name,
            parameters=parameters or {"query": "count orders"},
            work_spec=work_spec,
            create_if_missing=True,
        )
    elif store.get_workflow_run_invocation(run_id) is None:
        _admit_workflow_run(
            store,
            run_id=run_id,
            principal=principal,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            run_incarnation=run_incarnation,
            session_id=thread_id,
            workflow_name=workflow_name,
            parameters=parameters or {"query": "count orders"},
            work_spec=work_spec,
        )
    claimed = store.claim_next_queued(
        supervisor_id,
        10,
        10,
        10,
        9_999_999_999_999,
    )
    assert claimed is not None
    assert claimed.run.run_id == run_id
    assert store.set_worker_pid(
        run_id,
        worker_pid,
        claimed.claim.supervisor_id,
        claimed.claim.attempt_generation,
    )
    return claimed.claim


def _terminal_payload(
    *,
    run_id: str = "run-1",
    incarnation: str = "inc-1",
    status: str = "failed",
    reason: str = "RESULT_RECONCILIATION_FAILED",
) -> dict[str, object]:
    if status == "failed" and reason == "RESULT_RECONCILIATION_FAILED":
        terminal = {
            "run_id": run_id,
            "status": "failed",
            "reason_code": reason,
            "sql": "",
            "generated": False,
            "approved": False,
            "executed": False,
            "dry_run": False,
            "audited": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "error": "result reconciliation failed",
            "ambiguity": None,
            "execution": {},
            "audit": {},
            "persistence": {
                "status": "error",
                "error": "result reconciliation failed",
            },
        }
    else:
        terminal = _terminal_contract(run_id=run_id, status=status)
    return {
        "run_id": run_id,
        "run_incarnation": incarnation,
        "event_key": workflow_result_event_key(run_id, incarnation),
        "status": (
            "completed"
            if status == "succeeded"
            else "cancelled"
            if status == "cancelled"
            else "failed"
        ),
        "success": status == "succeeded",
        "terminal_outcome": terminal,
    }


def _terminal_contract(
    *,
    run_id: str = "run-1",
    status: str = "succeeded",
) -> dict[str, object]:
    succeeded = status == "succeeded"
    execution_failed = status == "failed"
    reason = {
        "succeeded": "",
        "failed": "EXECUTION_FAILED",
        "cancelled": "CANCELLED",
    }[status]
    execution: dict[str, object] = {}
    audit: dict[str, object] = {}
    if succeeded or execution_failed:
        execution = {
            "success": succeeded,
            "sql_query": "SELECT 1",
            "data": [[1]] if succeeded else [],
            "columns": ["value"] if succeeded else [],
            "rows_affected": 1 if succeeded else 0,
            "execution_time_ms": 1,
            "dry_run_only": False,
            "skipped_execution": False,
            "applied_row_limit": 100,
        }
        if execution_failed:
            execution["error_message"] = "execution failed"
        audit = {"status": "logged", "log_id": "audit-1"}
    return {
        "run_id": run_id,
        "status": status,
        "reason_code": reason,
        "sql": "SELECT 1",
        "generated": True,
        "approved": succeeded or execution_failed,
        "executed": succeeded or execution_failed,
        "dry_run": False,
        "audited": succeeded or execution_failed,
        "data": [[1]] if succeeded else [],
        "columns": ["value"] if succeeded else [],
        "rows_affected": 1 if succeeded else 0,
        "error": None if succeeded else status,
        "ambiguity": None,
        "execution": execution,
        "audit": audit,
        "persistence": (
            {
                "status": "saved",
                "filename": "query.md",
                "path": "/tmp/query.md",
            }
            if succeeded
            else {"status": "not_attempted"}
        ),
    }


def test_text_to_sql_start_fingerprint_is_canonical_and_client_id_neutral():
    first = {
        "query": "  count orders  ",
        "dsn": "sqlite:///example.db",
        "max_rows": "100",
        "include_explanation": "true",
        "client_id": "attacker-selected-a",
        "idempotency_key": "opaque-key",
    }
    second = {
        "query": "count orders",
        "dsn": "sqlite:///example.db",
        "max_rows": 100,
        "include_explanation": True,
        "client_id": "attacker-selected-b",
        "idempotency_key": "another-key",
    }

    assert canonical_text_to_sql_start_fingerprint(first) == (
        canonical_text_to_sql_start_fingerprint(second)
    )


@pytest.mark.parametrize(
    "key",
    ["", " padded ", "x" * 129, "line\nbreak", "delete\x7f"],
)
def test_text_to_sql_idempotency_key_is_bounded_canonical_opaque_text(key):
    with pytest.raises(ValueError, match="idempotency_key"):
        parse_text_to_sql_start(
            {
                "query": "count orders",
                "dsn": "sqlite:///example.db",
                "idempotency_key": key,
            }
        )


@pytest.mark.asyncio
async def test_run_manager_keeps_text_to_sql_unpersisted_before_task_and_reuses_same_id(
    tmp_path,
    monkeypatch,
):
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_run_agent(input_data):
        calls.append(input_data.run_id)
        await release.wait()
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result=None,
            timestamp=1,
        )

    monkeypatch.setattr(run_manager_module, "run_agent", fake_run_agent)
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    principal = _principal()
    try:
        first = await manager.start_run(
            _run_input("run-proposed-1", idempotency_key="retry-key"),
            principal=principal,
        )
        stored_before_task = store.get_run("run-proposed-1")
        assert stored_before_task is None

        second = await manager.start_run(
            _run_input(
                "run-proposed-2",
                idempotency_key="retry-key",
                client_id="different-untrusted-client",
            ),
            principal=principal,
        )
        assert second is first
        assert second.run_id == "run-proposed-1"
        assert store.get_run("run-proposed-2") is None
        await asyncio.sleep(0)
        assert calls == ["run-proposed-1"]
    finally:
        release.set()
        if first.task is not None:
            await first.task
        store.close()


@pytest.mark.asyncio
async def test_text_to_sql_cancel_before_outer_task_leaves_no_partial_admission(
    tmp_path,
    monkeypatch,
):
    entered = asyncio.Event()

    async def fake_run_agent(_input_data):
        entered.set()
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(run_manager_module, "run_agent", fake_run_agent)
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    try:
        info = await manager.start_run(
            _run_input("run-cancel-before-admission", idempotency_key="cancel-key"),
            principal=_principal(),
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert store.get_run(info.run_id) is None
        assert info.task is not None
        info.task.cancel()
        await info.task
        assert store.get_run(info.run_id) is None
        assert store.get_workflow_run_invocation(info.run_id) is None
        assert store.load_work_spec(info.run_id) is None
    finally:
        manager.close()
        store.close()


@pytest.mark.asyncio
async def test_run_manager_rejects_key_conflict_before_second_task(
    tmp_path,
    monkeypatch,
):
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_run_agent(input_data):
        calls.append(input_data.run_id)
        await release.wait()
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result=None,
            timestamp=1,
        )

    monkeypatch.setattr(run_manager_module, "run_agent", fake_run_agent)
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    first = await manager.start_run(
        _run_input("run-proposed-1", idempotency_key="retry-key"),
        principal=_principal(),
    )
    try:
        with pytest.raises(ValueError, match="idempotency_key"):
            await manager.start_run(
                _run_input(
                    "run-proposed-2",
                    idempotency_key="retry-key",
                    query="different request",
                ),
                principal=_principal(),
            )
        await asyncio.sleep(0)
        assert calls == ["run-proposed-1"]
    finally:
        release.set()
        if first.task is not None:
            await first.task
        store.close()


@pytest.mark.asyncio
async def test_retry_cursor_projection_is_ordered_with_pending_event_publication(
    tmp_path,
):
    run_id = "run-ordering"
    retry_input = _run_input(
        "retry-proposal",
        idempotency_key="ordering-key",
        request_id="current-request",
    )
    payload = retry_input.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    stored, created = store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key="ordering-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id=run_id,
        thread_id="thread-ordering",
    )
    assert created is True
    assert store.reserve_workflow_run(
        run_id,
        "inc-ordering",
        "thread-ordering",
        "text_to_sql_pipeline",
    )
    assert store.set_worker_pid(run_id, os.getpid())

    manager = run_manager_module.RunManager(store)
    info = manager._info_from_stored(store.get_run(run_id) or stored)
    manager._runs[run_id] = info
    pending_event = CustomEvent(
        type=EventType.CUSTOM,
        name="workflow.progress",
        value={"run_id": run_id, "status": "running", "step": 1},
        timestamp=1,
    )

    await manager._lock.acquire()
    retry_task = asyncio.create_task(
        manager.start_run_request_view(retry_input, principal=_principal())
    )
    await asyncio.sleep(0)
    publish_task = asyncio.create_task(manager._publish_event(run_id, pending_event))
    await asyncio.sleep(0)
    manager._lock.release()

    stream = None
    try:
        retried, replay_view = await asyncio.wait_for(retry_task, timeout=2)
        await asyncio.wait_for(publish_task, timeout=2)

        assert retried is info
        assert replay_view is not None
        assert replay_view.after == 0
        assert [seq for seq, _event in info.events] == [1, 2]
        assert [event.seq for event in store.list_after(run_id, 0)] == [1, 2]

        stream = manager.stream_live(
            run_id,
            request_view=replay_view,
        )
        first = await asyncio.wait_for(anext(stream), timeout=1)
        second = await asyncio.wait_for(anext(stream), timeout=1)
        assert isinstance(first, CustomEvent)
        assert first.name == "service.result"
        assert first.value["__request_id"] == "current-request"
        assert second == pending_event
    finally:
        if manager._lock.locked():
            manager._lock.release()
        if stream is not None:
            await stream.aclose()
        if info.task is not None and not info.task.done():
            info.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await info.task
        store.close()


@pytest.mark.asyncio
async def test_concurrent_pre_admission_retries_join_one_in_memory_task(
    tmp_path,
    monkeypatch,
):
    store = EventStore(str(tmp_path / "events.db"))
    runner_started = asyncio.Event()
    release_runner = asyncio.Event()
    runner_calls: list[str] = []

    async def fake_run_agent(input_data):
        runner_calls.append(input_data.run_id)
        runner_started.set()
        await release_runner.wait()
        deadline_at_ms = int(time.time() * 1000) + 60_000
        work_spec = {
            "spec_version": 1,
            "workflow_path": "/srv/workflows/text_to_sql_pipeline.yaml",
            "parameters": {},
            "session_id": input_data.thread_id,
            "client_id": None,
            "use_enhanced": True,
            "enable_telemetry": False,
            "run_incarnation": "inc-concurrent-retry",
            "deadline_at_ms": deadline_at_ms,
        }
        payload = input_data.forwarded_props["service_payload"]
        stored, admitted = _admit_workflow_run(
            store,
            run_id=input_data.run_id,
            principal=_principal(),
            idempotency_key="concurrent-retry-key",
            request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
            run_incarnation="inc-concurrent-retry",
            session_id=input_data.thread_id,
            workflow_name="text_to_sql_pipeline",
            work_spec=work_spec,
            create_if_missing=True,
        )
        assert admitted is True
        terminal = _terminal_payload(
            run_id=stored.run_id,
            incarnation="inc-concurrent-retry",
            status="succeeded",
            reason="",
        )
        store.finalize_run_with_event(stored.run_id, terminal)
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result=terminal,
            timestamp=1,
        )

    monkeypatch.setattr(run_manager_module, "run_agent", fake_run_agent)
    manager = run_manager_module.RunManager(store)
    info = None
    try:
        info, original_view = await manager.start_run_request_view(
            _run_input(
                "original-run",
                idempotency_key="concurrent-retry-key",
                request_id="original-request",
            ),
            principal=_principal(),
        )
        await asyncio.wait_for(runner_started.wait(), timeout=1)
        assert store.get_run(info.run_id) is None

        retry_a, retry_b = await asyncio.gather(
            manager.start_run_request_view(
                _run_input("proposal-a", idempotency_key="concurrent-retry-key", request_id="request-a"),
                principal=_principal(),
            ),
            manager.start_run_request_view(
                _run_input("proposal-b", idempotency_key="concurrent-retry-key", request_id="request-b"),
                principal=_principal(),
            ),
        )
        assert retry_a[0] is info
        assert retry_b[0] is info
        assert original_view is not None
        assert retry_a[1] is not None
        assert retry_b[1] is not None
        assert {retry_a[1].request_id, retry_b[1].request_id} == {"request-a", "request-b"}
        assert store.get_run(info.run_id) is None

        release_runner.set()
        await asyncio.wait_for(info.task, timeout=2)
        stored = store.get_run(info.run_id)
        assert stored is not None
        assert stored.status == "succeeded"
        assert store.get_workflow_run_invocation(info.run_id) is not None
        assert runner_calls == ["original-run"]
    finally:
        release_runner.set()
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
            await info.task
        manager.close()
        store.close()

@pytest.mark.asyncio
async def test_request_view_replaces_stale_terminal_loser_with_durable_winner(
    tmp_path,
):
    input_data = _run_input(
        "outer-run",
        idempotency_key="request-winner-key",
        request_id="request-winner",
    )
    payload = input_data.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    stored, created = store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key="request-winner-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id="outer-run",
        thread_id=input_data.thread_id,
    )
    assert created is True
    assert store.reserve_workflow_run(
        "outer-run",
        "inc-request-winner",
        input_data.thread_id,
        "text_to_sql_pipeline",
    )
    manager = run_manager_module.RunManager(store)
    info = manager._info_from_stored(store.get_run("outer-run") or stored)
    manager._runs["outer-run"] = info
    stale_result = _terminal_payload(
        run_id="outer-run",
        incarnation="inc-request-winner",
        status="failed",
        reason="EXECUTION_FAILED",
    )
    await manager._publish_event(
        "outer-run",
        CustomEvent(
            type=EventType.CUSTOM,
            name="service.result",
            value={
                "action": "presets.text_to_sql.generate",
                "ok": True,
                "data": {"run_id": "outer-run"},
                "__request_id": "request-winner",
            },
            timestamp=1,
        ),
    )
    await manager._publish_event(
        "outer-run",
        CustomEvent(
            type=EventType.CUSTOM,
            name="workflow.result",
            value=stale_result,
            timestamp=2,
        ),
    )
    await manager._publish_event(
        "outer-run",
        RunErrorEvent(
            type=EventType.RUN_ERROR,
            message="stale loser",
            code="text_to_sql_failed",
            timestamp=3,
        ),
    )
    view = run_manager_module.RequestReplayView(
        run_id="outer-run",
        request_id="request-winner",
        after=0,
    )

    async def collect():
        return [
            event
            async for event in manager.stream_live(
                "outer-run",
                request_view=view,
            )
        ]

    collector = asyncio.create_task(collect())
    try:
        for _attempt in range(20):
            if manager.subscriber_count("outer-run") == 1:
                break
            await asyncio.sleep(0)
        assert manager.subscriber_count("outer-run") == 1

        winner = _terminal_payload(
            run_id="outer-run",
            incarnation="inc-request-winner",
            status="succeeded",
            reason="COMPLETED",
        )
        store.finalize_run_with_event("outer-run", winner)
        admission = run_manager_module._text_to_sql_admission(input_data)
        assert admission is not None
        async with manager._lock:
            before = len(info.events)
            terminal_stored = store.get_run("outer-run")
            assert terminal_stored is not None
            manager._append_terminal_retry_projection(
                info,
                terminal_stored,
                input_data,
                admission,
                include_start=False,
            )
            published = info.events[before:]
            info.status = run_manager_module.RunStatus.FINISHED
            subscribers = list(info.subscribers)
            info.subscribers.clear()
        for queue in subscribers:
            for item in published:
                await queue.put(item)
            await queue.put(None)

        events = await asyncio.wait_for(collector, timeout=2)
        workflow_results = [
            event
            for event in events
            if isinstance(event, CustomEvent) and event.name == "workflow.result"
        ]
        terminals = [
            event
            for event in events
            if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
        ]
        assert len(workflow_results) == 1
        assert workflow_results[0].value == winner
        assert [event.type for event in terminals] == [EventType.RUN_FINISHED]
        assert any(
            isinstance(event, CustomEvent)
            and event.name == "workflow.result"
            and event.value
            == {**winner, "result_seq": terminal_stored.result_seq}
            for _seq, event in info.events
        )
        assert any(
            event.type == EventType.RUN_FINISHED
            for _seq, event in info.events
        )
    finally:
        if not collector.done():
            collector.cancel()
            with pytest.raises(asyncio.CancelledError):
                await collector
        store.close()


def test_terminal_retry_projection_deduplicates_by_durable_result_identity(
    tmp_path,
):
    input_data = _run_input(
        "outer-run",
        idempotency_key="projection-identity-key",
    )
    payload = input_data.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    _admit_workflow_run(
        store,
        run_id="outer-run",
        idempotency_key="projection-identity-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        run_incarnation="inc-projection",
        session_id=input_data.thread_id,
        create_if_missing=True,
    )
    primary = _terminal_payload(
        run_id="outer-run",
        incarnation="inc-projection",
        status="cancelled",
        reason="CANCELLED",
    )
    store.finalize_run_with_event("outer-run", primary)
    stored = store.get_run("outer-run")
    assert stored is not None and stored.result_seq is not None
    manager = run_manager_module.RunManager(store)
    info = manager._info_from_stored(stored)
    info.events.append(
        (
            stored.result_seq + 1,
            CustomEvent(
                type=EventType.CUSTOM,
                name="workflow.result",
                value={
                    **primary,
                    "result_seq": stored.result_seq,
                    "transport_report": {"persisted": True},
                },
                timestamp=1,
            ),
        )
    )
    admission = run_manager_module._text_to_sql_admission(input_data)
    assert admission is not None
    try:
        manager._append_terminal_retry_projection(
            info,
            stored,
            input_data,
            admission,
            include_start=False,
        )

        workflow_results = [
            event
            for _seq, event in info.events
            if isinstance(event, CustomEvent) and event.name == "workflow.result"
        ]
        assert len(workflow_results) == 1
        terminals = [
            event
            for _seq, event in info.events
            if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
        ]
        assert len(terminals) == 1
    finally:
        store.close()


def test_terminal_projection_identity_requires_authoritative_result_seq():
    primary = _terminal_payload(
        run_id="outer-run",
        incarnation="inc-projection",
        status="cancelled",
        reason="CANCELLED",
    )
    durable_projection = {**primary, "result_seq": 41}
    another_sequence = CustomEvent(
        type=EventType.CUSTOM,
        name="workflow.result",
        value={**primary, "result_seq": 42},
        timestamp=1,
    )
    same_sequence = CustomEvent(
        type=EventType.CUSTOM,
        name="workflow.result",
        value=durable_projection,
        timestamp=1,
    )

    assert not run_manager_module._projects_durable_workflow_result(
        another_sequence,
        durable_projection,
        result_seq=41,
    )
    assert run_manager_module._projects_durable_workflow_result(
        same_sequence,
        durable_projection,
        result_seq=41,
    )


@pytest.mark.asyncio
async def test_request_view_preserves_server_restarted_terminal_without_result(
    tmp_path,
):
    retry = _run_input(
        "retry-proposal",
        idempotency_key="server-restarted-view-key",
        request_id="restart-request",
    )
    payload = retry.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key="server-restarted-view-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id="original-run",
        thread_id="original-thread",
    )
    assert store.transition_run("original-run", {"pending"}, "running")
    assert store.transition_run(
        "original-run",
        {"running"},
        "failed",
        reason="SERVER_RESTARTED",
    )
    manager = run_manager_module.RunManager(store)
    try:
        info, view = await manager.start_run_request_view(
            retry,
            principal=_principal(),
        )
        assert info.task is None
        assert view is not None
        events = [
            event
            async for event in manager.stream_live(
                info.run_id,
                request_view=view,
            )
        ]
        errors = [event for event in events if event.type == EventType.RUN_ERROR]
        assert len(errors) == 1
        assert errors[0].code == "text_to_sql_server_restarted"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_outer_success_after_admission_follows_durable_winner(
    tmp_path,
    monkeypatch,
):
    async def fake_run_agent(input_data):
        _admit_workflow_run(
            store,
            run_id=input_data.run_id,
            session_id=input_data.thread_id,
            create_if_missing=True,
        )
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            result={"early_only": True},
            timestamp=1,
        )

    monkeypatch.setattr(run_manager_module, "run_agent", fake_run_agent)
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    try:
        info = await manager.start_run(
            _run_input("run-no-primary", idempotency_key="no-primary-key"),
            principal=_principal(),
        )
        assert info.task is not None
        await info.task

        stored = store.get_run("run-no-primary")
        assert stored is not None
        assert stored.status == "queued"
        follower = info.task
        assert follower is not None
        store.finalize_run_with_event(
            "run-no-primary",
            _terminal_payload(
                run_id="run-no-primary",
                incarnation="inc-1",
                status="succeeded",
                reason="COMPLETED",
            ),
        )
        await asyncio.wait_for(follower, timeout=2)
        assert [
            event.type
            for _seq, event in info.events
            if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
        ] == [EventType.RUN_FINISHED]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_pre_invocation_text_to_sql_failure_leaves_no_partial_admission(
    tmp_path,
    monkeypatch,
):
    async def fail_before_invocation(_input_data):
        raise RuntimeError("start validation failed")
        yield

    monkeypatch.setattr(run_manager_module, "run_agent", fail_before_invocation)
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    try:
        info = await manager.start_run(
            _run_input("run-start-failed", idempotency_key="start-failed-key"),
            principal=_principal(),
        )
        assert info.task is not None
        await info.task

        stored = store.get_run("run-start-failed")
        assert stored is None
        assert store.get_workflow_run_invocation("run-start-failed") is None
        assert store.load_work_spec("run-start-failed") is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_pre_invocation_text_to_sql_cancel_leaves_no_partial_admission(
    tmp_path,
    monkeypatch,
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_before_invocation(_input_data):
        entered.set()
        await release.wait()
        yield

    monkeypatch.setattr(run_manager_module, "run_agent", wait_before_invocation)
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    try:
        info = await manager.start_run(
            _run_input("run-start-cancelled", idempotency_key="start-cancelled-key"),
            principal=_principal(),
        )
        await entered.wait()

        assert await manager.cancel("run-start-cancelled") is True
        stored = store.get_run("run-start-cancelled")
        assert stored is None
        assert store.get_workflow_run_invocation("run-start-cancelled") is None
        assert store.load_work_spec("run-start-cancelled") is None
        assert info.status == run_manager_module.RunStatus.CANCELLED
        assert info.task is not None and info.task.done()
    finally:
        release.set()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_mode", ["error", "handled_cancel"])
async def test_post_invocation_outer_exit_adopts_durable_follower_until_winner(
    tmp_path,
    monkeypatch,
    exit_mode,
):
    store = EventStore(str(tmp_path / f"events-{exit_mode}.db"))
    invocation_ready = asyncio.Event()
    release_error = asyncio.Event()

    async def exit_after_invocation(input_data):
        _claim_text_to_sql_worker(
            store,
            run_id=input_data.run_id,
            run_incarnation="inc-adopted",
            thread_id=input_data.thread_id,
        )
        invocation_ready.set()
        if exit_mode == "error":
            await release_error.wait()
            raise RuntimeError("outer transport failed")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return
        if False:
            yield

    monkeypatch.setattr(run_manager_module, "run_agent", exit_after_invocation)
    manager = run_manager_module.RunManager(store)
    info = None
    try:
        info = await manager.start_run(
            _run_input(
                "outer-run",
                idempotency_key=f"adopt-{exit_mode}-key",
            ),
            principal=_principal(),
        )
        await asyncio.wait_for(invocation_ready.wait(), timeout=1)
        outer_task = info.task
        assert outer_task is not None
        queue = asyncio.Queue()
        async with manager._lock:
            info.subscribers.add(queue)

        if exit_mode == "error":
            release_error.set()
            await asyncio.wait_for(outer_task, timeout=1)
        else:
            assert await manager.cancel("outer-run") is False
            await asyncio.wait_for(outer_task, timeout=1)

        observer_task = info.task
        assert observer_task is not None
        assert observer_task is not outer_task
        assert not observer_task.done()
        assert info.durable_follower is True
        assert info.cancel_requested is False
        assert info.durable_cancel_attempted is False
        assert queue in info.subscribers
        assert queue.empty()
        assert not any(
            event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
            for _seq, event in info.events
        )
        stored = store.get_run("outer-run")
        assert stored is not None and stored.status == "running"
        assert stored.result_seq is None

        store.finalize_run_with_event(
            "outer-run",
            _terminal_payload(
                run_id="outer-run",
                incarnation="inc-adopted",
                status="succeeded",
                reason="COMPLETED",
            ),
        )
        await asyncio.wait_for(observer_task, timeout=2)

        published = []
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=1)
            if item is None:
                break
            published.append(item[1])
        assert [
            (
                event.type,
                event.name if isinstance(event, CustomEvent) else None,
            )
            for event in published
        ] == [
            (EventType.CUSTOM, "workflow.result"),
            (EventType.RUN_FINISHED, None),
        ]
        assert info.status == run_manager_module.RunStatus.FINISHED
    finally:
        release_error.set()
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await info.task
        store.close()


@pytest.mark.asyncio
async def test_adopted_follower_recovers_from_transient_store_read(
    tmp_path,
    monkeypatch,
):
    store = EventStore(str(tmp_path / "events.db"))
    invocation_ready = asyncio.Event()
    release_outer = asyncio.Event()
    transient_seen = asyncio.Event()

    async def exit_after_invocation(input_data):
        _claim_text_to_sql_worker(
            store,
            run_id=input_data.run_id,
            run_incarnation="inc-transient-follower",
            thread_id=input_data.thread_id,
        )
        invocation_ready.set()
        await release_outer.wait()
        if False:
            yield

    monkeypatch.setattr(run_manager_module, "run_agent", exit_after_invocation)
    manager = run_manager_module.RunManager(store)
    info = None
    original_get_run = store.get_run
    failed_once = False
    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="transient-follower-key"),
            principal=_principal(),
        )
        await asyncio.wait_for(invocation_ready.wait(), timeout=1)
        outer_task = info.task
        assert outer_task is not None
        queue = asyncio.Queue()
        async with manager._lock:
            info.subscribers.add(queue)

        # The durable-follower read runs inside RunManager._call_store's
        # executor thread (T2), so asyncio.current_task() cannot identify the
        # caller from within store.get_run itself (no running loop there).
        # Intercept at _call_store instead, on the event loop, before the
        # executor hop, where task identity is meaningful; raise directly to
        # reproduce the same observable failure as a transient get_run error
        # inside _read_durable_follow_state.
        original_call_store = run_manager_module.RunManager._call_store

        async def flaky_call_store(self, func, *args, **kwargs):
            nonlocal failed_once
            if (
                not failed_once
                and func == self._read_durable_follow_state
                and info is not None
                and info.durable_follower
                and asyncio.current_task() is info.task
            ):
                failed_once = True
                transient_seen.set()
                raise RuntimeError("transient durable read failure")
            return await original_call_store(self, func, *args, **kwargs)

        monkeypatch.setattr(
            run_manager_module.RunManager, "_call_store", flaky_call_store
        )
        release_outer.set()
        await asyncio.wait_for(outer_task, timeout=1)
        await asyncio.wait_for(transient_seen.wait(), timeout=1)

        observer_task = info.task
        assert observer_task is not None and observer_task is not outer_task
        assert not observer_task.done()
        assert queue in info.subscribers
        assert queue.empty()
        assert original_get_run("outer-run").status == "running"

        store.finalize_run_with_event(
            "outer-run",
            _terminal_payload(
                run_id="outer-run",
                incarnation="inc-transient-follower",
                status="succeeded",
                reason="COMPLETED",
            ),
        )
        await asyncio.wait_for(observer_task, timeout=2)

        published = []
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=1)
            if item is None:
                break
            published.append(item[1])
        assert [event.type for event in published] == [
            EventType.CUSTOM,
            EventType.RUN_FINISHED,
        ]
        assert info.status == run_manager_module.RunStatus.FINISHED
    finally:
        release_outer.set()
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await info.task
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_stage", "reserve_invocation"),
    [
        pytest.param("initial", True, id="initial-read"),
        pytest.param("locked", True, id="locked-read"),
        pytest.param("initial", False, id="initial-read-no-invocation"),
    ],
)
async def test_outer_finalizer_read_failure_adopts_durable_follower(
    tmp_path,
    monkeypatch,
    read_stage,
    reserve_invocation,
):
    store = EventStore(
        str(tmp_path / f"events-{read_stage}-{reserve_invocation}.db")
    )
    runner_ready = asyncio.Event()
    release_outer = asyncio.Event()
    read_failed = asyncio.Event()

    async def exit_after_optional_invocation(input_data):
        if reserve_invocation:
            _claim_text_to_sql_worker(
                store,
                run_id=input_data.run_id,
                run_incarnation="inc-finalizer-read",
                thread_id=input_data.thread_id,
            )
        runner_ready.set()
        await release_outer.wait()
        if False:
            yield

    monkeypatch.setattr(
        run_manager_module,
        "run_agent",
        exit_after_optional_invocation,
    )
    manager = run_manager_module.RunManager(store)
    info = None
    original_get_run = store.get_run
    outer_read_count = 0
    try:
        info = await manager.start_run(
            _run_input(
                "outer-run",
                idempotency_key=(
                    f"finalizer-{read_stage}-{reserve_invocation}-key"
                ),
            ),
            principal=_principal(),
        )
        await asyncio.wait_for(runner_ready.wait(), timeout=1)
        outer_task = info.task
        assert outer_task is not None
        queue = asyncio.Queue()
        async with manager._lock:
            info.subscribers.add(queue)

        target_read = 1 if read_stage == "initial" else 2

        # _finalize_text_to_sql_outer_run's get_run reads now go through
        # RunManager._call_store's executor thread (T2), where
        # asyncio.current_task() has no running loop to identify the caller.
        # Intercept at _call_store instead, on the event loop, before the
        # executor hop, matching only get_run calls (get_workflow_run_invocation
        # calls must not be counted) so the target_read numbering is preserved.
        original_call_store = run_manager_module.RunManager._call_store

        async def flaky_call_store(self, func, *args, **kwargs):
            nonlocal outer_read_count
            if func == self._store.get_run and asyncio.current_task() in {
                outer_task,
                info.finalization_task,
            }:
                outer_read_count += 1
                if outer_read_count == target_read:
                    read_failed.set()
                    raise RuntimeError("transient outer finalizer read failure")
            return await original_call_store(self, func, *args, **kwargs)

        monkeypatch.setattr(
            run_manager_module.RunManager, "_call_store", flaky_call_store
        )
        release_outer.set()
        await asyncio.wait_for(outer_task, timeout=1)
        await asyncio.wait_for(read_failed.wait(), timeout=1)

        observer_task = info.task
        assert observer_task is not None and observer_task is not outer_task
        assert info.durable_follower is True
        assert queue in info.subscribers or observer_task.done()

        if reserve_invocation:
            assert not observer_task.done()
            assert queue in info.subscribers
            assert queue.empty()
            assert original_get_run("outer-run").status == "running"
            store.finalize_run_with_event(
                "outer-run",
                _terminal_payload(
                    run_id="outer-run",
                    incarnation="inc-finalizer-read",
                    status="succeeded",
                    reason="COMPLETED",
                ),
            )

            await asyncio.wait_for(observer_task, timeout=2)
        else:
            await asyncio.wait_for(observer_task, timeout=2)
            assert original_get_run("outer-run") is None
            return
        stored = original_get_run("outer-run")
        assert stored is not None
        assert stored.status == ("succeeded" if reserve_invocation else "failed")
        assert stored.result_seq is not None
        if not reserve_invocation:
            primary = store.get_event("outer-run", stored.result_seq)
            assert primary is not None
            assert primary.payload["terminal_outcome"]["reason_code"] == (
                "MANDATORY_STEP_NOT_COMPLETED"
            )

        published = []
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=1)
            if item is None:
                break
            published.append(item[1])
        assert [event.type for event in published] == [
            EventType.CUSTOM,
            (
                EventType.RUN_FINISHED
                if reserve_invocation
                else EventType.RUN_ERROR
            ),
        ]
    finally:
        release_outer.set()
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await info.task
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("finalizer_mode", ["complete", "raise", "cancel_child"])
async def test_repeated_cancel_cannot_interrupt_text_to_sql_finalizer_adoption(
    tmp_path,
    monkeypatch,
    finalizer_mode,
):
    store = EventStore(str(tmp_path / f"events-{finalizer_mode}.db"))
    invocation_ready = asyncio.Event()
    finalizer_entered = asyncio.Event()
    release_finalizer = asyncio.Event()

    async def wait_after_invocation(input_data):
        _claim_text_to_sql_worker(
            store,
            run_id=input_data.run_id,
            run_incarnation="inc-finalizer-shield",
            thread_id=input_data.thread_id,
        )
        invocation_ready.set()
        await asyncio.Event().wait()
        if False:
            yield

    monkeypatch.setattr(run_manager_module, "run_agent", wait_after_invocation)
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.01)
    manager = run_manager_module.RunManager(store)
    original_finalizer = manager._finalize_text_to_sql_outer_run

    async def blocked_finalizer(*args, **kwargs):
        finalizer_entered.set()
        await release_finalizer.wait()
        if finalizer_mode == "raise":
            raise RuntimeError("finalizer failed after cancellation")
        if finalizer_mode == "cancel_child":
            raise asyncio.CancelledError
        await original_finalizer(*args, **kwargs)

    monkeypatch.setattr(
        manager,
        "_finalize_text_to_sql_outer_run",
        blocked_finalizer,
    )
    info = None
    try:
        input_data = _run_input(
            "outer-run",
            idempotency_key=f"finalizer-shield-{finalizer_mode}",
        )
        info = await manager.start_run(input_data, principal=_principal())
        await asyncio.wait_for(invocation_ready.wait(), timeout=1)
        outer_task = info.task
        assert outer_task is not None
        queue = asyncio.Queue()
        async with manager._lock:
            info.subscribers.add(queue)

        first_cancel = asyncio.create_task(manager.cancel("outer-run"))
        await asyncio.wait_for(finalizer_entered.wait(), timeout=1)
        assert await asyncio.wait_for(first_cancel, timeout=1) is False
        assert info.cancel_requested is False

        assert await asyncio.wait_for(manager.cancel("outer-run"), timeout=1) is False
        assert info.task is outer_task
        assert queue in info.subscribers
        assert store.get_run("outer-run").status == "running"

        release_finalizer.set()
        await asyncio.wait_for(outer_task, timeout=1)

        observer_task = info.task
        assert observer_task is not None
        assert observer_task is not outer_task
        assert info.durable_follower is True
        assert not observer_task.done()
        assert queue in info.subscribers

        store.finalize_run_with_event(
            "outer-run",
            _terminal_payload(
                run_id="outer-run",
                incarnation="inc-finalizer-shield",
                status="succeeded",
                reason="COMPLETED",
            ),
        )
        await asyncio.wait_for(observer_task, timeout=2)

        published = []
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=1)
            if item is None:
                break
            published.append(item[1])
        assert [event.type for event in published] == [
            EventType.CUSTOM,
            EventType.RUN_FINISHED,
        ]
    finally:
        release_finalizer.set()
        if info is not None:
            tasks = {info.task, getattr(info, "finalization_task", None)}
            for task in tasks:
                if task is not None and not task.done():
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
        store.close()


@pytest.mark.asyncio
async def test_post_invocation_workflow_mismatch_persists_typed_failure(
    tmp_path,
    monkeypatch,
):
    store = EventStore(str(tmp_path / "events.db"))
    invocation_ready = asyncio.Event()
    release_outer = asyncio.Event()

    async def exit_after_mismatched_invocation(input_data):
        payload = input_data.forwarded_props["service_payload"]
        _claim_text_to_sql_worker(
            store,
            run_id=input_data.run_id,
            run_incarnation="inc-mismatched-workflow",
            thread_id=input_data.thread_id,
            workflow_name="different_workflow",
            idempotency_key=payload["idempotency_key"],
            request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        )
        invocation_ready.set()
        await release_outer.wait()
        if False:
            yield

    monkeypatch.setattr(
        run_manager_module,
        "run_agent",
        exit_after_mismatched_invocation,
    )
    manager = run_manager_module.RunManager(store)
    info = None
    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="mismatched-workflow-key"),
            principal=_principal(),
        )
        await asyncio.wait_for(invocation_ready.wait(), timeout=1)
        outer_task = info.task
        assert outer_task is not None
        queue = asyncio.Queue()
        async with manager._lock:
            info.subscribers.add(queue)

        release_outer.set()
        await asyncio.wait_for(outer_task, timeout=1)

        stored = store.get_run("outer-run")
        assert stored is not None
        assert stored.status == "failed"
        assert stored.result_seq is not None
        primary = store.get_event("outer-run", stored.result_seq)
        assert primary is not None
        assert primary.event_type == "WORKFLOW_RESULT"
        assert primary.run_incarnation == "inc-mismatched-workflow"
        assert primary.payload["terminal_outcome"]["status"] == "failed"
        assert primary.payload["terminal_outcome"]["reason_code"] == (
            "MANDATORY_STEP_NOT_COMPLETED"
        )
        assert info.task is outer_task and outer_task.done()
        assert info.durable_follower is False

        published = []
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=1)
            if item is None:
                break
            published.append(item[1])
        assert [event.type for event in published] == [
            EventType.CUSTOM,
            EventType.RUN_ERROR,
        ]
        assert info.status == run_manager_module.RunStatus.ERRORED
    finally:
        release_outer.set()
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await info.task
        store.close()


@pytest.mark.asyncio
async def test_immediate_cancel_after_start_handshake_leaves_no_partial_admission(
    tmp_path,
    monkeypatch,
):
    never = asyncio.Event()

    async def no_initial_yield(_input_data):
        await never.wait()
        yield

    monkeypatch.setattr(run_manager_module, "run_agent", no_initial_yield)
    monkeypatch.setattr(
        run_manager_module.uuid,
        "uuid4",
        lambda: types.SimpleNamespace(
            hex="fc373f933a00425885e6624119768858",
        ),
    )
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    try:
        info = await manager.start_run(
            _run_input("run-immediate-cancel", idempotency_key="immediate-key"),
            principal=_principal(),
        )

        assert info.lifecycle_started.is_set()
        assert await manager.cancel("run-immediate-cancel") is True
        stored = store.get_run("run-immediate-cancel")
        assert stored is None
        assert store.get_workflow_run_invocation("run-immediate-cancel") is None
        assert store.load_work_spec("run-immediate-cancel") is None
        assert info.status == run_manager_module.RunStatus.CANCELLED
    finally:
        never.set()
        store.close()


@pytest.mark.asyncio
async def test_cancel_timeout_reopens_text_to_sql_cancel_attempt(
    tmp_path,
    monkeypatch,
):
    release = asyncio.Event()
    cancellation_count = 0
    claim: WorkflowClaimEnvelope | None = None

    async def stubborn_follower(input_data):
        nonlocal cancellation_count, claim
        payload = input_data.forwarded_props["service_payload"]
        claim = _claim_text_to_sql_worker(
            store,
            run_id=input_data.run_id,
            run_incarnation="inc-stubborn",
            thread_id=input_data.thread_id,
            idempotency_key=payload["idempotency_key"],
            request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        )
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_count += 1
            await release.wait()
        if False:
            yield

    monkeypatch.setattr(run_manager_module, "run_agent", stubborn_follower)
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.01)
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    try:
        info = await manager.start_run(
            _run_input("run-stubborn", idempotency_key="stubborn-key"),
            principal=_principal(),
        )

        assert await manager.cancel("run-stubborn") is False
        assert info.task is not None and not info.task.done()
        assert cancellation_count == 1
        assert info.cancel_requested is False
        assert await manager.cancel("run-stubborn") is False
        assert cancellation_count == 1
        assert info.durable_follower is True
        assert claim is not None
        release.set()
        store.finalize_run_with_event(
            "run-stubborn",
            _terminal_payload(
                run_id="run-stubborn",
                incarnation="inc-stubborn",
                status="cancelled",
                reason="CANCELLED",
            ),
            expected_supervisor_id=claim.supervisor_id,
            expected_attempt_generation=claim.attempt_generation,
        )
        await asyncio.sleep(0)
        stored = store.get_run("run-stubborn")
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.result_seq is not None
        task = info.task
        assert task is not None
        await asyncio.wait_for(task, timeout=2)
        assert task.done()
        assert info.status == run_manager_module.RunStatus.CANCELLED
    finally:
        release.set()
        if info.task is not None:
            task = info.task
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1)
            except asyncio.CancelledError:
                pass
        store.close()


@pytest.mark.asyncio
async def test_stale_transport_terminal_does_not_block_orphan_cancel(
    tmp_path,
    monkeypatch,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    cancellation_count = 0

    async def running_after_stale_terminal(input_data):
        nonlocal cancellation_count
        _claim_text_to_sql_worker(
            store,
            run_id=input_data.run_id,
            run_incarnation="inc-stale-terminal",
            thread_id=input_data.thread_id,
        )
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_count += 1
            await release.wait()
        if False:
            yield

    monkeypatch.setattr(
        run_manager_module,
        "run_agent",
        running_after_stale_terminal,
    )
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.01)
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    info = None
    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="stale-terminal-key"),
            principal=_principal(),
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        await manager._publish_event(
            "outer-run",
            RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                thread_id="outer-thread",
                run_id="outer-run",
                result={"stale": True},
                timestamp=1,
            ),
        )

        assert await manager.cancel_if_orphaned("outer-run") is False
        assert cancellation_count == 1
        assert info.cancel_requested is False
        stored = store.get_run("outer-run")
        assert stored is not None and stored.status == "running"
    finally:
        release.set()
        if info is not None and info.task is not None:
            task = info.task
            await task
            if info.task is not task and not info.task.done():
                info.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await info.task
        store.close()


def test_early_text_to_sql_start_service_result_is_not_terminal():
    payload = {
        "name": "service.result",
        "value": {
            "action": "presets.text_to_sql.generate",
            "ok": True,
            "data": {"run_id": "run-1"},
        },
    }
    assert (
        run_manager_module.is_terminal_event_payload(EventType.CUSTOM.value, payload)
        is False
    )
    assert run_manager_module.is_terminal_event_payload(
        EventType.CUSTOM.value,
        {"name": "service.result", "value": {"action": "utils.time.now"}},
    ) is True


@pytest.mark.asyncio
async def test_text_to_sql_runner_starts_and_follows_one_outer_id(monkeypatch):
    import backend.fastapi_app.agui.runner as runner_module
    import backend.fastapi_app.agui.service as service_module

    calls: list[tuple[str, dict[str, object], object]] = []
    status_count = 0

    def fake_handle(
        action,
        payload,
        principal=None,
        *,
        transport_context=None,
    ):
        nonlocal status_count
        calls.append((action, dict(payload), transport_context))
        if action == "presets.text_to_sql.generate":
            assert transport_context.run_id == "outer-run"
            return {"run_id": "outer-run", "workflow_name": "text_to_sql_pipeline"}
        if action == "workflows.status":
            status_count += 1
            if status_count == 1:
                return {
                    "status": {
                        "run_id": "outer-run",
                        "status": "running",
                        "progress_percentage": 50.0,
                        "result_seq": None,
                    }
                }
            return {
                "status": {
                    "run_id": "outer-run",
                    "status": "completed",
                    "progress_percentage": 100.0,
                    "result_seq": 7,
                }
            }
        if action == "workflows.result":
            return {
                "status": "completed",
                "success": True,
                "terminal_outcome": {
                    "run_id": "outer-run",
                    "status": "succeeded",
                    "reason_code": "COMPLETED",
                },
            }
        raise AssertionError(action)

    monkeypatch.setattr(service_module, "handle_service_action", fake_handle)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0)
    input_data = _run_input("outer-run", idempotency_key="runner-key")
    with principal_context(_principal()):
        events = [event async for event in runner_module.run_agent(input_data)]

    service_start = [
        event
        for event in events
        if event.type == EventType.CUSTOM and event.name == "service.result"
    ]
    workflow_results = [
        event
        for event in events
        if event.type == EventType.CUSTOM and event.name == "workflow.result"
    ]
    terminal_events = [
        event
        for event in events
        if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
    ]
    assert len(service_start) == 1
    assert service_start[0].value["data"]["run_id"] == "outer-run"
    assert len(workflow_results) == 1
    assert workflow_results[0].value["terminal_outcome"]["run_id"] == "outer-run"
    assert len(terminal_events) == 1
    assert terminal_events[0].type == EventType.RUN_FINISHED
    assert all(
        payload.get("run_id") == "outer-run"
        for action, payload, _context in calls
        if action.startswith("workflows.")
    )


@pytest.mark.asyncio
async def test_outer_cancel_waits_for_child_cancel_winner_and_emits_one_terminal(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.runner as runner_module
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    fake = _LifecycleServiceFake(store)
    monkeypatch.setattr(service_module, "handle_service_action", fake)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.01)
    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="cancel-key"),
            principal=_principal(),
        )
        assert await asyncio.to_thread(fake.status_entered.wait, 5)

        assert await manager.cancel("outer-run") is True

        assert fake.cancel_calls == 1
        workflow_results = [
            event
            for _seq, event in info.events
            if event.type == EventType.CUSTOM and event.name == "workflow.result"
        ]
        transport_terminals = [
            event
            for _seq, event in info.events
            if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
        ]
        assert len(workflow_results) == 1
        assert workflow_results[0].value["terminal_outcome"]["status"] == (
            "cancelled"
        )
        assert [event.type for event in transport_terminals] == [EventType.RUN_ERROR]
        stored = store.get_run("outer-run")
        assert stored.status == "cancelled"
        assert stored.result_seq is not None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unrequested_outer_task_cancel_preserves_durable_workflow(
    tmp_path,
    monkeypatch,
):
    """A foreign outer-task cancellation must not become a workflow cancel."""
    import backend.fastapi_app.agui.runner as runner_module
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    fake = _LifecycleServiceFake(store)
    fake.block_first_status = True
    monkeypatch.setattr(service_module, "handle_service_action", fake)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.01)
    info = None
    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="foreign-cancel-key"),
            principal=_principal(),
        )
        assert await asyncio.to_thread(fake.status_entered.wait, 5)
        original_task = info.task
        assert original_task is not None

        # This deliberately bypasses RunManager.cancel(). There is no user,
        # orphan, or shutdown cancellation intent attached to this task.cancel().
        original_task.cancel()
        for _attempt in range(100):
            if fake.cancel_calls or info.durable_follower:
                break
            await asyncio.sleep(0.01)

        assert fake.cancel_calls == 0
        assert info.durable_follower is True

        fake.complete_primary()
        fake.status_gate.set()
        follower = info.task
        assert follower is not None and follower is not original_task
        await asyncio.wait_for(follower, timeout=2)
        stored = store.get_run("outer-run")
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.cancel_requested_at_ms is None
    finally:
        fake.status_gate.set()
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
            try:
                await info.task
            except asyncio.CancelledError:
                pass
        store.close()


@pytest.mark.asyncio
async def test_cancel_delivered_during_event_processing_forwards_to_graceful_workflow_cancel(
    tmp_path,
    monkeypatch,
):
    """T4b regression test.

    T2 moved every EventStore call in RunManager onto ``await
    self._call_store(...)`` (a ThreadPoolExecutor round trip), which created
    new await points inside ``_execute_run``'s own event-processing body
    (the gate-check and ``_publish_event``). A cancellation delivered at one
    of *those* await points lands outside run_agent's frame, so
    run_agent/``_run_text_to_sql_service_action`` never observes
    ``asyncio.CancelledError`` and never calls its graceful
    ``workflows.cancel`` (``cancel_and_wait_for_terminal``); the run is left
    non-terminal and gets adopted as a durable follower, so
    ``cancel_cached``/``manager.cancel()`` returns False instead of True.

    Unlike ``test_outer_cancel_waits_for_child_cancel_winner_and_emits_one_terminal``
    (which races real wall-clock timing against ``_WORKFLOW_POLL_INTERVAL_SECONDS``
    and was flaky under T2), this test deterministically parks
    ``_execute_run``'s task inside ``_publish_event`` for a specific event
    (the first ``workflow.progress`` CustomEvent, i.e. strictly outside
    run_agent's frame) and only then calls ``manager.cancel()``, guaranteeing
    the cancellation is delivered exactly in that window every run.
    """
    import backend.fastapi_app.agui.runner as runner_module
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    fake = _LifecycleServiceFake(store)
    monkeypatch.setattr(service_module, "handle_service_action", fake)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.01)

    reached_gate = asyncio.Event()
    release_gate = asyncio.Event()
    publish_calls = 0
    original_publish_event = run_manager_module.RunManager._publish_event

    async def gate_nth_publish(self, run_id, event):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 5:
            # The 5th published event is the first "workflow.progress"
            # CustomEvent: by this point RUN_STARTED/STATE_SNAPSHOT/
            # MESSAGES_SNAPSHOT/service.result have already been produced and
            # run_agent is parked on its own yield, so this await is strictly
            # inside _execute_run's event-processing body, not inside
            # run_agent's frame -- exactly the T4b regression window.
            reached_gate.set()
            await release_gate.wait()
        return await original_publish_event(self, run_id, event)

    monkeypatch.setattr(
        run_manager_module.RunManager, "_publish_event", gate_nth_publish
    )

    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="deterministic-cancel-key"),
            principal=_principal(),
        )
        assert await asyncio.to_thread(fake.status_entered.wait, 5)
        await asyncio.wait_for(reached_gate.wait(), timeout=5)

        # manager.cancel() calls info.task.cancel() while the task is parked
        # at `await release_gate.wait()` above -- i.e. inside _publish_event,
        # outside run_agent's frame. Before the fix this returned False.
        assert await manager.cancel("outer-run") is True

        # (a) the graceful runner.py path ran and actually called
        # workflows.cancel exactly once.
        assert fake.cancel_calls == 1
        workflow_results = [
            event
            for _seq, event in info.events
            if event.type == EventType.CUSTOM and event.name == "workflow.result"
        ]
        transport_terminals = [
            event
            for _seq, event in info.events
            if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
        ]
        # (b) exactly one terminal projection, with a cancelled status.
        assert len(workflow_results) == 1
        assert workflow_results[0].value["terminal_outcome"]["status"] == (
            "cancelled"
        )
        assert [event.type for event in transport_terminals] == [EventType.RUN_ERROR]
        stored = store.get_run("outer-run")
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.result_seq is not None
        # No adoption as a durable follower: the outer run finalized itself.
        assert info.durable_follower is False
    finally:
        release_gate.set()
        store.close()


@pytest.mark.asyncio
async def test_pending_forwarded_cancel_does_not_abort_generic_run_finalization(
    tmp_path,
    monkeypatch,
):
    """T4b follow-up regression test (admission is None path).

    If the forwarded cancellation (cancel_forwarded + current_task.cancel() in
    _execute_run's event loop) is caught on the LAST event the generator will
    ever yield, the next ``agen.__anext__()`` finishes with StopAsyncIteration
    synchronously (every generic runner path yields its terminal event right
    before returning, with no awaits in between), ``aclose()`` on the exhausted
    generator does not suspend either, and the still-pending cancellation
    survives into ``_execute_run``'s ``finally``. Without shielding, the first
    ``await self._call_store(...)`` of the generic (admission is None)
    finalization got that CancelledError: transition_run never ran, info.status
    stayed RUNNING forever, is_active() stayed True, and stream_live
    subscribers never received the terminating None -- a silently hung run.

    The fix wraps the generic finalization in the same shield+retry idiom as
    _start_run/_publish_event/_await_text_to_sql_finalizer: finalization must
    COMPLETE despite the pending cancel, and the CancelledError is re-raised
    afterwards (the task still ends cancelled, but with finalized state).
    """
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)

    async def stub_run_agent(input_data):
        # The exact shape of every generic run_agent path: the terminal event
        # is yielded immediately before return, with no intervening awaits.
        yield RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=input_data.thread_id,
            run_id=input_data.run_id,
            timestamp=1_700_000_000_000,
        )

    monkeypatch.setattr(run_manager_module, "run_agent", stub_run_agent)

    reached_gate = asyncio.Event()
    release_gate = asyncio.Event()
    original_publish_event = run_manager_module.RunManager._publish_event

    async def gate_terminal_publish(self, run_id, event):
        if event.type == EventType.RUN_FINISHED and not reached_gate.is_set():
            # Park _execute_run's task inside the processing of its LAST
            # event, strictly outside the generator's frame, so that
            # info.task.cancel() below is delivered exactly here.
            reached_gate.set()
            await release_gate.wait()
        return await original_publish_event(self, run_id, event)

    monkeypatch.setattr(
        run_manager_module.RunManager, "_publish_event", gate_terminal_publish
    )

    generic_input = RunAgentInput(
        **{
            "threadId": "thread-generic-run",
            "runId": "generic-run",
            "state": {},
            "messages": [{"id": "msg-1", "role": "user", "content": "hello"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )

    try:
        info = await manager.start_run(generic_input, principal=_principal())

        async def consume():
            events = []
            async for event in manager.stream_live("generic-run", 0):
                events.append(event)
            return events

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(reached_gate.wait(), timeout=5)

        # Direct task cancellation, same as cancel_cached does: delivered at
        # `await release_gate.wait()` above, i.e. while processing the last
        # event the stub generator will ever yield.
        info.task.cancel()
        release_gate.set()

        done, _pending = await asyncio.wait({info.task}, timeout=5)
        assert info.task in done
        # The task itself still ends cancelled...
        assert info.task.cancelled()

        # ...but the finalization must have completed despite the pending
        # forwarded cancel: terminal in-memory status, not active anymore,
        # terminal durable status.
        assert info.status is not run_manager_module.RunStatus.RUNNING
        assert manager.is_active("generic-run") is False
        stored = store.get_run("generic-run")
        assert stored is not None
        assert stored.status == "succeeded"
        # And the stream_live subscriber receives completion (None sentinel)
        # instead of hanging forever.
        streamed = await asyncio.wait_for(consumer, timeout=5)
        assert isinstance(streamed, list)
    finally:
        release_gate.set()
        store.close()


@pytest.mark.asyncio
async def test_completion_winner_at_cancel_boundary_is_not_rewritten(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.runner as runner_module
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    fake = _LifecycleServiceFake(store)
    fake.block_first_status = True
    monkeypatch.setattr(service_module, "handle_service_action", fake)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0)
    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="completion-key"),
            principal=_principal(),
        )
        assert await asyncio.to_thread(fake.status_entered.wait, 5)
        fake.complete_primary()

        assert await manager.cancel("outer-run") is False
        fake.status_gate.set()
        assert info.task is not None
        await info.task

        assert fake.cancel_calls == 0
        workflow_results = [
            event
            for _seq, event in info.events
            if event.type == EventType.CUSTOM and event.name == "workflow.result"
        ]
        transport_terminals = [
            event
            for _seq, event in info.events
            if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
        ]
        assert len(workflow_results) == 1
        assert workflow_results[0].value["terminal_outcome"]["status"] == (
            "succeeded"
        )
        assert [event.type for event in transport_terminals] == [
            EventType.RUN_FINISHED
        ]
        stored = store.get_run("outer-run")
        assert stored.status == "succeeded"
        assert stored.result_seq is not None
    finally:
        fake.status_gate.set()
        store.close()


def test_workflow_manager_cancel_preserves_supervisor_completion_winner(
    tmp_path,
    monkeypatch,
):
    import workflow.streamlit_api as streamlit_api
    from workflow.process_supervisor import CancellationResult

    event_path = tmp_path / "events.db"
    outbox_path = tmp_path / "outbox.db"
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
    )
    monkeypatch.setattr(
        streamlit_api,
        "_schedule_workflow_result_outbox_drain",
        lambda **_kwargs: False,
    )

    store = EventStore(str(event_path))
    _create_text_to_sql_run(store)
    claim = _claim_text_to_sql_worker(
        store,
        run_id="run-1",
        run_incarnation="inc-1",
        thread_id="thread-1",
        worker_pid=43_210,
    )
    completion = _terminal_contract(run_id="run-1", status="succeeded")
    cancel_calls: list[str] = []

    class CompletionWinnerSupervisor:
        @staticmethod
        def cancel(run_id):
            cancel_calls.append(run_id)
            store.finalize_run_with_event(
                run_id,
                _terminal_payload(
                    run_id=run_id,
                    incarnation="inc-1",
                    status="succeeded",
                    reason="COMPLETED",
                ),
                expected_supervisor_id=claim.supervisor_id,
                expected_attempt_generation=claim.attempt_generation,
            )
            return CancellationResult(run_id, "succeeded", False, False)

    manager = streamlit_api.WorkflowManager(
        supervisor=CompletionWinnerSupervisor(),
    )
    with streamlit_api._GLOBAL_WORKFLOW_RUNS_LOCK:
        manager.active_runs.clear()
        manager.active_runs["run-1"] = {
            "run_id": "run-1",
            "run_incarnation": "inc-1",
            "workflow_name": "text_to_sql_pipeline",
            "status": "running",
            "pid": 43_210,
            "parameters": {},
            "session_id": "thread-1",
        }

    try:
        assert manager.cancel_workflow("run-1") is False
        assert cancel_calls == ["run-1"]
        assert manager.active_runs["run-1"]["status"] == "running"
        assert not outbox_path.exists()
        stored = store.get_run("run-1")
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.result_seq == 1
        winner = store.get_event("run-1", stored.result_seq)
        assert winner is not None
        assert winner.payload["terminal_outcome"] == completion
        assert len(list(store.list_after("run-1"))) == 1
    finally:
        with streamlit_api._GLOBAL_WORKFLOW_RUNS_LOCK:
            manager.active_runs.clear()
        store.close()


@pytest.mark.asyncio
async def test_transient_status_failure_keeps_following_without_cancelling(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.runner as runner_module
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    retried_status = threading.Event()
    terminal_gate = threading.Event()

    class TransientStatusFailure(_LifecycleServiceFake):
        def __call__(self, action, payload, principal=None, *, transport_context=None):
            if action == "workflows.status":
                self.status_calls += 1
                if self.status_calls == 1:
                    raise RuntimeError("transient status failure")
                if self.status_calls == 2:
                    retried_status.set()
                    return {
                        "status": {
                            "run_id": "outer-run",
                            "status": "running",
                            "progress_percentage": 10.0,
                            "result_seq": None,
                        }
                    }
                assert terminal_gate.wait(timeout=5)
            return super().__call__(
                action,
                payload,
                principal,
                transport_context=transport_context,
            )

    fake = TransientStatusFailure(store)
    monkeypatch.setattr(service_module, "handle_service_action", fake)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.001)
    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="status-error-key"),
            principal=_principal(),
        )
        assert info.task is not None
        assert await asyncio.to_thread(retried_status.wait, 2)
        await asyncio.sleep(0.02)
        assert fake.cancel_calls == 0

        fake.complete_primary()
        terminal_gate.set()
        await asyncio.wait_for(info.task, timeout=2)

        assert fake.cancel_calls == 0
        assert info.task.done()
        stored = store.get_run("outer-run")
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.result_seq is not None
        terminal_events = [
            event
            for _seq, event in info.events
            if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
        ]
        assert [event.type for event in terminal_events] == [EventType.RUN_FINISHED]
    finally:
        terminal_gate.set()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_mode", ["false", "exception", "timeout"])
async def test_unconfirmed_runner_cancel_is_bounded_and_keeps_following_winner(
    tmp_path,
    monkeypatch,
    cancel_mode,
):
    import backend.fastapi_app.agui.runner as runner_module
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / f"events-{cancel_mode}.db"))
    manager = run_manager_module.RunManager(store)
    fake = _LifecycleServiceFake(store)
    fake.cancel_result = False
    if cancel_mode == "exception":
        fake.cancel_exception = RuntimeError("cancel unavailable")
    elif cancel_mode == "timeout":
        fake.cancel_gate = threading.Event()
    monkeypatch.setattr(service_module, "handle_service_action", fake)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(
        runner_module,
        "_WORKFLOW_CANCEL_REQUEST_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(runner_module, "_WORKFLOW_CANCEL_OBSERVE_SECONDS", 0.02)
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.05)
    info = None
    try:
        info = await manager.start_run(
            _run_input(
                "outer-run",
                idempotency_key=f"unconfirmed-{cancel_mode}-key",
            ),
            principal=_principal(),
        )
        assert await asyncio.to_thread(fake.status_entered.wait, 5)

        assert await asyncio.wait_for(manager.cancel("outer-run"), timeout=0.5) is False
        assert fake.cancel_calls == 1
        assert info.task is not None and not info.task.done()
        stored = store.get_run("outer-run")
        assert stored is not None
        assert stored.status == "running"
        assert stored.result_seq is None
        assert not any(
            event.type == EventType.CUSTOM
            and event.name == "workflow.result"
            and event.value.get("terminal_outcome", {}).get("status") == "cancelled"
            for _seq, event in info.events
        )

        assert info.cancel_requested is False
        if fake.cancel_gate is not None:
            fake.cancel_gate.set()
        fake.complete_primary()
        await asyncio.wait_for(info.task, timeout=1)

        stored = store.get_run("outer-run")
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.result_seq is not None
        workflow_results = [
            event
            for _seq, event in info.events
            if event.type == EventType.CUSTOM and event.name == "workflow.result"
        ]
        assert [
            event.value["terminal_outcome"]["status"]
            for event in workflow_results
        ] == ["succeeded"]
        assert any(
            event.type == EventType.RUN_FINISHED
            for _seq, event in info.events
        )
    finally:
        if fake.cancel_gate is not None:
            fake.cancel_gate.set()
        if info is not None and info.task is not None and not info.task.done():
            stored = store.get_run("outer-run")
            if stored is not None and stored.result_seq is None:
                fake.complete_primary()
            await asyncio.wait_for(info.task, timeout=1)
        store.close()


@pytest.mark.asyncio
async def test_cancel_while_sync_start_is_blocked_waits_then_cancels_child(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.runner as runner_module
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    fake = _LifecycleServiceFake(store)
    fake.block_start_after_reservation = True
    monkeypatch.setattr(service_module, "handle_service_action", fake)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0)
    info = None
    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="cancel-during-start-key"),
            principal=_principal(),
        )
        assert await asyncio.to_thread(fake.start_entered.wait, 5)

        cancelling = asyncio.create_task(manager.cancel("outer-run"))
        await asyncio.sleep(0)
        assert not cancelling.done()
        fake.start_gate.set()

        assert await cancelling is True
        assert fake.cancel_calls == 1
        stored = store.get_run("outer-run")
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.result_seq is not None
        task = info.task
        assert task is not None
        await asyncio.wait_for(task, timeout=2)
        assert task.done()
    finally:
        fake.start_gate.set()
        if info is not None and info.task is not None:
            task = info.task
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1)
            except asyncio.CancelledError:
                pass
        store.close()


@pytest.mark.asyncio
async def test_start_response_error_after_worker_handoff_preserves_child(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.runner as runner_module
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    fake = _LifecycleServiceFake(store)
    fake.raise_after_start_reservation = True
    monkeypatch.setattr(service_module, "handle_service_action", fake)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0)
    info = None
    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="error-during-start-key"),
            principal=_principal(),
        )
        assert info.task is not None
        assert await asyncio.to_thread(fake.status_entered.wait, 5)

        assert not await asyncio.to_thread(fake.cancel_entered.wait, 0.2)
        assert fake.cancel_calls == 0
        fake.complete_primary()
        await asyncio.wait_for(info.task, timeout=2)
        stored = store.get_run("outer-run")
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.result_seq is not None
        assert info.task.done()
    finally:
        if info is not None and info.task is not None and not info.task.done():
            await asyncio.wait_for(info.task, timeout=2)
        store.close()


@pytest.mark.asyncio
async def test_start_error_before_worker_handoff_cleans_up_queued_child(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.runner as runner_module
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    fake = _LifecycleServiceFake(store)
    fake.leave_start_unclaimed = True
    fake.raise_after_start_reservation = True
    monkeypatch.setattr(service_module, "handle_service_action", fake)
    monkeypatch.setattr(runner_module, "_WORKFLOW_POLL_INTERVAL_SECONDS", 0)
    try:
        info = await manager.start_run(
            _run_input("outer-run", idempotency_key="queued-start-error-key"),
            principal=_principal(),
        )
        assert info.task is not None
        await asyncio.wait_for(info.task, timeout=2)

        assert fake.cancel_calls == 1
        stored = store.get_run("outer-run")
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.result_seq is not None
    finally:
        store.close()


class _WorkflowManagerSpy:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.start_calls: list[dict[str, object]] = []
        self.worker_starts = 0

    def start_workflow(self, **kwargs):
        self.start_calls.append(kwargs)
        run_id = str(kwargs["run_id"])
        owner = kwargs["owner"]
        principal = Principal(owner.subject, owner.tenant_id, owner.roles)
        admission = kwargs.get("text_to_sql_admission")
        idempotency_key = None if admission is None else admission.idempotency_key
        request_fingerprint = (
            None if admission is None else admission.request_fingerprint
        )
        if admission is not None:
            existing = self.store.get_run_by_idempotency(
                principal=principal,
                run_kind="text_to_sql",
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                assert existing.request_fingerprint == request_fingerprint
                return existing.run_id
        run_incarnation = f"inc-{len(self.start_calls)}"
        session_id = str(kwargs["session_id"])
        deadline_at_ms = int(time.time() * 1000) + 60_000
        work_spec = {
            "spec_version": 1,
            "workflow_path": os.path.abspath(
                "workflow_pipelines/text_to_sql_pipeline.yaml"
            ),
            "parameters": kwargs["parameters"],
            "session_id": session_id,
            "client_id": kwargs["client_id"],
            "use_enhanced": kwargs["use_enhanced"],
            "enable_telemetry": kwargs["enable_telemetry"],
            "run_incarnation": run_incarnation,
            "deadline_at_ms": deadline_at_ms,
        }
        _stored, should_wake = _admit_workflow_run(
            self.store,
            run_id=run_id,
            principal=principal,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            run_incarnation=run_incarnation,
            session_id=session_id,
            workflow_name=str(kwargs["workflow_name"]),
            work_spec=work_spec,
            create_if_missing=True,
        )
        assert should_wake is True
        _claim_text_to_sql_worker(
            self.store,
            run_id=run_id,
            run_incarnation=run_incarnation,
            thread_id=session_id,
            workflow_name=str(kwargs["workflow_name"]),
            supervisor_id=f"test-supervisor-{len(self.start_calls)}",
            worker_pid=10_000 + len(self.start_calls),
        )
        self.worker_starts += 1
        return run_id


class _LifecycleServiceFake:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.status_entered = threading.Event()
        self.status_gate = threading.Event()
        self.block_first_status = False
        self.status_calls = 0
        self.cancel_calls = 0
        self.cancel_entered = threading.Event()
        self.fail_status_calls: set[int] = set()
        self.start_entered = threading.Event()
        self.start_gate = threading.Event()
        self.block_start_after_reservation = False
        self.raise_after_start_reservation = False
        self.leave_start_unclaimed = False
        self.cancel_result = True
        self.cancel_exception: Exception | None = None
        self.cancel_gate: threading.Event | None = None
        self.claim: WorkflowClaimEnvelope | None = None

    def complete_primary(self) -> None:
        assert self.claim is not None
        self.store.finalize_run_with_event(
            "outer-run",
            _terminal_payload(
                run_id="outer-run",
                status="succeeded",
                reason="COMPLETED",
            ),
            expected_supervisor_id=self.claim.supervisor_id,
            expected_attempt_generation=self.claim.attempt_generation,
        )

    def __call__(
        self,
        action,
        payload,
        principal=None,
        *,
        transport_context=None,
    ):
        if action == "presets.text_to_sql.generate":
            assert transport_context.run_id == "outer-run"
            if self.leave_start_unclaimed:
                _admit_workflow_run(
                    self.store,
                    run_id="outer-run",
                    run_incarnation="inc-1",
                    session_id="outer-thread",
                    create_if_missing=True,
                )
            else:
                self.claim = _claim_text_to_sql_worker(
                    self.store,
                    run_id="outer-run",
                    run_incarnation="inc-1",
                    thread_id="outer-thread",
                )
            self.start_entered.set()
            if self.block_start_after_reservation:
                assert self.start_gate.wait(timeout=5)
            if self.raise_after_start_reservation:
                raise RuntimeError("start failed after reservation")
            return {"run_id": "outer-run", "workflow_name": "text_to_sql_pipeline"}
        if action == "workflows.status":
            self.status_calls += 1
            if self.status_calls in self.fail_status_calls:
                self.status_entered.set()
                raise RuntimeError("transient status failure")
            if self.status_calls == 1:
                self.status_entered.set()
                if self.block_first_status:
                    assert self.status_gate.wait(timeout=5)
            stored = self.store.get_run("outer-run")
            status = {
                "pending": "pending",
                "queued": "queued",
                "running": "running",
                "succeeded": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
            }[stored.status]
            return {
                "status": {
                    "run_id": "outer-run",
                    "status": status,
                    "progress_percentage": 100.0 if stored.result_seq else 10.0,
                    "result_seq": stored.result_seq,
                    "terminal_reason": stored.terminal_reason,
                    "worker_pid": stored.worker_pid,
                    "invocation_registered": (
                        self.store.get_workflow_run_invocation("outer-run")
                        is not None
                    ),
                }
            }
        if action == "workflows.cancel":
            self.cancel_entered.set()
            self.cancel_calls += 1
            if self.cancel_gate is not None:
                assert self.cancel_gate.wait(timeout=5)
            if self.cancel_exception is not None:
                raise self.cancel_exception
            if not self.cancel_result:
                return {"cancelled": False}
            terminal = _terminal_payload(
                run_id="outer-run",
                status="cancelled",
                reason="CANCELLED",
            )
            if self.claim is None:
                self.store.finalize_run_with_event("outer-run", terminal)
            else:
                self.store.finalize_run_with_event(
                    "outer-run",
                    terminal,
                    expected_supervisor_id=self.claim.supervisor_id,
                    expected_attempt_generation=self.claim.attempt_generation,
                )
            return {"cancelled": True}
        if action == "workflows.result":
            stored = self.store.get_run("outer-run")
            event = self.store.get_event("outer-run", stored.result_seq)
            return {
                **event.payload,
                "__durable_workflow_result": {
                    **event.payload,
                    "result_seq": stored.result_seq,
                },
            }
        raise AssertionError(action)


@pytest.mark.asyncio
async def test_run_manager_service_workflow_enqueue_preserves_pending_owner(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module
    import workflow.streamlit_api as streamlit_api

    event_path = tmp_path / "events.db"
    store = EventStore(str(event_path))
    principal = _principal()
    connection_ref = _install_service_connection(
        service_module,
        monkeypatch,
        tmp_path,
        principal,
    )
    observed_statuses: list[str] = []

    class PersistingSupervisor:
        def submit(self, run_id, work_spec, *, deadline_at_ms):
            observed_statuses.append(store.get_run(run_id).status)
            store.enqueue_run(run_id, work_spec, deadline_at_ms, 10)
            return types.SimpleNamespace(state="queued")

        @staticmethod
        def cancel(_run_id):
            return types.SimpleNamespace(
                accepted=False,
                state="queued",
                local=False,
            )

    workflow_manager = streamlit_api.WorkflowManager(
        supervisor=PersistingSupervisor(),
    )
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: tmp_path / "outbox.db",
    )
    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service_module, "_WF_MANAGER", workflow_manager)

    input_data = _run_input("outer-run", idempotency_key="pending-owner-key")
    service_payload = input_data.forwarded_props["service_payload"]
    service_payload.pop("dsn")
    service_payload["connection_ref"] = connection_ref

    async def run_service(request):
        response = await asyncio.to_thread(
            service_module.handle_service_action,
            "presets.text_to_sql.generate",
            request.forwarded_props["service_payload"],
            principal,
            transport_context=service_module.ServiceTransportContext(
                run_id=request.run_id,
                principal=principal,
            ),
        )
        yield CustomEvent(
            type=EventType.CUSTOM,
            name="service.result",
            value={
                "action": "presets.text_to_sql.generate",
                "ok": True,
                "data": response,
            },
            timestamp=1,
        )

    monkeypatch.setattr(run_manager_module, "run_agent", run_service)
    manager = run_manager_module.RunManager(store)
    info = None
    try:
        info = await manager.start_run(input_data, principal=principal)
        assert info.task is not None
        await asyncio.wait_for(info.task, timeout=2)
        stored = store.get_run("outer-run")
        assert observed_statuses == ["queued"]
        assert stored is not None
        assert stored.status == "queued"
        assert store.get_workflow_run_invocation("outer-run") is not None
    finally:
        if info is not None and info.task is not None and not info.task.done():
            outer_task = info.task
            outer_task.cancel()
            try:
                await outer_task
            except asyncio.CancelledError:
                pass
            follower_task = info.task
            if follower_task is not None and not follower_task.done():
                follower_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await follower_task
        store.close()


def test_workflow_admission_rolls_back_if_spec_insert_fails(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    try:
        store._conn.execute(
            """
            CREATE TRIGGER fail_work_spec_insert
            BEFORE INSERT ON workflow_run_specs
            BEGIN
                SELECT RAISE(ABORT, 'forced work_spec failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="forced work_spec failure"):
            _admit_workflow_run(
                store,
                run_id="rollback-run",
                create_if_missing=True,
            )
        assert store.get_run("rollback-run") is None
        assert store.get_workflow_run_invocation("rollback-run") is None
        assert store.load_work_spec("rollback-run") is None
    finally:
        store.close()


def test_workflow_admission_rejects_partial_legacy_reservation_unchanged(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    try:
        _create_text_to_sql_run(store, run_id="partial-run")
        assert store.reserve_workflow_run(
            "partial-run",
            "partial-inc",
            "session-partial-run",
            "text_to_sql_pipeline",
        )
        with pytest.raises(WorkflowAdmissionConflictError, match="partial"):
            _admit_workflow_run(
                store,
                run_id="partial-run",
                run_incarnation="partial-inc",
            )
        stored = store.get_run("partial-run")
        assert stored is not None
        assert stored.status == "pending"
        assert store.load_work_spec("partial-run") is None
    finally:
        store.close()


def test_matching_workflow_admission_is_idempotent_and_conflicts_are_unchanged(
    tmp_path,
):
    store = EventStore(str(tmp_path / "events.db"))
    try:
        _create_text_to_sql_run(store, run_id="admitted-run")
        stored, should_wake = _admit_workflow_run(
            store,
            run_id="admitted-run",
            session_id="session-admitted-run",
            parameters={"query": "first"},
        )
        spec = store.load_work_spec("admitted-run")
        assert stored.status == "queued"
        assert should_wake is True
        assert spec is not None
        with pytest.raises(WorkflowAdmissionConflictError, match="identity"):
            _admit_workflow_run(
                store,
                run_id="admitted-run",
                run_incarnation="conflicting-incarnation",
                session_id="session-admitted-run",
                parameters={"query": "second"},
            )
        assert store.load_work_spec("admitted-run") == spec
        assert store.get_workflow_run_invocation(
            "admitted-run"
        ).run_incarnation == "inc-1"
    finally:
        store.close()


def test_service_transport_context_uses_outer_id_and_owner_quota(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    principal = _principal()
    connection_ref = _install_service_connection(
        service_module,
        monkeypatch,
        tmp_path,
        principal,
    )
    payload = {
        "query": "count orders",
        "connection_ref": connection_ref,
        "client_id": "attacker-selected",
        "idempotency_key": "transport-key",
    }
    fingerprint = canonical_text_to_sql_start_fingerprint(payload)
    store.create_or_get_run(
        principal=principal,
        run_kind="text_to_sql",
        idempotency_key="transport-key",
        request_fingerprint=fingerprint,
        proposed_run_id="outer-run",
        thread_id="outer-thread",
    )
    manager = _WorkflowManagerSpy(store)
    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service_module, "_WF_MANAGER", manager)
    try:
        response = service_module.handle_service_action(
            "presets.text_to_sql.generate",
            payload,
            principal=principal,
            transport_context=service_module.ServiceTransportContext(
                run_id="outer-run",
                principal=principal,
            ),
        )
        assert response["run_id"] == "outer-run"
        assert [call["run_id"] for call in manager.start_calls] == ["outer-run"]
        call = manager.start_calls[0]
        assert call["client_id"] != "attacker-selected"
        assert call["owner"].subject == principal.subject
        assert call["owner"].tenant_id == principal.tenant_id
        assert store.get_run("outer-run").owner_subject == principal.subject
    finally:
        store.close()


def test_standalone_service_retry_reuses_one_durable_id_and_worker(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    manager = _WorkflowManagerSpy(store)
    principal = _principal()
    connection_ref = _install_service_connection(
        service_module,
        monkeypatch,
        tmp_path,
        principal,
    )
    payload = {
        "query": "count orders",
        "connection_ref": connection_ref,
        "idempotency_key": "standalone-key",
    }
    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service_module, "_WF_MANAGER", manager)
    try:
        first = service_module.handle_service_action(
            "presets.text_to_sql.generate",
            payload,
            principal=principal,
        )
        second = service_module.handle_service_action(
            "presets.text_to_sql.generate",
            payload,
            principal=principal,
        )

        assert second["run_id"] == first["run_id"]
        assert len(manager.start_calls) == 2
        assert manager.worker_starts == 1
        stored = store.get_run(first["run_id"])
        assert stored is not None
        assert stored.run_kind == "text_to_sql"
        assert stored.owner_subject == principal.subject
    finally:
        store.close()


def test_concurrent_standalone_retries_share_one_reservation_and_worker(
    tmp_path,
    monkeypatch,
):
    import workflow.streamlit_api as streamlit_api
    from backend.fastapi_app.agui._t2s_requests import TextToSqlWorkflowAdmission

    event_path = tmp_path / "events.db"
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: tmp_path / "outbox.db",
    )
    with streamlit_api._GLOBAL_WORKFLOW_RUNS_LOCK:
        streamlit_api._GLOBAL_WORKFLOW_ACTIVE_RUNS.clear()
        streamlit_api._GLOBAL_WORKFLOW_RUN_CALLBACKS.clear()
    barrier = threading.Barrier(2)
    submit_calls: list[str] = []
    submit_lock = threading.Lock()

    class Supervisor:
        def submit(self, run_id, _work_spec, *, deadline_at_ms):
            assert deadline_at_ms > int(time.time() * 1000)
            with submit_lock:
                submit_calls.append(run_id)
            return types.SimpleNamespace(state="queued")

    manager = streamlit_api.WorkflowManager(
        pipelines_dir="workflow_pipelines",
        supervisor=Supervisor(),
    )
    principal = _principal()
    owner = streamlit_api.WorkflowOwner(
        subject=principal.subject,
        tenant_id=principal.tenant_id,
        roles=principal.roles,
    )
    admission = TextToSqlWorkflowAdmission(
        idempotency_key="concurrent-standalone-key",
        request_fingerprint="a" * 64,
    )

    def start(index: int) -> str:
        barrier.wait(timeout=5)
        return manager.start_workflow(
            "text_to_sql_pipeline",
            parameters={"run_id": f"proposed-{index}"},
            session_id="session-race",
            run_id=f"run-proposed-{index}",
            owner=owner,
            text_to_sql_admission=admission,
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(start, range(2)))

        assert responses[0] == responses[1]
        assert submit_calls == [responses[0]]
        store = EventStore(str(event_path))
        try:
            stored = store.get_run(responses[0])
            assert stored is not None
            assert stored.run_kind == "text_to_sql"
            assert stored.status == "queued"
            assert store.get_workflow_run_invocation(stored.run_id) is not None
            assert store.load_work_spec(stored.run_id) is not None
        finally:
            store.close()
    finally:
        with streamlit_api._GLOBAL_WORKFLOW_RUNS_LOCK:
            streamlit_api._GLOBAL_WORKFLOW_ACTIVE_RUNS.clear()
            streamlit_api._GLOBAL_WORKFLOW_RUN_CALLBACKS.clear()


def test_matching_standalone_retry_does_not_wait_for_delayed_pid_attach(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    reserved = threading.Event()
    attach_gate = threading.Event()

    class DelayedAttachManager:
        def __init__(self):
            self.start_attempts = 0
            self.worker_starts = 0

        def start_workflow(self, **kwargs):
            self.start_attempts += 1
            run_id = str(kwargs["run_id"])
            owner = kwargs["owner"]
            admission_principal = Principal(
                owner.subject,
                owner.tenant_id,
                owner.roles,
            )
            admission = kwargs["text_to_sql_admission"]
            existing = store.get_run_by_idempotency(
                principal=admission_principal,
                run_kind="text_to_sql",
                idempotency_key=admission.idempotency_key,
            )
            if existing is not None:
                assert existing.request_fingerprint == admission.request_fingerprint
                return existing.run_id
            session_id = str(kwargs["session_id"])
            deadline_at_ms = int(time.time() * 1000) + 60_000
            _stored, should_wake = _admit_workflow_run(
                store,
                run_id=run_id,
                principal=admission_principal,
                idempotency_key=admission.idempotency_key,
                request_fingerprint=admission.request_fingerprint,
                run_incarnation="inc-delayed-attach",
                session_id=session_id,
                workflow_name=str(kwargs["workflow_name"]),
                work_spec={
                    "spec_version": 1,
                    "workflow_path": os.path.abspath(
                        "workflow_pipelines/text_to_sql_pipeline.yaml"
                    ),
                    "parameters": kwargs["parameters"],
                    "session_id": session_id,
                    "client_id": kwargs["client_id"],
                    "use_enhanced": kwargs["use_enhanced"],
                    "enable_telemetry": kwargs["enable_telemetry"],
                    "run_incarnation": "inc-delayed-attach",
                    "deadline_at_ms": deadline_at_ms,
                },
                create_if_missing=True,
            )
            assert should_wake is True
            claim = store.claim_next_queued(
                "test-supervisor-delayed-attach",
                10,
                10,
                10,
                9_999_999_999_999,
            )
            assert claim is not None
            reserved.set()
            assert attach_gate.wait(timeout=5)
            assert store.set_worker_pid(
                run_id,
                54321,
                claim.claim.supervisor_id,
                claim.claim.attempt_generation,
            )
            self.worker_starts += 1
            return run_id

    manager = DelayedAttachManager()
    principal = _principal()
    connection_ref = _install_service_connection(
        service_module,
        monkeypatch,
        tmp_path,
        principal,
    )
    payload = {
        "query": "count orders",
        "connection_ref": connection_ref,
        "idempotency_key": "delayed-attach-key",
    }
    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service_module, "_WF_MANAGER", manager)

    def start():
        return service_module.handle_service_action(
            "presets.text_to_sql.generate",
            payload,
            principal=principal,
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(start)
            assert reserved.wait(timeout=5)
            second = pool.submit(start)
            retry = second.result(timeout=0.25)
            assert not attach_gate.is_set()
            attach_gate.set()
            original = first.result(timeout=5)

        assert retry["run_id"] == original["run_id"]
        assert manager.start_attempts == 2
        assert manager.worker_starts == 1
    finally:
        attach_gate.set()
        store.close()


def test_own_startup_failure_after_reservation_is_not_idempotent_success(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))

    class FailingManager:
        def start_workflow(self, **kwargs):
            assert store.reserve_workflow_run(
                str(kwargs["run_id"]),
                "inc-own-failure",
                str(kwargs["session_id"]),
                str(kwargs["workflow_name"]),
            )
            raise RuntimeError("worker registration failed")

    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service_module, "_WF_MANAGER", FailingManager())
    principal = _principal()
    connection_ref = _install_service_connection(
        service_module,
        monkeypatch,
        tmp_path,
        principal,
    )
    try:
        with pytest.raises(RuntimeError, match="worker registration failed"):
            service_module.handle_service_action(
                "presets.text_to_sql.generate",
                {
                    "query": "count orders",
                    "connection_ref": connection_ref,
                    "idempotency_key": "own-start-failure-key",
                },
                principal=principal,
            )
    finally:
        store.close()


@pytest.mark.parametrize(
    "action",
    [
        "workflows.status",
        "workflows.result",
        "workflows.artifacts",
        "workflows.cancel",
    ],
)
def test_foreign_owner_is_rejected_before_workflow_manager_call(
    tmp_path,
    monkeypatch,
    action,
):
    import backend.fastapi_app.agui.service as service_module

    class RejectManagerAccess:
        def __getattr__(self, name):
            raise AssertionError(f"WorkflowManager accessed before authorization: {name}")

    store = EventStore(str(tmp_path / "events.db"))
    _create_text_to_sql_run(store, principal=_principal("owner-a"))
    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service_module, "_WF_MANAGER", RejectManagerAccess())
    try:
        with pytest.raises(ValueError, match="run not found"):
            service_module.handle_service_action(
                action,
                {"run_id": "run-1"},
                principal=_principal("owner-b"),
            )
    finally:
        store.close()


def test_nonprimary_outbox_result_is_not_visible_as_terminal_service_data(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    class NonterminalManager:
        def get_workflow_status(self, run_id):
            assert run_id == "run-1"
            return None

        def get_workflow_artifacts(self, _run_id):
            raise AssertionError("artifacts must come from primary result_seq")

    store = EventStore(str(tmp_path / "events.db"))
    _create_text_to_sql_run(store)
    store.transition_run("run-1", {"pending"}, "running")
    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service_module, "_WF_MANAGER", NonterminalManager())

    def reject_reconciled_outbox(_run_id):
        raise AssertionError("Text-to-SQL owner path must not reconcile outbox")

    monkeypatch.setattr(
        service_module,
        "_workflow_result_from_store",
        reject_reconciled_outbox,
    )
    try:
        status = service_module.handle_service_action(
            "workflows.status",
            {"run_id": "run-1"},
            principal=_principal(),
        )
        result = service_module.handle_service_action(
            "workflows.result",
            {"run_id": "run-1"},
            principal=_principal(),
        )
        artifacts = service_module.handle_service_action(
            "workflows.artifacts",
            {"run_id": "run-1"},
            principal=_principal(),
        )

        assert status["status"]["status"] == "running"
        assert "terminal_outcome" not in status["status"]
        assert status["status"]["result_seq"] is None
        assert result == {
            "result": None,
            "status": "running",
            "success": False,
            "error": None,
        }
        assert artifacts == {"artifacts": None}
    finally:
        store.close()


@pytest.mark.parametrize(
    ("winner_status", "cached_status"),
    [
        pytest.param("succeeded", "cancelled", id="success-beats-cached-cancel"),
        pytest.param("failed", "completed", id="failure-beats-cached-success"),
    ],
)
def test_workflow_status_projects_terminal_fields_from_primary_result_seq(
    tmp_path,
    monkeypatch,
    winner_status,
    cached_status,
):
    import backend.fastapi_app.agui.service as service_module

    winner = _terminal_payload(
        run_id="run-1",
        incarnation="inc-status-winner",
        status=winner_status,
        reason="COMPLETED" if winner_status == "succeeded" else "EXECUTION_FAILED",
    )
    winner["snapshot"] = {
        "workflow_name": "text_to_sql_pipeline",
        "session_id": "thread-1",
        "parameters": {"source": "primary-winner"},
    }
    cached_terminal = _terminal_contract(
        run_id="run-1",
        status="cancelled" if cached_status == "cancelled" else "succeeded",
    )

    class ConflictingStatus:
        def model_dump(self):
            return {
                "run_id": "run-1",
                "workflow_name": "text_to_sql_pipeline",
                "status": cached_status,
                "progress_percentage": 3.0,
                "error_message": "cached loser error",
                "parameters": {"source": "cached-loser"},
                "terminal_outcome": cached_terminal,
                "current_step": "cached-runtime-step",
            }

    class ConflictingManager:
        def get_workflow_status(self, run_id):
            assert run_id == "run-1"
            return ConflictingStatus()

    store = EventStore(str(tmp_path / "events.db"))
    _create_text_to_sql_run(store)
    assert store.reserve_workflow_run(
        "run-1",
        "inc-status-winner",
        "thread-1",
        "text_to_sql_pipeline",
    )
    store.finalize_run_with_event("run-1", winner)
    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service_module, "_WF_MANAGER", ConflictingManager())
    try:
        response = service_module.handle_service_action(
            "workflows.status",
            {"run_id": "run-1"},
            principal=_principal(),
        )["status"]

        assert response["status"] == (
            "completed" if winner_status == "succeeded" else "failed"
        )
        assert response["terminal_outcome"] == winner["terminal_outcome"]
        assert response["parameters"] == {"source": "primary-winner"}
        assert response["progress_percentage"] == 100.0
        assert response["result_seq"] == 1
        assert response["current_step"] == "cached-runtime-step"
        assert response.get("error_message") != "cached loser error"
    finally:
        store.close()


def test_internal_workflow_result_includes_durable_projection_identity(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    _admit_workflow_run(
        store,
        run_id="run-1",
        run_incarnation="inc-result-projection",
        create_if_missing=True,
    )
    primary = _terminal_payload(
        run_id="run-1",
        incarnation="inc-result-projection",
        status="cancelled",
        reason="CANCELLED",
    )
    store.finalize_run_with_event("run-1", primary)
    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    try:
        internal = service_module.handle_service_action(
            "workflows.result",
            {"run_id": "run-1"},
            principal=_principal(),
            transport_context=service_module.ServiceTransportContext(
                run_id="run-1",
                principal=_principal(),
            ),
        )
        public = service_module.handle_service_action(
            "workflows.result",
            {"run_id": "run-1"},
            principal=_principal(),
        )

        assert internal["__durable_workflow_result"] == {
            **primary,
            "result_seq": 1,
        }
        assert "__durable_workflow_result" not in public
    finally:
        store.close()


@pytest.mark.parametrize("as_admin", [False, True], ids=["owner", "admin"])
def test_owner_actions_allow_own_user_and_explicit_admin_cross_owner(
    tmp_path,
    monkeypatch,
    as_admin,
):
    import backend.fastapi_app.agui.service as service_module

    class ActionManager:
        def __init__(self):
            self.cancelled: list[str] = []

        def get_workflow_status(self, _run_id):
            return None

        def cancel_workflow(self, run_id, **_kwargs):
            self.cancelled.append(run_id)
            return True

    owner = _principal("owner-a")
    caller = (
        Principal(
            subject="admin",
            tenant_id="admin-tenant",
            roles=frozenset({"admin", "user"}),
        )
        if as_admin
        else owner
    )
    store = EventStore(str(tmp_path / "events.db"))
    _create_text_to_sql_run(store, principal=owner)
    manager = ActionManager()
    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service_module, "_WF_MANAGER", manager)
    try:
        status = service_module.handle_service_action(
            "workflows.status",
            {"run_id": "run-1"},
            principal=caller,
        )
        result = service_module.handle_service_action(
            "workflows.result",
            {"run_id": "run-1"},
            principal=caller,
        )
        artifacts = service_module.handle_service_action(
            "workflows.artifacts",
            {"run_id": "run-1"},
            principal=caller,
        )
        cancelled = service_module.handle_service_action(
            "workflows.cancel",
            {"run_id": "run-1"},
            principal=caller,
        )

        assert status["status"]["run_id"] == "run-1"
        assert result["status"] == "pending"
        assert artifacts == {"artifacts": None}
        assert cancelled == {"cancelled": True}
        assert manager.cancelled == ["run-1"]
    finally:
        store.close()


def test_workflow_cancel_uses_server_transport_cancellation_identity(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    observed: list[tuple[str, str | None, str | None]] = []

    class RecordingManager:
        def cancel_workflow(
            self,
            run_id,
            *,
            cancellation_request_id=None,
            cancellation_provenance=None,
        ):
            observed.append(
                (run_id, cancellation_request_id, cancellation_provenance)
            )
            return True

    store = EventStore(str(tmp_path / "events.db"))
    _create_text_to_sql_run(store)
    monkeypatch.setattr(service_module, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service_module, "_WF_MANAGER", RecordingManager())
    context = service_module.ServiceTransportContext(
        run_id="run-1",
        principal=_principal(),
        cancellation_request_id="cancel-server-1",
        cancellation_provenance="agui_run_manager:v1",
    )
    try:
        response = service_module.handle_service_action(
            "workflows.cancel",
            {
                "run_id": "run-1",
                "cancellation_request_id": "forged-client-id",
                "cancellation_provenance": "forged-client-source",
            },
            principal=_principal(),
            transport_context=context,
        )

        assert response == {"cancelled": True}
        assert observed == [
            ("run-1", "cancel-server-1", "agui_run_manager:v1")
        ]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_evicted_terminal_idempotent_retry_hydrates_replay_without_task(
    tmp_path,
    monkeypatch,
):
    input_data = _run_input("original-run", idempotency_key="terminal-key")
    forwarded = input_data.forwarded_props
    payload = forwarded["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key="terminal-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id="original-run",
        thread_id="original-thread",
    )
    store.reserve_workflow_run(
        "original-run",
        "inc-1",
        "original-thread",
        "text_to_sql_pipeline",
    )
    terminal_payload = _terminal_payload(
        run_id="original-run",
        status="succeeded",
        reason="",
    )
    store.finalize_run_with_event(
        "original-run",
        terminal_payload,
    )
    finished = RunFinishedEvent(
        type=EventType.RUN_FINISHED,
        thread_id="original-thread",
        run_id="original-run",
        result=terminal_payload,
        timestamp=1,
    )
    store.append(
        "original-run",
        EventType.RUN_FINISHED.value,
        finished.model_dump(by_alias=True, exclude_none=True),
    )

    async def reject_runner(_input_data):
        raise AssertionError("terminal retry must not start a task")
        yield

    monkeypatch.setattr(run_manager_module, "run_agent", reject_runner)
    manager = run_manager_module.RunManager(store)
    try:
        retried = await manager.start_run(
            _run_input(
                "new-proposal",
                idempotency_key="terminal-key",
                request_id="current-request",
            ),
            principal=_principal(),
        )
        assert retried.run_id == "original-run"
        assert retried.task is None
        assert retried.status == run_manager_module.RunStatus.FINISHED
        assert [event.type for _seq, event in retried.events] == [
            EventType.RUN_FINISHED,
            EventType.CUSTOM,
            EventType.CUSTOM,
        ]
        replayed = [event async for event in manager.stream_live("original-run")]
        assert [event.type for event in replayed] == [
            EventType.RUN_FINISHED,
            EventType.CUSTOM,
            EventType.CUSTOM,
        ]
        assert replayed[-2].name == "service.result"
        assert replayed[-2].value["__request_id"] == "current-request"
        assert replayed[-1].name == "workflow.result"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_terminal_retry_without_transport_events_replays_current_correlation(
    tmp_path,
    monkeypatch,
):
    original = _run_input(
        "original-run",
        idempotency_key="terminal-replay-key",
        request_id="old-request",
    )
    payload = original.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key="terminal-replay-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id="original-run",
        thread_id="original-thread",
    )
    assert store.reserve_workflow_run(
        "original-run",
        "inc-1",
        "original-thread",
        "text_to_sql_pipeline",
    )
    store.finalize_run_with_event(
        "original-run",
        _terminal_payload(run_id="original-run"),
    )

    async def reject_runner(_input_data):
        raise AssertionError("terminal retry must not start or attach a worker")
        yield

    monkeypatch.setattr(run_manager_module, "run_agent", reject_runner)
    manager = run_manager_module.RunManager(store)
    try:
        retried = await manager.start_run(
            _run_input(
                "new-proposal",
                idempotency_key="terminal-replay-key",
                request_id="new-request",
            ),
            principal=_principal(),
        )
        assert retried.run_id == "original-run"
        assert retried.task is None

        events = [event async for event in manager.stream_live("original-run")]
        service_results = [
            event
            for event in events
            if event.type == EventType.CUSTOM and event.name == "service.result"
        ]
        workflow_results = [
            event
            for event in events
            if event.type == EventType.CUSTOM and event.name == "workflow.result"
        ]
        terminal_events = [
            event
            for event in events
            if event.type in {EventType.RUN_FINISHED, EventType.RUN_ERROR}
        ]
        assert len(service_results) == 1
        assert service_results[0].value["__request_id"] == "new-request"
        assert service_results[0].value["data"]["run_id"] == "original-run"
        assert len(workflow_results) == 1
        assert workflow_results[0].value["terminal_outcome"]["status"] == "failed"
        assert len(terminal_events) == 1
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "expected_status"),
    (
        ("succeeded", run_manager_module.RunStatus.FINISHED),
        ("failed", run_manager_module.RunStatus.ERRORED),
        ("cancelled", run_manager_module.RunStatus.CANCELLED),
    ),
)
async def test_terminal_idempotent_retry_replays_without_worker(
    tmp_path,
    monkeypatch,
    terminal_status,
    expected_status,
):
    original = _run_input(
        "original-run",
        idempotency_key="terminal-status-key",
        request_id="original-request",
    )
    payload = original.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key="terminal-status-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id="original-run",
        thread_id="original-thread",
    )
    assert store.reserve_workflow_run(
        "original-run",
        "inc-1",
        "original-thread",
        "text_to_sql_pipeline",
    )
    result_seq = store.finalize_run_with_event(
        "original-run",
        _terminal_payload(
            run_id="original-run",
            status=terminal_status,
        ),
    )

    async def reject_runner(_input_data):
        raise AssertionError("terminal retry must not start a worker")
        yield

    monkeypatch.setattr(run_manager_module, "run_agent", reject_runner)
    manager = run_manager_module.RunManager(store)
    try:
        retried = await manager.start_run(
            _run_input(
                "retry-proposal",
                idempotency_key="terminal-status-key",
                request_id="retry-request",
            ),
            principal=_principal(),
        )

        assert retried.run_id == "original-run"
        assert retried.task is None
        assert retried.status is expected_status
        stored = store.get_run("original-run")
        assert stored is not None
        assert stored.result_seq == result_seq
        with pytest.raises(
            ValueError,
            match="idempotency_key was already used for a different request",
        ):
            await manager.start_run(
                _run_input(
                    "mismatch-proposal",
                    idempotency_key="terminal-status-key",
                    query="count paid orders",
                ),
                principal=_principal(),
            )
    finally:
        manager.close()
        store.close()


@pytest.mark.asyncio
async def test_nonterminal_retry_uses_durable_follower_without_second_start(
    tmp_path,
    monkeypatch,
):
    original = _run_input(
        "original-run",
        idempotency_key="follower-key",
        request_id="old-request",
    )
    payload = original.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key="follower-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id="original-run",
        thread_id="outer-thread",
    )
    assert store.reserve_workflow_run(
        "original-run",
        "inc-1",
        "workflow-session",
        "text_to_sql_pipeline",
    )
    assert store.set_worker_pid("original-run", 54321)

    async def reject_runner(_input_data):
        raise AssertionError("durable follower must not execute run_agent")
        yield

    monkeypatch.setattr(run_manager_module, "run_agent", reject_runner)
    monkeypatch.setattr(
        store,
        "reserve_workflow_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("durable follower must not reserve a second invocation")
        ),
    )
    manager = run_manager_module.RunManager(store)
    try:
        info = await manager.start_run(
            _run_input(
                "new-proposal",
                idempotency_key="follower-key",
                request_id="new-request",
            ),
            principal=_principal(),
        )
        assert info.task is not None
        service_results = [
            event
            for _seq, event in info.events
            if event.type == EventType.CUSTOM and event.name == "service.result"
        ]
        assert len(service_results) == 1
        assert service_results[0].value["__request_id"] == "new-request"
        assert service_results[0].value["data"]["session_id"] == (
            "workflow-session"
        )

        store.finalize_run_with_event(
            "original-run",
            _terminal_payload(run_id="original-run"),
        )
        await asyncio.wait_for(info.task, timeout=2)
        assert info.status == run_manager_module.RunStatus.ERRORED
        assert any(
            event.type == EventType.CUSTOM and event.name == "workflow.result"
            for _seq, event in info.events
        )
    finally:
        if info.task is not None and not info.task.done():
            info.task.cancel()
        store.close()


@pytest.mark.asyncio
async def test_durable_follower_accepts_reservation_before_pid_attach_and_delivers_winner(
    tmp_path,
    monkeypatch,
):
    retry = _run_input(
        "retry-proposal",
        idempotency_key="delayed-follower-key",
        request_id="retry-request",
    )
    payload = retry.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key="delayed-follower-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id="original-run",
        thread_id="workflow-session",
    )
    assert store.reserve_workflow_run(
        "original-run",
        "inc-delayed-follower",
        "workflow-session",
        "text_to_sql_pipeline",
    )

    async def reject_runner(_input_data):
        raise AssertionError("durable follower must not execute run_agent")
        yield

    monkeypatch.setattr(run_manager_module, "run_agent", reject_runner)
    manager = run_manager_module.RunManager(store)
    info = None
    try:
        info = await asyncio.wait_for(
            manager.start_run(retry, principal=_principal()),
            timeout=0.25,
        )
        assert info.run_id == "original-run"
        assert info.durable_follower is True
        assert info.task is not None and not info.task.done()
        assert store.get_run("original-run").worker_pid is None

        store.finalize_run_with_event(
            "original-run",
            _terminal_payload(
                run_id="original-run",
                incarnation="inc-delayed-follower",
                status="succeeded",
                reason="COMPLETED",
            ),
        )
        await asyncio.wait_for(info.task, timeout=2)
        assert info.status == run_manager_module.RunStatus.FINISHED
        assert any(
            event.type == EventType.CUSTOM and event.name == "workflow.result"
            for _seq, event in info.events
        )
    finally:
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await info.task
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_outcome", "expected_result", "winner_status"),
    [
        ("cancelled", True, "cancelled"),
        ("survives", False, "succeeded"),
        ("completion_wins", False, "succeeded"),
    ],
)
async def test_durable_follower_cancel_uses_owner_service_once_and_keeps_observer(
    tmp_path,
    monkeypatch,
    cancel_outcome,
    expected_result,
    winner_status,
):
    import backend.fastapi_app.agui.service as service_module

    retry = _run_input(
        "retry-proposal",
        idempotency_key=f"durable-cancel-{cancel_outcome}",
        request_id="retry-request",
    )
    payload = retry.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / f"events-{cancel_outcome}.db"))
    store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key=f"durable-cancel-{cancel_outcome}",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id="original-run",
        thread_id="workflow-session",
    )
    assert store.reserve_workflow_run(
        "original-run",
        "inc-durable-cancel",
        "workflow-session",
        "text_to_sql_pipeline",
    )
    assert store.set_worker_pid("original-run", 54321)
    calls: list[tuple[str, Principal]] = []

    def fake_handle(action, action_payload, principal=None, **_kwargs):
        assert action == "workflows.cancel"
        assert action_payload == {"run_id": "original-run"}
        calls.append((action, principal))
        if cancel_outcome == "cancelled":
            store.finalize_run_with_event(
                "original-run",
                _terminal_payload(
                    run_id="original-run",
                    incarnation="inc-durable-cancel",
                    status="cancelled",
                    reason="CANCELLED",
                ),
            )
            return {"cancelled": True}
        if cancel_outcome == "completion_wins":
            store.finalize_run_with_event(
                "original-run",
                _terminal_payload(
                    run_id="original-run",
                    incarnation="inc-durable-cancel",
                    status="succeeded",
                    reason="COMPLETED",
                ),
            )
        return {"cancelled": False}

    async def reject_runner(_input_data):
        raise AssertionError("durable follower must not execute run_agent")
        yield

    monkeypatch.setattr(service_module, "handle_service_action", fake_handle)
    monkeypatch.setattr(run_manager_module, "run_agent", reject_runner)
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.05)
    manager = run_manager_module.RunManager(store)
    info = None
    try:
        info = await manager.start_run(retry, principal=_principal())
        observer_task = info.task
        assert observer_task is not None and not observer_task.done()

        assert await manager.cancel("original-run") is expected_result
        assert calls == [("workflows.cancel", _principal())]
        assert info.task is observer_task
        assert observer_task.cancelled() is False

        if cancel_outcome == "survives":
            assert not observer_task.done()
            assert await manager.cancel("original-run") is False
            assert len(calls) == 2
            store.finalize_run_with_event(
                "original-run",
                _terminal_payload(
                    run_id="original-run",
                    incarnation="inc-durable-cancel",
                    status="succeeded",
                    reason="COMPLETED",
                ),
            )

        await asyncio.wait_for(observer_task, timeout=2)
        stored = store.get_run("original-run")
        assert stored is not None and stored.status == winner_status
        assert info.status == (
            run_manager_module.RunStatus.CANCELLED
            if winner_status == "cancelled"
            else run_manager_module.RunStatus.FINISHED
        )
    finally:
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await info.task
        store.close()


@pytest.mark.asyncio
async def test_durable_follower_cancel_exception_releases_for_one_retry(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    retry = _run_input(
        "retry-proposal",
        idempotency_key="durable-cancel-exception-key",
        request_id="retry-request",
    )
    payload = retry.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key="durable-cancel-exception-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id="original-run",
        thread_id="workflow-session",
    )
    assert store.reserve_workflow_run(
        "original-run",
        "inc-durable-exception",
        "workflow-session",
        "text_to_sql_pipeline",
    )
    calls: list[Principal] = []

    def fail_then_cancel(action, action_payload, principal=None, **_kwargs):
        assert action == "workflows.cancel"
        assert action_payload == {"run_id": "original-run"}
        calls.append(principal)
        if len(calls) == 1:
            raise RuntimeError("cancel transport failed")
        assert len(calls) == 2
        store.finalize_run_with_event(
            "original-run",
            _terminal_payload(
                run_id="original-run",
                incarnation="inc-durable-exception",
                status="cancelled",
                reason="CANCELLED",
            ),
        )
        return {"cancelled": True}

    async def reject_runner(_input_data):
        raise AssertionError("durable follower must not execute run_agent")
        yield

    monkeypatch.setattr(service_module, "handle_service_action", fail_then_cancel)
    monkeypatch.setattr(run_manager_module, "run_agent", reject_runner)
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.01)
    manager = run_manager_module.RunManager(store)
    info = None
    try:
        info = await manager.start_run(retry, principal=_principal())
        observer_task = info.task
        assert observer_task is not None and not observer_task.done()

        assert await manager.cancel("original-run") is False
        assert await manager.cancel("original-run") is True
        assert calls == [_principal(), _principal()]
        await asyncio.wait_for(observer_task, timeout=2)
        assert store.get_run("original-run").status == "cancelled"
    finally:
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await info.task
        store.close()


@pytest.mark.asyncio
async def test_durable_follower_cancel_timeout_keeps_singleflight_until_exception(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    retry = _run_input(
        "retry-proposal",
        idempotency_key="durable-cancel-timeout-key",
        request_id="retry-request",
    )
    payload = retry.forwarded_props["service_payload"]
    store = EventStore(str(tmp_path / "events.db"))
    store.create_or_get_run(
        principal=_principal(),
        run_kind="text_to_sql",
        idempotency_key="durable-cancel-timeout-key",
        request_fingerprint=canonical_text_to_sql_start_fingerprint(payload),
        proposed_run_id="original-run",
        thread_id="workflow-session",
    )
    assert store.reserve_workflow_run(
        "original-run",
        "inc-durable-timeout",
        "workflow-session",
        "text_to_sql_pipeline",
    )
    gate = threading.Event()
    calls: list[Principal] = []
    active = 0
    max_active = 0
    calls_lock = threading.Lock()

    def blocked_cancel(action, action_payload, principal=None, **_kwargs):
        nonlocal active, max_active
        assert action == "workflows.cancel"
        assert action_payload == {"run_id": "original-run"}
        with calls_lock:
            calls.append(principal)
            call_number = len(calls)
            active += 1
            max_active = max(max_active, active)
        try:
            if call_number == 1:
                assert gate.wait(timeout=5)
                raise RuntimeError("late cancel transport failure")
            assert call_number == 2
            store.finalize_run_with_event(
                "original-run",
                _terminal_payload(
                    run_id="original-run",
                    incarnation="inc-durable-timeout",
                    status="cancelled",
                    reason="CANCELLED",
                ),
            )
            return {"cancelled": True}
        finally:
            with calls_lock:
                active -= 1

    async def reject_runner(_input_data):
        raise AssertionError("durable follower must not execute run_agent")
        yield

    monkeypatch.setattr(service_module, "handle_service_action", blocked_cancel)
    monkeypatch.setattr(run_manager_module, "run_agent", reject_runner)
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.01)
    manager = run_manager_module.RunManager(store)
    info = None
    try:
        info = await manager.start_run(retry, principal=_principal())
        observer_task = info.task
        assert observer_task is not None and not observer_task.done()

        assert await manager.cancel("original-run") is False
        assert await manager.cancel("original-run") is False
        assert calls == [_principal()]
        assert max_active == 1
        first_dispatch = manager._uncached_cancel_dispatches["original-run"]

        gate.set()
        first_result = await asyncio.wait_for(
            asyncio.shield(first_dispatch),
            timeout=2,
        )
        assert first_result.failed is True
        assert first_result.explicit_false is False
        assert await manager.cancel("original-run") is True
        assert calls == [_principal(), _principal()]
        assert max_active == 1
        await asyncio.wait_for(observer_task, timeout=2)
        assert store.get_run("original-run").status == "cancelled"
    finally:
        gate.set()
        if info is not None and info.task is not None and not info.task.done():
            info.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await info.task
        store.close()


@pytest.mark.asyncio
async def test_uncached_cancel_rejects_foreign_principal_before_service(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    owner = _principal(subject="owner")
    foreign = _principal(subject="foreign")
    store.create_or_get_run(
        principal=owner,
        run_kind="text_to_sql",
        idempotency_key="foreign-cancel-key",
        request_fingerprint="f" * 64,
        proposed_run_id="foreign-cancel-run",
        thread_id="foreign-cancel-thread",
    )
    calls = []
    monkeypatch.setattr(
        service_module,
        "handle_service_action",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    manager = run_manager_module.RunManager(store)
    try:
        assert await manager.cancel_uncached_text_to_sql(
            "foreign-cancel-run",
            foreign,
        ) is False
        assert calls == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_uncached_cancel_delegates_when_cached_run_appears(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    owner = _principal()
    stored, created = store.create_or_get_run(
        principal=owner,
        run_kind="text_to_sql",
        idempotency_key="cache-race-key",
        request_fingerprint="a" * 64,
        proposed_run_id="cache-race-run",
        thread_id="cache-race-thread",
    )
    assert created is True
    manager = run_manager_module.RunManager(store)
    manager._runs[stored.run_id] = manager._info_from_stored(stored)
    cached_calls = []
    service_calls = []

    async def fake_cancel_cached(run_id):
        cached_calls.append(run_id)
        return True

    monkeypatch.setattr(manager, "cancel_cached", fake_cancel_cached)
    monkeypatch.setattr(
        service_module,
        "handle_service_action",
        lambda *args, **kwargs: service_calls.append((args, kwargs)),
    )
    try:
        assert await manager.cancel_uncached_text_to_sql(
            stored.run_id,
            owner,
        ) is True
        assert cached_calls == [stored.run_id]
        assert service_calls == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_uncached_cancel_singleflight_retries_only_after_explicit_false(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    owner = _principal()
    _create_text_to_sql_run(
        store,
        run_id="uncached-run",
        principal=owner,
        idempotency_key="uncached-singleflight-key",
        fingerprint="b" * 64,
    )
    assert store.reserve_workflow_run(
        "uncached-run",
        "inc-uncached-singleflight",
        "thread-1",
        "text_to_sql_pipeline",
    )
    first_entered = threading.Event()
    first_release = threading.Event()
    calls: list[Principal] = []
    calls_lock = threading.Lock()

    def fake_cancel(action, action_payload, principal=None, **_kwargs):
        assert action == "workflows.cancel"
        assert action_payload == {"run_id": "uncached-run"}
        with calls_lock:
            calls.append(principal)
            call_number = len(calls)
        if call_number == 1:
            first_entered.set()
            assert first_release.wait(timeout=5)
            return {"cancelled": False}
        assert call_number == 2
        store.finalize_run_with_event(
            "uncached-run",
            _terminal_payload(
                run_id="uncached-run",
                incarnation="inc-uncached-singleflight",
                status="cancelled",
                reason="CANCELLED",
            ),
        )
        return {"cancelled": True}

    monkeypatch.setattr(service_module, "handle_service_action", fake_cancel)
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.01)
    manager = run_manager_module.RunManager(store)
    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        first_two = await asyncio.gather(
            manager.cancel_uncached_text_to_sql("uncached-run", owner),
            manager.cancel_uncached_text_to_sql("uncached-run", owner),
        )
        assert first_two == [False, False]
        assert first_entered.is_set()
        assert calls == [owner]

        first_dispatch = manager._uncached_cancel_dispatches["uncached-run"]
        first_release.set()
        dispatch_result = await asyncio.wait_for(
            asyncio.shield(first_dispatch),
            timeout=2,
        )
        assert dispatch_result.explicit_false is True
        assert dispatch_result.failed is False
        assert await manager.cancel_uncached_text_to_sql(
            "uncached-run",
            owner,
        ) is False
        assert calls == [owner]

        monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 1.0)
        assert await manager.cancel_uncached_text_to_sql(
            "uncached-run",
            owner,
        ) is True
        assert calls == [owner, owner]
        assert store.get_run("uncached-run").status == "cancelled"
        assert getattr(manager, "_uncached_cancel_dispatches", {}) == {}
        await asyncio.sleep(0)
        assert not any(
            context.get("message") == "Task exception was never retrieved"
            for context in unhandled
        )
    finally:
        first_release.set()
        loop.set_exception_handler(previous_handler)
        store.close()


@pytest.mark.asyncio
async def test_uncached_cancel_completed_exception_releases_for_one_retry(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    owner = _principal()
    _create_text_to_sql_run(
        store,
        run_id="uncached-exception-run",
        principal=owner,
        idempotency_key="uncached-exception-key",
        fingerprint="d" * 64,
    )
    assert store.reserve_workflow_run(
        "uncached-exception-run",
        "inc-uncached-exception",
        "thread-1",
        "text_to_sql_pipeline",
    )
    calls: list[Principal] = []

    def fail_then_cancel(action, action_payload, principal=None, **_kwargs):
        assert action == "workflows.cancel"
        assert action_payload == {"run_id": "uncached-exception-run"}
        calls.append(principal)
        if len(calls) == 1:
            raise RuntimeError("cancel transport failed")
        assert len(calls) == 2
        store.finalize_run_with_event(
            "uncached-exception-run",
            _terminal_payload(
                run_id="uncached-exception-run",
                incarnation="inc-uncached-exception",
                status="cancelled",
                reason="CANCELLED",
            ),
        )
        return {"cancelled": True}

    monkeypatch.setattr(service_module, "handle_service_action", fail_then_cancel)
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.01)
    manager = run_manager_module.RunManager(store)
    try:
        assert await manager.cancel_uncached_text_to_sql(
            "uncached-exception-run",
            owner,
        ) is False
        first_dispatch = manager._uncached_cancel_dispatches.get(
            "uncached-exception-run"
        )
        if first_dispatch is not None:
            first_result = await asyncio.wait_for(
                asyncio.shield(first_dispatch),
                timeout=1,
            )
            assert first_result.failed is True
        assert await manager.cancel_uncached_text_to_sql(
            "uncached-exception-run",
            owner,
        ) is True
        assert calls == [owner, owner]
        assert manager._uncached_cancel_dispatches == {}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_uncached_cancel_timeout_keeps_singleflight_until_late_exception(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    owner = _principal()
    _create_text_to_sql_run(
        store,
        run_id="uncached-timeout-run",
        principal=owner,
        idempotency_key="uncached-timeout-key",
        fingerprint="e" * 64,
    )
    assert store.reserve_workflow_run(
        "uncached-timeout-run",
        "inc-uncached-timeout",
        "thread-1",
        "text_to_sql_pipeline",
    )
    gate = threading.Event()
    calls: list[Principal] = []
    active = 0
    max_active = 0
    calls_lock = threading.Lock()

    def blocked_then_cancel(action, action_payload, principal=None, **_kwargs):
        nonlocal active, max_active
        assert action == "workflows.cancel"
        assert action_payload == {"run_id": "uncached-timeout-run"}
        with calls_lock:
            calls.append(principal)
            call_number = len(calls)
            active += 1
            max_active = max(max_active, active)
        try:
            if call_number == 1:
                assert gate.wait(timeout=5)
                raise RuntimeError("late cancel transport failure")
            assert call_number == 2
            store.finalize_run_with_event(
                "uncached-timeout-run",
                _terminal_payload(
                    run_id="uncached-timeout-run",
                    incarnation="inc-uncached-timeout",
                    status="cancelled",
                    reason="CANCELLED",
                ),
            )
            return {"cancelled": True}
        finally:
            with calls_lock:
                active -= 1

    monkeypatch.setattr(service_module, "handle_service_action", blocked_then_cancel)
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.01)
    manager = run_manager_module.RunManager(store)
    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    first_dispatch = None
    try:
        assert await manager.cancel_uncached_text_to_sql(
            "uncached-timeout-run",
            owner,
        ) is False
        assert await manager.cancel_uncached_text_to_sql(
            "uncached-timeout-run",
            owner,
        ) is False
        assert calls == [owner]
        assert max_active == 1
        first_dispatch = manager._uncached_cancel_dispatches[
            "uncached-timeout-run"
        ]

        gate.set()
        first_result = await asyncio.wait_for(
            asyncio.shield(first_dispatch),
            timeout=2,
        )
        assert first_result.failed is True
        assert first_result.explicit_false is False
        monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 1.0)
        assert await manager.cancel_uncached_text_to_sql(
            "uncached-timeout-run",
            owner,
        ) is True
        assert calls == [owner, owner]
        assert max_active == 1
        await asyncio.sleep(0)
        assert not any(
            context.get("message") == "Task exception was never retrieved"
            for context in unhandled
        )
    finally:
        active_error = sys.exception()
        cleanup_error = None
        gate.set()
        if first_dispatch is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(first_dispatch),
                    timeout=2,
                )
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                if active_error is None:
                    cleanup_error = exc
        loop.set_exception_handler(previous_handler)
        store.close()
        if cleanup_error is not None:
            raise cleanup_error


@pytest.mark.asyncio
async def test_cached_cancel_does_not_overlap_uncached_dispatch(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app.agui.service as service_module

    store = EventStore(str(tmp_path / "events.db"))
    owner = _principal()
    _create_text_to_sql_run(
        store,
        run_id="cache-appeared-run",
        principal=owner,
        idempotency_key="cache-appeared-key",
        fingerprint="c" * 64,
    )
    assert store.reserve_workflow_run(
        "cache-appeared-run",
        "inc-cache-appeared",
        "thread-1",
        "text_to_sql_pipeline",
    )
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked_cancel(action, action_payload, principal=None, **_kwargs):
        nonlocal calls
        assert action == "workflows.cancel"
        assert action_payload == {"run_id": "cache-appeared-run"}
        assert principal == owner
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return {"cancelled": False}

    monkeypatch.setattr(service_module, "handle_service_action", blocked_cancel)
    monkeypatch.setattr(run_manager_module, "_RUN_CANCEL_WAIT_SECONDS", 0.01)
    manager = run_manager_module.RunManager(store)
    observer_gate = asyncio.Event()
    observer_task = asyncio.create_task(observer_gate.wait())
    dispatch_task = None
    try:
        assert await manager.cancel_uncached_text_to_sql(
            "cache-appeared-run",
            owner,
        ) is False
        assert entered.is_set()
        assert calls == 1
        dispatch_task = manager._uncached_cancel_dispatches["cache-appeared-run"]

        stored = store.get_run("cache-appeared-run")
        assert stored is not None
        info = manager._info_from_stored(stored)
        info.durable_follower = True
        info.task = observer_task
        async with manager._lock:
            manager._runs[info.run_id] = info

        assert await manager.cancel_cached("cache-appeared-run") is False
        assert calls == 1
    finally:
        release.set()
        if dispatch_task is not None:
            await asyncio.wait_for(asyncio.shield(dispatch_task), timeout=1)
        observer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer_task
        await asyncio.sleep(0)
        store.close()


@pytest.mark.asyncio
async def test_http_status_uses_cached_pending_run_before_durable_admission(
    tmp_path,
    monkeypatch,
):
    import workflow.state_files as state_files_module
    import backend.fastapi_app as fastapi_package

    monkeypatch.setattr(
        state_files_module,
        "default_state_database_path",
        lambda *_args, **_kwargs: tmp_path / "main-pending-events.db",
    )
    previous_module = sys.modules.pop("backend.fastapi_app.main", None)
    previous_package_main = getattr(fastapi_package, "main", None)
    main_module = importlib.import_module("backend.fastapi_app.main")

    release = asyncio.Event()

    async def no_admission(_input_data):
        await release.wait()
        if False:
            yield None

    monkeypatch.setattr(run_manager_module, "run_agent", no_admission)
    store = EventStore(str(tmp_path / "events.db"))
    manager = run_manager_module.RunManager(store)
    original_store = main_module.store
    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(main_module, "run_manager", manager)
    monkeypatch.setattr(
        main_module,
        "authenticate_request",
        lambda _request: _principal(),
    )
    info = None
    try:
        info = await manager.start_run(
            _run_input("run-pending", idempotency_key="pending-key"),
            principal=_principal(),
        )

        assert store.get_run(info.run_id) is None
        status = await main_module.run_status_v1(info.run_id, object())

        assert status == {
            "runId": info.run_id,
            "threadId": info.thread_id,
            "status": "pending",
            "startedAtMs": info.started_at_ms,
            "finishedAtMs": None,
        }
        assert store.get_run(info.run_id) is None
    finally:
        release.set()
        if info is not None and info.task is not None:
            await info.task
        manager.close()
        store.close()
        original_store.close()
        sys.modules.pop("backend.fastapi_app.main", None)
        if previous_module is not None:
            sys.modules["backend.fastapi_app.main"] = previous_module
            setattr(fastapi_package, "main", previous_module)
        elif previous_package_main is not None:
            setattr(fastapi_package, "main", previous_package_main)
        elif getattr(fastapi_package, "main", None) is main_module:
            delattr(fastapi_package, "main")


@pytest.mark.asyncio
async def test_http_status_result_and_cancel_fall_back_to_durable_terminal_run(
    tmp_path,
    monkeypatch,
):
    import workflow.state_files as state_files_module
    import backend.fastapi_app as fastapi_package

    monkeypatch.setattr(
        state_files_module,
        "default_state_database_path",
        lambda *_args, **_kwargs: tmp_path / "main-events.db",
    )
    previous_module = sys.modules.pop("backend.fastapi_app.main", None)
    previous_package_main = getattr(fastapi_package, "main", None)
    main_module = importlib.import_module("backend.fastapi_app.main")

    store = EventStore(str(tmp_path / "events.db"))
    _create_text_to_sql_run(store)
    store.reserve_workflow_run(
        "run-1",
        "inc-1",
        "thread-1",
        "text_to_sql_pipeline",
    )
    payload = _terminal_payload(
        status="succeeded",
        reason="COMPLETED",
    )
    store.finalize_run_with_event("run-1", payload)
    manager = run_manager_module.RunManager(store)
    original_store = main_module.store
    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(main_module, "run_manager", manager)
    monkeypatch.setattr(
        main_module,
        "authenticate_request",
        lambda _request: _principal(),
    )
    try:
        status = await main_module.run_status("run-1", object())
        result = await main_module.run_result_v1("run-1", object())
        cancelled = await main_module.cancel_run("run-1", object())

        assert manager.get_info("run-1") is None
        assert status == {
            "run_id": "run-1",
            "thread_id": "thread-1",
            "status": "finished",
            "started_at_ms": store.get_run("run-1").created_at_ms,
            "finished_at_ms": store.get_run("run-1").finished_at_ms,
        }
        assert result == {"result": payload}
        assert cancelled == {"cancelled": False}
    finally:
        store.close()
        original_store.close()
        sys.modules.pop("backend.fastapi_app.main", None)
        if previous_module is not None:
            sys.modules["backend.fastapi_app.main"] = previous_module
            setattr(fastapi_package, "main", previous_module)
        elif previous_package_main is not None:
            setattr(fastapi_package, "main", previous_package_main)
        elif getattr(fastapi_package, "main", None) is main_module:
            delattr(fastapi_package, "main")


@pytest.mark.asyncio
async def test_http_result_preserves_terminal_run_id(
    tmp_path,
    monkeypatch,
):
    import workflow.state_files as state_files_module
    import backend.fastapi_app as fastapi_package

    monkeypatch.setattr(
        state_files_module,
        "default_state_database_path",
        lambda *_args, **_kwargs: tmp_path / "main-terminal-run-id-events.db",
    )
    previous_module = sys.modules.pop("backend.fastapi_app.main", None)
    previous_package_main = getattr(fastapi_package, "main", None)
    main_module = importlib.import_module("backend.fastapi_app.main")

    run_id = "run-1114802362924b2d"
    store = EventStore(str(tmp_path / "events.db"))
    _create_text_to_sql_run(store, run_id=run_id)
    store.reserve_workflow_run(
        run_id,
        "inc-1",
        "thread-1",
        "text_to_sql_pipeline",
    )
    store.finalize_run_with_event(run_id, _terminal_payload(run_id=run_id))
    manager = run_manager_module.RunManager(store)
    original_store = main_module.store
    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(main_module, "run_manager", manager)
    monkeypatch.setattr(
        main_module,
        "authenticate_request",
        lambda _request: _principal(),
    )
    try:
        result = await main_module.run_result_v1(run_id, object())

        assert result["result"]["terminal_outcome"]["run_id"] == run_id
    finally:
        store.close()
        original_store.close()
        sys.modules.pop("backend.fastapi_app.main", None)
        if previous_module is not None:
            sys.modules["backend.fastapi_app.main"] = previous_module
            setattr(fastapi_package, "main", previous_module)
        elif previous_package_main is not None:
            setattr(fastapi_package, "main", previous_package_main)
        elif getattr(fastapi_package, "main", None) is main_module:
            delattr(fastapi_package, "main")


@pytest.mark.asyncio
async def test_http_status_projects_result_pending_as_running(
    tmp_path,
    monkeypatch,
):
    import workflow.state_files as state_files_module
    import backend.fastapi_app as fastapi_package

    monkeypatch.setattr(
        state_files_module,
        "default_state_database_path",
        lambda *_args, **_kwargs: tmp_path / "main-import-events.db",
    )
    previous_module = sys.modules.pop("backend.fastapi_app.main", None)
    previous_package_main = getattr(fastapi_package, "main", None)
    main_module = importlib.import_module("backend.fastapi_app.main")

    store = EventStore(str(tmp_path / "events.db"))
    _create_text_to_sql_run(store)
    assert store.reserve_workflow_run(
        "run-1",
        "inc-1",
        "thread-1",
        "text_to_sql_pipeline",
    )
    deadline = 9_999_999_999_999
    store.enqueue_run(
        "run-1",
        {
            "spec_version": 1,
            "workflow_path": str((tmp_path / "workflow.yaml").resolve()),
            "parameters": {"query": "count orders"},
            "session_id": "thread-1",
            "client_id": "owner:tenant:owner",
            "use_enhanced": True,
            "enable_telemetry": False,
            "run_incarnation": "inc-1",
            "deadline_at_ms": deadline,
        },
        deadline,
        10,
    )
    claim = store.claim_next_queued(
        "supervisor-current", 1, 1, 1, deadline
    )
    assert claim is not None
    assert store.mark_worker_result_pending(
        "run-1",
        "supervisor-current",
        claim.claim.attempt_generation,
    )
    original_store = main_module.store
    monkeypatch.setattr(main_module, "store", store)
    async def _allow_access(*_args):
        return True

    monkeypatch.setattr(main_module.run_manager, "can_access", _allow_access)
    monkeypatch.setattr(
        main_module,
        "authenticate_request",
        lambda _request: _principal(),
    )
    try:
        legacy = await main_module.run_status("run-1", object())
        versioned = await main_module.run_status_v1("run-1", object())
        assert legacy["status"] == "running"
        assert versioned["status"] == "running"
    finally:
        store.close()
        original_store.close()
        sys.modules.pop("backend.fastapi_app.main", None)
        if previous_module is not None:
            sys.modules["backend.fastapi_app.main"] = previous_module
            setattr(fastapi_package, "main", previous_module)
        elif previous_package_main is not None:
            setattr(fastapi_package, "main", previous_package_main)
        elif getattr(fastapi_package, "main", None) is main_module:
            delattr(fastapi_package, "main")


@pytest.mark.asyncio
async def test_startup_drains_terminal_outbox_before_orphan_reconciliation(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app as fastapi_package
    import workflow.state_files as state_files_module
    import workflow.streamlit_api as streamlit_api
    from workflow.result_outbox import WorkflowResultOutbox

    _configure_required_auth(monkeypatch)
    monkeypatch.setattr(
        state_files_module,
        "default_state_database_path",
        lambda *_args, **_kwargs: tmp_path / "main-bootstrap.db",
    )
    monkeypatch.delitem(sys.modules, "backend.fastapi_app.main", raising=False)
    monkeypatch.delattr(fastapi_package, "main", raising=False)
    main_module = importlib.import_module("backend.fastapi_app.main")
    bootstrap_store = main_module.store

    event_path = tmp_path / "events.db"
    outbox_path = tmp_path / "outbox.db"
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
    )
    monkeypatch.setattr(
        streamlit_api,
        "_schedule_workflow_result_outbox_drain",
        lambda **_kwargs: False,
    )

    store = EventStore(str(event_path))
    _create_text_to_sql_run(store)
    claim = _claim_text_to_sql_worker(
        store,
        run_id="run-1",
        run_incarnation="inc-1",
        thread_id="thread-1",
        worker_pid=2_000_000_000,
    )
    terminal = _terminal_contract(run_id="run-1", status="succeeded")
    payload = streamlit_api._build_workflow_result_event_payload(
        "run-1",
        terminal,
        "completed",
        terminal_outcome=terminal,
        snapshot={"workflow_name": "text_to_sql_pipeline"},
        run_incarnation="inc-1",
    )
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(
            payload,
            supervisor_id=claim.supervisor_id,
            attempt_generation=claim.attempt_generation,
        )
    finally:
        outbox.close()

    manager = run_manager_module.RunManager(store)
    monkeypatch.setattr(main_module, "run_manager", manager)
    try:
        async with main_module._lifespan(None):
            pass

        stored = store.get_run("run-1")
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.result_seq == 1
        assert stored.terminal_reason is None
        event = store.get_event("run-1", stored.result_seq)
        assert event is not None
        assert event.payload["terminal_outcome"]["status"] == "succeeded"
        outbox = WorkflowResultOutbox(str(outbox_path))
        try:
            assert outbox.count() == 0
        finally:
            outbox.close()
    finally:
        store.close()
        bootstrap_store.close()


@pytest.mark.asyncio
async def test_startup_skips_orphan_reconciliation_when_outbox_is_not_drained(
    tmp_path,
    monkeypatch,
):
    import backend.fastapi_app as fastapi_package
    import workflow.state_files as state_files_module

    _configure_required_auth(monkeypatch)
    monkeypatch.setattr(
        state_files_module,
        "default_state_database_path",
        lambda *_args, **_kwargs: tmp_path / "main-bootstrap.db",
    )
    monkeypatch.delitem(sys.modules, "backend.fastapi_app.main", raising=False)
    monkeypatch.delattr(fastapi_package, "main", raising=False)
    main_module = importlib.import_module("backend.fastapi_app.main")
    bootstrap_store = main_module.store

    class ManagerSpy:
        store_executor = None

        def reconcile_orphaned_runs(self):
            raise AssertionError("orphan reconciliation must fail closed")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        main_module,
        "_drain_workflow_results_before_reconcile",
        lambda: False,
    )
    monkeypatch.setattr(main_module, "run_manager", ManagerSpy())

    try:
        async with main_module._lifespan(None):
            pass
    finally:
        bootstrap_store.close()


def test_event_store_current_schema_has_one_authoritative_run_lifecycle(tmp_path):
    db_path = tmp_path / "events.db"
    store = EventStore(str(db_path))
    store.close()

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == AGUI_EVENT_STORE_SCHEMA_VERSION
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(agui_runs)")
        }
        assert {
            "run_kind",
            "status",
            "terminal_reason",
            "worker_pid",
            "updated_at_ms",
            "finished_at_ms",
            "result_seq",
            "idempotency_key",
            "request_fingerprint",
        } <= columns
        indexes = {
            str(row[1])
            for row in connection.execute("PRAGMA index_list(agui_runs)")
        }
        assert "idx_agui_runs_owner_idempotency" in indexes


def test_text_to_sql_raw_terminal_transition_is_fail_closed(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    try:
        _create_text_to_sql_run(store)
        for status in (
            "succeeded",
            "abstained",
            "failed",
            "cancelled",
            "timed_out",
        ):
            with pytest.raises(
                ValueError,
                match="finalize_run_with_event",
            ):
                store.transition_run(
                    "run-1",
                    {"pending"},
                    status,
                    reason="RUN_ERROR",
                )

        stored = store.get_run("run-1")
        assert stored is not None
        assert stored.status == "pending"
        assert stored.result_seq is None
        assert store.transition_run(
            "run-1",
            {"pending"},
            "failed",
            reason="SERVER_RESTARTED",
        ) is True
        restarted = store.get_run("run-1")
        assert restarted is not None
        assert restarted.status == "failed"
        assert restarted.terminal_reason == "SERVER_RESTARTED"
        assert restarted.result_seq is None
    finally:
        store.close()


def test_worker_pid_attach_is_idempotent_but_cannot_replace_owner(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    try:
        _create_text_to_sql_run(store)
        assert store.set_worker_pid("run-1", 11111) is True
        assert store.set_worker_pid("run-1", 11111) is True
        assert store.set_worker_pid("run-1", 22222) is False
        stored = store.get_run("run-1")
        assert stored is not None
        assert stored.status == "running"
        assert stored.worker_pid == 11111
    finally:
        store.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_terminal_field",
        "terminal_run_id",
        "legacy_status",
        "legacy_success",
    ],
)
def test_text_to_sql_finalizer_rejects_hostile_terminal_payload(
    tmp_path,
    corruption,
):
    store = EventStore(str(tmp_path / "events.db"))
    try:
        _create_text_to_sql_run(store)
        assert store.reserve_workflow_run(
            "run-1",
            "inc-1",
            "thread-1",
            "text_to_sql_pipeline",
        )
        payload = _terminal_payload()
        terminal = dict(payload["terminal_outcome"])
        payload["terminal_outcome"] = terminal
        if corruption == "missing_terminal_field":
            terminal.pop("execution")
        elif corruption == "terminal_run_id":
            terminal["run_id"] = "other-run"
        elif corruption == "legacy_status":
            payload["status"] = "completed"
        else:
            payload["success"] = True

        with pytest.raises(ValueError, match="WORKFLOW_RESULT|terminal_outcome"):
            store.finalize_run_with_event("run-1", payload)

        stored = store.get_run("run-1")
        assert stored is not None
        assert stored.status == "pending"
        assert stored.result_seq is None
        assert list(store.list_after("run-1")) == []
    finally:
        store.close()


def test_t01_v2_store_migrates_without_losing_owner_event_or_reservation(
    tmp_path,
):
    db_path = tmp_path / "events-v2.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE agui_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                run_incarnation TEXT,
                event_key TEXT
            );
            CREATE TABLE agui_runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                owner_subject TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                roles TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            );
            CREATE TABLE workflow_run_invocations (
                run_id TEXT PRIMARY KEY,
                run_incarnation TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX idx_agui_events_run_seq
            ON agui_events(run_id, seq);
            CREATE UNIQUE INDEX idx_agui_events_event_key
            ON agui_events(event_key) WHERE event_key IS NOT NULL;
            INSERT INTO agui_runs VALUES (
                'legacy-run', 'legacy-thread', 'legacy-owner', 'legacy-tenant',
                '["user"]', 11
            );
            INSERT INTO workflow_run_invocations VALUES (
                'legacy-run', 'legacy-inc', 'legacy-session',
                'text_to_sql_pipeline', 12
            );
            INSERT INTO agui_events
                (run_id, seq, event_type, payload, created_at_ms)
            VALUES ('legacy-run', 1, 'LEGACY_EVENT', '{"legacy":true}', 13);
            PRAGMA user_version=2;
            """
        )

    store = EventStore(str(db_path))
    try:
        stored = store.get_run("legacy-run")
        assert stored is not None
        assert stored.owner_subject == "legacy-owner"
        assert stored.tenant_id == "legacy-tenant"
        assert stored.run_kind == "legacy"
        assert stored.status == "legacy"
        assert stored.updated_at_ms == 11
        assert store.get_workflow_run_invocation("legacy-run") is not None
        events = list(store.list_after("legacy-run"))
        assert len(events) == 1
        assert events[0].payload == {"legacy": True}
    finally:
        store.close()


def test_duplicate_run_id_cannot_replace_owner(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    try:
        stored, created = _create_text_to_sql_run(store)
        assert created is True
        assert stored.owner_subject == "owner"

        with pytest.raises(ValueError, match="run_id already exists"):
            store.create_run(
                "run-1",
                "other-thread",
                _principal("attacker"),
                run_kind="text_to_sql",
            )

        unchanged = store.get_run("run-1")
        assert unchanged is not None
        assert unchanged.owner_subject == "owner"
        assert unchanged.thread_id == "thread-1"
    finally:
        store.close()


def test_same_owner_key_and_fingerprint_returns_existing_run(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    fingerprint = _fingerprint("request")
    try:
        first, first_created = _create_text_to_sql_run(
            store,
            idempotency_key="key-1",
            fingerprint=fingerprint,
        )
        second, second_created = _create_text_to_sql_run(
            store,
            run_id="run-2",
            idempotency_key="key-1",
            fingerprint=fingerprint,
        )
        assert first_created is True
        assert second_created is False
        assert second == first
        assert second.run_id == "run-1"
        assert store.get_run("run-2") is None
    finally:
        store.close()


def test_idempotency_key_reuse_with_different_fingerprint_is_rejected(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    try:
        _create_text_to_sql_run(
            store,
            idempotency_key="key-1",
            fingerprint=_fingerprint("first"),
        )
        with pytest.raises(ValueError, match="idempotency_key"):
            _create_text_to_sql_run(
                store,
                run_id="run-2",
                idempotency_key="key-1",
                fingerprint=_fingerprint("second"),
            )
        assert store.get_run("run-2") is None
    finally:
        store.close()


def test_same_key_is_isolated_by_owner_and_tenant(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    fingerprint = _fingerprint("request")
    try:
        first, _ = _create_text_to_sql_run(
            store,
            idempotency_key="shared-key",
            fingerprint=fingerprint,
        )
        second, created = _create_text_to_sql_run(
            store,
            run_id="run-2",
            principal=_principal("owner", "other-tenant"),
            idempotency_key="shared-key",
            fingerprint=fingerprint,
        )
        assert created is True
        assert second.run_id != first.run_id
        assert second.tenant_id == "other-tenant"
    finally:
        store.close()


def test_worker_attach_atomically_promotes_standalone_pending_run(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    try:
        _create_text_to_sql_run(store)
        assert store.set_worker_pid("run-1", 4321) is True
        stored = store.get_run("run-1")
        assert stored is not None
        assert stored.worker_pid == 4321
        assert stored.status == "running"
    finally:
        store.close()


def test_restart_reconciliation_preserves_live_pid_and_owner(tmp_path, monkeypatch):
    store = EventStore(str(tmp_path / "events.db"))
    owner = _principal()
    try:
        _create_text_to_sql_run(store, run_id="run-live", principal=owner)
        _create_text_to_sql_run(store, run_id="run-dead", principal=owner)
        _create_text_to_sql_run(store, run_id="run-no-pid", principal=owner)
        assert store.set_worker_pid("run-live", os.getpid())
        process = subprocess.Popen(["/bin/true"])
        dead_pid = process.pid
        process.wait(timeout=5)
        assert store.set_worker_pid("run-dead", dead_pid)

        def reject_worker_signal(*_args):
            raise AssertionError("restart reconciliation must not signal workers")

        monkeypatch.setattr(os, "kill", reject_worker_signal)

        assert store.reconcile_non_terminal_runs() == 3

        live = store.get_run("run-live")
        dead = store.get_run("run-dead")
        no_pid = store.get_run("run-no-pid")
        for reconciled in (live, dead, no_pid):
            assert reconciled.status == "failed"
            assert reconciled.terminal_reason == "SERVER_RESTARTED"
            assert reconciled.owner_subject == owner.subject
            assert reconciled.tenant_id == owner.tenant_id
        assert live.worker_pid == os.getpid()
        assert dead.worker_pid == dead_pid
        assert no_pid.worker_pid is None
    finally:
        store.close()


def test_terminal_result_insert_and_lifecycle_cas_are_atomic_and_replayable(
    tmp_path,
):
    store = EventStore(str(tmp_path / "events.db"))
    payload = _terminal_payload()
    try:
        _create_text_to_sql_run(store)
        assert store.reserve_workflow_run(
            "run-1",
            "inc-1",
            "thread-1",
            "text_to_sql_pipeline",
        )
        assert store.transition_run("run-1", {"pending"}, "running") is True

        first_seq = store.finalize_run_with_event("run-1", payload)
        replay_seq = store.finalize_run_with_event("run-1", dict(payload))

        assert replay_seq == first_seq == 1
        stored = store.get_run("run-1")
        assert stored is not None
        assert stored.status == "failed"
        assert stored.terminal_reason == "RESULT_RECONCILIATION_FAILED"
        assert stored.result_seq == first_seq
        assert len(list(store.list_after("run-1"))) == 1
    finally:
        store.close()


def test_same_incarnation_conflicting_terminal_loser_leaves_no_event(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    winner = _terminal_payload()
    loser = _terminal_payload(
        status="cancelled",
        reason="CANCELLED",
    )
    try:
        _create_text_to_sql_run(store)
        assert store.reserve_workflow_run(
            "run-1",
            "inc-1",
            "thread-1",
            "text_to_sql_pipeline",
        )
        store.finalize_run_with_event("run-1", winner)

        with pytest.raises(ValueError, match="event_key|terminal|winner|conflict"):
            store.finalize_run_with_event("run-1", loser)

        events = list(store.list_after("run-1"))
        assert len(events) == 1
        assert events[0].payload == winner
    finally:
        store.close()


def test_terminal_loser_is_not_enqueued_and_outbox_replay_accepts_winner(
    tmp_path,
    monkeypatch,
):
    import workflow.streamlit_api as streamlit_api
    from workflow.result_outbox import WorkflowResultOutbox

    event_path = tmp_path / "events.db"
    outbox_path = tmp_path / "outbox.db"
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: outbox_path,
    )
    monkeypatch.setattr(
        streamlit_api,
        "_schedule_workflow_result_outbox_drain",
        lambda **_kwargs: False,
    )
    store = EventStore(str(event_path))
    _create_text_to_sql_run(store)
    assert store.reserve_workflow_run(
        "run-1",
        "inc-1",
        "thread-1",
        "text_to_sql_pipeline",
    )
    store.close()

    winner = _terminal_contract(run_id="run-1", status="succeeded")
    loser = _terminal_contract(run_id="run-1", status="cancelled")
    winner_resolution = streamlit_api._append_workflow_result_event(
        "run-1",
        winner,
        "completed",
        terminal_outcome=winner,
        snapshot={"workflow_name": "text_to_sql_pipeline"},
        run_incarnation="inc-1",
    )
    loser_resolution = streamlit_api._append_workflow_result_event(
        "run-1",
        loser,
        "cancelled",
        terminal_outcome=loser,
        snapshot={"workflow_name": "text_to_sql_pipeline"},
        run_incarnation="inc-1",
    )
    assert winner_resolution.persistence_succeeded is True
    assert winner_resolution.candidate_won is True
    assert loser_resolution.persistence_succeeded is True
    assert loser_resolution.candidate_won is False
    assert loser_resolution.resolved_payload["terminal_outcome"] == winner
    assert not outbox_path.exists()

    loser_payload = streamlit_api._build_workflow_result_event_payload(
        "run-1",
        loser,
        "cancelled",
        terminal_outcome=loser,
        snapshot={"workflow_name": "text_to_sql_pipeline"},
        run_incarnation="inc-1",
    )
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(loser_payload)
    finally:
        outbox.close()

    delivered, retryable = streamlit_api._drain_workflow_result_outbox_batch(
        path=outbox_path,
        time_budget_seconds=1,
    )
    assert delivered == 1
    assert retryable is True
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.count() == 0
    finally:
        outbox.close()
    store = EventStore(str(event_path))
    try:
        stored = store.get_run("run-1")
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.result_seq == 1
        events = list(store.list_after("run-1"))
        assert len(events) == 1
        assert events[0].payload["terminal_outcome"]["status"] == "succeeded"
    finally:
        store.close()


def test_completion_cancel_barrier_has_one_terminal_winner_and_event(tmp_path):
    db_path = tmp_path / "events.db"
    setup_store = EventStore(str(db_path))
    _create_text_to_sql_run(setup_store)
    setup_store.reserve_workflow_run(
        "run-1",
        "inc-1",
        "thread-1",
        "text_to_sql_pipeline",
    )
    setup_store.close()

    barrier = threading.Barrier(2)
    results: list[tuple[str, object]] = []
    results_lock = threading.Lock()

    def finalize(payload: dict[str, object]) -> None:
        store = EventStore(str(db_path))
        try:
            barrier.wait(timeout=5)
            try:
                value: object = store.finalize_run_with_event("run-1", payload)
                outcome = "ok"
            except ValueError as exc:
                value = str(exc)
                outcome = "conflict"
            with results_lock:
                results.append((outcome, value))
        finally:
            store.close()

    completion = threading.Thread(target=finalize, args=(_terminal_payload(),))
    cancellation = threading.Thread(
        target=finalize,
        args=(
            _terminal_payload(status="cancelled", reason="CANCELLED"),
        ),
    )
    completion.start()
    cancellation.start()
    completion.join(timeout=10)
    cancellation.join(timeout=10)

    assert not completion.is_alive()
    assert not cancellation.is_alive()
    assert sorted(outcome for outcome, _value in results) == ["conflict", "ok"]
    verify_store = EventStore(str(db_path))
    try:
        stored = verify_store.get_run("run-1")
        assert stored is not None
        assert stored.status in {"failed", "cancelled"}
        assert stored.result_seq == 1
        assert len(list(verify_store.list_after("run-1"))) == 1
    finally:
        verify_store.close()
