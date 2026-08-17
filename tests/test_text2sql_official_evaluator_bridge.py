from __future__ import annotations

import argparse
import json
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, Sequence

import pytest

from custom_tools.text_to_sql.eval import official_evaluator_bridge as bridge
from custom_tools.text_to_sql.eval import public_benchmark_release as release
from custom_tools.text_to_sql.eval.official_evaluator_bridge import (
    ContainerRequest,
    docker_command,
)
from custom_tools.text_to_sql.eval.official_evaluator_contracts import (
    EVALUATOR_CALL_SURFACES,
    IMAGE_ID,
    IMAGE_IDENTITY,
    IMAGE_PLATFORM,
    IMAGE_USER,
    RAW_FREEZE_SHA256,
)


def test_docker_command_is_nonroot_readonly_offline_and_bounded(tmp_path: Path) -> None:
    request = ContainerRequest(
        name="text2sql-official-eval-1234",
        benchmark="bird",
        transaction_dir=tmp_path / "transaction",
        worker_root=tmp_path / "snapshot" / "eval",
        dataset_root=tmp_path / "bird",
        sqlite_root=None,
        identity_path=tmp_path / "identity.json",
        worker_arguments=("bird", "--raw-results", "/work/raw.json"),
        readonly_work_mounts=((tmp_path / "staged.json", "/work/staged.json"),),
    )
    command = docker_command(request)
    joined = " ".join(command)
    assert command[0:2] == ["docker", "run"]
    assert IMAGE_ID in command
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--user=65532:65532" in command
    assert "--cap-drop=ALL" in command
    assert "--memory=8g" in command
    assert "--memory-swap=8g" in command
    assert "docker.sock" not in joined
    assert "text2sql-benchmark-evaluator-py311" not in joined
    assert (
        f"src={(tmp_path / 'staged.json').resolve()},dst=/work/staged.json,readonly"
        in joined
    )


def test_release_hook_revalidates_lock_before_and_after_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    validations: list[str] = []
    calls: list[dict[str, object]] = []
    execution = SimpleNamespace(
        args=SimpleNamespace(),
        lock={"lock_digest": "sha256:lock"},
        snapshot=SimpleNamespace(root=tmp_path / "snapshot"),
        release_plan=[
            {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7},
            {"benchmark": "spider", "repeat_ordinal": 1, "seed": 8},
        ],
        state={
            "evaluation_leg": {
                "benchmark": "bird",
                "repeat_ordinal": 1,
                "seed": 7,
            }
        },
        _validate_frozen_inputs=lambda: validations.append("validated") or {},
    )
    identity = {
        "origin": "https://example.test/bird",
        "revision": "revision",
        "entrypoint": "evaluation/evaluation_ex.py",
        "sha256": "a" * 64,
        "call_surface": EVALUATOR_CALL_SURFACES["bird"],
        "source_closure_sha256": "b" * 64,
        "data_closure_sha256": "c" * 64,
        "runtime_identity_sha256": "d" * 64,
    }
    monkeypatch.setattr(
        bridge,
        "run_official_evaluation",
        lambda **kwargs: calls.append(kwargs),
    )

    bridge.run_for_release(execution, tmp_path / "leg", "bird", identity)

    assert validations == ["validated", "validated"]
    assert len(calls) == 1
    assert calls[0]["snapshot_root"] == tmp_path / "snapshot"
    assert calls[0]["not_started_release_legs"] == 1


