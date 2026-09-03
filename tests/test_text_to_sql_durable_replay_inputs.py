from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
from custom_tools.text_to_sql.adaptive.models import EvidenceCost
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, build_probe_result
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from workflow.adaptive_research_state_store import (
    AdaptiveResearchStateStore,
    AdaptiveResearchStateStoreConflictError,
)
from workflow.adaptive_state_store import (
    AdaptiveCheckpointConflictError,
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.adaptive_solver_checkpoint import (
    AdaptiveSolverCheckpointCorruptionError,
    AdaptiveSolverCheckpointConflictError,
    AdaptiveSolverCheckpointStore,
)
from text_to_sql_decision_resolver_helpers import (
    NOW,
    freshness,
    make_state,
    schema,
    tool_decision,
    resolve,
)
from test_text_to_sql_solver_loop import (
    _case as solver_case,
    _ids as solver_ids,
    _missing as missing_solver_proposal,
    _sql as sql_solver_proposal,
    _state as solver_state,
)
from text_to_sql_semantic_checks_helpers import POSTGRES_DSN


RUN_ID = "run-durable-replay"
INCARNATION = "inc-durable-replay"
SCHEMA = "schema:0123456789abcdef"


def _freshness() -> FreshnessContext:
    return FreshnessContext(
        evaluated_at=datetime(2026, 8, 2, tzinfo=UTC),
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        schema_namespace_version=SCHEMA,
    )


def _research_replay_case(
    *,
    before=None,
    tool_name: str = "inspect_table",
    tool_arguments: dict[str, object] | None = None,
):
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        ResearchSemanticReplayInput,
    )
    from custom_tools.text_to_sql.adaptive.semantic_reducer import (
        commit_semantic_turn,
    )

    loaded, namespace = schema()
    if before is None:
        before = make_state(namespace)
    decision = tool_decision(
        tool_name,
        tool_arguments or {"table": "public.orders"},
    )
    resolved = resolve(decision, loaded=loaded, namespace=namespace, state=before)
    action = resolved.admission.action
    invocation = resolved.invocation
    assert action is not None
    assert invocation is not None
    payload = {}
    if tool_name == "inspect_column":
        payload = {
            "schema_namespace_version": before.schema_namespace_version,
            "status": "matched",
            "column": action.target.model_dump(mode="json", by_alias=True),
            "metadata": {},
        }
    probe_result = build_probe_result(
        run_id=before.run_id,
        run_incarnation=before.run_incarnation,
        revision=before.revision,
        schema_namespace_version=before.schema_namespace_version,
        invocation_id=invocation.invocation_id,
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=action.target,
        started_at=NOW,
        completed_at=NOW,
        summary="orders inspected",
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=0,
            bytes=len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ),
        ),
        row_count=0,
        payload=payload,
    )
    after = commit_semantic_turn(
        resolved.admission,
        probe_result=probe_result,
    ).state
    replay_input = ResearchSemanticReplayInput(
        decision=resolved.decision,
        semantic_batch=resolved.semantic_batch,
        freshness_context=freshness(before),
        tool_claim=resolved.tool_claim,
        budget_state=before.budget_state,
        planned_action_digest="sha256:" + "1" * 64,
        observed_action_digest="sha256:" + "2" * 64,
        probe_result=probe_result,
    )
    return before, after, replay_input


def _record_research_journal(
    path,
    before,
    replay_input,
    *,
    semantic_repair_continuation: bool = False,
):
    from custom_tools.text_to_sql.adaptive.semantic_reducer import (
        admit_semantic_turn,
        commit_semantic_turn,
    )
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest

    admission = admit_semantic_turn(
        before,
        replay_input.decision,
        batch=replay_input.semantic_batch,
        freshness_context=replay_input.freshness_context,
        tool_claim=replay_input.tool_claim,
        budget_state=replay_input.budget_state,
    )
    action = admission.action
    assert action is not None
    replayed = commit_semantic_turn(
        admission,
        probe_result=replay_input.probe_result,
    )
    resolution_digest = "sha256:" + "5" * 64
    checkpoint = AdaptiveStateStore(path)
    key = AdaptiveCheckpointKey(
        before.run_id,
        before.run_incarnation,
        AdaptiveLoopKind.RESEARCH,
        before.revision,
    )
    planned = checkpoint.record_planned(
        key,
        expected_revision=None if before.revision == 0 else before.revision - 1,
        action={
            "action": action.model_dump(mode="json", by_alias=True),
            "contract_version": 1,
            "decision": replay_input.decision.model_dump(
                mode="json",
                by_alias=True,
            ),
            "invocation_id": replay_input.probe_result.invocation_id,
            "kind": "research_planned",
            "resolution_digest": resolution_digest,
            "state_digest": canonical_digest(before),
        },
        semantic_repair_continuation=semantic_repair_continuation,
    )
    observed = checkpoint.record_observed(
        key,
        expected_revision=before.revision,
        action={
            "contract_version": 1,
            "kind": "research_observed",
            "novel": replayed.novelty.is_novel,
            "result": replay_input.probe_result.model_dump(
                mode="json",
                by_alias=True,
            ),
            "resolution_digest": resolution_digest,
        },
    )
    return replay_input.model_copy(
        update={
            "planned_action_digest": planned.action_digest,
            "observed_action_digest": observed.action_digest,
        }
    )


