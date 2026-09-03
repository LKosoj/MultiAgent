"""Fail-closed process isolation for canonical public Text-to-SQL benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import shutil
import signal
import sqlite3
import stat
import subprocess
import time
from typing import Callable, Mapping, Protocol, TypeVar
from urllib.request import urlopen

from .sandbox_snapshot import (
    CANONICAL_RUNTIME_SOURCE_PATHS,
    SandboxError,
    SnapshotFile,
    SourceSnapshot,
    _sha256,
    _validate_snapshot_mount_layout,
    create_source_snapshot,
    source_snapshot_from_manifest,
    source_snapshot_manifest,
    verify_source_snapshot,
)

__all__ = (
    "CANONICAL_RUNTIME_SOURCE_PATHS",
    "SandboxError",
    "SnapshotFile",
    "SourceSnapshot",
    "create_source_snapshot",
    "source_snapshot_from_manifest",
    "source_snapshot_manifest",
    "verify_source_snapshot",
)

_SYSTEM_READ_ONLY_MOUNTS = (
    Path("/usr"),
    Path("/lib"),
    Path("/lib64"),
    Path("/bin"),
)
_SYSTEM_READ_ONLY_FILES = (
    Path("/etc/ssl/certs"),
    Path("/etc/resolv.conf"),
    Path("/etc/hosts"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/localtime"),
    Path("/etc/passwd"),
    Path("/etc/group"),
)
_WRITABLE_WORKSPACE_PATHS = frozenset(
    {
        ".venv",
        "data",
        "logs",
        "memory",
        "plots",
        "sqlrag",
        ".schema_write.lock",
    }
)
_EXPECTED_SECRET_FILES = frozenset(
    {"ag_ui_auth_token_map", "openai_api_key", "openai_api_key_db", "hf_token"}
)
_RESERVED_RUNTIME_ENV = frozenset(
    {
        "PYTHONPATH",
        "PATH",
        "HOME",
        "XDG_CACHE_HOME",
        "PYTHONDONTWRITEBYTECODE",
        "PORT",
        "AG_UI_AUTH_TOKEN_MAP_FILE",
        "OPENAI_API_KEY_FILE",
        "OPENAI_API_KEY_DB_FILE",
        "HF_TOKEN_FILE",
    }
)
_SENSITIVE_ENV_PARTS = frozenset({"TOKEN", "SECRET", "PASSWORD", "API_KEY"})
_SCHEMA_CACHE_KINDS = frozenset(
    {
        "schema_table",
        "schema_ready",
        "schema_probe_fact",
        "schema_semantic_fact",
        "successful_sql_example",
    }
)
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class BwrapSandboxSpec:
    snapshot_root: Path
    venv_root: Path
    case_root: Path
    database_path: Path
    database_id: str
    secret_dir: Path
    port: int
    runtime_env: Mapping[str, str]
    expected_database_sha256: str | None = None
    shared_schema_memory_root: Path | None = None

    def __post_init__(self) -> None:
        if not self.database_id or Path(self.database_id).name != self.database_id:
            raise SandboxError("database_id must be a single path component")
        if not 1 <= self.port <= 65535:
            raise SandboxError("sandbox port is out of range")
        if self.expected_database_sha256 is not None and (
            len(self.expected_database_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.expected_database_sha256
            )
        ):
            raise SandboxError("expected database SHA-256 is invalid")
        for name, value in self.runtime_env.items():
            if not name or not name.replace("_", "").isalnum() or not name.isupper():
                raise SandboxError("runtime environment key is invalid")
            if any(part in name for part in _SENSITIVE_ENV_PARTS):
                raise SandboxError("secret values must be supplied only through files")
            if name in _RESERVED_RUNTIME_ENV:
                raise SandboxError(f"runtime environment key is reserved: {name}")
            if not isinstance(value, str):
                raise SandboxError("runtime environment values must be text")


def validate_secret_dir(secret_dir: Path) -> None:
    if secret_dir.is_symlink():
        raise SandboxError("secret directory must not be a symlink")
    try:
        secret_dir = secret_dir.resolve(strict=True)
    except OSError as exc:
        raise SandboxError("secret directory is missing") from exc
    if (
        secret_dir.is_symlink()
        or not secret_dir.is_dir()
        or secret_dir.stat().st_uid != os.getuid()
        or secret_dir.stat().st_mode & 0o777 != 0o700
    ):
        raise SandboxError("secret directory must be private and owned by this user")
    entries = {entry.name for entry in secret_dir.iterdir()}
    if entries != _EXPECTED_SECRET_FILES:
        raise SandboxError("secret directory has an unexpected file set")
    for name in _EXPECTED_SECRET_FILES:
        path = secret_dir / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o777 != 0o600
        ):
            raise SandboxError(f"secret file is not private regular input: {name}")


def overlay_paths(spec: BwrapSandboxSpec) -> tuple[Path, ...]:
    return (
        spec.case_root / "workspace",
        spec.case_root / "data",
        spec.case_root / "logs",
        spec.case_root / "plots",
        spec.case_root / "sqlrag",
        spec.case_root / ".schema_write.lock",
    )


def prepare_case_overlays(spec: BwrapSandboxSpec) -> None:
    if spec.case_root.exists() or spec.case_root.is_symlink():
        raise SandboxError(f"case state root already exists: {spec.case_root}")
    spec.case_root.mkdir(parents=True, mode=0o700)
    spec.case_root.chmod(0o700)
    for path in overlay_paths(spec):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        if path.suffix == ".db" or path.name == ".schema_write.lock":
            path.touch(exist_ok=False)
            path.chmod(0o600)
        else:
            path.mkdir(exist_ok=False, mode=0o700)
            path.chmod(0o700)
    _prepare_workspace_mountpoints(spec)


def prepare_shared_schema_memory(root: Path) -> None:
    """Create the per-database schema-memory mount before a benchmark case."""

    if root.exists() and root.is_symlink():
        raise SandboxError("shared schema-memory root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    database = root / "smolagents_memory.db"
    chromadb_root = root / "chromadb"
    if database.exists() and database.is_symlink():
        raise SandboxError("shared schema-memory SQLite file must not be a symlink")
    database.touch(exist_ok=True)
    database.chmod(0o600)
    if chromadb_root.exists() and chromadb_root.is_symlink():
        raise SandboxError("shared schema-memory Chroma root must not be a symlink")
    chromadb_root.mkdir(exist_ok=True, mode=0o700)
    chromadb_root.chmod(0o700)
    verify_shared_schema_memory(root)


def verify_shared_schema_memory(root: Path) -> dict[str, object]:
    """Fail closed unless shared records are logically isolated by session."""

    if root.is_symlink() or not root.is_dir():
        raise SandboxError("shared schema-memory root is missing or unsafe")
    database = root / "smolagents_memory.db"
    chromadb_root = root / "chromadb"
    for path, expected_type, mode in (
        (root, "directory", 0o700),
        (database, "file", 0o600),
        (chromadb_root, "directory", 0o700),
    ):
        if (
            path.is_symlink()
            or not path.exists()
            or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o777 != mode
            or (expected_type == "directory" and not path.is_dir())
            or (expected_type == "file" and not path.is_file())
        ):
            raise SandboxError("shared schema-memory storage is unsafe")

    sqlite_records = _verify_shared_schema_sqlite(database)
    chroma_records = _verify_shared_schema_chroma(chromadb_root)
    return {
        "root": str(root.resolve()),
        "sqlite_records": sqlite_records,
        "chroma_records": chroma_records,
        "verification_status": "logically_isolated_shared_memory",
    }


def _verify_shared_schema_sqlite(database: Path) -> int:
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if tables - {"agent_memory", "strategic_memory"}:
                raise SandboxError("shared schema-memory has unexpected SQLite table")
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_memory'"
            ).fetchone()
            strategic_memory = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'strategic_memory'"
            ).fetchone()
            strategic_records = (
                conn.execute("SELECT session_id FROM strategic_memory").fetchall()
                if strategic_memory is not None
                else []
            )
            if exists is None:
                records = []
            else:
                records = conn.execute(
                    "SELECT session_id, data FROM agent_memory"
                ).fetchall()
    except sqlite3.Error as exc:
        raise SandboxError("shared schema-memory SQLite validation failed") from exc
    validated_records: list[tuple[str, str | None]] = []
    for session_id, raw_data in records:
        try:
            payload = json.loads(raw_data)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SandboxError("shared schema-memory SQLite record is invalid") from exc
        if not isinstance(payload, dict):
            raise SandboxError("shared schema-memory SQLite record is invalid")
        has_cache_kind = "cache_kind" in payload
        cache_kind = payload.get("cache_kind")
        if (
            not isinstance(session_id, str)
            or not session_id.strip()
            or (
                has_cache_kind
                and (not isinstance(cache_kind, str) or not cache_kind.strip())
            )
        ):
            raise SandboxError("shared schema-memory SQLite record is invalid")
        validated_records.append((session_id, cache_kind))
    schema_sessions = {
        session_id
        for session_id, cache_kind in validated_records
        if cache_kind in _SCHEMA_CACHE_KINDS
    }
    for (session_id,) in strategic_records:
        if not isinstance(session_id, str) or not session_id.strip():
            raise SandboxError("shared schema-memory strategic record is invalid")
        if session_id in schema_sessions:
            raise SandboxError("shared schema-memory contains non-schema cache kind")
    if any(
        cache_kind not in _SCHEMA_CACHE_KINDS and session_id in schema_sessions
        for session_id, cache_kind in validated_records
    ):
        raise SandboxError("shared schema-memory contains non-schema cache kind")
    return sum(
        cache_kind in _SCHEMA_CACHE_KINDS
        for _session_id, cache_kind in validated_records
    )


def _verify_shared_schema_chroma(chromadb_root: Path) -> int:
    if not any(chromadb_root.iterdir()):
        return 0
    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(chromadb_root))
        collections = client.list_collections()
        records: list[tuple[str, str | None]] = []
        for collection in collections:
            result = collection.get(include=["metadatas"])
            for metadata in result.get("metadatas", []):
                if not isinstance(metadata, dict):
                    raise SandboxError("shared schema-memory Chroma record is invalid")
                has_cache_kind = "cache_kind" in metadata
                cache_kind = metadata.get("cache_kind")
                session_id = metadata.get("session_id")
                if (
                    not isinstance(session_id, str)
                    or not session_id.strip()
                    or (
                        has_cache_kind
                        and (not isinstance(cache_kind, str) or not cache_kind.strip())
                    )
                ):
                    raise SandboxError("shared schema-memory Chroma record is invalid")
                records.append((session_id, cache_kind))
        schema_sessions = {
            session_id
            for session_id, cache_kind in records
            if cache_kind in _SCHEMA_CACHE_KINDS
        }
        if any(
            cache_kind not in _SCHEMA_CACHE_KINDS and session_id in schema_sessions
            for session_id, cache_kind in records
        ):
            raise SandboxError("shared schema-memory contains non-schema cache kind")
        return sum(
            cache_kind in _SCHEMA_CACHE_KINDS
            for _session_id, cache_kind in records
        )
    except SandboxError:
        raise
    except Exception as exc:
        raise SandboxError("shared schema-memory Chroma validation failed") from exc


def _prepare_workspace_mountpoints(spec: BwrapSandboxSpec) -> None:
    """Prepare private mount targets without copying executable source files."""

    workspace = spec.case_root / "workspace"
    for path in (
        workspace / ".venv",
        workspace / "data",
        workspace / "logs",
        workspace / "memory",
        workspace / "plots",
        workspace / "sqlrag",
    ):
        path.mkdir(exist_ok=True, mode=0o700)
        path.chmod(0o700)
    lock_path = workspace / ".schema_write.lock"
    lock_path.touch(exist_ok=True)
    lock_path.chmod(0o600)
    if not spec.snapshot_root.exists():
        return
    _validate_snapshot_mount_layout(spec.snapshot_root)
    for source in _immutable_snapshot_entries(spec.snapshot_root):
        target = workspace / source.name
        if source.is_dir():
            target.mkdir(exist_ok=False, mode=0o700)
            target.chmod(0o700)
        else:
            target.touch(exist_ok=False)
            target.chmod(0o600)


def _immutable_snapshot_entries(snapshot_root: Path) -> tuple[Path, ...]:
    root = snapshot_root.resolve(strict=True)
    entries: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name in _WRITABLE_WORKSPACE_PATHS:
            continue
        if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
            raise SandboxError(f"source snapshot mount entry is unsafe: {entry.name}")
        entries.append(entry)
    return tuple(entries)


def verify_private_case_files(spec: BwrapSandboxSpec) -> None:
    """Reject runtime output that another host user could read or traverse."""

    root = spec.case_root
    if root.is_symlink() or not root.is_dir():
        raise SandboxError("private case root is missing or unsafe")
    for path in (root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())):
        if path.is_symlink():
            raise SandboxError(f"private case inventory rejects symlink: {path}")
        metadata = path.stat(follow_symlinks=False)
        if metadata.st_uid != os.getuid():
            raise SandboxError(f"private case inventory rejects owner: {path}")
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise SandboxError(f"private case inventory rejects special file: {path}")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o077:
            raise SandboxError(f"private case inventory rejects mode {mode:o}: {path}")


def empty_history_receipt(
    *,
    benchmark: str,
    repeat_ordinal: int,
    case_key: str,
    spec: BwrapSandboxSpec,
) -> dict[str, object]:
    if not benchmark or repeat_ordinal < 1 or not case_key:
        raise SandboxError("empty-history receipt identity is invalid")
    verify_private_case_files(spec)
    if spec.shared_schema_memory_root is None:
        raise SandboxError("shared schema-memory root is required")
    schema_memory = verify_shared_schema_memory(spec.shared_schema_memory_root)
    case_root_inventory = _verify_prerun_case_root_inventory(spec)
    workspace_inventory = _verify_prerun_workspace_inventory(spec)
    stores = (
        ("durable_state", spec.case_root / "data", "directory"),
        ("logs", spec.case_root / "logs", "directory"),
        ("plots", spec.case_root / "plots", "directory"),
        ("successful_sql", spec.case_root / "sqlrag", "directory"),
        ("schema_write_lock", spec.case_root / ".schema_write.lock", "file"),
        (
            "workflow_state",
            spec.case_root / "workspace" / "workflow_state.db",
            "absent",
        ),
        (
            "workflow_state_secrets",
            spec.case_root / "workspace" / "workflow_state.db.secrets.json",
            "absent",
        ),
        (
            "workflow_state_secrets_lock",
            spec.case_root / "workspace" / "workflow_state.db.secrets.json.lock",
            "absent",
        ),
    )
    inspected_stores = [
        _inspect_empty_store(kind=kind, path=path, expected_type=expected_type)
        for kind, path, expected_type in stores
    ]
    preexisting_history_items = sum(
        int(item["initial_items"]) for item in inspected_stores
    )
    if preexisting_history_items != 0:
        raise SandboxError("case-local history store is not empty")
    writable_overlays = (
        ("workspace", spec.case_root / "workspace"),
        ("durable_state", spec.case_root / "data"),
        ("logs", spec.case_root / "logs"),
        ("plots", spec.case_root / "plots"),
        ("successful_sql", spec.case_root / "sqlrag"),
        ("schema_write_lock", spec.case_root / ".schema_write.lock"),
        ("workflow_state", spec.case_root / "workspace" / "workflow_state.db"),
    )
    return {
        "schema_version": 1,
        "benchmark": benchmark,
        "repeat_ordinal": repeat_ordinal,
        "case_key": case_key,
        "state_namespace": f"{benchmark}:{repeat_ordinal}:{case_key}",
        "state_root": str(spec.case_root.resolve()),
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "verification_phase": "before_process_start",
        "verification_status": "verified_empty",
        "preexisting_history_items": preexisting_history_items,
        "stores": inspected_stores,
        "schema_memory": schema_memory,
        "case_root_inventory": case_root_inventory,
        "workspace_inventory": workspace_inventory,
        "writable_overlays": [
            {"kind": kind, "path": str(path.resolve())}
            for kind, path in writable_overlays
        ],
    }


def _verify_prerun_case_root_inventory(spec: BwrapSandboxSpec) -> dict[str, object]:
    expected_root_names = {
        "workspace",
        "data",
        "logs",
        "plots",
        "sqlrag",
        ".schema_write.lock",
    }
    actual_root_names = {entry.name for entry in spec.case_root.iterdir()}
    if actual_root_names != expected_root_names:
        raise SandboxError("pre-run case-root inventory does not match manifest")
    return {
        "path": str(spec.case_root.resolve()),
        "expected_entry_count": len(expected_root_names),
        "actual_entry_count": len(actual_root_names),
        "manifest_matches": True,
    }


def _verify_prerun_workspace_inventory(spec: BwrapSandboxSpec) -> dict[str, object]:
    workspace = spec.case_root / "workspace"
    expected_names = _WRITABLE_WORKSPACE_PATHS | {
        entry.name for entry in _immutable_snapshot_entries(spec.snapshot_root)
    }
    actual_names = {entry.name for entry in workspace.iterdir()}
    if actual_names != expected_names:
        raise SandboxError("pre-run workspace inventory does not match mount manifest")
    return {
        "path": str(workspace.resolve()),
        "expected_entry_count": len(expected_names),
        "actual_entry_count": len(actual_names),
        "manifest_matches": True,
    }


def _inspect_empty_store(
    *,
    kind: str,
    path: Path,
    expected_type: str,
) -> dict[str, object]:
    if path.is_symlink():
        raise SandboxError(f"empty-history store rejects symlink: {kind}")
    exists = path.exists()
    if expected_type == "absent":
        if exists:
            raise SandboxError(f"empty-history store must not exist: {kind}")
        return {
            "kind": kind,
            "path": str(path.resolve()),
            "expected_type": expected_type,
            "exists": False,
            "initial_items": 0,
        }
    if not exists:
        raise SandboxError(f"empty-history store is missing: {kind}")
    if expected_type == "directory":
        if not path.is_dir():
            raise SandboxError(f"empty-history store is not a directory: {kind}")
        initial_items = sum(1 for _item in path.rglob("*"))
        size_bytes = None
    elif expected_type == "file":
        if not path.is_file():
            raise SandboxError(f"empty-history store is not a regular file: {kind}")
        size_bytes = path.stat().st_size
        initial_items = int(size_bytes > 0)
    else:
        raise SandboxError(f"empty-history store type is invalid: {kind}")
    if initial_items:
        raise SandboxError(f"empty-history store is not empty: {kind}")
    return {
        "kind": kind,
        "path": str(path.resolve()),
        "expected_type": expected_type,
        "exists": True,
        "initial_items": initial_items,
        "size_bytes": size_bytes,
    }


def build_bwrap_command(spec: BwrapSandboxSpec) -> list[str]:
    """Build a mount allowlist; the caller owns process start and cleanup."""

    if spec.snapshot_root.exists():
        _validate_snapshot_mount_layout(spec.snapshot_root)
    venv_root = _safe_mount_path(spec.venv_root, label="venv", expected="directory")
    database_path = _safe_mount_path(
        spec.database_path,
        label="database",
        expected="file",
    )
    command = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--clearenv",
    ]
    for host_path in _SYSTEM_READ_ONLY_MOUNTS:
        if host_path.exists():
            command.extend(("--ro-bind", str(host_path), str(host_path)))
    for host_path in _SYSTEM_READ_ONLY_FILES:
        if host_path.exists():
            command.extend(("--ro-bind", str(host_path), str(host_path)))
    command.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/workspace",
            "--dir",
            "/runtime-code",
        )
    )
    workspace_root = (spec.case_root / "workspace").resolve()
    command.extend(("--bind", str(workspace_root), "/workspace"))
    if spec.snapshot_root.exists():
        snapshot_root = spec.snapshot_root.resolve(strict=True)
        command.extend(("--ro-bind", str(snapshot_root), "/runtime-code"))
        for source in _immutable_snapshot_entries(snapshot_root):
            command.extend(("--ro-bind", str(source), f"/workspace/{source.name}"))
    command.extend(("--ro-bind", str(venv_root), "/workspace/.venv"))
    # Console scripts in a Python venv carry an absolute shebang.  Mounting the
    # venv at its original location exposes dependencies only, not the checkout.
    command.extend(("--ro-bind", str(venv_root), str(venv_root)))
    command.extend(
        (
            "--ro-bind",
            str(database_path),
            f"/benchmark-input/{spec.database_id}.sqlite",
            "--ro-bind",
            str(spec.secret_dir.resolve()),
            "/run/text2sql-secrets",
        )
    )
    for source, destination in zip(
        (
            spec.case_root / "data",
            spec.case_root / "logs",
            spec.case_root / "plots",
        ),
        ("/workspace/data", "/workspace/logs", "/workspace/plots"),
        strict=True,
    ):
        command.extend(("--bind", str(source.resolve()), destination))
    command.extend(
        (
            "--bind",
            str(_shared_schema_memory_mount_path(spec)),
            "/workspace/memory",
            "--bind",
            str((spec.case_root / "sqlrag").resolve()),
            "/workspace/sqlrag",
            "--bind",
            str((spec.case_root / ".schema_write.lock").resolve()),
            "/workspace/.schema_write.lock",
        )
    )
    env = {
        "PYTHONPATH": "/runtime-code:/workspace",
        "PATH": "/workspace/.venv/bin:/usr/bin:/bin",
        "HOME": "/tmp/home",
        "XDG_CACHE_HOME": "/tmp/xdg-cache",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PORT": str(spec.port),
        "AG_UI_AUTH_TOKEN_MAP_FILE": "/run/text2sql-secrets/ag_ui_auth_token_map",
        "OPENAI_API_KEY_FILE": "/run/text2sql-secrets/openai_api_key",
        "OPENAI_API_KEY_DB_FILE": "/run/text2sql-secrets/openai_api_key_db",
        "HF_TOKEN_FILE": "/run/text2sql-secrets/hf_token",
    }
    env.update(spec.runtime_env)
    for name, value in sorted(env.items()):
        command.extend(("--setenv", name, value))
    command.extend(
        (
            "--chdir",
            "/workspace",
            "/bin/sh",
            "-c",
            'umask 077; exec "$@"',
            "sandbox-entrypoint",
            "/workspace/deploy/entrypoint-backend.sh",
            "api",
        )
    )
    return command


def _shared_schema_memory_mount_path(spec: BwrapSandboxSpec) -> Path:
    if spec.shared_schema_memory_root is None:
        raise SandboxError("shared schema-memory root is required")
    return _safe_mount_path(
        spec.shared_schema_memory_root,
        label="shared schema-memory",
        expected="directory",
    )


def _safe_mount_path(path: Path, *, label: str, expected: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise SandboxError(f"{label} path is missing: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SandboxError(f"{label} path contains a symlink: {path}")
    metadata = absolute.stat(follow_symlinks=False)
    if expected == "file" and not stat.S_ISREG(metadata.st_mode):
        raise SandboxError(f"{label} path must be a regular file: {path}")
    if expected == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise SandboxError(f"{label} path must be a directory: {path}")
    return absolute


def resolve_safe_regular_file(path: Path, *, label: str) -> Path:
    return _safe_mount_path(path, label=label, expected="file")


def resolve_safe_directory(path: Path, *, label: str) -> Path:
    return _safe_mount_path(path, label=label, expected="directory")


def _verify_database_input(spec: BwrapSandboxSpec) -> None:
    database_path = _safe_mount_path(
        spec.database_path,
        label="database",
        expected="file",
    )
    if (
        spec.expected_database_sha256 is not None
        and _sha256(database_path) != spec.expected_database_sha256
    ):
        raise SandboxError("database content does not match frozen manifest")


def ensure_bwrap_available() -> str:
    executable = shutil.which("bwrap")
    if not executable:
        raise SandboxError("canonical benchmark mode requires bwrap")
    return executable


def validate_canonical_workers(workers: int) -> None:
    if workers != 1:
        raise SandboxError("canonical bwrap benchmark mode requires workers=1")


def validate_execution_mode(mode: str) -> str:
    if mode in {"bwrap", "remote"}:
        return "diagnostic_noncanonical"
    raise SandboxError("unknown benchmark execution mode")


def canonical_case_order(case_keys: list[str], *, seed: int) -> list[str]:
    if len(case_keys) != len(set(case_keys)):
        raise SandboxError("case keys must be unique before shuffling")
    result = sorted(case_keys)
    random.Random(seed).shuffle(result)
    return result


def validate_resume_integrity(
    manifest: Mapping[str, object],
    *,
    source_snapshot_digest: str,
    state_root: Path,
) -> None:
    if manifest.get("source_snapshot_digest") != source_snapshot_digest:
        raise SandboxError("resume source snapshot does not match manifest")
    if manifest.get("state_root") != str(state_root.resolve()):
        raise SandboxError("resume state_root does not match manifest")


class _StartedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...


class SandboxCaseRunner:
    """Run one case only after the sandbox server reaches health."""

    def __init__(
        self,
        *,
        start: Callable[[list[str]], _StartedProcess] | None = None,
        wait_for_health: Callable[[int], bool] | None = None,
        stop: Callable[[_StartedProcess], None] | None = None,
        verify_reaped: Callable[[_StartedProcess], bool] | None = None,
    ) -> None:
        self._start = start or _start_process
        self._wait_for_health = wait_for_health or _wait_for_health
        self._stop = stop or _stop_process
        self._verify_reaped = verify_reaped or _verify_reaped

    def run(
        self,
        spec: BwrapSandboxSpec,
        start_case: Callable[[], _T],
        *,
        expected_snapshot: SourceSnapshot,
        before_start: Callable[[], object] | None = None,
    ) -> _T:
        ensure_bwrap_available()
        if spec.snapshot_root.resolve() != expected_snapshot.root.resolve():
            raise SandboxError("sandbox spec does not use the frozen source snapshot")
        validate_secret_dir(spec.secret_dir)
        prepare_case_overlays(spec)
        if before_start is not None:
            before_start()
        verify_source_snapshot(expected_snapshot)
        _verify_database_input(spec)
        command = build_bwrap_command(spec)
        process = self._start(command)
        failure: BaseException | None = None
        result: _T | None = None
        try:
            if not self._wait_for_health(spec.port):
                raise SandboxError("sandbox API did not reach health")
            result = start_case()
        except BaseException as exc:
            failure = exc
        finally:
            self._stop(process)
            if not self._verify_reaped(process):
                raise SandboxError("sandbox process did not reap") from failure
            verify_private_case_files(spec)
            verify_source_snapshot(expected_snapshot)
            _verify_database_input(spec)
        if failure is not None:
            raise failure
        return result  # type: ignore[return-value]


def _start_process(command: list[str]) -> _StartedProcess:
    return subprocess.Popen(command, start_new_session=True)


def _wait_for_health(port: int) -> bool:
    deadline = time.monotonic() + 20.0
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(0.2)
    return False


def _stop_process(process: _StartedProcess) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _verify_reaped(process: _StartedProcess) -> bool:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.05)
    return process.poll() is not None
