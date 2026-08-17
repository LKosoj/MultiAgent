from __future__ import annotations

import pytest

from custom_tools.text_to_sql.adaptive.models import ResearchState
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from test_text_to_sql_adaptive_replay import (
    INCARNATION,
    RUN_ID,
    _decode_trusted,
    _query_spec,
    _replay_trusted,
    _research_state,
)
from test_text_to_sql_durable_replay_inputs import (
    _create_honest_v2_database,
    _insert_v2_checkpoint_event,
    _insert_v2_research_snapshot,
    _record_research_journal,
    _research_replay_case,
)
from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
from workflow.adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore
from workflow.adaptive_state_store import (
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)


def test_legacy_terminal_is_explicitly_unverifiable_and_never_partial(
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
        LegacyReplayReason,
        ReplayContractError,
    )
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    db_path = tmp_path / "legacy-replay.db"
    state = _research_state(revision=0)
    key = AdaptiveCheckpointKey(
        RUN_ID,
        INCARNATION,
        AdaptiveLoopKind.RESEARCH,
        0,
    )
    terminal_action = {
        "contract_version": 1,
        "kind": "research_terminal",
        "reason": "STAGNATED",
        "affected_source_ids": [],
        "citation_evidence_ids": [],
    }
    _create_honest_v2_database(db_path)
    _insert_v2_research_snapshot(db_path, state)
    _insert_v2_checkpoint_event(db_path, key, "terminal", terminal_action)
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    research_store.save_query_spec(state.query_spec)

    with pytest.raises(ReplayContractError, match="research journal action"):
        build_adaptive_replay_artifact(
            RUN_ID,
            INCARNATION,
            checkpoint_store=checkpoint_store,
            research_store=research_store,
            solver_store=solver_store,
            budget_ledger=budget_ledger,
        )


def test_export_rejects_concurrent_authority_mutation(tmp_path, monkeypatch) -> None:
    from custom_tools.text_to_sql.adaptive.replay import ReplayContractError
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    db_path = tmp_path / "concurrent-export.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    state = _research_state(revision=0)
    research_store.save_query_spec(state.query_spec)
    research_store.save_research_state(state, expected_previous_revision=None)
    original = research_store.load_query_spec_chain
    calls = 0

    def changing_query_chain(run_id, run_incarnation):
        nonlocal calls
        calls += 1
        chain = original(run_id, run_incarnation)
        return chain if calls == 1 else (*chain, _query_spec(revision=1))

    monkeypatch.setattr(
        research_store,
        "load_query_spec_chain",
        changing_query_chain,
    )

    with pytest.raises(ReplayContractError, match="changed during export"):
        build_adaptive_replay_artifact(
            RUN_ID,
            INCARNATION,
            checkpoint_store=checkpoint_store,
            research_store=research_store,
            solver_store=solver_store,
            budget_ledger=budget_ledger,
        )


def test_export_rejects_missing_input_after_first_v3_research_transition(
    tmp_path,
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive.replay import ReplayContractError
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    db_path = tmp_path / "post-v3-gap.db"
    before, after, replay_input = _research_replay_case()
    linked_input = _record_research_journal(db_path, before, replay_input)
    research_store = AdaptiveResearchStateStore(db_path)
    research_store.save_query_spec(before.query_spec)
    research_store.save_research_state(before, expected_previous_revision=None)
    research_store.save_replayable_semantic_transition(
        before,
        after,
        linked_input,
    )
    prior_action = after.action_history[-1]
    next_parameters = (("gap", "post-v3"),)
    next_action = prior_action.model_copy(
        update={
            "action_id": "post-v3-gap-action",
            "parameters": next_parameters,
            "action_digest": canonical_action_digest(
                kind=prior_action.kind,
                hypothesis_id=prior_action.hypothesis_id,
                target=prior_action.target,
                parameters=next_parameters,
                expected_revision=1,
            ),
            "expected_revision": 1,
        }
    )
    after_gap = ResearchState.model_validate(
        {
            **after.model_dump(mode="python"),
            "revision": 2,
            "action_history": (*after.action_history, next_action),
        }
    )
    stored_chain = research_store.load_research_state_chain(
        before.run_id,
        before.run_incarnation,
    )
    original_input_loader = research_store.load_research_replay_input
    monkeypatch.setattr(
        research_store,
        "load_research_state_chain",
        lambda *_args: (*stored_chain, after_gap),
    )
    monkeypatch.setattr(
        research_store,
        "load_research_replay_input",
        lambda run_id, run_incarnation, revision: (
            original_input_loader(run_id, run_incarnation, revision)
            if revision == 1
            else None
        ),
    )

    with pytest.raises(ReplayContractError, match="gap follows v3"):
        build_adaptive_replay_artifact(
            before.run_id,
            before.run_incarnation,
            checkpoint_store=AdaptiveStateStore(db_path),
            research_store=research_store,
            solver_store=AdaptiveSolverCheckpointStore(db_path),
            budget_ledger=AdaptiveBudgetLedger(db_path),
        )