def _create_honest_v2_database(path) -> None:
    from workflow.adaptive_research_state_store import _V2_OWNED_TABLE_SQL

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        AdaptiveStateStore._create_checkpoint_tables(connection)
        AdaptiveStateStore._migrate_v0_to_v1(connection)
        AdaptiveStateStore._migrate_v1_to_v2(connection)
        for statement in _V2_OWNED_TABLE_SQL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO adaptive_research_state_meta (key, value) VALUES (?, 2)",
            ("schema_version",),
        )


def _insert_v2_research_snapshot(path, state) -> None:
    from custom_tools.text_to_sql.adaptive.serialization import (
        canonical_digest,
        serialize_contract,
    )

    with sqlite3.connect(path) as connection:
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


def _insert_v2_checkpoint_event(path, key, phase, action) -> None:
    import hashlib

    from custom_tools.text_to_sql.adaptive.serialization import canonical_json_bytes

    action_json = canonical_json_bytes(action).decode("utf-8")
    action_digest = f"sha256:{hashlib.sha256(action_json.encode()).hexdigest()}"
    with sqlite3.connect(path) as connection:
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
        connection.execute(
            """
            INSERT INTO adaptive_checkpoint_heads (
                run_id, run_incarnation, loop_kind, revision
            ) VALUES (?, ?, ?, ?)
            """,
            (key.run_id, key.run_incarnation, key.loop_kind.value, key.revision),
        )


def _insert_v2_solver_transition(path, before, after, action) -> None:
    import hashlib

    from custom_tools.text_to_sql.adaptive.serialization import (
        canonical_json_bytes,
        serialize_contract,
    )

    before_bytes = serialize_contract(before)
    after_bytes = serialize_contract(after)
    before_digest = f"sha256:{hashlib.sha256(before_bytes).hexdigest()}"
    after_digest = f"sha256:{hashlib.sha256(after_bytes).hexdigest()}"
    action_bytes = canonical_json_bytes(action)
    action_digest = f"sha256:{hashlib.sha256(action_bytes).hexdigest()}"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_snapshots (
                run_id, run_incarnation, state_revision, source_action_revision,
                state_bytes, state_digest, created_at_ns
            ) VALUES (?, ?, ?, NULL, ?, ?, 1)
            """,
            (
                before.run_id,
                before.run_incarnation,
                before.revision,
                before_bytes,
                before_digest,
            ),
        )
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_actions (
                run_id, run_incarnation, action_revision, action_kind,
                base_state_revision, base_state_digest,
                result_state_revision, result_state_digest,
                candidate_id, execution_id, normalized_ast_digest,
                action_bytes, action_digest, created_at_ns
            ) VALUES (?, ?, 0, 'transition', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 2)
            """,
            (
                before.run_id,
                before.run_incarnation,
                before.revision,
                before_digest,
                after.revision,
                after_digest,
                action_bytes,
                action_digest,
            ),
        )
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_snapshots (
                run_id, run_incarnation, state_revision, source_action_revision,
                state_bytes, state_digest, created_at_ns
            ) VALUES (?, ?, ?, 0, ?, ?, 3)
            """,
            (
                after.run_id,
                after.run_incarnation,
                after.revision,
                after_bytes,
                after_digest,
            ),
        )
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_heads (
                run_id, run_incarnation, initial_state_revision, state_revision,
                state_digest, next_action_revision,
                pending_execution_action_revision, terminal_digest
            ) VALUES (?, ?, ?, ?, ?, 1, NULL, NULL)
            """,
            (
                before.run_id,
                before.run_incarnation,
                before.revision,
                after.revision,
                after_digest,
            ),
        )


def _solver_reentry_case(tmp_path):
    from custom_tools.text_to_sql.adaptive.models import (
        EvidenceSourceKind,
        ResearchState,
        SolverState,
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

    loaded, namespace = schema()
    del loaded
    raw_research = make_state(namespace)
    optional_item = raw_research.query_spec.semantic_items[0].model_copy(
        update={"required": False}
    )
    optional_query = raw_research.query_spec.model_copy(
        update={"semantic_items": (optional_item,)}
    )
    research = ResearchState.model_validate(
        {
            **raw_research.model_dump(mode="python"),
            "query_spec": optional_query,
            "unresolved_items": (),
        }
    )
    authority = validate_coverage_inputs(
        research,
        freshness(research),
        research.run_id,
        research.run_incarnation,
    )
    initial = SolverState(
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
    stopped = apply_solver_proposal(
        initial,
        SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id="source-1",
                question="Which source is authoritative?",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="One exact observation is required.",
            ),
        ),
        base_revision=initial.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=authority,
        id_factory=iter(("request-reentry", "action-reentry")).__next__,
    ).state
    admitted = admit_targeted_reentry(
        stopped,
        research,
        "request-reentry",
        base_revision=stopped.revision,
        id_factory=lambda: "reentry-1",
    )
    path = tmp_path / "reentry.sqlite"
    AdaptiveResearchStateStore(path).save_research_state(
        research,
        expected_previous_revision=None,
    )
    store = AdaptiveSolverCheckpointStore(path)
    store.initialize(stopped)
    return store, research, authority, stopped, admitted


