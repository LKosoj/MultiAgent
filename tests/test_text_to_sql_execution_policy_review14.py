"""Post-strategy result normalization is an attempted execution boundary."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _allow_typed_finalizer(monkeypatch):
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        terminal,
        "_pre_execution_gate_allowed",
        lambda **_kwargs: True,
    )

from db_plugins.base import (
    Capability,
    DatabaseCapabilities,
    EnforcementMode,
    PluginHealth,
)
from workflow.models import TEXT_TO_SQL_MAX_ERROR_LENGTH


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


class _HostileResult:
    def __iter__(self):
        raise ValueError(
            "invalid postgresql://review14:normalization-secret@db.example/app "
            + "x" * (TEXT_TO_SQL_MAX_ERROR_LENGTH + 100)
        )


class _UnstringifiableError(Exception):
    def __str__(self):
        raise RuntimeError("cannot stringify")


class _UnstringifiableHostileResult:
    def __iter__(self):
        raise _UnstringifiableError()


def _cyclic_result():
    cycle = []
    cycle.append(cycle)
    return {"data": cycle}


def _cyclic_safety_result():
    cycle = []
    cycle.append(cycle)
    return {
        "success": True,
        "data": [[1]],
        "columns": ["value"],
        "rows_affected": 1,
        "safety_issues": cycle,
    }


def _non_finite_result():
    return {
        "success": True,
        "data": [[float("nan")]],
        "columns": ["value"],
        "rows_affected": 1,
    }


def _non_string_key_result():
    return {
        "success": True,
        "data": [[1]],
        "columns": ["value"],
        "rows_affected": 1,
        1: "non-string-key",
    }


def _finalize_strategy_result(monkeypatch, strategy_result):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    calls = []

    class Plugin(_AdmittedPluginDouble):
        def connect(self, _dsn):
            calls.append("connect")
            return object()

        def execute_select(self, *_args, **_kwargs):
            calls.append("strategy")
            return strategy_result

        def close(self, _conn):
            calls.append("close")

    monkeypatch.setattr(core, "get_plugin", lambda _dsn: Plugin())
    monkeypatch.setattr(
        core,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )

    def audit_logger(_entry):
        calls.append("audit")
        return {"status": "logged", "log_id": "audit-review14"}

    monkeypatch.setattr(core, "audit_logger", audit_logger)
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda **_kwargs: pytest.fail("failed execution must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "sqlite:///unused.db",
        10,
        False,
        "session-review14",
        "run-review14",
    )
    return result, calls


@pytest.mark.parametrize(
    "strategy_result",
    [
        pytest.param(7, id="truthy-scalar"),
        pytest.param(0, id="zero"),
        pytest.param(False, id="false"),
        pytest.param(None, id="none"),
        pytest.param("", id="empty-string"),
        pytest.param([], id="empty-list"),
        pytest.param((), id="empty-tuple"),
        pytest.param(_cyclic_result(), id="cycle"),
        pytest.param(_cyclic_safety_result(), id="nested-cycle"),
        pytest.param(_non_finite_result(), id="non-finite"),
        pytest.param(_non_string_key_result(), id="non-string-key"),
    ],
)
def test_post_strategy_normalization_failure_is_not_reported_as_skipped(
    monkeypatch,
    strategy_result,
):
    result, calls = _finalize_strategy_result(monkeypatch, strategy_result)

    assert calls == ["connect", "strategy", "close", "audit"]
    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTION_FAILED"
    assert result["execution"]["skipped_execution"] is False
    assert result["executed"] is True
    assert result["dry_run"] is False
    assert result["execution"]["success"] is False
    assert result["execution"]["dry_run_only"] is False
    assert result["execution"]["data"] == []
    assert result["execution"]["columns"] == []
    assert result["execution"]["rows_affected"] == 0


def test_post_strategy_normalization_survives_unstringifiable_error(monkeypatch):
    result, calls = _finalize_strategy_result(
        monkeypatch,
        _UnstringifiableHostileResult(),
    )

    assert calls == ["connect", "strategy", "close", "audit"]
    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTION_FAILED"
    assert result["execution"]["skipped_execution"] is False
    assert result["executed"] is True
    assert result["execution"]["success"] is False
    assert result["execution"]["data"] == []
    assert result["execution"]["columns"] == []
    assert result["execution"]["rows_affected"] == 0
    assert result["persistence"] == {"status": "not_attempted"}


def test_post_strategy_normalization_error_is_bounded_and_sanitized(monkeypatch):
    result, calls = _finalize_strategy_result(monkeypatch, _HostileResult())

    assert calls == ["connect", "strategy", "close", "audit"]
    error = result["execution"]["error_message"]
    assert result["execution"]["skipped_execution"] is False
    assert len(error) <= TEXT_TO_SQL_MAX_ERROR_LENGTH
    assert "normalization-secret" not in error
    assert "postgresql://***:***@db.example/app" in error
