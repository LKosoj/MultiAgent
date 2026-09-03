"""Regression coverage for shared schema memory in the Typed research path."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive.evidence import probe_result_to_evidence
from custom_tools.text_to_sql.adaptive.models import (
    BudgetState,
    ColumnRef,
    EvidenceCost,
    ExpectedResultShape,
    QuerySpec,
    QueryProbeRef,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, build_probe_result
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)
from custom_tools.text_to_sql.schema_loader import LoadedSchema
from custom_tools.text_to_sql.schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from workflow.deadline import DeadlineBudget
from workflow.text_to_sql_typed_runtime import (
    TextToSqlTypedAdmission,
    TextToSqlTypedRuntime,
    _ADMISSION_CAPABILITY,
)


def _loaded_schema() -> LoadedSchema:
    schema = {"public.revenue": {"columns": {"amount": {"type": "DECIMAL"}}}}
    scope = SchemaScope.from_mapping(
        {
            "serialization_version": 1,
            "tenant_id": "tenant",
            "access_scope_id": "owner:alice",
            "connection_view_id": "registry:revenue",
            "transient": False,
        }
    )
    return LoadedSchema(
        schema,
        SchemaNamespace(scope, canonical_schema_fingerprint(schema)),
        "live",
    )


def _query_spec() -> QuerySpec:
    return QuerySpec(
        run_id="run-memory",
        run_incarnation="incarnation-memory",
        revision=0,
        schema_namespace_version=None,
        query_id="query-memory",
        original_text="monthly revenue",
        semantic_items=(
            SemanticItem(
                source_id="item-memory",
                kind=SemanticItemKind.METRIC,
                source_text="monthly revenue",
                normalized_meaning="revenue",
                required=True,
                operator=None,
                literal_or_reference=None,
                status=SemanticItemStatus.UNRESOLVED,
                binding_ids=(),
            ),
        ),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )


def _runtime(
    loaded: LoadedSchema,
    *,
    context_documents: tuple[str, ...] = (),
) -> TextToSqlTypedRuntime:
    deadline = DeadlineBudget.from_duration(30)
    admission = TextToSqlTypedAdmission(
        run_id="run-memory",
        run_incarnation="incarnation-memory",
        deadline=deadline,
        query="monthly revenue",
        context_documents=context_documents,
        dsn="postgresql://svc:secret@db.example:5432/revenue",
        schema_scope=loaded.namespace.scope.to_mapping(),
        _capability=_ADMISSION_CAPABILITY,
    )
    return TextToSqlTypedRuntime(
        run_id=admission.run_id,
        run_incarnation=admission.run_incarnation,
        deadline=deadline,
        query=admission.query,
        context_documents=admission.context_documents,
        dsn=admission.dsn,
        schema_scope=admission.schema_scope,
        research_state_store=SimpleNamespace(save_query_spec=lambda _query: None),
        checkpoint_store=object(),
        budget_ledger=object(),
        solver_checkpoint_store=object(),
        _capability=_ADMISSION_CAPABILITY,
        _admission=admission,
    )


def _probe_fact_state(
    namespace: SchemaNamespace,
    *,
    kind: ResearchActionKind,
    truncated: bool = False,
) -> ResearchState:
    table = TableRef(namespace="public", schema=None, table="revenue")
    target = (
        QueryProbeRef(probe_id="probe-memory", namespace="public")
        if kind is ResearchActionKind.EXECUTE_PROBE
        else ColumnRef(table=table, column="amount")
    )
    parameters = (("limit", 1),)
    action = ResearchAction(
        action_id="action-memory",
        kind=kind,
        hypothesis_id=None,
        target=target,
        parameters=parameters,
        action_digest=canonical_action_digest(
            kind=kind,
            hypothesis_id=None,
            target=target,
            parameters=parameters,
            expected_revision=0,
        ),
        expected_revision=0,
    )
    payload = {"values": ["revenue"]}
    now = datetime(2026, 8, 13, tzinfo=UTC)
    cost = EvidenceCost(
        wall_clock_ms=1,
        model_calls=0,
        model_tokens=0,
        db_probe_ms=1,
        rows=1,
        bytes=len(canonical_json_bytes(payload)),
    )
    evidence = probe_result_to_evidence(
        build_probe_result(
            run_id="run-memory",
            run_incarnation="incarnation-memory",
            revision=0,
            schema_namespace_version="sha256:" + namespace.version_key,
            invocation_id="evidence-memory",
            action_digest=action.action_digest,
            probe_kind=kind,
            status=ProbeStatus.SUCCESS,
            target=target,
            started_at=now,
            completed_at=now,
            summary="verified probe",
            cost=cost,
            row_count=1,
            truncated=truncated,
            payload=payload,
        ),
        action,
    )
    assert evidence is not None
    budget = BudgetState(**{name: 10 if name.startswith(("initial_", "remaining_")) else 0 for name in (
        "initial_wall_clock_ms", "used_wall_clock_ms", "remaining_wall_clock_ms",
        "initial_model_calls", "used_model_calls", "remaining_model_calls",
        "initial_model_tokens", "used_model_tokens", "remaining_model_tokens",
        "initial_db_probe_ms", "used_db_probe_ms", "remaining_db_probe_ms",
        "initial_rows", "used_rows", "remaining_rows",
        "initial_bytes", "used_bytes", "remaining_bytes",
    )})
    query = _query_spec().model_copy(
        update={"schema_namespace_version": "sha256:" + namespace.version_key}
    )
    return ResearchState(
        run_id=query.run_id,
        run_incarnation=query.run_incarnation,
        revision=1,
        schema_namespace_version="sha256:" + namespace.version_key,
        query_spec=query,
        hypotheses=(),
        evidence=(evidence,),
        bindings=(),
        join_candidates=(),
        unresolved_items=("item-memory",),
        action_history=(action,),
        result_expectations=(),
        budget_state=budget,
        stop_reason=None,
    )
def test_typed_research_indexes_and_searches_loaded_schema_memory_by_namespace(
    monkeypatch,
) -> None:
    import workflow.text_to_sql_typed_research as typed_research

    loaded = _loaded_schema()
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "1")
    loaded.schema["public.revenue"]["columns"].update(
        {
            "RespGiven1": {"type": "TEXT"},
            "RespFamily1": {"type": "TEXT"},
            "RespGiven2": {"type": "TEXT"},
            "RespFamily2": {"type": "TEXT"},
            "RespGiven3": {"type": "TEXT"},
            "RespFamily3": {"type": "TEXT"},
        }
    )
    loaded = LoadedSchema(
        loaded.schema,
        SchemaNamespace(
            loaded.namespace.scope,
            canonical_schema_fingerprint(loaded.schema),
        ),
        loaded.source,
    )
    query_spec = _query_spec()
    context_documents = (
        "Return the winning alternative label.",
        "Responsible contacts use given and family values.",
    )
    calls: list[tuple[str, object]] = []
    captured: dict[str, object] = {}
    final_state = SimpleNamespace(
        schema_namespace_version="sha256:" + loaded.namespace.version_key
    )

    class MemoryManager:
        def restore_descriptions_from_memory(self, namespace, schema):
            calls.append(("restore", namespace, schema))
            schema["public.revenue"]["description"] = "Monthly revenue records"
            schema["public.revenue"]["columns"]["amount"]["description"] = (
                "Recorded revenue amount"
            )
            return True

        def ensure_schema_indexed_in_memory(self, namespace, schema):
            calls.append(("ensure", namespace, schema))

        def replace_file_semantic_facts(self, namespace, facts):
            calls.append(("save_semantic_facts", namespace, facts))
            return True

        def find_semantic_relevant_tables(self, terms, namespace=None):
            calls.append(("find", tuple(terms), namespace))
            return ["public.revenue"]

        def find_verified_probe_facts(self, terms, namespace):
            calls.append(("find_facts", tuple(terms), namespace))
            return [{"probe_kind": "sample_rows", "summary": "recent revenue rows"}]

        def find_approved_semantic_facts(self, terms, namespace):
            calls.append(("find_semantic_facts", tuple(terms), namespace))
            return []

        def save_verified_probe_facts(self, namespace, state):
            calls.append(("save_facts", namespace, state))

    class Loader:
        def __init__(self, _repo_root):
            self.file_manager = SimpleNamespace(
                load_scoped_snapshot=lambda _scope: {
                    "snapshot_version": 1,
                    "schema_scope": loaded.namespace.scope.to_mapping(),
                    "schema_fingerprint": loaded.namespace.schema_fingerprint,
                    "schema_info": {"public.revenue": {"columns": {}}},
                },
                save_scoped_snapshot=lambda scope, snapshot: calls.append(
                    ("save_snapshot", scope, snapshot)
                ),
            )

        def load_scoped_schema(self, *_args):
            return loaded

    class Enricher:
        def enrich_descriptions_with_llm(self, schema, *, dsn=None):
            calls.append(("enrich", schema, dsn))
            assert schema["public.revenue"]["description"] == "Monthly revenue records"
            assert (
                schema["public.revenue"]["columns"]["amount"]["description"]
                == "Recorded revenue amount"
            )

    async def run_research(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            final_state=final_state,
            stop_reason=SimpleNamespace(value="complete"),
            ambiguity=None,
        )

    monkeypatch.setattr(typed_research, "SchemaLoader", Loader)
    monkeypatch.setattr(typed_research, "SchemaEnricher", Enricher, raising=False)
    monkeypatch.setattr(
        typed_research,
        "SchemaMemoryManager",
        lambda _root: MemoryManager(),
    )
    understanding_calls: list[dict[str, object]] = []

    def understand(_self, *_args, **kwargs):
        understanding_calls.append(kwargs)
        return query_spec

    monkeypatch.setattr(typed_research.NLUProcessor, "_understand_query", understand)
    monkeypatch.setattr(
        typed_research,
        "load_adaptive_policy_config",
        lambda: SimpleNamespace(
            model_budget=SimpleNamespace(output_tokens_per_call=1)
        ),
    )
    monkeypatch.setattr(
        typed_research,
        "load_schema_research_agent_profile",
        lambda: SimpleNamespace(model="test-model"),
    )
    monkeypatch.setattr(typed_research, "_research_model", lambda *_args: object())
    monkeypatch.setattr(
        typed_research, "_research_stop_review_model", lambda *_args: object()
    )
    monkeypatch.setattr(typed_research, "run_production_schema_research", run_research)
    monkeypatch.setattr(
        typed_research,
        "live_terminal_document_freshness_context",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        typed_research,
        "research_stop_terminal_result",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        typed_research,
        "evaluate_research_generation_authority",
        lambda *_args: SimpleNamespace(allowed=True),
    )

    runtime = _runtime(loaded, context_documents=context_documents)
    result = asyncio.run(typed_research.run_typed_schema_research(runtime))

    assert calls == [
        ("restore", loaded.namespace, loaded.schema),
        ("enrich", loaded.schema, _runtime(loaded).dsn),
        (
            "save_snapshot",
            loaded.namespace.scope,
            {
                "snapshot_version": 1,
                "schema_scope": loaded.namespace.scope.to_mapping(),
                "schema_fingerprint": loaded.namespace.schema_fingerprint,
                "schema_info": loaded.schema,
            },
        ),
        ("ensure", loaded.namespace, loaded.schema),
        ("save_semantic_facts", loaded.namespace, ()),
        ("find", ("monthly revenue", "revenue"), loaded.namespace),
        ("find_facts", ("monthly revenue", "revenue"), loaded.namespace),
        ("find_semantic_facts", ("monthly revenue", "revenue"), loaded.namespace),
        ("save_facts", loaded.namespace, final_state),
    ]
    assert captured["semantic_table_hints"] == ("public.revenue",)
    assert captured["verified_probe_fact_hints"] == (
        {"probe_kind": "sample_rows", "summary": "recent revenue rows"},
    )
    assert captured["approved_semantic_fact_hints"] == ()
    assert captured["stop_review_model"] is not None
    assert runtime.loaded_schema_digest == canonical_digest(
        {
            "namespace_version": loaded.namespace.version_key,
            "schema": loaded.schema,
            "source": loaded.source,
        }
    )
    assert len(understanding_calls) == 1
    assert understanding_calls[0]["run_id"] == "run-memory"
    assert understanding_calls[0]["run_incarnation"] == "incarnation-memory"
    assert understanding_calls[0]["context_documents"] == context_documents
    schema_context = understanding_calls[0]["schema_context"]
    assert "amount:DECIMAL 'Recorded revenue amount'" in schema_context
    for column in (
        "RespGiven1",
        "RespFamily1",
        "RespGiven2",
        "RespFamily2",
        "RespGiven3",
        "RespFamily3",
    ):
        assert f"{column}:TEXT" in schema_context
    assert result["ready_for_sql"] is True


def test_verified_probe_fact_extractor_accepts_only_complete_typed_data_probes() -> None:
    from custom_tools.text_to_sql.schema_memory_sqlite import _verified_probe_facts

    namespace = _loaded_schema().namespace

    accepted = _verified_probe_facts(
        namespace,
        _probe_fact_state(namespace, kind=ResearchActionKind.SEARCH_VALUE),
    )

    assert accepted == [
        {
            "probe_kind": "search_value",
            "target": {
                "table": {"namespace": "public", "schema": None, "table": "revenue"},
                "column": "amount",
            },
            "parameters": [["limit", 1]],
            "payload": {"values": ["revenue"]},
        }
    ]
    assert _verified_probe_facts(
        namespace,
        _probe_fact_state(
            namespace,
            kind=ResearchActionKind.SEARCH_VALUE,
            truncated=True,
        ),
    ) == []
    assert _verified_probe_facts(
        namespace,
        _probe_fact_state(namespace, kind=ResearchActionKind.EXECUTE_PROBE),
    ) == []


def test_probe_fact_manager_uses_exact_namespace_and_skips_existing_fact(
    monkeypatch,
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.schema_memory_sqlite import (
        SchemaMemoryManager,
        _probe_fact_key,
        _verified_probe_facts,
    )
    from memory import tools as memory_tools

    namespace = _loaded_schema().namespace
    state = _probe_fact_state(namespace, kind=ResearchActionKind.SEARCH_VALUE)
    fact = _verified_probe_facts(namespace, state)[0]
    calls: list[dict[str, object]] = []

    def get_memory(**kwargs):
        calls.append(kwargs)
        return [
            {"data": {"schema_version": namespace.version_key, "cache_key": _probe_fact_key(fact), "fact": fact}},
            {"data": {"schema_version": "other", "cache_key": _probe_fact_key(fact), "fact": fact}},
        ]

    monkeypatch.setattr(memory_tools, "get_memory", get_memory)
    monkeypatch.setattr(
        memory_tools,
        "save_memory",
        lambda **_kwargs: pytest.fail("existing fact must not be saved twice"),
    )
    manager = SchemaMemoryManager(Path(tmp_path))

    assert manager.find_verified_probe_facts(("revenue",), namespace) == [fact]
    manager.save_verified_probe_facts(namespace, state)
    assert all(call["session_id"] == namespace.version_key for call in calls)
    assert all(call["cache_kind"] == "schema_probe_fact" for call in calls)


def test_verified_column_value_probe_is_promoted_to_an_approved_semantic_fact(
    monkeypatch,
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.schema_memory import SchemaMemoryManager
    from memory import tools as memory_tools

    namespace = _loaded_schema().namespace
    state = _probe_fact_state(namespace, kind=ResearchActionKind.SEARCH_VALUE)
    saved: list[dict[str, object]] = []

    def get_memory(**kwargs):
        return [
            {"data": data}
            for data in saved
            if data["schema_version"] == kwargs["session_id"]
            and data["cache_kind"] == kwargs["cache_kind"]
        ]

    monkeypatch.setattr(memory_tools, "get_memory", get_memory)
    monkeypatch.setattr(
        memory_tools,
        "save_memory",
        lambda **kwargs: saved.append(kwargs["data"]) or 1,
    )
    manager = SchemaMemoryManager(Path(tmp_path))

    manager.save_verified_probe_facts(namespace, state)

    facts = manager.find_approved_semantic_facts(("revenue",), namespace)
    assert [(fact.source, fact.fact_kind, fact.value) for fact in facts] == [
        ("typed_probe", "example", "revenue"),
    ]


def test_probe_promotion_ignores_non_finite_example_values() -> None:
    from custom_tools.text_to_sql.schema_memory_sqlite import (
        _approved_semantic_facts_from_probe_fact,
    )

    facts = _approved_semantic_facts_from_probe_fact(
        {
            "probe_kind": "distinct_values",
            "target": {
                "table": {"namespace": "public", "schema": None, "table": "revenue"},
                "column": "amount",
            },
            "payload": {"values": [1.0, float("nan"), float("inf"), -float("inf")]},
        }
    )

    assert [fact.value for fact in facts] == [1.0]


def test_non_promotable_probe_does_not_replace_file_semantic_facts(
    monkeypatch,
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.schema_memory import SemanticFact, SchemaMemoryManager
    from memory import tools as memory_tools

    namespace = _loaded_schema().namespace
    saved: list[dict[str, object]] = []

    def get_memory(**kwargs):
        return [
            {"data": data}
            for data in saved
            if data["schema_version"] == kwargs["session_id"]
            and data["cache_kind"] == kwargs["cache_kind"]
        ]

    monkeypatch.setattr(memory_tools, "get_memory", get_memory)
    monkeypatch.setattr(
        memory_tools,
        "save_memory",
        lambda **kwargs: saved.append(kwargs["data"]) or 1,
    )
    manager = SchemaMemoryManager(Path(tmp_path))
    file_fact = SemanticFact(
        subject="column",
        table_fqn="public.revenue",
        column="amount",
        fact_kind="description",
        value="Net invoice amount",
        source="file",
        status="approved",
    )

    manager.replace_file_semantic_facts(namespace, (file_fact,))
    manager.save_verified_probe_facts(
        namespace,
        _probe_fact_state(namespace, kind=ResearchActionKind.PROFILE_COLUMN),
    )

    assert manager.find_approved_semantic_facts(("invoice",), namespace) == [
        file_fact
    ]
