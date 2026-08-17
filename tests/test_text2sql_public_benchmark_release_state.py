from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from custom_tools.text_to_sql.eval.release_state import (
    ReleaseBundleCoordinator,
    ReleasePhase,
    ReleaseProgressError,
    ReleaseProgressStore,
)
from custom_tools.text_to_sql.eval.release_leg_progress import ReleaseLegProgress


def _store(tmp_path: Path) -> ReleaseProgressStore:
    store = ReleaseProgressStore(tmp_path / "sandbox-state" / "release_progress.sqlite3")
    store.bind_bundle(
        bundle_id="bundle-1",
        release_lock_digest="sha256:lock",
        release_plan_digest="sha256:plan",
    )
    return store


def _start_leg(store: ReleaseProgressStore, case_keys: list[str]) -> None:
    store.start_leg(benchmark="bird", repeat_ordinal=1, seed=7)
    store.bind_leg_inputs(
        benchmark="bird",
        repeat_ordinal=1,
        run_manifest_sha256="sha256:run",
        case_manifest_sha256="sha256:cases",
        ordered_case_keys=case_keys,
    )


def _observation(case_key: str) -> dict[str, object]:
    return {"case_key": case_key, "observation_status": "completed"}


def _receipt(case_key: str) -> dict[str, object]:
    return {
        "case_key": case_key,
        "verification_status": "verified",
        "preexisting_history_items": 0,
    }


def _commit(store: ReleaseProgressStore, ordinal: int, case_key: str) -> None:
    store.begin_case(benchmark="bird", repeat_ordinal=1, case_key=case_key)
    store.commit_case(
        benchmark="bird",
        repeat_ordinal=1,
        ordinal=ordinal,
        case_key=case_key,
        observation=_observation(case_key),
        history_receipt=_receipt(case_key),
    )


def test_store_uses_explicit_sqlite_durability_contract(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA foreign_key_list(case_commits)").fetchall()
    with store._connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


@pytest.mark.parametrize("legacy_version", (1, 2))
def test_progress_v3_rejects_v1_and_v2_without_migration(
    tmp_path: Path,
    legacy_version: int,
) -> None:
    path = tmp_path / f"v{legacy_version}" / "release_progress.sqlite3"
    path.parent.mkdir()
    ReleaseProgressStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {legacy_version}")
        connection.commit()

    with pytest.raises(ReleaseProgressError, match="schema is unsupported"):
        ReleaseProgressStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == legacy_version


def test_bundle_identity_and_leg_inputs_are_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])

    with pytest.raises(ReleaseProgressError, match="identity"):
        store.bind_bundle(
            bundle_id="other",
            release_lock_digest="sha256:lock",
            release_plan_digest="sha256:plan",
        )
    with pytest.raises(ReleaseProgressError, match="input binding"):
        store.bind_leg_inputs(
            benchmark="bird",
            repeat_ordinal=1,
            run_manifest_sha256="sha256:different",
            case_manifest_sha256="sha256:cases",
            ordered_case_keys=["bird:1"],
        )


def test_in_flight_case_makes_release_invalid_and_cannot_rerun(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])
    store.begin_case(benchmark="bird", repeat_ordinal=1, case_key="bird:1")

    with pytest.raises(ReleaseProgressError, match="new release experiment"):
        store.authenticate_commits(benchmark="bird", repeat_ordinal=1)
    assert store.progress().phase is ReleasePhase.INVALID
    with pytest.raises(ReleaseProgressError, match="new release experiment"):
        store.begin_case(benchmark="bird", repeat_ordinal=1, case_key="bird:1")


