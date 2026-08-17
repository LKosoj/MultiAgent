"""Durability and fail-closed checks for typed adaptive research snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3
import threading

import pytest

import workflow.adaptive_research_state_store as state_store_module

from custom_tools.text_to_sql.adaptive.models import (
    BudgetState,
    EvidenceCost,
    EvidenceRecord,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExpectedResultShape,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
    serialize_contract,
)
from workflow.adaptive_research_state_store import (
    AdaptiveResearchStateStore,
    AdaptiveResearchStateStoreCasError,
    AdaptiveResearchStateStoreConflictError,
    AdaptiveResearchStateStoreCorruptionError,
    AdaptiveResearchStateStoreMigrationError,
)
from workflow._text_to_sql_reentry_recovery import (
    build_prepared_targeted_reentry_commit,
)
from workflow.state_manager import SQLiteWorkflowStore


RUN_ID = "run-1"
INCARNATION = "inc-1"
SCHEMA = "schema:0123456789abcdef"


def _query_spec(*, revision: int = 0, text: str = "sales") -> QuerySpec:
    return QuerySpec(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=revision,
        schema_namespace_version=None,
        query_id="query-1",
        original_text=text,
        semantic_items=(
            SemanticItem(
                source_id="source-1",
                kind=SemanticItemKind.METRIC,
                source_text=text,
                normalized_meaning=text,
                required=True,
                operator=None,
                literal_or_reference=None,
                status=SemanticItemStatus.UNRESOLVED,
                binding_ids=(),
            ),
        ),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )


def _budget(*, initial: int = 1) -> BudgetState:
    return BudgetState(
        initial_wall_clock_ms=initial,
        used_wall_clock_ms=0,
        remaining_wall_clock_ms=initial,
        initial_model_calls=initial,
        used_model_calls=0,
        remaining_model_calls=initial,
        initial_model_tokens=initial,
        used_model_tokens=0,
        remaining_model_tokens=initial,
        initial_db_probe_ms=initial,
        used_db_probe_ms=0,
        remaining_db_probe_ms=initial,
        initial_rows=initial,
        used_rows=0,
        remaining_rows=initial,
        initial_bytes=initial,
        used_bytes=0,
        remaining_bytes=initial,
    )


def _research_state(*, revision: int, budget_initial: int = 1) -> ResearchState:
    actions = tuple(
        ResearchAction(
            action_id=f"action-{index}",
            kind=ResearchActionKind.INSPECT_TABLE,
            hypothesis_id=None,
            target=TableRef(namespace="main", schema=None, table="orders"),
            parameters=(("detail", f"revision-{index}"),),
            action_digest=canonical_action_digest(
                kind=ResearchActionKind.INSPECT_TABLE,
                hypothesis_id=None,
                target=TableRef(namespace="main", schema=None, table="orders"),
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
        schema_namespace_version=SCHEMA,
        query_spec=_query_spec(),
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=("source-1",),
        action_history=actions,
        result_expectations=(),
        budget_state=_budget(initial=budget_initial),
        stop_reason=None,
    )


def _prepared_reentry_plan():
    base = _research_state(revision=0)
    successor = _research_state(revision=1)
    action = successor.action_history[0]
    plan = build_prepared_targeted_reentry_commit(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        research_reentry_id="reentry-1",
        missing_evidence_request_id="request-1",
        source_id="source-1",
        ordinal=1,
        base_solver_revision=2,
        solver_admission_digest="sha256:" + "a" * 64,
        store_base_research_revision=base.revision,
        store_base_research_digest=canonical_digest(base),
        projected_research=base,
        projected_research_digest=canonical_digest(base),
        action=action,
        hypotheses=(),
        bindings=(),
        join_candidates=(),
        invocation_id="invocation-1",
        reservation_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64,
        schema_namespace_version=SCHEMA,
    )
    return base, successor, plan


def test_prepared_reentry_plan_is_append_only_and_idempotent(tmp_path) -> None:
    store = AdaptiveResearchStateStore(tmp_path / "prepared.db")
    base, _, plan = _prepared_reentry_plan()
    store.save_research_state(base, expected_previous_revision=None)

    assert store.prepare_targeted_reentry_commit(plan) == plan
    assert store.prepare_targeted_reentry_commit(plan) == plan
    assert (
        store.load_prepared_targeted_reentry_commit(
            RUN_ID,
            INCARNATION,
            "reentry-1",
        )
        == plan
    )
    conflicting = build_prepared_targeted_reentry_commit(
        **{
            **plan.model_dump(mode="python", round_trip=True),
            "invocation_id": "different-invocation",
            "plan_digest": None,
        }
    )
    with pytest.raises(AdaptiveResearchStateStoreConflictError):
        store.prepare_targeted_reentry_commit(conflicting)


def test_prepared_reentry_successor_and_commit_marker_are_atomic(tmp_path) -> None:
    store = AdaptiveResearchStateStore(tmp_path / "prepared-commit.db")
    base, successor, plan = _prepared_reentry_plan()
    store.save_research_state(base, expected_previous_revision=None)
    store.prepare_targeted_reentry_commit(plan)

    assert store.commit_prepared_targeted_reentry(plan, successor) == successor
    assert store.commit_prepared_targeted_reentry(plan, successor) == successor
    assert store.load_latest_research_state(RUN_ID, INCARNATION) == successor
    assert store.is_prepared_targeted_reentry_committed(plan) is True

    different = _research_state(revision=1, budget_initial=2)
    with pytest.raises(AdaptiveResearchStateStoreConflictError):
        store.commit_prepared_targeted_reentry(plan, different)


def test_committed_reentry_chain_advances_terminal_authority(tmp_path) -> None:
    store = AdaptiveResearchStateStore(tmp_path / "prepared-chain.db")
    base, successor, plan = _prepared_reentry_plan()
    store.save_research_state(base, expected_previous_revision=None)
    store.prepare_targeted_reentry_commit(plan)
    store.commit_prepared_targeted_reentry(plan, successor)

    assert store.load_verified_reentry_successor_chain(base) == successor


def test_store_rejects_bypassed_research_state_with_gapped_history(tmp_path) -> None:
    store = AdaptiveResearchStateStore(tmp_path / "gapped-state.db")
    forged = _research_state(revision=1).model_copy(update={"action_history": ()})
    try:
        with pytest.raises(ValueError, match="every revision from zero"):
            store.save_research_state(forged, expected_previous_revision=None)
        assert store.load_latest_research_state(RUN_ID, INCARNATION) is None
    finally:
        store.close()


def test_snapshots_reopen_and_remain_separate_from_action_journal(tmp_path) -> None:
    path = tmp_path / "state.db"
    store = AdaptiveResearchStateStore(path)
    query_spec = _query_spec()
    state = _research_state(revision=0)

    assert store.save_query_spec(query_spec) == query_spec
    assert store.save_research_state(state, expected_previous_revision=None) == state
    store.close()

    reopened = AdaptiveResearchStateStore(path)
    assert reopened.load_query_spec(RUN_ID, INCARNATION) == query_spec
    assert reopened.load_latest_research_state(RUN_ID, INCARNATION) == state
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "adaptive_research_state_snapshots" in tables
        assert "adaptive_checkpoint_events" not in tables


def test_legacy_evidence_snapshot_reopens_without_rewrite_or_schema_change(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-evidence.db"
    legacy_evidence = EvidenceRecord(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        evidence_id="legacy-evidence-1",
        source_kind=EvidenceSourceKind.SCHEMA,
        target=TableRef(namespace="main", schema=None, table="orders"),
        action_digest="sha256:" + "a" * 64,
        observation=canonical_json_bytes(
            {
                "artifact_reference": None,
                "byte_count": 2,
                "invocation_id": "legacy-evidence-1",
                "payload": {},
                "payload_digest": canonical_digest({}),
                "probe_kind": "inspect_table",
                "row_count": 0,
                "storage": "inline",
                "summary": "legacy observation without provenance",
                "truncated": False,
            }
        ).decode("utf-8"),
        validity_scope=EvidenceValidityScope.SCHEMA_VERSION,
        data_snapshot_token=None,
        observed_at=datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
        strength=1.0,
        created_at=datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=0,
            bytes=2,
        ),
    )
    state = _research_state(revision=0).model_copy(
        update={"evidence": (legacy_evidence,)}
    )
    store = AdaptiveResearchStateStore(path)
    store.save_research_state(state, expected_previous_revision=None)
    before = _state_store_durable_bytes(path)
    store.close()

    reopened = AdaptiveResearchStateStore(path)
    loaded = reopened.load_latest_research_state(RUN_ID, INCARNATION)
    reopened.close()
    after = _state_store_durable_bytes(path)

    assert loaded == state
    assert before == after
    payload, digest, schema_version, _ = before
    assert json.loads(payload)["contract_version"] == 1
    assert digest == canonical_digest(state)
    assert schema_version == 3
    assert b'"provenance"' not in payload
    assert b'"observation_version"' not in payload


def _state_store_durable_bytes(path) -> tuple[bytes, str, int, tuple]:
    with sqlite3.connect(path) as connection:
        payload, digest = connection.execute(
            """
            SELECT payload, digest
            FROM adaptive_research_state_snapshots
            WHERE contract_name = 'research_state'
            """
        ).fetchone()
        schema_version = connection.execute(
            """
            SELECT value FROM adaptive_research_state_meta
            WHERE key = 'schema_version'
            """
        ).fetchone()[0]
        objects = tuple(
            connection.execute(
                """
                SELECT type, name, sql FROM sqlite_master
                WHERE lower(name) LIKE 'adaptive_research_state_%'
                ORDER BY type, name
                """
            )
        )
    return payload, digest, schema_version, objects


def test_query_spec_retries_are_idempotent_but_conflicts_and_skips_fail(
    tmp_path,
) -> None:
    store = AdaptiveResearchStateStore(tmp_path / "state.db")
    query_spec = _query_spec()
    assert store.save_query_spec(query_spec) == query_spec
    assert store.save_query_spec(query_spec) == query_spec

    with pytest.raises(AdaptiveResearchStateStoreConflictError):
        store.save_query_spec(_query_spec(text="orders"))
    with pytest.raises(AdaptiveResearchStateStoreCasError, match="monotonic"):
        store.save_query_spec(_query_spec(revision=2))


def test_idempotent_retry_records_both_contracts_as_durable(tmp_path) -> None:
    class _CommitGuard:
        def __init__(self) -> None:
            self.contracts: list[str] = []

        def require_active(self) -> None:
            return None

        def commit(self, connection, *, contract_name: str) -> None:
            connection.commit()
            self.contracts.append(contract_name)

    store = AdaptiveResearchStateStore(tmp_path / "state.db")
    query_spec = _query_spec()
    research_state = _research_state(revision=0)
    store.save_query_spec(query_spec)
    store.save_research_state(research_state, expected_previous_revision=None)
    guard = _CommitGuard()

    assert store.save_query_spec(query_spec, commit_guard=guard) == query_spec
    assert (
        store.save_research_state(
            research_state,
            expected_previous_revision=None,
            commit_guard=guard,
        )
        == research_state
    )
    assert guard.contracts == ["query_spec", "research_state"]


def test_research_state_cas_and_concurrent_writers_allow_one_transition(
    tmp_path,
) -> None:
    store = AdaptiveResearchStateStore(tmp_path / "state.db")
    initial = _research_state(revision=0)
    store.save_research_state(initial, expected_previous_revision=None)
    assert (
        store.save_research_state(initial, expected_previous_revision=None) == initial
    )
    with pytest.raises(AdaptiveResearchStateStoreConflictError, match="replay input"):
        store.save_research_state(
            _research_state(revision=1), expected_previous_revision=None
        )

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def write(budget_initial: int) -> None:
        barrier.wait(timeout=10)
        try:
            store.save_research_state(
                _research_state(revision=1, budget_initial=budget_initial),
                expected_previous_revision=0,
            )
            outcomes.append("written")
        except (
            AdaptiveResearchStateStoreCasError,
            AdaptiveResearchStateStoreConflictError,
        ):
            outcomes.append("rejected")

    threads = [threading.Thread(target=write, args=(value,)) for value in (2, 3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["rejected", "rejected"]
    assert store.load_latest_research_state(RUN_ID, INCARNATION) == initial


def test_research_state_can_be_loaded_at_one_exact_revision(tmp_path) -> None:
    store = AdaptiveResearchStateStore(tmp_path / "state.db")
    initial = _research_state(revision=0)
    store.save_research_state(initial, expected_previous_revision=None)

    assert store.load_research_state(RUN_ID, INCARNATION, revision=0) == initial
    assert store.load_research_state(RUN_ID, INCARNATION, revision=1) is None


@pytest.mark.parametrize(
    "column, value", [("payload", b"not-json"), ("digest", "sha256:bad")]
)
def test_damaged_snapshot_bytes_or_digest_fail_closed(tmp_path, column, value) -> None:
    path = tmp_path / "state.db"
    store = AdaptiveResearchStateStore(path)
    store.save_query_spec(_query_spec())
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"UPDATE adaptive_research_state_snapshots SET {column} = ?",
            (value,),
        )
    with pytest.raises(AdaptiveResearchStateStoreCorruptionError):
        store.load_query_spec(RUN_ID, INCARNATION)


def test_wrong_payload_identity_version_or_type_fails_closed(tmp_path) -> None:
    path = tmp_path / "state.db"
    store = AdaptiveResearchStateStore(path)
    query_spec = _query_spec()
    store.save_query_spec(query_spec)

    wrong_identity = query_spec.model_copy(update={"run_id": "other-run"})
    _replace_payload(
        path, serialize_contract(wrong_identity), canonical_digest(wrong_identity)
    )
    with pytest.raises(AdaptiveResearchStateStoreCorruptionError, match="identity"):
        store.load_query_spec(RUN_ID, INCARNATION)

    versioned = json.loads(serialize_contract(query_spec))
    versioned["contract_version"] = 2
    _replace_payload(path, canonical_json_bytes(versioned), "sha256:unchanged")
    with pytest.raises(AdaptiveResearchStateStoreCorruptionError, match="payload"):
        store.load_query_spec(RUN_ID, INCARNATION)

    research = _research_state(revision=0)
    _replace_payload(path, serialize_contract(research), canonical_digest(research))
    with pytest.raises(AdaptiveResearchStateStoreCorruptionError, match="payload"):
        store.load_query_spec(RUN_ID, INCARNATION)


def test_additive_migration_preserves_existing_workflow_database_and_lazy_getter(
    tmp_path,
) -> None:
    path = tmp_path / "existing.db"
    workflow_store = SQLiteWorkflowStore(str(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workflow_checkpoints'"
        ).fetchone()

    first = workflow_store.get_adaptive_research_state_store()
    assert first is workflow_store.get_adaptive_research_state_store()
    first.save_query_spec(_query_spec())
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workflow_checkpoints'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM adaptive_research_state_snapshots"
        ).fetchone()


def _replace_payload(path, payload: bytes, digest: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE adaptive_research_state_snapshots SET payload = ?, digest = ?",
            (payload, digest),
        )


def test_same_column_forged_schema_without_required_checks_is_rejected(
    tmp_path,
) -> None:
    path = tmp_path / "forged.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE adaptive_research_state_snapshots (
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                contract_name TEXT NOT NULL,
                revision INTEGER NOT NULL,
                payload BLOB NOT NULL,
                digest TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL,
                PRIMARY KEY (run_id, run_incarnation, contract_name, revision)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE adaptive_research_state_meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO adaptive_research_state_meta (key, value) VALUES ('schema_version', 1)"
        )

    with pytest.raises(AdaptiveResearchStateStoreMigrationError, match="constraints"):
        AdaptiveResearchStateStore(path)


def test_comment_only_constraint_text_does_not_validate_schema(tmp_path) -> None:
    path = tmp_path / "comment-forged.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE adaptive_research_state_snapshots (
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                contract_name TEXT NOT NULL, /* CHECK(contract_name IN ('query_spec', 'research_state')) */
                revision INTEGER NOT NULL,
                payload BLOB NOT NULL,
                digest TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL,
                PRIMARY KEY (run_id, run_incarnation, contract_name, revision)
            )
            """
        )
        connection.execute(
            "CREATE TABLE adaptive_research_state_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO adaptive_research_state_meta (key, value) VALUES ('schema_version', 1)"
        )

    with pytest.raises(AdaptiveResearchStateStoreMigrationError, match="constraints"):
        AdaptiveResearchStateStore(path)