def test_replay_input_contract_is_closed_canonical_and_has_no_secret_field() -> None:
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        ResearchTerminalReplayInput,
        deserialize_replay_input,
        serialize_replay_input,
    )

    replay_input = ResearchTerminalReplayInput(freshness_context=_freshness())
    encoded = serialize_replay_input(replay_input)

    assert deserialize_replay_input(encoded) == replay_input
    assert serialize_replay_input(deserialize_replay_input(encoded)) == encoded
    assert b"transcript" not in encoded
    assert b"prompt" not in encoded
    assert b"chain_of_thought" not in encoded
    assert b"gold" not in encoded
    assert b"benchmark" not in encoded
    assert b"dsn" not in encoded
    assert b"credentials" not in encoded
    assert b"host_url" not in encoded

    payload = json.loads(encoded)
    payload["raw_dsn"] = "postgresql://secret@host/database"
    with pytest.raises(ValidationError):
        ResearchTerminalReplayInput.model_validate(payload)


def test_replay_serializer_rejects_model_copy_forged_kind_and_datetime() -> None:
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        ResearchTerminalReplayInput,
        serialize_replay_input,
    )

    replay_input = ResearchTerminalReplayInput(freshness_context=_freshness())
    forged_kind = replay_input.model_copy(update={"input_kind": "forged"})
    forged_freshness = replay_input.freshness_context.model_copy(
        update={"evaluated_at": "2026-08-02T00:00:00Z"}
    )
    forged_datetime = replay_input.model_copy(
        update={"freshness_context": forged_freshness}
    )

    with pytest.raises((TypeError, ValueError)):
        serialize_replay_input(forged_kind)
    with pytest.raises((TypeError, ValueError)):
        serialize_replay_input(forged_datetime)


def test_parsed_sql_candidate_replay_value_is_canonical_and_verified() -> None:
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        ParsedSqlCandidateReplayValue,
    )

    parsed = parse_sql_candidate(
        "SELECT o.id FROM orders o",
        "sqlite://",
        "candidate-replay-1",
    )
    stored = ParsedSqlCandidateReplayValue.from_candidate(parsed)

    assert stored.to_candidate() == parsed
    assert (
        ParsedSqlCandidateReplayValue.model_validate_json(stored.model_dump_json())
        == stored
    )

    tampered = stored.model_copy(update={"wire_digest": "sha256:" + "0" * 64})
    with pytest.raises(ValueError, match="wire_digest"):
        tampered.to_candidate()


def test_live_and_replay_use_same_parsed_ast() -> None:
    sql = "SELECT o.id FROM orders o"
    live = parse_sql_candidate(sql, "sqlite://", "candidate-replay-1")
    replayed = parse_sql_candidate(sql, "sqlite://", "candidate-replay-1")

    assert replayed == live


def test_v3_migration_adds_only_immutable_replay_input_tables(tmp_path) -> None:
    db_path = tmp_path / "replay-input-v3.db"
    state_store = AdaptiveStateStore(db_path)
    research_store = AdaptiveResearchStateStore(db_path)
    state_store.close()
    research_store.close()

    with sqlite3.connect(db_path) as connection:
        state_version = connection.execute(
            "SELECT value FROM adaptive_checkpoint_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        research_version = connection.execute(
            "SELECT value FROM adaptive_research_state_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        replay_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'adaptive%replay_inputs'"
            )
        }
        replay_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'adaptive%replay_inputs_no_%'"
            )
        }

    assert state_version == 3
    assert research_version == 3
    assert replay_tables == {
        "adaptive_checkpoint_replay_inputs",
        "adaptive_research_replay_inputs",
        "adaptive_solver_checkpoint_replay_inputs",
    }
    assert replay_triggers == {
        "adaptive_checkpoint_replay_inputs_no_delete",
        "adaptive_checkpoint_replay_inputs_no_update",
        "adaptive_research_replay_inputs_no_delete",
        "adaptive_research_replay_inputs_no_update",
        "adaptive_solver_checkpoint_replay_inputs_no_delete",
        "adaptive_solver_checkpoint_replay_inputs_no_update",
    }