def test_commit_case_invalidates_bundle_when_empty_history_cannot_be_proved(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])
    store.begin_case(benchmark="bird", repeat_ordinal=1, case_key="bird:1")
    receipt = _receipt("bird:1")
    receipt.update(
        verification_status="unavailable",
        preexisting_history_items=None,
    )

    with pytest.raises(ReleaseProgressError, match="history receipt is not empty"):
        store.commit_case(
            benchmark="bird",
            repeat_ordinal=1,
            ordinal=0,
            case_key="bird:1",
            observation=_observation("bird:1"),
            history_receipt=receipt,
        )

    assert store.progress().phase is ReleasePhase.INVALID
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM case_commits").fetchone()[0] == 0


def test_case_order_is_closed_and_committed_case_cannot_run_twice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1", "bird:2"])

    store.begin_case(benchmark="bird", repeat_ordinal=1, case_key="bird:2")
    with pytest.raises(ReleaseProgressError, match="canonical next"):
        store.commit_case(
            benchmark="bird",
            repeat_ordinal=1,
            ordinal=0,
            case_key="bird:2",
            observation=_observation("bird:2"),
            history_receipt=_receipt("bird:2"),
        )

    # A failed transaction leaves an admitted case. Recovery must invalidate it.
    with pytest.raises(ReleaseProgressError, match="new release experiment"):
        store.authenticate_commits(benchmark="bird", repeat_ordinal=1)


def test_tampered_committed_blob_fails_authentication(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])
    _commit(store, 0, "bird:1")

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE case_commits SET observation_json = ?",
            (b'{"case_key":"bird:tampered"}\n',),
        )
        connection.commit()

    with pytest.raises(ReleaseProgressError, match="chain"):
        store.authenticate_commits(benchmark="bird", repeat_ordinal=1)


def test_authenticate_commits_invalidates_legacy_unavailable_history_receipt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])
    _commit(store, 0, "bird:1")
    receipt = {
        "case_key": "bird:1",
        "preexisting_history_items": None,
        "verification_status": "unavailable",
    }
    receipt_bytes = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    receipt_digest = "sha256:" + hashlib.sha256(receipt_bytes).hexdigest()
    previous_chain = "sha256:" + "0" * 64
    with sqlite3.connect(store.path) as connection:
        observation_digest = connection.execute(
            "SELECT observation_sha256 FROM case_commits"
        ).fetchone()[0]
        chain_digest = "sha256:" + hashlib.sha256(
            (previous_chain + observation_digest + receipt_digest).encode("ascii")
        ).hexdigest()
        connection.execute(
            """
            UPDATE case_commits
            SET history_receipt_json = ?, history_receipt_sha256 = ?,
                previous_chain_sha256 = ?, chain_sha256 = ?
            """,
            (receipt_bytes, receipt_digest, previous_chain, chain_digest),
        )
        connection.commit()

    with pytest.raises(ReleaseProgressError, match="committed history receipt is invalid"):
        store.authenticate_commits(benchmark="bird", repeat_ordinal=1)
    assert store.progress().phase is ReleasePhase.INVALID


@pytest.mark.parametrize("column", ["ordered_case_keys_json", "observation_json"])
def test_invalid_sqlite_utf8_marks_release_invalid_in_same_transaction(
    tmp_path: Path,
    column: str,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])
    _commit(store, 0, "bird:1")
    with sqlite3.connect(store.path) as connection:
        if column == "ordered_case_keys_json":
            connection.execute(
                "UPDATE leg_progress SET ordered_case_keys_json = ?",
                (b"\xff",),
            )
        else:
            observation = b"\xff"
            observation_digest = "sha256:" + hashlib.sha256(observation).hexdigest()
            receipt_digest = connection.execute(
                "SELECT history_receipt_sha256 FROM case_commits"
            ).fetchone()[0]
            previous_chain = "sha256:" + "0" * 64
            chain_digest = "sha256:" + hashlib.sha256(
                (previous_chain + observation_digest + receipt_digest).encode("ascii")
            ).hexdigest()
            connection.execute(
                """
                UPDATE case_commits
                SET observation_json = ?, observation_sha256 = ?, chain_sha256 = ?
                """,
                (observation, observation_digest, chain_digest),
            )
        connection.commit()

    with pytest.raises(ReleaseProgressError, match="invalid"):
        store.authenticate_commits(benchmark="bird", repeat_ordinal=1)
    assert store.progress().phase is ReleasePhase.INVALID


