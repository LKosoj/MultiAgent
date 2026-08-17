"""Versioned reviewed cases for Text-to-SQL release evaluation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


CASE_SCHEMA_VERSION = 1
EXPECTED_OUTCOMES = frozenset(
    {"succeeded", "abstained", "failed", "cancelled", "timed_out"}
)
COMPARISON_MODES = frozenset({"exact", "unordered", "none"})
REQUIRED_RELEASE_SLICES = frozenset(
    {
        "composite_join",
        "aggregate",
        "filter",
        "ambiguity_abstain",
        "schema_drift",
        "dry_run",
        "execution_failure",
        "timeout",
        "cancel",
        "recovery",
    }
)
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_REQUEST_BOOLEAN_FIELDS = frozenset(
    {
        "include_explanation",
        "validate_schema",
        "dry_run_only",
    }
)
_REQUEST_FIELDS = _REQUEST_BOOLEAN_FIELDS | {"max_rows", "safety_level"}
_CASE_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "question",
        "dialect",
        "fixture",
        "expected_outcome",
        "comparison_mode",
        "expected_sql",
        "expected_rows",
        "expected_schema_links",
        "expected_reason_codes",
        "slice_tags",
        "cancel_after_start",
        "request_options",
        "profile",
        "review",
    }
)


def _non_empty_text(value: Any, *, field_name: str, source: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: {field_name} must be non-empty text")
    return value.strip()


def _identifier(value: Any, *, field_name: str, source: str) -> str:
    normalized = _non_empty_text(value, field_name=field_name, source=source).lower()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{source}: {field_name} must be a lowercase identifier")
    return normalized


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    status: str
    reviewed_by: str | None
    reviewed_at: str | None
    reference: str | None

    @classmethod
    def from_mapping(cls, value: Any, *, source: str) -> "ReviewRecord":
        if not isinstance(value, Mapping):
            raise ValueError(f"{source}: review must be an object")
        if set(value) != {"status", "reviewed_by", "reviewed_at", "reference"}:
            raise ValueError(f"{source}: review fields do not match schema v1")
        status = _non_empty_text(
            value.get("status"), field_name="review.status", source=source
        ).lower()
        if status not in {"reviewed", "pending"}:
            raise ValueError(f"{source}: review.status is unsupported")

        def optional(field_name: str) -> str | None:
            raw = value.get(field_name)
            if raw is None:
                return None
            return _non_empty_text(
                raw, field_name=f"review.{field_name}", source=source
            )

        record = cls(
            status=status,
            reviewed_by=optional("reviewed_by"),
            reviewed_at=optional("reviewed_at"),
            reference=optional("reference"),
        )
        if status == "reviewed" and not all(
            (record.reviewed_by, record.reviewed_at, record.reference)
        ):
            raise ValueError(f"{source}: reviewed cases require complete review metadata")
        return record


def _request_options(value: Any, *, source: str) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{source}: request_options must be an object")
    unknown = set(value) - _REQUEST_FIELDS
    if unknown:
        raise ValueError(f"{source}: unknown request_options: {sorted(unknown)}")
    options: dict[str, Any] = {
        "max_rows": 100,
        "safety_level": "strict",
        "include_explanation": True,
        "validate_schema": True,
        "dry_run_only": False,
    }
    options.update(value)
    max_rows = options["max_rows"]
    if type(max_rows) is not int or not 1 <= max_rows <= 10_000:
        raise ValueError(f"{source}: request_options.max_rows must be 1..10000")
    if options["safety_level"] != "strict":
        raise ValueError(f"{source}: request_options.safety_level must be strict")
    for field_name in _REQUEST_BOOLEAN_FIELDS:
        if type(options[field_name]) is not bool:
            raise ValueError(f"{source}: request_options.{field_name} must be boolean")
    return options


@dataclass(frozen=True, slots=True)
class TextToSQLEvalCase:
    schema_version: int
    id: str
    question: str
    dialect: str
    fixture: str
    expected_outcome: str
    comparison_mode: str
    expected_sql: str | None
    expected_rows: list[dict[str, Any]] | None
    expected_schema_links: dict[str, Any] | None
    expected_reason_codes: tuple[str, ...]
    slice_tags: tuple[str, ...]
    cancel_after_start: bool = False
    request_options: dict[str, Any] = field(default_factory=dict)
    profile: str = "release"
    review: ReviewRecord = field(
        default_factory=lambda: ReviewRecord("pending", None, None, None)
    )

    @property
    def reviewed(self) -> bool:
        return self.review.status == "reviewed"

    @property
    def tags(self) -> tuple[str, ...]:
        return self.slice_tags

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        source: str,
    ) -> "TextToSQLEvalCase":
        unknown = set(raw) - _CASE_FIELDS
        if unknown:
            raise ValueError(f"{source}: unknown eval case fields: {sorted(unknown)}")
        if raw.get("schema_version") != CASE_SCHEMA_VERSION:
            raise ValueError(f"{source}: schema_version must be {CASE_SCHEMA_VERSION}")
        case_id = _identifier(raw.get("id"), field_name="id", source=source)
        question = _non_empty_text(
            raw.get("question"), field_name="question", source=source
        )
        dialect = _identifier(
            raw.get("dialect"), field_name="dialect", source=source
        )
        fixture = _identifier(
            raw.get("fixture"), field_name="fixture", source=source
        )
        expected_outcome = _non_empty_text(
            raw.get("expected_outcome"),
            field_name="expected_outcome",
            source=source,
        ).lower()
        if expected_outcome not in EXPECTED_OUTCOMES:
            raise ValueError(f"{source}: expected_outcome is unsupported")
        comparison_mode = _non_empty_text(
            raw.get("comparison_mode"),
            field_name="comparison_mode",
            source=source,
        ).lower()
        if comparison_mode not in COMPARISON_MODES:
            raise ValueError(f"{source}: comparison_mode is unsupported")

        expected_sql = raw.get("expected_sql")
        if expected_sql is not None:
            expected_sql = _non_empty_text(
                expected_sql, field_name="expected_sql", source=source
            )
        expected_rows = raw.get("expected_rows")
        if expected_rows is not None and (
            not isinstance(expected_rows, list)
            or not all(isinstance(row, dict) for row in expected_rows)
        ):
            raise ValueError(f"{source}: expected_rows must be a list of objects")
        expected_schema_links = raw.get("expected_schema_links")
        if expected_schema_links is not None and not isinstance(
            expected_schema_links, dict
        ):
            raise ValueError(f"{source}: expected_schema_links must be an object")

        reason_codes = raw.get("expected_reason_codes") or []
        if not isinstance(reason_codes, list) or not all(
            isinstance(reason, str) and reason.strip() for reason in reason_codes
        ):
            raise ValueError(f"{source}: expected_reason_codes must be non-empty strings")
        slice_tags = raw.get("slice_tags")
        if not isinstance(slice_tags, list) or not slice_tags:
            raise ValueError(f"{source}: slice_tags must be a non-empty list")
        normalized_slices = tuple(
            _identifier(tag, field_name="slice_tags entry", source=source)
            for tag in slice_tags
        )
        if len(set(normalized_slices)) != len(normalized_slices):
            raise ValueError(f"{source}: slice_tags must be unique")
        cancel_after_start = raw.get("cancel_after_start", False)
        if type(cancel_after_start) is not bool:
            raise ValueError(f"{source}: cancel_after_start must be boolean")
        profile = _identifier(
            raw.get("profile", "release"), field_name="profile", source=source
        )
        options = _request_options(raw.get("request_options"), source=source)
        review = ReviewRecord.from_mapping(raw.get("review"), source=source)

        if expected_outcome == "succeeded":
            if options["dry_run_only"]:
                if comparison_mode != "none":
                    raise ValueError(f"{source}: dry-run cases require comparison_mode=none")
            elif comparison_mode == "none" or expected_rows is None:
                raise ValueError(
                    f"{source}: executed success requires expected_rows comparison"
                )
            if reason_codes:
                raise ValueError(f"{source}: succeeded cases cannot expect reason codes")
        else:
            if comparison_mode != "none":
                raise ValueError(
                    f"{source}: non-success cases require comparison_mode=none"
                )
            if not reason_codes:
                raise ValueError(
                    f"{source}: non-success cases require expected_reason_codes"
                )
        if cancel_after_start and expected_outcome != "cancelled":
            raise ValueError(
                f"{source}: cancel_after_start requires expected_outcome=cancelled"
            )

        return cls(
            schema_version=CASE_SCHEMA_VERSION,
            id=case_id,
            question=question,
            dialect=dialect,
            fixture=fixture,
            expected_outcome=expected_outcome,
            comparison_mode=comparison_mode,
            expected_sql=expected_sql,
            expected_rows=expected_rows,
            expected_schema_links=expected_schema_links,
            expected_reason_codes=tuple(reason.strip() for reason in reason_codes),
            slice_tags=normalized_slices,
            cancel_after_start=cancel_after_start,
            request_options=options,
            profile=profile,
            review=review,
        )


@dataclass(frozen=True, slots=True)
class CaseCoverage:
    slices: tuple[str, ...]
    dialects: tuple[str, ...]
    missing_slices: tuple[str, ...]
    missing_dialects: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_slices and not self.missing_dialects


def _case_files(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(path.glob("*.jsonl"))
        if not files:
            raise ValueError(f"{path}: no JSONL gold files found")
        return files
    return [path]


def load_jsonl_cases(path: str | Path) -> list[TextToSQLEvalCase]:
    cases: list[TextToSQLEvalCase] = []
    for case_path in _case_files(Path(path)):
        with case_path.open("r", encoding="utf-8") as case_file:
            for line_no, line in enumerate(case_file, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{case_path}:{line_no}: malformed JSON"
                    ) from exc
                if not isinstance(raw, dict):
                    raise ValueError(
                        f"{case_path}:{line_no}: eval case must be an object"
                    )
                cases.append(
                    TextToSQLEvalCase.from_mapping(
                        raw,
                        source=f"{case_path}:{line_no}",
                    )
                )
    identifiers = [case.id for case in cases]
    duplicates = sorted(
        identifier for identifier in set(identifiers) if identifiers.count(identifier) > 1
    )
    if duplicates:
        raise ValueError(f"duplicate eval case ids: {', '.join(duplicates)}")
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


def validate_case_coverage(
    cases: Iterable[TextToSQLEvalCase],
    *,
    required_slices: Iterable[str],
    required_dialects: Iterable[str],
) -> CaseCoverage:
    case_list = list(cases)
    slices = {tag for case in case_list for tag in case.slice_tags}
    dialects = {case.dialect for case in case_list}
    expected_slices = {str(value) for value in required_slices}
    expected_dialects = {str(value) for value in required_dialects}
    return CaseCoverage(
        slices=tuple(sorted(slices)),
        dialects=tuple(sorted(dialects)),
        missing_slices=tuple(sorted(expected_slices - slices)),
        missing_dialects=tuple(sorted(expected_dialects - dialects)),
    )


def dump_jsonl_cases(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as output_file:
        for row in rows:
            output_file.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            output_file.write("\n")
