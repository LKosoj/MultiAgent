from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

import scripts.text2sql_public_benchmark as benchmark_runner
from scripts.text2sql_public_benchmark import (
    EMPTY_PREDICTION,
    _idempotency_key,
    _load_completed,
    _run_case,
    benchmark_prompt,
    export_predictions,
    load_bird_cases,
    load_spider_cases,
)
from streamlit_app.text_to_sql_client import TextToSqlResult


def _sqlite_file(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 2048)


def test_load_bird_cases_does_not_put_gold_sql_in_prompt(tmp_path: Path) -> None:
    root = tmp_path / "bird"
    root.mkdir()
    (root / "mini_dev_sqlite.json").write_text(
        json.dumps(
            [
                {
                    "question_id": 7,
                    "db_id": "school",
                    "question": "Count the students.",
                    "evidence": "Students are enrolled people.",
                    "difficulty": "simple",
                    "SQL": "SELECT COUNT(*) FROM students",
                }
            ]
        ),
        encoding="utf-8",
    )
    _sqlite_file(root / "dev_databases" / "school" / "school.sqlite")

    cases = load_bird_cases(root)

    assert len(cases) == 1
    assert cases[0].case_key == "bird:0"
    assert cases[0].case_id == "7"
    assert "SELECT COUNT(*)" not in cases[0].prompt()
    assert cases[0].prompt() == benchmark_prompt(
        "Count the students.",
        "Students are enrolled people.",
    )


def test_load_spider_cases_filters_non_local_and_loads_documents(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spider2-lite"
    documents = root / "resource" / "documents"
    documents.mkdir(parents=True)
    (documents / "rule.md").write_text("Use the documented rule.", encoding="utf-8")
    (root / "spider2-lite.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "instance_id": "bq001",
                        "question": "Remote question",
                        "external_knowledge": "",
                    }
                ),
                json.dumps(
                    {
                        "instance_id": "local001",
                        "question": "Local question",
                        "external_knowledge": "rule.md",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sqlite_root = tmp_path / "sqlite"
    _sqlite_file(sqlite_root / "db.sqlite")
    database_map = tmp_path / "local-map.jsonl"
    database_map.write_text(json.dumps({"local001": "db"}), encoding="utf-8")

    cases = load_spider_cases(root, sqlite_root, database_map)

    assert [case.case_id for case in cases] == ["local001"]
    assert [case.case_key for case in cases] == ["local001"]
    assert "Use the documented rule." in cases[0].prompt()


def test_export_predictions_is_stable_and_uses_failing_fallback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bird"
    root.mkdir()
    (root / "mini_dev_sqlite.json").write_text(
        json.dumps(
            [
                {
                    "question_id": 0,
                    "db_id": "db",
                    "question": "First",
                    "SQL": "gold must stay unused",
                },
                {
                    "question_id": 1,
                    "db_id": "db",
                    "question": "Second",
                    "SQL": "gold must stay unused",
                },
            ]
        ),
        encoding="utf-8",
    )
    _sqlite_file(root / "dev_databases" / "db" / "db.sqlite")
    cases = load_bird_cases(root)
    observations = tmp_path / "observations.jsonl"
    observations.write_text(
        json.dumps(
            {
                "benchmark": "bird",
                "ordinal": 1,
                "case_id": "1",
                "outcome": {
                    "status": "succeeded",
                    "executed": True,
                    "sql": "SELECT 2",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    export_predictions("bird", cases, observations, tmp_path)

    predictions = json.loads((tmp_path / "bird_predictions.json").read_text())
    assert list(predictions) == ["0", "1"]
    assert predictions["0"].startswith(EMPTY_PREDICTION)
    assert predictions["1"].startswith("SELECT 2")
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(EMPTY_PREDICTION)


def test_export_predictions_ignores_unexecuted_sql(tmp_path: Path) -> None:
    root = tmp_path / "bird"
    root.mkdir()
    (root / "mini_dev_sqlite.json").write_text(
        json.dumps(
            [
                {
                    "question_id": 0,
                    "db_id": "db",
                    "question": "Count rows.",
                    "SQL": "gold must stay unused",
                }
            ]
        ),
        encoding="utf-8",
    )
    _sqlite_file(root / "dev_databases" / "db" / "db.sqlite")
    observations = tmp_path / "observations.jsonl"
    observations.write_text(
        json.dumps(
            {
                "benchmark": "bird",
                "ordinal": 0,
                "case_id": "0",
                "outcome": {
                    "status": "failed",
                    "executed": False,
                    "sql": "SELECT COUNT(*) FROM items",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    export_predictions("bird", load_bird_cases(root), observations, tmp_path)

    predictions = json.loads((tmp_path / "bird_predictions.json").read_text())
    assert predictions["0"].startswith(EMPTY_PREDICTION)


def test_load_completed_uses_bird_ordinal_when_question_ids_repeat(
    tmp_path: Path,
) -> None:
    observations = tmp_path / "observations.jsonl"
    observations.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "benchmark": "bird",
                        "ordinal": 0,
                        "case_id": "duplicate",
                    }
                ),
                json.dumps(
                    {
                        "benchmark": "bird",
                        "ordinal": 1,
                        "case_id": "duplicate",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    completed = _load_completed(observations)

    assert list(completed) == ["bird:0", "bird:1"]


def test_idempotency_key_changes_when_connection_registration_changes() -> None:
    first = _idempotency_key("bird", "7", "show rows", "conn-first")
    second = _idempotency_key("bird", "7", "show rows", "conn-second")

    assert first != second
    assert first == _idempotency_key("bird", "7", "show rows", "conn-first")


def test_run_case_fetches_terminal_outcome_after_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "database" / "db.sqlite"
    _sqlite_file(database_path)
    case = benchmark_runner.BenchmarkCase(
        ordinal=0,
        case_key="bird:0",
        case_id="7",
        database_id="db",
        database_path=database_path,
        question="Count rows.",
        external_knowledge="",
        difficulty="simple",
    )
    result = TextToSqlResult(
        run_id="run-1",
        status="failed",
        reason_code="DB_AUDIT_OUTPUT_INVALID",
        sql="SELECT COUNT(*) FROM items",
        generated=True,
        approved=False,
        executed=False,
        dry_run=False,
        audited=False,
        rows=[],
        columns=[],
        rows_affected=0,
        error="audit failed",
        execution={},
        audit={},
        persistence={},
    )

    class FakeClient:
        def start(self, request: object) -> SimpleNamespace:
            return SimpleNamespace(run_id="run-1")

        def get_run(self, run_id: str) -> SimpleNamespace:
            assert run_id == "run-1"
            return SimpleNamespace(status="errored", error="transport projection")

        def get_result(self, run_id: str) -> TextToSqlResult:
            assert run_id == "run-1"
            return result

    monkeypatch.setattr(
        benchmark_runner,
        "_client",
        lambda base_url, token: FakeClient(),
    )

    observation = _run_case(
        case,
        benchmark_name="bird",
        base_url="http://example.invalid",
        token="token",
        connection_ref="connection",
        timeout_seconds=1,
        max_rows=100,
    )

    assert observation["workflow_status"] == "errored"
    assert observation["observation_status"] == "completed"
    assert observation["runner_error"] is None
    assert observation["outcome"]["reason_code"] == "DB_AUDIT_OUTPUT_INVALID"
