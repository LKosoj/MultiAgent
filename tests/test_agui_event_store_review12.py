from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

import backend.fastapi_app.agui.store as store_module
from workflow import state_files
import workflow.result_outbox as outbox_module
from workflow.result_outbox import WorkflowResultOutbox


EventStore = store_module.EventStore


def _external_write_lock_result(db_path) -> str:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], timeout=0.1)
try:
    connection.execute("PRAGMA busy_timeout=100")
    connection.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError:
    print("locked")
else:
    print("acquired")
    connection.rollback()
finally:
    connection.close()
""",
            str(db_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return probe.stdout.strip()


def _create_current_store(path) -> None:
    store = EventStore(str(path))
    store.close()


def test_event_store_never_opens_raw_sqlite_files_after_connection_is_live(
    tmp_path,
    monkeypatch,
):
    raw_calls = []
    real_prepare = state_files._prepare_explicit_sqlite_file
    real_secure = state_files._secure_explicit_sqlite_bundle

    def tracked_prepare(*args, **kwargs):
        raw_calls.append("prepare")
        return real_prepare(*args, **kwargs)

    def tracked_secure(*args, **kwargs):
        raw_calls.append("secure")
        return real_secure(*args, **kwargs)

    monkeypatch.setattr(
        state_files,
        "_prepare_explicit_sqlite_file",
        tracked_prepare,
    )
    monkeypatch.setattr(
        state_files,
        "_secure_explicit_sqlite_bundle",
        tracked_secure,
    )

    db_path = tmp_path / "connection-locks.db"
    first = EventStore(str(db_path))
    second = None
    try:
        assert raw_calls == ["prepare", "secure"]
        second = EventStore(str(db_path))
        assert raw_calls == ["prepare", "secure"]
        assert first.append("run-1", "TEST_EVENT", {"value": 1}) == 1
        second.close()
        assert raw_calls == ["prepare", "secure"]
    finally:
        if second is not None:
            second.close()
        first.close()

    assert raw_calls == ["prepare", "secure", "secure"]


def test_managed_bundle_validation_preserves_existing_sqlite_write_lock(tmp_path):
    db_path = tmp_path / "existing-live-connection.db"
    store = EventStore(str(db_path))
    try:
        store._conn.execute("BEGIN IMMEDIATE")
        assert _external_write_lock_result(db_path) == "locked"

        state_files.prepare_sqlite_file(db_path)
        state_files.secure_sqlite_sidecars(db_path)

        assert _external_write_lock_result(db_path) == "locked"
    finally:
        store._conn.rollback()
        store.close()


def test_default_state_path_uses_stat_only_fast_path_for_active_bundle(
    tmp_path,
    monkeypatch,
):
    db_path = state_files.default_state_database_path(tmp_path, "active.db")
    store = EventStore(str(db_path))
    try:
        def reject_raw_path_resolution(*_args, **_kwargs):
            raise AssertionError("active bundle must not be raw-opened again")

        monkeypatch.setattr(
            state_files,
            "_default_state_database_path_unlocked",
            reject_raw_path_resolution,
        )

        assert (
            state_files.default_state_database_path(tmp_path, "active.db")
            == db_path
        )
    finally:
        store.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_sqlite_bundle_registry_reinitializes_locked_rlock_after_fork():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

from workflow import state_files

ready = threading.Event()
release = threading.Event()

def hold_registry_lock():
    with state_files._SQLITE_BUNDLE_REGISTRY_LOCK:
        ready.set()
        release.wait(2)

holder = threading.Thread(target=hold_registry_lock)
holder.start()
if not ready.wait(1):
    raise SystemExit("holder did not acquire registry lock")

child = os.fork()
if child == 0:
    try:
        root = Path(tempfile.mkdtemp(prefix="sqlite-bundle-child-"))
        state_files.default_state_database_path(root, "child.db")
    except BaseException:
        os._exit(2)
    os._exit(0)

release.set()
holder.join(2)
deadline = time.monotonic() + 1
while time.monotonic() < deadline:
    waited, status = os.waitpid(child, os.WNOHANG)
    if waited:
        if os.waitstatus_to_exitcode(status) != 0:
            raise SystemExit("child failed")
        print("ok")
        raise SystemExit(0)
    time.sleep(0.01)
os.kill(child, 9)
os.waitpid(child, 0)
raise SystemExit("child-deadlocked")
""",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.strip().endswith("ok")


