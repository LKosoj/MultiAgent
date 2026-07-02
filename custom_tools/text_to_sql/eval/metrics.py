"""Metrics for Text-to-SQL eval runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class SchemaLinkingMetrics:
    expected_tables: int
    actual_tables: int
    matched_tables: int
    table_precision: float
    table_recall: float
    table_f1: float
    expected_columns: int
    actual_columns: int
    matched_columns: int
    column_precision: float
    column_recall: float
    column_f1: float


@dataclass(frozen=True)
class EvalSummary:
    total: int
    passed: int
    failed: int
    execution_accuracy: float
    avg_duration_ms: float
    schema_linking_cases: int = 0
    avg_table_precision: float | None = None
    avg_table_recall: float | None = None
    avg_column_precision: float | None = None
    avg_column_recall: float | None = None


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def _normalize_column(table: Any, column: Any) -> str:
    column_name = _norm(column)
    table_name = _norm(table)
    return f"{table_name}.{column_name}" if table_name else column_name


def _add_entity_link(entity: Any, tables: set[str], columns: set[str]) -> None:
    if isinstance(entity, str):
        if "." in entity:
            table, column = entity.rsplit(".", 1)
            tables.add(_norm(table))
            columns.add(_normalize_column(table, column))
        return
    if not isinstance(entity, dict):
        return
    table = entity.get("table") or entity.get("table_name")
    column = entity.get("column") or entity.get("column_name")
    if table:
        tables.add(_norm(table))
    if column:
        columns.add(_normalize_column(table, column))


def normalize_schema_links(payload: Any) -> dict[str, set[str]]:
    """Normalize eval/generator schema-link payloads into table/column sets."""
    tables: set[str] = set()
    columns: set[str] = set()
    if not isinstance(payload, dict):
        return {"tables": tables, "columns": columns}

    if isinstance(payload.get("linked_entities"), dict):
        payload = payload["linked_entities"]

    raw_tables = payload.get("tables")
    if isinstance(raw_tables, list):
        tables.update(_norm(table) for table in raw_tables if str(table).strip())

    raw_columns = payload.get("columns")
    if isinstance(raw_columns, list):
        for item in raw_columns:
            if isinstance(item, str):
                if item.strip():
                    columns.add(_norm(item))
                    if "." in item:
                        tables.add(_norm(item.rsplit(".", 1)[0]))
            else:
                _add_entity_link(item, tables, columns)

    for kind in ("metrics", "dimensions"):
        raw_entities = payload.get(kind)
        if isinstance(raw_entities, list):
            for entity in raw_entities:
                _add_entity_link(entity, tables, columns)

    filters = payload.get("filters")
    if isinstance(filters, dict):
        for value in filters.values():
            if isinstance(value, list):
                for entity in value:
                    _add_entity_link(entity, tables, columns)
            else:
                _add_entity_link(value, tables, columns)

    return {"tables": tables, "columns": columns}


def _precision_recall_f1(expected: set[str], actual: set[str]) -> tuple[int, float, float, float]:
    matched = len(expected & actual)
    precision = (matched / len(actual)) if actual else (1.0 if not expected else 0.0)
    recall = (matched / len(expected)) if expected else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return matched, precision, recall, f1


def compute_schema_linking_metrics(expected: Any, actual: Any) -> SchemaLinkingMetrics:
    expected_links = normalize_schema_links(expected)
    actual_links = normalize_schema_links(actual)
    expected_tables = expected_links["tables"]
    actual_tables = actual_links["tables"]
    expected_columns = expected_links["columns"]
    actual_columns = actual_links["columns"]
    matched_tables, table_precision, table_recall, table_f1 = _precision_recall_f1(
        expected_tables, actual_tables
    )
    matched_columns, column_precision, column_recall, column_f1 = _precision_recall_f1(
        expected_columns, actual_columns
    )
    return SchemaLinkingMetrics(
        expected_tables=len(expected_tables),
        actual_tables=len(actual_tables),
        matched_tables=matched_tables,
        table_precision=table_precision,
        table_recall=table_recall,
        table_f1=table_f1,
        expected_columns=len(expected_columns),
        actual_columns=len(actual_columns),
        matched_columns=matched_columns,
        column_precision=column_precision,
        column_recall=column_recall,
        column_f1=column_f1,
    )


def summarize_results(results: Iterable[object]) -> EvalSummary:
    result_list = list(results)
    total = len(result_list)
    passed = sum(1 for result in result_list if bool(getattr(result, "passed", False)))
    failed = total - passed
    accuracy = (passed / total) if total else 0.0
    avg_duration_ms = (
        sum(float(getattr(result, "duration_ms", 0.0) or 0.0) for result in result_list) / total
        if total
        else 0.0
    )
    schema_metrics = [
        metrics
        for metrics in (getattr(result, "schema_linking_metrics", None) for result in result_list)
        if isinstance(metrics, SchemaLinkingMetrics)
    ]
    schema_count = len(schema_metrics)
    avg_table_precision = (
        sum(metrics.table_precision for metrics in schema_metrics) / schema_count
        if schema_count
        else None
    )
    avg_table_recall = (
        sum(metrics.table_recall for metrics in schema_metrics) / schema_count
        if schema_count
        else None
    )
    avg_column_precision = (
        sum(metrics.column_precision for metrics in schema_metrics) / schema_count
        if schema_count
        else None
    )
    avg_column_recall = (
        sum(metrics.column_recall for metrics in schema_metrics) / schema_count
        if schema_count
        else None
    )
    return EvalSummary(
        total=total,
        passed=passed,
        failed=failed,
        execution_accuracy=accuracy,
        avg_duration_ms=avg_duration_ms,
        schema_linking_cases=schema_count,
        avg_table_precision=avg_table_precision,
        avg_table_recall=avg_table_recall,
        avg_column_precision=avg_column_precision,
        avg_column_recall=avg_column_recall,
    )
