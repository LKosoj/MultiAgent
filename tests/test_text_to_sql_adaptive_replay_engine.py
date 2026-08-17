from __future__ import annotations

import json
from hashlib import sha256

import pytest

from custom_tools.text_to_sql.adaptive.models import ResearchState, SolverState
from custom_tools.text_to_sql.adaptive.replay_inputs import (
    ResearchTerminalReplayInput,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)
from test_text_to_sql_adaptive_replay import (
    INCARNATION,
    RUN_ID,
    _decode_trusted,
    _forbid_external_replay,
    _freshness,
    _repack_replay_document,
    _replay_trusted,
    _research_state,
    _solver_state,
)
from test_text_to_sql_durable_replay_inputs import (
    _create_honest_v2_database,
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


def _use_synchronous_sql_parser(monkeypatch) -> None:
    from custom_tools.text_to_sql.adaptive import sql_ast
    from custom_tools.text_to_sql.adaptive._sql_ast_builder import (
        parse_and_build_candidate,
    )
    from custom_tools.text_to_sql.adaptive._sql_ast_models import AstLimits

    def parse(
        sql,
        dialect,
        candidate_id,
        *,
        max_ast_nodes,
        max_ast_depth,
    ):
        return parse_and_build_candidate(
            sql,
            dialect,
            candidate_id,
            AstLimits(max_nodes=max_ast_nodes, max_depth=max_ast_depth),
        )

    monkeypatch.setattr(sql_ast, "_parse_candidate_isolated", parse)


def test_w7_01_pure_replay_verifies_minimal_durable_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
    )
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    db_path = tmp_path / "w7-replay.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    research_state = _research_state(revision=0)
    solver_state = _solver_state(revision=0)
    research_store.save_query_spec(research_state.query_spec)
    research_store.save_research_state(
        research_state,
        expected_previous_revision=None,
    )
    checkpoint_store.record_replayable_terminal(
        AdaptiveCheckpointKey(
            RUN_ID,
            INCARNATION,
            AdaptiveLoopKind.RESEARCH,
            0,
        ),
        expected_revision=None,
        action={
            "contract_version": 2,
            "kind": "research_terminal",
            "rejection_signatures": [],
            "reason": "STAGNATED",
            "affected_source_ids": [],
            "citation_evidence_ids": [],
            "ambiguity": None,
        },
        replay_input=ResearchTerminalReplayInput(freshness_context=_freshness()),
    )
    solver_store.initialize(solver_state)

    raw = build_adaptive_replay_artifact(
        RUN_ID,
        INCARNATION,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    _forbid_external_replay(monkeypatch)
    artifact = _decode_trusted(raw)
    result = _replay_trusted(raw)

    assert result.status is HistoricalReplayStatus.VERIFIED
    assert (
        result.research_state_digest == artifact.payload.research_snapshots[-1].digest
    )
    assert result.solver_state_digest == artifact.payload.solver_snapshots[-1].digest


def test_exported_research_transition_replays_with_exact_budget_record(
    tmp_path,
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive.policy import (
        BudgetReservation,
        reconcile_probe_cost,
    )
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
    )
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    db_path = tmp_path / "typed-research-replay.db"
    before, after, replay_input = _research_replay_case()
    linked_input = _record_research_journal(db_path, before, replay_input)
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    research_store.save_query_spec(before.query_spec)
    research_store.save_research_state(before, expected_previous_revision=None)
    action = after.action_history[-1]
    reservation_values = {
        "run_id": before.run_id,
        "run_incarnation": before.run_incarnation,
        "revision": before.revision,
        "schema_namespace_version": before.schema_namespace_version,
        "action_digest": action.action_digest,
        "probe_kind": action.kind,
        "target": action.target,
        "policy_digest": "sha256:" + "9" * 64,
        "budget_before": before.budget_state,
        "maximum_cost": linked_input.probe_result.cost,
    }
    reservation = BudgetReservation(
        **reservation_values,
        reservation_digest=canonical_digest(reservation_values),
    )
    budget_ledger.record_reservation(reservation)
    assert budget_ledger.claim_execution(
        reservation,
        "replay-test-owner",
        now_ns=0,
    )
    budget_ledger.record_result(
        reservation,
        linked_input.probe_result,
        owner_token="replay-test-owner",
    )
    reconciliation = reconcile_probe_cost(reservation, linked_input.probe_result)
    budget_ledger.record_reconciliation(
        reconciliation,
        linked_input.probe_result,
    )
    linked_input = linked_input.model_copy(
        update={"budget_state": reconciliation.budget_after}
    )
    after = ResearchState.model_validate(
        {
            **after.model_dump(mode="python"),
            "budget_state": reconciliation.budget_after,
        }
    )
    research_store.save_replayable_semantic_transition(
        before,
        after,
        linked_input,
    )

    raw = build_adaptive_replay_artifact(
        before.run_id,
        before.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    _forbid_external_replay(monkeypatch)
    artifact = _decode_trusted(raw)
    result = _replay_trusted(raw)

    assert result.status is HistoricalReplayStatus.VERIFIED
    assert result.research_state_digest == canonical_digest(after)
    assert len(artifact.payload.budget_records) == 1


def test_exported_solver_stop_and_terminal_use_pure_reducer(
    tmp_path,
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive.models import SolverStopReason
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
    )
    from custom_tools.text_to_sql.adaptive.solver_loop import stop_solver
    from custom_tools.text_to_sql.adaptive.terminal import solver_stop_terminal_result
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    db_path = tmp_path / "solver-stop-replay.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    initial = _solver_state(revision=0)
    stopped = stop_solver(
        initial,
        SolverStopReason.STAGNATED,
        base_revision=initial.revision,
    )
    research_store.save_query_spec(initial.query_spec)
    solver_store.initialize(initial)
    checkpoint = solver_store.commit_non_execution(
        initial,
        stopped,
        action_revision=0,
        action={"kind": "solver_stop", "reason": "STAGNATED"},
    )
    terminal = solver_stop_terminal_result(stopped.run_id, stopped)
    assert terminal is not None
    solver_store.record_terminal(
        stopped,
        expected_action_revision=checkpoint.cursor.next_action_revision,
        terminal_bytes=canonical_json_bytes(terminal.to_mapping()),
    )

    raw = build_adaptive_replay_artifact(
        initial.run_id,
        initial.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    _forbid_external_replay(monkeypatch)
    _decode_trusted(raw)
    result = _replay_trusted(raw)

    assert result.status is HistoricalReplayStatus.VERIFIED
    assert result.solver_state_digest == canonical_digest(stopped)


def test_exported_solver_proposal_and_check_replay_from_typed_inputs(
    tmp_path,
    monkeypatch,
) -> None:
    from test_text_to_sql_solver_runner import _check, _runtime
    from text_to_sql_semantic_checks_helpers import POSTGRES_DSN

    from custom_tools.text_to_sql.adaptive.models import CheckKind
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
        ReplayContractError,
    )
    from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal
    from custom_tools.text_to_sql.adaptive.solver_protocol import (
        SolverProposalV1,
        SqlCandidateProposal,
    )
    from custom_tools.text_to_sql.adaptive.solver_results import (
        append_solver_check_result,
    )
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    _use_synchronous_sql_parser(monkeypatch)
    expected_candidate, _, requirements, _ = _runtime()
    initial = SolverState.model_validate(
        {
            **expected_candidate.model_dump(mode="python"),
            "revision": expected_candidate.revision - 1,
            "sql_candidates": (),
            "action_history": (),
        }
    )
    proposal_transition = apply_solver_proposal(
        initial,
        SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT o.status FROM orders o WHERE o.status = 'active'",
            ),
        ),
        base_revision=initial.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=requirements,
        id_factory=iter(("candidate-1", "plan-1", "action-1")).__next__,
    )
    assert proposal_transition.state == expected_candidate
    check_result = _check("candidate-1", CheckKind.SAFETY)
    check_transition = append_solver_check_result(
        expected_candidate,
        check_result,
        base_revision=expected_candidate.revision,
    )
    db_path = tmp_path / "solver-proposal-check-replay.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    research_store.save_query_spec(initial.query_spec)
    solver_store.initialize(initial)
    first = solver_store.commit_non_execution(
        initial,
        expected_candidate,
        action_revision=0,
        action=proposal_transition.action.model_dump(mode="json"),
        replay_input=proposal_transition.replay_input,
    )
    solver_store.commit_non_execution(
        expected_candidate,
        check_transition.state,
        action_revision=first.cursor.next_action_revision,
        action={
            "kind": "solver_check",
            "check": check_result.model_dump(mode="json"),
        },
    )

    raw = build_adaptive_replay_artifact(
        initial.run_id,
        initial.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    _forbid_external_replay(monkeypatch)
    _decode_trusted(raw)
    result = _replay_trusted(raw)

    assert result.status is HistoricalReplayStatus.VERIFIED
    assert result.solver_state_digest == canonical_digest(check_transition.state)

    document = json.loads(raw)
    proposal_step = document["payload"]["solver_steps"][0]
    proposal_step["action"]["action_id"] = "forged-action"
    proposal_step["action_digest"] = canonical_digest(proposal_step["action"])
    forged = _repack_replay_document(document)
    with pytest.raises(
        ReplayContractError,
        match="solver proposal action does not match reducer",
    ):
        _replay_trusted(forged)


def test_exported_missing_evidence_proposal_replays_from_typed_input(
    tmp_path,
    monkeypatch,
) -> None:
    from test_text_to_sql_solver_loop import (
        _case,
        _ids,
        _missing,
        _state,
    )
    from text_to_sql_semantic_checks_helpers import POSTGRES_DSN

    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
    )
    from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal
    from custom_tools.text_to_sql.adaptive.terminal import solver_stop_terminal_result
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    _use_synchronous_sql_parser(monkeypatch)
    case = _case()
    initial = _state(case)
    transition = apply_solver_proposal(
        initial,
        _missing(),
        base_revision=initial.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=case.requirements,
        id_factory=_ids("request-replay", "action-replay"),
    )
    db_path = tmp_path / "solver-missing-evidence-replay.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    research_store.save_query_spec(initial.query_spec)
    solver_store.initialize(initial)
    checkpoint = solver_store.commit_non_execution(
        initial,
        transition.state,
        action_revision=0,
        action=transition.action.model_dump(mode="json"),
        replay_input=transition.replay_input,
    )
    terminal = solver_stop_terminal_result(initial.run_id, transition.state)
    assert terminal is not None
    solver_store.record_terminal(
        transition.state,
        expected_action_revision=checkpoint.cursor.next_action_revision,
        terminal_bytes=canonical_json_bytes(terminal.to_mapping()),
    )

    raw = build_adaptive_replay_artifact(
        initial.run_id,
        initial.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    _forbid_external_replay(monkeypatch)
    _decode_trusted(raw)
    result = _replay_trusted(raw)

    assert result.status is HistoricalReplayStatus.VERIFIED
    assert result.solver_state_digest == canonical_digest(transition.state)


def test_exported_known_execution_replays_without_calling_finalizer(
    tmp_path,
    monkeypatch,
) -> None:
    from test_text_to_sql_adaptive_solver import _ready_state, _successful_terminal

    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
        ReplayContractError,
    )
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact
    from workflow.text_to_sql_adaptive_solver import reconcile_known_finalizer

    _use_synchronous_sql_parser(monkeypatch)
    db_path = tmp_path / "known-execution-replay.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    state = _ready_state()
    candidate = state.sql_candidates[-1]
    request = {
        "operation": "finalize_text_to_sql_run",
        "sql_query": candidate.sql,
        "row_limit": 10,
        "dry_run_only": False,
    }
    research_store.save_query_spec(state.query_spec)
    solver_store.initialize(state)
    reservation = solver_store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-replay-1",
        request=request,
    )
    checkpoint = reconcile_known_finalizer(
        solver_store,
        reservation,
        state,
        _successful_terminal(state),
    )

    raw = build_adaptive_replay_artifact(
        state.run_id,
        state.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    _forbid_external_replay(monkeypatch)
    _decode_trusted(raw)
    result = _replay_trusted(raw)

    assert result.status is HistoricalReplayStatus.VERIFIED
    assert result.solver_state_digest == canonical_digest(checkpoint.state)

    without_terminal = json.loads(raw)
    without_terminal["payload"]["solver_terminal"] = None
    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(without_terminal))


