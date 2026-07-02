"""Gold-case loading for local Text-to-SQL evals."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class TextToSQLEvalCase:
    id: str
    question: str
    expected_sql: str | None = None
    expected_rows: list[dict[str, Any]] | None = None
    expected_schema_links: dict[str, Any] | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    profile: str = "default"
    reviewed: bool = False

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], *, source: str) -> "TextToSQLEvalCase":
        case_id = raw.get("id")
        question = raw.get("question")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"{source}: eval case id must be a non-empty string")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"{source}: eval case question must be a non-empty string")

        expected_sql = raw.get("expected_sql")
        if expected_sql is not None and not isinstance(expected_sql, str):
            raise ValueError(f"{source}: expected_sql must be a string when present")

        expected_rows = raw.get("expected_rows")
        if expected_rows is not None:
            if not isinstance(expected_rows, list) or not all(isinstance(row, dict) for row in expected_rows):
                raise ValueError(f"{source}: expected_rows must be a list of objects")

        expected_schema_links = raw.get("expected_schema_links")
        if expected_schema_links is not None and not isinstance(expected_schema_links, dict):
            raise ValueError(f"{source}: expected_schema_links must be an object when present")

        tags = raw.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"{source}: tags must be a list of strings")

        profile = raw.get("profile") or "default"
        if not isinstance(profile, str):
            raise ValueError(f"{source}: profile must be a string")

        return cls(
            id=case_id.strip(),
            question=question.strip(),
            expected_sql=expected_sql.strip() if isinstance(expected_sql, str) else None,
            expected_rows=expected_rows,
            expected_schema_links=expected_schema_links,
            tags=tuple(tags),
            profile=profile,
            reviewed=bool(raw.get("reviewed", False)),
        )


def load_jsonl_cases(path: str | Path) -> list[TextToSQLEvalCase]:
    cases: list[TextToSQLEvalCase] = []
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            raw = json.loads(stripped)
            if not isinstance(raw, dict):
                raise ValueError(f"{path}:{line_no}: eval case must be a JSON object")
            cases.append(TextToSQLEvalCase.from_mapping(raw, source=f"{path}:{line_no}"))
    return cases


def load_gold_cases(path: str | Path) -> list[TextToSQLEvalCase]:
    cases = load_jsonl_cases(path)
    unreviewed = [case.id for case in cases if not case.reviewed]
    if unreviewed:
        raise ValueError(
            "Gold eval files must contain only reviewed cases; "
            f"unreviewed case ids: {', '.join(unreviewed)}"
        )
    return cases


def dump_jsonl_cases(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