def test_v2_to_v3_preserves_legacy_rows_without_backfill(tmp_path) -> None:
    path = tmp_path / "seeded-v2.db"
    before, _, _ = _research_replay_case()
    _create_honest_v2_database(path)
    _insert_v2_research_snapshot(path, before)
    key = AdaptiveCheckpointKey(
        before.run_id,
        before.run_incarnation,
        AdaptiveLoopKind.RESEARCH,
        0,
    )
    _insert_v2_checkpoint_event(path, key, "planned", {"kind": "legacy-planned"})

    migrated_checkpoint = AdaptiveStateStore(path)
    migrated_research = AdaptiveResearchStateStore(path)
    assert migrated_checkpoint.get_snapshot(key).planned is not None
    assert (
        migrated_research.load_latest_research_state(
            before.run_id,
            before.run_incarnation,
        )
        == before
    )
    assert (
        migrated_research.load_research_replay_input(
            before.run_id,
            before.run_incarnation,
            before.revision,
        )
        is None
    )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM adaptive_checkpoint_replay_inputs"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM adaptive_solver_checkpoint_replay_inputs"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM adaptive_research_replay_inputs"
            ).fetchone()[0]
            == 0
        )


def test_research_successor_and_replay_input_commit_atomically(tmp_path) -> None:
    path = tmp_path / "research-atomic.db"
    store = AdaptiveResearchStateStore(path)
    before, after, replay_input = _research_replay_case()
    store.save_research_state(before, expected_previous_revision=None)
    replay_input = _record_research_journal(path, before, replay_input)

    assert (
        store.save_replayable_semantic_transition(
            before,
            after,
            replay_input,
        )
        == after
    )
    assert (
        store.save_replayable_semantic_transition(
            before,
            after,
            replay_input,
        )
        == after
    )
    assert (
        store.load_research_replay_input(
            after.run_id,
            after.run_incarnation,
            after.revision,
        )
        == replay_input
    )
    with pytest.raises(AdaptiveResearchStateStoreConflictError, match="replay input"):
        store.save_research_state(
            after,
            expected_previous_revision=before.revision,
        )


def test_research_transition_after_prepared_reentry_uses_current_segment_journal(
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        ResearchTerminalReplayInput,
    )
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
    from workflow._text_to_sql_reentry_recovery import (
        build_prepared_targeted_reentry_commit,
    )

    path = tmp_path / "research-after-prepared-reentry.db"
    store = AdaptiveResearchStateStore(path)
    before, bridge, _ = _research_replay_case()
    store.save_research_state(before, expected_previous_revision=None)
    checkpoint = AdaptiveStateStore(path)
    checkpoint.record_replayable_terminal(
        AdaptiveCheckpointKey(
            before.run_id,
            before.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            before.revision,
        ),
        expected_revision=None,
        action={"kind": "research_terminal", "reason": "COMPLETE"},
        replay_input=ResearchTerminalReplayInput(
            freshness_context=freshness(before)
        ),
    )
    bridge_action = bridge.action_history[-1]
    plan = build_prepared_targeted_reentry_commit(
        run_id=before.run_id,
        run_incarnation=before.run_incarnation,
        research_reentry_id="reentry-1",
        missing_evidence_request_id="request-1",
        source_id="source-1",
        ordinal=1,
        base_solver_revision=1,
        solver_admission_digest="sha256:" + "a" * 64,
        store_base_research_revision=before.revision,
        store_base_research_digest=canonical_digest(before),
        projected_research=before,
        projected_research_digest=canonical_digest(before),
        action=bridge_action,
        hypotheses=(),
        bindings=(),
        join_candidates=(),
        invocation_id="invocation-prepared",
        reservation_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64,
        schema_namespace_version=before.schema_namespace_version,
    )
    store.prepare_targeted_reentry_commit(plan)
    store.commit_prepared_targeted_reentry(plan, bridge)

    _, successor, replay_input = _research_replay_case(
        before=bridge,
        tool_name="inspect_column",
        tool_arguments={"table": "public.orders", "column": "status"},
    )
    replay_input = _record_research_journal(
        path,
        bridge,
        replay_input,
        semantic_repair_continuation=True,
    )

    assert store.save_replayable_semantic_transition(
        bridge,
        successor,
        replay_input,
    ) == successor


def test_research_first_commit_rejects_unlinked_journal_digests(tmp_path) -> None:
    path = tmp_path / "research-unlinked.db"
    store = AdaptiveResearchStateStore(path)
    before, after, replay_input = _research_replay_case()
    store.save_research_state(before, expected_previous_revision=None)

    with pytest.raises(AdaptiveResearchStateStoreConflictError, match="journal"):
        store.save_replayable_semantic_transition(before, after, replay_input)

    assert (
        store.load_latest_research_state(before.run_id, before.run_incarnation)
        == before
    )


