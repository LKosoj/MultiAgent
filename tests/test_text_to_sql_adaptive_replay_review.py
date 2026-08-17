from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive.serialization import (
    ArtifactReference,
    DEFAULT_LIMITS,
    canonical_json_bytes,
)
from test_text_to_sql_adaptive_replay import (
    _minimal_payload,
    _repack_replay_document,
    _replay_trusted,
)
from test_text_to_sql_durable_replay_inputs import _research_replay_case


def _artifact_digest(raw: bytes) -> str:
    return f"sha256:{sha256(raw).hexdigest()}"


def _co_tampered_artifact(raw: bytes) -> bytes:
    document = json.loads(raw)
    document["payload"]["query_specs"][0]["original_text"] = "tampered"
    document["payload"]["research_snapshots"][0]["state"]["query_spec"][
        "original_text"
    ] = "tampered"
    state = document["payload"]["research_snapshots"][0]["state"]
    document["payload"]["research_snapshots"][0]["digest"] = _artifact_digest(
        canonical_json_bytes(state)
    )
    payload_bytes = canonical_json_bytes(document["payload"])
    document["payload_digest"] = _artifact_digest(payload_bytes)
    document["byte_count"] = len(payload_bytes)
    return canonical_json_bytes(document)


def test_external_anchor_rejects_co_tampered_artifact() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        ReplayContractError,
        decode_replay_artifact,
        encode_replay_artifact,
    )

    raw = encode_replay_artifact(_minimal_payload())
    trusted_digest = _artifact_digest(raw)

    assert (
        decode_replay_artifact(raw, trusted_artifact_digest=trusted_digest).payload
        == _minimal_payload()
    )
    with pytest.raises(ReplayContractError, match="trusted artifact digest"):
        decode_replay_artifact(
            _co_tampered_artifact(raw),
            trusted_artifact_digest=trusted_digest,
        )


def test_unanchored_public_decode_and_replay_are_unavailable() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        decode_replay_artifact,
        encode_replay_artifact,
        replay_adaptive_artifact,
    )

    raw = encode_replay_artifact(_minimal_payload())

    with pytest.raises(TypeError):
        decode_replay_artifact(raw)
    with pytest.raises(TypeError):
        replay_adaptive_artifact(raw)


def test_export_reference_dedupe_rejects_conflicting_same_id() -> None:
    from custom_tools.text_to_sql.adaptive.replay import ReplayContractError
    from workflow.text_to_sql_adaptive_replay import _artifact_references

    first = ArtifactReference(
        artifact_id="artifact-shared",
        digest="sha256:" + "1" * 64,
        byte_count=1,
    )
    conflicting = first.model_copy(
        update={"digest": "sha256:" + "2" * 64, "byte_count": 2}
    )
    transitions = tuple(
        SimpleNamespace(
            replay_input=SimpleNamespace(
                probe_result=SimpleNamespace(artifact_reference=reference)
            )
        )
        for reference in (first, conflicting)
    )

    with pytest.raises(ReplayContractError, match="conflicting artifact reference"):
        _artifact_references(transitions)


def test_decoder_rejects_conflicting_same_id_artifact_references() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        ReplayContractError,
        decode_replay_artifact,
        encode_replay_artifact,
    )

    first = ArtifactReference(
        artifact_id="artifact-shared",
        digest="sha256:" + "1" * 64,
        byte_count=1,
    )
    conflicting = first.model_copy(
        update={"digest": "sha256:" + "2" * 64, "byte_count": 2}
    )
    document = json.loads(encode_replay_artifact(_minimal_payload()))
    document["payload"]["artifact_references"] = [
        first.model_dump(mode="json"),
        conflicting.model_dump(mode="json"),
    ]
    raw = _repack_replay_document(document)

    with pytest.raises(ReplayContractError, match="closed contract"):
        decode_replay_artifact(
            raw,
            trusted_artifact_digest=_artifact_digest(raw),
        )