def test_schema_validation_rejects_missing_check_despite_existing_invalid_row(
    tmp_path,
) -> None:
    path = tmp_path / "constraint-probe-collision.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE adaptive_research_state_snapshots (
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                contract_name TEXT NOT NULL,
                revision INTEGER NOT NULL
                    CHECK (typeof(revision) = 'integer' AND revision >= 0),
                payload BLOB NOT NULL CHECK (typeof(payload) = 'blob'),
                digest TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL
                    CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
                PRIMARY KEY (run_id, run_incarnation, contract_name, revision)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE adaptive_research_state_meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO adaptive_research_state_snapshots (
                run_id, run_incarnation, contract_name, revision,
                payload, digest, created_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("bad-contract", "inc", "unexpected", 0, b"{}", "sha256:0", 0),
        )
        connection.execute(
            """
            INSERT INTO adaptive_research_state_meta (key, value)
            VALUES ('schema_version', 1)
            """
        )

    with pytest.raises(AdaptiveResearchStateStoreMigrationError, match="constraints"):
        AdaptiveResearchStateStore(path)


def test_fixed_value_check_spoof_is_rejected(tmp_path) -> None:
    path = tmp_path / "weak-checks.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE adaptive_research_state_snapshots (
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                contract_name TEXT NOT NULL CHECK (contract_name != 'unexpected'),
                revision INTEGER NOT NULL CHECK (revision != -1),
                payload BLOB NOT NULL CHECK (typeof(payload) != 'text'),
                digest TEXT NOT NULL,
                created_at_ns INTEGER NOT NULL CHECK (created_at_ns != -1),
                PRIMARY KEY (run_id, run_incarnation, contract_name, revision)
            );
            CREATE TABLE adaptive_research_state_meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            INSERT INTO adaptive_research_state_meta (key, value)
            VALUES ('schema_version', 1);
            """
        )

    with pytest.raises(
        AdaptiveResearchStateStoreMigrationError,
        match="schema definition",
    ):
        AdaptiveResearchStateStore(path)


def test_canonical_schema_accepts_harmless_formatting_and_comments(tmp_path) -> None:
    path = tmp_path / "formatted.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            create table adaptive_research_state_snapshots
            (
                run_id text not null,
                run_incarnation text not null,
                contract_name text not null check
                    (contract_name in ('query_spec', /* allowed */ 'research_state')),
                revision integer not null
                    check (typeof(revision) = 'integer' and revision >= 0),
                payload blob not null check (typeof(payload) = 'blob'),
                digest text not null,
                created_at_ns integer not null
                    check (typeof(created_at_ns) = 'integer' and created_at_ns >= 0),
                primary key (run_id, run_incarnation, contract_name, revision)
            );
            create table adaptive_research_state_meta
            (
                key text primary key,
                value integer not null
            );
            insert into adaptive_research_state_meta (key, value)
            values ('schema_version', 1);
            """
        )

    store = AdaptiveResearchStateStore(path)
    store.close()
    AdaptiveResearchStateStore(path).close()


