from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import text2sql_benchmark_reporting as reporting
from custom_tools.text_to_sql.eval import public_benchmark_release as release
from custom_tools.text_to_sql.eval.release_bundle_execution import ReleaseBundleExecution
from custom_tools.text_to_sql.eval import release_diagnostics as diagnostics
from custom_tools.text_to_sql.eval import release_coordinator as coordinator
from custom_tools.text_to_sql.eval import public_benchmark_artifacts as artifacts
from custom_tools.text_to_sql.eval import official_evaluator_bridge as evaluator_bridge
from custom_tools.text_to_sql.eval.release_state import ReleaseProgressStore
from custom_tools.text_to_sql.eval.release_diagnostics import (
    DiagnosticArtifactError,
    FAILURE_CLASS_ORDER,
    ordered_failure_rows,
)
from scripts.summarize_text2sql_public_benchmark import summarize


def _receipt(case_keys: list[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_kind": "text2sql_official_evaluator_receipt",
        "evaluator_identity": {
            "origin": "https://example.test/evaluator",
            "revision": "revision",
            "entrypoint": "evaluate.py",
            "sha256": "a" * 64,
        },
        "evaluator_input_sha256": "sha256:input",
        "score_sha256": "sha256:score",
        "case_manifest_sha256": "sha256:cases",
        "run_manifest_sha256": "sha256:run",
        "case_keys": case_keys,
    }


def _policy() -> dict[str, object]:
    return {
        "schema_version": 2,
        "record_kind": "text2sql_public_benchmark_early_stop_policy",
        "block_size": 1,
        "min_completed": 2,
        "min_signature_cases": 2,
    }


def _semantic_receipt(
    *,
    requirement: str = "required_filter",
    state_sha256: str = "sha256:" + "a" * 64,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_kind": "text2sql_adaptive_early_stop_evidence",
        "terminal_source": "research",
        "root_mechanism": "ambiguous",
        "error_class": "ambiguous_requirement",
        "violated_typed_requirement": requirement,
        "pipeline_component": "adaptive_schema_research",
        "state_sha256": state_sha256,
    }


def _observation(
    index: int,
    *,
    database_id: str = "db",
    requirement: str = "required_filter",
) -> dict[str, object]:
    receipt = _semantic_receipt(requirement=requirement)
    return {
        "case_key": f"bird:{index}",
        "database_id": database_id,
        "observation_status": "completed",
        "outcome": {"status": "abstained", "reason_code": "RESEARCH_AMBIGUOUS"},
        "runtime_evidence": {
            "schema_version": 2,
            "semantic_evidence": {
                "availability": "verified",
                "error_class": receipt["error_class"],
                "violated_typed_requirement": receipt[
                    "violated_typed_requirement"
                ],
                "pipeline_component": receipt["pipeline_component"],
            },
            "semantic_evidence_authority": receipt,
        },
    }


def _technical_observation(
    index: int,
    *,
    database_id: str = "db",
    reason_code: str = "RESEARCH_PROTOCOL_FAILURE",
    status: str = "failed",
    error: str = "research outcome invalid",
) -> dict[str, object]:
    return {
        "case_key": f"bird:{index}",
        "database_id": database_id,
        "observation_status": "completed",
        "outcome": {
            "status": status,
            "reason_code": reason_code,
            "error": error,
        },
        "runtime_evidence": {
            "schema_version": 2,
            "terminal": {
                "availability": "available",
                "reason_code": reason_code,
            },
        },
    }


def test_stagnation_pairs_are_grouped_independently_and_once_per_case() -> None:
    def observation(case_key: str) -> dict[str, object]:
        return {
            "case_key": case_key,
            "database_id": "db",
            "observation_status": "completed",
            "outcome": {
                "status": "abstained",
                "reason_code": "RESEARCH_STAGNATED",
            },
            "runtime_evidence": {
                "schema_version": 2,
                "terminal": {
                    "availability": "available",
                    "reason_code": "RESEARCH_STAGNATED",
                },
                "stagnation_classification": {
                    "availability": "verified",
                    "rejection_signatures": [
                        ["invalid_stop", "INVALID_STOP"],
                        ["research_query_admission", "research_query_limit"],
                    ],
                },
            },
        }

    candidate = reporting.find_early_stop_candidate(
        [observation("bird:1"), observation("bird:2")],
        reporting.parse_early_stop_policy(_policy()),
    )

    assert candidate is not None
    assert candidate["signature"] == {
        "error_class": "research_stagnation",
        "rejection_path": "invalid_stop",
        "rejection_code": "INVALID_STOP",
    }
    assert candidate["signature_case_count"] == 2


@pytest.mark.parametrize(
    "mutation",
    ("schema_version", "terminal_availability", "terminal_reason"),
)
def test_research_stagnation_pair_requires_closed_terminal_evidence(
    mutation: str,
) -> None:
    def observation(index: int) -> dict[str, object]:
        return {
            "case_key": f"case:{index}",
            "database_id": f"db:{index}",
            "observation_status": "completed",
            "outcome": {
                "status": "abstained",
                "reason_code": "RESEARCH_STAGNATED",
            },
            "runtime_evidence": {
                "schema_version": 2,
                "terminal": {
                    "availability": "available",
                    "reason_code": "RESEARCH_STAGNATED",
                },
                "stagnation_classification": {
                    "availability": "verified",
                    "rejection_signatures": [
                        ["contract_decode", "INVALID_DECISION"],
                    ],
                },
            },
        }

    rows = [observation(1), observation(2)]
    for row in rows:
        runtime = row["runtime_evidence"]
        assert isinstance(runtime, dict)
        terminal = runtime["terminal"]
        assert isinstance(terminal, dict)
        if mutation == "schema_version":
            runtime["schema_version"] = 1
        elif mutation == "terminal_availability":
            terminal["availability"] = "unavailable"
        else:
            terminal["reason_code"] = "OTHER_REASON"

    assert all(reporting.normalized_signatures(row) == () for row in rows)
    assert (
        reporting.find_early_stop_candidate(
            rows,
            reporting.parse_early_stop_policy(_policy()),
        )
        is None
    )


def _paused_candidate() -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_kind": "text2sql_public_benchmark_early_stop_candidate",
        "completed_case_count": 1,
        "signature": {"error_class": "binding"},
        "signature_case_count": 1,
        "signature_share": "1/1",
        "database_count": 1,
        "database_ids": ["db"],
        "completed_case_keys": ["bird:0"],
        "not_started_case_keys": [],
        "bundle_id": "bundle-1",
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "policy_sha256": "sha256:policy",
        "release_identity": {},
        "configuration_digest": "sha256:configuration",
        "source_snapshot_digest": "sha256:snapshot",
        "manifest_sha256": "sha256:manifest",
        "case_manifest_sha256": "sha256:cases",
        "observations_sha256": "sha256:observations",
        "empty_history_evidence_sha256": "sha256:history",
        "ordered_case_keys": ["bird:0"],
    }


