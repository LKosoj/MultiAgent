"""Oracle-free component and authenticated T13 evaluation adapters."""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlglot import exp, parse_one

from streamlit_app.text_to_sql_client import (
    TextToSqlApiClient,
    TextToSqlApiError,
    TextToSqlRunRequest,
)

from .cases import TextToSQLEvalCase
from .compare import rows_exact_match
from .metrics import SchemaLinkingMetrics, compute_schema_linking_metrics


@dataclass(frozen=True, slots=True)
class EvalGenerationRequest:
    case_id: str
    question: str
    dialect: str
    fixture: str
    max_rows: int
    safety_level: str
    include_explanation: bool
    validate_schema: bool
    dry_run_only: bool
    use_schema_suggestions: bool

    @classmethod
    def from_case(cls, case: TextToSQLEvalCase) -> "EvalGenerationRequest":
        options = case.request_options
        return cls(
            case_id=case.id,
            question=case.question,
            dialect=case.dialect,
            fixture=case.fixture,
            max_rows=options["max_rows"],
            safety_level=options["safety_level"],
            include_explanation=options["include_explanation"],
            validate_schema=options["validate_schema"],
            dry_run_only=options["dry_run_only"],
            use_schema_suggestions=options["use_schema_suggestions"],
        )

    def to_service_payload(self, *, connection_ref: str) -> dict[str, Any]:
        if not isinstance(connection_ref, str) or not connection_ref.strip():
            raise ValueError("connection_ref is required")
        return {
            "query": self.question,
            "connection_ref": connection_ref.strip(),
            "max_rows": self.max_rows,
            "safety_level": self.safety_level,
            "include_explanation": self.include_explanation,
            "validate_schema": self.validate_schema,
            "dry_run_only": self.dry_run_only,
            "use_schema_suggestions": self.use_schema_suggestions,
        }


@dataclass(frozen=True, slots=True)
class EvalResult:
    case_id: str
    generated_sql: str
    passed: bool
    actual_rows: list[dict[str, Any]]
    expected_rows: list[dict[str, Any]]
    duration_ms: float
    error: str | None = None
    schema_linking_metrics: SchemaLinkingMetrics | None = None


@dataclass(frozen=True, slots=True)
class EvalObservation:
    case_id: str
    run_id: str
    status: str
    reason_code: str
    generated_sql: str
    rows: list[Any]
    schema_links: dict[str, Any]
    duration_ms: float
    error: str | None
    columns: list[str] | None = None
    repetition: int = 0
    generated: bool = False
    approved: bool = False
    executed: bool = False
    dry_run: bool = False
    audited: bool = False


def schema_links_from_sql(sql: str, *, dialect: str) -> dict[str, list[str]]:
    """Derive observable table/column selections without accessing case oracles."""
    if not sql.strip():
        return {"tables": [], "columns": []}
    expression = parse_one(sql, read=dialect)
    aliases: dict[str, str] = {}
    tables: set[str] = set()
    for table in expression.find_all(exp.Table):
        table_name = table.name.lower()
        if not table_name:
            continue
        tables.add(table_name)
        aliases[(table.alias or table_name).lower()] = table_name
    columns: set[str] = set()
    for column in expression.find_all(exp.Column):
        column_name = column.name.lower()
        qualifier = column.table.lower()
        table_name = aliases.get(qualifier) if qualifier else None
        if table_name is None and len(tables) == 1:
            table_name = next(iter(tables))
        columns.add(f"{table_name}.{column_name}" if table_name else column_name)
    return {"tables": sorted(tables), "columns": sorted(columns)}


def run_sqlite_query(db_path: str | Path, sql: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(sql)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def run_sqlite_eval(
    case: TextToSQLEvalCase,
    *,
    db_path: str | Path,
    generate_sql: Callable[[EvalGenerationRequest], str],
    schema_linking_provider: Callable[[EvalGenerationRequest], Any] | None = None,
) -> EvalResult:
    """Run the component harness without exposing oracle fields to callbacks."""
    started = time.perf_counter()
    generated_sql = ""
    schema_linking_metrics = None
    request = EvalGenerationRequest.from_case(case)
    try:
        generated_sql = generate_sql(request)
        if not isinstance(generated_sql, str) or not generated_sql.strip():
            raise ValueError("generator must return non-empty SQL")
        if case.expected_schema_links is not None and schema_linking_provider is not None:
            schema_linking_metrics = compute_schema_linking_metrics(
                case.expected_schema_links,
                schema_linking_provider(request),
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


class AuthenticatedT13EvalAdapter:
    """Submit eval requests exclusively through the authenticated T13 client."""

    _TERMINAL_HTTP_STATUSES = frozenset(
        {"finished", "errored", "cancelled", "completed", "failed"}
    )

    def __init__(self, client: TextToSqlApiClient) -> None:
        if not isinstance(client, TextToSqlApiClient):
            raise TypeError("client must be TextToSqlApiClient")
        self._client = client

    def run_case(
        self,
        case: TextToSQLEvalCase,
        *,
        connection_ref: str,
    ) -> EvalObservation:
        request = EvalGenerationRequest.from_case(case)
        payload = request.to_service_payload(connection_ref=connection_ref)
        started = time.perf_counter()
        handle = self._client.start(
            TextToSqlRunRequest(
                query=payload["query"],
                connection_ref=payload["connection_ref"],
                idempotency_key=f"eval-{case.id}-{uuid.uuid4().hex}",
                max_rows=payload["max_rows"],
                safety_level=payload["safety_level"],
                include_explanation=payload["include_explanation"],
                validate_schema=payload["validate_schema"],
                dry_run_only=payload["dry_run_only"],
                use_schema_suggestions=payload["use_schema_suggestions"],
            )
        )
        if case.cancel_after_start:
            self._client.cancel(handle.run_id)
        terminal = False
        for _ in range(self._client.max_poll_attempts):
            status = self._client.get_run(handle.run_id)
            if status.status in self._TERMINAL_HTTP_STATUSES:
                terminal = True
                break
            time.sleep(self._client.poll_interval_seconds)
        if not terminal:
            self._client.cancel(handle.run_id)
            for _ in range(self._client.max_poll_attempts):
                status = self._client.get_run(handle.run_id)
                if status.status in self._TERMINAL_HTTP_STATUSES:
                    terminal = True
                    break
                time.sleep(self._client.poll_interval_seconds)
        if not terminal:
            raise TextToSqlApiError(
                "release eval run timed out and cleanup did not reach a "
                f"terminal status: {handle.run_id}"
            )
        result = self._client.get_result(handle.run_id)
        schema_links = result.raw.get("schema_links")
        if not isinstance(schema_links, dict):
            schema_links = schema_links_from_sql(result.sql, dialect=case.dialect)
        return EvalObservation(
            case_id=case.id,
            run_id=result.run_id,
            status=result.status,
            reason_code=result.reason_code,
            generated_sql=result.sql,
            rows=list(result.rows),
            schema_links=dict(schema_links),
            duration_ms=(time.perf_counter() - started) * 1000,
            error=result.error,
            columns=list(result.columns),
            generated=result.generated,
            approved=result.approved,
            executed=result.executed,
            dry_run=result.dry_run,
            audited=result.audited,
        )