def test_seeded_canonical_v1_migration_preserves_snapshot_bytes(tmp_path) -> None:
    path = tmp_path / "seeded-v1.db"
    state = _research_state(revision=0)
    payload = serialize_contract(state)
    digest = canonical_digest(state)
    created_at_ns = 123456789
    with sqlite3.connect(path) as connection:
        connection.execute(state_store_module._SNAPSHOT_TABLE_SQL)
        connection.execute(state_store_module._META_TABLE_SQL)
        connection.execute(
            """
            INSERT INTO adaptive_research_state_meta (key, value)
            VALUES ('schema_version', 1)
            """
        )
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
                payload,
                digest,
                created_at_ns,
            ),
        )

    store = AdaptiveResearchStateStore(path)
    loaded = store.load_latest_research_state(RUN_ID, INCARNATION)
    store.close()
    with sqlite3.connect(path) as connection:
        preserved = connection.execute(
            """
            SELECT payload, digest, created_at_ns
            FROM adaptive_research_state_snapshots
            WHERE run_id = ? AND run_incarnation = ?
              AND contract_name = 'research_state' AND revision = 0
            """,
            (RUN_ID, INCARNATION),
        ).fetchone()
        version = connection.execute(
            """
            SELECT value FROM adaptive_research_state_meta
            WHERE key = 'schema_version'
            """
        ).fetchone()[0]

    assert loaded == state
    assert preserved == (payload, digest, created_at_ns)
    assert version == 3


