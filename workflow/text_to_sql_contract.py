"""
Text-to-SQL terminal contract
==============================

Строгий, JSON-сериализуемый контракт терминального результата пайплайна
Text-to-SQL: константы, JSON-safe примитивы, перечисления статусов/причин,
валидаторы границы executor'а и класс ``TextToSqlTerminalResult``.

Вынесено из ``workflow.models`` (T8) без изменения публичного поведения;
``workflow.models`` реэкспортирует нужные имена для существующих импортёров.
"""

from dataclasses import dataclass
from collections.abc import Iterator, Sequence
from itertools import islice
from typing import Any, Dict, Mapping, Optional
from enum import Enum
import math
import json

TEXT_TO_SQL_WORKFLOW_NAME = "text_to_sql_pipeline"
TEXT_TO_SQL_WORKFLOW_CATEGORY = "text_to_sql"
TEXT_TO_SQL_MAX_ERROR_LENGTH = 4096
TEXT_TO_SQL_MAX_JSON_DEPTH = 64
TEXT_TO_SQL_MAX_JSON_NODES = 100_000


class _FrozenJsonList(tuple):
    """Immutable JSON array with JSON-semantic equality."""

    def __eq__(self, other: object) -> bool:
        return _json_values_equal(self, other)

    __hash__ = None


