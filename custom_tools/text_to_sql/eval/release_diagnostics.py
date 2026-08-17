"""Small, deterministic helpers for public benchmark diagnostic reports."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import statistics
import tempfile
from typing import Any, Callable, Mapping, Sequence

from .release_governance import governance_event_paths
from .sandbox import SandboxError, resolve_safe_regular_file


FAILURE_CLASS_ORDER = (
    "runner_or_transport_error",
    "typed_abstention",
    "pipeline_terminal_failure",
    "bad_sql",
    "wrong_result",
    "correct",
    "evaluator_failure",
    "evidence_incomplete",
)
_POST_REPEAT_CANDIDATE_FIELDS = frozenset(
    {
        "schema_version", "record_kind", "completed_case_count", "signature",
        "signature_case_count", "signature_share", "database_count",
        "database_ids", "completed_case_keys", "bundle_id", "benchmark",
        "repeat_ordinal", "evaluator_receipt_sha256", "diagnostics_sha256",
        "case_manifest_sha256", "manifest_sha256", "observations_sha256",
        "evaluation_manifest_sha256", "summary_sha256", "score_sha256",
        "evaluator_input_sha256",
    }
)


class DiagnosticArtifactError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_sealed(path: Path, expected: bytes, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise DiagnosticArtifactError(f"{label} is missing") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise DiagnosticArtifactError(f"{label} is unsafe")
    if path.read_bytes() != expected:
        raise DiagnosticArtifactError(f"{label} changed")
    if stat.S_IMODE(mode) != 0o444:
        raise DiagnosticArtifactError(f"{label} is not sealed")


def _write_new_or_identical_sealed(path: Path, payload: bytes, *, label: str) -> None:
    if path.exists() or path.is_symlink():
        _validate_sealed(path, payload, label=label)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError:
            _validate_sealed(path, payload, label=label)
        else:
            _fsync_parent(path)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def write_json_new_or_identical_sealed(
    path: Path, payload: Mapping[str, object], *, label: str
) -> str:
    _write_new_or_identical_sealed(path, _json_bytes(payload), label=label)
    return f"sha256:{_sha256_file(path)}"


def seal_existing_file(path: Path, *, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise DiagnosticArtifactError(f"{label} is missing or unsafe")
    path.chmod(0o444)
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_parent(path)
    mode = path.lstat().st_mode
    if stat.S_IMODE(mode) != 0o444:
        raise DiagnosticArtifactError(f"{label} is not sealed")
    return f"sha256:{_sha256_file(path)}"


def digest_existing_sealed_file(path: Path, *, label: str) -> str:
    """Return the digest of an already published immutable artifact."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise DiagnosticArtifactError(f"{label} is missing") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise DiagnosticArtifactError(f"{label} is unsafe")
    if stat.S_IMODE(mode) != 0o444:
        raise DiagnosticArtifactError(f"{label} is not sealed")
    return f"sha256:{_sha256_file(path)}"


def ordered_failure_rows(
    counts: Mapping[str, int],
    *,
    total: int,
) -> list[dict[str, int | float]]:
    """Return every public case class in the single published order."""
    if total < 0:
        raise ValueError("failure total must not be negative")
    unknown = set(counts) - set(FAILURE_CLASS_ORDER)
    if unknown:
        raise ValueError("unknown public failure class")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts.values()):
        raise ValueError("failure count is invalid")
    if sum(counts.values()) != total:
        raise ValueError("failure counts do not match total")
    return [
        {
            "failure_class": name,
            "count": counts.get(name, 0),
            "share": round(counts.get(name, 0) / total, 6) if total else 0.0,
        }
        for name in FAILURE_CLASS_ORDER
    ]


def failure_counts(classes: Sequence[str]) -> Counter[str]:
    counts = Counter(classes)
    ordered_failure_rows(counts, total=len(classes))
    return counts


def render_failure_rows(counts: Mapping[str, int], *, total: int) -> list[str]:
    return [
        f"- `{row['failure_class']}`: {row['count']} ({row['share']:.6f})"
        for row in ordered_failure_rows(counts, total=total)
    ]


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * fraction,
        3,
    )