@pytest.mark.parametrize(
    ("version", "message"),
    [(0, "not supported"), (4, "newer")],
)
def test_unknown_schema_version_is_not_repaired(tmp_path, version, message) -> None:
    path = tmp_path / f"version-{version}.db"
    AdaptiveResearchStateStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE adaptive_research_state_meta SET value = ? WHERE key = 'schema_version'",
            (version,),
        )

    with pytest.raises(AdaptiveResearchStateStoreMigrationError, match=message):
        AdaptiveResearchStateStore(path)


def test_owned_schema_rejects_extra_trigger(tmp_path) -> None:
    path = tmp_path / "trigger.db"
    AdaptiveResearchStateStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER unexpected_snapshot_trigger
            AFTER INSERT ON adaptive_research_state_snapshots
            BEGIN
                SELECT 1;
            END
            """
        )

    with pytest.raises(
        AdaptiveResearchStateStoreMigrationError,
        match="schema definition",
    ):
        AdaptiveResearchStateStore(path)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE adaptive_research_state_unexpected (value INTEGER)",
        """
        CREATE VIEW adaptive_research_state_unexpected_view
        AS SELECT value FROM unrelated_shared
        """,
        """
        CREATE INDEX adaptive_research_state_unexpected_index
        ON unrelated_shared(value)
        """,
        """
        CREATE TRIGGER adaptive_research_state_unexpected_trigger
        AFTER INSERT ON unrelated_shared
        BEGIN
            SELECT 1;
        END
        """,
        'CREATE TABLE "ADAPTIVE_RESEARCH_STATE_UNEXPECTED" (value INTEGER)',
        """
        CREATE VIEW "Adaptive_Research_State_Unexpected_View"
        AS SELECT value FROM unrelated_shared
        """,
        """
        CREATE INDEX "ADAPTIVE_RESEARCH_STATE_UNEXPECTED_INDEX"
        ON unrelated_shared(value)
        """,
        """
        CREATE TRIGGER "Adaptive_Research_State_Unexpected_Trigger"
        AFTER INSERT ON unrelated_shared
        BEGIN
            SELECT 1;
        END
        """,
    ],
)
def test_owned_namespace_rejects_every_unexpected_object(tmp_path, statement) -> None:
    path = tmp_path / "unexpected-owned-object.db"
    AdaptiveResearchStateStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated_shared (value INTEGER)")
        connection.execute(statement)

    with pytest.raises(
        AdaptiveResearchStateStoreMigrationError,
        match="schema definition",
    ):
        AdaptiveResearchStateStore(path)


def test_owned_schema_allows_unrelated_shared_database_objects(tmp_path) -> None:
    path = tmp_path / "unrelated-objects.db"
    AdaptiveResearchStateStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE shared_records (value INTEGER);
            CREATE VIEW shared_records_view AS SELECT value FROM shared_records;
            CREATE INDEX shared_records_index ON shared_records(value);
            CREATE TRIGGER shared_records_trigger
            AFTER INSERT ON shared_records
            BEGIN
                SELECT 1;
            END;
            """
        )

    AdaptiveResearchStateStore(path).close()


@pytest.mark.parametrize("fail_execute", [False, True])
def test_reference_memory_connection_always_closes(
    monkeypatch,
    fail_execute,
) -> None:
    real_connect = sqlite3.connect
    tracked = []

    class TrackingConnection:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def execute(self, statement, parameters=()):
            if fail_execute:
                raise RuntimeError("reference schema failure")
            return self.connection.execute(statement, parameters)

        def close(self):
            self.closed = True
            self.connection.close()

    def connect(database, *args, **kwargs):
        connection = real_connect(database, *args, **kwargs)
        if database != ":memory:":
            return connection
        wrapped = TrackingConnection(connection)
        tracked.append(wrapped)
        return wrapped

    monkeypatch.setattr(state_store_module.sqlite3, "connect", connect)

    if fail_execute:
        with pytest.raises(RuntimeError, match="reference schema failure"):
            state_store_module._canonical_owned_schema_signature()
    else:
        assert state_store_module._canonical_owned_schema_signature()
    assert len(tracked) == 1
    assert tracked[0].closed is True
