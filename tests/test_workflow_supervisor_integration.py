from __future__ import annotations

import asyncio
import importlib
import inspect
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.fastapi_app.agui.auth import Principal
from backend.fastapi_app.agui.run_manager import RunStatus, _compatibility_status
from backend.fastapi_app.agui.store import EventStore
from workflow.deadline import DeadlineBudget
from workflow import streamlit_api


@dataclass(frozen=True)
class _Submission:
    run_id: str
    state: str = "queued"


@dataclass(frozen=True)
class _Cancellation:
    accepted: bool
    state: str
    local: bool = False


class _FakeSupervisor:
    def __init__(self) -> None:
        self.submit_calls: list[tuple[str, dict[str, object], int]] = []
        self.cancel_calls: list[str] = []
        self.started = 0
        self.stopped = 0

    def submit(
        self,
        run_id: str,
        work_spec: dict[str, object],
        *,
        deadline_at_ms: int,
    ) -> _Submission:
        self.submit_calls.append((run_id, dict(work_spec), deadline_at_ms))
        return _Submission(run_id)

    def cancel(self, run_id: str) -> _Cancellation:
        self.cancel_calls.append(run_id)
        return _Cancellation(True, "cancelled")

    def start(self) -> bool:
        self.started += 1
        return True

    def stop(self) -> bool:
        self.stopped += 1
        return True


class _PersistingSupervisor(_FakeSupervisor):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def submit(
        self,
        run_id: str,
        work_spec: dict[str, object],
        *,
        deadline_at_ms: int,
    ) -> _Submission:
        result = super().submit(
            run_id,
            work_spec,
            deadline_at_ms=deadline_at_ms,
        )
        store = EventStore(str(self.path))
        try:
            store.enqueue_run(run_id, work_spec, deadline_at_ms, 10)
        finally:
            store.close()
        return result


@pytest.fixture(autouse=True)
def _isolated_workflow_globals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    event_path = tmp_path / "events.db"
    monkeypatch.setattr(streamlit_api, "_agui_event_store_path", lambda: event_path)
    monkeypatch.setattr(
        streamlit_api,
        "_workflow_result_outbox_path",
        lambda: tmp_path / "result-outbox.db",
    )
    with streamlit_api._GLOBAL_WORKFLOW_RUNS_LOCK:
        streamlit_api._GLOBAL_WORKFLOW_ACTIVE_RUNS.clear()
        streamlit_api._GLOBAL_WORKFLOW_RUN_CALLBACKS.clear()
    yield event_path
    with streamlit_api._GLOBAL_WORKFLOW_RUNS_LOCK:
        streamlit_api._GLOBAL_WORKFLOW_ACTIVE_RUNS.clear()
        streamlit_api._GLOBAL_WORKFLOW_RUN_CALLBACKS.clear()


def test_start_enqueues_exact_spec_without_direct_process(
    monkeypatch: pytest.MonkeyPatch,
    _isolated_workflow_globals: Path,
) -> None:
    supervisor = _FakeSupervisor()
    manager = streamlit_api.WorkflowManager(
        pipelines_dir="workflow_pipelines",
        supervisor=supervisor,
    )
    monkeypatch.setattr(streamlit_api.time, "time", lambda: 1_000.0)

    run_id = manager.start_workflow(
        "simple_research",
        parameters={"topic": "SQLite leases"},
        session_id="session-1",
    )

    assert len(supervisor.submit_calls) == 1
    submitted_run_id, spec, deadline_at_ms = supervisor.submit_calls[0]
    assert submitted_run_id == run_id
    assert spec == {
        "spec_version": 1,
        "workflow_path": str(
            Path("workflow_pipelines/simple_research.yaml").resolve()
        ),
        "parameters": {"topic": "SQLite leases"},
        "session_id": "session-1",
        "client_id": spec["client_id"],
        "use_enhanced": True,
        "enable_telemetry": False,
        "run_incarnation": spec["run_incarnation"],
        "deadline_at_ms": 1_300_000,
    }
    assert isinstance(spec["client_id"], str)
    assert deadline_at_ms == 1_300_000
    assert manager.active_runs[run_id]["status"] == "queued"
    assert "deadline_at_ms" not in manager.active_runs[run_id]["parameters"]
    source = inspect.getsource(streamlit_api.WorkflowManager.start_workflow)
    assert "multiprocessing" not in source
    assert "Process(" not in source
    assert "watchdog" not in source
    module_source = inspect.getsource(streamlit_api)
    assert module_source.count("def start_workflow(") == 1
    assert module_source.count("def cancel_workflow(") == 1
    assert "from multiprocessing import Process" not in module_source

    store = EventStore(str(_isolated_workflow_globals))
    try:
        stored = store.get_run(run_id)
        assert stored is not None
        assert stored.run_kind == "legacy"
        assert stored.owner_subject == "legacy-workflow"
        assert stored.status == "queued"
    finally:
        store.close()