def test_research_first_commit_rejects_co_tampered_replay_and_successor(
    tmp_path,
) -> None:
    path = tmp_path / "research-first-co-tamper.db"
    store = AdaptiveResearchStateStore(path)
    before, after, replay_input = _research_replay_case()
    store.save_research_state(before, expected_previous_revision=None)
    linked_input = _record_research_journal(path, before, replay_input)
    tampered_budget = linked_input.budget_state.model_copy(
        update={"used_rows": 1, "remaining_rows": 99}
    )
    co_tampered_input = linked_input.model_copy(
        update={
            "budget_state": tampered_budget,
            "planned_action_digest": "sha256:" + "3" * 64,
        }
    )
    co_tampered_after = type(after).model_validate(
        {
            **after.model_dump(mode="python"),
            "budget_state": tampered_budget,
        }
    )

    with pytest.raises(AdaptiveResearchStateStoreConflictError, match="journal"):
        store.save_replayable_semantic_transition(
            before,
            co_tampered_after,
            co_tampered_input,
        )

    assert (
        store.load_latest_research_state(before.run_id, before.run_incarnation)
        == before
    )


def test_research_replay_input_crash_rolls_back_successor(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "research-crash.db"
    store = AdaptiveResearchStateStore(path)
    before, after, replay_input = _research_replay_case()
    store.save_research_state(before, expected_previous_revision=None)
    replay_input = _record_research_journal(path, before, replay_input)

    def crash(*_args, **_kwargs):
        raise RuntimeError("crash before replay input")

    monkeypatch.setattr(store, "_insert_replay_input", crash)
    with pytest.raises(RuntimeError, match="crash before replay input"):
        store.save_replayable_semantic_transition(before, after, replay_input)

    assert (
        store.load_latest_research_state(before.run_id, before.run_incarnation)
        == before
    )
    assert (
        store.load_research_replay_input(
            after.run_id,
            after.run_incarnation,
            after.revision,
        )
        is None
    )


def test_research_replay_input_conflict_and_co_tamper_fail_closed(tmp_path) -> None:
    path = tmp_path / "research-conflict.db"
    store = AdaptiveResearchStateStore(path)
    before, after, replay_input = _research_replay_case()
    store.save_research_state(before, expected_previous_revision=None)
    replay_input = _record_research_journal(path, before, replay_input)
    store.save_replayable_semantic_transition(before, after, replay_input)

    conflicting = replay_input.model_copy(
        update={"planned_action_digest": "sha256:" + "3" * 64}
    )
    with pytest.raises(
        AdaptiveResearchStateStoreConflictError,
        match="conflicting duplicate",
    ):
        store.save_replayable_semantic_transition(before, after, conflicting)

    tampered_budget = replay_input.budget_state.model_copy(
        update={"initial_rows": 101, "remaining_rows": 101}
    )
    co_tampered = replay_input.model_copy(update={"budget_state": tampered_budget})
    with pytest.raises(
        AdaptiveResearchStateStoreConflictError,
        match="reproduce",
    ):
        store.save_replayable_semantic_transition(before, after, co_tampered)


def test_legacy_research_history_resumes_but_is_explicitly_replay_ineligible(
    tmp_path,
) -> None:
    path = tmp_path / "research-legacy.db"
    before, after, _ = _research_replay_case()
    _create_honest_v2_database(path)
    _insert_v2_research_snapshot(path, before)
    _insert_v2_research_snapshot(path, after)
    store = AdaptiveResearchStateStore(path)

    assert (
        store.load_latest_research_state(before.run_id, before.run_incarnation) == after
    )
    assert (
        store.save_research_state(
            after,
            expected_previous_revision=before.revision,
        )
        == after
    )
    assert (
        store.load_research_replay_input(
            after.run_id,
            after.run_incarnation,
            after.revision,
        )
        is None
    )


def test_v3_legacy_research_save_cannot_create_semantic_successor(tmp_path) -> None:
    store = AdaptiveResearchStateStore(tmp_path / "research-v3-bypass.db")
    before, after, _ = _research_replay_case()
    store.save_research_state(before, expected_previous_revision=None)

    with pytest.raises(AdaptiveResearchStateStoreConflictError, match="replay input"):
        store.save_research_state(after, expected_previous_revision=before.revision)


def test_v3_legacy_terminal_write_cannot_create_research_terminal(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "terminal-v3-bypass.db")
    key = AdaptiveCheckpointKey(
        RUN_ID,
        INCARNATION,
        AdaptiveLoopKind.RESEARCH,
        0,
    )

    with pytest.raises(AdaptiveCheckpointConflictError, match="replay input"):
        store.record_terminal(
            key,
            expected_revision=None,
            action={"kind": "research_terminal", "reason": "COMPLETE"},
        )


def test_honest_v2_legacy_terminal_allows_exact_retry_after_migration(
    tmp_path,
) -> None:
    path = tmp_path / "terminal-v2-retry.db"
    key = AdaptiveCheckpointKey(
        RUN_ID,
        INCARNATION,
        AdaptiveLoopKind.RESEARCH,
        0,
    )
    terminal = {"kind": "research_terminal", "reason": "COMPLETE"}
    _create_honest_v2_database(path)
    _insert_v2_checkpoint_event(path, key, "terminal", terminal)
    store = AdaptiveStateStore(path)

    assert (
        store.record_terminal(
            key,
            expected_revision=None,
            action=terminal,
        )
        == store.get_snapshot(key).terminal
    )
    assert store.load_terminal_replay_input(key) is None


def test_research_terminal_and_freshness_input_are_atomic(
    tmp_path, monkeypatch
) -> None:
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        ResearchTerminalReplayInput,
    )

    key = AdaptiveCheckpointKey(
        RUN_ID,
        INCARNATION,
        AdaptiveLoopKind.RESEARCH,
        0,
    )
    terminal_input = ResearchTerminalReplayInput(freshness_context=_freshness())
    store = AdaptiveStateStore(tmp_path / "terminal-atomic.db")
    terminal = {"kind": "research_terminal", "reason": "COMPLETE"}

    mismatched = ResearchTerminalReplayInput(
        freshness_context=_freshness().model_copy(update={"run_id": "other-run"})
    )
    with pytest.raises(ValueError, match="checkpoint identity"):
        store.record_replayable_terminal(
            key,
            expected_revision=None,
            action=terminal,
            replay_input=mismatched,
        )

    assert store.record_replayable_terminal(
        key,
        expected_revision=None,
        action=terminal,
        replay_input=terminal_input,
    ) == store.record_replayable_terminal(
        key,
        expected_revision=None,
        action=terminal,
        replay_input=terminal_input,
    )
    assert store.load_terminal_replay_input(key) == terminal_input
    with pytest.raises(AdaptiveCheckpointConflictError, match="replay input"):
        store.record_terminal(
            key,
            expected_revision=None,
            action=terminal,
        )

    conflicting = ResearchTerminalReplayInput(
        freshness_context=_freshness().model_copy(
            update={"evaluated_at": _freshness().evaluated_at + timedelta(seconds=1)}
        )
    )
    with pytest.raises(AdaptiveCheckpointConflictError, match="replay input"):
        store.record_replayable_terminal(
            key,
            expected_revision=None,
            action=terminal,
            replay_input=conflicting,
        )

    crash_key = AdaptiveCheckpointKey(
        "run-terminal-crash",
        "inc-terminal-crash",
        AdaptiveLoopKind.RESEARCH,
        0,
    )
    crash_input = ResearchTerminalReplayInput(
        freshness_context=_freshness().model_copy(
            update={
                "run_id": crash_key.run_id,
                "run_incarnation": crash_key.run_incarnation,
            }
        )
    )

    def crash(*_args, **_kwargs):
        raise RuntimeError("crash before terminal replay input")

    monkeypatch.setattr(store, "_insert_checkpoint_replay_input", crash)
    with pytest.raises(RuntimeError, match="crash before terminal replay input"):
        store.record_replayable_terminal(
            crash_key,
            expected_revision=None,
            action=terminal,
            replay_input=crash_input,
        )
    assert store.get_snapshot(crash_key).terminal is None
    assert store.load_terminal_replay_input(crash_key) is None


