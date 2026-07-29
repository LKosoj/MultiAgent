from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from scripts.summarize_text2sql_public_benchmark import (
    _latency_summary,
    _load_latest_checkpoints,
    classify_case,
    summarize,
)


def _observation(*, status: str, reason: str = "") -> dict:
    return {
        "ordinal": 0,
        "case_id": "case-1",
        "database_id": "db",
        "difficulty": "simple",
        "run_id": "run-1",
        "elapsed_seconds": 10.0,
        "observation_status": "completed",
        "runner_error": None,
        "outcome": {
            "status": status,
            "reason_code": reason,
            "generated": status == "succeeded",
            "approved": status == "succeeded",
            "executed": status == "succeeded",
            "sql": "SELECT 1" if status == "succeeded" else "",
        },
    }


def test_classify_distinguishes_schema_abstention_from_wrong_result() -> None:
    assert (
        classify_case(
            _observation(
                status="abstained",
                reason="SCHEMA_GROUNDING_FAILED",
            ),
            {"score": 0, "error_info": "Result Error"},
        )
        == "schema_linking_abstention"
    )
    assert (
        classify_case(
            _observation(status="succeeded"),
            {"score": 0, "error_info": "Result Error"},
        )
        == "wrong_result"
    )


def test_latency_summary_uses_interpolated_percentiles() -> None:
    summary = _latency_summary([1.0, 2.0, 3.0, 4.0])

    assert summary == {
        "mean": 2.5,
        "median": 2.5,
        "p90": 3.7,
        "p95": 3.85,
        "max": 4.0,
    }


def test_summarize_counts_official_scores_and_stage_reasons() -> None:
    observation = _observation(status="succeeded")
    checkpoints = {
        "run-1": {
            "checkpoint_status": "completed",
            "step_results": {
                "schema_linking_step": {
                    "status": "completed",
                    "duration_seconds": 2.0,
                    "output": {
                        "decision": "PROCEED",
                        "decision_reasons": ["ALL_ENTITIES_LINKED"],
                    },
                }
            },
        }
    }

    diagnostics, summary = summarize(
        [observation],
        {"case-1": {"score": 1, "error_info": None}},
        checkpoints,
    )

    assert diagnostics[0]["failure_class"] == "correct"
    assert summary["execution_accuracy"] == 1.0
    assert summary["execution_coverage"] == 1.0
    assert summary["conditional_execution_accuracy"] == 1.0
    assert summary["schema_decision_reasons"] == {"ALL_ENTITIES_LINKED": 1}
    assert summary["latency_seconds_by_failure_class"]["correct"]["mean"] == 10.0


def test_summarize_uses_case_key_when_source_ids_repeat() -> None:
    first = _observation(status="succeeded")
    first.update(
        case_key="bird:0",
        case_id="duplicate",
        ordinal=0,
        run_id="run-1",
    )
    second = _observation(status="succeeded")
    second.update(
        case_key="bird:1",
        case_id="duplicate",
        ordinal=1,
        run_id="run-2",
    )

    diagnostics, summary = summarize(
        [first, second],
        {
            "bird:0": {"score": 1, "error_info": None},
            "bird:1": {"score": 0, "error_info": "Result Error"},
        },
        {},
    )

    assert [row["case_key"] for row in diagnostics] == ["bird:0", "bird:1"]
    assert summary["correct"] == 1
    assert summary["execution_accuracy"] == 0.5


def test_summarize_reports_accuracy_conditioned_on_execution() -> None:
    correct = _observation(status="succeeded")
    correct.update(case_key="bird:0", ordinal=0, run_id="run-1")
    wrong = _observation(status="succeeded")
    wrong.update(case_key="bird:1", ordinal=1, run_id="run-2")
    abstained = _observation(
        status="abstained",
        reason="SCHEMA_GROUNDING_FAILED",
    )
    abstained.update(case_key="bird:2", ordinal=2, run_id="run-3")

    _, summary = summarize(
        [correct, wrong, abstained],
        {
            "bird:0": {"score": 1, "error_info": None},
            "bird:1": {"score": 0, "error_info": "Result Error"},
            "bird:2": {"score": 0, "error_info": "Result Error"},
        },
        {},
    )

    assert summary["execution_accuracy"] == 0.333333
    assert summary["execution_coverage"] == 0.666667
    assert summary["conditional_execution_accuracy"] == 0.5
    assert summary["latency_seconds_by_terminal_status"]["succeeded"]["mean"] == 10.0


def test_load_latest_checkpoints_merges_runtime_databases(
    tmp_path: Path,
) -> None:
    paths = [tmp_path / "first.db", tmp_path / "second.db"]
    for index, path in enumerate(paths, start=1):
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                CREATE TABLE workflow_checkpoints (
                    timestamp TEXT,
                    status TEXT,
                    current_step TEXT,
                    context TEXT,
                    step_results TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO workflow_checkpoints
                    (timestamp, status, current_step, context, step_results)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    f"2026-07-29T00:00:0{index}",
                    "completed",
                    "db_audit",
                    json.dumps({"variables": {"run_id": f"run-{index}"}}),
                    "{}",
                ),
            )

    checkpoints = _load_latest_checkpoints(paths)

    assert set(checkpoints) == {"run-1", "run-2"}
