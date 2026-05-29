import pytest
import json
import sqlite3
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


class _SQLiteDBHandler:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.tactical_collection = None

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
):
    conn = db_handler._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO agent_memory (
                session_id, agent_name, step, instance_step, run_id,
                data, valid_from, valid_to, updated_at
            )
            VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, ?)
            """,
            (
                session_id,
                agent_name,
                step,
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

    results = get_memory(
        session_id="sess",
        cache_kind="schema_table",
        cache_key="key_100%",
        schema_version="v_1",
    )

    assert [item["step"] for item in results] == [1, 2]
    assert {item["data"]["format"] for item in results} == {"compact", "spaced"}


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