@pytest.mark.parametrize("proposal_kind", ("sql", "missing"))
def test_solver_transition_replay_input_is_atomic_idempotent_and_secret_free(
    tmp_path,
    monkeypatch,
    proposal_kind,
) -> None:
    from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal

    case = solver_case()
    before = solver_state(case)
    if proposal_kind == "sql":
        proposal = sql_solver_proposal(
            "SELECT o.status FROM orders o WHERE o.status = 'active'"
        )
        ids = ("candidate-atomic", "action-atomic")
    else:
        proposal = missing_solver_proposal()
        ids = ("request-atomic", "action-atomic")
    transition = apply_solver_proposal(
        before,
        proposal,
        base_revision=before.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=case.requirements,
        id_factory=solver_ids(*ids),
    )
    assert transition.replay_input is not None

    path = tmp_path / f"solver-{proposal_kind}.db"
    store = AdaptiveSolverCheckpointStore(path)
    store.initialize(before)
    action = transition.action.model_dump(mode="json")
    committed = store.commit_non_execution(
        before,
        transition.state,
        action_revision=0,
        action=action,
        replay_input=transition.replay_input,
    )
    assert (
        store.commit_non_execution(
            before,
            transition.state,
            action_revision=0,
            action=action,
            replay_input=transition.replay_input,
        )
        == committed
    )
    assert (
        store.load_transition_replay_input(
            before.run_id,
            before.run_incarnation,
            0,
        )
        == transition.replay_input
    )
    with pytest.raises(
        AdaptiveSolverCheckpointConflictError,
        match="replay input",
    ):
        store.commit_non_execution(
            before,
            transition.state,
            action_revision=0,
            action=action,
        )
    tampered_ids = tuple(f"tampered-{index}" for index in range(len(ids)))
    co_tampered = transition.replay_input.model_copy(
        update={"generated_ids": tampered_ids}
    )
    with pytest.raises(
        AdaptiveSolverCheckpointConflictError,
        match="conflicting duplicate|reproduce",
    ):
        store.commit_non_execution(
            before,
            transition.state,
            action_revision=0,
            action=action,
            replay_input=co_tampered,
        )

    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT input_bytes FROM adaptive_solver_checkpoint_replay_inputs"
        ).fetchone()[0]
    assert POSTGRES_DSN.encode() not in raw
    assert b"password" not in raw
    assert b"postgresql://" not in raw

    crash_path = tmp_path / f"solver-{proposal_kind}-crash.db"
    crashing = AdaptiveSolverCheckpointStore(crash_path)
    crashing.initialize(before)

    def crash(*_args, **_kwargs):
        raise RuntimeError("crash before solver replay input")

    monkeypatch.setattr(crashing, "_insert_transition_replay_input", crash)
    with pytest.raises(RuntimeError, match="crash before solver replay input"):
        crashing.commit_non_execution(
            before,
            transition.state,
            action_revision=0,
            action=action,
            replay_input=transition.replay_input,
        )
    rolled_back = crashing.load(before.run_id, before.run_incarnation)
    assert rolled_back is not None
    assert rolled_back.state == before
    assert rolled_back.cursor.next_action_revision == 0
    assert (
        crashing.load_transition_replay_input(
            before.run_id,
            before.run_incarnation,
            0,
        )
        is None
    )


