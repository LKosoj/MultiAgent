import os
import sqlite3
from types import SimpleNamespace

import pytest


def test_allowed_artifacts_always_allows_general(monkeypatch):
    """Даже если general отсутствует в allowed_artifacts, он должен быть разрешен по умолчанию."""
    from memory import tools as mt

    # Подменяем policy запрашивающего агента
    monkeypatch.setattr(mt, "_get_requesting_agent_policy", lambda name: {
        "inter_agent_visibility": "readonly",
        "allowed_artifacts": ["sql_query"],  # general отсутствует
    })

    records = [
        {"agent_name": "a", "step": 1, "data": {"cache_kind": "agent_step", "artifact_type": "general", "agent_response": "x"}},
        {"agent_name": "a", "step": 2, "data": {"cache_kind": "agent_step", "artifact_type": "sql_query", "agent_response": "select 1"}},
        {"agent_name": "a", "step": 3, "data": {"cache_kind": "agent_step", "artifact_type": "summary", "agent_response": "s"}},
    ]

    out = mt._apply_policy_filters(records, requesting_agent="any_agent")
    types = {r["data"].get("artifact_type") for r in out}

    assert "general" in types
    assert "sql_query" in types
    assert "summary" not in types  # не разрешено профилем


def test_missing_requesting_agent_denies_private_memory():
    from memory import tools as mt

    records = [
        {"agent_name": "agent_a", "step": 1, "data": {"cache_kind": "agent_step"}},
        {"agent_name": "agent_b", "step": 2, "data": {"cache_kind": "agent_summary"}},
    ]

    assert mt._apply_policy_filters(records, requesting_agent=None, cache_kind=None) == []


def test_missing_requesting_agent_denies_technical_cache_reads():
    from memory import tools as mt

    records = [
        {"agent_name": "Schema-RAG-Agent", "step": 1, "data": {"cache_kind": "schema_table"}},
    ]

    assert mt._apply_policy_filters(records, requesting_agent=None, cache_kind="schema_table") == []


def test_get_memory_denies_private_records_without_requesting_agent(monkeypatch, tmp_path):
    from memory import tools as mt

    db_path = tmp_path / "memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE agent_memory (
                session_id TEXT,
                agent_name TEXT,
                step INTEGER,
                instance_step INTEGER,
                run_id TEXT,
                data TEXT,
                valid_from TEXT,
                valid_to TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO agent_memory
            (session_id, agent_name, step, instance_step, run_id, data, valid_from, valid_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("s1", "agent_a", 1, None, None, '{"cache_kind":"agent_step"}', "2026-07-02", None),
        )

    class DbHandler:
        tactical_collection = None

        def _get_connection(self):
            return sqlite3.connect(db_path)

    monkeypatch.setattr(mt.memory_manager, "db_handler", DbHandler())

    assert mt.get_memory(session_id="s1", agent_name="agent_a", requesting_agent=None) == []


def test_get_memory_summary_uses_trusted_requester_context(monkeypatch):
    from memory import tools as mt

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        db_handler=SimpleNamespace(lock=Lock()),
        is_memory_updated=True,
        summary="",
    )
    monkeypatch.setattr(mt, "memory_manager", manager)

    def fake_get_memory(session_id, requesting_agent=None, **_kwargs):
        requester = mt._resolve_requesting_agent(requesting_agent)
        if requester != "memory_archivist":
            return []
        return [
            {
                "agent_name": "agent_a",
                "step": 1,
                "data": {"cache_kind": "agent_step", "agent_response": "important fact"},
            }
        ]

    monkeypatch.setattr(mt, "get_memory", fake_get_memory)
    monkeypatch.setattr(mt, "model_summary", lambda _messages, max_tokens: "summary from memory")

    assert mt.get_memory_summary("s1") == "summary from memory"
    assert manager.summary == "summary from memory"
    assert manager.is_memory_updated is False


def test_default_routing_excludes_schema_and_cache(monkeypatch):
    """Если cache_kind не задан, по умолчанию исключаем schema_table и vector_db_search."""
    from memory import tools as mt

    monkeypatch.setenv("RAG_DEFAULT_EXCLUDE_CACHE_KINDS", "schema_table,vector_db_search")

    records = [
        {"agent_name": "x", "step": 1, "data": {"cache_kind": "schema_table", "artifact_type": "schema_info"}},
        {"agent_name": "x", "step": 2, "data": {"cache_kind": "vector_db_search", "artifact_type": "cache"}},
        {"agent_name": "x", "step": 3, "data": {"cache_kind": "agent_step", "artifact_type": "general"}},
        {"agent_name": "x", "step": 4, "data": {"cache_kind": "agent_summary", "artifact_type": "summary"}},
    ]

    out = mt._apply_default_cache_kind_routing(records, cache_kind=None)
    out_kinds = [r["data"].get("cache_kind") for r in out]

    assert "schema_table" not in out_kinds
    assert "vector_db_search" not in out_kinds
    assert "agent_step" in out_kinds
    assert "agent_summary" in out_kinds


