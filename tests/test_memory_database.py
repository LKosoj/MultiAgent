import sqlite3
import threading
from types import SimpleNamespace

import pytest


def _make_handler(tmp_path):
    from memory.database import DatabaseHandler

    handler = object.__new__(DatabaseHandler)
    handler.db_path = str(tmp_path / "memory.sqlite")
    handler.chroma_path = str(tmp_path / "chromadb")
    handler._lock = threading.Lock()
    handler._init_db()
    return handler


def test_database_connections_use_wal_busy_timeout_and_schema_version(tmp_path):
    from memory.database import MEMORY_DB_SCHEMA_VERSION

    handler = _make_handler(tmp_path)

    conn = handler._get_connection()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        assert conn.execute("PRAGMA user_version").fetchone()[0] == MEMORY_DB_SCHEMA_VERSION
    finally:
        conn.close()


def test_agent_memory_enforces_one_active_row_per_step(tmp_path):
    handler = _make_handler(tmp_path)

    conn = handler._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO agent_memory (session_id, agent_name, step, data, valid_to)
            VALUES (?, ?, ?, ?, NULL)
            """,
            ("s1", "agent", 1, "{}"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO agent_memory (session_id, agent_name, step, data, valid_to)
                VALUES (?, ?, ?, ?, NULL)
                """,
                ("s1", "agent", 1, "{}"),
            )

        conn.execute(
            """
            INSERT INTO agent_memory (session_id, agent_name, step, data, valid_to)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("s1", "agent", 1, "{}", "2026-07-02T00:00:00+00:00"),
        )
    finally:
        conn.close()


def test_init_db_soft_deletes_legacy_duplicate_active_rows_before_unique_index(tmp_path):
    from memory.database import DatabaseHandler

    db_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE agent_memory (
                session_id TEXT,
                agent_name TEXT,
                step INTEGER,
                instance_step INTEGER,
                run_id TEXT,
                data TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                valid_from DATETIME DEFAULT CURRENT_TIMESTAMP,
                valid_to DATETIME NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (session_id, agent_name, step, valid_to)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO agent_memory (
                session_id, agent_name, step, data, valid_from, valid_to,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                "s1",
                "agent",
                1,
                "old",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_memory (
                session_id, agent_name, step, data, valid_from, valid_to,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                "s1",
                "agent",
                1,
                "new",
                "2026-01-02T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    handler = object.__new__(DatabaseHandler)
    handler.db_path = str(db_path)
    handler.chroma_path = str(tmp_path / "chromadb")
    handler._lock = threading.Lock()
    handler._init_db()

    conn = handler._get_connection()
    try:
        active_rows = conn.execute(
            """
            SELECT data
            FROM agent_memory
            WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
            """,
            ("s1", "agent", 1),
        ).fetchall()
        closed_rows = conn.execute(
            """
            SELECT data, valid_to
            FROM agent_memory
            WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NOT NULL
            """,
            ("s1", "agent", 1),
        ).fetchall()

        assert active_rows == [("new",)]
        assert closed_rows and closed_rows[0][0] == "old"
        assert closed_rows[0][1]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO agent_memory (session_id, agent_name, step, data, valid_to)
                VALUES (?, ?, ?, ?, NULL)
                """,
                ("s1", "agent", 1, "{}"),
            )
    finally:
        conn.close()


def test_chroma_embedding_metadata_mismatch_is_visible(monkeypatch, tmp_path):
    from memory import database as db_module

    class FakeModel:
        def get_sentence_embedding_dimension(self):
            return 5

    class FakeCollection:
        metadata = {
            "hnsw:space": "cosine",
            "embedding_model_name": "old-model",
            "embedding_dimension": 3,
        }

    class FakeClient:
        def get_or_create_collection(self, name, metadata):
            self.last_metadata = metadata
            return FakeCollection()

    monkeypatch.setattr(db_module, "SentenceTransformer", lambda *args, **kwargs: FakeModel())
    monkeypatch.setattr(
        db_module.chromadb,
        "PersistentClient",
        lambda *args, **kwargs: FakeClient(),
    )

    handler = db_module.DatabaseHandler(
        db_path=str(tmp_path / "memory.sqlite"),
        chroma_path=str(tmp_path / "chroma"),
        embedding_model="new-model",
    )

    assert handler.embedding_dimension == 5
    assert handler.embedding_metadata_mismatch is True


def test_embedding_metadata_mismatch_is_enforced_except_rebuild_bypass():
    from memory.manager import EmbeddingUnavailableError, MemoryManager

    class FakeEmbedding:
        def tolist(self):
            return [0.1, 0.2]

    class FakeModel:
        def encode(self, text, convert_to_tensor=False):
            return FakeEmbedding()

    manager = object.__new__(MemoryManager)
    manager.db_handler = SimpleNamespace(
        embedding_model=FakeModel(),
        embedding_metadata_mismatch=True,
        embedding_model_name="new-model",
    )

    with pytest.raises(EmbeddingUnavailableError, match="metadata does not match"):
        manager._create_embedding("semantic text", purpose="passage")

    assert manager._create_embedding(
        "semantic text",
        purpose="passage",
        allow_metadata_mismatch=True,
    ) == [0.1, 0.2]
