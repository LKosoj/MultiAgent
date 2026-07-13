from __future__ import annotations

import base64
import gzip
from pathlib import Path
import sys
import types

import pytest

from backend.fastapi_app.agui.auth import Principal
from backend.fastapi_app.agui.report_renderer import render_static_report
from backend.fastapi_app.agui.store import EventStore
from test_text_to_sql_agui_workflow_contract import (
    _WorkflowManagerStub,
    _load_service_with_stubs,
)


def _user(
    subject: str = "alice",
    tenant_id: str = "tenant-a",
) -> Principal:
    return Principal(
        subject=subject,
        tenant_id=tenant_id,
        roles=frozenset({"user"}),
    )


def _admin() -> Principal:
    return Principal(
        subject="operator",
        tenant_id="ops",
        roles=frozenset({"admin"}),
    )


class _WorkflowReports:
    def __init__(self, report=None, final_output=None) -> None:
        self.active_runs = {"run-1": {"session_id": "session-1"}}
        if report is not None:
            self.active_runs["run-1"]["report"] = report
        self.final_output = final_output
        self.artifact_reads = 0
        self.snapshot_reads = 0

    def get_active_run_snapshot(self, run_id):
        self.snapshot_reads += 1
        return dict(self.active_runs[run_id])

    def get_workflow_artifacts(self, run_id):
        self.artifact_reads += 1
        if self.final_output is None:
            return None
        return types.SimpleNamespace(final_output=self.final_output)

    def update_active_run(self, run_id, update):
        self.active_runs[run_id].update(update)


def _decode_report(report) -> str:
    return gzip.decompress(
        base64.b64decode(report["content_b64_gzip"], validate=True)
    ).decode("utf-8")


def test_workflow_report_regenerates_legacy_cache_with_static_renderer(
    monkeypatch,
) -> None:
    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())
    manager = _WorkflowReports(
        report={"mime_type": "text/html", "base64_gzip": "legacy"},
        final_output={
            "type": "sql_result",
            "sql_query": "SELECT '<script>bad()</script>'",
            "explanation": "structured explanation",
            "execution_result": {"columns": ["value"], "data": [[1]]},
        },
    )

    report = service._workflow_generate_report(manager, "run-1")

    assert report["renderer_version"] == "static-v1"
    assert report["mime_type"] == "text/html; charset=utf-8"
    assert report["content_sha256"]
    assert "<script>" not in _decode_report(report)
    assert "&lt;script&gt;bad()&lt;/script&gt;" in _decode_report(report)
    assert manager.active_runs["run-1"]["report"] == report


def test_workflow_report_reuses_only_digest_valid_static_cache(monkeypatch) -> None:
    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())
    cached = dict(render_static_report(title="cached").to_mapping())
    manager = _WorkflowReports(report=cached)

    assert service._workflow_generate_report(manager, "run-1") == cached
    assert manager.artifact_reads == 0


def test_workflow_report_never_returns_legacy_cache_without_source(monkeypatch) -> None:
    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())
    manager = _WorkflowReports(
        report={"mime_type": "text/html", "base64_gzip": "legacy"},
    )

    with pytest.raises(ValueError, match="Workflow not found"):
        service._workflow_generate_report(manager, "run-1")


def test_workflow_report_action_is_owner_scoped_before_report_read(
    monkeypatch,
    tmp_path,
) -> None:
    manager = _WorkflowReports(final_output={"content": "owner report"})
    service = _load_service_with_stubs(monkeypatch, manager)
    store = EventStore(str(tmp_path / "events.db"))
    owner = _user()
    other = _user("bob", "tenant-b")
    store.create_run(
        "run-1",
        "thread-1",
        owner,
        run_kind="text_to_sql",
    )
    monkeypatch.setattr(service, "_AGUI_EVENT_STORE", store)
    try:
        owned = service.handle_service_action(
            "workflows.generate_report",
            {"run_id": "run-1"},
            principal=owner,
        )
        assert owned["report"]["renderer_version"] == "static-v1"
        assert manager.snapshot_reads == 1

        with pytest.raises(ValueError, match="run not found"):
            service.handle_service_action(
                "workflows.generate_report",
                {"run_id": "run-1"},
                principal=other,
            )
        assert manager.snapshot_reads == 1

        admin_result = service.handle_service_action(
            "workflows.generate_report",
            {"run_id": "run-1"},
            principal=_admin(),
        )
        assert admin_result["report"]["renderer_version"] == "static-v1"
        assert manager.snapshot_reads == 2
    finally:
        store.close()


def test_telemetry_report_uses_static_renderer(monkeypatch, tmp_path) -> None:
    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())
    monkeypatch.setattr(service, "_project_root", lambda: tmp_path)
    helpers = types.ModuleType("telemetry.helpers")
    helpers.get_trace_status = lambda spans: {"status": "completed"}
    monkeypatch.setitem(sys.modules, "telemetry.helpers", helpers)

    class Telemetry:
        def load_trace_file(self, run_id):
            return {
                "spans": [{
                    "name": "agent_run_demo",
                    "attributes": {
                        "session_id": "session-1",
                        "output.value": {"content": "<script>bad()</script>"},
                    },
                    "events": [],
                }]
            }

    report = service._telemetry_generate_report(
        Telemetry(),
        "run-telemetry",
        persist=False,
    )

    assert report["renderer_version"] == "static-v1"
    assert report["content_sha256"]
    assert "<script>" not in _decode_report(report)
    assert "&lt;script&gt;bad()&lt;/script&gt;" in _decode_report(report)


def test_retention_status_requires_admin_role(monkeypatch) -> None:
    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())

    with pytest.raises(PermissionError, match="requires role 'admin'"):
        service.handle_service_action(
            "telemetry.retention.status",
            {},
            principal=_user(),
        )


