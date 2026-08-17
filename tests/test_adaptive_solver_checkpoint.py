"""Durable SolverState checkpoints and non-replayable execution reservations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

import pytest

from custom_tools.text_to_sql.adaptive.models import (
    PredicateOperator,
    SemanticItemKind,
    SolverState,
    SolverStopReason,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_json_bytes,
    serialize_contract,
)
from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal
from custom_tools.text_to_sql.adaptive.solver_protocol import (
    SolverProposalV1,
    SqlCandidateProposal,
)
from text_to_sql_semantic_checks_helpers import POSTGRES_DSN, ItemSpec, build_case
from text_to_sql_semantic_coverage_helpers import _context
from workflow.adaptive_solver_checkpoint import (
    AdaptiveSolverCheckpointCasError,
    AdaptiveSolverCheckpointConflictError,
    AdaptiveSolverCheckpointCorruptionError,
    AdaptiveSolverCheckpointPendingExecutionError,
    AdaptiveSolverCheckpointReplayError,
    AdaptiveSolverCheckpointStore,
)
from workflow._adaptive_solver_checkpoint_sql import (
    SOLVER_CHECKPOINT_SCHEMA_SQL,
)


def _initial_state(*, revision: int = 1, incarnation: str | None = None) -> SolverState:
    case = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        (
            ItemSpec(
                source_id="status",
                kind=SemanticItemKind.FILTER,
                table="orders",
                column="status",
                operator=PredicateOperator.EQ,
                literal="active",
            ),
        ),
    )
    values = {
        "run_id": case.state.run_id,
        "run_incarnation": incarnation or case.state.run_incarnation,
        "revision": revision,
        "schema_namespace_version": case.state.schema_namespace_version,
        "query_spec": case.query_spec.model_copy(
            update={"run_incarnation": incarnation or case.state.run_incarnation}
        ),
        "sql_candidates": (),
        "check_results": (),
        "execution_results": (),
        "missing_evidence_requests": (),
        "action_history": (),
        "selected_candidate_id": None,
        "stop_reason": None,
    }
    return SolverState.model_validate(values)


def _candidate_state() -> SolverState:
    state = _initial_state()
    requirements = validate_coverage_inputs(
        build_case(
            "SELECT o.status FROM orders o WHERE o.status = 'active'",
            (
                ItemSpec(
                    source_id="status",
                    kind=SemanticItemKind.FILTER,
                    table="orders",
                    column="status",
                    operator=PredicateOperator.EQ,
                    literal="active",
                ),
            ),
        ).state,
        _context(),
        state.run_id,
        state.run_incarnation,
    )
    ids = iter(("candidate-1", "action-1"))
    return apply_solver_proposal(
        state,
        SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql="SELECT o.status FROM orders o WHERE o.status = 'active'",
            ),
        ),
        base_revision=state.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=requirements,
        id_factory=ids.__next__,
    ).state


def _advance(
    state: SolverState,
    *,
    stop_reason: SolverStopReason | None = None,
) -> SolverState:
    return SolverState.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": state.revision + 1,
            "stop_reason": stop_reason,
        }
    )


def _schema_statement(fragment: str) -> str:
    return next(
        statement
        for statement in SOLVER_CHECKPOINT_SCHEMA_SQL
        if fragment in statement
    )


def _execution_successor(
    state: SolverState,
    *,
    candidate_id: str,
    normalized_ast_digest: str,
) -> SolverState:
    candidate = state.sql_candidates[-1].model_copy(
        update={
            "candidate_id": candidate_id,
            "normalized_ast_digest": normalized_ast_digest,
        }
    )
    action = state.action_history[-1].model_copy(
        update={"candidate_id": candidate_id}
    )
    return SolverState.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": state.revision + 1,
            "sql_candidates": (candidate,),
            "action_history": (action,),
        }
    )


def _inject_second_execution(
    store: AdaptiveSolverCheckpointStore,
    base_state: SolverState,
    *,
    candidate_id: str,
    execution_id: str,
    normalized_ast_digest: str,
) -> None:
    result_state = _advance(base_state)
    base_bytes = serialize_contract(base_state)
    result_state_bytes = serialize_contract(result_state)
    base_digest = "sha256:" + hashlib.sha256(base_bytes).hexdigest()
    result_state_digest = "sha256:" + hashlib.sha256(result_state_bytes).hexdigest()
    action_bytes = canonical_json_bytes(
        {
            "candidate_id": candidate_id,
            "execution_id": execution_id,
            "normalized_ast_digest": normalized_ast_digest,
            "request": {"row_limit": 10},
        }
    )
    action_digest = "sha256:" + hashlib.sha256(action_bytes).hexdigest()
    result_bytes = canonical_json_bytes({"success": True})
    result_digest = "sha256:" + hashlib.sha256(result_bytes).hexdigest()
    identity = (base_state.run_id, base_state.run_incarnation)
    with sqlite3.connect(store.db_path) as connection:
        for index_name in (
            "adaptive_solver_checkpoint_execution_candidate_unique",
            "adaptive_solver_checkpoint_execution_id_unique",
            "adaptive_solver_checkpoint_execution_ast_unique",
        ):
            connection.execute(f"DROP INDEX {index_name}")
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_actions (
                run_id, run_incarnation, action_revision, action_kind,
                base_state_revision, base_state_digest,
                result_state_revision, result_state_digest,
                candidate_id, execution_id, normalized_ast_digest,
                action_bytes, action_digest, created_at_ns
            ) VALUES (?, ?, 1, 'execution', ?, ?, NULL, NULL, ?, ?, ?, ?, ?, 2)
            """,
            (
                *identity,
                base_state.revision,
                base_digest,
                candidate_id,
                execution_id,
                normalized_ast_digest,
                action_bytes,
                action_digest,
            ),
        )
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_execution_reconciliations (
                run_id, run_incarnation, action_revision, outcome,
                result_state_revision, result_state_digest,
                result_bytes, result_digest, created_at_ns
            ) VALUES (?, ?, 1, 'KNOWN', ?, ?, ?, ?, 2)
            """,
            (
                *identity,
                result_state.revision,
                result_state_digest,
                result_bytes,
                result_digest,
            ),
        )
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_snapshots (
                run_id, run_incarnation, state_revision, source_action_revision,
                state_bytes, state_digest, created_at_ns
            ) VALUES (?, ?, ?, 1, ?, ?, 2)
            """,
            (
                *identity,
                result_state.revision,
                result_state_bytes,
                result_state_digest,
            ),
        )
        connection.execute(
            """
            UPDATE adaptive_solver_checkpoint_heads
            SET state_revision = ?, state_digest = ?, next_action_revision = 2
            WHERE run_id = ? AND run_incarnation = ?
            """,
            (result_state.revision, result_state_digest, *identity),
        )


