from __future__ import annotations

import importlib
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.fastapi_app.agui.auth import Principal
from backend.fastapi_app.agui.store import EventStore
from workflow.result_outbox import WorkflowResultOutbox


@dataclass(frozen=True)
class _StartupEnvironment:
    main: types.ModuleType
    store: EventStore
    outbox_path: Path


@pytest.fixture
def startup_environment(tmp_path, monkeypatch):
    for name in (
        "AG_UI_AUTH_TOKEN_MAP",
        "AG_UI_ADMIN_TOKEN",
        "AG_UI_USER_TOKEN",
        "AG_UI_MEMORY_ARCHIVIST_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AG_UI_AUTH_MODE", "required")
    monkeypatch.setenv("AG_UI_AUTH_TOKEN", "startup-test-token")
    monkeypatch.setitem(
        sys.modules,
        "mcp_tools",
        types.SimpleNamespace(mcp_clients=[], mcp_tools=[]),
    )
    import backend.fastapi_app as fastapi_package
    import workflow.state_files as state_files

    bootstrap_path = tmp_path / "main-bootstrap.db"
    monkeypatch.setattr(
        state_files,
        "default_state_database_path",
        lambda *_args, **_kwargs: bootstrap_path,
    )
    monkeypatch.delitem(sys.modules, "backend.fastapi_app.main", raising=False)
    monkeypatch.delattr(fastapi_package, "main", raising=False)
    main = importlib.import_module("backend.fastapi_app.main")
    import workflow.streamlit_api as streamlit_api
    bootstrap_store = main.store

    event_path = tmp_path / "events.db"
    outbox_path = tmp_path / "outbox.db"
    store = EventStore(str(event_path))
    monkeypatch.setattr(main, "store", store)
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

    try:
        yield _StartupEnvironment(main, store, outbox_path)
    finally:
        store.close()
        bootstrap_store.close()


def _create_text_to_sql_run(store: EventStore, run_id: str) -> None:
    principal = Principal(
        subject="owner",
        tenant_id="tenant",
        roles=frozenset({"user"}),
    )
    _stored, created = store.create_or_get_run(
        principal=principal,
        run_kind="text_to_sql",
        idempotency_key=None,
        request_fingerprint=None,
        proposed_run_id=run_id,
        thread_id=f"thread-{run_id}",
    )
    assert created is True


def _enqueue_terminal_result(
    environment: _StartupEnvironment,
    *,
    run_id: str,
    run_incarnation: str,
) -> None:
    import workflow.streamlit_api as streamlit_api

    assert environment.store.reserve_workflow_run(
        run_id,
        run_incarnation,
        f"thread-{run_id}",
        "text_to_sql_pipeline",
    )
    terminal = streamlit_api._build_text_to_sql_no_runtime_terminal(
        run_id=run_id,
        status="failed",
        reason_code="MANDATORY_STEP_NOT_COMPLETED",
        error="workflow stopped before completion",
    )
    payload = streamlit_api._build_workflow_result_event_payload(
        run_id,
        terminal,
        "failed",
        error="workflow stopped before completion",
        terminal_outcome=terminal,
        snapshot={"workflow_name": "text_to_sql_pipeline"},
        run_incarnation=run_incarnation,
    )
    outbox = WorkflowResultOutbox(str(environment.outbox_path))
    try:
        outbox.enqueue(payload)
    finally:
        outbox.close()


@pytest.mark.asyncio
async def test_startup_reconciles_unrelated_orphan_but_excludes_stalled_outbox_run(
    startup_environment,
    monkeypatch,
):
    import workflow.streamlit_api as streamlit_api

    environment = startup_environment
    _create_text_to_sql_run(environment.store, "run-orphan")
    _create_text_to_sql_run(environment.store, "run-outbox")
    _enqueue_terminal_result(
        environment,
        run_id="run-outbox",
        run_incarnation="inc-outbox",
    )
    monkeypatch.setattr(
        streamlit_api,
        "_drain_workflow_result_outbox_batch",
        lambda **_kwargs: (0, True),
    )

    def unavailable_scheduler(**_kwargs) -> None:
        raise OSError("scheduler unavailable")

    monkeypatch.setattr(
        streamlit_api,
        "_schedule_workflow_result_outbox_drain",
        unavailable_scheduler,
    )

    async with environment.main._lifespan(None):
        pass

    orphan = environment.store.get_run("run-orphan")
    protected = environment.store.get_run("run-outbox")
    assert orphan is not None
    assert orphan.status == "failed"
    assert orphan.terminal_reason == "SERVER_RESTARTED"
    assert orphan.result_seq is None
    assert protected is not None
    assert protected.status == "pending"
    assert protected.terminal_reason is None
    assert protected.result_seq is None

    outbox = WorkflowResultOutbox(str(environment.outbox_path))
    try:
        assert outbox.pending_run_ids() == frozenset({"run-outbox"})
    finally:
        outbox.close()