def test_retention_status_reports_explicit_never_run_state(monkeypatch) -> None:
    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())

    class Store:
        def get_operational_retention_state(self, scope):
            assert scope == "t14-operational-retention-v1"
            return None

    monkeypatch.setattr(service, "_AGUI_EVENT_STORE", Store())

    result = service.handle_service_action(
        "telemetry.retention.status",
        {},
        principal=_admin(),
    )

    assert result == {
        "retention": {
            "scope": "t14-operational-retention-v1",
            "status": "never_run",
            "never_run": True,
            "not_due": False,
            "last_attempt_at_ms": None,
            "last_success_at_ms": None,
            "next_due_at_ms": None,
            "counters": {},
            "error": None,
            "lease": {
                "owner_id": None,
                "generation": 0,
                "expires_at_ms": None,
            },
        }
    }


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(
            types.SimpleNamespace(
                scope="t14-operational-retention-v1",
                status="succeeded",
                lease_owner=None,
                lease_generation=3,
                lease_expires_at_ms=None,
                last_attempt_at_ms=1_000,
                last_success_at_ms=1_100,
                next_due_at_ms=9_999_999_999_999,
                counters={
                    "history_deleted_rows": 4,
                    "trace_deleted_files": 2,
                    "trace_deleted_bytes": 128,
                },
                last_error=None,
            ),
            {
                "status": "succeeded",
                "never_run": False,
                "not_due": True,
                "last_attempt_at_ms": 1_000,
                "last_success_at_ms": 1_100,
                "next_due_at_ms": 9_999_999_999_999,
                "counters": {
                    "history_deleted_rows": 4,
                    "trace_deleted_files": 2,
                    "trace_deleted_bytes": 128,
                },
                "error": None,
                "lease": {
                    "owner_id": None,
                    "generation": 3,
                    "expires_at_ms": None,
                },
            },
            id="success-not-due",
        ),
        pytest.param(
            types.SimpleNamespace(
                scope="t14-operational-retention-v1",
                status="failed",
                lease_owner=None,
                lease_generation=4,
                lease_expires_at_ms=None,
                last_attempt_at_ms=2_000,
                last_success_at_ms=1_100,
                next_due_at_ms=1,
                counters={
                    "history_failures": 1,
                    "trace_cleanup_failures": 2,
                },
                last_error="trace cleanup failed",
            ),
            {
                "status": "failed",
                "never_run": False,
                "not_due": False,
                "last_attempt_at_ms": 2_000,
                "last_success_at_ms": 1_100,
                "next_due_at_ms": 1,
                "counters": {
                    "history_failures": 1,
                    "trace_cleanup_failures": 2,
                },
                "error": "trace cleanup failed",
                "lease": {
                    "owner_id": None,
                    "generation": 4,
                    "expires_at_ms": None,
                },
            },
            id="failure-due",
        ),
    ],
)
def test_retention_status_serializes_persisted_state(
    monkeypatch,
    state,
    expected,
) -> None:
    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())

    class Store:
        def get_operational_retention_state(self, scope):
            assert scope == "t14-operational-retention-v1"
            return state

    monkeypatch.setattr(service, "_AGUI_EVENT_STORE", Store())

    result = service.handle_service_action(
        "telemetry.retention.status",
        {},
        principal=_admin(),
    )

    assert result == {
        "retention": {
            "scope": "t14-operational-retention-v1",
            **expected,
        }
    }


def test_history_actions_delegate_to_principal_scoped_event_store(monkeypatch) -> None:
    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())
    principal = _user()

    class Store:
        def __init__(self) -> None:
            self.calls = []

        def list_text_to_sql_history(self, caller, *, limit, offset):
            self.calls.append(("list", caller, limit, offset))
            return [types.SimpleNamespace(to_mapping=lambda: {"run_id": "run-1"})]

        def clear_text_to_sql_history(self, caller):
            self.calls.append(("clear", caller))
            return 1

        def text_to_sql_history_analytics(self, caller):
            self.calls.append(("analytics", caller))
            return {"total": 1}

    store = Store()
    monkeypatch.setattr(service, "_AGUI_EVENT_STORE", store)

    listed = service.handle_service_action(
        "text_to_sql.history.list",
        {"limit": 10, "offset": 2},
        principal=principal,
    )
    cleared = service.handle_service_action(
        "text_to_sql.history.clear",
        {"confirm": True},
        principal=principal,
    )
    analytics = service.handle_service_action(
        "text_to_sql.history.analytics",
        {},
        principal=principal,
    )

    assert listed == {"entries": [{"run_id": "run-1"}]}
    assert cleared == {"cleared": 1}
    assert analytics == {"result": {"total": 1}}
    assert store.calls == [
        ("list", principal, 10, 2),
        ("clear", principal),
        ("analytics", principal),
    ]


def test_history_append_action_is_a_rejected_compatibility_adapter(
    monkeypatch,
) -> None:
    service = _load_service_with_stubs(monkeypatch, _WorkflowManagerStub())

    with pytest.raises(PermissionError, match="client history append is disabled"):
        service.handle_service_action(
            "text_to_sql.history.append",
            {"entry": {"run_id": "run-1"}},
            principal=_user(),
        )


def test_streamlit_page_uses_server_history_without_legacy_adapter() -> None:
    project_root = Path(__file__).resolve().parents[1]
    page = (project_root / "streamlit_app/pages/05_Text_to_SQL.py").read_text(
        encoding="utf-8"
    )

    assert "legacy_text_to_sql_history" not in page
    assert "LegacyTextToSqlHistory" not in page
    assert "st.session_state.sql_history" not in page
    assert "logs/sql_history.jsonl" not in page
    assert "client.list_history(" in page
    assert "client.clear_history(" in page