def _inject_sealed_legacy_known_terminal(
    store,
    reservation,
    after_state,
    terminal_bytes,
) -> None:
    state_bytes = serialize_contract(after_state)
    state_digest = "sha256:" + hashlib.sha256(state_bytes).hexdigest()
    terminal_digest = "sha256:" + hashlib.sha256(terminal_bytes).hexdigest()
    identity = (after_state.run_id, after_state.run_incarnation)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_execution_reconciliations (
                run_id, run_incarnation, action_revision, outcome,
                result_state_revision, result_state_digest,
                result_bytes, result_digest, created_at_ns
            ) VALUES (?, ?, ?, 'KNOWN', ?, ?, ?, ?, 2)
            """,
            (
                *identity,
                reservation.action_revision,
                after_state.revision,
                state_digest,
                terminal_bytes,
                terminal_digest,
            ),
        )
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_snapshots (
                run_id, run_incarnation, state_revision, source_action_revision,
                state_bytes, state_digest, created_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, 2)
            """,
            (
                *identity,
                after_state.revision,
                reservation.action_revision,
                state_bytes,
                state_digest,
            ),
        )
        connection.execute(
            """
            UPDATE adaptive_solver_checkpoint_heads
            SET state_revision = ?, state_digest = ?,
                pending_execution_action_revision = NULL
            WHERE run_id = ? AND run_incarnation = ?
            """,
            (after_state.revision, state_digest, *identity),
        )
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_terminals (
                run_id, run_incarnation, state_revision, state_digest,
                next_action_revision, terminal_bytes, terminal_digest,
                created_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 2)
            """,
            (
                *identity,
                after_state.revision,
                state_digest,
                reservation.action_revision + 1,
                terminal_bytes,
                terminal_digest,
            ),
        )
        connection.execute(
            """
            UPDATE adaptive_solver_checkpoint_heads
            SET terminal_digest = ?
            WHERE run_id = ? AND run_incarnation = ?
            """,
            (terminal_digest, *identity),
        )


def test_initial_revision_is_independent_from_action_revision(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _initial_state(revision=7)

    checkpoint = store.initialize(state)

    assert checkpoint.state == state
    assert checkpoint.cursor.initial_state_revision == 7
    assert checkpoint.cursor.state_revision == 7
    assert checkpoint.cursor.next_action_revision == 0
    assert checkpoint.cursor.pending_execution_action_revision is None
    assert store.initialize(state) == checkpoint
    assert (
        AdaptiveSolverCheckpointStore(store.db_path).load(
            state.run_id, state.run_incarnation
        )
        == checkpoint
    )


def test_non_execution_commit_is_atomic_and_action_revisions_are_contiguous(
    tmp_path,
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    initial = _initial_state()
    store.initialize(initial)
    second = _advance(initial)
    first = store.commit_non_execution(
        initial,
        second,
        action_revision=0,
        action={"kind": "candidate"},
    )
    third = _advance(second)
    last = store.commit_non_execution(
        second,
        third,
        action_revision=1,
        action={"kind": "check"},
    )

    assert first.cursor.next_action_revision == 1
    assert last.state == third
    assert last.cursor.next_action_revision == 2
    assert (
        store.commit_non_execution(
            initial,
            second,
            action_revision=0,
            action={"kind": "candidate"},
        ).state
        == second
    )
    with pytest.raises(AdaptiveSolverCheckpointConflictError):
        store.commit_non_execution(
            initial,
            second,
            action_revision=0,
            action={"kind": "different"},
        )
    with pytest.raises(AdaptiveSolverCheckpointCasError):
        store.commit_non_execution(
            third,
            _advance(third),
            action_revision=3,
            action={"kind": "gap"},
        )


def test_non_execution_failure_rolls_back_journal_snapshot_and_head(
    tmp_path, monkeypatch
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    initial = _initial_state()
    store.initialize(initial)
    real_insert = store._insert_snapshot

    def fail_after_snapshot(*args, **kwargs):
        real_insert(*args, **kwargs)
        raise RuntimeError("crash after snapshot")

    monkeypatch.setattr(store, "_insert_snapshot", fail_after_snapshot)
    with pytest.raises(RuntimeError, match="crash after snapshot"):
        store.commit_non_execution(
            initial,
            _advance(initial),
            action_revision=0,
            action={"kind": "candidate"},
        )

    resumed = AdaptiveSolverCheckpointStore(store.db_path).load(
        initial.run_id, initial.run_incarnation
    )
    assert resumed is not None
    assert resumed.state == initial
    assert resumed.cursor.next_action_revision == 0


def test_two_non_execution_writers_have_one_cas_winner(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    initial = _initial_state()
    store.initialize(initial)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def write(marker: str) -> None:
        barrier.wait(timeout=10)
        try:
            store.commit_non_execution(
                initial,
                _advance(initial),
                action_revision=0,
                action={"writer": marker},
            )
            outcomes.append("written")
        except (
            AdaptiveSolverCheckpointCasError,
            AdaptiveSolverCheckpointConflictError,
        ):
            outcomes.append("rejected")

    threads = [threading.Thread(target=write, args=(marker,)) for marker in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["rejected", "written"]


def test_execution_reservation_is_durable_and_never_regrants_execution(
    tmp_path,
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"row_limit": 10},
    )

    resumed = AdaptiveSolverCheckpointStore(store.db_path).load(
        state.run_id, state.run_incarnation
    )
    assert resumed is not None
    assert resumed.pending_execution == reservation
    assert resumed.cursor.next_action_revision == 1
    assert resumed.cursor.pending_execution_action_revision == 0
    with pytest.raises(AdaptiveSolverCheckpointReplayError):
        store.reserve_execution(
            state,
            action_revision=0,
            candidate_id=candidate.candidate_id,
            execution_id="execution-1",
            request={"row_limit": 10},
        )
    with pytest.raises(AdaptiveSolverCheckpointPendingExecutionError):
        store.commit_non_execution(
            state,
            _advance(state),
            action_revision=1,
            action={"kind": "blocked"},
        )


def test_known_execution_reconciliation_is_atomic_and_idempotent(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"row_limit": 10},
    )
    result_state = _advance(state)
    checkpoint = store.reconcile_execution(
        reservation,
        result_state,
        result={"success": False, "error": "known"},
    )

    assert checkpoint.state == result_state
    assert checkpoint.cursor.pending_execution_action_revision is None
    assert checkpoint.cursor.next_action_revision == 1
    assert (
        store.reconcile_execution(
            reservation,
            result_state,
            result={"success": False, "error": "known"},
        )
        == checkpoint
    )
    with pytest.raises(AdaptiveSolverCheckpointConflictError):
        store.reconcile_execution(
            reservation,
            result_state,
            result={"success": True},
        )


def test_unknown_execution_terminal_reconciliation_seals_null_result(
    tmp_path,
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"operation": "finalize_text_to_sql_run"},
    )
    result_state = _advance(state, stop_reason=SolverStopReason.TOOL_FAILURE)
    terminal_bytes = canonical_json_bytes({"reason_code": "EXECUTION_UNKNOWN"})

    checkpoint = store.reconcile_execution_terminal(
        reservation,
        result_state,
        outcome="UNKNOWN",
        terminal_bytes=terminal_bytes,
    )

    assert checkpoint.state == result_state
    assert checkpoint.pending_execution is None
    assert checkpoint.terminal is not None
    assert checkpoint.terminal.terminal_bytes == terminal_bytes
    assert checkpoint.cursor.pending_execution_action_revision is None
    with sqlite3.connect(store.db_path) as connection:
        stored_outcome, result_bytes, result_digest = connection.execute(
            """
            SELECT outcome, result_bytes, result_digest
            FROM adaptive_solver_checkpoint_execution_reconciliations
            """
        ).fetchone()
    assert stored_outcome == "UNKNOWN"
    assert result_bytes == b"null"
    assert result_digest == (
        "sha256:" + hashlib.sha256(b"null").hexdigest()
    )
    assert (
        store.reconcile_execution_terminal(
            reservation,
            result_state,
            outcome="UNKNOWN",
            terminal_bytes=terminal_bytes,
        )
        == checkpoint
    )


def test_known_execution_terminal_writer_requires_verified_evidence(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"operation": "finalize_text_to_sql_run"},
    )

    with pytest.raises(TypeError, match="verified terminal evidence"):
        store.reconcile_execution_terminal(
            reservation,
            _advance(state),
            outcome="KNOWN",
            terminal_bytes=canonical_json_bytes({"status": "succeeded"}),
        )

    checkpoint = store.load(state.run_id, state.run_incarnation)
    assert checkpoint is not None
    assert checkpoint.state == state
    assert checkpoint.pending_execution == reservation
    assert checkpoint.terminal is None
    with sqlite3.connect(store.db_path) as connection:
        count = connection.execute(
            """
            SELECT COUNT(*)
            FROM adaptive_solver_checkpoint_execution_reconciliations
            """
        ).fetchone()[0]
    assert count == 0


def test_unknown_execution_terminal_writer_rejects_verified_evidence(tmp_path) -> None:
    from test_text_to_sql_adaptive_solver import (
        _ready_state,
        _reservation,
        _successful_terminal,
    )
    from workflow.text_to_sql_adaptive_solver import reconcile_known_finalizer

    state = _ready_state()
    known_store = AdaptiveSolverCheckpointStore(tmp_path / "known.sqlite")
    known_store.initialize(state)
    known = reconcile_known_finalizer(
        known_store,
        _reservation(known_store, state),
        state,
        _successful_terminal(state),
    )
    assert known.verified_terminal_evidence is not None

    unknown_store = AdaptiveSolverCheckpointStore(tmp_path / "unknown.sqlite")
    unknown_store.initialize(state)
    reservation = _reservation(unknown_store, state)
    with pytest.raises(ValueError, match="cannot carry terminal evidence"):
        unknown_store.reconcile_execution_terminal(
            reservation,
            _advance(state, stop_reason=SolverStopReason.TOOL_FAILURE),
            outcome="UNKNOWN",
            terminal_bytes=canonical_json_bytes(
                {"reason_code": "EXECUTION_UNKNOWN"}
            ),
            verified_terminal_evidence=known.verified_terminal_evidence,
        )


def test_verified_terminal_envelope_loads_idempotently_and_replays_legacy_result(
    tmp_path,
) -> None:
    from test_text_to_sql_adaptive_solver import (
        _ready_state,
        _reservation,
        _successful_terminal,
    )
    from workflow.text_to_sql_adaptive_solver import reconcile_known_finalizer

    path = tmp_path / "solver.sqlite"
    store = AdaptiveSolverCheckpointStore(path)
    state = _ready_state()
    store.initialize(state)
    reservation = _reservation(store, state)
    terminal = _successful_terminal(state)

    checkpoint = reconcile_known_finalizer(
        store,
        reservation,
        state,
        terminal,
    )

    assert checkpoint.terminal is not None
    assert checkpoint.verified_terminal_evidence is not None
    terminal_bytes = canonical_json_bytes(terminal)
    assert checkpoint.terminal.terminal_bytes == terminal_bytes
    with sqlite3.connect(path) as connection:
        result_bytes, result_digest = connection.execute(
            """
            SELECT result_bytes, result_digest
            FROM adaptive_solver_checkpoint_execution_reconciliations
            """
        ).fetchone()
    assert result_bytes != terminal_bytes
    assert terminal["sql"].encode() not in result_bytes
    assert json.loads(result_bytes)["record_kind"] == (
        "text2sql_solver_terminal_evidence"
    )
    assert result_digest == "sha256:" + hashlib.sha256(result_bytes).hexdigest()

    reopened = AdaptiveSolverCheckpointStore(path)
    assert reopened.load(state.run_id, state.run_incarnation) == checkpoint
    assert (
        reconcile_known_finalizer(reopened, reservation, state, terminal)
        == checkpoint
    )
    chain = reopened.load_replay_chain(state.run_id, state.run_incarnation)
    assert chain is not None and chain.terminal is not None
    assert canonical_json_bytes(chain.reconciliations[-1].result) == terminal_bytes
    assert (
        chain.reconciliations[-1].result_digest
        == checkpoint.terminal.terminal_digest
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "future",
        "extra",
        "run_id",
        "run_incarnation",
        "schema",
        "query_id",
        "query_digest",
        "state_revision",
        "state_digest",
        "action_revision",
        "base_revision",
        "base_digest",
        "candidate_id",
        "execution_id",
        "ast_digest",
        "request_digest",
        "terminal_digest",
        "terminal_flag",
    ),
)
def test_unrecognized_or_mismatched_sealed_evidence_is_not_trusted(
    tmp_path,
    mutation,
) -> None:
    from test_text_to_sql_adaptive_solver import (
        _ready_state,
        _reservation,
        _successful_terminal,
    )
    from workflow.text_to_sql_adaptive_solver import reconcile_known_finalizer

    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _ready_state()
    store.initialize(state)
    checkpoint = reconcile_known_finalizer(
        store,
        _reservation(store, state),
        state,
        _successful_terminal(state),
    )
    assert checkpoint.terminal is not None
    with sqlite3.connect(store.db_path) as connection:
        raw = connection.execute(
            """
            SELECT result_bytes
            FROM adaptive_solver_checkpoint_execution_reconciliations
            """
        ).fetchone()[0]
        document = json.loads(raw)
        if mutation == "future":
            document["schema_version"] = 2
        elif mutation == "extra":
            document["unexpected"] = True
        elif mutation == "run_id":
            document["run_id"] = "other-run"
        elif mutation == "run_incarnation":
            document["run_incarnation"] = "other-incarnation"
        elif mutation == "schema":
            document["schema_namespace_version"] = "sha256:" + "0" * 64
        elif mutation == "query_id":
            document["query"]["query_id"] = "other-query"
        elif mutation == "query_digest":
            document["query"]["digest"] = "sha256:" + "0" * 64
        elif mutation == "state_revision":
            document["solver_state"]["revision"] += 1
        elif mutation == "state_digest":
            document["solver_state"]["digest"] = "sha256:" + "0" * 64
        elif mutation == "action_revision":
            document["reservation"]["action_revision"] += 1
        elif mutation == "base_revision":
            document["reservation"]["base_state_revision"] += 1
        elif mutation == "base_digest":
            document["reservation"]["base_state_digest"] = "sha256:" + "0" * 64
        elif mutation == "candidate_id":
            document["reservation"]["candidate_id"] = "other-candidate"
        elif mutation == "execution_id":
            document["reservation"]["execution_id"] = "other-execution"
        elif mutation == "ast_digest":
            document["reservation"]["normalized_ast_digest"] = (
                "sha256:" + "0" * 64
            )
        elif mutation == "request_digest":
            document["reservation"]["request_digest"] = "sha256:" + "0" * 64
        elif mutation == "terminal_digest":
            document["terminal"]["digest"] = "sha256:" + "0" * 64
        else:
            document["terminal"]["generated"] = False
        changed = canonical_json_bytes(document)
        changed_digest = "sha256:" + hashlib.sha256(changed).hexdigest()
        connection.execute(
            "DROP TRIGGER "
            "adaptive_solver_checkpoint_execution_reconciliations_no_update"
        )
        connection.execute(
            """
            UPDATE adaptive_solver_checkpoint_execution_reconciliations
            SET result_bytes = ?, result_digest = ?
            """,
            (changed, changed_digest),
        )

    loaded = store.load(state.run_id, state.run_incarnation)
    assert loaded is not None
    assert loaded.terminal == checkpoint.terminal
    assert loaded.verified_terminal_evidence is None
    chain = store.load_replay_chain(state.run_id, state.run_incarnation)
    assert chain is not None
    assert canonical_json_bytes(chain.reconciliations[-1].result) == (
        checkpoint.terminal.terminal_bytes
    )


def test_record_terminal_rejects_unrecognized_known_result_envelope(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"row_limit": 10},
    )
    result_state = _advance(state)
    store.reconcile_execution(
        reservation,
        result_state,
        result={
            "schema_version": 2,
            "record_kind": "text2sql_solver_terminal_evidence",
        },
    )

    with pytest.raises(AdaptiveSolverCheckpointConflictError):
        store.record_terminal(
            result_state,
            expected_action_revision=1,
            terminal_bytes=canonical_json_bytes({"status": "succeeded"}),
        )


def test_execution_terminal_reconciliation_rolls_back_as_one_unit(
    tmp_path,
    monkeypatch,
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"operation": "finalize_text_to_sql_run"},
    )

    def crash_before_seal(*args, **kwargs):
        raise RuntimeError("crash before atomic terminal seal")

    monkeypatch.setattr(store, "_seal_terminal_head", crash_before_seal)
    with pytest.raises(RuntimeError, match="crash before atomic terminal seal"):
        store.reconcile_execution_terminal(
            reservation,
            _advance(state, stop_reason=SolverStopReason.TOOL_FAILURE),
            outcome="UNKNOWN",
            terminal_bytes=canonical_json_bytes(
                {"reason_code": "EXECUTION_UNKNOWN"}
            ),
        )

    resumed = store.load(state.run_id, state.run_incarnation)
    assert resumed is not None
    assert resumed.state == state
    assert resumed.pending_execution == reservation
    assert resumed.terminal is None


def test_verified_terminal_evidence_rolls_back_with_terminal(
    tmp_path,
    monkeypatch,
) -> None:
    from test_text_to_sql_adaptive_solver import (
        _ready_state,
        _reservation,
        _successful_terminal,
    )
    from workflow.text_to_sql_adaptive_solver import reconcile_known_finalizer

    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _ready_state()
    store.initialize(state)
    reservation = _reservation(store, state)

    def crash_before_seal(*args, **kwargs):
        raise RuntimeError("crash before atomic evidence seal")

    monkeypatch.setattr(store, "_seal_terminal_head", crash_before_seal)
    with pytest.raises(RuntimeError, match="crash before atomic evidence seal"):
        reconcile_known_finalizer(
            store,
            reservation,
            state,
            _successful_terminal(state),
        )

    resumed = store.load(state.run_id, state.run_incarnation)
    assert resumed is not None
    assert resumed.state == state
    assert resumed.pending_execution == reservation
    assert resumed.terminal is None
    assert resumed.verified_terminal_evidence is None
    with sqlite3.connect(store.db_path) as connection:
        reconciliation_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM adaptive_solver_checkpoint_execution_reconciliations
            """
        ).fetchone()[0]
    assert reconciliation_count == 0


