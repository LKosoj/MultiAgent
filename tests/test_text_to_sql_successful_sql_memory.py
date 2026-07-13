from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.successful_sql_memory import (
    MAX_SUCCESSFUL_SQL_CONTEXT_BYTES,
    SUCCESSFUL_SQL_AGENT,
    SUCCESSFUL_SQL_CACHE_KIND,
    SuccessfulSqlMemoryRepository,
    canonical_example_id,
    canonical_schema_table_id,
)
from memory.index_consistency import SemanticIndexStatus


NAMESPACE = "a" * 64


def test_canonical_example_id_is_stable_and_namespace_isolated():
    first = canonical_example_id(
        namespace_version_key=NAMESPACE,
        user_query="  revenue\r\nby month  ",
        sql="  SELECT '\\r' AS value\r\n  ",
        dialect="SQLite",
    )
    same = canonical_example_id(
        namespace_version_key=NAMESPACE,
        user_query="revenue\nby month",
        sql="SELECT '\\r' AS value",
        dialect="sqlite",
    )
    changed = canonical_example_id(
        namespace_version_key="b" * 64,
        user_query="revenue\nby month",
        sql="SELECT '\\r' AS value",
        dialect="sqlite",
    )

    assert first == same
    assert first.startswith("text2sql-example-v1-")
    assert changed != first


def test_schema_table_id_is_stable_and_table_isolated():
    first = canonical_schema_table_id(NAMESPACE, "public.orders")

    assert first == canonical_schema_table_id(NAMESPACE, "public.orders")
    assert first != canonical_schema_table_id(NAMESPACE, "public.items")
    assert first.startswith("text2sql-schema-v1-")


@pytest.mark.parametrize(
    "evidence_override",
    [
        {"approved": False},
        {"executed": False},
        {"execution_success": False},
        {"audited": False},
    ],
)
def test_writer_rejects_incomplete_terminal_success_evidence(evidence_override):
    repository = SuccessfulSqlMemoryRepository()
    evidence = {
        "approved": True,
        "executed": True,
        "execution_success": True,
        "audited": True,
        **evidence_override,
    }

    with pytest.raises(ValueError, match="terminal success evidence"):
        repository.save(
            namespace_version_key=NAMESPACE,
            user_query="show orders",
            sql="SELECT * FROM orders",
            dialect="sqlite",
            run_id="run-1",
            **evidence,
        )


def test_writer_treats_save_memory_sentinel_as_failed(monkeypatch):
    repository = SuccessfulSqlMemoryRepository()
    monkeypatch.setattr(repository, "_save_memory", lambda **_kwargs: -1)

    receipt = repository.save(
        namespace_version_key=NAMESPACE,
        user_query="show orders",
        sql="SELECT * FROM orders",
        dialect="sqlite",
        run_id="run-1",
        approved=True,
        executed=True,
        execution_success=True,
        audited=True,
    )

    assert receipt.status is SemanticIndexStatus.FAILED
    assert receipt.sqlite_step is None
    assert receipt.error_code == "SQLITE_PERSISTENCE_FAILED"


def test_retrieval_context_is_bounded_and_namespace_scoped(monkeypatch):
    repository = SuccessfulSqlMemoryRepository()
    rows = []
    for index in range(5):
        user_query = ("q" * 4000) + str(index)
        sql = "SELECT '" + ("x" * 12000) + f"{index}'"
        example_id = canonical_example_id(
            namespace_version_key=NAMESPACE,
            user_query=user_query,
            sql=sql,
            dialect="sqlite",
        )
        rows.append({
            "serialization_version": 1,
            "example_id": example_id,
            "semantic_id": example_id,
            "namespace_version_key": NAMESPACE,
            "user_query": user_query,
            "sql": sql,
            "dialect": "sqlite",
            "run_id": f"run-{index}",
            "saved_at": "2026-07-12T00:00:00+00:00",
            "approved": True,
            "executed": True,
            "execution_success": True,
            "audited": True,
        })
    monkeypatch.setattr(repository, "_reconcile", lambda **_kwargs: _ready_reconciliation(rows))
    monkeypatch.setattr(repository, "_query_ids", lambda **_kwargs: [row["example_id"] for row in rows])
    monkeypatch.setattr(repository, "_load_active_payloads", lambda **_kwargs: rows)

    result = repository.retrieve(
        namespace_version_key=NAMESPACE,
        user_query="orders",
        top_k=50,
    )

    assert result.status == "READY"
    assert len(result.examples) == 3
    for example in result.examples:
        example._validate()
    assert len(result.context_json.encode("utf-8")) <= MAX_SUCCESSFUL_SQL_CONTEXT_BYTES
    assert len(json.loads(result.context_json)) <= 3


