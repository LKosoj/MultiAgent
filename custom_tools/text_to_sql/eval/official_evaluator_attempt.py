"""Crash-safe publication primitive for official evaluator attempts."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .sandbox import SandboxError


def _existing_sealed_bytes(path: Path, payload: bytes) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_mode & 0o777 == 0o444
        and path.read_bytes() == payload
    )


def publish_bytes_sealed(path: Path, payload: bytes) -> None:
    """Publish new 0444 bytes, or accept an identical sealed artifact."""
    if path.exists() or path.is_symlink():
        if _existing_sealed_bytes(path, payload):
            return
        raise SandboxError(f"sealed artifact differs: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if not _existing_sealed_bytes(path, payload):
                raise SandboxError(f"sealed artifact differs: {path.name}")
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