def test_unknown_reconciliation_requires_caller_tool_failure_state(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"row_limit": 10},
    )

    with pytest.raises(ValueError, match="TOOL_FAILURE"):
        store.reconcile_unknown_execution(reservation, _advance(state))
    failure_state = _advance(state, stop_reason=SolverStopReason.TOOL_FAILURE)
    checkpoint = store.reconcile_unknown_execution(reservation, failure_state)

    assert checkpoint.state == failure_state
    assert checkpoint.state.execution_results == state.execution_results
    assert checkpoint.cursor.pending_execution_action_revision is None
    assert store.reconcile_unknown_execution(reservation, failure_state) == checkpoint


def test_reconciliation_failure_rolls_back_and_keeps_reservation_pending(
    tmp_path, monkeypatch
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"row_limit": 10},
    )
    real_insert = store._insert_snapshot

    def fail_after_snapshot(*args, **kwargs):
        real_insert(*args, **kwargs)
        raise RuntimeError("crash during reconciliation")

    monkeypatch.setattr(store, "_insert_snapshot", fail_after_snapshot)
    with pytest.raises(RuntimeError, match="crash during reconciliation"):
        store.reconcile_execution(
            reservation,
            _advance(state),
            result={"success": True},
        )

    resumed = store.load(state.run_id, state.run_incarnation)
    assert resumed is not None
    assert resumed.state == state
    assert resumed.pending_execution == reservation