def test_fresh_evaluation_passes_remaining_legs_to_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = tmp_path / "attempt"
    finished: list[int] = []
    published: list[Path] = []
    identity = {
        "origin": "https://example.test/bird",
        "revision": "revision",
        "entrypoint": "evaluation/evaluation_ex.py",
        "sha256": "a" * 64,
        "call_surface": EVALUATOR_CALL_SURFACES["bird"],
        "source_closure_sha256": "b" * 64,
        "data_closure_sha256": "c" * 64,
        "runtime_identity_sha256": "d" * 64,
    }
    monkeypatch.setattr(bridge, "validate_image_identity", lambda _path: {})
    monkeypatch.setattr(bridge, "_case_keys", lambda *_args: ["bird:0"])
    monkeypatch.setattr(
        bridge, "_recoverable_attempt", lambda *_args, **_kwargs: (None, False)
    )
    monkeypatch.setattr(bridge, "_inspect_image", lambda: {})

    def new_attempt(
        *,
        args: argparse.Namespace,
        snapshot_root: Path,
        leg_dir: Path,
        benchmark: str,
        lock_digest: str,
        evaluator_identity: Mapping[str, object],
        identity_path: Path,
        identity: Mapping[str, object],
        image: Mapping[str, object],
        case_keys: Sequence[str],
    ) -> Path:
        return attempt

    args = SimpleNamespace(official_evaluator_image_identity=tmp_path / "image.json")
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        new_attempt(
            args=args,
            snapshot_root=tmp_path / "snapshot",
            leg_dir=tmp_path / "leg",
            benchmark="bird",
            lock_digest="sha256:lock",
            evaluator_identity=identity,
            identity_path=tmp_path / "image.json",
            identity={},
            image={},
            case_keys=["bird:0"],
            not_started_release_legs=2,
        )
    monkeypatch.setattr(bridge, "_run_new_attempt", new_attempt)

    def finish(
        _attempt: Path,
        _leg_dir: Path,
        *,
        benchmark: str,
        lock_digest: str,
        evaluator_identity: dict[str, object],
        not_started_release_legs: int,
    ) -> None:
        assert benchmark == "bird"
        assert lock_digest.startswith("sha256:")
        assert evaluator_identity == identity
        finished.append(not_started_release_legs)

    monkeypatch.setattr(bridge, "_finish_evaluated_attempt", finish)
    monkeypatch.setattr(
        bridge, "_publish_ready_attempt", lambda _leg_dir, path: published.append(path)
    )

    bridge.run_official_evaluation(
        args=args,
        lock={"lock_digest": "sha256:lock"},
        snapshot_root=tmp_path / "snapshot",
        leg_dir=tmp_path / "leg",
        benchmark="bird",
        evaluator_identity=identity,
        not_started_release_legs=2,
    )

    assert finished == [2]
    assert published == [attempt]


def test_spider_staging_binds_exact_prediction_manifest(tmp_path: Path) -> None:
    leg_dir = tmp_path / "leg"
    predictions = leg_dir / "spider_predictions"
    transaction = tmp_path / "transaction"
    predictions.mkdir(parents=True)
    transaction.mkdir()
    case_keys = [f"local{index:03d}" for index in range(135)]
    for key in case_keys:
        (predictions / f"{key}.sql").write_text("SELECT 1\n", encoding="utf-8")

    staged = bridge._stage_predictions(
        leg_dir, transaction, "spider", case_keys
    )

    manifest = json.loads(staged.evidence_bytes)
    assert len(manifest) == 135
    assert [row["path"] for row in manifest] == sorted(
        f"{key}.sql" for key in case_keys
    )
    assert bridge._spider_prediction_manifest(staged.worker_path, case_keys) == manifest


def test_bird_receipt_input_is_staged_bytes_not_mutable_source(tmp_path: Path) -> None:
    leg_dir = tmp_path / "leg"
    transaction = tmp_path / "transaction"
    leg_dir.mkdir()
    transaction.mkdir()
    source = leg_dir / "bird_predictions.json"
    original = json.dumps({str(index): "SELECT 1" for index in range(500)}).encode()
    source.write_bytes(original)

    staged = bridge._stage_predictions(
        leg_dir,
        transaction,
        "bird",
        [f"bird:{index}" for index in range(500)],
    )
    source.write_bytes(b"mutated source")

    bridge._validate_staged_input(staged)
    assert staged.evidence_bytes == original
    assert staged.evidence_sha256 == "sha256:" + hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize("benchmark", ("bird", "spider"))
def test_staged_input_mutation_is_rejected(tmp_path: Path, benchmark: str) -> None:
    leg_dir = tmp_path / "leg"
    transaction = tmp_path / "transaction"
    leg_dir.mkdir()
    transaction.mkdir()
    if benchmark == "bird":
        case_keys = [f"bird:{index}" for index in range(500)]
        (leg_dir / "bird_predictions.json").write_text(
            json.dumps({str(index): "SELECT 1" for index in range(500)}),
            encoding="utf-8",
        )
    else:
        case_keys = [f"local{index:03d}" for index in range(135)]
        source = leg_dir / "spider_predictions"
        source.mkdir()
        for key in case_keys:
            (source / f"{key}.sql").write_text("SELECT 1\n", encoding="utf-8")
    staged = bridge._stage_predictions(
        leg_dir, transaction, benchmark, case_keys
    )
    staged_path = (
        staged.worker_path
        if staged.worker_path.is_file()
        else next(staged.worker_path.iterdir())
    )
    staged_path.chmod(0o644)
    staged_path.write_text("mutated staged input", encoding="utf-8")

    with pytest.raises(release.SandboxError, match="staged evaluator input"):
        bridge._validate_staged_input(staged)


