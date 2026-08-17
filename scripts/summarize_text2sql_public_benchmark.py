#!/usr/bin/env python3
"""Build per-case diagnostics and aggregate metrics for public Text-to-SQL runs."""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import text2sql_benchmark_reporting as benchmark_reporting  # noqa: E402
from custom_tools.text_to_sql.eval.release_diagnostics import (  # noqa: E402
    FAILURE_CLASS_ORDER,
    build_diagnostic_summary,
    latency_summary,
    render_failure_rows,
)

_latency_summary = latency_summary


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


def _validate_evaluator_receipt(
    path: Path, score_path: Path, scores: Mapping[str, Any],
    *, expected_case_keys: Sequence[str] | None = None,
    evaluator_input: Path | None = None,
    run_manifest: Mapping[str, Any] | None = None,
    run_manifest_path: Path | None = None,
    case_manifest: Mapping[str, Any] | None = None,
    case_manifest_path: Path | None = None,
) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not benchmark_reporting.evaluator_receipt_is_closed(value):
        raise ValueError("evaluator receipt is invalid")
    if value["score_sha256"] != "sha256:" + hashlib.sha256(score_path.read_bytes()).hexdigest():
        raise ValueError("evaluator receipt score digest mismatch")
    if (
        not isinstance(value["case_keys"], list)
        or len(value["case_keys"]) != len(set(value["case_keys"]))
        or set(value["case_keys"]) != set(scores)
        or (expected_case_keys is not None and value["case_keys"] != list(expected_case_keys))
    ):
        raise ValueError("evaluator receipt case keys mismatch")
    if evaluator_input is not None and value["evaluator_input_sha256"] != (
        "sha256:" + hashlib.sha256(evaluator_input.read_bytes()).hexdigest()
    ):
        raise ValueError("evaluator receipt input digest mismatch")
    if run_manifest is not None:
        identity = run_manifest.get("evaluator_identity")
        if not isinstance(identity, Mapping) or value["evaluator_identity"] != identity:
            raise ValueError("evaluator receipt identity is not pinned by run manifest")
    if run_manifest_path is not None and value["run_manifest_sha256"] != (
        "sha256:" + hashlib.sha256(run_manifest_path.read_bytes()).hexdigest()
    ):
        raise ValueError("evaluator receipt run manifest digest mismatch")
    if case_manifest_path is not None and value["case_manifest_sha256"] != (
        "sha256:" + hashlib.sha256(case_manifest_path.read_bytes()).hexdigest()
    ):
        raise ValueError("evaluator receipt case manifest digest mismatch")
    if case_manifest is not None:
        cases = case_manifest.get("cases")
        if not isinstance(cases, list):
            raise ValueError("case manifest is invalid")
        manifest_keys = [
            row.get("case_key") for row in cases if isinstance(row, Mapping)
        ]
        if (
            len(manifest_keys) != len(cases)
            or any(not isinstance(key, str) or not key for key in manifest_keys)
            or value["case_keys"] != manifest_keys
        ):
            raise ValueError("evaluator receipt does not match case manifest order")
    return value