def test_sql_proposal_replay_input_reopens_after_owner_validation(tmp_path) -> None:
    from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal

    case = solver_case()
    before = solver_state(case)
    transition = apply_solver_proposal(
        before,
        sql_solver_proposal("SELECT o.status FROM orders o WHERE o.status = 'active'"),
        base_revision=before.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=case.requirements,
        id_factory=solver_ids("candidate-reopen", "action-reopen"),
    )
    path = tmp_path / "solver-reopen.db"
    store = AdaptiveSolverCheckpointStore(path)
    store.initialize(before)
    committed = store.commit_non_execution(
        before,
        transition.state,
        action_revision=0,
        action=transition.action.model_dump(mode="json"),
        replay_input=transition.replay_input,
    )
    store.close()

    reopened = AdaptiveSolverCheckpointStore(path)
    assert reopened.load(before.run_id, before.run_incarnation) == committed
    assert (
        reopened.load_transition_replay_input(
            before.run_id,
            before.run_incarnation,
            0,
        )
        == transition.replay_input
    )
    reopened.close()


def test_legacy_solver_transition_is_rejected_without_replay_fallback(tmp_path) -> None:
    from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal

    case = solver_case()
    before = solver_state(case)
    transition = apply_solver_proposal(
        before,
        missing_solver_proposal(),
        base_revision=before.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=case.requirements,
        id_factory=solver_ids("request-legacy", "action-legacy"),
    )
    path = tmp_path / "solver-legacy.db"
    action = transition.action.model_dump(mode="json")
    _create_honest_v2_database(path)
    _insert_v2_solver_transition(path, before, transition.state, action)
    store = AdaptiveSolverCheckpointStore(path)
    with pytest.raises(
        AdaptiveSolverCheckpointCorruptionError,
        match="requires replay input",
    ):
        store.load(before.run_id, before.run_incarnation)
    with pytest.raises(
        AdaptiveSolverCheckpointCorruptionError,
        match="requires replay input",
    ):
        store.commit_non_execution(
            before,
            transition.state,
            action_revision=0,
            action=action,
        )


