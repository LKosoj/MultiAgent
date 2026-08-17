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
    EvidenceCost,
    EvidenceSourceKind,
    ResearchAction,
    ResearchActionKind,
    ResearchReentryStatus,
    ResearchState,
    SolverState,
)
from custom_tools.text_to_sql.adaptive.model_budget import ModelTokenUsage
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
    load_schema_research_agent_profile,
)
from custom_tools.text_to_sql.adaptive.research_decision import (
    InspectTableIntent,
    ResearchDecisionV1,
    ToolIntent,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal
from custom_tools.text_to_sql.adaptive.solver_protocol import (
    MissingEvidenceProposal,
    SolverProposalV1,
)
from custom_tools.text_to_sql.adaptive.tool_registry import InspectTableArguments
from test_text_to_sql_solver_runner import _runtime
from workflow.text_to_sql_typed_runtime import (
    TextToSqlTypedAdmission,
    TextToSqlTypedRuntime,
    _ADMISSION_CAPABILITY,
)
from workflow._text_to_sql_document_authority import empty_schema_document_registry
from workflow._text_to_sql_solver_reentry import (
    build_production_reentry_boundary,
)
from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
from workflow.adaptive_state_store import (
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.deadline import DeadlineBudget


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
    research_context = reentry_module._research_context(request, trusted_targets)
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
