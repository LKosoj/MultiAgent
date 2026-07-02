from pathlib import Path

import pytest

from custom_tools.text_to_sql.eval import (
    load_gold_cases,
    run_sqlite_eval,
    summarize_results,
    write_eval_observability_jsonl,
)
from custom_tools.text_to_sql.eval.history_import import seed_candidates_from_history
from tests.eval.fixtures.sqlite_text2sql import create_sqlite_text2sql_fixture


GOLD = Path("tests/eval/gold/sqlite_smoke.jsonl")


def test_sqlite_eval_harness_computes_execution_accuracy(tmp_path):
    db_path = create_sqlite_text2sql_fixture(tmp_path / "eval.sqlite")
    cases = load_gold_cases(GOLD)

    results = [
        run_sqlite_eval(
            case,
            db_path=db_path,
            generate_sql=lambda c: c.expected_sql or "",
            schema_linking_provider=lambda c: {
                "linked_entities": {
                    "metrics": [{"table": "orders", "column": "amount"}],
                    "dimensions": [{"table": "customers", "column": "region"}],
                    "filters": {},
                }
            },
        )
        for case in cases
    ]
    summary = summarize_results(results)

    assert summary.total == 1
    assert summary.passed == 1
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
        generate_sql=lambda c: c.expected_sql or "",
        schema_linking_provider=lambda c: {
            "linked_entities": {
                "metrics": [{"table": "orders", "column": "amount"}],
                "dimensions": [],
                "filters": {"region": {"table": "wrong_table", "column": "region"}},
            }
        },
    )

    assert result.schema_linking_metrics is not None
    assert result.schema_linking_metrics.expected_tables == 2
    assert result.schema_linking_metrics.matched_tables == 1
    assert result.schema_linking_metrics.table_recall == 0.5
    assert result.schema_linking_metrics.expected_columns == 2
    assert result.schema_linking_metrics.matched_columns == 1
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
        '{"id":"candidate","question":"q","expected_sql":"SELECT 1","reviewed":false}\n',
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
        generate_sql=lambda c: c.expected_sql or "",
        schema_linking_provider=lambda c: c.expected_schema_links,
    )
    output = tmp_path / "observability" / "eval.jsonl"

    count = write_eval_observability_jsonl(output, [result])

    assert count == 1
    text = output.read_text(encoding="utf-8")
    assert '"case_id": "sqlite-region-sales"' in text
    assert '"duration_ms":' in text
    assert '"schema_linking_metrics":' in text
