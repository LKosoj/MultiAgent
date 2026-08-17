"""Frozen source-snapshot creation and manifest validation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import stat
from typing import Iterable, Mapping


class SandboxError(RuntimeError):
    """Raised when canonical benchmark isolation cannot be proven."""


CANONICAL_RUNTIME_SOURCE_PATHS = (
    Path("backend"),
    Path("custom_tools/sql_tools.py"),
    Path("custom_tools/text_to_sql"),
    Path("custom_tools/text_to_sql/eval/official_evaluator_attempt.py"),
    Path("custom_tools/text_to_sql/eval/official_evaluator_bridge.py"),
    Path("custom_tools/text_to_sql/eval/official_evaluator_contracts.py"),
    Path("custom_tools/text_to_sql/eval/official_evaluator_worker.py"),
    Path("custom_tools/storybook/__init__.py"),
    Path("custom_tools/storybook/project_paths.py"),
    Path("db_plugins"),
    Path("memory"),
    Path("telemetry"),
    Path("workflow"),
    Path("deploy/entrypoint-backend.sh"),
    Path("adaptive_planning.py"),
    Path("agent_command.py"),
    Path("agent_factory.py"),
    Path("agent_streamlit_api.py"),
    Path("agent_system.py"),
    Path("scripts/text2sql_benchmark_reporting.py"),
    Path("scripts/summarize_text2sql_public_benchmark.py"),
    Path("configuration_api.py"),
    Path("html_utils.py"),
    Path("logging_setup.py"),
    Path("retry_openai_model.py"),
    Path("tool_manager.py"),
    Path("tool_runtime_context.py"),
    Path("unified_logging.py"),
    Path("utils.py"),
    Path("workflow_redaction.py"),
    Path("workflow_pipelines/text_to_sql_pipeline.yaml"),
    Path("config/streamlit_config.yaml"),
    Path("config/pii/categories.yaml"),
    Path("config/text_to_sql/adaptive.yaml"),
    Path("config/text_to_sql/column_aliases.yaml"),
    Path("config/text_to_sql/joins.yaml"),
    Path("config/text_to_sql/llm_models.yaml"),
    Path("config/text_to_sql/main_table_scoring.yaml"),
    Path("config/text_to_sql/nlu_morphemes.yaml"),
    Path("config/text_to_sql/public_benchmark_release_policy.json"),
    Path("config/text_to_sql/safety.yaml"),
    Path("config/text_to_sql/significance.yaml"),
    Path("config/text_to_sql/similarity_thresholds.yaml"),
    Path("config/text_to_sql/state_schema.yaml"),
    Path("config/text_to_sql/type_categories.yaml"),
    Path("agent_profiles/schema_research_agent.yaml"),
    Path("agent_profiles/sql_solver_agent.yaml"),
    Path("tool_definitions/finalize_text_to_sql_run.yaml"),
)
_SOURCE_DIRECTORY_NAMES_TO_EXCLUDE = frozenset({"__pycache__", ".pytest_cache"})
_SOURCE_FILE_SUFFIXES_TO_EXCLUDE = frozenset({".pyc", ".pyo"})
_CANONICAL_SOURCE_SUBTREES_TO_EXCLUDE = (Path("custom_tools/text_to_sql/eval"),)
_RUNTIME_FILES_TO_OMIT = frozenset(
    {
        Path("memory/smolagents_memory.db"),
    }
)
_RUNTIME_DIRECTORIES_TO_OMIT = frozenset(
    {
        Path("memory/chromadb"),
    }
)


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    relative_path: str
    size_bytes: int
    sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    root: Path
    digest: str
    files: tuple[SnapshotFile, ...]
    tree_paths: tuple[str, ...]


def create_source_snapshot(
    source_root: Path,
    destination: Path,
    *,
    allowed_paths: Iterable[Path] = CANONICAL_RUNTIME_SOURCE_PATHS,
) -> SourceSnapshot:
    """Copy only executable/configuration inputs and hash the resulting tree."""

    source_root = source_root.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists():
        raise SandboxError(f"source snapshot destination already exists: {destination}")
    if destination == source_root or source_root in destination.parents:
        raise SandboxError("source snapshot destination must be outside source root")
    destination.mkdir(parents=True)
    try:
        normalized_paths = _normalize_source_allowlist(allowed_paths)
        for relative_path in normalized_paths:
            entry = source_root / relative_path
            if entry.is_symlink():
                raise SandboxError(f"source snapshot rejects symlink: {relative_path}")
            if entry.is_dir():
                target = destination / relative_path
                target.mkdir(parents=True, exist_ok=True)
                _copy_source_directory(source_root, entry, target)
            elif entry.is_file():
                _reject_unsafe_source_path(relative_path)
                _copy_file(entry, destination / relative_path)
            else:
                raise SandboxError(f"source snapshot input is missing: {relative_path}")
        _create_sandbox_mountpoints(destination)
        _freeze_source_snapshot(destination)
        files = _snapshot_files(destination)
        return SourceSnapshot(
            destination,
            _snapshot_digest(files),
            files,
            _snapshot_tree_paths(destination),
        )
    except Exception:
        # The partially built directory is intentionally retained as failed-run
        # evidence. A caller must choose a new destination for another attempt.
        raise


def _normalize_source_allowlist(allowed_paths: Iterable[Path]) -> tuple[Path, ...]:
    normalized: set[Path] = set()
    for raw_path in allowed_paths:
        path = Path(raw_path)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise SandboxError(f"source snapshot allowlist path is unsafe: {path}")
        normalized.add(path)
    if not normalized:
        raise SandboxError("source snapshot allowlist is empty")
    return tuple(sorted(normalized, key=lambda item: item.as_posix()))


def _copy_source_directory(source_root: Path, source: Path, destination: Path) -> None:
    for entry in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        relative_path = entry.relative_to(source_root)
        if entry.is_symlink():
            raise SandboxError(f"source snapshot rejects symlink: {relative_path}")
        if _is_excluded_source_path(relative_path):
            continue
        if _is_known_runtime_path(relative_path):
            continue
        _reject_unsafe_source_path(relative_path)
        target = destination / relative_path.relative_to(
            source.relative_to(source_root)
        )
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif entry.is_file():
            _copy_file(entry, target)
        else:
            raise SandboxError(
                f"source snapshot rejects non-regular path: {relative_path}"
            )


def _is_excluded_source_path(relative_path: Path) -> bool:
    if any(part in _SOURCE_DIRECTORY_NAMES_TO_EXCLUDE for part in relative_path.parts):
        return True
    if relative_path.suffix.lower() in _SOURCE_FILE_SUFFIXES_TO_EXCLUDE:
        return True
    return any(
        relative_path == excluded or excluded in relative_path.parents
        for excluded in _CANONICAL_SOURCE_SUBTREES_TO_EXCLUDE
    )


def _is_known_runtime_path(relative_path: Path) -> bool:
    if relative_path in _RUNTIME_FILES_TO_OMIT:
        return True
    return any(
        relative_path == item or item in relative_path.parents
        for item in _RUNTIME_DIRECTORIES_TO_OMIT
    )


def _reject_unsafe_source_path(relative_path: Path) -> None:
    lowered = [part.lower() for part in relative_path.parts]
    name = relative_path.name.lower()
    if name.startswith(".env") or any(
        part in {"gold", "evaluation", "evaluator"} for part in lowered
    ):
        raise SandboxError(
            f"source snapshot rejects secret or evaluator input: {relative_path}"
        )
    if name.startswith("gold") or name.endswith((".db", ".sqlite", ".sqlite3")):
        raise SandboxError(
            f"source snapshot rejects runtime or gold state: {relative_path}"
        )


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


def _create_sandbox_mountpoints(destination: Path) -> None:
    for path in (
        destination / ".venv",
        destination / "data",
        destination / "logs",
        destination / "plots",
        destination / "memory" / "chromadb",
        destination / "sqlrag",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (destination / "memory" / "smolagents_memory.db").touch(exist_ok=True)
    (destination / ".schema_write.lock").touch(exist_ok=True)


def _freeze_source_snapshot(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise SandboxError(
                f"source snapshot rejects symlink: {path.relative_to(root)}"
            )
        if path.is_dir():
            path.chmod(0o555)
        elif path.is_file():
            executable = bool(path.stat().st_mode & 0o111)
            path.chmod(0o555 if executable else 0o444)
        else:
            raise SandboxError(
                f"source snapshot rejects non-regular path: {path.relative_to(root)}"
            )
    root.chmod(0o555)


def _snapshot_files(root: Path) -> tuple[SnapshotFile, ...]:
    files: list[SnapshotFile] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise SandboxError(
                f"source snapshot has unsafe symlink: {path.relative_to(root)}"
            )
        if not path.is_file():
            continue
        if _is_known_runtime_path(path.relative_to(root)):
            continue
        files.append(
            SnapshotFile(
                relative_path=path.relative_to(root).as_posix(),
                size_bytes=path.stat().st_size,
                sha256=_sha256(path),
                mode=stat.S_IMODE(path.stat().st_mode),
            )
        )
    if not files:
        raise SandboxError("source snapshot has no allowed files")
    return tuple(files)


def _snapshot_digest(files: tuple[SnapshotFile, ...]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item.mode).encode("ascii"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _snapshot_tree_paths(root: Path) -> tuple[str, ...]:
    entries: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise SandboxError(
                f"source snapshot has unsafe symlink: {path.relative_to(root)}"
            )
        kind = "directory" if path.is_dir() else "file" if path.is_file() else None
        if kind is None:
            raise SandboxError(
                f"source snapshot has unsafe path type: {path.relative_to(root)}"
            )
        entries.append(f"{kind}:{path.relative_to(root).as_posix()}")
    return tuple(entries)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_snapshot_mount_layout(root: Path) -> None:
    if root.is_symlink():
        raise SandboxError("source snapshot root must not be a symlink")
    root = root.resolve(strict=True)
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SandboxError(
                f"source snapshot has unsafe symlink: {path.relative_to(root)}"
            )


def verify_source_snapshot(expected: SourceSnapshot) -> None:
    """Re-hash a frozen snapshot and reject any added, removed, or changed file."""

    if expected.root.is_symlink() or not expected.root.is_dir():
        raise SandboxError("source snapshot is missing or unsafe")
    _validate_snapshot_mount_layout(expected.root)
    _validate_frozen_snapshot_permissions(expected.root)
    actual_files = _snapshot_files(expected.root)
    actual_digest = _snapshot_digest(actual_files)
    if (
        actual_files != expected.files
        or actual_digest != expected.digest
        or _snapshot_tree_paths(expected.root) != expected.tree_paths
    ):
        raise SandboxError("source snapshot content does not match frozen manifest")


def _validate_frozen_snapshot_permissions(root: Path) -> None:
    for path in (root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())):
        mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        if path.is_dir():
            expected_mode = 0o555
        elif path.is_file():
            expected_mode = 0o555 if mode & 0o111 else 0o444
        else:
            raise SandboxError(f"source snapshot has unsafe path type: {path}")
        if mode != expected_mode:
            raise SandboxError(
                f"source snapshot permissions do not match frozen manifest "
                f"({mode:o}): {path}"
            )


def source_snapshot_manifest(snapshot: SourceSnapshot) -> dict[str, object]:
    """Return the complete portable inventory used to verify a frozen snapshot."""

    verify_source_snapshot(snapshot)
    return {
        "schema_version": 1,
        "record_kind": "text2sql_source_snapshot_manifest",
        "source_snapshot_digest": snapshot.digest,
        "files": [
            {
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "mode": item.mode,
            }
            for item in snapshot.files
        ],
        "tree_paths": list(snapshot.tree_paths),
    }


def source_snapshot_from_manifest(
    root: Path,
    payload: Mapping[str, object],
) -> SourceSnapshot:
    digest, files, tree_paths = _parse_source_snapshot_manifest(payload)
    snapshot = SourceSnapshot(
        root=Path(root),
        digest=digest,
        files=files,
        tree_paths=tree_paths,
    )
    verify_source_snapshot(snapshot)
    return snapshot


def _parse_source_snapshot_manifest(
    payload: Mapping[str, object],
) -> tuple[str, tuple[SnapshotFile, ...], tuple[str, ...]]:
    """Parse only the exact manifest emitted by ``source_snapshot_manifest``."""

    expected_fields = {
        "schema_version",
        "record_kind",
        "source_snapshot_digest",
        "files",
        "tree_paths",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_fields:
        raise SandboxError("source snapshot manifest fields are not closed")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise SandboxError("source snapshot manifest schema version is invalid")
    if payload["record_kind"] != "text2sql_source_snapshot_manifest":
        raise SandboxError("source snapshot manifest kind is invalid")
    digest = payload["source_snapshot_digest"]
    if not _is_prefixed_sha256(digest):
        raise SandboxError("source snapshot manifest digest is invalid")
    raw_files = payload["files"]
    raw_tree_paths = payload["tree_paths"]
    if not isinstance(raw_files, list) or not isinstance(raw_tree_paths, list):
        raise SandboxError("source snapshot manifest inventory is invalid")

    files: list[SnapshotFile] = []
    previous_file_path: str | None = None
    for item in raw_files:
        if not isinstance(item, Mapping) or set(item) != {
            "relative_path",
            "size_bytes",
            "sha256",
            "mode",
        }:
            raise SandboxError("source snapshot file inventory is invalid")
        relative_path = item["relative_path"]
        size_bytes = item["size_bytes"]
        sha256 = item["sha256"]
        mode = item["mode"]
        if (
            not _is_safe_relative_snapshot_path(relative_path)
            or type(size_bytes) is not int
            or size_bytes < 0
            or not _is_lower_sha256(sha256)
            or type(mode) is not int
            or mode not in (0o444, 0o555)
        ):
            raise SandboxError("source snapshot file inventory is invalid")
        assert isinstance(relative_path, str)
        assert isinstance(sha256, str)
        if previous_file_path is not None and relative_path <= previous_file_path:
            raise SandboxError("source snapshot file inventory is not canonical")
        previous_file_path = relative_path
        files.append(SnapshotFile(relative_path, size_bytes, sha256, mode))
    if not files:
        raise SandboxError("source snapshot file inventory is invalid")

    tree_paths: list[str] = []
    tree_kinds: dict[str, str] = {}
    previous_tree_path: str | None = None
    seen_tree_paths: set[str] = set()
    for item in raw_tree_paths:
        if not isinstance(item, str) or ":" not in item:
            raise SandboxError("source snapshot tree inventory is invalid")
        kind, relative_path = item.split(":", 1)
        if kind not in {"file", "directory"} or not _is_safe_relative_snapshot_path(
            relative_path
        ):
            raise SandboxError("source snapshot tree inventory is invalid")
        if (
            relative_path in seen_tree_paths
            or previous_tree_path is not None
            and relative_path <= previous_tree_path
        ):
            raise SandboxError("source snapshot tree inventory is not canonical")
        seen_tree_paths.add(relative_path)
        previous_tree_path = relative_path
        tree_kinds[relative_path] = kind
        tree_paths.append(item)

    file_paths = {item.relative_path for item in files}
    if any(tree_kinds.get(path) != "file" for path in file_paths):
        raise SandboxError("source snapshot manifest inventory does not match tree")
    if any(
        kind == "file"
        and Path(path) not in _RUNTIME_FILES_TO_OMIT
        and path not in file_paths
        for path, kind in tree_kinds.items()
    ):
        raise SandboxError("source snapshot manifest tree has an unlisted file")
    for path in tree_kinds:
        for ancestor in Path(path).parents:
            if ancestor == Path("."):
                break
            if tree_kinds.get(ancestor.as_posix()) != "directory":
                raise SandboxError("source snapshot manifest tree is inconsistent")
    return digest, tuple(files), tuple(tree_paths)


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_prefixed_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and _is_lower_sha256(value.removeprefix("sha256:"))
    )


def _is_safe_relative_snapshot_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = Path(value)
    return (
        not path.is_absolute()
        and path.parts
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.as_posix() == value
    )
