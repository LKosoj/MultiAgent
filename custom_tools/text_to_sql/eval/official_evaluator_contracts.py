"""Closed contracts for the pinned BIRD and Spider official evaluators."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence

try:
    from .sandbox import SandboxError, resolve_safe_regular_file
except ImportError:  # Mounted beside the standalone container worker.
    class SandboxError(RuntimeError):
        pass

    def resolve_safe_regular_file(path: Path, *, label: str) -> Path:
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_file():
            raise SandboxError(f"{label} is missing or unsafe")
        return resolved

IMAGE_ID = "sha256:e7b4d4a8080302520bd97bbb66712265e62f860b849f3fa4726e39618880466a"
IMAGE_USER = "65532:65532"
IMAGE_PLATFORM = "amd64/linux"
PYTHON_VERSION = "3.11.5"
RAW_FREEZE_SHA256 = "19da537fcbef0688391d6cada7c7eb69fce0cc9234431f9af1a0f4f9999e07b7"
BIRD_DIFFICULTY_JSONL_SHA256 = "09bf3807a96c861f3791803e2cfb81f74a2e81c18757510bcf21d80820dc2600"
IDENTITY_ARTIFACT_SHA256 = "177cb0d60c525cab9b09bddd678f250231f5ac8d5b24911568e1631218467dbd"
IMAGE_IDENTITY = {
    "architecture": "amd64",
    "base_image": "python@sha256:edaf703dce209d774af3ff768fc92b1e3b60261e7602126276f9ceb0e3a96874",
    "dockerfile_sha256": "fb094c29d5ed0b0676a3861ee3e8c2f06801a8a049cc7a7e216f3e7a0e583b7a",
    "image_id": IMAGE_ID,
    "local_tag": "text2sql-official-evaluator:py311-frozen",
    "operating_system": "linux",
    "pip_freeze_path": "/opt/evaluator-pip-freeze.txt",
    "pip_freeze_sha256": RAW_FREEZE_SHA256,
    "python_version": PYTHON_VERSION,
    "record_kind": "text2sql_official_evaluator_image_identity",
    "schema_version": 1,
}
BIRD_CASE_COUNT = 500
SPIDER_CASE_COUNT = 135
BIRD_EVALUATOR_SOURCE_PATHS = (
    "evaluation/evaluation_ex.py",
    "evaluation/evaluation_utils.py",
    "evaluation/run_evaluation.sh",
    "requirements.txt",
)
SPIDER_EVALUATOR_SOURCE_PATHS = ("evaluation_suite/evaluate.py",)
BIRD_EVALUATOR_DATA_PATHS = ("mini_dev_sqlite_gold.sql",)
SPIDER_EVALUATOR_DATA_PATHS = (
    "evaluation_suite/gold/spider2lite_eval.jsonl",
)
EVALUATOR_CALL_SURFACES = {
    "bird": "python __main__:evaluation/evaluation_ex.py",
    "spider": "python API:evaluate_spider2sql",
}

LEGACY_EVALUATOR_IDENTITY_FIELDS = frozenset(
    {"origin", "revision", "entrypoint", "sha256"}
)
EVALUATOR_IDENTITY_FIELDS = LEGACY_EVALUATOR_IDENTITY_FIELDS | frozenset(
    {
        "call_surface",
        "source_closure_sha256",
        "data_closure_sha256",
        "runtime_identity_sha256",
    }
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def validate_image_identity(
    path: Path,
    *,
    expected_file_sha256: str = IDENTITY_ARTIFACT_SHA256,
) -> dict[str, object]:
    safe_path = resolve_safe_regular_file(path, label="evaluator image identity")
    if sha256_file(safe_path) != expected_file_sha256:
        raise SandboxError("official evaluator image identity digest mismatch")
    try:
        value = json.loads(safe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError("official evaluator image identity is invalid") from exc
    if not isinstance(value, dict) or value != IMAGE_IDENTITY:
        raise SandboxError("official evaluator image identity is invalid")
    return value


def validate_bird_predictions(path: Path) -> list[str]:
    safe_path = resolve_safe_regular_file(path, label="BIRD evaluator input")
    try:
        payload = json.loads(safe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError("BIRD evaluator input is invalid") from exc
    expected_keys = [str(index) for index in range(BIRD_CASE_COUNT)]
    if not isinstance(payload, dict) or list(payload) != expected_keys:
        raise SandboxError("BIRD evaluator input must have exact ordered keys 0..499")
    values = list(payload.values())
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise SandboxError("BIRD evaluator input values must be non-empty SQL strings")
    return values


def bird_difficulty_jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    if len(rows) != BIRD_CASE_COUNT or any(not isinstance(row, Mapping) for row in rows):
        raise SandboxError("BIRD task input must contain exactly 500 objects")
    return b"".join(canonical_json_bytes(dict(row)) for row in rows)


def load_bird_task_jsonl(task_path: Path) -> bytes:
    safe_path = resolve_safe_regular_file(task_path, label="BIRD task file")
    try:
        value = json.loads(safe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError("BIRD task file is invalid") from exc
    if not isinstance(value, list):
        raise SandboxError("BIRD task file must be a JSON array")
    return bird_difficulty_jsonl(value)


def spider_local_references(
    task_path: Path,
    database_map_path: Path,
) -> list[tuple[str, str]]:
    safe_task = resolve_safe_regular_file(task_path, label="Spider task file")
    safe_map = resolve_safe_regular_file(database_map_path, label="Spider database map")
    try:
        mapping = json.loads(safe_map.read_text(encoding="utf-8"))
        rows = [
            json.loads(line)
            for line in safe_task.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError("Spider evaluator references are invalid") from exc
    if not isinstance(mapping, dict) or len(mapping) != SPIDER_CASE_COUNT:
        raise SandboxError("Spider database map must contain exactly 135 entries")
    references: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise SandboxError("Spider task row is invalid")
        instance_id = str(row.get("instance_id") or "")
        if not instance_id.startswith("local"):
            continue
        database_id = mapping.get(instance_id)
        if not isinstance(database_id, str) or row.get("db") != database_id:
            raise SandboxError("Spider local task/database mapping mismatch")
        references.append((instance_id, database_id))
    if len(references) != SPIDER_CASE_COUNT or len(set(references)) != SPIDER_CASE_COUNT:
        raise SandboxError("Spider task must contain exactly 135 local references")
    if set(mapping) != {key for key, _database in references}:
        raise SandboxError("Spider task and database map key sets differ")
    return references


def resolve_spider_gold_paths(instance_id: str, gold_dir: Path) -> tuple[Path, ...]:
    results_dir = gold_dir / "exec_result"
    base = results_dir / f"{instance_id}.csv"
    if base.is_file() and not base.is_symlink():
        return (base,)
    pattern = re.compile(rf"^{re.escape(instance_id)}(_[a-z])?\.csv$")
    matches = tuple(
        path
        for path in sorted(results_dir.iterdir(), key=lambda item: item.name)
        if path.is_file() and not path.is_symlink() and pattern.match(path.name)
    )
    if not matches:
        raise SandboxError(f"Spider gold result is missing for {instance_id}")
    return matches


def spider_gold_inventory(
    references: Sequence[tuple[str, str]],
    gold_dir: Path,
) -> list[tuple[str, Path]]:
    inventory: list[tuple[str, Path]] = []
    for instance_id, _database_id in references:
        inventory.extend(
            (instance_id, path)
            for path in resolve_spider_gold_paths(instance_id, gold_dir)
        )
    if len(inventory) != 328 or len({path for _key, path in inventory}) != 328:
        raise SandboxError("Spider selected gold inventory must contain exactly 328 files")
    return inventory


def file_records_digest(records: Iterable[Mapping[str, object]]) -> str:
    normalized = sorted(
        (dict(record) for record in records),
        key=lambda row: (str(row.get("kind")), str(row.get("root")), str(row.get("path"))),
    )
    return sha256_bytes(canonical_json_bytes(normalized))
