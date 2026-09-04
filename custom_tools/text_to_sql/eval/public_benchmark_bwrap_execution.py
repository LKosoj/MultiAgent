"""Bounded execution stages for one sandboxed benchmark leg."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from typing import Any, Mapping

from . import public_benchmark_bwrap as facade
from . import public_benchmark_release as release_support
from .release_leg_progress import ReleaseLegProgress
from .release_governance import governance_event_paths
from .sandbox import (
    CANONICAL_RUNTIME_SOURCE_PATHS,
    SandboxError,
    SourceSnapshot,
    validate_canonical_workers,
)
from scripts import text2sql_benchmark_reporting as benchmark_reporting
from scripts import text2sql_public_benchmark as runner


class BwrapBenchmarkExecution:
    """Run a benchmark leg while keeping each lifecycle stage explicit."""

    def __init__(self, args: argparse.Namespace, token: str) -> None:
        self.args = args
        self.token = token
        self.leg_progress: ReleaseLegProgress | None = None
        self.receipts: list[dict[str, object]] = []

    def run(self) -> int:
        self._select_cases()
        self._prepare_filesystem_and_snapshot()
        self._bind_configuration()
        self._write_input_artifacts()
        self._prepare_history()
        self._prepare_early_stop_policy()
        if (
            self.early_stop_policy is not None
            and self.evaluate_existing_prefix
            and self.completed_existing
        ):
            candidate = benchmark_reporting.find_early_stop_candidate(
                list(self.completed_existing.values()), self.early_stop_policy
            )
            if candidate is not None:
                return self._publish_early_stop(candidate, self.completed_existing)
        result = self._run_missing_cases()
        if result is not None:
            return result
        return self._finalize_leg()

    def _select_cases(self) -> None:
        validate_canonical_workers(self.args.workers)
        if self.args.sandbox_state_root is None or self.args.sandbox_secret_dir is None:
            raise ValueError(
                "canonical bwrap mode requires --sandbox-state-root and --sandbox-secret-dir"
            )
        self.canonical_release_leg = bool(
            getattr(self.args, "canonical_release_leg", False)
        )
        if self.canonical_release_leg:
            raw_cases = getattr(self.args, "release_cases", None)
            self.locked_case_manifest = getattr(
                self.args, "release_case_manifest", None
            )
            self.locked_database_digests = getattr(
                self.args, "release_database_digests", None
            )
            if (
                not isinstance(raw_cases, tuple)
                or not isinstance(self.locked_case_manifest, Mapping)
                or not isinstance(self.locked_database_digests, Mapping)
            ):
                raise SandboxError("canonical release inputs are not frozen")
            selected_cases = list(raw_cases)
            if facade._stable_case_manifest(
                self.args.dataset, selected_cases
            ) != self.locked_case_manifest:
                raise SandboxError("canonical release cases differ from release lock")
            expected_count = int(getattr(self.args, "expected_case_count"))
            if len(selected_cases) != expected_count:
                raise SandboxError(
                    f"canonical {self.args.dataset} case count must be "
                    f"{expected_count}, got {len(selected_cases)}"
                )
            self.leg_progress = ReleaseLegProgress.from_args(self.args)
            self.run_scope = "full_release"
        else:
            selected_cases = facade._select_cases(
                runner._load_cases(self.args),
                limit=self.args.limit,
                case_ids=set(self.args.case_id),
                ordinal_start=self.args.ordinal_start,
                ordinal_stop=self.args.ordinal_stop,
            )
            self.locked_case_manifest = None
            self.locked_database_digests = None
            self.run_scope = facade._canonical_run_scope(
                self.args, case_count=len(selected_cases)
            )
        self.selected_cases = selected_cases
        self.cases = facade._ordered_canonical_cases(
            selected_cases, seed=self.args.seed
        )
        self.partial_resume = bool(
            getattr(self.args, "resume_partial_leg", False)
        )

    def _prepare_filesystem_and_snapshot(self) -> None:
        self.output_dir = (
            self.args.output_dir.resolve()
            if self.partial_resume
            else runner._create_canonical_output_dir(self.args.output_dir)
        )
        self.observations_path = self.output_dir / "observations.jsonl"
        if self.observations_path.exists() and not self.partial_resume:
            raise SandboxError(
                "canonical bwrap mode does not overwrite or reuse observations"
            )
        self.state_root = self.args.sandbox_state_root.resolve()
        if self.state_root.exists() and not self.partial_resume:
            raise SandboxError("canonical bwrap state root must be new")
        if not self.partial_resume:
            self.state_root.mkdir(parents=True, mode=0o700)
            self.state_root.chmod(0o700)
            source = getattr(self.args, "schema_memory_source", None)
            if not self.canonical_release_leg and source is not None:
                source_identity = release_support.release_inputs.schema_memory_source_identity(
                    source
                )
                destination = self.state_root / "schema-memory"
                shutil.copytree(source_identity["root"], destination)
                release_support.release_inputs.filter_schema_memory_copy(destination)
                if (
                    release_support.release_inputs.schema_memory_source_identity(
                        destination
                    )["digest"]
                    != source_identity["digest"]
                ):
                    raise SandboxError("copied schema-memory does not match source")
        snapshot = getattr(self.args, "release_snapshot", None)
        if snapshot is None:
            snapshot = runner.create_source_snapshot(
                facade.REPO_ROOT,
                self.output_dir / "source-snapshot",
                allowed_paths=CANONICAL_RUNTIME_SOURCE_PATHS,
            )
            self.source_snapshot_manifest_digest = (
                facade._write_source_snapshot_artifact(
                    self.output_dir / "source_snapshot_manifest.json", snapshot
                )
            )
        else:
            if not isinstance(snapshot, SourceSnapshot):
                raise SandboxError("release source snapshot is invalid")
            self.source_snapshot_manifest_digest = str(
                getattr(self.args, "source_snapshot_manifest_digest")
            )
        self.snapshot = snapshot
        self.bundle_id = str(
            getattr(self.args, "release_bundle_id", "")
            or f"text2sql-benchmark-{runner.uuid.uuid4().hex}"
        )
        if self.leg_progress is not None:
            self.leg_progress.start()

    def _bind_configuration(self) -> None:
        self.configuration_sources = list(
            getattr(self.args, "release_configuration_sources", None)
            or runner._configuration_sources(self.snapshot.root)
        )
        if self.canonical_release_leg:
            evaluator_identities = getattr(
                self.args, "release_evaluator_identities", None
            )
            if not isinstance(evaluator_identities, Mapping):
                raise SandboxError("release configuration identity mismatch")
            self.configuration_digest = release_support.canonical_configuration_digest(
                configuration_sources=self.configuration_sources,
                canonical_environment=facade._sandbox_runtime_env(self.args),
                model_identity=getattr(self.args, "release_model_identity", None),
                evaluator_identities=evaluator_identities,
            )
        else:
            self.configuration_digest = facade._json_digest(
                self.configuration_sources
            )
        expected = getattr(
            self.args, "release_configuration_digest", self.configuration_digest
        )
        if self.configuration_digest != expected:
            raise SandboxError("release configuration identity mismatch")
        self.execution_mode = (
            "canonical_release"
            if self.canonical_release_leg
            else "diagnostic_noncanonical"
        )

    def _write_input_artifacts(self) -> None:
        if self.partial_resume:
            self.case_manifest_digest = (
                f"sha256:{facade._sha256(self.output_dir / 'case_manifest.json')}"
            )
            self.database_digests = dict(
                getattr(self.args, "release_database_digests")
            )
        else:
            self.case_manifest_digest, self.database_digests = (
                facade._write_case_manifest(
                    self.output_dir / "case_manifest.json",
                    cases=self.selected_cases,
                    benchmark=self.args.dataset,
                    repeat_ordinal=self.args.repeat_ordinal,
                    bundle_id=self.bundle_id,
                    seed=self.args.seed,
                    run_scope=self.run_scope,
                    execution_mode=self.execution_mode,
                    expected_locked_manifest=self.locked_case_manifest,
                    expected_database_digests=self.locked_database_digests,
                )
            )
            facade._write_manifest(
                args=self.args,
                cases=self.cases,
                output_dir=self.output_dir,
                principal={
                    "subject": "per_case_sandbox_authentication",
                    "roles": ["admin"],
                },
                completed_before=0,
                source_snapshot_digest=self.snapshot.digest,
                state_root=self.state_root,
                bundle_id=self.bundle_id,
                case_manifest_digest=self.case_manifest_digest,
                configuration_digest=self.configuration_digest,
                configuration_sources=self.configuration_sources,
                seed=self.args.seed,
                run_scope=self.run_scope,
                execution_mode=self.execution_mode,
                source_snapshot_manifest_digest=(
                    self.source_snapshot_manifest_digest
                ),
                release_identity=getattr(self.args, "release_identity", None),
                manifest_profile="bwrap_v2",
                source_snapshot=self.snapshot,
            )
        case_manifest = json.loads(
            (self.output_dir / "case_manifest.json").read_text(encoding="utf-8")
        )
        rows = case_manifest.get("cases") if isinstance(case_manifest, Mapping) else None
        if not isinstance(rows, list):
            raise SandboxError("case manifest has invalid schema description sidecars")
        self.schema_description_sidecars = {
            str(row["case_key"]): row.get("schema_description_sidecar")
            for row in rows
            if isinstance(row, Mapping) and isinstance(row.get("case_key"), str)
        }
        if set(self.schema_description_sidecars) != {
            case.case_key for case in self.selected_cases
        }:
            raise SandboxError("case manifest has invalid schema description sidecars")

    def _prepare_history(self) -> None:
        self.writer = facade.ObservationWriter(self.observations_path)
        self.evidence_path = self.output_dir / "empty_history_evidence.json"
        if self.leg_progress is not None:
            self.leg_progress.bind_inputs_and_materialize(
                partial_resume=self.partial_resume,
                run_manifest_sha256=(
                    f"sha256:{facade._sha256(self.output_dir / 'manifest.json')}"
                ),
                case_manifest_sha256=self.case_manifest_digest,
                ordered_case_keys=[item.case_key for item in self.cases],
                observations_path=self.observations_path,
            )
            completed = facade._load_completed(self.observations_path)
            expected_prefix = [
                item.case_key for item in self.cases[: len(completed)]
            ]
            if list(completed) != expected_prefix:
                raise SandboxError("release observations are not a canonical prefix")
            self._materialize_release_history()
            self.receipts = self.leg_progress.history_receipts()
            return
        if self.partial_resume:
            self.receipts = self._load_partial_history()
            return
        facade._write_empty_history_evidence(
            self.evidence_path,
            receipts=self.receipts,
            args=self.args,
            bundle_id=self.bundle_id,
            snapshot_digest=self.snapshot.digest,
            configuration_digest=self.configuration_digest,
            run_scope=self.run_scope,
        )

    def _load_partial_history(self) -> list[dict[str, object]]:
        if self.evidence_path.is_symlink() or not self.evidence_path.is_file():
            raise SandboxError("partial resume empty-history evidence is invalid")
        try:
            evidence = json.loads(self.evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SandboxError("partial resume empty-history evidence is invalid") from exc
        if not isinstance(evidence, Mapping):
            raise SandboxError("partial resume empty-history evidence is invalid")
        if (
            evidence.get("bundle_id") != self.bundle_id
            or evidence.get("source_snapshot_digest") != self.snapshot.digest
            or evidence.get("configuration_digest") != self.configuration_digest
            or not isinstance(evidence.get("receipts"), list)
        ):
            raise SandboxError("partial resume empty-history evidence is invalid")
        receipts = [
            dict(item)
            for item in evidence["receipts"]
            if isinstance(item, Mapping)
        ]
        if len(receipts) != len(evidence["receipts"]):
            raise SandboxError("partial resume empty-history receipts are invalid")
        return receipts

    def _materialize_release_history(self) -> None:
        if self.leg_progress is None:
            return
        self.leg_progress.materialize_history(
            self.evidence_path,
            render=lambda prefix: facade._empty_history_evidence_bytes(
                receipts=prefix,
                args=self.args,
                bundle_id=self.bundle_id,
                snapshot_digest=self.snapshot.digest,
                configuration_digest=self.configuration_digest,
                run_scope=self.run_scope,
            ),
        )

    def _persist_receipt(self, receipt: Mapping[str, object]) -> None:
        self.receipts.append(dict(receipt))
        if self.leg_progress is None:
            facade._write_empty_history_evidence(
                self.evidence_path,
                receipts=self.receipts,
                args=self.args,
                bundle_id=self.bundle_id,
                snapshot_digest=self.snapshot.digest,
                configuration_digest=self.configuration_digest,
                run_scope=self.run_scope,
            )

    def _prepare_early_stop_policy(self) -> None:
        self.raw_early_stop_policy = getattr(self.args, "early_stop_policy", None)
        self.evaluate_existing_prefix = (
            not self.partial_resume
            or (
                self.leg_progress is not None
                and self.leg_progress.should_evaluate_resumed_prefix
            )
        )
        self.early_stop_policy = (
            benchmark_reporting.parse_early_stop_policy(
                self.raw_early_stop_policy
            )
            if self.raw_early_stop_policy is not None
            else None
        )
        self.completed_existing = (
            facade._load_completed(self.observations_path)
            if self.partial_resume
            else {}
        )
        self.failures = (
            self.leg_progress.authenticated_failure_count()
            if self.partial_resume and self.leg_progress is not None
            else sum(
                row.get("observation_status") != "completed"
                for row in self.completed_existing.values()
            )
        )

    def _publish_early_stop(
        self,
        candidate: dict[str, object],
        completed_rows: Mapping[str, Mapping[str, object]],
    ) -> int:
        candidate.update(
            {
                "bundle_id": self.bundle_id,
                "benchmark": self.args.dataset,
                "repeat_ordinal": self.args.repeat_ordinal,
                "case_manifest_sha256": (
                    f"sha256:{facade._sha256(self.output_dir / 'case_manifest.json')}"
                ),
                "manifest_sha256": (
                    f"sha256:{facade._sha256(self.output_dir / 'manifest.json')}"
                ),
                "observations_sha256": (
                    f"sha256:{facade._sha256(self.observations_path)}"
                ),
                "policy_sha256": "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        self.raw_early_stop_policy,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "release_identity": dict(
                    getattr(self.args, "release_identity", {}) or {}
                ),
                "configuration_digest": self.configuration_digest,
                "source_snapshot_digest": self.snapshot.digest,
                "empty_history_evidence_sha256": (
                    f"sha256:{facade._sha256(self.evidence_path)}"
                ),
                "not_started_case_keys": [
                    item.case_key
                    for item in self.cases
                    if item.case_key not in completed_rows
                ],
                "ordered_case_keys": [item.case_key for item in self.cases],
            }
        )
        candidate_path = self.output_dir / governance_event_paths(
            "mid_repeat", len(completed_rows)
        )["candidate_path"]
        release_support.write_json_new_sealed(candidate_path, candidate)
        if self.leg_progress is not None:
            self.leg_progress.pause(
                candidate_sha256=f"sha256:{facade._sha256(candidate_path)}",
                prefix_case_count=len(completed_rows),
            )
        return facade.EARLY_STOP_AWAITING_DECISION

    def _run_missing_cases(self) -> int | None:
        for ordinal, case in enumerate(self.cases, start=1):
            if case.case_key in self.completed_existing:
                continue
            observation = self._run_one_case(case, ordinal)
            if observation["observation_status"] != "completed":
                self.failures += 1
            print(
                f"[{ordinal}/{len(self.cases)}] {case.case_id} "
                f"{observation['observation_status']} "
                f"{observation['elapsed_seconds']}s",
                flush=True,
            )
            completed_rows = facade._load_completed(self.observations_path)
            if self.early_stop_policy is not None:
                candidate = benchmark_reporting.find_early_stop_candidate(
                    list(completed_rows.values()), self.early_stop_policy
                )
                if candidate is not None:
                    return self._publish_early_stop(candidate, completed_rows)
        return None

    def _run_one_case(
        self, case: runner.BenchmarkCase, ordinal: int
    ) -> dict[str, Any]:
        if self.leg_progress is not None:
            self.leg_progress.begin_case(case.case_key)
        try:
            observation, _receipt = runner._run_case_in_sandbox(
                case,
                args=self.args,
                token=self.token,
                snapshot=self.snapshot,
                state_root=self.state_root,
                bundle_id=self.bundle_id,
                configuration_digest=self.configuration_digest,
                expected_database_digest=self.database_digests[case.case_key],
                expected_schema_description_sidecar=self.schema_description_sidecars[
                    case.case_key
                ],
                persist_receipt=self._persist_receipt,
                run_scope=self.run_scope,
            )
        except Exception as exc:
            self._record_unavailable_receipt(case, exc)
            observation = facade._failed_sandbox_observation(
                case,
                self.args.dataset,
                exc,
                repeat_ordinal=self.args.repeat_ordinal,
                bundle_id=self.bundle_id,
                snapshot_digest=self.snapshot.digest,
                configuration_digest=self.configuration_digest,
                seed=self.args.seed,
                run_scope=self.run_scope,
                execution_mode=self.execution_mode,
            )
        self._commit_observation(case, ordinal, observation)
        return observation

    def _record_unavailable_receipt(
        self, case: runner.BenchmarkCase, exc: Exception
    ) -> None:
        if any(item.get("case_key") == case.case_key for item in self.receipts):
            return
        self.receipts.append(
            {
                "schema_version": 1,
                "benchmark": self.args.dataset,
                "repeat_ordinal": self.args.repeat_ordinal,
                "case_key": case.case_key,
                "state_namespace": (
                    f"{self.args.dataset}:{self.args.repeat_ordinal}:{case.case_key}"
                ),
                "verification_phase": "before_process_start",
                "verification_status": "unavailable",
                "preexisting_history_items": None,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        )

    def _commit_observation(
        self,
        case: runner.BenchmarkCase,
        ordinal: int,
        observation: Mapping[str, Any],
    ) -> None:
        if self.leg_progress is not None:
            matching = [
                item
                for item in self.receipts
                if item.get("case_key") == case.case_key
            ]
            if len(matching) != 1:
                raise SandboxError("release case has no unique history receipt")
            self.leg_progress.commit_case(
                ordinal=ordinal - 1,
                case_key=case.case_key,
                observation=observation,
                history_receipt=matching[0],
                observations_path=self.observations_path,
            )
            self._materialize_release_history()
            return
        facade._write_empty_history_evidence(
            self.evidence_path,
            receipts=self.receipts,
            args=self.args,
            bundle_id=self.bundle_id,
            snapshot_digest=self.snapshot.digest,
            configuration_digest=self.configuration_digest,
            run_scope=self.run_scope,
        )
        self.writer.append(observation)

    def _finalize_leg(self) -> int:
        facade.export_predictions(
            self.args.dataset,
            self.cases,
            self.observations_path,
            self.output_dir,
        )
        if not bool(getattr(self.args, "defer_artifact_handshake", False)):
            facade._write_artifact_handshake(
                self.output_dir,
                benchmark=self.args.dataset,
                repeat_ordinal=self.args.repeat_ordinal,
                bundle_id=self.bundle_id,
                case_manifest_digest=self.case_manifest_digest,
                snapshot_digest=self.snapshot.digest,
                configuration_digest=self.configuration_digest,
                seed=self.args.seed,
                run_scope=self.run_scope,
                execution_mode=self.execution_mode,
                source_snapshot_manifest_digest=(
                    self.source_snapshot_manifest_digest
                ),
                governance_events=(
                    self.leg_progress.governance_events()
                    if self.leg_progress is not None
                    else ()
                ),
            )
        return 1 if self.failures else 0


def execute_bwrap_benchmark(args: argparse.Namespace, token: str) -> int:
    return BwrapBenchmarkExecution(args, token).run()