def test_candidate_prefix_rejects_recomputed_sqlite_chain_in_same_transaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])
    _commit(store, 0, "bird:1")
    original_observations = store.observation_bytes(
        benchmark="bird", repeat_ordinal=1
    )
    candidate_digest = "sha256:candidate"
    store.pause_for_candidate(
        candidate_sha256=candidate_digest,
        prefix_case_count=1,
    )

    tampered_observation = b'{"case_key":"bird:1","observation_status":"runner_error"}\n'
    observation_digest = "sha256:" + hashlib.sha256(tampered_observation).hexdigest()
    previous_chain = "sha256:" + "0" * 64
    with sqlite3.connect(store.path) as connection:
        receipt_digest = connection.execute(
            "SELECT history_receipt_sha256 FROM case_commits"
        ).fetchone()[0]
        chain_digest = "sha256:" + hashlib.sha256(
            (previous_chain + observation_digest + receipt_digest).encode("ascii")
        ).hexdigest()
        connection.execute(
            """
            UPDATE case_commits
            SET observation_json = ?, observation_sha256 = ?,
                previous_chain_sha256 = ?, chain_sha256 = ?
            """,
            (
                tampered_observation,
                observation_digest,
                previous_chain,
                chain_digest,
            ),
        )
        connection.execute(
            "UPDATE release_progress SET prefix_chain_sha256 = ?",
            (chain_digest,),
        )
        connection.commit()

    with pytest.raises(ReleaseProgressError, match="sealed candidate"):
        store.authenticate_candidate_prefix(
            benchmark="bird",
            repeat_ordinal=1,
            candidate_sha256=candidate_digest,
            completed_case_keys=["bird:1"],
            completed_case_count=1,
            observations_sha256=(
                "sha256:" + hashlib.sha256(original_observations).hexdigest()
            ),
        )

    assert store.progress().phase is ReleasePhase.INVALID


@pytest.mark.parametrize(
    ("completed_case_keys", "completed_case_count"),
    [(["bird:other"], 1), (["bird:1"], 2)],
)
def test_candidate_prefix_requires_exact_committed_keys_and_count(
    tmp_path: Path,
    completed_case_keys: list[str],
    completed_case_count: int,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])
    _commit(store, 0, "bird:1")
    observations = store.observation_bytes(benchmark="bird", repeat_ordinal=1)

    with pytest.raises(ReleaseProgressError, match="sealed candidate"):
        store.authenticate_candidate_prefix(
            benchmark="bird",
            repeat_ordinal=1,
            candidate_sha256="sha256:candidate",
            completed_case_keys=completed_case_keys,
            completed_case_count=completed_case_count,
            observations_sha256="sha256:" + hashlib.sha256(observations).hexdigest(),
        )

    assert store.progress().phase is ReleasePhase.INVALID


def test_candidate_prefix_rejects_partially_unbound_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])
    _commit(store, 0, "bird:1")
    observations = store.observation_bytes(benchmark="bird", repeat_ordinal=1)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE release_progress SET prefix_case_count = 1")
        connection.commit()

    with pytest.raises(ReleaseProgressError, match="sealed candidate"):
        store.authenticate_candidate_prefix(
            benchmark="bird",
            repeat_ordinal=1,
            candidate_sha256="sha256:candidate",
            completed_case_keys=["bird:1"],
            completed_case_count=1,
            observations_sha256="sha256:" + hashlib.sha256(observations).hexdigest(),
        )
    assert store.progress().phase is ReleasePhase.INVALID


