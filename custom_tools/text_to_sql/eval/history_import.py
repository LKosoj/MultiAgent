"""Bootstrap reviewed eval candidates from SQL history without making them gold."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cases import dump_jsonl_cases


def history_record_to_candidate(record: dict[str, Any], index: int) -> dict[str, Any] | None:
    question = (
        record.get("question")
        or record.get("user_query")
        or record.get("query")
        or record.get("natural_language_query")
    )
    sql = record.get("sql") or record.get("sql_query") or record.get("generated_sql")
    if not isinstance(question, str) or not question.strip():
        return None
    candidate = {
        "id": f"history-{index:05d}",
        "question": question.strip(),
        "reviewed": False,
        "source": "sql_history",
        "tags": ["candidate"],
    }
    if isinstance(sql, str) and sql.strip():
        candidate["candidate_sql"] = sql.strip()
    return candidate


def seed_candidates_from_history(history_path: str | Path, output_path: str | Path) -> int:
    history_path = Path(history_path)
    candidates: list[dict[str, Any]] = []
    with history_path.open("r", encoding="utf-8") as fh:
        for index, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            raw = json.loads(stripped)
            if isinstance(raw, dict):
                candidate = history_record_to_candidate(raw, index)
                if candidate is not None:
                    candidates.append(candidate)
    dump_jsonl_cases(output_path, candidates)
    return len(candidates)
