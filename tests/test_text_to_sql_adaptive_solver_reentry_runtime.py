"""Production targeted re-entry does not reopen the closed research journal."""

from __future__ import annotations

from datetime import UTC, datetime
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

import custom_tools.text_to_sql.adaptive.research_reentry as reentry_module
from custom_tools.text_to_sql.adaptive._policy_model_budget import _model_started
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    DiscriminatorValueBinding,
    EvidenceCost,
    EvidenceSourceKind,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    ResearchAction,
    ResearchActionKind,
    ResearchReentryStatus,
    ResearchState,
    SemanticItemKind,
    SolverState,
)
from custom_tools.text_to_sql.adaptive.model_budget import (
    ModelBudgetLedgerRecord,
    ModelTokenUsage,
)
from custom_tools.text_to_sql.adaptive.evidence import probe_result_to_evidence
from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
from custom_tools.text_to_sql.adaptive.policy import (
    canonical_action_digest,
    execute_model_call_with_budget,
    execute_probe_with_budget,
    initial_budget_state,
    load_adaptive_policy_config,
    reserve_model_call_budget,
)
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, build_probe_result
from custom_tools.text_to_sql.adaptive.replay_inputs import (
    ResearchTerminalReplayInput,
)
from custom_tools.text_to_sql.adaptive.production_research import (
    stable_schema_research_model_identity,
)
from custom_tools.text_to_sql.adaptive.research_loop import (
    _model_call_id,
    _state_with_reconciled_model_budget,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
    serialize_contract,
)
from custom_tools.text_to_sql.adaptive.schema_research_agent import (
    ResearchStopReview,
    load_schema_research_agent_profile,
)
from custom_tools.text_to_sql.adaptive.research_decision import (
    InspectTableIntent,
    ResearchDecisionV1,
    SampleRowsIntent,
    ToolIntent,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.solver_loop import (
    admit_targeted_reentry,
    apply_solver_proposal,
)
from custom_tools.text_to_sql.adaptive.solver_protocol import (
    MissingEvidenceProposal,
    SolverProposalV1,
)
from custom_tools.text_to_sql.adaptive.tool_registry import (
    InspectTableArguments,
    SampleRowsArguments,
)
from test_text_to_sql_solver_runner import _runtime
from workflow.text_to_sql_typed_runtime import (
    TextToSqlTypedAdmission,
    TextToSqlTypedRuntime,
    _ADMISSION_CAPABILITY,
)
from workflow._text_to_sql_document_authority import empty_schema_document_registry
from workflow._text_to_sql_solver_reentry import (
    build_production_reentry_boundary,
    settle_incomplete_reentry_model_call,
)
from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
from workflow.adaptive_state_store import (
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.deadline import DeadlineBudget
from workflow.text_to_sql_typed_research import _typed_response_format


def test_solver_and_targeted_reentry_preserve_typed_model_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both solver boundaries must reach the provider with the Typed values."""

    import agent_command
    import utils
    from smolagents import ChatMessage, MessageRole
    from workflow.text_to_sql_adaptive_solver import _production_json_model

    observed: list[tuple[int, float, int, int]] = []

    def fake_call_openai_api(
        *,
        prompt: str,
        model: object,
        max_tokens: int,
        max_retries: int,
        response_format: object,
        system_prompt: str,
        temperature: float = 0.3,
    ) -> str:
        del prompt, max_retries, response_format, system_prompt
        completion = model.model._prepare_completion_kwargs(
            [ChatMessage(role=MessageRole.USER, content="typed prompt")],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        observed.append(
            (
                completion["max_tokens"],
                completion["temperature"],
                model.max_retries,
                model.model.client.max_retries,
            )
        )
        return "{}"

    monkeypatch.setenv("OPENAI_API_KEY_DB", "test-key")
    monkeypatch.setattr(utils, "call_openai_api", fake_call_openai_api)
    agent_command._get_model.cache_clear()
    try:
        runtime = SimpleNamespace(
            run_id="test-run",
            verified_research_policy=load_adaptive_policy_config(),
        )
        asyncio.run(
            _production_json_model(
                runtime,
                "model_code",
                "SQL-solver",
                "typed system prompt",
            )("x")
        )
        asyncio.run(
            _production_json_model(
                runtime,
                "model_code",
                "schema-research",
                "typed system prompt",
            )("x")
        )
    finally:
        agent_command._get_model.cache_clear()

    assert observed == [(32_000, 0.3, 0, 1), (32_000, 0.3, 0, 1)]


def test_solver_and_targeted_reentry_pass_yaml_profile_as_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The closed JSON contracts must be guided by their owning YAML profiles."""

    import agent_command
    import utils
    from custom_tools.text_to_sql.adaptive.schema_research_agent import (
        load_schema_research_agent_profile,
    )
    from custom_tools.text_to_sql.adaptive.sql_solver_agent import (
        load_sql_solver_agent_profile,
    )
    from workflow.text_to_sql_adaptive_solver import _production_json_model

    observed: list[str | None] = []

    def fake_call_openai_api(
        *,
        prompt: str,
        model: object,
        max_tokens: int,
        max_retries: int,
        response_format: object,
        system_prompt: str | None = None,
        temperature: float = 0.3,
    ) -> str:
        del prompt, model, max_tokens, max_retries, response_format, temperature
        observed.append(system_prompt)
        return "{}"

    monkeypatch.setenv("OPENAI_API_KEY_DB", "test-key")
    monkeypatch.setattr(utils, "call_openai_api", fake_call_openai_api)
    agent_command._get_model.cache_clear()
    try:
        runtime = SimpleNamespace(
            run_id="test-run",
            verified_research_policy=load_adaptive_policy_config(),
        )
        solver_profile = load_sql_solver_agent_profile()
        research_profile = load_schema_research_agent_profile()
        asyncio.run(
            _production_json_model(
                runtime,
                solver_profile.model,
                "SQL-solver",
                solver_profile.instructions,
            )("x")
        )
        asyncio.run(
            _production_json_model(
                runtime,
                research_profile.model,
                "schema-research",
                research_profile.instructions,
            )("x")
        )
    finally:
        agent_command._get_model.cache_clear()

    assert observed == [solver_profile.instructions, research_profile.instructions]


def _seed_honest_v2_research_history(path, states) -> None:
    from workflow.adaptive_research_state_store import _V2_OWNED_TABLE_SQL

    with sqlite3.connect(path) as connection:
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


def _three_call_policy():
    policy = load_adaptive_policy_config()
    values = policy.model_dump(mode="python")
    values["model_budget"]["model_calls"] = 3
    values["operation_counts"]["model_decisions"] = 3
    return type(policy).model_validate(values)


def _one_remaining_reentry_case(tmp_path):
    _, original, _, loaded_schema = _runtime()
    policy = _three_call_policy()
    actions = []
    aligned_evidence = []
    for revision, item in enumerate(original.evidence):
        observation = json.loads(item.observation)
        kind = ResearchActionKind(observation["provenance"]["probe_kind"])
        digest = canonical_action_digest(
            kind=kind,
            hypothesis_id=None,
            target=item.target,
            parameters=(),
            expected_revision=revision,
        )
        action = ResearchAction(
            action_id=f"prior-action-{revision}",
            kind=kind,
            hypothesis_id=None,
            target=item.target,
            parameters=(),
            action_digest=digest,
            expected_revision=revision,
        )
        observation["provenance"]["action_digest"] = digest
        actions.append(action)
        aligned_evidence.append(
            item.model_copy(
                update={
                    "revision": revision + 1,
                    "action_digest": digest,
                    "observation": canonical_json_bytes(observation).decode("utf-8"),
                }
            )
        )
    base = ResearchState.model_validate(
        {
            **original.model_dump(mode="python"),
            "revision": len(actions),
            "evidence": tuple(aligned_evidence),
            "action_history": tuple(actions),
            "budget_state": initial_budget_state(policy),
        }
    )
    database = tmp_path / "adaptive.sqlite"
    ledger = AdaptiveBudgetLedger(database)
    prior_cost = EvidenceCost(
        wall_clock_ms=0,
        model_calls=0,
        model_tokens=0,
        db_probe_ms=0,
        rows=0,
        bytes=2,
    )
    prior_maximum = prior_cost.model_copy(update={"wall_clock_ms": 1_000})
    prior_states = []
    current_budget = initial_budget_state(policy)
    for revision, action in enumerate(actions):
        prior = ResearchState.model_validate(
            {
                **base.model_dump(mode="python"),
                "revision": revision,
                "action_history": tuple(actions[:revision]),
                "budget_state": current_budget,
            }
        )
        prior_states.append(prior)
        prior_result = build_probe_result(
            run_id=base.run_id,
            run_incarnation=base.run_incarnation,
            revision=revision,
            schema_namespace_version=base.schema_namespace_version,
            invocation_id=f"prior-probe-{revision}",
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=action.target,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            summary="prior durable probe",
            cost=prior_cost,
            row_count=0,
            payload={},
        )
        _, prior_reconciliation = execute_probe_with_budget(
            prior,
            action,
            prior_maximum,
            lambda _reservation, result=prior_result: result,
            config=policy,
            ledger=ledger,
        )
        assert prior_reconciliation.budget_exhausted is False
        current_budget = prior_reconciliation.budget_after
    base = ResearchState.model_validate(
        {
            **base.model_dump(mode="python"),
            "budget_state": current_budget,
        }
    )
    limits = policy.model_budget
    assert limits is not None
    for revision in range(base.revision):
        execute_model_call_with_budget(
            base.run_id,
            base.run_incarnation,
            _model_call_id(base.model_copy(update={"revision": revision}), 0),
            canonical_digest({"prior": "research decision", "revision": revision}),
            stable_schema_research_model_identity("model_code"),
            limits.input_tokens_per_call,
            limits.output_tokens_per_call,
            lambda _reservation: ModelTokenUsage(input_tokens=0, output_tokens=0),
            config=policy,
            ledger=ledger,
        )
    research = _state_with_reconciled_model_budget(base, ledger, policy)
    assert research.budget_state.remaining_model_calls == 1
    freshness = FreshnessContext(
        evaluated_at=datetime.now(UTC),
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        schema_namespace_version=research.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        research,
        freshness,
        research.run_id,
        research.run_incarnation,
    )
    solver = SolverState(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        query_spec=research.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    solver = apply_solver_proposal(
        solver,
        SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id="status",
                question="Refresh status column evidence",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="One exact schema observation is required",
            ),
        ),
        base_revision=solver.revision,
        dsn="postgresql://unused",
        table_namespace="main",
        requirements=requirements,
        id_factory=iter(("request-1", "action-1")).__next__,
    ).state
    _seed_honest_v2_research_history(database, (*prior_states, research))
    research_store = AdaptiveResearchStateStore(database)
    deadline = DeadlineBudget.from_duration(30)
    scope = loaded_schema.namespace.scope.to_mapping()
    admission = TextToSqlTypedAdmission(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        deadline=deadline,
        query=research.query_spec.original_text,
        dsn="postgresql://unused",
        schema_scope=scope,
        _capability=_ADMISSION_CAPABILITY,
    )
    runtime = TextToSqlTypedRuntime(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        deadline=deadline,
        query=research.query_spec.original_text,
        dsn="postgresql://unused",
        schema_scope=scope,
        research_state_store=research_store,
        checkpoint_store=None,
        budget_ledger=ledger,
        solver_checkpoint_store=None,
        _capability=_ADMISSION_CAPABILITY,
        _admission=admission,
    )
    runtime.loaded_schema = loaded_schema
    runtime.document_registry = empty_schema_document_registry(
        loaded_schema.namespace.scope,
        loaded_schema.namespace,
    )
    checkpoint_store = AdaptiveStateStore(database)
    for revision in range(research.revision):
        key = AdaptiveCheckpointKey(
            research.run_id,
            research.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            revision,
        )
        checkpoint_store.record_planned(
            key,
            expected_revision=None if revision == 0 else revision - 1,
            action={"kind": "historical_planned", "revision": revision},
        )
        checkpoint_store.record_observed(
            key,
            expected_revision=revision,
            action={"kind": "historical_observed", "revision": revision},
        )
    checkpoint_store.record_replayable_terminal(
        AdaptiveCheckpointKey(
            research.run_id,
            research.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            research.revision,
        ),
        expected_revision=research.revision - 1,
        action={
            "affected_source_ids": [],
            "citation_evidence_ids": [],
            "contract_version": 2,
            "kind": "research_terminal",
            "rejection_signatures": [],
            "reason": "COMPLETE",
            "ambiguity": None,
        },
        replay_input=ResearchTerminalReplayInput(freshness_context=freshness),
    )
    runtime.checkpoint_store = checkpoint_store
    runtime.verified_research_policy = policy
    return runtime, solver, research, requirements, freshness


def test_value_search_reentry_cites_matching_supported_binding(tmp_path) -> None:
    runtime, solver, research, requirements, _ = _one_remaining_reentry_case(tmp_path)
    binding = requirements.selected_bindings[0]
    target = binding.columns[0]
    request = solver.missing_evidence_requests[-1].model_copy(
        update={
            "source_id": binding.source_id,
            "required_evidence_kind": EvidenceSourceKind.VALUE_SEARCH,
        }
    )
    payload = {"columns": [target.column], "rows": [["active"]]}
    result = build_probe_result(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        invocation_id="reentry-value-evidence",
        action_digest=canonical_digest({"probe": "value-search"}),
        probe_kind=ResearchActionKind.SEARCH_VALUE,
        status=ProbeStatus.SUCCESS,
        target=target,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        summary="exact value found",
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )

    import workflow._text_to_sql_solver_reentry as production_reentry

    updated = production_reentry._semantic_repair_bindings(
        (),
        research,
        request,
        result,
    )

    updated_binding = next(item for item in updated if item.binding_id == binding.binding_id)
    assert result.invocation_id in updated_binding.evidence_ids
    runtime.research_state_store.close()
    runtime.budget_ledger.close()


def test_value_search_reentry_cites_distinct_values_evidence(tmp_path) -> None:
    runtime, solver, research, requirements, _ = _one_remaining_reentry_case(tmp_path)
    original = requirements.selected_bindings[0]
    target = original.columns[0]
    binding = PhysicalColumnBinding(
        binding_id="physical-status-distinct-values",
        source_id=original.source_id,
        tables=original.tables,
        columns=(target,),
        predicates=(),
        join_path=original.join_path,
        evidence_ids=original.evidence_ids,
        confidence=original.confidence,
        status=original.status,
        validator_rule=original.validator_rule,
        physical_column=target,
    )
    query_spec = research.query_spec.model_copy(
        update={
            "semantic_items": tuple(
                item.model_copy(
                    update={
                        "operator": None,
                        "literal_or_reference": None,
                        "binding_ids": (binding.binding_id,),
                    }
                )
                if item.source_id == binding.source_id
                else item
                for item in research.query_spec.semantic_items
            )
        }
    )
    research = ResearchState.model_validate(
        {
            **research.model_dump(mode="python"),
            "query_spec": query_spec,
            "bindings": (binding,),
        }
    )
    request = solver.missing_evidence_requests[-1].model_copy(
        update={
            "source_id": binding.source_id,
            "required_evidence_kind": EvidenceSourceKind.VALUE_SEARCH,
        }
    )
    payload = {"columns": [target.column], "rows": [["active"]]}
    result = build_probe_result(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        invocation_id="reentry-distinct-values-evidence",
        action_digest=canonical_digest({"probe": "distinct-values"}),
        probe_kind=ResearchActionKind.DISTINCT_VALUES,
        status=ProbeStatus.SUCCESS,
        target=target,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        summary="distinct values found",
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )

    import workflow._text_to_sql_solver_reentry as production_reentry

    updated = production_reentry._semantic_repair_bindings(
        (),
        research,
        request,
        result,
    )

    updated_binding = next(item for item in updated if item.binding_id == binding.binding_id)
    assert isinstance(updated_binding, PhysicalColumnBinding)
    assert result.invocation_id in updated_binding.evidence_ids
    runtime.research_state_store.close()
    runtime.budget_ledger.close()


def test_value_search_reentry_materializes_exact_filter_predicate(tmp_path) -> None:
    runtime, solver, research, requirements, _ = _one_remaining_reentry_case(tmp_path)
    original = requirements.selected_bindings[0]
    column = original.columns[0]
    physical = PhysicalColumnBinding(
        binding_id="physical-status",
        source_id=original.source_id,
        tables=original.tables,
        columns=(column,),
        predicates=(),
        join_path=original.join_path,
        evidence_ids=original.evidence_ids,
        confidence=original.confidence,
        status=original.status,
        validator_rule=original.validator_rule,
        physical_column=column,
    )
    query_spec = research.query_spec.model_copy(
        update={
            "semantic_items": tuple(
                item.model_copy(
                    update={
                        "operator": None,
                        "literal_or_reference": None,
                        "binding_ids": (physical.binding_id,),
                    }
                )
                if item.source_id == physical.source_id
                else item
                for item in research.query_spec.semantic_items
            )
        }
    )
    research = ResearchState.model_validate(
        {
            **research.model_dump(mode="python"),
            "query_spec": query_spec,
            "bindings": (physical,),
        }
    )
    request = solver.missing_evidence_requests[-1].model_copy(
        update={
            "source_id": physical.source_id,
            "required_evidence_kind": EvidenceSourceKind.VALUE_SEARCH,
        }
    )
    action = ResearchAction(
        action_id="reentry-exact-filter-action",
        kind=ResearchActionKind.SEARCH_VALUE,
        hypothesis_id=None,
        target=column,
        parameters=(("top_k", 1), ("value", "active")),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.SEARCH_VALUE,
            hypothesis_id=None,
            target=column,
            parameters=(("top_k", 1), ("value", "active")),
            expected_revision=research.revision,
        ),
        expected_revision=research.revision,
    )
    payload = {"columns": [column.column], "rows": [["active"]]}
    result = build_probe_result(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        invocation_id="reentry-exact-filter-value",
        action_digest=action.action_digest,
        probe_kind=ResearchActionKind.SEARCH_VALUE,
        status=ProbeStatus.SUCCESS,
        target=column,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        summary="exact filter value found",
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )

    import workflow._text_to_sql_solver_reentry as production_reentry

    updated = production_reentry._semantic_repair_bindings(
        (),
        research,
        request,
        result,
    )

    predicate_binding = next(
        item for item in updated if isinstance(item, DiscriminatorValueBinding)
    )
    assert predicate_binding.discriminator_column == column
    assert predicate_binding.discriminator_predicate.operator is PredicateOperator.EQ
    assert predicate_binding.discriminator_predicate.right == "active"
    assert result.invocation_id in predicate_binding.evidence_ids
    from custom_tools.text_to_sql.adaptive.state import (
        ResearchTransitionConflictError,
        _merge_bindings,
    )

    with pytest.raises(
        ResearchTransitionConflictError,
        match="binding kind is immutable",
    ):
        _merge_bindings((physical,), updated)
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    merged, added, changed = _merge_bindings((physical,), updated, (evidence,))
    assert merged == (predicate_binding,)
    assert added == ()
    assert changed == (physical.binding_id,)

    dimension_spec = research.query_spec.model_copy(
        update={
            "semantic_items": tuple(
                item.model_copy(
                    update={
                        "kind": SemanticItemKind.DIMENSION,
                        "operator": None,
                        "literal_or_reference": None,
                        "binding_ids": (physical.binding_id,),
                    }
                )
                if item.source_id == physical.source_id
                else item
                for item in research.query_spec.semantic_items
            )
        }
    )
    dimension_research = ResearchState.model_validate(
        {
            **research.model_dump(mode="python"),
            "query_spec": dimension_spec,
            "bindings": (physical,),
        }
    )
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right="active",
    )
    predicate_request = request.model_copy(
        update={"predicate_authority": predicate}
    )
    predicate_updated = production_reentry._semantic_repair_bindings(
        (),
        dimension_research,
        predicate_request,
        result,
    )

    assert len(predicate_updated) == 2
    preserved = next(
        item for item in predicate_updated if item.binding_id == physical.binding_id
    )
    predicate_binding = next(
        item
        for item in predicate_updated
        if isinstance(item, DiscriminatorValueBinding)
    )
    assert isinstance(preserved, PhysicalColumnBinding)
    assert preserved.status is BindingStatus.SUPPORTED
    assert predicate_binding.binding_id != physical.binding_id
    assert predicate_binding.status is BindingStatus.CANDIDATE
    assert predicate_binding.discriminator_predicate == predicate
    assert result.invocation_id in predicate_binding.evidence_ids
    merged, added, changed = _merge_bindings(
        (physical,), predicate_updated, (evidence,)
    )
    assert {item.binding_id for item in merged} == {
        physical.binding_id,
        predicate_binding.binding_id,
    }
    assert added == (predicate_binding.binding_id,)
    assert changed == (physical.binding_id,)
    runtime.research_state_store.close()
    runtime.budget_ledger.close()


