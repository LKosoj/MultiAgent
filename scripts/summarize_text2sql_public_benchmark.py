#!/usr/bin/env python3
"""Build per-case diagnostics and aggregate metrics for public Text-to-SQL runs."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import statistics
from typing import Any, Iterable, Mapping, Sequence


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected an object")
            rows.append(value)
    return rows


def _load_scores(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path}: expected a list")
    scores: dict[str, dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: score item is not an object")
        raw_case_key = row.get(
            "case_key",
            row.get("case_id", row.get("instance_id")),
        )
        if raw_case_key is None and row.get("ordinal") is not None:
            raw_case_key = row["ordinal"]
        case_key = str(raw_case_key)
        if not case_key or case_key == "None":
            raise ValueError(f"{path}: score item has no case identity")
        if case_key in scores:
            raise ValueError(f"{path}: duplicate score for {case_key}")
        scores[case_key] = row
    return scores


def _load_latest_checkpoints(
    path: Path | None,
) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    latest: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT timestamp, status, current_step, context, step_results
            FROM workflow_checkpoints
            ORDER BY timestamp
            """
        )
        for row in rows:
            context = json.loads(row["context"]) if row["context"] else {}
            variables = context.get("variables") if isinstance(context, dict) else {}
            run_id = variables.get("run_id") if isinstance(variables, dict) else None
            if not isinstance(run_id, str) or not run_id:
                continue
            latest[run_id] = {
                "checkpoint_timestamp": row["timestamp"],
                "checkpoint_status": row["status"],
                "current_step": row["current_step"],
                "step_results": (
                    json.loads(row["step_results"]) if row["step_results"] else {}
                ),
            }
    return latest


