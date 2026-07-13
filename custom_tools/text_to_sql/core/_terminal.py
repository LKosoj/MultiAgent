"""Deterministic Text-to-SQL execution, audit, and persistence finalizer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from time import monotonic_ns
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from ..validators import TextToSqlSafetyPolicy

from workflow.deadline import WorkflowDeadlineExceeded
from workflow.models import (
    TextToSqlTerminalResult,
    TextToSqlTerminalStatus,
    TextToSqlVerificationStatus,
    bound_text_to_sql_error,
    normalize_text_to_sql_json_value,
    text_to_sql_executor_contract_error,
    text_to_sql_type_name,
)

_SUMMARY_ERROR_LIMIT = 512

# Compatibility name retained for focused contract tests; the implementation
# lives in workflow.models and is shared with the trusted executor wrapper.
_executor_contract_error = text_to_sql_executor_contract_error
def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty string")
    return value


def _not_attempted() -> Dict[str, str]:
    return {"status": "not_attempted"}


def _content_fingerprint(value: str) -> Dict[str, Any]:
    return {
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "length": len(value),
    }


def _execution_summary(
    *,
    execution: Dict[str, Any],
    contract_error: Optional[str],
    executed: bool,
    dry_run: bool,
    row_limit: int,
    run_id: str,
    session_id: str,
    sql_query: str,
    user_query: str,
) -> Dict[str, Any]:
    data = execution.get("data")
    rows_affected = execution.get("rows_affected")
    duration_ms = execution.get("execution_time_ms")
    success = execution.get("success") is True and contract_error is None
    error = contract_error or execution.get("error_message")
    sql_fingerprint = _content_fingerprint(sql_query)
    query_fingerprint = _content_fingerprint(user_query)
    return {
        "status": "succeeded" if success else "failed",
        "success": success,
        "dry_run": dry_run,
        "executed": executed,
        "row_count": len(data) if isinstance(data, list) else 0,
        "row_limit": row_limit,
        "rows_affected": (
            rows_affected if type(rows_affected) is int and rows_affected >= 0 else 0
        ),
        "execution_time_ms": (
            duration_ms
            if type(duration_ms) is int and duration_ms >= 0
            else None
        ),
        "error": (
            bound_text_to_sql_error(error)[:_SUMMARY_ERROR_LIMIT]
            if error is not None
            else None
        ),
        "run_id": run_id,
        "session_id": session_id,
        "sql_sha256": sql_fingerprint["sha256"],
        "sql_length": sql_fingerprint["length"],
        "query_sha256": query_fingerprint["sha256"],
        "query_length": query_fingerprint["length"],
    }


def _terminal_mapping(
    *,
    run_id: str,
    status: TextToSqlTerminalStatus,
    reason_code: str,
    sql: str,
    generated: bool,
    approved: bool,
    executed: bool,
    dry_run: bool,
    audited: bool,
    data: list,
    columns: list[str],
    rows_affected: int,
    error: Optional[str],
    execution: Dict[str, Any],
    audit: Dict[str, Any],
    persistence: Dict[str, Any],
) -> Dict[str, Any]:
    outcome = TextToSqlTerminalResult(
        run_id=run_id,
        status=status,
        reason_code=reason_code,
        sql=sql,
        generated=generated,
        approved=approved,
        executed=executed,
        dry_run=dry_run,
        audited=audited,
        data=data,
        columns=columns,
        rows_affected=rows_affected,
        error=(bound_text_to_sql_error(error) if error is not None else None),
        execution=execution,
        audit=audit,
        persistence=persistence,
    )
    outcome.assert_invariants()
    return outcome.to_mapping()


def _audit_execution(
    *,
    core,
    execution_summary: Dict[str, Any],
) -> tuple[Dict[str, Any], Optional[str]]:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "text_to_sql_terminal_execution",
        "run_id": execution_summary["run_id"],
        "session_id": execution_summary["session_id"],
        "execution": execution_summary,
    }
    try:
        result = core.audit_logger(entry)
    except Exception as exc:
        error = bound_text_to_sql_error(exc)
        return (
            {
                "status": "error",
                "error": error if error.strip() else "audit logger failed",
            },
            None,
        )
    if type(result) is not dict:
        return _adapter_contract_failure(
            "audit",
            "audit logger returned "
            f"{text_to_sql_type_name(result)}, expected object",
        )
    try:
        normalized = normalize_text_to_sql_json_value(
            result,
            field_name="audit adapter result",
        )
    except (TypeError, ValueError) as exc:
        return _adapter_contract_failure("audit", exc)

    status = normalized.get("status")
    if status == "logged":
        if set(normalized) != {"status", "log_id"}:
            return _adapter_contract_failure(
                "audit",
                "status=logged requires exactly status and log_id",
            )
        log_id = normalized["log_id"]
        if not isinstance(log_id, str) or not log_id.strip():
            return _adapter_contract_failure(
                "audit",
                "status=logged requires a non-empty log_id",
            )
        return {"status": "logged", "log_id": log_id}, None
    if status == "error":
        allowed_keys = {"status", "error"}
        # The underlying file logger historically includes its attempted log_id
        # on operational write errors. It is intentionally omitted from the
        # closed terminal schema.
        normalized_keys = frozenset(normalized)
        if normalized_keys not in {
            frozenset(allowed_keys),
            frozenset({*allowed_keys, "log_id"}),
        }:
            return _adapter_contract_failure(
                "audit",
                "status=error requires exactly status and error",
            )
        error = normalized.get("error")
        if isinstance(error, str) and error.strip():
            return {
                "status": "error",
                "error": bound_text_to_sql_error(error),
            }, None
        return _adapter_contract_failure(
            "audit",
            "status=error requires a non-empty error",
        )
    return _adapter_contract_failure(
        "audit",
        f"audit logger returned unsupported status {status!r}",
    )


def _adapter_contract_failure(
    adapter_name: str,
    detail: Any,
) -> tuple[Dict[str, Any], str]:
    detail_text = bound_text_to_sql_error(detail)
    error = bound_text_to_sql_error(
        f"{adapter_name} adapter contract invalid: {detail_text}"
    )
    if not error.strip():
        error = f"{adapter_name} adapter contract invalid"
    return {"status": "error", "error": error}, error


def _adapt_persistence_result(
    result: Any,
) -> tuple[Dict[str, Any], Optional[str]]:
    if type(result) is not dict:
        return _adapter_contract_failure(
            "persistence",
            "persistence returned "
            f"{text_to_sql_type_name(result)}, expected object",
        )
    try:
        normalized = normalize_text_to_sql_json_value(
            result,
            field_name="persistence adapter result",
        )
    except (TypeError, ValueError) as exc:
        return _adapter_contract_failure("persistence", exc)

    status = normalized.get("status")
    if status in {"saved", "exists"}:
        if set(normalized) != {"status", "filename", "path"}:
            return _adapter_contract_failure(
                "persistence",
                f"status={status} requires exactly status, filename, and path",
            )
        filename = normalized["filename"]
        path = normalized["path"]
        if not isinstance(filename, str) or not filename.strip():
            return _adapter_contract_failure(
                "persistence",
                f"status={status} requires a non-empty filename",
            )
        if not isinstance(path, str) or not path.strip():
            return _adapter_contract_failure(
                "persistence",
                f"status={status} requires a non-empty path",
            )
        return {
            "status": "saved",
            "filename": filename,
            "path": path,
        }, None
    if status == "error":
        if set(normalized) != {"status", "error"}:
            return _adapter_contract_failure(
                "persistence",
                "status=error requires exactly status and error",
            )
        error = normalized["error"]
        if not isinstance(error, str) or not error.strip():
            return _adapter_contract_failure(
                "persistence",
                "status=error requires a non-empty error",
            )
        return {
            "status": "error",
            "error": bound_text_to_sql_error(error),
        }, None
    return _adapter_contract_failure(
        "persistence",
        f"persistence returned unsupported status {status!r}",
    )


def _normalize_execution_evidence(
    result: Any,
) -> tuple[Dict[str, Any], Optional[str]]:
    try:
        normalized = normalize_text_to_sql_json_value(
            result,
            field_name="executor result",
        )
    except Exception as exc:
        error = bound_text_to_sql_error(exc)
        return {
            "invalid_result_type": text_to_sql_type_name(result),
            "normalization_error": error,
        }, f"executor result is not finite JSON: {error}"
    if type(normalized) is dict:
        return normalized, None
    type_name = text_to_sql_type_name(normalized)
    error = f"executor returned {type_name}, expected object"
    return {"invalid_result_type": type_name}, error


def finalize_text_to_sql_run(
    sql_query: str,
    user_query: str,
    verification_status: str,
    dsn: str,
    row_limit: int,
    dry_run_only: bool,
    session_id: str,
    run_id: str,
    *,
    safety_policy: Optional["TextToSqlSafetyPolicy"] = None,
    namespace_version_key: str | None = None,
) -> Dict[str, Any]:
    """Finalize one approved generated query through deterministic runtime gates."""

    sql_query = _require_non_empty_string(sql_query, "sql_query")
    user_query = _require_non_empty_string(user_query, "user_query")
    dsn = _require_non_empty_string(dsn, "dsn")
    session_id = _require_non_empty_string(session_id, "session_id")
    run_id = _require_non_empty_string(run_id, "run_id")
    if namespace_version_key is not None:
        from ..successful_sql_memory import validate_namespace_version_key

        namespace_version_key = validate_namespace_version_key(
            namespace_version_key
        )
    if type(row_limit) is not int or row_limit <= 0:
        raise TypeError("row_limit must be a positive integer")
    if type(dry_run_only) is not bool:
        raise TypeError("dry_run_only must be a boolean")
    if safety_policy is not None:
        from ..validators import TextToSqlSafetyPolicy

        if not isinstance(safety_policy, TextToSqlSafetyPolicy):
            raise TypeError("safety_policy must be a TextToSqlSafetyPolicy or null")

    try:
        verifier_status = TextToSqlVerificationStatus(verification_status)
    except (TypeError, ValueError):
        return _terminal_mapping(
            run_id=run_id,
            status=TextToSqlTerminalStatus.FAILED,
            reason_code="VERIFIER_CONTRACT_INVALID",
            sql=sql_query,
            generated=True,
            approved=False,
            executed=False,
            dry_run=False,
            audited=False,
            data=[],
            columns=[],
            rows_affected=0,
            error=(
                "verification_status must be exactly 'Approved' or 'Rejected'"
            ),
            execution={},
            audit={},
            persistence=_not_attempted(),
        )

    if verifier_status is TextToSqlVerificationStatus.REJECTED:
        return _terminal_mapping(
            run_id=run_id,
            status=TextToSqlTerminalStatus.ABSTAINED,
            reason_code="VERIFIER_REJECTED",
            sql=sql_query,
            generated=True,
            approved=False,
            executed=False,
            dry_run=False,
            audited=False,
            data=[],
            columns=[],
            rows_affected=0,
            error=None,
            execution={},
            audit={},
            persistence=_not_attempted(),
        )

    from custom_tools.text_to_sql import core
    from ..utils import is_dry_run_only

    effective_dry_run_only = is_dry_run_only(payload_flag=dry_run_only)

    execution_started_ns = monotonic_ns()
    try:
        executor_kwargs = {
            "row_limit": row_limit,
            "dsn": dsn,
            "dry_run_only": effective_dry_run_only,
        }
        if safety_policy is not None:
            executor_kwargs["safety_policy"] = safety_policy
        raw_execution = core.secure_db_executor(sql_query, **executor_kwargs)
    except WorkflowDeadlineExceeded:
        raise
    except Exception as exc:
        elapsed_ms = max(0, monotonic_ns() - execution_started_ns) // 1_000_000
        raw_execution = {
            "success": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": elapsed_ms,
            "error_message": bound_text_to_sql_error(exc) or "database execution failed",
            "dry_run_only": effective_dry_run_only,
            "skipped_execution": True,
            "sql_query": sql_query,
            "applied_row_limit": row_limit,
        }

    execution, normalization_error = _normalize_execution_evidence(raw_execution)
    contract_error = normalization_error or text_to_sql_executor_contract_error(
        execution,
        expected_dry_run_only=effective_dry_run_only,
        expected_sql_query=sql_query,
        expected_row_limit=row_limit,
    )
    dry_run = bool(execution.get("dry_run_only")) if contract_error is None else False
    executed = (
        not execution["skipped_execution"] and not dry_run
        if contract_error is None
        else False
    )
    data = list(execution["data"]) if contract_error is None else []
    columns = list(execution["columns"]) if contract_error is None else []
    rows_affected = execution["rows_affected"] if contract_error is None else 0

    execution_summary = _execution_summary(
        execution=execution,
        contract_error=contract_error,
        executed=executed,
        dry_run=dry_run,
        row_limit=row_limit,
        run_id=run_id,
        session_id=session_id,
        sql_query=sql_query,
        user_query=user_query,
    )

    audit, audit_contract_error = _audit_execution(
        core=core,
        execution_summary=execution_summary,
    )
    audited = audit.get("status") == "logged"

    if audit_contract_error is not None and contract_error is None:
        return _terminal_mapping(
            run_id=run_id,
            status=TextToSqlTerminalStatus.FAILED,
            reason_code="AUDIT_CONTRACT_INVALID",
            sql=sql_query,
            generated=True,
            approved=True,
            executed=executed,
            dry_run=dry_run,
            audited=False,
            data=data,
            columns=columns,
            rows_affected=rows_affected,
            error=audit_contract_error,
            execution=execution,
            audit=audit,
            persistence=_not_attempted(),
        )

    if contract_error is not None:
        return _terminal_mapping(
            run_id=run_id,
            status=TextToSqlTerminalStatus.FAILED,
            reason_code="EXECUTOR_CONTRACT_INVALID",
            sql=sql_query,
            generated=True,
            approved=True,
            executed=False,
            dry_run=False,
            audited=audited,
            data=[],
            columns=[],
            rows_affected=0,
            error=contract_error,
            execution=execution,
            audit=audit,
            persistence=_not_attempted(),
        )

    if not audited:
        error = audit.get("error")
        return _terminal_mapping(
            run_id=run_id,
            status=TextToSqlTerminalStatus.FAILED,
            reason_code="AUDIT_FAILED",
            sql=sql_query,
            generated=True,
            approved=True,
            executed=executed,
            dry_run=dry_run,
            audited=False,
            data=data,
            columns=columns,
            rows_affected=rows_affected,
            error=bound_text_to_sql_error(error or "audit was not logged"),
            execution=execution,
            audit=audit,
            persistence=_not_attempted(),
        )

    if not execution["success"]:
        return _terminal_mapping(
            run_id=run_id,
            status=TextToSqlTerminalStatus.FAILED,
            reason_code="EXECUTION_FAILED",
            sql=sql_query,
            generated=True,
            approved=True,
            executed=executed,
            dry_run=dry_run,
            audited=True,
            data=data,
            columns=columns,
            rows_affected=rows_affected,
            error=execution.get("error_message") or "execution failed",
            execution=execution,
            audit=audit,
            persistence=_not_attempted(),
        )

    persistence: Dict[str, Any] = _not_attempted()
    persistence_contract_error: Optional[str] = None
    if executed:
        try:
            persistence_kwargs = {
                "sql_query": sql_query,
                "user_query": user_query,
                "execution_result": json.dumps(
                    execution_summary,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "dsn": dsn,
            }
            if namespace_version_key is not None:
                persistence_kwargs.update(
                    {
                        "namespace_version_key": namespace_version_key,
                        "run_id": run_id,
                        "approved": True,
                        "executed": True,
                        "execution_success": True,
                        "audited": True,
                    }
                )
            raw_persistence = core.save_successful_sql(
                **persistence_kwargs,
            )
            persistence, persistence_contract_error = _adapt_persistence_result(
                raw_persistence
            )
        except Exception as exc:
            persistence = {
                "status": "error",
                "error": bound_text_to_sql_error(exc) or "persistence failed",
            }

    if persistence_contract_error is not None:
        return _terminal_mapping(
            run_id=run_id,
            status=TextToSqlTerminalStatus.FAILED,
            reason_code="PERSISTENCE_CONTRACT_INVALID",
            sql=sql_query,
            generated=True,
            approved=True,
            executed=executed,
            dry_run=dry_run,
            audited=True,
            data=data,
            columns=columns,
            rows_affected=rows_affected,
            error=persistence_contract_error,
            execution=execution,
            audit=audit,
            persistence=persistence,
        )

    return _terminal_mapping(
        run_id=run_id,
        status=TextToSqlTerminalStatus.SUCCEEDED,
        reason_code="",
        sql=sql_query,
        generated=True,
        approved=True,
        executed=executed,
        dry_run=dry_run,
        audited=True,
        data=data,
        columns=columns,
        rows_affected=rows_affected,
        error=None,
        execution=execution,
        audit=audit,
        persistence=persistence,
    )
