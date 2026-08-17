from __future__ import annotations

import base64
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.serialization import (
    DEFAULT_LIMITS,
    ArtifactReference,
    canonical_digest,
    canonical_json_bytes,
)
from test_text_to_sql_adaptive_replay import (
    _artifact_digest,
    _decode_trusted,
    _minimal_payload,
    _repack_replay_document,
    _solver_state,
)
from test_text_to_sql_adaptive_replay_engine import _use_synchronous_sql_parser
from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
from workflow.adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore
from workflow.adaptive_state_store import AdaptiveStateStore


def _execution_artifact(
    tmp_path,
    monkeypatch,
    outcome: str,
) -> tuple[bytes, str, bytes]:
    from test_text_to_sql_adaptive_solver import _ready_state, _successful_terminal
    from workflow.text_to_sql_adaptive_replay import build_adaptive_replay_artifact
    from workflow.text_to_sql_adaptive_solver import (
        reconcile_known_finalizer,
        reconcile_reserved_finalizer_unknown,
    )

    _use_synchronous_sql_parser(monkeypatch)
    db_path = tmp_path / f"cold-{outcome.lower()}.db"
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
        execution_id=f"cold-{outcome.lower()}",
        request={
            "operation": "finalize_text_to_sql_run",
            "sql_query": candidate.sql,
            "row_limit": 10,
            "dry_run_only": False,
        },
    )
    checkpoint = (
        reconcile_known_finalizer(
            solver_store,
            reservation,
            state,
            _successful_terminal(state),
        )
        if outcome == "KNOWN"
        else reconcile_reserved_finalizer_unknown(
            solver_store,
            reservation,
            state,
        )
    )
    raw = build_adaptive_replay_artifact(
        state.run_id,
        state.run_incarnation,
        checkpoint_store=checkpoint_store,
        research_store=research_store,
        solver_store=solver_store,
        budget_ledger=budget_ledger,
    )
    assert checkpoint.terminal is not None
    return raw, canonical_digest(checkpoint.state), checkpoint.terminal.terminal_bytes


@pytest.mark.parametrize("outcome", ("KNOWN", "UNKNOWN"))
def test_fresh_process_execution_replay_is_pure_and_matches_production(
    tmp_path,
    monkeypatch,
    outcome: str,
) -> None:
    raw, expected_state_digest, _ = _execution_artifact(
        tmp_path,
        monkeypatch,
        outcome,
    )
    request = json.dumps(
        {
            "raw": base64.b64encode(raw).decode("ascii"),
            "trusted_digest": _artifact_digest(raw),
            "expected_state_digest": expected_state_digest,
        }
    )
    script = r"""
import base64
import json
import sys

request = json.loads(sys.stdin.read())
import pydantic

baseline_modules = frozenset(sys.modules)
external_calls = []

def audit_external_calls(event, _args):
    if event.startswith(("socket.", "subprocess.", "sqlite3.", "multiprocessing.")):
        external_calls.append(event)

sys.addaudithook(audit_external_calls)
from custom_tools.text_to_sql.adaptive.replay import replay_adaptive_artifact

result = replay_adaptive_artifact(
    base64.b64decode(request["raw"], validate=True),
    trusted_artifact_digest=request["trusted_digest"],
)
assert result.solver_state_digest == request["expected_state_digest"]
forbidden = (
    "custom_tools.text_to_sql.core._db_exec",
    "workflow.text_to_sql_adaptive_solver",
    "memory.manager",
    "chromadb",
    "sqlite3",
    "multiprocessing",
    "custom_tools.text_to_sql.adaptive._sql_ast_process",
    "custom_tools.text_to_sql.adaptive.research_loop",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
assert not loaded, loaded
incremental_runtime_modules = sorted(
    name
    for name in ("socket", "subprocess", "sqlite3", "multiprocessing")
    if name in sys.modules and name not in baseline_modules
)
assert not incremental_runtime_modules, incremental_runtime_modules
assert not external_calls, external_calls
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=request,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_fresh_process_research_replay_does_not_load_research_loop() -> None:
    from custom_tools.text_to_sql.adaptive.replay import encode_replay_artifact

    payload = _minimal_payload()
    raw = encode_replay_artifact(payload)
    request = json.dumps(
        {
            "raw": base64.b64encode(raw).decode("ascii"),
            "trusted_digest": _artifact_digest(raw),
            "expected_state_digest": payload.research_snapshots[-1].digest,
        }
    )
    script = r"""
