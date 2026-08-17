from __future__ import annotations

import errno
import hashlib
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import stat

import pytest


def _create_marker_database(
    path: Path,
    value: str,
    *,
    connect=sqlite3.connect,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (value,))
        connection.commit()
    os.chmod(path, 0o600)


def _open_wal_marker_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker VALUES ('trusted')")
    connection.commit()
    os.chmod(path, 0o600)
    assert Path(f"{path}-wal").exists()
    assert Path(f"{path}-shm").exists()
    return connection


def _marker_value(path: Path, *, connect=sqlite3.connect) -> str:
    with connect(path) as connection:
        return str(connection.execute("SELECT value FROM marker").fetchone()[0])


def _migration_artifacts(target: Path) -> list[Path]:
    return list(target.parent.glob(f".{target.name}.*"))


def _stable_file_state(path: Path) -> tuple[bytes, int, int, int]:
    path_stat = os.stat(path)
    return (
        hashlib.sha256(path.read_bytes()).digest(),
        path_stat.st_size,
        path_stat.st_mtime_ns,
        stat.S_IMODE(path_stat.st_mode),
    )


def _create_crash_style_wal_bundle(path: Path) -> None:
    live_path = path.parent / "live-source.db"
    live_connection = sqlite3.connect(live_path)
    try:
        live_connection.execute("PRAGMA journal_mode=WAL")
        live_connection.execute("PRAGMA wal_autocheckpoint=0")
        live_connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        live_connection.execute("INSERT INTO marker VALUES ('crash-value')")
        live_connection.commit()
        live_wal = Path(f"{live_path}-wal")
        assert live_wal.exists()
        path.write_bytes(live_path.read_bytes())
        Path(f"{path}-wal").write_bytes(live_wal.read_bytes())
    finally:
        live_connection.close()
    os.chmod(path, 0o600)
    os.chmod(Path(f"{path}-wal"), 0o600)
    assert not Path(f"{path}-shm").exists()


