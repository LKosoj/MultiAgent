from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from custom_tools.text_to_sql.adaptive.models import (
    BudgetState,
    ExpectedResultShape,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    SolverState,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.freshness import (
    DataSnapshotStatus,
    DataSnapshotValidation,
    FreshnessContext,
)
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.replay_inputs import (
    ResearchTerminalReplayInput,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)
from test_text_to_sql_durable_replay_inputs import (
    _create_honest_v2_database,
    _insert_v2_research_snapshot,
)
from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore
from workflow.adaptive_state_store import (
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)


RUN_ID = "run-replay-001"
INCARNATION = "inc-replay-001"
SCHEMA_NAMESPACE = "schema:0123456789abcdef"


def _query_spec(*, revision: int, incarnation: str = INCARNATION) -> QuerySpec:
    return QuerySpec(
        run_id=RUN_ID,
        run_incarnation=incarnation,
        revision=revision,
        schema_namespace_version=SCHEMA_NAMESPACE,
        query_id="query-replay-001",
        original_text="sales",
        semantic_items=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )


def _budget() -> BudgetState:
    values = {
        f"{prefix}_{name}": 0
        for name in (
            "wall_clock_ms",
            "model_calls",
            "model_tokens",
            "db_probe_ms",
            "rows",
            "bytes",
        )
        for prefix in ("initial", "used", "remaining")
    }
    return BudgetState(**values)


def _freshness() -> FreshnessContext:
    return FreshnessContext(
        evaluated_at=datetime(2026, 8, 2, tzinfo=UTC),
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        schema_namespace_version=SCHEMA_NAMESPACE,
    )


def _research_state(*, revision: int) -> ResearchState:
    target = TableRef(namespace="main", schema=None, table="orders")
    actions = tuple(
        ResearchAction(
            action_id=f"research-action-{index}",
            kind=ResearchActionKind.INSPECT_TABLE,
            hypothesis_id=None,
            target=target,
            parameters=(("detail", f"revision-{index}"),),
            action_digest=canonical_action_digest(
                kind=ResearchActionKind.INSPECT_TABLE,
                hypothesis_id=None,
                target=target,
                parameters=(("detail", f"revision-{index}"),),
                expected_revision=index,
            ),
            expected_revision=index,
        )
        for index in range(revision)
    )
    return ResearchState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=revision,
        schema_namespace_version=SCHEMA_NAMESPACE,
        query_spec=_query_spec(revision=0),
        hypotheses=(),
        evidence=(),
        result_expectations=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=(),
        action_history=actions,
        budget_state=_budget(),
        stop_reason=None,
    )


def _solver_state(*, revision: int) -> SolverState:
    return SolverState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=revision,
        schema_namespace_version=SCHEMA_NAMESPACE,
        query_spec=_query_spec(revision=0),
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        research_reentries=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )


def _minimal_payload():
    return _payload_for_research_root(_research_state(revision=0))


def _payload_for_research_root(state: ResearchState):
    from custom_tools.text_to_sql.adaptive.replay import (
        AdaptiveReplayPayload,
        HistoricalReplayStatus,
        ResearchReplayTerminal,
        ResearchReplaySnapshot,
        ResearchTerminalReplayAction,
    )
    from custom_tools.text_to_sql.adaptive.replay_contract import (
        durable_action_digest,
    )
    from custom_tools.text_to_sql.adaptive.models import ResearchStopReason

    terminal_action = ResearchTerminalReplayAction(
        contract_version=2,
        kind="research_terminal",
        reason=ResearchStopReason.STAGNATED,
        affected_source_ids=tuple(
            sorted(
                item.source_id
                for item in state.query_spec.semantic_items
                if item.required
            )
        ),
        citation_evidence_ids=tuple(
            sorted(item.evidence_id for item in state.evidence)
        ),
        ambiguity=None,
        rejection_signatures=(),
    )
    freshness = FreshnessContext(
        evaluated_at=datetime(2026, 8, 2, tzinfo=UTC),
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
        data_snapshots=tuple(
            DataSnapshotValidation(
                token=token,
                status=DataSnapshotStatus.VALID,
            )
            for token in sorted(
                {
                    item.data_snapshot_token
                    for item in state.evidence
                    if item.data_snapshot_token is not None
                }
            )
        ),
    )
    state_digest = canonical_digest(state)

    return AdaptiveReplayPayload(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
        historical_status=HistoricalReplayStatus.VERIFIED,
        legacy_reasons=(),
        query_specs=(state.query_spec,),
        research_snapshots=(ResearchReplaySnapshot(state=state, digest=state_digest),),
        research_transitions=(),
        research_abort_journal=None,
        research_terminal=ResearchReplayTerminal(
            state_revision=state.revision,
            state_digest=state_digest,
            action=terminal_action,
            action_digest=durable_action_digest(terminal_action),
            replay_input=ResearchTerminalReplayInput(freshness_context=freshness),
        ),
        solver_snapshots=(),
        solver_steps=(),
        solver_terminal=None,
        budget_records=(),
        model_budget_records=(),
        artifact_references=(),
        artifact_attachments=(),
    )


