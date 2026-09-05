from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.eval import sandbox as sandbox_module
from custom_tools.text_to_sql.eval.sandbox import (
    BwrapSandboxSpec,
    SandboxCaseRunner,
    SandboxError,
    build_bwrap_command,
    canonical_case_order,
    create_source_snapshot,
    empty_history_receipt,
    ensure_bwrap_available,
    overlay_paths,
    prepare_case_overlays,
    prepare_shared_schema_memory,
    verify_shared_schema_memory,
    validate_canonical_workers,
    validate_execution_mode,
    validate_resume_integrity,
    validate_secret_dir,
    verify_private_case_files,
)


def _prepared_shared_schema_memory(tmp_path: Path, name: str) -> Path:
    root = tmp_path / "state" / "schema-memory" / name
    prepare_shared_schema_memory(root)
    return root


def test_fixed_shuffle_and_explicitly_noncanonical_remote_mode() -> None:
    cases = ["bird:0", "bird:1", "bird:2", "bird:3"]

    assert canonical_case_order(cases, seed=7) == canonical_case_order(cases, seed=7)
    assert canonical_case_order(cases, seed=7) != canonical_case_order(cases, seed=8)
    assert validate_execution_mode("bwrap") == "diagnostic_noncanonical"
    assert validate_execution_mode("remote") == "diagnostic_noncanonical"


def test_secret_dir_is_private_closed_and_regular(tmp_path: Path) -> None:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(mode=0o700)
    secret_dir.chmod(0o700)
    for name in (
        "ag_ui_auth_token_map",
        "openai_api_key",
        "openai_api_key_db",
        "hf_token",
    ):
        path = secret_dir / name
        path.write_text("test\n", encoding="utf-8")
        path.chmod(0o600)

    validate_secret_dir(secret_dir)
    (secret_dir / "hf_token").chmod(0o644)
    with pytest.raises(SandboxError, match="secret"):
        validate_secret_dir(secret_dir)

    linked_dir = tmp_path / "linked-secrets"
    linked_dir.symlink_to(secret_dir, target_is_directory=True)
    with pytest.raises(SandboxError, match="symlink"):
        validate_secret_dir(linked_dir)


def test_runtime_environment_cannot_override_sandbox_controls(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="reserved"):
        BwrapSandboxSpec(
            snapshot_root=tmp_path / "snapshot",
            venv_root=tmp_path / "venv",
            case_root=tmp_path / "case",
            database_path=tmp_path / "input.sqlite",
            database_id="db",
            secret_dir=tmp_path / "secrets",
            port=18765,
            runtime_env={"PYTHONPATH": "/attacker"},
        )


def test_bwrap_command_has_only_explicit_inputs_and_never_exposes_secret_value(
    tmp_path: Path,
) -> None:
    case_root = tmp_path / "state" / "bird-000"
    database = tmp_path / "input" / "database.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"SQLite format 3\x00")
    (tmp_path / "venv").mkdir()
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    spec = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=case_root,
        database_path=database,
        database_id="database",
        secret_dir=secret_dir,
        port=18765,
        runtime_env={"TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED": "0"},
        shared_schema_memory_root=_prepared_shared_schema_memory(tmp_path, "database"),
    )

    command = build_bwrap_command(spec)

    assert command[:5] == [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--clearenv",
    ]
    assert ["--ro-bind", "/", "/"] not in [
        command[index : index + 3] for index in range(len(command) - 2)
    ]
    assert "super-secret" not in command
    assert "/workspace" in command
    assert "/benchmark-input/database.sqlite" in command
    assert "/run/text2sql-secrets" in command
    assert ["--bind", str(case_root / "plots"), "/workspace/plots"] in [
        command[index : index + 3] for index in range(len(command) - 2)
    ]
    assert [
        "--bind",
        str(spec.shared_schema_memory_root.resolve()),
        "/workspace/memory",
    ] in [command[index : index + 3] for index in range(len(command) - 2)]
    assert "PYTHONPATH" in command
    assert "/workspace" in command[command.index("PYTHONPATH") + 1 :]
    assert [
        "--ro-bind",
        str(spec.venv_root.resolve()),
        str(spec.venv_root.resolve()),
    ] in [command[index : index + 3] for index in range(len(command) - 2)]
    assert "--unshare-net" not in command
    assert "gold" not in " ".join(command).lower()


