import json
import os
from pathlib import Path

import pytest

from custom_tools.text_to_sql.eval import (
    load_gold_cases,
    pre_execution_gate_coverage_record,
    run_sqlite_eval,
    summarize_results,
    write_eval_observability_jsonl,
    write_pre_execution_gate_coverage_jsonl,
)
from custom_tools.text_to_sql.eval import observability as observability_module
from custom_tools.text_to_sql.eval.history_import import seed_candidates_from_history
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    CheckRepair,
    CheckResult,
    CheckStatus,
    RepairKind,
)
from custom_tools.text_to_sql.adaptive.pre_execution_gate import (
    PreExecutionGateReceipt,
)
from tests.eval.fixtures.sqlite_text2sql import create_sqlite_text2sql_fixture


GOLD = Path("tests/eval/gold/sqlite_smoke.jsonl")


def _gate_coverage_record(
    case_id: str,
    fixture_id: str,
    fixture_category: str,
) -> dict[str, object]:
    candidate_id = "candidate-eval-observability"
    check = CheckResult(
        check_id="semantic:candidate-eval-observability:ast_shape_unsupported",
        candidate_id=candidate_id,
        check_kind=CheckKind.SEMANTIC,
        status=CheckStatus.FAILED,
        failure_code=CheckFailureCode.AST_SHAPE_UNSUPPORTED,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=None,
        repair=CheckRepair(kind=RepairKind.REVISE_SQL),
    )
    receipt = PreExecutionGateReceipt(
        run_id="eval-observability-run",
        run_incarnation="eval-observability-incarnation",
        state_revision=0,
        candidate_id=candidate_id,
        sql_source_digest="sha256:" + "a" * 64,
        normalized_ast_digest=None,
        requirements_digest="sha256:" + "b" * 64,
        semantic_coverage=None,
        source_coverage_available=False,
        check_results=(check,),
        primary_check_id=check.check_id,
        allowed=False,
    )
    return pre_execution_gate_coverage_record(
        receipt,
        case_id=case_id,
        fixture_id=fixture_id,
        fixture_category=fixture_category,
    )


def test_sqlite_eval_harness_computes_execution_accuracy(tmp_path):
    db_path = create_sqlite_text2sql_fixture(tmp_path / "eval.sqlite")
    cases = load_gold_cases(GOLD)

    results = [
        run_sqlite_eval(
            case,
            db_path=db_path,
            generate_sql=lambda request: {
                "sqlite-region-sales": (
                    "SELECT c.region, SUM(o.amount) AS total_amount "
                    "FROM orders o JOIN customers c ON c.id = o.customer_id "
                    "WHERE o.amount >= 50 "
                    "GROUP BY c.region ORDER BY c.region"
                )
            }[request.case_id],
            schema_linking_provider=lambda _request: {
                "linked_entities": {
                    "metrics": [{"table": "orders", "column": "amount"}],
                    "dimensions": [{"table": "customers", "column": "region"}],
                    "filters": {},
                    "columns": ["orders.customer_id", "customers.id"],
                }
            },
        )
        for case in cases
    ]
    summary = summarize_results(results)

    assert summary.total == len(cases)
    assert summary.passed == len(cases)
    assert summary.failed == 0
    assert summary.execution_accuracy == 1.0
    assert summary.avg_duration_ms >= 0.0
    assert summary.schema_linking_cases == 1
    assert summary.avg_table_recall == 1.0
    assert summary.avg_column_recall == 1.0
    assert results[0].duration_ms >= 0.0


def test_schema_linking_metrics_expose_precision_and_recall(tmp_path):
    db_path = create_sqlite_text2sql_fixture(tmp_path / "eval.sqlite")
    case = load_gold_cases(GOLD)[0]

    result = run_sqlite_eval(
        case,
        db_path=db_path,
        generate_sql=lambda _request: (
            "SELECT c.region, SUM(o.amount) AS total_amount "
            "FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE o.amount >= 50 "
            "GROUP BY c.region ORDER BY c.region"
        ),
        schema_linking_provider=lambda _request: {
            "linked_entities": {
                "metrics": [{"table": "orders", "column": "amount"}],
                "dimensions": [],
                "filters": {"region": {"table": "wrong_table", "column": "region"}},
                "columns": ["orders.customer_id"],
            }
        },
    )

    assert result.schema_linking_metrics is not None
    assert result.schema_linking_metrics.expected_tables == 2
    assert result.schema_linking_metrics.matched_tables == 1
    assert result.schema_linking_metrics.table_recall == 0.5
    assert result.schema_linking_metrics.expected_columns == 4
    assert result.schema_linking_metrics.matched_columns == 2
    assert result.schema_linking_metrics.column_recall == 0.5


