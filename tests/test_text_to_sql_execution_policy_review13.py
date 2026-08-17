from __future__ import annotations

import importlib

import pytest

from db_plugins.base import (
    Capability,
    DatabaseCapabilities,
    EnforcementMode,
    PluginHealth,
)


@pytest.fixture(autouse=True)
def _allow_typed_finalizer(monkeypatch):
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        terminal,
        "_pre_execution_gate_allowed",
        lambda **_kwargs: True,
    )


class _AdmittedPluginDouble:
    dialect = "sqlite"

    def get_capabilities(self, _dsn=None):
        native = Capability.supported(EnforcementMode.DRIVER, "TEST_NATIVE")
        return DatabaseCapabilities(
            dialect=self.dialect,
            read_only=native,
            statement_timeout=native,
            cancellation=native,
            explain=native,
            introspection=native,
            composite_fk_introspection=Capability.unsupported("TEST_NOT_REQUIRED"),
            parameter_binding=Capability.unsupported("TEST_NOT_REQUIRED"),
        )

    def probe_capabilities(self, _conn=None, dsn=None):
        return PluginHealth(
            self.dialect,
            self.get_capabilities(dsn),
            True,
            ("TEST_PROBE_OK",),
        )

    def set_statement_timeout(self, _conn, _timeout_ms):
        return None


def _safe_executor(monkeypatch, plugin):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    monkeypatch.setattr(core, "get_plugin", lambda _dsn: plugin)
    monkeypatch.setattr(
        core,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    return core


def test_executor_ignores_plugin_supplied_dry_run_and_skip_markers(monkeypatch):
    calls = []

    class Plugin(_AdmittedPluginDouble):
        def connect(self, dsn):
            calls.append(("connect", dsn))
            return object()

        def execute_select(self, _conn, _sql, row_limit):
            calls.append(("strategy", row_limit))
            return {
                "success": True,
                "data": [[1]],
                "columns": ["one"],
                "rows_affected": 1,
                "execution_time_ms": 1,
                "error_message": None,
                "dry_run_only": True,
                "skipped_execution": True,
            }

        def close(self, _conn):
            calls.append(("close", None))

    core = _safe_executor(monkeypatch, Plugin())

    result = core.secure_db_executor(
        "SELECT 1",
        row_limit=10,
        dsn="sqlite:///unused.db",
        dry_run_only=False,
    )

    assert result["success"] is True
    assert result["dry_run_only"] is False
    assert result["skipped_execution"] is False
    assert [call[0] for call in calls] == ["connect", "strategy", "close"]


def test_executor_marks_connect_failure_as_pre_strategy_skip(monkeypatch):
    class Plugin(_AdmittedPluginDouble):
        def connect(self, _dsn):
            raise RuntimeError("connect failed")

    core = _safe_executor(monkeypatch, Plugin())

    result = core.secure_db_executor(
        "SELECT 1",
        row_limit=10,
        dsn="sqlite:///unused.db",
        dry_run_only=False,
    )

    assert result["success"] is False
    assert result["dry_run_only"] is False
    assert result["skipped_execution"] is True


def test_executor_marks_timeout_setup_failure_as_pre_strategy_skip(monkeypatch):
    class Plugin(_AdmittedPluginDouble):
        def connect(self, _dsn):
            return object()

        def set_statement_timeout(self, _conn, _timeout_ms):
            raise RuntimeError("timeout setup failed")

        def execute_select(self, *_args, **_kwargs):
            raise AssertionError("strategy must not run")

        def close(self, _conn):
            return None

    core = _safe_executor(monkeypatch, Plugin())

    result = core.secure_db_executor(
        "SELECT 1",
        row_limit=10,
        dsn="sqlite:///unused.db",
        dry_run_only=False,
    )

    assert result["success"] is False
    assert result["skipped_execution"] is True


def test_executor_marks_strategy_failure_as_attempted_execution(monkeypatch):
    class Plugin(_AdmittedPluginDouble):
        def connect(self, _dsn):
            return object()

        def execute_select(self, *_args, **_kwargs):
            raise RuntimeError("strategy failed")

        def close(self, _conn):
            return None

    core = _safe_executor(monkeypatch, Plugin())

    result = core.secure_db_executor(
        "SELECT 1",
        row_limit=10,
        dsn="sqlite:///unused.db",
        dry_run_only=False,
    )

    assert result["success"] is False
    assert result["skipped_execution"] is False


def test_finalizer_validates_operator_effective_dry_run_policy(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "true")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *_args, **_kwargs: {
            "success": True,
            "data": [[1]],
            "columns": ["one"],
            "rows_affected": 1,
            "execution_time_ms": 1,
            "error_message": None,
            "dry_run_only": False,
            "skipped_execution": False,
            "sql_query": "SELECT 1",
            "applied_row_limit": 10,
        },
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda _entry: {"status": "logged", "log_id": "audit-1"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda **_kwargs: {"status": "saved", "filename": "q.md", "path": "/q.md"},
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTOR_CONTRACT_INVALID"
    assert "dry_run_only" in result["error"]


def test_finalizer_accepts_failed_pre_strategy_result_under_effective_dry_run(
    monkeypatch,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "true")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *_args, **_kwargs: {
            "success": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": 1,
            "error_message": "safety check failed",
            "dry_run_only": True,
            "skipped_execution": True,
            "sql_query": "SELECT 1",
            "applied_row_limit": 10,
        },
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda _entry: {"status": "logged", "log_id": "audit-1"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed dry-run must not persist")
        ),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTION_FAILED"
    assert result["dry_run"] is True
    assert result["executed"] is False
