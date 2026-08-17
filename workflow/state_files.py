"""Secure filesystem primitives for local workflow state."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import stat
import sys
import threading
import time
from urllib.parse import quote
import uuid
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SQLITE_SIDECAR_MODE_RECHECK_ATTEMPTS = 8


class LegacyDatabaseUnrecoverable(RuntimeError):
    """A legacy SQLite database could not be safely recovered for migration."""

    def __init__(self, reason_code: str, *, display_path: Path) -> None:
        super().__init__(f"{reason_code}: {display_path}")
        self.reason_code = reason_code


@dataclass(eq=False)
class SQLiteBundleLease:
    """One managed in-process SQLite handle for a filesystem bundle."""

    key: str
    process_id: int
    lease_id: str
    tighten_existing: bool
    owner_uid: Optional[int]
    _state: str = "active"


@dataclass
class _ActiveSQLiteBundle:
    path: Path
    file_signature: tuple[int, ...]
    parent_signature: tuple[int, ...]
    tighten_existing: bool
    owner_uid: Optional[int]
    leases: dict[str, SQLiteBundleLease]
    finalizing_lease_id: str | None = None


_SQLITE_BUNDLE_REGISTRY_LOCK = threading.RLock()
_SQLITE_BUNDLE_REGISTRY_PID = os.getpid()
_ACTIVE_SQLITE_BUNDLES: dict[str, _ActiveSQLiteBundle] = {}
_DEFAULT_STATE_PATH_PREPARED: set[str] = set()


def _sqlite_bundle_key(path: Path | str) -> str:
    return os.path.abspath(os.fspath(path))


def _reset_sqlite_bundle_registry_after_fork() -> None:
    global _SQLITE_BUNDLE_REGISTRY_PID
    process_id = os.getpid()
    if process_id != _SQLITE_BUNDLE_REGISTRY_PID:
        _ACTIVE_SQLITE_BUNDLES.clear()
        _DEFAULT_STATE_PATH_PREPARED.clear()
        _SQLITE_BUNDLE_REGISTRY_PID = process_id


def _reset_sqlite_bundle_registry_in_child() -> None:
    global _ACTIVE_SQLITE_BUNDLES
    global _SQLITE_BUNDLE_REGISTRY_LOCK
    global _SQLITE_BUNDLE_REGISTRY_PID
    global _DEFAULT_STATE_PATH_PREPARED
    _SQLITE_BUNDLE_REGISTRY_LOCK = threading.RLock()
    _ACTIVE_SQLITE_BUNDLES = {}
    _DEFAULT_STATE_PATH_PREPARED = set()
    _SQLITE_BUNDLE_REGISTRY_PID = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_sqlite_bundle_registry_in_child)


def _validate_active_sqlite_bundle(
    entry: _ActiveSQLiteBundle,
    *,
    owner_uid: Optional[int],
) -> None:
    expected_owner = _expected_owner(owner_uid)
    try:
        parent_stat = os.stat(entry.path.parent, follow_symlinks=False)
        file_stat = os.stat(entry.path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise RuntimeError("active SQLite bundle path changed") from exc
    if (
        _directory_signature(parent_stat) != entry.parent_signature
        or _file_signature(file_stat, content_sensitive=False) != entry.file_signature
    ):
        raise RuntimeError("active SQLite bundle path changed")
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
        or file_stat.st_uid != expected_owner
        or stat.S_IMODE(file_stat.st_mode) != PRIVATE_FILE_MODE
    ):
        raise PermissionError("active SQLite database ownership or mode changed")
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        try:
            sidecar_stat = os.stat(f"{entry.path}{suffix}", follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(sidecar_stat.st_mode)
            or sidecar_stat.st_nlink != 1
            or sidecar_stat.st_uid != expected_owner
            or stat.S_IMODE(sidecar_stat.st_mode) != PRIVATE_FILE_MODE
        ):
            raise PermissionError(
                f"active SQLite sidecar ownership or mode changed: {suffix}"
            )


def acquire_sqlite_bundle(
    path: Path | str,
    *,
    tighten_existing: bool = True,
    owner_uid: Optional[int] = None,
) -> SQLiteBundleLease:
    """Register a managed connection without closing raw FDs under live peers."""
    db_path = Path(path)
    key = _sqlite_bundle_key(db_path)
    with _SQLITE_BUNDLE_REGISTRY_LOCK:
        _reset_sqlite_bundle_registry_after_fork()
        entry = _ACTIVE_SQLITE_BUNDLES.get(key)
        if entry is not None and entry.finalizing_lease_id is not None:
            completed_lease = entry.leases[entry.finalizing_lease_id]
            _finalize_sqlite_bundle_release(entry)
            completed_lease._state = "completed_on_behalf"
            del _ACTIVE_SQLITE_BUNDLES[key]
            entry = None
        if entry is None:
            prepare_sqlite_file(
                db_path,
                tighten_existing=tighten_existing,
                owner_uid=owner_uid,
            )
            secure_sqlite_sidecars(
                db_path,
                tighten_existing=tighten_existing,
                owner_uid=owner_uid,
            )
            absolute_path = Path(key)
            entry = _ActiveSQLiteBundle(
                path=absolute_path,
                file_signature=_file_signature(
                    os.stat(absolute_path, follow_symlinks=False),
                    content_sensitive=False,
                ),
                parent_signature=_directory_signature(
                    os.stat(absolute_path.parent, follow_symlinks=False)
                ),
                tighten_existing=tighten_existing,
                owner_uid=owner_uid,
                leases={},
            )
            _ACTIVE_SQLITE_BUNDLES[key] = entry
        else:
            _validate_active_sqlite_bundle(entry, owner_uid=owner_uid)
        lease_id = uuid.uuid4().hex
        lease = SQLiteBundleLease(
            key=key,
            process_id=os.getpid(),
            lease_id=lease_id,
            tighten_existing=tighten_existing,
            owner_uid=owner_uid,
        )
        entry.leases[lease_id] = lease
        return lease


def _finalize_sqlite_bundle_release(entry: _ActiveSQLiteBundle) -> None:
    lease_id = entry.finalizing_lease_id
    if lease_id is None or set(entry.leases) != {lease_id}:
        raise RuntimeError("SQLite bundle finalization state is invalid")
    _secure_explicit_sqlite_bundle(
        entry.path,
        tighten_existing=entry.tighten_existing,
        owner_uid=entry.owner_uid,
    )


def release_sqlite_bundle(lease: SQLiteBundleLease) -> None:
    """Release a managed connection and secure sidecars after the final close."""
    if not isinstance(lease, SQLiteBundleLease):
        raise TypeError("lease must be a SQLiteBundleLease")
    with _SQLITE_BUNDLE_REGISTRY_LOCK:
        _reset_sqlite_bundle_registry_after_fork()
        if lease.process_id != os.getpid():
            return
        if lease._state == "completed_on_behalf":
            lease._state = "released"
            return
        if lease._state != "active":
            raise RuntimeError("SQLite bundle lease is not active")
        entry = _ACTIVE_SQLITE_BUNDLES.get(lease.key)
        if entry is None or entry.leases.get(lease.lease_id) is not lease:
            raise RuntimeError("SQLite bundle lease is not active")
        if entry.finalizing_lease_id is not None:
            if entry.finalizing_lease_id != lease.lease_id:
                raise RuntimeError("SQLite bundle finalization state is invalid")
        elif len(entry.leases) > 1:
            del entry.leases[lease.lease_id]
            lease._state = "released"
            return
        else:
            entry.finalizing_lease_id = lease.lease_id
        _secure_explicit_sqlite_bundle(
            entry.path,
            tighten_existing=lease.tighten_existing,
            owner_uid=lease.owner_uid,
        )
        del entry.leases[lease.lease_id]
        lease._state = "released"
        del _ACTIVE_SQLITE_BUNDLES[lease.key]


def _expected_owner(owner_uid: Optional[int]) -> int:
    return os.geteuid() if owner_uid is None else int(owner_uid)


def _validate_or_soften_directory_mode(
    directory_fd: int,
    mode: int,
    *,
    description: object,
    tighten_existing: bool,
) -> bool:
    """Reject group/other-writable directory modes; tighten benign ones.

    A mode outside 0700 (for example 0750/0755, produced by a permissive
    umask) is not itself a security problem as long as nothing but the
    owner can write to the directory; only the write bits for group/other
    (0o022) are actually dangerous. Those are still a hard failure. A
    benign mode is tightened back to 0700 and logged only when the caller
    explicitly opts in with ``tighten_existing=True``.
    """
    if not tighten_existing or mode & 0o022:
        raise PermissionError(f"state directory mode must be 0700: {description}")
    os.fchmod(directory_fd, PRIVATE_DIRECTORY_MODE)
    logger.warning(
        "state directory mode %s is not 0700; tightening to 0700: %s",
        oct(mode),
        description,
    )
    return True


def ensure_private_directory(
    path: Path | str,
    *,
    create: bool = True,
    tighten_existing: bool = False,
    owner_uid: Optional[int] = None,
) -> Path:
    """Create or validate one owned 0700 directory without changing its parent."""
    directory = Path(path)
    absolute = Path(os.path.abspath(os.fspath(directory)))
    is_root = absolute.parent == absolute
    if is_root:
        raise ValueError("state directory must not be the filesystem root")
    chain = _PinnedDirectoryChain.open(absolute.parent)
    try:
        chain.open_child(
            absolute.name,
            create=create,
            mode=PRIVATE_DIRECTORY_MODE,
        )
        fd_stat = os.fstat(chain.fd)
        expected_owner = _expected_owner(owner_uid)
        if fd_stat.st_uid != expected_owner:
            raise PermissionError(f"state directory owner mismatch: {directory}")
        mode = stat.S_IMODE(fd_stat.st_mode)
        if mode != PRIVATE_DIRECTORY_MODE:
            _validate_or_soften_directory_mode(
                chain.fd,
                mode,
                description=directory,
                tighten_existing=tighten_existing,
            )
            chain.refresh_last_signature()
        chain.revalidate()
    finally:
        _close_explicit_handles([], chain)
    return directory


def prepare_sqlite_file(
    path: Path | str,
    *,
    tighten_existing: bool = True,
    owner_uid: Optional[int] = None,
) -> Path:
    """Validate a private explicit SQLite path and securely create/tighten its file."""
    db_path = Path(path)
    key = _sqlite_bundle_key(db_path)
    with _SQLITE_BUNDLE_REGISTRY_LOCK:
        _reset_sqlite_bundle_registry_after_fork()
        entry = _ACTIVE_SQLITE_BUNDLES.get(key)
        if entry is not None:
            _validate_active_sqlite_bundle(entry, owner_uid=owner_uid)
        else:
            _prepare_explicit_sqlite_file(
                db_path,
                tighten_existing=tighten_existing,
                owner_uid=owner_uid,
            )
    return db_path


def secure_sqlite_sidecars(
    path: Path | str,
    *,
    tighten_existing: bool = True,
    owner_uid: Optional[int] = None,
) -> None:
    db_path = Path(path)
    key = _sqlite_bundle_key(db_path)
    with _SQLITE_BUNDLE_REGISTRY_LOCK:
        _reset_sqlite_bundle_registry_after_fork()
        entry = _ACTIVE_SQLITE_BUNDLES.get(key)
        if entry is not None:
            _validate_active_sqlite_bundle(entry, owner_uid=owner_uid)
        else:
            _secure_explicit_sqlite_bundle(
                db_path,
                tighten_existing=tighten_existing,
                owner_uid=owner_uid,
            )


def _inode_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _directory_signature(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_uid,
        stat.S_IMODE(file_stat.st_mode),
    )


def _file_signature(
    file_stat: os.stat_result,
    *,
    content_sensitive: bool,
) -> tuple[int, ...]:
    common = (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_uid,
        stat.S_IMODE(file_stat.st_mode),
    )
    if not content_sensitive:
        return common
    return (
        *common,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _regular_flags(*, writable: bool) -> int:
    access = os.O_RDWR if writable else os.O_RDONLY
    return access | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _stat_at(directory_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _stat_at_optional(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return _stat_at(directory_fd, name)
    except FileNotFoundError:
        return None


def _close_file_descriptors(
    file_descriptors,
) -> BaseException | None:
    first_error: BaseException | None = None
    for file_descriptor in file_descriptors:
        try:
            os.close(file_descriptor)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    return first_error


def _validate_open_regular_at(
    directory_fd: int,
    name: str,
    file_descriptor: int,
    *,
    tighten_existing: bool,
    owner_uid: Optional[int],
) -> os.stat_result:
    opened = os.stat(file_descriptor)
    expected_owner = _expected_owner(owner_uid)
    if not stat.S_ISREG(opened.st_mode):
        raise ValueError(f"state file is not a regular file: {name}")
    if opened.st_nlink != 1:
        opened = os.fstat(file_descriptor)
        if opened.st_nlink == 1:
            pass
        elif opened.st_nlink == 2 and _has_inflight_creation_link(
            directory_fd,
            name,
            opened,
        ):
            raise _TransientStatePublication
        else:
            raise ValueError(f"state file must have exactly one hard link: {name}")
    if opened.st_uid != expected_owner:
        raise PermissionError(f"state file owner mismatch: {name}")
    if stat.S_IMODE(opened.st_mode) != PRIVATE_FILE_MODE:
        if not tighten_existing:
            raise PermissionError(f"state file mode must be 0600: {name}")
        os.fchmod(file_descriptor, PRIVATE_FILE_MODE)
        opened = os.fstat(file_descriptor)
    current = _stat_at(directory_fd, name)
    if (
        _inode_identity(opened) != _inode_identity(current)
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or current.st_uid != expected_owner
        or stat.S_IMODE(current.st_mode) != PRIVATE_FILE_MODE
    ):
        raise RuntimeError(f"state file changed during validation: {name}")
    return opened


class _TransientStatePublication(RuntimeError):
    pass


def _has_inflight_creation_link(
    directory_fd: int,
    name: str,
    opened: os.stat_result,
) -> bool:
    expected_identity = _inode_identity(opened)
    for candidate in os.listdir(directory_fd):
        if candidate == name or not candidate.startswith(".state-create-"):
            continue
        candidate_stat = _stat_at_optional(directory_fd, candidate)
        if (
            candidate_stat is not None
            and _inode_identity(candidate_stat) == expected_identity
            and candidate_stat.st_nlink == 2
        ):
            return True
    return False


def _unlink_unpublished_nonce_at(directory_fd: int, name: str) -> None:
    if not name.startswith(".state-create-"):
        raise ValueError("state creation nonce has an invalid name")
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _open_regular_at(
    directory_fd: int,
    name: str,
    *,
    create: bool,
    exclusive: bool,
    tighten_existing: bool,
    owner_uid: Optional[int],
) -> tuple[int, os.stat_result, bool]:
    flags = _regular_flags(writable=True)
    publication_retries = 0
    while True:
        created = False
        created_identity: tuple[int, int] | None = None
        published_identity: tuple[int, int] | None = None
        temporary_name: str | None = None
        try:
            try:
                file_descriptor = os.open(name, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise
                temporary_name = f".state-create-{os.getpid()}-{uuid.uuid4().hex}"
                file_descriptor = os.open(
                    temporary_name,
                    flags | os.O_CREAT | os.O_EXCL,
                    PRIVATE_FILE_MODE,
                    dir_fd=directory_fd,
                )
                created = True
            else:
                if exclusive:
                    close_error = _close_file_descriptors((file_descriptor,))
                    if close_error is not None:
                        raise close_error
                    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), name)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError(f"state path must not be a symlink: {name}") from exc
            raise

        try:
            opened_name = temporary_name if created else name
            assert opened_name is not None
            if created:
                created_identity = _inode_identity(os.fstat(file_descriptor))
                created_entry = _stat_at(directory_fd, opened_name)
                if _inode_identity(created_entry) != created_identity:
                    raise RuntimeError(
                        f"state file changed immediately after creation: {name}"
                    )
            try:
                opened = _validate_open_regular_at(
                    directory_fd,
                    opened_name,
                    file_descriptor,
                    tighten_existing=tighten_existing,
                    owner_uid=owner_uid,
                )
            except _TransientStatePublication as transient:
                close_error = _close_file_descriptors((file_descriptor,))
                if close_error is not None:
                    raise close_error
                publication_retries += 1
                if publication_retries >= 100:
                    raise ValueError(
                        f"state file must have exactly one hard link: {name}"
                    ) from transient
                time.sleep(0.001)
                continue
            if not created:
                return file_descriptor, opened, False

            assert temporary_name is not None
            assert created_identity is not None
            try:
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                close_error = _close_file_descriptors((file_descriptor,))
                _unlink_unpublished_nonce_at(directory_fd, temporary_name)
                if close_error is not None:
                    raise close_error
                if exclusive:
                    raise
                continue
            except BaseException:
                # The link syscall may have taken effect before raising (for
                # example if a caller's shim reports failure after the real
                # link already landed). Re-stat before assuming nothing was
                # published, so the handler below still reclaims the owned
                # inode instead of orphaning a live published file.
                published_after_failure = _stat_at_optional(directory_fd, name)
                if (
                    published_after_failure is not None
                    and _inode_identity(published_after_failure) == created_identity
                ):
                    published_identity = created_identity
                raise

            published_identity = created_identity
            os.unlink(temporary_name, dir_fd=directory_fd)
            temporary_name = None
            published = _stat_at(directory_fd, name)
            if (
                _inode_identity(published) != created_identity
                or published.st_nlink != 1
            ):
                raise RuntimeError(f"state file changed while publishing: {name}")
            opened = _validate_open_regular_at(
                directory_fd,
                name,
                file_descriptor,
                tighten_existing=tighten_existing,
                owner_uid=owner_uid,
            )
            return file_descriptor, opened, True
        except BaseException:
            _close_file_descriptors((file_descriptor,))
            if published_identity is not None:
                try:
                    _unlink_created_at(
                        directory_fd,
                        name,
                        published_identity,
                    )
                except BaseException:
                    pass
            if temporary_name is not None:
                try:
                    _unlink_unpublished_nonce_at(
                        directory_fd,
                        temporary_name,
                    )
                except BaseException:
                    pass
            raise


def _validate_private_parent(
    parent: _PinnedDirectoryChain,
    owner_uid: Optional[int],
    *,
    tighten_existing: bool,
) -> None:
    parent_stat = os.fstat(parent.fd)
    expected_owner = _expected_owner(owner_uid)
    if parent_stat.st_uid != expected_owner:
        raise PermissionError(f"state directory owner mismatch: {parent.path}")
    mode = stat.S_IMODE(parent_stat.st_mode)
    if mode != PRIVATE_DIRECTORY_MODE:
        _validate_or_soften_directory_mode(
            parent.fd,
            mode,
            description=parent.path,
            tighten_existing=tighten_existing,
        )
        parent.refresh_last_signature()


def _close_explicit_handles(
    file_descriptors: list[int],
    parent: _PinnedDirectoryChain,
) -> None:
    active_error = sys.exc_info()[1]
    cleanup_error = _close_file_descriptors(reversed(file_descriptors))
    try:
        parent.close()
    except BaseException as exc:
        if cleanup_error is None:
            cleanup_error = exc
    if active_error is None and cleanup_error is not None:
        raise cleanup_error


def _prepare_explicit_sqlite_file(
    path: Path,
    *,
    tighten_existing: bool,
    owner_uid: Optional[int],
) -> None:
    parent = _PinnedDirectoryChain.open(path.parent)
    opened_files: list[int] = []
    try:
        _validate_private_parent(
            parent,
            owner_uid,
            tighten_existing=tighten_existing,
        )
        file_descriptor, opened, _ = _open_regular_at(
            parent.fd,
            path.name,
            create=True,
            exclusive=False,
            tighten_existing=tighten_existing,
            owner_uid=owner_uid,
        )
        opened_files.append(file_descriptor)
        parent.revalidate()
        _revalidate_private_at(
            parent.fd,
            path.name,
            _inode_identity(opened),
            owner_uid=owner_uid,
        )
    finally:
        _close_explicit_handles(opened_files, parent)


def _validate_sqlite_sidecar_at(
    directory_fd: int,
    name: str,
    *,
    owner_uid: Optional[int],
) -> None:
    for attempt in range(_SQLITE_SIDECAR_MODE_RECHECK_ATTEMPTS):
        sidecar = _stat_at_optional(directory_fd, name)
        if sidecar is None:
            return
        if not stat.S_ISREG(sidecar.st_mode):
            raise ValueError(f"SQLite sidecar is not a regular file: {name}")
        if sidecar.st_nlink == 0:
            if attempt + 1 < _SQLITE_SIDECAR_MODE_RECHECK_ATTEMPTS:
                os.sched_yield()
                continue
            raise RuntimeError(f"SQLite sidecar changed during validation: {name}")
        if sidecar.st_nlink != 1:
            raise ValueError(f"SQLite sidecar must have exactly one hard link: {name}")
        if sidecar.st_uid != _expected_owner(owner_uid):
            raise PermissionError(f"SQLite sidecar owner mismatch: {name}")
        if stat.S_IMODE(sidecar.st_mode) == PRIVATE_FILE_MODE:
            return
        if attempt + 1 < _SQLITE_SIDECAR_MODE_RECHECK_ATTEMPTS:
            os.sched_yield()
    raise PermissionError(f"SQLite sidecar mode must be 0600: {name}")


def _secure_explicit_sqlite_bundle(
    path: Path,
    *,
    tighten_existing: bool,
    owner_uid: Optional[int],
) -> None:
    parent = _PinnedDirectoryChain.open(path.parent)
    opened_files: list[int] = []
    try:
        _validate_private_parent(
            parent,
            owner_uid,
            tighten_existing=tighten_existing,
        )
        file_descriptor, opened, _ = _open_regular_at(
            parent.fd,
            path.name,
            create=False,
            exclusive=False,
            tighten_existing=tighten_existing,
            owner_uid=owner_uid,
        )
        opened_files.append(file_descriptor)
        database_identity = _inode_identity(opened)
        for _validation_pass in range(2):
            parent.revalidate()
            _revalidate_private_at(
                parent.fd,
                path.name,
                database_identity,
                owner_uid=owner_uid,
            )
            for suffix in SQLITE_SIDECAR_SUFFIXES:
                _validate_sqlite_sidecar_at(
                    parent.fd,
                    f"{path.name}{suffix}",
                    owner_uid=owner_uid,
                )
    finally:
        _close_explicit_handles(opened_files, parent)


class _PinnedDirectoryChain:
    def __init__(
        self,
        path: Path,
        entries: list[tuple[str | None, int, tuple[int, ...]]],
    ) -> None:
        self.path = path
        self._entries = entries

    @classmethod
    def open(cls, path: Path | str) -> _PinnedDirectoryChain:
        absolute = Path(os.path.abspath(os.fspath(path)))
        entries: list[tuple[str | None, int, tuple[int, ...]]] = []
        try:
            root_fd = os.open(os.sep, _directory_flags())
            try:
                root_stat = os.fstat(root_fd)
            except BaseException:
                _close_file_descriptors((root_fd,))
                raise
            entries.append((None, root_fd, _directory_signature(root_stat)))
            current_path = Path(os.sep)
            for name in absolute.parts[1:]:
                before = _stat_at(entries[-1][1], name)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                    raise ValueError(
                        f"state ancestor is not a real directory: {current_path / name}"
                    )
                child_fd = os.open(
                    name,
                    _directory_flags(),
                    dir_fd=entries[-1][1],
                )
                try:
                    after = os.fstat(child_fd)
                except BaseException:
                    _close_file_descriptors((child_fd,))
                    raise
                if _inode_identity(before) != _inode_identity(after):
                    _close_file_descriptors((child_fd,))
                    raise RuntimeError(
                        f"state ancestor changed while opening: {current_path / name}"
                    )
                current_path /= name
                entries.append((name, child_fd, _directory_signature(after)))
            return cls(absolute, entries)
        except BaseException:
            _close_file_descriptors(entry[1] for entry in reversed(entries))
            raise

    @property
    def fd(self) -> int:
        return self._entries[-1][1]

    def open_child(self, name: str, *, create: bool, mode: int) -> None:
        if not name or Path(name).name != name:
            raise ValueError("state directory child must be a basename")
        if create:
            try:
                os.mkdir(name, mode, dir_fd=self.fd)
            except FileExistsError:
                pass
        before = _stat_at(self.fd, name)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise ValueError(f"state directory is not a directory: {self.path / name}")
        child_fd = os.open(name, _directory_flags(), dir_fd=self.fd)
        try:
            after = os.fstat(child_fd)
        except BaseException:
            _close_file_descriptors((child_fd,))
            raise
        if _inode_identity(before) != _inode_identity(after):
            _close_file_descriptors((child_fd,))
            raise RuntimeError(
                f"state directory changed while opening: {self.path / name}"
            )
        self.path /= name
        self._entries.append((name, child_fd, _directory_signature(after)))

    def create_and_open_child(self, name: str, mode: int) -> None:
        self.open_child(name, create=True, mode=mode)

    def refresh_last_signature(self) -> None:
        name, file_descriptor, _ = self._entries[-1]
        self._entries[-1] = (
            name,
            file_descriptor,
            _directory_signature(os.fstat(file_descriptor)),
        )

    def revalidate(self) -> None:
        for index, (name, file_descriptor, expected) in enumerate(self._entries):
            opened = os.fstat(file_descriptor)
            if _directory_signature(opened) != expected:
                raise RuntimeError(
                    f"state ancestor changed during migration: {self.path}"
                )
            if index == 0:
                continue
            current = _stat_at(self._entries[index - 1][1], str(name))
            if _directory_signature(current) != expected:
                raise RuntimeError(
                    f"state ancestor changed during migration: {self.path}"
                )

    def close(self) -> None:
        close_error = _close_file_descriptors(
            entry[1] for entry in reversed(self._entries)
        )
        self._entries.clear()
        if close_error is not None:
            raise close_error


def _read_fd_digest(file_descriptor: int) -> bytes:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(file_descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.digest()
        digest.update(chunk)
        offset += len(chunk)


class _PinnedLegacyFile:
    def __init__(
        self,
        *,
        name: str,
        suffix: str,
        display_path: Path,
        file_descriptor: int,
    ) -> None:
        self.name = name
        self.suffix = suffix
        self.display_path = display_path
        self.file_descriptor = file_descriptor
        self.content_sensitive = suffix != "-shm"
        self._baseline: tuple[int, ...] | None = None
        self._digest: bytes | None = None

    def capture_baseline(self, parent_fd: int) -> None:
        before = os.fstat(self.file_descriptor)
        current = _stat_at(parent_fd, self.name)
        if _inode_identity(before) != _inode_identity(current):
            raise RuntimeError(
                f"legacy state changed during migration: {self.display_path}"
            )
        digest = (
            _read_fd_digest(self.file_descriptor) if self.content_sensitive else None
        )
        after = os.fstat(self.file_descriptor)
        signature = _file_signature(
            after,
            content_sensitive=self.content_sensitive,
        )
        if (
            _file_signature(
                before,
                content_sensitive=self.content_sensitive,
            )
            != signature
        ):
            raise RuntimeError(
                f"legacy state changed during migration: {self.display_path}"
            )
        self._baseline = signature
        self._digest = digest

    def revalidate(self, parent_fd: int) -> None:
        if self._baseline is None:
            raise RuntimeError("legacy state baseline was not captured")
        opened_before = os.fstat(self.file_descriptor)
        current = _stat_at(parent_fd, self.name)
        if _inode_identity(opened_before) != _inode_identity(current):
            raise RuntimeError(
                f"legacy state changed during migration: {self.display_path}"
            )
        digest = (
            _read_fd_digest(self.file_descriptor) if self.content_sensitive else None
        )
        opened_after = os.fstat(self.file_descriptor)
        if (
            _file_signature(
                opened_before,
                content_sensitive=self.content_sensitive,
            )
            != self._baseline
            or _file_signature(
                opened_after,
                content_sensitive=self.content_sensitive,
            )
            != self._baseline
            or digest != self._digest
        ):
            raise RuntimeError(
                f"legacy state changed during migration: {self.display_path}"
            )

    def close(self) -> None:
        os.close(self.file_descriptor)


class _PinnedLegacyBundle:
    def __init__(
        self,
        parent: _PinnedDirectoryChain,
        members: list[_PinnedLegacyFile],
        absent_members: list[tuple[str, Path]],
    ) -> None:
        self.parent = parent
        self.members = members
        self.absent_members = absent_members

    def _revalidate_absent_members(self) -> None:
        for name, display_path in self.absent_members:
            if _stat_at_optional(self.parent.fd, name) is not None:
                raise RuntimeError(
                    f"legacy state changed during migration: {display_path}"
                )

    def capture_baseline(self) -> None:
        self.parent.revalidate()
        self._revalidate_absent_members()
        for member in self.members:
            member.capture_baseline(self.parent.fd)

    def revalidate(self) -> None:
        self.parent.revalidate()
        self._revalidate_absent_members()
        for member in self.members:
            member.revalidate(self.parent.fd)

    def close(self) -> None:
        first_error: BaseException | None = None
        for member in reversed(self.members):
            try:
                member.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self.members.clear()
        try:
            self.parent.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error


def _pin_optional_legacy_bundle(
    legacy_path: Path,
) -> _PinnedLegacyBundle | None:
    absolute = Path(os.path.abspath(os.fspath(legacy_path)))
    try:
        parent = _PinnedDirectoryChain.open(absolute.parent)
    except (FileNotFoundError, PermissionError, ValueError):
        return None
    members: list[_PinnedLegacyFile] = []
    absent_members: list[tuple[str, Path]] = []
    try:
        initial: list[tuple[str, str, Path, os.stat_result]] = []
        for suffix in ("", *SQLITE_SIDECAR_SUFFIXES):
            candidate = Path(f"{absolute}{suffix}")
            try:
                candidate_stat = _stat_at(parent.fd, candidate.name)
            except FileNotFoundError:
                if not suffix:
                    parent.close()
                    return None
                absent_members.append((candidate.name, candidate))
                continue
            if (
                stat.S_ISLNK(candidate_stat.st_mode)
                or not stat.S_ISREG(candidate_stat.st_mode)
                or candidate_stat.st_nlink != 1
                or candidate_stat.st_uid != os.geteuid()
            ):
                parent.close()
                return None
            initial.append((candidate.name, suffix, candidate, candidate_stat))
        for name, suffix, candidate, initial_stat in initial:
            try:
                file_descriptor = os.open(
                    name,
                    _regular_flags(writable=False),
                    dir_fd=parent.fd,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"legacy state changed during migration: {candidate}"
                ) from exc
            try:
                opened = os.fstat(file_descriptor)
            except BaseException:
                _close_file_descriptors((file_descriptor,))
                raise
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _inode_identity(opened) != _inode_identity(initial_stat)
            ):
                _close_file_descriptors((file_descriptor,))
                raise RuntimeError(
                    f"legacy state changed during migration: {candidate}"
                )
            members.append(
                _PinnedLegacyFile(
                    name=name,
                    suffix=suffix,
                    display_path=candidate,
                    file_descriptor=file_descriptor,
                )
            )
        for member in members:
            os.fchmod(member.file_descriptor, PRIVATE_FILE_MODE)
        bundle = _PinnedLegacyBundle(parent, members, absent_members)
        bundle.parent.revalidate()
        return bundle
    except BaseException:
        _close_file_descriptors(member.file_descriptor for member in reversed(members))
        members.clear()
        try:
            parent.close()
        except BaseException:
            pass
        raise


def _open_or_create_private_at(
    directory_fd: int,
    name: str,
) -> tuple[int, os.stat_result]:
    file_descriptor, opened, _ = _open_regular_at(
        directory_fd,
        name,
        create=True,
        exclusive=False,
        tighten_existing=True,
        owner_uid=None,
    )
    return file_descriptor, opened


def _create_private_at(
    directory_fd: int,
    name: str,
) -> tuple[int, os.stat_result]:
    file_descriptor, opened, _ = _open_regular_at(
        directory_fd,
        name,
        create=True,
        exclusive=True,
        tighten_existing=True,
        owner_uid=None,
    )
    return file_descriptor, opened


def _unlink_created_at(
    directory_fd: int,
    name: str,
    expected: tuple[int, int] | None,
) -> None:
    if expected is None:
        return
    current = _stat_at_optional(directory_fd, name)
    if current is None or _inode_identity(current) != expected:
        return
    os.unlink(name, dir_fd=directory_fd)


def _link_owned_at(
    source_directory_fd: int,
    source_name: str,
    destination_directory_fd: int,
    destination_name: str,
    *,
    expected_source: tuple[int, int],
    owned_entries: dict[str, tuple[int, int]],
) -> tuple[int, int]:
    try:
        os.link(
            source_name,
            destination_name,
            src_dir_fd=source_directory_fd,
            dst_dir_fd=destination_directory_fd,
            follow_symlinks=False,
        )
    except BaseException:
        try:
            destination = _stat_at_optional(
                destination_directory_fd,
                destination_name,
            )
            if (
                destination is not None
                and _inode_identity(destination) == expected_source
            ):
                owned_entries[destination_name] = expected_source
        except BaseException:
            pass
        raise

    owned_entries[destination_name] = expected_source
    destination = _stat_at(destination_directory_fd, destination_name)
    actual = _inode_identity(destination)
    owned_entries[destination_name] = actual
    if actual != expected_source:
        raise RuntimeError(
            f"state link source changed during migration: {destination_name}"
        )
    return actual


def _revalidate_private_at(
    directory_fd: int,
    name: str,
    expected: tuple[int, int],
    *,
    owner_uid: Optional[int] = None,
) -> None:
    current = _stat_at(directory_fd, name)
    if (
        _inode_identity(current) != expected
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or current.st_uid != _expected_owner(owner_uid)
        or stat.S_IMODE(current.st_mode) != PRIVATE_FILE_MODE
    ):
        raise RuntimeError(f"state file changed during validation: {name}")


def _copy_pinned_member_to_private(
    state_directory_fd: int,
    destination_name: str,
    member: _PinnedLegacyFile,
) -> tuple[int, os.stat_result]:
    """Copy a pinned legacy member's bytes into a freshly created private file.

    Unlike hardlinking, this never shares an inode with the live legacy file,
    so a subsequent RW WAL-recovery pass on the copy can never mutate the
    legacy original.
    """
    file_descriptor, created_stat = _create_private_at(
        state_directory_fd,
        destination_name,
    )
    created_identity = _inode_identity(created_stat)
    try:
        offset = 0
        while True:
            chunk = os.pread(member.file_descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(file_descriptor, view)
                view = view[written:]
            offset += len(chunk)
        os.fsync(file_descriptor)
        opened = os.fstat(file_descriptor)
    except BaseException:
        _close_file_descriptors((file_descriptor,))
        try:
            _unlink_created_at(
                state_directory_fd,
                destination_name,
                created_identity,
            )
        except BaseException:
            pass
        raise
    return file_descriptor, opened


def _recover_and_collapse_private_copy(
    state_directory_fd: int,
    main_snapshot_name: str,
    display_path: Path,
) -> None:
    """Run WAL recovery on a private copy and collapse it to a plain file.

    Operates exclusively on the private snapshot copy (never the legacy
    original). A non-empty WAL is checkpointed and truncated, then the copy
    is demoted out of WAL mode entirely so the remainder of the migration
    only ever has to deal with a single-file database.
    """
    connection_path = f"/proc/self/fd/{state_directory_fd}/{main_snapshot_name}"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(connection_path, timeout=30)
        busy = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        if busy != 0:
            raise LegacyDatabaseUnrecoverable(
                "LEGACY_WAL_CHECKPOINT_BUSY",
                display_path=display_path,
            )
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode != ("delete",):
            raise LegacyDatabaseUnrecoverable(
                "LEGACY_WAL_RECOVERY_FAILED",
                display_path=display_path,
            )
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise LegacyDatabaseUnrecoverable(
                "LEGACY_DB_INTEGRITY_CHECK_FAILED",
                display_path=display_path,
            )
    except sqlite3.DatabaseError as exc:
        raise LegacyDatabaseUnrecoverable(
            "LEGACY_WAL_RECOVERY_FAILED",
            display_path=display_path,
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _validate_snapshot_links(
    state_fd: int,
    snapshot_names: dict[str, str],
    created_entries: dict[str, tuple[int, int]],
) -> None:
    """Confirm every private snapshot copy is still the file we created.

    Snapshots are independent copies (not hardlinks) of the legacy member,
    so the invariant to police is no longer "same inode as the pinned
    legacy fd" but "still our own single-hardlink private file at that
    name" -- i.e. nobody swapped the copy out from under us between its
    creation and its use.
    """
    for name in snapshot_names.values():
        expected = created_entries.get(name)
        if expected is None:
            raise RuntimeError(f"legacy state changed during migration: {name}")
        _revalidate_private_at(state_fd, name, expected)


def _migrate_legacy_database(
    bundle: _PinnedLegacyBundle,
    state_directory: _PinnedDirectoryChain,
    target_name: str,
) -> None:
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    snapshot_base = f".{target_name}.snapshot-{token}"
    temporary_name = f".{target_name}.migrating-{token}"
    snapshot_names: dict[str, str] = {}
    created_entries: dict[str, tuple[int, int]] = {}
    temporary_identity: tuple[int, int] | None = None
    target_identity: tuple[int, int] | None = None
    source = None
    destination = None
    migration_succeeded = False
    try:
        state_directory.revalidate()
        bundle.parent.revalidate()
        if _stat_at_optional(state_directory.fd, target_name) is not None:
            raise FileExistsError(target_name)

        bundle.capture_baseline()
        main_snapshot_name: str | None = None
        main_display_path: Path | None = None
        for member in bundle.members:
            if member.suffix == "-shm":
                continue
            snapshot_name = f"{snapshot_base}{member.suffix}"
            copied_fd, copied_stat = _copy_pinned_member_to_private(
                state_directory.fd,
                snapshot_name,
                member,
            )
            created_entries[snapshot_name] = _inode_identity(copied_stat)
            os.close(copied_fd)
            snapshot_names[member.suffix] = snapshot_name
            if member.suffix == "":
                main_snapshot_name = snapshot_name
                main_display_path = member.display_path
        bundle.revalidate()
        _validate_snapshot_links(state_directory.fd, snapshot_names, created_entries)

        assert main_snapshot_name is not None
        assert main_display_path is not None
        _recover_and_collapse_private_copy(
            state_directory.fd,
            main_snapshot_name,
            main_display_path,
        )
        snapshot_names = {"": main_snapshot_name}

        temporary_fd, temporary_stat = _create_private_at(
            state_directory.fd,
            temporary_name,
        )
        temporary_identity = _inode_identity(temporary_stat)
        created_entries[temporary_name] = temporary_identity
        os.close(temporary_fd)

        source_path = f"/proc/self/fd/{state_directory.fd}/{main_snapshot_name}"
        source_uri = f"file:{quote(source_path, safe='/')}?mode=ro"
        destination_path = f"/proc/self/fd/{state_directory.fd}/{temporary_name}"
        source = sqlite3.connect(source_uri, timeout=30.0, uri=True)
        source.execute("PRAGMA query_only=ON")
        destination = sqlite3.connect(destination_path, timeout=30.0)
        destination.execute("PRAGMA journal_mode=OFF")
        source.backup(destination)
        destination.commit()
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise sqlite3.DatabaseError("migration destination integrity check failed")
        destination.close()
        destination = None
        source.close()
        source = None

        bundle.revalidate()
        state_directory.revalidate()
        _validate_snapshot_links(state_directory.fd, snapshot_names, created_entries)
        temporary = _stat_at(state_directory.fd, temporary_name)
        if (
            _inode_identity(temporary) != temporary_identity
            or not stat.S_ISREG(temporary.st_mode)
            or temporary.st_nlink != 1
            or temporary.st_uid != os.geteuid()
        ):
            raise RuntimeError("migration destination changed before publish")
        temporary_fd = os.open(
            temporary_name,
            _regular_flags(writable=False),
            dir_fd=state_directory.fd,
        )
        try:
            os.fsync(temporary_fd)
        finally:
            active_error = sys.exc_info()[1]
            close_error = _close_file_descriptors((temporary_fd,))
            if active_error is None and close_error is not None:
                raise close_error
        if _stat_at_optional(state_directory.fd, target_name) is not None:
            raise FileExistsError(target_name)

        target_identity = _link_owned_at(
            state_directory.fd,
            temporary_name,
            state_directory.fd,
            target_name,
            expected_source=temporary_identity,
            owned_entries=created_entries,
        )
        _unlink_created_at(
            state_directory.fd,
            temporary_name,
            temporary_identity,
        )
        created_entries.pop(temporary_name, None)

        bundle.revalidate()
        state_directory.revalidate()
        _validate_snapshot_links(state_directory.fd, snapshot_names, created_entries)
        _revalidate_private_at(
            state_directory.fd,
            target_name,
            target_identity,
        )
        os.fsync(state_directory.fd)
        migration_succeeded = True
    finally:
        active_error = sys.exc_info()[1]
        cleanup_error: BaseException | None = None
        for connection in (destination, source):
            if connection is None:
                continue
            try:
                connection.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            if suffix in snapshot_names:
                continue
            private_sidecar = f"{snapshot_base}{suffix}"
            try:
                sidecar_stat = _stat_at_optional(
                    state_directory.fd,
                    private_sidecar,
                )
                if sidecar_stat is None:
                    continue
                if (
                    not stat.S_ISREG(sidecar_stat.st_mode)
                    or sidecar_stat.st_nlink != 1
                    or sidecar_stat.st_uid != os.geteuid()
                ):
                    raise RuntimeError(
                        f"private snapshot sidecar is invalid: {private_sidecar}"
                    )
                created_entries[private_sidecar] = _inode_identity(sidecar_stat)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if migration_succeeded:
            created_entries.pop(target_name, None)
        cleanup_entries = list(reversed(tuple(created_entries.items())))
        for cleanup_name, expected in cleanup_entries:
            try:
                _unlink_created_at(
                    state_directory.fd,
                    cleanup_name,
                    expected,
                )
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        try:
            os.fsync(state_directory.fd)
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if active_error is None and cleanup_error is not None:
            raise cleanup_error


def default_state_database_path(
    project_root: Path | str,
    filename: str,
    *,
    legacy_path: Path | str | None = None,
) -> Path:
    """Return a private DB path without raw-opening an active managed bundle."""
    if not filename or Path(filename).name != filename:
        raise ValueError("state database filename must be a basename")
    target_path = Path(project_root) / "data" / "multiagent_state" / filename
    key = _sqlite_bundle_key(target_path)
    with _SQLITE_BUNDLE_REGISTRY_LOCK:
        _reset_sqlite_bundle_registry_after_fork()
        entry = _ACTIVE_SQLITE_BUNDLES.get(key)
        if entry is not None:
            _validate_active_sqlite_bundle(entry, owner_uid=None)
            return target_path
        if key in _DEFAULT_STATE_PATH_PREPARED:
            if os.path.isdir(target_path.parent):
                return target_path
            # The prepared directory was removed from under us (e.g. an
            # external `rm -rf data/`): the cache would otherwise keep
            # returning a path whose parent directory no longer exists,
            # and a later open would fail with a bare FileNotFoundError.
            # Drop the stale cache entry and fall through to the full
            # preparation path so the directory gets recreated.
            _DEFAULT_STATE_PATH_PREPARED.discard(key)
        prepared_path = _default_state_database_path_unlocked(
            project_root,
            filename,
            legacy_path=legacy_path,
        )
        _DEFAULT_STATE_PATH_PREPARED.add(key)
        return prepared_path


def _default_state_database_path_unlocked(
    project_root: Path | str,
    filename: str,
    *,
    legacy_path: Path | str | None = None,
) -> Path:
    """Return a private default DB path, importing one known legacy DB."""
    if not filename or Path(filename).name != filename:
        raise ValueError("state database filename must be a basename")
    root = Path(project_root)
    target_path = root / "data" / "multiagent_state" / filename
    state_directory = _PinnedDirectoryChain.open(root)
    lock_fd: int | None = None
    lock_held = False
    try:
        state_directory.create_and_open_child("data", 0o755)
        data_stat = os.fstat(state_directory.fd)
        if data_stat.st_uid != os.geteuid():
            raise PermissionError(f"project data owner mismatch: {root / 'data'}")
        state_directory.create_and_open_child(
            "multiagent_state",
            PRIVATE_DIRECTORY_MODE,
        )
        state_stat = os.fstat(state_directory.fd)
        if state_stat.st_uid != os.geteuid():
            raise PermissionError(
                f"state directory owner mismatch: {target_path.parent}"
            )
        state_mode = stat.S_IMODE(state_stat.st_mode)
        if state_mode != PRIVATE_DIRECTORY_MODE:
            _validate_or_soften_directory_mode(
                state_directory.fd,
                state_mode,
                description=target_path.parent,
                tighten_existing=True,
            )
            state_directory.refresh_last_signature()
        lock_fd, _ = _open_or_create_private_at(
            state_directory.fd,
            ".state-migration.lock",
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_held = True
        if _stat_at_optional(state_directory.fd, filename) is not None:
            target_fd, opened_target = _open_or_create_private_at(
                state_directory.fd,
                filename,
            )
            os.close(target_fd)
            state_directory.revalidate()
            _revalidate_private_at(
                state_directory.fd,
                filename,
                _inode_identity(opened_target),
            )
        elif legacy_path is not None:
            bundle = _pin_optional_legacy_bundle(Path(legacy_path))
            if bundle is None:
                target_fd, created_stat = _create_private_at(
                    state_directory.fd,
                    filename,
                )
                created_identity = _inode_identity(created_stat)
                try:
                    os.close(target_fd)
                    state_directory.revalidate()
                    _revalidate_private_at(
                        state_directory.fd,
                        filename,
                        created_identity,
                    )
                except BaseException:
                    try:
                        _unlink_created_at(
                            state_directory.fd,
                            filename,
                            created_identity,
                        )
                    except BaseException:
                        pass
                    raise
            else:
                try:
                    state_directory.revalidate()
                    _migrate_legacy_database(
                        bundle,
                        state_directory,
                        filename,
                    )
                finally:
                    active_error = sys.exc_info()[1]
                    try:
                        bundle.close()
                    except BaseException:
                        if active_error is None:
                            raise
        else:
            target_fd, created_stat = _create_private_at(
                state_directory.fd,
                filename,
            )
            created_identity = _inode_identity(created_stat)
            try:
                os.close(target_fd)
                state_directory.revalidate()
                _revalidate_private_at(
                    state_directory.fd,
                    filename,
                    created_identity,
                )
            except BaseException:
                try:
                    _unlink_created_at(
                        state_directory.fd,
                        filename,
                        created_identity,
                    )
                except BaseException:
                    pass
                raise
    finally:
        active_error = sys.exc_info()[1]
        cleanup_error: BaseException | None = None
        if lock_fd is not None:
            if lock_held:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except BaseException as exc:
                    cleanup_error = exc
            try:
                os.close(lock_fd)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        try:
            state_directory.close()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if active_error is None and cleanup_error is not None:
            raise cleanup_error
    return target_path
