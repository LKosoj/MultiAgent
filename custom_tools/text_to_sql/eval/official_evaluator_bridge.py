"""Host-side isolated bridge to immutable official evaluator workers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Mapping, Protocol, Sequence
import uuid

from .official_evaluator_attempt import publish_bytes_sealed as _publish_bytes_sealed
from .official_evaluator_contracts import (
    EVALUATOR_IDENTITY_FIELDS,
    IMAGE_ID,
    IMAGE_IDENTITY,
    IMAGE_PLATFORM,
    IMAGE_USER,
    LEGACY_EVALUATOR_IDENTITY_FIELDS,
    RAW_FREEZE_SHA256,
    validate_image_identity,
)
from .sandbox import SandboxError, resolve_safe_regular_file


@dataclass(frozen=True)
class ContainerRequest:
    name: str
    benchmark: str
    transaction_dir: Path
    worker_root: Path
    dataset_root: Path
    sqlite_root: Path | None
    identity_path: Path
    worker_arguments: tuple[str, ...]
    readonly_work_mounts: tuple[tuple[Path, str], ...] = ()


@dataclass(frozen=True)
class StagedEvaluatorInput:
    worker_path: Path
    evidence_bytes: bytes
    evidence_sha256: str
    input_kind: str
    case_keys: tuple[str, ...]


CANONICAL_PUBLISH_ORDER = (
    "official_evaluator_input.json",
    "official_scores.json",
    "official_evaluator_stdout.log",
    "official_evaluator_stderr.log",
    "official_evaluator_source.log",
    "official_evaluator_raw_results.json",
    "official_evaluator_execution.json",
    "evaluator_receipt.json",
    "diagnostics.jsonl",
    "summary.json",
    "failure_report.md",
    "evaluation_manifest.json",
)
EVALUATED_ARTIFACT_NAMES = CANONICAL_PUBLISH_ORDER[:6]


class ReleaseExecution(Protocol):
    args: argparse.Namespace
    lock: Mapping[str, object]
    release_plan: Sequence[Mapping[str, object]]
    snapshot: object
    state: Mapping[str, object]

    def _validate_frozen_inputs(self) -> Mapping[str, object]: ...


def _mount(source: Path, target: str, *, readonly: bool) -> list[str]:
    value = f"type=bind,src={source.resolve()},dst={target}"
    if readonly:
        value += ",readonly"
    return ["--mount", value]


def docker_command(request: ContainerRequest) -> list[str]:
    if request.benchmark not in {"bird", "spider"}:
        raise SandboxError("official evaluator benchmark is invalid")
    command = [
        "docker", "run", "--rm", "--init", f"--name={request.name}",
        "--network=none", "--read-only", f"--user={IMAGE_USER}",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--ipc=private",
        "--cpus=8.0", "--memory=8g", "--memory-swap=8g", "--pids-limit=128",
        "--ulimit=nofile=4096:4096", "--shm-size=256m",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777",
        "--env=HOME=/tmp", "--env=TMPDIR=/work/tmp", "--env=XDG_CACHE_HOME=/tmp/.cache",
        "--env=PYTHONDONTWRITEBYTECODE=1", "--env=PYTHONHASHSEED=0",
        "--env=LC_ALL=C.UTF-8", "--env=LANG=C.UTF-8", "--workdir=/work",
        *_mount(request.transaction_dir, "/work", readonly=False),
        *_mount(request.worker_root, "/bridge", readonly=True),
        *_mount(request.identity_path, "/identity.json", readonly=True),
    ]
    for source, target in request.readonly_work_mounts:
        if not target.startswith("/work/"):
            raise SandboxError("official evaluator staged mount target is invalid")
        command.extend(_mount(source, target, readonly=True))
    if request.benchmark == "bird":
        command.extend(_mount(request.dataset_root, "/official/bird", readonly=True))
    else:
        command.extend(_mount(request.dataset_root, "/official/spider", readonly=True))
        if request.sqlite_root is None:
            raise SandboxError("Spider official evaluator requires a SQLite root")
        command.extend(
            _mount(
                request.sqlite_root,
                "/official/spider/resource/databases",
                readonly=True,
            )
        )
    return [
        *command,
        IMAGE_ID,
        "/opt/evaluator-venv/bin/python",
        "/bridge/official_evaluator_worker.py",
        *request.worker_arguments,
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _publish_file_sealed(source: Path, target: Path) -> None:
    source = resolve_safe_regular_file(source, label="ready attempt artifact")
    if source.stat().st_size > 64 * 1024 * 1024:
        raise SandboxError("official evaluator output exceeded its limit")
    _publish_bytes_sealed(target, source.read_bytes())


def _write_attempt_marker(path: Path, payload: Mapping[str, object]) -> str:
    encoded = _json_bytes(payload)
    _publish_bytes_sealed(path, encoded)
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _read_json_object(path: Path, *, label: str) -> dict[str, object]:
    path = resolve_safe_regular_file(path, label=label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise SandboxError(f"{label} is invalid")
    return value


def _ready_artifacts(attempt: Path) -> dict[str, Path]:
    _sealed_marker_sha256(attempt / "READY.json", "READY marker")
    ready = _read_json_object(attempt / "READY.json", label="READY marker")
    if (
        set(ready) != {
            "schema_version", "record_kind", "attempt_id",
            "previous_marker_sha256", "artifacts",
        }
        or ready.get("schema_version") != 1
        or ready.get("record_kind")
        != "text2sql_official_evaluator_attempt_ready"
        or ready.get("attempt_id") != attempt.name
    ):
        raise SandboxError("READY marker is invalid")
    evaluated_sha256 = _sealed_marker_sha256(
        attempt / "EVALUATED.json", "EVALUATED marker"
    )
    if ready.get("previous_marker_sha256") != evaluated_sha256:
        raise SandboxError("READY marker chain is invalid")
    artifacts = ready.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(
        CANONICAL_PUBLISH_ORDER
    ):
        raise SandboxError("READY artifact inventory is invalid")
    resolved: dict[str, Path] = {}
    for name in CANONICAL_PUBLISH_ORDER:
        record = artifacts.get(name)
        if not isinstance(record, Mapping) or set(record) != {
            "path", "sha256", "size_bytes"
        }:
            raise SandboxError("READY artifact record is invalid")
        relative = record.get("path")
        if relative != f"candidates/{name}":
            raise SandboxError("READY artifact path is invalid")
        candidate = resolve_safe_regular_file(
            attempt / str(relative), label="READY artifact"
        )
        if (
            not candidate.is_relative_to(attempt.resolve())
            or candidate.stat().st_mode & 0o777 != 0o444
            or candidate.stat().st_size != record.get("size_bytes")
            or "sha256:" + _sha256(candidate) != record.get("sha256")
        ):
            raise SandboxError("READY artifact digest or mode is invalid")
        resolved[name] = candidate
    return resolved


def _sealed_marker_sha256(path: Path, label: str) -> str:
    path = resolve_safe_regular_file(path, label=label)
    if path.stat().st_mode & 0o777 != 0o444:
        raise SandboxError(f"{label} is not sealed")
    return "sha256:" + _sha256(path)


def _publish_ready_attempt(leg_dir: Path, attempt: Path) -> None:
    artifacts = _ready_artifacts(attempt)
    for name in CANONICAL_PUBLISH_ORDER:
        try:
            _publish_file_sealed(artifacts[name], leg_dir / name)
        except SandboxError as exc:
            raise SandboxError(
                f"canonical artifact differs from ready attempt: {name}"
            ) from exc


def _artifact_record(path: Path, attempt: Path) -> dict[str, object]:
    path = resolve_safe_regular_file(path, label="attempt artifact")
    if path.stat().st_mode & 0o777 != 0o444:
        raise SandboxError("attempt artifact is not sealed")
    return {
        "path": path.relative_to(attempt).as_posix(),
        "sha256": "sha256:" + _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _attempt_artifacts(
    attempt: Path,
    raw: object,
    expected_names: Sequence[str],
) -> dict[str, Path]:
    if not isinstance(raw, Mapping) or set(raw) != set(expected_names):
        raise SandboxError("attempt artifact inventory is invalid")
    artifacts: dict[str, Path] = {}
    for name in expected_names:
        record = raw.get(name)
        if not isinstance(record, Mapping) or set(record) != {
            "path", "sha256", "size_bytes"
        }:
            raise SandboxError("attempt artifact record is invalid")
        path = resolve_safe_regular_file(
            attempt / str(record.get("path")), label="attempt artifact"
        )
        if (
            not path.is_relative_to(attempt.resolve())
            or path.stat().st_mode & 0o777 != 0o444
            or path.stat().st_size != record.get("size_bytes")
            or "sha256:" + _sha256(path) != record.get("sha256")
        ):
            raise SandboxError("attempt artifact changed")
        artifacts[name] = path
    return artifacts


def _inspect_image() -> dict[str, str]:
    result = subprocess.run(
        [
            "docker", "image", "inspect", IMAGE_ID,
            "--format", "{{.Id}}\n{{.Config.User}}\n{{.Architecture}}/{{.Os}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    values = result.stdout.splitlines()
    if values != [IMAGE_ID, IMAGE_USER, IMAGE_PLATFORM]:
        raise SandboxError("official evaluator Docker image identity mismatch")
    return {"image_id": values[0], "user": values[1], "platform": values[2]}


def _run_container(request: ContainerRequest, *, timeout: int) -> tuple[Path, Path]:
    stdout_path = request.transaction_dir / "official_evaluator_stdout.log"
    stderr_path = request.transaction_dir / "official_evaluator_stderr.log"
    command = docker_command(request)
    try:
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            completed = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        subprocess.run(
            ["docker", "rm", "--force", request.name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        raise SandboxError("official evaluator container timed out") from exc
    if completed.returncode != 0:
        raise SandboxError("official evaluator container failed")
    if stdout_path.stat().st_size > 64 * 1024 * 1024 or stderr_path.stat().st_size > 64 * 1024 * 1024:
        raise SandboxError("official evaluator output exceeded its limit")
    return stdout_path, stderr_path


def _case_keys(leg_dir: Path, benchmark: str) -> list[str]:
    case_manifest = json.loads((leg_dir / "case_manifest.json").read_text(encoding="utf-8"))
    cases = case_manifest.get("cases") if isinstance(case_manifest, dict) else None
    keys = [row.get("case_key") for row in cases if isinstance(row, Mapping)] if isinstance(cases, list) else []
    if benchmark == "bird":
        expected_keys = [f"bird:{index}" for index in range(500)]
    elif benchmark == "spider":
        expected_keys = keys if len(keys) == 135 and all(
            isinstance(key, str) and key.startswith("local") for key in keys
        ) else []
    else:
        raise SandboxError("official evaluator benchmark is invalid")
    if keys != expected_keys or len(set(keys)) != len(expected_keys):
        raise SandboxError("official evaluator case manifest keys are invalid")
    return keys


def _spider_prediction_manifest(
    root: Path,
    expected_keys: Sequence[str],
) -> list[dict[str, str]]:
    if root.is_symlink() or not root.is_dir():
        raise SandboxError("Spider evaluator input directory is missing or unsafe")
    expected_names = {f"{key}.sql" for key in expected_keys}
    paths = sorted(root.iterdir(), key=lambda item: item.name)
    if {path.name for path in paths} != expected_names or any(
        path.is_symlink() or not path.is_file() for path in paths
    ):
        raise SandboxError("Spider evaluator input directory is invalid")
    return [{"path": path.name, "sha256": _sha256(path)} for path in paths]


def _stage_predictions(
    leg_dir: Path,
    transaction: Path,
    benchmark: str,
    case_keys: Sequence[str],
) -> StagedEvaluatorInput:
    if benchmark == "bird":
        source = resolve_safe_regular_file(leg_dir / "bird_predictions.json", label="BIRD evaluator input")
        target = transaction / source.name
        shutil.copyfile(source, target)
        if _sha256(source) != _sha256(target):
            raise SandboxError("BIRD evaluator input changed during staging")
        target.chmod(0o444)
        evidence = target.read_bytes()
        return StagedEvaluatorInput(
            worker_path=target,
            evidence_bytes=evidence,
            evidence_sha256="sha256:" + hashlib.sha256(evidence).hexdigest(),
            input_kind="bird_predictions_json",
            case_keys=tuple(case_keys),
        )
    source = leg_dir / "spider_predictions"
    manifest = _spider_prediction_manifest(source, case_keys)
    target = transaction / source.name
    target.mkdir()
    for path in sorted(source.iterdir(), key=lambda item: item.name):
        shutil.copyfile(path, target / path.name)
        (target / path.name).chmod(0o444)
    if _spider_prediction_manifest(target, case_keys) != manifest:
        raise SandboxError("Spider evaluator input changed during staging")
    target.chmod(0o555)
    evidence = _json_bytes(manifest)
    return StagedEvaluatorInput(
        worker_path=target,
        evidence_bytes=evidence,
        evidence_sha256="sha256:" + hashlib.sha256(evidence).hexdigest(),
        input_kind="spider_predictions_manifest",
        case_keys=tuple(case_keys),
    )


def _validate_staged_input(staged: StagedEvaluatorInput) -> None:
    if staged.input_kind == "bird_predictions_json":
        path = resolve_safe_regular_file(
            staged.worker_path, label="staged evaluator input"
        )
        valid = (
            path.stat().st_mode & 0o777 == 0o444
            and path.read_bytes() == staged.evidence_bytes
        )
    elif staged.input_kind == "spider_predictions_manifest":
        manifest = _spider_prediction_manifest(
            staged.worker_path, staged.case_keys
        )
        valid = (
            staged.worker_path.stat().st_mode & 0o777 == 0o555
            and _json_bytes(manifest) == staged.evidence_bytes
            and all(
                path.stat().st_mode & 0o777 == 0o444
                for path in staged.worker_path.iterdir()
            )
        )
    else:
        valid = False
    if not valid or staged.evidence_sha256 != (
        "sha256:" + hashlib.sha256(staged.evidence_bytes).hexdigest()
    ):
        raise SandboxError("staged evaluator input changed after staging")


def _worker_arguments(
    benchmark: str,
    predictions: Path,
    expected_keys_path: Path,
) -> tuple[str, ...]:
    if benchmark == "bird":
        return (
            "bird", "--entrypoint", "/official/bird/evaluation/evaluation_ex.py",
            "--predictions", f"/work/{predictions.name}", "--raw-results", "/work/raw_results.json",
            "--task", "/official/bird/mini_dev_sqlite.json", "--database-root", "/official/bird/dev_databases",
            "--gold-sql", "/official/bird/mini_dev_sqlite_gold.sql",
            "--difficulty-jsonl", "/work/mini_dev_sqlite.jsonl", "--source-log", "/work/official_source.log",
        )
    return (
        "spider", "--entrypoint", "/official/spider/evaluation_suite/evaluate.py",
        "--predictions", f"/work/{predictions.name}", "--raw-results", "/work/raw_results.json",
        "--gold-dir", "/official/spider/evaluation_suite/gold", "--temp-dir", "/work/spider_eval_temp",
        "--work-dir", "/work/cwd", "--expected-keys", f"/work/{expected_keys_path.name}",
    )


def _scores(raw_path: Path, case_keys: Sequence[str], benchmark: str) -> list[dict[str, object]]:
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != len(case_keys):
        raise SandboxError("official evaluator raw result denominator mismatch")
    if payload.get("freeze_before") != RAW_FREEZE_SHA256 or payload.get("freeze_after") != RAW_FREEZE_SHA256:
        raise SandboxError("official evaluator runtime changed during evaluation")
    if benchmark == "bird":
        if any(
            not isinstance(row, Mapping)
            or row.get("sql_idx") != index
            or not isinstance(row.get("res"), int)
            or isinstance(row.get("res"), bool)
            or row.get("res") not in {0, 1}
            for index, row in enumerate(rows)
        ):
            raise SandboxError("BIRD official evaluator result rows are invalid")
        return [
            {"case_key": case_keys[index], "score": row["res"]}
            for index, row in enumerate(rows)
        ]
    by_key = {row.get("instance_id"): row for row in rows if isinstance(row, Mapping)}
    if set(by_key) != set(case_keys) or any(
        not isinstance(row.get("score"), int)
        or isinstance(row.get("score"), bool)
        or row.get("score") not in {0, 1}
        for row in by_key.values()
    ):
        raise SandboxError("Spider official evaluator result keys mismatch")
    return [
        {
            "case_key": key,
            "score": by_key[key].get("score"),
            "error_info": by_key[key].get("error_info"),
        }
        for key in case_keys
    ]


def _new_attempt_layout(leg_dir: Path) -> tuple[str, Path, Path, Path, Path]:
    attempt_id = str(uuid.uuid4())
    attempt_root = leg_dir / "official-evaluator-attempts"
    attempt_root.mkdir(mode=0o700, exist_ok=True)
    attempt = attempt_root / attempt_id
    attempt.mkdir(mode=0o700)
    staged = attempt / "staged"
    work = attempt / "work"
    candidates = attempt / "candidates"
    for path in (staged, work, candidates):
        path.mkdir(mode=0o700)
    os.chown(work, 65532, 65532)
    return attempt_id, attempt, staged, work, candidates


def _attempt_marker_payload(
    attempt_id: str,
    benchmark: str,
    lock_digest: str,
    evaluator_identity: Mapping[str, object],
    case_keys: Sequence[str],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_kind": "text2sql_official_evaluator_attempt",
        "attempt_id": attempt_id,
        "benchmark": benchmark,
        "release_lock_digest": lock_digest,
        "evaluator_identity": dict(evaluator_identity),
        "case_keys_sha256": _json_digest(list(case_keys)),
    }


def _stage_attempt(
    leg_dir: Path,
    attempt: Path,
    staged_dir: Path,
    benchmark: str,
    case_keys: Sequence[str],
    attempt_marker_sha256: str,
) -> tuple[StagedEvaluatorInput, Path, str]:
    staged = _stage_predictions(leg_dir, staged_dir, benchmark, case_keys)
    expected_keys = staged_dir / "expected_keys.json"
    _publish_bytes_sealed(expected_keys, _json_bytes(list(case_keys)))
    staged_dir.chmod(0o555)
    marker = {
        "schema_version": 1,
        "record_kind": "text2sql_official_evaluator_attempt_staged",
        "attempt_id": attempt.name,
        "previous_marker_sha256": attempt_marker_sha256,
        "input_kind": staged.input_kind,
        "evaluator_input_sha256": staged.evidence_sha256,
        "case_keys": list(case_keys),
    }
    marker_sha256 = _write_attempt_marker(attempt / "STAGED.json", marker)
    return staged, expected_keys, marker_sha256


def _seal_evaluated_artifacts(
    attempt: Path,
    candidates: Path,
    work: Path,
    staged: StagedEvaluatorInput,
    scores: Sequence[Mapping[str, object]],
    benchmark: str,
) -> dict[str, dict[str, object]]:
    _validate_staged_input(staged)
    sources = {
        "official_evaluator_input.json": staged.evidence_bytes,
        "official_scores.json": _json_bytes(list(scores)),
    }
    for name, payload in sources.items():
        _publish_bytes_sealed(candidates / name, payload)
    paths = {
        "official_evaluator_stdout.log": work / "official_evaluator_stdout.log",
        "official_evaluator_stderr.log": work / "official_evaluator_stderr.log",
        "official_evaluator_raw_results.json": work / "raw_results.json",
        "official_evaluator_source.log": work
        / ("official_source.log" if benchmark == "bird" else "cwd/log.txt"),
    }
    for name, source in paths.items():
        _publish_file_sealed(source, candidates / name)
    return {
        name: _artifact_record(candidates / name, attempt)
        for name in EVALUATED_ARTIFACT_NAMES
    }


def _evaluated_payload(
    *,
    attempt: Path,
    staged_marker_sha256: str,
    benchmark: str,
    lock_digest: str,
    evaluator_identity: Mapping[str, object],
    identity: Mapping[str, object],
    image: Mapping[str, object],
    staged: StagedEvaluatorInput,
    artifacts: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_kind": "text2sql_official_evaluator_attempt_evaluated",
        "attempt_id": attempt.name,
        "previous_marker_sha256": staged_marker_sha256,
        "benchmark": benchmark,
        "release_lock_digest": lock_digest,
        "evaluator_identity": dict(evaluator_identity),
        "image_identity": dict(identity),
        "image_inspection": dict(image),
        "case_keys": list(staged.case_keys),
        "input_kind": staged.input_kind,
        "evaluator_input_sha256": staged.evidence_sha256,
        "summary_created_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": dict(artifacts),
    }


def _load_evaluated_attempt(
    attempt: Path,
    *,
    benchmark: str,
    lock_digest: str,
    evaluator_identity: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, Path], str, str]:
    evaluated_sha256 = _sealed_marker_sha256(
        attempt / "EVALUATED.json", "EVALUATED marker"
    )
    evaluated = _read_json_object(
        attempt / "EVALUATED.json", label="EVALUATED marker"
    )
    expected_fields = {
        "schema_version", "record_kind", "attempt_id",
        "previous_marker_sha256", "benchmark", "release_lock_digest",
        "evaluator_identity", "image_identity", "image_inspection",
        "case_keys", "input_kind", "evaluator_input_sha256",
        "summary_created_at", "artifacts",
    }
    if (
        set(evaluated) != expected_fields
        or evaluated.get("schema_version") != 1
        or evaluated.get("record_kind")
        != "text2sql_official_evaluator_attempt_evaluated"
        or evaluated.get("attempt_id") != attempt.name
        or evaluated.get("benchmark") != benchmark
        or evaluated.get("release_lock_digest") != lock_digest
        or evaluated.get("evaluator_identity") != dict(evaluator_identity)
        or evaluated.get("image_identity") != IMAGE_IDENTITY
        or evaluated.get("image_inspection")
        != {"image_id": IMAGE_ID, "user": IMAGE_USER, "platform": IMAGE_PLATFORM}
        or not isinstance(evaluated.get("case_keys"), list)
        or not isinstance(evaluated.get("summary_created_at"), str)
        or evaluated.get("input_kind")
        != (
            "bird_predictions_json"
            if benchmark == "bird"
            else "spider_predictions_manifest"
        )
    ):
        raise SandboxError("EVALUATED marker is invalid")
    staged_sha256 = _sealed_marker_sha256(
        attempt / "STAGED.json", "STAGED marker"
    )
    staged = _read_json_object(attempt / "STAGED.json", label="STAGED marker")
    if (
        set(staged)
        != {
            "schema_version", "record_kind", "attempt_id",
            "previous_marker_sha256", "input_kind",
            "evaluator_input_sha256", "case_keys",
        }
        or staged.get("schema_version") != 1
        or staged.get("record_kind")
        != "text2sql_official_evaluator_attempt_staged"
        or staged.get("attempt_id") != attempt.name
        or evaluated.get("previous_marker_sha256") != staged_sha256
        or staged.get("input_kind") != evaluated.get("input_kind")
        or staged.get("evaluator_input_sha256")
        != evaluated.get("evaluator_input_sha256")
        or staged.get("case_keys") != evaluated.get("case_keys")
    ):
        raise SandboxError("STAGED marker chain is invalid")
    attempt_sha256 = _sealed_marker_sha256(
        attempt / "ATTEMPT.json", "ATTEMPT marker"
    )
    started = _read_json_object(attempt / "ATTEMPT.json", label="ATTEMPT marker")
    if (
        set(started)
        != {
            "schema_version", "record_kind", "attempt_id", "benchmark",
            "release_lock_digest", "evaluator_identity", "case_keys_sha256",
        }
        or started.get("schema_version") != 1
        or started.get("record_kind")
        != "text2sql_official_evaluator_attempt"
        or started.get("attempt_id") != attempt.name
        or started.get("benchmark") != benchmark
        or started.get("release_lock_digest") != lock_digest
        or started.get("evaluator_identity") != dict(evaluator_identity)
        or started.get("case_keys_sha256")
        != _json_digest(evaluated["case_keys"])
        or staged.get("previous_marker_sha256") != attempt_sha256
    ):
        raise SandboxError("ATTEMPT marker chain is invalid")
    artifacts = _attempt_artifacts(
        attempt, evaluated.get("artifacts"), EVALUATED_ARTIFACT_NAMES
    )
    input_path = artifacts["official_evaluator_input.json"]
    if evaluated.get("evaluator_input_sha256") != "sha256:" + _sha256(input_path):
        raise SandboxError("EVALUATED input binding is invalid")
    return evaluated, artifacts, evaluated_sha256, staged_sha256


def _execution_payload(
    evaluated: Mapping[str, object],
    artifacts: Mapping[str, Path],
    staged_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "record_kind": "text2sql_official_evaluator_execution",
        "attempt_id": evaluated["attempt_id"],
        "benchmark": evaluated["benchmark"],
        "release_lock_digest": evaluated["release_lock_digest"],
        "evaluator_identity": evaluated["evaluator_identity"],
        "image_identity": evaluated["image_identity"],
        "image_inspection": evaluated["image_inspection"],
        "input_kind": evaluated["input_kind"],
        "evaluator_input_sha256": evaluated["evaluator_input_sha256"],
        "staged_receipt_sha256": staged_sha256,
        "freeze_before": RAW_FREEZE_SHA256,
        "freeze_after": RAW_FREEZE_SHA256,
        "artifacts": {
            name: "sha256:" + _sha256(artifacts[name])
            for name in (
                "official_evaluator_stdout.log",
                "official_evaluator_stderr.log",
                "official_evaluator_source.log",
                "official_evaluator_raw_results.json",
                "official_scores.json",
            )
        },
    }


def _finish_evaluated_attempt(
    attempt: Path,
    leg_dir: Path,
    *,
    benchmark: str,
    lock_digest: str,
    evaluator_identity: Mapping[str, object],
    not_started_release_legs: int,
) -> None:
    evaluated, artifacts, evaluated_sha256, staged_sha256 = (
        _load_evaluated_attempt(
            attempt,
            benchmark=benchmark,
            lock_digest=lock_digest,
            evaluator_identity=evaluator_identity,
        )
    )
    candidates = attempt / "candidates"
    execution_path = candidates / "official_evaluator_execution.json"
    _publish_bytes_sealed(
        execution_path,
        _json_bytes(_execution_payload(evaluated, artifacts, staged_sha256)),
    )
    receipt = {
        "schema_version": 2,
        "record_kind": "text2sql_official_evaluator_receipt",
        "evaluator_identity": dict(evaluator_identity),
        "evaluator_input_sha256": evaluated["evaluator_input_sha256"],
        "score_sha256": "sha256:" + _sha256(artifacts["official_scores.json"]),
        "case_keys": evaluated["case_keys"],
        "case_manifest_sha256": "sha256:" + _sha256(leg_dir / "case_manifest.json"),
        "run_manifest_sha256": "sha256:" + _sha256(leg_dir / "manifest.json"),
        "execution_evidence_sha256": "sha256:" + _sha256(execution_path),
    }
    receipt_path = candidates / "evaluator_receipt.json"
    _publish_bytes_sealed(receipt_path, _json_bytes(receipt))
    from scripts.summarize_text2sql_public_benchmark import main as summarize

    result = summarize(
        [
            "--observations", str(leg_dir / "observations.jsonl"),
            "--scores", str(artifacts["official_scores.json"]),
            "--evaluator-receipt", str(receipt_path),
            "--evaluator-input", str(artifacts["official_evaluator_input.json"]),
            "--run-manifest", str(leg_dir / "manifest.json"),
            "--case-manifest", str(leg_dir / "case_manifest.json"),
            "--output-dir", str(candidates),
            "--created-at", str(evaluated["summary_created_at"]),
            "--not-started-release-legs", str(not_started_release_legs),
        ]
    )
    if result != 0:
        raise SandboxError("official evaluator summary publication failed")
    all_artifacts = {
        name: _artifact_record(candidates / name, attempt)
        for name in CANONICAL_PUBLISH_ORDER
    }
    ready = {
        "schema_version": 1,
        "record_kind": "text2sql_official_evaluator_attempt_ready",
        "attempt_id": attempt.name,
        "previous_marker_sha256": evaluated_sha256,
        "artifacts": all_artifacts,
    }
    _write_attempt_marker(attempt / "READY.json", ready)


def _run_new_attempt(
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
    attempt_id, attempt, staged_dir, work, candidates = _new_attempt_layout(leg_dir)
    attempt_sha256 = _write_attempt_marker(
        attempt / "ATTEMPT.json",
        _attempt_marker_payload(
            attempt_id, benchmark, lock_digest, evaluator_identity, case_keys
        ),
    )
    staged, expected_keys, staged_sha256 = _stage_attempt(
        leg_dir, attempt, staged_dir, benchmark, case_keys, attempt_sha256
    )
    container_tmp = work / "tmp"
    container_tmp.mkdir(mode=0o700)
    os.chown(container_tmp, 65532, 65532)
    request = ContainerRequest(
        name=f"text2sql-official-eval-{uuid.uuid4().hex}",
        benchmark=benchmark,
        transaction_dir=work,
        worker_root=snapshot_root / "custom_tools/text_to_sql/eval",
        dataset_root=args.bird_root if benchmark == "bird" else args.spider_root,
        sqlite_root=args.spider_sqlite_root if benchmark == "spider" else None,
        identity_path=identity_path,
        worker_arguments=_worker_arguments(
            benchmark, staged.worker_path, expected_keys
        ),
        readonly_work_mounts=(
            (staged.worker_path, f"/work/{staged.worker_path.name}"),
            (expected_keys, f"/work/{expected_keys.name}"),
        ),
    )
    _run_container(request, timeout=900 if benchmark == "bird" else 1800)
    _validate_staged_input(staged)
    raw_path = resolve_safe_regular_file(
        work / "raw_results.json", label="official evaluator raw result"
    )
    scores = _scores(raw_path, case_keys, benchmark)
    artifact_records = _seal_evaluated_artifacts(
        attempt, candidates, work, staged, scores, benchmark
    )
    evaluated = _evaluated_payload(
        attempt=attempt,
        staged_marker_sha256=staged_sha256,
        benchmark=benchmark,
        lock_digest=lock_digest,
        evaluator_identity=evaluator_identity,
        identity=identity,
        image=image,
        staged=staged,
        artifacts=artifact_records,
    )
    _write_attempt_marker(attempt / "EVALUATED.json", evaluated)
    return attempt


def _attempt_directories(leg_dir: Path) -> list[Path]:
    root = leg_dir / "official-evaluator-attempts"
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise SandboxError("official evaluator attempt root is unsafe")
    paths = sorted(root.iterdir(), key=lambda path: path.name)
    if any(path.is_symlink() or not path.is_dir() for path in paths):
        raise SandboxError("official evaluator attempt directory is unsafe")
    return paths


def _recoverable_attempt(
    leg_dir: Path,
    *,
    benchmark: str,
    lock_digest: str,
    evaluator_identity: Mapping[str, object],
) -> tuple[Path | None, bool]:
    ready: list[tuple[Path, tuple[str, ...]]] = []
    evaluated: list[Path] = []
    for attempt in _attempt_directories(leg_dir):
        if (attempt / "READY.json").exists() or (attempt / "READY.json").is_symlink():
            _load_evaluated_attempt(
                attempt,
                benchmark=benchmark,
                lock_digest=lock_digest,
                evaluator_identity=evaluator_identity,
            )
            artifacts = _ready_artifacts(attempt)
            ready.append(
                (attempt, tuple(_sha256(artifacts[name]) for name in CANONICAL_PUBLISH_ORDER))
            )
        elif (attempt / "EVALUATED.json").exists() or (attempt / "EVALUATED.json").is_symlink():
            _load_evaluated_attempt(
                attempt,
                benchmark=benchmark,
                lock_digest=lock_digest,
                evaluator_identity=evaluator_identity,
            )
            evaluated.append(attempt)
    if ready:
        if any(signature != ready[0][1] for _, signature in ready[1:]):
            raise SandboxError("ready official evaluator attempts differ")
        return ready[0][0], True
    if len(evaluated) > 1:
        raise SandboxError("multiple evaluated official evaluator attempts exist")
    return (evaluated[0], False) if evaluated else (None, False)


def run_official_evaluation(
    *,
    args: argparse.Namespace,
    lock: Mapping[str, object],
    snapshot_root: Path,
    leg_dir: Path,
    benchmark: str,
    evaluator_identity: Mapping[str, object],
    not_started_release_legs: int = 0,
) -> None:
    evaluation_manifest = leg_dir / "evaluation_manifest.json"
    if evaluation_manifest.is_file() and not evaluation_manifest.is_symlink():
        return
    if set(evaluator_identity) != EVALUATOR_IDENTITY_FIELDS:
        raise SandboxError("official evaluator execution identity is invalid")
    if not isinstance(lock.get("lock_digest"), str):
        raise SandboxError("official evaluator release lock digest is invalid")
    lock_digest = _json_digest(lock)
    identity_path = Path(args.official_evaluator_image_identity)
    identity = validate_image_identity(identity_path)
    case_keys = _case_keys(leg_dir, benchmark)
    attempt, is_ready = _recoverable_attempt(
        leg_dir,
        benchmark=benchmark,
        lock_digest=lock_digest,
        evaluator_identity=evaluator_identity,
    )
    canonical_partial = any(
        (leg_dir / name).exists() or (leg_dir / name).is_symlink()
        for name in CANONICAL_PUBLISH_ORDER
    )
    if attempt is None and canonical_partial:
        raise SandboxError("partial canonical evaluation has no ready attempt")
    if attempt is None:
        attempt = _run_new_attempt(
            args=args,
            snapshot_root=snapshot_root,
            leg_dir=leg_dir,
            benchmark=benchmark,
            lock_digest=lock_digest,
            evaluator_identity=evaluator_identity,
            identity_path=identity_path,
            identity=identity,
            image=_inspect_image(),
            case_keys=case_keys,
        )
    if not is_ready:
        _finish_evaluated_attempt(
            attempt,
            leg_dir,
            benchmark=benchmark,
            lock_digest=lock_digest,
            evaluator_identity=evaluator_identity,
            not_started_release_legs=not_started_release_legs,
        )
    _publish_ready_attempt(leg_dir, attempt)


def run_for_release(
    execution: ReleaseExecution,
    leg_dir: Path,
    benchmark: str,
    evaluator_identity: Mapping[str, object],
) -> None:
    if set(evaluator_identity) == LEGACY_EVALUATOR_IDENTITY_FIELDS:
        return
    if set(evaluator_identity) != EVALUATOR_IDENTITY_FIELDS:
        raise SandboxError("official evaluator execution identity is invalid")
    evaluation_leg = execution.state.get("evaluation_leg")
    if not isinstance(evaluation_leg, Mapping):
        raise SandboxError("official evaluator evaluation leg is invalid")
    identity = tuple(
        evaluation_leg.get(field)
        for field in ("benchmark", "repeat_ordinal", "seed")
    )
    current_index = next(
        (
            index
            for index, item in enumerate(execution.release_plan)
            if (
                item.get("benchmark"), item.get("repeat_ordinal"), item.get("seed")
            ) == identity
        ),
        None,
    )
    if current_index is None:
        raise SandboxError("official evaluator evaluation leg is not in release plan")
    execution._validate_frozen_inputs()
    try:
        snapshot_root = getattr(execution.snapshot, "root", None)
        if not isinstance(snapshot_root, Path):
            raise SandboxError("official evaluator source snapshot is unavailable")
        run_official_evaluation(
            args=execution.args,
            lock=execution.lock,
            snapshot_root=snapshot_root,
            leg_dir=leg_dir,
            benchmark=benchmark,
            evaluator_identity=evaluator_identity,
            not_started_release_legs=len(execution.release_plan[current_index + 1 :]),
        )
    finally:
        execution._validate_frozen_inputs()
