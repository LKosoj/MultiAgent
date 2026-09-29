from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from workflow.engine import WorkflowEngine


def _sqlite_error(code: int, message: str) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


def test_checkpoint_write_retries_one_transient_sqlite_io_error(monkeypatch) -> None:
    engine = object.__new__(WorkflowEngine)
    context = SimpleNamespace(_deadline_budget=None)
    calls = 0
    sleeps: list[float] = []

    async def write() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _sqlite_error(sqlite3.SQLITE_IOERR_WRITE, "disk I/O error")
        return "saved"

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("workflow.engine.asyncio.sleep", sleep)

    result = asyncio.run(
        engine._await_state_manager_write(context, "step checkpoint", write)
    )

    assert result == "saved"
    assert calls == 2
    assert sleeps == [0.1]


def test_checkpoint_write_does_not_retry_other_sqlite_errors(monkeypatch) -> None:
    engine = object.__new__(WorkflowEngine)
    context = SimpleNamespace(_deadline_budget=None)
    calls = 0

    async def write() -> None:
        nonlocal calls
        calls += 1
        raise _sqlite_error(sqlite3.SQLITE_BUSY, "database is locked")

    async def forbidden_sleep(_delay: float) -> None:
        raise AssertionError("non-I/O SQLite errors must not be retried")

    monkeypatch.setattr("workflow.engine.asyncio.sleep", forbidden_sleep)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        asyncio.run(
            engine._await_state_manager_write(context, "step checkpoint", write)
        )

    assert calls == 1