def test_replay_model_dedupe_rejects_conflicting_same_id_references() -> None:
    from custom_tools.text_to_sql.adaptive.replay_contract import (
        dedupe_artifact_references,
    )

    first = ArtifactReference(
        artifact_id="artifact-shared",
        digest="sha256:" + "3" * 64,
        byte_count=3,
    )
    conflicting = first.model_copy(
        update={"digest": "sha256:" + "4" * 64, "byte_count": 4}
    )

    with pytest.raises(ValueError, match="conflicting artifact reference"):
        dedupe_artifact_references((first, conflicting))


def test_export_rejects_oversize_attachment_before_reader() -> None:
    from custom_tools.text_to_sql.adaptive.replay import ReplayContractError
    from workflow.text_to_sql_adaptive_replay import _artifact_attachments

    reference = ArtifactReference(
        artifact_id="artifact-too-large",
        digest="sha256:" + "0" * 64,
        byte_count=DEFAULT_LIMITS.max_state_bytes + 1,
    )
    calls = 0

    def reader(_reference: ArtifactReference) -> bytes:
        nonlocal calls
        calls += 1
        return b""

    with pytest.raises(ReplayContractError, match="attachment byte_count"):
        _artifact_attachments((reference,), reader)
    assert calls == 0


def test_root_only_history_is_unverifiable_and_never_reusable() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        AdaptiveReplayPayload,
        EvidenceReuseStatus,
        HistoricalReplayStatus,
        LegacyReplayReason,
        encode_replay_artifact,
        evaluate_replay_evidence_reuse,
        replay_adaptive_artifact,
    )

    base = _minimal_payload()
    payload = AdaptiveReplayPayload.model_validate(
        {
            **base.model_dump(mode="python"),
            "historical_status": HistoricalReplayStatus.UNVERIFIABLE,
            "legacy_reasons": (LegacyReplayReason.NO_TYPED_PROVENANCE,),
            "research_terminal": None,
        }
    )
    raw = encode_replay_artifact(payload)
    trusted_digest = _artifact_digest(raw)
    freshness = base.research_terminal
    assert freshness is not None and freshness.replay_input is not None

    replayed = replay_adaptive_artifact(
        raw,
        trusted_artifact_digest=trusted_digest,
    )
    reuse = evaluate_replay_evidence_reuse(
        raw,
        freshness.replay_input.freshness_context,
        trusted_artifact_digest=trusted_digest,
    )

    assert replayed.status is HistoricalReplayStatus.UNVERIFIABLE
    assert replayed.legacy_reasons == (LegacyReplayReason.NO_TYPED_PROVENANCE,)
    assert replayed.verified_research_transition_count == 0
    assert replayed.verified_solver_transition_count == 0
    assert reuse.status is EvidenceReuseStatus.REVALIDATION_REQUIRED
    assert reuse.historical_status is HistoricalReplayStatus.UNVERIFIABLE