def _ready_reconciliation(rows):
    from memory.index_consistency import NamespaceReconciliation

    ids = tuple(sorted(row["example_id"] for row in rows))
    return NamespaceReconciliation(
        expected_ids=ids,
        indexed_ids=ids,
        missing_ids=(),
        unexpected_ids=(),
        failed_ids=(),
    )


class _SemanticCollection:
    def __init__(self):
        self.records = {}
        self.fail_upsert = False

    def upsert(self, *, ids, embeddings, documents, metadatas):
        if self.fail_upsert:
            raise RuntimeError("chroma unavailable")
        for index, semantic_id in enumerate(ids):
            self.records[semantic_id] = {
                "embedding": embeddings[index],
                "document": documents[index],
                "metadata": metadatas[index],
            }

    def get(self, where=None):
        ids = []
        for semantic_id, record in self.records.items():
            metadata = record["metadata"]
            if where and any(
                metadata.get(next(iter(condition)))
                != next(iter(condition.values())).get("$eq")
                for condition in where.get("$and", [])
            ):
                continue
            ids.append(semantic_id)
        return {"ids": sorted(ids)}

    def query(self, **_kwargs):
        return {"ids": [sorted(self.records)]}


def _semantic_memory_manager(monkeypatch, tmp_path):
    import memory.index_consistency as consistency_module
    import memory.manager as manager_module
    import memory.tools as tools_module

    db_path = tmp_path / "memory.sqlite"

    def get_connection():
        return sqlite3.connect(db_path, check_same_thread=False)

    conn = get_connection()
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
    collection = _SemanticCollection()
    handler = SimpleNamespace(
        lock=threading.Lock(),
        _get_connection=get_connection,
        tactical_collection=collection,
        embedding_model=object(),
    )
    manager = SimpleNamespace(
        db_handler=handler,
        is_memory_updated=False,
        _resolve_conflicts=lambda *_args, **_kwargs: [],
        _deactivate_conflicting_records=lambda *_args, **_kwargs: None,
        _extract_text_content=lambda data: " ".join(
            str(value) for value in data.values() if isinstance(value, str)
        ),
        _create_embedding=lambda _text, purpose: [0.1, 0.2],
    )
    monkeypatch.setattr(tools_module, "memory_manager", manager)
    monkeypatch.setattr(consistency_module, "memory_manager", manager)
    monkeypatch.setattr(manager_module, "memory_manager", manager)
    return manager, collection


def test_writer_retriever_and_crash_window_repair_use_one_exact_id(monkeypatch, tmp_path):
    manager, collection = _semantic_memory_manager(monkeypatch, tmp_path)
    repository = SuccessfulSqlMemoryRepository()
    save_kwargs = {
        "namespace_version_key": NAMESPACE,
        "user_query": "show orders",
        "sql": "SELECT * FROM orders",
        "dialect": "sqlite",
        "approved": True,
        "executed": True,
        "execution_success": True,
        "audited": True,
    }

    first = repository.save(run_id="run-1", **save_kwargs)
    second = repository.save(run_id="run-2", **save_kwargs)

    assert first.status is SemanticIndexStatus.INDEXED
    assert second.semantic_id == first.semantic_id
    conn = manager.db_handler._get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0] == 1
    finally:
        conn.close()

    collection.records.clear()
    result = repository.retrieve(
        namespace_version_key=NAMESPACE,
        user_query="orders",
    )

    assert result.status == "READY"
    assert [example.example_id for example in result.examples] == [first.semantic_id]
    assert set(collection.records) == {first.semantic_id}


