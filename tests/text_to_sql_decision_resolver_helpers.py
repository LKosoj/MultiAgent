"""Shared trusted fixtures for decision resolver tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from custom_tools.text_to_sql.adaptive.controller import NormalizedToolResult
from custom_tools.text_to_sql.adaptive.decision_resolver import (
    ResolvedResearchDecision,
    resolve_research_decision,
)
from custom_tools.text_to_sql.adaptive.evidence import probe_result_to_evidence
from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
from custom_tools.text_to_sql.adaptive.models import (
    BudgetState,
    EvidenceCost,
    ExpectedResultShape,
    Hypothesis,
    HypothesisStatus,
    QuerySpec,
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
from custom_tools.text_to_sql.adaptive.research_decision import ResearchDecisionV1
from custom_tools.text_to_sql.adaptive.schema_probes import SchemaEvidenceDocument
from custom_tools.text_to_sql.adaptive.serialization import canonical_json_bytes
from custom_tools.text_to_sql.adaptive.tool_registry import (
    AdaptiveResearchToolContext,
    AdaptiveResearchToolRegistry,
)
from custom_tools.text_to_sql.schema_loader import LoadedSchema
from custom_tools.text_to_sql.schema_namespace import (
    SCHEMA_NAMESPACE_SERIALIZATION_VERSION,
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from workflow.deadline import DeadlineBudget


RUN_ID = "run-1"
RUN_INCARNATION = "incarnation-1"
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

TOOL_ARGUMENTS = {
    "search_schema_catalog": {"query": "orders", "top_k": 3},
    "inspect_table": {"table": "public.orders"},
    "inspect_column": {"table": "public.orders", "column": "status"},
    "inspect_relationships": {
        "table": "public.orders",
        "top_k": 3,
        "depth": 2,
    },
    "profile_column": {"table": "public.orders", "column": "status"},
    "sample_rows": {
        "table": "public.orders",
        "columns": ("id", "status"),
        "limit": 3,
    },
    "search_value": {
        "table": "public.orders",
        "column": "status",
        "value": "open",
        "top_k": 3,
    },
    "get_distinct_values": {
        "table": "public.orders",
        "column": "status",
        "top_k": 3,
    },
    "execute_research_probe": {
        "sql": "SELECT id FROM public.orders ORDER BY id LIMIT 2",
        "parameters": (),
    },
    "read_schema_evidence": {"document_id": "schema-doc"},
}


def budget() -> BudgetState:
    return BudgetState(
        **{
            f"{stage}_{name}": value
            for name in (
                "wall_clock_ms",
                "model_calls",
                "model_tokens",
                "db_probe_ms",
                "rows",
                "bytes",
            )
            for stage, value in (("initial", 100), ("used", 0), ("remaining", 100))
        }
    )


def scope() -> SchemaScope:
    return SchemaScope(
        serialization_version=SCHEMA_NAMESPACE_SERIALIZATION_VERSION,
        tenant_id="tenant-1",
        access_scope_id="scope-1",
        connection_view_id="view-1",
        transient=False,
    )


def schema(
    tables: dict[str, object] | None = None,
) -> tuple[LoadedSchema, SchemaNamespace]:
    schema_value = tables or {
        "public.orders": {
            "columns": {
                "id": {"type": "INTEGER"},
                "status": {"type": "TEXT"},
            }
        },
        "public.customers": {"columns": {"id": {"type": "INTEGER"}}},
    }
    namespace = SchemaNamespace(
        scope=scope(),
        schema_fingerprint=canonical_schema_fingerprint(schema_value),
    )
    return LoadedSchema(schema_value, namespace, "test"), namespace


def schema_version(namespace: SchemaNamespace) -> str:
    return f"sha256:{namespace.version_key}"


def query(schema_namespace_version: str, *, required: bool = True) -> QuerySpec:
    return QuerySpec(
        run_id=RUN_ID,
        run_incarnation=RUN_INCARNATION,
        revision=0,
        schema_namespace_version=schema_namespace_version,
        query_id="query-1",
        original_text="orders",
        semantic_items=(
            SemanticItem(
                source_id="source-1",
                kind=SemanticItemKind.FILTER,
                source_text="orders",
                normalized_meaning="orders",
                required=required,
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


def make_state(
    namespace: SchemaNamespace,
    *,
    with_evidence: bool = False,
    required: bool = True,
    hypothesis: bool = False,
) -> ResearchState:
    version = schema_version(namespace)
    evidence = ()
    history = ()
    revision = 0
    hypotheses = ()
    if with_evidence:
        target = TableRef(namespace="main", schema="public", table="orders")
        digest = canonical_action_digest(
            kind=ResearchActionKind.INSPECT_TABLE,
            hypothesis_id=None,
            target=target,
            parameters=(),
            expected_revision=0,
        )
        producer = ResearchAction(
            action_id="prior-action",
            kind=ResearchActionKind.INSPECT_TABLE,
            hypothesis_id=None,
            target=target,
            parameters=(),
            action_digest=digest,
            expected_revision=0,
        )
        payload = canonical_json_bytes({"status": "matched"})
        result = build_probe_result(
            run_id=RUN_ID,
            run_incarnation=RUN_INCARNATION,
            revision=0,
            schema_namespace_version=version,
            invocation_id="evidence-1",
            action_digest=digest,
            probe_kind=ResearchActionKind.INSPECT_TABLE,
            status=ProbeStatus.SUCCESS,
            target=target,
            started_at=NOW,
            completed_at=NOW,
            summary="trusted prior observation",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=1,
                bytes=len(payload),
            ),
            row_count=1,
            payload={"status": "matched"},
        )
        record = probe_result_to_evidence(result, producer)
        assert record is not None
        evidence = (record,)
        history = (producer,)
        revision = 1
        if hypothesis:
            hypotheses = (
                Hypothesis(
                    hypothesis_id="hypothesis-1",
                    source_ids=("source-1",),
                    claim="orders are relevant",
                    candidate_targets=(target,),
                    status=HypothesisStatus.PROPOSED,
                    evidence_ids=("evidence-1",),
                ),
            )
    return ResearchState(
        run_id=RUN_ID,
        run_incarnation=RUN_INCARNATION,
        revision=revision,
        schema_namespace_version=version,
        query_spec=query(version, required=required),
        hypotheses=hypotheses,
        evidence=evidence,
        result_expectations=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=("source-1",) if required else (),
        action_history=history,
        budget_state=budget(),
        stop_reason=None,
    )


def freshness(current: ResearchState) -> FreshnessContext:
    return FreshnessContext(
        evaluated_at=NOW,
        run_id=current.run_id,
        run_incarnation=current.run_incarnation,
        schema_namespace_version=current.schema_namespace_version,
    )


class CountingAdapter:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.recover_calls = 0
        self.result: NormalizedToolResult | None = None

    def execute(self, _invocation) -> NormalizedToolResult:
        self.execute_calls += 1
        assert self.result is not None
        return self.result

    def recover(self, _invocation) -> NormalizedToolResult | None:
        self.recover_calls += 1
        return self.result


class CountingRegistry(AdaptiveResearchToolRegistry):
    def __init__(self, context: AdaptiveResearchToolContext) -> None:
        super().__init__(context)
        self.adapter = CountingAdapter()
        self.resolve_calls = 0

    def resolve(self, tool_name: str):
        assert tool_name in self.names
        self.resolve_calls += 1
        return self.adapter

    def owns_resolved_adapter(self, tool_name: str, adapter: object) -> bool:
        return tool_name in self.names and adapter is self.adapter

    def bind_owned_adapter_methods(self, tool_name: str, adapter: object):
        if not self.owns_resolved_adapter(tool_name, adapter):
            return None
        return self.adapter.execute, self.adapter.recover


def make_registry(namespace: SchemaNamespace) -> CountingRegistry:
    version = schema_version(namespace)
    document = SchemaEvidenceDocument(
        document_id="schema-doc",
        namespace="main",
        schema_namespace_version=version,
        source_version="doc-v1",
        title="Schema guide",
        content="Orders schema",
        target=None,
    )

    def fixed_monotonic() -> float:
        return 100.0

    def fixed_wall_time() -> float:
        return 1_700_000_000.0

    deadline = DeadlineBudget.from_duration(
        30,
        monotonic=fixed_monotonic,
        wall_time=fixed_wall_time,
    )
    loader = SimpleNamespace(load_scoped_schema=lambda *_args: None)
    schema_runtime = SimpleNamespace(
        loader=loader,
        dsn="sqlite://trusted",
        namespace=namespace,
        scope=namespace.scope,
        table_namespace="main",
        deadline=deadline,
        documents=(document,),
        get_plugin=lambda _dsn: SimpleNamespace(dialect="sqlite"),
        containment_probe=None,
        containment_sample_size=None,
        monotonic_ns=lambda: 100_000_000_000,
        utc_now=lambda: NOW,
    )
    data_runtime = SimpleNamespace(
        loader=loader,
        dsn="sqlite://trusted",
        namespace=namespace,
        scope=namespace.scope,
        table_namespace="main",
        deadline=deadline,
        query_templates=(),
        get_plugin=lambda _dsn: SimpleNamespace(dialect="sqlite"),
        monotonic_ns=lambda: 100_000_000_000,
        utc_now=lambda: NOW,
    )
    return CountingRegistry(
        AdaptiveResearchToolContext(
            schema_runtime=schema_runtime,
            data_runtime=data_runtime,
            budget_factory=lambda *_args: None,
        )
    )


def tool_decision(name: str, arguments: dict[str, object]) -> ResearchDecisionV1:
    return ResearchDecisionV1.model_validate(
        {
            "proposals": (),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {"tool_name": name, "arguments": arguments},
            },
        },
        strict=True,
    )


def stop_decision(reason: str) -> ResearchDecisionV1:
    stop: dict[str, object] = {
        "next_kind": "stop",
        "reason": reason,
        "source_ids": () if reason == "complete" else ("source-1",),
        "citation_evidence_ids": ("evidence-1",),
    }
    if reason == "ambiguous":
        stop["ambiguity"] = {
            "interpretations": (
                "The source may use the first meaning.",
                "The source may use the second meaning.",
            ),
            "citation_evidence_ids": ("evidence-1",),
            "missing_distinguishing_fact": "The source does not define the intended meaning.",
        }
    return ResearchDecisionV1.model_validate(
        {
            "proposals": (),
            "next": stop,
        },
        strict=True,
    )


def resolve(
    decision: ResearchDecisionV1,
    *,
    loaded: LoadedSchema | None = None,
    namespace: SchemaNamespace | None = None,
    state: ResearchState | None = None,
    registry: CountingRegistry | None = None,
) -> ResolvedResearchDecision:
    if loaded is None or namespace is None:
        loaded, namespace = schema()
    exact_state = state or make_state(namespace)
    exact_registry = registry or make_registry(namespace)
    return resolve_research_decision(
        exact_state,
        decision,
        loaded_schema=loaded,
        freshness_context=freshness(exact_state),
        registry=exact_registry,
    )


def resolved_stop(
    reason: str,
) -> tuple[ResolvedResearchDecision, CountingRegistry]:
    loaded, namespace = schema()
    current = make_state(namespace, with_evidence=True, required=False)
    tools = make_registry(namespace)
    return (
        resolve_research_decision(
            current,
            stop_decision(reason),
            loaded_schema=loaded,
            freshness_context=freshness(current),
            registry=tools,
        ),
        tools,
    )


def normalized_failure(resolved: ResolvedResearchDecision) -> NormalizedToolResult:
    invocation = resolved.invocation
    action = resolved.admission.action
    assert invocation is not None
    assert action is not None
    result = build_probe_result(
        run_id=invocation.run_id,
        run_incarnation=invocation.run_incarnation,
        revision=invocation.revision,
        schema_namespace_version=resolved.admission.state.schema_namespace_version,
        invocation_id=invocation.invocation_id,
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.FAILED,
        target=action.target,
        started_at=NOW,
        completed_at=NOW,
        summary="typed failure",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=0,
        ),
        row_count=0,
        failure_code="typed_failure",
    )
    return NormalizedToolResult("error", result.model_dump(mode="json", by_alias=True))
