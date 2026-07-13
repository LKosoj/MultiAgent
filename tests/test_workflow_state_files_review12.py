from __future__ import annotations

import errno
import importlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat

import pytest


def _create_marker_database(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (value,))
        connection.commit()
    os.chmod(path, 0o600)


def _marker_value(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("SELECT value FROM marker").fetchone()[0])


def _migration_artifacts(target: Path) -> list[Path]:
    return list(target.parent.glob(f".{target.name}.*"))


def test_created_nonce_is_removed_when_first_fstat_fails_and_restart_retries(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    legacy_path = tmp_path / "data" / "legacy-events.db"
    target = tmp_path / "data" / "multiagent_state" / "events.db"
    real_open = os.open
    real_fstat = os.fstat
    created_target_fd: int | None = None
    injected = False

    def track_created_target(path, flags, *args, **kwargs):
        nonlocal created_target_fd
        file_descriptor = real_open(path, flags, *args, **kwargs)
        if os.fspath(path).startswith(".state-create-") and flags & os.O_EXCL:
            created_target_fd = file_descriptor
        return file_descriptor

    def fail_first_created_target_fstat(file_descriptor):
        nonlocal injected
        if file_descriptor == created_target_fd and not injected:
            injected = True
            raise OSError(errno.EIO, "injected post-create fstat failure")
        return real_fstat(file_descriptor)

    monkeypatch.setattr(state_files.os, "open", track_created_target)
    monkeypatch.setattr(state_files.os, "fstat", fail_first_created_target_fstat)

    with pytest.raises(OSError, match="post-create fstat failure"):
        state_files.default_state_database_path(
            tmp_path,
            target.name,
            legacy_path=legacy_path,
        )

    assert injected is True
    assert not target.exists()
    assert _migration_artifacts(target) == []

    monkeypatch.setattr(state_files.os, "open", real_open)
    monkeypatch.setattr(state_files.os, "fstat", real_fstat)
    _create_marker_database(legacy_path, "trusted-after-restart")

    restarted_target = state_files.default_state_database_path(
        tmp_path,
        target.name,
        legacy_path=legacy_path,
    )

    assert restarted_target == target
    assert _marker_value(target) == "trusted-after-restart"
    assert _migration_artifacts(target) == []


@pytest.mark.parametrize("failure_stage", ["snapshot", "publish"])
def test_post_link_exception_removes_only_owned_inode_and_restart_retries(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    state_files = importlib.import_module("workflow.state_files")
    legacy_path = tmp_path / "data" / "legacy-events.db"
    _create_marker_database(legacy_path, "trusted")
    target = tmp_path / "data" / "multiagent_state" / "events.db"
    real_link = os.link
    injected = False

    def link_then_raise(source, destination, *args, **kwargs):
        nonlocal injected
        result = real_link(source, destination, *args, **kwargs)
        destination_name = os.fspath(destination)
        is_snapshot = destination_name.startswith(f".{target.name}.snapshot-")
        is_publish = destination_name == target.name
        if not injected and (
            (failure_stage == "snapshot" and is_snapshot)
            or (failure_stage == "publish" and is_publish)
        ):
            injected = True
            raise OSError(errno.EIO, f"injected post-{failure_stage} link failure")
        return result

    monkeypatch.setattr(state_files.os, "link", link_then_raise)

    with pytest.raises(OSError, match=f"post-{failure_stage} link failure"):
        state_files.default_state_database_path(
            tmp_path,
            target.name,
            legacy_path=legacy_path,
        )

    assert injected is True
    assert not target.exists()
    assert _migration_artifacts(target) == []
    assert _marker_value(legacy_path) == "trusted"

    monkeypatch.setattr(state_files.os, "link", real_link)
    restarted_target = state_files.default_state_database_path(
        tmp_path,
        target.name,
        legacy_path=legacy_path,
    )

    assert restarted_target == target
    assert _marker_value(target) == "trusted"
    assert _migration_artifacts(target) == []


def test_link_error_never_claims_or_unlinks_a_foreign_destination_inode(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    source = tmp_path / "source.db"
    foreign = tmp_path / "foreign.db"
    destination = tmp_path / "destination.db"
    source.write_bytes(b"source")
    foreign.write_bytes(b"foreign")
    os.chmod(source, 0o600)
    os.chmod(foreign, 0o600)
    directory_fd = os.open(
        tmp_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    real_link = os.link

    def install_foreign_destination_then_raise(
        _source,
        destination_name,
        *,
        dst_dir_fd,
        **_kwargs,
    ):
        real_link(
            foreign.name,
            destination_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=False,
        )
        raise OSError(errno.EEXIST, "injected foreign destination")

    monkeypatch.setattr(state_files.os, "link", install_foreign_destination_then_raise)
    owned_entries: dict[str, tuple[int, int]] = {}
    expected_source = (source.stat().st_dev, source.stat().st_ino)
    try:
        with pytest.raises(OSError, match="foreign destination"):
            state_files._link_owned_at(
                directory_fd,
                source.name,
                directory_fd,
                destination.name,
                expected_source=expected_source,
                owned_entries=owned_entries,
            )
        assert owned_entries == {}
        state_files._unlink_created_at(
            directory_fd,
            destination.name,
            expected_source,
        )
        assert destination.read_bytes() == b"foreign"
        assert destination.stat().st_ino == foreign.stat().st_ino
    finally:
        os.close(directory_fd)


def test_ensure_private_directory_softens_benign_mode_with_warning(
    tmp_path,
    caplog,
):
    state_files = importlib.import_module("workflow.state_files")
    directory = tmp_path / "state"
    directory.mkdir()
    os.chmod(directory, 0o755)

    caplog.set_level("WARNING", logger="workflow.state_files")
    result = state_files.ensure_private_directory(directory, create=False)

    assert result == directory
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert "tightening to 0700" in caplog.text


def test_ensure_private_directory_still_rejects_group_writable_mode(tmp_path):
    state_files = importlib.import_module("workflow.state_files")
    directory = tmp_path / "state"
    directory.mkdir()
    os.chmod(directory, 0o775)

    with pytest.raises(PermissionError, match="directory mode"):
        state_files.ensure_private_directory(directory, create=False)

    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o775


def test_default_state_database_path_prepares_directory_only_once(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    call_count = 0
    real_unlocked = state_files._default_state_database_path_unlocked

    def counting_unlocked(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_unlocked(*args, **kwargs)

    monkeypatch.setattr(
        state_files,
        "_default_state_database_path_unlocked",
        counting_unlocked,
    )

    first = state_files.default_state_database_path(tmp_path, "events.db")
    second = state_files.default_state_database_path(tmp_path, "events.db")

    assert first == second
    assert call_count == 1


def test_default_state_database_path_recovers_after_directory_removal(
    tmp_path,
    monkeypatch,
):
    state_files = importlib.import_module("workflow.state_files")
    call_count = 0
    real_unlocked = state_files._default_state_database_path_unlocked

    def counting_unlocked(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_unlocked(*args, **kwargs)

    monkeypatch.setattr(
        state_files,
        "_default_state_database_path_unlocked",
        counting_unlocked,
    )

    first = state_files.default_state_database_path(tmp_path, "events.db")
    assert call_count == 1
    assert first.parent.is_dir()

    # Simulate an external removal of the whole data/ tree after the cache
    # already recorded this path as prepared.
    shutil.rmtree(tmp_path / "data")
    assert not first.parent.is_dir()

    second = state_files.default_state_database_path(tmp_path, "events.db")

    assert second == first
    assert call_count == 2
    assert second.parent.is_dir()
