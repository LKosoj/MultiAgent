"""Canonical per-case sandbox runner and release-leg artifacts."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import socket
from typing import Any, Callable, Mapping

from .sandbox import (
    BwrapSandboxSpec,
    SandboxError,
    SourceSnapshot,
    empty_history_receipt,
    prepare_shared_schema_memory,
    verify_shared_schema_memory,
)
from . import public_benchmark_artifacts as artifacts
from scripts import text2sql_public_benchmark as runner

CANONICAL_CASE_COUNTS = runner.CANONICAL_CASE_COUNTS
EARLY_STOP_AWAITING_DECISION = runner.EARLY_STOP_AWAITING_DECISION
ObservationWriter = runner.ObservationWriter
REPO_ROOT = runner.REPO_ROOT
_files_digest = runner._files_digest
_json_digest = runner._json_digest
_load_completed = runner._load_completed
_ordered_canonical_cases = runner._ordered_canonical_cases
_select_cases = runner._select_cases
_sha256 = runner._sha256
_source_paths = runner._source_paths
_stable_case_manifest = runner._stable_case_manifest
export_predictions = runner.export_predictions


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _case_state_root(
    state_root: Path,
    *,
    benchmark: str,
    repeat_ordinal: int,
    case_key: str,
) -> Path:
    digest = hashlib.sha256(case_key.encode("utf-8")).hexdigest()[:16]
    return state_root / f"{benchmark}-r{repeat_ordinal}-{digest}"


def _shared_schema_memory_root(
    schema_memory_base: Path,
    *,
    database_id: str,
    expected_database_digest: str,
) -> Path:
    if not database_id or Path(database_id).name != database_id:
        raise SandboxError("database_id must be a single path component")
    if len(expected_database_digest) != 64 or any(
        character not in "0123456789abcdef" for character in expected_database_digest
    ):
        raise SandboxError("expected database SHA-256 is invalid")
    return schema_memory_base / database_id / expected_database_digest


def _sandbox_runtime_env(args: argparse.Namespace) -> dict[str, str]:
    canonical_environment = getattr(args, "canonical_runtime_env", None)
    if canonical_environment is not None:
        if getattr(args, "sandbox_env", []):
            raise SandboxError("--sandbox-env is diagnostic-only")
        if not isinstance(canonical_environment, Mapping):
            raise SandboxError("canonical runtime environment is invalid")
        return {
            str(name): str(value) for name, value in canonical_environment.items()
        }
    values: dict[str, str] = {}
    for raw in getattr(args, "sandbox_env", []):
        name, separator, value = raw.partition("=")
        if not separator or not name:
            raise ValueError("--sandbox-env must use NAME=VALUE")
        values[name] = value
    return values


def _run_case_in_sandbox(
    case: runner.BenchmarkCase,
    *,
    args: argparse.Namespace,
    token: str,
    snapshot: SourceSnapshot,
    state_root: Path,
    bundle_id: str,
    configuration_digest: str,
    expected_database_digest: str,
    persist_receipt: Callable[[Mapping[str, object]], None],
    run_scope: str,
) -> tuple[dict[str, Any], dict[str, object]]:
    _verify_database_digest(case, expected_database_digest)
    case_root = _case_state_root(
        state_root,
        benchmark=args.dataset,
        repeat_ordinal=args.repeat_ordinal,
        case_key=case.case_key,
    )
    shared_schema_memory_root = _shared_schema_memory_root(
        getattr(args, "shared_schema_memory_base", None)
        or state_root / "schema-memory",
        database_id=case.database_id,
        expected_database_digest=expected_database_digest,
    )
    spec = BwrapSandboxSpec(
        snapshot_root=snapshot.root,
        venv_root=args.sandbox_venv_root,
        case_root=case_root,
        database_path=case.database_path,
        database_id=case.database_id,
        secret_dir=args.sandbox_secret_dir,
        port=_free_local_port(),
        runtime_env=_sandbox_runtime_env(args),
        expected_database_sha256=expected_database_digest,
        shared_schema_memory_root=shared_schema_memory_root,
    )
    receipt: dict[str, object] | None = None

    def verify_empty_history() -> None:
        nonlocal receipt
        receipt = empty_history_receipt(
            benchmark=args.dataset,
            repeat_ordinal=args.repeat_ordinal,
            case_key=case.case_key,
            spec=spec,
        )
        persist_receipt(receipt)

    def start_case() -> dict[str, Any]:
        client = runner._client(f"http://127.0.0.1:{spec.port}", token)
        principal = dict(client.get_me())
        if "admin" not in principal.get("roles", []):
            raise RuntimeError("sandbox benchmark principal must have the admin role")
        connection = client.register_connection(
            display_name=f"{args.dataset}:{case.database_id}",
            dsn=f"sqlite:////benchmark-input/{case.database_id}.sqlite",
            owner_subject="text2sql-benchmark",
            tenant_id="text2sql-benchmark",
            enabled_for_user=True,
        )
        return runner._run_case(
            case,
            benchmark_name=args.dataset,
            base_url=f"http://127.0.0.1:{spec.port}",
            token=token,
            connection_ref=connection.connection_ref,
            timeout_seconds=args.case_timeout,
            max_rows=args.max_rows,
        )

    prepare_shared_schema_memory(shared_schema_memory_root)
    try:
        observation = runner.SandboxCaseRunner().run(
            spec,
            start_case,
            expected_snapshot=snapshot,
            before_start=verify_empty_history,
        )
    finally:
        verify_shared_schema_memory(shared_schema_memory_root)
    if receipt is None:
        raise SandboxError("sandbox case completed without an empty-history receipt")
    observation.update(
        {
            "artifact_contract_version": 1,
            "bundle_id": bundle_id,
            "repeat_ordinal": args.repeat_ordinal,
            "execution_mode": (
                "canonical_release"
                if getattr(args, "canonical_release_leg", False)
                else "diagnostic_noncanonical"
            ),
            "run_scope": run_scope,
            "seed": args.seed,
            "state_namespace": receipt["state_namespace"],
            "preexisting_history_items": receipt["preexisting_history_items"],
            "empty_history_receipt_digest": _json_digest(receipt),
            "source_snapshot_digest": snapshot.digest,
            "configuration_digest": configuration_digest,
            "database_digest": f"sha256:{expected_database_digest}",
        }
    )
    observation["runtime_evidence"] = _runtime_evidence(observation, spec)
    return observation, receipt


def _verify_database_digest(
    case: runner.BenchmarkCase,
    expected_digest: str,
) -> None:
    if _sha256(case.database_path) != expected_digest:
        raise SandboxError(
            f"benchmark database changed after case manifest: {case.case_key}"
        )


def _runtime_evidence(
    observation: Mapping[str, Any],
    spec: BwrapSandboxSpec,
) -> dict[str, Any]:
    outcome = observation.get("outcome")
    terminal_available = isinstance(outcome, Mapping)
    terminal = {
        "availability": "available" if terminal_available else "unavailable",
        "run_id": observation.get("run_id") if terminal_available else None,
        "run_incarnation": None,
        "run_incarnation_availability": "unavailable",
        "reason_code": outcome.get("reason_code") if terminal_available else None,
        "primary_stage": None,
        "primary_stage_availability": "unavailable",
        "terminal_digest": _json_digest(outcome) if terminal_available else None,
    }
    unavailable = {
        name: {"availability": "unavailable", "reason": "runtime_not_reported"}
        for name in (
            "authoritative_terminal_count",
            "action_digest_receipts",
            "candidate_digest_receipts",
            "execution_digest_receipts",
            "research_action_count_and_cap",
            "model_calls_tokens_cost_receipts",
            "deadline_receipt",
            "stagnation_classification",
            "pre_execution_gate_receipt",
        )
    }
    semantic_evidence: dict[str, object] = {"availability": "unavailable"}
    semantic_evidence_authority: dict[str, object] = {
        "availability": "unavailable"
    }
    receipt = (
        runner._validated_semantic_evidence_receipt(
            outcome.get("semantic_evidence_receipt"),
            outcome_status=outcome.get("status"),
            reason_code=outcome.get("reason_code"),
        )
        if isinstance(outcome, Mapping)
        else None
    )
    if receipt is not None:
        semantic_evidence = {
            "availability": "verified",
            "error_class": receipt["error_class"],
            "violated_typed_requirement": receipt[
                "violated_typed_requirement"
            ],
            "pipeline_component": receipt["pipeline_component"],
        }
        semantic_evidence_authority = receipt
    stagnation_receipt = (
        runner._validated_stagnation_receipt(
            outcome.get("stagnation_receipt"),
            outcome_status=outcome.get("status"),
            reason_code=outcome.get("reason_code"),
        )
        if isinstance(outcome, Mapping)
        else None
    )
    if stagnation_receipt is not None:
        unavailable["stagnation_classification"] = {
            "availability": "verified",
            "rejection_signatures": stagnation_receipt["rejection_signatures"],
        }
    return {
        "schema_version": 2,
        "status": "incomplete",
        "terminal": terminal,
        "semantic_evidence": semantic_evidence,
        "semantic_evidence_authority": semantic_evidence_authority,
        **unavailable,
        "state_artifact_digest": {
            "availability": "available",
            "digest": _files_digest(spec.case_root),
        },
    }


def _run_bwrap_benchmark(args: argparse.Namespace, token: str) -> int:
    """Run each case in a fresh local sandbox; no state crosses cases."""
    from .public_benchmark_bwrap_execution import execute_bwrap_benchmark

    return execute_bwrap_benchmark(args, token)


_canonical_run_scope = artifacts._canonical_run_scope
_create_canonical_output_dir = artifacts._create_canonical_output_dir
_empty_history_evidence_bytes = artifacts._empty_history_evidence_bytes
_failed_sandbox_observation = artifacts._failed_sandbox_observation
_validate_release_resume = artifacts._validate_release_resume
_write_artifact_handshake = artifacts._write_artifact_handshake
_write_case_manifest = artifacts._write_case_manifest
_write_empty_history_evidence = artifacts._write_empty_history_evidence
_write_json_atomically = artifacts._write_json_atomically
_write_manifest = artifacts._write_manifest
_write_source_snapshot_artifact = artifacts._write_source_snapshot_artifact