def test_vector_failure_marker_clears_only_after_exact_id_is_observed(monkeypatch, tmp_path):
    manager, collection = _semantic_memory_manager(monkeypatch, tmp_path)
    repository = SuccessfulSqlMemoryRepository()
    collection.fail_upsert = True

    receipt = repository.save(
        namespace_version_key=NAMESPACE,
        user_query="show orders",
        sql="SELECT * FROM orders",
        dialect="sqlite",
        run_id="run-1",
        approved=True,
        executed=True,
        execution_success=True,
        audited=True,
    )

    assert receipt.status is SemanticIndexStatus.DEGRADED
    conn = manager.db_handler._get_connection()
    try:
        payload = json.loads(conn.execute("SELECT data FROM agent_memory").fetchone()[0])
    finally:
        conn.close()
    assert payload["needs_reindex"] is True

    collection.fail_upsert = False
    repaired = repository.retrieve(
        namespace_version_key=NAMESPACE,
        user_query="orders",
    )

    assert repaired.status == "READY"
    conn = manager.db_handler._get_connection()
    try:
        payload = json.loads(conn.execute("SELECT data FROM agent_memory").fetchone()[0])
    finally:
        conn.close()
    assert "needs_reindex" not in payload


def test_generation_plugin_injects_pre_serialized_context_deterministically():
    from custom_tools.text_to_sql.core._sql_generation_api import (
        sql_generation_plugin,
    )
    from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context

    captured = {}

    class Generator:
        def generate_sql(self, context, user_query, **kwargs):
            captured.update(context=context, user_query=user_query, kwargs=kwargs)
            return {"sql_query": "SELECT 1"}

    examples_json = json.dumps(
        [
            {
                "user_query": "one",
                "sql": "SELECT 1",
                "dialect": "sqlite",
            }
        ]
    )
    token = set_tool_runtime_context(
        {"successful_sql_context_json": examples_json}
    )
    try:
        result = sql_generation_plugin(
            json.dumps({"linked_entities": {}, "schema_info": {"t": {}}}),
            "one",
            dsn="sqlite:///unused.db",
            sql_generator=Generator(),
        )
    finally:
        reset_tool_runtime_context(token)

    assert result == {"sql_query": "SELECT 1"}
    structured = json.loads(captured["context"])
    assert structured["successful_sql_examples"] == json.loads(examples_json)


def test_generation_plugin_accepts_degraded_empty_context():
    from custom_tools.text_to_sql.core._sql_generation_api import (
        sql_generation_plugin,
    )
    from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context

    class Generator:
        def generate_sql(self, context, user_query, **kwargs):
            assert json.loads(context)["successful_sql_examples"] == []
            return {"sql_query": "SELECT 1"}

    token = set_tool_runtime_context({"successful_sql_context_json": "[]"})
    try:
        result = sql_generation_plugin(
            json.dumps({"linked_entities": {}, "schema_info": {"t": {}}}),
            "one",
            dsn="sqlite:///unused.db",
            sql_generator=Generator(),
        )
    finally:
        reset_tool_runtime_context(token)

    assert result == {"sql_query": "SELECT 1"}


@pytest.mark.parametrize(
    "query_response",
    [
        {"ids": [[]]},
        {"ids": "malformed"},
    ],
)
def test_existing_examples_never_map_empty_or_malformed_search_to_empty(
    monkeypatch,
    tmp_path,
    query_response,
):
    _, collection = _semantic_memory_manager(monkeypatch, tmp_path)
    repository = SuccessfulSqlMemoryRepository()
    receipt = repository.save(
        namespace_version_key=NAMESPACE,
        user_query="show orders",
        sql="SELECT * FROM orders",
        dialect="sqlite",
        run_id="run-1",
        approved=True,
        executed=True,
        execution_success=True,
        audited=True,
    )
    assert receipt.status is SemanticIndexStatus.INDEXED
    collection.query = lambda **_kwargs: query_response

    result = repository.retrieve(
        namespace_version_key=NAMESPACE,
        user_query="orders",
    )

    assert result.status == "DEGRADED"
    assert result.examples == ()
    assert result.context_json == "[]"
    assert result.error_code == "SEMANTIC_SEARCH_INCOMPLETE"