@pytest.mark.asyncio
async def test_targeted_reentry_charges_last_model_call_before_tool_admission(
    tmp_path,
    monkeypatch,
) -> None:
    runtime, solver, research, requirements, freshness = _one_remaining_reentry_case(
        tmp_path
    )
    calls = {"model": 0, "resolve": 0}
    import workflow._text_to_sql_solver_reentry as production_reentry

    original_resolve = production_reentry.resolve_research_decision

    def assert_budget_projected(state, decision, **kwargs):
        calls["resolve"] += 1
        assert state.budget_state.remaining_model_calls == 0
        return original_resolve(state, decision, **kwargs)

    monkeypatch.setattr(
        production_reentry,
        "resolve_research_decision",
        assert_budget_projected,
    )

    async def model(_prompt: str):
        import custom_tools.text_to_sql.adaptive.schema_research_agent as agent_contracts

        calls["model"] += 1
        return getattr(agent_contracts, "SchemaResearchModelResponse")(
            raw_response=ResearchDecisionV1.model_validate(
                {
                    "proposals": (),
                    "next": ToolIntent(
                        hypothesis_ref=None,
                        intent=InspectTableIntent(
                            arguments=InspectTableArguments(table="orders")
                        ),
                    ),
                }
            ).model_dump_json(),
            usage=ModelTokenUsage(input_tokens=7, output_tokens=4),
        )

    outcome = await build_production_reentry_boundary(
        runtime,
        table_namespace="main",
        model=model,
    )(
        solver,
        research,
        "request-1",
        requirements=requirements,
        freshness_context=freshness,
        commit_solver_admission=lambda transition: transition.state,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls == {"model": 1, "resolve": 1}, outcome.record.status
    assert outcome.record.status is ResearchReentryStatus.COMPLETED
    assert outcome.research_state.revision == research.revision + 1
    assert outcome.research_state.budget_state.used_model_calls == 3
    assert outcome.research_state.budget_state.remaining_model_calls == 0
    records = runtime.budget_ledger.load_model_records(
        runtime.run_id,
        runtime.run_incarnation,
    )
    assert len(records) == 3
    assert all(record.reconciliation is not None for record in records)
    assert records[-1].reconciliation.actual_usage == ModelTokenUsage(
        input_tokens=7,
        output_tokens=4,
    )
    assert records[-1].reconciliation.charged_total_tokens == 11
    assert records[-1].reconciliation.usage_was_conservative is False
    plan = runtime.research_state_store.load_prepared_targeted_reentry_commit(
        runtime.run_id,
        runtime.run_incarnation,
        "reentry-1",
    )
    assert plan is not None
    assert runtime.research_state_store.is_prepared_targeted_reentry_committed(plan)


@pytest.mark.asyncio
async def test_targeted_reentry_caps_sample_rows_reservation_at_admitted_limit(
    tmp_path,
    monkeypatch,
) -> None:
    runtime, solver, research, requirements, freshness = _one_remaining_reentry_case(
        tmp_path
    )
    assert research.budget_state.remaining_rows > 50
    request = solver.missing_evidence_requests[-1].model_copy(
        update={"required_evidence_kind": EvidenceSourceKind.SAMPLE}
    )
    solver = solver.model_copy(
        update={"missing_evidence_requests": (*solver.missing_evidence_requests[:-1], request)}
    )

    import custom_tools.text_to_sql.adaptive.probes as probes
    import custom_tools.text_to_sql.adaptive.schema_research_agent as agent_contracts
    sample_rows_called = False

    def sample_rows(target, columns, limit, *, runtime, budget):
        nonlocal sample_rows_called
        sample_rows_called = True
        assert limit == 50
        assert budget.maximum_cost.rows == 50
        payload = {
            "columns": list(columns),
            "probe_kind": budget.action.kind.value,
            "schema_namespace_version": budget.state.schema_namespace_version,
            "target": target.model_dump(mode="json", by_alias=True),
            "rows": [],
        }
        return build_probe_result(
            run_id=budget.state.run_id,
            run_incarnation=budget.state.run_incarnation,
            revision=budget.state.revision,
            schema_namespace_version=budget.state.schema_namespace_version,
            invocation_id=budget.invocation_id,
            action_digest=budget.action.action_digest,
            probe_kind=budget.action.kind,
            status=ProbeStatus.SUCCESS,
            target=target,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            summary="synthetic sample rows",
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

    monkeypatch.setattr(probes, "sample_rows", sample_rows)

    async def model(_prompt: str):
        return agent_contracts.SchemaResearchModelResponse(
            raw_response=ResearchDecisionV1.model_validate(
                {
                    "proposals": (),
                    "next": ToolIntent(
                        hypothesis_ref=None,
                        intent=SampleRowsIntent(
                            arguments=SampleRowsArguments(
                                table="orders",
                                columns=("status",),
                                limit=50,
                            )
                        ),
                    ),
                }
            ).model_dump_json(),
            usage=ModelTokenUsage(input_tokens=7, output_tokens=4),
        )

    outcome = await build_production_reentry_boundary(
        runtime,
        table_namespace="main",
        model=model,
    )(
        solver,
        research,
        "request-1",
        requirements=requirements,
        freshness_context=freshness,
        commit_solver_admission=lambda transition: transition.state,
        id_factory=iter(("reentry-sample-rows",)).__next__,
    )

    assert sample_rows_called
    assert outcome.record.status is not ResearchReentryStatus.TOOL_FAILURE


@pytest.mark.asyncio
async def test_targeted_reentry_replay_settles_started_model_without_provider(
    tmp_path,
) -> None:
    runtime, solver, research, requirements, freshness = _one_remaining_reentry_case(
        tmp_path
    )
    request = solver.missing_evidence_requests[-1]
    trusted_targets = reentry_module._trusted_targets(
        request.source_id,
        research,
        requirements,
    )
    research_context = reentry_module._research_context(
        request,
        trusted_targets,
        research,
    )
    limits = runtime.verified_research_policy.model_budget
    assert limits is not None
    profile = load_schema_research_agent_profile()
    reservation = reserve_model_call_budget(
        research.run_id,
        research.run_incarnation,
        _model_call_id(research, 0),
        canonical_digest(
            {
                "research_context": research_context,
                "state": research.model_dump(mode="json", by_alias=True),
                "task": request.question,
            }
        ),
        stable_schema_research_model_identity(profile.model),
        limits.input_tokens_per_call,
        limits.output_tokens_per_call,
        config=runtime.verified_research_policy,
        ledger=runtime.budget_ledger,
    )
    owner = "crashed-owner"
    claim = runtime.budget_ledger.claim_model_execution(
        reservation,
        owner,
        now_ns=1,
    )
    assert claim.acquired is True
    runtime.budget_ledger.record_model_started(
        _model_started(
            reservation,
            "crashed-invocation",
            claim.generation,
            2,
        ),
        owner_token=owner,
    )
    calls = {"model": 0}

    async def model(_prompt: str) -> str:
        calls["model"] += 1
        raise AssertionError("STARTED replay must not call the provider again")

    outcome = await build_production_reentry_boundary(
        runtime,
        table_namespace="main",
        model=model,
    )(
        solver,
        research,
        "request-1",
        requirements=requirements,
        freshness_context=freshness,
        commit_solver_admission=lambda transition: transition.state,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls == {"model": 0}
    assert outcome.record.status is ResearchReentryStatus.BUDGET_EXHAUSTED
    assert outcome.research_state.revision == research.revision
    assert outcome.research_state.budget_state.remaining_model_calls == 0
    records = runtime.budget_ledger.load_model_records(
        runtime.run_id,
        runtime.run_incarnation,
    )
    assert len(records) == 3
    assert records[-1].reconciliation is not None


@pytest.mark.asyncio
async def test_targeted_reentry_leaves_closed_w3_checkpoint_untouched(tmp_path) -> None:
    runtime, solver, research, requirements, freshness = _one_remaining_reentry_case(
        tmp_path
    )
    policy = runtime.verified_research_policy
    limits = policy.model_budget
    assert limits is not None
    execute_model_call_with_budget(
        research.run_id,
        research.run_incarnation,
        _model_call_id(research, 0),
        canonical_digest({"exhaust": "last targeted model call"}),
        stable_schema_research_model_identity("model_code"),
        limits.input_tokens_per_call,
        limits.output_tokens_per_call,
        lambda _reservation: ModelTokenUsage(input_tokens=0, output_tokens=0),
        config=policy,
        ledger=runtime.budget_ledger,
    )
    exhausted = _state_with_reconciled_model_budget(
        research,
        runtime.budget_ledger,
        policy,
    )
    assert exhausted.budget_state.remaining_model_calls == 0
    requirements = validate_coverage_inputs(
        exhausted,
        freshness,
        exhausted.run_id,
        exhausted.run_incarnation,
    )
    database = tmp_path / "adaptive.sqlite"
    checkpoint_store = AdaptiveStateStore(database)
    for revision in range(exhausted.revision):
        key = AdaptiveCheckpointKey(
            exhausted.run_id,
            exhausted.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            revision,
        )
        checkpoint_store.record_planned(
            key,
            expected_revision=None if revision == 0 else revision - 1,
            action={"kind": "historical_planned", "revision": revision},
        )
        checkpoint_store.record_observed(
            key,
            expected_revision=revision,
            action={"kind": "historical_observed", "revision": revision},
        )
    closed_key = AdaptiveCheckpointKey(
        exhausted.run_id,
        exhausted.run_incarnation,
        AdaptiveLoopKind.RESEARCH,
        exhausted.revision,
    )
    checkpoint_store.record_replayable_terminal(
        closed_key,
        expected_revision=exhausted.revision - 1,
        action={
            "affected_source_ids": [],
            "citation_evidence_ids": [],
            "contract_version": 2,
            "kind": "research_terminal",
            "rejection_signatures": [],
            "reason": "COMPLETE",
            "ambiguity": None,
        },
        replay_input=ResearchTerminalReplayInput(freshness_context=freshness),
    )
    before = checkpoint_store.list_events(closed_key)
    runtime.checkpoint_store = checkpoint_store

    async def model(_prompt: str) -> str:
        raise AssertionError("exhausted re-entry must not call the model")

    boundary = build_production_reentry_boundary(
        runtime,
        table_namespace="main",
        model=model,
    )
    outcome = await boundary(
        solver,
        exhausted,
        "request-1",
        requirements=requirements,
        freshness_context=freshness,
        commit_solver_admission=lambda transition: transition.state,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert outcome.record.status is ResearchReentryStatus.BUDGET_EXHAUSTED
    assert outcome.research_state == exhausted
    assert checkpoint_store.list_events(closed_key) == before
    assert checkpoint_store.get_snapshot(closed_key).planned is None
    assert checkpoint_store.get_snapshot(closed_key).observed is None


@pytest.mark.asyncio
async def test_reentry_continuation_routes_stop_review_through_its_own_model(
    tmp_path,
    monkeypatch,
) -> None:
    """Research continuation on the targeted re-entry path must review its stop
    decision through a dedicated ResearchStopReview json_schema route, not the
    ResearchDecisionV1 route the research-decision model uses. Reusing the
    research-decision model for the stop review is the same class of defect
    W0-0.4 fixed on the direct typed-research path (a flat model without
    $defs forced through a response_format shaped for a different, nested
    model)."""

    runtime, solver, research, _requirements, _freshness = _one_remaining_reentry_case(
        tmp_path
    )
    request = solver.missing_evidence_requests[-1]
    profile = load_schema_research_agent_profile()

    import agent_command
    import workflow._text_to_sql_solver_reentry as production_reentry

    class _NoOpMemoryManager:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def find_semantic_relevant_tables(self, *_args, **_kwargs):
            return ()

        def find_verified_probe_facts(self, *_args, **_kwargs):
            return ()

        def find_approved_semantic_facts(self, *_args, **_kwargs):
            return ()

    monkeypatch.setattr(production_reentry, "SchemaMemoryManager", _NoOpMemoryManager)

    class _StoppedBeforeLoop(Exception):
        """Raised once the loop assembly is reached, to avoid running it."""

    captured_assembly: dict[str, object] = {}

    def fake_assemble(**kwargs):
        captured_assembly.update(kwargs)
        raise _StoppedBeforeLoop

    monkeypatch.setattr(
        production_reentry, "assemble_production_research", fake_assemble
    )

    captured_provider_calls: list[dict[str, object]] = []

    class _FakeProvider:
        def __call__(self, messages, *, max_tokens, temperature, response_format):
            captured_provider_calls.append(
                {
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "response_format": response_format,
                }
            )
            return '{"decision":"stop_confirmed","hint":null}'

    monkeypatch.setattr(
        agent_command,
        "create_text_to_sql_model",
        lambda *_args, **_kwargs: _FakeProvider(),
    )

    async def research_decision_model(_prompt: str) -> str:
        raise AssertionError(
            "this test only exercises the continuation's model wiring"
        )

    with pytest.raises(_StoppedBeforeLoop):
        await production_reentry._continue_production_research(
            runtime,
            "main",
            research_decision_model,
            profile,
            research,
            request,
        )

    assert "stop_review_model" in captured_assembly
    stop_review_model = captured_assembly["stop_review_model"]
    assert stop_review_model is not None
    assert stop_review_model is not research_decision_model

    response = await stop_review_model("stop review prompt")
    assert response.raw_response == '{"decision":"stop_confirmed","hint":null}'

    assert len(captured_provider_calls) == 1
    observed_response_format = captured_provider_calls[0]["response_format"]
    assert observed_response_format == _typed_response_format(
        ResearchStopReview, "ResearchStopReview"
    )
    assert observed_response_format["type"] == "json_schema"
    assert observed_response_format["json_schema"]["name"] == "ResearchStopReview"
    assert observed_response_format != {"type": "json_object"}

    runtime.research_state_store.close()
    runtime.budget_ledger.close()


@pytest.mark.asyncio
async def test_reentry_continuation_stop_review_model_comes_from_step_models_registry(
    tmp_path,
    monkeypatch,
) -> None:
    """W1-1.1: the stop-review model alias on the re-entry continuation path is
    read from ``llm_models.yaml::step_models.research_stop_review`` — not
    hardcoded to ``profile.model``. Overriding the registry must change the
    alias passed to ``create_text_to_sql_model`` for the stop-review model,
    while ``stable_schema_research_model_identity`` (a separate ledger-identity
    concern, built from ``profile.model`` directly) stays unaffected.
    """
    from custom_tools.text_to_sql import llm_models_config

    cfg_path = tmp_path / "llm_models.yaml"
    cfg_path.write_text(
        "version: 1\n"
        "profiles:\n"
        "  default:\n"
        "    schema_linking: {}\n"
        "    sql_generation: {}\n"
        "    nlu: {}\n"
        "    step_models:\n"
        "      research_stop_review: model_lite\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT_TO_SQL_LLM_MODELS_PATH", str(cfg_path))
    llm_models_config.reset_cache()
    try:
        runtime, solver, research, _requirements, _freshness = _one_remaining_reentry_case(
            tmp_path
        )
        request = solver.missing_evidence_requests[-1]
        profile = load_schema_research_agent_profile()

        import agent_command
        import workflow._text_to_sql_solver_reentry as production_reentry

        class _NoOpMemoryManager:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def find_semantic_relevant_tables(self, *_args, **_kwargs):
                return ()

            def find_verified_probe_facts(self, *_args, **_kwargs):
                return ()

            def find_approved_semantic_facts(self, *_args, **_kwargs):
                return ()

        monkeypatch.setattr(production_reentry, "SchemaMemoryManager", _NoOpMemoryManager)

        class _StoppedBeforeLoop(Exception):
            """Raised once the loop assembly is reached, to avoid running it."""

        captured_assembly: dict[str, object] = {}

        def fake_assemble(**kwargs):
            captured_assembly.update(kwargs)
            raise _StoppedBeforeLoop

        monkeypatch.setattr(
            production_reentry, "assemble_production_research", fake_assemble
        )

        captured_names: list[str] = []

        class _FakeProvider:
            def __call__(self, messages, *, max_tokens, temperature, response_format):
                return '{"decision":"stop_confirmed","hint":null}'

        monkeypatch.setattr(
            agent_command,
            "create_text_to_sql_model",
            lambda name, *_args, **_kwargs: (captured_names.append(name), _FakeProvider())[-1],
        )

        async def research_decision_model(_prompt: str) -> str:
            raise AssertionError(
                "this test only exercises the continuation's model wiring"
            )

        with pytest.raises(_StoppedBeforeLoop):
            await production_reentry._continue_production_research(
                runtime,
                "main",
                research_decision_model,
                profile,
                research,
                request,
            )

        assert captured_names == ["model_lite"]
        assert captured_assembly["model_identity"] == stable_schema_research_model_identity(
            "model_code"
        )

        runtime.research_state_store.close()
        runtime.budget_ledger.close()
    finally:
        monkeypatch.delenv("TEXT_TO_SQL_LLM_MODELS_PATH", raising=False)
        llm_models_config.reset_cache()


def test_settle_incomplete_reentry_model_call_ignores_stray_solver_generate_record(
    tmp_path,
) -> None:
    """W0-0.6 remark B: the outstanding-record count settle_incomplete_reentry_model_call
    uses (workflow/_text_to_sql_solver_reentry.py) must ignore ledger records
    outside the targeted-research call_id prefixes. Since W0-0.6 the adaptive
    solver's own "solver-generate-*" calls share this ledger; before the fix,
    one such record alongside the genuinely outstanding targeted-research
    reservation made `len(outstanding) != 1`, so the function silently
    returned the research state unsettled.

    A genuinely outstanding "solver-generate-*" record is not this function's
    job either way: it is settled by the sibling
    ``settle_incomplete_solver_model_call`` (same module), called separately
    from ``_resume_open_generation``. This test only pins down that the
    prefix filter here keeps ignoring that prefix, not that nothing else in
    the codebase ever settles it.
    """

    runtime, solver, research, requirements, _ = _one_remaining_reentry_case(tmp_path)
    admitted = admit_targeted_reentry(
        solver,
        research,
        "request-1",
        base_revision=solver.revision,
        id_factory=iter(("reentry-1",)).__next__,
    )
    solver_state = admitted.state

    request = solver_state.missing_evidence_requests[-1]
    context = reentry_module._research_context(
        request,
        reentry_module._trusted_targets(request.source_id, research, requirements),
        research,
    )
    profile = load_schema_research_agent_profile()
    limits = runtime.verified_research_policy.model_budget
    assert limits is not None

    reservation = reserve_model_call_budget(
        research.run_id,
        research.run_incarnation,
        _model_call_id(research, 0),
        canonical_digest(
            {
                "research_context": context,
                "state": research.model_dump(mode="json", by_alias=True),
                "task": request.question,
            }
        ),
        stable_schema_research_model_identity(profile.model),
        limits.input_tokens_per_call,
        limits.output_tokens_per_call,
        config=runtime.verified_research_policy,
        ledger=runtime.budget_ledger,
    )
    owner = "crashed-owner"
    claim = runtime.budget_ledger.claim_model_execution(reservation, owner, now_ns=1)
    assert claim.acquired is True
    runtime.budget_ledger.record_model_started(
        _model_started(reservation, "crashed-invocation", claim.generation, 2),
        owner_token=owner,
    )

    # The real ledger allows only one outstanding (unreconciled) reservation
    # at a time (workflow/adaptive_budget_ledger.py), so a genuine
    # "solver-generate-0-0" reservation cannot coexist, unreconciled, with the
    # research one above. It is injected only into the *first*
    # load_model_records() read below -- the exact read
    # settle_incomplete_reentry_model_call uses to compute `outstanding`
    # (workflow/_text_to_sql_solver_reentry.py ~619-630) -- so the test pins
    # down that the prefix filter, not incidental ledger state, is what makes
    # settlement succeed.
    scratch_ledger = AdaptiveBudgetLedger(tmp_path / "scratch-ledger.sqlite")
    stray_reservation = reserve_model_call_budget(
        research.run_id,
        research.run_incarnation,
        "solver-generate-0-0",
        canonical_digest({"solver": "stray"}),
        "sql_solver_agent:stray-model",
        limits.input_tokens_per_call,
        limits.output_tokens_per_call,
        config=runtime.verified_research_policy,
        ledger=scratch_ledger,
    )
    stray_record = ModelBudgetLedgerRecord(
        reservation=stray_reservation,
        started=None,
        result=None,
        reconciliation=None,
    )
    real_ledger = runtime.budget_ledger

    class _LedgerWithStrayOnFirstRead:
        def __init__(self) -> None:
            self._reads = 0

        def load_model_records(self, run_id: str, run_incarnation: str):
            records = real_ledger.load_model_records(run_id, run_incarnation)
            self._reads += 1
            if self._reads == 1:
                return (*records, stray_record)
            return records

        def __getattr__(self, name: str):
            return getattr(real_ledger, name)

    runtime.budget_ledger = _LedgerWithStrayOnFirstRead()

    updated = asyncio.run(
        settle_incomplete_reentry_model_call(
            runtime, solver_state, research, requirements
        )
    )

    settled_records = real_ledger.load_model_records(
        research.run_id, research.run_incarnation
    )
    settled_by_call_id = {
        record.reservation.call_id: record for record in settled_records
    }
    target_call_id = _model_call_id(research, 0)
    assert target_call_id in settled_by_call_id
    assert settled_by_call_id[target_call_id].reconciliation is not None
    assert not any(
        call_id.startswith("solver-generate-") for call_id in settled_by_call_id
    )
    assert (
        updated.budget_state.used_model_calls
        == research.budget_state.used_model_calls + 1
    )

    runtime.research_state_store.close()
    real_ledger.close()
    scratch_ledger.close()


def test_settle_incomplete_reentry_model_call_logs_model_identity_change(
    tmp_path, caplog
) -> None:
    """W5: if the outstanding STARTED reservation for the exact targeted-
    research call (same call_id and request_digest) was made under a model
    identity that no longer matches what the current schema_research profile
    resolves to -- e.g. an operator switched the step's model between a crash
    and this resume -- settle_incomplete_reentry_model_call must still stay
    conservative (leave the call unsettled, return the research state
    unchanged), but it must log a WARNING naming the old/new model identity
    instead of settling (or failing) silently.
    """

    runtime, solver, research, requirements, _ = _one_remaining_reentry_case(tmp_path)
    admitted = admit_targeted_reentry(
        solver,
        research,
        "request-1",
        base_revision=solver.revision,
        id_factory=iter(("reentry-1",)).__next__,
    )
    solver_state = admitted.state

    request = solver_state.missing_evidence_requests[-1]
    context = reentry_module._research_context(
        request,
        reentry_module._trusted_targets(request.source_id, research, requirements),
        research,
    )
    profile = load_schema_research_agent_profile()
    limits = runtime.verified_research_policy.model_budget
    assert limits is not None
    expected_model_identity = stable_schema_research_model_identity(profile.model)
    stale_model_identity = "schema_research_agent:stale-pre-config-change-model"
    assert stale_model_identity != expected_model_identity

    reservation = reserve_model_call_budget(
        research.run_id,
        research.run_incarnation,
        _model_call_id(research, 0),
        canonical_digest(
            {
                "research_context": context,
                "state": research.model_dump(mode="json", by_alias=True),
                "task": request.question,
            }
        ),
        stale_model_identity,
        limits.input_tokens_per_call,
        limits.output_tokens_per_call,
        config=runtime.verified_research_policy,
        ledger=runtime.budget_ledger,
    )
    owner = "crashed-owner"
    claim = runtime.budget_ledger.claim_model_execution(reservation, owner, now_ns=1)
    assert claim.acquired is True
    runtime.budget_ledger.record_model_started(
        _model_started(reservation, "crashed-invocation", claim.generation, 2),
        owner_token=owner,
    )

    with caplog.at_level("WARNING"):
        updated = asyncio.run(
            settle_incomplete_reentry_model_call(
                runtime, solver_state, research, requirements
            )
        )

    assert updated is research
    unsettled_records = runtime.budget_ledger.load_model_records(
        research.run_id, research.run_incarnation
    )
    target_call_id = _model_call_id(research, 0)
    unsettled_by_call_id = {
        record.reservation.call_id: record for record in unsettled_records
    }
    assert unsettled_by_call_id[target_call_id].reconciliation is None
    assert any(
        "changed between incarnations" in record.message
        and stale_model_identity in record.message
        and expected_model_identity in record.message
        for record in caplog.records
    )

    runtime.research_state_store.close()
    runtime.budget_ledger.close()


def test_settle_incomplete_reentry_model_call_settles_reserved_without_started(
    tmp_path,
) -> None:
    """W0-0.6 remark D: a crash between RESERVED and STARTED must not durably
    block the targeted-research reentry either.

    ``record_model_reservation``, ``claim_model_execution`` and
    ``record_model_started`` (``workflow/adaptive_budget_ledger.py``) are
    three separate durable sqlite transactions, so a process killed right
    after the first one leaves the targeted-research call_id's ledger record
    with ``started is None, reconciliation is None`` -- a different crash
    window than the STARTED-without-RESULT one the other tests in this file
    cover. Before this fix, ``settle_incomplete_reentry_model_call`` bailed
    out on ``outstanding[0].started is None``, so resuming from this window
    left the call unsettled forever and every later reservation attempt hit
    ``BudgetConflictError``.
    """

    runtime, solver, research, requirements, _ = _one_remaining_reentry_case(tmp_path)
    admitted = admit_targeted_reentry(
        solver,
        research,
        "request-1",
        base_revision=solver.revision,
        id_factory=iter(("reentry-1",)).__next__,
    )
    solver_state = admitted.state

    request = solver_state.missing_evidence_requests[-1]
    context = reentry_module._research_context(
        request,
        reentry_module._trusted_targets(request.source_id, research, requirements),
        research,
    )
    profile = load_schema_research_agent_profile()
    limits = runtime.verified_research_policy.model_budget
    assert limits is not None

    target_call_id = _model_call_id(research, 0)
    # Durably record RESERVED only -- no claim, no STARTED -- emulating a
    # process kill right after the reservation's own sqlite transaction
    # commits, before ``claim_model_execution``/``record_model_started`` ever
    # ran.
    reserve_model_call_budget(
        research.run_id,
        research.run_incarnation,
        target_call_id,
        canonical_digest(
            {
                "research_context": context,
                "state": research.model_dump(mode="json", by_alias=True),
                "task": request.question,
            }
        ),
        stable_schema_research_model_identity(profile.model),
        limits.input_tokens_per_call,
        limits.output_tokens_per_call,
        config=runtime.verified_research_policy,
        ledger=runtime.budget_ledger,
    )
    crashed_records = runtime.budget_ledger.load_model_records(
        research.run_id, research.run_incarnation
    )
    # ``_one_remaining_reentry_case`` already durably records one fully
    # reconciled "research-model-*" call per prior revision, so the ledger is
    # not empty before this test's own RESERVED-only record is added -- only
    # the record count must not grow across the settle calls below.
    crashed_by_call_id = {
        record.reservation.call_id: record for record in crashed_records
    }
    assert crashed_by_call_id[target_call_id].started is None
    assert crashed_by_call_id[target_call_id].result is None
    assert crashed_by_call_id[target_call_id].reconciliation is None
    total_before_settle = len(crashed_records)

    updated = asyncio.run(
        settle_incomplete_reentry_model_call(
            runtime, solver_state, research, requirements
        )
    )

    settled_records = runtime.budget_ledger.load_model_records(
        research.run_id, research.run_incarnation
    )
    settled_by_call_id = {
        record.reservation.call_id: record for record in settled_records
    }
    assert len(settled_records) == total_before_settle
    assert settled_by_call_id[target_call_id].started is not None
    assert settled_by_call_id[target_call_id].reconciliation is not None
    # Settled with unknown (maximum-charged) usage, not a real model response.
    assert (
        settled_by_call_id[target_call_id].reconciliation.actual_usage.input_tokens
        is None
    )
    assert (
        updated.budget_state.used_model_calls
        == research.budget_state.used_model_calls + 1
    )

    # A redundant settle attempt must not create extra reservations: nothing
    # is outstanding anymore, so this is a no-op.
    replayed = asyncio.run(
        settle_incomplete_reentry_model_call(runtime, solver_state, updated, requirements)
    )
    final_records = runtime.budget_ledger.load_model_records(
        research.run_id, research.run_incarnation
    )
    assert len(final_records) == total_before_settle
    assert replayed.budget_state.used_model_calls == updated.budget_state.used_model_calls

    runtime.research_state_store.close()
    runtime.budget_ledger.close()
