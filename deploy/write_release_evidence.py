#!/usr/bin/env python3
"""Validate and index cross-bound Text-to-SQL release evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from custom_tools.text_to_sql.eval.observability import canonical_files_digest  # noqa: E402
from custom_tools.text_to_sql.eval.cases import (  # noqa: E402
    load_gold_cases,
    validate_case_coverage,
)
from custom_tools.text_to_sql.eval.metrics import (  # noqa: E402
    ReleaseMetrics,
    compute_release_metrics,
    compute_release_metrics_by_repetition,
    evaluate_release_thresholds,
    normalize_schema_links,
)
from custom_tools.text_to_sql.eval.release import (  # noqa: E402
    CandidateIdentity,
    ReleasePolicy,
    load_approved_latency_baseline,
    load_protected_lane_evidence,
)
from custom_tools.text_to_sql.eval.runner import EvalObservation  # noqa: E402
from scripts.migrate_text2sql_state import load_state_schema_manifest  # noqa: E402


logger = logging.getLogger(__name__)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_REFERENCE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
_CANDIDATE_FIELDS = {
    "schema_version",
    "commit",
    "dirty_tree",
    "generated_at",
    "candidate_digest",
    "lock_digest",
    "target_policy_sha256",
    "images",
}
_IMAGE_FIELDS = {
    "component",
    "oci_digest",
    "runtime_image",
    "sbom_sha256",
    "builds",
    "sboms",
}
_RUNTIME_FIELDS = {
    "schema_version",
    "candidate_digest",
    "backend_image",
    "backend_oci_digest",
    "frontend_image",
    "frontend_oci_digest",
    "readiness_sha256",
    "target_policy_sha256",
}
_READINESS_CHECKS = {
    "event_store",
    "memory_db",
    "result_outbox",
    "config",
    "supervisor",
    "retention",
    "probe_lanes",
}
_EVAL_FIELDS = {
    "schema_version",
    "mode",
    "contract_passed",
    "release_eligible",
    "model_lane_satisfied",
    "provenance",
    "evidence_index",
    "advisories",
    "gold_sha256",
    "policy_sha256",
    "policy_scope_sha256",
    "case_count",
    "all_reviewed_case_count",
    "coverage",
    "metrics",
    "observations",
    "per_repetition_metrics",
    "threshold_failures",
    "approval_status",
    "approval_digest_current",
    "missing_protected_lanes",
    "exit_code",
    "result_code",
}
_EVAL_PROVENANCE_FIELDS = {
    "commit",
    "candidate_digest",
    "lock_digest",
    "model_id",
    "provider_id",
    "model_config_digest",
    "baseline_id",
    "baseline_payload_sha256",
    "baseline_file_sha256",
    "baseline_provenance_kind",
    "target_policy_sha256",
}
_OBSERVATION_FIELDS = {
    "case_id",
    "repetition",
    "run_id",
    "status",
    "reason_code",
    "sql_sha256",
    "schema_links",
    "rows",
    "columns",
    "generated",
    "approved",
    "executed",
    "dry_run",
    "audited",
    "duration_ms",
    "error",
}
_REPORT_FIELDS = {
    "status",
    "reason_code",
    "candidate_ref",
    "schema_ref",
    "backup_ref",
    "rollback_ref",
    "release_eligible",
    "drills",
}
_ROLLBACK_REPORT_FIELDS = _REPORT_FIELDS | {"backup_manifest_sha256"}
_DETERMINISTIC_DRILLS = {
    "adaptive_rollback_operator",
    "supervisor_admission_and_reaping",
    "outbox_crash_and_restart",
    "memory_reconciliation_and_rebuild",
    "retention_and_report_fallback",
    "schema_backup_restore_rollback",
}
_ROLLBACK_DRILLS = {
    "backup_schema_validation",
    "previous_backend_pull",
    "previous_frontend_pull",
    "candidate_stop",
    "state_restore",
    "previous_images_start",
    "healthz",
    "readyz",
}
_TARGET_POLICY_FIELDS = {
    "schema_version",
    "allowed_dialects",
    "allowed_network_targets",
    "allowed_network_cidrs",
    "allowed_file_roots",
}
_BACKUP_MANIFEST_FIELDS = {
    "schema_version",
    "candidate_digest",
    "schema_ref",
    "databases",
}
_BACKUP_DATABASE_FIELDS = {
    "filename",
    "sha256",
    "user_version",
    "integrity_check",
}
_RELEASE_METRIC_FIELDS = set(ReleaseMetrics.__dataclass_fields__)
_BACKUP_DATABASES = {
    "agui_events.db": "event_store",
    "smolagents_memory.db": "memory_db",
    "workflow_result_outbox.db": "result_outbox",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--dirty-tree", required=True, choices=("true", "false"))
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--lock", required=True, action="append", type=Path)
    parser.add_argument("--migration-manifest", required=True, type=Path)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--model-config-digest", required=True)
    parser.add_argument("--latency-baseline", required=True, type=Path)
    parser.add_argument("--target-policy", required=True, type=Path)
    parser.add_argument("--previous-backend-image", required=True)
    parser.add_argument("--previous-frontend-image", required=True)
    parser.add_argument("--backup-reference", required=True, type=Path)
    parser.add_argument("--backup-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise ValueError(f"{label} fields do not match contract")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reference(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "present": path.is_file(),
        "sha256": _sha256_file(path) if path.is_file() else None,
    }


def _combined_lock_digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _fresh_timestamp(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be a timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include timezone")
    age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    if age < timedelta(0) or age > timedelta(hours=24):
        raise ValueError(f"{label} is stale")


def _artifact_file(raw: Any, artifacts: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} path is required")
    path = Path(raw)
    resolved = path.resolve()
    if not resolved.is_relative_to(artifacts.resolve()) or not resolved.is_file():
        raise ValueError(f"{label} must be a file under the artifacts directory")
    return path


def _validate_image(
    component: str,
    value: Any,
    artifacts: Path,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"candidate image {component} must be an object")
    _exact(value, _IMAGE_FIELDS, f"candidate image {component}")
    if value["component"] != component:
        raise ValueError(f"candidate image {component} component mismatch")
    if not isinstance(value["oci_digest"], str) or not _OCI_DIGEST.fullmatch(
        value["oci_digest"]
    ):
        raise ValueError(f"candidate image {component} OCI digest is invalid")
    if not isinstance(value["runtime_image"], str) or not _OCI_DIGEST.fullmatch(
        value["runtime_image"]
    ):
        raise ValueError(f"candidate image {component} runtime image is invalid")
    if not isinstance(value["sbom_sha256"], str) or not _SHA256.fullmatch(
        value["sbom_sha256"]
    ):
        raise ValueError(f"candidate image {component} SBOM digest is invalid")

    builds = value["builds"]
    if not isinstance(builds, list) or len(builds) != 2:
        raise ValueError(f"candidate image {component} requires two builds")
    build_paths: set[Path] = set()
    for index, build in enumerate(builds, start=1):
        if not isinstance(build, Mapping):
            raise ValueError(f"candidate image {component} build must be an object")
        _exact(
            build,
            {"metadata", "oci_digest", "config_digest"},
            f"{component} build",
        )
        if build["oci_digest"] != value["oci_digest"]:
            raise ValueError(f"candidate image {component} build digest mismatch")
        if build["config_digest"] != value["runtime_image"]:
            raise ValueError(f"candidate image {component} config digest mismatch")
        path = _artifact_file(build["metadata"], artifacts, f"{component} build {index}")
        if path in build_paths:
            raise ValueError(f"candidate image {component} build path is duplicated")
        build_paths.add(path)
        metadata = _load_object(path, f"{component} Buildx metadata")
        if (
            metadata.get("containerimage.digest") != value["oci_digest"]
            or metadata.get("containerimage.config.digest")
            != value["runtime_image"]
        ):
            raise ValueError(f"candidate image {component} Buildx identity mismatch")

    sboms = value["sboms"]
    if not isinstance(sboms, list) or len(sboms) != 2:
        raise ValueError(f"candidate image {component} requires two SBOMs")
    sbom_paths: set[Path] = set()
    for index, sbom in enumerate(sboms, start=1):
        if not isinstance(sbom, Mapping):
            raise ValueError(f"candidate image {component} SBOM must be an object")
        _exact(sbom, {"path", "sha256"}, f"{component} SBOM")
        if sbom["sha256"] != value["sbom_sha256"]:
            raise ValueError(f"candidate image {component} SBOM digest mismatch")
        path = _artifact_file(sbom["path"], artifacts, f"{component} SBOM {index}")
        if path in sbom_paths or _sha256_file(path) != value["sbom_sha256"]:
            raise ValueError(f"candidate image {component} SBOM file mismatch")
        sbom_paths.add(path)
        document = _load_object(path, f"{component} CycloneDX SBOM")
        metadata = document.get("metadata")
        sbom_component = metadata.get("component") if isinstance(metadata, Mapping) else None
        if (
            document.get("bomFormat") != "CycloneDX"
            or not isinstance(sbom_component, Mapping)
            or sbom_component.get("version") != value["runtime_image"]
        ):
            raise ValueError(
                f"candidate image {component} SBOM is not bound to runtime image"
            )
    return dict(value)


def _validate_candidate(
    args: argparse.Namespace,
    lock_digest: str,
    target_policy_digest: str,
) -> tuple[CandidateIdentity, dict[str, Any]]:
    path = args.artifacts / "build" / "candidate-metadata.json"
    raw = _load_object(path, "candidate metadata")
    _exact(raw, _CANDIDATE_FIELDS, "candidate metadata")
    if raw["schema_version"] != 1:
        raise ValueError("candidate metadata schema version is invalid")
    if raw["commit"] != args.commit:
        raise ValueError("candidate metadata commit mismatch")
    dirty_tree = args.dirty_tree == "true"
    if type(raw["dirty_tree"]) is not bool or raw["dirty_tree"] is not dirty_tree:
        raise ValueError("candidate metadata dirty-tree mismatch")
    if dirty_tree:
        raise ValueError("candidate source tree is dirty")
    _fresh_timestamp(raw["generated_at"], "candidate generated_at")
    if raw["lock_digest"] != lock_digest:
        raise ValueError("candidate metadata lock digest mismatch")
    if raw["target_policy_sha256"] != target_policy_digest:
        raise ValueError("candidate metadata target policy digest mismatch")
    images = raw["images"]
    if not isinstance(images, Mapping) or set(images) != {"backend", "frontend"}:
        raise ValueError("candidate metadata images do not match contract")
    validated_images = {
        component: _validate_image(component, images[component], args.artifacts)
        for component in ("backend", "frontend")
    }
    material = json.dumps(
        {
            component: validated_images[component]["oci_digest"]
            for component in sorted(validated_images)
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    candidate_digest = hashlib.sha256(material).hexdigest()
    if raw["candidate_digest"] != candidate_digest:
        raise ValueError("candidate metadata aggregate digest mismatch")
    return (
        CandidateIdentity(args.commit, candidate_digest, lock_digest),
        {**raw, "images": validated_images},
    )


def _validate_readiness(
    path: Path,
    manifest_heads: dict[str, int],
    target_policy_digest: str,
    driver_dialects: set[str],
) -> dict[str, Any]:
    raw = _load_object(path, "readiness")
    _exact(
        raw,
        {
            "status",
            "checks",
            "schema_heads",
            "probe_lanes_enabled",
            "target_policy_sha256",
        },
        "readiness",
    )
    if raw["status"] != "ready":
        raise ValueError("readiness status is not ready")
    checks = raw["checks"]
    if not isinstance(checks, Mapping) or set(checks) != _READINESS_CHECKS:
        raise ValueError("readiness checks do not match contract")
    for name, check in checks.items():
        if not isinstance(check, Mapping):
            raise ValueError(f"readiness check {name} must be an object")
        _exact(check, {"status", "reason_code"}, f"readiness check {name}")
        if check["status"] != "ready" or check["reason_code"] != "READY":
            raise ValueError(f"readiness check {name} is not ready")
    if raw["probe_lanes_enabled"] != sorted(driver_dialects):
        raise ValueError("readiness probe lanes do not match protected driver lanes")
    if raw["target_policy_sha256"] != target_policy_digest:
        raise ValueError("readiness target policy digest mismatch")
    heads = raw["schema_heads"]
    if not isinstance(heads, Mapping) or set(heads) != set(manifest_heads):
        raise ValueError("readiness schema heads do not match manifest")
    for name, expected in manifest_heads.items():
        if heads[name] != {"expected": expected, "actual": expected}:
            raise ValueError(f"readiness schema head {name} mismatch")
    return raw


def _validate_runtime(
    path: Path,
    candidate: CandidateIdentity,
    metadata: dict[str, Any],
    readiness_path: Path,
    target_policy_digest: str,
) -> dict[str, Any]:
    raw = _load_object(path, "runtime evidence")
    _exact(raw, _RUNTIME_FIELDS, "runtime evidence")
    expected = {
        "schema_version": 1,
        "candidate_digest": candidate.artifact_digest,
        "backend_image": metadata["images"]["backend"]["runtime_image"],
        "backend_oci_digest": metadata["images"]["backend"]["oci_digest"],
        "frontend_image": metadata["images"]["frontend"]["runtime_image"],
        "frontend_oci_digest": metadata["images"]["frontend"]["oci_digest"],
        "readiness_sha256": _sha256_file(readiness_path),
        "target_policy_sha256": target_policy_digest,
    }
    if raw != expected:
        raise ValueError("runtime evidence does not match exact candidate")
    return raw


def _validate_target_policy(
    path: Path,
    policy: ReleasePolicy,
    driver_dialects: set[str],
) -> tuple[dict[str, Any], str]:
    raw = _load_object(path, "target policy")
    _exact(raw, _TARGET_POLICY_FIELDS, "target policy")
    if raw["schema_version"] != 1:
        raise ValueError("target policy schema version is invalid")
    for field in _TARGET_POLICY_FIELDS - {"schema_version"}:
        values = raw[field]
        if (
            not isinstance(values, list)
            or values != sorted(set(values))
            or not all(
                isinstance(value, str) and value and value == value.strip()
                for value in values
            )
        ):
            raise ValueError(f"target policy {field} is not canonical")
    allowed_dialects = set(raw["allowed_dialects"])
    required_dialects = set(policy.required_dialects)
    if not allowed_dialects or not allowed_dialects <= required_dialects:
        raise ValueError("target policy enables a dialect outside release policy")
    network_dialects = allowed_dialects - {"sqlite", "duckdb"}
    if network_dialects != driver_dialects:
        raise ValueError("target policy network dialects do not match driver lanes")
    if network_dialects and not (
        raw["allowed_network_targets"] or raw["allowed_network_cidrs"]
    ):
        raise ValueError("target policy network dialects require allowed targets")
    if allowed_dialects & {"sqlite", "duckdb"} and not raw["allowed_file_roots"]:
        raise ValueError("target policy file dialects require allowed file roots")
    return raw, _sha256_file(path)


def _validate_metric_mapping(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    _exact(value, _RELEASE_METRIC_FIELDS, label)
    for field in (
        "total",
        "passed",
        "execution_total",
        "contract_and_safety_total",
    ):
        if type(value[field]) is not int or value[field] < 0:
            raise ValueError(f"{label} {field} must be a non-negative integer")
    if value["passed"] > value["total"]:
        raise ValueError(f"{label} passed exceeds total")
    for field in (
        "execution_accuracy_overall",
        "schema_table_recall",
        "schema_column_f1",
        "expected_abstention_precision",
        "expected_abstention_recall",
        "unexpected_error_rate",
        "contract_and_safety_case_pass_rate",
    ):
        metric = value[field]
        if (
            isinstance(metric, bool)
            or not isinstance(metric, (int, float))
            or not math.isfinite(float(metric))
            or not 0 <= float(metric) <= 1
        ):
            raise ValueError(f"{label} {field} must be a finite rate")
    per_slice = value["execution_accuracy_per_slice"]
    if not isinstance(per_slice, Mapping):
        raise ValueError(f"{label} per-slice metrics must be an object")
    for name, metric in per_slice.items():
        if (
            not isinstance(name, str)
            or not name
            or isinstance(metric, bool)
            or not isinstance(metric, (int, float))
            or not math.isfinite(float(metric))
            or not 0 <= float(metric) <= 1
        ):
            raise ValueError(f"{label} has an invalid per-slice metric")
    latency = value["latency_p95_ms"]
    regression = value["latency_p95_regression_fraction"]
    if (
        isinstance(latency, bool)
        or not isinstance(latency, (int, float))
        or not math.isfinite(float(latency))
        or float(latency) < 0
    ):
        raise ValueError(f"{label} latency must be finite and non-negative")
    if (
        isinstance(regression, bool)
        or not isinstance(regression, (int, float))
        or not math.isfinite(float(regression))
        or float(regression) < -1
    ):
        raise ValueError(f"{label} latency regression is invalid")


def _validate_observation(
    raw: Any,
    *,
    case: Any,
    repetition: int,
) -> EvalObservation:
    if not isinstance(raw, Mapping):
        raise ValueError("release evaluation observation must be an object")
    _exact(raw, _OBSERVATION_FIELDS, "release evaluation observation")
    if raw["case_id"] != case.id or raw["repetition"] != repetition:
        raise ValueError("release evaluation observation identity mismatch")
    if (
        not isinstance(raw["run_id"], str)
        or not raw["run_id"].strip()
        or raw["run_id"] != raw["run_id"].strip()
    ):
        raise ValueError("release evaluation run id is invalid")
    if raw["status"] != case.expected_outcome:
        raise ValueError("release evaluation observation status mismatch")
    reason = raw["reason_code"]
    if not isinstance(reason, str) or (
        case.expected_reason_codes
        and reason not in set(case.expected_reason_codes)
    ) or (not case.expected_reason_codes and reason):
        raise ValueError("release evaluation observation reason mismatch")
    if not isinstance(raw["sql_sha256"], str) or not _SHA256.fullmatch(
        raw["sql_sha256"]
    ):
        raise ValueError("release evaluation SQL hash is invalid")
    links = raw["schema_links"]
    if not isinstance(links, Mapping) or set(links) != {"tables", "columns"}:
        raise ValueError("release evaluation schema links are invalid")
    for values in links.values():
        if (
            not isinstance(values, list)
            or values != sorted(set(values))
            or not all(isinstance(value, str) and value for value in values)
        ):
            raise ValueError("release evaluation schema links are not canonical")
    expected_links = normalize_schema_links(case.expected_schema_links)
    canonical_expected_links = {
        "tables": sorted(expected_links["tables"]),
        "columns": sorted(expected_links["columns"]),
    }
    if dict(links) != canonical_expected_links:
        raise ValueError(
            "release evaluation schema links do not match reviewed gold"
        )
    rows = raw["rows"]
    columns = raw["columns"]
    if not isinstance(rows, list) or not (
        columns is None
        or (
            isinstance(columns, list)
            and len(columns) == len(set(columns))
            and all(isinstance(column, str) and column for column in columns)
        )
    ):
        raise ValueError("release evaluation rows or columns are invalid")
    for field in ("generated", "approved", "executed", "dry_run", "audited"):
        if type(raw[field]) is not bool:
            raise ValueError(f"release evaluation {field} must be a boolean")
    duration = raw["duration_ms"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) < 0
    ):
        raise ValueError("release evaluation duration is invalid")
    if raw["error"] is not None and not isinstance(raw["error"], str):
        raise ValueError("release evaluation error must be text or null")
    if case.expected_outcome == "succeeded" and raw["error"] is not None:
        raise ValueError("release evaluation successful observation has an error")
    flags = {
        field: raw[field]
        for field in ("generated", "approved", "executed", "dry_run", "audited")
    }
    expects_dry_run = (
        case.expected_outcome == "succeeded"
        and case.request_options["dry_run_only"]
    )
    if expects_dry_run and (
        flags
        != {
            "generated": True,
            "approved": True,
            "executed": False,
            "dry_run": True,
            "audited": True,
        }
        or rows
        or columns not in (None, [])
    ):
        raise ValueError("release evaluation dry-run observation is invalid")
    controlled_execution_failure = (
        case.expected_outcome == "failed"
        and "EXECUTION_FAILED" in case.expected_reason_codes
    )
    if controlled_execution_failure and (
        flags
        != {
            "generated": True,
            "approved": True,
            "executed": True,
            "dry_run": False,
            "audited": True,
        }
        or rows
        or columns not in (None, [])
        or not isinstance(raw["error"], str)
        or not raw["error"].strip()
    ):
        raise ValueError("release evaluation controlled execution failure is invalid")
    if (
        (case.expected_outcome != "succeeded" or not raw["executed"])
        and not expects_dry_run
        and not controlled_execution_failure
        and (rows or columns not in (None, []))
    ):
        raise ValueError(
            "release evaluation non-success observation contains result data"
        )
    if rows and all(isinstance(row, list) for row in rows):
        if columns is None or any(len(row) != len(columns) for row in rows):
            raise ValueError("release evaluation row width does not match columns")
    elif rows and all(isinstance(row, Mapping) for row in rows):
        if columns is None or any(
            len(row) != len(columns) or set(row) != set(columns)
            for row in rows
        ):
            raise ValueError(
                "release evaluation mapping row columns do not match row keys"
            )
    elif rows:
        raise ValueError("release evaluation rows have mixed shapes")
    elif columns not in (None, []):
        raise ValueError("release evaluation empty rows must not declare columns")
    empty_sql_sha256 = hashlib.sha256(b"").hexdigest()
    if raw["generated"] == (raw["sql_sha256"] == empty_sql_sha256):
        raise ValueError("release evaluation SQL hash contradicts generated flag")
    return EvalObservation(
        case_id=case.id,
        run_id=raw["run_id"],
        status=raw["status"],
        reason_code=reason,
        generated_sql="",
        rows=list(rows),
        columns=list(columns) if columns is not None else None,
        schema_links={key: list(value) for key, value in links.items()},
        duration_ms=float(duration),
        error=raw["error"],
        repetition=repetition,
        generated=raw["generated"],
        approved=raw["approved"],
        executed=raw["executed"],
        dry_run=raw["dry_run"],
        audited=raw["audited"],
    )


def _validate_eval(
    path: Path,
    *,
    args: argparse.Namespace,
    candidate: CandidateIdentity,
    policy: ReleasePolicy,
    policy_digest: str,
    gold_digest: str,
    driver_path: Path,
    baseline_path: Path,
    baseline: Any,
    target_policy_path: Path,
    target_policy_digest: str,
) -> dict[str, Any]:
    raw = _load_object(path, "release evaluation")
    _exact(raw, _EVAL_FIELDS, "release evaluation")
    if (
        raw["schema_version"] != 1
        or raw["mode"] != "release"
        or raw["contract_passed"] is not True
        or raw["model_lane_satisfied"] is not True
        or raw["release_eligible"] is not True
        or raw["exit_code"] != 0
        or raw["result_code"] != "RELEASE_ELIGIBLE"
    ):
        raise ValueError("release evaluation is not eligible")
    if (
        raw["approval_status"] != "approved"
        or raw["approval_digest_current"] is not True
        or raw["threshold_failures"] != []
        or raw["missing_protected_lanes"] != []
    ):
        raise ValueError("release evaluation has unresolved failures")
    provenance = raw["provenance"]
    if not isinstance(provenance, Mapping):
        raise ValueError("release evaluation provenance must be an object")
    _exact(provenance, _EVAL_PROVENANCE_FIELDS, "release evaluation provenance")
    expected_provenance = {
        "commit": candidate.commit,
        "candidate_digest": candidate.artifact_digest,
        "lock_digest": candidate.lock_digest,
        "model_id": args.model_id,
        "provider_id": args.provider_id,
        "model_config_digest": args.model_config_digest,
        "baseline_id": baseline.baseline_id,
        "baseline_payload_sha256": baseline.payload_sha256,
        "baseline_file_sha256": _sha256_file(baseline_path),
        "baseline_provenance_kind": baseline.provenance_kind,
        "target_policy_sha256": target_policy_digest,
    }
    if provenance != expected_provenance:
        raise ValueError("release evaluation provenance mismatch")
    advisories = raw["advisories"]
    if not isinstance(advisories, list) or not all(
        isinstance(item, str) and item for item in advisories
    ):
        raise ValueError("release evaluation advisories must be a list of text")
    if baseline.provenance_kind == "bootstrap":
        logger.warning(
            "release evidence: latency gate skipped for candidate %s "
            "(bootstrap baseline, self-measured); valid only for the first release",
            candidate.commit,
        )
    if (
        raw["gold_sha256"] != gold_digest
        or raw["policy_sha256"] != policy_digest
        or raw["policy_scope_sha256"] != policy.scope_digest
    ):
        raise ValueError("release evaluation policy or gold digest mismatch")

    expected_evidence = {
        ("gold", str(args.gold)): gold_digest,
        ("policy", str(args.policy)): policy_digest,
        ("protected_lane", str(driver_path)): _sha256_file(driver_path),
        ("latency_baseline", str(baseline_path)): _sha256_file(baseline_path),
        ("target_policy", str(target_policy_path)): target_policy_digest,
    }
    evidence = raw["evidence_index"]
    if not isinstance(evidence, list) or len(evidence) != len(expected_evidence):
        raise ValueError("release evaluation evidence index mismatch")
    indexed: dict[tuple[str, str], str] = {}
    for entry in evidence:
        if not isinstance(entry, Mapping):
            raise ValueError("release evaluation evidence entry must be an object")
        _exact(entry, {"kind", "path", "sha256"}, "release evidence entry")
        key = (entry["kind"], entry["path"])
        if key in indexed:
            raise ValueError("release evaluation evidence entry is duplicated")
        indexed[key] = entry["sha256"]
    if indexed != expected_evidence:
        raise ValueError("release evaluation evidence digests mismatch")

    all_cases = load_gold_cases(args.gold)
    cases = [case for case in all_cases if case.profile == "release"]
    if not cases:
        raise ValueError("release evaluation has no release-profile gold cases")
    coverage = validate_case_coverage(
        cases,
        required_slices=policy.required_slices,
        required_dialects=policy.required_dialects,
    )
    expected_coverage = {
        "slices": list(coverage.slices),
        "dialects": list(coverage.dialects),
        "missing_slices": list(coverage.missing_slices),
        "missing_dialects": list(coverage.missing_dialects),
    }
    if not coverage.complete or raw["coverage"] != expected_coverage:
        raise ValueError("release evaluation coverage mismatch")
    if (
        raw["case_count"] != len(cases)
        or type(raw["case_count"]) is not int
        or raw["all_reviewed_case_count"] != len(all_cases)
        or type(raw["all_reviewed_case_count"]) is not int
    ):
        raise ValueError("release evaluation gold case count mismatch")

    repetitions = int(policy.thresholds["repetitions"])
    expected_repetitions = {str(value) for value in range(1, repetitions + 1)}
    per_repetition = raw["per_repetition_metrics"]
    if not isinstance(per_repetition, Mapping) or set(per_repetition) != expected_repetitions:
        raise ValueError("release evaluation repetitions mismatch")
    serialized_observations = raw["observations"]
    if not isinstance(serialized_observations, list):
        raise ValueError("release evaluation observation count is invalid")
    if len(serialized_observations) != len(cases) * repetitions:
        raise ValueError("release evaluation observation count mismatch")
    indexed: dict[tuple[str, int], Any] = {}
    run_ids: set[str] = set()
    for observation in serialized_observations:
        if not isinstance(observation, Mapping):
            raise ValueError("release evaluation observation must be an object")
        key = (observation.get("case_id"), observation.get("repetition"))
        if key in indexed:
            raise ValueError("release evaluation observation is duplicated")
        indexed[key] = observation
    observations = []
    for repetition in range(1, repetitions + 1):
        for case in cases:
            key = (case.id, repetition)
            if key not in indexed:
                raise ValueError("release evaluation observation coverage mismatch")
            observation = _validate_observation(
                indexed[key],
                case=case,
                repetition=repetition,
            )
            if observation.run_id in run_ids:
                raise ValueError("release evaluation run id is duplicated")
            run_ids.add(observation.run_id)
            observations.append(observation)
    _validate_metric_mapping(raw["metrics"], "release evaluation metrics")
    recomputed = compute_release_metrics(
        cases,
        observations,
        latency_baseline_p95_ms=baseline.latency_p95_ms,
    )
    if raw["metrics"] != recomputed.to_mapping():
        raise ValueError("release evaluation aggregate metrics mismatch")
    recomputed_repetitions = compute_release_metrics_by_repetition(
        cases,
        observations,
        expected_repetitions=repetitions,
        latency_baseline_p95_ms=baseline.latency_p95_ms,
    )
    expected_per_repetition = {
        str(repetition): metrics.to_mapping()
        for repetition, metrics in recomputed_repetitions.items()
    }
    for repetition, metrics in expected_per_repetition.items():
        _validate_metric_mapping(
            raw["per_repetition_metrics"][repetition],
            f"release evaluation repetition {repetition} metrics",
        )
    if raw["per_repetition_metrics"] != expected_per_repetition:
        raise ValueError("release evaluation per-repetition metrics mismatch")
    recomputed_failures = [
        f"repetition:{repetition}:{failure}"
        for repetition, metrics in recomputed_repetitions.items()
        for failure in evaluate_release_thresholds(
            metrics, policy.thresholds, latency_gate_mode=baseline.provenance_kind
        )
    ]
    if recomputed_failures or evaluate_release_thresholds(
        recomputed,
        policy.thresholds,
        latency_gate_mode=baseline.provenance_kind,
    ):
        raise ValueError("release evaluation recomputed thresholds failed")
    return raw


def _validate_drills(
    path: Path,
    *,
    expected_reason: str,
    expected_names: set[str],
    candidate_digest: str,
    schema_ref: str,
    backup_ref: Any,
    rollback_ref: Any,
    backup_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    raw = _load_object(path, "drill report")
    _exact(
        raw,
        _ROLLBACK_REPORT_FIELDS if backup_manifest_sha256 else _REPORT_FIELDS,
        "drill report",
    )
    expected_scalars = {
        "status": "passed",
        "reason_code": expected_reason,
        "candidate_ref": candidate_digest,
        "schema_ref": schema_ref,
        "backup_ref": backup_ref,
        "rollback_ref": rollback_ref,
        "release_eligible": False,
    }
    if any(raw[key] != value for key, value in expected_scalars.items()):
        raise ValueError("drill report identity or status mismatch")
    if (
        backup_manifest_sha256 is not None
        and raw["backup_manifest_sha256"] != backup_manifest_sha256
    ):
        raise ValueError("drill report backup manifest digest mismatch")
    drills = raw["drills"]
    if not isinstance(drills, list) or len(drills) != len(expected_names):
        raise ValueError("drill report steps mismatch")
    indexed: dict[str, str] = {}
    for drill in drills:
        if not isinstance(drill, Mapping):
            raise ValueError("drill step must be an object")
        _exact(drill, {"name", "status"}, "drill step")
        if drill["name"] in indexed:
            raise ValueError("drill step is duplicated")
        indexed[drill["name"]] = drill["status"]
    if set(indexed) != expected_names or set(indexed.values()) != {"passed"}:
        raise ValueError("drill report has missing or failed steps")
    return raw


def _validate_backup(
    path: Path,
    backup_manifest_path: Path,
    manifest_heads: dict[str, int],
    *,
    candidate_digest: str,
    schema_ref: str,
) -> tuple[dict[str, Any], str]:
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("backup reference must be an existing local directory")
    manifest = _load_object(backup_manifest_path, "backup manifest")
    _exact(manifest, _BACKUP_MANIFEST_FIELDS, "backup manifest")
    if (
        manifest["schema_version"] != 1
        or manifest["candidate_digest"] != candidate_digest
        or manifest["schema_ref"] != schema_ref
    ):
        raise ValueError("backup manifest identity mismatch")
    entries = manifest["databases"]
    if not isinstance(entries, list) or len(entries) != len(_BACKUP_DATABASES):
        raise ValueError("backup manifest databases do not match contract")
    indexed: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("backup manifest database must be an object")
        _exact(entry, _BACKUP_DATABASE_FIELDS, "backup manifest database")
        filename = entry["filename"]
        if not isinstance(filename, str) or filename in indexed:
            raise ValueError("backup manifest database filename is invalid")
        indexed[filename] = entry
    if set(indexed) != set(_BACKUP_DATABASES):
        raise ValueError("backup manifest database set mismatch")
    for filename, store in _BACKUP_DATABASES.items():
        database = path / filename
        if not database.is_file():
            raise ValueError(f"backup database {filename} is missing")
        entry = indexed[filename]
        if (
            entry["sha256"] != _sha256_file(database)
            or not isinstance(entry["sha256"], str)
            or not _SHA256.fullmatch(entry["sha256"])
            or entry["integrity_check"] != "ok"
        ):
            raise ValueError(f"backup database {filename} digest mismatch")
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            head = int(connection.execute("PRAGMA user_version").fetchone()[0])
            integrity = [
                str(row[0])
                for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
        finally:
            connection.close()
        if (
            type(entry["user_version"]) is not int
            or entry["user_version"] != head
            or head != manifest_heads[store]
            or integrity != ["ok"]
        ):
            raise ValueError(f"backup database {filename} schema head mismatch")
    return manifest, _sha256_file(backup_manifest_path)


def _validate(args: argparse.Namespace) -> dict[str, Any]:
    if len(args.lock) != 3 or len(set(args.lock)) != 3:
        raise ValueError("exactly three distinct lock files are required")
    if not _SHA256.fullmatch(args.model_config_digest):
        raise ValueError("model config digest must be a lowercase SHA-256")
    config_path = Path("config/text_to_sql/llm_models.yaml")
    if _sha256_file(config_path) != args.model_config_digest:
        raise ValueError("model config digest does not match the candidate file")
    if not _IMAGE_REFERENCE.fullmatch(args.previous_backend_image):
        raise ValueError("previous backend image must be digest-qualified")
    if not _IMAGE_REFERENCE.fullmatch(args.previous_frontend_image):
        raise ValueError("previous frontend image must be digest-qualified")

    policy = ReleasePolicy.load(args.policy)
    if not policy.approval_is_current:
        raise ValueError("release policy approval is not current")
    policy_digest = _sha256_file(args.policy)
    gold_digest = canonical_files_digest(args.gold)
    lock_digest = _combined_lock_digest(args.lock)

    configured_model_lanes = {
        lane for lane in policy.required_protected_lanes if lane.startswith("model:")
    }
    if configured_model_lanes != {f"model:{args.model_id}"}:
        raise ValueError("model id does not match the release policy")
    driver_lanes = {
        lane for lane in policy.required_protected_lanes if lane.startswith("driver:")
    }
    if driver_lanes | configured_model_lanes != set(policy.required_protected_lanes):
        raise ValueError("release policy contains unsupported protected lanes")
    driver_dialects = {lane.split(":", 1)[1] for lane in driver_lanes}
    target_policy_path = _artifact_file(
        str(args.target_policy),
        args.artifacts,
        "target policy",
    )
    target_policy, target_policy_digest = _validate_target_policy(
        target_policy_path,
        policy,
        driver_dialects,
    )
    candidate, metadata = _validate_candidate(
        args,
        lock_digest,
        target_policy_digest,
    )

    driver_path = args.artifacts / "protected" / "real-driver-evidence.json"
    indexed_lanes = load_protected_lane_evidence(
        [driver_path],
        expected_candidate=candidate,
        required_driver_lanes=driver_lanes,
    )
    if set(indexed_lanes) != driver_lanes or not all(
        lane["production_ready"] is True for lane in indexed_lanes.values()
    ):
        raise ValueError("required real-driver evidence is incomplete")

    baseline = load_approved_latency_baseline(
        args.latency_baseline,
        expected_candidate=candidate,
        expected_model_id=args.model_id,
        expected_provider_id=args.provider_id,
        expected_model_config_digest=args.model_config_digest,
        expected_gold_sha256=gold_digest,
    )
    manifest_heads = load_state_schema_manifest(args.migration_manifest)
    readiness_path = args.artifacts / "runtime" / "readiness.json"
    readiness = _validate_readiness(
        readiness_path,
        manifest_heads,
        target_policy_digest,
        driver_dialects,
    )
    runtime_path = args.artifacts / "runtime" / "candidate-runtime.json"
    runtime = _validate_runtime(
        runtime_path,
        candidate,
        metadata,
        readiness_path,
        target_policy_digest,
    )
    eval_path = args.artifacts / "text2sql-eval-release.json"
    evaluation = _validate_eval(
        eval_path,
        args=args,
        candidate=candidate,
        policy=policy,
        policy_digest=policy_digest,
        gold_digest=gold_digest,
        driver_path=driver_path,
        baseline_path=args.latency_baseline,
        baseline=baseline,
        target_policy_path=target_policy_path,
        target_policy_digest=target_policy_digest,
    )

    schema_ref = "sha256:" + _sha256_file(args.migration_manifest)
    deterministic_path = args.artifacts / "deterministic-drills.json"
    deterministic = _validate_drills(
        deterministic_path,
        expected_reason="DRILL_OK",
        expected_names=_DETERMINISTIC_DRILLS,
        candidate_digest=candidate.artifact_digest,
        schema_ref=schema_ref,
        backup_ref="disposable-local",
        rollback_ref="not-executed-local",
    )
    backup_manifest_path = _artifact_file(
        str(args.backup_manifest),
        args.artifacts,
        "backup manifest",
    )
    backup_manifest, backup_manifest_digest = _validate_backup(
        args.backup_reference,
        backup_manifest_path,
        manifest_heads,
        candidate_digest=candidate.artifact_digest,
        schema_ref=schema_ref,
    )
    rollback_path = args.artifacts / "protected-rollback.json"
    rollback = _validate_drills(
        rollback_path,
        expected_reason="PROTECTED_ROLLBACK_OK",
        expected_names=_ROLLBACK_DRILLS,
        candidate_digest=candidate.artifact_digest,
        schema_ref=schema_ref,
        backup_ref=str(args.backup_reference),
        rollback_ref={
            "backend_image": args.previous_backend_image,
            "frontend_image": args.previous_frontend_image,
        },
        backup_manifest_sha256=backup_manifest_digest,
    )
    return {
        "baseline": baseline,
        "backup_manifest": backup_manifest,
        "backup_manifest_digest": backup_manifest_digest,
        "candidate": candidate,
        "candidate_metadata": metadata,
        "deterministic": deterministic,
        "driver_path": driver_path,
        "evaluation": evaluation,
        "eval_path": eval_path,
        "gold_digest": gold_digest,
        "lock_digest": lock_digest,
        "manifest_heads": manifest_heads,
        "policy": policy,
        "policy_digest": policy_digest,
        "readiness": readiness,
        "readiness_path": readiness_path,
        "rollback": rollback,
        "rollback_path": rollback_path,
        "runtime": runtime,
        "runtime_path": runtime_path,
        "target_policy": target_policy,
        "target_policy_digest": target_policy_digest,
        "target_policy_path": target_policy_path,
    }


def main() -> int:
    args = _parse_args()
    validation_errors: list[str] = []
    validated: dict[str, Any] | None = None
    try:
        validated = _validate(args)
    except Exception as exc:
        validation_errors.append(f"{type(exc).__name__}: {exc}")

    candidate_path = args.artifacts / "build" / "candidate-metadata.json"
    driver_path = args.artifacts / "protected" / "real-driver-evidence.json"
    readiness_path = args.artifacts / "runtime" / "readiness.json"
    runtime_path = args.artifacts / "runtime" / "candidate-runtime.json"
    eval_path = args.artifacts / "text2sql-eval-release.json"
    deterministic_path = args.artifacts / "deterministic-drills.json"
    rollback_path = args.artifacts / "protected-rollback.json"
    eligible = validated is not None and not validation_errors
    metadata = validated["candidate_metadata"] if validated else None
    index = {
        "schema_version": 1,
        "commit": args.commit,
        "dirty_tree": args.dirty_tree == "true",
        "release_eligible": eligible,
        "validation_errors": validation_errors,
        "policy": _reference(args.policy),
        "gold": {
            "path": str(args.gold),
            "present": args.gold.is_dir(),
            "sha256": (
                validated["gold_digest"]
                if validated
                else None
            ),
        },
        "locks": [_reference(path) for path in args.lock],
        "lock_digest": validated["lock_digest"] if validated else None,
        "candidate": _reference(candidate_path),
        "candidate_digest": (
            validated["candidate"].artifact_digest if validated else None
        ),
        "oci_images": metadata["images"] if metadata else None,
        "protected_lanes": {"real_drivers": _reference(driver_path)},
        "latency_baseline": {
            **_reference(args.latency_baseline),
            "provenance_kind": (
                validated["baseline"].provenance_kind if validated else None
            ),
        },
        "target_policy": {
            **_reference(args.target_policy),
            "sha256": (
                validated["target_policy_digest"] if validated else None
            ),
        },
        "migration_heads": {
            "manifest": _reference(args.migration_manifest),
            "heads": validated["manifest_heads"] if validated else None,
            "readiness": _reference(readiness_path),
        },
        "runtime": _reference(runtime_path),
        "evaluation": _reference(eval_path),
        "deterministic_drills": _reference(deterministic_path),
        "rollback": {
            "previous_backend_image": args.previous_backend_image,
            "previous_frontend_image": args.previous_frontend_image,
            "backup_reference": str(args.backup_reference),
            "backup_manifest": _reference(args.backup_manifest),
            "backup_manifest_sha256": (
                validated["backup_manifest_digest"] if validated else None
            ),
            "report": _reference(rollback_path),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(index, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0 if eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