def test_observations_view_repairs_exact_prefix_and_rejects_extra_bytes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1", "bird:2"])
    _commit(store, 0, "bird:1")
    _commit(store, 1, "bird:2")
    expected = store.observation_bytes(benchmark="bird", repeat_ordinal=1)
    path = tmp_path / "public" / "observations.jsonl"
    path.parent.mkdir()
    path.write_bytes(expected[: len(expected) // 2])

    store.materialize_observations(path, benchmark="bird", repeat_ordinal=1)
    assert path.read_bytes() == expected

    path.write_bytes(expected + b"extra")
    with pytest.raises(ReleaseProgressError, match="exact committed byte prefix"):
        store.materialize_observations(path, benchmark="bird", repeat_ordinal=1)


def test_history_snapshot_accepts_only_committed_receipt_prefixes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])
    _commit(store, 0, "bird:1")
    prefixes = store.history_view_receipt_prefixes(
        benchmark="bird", repeat_ordinal=1
    )
    views = [
        (json.dumps({"receipts": value}, sort_keys=True) + "\n").encode()
        for value in prefixes
    ]
    path = tmp_path / "public" / "empty_history_evidence.json"
    path.parent.mkdir()
    path.write_bytes(views[0])

    store.materialize_snapshot(path, committed_views=views, label="history view")
    assert path.read_bytes() == views[-1]

    path.write_text('{"receipts":["extra"]}\n', encoding="utf-8")
    with pytest.raises(ReleaseProgressError, match="authenticated committed view"):
        store.materialize_snapshot(path, committed_views=views, label="history view")


