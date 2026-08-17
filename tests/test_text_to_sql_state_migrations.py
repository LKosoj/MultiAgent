from __future__ import annotations

import inspect
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "config" / "text_to_sql" / "state_schema.yaml"


def _store_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "event_store": tmp_path / "agui_events.db",
        "memory_db": tmp_path / "memory.db",
        "result_outbox": tmp_path / "workflow_result_outbox.db",
    }


def _head(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def test_state_manifest_matches_all_owning_schema_heads() -> None:
    from backend.fastapi_app.agui.store import AGUI_EVENT_STORE_SCHEMA_VERSION
    from memory.database import MEMORY_DB_SCHEMA_VERSION
    from scripts.migrate_text2sql_state import load_state_schema_manifest
    from workflow.result_outbox import WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION

    manifest = load_state_schema_manifest(MANIFEST_PATH)

    assert manifest == {
        "event_store": AGUI_EVENT_STORE_SCHEMA_VERSION,
        "memory_db": MEMORY_DB_SCHEMA_VERSION,
        "result_outbox": WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION,
    }
    assert manifest == {"event_store": 10, "memory_db": 1, "result_outbox": 3}


def test_migration_runner_invokes_owner_initializers_and_verifies_exact_heads(
    tmp_path: Path,
) -> None:
    from scripts.migrate_text2sql_state import migrate_text2sql_state

    paths = _store_paths(tmp_path)
    report = migrate_text2sql_state(
        manifest_path=MANIFEST_PATH,
        project_root=PROJECT_ROOT,
        store_paths=paths,
    )

    assert report["status"] == "current"
    assert report["schema_heads"] == {
        key: {"expected": expected, "actual": expected}
        for key, expected in {
                "event_store": 10,
            "memory_db": 1,
            "result_outbox": 3,
        }.items()
    }
    assert {key: _head(path) for key, path in paths.items()} == {
        "event_store": 10,
        "memory_db": 1,
        "result_outbox": 3,
    }


def test_migration_runner_rejects_future_head_without_mutating_it(tmp_path: Path) -> None:
    from scripts.migrate_text2sql_state import (
        FutureSchemaVersionError,
        migrate_text2sql_state,
    )

    paths = _store_paths(tmp_path)
    connection = sqlite3.connect(paths["event_store"])
    connection.execute("PRAGMA user_version=11")
    connection.close()

    with pytest.raises(FutureSchemaVersionError, match="event_store"):
        migrate_text2sql_state(
            manifest_path=MANIFEST_PATH,
            project_root=PROJECT_ROOT,
            store_paths=paths,
        )

    assert _head(paths["event_store"]) == 11
    assert not paths["memory_db"].exists()
    assert not paths["result_outbox"].exists()


def test_migration_runner_contains_no_shadow_ddl_or_alembic_history() -> None:
    import scripts.migrate_text2sql_state as migration_module

    source = inspect.getsource(migration_module).upper()
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source
    assert "ALEMBIC" not in source


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update({"unexpected": True}),
        lambda doc: doc["stores"]["event_store"].update({"unexpected": True}),
        lambda doc: doc["stores"]["event_store"].update({"owner": "wrong.Owner"}),
        lambda doc: doc["stores"]["event_store"].update({"path": "/tmp/escape.db"}),
        lambda doc: doc["stores"]["event_store"].update({"path": "../escape.db"}),
    ],
)
def test_manifest_is_closed_and_rejects_unsafe_paths_and_owners(
    tmp_path: Path,
    mutate,
) -> None:
    from scripts.migrate_text2sql_state import StateSchemaError, load_state_schema_manifest

    document = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    mutate(document)
    candidate = tmp_path / "state-schema.yaml"
    candidate.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(StateSchemaError):
        load_state_schema_manifest(candidate)


def test_manifest_paths_resolve_inside_project_root(tmp_path: Path) -> None:
    from scripts.migrate_text2sql_state import resolve_manifest_store_paths

    paths = resolve_manifest_store_paths(MANIFEST_PATH, tmp_path)

    assert set(paths) == {"event_store", "memory_db", "result_outbox"}
    assert all(path.is_relative_to(tmp_path.resolve()) for path in paths.values())


def test_migration_runner_is_directly_executable(tmp_path: Path) -> None:
    paths = _store_paths(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "migrate_text2sql_state.py"),
            "--event-store",
            str(paths["event_store"]),
            "--memory-db",
            str(paths["memory_db"]),
            "--result-outbox",
            str(paths["result_outbox"]),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "current"


def test_disposable_schema_backup_restore_and_rollback(tmp_path: Path) -> None:
    from scripts.migrate_text2sql_state import migrate_text2sql_state

    paths = _store_paths(tmp_path / "live")
    backups = _store_paths(tmp_path / "backup")
    restored = _store_paths(tmp_path / "restored")
    migrate_text2sql_state(
        manifest_path=MANIFEST_PATH,
        project_root=PROJECT_ROOT,
        store_paths=paths,
    )

    for key, source_path in paths.items():
        backups[key].parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(source_path)
        destination = sqlite3.connect(backups[key])
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        restored[key].parent.mkdir(parents=True, exist_ok=True)
        backup = sqlite3.connect(backups[key])
        restore_target = sqlite3.connect(restored[key])
        try:
            backup.backup(restore_target)
        finally:
            restore_target.close()
            backup.close()

    check = migrate_text2sql_state(
        manifest_path=MANIFEST_PATH,
        project_root=PROJECT_ROOT,
        store_paths=restored,
        check_only=True,
    )
    assert check["status"] == "current"
    assert not list(tmp_path.rglob("*.lock"))