def test_spawn_wrapper_rehydrates_deadline_outside_public_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture(
        self,
        run_id,
        workflow_file,
        parameters,
        session_id,
        client_id,
        **kwargs,
    ) -> None:
        captured.update(
            {
                "run_id": run_id,
                "workflow_file": workflow_file,
                "parameters": parameters,
                "session_id": session_id,
                "client_id": client_id,
                **kwargs,
            }
        )

    monkeypatch.setattr(streamlit_api.WorkflowManager, "_run_workflow_thread", capture)
    monkeypatch.setattr(
        streamlit_api,
        "_setup_comprehensive_logging_from_env",
        lambda: None,
    )
    monkeypatch.setattr(
        streamlit_api,
        "_setup_process_run_log_capture",
        lambda _run_id: None,
    )
    deadline_at_ms = int(time.time() * 1_000) + 30_000
    spec = {
        "spec_version": 1,
        "workflow_path": str(
            Path("workflow_pipelines/simple_research.yaml").resolve()
        ),
        "parameters": {"topic": "queue wait"},
        "session_id": "session-1",
        "client_id": "owner:tenant:subject",
        "use_enhanced": True,
        "enable_telemetry": False,
        "run_incarnation": "inc-1",
        "deadline_at_ms": deadline_at_ms,
    }

    claim = {
        "supervisor_id": "supervisor-a",
        "attempt_generation": 17,
        "run_kind": "agui",
        "workflow_name": "simple_research",
    }
    streamlit_api._workflow_supervisor_process_entry("run-1", spec, claim)

    budget = captured["deadline_budget"]
    assert isinstance(budget, DeadlineBudget)
    assert budget.deadline_at_ms == deadline_at_ms
    assert captured["supervisor_id"] == "supervisor-a"
    assert captured["attempt_generation"] == 17
    assert "deadline_budget" not in captured["parameters"]
    assert "deadline_at_ms" not in captured["parameters"]
    assert "supervisor_id" not in captured["parameters"]
    assert "attempt_generation" not in captured["parameters"]
    assert "supervisor_id" not in spec
    assert "attempt_generation" not in spec


def test_manager_cancel_delegates_to_supervisor() -> None:
    supervisor = _FakeSupervisor()
    manager = streamlit_api.WorkflowManager(supervisor=supervisor)
    manager.active_runs["run-1"] = {
        "run_id": "run-1",
        "run_incarnation": "inc-1",
        "workflow_name": "simple_research",
        "status": "queued",
        "parameters": {},
    }

    assert manager.cancel_workflow("run-1") is True
    assert supervisor.cancel_calls == ["run-1"]
    assert manager.active_runs["run-1"]["status"] == "cancelled"


def test_durable_queue_has_explicit_compatibility_status() -> None:
    assert _compatibility_status("queued") is RunStatus.QUEUED


def test_owner_scoped_retry_keeps_one_durable_spec_and_quota_identity(
    _isolated_workflow_globals: Path,
) -> None:
    principal = Principal("alice", "tenant-a", frozenset({"user"}))
    owner = streamlit_api.WorkflowOwner(
        subject=principal.subject,
        tenant_id=principal.tenant_id,
        roles=principal.roles,
    )
    setup = EventStore(str(_isolated_workflow_globals))
    try:
        setup.create_run(
            "run-1",
            "session-1",
            principal,
            run_kind="agui",
        )
    finally:
        setup.close()
    supervisor = _PersistingSupervisor(_isolated_workflow_globals)
    manager = streamlit_api.WorkflowManager(supervisor=supervisor)

    first = manager.start_workflow(
        "simple_research",
        parameters={"topic": "owner quotas"},
        session_id="session-1",
        run_id="run-1",
        owner=owner,
    )
    second = manager.start_workflow(
        "simple_research",
        parameters={"topic": "owner quotas"},
        session_id="session-1",
        run_id="run-1",
        owner=owner,
    )

    assert first == second == "run-1"
    assert len(supervisor.submit_calls) == 1
    assert supervisor.submit_calls[0][1]["client_id"] == owner.quota_identity
    verify = EventStore(str(_isolated_workflow_globals))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == "queued"
        assert stored.owner_subject == "alice"
        assert verify.load_work_spec("run-1") is not None
    finally:
        verify.close()