def test_sqlite_eval_harness_returns_failed_result_for_generation_error(tmp_path):
    db_path = create_sqlite_text2sql_fixture(tmp_path / "eval.sqlite")
    case = load_gold_cases(GOLD)[0]

    def broken_generator(_case):
        raise RuntimeError("generation failed")

    result = run_sqlite_eval(case, db_path=db_path, generate_sql=broken_generator)

    assert result.passed is False
    assert result.generated_sql == ""
    assert result.error == "generation failed"


def test_gold_loader_rejects_unreviewed_cases(tmp_path):
    path = tmp_path / "candidate.jsonl"
    path.write_text(
        '{"schema_version":1,"id":"candidate","question":"q",'
        '"dialect":"sqlite","fixture":"sqlite_text2sql_v2",'
        '"expected_outcome":"succeeded","comparison_mode":"exact",'
        '"expected_rows":[{"value":1}],"slice_tags":["filter"],'
        '"request_options":{},"review":{"status":"pending",'
        '"reviewed_by":null,"reviewed_at":null,"reference":null}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reviewed"):
        load_gold_cases(path)


def test_history_import_writes_unreviewed_candidates(tmp_path):
    history = tmp_path / "sql_history.jsonl"
    output = tmp_path / "candidates.jsonl"
    history.write_text(
        '{"user_query":"show totals","sql_query":"SELECT 1"}\n'
        '{"message":"missing question"}\n',
        encoding="utf-8",
    )

    count = seed_candidates_from_history(history, output)

    assert count == 1
    text = output.read_text(encoding="utf-8")
    assert '"reviewed": false' in text
    assert '"candidate_sql": "SELECT 1"' in text


def test_eval_observability_jsonl_exports_duration_and_linking_metrics(tmp_path):
    db_path = create_sqlite_text2sql_fixture(tmp_path / "eval.sqlite")
    case = load_gold_cases(GOLD)[0]
    result = run_sqlite_eval(
        case,
        db_path=db_path,
        generate_sql=lambda _request: (
            "SELECT c.region, SUM(o.amount) AS total_amount "
            "FROM orders o JOIN customers c ON c.id = o.customer_id "
            "GROUP BY c.region ORDER BY c.region"
        ),
        schema_linking_provider=lambda _request: case.expected_schema_links,
    )
    output = tmp_path / "observability" / "eval.jsonl"

    count = write_eval_observability_jsonl(output, [result])

    assert count == 1
    text = output.read_text(encoding="utf-8")
    assert '"case_id": "sqlite-region-sales"' in text
    assert '"duration_ms":' in text
    assert '"schema_linking_metrics":' in text


def test_gate_coverage_jsonl_is_sorted_and_atomically_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "observability" / "gate-coverage.jsonl"
    output.parent.mkdir(parents=True)
    output.write_text("previous\n", encoding="utf-8")
    records = [
        _gate_coverage_record(case_id, fixture_id, category)
        for fixture_id, category, case_id in (
            ("F02_VERTICAL_EAV", "vertical_eav", "eav-valid"),
            ("F01_CONVENTIONAL_STAR", "conventional_star", "conventional-valid"),
        )
    ]
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        assert output.read_text(encoding="utf-8") == "previous\n"
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(observability_module.os, "replace", replace)

    count = write_pre_execution_gate_coverage_jsonl(output, records)

    assert count == 2
    assert replacements and replacements[0][1] == output
    rows = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["case_id"] for row in rows] == [
        "conventional-valid",
        "eav-valid",
    ]
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


