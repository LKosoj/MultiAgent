from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from scripts.text2sql_public_benchmark import (
    EMPTY_PREDICTION,
    _idempotency_key,
    benchmark_prompt,
    export_predictions,
    load_bird_cases,
    load_spider_cases,
)


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
                "case_id": "1",
                "outcome": {"sql": "SELECT 2"},
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


def test_idempotency_key_changes_when_connection_registration_changes() -> None:
    first = _idempotency_key("bird", "7", "show rows", "conn-first")
    second = _idempotency_key("bird", "7", "show rows", "conn-second")

    assert first != second
    assert first == _idempotency_key("bird", "7", "show rows", "conn-first")