def test_concurrent_known_and_unknown_reconciliation_has_one_winner(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"row_limit": 10},
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def reconcile_known() -> None:
        barrier.wait(timeout=10)
        try:
            store.reconcile_execution(
                reservation,
                _advance(state),
                result={"success": True},
            )
            outcomes.append("written")
        except AdaptiveSolverCheckpointConflictError:
            outcomes.append("rejected")

    def reconcile_unknown() -> None:
        barrier.wait(timeout=10)
        try:
            store.reconcile_unknown_execution(
                reservation,
                _advance(state, stop_reason=SolverStopReason.TOOL_FAILURE),
            )
            outcomes.append("written")
        except AdaptiveSolverCheckpointConflictError:
            outcomes.append("rejected")

    threads = [
        threading.Thread(target=reconcile_known),
        threading.Thread(target=reconcile_unknown),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["rejected", "written"]
    checkpoint = store.load(state.run_id, state.run_incarnation)
    assert checkpoint is not None
    assert checkpoint.cursor.pending_execution_action_revision is None


def test_concurrent_finalizer_known_and_unknown_preserve_evidence_boundary(
    tmp_path,
) -> None:
    from test_text_to_sql_adaptive_solver import (
        _ready_state,
        _reservation,
        _successful_terminal,
    )
    from workflow.text_to_sql_adaptive_solver import (
        reconcile_known_finalizer,
        reconcile_reserved_finalizer_unknown,
    )

    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _ready_state()
    store.initialize(state)
    reservation = _reservation(store, state)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def reconcile_known() -> None:
        barrier.wait(timeout=10)
        try:
            reconcile_known_finalizer(
                store,
                reservation,
                state,
                _successful_terminal(state),
            )
            outcomes.append("known")
        except AdaptiveSolverCheckpointConflictError:
            outcomes.append("rejected")

    def reconcile_unknown() -> None:
        barrier.wait(timeout=10)
        try:
            reconcile_reserved_finalizer_unknown(store, reservation, state)
            outcomes.append("unknown")
        except AdaptiveSolverCheckpointConflictError:
            outcomes.append("rejected")

    threads = [
        threading.Thread(target=reconcile_known),
        threading.Thread(target=reconcile_unknown),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert "rejected" in outcomes
    assert len(outcomes) == 2
    checkpoint = store.load(state.run_id, state.run_incarnation)
    assert checkpoint is not None and checkpoint.terminal is not None
    terminal = json.loads(checkpoint.terminal.terminal_bytes)
    if "known" in outcomes:
        assert terminal["reason_code"] == ""
        assert checkpoint.verified_terminal_evidence is not None
    else:
        assert terminal["reason_code"] == "EXECUTION_UNKNOWN"
        assert checkpoint.verified_terminal_evidence is None


def test_reconciled_execution_candidate_cannot_be_reserved_again(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"row_limit": 10},
    )
    next_state = _advance(state)
    store.reconcile_execution(
        reservation,
        next_state,
        result={"success": True},
    )

    with pytest.raises(AdaptiveSolverCheckpointReplayError):
        store.reserve_execution(
            next_state,
            action_revision=1,
            candidate_id=candidate.candidate_id,
            execution_id="execution-2",
            request={"row_limit": 20},
        )


def test_terminal_is_opaque_canonical_bytes_tied_to_exact_head(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _initial_state()
    store.initialize(state)
    terminal_bytes = canonical_json_bytes(
        {"contract": "future-w6-06", "payload": {"status": "done"}}
    )
    terminal = store.record_terminal(
        state,
        expected_action_revision=0,
        terminal_bytes=terminal_bytes,
    )

    assert terminal.terminal_bytes == terminal_bytes
    assert terminal.terminal_digest == (
        "sha256:" + hashlib.sha256(terminal_bytes).hexdigest()
    )
    with sqlite3.connect(store.db_path) as connection:
        marker = connection.execute(
            "SELECT terminal_digest FROM adaptive_solver_checkpoint_heads"
        ).fetchone()[0]
        assert marker == terminal.terminal_digest
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE adaptive_solver_checkpoint_heads SET terminal_digest = NULL"
            )
    assert (
        store.record_terminal(
            state,
            expected_action_revision=0,
            terminal_bytes=terminal_bytes,
        )
        == terminal
    )
    assert store.load(state.run_id, state.run_incarnation).terminal == terminal
    with pytest.raises(AdaptiveSolverCheckpointConflictError):
        store.record_terminal(
            state,
            expected_action_revision=0,
            terminal_bytes=canonical_json_bytes({"different": True}),
        )
    with pytest.raises(ValueError, match="canonical"):
        store.record_terminal(
            state,
            expected_action_revision=0,
            terminal_bytes=b'{"payload": 1}',
        )
    with pytest.raises(AdaptiveSolverCheckpointCasError):
        store.commit_non_execution(
            state,
            _advance(state),
            action_revision=0,
            action={"kind": "after terminal"},
        )


def test_record_terminal_rejects_raw_known_reconciliation(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"operation": "finalize_text_to_sql_run"},
    )
    result_state = _advance(state)
    terminal_bytes = canonical_json_bytes({"status": "succeeded"})
    store.reconcile_execution(
        reservation,
        result_state,
        result={"status": "succeeded"},
    )

    with pytest.raises(AdaptiveSolverCheckpointConflictError):
        store.record_terminal(
            result_state,
            expected_action_revision=1,
            terminal_bytes=canonical_json_bytes({"status": "different"}),
        )
    with pytest.raises(AdaptiveSolverCheckpointConflictError):
        store.record_terminal(
            result_state,
            expected_action_revision=1,
            terminal_bytes=terminal_bytes,
        )


