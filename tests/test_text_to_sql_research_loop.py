"""Focused W3-07 contracts for the durable asynchronous research loop."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import os
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive import research_loop as _research_loop_module
from custom_tools.text_to_sql.adaptive import production_research as _production_research_module
from custom_tools.text_to_sql.adaptive import state as _state_module
from custom_tools.text_to_sql.adaptive.freshness import (
    DocumentSourceAvailability,
    DocumentSourceState,
    FreshnessContext,
)
from custom_tools.text_to_sql.adaptive.model_budget import ModelBudgetLimits
from custom_tools.text_to_sql.adaptive.model_budget import (
    ModelCallStarted,
    ModelTokenUsage,
)
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    ColumnRef,
    DiscriminatorValueBinding,
    DocumentRef,
    DocumentRuleBinding,
    DerivedExpressionBinding,
    EvidenceCost,
    ExpressionRef,
    ExpectedResultShape,
    Hypothesis,
    HypothesisStatus,
    JoinCandidate,
    JoinCandidateStatus,
    JoinEdge,
    JoinType,
    PhysicalColumnBinding,
    PredicateRef,
    ResearchAction,
    ResearchActionKind,
    QuerySpec,
    ResearchState,
    ResearchStopReason,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    TableRef,
    PredicateOperator,
)
from custom_tools.text_to_sql.adaptive.replay_inputs import ResearchTerminalReplayInput
from custom_tools.text_to_sql.adaptive.controller import NormalizedToolResult
from custom_tools.text_to_sql.adaptive.decision_resolver import DecisionResolverError
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, build_probe_result
from custom_tools.text_to_sql.adaptive.research_query import ResearchQueryAdmissionError
from custom_tools.text_to_sql.adaptive.research_decision import (
    DiscriminatorValueCandidate,
    DerivedExpressionCandidate,
    LogicalColumnRef,
    LogicalPredicate,
    PhysicalColumnCandidate,
    ResearchDecisionV1,
)
from custom_tools.text_to_sql.adaptive.policy import (
    AdaptivePolicyConfig,
    BudgetAdmissionError,
    OperationCountBudget,
    PerActionBudget,
    ResourceBudget,
    ResultVolumeBudget,
    WallClockBudget,
    execute_model_call_with_budget_async,
    execute_probe_with_budget,
    initial_budget_state,
    canonical_action_digest,
    reserve_model_call_budget,
)
from custom_tools.text_to_sql.adaptive.evidence import probe_result_to_evidence
from custom_tools.text_to_sql.adaptive._policy_authority import (
    ResearchGenerationAuthority,
    ResearchGenerationAuthorityStatus,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import CoverageInputErrorCode
from custom_tools.text_to_sql.adaptive.research_loop import (
    _authority_stop_reason,
    _missing_binding_column_probe,
    _probe_from_observed,
    _state_with_reconciled_model_budget,
    _stable_planned_identity,
    _terminal_envelope,
    run_research_loop,
)
from custom_tools.text_to_sql.adaptive.production_research import (
    _exact_formula_documents,
)
from custom_tools.text_to_sql.adaptive.schema_probes import SchemaEvidenceDocument
from custom_tools.text_to_sql.adaptive.schema_research_agent import (
    SchemaResearchDecisionAdapter,
    SchemaResearchModelResponse,
    build_schema_research_prompt,
    load_schema_research_agent_profile,
)
from custom_tools.text_to_sql.adaptive.semantic_reducer import commit_semantic_turn
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
    serialize_contract,
)
from custom_tools.text_to_sql.adaptive.terminal import research_stop_terminal_result
from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.adaptive_budget_ledger import EXECUTION_CLAIM_LEASE_NS
from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
from workflow.adaptive_state_store import (
    AdaptiveCheckpointCasError,
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.deadline import DeadlineBudget
from text_to_sql_decision_resolver_helpers import (
    NOW as _FIXTURE_NOW,
    freshness as _fixture_freshness,
    make_registry as _make_registry,
    make_state as _make_fixture_state,
    resolve as _resolve_fixture,
    schema as _fixture_schema,
    tool_decision as _tool_decision,
)


def _seed_honest_v2_history(path, states=(), events=()) -> None:
    from workflow.adaptive_research_state_store import _V2_OWNED_TABLE_SQL

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        AdaptiveStateStore._create_checkpoint_tables(connection)
        AdaptiveStateStore._migrate_v0_to_v1(connection)
        AdaptiveStateStore._migrate_v1_to_v2(connection)
        for statement in _V2_OWNED_TABLE_SQL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO adaptive_research_state_meta (key, value) VALUES (?, 2)",
            ("schema_version",),
        )
        for state in states:
            connection.execute(
                """
                INSERT INTO adaptive_research_state_snapshots (
                    run_id, run_incarnation, contract_name, revision,
                    payload, digest, created_at_ns
                ) VALUES (?, ?, 'research_state', ?, ?, ?, ?)
                """,
                (
                    state.run_id,
                    state.run_incarnation,
                    state.revision,
                    serialize_contract(state),
                    canonical_digest(state),
                    state.revision + 1,
                ),
            )
        planned_revisions = []
        for key, phase, action in events:
            action_json = canonical_json_bytes(action).decode("utf-8")
            action_digest = f"sha256:{hashlib.sha256(action_json.encode()).hexdigest()}"
            connection.execute(
                """
                INSERT INTO adaptive_checkpoint_events (
                    run_id, run_incarnation, loop_kind, revision, phase,
                    action_json, action_digest, artifact_digest, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1)
                """,
                (
                    key.run_id,
                    key.run_incarnation,
                    key.loop_kind.value,
                    key.revision,
                    phase,
                    action_json,
                    action_digest,
                ),
            )
            if phase == "planned":
                planned_revisions.append(key)
        for key in planned_revisions:
            connection.execute(
                """
                INSERT INTO adaptive_checkpoint_heads (
                    run_id, run_incarnation, loop_kind, revision
                ) VALUES (?, ?, ?, ?)
                """,
                (key.run_id, key.run_incarnation, key.loop_kind.value, key.revision),
            )


_SCHEMA = "sha256:" + "a" * 64
_NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _observed_column_evidence(
    state: ResearchState,
    column: ColumnRef,
    *,
    invocation_id: str,
    kind: ResearchActionKind = ResearchActionKind.INSPECT_COLUMN,
):
    action = ResearchAction(
        action_id=f"{invocation_id}-action",
        kind=kind,
        hypothesis_id=None,
        target=column,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=kind,
            hypothesis_id=None,
            target=column,
            parameters=(),
            expected_revision=state.revision,
        ),
        expected_revision=state.revision,
    )
    payload = {
        "status": "matched",
        "column": column.model_dump(mode="json", by_alias=True),
    }
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id=invocation_id,
        action_digest=action.action_digest,
        probe_kind=kind,
        status=ProbeStatus.SUCCESS,
        target=column,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="trusted column observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    return action, evidence


def _observed_table_evidence(
    state: ResearchState,
    table: TableRef,
    *,
    invocation_id: str,
    columns: list[object],
    status: str = "matched",
    kind: ResearchActionKind = ResearchActionKind.INSPECT_TABLE,
):
    action = ResearchAction(
        action_id=f"{invocation_id}-action",
        kind=kind,
        hypothesis_id=None,
        target=table,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=kind,
            hypothesis_id=None,
            target=table,
            parameters=(),
            expected_revision=state.revision,
        ),
        expected_revision=state.revision,
    )
    payload = {
        "status": status,
        "table": table.model_dump(mode="json", by_alias=True),
        "columns": columns,
    }
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id=invocation_id,
        action_digest=action.action_digest,
        probe_kind=kind,
        status=ProbeStatus.SUCCESS,
        target=table,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="trusted table observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    return action, evidence


def _supported_state_after_probe(namespace, *, observed_at: datetime) -> ResearchState:
    base = _policy_state(namespace)
    table = TableRef(namespace="main", schema="public", table="orders")
    action = ResearchAction(
        action_id="schema-action",
        kind=ResearchActionKind.INSPECT_TABLE,
        hypothesis_id=None,
        target=table,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_TABLE,
            hypothesis_id=None,
            target=table,
            parameters=(),
            expected_revision=0,
        ),
        expected_revision=0,
    )
    payload = {"status": "matched"}
    result = build_probe_result(
        run_id=base.run_id,
        run_incarnation=base.run_incarnation,
        revision=0,
        schema_namespace_version=base.schema_namespace_version,
        invocation_id="schema-evidence",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=table,
        started_at=observed_at,
        completed_at=observed_at,
        summary="trusted schema observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    column = ColumnRef(table=evidence.target, column="status")
    binding = PhysicalColumnBinding(
        binding_id="binding-1",
        source_id="source-1",
        tables=(column.table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="schema evidence",
        physical_column=column,
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.DIMENSION,
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (binding.binding_id,),
        }
    )
    query = base.query_spec.model_copy(update={"semantic_items": (item,)})
    return ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "revision": 1,
            "query_spec": query,
            "evidence": (evidence,),
            "bindings": (binding,),
            "unresolved_items": (),
            "action_history": (action,),
        }
    )

def _document_supported_state_after_probe(
    namespace,
    *,
    observed_at: datetime,
    valid_until: datetime,
) -> tuple[ResearchState, DocumentRef]:
    base = _policy_state(namespace)
    document = DocumentRef(document_id="orders-rule", namespace="main")
    action = ResearchAction(
        action_id="document-action",
        kind=ResearchActionKind.READ_DOCUMENT,
        hypothesis_id=None,
        target=document,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.READ_DOCUMENT,
            hypothesis_id=None,
            target=document,
            parameters=(),
            expected_revision=0,
        ),
        expected_revision=0,
    )
    payload = {
        "document": {
            "source_version": "v1",
            "valid_until": valid_until,
        },
        "content": "Orders use the approved rule.",
        "title": "Orders rule",
    }
    result = build_probe_result(
        run_id=base.run_id,
        run_incarnation=base.run_incarnation,
        revision=0,
        schema_namespace_version=base.schema_namespace_version,
        invocation_id="document-evidence",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=document,
        started_at=observed_at,
        completed_at=observed_at,
        summary="trusted document observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=0,
        payload=payload,
    )
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    binding = DocumentRuleBinding(
        binding_id="binding-1",
        source_id="source-1",
        tables=(),
        columns=(),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="document evidence",
        document=document,
        rule_id="orders-rule",
        rule_text="Orders use the approved rule.",
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.DIMENSION,
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (binding.binding_id,),
        }
    )
    query = base.query_spec.model_copy(update={"semantic_items": (item,)})
    return (
        ResearchState.model_validate(
            {
                **base.model_dump(mode="python", round_trip=True),
                "revision": 1,
                "query_spec": query,
                "evidence": (evidence,),
                "bindings": (binding,),
                "unresolved_items": (),
                "action_history": (action,),
            }
        ),
        document,
    )


def _policy(model_calls: int = 4) -> AdaptivePolicyConfig:
    total_tokens = model_calls * 20
    limits = ModelBudgetLimits(
        model_calls=model_calls,
        input_tokens_per_call=10,
        output_tokens_per_call=10,
        total_tokens=total_tokens,
    )
    return AdaptivePolicyConfig(
        policy_version=2,
        wall_clock=WallClockBudget(wall_clock_seconds=10),
        resource_limits=ResourceBudget(
            model_tokens=total_tokens,
            db_probe_ms=1_000,
        ),
        operation_counts=OperationCountBudget(
            actions=4,
            model_decisions=model_calls,
            db_probes=4,
        ),
        result_volume=ResultVolumeBudget(returned_rows=10, inline_bytes=1_000),
        per_action=PerActionBudget(sample_rows=1),
        model_budget=limits,
    )


def _state(*, required: bool) -> ResearchState:
    items = ()
    unresolved = ()
    text = "orders"
    if required:
        items = (
            SemanticItem(
                source_id="source-1",
                kind=SemanticItemKind.FILTER,
                source_text=text,
                normalized_meaning=text,
                required=True,
                operator=None,
                literal_or_reference=None,
                status=SemanticItemStatus.UNRESOLVED,
                binding_ids=(),
            ),
        )
        unresolved = ("source-1",)
    query = QuerySpec(
        run_id="loop-run",
        run_incarnation="loop-incarnation",
        revision=0,
        schema_namespace_version=_SCHEMA,
        query_id="query-1",
        original_text=text,
        semantic_items=items,
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )
    return ResearchState(
        run_id=query.run_id,
        run_incarnation=query.run_incarnation,
        revision=0,
        schema_namespace_version=_SCHEMA,
        query_spec=query,
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=unresolved,
        action_history=(),
        result_expectations=(),
        budget_state=initial_budget_state(_policy()),
        stop_reason=None,
    )


def _two_unresolved_states():
    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _policy_state(namespace, with_evidence=True)
    first = state.query_spec.semantic_items[0]
    second = SemanticItem(
        source_id="source-2",
        kind=SemanticItemKind.FILTER,
        source_text="customers",
        normalized_meaning="customers",
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    query = state.query_spec.model_copy(
        update={
            "original_text": "orders customers",
            "semantic_items": (first, second),
        }
    )
    state = state.model_copy(
        update={"query_spec": query, "unresolved_items": ("source-1", "source-2")}
    )
    return initial, state, loaded_schema, namespace


def _seed_prior_model_budget(
    state: ResearchState,
    ledger: AdaptiveBudgetLedger,
    policy: AdaptivePolicyConfig | None = None,
    revision: int = 0,
) -> None:
    async def seed_model_budget(_reservation) -> ModelTokenUsage:
        return ModelTokenUsage(input_tokens=None, output_tokens=None)

    asyncio.run(
        execute_model_call_with_budget_async(
            state.run_id,
            state.run_incarnation,
            f"research-model-{revision}-0",
            canonical_digest({"seed": f"revision-{revision}"}),
            "test/model",
            10,
            10,
            seed_model_budget,
            config=policy or _policy(),
            ledger=ledger,
            claim_now_ns=lambda: 0,
            owner_token_factory=lambda: "seed-model-owner",
        )
    )


def _policy_state(namespace, **kwargs) -> ResearchState:
    """Use the loop's real policy totals for resolver fixture state."""

    state = _make_fixture_state(namespace, **kwargs)
    return ResearchState.model_validate(
        {
            **state.model_dump(mode="python", by_alias=True, round_trip=True),
            "budget_state": initial_budget_state(_policy()),
        }
    )


def _freshness(state: ResearchState) -> FreshnessContext:
    return FreshnessContext(
        evaluated_at=_NOW,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )


async def _run(tmp_path, state: ResearchState, model, **extra):
    database = tmp_path / "adaptive.sqlite"
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    ledger = extra.pop("budget_ledger", None)
    if ledger is None:
        ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    try:
        arguments = {
            "initial_state": state,
            "task": "research schema",
            "research_context": lambda current, _feedbacks, _rejected=(), *_args: canonical_digest(
                current
            ),
            "model": model,
            "model_identity": "test/model",
            "adapter": SchemaResearchDecisionAdapter(
                load_schema_research_agent_profile()
            ),
            "loaded_schema": object(),
            "freshness_context": _freshness(state),
            "registry": object(),
            "state_store": state_store,
            "checkpoint_store": checkpoint_store,
            "budget_ledger": ledger,
            "policy": _policy(),
        }
        arguments.update(extra)
        outcome = await run_research_loop(**arguments)
        return outcome, state_store, checkpoint_store, ledger
    except BaseException:
        state_store.close()
        checkpoint_store.close()
        ledger.close()
        raise


def _open_existing_research_state(tmp_path, initial: ResearchState, state: ResearchState):
    database = tmp_path / "adaptive.sqlite"
    _seed_honest_v2_history(
        database,
        states=(initial, state),
        events=(
            (
                AdaptiveCheckpointKey(
                    state.run_id,
                    state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    state.revision - 1,
                ),
                "planned",
                {"kind": "seed"},
            ),
            (
                AdaptiveCheckpointKey(
                    state.run_id,
                    state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    state.revision - 1,
                ),
                "observed",
                {"kind": "seed"},
            ),
        ),
    )
    return (
        AdaptiveResearchStateStore(database),
        AdaptiveStateStore(database),
        AdaptiveBudgetLedger(tmp_path / "budget.sqlite"),
    )


@pytest.mark.parametrize("_repeat", range(20))
def test_simple_schema_stops_without_model_or_action(tmp_path, _repeat: int) -> None:
    called = False

    async def model(_prompt: str) -> str:
        nonlocal called
        called = True
        return "{}"

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, _state(required=False), model)
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.COMPLETE
        assert outcome.final_state.revision == 0
        assert outcome.final_state.action_history == ()
        assert called is False
        terminal = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                "loop-run", "loop-incarnation", AdaptiveLoopKind.RESEARCH, 0
            )
        ).terminal
        assert terminal is not None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_unbound_formula_continuation_defers_automatic_complete(tmp_path) -> None:
    state = _state(required=False)
    formula = SemanticItem(
        source_id="formula-1",
        kind=SemanticItemKind.FORMULA,
        source_text="amount above the computed average",
        normalized_meaning="amount > AVG(amount)",
        required=True,
        operator=PredicateOperator.GT,
        literal_or_reference=None,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=(),
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (formula,)}
            ),
            "unresolved_items": (),
        }
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "complete",
                    "source_ids": [],
                    "citation_evidence_ids": [],
                },
            }
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            semantic_repair_continuation=True,
        )
    )
    try:
        assert outcome.stop_reason is not ResearchStopReason.COMPLETE
        assert calls > 0
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_unbound_formula_continuation_does_not_replay_prior_complete(
    tmp_path,
) -> None:
    state = _state(required=False)
    formula_source_id = f"semantic:{'f' * 64}"
    formula = SemanticItem(
        source_id=formula_source_id,
        kind=SemanticItemKind.FORMULA,
        source_text="amount above the computed average",
        normalized_meaning="amount > AVG(amount)",
        required=True,
        operator=PredicateOperator.GT,
        literal_or_reference=None,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=(),
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (formula,)}
            ),
            "unresolved_items": (),
        }
    )

    async def initial_model(_prompt: str) -> str:
        raise AssertionError("ordinary formula authority must complete automatically")

    first, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, state, initial_model)
    )
    calls = 0

    async def continuation_model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "unsupported",
                    "source_ids": [formula_source_id],
                    "citation_evidence_ids": ["evidence-1"],
                },
            }
        )

    try:
        assert first.stop_reason is ResearchStopReason.COMPLETE
        second = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(
                    current
                ),
                model=continuation_model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=object(),
                freshness_context=_freshness(state),
                registry=object(),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
                semantic_repair_continuation=True,
            )
        )
        assert second.stop_reason is not ResearchStopReason.COMPLETE
        assert calls > 0
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_semantic_repair_continuation_saves_current_revision_transition(
    tmp_path,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _policy_state(namespace, with_evidence=True)
    state_store, checkpoint_store, ledger = _open_existing_research_state(
        tmp_path, initial, state
    )

    async def no_model(_prompt: str) -> str:
        raise AssertionError("the direct semantic transition must not call the model")

    registry = _make_registry(namespace)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:replacement-binding",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )
    resolved = _resolve_fixture(
        decision,
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=registry,
    )
    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=no_model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_freshness(state),
        registry=registry,
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "owner",
        model_wait=None,
        semantic_repair_continuation=True,
    )
    try:
        assert coordinator._record_planned(state, resolved) is None
        committed = commit_semantic_turn(resolved.admission)
        assert coordinator._record_observed(state, resolved, None, True) is None

        assert (
            coordinator._save_semantic_transition(
                state,
                committed.state,
                resolved,
                resolved.admission,
                None,
            )
            is None
        )
        assert (
            state_store.load_latest_research_state(
                state.run_id, state.run_incarnation
            )
            == committed.state
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_unbound_formula_continuation_commits_after_prior_complete(tmp_path) -> None:
    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _policy_state(namespace, with_evidence=True)
    formula = SemanticItem(
        source_id="source-1",
        kind=SemanticItemKind.FORMULA,
        source_text="amount above the computed average",
        normalized_meaning="amount > AVG(amount)",
        required=True,
        operator=PredicateOperator.GT,
        literal_or_reference=None,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=(),
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (formula,)}
            ),
            "bindings": (),
            "unresolved_items": (),
        }
    )
    state_store, checkpoint_store, ledger = _open_existing_research_state(
        tmp_path, initial, state
    )

    async def no_model(_prompt: str) -> str:
        raise AssertionError("ordinary formula authority must complete automatically")

    arguments = {
        "initial_state": state,
        "task": "research schema",
        "research_context": lambda current, _feedbacks: canonical_digest(current),
        "model_identity": "test/model",
        "adapter": SchemaResearchDecisionAdapter(
            load_schema_research_agent_profile()
        ),
        "loaded_schema": loaded_schema,
        "freshness_context": _freshness(state),
        "registry": _make_registry(namespace),
        "state_store": state_store,
        "checkpoint_store": checkpoint_store,
        "budget_ledger": ledger,
        "policy": _policy(),
    }
    try:
        first = asyncio.run(run_research_loop(model=no_model, **arguments))
        assert first.stop_reason is ResearchStopReason.COMPLETE
        decision = ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "new_binding",
                        "proposal_key": "proposal:formula-input",
                        "source_id": formula.source_id,
                        "candidate": {
                            "kind": "physical_column",
                            "physical_column": {
                                "table": "public.orders",
                                "column": "status",
                            },
                        },
                        "join_references": (),
                        "citation_evidence_ids": (state.evidence[0].evidence_id,),
                    },
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )
        resolved = _resolve_fixture(
            decision,
            loaded=loaded_schema,
            namespace=namespace,
            state=state,
            registry=arguments["registry"],
        )
        coordinator = _research_loop_module._ResearchLoopCoordinator(
            model=no_model,
            deadline=None,
            is_cancelled=lambda: False,
            model_claim_now_ns=lambda: 0,
            model_owner_token_factory=lambda: "owner",
            model_wait=None,
            semantic_repair_continuation=True,
            **arguments,
        )
        assert coordinator._record_planned(state, resolved) is None
        committed = commit_semantic_turn(resolved.admission)
        assert coordinator._record_observed(state, resolved, None, True) is None
        assert (
            coordinator._save_semantic_transition(
                state,
                committed.state,
                resolved,
                resolved.admission,
                None,
            )
            is None
        )
        assert committed.state.revision == state.revision + 1
        assert committed.state.query_spec.semantic_items[0].binding_ids

        binding_id = committed.state.query_spec.semantic_items[0].binding_ids[0]
        assessment = ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": binding_id,
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": (state.evidence[0].evidence_id,),
                    },
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )
        assessed = _resolve_fixture(
            assessment,
            loaded=loaded_schema,
            namespace=namespace,
            state=committed.state,
            registry=arguments["registry"],
        )
        assert coordinator._record_planned(committed.state, assessed) is None
        supported = commit_semantic_turn(assessed.admission)
        assert (
            coordinator._record_observed(
                committed.state,
                assessed,
                None,
                True,
            )
            is None
        )
        assert (
            coordinator._save_semantic_transition(
                committed.state,
                supported.state,
                assessed,
                assessed.admission,
                None,
            )
            is None
        )
        assert supported.state.revision == committed.state.revision + 1
        assert supported.state.bindings[-1].status is BindingStatus.SUPPORTED
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_complete_reuses_captured_freshness_and_replays_it(
    tmp_path, monkeypatch
) -> None:
    t0 = _FIXTURE_NOW
    t1 = datetime(2026, 7, 31, 12, 1, tzinfo=UTC)
    t2 = datetime(2026, 7, 31, 12, 2, tzinfo=UTC)
    _, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _supported_state_after_probe(namespace, observed_at=t1)
    freshness = _fixture_freshness(state).model_copy(update={"evaluated_at": t0})

    class _TerminalClock:
        @classmethod
        def now(cls, zone):
            assert zone is UTC
            return t2

    monkeypatch.setattr(
        _research_loop_module,
        "datetime",
        _TerminalClock,
        raising=False,
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("current terminal authority must not call the model")

    state_store, checkpoint_store, ledger = _open_existing_research_state(
        tmp_path, initial, state
    )
    _seed_prior_model_budget(initial, ledger)
    outcome = asyncio.run(
        run_research_loop(
            initial_state=state,
            task="research schema",
            research_context=lambda current, _feedbacks: canonical_digest(current),
            model=model,
            model_identity="test/model",
            adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
            loaded_schema=object(),
            freshness_context=freshness,
            registry=object(),
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=_policy(),
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.COMPLETE
        assert calls == 0
        key = AdaptiveCheckpointKey(
            state.run_id,
            state.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            state.revision,
        )
        replay_input = checkpoint_store.load_terminal_replay_input(key)
        assert type(replay_input) is ResearchTerminalReplayInput
        assert replay_input.freshness_context.evaluated_at == t0

        async def replay_model(_prompt: str) -> str:
            raise AssertionError("terminal replay must not call the model")

        replay = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=replay_model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=object(),
                freshness_context=freshness,
                registry=object(),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert replay == outcome
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_complete_stop_citations_are_owned_by_durable_state() -> None:
    _, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_NOW)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (),
            "next": {
                "next_kind": "stop",
                "reason": "complete",
                "source_ids": (),
                "citation_evidence_ids": ("hypothesis:" + "f" * 64,),
            },
        }
    )

    normalized = _research_loop_module._normalize_complete_stop_citations(
        state,
        decision,
        _freshness(state),
    )

    assert normalized.next.citation_evidence_ids == (
        state.evidence[0].evidence_id,
    )


def test_unselected_formula_candidate_does_not_defer_automatic_complete(tmp_path) -> None:
    _, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    supported = state.bindings[0]
    candidate_column = supported.physical_column.model_copy(
        update={"column": "pending_value"}
    )
    candidate = PhysicalColumnBinding(
        binding_id="binding-pending",
        source_id=supported.source_id,
        tables=(candidate_column.table,),
        columns=(candidate_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(state.evidence[0].evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=candidate_column,
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        state.query_spec.semantic_items[0].model_copy(
                            update={
                                "kind": SemanticItemKind.FORMULA,
                                "source_text": "documented formula",
                                "normalized_meaning": "documented formula",
                            }
                        ),
                    )
                }
            ),
            "bindings": (*state.bindings, candidate),
        }
    )
    calls = 0

    async def model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("unselected candidate must not require a model turn")

    state_store, checkpoint_store, ledger = _open_existing_research_state(
        tmp_path, initial, state
    )
    _seed_prior_model_budget(initial, ledger)
    outcome = asyncio.run(
        run_research_loop(
            initial_state=state,
            task="research schema",
            research_context=lambda _current, _feedbacks: candidate.binding_id,
            model=model,
            model_identity="test/model",
            adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
            loaded_schema=object(),
            freshness_context=_fixture_freshness(state),
            registry=object(),
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=_policy(),
            semantic_repair_continuation=True,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.COMPLETE
        assert calls == 0
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_exact_document_formula_requires_selected_derived_binding_before_complete() -> None:
    _, namespace = _fixture_schema()
    physical_state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    formula = "AVG(measure)"
    document = DocumentRef(document_id="formula-rule", namespace="main")
    document_action = ResearchAction(
        action_id="formula-rule-action",
        kind=ResearchActionKind.READ_DOCUMENT,
        hypothesis_id=None,
        target=document,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.READ_DOCUMENT,
            hypothesis_id=None,
            target=document,
            parameters=(),
            expected_revision=physical_state.revision,
        ),
        expected_revision=physical_state.revision,
    )
    document_payload = {
        "document": {
            "source_version": "v1",
            "valid_until": _FIXTURE_NOW + timedelta(days=1),
        },
        "content": f"Exact formula: {formula}.",
        "title": "Formula rule",
    }
    document_result = build_probe_result(
        run_id=physical_state.run_id,
        run_incarnation=physical_state.run_incarnation,
        revision=physical_state.revision,
        schema_namespace_version=physical_state.schema_namespace_version,
        invocation_id="formula-rule-evidence",
        action_digest=document_action.action_digest,
        probe_kind=document_action.kind,
        status=ProbeStatus.SUCCESS,
        target=document,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="trusted formula document",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=len(canonical_json_bytes(document_payload)),
        ),
        row_count=0,
        payload=document_payload,
    )
    document_evidence = probe_result_to_evidence(document_result, document_action)
    assert document_evidence is not None
    physical_binding = physical_state.bindings[0]
    orders = physical_binding.physical_column.table
    records = TableRef(namespace="main", schema="public", table="records")
    order_id = ColumnRef(table=orders, column="id")
    record_order_id = ColumnRef(table=records, column="order_id")
    join = JoinCandidate(
        join_id="orders-records",
        left=order_id,
        right=record_order_id,
        join_type=JoinType.INNER,
        path=(JoinEdge(left=order_id, right=record_order_id, join_type=JoinType.INNER),),
        status=JoinCandidateStatus.VALIDATED,
        evidence_ids=(physical_state.evidence[0].evidence_id,),
    )
    formula_item = physical_state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": formula,
            "normalized_meaning": (
                f"{formula} computed over qualifying rows where category = 'X'; scalar"
            ),
            "required": True,
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (physical_binding.binding_id,),
        }
    )
    state = ResearchState.model_validate(
        {
            **physical_state.model_dump(mode="python", by_alias=True, round_trip=True),
            "revision": physical_state.revision + 1,
            "query_spec": physical_state.query_spec.model_copy(
                update={"semantic_items": (formula_item,)}
            ),
            "evidence": (*physical_state.evidence, document_evidence),
            "join_candidates": (join,),
            "action_history": (
                *physical_state.action_history,
                document_action,
            ),
        }
    )
    fresh_document_context = FreshnessContext(
        evaluated_at=_FIXTURE_NOW,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
        document_sources=(
            DocumentSourceState(
                document_id=document.document_id,
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )
    expired_document_context = fresh_document_context.model_copy(
        update={"evaluated_at": _FIXTURE_NOW + timedelta(days=2)}
    )
    runtime_exact_documents = ((formula_item.source_id, document),)
    state_without_document_evidence = ResearchState.model_validate(
        {
            **state.model_dump(mode="python", by_alias=True, round_trip=True),
            "evidence": physical_state.evidence,
        }
    )

    assert _research_loop_module._has_pending_required_formula_continuation(
        state, fresh_document_context, runtime_exact_documents
    )
    assert _research_loop_module._has_pending_required_formula_continuation(
        state_without_document_evidence,
        fresh_document_context,
        runtime_exact_documents,
    )
    no_match_state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        formula_item.model_copy(
                            update={"normalized_meaning": "COUNT(unmatched_record_id)"}
                        ),
                    )
                }
            )
        }
    )
    ambiguous_document = DocumentRef(document_id="formula-rule-2", namespace="main")
    ambiguous_document_action = ResearchAction(
        action_id="formula-rule-action-2",
        kind=ResearchActionKind.READ_DOCUMENT,
        hypothesis_id=None,
        target=ambiguous_document,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.READ_DOCUMENT,
            hypothesis_id=None,
            target=ambiguous_document,
            parameters=(),
            expected_revision=physical_state.revision,
        ),
        expected_revision=physical_state.revision,
    )
    ambiguous_document_result = build_probe_result(
        run_id=physical_state.run_id,
        run_incarnation=physical_state.run_incarnation,
        revision=physical_state.revision,
        schema_namespace_version=physical_state.schema_namespace_version,
        invocation_id="formula-rule-evidence-2",
        action_digest=ambiguous_document_action.action_digest,
        probe_kind=ambiguous_document_action.kind,
        status=ProbeStatus.SUCCESS,
        target=ambiguous_document,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="second trusted formula document",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=len(canonical_json_bytes(document_payload)),
        ),
        row_count=0,
        payload=document_payload,
    )
    ambiguous_document_evidence = probe_result_to_evidence(
        ambiguous_document_result, ambiguous_document_action
    )
    assert ambiguous_document_evidence is not None
    ambiguous_state = state.model_copy(
        update={"evidence": (*state.evidence, ambiguous_document_evidence)}
    )
    ambiguous_document_context = fresh_document_context.model_copy(
        update={
            "document_sources": (
                *fresh_document_context.document_sources,
                DocumentSourceState(
                    document_id="formula-rule-2",
                    availability=DocumentSourceAvailability.AVAILABLE,
                    source_version="v1",
                ),
            )
        }
    )

    assert not _research_loop_module._has_pending_required_formula_continuation(
        no_match_state, fresh_document_context
    )
    assert not _research_loop_module._has_pending_required_formula_continuation(
        ambiguous_state, ambiguous_document_context
    )
    null_normalized_formula_state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        formula_item.model_copy(update={"normalized_meaning": None}),
                    )
                }
            )
        }
    )
    assert not _research_loop_module._has_pending_required_formula_continuation(
        null_normalized_formula_state, fresh_document_context
    )
    assert not _research_loop_module._has_pending_required_formula_continuation(
        state, expired_document_context
    )

    derived = DerivedExpressionBinding(
        binding_id="document-formula",
        source_id=formula_item.source_id,
        tables=(physical_binding.physical_column.table,),
        columns=(physical_binding.physical_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(document_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="semantic-certificate:v1:derived_expression",
        expression=ExpressionRef(
            expression_id="document-formula-expression",
            expression=formula,
        ),
        document=document,
        rule_excerpt=f"Exact formula: {formula}.",
        input_columns=(physical_binding.physical_column,),
    )
    derived_query = _state_module._derive_query_spec(
        state.query_spec, (*state.bindings, derived), state.revision + 1
    )
    derived_state = state.model_copy(
        update={"query_spec": derived_query, "bindings": (*state.bindings, derived)}
    )

    assert derived_query.semantic_items[0].exact_formula_binding_id == derived.binding_id
    assert not _research_loop_module._has_pending_required_formula_continuation(
        derived_state, fresh_document_context, runtime_exact_documents
    )

    selected_item = derived_query.semantic_items[0].model_copy(
        update={
            "binding_ids": (physical_binding.binding_id,),
            "exact_formula_binding_id": derived.binding_id,
        }
    )
    selected_state = derived_state.model_copy(
        update={
            "query_spec": derived_query.model_copy(
                update={"semantic_items": (selected_item,)}
            )
        }
    )

    assert not _research_loop_module._runtime_exact_formula_continuation_source_ids(
        selected_state, runtime_exact_documents
    )
    assert not _research_loop_module._exact_document_formula_continuation_source_ids(
        selected_state, fresh_document_context
    )

    different_formula_item = selected_item.model_copy(
        update={
            "normalized_meaning": (
                "documented rate = CONCAT('a;c', value)"
            )
        }
    )
    different_formula_state = selected_state.model_copy(
        update={
            "query_spec": selected_state.query_spec.model_copy(
                update={"semantic_items": (different_formula_item,)}
            )
        }
    )
    assert _research_loop_module._has_pending_required_formula_continuation(
        different_formula_state, fresh_document_context, runtime_exact_documents
    )

    fallback_item = selected_item.model_copy(
        update={
            "binding_ids": (physical_binding.binding_id, derived.binding_id),
            "exact_formula_binding_id": None,
        }
    )
    fallback_state = derived_state.model_copy(
        update={
            "query_spec": derived_query.model_copy(
                update={"semantic_items": (fallback_item,)}
            )
        }
    )
    assert not _research_loop_module._has_pending_required_formula_continuation(
        fallback_state, fresh_document_context, runtime_exact_documents
    )

    for invalid_binding in (
        derived.model_copy(update={"binding_id": "candidate-formula", "status": BindingStatus.CANDIDATE}),
        derived.model_copy(
            update={
                "binding_id": "foreign-formula",
                "source_id": "foreign-formula-source",
            }
        ),
        derived.model_copy(
            update={
                "binding_id": "other-document-formula",
                "document": DocumentRef(document_id="other-formula-rule", namespace="main"),
            }
        ),
    ):
        invalid_item = selected_item.model_copy(
            update={"exact_formula_binding_id": invalid_binding.binding_id}
        )
        invalid_state = state.model_copy(
            update={
                "query_spec": derived_query.model_copy(
                    update={"semantic_items": (invalid_item,)}
                ),
                "bindings": (*state.bindings, invalid_binding),
            }
        )
        assert _research_loop_module._has_pending_required_formula_continuation(
            invalid_state, fresh_document_context, runtime_exact_documents
        )


def test_formula_part_extracts_human_label_but_preserves_sql_equalities() -> None:
    formula = "CONCAT('a;b', value)"
    inline_where_dsl = "DIVIDE(COUNT(entity_id WHERE YEAR(event_at)=2020), COUNT(entity_id))"

    assert _research_loop_module._formula_part(f"documented rate = {formula}") == (
        "CONCAT('a;b',value)"
    )
    assert _research_loop_module._formula_part(
        "documented rate = CONCAT('a'';b', value); explanation"
    ) == "CONCAT('a'';b',value)"
    assert _research_loop_module._formula_part(
        "documented rate = DIVIDE(COUNT(record_id WHERE code = 'A=B'), COUNT(record_id))*100"
    ) == "DIVIDE(COUNT(record_idWHEREcode='A=B'),COUNT(record_id))*100"
    assert _research_loop_module._formula_part("value = threshold") == "value=threshold"
    assert _research_loop_module._formula_part(
        "temperature <= 15 OR temperature >= 30"
    ) == "temperature<=15ORtemperature>=30"
    assert _research_loop_module._formula_part(
        "temperature != 20"
    ) == "temperature!=20"
    assert _research_loop_module._formula_part(
        "temperature == 20"
    ) == "temperature==20"
    assert _research_loop_module._formula_part("DIVIDE(COUNT(value = threshold), COUNT(id))") == (
        "DIVIDE(COUNT(value=threshold),COUNT(id))"
    )
    assert _research_loop_module._formula_part(
        "AVG(measure) computed over qualifying rows where category = 'X'; scalar"
    ) == "AVG(measure)"
    assert _research_loop_module._formula_part(
        "AVG(measure) FILTER (WHERE category = 'X'); scalar"
    ) == "AVG(measure)FILTER(WHEREcategory='X')"
    assert _research_loop_module._formula_part(
        "AVG(measure) FILTER (WHERE category = 'X') computed over rows; scalar"
    ) == "AVG(measure)FILTER(WHEREcategory='X')"
    assert _research_loop_module._formula_part(
        "SUM(category = 'two words') - SUM(category = 'O''Brien')"
    ) == "SUM(category='two words')-SUM(category='O''Brien')"
    assert _research_loop_module._formula_part(
        'CONCAT("two words", "O""Brien")'
    ) == 'CONCAT("two words","O""Brien")'
    assert _research_loop_module._formula_part("AVG(measure) +; scalar") == "AVG(measure)+"
    assert _research_loop_module._formula_part(f"{inline_where_dsl} FROM records") == (
        "DIVIDE(COUNT(entity_idWHEREYEAR(event_at)=2020),COUNT(entity_id))"
    )
    assert _research_loop_module._formula_part(
        f"{inline_where_dsl} FROM main.records"
    ) == "DIVIDE(COUNT(entity_idWHEREYEAR(event_at)=2020),COUNT(entity_id))"
    assert _research_loop_module._formula_part(
        "DIVIDE(COUNT('FROM records'), COUNT(entity_id))"
    ) == "DIVIDE(COUNT('FROM records'),COUNT(entity_id))"
    assert _research_loop_module._formula_part(
        f"{inline_where_dsl} FROM records extra"
    ) == "DIVIDE(COUNT(entity_idWHEREYEAR(event_at)=2020),COUNT(entity_id))FROMrecordsextra"
    assert _research_loop_module._formula_part("AVG(measure) FROM records") == (
        "AVG(measure)FROMrecords"
    )


def test_exact_formula_document_matching_preserves_literal_whitespace() -> None:
    _, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    formula = "SUM(category = 'two words')"
    formula_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": formula,
            "required": True,
            "status": SemanticItemStatus.RESOLVED,
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (formula_item,)}
            )
        }
    )
    exact_document = SchemaEvidenceDocument(
        document_id="literal-rule",
        namespace="main",
        schema_namespace_version=state.schema_namespace_version,
        source_version="v1",
        title="Literal rule",
        content="Exact formula: SUM(category = 'two words').",
        target=None,
    )
    compacted_literal_document = exact_document.model_copy(
        update={
            "document_id": "compacted-literal-rule",
            "content": "Exact formula: SUM(category = 'twowords').",
        }
    )

    assert _exact_formula_documents(state, (exact_document,)) == (
        (formula_item.source_id, DocumentRef(document_id="literal-rule", namespace="main")),
    )
    assert not _exact_formula_documents(state, (compacted_literal_document,))


@pytest.mark.parametrize(
    ("formula", "is_exact"),
    (
        ("COUNT(entry_id), SUM(total_amount)", False),
        ("A, B", False),
        (
            "DIVIDE(COUNT(entry_id WHERE category_code = 'x'), COUNT(entry_id))",
            True,
        ),
        ("DIVIDE(COUNT(entry_id)), COUNT(entry_id)", True),
        (
            "DIVIDE(COUNT(entry_id)), COUNT(entry_id), COUNT(other_entry_id)",
            False,
        ),
        ("CONCAT('a,b', value)", True),
        ('CONCAT("a,""b", value)', True),
    ),
)
def test_exact_formula_document_requires_one_root_expression(
    formula: str,
    is_exact: bool,
) -> None:
    _, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": formula,
            "required": True,
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            )
        }
    )
    document = SchemaEvidenceDocument(
        document_id="one-root-formula-rule",
        namespace="main",
        schema_namespace_version=state.schema_namespace_version,
        source_version="v1",
        title="One root formula",
        content=f"Exact formula: {formula}.",
        target=None,
    )

    assert bool(_exact_formula_documents(state, (document,))) is is_exact


def test_document_formula_predicate_constraint_skips_logical_formula_predicates() -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    formula = (
        "COUNT(record_id WHERE ignored_label = 'ignore'), "
        "COUNT(record_id WHERE canonical_label = 'target')"
    )
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": formula,
            "required": True,
        }
    )
    state = state.model_copy(
        update={"query_spec": state.query_spec.model_copy(update={"semantic_items": (item,)})}
    )
    document = SchemaEvidenceDocument(
        document_id="predicate-rule",
        namespace="main",
        schema_namespace_version=state.schema_namespace_version,
        source_version="v1",
        title="Predicate rule",
        content=f"Exact formula: {formula}.",
        target=None,
    )
    schema = replace(
        loaded_schema,
        schema={
            "public.records": {
                "columns": {
                    "record_id": {"type": "integer"},
                    "ignored_label": {"type": "text"},
                    "canonical_label": {"type": "text"},
                    "proxy_label": {"type": "text"},
                }
            }
        },
    )

    assert _exact_formula_documents(state, (document,)) == ()
    assert not item.exact_physical_predicate
    assert not _production_research_module._exact_formula_predicate_constraints(
        state, (document,), schema
    )

    physical_item = item.model_copy(update={"exact_physical_predicate": True})
    physical_state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (physical_item,)}
            )
        }
    )
    assert _production_research_module._exact_formula_predicate_constraints(
        physical_state, (document,), schema
    ) == (
        (
            item.source_id,
            DocumentRef(document_id="predicate-rule", namespace="main"),
            "canonical_label",
            "target",
        ),
        (
            item.source_id,
            DocumentRef(document_id="predicate-rule", namespace="main"),
            "ignored_label",
            "ignore",
        ),
    )


def test_document_exact_formula_constrains_predicate_without_nlu_physical_flag() -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    formula = (
        "SUBTRACT(COUNT(record_id WHERE canonical_label = 'target'), "
        "COUNT(record_id))"
    )
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": formula,
            "required": True,
            "exact_physical_predicate": False,
        }
    )
    state = state.model_copy(
        update={"query_spec": state.query_spec.model_copy(update={"semantic_items": (item,)})}
    )
    document = SchemaEvidenceDocument(
        document_id="single-root-predicate-rule",
        namespace="main",
        schema_namespace_version=state.schema_namespace_version,
        source_version="v1",
        title="Single root predicate",
        content=formula,
        target=None,
    )
    schema = replace(
        loaded_schema,
        schema={
            "public.orders": {
                "columns": {
                    "record_id": {"type": "integer"},
                    "canonical_label": {"type": "text"},
                    "proxy_label": {"type": "text"},
                }
            }
        },
    )

    assert _exact_formula_documents(state, (document,)) == (
        (
            item.source_id,
            DocumentRef(document_id=document.document_id, namespace="main"),
        ),
    )
    constraints = _production_research_module._exact_formula_predicate_constraints(
        state, (document,), schema
    )
    assert constraints == (
        (
            item.source_id,
            DocumentRef(document_id=document.document_id, namespace="main"),
            "canonical_label",
            "target",
        ),
    )
    assert _research_loop_module._has_exact_formula_predicate_mismatch(
        state,
        _formula_predicate_decision(
            item.source_id,
            state.bindings[0].evidence_ids,
            column="proxy_label",
        ),
        constraints,
    )
    assert not _research_loop_module._has_exact_formula_predicate_mismatch(
        state,
        _formula_predicate_decision(
            item.source_id,
            state.bindings[0].evidence_ids,
            column="canonical_label",
        ),
        constraints,
    )
    duplicate_document = document.model_copy(
        update={"document_id": "second-single-root-predicate-rule"}
    )
    assert _exact_formula_documents(state, (document, duplicate_document)) == ()
    assert not _production_research_module._exact_formula_predicate_constraints(
        state, (document, duplicate_document), schema
    )


def test_runtime_exact_document_formula_rejects_complete_with_only_physical_binding(
    tmp_path,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(model_calls=3)
    initial = _policy_state(namespace)
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    physical_binding = state.bindings[0]
    formula = "DIVIDE(COUNT(record_id WHERE qualifying), COUNT(record_id))*100"
    formula_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": formula,
            "normalized_meaning": f"{formula}; qualifying rows share the ratio.",
            "required": True,
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (physical_binding.binding_id,),
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (formula_item,)}
            ),
            "budget_state": initial_budget_state(policy),
        }
    )
    initial = initial.model_copy(update={"budget_state": initial_budget_state(policy)})
    document = DocumentRef(document_id="formula-authority", namespace="main")
    responses = iter(
        (
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "complete",
                    "source_ids": [],
                    "citation_evidence_ids": [],
                },
            },
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_column",
                        "arguments": {
                            "table": "public.orders",
                            "column": "id",
                        },
                    },
                },
            },
        )
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(next(responses))

    state_store, checkpoint_store, ledger = _open_existing_research_state(
        tmp_path, initial, state
    )
    _seed_prior_model_budget(initial, ledger, policy)
    registry = _make_registry(namespace)
    try:
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks, *_args: canonical_digest(
                    current
                ),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=policy,
                semantic_repair_continuation=True,
                exact_formula_documents=((formula_item.source_id, document),),
            )
        )

        assert calls == 2
        assert registry.adapter.execute_calls == 1
        assert outcome.stop_reason is not ResearchStopReason.COMPLETE
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_exact_document_formula_normalizes_matching_derived_candidate_before_commit(
    tmp_path,
) -> None:
    """A trusted exact formula, rather than a model paraphrase, is persisted."""

    loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    physical_binding = state.bindings[0]
    assert isinstance(physical_binding, PhysicalColumnBinding)
    column = physical_binding.physical_column
    document = DocumentRef(document_id="schema-doc", namespace="main")
    document_action = ResearchAction(
        action_id="exact-formula-document-action",
        kind=ResearchActionKind.READ_DOCUMENT,
        hypothesis_id=None,
        target=document,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.READ_DOCUMENT,
            hypothesis_id=None,
            target=document,
            parameters=(),
            expected_revision=state.revision,
        ),
        expected_revision=state.revision,
    )
    formula = "status > MULTIPLY(AVG(status), 0.7)"
    document_payload = {
        "document": {
            "source_version": "doc-v1",
            "valid_until": _FIXTURE_NOW + timedelta(days=1),
        },
        "content": f"Exact formula: {formula}.",
        "title": "Schema guide",
    }
    document_result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id="exact-formula-document-evidence",
        action_digest=document_action.action_digest,
        probe_kind=document_action.kind,
        status=ProbeStatus.SUCCESS,
        target=document,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="trusted exact formula",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=len(canonical_json_bytes(document_payload)),
        ),
        row_count=0,
        payload=document_payload,
    )
    document_evidence = probe_result_to_evidence(document_result, document_action)
    assert document_evidence is not None
    formula_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": formula,
            "normalized_meaning": f"{formula}; explanatory text",
            "required": True,
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (physical_binding.binding_id,),
        }
    )
    state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (formula_item,)}
            ),
            "evidence": (*state.evidence, document_evidence),
            "bindings": (physical_binding,),
            "action_history": (*state.action_history, document_action),
            "unresolved_items": (),
        }
    )
    document_evidence_id = document_evidence.evidence_id
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:exact-formula",
                    "source_id": formula_item.source_id,
                    "candidate": {
                        "kind": "derived_expression",
                        "expression_claim": "status > 0.7 * AVG(status)",
                        "document_id": document.document_id,
                        "rule_excerpt": "status > 0.7 * AVG(status)",
                        "input_columns": (
                            {"table": "public.orders", "column": "status"},
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": (document_evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    state_store = AdaptiveResearchStateStore(tmp_path / "formula-state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "formula-checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "formula-budget.sqlite")
    freshness = FreshnessContext(
        evaluated_at=_FIXTURE_NOW,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
        document_sources=(
            DocumentSourceState(
                document_id=document.document_id,
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="doc-v1",
            ),
        ),
    )
    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=lambda _prompt: None,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=freshness,
        registry=_make_registry(namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "formula-owner",
        model_wait=None,
        exact_formula_documents=((formula_item.source_id, document),),
    )
    try:
        committed = commit_semantic_turn(
            coordinator._resolve_current_decision(state, decision).admission
        ).state
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    derived = next(
        binding
        for binding in committed.bindings
        if isinstance(binding, DerivedExpressionBinding)
    )
    normalized_formula = _research_loop_module._formula_part(formula)
    assert derived.expression.expression == normalized_formula
    assert derived.rule_excerpt == normalized_formula
    assert derived.input_columns == (column,)
    assert derived.evidence_ids == (document_evidence_id,)
    supported = derived.model_copy(
        update={
            "status": BindingStatus.SUPPORTED,
            "validator_rule": "semantic-certificate:v1:derived_expression",
        }
    )
    bindings = tuple(
        supported if binding.binding_id == derived.binding_id else binding
        for binding in committed.bindings
    )
    query_spec = _state_module._derive_query_spec(
        committed.query_spec, bindings, committed.revision + 1
    )
    committed = committed.model_copy(
        update={"bindings": bindings, "query_spec": query_spec}
    )
    assert query_spec.semantic_items[0].exact_formula_binding_id == derived.binding_id
    assert _research_loop_module._runtime_exact_formula_continuation_source_ids(
        committed, ((formula_item.source_id, document),)
    ) == ()


def test_pending_required_formula_continuation_supplies_stop_review_authority(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(model_calls=5)
    initial = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    supported = state.bindings[0]
    main_formula = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": "SUM(primary_value)",
            "normalized_meaning": "SUM(primary_value)",
            "required": True,
            "binding_ids": (supported.binding_id,),
        }
    )
    pending_formula = main_formula.model_copy(
        update={
            "source_id": "source-2",
            "source_text": "SUM(secondary_value)",
            "normalized_meaning": "SUM(secondary_value)",
            "status": SemanticItemStatus.UNRESOLVED,
            "binding_ids": (),
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (main_formula, pending_formula)}
            ),
            "unresolved_items": (pending_formula.source_id,),
            "budget_state": initial_budget_state(policy),
        }
    )
    monkeypatch.setattr(
        _research_loop_module,
        "_invalid_complete_generation_authority",
        lambda *_args: None,
    )
    review_contexts: list[dict[str, object]] = []
    decision_calls = 0

    def research_context(
        _current: ResearchState,
        _feedbacks: tuple[str, ...],
        _rejected_duplicates: tuple[dict[str, object], ...] = (),
        _rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
        invalid_stop_generation_authority: (
            tuple[CoverageInputErrorCode, tuple[str, ...]] | None
        ) = None,
    ) -> str:
        context: dict[str, object] = {}
        if invalid_stop_generation_authority is not None:
            reason, source_ids = invalid_stop_generation_authority
            context["invalid_stop_generation_authority"] = {
                "reason_code": reason.value,
                "affected_source_ids": list(source_ids),
            }
        return json.dumps(context)

    async def decision_model(_prompt: str) -> str:
        nonlocal decision_calls
        decision_calls += 1
        if decision_calls == 3:
            return json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-2"],
                        "citation_evidence_ids": [state.evidence[0].evidence_id],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [state.evidence[0].evidence_id],
                            "missing_distinguishing_fact": "The formula binding is absent.",
                        },
                    },
                }
            )
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "complete",
                    "source_ids": [],
                    "citation_evidence_ids": [],
                },
            }
        )

    async def review_model(prompt: str) -> str:
        review_contexts.append(
            json.loads(json.loads(prompt)["input"]["research_context"])
        )
        return '{"decision":"continue","hint":"Preserve the pending formula."}'

    state_store, checkpoint_store, ledger = _open_existing_research_state(
        tmp_path, initial, state
    )
    _seed_prior_model_budget(initial, ledger, policy)
    registry = _make_registry(namespace)
    try:
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=research_context,
                model=decision_model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=policy,
                semantic_repair_continuation=True,
                stop_review_model=review_model,
            )
        )
        assert decision_calls == 3
        assert review_contexts[0]["invalid_stop_generation_authority"] == {
            "reason_code": "QUERY_REQUIREMENT_INCOMPLETE",
            "affected_source_ids": ["source-2"],
        }
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_formula_candidate_continuation_assesses_detached_input(tmp_path) -> None:
    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    supported_state = state
    supported = state.bindings[0]
    formula = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": "status derived from two physical inputs",
            "normalized_meaning": "derived status",
        }
    )
    candidate_column = supported.physical_column.model_copy(update={"column": "id"})
    candidate_action = ResearchAction(
        action_id="formula-input-inspection",
        kind=ResearchActionKind.INSPECT_COLUMN,
        hypothesis_id=None,
        target=candidate_column,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_COLUMN,
            hypothesis_id=None,
            target=candidate_column,
            parameters=(),
            expected_revision=state.revision,
        ),
        expected_revision=state.revision,
    )
    candidate_payload = {
        "status": "matched",
        "column": candidate_column.model_dump(mode="json", by_alias=True),
    }
    candidate_result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id="formula-input-evidence",
        action_digest=candidate_action.action_digest,
        probe_kind=candidate_action.kind,
        status=ProbeStatus.SUCCESS,
        target=candidate_column,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="trusted formula input observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(candidate_payload)),
        ),
        row_count=1,
        payload=candidate_payload,
    )
    candidate_evidence = probe_result_to_evidence(candidate_result, candidate_action)
    assert candidate_evidence is not None
    candidate = PhysicalColumnBinding(
        binding_id="binding-formula-input",
        source_id=supported.source_id,
        tables=(candidate_column.table,),
        columns=(candidate_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(candidate_evidence.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=candidate_column,
    )
    state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        formula.model_copy(
                            update={
                                "binding_ids": (
                                    candidate.binding_id,
                                ),
                                "status": SemanticItemStatus.PARTIALLY_RESOLVED,
                            }
                        ),
                    )
                }
            ),
            "evidence": (*state.evidence, candidate_evidence),
            "bindings": (candidate,),
            "action_history": (*state.action_history, candidate_action),
            "unresolved_items": (formula.source_id,),
        }
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": candidate.binding_id,
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": (candidate_evidence.evidence_id,),
                    },
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    database = tmp_path / "adaptive.sqlite"
    checkpoint_key = AdaptiveCheckpointKey(
        state.run_id,
        state.run_incarnation,
        AdaptiveLoopKind.RESEARCH,
        state.revision - 1,
    )
    _seed_honest_v2_history(
        database,
        states=(initial, supported_state, state),
        events=(
            (checkpoint_key, "planned", {"kind": "seed"}),
            (checkpoint_key, "observed", {"kind": "seed"}),
        ),
    )
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    _seed_prior_model_budget(initial, ledger)
    _seed_prior_model_budget(state, ledger, revision=1)
    outcome = asyncio.run(
        run_research_loop(
            initial_state=state,
            task="research schema",
            research_context=lambda current, _feedbacks: canonical_digest(current),
            model=model,
            model_identity="test/model",
            adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=_policy(),
            semantic_repair_continuation=True,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.COMPLETE
        assert calls == 1
        assert all(
            binding.status is BindingStatus.SUPPORTED
            for binding in outcome.final_state.bindings
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_formula_continuation_consumes_latest_unbound_probe_evidence(tmp_path) -> None:
    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    supported_state = _supported_state_after_probe(
        namespace, observed_at=_FIXTURE_NOW
    )
    supported = supported_state.bindings[0]
    formula = supported_state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": "percentage for the selected status",
            "normalized_meaning": "selected amount / total amount * 100",
        }
    )
    value_column = supported.physical_column.model_copy(update={"column": "id"})
    value_action = ResearchAction(
        action_id="formula-value-search",
        kind=ResearchActionKind.SEARCH_VALUE,
        hypothesis_id=None,
        target=value_column,
        parameters=(("top_k", 1), ("value", "selected")),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.SEARCH_VALUE,
            hypothesis_id=None,
            target=value_column,
            parameters=(("top_k", 1), ("value", "selected")),
            expected_revision=supported_state.revision,
        ),
        expected_revision=supported_state.revision,
    )
    value_payload = {
        "columns": [value_column.column],
        "rows": [["selected"]],
    }
    value_result = build_probe_result(
        run_id=supported_state.run_id,
        run_incarnation=supported_state.run_incarnation,
        revision=supported_state.revision,
        schema_namespace_version=supported_state.schema_namespace_version,
        invocation_id="formula-value-evidence",
        action_digest=value_action.action_digest,
        probe_kind=value_action.kind,
        status=ProbeStatus.SUCCESS,
        target=value_column,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="trusted formula value observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(value_payload)),
        ),
        row_count=1,
        payload=value_payload,
    )
    value_evidence = probe_result_to_evidence(value_result, value_action)
    assert value_evidence is not None
    state = supported_state.model_copy(
        update={
            "revision": supported_state.revision + 1,
            "query_spec": supported_state.query_spec.model_copy(
                update={"semantic_items": (formula,)}
            ),
            "evidence": (*supported_state.evidence, value_evidence),
            "action_history": (*supported_state.action_history, value_action),
        }
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "complete",
                    "source_ids": [],
                    "citation_evidence_ids": [value_evidence.evidence_id],
                },
            }
        )

    database = tmp_path / "adaptive.sqlite"
    checkpoint_key = AdaptiveCheckpointKey(
        state.run_id,
        state.run_incarnation,
        AdaptiveLoopKind.RESEARCH,
        state.revision - 1,
    )
    _seed_honest_v2_history(
        database,
        states=(initial, supported_state, state),
        events=(
            (checkpoint_key, "planned", {"kind": "seed"}),
            (checkpoint_key, "observed", {"kind": "seed"}),
        ),
    )
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    _seed_prior_model_budget(initial, ledger)
    _seed_prior_model_budget(supported_state, ledger, revision=1)
    outcome = asyncio.run(
        run_research_loop(
            initial_state=state,
            task="research schema",
            research_context=lambda current, _feedbacks: canonical_digest(current),
            model=model,
            model_identity="test/model",
            adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=_policy(),
            semantic_repair_continuation=True,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.COMPLETE
        assert calls == 1

        async def replay_model(_prompt: str) -> str:
            raise AssertionError("terminal replay must not call the model")

        replay = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=replay_model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=_make_registry(namespace),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
                semantic_repair_continuation=True,
            )
        )
        assert replay == outcome
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_complete_uses_captured_document_freshness(tmp_path, monkeypatch) -> None:
    t0 = _FIXTURE_NOW
    t1 = datetime(2026, 7, 31, 12, 1, tzinfo=UTC)
    expires_at = datetime(2026, 7, 31, 12, 2, tzinfo=UTC)
    _, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state, document = _document_supported_state_after_probe(
        namespace,
        observed_at=t1,
        valid_until=expires_at,
    )
    freshness = FreshnessContext(
        evaluated_at=t0,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
        document_sources=(
            DocumentSourceState(
                document_id=document.document_id,
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    real_authority = _research_loop_module.evaluate_research_generation_authority
    evaluated_at: list[datetime] = []

    def capture_authority(*args, **kwargs):
        evaluated_at.append(args[1].evaluated_at)
        return real_authority(*args, **kwargs)

    monkeypatch.setattr(
        _research_loop_module,
        "evaluate_research_generation_authority",
        capture_authority,
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("expired document cannot be completed")

    state_store, checkpoint_store, ledger = _open_existing_research_state(
        tmp_path, initial, state
    )
    outcome = asyncio.run(
        run_research_loop(
            initial_state=state,
            task="research schema",
            research_context=lambda current, _feedbacks: canonical_digest(current),
            model=model,
            model_identity="test/model",
            adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
            loaded_schema=object(),
            freshness_context=freshness,
            registry=object(),
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=_policy(),
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.COMPLETE
        assert calls == 0
        assert evaluated_at and set(evaluated_at) == {t0}
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_completeness_stop_preserves_authority_reason_and_affected_sources(
    tmp_path, monkeypatch
) -> None:
    expected = ResearchGenerationAuthority(
        allowed=False,
        status=ResearchGenerationAuthorityStatus.DEFERRED,
        reason=CoverageInputErrorCode.SCHEMA_NAMESPACE_MISMATCH,
        affected_source_ids=("source-1",),
        requirements=None,
    )
    monkeypatch.setattr(
        _research_loop_module,
        "evaluate_research_generation_authority",
        lambda *_args: expected,
    )

    async def model(_prompt: str) -> str:
        raise AssertionError("authority protocol failure must stop before the model")

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, _state(required=True), model)
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        assert outcome.affected_source_ids == expected.affected_source_ids
        terminal = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                "loop-run", "loop-incarnation", AdaptiveLoopKind.RESEARCH, 0
            )
        ).terminal
        assert terminal is not None
        assert terminal.action["reason"] == outcome.stop_reason.value
        assert terminal.action["affected_source_ids"] == list(
            expected.affected_source_ids
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_authority_mapper_fails_closed_for_forged_runtime_result() -> None:
    forged = object.__new__(ResearchGenerationAuthority)
    object.__setattr__(forged, "allowed", False)
    object.__setattr__(forged, "status", ResearchGenerationAuthorityStatus.DEFERRED)
    object.__setattr__(forged, "reason", "UNKNOWN")
    object.__setattr__(forged, "affected_source_ids", ())
    object.__setattr__(forged, "requirements", None)

    assert _authority_stop_reason(forged) is ResearchStopReason.PROTOCOL_FAILURE


def test_terminal_envelope_rejects_string_subclass_ids() -> None:
    class _Text(str):
        pass

    with pytest.raises(ValueError):
        _terminal_envelope(
            {
                "affected_source_ids": [_Text("source-1")],
                "citation_evidence_ids": [],
                "contract_version": 1,
                "kind": "research_terminal",
                "reason": ResearchStopReason.AMBIGUOUS.value,
            },
            _state(required=True),
        )


def test_terminal_envelope_rejects_v1_without_a_fallback() -> None:
    with pytest.raises(ValueError, match="invalid contract"):
        _terminal_envelope(
            {
                "affected_source_ids": ["source-1"],
                "ambiguity": None,
                "citation_evidence_ids": [],
                "contract_version": 1,
                "kind": "research_terminal",
                "rejection_signatures": [],
                "reason": ResearchStopReason.STAGNATED.value,
            },
            _state(required=True),
        )


@pytest.mark.parametrize(
    ("required", "reason"),
    (
        (True, ResearchStopReason.COMPLETE),
        (False, ResearchStopReason.AMBIGUOUS),
    ),
)
def test_terminal_replay_cannot_bypass_current_authority(
    tmp_path, required: bool, reason: ResearchStopReason
) -> None:
    state = _state(required=required)
    database = tmp_path / "adaptive.sqlite"
    key = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    terminal = {
        "affected_source_ids": [],
        "citation_evidence_ids": [],
        "contract_version": 1,
        "kind": "research_terminal",
        "reason": reason.value,
    }
    _seed_honest_v2_history(
        database,
        states=(state,),
        events=((key, "terminal", terminal),),
    )
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    async def model(_prompt: str) -> str:
        raise AssertionError("terminal replay must not call the model")

    try:
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=object(),
                freshness_context=_freshness(state),
                registry=object(),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize("reason", ("ambiguous", "unsupported"))
def test_model_semantic_stop_accepts_blocking_affected_source_subset(
    tmp_path, reason: str
) -> None:
    initial, state, loaded_schema, namespace = _two_unresolved_states()
    database = tmp_path / "adaptive.sqlite"
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        database,
        states=(initial, state),
        events=(
            (seed, "planned", {"kind": "seed"}),
            (seed, "observed", {"kind": "seed"}),
        ),
    )
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    ambiguity = (
        ',"ambiguity":{"interpretations":["First reading.","Second reading."],'
        '"citation_evidence_ids":["evidence-1"],'
        '"missing_distinguishing_fact":"The definition is absent."}'
        if reason == "ambiguous"
        else ""
    )

    async def model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            f'{{"next_kind":"stop","reason":"{reason}",'
            '"source_ids":["source-1"],"citation_evidence_ids":["evidence-1"]'
            f"{ambiguity}}}}}"
        )

    async def review_model(_prompt: str) -> str:
        return '{"decision":"stop_confirmed","hint":null}'

    try:
        _seed_prior_model_budget(state, ledger)
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=_make_registry(namespace),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
                stop_review_model=review_model,
            )
        )
        assert outcome.stop_reason is ResearchStopReason(reason.upper())
        assert outcome.affected_source_ids == ("source-1",)
        records = ledger.load_model_records(state.run_id, state.run_incarnation)
        assert len(records) == 3
        for record in records:
            assert record.reconciliation is not None
            assert record.reconciliation.actual_usage == ModelTokenUsage(
                input_tokens=None,
                output_tokens=None,
            )
            assert record.reconciliation.charged_input_tokens == 10
            assert record.reconciliation.charged_output_tokens == 10
            assert record.reconciliation.charged_total_tokens == 20
            assert record.reconciliation.usage_was_conservative is True
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id,
                    state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    state.revision,
                )
            ).terminal
            is not None
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize("reason", ("ambiguous", "unsupported"))
def test_model_semantic_stop_and_replay_preserve_exact_affected_sources(
    tmp_path, reason: str
) -> None:
    initial, state, loaded_schema, namespace = _two_unresolved_states()
    ambiguity = (
        ',"ambiguity":{"interpretations":["First reading.","Second reading."],'
        '"citation_evidence_ids":["evidence-1"],'
        '"missing_distinguishing_fact":"The definition is absent."}'
        if reason == "ambiguous"
        else ""
    )

    async def model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            f'{{"next_kind":"stop","reason":"{reason}",'
            '"source_ids":["source-2","source-1"],'
            '"citation_evidence_ids":["evidence-1"]'
            f"{ambiguity}}}}}"
        )

    database = tmp_path / "adaptive.sqlite"
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        database,
        states=(initial, state),
        events=(
            (seed, "planned", {"kind": "seed"}),
            (seed, "observed", {"kind": "seed"}),
        ),
    )
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")

    async def replay_model(_prompt: str) -> str:
        raise AssertionError("terminal replay must not call the model")

    try:
        _seed_prior_model_budget(state, ledger)
        common = {
            "initial_state": state,
            "task": "research schema",
            "research_context": lambda current, _feedbacks: canonical_digest(current),
            "model_identity": "test/model",
            "adapter": SchemaResearchDecisionAdapter(
                load_schema_research_agent_profile()
            ),
            "loaded_schema": loaded_schema,
            "freshness_context": _fixture_freshness(state),
            "registry": _make_registry(namespace),
            "state_store": state_store,
            "checkpoint_store": checkpoint_store,
            "budget_ledger": ledger,
            "policy": _policy(),
        }
        first = asyncio.run(run_research_loop(model=model, **common))
        replay = asyncio.run(run_research_loop(model=replay_model, **common))
        expected_reason = ResearchStopReason(reason.upper())
        assert first.stop_reason is expected_reason
        assert first.affected_source_ids == ("source-1", "source-2")
        assert first.citation_evidence_ids == ("evidence-1",)
        assert replay == first
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize(
    "terminal_updates",
    (
        {"affected_source_ids": ["source-1", "source-1"]},
        {"affected_source_ids": ["source-z"]},
        {"affected_source_ids": [1]},
        {"citation_evidence_ids": ["missing-evidence"]},
        {"citation_evidence_ids": [1]},
    ),
)
def test_corrupt_terminal_replay_fails_closed(
    tmp_path, terminal_updates: dict[str, object]
) -> None:
    state = _state(required=True)
    terminal = {
        "affected_source_ids": ["source-1"],
        "citation_evidence_ids": [],
        "contract_version": 1,
        "kind": "research_terminal",
        "reason": ResearchStopReason.AMBIGUOUS.value,
        **terminal_updates,
    }
    database = tmp_path / "adaptive.sqlite"
    key = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        database,
        states=(state,),
        events=((key, "terminal", terminal),),
    )
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")

    async def model(_prompt: str) -> str:
        raise AssertionError("terminal replay must not call the model")

    try:
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=object(),
                freshness_context=_freshness(state),
                registry=object(),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize("forgery", ("policy", "usage"))
def test_empty_model_ledger_rejects_forged_budget_before_terminal(
    tmp_path, forgery: str
) -> None:
    state = _state(required=False)
    active_policy = _policy()
    if forgery == "policy":
        limits = ModelBudgetLimits(
            model_calls=3,
            input_tokens_per_call=10,
            output_tokens_per_call=10,
            total_tokens=60,
        )
        active_policy = AdaptivePolicyConfig(
            policy_version=2,
            wall_clock=WallClockBudget(wall_clock_seconds=10),
            resource_limits=ResourceBudget(model_tokens=60, db_probe_ms=1_000),
            operation_counts=OperationCountBudget(
                actions=4, model_decisions=3, db_probes=4
            ),
            result_volume=ResultVolumeBudget(returned_rows=10, inline_bytes=1_000),
            per_action=PerActionBudget(sample_rows=1),
            model_budget=limits,
        )
    else:
        budget_values = state.budget_state.model_dump(mode="python")
        budget_values.update(
            used_model_calls=1,
            remaining_model_calls=3,
            used_model_tokens=20,
            remaining_model_tokens=60,
        )
        state = state.model_copy(
            update={
                "budget_state": type(state.budget_state).model_validate(budget_values)
            }
        )

    called = False

    async def model(_prompt: str) -> str:
        nonlocal called
        called = True
        return "{}"

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, state, model, policy=active_policy)
    )
    try:
        assert called is False
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id,
                    state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    state.revision,
                )
            ).terminal
            is None
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_invalid_model_stop_exhausts_bounded_retries_without_state_revision(
    tmp_path,
) -> None:
    calls = 0
    prompts: list[str] = []

    async def model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        prompts.append(prompt)
        return (
                '{"decision_version":1,"proposals":[],"next":'
                '{"next_kind":"stop","reason":"ambiguous",'
                '"source_ids":["source-1"],"citation_evidence_ids":["citation-1"],'
                '"ambiguity":{"interpretations":["First reading.","Second reading."],'
                '"citation_evidence_ids":["citation-1"],'
                '"missing_distinguishing_fact":"The definition is absent."}}}'
        )

    state = _state(required=True)
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, state, model)
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert outcome.final_state.revision == state.revision
        assert outcome.final_state.budget_state.used_model_calls == 3
        assert outcome.final_state.budget_state.remaining_model_calls == 1
        assert outcome.final_state.budget_state.used_model_tokens == 60
        assert outcome.final_state.budget_state.remaining_model_tokens == 20
        assert outcome.affected_source_ids == ("source-1",)
        assert calls == 3
        assert len(prompts) == calls
        for prompt in prompts[1:2]:
            instructions = json.loads(prompt)["instructions"]
            assert (
                "Previous decision rejected: INVALID_STOP. Correct the decision using the "
                "profile rules and return a replacement typed decision."
                in instructions
            )
        assert '"review_kind":"research_stop_review"' in prompts[2]
        assert len(ledger.load_model_records(state.run_id, state.run_incarnation)) == 3
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
                )
            ).planned
            is None
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_unsupported_stop_accepts_one_blocking_required_source() -> None:
    _initial, state, _loaded_schema, _namespace = _two_unresolved_states()
    citation = state.evidence[0].evidence_id
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (),
            "next": {
                "next_kind": "stop",
                "reason": "unsupported",
                "source_ids": ("source-1",),
                "citation_evidence_ids": (citation,),
            },
        }
    )
    freshness = _fixture_freshness(state)

    assert (
        _research_loop_module._validate_model_stop(state, decision, freshness)
        is ResearchStopReason.UNSUPPORTED
    )
    assert _research_loop_module._terminal_replay_is_authorized(
        state,
        freshness,
        ResearchStopReason.UNSUPPORTED,
        {
            "affected_source_ids": ["source-1"],
            "ambiguity": None,
            "citation_evidence_ids": [citation],
            "contract_version": 2,
            "kind": "research_terminal",
            "rejection_signatures": [],
            "reason": "unsupported",
        },
    )


@pytest.mark.parametrize(
    ("source_ids", "expected_authority"),
    (
        (
            (),
            (
                CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
                ("source-1",),
            ),
        ),
        (("source-1",), None),
    ),
)
def test_invalid_complete_generation_authority_is_transient_retry_context_only(
    tmp_path,
    monkeypatch,
    source_ids,
    expected_authority,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    base_state = _policy_state(namespace, with_evidence=True)
    state = ResearchState.model_validate(
        {
            **base_state.model_dump(mode="python", round_trip=True),
            "revision": 0,
            "evidence": tuple(
                item.model_copy(update={"revision": 0}) for item in base_state.evidence
            ),
            "action_history": (),
        }
    )
    citation = state.evidence[0].evidence_id
    contexts: list[tuple[CoverageInputErrorCode, tuple[str, ...]] | None] = []
    prompts: list[str] = []
    authority = ResearchGenerationAuthority(
        allowed=False,
        status=ResearchGenerationAuthorityStatus.DEFERRED,
        reason=CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        affected_source_ids=("source-1",),
        requirements=None,
    )
    monkeypatch.setattr(
        _research_loop_module,
        "evaluate_research_generation_authority",
        lambda *_args: authority,
    )

    def research_context(
        current: ResearchState,
        feedbacks: tuple[str, ...],
        _rejected_duplicates: tuple[dict[str, object], ...] = (),
        _rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
        invalid_stop_generation_authority: (
            tuple[CoverageInputErrorCode, tuple[str, ...]] | None
        ) = None,
    ) -> str:
        contexts.append(invalid_stop_generation_authority)
        context = {"state": canonical_digest(current), "feedbacks": feedbacks}
        if invalid_stop_generation_authority is not None:
            reason_code, affected_source_ids = invalid_stop_generation_authority
            context["invalid_stop_generation_authority"] = {
                "reason_code": reason_code.value,
                "affected_source_ids": list(affected_source_ids),
            }
        return json.dumps(context)

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "complete",
                        "source_ids": source_ids,
                        "citation_evidence_ids": [citation],
                    },
                }
            )
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "ambiguous",
                    "source_ids": ["source-1"],
                    "citation_evidence_ids": [citation],
                    "ambiguity": {
                        "interpretations": ["First reading.", "Second reading."],
                        "citation_evidence_ids": [citation],
                        "missing_distinguishing_fact": "The definition is absent.",
                    },
                },
            }
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            research_context=research_context,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert contexts == [None, expected_authority]
        retry_context = json.loads(json.loads(prompts[1])["input"]["research_context"])
        if expected_authority is None:
            assert "invalid_stop_generation_authority" not in retry_context
        else:
            assert retry_context["invalid_stop_generation_authority"] == {
                "reason_code": "QUERY_REQUIREMENT_INCOMPLETE",
                "affected_source_ids": ["source-1"],
            }
        terminal = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            )
        ).terminal
        assert terminal is not None
        assert "invalid_stop_generation_authority" not in terminal.action
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_invalid_complete_authority_context_is_consumed_after_decode_failure(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, namespace = _fixture_schema()
    base_state = _policy_state(namespace, with_evidence=True)
    state = ResearchState.model_validate(
        {
            **base_state.model_dump(mode="python", round_trip=True),
            "revision": 0,
            "evidence": tuple(
                item.model_copy(update={"revision": 0}) for item in base_state.evidence
            ),
            "action_history": (),
        }
    )
    citation = state.evidence[0].evidence_id
    authority = ResearchGenerationAuthority(
        allowed=False,
        status=ResearchGenerationAuthorityStatus.DEFERRED,
        reason=CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
        affected_source_ids=("source-1",),
        requirements=None,
    )
    monkeypatch.setattr(
        _research_loop_module,
        "evaluate_research_generation_authority",
        lambda *_args: authority,
    )

    def research_context(
        current: ResearchState,
        feedbacks: tuple[str, ...],
        _rejected_duplicates: tuple[dict[str, object], ...] = (),
        _rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
        invalid_stop_generation_authority: (
            tuple[CoverageInputErrorCode, tuple[str, ...]] | None
        ) = None,
    ) -> str:
        context = {"state": canonical_digest(current), "feedbacks": feedbacks}
        if invalid_stop_generation_authority is not None:
            reason_code, affected_source_ids = invalid_stop_generation_authority
            context["invalid_stop_generation_authority"] = {
                "reason_code": reason_code.value,
                "affected_source_ids": list(affected_source_ids),
            }
        return json.dumps(context)

    prompts: list[str] = []

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "complete",
                        "source_ids": [],
                        "citation_evidence_ids": [citation],
                    },
                }
            )
        if len(prompts) == 2:
            return "{}"
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "ambiguous",
                    "source_ids": ["source-1"],
                    "citation_evidence_ids": [citation],
                    "ambiguity": {
                        "interpretations": ["First reading.", "Second reading."],
                        "citation_evidence_ids": [citation],
                        "missing_distinguishing_fact": "The definition is absent.",
                    },
                },
            }
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            research_context=research_context,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert len(prompts) == 3
        prompt_contexts = [
            json.loads(json.loads(prompt)["input"]["research_context"])
            for prompt in prompts
        ]
        assert "invalid_stop_generation_authority" not in prompt_contexts[0]
        assert prompt_contexts[1]["invalid_stop_generation_authority"] == {
            "reason_code": "QUERY_REQUIREMENT_INCOMPLETE",
            "affected_source_ids": ["source-1"],
        }
        assert "invalid_stop_generation_authority" not in prompt_contexts[2]
        key = AdaptiveCheckpointKey(
            state.run_id,
            state.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            state.revision,
        )
        terminal = checkpoint_store.get_snapshot(key).terminal
        replay_input = checkpoint_store.load_terminal_replay_input(key)
        assert terminal is not None
        assert type(replay_input) is ResearchTerminalReplayInput
        with sqlite3.connect(tmp_path / "adaptive.sqlite") as connection:
            snapshots = connection.execute(
                "SELECT payload FROM adaptive_research_state_snapshots"
            ).fetchall()
            checkpoints = connection.execute(
                "SELECT action_json FROM adaptive_checkpoint_events"
            ).fetchall()
            replay_inputs = connection.execute(
                "SELECT input_bytes FROM adaptive_checkpoint_replay_inputs"
            ).fetchall()
        assert all(b"invalid_stop_generation_authority" not in row[0] for row in snapshots)
        assert all("invalid_stop_generation_authority" not in row[0] for row in checkpoints)
        assert all(b"invalid_stop_generation_authority" not in row[0] for row in replay_inputs)
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_five_mixed_model_rejections_stop_without_state_progress(
    tmp_path, caplog, monkeypatch
) -> None:
    policy = _policy(6)
    state = _state(required=True).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    calls = 0
    prompts: list[str] = []
    monkeypatch.setattr(
        _research_loop_module,
        "_invalid_complete_generation_authority",
        lambda *_args: (
            CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE,
            ("source-1",),
        ),
    )

    stop_with_proposals = {
        "decision_version": 1,
        "proposals": [
            {
                "proposal_type": "new_binding",
                "proposal_key": "proposal:rejected",
                "source_id": "source-1",
                "candidate": {
                    "kind": "physical_column",
                    "physical_column": {"table": "orders", "column": "status"},
                },
                "join_references": [],
                "citation_evidence_ids": ["citation-1"],
            }
        ],
        "next": {
            "next_kind": "stop",
            "reason": "complete",
            "source_ids": [],
            "citation_evidence_ids": ["citation-1"],
        },
    }
    invalid_stop = {
        "decision_version": 1,
        "proposals": [],
        "next": {
            "next_kind": "stop",
            "reason": "complete",
            "source_ids": [],
            "citation_evidence_ids": ["citation-1"],
        },
    }

    def research_context(
        current: ResearchState,
        feedbacks: tuple[str, ...],
        _rejected_duplicates: tuple[dict[str, object], ...] = (),
        _rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
        invalid_stop_generation_authority: (
            tuple[CoverageInputErrorCode, tuple[str, ...]] | None
        ) = None,
    ) -> str:
        context: dict[str, object] = {
            "state": canonical_digest(current),
            "feedbacks": feedbacks,
        }
        if invalid_stop_generation_authority is not None:
            reason_code, affected_source_ids = invalid_stop_generation_authority
            context["invalid_stop_generation_authority"] = {
                "reason_code": reason_code.value,
                "affected_source_ids": list(affected_source_ids),
            }
        return json.dumps(context)

    async def model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        prompts.append(prompt)
        if '"review_kind":"research_stop_review"' in prompt:
            return '{"decision":"stop_confirmed","hint":null}'
        payload = stop_with_proposals if calls % 2 else invalid_stop
        return json.dumps(payload)

    with caplog.at_level(logging.WARNING, logger=_research_loop_module.__name__):
        outcome, state_store, checkpoint_store, ledger = asyncio.run(
            _run(
                tmp_path,
                state,
                model,
                policy=policy,
                research_context=research_context,
            )
        )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert outcome.final_state.revision == state.revision
        assert outcome.final_state.action_history == ()
        assert calls == 5
        assert len(ledger.load_model_records(state.run_id, state.run_incarnation)) == 5
        diagnostics = [
            record.message
            for record in caplog.records
            if record.name == _research_loop_module.__name__
            and record.message.startswith("typed_schema_research_decision ")
        ]
        assert diagnostics == [
            "typed_schema_research_decision retry=true "
            "code=STOP_WITH_PROPOSALS rejection_path=stop_with_proposals",
            "typed_schema_research_decision retry=true "
            "code=INVALID_STOP rejection_path=invalid_stop",
            "typed_schema_research_decision retry=true "
            "code=STOP_WITH_PROPOSALS rejection_path=stop_with_proposals",
            "typed_schema_research_decision retry=true "
            "code=INVALID_STOP rejection_path=invalid_stop",
        ]
        feedback = [json.loads(prompt)["instructions"] for prompt in prompts]
        assert "STOP_WITH_PROPOSALS" in feedback[1]
        assert "INVALID_STOP" in feedback[2]
        assert "STOP_WITH_PROPOSALS" in feedback[3]
        assert '"review_kind":"research_stop_review"' in prompts[4]
        review = json.loads(prompts[4])
        review_context = json.loads(review["input"]["research_context"])
        assert review_context["invalid_stop_generation_authority"] == {
            "reason_code": "QUERY_REQUIREMENT_INCOMPLETE",
            "affected_source_ids": ["source-1"],
        }
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id,
                    state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    state.revision,
                )
            ).planned
            is None
        )
        terminal = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            )
        ).terminal
        assert terminal is not None
        assert terminal.action["rejection_signatures"] == [
            ["invalid_stop", "INVALID_STOP"],
            ["stop_with_proposals", "STOP_WITH_PROPOSALS"],
        ]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_two_identical_contract_decode_rejections_trigger_stop_review(
    tmp_path,
) -> None:
    policy = _policy(8)
    state = _state(required=True).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    prompts: list[str] = []

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        if '"review_kind":"research_stop_review"' in prompt:
            return '{"decision":"stop_confirmed","hint":null}'
        return "{}"

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, state, model, policy=policy)
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert len(prompts) == 3
        assert '"review_kind":"research_stop_review"' in prompts[2]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_repeated_unresolvable_preflight_records_its_terminal_signature(tmp_path) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(6)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    decision = json.dumps(
        {
            "decision_version": 1,
            "proposals": [],
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.missing"},
                },
            },
        }
    )

    async def model(_prompt: str) -> str:
        return decision

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            policy=policy,
            loaded_schema=loaded_schema,
            registry=_make_registry(namespace),
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        terminal = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            )
        ).terminal
        assert terminal is not None
        assert terminal.action["rejection_signatures"] == [
            ["unresolvable_preflight", "UNRESOLVABLE_PREFLIGHT"]
        ]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_distinct_unresolvable_preflights_do_not_trigger_repeated_stop(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(6)
    base_state = _policy_state(namespace, with_evidence=True)
    state = ResearchState.model_validate(
        {
            **base_state.model_dump(mode="python", round_trip=True),
            "revision": 0,
            "evidence": tuple(
                item.model_copy(update={"revision": 0})
                for item in base_state.evidence
            ),
            "action_history": (),
            "budget_state": initial_budget_state(policy),
        }
    )

    def rejected_decision(label: str) -> str:
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_table",
                        "arguments": {"table": f"synthetic_{label}"},
                    },
                },
            }
        )

    responses = iter(
        (
            rejected_decision("one"),
            rejected_decision("two"),
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [state.evidence[0].evidence_id],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [state.evidence[0].evidence_id],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            ),
        )
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return next(responses)

    async def stop_review(self, state, reason, context, attempt):
        return None, attempt

    preflight_results = iter(
        (
            ("UNRESOLVABLE_PREFLIGHT", None, None, ()),
            ("UNRESOLVABLE_PREFLIGHT", None, None, ()),
        )
    )
    monkeypatch.setattr(
        _research_loop_module._ResearchLoopCoordinator,
        "_review_stop",
        stop_review,
    )
    monkeypatch.setattr(
        _research_loop_module._ResearchLoopCoordinator,
        "_preflight_model_decision",
        lambda _self, _state, _decision: next(preflight_results),
    )
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            policy=policy,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
        )
    )
    try:
        assert calls == 3
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize(
    "proposal",
    (
        {
            "proposal_type": "hypothesis_assessment",
            "subject": {
                "reference_kind": "existing",
                "hypothesis_id": "hypothesis-1",
            },
            "certificate": "consistent",
            "citation_evidence_ids": ("missing-evidence",),
        },
        {
            "proposal_type": "new_binding",
            "proposal_key": "proposal:premature-binding",
            "source_id": "source-1",
            "candidate": {
                "kind": "physical_column",
                "physical_column": {
                    "table": "public.orders",
                    "column": "status",
                },
            },
            "join_references": (),
            "citation_evidence_ids": ("missing-evidence",),
        },
    ),
    ids=("existing-assessment", "new-binding"),
)
def test_rejected_proposal_tool_decision_executes_admissible_baseline(
    tmp_path, monkeypatch, proposal: dict[str, object]
) -> None:
    """Rejected semantic proposals cannot block their independently valid tool."""

    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = initial.model_copy(
        update={
            "hypotheses": (
                Hypothesis(
                    hypothesis_id="hypothesis-1",
                    source_ids=("source-1",),
                    claim="orders are relevant",
                    candidate_targets=(
                        TableRef(namespace="main", schema="public", table="orders"),
                    ),
                    status=HypothesisStatus.PROPOSED,
                    evidence_ids=(),
                ),
            )
        }
    )
    baseline = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.customers"},
                },
            },
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            **baseline.model_dump(mode="python"),
            "proposals": (proposal,),
        }
    )
    registry = _make_registry(namespace)
    prepared = _resolve_fixture(
        baseline,
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=registry,
    )
    action = prepared.admission.action
    assert action is not None
    assert prepared.invocation is not None
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id=prepared.invocation.invocation_id,
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=action.target,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="fixture success",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=11,
        ),
        row_count=1,
        payload={"ok": True},
    )
    registry.adapter.result = NormalizedToolResult(
        "success", result.model_dump(mode="json", by_alias=True)
    )
    registry.adapter.recover = lambda _invocation: None
    responses = iter(
        (
            decision.model_dump_json(),
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [prepared.invocation.invocation_id],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [prepared.invocation.invocation_id],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                },
            ),
        )
    )
    prompts: list[str] = []

    def research_context(
        current: ResearchState,
        feedbacks: tuple[str, ...],
        rejected_duplicates: tuple[dict[str, object], ...] = (),
        rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
    ) -> str:
        return json.dumps(
            {
                "state": canonical_digest(current),
                "feedbacks": feedbacks,
                "rejected_duplicates": rejected_duplicates,
                "rejected_preflight_assessments": rejected_preflight_assessments,
            }
        )

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        if '"review_kind":"research_stop_review"' in prompt:
            return '{"decision":"stop_confirmed","hint":null}'
        return next(responses)

    ledger = AdaptiveBudgetLedger(tmp_path / "assessment-fallback-budget.sqlite")
    execute = _research_loop_module.execute_resolved_research_decision

    def execute_fresh_probe(resolved, tools, *, recover=False):
        observed = execute(resolved, tools, recover=recover)
        action = resolved.admission.action
        assert action is not None
        charged, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            observed.cost,
            lambda _reservation: observed,
            config=_policy(),
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "assessment-fallback-tool-owner",
        )
        return charged

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", execute_fresh_probe
    )
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            registry=registry,
            budget_ledger=ledger,
            research_context=research_context,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert outcome.final_state.hypotheses == state.hypotheses
        assert outcome.final_state.bindings == state.bindings
        assert outcome.final_state.action_history == (*state.action_history, action)
        assert len(prompts) == 3
        assert "cited evidence_id does not exist" in prompts[1]
        assert "missing-evidence" in prompts[1]
        assert registry.adapter.execute_calls == 1
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_proposal_free_tool_baseline_drops_unpersisted_hypothesis_reference() -> None:
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_hypothesis",
                    "proposal_key": "proposal:orders",
                    "source_ids": ("source-1",),
                    "claim": "orders are relevant",
                    "candidate_targets": (
                        {"target_kind": "table", "table": "public.orders"},
                    ),
                    "citation_evidence_ids": ("evidence-1",),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": {
                    "reference_kind": "proposed",
                    "proposal_key": "proposal:orders",
                },
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.orders"},
                },
            },
        }
    )

    baseline = _research_loop_module._proposal_free_tool_baseline(decision)

    assert baseline is not None
    assert baseline.proposals == ()
    assert type(baseline.next) is type(decision.next)
    assert baseline.next.hypothesis_ref is None
    assert baseline.next.intent == decision.next.intent


def test_unresolvable_binding_assessment_feedback_names_missing_column_probe(
    tmp_path,
) -> None:
    """A rejected assessment must not hide the valid next column inspection."""

    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    prior_evidence = state.evidence[0]
    assert isinstance(prior_evidence.target, TableRef)
    currency = ColumnRef(table=prior_evidence.target, column="status")
    customer_id = ColumnRef(table=prior_evidence.target, column="id")
    bindings = (
        *(
            DiscriminatorValueBinding(
                binding_id=f"binding-filter-{value.casefold()}",
                source_id="source-1",
                tables=(currency.table,),
                columns=(currency,),
                predicates=(
                    {
                        "left": currency,
                        "operator": PredicateOperator.EQ,
                        "right": value,
                    },
                ),
                join_path=(),
                evidence_ids=(prior_evidence.evidence_id,),
                confidence=0.0,
                status=BindingStatus.CANDIDATE,
                validator_rule=None,
                discriminator_column=currency,
                discriminator_predicate={
                    "left": currency,
                    "operator": PredicateOperator.EQ,
                    "right": value,
                },
            )
            for value in ("EUR", "CZK")
        ),
        *(
            PhysicalColumnBinding(
                binding_id=f"binding-metric-{suffix}",
                source_id="source-1",
                tables=(customer_id.table,),
                columns=(customer_id,),
                predicates=(),
                join_path=(),
                evidence_ids=(prior_evidence.evidence_id,),
                confidence=0.0,
                status=BindingStatus.CANDIDATE,
                validator_rule=None,
                physical_column=customer_id,
            )
            for suffix in ("eur", "czk")
        ),
    )
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "binding_ids": tuple(sorted(binding.binding_id for binding in bindings)),
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
        }
    )
    state = state.model_copy(
        update={
            "bindings": bindings,
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
        }
    )
    prompts: list[dict[str, object]] = []
    registry = _make_registry(namespace)

    async def model(prompt: str) -> str:
        envelope = json.loads(prompt)
        prompts.append(envelope)
        if len(prompts) == 1:
            return json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [
                        {
                            "proposal_type": "binding_assessment",
                            "subject": {
                                "reference_kind": "existing",
                                "binding_id": binding.binding_id,
                            },
                            "certificate": "consistent",
                            "citation_evidence_ids": [prior_evidence.evidence_id],
                        }
                        for binding in bindings
                    ],
                    "next": {
                        "next_kind": "tool",
                        "hypothesis_ref": None,
                        "intent": {
                            "tool_name": "inspect_column",
                            "arguments": {
                                "table": "public.orders",
                                "column": "status",
                            },
                        },
                    },
                }
            )
        if len(prompts) == 2:
            return json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [
                        {
                            "proposal_type": "binding_assessment",
                            "subject": {
                                "reference_kind": "existing",
                                "binding_id": binding.binding_id,
                            },
                            "certificate": "consistent",
                            "citation_evidence_ids": [prior_evidence.evidence_id],
                        }
                        for binding in bindings
                    ],
                    "next": {
                        "next_kind": "tool",
                        "hypothesis_ref": None,
                        "intent": {
                            "tool_name": "inspect_column",
                            "arguments": {
                                "table": "public.orders",
                                "column": "id",
                            },
                        },
                    },
                }
            )
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "ambiguous",
                    "source_ids": ("source-1",),
                    "citation_evidence_ids": [prior_evidence.evidence_id],
                    "ambiguity": {
                        "interpretations": ["First reading.", "Second reading."],
                        "citation_evidence_ids": [prior_evidence.evidence_id],
                        "missing_distinguishing_fact": "The definition is absent.",
                    },
                },
            }
        )

    ledger = AdaptiveBudgetLedger(tmp_path / "preflight-feedback-budget.sqlite")
    _seed_prior_model_budget(state, ledger)
    initial = _policy_state(namespace)
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        tmp_path / "adaptive.sqlite",
        states=(initial, state),
        events=(
            (seed, "planned", {"kind": "seed"}),
            (seed, "observed", {"kind": "seed"}),
        ),
    )
    registry = _make_registry(namespace)

    def research_context(
        current: ResearchState,
        _feedbacks: tuple[str, ...],
        _rejected_duplicates: tuple[dict[str, object], ...] = (),
        rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
    ) -> str:
        context: dict[str, object] = {"state": canonical_digest(current)}
        if rejected_preflight_assessments:
            context["rejected_preflight_assessments"] = list(
                rejected_preflight_assessments
            )
        return json.dumps(context)

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            registry=registry,
            budget_ledger=ledger,
            research_context=research_context,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.TOOL_FAILURE
        assert outcome.final_state.bindings == state.bindings
        assert len(prompts) == 1
        assert registry.adapter.execute_calls == 1
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_partial_filter_binding_assessment_feedback_names_all_join_routes(
    tmp_path,
) -> None:
    """A filter commit names every selected candidate route it must assess."""

    loaded_schema, namespace = _fixture_schema(
        {
            "public.orders": {"columns": {"currency": {"type": "TEXT"}}},
            "public.catalog": {"columns": {"currency": {"type": "TEXT"}}},
        }
    )
    base = _policy_state(namespace)
    orders = TableRef(namespace="main", schema="public", table="orders")
    catalog = TableRef(namespace="main", schema="public", table="catalog")
    currency = ColumnRef(table=orders, column="currency")
    route_left = ColumnRef(table=orders, column="currency")
    route_right = ColumnRef(table=catalog, column="currency")
    search = ResearchAction(
        action_id="fixed-currency-search",
        kind=ResearchActionKind.SEARCH_VALUE,
        hypothesis_id=None,
        target=currency,
        parameters=(("top_k", 1), ("value", "fixed")),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.SEARCH_VALUE,
            hypothesis_id=None,
            target=currency,
            parameters=(("top_k", 1), ("value", "fixed")),
            expected_revision=base.revision,
        ),
        expected_revision=base.revision,
    )
    payload = {"columns": ["currency"], "rows": [["fixed"]]}
    result = build_probe_result(
        run_id=base.run_id,
        run_incarnation=base.run_incarnation,
        revision=base.revision,
        schema_namespace_version=base.schema_namespace_version,
        invocation_id="fixed-currency-evidence",
        action_digest=search.action_digest,
        probe_kind=search.kind,
        status=ProbeStatus.SUCCESS,
        target=currency,
        started_at=_NOW,
        completed_at=_NOW,
        summary="trusted fixed currency observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )
    evidence = probe_result_to_evidence(result, search)
    assert evidence is not None
    predicate = PredicateRef(
        left=currency,
        operator=PredicateOperator.EQ,
        right="fixed",
    )
    direct_proposal = {
        "proposal_type": "new_binding",
        "proposal_key": "proposal:direct",
        "source_id": "source-1",
        "candidate": {
            "kind": "discriminator_value",
            "discriminator_column": {
                "table": "public.orders",
                "column": "currency",
            },
            "discriminator_predicate": {
                "left": {
                    "table": "public.orders",
                    "column": "currency",
                },
                "operator": PredicateOperator.EQ,
                "right": "fixed",
            },
        },
        "join_references": (),
        "citation_evidence_ids": (evidence.evidence_id,),
    }
    def candidate(
        binding_id: str, join_path: tuple[JoinEdge, ...]
    ) -> DiscriminatorValueBinding:
        return DiscriminatorValueBinding(
            binding_id=binding_id,
            source_id="source-1",
            tables=(orders, catalog) if join_path else (orders,),
            columns=(currency,),
            predicates=(predicate,),
            join_path=join_path,
            evidence_ids=(evidence.evidence_id,),
            confidence=0.0,
            status=BindingStatus.CANDIDATE,
            validator_rule=None,
            discriminator_column=currency,
            discriminator_predicate=predicate,
        )

    direct = candidate("binding:direct", ())
    via_catalog = candidate(
        "binding:via-catalog",
        (JoinEdge(left=route_left, right=route_right, join_type=JoinType.INNER),),
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FILTER,
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (direct.binding_id, via_catalog.binding_id),
        }
    )
    state = base.model_copy(
        update={
            "revision": 1,
            "evidence": (evidence,),
            "bindings": (direct, via_catalog),
            "action_history": (search,),
            "query_spec": base.query_spec.model_copy(
                update={"revision": 1, "semantic_items": (item,)}
            ),
        }
    )
    direct = next(
        binding
        for binding in _resolve_fixture(
            ResearchDecisionV1.model_validate(
                {
                    "decision_version": 1,
                    "proposals": (direct_proposal,),
                    "next": {"next_kind": "semantic_commit"},
                }
            ),
            loaded=loaded_schema,
            namespace=namespace,
            state=state,
            registry=_make_registry(namespace),
        ).admission.bindings
        if binding.binding_id not in (direct.binding_id, via_catalog.binding_id)
    )
    assert isinstance(direct, DiscriminatorValueBinding)
    item = item.model_copy(
        update={"binding_ids": (direct.binding_id, via_catalog.binding_id)}
    )
    state = state.model_copy(
        update={
            "bindings": (direct, via_catalog),
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
        }
    )

    def parsed_decision(*binding_ids: str) -> ResearchDecisionV1:
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": tuple(
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": binding_id,
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": (evidence.evidence_id,),
                    }
                    for binding_id in binding_ids
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    def decision(*binding_ids: str) -> str:
        return json.dumps(parsed_decision(*binding_ids).model_dump(mode="json"))

    def duplicate_direct_decision() -> str:
        return json.dumps(
            ResearchDecisionV1.model_validate(
                {
                    "decision_version": 1,
                    "proposals": (
                        {
                            **direct_proposal,
                            "proposal_key": "proposal:duplicate-direct",
                        },
                    ),
                    "next": {"next_kind": "semantic_commit"},
                }
            ).model_dump(mode="json")
        )

    calls = 0
    rejected_batches: list[tuple[dict[str, object], ...]] = []

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return (
            duplicate_direct_decision()
            if calls == 1
            else json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": [item.source_id],
                        "citation_evidence_ids": [evidence.evidence_id],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [evidence.evidence_id],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            )
        )

    def research_context(
        _current: ResearchState,
        _feedbacks: tuple[str, ...],
        _rejected_duplicates: tuple[dict[str, object], ...] = (),
        rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
    ) -> str:
        if rejected_preflight_assessments:
            rejected_batches.append(rejected_preflight_assessments)
        return canonical_digest(state)

    initial = _policy_state(namespace)
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        tmp_path / "adaptive.sqlite",
        states=(initial, state),
        events=(
            (seed, "planned", {"kind": "seed"}),
            (seed, "observed", {"kind": "seed"}),
        ),
    )
    supported = commit_semantic_turn(
        _resolve_fixture(
            parsed_decision(direct.binding_id, via_catalog.binding_id),
            loaded=loaded_schema,
            namespace=namespace,
            state=state,
            registry=_make_registry(namespace),
        ).admission
    ).state
    assert all(binding.status is BindingStatus.SUPPORTED for binding in supported.bindings)
    ledger = AdaptiveBudgetLedger(tmp_path / "partial-filter-feedback-budget.sqlite")
    _seed_prior_model_budget(state, ledger)
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            registry=_make_registry(namespace),
            research_context=research_context,
            budget_ledger=ledger,
        )
    )
    try:
        assert calls == 2
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert len(rejected_batches) == 1
        feedback = rejected_batches[0]
        assert len(feedback) == 1
        assert feedback[0]["source_id"] == item.source_id
        assert feedback[0]["rejection_reason"] == (
            "semantic commit leaves selected candidate bindings unassessed"
        )
        assert feedback[0]["required_binding_assessment_ids"] == sorted(
            (direct.binding_id, via_catalog.binding_id)
        )
        assert feedback[0]["unassessed_binding_ids"] == [via_catalog.binding_id]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize(
    ("operator", "right", "expected_values"),
    (
        (PredicateOperator.IN, ("EUR", "CZK"), ("EUR", "CZK")),
        (PredicateOperator.EQ, "EUR", ("EUR",)),
        (PredicateOperator.IS_NULL, None, (None,)),
    ),
    ids=("in", "eq", "is-null"),
)
def test_discriminator_feedback_requests_each_unobserved_literal_once(
    operator,
    right,
    expected_values,
) -> None:
    """A checked discriminator column needs exact evidence for each literal."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace)
    table = TableRef(namespace="main", schema="public", table="orders")
    currency = ColumnRef(table=table, column="currency")

    def action(
        action_id: str,
        kind: ResearchActionKind,
        parameters: tuple[tuple[str, object], ...],
    ) -> ResearchAction:
        return ResearchAction(
            action_id=action_id,
            kind=kind,
            hypothesis_id=None,
            target=currency,
            parameters=parameters,
            action_digest=canonical_action_digest(
                kind=kind,
                hypothesis_id=None,
                target=currency,
                parameters=parameters,
                expected_revision=base.revision,
            ),
            expected_revision=base.revision,
        )

    def evidence_for(
        research_action: ResearchAction,
        invocation_id: str,
        payload: dict[str, object],
    ):
        result = build_probe_result(
            run_id=base.run_id,
            run_incarnation=base.run_incarnation,
            revision=base.revision,
            schema_namespace_version=base.schema_namespace_version,
            invocation_id=invocation_id,
            action_digest=research_action.action_digest,
            probe_kind=research_action.kind,
            status=ProbeStatus.SUCCESS,
            target=currency,
            started_at=_NOW,
            completed_at=_NOW,
            summary="trusted discriminator observation",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=len(payload.get("rows", [])),
                bytes=len(canonical_json_bytes(payload)),
            ),
            row_count=len(payload.get("rows", [])),
            payload=payload,
        )
        evidence = probe_result_to_evidence(result, research_action)
        assert evidence is not None
        return evidence

    inspect = action("currency-inspection", ResearchActionKind.INSPECT_COLUMN, ())
    inspected = evidence_for(
        inspect,
        "currency-inspection-evidence",
        {
            "status": "matched",
            "column": currency.model_dump(mode="json", by_alias=True),
        },
    )
    binding = DiscriminatorValueBinding(
        binding_id="currency-filter",
        source_id="source-1",
        tables=(table,),
        columns=(currency,),
        predicates=(
            {
                "left": currency,
                "operator": operator,
                "right": right,
            },
        ),
        join_path=(),
        evidence_ids=(inspected.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=currency,
        discriminator_predicate={
            "left": currency,
            "operator": operator,
            "right": right,
        },
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": binding.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (inspected.evidence_id,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {"table": "public.orders", "column": "currency"},
                },
            },
        }
    )

    def feedback(evidence, actions):
        state = base.model_copy(
            update={
                "evidence": evidence,
                "bindings": (binding,),
                "action_history": actions,
            }
        )
        return _research_loop_module._rejected_preflight_assessment_context(
            state, decision, _freshness(state), requested_action=None
        )

    first = feedback((inspected,), (inspect,))
    assert first[0]["missing_probe"] == {
        "tool_name": "search_value",
        "arguments": {
            "table": "public.orders",
            "column": "currency",
            "value": expected_values[0],
            "top_k": 1,
        },
    }

    first_search = action(
        "first-search",
        ResearchActionKind.SEARCH_VALUE,
        (("top_k", 1), ("value", expected_values[0])),
    )
    first_evidence = evidence_for(
        first_search,
        "first-search-evidence",
        {"columns": ["currency"], "rows": [[expected_values[0]]]},
    )
    second = feedback((inspected, first_evidence), (inspect, first_search))
    if len(expected_values) == 1:
        assert "missing_probe" not in second[0]
    else:
        assert second[0]["missing_probe"]["arguments"]["value"] == expected_values[1]
        second_search = action(
            "second-search",
            ResearchActionKind.SEARCH_VALUE,
            (("top_k", 1), ("value", expected_values[1])),
        )
        final = feedback(
            (inspected, first_evidence),
            (inspect, first_search, second_search),
        )
        assert "missing_probe" not in final[0]


@pytest.mark.parametrize(
    "value",
    (float("nan"), float("inf"), float("-inf")),
    ids=("nan", "positive-infinity", "negative-infinity"),
)
def test_discriminator_feedback_rejects_nonfinite_float_literals(value) -> None:
    """A non-finite float cannot become a strict search_value probe."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace)
    table = TableRef(namespace="main", schema="public", table="orders")
    currency = ColumnRef(table=table, column="currency")
    inspect = ResearchAction(
        action_id="currency-inspection",
        kind=ResearchActionKind.INSPECT_COLUMN,
        hypothesis_id=None,
        target=currency,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_COLUMN,
            hypothesis_id=None,
            target=currency,
            parameters=(),
            expected_revision=base.revision,
        ),
        expected_revision=base.revision,
    )
    payload = {
        "status": "matched",
        "column": currency.model_dump(mode="json", by_alias=True),
    }
    result = build_probe_result(
        run_id=base.run_id,
        run_incarnation=base.run_incarnation,
        revision=base.revision,
        schema_namespace_version=base.schema_namespace_version,
        invocation_id="currency-inspection-evidence",
        action_digest=inspect.action_digest,
        probe_kind=inspect.kind,
        status=ProbeStatus.SUCCESS,
        target=currency,
        started_at=_NOW,
        completed_at=_NOW,
        summary="trusted discriminator observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=0,
        payload=payload,
    )
    inspected = probe_result_to_evidence(result, inspect)
    assert inspected is not None
    predicate = {
        "left": currency,
        "operator": PredicateOperator.EQ,
        "right": value,
    }
    binding = DiscriminatorValueBinding(
        binding_id="currency-filter",
        source_id="source-1",
        tables=(table,),
        columns=(currency,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=(inspected.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=currency,
        discriminator_predicate=predicate,
    )
    state = base.model_copy(
        update={
            "evidence": (inspected,),
            "bindings": (binding,),
            "action_history": (inspect,),
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": binding.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (inspected.evidence_id,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {"table": "public.orders", "column": "currency"},
                },
            },
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert "missing_probe" not in feedback[0]


def test_schema_known_discriminator_column_probes_typed_literal() -> None:
    """A loaded column still needs exact evidence only for its predicate value."""

    loaded_schema, namespace = _fixture_schema(
        {"public.catalog": {"columns": {"code": {"type": "TEXT"}}}}
    )
    base = _policy_state(namespace, with_evidence=True)
    table = TableRef(namespace="main", schema="public", table="catalog")
    code = ColumnRef(table=table, column="code")
    binding = DiscriminatorValueBinding(
        binding_id="catalog-code-filter",
        source_id="source-1",
        tables=(table,),
        columns=(code,),
        predicates=(
            {
                "left": code,
                "operator": PredicateOperator.EQ,
                "right": 7,
            },
        ),
        join_path=(),
        evidence_ids=(base.evidence[0].evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=code,
        discriminator_predicate={
            "left": code,
            "operator": PredicateOperator.EQ,
            "right": 7,
        },
    )
    state = base.model_copy(update={"bindings": (binding,)})
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": binding.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (base.evidence[0].evidence_id,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "search_value",
                    "arguments": {
                        "table": "public.catalog",
                        "column": "code",
                        "value": 7,
                        "top_k": 1,
                    },
                },
            },
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    )

    assert feedback[0]["missing_probe"] == {
        "tool_name": "search_value",
        "arguments": {
            "table": "public.catalog",
            "column": "code",
            "value": 7,
            "top_k": 1,
        },
    }


def test_missing_column_probe_is_not_recommended_after_failed_inspection() -> None:
    """A prior inspect action is enough even when it produced no evidence."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    table = TableRef(namespace="main", schema="public", table="orders")
    column = ColumnRef(table=table, column="status")
    failed_action = ResearchAction(
        action_id="failed-status-inspection",
        kind=ResearchActionKind.INSPECT_COLUMN,
        hypothesis_id=None,
        target=column,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_COLUMN,
            hypothesis_id=None,
            target=column,
            parameters=(),
            expected_revision=1,
        ),
        expected_revision=1,
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", by_alias=True, round_trip=True),
            "revision": 2,
            "action_history": (*base.action_history, failed_action),
        }
    )
    evidence_id = state.evidence[0].evidence_id
    binding = PhysicalColumnBinding(
        binding_id="status-binding",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )

    assert state.evidence[0].target != column
    assert _missing_binding_column_probe(binding, state.evidence, state.action_history) is None


def test_derived_binding_feedback_names_missing_canonical_input_column() -> None:
    """A formula assessment must receive its first missing input inspection."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace)
    patient = TableRef(namespace="main", schema=None, table="Patient")
    patient_id = ColumnRef(table=patient, column="ID")
    patient_name = ColumnRef(table=patient, column="Name")
    document = DocumentRef(document_id="patient-rule", namespace="main")
    document_action = ResearchAction(
        action_id="patient-rule-action",
        kind=ResearchActionKind.READ_DOCUMENT,
        hypothesis_id=None,
        target=document,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.READ_DOCUMENT,
            hypothesis_id=None,
            target=document,
            parameters=(),
            expected_revision=0,
        ),
        expected_revision=0,
    )
    valid_until = datetime(2027, 1, 1, tzinfo=UTC)
    payload = {
        "document": {"source_version": "v1", "valid_until": valid_until},
        "content": "Patient formula rule.",
        "title": "Patient rule",
    }
    result = build_probe_result(
        run_id=base.run_id,
        run_incarnation=base.run_incarnation,
        revision=0,
        schema_namespace_version=base.schema_namespace_version,
        invocation_id="patient-rule-evidence",
        action_digest=document_action.action_digest,
        probe_kind=document_action.kind,
        status=ProbeStatus.SUCCESS,
        target=document,
        started_at=_NOW,
        completed_at=_NOW,
        summary="fresh document rule",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=0,
        payload=payload,
    )
    document_evidence = probe_result_to_evidence(result, document_action)
    assert document_evidence is not None
    binding = DerivedExpressionBinding(
        binding_id="patient-formula",
        source_id="source-1",
        tables=(patient,),
        columns=(patient_name, patient_id),
        predicates=(),
        join_path=(),
        evidence_ids=(document_evidence.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        expression=ExpressionRef(
            expression_id="patient-expression",
            expression="Name || ID",
        ),
        document=document,
        rule_excerpt="Patient formula rule.",
        input_columns=(patient_name, patient_id),
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (binding.binding_id,),
        }
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", by_alias=True, round_trip=True),
            "revision": 1,
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "evidence": (document_evidence,),
            "bindings": (binding,),
            "action_history": (document_action,),
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": binding.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (document_evidence.evidence_id,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {"table": "Patient", "column": "ID"},
                },
            },
        }
    )
    freshness = _freshness(state).model_copy(
        update={
            "document_sources": (
                DocumentSourceState(
                    document_id=document.document_id,
                    availability=DocumentSourceAvailability.AVAILABLE,
                    source_version="v1",
                ),
            )
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, freshness, requested_action=None
    )

    assert feedback[0]["missing_probe"] == {
        "tool_name": "inspect_column",
        "arguments": {"table": "Patient", "column": "ID"},
    }
    inspected_id = ResearchAction(
        action_id="patient-id-inspection",
        kind=ResearchActionKind.INSPECT_COLUMN,
        hypothesis_id=None,
        target=patient_id,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_COLUMN,
            hypothesis_id=None,
            target=patient_id,
            parameters=(),
            expected_revision=1,
        ),
        expected_revision=1,
    )

    next_probe = _missing_binding_column_probe(
        binding, state.evidence, (*state.action_history, inspected_id)
    )

    assert next_probe == (
        patient_name,
        {
            "tool_name": "inspect_column",
            "arguments": {"table": "Patient", "column": "Name"},
        },
    )


def test_rejected_preflight_feedback_keeps_the_complete_assessment_batch() -> None:
    """Retry context must not hide rejected join or hypothesis assessments."""

    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    citations = (state.evidence[0].evidence_id,)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "join_assessment",
                    "subject": {"reference_kind": "existing", "join_id": "join-1"},
                    "certificate": "insufficient",
                    "citation_evidence_ids": citations,
                },
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": "binding-1",
                    },
                    "certificate": "insufficient",
                    "citation_evidence_ids": citations,
                },
                {
                    "proposal_type": "hypothesis_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "hypothesis_id": "hypothesis-1",
                    },
                    "certificate": "insufficient",
                    "citation_evidence_ids": citations,
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.orders"},
                },
            },
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert [item["proposal"] for item in feedback] == sorted(
        (
            proposal.model_dump(mode="json", by_alias=True)
            for proposal in decision.proposals
        ),
        key=canonical_digest,
    )


def test_rejected_binding_assessment_feedback_names_unknown_binding() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": "binding:unknown",
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert feedback[0]["rejection_reason"] == (
        "referenced binding_id does not exist"
    )


def test_rejected_binding_contradiction_feedback_names_unsupported_certificate() -> None:
    """A retry must tell the model to omit an unsupported rejection."""

    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    table = TableRef(namespace="main", schema="public", table="orders")
    column = ColumnRef(table=table, column="status")
    binding = PhysicalColumnBinding(
        binding_id="binding-1",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(state.evidence[0].evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    state = state.model_copy(update={"bindings": (binding,)})
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": "binding-1",
                    },
                    "certificate": "contradicted",
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert feedback[0]["rejection_reason"] == (
        "binding contradiction is not a permitted certificate"
    )


@pytest.mark.parametrize(
    ("old_status", "old_confidence", "old_validator_rule"),
    (
        (
            BindingStatus.SUPPORTED,
            1.0,
            "semantic-certificate:v1:discriminator_value",
        ),
        (BindingStatus.CANDIDATE, 0.0, None),
    ),
)
def test_rejected_preflight_feedback_allows_only_certified_categorical_in_replacement(
    old_status: BindingStatus,
    old_confidence: float,
    old_validator_rule: str | None,
) -> None:
    """The R2568 batch is feedback-valid; nearby contradictions remain rejected."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace)
    table = TableRef(namespace="main", schema="public", table="orders")
    column = ColumnRef(table=table, column="hue")
    old_literal = "pale-blue"
    recovered_literal = "Pale Blue"

    def evidence(
        evidence_id: str,
        kind: ResearchActionKind,
        payload: dict[str, object],
        parameters: tuple[tuple[str, str | int | float | bool | None], ...] = (),
    ):
        action = ResearchAction(
            action_id=f"{evidence_id}-action",
            kind=kind,
            hypothesis_id=None,
            target=column,
            parameters=parameters,
            action_digest=canonical_action_digest(
                kind=kind,
                hypothesis_id=None,
                target=column,
                parameters=parameters,
                expected_revision=base.revision,
            ),
            expected_revision=base.revision,
        )
        result = build_probe_result(
            run_id=base.run_id,
            run_incarnation=base.run_incarnation,
            revision=base.revision,
            schema_namespace_version=base.schema_namespace_version,
            invocation_id=evidence_id,
            action_digest=action.action_digest,
            probe_kind=kind,
            status=ProbeStatus.SUCCESS,
            target=column,
            started_at=_NOW,
            completed_at=_NOW,
            summary="neutral categorical evidence",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=len(payload["rows"]),
                bytes=len(canonical_json_bytes(payload)),
            ),
            row_count=len(payload["rows"]),
            payload=payload,
        )
        record = probe_result_to_evidence(result, action)
        assert record is not None
        return record

    empty_search = evidence(
        "evidence:hue-empty",
        ResearchActionKind.SEARCH_VALUE,
        {"columns": [column.column], "requested_value": old_literal, "rows": []},
        (("value", old_literal),),
    )
    distinct = evidence(
        "evidence:hue-distinct",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [column.column], "rows": [[recovered_literal], ["other hue"]]},
    )
    recovered_search = evidence(
        "evidence:hue-recovered",
        ResearchActionKind.SEARCH_VALUE,
        {
            "columns": [column.column],
            "requested_value": recovered_literal,
            "rows": [[recovered_literal]],
        },
        (("value", recovered_literal),),
    )
    old = DiscriminatorValueBinding(
        binding_id="binding:old-hue",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(
            PredicateRef(
                left=column,
                operator=PredicateOperator.IN,
                right=(old_literal,),
            ),
        ),
        join_path=(),
        evidence_ids=(empty_search.evidence_id,),
        confidence=old_confidence,
        status=old_status,
        validator_rule=old_validator_rule,
        discriminator_column=column,
        discriminator_predicate=PredicateRef(
            left=column,
            operator=PredicateOperator.IN,
            right=(old_literal,),
        ),
    )
    state = base.model_copy(
        update={
            "evidence": (empty_search, distinct, recovered_search),
            "bindings": (old,),
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": old.binding_id,
                    },
                    "certificate": "contradicted",
                    "citation_evidence_ids": (
                        empty_search.evidence_id,
                        distinct.evidence_id,
                        recovered_search.evidence_id,
                    ),
                },
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:recovered-hue",
                    "source_id": old.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "hue",
                        },
                        "discriminator_predicate": {
                            "left": {"table": "public.orders", "column": "hue"},
                            "operator": PredicateOperator.IN,
                            "right": (recovered_literal,),
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (recovered_search.evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    contradicted = next(
        item
        for item in feedback
        if item["proposal"]["proposal_type"] == "binding_assessment"
    )
    assert "rejection_reason" not in contradicted

    for invalid in (
        decision.model_copy(update={"proposals": decision.proposals[:1]}),
        decision.model_copy(
            update={
                "proposals": (
                    decision.proposals[0].model_copy(
                        update={"citation_evidence_ids": (distinct.evidence_id,)}
                    ),
                    decision.proposals[1],
                )
            }
        ),
        decision.model_copy(
            update={
                "proposals": (
                    decision.proposals[0],
                    decision.proposals[1].model_copy(
                        update={"source_id": "source:other"}
                    ),
                )
            }
        ),
    ):
        invalid_feedback = _research_loop_module._rejected_preflight_assessment_context(
            state, invalid, _freshness(state), requested_action=None
        )
        invalid_contradicted = next(
            item
            for item in invalid_feedback
            if item["proposal"]["proposal_type"] == "binding_assessment"
        )
        assert invalid_contradicted["rejection_reason"] == (
            "binding contradiction is not a permitted certificate"
        )

    corrupted = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={
                        "citation_evidence_ids": (
                            empty_search.evidence_id,
                            distinct.evidence_id,
                            "model-corrupted-categorical-citation",
                        )
                    }
                ),
                decision.proposals[1],
            )
        }
    )
    normalized = _research_loop_module._normalize_model_source_ids(
        state,
        corrupted,
        freshness_context=_freshness(state),
    )
    assert normalized.proposals[0].citation_evidence_ids == tuple(
        sorted(
            (
                empty_search.evidence_id,
                distinct.evidence_id,
                recovered_search.evidence_id,
            )
        )
    )

    two_unknown = corrupted.model_copy(
        update={
            "proposals": (
                corrupted.proposals[0].model_copy(
                    update={
                        "citation_evidence_ids": (
                            empty_search.evidence_id,
                            distinct.evidence_id,
                            "model-corrupted-categorical-citation-a",
                            "model-corrupted-categorical-citation-b",
                        )
                    }
                ),
                corrupted.proposals[1],
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        state, two_unknown, freshness_context=_freshness(state)
    ) is two_unknown

    copied_recovered_search = evidence(
        "evidence:hue-recovered-copy",
        ResearchActionKind.SEARCH_VALUE,
        {
            "columns": [column.column],
            "requested_value": recovered_literal,
            "rows": [[recovered_literal]],
        },
        (("value", recovered_literal),),
    )
    ambiguous_state = state.model_copy(
        update={"evidence": (*state.evidence, copied_recovered_search)}
    )
    ambiguous = corrupted.model_copy(
        update={
            "proposals": (
                corrupted.proposals[0],
                corrupted.proposals[1].model_copy(
                    update={
                        "citation_evidence_ids": (
                            recovered_search.evidence_id,
                            copied_recovered_search.evidence_id,
                        )
                    }
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        ambiguous_state, ambiguous, freshness_context=_freshness(ambiguous_state)
    ) is ambiguous

    incomplete = corrupted.model_copy(update={"proposals": corrupted.proposals[:1]})
    assert _research_loop_module._normalize_model_source_ids(
        state, incomplete, freshness_context=_freshness(state)
    ) is incomplete

    wrong_source = corrupted.model_copy(
        update={
            "proposals": (
                corrupted.proposals[0],
                corrupted.proposals[1].model_copy(update={"source_id": "source:other"}),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        state, wrong_source, freshness_context=_freshness(state)
    ) is wrong_source

    wrong_column = LogicalColumnRef(table="public.orders", column="other_hue")
    wrong_column_candidate = corrupted.proposals[1].candidate.model_copy(
        update={
            "discriminator_column": wrong_column,
            "discriminator_predicate": corrupted.proposals[
                1
            ].candidate.discriminator_predicate.model_copy(
                update={"left": wrong_column}
            ),
        }
    )
    wrong_column_decision = corrupted.model_copy(
        update={
            "proposals": (
                corrupted.proposals[0],
                corrupted.proposals[1].model_copy(
                    update={"candidate": wrong_column_candidate}
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        state, wrong_column_decision, freshness_context=_freshness(state)
    ) is wrong_column_decision

    stale_context = _freshness(state).model_copy(update={"run_id": "other-run"})
    assert _research_loop_module._normalize_model_source_ids(
        state, corrupted, freshness_context=stale_context
    ) is corrupted
    assert _research_loop_module._normalize_model_source_ids(
        state, decision, freshness_context=_freshness(state)
    ) is decision


def test_categorical_replacement_assessment_completes_missing_zero_search_roles() -> None:
    """R2584 fills only unique fresh zero-search roles before the closed check."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace)
    table = TableRef(namespace="main", schema="public", table="orders")
    column = ColumnRef(table=table, column="hue")
    old_literals = ("dusty-blue", "pale-blue")
    recovered_literal = "Pale Blue"

    def evidence(
        evidence_id: str,
        payload: dict[str, object],
        *,
        target: ColumnRef = column,
        kind: ResearchActionKind = ResearchActionKind.SEARCH_VALUE,
    ):
        value = payload.get("requested_value")
        parameters = (("value", value),) if "requested_value" in payload else ()
        action = ResearchAction(
            action_id=f"{evidence_id}-action",
            kind=kind,
            hypothesis_id=None,
            target=target,
            parameters=parameters,
            action_digest=canonical_action_digest(
                kind=kind,
                hypothesis_id=None,
                target=target,
                parameters=parameters,
                expected_revision=base.revision,
            ),
            expected_revision=base.revision,
        )
        result = build_probe_result(
            run_id=base.run_id,
            run_incarnation=base.run_incarnation,
            revision=base.revision,
            schema_namespace_version=base.schema_namespace_version,
            invocation_id=evidence_id,
            action_digest=action.action_digest,
            probe_kind=kind,
            status=ProbeStatus.SUCCESS,
            target=target,
            started_at=_NOW,
            completed_at=_NOW,
            summary="neutral categorical evidence",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=len(payload["rows"]),
                bytes=len(canonical_json_bytes(payload)),
            ),
            row_count=len(payload["rows"]),
            payload=payload,
        )
        record = probe_result_to_evidence(result, action)
        assert record is not None
        return record

    first_zero = evidence(
        "evidence:hue-first-zero",
        {
            "columns": [column.column],
            "requested_value": old_literals[0],
            "rows": [],
        },
    )
    second_zero = evidence(
        "evidence:hue-second-zero",
        {
            "columns": [column.column],
            "requested_value": old_literals[1],
            "rows": [],
        },
    )
    distinct = evidence(
        "evidence:hue-distinct",
        {"columns": [column.column], "rows": [[recovered_literal], ["other hue"]]},
        kind=ResearchActionKind.DISTINCT_VALUES,
    )
    recovered_search = evidence(
        "evidence:hue-recovered",
        {
            "columns": [column.column],
            "requested_value": recovered_literal,
            "rows": [[recovered_literal]],
        },
    )

    def decision(citation_evidence_ids: tuple[str, ...]):
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": "binding:old-hue",
                        },
                        "certificate": "contradicted",
                        "citation_evidence_ids": citation_evidence_ids,
                    },
                    {
                        "proposal_type": "new_binding",
                        "proposal_key": "proposal:recovered-hue",
                        "source_id": "source-1",
                        "candidate": {
                            "kind": "discriminator_value",
                            "discriminator_column": {
                                "table": "public.orders",
                                "column": "hue",
                            },
                            "discriminator_predicate": {
                                "left": {"table": "public.orders", "column": "hue"},
                                "operator": PredicateOperator.IN,
                                "right": (recovered_literal,),
                            },
                        },
                        "join_references": (),
                        "citation_evidence_ids": (recovered_search.evidence_id,),
                    },
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    def state(
        old_status: BindingStatus,
        evidence_records: tuple,
    ) -> ResearchState:
        predicate = PredicateRef(
            left=column,
            operator=PredicateOperator.IN,
            right=old_literals,
        )
        old = DiscriminatorValueBinding(
            binding_id="binding:old-hue",
            source_id="source-1",
            tables=(table,),
            columns=(column,),
            predicates=(predicate,),
            join_path=(),
            evidence_ids=(distinct.evidence_id,),
            confidence=1.0 if old_status is BindingStatus.SUPPORTED else 0.0,
            status=old_status,
            validator_rule=(
                "semantic-certificate:v1:discriminator_value"
                if old_status is BindingStatus.SUPPORTED
                else None
            ),
            discriminator_column=column,
            discriminator_predicate=predicate,
        )
        return base.model_copy(
            update={"evidence": evidence_records, "bindings": (old,)}
        )

    original = decision((distinct.evidence_id, recovered_search.evidence_id))
    all_evidence = (first_zero, second_zero, distinct, recovered_search)
    expected = tuple(
        sorted(
            (
                first_zero.evidence_id,
                second_zero.evidence_id,
                distinct.evidence_id,
                recovered_search.evidence_id,
            )
        )
    )
    for old_status in (BindingStatus.SUPPORTED, BindingStatus.CANDIDATE):
        current = state(old_status, all_evidence)
        normalized = _research_loop_module._normalize_model_source_ids(
            current, original, freshness_context=_freshness(current)
        )
        assert normalized.proposals[0].citation_evidence_ids == expected
        assert _research_loop_module._normalize_model_source_ids(
            current, normalized, freshness_context=_freshness(current)
        ) is normalized

    missing = state(BindingStatus.SUPPORTED, (first_zero, distinct, recovered_search))
    assert _research_loop_module._normalize_model_source_ids(
        missing, original, freshness_context=_freshness(missing)
    ) is original

    duplicate = evidence(
        "evidence:hue-first-zero-copy",
        {
            "columns": [column.column],
            "requested_value": old_literals[0],
            "rows": [],
        },
    )
    ambiguous = state(BindingStatus.SUPPORTED, (*all_evidence, duplicate))
    assert _research_loop_module._normalize_model_source_ids(
        ambiguous, original, freshness_context=_freshness(ambiguous)
    ) is original

    wrong_column = ColumnRef(table=table, column="other_hue")
    wrong_target = evidence(
        "evidence:hue-wrong-target",
        {
            "columns": [wrong_column.column],
            "requested_value": old_literals[1],
            "rows": [],
        },
        target=wrong_column,
    )
    invalid_result = evidence(
        "evidence:hue-invalid-result",
        {"columns": [], "requested_value": old_literals[1], "rows": []},
    )
    nonempty = evidence(
        "evidence:hue-nonempty",
        {
            "columns": [column.column],
            "requested_value": old_literals[1],
            "rows": [[old_literals[1]]],
        },
    )
    wrong_type = evidence(
        "evidence:hue-wrong-type",
        {"columns": [column.column], "requested_value": 1, "rows": []},
    )
    wrong_literal = evidence(
        "evidence:hue-wrong-literal",
        {"columns": [column.column], "requested_value": "other", "rows": []},
    )
    for invalid in (wrong_target, invalid_result, nonempty, wrong_type, wrong_literal):
        current = state(
            BindingStatus.SUPPORTED,
            (first_zero, distinct, recovered_search, invalid),
        )
        assert _research_loop_module._normalize_model_source_ids(
            current, original, freshness_context=_freshness(current)
        ) is original

    missing_positive = decision((distinct.evidence_id,))
    current = state(BindingStatus.SUPPORTED, all_evidence)
    assert _research_loop_module._normalize_model_source_ids(
        current, missing_positive, freshness_context=_freshness(current)
    ) is missing_positive
    wrong_replacement = original.model_copy(
        update={
            "proposals": (
                original.proposals[0],
                original.proposals[1].model_copy(update={"source_id": "source:other"}),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        current, wrong_replacement, freshness_context=_freshness(current)
    ) is wrong_replacement
    no_replacement = original.model_copy(update={"proposals": original.proposals[:1]})
    assert _research_loop_module._normalize_model_source_ids(
        current, no_replacement, freshness_context=_freshness(current)
    ) is no_replacement
    complete = decision(expected)
    assert _research_loop_module._normalize_model_source_ids(
        current,
        complete,
        freshness_context=_freshness(current),
    ) is complete
    stale_context = _freshness(current).model_copy(update={"run_id": "other-run"})
    assert _research_loop_module._normalize_model_source_ids(
        current, original, freshness_context=stale_context
    ) is original


def test_rejected_hypothesis_contradiction_feedback_names_missing_certificate() -> None:
    """A retry must explain why a hypothesis contradiction was not proved."""

    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True, hypothesis=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "hypothesis_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "hypothesis_id": "hypothesis-1",
                    },
                    "certificate": "contradicted",
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.orders"},
                },
            },
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert feedback[0]["rejection_reason"] == (
        "hypothesis contradiction is not proven by cited evidence"
    )


def test_rejected_hypothesis_consistency_feedback_names_missing_certificate() -> None:
    """A retry must explain why hypothesis support was not proved."""

    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True, hypothesis=True)
    unrelated_target = TableRef(
        namespace="main", schema="public", table="customers"
    )
    hypothesis = state.hypotheses[0].model_copy(
        update={"candidate_targets": (unrelated_target,)}
    )
    state = state.model_copy(update={"hypotheses": (hypothesis,)})
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "hypothesis_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "hypothesis_id": hypothesis.hypothesis_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.customers"},
                },
            },
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert feedback[0]["rejection_reason"] == (
        "hypothesis consistency is not proven by cited evidence"
    )


def test_preflight_allows_operatorless_filter_discriminator() -> None:

    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    citation = state.evidence[0].evidence_id
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:typed-range",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "status",
                            },
                            "operator": PredicateOperator.BETWEEN,
                            "right": (201201, 201212),
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (citation,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.orders"},
                },
            },
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert len(feedback) == 1
    assert "rejection_reason" not in feedback[0]


def test_rejected_new_binding_feedback_names_unknown_evidence() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:status-filter",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "status",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": "open",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("evidence:unknown",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert feedback[0]["rejection_reason"] == (
        "cited evidence_id does not exist"
    )
    assert feedback[0]["available_evidence_ids"] == [
        state.evidence[0].evidence_id
    ]


def test_rejected_assessment_feedback_preserves_unknown_evidence_reason() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True, hypothesis=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "hypothesis_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "hypothesis_id": state.hypotheses[0].hypothesis_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": ("evidence:unknown",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert feedback[0]["rejection_reason"] == (
        "cited evidence_id does not exist"
    )


def test_rejected_new_binding_feedback_names_unknown_existing_join() -> None:
    """A retry tells the model to copy a real existing join ID verbatim."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    orders = TableRef(namespace="main", schema="public", table="orders")
    customers = TableRef(namespace="main", schema="public", table="customers")
    left = ColumnRef(table=orders, column="id")
    right = ColumnRef(table=customers, column="id")
    join = JoinCandidate(
        join_id="join-existing",
        left=left,
        right=right,
        join_type=JoinType.INNER,
        path=(JoinEdge(left=left, right=right, join_type=JoinType.INNER),),
        status=JoinCandidateStatus.CANDIDATE,
        evidence_ids=(base.evidence[0].evidence_id,),
    )
    state = base.model_copy(update={"join_candidates": (join,)})
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:status-filter",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "status",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": "open",
                        },
                    },
                    "join_references": (
                        {
                            "reference_kind": "existing",
                            "join_id": "join-typo",
                        },
                    ),
                    "citation_evidence_ids": (base.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert feedback == (
        {
            "proposal": decision.proposals[0].model_dump(
                mode="json", by_alias=True
            ),
            "rejection_reason": "referenced join_id does not exist",
        },
    )

def test_rejected_new_binding_feedback_names_unknown_source() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:status-filter",
                    "source_id": "source-typo",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "status",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": "open",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert feedback[0]["rejection_reason"] == "source_id does not exist"
    assert feedback[0]["available_source_ids"] == ["source-1"]


def test_rejected_new_binding_feedback_corrects_case_only_column_with_inspect_probe() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    exact_column = ColumnRef(
        table=TableRef(namespace="main", schema="public", table="orders"),
        column="OpenDate",
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:open-date",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "opendate",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        exact_column=exact_column,
    )

    assert feedback == (
        {
            "proposal": decision.proposals[0].model_dump(mode="json", by_alias=True),
            "rejection_reason": "logical column differs by case",
            "exact_column": exact_column.model_dump(mode="json", by_alias=True),
            "missing_probe": {
                "tool_name": "inspect_column",
                "arguments": {"table": "public.orders", "column": "OpenDate"},
            },
        },
    )

    inspected = ResearchAction(
        action_id="open-date-inspection",
        kind=ResearchActionKind.INSPECT_COLUMN,
        hypothesis_id=None,
        target=exact_column,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_COLUMN,
            hypothesis_id=None,
            target=exact_column,
            parameters=(),
            expected_revision=state.revision,
        ),
        expected_revision=state.revision,
    )
    completed = state.model_copy(
        update={"action_history": (*state.action_history, inspected)}
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        completed,
        decision,
        _freshness(completed),
        requested_action=None,
        exact_column=exact_column,
    )

    assert "missing_probe" not in feedback[0]


def test_unique_one_character_source_id_typo_is_normalized() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:status-filter",
                    "source_id": "source-x",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    assert normalized.proposals[0].source_id == "source-1"


def test_requested_dimension_discriminator_is_normalized_to_output_column() -> None:
    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.DIMENSION,
            "source_text": "risk flag",
            "normalized_meaning": "risk flag",
        }
    )
    state = base.model_copy(
        update={
            "query_spec": base.query_spec.model_copy(
                update={
                    "semantic_items": (item,),
                    "requested_output_source_ids": (item.source_id,),
                }
            )
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:risk-flag",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.accounts",
                            "column": "risk_flag",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.accounts",
                                "column": "risk_flag",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": 1,
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    candidate = normalized.proposals[0].candidate
    assert isinstance(candidate, PhysicalColumnCandidate)
    assert candidate.physical_column == LogicalColumnRef(
        table="public.accounts",
        column="risk_flag",
    )
    unchanged_filter = _research_loop_module._normalize_model_source_ids(
        base,
        decision,
    )
    assert isinstance(
        unchanged_filter.proposals[0].candidate,
        DiscriminatorValueCandidate,
    )


def test_single_required_source_is_reused_when_batch_has_exact_anchor() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    proposal = {
        "proposal_type": "new_binding",
        "candidate": {
            "kind": "physical_column",
            "physical_column": {
                "table": "public.orders",
                "column": "status",
            },
        },
        "join_references": (),
        "citation_evidence_ids": (state.evidence[0].evidence_id,),
    }
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    **proposal,
                    "proposal_key": "proposal:exact-source",
                    "source_id": "source-1",
                },
                {
                    **proposal,
                    "proposal_key": "proposal:repeated-source",
                    "source_id": "source-1-formula-condition",
                },
                {
                    **proposal,
                    "proposal_key": "proposal:second-repeated-source",
                    "source_id": "source-1-reputation-condition",
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    assert tuple(item.source_id for item in normalized.proposals) == (
        "source-1",
        "source-1",
        "source-1",
    )

    without_anchor = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[1],
            )
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(state, without_anchor)
        is without_anchor
    )

    second_item = state.query_spec.semantic_items[0].model_copy(
        update={"source_id": "source-2"}
    )
    multiple_items = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        *state.query_spec.semantic_items,
                        second_item,
                    )
                }
            )
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(multiple_items, decision)
        is decision
    )


def test_unknown_physical_column_citation_uses_its_unique_durable_evidence() -> None:
    _loaded_schema, namespace = _fixture_schema()
    base = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    column = ColumnRef(
        table=TableRef(namespace="main", schema="public", table="orders"),
        column="id",
    )
    action = ResearchAction(
        action_id="id-inspection",
        kind=ResearchActionKind.INSPECT_COLUMN,
        hypothesis_id=None,
        target=column,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_COLUMN,
            hypothesis_id=None,
            target=column,
            parameters=(),
            expected_revision=base.revision,
        ),
        expected_revision=base.revision,
    )
    payload = {
        "status": "matched",
        "column": column.model_dump(mode="json", by_alias=True),
    }
    result = build_probe_result(
        run_id=base.run_id,
        run_incarnation=base.run_incarnation,
        revision=base.revision,
        schema_namespace_version=base.schema_namespace_version,
        invocation_id="id-evidence",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=column,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="trusted id observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "revision": base.revision + 1,
            "evidence": (*base.evidence, evidence),
            "action_history": (*base.action_history, action),
        }
    )
    typo = f"{evidence.evidence_id[:-1]}a{evidence.evidence_id[-1]}"
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:id-output",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "id",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (typo,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    assert normalized.proposals[0].citation_evidence_ids == (evidence.evidence_id,)

    exact = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={"citation_evidence_ids": (evidence.evidence_id,)}
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(state, exact) is exact

    unmatched = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={
                        "candidate": decision.proposals[0].candidate.model_copy(
                            update={
                                "physical_column": decision.proposals[
                                    0
                                ].candidate.physical_column.model_copy(
                                    update={"column": "missing"}
                                )
                            }
                        )
                    }
                ),
            )
        }
    )

    assert (
        _research_loop_module._normalize_model_source_ids(state, unmatched)
        is unmatched
    )

    second_action = action.model_copy(
        update={
            "action_id": "second-id-inspection",
            "action_digest": canonical_action_digest(
                kind=ResearchActionKind.PROFILE_COLUMN,
                hypothesis_id=None,
                target=column,
                parameters=(),
                expected_revision=state.revision,
            ),
            "expected_revision": state.revision,
            "kind": ResearchActionKind.PROFILE_COLUMN,
        }
    )
    second_result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id="second-id-evidence",
        action_digest=second_action.action_digest,
        probe_kind=second_action.kind,
        status=ProbeStatus.SUCCESS,
        target=column,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="second trusted id observation",
        cost=result.cost,
        row_count=1,
        payload=payload,
    )
    second_evidence = probe_result_to_evidence(second_result, second_action)
    assert second_evidence is not None
    ambiguous_state = ResearchState.model_validate(
        {
            **state.model_dump(mode="python", round_trip=True),
            "revision": state.revision + 1,
            "evidence": (*state.evidence, second_evidence),
            "action_history": (*state.action_history, second_action),
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(
            ambiguous_state, decision
        )
        is decision
    )


def test_unknown_physical_column_citation_keeps_known_citations() -> None:
    _loaded_schema, namespace = _fixture_schema()
    base = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    table = TableRef(namespace="main", schema="public", table="orders")
    known_action, known_evidence = _observed_column_evidence(
        base,
        ColumnRef(table=table, column="priority"),
        invocation_id="priority-evidence",
    )
    with_known = base.model_copy(
        update={
            "revision": base.revision + 1,
            "evidence": (*base.evidence, known_evidence),
            "action_history": (*base.action_history, known_action),
        }
    )
    column = ColumnRef(table=table, column="id")
    action, evidence = _observed_column_evidence(
        with_known,
        column,
        invocation_id="id-evidence",
    )
    state = with_known.model_copy(
        update={
            "revision": with_known.revision + 1,
            "evidence": (*with_known.evidence, evidence),
            "action_history": (*with_known.action_history, action),
        }
    )
    known = (base.evidence[0].evidence_id, known_evidence.evidence_id)
    typo = f"{evidence.evidence_id[:-1]}a{evidence.evidence_id[-1]}"
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:id-output",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "id",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "id",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": "synthetic",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (*known, typo),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    assert normalized.proposals[0].citation_evidence_ids == tuple(
        sorted((*known, evidence.evidence_id))
    )

    two_unknown = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={
                        "citation_evidence_ids": (*known, typo, "evidence:second-typo")
                    }
                ),
            )
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(state, two_unknown)
        is two_unknown
    )

    second_action, second_evidence = _observed_column_evidence(
        state,
        column,
        invocation_id="second-id-evidence",
        kind=ResearchActionKind.PROFILE_COLUMN,
    )
    ambiguous_state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (*state.evidence, second_evidence),
            "action_history": (*state.action_history, second_action),
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(ambiguous_state, decision)
        is decision
    )


def test_categorical_in_citation_recovers_one_unique_complete_fresh_evidence() -> None:
    """Recover one corrupt citation only from exact categorical evidence."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace)
    column = ColumnRef(
        table=TableRef(namespace="main", schema="fictional", table="palette"),
        column="hue_code",
    )

    def value_evidence(
        evidence_id: str,
        rows: list[list[object]],
        *,
        target: ColumnRef = column,
        kind: ResearchActionKind = ResearchActionKind.DISTINCT_VALUES,
        requested_value: object | None = None,
    ):
        parameters = (
            (("value", requested_value),)
            if kind is ResearchActionKind.SEARCH_VALUE
            else ()
        )
        action = ResearchAction(
            action_id=f"{evidence_id}-action",
            kind=kind,
            hypothesis_id=None,
            target=target,
            parameters=parameters,
            action_digest=canonical_action_digest(
                kind=kind,
                hypothesis_id=None,
                target=target,
                parameters=parameters,
                expected_revision=base.revision,
            ),
            expected_revision=base.revision,
        )
        payload = {"columns": [target.column], "rows": rows}
        if kind is ResearchActionKind.SEARCH_VALUE:
            assert requested_value is not None
            payload["requested_value"] = requested_value
        result = build_probe_result(
            run_id=base.run_id,
            run_incarnation=base.run_incarnation,
            revision=base.revision,
            schema_namespace_version=base.schema_namespace_version,
            invocation_id=evidence_id,
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=target,
            started_at=_NOW,
            completed_at=_NOW,
            summary="neutral fictional categorical observation",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=len(rows),
                bytes=len(canonical_json_bytes(payload)),
            ),
            row_count=len(rows),
            payload=payload,
        )
        evidence = probe_result_to_evidence(result, action)
        assert evidence is not None
        return evidence

    _column_action, known_column = _observed_column_evidence(
        base, column, invocation_id="fictional-hue-column"
    )
    known_search = value_evidence(
        "fictional-hue-known",
        [["violet"]],
        kind=ResearchActionKind.SEARCH_VALUE,
        requested_value="violet",
    )
    incomplete = value_evidence("fictional-hue-incomplete", [["violet"]])
    complete = value_evidence(
        "fictional-hue-complete", [["violet"], ["amber"]]
    )
    state = base.model_copy(
        update={"evidence": (known_column, known_search, incomplete, complete)}
    )
    known = (known_column.evidence_id, known_search.evidence_id)

    def decision(
        citations: tuple[str, ...], *, values: tuple[object, ...] = ("violet", "amber")
    ) -> ResearchDecisionV1:
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "new_binding",
                        "proposal_key": "proposal:fictional-hue",
                        "source_id": "source-1",
                        "candidate": {
                            "kind": "discriminator_value",
                            "discriminator_column": {
                                "table": "fictional.palette",
                                "column": "hue_code",
                            },
                            "discriminator_predicate": {
                                "left": {
                                    "table": "fictional.palette",
                                    "column": "hue_code",
                                },
                                "operator": PredicateOperator.IN,
                                "right": values,
                            },
                        },
                        "join_references": (),
                        "citation_evidence_ids": citations,
                    },
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    corrupted = decision((*known, "model-corrupted-citation"))
    normalized = _research_loop_module._normalize_model_source_ids(
        state, corrupted, freshness_context=_freshness(state)
    )
    assert normalized.proposals[0].citation_evidence_ids == tuple(
        sorted((*known, complete.evidence_id))
    )

    two_unknown = decision((*known, "model-corrupted-a", "model-corrupted-b"))
    assert _research_loop_module._normalize_model_source_ids(
        state, two_unknown, freshness_context=_freshness(state)
    ) is two_unknown

    another_complete = value_evidence(
        "fictional-hue-another-complete", [["violet"], ["amber"]]
    )
    ambiguous_state = state.model_copy(
        update={"evidence": (*state.evidence, another_complete)}
    )
    assert _research_loop_module._normalize_model_source_ids(
        ambiguous_state, corrupted, freshness_context=_freshness(ambiguous_state)
    ) is corrupted

    unused_search = value_evidence(
        "fictional-hue-unused-search",
        [["violet"]],
        kind=ResearchActionKind.SEARCH_VALUE,
        requested_value="violet",
    )
    unused_both_state = state.model_copy(
        update={"evidence": (known_column, unused_search, complete)}
    )
    unused_both = decision(
        (known_column.evidence_id, "model-corrupted-unused"), values=("violet",)
    )
    assert _research_loop_module._normalize_model_source_ids(
        unused_both_state,
        unused_both,
        freshness_context=_freshness(unused_both_state),
    ) is unused_both

    stale_context = _freshness(state).model_copy(
        update={"schema_namespace_version": "sha256:" + "b" * 64}
    )
    assert _research_loop_module._normalize_model_source_ids(
        state, corrupted, freshness_context=stale_context
    ) is corrupted

    wrong_column = ColumnRef(table=column.table, column="other_hue_code")
    wrong_only_state = state.model_copy(
        update={
            "evidence": (
                known_column,
                known_search,
                value_evidence(
                    "fictional-wrong-column",
                    [["violet"], ["amber"]],
                    target=wrong_column,
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        wrong_only_state, corrupted, freshness_context=_freshness(wrong_only_state)
    ) is corrupted

    mismatched_values = state.model_copy(
        update={
            "evidence": (
                known_column,
                known_search,
                value_evidence("fictional-string-seven", [["7"]]),
                value_evidence("fictional-other-value", [["8"]]),
            )
        }
    )
    int_decision = decision((*known, "model-corrupted-int"), values=(7,))
    assert _research_loop_module._normalize_model_source_ids(
        mismatched_values, int_decision, freshness_context=_freshness(mismatched_values)
    ) is int_decision

    different_literal_state = state.model_copy(
        update={
            "evidence": (
                known_column,
                known_search,
                incomplete,
                value_evidence("fictional-different-literal", [["violet"], ["ochre"]]),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        different_literal_state,
        corrupted,
        freshness_context=_freshness(different_literal_state),
    ) is corrupted

    valid = decision((*known, complete.evidence_id))
    assert _research_loop_module._normalize_model_source_ids(
        state, valid, freshness_context=_freshness(state)
    ) is valid


def test_new_join_citation_recovers_only_one_exact_declared_relationship() -> None:
    """A corrupt model citation can use one exact durable join certificate."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    customers = TableRef(namespace="main", schema="public", table="customers")
    action = ResearchAction(
        action_id="relationship-certificate-action",
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        hypothesis_id=None,
        target=customers,
        parameters=(("depth", 1), ("top_k", 50)),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
            hypothesis_id=None,
            target=customers,
            parameters=(("depth", 1), ("top_k", 50)),
            expected_revision=base.revision,
        ),
        expected_revision=base.revision,
    )
    payload = {
        "relationships": [
            {
                "relationship_kind": "declared",
                "from_table": "public.orders",
                "to_table": "public.customers",
                "column_pairs": [{"from_column": "customer_id", "to_column": "id"}],
            }
        ]
    }
    result = build_probe_result(
        run_id=base.run_id,
        run_incarnation=base.run_incarnation,
        revision=base.revision,
        schema_namespace_version=base.schema_namespace_version,
        invocation_id="relationship-certificate",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=customers,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="declared relationship",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )
    relationship_evidence = probe_result_to_evidence(result, action)
    assert relationship_evidence is not None
    state = base.model_copy(
        update={
            "revision": base.revision + 1,
            "evidence": (*base.evidence, relationship_evidence),
            "action_history": (*base.action_history, action),
        }
    )
    known = base.evidence[0].evidence_id

    def decision(
        citations: tuple[str, ...], *, right_column: str = "id"
    ) -> ResearchDecisionV1:
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "new_join",
                        "proposal_key": "proposal:orders-customers",
                        "left": {"table": "public.orders", "column": "customer_id"},
                        "right": {"table": "public.customers", "column": right_column},
                        "join_type": JoinType.INNER,
                        "path": (
                            {
                                "left": {
                                    "table": "public.orders",
                                    "column": "customer_id",
                                },
                                "right": {
                                    "table": "public.customers",
                                    "column": right_column,
                                },
                                "join_type": JoinType.INNER,
                            },
                        ),
                        "citation_evidence_ids": citations,
                    },
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    no_relationship = decision((known, "model-corrupted-without-certificate"))
    assert _research_loop_module._normalize_model_source_ids(
        base, no_relationship
    ) is no_relationship

    corrupted = decision((known, "model-corrupted-patient-id"))
    normalized = _research_loop_module._normalize_model_source_ids(state, corrupted)
    assert normalized.proposals[0].citation_evidence_ids == tuple(
        sorted((known, relationship_evidence.evidence_id))
    )

    exact = decision((known, relationship_evidence.evidence_id))
    assert _research_loop_module._normalize_model_source_ids(state, exact) is exact
    two_unknown = decision((known, "model-corrupted-a", "model-corrupted-b"))
    assert (
        _research_loop_module._normalize_model_source_ids(state, two_unknown)
        is two_unknown
    )
    assert _research_loop_module._normalize_model_source_ids(
        state, decision((known, "model-corrupted-id"), right_column="status")
    ).proposals[0].citation_evidence_ids == (known, "model-corrupted-id")
    ambiguous_state = state.model_copy(
        update={
            "evidence": (
                *state.evidence,
                relationship_evidence.model_copy(
                    update={"evidence_id": "relationship-certificate-copy"}
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        ambiguous_state, corrupted
    ) is corrupted


def test_unknown_discriminator_column_citation_uses_unique_durable_evidence() -> None:
    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    column = ColumnRef(
        table=TableRef(namespace="main", schema="public", table="orders"),
        column="created_year",
    )
    action, evidence = _observed_column_evidence(
        base,
        column,
        invocation_id="created-year-evidence",
    )
    state = base.model_copy(
        update={
            "revision": base.revision + 1,
            "evidence": (*base.evidence, evidence),
            "action_history": (*base.action_history, action),
        }
    )
    replacement = "0" if evidence.evidence_id[-1] != "0" else "1"
    unknown = f"{evidence.evidence_id[:-1]}{replacement}"
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:created-year-filter",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "created_year",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "created_year",
                            },
                            "operator": PredicateOperator.BETWEEN,
                            "right": (2020, 2021),
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (unknown,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    assert normalized.proposals[0].citation_evidence_ids == (evidence.evidence_id,)

    mismatched = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={
                        "candidate": decision.proposals[0].candidate.model_copy(
                            update={
                                "discriminator_predicate": LogicalPredicate(
                                    left=LogicalColumnRef(
                                        table="public.orders",
                                        column="other_year",
                                    ),
                                    operator=PredicateOperator.BETWEEN,
                                    right=(2020, 2021),
                                )
                            }
                        )
                    }
                ),
            )
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(state, mismatched)
        is mismatched
    )


def test_unknown_discriminator_citation_uses_unique_table_schema_evidence() -> None:
    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    table = TableRef(namespace="main", schema="public", table="orders")
    action, evidence = _observed_table_evidence(
        base,
        table,
        invocation_id="orders-schema-evidence",
        columns=[
            {
                "constraint_type": "",
                "description": "created year",
                "name": "created_year",
                "not_null": "",
                "type": "INTEGER",
            }
        ],
    )
    state = base.model_copy(
        update={
            "revision": base.revision + 1,
            "evidence": (*base.evidence, evidence),
            "action_history": (*base.action_history, action),
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:created-year-table-filter",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "created_year",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "created_year",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": 2020,
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("invocation:unknown",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    assert normalized.proposals[0].citation_evidence_ids == (evidence.evidence_id,)

    second_action, second_evidence = _observed_table_evidence(
        state,
        table,
        invocation_id="second-orders-schema-evidence",
        columns=[
            {
                "constraint_type": "",
                "description": "created year",
                "name": "created_year",
                "not_null": "",
                "type": "INTEGER",
            }
        ],
    )
    ambiguous_state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (*state.evidence, second_evidence),
            "action_history": (*state.action_history, second_action),
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(ambiguous_state, decision)
        is decision
    )


def test_unknown_document_backed_derived_citation_uses_matching_document() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state, document = _document_supported_state_after_probe(
        namespace,
        observed_at=_FIXTURE_NOW,
        valid_until=_FIXTURE_NOW + timedelta(days=1),
    )
    unknown = "invocation:unknown-document-citation"
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:document-expression",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "derived_expression",
                        "expression_claim": "approved_status(status)",
                        "document_id": document.document_id,
                        "rule_excerpt": "approved status rule",
                        "input_columns": (
                            {"table": "public.orders", "column": "status"},
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": (unknown,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    assert normalized.proposals[0].citation_evidence_ids == (
        state.evidence[0].evidence_id,
    )
    exact = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={"citation_evidence_ids": (state.evidence[0].evidence_id,)}
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(state, exact) is exact
    other_document = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={
                        "candidate": decision.proposals[0].candidate.model_copy(
                            update={"document_id": "other-rule"}
                        )
                    }
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(state, other_document) is other_document
    ambiguous_state = state.model_copy(
        update={
            "evidence": (
                *state.evidence,
                state.evidence[0].model_copy(
                    update={"evidence_id": "invocation:duplicate-document"}
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(ambiguous_state, decision) is decision
    physical = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={
                        "candidate": {
                            "kind": "physical_column",
                            "physical_column": {
                                "table": "public.orders",
                                "column": "status",
                            },
                        }
                    }
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(state, physical) is physical


@pytest.mark.parametrize(
    ("document_id", "duplicate_document"),
    (("other-rule", False), ("orders-rule", True)),
    ids=("wrong-document", "ambiguous-document"),
)
def test_document_backed_derived_citation_does_not_fallback_to_input_column(
    document_id: str,
    duplicate_document: bool,
) -> None:
    _loaded_schema, namespace = _fixture_schema()
    state, document = _document_supported_state_after_probe(
        namespace,
        observed_at=_FIXTURE_NOW,
        valid_until=_FIXTURE_NOW + timedelta(days=1),
    )
    column = ColumnRef(
        table=TableRef(namespace="main", schema="public", table="orders"),
        column="status",
    )
    action, column_evidence = _observed_column_evidence(
        state,
        column,
        invocation_id=f"{document_id}-column",
    )
    state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (*state.evidence, column_evidence),
            "action_history": (*state.action_history, action),
        }
    )
    if duplicate_document:
        state = state.model_copy(
            update={
                "evidence": (
                    *state.evidence,
                    state.evidence[0].model_copy(
                        update={"evidence_id": "invocation:duplicate-document"}
                    ),
                )
            }
        )
        document_id = document.document_id
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:document-expression",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "derived_expression",
                        "expression_claim": "approved_status(status)",
                        "document_id": document_id,
                        "rule_excerpt": "approved status rule",
                        "input_columns": (
                            {"table": "public.orders", "column": "status"},
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("invocation:unknown-document",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    assert _research_loop_module._normalize_model_source_ids(state, decision) is decision


def test_non_document_backed_derived_citation_uses_unique_input_column() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    column = ColumnRef(
        table=TableRef(namespace="main", schema="public", table="orders"),
        column="status",
    )
    action, evidence = _observed_column_evidence(
        state,
        column,
        invocation_id="unbacked-derived-column",
    )
    state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (evidence,),
            "action_history": (action,),
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:unbacked-expression",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "derived_expression",
                        "expression_claim": "approved_status(status)",
                        "document_id": "unbacked-rule",
                        "rule_excerpt": "approved status rule",
                        "input_columns": (
                            {"table": "public.orders", "column": "status"},
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("invocation:unknown-column",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    assert normalized.proposals[0].citation_evidence_ids == (evidence.evidence_id,)


def test_rejected_new_physical_column_without_durable_evidence_gets_inspect_probe() -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:status",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("invocation:unknown-column",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    )

    assert feedback[0]["rejection_reason"] == "cited evidence_id does not exist"
    assert feedback[0]["missing_probe"] == {
        "tool_name": "inspect_column",
        "arguments": {"table": "public.orders", "column": "status"},
    }
    column = ColumnRef(
        table=TableRef(namespace="main", schema="public", table="orders"),
        column="status",
    )
    inspected_action, inspected_evidence = _observed_column_evidence(
        state,
        column,
        invocation_id="status-evidence",
    )
    evidenced = state.model_copy(
        update={
            "evidence": (*state.evidence, inspected_evidence),
            "action_history": (*state.action_history, inspected_action),
        }
    )
    assert "missing_probe" not in _research_loop_module._rejected_preflight_assessment_context(
        evidenced,
        decision,
        _freshness(evidenced),
        requested_action=None,
        loaded_schema=loaded_schema,
    )[0]
    ambiguous_schema, _ = _fixture_schema(
        {
            "public.orders": {"columns": {"status": {"type": "TEXT"}}},
            "audit.orders": {"columns": {"status": {"type": "TEXT"}}},
        }
    )
    ambiguous = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={
                        "candidate": decision.proposals[0].candidate.model_copy(
                            update={
                                "physical_column": decision.proposals[
                                    0
                                ].candidate.physical_column.model_copy(
                                    update={"table": "orders"}
                                )
                            }
                        )
                    }
                ),
            )
        }
    )
    assert "missing_probe" not in _research_loop_module._rejected_preflight_assessment_context(
        state,
        ambiguous,
        _freshness(state),
        requested_action=None,
        loaded_schema=ambiguous_schema,
    )[0]
    absent = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={
                        "candidate": decision.proposals[0].candidate.model_copy(
                            update={
                                "physical_column": decision.proposals[
                                    0
                                ].candidate.physical_column.model_copy(
                                    update={"column": "missing"}
                                )
                            }
                        )
                    }
                ),
            )
        }
    )
    assert "missing_probe" not in _research_loop_module._rejected_preflight_assessment_context(
        state,
        absent,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    )[0]
    completed = state.model_copy(
        update={"action_history": (*state.action_history, inspected_action)}
    )
    assert "missing_probe" not in _research_loop_module._rejected_preflight_assessment_context(
        completed,
        decision,
        _freshness(completed),
        requested_action=None,
        loaded_schema=loaded_schema,
    )[0]


@pytest.mark.parametrize("kind", (SemanticItemKind.FILTER, SemanticItemKind.TIME))
def test_rejected_new_discriminator_without_exact_value_gets_search_probe(kind) -> None:
    """A known required categorical predicate needs only its exact value probe."""

    loaded_schema, namespace = _fixture_schema(
        {"public.catalog": {"columns": {"category": {"type": "TEXT"}}}}
    )
    base = _policy_state(namespace, with_evidence=True)
    table = TableRef(namespace="main", schema="public", table="catalog")
    category = ColumnRef(table=table, column="category")
    inspect, inspected = _observed_column_evidence(
        base, category, invocation_id="fictional-category"
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": kind,
            "required": True,
            "exact_physical_predicate": True,
            "exact_physical_column_name": "category",
            "operator": PredicateOperator.EQ,
            "literal_or_reference": "fictional-category",
        }
    )
    state = base.model_copy(
        update={
            "evidence": (*base.evidence, inspected),
            "action_history": (*base.action_history, inspect),
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:fictional-category",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.catalog",
                            "column": "category",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.catalog",
                                "column": "category",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": "fictional-category",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (inspected.evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    )

    assert feedback[0]["missing_probe"] == {
        "tool_name": "search_value",
        "arguments": {
            "table": "public.catalog",
            "column": "category",
            "value": "fictional-category",
            "top_k": 1,
        },
    }
    search = ResearchAction(
        action_id="fictional-category-search",
        kind=ResearchActionKind.SEARCH_VALUE,
        hypothesis_id=None,
        target=category,
        parameters=(("top_k", 1), ("value", "fictional-category")),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.SEARCH_VALUE,
            hypothesis_id=None,
            target=category,
            parameters=(("top_k", 1), ("value", "fictional-category")),
            expected_revision=state.revision,
        ),
        expected_revision=state.revision,
    )
    payload = {"columns": ["category"], "rows": [["fictional-category"]]}
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id="fictional-category-search-evidence",
        action_digest=search.action_digest,
        probe_kind=search.kind,
        status=ProbeStatus.SUCCESS,
        target=category,
        started_at=_NOW,
        completed_at=_NOW,
        summary="trusted categorical value observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )
    exact_value_evidence = probe_result_to_evidence(result, search)
    assert exact_value_evidence is not None
    evidenced = state.model_copy(
        update={
            "evidence": (*state.evidence, exact_value_evidence),
            "action_history": (*state.action_history, search),
        }
    )
    assert "missing_probe" not in _research_loop_module._rejected_preflight_assessment_context(
        evidenced,
        decision,
        _freshness(evidenced),
        requested_action=None,
        loaded_schema=loaded_schema,
    )[0]
    attempted = state.model_copy(
        update={"action_history": (*state.action_history, search)}
    )
    assert "missing_probe" not in _research_loop_module._rejected_preflight_assessment_context(
        attempted,
        decision,
        _freshness(attempted),
        requested_action=None,
        loaded_schema=loaded_schema,
    )[0]
    ambiguous_schema, _ = _fixture_schema(
        {
            "public.catalog": {"columns": {"category": {"type": "TEXT"}}},
            "audit.catalog": {"columns": {"category": {"type": "TEXT"}}},
        }
    )
    ambiguous = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={
                        "candidate": decision.proposals[0].candidate.model_copy(
                            update={
                                "discriminator_column": LogicalColumnRef(
                                    table="catalog", column="category"
                                ),
                                "discriminator_predicate": LogicalPredicate(
                                    left=LogicalColumnRef(
                                        table="catalog", column="category"
                                    ),
                                    operator=PredicateOperator.EQ,
                                    right="fictional-category",
                                ),
                            }
                        )
                    }
                ),
            )
        }
    )
    assert "missing_probe" not in _research_loop_module._rejected_preflight_assessment_context(
        state,
        ambiguous,
        _freshness(state),
        requested_action=None,
        loaded_schema=ambiguous_schema,
    )[0]


def test_existing_binding_assessment_uses_its_unique_missing_durable_citation() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    binding = state.bindings[0]
    evidence_id = state.evidence[0].evidence_id
    unknown = f"{evidence_id[:-1]}{'0' if evidence_id[-1] != '0' else '1'}"
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": binding.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (unknown,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    assert normalized.proposals[0].citation_evidence_ids == (evidence_id,)


def test_candidate_existing_binding_assessment_uses_its_durable_evidence_batch() -> None:
    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    table = base.evidence[0].target
    assert isinstance(table, TableRef)
    first_column = ColumnRef(table=table, column="first_component")
    second_column = ColumnRef(table=table, column="second_component")
    second_action, second_evidence = _observed_column_evidence(
        base,
        second_column,
        invocation_id="second-component-evidence",
    )
    first_binding = PhysicalColumnBinding(
        binding_id="binding-first-component",
        source_id="source-1",
        tables=(table,),
        columns=(first_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(base.evidence[0].evidence_id, second_evidence.evidence_id),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=first_column,
    )
    second_binding = PhysicalColumnBinding(
        binding_id="binding-second-component",
        source_id="source-1",
        tables=(table,),
        columns=(second_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(second_evidence.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=second_column,
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (first_binding.binding_id, second_binding.binding_id),
        }
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", round_trip=True),
            "revision": base.revision + 1,
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "evidence": (*base.evidence, second_evidence),
            "bindings": (first_binding, second_binding),
            "action_history": (*base.action_history, second_action),
        }
    )
    composite_citation = (
        f"composite:{base.evidence[0].evidence_id}:{second_evidence.evidence_id}"
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": first_binding.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (composite_citation,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(
        state,
        decision,
        freshness_context=_fixture_freshness(state),
    )

    assert normalized.proposals[0].citation_evidence_ids == tuple(
        sorted(first_binding.evidence_ids)
    )
    non_fresh_context = _fixture_freshness(state).model_copy(
        update={"schema_namespace_version": "sha256:" + "b" * 64}
    )
    assert _research_loop_module._normalize_model_source_ids(
        state,
        decision,
        freshness_context=non_fresh_context,
    ) is decision
    single_evidence_decision = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(
                    update={
                        "subject": decision.proposals[0].subject.model_copy(
                            update={"binding_id": second_binding.binding_id}
                        )
                    }
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        state,
        single_evidence_decision,
        freshness_context=non_fresh_context,
    ) is single_evidence_decision
    non_consistent = decision.model_copy(
        update={
            "proposals": (
                decision.proposals[0].model_copy(update={"certificate": "contradicted"}),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        state, non_consistent
    ) is non_consistent


def test_existing_proposed_hypothesis_assessment_uses_its_durable_evidence_batch() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True, hypothesis=True)
    first_hypothesis = state.hypotheses[0]
    first_evidence = state.evidence[0]
    table = first_evidence.target
    assert isinstance(table, TableRef)
    second_action, second_evidence = _observed_table_evidence(
        state,
        table,
        invocation_id="hypothesis-reviewed-evidence",
        columns=[],
    )
    second_hypothesis = Hypothesis(
        hypothesis_id="hypothesis-2",
        source_ids=first_hypothesis.source_ids,
        claim="reviewed orders are relevant",
        candidate_targets=first_hypothesis.candidate_targets,
        status=HypothesisStatus.PROPOSED,
        evidence_ids=(second_evidence.evidence_id,),
    )
    state = state.model_copy(
        update={
            "evidence": (first_evidence, second_evidence),
            "hypotheses": (first_hypothesis, second_hypothesis),
            "action_history": (*state.action_history, second_action),
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "hypothesis_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "hypothesis_id": first_hypothesis.hypothesis_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": ("evidence:mistyped-published",),
                },
                {
                    "proposal_type": "hypothesis_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "hypothesis_id": second_hypothesis.hypothesis_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": ("evidence:mistyped-reviewed",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(
        state,
        decision,
        freshness_context=_freshness(state),
    )

    assert tuple(
        proposal.citation_evidence_ids for proposal in normalized.proposals
    ) == (
        tuple(sorted(first_hypothesis.evidence_ids)),
        tuple(sorted(second_hypothesis.evidence_ids)),
    )
    first_decision = decision.model_copy(update={"proposals": (decision.proposals[0],)})

    assert _research_loop_module._normalize_model_source_ids(
        state,
        first_decision,
    ) is first_decision
    stale_context = _freshness(state).model_copy(
        update={"schema_namespace_version": "sha256:" + "b" * 64}
    )
    assert _research_loop_module._normalize_model_source_ids(
        state,
        first_decision,
        freshness_context=stale_context,
    ) is first_decision
    testing_state = state.model_copy(
        update={
            "hypotheses": (
                first_hypothesis.model_copy(update={"status": HypothesisStatus.TESTING}),
                second_hypothesis,
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        testing_state,
        first_decision,
        freshness_context=_freshness(testing_state),
    ) is first_decision
    missing_evidence_state = state.model_copy(
        update={
            "hypotheses": (
                first_hypothesis.model_copy(update={"evidence_ids": ("evidence:missing",)}),
                second_hypothesis,
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        missing_evidence_state,
        first_decision,
        freshness_context=_freshness(missing_evidence_state),
    ) is first_decision
    contradicted = first_decision.model_copy(
        update={
            "proposals": (
                first_decision.proposals[0].model_copy(
                    update={"certificate": "contradicted"}
                ),
            )
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        state,
        contradicted,
        freshness_context=_freshness(state),
    ) is contradicted
    proposed_reference = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_hypothesis",
                    "proposal_key": "proposal:published-hypothesis",
                    "source_ids": first_hypothesis.source_ids,
                    "claim": "published orders are relevant",
                    "candidate_targets": (
                        {"target_kind": "table", "table": "public.orders"},
                    ),
                    "citation_evidence_ids": (first_evidence.evidence_id,),
                },
                {
                    "proposal_type": "hypothesis_assessment",
                    "subject": {
                        "reference_kind": "proposed",
                        "proposal_key": "proposal:published-hypothesis",
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": ("evidence:mistyped-published",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )
    assert _research_loop_module._normalize_model_source_ids(
        state,
        proposed_reference,
        freshness_context=_freshness(state),
    ) is proposed_reference


def test_existing_binding_assessment_citation_stays_fail_closed_without_one_replacement() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    binding = state.bindings[0]
    evidence_id = state.evidence[0].evidence_id
    unknown = f"{evidence_id[:-1]}{'0' if evidence_id[-1] != '0' else '1'}"

    def assessment(binding_id: str, citations: tuple[str, ...]) -> ResearchDecisionV1:
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": binding_id,
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": citations,
                    },
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    exact = assessment(binding.binding_id, (evidence_id,))
    assert _research_loop_module._normalize_model_source_ids(state, exact) is exact

    two_unknown = assessment(binding.binding_id, (unknown, f"{unknown}-other"))
    assert _research_loop_module._normalize_model_source_ids(state, two_unknown) is two_unknown

    unmatched_state = state.model_copy(
        update={"bindings": (binding.model_copy(update={"evidence_ids": ()}),)}
    )
    unmatched = assessment(binding.binding_id, (unknown,))
    assert (
        _research_loop_module._normalize_model_source_ids(unmatched_state, unmatched)
        is unmatched
    )

    other_evidence_id = "invocation:" + "f" * 64
    other_evidence = state.evidence[0].model_copy(
        update={"evidence_id": other_evidence_id}
    )
    ambiguous_state = state.model_copy(
        update={
            "evidence": (*state.evidence, other_evidence),
            "bindings": (
                binding.model_copy(
                    update={"evidence_ids": (evidence_id, other_evidence_id)}
                ),
            ),
        }
    )
    ambiguous = assessment(binding.binding_id, (unknown,))
    assert (
        _research_loop_module._normalize_model_source_ids(ambiguous_state, ambiguous)
        is ambiguous
    )

    foreign_binding_state = ambiguous_state.model_copy(
        update={"bindings": (binding,)}
    )
    foreign_known = assessment(
        binding.binding_id,
        (evidence_id, other_evidence_id, unknown),
    )
    assert (
        _research_loop_module._normalize_model_source_ids(
            foreign_binding_state, foreign_known
        )
        is foreign_known
    )

    unknown_binding = assessment("binding-missing", (unknown,))
    assert (
        _research_loop_module._normalize_model_source_ids(state, unknown_binding)
        is unknown_binding
    )


def test_model_citation_normalization_repairs_only_the_full_durable_batch() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state, document = _document_supported_state_after_probe(
        namespace,
        observed_at=_FIXTURE_NOW,
        valid_until=_FIXTURE_NOW + timedelta(days=1),
    )
    table = TableRef(namespace="main", schema="public", table="orders")
    id_column = ColumnRef(table=table, column="id")
    status_column = ColumnRef(table=table, column="status")
    id_action, id_evidence = _observed_column_evidence(
        state, id_column, invocation_id="id-evidence"
    )
    status_action, status_evidence = _observed_table_evidence(
        state,
        table,
        invocation_id="status-evidence",
        columns=[
            {
                "constraint_type": "",
                "description": "status",
                "name": "status",
                "not_null": "",
                "type": "TEXT",
            }
        ],
    )
    missing_binding = PhysicalColumnBinding(
        binding_id="binding-missing",
        source_id="source-1",
        tables=(table,),
        columns=(status_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(status_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="status observation",
        physical_column=status_column,
    )
    complete_binding = PhysicalColumnBinding(
        binding_id="binding-complete",
        source_id="source-1",
        tables=(table,),
        columns=(id_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(id_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="id observation",
        physical_column=id_column,
    )
    state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (*state.evidence, id_evidence, status_evidence),
            "bindings": (missing_binding, complete_binding),
            "action_history": (*state.action_history, id_action, status_action),
        }
    )
    document_evidence_id = next(
        evidence.evidence_id for evidence in state.evidence if evidence.target == document
    )
    unknown = f"{status_evidence.evidence_id[:-1]}{'0' if status_evidence.evidence_id[-1] != '0' else '1'}"
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:derived-status",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "derived_expression",
                        "expression_claim": "A status-derived value.",
                        "document_id": document.document_id,
                        "rule_excerpt": "Use the documented status rule.",
                        "input_columns": (
                            {"table": "public.orders", "column": "status"},
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": (document_evidence_id, unknown),
                },
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": missing_binding.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (document_evidence_id, unknown),
                },
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": complete_binding.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (id_evidence.evidence_id, unknown),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    derived = next(
        proposal
        for proposal in normalized.proposals
        if proposal.proposal_type == "new_binding"
    )
    assessments = {
        proposal.subject.binding_id: proposal
        for proposal in normalized.proposals
        if proposal.proposal_type == "binding_assessment"
    }
    assert derived.citation_evidence_ids == tuple(
        sorted((document_evidence_id, status_evidence.evidence_id))
    )
    assert assessments[missing_binding.binding_id].citation_evidence_ids == tuple(
        sorted((document_evidence_id, status_evidence.evidence_id))
    )
    assert assessments[complete_binding.binding_id].citation_evidence_ids == (
        id_evidence.evidence_id,
    )
    assert {
        citation
        for proposal in normalized.proposals
        for citation in proposal.citation_evidence_ids
    } <= {evidence.evidence_id for evidence in state.evidence}


def test_derived_expression_citation_stays_fail_closed_without_one_exact_input() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state, document = _document_supported_state_after_probe(
        namespace,
        observed_at=_FIXTURE_NOW,
        valid_until=_FIXTURE_NOW + timedelta(days=1),
    )
    table = TableRef(namespace="main", schema="public", table="orders")
    action, evidence = _observed_table_evidence(
        state,
        table,
        invocation_id="status-evidence",
        columns=[
            {
                "constraint_type": "",
                "description": "status",
                "name": "status",
                "not_null": "",
                "type": "TEXT",
            }
        ],
    )
    state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (*state.evidence, evidence),
            "action_history": (*state.action_history, action),
        }
    )
    document_evidence_id = next(
        evidence.evidence_id for evidence in state.evidence if evidence.target == document
    )
    unknown = f"{evidence.evidence_id[:-1]}{'0' if evidence.evidence_id[-1] != '0' else '1'}"

    def decision(
        citations: tuple[str, ...],
        *,
        table_name: str = "public.orders",
        column_name: str = "status",
    ) -> ResearchDecisionV1:
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "new_binding",
                        "proposal_key": "proposal:derived-status",
                        "source_id": "source-1",
                        "candidate": {
                            "kind": "derived_expression",
                            "expression_claim": "A status-derived value.",
                            "document_id": document.document_id,
                            "rule_excerpt": "Use the documented status rule.",
                            "input_columns": (
                                {"table": table_name, "column": column_name},
                            ),
                        },
                        "join_references": (),
                        "citation_evidence_ids": citations,
                    },
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    exact = decision((document_evidence_id, evidence.evidence_id))
    assert _research_loop_module._normalize_model_source_ids(state, exact) is exact

    two_unknown = decision((document_evidence_id, unknown, f"{unknown}-other"))
    assert _research_loop_module._normalize_model_source_ids(state, two_unknown) is two_unknown

    wrong_case = decision((document_evidence_id, unknown), table_name="public.Orders")
    assert _research_loop_module._normalize_model_source_ids(state, wrong_case) is wrong_case

    wrong_column = decision((document_evidence_id, unknown), column_name="missing")
    assert (
        _research_loop_module._normalize_model_source_ids(state, wrong_column)
        is wrong_column
    )

    other_action, other_evidence = _observed_table_evidence(
        state,
        table,
        invocation_id="other-status-evidence",
        columns=[
            {
                "constraint_type": "",
                "description": "status",
                "name": "status",
                "not_null": "",
                "type": "TEXT",
            }
        ],
    )
    ambiguous_state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (*state.evidence, other_evidence),
            "action_history": (*state.action_history, other_action),
        }
    )
    one_unknown = decision((document_evidence_id, unknown))
    assert (
        _research_loop_module._normalize_model_source_ids(ambiguous_state, one_unknown)
        is one_unknown
    )

    profile_action, profile_evidence = _observed_table_evidence(
        state,
        table,
        invocation_id="profile-status-evidence",
        columns=[
            {
                "constraint_type": "",
                "description": "status",
                "name": "status",
                "not_null": "",
                "type": "TEXT",
            }
        ],
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
    )
    profile_state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (state.evidence[0], profile_evidence),
            "action_history": (*state.action_history, profile_action),
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(profile_state, one_unknown)
        is one_unknown
    )

    unmatched_action, unmatched_evidence = _observed_table_evidence(
        state,
        table,
        invocation_id="unmatched-status-evidence",
        columns=[],
        status="missing",
    )
    unmatched_state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (state.evidence[0], unmatched_evidence),
            "action_history": (*state.action_history, unmatched_action),
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(unmatched_state, one_unknown)
        is one_unknown
    )

    malformed_action, malformed_evidence = _observed_table_evidence(
        state,
        table,
        invocation_id="malformed-status-evidence",
        columns=[{"name": 1}],
    )
    malformed_state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (state.evidence[0], malformed_evidence),
            "action_history": (*state.action_history, malformed_action),
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(malformed_state, one_unknown)
        is one_unknown
    )

    corrupt_observation = evidence.model_copy(
        update={"observation": '{"observation_version":1,"provenance":{}}'}
    )
    corrupt_state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (state.evidence[0], corrupt_observation),
        }
    )
    assert (
        _research_loop_module._normalize_model_source_ids(corrupt_state, one_unknown)
        is one_unknown
    )


def test_unknown_binding_assessment_is_canonicalized_only_when_unambiguous() -> None:
    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    table = base.evidence[0].target
    assert isinstance(table, TableRef)
    column = ColumnRef(table=table, column="status")

    def state_with_candidates(candidate_ids: tuple[str, ...]) -> ResearchState:
        bindings = tuple(
            PhysicalColumnBinding(
                binding_id=binding_id,
                source_id="source-1",
                tables=(table,),
                columns=(column,),
                predicates=(),
                join_path=(),
                evidence_ids=(base.evidence[0].evidence_id,),
                confidence=0.0,
                status=BindingStatus.CANDIDATE,
                validator_rule=None,
                physical_column=column,
            )
            for binding_id in candidate_ids
        )
        item = base.query_spec.semantic_items[0].model_copy(
            update={
                "status": SemanticItemStatus.PARTIALLY_RESOLVED,
                "binding_ids": candidate_ids,
            }
        )
        return base.model_copy(
            update={
                "bindings": bindings,
                "query_spec": base.query_spec.model_copy(
                    update={"semantic_items": (item,)}
                ),
            }
        )

    def decision(*binding_ids: str) -> ResearchDecisionV1:
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": tuple(
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": binding_id,
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": (base.evidence[0].evidence_id,),
                    }
                    for binding_id in binding_ids
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    unique = state_with_candidates(("binding-candidate",))
    normalized = _research_loop_module._normalize_model_source_ids(
        unique, decision("binding-candidatf")
    )
    assert normalized.proposals[0].subject.binding_id == "binding-candidate"

    no_candidates = state_with_candidates(())
    unknown = decision("binding-candidatf")
    assert _research_loop_module._normalize_model_source_ids(
        no_candidates, unknown
    ) is unknown

    additional_candidate = state_with_candidates(
        ("binding-candidate", "binding-other")
    )
    normalized = _research_loop_module._normalize_model_source_ids(
        additional_candidate, unknown
    )
    assert normalized.proposals[0].subject.binding_id == "binding-candidate"

    multiple_unknown = decision("binding-unknown", "binding-other")
    assert _research_loop_module._normalize_model_source_ids(
        unique, multiple_unknown
    ) is multiple_unknown

    valid = decision("binding-candidate")
    assert _research_loop_module._normalize_model_source_ids(unique, valid) is valid

    mixed = decision("binding-candidate", "binding-candidatf")
    normalized = _research_loop_module._normalize_model_source_ids(unique, mixed)
    assert tuple(
        proposal.subject.binding_id for proposal in normalized.proposals
    ) == ("binding-candidate", "binding-candidate")

    mixed_reference_kinds = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:other-binding",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (base.evidence[0].evidence_id,),
                },
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": "binding-candidatf",
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (base.evidence[0].evidence_id,),
                },
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "proposed",
                        "proposal_key": "proposal:other-binding",
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (base.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )
    normalized = _research_loop_module._normalize_model_source_ids(
        unique, mixed_reference_kinds
    )
    assert normalized.proposals[0].proposal_type == "binding_assessment"
    assert normalized.proposals[0].subject.binding_id == "binding-candidate"
    assert normalized.proposals[1].subject.proposal_key == "proposal:other-binding"


def test_existing_binding_assessment_binding_id_typo_needs_one_unique_match() -> None:
    _loaded_schema, namespace = _fixture_schema()
    base = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    binding = base.bindings[0]
    evidence_id = base.evidence[0].evidence_id

    def state_with_bindings(*binding_ids: str) -> ResearchState:
        return base.model_copy(
            update={
                "bindings": tuple(
                    binding.model_copy(update={"binding_id": binding_id})
                    for binding_id in binding_ids
                )
            }
        )

    def decision(*binding_ids: str) -> ResearchDecisionV1:
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": tuple(
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": binding_id,
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": (evidence_id,),
                    }
                    for binding_id in binding_ids
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    unique = state_with_bindings("binding-alpha", "binding-bravo")
    normalized = _research_loop_module._normalize_model_source_ids(
        unique, decision("binding-alphb", "binding-bravo")
    )
    assert tuple(
        proposal.subject.binding_id for proposal in normalized.proposals
    ) == ("binding-alpha", "binding-bravo")

    no_match = decision("binding-alphb")
    assert _research_loop_module._normalize_model_source_ids(
        state_with_bindings(), no_match
    ) is no_match

    ambiguous = decision("binding-alphb")
    assert _research_loop_module._normalize_model_source_ids(
        state_with_bindings("binding-alpha", "binding-alphc"), ambiguous
    ) is ambiguous


def test_source_id_typo_is_not_normalized_without_one_unique_match() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    second_item = state.query_spec.semantic_items[0].model_copy(
        update={"source_id": "source-2"}
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        *state.query_spec.semantic_items,
                        second_item,
                    )
                }
            )
        }
    )

    def decision(source_id: str) -> ResearchDecisionV1:
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "new_binding",
                        "proposal_key": "proposal:status-filter",
                        "source_id": source_id,
                        "candidate": {
                            "kind": "physical_column",
                            "physical_column": {
                                "table": "public.orders",
                                "column": "status",
                            },
                        },
                        "join_references": (),
                        "citation_evidence_ids": (
                            state.evidence[0].evidence_id,
                        ),
                    },
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    ambiguous = _research_loop_module._normalize_model_source_ids(
        state, decision("source-x")
    )
    two_changes = _research_loop_module._normalize_model_source_ids(
        state, decision("source-xx")
    )
    unrelated = _research_loop_module._normalize_model_source_ids(
        state, decision("unrelated")
    )

    assert ambiguous.proposals[0].source_id == "source-x"
    assert two_changes.proposals[0].source_id == "source-xx"
    assert unrelated.proposals[0].source_id == "unrelated"


@pytest.mark.parametrize("model_source_id", ("source-", "source-1x", "source-x"))
def test_source_id_single_edit_is_normalized_when_unique(
    model_source_id: str,
) -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:status-filter",
                    "source_id": model_source_id,
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    normalized = _research_loop_module._normalize_model_source_ids(state, decision)

    assert normalized.proposals[0].source_id == "source-1"


def test_exact_duplicate_existing_bindings_are_assessed_without_retry(
    tmp_path,
) -> None:
    """A corrected repeated binding reuses its evidence as an assessment."""

    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _policy_state(namespace, with_evidence=True)
    citation = state.evidence[0].evidence_id
    registry = _make_registry(namespace)
    new_binding = {
        "proposal_type": "new_binding",
        "proposal_key": "proposal:status",
        "source_id": "source-1",
        "candidate": {
            "kind": "physical_column",
            "physical_column": {
                "table": "public.orders",
                "column": "status",
            },
        },
        "join_references": (),
        "citation_evidence_ids": (citation,),
    }
    second_new_binding = {
        **new_binding,
        "proposal_key": "proposal:status-copy",
        "candidate": {
            "kind": "physical_column",
            "physical_column": {
                "table": "public.orders",
                "column": "id",
            },
        },
    }
    prepared = _resolve_fixture(
        ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (new_binding, second_new_binding),
                "next": {"next_kind": "semantic_commit"},
            }
        ),
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=registry,
    )
    bindings = prepared.admission.bindings
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "binding_ids": tuple(item.binding_id for item in bindings),
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
        }
    )
    state = state.model_copy(
        update={
            "bindings": bindings,
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
        }
    )
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        tmp_path / "adaptive.sqlite",
        states=(initial, state),
        events=(
            (seed, "planned", {"kind": "seed"}),
            (seed, "observed", {"kind": "seed"}),
        ),
    )
    model_calls = 0

    def research_context(
        current: ResearchState,
        _feedbacks: tuple[str, ...],
        _rejected_duplicates: tuple[dict[str, object], ...] = (),
        rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
    ) -> str:
        return json.dumps(
            {
                "state": canonical_digest(current),
                "rejected_preflight_assessments": list(
                    rejected_preflight_assessments
                ),
            }
        )

    async def model(prompt: str) -> str:
        nonlocal model_calls
        model_calls += 1
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": bindings[0].binding_id,
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": (citation,),
                    },
                    {**new_binding, "source_id": "source-"},
                    second_new_binding,
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    ledger = AdaptiveBudgetLedger(tmp_path / "duplicate-binding-feedback.sqlite")
    _seed_prior_model_budget(state, ledger)
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            registry=registry,
            budget_ledger=ledger,
            research_context=research_context,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.COMPLETE
        assert all(
            item.status is BindingStatus.SUPPORTED
            for item in outcome.final_state.bindings
        )
        assert model_calls == 1
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_rejected_join_assessment_names_one_missing_relationship_probe() -> None:
    """A rejected direct join assessment keeps its batch and names one probe."""

    _loaded_schema, namespace = _fixture_schema(
        {
            "public.orders": {
                "columns": {
                    "customer_id": {"type": "INTEGER"},
                    "invoice_id": {"type": "INTEGER"},
                }
            },
            "public.customers": {"columns": {"id": {"type": "INTEGER"}}},
            "public.invoices": {"columns": {"id": {"type": "INTEGER"}}},
        }
    )
    base = _policy_state(namespace, with_evidence=True)
    orders = TableRef(namespace="main", schema="public", table="orders")
    customers = TableRef(namespace="main", schema="public", table="customers")
    invoices = TableRef(namespace="main", schema="public", table="invoices")
    orders_customer_id = ColumnRef(table=orders, column="customer_id")
    customers_id = ColumnRef(table=customers, column="id")
    orders_invoice_id = ColumnRef(table=orders, column="invoice_id")
    invoices_id = ColumnRef(table=invoices, column="id")

    def candidate(join_id: str, left: ColumnRef, right: ColumnRef) -> JoinCandidate:
        return JoinCandidate(
            join_id=join_id,
            left=left,
            right=right,
            join_type=JoinType.INNER,
            path=(JoinEdge(left=left, right=right, join_type=JoinType.INNER),),
            status=JoinCandidateStatus.CANDIDATE,
            evidence_ids=(),
        )

    state = base.model_copy(
        update={
            "join_candidates": (
                candidate("join-customer", orders_customer_id, customers_id),
                candidate("join-invoice", orders_invoice_id, invoices_id),
            ),
        }
    )
    citations = (state.evidence[0].evidence_id,)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "join_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "join_id": "join-customer",
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": citations,
                },
                {
                    "proposal_type": "join_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "join_id": "join-invoice",
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": citations,
                },
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": "binding-1",
                    },
                    "certificate": "insufficient",
                    "citation_evidence_ids": citations,
                },
                {
                    "proposal_type": "hypothesis_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "hypothesis_id": "hypothesis-1",
                    },
                    "certificate": "insufficient",
                    "citation_evidence_ids": citations,
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.orders"},
                },
            },
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state, decision, _freshness(state), requested_action=None
    )

    assert [item["proposal"] for item in feedback] == sorted(
        (
            proposal.model_dump(mode="json", by_alias=True)
            for proposal in decision.proposals
        ),
        key=canonical_digest,
    )
    missing_probes = [item["missing_probe"] for item in feedback if "missing_probe" in item]
    assert missing_probes == [
        {
            "tool_name": "inspect_relationships",
            "arguments": {"table": "public.customers", "top_k": 50, "depth": 1},
        }
    ]


def test_rejected_join_assessment_skips_certified_or_completed_probe() -> None:
    """Exact relationship evidence or an exact action prevents a repeat hint."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    orders = TableRef(namespace="main", schema="public", table="orders")
    customers = TableRef(namespace="main", schema="public", table="customers")
    left = ColumnRef(table=orders, column="customer_id")
    right = ColumnRef(table=customers, column="id")
    join = JoinCandidate(
        join_id="join-customer",
        left=left,
        right=right,
        join_type=JoinType.INNER,
        path=(JoinEdge(left=left, right=right, join_type=JoinType.INNER),),
        status=JoinCandidateStatus.CANDIDATE,
        evidence_ids=(),
    )
    parameters = (("depth", 1), ("top_k", 50))
    action = ResearchAction(
        action_id="relationships-customers",
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        hypothesis_id=None,
        target=customers,
        parameters=parameters,
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
            hypothesis_id=None,
            target=customers,
            parameters=parameters,
            expected_revision=base.revision,
        ),
        expected_revision=base.revision,
    )
    payload = {
        "relationships": [
            {
                "relationship_kind": "declared",
                "from_table": "public.orders",
                "to_table": "public.customers",
                "column_pairs": [{"from_column": "customer_id", "to_column": "id"}],
            }
        ]
    }
    result = build_probe_result(
        run_id=base.run_id,
        run_incarnation=base.run_incarnation,
        revision=base.revision,
        schema_namespace_version=base.schema_namespace_version,
        invocation_id="relationships-certificate",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=customers,
        started_at=_NOW,
        completed_at=_NOW,
        summary="declared relationship",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "join_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "join_id": join.join_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": (base.evidence[0].evidence_id,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.orders"},
                },
            },
        }
    )

    certified = base.model_copy(
        update={"join_candidates": (join,), "evidence": (*base.evidence, evidence)}
    )
    completed = base.model_copy(
        update={"join_candidates": (join,), "action_history": (*base.action_history, action)}
    )

    assert _research_loop_module._rejected_preflight_assessment_context(
        certified, decision, _freshness(certified), requested_action=None
    ) == (
        {
            "proposal": decision.proposals[0].model_dump(mode="json", by_alias=True),
            "existing_evidence_id": evidence.evidence_id,
        },
    )
    assert _research_loop_module._rejected_preflight_assessment_context(
        completed, decision, _freshness(completed), requested_action=None
    ) == ({"proposal": decision.proposals[0].model_dump(mode="json", by_alias=True)},)


def test_rejected_physical_binding_assessment_keeps_exact_column_evidence(
    tmp_path,
) -> None:
    """Preflight feedback keeps fresh column evidence after a bad citation."""

    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    base = _policy_state(namespace, with_evidence=True)
    old_evidence = base.evidence[0]
    player = TableRef(namespace="main", schema=None, table="Player")
    height = ColumnRef(table=player, column="height")
    action = ResearchAction(
        action_id="player-height-inspection",
        kind=ResearchActionKind.INSPECT_COLUMN,
        hypothesis_id=None,
        target=height,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_COLUMN,
            hypothesis_id=None,
            target=height,
            parameters=(),
            expected_revision=base.revision,
        ),
        expected_revision=base.revision,
    )
    payload = {
        "status": "matched",
        "column": height.model_dump(mode="json", by_alias=True),
    }
    result = build_probe_result(
        run_id=base.run_id,
        run_incarnation=base.run_incarnation,
        revision=base.revision,
        schema_namespace_version=base.schema_namespace_version,
        invocation_id="player-height-evidence",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=height,
        started_at=_NOW,
        completed_at=_NOW,
        summary="trusted Player.height observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    binding = PhysicalColumnBinding(
        binding_id="player-height-binding",
        source_id="source-1",
        tables=(player,),
        columns=(height,),
        predicates=(),
        join_path=(),
        evidence_ids=(old_evidence.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=height,
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (binding.binding_id,),
        }
    )
    state = base.model_copy(
        update={
            "evidence": (old_evidence, evidence),
            "bindings": (binding,),
            "action_history": base.action_history,
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
        }
    )
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        tmp_path / "adaptive.sqlite",
        states=(initial, state),
        events=((seed, "planned", {"kind": "seed"}), (seed, "observed", {"kind": "seed"})),
    )
    responses = iter(
        (
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [
                        {
                            "proposal_type": "binding_assessment",
                            "subject": {
                                "reference_kind": "existing",
                                "binding_id": binding.binding_id,
                            },
                            "certificate": "consistent",
                            "citation_evidence_ids": [old_evidence.evidence_id],
                        }
                    ],
                    "next": {
                        "next_kind": "tool",
                        "hypothesis_ref": None,
                        "intent": {
                            "tool_name": "inspect_table",
                            "arguments": {"table": "public.customers"},
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [old_evidence.evidence_id],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [old_evidence.evidence_id],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            ),
        )
    )
    prompts: list[dict[str, object]] = []

    async def model(prompt: str) -> str:
        prompts.append(json.loads(prompt))
        return next(responses)

    def research_context(
        current: ResearchState,
        _feedbacks: tuple[str, ...],
        _rejected_duplicates: tuple[dict[str, object], ...] = (),
        rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
    ) -> str:
        context: dict[str, object] = {"state": canonical_digest(current)}
        if rejected_preflight_assessments:
            context["rejected_preflight_assessments"] = list(
                rejected_preflight_assessments
            )
        return json.dumps(context)

    ledger = AdaptiveBudgetLedger(tmp_path / "player-height-feedback.sqlite")
    _seed_prior_model_budget(state, ledger)
    registry = _make_registry(namespace)
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            registry=registry,
            budget_ledger=ledger,
            research_context=research_context,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.TOOL_FAILURE
        assert outcome.final_state.bindings == state.bindings
        assert len(prompts) == 1
        assert registry.adapter.execute_calls == 1
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_duplicate_rejection_clears_prior_preflight_feedback(tmp_path, monkeypatch) -> None:
    """A duplicate retry must not be hidden behind an older preflight rejection."""

    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    preflight_calls = 0

    def preflight(_self, _state, _decision):
        nonlocal preflight_calls
        preflight_calls += 1
        if preflight_calls == 1:
            return (
                "UNRESOLVABLE_PREFLIGHT",
                None,
                None,
                ({"missing_probe": {"tool_name": "inspect_column"}},),
            )
        return (
            "DUPLICATE_ACTION",
            None,
            {"kind": "inspect_table"},
            (),
        )

    monkeypatch.setattr(
        _research_loop_module._ResearchLoopCoordinator,
        "_preflight_model_decision",
        preflight,
    )
    contexts: list[tuple[object, ...]] = []
    prompts: list[dict[str, object]] = []

    def research_context(*arguments: object) -> str:
        contexts.append(arguments)
        return json.dumps({"state": canonical_digest(arguments[0])})

    async def model(prompt: str) -> str:
        prompts.append(json.loads(prompt))
        if '"review_kind":"research_stop_review"' in prompt:
            return '{"decision":"stop_confirmed","hint":null}'
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":'
            '{"table":"public.orders"}}}}'
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            registry=_make_registry(namespace),
            research_context=research_context,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert len(prompts) == 3
        assert [len(context) for context in contexts[:3]] == [2, 4, 3]
        assert contexts[2][2] == ({"kind": "inspect_table"},)
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_repeated_preflight_rejection_triggers_stop_review(tmp_path, monkeypatch) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    rejected_batches = iter(
        (
            ({"proposal": {"proposal_key": "proposal:first"}},),
            ({"proposal": {"proposal_key": "proposal:threshold"}},),
        )
    )

    monkeypatch.setattr(
        _research_loop_module._ResearchLoopCoordinator,
        "_preflight_model_decision",
        lambda _self, _state, _decision: (
            "UNRESOLVABLE_PREFLIGHT",
            None,
            None,
            next(rejected_batches),
        ),
    )
    prompts: list[str] = []

    def research_context(
        current: ResearchState,
        _feedbacks: tuple[str, ...],
        _rejected_duplicates: tuple[dict[str, object], ...] = (),
        rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
    ) -> str:
        return json.dumps(
            {
                "rejected_preflight_assessments": list(
                    rejected_preflight_assessments
                ),
                "state": canonical_digest(current),
            }
        )

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":'
            '{"table":"public.orders"}}}}'
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            registry=_make_registry(namespace),
            research_context=research_context,
        )
    )
    try:
        assert outcome.final_state.bindings == state.bindings
        assert len(prompts) == 3
        assert prompts[1] != prompts[2]
        assert '"review_kind":"research_stop_review"' in prompts[2]
        review = json.loads(prompts[2])
        review_context = json.loads(review["input"]["research_context"])
        assert review_context["rejected_preflight_assessments"] == [
            {"proposal": {"proposal_key": "proposal:threshold"}}
        ]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize(
    (
        "rejection_kind",
        "expected_feedback",
        "expected_rejection_path",
        "expected_log_code",
    ),
    (
        (
            "stop",
            "STOP_WITH_PROPOSALS",
            "stop_with_proposals",
            "STOP_WITH_PROPOSALS",
        ),
        (
            "raw_query_limit",
            "RAW_RESEARCH_QUERY_LIMIT",
            "research_query_admission",
            "research_query_limit",
        ),
        (
            "raw_query_admission",
            "INVALID_RESEARCH_QUERY",
            "research_query_admission",
            "research_query_star",
        ),
        (
            "resolver",
            "UNRESOLVABLE_PREFLIGHT",
            "unresolvable_preflight",
            "UNRESOLVABLE_PREFLIGHT",
        ),
        (
            "missing_source",
            "UNRESOLVABLE_PREFLIGHT",
            "unresolvable_preflight",
            "UNRESOLVABLE_PREFLIGHT",
        ),
        (
            "missing_hypothesis_source",
            "UNRESOLVABLE_PREFLIGHT",
            "unresolvable_preflight",
            "UNRESOLVABLE_PREFLIGHT",
        ),
        (
            "missing_join",
            "UNRESOLVABLE_PREFLIGHT",
            "unresolvable_preflight",
            "UNRESOLVABLE_PREFLIGHT",
        ),
        (
            "missing_hypothesis",
            "UNRESOLVABLE_PREFLIGHT",
            "unresolvable_preflight",
            "UNRESOLVABLE_PREFLIGHT",
        ),
        (
            "missing_proposal_citation",
            "UNRESOLVABLE_PREFLIGHT",
            "unresolvable_preflight",
            "UNRESOLVABLE_PREFLIGHT",
        ),
        (
            "semantic_admission",
            "UNRESOLVABLE_PREFLIGHT",
            "unresolvable_preflight",
            "UNRESOLVABLE_PREFLIGHT",
        ),
    ),
)
def test_invalid_model_decision_is_retried_with_trusted_feedback(
    tmp_path,
    rejection_kind: str,
    expected_feedback: str,
    expected_rejection_path: str,
    expected_log_code: str,
    caplog,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _policy_state(namespace, with_evidence=True)
    citation = state.evidence[0].evidence_id
    database = tmp_path / f"{rejection_kind}-retry.sqlite"
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        database,
        states=(initial, state),
        events=(
            (seed, "planned", {"kind": "seed"}),
            (seed, "observed", {"kind": "seed"}),
        ),
    )
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    ledger = AdaptiveBudgetLedger(
        tmp_path / f"{rejection_kind}-retry-budget.sqlite"
    )
    registry = _make_registry(namespace)
    prompts: list[str] = []
    invalid_proposals: list[dict[str, object]] = [
        {
            "proposal_type": "new_binding",
            "proposal_key": "proposal:rejected",
            "source_id": "source-1",
            "candidate": {
                "kind": "physical_column",
                "physical_column": {
                    "table": "public.orders",
                    "column": "status",
                },
            },
            "join_references": [],
            "citation_evidence_ids": [citation],
        }
    ]
    invalid_next: dict[str, object]
    if rejection_kind == "stop":
        invalid_next = {
            "next_kind": "stop",
            "reason": "complete",
            "source_ids": [],
            "citation_evidence_ids": [citation],
        }
    elif rejection_kind == "raw_query_limit":
        invalid_next = {
            "next_kind": "tool",
            "hypothesis_ref": None,
            "intent": {
                "tool_name": "execute_research_probe",
                "arguments": {
                    "sql": "SELECT status FROM public.orders",
                    "parameters": [],
                },
            },
        }
    elif rejection_kind == "raw_query_admission":
        invalid_next = {
            "next_kind": "tool",
            "hypothesis_ref": None,
            "intent": {
                "tool_name": "execute_research_probe",
                "arguments": {
                    "sql": "SELECT o.* FROM public.orders AS o LIMIT 10",
                    "parameters": [],
                },
            },
        }
    elif rejection_kind == "resolver":
        invalid_next = {
            "next_kind": "tool",
            "hypothesis_ref": None,
            "intent": {
                "tool_name": "inspect_table",
                "arguments": {"table": "public.missing"},
            },
        }
    elif rejection_kind == "missing_source":
        invalid_proposals[0]["source_id"] = "missing-source"
        invalid_next = {
            "next_kind": "tool",
            "hypothesis_ref": None,
            "intent": {
                "tool_name": "inspect_column",
                "arguments": {"table": "public.orders", "column": "status"},
            },
        }
    elif rejection_kind == "missing_hypothesis_source":
        invalid_proposals = [
            {
                "proposal_type": "new_hypothesis",
                "proposal_key": "proposal:rejected",
                "source_ids": ["missing-source"],
                "claim": "orders are relevant",
                "candidate_targets": [
                    {"target_kind": "table", "table": "public.orders"},
                ],
                "citation_evidence_ids": [citation],
            }
        ]
        invalid_next = {
            "next_kind": "tool",
            "hypothesis_ref": None,
            "intent": {
                "tool_name": "inspect_column",
                "arguments": {"table": "public.orders", "column": "status"},
            },
        }
    elif rejection_kind == "missing_join":
        invalid_proposals[0]["join_references"] = [
            {"reference_kind": "existing", "join_id": "missing-join"},
        ]
        invalid_next = {
            "next_kind": "tool",
            "hypothesis_ref": None,
            "intent": {
                "tool_name": "inspect_column",
                "arguments": {"table": "public.orders", "column": "status"},
            },
        }
    elif rejection_kind == "missing_hypothesis":
        invalid_next = {
            "next_kind": "tool",
            "hypothesis_ref": {
                "reference_kind": "existing",
                "hypothesis_id": "missing-hypothesis",
            },
            "intent": {
                "tool_name": "inspect_column",
                "arguments": {"table": "public.orders", "column": "status"},
            },
        }
    elif rejection_kind == "missing_proposal_citation":
        invalid_proposals[0]["citation_evidence_ids"] = ["missing-evidence"]
        invalid_next = {
            "next_kind": "tool",
            "hypothesis_ref": None,
            "intent": {
                "tool_name": "inspect_column",
                "arguments": {"table": "public.orders", "column": "status"},
            },
        }
    else:
        invalid_next = {
            "next_kind": "tool",
            "hypothesis_ref": None,
            "intent": {
                "tool_name": "inspect_column",
                "arguments": {"table": "public.orders", "column": "status"},
            },
        }
        invalid_proposals = [
            {
                "proposal_type": "new_binding",
                "proposal_key": f"proposal:binding-{index}",
                "source_id": "source-1",
                "candidate": {
                    "kind": "discriminator_value",
                    "discriminator_column": {"table": table, "column": "status"},
                    "discriminator_predicate": {
                        "left": {"table": table, "column": "status"},
                                "operator": PredicateOperator.EQ,
                        "right": "paid",
                    },
                },
                "join_references": [],
                "citation_evidence_ids": [citation],
            }
            for index, table in enumerate(("public.orders", "orders"), start=1)
        ]
    if rejection_kind in {
        "missing_source",
        "missing_hypothesis_source",
        "missing_join",
        "missing_proposal_citation",
        "semantic_admission",
    }:
        invalid_next = {
            "next_kind": "tool",
            "hypothesis_ref": None,
            "intent": {
                "tool_name": "inspect_table",
                "arguments": {"table": "public.missing"},
            },
        }
    responses = iter(
        (
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": invalid_proposals,
                    "next": invalid_next,
                }
            ),
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [citation],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [citation],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            ),
        )
    )

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    try:
        _seed_prior_model_budget(state, ledger)
        with caplog.at_level(
            logging.WARNING, logger=_research_loop_module.__name__
        ):
            outcome = asyncio.run(
                run_research_loop(
                    initial_state=state,
                    task="research schema",
                    research_context=lambda current, _feedbacks, *_details: canonical_digest(current),
                    model=model,
                    model_identity="test/model",
                    adapter=SchemaResearchDecisionAdapter(
                        load_schema_research_agent_profile()
                    ),
                    loaded_schema=loaded_schema,
                    freshness_context=_fixture_freshness(state),
                    registry=registry,
                    state_store=state_store,
                    checkpoint_store=checkpoint_store,
                    budget_ledger=ledger,
                    policy=_policy(),
                )
            )

        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert outcome.final_state.revision == state.revision
        assert outcome.final_state.bindings == state.bindings
        assert len(prompts) == 2
        assert expected_feedback not in json.loads(prompts[0])["instructions"]
        assert expected_feedback in json.loads(prompts[1])["instructions"]
        if rejection_kind == "resolver":
            retry_instructions = json.loads(prompts[1])["instructions"]
            assert (
                "Use the rejected preflight proposal details in the research context."
                in retry_instructions
            )
        diagnostics = [
            record.message
            for record in caplog.records
            if record.name == _research_loop_module.__name__
            and record.message.startswith("typed_schema_research_decision ")
        ]
        assert diagnostics == [
            "typed_schema_research_decision retry=true "
            f"code={expected_log_code} "
            f"rejection_path={expected_rejection_path}"
        ]
        records = ledger.load_model_records(state.run_id, state.run_incarnation)
        retry_records = records[-2:]
        assert [record.reservation.call_id for record in retry_records] == [
            "research-model-1-0",
            "research-model-1-1",
        ]
        assert (
            retry_records[0].reservation.request_digest
            != retry_records[1].reservation.request_digest
        )
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id,
                    state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    state.revision,
                )
            ).planned
            is None
        )
        assert ledger.load_records(state.run_id, state.run_incarnation) == ()
        assert registry.adapter.execute_calls == 0
        assert registry.adapter.recover_calls == 0
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_conflicting_model_semantic_commit_is_retried_with_feedback(tmp_path) -> None:
    """A model cannot turn a validated join back into a candidate."""

    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _policy_state(namespace, with_evidence=True)
    citation = state.evidence[0].evidence_id
    registry = _make_registry(namespace)
    forward = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_join",
                    "proposal_key": "proposal:forward-join",
                    "left": {"table": "public.orders", "column": "status"},
                    "right": {"table": "public.customers", "column": "id"},
                    "join_type": JoinType.INNER,
                    "path": (),
                    "citation_evidence_ids": (citation,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )
    join = _resolve_fixture(
        forward,
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=registry,
    ).admission.join_candidates[0].model_copy(
        update={"status": JoinCandidateStatus.VALIDATED}
    )
    state = state.model_copy(update={"join_candidates": (join,)})
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        tmp_path / "adaptive.sqlite",
        states=(initial, state),
        events=(
            (seed, "planned", {"kind": "seed"}),
            (seed, "observed", {"kind": "seed"}),
        ),
    )
    responses = iter(
        (
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [
                        {
                            "proposal_type": "new_join",
                            "proposal_key": "proposal:reversed-join",
                            "left": {
                                "table": "public.customers",
                                "column": "id",
                            },
                            "right": {"table": "public.orders", "column": "status"},
                            "join_type": "inner",
                            "path": [],
                            "citation_evidence_ids": [citation],
                        }
                    ],
                    "next": {"next_kind": "semantic_commit"},
                }
            ),
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [citation],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [citation],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            ),
        )
    )
    prompts: list[dict[str, object]] = []

    async def model(prompt: str) -> str:
        prompts.append(json.loads(prompt))
        return next(responses)

    ledger = AdaptiveBudgetLedger(
        tmp_path / "semantic-commit-conflict-budget.sqlite"
    )
    _seed_prior_model_budget(state, ledger)
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            registry=registry,
            budget_ledger=ledger,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert outcome.final_state.action_history == state.action_history
        assert outcome.final_state.join_candidates == (join,)
        assert len(prompts) == 2
        assert "UNRESOLVABLE_PREFLIGHT" not in prompts[0]["instructions"]
        assert "UNRESOLVABLE_PREFLIGHT" in prompts[1]["instructions"]
        assert checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            )
        ).planned is None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_conflicting_model_join_proposal_with_invalid_tool_is_retried(
    tmp_path,
) -> None:
    """A conflicting join and invalid tool are retried without execution."""

    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _policy_state(namespace, with_evidence=True)
    citation = state.evidence[0].evidence_id
    registry = _make_registry(namespace)
    forward = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_join",
                    "proposal_key": "proposal:forward-join",
                    "left": {"table": "public.orders", "column": "status"},
                    "right": {"table": "public.customers", "column": "id"},
                    "join_type": JoinType.INNER,
                    "path": (),
                    "citation_evidence_ids": (citation,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )
    join = _resolve_fixture(
        forward,
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=registry,
    ).admission.join_candidates[0].model_copy(
        update={"status": JoinCandidateStatus.VALIDATED}
    )
    state = state.model_copy(update={"join_candidates": (join,)})
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        tmp_path / "adaptive.sqlite",
        states=(initial, state),
        events=(
            (seed, "planned", {"kind": "seed"}),
            (seed, "observed", {"kind": "seed"}),
        ),
    )
    duplicate = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_join",
                    "proposal_key": "proposal:duplicate-join",
                    "left": {"table": "public.customers", "column": "id"},
                    "right": {"table": "public.orders", "column": "status"},
                    "join_type": JoinType.INNER,
                    "path": (),
                    "citation_evidence_ids": (citation,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.missing"},
                },
            },
        }
    )
    responses = iter(
        (
            json.dumps(duplicate.model_dump(mode="json", by_alias=True)),
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [citation],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [citation],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            ),
        )
    )
    prompts: list[dict[str, object]] = []

    async def model(prompt: str) -> str:
        prompts.append(json.loads(prompt))
        return next(responses)

    ledger = AdaptiveBudgetLedger(tmp_path / "join-proposal-tool-conflict.sqlite")
    _seed_prior_model_budget(state, ledger)
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            registry=registry,
            budget_ledger=ledger,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert outcome.final_state.action_history == state.action_history
        assert outcome.final_state.join_candidates == (join,)
        assert len(prompts) == 2
        assert "UNRESOLVABLE_PREFLIGHT" not in prompts[0]["instructions"]
        assert "UNRESOLVABLE_PREFLIGHT" in prompts[1]["instructions"]
        assert registry.adapter.execute_calls == 0
        assert checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            )
        ).planned is None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_strict_schema_invalid_model_decision_is_retried_with_feedback(
    tmp_path, caplog
) -> None:
    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _policy_state(namespace, with_evidence=True)
    citation = state.evidence[0].evidence_id
    database = tmp_path / "strict-schema-retry.sqlite"
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        database,
        states=(initial, state),
        events=(
            (seed, "planned", {"kind": "seed"}),
            (seed, "observed", {"kind": "seed"}),
        ),
    )
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    ledger = AdaptiveBudgetLedger(tmp_path / "strict-schema-retry-budget.sqlite")
    registry = _make_registry(namespace)
    prompts: list[str] = []
    context_feedbacks: list[tuple[str, ...]] = []

    def research_context(current: ResearchState, feedbacks: tuple[str, ...] = ()) -> str:
        context_feedbacks.append(feedbacks)
        return canonical_digest(current)
    responses = iter(
        (
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [citation],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [citation],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [citation],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [citation],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            ),
        )
    )

    async def model(prompt: str) -> str | SchemaResearchModelResponse:
        prompts.append(prompt)
        raw_response = next(responses)
        if len(prompts) == 1:
            return SchemaResearchModelResponse(
                raw_response=raw_response,
                usage=ModelTokenUsage(input_tokens=3, output_tokens=2),
            )
        return raw_response

    try:
        _seed_prior_model_budget(state, ledger)
        with caplog.at_level(
            logging.WARNING, logger=_research_loop_module.__name__
        ):
            outcome = asyncio.run(
                run_research_loop(
                    initial_state=state,
                    task="research schema",
                    research_context=research_context,
                    model=model,
                    model_identity="test/model",
                    adapter=SchemaResearchDecisionAdapter(
                        load_schema_research_agent_profile()
                    ),
                    loaded_schema=loaded_schema,
                    freshness_context=_fixture_freshness(state),
                    registry=registry,
                    state_store=state_store,
                    checkpoint_store=checkpoint_store,
                    budget_ledger=ledger,
                    policy=_policy(),
                )
            )

        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert len(prompts) == 2
        assert context_feedbacks == [(), ("INVALID_DECISION",)]
        assert "INVALID_DECISION" not in json.loads(prompts[0])["instructions"]
        assert "INVALID_DECISION" in json.loads(prompts[1])["instructions"]
        assert (
            "Correct the decision using the profile rules and return a replacement typed "
            "decision."
            in json.loads(prompts[1])["instructions"]
        )
        diagnostics = [
            record.message
            for record in caplog.records
            if record.name == _research_loop_module.__name__
            and record.message.startswith("typed_schema_research_decision ")
        ]
        assert diagnostics == [
            "typed_schema_research_decision retry=true "
            "code=INVALID_DECISION rejection_path=contract_decode"
        ]
        records = ledger.load_model_records(state.run_id, state.run_incarnation)
        assert [record.reservation.call_id for record in records[-2:]] == [
            "research-model-1-0",
            "research-model-1-1",
        ]
        assert records[-2].reconciliation is not None
        assert records[-2].reconciliation.actual_usage == ModelTokenUsage(
            input_tokens=3,
            output_tokens=2,
        )
        assert records[-2].reconciliation.charged_total_tokens == 5
        assert records[-2].reconciliation.usage_was_conservative is False
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            )
        )
        assert snapshot.planned is None
        assert ledger.load_records(state.run_id, state.run_incarnation) == ()
        assert registry.adapter.execute_calls == 0
        assert registry.adapter.recover_calls == 0
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_minimum_schema_research_prompt_exhausts_budget_before_provider(tmp_path) -> None:
    state = _state(required=True)
    policy = _policy()
    profile = load_schema_research_agent_profile()
    calls = 0

    def research_context(
        _current: ResearchState,
        _feedbacks: tuple[str, ...] = (),
    ) -> str:
        prompt = build_schema_research_prompt(
            profile,
            task="research schema",
            research_context="",
        )
        if len(prompt.encode("utf-8")) > policy.model_budget.input_tokens_per_call * 4:
            raise BudgetAdmissionError("schema-research prompt exceeds input envelope")
        return "{}"

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "{}"

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            policy=policy,
            research_context=research_context,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.BUDGET_EXHAUSTED
        assert calls == 0
        assert ledger.load_model_records(state.run_id, state.run_incarnation) == ()
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_model_decision_keeps_ordered_retry_feedback_after_an_unavailable_probe(
    tmp_path,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(model_calls=5)
    state = _policy_state(namespace, with_evidence=True).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    citation = state.evidence[0].evidence_id
    state_store = AdaptiveResearchStateStore(tmp_path / "feedback-state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "feedback-checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "feedback-budget.sqlite")
    prompts: list[str] = []

    def research_query(sql: str) -> str:
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "execute_research_probe",
                        "arguments": {"sql": sql, "parameters": []},
                    },
                },
            }
        )

    responses = iter(
        (
            research_query("SELECT status FROM public.orders"),
            research_query("DELETE FROM public.orders"),
            research_query("SELECT id + 1 FROM public.orders ORDER BY id LIMIT 1"),
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [citation],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [citation],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            ),
        )
    )

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=policy,
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "feedback-owner",
        model_wait=None,
    )
    try:
        decision, reason, _terminal_freshness_context = asyncio.run(
            coordinator._model_decision(state, "PROBE_UNAVAILABLE")
        )

        assert reason is None
        assert decision is not None
        instructions = [json.loads(prompt)["instructions"] for prompt in prompts]
        feedback_markers = (
            "Previous probe unavailable: PROBE_UNAVAILABLE.",
            "Previous decision rejected: RAW_RESEARCH_QUERY_LIMIT.",
            "Previous decision rejected: INVALID_RESEARCH_QUERY.",
            "Previous decision rejected: INVALID_RESEARCH_QUERY_OUTPUT.",
        )
        assert len(instructions) == len(feedback_markers)
        for index, current in enumerate(instructions):
            expected_prefix = feedback_markers[: index + 1]
            assert [current.count(marker) for marker in feedback_markers] == [
                1 if marker in expected_prefix else 0 for marker in feedback_markers
            ]
            assert [current.index(marker) for marker in expected_prefix] == sorted(
                current.index(marker) for marker in expected_prefix
            )
        first_record = ledger.load_model_records(
            state.run_id,
            state.run_incarnation,
        )[0]
        assert first_record.reservation.request_digest == canonical_digest(
            {
                "research_context": canonical_digest(state),
                "state": state.model_dump(mode="json", by_alias=True),
                "task": "research schema",
                "validation_feedback": "PROBE_UNAVAILABLE",
            }
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_execute_probe_limit_is_capped_without_dropping_proposals() -> None:
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:status",
                    "source_id": "source-1",
                    "citation_evidence_ids": ("evidence-1",),
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                    },
                    "join_references": (),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "execute_research_probe",
                    "arguments": {
                        "sql": (
                            "SELECT status FROM public.orders "
                            "ORDER BY status LIMIT 10"
                        ),
                        "parameters": (),
                    },
                },
            },
        }
    )

    bounded = _research_loop_module._cap_execute_research_probe_limit(
        decision,
        maximum_row_limit=4,
        dialect="postgres",
    )

    assert bounded.proposals == decision.proposals
    assert bounded.next.intent.arguments.sql == (
        "SELECT status FROM public.orders ORDER BY status LIMIT 4"
    )

    def with_sql(sql: str) -> ResearchDecisionV1:
        arguments = decision.next.intent.arguments.model_copy(update={"sql": sql})
        intent = decision.next.intent.model_copy(update={"arguments": arguments})
        return decision.model_copy(
            update={"next": decision.next.model_copy(update={"intent": intent})}
        )

    for unchanged_sql in (
        "SELECT status FROM public.orders ORDER BY status LIMIT 4",
        "SELECT status FROM public.orders ORDER BY status",
        "SELECT status FROM public.orders ORDER BY status LIMIT ?",
        "SELECT status FROM public.orders ORDER BY status LIMIT 0",
        "SELECT status FROM public.orders ORDER BY status LIMIT '10'",
        "SELECT status FROM public.orders ORDER BY status LIMIT 10 OFFSET 1",
    ):
        unchanged = with_sql(unchanged_sql)
        assert _research_loop_module._cap_execute_research_probe_limit(
            unchanged,
            maximum_row_limit=4,
            dialect="postgres",
        ) == unchanged

    nested = with_sql(
        "SELECT status FROM (SELECT status FROM public.orders LIMIT 10) AS recent "
        "ORDER BY status LIMIT 8"
    )
    bounded_nested = _research_loop_module._cap_execute_research_probe_limit(
        nested,
        maximum_row_limit=4,
        dialect="postgres",
    )
    assert bounded_nested.next.intent.arguments.sql == (
        "SELECT status FROM (SELECT status FROM public.orders LIMIT 4) AS recent "
        "ORDER BY status LIMIT 4"
    )

    nested_union = with_sql(
        "WITH recent AS (SELECT status FROM public.orders UNION ALL "
        "SELECT status FROM public.orders LIMIT 10) "
        "SELECT status FROM recent LIMIT 4"
    )
    bounded_union = _research_loop_module._cap_execute_research_probe_limit(
        nested_union,
        maximum_row_limit=4,
        dialect="postgres",
    )
    assert bounded_union.next.intent.arguments.sql.count("LIMIT 4") == 2
    assert "LIMIT 10" not in bounded_union.next.intent.arguments.sql


def test_invalid_research_query_logs_safe_failure_code(caplog) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(6)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    registry = _make_registry(namespace)
    decision = _tool_decision(
        "execute_research_probe",
        {
            "sql": "SELECT o.* FROM public.orders AS o LIMIT 10",
            "parameters": [],
        },
    )

    with caplog.at_level(logging.WARNING, logger=_research_loop_module.__name__):
        feedback = _research_loop_module._model_research_query_admission_feedback(
            state,
            decision,
            loaded_schema,
            registry,
        )

    assert feedback == ("INVALID_RESEARCH_QUERY", "research_query_star")
    diagnostics = [
        record.message
        for record in caplog.records
        if record.name == _research_loop_module.__name__
        and record.message.startswith("typed_schema_research_query ")
    ]
    assert diagnostics == [
        "typed_schema_research_query retry=true code=research_query_star"
    ]
    assert "SELECT" not in diagnostics[0]
    assert "orders" not in diagnostics[0]


@pytest.mark.parametrize(
    ("sql", "feedback_value", "failure_code"),
    (
        (
            "SELECT missing FROM public.orders ORDER BY id LIMIT 1",
            "INVALID_RESEARCH_QUERY_COLUMN",
            "research_query_column",
        ),
        (
            "SELECT id + 1 FROM public.orders ORDER BY id LIMIT 1",
            "INVALID_RESEARCH_QUERY_OUTPUT",
            "research_query_output",
        ),
    ),
)
def test_research_query_admission_feedback_preserves_closed_subtype(
    sql: str,
    feedback_value: str,
    failure_code: str,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    decision = _tool_decision(
        "execute_research_probe", {"sql": sql, "parameters": []}
    )

    assert _research_loop_module._model_research_query_admission_feedback(
        state, decision, loaded_schema, registry
    ) == (feedback_value, failure_code)


def test_missing_group_order_executes_without_model_correction(
    tmp_path,
    monkeypatch,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    sql = (
        "SELECT status, COUNT(*) AS n FROM public.orders "
        "GROUP BY status LIMIT 10"
    )
    decision = _tool_decision(
        "execute_research_probe",
        {"sql": sql, "parameters": []},
    )
    assert (
        _research_loop_module._model_research_query_admission_feedback(
            state,
            decision,
            loaded_schema,
            registry,
        )
        is None
    )
    payload = {"columns": ["status", "n"], "rows": [["open", 2], ["paid", 1]]}
    probe_cost = EvidenceCost(
        wall_clock_ms=0,
        model_calls=0,
        model_tokens=0,
        db_probe_ms=0,
        rows=2,
        bytes=len(canonical_json_bytes(payload)),
    )
    budget_ledger = AdaptiveBudgetLedger(tmp_path / "group-budget.sqlite")
    invocation_ids: list[str] = []

    def execute_grouped_probe(resolved, _tools, *, recover=False):
        assert recover is False
        action = resolved.admission.action
        invocation = resolved.invocation
        assert action is not None
        assert invocation is not None
        invocation_ids.append(invocation.invocation_id)
        result = build_probe_result(
            run_id=resolved.admission.state.run_id,
            run_incarnation=resolved.admission.state.run_incarnation,
            revision=action.expected_revision,
            schema_namespace_version=resolved.admission.state.schema_namespace_version,
            invocation_id=invocation.invocation_id,
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=action.target,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="grouped fixture success",
            cost=probe_cost,
            row_count=2,
            payload=payload,
        )
        result, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            probe_cost,
            lambda _reservation: result,
            config=_policy(),
            ledger=budget_ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 0,
            owner_token_factory=lambda: "group-probe-owner",
        )
        return result

    monkeypatch.setattr(
        _research_loop_module,
        "execute_resolved_research_decision",
        execute_grouped_probe,
    )
    prompts: list[str] = []

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        if '"review_kind":"research_stop_review"' in prompt:
            return '{"decision":"stop_confirmed","hint":null}'
        if len(prompts) == 1:
            return json.dumps(decision.model_dump(mode="json", by_alias=True))
        assert invocation_ids
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "ambiguous",
                    "source_ids": ("source-1",),
                    "citation_evidence_ids": [invocation_ids[0]],
                    "ambiguity": {
                        "interpretations": ["First reading.", "Second reading."],
                        "citation_evidence_ids": [invocation_ids[0]],
                        "missing_distinguishing_fact": "The definition is absent.",
                    },
                },
            }
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=registry,
            budget_ledger=budget_ledger,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert outcome.final_state.revision == 1
        assert len(invocation_ids) == 1
        assert len(prompts) == 3
        assert "INVALID_RESEARCH_QUERY" not in json.loads(prompts[1])["instructions"]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_trusted_research_query_dialect_failure_is_not_model_feedback(
    tmp_path,
    monkeypatch,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    prompts: list[str] = []

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"execute_research_probe","arguments":'
            '{"sql":"SELECT COUNT(*) AS n FROM public.orders LIMIT 1",'
            '"parameters":[]}}}}'
        )

    def fail_trusted_dialect(_plugin):
        raise ResearchQueryAdmissionError(
            "research_query_dialect",
            "trusted dialect is invalid",
        )

    monkeypatch.setattr(
        _research_loop_module,
        "dialect_for_plugin",
        fail_trusted_dialect,
    )
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=registry,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        assert len(prompts) == 1
        assert "INVALID_RESEARCH_QUERY" not in prompts[0]
        assert len(ledger.load_model_records(state.run_id, state.run_incarnation)) == 1
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            )
        )
        assert snapshot.planned is None
        assert registry.adapter.execute_calls == 0
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_trusted_runtime_failure_is_not_retried_as_model_feedback(
    tmp_path,
    monkeypatch,
) -> None:
    import custom_tools.text_to_sql.adaptive.decision_resolver as resolver_module

    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    prompts: list[str] = []

    async def model(prompt: str):
        import custom_tools.text_to_sql.adaptive.schema_research_agent as agent_contracts

        prompts.append(prompt)
        return getattr(agent_contracts, "SchemaResearchModelResponse")(
            raw_response=(
                '{"decision_version":1,"proposals":[],"next":'
                '{"next_kind":"tool","hypothesis_ref":null,"intent":'
                '{"tool_name":"inspect_table",'
                '"arguments":{"table":"public.orders"}}}}'
            ),
            usage=ModelTokenUsage(input_tokens=3, output_tokens=2),
        )

    def fail_seal(_registry):
        raise RuntimeError("trusted runtime failed")

    monkeypatch.setattr(
        resolver_module,
        "capture_trusted_execution_seal",
        fail_seal,
    )
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=registry,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        assert len(prompts) == 1
        assert "INVALID_DECISION" not in prompts[0]
        records = ledger.load_model_records(state.run_id, state.run_incarnation)
        assert len(records) == 1
        assert records[0].result is not None
        assert records[0].reconciliation is not None
        assert records[0].reconciliation.actual_usage == ModelTokenUsage(
            input_tokens=3,
            output_tokens=2,
        )
        assert records[0].reconciliation.charged_total_tokens == 5
        assert records[0].reconciliation.usage_was_conservative is False
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            )
        )
        assert snapshot.planned is None
        assert ledger.load_records(state.run_id, state.run_incarnation) == ()
        assert registry.adapter.execute_calls == 0
        assert registry.adapter.recover_calls == 0
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_non_retryable_preflight_failure_logs_safe_reason_once(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)

    async def model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table",'
            '"arguments":{"table":"public.orders"}}}}'
        )

    def fail_preflight(*_args, **_kwargs):
        try:
            raise ValueError("internal detail must not be logged")
        except ValueError as cause:
            raise DecisionResolverError(
                "semantic decision admission failed"
            ) from cause

    monkeypatch.setattr(
        _research_loop_module._ResearchLoopCoordinator,
        "_resolve_current_decision",
        fail_preflight,
    )
    with caplog.at_level(logging.WARNING, logger=_research_loop_module.__name__):
        outcome, state_store, checkpoint_store, ledger = asyncio.run(
            _run(
                tmp_path,
                state,
                model,
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
            )
        )
    try:
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        diagnostics = [
            record.message
            for record in caplog.records
            if record.name == _research_loop_module.__name__
            and record.message.startswith("typed_schema_research_preflight ")
        ]
        assert diagnostics == [
            "typed_schema_research_preflight retry=false "
            "code=PRECHECK_INTERNAL error_class=DecisionResolverError "
            "cause_class=ValueError"
        ]
        assert "semantic decision admission failed" not in diagnostics[0]
        assert "internal detail must not be logged" not in diagnostics[0]
        assert len(ledger.load_model_records(state.run_id, state.run_incarnation)) == 1
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            )
        )
        assert snapshot.planned is None
        assert snapshot.terminal is not None
        assert registry.adapter.execute_calls == 0
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_provider_failure_logs_safe_reason_without_retry(
    tmp_path,
    caplog,
) -> None:
    state = _state(required=True)
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider detail must not be logged")

    with caplog.at_level(logging.WARNING, logger=_research_loop_module.__name__):
        outcome, state_store, checkpoint_store, ledger = asyncio.run(
            _run(tmp_path, state, model)
        )
    try:
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        assert calls == 1
        diagnostics = [
            record.message
            for record in caplog.records
            if record.name == _research_loop_module.__name__
            and record.message.startswith("typed_schema_research_decision ")
        ]
        assert diagnostics == [
            "typed_schema_research_decision retry=false "
            "code=PROVIDER_OR_ADAPTER error_class=RuntimeError"
        ]
        assert "provider detail must not be logged" not in diagnostics[0]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_resolution_failure_after_preflight_logs_safe_reason(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    calls = 0
    resolve_current = _research_loop_module._ResearchLoopCoordinator._resolve_current_decision

    async def model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table",'
            '"arguments":{"table":"public.orders"}}}}'
        )

    def fail_after_preflight(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DecisionResolverError("resolution detail must not be logged")
        return resolve_current(self, *args, **kwargs)

    monkeypatch.setattr(
        _research_loop_module._ResearchLoopCoordinator,
        "_resolve_current_decision",
        fail_after_preflight,
    )
    with caplog.at_level(logging.WARNING, logger=_research_loop_module.__name__):
        outcome, state_store, checkpoint_store, ledger = asyncio.run(
            _run(
                tmp_path,
                state,
                model,
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
            )
        )
    try:
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        assert calls == 2
        diagnostics = [
            record.message
            for record in caplog.records
            if record.name == _research_loop_module.__name__
            and record.message.startswith("typed_schema_research_decision ")
        ]
        assert diagnostics == [
            "typed_schema_research_decision retry=false "
            "code=DECISION_RESOLUTION_INTERNAL "
            "error_class=DecisionResolverError"
        ]
        assert "resolution detail must not be logged" not in diagnostics[0]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_checkpoint_plan_write_failure_logs_safe_reason(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)

    async def model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table",'
            '"arguments":{"table":"public.orders"}}}}'
        )

    def fail_plan_write(*_args, **_kwargs):
        raise AdaptiveCheckpointCasError("checkpoint detail must not be logged")

    monkeypatch.setattr(
        _research_loop_module.AdaptiveStateStore,
        "record_planned",
        fail_plan_write,
    )
    with caplog.at_level(logging.WARNING, logger=_research_loop_module.__name__):
        outcome, state_store, checkpoint_store, ledger = asyncio.run(
            _run(
                tmp_path,
                state,
                model,
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
            )
        )
    try:
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        diagnostics = [
            record.message
            for record in caplog.records
            if record.name == _research_loop_module.__name__
            and record.message.startswith("typed_schema_research_decision ")
        ]
        assert diagnostics == [
            "typed_schema_research_decision retry=false "
            "code=CHECKPOINT_PLAN_WRITE error_class=AdaptiveCheckpointCasError"
        ]
        assert "checkpoint detail must not be logged" not in diagnostics[0]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_model_budget_integrity_failure_is_not_retried_as_model_feedback(
    tmp_path,
    monkeypatch,
) -> None:
    state = _state(required=True)
    prompts: list[str] = []

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table",'
            '"arguments":{"table":"public.orders"}}}}'
        )

    def fail_reconciliation(*_args, **_kwargs):
        raise ValueError("model ledger integrity failed")

    monkeypatch.setattr(
        _research_loop_module,
        "_state_with_reconciled_model_budget",
        fail_reconciliation,
    )
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, state, model)
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        assert len(prompts) == 1
        assert "INVALID_DECISION" not in prompts[0]
        records = ledger.load_model_records(state.run_id, state.run_incarnation)
        assert len(records) == 1
        assert records[0].result is not None
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            )
        )
        assert snapshot.planned is None
        assert ledger.load_records(state.run_id, state.run_incarnation) == ()
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_model_reconciliation_write_failure_leaves_no_clean_terminal(tmp_path) -> None:
    class _ReconciliationFailureLedger(AdaptiveBudgetLedger):
        def record_model_reconciliation(self, reconciliation, result):
            raise OSError("model reconciliation storage failed")

    state = _state(required=True)
    ledger = _ReconciliationFailureLedger(tmp_path / "broken-model-budget.sqlite")
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
        )

    outcome, state_store, checkpoint_store, returned_ledger = asyncio.run(
        _run(tmp_path, state, model, budget_ledger=ledger)
    )
    try:
        assert returned_ledger is ledger
        assert calls == 1
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        record = ledger.load_model_records(state.run_id, state.run_incarnation)[0]
        assert record.started is not None
        assert record.result is not None
        assert record.reconciliation is None
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
            )
        )
        assert snapshot.terminal is None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_model_result_write_failure_leaves_started_without_terminal(tmp_path) -> None:
    class _ResultFailureLedger(AdaptiveBudgetLedger):
        def record_model_result(self, result, *, owner_token):
            raise OSError("model result storage failed")

    state = _state(required=True)
    ledger = _ResultFailureLedger(tmp_path / "broken-model-result.sqlite")

    async def model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
        )

    outcome, state_store, checkpoint_store, returned_ledger = asyncio.run(
        _run(tmp_path, state, model, budget_ledger=ledger)
    )
    try:
        assert returned_ledger is ledger
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        record = ledger.load_model_records(state.run_id, state.run_incarnation)[0]
        assert record.started is not None
        assert record.result is None
        assert record.reconciliation is None
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
            )
        )
        assert snapshot.terminal is None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize("_repeat", range(20))
def test_cited_ambiguous_stop_is_closed_once(tmp_path, _repeat: int) -> None:
    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _policy_state(namespace, with_evidence=True)
    citation = state.evidence[0].evidence_id
    ambiguity = {
        "interpretations": [
            "Revenue means invoiced amount.",
            "Revenue means collected payment amount.",
        ],
        "citation_evidence_ids": [citation],
        "missing_distinguishing_fact": "The metric definition is absent.",
    }
    handled_ambiguity = {
        **ambiguity,
        "citation_evidence_handles": ["e1"],
    }
    handled_ambiguity.pop("citation_evidence_ids")
    ledger = AdaptiveBudgetLedger(tmp_path / "ambiguous-budget.sqlite")
    calls = 0

    async def model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        if '"review_kind":"research_stop_review"' in prompt:
            return '{"decision":"stop_confirmed","hint":null}'
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"stop","reason":"ambiguous",'
            '"source_handles":["s1"],"citation_evidence_handles":["e1"],'
            '"ambiguity":%s}}' % json.dumps(handled_ambiguity)
        )

    async def seed_model_budget(_reservation) -> ModelTokenUsage:
        return ModelTokenUsage(input_tokens=None, output_tokens=None)

    asyncio.run(
        execute_model_call_with_budget_async(
            state.run_id,
            state.run_incarnation,
            "research-model-0-0",
            canonical_digest({"seed": "revision-0"}),
            "test/model",
            10,
            10,
            seed_model_budget,
            config=_policy(),
            ledger=ledger,
            claim_now_ns=lambda: 0,
            owner_token_factory=lambda: "seed-model-owner",
        )
    )
    database = tmp_path / "ambiguous.sqlite"
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        database,
        states=(initial, state),
        events=(
            (seed, "planned", {"kind": "seed"}),
            (seed, "observed", {"kind": "seed"}),
        ),
    )
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    try:
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=_make_registry(namespace),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert outcome.final_state.revision == state.revision
        assert outcome.final_state.action_history == state.action_history
        assert outcome.final_state.evidence == state.evidence
        assert outcome.final_state.budget_state.used_model_calls == 3
        assert outcome.final_state.budget_state.used_model_tokens == 60
        assert outcome.affected_source_ids == ("source-1",)
        assert outcome.citation_evidence_ids == (citation,)
        assert outcome.ambiguity.model_dump(mode="json") == ambiguity
        assert calls == 2
        assert len(ledger.load_model_records(state.run_id, state.run_incarnation)) == 3
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 1
                )
            ).terminal
            is not None
        )
        terminal = research_stop_terminal_result(
            state.run_id,
            outcome.stop_reason,
            outcome.ambiguity,
        )
        assert terminal is not None
        assert terminal.executed is False
        assert terminal.ambiguity == outcome.ambiguity

        async def replay_model(_prompt: str) -> str:
            raise AssertionError("terminal replay must not call the model")

        replay = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=replay_model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=_make_registry(namespace),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert replay == outcome
        assert replay.ambiguity == outcome.ambiguity
        assert replay.final_state.budget_state.used_model_calls == 3
        assert replay.final_state.budget_state.used_model_tokens == 60
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize("_repeat", range(20))
def test_duplicate_semantic_action_stagnates_before_second_tool(
    tmp_path, monkeypatch, _repeat: int
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(8)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    registry = _make_registry(namespace)
    ledger = AdaptiveBudgetLedger(tmp_path / "duplicate-budget.sqlite")
    tool_calls = 0

    def execute_once(resolved, _tools, *, recover=False):
        nonlocal tool_calls
        assert recover is False
        tool_calls += 1
        action = resolved.admission.action
        invocation = resolved.invocation
        assert action is not None and invocation is not None
        maximum_cost = EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes({"ok": True})),
        )
        result, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            maximum_cost,
            lambda _reservation: build_probe_result(
                run_id=state.run_id,
                run_incarnation=state.run_incarnation,
                revision=action.expected_revision,
                schema_namespace_version=state.schema_namespace_version,
                invocation_id=invocation.invocation_id,
                action_digest=action.action_digest,
                probe_kind=action.kind,
                status=ProbeStatus.SUCCESS,
                target=action.target,
                started_at=_FIXTURE_NOW,
                completed_at=_FIXTURE_NOW,
                summary="one semantic observation",
                cost=maximum_cost,
                row_count=1,
                payload={"ok": True},
            ),
            config=policy,
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "duplicate-tool-owner",
        )
        return result

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", execute_once
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
            budget_ledger=ledger,
            policy=policy,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert outcome.final_state.revision == 1
        assert len(outcome.final_state.action_history) == 1
        assert len(outcome.final_state.evidence) == 1
        assert calls == 4
        assert tool_calls == 1
        assert outcome.rejection_signatures == (
            ("duplicate_action", "DUPLICATE_ACTION"),
        )
        checkpoint = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 1
            )
        )
        assert checkpoint.terminal is not None
        assert checkpoint.terminal.action["rejection_signatures"] == [
            ["duplicate_action", "DUPLICATE_ACTION"]
        ]

        async def replay_model(_prompt: str) -> str:
            raise AssertionError("terminal replay must not call the model")

        replay = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=replay_model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=policy,
            )
        )
        assert replay == outcome
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_stop_review_continue_passes_hint_to_one_normal_research_turn(tmp_path) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(8)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    calls: list[str] = []
    invalid_stop = (
        '{"decision_version":1,"proposals":[],"next":'
        '{"next_kind":"stop","reason":"ambiguous",'
        '"source_ids":["source-1"],"citation_evidence_ids":["citation-1"],'
        '"ambiguity":{"interpretations":["First reading.","Second reading."],'
        '"citation_evidence_ids":["citation-1"],'
        '"missing_distinguishing_fact":"The definition is absent."}}}'
    )

    async def model(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 3:
            assert '"review_kind":"research_stop_review"' in prompt
            return (
                '{"decision":"continue","hint":'
                '"Inspect the visible relationship from the supported facts."}'
            )
        if len(calls) == 4:
            assert (
                "Inspect the visible relationship from the supported facts."
            ) in prompt
        return invalid_stop

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
            policy=policy,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert outcome.final_state.revision == 0
        assert len(calls) == 4
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize(
    (
        "kind",
        "normalized_meaning",
        "literal",
        "status",
        "exact_physical_predicate",
        "overrides_reviewer_hint",
    ),
    (
        (
            SemanticItemKind.FILTER,
            "body_text = 'needle'",
            "needle",
            SemanticItemStatus.UNRESOLVED,
            True,
            True,
        ),
        (
            SemanticItemKind.TIME,
            "event_time = '2024-01-01'",
            "2024-01-01",
            SemanticItemStatus.UNRESOLVED,
            True,
            True,
        ),
        (
            SemanticItemKind.FILTER,
            "body_text = 'needle'",
            "needle",
            SemanticItemStatus.RESOLVED,
            True,
            False,
        ),
        (
            SemanticItemKind.TIME,
            "event_time = '2024-01-01'",
            "2024-01-01",
            SemanticItemStatus.RESOLVED,
            True,
            False,
        ),
        (
            SemanticItemKind.FILTER,
            "body_text = 'needle'",
            "needle",
            SemanticItemStatus.UNRESOLVED,
            False,
            False,
        ),
        (
            SemanticItemKind.TIME,
            "event_time = '2024-01-01'",
            "2024-01-01",
            SemanticItemStatus.UNRESOLVED,
            False,
            False,
        ),
    ),
)
def test_stop_review_canonicalizes_only_unresolved_exact_physical_predicates(
    tmp_path,
    kind: SemanticItemKind,
    normalized_meaning: str,
    literal: str,
    status: SemanticItemStatus,
    exact_physical_predicate: bool,
    overrides_reviewer_hint: bool,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    source_id = "semantic:body-filter"
    base = (
        _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
        if status is SemanticItemStatus.RESOLVED
        else _policy_state(namespace)
    )
    bindings = (
        (
            base.bindings[0].model_copy(update={"source_id": source_id}),
        )
        if status is SemanticItemStatus.RESOLVED
        else ()
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "source_id": source_id,
            "kind": kind,
            "source_text": "body text",
            "normalized_meaning": normalized_meaning,
            "required": True,
            "exact_physical_predicate": exact_physical_predicate,
            "operator": PredicateOperator.EQ,
            "literal_or_reference": literal,
            "status": status,
            "binding_ids": (
                (bindings[0].binding_id,)
                if status is SemanticItemStatus.RESOLVED
                else ()
            ),
        }
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", by_alias=True, round_trip=True),
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "unresolved_items": (
                (source_id,) if status is not SemanticItemStatus.RESOLVED else ()
            ),
            "bindings": bindings,
        }
    )
    state_store = AdaptiveResearchStateStore(tmp_path / "state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")

    async def no_decision_model(_prompt: str) -> str:
        raise AssertionError("_review_stop must not call the decision model")

    async def review_model(_prompt: str) -> str:
        return '{"decision":"continue","hint":"Research the title instead."}'

    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=no_decision_model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "exact-physical-predicate-hint-owner",
        model_wait=None,
        stop_review_model=review_model,
    )
    try:
        hint, _attempt = asyncio.run(
            coordinator._review_stop(state, ResearchStopReason.STAGNATED, "{}", 0)
        )
        continuation_source_ids = (
            coordinator._exact_physical_predicate_continuation_source_ids
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    assert hint is not None
    if overrides_reviewer_hint:
        assert source_id in hint
        assert normalized_meaning in hint
        assert "title" not in hint.casefold()
        assert continuation_source_ids == (source_id,)
        assert "create the corresponding typed binding" in hint
        assert "citing only that evidence" in hint
        assert "semantic_commit" in hint
        assert "do not repeat a probe" in hint
        assert "acquire only the missing evidence" in hint
        assert "does not select a physical table or column" in hint
        assert "physical candidates must come from schema or evidence" in hint
        assert "confirms the named column" not in hint
        assert "confirms the physical column from schema or evidence" in hint
    else:
        assert hint == "Research the title instead."
        assert continuation_source_ids == ()


def test_exact_predicate_stop_review_hint_persists_after_nonresolving_commit(
    tmp_path,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    source_id = "semantic:body-filter"
    normalized_meaning = "body_text = 'needle'"
    base = _policy_state(namespace)
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "source_id": source_id,
            "kind": SemanticItemKind.FILTER,
            "source_text": "body text",
            "normalized_meaning": normalized_meaning,
            "required": True,
            "exact_physical_predicate": True,
            "operator": PredicateOperator.EQ,
            "literal_or_reference": "needle",
            "status": SemanticItemStatus.UNRESOLVED,
            "binding_ids": (),
        }
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", by_alias=True, round_trip=True),
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "unresolved_items": (source_id,),
        }
    )
    state_store = AdaptiveResearchStateStore(tmp_path / "state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    prompts: list[dict[str, object]] = []
    responses = iter(
        (
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_table",
                        "arguments": {"table": "public.customers"},
                    },
                },
            },
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_table",
                        "arguments": {"table": "public.orders"},
                    },
                },
            },
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_table",
                        "arguments": {"table": "public.orders"},
                    },
                },
            },
        )
    )

    async def model(prompt: str) -> str:
        prompts.append(json.loads(prompt))
        return json.dumps(next(responses))

    async def review_model(_prompt: str) -> str:
        return '{"decision":"continue","hint":"Research the title instead."}'

    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "exact-predicate-persistence-owner",
        model_wait=None,
        stop_review_model=review_model,
    )
    try:
        hint, _attempt = asyncio.run(
            coordinator._review_stop(state, ResearchStopReason.STAGNATED, "{}", 0)
        )
        assert hint is not None
        coordinator._pending_stop_review_hint = hint
        first, reason, _freshness = asyncio.run(coordinator._model_decision(state))
        assert first is not None
        assert reason is None
        resolved, reason = coordinator._resolve(state, first)
        assert resolved is not None
        assert reason is None
        assert resolved.admission.action is not None
        assert resolved.invocation is not None
        probe_result = build_probe_result(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            revision=state.revision,
            schema_namespace_version=state.schema_namespace_version,
            invocation_id=resolved.invocation.invocation_id,
            action_digest=resolved.admission.action.action_digest,
            probe_kind=resolved.admission.action.kind,
            status=ProbeStatus.SUCCESS,
            target=resolved.admission.action.target,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="fixture success",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=1,
                bytes=11,
            ),
            row_count=1,
            payload={"ok": True},
        )
        committed = commit_semantic_turn(
            resolved.admission,
            probe_result=probe_result,
        )
        assert committed.state.revision == state.revision + 1
        assert committed.state.query_spec.semantic_items[0].status is SemanticItemStatus.UNRESOLVED

        second, reason, _freshness = asyncio.run(
            coordinator._model_decision(committed.state)
        )
        assert second is not None
        assert reason is None
        evidence = committed.state.evidence[-1]
        table = TableRef(namespace="main", schema="public", table="customers")
        column = ColumnRef(table=table, column="id")
        binding = PhysicalColumnBinding(
            binding_id="binding:resolved-body-filter",
            source_id=source_id,
            tables=(table,),
            columns=(column,),
            predicates=(),
            join_path=(),
            evidence_ids=(evidence.evidence_id,),
            confidence=1.0,
            status=BindingStatus.SUPPORTED,
            validator_rule="schema evidence",
            physical_column=column,
        )
        resolved_item = committed.state.query_spec.semantic_items[0].model_copy(
            update={
                "status": SemanticItemStatus.RESOLVED,
                "binding_ids": (binding.binding_id,),
            }
        )
        resolved_state = ResearchState.model_validate(
            {
                **committed.state.model_dump(
                    mode="python",
                    by_alias=True,
                    round_trip=True,
                ),
                "query_spec": committed.state.query_spec.model_copy(
                    update={"semantic_items": (resolved_item,)}
                ),
                "bindings": (binding,),
                "unresolved_items": (),
            }
        )
        third, reason, _freshness = asyncio.run(
            coordinator._model_decision(resolved_state)
        )
        assert third is not None
        assert reason is None
        assert coordinator._exact_physical_predicate_continuation_source_ids == ()
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    first_context = prompts[0]["input"]["research_context"]
    second_context = prompts[1]["input"]["research_context"]
    third_context = prompts[2]["input"]["research_context"]
    assert first_context.count("Continue ordinary research only") == 1
    assert second_context.count("Continue ordinary research only") == 1
    assert source_id in second_context
    assert normalized_meaning in second_context
    assert "create the corresponding typed binding" in second_context
    assert "do not repeat a probe" in second_context
    assert "title" not in second_context.casefold()
    assert "Continue ordinary research only" not in third_context


def test_exact_predicate_stop_review_hint_allows_evidence_backed_stored_spelling() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FILTER,
            "required": True,
            "exact_physical_predicate": True,
            "operator": PredicateOperator.EQ,
            "literal_or_reference": "Portuguese (Brasil)",
            "status": SemanticItemStatus.UNRESOLVED,
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(update={"semantic_items": (item,)}),
            "unresolved_items": (item.source_id,),
        }
    )

    hint = _research_loop_module._unresolved_exact_physical_predicate_stop_review_hint(
        state
    )

    assert hint is not None
    assert "Preserve QuerySpec meaning" in hint
    assert "stored spelling for an empty trusted categorical string search" in hint
    assert "same physical column and operator" in hint
    assert "exact search_value certificate" in hint
    assert "profile permits" in hint


def _r2101_exact_filter_state(
    searches: tuple[tuple[ColumnRef, str | int], ...],
) -> tuple[ResearchState, str]:
    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace)
    source_id = "semantic:record-label"
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "source_id": source_id,
            "kind": SemanticItemKind.FILTER,
            "source_text": "record label",
            "normalized_meaning": "record_label = 'Needle'",
            "required": True,
            "exact_physical_predicate": True,
            "operator": PredicateOperator.EQ,
            "literal_or_reference": "Needle",
            "status": SemanticItemStatus.UNRESOLVED,
            "binding_ids": (),
        }
    )
    actions = tuple(
        ResearchAction(
            action_id=f"search:{index}",
            kind=ResearchActionKind.SEARCH_VALUE,
            hypothesis_id=None,
            target=column,
            parameters=(("top_k", index + 1), ("value", value)),
            action_digest=canonical_action_digest(
                kind=ResearchActionKind.SEARCH_VALUE,
                hypothesis_id=None,
                target=column,
                parameters=(("top_k", index + 1), ("value", value)),
                expected_revision=index,
            ),
            expected_revision=index,
        )
        for index, (column, value) in enumerate(searches)
    )
    return (
        ResearchState.model_validate(
            {
                **base.model_dump(mode="python", by_alias=True, round_trip=True),
                "revision": len(actions),
                "query_spec": base.query_spec.model_copy(
                    update={
                        "revision": len(actions),
                        "semantic_items": (item,),
                    }
                ),
                "unresolved_items": (source_id,),
                "action_history": actions,
            }
        ),
        source_id,
    )


def test_exact_predicate_continuation_advises_against_case_only_repeated_search(
    tmp_path,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    column = ColumnRef(
        table=TableRef(namespace="main", schema="public", table="catalog"),
        column="label",
    )
    state, source_id = _r2101_exact_filter_state(
        ((column, "Needle"), (column, "needle"))
    )
    prompts: list[dict[str, object]] = []

    async def model(prompt: str) -> str:
        prompts.append(json.loads(prompt))
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": (),
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_table",
                        "arguments": {"table": "public.orders"},
                    },
                },
            }
        )

    state_store = AdaptiveResearchStateStore(tmp_path / "state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "r2101-owner",
        model_wait=None,
    )
    coordinator._exact_physical_predicate_continuation_source_ids = (source_id,)
    try:
        decision, reason, _freshness = asyncio.run(coordinator._model_decision(state))
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    assert prompts
    context = prompts[0]["input"]["research_context"]
    assert source_id in context
    assert "record_label = 'Needle'" in context
    assert "case-only value" in context
    assert "different physical column or hypothesis" in context


@pytest.mark.parametrize(
    "searches,resolved",
    (
        ((("label", "Needle"),), False),
        ((("label", "Needle"), ("title", "needle")), False),
        ((("label", "Needle"), ("label", "Other")), False),
        ((("label", 1), ("label", 1)), False),
        ((("label", "Needle"), ("label", "needle")), True),
    ),
)
def test_exact_predicate_repeated_search_advisory_requires_same_unresolved_hypothesis(
    searches: tuple[tuple[str, str | int], ...],
    resolved: bool,
) -> None:
    table = TableRef(namespace="main", schema="public", table="catalog")
    state, source_id = _r2101_exact_filter_state(
        tuple(
            (ColumnRef(table=table, column=column), value)
            for column, value in searches
        )
    )
    if resolved:
        resolved_item = state.query_spec.semantic_items[0].model_copy(
            update={"status": SemanticItemStatus.RESOLVED}
        )
        state = state.model_copy(
            update={
                "query_spec": state.query_spec.model_copy(
                    update={"semantic_items": (resolved_item,)}
                ),
                "unresolved_items": (),
            }
        )

    assert _research_loop_module._repeated_exact_physical_predicate_search_value_hint(
        state, (source_id,)
    ) is None


@pytest.mark.parametrize(
    "unknown_physical_reference",
    ("orders.unknown_code", "public.orders.unknown_code"),
)
def test_stop_review_continue_does_not_forward_unknown_loaded_physical_column(
    tmp_path,
    unknown_physical_reference: str,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(8)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    calls: list[str] = []
    invalid_stop = (
        '{"decision_version":1,"proposals":[],"next":'
        '{"next_kind":"stop","reason":"ambiguous",'
        '"source_ids":["source-1"],"citation_evidence_ids":["citation-1"],'
        '"ambiguity":{"interpretations":["First reading.","Second reading."],'
        '"citation_evidence_ids":["citation-1"],'
        '"missing_distinguishing_fact":"The definition is absent."}}}'
    )

    async def model(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 3:
            assert '"review_kind":"research_stop_review"' in prompt
            return (
                '{"decision":"continue","hint":'
                f'"Inspect {unknown_physical_reference} before continuing."}}'
            )
        if len(calls) == 4:
            assert unknown_physical_reference not in prompt
            assert "the verified physical column for the resolved table" in prompt
        return invalid_stop

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
            policy=policy,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert len(calls) == 4
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_stop_review_hint_keeps_valid_or_unresolved_physical_references() -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)

    validated = _research_loop_module._validated_stop_review_hint(
        "Inspect orders.status and inventory.unknown_code.",
        state,
        loaded_schema=loaded_schema,
    )
    without_schema = _research_loop_module._validated_stop_review_hint(
        "Inspect public.orders.unknown_code.",
        state,
    )

    assert "orders.status" in validated
    assert "inventory.unknown_code" in validated
    assert "public.orders.unknown_code" in without_schema


def test_stop_review_hint_keeps_durable_ids_with_qualified_suffix() -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    table = TableRef(namespace="main", schema="public", table="orders")
    column = ColumnRef(table=table, column="status")
    binding = PhysicalColumnBinding(
        binding_id="binding:orders.unknown_code",
        source_id="semantic:orders.unknown_code",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(state.evidence[0].evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "source_id": binding.source_id,
            "binding_ids": (binding.binding_id,),
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "bindings": (binding,),
            "unresolved_items": (item.source_id,),
        }
    )
    hint = (
        "Assess binding:orders.unknown_code for semantic:orders.unknown_code."
    )

    validated = _research_loop_module._validated_stop_review_hint(
        hint,
        state,
        loaded_schema=loaded_schema,
    )

    assert validated.startswith(hint)


def test_stop_review_hint_does_not_forward_unknown_binding_id() -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    evidence_id = state.evidence[0].evidence_id
    table = TableRef(namespace="main", schema="public", table="orders")
    column = ColumnRef(table=table, column="status")
    binding = PhysicalColumnBinding(
        binding_id="binding:durable.alpha:1-",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    state = state.model_copy(update={"bindings": (binding,)})

    hint = _research_loop_module._validated_stop_review_hint(
        "Assess binding:mistyped.alpha:1- then assess binding:durable.alpha:1-.", state
    )

    assert "mistyped.alpha:1-" not in hint
    assert "the exact durable binding_id for the affected source_id" in hint
    assert "binding:durable.alpha:1-." in hint
    assert "binding_assessment" not in hint


def test_stop_review_hint_names_existing_physical_candidates_and_evidence() -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    evidence_id = state.evidence[0].evidence_id
    table = TableRef(namespace="main", schema="public", table="orders")
    column = ColumnRef(table=table, column="status")
    binding = PhysicalColumnBinding(
        binding_id="binding:durable-status",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    second_column = ColumnRef(table=table, column="priority")
    second_binding = PhysicalColumnBinding(
        binding_id="binding:durable-priority",
        source_id="source-2",
        tables=(table,),
        columns=(second_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=second_column,
    )
    semantic_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "binding_ids": (binding.binding_id,),
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
        }
    )
    second_semantic_item = semantic_item.model_copy(
        update={
            "source_id": second_binding.source_id,
            "binding_ids": (second_binding.binding_id,),
        }
    )
    state = state.model_copy(
        update={
            "bindings": (binding, second_binding),
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (semantic_item, second_semantic_item)}
            ),
        }
    )

    hint = _research_loop_module._validated_stop_review_hint(
        "Re-submit the binding proposal.", state
    )

    assert "binding_assessment" in hint
    assert binding.binding_id in hint
    assert second_binding.binding_id in hint
    assert evidence_id in hint
    assert "do not create a replacement binding" in hint


def test_stop_review_hint_preserves_uncertified_categorical_in_recovery() -> None:
    """An uncertified categorical recovery must retain its reviewer guidance."""

    _loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    table = TableRef(namespace="main", schema="public", table="catalog")
    column = ColumnRef(table=table, column="shade")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.IN,
        right=("mist", "sun"),
    )

    def categorical_candidate(evidence_ids: tuple[str, ...]) -> DiscriminatorValueBinding:
        return DiscriminatorValueBinding(
            binding_id="binding:catalog-shade",
            source_id="source-1",
            tables=(table,),
            columns=(column,),
            predicates=(predicate,),
            join_path=(),
            evidence_ids=evidence_ids,
            confidence=0.0,
            status=BindingStatus.CANDIDATE,
            validator_rule=None,
            discriminator_column=column,
            discriminator_predicate=predicate,
        )

    def state_for(bindings, evidence):
        item = base.query_spec.semantic_items[0].model_copy(
            update={
                "required": True,
                "status": SemanticItemStatus.PARTIALLY_RESOLVED,
                "binding_ids": tuple(binding.binding_id for binding in bindings),
            }
        )
        return base.model_copy(
            update={
                "evidence": evidence,
                "bindings": bindings,
                "query_spec": base.query_spec.model_copy(
                    update={"semantic_items": (item,)}
                ),
            }
        )

    uncertified = categorical_candidate((base.evidence[0].evidence_id,))
    ordinary = PhysicalColumnBinding(
        binding_id="binding:catalog-shade-column",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(base.evidence[0].evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    recovery_hint = "Continue the existing categorical recovery."
    assert _research_loop_module._validated_stop_review_hint(
        recovery_hint,
        state_for((uncertified,), base.evidence),
    ) == recovery_hint
    assert _research_loop_module._validated_stop_review_hint(
        recovery_hint,
        state_for((uncertified, ordinary), base.evidence),
    ) == recovery_hint
    legacy_evidence = base.evidence[0].model_copy(
        update={"observation": "legacy categorical observation"}
    )
    assert _research_loop_module._validated_stop_review_hint(
        recovery_hint,
        state_for((uncertified,), (legacy_evidence,)),
    ) == recovery_hint
    malformed_evidence = base.evidence[0].model_copy(update={"observation": "{"})
    assert _research_loop_module._validated_stop_review_hint(
        recovery_hint,
        state_for((uncertified,), (malformed_evidence,)),
    ) == recovery_hint

    exact_evidence = []
    for index, value in enumerate(predicate.right):
        action = ResearchAction(
            action_id=f"shade-search-{index}",
            kind=ResearchActionKind.SEARCH_VALUE,
            hypothesis_id=None,
            target=column,
            parameters=(("value", value), ("top_k", 1)),
            action_digest=canonical_action_digest(
                kind=ResearchActionKind.SEARCH_VALUE,
                hypothesis_id=None,
                target=column,
                parameters=(("value", value), ("top_k", 1)),
                expected_revision=base.revision,
            ),
            expected_revision=base.revision,
        )
        payload = {
            "columns": [column.column],
            "requested_value": value,
            "rows": [[value]],
        }
        result = build_probe_result(
            run_id=base.run_id,
            run_incarnation=base.run_incarnation,
            revision=base.revision,
            schema_namespace_version=base.schema_namespace_version,
            invocation_id=f"shade-evidence-{index}",
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=column,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="neutral exact categorical observation",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=1,
                bytes=len(canonical_json_bytes(payload)),
            ),
            row_count=1,
            payload=payload,
        )
        evidence = probe_result_to_evidence(result, action)
        assert evidence is not None
        exact_evidence.append(evidence)

    certified = categorical_candidate(
        tuple(evidence.evidence_id for evidence in exact_evidence)
    )
    assert "not new_binding" in _research_loop_module._validated_stop_review_hint(
        recovery_hint,
        state_for((certified,), tuple(exact_evidence)),
    )

    assert "not new_binding" in _research_loop_module._validated_stop_review_hint(
        recovery_hint,
        state_for((ordinary,), base.evidence),
    )


def test_stop_review_hint_names_existing_formula_candidates_and_evidence() -> None:
    """Required formula candidates need explicit assessment before commit."""

    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    evidence_id = state.evidence[0].evidence_id
    table = TableRef(namespace="main", schema="public", table="orders")
    amount = ColumnRef(table=table, column="amount")
    category = ColumnRef(table=table, column="category")
    derived = DerivedExpressionBinding(
        binding_id="binding:formula-derived",
        source_id="source-1",
        tables=(table,),
        columns=(amount,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        expression=ExpressionRef(
            expression_id="expression:formula-derived",
            expression="SUM(amount)",
        ),
        document=DocumentRef(document_id="formula-rule", namespace="main"),
        rule_excerpt="SUM(amount)",
        input_columns=(amount,),
    )
    predicate = {
        "left": category,
        "operator": PredicateOperator.EQ,
        "right": "approved",
    }
    discriminator = DiscriminatorValueBinding(
        binding_id="binding:formula-category",
        source_id="source-1",
        tables=(table,),
        columns=(category,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=(evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=category,
        discriminator_predicate=predicate,
    )
    formula_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (derived.binding_id, discriminator.binding_id),
        }
    )
    state = state.model_copy(
        update={
            "bindings": (derived, discriminator),
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (formula_item,)}
            ),
        }
    )

    hint = _research_loop_module._validated_stop_review_hint(
        "Assess the existing formula candidates.", state
    )

    assert "nonempty binding_assessment proposals" in hint
    assert "semantic_commit with those assessments" in hint
    assert "never use an empty semantic_commit" in hint
    candidate_payload = json.loads(
        hint.split("listed durable evidence_ids: ", 1)[1].split(". Then ", 1)[0]
    )
    assert candidate_payload == [
        {"binding_id": derived.binding_id, "evidence_ids": [evidence_id]},
        {"binding_id": discriminator.binding_id, "evidence_ids": [evidence_id]},
    ]


def test_stop_confirmed_reuses_prior_hint_for_unresolved_durable_sources(tmp_path) -> None:
    loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    evidence_id = base.evidence[0].evidence_id
    orders = TableRef(namespace="main", schema="public", table="orders")
    details = TableRef(namespace="main", schema="public", table="details")
    order_id = ColumnRef(table=orders, column="id")
    detail_order_id = ColumnRef(table=details, column="order_id")
    order_binding = PhysicalColumnBinding(
        binding_id="binding:orders",
        source_id="semantic:orders",
        tables=(orders,),
        columns=(order_id,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=order_id,
    )
    detail_binding = PhysicalColumnBinding(
        binding_id="binding:details",
        source_id="semantic:details",
        tables=(details,),
        columns=(detail_order_id,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=detail_order_id,
    )
    order_item = base.query_spec.semantic_items[0].model_copy(
        update={
            "source_id": order_binding.source_id,
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (order_binding.binding_id,),
        }
    )
    detail_item = order_item.model_copy(
        update={
            "source_id": detail_binding.source_id,
            "binding_ids": (detail_binding.binding_id,),
        }
    )
    state = base.model_copy(
        update={
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (order_item, detail_item)}
            ),
            "bindings": (order_binding, detail_binding),
            "join_candidates": (
                JoinCandidate(
                    join_id="join:orders-details",
                    left=order_id,
                    right=detail_order_id,
                    join_type=JoinType.INNER,
                    path=(
                        JoinEdge(
                            left=order_id,
                            right=detail_order_id,
                            join_type=JoinType.INNER,
                        ),
                    ),
                    status=JoinCandidateStatus.VALIDATED,
                    evidence_ids=(evidence_id,),
                ),
            ),
            "unresolved_items": tuple(
                sorted((order_item.source_id, detail_item.source_id))
            ),
        }
    )

    async def no_decision_model(_prompt: str) -> str:
        raise AssertionError("_review_stop must not call the decision model")

    async def confirmed_review_model(_prompt: str) -> str:
        return '{"decision":"stop_confirmed","hint":null}'

    def review_hint(current: ResearchState, hint: str, name: str) -> str | None:
        state_store = AdaptiveResearchStateStore(tmp_path / f"{name}-state.sqlite")
        checkpoint_store = AdaptiveStateStore(tmp_path / f"{name}-checkpoint.sqlite")
        ledger = AdaptiveBudgetLedger(tmp_path / f"{name}-budget.sqlite")
        coordinator = _research_loop_module._ResearchLoopCoordinator(
            initial_state=current,
            task="research schema",
            research_context=lambda current, _feedbacks: canonical_digest(current),
            model=no_decision_model,
            model_identity="test/model",
            adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(current),
            registry=_make_registry(namespace),
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=_policy(),
            deadline=None,
            is_cancelled=lambda: False,
            model_claim_now_ns=lambda: 0,
            model_owner_token_factory=lambda: f"{name}-owner",
            model_wait=None,
            stop_review_model=confirmed_review_model,
        )
        coordinator._last_stop_review_hint = hint
        try:
            returned_hint, _attempt = asyncio.run(
                coordinator._review_stop(
                    current, ResearchStopReason.STAGNATED, "{}", 0
                )
            )
            return returned_hint
        finally:
            state_store.close()
            checkpoint_store.close()
            ledger.close()

    prior_hint = "Assess semantic:orders and semantic:details using durable facts."
    continued_hint = review_hint(state, prior_hint, "unresolved")
    assert continued_hint is not None
    assert prior_hint in continued_hint
    assert order_binding.binding_id in continued_hint
    assert detail_binding.binding_id in continued_hint
    assert evidence_id in continued_hint

    resolved_state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": tuple(
                        item.model_copy(update={"status": SemanticItemStatus.RESOLVED})
                        for item in state.query_spec.semantic_items
                    )
                }
            ),
            "bindings": tuple(
                binding.model_copy(
                    update={
                        "status": BindingStatus.SUPPORTED,
                        "validator_rule": "schema evidence",
                    }
                )
                for binding in state.bindings
            ),
            "unresolved_items": (),
        }
    )
    assert review_hint(resolved_state, prior_hint, "resolved") is None
    assert review_hint(state, "Inspect the confirmed relationship.", "no-source") is None


def test_budget_stop_confirmed_preserves_turn_for_partial_candidate(tmp_path) -> None:
    loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    evidence_id = base.evidence[0].evidence_id
    table = TableRef(namespace="main", schema="public", table="orders")
    column = ColumnRef(table=table, column="status")
    binding = PhysicalColumnBinding(
        binding_id="binding:partial-status",
        source_id="source-1",
        tables=(table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (binding.binding_id,),
        }
    )
    state = base.model_copy(
        update={
            "bindings": (binding,),
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "unresolved_items": (item.source_id,),
        }
    )

    async def no_decision_model(_prompt: str) -> str:
        raise AssertionError("_review_stop must not call the decision model")

    async def confirmed_review_model(_prompt: str) -> str:
        return '{"decision":"stop_confirmed","hint":null}'

    state_store = AdaptiveResearchStateStore(tmp_path / "partial-state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "partial-checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "partial-budget.sqlite")
    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=no_decision_model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "partial-owner",
        model_wait=None,
        stop_review_model=confirmed_review_model,
    )
    try:
        hint, _attempt = asyncio.run(
            coordinator._review_stop(
                state,
                ResearchStopReason.BUDGET_EXHAUSTED,
                "{}",
                0,
            )
        )
        assert hint is not None
        assert binding.binding_id in hint
        assert evidence_id in hint
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_stop_review_drops_closed_auto_binding_assessment_hint(tmp_path) -> None:
    """A completed assessment/commit must not be reissued on the next review."""

    loaded_schema, namespace = _fixture_schema()
    base = _policy_state(namespace, with_evidence=True)
    evidence_id = base.evidence[0].evidence_id
    table = TableRef(namespace="main", schema="public", table="orders")
    columns = ("id", "status", "priority", "amount")
    bindings = tuple(
        PhysicalColumnBinding(
            binding_id=f"binding:assessment-{index}",
            source_id=f"semantic:assessment-{index}",
            tables=(table,),
            columns=(ColumnRef(table=table, column=column),),
            predicates=(),
            join_path=(),
            evidence_ids=(evidence_id,),
            confidence=0.0,
            status=BindingStatus.CANDIDATE,
            validator_rule=None,
            physical_column=ColumnRef(table=table, column=column),
        )
        for index, column in enumerate(columns)
    )
    semantic_items = tuple(
        base.query_spec.semantic_items[0].model_copy(
            update={
                "source_id": binding.source_id,
                "status": SemanticItemStatus.PARTIALLY_RESOLVED,
                "binding_ids": (binding.binding_id,),
            }
        )
        for binding in bindings
    )
    candidate_state = base.model_copy(
        update={
            "bindings": bindings,
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": semantic_items}
            ),
            "unresolved_items": tuple(item.source_id for item in semantic_items),
        }
    )
    hint = _research_loop_module._validated_stop_review_hint(
        "Assess " + " and ".join(item.source_id for item in semantic_items) + ".",
        candidate_state,
    )
    assert "nonempty binding_assessment proposals" in hint

    resolved_state = candidate_state.model_copy(
        update={
            "bindings": tuple(
                binding.model_copy(
                    update={
                        "status": BindingStatus.SUPPORTED,
                        "validator_rule": "schema evidence",
                    }
                )
                for binding in bindings
            ),
            "query_spec": candidate_state.query_spec.model_copy(
                update={
                    "semantic_items": tuple(
                        item.model_copy(update={"status": SemanticItemStatus.RESOLVED})
                        for item in semantic_items
                    )
                }
            ),
            "unresolved_items": (),
        }
    )

    hint_prefix, serialized_candidates = hint.split("evidence_ids: ", 1)
    candidate_payload, hint_suffix = serialized_candidates.split(". Then ", 1)

    def hint_with_first_evidence_ids(evidence_ids: list[str]) -> str:
        candidates = json.loads(candidate_payload)
        candidates[0]["evidence_ids"] = evidence_ids
        return (
            f"{hint_prefix}evidence_ids: "
            f"{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}. Then "
            f"{hint_suffix}"
        )

    def review_context(
        current: ResearchState,
        name: str,
        previous_hint: str = hint,
    ) -> tuple[str | None, dict[str, object]]:
        state_store = AdaptiveResearchStateStore(tmp_path / f"{name}-state.sqlite")
        checkpoint_store = AdaptiveStateStore(tmp_path / f"{name}-checkpoint.sqlite")
        ledger = AdaptiveBudgetLedger(tmp_path / f"{name}-budget.sqlite")
        contexts: list[dict[str, object]] = []

        async def no_decision_model(_prompt: str) -> str:
            raise AssertionError("_review_stop must not call the decision model")

        async def confirmed_review_model(prompt: str) -> str:
            contexts.append(json.loads(json.loads(prompt)["input"]["research_context"]))
            return '{"decision":"stop_confirmed","hint":null}'

        coordinator = _research_loop_module._ResearchLoopCoordinator(
            initial_state=current,
            task="research schema",
            research_context=lambda current, _feedbacks: canonical_digest(current),
            model=no_decision_model,
            model_identity="test/model",
            adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(current),
            registry=_make_registry(namespace),
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=_policy(),
            deadline=None,
            is_cancelled=lambda: False,
            model_claim_now_ns=lambda: 0,
            model_owner_token_factory=lambda: f"{name}-owner",
            model_wait=None,
            stop_review_model=confirmed_review_model,
        )
        coordinator._last_stop_review_hint = previous_hint
        try:
            returned_hint, _attempt = asyncio.run(
                coordinator._review_stop(
                    current,
                    ResearchStopReason.STAGNATED,
                    '{"evidence":[]}',
                    0,
                )
            )
            assert len(contexts) == 1
            return returned_hint, contexts[0]
        finally:
            state_store.close()
            checkpoint_store.close()
            ledger.close()

    closed_hint, closed_context = review_context(resolved_state, "closed")
    assert closed_hint is None
    assert "previous_stop_review_hint" not in closed_context

    candidate_hint, candidate_context = review_context(candidate_state, "candidate")
    assert candidate_hint is not None
    assert candidate_context["previous_stop_review_hint"] == hint

    mixed_hint = hint_with_first_evidence_ids([evidence_id, "evidence:unknown"])
    _mixed_result, mixed_context = review_context(
        resolved_state,
        "mixed-evidence",
        mixed_hint,
    )
    assert mixed_context["previous_stop_review_hint"] == mixed_hint

    duplicate_hint = hint_with_first_evidence_ids([evidence_id, evidence_id])
    _duplicate_result, duplicate_context = review_context(
        resolved_state,
        "duplicate-evidence",
        duplicate_hint,
    )
    assert duplicate_context["previous_stop_review_hint"] == duplicate_hint

    additional_evidence_id = "evidence:additional"
    partial_state = resolved_state.model_copy(
        update={
            "evidence": (
                *resolved_state.evidence,
                resolved_state.evidence[0].model_copy(
                    update={"evidence_id": additional_evidence_id}
                ),
            ),
            "bindings": (
                resolved_state.bindings[0].model_copy(
                    update={"evidence_ids": (evidence_id, additional_evidence_id)}
                ),
                *resolved_state.bindings[1:],
            ),
        }
    )
    _partial_result, partial_context = review_context(partial_state, "partial-evidence")
    assert partial_context["previous_stop_review_hint"] == hint


def test_stop_review_current_generation_authority_omits_stale_prior_hint(
    tmp_path,
) -> None:
    """Current typed authority, not an unrelated old hint, directs review."""

    loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    prior_hint = "Finish the already resolved synthetic metric."

    def review_context(name: str, context: str) -> dict[str, object]:
        state_store = AdaptiveResearchStateStore(tmp_path / f"{name}-state.sqlite")
        checkpoint_store = AdaptiveStateStore(tmp_path / f"{name}-checkpoint.sqlite")
        ledger = AdaptiveBudgetLedger(tmp_path / f"{name}-budget.sqlite")
        contexts: list[dict[str, object]] = []

        async def no_decision_model(_prompt: str) -> str:
            raise AssertionError("_review_stop must not call the decision model")

        async def confirmed_review_model(prompt: str) -> str:
            contexts.append(json.loads(json.loads(prompt)["input"]["research_context"]))
            return '{"decision":"stop_confirmed","hint":null}'

        coordinator = _research_loop_module._ResearchLoopCoordinator(
            initial_state=state,
            task="research schema",
            research_context=lambda current, _feedbacks: canonical_digest(current),
            model=no_decision_model,
            model_identity="test/model",
            adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=_policy(),
            deadline=None,
            is_cancelled=lambda: False,
            model_claim_now_ns=lambda: 0,
            model_owner_token_factory=lambda: f"{name}-owner",
            model_wait=None,
            stop_review_model=confirmed_review_model,
        )
        coordinator._last_stop_review_hint = prior_hint
        try:
            asyncio.run(
                coordinator._review_stop(
                    state, ResearchStopReason.STAGNATED, context, 0
                )
            )
            assert len(contexts) == 1
            return contexts[0]
        finally:
            state_store.close()
            checkpoint_store.close()
            ledger.close()

    authority = {
        "reason_code": "QUERY_REQUIREMENT_INCOMPLETE",
        "affected_source_ids": ["source-1"],
    }
    authority_context = review_context(
        "current-authority",
        json.dumps({"invalid_stop_generation_authority": authority}),
    )
    assert authority_context["invalid_stop_generation_authority"] == authority
    assert "previous_stop_review_hint" not in authority_context

    open_hint_context = review_context("no-authority", "{}")
    assert open_hint_context["previous_stop_review_hint"] == prior_hint


def test_stop_review_assesses_pending_exact_formula_candidate_not_selected_by_item(
    tmp_path,
) -> None:
    """A pending exact document formula still needs its durable candidate assessed."""

    loaded_schema, namespace = _fixture_schema()
    base = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    physical_binding = base.bindings[0]
    formula = "SUM(status)"
    document = DocumentRef(document_id="formula-rule", namespace="main")
    formula_item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": formula,
            "normalized_meaning": formula,
            "required": True,
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (physical_binding.binding_id,),
        }
    )

    def derived_candidate(
        binding_id: str,
        *,
        source_id: str = formula_item.source_id,
        candidate_document: DocumentRef = document,
        expression: str = formula,
    ) -> DerivedExpressionBinding:
        return DerivedExpressionBinding(
            binding_id=binding_id,
            source_id=source_id,
            tables=physical_binding.tables,
            columns=physical_binding.columns,
            predicates=(),
            join_path=(),
            evidence_ids=physical_binding.evidence_ids,
            confidence=0.0,
            status=BindingStatus.CANDIDATE,
            validator_rule=None,
            expression=ExpressionRef(
                expression_id=f"{binding_id}-expression",
                expression=expression,
            ),
            document=candidate_document,
            rule_excerpt=expression,
            input_columns=physical_binding.columns,
        )

    matching = derived_candidate("binding:formula-candidate")
    wrong_expression = derived_candidate(
        "binding:wrong-expression",
        expression="SUM(other_status)",
    )
    wrong_document = derived_candidate(
        "binding:wrong-document",
        candidate_document=DocumentRef(document_id="other-rule", namespace="main"),
    )
    wrong_source = derived_candidate(
        "binding:wrong-source",
        source_id="other-formula",
    )
    foreign_item = formula_item.model_copy(
        update={
            "source_id": wrong_source.source_id,
            "required": False,
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (wrong_source.binding_id,),
        }
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", by_alias=True, round_trip=True),
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (formula_item, foreign_item)}
            ),
            "bindings": (
                physical_binding,
                matching,
                wrong_expression,
                wrong_document,
                wrong_source,
            ),
        }
    )
    exact_formula_documents = ((formula_item.source_id, document),)
    assert _research_loop_module._runtime_exact_formula_continuation_source_ids(
        state,
        exact_formula_documents,
    ) == (formula_item.source_id,)

    def review_hint(
        name: str,
        response: str,
        prior_hint: str | None = None,
    ) -> str | None:
        state_store = AdaptiveResearchStateStore(tmp_path / f"{name}-state.sqlite")
        checkpoint_store = AdaptiveStateStore(tmp_path / f"{name}-checkpoint.sqlite")
        ledger = AdaptiveBudgetLedger(tmp_path / f"{name}-budget.sqlite")

        async def no_decision_model(_prompt: str) -> str:
            raise AssertionError("_review_stop must not call the decision model")

        async def review_model(_prompt: str) -> str:
            return response

        coordinator = _research_loop_module._ResearchLoopCoordinator(
            initial_state=state,
            task="research schema",
            research_context=lambda current, _feedbacks: canonical_digest(current),
            model=no_decision_model,
            model_identity="test/model",
            adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=_policy(),
            deadline=None,
            is_cancelled=lambda: False,
            model_claim_now_ns=lambda: 0,
            model_owner_token_factory=lambda: f"{name}-owner",
            model_wait=None,
            exact_formula_documents=exact_formula_documents,
            stop_review_model=review_model,
        )
        coordinator._last_stop_review_hint = prior_hint
        try:
            hint, _attempt = asyncio.run(
                coordinator._review_stop(
                    state,
                    ResearchStopReason.STAGNATED,
                    "{}",
                    0,
                )
            )
            return hint
        finally:
            state_store.close()
            checkpoint_store.close()
            ledger.close()

    hint = review_hint(
        "continue",
        '{"decision":"continue","hint":"Assess the exact formula candidate."}',
    )
    assert hint is not None
    assert "nonempty binding_assessment proposals" in hint
    assert matching.binding_id in hint
    assert physical_binding.evidence_ids[0] in hint
    assert wrong_expression.binding_id not in hint
    assert wrong_document.binding_id not in hint
    assert wrong_source.binding_id not in hint

    confirmed_without_prior_hint = review_hint(
        "confirmed-without-prior-hint",
        '{"decision":"stop_confirmed","hint":null}',
    )
    assert confirmed_without_prior_hint is not None
    assert "nonempty binding_assessment proposals" in confirmed_without_prior_hint
    assert matching.binding_id in confirmed_without_prior_hint

    assert review_hint(
        "confirmed",
        '{"decision":"stop_confirmed","hint":null}',
        hint,
    ) == hint


@pytest.mark.parametrize(
    ("rejected_preflight", "invalid_stop", "expected_hint"),
    ((True, False, "formula"), (False, False, "model"), (False, True, "formula")),
)
def test_stop_review_overrides_stale_hint_for_pending_external_exact_formula(
    tmp_path, rejected_preflight: bool, invalid_stop: bool, expected_hint: str
) -> None:
    loaded_schema, namespace = _fixture_schema()
    base = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    physical = base.bindings[0]
    formula = "COUNT(event_entry_id WHERE category_code = 'x')"
    formula_item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": formula,
            "required": True,
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (physical.binding_id,),
        }
    )
    state = base.model_copy(
        update={
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (formula_item,)}
            )
        }
    )
    document = DocumentRef(document_id="event-rule", namespace="main")
    state_store = AdaptiveResearchStateStore(tmp_path / "state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    registry = _make_registry(loaded_schema.namespace)
    registry.context.schema_runtime.documents = (
        SchemaEvidenceDocument(
            document_id=document.document_id,
            namespace=document.namespace,
            schema_namespace_version=state.schema_namespace_version,
            source_version="v1",
            title="Event formula",
            content="COUNT(logical_event_time)",
            target=None,
        ),
    )

    async def no_decision_model(_prompt: str) -> str:
        raise AssertionError("_review_stop must not call the decision model")

    async def stale_review_model(_prompt: str) -> str:
        return '{"decision":"continue","hint":"Use events.id and base-table state."}'

    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="Count verified event entries.",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=no_decision_model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "exact-formula-owner",
        model_wait=None,
        semantic_repair_continuation=not rejected_preflight,
        exact_formula_documents=((formula_item.source_id, document),),
        stop_review_model=stale_review_model,
    )
    if rejected_preflight:
        coordinator._pending_rejected_preflight_assessments = ({"reason": "corrective"},)
    try:
        context = (
            json.dumps(
                {
                    "invalid_stop_generation_authority": {
                        "reason_code": "QUERY_REQUIREMENT_INCOMPLETE",
                        "affected_source_ids": [formula_item.source_id],
                    }
                }
            )
            if invalid_stop
            else "{}"
        )
        hint, _attempt = asyncio.run(
            coordinator._review_stop(state, ResearchStopReason.STAGNATED, context, 0)
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    assert hint is not None
    if expected_hint == "formula":
        assert formula in hint
        assert "events.id" not in hint
        assert "derived_expression" in hint
        assert "semantic_commit" in hint
        assert "SQL" in hint
    else:
        assert hint == "Use events.id and base-table state."


@pytest.mark.parametrize("still_affected", (True, False))
def test_pending_rejected_preflight_lifecycle_tracks_exact_formula_authority(
    monkeypatch, still_affected: bool
) -> None:
    _loaded_schema, namespace = _fixture_schema()
    base = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    formula_item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": "COUNT(event_entry_id WHERE category_code = 'x')",
            "required": True,
            "status": SemanticItemStatus.RESOLVED,
        }
    )
    state = base.model_copy(
        update={"query_spec": base.query_spec.model_copy(update={"semantic_items": (formula_item,)})}
    )
    monkeypatch.setattr(
        _research_loop_module,
        "evaluate_research_generation_authority",
        lambda *_args: SimpleNamespace(
            affected_source_ids=(formula_item.source_id,) if still_affected else ()
        ),
    )

    assert _research_loop_module._pending_exact_formula_preflight_is_affected(
        state,
        _fixture_freshness(state),
        ((formula_item.source_id, DocumentRef(document_id="event-rule", namespace="main")),),
    ) is still_affected


def test_pending_rejected_preflight_filter_keeps_only_affected_exact_formula_sources() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    source_id = state.query_spec.semantic_items[0].source_id
    kept = {"proposal": {"proposal_type": "new_binding", "proposal_key": "proposal:kept", "source_id": source_id, "candidate": {"kind": "physical_column", "physical_column": {"table": "public.orders", "column": "status"}}, "join_references": [], "citation_evidence_ids": [state.evidence[0].evidence_id]}}
    unknown = {"proposal": {"proposal_type": "binding_assessment", "subject": {"reference_kind": "proposed", "proposal_key": "proposal:missing"}, "certificate": "insufficient", "citation_evidence_ids": [state.evidence[0].evidence_id]}}
    assert _research_loop_module._filter_pending_rejected_preflight_assessments(
        (kept, unknown), state, {source_id}
    ) == (kept,)


def test_pending_rejected_preflight_filter_resolves_existing_binding_source() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    binding = state.bindings[0]
    item = {"proposal": {"proposal_type": "binding_assessment", "subject": {"reference_kind": "existing", "binding_id": binding.binding_id}, "certificate": "insufficient", "citation_evidence_ids": [state.evidence[0].evidence_id]}}
    assert _research_loop_module._filter_pending_rejected_preflight_assessments(
        (item,), state, {binding.source_id}
    ) == (item,)


@pytest.mark.parametrize(
    ("schema_names", "input_names", "mismatch"),
    (
        (("EventRef",), ("Id",), True),
        (("EventRef",), ("EventRef",), False),
        ((), ("Id",), False),
        (("EventRef", "eventref"), ("eventref",), False),
    ),
)
def test_exact_aggregate_operand_preflight_requires_same_named_input(
    schema_names, input_names, mismatch
) -> None:
    assert _research_loop_module._exact_aggregate_operand_input_mismatch(
        "COUNT(EventRef)", schema_names, input_names
    ) is mismatch


def test_exact_aggregate_operand_ignores_quoted_aggregate_text() -> None:
    assert not _research_loop_module._exact_aggregate_operand_input_mismatch(
        "COUNT(category = 'SUM(EventRef)')", ("EventRef",), ()
    )


def test_exact_aggregate_operand_skips_no_document_opaque_schema() -> None:
    assert not _research_loop_module._has_exact_aggregate_operand_mismatch(
        None, None, (), object()
    )


@pytest.mark.parametrize(
    ("formula", "schema_names", "input_names", "mismatch"),
    (
        ("COUNT(status WHERE kind = 'x')", ("status",), ("id",), True),
        ("SUM( status WHERE kind = 'x')", ("status",), ("id",), True),
        ("COUNT(unknown WHERE kind = 'x')", ("status",), ("id",), False),
        ("COUNT(*)", ("status",), ("id",), False),
        ("COUNT(status WHERE kind = 'x')", ("status",), ("status",), False),
    ),
)
def test_exact_aggregate_operand_preflight_accepts_compact_where_formula(
    formula, schema_names, input_names, mismatch
) -> None:
    assert _research_loop_module._exact_aggregate_operand_input_mismatch(
        formula, schema_names, input_names
    ) is mismatch


@pytest.mark.parametrize(
    ("formula", "input_names", "mismatch"),
    (
        (
            "DIVIDE(COUNT(record_id WHERE YEAR(recorded_at) = 2020 "
            "AND threshold_total <= 2), 12)",
            ("record_id", "proxy_timestamp", "threshold_total"),
            True,
        ),
        (
            "DIVIDE(COUNT(record_idwhereYEAR(recorded_at)=2020andthreshold_total<=2),12)",
            ("record_id", "proxy_timestamp", "threshold_total"),
            True,
        ),
        (
            "DIVIDE(COUNT(record_id WHERE YEAR(recorded_at) = 2020 "
            "AND threshold_total <= 2), 12)",
            ("record_id", "recorded_at", "threshold_total"),
            False,
        ),
    ),
)
def test_exact_aggregate_operand_preflight_checks_where_columns(
    formula, input_names, mismatch
) -> None:
    assert _research_loop_module._exact_aggregate_operand_input_mismatch(
        formula,
        ("record_id", "recorded_at", "proxy_timestamp", "threshold_total"),
        input_names,
    ) is mismatch


@pytest.mark.parametrize(
    ("formula", "missing_names"),
    (
        (
            "COUNT(record_id WHERE YEAR(recorded_at)=2011ANDscore>1000)",
            ("score",),
        ),
        (
            "COUNT(record_id WHERE (YEAR(recorded_at)=2011)ORscore>1000)",
            ("score",),
        ),
        ("COUNT(record_id WHERE 'closed'ANDscore>1000)", ("score",)),
        ("COUNT(record_id WHERE brandScore>1000)", ()),
        ("COUNT(record_id WHERE userORscore>1000)", ()),
        ("COUNT(record_id WHERE ANDscore>1000)", ()),
        ("COUNT(record_id WHERE records.ORscore>1000)", ()),
        ("COUNT(record_id WHERE canonical_label=ANDscore)", ()),
    ),
)
def test_exact_aggregate_operand_preflight_recognizes_compact_connectors(
    formula, missing_names
) -> None:
    assert _research_loop_module._missing_exact_aggregate_operand_names(
        formula,
        ("record_id", "recorded_at", "score"),
        ("record_id", "recorded_at"),
    ) == missing_names


def test_exact_aggregate_operand_preflight_does_not_treat_function_as_column() -> None:
    assert not _research_loop_module._exact_aggregate_operand_input_mismatch(
        "COUNT(record_id WHERE YEAR(recorded_at) = 2020)",
        ("record_id", "recorded_at", "year"),
        ("record_id", "recorded_at"),
    )


def _exact_aggregate_candidate_state(
    input_column: str,
    formula: str = "COUNT(status WHERE kind = 'x')",
    tables: dict[str, object] | None = None,
):
    loaded_schema, namespace = _fixture_schema(tables)
    base = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    physical = base.bindings[0]
    document = DocumentRef(document_id="aggregate-rule", namespace="main")
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": formula,
            "required": True,
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (physical.binding_id,),
        }
    )
    candidate = DerivedExpressionBinding(
        binding_id="binding:aggregate-candidate",
        source_id=item.source_id,
        tables=physical.tables,
        columns=(ColumnRef(table=physical.tables[0], column=input_column),),
        predicates=(),
        join_path=(),
        evidence_ids=physical.evidence_ids,
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        expression=ExpressionRef(
            expression_id="expression:aggregate-candidate", expression=formula
        ),
        document=document,
        rule_excerpt=formula,
        input_columns=(ColumnRef(table=physical.tables[0], column=input_column),),
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", by_alias=True, round_trip=True),
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "bindings": (physical, candidate),
        }
    )
    return loaded_schema, state, document, candidate


@pytest.mark.parametrize(("input_column", "rejects"), (("id", True), ("status", False)))
def test_exact_aggregate_operand_preflight_checks_existing_candidate_assessment(
    input_column, rejects
) -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        input_column
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": candidate.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": candidate.evidence_ids,
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    assert _research_loop_module._has_exact_aggregate_operand_mismatch(
        state, decision, ((candidate.source_id, document),), loaded_schema
    ) is rejects


def _differently_named_exact_aggregate_input():
    loaded_schema, namespace = _fixture_schema(
        {
            "public.orders": {
                "columns": {
                    "id": {"type": "INTEGER"},
                    "status": {"type": "TEXT"},
                    "logical_status": {"type": "TEXT"},
                }
            }
        }
    )
    base = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    physical = base.bindings[0].model_copy(
        update={
            "confidence": 0.0,
            "status": BindingStatus.CANDIDATE,
            "validator_rule": None,
        }
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": "COUNT(logical_status)",
            "required": True,
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (physical.binding_id,),
        }
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", by_alias=True, round_trip=True),
            "bindings": (physical,),
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "unresolved_items": (item.source_id,),
        }
    )
    document = DocumentRef(document_id="aggregate-rule", namespace="main")

    def decision(input_column: str) -> ResearchDecisionV1:
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (
                    {
                        "proposal_type": "new_binding",
                        "proposal_key": "proposal:exact-aggregate",
                        "source_id": item.source_id,
                        "candidate": {
                            "kind": "derived_expression",
                            "expression_claim": "COUNT(logical_status)",
                            "document_id": document.document_id,
                            "rule_excerpt": "COUNT(logical_status)",
                            "input_columns": (
                                {
                                    "table": "public.orders",
                                    "column": input_column,
                                },
                            ),
                        },
                        "join_references": (),
                        "citation_evidence_ids": (physical.evidence_ids[0],),
                    },
                ),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    return loaded_schema, state, document, decision


def test_exact_aggregate_preflight_accepts_confirmed_differently_named_input() -> None:
    loaded_schema, state, document, decision = _differently_named_exact_aggregate_input()

    assert _research_loop_module._has_exact_aggregate_operand_mismatch(
        state, decision("status"), ((state.query_spec.semantic_items[0].source_id, document),), loaded_schema
    ) is False


def test_exact_aggregate_preflight_accepts_existing_confirmed_renamed_input(
    tmp_path,
) -> None:
    loaded_schema, state, document, _decision = (
        _same_batch_differently_named_exact_aggregate_input()
    )
    source_id = state.query_spec.semantic_items[0].source_id
    table = TableRef(namespace="main", schema="public", table="events")
    input_column = ColumnRef(table=table, column="stored_timestamp")
    physical = PhysicalColumnBinding(
        binding_id="binding:existing-stored-time",
        source_id=source_id,
        tables=(table,),
        columns=(input_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(state.evidence[1].evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=input_column,
    )
    candidate = DerivedExpressionBinding(
        binding_id="binding:existing-exact-aggregate",
        source_id=source_id,
        tables=(table,),
        columns=(input_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(state.evidence[0].evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        expression=ExpressionRef(
            expression_id="expression:existing-exact-aggregate",
            expression="COUNT(logical_event_time)",
        ),
        document=document,
        rule_excerpt="COUNT(logical_event_time)",
        input_columns=(input_column,),
    )
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (candidate.binding_id, physical.binding_id),
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "bindings": (physical, candidate),
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": physical.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": physical.evidence_ids,
                },
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": candidate.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": candidate.evidence_ids,
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    feedback, reason, _action, _rejected = _same_batch_aggregate_preflight(
        tmp_path, loaded_schema, state, document, decision
    )

    assert feedback is None
    assert reason is None


def test_exact_aggregate_preflight_keeps_unconfirmed_or_missing_input_rejected() -> None:
    loaded_schema, state, document, decision = _differently_named_exact_aggregate_input()
    source_id = state.query_spec.semantic_items[0].source_id
    without_physical = ResearchState.model_validate(
        {
            **state.model_dump(mode="python", by_alias=True, round_trip=True),
            "bindings": (),
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        state.query_spec.semantic_items[0].model_copy(
                            update={
                                "binding_ids": (),
                                "status": SemanticItemStatus.UNRESOLVED,
                            }
                        ),
                    )
                }
            ),
        }
    )

    assert _research_loop_module._has_exact_aggregate_operand_mismatch(
        without_physical, decision("status"), ((source_id, document),), loaded_schema
    )
    different_source = ResearchState.model_validate(
        {
            **without_physical.model_dump(
                mode="python", by_alias=True, round_trip=True
            ),
            "bindings": (
                state.bindings[0].model_copy(update={"source_id": "source-2"}),
            ),
            "query_spec": without_physical.query_spec.model_copy(
                update={
                    "semantic_items": (
                        without_physical.query_spec.semantic_items[0],
                        without_physical.query_spec.semantic_items[0].model_copy(
                            update={
                                "source_id": "source-2",
                                "required": False,
                                "status": SemanticItemStatus.PARTIALLY_RESOLVED,
                                "binding_ids": (state.bindings[0].binding_id,),
                            }
                        ),
                    )
                }
            ),
        }
    )
    assert _research_loop_module._has_exact_aggregate_operand_mismatch(
        different_source, decision("status"), ((source_id, document),), loaded_schema
    )
    assert _research_loop_module._has_exact_aggregate_operand_mismatch(
        state, decision("id"), ((source_id, document),), loaded_schema
    )


def _same_batch_differently_named_exact_aggregate_input():
    loaded_schema, namespace = _fixture_schema(
        {
            "public.events": {
                "columns": {
                    "logical_event_time": {"type": "DATETIME"},
                    "stored_timestamp": {"type": "DATETIME"},
                    "logical_existing": {"type": "TEXT"},
                    "stored_existing": {"type": "TEXT"},
                }
            }
        }
    )
    base, document = _document_supported_state_after_probe(
        namespace,
        observed_at=_FIXTURE_NOW,
        valid_until=_FIXTURE_NOW + timedelta(days=1),
    )
    table = TableRef(namespace="main", schema="public", table="events")
    action, schema_evidence = _observed_table_evidence(
        base,
        table,
        invocation_id="events-schema",
        columns=[
            "logical_event_time",
            "stored_timestamp",
            "logical_existing",
            "stored_existing",
        ],
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": "COUNT(logical_event_time)",
            "required": True,
            "status": SemanticItemStatus.UNRESOLVED,
            "binding_ids": (),
        }
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", by_alias=True, round_trip=True),
            "revision": 2,
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "evidence": (*base.evidence, schema_evidence),
            "bindings": (),
            "unresolved_items": (item.source_id,),
            "action_history": (*base.action_history, action),
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:stored-time",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.events",
                            "column": "stored_timestamp",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (schema_evidence.evidence_id,),
                },
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:exact-formula",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "derived_expression",
                        "expression_claim": "COUNT(logical_event_time)",
                        "document_id": document.document_id,
                        "rule_excerpt": "COUNT(logical_event_time)",
                        "input_columns": (
                            {
                                "table": "public.events",
                                "column": "stored_timestamp",
                            },
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": (base.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )
    return loaded_schema, state, document, decision


def _same_batch_aggregate_preflight(
    tmp_path,
    loaded_schema,
    state: ResearchState,
    document: DocumentRef,
    decision: ResearchDecisionV1,
):
    state_store = AdaptiveResearchStateStore(tmp_path / "state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    registry = _make_registry(loaded_schema.namespace)
    registry.context.schema_runtime.documents = (
        SchemaEvidenceDocument(
            document_id=document.document_id,
            namespace=document.namespace,
            schema_namespace_version=state.schema_namespace_version,
            source_version="v1",
            title="Event formula",
            content="COUNT(logical_event_time); COUNT(logical_existing)",
            target=None,
        ),
    )

    async def no_model(_prompt: str) -> str:
        raise AssertionError("preflight must not call the model")

    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=no_model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=FreshnessContext(
            evaluated_at=_FIXTURE_NOW,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
            document_sources=(
                DocumentSourceState(
                    document_id=document.document_id,
                    availability=DocumentSourceAvailability.AVAILABLE,
                    source_version="v1",
                ),
            ),
        ),
        registry=registry,
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "same-batch-aggregate-owner",
        model_wait=None,
        exact_formula_documents=tuple(
            (item.source_id, document)
            for item in state.query_spec.semantic_items
            if item.kind is SemanticItemKind.FORMULA
        ),
    )
    try:
        return coordinator._preflight_model_decision(state, decision)
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_exact_aggregate_preflight_accepts_same_batch_confirmed_input(tmp_path) -> None:
    """A resolver-admitted same-source physical input may satisfy an exact formula."""

    loaded_schema, state, document, decision = (
        _same_batch_differently_named_exact_aggregate_input()
    )
    feedback, reason, _action, _rejected = _same_batch_aggregate_preflight(
        tmp_path, loaded_schema, state, document, decision
    )

    assert feedback is None
    assert reason is None


def test_exact_aggregate_preflight_detects_compact_connector_input(tmp_path) -> None:
    """A compact connector cannot hide another exact formula input."""

    loaded_schema, state, document, decision = (
        _same_batch_differently_named_exact_aggregate_input()
    )
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "normalized_meaning": (
                "COUNT(logical_event_time WHERE 2011ANDlogical_existing = 'x')"
            )
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            )
        }
    )

    feedback, reason, _action, _rejected = _same_batch_aggregate_preflight(
        tmp_path, loaded_schema, state, document, decision
    )

    assert _research_loop_module._has_exact_aggregate_operand_mismatch(
        state, decision, ((item.source_id, document),), loaded_schema
    )
    assert feedback == "UNRESOLVABLE_PREFLIGHT"
    assert reason is None


def test_exact_aggregate_preflight_accepts_compact_connector_inputs_same_batch(
    tmp_path,
) -> None:
    """A compact exact input and renamed input can be confirmed together."""

    loaded_schema, state, document, decision = (
        _same_batch_differently_named_exact_aggregate_input()
    )
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "normalized_meaning": (
                "COUNT(logical_event_time WHERE 2011ANDlogical_existing = 'x')"
            )
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            )
        }
    )
    stored_time = next(
        proposal
        for proposal in decision.proposals
        if isinstance(proposal.candidate, PhysicalColumnCandidate)
    )
    formula = next(
        proposal
        for proposal in decision.proposals
        if isinstance(proposal.candidate, DerivedExpressionCandidate)
    )
    logical_existing = LogicalColumnRef(
        table="public.events", column="logical_existing"
    )
    decision = decision.model_copy(
        update={
            "proposals": (
                stored_time,
                stored_time.model_copy(
                    update={
                        "proposal_key": "proposal:logical-existing",
                        "candidate": stored_time.candidate.model_copy(
                            update={"physical_column": logical_existing}
                        ),
                    }
                ),
                formula.model_copy(
                    update={
                        "candidate": formula.candidate.model_copy(
                            update={
                                "input_columns": (
                                    *formula.candidate.input_columns,
                                    logical_existing,
                                )
                            }
                        )
                    }
                ),
            )
        }
    )

    feedback, reason, _action, _rejected = _same_batch_aggregate_preflight(
        tmp_path, loaded_schema, state, document, decision
    )

    assert feedback is None
    assert reason is None


def test_exact_aggregate_preflight_combines_existing_and_same_batch_inputs(
    tmp_path,
) -> None:
    loaded_schema, state, document, decision = (
        _same_batch_differently_named_exact_aggregate_input()
    )
    table = TableRef(namespace="main", schema="public", table="events")
    existing_column = ColumnRef(table=table, column="stored_existing")
    existing_binding = PhysicalColumnBinding(
        binding_id="binding:existing-input",
        source_id="source-existing",
        tables=(table,),
        columns=(existing_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(state.evidence[1].evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=existing_column,
    )
    existing_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "source_id": "source-existing",
            "source_text": "existing aggregate",
            "normalized_meaning": "COUNT(logical_existing)",
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (existing_binding.binding_id,),
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        *state.query_spec.semantic_items,
                        existing_item,
                    )
                }
            ),
            "bindings": (existing_binding,),
            "unresolved_items": (
                *state.unresolved_items,
                existing_item.source_id,
            ),
        }
    )
    existing_formula = {
        "proposal_type": "new_binding",
        "proposal_key": "proposal:existing-formula",
        "source_id": existing_item.source_id,
        "candidate": {
            "kind": "derived_expression",
            "expression_claim": "COUNT(logical_existing)",
            "document_id": document.document_id,
            "rule_excerpt": "COUNT(logical_existing)",
            "input_columns": (
                {"table": "public.events", "column": "stored_existing"},
            ),
        },
        "join_references": (),
        "citation_evidence_ids": (state.evidence[0].evidence_id,),
    }
    decision = decision.model_copy(
        update={
            "proposals": (
                *decision.proposals,
                _research_loop_module.NewBindingProposal.model_validate(existing_formula),
            )
        }
    )

    feedback, reason, _action, _rejected = _same_batch_aggregate_preflight(
        tmp_path, loaded_schema, state, document, decision
    )

    assert feedback is None
    assert reason is None


def test_exact_aggregate_preflight_keeps_invalid_same_batch_input_rejected(tmp_path) -> None:
    loaded_schema, state, document, decision = (
        _same_batch_differently_named_exact_aggregate_input()
    )
    physical = next(
        proposal
        for proposal in decision.proposals
        if proposal.candidate.kind == "physical_column"
    )
    invalid = decision.model_copy(
        update={
            "proposals": tuple(
                physical.model_copy(
                    update={
                        "candidate": physical.candidate.model_copy(
                            update={
                                "physical_column": LogicalColumnRef(
                                    table="public.events", column="missing_timestamp"
                                )
                            }
                        )
                    }
                )
                if proposal is physical
                else proposal
                for proposal in decision.proposals
            )
        }
    )

    feedback, reason, _action, _rejected = _same_batch_aggregate_preflight(
        tmp_path, loaded_schema, state, document, invalid
    )

    assert _research_loop_module._has_exact_aggregate_operand_mismatch(
        state,
        invalid,
        ((state.query_spec.semantic_items[0].source_id, document),),
        loaded_schema,
    )
    assert feedback == "UNRESOLVABLE_PREFLIGHT"
    assert reason is None


@pytest.mark.parametrize("change", ("other_source", "other_input", "missing_input"))
def test_exact_aggregate_preflight_requires_matching_same_batch_input(
    tmp_path, change: str
) -> None:
    loaded_schema, state, document, decision = (
        _same_batch_differently_named_exact_aggregate_input()
    )
    physical = next(
        proposal
        for proposal in decision.proposals
        if proposal.candidate.kind == "physical_column"
    )
    if change == "other_source":
        other_item = state.query_spec.semantic_items[0].model_copy(
            update={
                "source_id": "source-2",
                "kind": SemanticItemKind.DIMENSION,
                "source_text": "other attribute",
                "normalized_meaning": "other attribute",
                "required": False,
            }
        )
        state = state.model_copy(
            update={
                "query_spec": state.query_spec.model_copy(
                    update={
                        "semantic_items": (
                            *state.query_spec.semantic_items,
                            other_item,
                        )
                    }
                )
            }
        )
        proposals = tuple(
            physical.model_copy(update={"source_id": "source-2"})
            if proposal is physical
            else proposal
            for proposal in decision.proposals
        )
    elif change == "other_input":
        proposals = (
            physical.model_copy(
                update={
                    "candidate": physical.candidate.model_copy(
                        update={
                            "physical_column": LogicalColumnRef(
                                table="public.events", column="logical_event_time"
                            )
                        }
                    )
                }
            ),
            *(proposal for proposal in decision.proposals if proposal is not physical),
        )
    else:
        proposals = tuple(
            proposal for proposal in decision.proposals if proposal is not physical
        )
    feedback, reason, _action, _rejected = _same_batch_aggregate_preflight(
        tmp_path,
        loaded_schema,
        state,
        document,
        decision.model_copy(update={"proposals": proposals}),
    )

    assert _research_loop_module._has_exact_aggregate_operand_mismatch(
        state,
        decision.model_copy(update={"proposals": proposals}),
        ((state.query_spec.semantic_items[0].source_id, document),),
        loaded_schema,
    )
    assert feedback == "UNRESOLVABLE_PREFLIGHT"
    assert reason is None


def _formula_predicate_decision(
    source_id: str,
    evidence_ids: tuple[str, ...],
    *,
    column: str,
    literal: str = "target",
) -> ResearchDecisionV1:
    return ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:predicate",
                    "source_id": source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": column,
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": column,
                            },
                            "operator": PredicateOperator.EQ,
                            "right": literal,
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": evidence_ids,
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )


def _formula_predicate_candidate(
    state: ResearchState,
    source_id: str,
    *,
    binding_id: str,
    column_name: str = "proxy_label",
    literal: str = "target",
) -> DiscriminatorValueBinding:
    table = state.bindings[0].tables[0]
    column = ColumnRef(table=table, column=column_name)
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right=literal,
    )
    return DiscriminatorValueBinding(
        binding_id=binding_id,
        source_id=source_id,
        tables=(table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=state.bindings[0].evidence_ids,
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=column,
        discriminator_predicate=predicate,
    )


@pytest.mark.parametrize(
    ("column", "rejects"),
    (("proxy_label", True), ("canonical_label", False)),
)
def test_exact_formula_predicate_preflight_rejects_only_proxy_new_candidate(
    column: str, rejects: bool
) -> None:
    _loaded_schema, state, _document, candidate = _exact_aggregate_candidate_state("status")
    decision = _formula_predicate_decision(
        candidate.source_id,
        candidate.evidence_ids,
        column=column,
    )

    assert _research_loop_module._has_exact_formula_predicate_mismatch(
        state,
        decision,
        (
            (
                candidate.source_id,
                DocumentRef(document_id="predicate-rule", namespace="main"),
                "ignored_label",
                "ignore",
            ),
            (
                candidate.source_id,
                DocumentRef(document_id="predicate-rule", namespace="main"),
                "canonical_label",
                "target",
            ),
        ),
    ) is rejects


def test_exact_formula_predicate_preflight_rejects_existing_proxy_candidate() -> None:
    _loaded_schema, state, _document, candidate = _exact_aggregate_candidate_state("status")
    proxy = _formula_predicate_candidate(
        state,
        candidate.source_id,
        binding_id="binding:proxy",
    )
    state = state.model_copy(update={"bindings": (*state.bindings, proxy)})
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": proxy.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": candidate.evidence_ids,
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    assert _research_loop_module._has_exact_formula_predicate_mismatch(
        state,
        decision,
        (
            (
                candidate.source_id,
                DocumentRef(document_id="predicate-rule", namespace="main"),
                "canonical_label",
                "target",
            ),
        ),
    )


def test_predicate_feedback_names_missing_column_for_new_and_existing() -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state("status")
    proxy = _formula_predicate_candidate(
        state,
        candidate.source_id,
        binding_id="binding:proxy-feedback",
    )
    state = state.model_copy(update={"bindings": (state.bindings[0], proxy)})
    new = _formula_predicate_decision(
        candidate.source_id,
        candidate.evidence_ids,
        column="proxy_label",
    )
    existing = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": proxy.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": candidate.evidence_ids,
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )
    unrelated = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": state.bindings[0].binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": state.bindings[0].evidence_ids,
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )
    rejected = tuple(
        {"proposal": proposal.model_dump(mode="json", by_alias=True)}
        for decision in (new, existing, unrelated)
        for proposal in decision.proposals
    )

    assert _research_loop_module._rejected_external_exact_formula_missing_operands(
        rejected,
        state,
        ((candidate.source_id, document),),
        loaded_schema,
        (
            (candidate.source_id, document, "ignored_label", "ignore"),
            (candidate.source_id, document, "canonical_label", "target"),
        ),
    ) == {candidate.source_id: ("canonical_label",)}


def test_exact_formula_retry_hint_identifies_predicate_as_predicate() -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        "status",
        "COUNT(status WHERE canonical_label = 'target')",
        {
            "public.orders": {
                "columns": {
                    "status": {},
                    "canonical_label": {},
                    "proxy_label": {},
                }
            }
        },
    )

    hint = _research_loop_module._external_exact_formula_stop_review_hint(
        state,
        ((candidate.source_id, document),),
        (candidate.source_id,),
        loaded_schema,
        {candidate.source_id: ("canonical_label",)},
        ((candidate.source_id, document, "canonical_label", "target"),),
    )

    assert hint is not None
    assert "Missing exact predicate column: canonical_label" in hint
    assert "Missing same-name aggregate operand: canonical_label" not in hint
    assert "locate its loaded same-name column and declared relationship path" in hint
    assert "public.orders" not in hint


def test_exact_formula_retry_hint_keeps_roles_separate_across_sources() -> None:
    loaded_schema, state, aggregate_document, candidate = (
        _exact_aggregate_candidate_state(
            "id",
            "COUNT(status)",
            {"public.orders": {"columns": {"id": {}, "status": {}}}},
        )
    )
    predicate_source_id = "semantic:predicate-source"
    predicate_document = DocumentRef(
        document_id="predicate-rule",
        namespace="main",
    )
    predicate_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "source_id": predicate_source_id,
            "normalized_meaning": "COUNT(id WHERE status = 'target')",
            "requested_output": False,
            "status": SemanticItemStatus.UNRESOLVED,
            "binding_ids": (),
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        state.query_spec.semantic_items[0],
                        predicate_item,
                    )
                }
            )
        }
    )

    hint = _research_loop_module._external_exact_formula_stop_review_hint(
        state,
        (
            (candidate.source_id, aggregate_document),
            (predicate_source_id, predicate_document),
        ),
        (candidate.source_id, predicate_source_id),
        loaded_schema,
        {
            candidate.source_id: ("status",),
            predicate_source_id: ("status",),
        },
        ((predicate_source_id, predicate_document, "status", "target"),),
    )

    assert hint is not None
    assert "Missing same-name aggregate operand: status" in hint
    assert "Missing exact predicate column: status" in hint


def test_predicate_only_preflight_retries_with_exact_column_hint(tmp_path) -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        "status"
    )
    prompts: list[dict[str, object]] = []
    proxy_decision = _formula_predicate_decision(
        candidate.source_id,
        candidate.evidence_ids,
        column="proxy_label",
    )

    async def model(prompt: str) -> str:
        prompts.append(json.loads(prompt))
        return proxy_decision.model_dump_json(by_alias=True)

    async def stop_review_model(_prompt: str) -> str:
        return '{"decision":"stop_confirmed","hint":null}'

    state_store = AdaptiveResearchStateStore(tmp_path / "state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks, *_args: canonical_digest(
            current
        ),
        model=model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(loaded_schema.namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(model_calls=4),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "predicate-retry-owner",
        model_wait=None,
        exact_formula_documents=(),
        exact_formula_predicate_constraints=(
            (candidate.source_id, document, "ignored_label", "ignore"),
            (candidate.source_id, document, "canonical_label", "target"),
        ),
        stop_review_model=stop_review_model,
    )
    try:
        decision, reason, _freshness = asyncio.run(
            coordinator._model_decision(state)
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    assert decision is None
    assert reason is ResearchStopReason.STAGNATED
    assert len(prompts) == 2
    retry_context = prompts[1]["input"]["research_context"]
    assert isinstance(retry_context, str)
    assert "Trusted document predicate" in retry_context
    assert "Missing exact predicate column: canonical_label" in retry_context
    assert state.query_spec.semantic_items[0].normalized_meaning not in retry_context


def test_predicate_only_hint_avoids_malformed_formula_text() -> None:
    _loaded_schema, state, document, candidate = _exact_aggregate_candidate_state("status")
    hint = _research_loop_module._external_exact_formula_stop_review_hint(
        state, (), (candidate.source_id,), None,
        {candidate.source_id: ("canonical_label",)},
        ((candidate.source_id, document, "canonical_label", "target"),),
    )

    assert hint is not None
    assert "Trusted document predicate" in hint
    assert "Missing exact predicate column: canonical_label" in hint
    assert state.query_spec.semantic_items[0].normalized_meaning not in hint


def test_rejected_preflight_hint_requires_separate_missing_predicate_binding() -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        "status"
    )
    constraints = (
        (candidate.source_id, document, "canonical_label", "target"),
    )
    rejected = (
        {
            "source_id": candidate.source_id,
            "missing_exact_predicate_columns": ["canonical_label"],
        },
    )

    missing = _research_loop_module._rejected_preflight_missing_exact_predicates(
        rejected,
        state,
        constraints,
    )
    hint = _research_loop_module._validated_stop_review_hint(
        "Finish the exact formula evidence.",
        state,
        (candidate.binding_id,),
        loaded_schema,
        missing,
    )

    assert missing == {candidate.source_id: ("canonical_label",)}
    assert "separate new_binding" in hint
    assert "binding_assessment" not in hint
    assert "semantic_commit with those assessments" not in hint
    assert "not new_binding" not in hint
    assert "Do not replace" not in hint


def test_rejected_preflight_hint_keeps_ordinary_assessment_after_predicate_exists() -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        "status"
    )
    constraints = (
        (candidate.source_id, document, "canonical_label", "target"),
    )
    prior_hint = _research_loop_module._validated_stop_review_hint(
        "Assess the existing candidate.",
        state,
        (candidate.binding_id,),
        loaded_schema,
        {candidate.source_id: ("canonical_label",)},
    )
    predicate = _formula_predicate_candidate(
        state,
        candidate.source_id,
        binding_id="binding:canonical-predicate",
        column_name="canonical_label",
    )
    state = state.model_copy(update={"bindings": (*state.bindings, predicate)})
    rejected = (
        {
            "source_id": candidate.source_id,
            "missing_exact_predicate_columns": ["canonical_label"],
        },
    )

    missing = _research_loop_module._rejected_preflight_missing_exact_predicates(
        rejected,
        state,
        constraints,
    )
    hint = _research_loop_module._validated_stop_review_hint(
        prior_hint,
        state,
        (candidate.binding_id,),
        loaded_schema,
        missing,
    )

    assert missing == {}
    assert "not new_binding" in hint
    assert "separate new_binding" not in hint


def test_exact_predicate_commit_preview_requires_all_constraints() -> None:
    _loaded_schema, state, document, candidate = _exact_aggregate_candidate_state("status")
    canonical = _formula_predicate_candidate(
        state,
        candidate.source_id,
        binding_id="binding:canonical-predicate",
        column_name="canonical_label",
    ).model_copy(
        update={
            "status": BindingStatus.SUPPORTED,
            "validator_rule": "semantic-certificate:v1:discriminator_value",
        }
    )
    state = state.model_copy(update={"bindings": (*state.bindings, canonical)})
    constraints = (
        (candidate.source_id, document, "canonical_label", "target"),
        (candidate.source_id, document, "second_label", "other"),
    )

    assert _research_loop_module._missing_resolved_exact_formula_predicates(
        state, constraints
    ) == {candidate.source_id: ("second_label",)}

    second = _formula_predicate_candidate(
        state,
        candidate.source_id,
        binding_id="binding:second-predicate",
        column_name="second_label",
        literal="other",
    ).model_copy(
        update={
            "status": BindingStatus.SUPPORTED,
            "validator_rule": "semantic-certificate:v1:discriminator_value",
        }
    )
    complete = state.model_copy(update={"bindings": (*state.bindings, second)})

    assert not _research_loop_module._missing_resolved_exact_formula_predicates(
        complete, constraints
    )


def test_exact_predicate_commit_accepts_same_name_on_another_table_without_rows() -> None:
    _loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        "status"
    )
    original_table = state.bindings[0].tables[0]
    other_table = TableRef(
        namespace=original_table.namespace,
        schema_name=original_table.schema_name,
        table="other_orders",
    )
    column = ColumnRef(table=other_table, column="canonical_label")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right="target",
    )
    supported = DiscriminatorValueBinding(
        binding_id="binding:other-table-predicate",
        source_id=candidate.source_id,
        tables=(other_table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=state.bindings[0].evidence_ids,
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="semantic-certificate:v1:discriminator_value",
        discriminator_column=column,
        discriminator_predicate=predicate,
    )
    state = state.model_copy(
        update={"bindings": (*state.bindings, supported), "evidence": ()}
    )

    assert not _research_loop_module._missing_resolved_exact_formula_predicates(
        state,
        ((candidate.source_id, document, "canonical_label", "target"),),
    )


@pytest.mark.parametrize(
    ("column", "operator", "literal", "source_id"),
    (
        ("other_label", PredicateOperator.EQ, "target", None),
        ("canonical_label", PredicateOperator.LIKE, "target", None),
        ("canonical_label", PredicateOperator.EQ, "other", None),
        ("canonical_label", PredicateOperator.EQ, "target", "semantic:other"),
    ),
)
def test_exact_predicate_commit_rejects_nonmatching_binding(
    column: str,
    operator: PredicateOperator,
    literal: str,
    source_id: str | None,
) -> None:
    _loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        "status"
    )
    table = state.bindings[0].tables[0]
    column_ref = ColumnRef(table=table, column=column)
    predicate = PredicateRef(left=column_ref, operator=operator, right=literal)
    binding = DiscriminatorValueBinding(
        binding_id="binding:nonmatching-predicate",
        source_id=source_id or candidate.source_id,
        tables=(table,),
        columns=(column_ref,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=state.bindings[0].evidence_ids,
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="semantic-certificate:v1:discriminator_value",
        discriminator_column=column_ref,
        discriminator_predicate=predicate,
    )
    state = state.model_copy(update={"bindings": (*state.bindings, binding)})

    assert _research_loop_module._missing_resolved_exact_formula_predicates(
        state,
        ((candidate.source_id, document, "canonical_label", "target"),),
    ) == {candidate.source_id: ("canonical_label",)}


def test_exact_predicate_commit_ignores_non_formula_source() -> None:
    _loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        "status"
    )
    item = state.query_spec.semantic_items[0].model_copy(
        update={"kind": SemanticItemKind.FILTER}
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            )
        }
    )

    assert not _research_loop_module._missing_resolved_exact_formula_predicates(
        state,
        ((candidate.source_id, document, "canonical_label", "target"),),
    )


def test_exact_predicate_commit_preflight_reports_omitted_constraint(tmp_path) -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        "status"
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": candidate.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": candidate.evidence_ids,
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )
    constraints = (
        (candidate.source_id, document, "canonical_label", "target"),
    )
    state_store = AdaptiveResearchStateStore(tmp_path / "state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")

    async def no_model(_prompt: str) -> str:
        raise AssertionError("preflight must not call the model")

    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=no_model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(loaded_schema.namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "predicate-commit-owner",
        model_wait=None,
        exact_formula_documents=((candidate.source_id, document),),
        exact_formula_predicate_constraints=constraints,
    )
    try:
        feedback, reason, _action, rejected = coordinator._preflight_model_decision(
            state, decision
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    assert feedback == "UNRESOLVABLE_PREFLIGHT"
    assert reason is None
    assert _research_loop_module._rejected_external_exact_formula_missing_operands(
        rejected,
        state,
        ((candidate.source_id, document),),
        loaded_schema,
        constraints,
    ) == {candidate.source_id: ("canonical_label",)}


def test_rejected_preflight_feedback_explains_calendar_year_equality() -> None:
    """A rejected full-temporal year equality needs a specific retry reason."""

    loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    formula = (
        "PERCENTAGE(COUNT(record_id WHERE YEAR(recorded_at) = 2024 "
        "AND score > 80), COUNT(record_id))"
    )
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": formula,
            "required": True,
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            )
        }
    )
    document = SchemaEvidenceDocument(
        document_id="calendar-year-rule",
        namespace="main",
        schema_namespace_version=state.schema_namespace_version,
        source_version="v1",
        title="Calendar-year formula",
        content=f"Exact formula: {formula}.",
        target=None,
    )
    schema = replace(
        loaded_schema,
        schema={
            "public.orders": {
                "columns": {
                    "record_id": {"type": "INTEGER"},
                    "recorded_at": {"type": "DATETIME"},
                    "score": {"type": "INTEGER"},
                }
            }
        },
    )
    evidence_ids = state.bindings[0].evidence_ids
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:calendar-year",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "recorded_at",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "recorded_at",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": 2024,
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": evidence_ids,
                },
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:score-bound",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "score",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "score",
                            },
                            "operator": PredicateOperator.GT,
                            "right": 80,
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": evidence_ids,
                },
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:formula",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "derived_expression",
                        "expression_claim": formula,
                        "document_id": document.document_id,
                        "rule_excerpt": formula,
                        "input_columns": (
                            {"table": "public.orders", "column": "record_id"},
                            {"table": "public.orders", "column": "recorded_at"},
                            {"table": "public.orders", "column": "score"},
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": evidence_ids,
                },
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:mismatched-left",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "recorded_at",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "score",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": 2024,
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": evidence_ids,
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    assert _exact_formula_documents(state, (document,)) == (
        (item.source_id, DocumentRef(document_id=document.document_id, namespace="main")),
    )
    rejected = _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=schema,
        exact_formula_documents=(
            (item.source_id, DocumentRef(document_id=document.document_id, namespace="main")),
        ),
    )
    by_key = {
        assessment["proposal"]["proposal_key"]: assessment for assessment in rejected
    }

    assert by_key["proposal:calendar-year"]["rejection_reason"] == (
        "calendar component on full temporal column requires a range predicate"
    )
    assert by_key["proposal:formula"]["rejection_reason"] == (
        "trusted exact formula requires a calendar-year range on its only confirmed temporal input"
    )
    assert "rejection_reason" not in by_key["proposal:score-bound"]
    assert "rejection_reason" not in by_key["proposal:mismatched-left"]
    without_document = _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=schema,
    )
    assert all("rejection_reason" not in assessment for assessment in without_document)


def test_rejected_preflight_feedback_explains_missing_calendar_year_range() -> None:
    """A unique temporal formula input makes an omitted year range actionable."""

    loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    formula_expression = (
        "PERCENTAGE(COUNT(record_id WHERE YEAR(logical_event_time) = 2024 "
        "AND score > 80), COUNT(record_id))"
    )
    formula = (
        formula_expression
        + "; explanation repeats YEAR(second_timestamp) = 2023"
    )
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": formula,
            "required": True,
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            )
        }
    )
    document = SchemaEvidenceDocument(
        document_id="calendar-range-rule",
        namespace="main",
        schema_namespace_version=state.schema_namespace_version,
        source_version="v1",
        title="Calendar range formula",
        content=f"Exact formula: {formula_expression}.",
        target=None,
    )
    schema = replace(
        loaded_schema,
        schema={
            "public.orders": {
                "columns": {
                    "record_id": {"type": "INTEGER"},
                    "stored_timestamp": {"type": "DATETIME"},
                    "second_timestamp": {"type": "TIMESTAMP"},
                    "score": {"type": "INTEGER"},
                }
            }
        },
    )
    evidence_ids = state.bindings[0].evidence_ids

    def decision(
        temporal_inputs: tuple[str, ...],
        *,
        include_range: bool = False,
        include_temporal_equality: bool = False,
        include_timestamp_equality: bool = False,
        include_lower_bound: bool = False,
        include_wrong_range: bool = False,
        expression_claim: str = formula_expression,
    ) -> ResearchDecisionV1:
        proposals: list[dict[str, object]] = [
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:formula",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "derived_expression",
                        "expression_claim": expression_claim,
                        "document_id": document.document_id,
                        "rule_excerpt": formula,
                        "input_columns": (
                        {"table": "public.orders", "column": "record_id"},
                        *(
                            {"table": "public.orders", "column": column}
                            for column in temporal_inputs
                        ),
                            {"table": "public.orders", "column": "score"},
                        ),
                    },
                "join_references": (),
                "citation_evidence_ids": evidence_ids,
            },
            {
                "proposal_type": "new_binding",
                "proposal_key": "proposal:score-bound",
                "source_id": item.source_id,
                "candidate": {
                    "kind": "discriminator_value",
                    "discriminator_column": {
                        "table": "public.orders",
                        "column": "score",
                    },
                    "discriminator_predicate": {
                        "left": {"table": "public.orders", "column": "score"},
                        "operator": PredicateOperator.GT,
                        "right": 80,
                    },
                },
                "join_references": (),
                "citation_evidence_ids": evidence_ids,
            },
        ]
        if include_range:
            proposals.append(
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:time-range",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "stored_timestamp",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "stored_timestamp",
                            },
                            "operator": PredicateOperator.GTE,
                            "right": "2024-01-01",
                        },
                        "additional_predicates": (
                            {
                                "left": {
                                    "table": "public.orders",
                                    "column": "stored_timestamp",
                                },
                                "operator": PredicateOperator.LT,
                                "right": "2025-01-01",
                            },
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": evidence_ids,
                }
            )
        if include_temporal_equality:
            proposals.append(
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:time-equality",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "stored_timestamp",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "stored_timestamp",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": 2024,
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": evidence_ids,
                }
            )
        if include_timestamp_equality:
            proposals.append(
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:time-timestamp-equality",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "stored_timestamp",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "stored_timestamp",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": "2024-01-01",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": evidence_ids,
                }
            )
        if include_lower_bound:
            proposals.append(
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:time-lower-bound",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "stored_timestamp",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "stored_timestamp",
                            },
                            "operator": PredicateOperator.GTE,
                            "right": "2024-01-01",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": evidence_ids,
                }
            )
        if include_wrong_range:
            proposals.append(
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:time-wrong-range",
                    "source_id": item.source_id,
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "stored_timestamp",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "stored_timestamp",
                            },
                            "operator": PredicateOperator.GTE,
                            "right": "2024-01-01",
                        },
                        "additional_predicates": (
                            {
                                "left": {
                                    "table": "public.orders",
                                    "column": "stored_timestamp",
                                },
                                "operator": PredicateOperator.LT,
                                "right": "2024-12-31",
                            },
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": evidence_ids,
                }
            )
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": tuple(proposals),
                "next": {"next_kind": "semantic_commit"},
            }
        )

    exact_documents = (
        (item.source_id, DocumentRef(document_id=document.document_id, namespace="main")),
    )
    assert _exact_formula_documents(state, (document,)) == exact_documents

    def feedback(
        candidate: ResearchDecisionV1,
        *,
        current_state: ResearchState = state,
        documents=exact_documents,
    ) -> dict[str, dict[str, object]]:
        return {
            assessment["proposal"]["proposal_key"]: assessment
            for assessment in _research_loop_module._rejected_preflight_assessment_context(
                current_state,
                candidate,
                _freshness(current_state),
                requested_action=None,
                loaded_schema=schema,
                exact_formula_documents=documents,
            )
        }

    def state_with_time_range(
        *,
        required: bool = True,
        year: int = 2024,
        column_name: str = "stored_timestamp",
        complete: bool = True,
        status: BindingStatus = BindingStatus.CANDIDATE,
    ) -> ResearchState:
        table = TableRef(namespace="main", schema="public", table="orders")
        column = ColumnRef(table=table, column=column_name)
        start = {
            "left": column,
            "operator": PredicateOperator.GTE,
            "right": f"{year:04d}-01-01",
        }
        predicates = (
            start,
            {
                "left": column,
                "operator": PredicateOperator.LT,
                "right": f"{year + 1:04d}-01-01",
            },
        ) if complete else (start,)
        binding = DiscriminatorValueBinding(
            binding_id="binding:time-range",
            source_id="semantic:time-range",
            tables=(table,),
            columns=(column,),
            predicates=predicates,
            join_path=(),
            evidence_ids=evidence_ids,
            confidence=1.0,
            status=status,
            validator_rule="range evidence" if status is BindingStatus.SUPPORTED else None,
            discriminator_column=column,
            discriminator_predicate=start,
        )
        time_item = item.model_copy(
            update={
                "source_id": binding.source_id,
                "kind": SemanticItemKind.TIME,
                "source_text": "in calendar year",
                "normalized_meaning": "calendar year",
                "literal_or_reference": year,
                "operator": PredicateOperator.EQ,
                "required": required,
                "status": SemanticItemStatus.PARTIALLY_RESOLVED,
                "binding_ids": (binding.binding_id,),
            }
        )
        return state.model_copy(
            update={
                "query_spec": state.query_spec.model_copy(
                    update={"semantic_items": (item, time_item)}
                ),
                "bindings": (*state.bindings, binding),
            }
        )

    omitted = feedback(decision(("stored_timestamp",)))
    assert omitted["proposal:formula"]["rejection_reason"] == (
        "trusted exact formula requires a calendar-year range on its only confirmed temporal input"
    )
    assert "rejection_reason" not in omitted["proposal:score-bound"]
    assert "rejection_reason" not in feedback(
        decision(("stored_timestamp", "second_timestamp"))
    )["proposal:formula"]
    assert "rejection_reason" not in feedback(
        decision(("stored_timestamp",)), documents=()
    )["proposal:formula"]
    assert "rejection_reason" not in feedback(
        decision(("stored_timestamp",), include_range=True)
    )["proposal:formula"]
    for decision_with_incomplete_temporal_predicate in (
        decision(("stored_timestamp",), include_temporal_equality=True),
        decision(("stored_timestamp",), include_timestamp_equality=True),
        decision(("stored_timestamp",), include_lower_bound=True),
        decision(("stored_timestamp",), include_wrong_range=True),
    ):
        assert feedback(decision_with_incomplete_temporal_predicate)["proposal:formula"][
            "rejection_reason"
        ] == (
            "trusted exact formula requires a calendar-year range on its only confirmed temporal input"
        )
    assert "rejection_reason" not in feedback(
        decision(("stored_timestamp",), expression_claim="COUNT(unrelated_id)")
    )["proposal:formula"]
    for status in (BindingStatus.CANDIDATE, BindingStatus.SUPPORTED):
        assert "rejection_reason" not in feedback(
            decision(("stored_timestamp",)),
            current_state=state_with_time_range(status=status),
        )["proposal:formula"]
    for time_state in (
        state_with_time_range(required=False),
        state_with_time_range(year=2023),
        state_with_time_range(column_name="second_timestamp"),
        state_with_time_range(complete=False),
        state_with_time_range(status=BindingStatus.REJECTED),
    ):
        assert feedback(
            decision(("stored_timestamp",)), current_state=time_state
        )["proposal:formula"]["rejection_reason"] == (
            "trusted exact formula requires a calendar-year range on its only confirmed temporal input"
        )


def test_formula_predicate_constraint_decodes_sql_quote_before_preflight() -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    formula = "COUNT(record_id WHERE canonical_label = 'O''Brien')"
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": formula,
            "required": True,
            "exact_physical_predicate": True,
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            )
        }
    )
    document = SchemaEvidenceDocument(
        document_id="quoted-predicate-rule",
        namespace="main",
        schema_namespace_version=state.schema_namespace_version,
        source_version="v1",
        title="Quoted predicate",
        content=formula,
        target=None,
    )
    schema = replace(
        loaded_schema,
        schema={
            "public.records": {
                "columns": {
                    "record_id": {},
                    "canonical_label": {},
                    "proxy_label": {},
                }
            }
        },
    )
    constraints = _production_research_module._exact_formula_predicate_constraints(
        state,
        (document,),
        schema,
    )
    decision = _formula_predicate_decision(
        item.source_id,
        state.bindings[0].evidence_ids,
        column="proxy_label",
        literal="O'Brien",
    )

    assert constraints == (
        (
            item.source_id,
            DocumentRef(document_id=document.document_id, namespace="main"),
            "canonical_label",
            "O'Brien",
        ),
    )
    assert _research_loop_module._has_exact_formula_predicate_mismatch(
        state,
        decision,
        constraints,
    )


@pytest.mark.parametrize(
    "formula",
    (
        "COUNT(record_id WHERE missing_label = 'target')",
        "COUNT(record_id WHERE canonical_label != 'target')",
        'COUNT(record_id WHERE canonical_label = "target")',
        "COUNT(record_id) WHERE canonical_label = 'target'",
    ),
)
def test_document_formula_predicate_constraint_skips_outside_narrow_grammar(formula: str) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "normalized_meaning": formula,
            "required": True,
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            )
        }
    )
    document = SchemaEvidenceDocument(
        document_id="grammar-rule",
        namespace="main",
        schema_namespace_version=state.schema_namespace_version,
        source_version="v1",
        title="Grammar",
        content=formula,
        target=None,
    )
    schema = replace(
        loaded_schema,
        schema={
            "public.records": {
                "columns": {"record_id": {}, "canonical_label": {}}
            }
        },
    )

    assert _production_research_module._exact_formula_predicate_constraints(
        state,
        (document,),
        schema,
    ) == ()


def test_runtime_exact_formula_candidates_exclude_missing_aggregate_operand_input() -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state("id")

    assert _research_loop_module._runtime_exact_formula_candidate_binding_ids(
        state, ((candidate.source_id, document),), loaded_schema
    ) == ()


def test_stop_review_authority_excludes_mismatching_exact_formula_candidate(tmp_path) -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state("id")
    state_store = AdaptiveResearchStateStore(tmp_path / "state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")

    async def no_decision_model(_prompt: str) -> str:
        raise AssertionError("_review_stop must not call the decision model")

    async def review_model(_prompt: str) -> str:
        return '{"decision":"continue","hint":"Assess the affected candidate."}'

    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=no_decision_model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(loaded_schema.namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "aggregate-authority-owner",
        model_wait=None,
        exact_formula_documents=((candidate.source_id, document),),
        stop_review_model=review_model,
    )
    context = json.dumps(
        {
            "invalid_stop_generation_authority": {
                "reason_code": "QUERY_REQUIREMENT_INCOMPLETE",
                "affected_source_ids": [candidate.source_id],
            }
        }
    )
    try:
        hint, _attempt = asyncio.run(
            coordinator._review_stop(state, ResearchStopReason.STAGNATED, context, 0)
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    assert hint is not None
    assert candidate.binding_id not in hint


@pytest.mark.parametrize(
    ("formula", "input_column", "overrides_judge_hint"),
    (
        ("COUNT(status WHERE kind = 'x')", "id", True),
        ("COUNT(status WHERE kind = 'x')", "status", False),
        ("COUNT(unknown WHERE kind = 'x')", "id", False),
        ("COUNT(*)", "id", False),
        ("COUNT(category = 'SUM(status)')", "id", False),
    ),
)
def test_stop_review_routes_mismatching_exact_formula_without_rejection(
    tmp_path, formula, input_column, overrides_judge_hint
) -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        input_column, formula
    )
    state_store = AdaptiveResearchStateStore(tmp_path / "state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    judge_hint = "Use the base table proxy."

    async def no_decision_model(_prompt: str) -> str:
        raise AssertionError("_review_stop must not call the decision model")

    async def review_model(_prompt: str) -> str:
        return json.dumps({"decision": "continue", "hint": judge_hint})

    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks: canonical_digest(current),
        model=no_decision_model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(loaded_schema.namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "aggregate-routing-owner",
        model_wait=None,
        exact_formula_documents=((candidate.source_id, document),),
        stop_review_model=review_model,
    )
    try:
        hint, _attempt = asyncio.run(
            coordinator._review_stop(state, ResearchStopReason.STAGNATED, "{}", 0)
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    assert hint is not None
    if overrides_judge_hint:
        assert formula in hint
        assert judge_hint not in hint
        assert candidate.binding_id not in hint
    else:
        assert judge_hint in hint


def test_model_retries_keep_mismatch_formula_hint_after_preflight_rejection(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        "Id",
        "COUNT(RecordRef WHERE Label = 'x')",
        {"public.orders": {"columns": {"Id": {}, "RecordRef": {}}}},
    )
    state = state.model_copy(update={"bindings": (state.bindings[0],)})
    state_store = AdaptiveResearchStateStore(tmp_path / "state.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "checkpoint.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")
    prompts: list[dict[str, object]] = []
    responses = iter(
        (
            '{"decision_version":1,"proposals":[],"next":{"next_kind":"tool",'
            '"hypothesis_ref":null,"intent":{"tool_name":"inspect_table",'
            '"arguments":{"table":"public.orders"}}}}',
            "not a typed decision",
            '{"decision_version":1,"proposals":[],"next":{"next_kind":"tool",'
            '"hypothesis_ref":null,"intent":{"tool_name":"inspect_table",'
            '"arguments":{"table":"public.orders"}}}}',
            '{"decision_version":1,"proposals":[],"next":{"next_kind":"tool",'
            '"hypothesis_ref":null,"intent":{"tool_name":"inspect_relationships",'
            '"arguments":{"table":"public.orders","top_k":1,"depth":1}}}}',
        )
    )
    rejected_assessments = (
        {
            "proposal": {
                "proposal_type": "new_binding",
                "proposal_key": "proposal:record-ref",
                "source_id": candidate.source_id,
                "candidate": {
                    "kind": "derived_expression",
                    "expression_claim": candidate.expression.expression,
                    "document_id": document.document_id,
                    "rule_excerpt": candidate.rule_excerpt,
                    "input_columns": [{"table": "public.orders", "column": "Id"}],
                },
                "join_references": (),
                "citation_evidence_ids": list(candidate.evidence_ids),
            }
        },
    )
    assert _research_loop_module._rejected_external_exact_formula_missing_operands(
        rejected_assessments,
        state,
        ((candidate.source_id, document),),
        loaded_schema,
    ) == {candidate.source_id: ("RecordRef",)}
    preflights = iter(
        (
            ("UNRESOLVABLE_PREFLIGHT", None, None, rejected_assessments),
            ("DUPLICATE_ACTION", None, {"kind": "inspect_table"}, ()),
            (None, None, None, ()),
        )
    )
    monkeypatch.setattr(
        _research_loop_module._ResearchLoopCoordinator,
        "_preflight_model_decision",
        lambda *_args: next(preflights),
    )

    async def model(prompt: str) -> str:
        prompts.append(json.loads(prompt))
        return next(responses)

    async def no_stop_review(*_args):
        raise AssertionError("immediate retry must not call stop review")

    coordinator = _research_loop_module._ResearchLoopCoordinator(
        initial_state=state,
        task="research schema",
        research_context=lambda current, _feedbacks, *_args: canonical_digest(current),
        model=model,
        model_identity="test/model",
        adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        loaded_schema=loaded_schema,
        freshness_context=_fixture_freshness(state),
        registry=_make_registry(loaded_schema.namespace),
        state_store=state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=ledger,
        policy=_policy(model_calls=4),
        deadline=None,
        is_cancelled=lambda: False,
        model_claim_now_ns=lambda: 0,
        model_owner_token_factory=lambda: "immediate-hint-owner",
        model_wait=None,
        exact_formula_documents=((candidate.source_id, document),),
    )
    monkeypatch.setattr(coordinator, "_review_stop", no_stop_review)
    try:
        decision, reason, _freshness = asyncio.run(coordinator._model_decision(state))
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()

    assert decision is not None
    assert reason is None
    assert not any(
        isinstance(binding, DerivedExpressionBinding) for binding in state.bindings
    )
    contexts = [prompt["input"]["research_context"] for prompt in prompts]
    assert document.document_id not in contexts[0]
    assert all(formula in context for formula, context in zip((
        state.query_spec.semantic_items[0].normalized_meaning,
        state.query_spec.semantic_items[0].normalized_meaning,
        state.query_spec.semantic_items[0].normalized_meaning,
    ), contexts[1:]))
    assert all("Missing same-name aggregate operand: RecordRef" in context for context in contexts[1:])


def test_immediate_retry_hint_filter_excludes_other_source_assessment() -> None:
    _loaded_schema, state, _document, candidate = _exact_aggregate_candidate_state("id")
    other_source_assessment = (
        {
            "proposal": {
                "proposal_type": "new_binding",
                "proposal_key": "proposal:other-source",
                "source_id": "source-other",
                "candidate": {
                    "kind": "physical_column",
                    "physical_column": {"table": "public.orders", "column": "id"},
                },
                "join_references": (),
                "citation_evidence_ids": list(candidate.evidence_ids),
            }
        },
    )

    assert not _research_loop_module._filter_pending_rejected_preflight_assessments(
        other_source_assessment, state, {candidate.source_id}
    )


def test_exact_formula_hint_uses_only_candidate_inputs_for_missing_operands() -> None:
    loaded_schema, state, document, candidate = _exact_aggregate_candidate_state(
        "OtherRef",
        "COUNT(RecordRef) + COUNT(OtherRef)",
        {"public.orders": {"columns": {"RecordRef": {}, "OtherRef": {}}}},
    )
    rejected = candidate.model_copy(
        update={
            "binding_id": "binding:rejected-aggregate",
            "status": BindingStatus.REJECTED,
            "input_columns": (ColumnRef(table=candidate.tables[0], column="RecordRef"),),
        }
    )
    state = state.model_copy(update={"bindings": (*state.bindings, rejected)})

    hint = _research_loop_module._external_exact_formula_stop_review_hint(
        state, ((candidate.source_id, document),), (candidate.source_id,), loaded_schema
    )

    assert hint is not None
    assert "Missing same-name aggregate operand: RecordRef" in hint
    assert "RecordRef, OtherRef" not in hint


def test_stop_review_assesses_authority_affected_resolved_filter_candidate(
    tmp_path,
) -> None:
    """An incomplete required filter keeps only its affected candidate in the hint."""

    loaded_schema, namespace = _fixture_schema()
    base = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    physical_binding = base.bindings[0]
    table = physical_binding.tables[0]
    category = ColumnRef(table=table, column="category")
    predicate = {
        "left": category,
        "operator": PredicateOperator.EQ,
        "right": "amber",
    }
    filter_item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FILTER,
            "required": True,
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (physical_binding.binding_id,),
        }
    )

    def candidate(binding_id: str, source_id: str) -> DiscriminatorValueBinding:
        return DiscriminatorValueBinding(
            binding_id=binding_id,
            source_id=source_id,
            tables=(table,),
            columns=(category,),
            predicates=(predicate,),
            join_path=(),
            evidence_ids=physical_binding.evidence_ids,
            confidence=0.0,
            status=BindingStatus.CANDIDATE,
            validator_rule=None,
            discriminator_column=category,
            discriminator_predicate=predicate,
        )

    affected = candidate("binding:affected-filter", filter_item.source_id)
    unrelated = candidate("binding:unrelated-filter", "semantic:unrelated-filter")
    unrelated_item = filter_item.model_copy(
        update={
            "source_id": unrelated.source_id,
            "required": False,
            "status": SemanticItemStatus.PARTIALLY_RESOLVED,
            "binding_ids": (unrelated.binding_id,),
        }
    )
    state = ResearchState.model_validate(
        {
            **base.model_dump(mode="python", by_alias=True, round_trip=True),
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (filter_item, unrelated_item)}
            ),
            "bindings": (physical_binding, affected, unrelated),
        }
    )
    authority_context = json.dumps(
        {
            "invalid_stop_generation_authority": {
                "reason_code": "QUERY_REQUIREMENT_INCOMPLETE",
                "affected_source_ids": [filter_item.source_id],
            }
        }
    )

    def review_hint(
        name: str,
        response: str,
        prior_hint: str | None = None,
        context: str = authority_context,
    ) -> str | None:
        state_store = AdaptiveResearchStateStore(tmp_path / f"{name}-state.sqlite")
        checkpoint_store = AdaptiveStateStore(tmp_path / f"{name}-checkpoint.sqlite")
        ledger = AdaptiveBudgetLedger(tmp_path / f"{name}-budget.sqlite")

        async def no_decision_model(_prompt: str) -> str:
            raise AssertionError("_review_stop must not call the decision model")

        async def review_model(_prompt: str) -> str:
            return response

        coordinator = _research_loop_module._ResearchLoopCoordinator(
            initial_state=state,
            task="research schema",
            research_context=lambda current, _feedbacks: canonical_digest(current),
            model=no_decision_model,
            model_identity="test/model",
            adapter=SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
            state_store=state_store,
            checkpoint_store=checkpoint_store,
            budget_ledger=ledger,
            policy=_policy(),
            deadline=None,
            is_cancelled=lambda: False,
            model_claim_now_ns=lambda: 0,
            model_owner_token_factory=lambda: f"{name}-owner",
            model_wait=None,
            stop_review_model=review_model,
        )
        coordinator._last_stop_review_hint = prior_hint
        try:
            hint, _attempt = asyncio.run(
                coordinator._review_stop(
                    state,
                    ResearchStopReason.STAGNATED,
                    context,
                    0,
                )
            )
            return hint
        finally:
            state_store.close()
            checkpoint_store.close()
            ledger.close()

    hint = review_hint(
        "continue",
        '{"decision":"continue","hint":"Assess the incomplete filter."}',
    )
    assert hint is not None
    assert affected.binding_id in hint
    assert unrelated.binding_id not in hint
    assert "Use nonempty binding_assessment proposals" in hint

    relationship_context = json.dumps(
        {
            "invalid_stop_generation_authority": {
                "reason_code": "QUERY_REQUIREMENT_INCOMPLETE",
                "affected_source_ids": [filter_item.source_id],
            },
            "required_continuation": {
                "kind": "establish_required_relationship",
            },
        }
    )
    relationship_hint = review_hint(
        "relationship",
        '{"decision":"continue","hint":"Preserve exactly one new_join for the missing relationship."}',
        context=relationship_context,
    )
    assert relationship_hint is not None
    assert "Preserve exactly one new_join" in relationship_hint
    assert "binding_assessment" not in relationship_hint
    assert "semantic_commit" not in relationship_hint
    assert "do not create a replacement binding" not in relationship_hint

    assert review_hint(
        "confirmed",
        '{"decision":"stop_confirmed","hint":null}',
        hint,
    ) == hint

    missing_candidate_hint = review_hint(
        "confirmed-missing-candidate",
        '{"decision":"stop_confirmed","hint":null}',
        "Recheck the incomplete filter.",
    )
    assert missing_candidate_hint is not None
    assert affected.binding_id in missing_candidate_hint
    assert unrelated.binding_id not in missing_candidate_hint

    model_authored_json_hint = (
        "The previous reviewer mentioned "
        + json.dumps(
            [{"binding_id": affected.binding_id, "evidence_ids": []}],
            separators=(",", ":"),
        )
        + "."
    )
    canonicalized_hint = review_hint(
        "confirmed-model-json",
        '{"decision":"stop_confirmed","hint":null}',
        model_authored_json_hint,
    )
    assert canonicalized_hint is not None
    assert "Use nonempty binding_assessment proposals" in canonicalized_hint
    assert unrelated.binding_id not in canonicalized_hint

    model_authored_full_hint = (
        "Model-authored note: Use nonempty binding_assessment proposals, not new_binding, for these existing "
        "CANDIDATE bindings, citing only their listed durable evidence_ids: "
        + json.dumps(
            [{"binding_id": affected.binding_id, "evidence_ids": ["evidence:false"]}],
            separators=(",", ":"),
        )
        + ". Then semantic_commit with those assessments; never use an empty semantic_commit. "
        "do not create a replacement binding."
    )
    corrected_full_hint = review_hint(
        "confirmed-model-full",
        '{"decision":"stop_confirmed","hint":null}',
        model_authored_full_hint,
    )
    assert corrected_full_hint is not None
    assert (
        f'"binding_id":"{affected.binding_id}",'
        f'"evidence_ids":["{physical_binding.evidence_ids[0]}"]'
    ) in corrected_full_hint


def test_stop_review_hint_does_not_forward_unknown_semantic_source_id() -> None:
    _loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    semantic_item = state.query_spec.semantic_items[0].model_copy(
        update={"source_id": "semantic:durable.alpha:1-"}
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (semantic_item,)}
            )
        }
    )

    hint = _research_loop_module._validated_stop_review_hint(
        "Use semantic:mistyped.alpha:1- then semantic:durable.alpha:1-.", state
    )

    assert "semantic:mistyped.alpha:1-" not in hint
    assert "the exact durable source_id for the affected semantic item" in hint
    assert "semantic:durable.alpha:1-." in hint


def test_stop_review_uses_separate_model_and_forwards_hint(tmp_path) -> None:
    """Regression: decision and stop-review must use distinct providers."""

    loaded_schema, namespace = _fixture_schema()
    policy = _policy(8)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    decision_calls: list[str] = []
    review_calls: list[str] = []
    invalid_stop = (
        '{"decision_version":1,"proposals":[],"next":'
        '{"next_kind":"stop","reason":"ambiguous",'
        '"source_ids":["source-1"],"citation_evidence_ids":["citation-1"],'
        '"ambiguity":{"interpretations":["First reading.","Second reading."],'
        '"citation_evidence_ids":["citation-1"],'
        '"missing_distinguishing_fact":"The definition is absent."}}}'
    )

    async def decision_model(prompt: str) -> str:
        if '"review_kind":"research_stop_review"' in prompt:
            raise AssertionError("decision model received a stop-review prompt")
        decision_calls.append(prompt)
        if len(decision_calls) == 3:
            assert (
                "Inspect the visible relationship from the supported facts."
            ) in prompt
        return invalid_stop

    async def review_model(prompt: str) -> str:
        if '"review_kind":"research_stop_review"' not in prompt:
            raise AssertionError("review model received a decision prompt")
        review_calls.append(prompt)
        return (
            '{"decision":"continue","hint":'
            '"Inspect the visible relationship from the supported facts."}'
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            decision_model,
            stop_review_model=review_model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
            policy=policy,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert outcome.final_state.revision == 0
        assert len(decision_calls) == 3
        assert len(review_calls) == 1
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_stop_review_provider_failure_logs_safe_reason_without_retry(
    tmp_path, caplog
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(8)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    decision_calls = 0
    review_calls = 0
    invalid_stop = (
        '{"decision_version":1,"proposals":[],"next":'
        '{"next_kind":"stop","reason":"ambiguous",'
        '"source_ids":["source-1"],"citation_evidence_ids":["citation-1"],'
        '"ambiguity":{"interpretations":["First reading.","Second reading."],'
        '"citation_evidence_ids":["citation-1"],'
        '"missing_distinguishing_fact":"The definition is absent."}}}'
    )

    async def decision_model(_prompt: str) -> str:
        nonlocal decision_calls
        decision_calls += 1
        return invalid_stop

    async def review_model(_prompt: str) -> str:
        nonlocal review_calls
        review_calls += 1
        raise RuntimeError("stop-review provider detail must not be logged")

    with caplog.at_level(logging.WARNING, logger=_research_loop_module.__name__):
        outcome, state_store, checkpoint_store, ledger = asyncio.run(
            _run(
                tmp_path,
                state,
                decision_model,
                stop_review_model=review_model,
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=_make_registry(namespace),
                policy=policy,
            )
        )
    try:
        assert review_calls == 1
        diagnostics = [
            record.message
            for record in caplog.records
            if record.name == _research_loop_module.__name__
            and record.message.startswith("typed_schema_research_stop_review ")
        ]
        assert diagnostics == [
            "typed_schema_research_stop_review retry=false "
            "code=PROVIDER_OR_ADAPTER error_class=RuntimeError"
        ]
        assert "stop-review provider detail must not be logged" not in diagnostics[0]
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_stop_review_can_run_again_after_research_progress(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(8)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    registry = _make_registry(namespace)
    tool_decision = _tool_decision("inspect_table", {"table": "public.orders"})
    prepared = _resolve_fixture(
        tool_decision,
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=registry,
    )
    assert prepared.admission.action is not None
    assert prepared.invocation is not None
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id=prepared.invocation.invocation_id,
        action_digest=prepared.admission.action.action_digest,
        probe_kind=prepared.admission.action.kind,
        status=ProbeStatus.SUCCESS,
        target=prepared.admission.action.target,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="fixture success",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=11,
        ),
        row_count=1,
        payload={"ok": True},
    )
    registry.adapter.result = NormalizedToolResult(
        "success", result.model_dump(mode="json", by_alias=True)
    )
    registry.adapter.recover = lambda _invocation: None
    ledger = AdaptiveBudgetLedger(tmp_path / "repeated-stop-review-budget.sqlite")
    execute = _research_loop_module.execute_resolved_research_decision

    def execute_fresh_probe(resolved, tools, *, recover=False):
        observed = execute(resolved, tools, recover=recover)
        action = resolved.admission.action
        assert action is not None
        charged, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            observed.cost,
            lambda _reservation: observed,
            config=policy,
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "repeated-stop-review-tool-owner",
        )
        return charged

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", execute_fresh_probe
    )
    calls: list[str] = []
    invalid_stop = (
        '{"decision_version":1,"proposals":[],"next":'
        '{"next_kind":"stop","reason":"ambiguous",'
        '"source_ids":["source-1"],"citation_evidence_ids":["citation-1"],'
        '"ambiguity":{"interpretations":["First reading.","Second reading."],'
        '"citation_evidence_ids":["citation-1"],'
        '"missing_distinguishing_fact":"The definition is absent."}}}'
    )

    async def model(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 3:
            assert '"review_kind":"research_stop_review"' in prompt
            return '{"decision":"continue","hint":"Inspect the known relationship."}'
        if len(calls) == 4:
            return (
                '{"decision_version":1,"proposals":[],"next":'
                '{"next_kind":"tool","hypothesis_ref":null,"intent":'
                '{"tool_name":"inspect_table","arguments":'
                '{"table":"public.orders"}}}}'
            )
        if len(calls) == 7:
            assert '"review_kind":"research_stop_review"' in prompt
            return '{"decision":"stop_confirmed","hint":null}'
        return invalid_stop

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=registry,
            budget_ledger=ledger,
            policy=policy,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert outcome.final_state.revision == 1
        assert len(calls) == 7
        assert sum(
            '"review_kind":"research_stop_review"' in prompt for prompt in calls
        ) == 2
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize(
    ("revision", "restart", "review_call_id", "expected_model_calls", "expected_review_calls"),
    (
        (4, False, "research-stop-review-3-0", 1, 0),
        (4, True, "research-stop-review-3-0", 1, 0),
        (5, False, "research-stop-review-3-0", 0, 0),
        (4, False, "research-stop-review-3-0-extra", 0, 1),
    ),
)
def test_stop_review_follow_up_decision_is_single_use_and_durable(
    tmp_path,
    monkeypatch,
    revision,
    restart,
    review_call_id,
    expected_model_calls,
    expected_review_calls,
) -> None:
    """A reviewed non-novel turn allows one durable follow-up decision."""

    policy = _policy(8)
    initial = _state(required=True)
    action_history = tuple(
        ResearchAction(
            action_id=f"action-{action_revision}",
            kind=ResearchActionKind.SEMANTIC_COMMIT,
            hypothesis_id=None,
            target=None,
            parameters=(),
            action_digest=canonical_action_digest(
                kind=ResearchActionKind.SEMANTIC_COMMIT,
                hypothesis_id=None,
                target=None,
                parameters=(),
                expected_revision=action_revision,
            ),
            expected_revision=action_revision,
        )
        for action_revision in range(revision)
    )
    state = ResearchState.model_validate(
        {
            **initial.model_dump(mode="python", by_alias=True, round_trip=True),
            "revision": revision,
            "query_spec": initial.query_spec.model_copy(update={"revision": revision}),
            "action_history": action_history,
            "budget_state": initial_budget_state(policy),
        }
    )
    database = tmp_path / "non-novel-streak.sqlite"
    checkpoint_events = []
    for action_revision in range(revision):
        key = AdaptiveCheckpointKey(
            state.run_id,
            state.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            action_revision,
        )
        checkpoint_events.extend(
            (
                (
                    key,
                    "observed",
                    {
                        "contract_version": 1,
                        "kind": "research_observed",
                        "novel": action_revision == 0,
                        "result": None,
                        "resolution_digest": "sha256:" + "1" * 64,
                    },
                ),
            )
        )
    checkpoint_events.extend(
        (
            (
                AdaptiveCheckpointKey(
                    state.run_id,
                    state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    action_revision,
                ),
                "planned",
                {"kind": "seed"},
            )
            for action_revision in (revision - 1,)
        )
    )
    _seed_honest_v2_history(database, states=(state,), events=checkpoint_events)
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    monkeypatch.setattr(
        _research_loop_module._ResearchLoopCoordinator,
        "_stop",
        lambda _self, _state, reason, **_kwargs: SimpleNamespace(stop_reason=reason),
    )
    ledger = AdaptiveBudgetLedger(tmp_path / "non-novel-streak-budget.sqlite")

    async def reviewed(_reservation) -> ModelTokenUsage:
        return ModelTokenUsage(input_tokens=None, output_tokens=None)

    asyncio.run(
        execute_model_call_with_budget_async(
            state.run_id,
            state.run_incarnation,
            review_call_id,
            canonical_digest({"review": "continued"}),
            "test/model",
            10,
            10,
            reviewed,
            config=policy,
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "non-novel-streak-review-owner",
        )
    )
    model_calls = 0
    review_calls = 0

    async def allowed_decision(_self, _state, _feedback):
        nonlocal model_calls
        model_calls += 1
        assert _self._pending_stop_review_hint is None
        return None, ResearchStopReason.UNSUPPORTED, None

    async def review_model(_prompt: str) -> str:
        nonlocal review_calls
        review_calls += 1
        return '{"decision":"stop_confirmed","hint":null}'

    try:
        monkeypatch.setattr(
            _research_loop_module._ResearchLoopCoordinator,
            "_model_decision",
            allowed_decision,
        )
        if restart:
            state_store.close()
            checkpoint_store.close()
            ledger.close()
            state_store = AdaptiveResearchStateStore(database)
            checkpoint_store = AdaptiveStateStore(database)
            ledger = AdaptiveBudgetLedger(tmp_path / "non-novel-streak-budget.sqlite")
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks, *_args: canonical_digest(
                    current
                ),
                model=review_model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=object(),
                freshness_context=_freshness(state),
                registry=object(),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=policy,
                stop_review_model=review_model,
            )
        )
        assert model_calls == expected_model_calls
        assert outcome.stop_reason is (
            ResearchStopReason.UNSUPPORTED
            if expected_model_calls
            else ResearchStopReason.STAGNATED
        )
        assert review_calls == expected_review_calls
        assert sum(
            _research_loop_module._stop_review_call_revision(
                record.reservation.call_id
            )
            is not None
            for record in ledger.load_model_records(
                state.run_id, state.run_incarnation
            )
        ) == 1
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_stop_review_follow_up_consumes_persisted_non_novel_result(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(8).model_copy(
        update={
            "operation_counts": OperationCountBudget(
                actions=6,
                model_decisions=8,
                db_probes=6,
            )
        }
    )
    initial = _policy_state(namespace)
    action_history = tuple(
        ResearchAction(
            action_id=f"action-{revision}",
            kind=ResearchActionKind.SEMANTIC_COMMIT,
            hypothesis_id=None,
            target=None,
            parameters=(),
            action_digest=canonical_action_digest(
                kind=ResearchActionKind.SEMANTIC_COMMIT,
                hypothesis_id=None,
                target=None,
                parameters=(),
                expected_revision=revision,
            ),
            expected_revision=revision,
        )
        for revision in range(4)
    )
    state = ResearchState.model_validate(
        {
            **initial.model_dump(mode="python", by_alias=True, round_trip=True),
            "revision": 4,
            "query_spec": initial.query_spec.model_copy(update={"revision": 4}),
            "action_history": action_history,
            "budget_state": initial_budget_state(policy),
        }
    )
    database = tmp_path / "follow-up.sqlite"
    events = []
    for revision in range(4):
        key = AdaptiveCheckpointKey(
            state.run_id,
            state.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            revision,
        )
        events.append(
            (
                key,
                "observed",
                {
                    "contract_version": 1,
                    "kind": "research_observed",
                    "novel": revision == 0,
                    "result": None,
                    "resolution_digest": "sha256:" + "1" * 64,
                },
            )
        )
    events.append((key, "planned", {"kind": "seed"}))
    _seed_honest_v2_history(database, states=(state,), events=events)
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    ledger = AdaptiveBudgetLedger(tmp_path / "follow-up-budget.sqlite")

    async def recorded_model_usage(_reservation) -> ModelTokenUsage:
        return ModelTokenUsage(input_tokens=None, output_tokens=None)

    for revision in range(4):
        asyncio.run(
            execute_model_call_with_budget_async(
                state.run_id,
                state.run_incarnation,
                f"research-model-{revision}-0",
                canonical_digest({"revision": revision}),
                "test/model",
                10,
                10,
                recorded_model_usage,
                config=policy,
                ledger=ledger,
                claim_now_ns=lambda: 1,
                owner_token_factory=lambda: f"prior-model-{revision}",
            )
        )
    registry = _make_registry(namespace)
    tables = tuple(loaded_schema.schema)
    actions: list[ResearchAction] = []

    def execute_non_novel(resolved, _tools, *, recover=False):
        assert recover is False
        action = resolved.admission.action
        invocation = resolved.invocation
        assert action is not None and invocation is not None
        actions.append(action)
        cost = EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=2,
        )
        result = build_probe_result(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            revision=action.expected_revision,
            schema_namespace_version=state.schema_namespace_version,
            invocation_id=invocation.invocation_id,
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=action.target,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="repeated observation",
            cost=cost,
            row_count=0,
            payload={},
        )
        charged, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            cost,
            lambda _reservation: result,
            config=policy,
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: action.expected_revision + 1,
            owner_token_factory=lambda: f"follow-up-{action.expected_revision}",
        )
        return charged

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", execute_non_novel
    )
    monkeypatch.setattr(
        _research_loop_module._ResearchLoopCoordinator,
        "_preflight_model_decision",
        lambda _self, _state, _decision: (None, None, None, ()),
    )
    model_prompts: list[str] = []
    model_checks: list[tuple[bool, int]] = []
    review_prompts: list[str] = []

    async def model(prompt: str) -> str:
        model_prompts.append(prompt)
        if len(model_prompts) == 1:
            model_checks.append(
                ("Use the persisted observation." in prompt, len(actions))
            )
        elif len(model_prompts) == 2:
            model_checks.append(
                ("Use the persisted observation." not in prompt, len(actions))
            )
        else:
            raise AssertionError("a second follow-up decision is not allowed")
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_table",
                        "arguments": {
                            "table": tables[len(model_prompts) - 1],
                        },
                    },
                },
            }
        )

    async def review_model(prompt: str) -> str:
        review_prompts.append(prompt)
        return '{"decision":"continue","hint":"Use the persisted observation."}'

    try:
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks, *_args: json.dumps(
                    current.model_dump(mode="json"), sort_keys=True
                ),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=policy,
                stop_review_model=review_model,
            )
        )
        assert model_checks == [(True, 0), (True, 1)]
        assert checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                4,
            )
        ).observed is not None
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert outcome.final_state.revision == 6
        assert len(actions) == 2
        assert len(model_prompts) == 2
        assert len(review_prompts) == 1
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_stop_review_follow_up_does_not_reuse_hint_after_irrelevant_sample(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(4)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    ledger = AdaptiveBudgetLedger(tmp_path / "prior-hint-budget.sqlite")
    sample_actions: list[ResearchAction] = []

    def execute_sample(resolved, _tools, *, recover=False):
        assert recover is False
        action = resolved.admission.action
        invocation = resolved.invocation
        assert action is not None and invocation is not None
        assert action.kind is ResearchActionKind.SAMPLE_ROWS
        sample_actions.append(action)
        cost = EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes({"sample": "one input"})),
        )
        result = build_probe_result(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            revision=action.expected_revision,
            schema_namespace_version=state.schema_namespace_version,
            invocation_id=invocation.invocation_id,
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=action.target,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="one-input sample",
            cost=cost,
            row_count=1,
            payload={"sample": "one input"},
        )
        charged, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            cost,
            lambda _reservation: result,
            config=policy,
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "prior-hint-sample-owner",
        )
        return charged

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", execute_sample
    )
    monkeypatch.setattr(
        _research_loop_module, "_consecutive_non_novel", lambda _store, _state: 2
    )
    decision_calls = 0
    decision_prompts: list[str] = []
    review_contexts: list[dict[str, object]] = []

    async def decision_model(prompt: str) -> str:
        nonlocal decision_calls
        decision_calls += 1
        decision_prompts.append(prompt)
        if decision_calls == 1:
            return (
                '{"decision_version":1,"proposals":[],"next":'
                '{"next_kind":"tool","hypothesis_ref":null,"intent":'
                '{"tool_name":"sample_rows","arguments":'
                '{"table":"public.orders","columns":["id","status"],"limit":1}}}}'
            )
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"stop","reason":"ambiguous",'
            '"source_ids":["source-1"],"citation_evidence_ids":'
            '["citation-1"],"ambiguity":{"interpretations":'
            '["First reading.","Second reading."],"citation_evidence_ids":'
            '["citation-1"],"missing_distinguishing_fact":'
            '"The definition is absent."}}}'
        )

    async def review_model(prompt: str) -> str:
        context = json.loads(json.loads(prompt)["input"]["research_context"])
        review_contexts.append(context)
        assert len(review_contexts) == 1
        return (
            '{"decision":"continue","hint":'
            '"Apply the confirmed condition through the validated relationship."}'
        )

    try:
        outcome, state_store, checkpoint_store, ledger = asyncio.run(
            _run(
                tmp_path,
                state,
                decision_model,
                task="Return the ratio for qualifying detail rows.",
                research_context=lambda current, _feedbacks, *_args: json.dumps(
                    {
                        "completed_action_index": [
                            {"kind": action.kind.value}
                            for action in current.action_history
                        ],
                        "evidence": [
                            evidence.model_dump(mode="json", by_alias=True)
                            for evidence in current.evidence
                        ],
                        "semantic_requirements": {
                            "formula": "ratio over qualifying detail rows",
                            "condition": "qualifying detail rows",
                            "validated_relationship": "orders to details",
                        },
                    }
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=_make_registry(namespace),
                budget_ledger=ledger,
                policy=policy,
                stop_review_model=review_model,
            )
        )

        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert decision_calls >= 2
        assert (
            "Apply the confirmed condition through the validated relationship."
            in decision_prompts[0]
        )
        assert all(
            "Apply the confirmed condition through the validated relationship."
            not in prompt
            for prompt in decision_prompts[1:]
        )
        assert len(review_contexts) == 1
        assert len(sample_actions) == 1
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_boundary_stop_review_receives_generation_authority(
    tmp_path, monkeypatch
) -> None:
    state = _state(required=True)
    contexts: list[object] = []

    def research_context(
        _state,
        _feedbacks,
        _rejected=(),
        _rejected_preflight=(),
        generation_authority=None,
    ) -> str:
        contexts.append(generation_authority)
        return canonical_digest(generation_authority)

    async def decision_model(_prompt: str) -> str:
        raise AssertionError("boundary review must run before another research turn")

    async def review_model(_prompt: str) -> str:
        return '{"decision":"stop_confirmed","hint":null}'

    monkeypatch.setattr(
        _research_loop_module, "_consecutive_non_novel", lambda _store, _state: 2
    )
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            decision_model,
            research_context=research_context,
            stop_review_model=review_model,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert contexts == [
            (CoverageInputErrorCode.RESEARCH_STATE_INCOMPLETE, ("source-1",))
        ]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_duplicate_action_is_retried_then_a_different_action_executes(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, namespace = _fixture_schema()
    policy = _policy(3)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    ledger = AdaptiveBudgetLedger(tmp_path / "duplicate-then-different-budget.sqlite")
    executed: list[ResearchActionKind] = []
    feedbacks: list[tuple[str, ...]] = []

    def execute_once(resolved, _tools, *, recover=False):
        assert recover is False
        action = resolved.admission.action
        invocation = resolved.invocation
        assert action is not None and invocation is not None
        executed.append(action.kind)
        cost = EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes({"different": True})),
        )
        result = build_probe_result(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            revision=action.expected_revision,
            schema_namespace_version=state.schema_namespace_version,
            invocation_id=invocation.invocation_id,
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=action.target,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="different semantic observation",
            cost=cost,
            row_count=1,
            payload={"different": True},
        )
        result, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            cost,
            lambda _reservation: result,
            config=policy,
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: (
                f"duplicate-then-different-tool-{action.expected_revision}"
            ),
        )
        return result

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", execute_once
    )
    monkeypatch.setattr(
        _research_loop_module, "_consecutive_non_novel", lambda _store, _state: 0
    )
    prompts: list[dict[str, object]] = []

    async def model(prompt: str) -> str:
        prompts.append(json.loads(prompt))
        rejected = json.loads(prompts[-1]["input"]["research_context"]).get(
            "rejected_duplicate_actions"
        )
        if rejected:
            return (
                '{"decision_version":1,"proposals":[],"next":'
                '{"next_kind":"tool","hypothesis_ref":null,"intent":'
                '{"tool_name":"inspect_relationships","arguments":'
                '{"table":"public.orders","top_k":1,"depth":1}}}}'
            )
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
            budget_ledger=ledger,
            policy=policy,
            research_context=lambda current, current_feedbacks, rejected=(), *_args: (
                feedbacks.append(current_feedbacks)
                or json.dumps(
                    {
                        "state": canonical_digest(current),
                        "rejected_duplicate_actions": list(rejected),
                    }
                )
            ),
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.BUDGET_EXHAUSTED
        assert executed == [
            ResearchActionKind.INSPECT_TABLE,
            ResearchActionKind.INSPECT_RELATIONSHIPS,
        ]
        assert feedbacks == [(), (), ("DUPLICATE_ACTION",), ()]
        rejected = json.loads(prompts[2]["input"]["research_context"])[
            "rejected_duplicate_actions"
        ]
        first = outcome.final_state.action_history[0]
        assert rejected == [
            {
                "action_digest": first.action_digest,
                "kind": first.kind,
                "target": first.target.model_dump(mode="json", by_alias=True),
                "parameters": [list(item) for item in first.parameters],
            }
        ]
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_duplicate_tool_commits_valid_join_proposals_without_repeating_tool(
    tmp_path,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _policy_state(namespace, with_evidence=True)
    citation = state.evidence[0].evidence_id
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        tmp_path / "adaptive.sqlite",
        states=(initial, state),
        events=((seed, "planned", {"kind": "seed"}), (seed, "observed", {"kind": "seed"})),
    )
    ledger = AdaptiveBudgetLedger(tmp_path / "duplicate-tool-proposals-budget.sqlite")
    _seed_prior_model_budget(state, ledger)
    registry = _make_registry(namespace)
    responses = iter(
        (
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [
                        {
                            "proposal_type": "new_join",
                            "proposal_key": "proposal:related-join",
                            "left": {"table": "public.orders", "column": "status"},
                            "right": {"table": "public.customers", "column": "id"},
                            "join_type": "inner",
                            "path": [],
                            "citation_evidence_ids": [citation],
                        }
                    ],
                    "next": {
                        "next_kind": "tool",
                        "hypothesis_ref": None,
                        "intent": {
                            "tool_name": "inspect_table",
                            "arguments": {"table": "public.orders"},
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [citation],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [citation],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            ),
        )
    )
    prompts: list[dict[str, object]] = []

    async def model(prompt: str) -> str:
        prompts.append(json.loads(prompt))
        return next(responses)

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=registry,
            budget_ledger=ledger,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert len(outcome.final_state.join_candidates) == 1
        assert outcome.final_state.action_history[-1].kind is ResearchActionKind.SEMANTIC_COMMIT
        assert len(prompts) == 2
        assert registry.adapter.execute_calls == 0
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize(
    "failure_message",
    ("typed tool failure", "exact prior invocation was not recovered"),
)
def test_tool_failure_is_durably_aborted_and_terminal_replays(
    tmp_path, monkeypatch, failure_message: str
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    calls = 0

    def fail_tool(_resolved, _tools, *, recover=False):
        nonlocal calls
        assert recover is False
        calls += 1
        raise _research_loop_module.DecisionExecutionError(failure_message)

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", fail_tool
    )

    async def model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=registry,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.TOOL_FAILURE
        assert calls == 1
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
            )
        )
        assert snapshot.observed is not None
        assert snapshot.observed.action["kind"] == "research_aborted"
        assert (
            snapshot.observed.action["reason"] == ResearchStopReason.TOOL_FAILURE.value
        )
        assert snapshot.terminal is not None

        async def replay_model(_prompt: str) -> str:
            raise AssertionError("terminal replay must not call the model")

        replay, replay_state, replay_checkpoint, replay_ledger = asyncio.run(
            _run(
                tmp_path,
                state,
                replay_model,
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                budget_ledger=ledger,
            )
        )
        try:
            assert replay == outcome
            assert calls == 1
        finally:
            replay_state.close()
            replay_checkpoint.close()
            replay_ledger.close()
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_unrecovered_planned_turn_uses_recovery_once_and_is_terminal(tmp_path) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    prepared = _resolve_fixture(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=registry,
    )
    state_store = AdaptiveResearchStateStore(tmp_path / "planned-recovery.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "planned-recovery.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "planned-recovery-budget.sqlite")
    key = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )

    async def no_model(_prompt: str) -> str:
        raise AssertionError("planned replay must not ask the model")

    try:
        state_store.save_research_state(state, expected_previous_revision=None)
        checkpoint_store.record_planned(
            key,
            expected_revision=None,
            action=_research_loop_module._planned_action(prepared),
        )
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=no_model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert outcome.stop_reason is ResearchStopReason.TOOL_FAILURE
        assert registry.adapter.execute_calls == 0
        assert registry.adapter.recover_calls == 1
        snapshot = checkpoint_store.get_snapshot(key)
        assert snapshot.observed is not None
        assert snapshot.observed.action["kind"] == "research_aborted"
        assert snapshot.terminal is not None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        (ProbeStatus.TIMED_OUT, ResearchStopReason.DEADLINE_EXCEEDED),
        (ProbeStatus.CANCELLED, ResearchStopReason.CANCELLED),
    ],
)
def test_non_success_probe_is_observed_once_without_semantic_transition(
    tmp_path, monkeypatch, status: ProbeStatus, expected_reason: ResearchStopReason
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    ledger = AdaptiveBudgetLedger(tmp_path / "non-success-budget.sqlite")
    tool_calls = 0

    def failed_probe(resolved, _tools, *, recover=False):
        nonlocal tool_calls
        assert recover is False
        tool_calls += 1
        action = resolved.admission.action
        invocation = resolved.invocation
        assert action is not None and invocation is not None
        maximum_cost = EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=0,
            bytes=0,
        )
        result = build_probe_result(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            revision=action.expected_revision,
            schema_namespace_version=state.schema_namespace_version,
            invocation_id=invocation.invocation_id,
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=status,
            target=action.target,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="typed unsuccessful probe",
            cost=maximum_cost,
            row_count=0,
            failure_code=status.value,
        )
        result, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            maximum_cost,
            lambda _reservation: result,
            config=_policy(),
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "non-success-tool-owner",
        )
        return result

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", failed_probe
    )

    async def model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
        )

    outcome, state_store, checkpoint_store, returned_ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=registry,
            budget_ledger=ledger,
        )
    )
    try:
        assert returned_ledger is ledger
        assert outcome.stop_reason is expected_reason
        assert outcome.final_state.revision == state.revision
        assert outcome.final_state.action_history == state.action_history
        assert outcome.final_state.evidence == state.evidence
        assert outcome.final_state.bindings == state.bindings
        assert outcome.final_state.join_candidates == state.join_candidates
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
            )
        )
        assert snapshot.observed is not None
        assert snapshot.observed.action["kind"] == "research_observed"
        assert snapshot.observed.action["novel"] is False
        assert snapshot.observed.action["result"]["status"] == status.value
        assert snapshot.terminal is not None

        async def replay_model(_prompt: str) -> str:
            raise AssertionError("durable unsuccessful result must not call model")

        replay, replay_state, replay_checkpoint, replay_ledger = asyncio.run(
            _run(
                tmp_path,
                state,
                replay_model,
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                budget_ledger=ledger,
            )
        )
        try:
            assert replay == outcome
            assert tool_calls == 1
        finally:
            replay_state.close()
            replay_checkpoint.close()
            replay_ledger.close()
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_failed_probe_commits_then_recovery_uses_generic_feedback(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    budget_path = tmp_path / "failed-probe-recovery-budget.sqlite"
    ledger = AdaptiveBudgetLedger(budget_path)
    tool_calls = 0

    def failed_probe(resolved, _tools, *, recover=False):
        nonlocal tool_calls
        assert recover is False
        tool_calls += 1
        action = resolved.admission.action
        invocation = resolved.invocation
        assert action is not None and invocation is not None
        is_failed_action = tool_calls == 1
        maximum_cost = EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=0 if is_failed_action else 1,
            bytes=0 if is_failed_action else 11,
        )
        result_kwargs: dict[str, object] = {
            "failure_code": "synthetic_failure" if is_failed_action else None,
        }
        if not is_failed_action:
            result_kwargs["payload"] = {"ok": True}
        result = build_probe_result(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            revision=action.expected_revision,
            schema_namespace_version=state.schema_namespace_version,
            invocation_id=invocation.invocation_id,
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.FAILED if is_failed_action else ProbeStatus.SUCCESS,
            target=action.target,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="synthetic failed probe" if is_failed_action else "recovered probe",
            cost=maximum_cost,
            row_count=0 if is_failed_action else 1,
            **result_kwargs,
        )
        result, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            maximum_cost,
            lambda _reservation: result,
            config=_policy(),
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "failed-probe-owner",
        )
        return result

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", failed_probe
    )
    monkeypatch.setattr(
        _research_loop_module, "_consecutive_non_novel", lambda _store, _state: 0
    )
    first_model_calls = 0

    async def interrupted_model(_prompt: str) -> str:
        nonlocal first_model_calls
        first_model_calls += 1
        if first_model_calls == 1:
            return (
                '{"decision_version":1,"proposals":[],"next":'
                '{"next_kind":"tool","hypothesis_ref":null,"intent":'
                '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
            )
        raise KeyboardInterrupt("simulate restart after durable FAILED commit")

    with pytest.raises(KeyboardInterrupt, match="durable FAILED commit"):
        asyncio.run(
            _run(
                tmp_path,
                state,
                interrupted_model,
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                budget_ledger=ledger,
            )
        )

    first_ledger = AdaptiveBudgetLedger(budget_path)
    first_records = first_ledger.load_records(state.run_id, state.run_incarnation)
    first_ledger.close()
    assert len(first_records) == 1
    assert first_records[0].reservation.revision == 0
    assert first_records[0].reconciliation is not None

    ledger = AdaptiveBudgetLedger(budget_path)
    resumed_prompts: list[str] = []

    async def resumed_model(prompt: str) -> str:
        resumed_prompts.append(prompt)
        if len(resumed_prompts) == 1:
            return (
                '{"decision_version":1,"proposals":[],"next":'
                '{"next_kind":"tool","hypothesis_ref":null,"intent":'
                '{"tool_name":"inspect_table","arguments":'
                '{"table":"public.customers"}}}}'
            )
        raise asyncio.CancelledError()

    outcome, state_store, checkpoint_store, returned_ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            resumed_model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=_make_registry(namespace),
                budget_ledger=ledger,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.CANCELLED
        assert outcome.final_state.revision == 2
        assert len(outcome.final_state.action_history) == 2
        assert len(outcome.final_state.evidence) == 1
        assert tool_calls == 2
        assert first_model_calls == 2
        assert len(resumed_prompts) == 2
        assert "PROBE_UNAVAILABLE" in resumed_prompts[0]
        assert "synthetic failed probe" not in resumed_prompts[0]
        assert "synthetic_failure" not in resumed_prompts[0]
        records = returned_ledger.load_records(state.run_id, state.run_incarnation)
        assert len(records) == 2
        assert records[0] == first_records[0]
        assert [record.reservation.revision for record in records] == [0, 1]
        assert all(record.reconciliation is not None for record in records)
        replay_input = state_store.load_research_replay_input(
            state.run_id, state.run_incarnation, 1
        )
        assert replay_input is not None
        assert replay_input.probe_result.status is ProbeStatus.FAILED
        snapshot = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
            )
        )
        assert snapshot.observed is not None
        assert snapshot.observed.action["novel"] is False
    finally:
        state_store.close()
        checkpoint_store.close()
        returned_ledger.close()


@pytest.mark.parametrize("mutation", ("coercion", "extra", "timestamp", "oversized"))
def test_observed_replay_mutations_fail_closed(tmp_path, mutation: str) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    prepared = _resolve_fixture(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=_make_registry(namespace),
    )
    assert prepared.admission.action is not None
    assert prepared.invocation is not None
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id=prepared.invocation.invocation_id,
        action_digest=prepared.admission.action.action_digest,
        probe_kind=prepared.admission.action.kind,
        status=ProbeStatus.SUCCESS,
        target=prepared.admission.action.target,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="strict replay fixture",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=11,
        ),
        row_count=1,
        payload={"ok": True},
    )
    observed = {
        "contract_version": 1,
        "kind": "research_observed",
        "novel": True,
        "result": result.model_dump(mode="json", by_alias=True),
        "resolution_digest": prepared.resolution_digest,
    }
    mutated = json.loads(json.dumps(observed))
    if mutation == "coercion":
        mutated["contract_version"] = "1"
    elif mutation == "extra":
        mutated["extra"] = True
    elif mutation == "timestamp":
        mutated["result"]["started_at"] = "not-a-timestamp"
    else:
        mutated["result"]["inline_payload_json"] = "x" * (3 * 1024 * 1024)
    assert _probe_from_observed(mutated) is None
    state_store = AdaptiveResearchStateStore(tmp_path / "mutation.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "mutation.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "mutation-budget.sqlite")
    replay_registry = _make_registry(namespace)
    model_calls = 0

    async def model(_prompt: str) -> str:
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("corrupt replay must not ask the model")

    try:
        state_store.save_research_state(state, expected_previous_revision=None)
        key = AdaptiveCheckpointKey(
            state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
        )
        checkpoint_store.record_planned(
            key,
            expected_revision=None,
            action=_research_loop_module._planned_action(prepared),
        )
        checkpoint_store.record_observed(key, expected_revision=0, action=mutated)
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=replay_registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        assert model_calls == 0
        assert replay_registry.adapter.execute_calls == 0
        assert replay_registry.adapter.recover_calls == 0
        assert checkpoint_store.get_snapshot(key).terminal is not None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_malformed_non_success_observed_result_stops_without_model_or_tool(
    tmp_path,
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    prepared = _resolve_fixture(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=_make_registry(namespace),
    )
    assert prepared.admission.action is not None
    assert prepared.invocation is not None
    state_store = AdaptiveResearchStateStore(tmp_path / "malformed.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "malformed.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "malformed-budget.sqlite")
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id=prepared.invocation.invocation_id,
        action_digest=prepared.admission.action.action_digest,
        probe_kind=prepared.admission.action.kind,
        status=ProbeStatus.FAILED,
        target=prepared.admission.action.target,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="failed probe",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=0,
        ),
        row_count=0,
        failure_code="failed",
    )
    try:
        state_store.save_research_state(state, expected_previous_revision=None)
        key = AdaptiveCheckpointKey(
            state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
        )
        checkpoint_store.record_planned(
            key,
            expected_revision=None,
            action=_research_loop_module._planned_action(prepared),
        )
        checkpoint_store.record_observed(
            key,
            expected_revision=0,
            action={
                "contract_version": 1,
                "kind": "research_observed",
                "novel": "false",
                "result": result.model_dump(mode="json", by_alias=True),
                "resolution_digest": prepared.resolution_digest,
            },
        )

        async def no_model(_prompt: str) -> str:
            raise AssertionError("malformed observed result must not call model")

        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=no_model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=_make_registry(namespace),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert outcome.stop_reason is ResearchStopReason.PROTOCOL_FAILURE
        assert outcome.final_state == state
        assert checkpoint_store.get_snapshot(key).terminal is not None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_task_cancellation_after_durable_state_uses_latest_persisted_state(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    ledger = AdaptiveBudgetLedger(tmp_path / "outer-cancel-budget.sqlite")
    state_store = AdaptiveResearchStateStore(tmp_path / "outer-cancel.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "outer-cancel.sqlite")
    task_holder: dict[str, asyncio.Task[object]] = {}
    original_save = state_store.save_replayable_semantic_transition

    def successful_probe(resolved, _tools, *, recover=False):
        assert recover is False
        action = resolved.admission.action
        invocation = resolved.invocation
        assert action is not None and invocation is not None
        cost = EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=11,
        )
        result = build_probe_result(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            revision=action.expected_revision,
            schema_namespace_version=state.schema_namespace_version,
            invocation_id=invocation.invocation_id,
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=action.target,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="one successful probe before task cancellation",
            cost=cost,
            row_count=1,
            payload={"ok": True},
        )
        result, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            cost,
            lambda _reservation: result,
            config=_policy(),
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "outer-cancel-tool-owner",
        )
        return result

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", successful_probe
    )

    def cancel_after_durable_save(previous, saved_state, replay_input):
        stored = original_save(previous, saved_state, replay_input)
        if saved_state.revision == 1:
            task_holder["task"].cancel()
        return stored

    state_store.save_replayable_semantic_transition = cancel_after_durable_save

    async def model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
        )

    async def scenario():
        task = asyncio.create_task(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        task_holder["task"] = task
        return await task

    try:
        outcome = asyncio.run(scenario())
        persisted = state_store.load_latest_research_state(
            state.run_id, state.run_incarnation
        )
        assert persisted is not None
        assert persisted.revision == 1
        assert outcome.stop_reason is ResearchStopReason.CANCELLED
        assert outcome.final_state.revision == 1
        assert outcome.final_state.action_history == persisted.action_history
        assert outcome.final_state.evidence == persisted.evidence
        assert outcome.final_state.bindings == persisted.bindings
        assert outcome.final_state.join_candidates == persisted.join_candidates
        assert (
            outcome.final_state.budget_state.used_model_calls
            >= persisted.budget_state.used_model_calls
        )
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 1
                )
            ).terminal
            is not None
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_pre_cancel_returns_closed_partial_state_without_model_call(tmp_path) -> None:
    called = False

    async def model(_prompt: str) -> str:
        nonlocal called
        called = True
        return "{}"

    state = _state(required=True)
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, state, model, is_cancelled=lambda: True)
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.CANCELLED
        assert outcome.final_state == state
        assert called is False
        assert ledger.load_model_records(state.run_id, state.run_incarnation) == ()
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
                )
            ).terminal
            is not None
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize("_repeat", range(20))
def test_expired_deadline_stops_before_model_or_tool(tmp_path, _repeat: int) -> None:
    called = False

    async def model(_prompt: str) -> str:
        nonlocal called
        called = True
        return "{}"

    state = _state(required=True)
    deadline = DeadlineBudget(
        deadline_monotonic=0.0,
        deadline_at_ms=0,
        monotonic=lambda: 0.0,
    )
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, state, model, deadline=deadline)
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.DEADLINE_EXCEEDED
        assert outcome.final_state == state
        assert called is False
        assert ledger.load_model_records(state.run_id, state.run_incarnation) == ()
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
                )
            ).terminal
            is not None
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_deadline_after_completed_model_call_projects_terminal_budget(tmp_path) -> None:
    clock_values = iter((0.0, 0.0, 0.0, 0.0, 1.0))
    deadline = DeadlineBudget(
        deadline_monotonic=0.5,
        deadline_at_ms=500,
        monotonic=lambda: next(clock_values),
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
        )

    state = _state(required=True)
    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, state, model, deadline=deadline)
    )
    try:
        assert calls == 1
        assert outcome.stop_reason is ResearchStopReason.DEADLINE_EXCEEDED
        assert outcome.final_state.revision == 0
        assert outcome.final_state.budget_state.used_model_calls == 1
        assert outcome.final_state.budget_state.used_model_tokens == 20
        terminal = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
            )
        ).terminal
        assert terminal is not None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_inflight_model_call_stops_at_deadline_and_settles_conservatively(
    tmp_path,
) -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()
    clock = [0.0]
    deadline = DeadlineBudget(
        deadline_monotonic=0.05,
        deadline_at_ms=50,
        monotonic=lambda: clock[0],
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        started.set()
        clock[0] = 0.05
        await blocker.wait()
        raise AssertionError("blocked model call must be cancelled at the deadline")

    async def scenario():
        task = asyncio.create_task(
            _run(tmp_path, _state(required=True), model, deadline=deadline)
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        return await asyncio.wait_for(task, timeout=0.5)

    outcome, state_store, checkpoint_store, ledger = asyncio.run(scenario())
    try:
        records = ledger.load_model_records("loop-run", "loop-incarnation")
        assert calls == 1
        assert outcome.stop_reason is ResearchStopReason.DEADLINE_EXCEEDED
        assert outcome.final_state.revision == 0
        assert len(records) == 1
        assert records[0].reconciliation is not None
        assert records[0].reconciliation.usage_was_conservative is True
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    outcome.final_state.run_id,
                    outcome.final_state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    0,
                )
            ).terminal
            is not None
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_terminal_checkpoint_is_idempotent_on_reentry(tmp_path) -> None:
    async def model(_prompt: str) -> str:
        raise AssertionError("complete state must not call the model")

    state = _state(required=False)
    first, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, state, model)
    )
    state_store.close()
    checkpoint_store.close()
    ledger.close()
    second, state_store, checkpoint_store, ledger = asyncio.run(
        _run(tmp_path, state, model)
    )
    try:
        assert first == second
        assert second.stop_reason is ResearchStopReason.COMPLETE
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_foreign_incarnation_snapshot_is_ignored(tmp_path) -> None:
    state = _state(required=False)
    foreign_query = state.query_spec.model_copy(update={"run_incarnation": "foreign"})
    foreign = state.model_copy(
        update={"run_incarnation": "foreign", "query_spec": foreign_query}
    )
    state_store = AdaptiveResearchStateStore(tmp_path / "foreign.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "foreign.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "foreign-budget.sqlite")

    async def model(_prompt: str) -> str:
        raise AssertionError("complete state must not call the model")

    try:
        state_store.save_research_state(foreign, expected_previous_revision=None)
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=object(),
                freshness_context=_freshness(state),
                registry=object(),
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert outcome.final_state.run_incarnation == state.run_incarnation
        assert state_store.load_latest_research_state("loop-run", "foreign") == foreign
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_planned_replay_identity_ignores_transient_resolution_digest() -> None:
    planned = {
        "action": {"action_id": "action-1"},
        "decision": {"decision_version": 1},
        "invocation_id": "invocation-1",
        "state_digest": "sha256:" + "a" * 64,
        "resolution_digest": "sha256:" + "b" * 64,
    }
    replayed = {**planned, "resolution_digest": "sha256:" + "c" * 64}

    assert _stable_planned_identity(planned) == _stable_planned_identity(replayed)


def test_expired_model_started_lease_settles_without_recalling_provider(
    tmp_path,
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "takeover.sqlite")
    try:
        request_digest = canonical_digest({"request": "takeover"})
        reservation = reserve_model_call_budget(
            "takeover-run",
            "takeover-incarnation",
            "call-0",
            request_digest,
            "test/model",
            10,
            10,
            config=_policy(),
            ledger=ledger,
        )
        claim = ledger.claim_model_execution(reservation, "dead-owner", now_ns=0)
        started_values = {
            "reservation": reservation,
            "invocation_id": "model-started",
            "claim_generation": claim.generation,
            "started_at_ns": 0,
        }
        started = ModelCallStarted(
            **started_values, started_digest=canonical_digest(started_values)
        )
        ledger.record_model_started(started, owner_token="dead-owner")
        calls = 0

        async def provider(_reservation) -> ModelTokenUsage:
            nonlocal calls
            calls += 1
            return ModelTokenUsage(input_tokens=1, output_tokens=1)

        reconciliation = asyncio.run(
            execute_model_call_with_budget_async(
                reservation.run_id,
                reservation.run_incarnation,
                reservation.call_id,
                request_digest,
                "test/model",
                10,
                10,
                provider,
                config=_policy(),
                ledger=ledger,
                claim_now_ns=lambda: EXECUTION_CLAIM_LEASE_NS + 1,
                owner_token_factory=lambda: "takeover-owner",
            )
        )
        assert calls == 0
        assert reconciliation.usage_was_conservative is True
        record = ledger.load_model_records("takeover-run", "takeover-incarnation")[0]
        assert record.result is not None
        assert record.result.usage.input_tokens is None
    finally:
        ledger.close()


def test_tool_turn_is_planned_observed_committed_once_then_stops(
    tmp_path, monkeypatch
) -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    decision = _tool_decision("inspect_table", {"table": "public.orders"})
    prepared = _resolve_fixture(
        decision,
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=registry,
    )
    assert prepared.admission.action is not None
    assert prepared.invocation is not None
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id=prepared.invocation.invocation_id,
        action_digest=prepared.admission.action.action_digest,
        probe_kind=prepared.admission.action.kind,
        status=ProbeStatus.SUCCESS,
        target=prepared.admission.action.target,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="fixture success",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=11,
        ),
        row_count=1,
        payload={"ok": True},
    )
    registry.adapter.result = NormalizedToolResult(
        "success", result.model_dump(mode="json", by_alias=True)
    )
    registry.adapter.recover = lambda _invocation: None
    responses = iter(
        (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}',
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}',
            json.dumps(
                {
                    "decision_version": 1,
                    "proposals": [],
                    "next": {
                        "next_kind": "stop",
                        "reason": "ambiguous",
                        "source_ids": ["source-1"],
                        "citation_evidence_ids": [prepared.invocation.invocation_id],
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [prepared.invocation.invocation_id],
                            "missing_distinguishing_fact": "The definition is absent.",
                        },
                    },
                }
            ),
        )
    )

    async def model(_prompt: str) -> str:
        return next(responses)

    state_store = AdaptiveResearchStateStore(tmp_path / "tool.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "tool.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "tool-budget.sqlite")
    execute = _research_loop_module.execute_resolved_research_decision

    def execute_fresh_probe(resolved, tools, *, recover=False):
        assert recover is False
        observed = execute(resolved, tools, recover=recover)
        action = resolved.admission.action
        assert action is not None
        assert isinstance(observed, type(result))
        charged, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            observed.cost,
            lambda _reservation: observed,
            config=_policy(),
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "tool-budget-owner",
        )
        return charged

    monkeypatch.setattr(
        _research_loop_module, "execute_resolved_research_decision", execute_fresh_probe
    )
    try:
        durable_planned = checkpoint_store.record_planned
        crashed = False

        def crash_before_planned(*args, **kwargs):
            nonlocal crashed
            if not crashed:
                crashed = True
                raise RuntimeError("crash before durable planned")
            return durable_planned(*args, **kwargs)

        checkpoint_store.record_planned = crash_before_planned
        with pytest.raises(RuntimeError, match="before durable planned"):
            asyncio.run(
                run_research_loop(
                    initial_state=state,
                    task="research schema",
                    research_context=lambda current, _feedbacks: canonical_digest(current),
                    model=model,
                    model_identity="test/model",
                    adapter=SchemaResearchDecisionAdapter(
                        load_schema_research_agent_profile()
                    ),
                    loaded_schema=loaded_schema,
                    freshness_context=_fixture_freshness(state),
                    registry=registry,
                    state_store=state_store,
                    checkpoint_store=checkpoint_store,
                    budget_ledger=ledger,
                    policy=_policy(),
                )
            )
        assert (
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    state.run_id,
                    state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    0,
                )
            ).planned
            is None
        )
        assert [
            record.reservation.call_id
            for record in ledger.load_model_records(state.run_id, state.run_incarnation)
        ] == ["research-model-0-0"]
        checkpoint_store.record_planned = durable_planned
        durable_observed = checkpoint_store.record_observed
        crashed_after_observed = False

        def crash_after_observed(*args, **kwargs):
            nonlocal crashed_after_observed
            event = durable_observed(*args, **kwargs)
            if not crashed_after_observed:
                crashed_after_observed = True
                raise RuntimeError("crash after durable observed")
            return event

        checkpoint_store.record_observed = crash_after_observed
        with pytest.raises(RuntimeError, match="durable observed"):
            asyncio.run(
                run_research_loop(
                    initial_state=state,
                    task="research schema",
                    research_context=lambda current, _feedbacks: canonical_digest(current),
                    model=model,
                    model_identity="test/model",
                    adapter=SchemaResearchDecisionAdapter(
                        load_schema_research_agent_profile()
                    ),
                    loaded_schema=loaded_schema,
                    freshness_context=_fixture_freshness(state),
                    registry=registry,
                    state_store=state_store,
                    checkpoint_store=checkpoint_store,
                    budget_ledger=ledger,
                    policy=_policy(),
                )
            )
        checkpoint_store.record_observed = durable_observed
        durable_save = state_store.save_replayable_semantic_transition
        crashed_after_cas = False

        def crash_after_cas(previous, saved_state, replay_input):
            nonlocal crashed_after_cas
            stored = durable_save(previous, saved_state, replay_input)
            if saved_state.revision == 1 and not crashed_after_cas:
                crashed_after_cas = True
                raise RuntimeError("crash after durable state cas")
            return stored

        state_store.save_replayable_semantic_transition = crash_after_cas
        with pytest.raises(RuntimeError, match="durable state cas"):
            asyncio.run(
                run_research_loop(
                    initial_state=state,
                    task="research schema",
                    research_context=lambda current, _feedbacks: canonical_digest(current),
                    model=model,
                    model_identity="test/model",
                    adapter=SchemaResearchDecisionAdapter(
                        load_schema_research_agent_profile()
                    ),
                    loaded_schema=loaded_schema,
                    freshness_context=_fixture_freshness(state),
                    registry=registry,
                    state_store=state_store,
                    checkpoint_store=checkpoint_store,
                    budget_ledger=ledger,
                    policy=_policy(),
                )
            )
        state_store.save_replayable_semantic_transition = durable_save
        outcome = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert outcome.stop_reason is ResearchStopReason.AMBIGUOUS
        assert outcome.final_state.revision == 1
        assert len(outcome.final_state.action_history) == 1
        assert registry.adapter.execute_calls == 1
        assert [
            record.reservation.call_id
            for record in ledger.load_model_records(state.run_id, state.run_incarnation)
        ] == ["research-model-0-0", "research-model-0-1", "research-model-1-0"]
        assert all(
            record.reconciliation is not None
            for record in ledger.load_model_records(state.run_id, state.run_incarnation)
        )
        persisted = state_store.load_latest_research_state(
            state.run_id, state.run_incarnation
        )
        assert persisted is not None
        assert persisted.budget_state.used_model_calls == 2
        checkpoint = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
            )
        )
        assert checkpoint.planned is not None
        assert checkpoint.observed is not None
        assert outcome.final_state.budget_state.used_model_calls == 3
        assert outcome.final_state.budget_state.used_model_tokens == 60
        assert (
            persisted.model_copy(
                update={"budget_state": outcome.final_state.budget_state}
            )
            == outcome.final_state
        )

        async def replay_model(_prompt: str) -> str:
            raise AssertionError("terminal replay must not charge a new model turn")

        replay = asyncio.run(
            run_research_loop(
                initial_state=state,
                task="research schema",
                research_context=lambda current, _feedbacks: canonical_digest(current),
                model=replay_model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(state),
                registry=registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert replay == outcome
        assert registry.adapter.execute_calls == 1
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


@pytest.mark.parametrize("_repeat", range(20))
def test_probe_result_before_deadline_is_observed_then_closed_without_reexecution(
    tmp_path, monkeypatch, _repeat: int
) -> None:
    """A completed probe is durable even when the next boundary sees timeout."""

    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    clock = [0.0]
    deadline = DeadlineBudget(
        deadline_monotonic=1.0,
        deadline_at_ms=1_000,
        monotonic=lambda: clock[0],
    )
    registry.context.schema_runtime.deadline = deadline
    registry.context.data_runtime.deadline = deadline
    prepared = _resolve_fixture(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=registry,
    )
    assert prepared.admission.action is not None
    assert prepared.invocation is not None
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id=prepared.invocation.invocation_id,
        action_digest=prepared.admission.action.action_digest,
        probe_kind=prepared.admission.action.kind,
        status=ProbeStatus.SUCCESS,
        target=prepared.admission.action.target,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="completed before deadline boundary",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=11,
        ),
        row_count=1,
        payload={"ok": True},
    )
    registry.adapter.result = NormalizedToolResult(
        "success", result.model_dump(mode="json", by_alias=True)
    )
    ledger = AdaptiveBudgetLedger(tmp_path / "deadline-budget.sqlite")
    execute = _research_loop_module.execute_resolved_research_decision

    def execute_then_expire(resolved, tools, *, recover=False):
        observed = execute(resolved, tools, recover=recover)
        action = resolved.admission.action
        assert action is not None
        observed, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            observed.cost,
            lambda _reservation: observed,
            config=_policy(),
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "deadline-tool-owner",
        )
        clock[0] = 1.0
        return observed

    monkeypatch.setattr(
        _research_loop_module,
        "execute_resolved_research_decision",
        execute_then_expire,
    )

    async def model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
        )

    outcome, state_store, checkpoint_store, ledger = asyncio.run(
        _run(
            tmp_path,
            state,
            model,
            loaded_schema=loaded_schema,
            freshness_context=_fixture_freshness(state),
            registry=registry,
            deadline=deadline,
            budget_ledger=ledger,
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.DEADLINE_EXCEEDED
        assert outcome.final_state.revision == 1
        assert len(outcome.final_state.action_history) == 1
        assert len(outcome.final_state.evidence) == 1
        assert registry.adapter.execute_calls + registry.adapter.recover_calls == 1
        checkpoint = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
            )
        )
        assert checkpoint.observed is not None
        assert checkpoint.observed.action["kind"] == "research_observed"
        assert checkpoint.terminal is None
        terminal = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 1
            )
        ).terminal
        assert terminal is not None

        async def replay_model(_prompt: str) -> str:
            raise AssertionError("closed deadline replay must not call model")

        replay, replay_state_store, replay_checkpoint_store, replay_ledger = (
            asyncio.run(
                _run(
                    tmp_path,
                    state,
                    replay_model,
                    loaded_schema=loaded_schema,
                    freshness_context=_fixture_freshness(state),
                    registry=registry,
                    deadline=deadline,
                    budget_ledger=ledger,
                )
            )
        )
        try:
            assert replay == outcome
            assert replay_ledger is ledger
            assert registry.adapter.execute_calls + registry.adapter.recover_calls == 1
        finally:
            replay_state_store.close()
            replay_checkpoint_store.close()
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_cancelled_planned_turn_replays_durable_abort_after_terminal_crash(
    tmp_path,
) -> None:
    """An abort is an observable event, so a terminal-write crash is replay-safe."""

    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    state_store = AdaptiveResearchStateStore(tmp_path / "abort.sqlite")
    checkpoint_store = AdaptiveStateStore(tmp_path / "abort.sqlite")
    ledger = AdaptiveBudgetLedger(tmp_path / "abort-budget.sqlite")

    async def tool_model(_prompt: str) -> str:
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"public.orders"}}}}'
        )

    async def no_model(_prompt: str) -> str:
        raise AssertionError("planned replay must not ask the model again")

    arguments = {
        "initial_state": state,
        "task": "research schema",
        "research_context": lambda current, _feedbacks: canonical_digest(current),
        "model_identity": "test/model",
        "adapter": SchemaResearchDecisionAdapter(load_schema_research_agent_profile()),
        "loaded_schema": loaded_schema,
        "freshness_context": _fixture_freshness(state),
        "registry": registry,
        "state_store": state_store,
        "checkpoint_store": checkpoint_store,
        "budget_ledger": ledger,
        "policy": _policy(),
    }
    try:
        record_planned = checkpoint_store.record_planned

        def crash_after_planned(*args, **kwargs):
            record_planned(*args, **kwargs)
            raise RuntimeError("crash after planned")

        checkpoint_store.record_planned = crash_after_planned
        with pytest.raises(RuntimeError, match="crash after planned"):
            asyncio.run(run_research_loop(model=tool_model, **arguments))
        checkpoint_store.record_planned = record_planned

        record_observed = checkpoint_store.record_observed

        def crash_after_abort(*args, **kwargs):
            record_observed(*args, **kwargs)
            raise RuntimeError("crash after abort observed")

        checkpoint_store.record_observed = crash_after_abort
        with pytest.raises(RuntimeError, match="crash after abort observed"):
            asyncio.run(
                run_research_loop(
                    model=no_model,
                    is_cancelled=lambda: True,
                    **arguments,
                )
            )
        checkpoint_store.record_observed = record_observed

        replay = asyncio.run(
            run_research_loop(
                model=no_model,
                is_cancelled=lambda: True,
                **arguments,
            )
        )
        assert replay.stop_reason is ResearchStopReason.CANCELLED
        checkpoint = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
            )
        )
        assert checkpoint.observed is not None
        assert checkpoint.observed.action["kind"] == "research_aborted"
        assert (
            checkpoint.observed.action["reason"] == ResearchStopReason.CANCELLED.value
        )
        assert checkpoint.terminal is not None
        assert (
            checkpoint.terminal.action["reason"] == ResearchStopReason.CANCELLED.value
        )
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_three_repeated_semantic_observations_stop_as_stagnated(
    tmp_path, monkeypatch
) -> None:
    """Different valid actions do not extend research when their facts repeat."""

    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace)
    registry = _make_registry(namespace)
    ledger = AdaptiveBudgetLedger(tmp_path / "stagnated-budget.sqlite")

    async def baseline_usage(_reservation) -> ModelTokenUsage:
        return ModelTokenUsage(input_tokens=None, output_tokens=None)

    asyncio.run(
        execute_model_call_with_budget_async(
            state.run_id,
            state.run_incarnation,
            "research-model-0-0",
            canonical_digest({"baseline": True}),
            "test/model",
            10,
            10,
            baseline_usage,
            config=_policy(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "baseline-model-owner",
        )
    )
    projected_state = _state_with_reconciled_model_budget(state, ledger, _policy())
    baseline = _resolve_fixture(
        _tool_decision(
            "inspect_relationships",
            {"table": "public.orders", "top_k": 4, "depth": 1},
        ),
        loaded=loaded_schema,
        namespace=namespace,
        state=projected_state,
        registry=registry,
    )
    assert baseline.admission.action is not None
    assert baseline.invocation is not None
    baseline_result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id=baseline.invocation.invocation_id,
        action_digest=baseline.admission.action.action_digest,
        probe_kind=baseline.admission.action.kind,
        status=ProbeStatus.SUCCESS,
        target=baseline.admission.action.target,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="the same semantic observation",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=15,
        ),
        row_count=1,
        payload={"same": "fact"},
    )
    baseline_result, baseline_reconciliation = execute_probe_with_budget(
        projected_state,
        baseline.admission.action,
        baseline_result.cost,
        lambda _reservation: baseline_result,
        config=_policy(),
        ledger=ledger,
        monotonic_ns=lambda: 0,
        utc_now=lambda: _FIXTURE_NOW,
        claim_now_ns=lambda: 1,
        owner_token_factory=lambda: "baseline-tool-owner",
    )
    baseline_state = commit_semantic_turn(
        replace(
            baseline.admission,
            budget_state=baseline_reconciliation.budget_after,
        ),
        probe_result=baseline_result,
    ).state
    tool_calls = 0

    def repeated_observation(resolved, _tools, *, recover=False):
        nonlocal tool_calls
        assert recover is False
        tool_calls += 1
        action = resolved.admission.action
        invocation = resolved.invocation
        assert action is not None
        assert invocation is not None
        maximum_cost = EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=15,
        )
        result = build_probe_result(
            run_id=baseline_state.run_id,
            run_incarnation=baseline_state.run_incarnation,
            revision=action.expected_revision,
            schema_namespace_version=baseline_state.schema_namespace_version,
            invocation_id=invocation.invocation_id,
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=action.target,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="the same semantic observation",
            cost=maximum_cost,
            row_count=1,
            payload={"same": "fact"},
        )
        result, _ = execute_probe_with_budget(
            resolved.admission.state,
            action,
            maximum_cost,
            lambda _reservation: result,
            config=_policy(),
            ledger=ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: action.expected_revision + 1,
            owner_token_factory=lambda: f"stagnated-tool-{action.expected_revision}",
        )
        return result

    monkeypatch.setattr(
        _research_loop_module,
        "execute_resolved_research_decision",
        repeated_observation,
    )
    responses = iter(
        (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_relationships","arguments":'
            '{"table":"public.orders","top_k":1,"depth":1}}}}',
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_relationships","arguments":'
            '{"table":"public.orders","top_k":2,"depth":1}}}}',
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_relationships","arguments":'
            '{"table":"public.orders","top_k":3,"depth":1}}}}',
        )
    )

    async def model(_prompt: str) -> str:
        return next(responses)

    database = tmp_path / "stagnated.sqlite"
    seed = AdaptiveCheckpointKey(
        state.run_id, state.run_incarnation, AdaptiveLoopKind.RESEARCH, 0
    )
    _seed_honest_v2_history(
        database,
        states=(state, baseline_state),
        events=(
            (seed, "planned", _research_loop_module._planned_action(baseline)),
            (
                seed,
                "observed",
                {
                    "contract_version": 1,
                    "kind": "research_observed",
                    "novel": True,
                    "result": baseline_result.model_dump(mode="json", by_alias=True),
                    "resolution_digest": baseline.resolution_digest,
                },
            ),
        ),
    )
    state_store = AdaptiveResearchStateStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    try:
        outcome = asyncio.run(
            run_research_loop(
                initial_state=baseline_state,
                task="research schema",
                research_context=lambda current, _feedbacks, *_args: canonical_digest(
                    current
                ),
                model=model,
                model_identity="test/model",
                adapter=SchemaResearchDecisionAdapter(
                    load_schema_research_agent_profile()
                ),
                loaded_schema=loaded_schema,
                freshness_context=_fixture_freshness(baseline_state),
                registry=registry,
                state_store=state_store,
                checkpoint_store=checkpoint_store,
                budget_ledger=ledger,
                policy=_policy(),
            )
        )
        assert tool_calls == 3, (outcome.stop_reason, outcome.final_state.revision)
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
        assert outcome.final_state.revision == 4
        assert len(outcome.final_state.action_history) == 4
        assert len(outcome.final_state.evidence) == 4
        assert tool_calls == 3
        assert [
            checkpoint_store.get_snapshot(
                AdaptiveCheckpointKey(
                    baseline_state.run_id,
                    baseline_state.run_incarnation,
                    AdaptiveLoopKind.RESEARCH,
                    revision,
                )
            ).observed.action["novel"]
            for revision in range(1, 4)
        ] == [False, False, False]
        terminal = checkpoint_store.get_snapshot(
            AdaptiveCheckpointKey(
                baseline_state.run_id,
                baseline_state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                4,
            )
        ).terminal
        assert terminal is not None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_semantic_novelty_ignores_complete_row_subset() -> None:
    _, namespace = _fixture_schema()
    state = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    prior_observation = json.dumps(
        {
            "payload": {
                "columns": ["CustomerID", "Date", "Consumption"],
                "rows": [
                    [38508, "201201", 67156.94],
                    [38508, "201202", 88658.88],
                ],
                "schema_namespace_version": state.schema_namespace_version,
            },
            "row_count": 2,
            "truncated": False,
        }
    )
    subset_observation = json.dumps(
        {
            "payload": {
                "columns": ["CustomerID", "Date", "Consumption"],
                "rows": [[38508, "201201", 67156.94]],
                "schema_namespace_version": state.schema_namespace_version,
            },
            "row_count": 1,
            "truncated": False,
        }
    )
    prior = state.evidence[0].model_copy(
        update={"evidence_id": "prior-rows", "observation": prior_observation}
    )
    subset = prior.model_copy(
        update={"evidence_id": "subset-rows", "observation": subset_observation}
    )
    current = state.model_copy(update={"evidence": (prior,)})
    committed = SimpleNamespace(
        state=state.model_copy(update={"evidence": (prior, subset)}),
        novelty=SimpleNamespace(
            added_hypothesis_ids=(),
            updated_hypothesis_ids=(),
            added_binding_ids=(),
            updated_binding_ids=(),
            added_join_ids=(),
            updated_join_ids=(),
            unresolved_items=current.unresolved_items,
            stop_reason=current.stop_reason,
        ),
    )

    assert (
        _research_loop_module._is_semantically_novel_turn(current, committed)
        is False
    )


def test_semantic_novelty_ignores_reworded_hypothesis_for_same_targets() -> None:
    state = _state(required=True)
    target = TableRef(namespace="main", schema="main", table="yearmonth")
    prior = Hypothesis(
        hypothesis_id="hypothesis-prior",
        source_ids=("source-1",),
        claim="The monthly value may be in yearmonth.",
        candidate_targets=(target,),
        status=HypothesisStatus.PROPOSED,
        evidence_ids=(),
    )
    reworded = prior.model_copy(
        update={
            "hypothesis_id": "hypothesis-reworded",
            "claim": "The yearmonth table may contain the monthly value.",
        }
    )
    current = state.model_copy(update={"hypotheses": (prior,)})
    committed = SimpleNamespace(
        state=current.model_copy(update={"hypotheses": (prior, reworded)}),
        novelty=SimpleNamespace(
            added_hypothesis_ids=(reworded.hypothesis_id,),
            updated_hypothesis_ids=(),
            added_binding_ids=(),
            updated_binding_ids=(),
            added_join_ids=(),
            updated_join_ids=(),
            unresolved_items=current.unresolved_items,
            stop_reason=current.stop_reason,
        ),
    )

    assert (
        _research_loop_module._is_semantically_novel_turn(current, committed)
        is False
    )


def test_semantic_novelty_requires_semantic_change_not_only_new_evidence() -> None:
    _, namespace = _fixture_schema()
    observed = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    current = observed.model_copy(update={"evidence": ()})
    committed = SimpleNamespace(
        state=observed,
        novelty=SimpleNamespace(
            added_hypothesis_ids=(),
            updated_hypothesis_ids=(),
            added_binding_ids=(),
            updated_binding_ids=(),
            added_join_ids=(),
            updated_join_ids=(),
            unresolved_items=current.unresolved_items,
            stop_reason=current.stop_reason,
        ),
    )

    assert (
        _research_loop_module._is_semantically_novel_turn(current, committed)
        is False
    )


@pytest.mark.parametrize(
    "exact_kind", (SemanticItemKind.FILTER, SemanticItemKind.TIME)
)
def test_semantic_novelty_counts_first_qualifying_value_evidence(
    tmp_path, exact_kind: SemanticItemKind
) -> None:
    """Only qualifying relevant closed value observations extend research."""

    _, namespace = _fixture_schema()
    base = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    column = base.bindings[0].physical_column
    actions: dict[str, ResearchAction] = {}

    def evidence(
        evidence_id: str,
        kind: ResearchActionKind,
        payload: dict[str, object],
        *,
        target: ColumnRef = column,
        truncated: bool = False,
        expected_revision: int = base.revision,
    ):
        value = payload.get("requested_value")
        parameters = (
            (("top_k", 2),)
            if kind is ResearchActionKind.DISTINCT_VALUES
            else (("value", value),) if value is not None else ()
        )
        action = ResearchAction(
            action_id=f"{evidence_id}-action",
            kind=kind,
            hypothesis_id=None,
            target=target,
            parameters=parameters,
            action_digest=canonical_action_digest(
                kind=kind,
                hypothesis_id=None,
                target=target,
                parameters=parameters,
                expected_revision=expected_revision,
            ),
            expected_revision=expected_revision,
        )
        result = build_probe_result(
            run_id=base.run_id,
            run_incarnation=base.run_incarnation,
            revision=expected_revision,
            schema_namespace_version=base.schema_namespace_version,
            invocation_id=evidence_id,
            action_digest=action.action_digest,
            probe_kind=kind,
            status=ProbeStatus.SUCCESS,
            target=target,
            started_at=_FIXTURE_NOW,
            completed_at=_FIXTURE_NOW,
            summary="neutral value observation",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=len(payload["rows"]),
                bytes=len(canonical_json_bytes(payload)),
            ),
            row_count=len(payload["rows"]),
            truncated=truncated,
            payload=payload,
        )
        record = probe_result_to_evidence(result, action)
        assert record is not None
        actions[evidence_id] = action
        return record

    def is_novel(
        current: ResearchState,
        record,
        *,
        action_history: tuple[ResearchAction, ...] | None = None,
    ) -> bool:
        committed = SimpleNamespace(
            state=current.model_copy(
                update={
                    "evidence": (*current.evidence, record),
                    "action_history": action_history or current.action_history,
                    "revision": current.revision + 1,
                    "query_spec": current.query_spec.model_copy(
                        update={"revision": current.revision + 1}
                    ),
                }
            ),
            novelty=SimpleNamespace(
                added_hypothesis_ids=(),
                updated_hypothesis_ids=(),
                added_binding_ids=(),
                updated_binding_ids=(),
                added_join_ids=(),
                updated_join_ids=(),
                unresolved_items=current.unresolved_items,
                stop_reason=current.stop_reason,
            ),
        )
        return _research_loop_module._is_semantically_novel_turn(current, committed)

    full_distinct = evidence(
        "evidence:marker-distinct",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [column.column], "rows": [["marker-a"], ["marker-b"]]},
    )
    exact_search = evidence(
        "evidence:marker-exact",
        ResearchActionKind.SEARCH_VALUE,
        {
            "columns": [column.column],
            "requested_value": "marker-a",
            "rows": [["marker-a"]],
        },
    )
    with_distinct = base.model_copy(
        update={
            "evidence": (*base.evidence, full_distinct),
            "action_history": (*base.action_history, actions[full_distinct.evidence_id]),
            "revision": base.revision + 1,
            "query_spec": base.query_spec.model_copy(
                update={"revision": base.revision + 1}
            ),
        }
    )

    qualifying_novelty = is_novel(
        base,
        full_distinct,
        action_history=(*base.action_history, actions[full_distinct.evidence_id]),
    )
    assert qualifying_novelty
    assert is_novel(
        with_distinct,
        exact_search,
        action_history=(*with_distinct.action_history, actions[exact_search.evidence_id]),
    )

    duplicate = evidence(
        "evidence:marker-duplicate",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [column.column], "rows": [["marker-a"], ["marker-b"]]},
    )
    reordered = evidence(
        "evidence:marker-reordered",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [column.column], "rows": [["marker-b"], ["marker-a"]]},
    )
    subset = evidence(
        "evidence:marker-subset",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [column.column], "rows": [["marker-a"]]},
    )
    growing = evidence(
        "evidence:marker-growing",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [column.column], "rows": [["marker-c"]]},
    )
    typed_distinct = evidence(
        "evidence:marker-typed-distinct",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [column.column], "rows": [[1]]},
    )
    type_exact_growth = evidence(
        "evidence:marker-type-exact",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [column.column], "rows": [[True]]},
    )
    empty = evidence(
        "evidence:marker-empty",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [column.column], "rows": []},
    )
    truncated = evidence(
        "evidence:marker-truncated",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [column.column], "rows": [["marker-a"]]},
        truncated=True,
    )
    sample = evidence(
        "evidence:marker-sample",
        ResearchActionKind.SAMPLE_ROWS,
        {"columns": [column.column], "rows": [["marker-a"]]},
    )
    custom = evidence(
        "evidence:marker-custom",
        ResearchActionKind.EXECUTE_PROBE,
        {"columns": [column.column], "rows": [["marker-a"]]},
    )
    unrelated = evidence(
        "evidence:other-column",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": ["other_marker"], "rows": [["marker-a"]]},
        target=ColumnRef(table=column.table, column="other_marker"),
    )
    out_of_domain = evidence(
        "evidence:other-table",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": ["status"], "rows": [["marker-a"]]},
        target=ColumnRef(
            table=TableRef(namespace="main", schema="public", table="archive"),
            column="status",
        ),
    )
    malformed = full_distinct.model_copy(
        update={"evidence_id": "evidence:marker-malformed", "observation": "{not-json"}
    )

    for record in (
        duplicate,
        reordered,
        subset,
        empty,
        truncated,
        sample,
        custom,
        unrelated,
        out_of_domain,
        malformed,
    ):
        assert not is_novel(with_distinct, record)

    replay_state = base.model_copy(
        update={
            "revision": 4,
            "query_spec": base.query_spec.model_copy(update={"revision": 4}),
        }
    )
    database = tmp_path / "value-evidence-novelty.sqlite"
    checkpoint_events = tuple(
        (
            AdaptiveCheckpointKey(
                replay_state.run_id,
                replay_state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                revision,
            ),
            "observed",
            {
                "contract_version": 1,
                "kind": "research_observed",
                "novel": qualifying_novelty if revision == 3 else False,
                "result": None,
                "resolution_digest": "sha256:" + "1" * 64,
            },
        )
        for revision in range(4)
    )
    _seed_honest_v2_history(
        database, states=(replay_state,), events=checkpoint_events
    )
    checkpoint_store = AdaptiveStateStore(database)
    try:
        assert _research_loop_module._consecutive_non_novel(
            checkpoint_store, replay_state
        ) == 0
    finally:
        checkpoint_store.close()

    optional_item = base.query_spec.semantic_items[0].model_copy(
        update={"required": False}
    )
    optional_state = base.model_copy(
        update={
            "query_spec": base.query_spec.model_copy(
                update={"semantic_items": (optional_item,)}
            )
        }
    )
    candidate_state = base.model_copy(
        update={
            "bindings": (
                base.bindings[0].model_copy(update={"status": BindingStatus.CANDIDATE}),
            )
        }
    )
    rejected_state = base.model_copy(
        update={
            "bindings": (
                base.bindings[0].model_copy(update={"status": BindingStatus.REJECTED}),
            )
        }
    )
    stale_state = base.model_copy(
        update={
            "bindings": (
                base.bindings[0].model_copy(update={"status": BindingStatus.STALE}),
            )
        }
    )
    unselected_state = base.model_copy(
        update={
            "query_spec": base.query_spec.model_copy(
                update={
                    "semantic_items": (
                        base.query_spec.semantic_items[0].model_copy(
                            update={"binding_ids": ()}
                        ),
                    )
                }
            )
        }
    )
    with_typed_distinct = base.model_copy(
        update={
            "evidence": (*base.evidence, typed_distinct),
            "action_history": (
                *base.action_history,
                actions[typed_distinct.evidence_id],
            ),
            "revision": base.revision + 1,
            "query_spec": base.query_spec.model_copy(
                update={"revision": base.revision + 1}
            ),
        }
    )
    constrained_column = ColumnRef(table=column.table, column="constraint_marker")
    constrained_state = base.model_copy(
        update={
            "query_spec": base.query_spec.model_copy(
                update={
                    "global_constraints": (
                        PredicateRef(
                            left=constrained_column,
                            operator=PredicateOperator.EQ,
                            right="marker-a",
                        ),
                    )
                }
            )
        }
    )
    constrained_distinct = evidence(
        "evidence:constraint-distinct",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [constrained_column.column], "rows": [["marker-a"]]},
        target=constrained_column,
    )
    constraint_right_column = ColumnRef(table=column.table, column="right_marker")
    right_constrained_state = base.model_copy(
        update={
            "query_spec": base.query_spec.model_copy(
                update={
                    "global_constraints": (
                        PredicateRef(
                            left=constrained_column,
                            operator=PredicateOperator.EQ,
                            right=constraint_right_column,
                        ),
                    )
                }
            )
        }
    )
    right_constrained_distinct = evidence(
        "evidence:right-constraint-distinct",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [constraint_right_column.column], "rows": [["marker-a"]]},
        target=constraint_right_column,
    )
    forged_distinct_action = actions[full_distinct.evidence_id].model_copy(
        update={
            "action_id": "evidence:marker-forged-distinct-action",
            "parameters": (("top_k", 3),),
            "action_digest": canonical_action_digest(
                kind=ResearchActionKind.DISTINCT_VALUES,
                hypothesis_id=None,
                target=column,
                parameters=(("top_k", 3),),
                expected_revision=base.revision,
            ),
        }
    )
    forged_distinct = full_distinct.model_copy(
        update={
            "evidence_id": "evidence:marker-forged-distinct",
            "action_digest": forged_distinct_action.action_digest,
        }
    )
    foreign_distinct = full_distinct.model_copy(
        update={"evidence_id": "evidence:foreign-distinct", "run_id": "foreign-run"}
    )
    forged_action = actions[exact_search.evidence_id].model_copy(
        update={
            "action_id": "evidence:marker-forged-action",
            "parameters": (("value", "marker-b"),),
            "action_digest": canonical_action_digest(
                kind=ResearchActionKind.SEARCH_VALUE,
                hypothesis_id=None,
                target=column,
                parameters=(("value", "marker-b"),),
                expected_revision=base.revision,
            ),
        }
    )
    forged_search = exact_search.model_copy(
        update={"evidence_id": "evidence:marker-forged", "action_digest": forged_action.action_digest}
    )
    foreign_search = exact_search.model_copy(
        update={
            "evidence_id": "evidence:foreign-search",
            "run_incarnation": "foreign-incarnation",
        }
    )

    novelties = (
        is_novel(optional_state, full_distinct),
        is_novel(
            candidate_state,
            full_distinct,
            action_history=(
                *base.action_history,
                actions[full_distinct.evidence_id],
            ),
        ),
        is_novel(rejected_state, full_distinct),
        is_novel(stale_state, full_distinct),
        is_novel(unselected_state, full_distinct),
        is_novel(
            with_distinct,
            growing,
            action_history=(
                *with_distinct.action_history,
                actions[growing.evidence_id],
            ),
        ),
        is_novel(
            with_typed_distinct,
            type_exact_growth,
            action_history=(
                *with_typed_distinct.action_history,
                actions[type_exact_growth.evidence_id],
            ),
        ),
        is_novel(
            constrained_state,
            constrained_distinct,
            action_history=(*base.action_history, actions[constrained_distinct.evidence_id]),
        ),
        is_novel(
            right_constrained_state,
            right_constrained_distinct,
            action_history=(
                *base.action_history,
                actions[right_constrained_distinct.evidence_id],
            ),
        ),
        is_novel(base, full_distinct),
        is_novel(
            base,
            forged_distinct,
            action_history=(*base.action_history, forged_distinct_action),
        ),
        is_novel(
            base,
            foreign_distinct,
            action_history=(*base.action_history, actions[full_distinct.evidence_id]),
        ),
        is_novel(with_distinct, exact_search),
        is_novel(
            with_distinct,
            forged_search,
                action_history=(*with_distinct.action_history, forged_action),
            ),
        is_novel(
            with_distinct,
            foreign_search,
            action_history=(*with_distinct.action_history, actions[exact_search.evidence_id]),
        ),
    )
    assert novelties == (
        False,
        True,
        False,
        False,
        False,
        True,
        True,
        True,
        True,
        False,
        False,
        False,
        False,
        False,
        False,
    ), (
        "candidate="
        f"{novelties[1]}, growing={novelties[5]}, "
        f"type_exact_growth={novelties[6]}"
    )

    exact_column = ColumnRef(
        table=TableRef(namespace="main", schema="public", table="categories"),
        column="category_marker",
    )
    unbound_base = _policy_state(namespace)
    exact_item = unbound_base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": exact_kind,
            "required": True,
            "exact_physical_predicate": True,
            "exact_physical_column_name": exact_column.column,
            "operator": PredicateOperator.EQ,
            "literal_or_reference": "neutral-marker",
            "status": SemanticItemStatus.UNRESOLVED,
            "binding_ids": (),
        }
    )
    exact_unbound = unbound_base.model_copy(
        update={
            "query_spec": unbound_base.query_spec.model_copy(
                update={"semantic_items": (exact_item,)}
            ),
            "unresolved_items": (exact_item.source_id,),
        }
    )
    exact_distinct = evidence(
        "evidence:exact-unbound-distinct",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [exact_column.column], "rows": [["marker-a"]]},
        target=exact_column,
        expected_revision=exact_unbound.revision,
    )
    exact_novelty = is_novel(
        exact_unbound,
        exact_distinct,
        action_history=(actions[exact_distinct.evidence_id],),
    )
    assert exact_novelty

    exact_search_without_distinct = evidence(
        "evidence:exact-unbound-search",
        ResearchActionKind.SEARCH_VALUE,
        {
            "columns": [exact_column.column],
            "requested_value": "neutral-marker",
            "rows": [["neutral-marker"]],
        },
        target=exact_column,
        expected_revision=exact_unbound.revision,
    )
    exact_search_novelty = is_novel(
        exact_unbound,
        exact_search_without_distinct,
        action_history=(actions[exact_search_without_distinct.evidence_id],),
    )
    assert exact_search_novelty

    optional_exact = exact_item.model_copy(update={"required": False})
    optional_exact_state = exact_unbound.model_copy(
        update={
            "query_spec": exact_unbound.query_spec.model_copy(
                update={"semantic_items": (optional_exact,)}
            ),
            "unresolved_items": (),
        }
    )
    non_exact = exact_item.model_copy(
        update={
            "exact_physical_predicate": False,
            "exact_physical_column_name": None,
        }
    )
    non_exact_state = exact_unbound.model_copy(
        update={
            "query_spec": exact_unbound.query_spec.model_copy(
                update={"semantic_items": (non_exact,)}
            )
        }
    )
    typed_literal_item = exact_item.model_copy(update={"literal_or_reference": 1})
    typed_literal_state = exact_unbound.model_copy(
        update={
            "query_spec": exact_unbound.query_spec.model_copy(
                update={"semantic_items": (typed_literal_item,)}
            )
        }
    )
    typed_mismatch_search = evidence(
        "evidence:exact-unbound-typed-mismatch-search",
        ResearchActionKind.SEARCH_VALUE,
        {
            "columns": [exact_column.column],
            "requested_value": True,
            "rows": [[True]],
        },
        target=exact_column,
        expected_revision=exact_unbound.revision,
    )
    wrong_literal_search = evidence(
        "evidence:exact-unbound-wrong-literal-search",
        ResearchActionKind.SEARCH_VALUE,
        {
            "columns": [exact_column.column],
            "requested_value": "other-neutral-marker",
            "rows": [["other-neutral-marker"]],
        },
        target=exact_column,
        expected_revision=exact_unbound.revision,
    )
    mismatched_column = ColumnRef(
        table=exact_column.table, column="other_category_marker"
    )
    mismatched_distinct = evidence(
        "evidence:exact-unbound-mismatched",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [mismatched_column.column], "rows": [["marker-a"]]},
        target=mismatched_column,
        expected_revision=exact_unbound.revision,
    )
    mismatched_search = evidence(
        "evidence:exact-unbound-mismatched-search",
        ResearchActionKind.SEARCH_VALUE,
        {
            "columns": [mismatched_column.column],
            "requested_value": "neutral-marker",
            "rows": [["neutral-marker"]],
        },
        target=mismatched_column,
        expected_revision=exact_unbound.revision,
    )
    zero_row_distinct = evidence(
        "evidence:exact-unbound-empty",
        ResearchActionKind.DISTINCT_VALUES,
        {"columns": [exact_column.column], "rows": []},
        target=exact_column,
        expected_revision=exact_unbound.revision,
    )
    zero_row_search = evidence(
        "evidence:exact-unbound-empty-search",
        ResearchActionKind.SEARCH_VALUE,
        {
            "columns": [exact_column.column],
            "requested_value": "neutral-marker",
            "rows": [],
        },
        target=exact_column,
        expected_revision=exact_unbound.revision,
    )
    duplicate_exact_search = evidence(
        "evidence:exact-unbound-duplicate-search",
        ResearchActionKind.SEARCH_VALUE,
        {
            "columns": [exact_column.column],
            "requested_value": "neutral-marker",
            "rows": [["neutral-marker"]],
        },
        target=exact_column,
        expected_revision=exact_unbound.revision,
    )
    forged_exact_action = actions[exact_search_without_distinct.evidence_id].model_copy(
        update={
            "action_id": "evidence:exact-unbound-forged-search-action",
            "parameters": (("value", "other-neutral-marker"),),
            "action_digest": canonical_action_digest(
                kind=ResearchActionKind.SEARCH_VALUE,
                hypothesis_id=None,
                target=exact_column,
                parameters=(("value", "other-neutral-marker"),),
                expected_revision=exact_unbound.revision,
            ),
        }
    )
    forged_exact_search = exact_search_without_distinct.model_copy(
        update={
            "evidence_id": "evidence:exact-unbound-forged-search",
            "action_digest": forged_exact_action.action_digest,
        }
    )
    foreign_exact_search = exact_search_without_distinct.model_copy(
        update={
            "evidence_id": "evidence:exact-unbound-foreign-search",
            "run_id": "foreign-run",
        }
    )
    with_exact_search = exact_unbound.model_copy(
        update={
            "evidence": (*exact_unbound.evidence, exact_search_without_distinct),
            "action_history": (actions[exact_search_without_distinct.evidence_id],),
            "revision": exact_unbound.revision + 1,
            "query_spec": exact_unbound.query_spec.model_copy(
                update={"revision": exact_unbound.revision + 1}
            ),
        }
    )
    assert not is_novel(
        optional_exact_state,
        exact_distinct,
        action_history=(actions[exact_distinct.evidence_id],),
    )
    assert not is_novel(
        non_exact_state,
        exact_distinct,
        action_history=(actions[exact_distinct.evidence_id],),
    )
    assert not is_novel(
        exact_unbound,
        mismatched_distinct,
        action_history=(actions[mismatched_distinct.evidence_id],),
    )
    assert not is_novel(
        exact_unbound,
        zero_row_distinct,
        action_history=(actions[zero_row_distinct.evidence_id],),
    )
    assert not is_novel(
        optional_exact_state,
        exact_search_without_distinct,
        action_history=(actions[exact_search_without_distinct.evidence_id],),
    )
    assert not is_novel(
        non_exact_state,
        exact_search_without_distinct,
        action_history=(actions[exact_search_without_distinct.evidence_id],),
    )
    assert not is_novel(
        typed_literal_state,
        typed_mismatch_search,
        action_history=(actions[typed_mismatch_search.evidence_id],),
    )
    assert not is_novel(
        exact_unbound,
        wrong_literal_search,
        action_history=(actions[wrong_literal_search.evidence_id],),
    )
    assert not is_novel(
        exact_unbound,
        mismatched_search,
        action_history=(actions[mismatched_search.evidence_id],),
    )
    assert not is_novel(
        exact_unbound,
        zero_row_search,
        action_history=(actions[zero_row_search.evidence_id],),
    )
    assert not is_novel(
        with_exact_search,
        duplicate_exact_search,
        action_history=(
            *with_exact_search.action_history,
            actions[duplicate_exact_search.evidence_id],
        ),
    )
    assert not is_novel(
        exact_unbound,
        forged_exact_search,
        action_history=(forged_exact_action,),
    )
    assert not is_novel(
        exact_unbound,
        foreign_exact_search,
        action_history=(actions[exact_search_without_distinct.evidence_id],),
    )
    exact_replay_state = exact_unbound.model_copy(
        update={
            "revision": 4,
            "query_spec": exact_unbound.query_spec.model_copy(update={"revision": 4}),
        }
    )
    exact_database = tmp_path / "exact-unbound-value-evidence-novelty.sqlite"
    exact_events = tuple(
        (
            AdaptiveCheckpointKey(
                exact_replay_state.run_id,
                exact_replay_state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                revision,
            ),
            "observed",
            {
                "contract_version": 1,
                "kind": "research_observed",
                "novel": exact_novelty if revision == 3 else False,
                "result": None,
                "resolution_digest": "sha256:" + "2" * 64,
            },
        )
        for revision in range(4)
    )
    _seed_honest_v2_history(
        exact_database, states=(exact_replay_state,), events=exact_events
    )
    exact_checkpoint_store = AdaptiveStateStore(exact_database)
    try:
        assert _research_loop_module._consecutive_non_novel(
            exact_checkpoint_store, exact_replay_state
        ) == 0
    finally:
        exact_checkpoint_store.close()


def test_semantic_novelty_ignores_binding_addition_after_all_items_resolved() -> None:
    _, namespace = _fixture_schema()
    current = _supported_state_after_probe(namespace, observed_at=_FIXTURE_NOW)
    current = current.model_copy(update={"unresolved_items": ()})
    added_binding = current.bindings[0].model_copy(
        update={"binding_id": "binding-variant"}
    )
    committed = SimpleNamespace(
        state=current.model_copy(
            update={"bindings": (*current.bindings, added_binding)}
        ),
        novelty=SimpleNamespace(
            added_hypothesis_ids=(),
            updated_hypothesis_ids=(),
            added_binding_ids=(added_binding.binding_id,),
            updated_binding_ids=(),
            added_join_ids=(),
            updated_join_ids=(),
            unresolved_items=(),
            stop_reason=current.stop_reason,
        ),
    )

    assert (
        _research_loop_module._is_semantically_novel_turn(current, committed)
        is False
    )


@pytest.mark.parametrize("_repeat", range(20))
def test_model_cancellation_reconciles_unknown_usage_without_leaked_task(
    tmp_path, _repeat: int
) -> None:
    entered = asyncio.Event()

    async def model(_prompt: str) -> str:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def scenario():
        task = asyncio.create_task(_run(tmp_path, _state(required=True), model))
        await entered.wait()
        task.cancel()
        task.cancel()
        outcome = await task
        await asyncio.sleep(0)
        assert task.done()
        assert all(
            candidate is asyncio.current_task() or candidate.done()
            for candidate in asyncio.all_tasks()
        )
        return outcome

    threads_before = {thread.ident for thread in threading.enumerate()}
    fds_before = len(os.listdir("/proc/self/fd"))
    outcome, state_store, checkpoint_store, ledger = asyncio.run(scenario())
    try:
        records = ledger.load_model_records("loop-run", "loop-incarnation")
        assert outcome.stop_reason is ResearchStopReason.CANCELLED
        assert len(records) == 1
        assert records[0].result is not None
        assert records[0].reconciliation is not None
        assert records[0].result.usage.input_tokens is None
        assert records[0].result.usage.output_tokens is None
    finally:
        state_store.close()
        checkpoint_store.close()
        ledger.close()
    assert {thread.ident for thread in threading.enumerate()} == threads_before
    assert len(os.listdir("/proc/self/fd")) <= fds_before


def test_observed_null_result_is_exact_only_for_semantic_commit() -> None:
    observed = {
        "contract_version": 1,
        "kind": "research_observed",
        "novel": True,
        "result": None,
        "resolution_digest": "sha256:" + "1" * 64,
    }

    assert _research_loop_module._is_semantic_observed(observed) is True
    assert _research_loop_module._is_semantic_observed(
        {**observed, "result": {}}
    ) is False


def test_saved_semantic_transition_has_no_failed_probe_feedback() -> None:
    assert (
        _research_loop_module._replay_input_has_failed_probe(
            SimpleNamespace(probe_result=None)
        )
        is False
    )


def test_reconciled_probe_lookup_allows_sparse_semantic_revision() -> None:
    action = SimpleNamespace(expected_revision=1, action_digest="probe-digest")
    reconciliation = SimpleNamespace(budget_after="budget-after")
    record = SimpleNamespace(
        reservation=SimpleNamespace(revision=1, action_digest="probe-digest"),
        reconciliation=reconciliation,
    )

    assert _research_loop_module._reconciled_record_for_action((record,), action) is record


def test_semantic_commit_skips_raw_query_admission() -> None:
    loaded_schema, namespace = _fixture_schema()
    state = _policy_state(namespace, with_evidence=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_hypothesis",
                    "proposal_key": "proposal:semantic-admission",
                    "source_ids": ("source-1",),
                    "claim": "orders are relevant",
                    "candidate_targets": (
                        {"target_kind": "table", "table": "public.orders"},
                    ),
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    assert (
        _research_loop_module._model_research_query_admission_feedback(
            state,
            decision,
            loaded_schema,
            _make_registry(namespace),
        )
        is None
    )


@pytest.mark.asyncio
async def test_semantic_transition_preserves_model_budget_for_terminal_replay_export(
    tmp_path,
    monkeypatch,
) -> None:
    from workflow.adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    loaded_schema, namespace = _fixture_schema()
    policy = _policy(2)
    state = _policy_state(namespace).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    registry = _make_registry(namespace)
    first_decision = _tool_decision("inspect_table", {"table": "public.orders"})
    prepared = _resolve_fixture(
        first_decision,
        loaded=loaded_schema,
        namespace=namespace,
        state=state,
        registry=registry,
    )
    action = prepared.admission.action
    invocation = prepared.invocation
    assert action is not None and invocation is not None
    payload = {"status": "matched"}
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id=invocation.invocation_id,
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=action.target,
        started_at=_FIXTURE_NOW,
        completed_at=_FIXTURE_NOW,
        summary="orders inspected",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )
    observed_invocation_ids: list[str] = []
    budget_ledger = AdaptiveBudgetLedger(tmp_path / "budget.sqlite")

    def execute(resolved, _tools, *, recover=False):
        assert recover is False
        runtime_action = resolved.admission.action
        runtime_invocation = resolved.invocation
        assert runtime_action is not None and runtime_invocation is not None
        observed_invocation_ids.append(runtime_invocation.invocation_id)
        runtime_result = result.model_copy(
            update={"invocation_id": runtime_invocation.invocation_id}
        )
        runtime_result, _ = execute_probe_with_budget(
            resolved.admission.state,
            runtime_action,
            runtime_result.cost,
            lambda _reservation: runtime_result,
            config=policy,
            ledger=budget_ledger,
            monotonic_ns=lambda: 0,
            utc_now=lambda: _FIXTURE_NOW,
            claim_now_ns=lambda: 0,
            owner_token_factory=lambda: "semantic-budget-probe-owner",
        )
        return runtime_result

    monkeypatch.setattr(
        _research_loop_module,
        "execute_resolved_research_decision",
        execute,
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first_decision.model_dump_json()
        assert observed_invocation_ids
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [
                    {
                        "proposal_type": "new_hypothesis",
                        "proposal_key": "proposal:semantic-budget",
                        "source_ids": ["source-1"],
                        "claim": "orders are relevant",
                        "candidate_targets": [
                            {"target_kind": "table", "table": "public.orders"},
                        ],
                        "citation_evidence_ids": [observed_invocation_ids[0]],
                    },
                ],
                "next": {"next_kind": "semantic_commit"},
            }
        )

    outcome, state_store, checkpoint_store, ledger = await _run(
        tmp_path,
        state,
        model,
        loaded_schema=loaded_schema,
        registry=registry,
        policy=policy,
        budget_ledger=budget_ledger,
    )
    solver_store = AdaptiveSolverCheckpointStore(tmp_path / "adaptive.sqlite")
    try:
        assert outcome.stop_reason is ResearchStopReason.BUDGET_EXHAUSTED
        replay_input = state_store.load_research_replay_input(
            state.run_id, state.run_incarnation, 2
        )
        assert replay_input is not None
        assert replay_input.budget_state.used_model_calls == 2
        state_store.save_query_spec(state.query_spec)
        assert build_adaptive_replay_artifact(
            state.run_id,
            state.run_incarnation,
            checkpoint_store=checkpoint_store,
            research_store=state_store,
            solver_store=solver_store,
            budget_ledger=ledger,
        )
    finally:
        solver_store.close()
        state_store.close()
        checkpoint_store.close()
        ledger.close()


def test_state_with_reconciled_model_budget_accepts_solver_generate_records(
    tmp_path,
) -> None:
    """W0-0.6: the adaptive solver's own model calls share this ledger.

    ``workflow/text_to_sql_adaptive_solver.py`` records its proposal model
    calls under ``solver-generate-<SolverState.revision>-<attempt>``, a
    revision counter independent of ``ResearchState.revision``. Those records
    must not trip the research-turn attempt-contiguity check here, and the
    shared model budget must still account for them.
    """

    _, namespace = _fixture_schema()
    state = _policy_state(namespace)
    ledger = AdaptiveBudgetLedger(tmp_path / "mixed-model-budget.sqlite")

    async def usage(_reservation) -> ModelTokenUsage:
        return ModelTokenUsage(input_tokens=None, output_tokens=None)

    _seed_prior_model_budget(state, ledger, _policy())
    asyncio.run(
        execute_model_call_with_budget_async(
            state.run_id,
            state.run_incarnation,
            "solver-generate-0-0",
            canonical_digest({"seed": "solver-revision-0"}),
            "sql_solver_agent:test/model",
            10,
            10,
            usage,
            config=_policy(),
            ledger=ledger,
            claim_now_ns=lambda: 0,
            owner_token_factory=lambda: "seed-solver-owner",
        )
    )

    projected = _state_with_reconciled_model_budget(state, ledger, _policy())

    assert projected.budget_state.used_model_calls == 2
    assert projected.budget_state.remaining_model_calls == (
        _policy().model_budget.model_calls - 2
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    (
        ("inspect_column", {"table": "public.missing", "column": "signal"}),
        ("profile_column", {"table": "public.missing", "column": "signal"}),
        (
            "search_value",
            {
                "table": "public.missing",
                "column": "signal",
                "value": "active",
                "top_k": 1,
            },
        ),
        (
            "get_distinct_values",
            {"table": "public.missing", "column": "signal", "top_k": 1},
        ),
    ),
)
def test_unresolvable_proposal_free_column_tool_names_exact_schema_candidates(
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    """Rejected single-column tools name exact schema alternatives without choosing one."""

    loaded_schema, namespace = _fixture_schema(
        {
            "public.beta": {"columns": {"signal": {"type": "TEXT"}}},
            "public.alpha": {"columns": {"signal": {"type": "TEXT"}}},
        }
    )
    state = _policy_state(namespace)
    decision = _tool_decision(tool_name, arguments)

    assert _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    ) == (
        {
            "rejected_tool": {
                "tool_name": tool_name,
                "table": "public.missing",
                "column": "signal",
            },
            "rejection_reason": "target column is not resolvable",
            "same_name_schema_columns": [
                {"table": "public.alpha", "column": "signal"},
                {"table": "public.beta", "column": "signal"},
            ],
        },
    )


@pytest.mark.parametrize(
    "tool_name,arguments",
    (
        ("inspect_column", {"table": "public.missing", "column": "absent"}),
        ("inspect_column", {"table": "public.missing", "column": "Signal"}),
        ("inspect_table", {"table": "public.missing"}),
    ),
)
def test_unresolvable_preflight_schema_candidates_require_exact_column_tool_match(
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    """No case-folded or non-column diagnostic alternative is offered."""

    loaded_schema, namespace = _fixture_schema(
        {"public.alpha": {"columns": {"signal": {"type": "TEXT"}}}}
    )
    state = _policy_state(namespace)

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state,
        _tool_decision(tool_name, arguments),
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    )
    if tool_name == "inspect_table":
        assert feedback == ()
    else:
        assert feedback == (
            {
                "rejected_tool": {
                    "tool_name": tool_name,
                    "table": arguments["table"],
                    "column": arguments["column"],
                },
                "rejection_reason": "target column is absent from captured schema",
            },
        )


def test_unresolvable_preflight_valid_column_target_has_no_schema_candidate_feedback() -> None:
    """A later resolver failure must not recast a valid target as missing."""

    loaded_schema, namespace = _fixture_schema(
        {"public.alpha": {"columns": {"signal": {"type": "TEXT"}}}}
    )
    state = _policy_state(namespace)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": {
                    "reference_kind": "existing",
                    "hypothesis_id": "hypothesis:missing",
                },
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {"table": "public.alpha", "column": "signal"},
                },
            },
        }
    )

    assert _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    ) == ()


def test_unresolvable_preflight_unqualified_resolved_table_lists_other_exact_column() -> None:
    """A known unqualified table may still be missing a column held elsewhere."""

    loaded_schema, namespace = _fixture_schema(
        {
            "main.posts": {"columns": {"post_id": {"type": "INTEGER"}}},
            "main.comments": {"columns": {"Text": {"type": "TEXT"}}},
        }
    )
    state = _policy_state(namespace)
    decision = _tool_decision(
        "inspect_column", {"table": "posts", "column": "Text"}
    )

    assert _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    ) == (
        {
            "rejected_tool": {
                "tool_name": "inspect_column",
                "table": "posts",
                "column": "Text",
            },
            "rejection_reason": "target column is not resolvable",
            "same_name_schema_columns": [
                {"table": "main.comments", "column": "Text"},
            ],
        },
    )


def test_unresolvable_preflight_case_ambiguous_table_keeps_schema_candidate_feedback() -> None:
    """A case-ambiguous table name is not a resolved raw target."""

    loaded_schema, namespace = _fixture_schema(
        {
            "main.posts": {"columns": {"signal": {"type": "TEXT"}}},
            "other.POSTS": {"columns": {"signal": {"type": "TEXT"}}},
        }
    )
    state = _policy_state(namespace)
    decision = _tool_decision(
        "inspect_column", {"table": "posts", "column": "signal"}
    )

    assert _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    ) == (
        {
            "rejected_tool": {
                "tool_name": "inspect_column",
                "table": "posts",
                "column": "signal",
            },
            "rejection_reason": "target column is not resolvable",
            "same_name_schema_columns": [
                {"table": "main.posts", "column": "signal"},
                {"table": "other.POSTS", "column": "signal"},
            ],
        },
    )


def test_unresolvable_preflight_schema_candidates_do_not_replace_proposal_feedback() -> None:
    """A proposal-bearing decision keeps its established assessment feedback."""

    loaded_schema, namespace = _fixture_schema(
        {"public.alpha": {"columns": {"signal": {"type": "TEXT"}}}}
    )
    state = _policy_state(namespace, with_evidence=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:signal",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.missing",
                            "column": "signal",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {"table": "public.missing", "column": "signal"},
                },
            },
        }
    )

    feedback = _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    )

    assert feedback == (
        {
            "proposal": decision.proposals[0].model_dump(mode="json", by_alias=True),
        },
        {
            "rejected_tool": {
                "tool_name": "inspect_column",
                "table": "public.missing",
                "column": "signal",
            },
            "rejection_reason": "target column is not resolvable",
            "same_name_schema_columns": [
                {"table": "public.alpha", "column": "signal"},
            ],
        },
    )


def test_unresolvable_preflight_reports_absent_tool_with_valid_proposal() -> None:
    loaded_schema, namespace = _fixture_schema(
        {"public.alpha": {"columns": {"signal": {"type": "TEXT"}}}}
    )
    state = _policy_state(namespace, with_evidence=True)
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:signal",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.alpha",
                            "column": "signal",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {"table": "public.alpha", "column": "missing"},
                },
            },
        }
    )

    assert _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    ) == (
        {
            "proposal": decision.proposals[0].model_dump(mode="json", by_alias=True),
        },
        {
            "rejected_tool": {
                "tool_name": "inspect_column",
                "table": "public.alpha",
                "column": "missing",
            },
            "rejection_reason": "target column is absent from captured schema",
        },
    )


def test_unresolvable_preflight_names_required_exact_discriminator_column() -> None:
    """A rejected exact-column binding tells the model which column is required."""

    loaded_schema, namespace = _fixture_schema(
        {
            "public.orders": {
                "columns": {
                    "status": {"type": "TEXT"},
                    "statuses": {"type": "TEXT"},
                }
            }
        }
    )
    state = _policy_state(namespace, with_evidence=True)
    semantic_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "exact_physical_predicate": True,
            "exact_physical_column_name": "statuses",
            "operator": PredicateOperator.EQ,
            "literal_or_reference": "active",
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (semantic_item,)}
            )
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:status",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "status",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": "active",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (state.evidence[0].evidence_id,),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    assert _research_loop_module._rejected_preflight_assessment_context(
        state,
        decision,
        _freshness(state),
        requested_action=None,
        loaded_schema=loaded_schema,
    ) == (
        {
            "proposal": decision.proposals[0].model_dump(mode="json", by_alias=True),
            "rejection_reason": (
                "discriminator binding differs from exact physical column"
            ),
            "expected_exact_physical_column_name": "statuses",
            "required_revision": (
                "replace discriminator_column.column and "
                "discriminator_predicate.left.column with the expected exact "
                "physical column name before resubmitting the proposal"
            ),
        },
    )
