"""Command-line release gate for reviewed Text-to-SQL evaluation cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from streamlit_app.text_to_sql_client import TextToSqlApiClient

from .cases import load_gold_cases, validate_case_coverage
from .metrics import (
    compute_release_metrics,
    compute_release_metrics_by_repetition,
    evaluate_release_thresholds,
    normalize_schema_links,
)
from .observability import canonical_files_digest, write_json_evidence
from .release import (
    EXIT_APPROVAL,
    EXIT_CONTRACT,
    EXIT_ELIGIBLE,
    EXIT_INFRASTRUCTURE,
    EXIT_PROTECTED_LANE,
    CandidateIdentity,
    ReleasePolicy,
    load_approved_latency_baseline,
    load_protected_lane_evidence,
)
from .runner import AuthenticatedT13EvalAdapter, EvalObservation


logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate Text-to-SQL release evidence")
    parser.add_argument("--gold", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("deterministic-contract", "release"),
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--protected-evidence", action="append", default=[])
    parser.add_argument("--connection-map")
    parser.add_argument("--api-url")
    parser.add_argument("--auth-token-env", default="TEXT2SQL_EVAL_API_TOKEN")
    parser.add_argument("--model-id")
    parser.add_argument("--provider-id")
    parser.add_argument("--model-config-digest")
    parser.add_argument("--latency-baseline")
    parser.add_argument("--target-policy")
    parser.add_argument("--commit")
    parser.add_argument("--candidate-digest")
    parser.add_argument("--lock-digest")
    return parser


def _sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _finish(
    output: str | Path,
    artifact: dict[str, Any],
    *,
    exit_code: int,
    result_code: str,
) -> int:
    artifact.update(
        {
            "exit_code": exit_code,
            "result_code": result_code,
            "release_eligible": exit_code == EXIT_ELIGIBLE
            and artifact.get("mode") == "release",
        }
    )
    write_json_evidence(output, artifact)
    return exit_code


def _load_connection_map(path: str) -> dict[str, str]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("connection map must be a non-empty object")
    if not all(
        isinstance(key, str)
        and key.strip()
        and isinstance(value, str)
        and value.strip()
        for key, value in raw.items()
    ):
        raise ValueError("connection map entries must be non-empty strings")
    return {key.strip(): value.strip() for key, value in raw.items()}


def _release_interface_complete(args: argparse.Namespace) -> bool:
    return bool(
        args.commit
        and args.candidate_digest
        and args.lock_digest
        and args.model_id
        and args.provider_id
        and args.model_config_digest
        and args.latency_baseline
        and args.target_policy
        and args.protected_evidence
        and args.connection_map
        and args.api_url
    )


def _observation_record(observation: EvalObservation) -> dict[str, Any]:
    links = normalize_schema_links(observation.schema_links)
    return {
        "case_id": observation.case_id,
        "repetition": observation.repetition,
        "run_id": observation.run_id,
        "status": observation.status,
        "reason_code": observation.reason_code,
        "sql_sha256": hashlib.sha256(
            observation.generated_sql.encode("utf-8")
        ).hexdigest(),
        "schema_links": {
            "tables": sorted(links["tables"]),
            "columns": sorted(links["columns"]),
        },
        "rows": observation.rows,
        "columns": observation.columns,
        "generated": observation.generated,
        "approved": observation.approved,
        "executed": observation.executed,
        "dry_run": observation.dry_run,
        "audited": observation.audited,
        "duration_ms": observation.duration_ms,
        "error": observation.error,
    }


def _set_metrics(
    artifact: dict[str, Any],
    cases: list[object],
    observations: list[EvalObservation],
    policy: ReleasePolicy,
    *,
    latency_baseline_p95_ms: float | None,
    latency_gate_mode: str = "prior_release",
) -> tuple[str, ...]:
    metrics = compute_release_metrics(
        cases,
        observations,
        latency_baseline_p95_ms=latency_baseline_p95_ms,
    )
    artifact["metrics"] = metrics.to_mapping()
    artifact["observations"] = [
        _observation_record(observation) for observation in observations
    ]
    if not observations:
        failures = evaluate_release_thresholds(
            metrics, policy.thresholds, latency_gate_mode=latency_gate_mode
        )
        artifact["per_repetition_metrics"] = {}
        artifact["threshold_failures"] = list(failures)
        return failures
    repetition_metrics = compute_release_metrics_by_repetition(
        cases,
        observations,
        expected_repetitions=int(policy.thresholds["repetitions"]),
        latency_baseline_p95_ms=latency_baseline_p95_ms,
    )
    failures = tuple(
        f"repetition:{repetition}:{failure}"
        for repetition, repetition_metric in repetition_metrics.items()
        for failure in evaluate_release_thresholds(
            repetition_metric,
            policy.thresholds,
            latency_gate_mode=latency_gate_mode,
        )
    )
    artifact["per_repetition_metrics"] = {
        str(repetition): repetition_metric.to_mapping()
        for repetition, repetition_metric in repetition_metrics.items()
    }
    artifact["threshold_failures"] = list(failures)
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "mode": args.mode,
        "contract_passed": False,
        "release_eligible": False,
        "model_lane_satisfied": False,
        "provenance": {
            "commit": args.commit,
            "candidate_digest": args.candidate_digest,
            "lock_digest": args.lock_digest,
            "model_id": args.model_id,
            "provider_id": args.provider_id,
            "model_config_digest": args.model_config_digest,
        },
        "evidence_index": [],
        "advisories": [],
    }
    try:
        policy = ReleasePolicy.load(args.policy)
        all_cases = load_gold_cases(args.gold)
        cases = [case for case in all_cases if case.profile == "release"]
        if not cases:
            raise ValueError("gold set has no release-profile cases")
        coverage = validate_case_coverage(
            cases,
            required_slices=policy.required_slices,
            required_dialects=policy.required_dialects,
        )
        if args.repetitions is not None and args.repetitions != int(
            policy.thresholds["repetitions"]
        ):
            raise ValueError("--repetitions must equal the approved policy value")
        gold_digest = canonical_files_digest(args.gold)
        policy_digest = _sha256_file(args.policy)
        artifact.update(
            {
                "gold_sha256": gold_digest,
                "policy_sha256": policy_digest,
                "policy_scope_sha256": policy.scope_digest,
                "case_count": len(cases),
                "all_reviewed_case_count": len(all_cases),
                "coverage": {
                    "slices": list(coverage.slices),
                    "dialects": list(coverage.dialects),
                    "missing_slices": list(coverage.missing_slices),
                    "missing_dialects": list(coverage.missing_dialects),
                },
                "evidence_index": [
                    {"kind": "gold", "path": str(args.gold), "sha256": gold_digest},
                    {
                        "kind": "policy",
                        "path": str(args.policy),
                        "sha256": policy_digest,
                    },
                ],
            }
        )
        if not coverage.complete:
            raise ValueError("required release case coverage is incomplete")
        artifact["contract_passed"] = True
        _set_metrics(
            artifact,
            cases,
            [],
            policy,
            latency_baseline_p95_ms=None,
        )
    except Exception as exc:
        artifact["error"] = f"{type(exc).__name__}: {exc}"
        return _finish(
            args.output,
            artifact,
            exit_code=EXIT_CONTRACT,
            result_code="CONTRACT_OR_COVERAGE_FAILED",
        )

    if args.mode == "deterministic-contract":
        return _finish(
            args.output,
            artifact,
            exit_code=EXIT_ELIGIBLE,
            result_code="DETERMINISTIC_CONTRACT_ONLY",
        )

    approval_current = policy.approval_is_current
    artifact["approval_status"] = policy.approval.status
    artifact["approval_digest_current"] = (
        policy.approval.thresholds_sha256 == policy.scope_digest
    )
    if not _release_interface_complete(args):
        artifact["error"] = "release mode requires complete candidate and infrastructure arguments"
        return _finish(
            args.output,
            artifact,
            exit_code=EXIT_INFRASTRUCTURE,
            result_code="EVALUATOR_INFRASTRUCTURE_FAILED",
        )

    try:
        candidate = CandidateIdentity(
            args.commit,
            args.candidate_digest,
            args.lock_digest,
        )
        if (
            not isinstance(args.model_config_digest, str)
            or len(args.model_config_digest) != 64
            or any(character not in "0123456789abcdef" for character in args.model_config_digest)
        ):
            raise ValueError("--model-config-digest must be a lowercase SHA-256")
        model_lane = f"model:{args.model_id}"
        configured_model_lanes = {
            lane
            for lane in policy.required_protected_lanes
            if lane.startswith("model:")
        }
        if configured_model_lanes != {model_lane}:
            raise ValueError("--model-id does not match the approved model lane")
        driver_lanes = {
            lane
            for lane in policy.required_protected_lanes
            if lane.startswith("driver:")
        }
        if driver_lanes | configured_model_lanes != set(
            policy.required_protected_lanes
        ):
            raise ValueError("unsupported protected lane kind")
        lane_evidence = load_protected_lane_evidence(
            args.protected_evidence,
            expected_candidate=candidate,
            required_driver_lanes=driver_lanes,
        )
        latency_baseline = load_approved_latency_baseline(
            args.latency_baseline,
            expected_candidate=candidate,
            expected_model_id=args.model_id,
            expected_provider_id=args.provider_id,
            expected_model_config_digest=args.model_config_digest,
            expected_gold_sha256=gold_digest,
        )
        baseline_file_sha256 = _sha256_file(args.latency_baseline)
        target_policy_sha256 = _sha256_file(args.target_policy)
        artifact["provenance"].update(
            {
                "baseline_id": latency_baseline.baseline_id,
                "baseline_payload_sha256": latency_baseline.payload_sha256,
                "baseline_file_sha256": baseline_file_sha256,
                "baseline_provenance_kind": latency_baseline.provenance_kind,
                "target_policy_sha256": target_policy_sha256,
            }
        )
        if latency_baseline.provenance_kind == "bootstrap":
            advisory = (
                "latency gate skipped: bootstrap baseline (self-measured); "
                "valid only for the first release"
            )
            artifact["advisories"].append(advisory)
            logger.warning(advisory)
        artifact["evidence_index"].extend(
            {
                "kind": "protected_lane",
                "path": str(path),
                "sha256": _sha256_file(path),
            }
            for path in args.protected_evidence
        )
        artifact["evidence_index"].append(
            {
                "kind": "latency_baseline",
                "path": str(args.latency_baseline),
                "sha256": baseline_file_sha256,
            }
        )
        artifact["evidence_index"].append(
            {
                "kind": "target_policy",
                "path": str(args.target_policy),
                "sha256": target_policy_sha256,
            }
        )
    except Exception as exc:
        artifact["error"] = f"{type(exc).__name__}: {exc}"
        return _finish(
            args.output,
            artifact,
            exit_code=EXIT_CONTRACT,
            result_code="CONTRACT_OR_COVERAGE_FAILED",
        )

    missing_lanes = sorted(
        lane
        for lane in driver_lanes
        if not lane_evidence.get(lane, {}).get("production_ready", False)
    )
    artifact["missing_protected_lanes"] = missing_lanes
    if missing_lanes:
        return _finish(
            args.output,
            artifact,
            exit_code=EXIT_PROTECTED_LANE,
            result_code="PROTECTED_LANE_UNAVAILABLE",
        )

    try:
        connection_map = _load_connection_map(args.connection_map)
        missing_fixtures = sorted({case.fixture for case in cases} - set(connection_map))
        if missing_fixtures:
            raise ValueError(
                "connection map is missing fixtures: " + ", ".join(missing_fixtures)
            )
        token = os.getenv(args.auth_token_env)
        if not token:
            raise ValueError(
                f"authentication environment {args.auth_token_env} is unavailable"
            )
        client = TextToSqlApiClient(
            base_url=args.api_url,
            auth_headers=lambda: {"Authorization": f"Bearer {token}"},
        )
        adapter = AuthenticatedT13EvalAdapter(client)
        observations = [
            replace(
                adapter.run_case(
                    case,
                    connection_ref=connection_map[case.fixture],
                ),
                repetition=repetition,
            )
            for repetition in range(1, int(policy.thresholds["repetitions"]) + 1)
            for case in cases
        ]
        failures = _set_metrics(
            artifact,
            cases,
            observations,
            policy,
            latency_baseline_p95_ms=latency_baseline.latency_p95_ms,
            latency_gate_mode=latency_baseline.provenance_kind,
        )
        artifact["model_lane_satisfied"] = not failures
    except Exception as exc:
        artifact["error"] = f"{type(exc).__name__}: {exc}"
        return _finish(
            args.output,
            artifact,
            exit_code=EXIT_INFRASTRUCTURE,
            result_code="EVALUATOR_INFRASTRUCTURE_FAILED",
        )

    if not approval_current:
        return _finish(
            args.output,
            artifact,
            exit_code=EXIT_APPROVAL,
            result_code="POLICY_UNAPPROVED",
        )
    if failures:
        return _finish(
            args.output,
            artifact,
            exit_code=EXIT_CONTRACT,
            result_code="THRESHOLDS_FAILED",
        )
    return _finish(
        args.output,
        artifact,
        exit_code=EXIT_ELIGIBLE,
        result_code="RELEASE_ELIGIBLE",
    )


if __name__ == "__main__":
    raise SystemExit(main())