def _with_linked_query_schema(state: ResearchState) -> ResearchState:
    query_spec = state.query_spec.model_copy(
        update={"schema_namespace_version": state.schema_namespace_version}
    )
    return ResearchState.model_validate(
        {**state.model_dump(mode="python"), "query_spec": query_spec}
    )


def _repack_replay_document(document: dict[str, object]) -> bytes:
    payload = document["payload"]
    assert isinstance(payload, dict)
    payload_bytes = canonical_json_bytes(payload)
    document["byte_count"] = len(payload_bytes)
    document["payload_digest"] = f"sha256:{sha256(payload_bytes).hexdigest()}"
    return canonical_json_bytes(document)


def _artifact_digest(raw: bytes) -> str:
    return f"sha256:{sha256(raw).hexdigest()}"


def _decode_trusted(raw: bytes):
    from custom_tools.text_to_sql.adaptive.replay import decode_replay_artifact

    return decode_replay_artifact(
        raw,
        trusted_artifact_digest=_artifact_digest(raw),
    )


def _replay_trusted(raw: bytes):
    from custom_tools.text_to_sql.adaptive.replay import replay_adaptive_artifact

    return replay_adaptive_artifact(
        raw,
        trusted_artifact_digest=_artifact_digest(raw),
    )


def _reuse_trusted(raw: bytes, freshness_context: FreshnessContext):
    from custom_tools.text_to_sql.adaptive.replay import (
        evaluate_replay_evidence_reuse,
    )

    return evaluate_replay_evidence_reuse(
        raw,
        freshness_context,
        trusted_artifact_digest=_artifact_digest(raw),
    )


def _forbid_external_replay(monkeypatch) -> None:
    import socket
    import sqlite3
    import subprocess

    def forbidden(*_args, **_kwargs):
        raise AssertionError("external operation attempted during pure replay")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


def test_replay_contract_round_trip_is_canonical_and_root_covers_payload() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        encode_replay_artifact,
    )

    payload = _minimal_payload()
    first = encode_replay_artifact(payload)
    second = encode_replay_artifact(payload)

    assert first == second
    decoded = _decode_trusted(first)
    assert decoded.payload == payload
    assert encode_replay_artifact(decoded.payload) == first

    document = json.loads(first)
    payload_bytes = canonical_json_bytes(document["payload"])
    assert document["byte_count"] == len(payload_bytes)
    assert document["payload_digest"] == f"sha256:{sha256(payload_bytes).hexdigest()}"


def test_replay_contract_rejects_root_digest_tamper() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        ReplayContractError,
        encode_replay_artifact,
    )

    document = json.loads(encode_replay_artifact(_minimal_payload()))
    document["payload_digest"] = f"sha256:{'0' * 64}"

    with pytest.raises(ReplayContractError, match="payload_digest"):
        _decode_trusted(canonical_json_bytes(document))


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "transcript",
        "prompt",
        "chain_of_thought",
        "dsn",
        "credentials",
        "gold",
        "benchmark",
    ],
)
def test_replay_contract_is_closed_and_rejects_forbidden_fields(
    forbidden_field: str,
) -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        ReplayContractError,
        encode_replay_artifact,
    )

    document = json.loads(encode_replay_artifact(_minimal_payload()))
    document["payload"][forbidden_field] = "must not be persisted"
    with pytest.raises(ReplayContractError, match="payload"):
        _decode_trusted(_repack_replay_document(document))


def test_replay_contract_rejects_forged_model_copy() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
        ReplayContractError,
        encode_replay_artifact,
    )

    forged = _minimal_payload().model_copy(
        update={"historical_status": HistoricalReplayStatus.UNVERIFIABLE}
    )

    with pytest.raises(ReplayContractError, match="closed contract"):
        encode_replay_artifact(forged)