def test_load_accepts_sealed_legacy_raw_known_terminal_as_untrusted(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"operation": "finalize_text_to_sql_run"},
    )
    after_state = _advance(state)
    terminal_bytes = canonical_json_bytes({"status": "succeeded"})
    _inject_sealed_legacy_known_terminal(
        store,
        reservation,
        after_state,
        terminal_bytes,
    )

    checkpoint = store.load(state.run_id, state.run_incarnation)
    assert checkpoint is not None and checkpoint.terminal is not None
    assert checkpoint.terminal.terminal_bytes == terminal_bytes
    assert checkpoint.verified_terminal_evidence is None
    assert (
        store.record_terminal(
            after_state,
            expected_action_revision=1,
            terminal_bytes=terminal_bytes,
        )
        == checkpoint.terminal
    )


def test_load_rejects_known_reconciliation_terminal_byte_mismatch(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"operation": "finalize_text_to_sql_run"},
    )
    _inject_sealed_legacy_known_terminal(
        store,
        reservation,
        _advance(state),
        canonical_json_bytes({"status": "succeeded"}),
    )

    tampered = canonical_json_bytes({"status": "different"})
    tampered_digest = "sha256:" + hashlib.sha256(tampered).hexdigest()
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "DROP TRIGGER adaptive_solver_checkpoint_terminals_no_update"
        )
        connection.execute(
            """
            UPDATE adaptive_solver_checkpoint_terminals
            SET terminal_bytes = ?, terminal_digest = ?
            """,
            (tampered, tampered_digest),
        )
        connection.execute(
            "DROP TRIGGER adaptive_solver_checkpoint_heads_terminal_digest_immutable"
        )
        connection.execute(
            "UPDATE adaptive_solver_checkpoint_heads SET terminal_digest = ?",
            (tampered_digest,),
        )

    with pytest.raises(AdaptiveSolverCheckpointCorruptionError):
        store.load(state.run_id, state.run_incarnation)


