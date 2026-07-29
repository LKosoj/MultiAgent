from __future__ import annotations

from scripts.summarize_text2sql_public_benchmark import (
    _latency_summary,
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
    assert summary["schema_decision_reasons"] == {"ALL_ENTITIES_LINKED": 1}
