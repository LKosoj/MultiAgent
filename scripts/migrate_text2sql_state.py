#!/usr/bin/env python3
"""Invoke and verify the existing owners of Text-to-SQL SQLite schemas."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Mapping

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

STORE_NAMES = ("event_store", "memory_db", "result_outbox")
STORE_OWNERS = {
    "event_store": "backend.fastapi_app.agui.store.EventStore",
    "memory_db": "memory.database.DatabaseHandler._init_db",
    "result_outbox": "workflow.result_outbox.WorkflowResultOutbox",
}
_MANIFEST_KEYS = {"manifest_version", "stores"}
_STORE_KEYS = {"head", "path", "owner"}


class StateSchemaError(RuntimeError):
    """The migration manifest or resulting durable schema is invalid."""


class FutureSchemaVersionError(StateSchemaError):
    """A durable store is newer than this application understands."""


def _load_manifest_document(path: Path) -> dict:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise StateSchemaError("state schema manifest is unreadable") from exc
    if (
        not isinstance(document, dict)
        or set(document) != _MANIFEST_KEYS
        or document.get("manifest_version") != 1
    ):
        raise StateSchemaError("unsupported state schema manifest version")
    stores = document.get("stores")
    if not isinstance(stores, dict) or set(stores) != set(STORE_NAMES):
        raise StateSchemaError("state schema manifest store set is invalid")
    for name in STORE_NAMES:
        store = stores[name]
        if not isinstance(store, dict) or set(store) != _STORE_KEYS:
            raise StateSchemaError(f"state schema manifest {name} fields are invalid")
        if store["owner"] != STORE_OWNERS[name]:
            raise StateSchemaError(f"state schema manifest {name} owner is invalid")
        raw_path = store["path"]
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or "\\" in raw_path
            or Path(raw_path).is_absolute()
            or any(part in {"", ".", ".."} for part in Path(raw_path).parts)
        ):
            raise StateSchemaError(f"state schema manifest {name} path is unsafe")
    return document


def load_state_schema_manifest(path: Path | str) -> dict[str, int]:
    document = _load_manifest_document(Path(path))
    heads: dict[str, int] = {}
    for name in STORE_NAMES:
        store = document["stores"].get(name)
        head = store.get("head") if isinstance(store, dict) else None
        if isinstance(head, bool) or not isinstance(head, int) or head < 1:
            raise StateSchemaError(f"invalid schema head for {name}")
        heads[name] = head
    return heads


def _owning_schema_heads() -> dict[str, int]:
    from backend.fastapi_app.agui.store import AGUI_EVENT_STORE_SCHEMA_VERSION
    from memory.database import MEMORY_DB_SCHEMA_VERSION
    from workflow.result_outbox import WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION

    return {
        "event_store": AGUI_EVENT_STORE_SCHEMA_VERSION,
        "memory_db": MEMORY_DB_SCHEMA_VERSION,
        "result_outbox": WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION,
    }


def _read_head(path: Path) -> int:
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=rw", uri=True)
    except sqlite3.Error as exc:
        raise StateSchemaError("durable store is unavailable") from exc
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def resolve_manifest_store_paths(
    manifest_path: Path | str,
    project_root: Path | str,
) -> dict[str, Path]:
    document = _load_manifest_document(Path(manifest_path))
    root = Path(project_root).resolve()
    paths = {}
    for name in STORE_NAMES:
        candidate = (root / document["stores"][name]["path"]).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise StateSchemaError(
                f"state schema manifest {name} path escapes project root"
            ) from exc
        paths[name] = candidate
    return paths


def _initialize_event_store(path: Path) -> None:
    from backend.fastapi_app.agui.store import EventStore
    from workflow.state_files import ensure_private_directory

    ensure_private_directory(path.parent, create=True, tighten_existing=False)
    store = EventStore(str(path))
    store.close()


def _initialize_memory_db(path: Path) -> None:
    from memory.database import DatabaseHandler

    path.parent.mkdir(parents=True, exist_ok=True)
    handler = object.__new__(DatabaseHandler)
    handler.db_path = str(path)
    handler.chroma_path = ""
    handler._lock = threading.Lock()
    handler._init_db()


def _initialize_result_outbox(path: Path) -> None:
    from workflow.result_outbox import WorkflowResultOutbox
    from workflow.state_files import ensure_private_directory

    ensure_private_directory(path.parent, create=True, tighten_existing=False)
    outbox = WorkflowResultOutbox(str(path))
    outbox.close()


_OWNING_INITIALIZERS = {
    "event_store": _initialize_event_store,
    "memory_db": _initialize_memory_db,
    "result_outbox": _initialize_result_outbox,
}


def migrate_text2sql_state(
    *,
    manifest_path: Path | str,
    project_root: Path | str,
    store_paths: Mapping[str, Path | str] | None = None,
    check_only: bool = False,
) -> dict:
    manifest_path = Path(manifest_path)
    project_root = Path(project_root)
    expected_heads = load_state_schema_manifest(manifest_path)
    owner_heads = _owning_schema_heads()
    if expected_heads != owner_heads:
        raise StateSchemaError("manifest heads do not match owning schema heads")

    paths = resolve_manifest_store_paths(manifest_path, project_root)
    if store_paths is not None:
        if set(store_paths) != set(STORE_NAMES):
            raise StateSchemaError("store path set is invalid")
        paths = {name: Path(store_paths[name]) for name in STORE_NAMES}

    for name in STORE_NAMES:
        path = paths[name]
        if not path.exists():
            if check_only:
                raise StateSchemaError(f"{name} durable store is missing")
            continue
        actual = _read_head(path)
        if actual > expected_heads[name]:
            raise FutureSchemaVersionError(
                f"{name} schema {actual} is newer than supported head "
                f"{expected_heads[name]}"
            )

    schema_heads = {}
    for name in STORE_NAMES:
        path = paths[name]
        if not check_only:
            _OWNING_INITIALIZERS[name](path)
        actual = _read_head(path)
        expected = expected_heads[name]
        if actual != expected:
            raise StateSchemaError(
                f"{name} schema head mismatch: expected {expected}, got {actual}"
            )
        schema_heads[name] = {"expected": expected, "actual": actual}
    return {"status": "current", "schema_heads": schema_heads}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PROJECT_ROOT / "config" / "text_to_sql" / "state_schema.yaml",
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--event-store", type=Path)
    parser.add_argument("--memory-db", type=Path)
    parser.add_argument("--result-outbox", type=Path)
    parser.add_argument("--check-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    overrides = {
        "event_store": arguments.event_store,
        "memory_db": arguments.memory_db,
        "result_outbox": arguments.result_outbox,
    }
    paths = None if all(value is None for value in overrides.values()) else overrides
    if paths is not None and any(value is None for value in paths.values()):
        print(json.dumps({"status": "failed", "reason_code": "PATH_SET_INCOMPLETE"}))
        return 2
    try:
        report = migrate_text2sql_state(
            manifest_path=arguments.manifest,
            project_root=arguments.project_root,
            store_paths=paths,
            check_only=arguments.check_only,
        )
    except FutureSchemaVersionError:
        print(json.dumps({"status": "failed", "reason_code": "SCHEMA_AHEAD"}))
        return 2
    except StateSchemaError:
        print(json.dumps({"status": "failed", "reason_code": "SCHEMA_INVALID"}))
        return 2
    except Exception:
        print(json.dumps({"status": "failed", "reason_code": "MIGRATION_FAILED"}))
        return 5
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