def test_ownerless_explicit_run_id_replay_is_rejected(
    _isolated_workflow_globals: Path,
) -> None:
    manager = streamlit_api.WorkflowManager(supervisor=_FakeSupervisor())
    manager.start_workflow(
        "simple_research",
        parameters={"topic": "legacy replay"},
        session_id="legacy-session",
        run_id="legacy-explicit-run",
    )

    with pytest.raises(streamlit_api.WorkflowRunAlreadyReservedError):
        manager.start_workflow(
            "simple_research",
            parameters={"topic": "legacy replay"},
            session_id="legacy-session",
            run_id="legacy-explicit-run",
        )


def test_text_to_sql_admission_cannot_start_a_generic_workflow(
    _isolated_workflow_globals: Path,
) -> None:
    from backend.fastapi_app.agui._t2s_requests import TextToSqlWorkflowAdmission

    owner = streamlit_api.WorkflowOwner(
        subject="alice",
        tenant_id="tenant-a",
        roles=frozenset({"user"}),
    )
    manager = streamlit_api.WorkflowManager(supervisor=_FakeSupervisor())

    with pytest.raises(ValueError, match="text_to_sql_pipeline"):
        manager.start_workflow(
            "simple_research",
            parameters={"topic": "must stay generic"},
            session_id="session-1",
            run_id="run-generic",
            owner=owner,
            text_to_sql_admission=TextToSqlWorkflowAdmission(
                idempotency_key="internal-only",
                request_fingerprint="a" * 64,
            ),
        )


def test_service_workflow_start_passes_current_principal_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from backend.fastapi_app.agui import service

    calls: list[dict[str, object]] = []

    class Manager:
        def start_workflow(self, **kwargs):
            calls.append(kwargs)
            return str(kwargs["run_id"])

    event_store = EventStore(str(tmp_path / "service-events.db"))
    monkeypatch.setattr(service, "_AGUI_EVENT_STORE", event_store)
    monkeypatch.setattr(service, "_WF_MANAGER", Manager())
    principal = Principal("alice", "tenant-a", frozenset({"admin"}))
    try:
        result = service.handle_service_action(
            "workflows.start",
            {
                "workflow_name": "simple_research",
                "parameters": {"topic": "tenant quota"},
                "session_id": "session-1",
            },
            principal,
        )

        assert result["status"] == "queued"
        assert len(calls) == 1
        owner = calls[0]["owner"]
        assert (owner.subject, owner.tenant_id) == ("alice", "tenant-a")
        assert calls[0]["client_id"] == owner.quota_identity
        stored = event_store.get_run(str(result["run_id"]))
        assert stored is not None
        assert stored.run_kind == "agui"
        assert (stored.owner_subject, stored.tenant_id) == (
            "alice",
            "tenant-a",
        )
    finally:
        event_store.close()


@pytest.mark.parametrize(
    ("status", "expected_status", "expected_reason"),
    [
        ("cancelled", "cancelled", "CANCELLED"),
        ("timed_out", "timed_out", "TIMED_OUT"),
        ("failed", "failed", "MANDATORY_STEP_NOT_COMPLETED"),
    ],
)
def test_text_to_sql_supervisor_terminalizer_persists_exact_terminal_event(
    _isolated_workflow_globals: Path,
    status: str,
    expected_status: str,
    expected_reason: str,
) -> None:
    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
        )
    finally:
        store.close()

    assert streamlit_api._terminalize_supervised_workflow(
        "run-1",
        None,
        None,
        {"status": status, "reason": "supervisor reason"},
    )

    verify = EventStore(str(_isolated_workflow_globals))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == expected_status
        assert stored.result_seq is not None
        event = verify.get_event("run-1", stored.result_seq)
        assert event is not None
        terminal = event.payload["terminal_outcome"]
        assert terminal["status"] == expected_status
        assert terminal["reason_code"] == expected_reason
        assert len(
            [item for item in verify.list_after("run-1") if item.event_type == "WORKFLOW_RESULT"]
        ) == 1
    finally:
        verify.close()


def _enqueue_text_to_sql_run(store: EventStore, run_id: str) -> int:
    deadline = int(time.time() * 1_000) + 60_000
    store.enqueue_run(
        run_id,
        {
            "spec_version": 1,
            "workflow_path": str(
                Path("workflow_pipelines/text_to_sql_pipeline.yaml").resolve()
            ),
            "parameters": {"query": "count orders"},
            "session_id": "session-1",
            "client_id": "owner:tenant-a:alice",
            "use_enhanced": True,
            "enable_telemetry": False,
            "run_incarnation": "inc-1",
            "deadline_at_ms": deadline,
        },
        deadline,
        10,
    )
    return deadline