def latency_summary(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "mean": round(statistics.mean(values), 3) if values else None,
        "median": round(statistics.median(values), 3) if values else None,
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 3) if values else None,
    }


def _group_summary(
    diagnostics: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in diagnostics:
        value = row.get(field)
        groups[str(value) if value is not None else "unknown"].append(row)
    result: dict[str, dict[str, Any]] = {}
    for key, rows in sorted(groups.items()):
        evaluated = [row for row in rows if row.get("official_score") is not None]
        correct = sum(row.get("official_score") == 1 for row in evaluated)
        result[key] = {
            "cases": len(rows),
            "evaluated": len(evaluated),
            "correct": correct,
            "execution_accuracy": (
                round(correct / len(evaluated), 6) if evaluated else None
            ),
        }
    return result


def _validate_diagnostic_aggregate_shape(
    diagnostics: Sequence[Mapping[str, Any]],
) -> None:
    for row in diagnostics:
        schema_research = row.get("schema_research")
        stages = row.get("stages")
        if not isinstance(schema_research, Mapping) or not isinstance(stages, Mapping):
            raise DiagnosticArtifactError("diagnostic aggregate shape is invalid")
        stop_reason = schema_research.get("stop_reason")
        terminal_reason = schema_research.get("terminal_reason_code")
        ready_for_sql = schema_research.get("ready_for_sql")
        if (
            stop_reason is not None
            and not isinstance(stop_reason, str)
            or terminal_reason is not None
            and not isinstance(terminal_reason, str)
            or ready_for_sql is not None
            and type(ready_for_sql) is not bool
        ):
            raise DiagnosticArtifactError("diagnostic aggregate shape is invalid")
        for step_id, stage in stages.items():
            if not isinstance(step_id, str) or not step_id or not isinstance(stage, Mapping):
                raise DiagnosticArtifactError("diagnostic aggregate shape is invalid")
            duration = stage.get("duration_seconds")
            if duration is not None and (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
            ):
                raise DiagnosticArtifactError("diagnostic aggregate shape is invalid")


def build_diagnostic_summary(
    diagnostics: Sequence[Mapping[str, Any]],
    *,
    created_at: str,
) -> dict[str, object]:
    """Build the one canonical aggregate summary for evaluated diagnostics."""
    _validate_diagnostic_aggregate_shape(diagnostics)
    evaluated = [row for row in diagnostics if row["official_score"] is not None]
    correct = sum(row["official_score"] == 1 for row in evaluated)
    executed = [row for row in diagnostics if row["executed"] is True]
    executed_evaluated = [row for row in evaluated if row["executed"] is True]
    executed_correct = sum(
        row["official_score"] == 1 for row in executed_evaluated
    )
    latency_values = [
        float(row["elapsed_seconds"])
        for row in diagnostics
        if isinstance(row.get("elapsed_seconds"), (int, float))
    ]
    latency_by_failure_class: dict[str, list[float]] = defaultdict(list)
    latency_by_terminal_status: dict[str, list[float]] = defaultdict(list)
    research_stop_reasons: Counter[str] = Counter()
    research_terminal_reasons: Counter[str] = Counter()
    step_durations: dict[str, list[float]] = defaultdict(list)
    for row in diagnostics:
        elapsed_seconds = row.get("elapsed_seconds")
        if isinstance(elapsed_seconds, (int, float)):
            latency_by_failure_class[row["failure_class"]].append(
                float(elapsed_seconds)
            )
            latency_by_terminal_status[str(row["terminal_status"])].append(
                float(elapsed_seconds)
            )
        research = row["schema_research"]
        research_stop_reasons[str(research.get("stop_reason") or "missing")] += 1
        research_terminal_reasons[
            str(research.get("terminal_reason_code") or "none")
        ] += 1
        for step_id, stage in row["stages"].items():
            duration = stage.get("duration_seconds")
            if isinstance(duration, (int, float)):
                step_durations[step_id].append(float(duration))
    return {
        "schema_version": 1,
        "created_at": created_at,
        "cases": len(diagnostics),
        "evaluated": len(evaluated),
        "correct": correct,
        "execution_accuracy": (
            round(correct / len(evaluated), 6) if evaluated else None
        ),
        "execution_coverage": (
            round(len(executed_evaluated) / len(evaluated), 6)
            if evaluated
            else None
        ),
        "conditional_execution_accuracy": (
            round(executed_correct / len(executed_evaluated), 6)
            if executed_evaluated
            else None
        ),
        "terminal_statuses": dict(
            Counter(str(row["terminal_status"]) for row in diagnostics)
        ),
        "reason_codes": dict(
            Counter(str(row["reason_code"] or "") for row in diagnostics)
        ),
        "failure_classes": dict(
            Counter(row["failure_class"] for row in diagnostics)
        ),
        "generated": sum(row["generated"] is True for row in diagnostics),
        "executed": len(executed),
        "research_stop_reasons": dict(research_stop_reasons),
        "research_terminal_reasons": dict(research_terminal_reasons),
        "latency_seconds": latency_summary(latency_values),
        "latency_seconds_by_failure_class": {
            failure_class: latency_summary(values)
            for failure_class, values in sorted(latency_by_failure_class.items())
        },
        "latency_seconds_by_terminal_status": {
            terminal_status: latency_summary(values)
            for terminal_status, values in sorted(
                latency_by_terminal_status.items()
            )
        },
        "step_latency_seconds": {
            step_id: latency_summary(values)
            for step_id, values in sorted(step_durations.items())
        },
        "by_database": _group_summary(diagnostics, "database_id"),
        "by_difficulty": _group_summary(diagnostics, "difficulty"),
    }


def _read_jsonl_objects(path: Path, *, label: str) -> list[dict[str, object]]:
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticArtifactError(f"{label} are invalid") from exc
    if not all(isinstance(row, dict) for row in rows):
        raise DiagnosticArtifactError(f"{label} are invalid")
    return rows


def _safe_artifact_digest(leg_dir: Path, relative: str, *, label: str) -> str:
    path = leg_dir / relative
    try:
        resolved = resolve_safe_regular_file(path, label=label)
        root = leg_dir.resolve(strict=True)
    except (OSError, SandboxError) as exc:
        raise DiagnosticArtifactError(f"{label} is missing or unsafe") from exc
    if not resolved.is_relative_to(root):
        raise DiagnosticArtifactError(f"{label} is missing or unsafe")
    return f"sha256:{_sha256_file(resolved)}"


def is_canonical_relative_artifact_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or Path(value).is_absolute()
    ):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def validate_post_repeat_candidate_artifacts(
    leg_dir: Path,
    candidate: Mapping[str, object],
    *,
    expected_bundle_id: str,
    expected_benchmark: str,
    expected_repeat_ordinal: int,
) -> None:
    """Bind a paused post-repeat decision to the exact evaluated artifacts."""
    if (
        set(candidate) != _POST_REPEAT_CANDIDATE_FIELDS
        or candidate.get("schema_version") != 1
        or candidate.get("record_kind")
        != "text2sql_public_benchmark_post_repeat_candidate"
        or candidate.get("bundle_id") != expected_bundle_id
        or candidate.get("benchmark") != expected_benchmark
        or candidate.get("repeat_ordinal") != expected_repeat_ordinal
    ):
        raise DiagnosticArtifactError("post-repeat candidate identity is invalid")
    direct_files = {
        "evaluation_manifest_sha256": "evaluation_manifest.json",
        "evaluator_receipt_sha256": "evaluator_receipt.json",
        "diagnostics_sha256": "diagnostics.jsonl",
        "summary_sha256": "summary.json",
        "case_manifest_sha256": "case_manifest.json",
        "manifest_sha256": "manifest.json",
        "observations_sha256": "observations.jsonl",
    }
    for field, relative in direct_files.items():
        if candidate.get(field) != _safe_artifact_digest(
            leg_dir, relative, label=f"post-repeat {relative}"
        ):
            raise DiagnosticArtifactError(
                "post-repeat candidate artifact binding changed"
            )
    try:
        evaluation = json.loads(
            (leg_dir / "evaluation_manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticArtifactError(
            "post-repeat evaluation manifest is invalid"
        ) from exc
    if not isinstance(evaluation, Mapping):
        raise DiagnosticArtifactError("post-repeat evaluation manifest is invalid")
    manifest_bindings = {
        "evaluator_receipt_sha256": "evaluator_receipt_sha256",
        "run_manifest_sha256": "manifest_sha256",
        "case_manifest_sha256": "case_manifest_sha256",
        "diagnostics_sha256": "diagnostics_sha256",
        "summary_sha256": "summary_sha256",
        "score_sha256": "score_sha256",
        "evaluator_input_sha256": "evaluator_input_sha256",
    }
    if any(
        evaluation.get(manifest_field) != candidate.get(candidate_field)
        for manifest_field, candidate_field in manifest_bindings.items()
    ):
        raise DiagnosticArtifactError(
            "post-repeat candidate evaluation binding changed"
        )
    for path_field, digest_field in (
        ("score_path", "score_sha256"),
        ("evaluator_input_path", "evaluator_input_sha256"),
    ):
        relative = evaluation.get(path_field)
        if not is_canonical_relative_artifact_path(relative):
            raise DiagnosticArtifactError(
                "post-repeat evaluation manifest is invalid"
            )
        assert isinstance(relative, str)
        if candidate.get(digest_field) != _safe_artifact_digest(
            leg_dir, relative, label=f"post-repeat {path_field}"
        ):
            raise DiagnosticArtifactError(
                "post-repeat candidate evaluation binding changed"
            )


def validate_post_repeat_case_evidence(
    leg_dir: Path,
    *,
    expected_case_keys: Sequence[str] | None = None,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
]:
    """Validate row alignment and counters for one completed repeat."""
    try:
        case_manifest = json.loads(
            (leg_dir / "case_manifest.json").read_text(encoding="utf-8")
        )
        summary = json.loads((leg_dir / "summary.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticArtifactError("post-repeat case evidence is invalid") from exc
    if not isinstance(case_manifest, Mapping) or not isinstance(summary, dict):
        raise DiagnosticArtifactError("post-repeat case evidence is invalid")
    cases = case_manifest.get("cases")
    if not isinstance(cases, list) or not all(
        isinstance(row, Mapping) for row in cases
    ):
        raise DiagnosticArtifactError("post-repeat case manifest is invalid")
    case_keys = [row.get("case_key") for row in cases]
    if (
        any(not isinstance(key, str) or not key for key in case_keys)
        or len(case_keys) != len(set(case_keys))
        or (
            expected_case_keys is not None
            and case_keys != list(expected_case_keys)
        )
    ):
        raise DiagnosticArtifactError("post-repeat case keys are invalid")
    observations = _read_jsonl_objects(
        leg_dir / "observations.jsonl", label="post-repeat observations"
    )
    diagnostics = _read_jsonl_objects(
        leg_dir / "diagnostics.jsonl", label="post-repeat diagnostics"
    )
    if [row.get("case_key") for row in observations] != case_keys or [
        row.get("case_key") for row in diagnostics
    ] != case_keys:
        raise DiagnosticArtifactError("post-repeat case evidence is not aligned")
    created_at = summary.get("created_at")
    try:
        if not isinstance(created_at, str):
            raise ValueError("created_at is missing")
        timestamp = datetime.fromisoformat(created_at)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("created_at has no timezone")
        failure_counts([str(row.get("failure_class")) for row in diagnostics])
        expected_summary = build_diagnostic_summary(
            diagnostics,
            created_at=created_at,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise DiagnosticArtifactError(
            "post-repeat summary counters are invalid"
        ) from exc
    if summary != expected_summary:
        raise DiagnosticArtifactError("post-repeat summary counters are invalid")
    return observations, diagnostics, summary


def _continue_result_payload(
    *,
    event_kind: str,
    candidate: Mapping[str, object],
    candidate_sha256: str,
    decision_sha256: str,
) -> dict[str, object]:
    completed = candidate.get("completed_case_count")
    observations_sha256 = candidate.get("observations_sha256")
    benchmark = candidate.get("benchmark")
    repeat_ordinal = candidate.get("repeat_ordinal")
    if (
        not isinstance(completed, int)
        or isinstance(completed, bool)
        or completed <= 0
        or not isinstance(observations_sha256, str)
        or not observations_sha256.startswith("sha256:")
        or not isinstance(benchmark, str)
        or not benchmark
        or not isinstance(repeat_ordinal, int)
        or isinstance(repeat_ordinal, bool)
        or repeat_ordinal <= 0
    ):
        raise DiagnosticArtifactError("CONTINUE candidate evidence is invalid")
    return {
        "schema_version": 1,
        "record_kind": "text2sql_public_benchmark_early_stop",
        "event_kind": event_kind,
        "benchmark": benchmark,
        "repeat_ordinal": repeat_ordinal,
        "decision": "CONTINUE",
        "candidate_sha256": candidate_sha256,
        "repair_decision_sha256": decision_sha256,
        "completed_case_count": completed,
        "observations_sha256": observations_sha256,
    }


def validate_continue_decision(
    leg_dir: Path,
    *,
    event_kind: str,
    completed_case_count: int,
    expected: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate one closed candidate/decision/result CONTINUE event."""

    try:
        paths = governance_event_paths(event_kind, completed_case_count)
    except ValueError as exc:
        raise DiagnosticArtifactError(str(exc)) from exc
    resolved = {name: leg_dir / relative for name, relative in paths.items()}
    for name, path in resolved.items():
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as exc:
            raise DiagnosticArtifactError(
                f"CONTINUE governance {name} is missing"
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise DiagnosticArtifactError(f"CONTINUE governance {name} is unsafe")
        if stat.S_IMODE(mode) != 0o444:
            raise DiagnosticArtifactError(
                f"CONTINUE governance {name} is not sealed"
            )
    try:
        candidate = json.loads(resolved["candidate_path"].read_text(encoding="utf-8"))
        decision = json.loads(resolved["decision_path"].read_text(encoding="utf-8"))
        result = json.loads(resolved["result_path"].read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticArtifactError("CONTINUE governance JSON is invalid") from exc
    if not all(isinstance(item, Mapping) for item in (candidate, decision, result)):
        raise DiagnosticArtifactError("CONTINUE governance JSON is invalid")
    if candidate.get("completed_case_count") != completed_case_count:
        raise DiagnosticArtifactError("CONTINUE candidate count is invalid")
    candidate_digest = f"sha256:{_sha256_file(resolved['candidate_path'])}"
    decision_digest = f"sha256:{_sha256_file(resolved['decision_path'])}"
    if (
        decision.get("decision") != "CONTINUE"
        or decision.get("candidate_sha256") != candidate_digest
    ):
        raise DiagnosticArtifactError("CONTINUE repair decision is invalid")
    payload = _continue_result_payload(
        event_kind=event_kind,
        candidate=candidate,
        candidate_sha256=candidate_digest,
        decision_sha256=decision_digest,
    )
    if dict(result) != payload:
        raise DiagnosticArtifactError("CONTINUE final evidence changed")
    event = {
        "event_kind": event_kind,
        "benchmark": candidate["benchmark"],
        "repeat_ordinal": candidate["repeat_ordinal"],
        "completed_case_count": completed_case_count,
        **paths,
        "candidate_sha256": candidate_digest,
        "decision_sha256": decision_digest,
        "result_sha256": f"sha256:{_sha256_file(resolved['result_path'])}",
    }
    if expected is not None and dict(expected) != event:
        raise DiagnosticArtifactError("CONTINUE governance inventory changed")
    return event


def finalize_continue_decision(
    leg_dir: Path,
    *,
    event_kind: str,
    completed_case_count: int,
) -> dict[str, object]:
    """Create immutable final evidence for a confirmed CONTINUE decision."""

    try:
        paths = governance_event_paths(event_kind, completed_case_count)
    except ValueError as exc:
        raise DiagnosticArtifactError(str(exc)) from exc
    candidate_path = leg_dir / paths["candidate_path"]
    decision_path = leg_dir / paths["decision_path"]
    candidate_digest = digest_existing_sealed_file(
        candidate_path, label=f"{event_kind} CONTINUE candidate"
    )
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticArtifactError("CONTINUE governance JSON is invalid") from exc
    if not isinstance(candidate, Mapping) or not isinstance(decision, Mapping):
        raise DiagnosticArtifactError("CONTINUE governance JSON is invalid")
    if candidate.get("completed_case_count") != completed_case_count:
        raise DiagnosticArtifactError("CONTINUE candidate count is invalid")
    decision_digest = seal_existing_file(
        decision_path, label=f"{event_kind} CONTINUE decision"
    )
    if (
        decision.get("decision") != "CONTINUE"
        or decision.get("candidate_sha256")
        != candidate_digest
    ):
        raise DiagnosticArtifactError("CONTINUE repair decision is invalid")
    payload = _continue_result_payload(
        event_kind=event_kind,
        candidate=candidate,
        candidate_sha256=candidate_digest,
        decision_sha256=decision_digest,
    )
    _write_new_or_identical_sealed(
        leg_dir / paths["result_path"],
        _json_bytes(payload),
        label=f"{event_kind} CONTINUE final evidence",
    )
    return validate_continue_decision(
        leg_dir,
        event_kind=event_kind,
        completed_case_count=completed_case_count,
    )


def _diagnostics_from_observations(
    observations: Sequence[Mapping[str, object]],
    *,
    classify: Callable[[Mapping[str, object]], str],
) -> list[dict[str, object]]:
    return [
        {
            "case_key": row.get("case_key"),
            "database_id": row.get("database_id"),
            "failure_class": classify(row),
            "official_score": None,
        }
        for row in observations
    ]


def _reviewed_repair_lines(decision_path: Path) -> list[str]:
    """Render the already reviewed repair decision without raw case inputs."""
    try:
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(decision, Mapping):
        return []
    required = (
        "reviewed_by",
        "reviewed_at",
        "root_hypothesis",
        "red_test_plan",
        "predicted_improvement",
    )
    if any(
        not isinstance(decision.get(field), str) or not decision[field].strip()
        for field in required
    ):
        return []
    guarantees = decision.get("safety_guarantees")
    if not isinstance(guarantees, list) or any(
        not isinstance(item, str) or not item.strip() for item in guarantees
    ):
        return []
    return [
        "## Reviewed repair decision",
        "",
        f"- Reviewer: {decision['reviewed_by']} at {decision['reviewed_at']}",
        f"- Root-cause hypothesis: {decision['root_hypothesis']}",
        f"- Regression test plan: {decision['red_test_plan']}",
        f"- Expected improvement: {decision['predicted_improvement']}",
        "- Safety checks: " + "; ".join(guarantees),
        "",
    ]


def finalize_partial_stop(
    leg_dir: Path,
    *,
    candidate: Mapping[str, object],
    candidate_path: Path,
    decision_path: Path,
    observations: Sequence[Mapping[str, object]],
    classify: Callable[[Mapping[str, object]], str],
    release_plan: Sequence[Mapping[str, object]],
    evaluation_leg: Mapping[str, object],
) -> dict[str, str]:
    """Create and seal the incomplete, unscored diagnostic terminal set."""

    candidate_digest = digest_existing_sealed_file(
        candidate_path, label="early-stop candidate"
    )
    decision_digest = seal_existing_file(
        decision_path, label="repair decision"
    )
    diagnostics = _diagnostics_from_observations(observations, classify=classify)
    counts = failure_counts(
        [str(item["failure_class"]) for item in diagnostics]
    )
    total = len(diagnostics)
    not_started = candidate.get("not_started_case_keys")
    if not isinstance(not_started, list):
        raise DiagnosticArtifactError("partial stop candidate is invalid")
    identity = tuple(
        evaluation_leg.get(field)
        for field in ("benchmark", "repeat_ordinal", "seed")
    )
    current_index = next(
        (
            index
            for index, item in enumerate(release_plan)
            if (
                item.get("benchmark"), item.get("repeat_ordinal"), item.get("seed")
            ) == identity
        ),
        None,
    )
    if current_index is None:
        raise DiagnosticArtifactError("partial stop has no canonical release leg")
    observations_sha256 = f"sha256:{_sha256_file(leg_dir / 'observations.jsonl')}"
    completed_case_count = candidate.get("completed_case_count")
    if not isinstance(completed_case_count, int) or isinstance(
        completed_case_count, bool
    ) or completed_case_count <= 0:
        raise DiagnosticArtifactError("partial stop candidate count is invalid")
    try:
        governance_paths = governance_event_paths(
            "mid_repeat", completed_case_count
        )
    except ValueError as exc:
        raise DiagnosticArtifactError(str(exc)) from exc
    early_stop_path = governance_paths["result_path"]
    report = [
        "# Diagnostic early stop", "", "Status: **INCOMPLETE / NOT_SCORED**", "",
        "## Verified signature", "",
        "- Signature: `" + json.dumps(candidate.get("signature"), sort_keys=True) + "`",
        f"- Completed: {total}; not started: {len(not_started)}; total: {total + len(not_started)}.",
        f"- Completed cases: {total}",
        f"- Not-started release legs: {len(release_plan[current_index + 1:])}",
        f"- Observations evidence: observations.jsonl ({observations_sha256})",
        "", "## Failure classes", "", *render_failure_rows(counts, total=total),
        "", "## Evidence", "",
        f"- `{governance_paths['candidate_path']}`",
        f"- `{governance_paths['decision_path']}`", "- `observations.jsonl`",
        "- `diagnostics.jsonl`", "- `manifest.json`", "- `case_manifest.json`",
        "- `empty_history_evidence.json`", "",
        *_reviewed_repair_lines(decision_path),
        "The reviewed hypothesis is a repair plan, not a proven root cause. "
        "This report omits questions and SQL.", "",
    ]
    artifacts = {
        "diagnostics.jsonl": "".join(
            json.dumps(row, sort_keys=True) + "\n" for row in diagnostics
        ).encode("utf-8"),
        early_stop_path: _json_bytes(
            {
                "schema_version": 1,
                "record_kind": "text2sql_public_benchmark_early_stop",
                "candidate_sha256": candidate_digest,
                "repair_decision_sha256": decision_digest,
                "decision": "STOP_AND_REPAIR",
                "status": "diagnostic_early_stop",
            }
        ),
        "diagnostic_summary.json": _json_bytes(
            {
                "schema_version": 1,
                "status": "INCOMPLETE",
                "score_status": "NOT_SCORED",
                "completed_case_count": total,
            }
        ),
        "failure_report.md": "\n".join(report).encode("utf-8"),
    }
    for name, payload in artifacts.items():
        _write_new_or_identical_sealed(leg_dir / name, payload, label=name)
    return {
        name: f"sha256:{_sha256_file(leg_dir / name)}" for name in artifacts
    }


def finalize_post_repeat_stop(
    leg_dir: Path,
    *,
    candidate_path: Path,
    decision_path: Path,
    release_plan: Sequence[Mapping[str, object]],
    evaluation_leg: Mapping[str, object],
    expected_bundle_id: str,
) -> dict[str, str]:
    """Create and seal the scored diagnostic terminal set."""

    candidate_digest = digest_existing_sealed_file(
        candidate_path, label="post-repeat candidate"
    )
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticArtifactError("post-repeat candidate is invalid") from exc
    if not isinstance(candidate, Mapping):
        raise DiagnosticArtifactError("post-repeat candidate is invalid")
    benchmark = evaluation_leg.get("benchmark")
    repeat_ordinal = evaluation_leg.get("repeat_ordinal")
    seed = evaluation_leg.get("seed")
    identity = (benchmark, repeat_ordinal, seed)
    current_index = next(
        (
            index
            for index, item in enumerate(release_plan)
            if (
                item.get("benchmark"), item.get("repeat_ordinal"), item.get("seed")
            ) == identity
        ),
        None,
    )
    if current_index is None or not isinstance(benchmark, str) or not isinstance(
        repeat_ordinal, int
    ):
        raise DiagnosticArtifactError(
            "post-repeat stop has no canonical release leg"
        )
    validate_post_repeat_candidate_artifacts(
        leg_dir,
        candidate,
        expected_bundle_id=expected_bundle_id,
        expected_benchmark=benchmark,
        expected_repeat_ordinal=repeat_ordinal,
    )
    not_started_legs = [
        {
            "benchmark": item["benchmark"],
            "repeat_ordinal": item["repeat_ordinal"],
            "seed": item["seed"],
        }
        for item in release_plan[current_index + 1 :]
    ]
    _observations, diagnostics, _summary = validate_post_repeat_case_evidence(
        leg_dir
    )
    decision_digest = seal_existing_file(
        decision_path, label="post-repeat repair decision"
    )
    counts = failure_counts([str(row.get("failure_class")) for row in diagnostics])
    total = len(_observations)
    completed_case_count = candidate.get("completed_case_count")
    if not isinstance(completed_case_count, int) or isinstance(
        completed_case_count, bool
    ) or completed_case_count <= 0:
        raise DiagnosticArtifactError("post-repeat candidate count is invalid")
    try:
        governance_paths = governance_event_paths(
            "post_repeat", completed_case_count
        )
    except ValueError as exc:
        raise DiagnosticArtifactError(str(exc)) from exc
    early_stop_path = governance_paths["result_path"]
    early_stop = {
        "schema_version": 1,
        "record_kind": "text2sql_public_benchmark_early_stop",
        "status": "diagnostic_post_repeat_stop",
        "benchmark": benchmark,
        "repeat_ordinal": repeat_ordinal,
        "candidate_sha256": candidate_digest,
        "repair_decision_sha256": decision_digest,
        "signature": candidate.get("signature"),
        "evaluator_receipt_sha256": candidate.get("evaluator_receipt_sha256"),
        "diagnostics_sha256": candidate.get("diagnostics_sha256"),
        "case_manifest_sha256": candidate.get("case_manifest_sha256"),
        "manifest_sha256": candidate.get("manifest_sha256"),
        "observations_sha256": candidate.get("observations_sha256"),
        "not_started_legs": not_started_legs,
    }
    report = [
        "# Diagnostic post-repeat stop", "", "Status: **FULL / SCORED DIAGNOSTIC**", "",
        "## Verified signature", "",
        "- Signature: `" + json.dumps(candidate.get("signature"), sort_keys=True) + "`",
        f"- Candidate: `{candidate_digest}`", f"- Repair decision: `{decision_digest}`",
        f"- Completed cases: {total}",
        f"- Not-started release legs: {len(not_started_legs)}",
        "- Observations evidence: observations.jsonl "
        f"({candidate.get('observations_sha256')})",
        "", "## Failure classes", "", *render_failure_rows(counts, total=total),
        "", "## Not started release legs", "",
        *(f"- `{item['benchmark']}:r{item['repeat_ordinal']}`" for item in not_started_legs),
        "", "## Evidence", "",
        f"- `{governance_paths['candidate_path']}`",
        f"- `{governance_paths['decision_path']}`", "- `evaluator_receipt.json`",
        "- `diagnostics.jsonl`", "- `summary.json`", "- `manifest.json`",
        "- `case_manifest.json`", "",
        *_reviewed_repair_lines(decision_path),
        "The reviewed hypothesis is a repair plan, not a proven root cause. "
        "This report omits questions and SQL.", "",
    ]
    artifacts = {
        early_stop_path: _json_bytes(early_stop),
        "early_stop_report.md": "\n".join(report).encode("utf-8"),
    }
    for name, payload in artifacts.items():
        _write_new_or_identical_sealed(leg_dir / name, payload, label=name)
    return {
        name: f"sha256:{_sha256_file(leg_dir / name)}" for name in artifacts
    }


def validate_terminal_artifacts(
    leg_dir: Path,
    *,
    names: Sequence[str],
    expected_sha256: Mapping[str, str] | None = None,
) -> dict[str, str]:
    digests: dict[str, str] = {}
    for name in names:
        path = leg_dir / name
        if path.is_symlink() or not path.is_file():
            raise DiagnosticArtifactError(f"terminal artifact {name} is missing or unsafe")
        if stat.S_IMODE(path.lstat().st_mode) != 0o444:
            raise DiagnosticArtifactError(f"terminal artifact {name} is not sealed")
        digest = f"sha256:{_sha256_file(path)}"
        if expected_sha256 is not None and expected_sha256.get(name) != digest:
            raise DiagnosticArtifactError(f"terminal artifact {name} changed")
        digests[name] = digest
    return digests
