"""Canonical public benchmark artifact construction and validation."""

from __future__ import annotations

import argparse
from datetime import timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

import yaml

from . import public_benchmark_release as release_support
from .release_diagnostics import (
    DiagnosticArtifactError,
    validate_continue_decision,
    write_json_new_or_identical_sealed,
)
from .sandbox import (
    SandboxError,
    SourceSnapshot,
    source_snapshot_manifest,
    validate_execution_mode,
)
from custom_tools.text_to_sql.adaptive.policy import AdaptivePolicyConfig
from scripts import text2sql_public_benchmark as runner

CANONICAL_CASE_COUNTS = runner.CANONICAL_CASE_COUNTS
_files_digest = runner._files_digest
_json_digest = runner._json_digest
_ordered_canonical_cases = runner._ordered_canonical_cases
_sha256 = runner._sha256
_source_paths = runner._source_paths
_stable_case_manifest = runner._stable_case_manifest
_sandbox_runtime_env = runner._sandbox_runtime_env

def _canonical_run_scope(args: argparse.Namespace, *, case_count: int) -> str:
    if args.seed is None:
        raise ValueError("diagnostic bwrap mode requires --seed")
    subset_requested = bool(
        args.limit is not None
        or args.case_id
        or args.ordinal_start is not None
        or args.ordinal_stop is not None
    )
    if subset_requested and not args.diagnostic_subset:
        raise ValueError("bwrap subset selectors require --diagnostic-subset")
    if args.diagnostic_subset:
        return "diagnostic_subset"
    expected_count = CANONICAL_CASE_COUNTS[args.dataset]
    if case_count != expected_count:
        raise SandboxError(
            f"diagnostic full-dataset {args.dataset} case count must be "
            f"{expected_count}, got {case_count}"
        )
    return "diagnostic_full_dataset"