def _stage_diagnostics(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    step_results = checkpoint.get("step_results")
    if not isinstance(step_results, dict):
        step_results = {}
    stages: dict[str, Any] = {}
    for step_id, raw_result in step_results.items():
        if not isinstance(raw_result, dict):
            continue
        stages[step_id] = {
            "status": raw_result.get("status"),
            "duration_seconds": raw_result.get("duration_seconds"),
            "attempt_number": raw_result.get("attempt_number"),
            "error": raw_result.get("error"),
        }

    schema_result = step_results.get("schema_linking_step", {})
    schema_output = (
        schema_result.get("output") if isinstance(schema_result, dict) else {}
    )
    if not isinstance(schema_output, dict):
        schema_output = {}
    generation_result = step_results.get("sql_generation", {})
    generation_output = (
        generation_result.get("output")
        if isinstance(generation_result, dict)
        else {}
    )
    if not isinstance(generation_output, dict):
        generation_output = {}
    verification_result = step_results.get("sql_verification", {})
    verification_output = (
        verification_result.get("output")
        if isinstance(verification_result, dict)
        else {}
    )
    if not isinstance(verification_output, dict):
        verification_output = {}

    return {
        "checkpoint_status": checkpoint.get("checkpoint_status"),
        "checkpoint_timestamp": checkpoint.get("checkpoint_timestamp"),
        "stages": stages,
        "schema_linking": {
            "decision": schema_output.get("decision"),
            "decision_reasons": schema_output.get("decision_reasons") or [],
            "linking_strategy": schema_output.get("linking_strategy"),
            "confidence": schema_output.get("confidence"),
            "join_success": schema_output.get("join_success"),
            "unlinked_entities": schema_output.get("unlinked_entities") or [],
            "unresolved_entities": schema_output.get("unresolved_entities") or [],
            "ambiguous_bindings": schema_output.get("ambiguous_bindings") or [],
            "linked_entities": schema_output.get("linked_entities") or {},
            "joins": schema_output.get("joins") or [],
        },
        "generation": {
            "has_sql": bool(generation_output.get("sql")),
            "error": generation_output.get("error"),
        },
        "verification": {
            "status": verification_output.get("verification_status"),
            "recommendations": verification_output.get("recommendations") or [],
        },
    }


def classify_case(
    observation: Mapping[str, Any],
    score: Mapping[str, Any] | None,
) -> str:
    if observation.get("observation_status") != "completed":
        return "runner_or_transport_error"
    outcome = observation.get("outcome")
    if not isinstance(outcome, Mapping):
        return "missing_terminal_outcome"
    terminal_status = outcome.get("status")
    reason_code = str(outcome.get("reason_code") or "")
    if terminal_status != "succeeded":
        if reason_code.startswith("SCHEMA_"):
            return "schema_linking_abstention"
        if reason_code in {"VERIFIER_REJECTED", "SAFETY_REJECTED"}:
            return "verification_rejection"
        if reason_code == "EXECUTION_FAILED":
            return "pipeline_execution_failure"
        if terminal_status == "timed_out":
            return "pipeline_timeout"
        return "other_terminal_failure"
    if score is None:
        return "not_evaluated"
    if score.get("score") == 1:
        return "correct"
    error_info = str(score.get("error_info") or "")
    if error_info and error_info != "Result Error":
        return "prediction_execution_error"
    return "wrong_result"


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


def _latency_summary(values: Sequence[float]) -> dict[str, float | None]:
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


def summarize(
    observations: Sequence[Mapping[str, Any]],
    scores: Mapping[str, Mapping[str, Any]],
    checkpoints: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for observation in sorted(
        observations,
        key=lambda row: int(row.get("ordinal", 0)),
    ):
        case_id = str(observation["case_id"])
        raw_case_key = observation.get("case_key")
        if isinstance(raw_case_key, str) and raw_case_key:
            case_key = raw_case_key
        elif (
            observation.get("benchmark") == "bird"
            and isinstance(observation.get("ordinal"), int)
        ):
            case_key = f"bird:{observation['ordinal']}"
        else:
            case_key = case_id
        score = scores.get(case_key)
        checkpoint = checkpoints.get(str(observation.get("run_id") or ""), {})
        stage_data = _stage_diagnostics(checkpoint)
        outcome = observation.get("outcome")
        if not isinstance(outcome, Mapping):
            outcome = {}
        diagnostics.append(
            {
                "ordinal": observation.get("ordinal"),
                "case_key": case_key,
                "case_id": case_id,
                "database_id": observation.get("database_id"),
                "difficulty": observation.get("difficulty"),
                "run_id": observation.get("run_id"),
                "elapsed_seconds": observation.get("elapsed_seconds"),
                "observation_status": observation.get("observation_status"),
                "runner_error": observation.get("runner_error"),
                "terminal_status": outcome.get("status"),
                "reason_code": outcome.get("reason_code"),
                "generated": outcome.get("generated"),
                "approved": outcome.get("approved"),
                "executed": outcome.get("executed"),
                "sql": outcome.get("sql"),
                "official_score": score.get("score") if score else None,
                "official_error": score.get("error_info") if score else None,
                "failure_class": classify_case(observation, score),
                **stage_data,
            }
        )

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
    schema_reasons: Counter[str] = Counter()
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
        for reason in row["schema_linking"]["decision_reasons"]:
            schema_reasons[str(reason)] += 1
        for step_id, stage in row["stages"].items():
            duration = stage.get("duration_seconds")
            if isinstance(duration, (int, float)):
                step_durations[step_id].append(float(duration))

    summary = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
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
        "schema_decisions": dict(
            Counter(
                str(row["schema_linking"].get("decision") or "missing")
                for row in diagnostics
            )
        ),
        "schema_decision_reasons": dict(schema_reasons),
        "latency_seconds": _latency_summary(latency_values),
        "latency_seconds_by_failure_class": {
            failure_class: _latency_summary(values)
            for failure_class, values in sorted(latency_by_failure_class.items())
        },
        "latency_seconds_by_terminal_status": {
            terminal_status: _latency_summary(values)
            for terminal_status, values in sorted(
                latency_by_terminal_status.items()
            )
        },
        "step_latency_seconds": {
            step_id: _latency_summary(values)
            for step_id, values in sorted(step_durations.items())
        },
        "by_database": _group_summary(diagnostics, "database_id"),
        "by_difficulty": _group_summary(diagnostics, "difficulty"),
    }
    return diagnostics, summary


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--scores", type=Path)
    parser.add_argument("--workflow-state-db", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observations = _read_jsonl(args.observations)
    scores = _load_scores(args.scores)
    checkpoints = _load_latest_checkpoints(args.workflow_state_db)
    diagnostics, summary = summarize(observations, scores, checkpoints)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(args.output_dir / "diagnostics.jsonl", diagnostics)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