def _failed_result_payload(run_id: str = "run-1") -> dict[str, object]:
    terminal = streamlit_api._build_text_to_sql_no_runtime_terminal(
        run_id=run_id,
        status="failed",
        reason_code="MANDATORY_STEP_NOT_COMPLETED",
        error="worker failed",
    )
    return streamlit_api._build_workflow_result_event_payload(
        run_id,
        terminal,
        "failed",
        error="worker failed",
        artifacts={
            "final_output": terminal,
            "terminal_outcome": terminal,
        },
        snapshot={
            "workflow_name": "text_to_sql_pipeline",
            "session_id": "session-1",
        },
        terminal_outcome=terminal,
        run_incarnation="inc-1",
    )


@pytest.mark.parametrize(
    ("candidate_supervisor", "generation_delta"),
    [
        ("supervisor-old", 0),
        ("supervisor-current", -1),
    ],
    ids=["old-owner", "old-generation"],
)
def test_worker_primary_delivery_requires_exact_claim(
    _isolated_workflow_globals: Path,
    candidate_supervisor: str,
    generation_delta: int,
) -> None:
    from backend.fastapi_app.agui.store import (
        WorkflowSupervisorOwnershipConflictError,
    )

    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
        )
        deadline = _enqueue_text_to_sql_run(store, "run-1")
        claim = store.claim_next_queued(
            "supervisor-current", 1, 1, 1, deadline
        )
        assert claim is not None
        generation = claim.claim.attempt_generation
    finally:
        store.close()

    with pytest.raises(WorkflowSupervisorOwnershipConflictError):
        streamlit_api._append_workflow_result_payload_to_primary(
            _failed_result_payload(),
            supervisor_id=candidate_supervisor,
            attempt_generation=generation + generation_delta,
        )

    verify = EventStore(str(_isolated_workflow_globals))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == "running"
        assert stored.result_seq is None
        assert list(verify.list_after("run-1")) == []
    finally:
        verify.close()


def test_running_text_to_sql_terminalizer_rejects_stale_supervisor(
    _isolated_workflow_globals: Path,
) -> None:
    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
        )
        deadline = _enqueue_text_to_sql_run(store, "run-1")
        claim = store.claim_next_queued(
            "supervisor-current", 1, 1, 1, deadline
        )
        assert claim is not None
        generation = claim.claim.attempt_generation
    finally:
        store.close()

    assert not streamlit_api._terminalize_supervised_workflow(
        "run-1",
        "supervisor-stale",
        generation,
        {"status": "cancelled", "reason": "stale cancellation"},
    )

    verify = EventStore(str(_isolated_workflow_globals))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == "running"
        assert stored.supervisor_id == "supervisor-current"
        assert list(verify.list_after("run-1")) == []
    finally:
        verify.close()


def test_queued_text_to_sql_terminalizer_requires_null_supervisor(
    _isolated_workflow_globals: Path,
) -> None:
    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
        )
        _enqueue_text_to_sql_run(store, "run-1")
    finally:
        store.close()

    outcome = {"status": "cancelled", "reason": "queued cancellation"}
    assert not streamlit_api._terminalize_supervised_workflow(
        "run-1", "supervisor-stale", 1, outcome
    )
    assert streamlit_api._terminalize_supervised_workflow(
        "run-1", None, None, outcome
    )

    verify = EventStore(str(_isolated_workflow_globals))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.result_seq is not None
        assert len(
            [
                event
                for event in verify.list_after("run-1")
                if event.event_type == "WORKFLOW_RESULT"
            ]
        ) == 1
    finally:
        verify.close()


def test_supervisor_terminal_primary_failure_never_uses_outbox(
    monkeypatch: pytest.MonkeyPatch,
    _isolated_workflow_globals: Path,
) -> None:
    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
        )
        _enqueue_text_to_sql_run(store, "run-1")
    finally:
        store.close()

    outbox_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        EventStore,
        "finalize_run_with_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("primary unavailable")
        ),
    )
    monkeypatch.setattr(
        streamlit_api,
        "_enqueue_workflow_result_payload",
        lambda payload: outbox_calls.append(payload),
    )
    monkeypatch.setattr(
        streamlit_api,
        "_persist_workflow_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("supervisor terminalization must not use outbox fallback")
        ),
    )

    with pytest.raises(sqlite3.OperationalError, match="primary unavailable"):
        streamlit_api._terminalize_supervised_workflow(
            "run-1",
            None,
            None,
            {"status": "timed_out", "reason": "queue deadline"},
        )

    assert outbox_calls == []
    verify = EventStore(str(_isolated_workflow_globals))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == "queued"
        assert stored.result_seq is None
        assert verify.load_work_spec("run-1") is not None
        assert list(verify.list_after("run-1")) == []
    finally:
        verify.close()