def _create_canonical_output_dir(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise SandboxError("canonical output directory must be new")
    output_dir = path.resolve()
    if output_dir.exists():
        raise SandboxError("canonical output directory must be new")
    output_dir.mkdir(parents=True)
    return output_dir


def _write_case_manifest(
    path: Path,
    *,
    cases: Sequence[runner.BenchmarkCase],
    benchmark: str,
    repeat_ordinal: int,
    bundle_id: str,
    seed: int,
    run_scope: str,
    execution_mode: str = "diagnostic_noncanonical",
    expected_locked_manifest: Mapping[str, object] | None = None,
    expected_database_digests: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    ordered_cases = sorted(cases, key=lambda case: (case.ordinal, case.case_key))
    actual_database_digests = {
        case.case_key: _sha256(case.database_path) for case in ordered_cases
    }
    if expected_locked_manifest is not None:
        if expected_database_digests is None:
            raise SandboxError("canonical release database digests are missing")
        if _stable_case_manifest(benchmark, ordered_cases) != expected_locked_manifest:
            raise SandboxError("canonical case manifest differs from release lock")
        database_digests = {
            str(case_key): str(digest)
            for case_key, digest in expected_database_digests.items()
        }
        if actual_database_digests != database_digests:
            raise SandboxError("canonical database input differs from release lock")
        locked_rows = expected_locked_manifest.get("cases")
        if not isinstance(locked_rows, list):
            raise SandboxError("canonical release sidecar identities are invalid")
        locked_sidecars = {
            row["case_key"]: row["schema_description_sidecar"]
            for row in locked_rows
            if isinstance(row, Mapping)
            and isinstance(row.get("case_key"), str)
            and "schema_description_sidecar" in row
        }
        if set(locked_sidecars) != set(actual_database_digests):
            raise SandboxError("canonical release sidecar identities are invalid")
    else:
        database_digests = actual_database_digests
        locked_sidecars = None
    payload = {
        "schema_version": 1,
        "record_kind": "text2sql_case_manifest",
        "bundle_id": bundle_id,
        "benchmark": benchmark,
        "repeat_ordinal": repeat_ordinal,
        "execution_mode": execution_mode,
        "seed": seed,
        "run_scope": run_scope,
        "cases": [
            {
                "ordinal": case.ordinal,
                "case_key": case.case_key,
                "case_id": case.case_id,
                "database_id": case.database_id,
                "question_sha256": hashlib.sha256(
                    case.question.encode("utf-8")
                ).hexdigest(),
                "external_knowledge_sha256": hashlib.sha256(
                    case.external_knowledge.encode("utf-8")
                ).hexdigest(),
                "prompt_sha256": hashlib.sha256(
                    case.prompt().encode("utf-8")
                ).hexdigest(),
                "database_sha256": database_digests[case.case_key],
                "schema_description_sidecar": (
                    locked_sidecars[case.case_key]
                    if locked_sidecars is not None
                    else release_support.release_inputs.schema_description_sidecar_identity(
                        case
                    )
                ),
            }
            for case in ordered_cases
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return f"sha256:{_sha256(path)}", database_digests


def _write_empty_history_evidence(
    path: Path,
    *,
    receipts: Sequence[Mapping[str, object]],
    args: argparse.Namespace,
    bundle_id: str,
    snapshot_digest: str,
    configuration_digest: str,
    run_scope: str,
) -> None:
    _write_json_atomically(
        path,
        _empty_history_evidence_payload(
            receipts=receipts,
            args=args,
            bundle_id=bundle_id,
            snapshot_digest=snapshot_digest,
            configuration_digest=configuration_digest,
            run_scope=run_scope,
        ),
    )


def _empty_history_evidence_payload(
    *,
    receipts: Sequence[Mapping[str, object]],
    args: argparse.Namespace,
    bundle_id: str,
    snapshot_digest: str,
    configuration_digest: str,
    run_scope: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_kind": "text2sql_empty_history_evidence",
        "bundle_id": bundle_id,
        "benchmark": args.dataset,
        "repeat_ordinal": args.repeat_ordinal,
        "execution_mode": (
            "canonical_release"
            if getattr(args, "canonical_release_leg", False)
            else "diagnostic_noncanonical"
        ),
        "run_scope": run_scope,
        "seed": args.seed,
        "source_snapshot_digest": snapshot_digest,
        "configuration_digest": configuration_digest,
        "receipts": list(receipts),
    }


def _empty_history_evidence_bytes(
    *,
    receipts: Sequence[Mapping[str, object]],
    args: argparse.Namespace,
    bundle_id: str,
    snapshot_digest: str,
    configuration_digest: str,
    run_scope: str,
) -> bytes:
    payload = _empty_history_evidence_payload(
        receipts=receipts,
        args=args,
        bundle_id=bundle_id,
        snapshot_digest=snapshot_digest,
        configuration_digest=configuration_digest,
        run_scope=run_scope,
    )
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_json_atomically(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _write_source_snapshot_artifact(path: Path, snapshot: SourceSnapshot) -> str:
    _write_json_atomically(path, source_snapshot_manifest(snapshot))
    path.chmod(0o444)
    return f"sha256:{_sha256(path)}"


_write_bundle_state = release_support.write_bundle_state


def _validate_release_resume(
    output_dir: Path,
    *,
    state_root: Path,
    identity: Mapping[str, object],
    release_plan: Sequence[Mapping[str, object]],
    locked_case_manifests: Mapping[str, object],
    allow_active_leg: bool = False,
) -> set[tuple[str, int]]:
    return release_support.validate_release_resume(
        output_dir,
        state_root=state_root,
        identity=identity,
        release_plan=release_plan,
        locked_case_manifests=locked_case_manifests,
        allow_active_leg=allow_active_leg,
    )


def _write_artifact_handshake(
    output_dir: Path,
    *,
    benchmark: str,
    repeat_ordinal: int,
    bundle_id: str,
    case_manifest_digest: str,
    snapshot_digest: str,
    configuration_digest: str,
    seed: int,
    run_scope: str,
    execution_mode: str = "diagnostic_noncanonical",
    source_snapshot_manifest_digest: str | None = None,
    governance_events: Sequence[Mapping[str, object]] = (),
    additional_artifact_names: Sequence[str] = (),
) -> str:
    artifacts = {
        name: f"sha256:{_sha256(output_dir / name)}"
        for name in (
            "manifest.json",
            "case_manifest.json",
            "observations.jsonl",
            "empty_history_evidence.json",
        )
    }
    snapshot_manifest_path = output_dir / "source_snapshot_manifest.json"
    if snapshot_manifest_path.is_file() and not snapshot_manifest_path.is_symlink():
        artifacts["source_snapshot_manifest.json"] = (
            f"sha256:{_sha256(snapshot_manifest_path)}"
        )
    for name in additional_artifact_names:
        path = output_dir / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_mode & 0o777 != 0o444
        ):
            raise SandboxError("additional release artifact is missing or unsealed")
        artifacts[name] = f"sha256:{_sha256(path)}"
    seen_events: set[tuple[str, int]] = set()
    for raw_event in governance_events:
        event_kind = raw_event.get("event_kind")
        completed_case_count = raw_event.get("completed_case_count")
        if (
            not isinstance(event_kind, str)
            or not isinstance(completed_case_count, int)
            or isinstance(completed_case_count, bool)
            or (event_kind, completed_case_count) in seen_events
        ):
            raise SandboxError("release governance event inventory is invalid")
        seen_events.add((event_kind, completed_case_count))
        try:
            event = validate_continue_decision(
                output_dir,
                event_kind=event_kind,
                completed_case_count=completed_case_count,
                expected={str(name): value for name, value in raw_event.items()},
            )
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        if (
            event.get("benchmark") != benchmark
            or event.get("repeat_ordinal") != repeat_ordinal
        ):
            raise SandboxError("release governance event leg identity mismatch")
        for path_field, digest_field in (
            ("candidate_path", "candidate_sha256"),
            ("decision_path", "decision_sha256"),
            ("result_path", "result_sha256"),
        ):
            artifacts[str(event[path_field])] = str(event[digest_field])
    payload = {
        "schema_version": 1,
        "record_kind": "text2sql_benchmark_artifact_handshake",
        "bundle_id": bundle_id,
        "benchmark": benchmark,
        "repeat_ordinal": repeat_ordinal,
        "execution_mode": execution_mode,
        "seed": seed,
        "run_scope": run_scope,
        "case_manifest_digest": case_manifest_digest,
        "source_snapshot_digest": snapshot_digest,
        "configuration_digest": configuration_digest,
        "artifacts": artifacts,
    }
    if source_snapshot_manifest_digest is not None:
        payload["source_snapshot_manifest_digest"] = source_snapshot_manifest_digest
    handshake_path = output_dir / "artifact_handshake.json"
    if execution_mode == "canonical_release":
        return write_json_new_or_identical_sealed(
            handshake_path,
            payload,
            label="release leg artifact handshake",
        )
    handshake_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return f"sha256:{_sha256(handshake_path)}"


def _failed_sandbox_observation(
    case: runner.BenchmarkCase,
    benchmark_name: str,
    error: Exception,
    *,
    repeat_ordinal: int,
    bundle_id: str,
    snapshot_digest: str,
    configuration_digest: str,
    seed: int,
    run_scope: str,
    execution_mode: str = "diagnostic_noncanonical",
) -> dict[str, Any]:
    now = runner.datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "artifact_contract_version": 1,
        "bundle_id": bundle_id,
        "benchmark": benchmark_name,
        "repeat_ordinal": repeat_ordinal,
        "execution_mode": execution_mode,
        "run_scope": run_scope,
        "seed": seed,
        "ordinal": case.ordinal,
        "case_key": case.case_key,
        "case_id": case.case_id,
        "database_id": case.database_id,
        "state_namespace": f"{benchmark_name}:{repeat_ordinal}:{case.case_key}",
        "preexisting_history_items": None,
        "empty_history_receipt_digest": None,
        "prompt_sha256": hashlib.sha256(case.prompt().encode("utf-8")).hexdigest(),
        "run_id": None,
        "workflow_status": None,
        "observation_status": "runner_error",
        "runner_error": f"{type(error).__name__}: {error}",
        "started_at": now,
        "finished_at": now,
        "elapsed_seconds": 0.0,
        "outcome": None,
        "source_snapshot_digest": snapshot_digest,
        "configuration_digest": configuration_digest,
        "runtime_evidence": {
            "schema_version": 1,
            "status": "unavailable",
            "reason": "sandbox_case_failed_before_evidence_collection",
        },
    }


def _write_manifest(
    *,
    args: argparse.Namespace,
    cases: Sequence[runner.BenchmarkCase],
    output_dir: Path,
    principal: Mapping[str, Any],
    completed_before: int,
    source_snapshot_digest: str | None = None,
    state_root: Path | None = None,
    bundle_id: str | None = None,
    case_manifest_digest: str | None = None,
    configuration_digest: str | None = None,
    configuration_sources: Sequence[Mapping[str, object]] | None = None,
    seed: int | None = None,
    run_scope: str | None = None,
    execution_mode: str | None = None,
    source_snapshot_manifest_digest: str | None = None,
    release_identity: Mapping[str, object] | None = None,
    manifest_profile: str,
    source_snapshot: SourceSnapshot | None = None,
) -> None:
    source_paths = _source_paths(args)
    runtime_environment = _sandbox_runtime_env(args)
    frozen_configuration_sources = (
        list(configuration_sources)
        if configuration_sources is not None
        else runner._configuration_sources()
    )
    if manifest_profile == "remote_diagnostic_v1":
        if source_snapshot is not None:
            raise SandboxError("remote diagnostic manifest cannot use a source snapshot")
        schema_version = 1
        execution_policy = None
    elif manifest_profile == "bwrap_v2":
        if type(source_snapshot) is not SourceSnapshot:
            raise SandboxError("bwrap manifest requires an explicit source snapshot")
        schema_version = 2
        execution_policy = _execution_policy_from_snapshot(source_snapshot, args)
    else:
        raise SandboxError("manifest profile is invalid")
    manifest = {
        "schema_version": schema_version,
        "created_at": runner.datetime.now(timezone.utc).isoformat(),
        "benchmark": args.dataset,
        "case_count": len(cases),
        "completed_before": completed_before,
        "repo_revision": runner._git_revision(),
        "pipeline_revision": args.pipeline_revision,
        "base_url": args.base_url,
        "repeat_ordinal": getattr(args, "repeat_ordinal", 1),
        "workers": args.workers,
        "case_timeout": args.case_timeout,
        "max_rows": args.max_rows,
        "model_configuration": {
            "reported_by_runtime": False,
            "note": (
                "The API does not report resolved model IDs, sampling "
                "parameters, or seeds for every pipeline stage."
            ),
        },
        "configuration_sources": frozen_configuration_sources,
        "successful_sql_memory_enabled": runtime_environment.get(
            "TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED",
            os.getenv("TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED", "1"),
        ),
        "clarifying_questions_enabled": runtime_environment.get(
            "TEXT_TO_SQL_CLARIFYING_QUESTIONS",
            os.getenv("TEXT_TO_SQL_CLARIFYING_QUESTIONS", "1"),
        ),
        "principal": {
            "subject": principal.get("subject"),
            "tenant_id": principal.get("tenant_id"),
            "roles": principal.get("roles"),
        },
        "sources": [
            {
                "path": str(path),
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
            for path in source_paths
        ],
    }
    if execution_policy is not None:
        manifest["artifact_contract_version"] = 1
        manifest["execution_mode"] = execution_mode or validate_execution_mode(
            getattr(args, "execution_mode", "remote")
        )
        manifest["execution_policy"] = execution_policy
    if source_snapshot_digest is not None:
        manifest["source_snapshot_digest"] = source_snapshot_digest
    if state_root is not None:
        manifest["state_root"] = str(state_root.resolve())
    if bundle_id is not None:
        manifest["artifact_contract_version"] = 1
        manifest["bundle_id"] = bundle_id
        manifest["history_mode"] = "empty_per_case"
    if case_manifest_digest is not None:
        manifest["case_manifest_digest"] = case_manifest_digest
    if configuration_digest is not None:
        manifest["configuration_digest"] = configuration_digest
    if seed is not None:
        manifest["seed"] = seed
    if run_scope is not None:
        manifest["run_scope"] = run_scope
    if source_snapshot_manifest_digest is not None:
        manifest["source_snapshot_manifest_digest"] = source_snapshot_manifest_digest
    if release_identity is not None:
        manifest["release_identity"] = dict(release_identity)
    if getattr(args, "canonical_release_leg", False):
        manifest["canonical_environment"] = _sandbox_runtime_env(args)
        manifest["model_identity"] = getattr(args, "release_model_identity", None)
        manifest["evaluator_identity"] = getattr(
            args, "release_evaluator_identity", None
        )
    target = output_dir / "manifest.json"
    _write_json_atomically(target, manifest)


def _execution_policy_from_snapshot(
    snapshot: SourceSnapshot,
    args: argparse.Namespace,
) -> dict[str, object]:
    outer_case_deadline = getattr(args, "case_timeout", None)
    workers = getattr(args, "workers", None)
    if (
        isinstance(outer_case_deadline, bool)
        or not isinstance(outer_case_deadline, (int, float))
        or not math.isfinite(outer_case_deadline)
        or outer_case_deadline <= 0
    ):
        raise SandboxError("bwrap case deadline is invalid")
    if type(workers) is not int or workers < 1:
        raise SandboxError("bwrap worker count is invalid")
    adaptive_path = _snapshot_file(
        snapshot, Path("config/text_to_sql/adaptive.yaml")
    )
    workflow_path = _snapshot_file(
        snapshot, Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    try:
        adaptive_raw = yaml.safe_load(adaptive_path.read_text(encoding="utf-8"))
        adaptive_policy = AdaptivePolicyConfig.model_validate(adaptive_raw)
    except Exception as exc:
        raise SandboxError("snapshot adaptive policy is invalid") from exc
    return {
        "outer_case_deadline_seconds": outer_case_deadline,
        "workers": workers,
        "adaptive_policy": adaptive_policy.model_dump(mode="json", warnings="error"),
        "workflow_retry_policy": _workflow_retry_policy(workflow_path),
    }


def _snapshot_file(snapshot: SourceSnapshot, relative_path: Path) -> Path:
    root = snapshot.root.resolve(strict=True)
    path = root / relative_path
    if path.is_symlink() or not path.is_file():
        raise SandboxError("source snapshot execution policy input is unsafe")
    return path


def _workflow_retry_policy(path: Path) -> dict[str, object]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SandboxError("snapshot workflow policy is invalid") from exc
    if not isinstance(payload, Mapping):
        raise SandboxError("snapshot workflow policy is invalid")
    global_policy = _validated_retry_policy(
        payload.get("global_retry_policy"), global_policy=True
    )
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise SandboxError("snapshot workflow steps are invalid")
    per_step: dict[str, object] = {}
    for raw_step in raw_steps:
        if not isinstance(raw_step, Mapping):
            raise SandboxError("snapshot workflow step is invalid")
        step_id = raw_step.get("id")
        if not isinstance(step_id, str) or not step_id or step_id in per_step:
            raise SandboxError("snapshot workflow step identity is invalid")
        per_step[step_id] = _validated_retry_policy(
            raw_step.get("retry_policy"), global_policy=False
        )
    return {"global": global_policy, "per_step": per_step}


def _validated_retry_policy(value: object, *, global_policy: bool) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise SandboxError("snapshot workflow retry policy is invalid")
    required = (
        {"max_retries", "backoff_strategy", "base_delay", "max_delay", "retry_on_errors"}
        if global_policy
        else {"max_retries"}
    )
    if set(value) != required:
        raise SandboxError("snapshot workflow retry policy is invalid")
    max_retries = value.get("max_retries")
    if type(max_retries) is not int or max_retries < 0:
        raise SandboxError("snapshot workflow retry policy is invalid")
    if not global_policy:
        return {"max_retries": max_retries}
    backoff = value.get("backoff_strategy")
    retry_errors = value.get("retry_on_errors")
    delays = (value.get("base_delay"), value.get("max_delay"))
    if (
        not isinstance(backoff, str)
        or not backoff
        or not isinstance(retry_errors, list)
        or any(not isinstance(item, str) or not item for item in retry_errors)
        or any(
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(delay)
            or delay < 0
            for delay in delays
        )
    ):
        raise SandboxError("snapshot workflow retry policy is invalid")
    return {
        "max_retries": max_retries,
        "backoff_strategy": backoff,
        "base_delay": delays[0],
        "max_delay": delays[1],
        "retry_on_errors": list(retry_errors),
    }
