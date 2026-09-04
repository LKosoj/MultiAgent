"""Canonical per-case sandbox runner and release-leg artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
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
from ..schema_namespace import SchemaScope
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


def seed_case_schema_snapshot(*, state_root: Path, case_root: Path, dsn: str) -> None:
    """Copy the latest matching schema snapshot after empty-history validation."""

    scope = SchemaScope.from_mapping(
        {
            "serialization_version": 1,
            "tenant_id": "text2sql-benchmark",
            "access_scope_id": "owner:text2sql-benchmark",
            "connection_view_id": (
                "dsn:" + hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]
            ),
            "transient": False,
        }
    )
    filename = f"schema-v1-{scope.scope_key}.json"
    candidates: list[tuple[str, int, str, Path]] = []
    for path in state_root.rglob(filename):
        if path.is_symlink() or not path.is_file() or case_root in path.parents:
            continue
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("snapshot_version") != 1
            or snapshot.get("schema_scope") != scope.to_mapping()
            or not isinstance(snapshot.get("schema_info"), dict)
        ):
            continue
        captured_at = snapshot.get("captured_at")
        if not isinstance(captured_at, str):
            continue
        relative = path.relative_to(state_root)
        candidates.append(
            (
                captured_at,
                -len(relative.parts),
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path,
            )
        )
    if not candidates:
        return
    source = max(candidates)[-1]
    destination = case_root / "sqlrag" / filename
    shutil.copyfile(source, destination)
    destination.chmod(0o600)


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
    expected_schema_description_sidecar: Mapping[str, object] | None = None,
    persist_receipt: Callable[[Mapping[str, object]], None],
    run_scope: str,
) -> tuple[dict[str, Any], dict[str, object]]:
    _verify_database_digest(case, expected_database_digest)
    case_dsn = f"sqlite:////benchmark-input/{case.database_id}.sqlite"
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
        seed_case_schema_snapshot(
            state_root=state_root,
            case_root=case_root,
            dsn=case_dsn,
        )
        runner.materialize_schema_description_sidecar(
            case,
            case_root=case_root,
            dsn=case_dsn,
            expected_identity=expected_schema_description_sidecar,
        )

    def start_case() -> dict[str, Any]:
        client = runner._client(f"http://127.0.0.1:{spec.port}", token)
        principal = dict(client.get_me())
        if "admin" not in principal.get("roles", []):
            raise RuntimeError("sandbox benchmark principal must have the admin role")
        connection = client.register_connection(
            display_name=f"{args.dataset}:{case.database_id}",
            dsn=case_dsn,
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
    run_id = observation.get("run_id") if terminal_available else None
    terminal = {
        "availability": "available" if terminal_available else "unavailable",
        "run_id": run_id,
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
    unavailable["model_calls_tokens_cost_receipts"] = (
        _model_calls_tokens_cost_receipts_evidence(run_id, spec)
    )
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


_MODEL_CALL_STEP_RE = re.compile(r"^(?P<step>.+)-\d+-\d+\Z")


def _model_call_step(call_id: str) -> str:
    """Strip a trailing ``-<revision>-<attempt>`` suffix off a model-call id.

    E.g. ``"research-stop-review-2-3"`` -> ``"research-stop-review"``. Call ids
    that do not end in two dash-separated integers are returned unchanged;
    this deliberately does not hardcode any specific step-name prefix.
    """

    match = _MODEL_CALL_STEP_RE.match(call_id)
    return match.group("step") if match is not None else call_id


def _empty_model_call_totals() -> dict[str, int]:
    return {
        "call_count": 0,
        "reconciled_call_count": 0,
        # Of the reconciled calls, how many were charged at the reserved
        # maximum because the provider did not report real usage (see
        # ModelCallReconciliation.usage_was_conservative). Lets a reader
        # distinguish measured cost from an upper-bound estimate.
        "conservative_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "charged_input_tokens": 0,
        "charged_output_tokens": 0,
        "charged_total_tokens": 0,
    }


def _model_calls_tokens_cost_receipts_evidence(
    run_id: str | None,
    spec: BwrapSandboxSpec,
) -> dict[str, Any]:
    """Read the model-call budget ledger the sandboxed run already wrote.

    ``finalize_text_to_sql_run`` does not have access to the ledger, so this
    reopens a second, independent ``AdaptiveBudgetLedger`` instance against
    the already-written ``workflow_state.db`` under the case workspace,
    post-hoc on the host once the sandbox process has exited. This mirrors
    the reopen-an-existing-ledger-file pattern already used in
    ``tests/test_adaptive_model_budget.py``.
    """

    import sqlite3

    from custom_tools.text_to_sql.adaptive.policy import BudgetAdmissionError
    from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger

    if not isinstance(run_id, str) or not run_id:
        return {"availability": "unavailable", "reason": "run_id_unavailable"}
    db_path = spec.case_root / "workspace" / "workflow_state.db"
    if not db_path.is_file():
        return {"availability": "unavailable", "reason": "ledger_database_missing"}

    # workflow/state_files.py::prepare_sqlite_file (invoked from
    # AdaptiveBudgetLedger.__init__) performs TOCTOU-safe validation of the
    # already-written ledger file (symlink/hardlink/ownership races) and
    # raises plain ValueError for those checks -- not just the OSError,
    # RuntimeError, sqlite3.Error, and BudgetAdmissionError already caught
    # here. Because db_path is always a well-formed Path we constructed
    # ourselves, a ValueError here can only come from that race-detection
    # path, not from a malformed argument, so it is safe to fold into this
    # best-effort telemetry catch alongside the others.
    try:
        ledger = AdaptiveBudgetLedger(db_path)
    except (BudgetAdmissionError, OSError, RuntimeError, ValueError, sqlite3.Error):
        return {"availability": "unavailable", "reason": "ledger_unreadable"}
    try:
        run_incarnations = ledger.list_model_run_incarnations(run_id)
        if not run_incarnations:
            return {
                "availability": "unavailable",
                "reason": "no_model_calls_recorded",
            }
        calls: list[dict[str, Any]] = []
        by_step: dict[str, dict[str, int]] = {}
        totals = _empty_model_call_totals()
        for run_incarnation in run_incarnations:
            for record in ledger.load_model_records(run_id, run_incarnation):
                reservation = record.reservation
                step = _model_call_step(reservation.call_id)
                usage = record.result.usage if record.result is not None else None
                input_tokens = usage.input_tokens if usage is not None else None
                output_tokens = usage.output_tokens if usage is not None else None
                reconciliation = record.reconciliation
                charged_total = (
                    reconciliation.charged_total_tokens
                    if reconciliation is not None
                    else None
                )
                # None means "not yet reconciled" (unknown), as distinct from
                # a reconciled call known to be either measured (False) or
                # conservative (True).
                usage_was_conservative = (
                    reconciliation.usage_was_conservative
                    if reconciliation is not None
                    else None
                )
                calls.append(
                    {
                        "call_id": reservation.call_id,
                        "step": step,
                        "run_incarnation": run_incarnation,
                        "model_identity": reservation.model_identity,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "charged_total": charged_total,
                        "usage_was_conservative": usage_was_conservative,
                        "started_at_ns": (
                            record.started.started_at_ns
                            if record.started is not None
                            else None
                        ),
                        "duration_ns": None,
                    }
                )
                step_totals = by_step.setdefault(step, _empty_model_call_totals())
                for bucket in (step_totals, totals):
                    bucket["call_count"] += 1
                    if reconciliation is not None:
                        bucket["reconciled_call_count"] += 1
                        bucket["charged_input_tokens"] += (
                            reconciliation.charged_input_tokens
                        )
                        bucket["charged_output_tokens"] += (
                            reconciliation.charged_output_tokens
                        )
                        bucket["charged_total_tokens"] += charged_total
                        if usage_was_conservative:
                            bucket["conservative_call_count"] += 1
                    if input_tokens is not None:
                        bucket["input_tokens"] += input_tokens
                    if output_tokens is not None:
                        bucket["output_tokens"] += output_tokens
    except (BudgetAdmissionError, OSError, RuntimeError, ValueError, sqlite3.Error):
        return {"availability": "unavailable", "reason": "ledger_unreadable"}
    finally:
        ledger.close()

    return {
        "availability": "available",
        # v2 adds "conservative_call_count" to by_step/totals and
        # "usage_was_conservative" to each call; existing field names and
        # meanings are unchanged.
        "schema_version": 2,
        "run_id": run_id,
        "run_incarnations": list(run_incarnations),
        "by_step": by_step,
        "totals": totals,
        "calls": sorted(
            calls, key=lambda call: (call["run_incarnation"], call["call_id"])
        ),
        "duration_ns_availability": "unavailable",
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
