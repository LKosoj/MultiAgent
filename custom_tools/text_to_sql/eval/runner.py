"""Deterministic local runners for Text-to-SQL eval cases."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .cases import TextToSQLEvalCase
from .compare import rows_exact_match
from .metrics import SchemaLinkingMetrics, compute_schema_linking_metrics


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    generated_sql: str
    passed: bool
    actual_rows: list[dict]
    expected_rows: list[dict]
    duration_ms: float
    error: str | None = None
    schema_linking_metrics: SchemaLinkingMetrics | None = None


def run_sqlite_query(db_path: str | Path, sql: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(sql)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def run_sqlite_eval(
    case: TextToSQLEvalCase,
    *,
    db_path: str | Path,
    generate_sql: Callable[[TextToSQLEvalCase], str],
    schema_linking_provider: Callable[[TextToSQLEvalCase], Any] | None = None,
) -> EvalResult:
    started = time.perf_counter()
    generated_sql = ""
    schema_linking_metrics = None
    try:
        generated_sql = generate_sql(case)
        if case.expected_schema_links is not None and schema_linking_provider is not None:
            schema_linking_metrics = compute_schema_linking_metrics(
                case.expected_schema_links,
                schema_linking_provider(case),
            )
        actual_rows = run_sqlite_query(db_path, generated_sql)
        if case.expected_rows is not None:
            expected_rows = case.expected_rows
        elif case.expected_sql:
            expected_rows = run_sqlite_query(db_path, case.expected_sql)
        else:
            raise ValueError(f"case {case.id}: expected_rows or expected_sql is required")
        return EvalResult(
            case_id=case.id,
            generated_sql=generated_sql,
            passed=rows_exact_match(actual_rows, expected_rows),
            actual_rows=actual_rows,
            expected_rows=expected_rows,
            duration_ms=(time.perf_counter() - started) * 1000,
            schema_linking_metrics=schema_linking_metrics,
        )
    except Exception as exc:
        return EvalResult(
            case_id=case.id,
            generated_sql=generated_sql,
            passed=False,
            actual_rows=[],
            expected_rows=case.expected_rows or [],
            duration_ms=(time.perf_counter() - started) * 1000,
            error=str(exc),
            schema_linking_metrics=schema_linking_metrics,
        )