import base64
import json
import sys

request = json.loads(sys.stdin.read())
import pydantic

baseline_modules = frozenset(sys.modules)
external_calls = []

def audit_external_calls(event, _args):
    if event.startswith(("socket.", "subprocess.", "sqlite3.", "multiprocessing.")):
        external_calls.append(event)

sys.addaudithook(audit_external_calls)
from custom_tools.text_to_sql.adaptive.replay import replay_adaptive_artifact

result = replay_adaptive_artifact(
    base64.b64decode(request["raw"], validate=True),
    trusted_artifact_digest=request["trusted_digest"],
)
assert result.research_state_digest == request["expected_state_digest"]
assert "custom_tools.text_to_sql.adaptive.research_loop" not in sys.modules
incremental_runtime_modules = sorted(
    name
    for name in ("socket", "subprocess", "sqlite3", "multiprocessing")
    if name in sys.modules and name not in baseline_modules
)
assert not incremental_runtime_modules, incremental_runtime_modules
assert not external_calls, external_calls
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=request,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_research_loop_preserves_shared_terminal_authority_names() -> None:
    from custom_tools.text_to_sql.adaptive import _research_terminal_authority
    from custom_tools.text_to_sql.adaptive import research_loop

    assert research_loop._terminal_envelope is (
        _research_terminal_authority._terminal_envelope
    )
    assert research_loop._terminal_replay_is_authorized is (
        _research_terminal_authority._terminal_replay_is_authorized
    )


def test_stopped_solver_state_requires_terminal_authority() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        AdaptiveReplayPayload,
        SolverReplaySnapshot,
    )
    from custom_tools.text_to_sql.adaptive.solver_loop import stop_solver
    from custom_tools.text_to_sql.adaptive.models import SolverStopReason

    base = _minimal_payload()
    stopped = stop_solver(
        _solver_state(revision=0),
        SolverStopReason.STAGNATED,
        base_revision=0,
    )
    snapshot = SolverReplaySnapshot(
        state=stopped,
        digest=canonical_digest(stopped),
        source_action_revision=None,
    )

    with pytest.raises(ValidationError, match="terminal authority"):
        AdaptiveReplayPayload.model_validate(
            {
                **base.model_dump(mode="python"),
                "solver_snapshots": (snapshot,),
            }
        )


def test_open_solver_state_may_omit_terminal() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        AdaptiveReplayPayload,
        SolverReplaySnapshot,
    )

    base = _minimal_payload()
    state = _solver_state(revision=0)
    snapshot = SolverReplaySnapshot(
        state=state,
        digest=canonical_digest(state),
        source_action_revision=None,
    )

    payload = AdaptiveReplayPayload.model_validate(
        {
            **base.model_dump(mode="python"),
            "solver_snapshots": (snapshot,),
        }
    )

    assert payload.solver_terminal is None
    assert payload.solver_snapshots[-1].state.stop_reason is None


