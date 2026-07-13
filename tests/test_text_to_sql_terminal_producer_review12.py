"""Producer-side execution-attempt semantics for terminal results."""

from __future__ import annotations

import importlib

import pytest


def test_finalizer_preserves_preflight_skip_as_nonexecuted_failure(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: {
            "success": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": 1,
            "error_message": "Unsafe query.",
            "dry_run_only": False,
            "skipped_execution": True,
            "sql_query": "SELECT 1",
            "applied_row_limit": 10,
            "safety_issues": [
                {"issue_type": "UNSAFE", "description": "blocked"}
            ],
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
        lambda *args, **kwargs: pytest.fail("preflight failure must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTION_FAILED"
    assert result["executed"] is False
    assert result["execution"]["success"] is False
    assert result["execution"]["skipped_execution"] is True
    assert result["persistence"] == {"status": "not_attempted"}


def test_secure_executor_marks_unsafe_preflight_as_skipped(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    db_exec = importlib.import_module("custom_tools.text_to_sql.core._db_exec")
    monkeypatch.setattr(
        core,
        "sql_safety_check",
        lambda *_args, **_kwargs: {
            "is_safe": False,
            "issues": [{"issue_type": "UNSAFE", "description": "blocked"}],
        },
    )
    monkeypatch.setattr(
        core,
        "get_plugin",
        lambda *_args, **_kwargs: pytest.fail("preflight failure must not connect"),
    )

    result = db_exec.secure_db_executor(
        "SELECT 1",
        10,
        "sqlite:///unused.db",
        dry_run_only=False,
        sql_validator=object(),
    )

    assert result["success"] is False
    assert result["dry_run_only"] is False
    assert result["skipped_execution"] is True
