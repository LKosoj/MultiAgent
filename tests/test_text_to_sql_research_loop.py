"""Focused W3-07 contracts for the durable asynchronous research loop."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive import research_loop as _research_loop_module
from custom_tools.text_to_sql.adaptive.freshness import (
    DocumentSourceAvailability,
    DocumentSourceState,
    FreshnessContext,
    FreshnessReason,
    evaluate_evidence_freshness,
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
from custom_tools.text_to_sql.adaptive.research_decision import ResearchDecisionV1
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
            "research_context": lambda current, _feedbacks, _rejected=(): canonical_digest(
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
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (formula,)}
            )
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
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (formula,)}
            )
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
        status=SemanticItemStatus.UNRESOLVED,
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


def test_complete_uses_current_terminal_freshness_and_replays_it(
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
        assert replay_input.freshness_context.evaluated_at == t2

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


def test_unselected_candidate_does_not_defer_automatic_complete(tmp_path) -> None:
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
        update={"bindings": (*state.bindings, candidate)}
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
        )
    )
    try:
        assert outcome.stop_reason is ResearchStopReason.COMPLETE
        assert calls == 0
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
                update={"semantic_items": (formula,)}
            ),
            "evidence": (*state.evidence, candidate_evidence),
            "bindings": (*state.bindings, candidate),
            "action_history": (*state.action_history, candidate_action),
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


def test_model_complete_uses_post_response_freshness_and_replays_it(
    tmp_path, monkeypatch
) -> None:
    t0 = _FIXTURE_NOW
    t1 = datetime(2026, 7, 31, 12, 1, tzinfo=UTC)
    t2 = datetime(2026, 7, 31, 12, 2, tzinfo=UTC)
    _, namespace = _fixture_schema()
    initial = _policy_state(namespace)
    state = _supported_state_after_probe(namespace, observed_at=t1)
    freshness = _fixture_freshness(state).model_copy(update={"evaluated_at": t0})
    after_model_response = False

    class _TerminalClock:
        @classmethod
        def now(cls, zone):
            assert zone is UTC
            return t2 if after_model_response else t0

    monkeypatch.setattr(
        _research_loop_module,
        "datetime",
        _TerminalClock,
        raising=False,
    )
    calls = 0

    async def model(_prompt: str) -> str:
        nonlocal after_model_response, calls
        calls += 1
        after_model_response = True
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": [],
                "next": {
                    "next_kind": "stop",
                    "reason": "complete",
                    "source_ids": [],
                    "citation_evidence_ids": [state.evidence[0].evidence_id],
                },
            }
        )

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
        assert calls == 1
        key = AdaptiveCheckpointKey(
            state.run_id,
            state.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            state.revision,
        )
        replay_input = checkpoint_store.load_terminal_replay_input(key)
        assert type(replay_input) is ResearchTerminalReplayInput
        assert replay_input.freshness_context.evaluated_at == t2

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


def test_complete_does_not_backdate_an_expired_document(tmp_path, monkeypatch) -> None:
    t0 = _FIXTURE_NOW
    t1 = datetime(2026, 7, 31, 12, 1, tzinfo=UTC)
    expires_at = datetime(2026, 7, 31, 12, 2, tzinfo=UTC)
    t2 = datetime(2026, 7, 31, 12, 3, tzinfo=UTC)
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

    class _TerminalClock:
        @classmethod
        def now(cls, zone):
            assert zone is UTC
            return t2

    real_authority = _research_loop_module.evaluate_research_generation_authority
    evaluated_at: list[datetime] = []

    def capture_authority(*args, **kwargs):
        evaluated_at.append(args[1].evaluated_at)
        return real_authority(*args, **kwargs)

    monkeypatch.setattr(
        _research_loop_module,
        "datetime",
        _TerminalClock,
        raising=False,
    )
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
        assert outcome.stop_reason is not ResearchStopReason.COMPLETE
        assert t2 in evaluated_at
        terminal_context = freshness.model_copy(update={"evaluated_at": t2})
        assert (
            evaluate_evidence_freshness(state.evidence[0], terminal_context).reason
            is FreshnessReason.SOURCE_EXPIRED
        )
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
def test_model_semantic_stop_rejects_affected_source_subset(
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
            )
        )
        assert outcome.stop_reason is ResearchStopReason.STAGNATED
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
    tmp_path, caplog
) -> None:
    policy = _policy(6)
    state = _state(required=True).model_copy(
        update={"budget_state": initial_budget_state(policy)}
    )
    calls = 0
    prompts: list[str] = []

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
                "reason": "ambiguous",
                "source_ids": ["source-1"],
                "citation_evidence_ids": ["citation-1"],
                "ambiguity": {
                    "interpretations": ["First reading.", "Second reading."],
                    "citation_evidence_ids": ["citation-1"],
                    "missing_distinguishing_fact": "The definition is absent.",
                },
            },
    }

    async def model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        prompts.append(prompt)
        payload = stop_with_proposals if calls % 2 else invalid_stop
        return json.dumps(payload)

    with caplog.at_level(logging.WARNING, logger=_research_loop_module.__name__):
        outcome, state_store, checkpoint_store, ledger = asyncio.run(
            _run(tmp_path, state, model, policy=policy)
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
                }
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


def test_rejected_preflight_feedback_keeps_new_binding_proposal_unchanged() -> None:
    """A rejected new binding is returned verbatim for the bounded retry."""

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

    assert feedback[0]["proposal"] == decision.proposals[0].model_dump(
        mode="json", by_alias=True
    )
    assert feedback[0]["rejection_reason"] == (
        "FILTER without operator cannot use discriminator_value"
    )


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


def test_rejected_new_binding_feedback_names_operatorless_filter() -> None:
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
        "FILTER without operator cannot use discriminator_value"
    )


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

    ambiguous = state_with_candidates(("binding-candidate", "binding-other"))
    assert _research_loop_module._normalize_model_source_ids(
        ambiguous, unknown
    ) is unknown

    multiple_unknown = decision("binding-unknown", "binding-other")
    assert _research_loop_module._normalize_model_source_ids(
        unique, multiple_unknown
    ) is multiple_unknown

    valid = decision("binding-candidate")
    assert _research_loop_module._normalize_model_source_ids(unique, valid) is valid

    mixed = decision("binding-candidate", "binding-candidatf")
    assert _research_loop_module._normalize_model_source_ids(unique, mixed) is mixed

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
    assert (
        _research_loop_module._normalize_model_source_ids(
            unique, mixed_reference_kinds
        )
        is mixed_reference_kinds
    )


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

    assert ambiguous.proposals[0].source_id == "source-x"
    assert two_changes.proposals[0].source_id == "source-xx"


def test_rejected_duplicate_existing_bindings_name_every_exact_new_proposal(
    tmp_path,
) -> None:
    """Every repeated exact binding must receive the closed duplicate reason."""

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
    binding = next(
        item for item in bindings if item.physical_column.column == "status"
    )
    id_binding = next(item for item in bindings if item.physical_column.column == "id")
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
    contexts: list[tuple[dict[str, object], ...]] = []

    def research_context(
        current: ResearchState,
        _feedbacks: tuple[str, ...],
        _rejected_duplicates: tuple[dict[str, object], ...] = (),
        rejected_preflight_assessments: tuple[dict[str, object], ...] = (),
    ) -> str:
        contexts.append(rejected_preflight_assessments)
        return json.dumps(
            {
                "state": canonical_digest(current),
                "rejected_preflight_assessments": list(
                    rejected_preflight_assessments
                ),
            }
        )

    async def model(prompt: str) -> str:
        research_context = json.loads(json.loads(prompt)["input"]["research_context"])
        rejected = research_context.get("rejected_preflight_assessments", ())
        if rejected:
            existing_binding_ids = {
                item["proposal"]["proposal_key"]: item["existing_binding_id"]
                for item in rejected
                if item["proposal"].get("proposal_key")
                in {"proposal:status", "proposal:status-copy"}
            }
            assert existing_binding_ids == {
                "proposal:status": binding.binding_id,
                "proposal:status-copy": id_binding.binding_id,
            }
            return json.dumps(
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
                            "citation_evidence_ids": (citation,),
                        }
                        for binding_id in existing_binding_ids.values()
                    ),
                    "next": {"next_kind": "semantic_commit"},
                }
            )
        return json.dumps(
            {
                "decision_version": 1,
                "proposals": (
                    new_binding,
                    second_new_binding,
                    {
                        "proposal_type": "binding_assessment",
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": binding.binding_id,
                        },
                        "certificate": "consistent",
                        "citation_evidence_ids": (citation,),
                    },
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
        duplicate_feedback = tuple(
            item
            for item in contexts[1]
            if item["proposal"].get("proposal_key")
            in {"proposal:status", "proposal:status-copy"}
        )
        assert len(duplicate_feedback) == 2
        assert all(
            item["rejection_reason"] == "binding already exists"
            and "missing_probe" not in item
            for item in duplicate_feedback
        )
        assert {
            item["proposal"]["proposal_key"]: item["existing_binding_id"]
            for item in duplicate_feedback
        } == {
            "proposal:status": binding.binding_id,
            "proposal:status-copy": id_binding.binding_id,
        }
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
    preflights = iter(
        (
            (
                "UNRESOLVABLE_PREFLIGHT",
                None,
                None,
                ({"missing_probe": {"tool_name": "inspect_column"}},),
            ),
            (
                "DUPLICATE_ACTION",
                None,
                {"kind": "inspect_table"},
                (),
            ),
        )
    )

    def preflight(_self, _state, _decision):
        return next(preflights)

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
        if len(prompts) < 3:
            return (
                '{"decision_version":1,"proposals":[],"next":'
                '{"next_kind":"tool","hypothesis_ref":null,"intent":'
                '{"tool_name":"inspect_table","arguments":'
                '{"table":"public.orders"}}}}'
            )
        return (
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"stop","reason":"ambiguous",'
            '"source_ids":["source-1"],"citation_evidence_ids":[],'
            '"ambiguity":{"interpretations":["First reading.","Second reading."],'
            '"citation_evidence_ids":[],"missing_distinguishing_fact":'
            '"The definition is absent."}}}'
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
        assert outcome.stop_reason is not ResearchStopReason.PROTOCOL_FAILURE
        assert len(prompts) >= 3
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
                        "operator": "eq",
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
            '"source_ids":["source-1"],"citation_evidence_ids":["%s"],'
            '"ambiguity":%s}}' % (citation, json.dumps(ambiguity))
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
            registry=registry,
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
            research_context=lambda current, current_feedbacks, rejected=(): (
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
                research_context=lambda current, _feedbacks: canonical_digest(current),
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
    assert len(os.listdir("/proc/self/fd")) == fds_before


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