def test_terminal_marker_blocks_idempotent_transition_retry(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    initial = _initial_state()
    second = _advance(initial)
    store.initialize(initial)
    store.commit_non_execution(
        initial,
        second,
        action_revision=0,
        action={"kind": "transition"},
    )
    store.record_terminal(
        second,
        expected_action_revision=1,
        terminal_bytes=canonical_json_bytes({"status": "done"}),
    )

    with pytest.raises(AdaptiveSolverCheckpointCasError):
        store.commit_non_execution(
            initial,
            second,
            action_revision=0,
            action={"kind": "transition"},
        )


@pytest.mark.parametrize(
    "corruption",
    ("snapshot_digest", "action_gap", "head_digest", "wrong_incarnation"),
)
def test_load_rejects_corrupt_digest_gap_head_or_incarnation(
    tmp_path, corruption
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    initial = _initial_state()
    store.initialize(initial)
    store.commit_non_execution(
        initial,
        _advance(initial),
        action_revision=0,
        action={"kind": "candidate"},
    )
    with sqlite3.connect(store.db_path) as connection:
        if corruption == "snapshot_digest":
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_snapshots_no_update"
            )
            connection.execute(
                "UPDATE adaptive_solver_checkpoint_snapshots SET state_digest = ? "
                "WHERE state_revision = ?",
                ("sha256:" + "0" * 64, initial.revision + 1),
            )
        elif corruption == "action_gap":
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_actions_no_delete"
            )
            connection.execute(
                "DELETE FROM adaptive_solver_checkpoint_actions WHERE action_revision = 0"
            )
        elif corruption == "head_digest":
            connection.execute(
                "UPDATE adaptive_solver_checkpoint_heads SET state_digest = ?",
                ("sha256:" + "0" * 64,),
            )
        else:
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_snapshots_no_update"
            )
            foreign_state = _advance(_initial_state(incarnation="foreign-incarnation"))
            foreign_bytes = serialize_contract(foreign_state)
            connection.execute(
                "UPDATE adaptive_solver_checkpoint_snapshots "
                "SET state_bytes = ?, state_digest = ? WHERE state_revision = ?",
                (
                    foreign_bytes,
                    "sha256:" + hashlib.sha256(foreign_bytes).hexdigest(),
                    initial.revision + 1,
                ),
            )

    with pytest.raises(AdaptiveSolverCheckpointCorruptionError):
        store.load(initial.run_id, initial.run_incarnation)


