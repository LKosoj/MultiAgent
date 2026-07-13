"""JSONL observability export for local Text-to-SQL eval runs."""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable


def canonical_files_digest(path: str | Path) -> str:
    """Hash JSONL evidence with stable file ordering and path boundaries."""
    root = Path(path)
    files = sorted(root.glob("*.jsonl")) if root.is_dir() else [root]
    if not files or any(not item.is_file() for item in files):
        raise ValueError(f"{root}: no digestible JSONL files found")
    digest = hashlib.sha256()
    for item in files:
        relative_name = item.name if root.is_dir() else root.name
        content = item.read_bytes()
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def write_json_evidence(path: str | Path, payload: dict[str, Any]) -> None:
    """Write a deterministic JSON evidence artifact."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def eval_result_observability_record(result: Any) -> dict[str, Any]:
    metrics = getattr(result, "schema_linking_metrics", None)
    return {
        "case_id": getattr(result, "case_id", None),
        "passed": bool(getattr(result, "passed", False)),
        "duration_ms": float(getattr(result, "duration_ms", 0.0) or 0.0),
        "error": getattr(result, "error", None),
        "schema_linking_metrics": asdict(metrics) if is_dataclass(metrics) else None,
    }


def write_eval_observability_jsonl(path: str | Path, results: Iterable[Any]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(eval_result_observability_record(result), ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            count += 1
    return count
