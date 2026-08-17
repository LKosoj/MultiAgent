"""Authenticated public views materialized from release progress."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from typing import Sequence


class ReleaseMaterializationMixin:
    error_type: type[RuntimeError] = RuntimeError

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _read_regular_bytes(cls, path: Path, *, label: str) -> bytes:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            raise
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise cls.error_type(f"{label} is unsafe")
        return path.read_bytes()

    @classmethod
    def _write_snapshot_atomically(cls, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            cls._fsync_parent(path)
        finally:
            if descriptor != -1:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    def materialize_observations(
        self,
        path: Path,
        *,
        benchmark: str,
        repeat_ordinal: int,
    ) -> None:
        """Repair only an exact byte prefix of authenticated committed rows."""

        expected = self.observation_bytes(
            benchmark=benchmark, repeat_ordinal=repeat_ordinal
        )
        try:
            actual = self._read_regular_bytes(path, label="observations view")
        except FileNotFoundError:
            actual = None
        if actual is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise self.error_type(
                    "observations view changed during materialization"
                ) from exc
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(expected)
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_parent(path)
            return
        if not expected.startswith(actual):
            raise self.error_type(
                "observations view is not an exact committed byte prefix"
            )
        if actual == expected:
            return
        self._write_snapshot_atomically(path, expected)

    def materialize_snapshot(
        self,
        path: Path,
        *,
        committed_views: Sequence[bytes],
        label: str,
    ) -> None:
        """Publish the latest snapshot, accepting only an authenticated old view."""

        if not committed_views:
            raise self.error_type(f"{label} has no committed views")
        target = committed_views[-1]
        try:
            actual = self._read_regular_bytes(path, label=label)
        except FileNotFoundError:
            actual = None
        if actual == target:
            return
        if actual is not None and actual not in committed_views[:-1]:
            raise self.error_type(f"{label} is not an authenticated committed view")
        self._write_snapshot_atomically(path, target)

    def history_view_receipt_prefixes(
        self, *, benchmark: str, repeat_ordinal: int
    ) -> list[list[dict[str, object]]]:
        receipts = self.history_receipts(
            benchmark=benchmark, repeat_ordinal=repeat_ordinal
        )
        return [receipts[:count] for count in range(len(receipts) + 1)]