def test_replay_contract_rejects_corrupt_nested_snapshot_digest() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        ReplayContractError,
        encode_replay_artifact,
    )

    document = json.loads(encode_replay_artifact(_minimal_payload()))
    document["payload"]["research_snapshots"][0]["digest"] = "sha256:" + "0" * 64

    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(document))


def test_replay_contract_rejects_corrupt_terminal_action_digest() -> None:
    from custom_tools.text_to_sql.adaptive.models import ResearchStopReason
    from custom_tools.text_to_sql.adaptive.replay import (
        AdaptiveReplayPayload,
        ReplayContractError,
        ResearchReplayTerminal,
        ResearchTerminalReplayAction,
        encode_replay_artifact,
    )

    base = _minimal_payload()
    action = ResearchTerminalReplayAction(
        contract_version=2,
        kind="research_terminal",
        reason=ResearchStopReason.STAGNATED,
        affected_source_ids=(),
        citation_evidence_ids=(),
        ambiguity=None,
        rejection_signatures=(),
    )
    terminal = ResearchReplayTerminal(
        state_revision=base.research_snapshots[-1].state.revision,
        state_digest=base.research_snapshots[-1].digest,
        action=action,
        action_digest=canonical_digest(action),
        replay_input=ResearchTerminalReplayInput(freshness_context=_freshness()),
    )
    payload = AdaptiveReplayPayload.model_validate(
        {**base.model_dump(mode="python"), "research_terminal": terminal}
    )
    document = json.loads(encode_replay_artifact(payload))
    document["payload"]["research_terminal"]["action_digest"] = "sha256:" + "0" * 64

    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(document))


def test_replay_contract_rejects_unreferenced_or_tampered_attachment() -> None:
    import base64

    from pydantic import ValidationError

    from custom_tools.text_to_sql.adaptive.replay import (
        AdaptiveReplayPayload,
        ReplayArtifactAttachment,
    )
    from custom_tools.text_to_sql.adaptive.serialization import ArtifactReference

    content = b"bounded replay artifact"
    reference = ArtifactReference(
        artifact_id="replay-artifact-1",
        digest=f"sha256:{sha256(content).hexdigest()}",
        byte_count=len(content),
    )
    attachment = ReplayArtifactAttachment(
        reference=reference,
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    base = _minimal_payload()

    with pytest.raises(ValidationError, match="attachments"):
        AdaptiveReplayPayload.model_validate(
            {
                **base.model_dump(mode="python"),
                "artifact_references": (reference,),
                "artifact_attachments": (attachment,),
            }
        )
    with pytest.raises(ValidationError, match="does not match"):
        ReplayArtifactAttachment(
            reference=reference,
            content_base64=base64.b64encode(content + b"tamper").decode("ascii"),
        )


def test_export_attachment_requires_reader_and_exact_bytes() -> None:

    from custom_tools.text_to_sql.adaptive.replay import ReplayContractError
    from custom_tools.text_to_sql.adaptive.serialization import (
        ArtifactReference,
        ArtifactReferenceError,
    )
    from workflow.text_to_sql_adaptive_replay import _artifact_attachments

    content = b"artifact-backed reducer input"
    reference = ArtifactReference(
        artifact_id="artifact-backed-replay-1",
        digest=f"sha256:{sha256(content).hexdigest()}",
        byte_count=len(content),
    )

    with pytest.raises(ReplayContractError, match="requires artifact reader"):
        _artifact_attachments((reference,), None)
    with pytest.raises(ArtifactReferenceError, match="byte_count"):
        _artifact_attachments((reference,), lambda _reference: b"wrong")
    attachments = _artifact_attachments((reference,), lambda _reference: content)
    assert len(attachments) == 1
    assert attachments[0].reference == reference
    assert attachments[0].content() == content

    oversized = b"x" * (2 * 1024 * 1024)
    oversized_reference = ArtifactReference(
        artifact_id="oversized-replay-artifact",
        digest=f"sha256:{sha256(oversized).hexdigest()}",
        byte_count=len(oversized),
    )
    with pytest.raises(ReplayContractError, match="attachment byte_count"):
        _artifact_attachments(
            (oversized_reference,),
            lambda _reference: oversized,
        )


@pytest.mark.parametrize("mutation", ["reordered", "duplicate", "gap"])
def test_replay_contract_rejects_bad_query_root_chain(mutation: str) -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        AdaptiveReplayPayload,
        ReplayContractError,
        encode_replay_artifact,
    )

    base = _minimal_payload()
    payload = AdaptiveReplayPayload.model_validate(
        {
            **base.model_dump(mode="python"),
            "query_specs": (base.query_specs[0], _query_spec(revision=1)),
        }
    )
    document = json.loads(encode_replay_artifact(payload))
    roots = document["payload"]["query_specs"]
    if mutation == "reordered":
        roots.reverse()
    elif mutation == "duplicate":
        roots[1] = roots[0]
    else:
        roots[1]["revision"] = 2

    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(document))