def test_gate_coverage_jsonl_preserves_existing_file_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "gate-coverage.jsonl"
    output.write_text("previous\n", encoding="utf-8")
    record = _gate_coverage_record(
        "conventional-valid",
        "F01_CONVENTIONAL_STAR",
        "conventional_star",
    )
    monkeypatch.setattr(
        observability_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        write_pre_execution_gate_coverage_jsonl(output, [record])

    assert output.read_text(encoding="utf-8") == "previous\n"
    assert list(tmp_path.glob(f".{output.name}.*.tmp")) == []


def test_gate_coverage_jsonl_rejects_unbounded_record_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "gate-coverage.jsonl"
    record = _gate_coverage_record(
        "conventional-valid",
        "F01_CONVENTIONAL_STAR",
        "conventional_star",
    )
    monkeypatch.setattr(
        observability_module,
        "_MAX_PRE_EXECUTION_GATE_COVERAGE_RECORDS",
        1,
    )

    with pytest.raises(ValueError, match="too many"):
        write_pre_execution_gate_coverage_jsonl(output, [record, record])

    assert not output.exists()


def test_gate_coverage_jsonl_rejects_an_arbitrary_raw_receipt(tmp_path: Path) -> None:
    output = tmp_path / "gate-coverage.jsonl"
    record = _gate_coverage_record(
        "conventional-valid",
        "F01_CONVENTIONAL_STAR",
        "conventional_star",
    )
    record["receipt"] = {}

    with pytest.raises(ValueError, match="receipt"):
        write_pre_execution_gate_coverage_jsonl(output, [record])

    assert not output.exists()


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_gate_coverage_jsonl_rejects_non_finite_nested_values(
    tmp_path: Path,
    value: float,
) -> None:
    output = tmp_path / "gate-coverage.jsonl"
    record = _gate_coverage_record(
        "conventional-valid",
        "F01_CONVENTIONAL_STAR",
        "conventional_star",
    )
    record["source_coverage"] = {"forged": value}

    with pytest.raises(ValueError, match="canonical JSON"):
        write_pre_execution_gate_coverage_jsonl(output, [record])

    assert not output.exists()


def test_gate_coverage_jsonl_requires_globally_unique_case_ids(tmp_path: Path) -> None:
    output = tmp_path / "gate-coverage.jsonl"
    records = [
        _gate_coverage_record(
            "shared-case",
            "F01_CONVENTIONAL_STAR",
            "conventional_star",
        ),
        _gate_coverage_record(
            "shared-case",
            "F02_VERTICAL_EAV",
            "vertical_eav",
        ),
    ]

    with pytest.raises(ValueError, match="duplicate case_id"):
        write_pre_execution_gate_coverage_jsonl(output, records)

    assert not output.exists()


def test_adaptive_replay_observability_keeps_history_and_reuse_separate(
    tmp_path: Path,
) -> None:
    from custom_tools.text_to_sql.eval import (
        AdaptiveReplayObservabilityRecord,
        HistoricalReplayObservationCategory,
        HistoricalReplayReasonCode,
        ReplayReuseReasonCode,
        write_adaptive_replay_observability_jsonl,
    )

    record = AdaptiveReplayObservabilityRecord(
        case_id="replay-case",
        run_id="run-replay",
        run_incarnation="inc-replay",
        trusted_artifact_digest="sha256:" + "a" * 64,
        historical_status="VERIFIED",
        historical_category=HistoricalReplayObservationCategory.VERIFIED,
        historical_reason_code=HistoricalReplayReasonCode.VERIFIED,
        verified_research_transition_count=2,
        verified_solver_transition_count=3,
        reuse_status="REVALIDATION_REQUIRED",
        reuse_reason_code=ReplayReuseReasonCode.CURRENT_EVIDENCE_REVALIDATION_REQUIRED,
    )
    output = tmp_path / "replay-observability.jsonl"

    assert write_adaptive_replay_observability_jsonl(output, (record,)) == 1
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row == record.model_dump(mode="json")
    assert "historical_error" not in row
    assert "reuse_error" not in row
    with pytest.raises(ValueError, match="exact SHA-256"):
        AdaptiveReplayObservabilityRecord.model_validate(
            {
                **record.model_dump(mode="python"),
                "trusted_artifact_digest": "sha256:abc",
            }
        )


def test_adaptive_replay_observability_rejects_forged_record(tmp_path: Path) -> None:
    from custom_tools.text_to_sql.eval import (
        AdaptiveReplayObservabilityRecord,
        HistoricalReplayObservationCategory,
        HistoricalReplayReasonCode,
        ReplayReuseReasonCode,
        write_adaptive_replay_observability_jsonl,
    )

    record = AdaptiveReplayObservabilityRecord(
        case_id="replay-case",
        run_id="run-replay",
        run_incarnation="inc-replay",
        trusted_artifact_digest="sha256:" + "b" * 64,
        historical_status="VERIFIED",
        historical_category=HistoricalReplayObservationCategory.VERIFIED,
        historical_reason_code=HistoricalReplayReasonCode.VERIFIED,
        verified_research_transition_count=1,
        verified_solver_transition_count=1,
        reuse_status="REUSABLE",
        reuse_reason_code=ReplayReuseReasonCode.CURRENT_EVIDENCE_REUSABLE,
    )
    forged = record.model_copy(update={"historical_status": "ERROR"})
    output = tmp_path / "forged-replay-observability.jsonl"

    with pytest.raises(ValueError, match="historical observation is inconsistent"):
        write_adaptive_replay_observability_jsonl(output, (forged,))
    assert not output.exists()


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        (
            {
                "historical_status": "UNVERIFIABLE",
                "historical_category": "LEGACY",
                "historical_reason_code": "NO_TYPED_PROVENANCE",
                "verified_research_transition_count": 1,
                "reuse_status": "REVALIDATION_REQUIRED",
                "reuse_reason_code": "HISTORICAL_REPLAY_NOT_VERIFIED",
            },
            "cannot report verified transitions",
        ),
        (
            {
                "historical_status": "ERROR",
                "historical_category": "ARTIFACT_TAMPER",
                "historical_reason_code": "CONTRACT_CORRUPTION",
                "verified_research_transition_count": 0,
                "verified_solver_transition_count": 0,
                "reuse_status": "NOT_EVALUATED",
                "reuse_reason_code": "NOT_EVALUATED",
            },
            "historical observation is inconsistent",
        ),
        (
            {
                "reuse_status": "REVALIDATION_REQUIRED",
                "reuse_reason_code": "HISTORICAL_REPLAY_NOT_VERIFIED",
            },
            "reuse observation is inconsistent",
        ),
    ),
)
def test_adaptive_replay_observability_rejects_inconsistent_codes_and_counts(
    updates: dict[str, object],
    message: str,
) -> None:
    from custom_tools.text_to_sql.eval import (
        AdaptiveReplayObservabilityRecord,
        HistoricalReplayObservationCategory,
        HistoricalReplayReasonCode,
        ReplayReuseReasonCode,
    )

    values = {
        "case_id": "replay-case",
        "run_id": "run-replay",
        "run_incarnation": "inc-replay",
        "trusted_artifact_digest": "sha256:" + "e" * 64,
        "historical_status": "VERIFIED",
        "historical_category": HistoricalReplayObservationCategory.VERIFIED,
        "historical_reason_code": HistoricalReplayReasonCode.VERIFIED,
        "verified_research_transition_count": 1,
        "verified_solver_transition_count": 1,
        "reuse_status": "REUSABLE",
        "reuse_reason_code": ReplayReuseReasonCode.CURRENT_EVIDENCE_REUSABLE,
    }
    enum_types = {
        "historical_category": HistoricalReplayObservationCategory,
        "historical_reason_code": HistoricalReplayReasonCode,
        "reuse_reason_code": ReplayReuseReasonCode,
    }
    typed_updates = {
        name: enum_types[name](value) if name in enum_types else value
        for name, value in updates.items()
    }

    with pytest.raises(ValueError, match=message):
        AdaptiveReplayObservabilityRecord.model_validate({**values, **typed_updates})

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        AdaptiveReplayObservabilityRecord.model_validate(
            {**values, "historical_error": "free-form text"}
        )