def _insert_raw_agent_memory_row(manager, *, step, data_text):
    conn = manager.db_handler._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO agent_memory
                (session_id, agent_name, step, instance_step, run_id, data, valid_from, valid_to, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                NAMESPACE,
                SUCCESSFUL_SQL_AGENT,
                step,
                None,
                None,
                data_text,
                "2026-07-12T00:00:00+00:00",
                "2026-07-12T00:00:00+00:00",
                "2026-07-12T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_retrieve_skips_malformed_row_missing_required_field(monkeypatch, tmp_path):
    # DoS regression: a single malformed row (missing a required field) must not
    # crash retrieval for the whole namespace; it should be logged and skipped.
    manager, collection = _semantic_memory_manager(monkeypatch, tmp_path)
    repository = SuccessfulSqlMemoryRepository()

    valid = repository.save(
        namespace_version_key=NAMESPACE,
        user_query="show orders",
        sql="SELECT * FROM orders",
        dialect="sqlite",
        run_id="run-1",
        approved=True,
        executed=True,
        execution_success=True,
        audited=True,
    )
    assert valid.status is SemanticIndexStatus.INDEXED

    malformed_id = "text2sql-example-v1-" + "0" * 64
    malformed_payload = {
        "serialization_version": 1,
        "example_id": malformed_id,
        "semantic_id": malformed_id,
        "namespace_version_key": NAMESPACE,
        "user_query": "broken row",
        "sql": "SELECT 2",
        "dialect": "sqlite",
        # "run_id" intentionally omitted -> malformed per SuccessfulSqlExample.from_payload
        "saved_at": "2026-07-12T00:00:00+00:00",
        "approved": True,
        "executed": True,
        "execution_success": True,
        "audited": True,
        "cache_kind": SUCCESSFUL_SQL_CACHE_KIND,
        "cache_source": "text_to_sql_terminal",
        "schema_version": NAMESPACE,
    }
    _insert_raw_agent_memory_row(
        manager,
        step=1,
        data_text=json.dumps(
            malformed_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )

    result = repository.retrieve(
        namespace_version_key=NAMESPACE,
        user_query="orders",
    )

    assert result.status == "READY"
    example_ids = [example.example_id for example in result.examples]
    assert malformed_id not in example_ids
    assert valid.semantic_id in example_ids


def test_retrieve_skips_non_json_data_blob(monkeypatch, tmp_path):
    # DoS regression: a row whose data column is not valid JSON must not crash
    # retrieval; it should be logged and skipped in _load_active_payloads.
    manager, collection = _semantic_memory_manager(monkeypatch, tmp_path)
    repository = SuccessfulSqlMemoryRepository()

    valid = repository.save(
        namespace_version_key=NAMESPACE,
        user_query="show orders",
        sql="SELECT * FROM orders",
        dialect="sqlite",
        run_id="run-1",
        approved=True,
        executed=True,
        execution_success=True,
        audited=True,
    )
    assert valid.status is SemanticIndexStatus.INDEXED

    _insert_raw_agent_memory_row(manager, step=1, data_text="{not valid json")

    result = repository.retrieve(
        namespace_version_key=NAMESPACE,
        user_query="orders",
    )

    assert result.status == "READY"
    example_ids = [example.example_id for example in result.examples]
    assert valid.semantic_id in example_ids


def test_retrieve_skips_row_with_non_string_field_type_error(monkeypatch, tmp_path):
    # SuccessfulSqlExample.from_payload() raises TypeError (not ValueError) when a
    # required field is present but has the wrong type (e.g. "dialect": 123, via
    # _validate -> _normalize_dialect -> _normalize_text). retrieve() must skip
    # that row rather than degrading the whole namespace.
    manager, collection = _semantic_memory_manager(monkeypatch, tmp_path)
    repository = SuccessfulSqlMemoryRepository()

    valid = repository.save(
        namespace_version_key=NAMESPACE,
        user_query="show orders",
        sql="SELECT * FROM orders",
        dialect="sqlite",
        run_id="run-1",
        approved=True,
        executed=True,
        execution_success=True,
        audited=True,
    )
    assert valid.status is SemanticIndexStatus.INDEXED

    malformed_id = "text2sql-example-v1-" + "1" * 64
    malformed_payload = {
        "serialization_version": 1,
        "example_id": malformed_id,
        "semantic_id": malformed_id,
        "namespace_version_key": NAMESPACE,
        "user_query": "broken row",
        "sql": "SELECT 2",
        "dialect": 123,
        "run_id": "run-2",
        "saved_at": "2026-07-12T00:00:00+00:00",
        "approved": True,
        "executed": True,
        "execution_success": True,
        "audited": True,
        "cache_kind": SUCCESSFUL_SQL_CACHE_KIND,
        "cache_source": "text_to_sql_terminal",
        "schema_version": NAMESPACE,
    }
    _insert_raw_agent_memory_row(
        manager,
        step=1,
        data_text=json.dumps(
            malformed_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )

    result = repository.retrieve(
        namespace_version_key=NAMESPACE,
        user_query="orders",
    )

    assert result.status == "READY"
    example_ids = [example.example_id for example in result.examples]
    assert malformed_id not in example_ids
    assert valid.semantic_id in example_ids