def test_v3_legacy_solver_commit_cannot_create_missing_evidence_transition(
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal

    case = solver_case()
    before = solver_state(case)
    transition = apply_solver_proposal(
        before,
        missing_solver_proposal(),
        base_revision=before.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=case.requirements,
        id_factory=solver_ids("request-v3-bypass", "action-v3-bypass"),
    )
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver-v3-bypass.db")
    store.initialize(before)

    with pytest.raises(AdaptiveSolverCheckpointConflictError, match="replay input"):
        store.commit_non_execution(
            before,
            transition.state,
            action_revision=0,
            action=transition.action.model_dump(mode="json"),
        )


def test_reentry_admission_replay_rejects_record_derived_from_after_state(
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        SolverReentryAdmissionReplayInput,
    )
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest

    store, research, _, before, admitted = _solver_reentry_case(tmp_path)
    forged_record = admitted.record.model_copy(
        update={"baseline_evidence_ids": ("forged-baseline",)}
    )
    forged_after = type(admitted.state).model_validate(
        {
            **admitted.state.model_dump(mode="python"),
            "research_reentries": (forged_record,),
        }
    )
    replay_input = SolverReentryAdmissionReplayInput(
        research_state_revision=research.revision,
        research_state_digest=canonical_digest(research),
        missing_evidence_request_id="request-reentry",
        generated_reentry_id="reentry-1",
    )

    with pytest.raises(
        AdaptiveSolverCheckpointConflictError,
        match="research snapshot|reproduce",
    ):
        store.commit_non_execution(
            before,
            forged_after,
            action_revision=0,
            action={
                "kind": "research_reentry_admitted",
                "record": forged_record.model_dump(mode="json"),
            },
            replay_input=replay_input,
        )


def test_reentry_completed_replay_uses_independent_research_snapshot(tmp_path) -> None:
    from custom_tools.text_to_sql.adaptive.models import ResearchReentryStatus
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        SolverReentryAdmissionReplayInput,
        SolverReentryCompletedReplayInput,
    )
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest

    store, research, authority, before, admitted = _solver_reentry_case(tmp_path)
    admission_input = SolverReentryAdmissionReplayInput(
        research_state_revision=research.revision,
        research_state_digest=canonical_digest(research),
        missing_evidence_request_id="request-reentry",
        generated_reentry_id="reentry-1",
    )
    store.commit_non_execution(
        before,
        admitted.state,
        action_revision=0,
        action={
            "kind": "research_reentry_admitted",
            "record": admitted.record.model_dump(mode="json"),
        },
        replay_input=admission_input,
    )
    with pytest.raises(AdaptiveSolverCheckpointConflictError, match="replay input"):
        store.commit_non_execution(
            before,
            admitted.state,
            action_revision=0,
            action={
                "kind": "research_reentry_admitted",
                "record": admitted.record.model_dump(mode="json"),
            },
        )
    missing_revision = research.revision + 1
    forged_record = admitted.record.model_copy(
        update={
            "status": ResearchReentryStatus.COMPLETED,
            "research_result_revision": missing_revision,
            "evidence_ids": ("forged-evidence",),
        }
    )
    forged_after = type(admitted.state).model_validate(
        {
            **admitted.state.model_dump(mode="python"),
            "revision": admitted.state.revision + 1,
            "research_reentries": (forged_record,),
            "stop_reason": None,
        }
    )
    missing_authority_payload = authority.model_dump(
        mode="python",
        exclude={"requirements_digest"},
    )
    missing_authority_payload["state_revision"] = missing_revision
    missing_authority = type(authority)(
        **missing_authority_payload,
        requirements_digest=canonical_digest(missing_authority_payload),
    )
    replay_input = SolverReentryCompletedReplayInput(
        research_reentry_id="reentry-1",
        research_state_revision=missing_revision,
        research_state_digest="sha256:" + "4" * 64,
        freshness_context=freshness(research),
        requirements=missing_authority,
    )

    with pytest.raises(
        AdaptiveSolverCheckpointConflictError,
        match="research snapshot|reproduce",
    ):
        store.commit_non_execution(
            admitted.state,
            forged_after,
            action_revision=1,
            action={
                "kind": "research_reentry_finalized",
                "record": forged_record.model_dump(mode="json"),
            },
            replay_input=replay_input,
        )


def test_completed_reentry_replay_input_uses_durable_research_snapshot(
    tmp_path,
) -> None:
    from custom_tools.text_to_sql.adaptive.models import ResearchReentryStatus
    from custom_tools.text_to_sql.adaptive.semantic_coverage import (
        validate_coverage_inputs,
    )
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
    from workflow.text_to_sql_adaptive_solver import (
        _completed_reentry_replay_input,
    )

    solver_store, research, authority, _, admitted = _solver_reentry_case(tmp_path)
    _, durable, research_replay_input = _research_replay_case(before=research)
    research_store = AdaptiveResearchStateStore(solver_store.db_path)
    research_replay_input = _record_research_journal(
        solver_store.db_path,
        research,
        research_replay_input,
    )
    research_store.save_replayable_semantic_transition(
        research,
        durable,
        research_replay_input,
    )
    projected_budget = durable.budget_state.model_copy(
        update={
            "used_model_calls": durable.budget_state.used_model_calls + 1,
            "remaining_model_calls": durable.budget_state.remaining_model_calls - 1,
        }
    )
    projected = durable.model_copy(update={"budget_state": projected_budget})
    completed = admitted.record.model_copy(
        update={
            "status": ResearchReentryStatus.COMPLETED,
            "research_result_revision": durable.revision,
        }
    )
    context = freshness(durable)
    projected_authority = validate_coverage_inputs(
        projected,
        context,
        projected.run_id,
        projected.run_incarnation,
    )

    replay_input = _completed_reentry_replay_input(
        completed,
        projected,
        context,
        projected_authority,
        research_state_store=research_store,
    )

    assert replay_input.research_state_digest == canonical_digest(durable)
    assert replay_input.requirements == validate_coverage_inputs(
        durable,
        context,
        durable.run_id,
        durable.run_incarnation,
    )