def test_normal_worker_exit_adopts_pending_child_result_before_fallback(
    _isolated_workflow_globals: Path,
) -> None:
    from workflow.models import TextToSqlTerminalResult
    from workflow.result_outbox import WorkflowResultOutbox

    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
        )
        deadline = _enqueue_text_to_sql_run(store, "run-1")
        claim = store.claim_next_queued(
            "supervisor-current", 1, 1, 1, deadline
        )
        assert claim is not None
        generation = claim.claim.attempt_generation
    finally:
        store.close()

    terminal = TextToSqlTerminalResult.from_mapping(
        {
            "run_id": "run-1",
            "status": "succeeded",
            "reason_code": "",
            "sql": "SELECT 1",
            "generated": True,
            "approved": True,
            "executed": True,
            "dry_run": False,
            "audited": True,
            "data": [[1]],
            "columns": ["value"],
            "rows_affected": 1,
            "error": None,
            "execution": {
                "success": True,
                "data": [[1]],
                "columns": ["value"],
                "rows_affected": 1,
                "execution_time_ms": 1,
                "dry_run_only": False,
                "skipped_execution": False,
                "sql_query": "SELECT 1",
                "applied_row_limit": 10,
            },
            "audit": {"status": "logged", "log_id": "audit-1"},
            "persistence": {
                "status": "saved",
                "filename": "query.md",
                "path": "/tmp/query.md",
            },
        }
    ).to_mapping()
    payload = streamlit_api._build_workflow_result_event_payload(
        "run-1",
        terminal,
        "completed",
        artifacts={
            "final_output": terminal,
            "terminal_outcome": terminal,
        },
        snapshot={
            "workflow_name": "text_to_sql_pipeline",
            "session_id": "session-1",
        },
        terminal_outcome=terminal,
        run_incarnation="inc-1",
    )
    outbox_path = streamlit_api._workflow_result_outbox_path()
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        assert outbox.enqueue(
            payload,
            supervisor_id="supervisor-current",
            attempt_generation=generation,
        )
    finally:
        outbox.close()

    assert streamlit_api._terminalize_supervised_workflow(
        "run-1",
        "supervisor-current",
        generation,
        {"status": "succeeded", "reason": "WORKER_EXITED"},
    )

    verify = EventStore(str(_isolated_workflow_globals))
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.result_seq is not None
        event = verify.get_event("run-1", stored.result_seq)
        assert event is not None
        assert event.payload["terminal_outcome"] == terminal
        assert outbox.count() == 0
    finally:
        outbox.close()
        verify.close()


def test_worker_outbox_enqueue_marks_exact_result_pending_privately(
    monkeypatch: pytest.MonkeyPatch,
    _isolated_workflow_globals: Path,
) -> None:
    from workflow.result_outbox import WorkflowResultOutbox

    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
        )
        deadline = _enqueue_text_to_sql_run(store, "run-1")
        claim = store.claim_next_queued(
            "supervisor-current", 1, 1, 1, deadline
        )
        assert claim is not None
        generation = claim.claim.attempt_generation
    finally:
        store.close()

    monkeypatch.setattr(
        streamlit_api,
        "_schedule_workflow_result_outbox_drain",
        lambda **_kwargs: True,
    )
    payload = _failed_result_payload()
    streamlit_api._enqueue_workflow_result_payload(
        payload,
        supervisor_id="supervisor-current",
        attempt_generation=generation,
    )

    verify = EventStore(str(_isolated_workflow_globals))
    outbox = WorkflowResultOutbox(
        str(streamlit_api._workflow_result_outbox_path())
    )
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == "result_pending"
        assert stored.worker_lease_expires_at_ms is None
        [entry] = outbox.list_pending()
        assert entry.supervisor_id == "supervisor-current"
        assert entry.attempt_generation == generation
        assert entry.payload == payload
        assert "supervisor_id" not in entry.payload
        assert "attempt_generation" not in entry.payload
    finally:
        outbox.close()
        verify.close()