def _ready_attempt(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    attempt = tmp_path / "attempt"
    candidates = attempt / "candidates"
    candidates.mkdir(parents=True)
    payloads = {
        name: f"sealed:{name}\n".encode()
        for name in bridge.CANONICAL_PUBLISH_ORDER
    }
    for name, payload in payloads.items():
        bridge._publish_bytes_sealed(candidates / name, payload)
    evaluated = {
        "schema_version": 1,
        "record_kind": "text2sql_official_evaluator_attempt_evaluated",
        "attempt_id": attempt.name,
    }
    evaluated_sha256 = bridge._write_attempt_marker(
        attempt / "EVALUATED.json", evaluated
    )
    ready = {
        "schema_version": 1,
        "record_kind": "text2sql_official_evaluator_attempt_ready",
        "attempt_id": attempt.name,
        "previous_marker_sha256": evaluated_sha256,
        "artifacts": {
            name: {
                "path": f"candidates/{name}",
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in payloads.items()
        },
    }
    bridge._write_attempt_marker(attempt / "READY.json", ready)
    return attempt, payloads


def test_ready_attempt_recovers_after_partial_canonical_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attempt, payloads = _ready_attempt(tmp_path)
    leg_dir = tmp_path / "leg"
    leg_dir.mkdir()
    original = bridge._publish_file_sealed
    crashed = False

    def crash_once(source: Path, target: Path) -> None:
        nonlocal crashed
        if target.name == "summary.json" and not crashed:
            crashed = True
            raise RuntimeError("simulated publication crash")
        original(source, target)

    monkeypatch.setattr(bridge, "_publish_file_sealed", crash_once)
    with pytest.raises(RuntimeError, match="simulated publication crash"):
        bridge._publish_ready_attempt(leg_dir, attempt)
    assert not (leg_dir / "evaluation_manifest.json").exists()

    monkeypatch.setattr(bridge, "_publish_file_sealed", original)
    bridge._publish_ready_attempt(leg_dir, attempt)

    for name, payload in payloads.items():
        path = leg_dir / name
        assert path.read_bytes() == payload
        assert path.stat().st_mode & 0o777 == 0o444
        assert not path.is_symlink()
    assert (leg_dir / "evaluation_manifest.json").is_file()


def test_ready_attempt_rejects_conflicting_partial_canonical_file(
    tmp_path: Path,
) -> None:
    attempt, _payloads = _ready_attempt(tmp_path)
    leg_dir = tmp_path / "leg"
    leg_dir.mkdir()
    conflict = leg_dir / "official_scores.json"
    conflict.write_text("conflict\n", encoding="utf-8")
    os.chmod(conflict, 0o444)

    with pytest.raises(release.SandboxError, match="differs from ready attempt"):
        bridge._publish_ready_attempt(leg_dir, attempt)


def test_execution_evidence_is_closed_and_bound_to_artifacts(tmp_path: Path) -> None:
    identity = {
        "origin": "https://example.test/bird",
        "revision": "revision",
        "entrypoint": "evaluation/evaluation_ex.py",
        "sha256": "a" * 64,
        "call_surface": EVALUATOR_CALL_SURFACES["bird"],
        "source_closure_sha256": "b" * 64,
        "data_closure_sha256": "c" * 64,
        "runtime_identity_sha256": "d" * 64,
    }
    names = (
        "official_evaluator_stdout.log",
        "official_evaluator_stderr.log",
        "official_evaluator_source.log",
        "official_evaluator_raw_results.json",
        "official_scores.json",
    )
    for name in names:
        (tmp_path / name).write_text(name, encoding="utf-8")
    execution = {
        "schema_version": 1,
        "record_kind": "text2sql_official_evaluator_execution",
        "benchmark": "bird",
        "release_lock_digest": "sha256:lock",
        "evaluator_identity": identity,
        "image_identity": IMAGE_IDENTITY,
        "image_inspection": {
            "image_id": IMAGE_ID,
            "user": IMAGE_USER,
            "platform": IMAGE_PLATFORM,
        },
        "freeze_before": RAW_FREEZE_SHA256,
        "freeze_after": RAW_FREEZE_SHA256,
        "artifacts": {
            name: "sha256:" + hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
            for name in names
        },
    }
    run_manifest = {"release_identity": {"release_lock_digest": "sha256:lock"}}

    release._validate_execution_evidence(
        tmp_path,
        execution,
        evaluator_identity=identity,
        case_keys=["bird:0"],
        run_manifest=run_manifest,
    )
    execution["freeze_after"] = "0" * 64
    with pytest.raises(release.SandboxError, match="execution evidence"):
        release._validate_execution_evidence(
            tmp_path,
            execution,
            evaluator_identity=identity,
            case_keys=["bird:0"],
            run_manifest=run_manifest,
        )


def test_execution_v2_binds_sealed_staged_receipt_and_input(tmp_path: Path) -> None:
    identity = {
        "origin": "https://example.test/bird",
        "revision": "revision",
        "entrypoint": "evaluation/evaluation_ex.py",
        "sha256": "a" * 64,
        "call_surface": EVALUATOR_CALL_SURFACES["bird"],
        "source_closure_sha256": "b" * 64,
        "data_closure_sha256": "c" * 64,
        "runtime_identity_sha256": "d" * 64,
    }
    artifact_names = (
        "official_evaluator_stdout.log",
        "official_evaluator_stderr.log",
        "official_evaluator_source.log",
        "official_evaluator_raw_results.json",
        "official_scores.json",
    )
    for name in artifact_names:
        (tmp_path / name).write_text(name, encoding="utf-8")
    staged_path = (
        tmp_path / "official-evaluator-attempts" / "attempt-1" / "STAGED.json"
    )
    bridge._publish_bytes_sealed(staged_path, b'{"staged":true}\n')
    input_sha256 = "sha256:" + "e" * 64
    execution = {
        "schema_version": 2,
        "record_kind": "text2sql_official_evaluator_execution",
        "attempt_id": "attempt-1",
        "benchmark": "bird",
        "release_lock_digest": "sha256:lock",
        "evaluator_identity": identity,
        "image_identity": IMAGE_IDENTITY,
        "image_inspection": {
            "image_id": IMAGE_ID,
            "user": IMAGE_USER,
            "platform": IMAGE_PLATFORM,
        },
        "input_kind": "bird_predictions_json",
        "evaluator_input_sha256": input_sha256,
        "staged_receipt_sha256": "sha256:" + hashlib.sha256(
            staged_path.read_bytes()
        ).hexdigest(),
        "freeze_before": RAW_FREEZE_SHA256,
        "freeze_after": RAW_FREEZE_SHA256,
        "artifacts": {
            name: "sha256:" + hashlib.sha256(
                (tmp_path / name).read_bytes()
            ).hexdigest()
            for name in artifact_names
        },
    }
    kwargs = {
        "evaluator_identity": identity,
        "case_keys": ["bird:0"],
        "run_manifest": {
            "release_identity": {"release_lock_digest": "sha256:lock"}
        },
        "evaluator_input_sha256": input_sha256,
    }

    release._validate_execution_evidence(tmp_path, execution, **kwargs)
    staged_path.chmod(0o644)

    with pytest.raises(release.SandboxError, match="staged evaluator receipt"):
        release._validate_execution_evidence(tmp_path, execution, **kwargs)


def test_execution_v2_requires_every_canonical_artifact_to_be_sealed(
    tmp_path: Path,
) -> None:
    for name in release._SEALED_EVALUATION_ARTIFACTS:
        bridge._publish_bytes_sealed(tmp_path / name, f"{name}\n".encode())

    release._validate_sealed_evaluation_artifacts(tmp_path)
    (tmp_path / "evaluator_receipt.json").chmod(0o644)

    with pytest.raises(release.SandboxError, match="sealed artifact"):
        release._validate_sealed_evaluation_artifacts(tmp_path)
