"""Contract tests for T8: ``capability_error`` and the relaxed
``applied_row_limit`` invariant.

Before T8 the terminal contract required ``applied_row_limit`` to always be a
positive integer and rejected ``capability_error``/``pre_execution_error_code``
as unknown fields. The executor (``custom_tools/text_to_sql/core/_db_exec.py``)
already emits both when a required database capability is unavailable, or
when the row-limit/statement-timeout env configuration is invalid before a
row limit can be established (``applied_row_limit=None``). That mismatch made
every such failure finalize as ``EXECUTOR_CONTRACT_INVALID`` (or raise an
unhandled ``TypeError`` from ``__post_init__``) instead of the intended
``EXECUTION_FAILED``.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap

import pytest


@pytest.fixture(autouse=True)
def _allow_typed_finalizer(monkeypatch):
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        terminal,
        "_pre_execution_gate_allowed",
        lambda **_kwargs: True,
    )

from workflow.models import (
    TextToSqlTerminalResult,
    text_to_sql_executor_contract_error,
)


def _executor_result(**overrides):
    result = {
        "success": True,
        "data": [[1]],
        "columns": ["value"],
        "rows_affected": 1,
        "execution_time_ms": 1,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": "SELECT 1",
        "applied_row_limit": 10,
    }
    result.update(overrides)
    return result


def _pre_execution_failure(pre_execution_error_code, **overrides):
    result = _executor_result(
        success=False,
        data=[],
        columns=[],
        rows_affected=0,
        skipped_execution=True,
        applied_row_limit=None,
        error_message="row limit configuration rejected",
        pre_execution_error_code=pre_execution_error_code,
    )
    result.update(overrides)
    return result


def _capability_failure(**overrides):
    result = _executor_result(
        success=False,
        data=[],
        columns=[],
        rows_affected=0,
        skipped_execution=True,
        error_message="Statement timeout capability is unavailable",
        capability_error={
            "capability": "statement_timeout",
            "reason_code": "DECLARED_TIMEOUT_SETTER_MISSING",
        },
    )
    result.update(overrides)
    return result


# --------------------------------------------------------------------------
# Boundary layer: text_to_sql_executor_contract_error
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pre_execution_error_code",
    ["INVALID_ROW_LIMIT", "INVALID_ROW_LIMIT_CAP", "INVALID_STATEMENT_TIMEOUT"],
)
def test_boundary_accepts_null_row_limit_with_pre_execution_error_code(
    pre_execution_error_code,
):
    result = _pre_execution_failure(pre_execution_error_code)

    error = text_to_sql_executor_contract_error(
        result,
        expected_dry_run_only=False,
        expected_sql_query="SELECT 1",
        expected_row_limit=10,
    )

    assert error is None


def test_boundary_rejects_null_row_limit_without_pre_execution_error_code():
    result = _executor_result(
        success=False,
        data=[],
        columns=[],
        rows_affected=0,
        skipped_execution=True,
        applied_row_limit=None,
        error_message="row limit configuration rejected",
    )

    error = text_to_sql_executor_contract_error(
        result,
        expected_dry_run_only=False,
        expected_sql_query="SELECT 1",
        expected_row_limit=10,
    )

    assert error is not None
    assert "applied_row_limit" in error


def test_boundary_rejects_null_row_limit_with_pre_execution_error_code_when_success_true():
    """success=True must not be able to smuggle an unbounded result set past the
    row-limit control by pairing applied_row_limit=None with
    pre_execution_error_code (which is only legitimate for a failed
    pre-execution abort)."""
    result = _pre_execution_failure("INVALID_ROW_LIMIT", success=True)

    error = text_to_sql_executor_contract_error(
        result,
        expected_dry_run_only=False,
        expected_sql_query="SELECT 1",
        expected_row_limit=10,
    )

    assert error is not None
    assert "applied_row_limit" in error


def test_boundary_accepts_capability_error_alongside_valid_row_limit():
    result = _capability_failure()

    error = text_to_sql_executor_contract_error(
        result,
        expected_dry_run_only=False,
        expected_sql_query="SELECT 1",
        expected_row_limit=10,
    )

    assert error is None


@pytest.mark.parametrize(
    "capability_error",
    [
        {"capability": "statement_timeout"},
        {"capability": "", "reason_code": "X"},
        {"capability": "statement_timeout", "reason_code": "X", "extra": 1},
        "not-a-dict",
    ],
)
def test_boundary_rejects_malformed_capability_error(capability_error):
    result = _capability_failure(capability_error=capability_error)

    error = text_to_sql_executor_contract_error(
        result,
        expected_dry_run_only=False,
        expected_sql_query="SELECT 1",
        expected_row_limit=10,
    )

    assert error is not None


# --------------------------------------------------------------------------
# Nested layer: TextToSqlTerminalResult.assert_invariants
# --------------------------------------------------------------------------


def _terminal_payload(execution, **overrides):
    payload = {
        "run_id": "run-1",
        "status": "failed",
        "reason_code": "EXECUTION_FAILED",
        "sql": "SELECT 1",
        "generated": True,
        "approved": True,
        "executed": False,
        "dry_run": False,
        "audited": True,
        "data": [],
        "columns": [],
        "rows_affected": 0,
        "error": "execution failed",
        "execution": execution,
        "audit": {"status": "logged", "log_id": "audit-1"},
        "persistence": {"status": "not_attempted"},
    }
    payload.update(overrides)
    return payload


def _failed_execution_evidence(**overrides):
    execution = {
        "success": False,
        "data": [],
        "columns": [],
        "rows_affected": 0,
        "execution_time_ms": 1,
        "dry_run_only": False,
        "skipped_execution": True,
        "sql_query": "SELECT 1",
        "applied_row_limit": None,
        "error_message": "row limit configuration rejected",
    }
    execution.update(overrides)
    return execution


@pytest.mark.parametrize(
    "pre_execution_error_code",
    ["INVALID_ROW_LIMIT", "INVALID_ROW_LIMIT_CAP", "INVALID_STATEMENT_TIMEOUT"],
)
def test_terminal_result_accepts_null_row_limit_with_pre_execution_error_code(
    pre_execution_error_code,
):
    execution = _failed_execution_evidence(
        pre_execution_error_code=pre_execution_error_code
    )

    outcome = TextToSqlTerminalResult.from_mapping(_terminal_payload(execution))

    assert outcome.status.value == "failed"
    assert outcome.reason_code == "EXECUTION_FAILED"
    assert outcome.execution["applied_row_limit"] is None


def test_terminal_result_still_rejects_null_row_limit_without_pre_execution_error_code():
    execution = _failed_execution_evidence()

    with pytest.raises(TypeError, match="applied_row_limit"):
        TextToSqlTerminalResult.from_mapping(_terminal_payload(execution))


def test_terminal_result_rejects_null_row_limit_with_pre_execution_error_code_when_success_true():
    execution = _failed_execution_evidence(
        pre_execution_error_code="INVALID_ROW_LIMIT",
        success=True,
    )

    with pytest.raises(TypeError, match="applied_row_limit"):
        TextToSqlTerminalResult.from_mapping(_terminal_payload(execution))


def test_terminal_result_accepts_capability_error_alongside_valid_row_limit():
    execution = {
        "success": False,
        "data": [],
        "columns": [],
        "rows_affected": 0,
        "execution_time_ms": 1,
        "dry_run_only": False,
        "skipped_execution": True,
        "sql_query": "SELECT 1",
        "applied_row_limit": 10,
        "error_message": "Statement timeout capability is unavailable",
        "capability_error": {
            "capability": "statement_timeout",
            "reason_code": "DECLARED_TIMEOUT_SETTER_MISSING",
        },
    }

    outcome = TextToSqlTerminalResult.from_mapping(_terminal_payload(execution))

    assert outcome.reason_code == "EXECUTION_FAILED"
    assert outcome.execution["capability_error"] == {
        "capability": "statement_timeout",
        "reason_code": "DECLARED_TIMEOUT_SETTER_MISSING",
    }


# --------------------------------------------------------------------------
# End-to-end: custom_tools.text_to_sql.core._terminal.finalize_text_to_sql_run
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pre_execution_error_code",
    ["INVALID_ROW_LIMIT", "INVALID_ROW_LIMIT_CAP", "INVALID_STATEMENT_TIMEOUT"],
)
def test_finalize_run_reports_pre_execution_failure_as_execution_failed(
    monkeypatch,
    pre_execution_error_code,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")

    executor_result = _pre_execution_failure(pre_execution_error_code)
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: executor_result,
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-pre-execution"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda **kwargs: pytest.fail("failed execution must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1", "one", "sqlite:///unused.db", 10, False,
        "session-1", "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTION_FAILED"
    assert result["executed"] is False
    assert result["execution"]["applied_row_limit"] is None
    assert result["execution"]["pre_execution_error_code"] == pre_execution_error_code


def test_finalize_run_reports_capability_error_as_execution_failed(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")

    executor_result = _capability_failure()
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: executor_result,
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-capability"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda **kwargs: pytest.fail("failed execution must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1", "one", "sqlite:///unused.db", 10, False,
        "session-1", "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTION_FAILED"
    assert result["execution"]["capability_error"] == {
        "capability": "statement_timeout",
        "reason_code": "DECLARED_TIMEOUT_SETTER_MISSING",
    }


# --------------------------------------------------------------------------
# Total-ness: the contract layer's defensive helpers must never let a
# hostile ``SystemExit``-raising value escape (SystemExit is a
# BaseException, not an Exception, so an ``except Exception`` guard would
# let it propagate out of these documented "never raise" helpers).
# --------------------------------------------------------------------------


def test_contract_layer_absorbs_hostile_systemexit_values():
    script = textwrap.dedent(
        """
        from workflow.models import (
            bound_text_to_sql_error,
            text_to_sql_type_name,
            text_to_sql_executor_contract_error,
        )

        class HostileMeta(type):
            def __getattribute__(cls, name):
                if name == "__name__":
                    raise SystemExit(1)
                return super().__getattribute__(name)

            def __str__(cls):
                raise SystemExit(1)

            def __repr__(cls):
                raise SystemExit(1)

        class Hostile(metaclass=HostileMeta):
            def __str__(self):
                raise SystemExit(1)

            def __repr__(self):
                raise SystemExit(1)

        hostile = Hostile()

        bound = bound_text_to_sql_error(hostile)
        assert isinstance(bound, str) and bound, bound

        type_name = text_to_sql_type_name(hostile)
        assert isinstance(type_name, str) and type_name, type_name

        class HostileErrorMessage(str):
            def strip(self):
                raise SystemExit(1)

        executor_result = {
            "success": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": 1,
            "error_message": HostileErrorMessage("boom"),
            "dry_run_only": False,
            "skipped_execution": False,
            "sql_query": "SELECT 1",
            "applied_row_limit": 10,
        }

        error = text_to_sql_executor_contract_error(
            executor_result,
            expected_dry_run_only=False,
            expected_sql_query="SELECT 1",
            expected_row_limit=10,
        )
        assert isinstance(error, str) and error, error
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