@pytest.mark.asyncio
async def test_startup_drains_terminal_winner_before_reconciling_other_orphan(
    startup_environment,
):
    environment = startup_environment
    _create_text_to_sql_run(environment.store, "run-orphan")
    _create_text_to_sql_run(environment.store, "run-outbox")
    _enqueue_terminal_result(
        environment,
        run_id="run-outbox",
        run_incarnation="inc-outbox",
    )

    async with environment.main._lifespan(None):
        pass

    orphan = environment.store.get_run("run-orphan")
    winner = environment.store.get_run("run-outbox")
    assert orphan is not None
    assert orphan.status == "failed"
    assert orphan.terminal_reason == "SERVER_RESTARTED"
    assert orphan.result_seq is None
    assert winner is not None
    assert winner.status == "failed"
    assert winner.terminal_reason == "MANDATORY_STEP_NOT_COMPLETED"
    assert winner.result_seq == 1
    event = environment.store.get_event("run-outbox", winner.result_seq)
    assert event is not None
    assert event.event_type == "WORKFLOW_RESULT"

    outbox = WorkflowResultOutbox(str(environment.outbox_path))
    try:
        assert outbox.pending_run_ids() == frozenset()
    finally:
        outbox.close()


@pytest.mark.asyncio
async def test_startup_reconciles_pre_restart_live_pid_and_projects_terminal(
    startup_environment,
    monkeypatch,
):
    environment = startup_environment
    run_id = "run-pre-restart-worker"
    _create_text_to_sql_run(environment.store, run_id)
    assert environment.store.set_worker_pid(run_id, os.getpid()) is True

    def unexpected_pid_probe(*_args, **_kwargs):
        pytest.fail("startup reconciliation must not use worker PID as a lease")

    monkeypatch.setattr(os, "kill", unexpected_pid_probe)
    manager = type(environment.main.run_manager)(environment.store)
    monkeypatch.setattr(environment.main, "run_manager", manager)
    owner = Principal(
        subject="owner",
        tenant_id="tenant",
        roles=frozenset({"user"}),
    )
    monkeypatch.setattr(
        environment.main,
        "authenticate_request",
        lambda _request: owner,
    )

    class RequestStub:
        headers = {"accept": "text/event-stream"}

        async def is_disconnected(self):
            return False

    request = RequestStub()
    async with environment.main._lifespan(None):
        pass

    stored = environment.store.get_run(run_id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.terminal_reason == "SERVER_RESTARTED"
    assert stored.worker_pid == os.getpid()
    assert stored.owner_subject == owner.subject
    assert stored.tenant_id == owner.tenant_id
    assert stored.result_seq is None

    status = await environment.main.run_status(run_id, request)
    assert status["status"] == "errored"
    assert status["finished_at_ms"] == stored.finished_at_ms
    assert await environment.main.run_result_v1(run_id, request) == {
        "result": None
    }
    assert await environment.main.cancel_run(run_id, request) == {
        "cancelled": False
    }
    response = await environment.main.replay_events(
        run_id,
        request,
        follow=False,
    )
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    assert "text_to_sql_server_restarted" in "".join(chunks)


@pytest.mark.asyncio
async def test_startup_fails_closed_when_outbox_identities_cannot_be_inspected(
    startup_environment,
    monkeypatch,
):
    environment = startup_environment
    _create_text_to_sql_run(environment.store, "run-orphan")
    monkeypatch.setattr(
        environment.main,
        "_drain_workflow_results_before_reconcile",
        lambda: None,
    )

    async with environment.main._lifespan(None):
        pass

    orphan = environment.store.get_run("run-orphan")
    assert orphan is not None
    assert orphan.status == "pending"
    assert orphan.terminal_reason is None
    assert orphan.result_seq is None
