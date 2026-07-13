"""Opt-in deterministic filter value grounding for schema-linked filters."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from typing import Any

from workflow.deadline import WorkflowDeadlineExceeded

from .core._db_exec import (
    QueryExecutionRequest,
    QueryExecutor,
    QueryPurpose,
)
from .redaction import redact_text


_TRUE_VALUES = {"1", "true", "yes", "on"}
_LABEL_COLUMN_HINTS = (
    "name",
    "title",
    "label",
    "description",
    "full_name",
    "short_name",
    "display_name",
    "region",
    "city",
    "municipality",
)


def value_grounding_enabled(value: bool | None = None) -> bool:
    if value is not None:
        if isinstance(value, str):
            return value.strip().casefold() in _TRUE_VALUES
        return bool(value)
    return os.getenv("SCHEMA_LINKING_VALUE_GROUNDING", "0").strip().casefold() in _TRUE_VALUES


def _int_env(name: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except ValueError:
        value = default
    return max(min_value, min(max_value, value))


def _limits() -> dict[str, int]:
    return {
        "lookup_limit": _int_env("SCHEMA_LINKING_VALUE_GROUNDING_LIMIT", 20, min_value=1, max_value=200),
        "max_filters": _int_env("SCHEMA_LINKING_VALUE_GROUNDING_MAX_FILTERS", 5, min_value=1, max_value=20),
        "max_label_columns": _int_env("SCHEMA_LINKING_VALUE_GROUNDING_MAX_LABEL_COLUMNS", 4, min_value=1, max_value=20),
        "max_distinct_scan": _int_env("SCHEMA_LINKING_VALUE_GROUNDING_MAX_DISTINCT_SCAN", 200, min_value=1, max_value=1000),
    }


def ground_linked_filter_values(
    linking_result: dict[str, Any],
    *,
    original_entities: dict[str, Any],
    db_schema: dict[str, Any],
    dsn: str | None,
    value_grounding: bool | None = None,
) -> dict[str, Any]:
    """Return a copy of schema-linking result with grounded filter values.

    The function is fail-open: any lookup failure preserves the original filter
    value and records compact metadata on that filter.
    """
    if not value_grounding_enabled(value_grounding):
        return linking_result
    result = deepcopy(linking_result)

    linked = result.get("linked_entities")
    filters = linked.get("filters") if isinstance(linked, dict) else None
    if not isinstance(filters, dict) or not filters:
        return result
    if not dsn:
        _mark_all(filters, "skipped_no_dsn")
        return result

    raw_filters = original_entities.get("filters", {}) if isinstance(original_entities, dict) else {}
    limits = _limits()
    for idx, (filter_name, filter_info) in enumerate(filters.items()):
        if idx >= limits["max_filters"]:
            if isinstance(filter_info, dict):
                filter_info["value_grounding"] = {"status": "skipped_filter_cap"}
            continue
        if not isinstance(filter_info, dict):
            continue
        raw_value = _raw_filter_value(filter_name, raw_filters, filter_info)
        try:
            filters[filter_name] = _ground_filter(
                filter_info,
                raw_value=raw_value,
                db_schema=db_schema,
                dsn=dsn,
                limits=limits,
            )
        except WorkflowDeadlineExceeded:
            raise
        except Exception as exc:
            preserved = dict(filter_info)
            preserved["value_grounding"] = {
                "status": "lookup_error",
                "error": redact_text(str(exc)),
            }
            filters[filter_name] = preserved
    return result


def _mark_all(filters: dict[str, Any], status: str) -> None:
    for filter_info in filters.values():
        if isinstance(filter_info, dict):
            filter_info["value_grounding"] = {"status": status}


def _raw_filter_value(filter_name: str, raw_filters: Any, filter_info: dict[str, Any]) -> Any:
    if isinstance(raw_filters, dict) and filter_name in raw_filters:
        raw = raw_filters[filter_name]
        if isinstance(raw, dict) and "value" in raw:
            return raw["value"]
        return raw
    return filter_info.get("value")


def _ground_filter(
    filter_info: dict[str, Any],
    *,
    raw_value: Any,
    db_schema: dict[str, Any],
    dsn: str,
    limits: dict[str, int],
) -> dict[str, Any]:
    grounded = dict(filter_info)
    if _is_date_range(raw_value) or _is_date_range(filter_info.get("value")):
        grounded["value_grounding"] = {"status": "skipped_date_range"}
        return grounded
    if raw_value is None or isinstance(raw_value, (dict, list, tuple, set)):
        grounded["value_grounding"] = {"status": "skipped_unsupported_value"}
        return grounded

    table = str(filter_info.get("table") or "").strip()
    column = str(filter_info.get("column") or "").strip()
    if not table or not column:
        grounded["value_grounding"] = {"status": "skipped_unlinked_filter"}
        return grounded

    same_column = _lookup_same_column_value(dsn, table, column, raw_value, limits)
    if same_column["status"] == "matched":
        grounded["value"] = same_column["value"]
        grounded["value_grounding"] = {"status": "matched_same_column", "source": table}
        return grounded
    if same_column["status"] == "ambiguous":
        grounded["value_grounding"] = {"status": "ambiguous_same_column"}
        return grounded

    fuzzy = _lookup_same_column_fuzzy(dsn, table, column, raw_value, limits)
    if fuzzy["status"] == "matched":
        grounded["value"] = fuzzy["value"]
        grounded["value_grounding"] = {"status": "matched_same_column_fuzzy", "source": table}
        return grounded
    if fuzzy["status"] == "ambiguous":
        grounded["value_grounding"] = {"status": "ambiguous_same_column_fuzzy"}
        return grounded

    if _is_code_like_column(column, _column_meta(db_schema, table, column)):
        mapped = _lookup_code_via_label_column(dsn, table, column, raw_value, db_schema, limits)
        if mapped["status"] == "matched":
            grounded["value"] = mapped["value"]
            grounded["value_grounding"] = {
                "status": "matched_label_to_code",
                "source": mapped["source"],
                "label_column": mapped["label_column"],
            }
            return grounded
        if mapped["status"] == "ambiguous":
            grounded["value_grounding"] = {"status": "ambiguous_label_to_code"}
            return grounded

    grounded["value_grounding"] = {"status": "no_match"}
    return grounded


def _lookup_same_column_value(
    dsn: str,
    table: str,
    column: str,
    value: Any,
    limits: dict[str, int],
) -> dict[str, Any]:
    rows = _execute_lookup(
        dsn,
        lambda plugin: plugin.build_values_membership_query(table, column, [value]),
        row_limit=limits["lookup_limit"],
    )
    values = _unique_values(_row_value(row, 0) for row in rows)
    if len(values) == 1:
        return {"status": "matched", "value": values[0]}
    if len(values) > 1:
        return {"status": "ambiguous"}
    return {"status": "no_match"}


def _lookup_same_column_fuzzy(
    dsn: str,
    table: str,
    column: str,
    value: Any,
    limits: dict[str, int],
) -> dict[str, Any]:
    rows = _execute_lookup(
        dsn,
        lambda plugin: plugin.build_distinct_values_query(table, column, limits["max_distinct_scan"]),
        row_limit=limits["max_distinct_scan"],
    )
    target = _normalize_match_text(value)
    matches = [
        _row_value(row, 0)
        for row in rows
        if _normalize_match_text(_row_value(row, 0)) == target
    ]
    values = _unique_values(matches)
    if len(values) == 1:
        return {"status": "matched", "value": values[0]}
    if len(values) > 1:
        return {"status": "ambiguous"}
    return {"status": "no_match"}


def _lookup_code_via_label_column(
    dsn: str,
    table: str,
    column: str,
    value: Any,
    db_schema: dict[str, Any],
    limits: dict[str, int],
) -> dict[str, Any]:
    targets: list[tuple[str, str, str]] = []
    for label_column in _candidate_label_columns(db_schema, table, exclude=column, limit=limits["max_label_columns"]):
        targets.append((table, label_column, column))

    ref_table, ref_column = _referenced_table_column(db_schema, table, column)
    if ref_table:
        return_column = ref_column or _default_return_column(db_schema, ref_table)
        if return_column:
            for label_column in _candidate_label_columns(
                db_schema,
                ref_table,
                exclude=return_column,
                limit=limits["max_label_columns"],
            ):
                targets.append((ref_table, label_column, return_column))

    for lookup_table, label_column, return_column in targets:
        exact = _lookup_label_to_code(
            dsn, lookup_table, label_column, return_column, value, limits, fuzzy=False
        )
        if exact["status"] == "matched":
            exact["source"] = lookup_table
            exact["label_column"] = label_column
            return exact
        if exact["status"] == "ambiguous":
            return exact

        fuzzy = _lookup_label_to_code(
            dsn, lookup_table, label_column, return_column, value, limits, fuzzy=True
        )
        if fuzzy["status"] == "matched":
            fuzzy["source"] = lookup_table
            fuzzy["label_column"] = label_column
            return fuzzy
        if fuzzy["status"] == "ambiguous":
            return fuzzy
    return {"status": "no_match"}


def _lookup_label_to_code(
    dsn: str,
    table: str,
    label_column: str,
    return_column: str,
    value: Any,
    limits: dict[str, int],
    *,
    fuzzy: bool,
) -> dict[str, Any]:
    if not fuzzy:
        rows = _execute_lookup(
            dsn,
            lambda plugin: plugin.build_lookup_values_query(table, label_column, return_column, [value]),
            row_limit=limits["lookup_limit"],
        )
    else:
        rows = _execute_lookup(
            dsn,
            lambda plugin: plugin.build_distinct_values_query(table, label_column, limits["max_distinct_scan"]),
            row_limit=limits["max_distinct_scan"],
        )
        labels = [
            _row_value(row, 0)
            for row in rows
            if _normalize_match_text(_row_value(row, 0)) == _normalize_match_text(value)
        ]
        if len(_unique_values(labels)) != 1:
            return {"status": "ambiguous" if labels else "no_match"}
        rows = _execute_lookup(
            dsn,
            lambda plugin: plugin.build_lookup_values_query(table, label_column, return_column, [labels[0]]),
            row_limit=limits["lookup_limit"],
        )

    values = _unique_values(_row_value(row, 1) for row in rows)
    if len(values) == 1:
        return {"status": "matched", "value": values[0]}
    if len(values) > 1:
        return {"status": "ambiguous"}
    return {"status": "no_match"}


def _execute_lookup(dsn: str, build_sql, *, row_limit: int) -> list[Any]:
    from db_plugins import get_plugin

    plugin = get_plugin(dsn)
    sql = build_sql(plugin)
    result = QueryExecutor(get_plugin=get_plugin).execute(
        QueryExecutionRequest(
            sql_query=sql,
            purpose=QueryPurpose.GROUNDING,
            row_limit=row_limit,
            dsn=dsn,
        )
    )
    if not result.success:
        raise RuntimeError(result.error_message or "lookup query failed")
    return result.data


def _candidate_label_columns(
    db_schema: dict[str, Any],
    table: str,
    *,
    exclude: str,
    limit: int,
) -> list[str]:
    table_info = _table_info(db_schema, table)
    columns = table_info.get("columns", {}) if isinstance(table_info, dict) else {}
    candidates: list[tuple[int, str]] = []
    for column_name, meta in columns.items():
        if str(column_name).casefold() == exclude.casefold():
            continue
        if not _is_text_column(meta):
            continue
        lower = str(column_name).casefold()
        rank = next((idx for idx, hint in enumerate(_LABEL_COLUMN_HINTS) if hint in lower), 999)
        if rank < 999:
            candidates.append((rank, str(column_name)))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [column for _, column in candidates[:limit]]


def _referenced_table_column(
    db_schema: dict[str, Any],
    table: str,
    column: str,
) -> tuple[str | None, str | None]:
    meta = _column_meta(db_schema, table, column)
    ref = meta.get("references") if isinstance(meta, dict) else None
    if not isinstance(ref, str) or not ref.strip():
        return None, None
    text = ref.strip()
    if "(" in text and text.endswith(")"):
        table_part, column_part = text.split("(", 1)
        return table_part.strip(), column_part.rstrip(")").strip() or None
    if "." in text:
        ref_table, ref_column = text.rsplit(".", 1)
        return ref_table.strip(), ref_column.strip() or None
    return text, None


def _default_return_column(db_schema: dict[str, Any], table: str) -> str | None:
    columns = (_table_info(db_schema, table) or {}).get("columns", {})
    if not isinstance(columns, dict):
        return None
    for candidate in ("id", "code"):
        if candidate in columns:
            return candidate
    for column_name in columns:
        if _is_code_like_column(str(column_name), columns.get(column_name)):
            return str(column_name)
    return None


def _table_info(db_schema: dict[str, Any], table: str) -> dict[str, Any]:
    if table in db_schema and isinstance(db_schema[table], dict):
        return db_schema[table]
    suffix_matches = [
        info
        for name, info in db_schema.items()
        if str(name).split(".")[-1].casefold() == table.split(".")[-1].casefold()
        and isinstance(info, dict)
    ]
    return suffix_matches[0] if len(suffix_matches) == 1 else {}


def _column_meta(db_schema: dict[str, Any], table: str, column: str) -> dict[str, Any]:
    columns = (_table_info(db_schema, table) or {}).get("columns", {})
    if not isinstance(columns, dict):
        return {}
    if column in columns and isinstance(columns[column], dict):
        return columns[column]
    for name, meta in columns.items():
        if str(name).casefold() == column.casefold() and isinstance(meta, dict):
            return meta
    return {}


def _is_code_like_column(column: str, meta: dict[str, Any] | None = None) -> bool:
    lower = column.casefold()
    constraint = str((meta or {}).get("constraint_type") or "").casefold()
    return (
        lower in {"id", "code", "uuid", "guid"}
        or lower.endswith(("_id", "_code", "_key"))
        or constraint in {"pk", "fk", "primary key", "foreign key"}
        or bool((meta or {}).get("references"))
    )


def _is_text_column(meta: Any) -> bool:
    sql_type = str(meta.get("type", "") if isinstance(meta, dict) else meta).casefold()
    return any(token in sql_type for token in ("char", "text", "string", "varchar", "nvarchar"))


def _is_date_range(value: Any) -> bool:
    return isinstance(value, dict) and ("start" in value or "end" in value)


def _row_value(row: Any, index: int) -> Any:
    if isinstance(row, dict):
        values = list(row.values())
        return values[index] if index < len(values) else None
    return row[index] if index < len(row) else None


def _unique_values(values: Any) -> list[Any]:
    unique: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = repr(value)
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def _normalize_match_text(value: Any) -> str:
    text = str(value).casefold().strip()
    text = re.sub(r"\s+", " ", text)
    return text
