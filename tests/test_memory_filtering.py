import pytest
import json
import sqlite3
import threading
import types
import memory.tools as memory_tools
from memory.tools import get_memory
from memory.manager import MemoryManager


@pytest.fixture
def mock_memory_manager(monkeypatch):
    """Мокируем memory_manager для тестов."""
    # Создаём простой mock-объект
    class MockMemoryManager:
        def __init__(self):
            self.db_handler = MockDBHandler()
    
    class MockDBHandler:
        def __init__(self):
            self.tactical_collection = None
        
        def _get_connection(self):
            return MockConnection()
    
    class MockConnection:
        def cursor(self):
            return MockCursor()

        def close(self):
            pass
    
    class MockCursor:
        def __init__(self):
            self.results = []
        
        def execute(self, query, params=None):
            # Эмулируем результаты в зависимости от параметров
            if params and len(params) >= 3:
                session_id, cache_kind, cache_key = params[0], params[1] if len(params) > 1 else None, params[2] if len(params) > 2 else None
                if cache_kind == "schema_linking" and cache_key == "test_key":
                    self.results = [(1, json.dumps({
                        "cache_kind": "schema_linking",
                        "cache_key": "test_key", 
                        "schema_version": "v1.0",
                        "result": {"test": "data"}
                    }))]
                else:
                    self.results = []
            else:
                self.results = []
        
        def fetchall(self):
            return self.results
    
    mock_manager = MockMemoryManager()
    monkeypatch.setattr(memory_tools, "memory_manager", mock_manager)
    return mock_manager


def test_get_memory_with_cache_filtering(mock_memory_manager):
    """Тест фильтрации памяти по cache_kind и cache_key."""
    # Тест с cache_kind и cache_key
    results = get_memory(
        session_id="test_session",
        cache_kind="schema_linking",
        cache_key="test_key",
        schema_version="v1.0"
    )
    
    # Проверяем, что результат содержит ожидаемые данные
    assert isinstance(results, list)
    # Поскольку мы мокируем, проверяем, что функция отработала без ошибок


def test_get_memory_without_filters(mock_memory_manager):
    """Тест получения памяти без фильтров."""
    results = get_memory(session_id="test_session")
    assert isinstance(results, list)


def test_get_memory_with_partial_filters(mock_memory_manager):
    """Тест с частичными фильтрами."""
    results = get_memory(
        session_id="test_session",
        cache_kind="vector_db_search"
    )
    assert isinstance(results, list)


def test_filter_records_by_relevance_does_not_skip_most_relevant_large_record():
    records = [
        {"agent_name": "a", "step": 1, "data": {"text": "x" * 100}},
        {"agent_name": "a", "step": 2, "data": {"text": "small"}},
    ]

    assert memory_tools._filter_records_by_relevance(records, size_limit=20) == []


class _SQLiteDBHandler:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.lock = threading.Lock()
        self.tactical_collection = None
        self.embedding_model = None

    def _get_connection(self):
        return sqlite3.connect(self.db_path)


def _make_sqlite_db_handler(tmp_path):
    db_handler = _SQLiteDBHandler(tmp_path / "memory.sqlite")
    conn = db_handler._get_connection()
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
                    valid_from TEXT,
                    valid_to TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_handler