def test_explicit_cache_kind_disables_default_routing(monkeypatch):
    """Если cache_kind задан явно, дефолтные исключения не применяются."""
    from memory import tools as mt

    monkeypatch.setenv("RAG_DEFAULT_EXCLUDE_CACHE_KINDS", "schema_table,vector_db_search")

    records = [
        {"agent_name": "x", "step": 1, "data": {"cache_kind": "schema_table", "artifact_type": "schema_info"}},
    ]

    out = mt._apply_default_cache_kind_routing(records, cache_kind="schema_table")
    assert len(out) == 1


def test_schema_probe_fact_never_uses_model_summary_or_filtering(monkeypatch):
    from memory import tools as mt

    records = [
        {
            "agent_name": "Schema-RAG-Agent",
            "step": 1,
            "data": {"cache_kind": "schema_probe_fact", "payload": "x" * 70_001},
        }
    ]
    monkeypatch.setattr(
        mt,
        "_create_memory_summary",
        lambda *_args: pytest.fail("structured probe facts must not be summarized"),
    )

    assert mt._apply_memory_filtering(
        records,
        query="revenue",
        total_length=70_001,
        cache_kind="schema_probe_fact",
    ) == records


def test_streamlit_memory_search_requires_session_for_user(monkeypatch):
    from backend.fastapi_app.agui.auth import Principal
    from memory.streamlit_api import MemoryRAGManager

    manager = MemoryRAGManager()
    user = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))

    result = manager.search_memory("query", principal=user)

    assert "requires session_id" in result.error_message


def test_streamlit_memory_search_scopes_user_session(monkeypatch):
    from backend.fastapi_app.agui.auth import Principal, scope_session_id
    from memory.streamlit_api import MemoryRAGManager, MemoryStatus

    manager = MemoryRAGManager()
    user = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))
    captured = {}
    monkeypatch.setattr(
        manager,
        "get_memory_status",
        lambda: MemoryStatus(
            sqlite_available=True,
            chromadb_available=True,
            embedding_model_available=True,
            sqlite_path="",
            chromadb_path="",
            embedding_model_name="fake",
        ),
    )

    def fake_search(db_handler, query, limit, session_id, agent_name):
        captured["session_id"] = session_id
        return []

    manager.memory_manager = SimpleNamespace(db_handler=object())
    monkeypatch.setattr(manager, "_search_tactical_memory", fake_search)

    result = manager.search_memory("query", session_id="base-session", principal=user)

    assert result.error_message is None
    assert captured["session_id"] == scope_session_id("base-session", user)


def test_streamlit_memory_export_requires_session_for_user():
    from backend.fastapi_app.agui.auth import Principal
    from memory.streamlit_api import MemoryRAGManager

    manager = MemoryRAGManager()
    user = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))

    result = manager.export_memory(principal=user)

    assert result["success"] is False
    assert "requires session_id" in result["error"]


def test_streamlit_memory_export_scopes_user_session(monkeypatch, tmp_path):
    from backend.fastapi_app.agui.auth import Principal, scope_session_id
    from memory.streamlit_api import MemoryRAGManager, MemoryStatus

    db_path = tmp_path / "memory.db"
    user = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))
    scoped_session = scope_session_id("base-session", user)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE agent_memory (
                session_id TEXT,
                agent_name TEXT,
                data TEXT,
                timestamp TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO agent_memory VALUES (?, ?, ?, ?)",
            (scoped_session, "agent", "own", "2026-07-02T00:00:00"),
        )
        conn.execute(
            "INSERT INTO agent_memory VALUES (?, ?, ?, ?)",
            ("base-session", "agent", "legacy", "2026-07-02T00:00:01"),
        )

    manager = MemoryRAGManager()
    manager.memory_manager = SimpleNamespace(db_handler=SimpleNamespace(db_path=str(db_path)))
    monkeypatch.setattr(
        manager,
        "get_memory_status",
        lambda: MemoryStatus(
            sqlite_available=True,
            chromadb_available=False,
            embedding_model_available=False,
            sqlite_path=str(db_path),
            chromadb_path="",
            embedding_model_name="",
        ),
    )

    result = manager.export_memory(session_id="base-session", principal=user)

    assert result["success"] is True
    assert [row["data"] for row in result["data"]] == ["own"]
    assert result["data"][0]["session_id"] == scoped_session


def test_streamlit_memory_rejects_foreign_scoped_session():
    from backend.fastapi_app.agui.auth import Principal, scope_session_id
    from memory.streamlit_api import MemoryRAGManager

    manager = MemoryRAGManager()
    alice = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))
    bob = Principal(subject="bob", tenant_id="tenant-1", roles=frozenset({"user"}))
    alice_session = scope_session_id("base-session", alice)

    search = manager.search_memory("query", session_id=alice_session, principal=bob)
    export = manager.export_memory(session_id=alice_session, principal=bob)

    assert "session_id scope" in search.error_message
    assert export["success"] is False
    assert "session_id scope" in export["error"]