def test_adaptive_state_store_loads_one_complete_ordered_run_chain(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "replay-chain.db")
    first = AdaptiveCheckpointKey(
        RUN_ID,
        INCARNATION,
        AdaptiveLoopKind.RESEARCH,
        0,
    )
    second = AdaptiveCheckpointKey(
        RUN_ID,
        INCARNATION,
        AdaptiveLoopKind.RESEARCH,
        1,
    )
    other = AdaptiveCheckpointKey(
        RUN_ID,
        "inc-replay-other",
        AdaptiveLoopKind.RESEARCH,
        0,
    )
    store.record_planned(first, expected_revision=None, action={"tool": "first"})
    store.record_observed(first, expected_revision=0, action={"rows": 1})
    store.record_planned(second, expected_revision=0, action={"tool": "second"})
    store.record_observed(second, expected_revision=1, action={"rows": 2})
    store.record_replayable_terminal(
        second,
        expected_revision=1,
        action={"reason": "complete"},
        replay_input=ResearchTerminalReplayInput(freshness_context=_freshness()),
    )
    store.record_planned(other, expected_revision=None, action={"tool": "other"})

    events = store.load_run_events(
        RUN_ID,
        INCARNATION,
        AdaptiveLoopKind.RESEARCH,
    )

    assert [(event.key.revision, event.phase.value) for event in events] == [
        (0, "planned"),
        (0, "observed"),
        (1, "planned"),
        (1, "observed"),
        (1, "terminal"),
    ]
    assert all(event.key.run_incarnation == INCARNATION for event in events)


def test_research_store_loads_complete_typed_snapshot_chains(tmp_path) -> None:
    database = tmp_path / "research-replay-chain.db"
    query_specs = (_query_spec(revision=0), _query_spec(revision=1))
    research_states = (_research_state(revision=0), _research_state(revision=1))
    _create_honest_v2_database(database)
    for state in research_states:
        _insert_v2_research_snapshot(database, state)
    store = AdaptiveResearchStateStore(database)
    for query_spec in query_specs:
        store.save_query_spec(query_spec)
    store.save_query_spec(_query_spec(revision=0, incarnation="inc-replay-other"))

    assert store.load_query_spec_chain(RUN_ID, INCARNATION) == query_specs
    assert store.load_research_state_chain(RUN_ID, INCARNATION) == research_states