def _artifact_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_paused_leg_inputs(
    tmp_path: Path,
    *,
    manifest_keys: list[object],
    ordered_keys: list[object],
    completed_keys: list[object],
    not_started_keys: list[object],
) -> tuple[Path, dict[str, object], dict[str, object]]:
    leg_dir = tmp_path / "runs" / "bird" / "r1"
    leg_dir.mkdir(parents=True)
    (leg_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
    (leg_dir / "case_manifest.json").write_text(
        json.dumps({"cases": [{"case_key": key} for key in manifest_keys]}) + "\n",
        encoding="utf-8",
    )
    (leg_dir / "observations.jsonl").write_text(
        "".join(
            json.dumps({"case_key": key, "preexisting_history_items": 0}) + "\n"
            for key in completed_keys
        ),
        encoding="utf-8",
    )
    (leg_dir / "empty_history_evidence.json").write_text(
        json.dumps(
            {
                "receipts": [
                    {
                        "case_key": key,
                        "verification_status": "verified",
                        "preexisting_history_items": 0,
                    }
                    for key in completed_keys
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    identity: dict[str, object] = {
        "source_snapshot_digest": "sha256:snapshot",
    }
    candidate: dict[str, object] = {
        "configuration_digest": "sha256:configuration",
        "source_snapshot_digest": "sha256:snapshot",
        "release_identity": dict(identity),
        "manifest_sha256": _artifact_sha256(leg_dir / "manifest.json"),
        "case_manifest_sha256": _artifact_sha256(leg_dir / "case_manifest.json"),
        "observations_sha256": _artifact_sha256(leg_dir / "observations.jsonl"),
        "empty_history_evidence_sha256": _artifact_sha256(
            leg_dir / "empty_history_evidence.json"
        ),
        "ordered_case_keys": ordered_keys,
        "completed_case_keys": completed_keys,
        "completed_case_count": len(completed_keys),
        "not_started_case_keys": not_started_keys,
    }
    return leg_dir, candidate, identity


def _write_post_repeat_evaluation(leg_dir: Path) -> dict[str, object]:
    leg_dir.mkdir(parents=True)
    identity = _receipt(["bird:0"])["evaluator_identity"]
    observation = {
        "ordinal": 0,
        "case_key": "bird:0",
        "case_id": "0",
        "database_id": "db",
        "difficulty": "simple",
        "run_id": "run-0",
        "elapsed_seconds": 1.0,
        "observation_status": "completed",
        "runner_error": None,
        "outcome": {
            "status": "succeeded",
            "reason_code": "OK",
            "generated": True,
            "approved": True,
            "executed": True,
            "sql": "SELECT 1",
        },
    }
    (leg_dir / "observations.jsonl").write_text(
        json.dumps(observation) + "\n", encoding="utf-8"
    )
    (leg_dir / "manifest.json").write_text(
        json.dumps({"evaluator_identity": identity}) + "\n", encoding="utf-8"
    )
    (leg_dir / "case_manifest.json").write_text(
        json.dumps({"cases": [{"case_key": "bird:0"}]}) + "\n",
        encoding="utf-8",
    )
    (leg_dir / "bird_predictions.json").write_text(
        '{"0":"SELECT 1"}\n', encoding="utf-8"
    )
    (leg_dir / "official_scores.json").write_text(
        '[{"case_key":"bird:0","score":1}]\n', encoding="utf-8"
    )
    receipt = {
        "schema_version": 1,
        "record_kind": "text2sql_official_evaluator_receipt",
        "evaluator_identity": identity,
        "evaluator_input_sha256": _artifact_sha256(
            leg_dir / "bird_predictions.json"
        ),
        "score_sha256": _artifact_sha256(leg_dir / "official_scores.json"),
        "case_manifest_sha256": _artifact_sha256(leg_dir / "case_manifest.json"),
        "run_manifest_sha256": _artifact_sha256(leg_dir / "manifest.json"),
        "case_keys": ["bird:0"],
    }
    (leg_dir / "evaluator_receipt.json").write_text(
        json.dumps(receipt) + "\n", encoding="utf-8"
    )
    diagnostics_rows, summary = summarize(
        [observation], {"bird:0": {"score": 1}}, {}, receipt
    )
    (leg_dir / "diagnostics.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in diagnostics_rows),
        encoding="utf-8",
    )
    (leg_dir / "summary.json").write_text(
        json.dumps(summary) + "\n", encoding="utf-8"
    )
    evaluation = {
        "schema_version": 1,
        "record_kind": "text2sql_public_benchmark_evaluation_manifest",
        "evaluator_input_sha256": receipt["evaluator_input_sha256"],
        "evaluator_input_path": "bird_predictions.json",
        "score_sha256": receipt["score_sha256"],
        "score_path": "official_scores.json",
        "evaluator_receipt_sha256": _artifact_sha256(
            leg_dir / "evaluator_receipt.json"
        ),
        "run_manifest_sha256": receipt["run_manifest_sha256"],
        "case_manifest_sha256": receipt["case_manifest_sha256"],
        "diagnostics_sha256": _artifact_sha256(leg_dir / "diagnostics.jsonl"),
        "summary_sha256": _artifact_sha256(leg_dir / "summary.json"),
        "case_keys": ["bird:0"],
    }
    (leg_dir / "evaluation_manifest.json").write_text(
        json.dumps(evaluation) + "\n", encoding="utf-8"
    )
    return dict(identity)


def _post_repeat_candidate(leg_dir: Path) -> dict[str, object]:
    evaluation = json.loads(
        (leg_dir / "evaluation_manifest.json").read_text(encoding="utf-8")
    )
    return {
        "schema_version": 1,
        "record_kind": "text2sql_public_benchmark_post_repeat_candidate",
        "completed_case_count": 1,
        "signature": {"error_class": "binding"},
        "signature_case_count": 1,
        "signature_share": "1/1",
        "database_count": 1,
        "database_ids": ["db"],
        "completed_case_keys": ["bird:0"],
        "bundle_id": "bundle-1",
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "evaluation_manifest_sha256": _artifact_sha256(
            leg_dir / "evaluation_manifest.json"
        ),
        "evaluator_input_sha256": evaluation["evaluator_input_sha256"],
        "score_sha256": evaluation["score_sha256"],
        "evaluator_receipt_sha256": _artifact_sha256(
            leg_dir / "evaluator_receipt.json"
        ),
        "diagnostics_sha256": _artifact_sha256(leg_dir / "diagnostics.jsonl"),
        "summary_sha256": _artifact_sha256(leg_dir / "summary.json"),
        "case_manifest_sha256": _artifact_sha256(leg_dir / "case_manifest.json"),
        "manifest_sha256": _artifact_sha256(leg_dir / "manifest.json"),
        "observations_sha256": _artifact_sha256(leg_dir / "observations.jsonl"),
    }


def _replace_post_repeat_diagnostics(leg_dir: Path) -> None:
    diagnostics_path = leg_dir / "diagnostics.jsonl"
    diagnostic = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    diagnostic["failure_class"] = "bad_sql"
    diagnostics_path.write_text(json.dumps(diagnostic) + "\n", encoding="utf-8")
    summary_path = leg_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["failure_classes"] = {"bad_sql": 1}
    summary["latency_seconds_by_failure_class"] = {
        "bad_sql": summary["latency_seconds_by_failure_class"]["correct"]
    }
    summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")
    evaluation_path = leg_dir / "evaluation_manifest.json"
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation["diagnostics_sha256"] = _artifact_sha256(diagnostics_path)
    evaluation["summary_sha256"] = _artifact_sha256(summary_path)
    evaluation_path.write_text(json.dumps(evaluation) + "\n", encoding="utf-8")


def _post_repeat_candidate_base() -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_kind": "text2sql_public_benchmark_early_stop_candidate",
        "completed_case_count": 1,
        "signature": {"error_class": "binding"},
        "signature_case_count": 1,
        "signature_share": "1/1",
        "database_count": 1,
        "database_ids": ["db"],
        "completed_case_keys": ["bird:0"],
    }


def _mutate_post_repeat_candidate(
    candidate: dict[str, object], mutation: str
) -> None:
    if mutation == "wrong_kind":
        candidate["record_kind"] = "wrong"
    elif mutation == "wrong_benchmark":
        candidate["benchmark"] = "spider"
    elif mutation == "wrong_bundle":
        candidate["bundle_id"] = "other-bundle"
    else:
        candidate["unexpected"] = True


def test_policy_contains_only_thresholds_without_predeclared_failures() -> None:
    policy = reporting.parse_early_stop_policy(_policy())

    assert (
        policy.block_size,
        policy.min_completed,
        policy.min_signature_cases,
    ) == (1, 2, 2)
    for field in ("candidate", "allowed_signatures", "root_hypothesis"):
        with pytest.raises(ValueError, match="unknown"):
            reporting.parse_early_stop_policy({**_policy(), field: {}})


def test_canonical_early_stop_policy_matches_documented_thresholds() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "config/text_to_sql/public_benchmark_early_stop_policy.json"
    )

    assert json.loads(path.read_text(encoding="utf-8")) == _policy()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("block_size", 2),
        ("min_completed", 3),
        ("min_signature_cases", 3),
    ],
)
def test_policy_rejects_thresholds_that_are_not_canonical(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="canonical"):
        reporting.parse_early_stop_policy({**_policy(), field: value})


def test_second_identical_semantic_failure_stops_on_one_database() -> None:
    policy = reporting.parse_early_stop_policy(_policy())
    observations = [
        _observation(0, database_id="db"),
        _observation(1, database_id="db", requirement="required_dimension"),
        _observation(2, database_id="db"),
    ]

    assert reporting.find_early_stop_candidate(observations[:2], policy) is None
    candidate = reporting.find_early_stop_candidate(observations, policy)

    assert candidate is not None
    assert candidate["signature"] == {
        "error_class": "ambiguous_requirement",
        "violated_typed_requirement": "required_filter",
        "pipeline_component": "adaptive_schema_research",
    }
    assert candidate["signature_case_count"] == 2
    assert candidate["database_count"] == 1


def test_signature_ignores_case_database_and_exception_text() -> None:
    policy = reporting.parse_early_stop_policy(_policy())
    rows = [_observation(index, database_id=f"db-{index % 3}") for index in range(20)]
    for index, row in enumerate(rows):
        row["case_key"] = f"question {index} SELECT {index}"
        row["runner_error"] = f"random exception text {index}"

    candidate = reporting.find_early_stop_candidate(rows, policy)

    assert candidate is not None
    assert candidate["signature_case_count"] == 20


def test_same_terminal_reason_does_not_merge_distinct_typed_requirements() -> None:
    policy = reporting.parse_early_stop_policy(_policy())
    requirements = (
        "required_filter",
        "required_formula",
        "required_limit",
        "required_time",
    )
    rows = [
        _observation(
            index,
            database_id=f"db-{index % 3}",
            requirement=requirements[index % len(requirements)],
        )
        for index in range(len(requirements))
    ]

    assert reporting.find_early_stop_candidate(rows, policy) is None


def test_failed_technical_terminals_without_safe_subcause_do_not_stop() -> None:
    policy = reporting.parse_early_stop_policy(_policy())
    observations = [
        _technical_observation(
            0,
            database_id="first-db",
            error="first unrelated exception text",
        ),
        _technical_observation(
            1,
            database_id="second-db",
            error="second unrelated exception text",
        ),
    ]

    candidate = reporting.find_early_stop_candidate(observations, policy)

    assert candidate is None


def test_failed_technical_terminal_without_safe_subcause_has_no_signature() -> None:
    assert reporting.normalized_signature(_technical_observation(0)) is None


def test_two_same_research_budget_exhaustions_stop() -> None:
    policy = reporting.parse_early_stop_policy(_policy())
    observations = [
        _technical_observation(
            index,
            database_id=f"db-{index}",
            reason_code="RESEARCH_BUDGET_EXHAUSTED",
            status="abstained",
            error=f"untrusted text {index}",
        )
        for index in range(2)
    ]

    candidate = reporting.find_early_stop_candidate(observations, policy)

    assert candidate is not None
    assert candidate["signature"] == {
        "error_class": "technical_terminal",
        "terminal_reason_code": "RESEARCH_BUDGET_EXHAUSTED",
    }
    assert candidate["signature_case_count"] == 2


@pytest.mark.parametrize(
    "mutation",
    (
        "observation_status",
        "outcome_status",
        "schema_version",
        "terminal_availability",
        "terminal_reason",
    ),
)
def test_research_budget_exhaustion_requires_closed_terminal_evidence(
    mutation: str,
) -> None:
    observation = _technical_observation(
        0,
        reason_code="RESEARCH_BUDGET_EXHAUSTED",
        status="abstained",
    )
    runtime = observation["runtime_evidence"]
    assert isinstance(runtime, dict)
    terminal = runtime["terminal"]
    assert isinstance(terminal, dict)
    outcome = observation["outcome"]
    assert isinstance(outcome, dict)
    if mutation == "observation_status":
        observation["observation_status"] = "running"
    elif mutation == "outcome_status":
        outcome["status"] = "failed"
    elif mutation == "schema_version":
        runtime["schema_version"] = 1
    elif mutation == "terminal_availability":
        terminal["availability"] = "unavailable"
    else:
        terminal["reason_code"] = "RESEARCH_STAGNATED"

    assert reporting.normalized_signatures(observation) == ()


def test_two_same_authoritative_research_stagnations_stop() -> None:
    policy = reporting.parse_early_stop_policy(_policy())
    observations = [
        _technical_observation(
            index,
            database_id=f"db-{index}",
            reason_code="RESEARCH_STAGNATED",
            status="abstained",
        )
        for index in range(2)
    ]

    candidate = reporting.find_early_stop_candidate(observations, policy)

    assert candidate is None


@pytest.mark.parametrize("reason_code", ["RESEARCH_AMBIGUOUS", "RESEARCH_UNSUPPORTED"])
def test_semantic_abstention_is_not_a_technical_terminal(reason_code: str) -> None:
    observation = _technical_observation(
        0,
        reason_code=reason_code,
        status="abstained",
    )

    assert reporting.normalized_signature(observation) is None


def test_technical_terminal_signature_does_not_merge_reason_codes() -> None:
    policy = reporting.parse_early_stop_policy(_policy())
    observations = [
        _technical_observation(0, reason_code="RESEARCH_PROTOCOL_FAILURE"),
        _technical_observation(1, reason_code="RESEARCH_TOOL_FAILURE"),
    ]

    assert reporting.find_early_stop_candidate(observations, policy) is None


@pytest.mark.parametrize(
    "mutation", ("missing", "mismatched", "non_failed", "runner_error")
)
def test_technical_terminal_signature_requires_matching_failed_terminal_evidence(
    mutation: str,
) -> None:
    observation = _technical_observation(0)
    if mutation == "missing":
        runtime = observation["runtime_evidence"]
        assert isinstance(runtime, dict)
        runtime.pop("terminal")
    elif mutation == "mismatched":
        runtime = observation["runtime_evidence"]
        assert isinstance(runtime, dict)
        terminal = runtime["terminal"]
        assert isinstance(terminal, dict)
        terminal["reason_code"] = "RESEARCH_TOOL_FAILURE"
    elif mutation == "non_failed":
        outcome = observation["outcome"]
        assert isinstance(outcome, dict)
        outcome["status"] = "abstained"
    else:
        observation.pop("outcome")
        observation["runner_error"] = "transport failure"

    assert reporting.normalized_signature(observation) is None


def test_signature_rejects_terminal_reason_without_typed_authority() -> None:
    observation = _observation(0)
    runtime = observation["runtime_evidence"]
    assert isinstance(runtime, dict)
    runtime.pop("semantic_evidence_authority")

    assert reporting.normalized_signature(observation) is None


@pytest.mark.parametrize("unknown_requirement", ["required_unknown", []])
def test_signature_rejects_unknown_typed_authority_values(
    unknown_requirement: object,
) -> None:
    observation = _observation(0)
    runtime = observation["runtime_evidence"]
    assert isinstance(runtime, dict)
    semantic = runtime["semantic_evidence"]
    authority = runtime["semantic_evidence_authority"]
    assert isinstance(semantic, dict)
    assert isinstance(authority, dict)
    semantic["violated_typed_requirement"] = unknown_requirement
    authority["violated_typed_requirement"] = unknown_requirement

    assert reporting.normalized_signature(observation) is None


def test_decision_must_be_closed_and_bound_to_immutable_candidate(
    tmp_path: Path,
) -> None:
    candidate = {"record_kind": "text2sql_public_benchmark_early_stop_candidate"}
    candidate_path = tmp_path / "early_stop_candidate.json"
    reporting.write_json_new(candidate_path, candidate)
    candidate_digest = reporting.sha256_file(candidate_path)
    decision = {
        "schema_version": 1,
        "record_kind": "text2sql_public_benchmark_repair_decision",
        "candidate_sha256": f"sha256:{candidate_digest}",
        "decision": "CONTINUE",
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-08-03T00:00:00+00:00",
        "root_hypothesis": "General timeout accounting defect.",
        "red_test_plan": "Add a synthetic deadline test.",
        "predicted_improvement": "Reduce timeout failures.",
        "safety_guarantees": ["no unsafe SQL"],
    }

    parsed = reporting.parse_repair_decision(decision, candidate_sha256=candidate_digest)

    assert parsed["decision"] == "CONTINUE"
    with pytest.raises(ValueError, match="candidate"):
        reporting.parse_repair_decision(
            {**decision, "candidate_sha256": "sha256:" + "0" * 64},
            candidate_sha256=candidate_digest,
        )


def test_paused_candidate_digest_must_match_state(tmp_path: Path) -> None:
    candidate = {"record_kind": "text2sql_public_benchmark_early_stop_candidate"}
    path = tmp_path / "early_stop_candidate.json"
    reporting.write_json_new(path, candidate)
    with pytest.raises(release.SandboxError, match="candidate"):
        release.validate_paused_candidate(path, {"candidate_sha256": "sha256:" + "0" * 64})


def test_paused_candidate_must_already_be_sealed_and_is_never_resealed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "early_stop_candidate.json"
    path.write_text(json.dumps(_paused_candidate()) + "\n", encoding="utf-8")
    digest = "sha256:" + release.sha256_file(path)
    path.chmod(0o644)

    with pytest.raises(release.SandboxError, match="sealed"):
        release.validate_paused_candidate(path, {"candidate_sha256": digest})

    assert path.stat().st_mode & 0o777 == 0o644


def test_paused_leg_accepts_shuffled_execution_order_as_exact_partition(
    tmp_path: Path,
) -> None:
    leg_dir, candidate, identity = _write_paused_leg_inputs(
        tmp_path,
        manifest_keys=["bird:0", "bird:1", "bird:2"],
        ordered_keys=["bird:2", "bird:0", "bird:1"],
        completed_keys=["bird:2", "bird:0"],
        not_started_keys=["bird:1"],
    )

    release.validate_paused_leg_inputs(
        leg_dir,
        candidate,
        identity=identity,
        configuration_digest="sha256:configuration",
    )


@pytest.mark.parametrize(
    ("receipt_preexisting", "observation_preexisting", "message"),
    (
        (None, 0, "paused empty-history receipt is invalid"),
        (0, None, "paused observations do not prove empty history"),
    ),
)
def test_paused_leg_rejects_unavailable_empty_history_proof(
    tmp_path: Path,
    receipt_preexisting: object,
    observation_preexisting: object,
    message: str,
) -> None:
    leg_dir, candidate, identity = _write_paused_leg_inputs(
        tmp_path,
        manifest_keys=["bird:0"],
        ordered_keys=["bird:0"],
        completed_keys=["bird:0"],
        not_started_keys=[],
    )
    (leg_dir / "observations.jsonl").write_text(
        json.dumps(
            {
                "case_key": "bird:0",
                "preexisting_history_items": observation_preexisting,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (leg_dir / "empty_history_evidence.json").write_text(
        json.dumps(
            {
                "receipts": [
                    {
                        "case_key": "bird:0",
                        "verification_status": "unavailable",
                        "preexisting_history_items": receipt_preexisting,
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    candidate["observations_sha256"] = _artifact_sha256(
        leg_dir / "observations.jsonl"
    )
    candidate["empty_history_evidence_sha256"] = _artifact_sha256(
        leg_dir / "empty_history_evidence.json"
    )

    with pytest.raises(release.SandboxError, match=message):
        release.validate_paused_leg_inputs(
            leg_dir,
            candidate,
            identity=identity,
            configuration_digest="sha256:configuration",
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "duplicate_manifest",
        "duplicate_ordered",
        "missing_ordered",
        "foreign_ordered",
        "invalid_manifest_key",
        "invalid_ordered_key",
        "wrong_completed_count",
        "wrong_prefix",
        "wrong_suffix",
    ),
)
def test_paused_leg_rejects_invalid_execution_partition(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest_keys: list[object] = ["bird:0", "bird:1", "bird:2"]
    ordered_keys: list[object] = ["bird:0", "bird:1", "bird:2"]
    completed_keys: list[object] = ["bird:0"]
    not_started_keys: list[object] = ["bird:1", "bird:2"]
    if mutation == "duplicate_manifest":
        manifest_keys[-1] = "bird:1"
    elif mutation == "duplicate_ordered":
        ordered_keys[-1] = "bird:1"
    elif mutation == "missing_ordered":
        ordered_keys.pop()
        not_started_keys.pop()
    elif mutation == "foreign_ordered":
        ordered_keys[-1] = "bird:foreign"
        not_started_keys[-1] = "bird:foreign"
    elif mutation == "invalid_manifest_key":
        manifest_keys[-1] = ""
    elif mutation == "invalid_ordered_key":
        ordered_keys[-1] = 2
        not_started_keys[-1] = 2
    elif mutation == "wrong_prefix":
        completed_keys[:] = ["bird:1"]
        not_started_keys[:] = ["bird:0", "bird:2"]
    elif mutation == "wrong_suffix":
        not_started_keys.reverse()

    leg_dir, candidate, identity = _write_paused_leg_inputs(
        tmp_path,
        manifest_keys=manifest_keys,
        ordered_keys=ordered_keys,
        completed_keys=completed_keys,
        not_started_keys=not_started_keys,
    )
    if mutation == "wrong_completed_count":
        candidate["completed_case_count"] = 2

    with pytest.raises(release.SandboxError, match="paused"):
        release.validate_paused_leg_inputs(
            leg_dir,
            candidate,
            identity=identity,
            configuration_digest="sha256:configuration",
        )


@pytest.mark.parametrize(
    ("writer", "payload"),
    [
        (reporting.write_json_new, {"record_kind": "candidate"}),
        (reporting.write_text_new, "candidate\n"),
    ],
)
def test_new_diagnostic_artifact_never_overwrites_a_late_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    writer: object,
    payload: object,
) -> None:
    target = tmp_path / "diagnostic.json"
    original_link = reporting.os.link

    def late_writer(source: object, destination: object, *args: object, **kwargs: object) -> None:
        Path(destination).write_text("winner\n", encoding="utf-8")
        original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(reporting.os, "link", late_writer)
    with pytest.raises(ValueError, match="another process"):
        writer(target, payload)  # type: ignore[operator]
    assert target.read_text(encoding="utf-8") == "winner\n"
    assert not list(tmp_path.glob(".diagnostic.json.*"))


def test_sealed_release_lock_never_overwrites_a_late_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "release-lock.json"
    original_link = release.os.link

    def late_writer(source: object, destination: object, *args: object, **kwargs: object) -> None:
        Path(destination).write_bytes(b'{"winner":true}\n')
        original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(release.os, "link", late_writer)
    with pytest.raises(release.SandboxError, match="another process"):
        release.write_release_input_lock_new(target, {"lock": "loser"})
    assert target.read_bytes() == b'{"winner":true}\n'
    assert not list(tmp_path.glob(".release-lock.json.*"))


def test_sealed_release_lock_is_read_only_before_publication(tmp_path: Path) -> None:
    target = tmp_path / "release-lock.json"

    release.write_release_input_lock_new(target, {"lock": "sealed"})

    assert (target.stat().st_mode & 0o777) == 0o444


def test_failure_taxonomy_is_complete_and_ordered_including_zeroes() -> None:
    rows = ordered_failure_rows({"correct": 2, "wrong_result": 1}, total=3)

    assert [row["failure_class"] for row in rows] == list(FAILURE_CLASS_ORDER)
    assert rows[0] == {
        "failure_class": "runner_or_transport_error", "count": 0, "share": 0.0
    }
    assert sum(int(row["count"]) for row in rows) == 3


def test_recovery_seals_candidate_before_bundle_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle"
    leg_dir = output / "runs" / "bird" / "r1"
    leg_dir.mkdir(parents=True)
    candidate_path = leg_dir / "governance/mid_repeat/000020/early_stop_candidate.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text('{"candidate":true}\n', encoding="utf-8")
    candidate_path.chmod(0o444)
    state = {
        "record_kind": "text2sql_public_benchmark_bundle_state",
        "active_leg": {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7},
        "status": "running",
    }
    seen: dict[str, object] = {}
    def validate_candidate(_path: Path, active: object) -> dict[str, object]:
        seen["active"] = dict(active)  # type: ignore[arg-type]
        return {"completed_case_count": 20}

    monkeypatch.setattr(release, "validate_paused_candidate", validate_candidate)
    monkeypatch.setattr(release, "validate_paused_leg_inputs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        coordinator,
        "_authenticate_paused_prefix",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(release, "write_bundle_state", lambda _path, payload: seen.setdefault("state", dict(payload)))

    assert not release.recover_active_leg(
        output,
        state=state,
        identity={},
        configuration_digest="sha256:config",
        progress_store=SimpleNamespace(
            progress=lambda: SimpleNamespace(prefix_case_count=20)
        ),
    )

    assert state["status"] == "AWAITING_REPAIR_DECISION"
    assert str(state["active_leg"]["candidate_sha256"]).startswith("sha256:")
    assert seen["active"] == state["active_leg"]


def test_recovery_rejects_missing_indexed_candidate(tmp_path: Path) -> None:
    output = tmp_path / "bundle"
    leg_dir = output / "runs" / "bird" / "r1"
    leg_dir.mkdir(parents=True)
    with pytest.raises(release.SandboxError, match="candidate is missing"):
        release.recover_active_leg(
            output,
            state={
                "record_kind": "text2sql_public_benchmark_bundle_state",
                "active_leg": {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7},
            },
                identity={},
                configuration_digest="sha256:config",
                progress_store=SimpleNamespace(
                    progress=lambda: SimpleNamespace(prefix_case_count=1)
                ),
            )


def test_recovery_authenticates_sqlite_before_repairing_missing_observations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle"
    leg_dir = output / "runs" / "bird" / "r1"
    leg_dir.mkdir(parents=True)
    store = ReleaseProgressStore(tmp_path / "state" / "release_progress.sqlite3")
    store.bind_bundle(
        bundle_id="bundle-1",
        release_lock_digest="sha256:lock",
        release_plan_digest="sha256:plan",
    )
    store.start_leg(benchmark="bird", repeat_ordinal=1, seed=7)
    store.bind_leg_inputs(
        benchmark="bird",
        repeat_ordinal=1,
        run_manifest_sha256="sha256:run",
        case_manifest_sha256="sha256:cases",
        ordered_case_keys=["bird:1"],
    )
    store.begin_case(benchmark="bird", repeat_ordinal=1, case_key="bird:1")
    store.commit_case(
        benchmark="bird",
        repeat_ordinal=1,
        ordinal=0,
        case_key="bird:1",
        observation={"case_key": "bird:1", "observation_status": "completed"},
        history_receipt={
            "case_key": "bird:1",
            "verification_status": "verified",
            "preexisting_history_items": 0,
        },
    )
    expected_observations = store.observation_bytes(
        benchmark="bird", repeat_ordinal=1
    )
    candidate = {
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "completed_case_keys": ["bird:1"],
        "completed_case_count": 1,
        "observations_sha256": (
            "sha256:" + hashlib.sha256(expected_observations).hexdigest()
        ),
    }
    candidate_path = leg_dir / "governance/mid_repeat/000001/early_stop_candidate.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(json.dumps(candidate) + "\n", encoding="utf-8")
    candidate_path.chmod(0o444)
    store.pause_for_candidate(
        candidate_sha256="sha256:" + release.sha256_file(candidate_path),
        prefix_case_count=1,
    )
    monkeypatch.setattr(release, "validate_paused_candidate", lambda *_args: candidate)

    def validate_views(*_args: object, **_kwargs: object) -> None:
        assert (leg_dir / "observations.jsonl").read_bytes() == expected_observations

    monkeypatch.setattr(release, "validate_paused_leg_inputs", validate_views)
    state = {
        "record_kind": "text2sql_public_benchmark_bundle_state",
        "active_leg": {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7},
        "status": "running",
    }

    assert not release.recover_active_leg(
        output,
        state=state,
        identity={},
        configuration_digest="sha256:config",
        progress_store=store,
    )
    assert (leg_dir / "observations.jsonl").read_bytes() == expected_observations


def test_post_repeat_candidate_binds_the_complete_current_evaluation(
    tmp_path: Path,
) -> None:
    leg_dir = tmp_path / "runs" / "bird" / "r1"
    _write_post_repeat_evaluation(leg_dir)
    execution = object.__new__(ReleaseBundleExecution)
    execution.progress_store = SimpleNamespace(transition=lambda **_kwargs: None)
    execution.bundle_id = "bundle-1"
    execution.state = {"bundle_id": "bundle-1"}
    execution._write_state = lambda: None

    execution._pause_for_post_repeat_candidate(
        leg_dir,
        {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7},
        _post_repeat_candidate_base(),
    )

    candidate = json.loads(
        (
            leg_dir
            / "governance/post_repeat/000001/early_stop_candidate.json"
        ).read_text(encoding="utf-8")
    )
    evaluation = json.loads(
        (leg_dir / "evaluation_manifest.json").read_text(encoding="utf-8")
    )
    assert candidate["schema_version"] == 1
    assert {
        "evaluation_manifest_sha256": _artifact_sha256(
            leg_dir / "evaluation_manifest.json"
        ),
        "summary_sha256": _artifact_sha256(leg_dir / "summary.json"),
        "score_sha256": evaluation["score_sha256"],
        "evaluator_input_sha256": evaluation["evaluator_input_sha256"],
        "diagnostics_sha256": _artifact_sha256(leg_dir / "diagnostics.jsonl"),
    }.items() <= candidate.items()


@pytest.mark.parametrize(
    "mutation",
    ("wrong_kind", "wrong_benchmark", "wrong_bundle", "unexpected_field"),
)
def test_post_repeat_finalizer_rejects_candidate_schema_and_identity_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    leg_dir = tmp_path / "runs" / "bird" / "r1"
    _write_post_repeat_evaluation(leg_dir)
    candidate_payload = _post_repeat_candidate(leg_dir)
    _mutate_post_repeat_candidate(candidate_payload, mutation)
    candidate_path = leg_dir / "governance/post_repeat/000001/early_stop_candidate.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(
        json.dumps(candidate_payload) + "\n", encoding="utf-8"
    )
    candidate_path.chmod(0o444)
    decision_path = candidate_path.with_name("repair_decision.json")
    decision_path.write_text('{"decision":"STOP_AND_REPAIR"}\n', encoding="utf-8")
    plan = [{"benchmark": "bird", "repeat_ordinal": 1, "seed": 7}]

    with pytest.raises(DiagnosticArtifactError):
        release.finalize_post_repeat_stop(
            candidate_path.parent,
            candidate_path=candidate_path,
            decision_path=decision_path,
            release_plan=plan,
            evaluation_leg=plan[0],
            expected_bundle_id="bundle-1",
        )


@pytest.mark.parametrize(
    "mutation",
    ("wrong_kind", "wrong_benchmark", "wrong_bundle", "unexpected_field"),
)
def test_post_repeat_resume_rejects_candidate_schema_and_identity_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    output_dir = tmp_path / "bundle"
    leg_dir = output_dir / "runs" / "bird" / "r1"
    identity = _write_post_repeat_evaluation(leg_dir)
    candidate_payload = _post_repeat_candidate(leg_dir)
    _mutate_post_repeat_candidate(candidate_payload, mutation)
    candidate_path = leg_dir / "governance/post_repeat/000001/early_stop_candidate.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(
        json.dumps(candidate_payload) + "\n", encoding="utf-8"
    )
    candidate_path.chmod(0o444)
    execution = object.__new__(ReleaseBundleExecution)
    execution.progress_store = SimpleNamespace(
        progress=lambda: SimpleNamespace(prefix_case_count=1)
    )
    execution.bundle_id = "bundle-1"
    execution.output_dir = output_dir
    execution.evaluator_identities = {"bird": identity}
    execution.state = {
        "evaluation_leg": {
            "benchmark": "bird",
            "repeat_ordinal": 1,
            "seed": 7,
            "candidate_sha256": _artifact_sha256(candidate_path),
        }
    }
    execution.args = SimpleNamespace(repair_decision=None)

    with pytest.raises(release.SandboxError):
        execution._resume_post_repeat_decision()


@pytest.mark.parametrize(
    ("path_field", "artifact_name"),
    (
        ("score_path", "official_scores.json"),
        ("evaluator_input_path", "bird_predictions.json"),
    ),
)
def test_post_repeat_finalizer_rejects_noncanonical_evaluation_artifact_path(
    tmp_path: Path,
    path_field: str,
    artifact_name: str,
) -> None:
    leg_dir = tmp_path / "runs" / "bird" / "r1"
    _write_post_repeat_evaluation(leg_dir)
    evaluation_path = leg_dir / "evaluation_manifest.json"
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation[path_field] = f"nested/../{artifact_name}"
    evaluation_path.write_text(json.dumps(evaluation) + "\n", encoding="utf-8")
    candidate_path = leg_dir / "governance/post_repeat/000001/early_stop_candidate.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(
        json.dumps(_post_repeat_candidate(leg_dir)) + "\n", encoding="utf-8"
    )
    candidate_path.chmod(0o444)
    decision_path = candidate_path.with_name("repair_decision.json")
    decision_path.write_text('{"decision":"STOP_AND_REPAIR"}\n', encoding="utf-8")
    plan = [{"benchmark": "bird", "repeat_ordinal": 1, "seed": 7}]

    with pytest.raises(DiagnosticArtifactError):
        release.finalize_post_repeat_stop(
            candidate_path.parent,
            candidate_path=candidate_path,
            decision_path=decision_path,
            release_plan=plan,
            evaluation_leg=plan[0],
            expected_bundle_id="bundle-1",
        )


def test_post_repeat_finalizer_rejects_self_consistent_evidence_replacement(
    tmp_path: Path,
) -> None:
    leg_dir = tmp_path / "runs" / "bird" / "r1"
    _write_post_repeat_evaluation(leg_dir)
    candidate_path = leg_dir / "governance/post_repeat/000001/early_stop_candidate.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(
        json.dumps(_post_repeat_candidate(leg_dir)) + "\n", encoding="utf-8"
    )
    candidate_path.chmod(0o444)
    decision_path = candidate_path.with_name("repair_decision.json")
    decision_path.write_text('{"decision":"STOP_AND_REPAIR"}\n', encoding="utf-8")
    _replace_post_repeat_diagnostics(leg_dir)
    plan = [
        {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7},
        {"benchmark": "bird", "repeat_ordinal": 2, "seed": 8},
    ]

    with pytest.raises(DiagnosticArtifactError):
        release.finalize_post_repeat_stop(
            candidate_path.parent,
            candidate_path=candidate_path,
            decision_path=decision_path,
            release_plan=plan,
            evaluation_leg=plan[0],
            expected_bundle_id="bundle-1",
        )

    assert not (candidate_path.parent / "early_stop.json").exists()
    assert not (candidate_path.parent / "early_stop_report.md").exists()


def test_post_repeat_resume_rejects_replacement_before_decision_discovery(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "bundle"
    leg_dir = output_dir / "runs" / "bird" / "r1"
    identity = _write_post_repeat_evaluation(leg_dir)
    candidate_path = leg_dir / "governance/post_repeat/000001/early_stop_candidate.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(
        json.dumps(_post_repeat_candidate(leg_dir)) + "\n", encoding="utf-8"
    )
    candidate_path.chmod(0o444)
    candidate_digest = _artifact_sha256(candidate_path)
    _replace_post_repeat_diagnostics(leg_dir)
    execution = object.__new__(ReleaseBundleExecution)
    execution.progress_store = SimpleNamespace(
        progress=lambda: SimpleNamespace(prefix_case_count=1)
    )
    execution.bundle_id = "bundle-1"
    execution.output_dir = output_dir
    execution.evaluator_identities = {"bird": identity}
    execution.state = {
        "evaluation_leg": {
            "benchmark": "bird",
            "repeat_ordinal": 1,
            "seed": 7,
            "candidate_sha256": candidate_digest,
        }
    }
    execution.args = SimpleNamespace(repair_decision=None)

    with pytest.raises(release.SandboxError):
        execution._resume_post_repeat_decision()


def test_post_repeat_stop_finalization_is_idempotent_and_records_remaining_legs(
    tmp_path: Path,
) -> None:
    leg_dir = tmp_path / "runs" / "bird" / "r1"
    _write_post_repeat_evaluation(leg_dir)
    candidate = leg_dir / "governance/post_repeat/000001/early_stop_candidate.json"
    candidate.parent.mkdir(parents=True)
    decision = candidate.with_name("repair_decision.json")
    candidate_payload = _post_repeat_candidate(leg_dir)
    candidate.write_text(
        json.dumps(candidate_payload) + "\n", encoding="utf-8"
    )
    candidate.chmod(0o444)
    decision.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_kind": "text2sql_public_benchmark_repair_decision",
                "candidate_sha256": _artifact_sha256(candidate),
                "decision": "STOP_AND_REPAIR",
                "reviewed_by": "benchmark-reviewer",
                "reviewed_at": "2026-08-04T12:00:00+00:00",
                "root_hypothesis": "Typed evidence is missing for one requirement.",
                "red_test_plan": "Add a regression case for that requirement.",
                "predicted_improvement": "Reduce this repeated failure class.",
                "safety_guarantees": ["keep the pre-execution gate"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    plan = [
        {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7},
        {"benchmark": "bird", "repeat_ordinal": 2, "seed": 8},
    ]
    active = plan[0]

    release.finalize_post_repeat_stop(
        leg_dir,
        candidate_path=candidate,
        decision_path=decision,
        release_plan=plan,
        evaluation_leg=active,
        expected_bundle_id="bundle-1",
    )
    first = (
        candidate.parent / "early_stop.json"
    ).read_bytes()
    release.finalize_post_repeat_stop(
        leg_dir,
        candidate_path=candidate,
        decision_path=decision,
        release_plan=plan,
        evaluation_leg=active,
        expected_bundle_id="bundle-1",
    )

    assert (candidate.parent / "early_stop.json").read_bytes() == first
    for name in (
        "early_stop_candidate.json",
        "repair_decision.json",
        "early_stop.json",
    ):
        assert (candidate.parent / name).stat().st_mode & 0o777 == 0o444
    assert (leg_dir / "early_stop_report.md").stat().st_mode & 0o777 == 0o444
    payload = json.loads(first)
    assert payload["not_started_legs"] == [plan[1]]
    report = (leg_dir / "early_stop_report.md").read_text(encoding="utf-8")
    assert "Completed cases: 1" in report
    assert "Not-started release legs: 1" in report
    assert "## Reviewed repair decision" in report
    assert "Typed evidence is missing for one requirement." in report
    assert "Add a regression case for that requirement." in report
    assert (
        f"observations.jsonl ({candidate_payload['observations_sha256']})" in report
    )
    assert "runner_or_transport_error" in report
    assert "correct" in report
    assert (
        "- `governance/post_repeat/000001/early_stop_candidate.json`" in report
    )
    assert "- `governance/post_repeat/000001/repair_decision.json`" in report
    assert "- `post_repeat_candidate.json`" not in report
    assert "- `post_repeat_repair_decision.json`" not in report

    (leg_dir / "early_stop_report.md").chmod(0o600)
    (leg_dir / "early_stop_report.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(DiagnosticArtifactError, match="changed"):
        release.finalize_post_repeat_stop(
            leg_dir,
            candidate_path=candidate,
            decision_path=decision,
            release_plan=plan,
            evaluation_leg=active,
            expected_bundle_id="bundle-1",
        )


def test_mid_repeat_stop_report_records_completed_and_remaining_release_work(
    tmp_path: Path,
) -> None:
    leg_dir = tmp_path / "runs" / "bird" / "r1"
    leg_dir.mkdir(parents=True)
    observation = _observation(0)
    observations_path = leg_dir / "observations.jsonl"
    observations_path.write_text(json.dumps(observation) + "\n", encoding="utf-8")
    candidate = {
        "signature": {"error_class": "ambiguous_requirement"},
        "not_started_case_keys": ["bird:1"],
        "completed_case_count": 1,
        "observations_sha256": _artifact_sha256(observations_path),
    }
    candidate_path = leg_dir / "governance/mid_repeat/000001/early_stop_candidate.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(json.dumps(candidate) + "\n", encoding="utf-8")
    candidate_path.chmod(0o444)
    decision_path = candidate_path.with_name("repair_decision.json")
    decision_path.write_text('{"decision":"STOP_AND_REPAIR"}\n', encoding="utf-8")
    plan = [
        {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7},
        {"benchmark": "spider", "repeat_ordinal": 1, "seed": 8},
    ]

    diagnostics.finalize_partial_stop(
        leg_dir,
        candidate=candidate,
        candidate_path=candidate_path,
        decision_path=decision_path,
        observations=[observation],
        classify=reporting.failure_class,
        release_plan=plan,
        evaluation_leg=plan[0],
    )

    report = (leg_dir / "failure_report.md").read_text(encoding="utf-8")
    assert "Completed cases: 1" in report
    assert "Not-started release legs: 1" in report
    assert f"observations.jsonl ({candidate['observations_sha256']})" in report
    assert "- `governance/mid_repeat/000001/early_stop_candidate.json`" in report
    assert "- `governance/mid_repeat/000001/repair_decision.json`" in report


@pytest.mark.parametrize(
    ("event_kind", "candidate_name", "decision_name", "result_name"),
    [
        (
            "mid_repeat",
            "governance/mid_repeat/000020/early_stop_candidate.json",
            "governance/mid_repeat/000020/repair_decision.json",
            "governance/mid_repeat/000020/early_stop.json",
        ),
        (
            "post_repeat",
            "governance/post_repeat/000020/early_stop_candidate.json",
            "governance/post_repeat/000020/repair_decision.json",
            "governance/post_repeat/000020/early_stop.json",
        ),
    ],
)
def test_continue_decision_creates_distinct_sealed_final_evidence(
    tmp_path: Path,
    event_kind: str,
    candidate_name: str,
    decision_name: str,
    result_name: str,
) -> None:
    leg_dir = tmp_path / "leg"
    leg_dir.mkdir()
    candidate = {
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "completed_case_count": 20,
        "observations_sha256": "sha256:" + "a" * 64,
    }
    candidate_path = leg_dir / candidate_name
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(json.dumps(candidate) + "\n", encoding="utf-8")
    candidate_path.chmod(0o444)
    candidate_sha256 = release.sha256_file(candidate_path)
    decision_path = leg_dir / decision_name
    decision_path.write_text(
        json.dumps(
            {
                "decision": "CONTINUE",
                "candidate_sha256": "sha256:" + candidate_sha256,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decision_path.chmod(0o444)

    event = diagnostics.finalize_continue_decision(
        leg_dir, event_kind=event_kind, completed_case_count=20
    )
    second = diagnostics.finalize_continue_decision(
        leg_dir, event_kind=event_kind, completed_case_count=20
    )

    assert event == second
    assert event["result_path"] == result_name
    result_path = leg_dir / result_name
    assert result_path.stat().st_mode & 0o777 == 0o444
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result == {
        "schema_version": 1,
        "record_kind": "text2sql_public_benchmark_early_stop",
        "event_kind": event_kind,
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "decision": "CONTINUE",
        "candidate_sha256": "sha256:" + candidate_sha256,
        "repair_decision_sha256": "sha256:" + release.sha256_file(decision_path),
        "completed_case_count": 20,
        "observations_sha256": "sha256:" + "a" * 64,
    }


@pytest.mark.parametrize(
    ("event_kind", "candidate_name", "decision_name"),
    [
        (
            "mid_repeat",
            "governance/mid_repeat/000001/early_stop_candidate.json",
            "governance/mid_repeat/000001/repair_decision.json",
        ),
        (
            "post_repeat",
            "governance/post_repeat/000001/early_stop_candidate.json",
            "governance/post_repeat/000001/repair_decision.json",
        ),
    ],
)
def test_continue_finalization_rejects_writable_candidate_without_resealing(
    tmp_path: Path,
    event_kind: str,
    candidate_name: str,
    decision_name: str,
) -> None:
    leg_dir = tmp_path / "leg"
    leg_dir.mkdir()
    candidate_path = leg_dir / candidate_name
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(
        json.dumps(
            {
                "benchmark": "bird",
                "repeat_ordinal": 1,
                "completed_case_count": 1,
                "observations_sha256": "sha256:observations",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    candidate_path.chmod(0o644)
    decision_path = leg_dir / decision_name
    decision_path.write_text(
        json.dumps(
            {
                "decision": "CONTINUE",
                "candidate_sha256": "sha256:" + release.sha256_file(candidate_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DiagnosticArtifactError, match="not sealed"):
        diagnostics.finalize_continue_decision(
            leg_dir, event_kind=event_kind, completed_case_count=1
        )

    assert candidate_path.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("missing", ["benchmark", "repeat_ordinal"])
def test_continue_decision_requires_exact_leg_identity(
    tmp_path: Path,
    missing: str,
) -> None:
    leg_dir = tmp_path / "leg"
    leg_dir.mkdir()
    candidate = {
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "completed_case_count": 20,
        "observations_sha256": "sha256:" + "a" * 64,
    }
    candidate.pop(missing)
    candidate_path = leg_dir / "governance/mid_repeat/000020/early_stop_candidate.json"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(json.dumps(candidate) + "\n", encoding="utf-8")
    candidate_path.chmod(0o444)
    decision_path = candidate_path.with_name("repair_decision.json")
    decision_path.write_text(
        json.dumps(
            {
                "decision": "CONTINUE",
                "candidate_sha256": "sha256:" + release.sha256_file(candidate_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decision_path.chmod(0o444)

    with pytest.raises(DiagnosticArtifactError, match="candidate evidence"):
        diagnostics.finalize_continue_decision(
            leg_dir, event_kind="mid_repeat", completed_case_count=20
        )


def test_leg_handshake_accepts_both_complete_governance_events_and_no_partial_set(
    tmp_path: Path,
) -> None:
    leg_dir = tmp_path / "leg"
    leg_dir.mkdir()
    for name in (
        "manifest.json",
        "case_manifest.json",
        "observations.jsonl",
        "empty_history_evidence.json",
    ):
        (leg_dir / name).write_text("{}\n", encoding="utf-8")
    events: list[dict[str, str]] = []
    for event_kind in ("mid_repeat", "post_repeat"):
        candidate_name = (
            f"governance/{event_kind}/000020/early_stop_candidate.json"
        )
        decision_name = f"governance/{event_kind}/000020/repair_decision.json"
        candidate_path = leg_dir / candidate_name
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(
            json.dumps(
                {
                    "benchmark": "bird",
                    "repeat_ordinal": 1,
                    "completed_case_count": 20,
                    "observations_sha256": "sha256:" + "a" * 64,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        candidate_path.chmod(0o444)
        decision_path = leg_dir / decision_name
        decision_path.write_text(
            json.dumps(
                {
                    "decision": "CONTINUE",
                    "candidate_sha256": "sha256:" + release.sha256_file(candidate_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        decision_path.chmod(0o444)
        events.append(
            diagnostics.finalize_continue_decision(
                leg_dir, event_kind=event_kind, completed_case_count=20
            )
        )

    artifacts._write_artifact_handshake(
        leg_dir,
        benchmark="bird",
        repeat_ordinal=1,
        bundle_id="bundle-1",
        case_manifest_digest=(
            "sha256:" + release.sha256_file(leg_dir / "case_manifest.json")
        ),
        snapshot_digest="sha256:snapshot",
        configuration_digest="sha256:configuration",
        seed=7,
        run_scope="full_release",
        execution_mode="canonical_release",
        governance_events=events,
    )
    handshake = json.loads(
        (leg_dir / "artifact_handshake.json").read_text(encoding="utf-8")
    )

    release._validate_leg_artifacts(leg_dir, handshake)
    assert {
        "governance/mid_repeat/000020/early_stop.json",
        "governance/post_repeat/000020/early_stop.json",
    }.issubset(handshake["artifacts"])

    partial = {**handshake, "artifacts": dict(handshake["artifacts"])}
    partial["artifacts"].pop("governance/mid_repeat/000020/early_stop.json")
    with pytest.raises(release.SandboxError, match="partial"):
        release._validate_leg_artifacts(leg_dir, partial)


def test_indexed_governance_triple_rejects_partial_event(tmp_path: Path) -> None:
    leg_dir = tmp_path / "leg"
    leg_dir.mkdir()
    for name in (
        "manifest.json",
        "case_manifest.json",
        "observations.jsonl",
        "empty_history_evidence.json",
    ):
        (leg_dir / name).write_text("{}\n", encoding="utf-8")
    candidate = leg_dir / "governance/mid_repeat/000001/early_stop_candidate.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("{}\n", encoding="utf-8")
    candidate.chmod(0o444)
    handshake = {
        "artifacts": {
            **{
                name: _artifact_sha256(leg_dir / name)
                for name in (
                    "manifest.json",
                    "case_manifest.json",
                    "observations.jsonl",
                    "empty_history_evidence.json",
                )
            },
            "governance/mid_repeat/000001/early_stop_candidate.json": (
                _artifact_sha256(candidate)
            ),
        }
    }

    with pytest.raises(release.SandboxError, match="partial|incomplete"):
        release._validate_leg_artifacts(leg_dir, handshake)


def _evaluated_leg_artifacts(leg_dir: Path) -> dict[str, str]:
    names = (
        *release.MANDATORY_LEG_ARTIFACTS,
        *release.EVALUATED_LEG_ARTIFACTS,
    )
    for name in names:
        path = leg_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name + "\n", encoding="utf-8")
        if name in release.EVALUATED_LEG_ARTIFACTS:
            path.chmod(0o444)
    return {name: _artifact_sha256(leg_dir / name) for name in names}


def test_deferred_handshake_includes_complete_evaluated_artifacts(
    tmp_path: Path,
) -> None:
    leg_dir = tmp_path / "leg"
    leg_dir.mkdir()
    _evaluated_leg_artifacts(leg_dir)
    progress_store = SimpleNamespace(governance_events=lambda **_kwargs: [])

    coordinator._write_deferred_leg_handshake(
        leg_dir=leg_dir,
        benchmark="bird",
        repeat_ordinal=1,
        seed=7,
        bundle_id="bundle-1",
        identity={
            "source_snapshot_digest": "sha256:snapshot",
            "source_snapshot_manifest_digest": "sha256:snapshot-manifest",
        },
        configuration_digest="sha256:configuration",
        progress_store=progress_store,
    )

    handshake = json.loads(
        (leg_dir / "artifact_handshake.json").read_text(encoding="utf-8")
    )
    assert set(handshake["artifacts"]) == (
        set(release.MANDATORY_LEG_ARTIFACTS)
        | set(release.EVALUATED_LEG_ARTIFACTS)
    )
    assert set(release.EVALUATED_LEG_ARTIFACTS) == {
        "runner_stdout.log",
        "runner_stderr.log",
        *evaluator_bridge.CANONICAL_PUBLISH_ORDER,
    }
    release._validate_leg_artifacts(leg_dir, handshake)


def test_evaluated_leg_handshake_requires_complete_sealed_inventory(
    tmp_path: Path,
) -> None:
    leg_dir = tmp_path / "leg"
    leg_dir.mkdir()
    artifacts_by_name = _evaluated_leg_artifacts(leg_dir)
    handshake = {"artifacts": artifacts_by_name}

    release._validate_leg_artifacts(leg_dir, handshake)

    partial = {"artifacts": dict(artifacts_by_name)}
    partial["artifacts"].pop("runner_stdout.log")
    with pytest.raises(release.SandboxError, match="evaluated.*partial"):
        release._validate_leg_artifacts(leg_dir, partial)

    unknown = {"artifacts": dict(artifacts_by_name)}
    unknown_path = leg_dir / "unexpected.log"
    unknown_path.write_text("unexpected\n", encoding="utf-8")
    unknown_path.chmod(0o444)
    unknown["artifacts"]["unexpected.log"] = _artifact_sha256(unknown_path)
    with pytest.raises(release.SandboxError, match="incomplete or unknown"):
        release._validate_leg_artifacts(leg_dir, unknown)

    tampered = {"artifacts": dict(artifacts_by_name)}
    evaluated_path = leg_dir / "runner_stdout.log"
    evaluated_path.chmod(0o600)
    evaluated_path.write_text("tampered\n", encoding="utf-8")
    evaluated_path.chmod(0o444)
    with pytest.raises(release.SandboxError, match="changed"):
        release._validate_leg_artifacts(leg_dir, tampered)

    unsealed = {"artifacts": dict(artifacts_by_name)}
    evaluated_path.chmod(0o600)
    evaluated_path.write_text("runner_stdout.log\n", encoding="utf-8")
    with pytest.raises(release.SandboxError, match="not sealed"):
        release._validate_leg_artifacts(leg_dir, unsealed)


def test_score_classes_require_closed_evaluator_receipt() -> None:
    row = _observation(0)
    row["outcome"] = {"status": "succeeded", "reason_code": "OK"}
    receipt = _receipt(["bird:0"])
    assert reporting.failure_class(row, 1, receipt) == "correct"
    assert reporting.failure_class(row, 0, receipt) == "wrong_result"
    assert reporting.failure_class(row, True, receipt) == "evaluator_failure"
    assert reporting.failure_class(row, 1) == "evidence_incomplete"


def test_no_policy_cannot_create_early_stop_candidate() -> None:
    rows = [_observation(index, database_id=f"db-{index % 3}") for index in range(20)]
    assert reporting.find_early_stop_candidate(rows, reporting.parse_early_stop_policy(_policy()))
