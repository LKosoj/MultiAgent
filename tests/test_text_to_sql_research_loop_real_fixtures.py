"""Real-registry completion checks for the difficult adaptive fixtures."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import custom_tools.text_to_sql.adaptive.production_research as production_research
from custom_tools.text_to_sql.adaptive.freshness import (
    DocumentSourceAvailability,
    FreshnessContext,
)
from custom_tools.text_to_sql.adaptive.model_budget import ModelBudgetLimits
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    ColumnRef,
    DiscriminatorValueBinding,
    EvidenceCost,
    EvidenceRecord,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExpectedResultShape,
    Hypothesis,
    HypothesisStatus,
    JoinCandidateStatus,
    JoinCandidate,
    JoinEdge,
    JoinType,
    PhysicalColumnBinding,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    ResearchStopReason,
    PredicateOperator,
    ResultExpectation,
    ResultExpectationKind,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    TableRef,
    VerticalAttributeBinding,
)
from custom_tools.text_to_sql.adaptive.policy import (
    AdaptivePolicyConfig,
    OperationCountBudget,
    PerActionBudget,
    ResourceBudget,
    ResultVolumeBudget,
    WallClockBudget,
    initial_budget_state,
    canonical_action_digest,
)
from custom_tools.text_to_sql.adaptive.provenance import ProbeProvenance
from custom_tools.text_to_sql.adaptive.production_research import (
    _build_initial_research_state,
    _bounded_research_context as _bounded_production_research_context,
    assemble_production_research,
    stable_schema_research_model_identity,
)
from custom_tools.text_to_sql.adaptive.decision_resolver import (
    execute_resolved_research_decision,
    resolve_research_decision,
)
from custom_tools.text_to_sql.adaptive.research_decision import (
    parse_research_decision,
)
from custom_tools.text_to_sql.adaptive.research_loop import (
    _planned_action,
    _state_with_reconciled_model_budget,
    run_research_loop,
)
from custom_tools.text_to_sql.adaptive.schema_research_agent import (
    SchemaResearchDecisionAdapter,
    build_schema_research_prompt,
    load_schema_research_agent_profile,
)
from custom_tools.text_to_sql.adaptive.schema_probes import (
    SchemaEvidenceDocument,
    SchemaProbeBudgetRuntime,
    SchemaProbeRuntime,
)
from custom_tools.text_to_sql.adaptive.data_probes import DataProbeRuntime
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)
from custom_tools.text_to_sql.adaptive.tool_registry import (
    AdaptiveResearchToolContext,
    AdaptiveResearchToolRegistry,
    resolve_research_tool_claim,
)
from custom_tools.text_to_sql.schema_loader import LoadedSchema, SchemaLoader
from custom_tools.text_to_sql.schema_namespace import (
    SCHEMA_NAMESPACE_SERIALIZATION_VERSION,
    SchemaNamespace,
    SchemaScope,
)
from tests.fixtures.text_to_sql_adaptive.sqlite import create_sqlite_adaptive_fixture
from tool_runtime_context import (
    SupervisorExecutionEvidence,
    reset_tool_runtime_context,
    set_tool_runtime_context,
)
from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
from workflow.adaptive_state_store import (
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.deadline import DeadlineBudget


def _policy(actions: int) -> AdaptivePolicyConfig:
    return AdaptivePolicyConfig(
        policy_version=2,
        wall_clock=WallClockBudget(wall_clock_seconds=120),
        resource_limits=ResourceBudget(model_tokens=20_000, db_probe_ms=30_000),
        operation_counts=OperationCountBudget(
            actions=actions,
            model_decisions=actions,
            db_probes=actions,
        ),
        result_volume=ResultVolumeBudget(
            returned_rows=1_000,
            inline_bytes=200_000,
        ),
        per_action=PerActionBudget(sample_rows=20),
        model_budget=ModelBudgetLimits(
            model_calls=actions,
            input_tokens_per_call=500,
            output_tokens_per_call=500,
            total_tokens=20_000,
        ),
    )


def _scope() -> SchemaScope:
    return SchemaScope(
        serialization_version=SCHEMA_NAMESPACE_SERIALIZATION_VERSION,
        tenant_id="fixture-tenant",
        access_scope_id="fixture-scope",
        connection_view_id="fixture-view",
        transient=True,
    )


def _initial_state(
    fixture_id: str,
    schema_version: str,
    policy: AdaptivePolicyConfig,
) -> ResearchState:
    text = "gold members with membership level"
    item = SemanticItem(
        source_id="source-1",
        kind=SemanticItemKind.FILTER,
        source_text=text,
        normalized_meaning=text,
        required=True,
        operator=PredicateOperator.EQ,
        literal_or_reference="gold",
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    query = QuerySpec(
        run_id=f"real-{fixture_id.lower()}",
        run_incarnation="incarnation-1",
        revision=0,
        schema_namespace_version=schema_version,
        query_id="query-1",
        original_text=text,
        semantic_items=(item,),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )
    return ResearchState(
        run_id=query.run_id,
        run_incarnation=query.run_incarnation,
        revision=0,
        schema_namespace_version=schema_version,
        query_spec=query,
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=(item.source_id,),
        action_history=(),
        result_expectations=(),
        budget_state=initial_budget_state(policy),
        stop_reason=None,
    )


def test_initial_research_state_resolves_binding_free_positive_integer_limit() -> None:
    policy = _policy(1)
    limit = SemanticItem(
        source_id="limit-1",
        kind=SemanticItemKind.LIMIT,
        source_text="highest",
        normalized_meaning="limit",
        required=True,
        operator=None,
        literal_or_reference=1,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    query = QuerySpec(
        run_id="production-limit",
        run_incarnation="incarnation-1",
        revision=0,
        schema_namespace_version=None,
        query_id="query-limit",
        original_text="highest",
        semantic_items=(limit,),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )

    state = _build_initial_research_state(
        query,
        schema_namespace_version="sha256:" + "a" * 64,
        budget_state=initial_budget_state(policy),
    )

    assert state.query_spec.semantic_items[0].status is SemanticItemStatus.RESOLVED
    assert state.unresolved_items == ()


def test_initial_research_state_defers_binding_free_unknown_limit() -> None:
    policy = _policy(1)
    limit = SemanticItem(
        source_id="limit-1",
        kind=SemanticItemKind.LIMIT,
        source_text="highest",
        normalized_meaning="limit",
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    query = QuerySpec(
        run_id="production-unknown-limit",
        run_incarnation="incarnation-1",
        revision=0,
        schema_namespace_version=None,
        query_id="query-unknown-limit",
        original_text="highest",
        semantic_items=(limit,),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )

    state = _build_initial_research_state(
        query,
        schema_namespace_version="sha256:" + "a" * 64,
        budget_state=initial_budget_state(policy),
    )

    assert state.query_spec.semantic_items[0].status is SemanticItemStatus.UNRESOLVED
    assert state.unresolved_items == ()


def _bounded_research_context(schema: dict[str, object]):
    """Exercise the bounded W3-F3 contract without adding production wiring."""

    def build(
        state: ResearchState,
        _feedbacks: tuple[str, ...],
    ) -> str:
        return canonical_json_bytes(
            {
                "schema": schema,
                "state": state.model_dump(mode="json", by_alias=True),
            }
        ).decode("utf-8")

    return build


def _bounded_context_policy(maximum_bytes: int) -> AdaptivePolicyConfig:
    values = _policy(actions=1).model_dump(mode="python")
    values["result_volume"]["inline_bytes"] = maximum_bytes
    values["model_budget"]["input_tokens_per_call"] = 4_096
    values["model_budget"]["total_tokens"] = 4_596
    values["resource_limits"]["model_tokens"] = 4_596
    return AdaptivePolicyConfig.model_validate(values)


def _state_with_second_required_item(
    policy: AdaptivePolicyConfig,
    *,
    source_text: str,
) -> ResearchState:
    base = _initial_state("bounded-items", "sha256:" + "a" * 64, policy)
    original_text = f"{base.query_spec.original_text} {source_text}"
    second = SemanticItem(
        source_id="source-2",
        kind=SemanticItemKind.METRIC,
        source_text=source_text,
        normalized_meaning=source_text,
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    query = base.query_spec.model_copy(
        update={
            "original_text": original_text,
            "semantic_items": (*base.query_spec.semantic_items, second),
        }
    )
    return ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "query_spec": query,
            "unresolved_items": ("source-1", "source-2"),
        }
    )


def _context_payload(
    state: ResearchState,
    policy: AdaptivePolicyConfig,
) -> tuple[str, dict[str, object]]:
    context = _bounded_production_research_context(
        SimpleNamespace(schema={"orders": {"columns": ["id"]}}),
        state,
        policy,
        profile=load_schema_research_agent_profile(),
        task=state.query_spec.original_text,
        validation_feedback=(),
    )
    return context, json.loads(context)


def _context_evidence(state: ResearchState, evidence_id: str, observation: str) -> EvidenceRecord:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    return EvidenceRecord(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=0,
        schema_namespace_version=state.schema_namespace_version,
        evidence_id=evidence_id,
        source_kind=EvidenceSourceKind.SCHEMA,
        target=TableRef(namespace="main", schema=None, table="orders"),
        action_digest="sha256:" + "e" * 64,
        observation=observation,
        validity_scope=EvidenceValidityScope.SCHEMA_VERSION,
        data_snapshot_token=None,
        observed_at=now,
        strength=1.0,
        created_at=now,
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=0,
        ),
    )


def _inline_context_evidence(
    state: ResearchState,
    evidence_id: str,
    column: ColumnRef,
) -> EvidenceRecord:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    payload = {
        "column": column.model_dump(mode="json", by_alias=True),
        "status": "matched",
    }
    payload_bytes = canonical_json_bytes(payload)
    provenance = ProbeProvenance(
        provenance_version=1,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        invocation_id=evidence_id,
        action_digest="sha256:" + "e" * 64,
        probe_kind=ResearchActionKind.INSPECT_COLUMN,
        target=column,
        schema_namespace_version=state.schema_namespace_version,
        payload_digest=canonical_digest(payload),
        started_at=now,
        completed_at=now,
    )
    observation = canonical_json_bytes(
        {
            "artifact_reference": None,
            "byte_count": len(payload_bytes),
            "invocation_id": evidence_id,
            "observation_version": 1,
            "payload": payload,
            "payload_digest": provenance.payload_digest,
            "probe_kind": provenance.probe_kind,
            "provenance": provenance,
            "row_count": 1,
            "storage": "inline",
            "summary": "context evidence",
            "truncated": False,
        }
    ).decode("utf-8")
    return _context_evidence(state, evidence_id, observation).model_copy(
        update={"target": column}
    )


def _context_action(
    action_id: str,
    hypothesis_id: str | None,
    expected_revision: int,
) -> ResearchAction:
    target = TableRef(namespace="main", schema=None, table="orders")
    parameters = (("table", "orders"),)
    return ResearchAction(
        action_id=action_id,
        kind=ResearchActionKind.INSPECT_TABLE,
        hypothesis_id=hypothesis_id,
        target=target,
        parameters=parameters,
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_TABLE,
            hypothesis_id=hypothesis_id,
            target=target,
            parameters=parameters,
            expected_revision=expected_revision,
        ),
        expected_revision=expected_revision,
    )


def test_bounded_context_refreshes_exact_omissions_after_final_rollback(monkeypatch) -> None:
    broad_policy = _bounded_context_policy(100_000)
    state = _state_with_second_required_item(broad_policy, source_text="x" * 1_000)
    full_context, _ = _context_payload(state, broad_policy)
    policy = _bounded_context_policy(len(full_context.encode("utf-8")) - 1)
    calls = 0
    real_encode = production_research._encode_context

    def count_encode(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_encode(*args, **kwargs)

    monkeypatch.setattr(production_research, "_encode_context", count_encode)

    context, payload = _context_payload(state, policy)
    included = payload["state"]
    assert isinstance(included, dict)
    query_spec = included["query_spec"]
    assert isinstance(query_spec, dict)
    omitted = payload["omitted"]
    assert isinstance(omitted, dict)

    assert len(context.encode("utf-8")) <= policy.result_volume.inline_bytes
    assert [item["source_id"] for item in query_spec["semantic_items"]] == ["source-1"]
    assert omitted == {
        "semantic_items": len(state.query_spec.semantic_items)
        - len(query_spec["semantic_items"]),
    }
    assert calls == 3


def test_bounded_context_omits_result_expectation_without_its_source() -> None:
    broad_policy = _bounded_context_policy(100_000)
    state = _state_with_second_required_item(broad_policy, source_text="x" * 1_500)
    evidence = _context_evidence(state, "evidence-2", "orders.id exists")
    expectation = ResultExpectation(
        source_id="source-2",
        evidence_id=evidence.evidence_id,
        kind=ResultExpectationKind.FILTER_MATCH_ABSENT,
        column=ColumnRef(table=TableRef(namespace="main", schema=None, table="orders"), column="id"),
    )
    state = ResearchState.model_validate(
        {
            **state.model_dump(mode="python", round_trip=True),
            "evidence": (evidence,),
            "result_expectations": (expectation,),
        }
    )
    reference = ResearchState.model_validate(
        {
            **state.model_dump(mode="python", round_trip=True),
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (state.query_spec.semantic_items[0],)}
            ),
            "unresolved_items": ("source-1",),
            "result_expectations": (),
        }
    )
    reference_context, _ = _context_payload(reference, broad_policy)
    policy = _bounded_context_policy(len(reference_context.encode("utf-8")) + 2_000)

    _, payload = _context_payload(state, policy)
    included = payload["state"]

    assert [item["source_id"] for item in included["query_spec"]["semantic_items"]] == ["source-1"]
    assert [item["evidence_id"] for item in included["evidence"]] == [evidence.evidence_id]
    assert included["result_expectations"] == []
    assert payload["omitted"]["result_expectations"] == 1


def test_bounded_context_keeps_distinct_result_expectation_identities() -> None:
    policy = _bounded_context_policy(100_000)
    state = _initial_state("bounded-expectations", "sha256:" + "a" * 64, policy)
    evidence = _context_evidence(state, "evidence-1", "orders.id exists")
    expectations = tuple(
        ResultExpectation(
            source_id="source-1",
            evidence_id=evidence.evidence_id,
            kind=ResultExpectationKind.FILTER_MATCH_ABSENT,
            column=ColumnRef(
                table=TableRef(namespace=namespace, schema=schema, table="orders"),
                column="id",
            ),
        )
        for namespace, schema in (("namespace-a", "schema-a"), ("namespace-b", "schema-b"))
    )
    state = ResearchState.model_validate(
        {
            **state.model_dump(mode="python", round_trip=True),
            "evidence": (evidence,),
            "result_expectations": expectations,
        }
    )

    _, payload = _context_payload(state, policy)
    included = payload["state"]["result_expectations"]

    assert [
        (item["column"]["table"]["namespace"], item["column"]["table"]["schema"])
        for item in included
    ] == [("namespace-a", "schema-a"), ("namespace-b", "schema-b")]


def test_bounded_context_omits_source_with_binding_bundle_that_does_not_fit() -> None:
    policy = _bounded_context_policy(5_000)
    base = _initial_state("bounded-binding-bundle", "sha256:" + "a" * 64, policy)
    table = TableRef(namespace="main", schema=None, table="orders")
    column = ColumnRef(table=table, column="id")
    evidence = _context_evidence(base, "evidence-1", "x" * 20_000)
    binding = PhysicalColumnBinding(
        binding_id="binding-1",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    source = base.query_spec.semantic_items[0].model_copy(
        update={
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (binding.binding_id,),
        }
    )
    query = base.query_spec.model_copy(update={"semantic_items": (source,)})
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "query_spec": query,
            "evidence": (evidence,),
            "bindings": (binding,),
        }
    )

    _, payload = _context_payload(state, policy)
    included = payload["state"]

    assert [item["source_id"] for item in included["query_spec"]["semantic_items"]] == []
    assert included["bindings"] == []
    assert included["evidence"] == []
    assert payload["omitted"] == {
        "semantic_items": 1,
        "evidence": 1,
        "bindings": 1,
    }


def test_bounded_context_prioritizes_validated_join_over_supported_binding_bundle() -> None:
    policy = _bounded_context_policy(3_250)
    base = _initial_state("bounded-validated-join", "sha256:" + "a" * 64, policy)
    table = TableRef(namespace="main", schema=None, table="orders")
    column = ColumnRef(table=table, column="id")
    related_column = ColumnRef(
        table=TableRef(namespace="main", schema=None, table="customers"),
        column="order_id",
    )
    binding_evidence = _context_evidence(base, "evidence-binding", "x" * 500)
    join_evidence = _context_evidence(
        base,
        "evidence-join",
        "declared orders to customers relationship",
    )
    binding = PhysicalColumnBinding(
        binding_id="binding-1",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(binding_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="schema evidence",
        physical_column=column,
    )
    source = base.query_spec.semantic_items[0].model_copy(
        update={
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (binding.binding_id,),
        }
    )
    join = JoinCandidate(
        join_id="join-1",
        left=column,
        right=related_column,
        join_type=JoinType.INNER,
        path=(
            JoinEdge(left=column, right=related_column, join_type=JoinType.INNER),
        ),
        status=JoinCandidateStatus.VALIDATED,
        evidence_ids=(join_evidence.evidence_id,),
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (source,)}
            ),
            "evidence": (binding_evidence, join_evidence),
            "bindings": (binding,),
            "join_candidates": (join,),
            "unresolved_items": (),
        }
    )

    context, payload = _context_payload(state, policy)
    included = payload["state"]

    assert len(context.encode("utf-8")) <= policy.result_volume.inline_bytes
    assert [item["join_id"] for item in included["join_candidates"]] == ["join-1"]
    assert included["bindings"] == []
    assert [item["evidence_id"] for item in included["evidence"]] == [
        "evidence-join"
    ]
    assert payload["omitted"]["bindings"] == 1


def test_bounded_context_keeps_validated_join_when_its_evidence_exceeds_prompt_cap() -> None:
    policy = _bounded_context_policy(100_000)
    base = _initial_state("bounded-large-validated-join", "sha256:" + "a" * 64, policy)
    table = TableRef(namespace="main", schema=None, table="orders")
    column = ColumnRef(table=table, column="id")
    related_column = ColumnRef(
        table=TableRef(namespace="main", schema=None, table="customers"),
        column="order_id",
    )
    evidence = _context_evidence(base, "evidence-join", "x" * 40_000)
    join = JoinCandidate(
        join_id="join-1",
        left=column,
        right=related_column,
        join_type=JoinType.INNER,
        path=(
            JoinEdge(left=column, right=related_column, join_type=JoinType.INNER),
        ),
        status=JoinCandidateStatus.VALIDATED,
        evidence_ids=(evidence.evidence_id,),
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "evidence": (evidence,),
            "join_candidates": (join,),
        }
    )
    before = state.model_dump(mode="python", round_trip=True)

    context, payload = _context_payload(state, policy)
    included = payload["state"]
    prompt = build_schema_research_prompt(
        load_schema_research_agent_profile(),
        task=state.query_spec.original_text,
        research_context=context,
        validation_feedback=None,
    )

    assert len(prompt.encode("utf-8")) <= 32_768
    assert state.model_dump(mode="python", round_trip=True) == before
    assert included["evidence"] == []
    assert included["join_candidates"] == [
        {
            "join_id": "join-1",
            "left": column.model_dump(mode="json", by_alias=True),
            "right": related_column.model_dump(mode="json", by_alias=True),
            "join_type": "inner",
            "path": [
                JoinEdge(
                    left=column,
                    right=related_column,
                    join_type=JoinType.INNER,
                ).model_dump(mode="json", by_alias=True)
            ],
            "status": "validated",
            "evidence_ids": [evidence.evidence_id],
        }
    ]


def test_bounded_context_keeps_compact_inline_evidence_for_required_binding() -> None:
    policy = _bounded_context_policy(3_000)
    base = _initial_state("bounded-compact-evidence", "sha256:" + "a" * 64, policy)
    table = TableRef(namespace="main", schema=None, table="orders")
    column = ColumnRef(table=table, column="id")
    evidence = _inline_context_evidence(base, "evidence-1", column)
    binding = PhysicalColumnBinding(
        binding_id="binding-1",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    source = base.query_spec.semantic_items[0].model_copy(
        update={
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (binding.binding_id,),
        }
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "query_spec": base.query_spec.model_copy(update={"semantic_items": (source,)}),
            "evidence": (evidence,),
            "bindings": (binding,),
        }
    )

    context, payload = _context_payload(state, policy)
    included = payload["state"]

    assert len(context.encode("utf-8")) <= policy.result_volume.inline_bytes
    assert [item["source_id"] for item in included["query_spec"]["semantic_items"]] == [
        "source-1"
    ]
    assert [item["binding_id"] for item in included["bindings"]] == ["binding-1"]
    assert included["evidence"] == [
        {
            "evidence_id": "evidence-1",
            "source_kind": "schema",
            "target": column.model_dump(mode="json", by_alias=True),
            "probe_kind": "inspect_column",
            "payload": {
                "column": column.model_dump(mode="json", by_alias=True),
                "status": "matched",
            },
            "summary": "context evidence",
            "truncated": False,
        }
    ]


def test_bounded_context_keeps_old_exact_inspection_for_active_candidate() -> None:
    policy = _bounded_context_policy(3_250)
    base = _initial_state("bounded-old-inspection", "sha256:" + "a" * 64, policy)
    table = TableRef(namespace="main", schema=None, table="orders")
    column = ColumnRef(table=table, column="id")
    proposal_evidence = _context_evidence(
        base,
        "evidence-proposal",
        "candidate source evidence",
    )
    exact_inspection = _inline_context_evidence(
        base,
        "evidence-exact-inspection",
        column,
    ).model_copy(update={"observed_at": datetime(2026, 8, 9, tzinfo=UTC)})
    newer_noise = tuple(
        _inline_context_evidence(
            base,
            f"evidence-newer-{index}",
            ColumnRef(table=table, column=f"noise_{index}"),
        ).model_copy(
            update={"observed_at": datetime(2026, 8, 11, tzinfo=UTC) + timedelta(minutes=index)}
        )
        for index in range(12)
    )
    binding = PhysicalColumnBinding(
        binding_id="binding-1",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(proposal_evidence.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    source = base.query_spec.semantic_items[0].model_copy(
        update={
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (binding.binding_id,),
        }
    )
    inspection_action = ResearchAction(
        action_id="action-inspect-column",
        kind=ResearchActionKind.INSPECT_COLUMN,
        hypothesis_id=None,
        target=column,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_COLUMN,
            hypothesis_id=None,
            target=column,
            parameters=(),
            expected_revision=0,
        ),
        expected_revision=0,
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "revision": 1,
            "query_spec": base.query_spec.model_copy(
                update={"revision": 1, "semantic_items": (source,)}
            ),
            "evidence": (proposal_evidence, exact_inspection, *newer_noise),
            "bindings": (binding,),
            "action_history": (inspection_action,),
        }
    )

    _, payload = _context_payload(state, policy)
    included = payload["state"]
    included_evidence_ids = {item["evidence_id"] for item in included["evidence"]}

    assert [item["binding_id"] for item in included["bindings"]] == ["binding-1"]
    assert exact_inspection.evidence_id in included_evidence_ids
    assert any(item.evidence_id not in included_evidence_ids for item in newer_noise)


def test_bounded_context_keeps_all_required_r41_binding_decision_facts() -> None:
    """A production-shaped R41 state keeps every next-decision binding fact."""

    policy = _bounded_context_policy(16 * 1024)
    base = _initial_state("bounded-r41", "sha256:" + "a" * 64, policy)
    table = TableRef(namespace="main", schema=None, table="customers")
    column = ColumnRef(table=table, column="Currency")
    formula_id = "semantic:" + "a" * 64
    eur_id = "semantic:" + "b" * 64
    czk_id = "semantic:" + "c" * 64
    eur_binding_id = "binding:" + "d" * 64
    czk_binding_id = "binding:" + "e" * 64
    raw_evidence_id = "invocation:" + "f" * 64
    original_text = "EUR customer count divided by CZK customer count; pay in EUR; pay in CZK"
    formula_text = "EUR customer count divided by CZK customer count"
    eur_text = "pay in EUR"
    czk_text = "pay in CZK"
    formula = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_id": formula_id,
            "source_text": formula_text,
            "normalized_meaning": "count EUR / count CZK",
            "operator": None,
            "literal_or_reference": None,
        }
    )
    eur = base.query_spec.semantic_items[0].model_copy(
        update={
            "source_id": eur_id,
            "source_text": eur_text,
            "normalized_meaning": "currency equals EUR",
            "literal_or_reference": "EUR",
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (eur_binding_id,),
        }
    )
    czk = base.query_spec.semantic_items[0].model_copy(
        update={
            "source_id": czk_id,
            "source_text": czk_text,
            "normalized_meaning": "currency equals CZK",
            "literal_or_reference": "CZK",
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (czk_binding_id,),
        }
    )
    evidence = (_inline_context_evidence(base, raw_evidence_id, column),)
    bindings = tuple(
        DiscriminatorValueBinding(
            binding_id=binding_id,
            source_id=source_id,
            tables=(table,),
            columns=(column,),
            predicates=(
                {
                    "left": column,
                    "operator": PredicateOperator.EQ,
                    "right": value,
                },
            ),
            join_path=(),
            evidence_ids=(evidence[0].evidence_id,),
            confidence=0.0,
            status=BindingStatus.CANDIDATE,
            validator_rule=None,
            discriminator_column=column,
            discriminator_predicate={
                "left": column,
                "operator": PredicateOperator.EQ,
                "right": value,
            },
        )
        for binding_id, source_id, value in (
            (eur_binding_id, eur_id, "EUR"),
            (czk_binding_id, czk_id, "CZK"),
        )
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "query_spec": base.query_spec.model_copy(
                update={
                    "original_text": original_text,
                    "semantic_items": (formula, eur, czk),
                }
            ),
            "evidence": evidence,
            "bindings": bindings,
            "unresolved_items": (formula_id, eur_id, czk_id),
        }
    )
    schema = {
        "main.customers": {
            "description": "",
            "columns": {
                "CustomerID": {"type": "INTEGER", "description": ""},
                "Segment": {"type": "TEXT", "description": ""},
                "Currency": {"type": "TEXT", "description": ""},
            },
        },
        "main.gasstations": {"description": "", "columns": {"GasStationID": {"type": "INTEGER", "description": ""}, "ChainID": {"type": "INTEGER", "description": ""}, "Country": {"type": "TEXT", "description": ""}, "Segment": {"type": "TEXT", "description": ""}}},
        "main.products": {"description": "", "columns": {"ProductID": {"type": "INTEGER", "description": ""}, "Description": {"type": "TEXT", "description": ""}}},
        "main.transactions_1k": {"description": "", "columns": {"TransactionID": {"type": "INTEGER", "description": ""}, "Date": {"type": "DATE", "description": ""}, "Time": {"type": "TEXT", "description": ""}, "CustomerID": {"type": "INTEGER", "description": ""}, "CardID": {"type": "INTEGER", "description": ""}, "GasStationID": {"type": "INTEGER", "description": ""}, "ProductID": {"type": "INTEGER", "description": ""}, "Amount": {"type": "INTEGER", "description": ""}, "Price": {"type": "REAL", "description": ""}}},
        "main.yearmonth": {"description": "", "columns": {"CustomerID": {"type": "INTEGER", "description": ""}, "Date": {"type": "TEXT", "description": ""}, "Consumption": {"type": "REAL", "description": ""}}},
    }
    document = SchemaEvidenceDocument(
        document_id="context-1",
        namespace="main",
        schema_namespace_version=state.schema_namespace_version,
        source_version="v1",
        title="Context document 1",
        content="Business formula authority is available only after reading this document.",
        target=None,
    )

    for feedback in ((), ("INVALID_STOP",), ("UNRESOLVABLE_PREFLIGHT",)):
        context = _bounded_production_research_context(
            SimpleNamespace(schema=schema),
            state,
            policy,
            profile=load_schema_research_agent_profile(),
            task=state.query_spec.original_text,
            validation_feedback=feedback,
            documents=(document,),
        )
        included = json.loads(context)["state"]
        prompt = build_schema_research_prompt(
            load_schema_research_agent_profile(),
            task=state.query_spec.original_text,
            research_context=context,
            validation_feedback=feedback or None,
        )

        assert len(context.encode("utf-8")) <= policy.result_volume.inline_bytes
        assert len(prompt.encode("utf-8")) <= 32 * 1024
        assert {
            item["source_id"] for item in included["query_spec"]["semantic_items"]
        } == {formula_id, eur_id, czk_id}
        assert {item["binding_id"] for item in included["bindings"]} == {
            eur_binding_id,
            czk_binding_id,
        }
        included_evidence_ids = {item["evidence_id"] for item in included["evidence"]}
        assert included_evidence_ids == {raw_evidence_id}
        assert all(
            set(item["evidence_ids"]).issubset(included_evidence_ids)
            for item in included["bindings"]
        )
        assert all(
            set(item) == {
                "binding_id",
                "source_id",
                "kind",
                "evidence_ids",
                "status",
                "discriminator_column",
                "discriminator_predicate",
            }
            for item in included["bindings"]
        )


def test_bounded_context_omits_action_for_an_omitted_hypothesis_but_keeps_complete_action_index() -> None:
    broad_policy = _bounded_context_policy(100_000)
    state = _state_with_second_required_item(broad_policy, source_text="x" * 1_024)
    hypothesis = Hypothesis(
        hypothesis_id="hypothesis-2",
        source_ids=("source-2",),
        claim="source two identifies orders",
        candidate_targets=(TableRef(namespace="main", schema=None, table="orders"),),
        status=HypothesisStatus.TESTING,
        evidence_ids=(),
    )
    state = ResearchState.model_validate(
        {
            **state.model_dump(mode="python", round_trip=True),
            "revision": 2,
            "hypotheses": (hypothesis,),
            "action_history": (
                _context_action("action-hypothesis", hypothesis.hypothesis_id, 0),
                _context_action("action-neutral", None, 1),
            ),
        }
    )
    reference = ResearchState.model_validate(
        {
            **state.model_dump(mode="python", round_trip=True),
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (state.query_spec.semantic_items[0],)}
            ),
            "unresolved_items": ("source-1",),
            "revision": 1,
            "hypotheses": (),
            "action_history": (_context_action("action-neutral", None, 0),),
        }
    )
    reference_context, _ = _context_payload(reference, broad_policy)
    policy = _bounded_context_policy(len(reference_context.encode("utf-8")) + 2_000)

    _, payload = _context_payload(state, policy)

    assert [item["action_id"] for item in payload["state"]["action_history"]] == ["action-neutral"]
    assert payload["omitted"]["action_history"] == 1
    assert payload["completed_action_index"] == [
        {
            "kind": action.kind.value,
            "target": action.target.model_dump(mode="json", by_alias=True),
            "parameters": [list(item) for item in action.parameters],
            "action_digest": action.action_digest,
        }
        for action in sorted(state.action_history, key=lambda action: action.action_digest)
    ]


def test_bounded_context_keeps_unresolved_item_and_identifier_atomic() -> None:
    broad_policy = _bounded_context_policy(100_000)
    state = _initial_state("bounded-atomic", "sha256:" + "a" * 64, broad_policy)
    full_context, _ = _context_payload(state, broad_policy)
    policy = _bounded_context_policy(len(full_context.encode("utf-8")) - 1)

    context, payload = _context_payload(state, policy)
    included = payload["state"]
    assert isinstance(included, dict)
    query_spec = included["query_spec"]
    assert isinstance(query_spec, dict)

    assert len(context.encode("utf-8")) <= policy.result_volume.inline_bytes
    assert query_spec["semantic_items"] == []
    assert included["unresolved_items"] == []
    assert payload["omitted"]["semantic_items"] == 1


def test_bounded_context_hides_active_facts_without_all_source_items() -> None:
    broad_policy = _bounded_context_policy(100_000)
    state = _state_with_second_required_item(broad_policy, source_text="x" * 1_000)
    second = state.query_spec.semantic_items[1].model_copy(
        update={
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": ("binding-2",),
        }
    )
    query = state.query_spec.model_copy(
        update={"semantic_items": (state.query_spec.semantic_items[0], second)}
    )
    table = TableRef(namespace="main", schema=None, table="orders")
    column = ColumnRef(table=table, column="id")
    now = datetime(2026, 8, 10, tzinfo=UTC)
    evidence = EvidenceRecord(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=0,
        schema_namespace_version=state.schema_namespace_version,
        evidence_id="evidence-2",
        source_kind=EvidenceSourceKind.SCHEMA,
        target=table,
        action_digest="sha256:" + "2" * 64,
        observation="orders.id exists",
        validity_scope=EvidenceValidityScope.SCHEMA_VERSION,
        data_snapshot_token=None,
        observed_at=now,
        strength=1.0,
        created_at=now,
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=0,
        ),
    )
    binding = PhysicalColumnBinding(
        binding_id="binding-2",
        source_id="source-2",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    hypothesis = Hypothesis(
        hypothesis_id="hypothesis-1",
        source_ids=("source-1", "source-2"),
        claim="both items identify orders",
        candidate_targets=(table,),
        status=HypothesisStatus.TESTING,
        evidence_ids=(),
    )
    state = ResearchState.model_validate(
        {
            **state.model_dump(mode="python", round_trip=True),
            "query_spec": query,
            "hypotheses": (hypothesis,),
            "evidence": (evidence,),
            "bindings": (binding,),
        }
    )
    reference = ResearchState.model_validate(
        {
            **state.model_dump(mode="python", round_trip=True),
            "query_spec": query.model_copy(
                update={"semantic_items": (query.semantic_items[0],)}
            ),
            "hypotheses": (),
            "evidence": (),
            "bindings": (),
            "unresolved_items": ("source-1",),
        }
    )
    reference_context, _ = _context_payload(reference, broad_policy)
    policy = _bounded_context_policy(len(reference_context.encode("utf-8")) + 300)

    context, payload = _context_payload(state, policy)
    included = payload["state"]
    assert isinstance(included, dict)
    query_spec = included["query_spec"]
    assert isinstance(query_spec, dict)

    assert len(context.encode("utf-8")) <= policy.result_volume.inline_bytes
    assert [item["source_id"] for item in query_spec["semantic_items"]] == ["source-1"]
    assert included["hypotheses"] == []
    assert included["bindings"] == []


def test_bounded_context_is_deterministic_across_persisted_active_fact_order() -> None:
    policy = _bounded_context_policy(100_000)
    text = "alpha beta"
    table = TableRef(namespace="main", schema=None, table="orders")
    column = ColumnRef(table=table, column="id")
    items = (
        SemanticItem(
            source_id="source-a",
            kind=SemanticItemKind.DIMENSION,
            source_text="alpha",
            normalized_meaning="alpha",
            required=False,
            operator=None,
            literal_or_reference=None,
            status=SemanticItemStatus.PARTIALLY_RESOLVED,
            binding_ids=("binding-a",),
        ),
        SemanticItem(
            source_id="source-b",
            kind=SemanticItemKind.DIMENSION,
            source_text="beta",
            normalized_meaning="beta",
            required=False,
            operator=None,
            literal_or_reference=None,
            status=SemanticItemStatus.PARTIALLY_RESOLVED,
            binding_ids=("binding-b",),
        ),
    )
    query = QuerySpec(
        run_id="bounded-order-run",
        run_incarnation="bounded-order-incarnation",
        revision=0,
        schema_namespace_version="sha256:" + "a" * 64,
        query_id="bounded-order-query",
        original_text=text,
        semantic_items=items,
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )
    bindings = tuple(
        PhysicalColumnBinding(
            binding_id=f"binding-{suffix}",
            source_id=f"source-{suffix}",
            tables=(table,),
            columns=(column,),
            predicates=(),
            join_path=(),
            evidence_ids=(),
            confidence=1.0,
            status=BindingStatus.CANDIDATE,
            validator_rule=None,
            physical_column=column,
        )
        for suffix in ("a", "b")
    )
    hypotheses = tuple(
        Hypothesis(
            hypothesis_id=f"hypothesis-{suffix}",
            source_ids=(f"source-{suffix}",),
            claim=f"{suffix} identifies orders",
            candidate_targets=(table,),
            status=HypothesisStatus.TESTING,
            evidence_ids=(),
        )
        for suffix in ("a", "b")
    )
    common = {
        "run_id": query.run_id,
        "run_incarnation": query.run_incarnation,
        "revision": 0,
        "schema_namespace_version": query.schema_namespace_version,
        "query_spec": query,
        "evidence": (),
        "join_candidates": (),
        "unresolved_items": (),
        "action_history": (),
        "result_expectations": (),
        "budget_state": initial_budget_state(policy),
        "stop_reason": None,
    }
    first = ResearchState(hypotheses=hypotheses, bindings=bindings, **common)
    second = ResearchState(
        hypotheses=tuple(reversed(hypotheses)),
        bindings=tuple(reversed(bindings)),
        **common,
    )

    first_context, _ = _context_payload(first, policy)
    second_context, _ = _context_payload(second, policy)

    assert first_context == second_context


def test_bounded_production_context_stays_within_synthetic_envelope() -> None:
    policy_values = _policy(actions=1).model_dump(mode="python")
    policy_values["result_volume"]["inline_bytes"] = 1_500
    policy_values["model_budget"]["input_tokens_per_call"] = 4_096
    policy_values["model_budget"]["total_tokens"] = 4_596
    policy_values["resource_limits"]["model_tokens"] = 4_596
    policy = AdaptivePolicyConfig.model_validate(policy_values)
    scope = SchemaScope(
        serialization_version=SCHEMA_NAMESPACE_SERIALIZATION_VERSION,
        tenant_id="synthetic-tenant",
        access_scope_id="synthetic-access",
        connection_view_id="synthetic-view",
        transient=True,
    )
    namespace = SchemaNamespace(scope, "0" * 64)
    query = QuerySpec(
        run_id="synthetic-run",
        run_incarnation="synthetic-incarnation",
        revision=0,
        schema_namespace_version=f"sha256:{namespace.version_key}",
        query_id="synthetic-query",
        original_text="request",
        semantic_items=(),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )
    state = ResearchState(
        run_id=query.run_id,
        run_incarnation=query.run_incarnation,
        revision=0,
        schema_namespace_version=query.schema_namespace_version,
        query_spec=query,
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=(),
        action_history=(),
        result_expectations=(),
        budget_state=initial_budget_state(policy),
        stop_reason=None,
    )
    loaded_schema = LoadedSchema(
        {
            "a": {},
            "b": {"payload": "x" * 20_000},
        },
        namespace,
        "synthetic",
    )

    context = _bounded_production_research_context(
        loaded_schema,
        state,
        policy,
        profile=load_schema_research_agent_profile(),
        task=state.query_spec.original_text,
        validation_feedback=(),
    )

    assert len(context.encode("utf-8")) <= policy.result_volume.inline_bytes


def test_bounded_production_context_omits_requeryable_old_facts_without_mutating_state() -> None:
    policy_values = _policy(actions=1).model_dump(mode="python")
    policy_values["result_volume"]["inline_bytes"] = 16 * 1024
    policy_values["model_budget"]["input_tokens_per_call"] = 4_096
    policy = AdaptivePolicyConfig.model_validate(policy_values)
    base = _initial_state("bounded-view", "sha256:" + "a" * 64, policy)
    text = f"{base.query_spec.original_text} and total"
    source = SemanticItem(
        source_id="source-2",
        kind=SemanticItemKind.METRIC,
        source_text="total",
        normalized_meaning="total",
        required=False,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=("binding-1",),
    )
    query = base.query_spec.model_copy(
        update={"original_text": text, "semantic_items": (*base.query_spec.semantic_items, source)}
    )
    table = TableRef(namespace="main", schema=None, table="orders")
    column = ColumnRef(table=table, column="total")
    now = datetime(2026, 8, 10, tzinfo=UTC)
    evidence = tuple(
        EvidenceRecord(
            run_id=base.run_id,
            run_incarnation=base.run_incarnation,
            revision=0,
            schema_namespace_version=base.schema_namespace_version,
            evidence_id=f"evidence-{index:02d}",
            source_kind=EvidenceSourceKind.SCHEMA,
            target=table,
            action_digest="sha256:" + f"{index:064x}",
            observation=f"json-like {index}: " + '{"key":"\\\\"}' * 100,
            validity_scope=EvidenceValidityScope.SCHEMA_VERSION,
            data_snapshot_token=None,
            observed_at=now + timedelta(seconds=index),
            strength=1.0,
            created_at=now + timedelta(seconds=index),
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=0,
                bytes=0,
            ),
        )
        for index in range(24)
    )
    binding = PhysicalColumnBinding(
        binding_id="binding-1",
        source_id=source.source_id,
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence[-1].evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="schema evidence",
        physical_column=column,
    )
    hypothesis = Hypothesis(
        hypothesis_id="hypothesis-1",
        source_ids=(base.query_spec.semantic_items[0].source_id,),
        claim="orders contain totals",
        candidate_targets=(table,),
        status=HypothesisStatus.TESTING,
        evidence_ids=(evidence[-1].evidence_id,),
    )
    join = JoinCandidate(
        join_id="join-1",
        left=column,
        right=ColumnRef(table=table, column="id"),
        join_type=JoinType.INNER,
        path=(),
        status=JoinCandidateStatus.CANDIDATE,
        evidence_ids=(evidence[-1].evidence_id,),
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "query_spec": query,
            "evidence": evidence,
            "bindings": (binding,),
            "hypotheses": (hypothesis,),
            "join_candidates": (join,),
        }
    )
    before = canonical_json_bytes(state)
    assert len(before) > policy.result_volume.inline_bytes
    loaded = SimpleNamespace(schema={"orders": {"columns": ["id", "total"]}})

    context = _bounded_production_research_context(
        loaded,
        state,
        policy,
        profile=load_schema_research_agent_profile(),
        task=state.query_spec.original_text,
        validation_feedback=(),
    )
    prompt = build_schema_research_prompt(
        load_schema_research_agent_profile(),
        task=state.query_spec.original_text,
        research_context=context,
    )

    payload = json.loads(context)
    included = payload["state"]
    assert len(context.encode("utf-8")) <= 16 * 1024
    assert len(prompt.encode("utf-8")) <= 32 * 1024
    assert canonical_json_bytes(state) == before
    assert [item["source_id"] for item in included["query_spec"]["semantic_items"]][0] == "source-1"
    assert included["hypotheses"][0]["hypothesis_id"] == hypothesis.hypothesis_id
    assert included["bindings"][0]["binding_id"] == binding.binding_id
    assert included["join_candidates"][0]["join_id"] == join.join_id
    assert evidence[-1].evidence_id in {item["evidence_id"] for item in included["evidence"]}
    assert payload["requery_with_existing_probes"] is True
    assert payload["omitted"]["evidence"] > 0
    assert payload["omitted"]["evidence"] == len(evidence) - len(included["evidence"])
    assert {
        item["source_id"] for item in included["query_spec"]["semantic_items"]
    } >= {source.source_id, base.query_spec.semantic_items[0].source_id}
    assert evidence[0].evidence_id not in context
    claim = resolve_research_tool_claim(
        "inspect_table",
        {"table": "orders"},
        AdaptiveResearchToolContext(
            schema_runtime=SimpleNamespace(table_namespace="main", documents=()),
            data_runtime=object(),
            budget_factory=lambda *_args: object(),
        ),
    )
    assert claim.kind.value == "inspect_table"


class _VerticalFixtureModel:
    """Scripted decisions selected from trusted schema and observed probe payloads."""

    def __init__(self) -> None:
        self.prompts: list[dict[str, object]] = []

    async def __call__(self, prompt: str) -> str:
        return self._decide(prompt)

    def _decide(self, prompt: str) -> str:
        envelope = json.loads(prompt)
        self.prompts.append(envelope)
        raw_context = envelope["input"]["research_context"]
        assert isinstance(raw_context, str)
        context = json.loads(raw_context)
        schema = context["schema"]
        state = context["state"]
        assert isinstance(schema, dict)
        assert isinstance(state, dict)
        revision = state["revision"]
        fact_table = _fact_table(schema)
        if revision == 0:
            return _tool("inspect_table", {"table": fact_table})
        if revision == 1:
            return _tool(
                "get_distinct_values",
                {
                    "table": fact_table,
                    "column": _text_column(state, fact_table),
                    "top_k": 4,
                },
            )
        if revision == 2:
            return _tool(
                "inspect_relationships",
                {"table": fact_table, "top_k": 4, "depth": 1},
            )
        targets = _relationship_targets(state, fact_table)
        if revision == 3:
            return _tool("inspect_table", {"table": targets[0]})
        if revision == 4:
            return _tool(
                "get_distinct_values",
                {
                    "table": targets[0],
                    "column": _text_column(state, targets[0]),
                    "top_k": 4,
                },
            )
        if revision == 5:
            return _tool("inspect_table", {"table": targets[1]})
        if revision == 6:
            return _tool(
                "get_distinct_values",
                {
                    "table": targets[1],
                    "column": _text_column(state, targets[1]),
                    "top_k": 4,
                },
            )
        entity_table, catalog_table = _vertical_roles(state, fact_table, targets)
        fact_value = _text_column(state, fact_table)
        relations = _relationships(state, fact_table)
        if revision == 7:
            relationship_evidence = _evidence_id_for_kind(
                state, "inspect_relationships"
            )
            return _tool(
                "inspect_column",
                {"table": fact_table, "column": fact_value},
                proposals=tuple(
                    _new_join_proposal(
                        proposal_key=f"proposal:join-{index}",
                        relationship=relations[target],
                        citation_evidence_id=relationship_evidence,
                    )
                    for index, target in enumerate(targets)
                ),
            )
        if revision == 8:
            relationship_evidence = _evidence_id_for_kind(
                state, "inspect_relationships"
            )
            return _tool(
                "inspect_column",
                {
                    "table": fact_table,
                    "column": _relationship_key(relations[targets[0]], "from_column"),
                },
                proposals=tuple(
                    {
                        "proposal_type": "join_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "join_id": join["join_id"],
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": [relationship_evidence],
                    }
                    for join in state["join_candidates"]
                ),
            )
        if revision == 9:
            return _tool(
                "inspect_column",
                {
                    "table": fact_table,
                    "column": _relationship_key(relations[targets[1]], "from_column"),
                },
            )
        if revision == 10:
            return _tool(
                "inspect_column",
                {
                    "table": catalog_table,
                    "column": _relationship_key(relations[catalog_table], "to_column"),
                },
            )
        if revision == 11:
            return _tool(
                "inspect_column",
                {"table": catalog_table, "column": _text_column(state, catalog_table)},
            )
        if revision == 12:
            return _tool(
                "inspect_column",
                {
                    "table": entity_table,
                    "column": _relationship_key(relations[entity_table], "to_column"),
                },
            )
        if revision == 13:
            return _tool(
                "profile_column",
                {"table": fact_table, "column": fact_value},
                proposals=(
                    _vertical_binding_proposal(
                        state,
                        fact_table=fact_table,
                        entity_table=entity_table,
                        catalog_table=catalog_table,
                        relations=relations,
                        fact_value=fact_value,
                    ),
                ),
            )
        if revision == 14:
            return _tool(
                "sample_rows",
                {
                    "table": entity_table,
                    "columns": [
                        _relationship_key(relations[entity_table], "to_column")
                    ],
                    "limit": 1,
                },
                proposals=(
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": state["bindings"][0]["binding_id"],
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": _evidence_ids(state),
                    },
                ),
            )
        raise AssertionError(f"the loop should complete before revision {revision}")


def _fact_table(schema: dict[str, Any]) -> str:
    tables = [
        (name, body)
        for name, body in schema.items()
        if isinstance(name, str) and isinstance(body, dict)
    ]
    assert tables
    name, _ = max(
        sorted(tables),
        key=lambda item: len(item[1].get("columns", {})),
    )
    return _short_table(name)


def _payload(evidence: dict[str, Any]) -> dict[str, Any]:
    observation = json.loads(evidence["observation"])
    payload = observation["payload"]
    assert isinstance(payload, dict)
    return payload


def _evidence_for_kind(state: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        item
        for item in state["evidence"]
        if json.loads(item["observation"])["probe_kind"] == kind
    ]


def _evidence_id_for_kind(state: dict[str, Any], kind: str) -> str:
    matches = _evidence_for_kind(state, kind)
    assert len(matches) == 1
    return matches[0]["evidence_id"]


def _evidence_ids(state: dict[str, Any]) -> list[str]:
    return [item["evidence_id"] for item in state["evidence"]]


def _short_table(value: object) -> str:
    assert isinstance(value, str) and value
    return value.rsplit(".", 1)[-1]


def _payload_table(payload: dict[str, Any]) -> str:
    target = payload.get("target", payload.get("table"))
    assert isinstance(target, dict)
    table = target["table"] if isinstance(target.get("table"), dict) else target
    assert isinstance(table, dict)
    return _short_table(table["table"])


def _text_column(state: dict[str, Any], table: str) -> str:
    matches = [
        _payload(item)
        for item in _evidence_for_kind(state, "inspect_table")
        if _payload_table(_payload(item)) == table
    ]
    assert len(matches) == 1
    columns = matches[0]["columns"]
    assert isinstance(columns, list)
    text_columns = [
        item["name"]
        for item in columns
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item.get("type") == "TEXT"
    ]
    assert len(text_columns) == 1
    return text_columns[0]


def _relationships(state: dict[str, Any], fact_table: str) -> dict[str, dict[str, Any]]:
    payload = _payload(_evidence_for_kind(state, "inspect_relationships")[0])
    relationships = payload["relationships"]
    assert isinstance(relationships, list)
    values = {
        _short_table(item["to_table"]): item
        for item in relationships
        if isinstance(item, dict)
        and item.get("relationship_kind") == "declared"
        and _short_table(item.get("from_table")) == fact_table
    }
    assert len(values) == 2
    return values


def _relationship_targets(state: dict[str, Any], fact_table: str) -> tuple[str, str]:
    targets = tuple(sorted(_relationships(state, fact_table)))
    assert len(targets) == 2
    return targets


def _relationship_key(relationship: dict[str, Any], name: str) -> str:
    pairs = relationship["column_pairs"]
    assert isinstance(pairs, list) and len(pairs) == 1
    value = pairs[0][name]
    assert isinstance(value, str)
    return value


def _vertical_roles(
    state: dict[str, Any],
    fact_table: str,
    targets: tuple[str, str],
) -> tuple[str, str]:
    assert _distinct_values(state, fact_table, _text_column(state, fact_table)) >= {
        "gold"
    }
    catalog_candidates = [
        table
        for table in targets
        if "membership_level"
        in _distinct_values(state, table, _text_column(state, table))
    ]
    assert len(catalog_candidates) == 1
    catalog_table = catalog_candidates[0]
    entity_table = next(table for table in targets if table != catalog_table)
    return entity_table, catalog_table


def _distinct_values(state: dict[str, Any], table: str, column: str) -> set[str]:
    matches = [
        _payload(item)
        for item in _evidence_for_kind(state, "distinct_values")
        if _payload_table(_payload(item)) == table
        and _payload(item).get("columns") == [column]
    ]
    assert len(matches) == 1
    rows = matches[0]["rows"]
    assert isinstance(rows, list)
    return {
        row[0]
        for row in rows
        if isinstance(row, list) and len(row) == 1 and isinstance(row[0], str)
    }


def _new_join_proposal(
    *,
    proposal_key: str,
    relationship: dict[str, Any],
    citation_evidence_id: str,
) -> dict[str, object]:
    from_table = _short_table(relationship["from_table"])
    to_table = _short_table(relationship["to_table"])
    from_column = _relationship_key(relationship, "from_column")
    to_column = _relationship_key(relationship, "to_column")
    edge = {
        "left": {"table": from_table, "column": from_column},
        "right": {"table": to_table, "column": to_column},
        "join_type": "inner",
    }
    return {
        "proposal_type": "new_join",
        "proposal_key": proposal_key,
        "left": edge["left"],
        "right": edge["right"],
        "join_type": "inner",
        "path": [edge],
        "citation_evidence_ids": [citation_evidence_id],
    }


def _vertical_binding_proposal(
    state: dict[str, Any],
    *,
    fact_table: str,
    entity_table: str,
    catalog_table: str,
    relations: dict[str, dict[str, Any]],
    fact_value: str,
) -> dict[str, object]:
    return {
        "proposal_type": "new_binding",
        "proposal_key": "proposal:vertical-membership-level",
        "source_id": "source-1",
        "candidate": {
            "kind": "vertical_attribute",
            "entity_table": {"table": entity_table},
            "entity_key": {
                "table": entity_table,
                "column": _relationship_key(relations[entity_table], "to_column"),
            },
            "attribute_catalog_table": {"table": catalog_table},
            "attribute_catalog_key": {
                "table": catalog_table,
                "column": _relationship_key(relations[catalog_table], "to_column"),
            },
            "attribute_name_predicate": {
                "left": {
                    "table": catalog_table,
                    "column": _text_column(state, catalog_table),
                },
                "operator": "eq",
                "right": "membership_level",
            },
            "value_table": {"table": fact_table},
            "value_entity_key": {
                "table": fact_table,
                "column": _relationship_key(relations[entity_table], "from_column"),
            },
            "value_attribute_key": {
                "table": fact_table,
                "column": _relationship_key(relations[catalog_table], "from_column"),
            },
            "value_predicate": {
                "left": {"table": fact_table, "column": fact_value},
                "operator": "eq",
                "right": "gold",
            },
        },
        "join_references": [
            {"reference_kind": "existing", "join_id": item["join_id"]}
            for item in state["join_candidates"]
        ],
        "citation_evidence_ids": _evidence_ids(state),
    }


def _tool(
    tool_name: str,
    arguments: dict[str, object],
    *,
    proposals: tuple[dict[str, object], ...] = (),
) -> str:
    return json.dumps(
        {
            "decision_version": 1,
            "proposals": proposals,
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {"tool_name": tool_name, "arguments": arguments},
            },
        }
    )


def test_production_registry_decodes_durable_planned_actions(tmp_path: Path) -> None:
    policy = _policy(actions=15)
    database = create_sqlite_adaptive_fixture(
        "F02_VERTICAL_EAV",
        tmp_path / "fixture.sqlite",
    )
    dsn = f"sqlite://{database}"
    scope = _scope()
    loaded = SchemaLoader(tmp_path / "schema-cache").load_scoped_schema(
        {},
        dsn,
        scope,
    )
    schema_version = f"sha256:{loaded.namespace.version_key}"
    state = _initial_state(
        "F02_VERTICAL_EAV",
        schema_version,
        policy,
    )
    state_path = tmp_path / "production-state.sqlite"
    state_store = AdaptiveResearchStateStore(state_path)
    checkpoint_store = AdaptiveStateStore(state_path)
    ledger = AdaptiveBudgetLedger(state_path)
    profile = load_schema_research_agent_profile()
    deadline = DeadlineBudget.from_duration(60)
    state_store.save_research_state(state, expected_previous_revision=None)
    assembly = assemble_production_research(
        initial_state=state,
        query=state.query_spec.original_text,
        loaded_schema=loaded,
        dsn=dsn,
        scope=scope,
        table_namespace="main",
        model=_VerticalFixtureModel(),
        model_identity=stable_schema_research_model_identity(profile.model),
        profile=profile,
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=policy,
        deadline=deadline,
        is_cancelled=lambda: False,
    )
    decision = parse_research_decision(
        _tool(
            "get_distinct_values",
            {
                "table": "attribute_fact",
                "column": "value_text",
                "top_k": 10,
            },
        )
    )
    resolved = resolve_research_decision(
        state,
        decision,
        loaded_schema=loaded,
        freshness_context=assembly.freshness_context,
        registry=assembly.registry,
        deadline=deadline,
    )
    checkpoint_store.record_planned(
        AdaptiveCheckpointKey(
            state.run_id,
            state.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            state.revision,
        ),
        expected_revision=None,
        action=_planned_action(resolved),
    )
    token = set_tool_runtime_context(
        {"supervisor_evidence": SupervisorExecutionEvidence("production-loop", 1)}
    )
    try:
        result = execute_resolved_research_decision(
            resolved,
            assembly.registry,
        )
    finally:
        reset_tool_runtime_context(token)

    assert result is not None
    assert result.status.value == "success"
    assert result.inline_payload_json is not None
    assert json.loads(result.inline_payload_json)["rows"] == [
        ["gold"],
        ["north"],
        ["silver"],
    ]
    assert len(ledger.load_records(state.run_id, state.run_incarnation)) == 1


def test_production_research_assembles_available_document_freshness(tmp_path: Path) -> None:
    policy = _policy(actions=1)
    database = create_sqlite_adaptive_fixture(
        "F02_VERTICAL_EAV",
        tmp_path / "fixture.sqlite",
    )
    dsn = f"sqlite://{database}"
    scope = _scope()
    loaded = SchemaLoader(tmp_path / "schema-cache").load_scoped_schema(
        {},
        dsn,
        scope,
    )
    schema_version = f"sha256:{loaded.namespace.version_key}"
    state = _initial_state("F02_VERTICAL_EAV", schema_version, policy)
    document = SchemaEvidenceDocument(
        document_id="production-rule",
        namespace="main",
        schema_namespace_version=schema_version,
        source_version="v1",
        title="Production rule",
        content="Use the documented membership rule.",
        target=None,
    )
    state_path = tmp_path / "production-state.sqlite"
    state_store = AdaptiveResearchStateStore(state_path)
    checkpoint_store = AdaptiveStateStore(state_path)
    ledger = AdaptiveBudgetLedger(state_path)
    profile = load_schema_research_agent_profile()
    deadline = DeadlineBudget.from_duration(60)
    try:
        assembly = assemble_production_research(
            initial_state=state,
            query=state.query_spec.original_text,
            loaded_schema=loaded,
            documents=(document,),
            dsn=dsn,
            scope=scope,
            table_namespace="main",
            model=_VerticalFixtureModel(),
            model_identity=stable_schema_research_model_identity(profile.model),
            profile=profile,
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=policy,
            deadline=deadline,
            is_cancelled=lambda: False,
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    assert assembly.freshness_context.document_sources[0].availability is (
        DocumentSourceAvailability.AVAILABLE
    )


def test_production_registry_admits_sample_rows_with_its_planned_limit(
    tmp_path: Path,
) -> None:
    policy = _policy(actions=15)
    database = create_sqlite_adaptive_fixture(
        "F02_VERTICAL_EAV",
        tmp_path / "fixture.sqlite",
    )
    dsn = f"sqlite://{database}"
    scope = _scope()
    loaded = SchemaLoader(tmp_path / "schema-cache").load_scoped_schema(
        {},
        dsn,
        scope,
    )
    schema_version = f"sha256:{loaded.namespace.version_key}"
    state = _initial_state("F02_VERTICAL_EAV", schema_version, policy)
    state_path = tmp_path / "production-state.sqlite"
    state_store = AdaptiveResearchStateStore(state_path)
    checkpoint_store = AdaptiveStateStore(state_path)
    ledger = AdaptiveBudgetLedger(state_path)
    profile = load_schema_research_agent_profile()
    deadline = DeadlineBudget.from_duration(60)
    state_store.save_research_state(state, expected_previous_revision=None)
    assembly = assemble_production_research(
        initial_state=state,
        query=state.query_spec.original_text,
        loaded_schema=loaded,
        dsn=dsn,
        scope=scope,
        table_namespace="main",
        model=_VerticalFixtureModel(),
        model_identity=stable_schema_research_model_identity(profile.model),
        profile=profile,
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=policy,
        deadline=deadline,
        is_cancelled=lambda: False,
    )
    decision = parse_research_decision(
        _tool(
            "sample_rows",
            {"table": "member", "columns": ["member_label"], "limit": 1},
        )
    )
    resolved = resolve_research_decision(
        state,
        decision,
        loaded_schema=loaded,
        freshness_context=assembly.freshness_context,
        registry=assembly.registry,
        deadline=deadline,
    )
    checkpoint_store.record_planned(
        AdaptiveCheckpointKey(
            state.run_id,
            state.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            state.revision,
        ),
        expected_revision=None,
        action=_planned_action(resolved),
    )
    token = set_tool_runtime_context(
        {"supervisor_evidence": SupervisorExecutionEvidence("production-loop", 1)}
    )
    try:
        result = execute_resolved_research_decision(resolved, assembly.registry)
    finally:
        reset_tool_runtime_context(token)
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    assert result is not None
    assert result.status.value == "success"
    assert result.inline_payload_json is not None
    assert json.loads(result.inline_payload_json)["rows"] == [["member-a"]]


@pytest.mark.parametrize(
    ("fixture_id", "_repeat"),
    [
        (fixture_id, repeat)
        for fixture_id in ("F02_VERTICAL_EAV", "F03_OPAQUE_NAMES")
        for repeat in range(20)
    ],
)
def test_eav_and_opaque_fixtures_reach_complete_through_real_registry(
    tmp_path: Path,
    fixture_id: str,
    _repeat: int,
) -> None:
    policy = _policy(actions=15)
    database = create_sqlite_adaptive_fixture(fixture_id, tmp_path / "fixture.sqlite")
    dsn = f"sqlite://{database}"
    loader = SchemaLoader(tmp_path / "schema-cache")
    scope = _scope()
    loaded = loader.load_scoped_schema({}, dsn, scope)
    schema_version = f"sha256:{loaded.namespace.version_key}"
    deadline = DeadlineBudget.from_duration(60)
    schema_runtime = SchemaProbeRuntime(
        loader=loader,
        dsn=dsn,
        scope=scope,
        namespace=loaded.namespace,
        table_namespace="main",
        deadline=deadline,
    )
    data_runtime = DataProbeRuntime(
        loader=loader,
        dsn=dsn,
        scope=scope,
        namespace=loaded.namespace,
        table_namespace="main",
        deadline=deadline,
    )
    state = _initial_state(fixture_id, schema_version, policy)
    durable_state_path = tmp_path / "state.sqlite"
    state_store = AdaptiveResearchStateStore(durable_state_path)
    checkpoint_store = AdaptiveStateStore(durable_state_path)
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")

    factory_events: list[str] = []

    def budget_factory(kind, target, parameters) -> SchemaProbeBudgetRuntime:
        factory_events.append("called")
        try:
            persisted = state_store.load_latest_research_state(
                state.run_id, state.run_incarnation
            )
            assert persisted is not None
            snapshot = checkpoint_store.get_snapshot(
                # The planned checkpoint is durable before the registry calls us.
                AdaptiveCheckpointKey(
                    persisted.run_id,
                    persisted.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    persisted.revision,
                )
            )
            assert snapshot.planned is not None
            planned = snapshot.planned.action
            assert isinstance(planned, dict)
            action = ResearchAction.model_validate_json(
                canonical_json_bytes(planned["action"]), strict=True
            )
            assert action.kind is kind
            assert action.target == target
            assert action.parameters == parameters
            invocation_id = planned["invocation_id"]
            assert isinstance(invocation_id, str)
            factory_events.append("validated")
            return SchemaProbeBudgetRuntime(
                state=_state_with_reconciled_model_budget(persisted, ledger, policy),
                action=action,
                maximum_cost=_maximum_cost(rows=dict(parameters).get("limit", 100)),
                config=policy,
                ledger=ledger,
                invocation_id=invocation_id,
            )
        except Exception as error:
            factory_events.append(f"{type(error).__name__}: {error}")
            raise

    registry = AdaptiveResearchToolRegistry(
        AdaptiveResearchToolContext(
            schema_runtime=schema_runtime,
            data_runtime=data_runtime,
            budget_factory=budget_factory,
        )
    )
    model = _VerticalFixtureModel()
    freshness = FreshnessContext(
        evaluated_at=datetime.now(UTC) + timedelta(minutes=5),
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=schema_version,
    )
    token = set_tool_runtime_context(
        {"supervisor_evidence": SupervisorExecutionEvidence("fixture-loop", 1)}
    )
    try:
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="gold members with membership level",
                research_context=_bounded_research_context(loaded.schema),
                model=model,
                model_identity="tests/real-fixture-script",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded,
                freshness_context=freshness,
                registry=registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=policy,
                deadline=deadline,
            )
        )
        assert outcome.stop_reason is ResearchStopReason.COMPLETE, (
            outcome,
            ledger.load_records(state.run_id, state.run_incarnation),
            factory_events,
        )
        assert outcome.final_state.unresolved_items == ()
        semantic_item = outcome.final_state.query_spec.semantic_items[0]
        assert semantic_item.status is SemanticItemStatus.RESOLVED
        assert semantic_item.operator is PredicateOperator.EQ
        assert semantic_item.literal_or_reference == "gold"
        assert len(outcome.final_state.join_candidates) == 2
        assert all(
            join.status is JoinCandidateStatus.VALIDATED
            for join in outcome.final_state.join_candidates
        )
        assert len(outcome.final_state.bindings) == 1
        binding = outcome.final_state.bindings[0]
        assert isinstance(binding, VerticalAttributeBinding)
        assert binding.status is BindingStatus.SUPPORTED
        assert semantic_item.binding_ids == (binding.binding_id,)
        assert binding.attribute_name_predicate.operator is PredicateOperator.EQ
        assert binding.attribute_name_predicate.right == "membership_level"
        assert binding.value_predicate.operator is PredicateOperator.EQ
        assert binding.value_predicate.right == "gold"
        assert (
            len(outcome.final_state.action_history) == policy.operation_counts.actions
        )
        assert (
            len({item.action_digest for item in outcome.final_state.action_history})
            == policy.operation_counts.actions
        )
        probe_records = ledger.load_records(state.run_id, state.run_incarnation)
        model_records = ledger.load_model_records(state.run_id, state.run_incarnation)
        final_budget = outcome.final_state.budget_state
        assert len(probe_records) == len(outcome.final_state.action_history)
        assert len(model_records) == final_budget.used_model_calls
        assert all(record.reconciliation is not None for record in probe_records)
        assert all(record.reconciliation is not None for record in model_records)
        assert final_budget.used_model_calls == policy.model_budget.model_calls
        assert final_budget.remaining_model_calls == 0
        assert final_budget.used_model_tokens == sum(
            record.reconciliation.charged_total_tokens
            for record in model_records
            if record.reconciliation is not None
        )
        assert final_budget.remaining_model_tokens == (
            final_budget.initial_model_tokens - final_budget.used_model_tokens
        )
        assert [record.reservation.call_id for record in model_records] == [
            f"research-model-{revision}-0"
            for revision in range(policy.operation_counts.actions)
        ]
        for probe_record, model_record in zip(
            probe_records, model_records, strict=True
        ):
            assert model_record.reconciliation is not None
            assert (
                probe_record.reservation.budget_before.used_model_calls
                == model_record.reconciliation.budget_after.used_model_calls
            )
            assert (
                probe_record.reservation.budget_before.used_model_tokens
                == model_record.reconciliation.budget_after.used_total_tokens
            )
        for field in ("wall_clock_ms", "db_probe_ms", "rows", "bytes"):
            assert getattr(final_budget, f"used_{field}") == sum(
                getattr(record.reconciliation.charged_cost, field)
                for record in probe_records
                if record.reconciliation is not None
            )
            assert getattr(final_budget, f"remaining_{field}") == (
                getattr(final_budget, f"initial_{field}")
                - getattr(final_budget, f"used_{field}")
            )
        assert len(model.prompts) == policy.operation_counts.actions
    finally:
        reset_tool_runtime_context(token)
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def _maximum_cost(*, rows: int):
    from custom_tools.text_to_sql.adaptive.models import EvidenceCost

    return EvidenceCost(
        wall_clock_ms=10_000,
        model_calls=0,
        model_tokens=0,
        db_probe_ms=10_000,
        rows=rows,
        bytes=100_000,
    )
