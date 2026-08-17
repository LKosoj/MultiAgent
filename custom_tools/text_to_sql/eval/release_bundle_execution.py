"""Bounded execution stages for canonical public benchmark bundles."""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
from typing import Callable, Mapping, Sequence
import uuid

from . import public_benchmark_release as support
from . import official_evaluator_bridge
from . import release_diagnostics
from . import release_state
from .release_coordinator import (
    _authenticate_paused_prefix,
    _write_deferred_leg_handshake,
)
from .release_diagnostics import (
    DiagnosticArtifactError,
    finalize_continue_decision,
    finalize_partial_stop,
    finalize_post_repeat_stop,
    seal_existing_file,
    validate_terminal_artifacts,
    write_json_new_or_identical_sealed,
)
from .release_governance import governance_event_paths
from .sandbox import SandboxError

class ReleaseBundleExecution:
    """Execute one canonical release through small, explicit lifecycle stages."""

    def __init__(
        self,
        args: argparse.Namespace,
        token: str,
        *,
        repo_root: Path,
        policy_path: Path,
        configuration_paths: Sequence[Path],
        load_bird_cases: Callable[[Path], Sequence[support.ReleaseCase]],
        load_spider_cases: Callable[
            [Path, Path, Path], Sequence[support.ReleaseCase]
        ],
        run_leg: Callable[[argparse.Namespace, str], int],
        prove_git: Callable[..., Mapping[str, object]],
    ) -> None:
        self.args = args
        self.token = token
        self.repo_root = repo_root
        self.policy_path = policy_path
        self.configuration_paths = configuration_paths
        self.load_bird_cases = load_bird_cases
        self.load_spider_cases = load_spider_cases
        self.run_leg = run_leg
        self.prove_git = prove_git
        self.progress_store: release_state.ReleaseProgressStore | None = None
        self.coordinator: release_state.ReleaseBundleCoordinator | None = None
        self.resuming_active_leg = False

    def run(self) -> int:
        self._load_and_validate_inputs()
        self._prepare_snapshot()
        self._bind_release_identity()
        if self.resume:
            result = self._resume_bundle()
        else:
            self._prepare_schema_memory_seed()
            result = self._initialize_bundle()
        if result is not None:
            return result
        self._ensure_progress_authority()
        self._reconcile_state()
        result = self._execute_release_legs()
        if result is not None:
            return result
        return self._finalize_bundle()

    def _load_and_validate_inputs(self) -> None:
        support.require_release_arguments(self.args)
        support.validate_canonical_workers(self.args.workers)
        if self.args.output_dir is None or self.args.sandbox_state_root is None:
            raise ValueError(
                "canonical release requires --output-dir and --sandbox-state-root"
            )
        if self.args.sandbox_secret_dir is None:
            raise ValueError("canonical release requires --sandbox-secret-dir")
        overrides = {
            "--dataset": self.args.dataset,
            "--dataset-root": self.args.dataset_root,
            "--repeat-ordinal": (
                self.args.repeat_ordinal
                if self.args.repeat_ordinal not in (None, 1)
                else None
            ),
            "--seed": self.args.seed,
            "--limit": self.args.limit,
            "--case-id": self.args.case_id,
            "--ordinal-start": self.args.ordinal_start,
            "--ordinal-stop": self.args.ordinal_stop,
            "--diagnostic-subset": self.args.diagnostic_subset,
            "--sandbox-env": self.args.sandbox_env,
        }
        used_overrides = [name for name, value in overrides.items() if value]
        if used_overrides:
            raise ValueError(
                "canonical release rejects diagnostic overrides: "
                + ", ".join(used_overrides)
            )
        self.policy = support.load_release_policy(self.policy_path)
        self.lock = support.load_release_input_lock(self.args.release_lock)
        self.frozen_inputs = self._validate_frozen_inputs()
        identities = self.lock.get("evaluator_identities")
        if (
            not isinstance(identities, Mapping)
            or set(identities) != set(support.CANONICAL_RELEASE_DATASET_ORDER)
            or any(not isinstance(item, Mapping) for item in identities.values())
        ):
            raise SandboxError("release lock evaluator identities are invalid")
        self.evaluator_identities = identities
        barrier = self.lock.get("post_repeat_evaluation_barrier", False)
        if not isinstance(barrier, bool):
            raise SandboxError("release lock post-repeat evaluation barrier is invalid")
        self.post_repeat_evaluation_barrier = barrier
        self.release_plan = support.build_release_plan(self.policy)
        self.output_dir = self.args.output_dir.resolve()
        self.state_root = self.args.sandbox_state_root.resolve()
        self.resume = bool(self.args.resume_release)

    def _validate_frozen_inputs(self) -> Mapping[str, object]:
        return support.validate_release_input_lock(
            self.args,
            self.lock,
            policy=self.policy,
            load_bird_cases=self.load_bird_cases,
            load_spider_cases=self.load_spider_cases,
            prove_git=self.prove_git,
        )

    def _prepare_snapshot(self) -> None:
        if self.resume:
            if self.output_dir.is_symlink() or not self.output_dir.is_dir():
                raise SandboxError("resume bundle output is missing or unsafe")
            if self.state_root.is_symlink() or not self.state_root.is_dir():
                raise SandboxError("resume bundle state root is missing or unsafe")
            manifest_path = self.output_dir / "source_snapshot_manifest.json"
            manifest = support.read_json_object(
                manifest_path, label="source snapshot manifest"
            )
            if manifest_path.stat().st_mode & 0o777 != 0o444:
                raise SandboxError("resume source snapshot manifest is not sealed")
            self.snapshot = support.source_snapshot_from_manifest(
                self.output_dir / "source-snapshot", manifest
            )
            self.source_snapshot_manifest_digest = (
                f"sha256:{support.sha256_file(manifest_path)}"
            )
            return
        self.output_dir = support.create_canonical_output_dir(self.output_dir)
        self.output_dir.chmod(0o700)
        if self.state_root.exists() or self.state_root.is_symlink():
            raise SandboxError("canonical release state root must be new")
        self.state_root.mkdir(parents=True, mode=0o700)
        self.state_root.chmod(0o700)
        self.snapshot = support.create_source_snapshot(
            self.repo_root,
            self.output_dir / "source-snapshot",
            allowed_paths=support.CANONICAL_RUNTIME_SOURCE_PATHS,
        )
        self.source_snapshot_manifest_digest = support.write_source_snapshot_artifact(
            self.output_dir / "source_snapshot_manifest.json", self.snapshot
        )

    def _bind_release_identity(self) -> None:
        self.frozen_configuration_sources = support.configuration_sources(
            self.snapshot.root, self.configuration_paths
        )
        self.configuration_digest = support.canonical_configuration_digest(
            configuration_sources=self.frozen_configuration_sources,
            canonical_environment=self.lock["canonical_environment"],
            model_identity=self.lock["model_identity"],
            evaluator_identities=self.evaluator_identities,
        )
        self.identity = support.release_bundle_identity(
            lock=self.lock,
            snapshot=self.snapshot,
            source_snapshot_manifest_digest=self.source_snapshot_manifest_digest,
            configuration_digest=self.configuration_digest,
        )
        manifests = self.lock.get("case_manifests")
        if not isinstance(manifests, Mapping):
            raise SandboxError("release lock case manifests are invalid")
        self.raw_case_manifests = manifests
        self.release_progress_path = self.state_root / "release_progress.sqlite3"

    def _prepare_schema_memory_seed(self) -> None:
        """Copy the previous release's schema memory once into this release."""

        source_identity = self._schema_memory_source_identity()
        source_root = Path(source_identity["root"])
        if self._schema_memory_digest(source_root) != source_identity["digest"]:
            raise SandboxError("schema-memory source changed after lock creation")
        destination = self.state_root / "schema-memory"
        if destination.exists() or destination.is_symlink():
            raise SandboxError("release schema-memory root already exists")
        shutil.copytree(source_root, destination)
        support.release_inputs.filter_schema_memory_copy(destination)
        copied_digest = self._schema_memory_digest(destination)
        if copied_digest != source_identity["digest"]:
            raise SandboxError("copied schema-memory does not match release lock")
        self.schema_memory_seed = {
            "source_root": source_identity["root"],
            "source_digest": source_identity["digest"],
            "copied_digest": copied_digest,
        }

    def _schema_memory_source_identity(self) -> dict[str, str]:
        value = self.lock.get("schema_memory_source")
        if (
            not isinstance(value, Mapping)
            or set(value) != {"root", "digest"}
            or not isinstance(value.get("root"), str)
            or not isinstance(value.get("digest"), str)
        ):
            raise SandboxError("release schema-memory source identity is invalid")
        return {"root": value["root"], "digest": value["digest"]}

    def _verify_resumed_schema_memory(
        self, bundle_manifest: Mapping[str, object]
    ) -> None:
        source_identity = self._schema_memory_source_identity()
        expected_seed = {
            "source_root": source_identity["root"],
            "source_digest": source_identity["digest"],
            "copied_digest": source_identity["digest"],
        }
        if bundle_manifest.get("schema_memory_seed") != expected_seed:
            raise SandboxError("resume schema-memory seed identity mismatch")
        root = self.state_root / "schema-memory"
        if root.is_symlink() or not root.is_dir():
            raise SandboxError("resume schema-memory root is missing or unsafe")

    @staticmethod
    def _schema_memory_digest(root: Path) -> str:
        return support.release_inputs.schema_memory_source_identity(root)["digest"]

    def _initialize_bundle(self) -> None:
        support.write_release_input_lock_new(
            self.output_dir / "release_input_lock.json", self.lock
        )
        self.bundle_id = f"text2sql-public-release-{uuid.uuid4().hex}"
        bundle_manifest: dict[str, object] = {
            "schema_version": 1,
            "record_kind": "text2sql_public_benchmark_bundle_manifest",
            "bundle_id": self.bundle_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "execution_mode": "canonical_release",
            "state_root": str(self.state_root),
            "release_plan": self.release_plan,
            "case_manifests": self.raw_case_manifests,
            "canonical_environment": self.lock["canonical_environment"],
            "model_identity": self.lock["model_identity"],
            "evaluator_identities": self.evaluator_identities,
            "configuration_sources": self.frozen_configuration_sources,
            "schema_memory_seed": self.schema_memory_seed,
            **self.identity,
        }
        support.write_json_new_sealed(
            self.output_dir / "bundle_manifest.json", bundle_manifest
        )
        self.state = {
            "schema_version": 1,
            "record_kind": "text2sql_public_benchmark_bundle_state",
            "bundle_id": self.bundle_id,
            "release_plan": self.release_plan,
            "active_leg": None,
            "completed_legs": [],
            "status": "running",
        }
        support.write_bundle_state(self.output_dir / "bundle_state.json", self.state)
        return None

    def _resume_bundle(self) -> int | None:
        frozen_lock_path = self.output_dir / "release_input_lock.json"
        frozen_lock = support.load_release_input_lock(frozen_lock_path)
        if frozen_lock_path.stat().st_mode & 0o777 != 0o444:
            raise SandboxError("resume release input lock is not sealed")
        if frozen_lock != self.lock:
            raise SandboxError("resume release input lock changed")
        manifest_path = self.output_dir / "bundle_manifest.json"
        bundle_manifest = support.read_json_object(
            manifest_path, label="bundle manifest"
        )
        if manifest_path.stat().st_mode & 0o777 != 0o444:
            raise SandboxError("resume bundle manifest is not sealed")
        self._verify_resumed_schema_memory(bundle_manifest)
        self.bundle_id = str(bundle_manifest["bundle_id"])
        if self.release_progress_path.is_symlink() or not self.release_progress_path.is_file():
            raise SandboxError("resume release progress store is missing or unsafe")
        self.progress_store = release_state.ReleaseProgressStore(
            self.release_progress_path
        )
        self.progress_store.bind_bundle(
            bundle_id=self.bundle_id,
            release_lock_digest=support.json_digest(self.lock),
            release_plan_digest=support.json_digest(self.release_plan),
        )
        self.state = support.read_json_object(
            self.output_dir / "bundle_state.json", label="bundle state"
        )
        self.coordinator = release_state.ReleaseBundleCoordinator(
            self.progress_store,
            bundle_id=self.bundle_id,
            release_plan=self.release_plan,
        )
        self._reconcile_state()
        result = self._dispatch_resume_state()
        if result is not None:
            return result
        self.state = support.read_json_object(
            self.output_dir / "bundle_state.json", label="bundle state"
        )
        return None

    def _dispatch_resume_state(self) -> int | None:
        assert self.progress_store is not None
        progress = self.progress_store.progress()
        if progress.phase is release_state.ReleasePhase.COMPLETE:
            return self._validate_complete_resume()
        if progress.phase is release_state.ReleasePhase.ACTIVE_LEG:
            result = self._resume_active_leg()
            if result is not None:
                return result
        elif progress.phase is release_state.ReleasePhase.CONTINUING_ACTIVE_LEG:
            self._resume_continuing_leg(progress)
        if self.state.get("status") == "AWAITING_POST_REPEAT_EVALUATION":
            result = self._resume_post_repeat_evaluation()
            if result is not None:
                return result
        if self.state.get("status") == "AWAITING_POST_REPEAT_REPAIR_DECISION":
            result = self._resume_post_repeat_decision()
            if result is not None:
                return result
        terminal_result = self._validate_diagnostic_resume()
        if terminal_result is not None:
            return terminal_result
        if self.state.get("status") == "AWAITING_REPAIR_DECISION":
            return self._resume_mid_repeat_decision()
        self._validate_release_resume()
        return None

    def _validate_complete_resume(self) -> int:
        assert self.progress_store is not None
        self._validate_release_resume()
        expected = self.progress_store.terminal_artifact_digests()
        try:
            validate_terminal_artifacts(
                self.output_dir,
                names=tuple(expected),
                expected_sha256=expected,
            )
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        return int(self.state["return_code"])

    def _resume_active_leg(self) -> int | None:
        assert self.progress_store is not None
        assert self.coordinator is not None
        self.resuming_active_leg = support.recover_active_leg(
            self.output_dir,
            state=self.state,
            identity=self.identity,
            configuration_digest=self.configuration_digest,
            progress_store=self.progress_store,
            persist_state=False,
        )
        if self.state.get("status") != "AWAITING_REPAIR_DECISION":
            return None
        active = self.state.get("active_leg")
        if not isinstance(active, Mapping):
            raise SandboxError("recovered release leg is invalid")
        progress = self.progress_store.progress()
        if progress.prefix_case_count is None:
            raise SandboxError("recovered release candidate count is invalid")
        candidate_path = support.leg_output_dir(
            self.output_dir,
            str(active["benchmark"]),
            int(active["repeat_ordinal"]),
        ) / governance_event_paths(
            "mid_repeat", progress.prefix_case_count
        )["candidate_path"]
        candidate = support.read_json_object(
            candidate_path, label="early-stop candidate"
        )
        self.progress_store.pause_for_candidate(
            candidate_sha256=f"sha256:{support.sha256_file(candidate_path)}",
            prefix_case_count=int(candidate["completed_case_count"]),
        )
        self._reconcile_state()
        if getattr(self.args, "repair_decision", None) is None:
            return 2
        return None

    def _resume_continuing_leg(self, progress: release_state.ReleaseProgress) -> None:
        assert self.progress_store is not None
        if (
            progress.active_benchmark is None
            or progress.active_repeat_ordinal is None
            or progress.candidate_sha256 is None
            or progress.decision_sha256 is None
        ):
            raise SandboxError("continued release authority is incomplete")
        leg_dir = support.leg_output_dir(
            self.output_dir,
            progress.active_benchmark,
            progress.active_repeat_ordinal,
        )
        if progress.prefix_case_count is None:
            raise SandboxError("continued release candidate count is invalid")
        candidate_path = leg_dir / governance_event_paths(
            "mid_repeat", progress.prefix_case_count
        )["candidate_path"]
        candidate = support.validate_paused_candidate(
            candidate_path, {"candidate_sha256": progress.candidate_sha256}
        )
        _authenticate_paused_prefix(
            self.progress_store,
            leg_dir,
            candidate,
            candidate_sha256=progress.candidate_sha256,
        )
        support.validate_paused_leg_inputs(
            leg_dir,
            candidate,
            identity=self.identity,
            configuration_digest=self.configuration_digest,
        )
        events = self.progress_store.governance_events(
            benchmark=progress.active_benchmark,
            repeat_ordinal=progress.active_repeat_ordinal,
        )
        mid_events = [event for event in events if event.get("event_kind") == "mid_repeat"]
        if not mid_events:
            raise SandboxError("continued release governance is incomplete")
        try:
            for event in mid_events:
                completed_case_count = event.get("completed_case_count")
                if not isinstance(completed_case_count, int):
                    raise SandboxError("continued release governance is incomplete")
                release_diagnostics.validate_continue_decision(
                    leg_dir,
                    event_kind="mid_repeat",
                    completed_case_count=completed_case_count,
                    expected={str(name): value for name, value in event.items()},
                )
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        decision = support.read_json_object(
            leg_dir / governance_event_paths(
                "mid_repeat", progress.prefix_case_count
            )["decision_path"],
            label="repair decision",
        )
        try:
            support.benchmark_reporting.parse_repair_decision(
                decision,
                candidate_sha256=progress.candidate_sha256.removeprefix("sha256:"),
            )
        except ValueError as exc:
            raise SandboxError("continued repair decision is invalid") from exc
        if decision.get("decision") != "CONTINUE":
            raise SandboxError("continued repair decision is not CONTINUE")
        self.resuming_active_leg = True

    def _resume_post_repeat_evaluation(self) -> int | None:
        assert self.progress_store is not None
        evaluation_leg = self.state.get("evaluation_leg")
        if not isinstance(evaluation_leg, Mapping):
            raise SandboxError("post-repeat evaluation has no completed leg")
        benchmark = evaluation_leg.get("benchmark")
        repeat_ordinal = evaluation_leg.get("repeat_ordinal")
        if not isinstance(benchmark, str) or not isinstance(repeat_ordinal, int):
            raise SandboxError("post-repeat evaluation leg identity is invalid")
        evaluator_identity = self.evaluator_identities.get(benchmark)
        if not isinstance(evaluator_identity, Mapping):
            raise SandboxError("post-repeat evaluator identity is invalid")
        leg_dir = support.leg_output_dir(
            self.output_dir, benchmark, repeat_ordinal
        )
        official_evaluator_bridge.run_for_release(self, leg_dir, benchmark, evaluator_identity)
        observations = support.validate_post_repeat_evaluation(
            leg_dir, evaluator_identity=evaluator_identity
        )
        policy = self.lock.get("early_stop_policy")
        if isinstance(policy, Mapping):
            parsed_policy = support.benchmark_reporting.parse_early_stop_policy(policy)
            candidate = support.benchmark_reporting.find_early_stop_candidate(
                observations, parsed_policy
            )
            if candidate is not None:
                self._pause_for_post_repeat_candidate(
                    leg_dir, evaluation_leg, candidate
                )
                return 2
        handshake_digest = self._write_deferred_handshake(
            leg_dir, evaluation_leg, benchmark, repeat_ordinal
        )
        self.progress_store.complete_post_repeat_evaluation(
            artifact_handshake_sha256=handshake_digest
        )
        self._reconcile_state()
        return None

    def _pause_for_post_repeat_candidate(
        self,
        leg_dir: Path,
        evaluation_leg: Mapping[str, object],
        candidate: dict[str, object],
    ) -> None:
        assert self.progress_store is not None
        evaluation = support.read_json_object(leg_dir / "evaluation_manifest.json", label="evaluation manifest")
        candidate.update(
            {
                "record_kind": "text2sql_public_benchmark_post_repeat_candidate",
                "bundle_id": self.bundle_id,
                "benchmark": evaluation_leg["benchmark"],
                "repeat_ordinal": evaluation_leg["repeat_ordinal"],
                "evaluator_receipt_sha256": f"sha256:{support.sha256_file(leg_dir / 'evaluator_receipt.json')}",
                "diagnostics_sha256": f"sha256:{support.sha256_file(leg_dir / 'diagnostics.jsonl')}",
                "case_manifest_sha256": f"sha256:{support.sha256_file(leg_dir / 'case_manifest.json')}",
                "manifest_sha256": f"sha256:{support.sha256_file(leg_dir / 'manifest.json')}",
                "observations_sha256": f"sha256:{support.sha256_file(leg_dir / 'observations.jsonl')}",
                "evaluation_manifest_sha256": f"sha256:{support.sha256_file(leg_dir / 'evaluation_manifest.json')}",
                "summary_sha256": evaluation["summary_sha256"],
                "score_sha256": evaluation["score_sha256"],
                "evaluator_input_sha256": evaluation["evaluator_input_sha256"],
            }
        )
        completed_case_count = candidate.get("completed_case_count")
        if not isinstance(completed_case_count, int) or isinstance(
            completed_case_count, bool
        ):
            raise SandboxError("post-repeat candidate count is invalid")
        candidate_path = leg_dir / governance_event_paths(
            "post_repeat", completed_case_count
        )["candidate_path"]
        try:
            support.benchmark_reporting.ensure_json_new_or_identical(
                candidate_path, candidate
            )
            candidate_digest = seal_existing_file(
                candidate_path, label="post-repeat candidate"
            )
        except ValueError as exc:
            raise SandboxError("post-repeat candidate output is invalid") from exc
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        self.progress_store.transition(
            expected=[release_state.ReleasePhase.AWAITING_POST_REPEAT_EVALUATION],
            target=release_state.ReleasePhase.AWAITING_POST_REPEAT_REPAIR_DECISION,
            candidate_sha256=candidate_digest,
            prefix_case_count=completed_case_count,
        )
        self.state["status"] = "AWAITING_POST_REPEAT_REPAIR_DECISION"
        self.state["evaluation_leg"] = {
            **dict(evaluation_leg),
            "candidate_sha256": candidate_digest,
        }
        self._write_state()

    def _resume_post_repeat_decision(self) -> int | None:
        assert self.progress_store is not None
        evaluation_leg = self.state.get("evaluation_leg")
        if not isinstance(evaluation_leg, Mapping):
            raise SandboxError("post-repeat repair decision is required")
        benchmark = evaluation_leg.get("benchmark")
        repeat_ordinal = evaluation_leg.get("repeat_ordinal")
        candidate_digest = evaluation_leg.get("candidate_sha256")
        if (
            not isinstance(benchmark, str)
            or not isinstance(repeat_ordinal, int)
            or not isinstance(candidate_digest, str)
        ):
            raise SandboxError("post-repeat candidate state is invalid")
        leg_dir = support.leg_output_dir(self.output_dir, benchmark, repeat_ordinal)
        progress = self.progress_store.progress()
        if progress.prefix_case_count is None:
            raise SandboxError("post-repeat candidate count is invalid")
        candidate_path = leg_dir / governance_event_paths(
            "post_repeat", progress.prefix_case_count
        )["candidate_path"]
        try:
            actual_candidate_digest = (
                release_diagnostics.digest_existing_sealed_file(
                    candidate_path, label="post-repeat candidate"
                )
            )
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        if candidate_digest != actual_candidate_digest:
            raise SandboxError("post-repeat candidate digest changed")
        candidate = support.read_json_object(candidate_path, label="post-repeat candidate")
        evaluator_identity = self.evaluator_identities.get(benchmark)
        if not isinstance(evaluator_identity, Mapping):
            raise SandboxError("post-repeat evaluator identity is invalid")
        support.validate_post_repeat_evaluation(
            leg_dir, evaluator_identity=evaluator_identity
        )
        try:
            release_diagnostics.validate_post_repeat_candidate_artifacts(
                leg_dir, candidate, expected_bundle_id=self.bundle_id,
                expected_benchmark=benchmark, expected_repeat_ordinal=repeat_ordinal,
            )
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        decision_path, decision, decision_digest = self._load_repair_decision(
            candidate_path, post_repeat=True
        )
        if decision is None:
            return 2
        if decision["decision"] == "STOP_AND_REPAIR":
            return self._stop_after_post_repeat(
                candidate_path,
                decision_path,
                decision_digest,
                candidate_digest,
                evaluation_leg,
            )
        try:
            event = finalize_continue_decision(
                leg_dir,
                event_kind="post_repeat",
                completed_case_count=progress.prefix_case_count,
            )
            self.progress_store.register_governance_event(event)
            handshake_digest = self._write_deferred_handshake(
                leg_dir, evaluation_leg, benchmark, repeat_ordinal
            )
            self.progress_store.complete_post_repeat_evaluation(
                artifact_handshake_sha256=handshake_digest
            )
        except (DiagnosticArtifactError, release_state.ReleaseProgressError) as exc:
            raise SandboxError(str(exc)) from exc
        self._reconcile_state()
        return None

    def _load_repair_decision(
        self, candidate_path: Path, *, post_repeat: bool
    ) -> tuple[Path, Mapping[str, object] | None, str]:
        supplied_path = getattr(self.args, "repair_decision", None)
        target = candidate_path.with_name("repair_decision.json")
        if supplied_path is None:
            if not target.exists():
                return target, None, ""
            supplied_path = target
        decision = support.read_json_object(supplied_path, label="repair decision")
        try:
            support.benchmark_reporting.parse_repair_decision(
                decision, candidate_sha256=support.sha256_file(candidate_path)
            )
            support.benchmark_reporting.ensure_json_new_or_identical(target, decision)
            decision_digest = seal_existing_file(target, label="repair decision")
        except ValueError as exc:
            label = "post-repeat repair decision" if post_repeat else "repair decision"
            raise SandboxError(f"{label} is invalid") from exc
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        return target, decision, decision_digest

    def _stop_after_post_repeat(
        self,
        candidate_path: Path,
        decision_path: Path,
        decision_digest: str,
        candidate_digest: str,
        evaluation_leg: Mapping[str, object],
    ) -> int:
        assert self.progress_store is not None
        if (
            self.progress_store.progress().phase
            is release_state.ReleasePhase.AWAITING_POST_REPEAT_REPAIR_DECISION
        ):
            self.progress_store.transition(
                expected=[release_state.ReleasePhase.AWAITING_POST_REPEAT_REPAIR_DECISION],
                target=release_state.ReleasePhase.FINALIZING_POST_REPEAT_STOP,
                decision_sha256=decision_digest,
            )
        try:
            artifacts = finalize_post_repeat_stop(
                support.leg_output_dir(
                    self.output_dir,
                    str(evaluation_leg["benchmark"]),
                    int(evaluation_leg["repeat_ordinal"]),
                ),
                candidate_path=candidate_path,
                decision_path=decision_path,
                release_plan=self.release_plan,
                evaluation_leg=evaluation_leg,
                expected_bundle_id=self.bundle_id,
            )
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        artifacts.update(
            {
                candidate_path.relative_to(
                    support.leg_output_dir(
                        self.output_dir,
                        str(evaluation_leg["benchmark"]),
                        int(evaluation_leg["repeat_ordinal"]),
                    )
                ).as_posix(): candidate_digest,
                decision_path.relative_to(
                    support.leg_output_dir(
                        self.output_dir,
                        str(evaluation_leg["benchmark"]),
                        int(evaluation_leg["repeat_ordinal"]),
                    )
                ).as_posix(): decision_digest,
            }
        )
        self.progress_store.seal_terminal_state(
            expected=release_state.ReleasePhase.FINALIZING_POST_REPEAT_STOP,
            target=release_state.ReleasePhase.DIAGNOSTIC_POST_REPEAT_STOP,
            artifact_sha256=artifacts,
        )
        self.state["status"] = "diagnostic_post_repeat_stop"
        self._write_state()
        return 2

    def _validate_diagnostic_resume(self) -> int | None:
        status = self.state.get("status")
        if status == "diagnostic_early_stop":
            active = self.state.get("active_leg")
            if not isinstance(active, Mapping):
                raise SandboxError("diagnostic early stop has no active leg")
            leg_dir = support.leg_output_dir(
                self.output_dir,
                str(active.get("benchmark")),
                int(active.get("repeat_ordinal")),
            )
            progress = self.progress_store.progress()
            if progress.prefix_case_count is None:
                raise SandboxError("diagnostic early-stop candidate count is invalid")
            candidate = support.validate_paused_candidate(
                leg_dir / governance_event_paths(
                    "mid_repeat", progress.prefix_case_count
                )["candidate_path"],
                active,
            )
            support.validate_paused_leg_inputs(
                leg_dir,
                candidate,
                identity=self.identity,
                configuration_digest=self.configuration_digest,
            )
            self._validate_terminal_in(leg_dir)
            return 2
        if status == "diagnostic_post_repeat_stop":
            self._validate_post_repeat_terminal()
            return 2
        return None

    def _validate_post_repeat_terminal(self) -> None:
        evaluation_leg = self.state.get("evaluation_leg")
        if not isinstance(evaluation_leg, Mapping):
            raise SandboxError("diagnostic post-repeat stop has no release leg")
        benchmark = str(evaluation_leg.get("benchmark"))
        leg_dir = support.leg_output_dir(
            self.output_dir, benchmark, int(evaluation_leg.get("repeat_ordinal"))
        )
        evaluator_identity = self.evaluator_identities.get(benchmark)
        if not isinstance(evaluator_identity, Mapping):
            raise SandboxError("post-repeat evaluator identity is invalid")
        support.validate_post_repeat_evaluation(
            leg_dir, evaluator_identity=evaluator_identity
        )
        self._validate_terminal_in(leg_dir)

    def _validate_terminal_in(self, directory: Path) -> None:
        assert self.progress_store is not None
        expected = self.progress_store.terminal_artifact_digests()
        try:
            validate_terminal_artifacts(
                directory, names=tuple(expected), expected_sha256=expected
            )
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc

    def _resume_mid_repeat_decision(self) -> int | None:
        assert self.progress_store is not None
        active = self.state.get("active_leg")
        if not isinstance(active, Mapping):
            raise SandboxError(
                "awaiting repair decision requires an immutable decision file"
            )
        leg_dir = support.leg_output_dir(
            self.output_dir,
            str(active.get("benchmark")),
            int(active.get("repeat_ordinal")),
        )
        progress = self.progress_store.progress()
        if progress.prefix_case_count is None:
            raise SandboxError("mid-repeat candidate count is invalid")
        candidate_path = leg_dir / governance_event_paths(
            "mid_repeat", progress.prefix_case_count
        )["candidate_path"]
        candidate = support.validate_paused_candidate(candidate_path, active)
        _authenticate_paused_prefix(
            self.progress_store,
            leg_dir,
            candidate,
            candidate_sha256=f"sha256:{support.sha256_file(candidate_path)}",
        )
        support.validate_paused_leg_inputs(
            leg_dir,
            candidate,
            identity=self.identity,
            configuration_digest=self.configuration_digest,
        )
        decision_path, decision, decision_digest = self._load_repair_decision(
            candidate_path, post_repeat=False
        )
        if decision is None:
            return 2
        candidate_digest = support.sha256_file(candidate_path)
        if decision["decision"] == "STOP_AND_REPAIR":
            return self._stop_mid_repeat(
                candidate_path,
                decision_path,
                decision_digest,
                candidate_digest,
                candidate,
                active,
            )
        try:
            event = finalize_continue_decision(
                leg_dir,
                event_kind="mid_repeat",
                completed_case_count=progress.prefix_case_count,
            )
            self.progress_store.register_governance_event(event)
            self.progress_store.continue_active_leg(
                candidate_sha256=f"sha256:{candidate_digest}",
                decision_sha256=decision_digest,
            )
        except (DiagnosticArtifactError, release_state.ReleaseProgressError) as exc:
            raise SandboxError(str(exc)) from exc
        self.state["status"] = "running"
        self.state["resume_active_leg"] = True
        self.resuming_active_leg = True
        self._write_state()
        return None

    def _stop_mid_repeat(
        self,
        candidate_path: Path,
        decision_path: Path,
        decision_digest: str,
        candidate_digest: str,
        candidate: Mapping[str, object],
        evaluation_leg: Mapping[str, object],
    ) -> int:
        assert self.progress_store is not None
        leg_dir = support.leg_output_dir(
            self.output_dir,
            str(evaluation_leg["benchmark"]),
            int(evaluation_leg["repeat_ordinal"]),
        )
        observations_path = leg_dir / "observations.jsonl"
        rows = [
            json.loads(line)
            for line in observations_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if (
            self.progress_store.progress().phase
            is release_state.ReleasePhase.AWAITING_REPAIR_DECISION
        ):
            self.progress_store.transition(
                expected=[release_state.ReleasePhase.AWAITING_REPAIR_DECISION],
                target=release_state.ReleasePhase.FINALIZING_PARTIAL_STOP,
                candidate_sha256=f"sha256:{candidate_digest}",
                decision_sha256=decision_digest,
            )
        self._seal_runner_logs(leg_dir)
        try:
            artifacts = finalize_partial_stop(
                leg_dir,
                candidate=candidate,
                candidate_path=candidate_path,
                decision_path=decision_path,
                observations=[row for row in rows if isinstance(row, Mapping)],
                classify=support.benchmark_reporting.failure_class,
                release_plan=self.release_plan,
                evaluation_leg=evaluation_leg,
            )
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        artifacts.update(
            {
                candidate_path.relative_to(leg_dir).as_posix(): (
                    f"sha256:{candidate_digest}"
                ),
                decision_path.relative_to(leg_dir).as_posix(): decision_digest,
            }
        )
        self.progress_store.seal_terminal_state(
            expected=release_state.ReleasePhase.FINALIZING_PARTIAL_STOP,
            target=release_state.ReleasePhase.DIAGNOSTIC_PARTIAL_STOP,
            artifact_sha256=artifacts,
        )
        self.state["status"] = "diagnostic_early_stop"
        self._write_state()
        return 2

    def _ensure_progress_authority(self) -> None:
        if self.progress_store is None:
            self.progress_store = release_state.ReleaseProgressStore(
                self.release_progress_path
            )
            self.progress_store.bind_bundle(
                bundle_id=self.bundle_id,
                release_lock_digest=support.json_digest(self.lock),
                release_plan_digest=support.json_digest(self.release_plan),
            )
        if self.coordinator is None:
            self.coordinator = release_state.ReleaseBundleCoordinator(
                self.progress_store,
                bundle_id=self.bundle_id,
                release_plan=self.release_plan,
            )

    def _reconcile_state(self) -> None:
        assert self.coordinator is not None
        try:
            self.state = self.coordinator.reconcile_public_state(self.state)
        except release_state.ReleaseProgressError as exc:
            raise SandboxError(str(exc)) from exc
        self._write_state()

    def _write_state(self) -> None:
        support.write_bundle_state(
            self.output_dir / "bundle_state.json", self.state
        )

    def _validate_release_resume(self, *, allow_active_leg: bool = False) -> set[tuple[str, int]]:
        return support.validate_release_resume(
            self.output_dir,
            state_root=self.state_root,
            identity=self.identity,
            release_plan=self.release_plan,
            locked_case_manifests=self.raw_case_manifests,
            evaluator_identities=self.lock.get("evaluator_identities"),
            allow_active_leg=allow_active_leg,
        )

    def _execute_release_legs(self) -> int | None:
        assert self.progress_store is not None
        completed = {
            (item.benchmark, item.repeat_ordinal)
            for item in self.progress_store.completed_legs()
        }
        raw_completed = self.state.get("completed_legs")
        if not isinstance(raw_completed, list):
            raise SandboxError("bundle completed leg inventory is invalid")
        for plan_item in self.release_plan:
            benchmark = str(plan_item["benchmark"])
            repeat_ordinal = int(plan_item["repeat_ordinal"])
            if (benchmark, repeat_ordinal) in completed:
                continue
            result = self._execute_one_leg(plan_item)
            if result is not None:
                return result
        return None

    def _execute_one_leg(self, plan_item: Mapping[str, object]) -> int | None:
        assert self.progress_store is not None
        benchmark = str(plan_item["benchmark"])
        repeat_ordinal = int(plan_item["repeat_ordinal"])
        self.frozen_inputs = self._validate_frozen_inputs()
        active_leg = self.state.get("active_leg")
        continuing = (
            self.resuming_active_leg
            and isinstance(active_leg, Mapping)
            and release_state.active_leg_matches_plan(active_leg, plan_item)
        )
        if not continuing:
            self.progress_store.start_leg(
                benchmark=benchmark,
                repeat_ordinal=repeat_ordinal,
                seed=int(plan_item["seed"]),
            )
            self._reconcile_state()
        leg_output = support.leg_output_dir(
            self.output_dir, benchmark, repeat_ordinal
        )
        leg_args = support.release_leg_args(
            self.args,
            plan_item=plan_item,
            output_dir=leg_output,
            state_root=self.state_root / f"{benchmark}-r{repeat_ordinal}",
            shared_schema_memory_base=self.state_root / "schema-memory",
            release_progress_path=self.release_progress_path,
            snapshot=self.snapshot,
            source_snapshot_manifest_digest=self.source_snapshot_manifest_digest,
            configuration_sources=self.frozen_configuration_sources,
            configuration_digest=self.configuration_digest,
            bundle_id=self.bundle_id,
            release_identity=self.identity,
            lock=self.lock,
            policy=self.policy,
            frozen_inputs=self.frozen_inputs,
        )
        leg_args.resume_partial_leg = continuing or bool(
            self.state.pop("resume_active_leg", False)
        )
        leg_args.defer_artifact_handshake = self.post_repeat_evaluation_barrier
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return_code = self.run_leg(leg_args, self.token)
        self._write_runner_logs(
            leg_output,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            append=continuing,
        )
        if return_code == 2:
            return self._pause_after_early_stop(leg_output)
        self._seal_runner_logs(leg_output)
        if self.post_repeat_evaluation_barrier:
            self.progress_store.defer_leg_for_post_repeat(return_code=return_code)
            self._reconcile_state()
            return 3
        handshake_path = leg_output / "artifact_handshake.json"
        if not handshake_path.is_file() or handshake_path.is_symlink():
            raise SandboxError("completed release leg has no artifact handshake")
        self.progress_store.complete_leg(
            return_code=return_code,
            artifact_handshake_sha256=(
                f"sha256:{support.sha256_file(handshake_path)}"
            ),
        )
        self._reconcile_state()
        self._validate_release_resume()
        return None

    @staticmethod
    def _write_runner_logs(
        leg_output: Path,
        *,
        stdout: str,
        stderr: str,
        append: bool,
    ) -> None:
        for name, content in (
            ("runner_stdout.log", stdout),
            ("runner_stderr.log", stderr),
        ):
            path = leg_output / name
            if append:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_mode & 0o777 != 0o600
                ):
                    raise SandboxError("continued release runner log is invalid")
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(content)
            else:
                try:
                    with path.open("x", encoding="utf-8") as handle:
                        handle.write(content)
                except FileExistsError as exc:
                    raise SandboxError("new release runner log already exists") from exc
            path.chmod(0o600)

    @staticmethod
    def _seal_runner_logs(leg_output: Path) -> None:
        for name in ("runner_stdout.log", "runner_stderr.log"):
            seal_existing_file(leg_output / name, label=name)

    def _pause_after_early_stop(self, leg_output: Path) -> int:
        assert self.progress_store is not None
        progress = self.progress_store.progress()
        if progress.prefix_case_count is None:
            raise SandboxError("early-stop candidate count is invalid")
        candidate_path = leg_output / governance_event_paths(
            "mid_repeat", progress.prefix_case_count
        )["candidate_path"]
        if candidate_path.is_symlink() or not candidate_path.is_file():
            raise SandboxError("early-stop leg has no immutable candidate")
        if (
            self.progress_store.progress().phase
            is not release_state.ReleasePhase.AWAITING_REPAIR_DECISION
        ):
            raise SandboxError("early-stop leg progress was not committed")
        self._reconcile_state()
        return 2

    def _write_deferred_handshake(
        self,
        leg_dir: Path,
        evaluation_leg: Mapping[str, object],
        benchmark: str,
        repeat_ordinal: int,
    ) -> str:
        assert self.progress_store is not None
        return _write_deferred_leg_handshake(
            leg_dir=leg_dir,
            benchmark=benchmark,
            repeat_ordinal=repeat_ordinal,
            seed=int(evaluation_leg["seed"]),
            bundle_id=self.bundle_id,
            identity=self.identity,
            configuration_digest=self.configuration_digest,
            progress_store=self.progress_store,
        )

    def _finalize_bundle(self) -> int:
        assert self.progress_store is not None
        completed_legs = self.state.get("completed_legs")
        if not isinstance(completed_legs, list):
            raise SandboxError("bundle state completed legs are invalid")
        artifacts = {
            f"{item['benchmark']}:r{item['repeat_ordinal']}": item[
                "artifact_handshake_sha256"
            ]
            for item in completed_legs
            if isinstance(item, Mapping)
        }
        if len(artifacts) != 6:
            raise SandboxError("canonical release bundle is missing a completed leg")
        handshake_path = self.output_dir / "bundle_artifact_handshake.json"
        try:
            handshake_digest = write_json_new_or_identical_sealed(
                handshake_path,
                {
                    "schema_version": 1,
                    "record_kind": "text2sql_public_benchmark_bundle_artifact_handshake",
                    "bundle_id": self.bundle_id,
                    "release_plan": self.release_plan,
                    **self.identity,
                    "leg_artifacts": artifacts,
                },
                label="bundle artifact handshake",
            )
        except DiagnosticArtifactError as exc:
            raise SandboxError(str(exc)) from exc
        self.progress_store.seal_terminal_state(
            expected=release_state.ReleasePhase.BETWEEN_LEGS,
            target=release_state.ReleasePhase.COMPLETE,
            artifact_sha256={handshake_path.name: handshake_digest},
        )
        self._reconcile_state()
        return int(self.state["return_code"])

def execute_release_bundle(
    args: argparse.Namespace,
    token: str,
    *,
    repo_root: Path,
    policy_path: Path,
    configuration_paths: Sequence[Path],
    load_bird_cases: Callable[[Path], Sequence[support.ReleaseCase]],
    load_spider_cases: Callable[[Path, Path, Path], Sequence[support.ReleaseCase]],
    run_leg: Callable[[argparse.Namespace, str], int],
    prove_git: Callable[..., Mapping[str, object]],
) -> int:
    return ReleaseBundleExecution(
        args,
        token,
        repo_root=repo_root,
        policy_path=policy_path,
        configuration_paths=configuration_paths,
        load_bird_cases=load_bird_cases,
        load_spider_cases=load_spider_cases,
        run_leg=run_leg,
        prove_git=prove_git,
    ).run()
