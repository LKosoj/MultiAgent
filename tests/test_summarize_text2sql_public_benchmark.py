from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import pytest

from custom_tools.text_to_sql.eval import public_benchmark_release as release
from scripts import summarize_text2sql_public_benchmark as summarizer_module
from scripts.summarize_text2sql_public_benchmark import (
    _failure_report,
    _latency_summary,
    _load_latest_checkpoints,
    classify_case,
    summarize,
    _validate_evaluator_receipt,
    main,
)


def test_direct_cli_help_runs_from_repo_root() -> None:
    result = subprocess.run(
        ["python3", "scripts/summarize_text2sql_public_benchmark.py", "--help"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def _receipt(case_keys: list[str], *, score_sha256: str = "sha256:score") -> dict:
    return {
        "schema_version": 1,
        "record_kind": "text2sql_official_evaluator_receipt",
        "evaluator_identity": {
            "origin": "https://example.test/evaluator",
            "revision": "revision",
            "entrypoint": "evaluate.py",
            "sha256": "a" * 64,
        },
        "evaluator_input_sha256": "sha256:input",
        "score_sha256": score_sha256,
        "case_manifest_sha256": "sha256:cases",
        "run_manifest_sha256": "sha256:run",
        "case_keys": case_keys,
    }


def test_evaluator_receipt_binds_score_digest_and_case_set(tmp_path: Path) -> None:
    scores = tmp_path / "scores.json"
    scores.write_text('[{"case_key":"bird:0","score":1}]', encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(_receipt(
        ["bird:0"],
        score_sha256="sha256:" + hashlib.sha256(scores.read_bytes()).hexdigest(),
    )), encoding="utf-8")
    _validate_evaluator_receipt(receipt, scores, {"bird:0": {"score": 1}})
    with pytest.raises(ValueError, match="case keys"):
        _validate_evaluator_receipt(receipt, scores, {"bird:1": {"score": 1}})


def _single_case_cli_args(
    tmp_path: Path,
) -> tuple[Path, dict[str, object], list[str]]:
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    observations = output_dir / "observations.jsonl"
    observations.write_text(
        json.dumps({"case_key": "bird:0", **_observation(status="succeeded")}) + "\n",
        encoding="utf-8",
    )
    evaluator_input = output_dir / "official_evaluator_input.json"
    evaluator_input.write_text('{"0":"SELECT 1"}\n', encoding="utf-8")
    scores = output_dir / "official_scores.json"
    scores.write_text('[{"case_key":"bird:0","score":1}]\n', encoding="utf-8")
    identity = _receipt(["bird:0"])["evaluator_identity"]
    run_manifest = output_dir / "manifest.json"
    run_manifest.write_text(json.dumps({"evaluator_identity": identity}) + "\n", encoding="utf-8")
    case_manifest = output_dir / "case_manifest.json"
    case_manifest.write_text(json.dumps({"cases": [{"case_key": "bird:0"}]}) + "\n", encoding="utf-8")
    receipt = _receipt(
        ["bird:0"],
        score_sha256="sha256:" + hashlib.sha256(scores.read_bytes()).hexdigest(),
    )
    receipt.update(
        evaluator_input_sha256="sha256:" + hashlib.sha256(evaluator_input.read_bytes()).hexdigest(),
        run_manifest_sha256="sha256:" + hashlib.sha256(run_manifest.read_bytes()).hexdigest(),
        case_manifest_sha256="sha256:" + hashlib.sha256(case_manifest.read_bytes()).hexdigest(),
    )
    evaluator_receipt = output_dir / "evaluator_receipt.json"
    evaluator_receipt.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

    argv = [
        "--observations", str(observations), "--scores", str(scores),
        "--evaluator-receipt", str(evaluator_receipt),
        "--evaluator-input", str(evaluator_input),
        "--run-manifest", str(run_manifest),
        "--case-manifest", str(case_manifest),
        "--output-dir", str(output_dir),
        "--created-at", "2026-08-03T12:00:00+00:00",
    ]
    return output_dir, identity, argv


def test_cli_writes_closed_evaluation_manifest_bound_to_local_artifacts(
    tmp_path: Path,
) -> None:
    output_dir, identity, argv = _single_case_cli_args(tmp_path)
    assert main(argv) == 0

    evaluation = json.loads((output_dir / "evaluation_manifest.json").read_text())
    assert evaluation["score_path"] == "official_scores.json"
    assert evaluation["evaluator_input_path"] == "official_evaluator_input.json"
    assert evaluation["case_keys"] == ["bird:0"]
    for name in (
        "diagnostics.jsonl",
        "summary.json",
        "failure_report.md",
        "evaluation_manifest.json",
    ):
        path = output_dir / name
        assert path.stat().st_mode & 0o777 == 0o444
        assert not path.is_symlink()
    release.validate_post_repeat_evaluation(
        output_dir, evaluator_identity=identity
    )


def test_cli_recovers_after_crash_during_derived_artifact_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir, _identity, argv = _single_case_cli_args(tmp_path)
    original = summarizer_module._publish_bytes_sealed
    crashed = False

    def crash_once(path: Path, payload: bytes) -> None:
        nonlocal crashed
        if path.name == "failure_report.md" and not crashed:
            crashed = True
            raise RuntimeError("simulated derived publication crash")
        original(path, payload)

    monkeypatch.setattr(summarizer_module, "_publish_bytes_sealed", crash_once)
    with pytest.raises(RuntimeError, match="derived publication crash"):
        summarizer_module.main(argv)
    assert not (output_dir / "evaluation_manifest.json").exists()

    monkeypatch.setattr(summarizer_module, "_publish_bytes_sealed", original)
    assert summarizer_module.main(argv) == 0
    for name in (
        "diagnostics.jsonl", "summary.json", "failure_report.md",
        "evaluation_manifest.json",
    ):
        path = output_dir / name
        assert path.is_file() and not path.is_symlink()
        assert path.stat().st_mode & 0o777 == 0o444


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


def _write_two_case_evaluated_run(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    case_keys = ["bird:0", "bird:1"]
    observations = output_dir / "observations.jsonl"
    observations.write_text(
        "".join(
            json.dumps(
                {
                    "case_key": case_key,
                    **_observation(status="succeeded"),
                    "ordinal": index,
                    "case_id": str(index),
                    "run_id": f"run-{index}",
                }
            )
            + "\n"
            for index, case_key in enumerate(case_keys)
        ),
        encoding="utf-8",
    )
    evaluator_input = output_dir / "bird_predictions.json"
    evaluator_input.write_text('{"0":"SELECT 1","1":"SELECT 1"}\n', encoding="utf-8")
    scores = output_dir / "official_scores.json"
    scores.write_text(
        json.dumps(
            [
                {"case_key": "bird:0", "score": 1},
                {"case_key": "bird:1", "score": 1},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    identity = _receipt(case_keys)["evaluator_identity"]
    run_manifest = output_dir / "manifest.json"
    run_manifest.write_text(
        json.dumps({"evaluator_identity": identity}) + "\n", encoding="utf-8"
    )
    case_manifest = output_dir / "case_manifest.json"
    case_manifest.write_text(
        json.dumps({"cases": [{"case_key": key} for key in case_keys]}) + "\n",
        encoding="utf-8",
    )
    receipt = _receipt(
        case_keys,
        score_sha256="sha256:" + hashlib.sha256(scores.read_bytes()).hexdigest(),
    )
    receipt.update(
        evaluator_input_sha256="sha256:"
        + hashlib.sha256(evaluator_input.read_bytes()).hexdigest(),
        run_manifest_sha256="sha256:"
        + hashlib.sha256(run_manifest.read_bytes()).hexdigest(),
        case_manifest_sha256="sha256:"
        + hashlib.sha256(case_manifest.read_bytes()).hexdigest(),
    )
    evaluator_receipt = output_dir / "evaluator_receipt.json"
    evaluator_receipt.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    assert main(
        [
            "--observations", str(observations),
            "--scores", str(scores),
            "--evaluator-receipt", str(evaluator_receipt),
            "--evaluator-input", str(evaluator_input),
            "--run-manifest", str(run_manifest),
            "--case-manifest", str(case_manifest),
            "--output-dir", str(output_dir),
        ]
    ) == 0
    return output_dir, identity


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_diagnostic",
        "reordered_diagnostics",
        "wrong_diagnostic_key",
        "summary_cases",
        "summary_evaluated",
        "summary_correct",
        "summary_generated",
        "summary_executed",
        "summary_terminal_statuses",
        "summary_reason_codes",
        "summary_failure_classes",
        "summary_execution_accuracy",
        "summary_execution_coverage",
        "summary_conditional_execution_accuracy",
        "summary_research_stop_reasons",
        "summary_research_terminal_reasons",
        "summary_latency_seconds",
        "summary_latency_seconds_by_failure_class",
        "summary_latency_seconds_by_terminal_status",
        "summary_step_latency_seconds",
        "summary_by_database",
        "summary_by_difficulty",
        "summary_missing_field",
        "summary_unexpected_field",
    ),
)
def test_post_repeat_evaluation_requires_aligned_case_evidence_and_counters(
    tmp_path: Path,
    mutation: str,
) -> None:
    output_dir, identity = _write_two_case_evaluated_run(tmp_path)
    diagnostics_path = output_dir / "diagnostics.jsonl"
    summary_path = output_dir / "summary.json"
    diagnostics = [
        json.loads(line)
        for line in diagnostics_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if mutation == "missing_diagnostic":
        diagnostics.pop()
    elif mutation == "reordered_diagnostics":
        diagnostics.reverse()
    elif mutation == "wrong_diagnostic_key":
        diagnostics[1]["case_key"] = "bird:other"
    elif mutation == "summary_missing_field":
        summary.pop("execution_accuracy")
    elif mutation == "summary_unexpected_field":
        summary["unexpected"] = "tampered"
    else:
        field = mutation.removeprefix("summary_")
        summary[field] = {
            "terminal_statuses": {"tampered": 2},
            "reason_codes": {"tampered": 2},
            "failure_classes": {"bad_sql": 2},
            "research_stop_reasons": {"tampered": 2},
            "research_terminal_reasons": {"tampered": 2},
            "latency_seconds": {
                "mean": 0.0,
                "median": 0.0,
                "p90": 0.0,
                "p95": 0.0,
                "max": 0.0,
            },
            "latency_seconds_by_failure_class": {},
            "latency_seconds_by_terminal_status": {},
            "step_latency_seconds": {"tampered": {}},
            "by_database": {},
            "by_difficulty": {},
        }.get(field, 0.0)
    diagnostics_path.write_text(
        "".join(json.dumps(row) + "\n" for row in diagnostics),
        encoding="utf-8",
    )
    summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")
    evaluation_path = output_dir / "evaluation_manifest.json"
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation["diagnostics_sha256"] = (
        "sha256:" + hashlib.sha256(diagnostics_path.read_bytes()).hexdigest()
    )
    evaluation["summary_sha256"] = (
        "sha256:" + hashlib.sha256(summary_path.read_bytes()).hexdigest()
    )
    evaluation_path.write_text(json.dumps(evaluation) + "\n", encoding="utf-8")

    with pytest.raises(release.SandboxError, match="post-repeat"):
        release.validate_post_repeat_evaluation(
            output_dir, evaluator_identity=identity
        )


def test_post_repeat_summary_allows_a_different_valid_created_at(
    tmp_path: Path,
) -> None:
    output_dir, identity = _write_two_case_evaluated_run(tmp_path)
    summary_path = output_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["created_at"] = "2026-08-03T12:00:00+00:00"
    summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")
    evaluation_path = output_dir / "evaluation_manifest.json"
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation["summary_sha256"] = (
        "sha256:" + hashlib.sha256(summary_path.read_bytes()).hexdigest()
    )
    evaluation_path.write_text(json.dumps(evaluation) + "\n", encoding="utf-8")

    release.validate_post_repeat_evaluation(
        output_dir, evaluator_identity=identity
    )


def test_post_repeat_evaluation_rejects_malformed_nested_diagnostic_shape(
    tmp_path: Path,
) -> None:
    output_dir, identity = _write_two_case_evaluated_run(tmp_path)
    diagnostics_path = output_dir / "diagnostics.jsonl"
    diagnostics = [
        json.loads(line)
        for line in diagnostics_path.read_text(encoding="utf-8").splitlines()
    ]
    diagnostics[0]["schema_research"]["ready_for_sql"] = "yes"
    diagnostics_path.write_text(
        "".join(json.dumps(row) + "\n" for row in diagnostics),
        encoding="utf-8",
    )
    summary_path = output_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["research_stop_reasons"] = {"COMPLETE": 1}
    summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")
    evaluation_path = output_dir / "evaluation_manifest.json"
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    evaluation["diagnostics_sha256"] = (
        "sha256:" + hashlib.sha256(diagnostics_path.read_bytes()).hexdigest()
    )
    evaluation["summary_sha256"] = (
        "sha256:" + hashlib.sha256(summary_path.read_bytes()).hexdigest()
    )
    evaluation_path.write_text(json.dumps(evaluation) + "\n", encoding="utf-8")

    with pytest.raises(release.SandboxError):
        release.validate_post_repeat_evaluation(
            output_dir, evaluator_identity=identity
        )


def test_classify_distinguishes_schema_abstention_from_wrong_result() -> None:
    receipt = _receipt(["case-1"])
    assert (
        classify_case(
            _observation(
                status="abstained",
                reason="SCHEMA_GROUNDING_FAILED",
            ),
            {"score": 0, "error_info": "Result Error"}, receipt,
        )
        == "typed_abstention"
    )
    assert (
        classify_case(
            _observation(status="succeeded"),
            {"score": 0, "error_info": "Result Error"}, receipt,
        )
        == "wrong_result"
    )


def test_classify_uses_only_the_closed_w705_taxonomy() -> None:
    receipt = _receipt(["case-1"])
    runner_error = _observation(status="succeeded")
    runner_error["observation_status"] = "runner_error"
    typed_abstention = _observation(status="abstained", reason="SCHEMA_UNRESOLVED")
    terminal_failure = _observation(status="failed", reason="EXECUTION_FAILED")
    succeeded = _observation(status="succeeded")

    classes = {
        classify_case(runner_error, None, None),
        classify_case(typed_abstention, None, None),
        classify_case(terminal_failure, None, None),
        classify_case(succeeded, None, None),
        classify_case(succeeded, {"score": 1}, receipt),
        classify_case(succeeded, {"score": 0, "error_info": "Result Error"}, receipt),
        classify_case(succeeded, {"score": 0, "error_info": "SQL error"}, receipt),
        classify_case(succeeded, {"score": True}, receipt),
    }

    assert classes == {
        "runner_or_transport_error",
        "typed_abstention",
        "pipeline_terminal_failure",
        "evidence_incomplete",
        "correct",
        "wrong_result",
        "bad_sql",
        "evaluator_failure",
    }


def test_failure_report_is_human_readable_and_does_not_claim_a_root_cause() -> None:
    report = _failure_report(
        {
            "cases": 20,
            "evaluated": 0,
            "correct": 0,
            "failure_classes": {"pipeline_terminal_failure": 20},
        }
    )

    assert "## Verified conclusion" in report
    assert "## General repair plan" in report
    assert "diagnostics.jsonl" in report
    assert "root cause" in report
    assert "SELECT" not in report


def test_scored_failure_report_records_completed_and_remaining_release_work(
    tmp_path: Path,
) -> None:
    output_dir, _identity, argv = _single_case_cli_args(tmp_path)
    observations_sha256 = "sha256:" + hashlib.sha256(
        (output_dir / "observations.jsonl").read_bytes()
    ).hexdigest()
    argv.extend(["--not-started-release-legs", "2"])

    assert main(argv) == 0

    report = (output_dir / "failure_report.md").read_text(encoding="utf-8")
    assert "Completed cases: 1" in report
    assert "Not-started release legs: 2" in report
    assert f"observations.jsonl ({observations_sha256})" in report


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
                "schema_research": {
                    "status": "completed",
                    "duration_seconds": 2.0,
                    "output": {
                        "stop_reason": "COMPLETE",
                        "ready_for_sql": True,
                        "terminal_reason_code": None,
                        "schema_namespace_version": "sha256:" + "a" * 64,
                    },
                }
            },
        }
    }

    diagnostics, summary = summarize(
        [observation],
        {"case-1": {"score": 1, "error_info": None}},
        checkpoints,
        _receipt(["case-1"]),
    )

    assert diagnostics[0]["failure_class"] == "correct"
    assert summary["execution_accuracy"] == 1.0
    assert summary["execution_coverage"] == 1.0
    assert summary["conditional_execution_accuracy"] == 1.0
    assert summary["research_stop_reasons"] == {"COMPLETE": 1}
    assert summary["research_terminal_reasons"] == {"none": 1}
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
        _receipt(["bird:0", "bird:1"]),
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
        _receipt(["bird:0", "bird:1", "bird:2"]),
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