def test_exported_result_contradiction_execution_replays_open_s1(
    tmp_path,
    monkeypatch,
) -> None:
    from test_text_to_sql_adaptive_solver import (
        _ready_state,
        _result_contradiction_receipt,
    )

    from custom_tools.text_to_sql.adaptive.replay import HistoricalReplayStatus
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact
    from workflow.text_to_sql_adaptive_solver import reconcile_known_finalizer

    _use_synchronous_sql_parser(monkeypatch)
    db_path = tmp_path / "result-contradiction-replay.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    state = _ready_state()
    candidate = state.sql_candidates[-1]
    research_store.save_query_spec(state.query_spec)
    solver_store.initialize(state)
    reservation = solver_store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="result-contradiction-replay-1",
        request={
            "operation": "finalize_text_to_sql_run",
            "sql_query": candidate.sql,
            "row_limit": 10,
            "dry_run_only": False,
        },
    )
    checkpoint = reconcile_known_finalizer(
        solver_store,
        reservation,
        state,
        _result_contradiction_receipt(state).model_dump(mode="json"),
    )
    assert checkpoint.terminal is None

    raw = build_adaptive_replay_artifact(
        state.run_id,
        state.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    _forbid_external_replay(monkeypatch)
    _decode_trusted(raw)
    result = _replay_trusted(raw)

    assert result.status is HistoricalReplayStatus.VERIFIED
    assert result.solver_state_digest == canonical_digest(checkpoint.state)


def test_exported_unknown_execution_replays_as_tool_failure(
    tmp_path,
    monkeypatch,
) -> None:
    from test_text_to_sql_adaptive_solver import _ready_state

    from custom_tools.text_to_sql.adaptive.models import SolverStopReason
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
        ReplayContractError,
    )
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact
    from workflow.text_to_sql_adaptive_solver import (
        reconcile_reserved_finalizer_unknown,
    )

    _use_synchronous_sql_parser(monkeypatch)
    db_path = tmp_path / "unknown-execution-replay.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    state = _ready_state()
    candidate = state.sql_candidates[-1]
    research_store.save_query_spec(state.query_spec)
    solver_store.initialize(state)
    reservation = solver_store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-replay-unknown",
        request={
            "operation": "finalize_text_to_sql_run",
            "sql_query": candidate.sql,
            "row_limit": 10,
            "dry_run_only": False,
        },
    )
    checkpoint = reconcile_reserved_finalizer_unknown(
        solver_store,
        reservation,
        state,
    )

    raw = build_adaptive_replay_artifact(
        state.run_id,
        state.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    _forbid_external_replay(monkeypatch)
    _decode_trusted(raw)
    result = _replay_trusted(raw)

    assert checkpoint.state.stop_reason is SolverStopReason.TOOL_FAILURE
    assert result.status is HistoricalReplayStatus.VERIFIED
    assert result.solver_state_digest == canonical_digest(checkpoint.state)

    for field, value, message in (
        ("candidate_id", "forged-candidate", "reservation does not match state"),
        (
            "normalized_ast_digest",
            "sha256:" + "0" * 64,
            "reservation does not match state",
        ),
        (
            "request.sql_query",
            "SELECT 'forged'",
            "request does not match reserved candidate",
        ),
    ):
        document = json.loads(raw)
        execution_step = document["payload"]["solver_steps"][0]
        if field == "request.sql_query":
            execution_step["action"]["request"]["sql_query"] = value
        else:
            execution_step["action"][field] = value
        execution_step["action_digest"] = canonical_digest(execution_step["action"])
        forged = _repack_replay_document(document)
        with pytest.raises(ReplayContractError, match=message):
            _replay_trusted(forged)

    without_terminal = json.loads(raw)
    without_terminal["payload"]["solver_terminal"] = None
    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(without_terminal))

    non_null_result = json.loads(raw)
    result_blob = non_null_result["payload"]["solver_steps"][0]["reconciliation"][
        "result"
    ]
    result_blob.update(
        {
            "byte_count": 2,
            "content_base64": "e30=",
            "digest": "sha256:" + sha256(b"{}").hexdigest(),
        }
    )
    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(non_null_result))