def test_export_preserves_final_research_abort_journal_and_terminal(tmp_path) -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
        LegacyReplayReason,
        decode_replay_artifact,
        evaluate_replay_evidence_reuse,
        replay_adaptive_artifact,
    )
    from custom_tools.text_to_sql.eval import (
        HistoricalReplayReasonCode,
        adaptive_replay_observability_record,
    )
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        ResearchTerminalReplayInput,
    )
    from custom_tools.text_to_sql.adaptive.semantic_reducer import admit_semantic_turn
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
    from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
    from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
    from workflow.adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore
    from workflow.adaptive_state_store import (
        AdaptiveCheckpointKey,
        AdaptiveLoopKind,
        AdaptiveStateStore,
    )
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact

    before, _, replay_input = _research_replay_case()
    admission = admit_semantic_turn(
        before,
        replay_input.decision,
        batch=replay_input.semantic_batch,
        freshness_context=replay_input.freshness_context,
        tool_claim=replay_input.tool_claim,
        budget_state=replay_input.budget_state,
    )
    assert admission.action is not None
    resolution_digest = "sha256:" + "5" * 64
    db_path = tmp_path / "research-abort-replay.db"
    checkpoint_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    research_store.save_query_spec(before.query_spec)
    research_store.save_research_state(before, expected_previous_revision=None)
    key = AdaptiveCheckpointKey(
        before.run_id,
        before.run_incarnation,
        AdaptiveLoopKind.RESEARCH,
        before.revision,
    )
    checkpoint_store.record_planned(
        key,
        expected_revision=None,
        action={
            "action": admission.action.model_dump(mode="json", by_alias=True),
            "contract_version": 1,
            "decision": replay_input.decision.model_dump(mode="json", by_alias=True),
            "invocation_id": replay_input.probe_result.invocation_id,
            "kind": "research_planned",
            "resolution_digest": resolution_digest,
            "state_digest": canonical_digest(before),
        },
    )
    checkpoint_store.record_observed(
        key,
        expected_revision=before.revision,
        action={
            "action": admission.action.model_dump(mode="json", by_alias=True),
            "contract_version": 1,
            "kind": "research_aborted",
            "reason": "TOOL_FAILURE",
            "resolution_digest": resolution_digest,
        },
    )
    affected = tuple(
        sorted(
            item.source_id for item in before.query_spec.semantic_items if item.required
        )
    )
    checkpoint_store.record_replayable_terminal(
        key,
        expected_revision=before.revision,
        action={
            "affected_source_ids": list(affected),
            "citation_evidence_ids": [],
            "contract_version": 2,
            "kind": "research_terminal",
            "rejection_signatures": [],
            "reason": "TOOL_FAILURE",
            "ambiguity": None,
        },
        replay_input=ResearchTerminalReplayInput(
            freshness_context=replay_input.freshness_context
        ),
    )

    raw = build_adaptive_replay_artifact(
        before.run_id,
        before.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=AdaptiveSolverCheckpointStore(db_path),
        budget_ledger=AdaptiveBudgetLedger(db_path),
    )
    trusted_digest = _artifact_digest(raw)
    decoded = decode_replay_artifact(
        raw,
        trusted_artifact_digest=trusted_digest,
    )
    journal = decoded.payload.research_abort_journal

    assert journal is not None
    assert journal.planned.action == journal.aborted.action
    assert journal.aborted.reason == decoded.payload.research_terminal.action.reason
    replayed = replay_adaptive_artifact(
        raw,
        trusted_artifact_digest=trusted_digest,
    )
    assert replayed.status is HistoricalReplayStatus.UNVERIFIABLE
    assert replayed.legacy_reasons == (LegacyReplayReason.RESEARCH_ABORT_INPUT,)
    assert replayed.research_state_digest is None
    assert replayed.solver_state_digest is None
    reuse = evaluate_replay_evidence_reuse(
        raw,
        replay_input.freshness_context,
        trusted_artifact_digest=trusted_digest,
    )
    observation = adaptive_replay_observability_record(
        case_id="research-abort",
        envelope=decoded,
        historical=replayed,
        reuse=reuse,
    )
    assert observation.historical_reason_code is (
        HistoricalReplayReasonCode.MISSING_RESEARCH_ABORT_REDUCER_INPUT
    )

    forged_document = json.loads(raw)
    forged_journal = forged_document["payload"]["research_abort_journal"]
    forged_journal["planned"]["action"]["action_id"] = "forged-abort-action"
    forged_journal["aborted"]["action"]["action_id"] = "forged-abort-action"
    forged_journal["planned_digest"] = canonical_digest(forged_journal["planned"])
    forged_journal["aborted_digest"] = canonical_digest(forged_journal["aborted"])
    forged = _replay_trusted(_repack_replay_document(forged_document))
    assert forged.status is HistoricalReplayStatus.UNVERIFIABLE
    assert forged.legacy_reasons == (LegacyReplayReason.RESEARCH_ABORT_INPUT,)
    assert forged.research_state_digest is None