def test_continue_preserves_committed_prefix_and_accepts_first_suffix_case(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1", "bird:2"])
    _commit(store, 0, "bird:1")
    store.pause_for_candidate(candidate_sha256="sha256:candidate", prefix_case_count=1)
    store.register_governance_event(
        {
            "event_kind": "mid_repeat",
            "benchmark": "bird",
            "repeat_ordinal": 1,
            "completed_case_count": 1,
            "candidate_path": "governance/mid_repeat/000001/early_stop_candidate.json",
            "candidate_sha256": "sha256:candidate",
            "decision_path": "governance/mid_repeat/000001/repair_decision.json",
            "decision_sha256": "sha256:decision",
            "result_path": "governance/mid_repeat/000001/early_stop.json",
            "result_sha256": "sha256:result",
        }
    )
    store.continue_active_leg(
        candidate_sha256="sha256:candidate", decision_sha256="sha256:decision"
    )

    _commit(store, 1, "bird:2")

    assert store.committed_case_keys(benchmark="bird", repeat_ordinal=1) == [
        "bird:1",
        "bird:2",
    ]


def test_continue_suffix_commit_crash_resume_authenticates_historical_candidate_prefix(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1", "bird:2"])
    _commit(store, 0, "bird:1")
    prefix_bytes = store.observation_bytes(benchmark="bird", repeat_ordinal=1)
    candidate_digest = "sha256:candidate"
    store.pause_for_candidate(
        candidate_sha256=candidate_digest,
        prefix_case_count=1,
    )
    store.register_governance_event(
        {
            "event_kind": "mid_repeat",
            "benchmark": "bird",
            "repeat_ordinal": 1,
            "completed_case_count": 1,
            "candidate_path": "governance/mid_repeat/000001/early_stop_candidate.json",
            "candidate_sha256": candidate_digest,
            "decision_path": "governance/mid_repeat/000001/repair_decision.json",
            "decision_sha256": "sha256:decision",
            "result_path": "governance/mid_repeat/000001/early_stop.json",
            "result_sha256": "sha256:result",
        }
    )
    store.continue_active_leg(
        candidate_sha256=candidate_digest,
        decision_sha256="sha256:decision",
    )
    _commit(store, 1, "bird:2")

    resumed = ReleaseProgressStore(store.path)
    resumed.authenticate_candidate_prefix(
        benchmark="bird",
        repeat_ordinal=1,
        candidate_sha256=candidate_digest,
        completed_case_keys=["bird:1"],
        completed_case_count=1,
        observations_sha256=(
            "sha256:" + hashlib.sha256(prefix_bytes).hexdigest()
        ),
    )

    assert resumed.progress().phase is ReleasePhase.CONTINUING_ACTIVE_LEG
    assert resumed.committed_case_keys(benchmark="bird", repeat_ordinal=1) == [
        "bird:1",
        "bird:2",
    ]


@pytest.mark.parametrize("continuing", [False, True])
def test_resumed_failure_count_uses_authenticated_prefix_for_both_phases(
    tmp_path: Path,
    continuing: bool,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1", "bird:2"])
    store.begin_case(benchmark="bird", repeat_ordinal=1, case_key="bird:1")
    store.commit_case(
        benchmark="bird",
        repeat_ordinal=1,
        ordinal=0,
        case_key="bird:1",
        observation={
            "case_key": "bird:1",
            "observation_status": "runner_error",
        },
        history_receipt=_receipt("bird:1"),
    )
    if continuing:
        store.pause_for_candidate(
            candidate_sha256="sha256:candidate",
            prefix_case_count=1,
        )
        store.register_governance_event(
            {
                "event_kind": "mid_repeat",
                "benchmark": "bird",
                "repeat_ordinal": 1,
                "completed_case_count": 1,
                "candidate_path": "governance/mid_repeat/000001/early_stop_candidate.json",
                "candidate_sha256": "sha256:candidate",
                "decision_path": "governance/mid_repeat/000001/repair_decision.json",
                "decision_sha256": "sha256:decision",
                "result_path": "governance/mid_repeat/000001/early_stop.json",
                "result_sha256": "sha256:result",
            }
        )
        store.continue_active_leg(
            candidate_sha256="sha256:candidate",
            decision_sha256="sha256:decision",
        )
    progress = ReleaseLegProgress(
        store=store,
        benchmark="bird",
        repeat_ordinal=1,
        seed=7,
        initial_phase=store.progress().phase,
    )

    assert progress.authenticated_failure_count() == 1


def test_governance_inventory_keeps_both_continue_events_until_final_handshake(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1"])
    _commit(store, 0, "bird:1")
    mid_event = {
        "event_kind": "mid_repeat",
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "completed_case_count": 1,
        "candidate_path": "governance/mid_repeat/000001/early_stop_candidate.json",
        "candidate_sha256": "sha256:mid-candidate",
        "decision_path": "governance/mid_repeat/000001/repair_decision.json",
        "decision_sha256": "sha256:mid-decision",
        "result_path": "governance/mid_repeat/000001/early_stop.json",
        "result_sha256": "sha256:mid-result",
    }
    store.pause_for_candidate(
        candidate_sha256=mid_event["candidate_sha256"],
        prefix_case_count=1,
    )
    store.register_governance_event(mid_event)
    store.continue_active_leg(
        candidate_sha256=mid_event["candidate_sha256"],
        decision_sha256=mid_event["decision_sha256"],
    )
    store.defer_leg_for_post_repeat(return_code=1)
    assert store.completed_legs() == []
    store.transition(
        expected=[ReleasePhase.AWAITING_POST_REPEAT_EVALUATION],
        target=ReleasePhase.AWAITING_POST_REPEAT_REPAIR_DECISION,
        candidate_sha256="sha256:post-candidate",
    )
    post_event = {
        "event_kind": "post_repeat",
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "completed_case_count": 1,
        "candidate_path": "governance/post_repeat/000001/early_stop_candidate.json",
        "candidate_sha256": "sha256:post-candidate",
        "decision_path": "governance/post_repeat/000001/repair_decision.json",
        "decision_sha256": "sha256:post-decision",
        "result_path": "governance/post_repeat/000001/early_stop.json",
        "result_sha256": "sha256:post-result",
    }
    store.register_governance_event(post_event)

    assert store.governance_events(benchmark="bird", repeat_ordinal=1) == [
        mid_event,
        post_event,
    ]
    store.complete_post_repeat_evaluation(
        artifact_handshake_sha256="sha256:final-handshake"
    )
    assert store.completed_legs()[0].artifact_handshake_sha256 == (
        "sha256:final-handshake"
    )


def test_multiple_mid_repeat_governance_events_are_immutable_and_ordered_by_completed_case_count(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start_leg(store, ["bird:1", "bird:2"])
    first_event = {
        "event_kind": "mid_repeat",
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "completed_case_count": 1,
        "candidate_path": "governance/mid_repeat/000001/early_stop_candidate.json",
        "candidate_sha256": "sha256:first-candidate",
        "decision_path": "governance/mid_repeat/000001/repair_decision.json",
        "decision_sha256": "sha256:first-decision",
        "result_path": "governance/mid_repeat/000001/early_stop.json",
        "result_sha256": "sha256:first-result",
    }
    _commit(store, 0, "bird:1")
    store.pause_for_candidate(
        candidate_sha256=first_event["candidate_sha256"],
        prefix_case_count=1,
    )
    store.register_governance_event(first_event)
    store.continue_active_leg(
        candidate_sha256=first_event["candidate_sha256"],
        decision_sha256=first_event["decision_sha256"],
    )

    _commit(store, 1, "bird:2")
    second_event = {
        "event_kind": "mid_repeat",
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "completed_case_count": 2,
        "candidate_path": "governance/mid_repeat/000002/early_stop_candidate.json",
        "candidate_sha256": "sha256:second-candidate",
        "decision_path": "governance/mid_repeat/000002/repair_decision.json",
        "decision_sha256": "sha256:second-decision",
        "result_path": "governance/mid_repeat/000002/early_stop.json",
        "result_sha256": "sha256:second-result",
    }
    store.pause_for_candidate(
        candidate_sha256=second_event["candidate_sha256"],
        prefix_case_count=2,
    )
    store.register_governance_event(second_event)

    assert store.governance_events(benchmark="bird", repeat_ordinal=1) == [
        first_event,
        second_event,
    ]


def test_coordinator_repairs_only_a_committed_completed_leg_prefix(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    plan = [
        {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7},
        {"benchmark": "bird", "repeat_ordinal": 2, "seed": 8},
    ]
    _start_leg(store, [])
    store.complete_leg(
        return_code=0,
        artifact_handshake_sha256="sha256:handshake",
    )
    coordinator = ReleaseBundleCoordinator(
        store, bundle_id="bundle-1", release_plan=plan
    )
    stale = {
        "record_kind": "text2sql_public_benchmark_bundle_state",
        "bundle_id": "bundle-1",
        "release_plan": plan,
        "completed_legs": [],
    }

    repaired = coordinator.reconcile_public_state(stale)

    assert repaired["completed_legs"] == [
        {
            "benchmark": "bird",
            "repeat_ordinal": 1,
            "seed": 7,
            "return_code": 0,
            "artifact_handshake_sha256": "sha256:handshake",
        }
    ]
    with pytest.raises(ReleaseProgressError, match="committed prefix"):
        coordinator.reconcile_public_state(
            {**stale, "completed_legs": [{"benchmark": "tampered"}]}
        )
    with pytest.raises(ReleaseProgressError, match="cannot be run again"):
        store.start_leg(benchmark="bird", repeat_ordinal=1, seed=7)


def test_terminal_inventory_is_committed_with_terminal_phase(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.seal_terminal_state(
        expected=ReleasePhase.BETWEEN_LEGS,
        target=ReleasePhase.COMPLETE,
        artifact_sha256={"bundle_artifact_handshake.json": "sha256:terminal"},
    )

    assert store.progress().phase is ReleasePhase.COMPLETE
    assert store.terminal_artifact_digests() == {
        "bundle_artifact_handshake.json": "sha256:terminal"
    }