def test_bwrap_command_exposes_no_original_source_and_uses_read_only_database_mount(
    tmp_path: Path,
) -> None:
    original_source = tmp_path / "original-source"
    original_source.mkdir()
    database = tmp_path / "input" / "database.sqlite"
    database.parent.mkdir()
    database.write_bytes(b"SQLite format 3\x00")
    (tmp_path / "venv").mkdir()
    spec = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "case",
        database_path=database,
        database_id="database",
        secret_dir=tmp_path / "secrets",
        port=18765,
        runtime_env={},
        shared_schema_memory_root=_prepared_shared_schema_memory(tmp_path, "database"),
    )

    command = build_bwrap_command(spec)

    triples = [command[index : index + 3] for index in range(len(command) - 2)]
    assert [
        "--ro-bind",
        str(database.resolve()),
        "/benchmark-input/database.sqlite",
    ] in triples
    assert str(original_source.resolve()) not in command


def test_case_overlays_are_unique_for_every_case(tmp_path: Path) -> None:
    first = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "one",
        database_path=tmp_path / "one.sqlite",
        database_id="one",
        secret_dir=tmp_path / "secrets",
        port=18765,
        runtime_env={},
        shared_schema_memory_root=_prepared_shared_schema_memory(tmp_path, "db-history"),
    )
    second = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "two",
        database_path=tmp_path / "two.sqlite",
        database_id="two",
        secret_dir=tmp_path / "secrets",
        port=18766,
        runtime_env={},
    )

    assert set(overlay_paths(first)).isdisjoint(overlay_paths(second))


def test_same_database_cases_share_only_schema_memory_root(tmp_path: Path) -> None:
    shared = tmp_path / "state" / "schema-memory" / ("db-a-" + "a" * 16)
    first = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "case-one",
        database_path=tmp_path / "one.sqlite",
        database_id="db-a",
        secret_dir=tmp_path / "secrets",
        port=18765,
        runtime_env={},
        shared_schema_memory_root=shared,
    )
    second = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "case-two",
        database_path=tmp_path / "one.sqlite",
        database_id="db-a",
        secret_dir=tmp_path / "secrets",
        port=18766,
        runtime_env={},
        shared_schema_memory_root=shared,
    )
    other_database = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "case-three",
        database_path=tmp_path / "two.sqlite",
        database_id="db-b",
        secret_dir=tmp_path / "secrets",
        port=18767,
        runtime_env={},
        shared_schema_memory_root=(
            tmp_path / "state" / "schema-memory" / ("db-b-" + "b" * 16)
        ),
    )

    assert first.shared_schema_memory_root == second.shared_schema_memory_root
    assert first.shared_schema_memory_root != other_database.shared_schema_memory_root
    assert set(overlay_paths(first)).isdisjoint(overlay_paths(second))
    assert first.case_root / "sqlrag" not in overlay_paths(second)