def test_known_execution_reconciliation_requires_terminal_authority(
    tmp_path,
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive.replay import ReplayContractError

    raw, _, _ = _execution_artifact(tmp_path, monkeypatch, "KNOWN")
    document = json.loads(raw)
    document["payload"]["solver_terminal"] = None
    document["payload"]["historical_status"] = "UNVERIFIABLE"
    document["payload"]["legacy_reasons"] = ["NO_TYPED_PROVENANCE"]

    with pytest.raises(ReplayContractError, match="closed contract"):
        _decode_trusted(_repack_replay_document(document))


def test_cumulative_attachment_budget_rejects_before_first_reader_call() -> None:
    from custom_tools.text_to_sql.adaptive.replay import ReplayContractError
    from workflow.text_to_sql_adaptive_replay import _artifact_attachments

    references = tuple(
        ArtifactReference(
            artifact_id=f"aggregate-{index}",
            digest="sha256:" + f"{index:064x}",
            byte_count=48 * 1024,
        )
        for index in range(50)
    )
    calls = 0

    def reader(_reference: ArtifactReference) -> bytes:
        nonlocal calls
        calls += 1
        return b""

    with pytest.raises(ReplayContractError, match="aggregate|count"):
        _artifact_attachments(references, reader)
    assert calls == 0


def test_exact_duplicate_attachment_reference_is_deduplicated() -> None:
    from custom_tools.text_to_sql.adaptive.replay_contract import (
        dedupe_artifact_references,
    )

    reference = ArtifactReference(
        artifact_id="same-reference",
        digest="sha256:" + "a" * 64,
        byte_count=1,
    )

    assert dedupe_artifact_references((reference, reference)) == (reference,)


def test_production_and_replay_share_unknown_terminal_projector(
    tmp_path,
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive.replay import decode_replay_artifact
    from workflow import _text_to_sql_solver_execution_reducer as pure_reducer
    from workflow import text_to_sql_adaptive_solver as production_solver

    raw, _, production_terminal = _execution_artifact(
        tmp_path,
        monkeypatch,
        "UNKNOWN",
    )
    decoded = decode_replay_artifact(
        raw,
        trusted_artifact_digest=_artifact_digest(raw),
    )
    terminal = decoded.payload.solver_terminal
    assert terminal is not None

    assert production_solver.execution_unknown_terminal_result is (
        pure_reducer.execution_unknown_terminal_result
    )
    assert terminal.terminal.content() == production_terminal


def _projected_envelope_size(payload: dict[str, object]) -> int:
    from custom_tools.text_to_sql.adaptive.replay_contract import sha256_digest

    payload_bytes = canonical_json_bytes(payload)
    return len(
        canonical_json_bytes(
            {
                "artifact_version": 3,
                "record_kind": "adaptive_replay",
                "payload": payload,
                "payload_digest": sha256_digest(payload_bytes),
                "byte_count": len(payload_bytes),
            }
        )
    )


def _large_durable_payload_projection() -> dict[str, object]:
    payload = _minimal_payload().model_dump(mode="python")
    original = payload["query_specs"][0]
    query_specs = [original]
    target = DEFAULT_LIMITS.max_state_bytes - 164
    while True:
        revision = len(query_specs)
        query_specs.append(
            {
                **original,
                "revision": revision,
                "original_text": original["original_text"]
                + "x" * (65_536 - len(original["original_text"])),
            }
        )
        candidate = {
            **payload,
            "query_specs": tuple(query_specs),
            "artifact_references": (),
            "artifact_attachments": (),
        }
        size = _projected_envelope_size(candidate)
        if size < target:
            continue
        excess = size - target
        text = query_specs[-1]["original_text"]
        assert 0 <= excess < len(text) - len(original["original_text"])
        query_specs[-1] = {
            **query_specs[-1],
            "original_text": text[:-excess] if excess else text,
        }
        projected = {
            **payload,
            "query_specs": tuple(query_specs),
            "artifact_references": (),
        }
        projected.pop("artifact_attachments")
        return projected


def test_full_artifact_preflight_rejects_large_durable_payload_before_reader() -> None:
    from custom_tools.text_to_sql.adaptive.replay import ReplayContractError
    from workflow.text_to_sql_adaptive_replay import _artifact_attachments

    content = b"x"
    reference = ArtifactReference(
        artifact_id="small-attachment",
        digest="sha256:" + "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
        byte_count=len(content),
    )
    payload = _large_durable_payload_projection()
    payload["artifact_references"] = (reference,)
    projected_payload = {
        **payload,
        "artifact_attachments": (
            {
                "reference": reference,
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        ),
    }
    assert len(canonical_json_bytes(projected_payload)) < DEFAULT_LIMITS.max_state_bytes
    assert _projected_envelope_size(projected_payload) > DEFAULT_LIMITS.max_state_bytes
    calls = 0

    def reader(_reference: ArtifactReference) -> bytes:
        nonlocal calls
        calls += 1
        return content

    with pytest.raises(ReplayContractError, match="projected replay artifact"):
        _artifact_attachments(
            (reference,),
            reader,
            payload_projection=payload,
        )
    assert calls == 0


def test_full_artifact_preflight_accepts_normal_payload() -> None:
    from workflow.text_to_sql_adaptive_replay import _artifact_attachments

    content = b"x"
    reference = ArtifactReference(
        artifact_id="normal-attachment",
        digest="sha256:" + "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
        byte_count=len(content),
    )
    payload = _minimal_payload().model_dump(mode="python")
    payload["artifact_references"] = (reference,)
    payload.pop("artifact_attachments")
    calls = 0

    def reader(_reference: ArtifactReference) -> bytes:
        nonlocal calls
        calls += 1
        return content

    attachments = _artifact_attachments(
        (reference,),
        reader,
        payload_projection=payload,
    )

    assert calls == 1
    assert attachments[0].content() == content


def test_observability_mapper_copies_anchored_authority() -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        evaluate_replay_evidence_reuse,
        replay_adaptive_artifact,
        encode_replay_artifact,
    )
    from custom_tools.text_to_sql.eval import (
        HistoricalReplayReasonCode,
        adaptive_replay_observability_record,
    )

    payload = _minimal_payload()
    raw = encode_replay_artifact(payload)
    trusted_digest = _artifact_digest(raw)
    envelope = _decode_trusted(raw)
    historical = replay_adaptive_artifact(
        raw,
        trusted_artifact_digest=trusted_digest,
    )
    terminal = payload.research_terminal
    assert terminal is not None and terminal.replay_input is not None
    reuse = evaluate_replay_evidence_reuse(
        raw,
        terminal.replay_input.freshness_context,
        trusted_artifact_digest=trusted_digest,
    )

    record = adaptive_replay_observability_record(
        case_id="anchored-case",
        envelope=envelope,
        historical=historical,
        reuse=reuse,
    )

    assert record.run_id == envelope.payload.run_id
    assert record.run_incarnation == envelope.payload.run_incarnation
    assert record.trusted_artifact_digest == historical.trusted_artifact_digest
    assert record.historical_reason_code is HistoricalReplayReasonCode.VERIFIED
    assert (
        record.verified_research_transition_count
        == historical.verified_research_transition_count
    )
    assert (
        record.verified_solver_transition_count
        == historical.verified_solver_transition_count
    )


@pytest.mark.parametrize(
    "forgery",
    ("trusted_digest", "historical_status", "transition_count"),
)
def test_observability_mapper_rejects_unanchored_outcome_mismatches(
    forgery: str,
) -> None:
    from custom_tools.text_to_sql.adaptive.replay import (
        HistoricalReplayStatus,
        encode_replay_artifact,
        evaluate_replay_evidence_reuse,
        replay_adaptive_artifact,
    )
    from custom_tools.text_to_sql.eval import adaptive_replay_observability_record

    payload = _minimal_payload()
    raw = encode_replay_artifact(payload)
    trusted_digest = _artifact_digest(raw)
    envelope = _decode_trusted(raw)
    historical = replay_adaptive_artifact(
        raw,
        trusted_artifact_digest=trusted_digest,
    )
    terminal = payload.research_terminal
    assert terminal is not None and terminal.replay_input is not None
    reuse = evaluate_replay_evidence_reuse(
        raw,
        terminal.replay_input.freshness_context,
        trusted_artifact_digest=trusted_digest,
    )
    if forgery == "trusted_digest":
        reuse = reuse.model_copy(
            update={"trusted_artifact_digest": "sha256:" + "f" * 64}
        )
    elif forgery == "historical_status":
        reuse = reuse.model_copy(
            update={"historical_status": HistoricalReplayStatus.UNVERIFIABLE}
        )
    else:
        historical = historical.model_copy(
            update={
                "verified_research_transition_count": (
                    historical.verified_research_transition_count + 1
                )
            }
        )

    with pytest.raises(ValueError, match="does not agree"):
        adaptive_replay_observability_record(
            case_id="forged-case",
            envelope=envelope,
            historical=historical,
            reuse=reuse,
        )