def test_clear_old_memories_purges_only_soft_deleted_rows(monkeypatch, tmp_path):
    from memory.streamlit_api import MemoryRAGManager, MemoryStatus

    db_path = tmp_path / "memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE agent_memory (
                session_id TEXT,
                agent_name TEXT,
                step INTEGER,
                data TEXT,
                valid_from TEXT,
                valid_to TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO agent_memory (session_id, agent_name, step, data, valid_from, valid_to)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("s1", "a", 1, "active old", "2000-01-01T00:00:00+00:00", None),
                ("s1", "a", 2, "deleted old", "2000-01-01T00:00:00+00:00", "2000-01-02T00:00:00+00:00"),
            ],
        )

    manager = MemoryRAGManager()
    manager.memory_manager = SimpleNamespace(
        db_handler=SimpleNamespace(
            db_path=str(db_path),
            tactical_collection=None,
        ),
        is_memory_updated=False,
    )
    monkeypatch.setattr(
        manager,
        "get_memory_status",
        lambda: MemoryStatus(
            sqlite_available=True,
            chromadb_available=False,
            embedding_model_available=False,
            sqlite_path=str(db_path),
            chromadb_path="",
            embedding_model_name="",
        ),
    )
    principal = SimpleNamespace(has_role=lambda role: role == "memory_archivist")

    result = manager.clear_old_memories(1, principal=principal)

    assert result["success"] is True
    assert result["deleted_sqlite"] == 1
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT step, data FROM agent_memory ORDER BY step").fetchall()
    assert rows == [(1, "active old")]


def test_get_memory_ignores_untrusted_requesting_agent(monkeypatch, tmp_path):
    from memory import tools as mt

    db_path = tmp_path / "memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE agent_memory (
                session_id TEXT,
                agent_name TEXT,
                step INTEGER,
                instance_step INTEGER,
                run_id TEXT,
                data TEXT,
                valid_from TEXT,
                valid_to TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO agent_memory
            (session_id, agent_name, step, instance_step, run_id, data, valid_from, valid_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("s1", "victim", 1, None, None, '{"cache_kind":"agent_step"}', "2026-07-02", None),
        )

    class DbHandler:
        tactical_collection = None

        def _get_connection(self):
            return sqlite3.connect(db_path)

    monkeypatch.setattr(mt.memory_manager, "db_handler", DbHandler())

    assert mt.get_memory(session_id="s1", agent_name="victim", requesting_agent="victim") == []
    assert mt.get_memory(session_id=None, requesting_agent="memory_archivist", query="x") == []


def test_get_memory_accepts_requesting_agent_from_trusted_context(monkeypatch, tmp_path):
    from memory import tools as mt

    db_path = tmp_path / "memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE agent_memory (
                session_id TEXT,
                agent_name TEXT,
                step INTEGER,
                instance_step INTEGER,
                run_id TEXT,
                data TEXT,
                valid_from TEXT,
                valid_to TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO agent_memory
            (session_id, agent_name, step, instance_step, run_id, data, valid_from, valid_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("s1", "agent_a", 1, None, None, '{"cache_kind":"agent_step"}', "2026-07-02", None),
        )

    class DbHandler:
        tactical_collection = None

        def _get_connection(self):
            return sqlite3.connect(db_path)

    monkeypatch.setattr(mt.memory_manager, "db_handler", DbHandler())

    with mt.memory_requester_context("agent_a"):
        records = mt.get_memory(session_id="s1", agent_name="agent_a", requesting_agent="agent_a")

    assert [item["step"] for item in records] == [1]


@pytest.mark.parametrize(
    "method,args",
    [
        ("import_memory_records", ([{"session_id": "s", "agent_name": "a", "data": "x"}],)),
        ("clear_all_memory", ()),
    ],
)
def test_streamlit_memory_global_mutations_require_archivist(method, args):
    from backend.fastapi_app.agui.auth import Principal
    from memory.streamlit_api import MemoryRAGManager

    manager = MemoryRAGManager()
    user = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))

    result = getattr(manager, method)(*args, principal=user)

    assert result["success"] is False
    assert "memory_archivist" in result["error"]


@pytest.mark.parametrize(
    "method,args",
    [
        ("get_active_agents", ()),
        ("get_temporal_stats", ()),
    ],
)
def test_streamlit_memory_global_reads_require_archivist(method, args):
    from backend.fastapi_app.agui.auth import Principal
    from memory.streamlit_api import MemoryRAGManager

    manager = MemoryRAGManager()
    user = Principal(subject="alice", tenant_id="tenant-1", roles=frozenset({"user"}))

    with pytest.raises(PermissionError, match="memory_archivist"):
        getattr(manager, method)(*args, principal=user)
