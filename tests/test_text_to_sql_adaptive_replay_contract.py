from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.models import ResearchState, ResearchStopReason
from custom_tools.text_to_sql.adaptive.policy import (
    BudgetLedgerRecord,
    BudgetReservation,
    reconcile_probe_cost,
)
from custom_tools.text_to_sql.adaptive.model_budget import (
    ModelBudgetLedgerRecord,
    ModelTokenUsage,
)
from custom_tools.text_to_sql.adaptive.policy import (
    execute_model_call_with_budget,
    reserve_model_call_budget,
)
from custom_tools.text_to_sql.adaptive.probes import ProbeResult
from custom_tools.text_to_sql.adaptive.research_decision import ResearchDecisionV1
from custom_tools.text_to_sql.adaptive.replay import (
    AdaptiveReplayPayload,
    ReplayContractError,
    ResearchReplayTerminal,
    ResearchTerminalReplayAction,
    encode_replay_artifact,
)
from custom_tools.text_to_sql.adaptive.replay_contract import (
    FinalizerExecutionRequest,
    ResearchObservedReplayAction,
    ResearchPlannedReplayAction,
    ResearchReplayTransition,
    durable_action_digest,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
)
from test_text_to_sql_adaptive_replay import (
    _decode_trusted,
    _minimal_payload,
    _repack_replay_document,
)
from test_adaptive_model_budget import _config
from test_text_to_sql_durable_replay_inputs import (
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
from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact


def _semantic_only_replay_transition() -> ResearchReplayTransition:
    from custom_tools.text_to_sql.adaptive.replay_inputs import ResearchSemanticReplayInput
    from custom_tools.text_to_sql.adaptive.semantic_reducer import commit_semantic_turn
    from text_to_sql_decision_resolver_helpers import freshness, make_state, resolve, schema

    loaded, namespace = schema()
    before = make_state(namespace, with_evidence=True, required=False)
    decision = ResearchDecisionV1.model_validate(
        {
            "proposals": (
                {
                    "proposal_type": "new_hypothesis",
                    "proposal_key": "proposal:semantic-replay",
                    "source_ids": ("source-1",),
                    "claim": "orders are relevant",
                    "candidate_targets": (
                        {"target_kind": "table", "table": "public.orders"},
                    ),
                    "citation_evidence_ids": ("evidence-1",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )
    resolved = resolve(decision, loaded=loaded, namespace=namespace, state=before)
    committed = commit_semantic_turn(resolved.admission)
    action = resolved.admission.action
    assert action is not None
    planned = ResearchPlannedReplayAction(
        action=action,
        decision=decision,
        invocation_id=None,
        resolution_digest=resolved.resolution_digest,
        state_digest=canonical_digest(before),
    )
    observed = ResearchObservedReplayAction(
        novel=committed.novelty.is_novel,
        result=None,
        resolution_digest=resolved.resolution_digest,
    )
    replay_input = ResearchSemanticReplayInput(
        decision=decision,
        semantic_batch=resolved.semantic_batch,
        freshness_context=freshness(before),
        tool_claim=None,
        budget_state=before.budget_state,
        planned_action_digest=durable_action_digest(planned),
        observed_action_digest=durable_action_digest(observed),
        probe_result=None,
    )
    return ResearchReplayTransition(
        predecessor_revision=before.revision,
        predecessor_digest=canonical_digest(before),
        successor_revision=committed.state.revision,
        successor_digest=canonical_digest(committed.state),
        planned=planned,
        planned_digest=durable_action_digest(planned),
        observed=observed,
        observed_digest=durable_action_digest(observed),
        replay_input=replay_input,
    )


def test_semantic_only_replay_artifact_has_no_probe_reference() -> None:
    from workflow.text_to_sql_adaptive_replay import _artifact_references

    transition = _semantic_only_replay_transition()

    assert transition.replay_input.probe_result is None
    assert _artifact_references((transition,), ()) == ()


def test_old_finalizer_request_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        FinalizerExecutionRequest.model_validate(
            {
                "sql_query": "SELECT 1",
                "verification_status": "passed",
                "row_limit": 1,
                "dry_run_only": False,
            }
        )

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"
    assert exc_info.value.errors()[0]["loc"] == ("verification_status",)


def _typed_research_artifact(tmp_path) -> bytes:
    db_path = tmp_path / "typed-contract-replay.db"
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
    assert budget_ledger.claim_execution(reservation, "contract-owner", now_ns=0)
    budget_ledger.record_result(
        reservation,
        linked_input.probe_result,
        owner_token="contract-owner",
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
    return build_adaptive_replay_artifact(
        before.run_id,
        before.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )


def _shift_probe_record(
    record: BudgetLedgerRecord,
    *,
    revision: int,
    action_digest: str | None = None,
) -> BudgetLedgerRecord:
    reservation_values = record.reservation.model_dump(
        mode="python",
        exclude={"contract_version", "reservation_digest"},
    )
    reservation_values["revision"] = revision
    if action_digest is not None:
        reservation_values["action_digest"] = action_digest
    reservation = BudgetReservation(
        **reservation_values,
        reservation_digest=canonical_digest(reservation_values),
    )
    assert record.result is not None
    result_values = record.result.model_dump(mode="python")
    result_values["revision"] = revision
    result_values["action_digest"] = reservation.action_digest
    result = ProbeResult.model_validate(result_values)
    return BudgetLedgerRecord(
        reservation=reservation,
        result=result,
        reconciliation=reconcile_probe_cost(reservation, result),
    )


def test_direct_payload_rejects_missing_terminal_input_after_typed_research(
    tmp_path,
) -> None:
    raw = _typed_research_artifact(tmp_path)
    payload = _decode_trusted(raw).payload
    final = payload.research_snapshots[-1]
    action = ResearchTerminalReplayAction(
        contract_version=2,
        kind="research_terminal",
        reason=ResearchStopReason.STAGNATED,
        affected_source_ids=tuple(
            sorted(
                item.source_id
                for item in final.state.query_spec.semantic_items
                if item.required
            )
        ),
        citation_evidence_ids=tuple(
            sorted(item.evidence_id for item in final.state.evidence)
        ),
        ambiguity=None,
        rejection_signatures=(),
    )
    terminal = ResearchReplayTerminal(
        state_revision=final.state.revision,
        state_digest=final.digest,
        action=action,
        action_digest=durable_action_digest(action),
        replay_input=None,
    )

    with pytest.raises(ValidationError, match="terminal input is missing"):
        AdaptiveReplayPayload.model_validate(
            {
                **payload.model_dump(mode="python"),
                "research_terminal": terminal,
            }
        )


@pytest.mark.parametrize(
    "missing_field",
    ("contract_version", "kind", "rejection_signatures"),
)
def test_v2_terminal_replay_action_rejects_missing_required_fields(
    missing_field: str,
) -> None:
    payload = {
        "contract_version": 2,
        "kind": "research_terminal",
        "reason": "STAGNATED",
        "affected_source_ids": [],
        "citation_evidence_ids": [],
        "ambiguity": None,
        "rejection_signatures": [],
    }
    del payload[missing_field]
    with pytest.raises(ValidationError, match=missing_field):
        ResearchTerminalReplayAction.model_validate_json(
            json.dumps(payload)
        )


@pytest.mark.parametrize(
    "mutation",
    ("duplicate", "unreferenced", "gap", "reordered"),
)
def test_decoder_rejects_invalid_probe_budget_chain(
    tmp_path,
    mutation: str,
) -> None:
    raw = _typed_research_artifact(tmp_path)
    payload = _decode_trusted(raw).payload
    (record,) = payload.budget_records
    if mutation == "duplicate":
        records = (record, record)
    elif mutation == "unreferenced":
        records = (
            _shift_probe_record(
                record,
                revision=0,
                action_digest="sha256:" + "7" * 64,
            ),
        )
    elif mutation == "gap":
        records = (record, _shift_probe_record(record, revision=2))
    else:
        records = (_shift_probe_record(record, revision=1), record)
    document = json.loads(raw)
    document["payload"]["budget_records"] = [
        item.model_dump(mode="json") for item in records
    ]

    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(document))


def test_typed_research_transition_requires_complete_budget_record(tmp_path) -> None:
    raw = _typed_research_artifact(tmp_path)
    document = json.loads(raw)
    document["payload"]["budget_records"] = []

    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(document))


def _model_budget_records(
    tmp_path,
    call_ids: tuple[str, ...] = (
        "research-model-0-0",
        "research-model-0-1",
    ),
) -> tuple[ModelBudgetLedgerRecord, ...]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    base = _minimal_payload()
    ledger = AdaptiveBudgetLedger(tmp_path / "model-budget.db")
    config = _config()
    request_digest = canonical_digest({"request": "replay-contract"})
    try:
        for index, call_id in enumerate(call_ids):
            execute_model_call_with_budget(
                base.run_id,
                base.run_incarnation,
                call_id,
                request_digest,
                "provider/replay-model",
                200,
                100,
                lambda _reservation: ModelTokenUsage(
                    input_tokens=10,
                    output_tokens=5,
                ),
                config=config,
                ledger=ledger,
                claim_now_ns=lambda: index + 1,
                owner_token_factory=lambda: f"owner-{index}",
            )
        return ledger.load_model_records(base.run_id, base.run_incarnation)
    finally:
        ledger.close()


def _model_budget_artifact(
    tmp_path,
) -> tuple[bytes, tuple[ModelBudgetLedgerRecord, ...]]:
    base = _minimal_payload()
    records = _model_budget_records(tmp_path)
    final = records[-1].reconciliation
    assert final is not None
    model_budget = final.budget_after
    snapshot = base.research_snapshots[0]
    budget = snapshot.state.budget_state.model_copy(
        update={
            "initial_model_calls": model_budget.initial_model_calls,
            "used_model_calls": model_budget.used_model_calls,
            "remaining_model_calls": model_budget.remaining_model_calls,
            "initial_model_tokens": model_budget.initial_total_tokens,
            "used_model_tokens": model_budget.used_total_tokens,
            "remaining_model_tokens": model_budget.remaining_total_tokens,
        }
    )
    state = ResearchState.model_validate(
        {
            **snapshot.state.model_dump(mode="python"),
            "budget_state": budget,
        }
    )
    state_digest = canonical_digest(state)
    terminal = base.research_terminal
    assert terminal is not None
    payload = AdaptiveReplayPayload.model_validate(
        {
            **base.model_dump(mode="python"),
            "research_snapshots": (
                snapshot.model_copy(update={"state": state, "digest": state_digest}),
            ),
            "research_terminal": terminal.model_copy(
                update={
                    "state_digest": state_digest,
                }
            ),
            "model_budget_records": records,
        }
    )
    return encode_replay_artifact(payload), records


@pytest.mark.parametrize("mutation", ("duplicate", "reordered"))
def test_decoder_rejects_duplicate_or_reordered_model_budget_records(
    tmp_path,
    mutation: str,
) -> None:
    raw, records = _model_budget_artifact(tmp_path)
    document = json.loads(raw)
    malformed = (records[0], records[0]) if mutation == "duplicate" else records[::-1]
    document["payload"]["model_budget_records"] = [
        item.model_dump(mode="json") for item in malformed
    ]

    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(document))


@pytest.mark.parametrize(
    "call_ids",
    (
        ("research-model-0-0", "research-model-0-2"),
        ("research-model-1-0",),
    ),
)
def test_decoder_rejects_model_budget_attempt_gap_or_unreferenced_revision(
    tmp_path,
    call_ids: tuple[str, ...],
) -> None:
    valid_raw, _ = _model_budget_artifact(tmp_path / "valid")
    malformed_records = _model_budget_records(tmp_path / "malformed", call_ids)
    document = json.loads(valid_raw)
    document["payload"]["model_budget_records"] = [
        item.model_dump(mode="json") for item in malformed_records
    ]

    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(document))


def test_decoder_rejects_incomplete_model_budget_phase_chain(tmp_path) -> None:
    raw, _ = _model_budget_artifact(tmp_path / "valid")
    payload = _decode_trusted(raw).payload
    config = _config()
    (tmp_path / "outstanding").mkdir()
    ledger = AdaptiveBudgetLedger(tmp_path / "outstanding" / "model-budget.db")
    try:
        reserve_model_call_budget(
            payload.run_id,
            payload.run_incarnation,
            "research-model-0-0",
            canonical_digest({"request": "outstanding"}),
            "provider/replay-model",
            200,
            100,
            config=config,
            ledger=ledger,
        )
        outstanding = ledger.load_model_records(payload.run_id, payload.run_incarnation)
    finally:
        ledger.close()
    document = json.loads(raw)
    document["payload"]["model_budget_records"] = [
        item.model_dump(mode="json") for item in outstanding
    ]

    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(document))
