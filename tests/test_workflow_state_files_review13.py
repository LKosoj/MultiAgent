from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from workflow import state_files


def test_existing_directory_mode_is_rejected_without_tightening(tmp_path):
    directory = tmp_path / "existing"
    directory.mkdir(mode=0o700)
    directory.chmod(0o750)

    with pytest.raises(PermissionError, match="0700"):
        state_files.ensure_private_directory(
            directory,
            create=False,
            tighten_existing=False,
        )

    assert directory.stat().st_mode & 0o777 == 0o750


def test_existing_directory_mode_is_tightened_only_when_requested(tmp_path):
    directory = tmp_path / "existing"
    directory.mkdir(mode=0o700)
    directory.chmod(0o750)

    state_files.ensure_private_directory(
        directory,
        create=False,
        tighten_existing=True,
    )

    assert directory.stat().st_mode & 0o777 == 0o700


def test_filesystem_root_is_rejected_without_fchmod(monkeypatch):
    calls = []
    monkeypatch.setattr(
        state_files.os,
        "fchmod",
        lambda *_args: calls.append("fchmod"),
    )

    with pytest.raises(ValueError, match="filesystem root"):
        state_files.ensure_private_directory(Path("/"), create=False)

    assert calls == []


def test_directory_validation_error_precedes_close_error(tmp_path, monkeypatch):
    directory = tmp_path / "existing"
    directory.mkdir(mode=0o700)
    directory.chmod(0o750)

    monkeypatch.setattr(
        state_files._PinnedDirectoryChain,
        "close",
        lambda _self: (_ for _ in ()).throw(RuntimeError("close failed")),
    )

    with pytest.raises(PermissionError, match="0700"):
        state_files.ensure_private_directory(
            directory,
            create=False,
            tighten_existing=False,
        )

    assert directory.stat().st_mode & 0o777 == 0o750