@pytest.mark.parametrize("late_status", ["cancelled", "timed_out"])
def test_result_pending_worker_result_is_single_winner_over_late_barrier(
    monkeypatch: pytest.MonkeyPatch,
    _isolated_workflow_globals: Path,
    late_status: str,
) -> None:
    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
        )
        deadline = _enqueue_text_to_sql_run(store, "run-1")
        claim = store.claim_next_queued(
            "supervisor-current", 1, 1, 1, deadline
        )
        assert claim is not None
        generation = claim.claim.attempt_generation
    finally:
        store.close()

    monkeypatch.setattr(
        streamlit_api,
        "_schedule_workflow_result_outbox_drain",
        lambda **_kwargs: True,
    )
    streamlit_api._enqueue_workflow_result_payload(
        _failed_result_payload(),
        supervisor_id="supervisor-current",
        attempt_generation=generation,
    )
    assert streamlit_api._terminalize_supervised_workflow(
        "run-1",
        "supervisor-current",
        generation,
        {"status": late_status, "reason": "late barrier"},
    )
    assert streamlit_api._drain_workflow_result_outbox_batch(
        path=streamlit_api._workflow_result_outbox_path()
    ) == (1, True)

    verify = EventStore(str(_isolated_workflow_globals))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == "failed"
        assert len(
            [
                event
                for event in verify.list_after("run-1")
                if event.event_type == "WORKFLOW_RESULT"
            ]
        ) == 1
    finally:
        verify.close()


def test_delayed_old_generation_result_is_fenced_and_retired(
    _isolated_workflow_globals: Path,
) -> None:
    from workflow.result_outbox import WorkflowResultOutbox

    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
        )
        deadline = _enqueue_text_to_sql_run(store, "run-1")
        claim = store.claim_next_queued(
            "supervisor-current", 1, 1, 1, deadline
        )
        assert claim is not None
        old_generation = claim.claim.attempt_generation
    finally:
        store.close()

    outbox_path = streamlit_api._workflow_result_outbox_path()
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(
            _failed_result_payload(),
            supervisor_id="supervisor-current",
            attempt_generation=old_generation,
        )
    finally:
        outbox.close()

    store = EventStore(str(_isolated_workflow_globals))
    try:
        new_generation = store.fence_worker_attempt(
            "run-1",
            "supervisor-current",
            old_generation,
            deadline,
        )
        assert new_generation is not None
    finally:
        store.close()

    assert streamlit_api._drain_workflow_result_outbox_batch(
        path=outbox_path
    ) == (1, True)

    verify = EventStore(str(_isolated_workflow_globals))
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == "running"
        assert stored.attempt_started_at_ms == new_generation
        assert stored.result_seq is None
        assert list(verify.list_after("run-1")) == []
        assert outbox.count() == 0
    finally:
        outbox.close()
        verify.close()


def test_generic_crash_adopts_exact_pending_worker_result(
    _isolated_workflow_globals: Path,
) -> None:
    from workflow.result_outbox import WorkflowResultOutbox

    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="agui",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "simple_research"
        )
        deadline = int(time.time() * 1_000) + 60_000
        spec = {
            "spec_version": 1,
            "workflow_path": str(
                Path("workflow_pipelines/simple_research.yaml").resolve()
            ),
            "parameters": {"topic": "crash adoption"},
            "session_id": "session-1",
            "client_id": "owner:tenant-a:alice",
            "use_enhanced": True,
            "enable_telemetry": False,
            "run_incarnation": "inc-1",
            "deadline_at_ms": deadline,
        }
        store.enqueue_run("run-1", spec, deadline, 10)
        claim = store.claim_next_queued(
            "supervisor-current", 1, 1, 1, deadline
        )
        assert claim is not None
        generation = claim.claim.attempt_generation
    finally:
        store.close()

    payload = streamlit_api._build_workflow_result_event_payload(
        "run-1",
        {"answer": "adopted"},
        "completed",
        snapshot={"workflow_name": "simple_research"},
        run_incarnation="inc-1",
    )
    outbox_path = streamlit_api._workflow_result_outbox_path()
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(
            payload,
            supervisor_id="supervisor-current",
            attempt_generation=generation,
        )
    finally:
        outbox.close()

    assert streamlit_api._terminalize_supervised_workflow(
        "run-1",
        "supervisor-current",
        generation,
        {"status": "failed", "reason": "WORKER_CRASHED"},
    )

    verify = EventStore(str(_isolated_workflow_globals))
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == "succeeded"
        [event] = [
            event
            for event in verify.list_after("run-1")
            if event.event_type == "WORKFLOW_RESULT"
        ]
        assert event.payload == payload
        assert outbox.count() == 0
    finally:
        outbox.close()
        verify.close()