def test_exported_reentry_admission_and_failure_replay_from_research_root(
    tmp_path,
    monkeypatch,
) -> None:
    from test_text_to_sql_durable_replay_inputs import _solver_reentry_case

    from custom_tools.text_to_sql.adaptive.models import ResearchReentryStatus
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
        ReplayContractError,
    )
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        SolverReentryAdmissionReplayInput,
    )
    from custom_tools.text_to_sql.adaptive.solver_loop import finalize_targeted_reentry
    from custom_tools.text_to_sql.adaptive.terminal import (
        solver_stop_terminal_result,
    )
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    solver_store, research, _, before, admitted = _solver_reentry_case(tmp_path)
    db_path = tmp_path / "reentry.sqlite"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    research_store.save_query_spec(research.query_spec)
    admission_input = SolverReentryAdmissionReplayInput(
        research_state_revision=research.revision,
        research_state_digest=canonical_digest(research),
        missing_evidence_request_id="request-reentry",
        generated_reentry_id="reentry-1",
    )
    first = solver_store.commit_non_execution(
        before,
        admitted.state,
        action_revision=0,
        action={
            "kind": "research_reentry_admitted",
            "record": admitted.record.model_dump(mode="json"),
        },
        replay_input=admission_input,
    )
    failed = finalize_targeted_reentry(
        admitted.state,
        admitted.record.research_reentry_id,
        ResearchReentryStatus.PROTOCOL_FAILURE,
        base_revision=admitted.state.revision,
    )
    second = solver_store.commit_non_execution(
        admitted.state,
        failed.state,
        action_revision=first.cursor.next_action_revision,
        action={
            "kind": "research_reentry_finalized",
            "record": failed.record.model_dump(mode="json"),
        },
    )
    terminal = solver_stop_terminal_result(before.run_id, failed.state)
    assert terminal is not None
    solver_store.record_terminal(
        failed.state,
        expected_action_revision=second.cursor.next_action_revision,
        terminal_bytes=canonical_json_bytes(terminal.to_mapping()),
    )

    raw = build_adaptive_replay_artifact(
        before.run_id,
        before.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    _forbid_external_replay(monkeypatch)
    _decode_trusted(raw)
    result = _replay_trusted(raw)

    assert result.status is HistoricalReplayStatus.VERIFIED
    assert result.solver_state_digest == canonical_digest(failed.state)

    for index, message in (
        (0, "re-entry admission action does not match reducer"),
        (1, "re-entry finalization action does not match reducer"),
    ):
        document = json.loads(raw)
        step = document["payload"]["solver_steps"][index]
        step["action"]["record"]["ordinal"] = 2
        step["action_digest"] = canonical_digest(step["action"])
        forged = _repack_replay_document(document)
        with pytest.raises(ReplayContractError, match=message):
            _replay_trusted(forged)


def test_exported_completed_reentry_replays_with_fresh_research_root(
    tmp_path,
    monkeypatch,
) -> None:
    from test_text_to_sql_solver_loop import (
        _case,
        _fresh_research,
        _ids,
        _missing_state,
    )
    from text_to_sql_semantic_coverage_helpers import _context as coverage_context

    from custom_tools.text_to_sql.adaptive.models import ResearchReentryStatus
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
    )
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        SolverReentryCompletedReplayInput,
    )
    from custom_tools.text_to_sql.adaptive.semantic_coverage import (
        validate_coverage_inputs,
    )
    from custom_tools.text_to_sql.adaptive.solver_loop import (
        admit_targeted_reentry,
        finalize_targeted_reentry,
    )
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    _use_synchronous_sql_parser(monkeypatch)
    case = _case()
    stopped = _missing_state(case)
    admitted = admit_targeted_reentry(
        stopped,
        case.state,
        "request-1",
        base_revision=stopped.revision,
        id_factory=_ids("reentry-1"),
    )
    fresh = _fresh_research(case)
    freshness = coverage_context()
    requirements = validate_coverage_inputs(
        fresh,
        freshness,
        fresh.run_id,
        fresh.run_incarnation,
    )
    completed = finalize_targeted_reentry(
        admitted.state,
        admitted.record.research_reentry_id,
        ResearchReentryStatus.COMPLETED,
        base_revision=admitted.state.revision,
        research_state=fresh,
        freshness_context=freshness,
        requirements=requirements,
    )
    db_path = tmp_path / "completed-reentry-replay.db"
    _create_honest_v2_database(db_path)
    _insert_v2_research_snapshot(db_path, fresh)
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    research_store.save_query_spec(fresh.query_spec)
    monkeypatch.setattr(
        research_store,
        "load_research_state_chain",
        lambda *_args: (fresh,),
    )
    solver_store.initialize(admitted.state)
    solver_store.commit_non_execution(
        admitted.state,
        completed.state,
        action_revision=0,
        action={
            "kind": "research_reentry_finalized",
            "record": completed.record.model_dump(mode="json"),
        },
        replay_input=SolverReentryCompletedReplayInput(
            research_reentry_id=admitted.record.research_reentry_id,
            research_state_revision=fresh.revision,
            research_state_digest=canonical_digest(fresh),
            freshness_context=freshness,
            requirements=requirements,
        ),
    )

    raw = build_adaptive_replay_artifact(
        stopped.run_id,
        stopped.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    _forbid_external_replay(monkeypatch)
    _decode_trusted(raw)
    result = _replay_trusted(raw)

    assert result.status is HistoricalReplayStatus.VERIFIED
    assert result.solver_state_digest == canonical_digest(completed.state)
