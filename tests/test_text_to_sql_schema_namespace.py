from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.fastapi_app.agui.auth import Principal
from backend.fastapi_app.agui.connection_registry import (
    ConnectionAccessError,
    ConnectionRegistry,
    ConnectionTargetPolicy,
)
from custom_tools.text_to_sql.schema_cache import SchemaCacheManager
from custom_tools.text_to_sql.schema_filtering import SchemaContextBuilder
from custom_tools.text_to_sql.schema_linking.heuristic_linker import HeuristicLinker
from custom_tools.text_to_sql.schema_loader import SchemaLoader
from custom_tools.text_to_sql.schema_memory import SchemaMemoryManager
from custom_tools.text_to_sql.schema_namespace import (
    SchemaFreshnessUnavailable,
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from workflow.process_supervisor import validate_work_spec


def _scope(*, transient: bool = False, connection_view_id: str = "registry:db-1") -> SchemaScope:
    return SchemaScope.from_mapping(
        {
            "serialization_version": 1,
            "tenant_id": "tenant-a",
            "access_scope_id": "owner:alice",
            "connection_view_id": connection_view_id,
            "transient": transient,
        }
    )


def _schema(*, amount_type: str = "DECIMAL") -> dict[str, object]:
    return {
        "public.orders": {
            "description": "Orders",
            "columns": {
                "id": {
                    "type": "INTEGER",
                    "not_null": "True",
                    "default_value": "",
                    "constraint_type": "PK",
                    "references": "",
                },
                "amount": {
                    "type": amount_type,
                    "not_null": "False",
                    "default_value": "0",
                    "constraint_type": "",
                    "references": "",
                },
            },
        }
    }


def _namespace(scope: SchemaScope | None = None) -> SchemaNamespace:
    return SchemaNamespace(
        scope=scope or _scope(),
        schema_fingerprint=canonical_schema_fingerprint(_schema()),
    )


def test_resolve_with_record_returns_the_authorized_immutable_record_and_dsn() -> None:
    registry = ConnectionRegistry(
        ConnectionTargetPolicy(
            allowed_schemes={"postgresql"},
            allowed_network_targets={"db.example:5432"},
        )
    )
    admin = Principal("root", "ops", frozenset({"admin"}))
    alice = Principal("alice", "tenant-a", frozenset({"user"}))
    bob = Principal("bob", "tenant-a", frozenset({"user"}))
    dsn = "postgresql://alice:secret@db.example:5432/app"
    record = registry.register(
        admin,
        display_name="Production",
        dsn=dsn,
        owner_subject="alice",
        tenant_id="tenant-a",
    )

    resolved_record, resolved_dsn = registry.resolve_with_record(
        record.connection_ref,
        alice,
    )

    assert resolved_record is record
    assert resolved_dsn == dsn
    assert registry.resolve(record.connection_ref, alice) == dsn
    with pytest.raises(ConnectionAccessError):
        registry.resolve_with_record(record.connection_ref, bob)


def test_strict_work_spec_preserves_private_schema_scope_for_child_parameters() -> None:
    scope = _scope().to_mapping()
    work_spec = {
        "spec_version": 1,
        "workflow_path": "/tmp/text_to_sql_pipeline.yaml",
        "parameters": {"query": "show users", "schema_scope": scope},
        "session_id": "session-1",
        "client_id": "owner:tenant-a:alice",
        "use_enhanced": True,
        "enable_telemetry": False,
        "run_incarnation": "incarnation-1",
        "deadline_at_ms": 2_000_000_000_000,
    }

    validated = validate_work_spec(work_spec)

    assert validated["parameters"]["schema_scope"] == scope


def test_scope_is_strict_immutable_and_keys_isolate_authorization_view() -> None:
    scope = _scope()

    assert scope.to_mapping() == {
        "serialization_version": 1,
        "tenant_id": "tenant-a",
        "access_scope_id": "owner:alice",
        "connection_view_id": "registry:db-1",
        "transient": False,
    }
    assert len(scope.scope_key) == 64
    assert scope.scope_key != _scope(connection_view_id="registry:db-2").scope_key

    with pytest.raises(ValueError, match="unexpected fields"):
        SchemaScope.from_mapping({**scope.to_mapping(), "effective_role": "invented"})
    with pytest.raises(TypeError, match="transient"):
        SchemaScope.from_mapping({**scope.to_mapping(), "transient": 1})
    with pytest.raises(Exception):
        scope.tenant_id = "tenant-b"  # type: ignore[misc]


def test_full_fingerprint_is_order_stable_and_changes_with_structural_metadata() -> None:
    schema = _schema()
    reordered = {
        "public.orders": {
            "columns": {
                "amount": schema["public.orders"]["columns"]["amount"],  # type: ignore[index]
                "id": schema["public.orders"]["columns"]["id"],  # type: ignore[index]
            },
            "description": "Different non-structural comment",
        }
    }

    first = canonical_schema_fingerprint(schema)
    assert first == canonical_schema_fingerprint(reordered)
    assert first != canonical_schema_fingerprint(_schema(amount_type="BIGINT"))
    assert _namespace().version_key != SchemaNamespace(
        scope=_scope(),
        schema_fingerprint=canonical_schema_fingerprint(_schema(amount_type="BIGINT")),
    ).version_key


def test_scoped_loader_live_validates_before_reusing_or_replacing_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = SchemaLoader(tmp_path)
    scope = _scope()
    live = _schema()
    calls: list[str] = []
    monkeypatch.setattr(
        loader,
        "_introspect_via_plugin",
        lambda dsn, *, autosave=True: calls.append(dsn) or live,
    )

    first = loader.load_scoped_schema({}, "postgresql://first", scope)
    snapshot_path = tmp_path / "sqlrag" / f"schema-v1-{scope.scope_key}.json"
    assert first.source == "live"
    assert snapshot_path.exists()

    second = loader.load_scoped_schema({}, "postgresql://rotated-secret", scope)
    assert second.source == "validated_snapshot"
    assert second.namespace == first.namespace
    assert calls == ["postgresql://first", "postgresql://rotated-secret"]

    stored = json.loads(snapshot_path.read_text(encoding="utf-8"))
    stored["schema_info"] = _schema(amount_type="TEXT")
    snapshot_path.write_text(json.dumps(stored), encoding="utf-8")

    replaced = loader.load_scoped_schema({}, "postgresql://rotated-secret", scope)
    assert replaced.source == "live"
    assert replaced.schema == live
    assert json.loads(snapshot_path.read_text(encoding="utf-8"))["schema_info"] == live


def test_transient_scope_is_live_only_and_freshness_failure_never_uses_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = SchemaLoader(tmp_path)
    persistent = _scope()
    transient = _scope(transient=True, connection_view_id="compatibility-run:run-1")
    monkeypatch.setattr(
        loader,
        "_introspect_via_plugin",
        lambda dsn, *, autosave=True: _schema(),
    )
    loader.load_scoped_schema({}, "sqlite:///live.db", persistent)
    loaded = loader.load_scoped_schema({}, "sqlite:///live.db", transient)

    assert loaded.source == "live"
    assert not (tmp_path / "sqlrag" / f"schema-v1-{transient.scope_key}.json").exists()

    def fail_live(dsn: str, *, autosave: bool = True) -> dict[str, object]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(loader, "_introspect_via_plugin", fail_live)
    with pytest.raises(SchemaFreshnessUnavailable, match="live schema introspection"):
        loader.load_scoped_schema({}, "sqlite:///live.db", persistent)


def test_scoped_memory_uses_namespace_identity_and_schema_content_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    namespace = _namespace()
    saved: list[dict[str, object]] = []

    def save_memory(**kwargs: object) -> int:
        saved.append(kwargs)
        return 1

    memory_tools = SimpleNamespace(
        save_memory=save_memory,
        get_memory=lambda **kwargs: [
            {"data": item["data"]} for item in saved
        ],
        memory_requester_context=lambda _name: __import__("contextlib").nullcontext(),
    )
    monkeypatch.setitem(sys.modules, "memory.tools", memory_tools)
    monkeypatch.setattr(
        "memory.index_consistency.reconcile_tactical_namespace",
        lambda **_kwargs: SimpleNamespace(
            expected_ids=tuple(
                item["data"]["semantic_id"] for item in saved  # type: ignore[index]
            ),
            indexed_ids=tuple(
                item["data"]["semantic_id"] for item in saved  # type: ignore[index]
            ),
            missing_ids=(),
            unexpected_ids=(),
            failed_ids=(),
            exact=True,
        ),
    )
    monkeypatch.setattr(
        "memory.index_consistency.source_namespace_semantic_ids",
        lambda **kwargs: (
            {
                item["data"]["semantic_id"]  # type: ignore[index]
                for item in saved
            },
            {
                item["data"]["semantic_id"]  # type: ignore[index]
                for item in saved
                if item["data"]["file_hash"]  # type: ignore[index]
                != kwargs["expected_file_hash"]
            },
        ),
    )

    manager = SchemaMemoryManager(tmp_path)
    assert manager.ensure_schema_indexed_in_memory(namespace, _schema()) is True
    assert saved[0]["session_id"] == namespace.version_key
    assert len(saved[0]["data"]["file_hash"]) == 32  # type: ignore[index]
    assert saved[0]["data"]["file_hash"] != namespace.version_key  # type: ignore[index]

    captured: dict[str, object] = {}
    collection = SimpleNamespace(metadata={"hnsw:space": "cosine"}, configuration=None)
    fake_manager = SimpleNamespace(
        get_tactical_collection=lambda: collection,
        search_semantic_with_scores=lambda *args, **kwargs: captured.update(kwargs)
        or {"ids": [], "distances": [], "metadatas": []},
    )
    import memory.manager as memory_manager_module

    monkeypatch.setattr(memory_manager_module, "memory_manager", fake_manager)
    manager.find_semantic_relevant_tables(["orders"], namespace)
    assert captured["where"] == {
        "$and": [
            {"session_id": namespace.version_key},
            {"cache_kind": "schema_table"},
        ]
    }


def test_scoped_cache_uses_namespace_and_does_not_derive_identity_from_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    monkeypatch.setattr(
        "custom_tools.text_to_sql.schema_cache.dsn_to_sanitized_name",
        lambda _dsn: pytest.fail("scoped cache must not derive a DSN key"),
    )

    info = SchemaCacheManager().prepare_cache_info(
        {"metrics": ["amount"]},
        _schema(),
        dsn="postgresql://alice:secret@example/orders",
        namespace=namespace,
    )

    assert info["session_id"] == namespace.version_key
    assert info["namespace_version_key"] == namespace.version_key
    assert info["schema_version"] == namespace.schema_fingerprint


def test_all_semantic_consumers_forward_the_same_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    seen: list[SchemaNamespace] = []

    class MemoryStub:
        last_search_status = "no_hits"
        last_search_error = None

        def find_semantic_relevant_tables(self, entities, namespace=None, dsn=None):
            seen.append(namespace)
            return []

    memory = MemoryStub()
    HeuristicLinker(memory).heuristic_linking(
        {"metrics": [], "dimensions": [], "filters": {}},
        _schema(),
        namespace=namespace,
    )
    SchemaContextBuilder(memory).build_relevant_schema_context(
        [{"name": "amount", "table": "public.orders", "column": "amount"}],
        [],
        {},
        [],
        _schema(),
        namespace=namespace,
    )

    from custom_tools.text_to_sql.schema_linking.llm_linker import LLMLinker

    linker = LLMLinker(
        schema_limiter=SimpleNamespace(build_schema_summary=lambda *args, **kwargs: ""),
        memory_manager=memory,
        entity_term_collector=lambda _entities: ["amount"],
        llm_caller=lambda *args, **kwargs: "{}",
    )
    linker.llm_linking(
        {"metrics": ["amount"], "dimensions": [], "filters": {}},
        _schema(),
        namespace=namespace,
    )

    assert seen == [namespace, namespace, namespace]