def _load_latest_checkpoints(
    paths: Sequence[Path],
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for path in paths:
        with sqlite3.connect(
            f"file:{path.resolve()}?mode=ro",
            uri=True,
        ) as connection:
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
                variables = (
                    context.get("variables") if isinstance(context, dict) else {}
                )
                run_id = (
                    variables.get("run_id")
                    if isinstance(variables, dict)
                    else None
                )
                if not isinstance(run_id, str) or not run_id:
                    continue
                checkpoint = {
                    "checkpoint_timestamp": row["timestamp"],
                    "checkpoint_status": row["status"],
                    "current_step": row["current_step"],
                    "step_results": (
                        json.loads(row["step_results"])
                        if row["step_results"]
                        else {}
                    ),
                }
                previous = latest.get(run_id)
                if (
                    previous is None
                    or str(previous["checkpoint_timestamp"]) < row["timestamp"]
                ):
                    latest[run_id] = checkpoint
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

    research_result = step_results.get("schema_research", {})
    research_output = (
        research_result.get("output") if isinstance(research_result, dict) else {}
    )
    if not isinstance(research_output, dict):
        research_output = {}
    solving_result = step_results.get("sql_solving", {})
    solving_output = (
        solving_result.get("output")
        if isinstance(solving_result, dict)
        else {}
    )
    if not isinstance(solving_output, dict):
        solving_output = {}

    return {
        "checkpoint_status": checkpoint.get("checkpoint_status"),
        "checkpoint_timestamp": checkpoint.get("checkpoint_timestamp"),
        "stages": stages,
        "schema_research": {
            "stop_reason": research_output.get("stop_reason"),
            "ready_for_sql": research_output.get("ready_for_sql"),
            "terminal_reason_code": research_output.get("terminal_reason_code"),
            "schema_namespace_version": research_output.get(
                "schema_namespace_version"
            ),
        },
        "sql_solving": {
            "has_sql": bool(solving_output.get("sql")),
            "description": solving_output.get("description"),
        },
    }


def classify_case(
    observation: Mapping[str, Any],
    score: Mapping[str, Any] | None,
    evaluator_receipt: Mapping[str, Any] | None = None,
) -> str:
    result = benchmark_reporting.failure_class(
        observation,
        score.get("score") if score is not None else None,
        evaluator_receipt,
    )
    if result != "wrong_result":
        return result
    error_info = str(score.get("error_info") or "")
    if error_info and error_info != "Result Error":
        return "bad_sql"
    return result


def _build_case_diagnostics(
    observations: Sequence[Mapping[str, Any]],
    scores: Mapping[str, Mapping[str, Any]],
    checkpoints: Mapping[str, Mapping[str, Any]],
    evaluator_receipt: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
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
                "failure_class": classify_case(
                    observation, score, evaluator_receipt
                ),
                **stage_data,
            }
        )
    return diagnostics