@pytest.mark.parametrize(
    ("table", "column", "update_trigger"),
    (
        ("adaptive_solver_checkpoint_heads", "state_digest", None),
        (
            "adaptive_solver_checkpoint_snapshots",
            "state_digest",
            "adaptive_solver_checkpoint_snapshots_no_update",
        ),
    ),
)
@pytest.mark.parametrize("restart", (False, True))
def test_invalid_utf8_text_is_typed_corruption(
    tmp_path,
    table,
    column,
    update_trigger,
    restart,
) -> None:
    path = tmp_path / "solver.sqlite"
    store = AdaptiveSolverCheckpointStore(path)
    state = _initial_state()
    store.initialize(state)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        if update_trigger is not None:
            connection.execute(f"DROP TRIGGER {update_trigger}")
        connection.execute(
            f"UPDATE {table} SET {column} = CAST(X'80' AS TEXT)"
        )
        if update_trigger is not None:
            connection.execute(
                _schema_statement(f"CREATE TRIGGER {update_trigger}")
            )
    if restart:
        store.close()
        store = AdaptiveSolverCheckpointStore(path)

    with pytest.raises(AdaptiveSolverCheckpointCorruptionError):
        store.load(state.run_id, state.run_incarnation)


def test_non_decode_sqlite_operational_errors_are_not_reclassified(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")

    for message in (
        "database is locked",
        "database table is locked",
        "disk I/O error",
        "no such table: adaptive_solver_checkpoint_heads",
    ):
        error = sqlite3.OperationalError(message)

        class FailingConnection:
            def execute(self, *args, **kwargs):
                raise error

        with pytest.raises(sqlite3.OperationalError) as caught:
            store._load_checkpoint(FailingConnection(), "run-1", "inc-1")
        assert caught.value is error


@pytest.mark.parametrize(
    "corruption",
    (
        "snapshot_created_at_type",
        "transition_created_at_type",
        "reconciliation_created_at_type",
        "transition_candidate_id",
        "execution_result_state",
    ),
)
def test_load_rejects_storage_shape_corruption_when_sql_guards_are_bypassed(
    tmp_path, corruption
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    initial = _candidate_state()
    store.initialize(initial)
    candidate = initial.sql_candidates[-1]

    if corruption in {
        "snapshot_created_at_type",
        "transition_created_at_type",
        "transition_candidate_id",
    }:
        store.commit_non_execution(
            initial,
            _advance(initial),
            action_revision=0,
            action={"kind": "candidate"},
        )
    else:
        reservation = store.reserve_execution(
            initial,
            action_revision=0,
            candidate_id=candidate.candidate_id,
            execution_id="execution-1",
            request={"row_limit": 10},
        )
        if corruption == "reconciliation_created_at_type":
            store.reconcile_execution(
                reservation,
                _advance(initial),
                result={"success": True},
            )

    with sqlite3.connect(store.db_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        if corruption == "snapshot_created_at_type":
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_snapshots_no_update"
            )
            connection.execute(
                "UPDATE adaptive_solver_checkpoint_snapshots "
                "SET created_at_ns = 'bad' WHERE state_revision = ?",
                (initial.revision + 1,),
            )
        elif corruption == "transition_created_at_type":
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_actions_no_update"
            )
            connection.execute(
                "UPDATE adaptive_solver_checkpoint_actions "
                "SET created_at_ns = 'bad' WHERE action_revision = 0"
            )
        elif corruption == "reconciliation_created_at_type":
            connection.execute(
                "DROP TRIGGER "
                "adaptive_solver_checkpoint_execution_reconciliations_no_update"
            )
            connection.execute(
                "UPDATE adaptive_solver_checkpoint_execution_reconciliations "
                "SET created_at_ns = 'bad' WHERE action_revision = 0"
            )
        elif corruption == "transition_candidate_id":
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_actions_no_update"
            )
            connection.execute(
                "UPDATE adaptive_solver_checkpoint_actions "
                "SET candidate_id = 'forbidden' WHERE action_revision = 0"
            )
        else:
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_actions_no_update"
            )
            connection.execute(
                "UPDATE adaptive_solver_checkpoint_actions "
                "SET result_state_revision = ?, result_state_digest = ? "
                "WHERE action_revision = 0",
                (initial.revision + 1, "sha256:" + "0" * 64),
            )

    with pytest.raises(AdaptiveSolverCheckpointCorruptionError):
        store.load(initial.run_id, initial.run_incarnation)


def test_deleted_terminal_cannot_reopen_run_after_exact_trigger_restart(
    tmp_path,
) -> None:
    path = tmp_path / "solver.sqlite"
    store = AdaptiveSolverCheckpointStore(path)
    state = _initial_state()
    store.initialize(state)
    store.record_terminal(
        state,
        expected_action_revision=0,
        terminal_bytes=canonical_json_bytes({"status": "done"}),
    )
    store.close()

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DROP TRIGGER adaptive_solver_checkpoint_terminals_no_delete"
        )
        connection.execute("DELETE FROM adaptive_solver_checkpoint_terminals")
        connection.execute(
            _schema_statement(
                "CREATE TRIGGER adaptive_solver_checkpoint_terminals_no_delete"
            )
        )

    resumed = AdaptiveSolverCheckpointStore(path)
    with pytest.raises(AdaptiveSolverCheckpointCorruptionError):
        resumed.load(state.run_id, state.run_incarnation)
    with pytest.raises(AdaptiveSolverCheckpointCorruptionError):
        resumed.commit_non_execution(
            state,
            _advance(state),
            action_revision=0,
            action={"kind": "must-stay-closed"},
        )