def test_adaptive_replay_observability_is_sorted_and_rejects_duplicates(
    tmp_path: Path,
) -> None:
    from custom_tools.text_to_sql.eval import (
        AdaptiveReplayObservabilityRecord,
        HistoricalReplayObservationCategory,
        HistoricalReplayReasonCode,
        ReplayReuseReasonCode,
        write_adaptive_replay_observability_jsonl,
    )

    later = AdaptiveReplayObservabilityRecord(
        case_id="case-z",
        run_id="run-z",
        run_incarnation="inc-z",
        trusted_artifact_digest="sha256:" + "c" * 64,
        historical_status="UNVERIFIABLE",
        historical_category=HistoricalReplayObservationCategory.LEGACY,
        historical_reason_code=HistoricalReplayReasonCode.NO_TYPED_PROVENANCE,
        verified_research_transition_count=0,
        verified_solver_transition_count=0,
        reuse_status="REVALIDATION_REQUIRED",
        reuse_reason_code=ReplayReuseReasonCode.HISTORICAL_REPLAY_NOT_VERIFIED,
    )
    earlier = AdaptiveReplayObservabilityRecord(
        case_id="case-a",
        run_id="run-a",
        run_incarnation="inc-a",
        trusted_artifact_digest="sha256:" + "d" * 64,
        historical_status="VERIFIED",
        historical_category=HistoricalReplayObservationCategory.VERIFIED,
        historical_reason_code=HistoricalReplayReasonCode.VERIFIED,
        verified_research_transition_count=4,
        verified_solver_transition_count=5,
        reuse_status="REUSABLE",
        reuse_reason_code=ReplayReuseReasonCode.CURRENT_EVIDENCE_REUSABLE,
    )
    output = tmp_path / "ordered-replay-observability.jsonl"

    assert write_adaptive_replay_observability_jsonl(output, (later, earlier)) == 2
    rows = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["case_id"] for row in rows] == ["case-a", "case-z"]
    with pytest.raises(ValueError, match="duplicate"):
        write_adaptive_replay_observability_jsonl(output, (earlier, earlier))


def test_eval_replay_observability_lazy_export_does_not_import_replay_runtime() -> None:
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import custom_tools.text_to_sql.eval as package; "
                "package.AdaptiveReplayObservabilityRecord; "
                "assert 'custom_tools.text_to_sql.adaptive.replay' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
