"""Coordinator facade for canonical public Text-to-SQL release bundles."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import public_benchmark_release as support
from . import release_state
from .release_governance import governance_event_paths
from .sandbox import SandboxError


def _authenticate_paused_prefix(
    progress_store: release_state.ReleaseProgressStore,
    leg_dir: Path,
    candidate: Mapping[str, object],
    *,
    candidate_sha256: str,
) -> None:
    benchmark = candidate.get("benchmark")
    repeat_ordinal = candidate.get("repeat_ordinal")
    completed_case_keys = candidate.get("completed_case_keys")
    completed_case_count = candidate.get("completed_case_count")
    observations_sha256 = candidate.get("observations_sha256")
    if (
        not isinstance(benchmark, str)
        or not isinstance(repeat_ordinal, int)
        or isinstance(repeat_ordinal, bool)
        or not isinstance(completed_case_keys, list)
        or any(not isinstance(item, str) for item in completed_case_keys)
        or not isinstance(completed_case_count, int)
        or isinstance(completed_case_count, bool)
        or not isinstance(observations_sha256, str)
    ):
        raise SandboxError("paused candidate prefix authority is invalid")
    try:
        progress_store.authenticate_candidate_prefix(
            benchmark=benchmark,
            repeat_ordinal=repeat_ordinal,
            candidate_sha256=candidate_sha256,
            completed_case_keys=completed_case_keys,
            completed_case_count=completed_case_count,
            observations_sha256=observations_sha256,
        )
        progress_store.materialize_observations(
            leg_dir / "observations.jsonl",
            benchmark=benchmark,
            repeat_ordinal=repeat_ordinal,
        )
    except release_state.ReleaseProgressError as exc:
        raise SandboxError(str(exc)) from exc


def _write_deferred_leg_handshake(
    *,
    leg_dir: Path,
    benchmark: str,
    repeat_ordinal: int,
    seed: int,
    bundle_id: str,
    identity: Mapping[str, object],
    configuration_digest: str,
    progress_store: release_state.ReleaseProgressStore,
) -> str:
    from .public_benchmark_artifacts import _write_artifact_handshake

    source_digest = identity.get("source_snapshot_digest")
    snapshot_manifest_digest = identity.get("source_snapshot_manifest_digest")
    if not isinstance(source_digest, str) or not isinstance(
        snapshot_manifest_digest, str
    ):
        raise SandboxError("deferred release handshake identity is invalid")
    return _write_artifact_handshake(
        leg_dir,
        benchmark=benchmark,
        repeat_ordinal=repeat_ordinal,
        bundle_id=bundle_id,
        case_manifest_digest=(
            f"sha256:{support.sha256_file(leg_dir / 'case_manifest.json')}"
        ),
        snapshot_digest=source_digest,
        configuration_digest=configuration_digest,
        seed=seed,
        run_scope="full_release",
        execution_mode="canonical_release",
        source_snapshot_manifest_digest=snapshot_manifest_digest,
        governance_events=progress_store.governance_events(
            benchmark=benchmark,
            repeat_ordinal=repeat_ordinal,
        ),
        additional_artifact_names=support.EVALUATED_LEG_ARTIFACTS,
    )


def recover_active_leg(
    output_dir: Path,
    *,
    state: dict[str, object],
    identity: Mapping[str, object],
    configuration_digest: str,
    progress_store: release_state.ReleaseProgressStore,
    persist_state: bool = True,
) -> bool:
    """Recover a closed early stop, or permit only its exact partial resume."""
    active = state.get("active_leg")
    if active is None:
        return False
    if not isinstance(active, Mapping):
        raise SandboxError("bundle active leg is invalid")
    benchmark, repeat_ordinal, _seed = release_state.active_leg_key(active)
    leg_dir = support.leg_output_dir(output_dir, benchmark, repeat_ordinal)
    progress = progress_store.progress()
    if progress.prefix_case_count is None:
        return True
    candidate_path = leg_dir / governance_event_paths(
        "mid_repeat", progress.prefix_case_count
    )["candidate_path"]
    candidate_exists = candidate_path.exists() or candidate_path.is_symlink()
    if not candidate_exists:
        raise SandboxError("early-stop candidate is missing")
    if candidate_path.is_symlink() or not candidate_path.is_file() or (
        candidate_path.stat().st_mode & 0o777
    ) != 0o444:
        raise SandboxError("early-stop candidate is not sealed")
    recovered_active = release_state.awaiting_repair_leg(
        active, f"sha256:{support.sha256_file(candidate_path)}"
    )
    candidate = support.validate_paused_candidate(candidate_path, recovered_active)
    _authenticate_paused_prefix(
        progress_store,
        leg_dir,
        candidate,
        candidate_sha256=f"sha256:{support.sha256_file(candidate_path)}",
    )
    support.validate_paused_leg_inputs(
        leg_dir,
        candidate,
        identity=identity,
        configuration_digest=configuration_digest,
    )
    state["status"] = "AWAITING_REPAIR_DECISION"
    state["active_leg"] = recovered_active
    if persist_state:
        support.write_bundle_state(output_dir / "bundle_state.json", state)
    return False


def run_release_bundle(
    args: argparse.Namespace,
    token: str,
    *,
    repo_root: Path,
    policy_path: Path,
    configuration_paths: Sequence[Path],
    load_bird_cases: Callable[[Path], Sequence[support.ReleaseCase]],
    load_spider_cases: Callable[[Path, Path, Path], Sequence[support.ReleaseCase]],
    run_leg: Callable[[argparse.Namespace, str], int],
    prove_git: Callable[..., Mapping[str, object]] = support.verified_git_provenance,
) -> int:
    """Delegate execution to bounded lifecycle stages."""
    from .release_bundle_execution import execute_release_bundle

    return execute_release_bundle(
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