def test_sqlite_bundle_lease_cannot_be_released_twice(tmp_path):
    db_path = tmp_path / "double-release.db"
    first = state_files.acquire_sqlite_bundle(db_path)
    peer = state_files.acquire_sqlite_bundle(db_path)
    state_files.release_sqlite_bundle(first)
    try:
        with pytest.raises(RuntimeError, match="lease"):
            state_files.release_sqlite_bundle(first)
    finally:
        try:
            state_files.release_sqlite_bundle(peer)
        except RuntimeError:
            pass


def test_stale_sqlite_bundle_lease_cannot_release_new_generation(tmp_path):
    db_path = tmp_path / "stale-release.db"
    stale = state_files.acquire_sqlite_bundle(db_path)
    state_files.release_sqlite_bundle(stale)
    current = state_files.acquire_sqlite_bundle(db_path)
    try:
        with pytest.raises(RuntimeError, match="lease"):
            state_files.release_sqlite_bundle(stale)
    finally:
        try:
            state_files.release_sqlite_bundle(current)
        except RuntimeError:
            pass


def test_sqlite_bundle_final_release_can_retry_sidecar_failure(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "retry-final-release.db"
    lease = state_files.acquire_sqlite_bundle(db_path)
    real_secure = state_files._secure_explicit_sqlite_bundle
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected final secure failure")
        return real_secure(*args, **kwargs)

    monkeypatch.setattr(
        state_files,
        "_secure_explicit_sqlite_bundle",
        fail_once,
    )

    with pytest.raises(OSError, match="final secure failure"):
        state_files.release_sqlite_bundle(lease)
    state_files.release_sqlite_bundle(lease)
    with pytest.raises(RuntimeError, match="lease"):
        state_files.release_sqlite_bundle(lease)
    assert attempts == 2


@pytest.mark.parametrize("owner", ["event-store", "outbox"])
def test_store_close_retries_lease_finalization_after_sidecar_failure(
    tmp_path,
    monkeypatch,
    owner,
):
    db_path = tmp_path / f"{owner}.db"
    managed = (
        EventStore(str(db_path))
        if owner == "event-store"
        else WorkflowResultOutbox(str(db_path))
    )
    real_secure = state_files._secure_explicit_sqlite_bundle
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected final secure failure")
        return real_secure(*args, **kwargs)

    monkeypatch.setattr(
        state_files,
        "_secure_explicit_sqlite_bundle",
        fail_once,
    )

    with pytest.raises(OSError, match="final secure failure"):
        managed.close()
    managed.close()
    assert attempts == 2


@pytest.mark.parametrize("owner", ["event-store", "outbox"])
def test_store_close_accepts_finalization_completed_by_next_acquire(
    tmp_path,
    monkeypatch,
    owner,
):
    db_path = tmp_path / f"{owner}-assisted-finalization.db"
    constructor = EventStore if owner == "event-store" else WorkflowResultOutbox
    first = constructor(str(db_path))
    real_secure = state_files._secure_explicit_sqlite_bundle
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected final secure failure")
        return real_secure(*args, **kwargs)

    monkeypatch.setattr(
        state_files,
        "_secure_explicit_sqlite_bundle",
        fail_once,
    )

    with pytest.raises(OSError, match="final secure failure"):
        first.close()
    second = constructor(str(db_path))
    try:
        attempts_after_acquire = attempts
        first.close()
        assert first._closed is True
        assert attempts == attempts_after_acquire
    finally:
        second.close()
    assert attempts == attempts_after_acquire + 1


@pytest.mark.parametrize("owner", ["event-store", "outbox"])
def test_store_constructor_preserves_primary_error_during_close_retry(
    tmp_path,
    monkeypatch,
    owner,
):
    class FailingConnection:
        def __init__(self):
            self.close_attempts = 0

        def execute(self, *_args, **_kwargs):
            raise ValueError("primary initialization failure")

        def close(self):
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise OSError("transient close failure")

    connection = FailingConnection()
    module = store_module if owner == "event-store" else outbox_module
    constructor = EventStore if owner == "event-store" else WorkflowResultOutbox
    monkeypatch.setattr(module.sqlite3, "connect", lambda *_a, **_k: connection)

    with pytest.raises(ValueError, match="primary initialization failure"):
        constructor(str(tmp_path / f"{owner}-constructor-cleanup.db"))

    assert connection.close_attempts == 2
    key = state_files._sqlite_bundle_key(
        tmp_path / f"{owner}-constructor-cleanup.db"
    )
    assert key not in state_files._ACTIVE_SQLITE_BUNDLES


@pytest.mark.parametrize(
    "malformation_sql",
    [
        "DROP TABLE agui_runs",
        """
        DROP TABLE agui_runs;
        CREATE TABLE agui_runs (
            run_id INTEGER PRIMARY KEY,
            thread_id TEXT NOT NULL,
            owner_subject TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            roles TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        )
        """,
        """
        DROP TABLE agui_runs;
        CREATE TABLE agui_runs (
            run_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            owner_subject TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            roles TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        )
        """,
        """
        DROP TABLE workflow_run_invocations;
        CREATE TABLE workflow_run_invocations (
            run_id TEXT PRIMARY KEY,
            run_incarnation TEXT NOT NULL,
            session_id TEXT NOT NULL,
            workflow_name TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        )
        """,
    ],
    ids=[
        "missing-table",
        "wrong-column-type",
        "missing-primary-key",
        "missing-unique-constraint",
    ],
)
def test_claimed_current_event_store_rejects_malformed_table_schema(
    tmp_path,
    malformation_sql,
):
    db_path = tmp_path / "malformed-table.db"
    _create_current_store(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(malformation_sql)
        connection.commit()

    with pytest.raises(RuntimeError, match="schema|table|index"):
        EventStore(str(db_path))


@pytest.mark.parametrize(
    "replacement_sql",
    [
        """
        CREATE INDEX idx_agui_events_event_key
        ON agui_events(created_at_ms)
        """,
        """
        CREATE INDEX idx_agui_events_event_key
        ON agui_events(event_key) WHERE event_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_agui_events_event_key
        ON agui_events(event_key)
        """,
        """
        CREATE UNIQUE INDEX idx_agui_events_event_key
        ON agui_events(event_key COLLATE NOCASE)
        WHERE event_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_agui_events_event_key
        ON agui_events(event_key DESC)
        WHERE event_key IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_agui_events_event_key
        ON agui_events(event_key) WHERE event_key <> ''
        """,
    ],
    ids=[
        "same-name-wrong-column-nonunique",
        "nonunique",
        "nonpartial",
        "wrong-collation",
        "descending-key",
        "wrong-partial-predicate",
    ],
)
def test_claimed_current_event_store_rejects_malformed_event_key_index(
    tmp_path,
    replacement_sql,
):
    db_path = tmp_path / "malformed-event-key-index.db"
    _create_current_store(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX idx_agui_events_event_key")
        connection.execute(replacement_sql)
        connection.commit()

    with pytest.raises(RuntimeError, match="schema|index"):
        EventStore(str(db_path))


@pytest.mark.parametrize(
    "replacement_sql",
    [
        """
        CREATE INDEX idx_agui_events_run_seq
        ON agui_events(run_id, seq)
        """,
        """
        CREATE UNIQUE INDEX idx_agui_events_run_seq
        ON agui_events(run_id COLLATE NOCASE, seq)
        """,
        """
        CREATE UNIQUE INDEX idx_agui_events_run_seq
        ON agui_events(seq, run_id)
        """,
    ],
    ids=["nonunique", "wrong-collation", "wrong-key-order"],
)
def test_claimed_current_event_store_rejects_malformed_run_sequence_index(
    tmp_path,
    replacement_sql,
):
    db_path = tmp_path / "malformed-run-sequence-index.db"
    _create_current_store(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX idx_agui_events_run_seq")
        connection.execute(replacement_sql)
        connection.commit()

    with pytest.raises(RuntimeError, match="schema|index"):
        EventStore(str(db_path))
