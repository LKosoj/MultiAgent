"""Metrics for Text-to-SQL eval runs."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

from .compare import normalize_rows


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


@dataclass(frozen=True, slots=True)
class ReleaseMetrics:
    total: int
    passed: int
    execution_total: int
    contract_and_safety_total: int
    execution_accuracy_overall: float
    execution_accuracy_per_slice: dict[str, float]
    schema_table_recall: float
    schema_column_f1: float
    expected_abstention_precision: float
    expected_abstention_recall: float
    unexpected_error_rate: float
    contract_and_safety_case_pass_rate: float
    latency_p95_ms: float
    latency_p95_regression_fraction: float | None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "execution_total": self.execution_total,
            "contract_and_safety_total": self.contract_and_safety_total,
            "execution_accuracy_overall": self.execution_accuracy_overall,
            "execution_accuracy_per_slice": dict(self.execution_accuracy_per_slice),
            "schema_table_recall": self.schema_table_recall,
            "schema_column_f1": self.schema_column_f1,
            "expected_abstention_precision": self.expected_abstention_precision,
            "expected_abstention_recall": self.expected_abstention_recall,
            "unexpected_error_rate": self.unexpected_error_rate,
            "contract_and_safety_case_pass_rate": self.contract_and_safety_case_pass_rate,
            "latency_p95_ms": self.latency_p95_ms,
            "latency_p95_regression_fraction": self.latency_p95_regression_fraction,
        }


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


def _observation_rows(observation: object) -> list[dict[str, Any]]:
    rows = getattr(observation, "rows", None)
    if not isinstance(rows, list):
        return []
    if all(isinstance(row, dict) for row in rows):
        return [dict(row) for row in rows]
    columns = getattr(observation, "columns", None)
    if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns):
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != len(columns):
            return []
        normalized.append(dict(zip(columns, row)))
    return normalized


def _rows_match(mode: str, actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    if mode == "exact":
        return normalize_rows(actual) == normalize_rows(expected)
    if mode == "unordered":
        def canonical(rows: list[dict[str, Any]]) -> list[tuple[tuple[str, Any], ...]]:
            return sorted(
                (tuple(sorted(row.items())) for row in rows),
                key=repr,
            )

        return canonical(actual) == canonical(expected)
    return mode == "none"


def _contract_passed(case: object, observation: object) -> bool:
    expected_outcome = getattr(case, "expected_outcome", None)
    actual_outcome = getattr(observation, "status", None)
    if actual_outcome != expected_outcome:
        return False
    expected_reasons = tuple(getattr(case, "expected_reason_codes", ()))
    if expected_reasons and getattr(observation, "reason_code", None) not in expected_reasons:
        return False
    executed = bool(getattr(observation, "executed", False))
    dry_run = bool(getattr(observation, "dry_run", False))
    generated = bool(getattr(observation, "generated", False))
    approved = bool(getattr(observation, "approved", False))
    audited = bool(getattr(observation, "audited", False))
    if executed and dry_run:
        return False
    if (executed or dry_run or audited) and not (generated and approved):
        return False
    request_options = getattr(case, "request_options", {})
    expects_dry_run = bool(request_options.get("dry_run_only", False))
    if expected_outcome == "succeeded":
        if expects_dry_run:
            return dry_run and not executed and generated and approved and audited
        return executed and not dry_run and generated and approved and audited
    if expected_outcome in {"abstained", "cancelled", "timed_out"}:
        return not executed and not dry_run and not audited
    return True


def _execution_passed(case: object, observation: object) -> bool:
    if not _contract_passed(case, observation):
        return False
    if getattr(case, "expected_outcome", None) != "succeeded":
        return False
    if getattr(case, "request_options", {}).get("dry_run_only", False):
        return True
    expected_rows = getattr(case, "expected_rows", None)
    return isinstance(expected_rows, list) and _rows_match(
        getattr(case, "comparison_mode", "none"),
        _observation_rows(observation),
        expected_rows,
    )


def _is_execution_case(case: object) -> bool:
    return (
        getattr(case, "expected_outcome", None) == "succeeded"
        and not getattr(case, "request_options", {}).get("dry_run_only", False)
    )


def _case_passed(case: object, observation: object) -> bool:
    if _is_execution_case(case):
        return _execution_passed(case, observation)
    return _contract_passed(case, observation)


def _percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def compute_release_metrics(
    cases: Iterable[object],
    observations: Iterable[object],
    *,
    latency_baseline_p95_ms: float | None = None,
) -> ReleaseMetrics:
    case_list = list(cases)
    by_case: dict[str, list[object]] = {}
    for item in observations:
        case_id = getattr(item, "case_id", None)
        if isinstance(case_id, str):
            by_case.setdefault(case_id, []).append(item)
    passed_count = 0
    total = 0
    execution_total = 0
    contract_passed_count = 0
    slice_values: dict[str, list[bool]] = {}
    schema_metrics: list[SchemaLinkingMetrics] = []
    expected_abstentions = 0
    actual_abstentions = 0
    correct_abstentions = 0
    unexpected_errors = 0
    durations: list[float] = []
    for case in case_list:
        trials: list[object | None] = by_case.get(case.id) or [None]
        for observation in trials:
            passed = observation is not None and _case_passed(case, observation)
            passed_count += int(passed)
            total += 1
            contract_passed = (
                observation is not None and _contract_passed(case, observation)
            )
            contract_passed_count += int(contract_passed)
            if _is_execution_case(case):
                execution_passed = (
                    observation is not None
                    and _execution_passed(case, observation)
                )
                execution_total += 1
                for tag in case.slice_tags:
                    slice_values.setdefault(tag, []).append(execution_passed)
            expected_abstain = case.expected_outcome == "abstained"
            actual_abstain = (
                observation is not None
                and getattr(observation, "status", None) == "abstained"
            )
            expected_abstentions += int(expected_abstain)
            actual_abstentions += int(actual_abstain)
            correct_abstentions += int(
                expected_abstain and actual_abstain and contract_passed
            )
            if observation is None:
                if case.expected_schema_links is not None:
                    schema_metrics.append(
                        compute_schema_linking_metrics(case.expected_schema_links, {})
                    )
                continue
            duration = float(getattr(observation, "duration_ms", 0.0))
            if math.isfinite(duration) and duration >= 0:
                durations.append(duration)
            unexpected_errors += int(
                getattr(observation, "status", None) == "failed"
                and case.expected_outcome != "failed"
            )
            if case.expected_schema_links is not None:
                schema_metrics.append(
                    compute_schema_linking_metrics(
                        case.expected_schema_links,
                        getattr(observation, "schema_links", {}),
                    )
                )
    accuracy = (
        sum(
            observation is not None and _execution_passed(case, observation)
            for case in case_list
            for observation in (by_case.get(case.id) or [None])
            if _is_execution_case(case)
        )
        / execution_total
        if execution_total
        else 0.0
    )
    per_slice = {
        tag: sum(values) / len(values)
        for tag, values in sorted(slice_values.items())
    }
    schema_table_recall = (
        sum(item.table_recall for item in schema_metrics) / len(schema_metrics)
        if schema_metrics
        else 0.0
    )
    schema_column_f1 = (
        sum(item.column_f1 for item in schema_metrics) / len(schema_metrics)
        if schema_metrics
        else 0.0
    )
    latency_p95 = _percentile_95(durations)
    latency_regression = None
    if latency_baseline_p95_ms is not None:
        if latency_baseline_p95_ms <= 0 or not math.isfinite(latency_baseline_p95_ms):
            raise ValueError("latency baseline must be a positive finite number")
        latency_regression = (latency_p95 - latency_baseline_p95_ms) / latency_baseline_p95_ms
    return ReleaseMetrics(
        total=total,
        passed=passed_count,
        execution_total=execution_total,
        contract_and_safety_total=total,
        execution_accuracy_overall=accuracy,
        execution_accuracy_per_slice=per_slice,
        schema_table_recall=schema_table_recall,
        schema_column_f1=schema_column_f1,
        expected_abstention_precision=(
            correct_abstentions / actual_abstentions if actual_abstentions else 0.0
        ),
        expected_abstention_recall=(
            correct_abstentions / expected_abstentions if expected_abstentions else 1.0
        ),
        unexpected_error_rate=unexpected_errors / total if total else 0.0,
        contract_and_safety_case_pass_rate=(
            contract_passed_count / total if total else 0.0
        ),
        latency_p95_ms=latency_p95,
        latency_p95_regression_fraction=latency_regression,
    )


def compute_release_metrics_by_repetition(
    cases: Iterable[object],
    observations: Iterable[object],
    *,
    expected_repetitions: int,
    latency_baseline_p95_ms: float | None = None,
) -> dict[int, ReleaseMetrics]:
    if type(expected_repetitions) is not int or expected_repetitions <= 0:
        raise ValueError("expected_repetitions must be a positive integer")
    grouped: dict[int, list[object]] = {
        repetition: [] for repetition in range(1, expected_repetitions + 1)
    }
    seen: set[tuple[int, str]] = set()
    for observation in observations:
        repetition = getattr(observation, "repetition", None)
        case_id = getattr(observation, "case_id", None)
        if repetition not in grouped or not isinstance(case_id, str):
            raise ValueError("observation repetition is outside the policy range")
        key = (repetition, case_id)
        if key in seen:
            raise ValueError("duplicate case observation within a repetition")
        seen.add(key)
        grouped[repetition].append(observation)
    case_list = list(cases)
    return {
        repetition: compute_release_metrics(
            case_list,
            grouped[repetition],
            latency_baseline_p95_ms=latency_baseline_p95_ms,
        )
        for repetition in grouped
    }


def evaluate_release_thresholds(
    metrics: ReleaseMetrics,
    thresholds: dict[str, Any],
    *,
    latency_gate_mode: str = "prior_release",
) -> tuple[str, ...]:
    if latency_gate_mode not in {"prior_release", "bootstrap"}:
        raise ValueError("latency_gate_mode must be prior_release or bootstrap")
    failures: list[str] = []
    minimums = {
        "execution_accuracy_overall_min": metrics.execution_accuracy_overall,
        "schema_table_recall_min": metrics.schema_table_recall,
        "schema_column_f1_min": metrics.schema_column_f1,
        "expected_abstention_precision_min": metrics.expected_abstention_precision,
        "expected_abstention_recall_min": metrics.expected_abstention_recall,
        "contract_and_safety_case_pass_rate_min": metrics.contract_and_safety_case_pass_rate,
    }
    for threshold_name, actual in minimums.items():
        if actual < float(thresholds[threshold_name]):
            failures.append(f"{threshold_name}:{actual}")
    per_slice_minimum = float(thresholds["execution_accuracy_per_slice_min"])
    for slice_name, actual in metrics.execution_accuracy_per_slice.items():
        if actual < per_slice_minimum:
            failures.append(f"execution_accuracy_per_slice_min:{slice_name}:{actual}")
    if metrics.unexpected_error_rate > float(thresholds["unexpected_error_rate_max"]):
        failures.append(f"unexpected_error_rate_max:{metrics.unexpected_error_rate}")
    if latency_gate_mode == "prior_release":
        if metrics.latency_p95_regression_fraction is None:
            failures.append("latency_p95_regression:baseline_missing")
        elif metrics.latency_p95_regression_fraction > float(
            thresholds["latency_p95_regression_max_fraction"]
        ):
            failures.append(
                f"latency_p95_regression_max_fraction:{metrics.latency_p95_regression_fraction}"
            )
    return tuple(failures)