def test_shared_schema_memory_rejects_non_schema_cache_kind(tmp_path: Path) -> None:
    root = tmp_path / "state" / "schema-memory" / ("db-a-" + "a" * 16)
    prepare_shared_schema_memory(root)
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            (
                ("run", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
                ("run", "agent", 2, '{"cache_kind":"agent_step"}'),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SandboxError, match="non-schema"):
        verify_shared_schema_memory(root)


def test_shared_schema_memory_allows_checkpoint_in_other_session(tmp_path: Path) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-shared-memory")
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            (
                ("schema-session", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
                ("r55-checkpoint", "agent", 1, '{"cache_kind":"checkpoint"}'),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    receipt = verify_shared_schema_memory(root)

    assert receipt["sqlite_records"] == 1
    assert receipt["verification_status"] == "logically_isolated_shared_memory"


def test_shared_schema_memory_allows_semantic_and_probe_facts(tmp_path: Path) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-semantic-facts")
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            (
                ("schema-session", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
                (
                    "schema-session",
                    "Schema-RAG-Agent",
                    2,
                    '{"cache_kind":"schema_semantic_fact"}',
                ),
                (
                    "schema-session",
                    "Schema-RAG-Agent",
                    3,
                    '{"cache_kind":"schema_probe_fact"}',
                ),
                (
                    "schema-session",
                    "Schema-RAG-Agent",
                    4,
                    '{"cache_kind":"successful_sql_example"}',
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    receipt = verify_shared_schema_memory(root)

    assert receipt["sqlite_records"] == 4


def test_schema_memory_copy_preserves_semantic_and_probe_facts(tmp_path: Path) -> None:
    from custom_tools.text_to_sql.eval.release_inputs import filter_schema_memory_copy

    root = _prepared_shared_schema_memory(tmp_path, "db-semantic-fact-copy")
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            (
                ("schema-session", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
                (
                    "schema-session",
                    "Schema-RAG-Agent",
                    2,
                    '{"cache_kind":"schema_semantic_fact"}',
                ),
                (
                    "schema-session",
                    "Schema-RAG-Agent",
                    3,
                    '{"cache_kind":"schema_probe_fact"}',
                ),
                (
                    "schema-session",
                    "Schema-RAG-Agent",
                    4,
                    '{"cache_kind":"successful_sql_example"}',
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    filter_schema_memory_copy(root.parent)

    with sqlite3.connect(database) as copied:
        assert copied.execute("SELECT data FROM agent_memory ORDER BY step").fetchall() == [
            ('{"cache_kind":"schema_table"}',),
            ('{"cache_kind":"schema_semantic_fact"}',),
            ('{"cache_kind":"schema_probe_fact"}',),
            ('{"cache_kind":"successful_sql_example"}',),
        ]


def test_shared_schema_memory_allows_untyped_workflow_checkpoint_in_other_session(
    tmp_path: Path,
) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-shared-untyped-memory")
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            (
                ("schema-session", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
                (
                    "workflow-session",
                    "workflow",
                    1,
                    '{"artifact_type":"workflow_checkpoint","workflow_checkpoint":true}',
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    receipt = verify_shared_schema_memory(root)

    assert receipt["sqlite_records"] == 1


def test_shared_schema_memory_rejects_checkpoint_in_schema_session(tmp_path: Path) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-schema-collision")
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            (
                ("schema-session", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
                ("schema-session", "agent", 2, '{"cache_kind":"checkpoint"}'),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SandboxError, match="non-schema"):
        verify_shared_schema_memory(root)


def test_shared_schema_memory_rejects_untyped_checkpoint_in_schema_session(
    tmp_path: Path,
) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-untyped-schema-collision")
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            (
                ("schema-session", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
                (
                    "schema-session",
                    "workflow",
                    2,
                    '{"artifact_type":"workflow_checkpoint","workflow_checkpoint":true}',
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SandboxError, match="non-schema"):
        verify_shared_schema_memory(root)


def test_shared_schema_memory_rejects_malformed_sqlite_session(tmp_path: Path) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-malformed-session")
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.execute(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            (None, "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SandboxError, match="invalid"):
        verify_shared_schema_memory(root)


@pytest.mark.parametrize(
    ("session_id", "cache_kind"),
    (("   ", "schema_table"), ("schema-session", "   "), ("schema-session", None)),
)
def test_shared_schema_memory_rejects_whitespace_sqlite_identity(
    tmp_path: Path,
    session_id: str,
    cache_kind: str | None,
) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-whitespace-sqlite")
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.execute(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            (session_id, "Schema-RAG-Agent", 1, json.dumps({"cache_kind": cache_kind})),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SandboxError, match="invalid"):
        verify_shared_schema_memory(root)


def test_shared_schema_memory_chroma_allows_other_session_checkpoint(
    monkeypatch, tmp_path: Path
) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-chroma-shared-memory")
    (root / "chromadb" / "chroma.sqlite3").touch()

    class Collection:
        def get(self, **_kwargs):
            return {
                "metadatas": [
                    {"session_id": "schema-session", "cache_kind": "schema_table"},
                    {"session_id": "r55-checkpoint", "cache_kind": "checkpoint"},
                ]
            }

    class Client:
        def list_collections(self):
            return [Collection()]

    monkeypatch.setitem(
        sys.modules,
        "chromadb",
        SimpleNamespace(PersistentClient=lambda **_kwargs: Client()),
    )

    receipt = verify_shared_schema_memory(root)

    assert receipt["chroma_records"] == 1


def test_shared_schema_memory_chroma_allows_untyped_checkpoint_in_other_session(
    monkeypatch, tmp_path: Path
) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-chroma-untyped-shared-memory")
    (root / "chromadb" / "chroma.sqlite3").touch()

    class Collection:
        def get(self, **_kwargs):
            return {
                "metadatas": [
                    {"session_id": "schema-session", "cache_kind": "schema_table"},
                    {
                        "session_id": "workflow-session",
                        "artifact_type": "workflow_checkpoint",
                    },
                ]
            }

    class Client:
        def list_collections(self):
            return [Collection()]

    monkeypatch.setitem(
        sys.modules,
        "chromadb",
        SimpleNamespace(PersistentClient=lambda **_kwargs: Client()),
    )

    receipt = verify_shared_schema_memory(root)

    assert receipt["chroma_records"] == 1


def test_shared_schema_memory_chroma_rejects_schema_session_checkpoint(
    monkeypatch, tmp_path: Path
) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-chroma-session-collision")
    (root / "chromadb" / "chroma.sqlite3").touch()

    class Collection:
        def get(self, **_kwargs):
            return {
                "metadatas": [
                    {"session_id": "schema-session", "cache_kind": "schema_table"},
                    {"session_id": "schema-session", "cache_kind": "checkpoint"},
                ]
            }

    class Client:
        def list_collections(self):
            return [Collection()]

    monkeypatch.setitem(
        sys.modules,
        "chromadb",
        SimpleNamespace(PersistentClient=lambda **_kwargs: Client()),
    )

    with pytest.raises(SandboxError, match="non-schema"):
        verify_shared_schema_memory(root)


def test_shared_schema_memory_chroma_rejects_untyped_checkpoint_in_schema_session(
    monkeypatch, tmp_path: Path
) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-chroma-untyped-collision")
    (root / "chromadb" / "chroma.sqlite3").touch()

    class Collection:
        def get(self, **_kwargs):
            return {
                "metadatas": [
                    {"session_id": "schema-session", "cache_kind": "schema_table"},
                    {
                        "session_id": "schema-session",
                        "artifact_type": "workflow_checkpoint",
                    },
                ]
            }

    class Client:
        def list_collections(self):
            return [Collection()]

    monkeypatch.setitem(
        sys.modules,
        "chromadb",
        SimpleNamespace(PersistentClient=lambda **_kwargs: Client()),
    )

    with pytest.raises(SandboxError, match="non-schema"):
        verify_shared_schema_memory(root)


@pytest.mark.parametrize(
    "metadata",
    (
        {"session_id": "   ", "cache_kind": "schema_table"},
        {"session_id": "schema-session", "cache_kind": "   "},
        {"session_id": "schema-session", "cache_kind": None},
    ),
)
def test_shared_schema_memory_chroma_rejects_whitespace_identity(
    monkeypatch,
    tmp_path: Path,
    metadata: dict[str, str],
) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-whitespace-chroma")
    (root / "chromadb" / "chroma.sqlite3").touch()

    class Collection:
        def get(self, **_kwargs):
            return {"metadatas": [metadata]}

    class Client:
        def list_collections(self):
            return [Collection()]

    monkeypatch.setitem(
        sys.modules,
        "chromadb",
        SimpleNamespace(PersistentClient=lambda **_kwargs: Client()),
    )

    with pytest.raises(SandboxError, match="invalid"):
        verify_shared_schema_memory(root)


def test_shared_schema_memory_allows_strategic_memory_in_other_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state" / "schema-memory" / ("db-a-" + "a" * 16)
    prepare_shared_schema_memory(root)
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.execute(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            ("schema-session", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
        )
        conn.execute(
            "CREATE TABLE strategic_memory (session_id TEXT, type TEXT, content TEXT)"
        )
        conn.execute(
            "INSERT INTO strategic_memory (session_id, type, content) VALUES (?, ?, ?)",
            ("other-session", "goal", "ordinary workflow record"),
        )
        conn.commit()
    finally:
        conn.close()

    assert verify_shared_schema_memory(root)["sqlite_records"] == 1


def test_shared_schema_memory_rejects_strategic_memory_in_schema_session(
    tmp_path: Path,
) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-strategic-collision")
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.execute(
            "INSERT INTO agent_memory (session_id, agent_name, step, data) "
            "VALUES (?, ?, ?, ?)",
            ("schema-session", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
        )
        conn.execute(
            "CREATE TABLE strategic_memory (session_id TEXT, type TEXT, content TEXT)"
        )
        conn.execute(
            "INSERT INTO strategic_memory (session_id, type, content) VALUES (?, ?, ?)",
            ("schema-session", "goal", "workflow record"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SandboxError, match="non-schema"):
        verify_shared_schema_memory(root)


@pytest.mark.parametrize("session_id", (None, "", "   "))
def test_shared_schema_memory_rejects_malformed_strategic_memory_session(
    tmp_path: Path,
    session_id: str | None,
) -> None:
    root = _prepared_shared_schema_memory(tmp_path, "db-strategic-invalid")
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE strategic_memory (session_id TEXT, type TEXT, content TEXT)"
        )
        conn.execute(
            "INSERT INTO strategic_memory (session_id, type, content) VALUES (?, ?, ?)",
            (session_id, "goal", "workflow record"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SandboxError, match="invalid"):
        verify_shared_schema_memory(root)


def test_shared_schema_memory_rejects_unknown_sqlite_table(tmp_path: Path) -> None:
    root = tmp_path / "state" / "schema-memory" / ("db-a-" + "a" * 16)
    prepare_shared_schema_memory(root)
    database = root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute("CREATE TABLE replay_history (run_id TEXT, payload TEXT)")
        conn.execute(
            "INSERT INTO replay_history (run_id, payload) VALUES (?, ?)",
            ("run", "must not be shared"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SandboxError, match="unexpected SQLite table"):
        verify_shared_schema_memory(root)


def test_empty_history_receipt_lists_every_writable_store_at_zero(
    tmp_path: Path,
) -> None:
    (tmp_path / "snapshot").mkdir()
    spec = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "case",
        database_path=tmp_path / "input.sqlite",
        database_id="db",
        secret_dir=tmp_path / "secrets",
        port=18765,
        runtime_env={},
        shared_schema_memory_root=_prepared_shared_schema_memory(tmp_path, "db"),
    )

    prepare_case_overlays(spec)
    receipt = empty_history_receipt(
        benchmark="bird",
        repeat_ordinal=1,
        case_key="bird:0",
        spec=spec,
    )

    assert receipt["state_namespace"] == "bird:1:bird:0"
    assert receipt["preexisting_history_items"] == 0
    assert {item["kind"] for item in receipt["stores"]} == {
        "durable_state",
        "logs",
        "plots",
        "successful_sql",
        "schema_write_lock",
        "workflow_state",
        "workflow_state_secrets",
        "workflow_state_secrets_lock",
    }
    assert all(item["initial_items"] == 0 for item in receipt["stores"])
    assert receipt["verification_phase"] == "before_process_start"
    assert {item["kind"] for item in receipt["writable_overlays"]} == {
        "workspace",
        "durable_state",
        "logs",
        "successful_sql",
        "plots",
        "schema_write_lock",
        "workflow_state",
    }


def test_empty_history_receipt_fails_closed_for_existing_item(tmp_path: Path) -> None:
    (tmp_path / "snapshot").mkdir()
    spec = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "case",
        database_path=tmp_path / "input.sqlite",
        database_id="db",
        secret_dir=tmp_path / "secrets",
        port=18765,
        runtime_env={},
        shared_schema_memory_root=_prepared_shared_schema_memory(tmp_path, "db-runner"),
    )
    prepare_case_overlays(spec)
    (spec.case_root / "logs" / "old.jsonl").write_text("history\n", encoding="utf-8")
    (spec.case_root / "logs" / "old.jsonl").chmod(0o600)

    with pytest.raises(SandboxError, match="not empty"):
        empty_history_receipt(
            benchmark="bird",
            repeat_ordinal=1,
            case_key="bird:0",
            spec=spec,
        )


def test_case_overlays_are_private_and_reject_reuse(tmp_path: Path) -> None:
    spec = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "case",
        database_path=tmp_path / "input.sqlite",
        database_id="db",
        secret_dir=tmp_path / "secrets",
        port=18765,
        runtime_env={},
    )

    prepare_case_overlays(spec)

    assert spec.case_root.stat().st_mode & 0o777 == 0o700
    for directory in (
        spec.case_root / "workspace",
        spec.case_root / "data",
        spec.case_root / "logs",
        spec.case_root / "plots",
        spec.case_root / "sqlrag",
    ):
        assert directory.stat().st_mode & 0o777 == 0o700
    for file_path in (
        spec.case_root / ".schema_write.lock",
        spec.case_root / "workspace" / ".schema_write.lock",
    ):
        assert file_path.stat().st_mode & 0o777 == 0o600
    assert not (spec.case_root / "memory").exists()
    with pytest.raises(SandboxError, match="already exists"):
        prepare_case_overlays(spec)

    (spec.case_root / "logs" / "leaked.log").write_text("nope\n", encoding="utf-8")
    (spec.case_root / "logs" / "leaked.log").chmod(0o644)
    with pytest.raises(SandboxError, match="private"):
        verify_private_case_files(spec)


def test_private_inventory_rejects_wrong_owner_and_special_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "case",
        database_path=tmp_path / "input.sqlite",
        database_id="db",
        secret_dir=tmp_path / "secrets",
        port=18765,
        runtime_env={},
        shared_schema_memory_root=_prepared_shared_schema_memory(tmp_path, "db"),
    )
    prepare_case_overlays(spec)
    actual_uid = os.getuid()
    monkeypatch.setattr(sandbox_module.os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(SandboxError, match="owner"):
        verify_private_case_files(spec)
    monkeypatch.setattr(sandbox_module.os, "getuid", lambda: actual_uid)
    fifo = spec.case_root / "logs" / "special"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(SandboxError, match="special file"):
        verify_private_case_files(spec)


def test_canonical_mode_rejects_parallel_workers() -> None:
    with pytest.raises(SandboxError, match="workers=1"):
        validate_canonical_workers(2)


def test_resume_rejects_snapshot_or_state_root_mismatch(tmp_path: Path) -> None:
    manifest = {
        "source_snapshot_digest": "sha256:" + "a" * 64,
        "state_root": str((tmp_path / "state").resolve()),
    }

    with pytest.raises(SandboxError, match="snapshot"):
        validate_resume_integrity(
            manifest,
            source_snapshot_digest="sha256:" + "b" * 64,
            state_root=tmp_path / "state",
        )
    with pytest.raises(SandboxError, match="state_root"):
        validate_resume_integrity(
            manifest,
            source_snapshot_digest="sha256:" + "a" * 64,
            state_root=tmp_path / "other",
        )


def test_bwrap_missing_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(SandboxError, match="bwrap"):
        ensure_bwrap_available()


def test_stop_process_escalates_from_term_to_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []

    class StubbornProcess:
        pid = 123

        @staticmethod
        def poll() -> None:
            return None

    clock = iter((0.0, 0.0, 5.1))
    monkeypatch.setattr(sandbox_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(sandbox_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        sandbox_module.os,
        "killpg",
        lambda _pid, signal_value: signals.append(signal_value),
    )

    sandbox_module._stop_process(StubbornProcess())

    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_case_runner_fails_closed_for_health_reap_and_starts_once(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(mode=0o700)
    secret_dir.chmod(0o700)
    for name in (
        "ag_ui_auth_token_map",
        "openai_api_key",
        "openai_api_key_db",
        "hf_token",
    ):
        path = secret_dir / name
        path.write_text("test\n", encoding="utf-8")
        path.chmod(0o600)
    (tmp_path / "venv").mkdir()
    (tmp_path / "input.sqlite").write_bytes(b"SQLite format 3\x00")
    spec = BwrapSandboxSpec(
        snapshot_root=snapshot.root,
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "case",
        database_path=tmp_path / "input.sqlite",
        database_id="db",
        secret_dir=secret_dir,
        port=18765,
        runtime_env={},
        shared_schema_memory_root=_prepared_shared_schema_memory(tmp_path, "db-case-runner"),
    )
    events: list[str] = []
    starts: list[list[str]] = []
    runner = SandboxCaseRunner(
        start=lambda command: (
            events.append("process") or starts.append(command) or object()
        ),
        wait_for_health=lambda _port: False,
        stop=lambda _process: None,
        verify_reaped=lambda _process: True,
    )

    with pytest.raises(SandboxError, match="health"):
        runner.run(
            spec,
            lambda: pytest.fail("case must not start before health"),
            expected_snapshot=snapshot,
            before_start=lambda: events.append("receipt"),
        )
    assert len(starts) == 1
    assert events == ["receipt", "process"]

    runner = SandboxCaseRunner(
        start=lambda _command: object(),
        wait_for_health=lambda _port: True,
        stop=lambda _process: None,
        verify_reaped=lambda _process: False,
    )
    reap_spec = BwrapSandboxSpec(
        snapshot_root=snapshot.root,
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "reap-case",
        database_path=tmp_path / "input.sqlite",
        database_id="db",
        secret_dir=secret_dir,
        port=18766,
        runtime_env={},
        shared_schema_memory_root=_prepared_shared_schema_memory(tmp_path, "db-reap"),
    )
    with pytest.raises(SandboxError, match="reap"):
        runner.run(
            reap_spec,
            lambda: {"run_id": "one"},
            expected_snapshot=snapshot,
        )


def test_case_runner_final_verification_happens_after_receipt_before_spawn(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    snapshot = create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(mode=0o700)
    secret_dir.chmod(0o700)
    for name in (
        "ag_ui_auth_token_map",
        "openai_api_key",
        "openai_api_key_db",
        "hf_token",
    ):
        path = secret_dir / name
        path.write_text("test\n", encoding="utf-8")
        path.chmod(0o600)
    database = tmp_path / "input.sqlite"
    database.write_bytes(b"SQLite format 3\x00")
    spec = BwrapSandboxSpec(
        snapshot_root=snapshot.root,
        venv_root=tmp_path / "venv",
        case_root=tmp_path / "state" / "case",
        database_path=database,
        database_id="db",
        secret_dir=secret_dir,
        port=18765,
        runtime_env={},
        expected_database_sha256=sandbox_module._sha256(database),
        shared_schema_memory_root=_prepared_shared_schema_memory(tmp_path, "db"),
    )
    (tmp_path / "venv").mkdir()
    spawned = False

    def start(_command: list[str]) -> object:
        nonlocal spawned
        spawned = True
        return object()

    runner = SandboxCaseRunner(
        start=start,
        wait_for_health=lambda _port: False,
        stop=lambda _process: None,
        verify_reaped=lambda _process: True,
    )

    def tamper_after_receipt() -> None:
        # The snapshot seals files to 0o444, so unlock/reseal around the
        # tamper write — otherwise even the owning user can't write the
        # "changed" content, and leaving it unlocked would let the
        # permission check fire first and mask the content-mismatch
        # rejection this test is actually about.
        app_py = snapshot.root / "backend" / "app.py"
        app_py.chmod(0o644)
        app_py.write_text("APP = 2\n", encoding="utf-8")
        app_py.chmod(0o444)

    with pytest.raises(SandboxError, match="frozen manifest"):
        runner.run(
            spec,
            lambda: None,
            expected_snapshot=snapshot,
            before_start=tamper_after_receipt,
        )
    assert spawned is False


@pytest.mark.parametrize("target_kind", ["database", "venv"])
@pytest.mark.parametrize("symlink_kind", ["leaf", "parent"])
def test_bwrap_rejects_symlinked_database_or_venv_before_resolve(
    tmp_path: Path,
    target_kind: str,
    symlink_kind: str,
) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    real_database = real_root / "input.sqlite"
    real_database.write_bytes(b"SQLite format 3\x00")
    real_venv = real_root / "venv"
    real_venv.mkdir()
    if symlink_kind == "leaf":
        database = tmp_path / "database-link"
        venv = tmp_path / "venv-link"
        database.symlink_to(real_database)
        venv.symlink_to(real_venv, target_is_directory=True)
    else:
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(real_root, target_is_directory=True)
        database = linked_parent / "input.sqlite"
        venv = linked_parent / "venv"
    spec = BwrapSandboxSpec(
        snapshot_root=tmp_path / "snapshot",
        venv_root=venv if target_kind == "venv" else real_venv,
        case_root=tmp_path / "case",
        database_path=database if target_kind == "database" else real_database,
        database_id="db",
        secret_dir=tmp_path / "secrets",
        port=18765,
        runtime_env={},
    )

    with pytest.raises(SandboxError, match=target_kind):
        build_bwrap_command(spec)