class _BeforeBackupConnection:
    def __init__(self, connection: sqlite3.Connection, before_backup) -> None:
        self._connection = connection
        self._before_backup = before_backup

    def backup(self, destination: sqlite3.Connection) -> None:
        self._before_backup()
        self._connection.backup(destination)

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def _legacy_migration_worker(
    db_path: str,
    worker_id: int,
    start_event,
    result_queue,
) -> None:
    try:
        if not start_event.wait(10):
            raise RuntimeError("migration start gate timed out")
        store_module = importlib.import_module("backend.fastapi_app.agui.store")
        store = store_module.EventStore(db_path)
        try:
            store.append(
                f"worker-{worker_id}",
                "WORKER_EVENT",
                {"worker_id": worker_id},
            )
        finally:
            store.close()
        with sqlite3.connect(db_path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
        result_queue.put(("ok", worker_id, version, journal_mode))
    except Exception as exc:
        result_queue.put(("error", worker_id, type(exc).__name__, str(exc)))


def _create_legacy_event_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE agui_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE agui_runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                owner_subject TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                roles TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agui_events
                (run_id, seq, event_type, payload, created_at_ms)
            VALUES ('legacy-run', 1, 'LEGACY_EVENT', '{"legacy": true}', 1)
            """
        )
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    finally:
        connection.close()


@pytest.mark.filterwarnings("ignore")
def test_event_store_real_multiprocess_legacy_migration_is_versioned_and_wal_safe(
    tmp_path,
):
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    expected_version = getattr(store_module, "AGUI_EVENT_STORE_SCHEMA_VERSION", None)
    db_path = tmp_path / "legacy-events.db"
    _create_legacy_event_db(db_path)
    os.chmod(db_path, 0o600)
    assert not any(
        Path(f"{db_path}{suffix}").exists()
        for suffix in ("-wal", "-shm", "-journal")
    )

    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    process_count = 16
    processes = [
        context.Process(
            target=_legacy_migration_worker,
            args=(str(db_path), worker_id, start_event, result_queue),
        )
        for worker_id in range(process_count)
    ]
    for process in processes:
        process.start()
    start_event.set()
    results = [result_queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert expected_version is not None
    assert all(process.exitcode == 0 for process in processes)
    assert all(result[0] == "ok" for result in results), results
    assert {result[2] for result in results} == {expected_version}
    assert {result[3] for result in results} == {"wal"}
    with sqlite3.connect(db_path) as connection:
        columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(agui_events)")
        ]
        assert columns.count("run_incarnation") == 1
        assert columns.count("event_key") == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM agui_events"
        ).fetchone()[0] == process_count + 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _workflow_result_payload(store_module, *, run_id="run-1", incarnation="inc-1"):
    key_builder = store_module.workflow_result_event_key
    return {
        "run_id": run_id,
        "run_incarnation": incarnation,
        "event_key": key_builder(run_id, incarnation),
        "status": "failed",
        "terminal_outcome": {"reason_code": "RESULT_RECONCILIATION_FAILED"},
    }


def test_event_key_exact_retry_is_idempotent_and_payload_is_canonical(tmp_path):
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    store = store_module.EventStore(str(tmp_path / "events.db"))
    payload = _workflow_result_payload(store_module)
    reordered = {key: payload[key] for key in reversed(payload)}
    try:
        assert store.reserve_workflow_run(
            "run-1",
            "inc-1",
            "session-1",
            "text_to_sql_pipeline",
        ) is True
        first_seq = store.append("run-1", "WORKFLOW_RESULT", payload)
        second_seq = store.append("run-1", "WORKFLOW_RESULT", reordered)
        assert first_seq == second_seq
        events = list(store.list_after("run-1", 0))
        assert len(events) == 1
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with sqlite3.connect(tmp_path / "events.db") as connection:
            stored_json = connection.execute(
                "SELECT payload FROM agui_events WHERE event_key = ?",
                (payload["event_key"],),
            ).fetchone()[0]
        assert stored_json == canonical
    finally:
        store.close()


@pytest.mark.parametrize(
    "mismatch",
    ["run_id", "run_incarnation", "event_type", "canonical_payload"],
)
def test_event_key_mismatch_is_rejected_atomically(tmp_path, mismatch):
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    store = store_module.EventStore(str(tmp_path / f"events-{mismatch}.db"))
    payload = _workflow_result_payload(store_module)
    assert store.reserve_workflow_run(
        "run-1",
        "inc-1",
        "session-1",
        "text_to_sql_pipeline",
    ) is True
    store.append("run-1", "WORKFLOW_RESULT", payload)
    retry_run_id = "run-1"
    retry_event_type = "WORKFLOW_RESULT"
    retry_payload = dict(payload)
    if mismatch == "run_id":
        retry_run_id = "run-2"
    elif mismatch == "run_incarnation":
        retry_payload["run_incarnation"] = "inc-2"
    elif mismatch == "event_type":
        retry_event_type = "OTHER_EVENT"
    else:
        retry_payload["status"] = "completed"

    try:
        with pytest.raises(ValueError, match="event_key"):
            store.append(retry_run_id, retry_event_type, retry_payload)
        original = list(store.list_after("run-1", 0))
        assert len(original) == 1
        assert original[0].payload == payload
        if retry_run_id != "run-1":
            assert list(store.list_after(retry_run_id, 0)) == []
    finally:
        store.close()


@pytest.mark.parametrize(("run_id", "incarnation"), [("", "inc"), ("run", "")])
def test_workflow_result_event_key_rejects_empty_identity(run_id, incarnation):
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    key_builder = store_module.workflow_result_event_key
    with pytest.raises(ValueError):
        key_builder(run_id, incarnation)


def test_event_store_registration_read_apis_are_exact_and_immutable(tmp_path):
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    auth_module = importlib.import_module("backend.fastapi_app.agui.auth")
    store = store_module.EventStore(str(tmp_path / "events.db"))
    principal = auth_module.Principal(
        subject="owner",
        tenant_id="tenant",
        roles=frozenset({"user"}),
    )
    try:
        assert store.reserve_workflow_run(
            "run-1",
            "inc-1",
            "session-1",
            "text_to_sql_pipeline",
        ) is True
        store.record_run("run-1", "thread-1", principal)
        invocation = store.get_workflow_run_invocation("run-1")
        assert invocation == store_module.WorkflowRunInvocation(
            run_id="run-1",
            run_incarnation="inc-1",
            session_id="session-1",
            workflow_name="text_to_sql_pipeline",
        )
        with pytest.raises((AttributeError, TypeError)):
            invocation.session_id = "changed"
        assert store.get_run_thread_id("run-1") == "thread-1"
        assert store.get_workflow_run_invocation("missing") is None
        assert store.get_run_thread_id("missing") is None
    finally:
        store.close()


def test_default_state_database_migrates_known_legacy_without_touching_parent(
    tmp_path,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o770)
    legacy_path = data_dir / "agui_events.db"
    with sqlite3.connect(legacy_path) as connection:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('legacy-value')")
        connection.commit()
    os.chmod(legacy_path, 0o660)
    unrelated = data_dir / "unrelated.txt"
    unrelated.write_text("untouched", encoding="utf-8")
    os.chmod(unrelated, 0o664)
    parent_mode = stat.S_IMODE(os.lstat(data_dir).st_mode)
    unrelated_mode = stat.S_IMODE(os.lstat(unrelated).st_mode)

    target = state_files.default_state_database_path(
        tmp_path,
        "agui_events.db",
        legacy_path=legacy_path,
    )

    assert target == data_dir / "multiagent_state" / "agui_events.db"
    assert stat.S_IMODE(os.lstat(target.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(data_dir).st_mode) == parent_mode
    assert unrelated.read_text(encoding="utf-8") == "untouched"
    assert stat.S_IMODE(os.lstat(unrelated).st_mode) == unrelated_mode
    assert legacy_path.exists()
    assert stat.S_IMODE(os.lstat(legacy_path).st_mode) == 0o600
    assert _marker_value(legacy_path) == "legacy-value"
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == (
            "legacy-value"
        )


def test_default_state_database_skips_untrusted_legacy_symlink(tmp_path):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o770)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"not workflow state")
    os.chmod(outside, 0o600)
    legacy_path = data_dir / "agui_events.db"
    legacy_path.symlink_to(outside)

    target = state_files.default_state_database_path(
        tmp_path,
        "agui_events.db",
        legacy_path=legacy_path,
    )

    assert target.exists()
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o600
    assert legacy_path.is_symlink()
    assert legacy_path.resolve() == outside.resolve()
    assert outside.read_bytes() == b"not workflow state"


def test_default_state_database_rejects_unsafe_existing_state_directory(tmp_path):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o770)
    state_dir = data_dir / "multiagent_state"
    state_dir.mkdir(mode=0o770)
    state_dir.chmod(0o770)
    original_mode = stat.S_IMODE(os.lstat(state_dir).st_mode)

    with pytest.raises(PermissionError, match="directory mode"):
        state_files.default_state_database_path(tmp_path, "agui_events.db")

    assert stat.S_IMODE(os.lstat(state_dir).st_mode) == original_mode
    assert not (state_dir / "agui_events.db").exists()


def test_explicit_sqlite_path_rejects_group_writable_parent_without_chmod(tmp_path):
    state_files = importlib.import_module("workflow.state_files")
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o770)
    unsafe_parent.chmod(0o770)
    original_mode = stat.S_IMODE(os.lstat(unsafe_parent).st_mode)

    with pytest.raises(PermissionError, match="directory mode"):
        state_files.prepare_sqlite_file(unsafe_parent / "events.db")

    assert stat.S_IMODE(os.lstat(unsafe_parent).st_mode) == original_mode
    assert not (unsafe_parent / "events.db").exists()


def test_sqlite_file_rejects_symlink_and_wrong_owner(tmp_path):
    state_files = importlib.import_module("workflow.state_files")
    real_file = tmp_path / "real.db"
    real_file.write_bytes(b"")
    os.chmod(real_file, 0o600)
    symlink = tmp_path / "linked.db"
    symlink.symlink_to(real_file)

    with pytest.raises((OSError, ValueError), match="symlink"):
        state_files.prepare_sqlite_file(symlink)
    with pytest.raises(PermissionError, match="owner"):
        state_files.prepare_sqlite_file(
            real_file,
            owner_uid=os.geteuid() + 1,
        )


def test_sqlite_file_safely_tightens_owned_legacy_file_only(tmp_path):
    state_files = importlib.import_module("workflow.state_files")
    db_path = tmp_path / "legacy.db"
    db_path.write_bytes(b"")
    os.chmod(db_path, 0o660)
    parent_mode = stat.S_IMODE(os.lstat(tmp_path).st_mode)

    assert state_files.prepare_sqlite_file(db_path) == db_path

    assert stat.S_IMODE(os.lstat(db_path).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(tmp_path).st_mode) == parent_mode


def test_event_store_database_and_wal_sidecars_are_private(tmp_path):
    store_module = importlib.import_module("backend.fastapi_app.agui.store")
    db_path = tmp_path / "events.db"
    store = store_module.EventStore(str(db_path))
    try:
        store.append("run-1", "EVENT", {"value": 1})
        paths = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
        assert db_path.exists()
        for path in paths:
            if path.exists():
                path_stat = os.lstat(path)
                assert stat.S_ISREG(path_stat.st_mode)
                assert path_stat.st_uid == os.geteuid()
                assert stat.S_IMODE(path_stat.st_mode) == 0o600
    finally:
        store.close()


def test_fastapi_main_uses_dedicated_state_child_without_chmodding_data_parent():
    source = Path("backend/fastapi_app/main.py").read_text(encoding="utf-8")
    assert "default_state_database_path" in source
    assert 'DATA_DIR / "multiagent_state"' not in source
    assert "DATA_DIR.mkdir" not in source


def test_legacy_migration_rejects_main_path_swap_at_source_connect(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    preserved_trusted = data_dir / "trusted-original.db"
    hostile = tmp_path / "hostile.db"
    _create_marker_database(hostile, "hostile")
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_connect = sqlite3.connect
    swapped = False
    hostile_backup_attempted = False

    def connect_after_main_swap(database, *args, **kwargs):
        nonlocal swapped, hostile_backup_attempted
        if not swapped:
            swapped = True
            os.replace(legacy_path, preserved_trusted)
            os.replace(hostile, legacy_path)
        connection = real_connect(database, *args, **kwargs)
        if Path(str(database)) == legacy_path:
            return _BeforeBackupConnection(
                connection,
                lambda: _record_hostile_backup_attempt(),
            )
        return connection

    def _record_hostile_backup_attempt() -> None:
        nonlocal hostile_backup_attempted
        hostile_backup_attempted = True

    monkeypatch.setattr(state_files.sqlite3, "connect", connect_after_main_swap)

    with pytest.raises(RuntimeError, match="changed during migration"):
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert swapped is True
    assert hostile_backup_attempted is False
    assert _marker_value(preserved_trusted, connect=real_connect) == "trusted"
    assert _marker_value(legacy_path, connect=real_connect) == "hostile"
    assert not target.exists()
    assert _migration_artifacts(target) == []


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_legacy_migration_rejects_sidecar_inode_swap_during_source_connect(
    tmp_path,
    monkeypatch,
    suffix,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    legacy_path = data_dir / "agui_events.db"
    keeper = _open_wal_marker_database(legacy_path)
    sidecar = Path(f"{legacy_path}{suffix}")
    held_sidecar = data_dir / f"held{suffix}"
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_connect = sqlite3.connect
    swapped = False

    def connect_then_swap_sidecar(database, *args, **kwargs):
        nonlocal swapped
        connection = real_connect(database, *args, **kwargs)
        if not swapped:
            swapped = True
            os.replace(sidecar, held_sidecar)
            sidecar.write_bytes(held_sidecar.read_bytes())
            os.chmod(sidecar, 0o600)
        return connection

    monkeypatch.setattr(
        state_files.sqlite3,
        "connect",
        connect_then_swap_sidecar,
    )
    try:
        with pytest.raises(RuntimeError, match="changed during migration"):
            state_files.default_state_database_path(
                tmp_path,
                "agui_events.db",
                legacy_path=legacy_path,
            )

        assert swapped is True
        assert legacy_path.exists()
        assert sidecar.exists()
        assert held_sidecar.exists()
        assert not target.exists()
        assert _migration_artifacts(target) == []
    finally:
        keeper.close()


@pytest.mark.parametrize("swapped_ancestor", ["data", "state"])
def test_legacy_migration_rejects_ancestor_path_swap_during_source_connect(
    tmp_path,
    monkeypatch,
    swapped_ancestor,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    state_dir = data_dir / "multiagent_state"
    target = state_dir / "agui_events.db"
    real_connect = sqlite3.connect
    swapped = False
    if swapped_ancestor == "data":
        pinned_data = tmp_path / "pinned-data"
        pinned_state = pinned_data / "multiagent_state"
    else:
        pinned_data = data_dir
        pinned_state = data_dir / "pinned-state"

    def connect_after_ancestor_swap(database, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            if swapped_ancestor == "data":
                os.replace(data_dir, pinned_data)
                data_dir.mkdir(mode=0o755)
                state_dir.mkdir(mode=0o700)
                _create_marker_database(
                    legacy_path,
                    "hostile",
                    connect=real_connect,
                )
            else:
                os.replace(state_dir, pinned_state)
                state_dir.mkdir(mode=0o700)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        state_files.sqlite3,
        "connect",
        connect_after_ancestor_swap,
    )

    with pytest.raises(RuntimeError):
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert swapped is True
    assert not target.exists()
    assert not (pinned_state / target.name).exists()
    assert _migration_artifacts(target) == []
    assert _migration_artifacts(pinned_state / target.name) == []


@pytest.mark.parametrize("changed_member", ["main", "wal"])
def test_legacy_migration_rejects_source_stat_change_during_backup(
    tmp_path,
    monkeypatch,
    changed_member,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    legacy_path = data_dir / "agui_events.db"
    keeper = _open_wal_marker_database(legacy_path)
    changed_path = (
        legacy_path
        if changed_member == "main"
        else Path(f"{legacy_path}-wal")
    )
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_connect = sqlite3.connect
    wrapped_source = False
    connect_count = 0

    def change_stat_before_backup() -> None:
        before = os.stat(changed_path)
        os.utime(
            changed_path,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )

    def connect_with_backup_hook(database, *args, **kwargs):
        nonlocal wrapped_source, connect_count
        connect_count += 1
        connection = real_connect(database, *args, **kwargs)
        # Connect ordinals: 1=WAL-recovery connection, 2=source (the
        # connection .backup() is actually invoked on), 3=destination.
        if connect_count == 2 and not wrapped_source:
            wrapped_source = True
            return _BeforeBackupConnection(connection, change_stat_before_backup)
        return connection

    monkeypatch.setattr(
        state_files.sqlite3,
        "connect",
        connect_with_backup_hook,
    )
    try:
        with pytest.raises(RuntimeError, match="changed during migration"):
            state_files.default_state_database_path(
                tmp_path,
                "agui_events.db",
                legacy_path=legacy_path,
            )

        assert wrapped_source is True
        assert legacy_path.exists()
        assert changed_path.exists()
        assert not target.exists()
        assert _migration_artifacts(target) == []
    finally:
        keeper.close()


def test_legacy_migration_exdev_publish_link_fails_closed(tmp_path, monkeypatch):
    # Snapshot content is now transferred by pread/write copy, not by
    # hardlinking across the legacy-parent/state-dir boundary. The only
    # os.link() calls left in the migration are the same-directory
    # create-then-publish rename performed for every private file (each
    # snapshot copy, the backup temp file, and the final target). This test
    # now covers that publish-stage link failing closed with no artifacts
    # left behind, rather than a genuine cross-filesystem content link.
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    target = data_dir / "multiagent_state" / "agui_events.db"
    link_calls = 0

    def reject_cross_filesystem_link(*args, **kwargs):
        nonlocal link_calls
        link_calls += 1
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(state_files.os, "link", reject_cross_filesystem_link)

    with pytest.raises(OSError) as caught:
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert caught.value.errno == errno.EXDEV
    assert link_calls >= 1
    assert _marker_value(legacy_path) == "trusted"
    assert not target.exists()
    assert _migration_artifacts(target) == []


def test_legacy_migration_succeeds_across_filesystems_without_cross_directory_link(
    tmp_path,
    monkeypatch,
):
    # The redesigned migration never hardlinks between the legacy parent
    # directory and the state directory (content is copied via pread/write
    # instead), so a genuinely cross-filesystem legacy DB -- where such a
    # link would raise EXDEV -- now migrates successfully. Simulate "cross
    # filesystem" by rejecting only links whose source and destination
    # directory fds differ; every os.link() call the new code makes is
    # same-directory (internal create-then-publish renames), so none of
    # them should ever trip this guard.
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_link = os.link

    def reject_cross_directory_link(
        source,
        destination,
        *,
        src_dir_fd=None,
        dst_dir_fd=None,
        **kwargs,
    ):
        if (
            src_dir_fd is not None
            and dst_dir_fd is not None
            and src_dir_fd != dst_dir_fd
        ):
            raise OSError(errno.EXDEV, "simulated cross-filesystem link")
        return real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            **kwargs,
        )

    monkeypatch.setattr(state_files.os, "link", reject_cross_directory_link)

    target_result = state_files.default_state_database_path(
        tmp_path,
        "agui_events.db",
        legacy_path=legacy_path,
    )

    assert target_result == target
    assert _marker_value(target) == "trusted"
    assert _marker_value(legacy_path) == "trusted"
    assert _migration_artifacts(target) == []


def test_legacy_migration_preserves_target_created_concurrently_at_connect(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    legacy_path = data_dir / "agui_events.db"
    keeper = _open_wal_marker_database(legacy_path)
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_connect = sqlite3.connect
    target_injected = False

    def connect_then_create_competing_target(database, *args, **kwargs):
        nonlocal target_injected
        connection = real_connect(database, *args, **kwargs)
        if not target_injected:
            target_injected = True
            _create_marker_database(
                target,
                "pre-existing-target",
                connect=real_connect,
            )
        return connection

    monkeypatch.setattr(
        state_files.sqlite3,
        "connect",
        connect_then_create_competing_target,
    )
    try:
        with pytest.raises((FileExistsError, RuntimeError)):
            state_files.default_state_database_path(
                tmp_path,
                "agui_events.db",
                legacy_path=legacy_path,
            )

        assert target_injected is True
        assert _marker_value(target, connect=real_connect) == "pre-existing-target"
        assert legacy_path.exists()
        assert Path(f"{legacy_path}-wal").exists()
        assert Path(f"{legacy_path}-shm").exists()
        assert _migration_artifacts(target) == []
    finally:
        keeper.close()


def test_legacy_migration_rejects_sidecar_appearing_after_bundle_pin(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    late_wal = Path(f"{legacy_path}-wal")
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_connect = sqlite3.connect
    sidecar_created = False

    def connect_then_create_sidecar(database, *args, **kwargs):
        nonlocal sidecar_created
        connection = real_connect(database, *args, **kwargs)
        if not sidecar_created:
            sidecar_created = True
            late_wal.write_bytes(b"late hostile sidecar")
            os.chmod(late_wal, 0o600)
        return connection

    monkeypatch.setattr(
        state_files.sqlite3,
        "connect",
        connect_then_create_sidecar,
    )

    with pytest.raises(RuntimeError, match="changed during migration"):
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert sidecar_created is True
    assert late_wal.read_bytes() == b"late hostile sidecar"
    assert not target.exists()
    assert _migration_artifacts(target) == []


def test_legacy_migration_cleans_actual_snapshot_inode_after_copy_time_swap(
    tmp_path,
    monkeypatch,
):
    # Snapshot content is copied via pread/write rather than hardlinked, so
    # there is no longer an os.link() call whose source is the legacy file's
    # own name. The read is immune to a same-name swap (an already-open fd
    # keeps referring to the original inode), but the swap must still be
    # caught by the post-copy bundle.revalidate() name-vs-fd identity check.
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    preserved_trusted = data_dir / "trusted-original.db"
    hostile = tmp_path / "hostile.db"
    _create_marker_database(hostile, "hostile")
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_pread = os.pread
    swapped = False

    def pread_then_swap_legacy_name(file_descriptor, *args, **kwargs):
        nonlocal swapped
        result = real_pread(file_descriptor, *args, **kwargs)
        if not swapped:
            swapped = True
            os.replace(legacy_path, preserved_trusted)
            os.replace(hostile, legacy_path)
        return result

    monkeypatch.setattr(state_files.os, "pread", pread_then_swap_legacy_name)

    with pytest.raises(RuntimeError, match="changed during migration"):
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert swapped is True
    assert _marker_value(preserved_trusted) == "trusted"
    assert _marker_value(legacy_path) == "hostile"
    assert not target.exists()
    assert _migration_artifacts(target) == []


def test_legacy_migration_rejects_failed_destination_integrity_check(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_connect = sqlite3.connect
    connect_count = 0
    integrity_checked = False

    class CorruptIntegrityCursor:
        @staticmethod
        def fetchone():
            return ("database disk image is malformed",)

    class CorruptIntegrityConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            nonlocal integrity_checked
            if statement.strip().upper() == "PRAGMA INTEGRITY_CHECK":
                integrity_checked = True
                return CorruptIntegrityCursor()
            return super().execute(statement, *args, **kwargs)

    def connect_with_corrupt_destination(database, *args, **kwargs):
        nonlocal connect_count
        connect_count += 1
        # Connect ordinals: 1=WAL-recovery connection, 2=source, 3=destination.
        if connect_count == 3:
            kwargs["factory"] = CorruptIntegrityConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        state_files.sqlite3,
        "connect",
        connect_with_corrupt_destination,
    )

    with pytest.raises(sqlite3.DatabaseError, match="integrity check"):
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert integrity_checked is True
    assert legacy_path.exists()
    assert not target.exists()
    assert _migration_artifacts(target) == []


def _create_torn_wal_bundle(path: Path) -> None:
    """Build a crash-style bundle whose WAL ends mid-frame.

    The first transaction's WAL frame(s) are left fully intact and
    checksummed; the second transaction's frame is truncated partway
    through so SQLite's WAL recovery must stop replay at the last valid
    frame and silently drop the torn tail, exactly as it would after a
    real crash mid-write.
    """
    live_path = path.parent / "torn-live-source.db"
    live_connection = sqlite3.connect(live_path)
    try:
        live_connection.execute("PRAGMA journal_mode=WAL")
        live_connection.execute("PRAGMA wal_autocheckpoint=0")
        live_connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        live_connection.execute("INSERT INTO marker VALUES ('first-committed')")
        live_connection.commit()
        live_wal = Path(f"{live_path}-wal")
        committed_size = live_wal.stat().st_size
        assert committed_size > 0
        live_connection.execute("INSERT INTO marker VALUES ('torn-transaction')")
        live_connection.commit()
        full_wal_bytes = live_wal.read_bytes()
        assert len(full_wal_bytes) > committed_size
        torn_wal_bytes = full_wal_bytes[: committed_size + 10]
        path.write_bytes(live_path.read_bytes())
        Path(f"{path}-wal").write_bytes(torn_wal_bytes)
    finally:
        live_connection.close()
    os.chmod(path, 0o600)
    os.chmod(Path(f"{path}-wal"), 0o600)
    assert not Path(f"{path}-shm").exists()


def test_legacy_migration_recovers_torn_wal_tail_and_succeeds(tmp_path):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    legacy_wal = Path(f"{legacy_path}-wal")
    _create_torn_wal_bundle(legacy_path)
    main_before = _stable_file_state(legacy_path)
    wal_before = _stable_file_state(legacy_wal)

    target = state_files.default_state_database_path(
        tmp_path,
        "agui_events.db",
        legacy_path=legacy_path,
    )

    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        values = {
            row[0] for row in connection.execute("SELECT value FROM marker")
        }
    assert values == {"first-committed"}
    assert _stable_file_state(legacy_path) == main_before
    assert _stable_file_state(legacy_wal) == wal_before
    assert _migration_artifacts(target) == []


def _create_corrupted_main_db(path: Path) -> None:
    """Build a valid multi-page DB, then stamp a page header far from
    page 1 so PRAGMA integrity_check reports corruption without SQLite
    raising a hard DatabaseError while opening/checkpointing the copy."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        for index in range(500):
            connection.execute(
                "INSERT INTO marker VALUES (?)",
                (f"row-{index}-{'x' * 200}",),
            )
        connection.commit()
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
    os.chmod(path, 0o600)
    assert page_count > 4
    with open(path, "r+b") as handle:
        handle.seek(page_size * 3 + 5)
        handle.write(b"\xab\xab")


def test_legacy_migration_rejects_corrupted_main_page_with_reason_code(tmp_path):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    legacy_path = data_dir / "agui_events.db"
    _create_corrupted_main_db(legacy_path)
    target = data_dir / "multiagent_state" / "agui_events.db"
    legacy_before = _stable_file_state(legacy_path)

    with pytest.raises(state_files.LegacyDatabaseUnrecoverable) as caught:
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert caught.value.reason_code == "LEGACY_DB_INTEGRITY_CHECK_FAILED"
    assert _stable_file_state(legacy_path) == legacy_before
    assert not target.exists()
    assert _migration_artifacts(target) == []


def test_legacy_migration_detects_concurrent_write_after_baseline_and_aborts(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_capture_baseline = state_files._PinnedLegacyBundle.capture_baseline
    mutated = False

    def capture_baseline_then_mutate(self):
        nonlocal mutated
        real_capture_baseline(self)
        if not mutated:
            mutated = True
            # Simulate a write landing in the original legacy file from a
            # concurrent process while our copy loop is in flight.
            with sqlite3.connect(legacy_path) as concurrent_connection:
                concurrent_connection.execute(
                    "INSERT INTO marker VALUES ('concurrent-write')"
                )
                concurrent_connection.commit()

    monkeypatch.setattr(
        state_files._PinnedLegacyBundle,
        "capture_baseline",
        capture_baseline_then_mutate,
    )

    with pytest.raises(RuntimeError, match="changed during migration"):
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert mutated is True
    assert not target.exists()
    assert _migration_artifacts(target) == []
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute(
            "SELECT value FROM marker WHERE value = 'trusted'"
        ).fetchone() is not None


def test_validate_snapshot_links_rejects_copy_swapped_between_creation_and_use(
    tmp_path,
    monkeypatch,
):
    # Regression test for the reworked _validate_snapshot_links invariant:
    # since snapshots are private copies (not hardlinks into the legacy
    # file), the check can no longer compare inode identity against the
    # pinned legacy fd. It must instead detect that the private copy itself
    # was swapped out between creation and use -- otherwise the check would
    # be vacuous.
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    target = data_dir / "multiagent_state" / "agui_events.db"
    state_dir = data_dir / "multiagent_state"
    real_validate_snapshot_links = state_files._validate_snapshot_links
    swapped = False
    swapped_snapshot_path: Path | None = None

    def validate_then_swap_first_call(state_fd, snapshot_names, created_entries):
        nonlocal swapped, swapped_snapshot_path
        if not swapped:
            swapped = True
            main_snapshot_name = snapshot_names[""]
            swapped_snapshot_path = state_dir / main_snapshot_name
            hostile_source = state_dir / "hostile-swap-source.db"
            hostile_source.write_bytes(b"hostile snapshot content")
            os.chmod(hostile_source, 0o600)
            # os.replace guarantees a distinct inode from the original
            # snapshot copy (unlike unlink-then-recreate, which risks the
            # filesystem reusing the freed inode number).
            os.replace(hostile_source, swapped_snapshot_path)
        return real_validate_snapshot_links(state_fd, snapshot_names, created_entries)

    monkeypatch.setattr(
        state_files,
        "_validate_snapshot_links",
        validate_then_swap_first_call,
    )

    with pytest.raises(RuntimeError, match="changed during"):
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert swapped is True
    assert not target.exists()
    assert swapped_snapshot_path is not None
    # The swapped-in file is foreign (not a recognized owned inode), so
    # cleanup must never claim or delete it -- it is left exactly as the
    # attacker (or racing process) left it.
    assert swapped_snapshot_path.read_bytes() == b"hostile snapshot content"
    assert _marker_value(legacy_path) == "trusted"


def test_existing_target_name_is_revalidated_after_fd_mode_check(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    state_dir = tmp_path / "data" / "multiagent_state"
    state_dir.mkdir(mode=0o700, parents=True)
    target = state_dir / "agui_events.db"
    _create_marker_database(target, "trusted-target")
    preserved_trusted = state_dir / "trusted-target.db"
    hostile = tmp_path / "hostile-target.db"
    _create_marker_database(hostile, "hostile-target")
    real_open_private = state_files._open_or_create_private_at
    swapped = False

    def open_then_swap_target(directory_fd, name):
        nonlocal swapped
        result = real_open_private(directory_fd, name)
        if not swapped and name == target.name:
            swapped = True
            os.replace(target, preserved_trusted)
            os.replace(hostile, target)
        return result

    monkeypatch.setattr(
        state_files,
        "_open_or_create_private_at",
        open_then_swap_target,
    )

    with pytest.raises(RuntimeError, match="changed"):
        state_files.default_state_database_path(tmp_path, target.name)

    assert swapped is True
    assert _marker_value(preserved_trusted) == "trusted-target"
    assert _marker_value(target) == "hostile-target"


def test_readonly_snapshot_preserves_crash_style_main_and_wal_without_shm(
    tmp_path,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    legacy_wal = Path(f"{legacy_path}-wal")
    legacy_shm = Path(f"{legacy_path}-shm")
    _create_crash_style_wal_bundle(legacy_path)
    main_before = _stable_file_state(legacy_path)
    wal_before = _stable_file_state(legacy_wal)

    target = state_files.default_state_database_path(
        tmp_path,
        "agui_events.db",
        legacy_path=legacy_path,
    )

    assert _marker_value(target) == "crash-value"
    assert _stable_file_state(legacy_path) == main_before
    assert _stable_file_state(legacy_wal) == wal_before
    assert not legacy_shm.exists()
    assert _migration_artifacts(target) == []


def test_connection_close_failure_preserves_primary_error_and_cleans_artifacts(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_connect = sqlite3.connect
    connect_count = 0

    class CloseFailingConnection(sqlite3.Connection):
        def close(self):
            super().close()
            raise RuntimeError("destination close failed")

    def raise_primary_backup_error() -> None:
        raise ValueError("primary backup failure")

    def connect_with_cleanup_failures(database, *args, **kwargs):
        nonlocal connect_count
        connect_count += 1
        # Connect ordinals: 1=WAL-recovery connection, 2=source, 3=destination.
        if connect_count == 3:
            kwargs["factory"] = CloseFailingConnection
            return real_connect(database, *args, **kwargs)
        connection = real_connect(database, *args, **kwargs)
        if connect_count == 2:
            return _BeforeBackupConnection(
                connection,
                raise_primary_backup_error,
            )
        return connection

    monkeypatch.setattr(
        state_files.sqlite3,
        "connect",
        connect_with_cleanup_failures,
    )

    with pytest.raises(ValueError, match="primary backup failure"):
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert legacy_path.exists()
    assert not target.exists()
    assert _migration_artifacts(target) == []


@pytest.mark.parametrize("failed_open_index", [0, 1])
def test_pinned_chain_open_closes_untracked_fd_when_fstat_raises(
    tmp_path,
    monkeypatch,
    failed_open_index,
):
    state_files = importlib.import_module("workflow.state_files")
    real_open = os.open
    real_close = os.close
    real_fstat = os.fstat
    acquired = []

    def tracking_open(*args, **kwargs):
        file_descriptor = real_open(*args, **kwargs)
        acquired.append(file_descriptor)
        return file_descriptor

    def failing_fstat(file_descriptor):
        if (
            len(acquired) > failed_open_index
            and file_descriptor == acquired[failed_open_index]
        ):
            raise OSError(errno.EIO, "injected fstat failure")
        return real_fstat(file_descriptor)

    monkeypatch.setattr(state_files.os, "open", tracking_open)
    monkeypatch.setattr(state_files.os, "fstat", failing_fstat)
    try:
        with pytest.raises(OSError, match="injected fstat failure"):
            state_files._PinnedDirectoryChain.open(tmp_path)
        for file_descriptor in acquired:
            with pytest.raises(OSError) as caught:
                real_fstat(file_descriptor)
            assert caught.value.errno == errno.EBADF
    finally:
        for file_descriptor in acquired:
            try:
                real_close(file_descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


def test_pinned_chain_child_open_closes_fd_when_fstat_raises(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    chain = state_files._PinnedDirectoryChain.open(tmp_path)
    real_open = os.open
    real_close = os.close
    real_fstat = os.fstat
    acquired = None

    def tracking_open(path, *args, **kwargs):
        nonlocal acquired
        file_descriptor = real_open(path, *args, **kwargs)
        if path == "child":
            acquired = file_descriptor
        return file_descriptor

    def failing_fstat(file_descriptor):
        if file_descriptor == acquired:
            raise OSError(errno.EIO, "injected child fstat failure")
        return real_fstat(file_descriptor)

    monkeypatch.setattr(state_files.os, "open", tracking_open)
    monkeypatch.setattr(state_files.os, "fstat", failing_fstat)
    try:
        with pytest.raises(OSError, match="injected child fstat failure"):
            chain.create_and_open_child("child", 0o700)
        assert acquired is not None
        with pytest.raises(OSError) as caught:
            real_fstat(acquired)
        assert caught.value.errno == errno.EBADF
    finally:
        chain.close()
        if acquired is not None:
            try:
                real_close(acquired)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


def test_legacy_member_open_closes_fd_when_fstat_raises(tmp_path, monkeypatch):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    real_open = os.open
    real_close = os.close
    real_fstat = os.fstat
    acquired = None

    def tracking_open(path, *args, **kwargs):
        nonlocal acquired
        file_descriptor = real_open(path, *args, **kwargs)
        if os.fspath(path) == legacy_path.name:
            acquired = file_descriptor
        return file_descriptor

    def failing_fstat(file_descriptor):
        if file_descriptor == acquired:
            raise OSError(errno.EIO, "injected member fstat failure")
        return real_fstat(file_descriptor)

    monkeypatch.setattr(state_files.os, "open", tracking_open)
    monkeypatch.setattr(state_files.os, "fstat", failing_fstat)
    try:
        with pytest.raises(OSError, match="injected member fstat failure"):
            state_files._pin_optional_legacy_bundle(legacy_path)
        assert acquired is not None
        with pytest.raises(OSError) as caught:
            real_fstat(acquired)
        assert caught.value.errno == errno.EBADF
    finally:
        if acquired is not None:
            try:
                real_close(acquired)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


@pytest.mark.parametrize(
    "helper_name",
    ["_open_or_create_private_at", "_create_private_at"],
)
def test_private_file_acquisition_closes_fd_when_fstat_raises(
    tmp_path,
    monkeypatch,
    helper_name,
):
    state_files = importlib.import_module("workflow.state_files")
    real_open = os.open
    real_close = os.close
    real_fstat = os.fstat
    parent_fd = real_open(
        tmp_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    acquired = None

    def tracking_open(path, *args, **kwargs):
        nonlocal acquired
        file_descriptor = real_open(path, *args, **kwargs)
        if os.fspath(path).startswith(".state-create-"):
            acquired = file_descriptor
        return file_descriptor

    def failing_fstat(file_descriptor):
        if file_descriptor == acquired:
            raise OSError(errno.EIO, "injected private fstat failure")
        return real_fstat(file_descriptor)

    monkeypatch.setattr(state_files.os, "open", tracking_open)
    monkeypatch.setattr(state_files.os, "fstat", failing_fstat)
    try:
        helper = getattr(state_files, helper_name)
        with pytest.raises(OSError, match="injected private fstat failure"):
            helper(parent_fd, "candidate.db")
        assert acquired is not None
        with pytest.raises(OSError) as caught:
            real_fstat(acquired)
        assert caught.value.errno == errno.EBADF
    finally:
        if acquired is not None:
            try:
                real_close(acquired)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
        real_close(parent_fd)


def test_pinned_chain_close_attempts_every_fd_after_close_error(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    chain = state_files._PinnedDirectoryChain.open(tmp_path)
    descriptors = [entry[1] for entry in chain._entries]
    real_close = os.close
    real_fstat = os.fstat
    attempts = []
    failed_fd = descriptors[-1]

    def failing_close(file_descriptor):
        attempts.append(file_descriptor)
        real_close(file_descriptor)
        if file_descriptor == failed_fd:
            raise OSError(errno.EIO, "injected chain close failure")

    monkeypatch.setattr(state_files.os, "close", failing_close)
    try:
        with pytest.raises(OSError, match="injected chain close failure"):
            chain.close()
        assert set(attempts) == set(descriptors)
        for file_descriptor in descriptors:
            with pytest.raises(OSError) as caught:
                real_fstat(file_descriptor)
            assert caught.value.errno == errno.EBADF
    finally:
        for file_descriptor in descriptors:
            try:
                real_close(file_descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


def test_legacy_bundle_close_attempts_members_and_parent_after_close_error(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_crash_style_wal_bundle(legacy_path)
    bundle = state_files._pin_optional_legacy_bundle(legacy_path)
    assert bundle is not None
    descriptors = [member.file_descriptor for member in bundle.members]
    descriptors.extend(entry[1] for entry in bundle.parent._entries)
    failed_fd = bundle.members[-1].file_descriptor
    real_close = os.close
    real_fstat = os.fstat
    attempts = []

    def failing_close(file_descriptor):
        attempts.append(file_descriptor)
        real_close(file_descriptor)
        if file_descriptor == failed_fd:
            raise OSError(errno.EIO, "injected bundle close failure")

    monkeypatch.setattr(state_files.os, "close", failing_close)
    try:
        with pytest.raises(OSError, match="injected bundle close failure"):
            bundle.close()
        assert set(attempts) == set(descriptors)
        for file_descriptor in descriptors:
            with pytest.raises(OSError) as caught:
                real_fstat(file_descriptor)
            assert caught.value.errno == errno.EBADF
    finally:
        for file_descriptor in descriptors:
            try:
                real_close(file_descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_explicit_sqlite_bundle_rejects_name_swap_after_open(
    tmp_path,
    monkeypatch,
    suffix,
):
    state_files = importlib.import_module("workflow.state_files")
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    db_path = parent / "events.db"
    db_path.write_bytes(b"main")
    os.chmod(db_path, 0o600)
    target = Path(f"{db_path}{suffix}")
    if suffix:
        target.write_bytes(b"trusted-sidecar")
        os.chmod(target, 0o600)
    preserved = parent / f"preserved{suffix or '-main'}"
    hostile = tmp_path / f"hostile{suffix or '-main'}"
    hostile.write_bytes(b"hostile")
    os.chmod(hostile, 0o644)
    swapped = False

    if suffix:
        real_optional_stat = state_files._stat_at_optional

        def stat_then_swap(directory_fd, name):
            nonlocal swapped
            result = real_optional_stat(directory_fd, name)
            if not swapped and name == target.name:
                swapped = True
                os.replace(target, preserved)
                os.replace(hostile, target)
            return result

        monkeypatch.setattr(
            state_files,
            "_stat_at_optional",
            stat_then_swap,
        )
        with pytest.raises(
            (PermissionError, RuntimeError, ValueError),
            match="sidecar|mode|changed",
        ):
            state_files.secure_sqlite_sidecars(db_path)
    else:
        real_open = os.open

        def open_then_swap(path, *args, **kwargs):
            nonlocal swapped
            file_descriptor = real_open(path, *args, **kwargs)
            if not swapped and Path(os.fspath(path)).name == target.name:
                swapped = True
                os.replace(target, preserved)
                os.replace(hostile, target)
            return file_descriptor

        monkeypatch.setattr(state_files.os, "open", open_then_swap)
        with pytest.raises(RuntimeError, match="changed"):
            state_files.prepare_sqlite_file(db_path)

    assert swapped is True
    assert preserved.read_bytes() in {b"main", b"trusted-sidecar"}
    assert target.read_bytes() == b"hostile"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644


def test_explicit_sqlite_path_rejects_ancestor_swap_after_directory_open(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    target = parent / "events.db"
    _create_marker_database(target, "trusted")
    preserved_parent = tmp_path / "preserved-private"
    real_open = os.open
    real_fstat = os.fstat
    swapped = False

    def open_then_swap_parent(path, *args, **kwargs):
        nonlocal swapped
        file_descriptor = real_open(path, *args, **kwargs)
        if (
            not swapped
            and Path(os.fspath(path)).name == parent.name
            and stat.S_ISDIR(real_fstat(file_descriptor).st_mode)
        ):
            swapped = True
            os.replace(parent, preserved_parent)
            parent.mkdir(mode=0o700)
            _create_marker_database(target, "hostile")
        return file_descriptor

    monkeypatch.setattr(state_files.os, "open", open_then_swap_parent)

    with pytest.raises(RuntimeError, match="ancestor changed"):
        state_files.prepare_sqlite_file(target)

    assert swapped is True
    assert _marker_value(preserved_parent / target.name) == "trusted"
    assert _marker_value(target) == "hostile"


@pytest.mark.parametrize("swapped_component", ["directory", "ancestor"])
def test_ensure_private_directory_rejects_name_or_ancestor_swap_after_open(
    tmp_path,
    monkeypatch,
    swapped_component,
):
    state_files = importlib.import_module("workflow.state_files")
    ancestor = tmp_path / "ancestor"
    directory = ancestor / "private"
    directory.mkdir(mode=0o700, parents=True)
    marker = directory / "marker.txt"
    marker.write_text("trusted", encoding="utf-8")
    real_open = os.open
    real_fstat = os.fstat
    swapped = False
    if swapped_component == "directory":
        preserved = ancestor / "preserved-private"
    else:
        preserved = tmp_path / "preserved-ancestor"

    def open_then_swap(path, *args, **kwargs):
        nonlocal swapped
        file_descriptor = real_open(path, *args, **kwargs)
        opened = real_fstat(file_descriptor)
        component_name = (
            directory.name if swapped_component == "directory" else ancestor.name
        )
        if (
            not swapped
            and Path(os.fspath(path)).name == component_name
            and stat.S_ISDIR(opened.st_mode)
        ):
            swapped = True
            if swapped_component == "directory":
                os.replace(directory, preserved)
                directory.mkdir(mode=0o700)
            else:
                os.replace(ancestor, preserved)
                directory.mkdir(mode=0o700, parents=True)
            (directory / "marker.txt").write_text("hostile", encoding="utf-8")
        return file_descriptor

    monkeypatch.setattr(state_files.os, "open", open_then_swap)

    with pytest.raises(RuntimeError, match="changed"):
        state_files.ensure_private_directory(directory, create=False)

    assert swapped is True
    assert (directory / "marker.txt").read_text(encoding="utf-8") == "hostile"
    preserved_marker = (
        preserved / "marker.txt"
        if swapped_component == "directory"
        else preserved / "private" / "marker.txt"
    )
    assert preserved_marker.read_text(encoding="utf-8") == "trusted"


def test_ensure_private_directory_close_error_does_not_mask_validation_error(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    directory = tmp_path / "unsafe"
    directory.mkdir(mode=0o770)
    real_close = os.close
    real_fstat = os.fstat
    closed_with_error = False

    def close_then_raise(file_descriptor):
        nonlocal closed_with_error
        opened = real_fstat(file_descriptor)
        real_close(file_descriptor)
        if stat.S_ISDIR(opened.st_mode) and not closed_with_error:
            closed_with_error = True
            raise RuntimeError("injected directory close failure")

    monkeypatch.setattr(state_files.os, "close", close_then_raise)

    with pytest.raises(PermissionError, match="directory mode"):
        state_files.ensure_private_directory(directory, create=False)

    assert closed_with_error is True


def test_default_state_cleanup_preserves_primary_and_attempts_unlock_and_all_closes(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    real_open = os.open
    real_close = os.close
    real_fstat = os.fstat
    real_flock = state_files.fcntl.flock
    acquired = []
    unlock_attempted = False

    def tracking_open(*args, **kwargs):
        file_descriptor = real_open(*args, **kwargs)
        acquired.append(file_descriptor)
        return file_descriptor

    def fail_target_stat(directory_fd, name):
        if name == "events.db":
            raise ValueError("primary default failure")
        return state_files.os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )

    def flock_with_unlock_failure(file_descriptor, operation):
        nonlocal unlock_attempted
        if operation == state_files.fcntl.LOCK_UN:
            unlock_attempted = True
            raise RuntimeError("injected unlock failure")
        return real_flock(file_descriptor, operation)

    monkeypatch.setattr(state_files.os, "open", tracking_open)
    monkeypatch.setattr(state_files, "_stat_at_optional", fail_target_stat)
    monkeypatch.setattr(state_files.fcntl, "flock", flock_with_unlock_failure)
    try:
        with pytest.raises(ValueError, match="primary default failure"):
            state_files.default_state_database_path(tmp_path, "events.db")
        assert unlock_attempted is True
        for file_descriptor in acquired:
            with pytest.raises(OSError) as caught:
                real_fstat(file_descriptor)
            assert caught.value.errno == errno.EBADF
    finally:
        for file_descriptor in acquired:
            try:
                real_close(file_descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


def test_default_state_bundle_close_error_does_not_mask_migration_error(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    real_bundle_close = state_files._PinnedLegacyBundle.close
    close_attempted = False

    def fail_migration(*args, **kwargs):
        raise ValueError("primary migration failure")

    def close_then_raise(bundle):
        nonlocal close_attempted
        real_bundle_close(bundle)
        close_attempted = True
        raise RuntimeError("injected bundle close failure")

    monkeypatch.setattr(state_files, "_migrate_legacy_database", fail_migration)
    monkeypatch.setattr(state_files._PinnedLegacyBundle, "close", close_then_raise)

    with pytest.raises(ValueError, match="primary migration failure"):
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert close_attempted is True


def test_legacy_shm_is_pinned_but_never_hardlinked_into_snapshot(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    legacy_path = data_dir / "agui_events.db"
    keeper = _open_wal_marker_database(legacy_path)
    legacy_wal = Path(f"{legacy_path}-wal")
    legacy_shm = Path(f"{legacy_path}-shm")
    os.chmod(legacy_wal, 0o600)
    os.chmod(legacy_shm, 0o600)
    main_before = _stable_file_state(legacy_path)
    wal_before = _stable_file_state(legacy_wal)
    shm_inode = os.stat(legacy_shm).st_ino
    real_link = os.link
    linked_sources = []

    def record_link_source(source, destination, *args, **kwargs):
        linked_sources.append(os.fspath(source))
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(state_files.os, "link", record_link_source)
    try:
        target = state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

        assert _marker_value(target) == "trusted"
        assert all(not source.endswith("-shm") for source in linked_sources)
        assert _stable_file_state(legacy_path) == main_before
        assert _stable_file_state(legacy_wal) == wal_before
        assert os.stat(legacy_shm).st_ino == shm_inode
        assert stat.S_IMODE(os.stat(legacy_shm).st_mode) == 0o600
        assert _migration_artifacts(target) == []
    finally:
        keeper.close()


@pytest.mark.parametrize("iteration", range(5))
def test_readonly_snapshot_accepts_ctime_only_source_change(
    tmp_path,
    monkeypatch,
    iteration,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / f"data-{iteration}"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    stable_before = _stable_file_state(legacy_path)
    real_connect = sqlite3.connect
    real_fstat = os.fstat
    legacy_inode = os.stat(legacy_path).st_ino
    source_wrapped = False
    ctime_changed = False
    connect_count = 0

    def change_ctime_only() -> None:
        nonlocal ctime_changed
        ctime_changed = True

    class CtimeOnlyStat:
        def __init__(self, original):
            self._original = original

        @property
        def st_ctime_ns(self):
            return self._original.st_ctime_ns + 1

        def __getattr__(self, name):
            return getattr(self._original, name)

    def fstat_with_ctime_only_change(file_descriptor):
        result = real_fstat(file_descriptor)
        if ctime_changed and result.st_ino == legacy_inode:
            return CtimeOnlyStat(result)
        return result

    def connect_with_ctime_change(database, *args, **kwargs):
        nonlocal source_wrapped, connect_count
        connect_count += 1
        connection = real_connect(database, *args, **kwargs)
        # Connect ordinals: 1=WAL-recovery connection, 2=source (the
        # connection .backup() is actually invoked on), 3=destination.
        if connect_count == 2 and not source_wrapped:
            source_wrapped = True
            return _BeforeBackupConnection(connection, change_ctime_only)
        return connection

    monkeypatch.setattr(
        state_files.sqlite3,
        "connect",
        connect_with_ctime_change,
    )
    monkeypatch.setattr(state_files.os, "fstat", fstat_with_ctime_only_change)

    target = state_files.default_state_database_path(
        tmp_path,
        f"events-{iteration}.db",
        legacy_path=legacy_path,
    )

    assert source_wrapped is True
    assert _marker_value(target, connect=real_connect) == "trusted"
    assert _stable_file_state(legacy_path) == stable_before


@pytest.mark.parametrize("missing_legacy", [False, True])
def test_created_target_rollback_error_does_not_mask_validation_error(
    tmp_path,
    monkeypatch,
    missing_legacy,
):
    state_files = importlib.import_module("workflow.state_files")
    real_revalidate = state_files._revalidate_private_at
    real_unlink = state_files._unlink_created_at
    rollback_attempted = False

    def fail_created_target_validation(directory_fd, name, expected, **kwargs):
        if name == "events.db":
            raise ValueError("primary created-target validation failure")
        return real_revalidate(
            directory_fd,
            name,
            expected,
            **kwargs,
        )

    def fail_created_target_rollback(directory_fd, name, expected):
        nonlocal rollback_attempted
        if name == "events.db":
            rollback_attempted = True
            raise RuntimeError("injected created-target rollback failure")
        return real_unlink(directory_fd, name, expected)

    monkeypatch.setattr(
        state_files,
        "_revalidate_private_at",
        fail_created_target_validation,
    )
    monkeypatch.setattr(
        state_files,
        "_unlink_created_at",
        fail_created_target_rollback,
    )
    kwargs = {}
    if missing_legacy:
        kwargs["legacy_path"] = tmp_path / "data" / "missing-legacy.db"

    with pytest.raises(
        ValueError,
        match="primary created-target validation failure",
    ):
        state_files.default_state_database_path(
            tmp_path,
            "events.db",
            **kwargs,
        )

    assert rollback_attempted is True


def test_temporary_fsync_close_error_does_not_mask_fsync_error(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    legacy_path = data_dir / "agui_events.db"
    _create_marker_database(legacy_path, "trusted")
    target = data_dir / "multiagent_state" / "agui_events.db"
    real_fsync = os.fsync
    real_close = os.close
    real_fstat = os.fstat
    temporary_fsync_fd = None
    close_attempted = False

    def fail_temporary_fsync(file_descriptor):
        nonlocal temporary_fsync_fd
        if stat.S_ISREG(real_fstat(file_descriptor).st_mode):
            temporary_fsync_fd = file_descriptor
            raise ValueError("primary temporary fsync failure")
        return real_fsync(file_descriptor)

    def close_then_raise(file_descriptor):
        nonlocal close_attempted
        real_close(file_descriptor)
        if file_descriptor == temporary_fsync_fd and not close_attempted:
            close_attempted = True
            raise RuntimeError("injected temporary close failure")

    monkeypatch.setattr(state_files.os, "fsync", fail_temporary_fsync)
    monkeypatch.setattr(state_files.os, "close", close_then_raise)

    with pytest.raises(ValueError, match="primary temporary fsync failure"):
        state_files.default_state_database_path(
            tmp_path,
            "agui_events.db",
            legacy_path=legacy_path,
        )

    assert close_attempted is True
    assert not target.exists()
    assert _migration_artifacts(target) == []


@pytest.mark.parametrize(
    ("directory", "case_name"),
    [(Path("."), "dot"), (Path(""), "empty")],
)
def test_ensure_private_directory_accepts_relative_current_directory(
    tmp_path,
    monkeypatch,
    directory,
    case_name,
):
    state_files = importlib.import_module("workflow.state_files")
    private_cwd = tmp_path / case_name
    private_cwd.mkdir(mode=0o700)
    monkeypatch.chdir(private_cwd)

    assert state_files.ensure_private_directory(
        directory,
        create=False,
    ) == directory


def test_ensure_private_directory_handles_filesystem_root_explicitly(monkeypatch):
    state_files = importlib.import_module("workflow.state_files")
    calls = []
    monkeypatch.setattr(
        state_files.os,
        "fchmod",
        lambda *_args: calls.append("fchmod"),
    )

    with pytest.raises(ValueError, match="filesystem root"):
        state_files.ensure_private_directory(Path("/"), create=False)
    assert calls == []


def test_workflow_result_outbox_accepts_relative_database_in_private_cwd(
    tmp_path,
    monkeypatch,
):
    from workflow.result_outbox import WorkflowResultOutbox

    private_cwd = tmp_path / "private-cwd"
    private_cwd.mkdir(mode=0o700)
    monkeypatch.chdir(private_cwd)

    outbox = WorkflowResultOutbox("relative.db")
    try:
        assert outbox._db_path == Path("relative.db")
        assert Path("relative.db").is_file()
        assert stat.S_IMODE(os.stat("relative.db").st_mode) == 0o600
    finally:
        outbox.close()