@pytest.mark.parametrize("late_status", ["cancelled", "timed_out"])
def test_fenced_cancel_or_timeout_never_adopts_old_generic_result(
    _isolated_workflow_globals: Path,
    late_status: str,
) -> None:
    from workflow.result_outbox import WorkflowResultOutbox

    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="agui",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "simple_research"
        )
        deadline = int(time.time() * 1_000) + 60_000
        spec = {
            "spec_version": 1,
            "workflow_path": str(
                Path("workflow_pipelines/simple_research.yaml").resolve()
            ),
            "parameters": {"topic": "stale result"},
            "session_id": "session-1",
            "client_id": "owner:tenant-a:alice",
            "use_enhanced": True,
            "enable_telemetry": False,
            "run_incarnation": "inc-1",
            "deadline_at_ms": deadline,
        }
        store.enqueue_run("run-1", spec, deadline, 10)
        claim = store.claim_next_queued(
            "supervisor-current", 1, 1, 1, deadline
        )
        assert claim is not None
        old_generation = claim.claim.attempt_generation
    finally:
        store.close()

    payload = streamlit_api._build_workflow_result_event_payload(
        "run-1",
        {"answer": "stale"},
        "completed",
        snapshot={"workflow_name": "simple_research"},
        run_incarnation="inc-1",
    )
    outbox_path = streamlit_api._workflow_result_outbox_path()
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(
            payload,
            supervisor_id="supervisor-current",
            attempt_generation=old_generation,
        )
    finally:
        outbox.close()

    store = EventStore(str(_isolated_workflow_globals))
    try:
        new_generation = store.fence_worker_attempt(
            "run-1",
            "supervisor-current",
            old_generation,
            deadline,
        )
        assert new_generation is not None
    finally:
        store.close()

    assert streamlit_api._terminalize_supervised_workflow(
        "run-1",
        "supervisor-current",
        new_generation,
        {"status": late_status, "reason": "fenced barrier"},
    )
    assert streamlit_api._drain_workflow_result_outbox_batch(
        path=outbox_path
    ) == (1, True)

    verify = EventStore(str(_isolated_workflow_globals))
    try:
        stored = verify.get_run("run-1")
        assert stored is not None
        assert stored.status == late_status
        assert not any(
            event.event_type == "WORKFLOW_RESULT"
            for event in verify.list_after("run-1")
        )
    finally:
        verify.close()