def test_load_rejects_terminal_row_without_head_marker(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _initial_state()
    checkpoint = store.initialize(state)
    terminal_bytes = canonical_json_bytes({"status": "injected"})
    terminal_digest = "sha256:" + hashlib.sha256(terminal_bytes).hexdigest()
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            INSERT INTO adaptive_solver_checkpoint_terminals (
                run_id, run_incarnation, state_revision, state_digest,
                next_action_revision, terminal_bytes, terminal_digest,
                created_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.run_id,
                state.run_incarnation,
                state.revision,
                checkpoint.cursor.state_digest,
                0,
                terminal_bytes,
                terminal_digest,
                1,
            ),
        )

    with pytest.raises(AdaptiveSolverCheckpointCorruptionError):
        store.load(state.run_id, state.run_incarnation)


@pytest.mark.parametrize(
    "corruption",
    ("marker_digest", "terminal_state", "terminal_action_cursor"),
)
def test_load_rejects_terminal_marker_or_identity_mismatch(
    tmp_path,
    corruption,
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _initial_state()
    store.initialize(state)
    store.record_terminal(
        state,
        expected_action_revision=0,
        terminal_bytes=canonical_json_bytes({"status": "done"}),
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        if corruption == "marker_digest":
            connection.execute(
                "DROP TRIGGER "
                "adaptive_solver_checkpoint_heads_terminal_digest_immutable"
            )
            connection.execute(
                "UPDATE adaptive_solver_checkpoint_heads SET terminal_digest = ?",
                ("sha256:" + "0" * 64,),
            )
        else:
            connection.execute(
                "DROP TRIGGER adaptive_solver_checkpoint_terminals_no_update"
            )
            column = (
                "state_revision"
                if corruption == "terminal_state"
                else "next_action_revision"
            )
            connection.execute(
                f"UPDATE adaptive_solver_checkpoint_terminals SET {column} = ?",
                (state.revision + 1 if column == "state_revision" else 1,),
            )

    with pytest.raises(AdaptiveSolverCheckpointCorruptionError):
        store.load(state.run_id, state.run_incarnation)


def test_terminal_marker_without_terminal_row_is_corruption(tmp_path) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _initial_state()
    store.initialize(state)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE adaptive_solver_checkpoint_heads SET terminal_digest = ?",
            ("sha256:" + "0" * 64,),
        )

    with pytest.raises(AdaptiveSolverCheckpointCorruptionError):
        store.load(state.run_id, state.run_incarnation)


def test_terminal_record_and_head_marker_roll_back_together(
    tmp_path,
    monkeypatch,
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _initial_state()
    store.initialize(state)
    real_seal = store._seal_terminal_head

    def crash_before_head_seal(*args, **kwargs):
        raise RuntimeError("crash before terminal head seal")

    monkeypatch.setattr(store, "_seal_terminal_head", crash_before_head_seal)
    with pytest.raises(RuntimeError, match="crash before terminal head seal"):
        store.record_terminal(
            state,
            expected_action_revision=0,
            terminal_bytes=canonical_json_bytes({"status": "done"}),
        )

    monkeypatch.setattr(store, "_seal_terminal_head", real_seal)
    resumed = store.load(state.run_id, state.run_incarnation)
    assert resumed is not None
    assert resumed.terminal is None
    terminal = store.record_terminal(
        state,
        expected_action_revision=0,
        terminal_bytes=canonical_json_bytes({"status": "done"}),
    )
    assert terminal.terminal_digest


def test_terminal_marker_blocks_reservation_and_reconciliation_replay(
    tmp_path,
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=candidate.candidate_id,
        execution_id="execution-1",
        request={"row_limit": 10},
    )
    next_state = _advance(state)
    result = {"success": True}
    _inject_sealed_legacy_known_terminal(
        store,
        reservation,
        next_state,
        canonical_json_bytes(result),
    )

    with pytest.raises(AdaptiveSolverCheckpointCasError):
        store.reserve_execution(
            next_state,
            action_revision=1,
            candidate_id=candidate.candidate_id,
            execution_id="execution-2",
            request={"row_limit": 10},
        )
    with pytest.raises(AdaptiveSolverCheckpointCasError):
        store.reconcile_execution(reservation, next_state, result=result)


@pytest.mark.parametrize(
    "duplicate",
    ("candidate_id", "execution_id", "normalized_ast_digest", "all_three"),
)
def test_load_rejects_repeated_execution_identity_without_unique_indexes(
    tmp_path,
    duplicate,
) -> None:
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    state = _candidate_state()
    store.initialize(state)
    first_candidate = state.sql_candidates[-1]
    reservation = store.reserve_execution(
        state,
        action_revision=0,
        candidate_id=first_candidate.candidate_id,
        execution_id="execution-1",
        request={"row_limit": 10},
    )
    second_candidate_id = (
        first_candidate.candidate_id
        if duplicate in {"candidate_id", "all_three"}
        else "candidate-2"
    )
    second_execution_id = (
        reservation.execution_id
        if duplicate in {"execution_id", "all_three"}
        else "execution-2"
    )
    second_ast_digest = (
        first_candidate.normalized_ast_digest
        if duplicate in {"normalized_ast_digest", "all_three"}
        else "sha256:" + "f" * 64
    )
    base_state = _execution_successor(
        state,
        candidate_id=second_candidate_id,
        normalized_ast_digest=second_ast_digest,
    )
    store.reconcile_execution(
        reservation,
        base_state,
        result={"success": True},
    )
    _inject_second_execution(
        store,
        base_state,
        candidate_id=second_candidate_id,
        execution_id=second_execution_id,
        normalized_ast_digest=second_ast_digest,
    )

    with pytest.raises(AdaptiveSolverCheckpointCorruptionError):
        store.load(state.run_id, state.run_incarnation)