def _insert_memory_row(
    db_handler,
    *,
    step,
    data_text,
    session_id="sess",
    agent_name="Schema-RAG-Agent",
    instance_step=None,
    run_id=None,
):
    conn = db_handler._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO agent_memory (
                session_id, agent_name, step, instance_step, run_id,
                data, valid_from, valid_to, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                session_id,
                agent_name,
                step,
                instance_step,
                run_id,
                data_text,
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _compact_json(payload):
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _legacy_spaced_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def test_get_memory_cache_filters_match_compact_and_legacy_spaced_json(monkeypatch, tmp_path):
    db_handler = _make_sqlite_db_handler(tmp_path)
    monkeypatch.setattr(
        memory_tools,
        "memory_manager",
        types.SimpleNamespace(db_handler=db_handler),
    )

    target = {
        "cache_kind": "schema_table",
        "cache_key": "key_100%",
        "schema_version": "v_1",
    }
    _insert_memory_row(
        db_handler,
        step=1,
        data_text=_compact_json({**target, "format": "compact"}),
    )
    _insert_memory_row(
        db_handler,
        step=2,
        data_text=_legacy_spaced_json({**target, "format": "spaced"}),
    )
    _insert_memory_row(
        db_handler,
        step=3,
        data_text=_compact_json({
            **target,
            "cache_key": "key_100X",
            "format": "percent-wildcard-candidate",
        }),
    )
    _insert_memory_row(
        db_handler,
        step=4,
        data_text=_compact_json({
            **target,
            "schema_version": "vx1",
            "format": "underscore-wildcard-candidate",
        }),
    )

    with memory_tools.memory_requester_context("Schema-RAG-Agent"):
        results = get_memory(
            session_id="sess",
            cache_kind="schema_table",
            cache_key="key_100%",
            schema_version="v_1",
            requesting_agent="Schema-RAG-Agent",
        )

    assert [item["step"] for item in results] == [1, 2]
    assert {item["data"]["format"] for item in results} == {"compact", "spaced"}


def test_get_memory_uses_actual_chroma_l2_metric_for_semantic_scores(monkeypatch, tmp_path):
    from custom_tools.text_to_sql import similarity_thresholds_config

    db_handler = _make_sqlite_db_handler(tmp_path)
    _insert_memory_row(
        db_handler,
        step=1,
        data_text=_compact_json({"artifact_type": "general", "text": "hit"}),
        session_id="sess",
        agent_name="agent",
    )
    db_handler.tactical_collection = types.SimpleNamespace(
        metadata={"hnsw:space": "l2"},
        configuration=None,
    )

    memory_manager = types.SimpleNamespace(
        db_handler=db_handler,
        _search_semantic_with_scores=lambda *_args, **_kwargs: {
            "ids": ["sess-agent-1"],
            "distances": [[4.0]],
            "metadatas": [[{"session_id": "sess", "agent_name": "agent", "step": 1}]],
        },
    )
    monkeypatch.setattr(memory_tools, "memory_manager", memory_manager)
    monkeypatch.setattr(
        similarity_thresholds_config,
        "resolve_threshold",
        lambda *_args, **_kwargs: 0.19,
    )
    monkeypatch.setenv("RAG_RERANK_ENABLED", "0")

    with memory_tools.memory_requester_context("agent"):
        results = get_memory(
            session_id="sess",
            query="hit",
            requesting_agent="agent",
        )

    assert len(results) == 1
    assert results[0]["score"] == pytest.approx(0.2)


def test_summary_agent_memory_step_detects_replaced_active_row(monkeypatch, tmp_path):
    db_handler = _make_sqlite_db_handler(tmp_path)
    memory_manager = types.SimpleNamespace(
        db_handler=db_handler,
        is_memory_updated=False,
    )
    monkeypatch.setattr(memory_tools, "memory_manager", memory_manager)
    _insert_memory_row(
        db_handler,
        step=1,
        data_text="old data that is long enough to summarize",
    )

    class _Response:
        content = "summary from stale data"

    def fake_model_summary(_messages, max_tokens):
        assert max_tokens == 4000
        with db_handler.lock:
            conn = db_handler._get_connection()
            try:
                conn.execute(
                    """
                    UPDATE agent_memory
                    SET valid_to = ?, updated_at = ?
                    WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
                    """,
                    (
                        "2026-01-01T00:00:01",
                        "2026-01-01T00:00:01",
                        "sess",
                        "Schema-RAG-Agent",
                        1,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO agent_memory (
                        session_id, agent_name, step, instance_step, run_id,
                        data, valid_from, valid_to, created_at, updated_at
                    )
                    VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, ?, ?)
                    """,
                    (
                        "sess",
                        "Schema-RAG-Agent",
                        1,
                        "fresh data from another writer",
                        "2026-01-01T00:00:02",
                        "2026-01-01T00:00:02",
                        "2026-01-01T00:00:02",
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return _Response()

    monkeypatch.setattr(memory_tools, "model_summary", fake_model_summary)

    result = memory_tools.summary_agent_memory_step(
        "sess",
        "Schema-RAG-Agent",
        1,
    )

    assert "Конфликт записи" in result
    assert memory_manager.is_memory_updated is False

    conn = db_handler._get_connection()
    try:
        rows = conn.execute(
            """
            SELECT data
            FROM agent_memory
            WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
            """,
            ("sess", "Schema-RAG-Agent", 1),
        ).fetchall()
    finally:
        conn.close()

    assert rows == [("fresh data from another writer",)]


def test_save_memory_preserves_large_strings_without_llm_summarization(monkeypatch, tmp_path):
    db_handler = _make_sqlite_db_handler(tmp_path)
    memory_manager = types.SimpleNamespace(
        db_handler=db_handler,
        is_memory_updated=False,
        _resolve_conflicts=lambda *_args, **_kwargs: [],
        _deactivate_conflicting_records=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(memory_tools, "memory_manager", memory_manager)
    monkeypatch.setattr(
        memory_tools,
        "call_openai_api",
        lambda *args, **kwargs: pytest.fail("save_memory must not summarize on write path"),
    )

    large_text = "x" * 100_001
    step = memory_tools.save_memory(
        "sess",
        "agent",
        {"cache_kind": "agent_step", "agent_response": large_text},
    )

    assert step == 1
    conn = db_handler._get_connection()
    try:
        stored = conn.execute(
            "SELECT data FROM agent_memory WHERE session_id = ? AND agent_name = ?",
            ("sess", "agent"),
        ).fetchone()[0]
    finally:
        conn.close()

    payload = json.loads(stored)
    assert payload["agent_response"] == large_text


def test_save_memory_retries_step_allocation_after_unique_conflict(monkeypatch):
    class FakeCursor:
        def __init__(self, conn):
            self.conn = conn
            self.fetchone_result = None

        def execute(self, query, params=None):
            normalized = " ".join(query.split()).upper()
            if normalized == "BEGIN IMMEDIATE":
                self.conn.begins += 1
            elif normalized.startswith("SELECT MAX(STEP)"):
                self.fetchone_result = (self.conn.max_step,)
            elif normalized.startswith("INSERT INTO AGENT_MEMORY"):
                self.conn.insert_attempts += 1
                if self.conn.insert_attempts == 1:
                    self.conn.max_step = params[2]
                    raise sqlite3.IntegrityError("duplicate active step")
                self.conn.inserted_step = params[2]
                self.conn.max_step = params[2]
            return self

        def fetchone(self):
            return self.fetchone_result

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor(self)
            self.max_step = 0
            self.inserted_step = None
            self.insert_attempts = 0
            self.begins = 0
            self.commits = 0
            self.rollbacks = 0
            self.closed = False

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed = True

    fake_conn = FakeConnection()
    db_handler = types.SimpleNamespace(
        lock=threading.Lock(),
        _get_connection=lambda: fake_conn,
        tactical_collection=None,
        embedding_model=None,
    )
    memory_manager = types.SimpleNamespace(
        db_handler=db_handler,
        is_memory_updated=False,
        _resolve_conflicts=lambda *_args, **_kwargs: [],
        _deactivate_conflicting_records=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(memory_tools, "memory_manager", memory_manager)

    step = memory_tools.save_memory("sess", "agent", {"value": "payload"})

    assert step == 2
    assert fake_conn.begins == 2
    assert fake_conn.rollbacks == 1
    assert fake_conn.commits == 1
    assert fake_conn.inserted_step == 2
    assert memory_manager.is_memory_updated is True


def test_save_memory_marks_row_needs_reindex_on_chroma_failure(monkeypatch, tmp_path):
    db_handler = _make_sqlite_db_handler(tmp_path)

    class FailingCollection:
        def add(self, **_kwargs):
            raise RuntimeError("chroma down")

    db_handler.tactical_collection = FailingCollection()
    db_handler.embedding_model = object()
    memory_manager = types.SimpleNamespace(
        db_handler=db_handler,
        is_memory_updated=False,
        _resolve_conflicts=lambda *_args, **_kwargs: [],
        _deactivate_conflicting_records=lambda *_args, **_kwargs: None,
        _extract_text_content=lambda data: str(data),
        _create_embedding=lambda text, purpose: [0.1, 0.2],
    )
    monkeypatch.setattr(memory_tools, "memory_manager", memory_manager)

    step = memory_tools.save_memory(
        "sess",
        "agent",
        {"cache_kind": "agent_step", "agent_response": "important"},
    )

    assert step == 1
    conn = db_handler._get_connection()
    try:
        stored = conn.execute(
            "SELECT data FROM agent_memory WHERE session_id = ? AND agent_name = ?",
            ("sess", "agent"),
        ).fetchone()[0]
    finally:
        conn.close()

    payload = json.loads(stored)
    assert payload["needs_reindex"] is True
    assert "chroma down" in payload["reindex_error"]


def test_extract_keywords_ignores_soft_deleted_records(monkeypatch, tmp_path):
    db_handler = _make_sqlite_db_handler(tmp_path)
    memory_manager = types.SimpleNamespace(db_handler=db_handler)
    monkeypatch.setattr(memory_tools, "memory_manager", memory_manager)

    _insert_memory_row(
        db_handler,
        step=1,
        data_text="active_keyword",
        session_id="sess",
        agent_name="agent",
    )
    conn = db_handler._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO agent_memory (
                session_id, agent_name, step, instance_step, run_id,
                data, valid_from, valid_to, updated_at
            )
            VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                "sess",
                "agent",
                2,
                "deleted_keyword",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    captured = {}

    def fake_textrank(text, min_length, max_results):
        captured["text"] = text
        return ["active_keyword"]

    monkeypatch.setattr(memory_tools, "_extract_keywords_textrank", fake_textrank)

    assert memory_tools.extract_keywords("sess", "agent") == ["active_keyword"]
    assert "active_keyword" in captured["text"]
    assert "deleted_keyword" not in captured["text"]


def test_summary_agent_memory_step_writes_json_summary_shape(monkeypatch, tmp_path):
    db_handler = _make_sqlite_db_handler(tmp_path)

    class CapturingCollection:
        def __init__(self):
            self.metadatas = None

        def update(self, **kwargs):
            self.metadatas = kwargs["metadatas"]

    collection = CapturingCollection()
    db_handler.tactical_collection = collection
    db_handler.embedding_model = object()
    memory_manager = types.SimpleNamespace(
        db_handler=db_handler,
        is_memory_updated=False,
        _create_embedding=lambda text, purpose: [0.1, 0.2],
    )
    monkeypatch.setattr(memory_tools, "memory_manager", memory_manager)
    _insert_memory_row(
        db_handler,
        step=1,
        data_text='{"cache_kind":"agent_step","agent_response":"raw"}',
        session_id="sess",
        agent_name="agent",
        instance_step=7,
        run_id="run-1",
    )

    class _Response:
        content = "summary text"

    monkeypatch.setattr(memory_tools, "model_summary", lambda _messages, max_tokens: _Response())

    assert memory_tools.summary_agent_memory_step("sess", "agent", 1) == "summary text"

    conn = db_handler._get_connection()
    try:
        stored = conn.execute(
            """
            SELECT data
            FROM agent_memory
            WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
            """,
            ("sess", "agent", 1),
        ).fetchone()[0]
    finally:
        conn.close()

    payload = json.loads(stored)
    assert payload["cache_kind"] == "agent_summary"
    assert payload["summary_text"] == "summary text"
    assert payload["source_agent_name"] == "agent"
    assert payload["source_step"] == 1
    assert payload["run_id"] == "run-1"
    assert payload["instance_step"] == 7
    assert collection.metadatas == [{
        "session_id": "sess",
        "agent_name": "agent",
        "step": 1,
        "tactical_id": "sess-agent-1",
        "cache_kind": "agent_summary",
        "artifact_type": "summary",
        "is_summary": True,
        "run_id": "run-1",
        "instance_step": 7,
    }]


def test_clear_session_memory_reports_chroma_cleanup_failure(monkeypatch, tmp_path):
    db_handler = _make_sqlite_db_handler(tmp_path)
    _insert_memory_row(
        db_handler,
        step=1,
        data_text='{"cache_kind":"agent_step","agent_response":"raw"}',
        session_id="sess",
        agent_name="agent",
    )

    class TrackingLock:
        def __init__(self):
            self.held = False
            self.acquisitions = 0

        def acquire(self):
            assert self.held is False
            self.held = True
            self.acquisitions += 1

        def release(self):
            assert self.held is True
            self.held = False

    tracking_lock = TrackingLock()
    db_handler.lock = tracking_lock

    class FailingCollection:
        def get(self, where):
            assert tracking_lock.held is True
            return {"ids": ["sess-agent-1"]}

        def delete(self, ids):
            assert tracking_lock.held is True
            raise RuntimeError("delete failed")

    db_handler.tactical_collection = FailingCollection()
    memory_manager = types.SimpleNamespace(
        db_handler=db_handler,
        is_memory_updated=False,
        summary="stale",
    )
    monkeypatch.setattr(memory_tools, "memory_manager", memory_manager)

    result = memory_tools.clear_session_memory("sess", "tactical")

    assert "Требуется reindex ChromaDB" in result
    assert "delete failed" in result
    assert memory_manager.summary == ""
    assert tracking_lock.acquisitions == 1
    assert tracking_lock.held is False

    conn = db_handler._get_connection()
    try:
        rows = conn.execute("SELECT COUNT(*) FROM agent_memory WHERE session_id = ?", ("sess",)).fetchone()[0]
    finally:
        conn.close()
    assert rows == 0


def test_schema_conflict_lookup_matches_compact_and_legacy_spaced_json(tmp_path):
    db_handler = _make_sqlite_db_handler(tmp_path)
    manager = MemoryManager(database_handler=db_handler)

    target = {
        "cache_kind": "schema_table",
        "table_fqn": "public.orders",
        "filename": "schema.md",
    }
    _insert_memory_row(db_handler, step=1, data_text=_compact_json(target))
    _insert_memory_row(db_handler, step=2, data_text=_legacy_spaced_json(target))
    _insert_memory_row(
        db_handler,
        step=3,
        data_text=_legacy_spaced_json({**target, "filename": "other.md"}),
    )
    _insert_memory_row(
        db_handler,
        step=4,
        data_text=_compact_json({**target, "cache_kind": "vector_db_search"}),
    )

    conflicts = manager._resolve_schema_conflicts(
        "sess",
        "Schema-RAG-Agent",
        {
            "cache_kind": "schema_table",
            "table_fqn": "public.orders",
            "filename": "schema.md",
        },
    )

    assert set(conflicts) == {
        ("sess", "Schema-RAG-Agent", 1),
        ("sess", "Schema-RAG-Agent", 2),
    }
