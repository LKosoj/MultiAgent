"""JSONL observability export for local Text-to-SQL eval runs."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable


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