class _FrozenJsonDict(Mapping[str, Any]):
    """Immutable JSON object backed only by immutable key/value pairs."""

    __slots__ = ("_data",)

    def __init__(self, value: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_data", dict(value))

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("frozen JSON object does not support mutation")

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        return _json_values_equal(self, other)


def _json_values_equal(left: Any, right: Any) -> bool:
    """Compare JSON values with JavaScript ``Object.is`` scalar semantics."""
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is bool and type(right) is bool and left is right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return False
        if isinstance(left, bool) or isinstance(right, bool):
            return False
        if left == 0 and right == 0:
            return math.copysign(1.0, float(left)) == math.copysign(1.0, float(right))
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return type(left) is str and type(right) is str and left == right
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if set(left) != set(right):
            return False
        return all(_json_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
            return False
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return False


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenJsonDict({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return _FrozenJsonList(_freeze_json_value(item) for item in value)
    return value


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json_value(item) for item in value]
    return value


def is_text_to_sql_workflow_name(value: Any) -> bool:
    """Return whether *value* is the authoritative Text-to-SQL workflow name."""
    return value == TEXT_TO_SQL_WORKFLOW_NAME


def bound_text_to_sql_error(value: Any) -> str:
    """Return bounded text without allowing hostile formatting to escape."""
    try:
        rendered = str(value)
    except BaseException:
        try:
            rendered = repr(value)
        except BaseException:
            rendered = text_to_sql_type_name(value)
    if type(rendered) is not str or not rendered:
        rendered = "unprintable error"
    try:
        return rendered[:TEXT_TO_SQL_MAX_ERROR_LENGTH]
    except BaseException:
        return "unprintable error"


def text_to_sql_type_name(value: Any) -> str:
    """Return a stable type name even for objects with a hostile metaclass."""
    try:
        value_type = type(value)
        name = type.__getattribute__(value_type, "__name__")
    except BaseException:
        return "unprintable error"
    return name if type(name) is str and name else "unprintable error"


def preflight_text_to_sql_json_value(
    value: Any,
    *,
    field_name: str,
    allow_non_json_leaves: bool = False,
) -> None:
    """Validate JSON shape iteratively before any recursive copy or freeze."""
    _copy_text_to_sql_json_value(
        value,
        field_name=field_name,
        allow_non_json_leaves=allow_non_json_leaves,
    )


def _copy_text_to_sql_json_value(
    value: Any,
    *,
    field_name: str,
    allow_non_json_leaves: bool = False,
) -> Any:
    root: list[Any] = [None]
    stack: list[tuple[bool, Any, str, int, Any, Any]] = [
        (False, value, field_name, 0, root, 0)
    ]
    active_containers: set[int] = set()
    scheduled_node_count = 1

    while stack:
        leaving, current, path, depth, parent, slot = stack.pop()
        if leaving:
            active_containers.remove(id(current))
            continue

        if scheduled_node_count > TEXT_TO_SQL_MAX_JSON_NODES:
            raise ValueError(
                f"{field_name} exceeds the supported JSON node limit "
                f"({TEXT_TO_SQL_MAX_JSON_NODES})"
            )
        if current is None or isinstance(current, (str, bool, int)):
            parent[slot] = current
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{path} must contain only finite JSON numbers")
            parent[slot] = current
            continue

        is_sequence = isinstance(current, (list, tuple))
        is_mapping = isinstance(current, Mapping)
        if not is_sequence and not is_mapping:
            if allow_non_json_leaves:
                parent[slot] = current
                continue
            raise TypeError(
                f"{path} contains non-JSON-serializable value "
                f"{text_to_sql_type_name(current)}"
            )
        if depth >= TEXT_TO_SQL_MAX_JSON_DEPTH:
            raise ValueError(
                f"{field_name} exceeds the supported JSON nesting depth "
                f"({TEXT_TO_SQL_MAX_JSON_DEPTH})"
            )

        identity = id(current)
        if identity in active_containers:
            raise ValueError(f"{path} must not contain circular JSON values")
        active_containers.add(identity)
        stack.append((True, current, path, depth, None, None))

        remaining_nodes = TEXT_TO_SQL_MAX_JSON_NODES - scheduled_node_count
        if type(current) in {dict, list, tuple} and len(current) > remaining_nodes:
            raise ValueError(
                f"{field_name} exceeds the supported JSON node limit "
                f"({TEXT_TO_SQL_MAX_JSON_NODES})"
            )
        try:
            children = list(islice(iter(current), remaining_nodes + 1))
        except BaseException as exc:
            container_kind = "object" if is_mapping else "array"
            raise TypeError(
                f"{path} is not a readable JSON {container_kind}: "
                f"{bound_text_to_sql_error(exc)}"
            ) from None
        if len(children) > remaining_nodes:
            raise ValueError(
                f"{field_name} exceeds the supported JSON node limit "
                f"({TEXT_TO_SQL_MAX_JSON_NODES})"
            )
        scheduled_node_count += len(children)

        if is_mapping:
            copied: Any = {}
            parent[slot] = copied
            items: list[tuple[str, Any]] = []
            for key in children:
                lookup_key = key
                if not isinstance(key, str):
                    if allow_non_json_leaves:
                        key = bound_text_to_sql_error(key)
                    else:
                        raise TypeError(
                            f"{path} JSON object must use string keys"
                        )
                elif type(key) is not str:
                    key = str.__str__(key)
                try:
                    item = current[lookup_key]
                except BaseException as exc:
                    raise TypeError(
                        f"{path} is not a readable JSON object: "
                        f"{bound_text_to_sql_error(exc)}"
                    ) from None
                items.append((key, item))
            for key, item in reversed(items):
                stack.append(
                    (False, item, f"{path}.{key}", depth + 1, copied, key)
                )
        else:
            copied = [None] * len(children)
            parent[slot] = copied
            for index in range(len(children) - 1, -1, -1):
                stack.append(
                    (
                        False,
                        children[index],
                        f"{path}[{index}]",
                        depth + 1,
                        copied,
                        index,
                    )
                )
    return root[0]


def normalize_text_to_sql_json_value(value: Any, *, field_name: str) -> Any:
    """Return an isolated, finite, JSON-safe copy for a terminal adapter."""
    return _isolated_json_value(value, field_name=field_name)


def _isolated_json_value(value: Any, *, field_name: str) -> Any:
    """Validate and deep-copy one terminal-contract JSON value."""
    isolated = _copy_text_to_sql_json_value(value, field_name=field_name)
    try:
        return json.loads(
            json.dumps(
                isolated,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except RecursionError as exc:
        raise ValueError(
            f"{field_name} exceeds the supported JSON nesting depth"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field_name} must be JSON-serializable") from exc


class TextToSqlTerminalStatus(str, Enum):
    """Authoritative terminal states for the Text-to-SQL workflow."""

    SUCCEEDED = "succeeded"
    ABSTAINED = "abstained"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TextToSqlVerificationStatus(str, Enum):
    """Exact verifier outcomes accepted by the deterministic finalizer."""

    APPROVED = "Approved"
    REJECTED = "Rejected"


class TextToSqlTerminalReasonCode(str, Enum):
    """Closed reason-code vocabulary for non-success terminal outcomes."""

    VERIFIER_CONTRACT_INVALID = "VERIFIER_CONTRACT_INVALID"
    VERIFIER_REJECTED = "VERIFIER_REJECTED"
    SCHEMA_CLARIFICATION_REQUIRED = "SCHEMA_CLARIFICATION_REQUIRED"
    SCHEMA_GROUNDING_FAILED = "SCHEMA_GROUNDING_FAILED"
    SCHEMA_CONTEXT_BUDGET_EXCEEDED = "SCHEMA_CONTEXT_BUDGET_EXCEEDED"
    EXECUTOR_CONTRACT_INVALID = "EXECUTOR_CONTRACT_INVALID"
    AUDIT_CONTRACT_INVALID = "AUDIT_CONTRACT_INVALID"
    AUDIT_FAILED = "AUDIT_FAILED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    PERSISTENCE_CONTRACT_INVALID = "PERSISTENCE_CONTRACT_INVALID"
    DB_AUDIT_MISSING = "DB_AUDIT_MISSING"
    DB_AUDIT_FAILED = "DB_AUDIT_FAILED"
    DB_AUDIT_NOT_TERMINAL = "DB_AUDIT_NOT_TERMINAL"
    DB_AUDIT_OUTPUT_INVALID = "DB_AUDIT_OUTPUT_INVALID"
    DB_AUDIT_RUN_ID_MISMATCH = "DB_AUDIT_RUN_ID_MISMATCH"
    DB_AUDIT_SKIPPED_WITHOUT_ABSTENTION = "DB_AUDIT_SKIPPED_WITHOUT_ABSTENTION"
    DB_AUDIT_SKIPPED_AFTER_APPROVAL = "DB_AUDIT_SKIPPED_AFTER_APPROVAL"
    MANDATORY_STEP_NOT_COMPLETED = "MANDATORY_STEP_NOT_COMPLETED"
    SQL_GENERATION_OUTPUT_MISMATCH = "SQL_GENERATION_OUTPUT_MISMATCH"
    SQL_VERIFICATION_OUTPUT_MISMATCH = "SQL_VERIFICATION_OUTPUT_MISMATCH"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    RESULT_AGGREGATION_FAILED = "RESULT_AGGREGATION_FAILED"
    RESULT_PERSISTENCE_FAILED = "RESULT_PERSISTENCE_FAILED"
    RESULT_RECONCILIATION_FAILED = "RESULT_RECONCILIATION_FAILED"
    OUTPUT_RETRY_CHAIN_FAILED = "OUTPUT_RETRY_CHAIN_FAILED"


_TEXT_TO_SQL_FAILURE_EVIDENCE_PROFILES = {
    TextToSqlTerminalReasonCode.VERIFIER_CONTRACT_INVALID: "no_runtime",
    TextToSqlTerminalReasonCode.EXECUTOR_CONTRACT_INVALID: "untrusted_executor",
    TextToSqlTerminalReasonCode.AUDIT_CONTRACT_INVALID: "audit_failure",
    TextToSqlTerminalReasonCode.AUDIT_FAILED: "audit_failure",
    TextToSqlTerminalReasonCode.EXECUTION_FAILED: "execution_failure",
    TextToSqlTerminalReasonCode.PERSISTENCE_CONTRACT_INVALID: (
        "persistence_contract_failure"
    ),
    TextToSqlTerminalReasonCode.DB_AUDIT_MISSING: "no_runtime",
    TextToSqlTerminalReasonCode.DB_AUDIT_FAILED: "no_runtime",
    TextToSqlTerminalReasonCode.DB_AUDIT_NOT_TERMINAL: "no_runtime",
    TextToSqlTerminalReasonCode.DB_AUDIT_OUTPUT_INVALID: "no_runtime",
    TextToSqlTerminalReasonCode.DB_AUDIT_RUN_ID_MISMATCH: "no_runtime",
    TextToSqlTerminalReasonCode.DB_AUDIT_SKIPPED_WITHOUT_ABSTENTION: "no_runtime",
    TextToSqlTerminalReasonCode.DB_AUDIT_SKIPPED_AFTER_APPROVAL: "no_runtime",
    TextToSqlTerminalReasonCode.MANDATORY_STEP_NOT_COMPLETED: "no_runtime",
    TextToSqlTerminalReasonCode.SQL_GENERATION_OUTPUT_MISMATCH: "no_runtime",
    TextToSqlTerminalReasonCode.SQL_VERIFICATION_OUTPUT_MISMATCH: "no_runtime",
    TextToSqlTerminalReasonCode.RESULT_AGGREGATION_FAILED: "post_success",
    TextToSqlTerminalReasonCode.RESULT_PERSISTENCE_FAILED: "result_failure",
    TextToSqlTerminalReasonCode.RESULT_RECONCILIATION_FAILED: "result_failure",
    TextToSqlTerminalReasonCode.OUTPUT_RETRY_CHAIN_FAILED: "execution_failure",
}
_TEXT_TO_SQL_ABSTAIN_REASONS = frozenset({
    TextToSqlTerminalReasonCode.VERIFIER_REJECTED,
    TextToSqlTerminalReasonCode.SCHEMA_CLARIFICATION_REQUIRED,
    TextToSqlTerminalReasonCode.SCHEMA_GROUNDING_FAILED,
    TextToSqlTerminalReasonCode.SCHEMA_CONTEXT_BUDGET_EXCEEDED,
})
_TEXT_TO_SQL_NON_FAILED_REASONS = frozenset({
    *_TEXT_TO_SQL_ABSTAIN_REASONS,
    TextToSqlTerminalReasonCode.CANCELLED,
    TextToSqlTerminalReasonCode.TIMED_OUT,
})
if set(_TEXT_TO_SQL_FAILURE_EVIDENCE_PROFILES) != (
    set(TextToSqlTerminalReasonCode) - _TEXT_TO_SQL_NON_FAILED_REASONS
):
    raise RuntimeError("Text-to-SQL failure evidence matrix is incomplete")


# T13b: dedup — раньше дублировался в workflow/enhanced_engine.py как
# frozenset строковых литералов; перенесено сюда как frozenset членов enum'а
# (TextToSqlTerminalReasonCode — str-enum, membership по строке работает).
_TEXT_TO_SQL_SCHEMA_ABSTENTION_REASONS = frozenset({
    TextToSqlTerminalReasonCode.SCHEMA_CLARIFICATION_REQUIRED,
    TextToSqlTerminalReasonCode.SCHEMA_GROUNDING_FAILED,
    TextToSqlTerminalReasonCode.SCHEMA_CONTEXT_BUDGET_EXCEEDED,
})


_TEXT_TO_SQL_ENFORCEMENT_MODE_FIELDS = (
    "timeout_enforcement_mode",
    "cancellation_enforcement_mode",
)
_TEXT_TO_SQL_ENFORCEMENT_MODE_VALUES = frozenset({
    "database",
    "driver",
    "read_only_file",
    "supervisor",
    "none",
})
_TEXT_TO_SQL_EXECUTION_REQUIRED_FIELDS = frozenset({
    "success",
    "data",
    "columns",
    "rows_affected",
    "execution_time_ms",
    "dry_run_only",
    "skipped_execution",
    "sql_query",
    "applied_row_limit",
})
_TEXT_TO_SQL_EXECUTION_OPTIONAL_FIELDS = frozenset({
    "error_message",
    "safety_issues",
    "explain_result",
    "profile_name",
    "policy_version",
    "capability_error",
    "pre_execution_error_code",
    *_TEXT_TO_SQL_ENFORCEMENT_MODE_FIELDS,
})
_TEXT_TO_SQL_EXECUTOR_BOUNDARY_REQUIRED_FIELDS = frozenset({
    *_TEXT_TO_SQL_EXECUTION_REQUIRED_FIELDS,
    "error_message",
})
_TEXT_TO_SQL_EXECUTOR_BOUNDARY_OPTIONAL_FIELDS = frozenset({
    "safety_issues",
    "explain_result",
    "profile_name",
    "policy_version",
    "capability_error",
    "pre_execution_error_code",
    *_TEXT_TO_SQL_ENFORCEMENT_MODE_FIELDS,
})
_TEXT_TO_SQL_ISSUE_FIELDS = frozenset({"issue_type", "description"})
_TEXT_TO_SQL_EXPLAIN_FIELDS = frozenset({
    "plan",
    "estimated_cost",
    "rows_to_scan",
    "issues",
})
_TEXT_TO_SQL_CAPABILITY_ERROR_FIELDS = frozenset({"capability", "reason_code"})


def _validate_text_to_sql_enforcement_modes(
    execution: Mapping[str, Any],
    *,
    field_name: str,
) -> None:
    present = tuple(
        field for field in _TEXT_TO_SQL_ENFORCEMENT_MODE_FIELDS if field in execution
    )
    if not present:
        return
    if len(present) != len(_TEXT_TO_SQL_ENFORCEMENT_MODE_FIELDS):
        raise ValueError(
            f"{field_name} enforcement modes require "
            "timeout_enforcement_mode and cancellation_enforcement_mode together"
        )
    for mode_field in _TEXT_TO_SQL_ENFORCEMENT_MODE_FIELDS:
        value = execution[mode_field]
        if not isinstance(value, str) or not value:
            raise TypeError(
                f"{field_name}.{mode_field} must be a non-empty string"
            )
        if value not in _TEXT_TO_SQL_ENFORCEMENT_MODE_VALUES:
            allowed = ", ".join(sorted(_TEXT_TO_SQL_ENFORCEMENT_MODE_VALUES))
            raise ValueError(
                f"{field_name}.{mode_field} must be one of: {allowed}"
            )


def _validate_text_to_sql_issues(value: Any, *, field_name: str) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list")
    for index, issue in enumerate(value):
        item_name = f"{field_name}[{index}]"
        if not isinstance(issue, dict) or set(issue) != _TEXT_TO_SQL_ISSUE_FIELDS:
            raise ValueError(
                f"{item_name} must contain exactly issue_type and description"
            )
        for key in _TEXT_TO_SQL_ISSUE_FIELDS:
            if not isinstance(issue[key], str) or not issue[key].strip():
                raise TypeError(f"{item_name}.{key} must be a non-empty string")


def _validate_text_to_sql_capability_error(value: Any, *, field_name: str) -> None:
    if not isinstance(value, dict) or set(value) != _TEXT_TO_SQL_CAPABILITY_ERROR_FIELDS:
        raise ValueError(
            f"{field_name} must contain exactly capability and reason_code"
        )
    for key in _TEXT_TO_SQL_CAPABILITY_ERROR_FIELDS:
        if not isinstance(value[key], str) or not value[key].strip():
            raise TypeError(f"{field_name}.{key} must be a non-empty string")


def _validate_text_to_sql_pre_execution_error_code(
    value: Any, *, field_name: str
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty string")


def _validate_text_to_sql_rows(
    data: list[Any],
    columns: list[str],
    *,
    applied_row_limit: int,
) -> None:
    if len(data) > applied_row_limit:
        raise ValueError("execution.data exceeds execution.applied_row_limit")
    for index, row in enumerate(data):
        if isinstance(row, list):
            if len(row) != len(columns):
                raise ValueError(
                    f"execution.data[{index}] does not match execution.columns length"
                )
        elif isinstance(row, dict):
            if len(row) != len(columns) or set(row) != set(columns):
                raise ValueError(
                    f"execution.data[{index}] does not match execution.columns names"
                )
        else:
            raise TypeError(f"execution.data[{index}] must be a list or object")


def _validate_text_to_sql_explain_result(
    value: Any,
    *,
    execution: Mapping[str, Any],
) -> None:
    if not isinstance(value, dict) or set(value) != _TEXT_TO_SQL_EXPLAIN_FIELDS:
        raise ValueError(
            "execution.explain_result must contain exactly plan, estimated_cost, "
            "rows_to_scan, and issues"
        )
    plan = value["plan"]
    if not isinstance(plan, str) or not plan.strip():
        raise TypeError("execution.explain_result.plan must be a non-empty string")
    estimated_cost = value["estimated_cost"]
    if estimated_cost is not None:
        if type(estimated_cost) not in {int, float}:
            raise TypeError(
                "execution.explain_result.estimated_cost must be a non-negative number or null"
            )
        if estimated_cost < 0 or (
            isinstance(estimated_cost, float) and not math.isfinite(estimated_cost)
        ):
            raise ValueError(
                "execution.explain_result.estimated_cost must be a non-negative finite number"
            )
    rows_to_scan = value["rows_to_scan"]
    if rows_to_scan is not None and (
        type(rows_to_scan) is not int or rows_to_scan < 0
    ):
        raise TypeError(
            "execution.explain_result.rows_to_scan must be a non-negative integer or null"
        )
    _validate_text_to_sql_issues(
        value["issues"],
        field_name="execution.explain_result.issues",
    )
    if execution["success"] is not True:
        raise ValueError("execution.explain_result requires successful execution")
    if not _json_values_equal(execution["data"], [[plan]]):
        raise ValueError("execution.explain_result.plan must match execution.data")
    if execution["columns"] != ["Plan"] or execution["rows_affected"] != 1:
        raise ValueError(
            "execution.explain_result requires columns=['Plan'] and rows_affected=1"
        )


def text_to_sql_executor_contract_error(
    result: Any,
    *,
    expected_dry_run_only: Optional[bool],
    expected_sql_query: str,
    expected_row_limit: int,
) -> Optional[str]:
    """Return one neutral executor-envelope contract error, or ``None``."""
    try:
        preflight_text_to_sql_json_value(
            result,
            field_name="executor result",
        )
        if type(result) is not dict:
            return (
                f"executor returned {text_to_sql_type_name(result)}, "
                "expected object"
            )
        fields = set(result)
        missing = _TEXT_TO_SQL_EXECUTOR_BOUNDARY_REQUIRED_FIELDS - fields
        if missing:
            return "executor result missing fields: " + ", ".join(sorted(missing))
        unknown = fields - (
            _TEXT_TO_SQL_EXECUTOR_BOUNDARY_REQUIRED_FIELDS
            | _TEXT_TO_SQL_EXECUTOR_BOUNDARY_OPTIONAL_FIELDS
        )
        if unknown:
            return "executor result has unknown fields: " + ", ".join(
                sorted(unknown)
            )
        _validate_text_to_sql_enforcement_modes(
            result,
            field_name="executor",
        )

        returned_sql = result["sql_query"]
        if not isinstance(returned_sql, str):
            return "executor field 'sql_query' must be a string"
        if returned_sql != expected_sql_query:
            return "executor field 'sql_query' does not match requested SQL"
        applied_row_limit = result["applied_row_limit"]
        pre_execution_error_code = result.get("pre_execution_error_code")
        if applied_row_limit is None:
            try:
                _validate_text_to_sql_pre_execution_error_code(
                    pre_execution_error_code,
                    field_name="executor field 'pre_execution_error_code'",
                )
            except (TypeError, ValueError):
                return "executor field 'applied_row_limit' must be a positive integer"
            if result["success"] is not False:
                return "executor field 'applied_row_limit' must be a positive integer"
        else:
            if type(applied_row_limit) is not int or applied_row_limit <= 0:
                return "executor field 'applied_row_limit' must be a positive integer"
            if applied_row_limit != expected_row_limit:
                return (
                    "executor field 'applied_row_limit' does not match requested row limit"
                )
        for field_name in ("success", "dry_run_only", "skipped_execution"):
            if type(result[field_name]) is not bool:
                return f"executor field {field_name!r} must be a boolean"
        if result["dry_run_only"] and not result["skipped_execution"]:
            return "executor dry-run result must skip execution"
        if (
            result["success"]
            and result["skipped_execution"]
            and not result["dry_run_only"]
        ):
            return "successful non-dry-run executor result cannot skip execution"
        if (
            expected_dry_run_only is not None
            and result["dry_run_only"] is not expected_dry_run_only
        ):
            return (
                "executor field 'dry_run_only' does not match effective "
                f"dry_run_only={str(expected_dry_run_only).lower()}"
            )

        data = result["data"]
        columns = result["columns"]
        if not isinstance(data, list):
            return "executor field 'data' must be a list"
        if not isinstance(columns, list) or not all(
            isinstance(column, str) for column in columns
        ):
            return "executor field 'columns' must be a list of strings"
        if applied_row_limit is not None and len(data) > applied_row_limit:
            return "executor returned more rows than the applied row limit"
        for row_index, row in enumerate(data):
            if isinstance(row, list):
                if len(row) != len(columns):
                    return (
                        f"executor row {row_index} does not match the column count"
                    )
                continue
            if isinstance(row, dict):
                if len(row) != len(columns) or set(row) != set(columns):
                    return (
                        f"executor row {row_index} does not match the column names"
                    )
                continue
            return f"executor row {row_index} must be an array or object"

        rows_affected = result["rows_affected"]
        if type(rows_affected) is not int or rows_affected < 0:
            return "executor field 'rows_affected' must be a non-negative integer"
        execution_time_ms = result["execution_time_ms"]
        if type(execution_time_ms) is not int or execution_time_ms < 0:
            return (
                "executor field 'execution_time_ms' must be a non-negative integer"
            )
        error_message = result["error_message"]
        if error_message is not None and not isinstance(error_message, str):
            return "executor field 'error_message' must be a string or null"
        if result["success"]:
            if error_message not in {None, ""}:
                return "successful executor result cannot contain error_message"
        else:
            if not isinstance(error_message, str) or not error_message.strip():
                return "failed executor result requires a non-empty error_message"
            if len(error_message) > TEXT_TO_SQL_MAX_ERROR_LENGTH:
                return (
                    "failed executor error_message exceeds "
                    f"{TEXT_TO_SQL_MAX_ERROR_LENGTH} characters"
                )
            if data or columns or rows_affected != 0:
                return "failed executor result cannot contain row data"
        if result["dry_run_only"] and (data or columns or rows_affected != 0):
            return "dry-run executor result cannot contain row data"

        policy_fields = ("profile_name", "policy_version")
        if any(field in result for field in policy_fields):
            if not all(field in result for field in policy_fields):
                return "executor policy identity requires profile_name and policy_version"
            for field in policy_fields:
                if not isinstance(result[field], str) or not result[field].strip():
                    return f"executor field {field!r} must be a non-empty string"

        if "pre_execution_error_code" in result:
            try:
                _validate_text_to_sql_pre_execution_error_code(
                    pre_execution_error_code,
                    field_name="executor field 'pre_execution_error_code'",
                )
            except (TypeError, ValueError) as exc:
                return bound_text_to_sql_error(exc)
        if "capability_error" in result:
            try:
                _validate_text_to_sql_capability_error(
                    result["capability_error"],
                    field_name="executor field 'capability_error'",
                )
            except (TypeError, ValueError) as exc:
                return bound_text_to_sql_error(exc)

        safety_issues = result.get("safety_issues", [])
        try:
            _validate_text_to_sql_issues(
                safety_issues,
                field_name="executor safety_issues",
            )
        except (TypeError, ValueError) as exc:
            return bound_text_to_sql_error(exc)
        if result["success"] and safety_issues:
            return "successful executor result cannot contain safety_issues"
        if "explain_result" in result:
            try:
                _validate_text_to_sql_explain_result(
                    result["explain_result"],
                    execution=result,
                )
            except (TypeError, ValueError) as exc:
                return bound_text_to_sql_error(exc)
        return None
    except BaseException as exc:
        detail = bound_text_to_sql_error(exc)
        return (
            "executor contract validation failed"
            + (f": {detail}" if detail else "")
        )


@dataclass(frozen=True)
class TextToSqlTerminalResult:
    """Strict, JSON-serializable proof of the Text-to-SQL runtime gates."""

    run_id: str
    status: TextToSqlTerminalStatus
    reason_code: str
    sql: str
    generated: bool
    approved: bool
    executed: bool
    dry_run: bool
    audited: bool
    data: Sequence[Any]
    columns: Sequence[str]
    rows_affected: int
    error: Optional[str]
    execution: Mapping[str, Any]
    audit: Mapping[str, Any]
    persistence: Mapping[str, Any]

    _FIELDS = frozenset({
        "run_id",
        "status",
        "reason_code",
        "sql",
        "generated",
        "approved",
        "executed",
        "dry_run",
        "audited",
        "data",
        "columns",
        "rows_affected",
        "error",
        "execution",
        "audit",
        "persistence",
    })

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise TypeError("run_id must be a non-empty string")
        if not isinstance(self.status, TextToSqlTerminalStatus):
            raise TypeError("status must be a TextToSqlTerminalStatus")
        if not isinstance(self.reason_code, str):
            raise TypeError("reason_code must be a string")
        if not isinstance(self.sql, str):
            raise TypeError("sql must be a string")
        for field_name in (
            "generated",
            "approved",
            "executed",
            "dry_run",
            "audited",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be a boolean")
        if not isinstance(self.data, (list, _FrozenJsonList)):
            raise TypeError("data must be a list")
        if not isinstance(self.columns, (list, _FrozenJsonList)) or not all(
            isinstance(column, str) for column in self.columns
        ):
            raise TypeError("columns must be a list of strings")
        if type(self.rows_affected) is not int or self.rows_affected < 0:
            raise TypeError("rows_affected must be a non-negative integer")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("error must be a string or null")
        for field_name in ("execution", "audit", "persistence"):
            if not isinstance(getattr(self, field_name), Mapping):
                raise TypeError(f"{field_name} must be an object")
        evidence = _isolated_json_value(
            {
                "data": self.data,
                "columns": self.columns,
                "execution": self.execution,
                "audit": self.audit,
                "persistence": self.persistence,
            },
            field_name="terminal evidence",
        )
        for field_name in ("data", "columns", "execution", "audit", "persistence"):
            object.__setattr__(self, field_name, evidence[field_name])
        self.assert_invariants()
        for field_name in ("data", "columns", "execution", "audit", "persistence"):
            object.__setattr__(
                self,
                field_name,
                _freeze_json_value(getattr(self, field_name)),
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TextToSqlTerminalResult":
        if not isinstance(value, Mapping):
            raise TypeError("Text-to-SQL terminal result must be an object")
        value = normalize_text_to_sql_json_value(
            value,
            field_name="terminal result",
        )
        keys = set(value)
        missing = cls._FIELDS - keys
        unknown = keys - cls._FIELDS
        if missing:
            raise ValueError(f"Text-to-SQL terminal result missing fields: {sorted(missing)}")
        if unknown:
            raise ValueError(f"Text-to-SQL terminal result has unknown fields: {sorted(unknown)}")

        raw_status = value["status"]
        if not isinstance(raw_status, str):
            raise TypeError("status must be a string")
        try:
            status = TextToSqlTerminalStatus(raw_status)
        except ValueError as exc:
            raise ValueError(f"Unsupported Text-to-SQL terminal status: {raw_status!r}") from exc

        return cls(
            run_id=value["run_id"],
            status=status,
            reason_code=value["reason_code"],
            sql=value["sql"],
            generated=value["generated"],
            approved=value["approved"],
            executed=value["executed"],
            dry_run=value["dry_run"],
            audited=value["audited"],
            data=list(value["data"]) if isinstance(value["data"], list) else value["data"],
            columns=(
                list(value["columns"])
                if isinstance(value["columns"], list)
                else value["columns"]
            ),
            rows_affected=value["rows_affected"],
            error=value["error"],
            execution=(
                dict(value["execution"])
                if isinstance(value["execution"], dict)
                else value["execution"]
            ),
            audit=dict(value["audit"]) if isinstance(value["audit"], dict) else value["audit"],
            persistence=(
                dict(value["persistence"])
                if isinstance(value["persistence"], dict)
                else value["persistence"]
            ),
        )

    def assert_invariants(self) -> None:
        data = _thaw_json_value(self.data)
        columns = _thaw_json_value(self.columns)
        execution = _thaw_json_value(self.execution)
        audit = _thaw_json_value(self.audit)
        persistence = _thaw_json_value(self.persistence)
        reason: Optional[TextToSqlTerminalReasonCode] = None

        if self.error is not None and len(self.error) > TEXT_TO_SQL_MAX_ERROR_LENGTH:
            raise ValueError(
                f"error must not exceed {TEXT_TO_SQL_MAX_ERROR_LENGTH} characters"
            )
        if self.status is TextToSqlTerminalStatus.SUCCEEDED:
            if self.reason_code:
                raise ValueError("SUCCEEDED requires an empty reason_code")
        else:
            if not self.reason_code.strip():
                raise ValueError(f"{self.status.value} requires a reason_code")
            try:
                reason = TextToSqlTerminalReasonCode(self.reason_code)
            except ValueError as exc:
                raise ValueError(
                    f"Unsupported Text-to-SQL reason_code: {self.reason_code!r}"
                ) from exc
            status_reasons = {
                TextToSqlTerminalStatus.ABSTAINED: _TEXT_TO_SQL_ABSTAIN_REASONS,
                TextToSqlTerminalStatus.CANCELLED: frozenset({
                    TextToSqlTerminalReasonCode.CANCELLED
                }),
                TextToSqlTerminalStatus.TIMED_OUT: frozenset({
                    TextToSqlTerminalReasonCode.TIMED_OUT
                }),
            }
            expected_reasons = status_reasons.get(self.status)
            if expected_reasons is not None and reason not in expected_reasons:
                raise ValueError(
                    f"{self.status.value} reason_code must be one of "
                    + ", ".join(sorted(item.value for item in expected_reasons))
                )
            non_failed_reasons = {
                item
                for allowed in status_reasons.values()
                for item in allowed
            }
            if (
                self.status is TextToSqlTerminalStatus.FAILED
                and reason in non_failed_reasons
            ):
                raise ValueError(
                    f"failed reason_code cannot be {reason.value}"
                )
        if self.dry_run and self.executed:
            raise ValueError("dry_run=true requires executed=false")
        if self.generated is not bool(self.sql.strip()):
            raise ValueError("generated must equal bool(sql.strip())")
        if self.approved and not self.generated:
            raise ValueError("approved=true requires generated=true")
        if (self.executed or self.dry_run or self.audited) and not (
            self.generated and self.approved
        ):
            raise ValueError(
                "runtime evidence requires generated=true and approved=true"
            )

        evidence_statuses = {
            TextToSqlTerminalStatus.SUCCEEDED,
            TextToSqlTerminalStatus.FAILED,
        }
        if self.status not in evidence_statuses and (execution or audit):
            raise ValueError(
                f"{self.status.value} cannot contain execution or audit evidence"
            )

        trusted_execution = bool(execution) and (
            self.reason_code != TextToSqlTerminalReasonCode.EXECUTOR_CONTRACT_INVALID.value
        )
        if (data or columns or self.rows_affected) and not trusted_execution:
            raise ValueError(
                "result data requires trusted execution evidence"
            )

        if execution:
            if self.reason_code == "EXECUTOR_CONTRACT_INVALID":
                if (
                    self.executed
                    or self.dry_run
                    or data
                    or columns
                    or self.rows_affected
                ):
                    raise ValueError(
                        "EXECUTOR_CONTRACT_INVALID cannot claim trusted execution results"
                    )
            else:
                missing = _TEXT_TO_SQL_EXECUTION_REQUIRED_FIELDS - set(execution)
                if missing:
                    raise ValueError(
                        "execution evidence missing fields: " + ", ".join(sorted(missing))
                    )
                unknown = set(execution) - (
                    _TEXT_TO_SQL_EXECUTION_REQUIRED_FIELDS
                    | _TEXT_TO_SQL_EXECUTION_OPTIONAL_FIELDS
                )
                if unknown:
                    raise ValueError(
                        "execution evidence has unknown fields: "
                        + ", ".join(sorted(unknown))
                    )
                _validate_text_to_sql_enforcement_modes(
                    execution,
                    field_name="execution",
                )
                if type(execution["success"]) is not bool:
                    raise TypeError("execution.success must be a boolean")
                if not isinstance(execution["data"], list):
                    raise TypeError("execution.data must be a list")
                if not isinstance(execution["columns"], list) or not all(
                    isinstance(column, str) for column in execution["columns"]
                ):
                    raise TypeError("execution.columns must be a list of strings")
                nested_rows = execution["rows_affected"]
                if type(nested_rows) is not int or nested_rows < 0:
                    raise TypeError(
                        "execution.rows_affected must be a non-negative integer"
                    )
                execution_time_ms = execution["execution_time_ms"]
                if type(execution_time_ms) is not int or execution_time_ms < 0:
                    raise TypeError(
                        "execution.execution_time_ms must be a non-negative integer"
                    )
                sql_query = execution["sql_query"]
                if not isinstance(sql_query, str):
                    raise TypeError("execution.sql_query must be a string")
                if sql_query != self.sql:
                    raise ValueError("execution.sql_query must match top-level sql")
                applied_row_limit = execution["applied_row_limit"]
                pre_execution_error_code = execution.get("pre_execution_error_code")
                if applied_row_limit is None:
                    try:
                        _validate_text_to_sql_pre_execution_error_code(
                            pre_execution_error_code,
                            field_name="execution.pre_execution_error_code",
                        )
                    except (TypeError, ValueError):
                        raise TypeError(
                            "execution.applied_row_limit must be a positive integer"
                        ) from None
                    if execution["success"] is not False:
                        raise TypeError(
                            "execution.applied_row_limit must be a positive integer"
                        )
                else:
                    if type(applied_row_limit) is not int or applied_row_limit <= 0:
                        raise TypeError(
                            "execution.applied_row_limit must be a positive integer"
                        )
                    _validate_text_to_sql_rows(
                        execution["data"],
                        execution["columns"],
                        applied_row_limit=applied_row_limit,
                    )
                if not _json_values_equal(execution["data"], data):
                    raise ValueError("execution.data must match top-level data")
                if not _json_values_equal(execution["columns"], columns):
                    raise ValueError("execution.columns must match top-level columns")
                if nested_rows != self.rows_affected:
                    raise ValueError(
                        "execution.rows_affected must match top-level rows_affected"
                    )
                for marker in ("dry_run_only", "skipped_execution"):
                    if type(execution[marker]) is not bool:
                        raise TypeError(f"execution.{marker} must be a boolean")
                if execution["dry_run_only"] is not self.dry_run:
                    raise ValueError(
                        "execution.dry_run_only must match top-level dry_run"
                    )
                expected_executed = (
                    not execution["dry_run_only"]
                    and not execution["skipped_execution"]
                )
                if self.executed is not expected_executed:
                    raise ValueError(
                        "executed must reflect a non-dry-run executor attempt"
                    )
                if execution["success"] and not (self.executed or self.dry_run):
                    raise ValueError(
                        "execution.success=true requires executed=true or dry_run=true"
                    )
                execution_error = execution.get("error_message")
                if execution["success"]:
                    if execution_error is not None and execution_error != "":
                        raise ValueError(
                            "successful execution cannot contain error_message"
                        )
                else:
                    if not isinstance(execution_error, str) or not execution_error.strip():
                        raise ValueError(
                            "failed execution requires a non-empty error_message"
                        )
                    if len(execution_error) > TEXT_TO_SQL_MAX_ERROR_LENGTH:
                        raise ValueError(
                            "execution.error_message must not exceed "
                            f"{TEXT_TO_SQL_MAX_ERROR_LENGTH} characters"
                        )
                    if (
                        execution["data"]
                        or execution["columns"]
                        or nested_rows != 0
                    ):
                        raise ValueError(
                            "failed execution cannot contain row data"
                        )
                safety_issues = execution.get("safety_issues", [])
                _validate_text_to_sql_issues(
                    safety_issues,
                    field_name="execution.safety_issues",
                )
                if execution["success"] and safety_issues:
                    raise ValueError(
                        "successful execution cannot contain safety_issues"
                    )
                policy_fields = ("profile_name", "policy_version")
                if any(field in execution for field in policy_fields):
                    if not all(field in execution for field in policy_fields):
                        raise ValueError(
                            "execution policy identity requires profile_name and policy_version"
                        )
                    for field in policy_fields:
                        if (
                            not isinstance(execution[field], str)
                            or not execution[field].strip()
                        ):
                            raise TypeError(
                                f"execution.{field} must be a non-empty string"
                            )
                if "pre_execution_error_code" in execution:
                    _validate_text_to_sql_pre_execution_error_code(
                        pre_execution_error_code,
                        field_name="execution.pre_execution_error_code",
                    )
                if "capability_error" in execution:
                    _validate_text_to_sql_capability_error(
                        execution["capability_error"],
                        field_name="execution.capability_error",
                    )
                if "explain_result" in execution:
                    _validate_text_to_sql_explain_result(
                        execution["explain_result"],
                        execution=execution,
                    )
        elif self.executed or self.dry_run:
            raise ValueError(
                "executed or dry_run terminal state requires execution evidence"
            )

        if audit:
            audit_status = audit.get("status")
            if audit_status not in {"logged", "error"}:
                raise ValueError("audit.status must be exactly logged or error")
            if audit_status == "logged":
                if set(audit) != {"status", "log_id"}:
                    raise ValueError(
                        "audit.status=logged requires exactly status and log_id"
                    )
                log_id = audit["log_id"]
                if not isinstance(log_id, str) or not log_id.strip():
                    raise ValueError("audit.log_id must be a non-empty string")
                if not self.audited:
                    raise ValueError(
                        "audit.status=logged requires top-level audited=true"
                    )
            else:
                if set(audit) != {"status", "error"}:
                    raise ValueError(
                        "audit.status=error requires exactly status and error"
                    )
                audit_error = audit["error"]
                if self.audited:
                    raise ValueError(
                        "audit.status=error requires top-level audited=false"
                    )
                if not isinstance(audit_error, str) or not audit_error.strip():
                    raise ValueError(
                        "audit.status=error requires a non-empty audit.error"
                    )
                if len(audit_error) > TEXT_TO_SQL_MAX_ERROR_LENGTH:
                    raise ValueError(
                        "audit.error must not exceed "
                        f"{TEXT_TO_SQL_MAX_ERROR_LENGTH} characters"
                    )
        elif self.audited:
            raise ValueError("audited=true requires audit evidence")

        persistence_status = persistence.get("status")
        if persistence_status == "not_attempted":
            if set(persistence) != {"status"}:
                raise ValueError(
                    "persistence.status=not_attempted requires exactly status"
                )
        elif persistence_status == "saved":
            if set(persistence) != {"status", "filename", "path"}:
                raise ValueError(
                    "persistence.status=saved requires exactly status, filename, and path"
                )
            for field_name in ("filename", "path"):
                field_value = persistence[field_name]
                if not isinstance(field_value, str) or not field_value.strip():
                    raise ValueError(
                        f"persistence.{field_name} must be a non-empty string"
                    )
        elif persistence_status == "error":
            if set(persistence) != {"status", "error"}:
                raise ValueError(
                    "persistence.status=error requires exactly status and error"
                )
            persistence_error = persistence["error"]
            if not isinstance(persistence_error, str) or not persistence_error.strip():
                raise ValueError(
                    "persistence.status=error requires a non-empty error"
                )
            if len(persistence_error) > TEXT_TO_SQL_MAX_ERROR_LENGTH:
                raise ValueError(
                    "persistence.error must not exceed "
                    f"{TEXT_TO_SQL_MAX_ERROR_LENGTH} characters"
                )
        else:
            raise ValueError(
                "persistence.status must be exactly not_attempted, saved, or error"
            )

        if persistence_status == "saved" and (
            not self.executed
            or not trusted_execution
            or execution.get("success") is not True
        ):
            raise ValueError(
                "persistence.status=saved requires successful execution"
            )
        if self.status is TextToSqlTerminalStatus.SUCCEEDED:
            if self.dry_run and persistence_status != "not_attempted":
                raise ValueError(
                    "SUCCEEDED dry-run requires persistence.status=not_attempted"
                )
            if self.executed and persistence_status not in {"saved", "error"}:
                raise ValueError(
                    "SUCCEEDED execution requires persistence.status=saved or error"
                )
        elif self.status in {
            TextToSqlTerminalStatus.ABSTAINED,
            TextToSqlTerminalStatus.CANCELLED,
            TextToSqlTerminalStatus.TIMED_OUT,
        } and persistence_status != "not_attempted":
            raise ValueError(
                f"{self.status.value} requires persistence.status=not_attempted"
            )

        if self.status is TextToSqlTerminalStatus.SUCCEEDED:
            if not (self.generated and self.approved and self.audited):
                raise ValueError(
                    "SUCCEEDED requires generated=true, approved=true, and audited=true"
                )
            if self.executed == self.dry_run:
                raise ValueError(
                    "SUCCEEDED requires exactly one of executed=true or dry_run=true"
                )
            if self.error:
                raise ValueError("SUCCEEDED cannot contain an error")
            if execution.get("success") is not True:
                raise ValueError("SUCCEEDED requires execution.success=true")
            return

        if self.status is TextToSqlTerminalStatus.ABSTAINED:
            if self.executed or self.dry_run or self.audited:
                raise ValueError(
                    "ABSTAINED cannot claim execution, dry-run, or audit completion"
                )
        elif self.status in {
            TextToSqlTerminalStatus.CANCELLED,
            TextToSqlTerminalStatus.TIMED_OUT,
        }:
            if self.executed or self.dry_run or self.audited:
                raise ValueError(
                    f"{self.status.value} cannot contain successful runtime state"
                )

        if self.status is TextToSqlTerminalStatus.FAILED:
            if reason is None:
                raise ValueError("FAILED requires a recognized reason_code")
            self._assert_failure_evidence_profile(
                reason=reason,
                data=data,
                columns=columns,
                execution=execution,
                audit=audit,
                persistence=persistence,
            )

    def _assert_failure_evidence_profile(
        self,
        *,
        reason: TextToSqlTerminalReasonCode,
        data: list[Any],
        columns: list[str],
        execution: Dict[str, Any],
        audit: Dict[str, Any],
        persistence: Dict[str, Any],
    ) -> None:
        profile = _TEXT_TO_SQL_FAILURE_EVIDENCE_PROFILES[reason]
        persistence_status = persistence["status"]

        if profile == "no_runtime":
            if (
                self.executed
                or self.dry_run
                or self.audited
                or data
                or columns
                or self.rows_affected
                or execution
                or audit
                or persistence_status != "not_attempted"
            ):
                raise ValueError(
                    f"{reason.value} cannot contain runtime evidence"
                )
            return

        if profile == "untrusted_executor":
            if (
                not execution
                or self.executed
                or self.dry_run
                or data
                or columns
                or self.rows_affected
                or persistence_status != "not_attempted"
            ):
                raise ValueError(
                    "EXECUTOR_CONTRACT_INVALID requires only untrusted executor evidence"
                )
            return

        if profile == "execution_failure":
            if (
                not execution
                or execution.get("success") is not False
                or not self.audited
                or audit.get("status") != "logged"
                or persistence_status != "not_attempted"
            ):
                raise ValueError(
                    f"{reason.value} requires failed execution and logged audit evidence"
                )
            return

        if profile == "audit_failure":
            if (
                not execution
                or self.audited
                or audit.get("status") != "error"
                or persistence_status != "not_attempted"
            ):
                raise ValueError(
                    f"{reason.value} requires trusted execution and failed audit evidence"
                )
            return

        if profile == "persistence_contract_failure":
            if (
                not self.executed
                or self.dry_run
                or execution.get("success") is not True
                or not self.audited
                or audit.get("status") != "logged"
                or persistence_status != "error"
            ):
                raise ValueError(
                    "PERSISTENCE_CONTRACT_INVALID requires successful execution, "
                    "logged audit, and failed persistence evidence"
                )
            return

        if profile == "post_success":
            if (
                execution.get("success") is not True
                or self.executed == self.dry_run
                or not self.audited
                or audit.get("status") != "logged"
                or (
                    self.dry_run
                    and persistence_status != "not_attempted"
                )
                or (
                    self.executed
                    and persistence_status not in {"saved", "error"}
                )
            ):
                raise ValueError(
                    "RESULT_AGGREGATION_FAILED must preserve successful terminal evidence"
                )
            return

        if profile == "result_failure":
            if persistence_status != "error":
                raise ValueError(
                    f"{reason.value} requires persistence.status=error"
                )
            return

        raise RuntimeError(f"Unsupported Text-to-SQL evidence profile: {profile}")

    def to_mapping(self) -> Dict[str, Any]:
        return _isolated_json_value({
            "run_id": self.run_id,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "sql": self.sql,
            "generated": self.generated,
            "approved": self.approved,
            "executed": self.executed,
            "dry_run": self.dry_run,
            "audited": self.audited,
            "data": _thaw_json_value(self.data),
            "columns": _thaw_json_value(self.columns),
            "rows_affected": self.rows_affected,
            "error": self.error,
            "execution": _thaw_json_value(self.execution),
            "audit": _thaw_json_value(self.audit),
            "persistence": _thaw_json_value(self.persistence),
        }, field_name="terminal_result")


# Public alias for the top-level required field names of a Text-to-SQL
# terminal result, derived from the dataclass' own field set so consumers
# (e.g. the Streamlit client's shape-check) never duplicate the literal list.
TEXT_TO_SQL_TERMINAL_REQUIRED_FIELDS = TextToSqlTerminalResult._FIELDS
