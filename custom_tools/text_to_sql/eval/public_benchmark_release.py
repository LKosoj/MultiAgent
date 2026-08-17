"""Canonical release contracts for the public Text-to-SQL benchmark runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable, Mapping, Sequence

from . import sandbox as sandbox_support
from .sandbox import (
    SandboxError,
    SourceSnapshot,
    resolve_safe_regular_file,
    source_snapshot_manifest,
)
from scripts import text2sql_benchmark_reporting as benchmark_reporting
from . import release_inputs
from . import release_state
from . import release_diagnostics
from .release_governance import governance_event_paths
from .official_evaluator_bridge import CANONICAL_PUBLISH_ORDER
from .official_evaluator_contracts import (
    IMAGE_ID,
    IMAGE_IDENTITY,
    IMAGE_PLATFORM,
    IMAGE_USER,
    RAW_FREEZE_SHA256,
)


finalize_post_repeat_stop = release_diagnostics.finalize_post_repeat_stop
CANONICAL_RUNTIME_SOURCE_PATHS = sandbox_support.CANONICAL_RUNTIME_SOURCE_PATHS
create_source_snapshot = sandbox_support.create_source_snapshot
source_snapshot_from_manifest = sandbox_support.source_snapshot_from_manifest
validate_canonical_workers = sandbox_support.validate_canonical_workers


MANDATORY_LEG_ARTIFACTS = frozenset(
    {
        "manifest.json",
        "case_manifest.json",
        "observations.jsonl",
        "empty_history_evidence.json",
    }
)
EVALUATED_LEG_ARTIFACTS = (
    "runner_stdout.log",
    "runner_stderr.log",
    *CANONICAL_PUBLISH_ORDER,
)
_EVALUATION_MANIFEST_FIELDS = frozenset(
    {
        "schema_version", "record_kind", "evaluator_input_sha256",
        "evaluator_input_path", "score_sha256", "score_path",
        "evaluator_receipt_sha256", "run_manifest_sha256",
        "case_manifest_sha256", "diagnostics_sha256", "summary_sha256",
        "case_keys",
    }
)
_EXECUTION_EVIDENCE_V1_FIELDS = frozenset(
    {
        "schema_version", "record_kind", "benchmark", "release_lock_digest",
        "evaluator_identity", "image_identity", "image_inspection",
        "freeze_before", "freeze_after", "artifacts",
    }
)
_EXECUTION_EVIDENCE_V2_FIELDS = _EXECUTION_EVIDENCE_V1_FIELDS | {
    "attempt_id", "input_kind", "evaluator_input_sha256",
    "staged_receipt_sha256",
}
_EXECUTION_ARTIFACTS = frozenset(
    {
        "official_evaluator_stdout.log", "official_evaluator_stderr.log",
        "official_evaluator_source.log", "official_evaluator_raw_results.json",
        "official_scores.json",
    }
)
_SEALED_EVALUATION_ARTIFACTS = (
    "official_evaluator_input.json", "official_scores.json",
    "official_evaluator_stdout.log", "official_evaluator_stderr.log",
    "official_evaluator_source.log", "official_evaluator_raw_results.json",
    "official_evaluator_execution.json", "evaluator_receipt.json",
    "diagnostics.jsonl", "summary.json", "failure_report.md", "evaluation_manifest.json",
)
_COMPLETED_LEG_RECORD_FIELDS = frozenset(
    {"benchmark", "repeat_ordinal", "seed", "return_code", "artifact_handshake_sha256"}
)
_COMPLETED_LEG_HANDSHAKE_FIELDS = frozenset(
    {
        "schema_version", "record_kind", "bundle_id", "benchmark",
        "repeat_ordinal", "execution_mode", "seed", "run_scope",
        "case_manifest_digest", "source_snapshot_digest",
        "source_snapshot_manifest_digest", "configuration_digest", "artifacts",
    }
)
_COMPLETED_LEG_MANIFEST_FIELDS = frozenset(
    {
        "schema_version", "created_at", "benchmark", "case_count",
        "completed_before", "repo_revision", "pipeline_revision", "base_url",
        "repeat_ordinal", "workers", "case_timeout", "max_rows",
        "model_configuration", "configuration_sources",
        "successful_sql_memory_enabled", "principal", "sources",
        "artifact_contract_version", "execution_mode", "execution_policy",
        "source_snapshot_digest", "state_root", "bundle_id", "history_mode",
        "case_manifest_digest", "configuration_digest", "seed", "run_scope",
        "source_snapshot_manifest_digest", "release_identity",
        "canonical_environment", "model_identity", "evaluator_identity",
    }
)

CANONICAL_RELEASE_DATASET_ORDER = release_inputs.CANONICAL_RELEASE_DATASET_ORDER
ReleaseCase = release_inputs.ReleaseCase
FrozenReleaseInputs = release_inputs.FrozenReleaseInputs
FileDigestCache = release_inputs.FileDigestCache
json_digest = release_inputs.json_digest
sha256_file = release_inputs.sha256_file
_cached_sha256_file = release_inputs._cached_sha256_file
file_identity = release_inputs.file_identity
release_dataset_policy = release_inputs.release_dataset_policy
evaluator_identity = release_inputs.evaluator_identity
build_release_plan = release_inputs.build_release_plan
canonical_runtime_environment = release_inputs.canonical_runtime_environment
verified_git_provenance = release_inputs.verified_git_provenance
_normalize_git_origin = release_inputs._normalize_git_origin


def stable_case_manifest(
    benchmark: str,
    cases: Sequence[ReleaseCase],
    *,
    digest_cache: FileDigestCache | None = None,
) -> dict[str, object]:
    """Keep the historical patch point for release-input file hashing."""

    return release_inputs.stable_case_manifest(
        benchmark,
        cases,
        digest_cache=digest_cache,
        digest_file=sha256_file,
    )


_assert_policy_file = release_inputs._assert_policy_file
_release_input_record = release_inputs._release_input_record
inspect_release_inputs = release_inputs.inspect_release_inputs
release_input_path = release_inputs.release_input_path
validate_release_input_lock = release_inputs.validate_release_input_lock

def write_json_atomically(path: Path, payload: Mapping[str, object]) -> None:
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


def write_json_new_sealed(path: Path, payload: Mapping[str, object]) -> None:
    """Publish a new immutable JSON artifact without an overwrite window."""
    if path.exists() or path.is_symlink():
        raise SandboxError("release artifact output must be new")
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
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise SandboxError("release artifact was created by another process") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def write_release_input_lock_new(path: Path, lock: Mapping[str, object]) -> None:
    write_json_new_sealed(path, lock)


def read_json_object(path: Path, *, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise SandboxError(f"{label} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise SandboxError(f"{label} must be an object")
    return value


def validate_paused_candidate(path: Path, active_leg: Mapping[str, object]) -> dict[str, object]:
    """Verify the immutable candidate recorded when the leg was paused."""
    try:
        actual = release_diagnostics.digest_existing_sealed_file(
            path, label="early-stop candidate"
        )
    except release_diagnostics.DiagnosticArtifactError as exc:
        raise SandboxError(str(exc)) from exc
    candidate = read_json_object(path, label="early-stop candidate")
    expected = active_leg.get("candidate_sha256")
    if not isinstance(expected, str) or expected != actual:
        raise SandboxError("paused candidate digest does not match bundle state")
    if candidate.get("record_kind") != "text2sql_public_benchmark_early_stop_candidate":
        raise SandboxError("paused candidate kind is invalid")
    required = {
        "schema_version", "record_kind", "completed_case_count", "signature",
        "signature_case_count", "signature_share", "database_count", "database_ids",
        "completed_case_keys", "not_started_case_keys", "bundle_id", "benchmark",
        "repeat_ordinal", "policy_sha256", "release_identity", "configuration_digest",
        "source_snapshot_digest", "manifest_sha256", "case_manifest_sha256",
        "observations_sha256", "empty_history_evidence_sha256", "ordered_case_keys",
    }
    if set(candidate) != required:
        raise SandboxError("paused candidate schema is not closed")
    return candidate


def validate_paused_leg_inputs(
    leg_dir: Path, candidate: Mapping[str, object], *, identity: Mapping[str, object],
    configuration_digest: str,
) -> None:
    """Reject any changed partial-leg evidence before a resumed process starts."""
    expected = {
        "configuration_digest": configuration_digest,
        "source_snapshot_digest": identity.get("source_snapshot_digest"),
        "release_identity": dict(identity),
    }
    if any(candidate.get(key) != value for key, value in expected.items()):
        raise SandboxError("paused candidate identity mismatch")
    for name, field in (
        ("manifest.json", "manifest_sha256"),
        ("case_manifest.json", "case_manifest_sha256"),
        ("observations.jsonl", "observations_sha256"),
        ("empty_history_evidence.json", "empty_history_evidence_sha256"),
    ):
        artifact = leg_dir / name
        if artifact.is_symlink() or not artifact.is_file():
            raise SandboxError("paused leg artifact is missing or unsafe")
        if field in candidate and candidate[field] != f"sha256:{sha256_file(artifact)}":
            raise SandboxError("paused leg artifact digest mismatch")
    observations = [
        json.loads(line) for line in (leg_dir / "observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    keys = [row.get("case_key") for row in observations if isinstance(row, Mapping)]
    completed = candidate.get("completed_case_keys")
    if (
        not isinstance(completed, list)
        or any(not isinstance(key, str) or not key for key in keys + completed)
        or keys != completed
        or len(keys) != len(set(keys))
    ):
        raise SandboxError("paused observations are not the recorded exact prefix")
    completed_count = candidate.get("completed_case_count")
    if (
        not isinstance(completed_count, int)
        or isinstance(completed_count, bool)
        or completed_count != len(keys)
    ):
        raise SandboxError("paused candidate completed case count is invalid")
    history = read_json_object(
        leg_dir / "empty_history_evidence.json",
        label="paused empty-history evidence",
    )
    receipts = history.get("receipts")
    if not isinstance(receipts, list) or len(receipts) != len(keys):
        raise SandboxError("paused empty-history evidence is incomplete")
    for case_key, row, receipt in zip(keys, observations, receipts, strict=True):
        if not isinstance(receipt, Mapping) or receipt.get("case_key") != case_key:
            raise SandboxError("paused empty-history receipt identity is invalid")
        if receipt.get("preexisting_history_items") != 0:
            raise SandboxError("paused empty-history receipt is invalid")
        if row.get("preexisting_history_items") != 0:
            raise SandboxError("paused observations do not prove empty history")
    manifest = read_json_object(leg_dir / "case_manifest.json", label="paused case manifest")
    manifest_cases = manifest.get("cases")
    if not isinstance(manifest_cases, list) or any(
        not isinstance(row, Mapping) for row in manifest_cases
    ):
        raise SandboxError("paused case manifest keys are invalid")
    all_keys = [row.get("case_key") for row in manifest_cases]
    if (
        any(not isinstance(key, str) or not key for key in all_keys)
        or len(all_keys) != len(set(all_keys))
    ):
        raise SandboxError("paused case manifest keys are invalid")
    ordered = candidate.get("ordered_case_keys")
    if (
        not isinstance(ordered, list)
        or any(not isinstance(key, str) or not key for key in ordered)
        or len(ordered) != len(set(ordered))
    ):
        raise SandboxError("paused candidate ordered case keys are invalid")
    if len(ordered) != len(all_keys) or set(ordered) != set(all_keys):
        raise SandboxError("paused candidate case universe does not match manifest")
    not_started = candidate.get("not_started_case_keys")
    if (
        not isinstance(not_started, list)
        or any(not isinstance(key, str) or not key for key in not_started)
        or keys + not_started != ordered
    ):
        raise SandboxError("paused candidate does not partition case manifest")


def recover_active_leg(
    output_dir: Path,
    *,
    state: dict[str, object],
    identity: Mapping[str, object],
    configuration_digest: str,
    progress_store: release_state.ReleaseProgressStore,
    persist_state: bool = True,
) -> bool:
    """Delegate crash recovery to the release coordinator."""
    from .release_coordinator import recover_active_leg as recover

    return recover(
        output_dir,
        state=state,
        identity=identity,
        configuration_digest=configuration_digest,
        progress_store=progress_store,
        persist_state=persist_state,
    )


def _validate_execution_evidence(
    leg_dir: Path,
    execution: Mapping[str, object],
    *,
    evaluator_identity: Mapping[str, object],
    case_keys: Sequence[str],
    run_manifest: Mapping[str, object],
    evaluator_input_sha256: str | None = None,
) -> None:
    benchmark = "bird" if case_keys and case_keys[0].startswith("bird:") else "spider"
    release_identity = run_manifest.get("release_identity")
    expected_lock_digest = (
        release_identity.get("release_lock_digest")
        if isinstance(release_identity, Mapping)
        else None
    )
    schema_version = execution.get("schema_version")
    expected_fields = (
        _EXECUTION_EVIDENCE_V2_FIELDS
        if schema_version == 2
        else _EXECUTION_EVIDENCE_V1_FIELDS
    )
    if (
        set(execution) != expected_fields
        or schema_version not in {1, 2}
        or execution.get("record_kind") != "text2sql_official_evaluator_execution"
        or execution.get("benchmark") != benchmark
        or execution.get("release_lock_digest") != expected_lock_digest
        or execution.get("evaluator_identity") != dict(evaluator_identity)
        or execution.get("image_identity") != IMAGE_IDENTITY
        or execution.get("image_inspection")
        != {"image_id": IMAGE_ID, "user": IMAGE_USER, "platform": IMAGE_PLATFORM}
        or execution.get("freeze_before") != RAW_FREEZE_SHA256
        or execution.get("freeze_after") != RAW_FREEZE_SHA256
    ):
        raise SandboxError("post-repeat execution evidence is invalid")
    if schema_version == 2:
        attempt_id = execution.get("attempt_id")
        input_kind = execution.get("input_kind")
        expected_kind = (
            "bird_predictions_json"
            if benchmark == "bird"
            else "spider_predictions_manifest"
        )
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or input_kind != expected_kind
            or execution.get("evaluator_input_sha256")
            != evaluator_input_sha256
        ):
            raise SandboxError("post-repeat staged evaluator binding is invalid")
        staged_path = (
            leg_dir / "official-evaluator-attempts" / attempt_id / "STAGED.json"
        )
        try:
            staged_path = resolve_safe_regular_file(
                staged_path, label="official evaluator staged receipt"
            )
        except OSError as exc:
            raise SandboxError("post-repeat staged evaluator receipt is missing") from exc
        if (
            staged_path.stat().st_mode & 0o777 != 0o444
            or execution.get("staged_receipt_sha256")
            != f"sha256:{sha256_file(staged_path)}"
        ):
            raise SandboxError("post-repeat staged evaluator receipt is invalid")
    artifacts = execution.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != _EXECUTION_ARTIFACTS:
        raise SandboxError("post-repeat execution artifact inventory is invalid")
    for name in _EXECUTION_ARTIFACTS:
        try:
            artifact = resolve_safe_regular_file(
                leg_dir / name, label="official evaluator execution artifact"
            )
        except OSError as exc:
            raise SandboxError("post-repeat execution artifact is missing") from exc
        if artifacts.get(name) != f"sha256:{sha256_file(artifact)}":
            raise SandboxError("post-repeat execution artifact digest mismatch")


def _validate_sealed_evaluation_artifacts(leg_dir: Path) -> None:
    for name in _SEALED_EVALUATION_ARTIFACTS:
        artifact = leg_dir / name
        if (
            artifact.is_symlink()
            or not artifact.is_file()
            or artifact.stat().st_mode & 0o777 != 0o444
        ):
            raise SandboxError("post-repeat sealed artifact is invalid")


def validate_post_repeat_evaluation(
    leg_dir: Path,
    *,
    evaluator_identity: Mapping[str, object],
) -> list[dict[str, object]]:
    """Require a closed official evaluation before the next release repeat.

    Evaluation remains outside the pipeline sandbox, so this function only
    validates its immutable receipts and diagnostics.  It never opens gold
    inputs or exposes evaluator output to the pipeline.
    """
    paths = {
        "evaluation_manifest": leg_dir / "evaluation_manifest.json",
        "evaluator_receipt": leg_dir / "evaluator_receipt.json",
        "manifest": leg_dir / "manifest.json",
        "case_manifest": leg_dir / "case_manifest.json",
        "observations": leg_dir / "observations.jsonl",
        "diagnostics": leg_dir / "diagnostics.jsonl",
        "summary": leg_dir / "summary.json",
    }
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise SandboxError(f"post-repeat evaluation is missing {name}")
    evaluation = read_json_object(paths["evaluation_manifest"], label="evaluation manifest")
    if set(evaluation) != _EVALUATION_MANIFEST_FIELDS or (
        evaluation.get("schema_version") != 1
        or evaluation.get("record_kind")
        != "text2sql_public_benchmark_evaluation_manifest"
    ):
        raise SandboxError("post-repeat evaluation manifest is invalid")
    receipt = read_json_object(paths["evaluator_receipt"], label="evaluator receipt")
    if not benchmark_reporting.evaluator_receipt_is_closed(receipt):
        raise SandboxError("post-repeat evaluator receipt is invalid")
    if receipt.get("schema_version") == 2:
        execution_path = leg_dir / "official_evaluator_execution.json"
        if execution_path.is_symlink() or not execution_path.is_file():
            raise SandboxError("post-repeat evaluation is missing execution evidence")
        if receipt.get("execution_evidence_sha256") != (
            f"sha256:{sha256_file(execution_path)}"
        ):
            raise SandboxError("post-repeat execution evidence digest mismatch")
    if receipt.get("evaluator_identity") != dict(evaluator_identity):
        raise SandboxError("post-repeat evaluator identity is not pinned")
    case_manifest = read_json_object(paths["case_manifest"], label="case manifest")
    cases = case_manifest.get("cases")
    case_keys = [row.get("case_key") for row in cases if isinstance(row, Mapping)] if isinstance(cases, list) else []
    if (
        len(case_keys) != len(cases or [])
        or any(not isinstance(key, str) or not key for key in case_keys)
        or receipt.get("case_keys") != case_keys
        or evaluation.get("case_keys") != case_keys
    ):
        raise SandboxError("post-repeat evaluator case keys are invalid")
    if receipt.get("schema_version") == 2:
        execution = read_json_object(
            leg_dir / "official_evaluator_execution.json",
            label="official evaluator execution evidence",
        )
        run_manifest = read_json_object(paths["manifest"], label="run manifest")
        _validate_execution_evidence(
            leg_dir,
            execution,
            evaluator_identity=evaluator_identity,
            case_keys=[str(key) for key in case_keys],
            run_manifest=run_manifest,
            evaluator_input_sha256=str(receipt.get("evaluator_input_sha256")),
        )
        if execution.get("schema_version") == 2:
            _validate_sealed_evaluation_artifacts(leg_dir)
    expected = {
        "evaluator_receipt_sha256": paths["evaluator_receipt"],
        "run_manifest_sha256": paths["manifest"],
        "case_manifest_sha256": paths["case_manifest"],
        "diagnostics_sha256": paths["diagnostics"],
        "summary_sha256": paths["summary"],
    }
    for field, path in expected.items():
        if evaluation.get(field) != f"sha256:{sha256_file(path)}":
            raise SandboxError("post-repeat evaluation artifact digest mismatch")
    for field, digest_field in (
        ("evaluator_input_path", "evaluator_input_sha256"),
        ("score_path", "score_sha256"),
    ):
        relative = evaluation.get(field)
        if not release_diagnostics.is_canonical_relative_artifact_path(relative):
            raise SandboxError("post-repeat evaluation path is invalid")
        assert isinstance(relative, str)
        try:
            artifact = resolve_safe_regular_file(
                leg_dir / relative, label="post-repeat evaluation input"
            )
        except OSError as exc:
            raise SandboxError("post-repeat evaluation input is missing or unsafe") from exc
        if not artifact.is_relative_to(leg_dir.resolve()) or evaluation.get(digest_field) != f"sha256:{sha256_file(artifact)}":
            raise SandboxError("post-repeat evaluation input digest mismatch")
    if receipt.get("run_manifest_sha256") != evaluation.get("run_manifest_sha256") or (
        receipt.get("case_manifest_sha256") != evaluation.get("case_manifest_sha256")
    ):
        raise SandboxError("post-repeat evaluator receipt binding is invalid")
    if receipt.get("score_sha256") != evaluation.get("score_sha256") or (
        receipt.get("evaluator_input_sha256")
        != evaluation.get("evaluator_input_sha256")
    ):
        raise SandboxError("post-repeat evaluator score binding is invalid")
    try:
        observations, _diagnostics, _summary = (
            release_diagnostics.validate_post_repeat_case_evidence(
                leg_dir,
                expected_case_keys=[str(key) for key in case_keys],
            )
        )
    except release_diagnostics.DiagnosticArtifactError as exc:
        raise SandboxError(str(exc)) from exc
    return observations


def write_bundle_state(path: Path, payload: Mapping[str, object]) -> None:
    if payload.get("record_kind") != "text2sql_public_benchmark_bundle_state":
        raise SandboxError("bundle state kind is invalid")
    write_json_atomically(path, payload)


def leg_output_dir(output_dir: Path, benchmark: str, repeat_ordinal: int) -> Path:
    return output_dir / "runs" / benchmark / f"r{repeat_ordinal}"


def _validate_leg_artifacts(
    leg_dir: Path,
    handshake: Mapping[str, object],
) -> None:
    artifacts = handshake.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise SandboxError("completed leg artifact inventory is invalid")
    artifact_names = set(artifacts)
    evaluated_names = set(EVALUATED_LEG_ARTIFACTS)
    governance_events: dict[tuple[str, int], set[str]] = {}
    for name in artifact_names:
        if not name.startswith("governance/"):
            continue
        parts = name.split("/")
        if len(parts) != 4:
            raise SandboxError("completed leg governance inventory is invalid")
        event_kind, count_text = parts[1], parts[2]
        if not count_text.isdecimal() or len(count_text) != 6:
            raise SandboxError("completed leg governance inventory is invalid")
        completed_case_count = int(count_text)
        try:
            expected_paths = governance_event_paths(event_kind, completed_case_count)
        except ValueError as exc:
            raise SandboxError(str(exc)) from exc
        if name not in expected_paths.values():
            raise SandboxError("completed leg governance inventory is invalid")
        governance_events.setdefault((event_kind, completed_case_count), set()).add(name)
    governance_names = {
        name for names in governance_events.values() for name in names
    }
    evaluated_present = artifact_names & evaluated_names
    if evaluated_present and evaluated_present != evaluated_names:
        raise SandboxError("completed leg evaluated artifact inventory is partial")
    if not MANDATORY_LEG_ARTIFACTS.issubset(artifact_names) or (
        artifact_names - MANDATORY_LEG_ARTIFACTS - governance_names - evaluated_names
    ):
        raise SandboxError("completed leg artifact inventory is incomplete or unknown")
    for (event_kind, completed_case_count), present in governance_events.items():
        paths = governance_event_paths(event_kind, completed_case_count)
        event_names = set(paths.values())
        if present != event_names:
            raise SandboxError("completed leg governance inventory is partial")
        try:
            event = release_diagnostics.validate_continue_decision(
                leg_dir,
                event_kind=event_kind,
                completed_case_count=completed_case_count,
            )
        except release_diagnostics.DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        for path_field, digest_field in (
            ("candidate_path", "candidate_sha256"),
            ("decision_path", "decision_sha256"),
            ("result_path", "result_sha256"),
        ):
            if artifacts.get(event[path_field]) != event[digest_field]:
                raise SandboxError("completed leg governance artifact changed")
    for name in sorted(artifact_names):
        expected_digest = artifacts.get(name)
        if not isinstance(expected_digest, str):
            raise SandboxError("completed leg artifact identity is invalid")
        path = leg_dir / name
        if path.is_symlink() or not path.is_file():
            raise SandboxError(f"completed leg artifact is missing: {name}")
        if name in evaluated_names and path.stat().st_mode & 0o777 != 0o444:
            raise SandboxError(f"completed leg evaluated artifact is not sealed: {name}")
        if expected_digest != f"sha256:{sha256_file(path)}":
            raise SandboxError(f"completed leg artifact changed: {name}")


def _validate_case_manifest(
    case_manifest: Mapping[str, object],
    *,
    bundle_id: str,
    plan_item: Mapping[str, object],
    locked_case_manifest: Mapping[str, object],
) -> None:
    expected_fields = {
        "record_kind": "text2sql_case_manifest",
        "bundle_id": bundle_id,
        "benchmark": plan_item["benchmark"],
        "repeat_ordinal": plan_item["repeat_ordinal"],
        "execution_mode": "canonical_release",
        "seed": plan_item["seed"],
        "run_scope": "full_release",
    }
    if any(case_manifest.get(name) != value for name, value in expected_fields.items()):
        raise SandboxError("completed leg case manifest identity mismatch")
    cases = case_manifest.get("cases")
    if cases != locked_case_manifest.get("cases"):
        raise SandboxError("completed leg case manifest differs from release lock")
    if not isinstance(cases, list):
        raise SandboxError("completed leg case manifest cases are invalid")
    if locked_case_manifest.get("case_count") != len(cases) or locked_case_manifest.get(
        "cases_digest"
    ) != json_digest(cases):
        raise SandboxError("release lock case manifest is internally inconsistent")


def _validate_completed_leg(
    output_dir: Path,
    *,
    record: Mapping[str, object],
    plan_item: Mapping[str, object],
    bundle_id: str,
    identity: Mapping[str, object],
    locked_case_manifests: Mapping[str, object],
    evaluator_identities: Mapping[str, object] | None,
) -> tuple[str, int]:
    benchmark = plan_item.get("benchmark")
    repeat_ordinal = plan_item.get("repeat_ordinal")
    seed = plan_item.get("seed")
    if (
        set(record) != _COMPLETED_LEG_RECORD_FIELDS
        or type(benchmark) is not str
        or type(repeat_ordinal) is not int
        or type(seed) is not int
        or record.get("benchmark") != benchmark
        or record.get("repeat_ordinal") != repeat_ordinal
        or record.get("seed") != seed
        or type(record.get("repeat_ordinal")) is not int
        or type(record.get("seed")) is not int
        or type(record.get("return_code")) is not int
    ):
        raise SandboxError("completed release legs are not a canonical prefix")
    leg_dir = leg_output_dir(output_dir, benchmark, repeat_ordinal)
    handshake_path = leg_dir / "artifact_handshake.json"
    handshake = read_json_object(
        handshake_path, label="completed leg artifact handshake"
    )
    if record.get("artifact_handshake_sha256") != (
        f"sha256:{sha256_file(handshake_path)}"
    ):
        raise SandboxError("completed leg artifact handshake changed")
    expected_handshake = {
        "schema_version": 1,
        "record_kind": "text2sql_benchmark_artifact_handshake",
        "bundle_id": bundle_id,
        "benchmark": benchmark,
        "repeat_ordinal": repeat_ordinal,
        "execution_mode": "canonical_release",
        "seed": seed,
        "run_scope": "full_release",
        "source_snapshot_digest": identity.get("source_snapshot_digest"),
        "source_snapshot_manifest_digest": identity.get(
            "source_snapshot_manifest_digest"
        ),
        "configuration_digest": identity.get("configuration_digest"),
    }
    if any(
        set(handshake) != _COMPLETED_LEG_HANDSHAKE_FIELDS
        or type(handshake.get("schema_version")) is not int
        or type(handshake.get("repeat_ordinal")) is not int
        or type(handshake.get("seed")) is not int
        or handshake.get(name) != value for name, value in expected_handshake.items()
    ):
        raise SandboxError("completed leg artifact handshake identity mismatch")
    _validate_leg_artifacts(leg_dir, handshake)
    artifacts = handshake["artifacts"]
    if handshake.get("case_manifest_digest") != artifacts.get("case_manifest.json"):
        raise SandboxError("completed leg case manifest digest mismatch")
    locked_manifest = locked_case_manifests.get(benchmark)
    if not isinstance(locked_manifest, Mapping):
        raise SandboxError("release lock case manifest is missing")
    case_manifest = read_json_object(
        leg_dir / "case_manifest.json", label="completed leg case manifest"
    )
    _validate_case_manifest(
        case_manifest,
        bundle_id=bundle_id,
        plan_item=plan_item,
        locked_case_manifest=locked_manifest,
    )
    run_manifest = read_json_object(
        leg_dir / "manifest.json", label="completed leg manifest"
    )
    expected_run_manifest = {
        "schema_version": 2,
        "artifact_contract_version": 1,
        "bundle_id": bundle_id,
        "benchmark": benchmark,
        "repeat_ordinal": repeat_ordinal,
        "execution_mode": "canonical_release",
        "seed": seed,
        "run_scope": "full_release",
        "source_snapshot_digest": identity.get("source_snapshot_digest"),
        "source_snapshot_manifest_digest": identity.get(
            "source_snapshot_manifest_digest"
        ),
        "configuration_digest": identity.get("configuration_digest"),
        "release_identity": dict(identity),
    }
    if evaluator_identities is not None:
        expected_run_manifest["evaluator_identity"] = evaluator_identities.get(
            benchmark
        )
    if set(run_manifest) != _COMPLETED_LEG_MANIFEST_FIELDS:
        if "execution_policy" not in run_manifest:
            raise SandboxError("completed leg manifest execution policy is invalid")
        raise SandboxError("completed leg manifest identity mismatch")
    if any(
        type(run_manifest.get("schema_version")) is not int
        or type(run_manifest.get("case_count")) is not int
        or type(run_manifest.get("completed_before")) is not int
        or type(run_manifest.get("repeat_ordinal")) is not int
        or type(run_manifest.get("seed")) is not int
        or type(run_manifest.get("artifact_contract_version")) is not int
        or run_manifest.get(name) != value
        for name, value in expected_run_manifest.items()
    ):
        raise SandboxError("completed leg manifest identity mismatch")
    if type(run_manifest.get("workers")) is not int:
        raise SandboxError("completed leg manifest execution policy is invalid")
    execution_policy = run_manifest.get("execution_policy")
    if not isinstance(execution_policy, Mapping) or set(execution_policy) != {
        "outer_case_deadline_seconds",
        "workers",
        "adaptive_policy",
        "workflow_retry_policy",
    }:
        raise SandboxError("completed leg manifest execution policy is invalid")
    outer_deadline = execution_policy["outer_case_deadline_seconds"]
    manifest_deadline = run_manifest.get("case_timeout")
    workers = execution_policy["workers"]
    manifest_workers = run_manifest.get("workers")
    if (
        isinstance(outer_deadline, bool)
        or not isinstance(outer_deadline, (int, float))
        or not 0 < outer_deadline < float("inf")
        or isinstance(manifest_deadline, bool)
        or not isinstance(manifest_deadline, (int, float))
        or not 0 < manifest_deadline < float("inf")
        or outer_deadline != manifest_deadline
        or type(workers) is not int
        or type(manifest_workers) is not int
        or workers != manifest_workers
        or workers != 1
    ):
        raise SandboxError("completed leg manifest execution policy is invalid")
    adaptive_policy = execution_policy["adaptive_policy"]
    try:
        from ..adaptive.policy import AdaptivePolicyConfig

        validated_adaptive_policy = AdaptivePolicyConfig.model_validate(
            adaptive_policy
        ).model_dump(mode="json", warnings="error")
    except Exception as exc:
        raise SandboxError("completed leg manifest execution policy is invalid") from exc
    if validated_adaptive_policy != adaptive_policy:
        raise SandboxError("completed leg manifest execution policy is invalid")
    workflow_retry_policy = execution_policy["workflow_retry_policy"]
    if not isinstance(workflow_retry_policy, Mapping) or set(
        workflow_retry_policy
    ) != {"global", "per_step"}:
        raise SandboxError("completed leg manifest execution policy is invalid")
    global_retry_policy = workflow_retry_policy["global"]
    per_step_retry_policy = workflow_retry_policy["per_step"]
    if not isinstance(global_retry_policy, Mapping) or set(global_retry_policy) != {
        "max_retries",
        "backoff_strategy",
        "base_delay",
        "max_delay",
        "retry_on_errors",
    }:
        raise SandboxError("completed leg manifest execution policy is invalid")
    if (
        type(global_retry_policy["max_retries"]) is not int
        or global_retry_policy["max_retries"] < 0
        or not isinstance(global_retry_policy["backoff_strategy"], str)
        or not global_retry_policy["backoff_strategy"]
        or any(
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not 0 <= delay < float("inf")
            for delay in (
                global_retry_policy["base_delay"],
                global_retry_policy["max_delay"],
            )
        )
        or not isinstance(global_retry_policy["retry_on_errors"], list)
        or any(
            not isinstance(error, str) or not error
            for error in global_retry_policy["retry_on_errors"]
        )
        or not isinstance(per_step_retry_policy, Mapping)
        or any(
            not isinstance(step_id, str)
            or not step_id
            or not isinstance(retry_policy, Mapping)
            or set(retry_policy) != {"max_retries"}
            or type(retry_policy["max_retries"]) is not int
            or retry_policy["max_retries"] < 0
            for step_id, retry_policy in per_step_retry_policy.items()
        )
    ):
        raise SandboxError("completed leg manifest execution policy is invalid")
    source_snapshot_manifest_path = output_dir / "source_snapshot_manifest.json"
    if (
        source_snapshot_manifest_path.is_symlink()
        or not source_snapshot_manifest_path.is_file()
        or stat.S_IMODE(source_snapshot_manifest_path.stat().st_mode) != 0o444
        or identity.get("source_snapshot_manifest_digest")
        != f"sha256:{sha256_file(source_snapshot_manifest_path)}"
    ):
        raise SandboxError("completed leg source snapshot is invalid")
    source_snapshot = source_snapshot_from_manifest(
        output_dir / "source-snapshot",
        read_json_object(
            source_snapshot_manifest_path, label="completed leg source snapshot manifest"
        ),
    )
    if source_snapshot.digest != identity.get("source_snapshot_digest"):
        raise SandboxError("completed leg source snapshot is invalid")
    from .public_benchmark_artifacts import _execution_policy_from_snapshot

    expected_execution_policy = _execution_policy_from_snapshot(
        source_snapshot,
        argparse.Namespace(case_timeout=manifest_deadline, workers=manifest_workers),
    )
    if execution_policy != expected_execution_policy:
        raise SandboxError("completed leg manifest execution policy is invalid")
    return benchmark, repeat_ordinal


def _validate_pending_release_legs(
    output_dir: Path,
    *,
    state_root: Path,
    release_plan: Sequence[Mapping[str, object]],
    completed_count: int,
    active_leg: object,
    allow_active_leg: bool,
) -> None:
    active_key: tuple[str, int] | None = None
    if active_leg is not None:
        if not allow_active_leg or not isinstance(active_leg, Mapping):
            raise SandboxError("bundle contains a partial active leg and cannot resume")
        next_item = (
            release_plan[completed_count]
            if completed_count < len(release_plan)
            else None
        )
        if next_item is None or not release_state.active_leg_matches_plan(
            active_leg, next_item
        ):
            raise SandboxError("bundle active leg is not the canonical next leg")
        active_key = (str(next_item["benchmark"]), int(next_item["repeat_ordinal"]))
    for item in release_plan[completed_count:]:
        benchmark = str(item["benchmark"])
        repeat_ordinal = int(item["repeat_ordinal"])
        if active_key == (benchmark, repeat_ordinal):
            continue
        if leg_output_dir(output_dir, benchmark, repeat_ordinal).exists():
            raise SandboxError("pending release leg already has partial artifacts")
        if (state_root / f"{benchmark}-r{repeat_ordinal}").exists():
            raise SandboxError("pending release leg already has partial state")


def validate_release_resume(
    output_dir: Path,
    *,
    state_root: Path,
    identity: Mapping[str, object],
    release_plan: Sequence[Mapping[str, object]],
    locked_case_manifests: Mapping[str, object],
    evaluator_identities: Mapping[str, object] | None = None,
    allow_active_leg: bool = False,
) -> set[tuple[str, int]]:
    manifest = read_json_object(
        output_dir / "bundle_manifest.json",
        label="bundle manifest",
    )
    if manifest.get("record_kind") != "text2sql_public_benchmark_bundle_manifest":
        raise SandboxError("bundle manifest kind is invalid")
    bundle_id = manifest.get("bundle_id")
    if not isinstance(bundle_id, str) or not bundle_id:
        raise SandboxError("bundle manifest bundle_id is invalid")
    if manifest.get("release_plan") != list(release_plan):
        raise SandboxError("bundle manifest release plan mismatch")
    if manifest.get("state_root") != str(state_root.resolve()):
        raise SandboxError("bundle resume state_root mismatch")
    if manifest.get("case_manifests") != locked_case_manifests:
        raise SandboxError("bundle resume case manifests differ from release lock")
    if any(manifest.get(name) != value for name, value in identity.items()):
        raise SandboxError("bundle resume identity mismatch")
    state = read_json_object(
        output_dir / "bundle_state.json",
        label="bundle state",
    )
    if state.get("record_kind") != "text2sql_public_benchmark_bundle_state":
        raise SandboxError("bundle state kind is invalid")
    if state.get("bundle_id") != bundle_id:
        raise SandboxError("bundle state bundle_id mismatch")
    if state.get("release_plan") != list(release_plan):
        raise SandboxError("bundle state release plan mismatch")
    raw_completed = state.get("completed_legs")
    if not isinstance(raw_completed, list):
        raise SandboxError("bundle completed leg inventory is invalid")
    completed: set[tuple[str, int]] = set()
    for index, record in enumerate(raw_completed):
        if not isinstance(record, Mapping):
            raise SandboxError("bundle completed leg record is invalid")
        plan_item = release_plan[index] if index < len(release_plan) else None
        if plan_item is None:
            raise SandboxError("completed release legs are not a canonical prefix")
        completed.add(
            _validate_completed_leg(
                output_dir,
                record=record,
                plan_item=plan_item,
                bundle_id=bundle_id,
                identity=identity,
                locked_case_manifests=locked_case_manifests,
                evaluator_identities=evaluator_identities,
            )
        )
    _validate_pending_release_legs(
        output_dir,
        state_root=state_root,
        release_plan=release_plan,
        completed_count=len(raw_completed),
        active_leg=state.get("active_leg"),
        allow_active_leg=allow_active_leg,
    )
    return completed


def load_release_policy(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise SandboxError("canonical benchmark release policy is missing or unsafe")
    payload = read_json_object(path, label="canonical benchmark release policy")
    if payload.get("record_kind") != "text2sql_public_benchmark_release_policy":
        raise SandboxError("canonical benchmark release policy kind is invalid")
    return payload


def load_release_input_lock(path: Path) -> dict[str, object]:
    safe_path = resolve_safe_regular_file(path, label="release input lock")
    if stat.S_IMODE(safe_path.stat(follow_symlinks=False).st_mode) != 0o444:
        raise SandboxError("release input lock must be sealed with mode 0444")
    return read_json_object(safe_path, label="release input lock")


def require_release_arguments(args: argparse.Namespace) -> None:
    required = {
        "--bird-root": getattr(args, "bird_root", None),
        "--spider-root": getattr(args, "spider_root", None),
        "--spider-sqlite-root": getattr(args, "spider_sqlite_root", None),
        "--spider-database-map": getattr(args, "spider_database_map", None),
        "--model-api-base": getattr(args, "model_api_base", None),
        "--model-backend-id": getattr(args, "model_backend_id", None),
    }
    missing = [name for name, value in required.items() if value is None or value == ""]
    if missing:
        raise ValueError("release mode requires " + ", ".join(missing))


def write_release_input_lock(
    args: argparse.Namespace,
    *,
    policy_path: Path,
    load_bird_cases: Callable[[Path], Sequence[ReleaseCase]],
    load_spider_cases: Callable[[Path, Path, Path], Sequence[ReleaseCase]],
    prove_git: Callable[..., Mapping[str, object]] = verified_git_provenance,
) -> int:
    require_release_arguments(args)
    target = args.create_release_lock
    policy = load_release_policy(policy_path)
    lock, _binding = inspect_release_inputs(
        args,
        policy=policy,
        load_bird_cases=load_bird_cases,
        load_spider_cases=load_spider_cases,
        prove_git=prove_git,
    )
    write_release_input_lock_new(target, lock)
    print(f"release input lock written: {target}", flush=True)
    return 0


def create_canonical_output_dir(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise SandboxError("canonical output directory must be new")
    output_dir = path.resolve()
    if output_dir.exists():
        raise SandboxError("canonical output directory must be new")
    output_dir.mkdir(parents=True)
    return output_dir


def write_source_snapshot_artifact(path: Path, snapshot: SourceSnapshot) -> str:
    write_json_atomically(path, source_snapshot_manifest(snapshot))
    path.chmod(0o444)
    return f"sha256:{sha256_file(path)}"


def configuration_sources(
    root: Path,
    paths: Sequence[Path],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for relative_path in paths:
        path = root / relative_path
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(
                f"runtime configuration is missing or unsafe: {relative_path}"
            )
        content = path.read_bytes()
        records.append(
            {
                "path": str(relative_path),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    return records


def canonical_configuration_digest(
    *,
    configuration_sources: Sequence[Mapping[str, object]],
    canonical_environment: Mapping[str, object],
    model_identity: Mapping[str, object],
    evaluator_identities: Mapping[str, object],
) -> str:
    return json_digest(
        {
            "configuration_sources": configuration_sources,
            "canonical_environment": canonical_environment,
            "model_identity": model_identity,
            "evaluator_identities": evaluator_identities,
        }
    )


def release_bundle_identity(
    *,
    lock: Mapping[str, object],
    snapshot: SourceSnapshot,
    source_snapshot_manifest_digest: str,
    configuration_digest: str,
) -> dict[str, object]:
    environment_digest = lock.get("canonical_environment_digest")
    model_identity_digest = lock.get("model_identity_digest")
    if not isinstance(environment_digest, str) or not isinstance(
        model_identity_digest, str
    ):
        raise SandboxError("release lock runtime identity is invalid")
    identity = {
        "release_lock_digest": json_digest(lock),
        "source_snapshot_digest": snapshot.digest,
        "source_snapshot_manifest_digest": source_snapshot_manifest_digest,
        "configuration_digest": configuration_digest,
        "canonical_environment_digest": environment_digest,
        "model_identity_digest": model_identity_digest,
    }
    return identity


def release_leg_args(
    args: argparse.Namespace,
    *,
    plan_item: Mapping[str, object],
    output_dir: Path,
    state_root: Path,
    shared_schema_memory_base: Path | None = None,
    release_progress_path: Path,
    snapshot: SourceSnapshot,
    source_snapshot_manifest_digest: str,
    configuration_sources: Sequence[Mapping[str, object]],
    configuration_digest: str,
    bundle_id: str,
    release_identity: Mapping[str, object],
    lock: Mapping[str, object],
    policy: Mapping[str, object],
    frozen_inputs: FrozenReleaseInputs,
) -> argparse.Namespace:
    benchmark = str(plan_item["benchmark"])
    values = vars(args).copy()
    values.update(
        {
            "dataset": benchmark,
            "dataset_root": args.bird_root if benchmark == "bird" else args.spider_root,
            "sqlite_root": (args.spider_sqlite_root if benchmark == "spider" else None),
            "database_map": (
                args.spider_database_map if benchmark == "spider" else None
            ),
            "output_dir": output_dir,
            "sandbox_state_root": state_root,
            "shared_schema_memory_base": shared_schema_memory_base,
            "release_progress_path": release_progress_path,
            "release_bundle_mode": True,
            "repeat_ordinal": int(plan_item["repeat_ordinal"]),
            "seed": int(plan_item["seed"]),
            "execution_mode": "bwrap",
            "workers": 1,
            "limit": None,
            "case_id": [],
            "ordinal_start": None,
            "ordinal_stop": None,
            "diagnostic_subset": False,
            "sandbox_env": [],
            "canonical_runtime_env": dict(lock["canonical_environment"]),
            "canonical_release_leg": True,
            "expected_case_count": int(
                release_dataset_policy(policy, benchmark)["case_count"]
            ),
            "release_snapshot": snapshot,
            "source_snapshot_manifest_digest": source_snapshot_manifest_digest,
            "release_configuration_sources": list(configuration_sources),
            "release_configuration_digest": configuration_digest,
            "release_bundle_id": bundle_id,
            "release_identity": dict(release_identity),
            "release_model_identity": dict(lock["model_identity"]),
            "release_evaluator_identities": dict(lock["evaluator_identities"]),
            "release_evaluator_identity": dict(lock["evaluator_identities"][benchmark]),
            "release_cases": frozen_inputs.cases_for(benchmark),
            "release_case_manifest": frozen_inputs.case_manifests[benchmark],
            "release_database_digests": (
                frozen_inputs.database_digests_by_benchmark[benchmark]
            ),
            "early_stop_policy": lock.get("early_stop_policy"),
        }
    )
    return argparse.Namespace(**values)


def run_release_bundle(
    args: argparse.Namespace,
    token: str,
    *,
    repo_root: Path,
    policy_path: Path,
    configuration_paths: Sequence[Path],
    load_bird_cases: Callable[[Path], Sequence[ReleaseCase]],
    load_spider_cases: Callable[[Path, Path, Path], Sequence[ReleaseCase]],
    run_leg: Callable[[argparse.Namespace, str], int],
    prove_git: Callable[..., Mapping[str, object]] = verified_git_provenance,
) -> int:
    """Delegate release progression to the transactional coordinator."""
    from .release_coordinator import run_release_bundle as coordinate_release

    return coordinate_release(
        args,
        token,
        repo_root=repo_root,
        policy_path=policy_path,
        configuration_paths=configuration_paths,
        load_bird_cases=load_bird_cases,
        load_spider_cases=load_spider_cases,
        run_leg=run_leg,
        prove_git=prove_git,
    )