def test_solver_store_exports_its_complete_validated_replay_chain(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver-replay-chain.db")
    initial = _solver_state(revision=0)
    successor = _solver_state(revision=1)
    action = {"kind": "solver_stop", "reason": "STAGNATED"}
    store.initialize(initial)
    store.commit_non_execution(
        initial,
        successor,
        action_revision=0,
        action=action,
    )

    chain = store.load_replay_chain(RUN_ID, INCARNATION)

    assert chain is not None
    assert tuple(snapshot.state for snapshot in chain.snapshots) == (
        initial,
        successor,
    )
    assert len(chain.actions) == 1
    assert chain.actions[0].action_revision == 0
    assert chain.actions[0].action == action


def test_verified_terminal_evidence_replays_through_legacy_execution_result(
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.adaptive.replay import HistoricalReplayStatus
    from test_text_to_sql_adaptive_solver import (
        _ready_state,
        _successful_terminal,
    )
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact
    from workflow.text_to_sql_adaptive_solver import reconcile_known_finalizer

    db_path = tmp_path / "terminal-evidence-replay.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    state = _ready_state()
    research_store.save_query_spec(state.query_spec)
    solver_store.initialize(state)
    terminal = _successful_terminal(state)
    candidate = state.sql_candidates[-1]
    reservation = solver_store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="finalizer-1",
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
        terminal,
    )
    assert checkpoint.verified_terminal_evidence is not None

    first = build_adaptive_replay_artifact(
        state.run_id,
        state.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    second = build_adaptive_replay_artifact(
        state.run_id,
        state.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )

    assert first == second
    decoded = _decode_trusted(first)
    execution = decoded.payload.solver_steps[-1]
    assert execution.reconciliation.result.content() == canonical_json_bytes(terminal)
    assert (
        execution.reconciliation.result.digest
        == checkpoint.terminal.terminal_digest
    )
    assert _replay_trusted(first).status is HistoricalReplayStatus.VERIFIED


def test_workflow_builds_same_replay_artifact_from_durable_stores_twice(
    tmp_path,
) -> None:
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    db_path = tmp_path / "assembled-replay.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    solver_store = AdaptiveSolverCheckpointStore(db_path)
    budget_ledger = AdaptiveBudgetLedger(db_path)
    query_spec = _query_spec(revision=0)
    research_state = _research_state(revision=0)
    solver_state = _solver_state(revision=0)
    research_store.save_query_spec(query_spec)
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
                "reason": "STAGNATED",
                "affected_source_ids": [],
                "citation_evidence_ids": [],
                "ambiguity": None,
                "rejection_signatures": [],
        },
        replay_input=ResearchTerminalReplayInput(freshness_context=_freshness()),
    )
    solver_store.initialize(solver_state)

    first = build_adaptive_replay_artifact(
        RUN_ID,
        INCARNATION,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    second = build_adaptive_replay_artifact(
        RUN_ID,
        INCARNATION,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )

    assert first == second
    decoded = _decode_trusted(first)
    assert tuple(snapshot.state for snapshot in decoded.payload.research_snapshots) == (
        research_state,
    )
    assert tuple(snapshot.state for snapshot in decoded.payload.solver_snapshots) == (
        solver_state,
    )
    assert decoded.payload.research_terminal is not None


def test_historical_replay_is_independent_from_current_schema_reuse() -> None:
    from test_text_to_sql_adaptive_freshness import _context, _evidence, _state

    from custom_tools.text_to_sql.adaptive.replay import (
        EvidenceReuseStatus,
        HistoricalReplayStatus,
        encode_replay_artifact,
    )

    state = _with_linked_query_schema(_state((_evidence(),)))
    raw = encode_replay_artifact(_payload_for_research_root(state))

    assert _replay_trusted(raw).status is HistoricalReplayStatus.VERIFIED
    assert _reuse_trusted(raw, _context()).status is EvidenceReuseStatus.REUSABLE
    stale = _reuse_trusted(
        raw,
        _context(schema="sha256:" + "b" * 64),
    )
    assert stale.status is EvidenceReuseStatus.REVALIDATION_REQUIRED
    assert stale.historical_status is HistoricalReplayStatus.VERIFIED


def test_data_evidence_reuse_requires_matching_valid_snapshot_token() -> None:
    from test_text_to_sql_adaptive_freshness import (
        _context,
        _evidence,
        _provenance,
        _state,
    )

    from custom_tools.text_to_sql.adaptive.freshness import (
        DataSnapshotStatus,
        DataSnapshotValidation,
    )
    from custom_tools.text_to_sql.adaptive.models import (
        EvidenceSourceKind,
        EvidenceValidityScope,
        ResearchActionKind,
    )
    from custom_tools.text_to_sql.adaptive.replay import (
        EvidenceReuseStatus,
        encode_replay_artifact,
    )

    evidence = _evidence(
        scope=EvidenceValidityScope.DATA_SNAPSHOT,
        source_kind=EvidenceSourceKind.PROBE,
        provenance=_provenance(kind=ResearchActionKind.EXECUTE_PROBE),
        snapshot_token="snapshot-1",
    )
    raw = encode_replay_artifact(
        _payload_for_research_root(_with_linked_query_schema(_state((evidence,))))
    )

    valid = _context(
        snapshots=(
            DataSnapshotValidation(
                token="snapshot-1",
                status=DataSnapshotStatus.VALID,
            ),
        )
    )
    invalid = _context(
        snapshots=(
            DataSnapshotValidation(
                token="snapshot-1",
                status=DataSnapshotStatus.INVALID,
            ),
        )
    )
    assert _reuse_trusted(raw, valid).status is EvidenceReuseStatus.REUSABLE
    assert (
        _reuse_trusted(raw, invalid).status is EvidenceReuseStatus.REVALIDATION_REQUIRED
    )


def test_pure_replay_does_not_open_database_network_or_process(
    monkeypatch,
) -> None:
    import socket
    import sqlite3
    import subprocess

    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
        encode_replay_artifact,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("external operation attempted during pure replay")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    raw = encode_replay_artifact(_minimal_payload())

    assert _replay_trusted(raw).status is HistoricalReplayStatus.VERIFIED