def test_normal_exit_retries_result_pending_marker_before_releasing_claim(
    monkeypatch: pytest.MonkeyPatch,
    _isolated_workflow_globals: Path,
) -> None:
    from workflow.result_outbox import WorkflowResultOutbox

    store = EventStore(str(_isolated_workflow_globals))
    try:
        store.create_run(
            "run-1",
            "session-1",
            Principal("alice", "tenant-a", frozenset({"user"})),
            run_kind="text_to_sql",
        )
        assert store.reserve_workflow_run(
            "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
        )
        deadline = _enqueue_text_to_sql_run(store, "run-1")
        claim = store.claim_next_queued(
            "supervisor-current", 1, 1, 1, deadline
        )
        assert claim is not None
        generation = claim.claim.attempt_generation
    finally:
        store.close()

    outbox_path = streamlit_api._workflow_result_outbox_path()
    outbox = WorkflowResultOutbox(str(outbox_path))
    try:
        outbox.enqueue(
            _failed_result_payload(),
            supervisor_id="supervisor-current",
            attempt_generation=generation,
        )
    finally:
        outbox.close()

    real_drain = streamlit_api._drain_workflow_result_outbox_batch
    real_mark = EventStore.mark_worker_result_pending
    mark_calls = 0

    def flaky_mark(self, run_id, supervisor_id, attempt_generation):
        nonlocal mark_calls
        mark_calls += 1
        if mark_calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_mark(self, run_id, supervisor_id, attempt_generation)

    monkeypatch.setattr(
        streamlit_api,
        "_drain_workflow_result_outbox_batch",
        lambda **_kwargs: (0, True),
    )
    monkeypatch.setattr(
        streamlit_api,
        "_schedule_workflow_result_outbox_drain",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(EventStore, "mark_worker_result_pending", flaky_mark)

    outcome = {"status": "succeeded", "reason": "WORKER_EXITED"}
    assert not streamlit_api._terminalize_supervised_workflow(
        "run-1", "supervisor-current", generation, outcome
    )
    running = EventStore(str(_isolated_workflow_globals))
    try:
        assert running.get_run("run-1").status == "running"
    finally:
        running.close()

    assert streamlit_api._terminalize_supervised_workflow(
        "run-1", "supervisor-current", generation, outcome
    )
    pending = EventStore(str(_isolated_workflow_globals))
    try:
        stored = pending.get_run("run-1")
        assert stored.status == "result_pending"
        assert stored.worker_lease_expires_at_ms is None
        assert pending.claim_next_queued(
            "supervisor-other", 1, 1, 1, deadline
        ) is None
    finally:
        pending.close()

    monkeypatch.setattr(
        streamlit_api,
        "_drain_workflow_result_outbox_batch",
        real_drain,
    )
    monkeypatch.setattr(EventStore, "mark_worker_result_pending", real_mark)
    assert real_drain(path=outbox_path) == (1, True)
    verify = EventStore(str(_isolated_workflow_globals))
    try:
        assert verify.get_run("run-1").status == "failed"
    finally:
        verify.close()


def test_result_pending_projects_as_public_running(
    monkeypatch: pytest.MonkeyPatch,
    _isolated_workflow_globals: Path,
) -> None:
    from backend.fastapi_app.agui import service

    principal = Principal("alice", "tenant-a", frozenset({"user"}))
    store = EventStore(str(_isolated_workflow_globals))
    store.create_run(
        "run-1",
        "session-1",
        principal,
        run_kind="text_to_sql",
    )
    assert store.reserve_workflow_run(
        "run-1", "inc-1", "session-1", "text_to_sql_pipeline"
    )
    deadline = _enqueue_text_to_sql_run(store, "run-1")
    claim = store.claim_next_queued(
        "supervisor-current", 1, 1, 1, deadline
    )
    assert claim is not None
    assert store.mark_worker_result_pending(
        "run-1",
        "supervisor-current",
        claim.claim.attempt_generation,
    )

    class Manager:
        @staticmethod
        def get_workflow_status(_run_id):
            return None

    monkeypatch.setattr(service, "_AGUI_EVENT_STORE", store)
    monkeypatch.setattr(service, "_WF_MANAGER", Manager())
    try:
        assert _compatibility_status("result_pending") is RunStatus.RUNNING
        status = service.handle_service_action(
            "workflows.status",
            {"run_id": "run-1"},
            principal,
        )
        assert status["status"]["status"] == "running"
        assert "terminal_outcome" not in status["status"]

        result = service.handle_service_action(
            "workflows.result",
            {"run_id": "run-1"},
            principal,
        )
        assert result == {
            "result": None,
            "status": "running",
            "success": False,
            "error": None,
        }
    finally:
        store.close()


def test_lifespan_starts_supervisor_and_preserves_durable_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_main = sys.modules.pop("backend.fastapi_app.main", None)
    old_store = getattr(old_main, "store", None)
    if isinstance(old_store, EventStore):
        old_store.close()
    package = importlib.import_module("backend.fastapi_app")
    monkeypatch.delattr(package, "main", raising=False)
    state_files = importlib.import_module("workflow.state_files")
    monkeypatch.setattr(
        state_files,
        "default_state_database_path",
        lambda *_args, **_kwargs: tmp_path / "import-events.db",
    )
    main = importlib.import_module("backend.fastapi_app.main")

    event_path = tmp_path / "main-events.db"
    replacement = EventStore(str(event_path))
    principal = Principal("alice", "tenant-a", frozenset({"user"}))
    replacement.create_run("run-1", "thread-1", principal, run_kind="agui")
    deadline = int(time.time() * 1_000) + 60_000
    replacement.enqueue_run(
        "run-1",
        {
            "spec_version": 1,
            "workflow_path": str(
                Path("workflow_pipelines/simple_research.yaml").resolve()
            ),
            "parameters": {"topic": "restart"},
            "session_id": "thread-1",
            "client_id": "owner:tenant:alice",
            "use_enhanced": True,
            "enable_telemetry": False,
            "run_incarnation": "inc-1",
            "deadline_at_ms": deadline,
        },
        deadline,
        10,
    )
    supervisor = _FakeSupervisor()
    monkeypatch.setattr(main, "store", replacement)
    monkeypatch.setattr(main, "validate_auth_configuration", lambda: None)
    monkeypatch.setattr(main, "_drain_workflow_results_before_reconcile", lambda: frozenset())
    monkeypatch.setattr(
        main,
        "configure_workflow_process_supervisor",
        lambda store: supervisor,
    )

    async def exercise() -> None:
        async with main._lifespan(main.app):
            assert supervisor.started == 1
            assert replacement.get_run("run-1").status == "queued"
        assert supervisor.stopped == 1

    try:
        asyncio.run(exercise())
    finally:
        replacement.close()
