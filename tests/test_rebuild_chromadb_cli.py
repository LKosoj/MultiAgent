"""CLI contract for the fixed schema-memory embedding gateway."""

from __future__ import annotations

import importlib
import sqlite3
import sys
from types import SimpleNamespace

import pytest


def _argv(monkeypatch, db_path, chroma_path, *extra):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rebuild_chromadb.py",
            "--db-path",
            str(db_path),
            "--chroma-path",
            str(chroma_path),
            *extra,
        ],
    )


def test_rebuild_cli_passes_exact_handler_to_typed_rebuild(monkeypatch, tmp_path):
    import memory.database as database_module
    import memory.manager as manager_module
    import memory.rebuild as rebuild_module
    from memory.rebuild import RebuildStatus

    db_path = tmp_path / "memory.sqlite"
    db_path.touch()
    handler = SimpleNamespace()
    received: list[object] = []

    monkeypatch.setattr(database_module, "DatabaseHandler", lambda **_kwargs: handler)
    monkeypatch.setattr(manager_module, "MemoryManager", lambda **_kwargs: None)
    monkeypatch.setattr(
        rebuild_module,
        "rebuild_chromadb_from_sqlite",
        lambda *, db_handler: (
            received.append(db_handler)
            or SimpleNamespace(status=RebuildStatus.COMPLETE)
        ),
    )
    _argv(monkeypatch, db_path, tmp_path / "chroma")

    importlib.import_module("rebuild_chromadb").main()

    assert received == [handler]


def test_rebuild_cli_exits_nonzero_for_incomplete_typed_report(monkeypatch, tmp_path):
    import memory.database as database_module
    import memory.manager as manager_module
    import memory.rebuild as rebuild_module
    from memory.rebuild import RebuildStatus

    db_path = tmp_path / "memory.sqlite"
    db_path.touch()
    handler = SimpleNamespace()
    received: list[object] = []

    monkeypatch.setattr(database_module, "DatabaseHandler", lambda **_kwargs: handler)
    monkeypatch.setattr(manager_module, "MemoryManager", lambda **_kwargs: None)
    monkeypatch.setattr(
        rebuild_module,
        "rebuild_chromadb_from_sqlite",
        lambda *, db_handler: (
            received.append(db_handler)
            or SimpleNamespace(status=RebuildStatus.FAILED)
        ),
    )
    _argv(monkeypatch, db_path, tmp_path / "chroma")

    with pytest.raises(SystemExit) as raised:
        importlib.import_module("rebuild_chromadb").main()

    assert raised.value.code == 1
    assert received == [handler]


def test_rebuild_cli_stats_reads_collections_from_handler(monkeypatch, tmp_path):
    import memory.database as database_module
    import memory.manager as manager_module

    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE agent_memory (id INTEGER)")
        conn.execute("CREATE TABLE strategic_memory (id INTEGER)")
        conn.commit()
    finally:
        conn.close()

    handler = SimpleNamespace(
        tactical_collection=SimpleNamespace(count=lambda: 2),
        strategic_collection=SimpleNamespace(count=lambda: 3),
    )
    monkeypatch.setattr(database_module, "DatabaseHandler", lambda **_kwargs: handler)
    monkeypatch.setattr(manager_module, "MemoryManager", lambda **_kwargs: object())
    _argv(monkeypatch, db_path, tmp_path / "chroma", "--stats-only")

    importlib.import_module("rebuild_chromadb").main()