def test_first_created_descriptor_stat_failure_leaves_no_published_target(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "descriptor-stat.db"
    real_stat = os.stat
    failed = False

    def fail_first_descriptor_stat(path, *args, **kwargs):
        nonlocal failed
        if isinstance(path, int) and not failed:
            failed = True
            raise OSError(errno.EIO, "injected descriptor stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(state_files.os, "stat", fail_first_descriptor_stat)

    with pytest.raises(OSError, match="descriptor stat failure"):
        state_files.prepare_sqlite_file(db_path)

    assert failed is True
    assert not db_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_first_nonce_path_stat_failure_closes_and_removes_owned_temp(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "path-stat.db"
    real_open = os.open
    real_stat = os.stat
    created_fd = None

    def track_nonce_open(path, *args, **kwargs):
        nonlocal created_fd
        file_descriptor = real_open(path, *args, **kwargs)
        if os.fspath(path).startswith(".state-create-"):
            created_fd = file_descriptor
        return file_descriptor

    def fail_nonce_path_stat(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith(".state-create-"):
            raise OSError(errno.EIO, "injected nonce path stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(state_files.os, "open", track_nonce_open)
    monkeypatch.setattr(state_files.os, "stat", fail_nonce_path_stat)

    with pytest.raises(OSError, match="nonce path stat failure"):
        state_files.prepare_sqlite_file(db_path)

    assert created_fd is not None
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(created_fd)
    assert not db_path.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("member", ["database", "wal-sidecar"])
def test_sqlite_bundle_rejects_multiply_linked_regular_files(tmp_path, member):
    db_path = tmp_path / "state.db"
    state_files.prepare_sqlite_file(db_path)
    if member == "database":
        protected_path = db_path
    else:
        protected_path = tmp_path / "state.db-wal"
        protected_path.write_bytes(b"wal")
        protected_path.chmod(0o600)
    os.link(protected_path, tmp_path / f"{member}.alias")

    with pytest.raises(
        (PermissionError, ValueError, RuntimeError), match="link|private"
    ):
        if member == "database":
            state_files.prepare_sqlite_file(db_path)
        else:
            state_files.secure_sqlite_sidecars(db_path)


def test_sidecar_created_after_absence_is_validated_without_presence_invariant(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "appearance.db"
    journal_path = tmp_path / "appearance.db-journal"
    state_files.prepare_sqlite_file(db_path)
    real_optional = state_files._stat_at_optional
    journal_checks = 0

    def create_safe_journal_between_validation_passes(directory_fd, name):
        nonlocal journal_checks
        if name == journal_path.name:
            journal_checks += 1
            if journal_checks == 1:
                return None
            if journal_checks == 2:
                journal_path.write_bytes(b"sqlite journal")
                journal_path.chmod(0o600)
        return real_optional(directory_fd, name)

    monkeypatch.setattr(
        state_files,
        "_stat_at_optional",
        create_safe_journal_between_validation_passes,
    )

    state_files.secure_sqlite_sidecars(db_path)

    assert journal_checks >= 2
    assert journal_path.read_bytes() == b"sqlite journal"


def test_sidecar_validation_never_raw_opens_live_sidecar(tmp_path, monkeypatch):
    db_path = tmp_path / "live.db"
    journal_path = tmp_path / "live.db-journal"
    state_files.prepare_sqlite_file(db_path)
    journal_path.write_bytes(b"sqlite journal")
    journal_path.chmod(0o600)
    real_open = os.open

    def reject_sidecar_open(path, *args, **kwargs):
        if os.fspath(path) == journal_path.name:
            raise AssertionError("live SQLite sidecar must not be raw-opened")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(state_files.os, "open", reject_sidecar_open)

    state_files.secure_sqlite_sidecars(db_path)


def test_present_sidecar_with_unsafe_mode_fails_closed(tmp_path):
    db_path = tmp_path / "unsafe.db"
    journal_path = tmp_path / "unsafe.db-journal"
    state_files.prepare_sqlite_file(db_path)
    journal_path.write_bytes(b"unsafe")
    journal_path.chmod(0o644)

    with pytest.raises(PermissionError, match="sidecar|mode"):
        state_files.secure_sqlite_sidecars(db_path)

    assert (journal_path.stat().st_mode & 0o777) == 0o644


def test_transient_sqlite_sidecar_mode_is_rechecked_before_rejection(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "mode-transition.db"
    journal_path = tmp_path / "mode-transition.db-journal"
    state_files.prepare_sqlite_file(db_path)
    journal_path.write_bytes(b"sqlite journal")
    journal_path.chmod(0o644)
    real_optional = state_files._stat_at_optional
    journal_checks = 0

    def complete_sqlite_mode_transition_after_first_stat(directory_fd, name):
        nonlocal journal_checks
        snapshot = real_optional(directory_fd, name)
        if name == journal_path.name:
            journal_checks += 1
            if journal_checks == 1:
                journal_path.chmod(0o600)
        return snapshot

    monkeypatch.setattr(
        state_files,
        "_stat_at_optional",
        complete_sqlite_mode_transition_after_first_stat,
    )

    state_files.secure_sqlite_sidecars(db_path)

    assert journal_checks >= 2
    assert (journal_path.stat().st_mode & 0o777) == 0o600


def test_transient_unlinked_sidecar_is_retried_and_recreated_inode_is_validated(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "unlinked-sidecar.db"
    wal_path = tmp_path / "unlinked-sidecar.db-wal"
    state_files.prepare_sqlite_file(db_path)
    wal_path.write_bytes(b"old wal")
    wal_path.chmod(0o600)
    real_optional = state_files._stat_at_optional
    wal_checks = 0

    def replace_unlinked_wal_after_first_stat(directory_fd, name):
        nonlocal wal_checks
        snapshot = real_optional(directory_fd, name)
        if name == wal_path.name:
            wal_checks += 1
            if wal_checks == 1:
                assert snapshot is not None
                wal_path.unlink()
                wal_path.write_bytes(b"replacement wal")
                wal_path.chmod(0o600)
                return os.stat_result(
                    (
                        snapshot.st_mode,
                        snapshot.st_ino,
                        snapshot.st_dev,
                        0,
                        snapshot.st_uid,
                        snapshot.st_gid,
                        snapshot.st_size,
                        snapshot.st_atime,
                        snapshot.st_mtime,
                        snapshot.st_ctime,
                    )
                )
        return snapshot

    monkeypatch.setattr(
        state_files,
        "_stat_at_optional",
        replace_unlinked_wal_after_first_stat,
    )

    state_files.secure_sqlite_sidecars(db_path)

    assert wal_checks >= 2
    assert wal_path.read_bytes() == b"replacement wal"
    assert wal_path.stat().st_nlink == 1


def test_inflight_nonce_hardlink_is_retried_without_weakening_alias_check(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "publication.db"
    nonce_path = tmp_path / ".state-create-peer"
    db_path.write_bytes(b"")
    db_path.chmod(0o600)
    os.link(db_path, nonce_path)
    real_inflight_check = state_files._has_inflight_creation_link
    observed = False

    def finish_peer_publication(directory_fd, name, opened):
        nonlocal observed
        inflight = real_inflight_check(directory_fd, name, opened)
        if inflight and not observed:
            observed = True
            nonce_path.unlink()
        return inflight

    monkeypatch.setattr(
        state_files,
        "_has_inflight_creation_link",
        finish_peer_publication,
    )

    state_files.prepare_sqlite_file(db_path)

    assert observed is True
    assert db_path.stat().st_nlink == 1