def summarize(
    observations: Sequence[Mapping[str, Any]],
    scores: Mapping[str, Mapping[str, Any]],
    checkpoints: Mapping[str, Mapping[str, Any]],
    evaluator_receipt: Mapping[str, Any] | None = None,
    *,
    created_at: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    diagnostics = _build_case_diagnostics(
        observations, scores, checkpoints, evaluator_receipt
    )

    summary = build_diagnostic_summary(
        diagnostics,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )
    return diagnostics, summary


def _publish_bytes_sealed(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        if (
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_mode & 0o777 == 0o444
            and path.read_bytes() == payload
        ):
            return
        raise ValueError(f"sealed summary artifact differs: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if not (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_mode & 0o777 == 0o444
                and path.read_bytes() == payload
            ):
                raise ValueError(f"sealed summary artifact differs: {path.name}")
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    payload = "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for row in rows
    ).encode("utf-8")
    _publish_bytes_sealed(path, payload)


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_artifact_path(path: Path, output_dir: Path, *, label: str) -> str:
    resolved = path.resolve(strict=True)
    root = output_dir.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file() or not resolved.is_relative_to(root):
        raise ValueError(f"{label} must be a regular file inside --output-dir")
    return resolved.relative_to(root).as_posix()


def _failure_report(
    summary: Mapping[str, Any],
    *,
    not_started_release_legs: int = 0,
    observations_sha256: str = "unavailable",
) -> str:
    status = "FULL / SCORED" if summary.get("evaluated") else "INCOMPLETE / NOT_SCORED"
    failure_classes = summary["failure_classes"]
    lines = [
        "# Text-to-SQL benchmark report",
        "",
        f"Status: **{status}**",
        "",
        "## Verified conclusion",
        "",
        (
            f"Processed cases: {summary['cases']}; official scores: "
            f"{summary['evaluated']}; correct scored results: {summary['correct']}."
        ),
        f"Completed cases: {summary['cases']}",
        f"Not-started release legs: {not_started_release_legs}",
        f"Observations evidence: observations.jsonl ({observations_sha256})",
        "The counts below are verified observations, not a claim about root cause.",
        "",
        "## Failure classes",
        "",
    ]
    counts = {name: int(failure_classes.get(name, 0)) for name in FAILURE_CLASS_ORDER}
    lines.extend(render_failure_rows(counts, total=int(summary["cases"])))
    lines.extend(
        [
            "",
            "## General repair plan",
            "",
            "1. Reproduce the largest verified failure class from `diagnostics.jsonl` and `summary.json`.",
            "2. Add a focused regression test before changing the pipeline.",
            "3. If this is an early stop, record a separately reviewed `repair_decision.json` bound to the candidate before resuming.",
            "",
            "Evidence: `diagnostics.jsonl`, `summary.json`, and `evaluator_receipt.json` when scored.",
            "This report intentionally omits SQL and questions.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--scores", type=Path)
    parser.add_argument("--evaluator-receipt", type=Path)
    parser.add_argument("--evaluator-input", type=Path)
    parser.add_argument("--run-manifest", type=Path)
    parser.add_argument("--case-manifest", type=Path)
    parser.add_argument("--workflow-state-db", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--created-at")
    parser.add_argument("--not-started-release-legs", type=int, default=0)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observations = _read_jsonl(args.observations)
    scores = _load_scores(args.scores)
    evaluator_paths = (
        args.evaluator_receipt,
        args.evaluator_input,
        args.run_manifest,
        args.case_manifest,
    )
    if scores and any(path is None for path in evaluator_paths):
        raise ValueError(
            "official scores require --evaluator-receipt, --evaluator-input, "
            "--run-manifest, and --case-manifest"
        )
    evaluator_receipt = None
    if scores:
        evaluator_receipt = _validate_evaluator_receipt(
            args.evaluator_receipt,
            args.scores,
            scores,
            evaluator_input=args.evaluator_input,
            run_manifest=json.loads(args.run_manifest.read_text(encoding="utf-8")),
            run_manifest_path=args.run_manifest,
            case_manifest=json.loads(args.case_manifest.read_text(encoding="utf-8")),
            case_manifest_path=args.case_manifest,
        )
    checkpoints = _load_latest_checkpoints(args.workflow_state_db)
    diagnostics, summary = summarize(
        observations,
        scores,
        checkpoints,
        evaluator_receipt,
        created_at=args.created_at,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(args.output_dir / "diagnostics.jsonl", diagnostics)
    _publish_bytes_sealed(
        args.output_dir / "summary.json",
        (
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
    )
    _publish_bytes_sealed(
        args.output_dir / "failure_report.md",
        _failure_report(
            summary,
            not_started_release_legs=args.not_started_release_legs,
            observations_sha256=_sha256(args.observations),
        ).encode("utf-8"),
    )
    if scores:
        evaluation_manifest = {
            "schema_version": 1,
            "record_kind": "text2sql_public_benchmark_evaluation_manifest",
            "evaluator_input_sha256": _sha256(args.evaluator_input),
            "evaluator_input_path": _relative_artifact_path(
                args.evaluator_input, args.output_dir, label="evaluator input"
            ),
            "score_sha256": _sha256(args.scores),
            "score_path": _relative_artifact_path(
                args.scores, args.output_dir, label="official scores"
            ),
            "evaluator_receipt_sha256": _sha256(args.evaluator_receipt),
            "run_manifest_sha256": _sha256(args.run_manifest),
            "case_manifest_sha256": _sha256(args.case_manifest),
            "diagnostics_sha256": _sha256(args.output_dir / "diagnostics.jsonl"),
            "summary_sha256": _sha256(args.output_dir / "summary.json"),
            "case_keys": list(evaluator_receipt["case_keys"]),
        }
        _publish_bytes_sealed(
            args.output_dir / "evaluation_manifest.json",
            (
                json.dumps(
                    evaluation_manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
