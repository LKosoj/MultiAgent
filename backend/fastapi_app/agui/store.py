"""SQLite-backed event store for AG-UI replay support."""

from __future__ import annotations

import json
import hashlib
import math
import posixpath
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from .auth import Principal, principal_from_record, principal_to_record
from workflow.result_identity import (
    parse_workflow_result_event_key,
    require_canonical_nonblank,
    workflow_result_event_key as workflow_result_event_key,
    workflow_result_identity_from_payload,
)
from workflow.sqlite_optimizer_stats import (
    discard_validated_sqlite_optimizer_statistics,
    normalize_sqlite_schema_sql,
    validate_sqlite_optimizer_statistics,
)
from workflow.state_files import acquire_sqlite_bundle, release_sqlite_bundle
from workflow.models import TextToSqlTerminalResult, TextToSqlTerminalStatus


AGUI_EVENT_STORE_SCHEMA_VERSION = 8
WORKFLOW_RUN_SPEC_VERSION = 1
WORKFLOW_RUN_SPEC_MAX_BYTES = 1024 * 1024

_NON_TERMINAL_RUN_STATUSES = frozenset(
    {"pending", "queued", "running", "result_pending"}
)
_TERMINAL_RUN_STATUSES = frozenset(
    {"succeeded", "abstained", "failed", "cancelled", "timed_out"}
)
_RUN_KINDS = frozenset({"agui", "text_to_sql", "legacy"})


class TerminalWorkflowResultConflictError(ValueError):
    """A terminal Text-to-SQL lifecycle already has another exact winner."""


class WorkflowSupervisorOwnershipConflictError(ValueError):
    """A supervisor terminalization no longer owns the durable lifecycle."""


class WorkflowAdmissionRejected(RuntimeError):
    """The durable workflow queue has reached its configured limit."""


class TextToSqlHistoryConflictError(ValueError):
    """A history key already contains a different terminal projection."""


class LegacyTextToSqlHistoryImportConflictError(RuntimeError):
    """A completed legacy source identity was reused for different content."""


class OperationalRetentionLeaseConflictError(RuntimeError):
    """A stale retention worker no longer owns the persisted lease."""


_NO_SUPERVISOR_PRECONDITION = object()
_NO_ATTEMPT_GENERATION_PRECONDITION = object()

_EVENT_STORE_TABLE_SIGNATURES = {
    "agui_events": (
        ("id", "INTEGER", 0, 1, 0),
        ("run_id", "TEXT", 1, 0, 0),
        ("seq", "INTEGER", 1, 0, 0),
        ("event_type", "TEXT", 1, 0, 0),
        ("payload", "TEXT", 1, 0, 0),
        ("created_at_ms", "INTEGER", 1, 0, 0),
        ("run_incarnation", "TEXT", 0, 0, 0),
        ("event_key", "TEXT", 0, 0, 0),
    ),
    "agui_runs": (
        ("run_id", "TEXT", 0, 1, 0),
        ("thread_id", "TEXT", 1, 0, 0),
        ("owner_subject", "TEXT", 1, 0, 0),
        ("tenant_id", "TEXT", 1, 0, 0),
        ("roles", "TEXT", 1, 0, 0),
        ("created_at_ms", "INTEGER", 1, 0, 0),
        ("run_kind", "TEXT", 1, 0, 0),
        ("status", "TEXT", 1, 0, 0),
        ("terminal_reason", "TEXT", 0, 0, 0),
        ("worker_pid", "INTEGER", 0, 0, 0),
        ("updated_at_ms", "INTEGER", 1, 0, 0),
        ("finished_at_ms", "INTEGER", 0, 0, 0),
        ("result_seq", "INTEGER", 0, 0, 0),
        ("idempotency_key", "TEXT", 0, 0, 0),
        ("request_fingerprint", "TEXT", 0, 0, 0),
        ("queued_at_ms", "INTEGER", 0, 0, 0),
        ("deadline_at_ms", "INTEGER", 0, 0, 0),
        ("supervisor_id", "TEXT", 0, 0, 0),
        ("worker_lease_expires_at_ms", "INTEGER", 0, 0, 0),
        ("worker_heartbeat_at_ms", "INTEGER", 0, 0, 0),
        ("cancel_requested_at_ms", "INTEGER", 0, 0, 0),
        ("attempt_started_at_ms", "INTEGER", 0, 0, 0),
        ("worker_pid_started_at_ms", "INTEGER", 0, 0, 0),
    ),
    "workflow_run_invocations": (
        ("run_id", "TEXT", 0, 1, 0),
        ("run_incarnation", "TEXT", 1, 0, 0),
        ("session_id", "TEXT", 1, 0, 0),
        ("workflow_name", "TEXT", 1, 0, 0),
        ("created_at_ms", "INTEGER", 1, 0, 0),
    ),
    "workflow_run_specs": (
        ("run_id", "TEXT", 0, 1, 0),
        ("spec_version", "INTEGER", 1, 0, 0),
        ("spec_json", "TEXT", 1, 0, 0),
        ("spec_sha256", "TEXT", 1, 0, 0),
        ("created_at_ms", "INTEGER", 1, 0, 0),
    ),
    "text_to_sql_history": (
        ("tenant_id", "TEXT", 1, 1, 0),
        ("owner_subject", "TEXT", 1, 2, 0),
        ("run_id", "TEXT", 1, 3, 0),
        ("created_at_ms", "INTEGER", 1, 0, 0),
        ("status", "TEXT", 1, 0, 0),
        ("dialect", "TEXT", 1, 0, 0),
        ("profile_name", "TEXT", 1, 0, 0),
        ("terminal_snapshot_json", "TEXT", 1, 0, 0),
    ),
    "text_to_sql_history_quarantine": (
        ("id", "INTEGER", 0, 1, 0),
        ("reason", "TEXT", 1, 0, 0),
        ("raw_payload", "TEXT", 1, 0, 0),
        ("quarantined_at_ms", "INTEGER", 1, 0, 0),
    ),
    "text_to_sql_history_imports": (
        ("source_identity", "TEXT", 0, 1, 0),
        ("source_path", "TEXT", 1, 0, 0),
        ("content_sha256", "TEXT", 1, 0, 0),
        ("completed_at_ms", "INTEGER", 1, 0, 0),
        ("imported", "INTEGER", 1, 0, 0),
        ("duplicates", "INTEGER", 1, 0, 0),
        ("quarantined_malformed", "INTEGER", 1, 0, 0),
        ("quarantined_unowned", "INTEGER", 1, 0, 0),
        ("quarantined_invalid", "INTEGER", 1, 0, 0),
    ),
    "operational_retention_state": (
        ("scope", "TEXT", 0, 1, 0),
        ("lease_owner", "TEXT", 0, 0, 0),
        ("lease_generation", "INTEGER", 1, 0, 0),
        ("lease_expires_at_ms", "INTEGER", 0, 0, 0),
        ("last_attempt_at_ms", "INTEGER", 0, 0, 0),
        ("last_success_at_ms", "INTEGER", 0, 0, 0),
        ("next_due_at_ms", "INTEGER", 1, 0, 0),
        ("status", "TEXT", 1, 0, 0),
        ("counters_json", "TEXT", 1, 0, 0),
        ("last_error", "TEXT", 0, 0, 0),
        ("updated_at_ms", "INTEGER", 1, 0, 0),
    ),
}

_EVENT_STORE_COLUMN_DEFAULTS = {
    "agui_events": {},
    "agui_runs": {
        "run_kind": "'legacy'",
        "status": "'legacy'",
        "updated_at_ms": "0",
    },
    "workflow_run_invocations": {},
    "workflow_run_specs": {},
    "text_to_sql_history": {},
    "text_to_sql_history_quarantine": {},
    "text_to_sql_history_imports": {},
    "operational_retention_state": {
        "lease_generation": "0",
        "next_due_at_ms": "0",
        "status": "'never'",
        "counters_json": "'{}'",
        "updated_at_ms": "0",
    },
}

_EVENT_STORE_NAMED_INDEXES = {
    "idx_agui_events_run_seq": (
        "agui_events",
        1,
        "c",
        0,
        (
            (0, 1, "run_id", 0, "BINARY", 1),
            (1, 2, "seq", 0, "BINARY", 1),
            (2, -1, None, 0, "BINARY", 0),
        ),
        (
            "CREATE UNIQUE INDEX IDX_AGUI_EVENTS_RUN_SEQ "
            "ON AGUI_EVENTS(RUN_ID, SEQ)"
        ),
    ),
    "idx_agui_events_event_key": (
        "agui_events",
        1,
        "c",
        1,
        (
            (0, 7, "event_key", 0, "BINARY", 1),
            (1, -1, None, 0, "BINARY", 0),
        ),
        (
            "CREATE UNIQUE INDEX IDX_AGUI_EVENTS_EVENT_KEY "
            "ON AGUI_EVENTS(EVENT_KEY) WHERE EVENT_KEY IS NOT NULL"
        ),
    ),
    "idx_agui_runs_owner_idempotency": (
        "agui_runs",
        1,
        "c",
        1,
        (
            (0, 3, "tenant_id", 0, "BINARY", 1),
            (1, 2, "owner_subject", 0, "BINARY", 1),
            (2, 6, "run_kind", 0, "BINARY", 1),
            (3, 13, "idempotency_key", 0, "BINARY", 1),
            (4, -1, None, 0, "BINARY", 0),
        ),
        (
            "CREATE UNIQUE INDEX IDX_AGUI_RUNS_OWNER_IDEMPOTENCY "
            "ON AGUI_RUNS(TENANT_ID, OWNER_SUBJECT, RUN_KIND, IDEMPOTENCY_KEY) "
            "WHERE IDEMPOTENCY_KEY IS NOT NULL"
        ),
    ),
    "idx_agui_runs_queue": (
        "agui_runs",
        0,
        "c",
        1,
        (
            (0, 7, "status", 0, "BINARY", 1),
            (1, 15, "queued_at_ms", 0, "BINARY", 1),
            (2, 5, "created_at_ms", 0, "BINARY", 1),
            (3, 0, "run_id", 0, "BINARY", 1),
            (4, -1, None, 0, "BINARY", 0),
        ),
        (
            "CREATE INDEX IDX_AGUI_RUNS_QUEUE ON AGUI_RUNS"
            "(STATUS, QUEUED_AT_MS, CREATED_AT_MS, RUN_ID) "
            "WHERE STATUS = 'queued'"
        ),
    ),
    "idx_agui_runs_active_leases": (
        "agui_runs",
        0,
        "c",
        1,
        (
            (0, 7, "status", 0, "BINARY", 1),
            (1, 18, "worker_lease_expires_at_ms", 0, "BINARY", 1),
            (2, 3, "tenant_id", 0, "BINARY", 1),
            (3, 2, "owner_subject", 0, "BINARY", 1),
            (4, -1, None, 0, "BINARY", 0),
        ),
        (
            "CREATE INDEX IDX_AGUI_RUNS_ACTIVE_LEASES ON AGUI_RUNS"
            "(STATUS, WORKER_LEASE_EXPIRES_AT_MS, TENANT_ID, OWNER_SUBJECT) "
            "WHERE STATUS = 'running'"
        ),
    ),
    "idx_text_to_sql_history_owner_created": (
        "text_to_sql_history",
        0,
        "c",
        0,
        (
            (0, 0, "tenant_id", 0, "BINARY", 1),
            (1, 1, "owner_subject", 0, "BINARY", 1),
            (2, 3, "created_at_ms", 1, "BINARY", 1),
            (3, 2, "run_id", 1, "BINARY", 1),
            (4, -1, None, 0, "BINARY", 0),
        ),
        (
            "CREATE INDEX IDX_TEXT_TO_SQL_HISTORY_OWNER_CREATED ON "
            "TEXT_TO_SQL_HISTORY(TENANT_ID, OWNER_SUBJECT, "
            "CREATED_AT_MS DESC, RUN_ID DESC)"
        ),
    ),
}

_EVENT_STORE_AUTOMATIC_UNIQUE_INDEXES = {
    "agui_events": (),
    "agui_runs": (
        (
            "pk",
            0,
            (
                (0, 0, "run_id", 0, "BINARY", 1),
                (1, -1, None, 0, "BINARY", 0),
            ),
        ),
    ),
    "workflow_run_invocations": (
        (
            "pk",
            0,
            (
                (0, 0, "run_id", 0, "BINARY", 1),
                (1, -1, None, 0, "BINARY", 0),
            ),
        ),
        (
            "u",
            0,
            (
                (0, 1, "run_incarnation", 0, "BINARY", 1),
                (1, -1, None, 0, "BINARY", 0),
            ),
        ),
    ),
    "workflow_run_specs": (
        (
            "pk",
            0,
            (
                (0, 0, "run_id", 0, "BINARY", 1),
                (1, -1, None, 0, "BINARY", 0),
            ),
        ),
    ),
    "text_to_sql_history": (
        (
            "pk",
            0,
            (
                (0, 0, "tenant_id", 0, "BINARY", 1),
                (1, 1, "owner_subject", 0, "BINARY", 1),
                (2, 2, "run_id", 0, "BINARY", 1),
                (3, -1, None, 0, "BINARY", 0),
            ),
        ),
    ),
    "text_to_sql_history_quarantine": (),
    "text_to_sql_history_imports": (
        (
            "pk",
            0,
            (
                (0, 0, "source_identity", 0, "BINARY", 1),
                (1, -1, None, 0, "BINARY", 0),
            ),
        ),
    ),
    "operational_retention_state": (
        (
            "pk",
            0,
            (
                (0, 0, "scope", 0, "BINARY", 1),
                (1, -1, None, 0, "BINARY", 0),
            ),
        ),
    ),
}

_EVENT_STORE_SQLITE_MASTER = {
    "agui_events": (
        "table",
        "agui_events",
        "CREATE TABLE AGUI_EVENTS ( ID INTEGER PRIMARY KEY AUTOINCREMENT, "
        "RUN_ID TEXT NOT NULL, SEQ INTEGER NOT NULL, EVENT_TYPE TEXT NOT NULL, "
        "PAYLOAD TEXT NOT NULL, CREATED_AT_MS INTEGER NOT NULL , "
        "RUN_INCARNATION TEXT, EVENT_KEY TEXT)",
    ),
    "agui_runs": (
        "table",
        "agui_runs",
        "CREATE TABLE AGUI_RUNS ( RUN_ID TEXT PRIMARY KEY, "
        "THREAD_ID TEXT NOT NULL, OWNER_SUBJECT TEXT NOT NULL, "
        "TENANT_ID TEXT NOT NULL, ROLES TEXT NOT NULL, "
        "CREATED_AT_MS INTEGER NOT NULL , "
        "RUN_KIND TEXT NOT NULL DEFAULT 'legacy', "
        "STATUS TEXT NOT NULL DEFAULT 'legacy', TERMINAL_REASON TEXT, "
        "WORKER_PID INTEGER, UPDATED_AT_MS INTEGER NOT NULL DEFAULT 0, "
        "FINISHED_AT_MS INTEGER, RESULT_SEQ INTEGER, "
        "IDEMPOTENCY_KEY TEXT, REQUEST_FINGERPRINT TEXT, "
        "QUEUED_AT_MS INTEGER, DEADLINE_AT_MS INTEGER, SUPERVISOR_ID TEXT, "
        "WORKER_LEASE_EXPIRES_AT_MS INTEGER, WORKER_HEARTBEAT_AT_MS INTEGER, "
        "CANCEL_REQUESTED_AT_MS INTEGER, ATTEMPT_STARTED_AT_MS INTEGER, "
        "WORKER_PID_STARTED_AT_MS INTEGER)",
    ),
    "workflow_run_invocations": (
        "table",
        "workflow_run_invocations",
        "CREATE TABLE WORKFLOW_RUN_INVOCATIONS ( RUN_ID TEXT PRIMARY KEY, "
        "RUN_INCARNATION TEXT NOT NULL UNIQUE, SESSION_ID TEXT NOT NULL, "
        "WORKFLOW_NAME TEXT NOT NULL, CREATED_AT_MS INTEGER NOT NULL )",
    ),
    "workflow_run_specs": (
        "table",
        "workflow_run_specs",
        "CREATE TABLE WORKFLOW_RUN_SPECS ( RUN_ID TEXT PRIMARY KEY, "
        "SPEC_VERSION INTEGER NOT NULL, SPEC_JSON TEXT NOT NULL, "
        "SPEC_SHA256 TEXT NOT NULL, CREATED_AT_MS INTEGER NOT NULL )",
    ),
    "text_to_sql_history": (
        "table",
        "text_to_sql_history",
        "CREATE TABLE TEXT_TO_SQL_HISTORY ( TENANT_ID TEXT NOT NULL, "
        "OWNER_SUBJECT TEXT NOT NULL, RUN_ID TEXT NOT NULL, "
        "CREATED_AT_MS INTEGER NOT NULL, STATUS TEXT NOT NULL, "
        "DIALECT TEXT NOT NULL, PROFILE_NAME TEXT NOT NULL, "
        "TERMINAL_SNAPSHOT_JSON TEXT NOT NULL, "
        "PRIMARY KEY (TENANT_ID, OWNER_SUBJECT, RUN_ID) )",
    ),
    "text_to_sql_history_quarantine": (
        "table",
        "text_to_sql_history_quarantine",
        "CREATE TABLE TEXT_TO_SQL_HISTORY_QUARANTINE ( "
        "ID INTEGER PRIMARY KEY AUTOINCREMENT, REASON TEXT NOT NULL, "
        "RAW_PAYLOAD TEXT NOT NULL, QUARANTINED_AT_MS INTEGER NOT NULL )",
    ),
    "text_to_sql_history_imports": (
        "table",
        "text_to_sql_history_imports",
        "CREATE TABLE TEXT_TO_SQL_HISTORY_IMPORTS ( "
        "SOURCE_IDENTITY TEXT PRIMARY KEY, SOURCE_PATH TEXT NOT NULL, "
        "CONTENT_SHA256 TEXT NOT NULL, COMPLETED_AT_MS INTEGER NOT NULL, "
        "IMPORTED INTEGER NOT NULL, DUPLICATES INTEGER NOT NULL, "
        "QUARANTINED_MALFORMED INTEGER NOT NULL, "
        "QUARANTINED_UNOWNED INTEGER NOT NULL, "
        "QUARANTINED_INVALID INTEGER NOT NULL )",
    ),
    "operational_retention_state": (
        "table",
        "operational_retention_state",
        "CREATE TABLE OPERATIONAL_RETENTION_STATE ( SCOPE TEXT PRIMARY KEY, "
        "LEASE_OWNER TEXT, LEASE_GENERATION INTEGER NOT NULL DEFAULT 0, "
        "LEASE_EXPIRES_AT_MS INTEGER, LAST_ATTEMPT_AT_MS INTEGER, "
        "LAST_SUCCESS_AT_MS INTEGER, NEXT_DUE_AT_MS INTEGER NOT NULL DEFAULT 0, "
        "STATUS TEXT NOT NULL DEFAULT 'never', "
        "COUNTERS_JSON TEXT NOT NULL DEFAULT '{}', LAST_ERROR TEXT, "
        "UPDATED_AT_MS INTEGER NOT NULL DEFAULT 0 )",
    ),
    "sqlite_sequence": (
        "table",
        "sqlite_sequence",
        "CREATE TABLE SQLITE_SEQUENCE(NAME,SEQ)",
    ),
    "idx_agui_events_run_seq": (
        "index",
        "agui_events",
        "CREATE UNIQUE INDEX IDX_AGUI_EVENTS_RUN_SEQ "
        "ON AGUI_EVENTS(RUN_ID, SEQ)",
    ),
    "idx_agui_events_event_key": (
        "index",
        "agui_events",
        "CREATE UNIQUE INDEX IDX_AGUI_EVENTS_EVENT_KEY "
        "ON AGUI_EVENTS(EVENT_KEY) WHERE EVENT_KEY IS NOT NULL",
    ),
    "idx_agui_runs_owner_idempotency": (
        "index",
        "agui_runs",
        "CREATE UNIQUE INDEX IDX_AGUI_RUNS_OWNER_IDEMPOTENCY "
        "ON AGUI_RUNS(TENANT_ID, OWNER_SUBJECT, RUN_KIND, IDEMPOTENCY_KEY) "
        "WHERE IDEMPOTENCY_KEY IS NOT NULL",
    ),
    "idx_agui_runs_queue": (
        "index",
        "agui_runs",
        "CREATE INDEX IDX_AGUI_RUNS_QUEUE ON AGUI_RUNS"
        "(STATUS, QUEUED_AT_MS, CREATED_AT_MS, RUN_ID) "
        "WHERE STATUS = 'queued'",
    ),
    "idx_agui_runs_active_leases": (
        "index",
        "agui_runs",
        "CREATE INDEX IDX_AGUI_RUNS_ACTIVE_LEASES ON AGUI_RUNS"
        "(STATUS, WORKER_LEASE_EXPIRES_AT_MS, TENANT_ID, OWNER_SUBJECT) "
        "WHERE STATUS = 'running'",
    ),
    "idx_text_to_sql_history_owner_created": (
        "index",
        "text_to_sql_history",
        "CREATE INDEX IDX_TEXT_TO_SQL_HISTORY_OWNER_CREATED ON "
        "TEXT_TO_SQL_HISTORY(TENANT_ID, OWNER_SUBJECT, "
        "CREATED_AT_MS DESC, RUN_ID DESC)",
    ),
    "sqlite_autoindex_agui_runs_1": ("index", "agui_runs", None),
    "sqlite_autoindex_workflow_run_invocations_1": (
        "index",
        "workflow_run_invocations",
        None,
    ),
    "sqlite_autoindex_workflow_run_invocations_2": (
        "index",
        "workflow_run_invocations",
        None,
    ),
    "sqlite_autoindex_workflow_run_specs_1": (
        "index",
        "workflow_run_specs",
        None,
    ),
    "sqlite_autoindex_text_to_sql_history_1": (
        "index",
        "text_to_sql_history",
        None,
    ),
    "sqlite_autoindex_text_to_sql_history_imports_1": (
        "index",
        "text_to_sql_history_imports",
        None,
    ),
    "sqlite_autoindex_operational_retention_state_1": (
        "index",
        "operational_retention_state",
        None,
    ),
}

def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _require_run_kind(value: Any) -> str:
    run_kind = require_canonical_nonblank(value, field_name="run_kind")
    if run_kind not in _RUN_KINDS:
        raise ValueError(f"unsupported run_kind: {run_kind}")
    return run_kind


def _require_run_status(value: Any, *, allow_legacy: bool = False) -> str:
    status = require_canonical_nonblank(value, field_name="status")
    allowed = _NON_TERMINAL_RUN_STATUSES | _TERMINAL_RUN_STATUSES
    if allow_legacy:
        allowed = allowed | {"legacy"}
    if status not in allowed:
        raise ValueError(f"unsupported run status: {status}")
    return status


def _require_idempotency_key(value: Any) -> str:
    key = require_canonical_nonblank(value, field_name="idempotency_key")
    if len(key) > 128 or not all(character.isprintable() for character in key):
        raise ValueError("idempotency_key must be at most 128 printable characters")
    return key


def _require_request_fingerprint(value: Any) -> str:
    fingerprint = require_canonical_nonblank(
        value,
        field_name="request_fingerprint",
    )
    if (
        len(fingerprint) != 64
        or fingerprint != fingerprint.lower()
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise ValueError("request_fingerprint must be a lowercase SHA-256 digest")
    return fingerprint


def _require_positive_integer(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_optional_canonical_string(value: Any, *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return require_canonical_nonblank(value, field_name=field_name)


def _require_json_primitives(value: Any, *, field_name: str) -> None:
    seen: set[int] = set()
    node_count = 0

    def visit(item: Any, *, path: str, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > 100_000:
            raise ValueError(f"{field_name} exceeds the JSON node limit")
        if depth > 64:
            raise ValueError(f"{field_name} exceeds the JSON depth limit")
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(
                    f"{field_name} must contain only finite JSON primitives"
                )
            return
        if not isinstance(item, (list, dict)):
            raise ValueError(
                f"{field_name} must contain only JSON primitive values"
            )
        identity = id(item)
        if identity in seen:
            raise ValueError(f"{field_name} must not contain cycles")
        seen.add(identity)
        try:
            if isinstance(item, list):
                for index, nested in enumerate(item):
                    visit(nested, path=f"{path}[{index}]", depth=depth + 1)
            else:
                for key, nested in item.items():
                    if not isinstance(key, str):
                        raise ValueError(
                            f"{field_name} must contain only string object keys"
                        )
                    visit(nested, path=f"{path}.{key}", depth=depth + 1)
        finally:
            seen.remove(identity)

    visit(value, path=field_name, depth=0)


_WORKFLOW_RUN_SPEC_KEYS = frozenset(
    {
        "spec_version",
        "workflow_path",
        "parameters",
        "session_id",
        "client_id",
        "use_enhanced",
        "enable_telemetry",
        "run_incarnation",
        "deadline_at_ms",
    }
)


def _validate_work_spec(
    work_spec: Mapping[str, Any],
    *,
    deadline_at_ms: Optional[int] = None,
) -> tuple[dict[str, Any], str, str]:
    if not isinstance(work_spec, dict):
        raise ValueError("work_spec must be a JSON object")
    if set(work_spec) != _WORKFLOW_RUN_SPEC_KEYS:
        raise ValueError("work_spec must contain exactly the required keys")
    if work_spec["spec_version"] != WORKFLOW_RUN_SPEC_VERSION:
        raise ValueError("work_spec has an unsupported spec_version")
    workflow_path = require_canonical_nonblank(
        work_spec["workflow_path"], field_name="workflow_path"
    )
    if not workflow_path.startswith("/") or posixpath.normpath(workflow_path) != workflow_path:
        raise ValueError("workflow_path must be an absolute canonical path")
    parameters = work_spec["parameters"]
    if not isinstance(parameters, dict):
        raise ValueError("work_spec parameters must be an object")
    _require_json_primitives(parameters, field_name="work_spec parameters")
    session_id = require_canonical_nonblank(
        work_spec["session_id"], field_name="session_id"
    )
    client_id = _require_optional_canonical_string(
        work_spec["client_id"], field_name="client_id"
    )
    use_enhanced = work_spec["use_enhanced"]
    enable_telemetry = work_spec["enable_telemetry"]
    if not isinstance(use_enhanced, bool) or not isinstance(enable_telemetry, bool):
        raise ValueError("work_spec flags must be booleans")
    run_incarnation = require_canonical_nonblank(
        work_spec["run_incarnation"], field_name="run_incarnation"
    )
    spec_deadline = _require_positive_integer(
        work_spec["deadline_at_ms"], field_name="work_spec deadline_at_ms"
    )
    if deadline_at_ms is not None and spec_deadline != deadline_at_ms:
        raise ValueError("work_spec deadline does not match deadline_at_ms")
    normalized = {
        "spec_version": WORKFLOW_RUN_SPEC_VERSION,
        "workflow_path": workflow_path,
        "parameters": parameters,
        "session_id": session_id,
        "client_id": client_id,
        "use_enhanced": use_enhanced,
        "enable_telemetry": enable_telemetry,
        "run_incarnation": run_incarnation,
        "deadline_at_ms": spec_deadline,
    }
    spec_json = _canonical_payload(normalized)
    if len(spec_json.encode("utf-8")) > WORKFLOW_RUN_SPEC_MAX_BYTES:
        raise ValueError("work_spec exceeds the JSON byte limit")
    return normalized, spec_json, hashlib.sha256(spec_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    run_id: str
    event_type: str
    payload: dict[str, Any]
    created_at_ms: int
    run_incarnation: Optional[str] = None
    event_key: Optional[str] = None


@dataclass(frozen=True)
class WorkflowRunInvocation:
    run_id: str
    run_incarnation: str
    session_id: str
    workflow_name: str


@dataclass(frozen=True)
class WorkflowRunSpec:
    run_id: str
    spec_version: int
    spec_json: str
    spec_sha256: str
    created_at_ms: int

    def to_mapping(self) -> dict[str, Any]:
        if self.spec_version != WORKFLOW_RUN_SPEC_VERSION:
            raise ValueError("stored work_spec has an unsupported version")
        if hashlib.sha256(self.spec_json.encode("utf-8")).hexdigest() != self.spec_sha256:
            raise ValueError("stored work_spec digest does not match its JSON")
        try:
            value = json.loads(self.spec_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("stored work_spec JSON is malformed") from exc
        normalized, canonical_json, digest = _validate_work_spec(value)
        if canonical_json != self.spec_json or digest != self.spec_sha256:
            raise ValueError("stored work_spec JSON is not canonical")
        return normalized


@dataclass(frozen=True)
class TextToSqlHistoryEntry:
    tenant_id: str
    owner_subject: str
    run_id: str
    created_at_ms: int
    status: TextToSqlTerminalStatus
    dialect: str
    profile_name: str
    terminal_result: TextToSqlTerminalResult

    def to_mapping(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "owner_subject": self.owner_subject,
            "run_id": self.run_id,
            "created_at_ms": self.created_at_ms,
            "status": self.status.value,
            "dialect": self.dialect,
            "profile_name": self.profile_name,
            "terminal_snapshot": self.terminal_result.to_mapping(),
        }


@dataclass(frozen=True)
class LegacyTextToSqlHistoryImportResult:
    imported: int
    duplicates: int
    quarantined_malformed: int
    quarantined_unowned: int
    quarantined_invalid: int
    already_completed: bool = False

    @property
    def quarantined(self) -> int:
        return (
            self.quarantined_malformed
            + self.quarantined_unowned
            + self.quarantined_invalid
        )


@dataclass(frozen=True)
class OperationalRetentionLease:
    scope: str
    owner_id: str
    generation: int
    expires_at_ms: int


@dataclass(frozen=True)
class OperationalRetentionState:
    scope: str
    lease_owner: Optional[str]
    lease_generation: int
    lease_expires_at_ms: Optional[int]
    last_attempt_at_ms: Optional[int]
    last_success_at_ms: Optional[int]
    next_due_at_ms: int
    status: str
    counters: dict[str, Any]
    last_error: Optional[str]
    updated_at_ms: int


def _require_non_negative_integer(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_history_dimension(value: Any, *, field_name: str) -> str:
    normalized = require_canonical_nonblank(value, field_name=field_name)
    if len(normalized) > 128 or not all(
        character.isprintable() for character in normalized
    ):
        raise ValueError(
            f"{field_name} must be at most 128 printable characters"
        )
    return normalized


def _require_sha256_digest(value: Any, *, field_name: str) -> str:
    digest = require_canonical_nonblank(value, field_name=field_name)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return digest


def _require_legacy_source_path(value: Any) -> str:
    source_path = require_canonical_nonblank(value, field_name="source_path")
    if len(source_path) > 4096 or not all(
        character.isprintable() for character in source_path
    ):
        raise ValueError("source_path must be at most 4096 printable characters")
    return source_path


def _require_history_page(limit: Any, offset: Any) -> tuple[int, int]:
    limit = _require_positive_integer(limit, field_name="limit")
    offset = _require_non_negative_integer(offset, field_name="offset")
    if limit > 1000:
        raise ValueError("limit must not exceed 1000")
    return limit, offset


def _text_to_sql_history_dimensions(
    payload: Mapping[str, Any],
    terminal_result: TextToSqlTerminalResult,
) -> tuple[str, str]:
    snapshot = payload.get("snapshot")
    parameters = snapshot.get("parameters") if isinstance(snapshot, dict) else None
    parameters = parameters if isinstance(parameters, dict) else {}
    execution = terminal_result.execution

    def dimension(value: Any) -> Optional[str]:
        try:
            return _require_history_dimension(value, field_name="history dimension")
        except (TypeError, ValueError):
            return None

    dialect = dimension(parameters.get("dialect")) or "unknown"
    profile_name = (
        dimension(execution.get("profile_name"))
        or dimension(parameters.get("profile_name"))
        or dimension(parameters.get("safety_profile"))
        or "unknown"
    )
    return dialect, profile_name


@dataclass(frozen=True)
class StoredRun:
    run_id: str
    thread_id: str
    owner_subject: str
    tenant_id: str
    roles: frozenset[str]
    created_at_ms: int
    run_kind: str
    status: str
    terminal_reason: Optional[str]
    worker_pid: Optional[int]
    updated_at_ms: int
    finished_at_ms: Optional[int]
    result_seq: Optional[int]
    idempotency_key: Optional[str]
    request_fingerprint: Optional[str]
    queued_at_ms: Optional[int]
    deadline_at_ms: Optional[int]
    supervisor_id: Optional[str]
    worker_lease_expires_at_ms: Optional[int]
    worker_heartbeat_at_ms: Optional[int]
    cancel_requested_at_ms: Optional[int]
    attempt_started_at_ms: Optional[int]
    worker_pid_started_at_ms: Optional[int]

    @property
    def principal(self) -> Principal:
        return Principal(
            subject=self.owner_subject,
            tenant_id=self.tenant_id,
            roles=self.roles,
        )


@dataclass(frozen=True)
class WorkflowClaimEnvelope:
    supervisor_id: str
    attempt_generation: int


@dataclass(frozen=True)
class ClaimedWorkflowRun:
    run: StoredRun
    work_spec: Optional[WorkflowRunSpec]
    claim: WorkflowClaimEnvelope
    invalid_work_spec: bool = False


@dataclass(frozen=True)
class WorkflowExpiryDecision:
    run_id: str
    run_kind: str
    action: str
    terminal_status: Optional[str]
    terminal_reason: Optional[str]
    worker_pid: Optional[int]
    supervisor_id: Optional[str]
    attempt_generation: Optional[int]
    worker_lease_expires_at_ms: Optional[int]
    worker_pid_started_at_ms: Optional[int]


@dataclass(frozen=True)
class WorkflowExpiryResult:
    changed_count: int
    terminal_decisions: tuple[WorkflowExpiryDecision, ...]


class EventStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._closed = False
        self._connection_closed = False
        self._lease_released = False
        self._bundle_lease = acquire_sqlite_bundle(self._db_path)
        self._conn: sqlite3.Connection | None = None
        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                timeout=30.0,
            )
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._migrate_schema()
            journal_mode = str(
                self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            ).lower()
            if journal_mode != "wal":
                raise sqlite3.DatabaseError(
                    f"AG-UI EventStore requires WAL mode, got {journal_mode!r}"
                )
        except Exception as primary_error:
            cleanup_errors: list[Exception] = []
            if self._conn is not None:
                for _attempt in range(2):
                    try:
                        self._conn.close()
                    except Exception as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                    else:
                        self._connection_closed = True
                        break
            else:
                self._connection_closed = True
            if self._connection_closed:
                for _attempt in range(2):
                    try:
                        release_sqlite_bundle(self._bundle_lease)
                    except Exception as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                    else:
                        self._lease_released = True
                        break
            self._closed = self._connection_closed and self._lease_released
            if cleanup_errors:
                primary_error.add_note(
                    "EventStore initialization cleanup encountered errors"
                )
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(
                        f"cleanup {type(cleanup_error).__name__}: {cleanup_error}"
                    )
                raise primary_error from cleanup_errors[-1]
            raise

    def _migrate_schema(self) -> None:
        """Apply additive migrations once under a cross-process exclusive lock."""
        try:
            self._conn.execute("BEGIN EXCLUSIVE")
            current_version = int(
                self._conn.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version > AGUI_EVENT_STORE_SCHEMA_VERSION:
                raise RuntimeError(
                    "AG-UI EventStore schema is newer than this application"
                )
            if current_version == AGUI_EVENT_STORE_SCHEMA_VERSION:
                self._validate_current_schema()
                self._conn.commit()
                return
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agui_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agui_runs (
                    run_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    owner_subject TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    roles TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            run_columns = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(agui_runs)")
            }
            lifecycle_columns = (
                ("run_kind", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("status", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("terminal_reason", "TEXT"),
                ("worker_pid", "INTEGER"),
                ("updated_at_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("finished_at_ms", "INTEGER"),
                ("result_seq", "INTEGER"),
                ("idempotency_key", "TEXT"),
                ("request_fingerprint", "TEXT"),
                ("queued_at_ms", "INTEGER"),
                ("deadline_at_ms", "INTEGER"),
                ("supervisor_id", "TEXT"),
                ("worker_lease_expires_at_ms", "INTEGER"),
                ("worker_heartbeat_at_ms", "INTEGER"),
                ("cancel_requested_at_ms", "INTEGER"),
                ("attempt_started_at_ms", "INTEGER"),
                ("worker_pid_started_at_ms", "INTEGER"),
            )
            for column_name, column_definition in lifecycle_columns:
                if column_name not in run_columns:
                    self._conn.execute(
                        f"ALTER TABLE agui_runs ADD COLUMN "
                        f"{column_name} {column_definition}"
                    )
            self._conn.execute(
                """
                UPDATE agui_runs
                SET updated_at_ms = created_at_ms
                WHERE updated_at_ms = 0
                """
            )
            event_columns = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(agui_events)")
            }
            if "run_incarnation" not in event_columns:
                self._conn.execute(
                    "ALTER TABLE agui_events ADD COLUMN run_incarnation TEXT"
                )
            if "event_key" not in event_columns:
                self._conn.execute(
                    "ALTER TABLE agui_events ADD COLUMN event_key TEXT"
                )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_run_invocations (
                    run_id TEXT PRIMARY KEY,
                    run_incarnation TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    workflow_name TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_run_specs (
                    run_id TEXT PRIMARY KEY,
                    spec_version INTEGER NOT NULL,
                    spec_json TEXT NOT NULL,
                    spec_sha256 TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS text_to_sql_history (
                    tenant_id TEXT NOT NULL,
                    owner_subject TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    dialect TEXT NOT NULL,
                    profile_name TEXT NOT NULL,
                    terminal_snapshot_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, owner_subject, run_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS text_to_sql_history_quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reason TEXT NOT NULL,
                    raw_payload TEXT NOT NULL,
                    quarantined_at_ms INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS text_to_sql_history_imports (
                    source_identity TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    completed_at_ms INTEGER NOT NULL,
                    imported INTEGER NOT NULL,
                    duplicates INTEGER NOT NULL,
                    quarantined_malformed INTEGER NOT NULL,
                    quarantined_unowned INTEGER NOT NULL,
                    quarantined_invalid INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operational_retention_state (
                    scope TEXT PRIMARY KEY,
                    lease_owner TEXT,
                    lease_generation INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at_ms INTEGER,
                    last_attempt_at_ms INTEGER,
                    last_success_at_ms INTEGER,
                    next_due_at_ms INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'never',
                    counters_json TEXT NOT NULL DEFAULT '{}',
                    last_error TEXT,
                    updated_at_ms INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._migrate_run_sequence_index(
                current_version=current_version,
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agui_events_event_key
                ON agui_events(event_key)
                WHERE event_key IS NOT NULL
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agui_runs_owner_idempotency
                ON agui_runs(tenant_id, owner_subject, run_kind, idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agui_runs_queue
                ON agui_runs(status, queued_at_ms, created_at_ms, run_id)
                WHERE status = 'queued'
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agui_runs_active_leases
                ON agui_runs(
                    status, worker_lease_expires_at_ms, tenant_id, owner_subject
                )
                WHERE status = 'running'
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_text_to_sql_history_owner_created
                ON text_to_sql_history(
                    tenant_id,
                    owner_subject,
                    created_at_ms DESC,
                    run_id DESC
                )
                """
            )
            self._validate_current_schema()
            self._conn.execute(
                f"PRAGMA user_version={AGUI_EVENT_STORE_SCHEMA_VERSION}"
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def _normalize_schema_sql(sql: str) -> str:
        return normalize_sqlite_schema_sql(sql)

    def _migrate_run_sequence_index(self, *, current_version: int) -> None:
        index_name = "idx_agui_events_run_seq"
        (
            table_name,
            expected_unique,
            expected_origin,
            expected_partial,
            expected_xinfo,
            expected_sql,
        ) = _EVENT_STORE_NAMED_INDEXES[index_name]
        create_sql = (
            "CREATE UNIQUE INDEX idx_agui_events_run_seq "
            "ON agui_events(run_id, seq)"
        )
        object_row = self._conn.execute(
            "SELECT type, tbl_name, sql FROM sqlite_master WHERE name = ?",
            (index_name,),
        ).fetchone()
        if object_row is None:
            self._conn.execute(create_sql)
            return

        index = next(
            (
                row
                for row in self._conn.execute(
                    "SELECT * FROM pragma_index_list(?)",
                    (table_name,),
                )
                if str(row[1]) == index_name
            ),
            None,
        )
        actual_shape = (
            str(object_row[0]),
            str(object_row[1]),
            int(index[2]) if index is not None else None,
            str(index[3]) if index is not None else None,
            int(index[4]) if index is not None else None,
            self._index_xinfo_signature(index_name) if index is not None else None,
            (
                self._normalize_schema_sql(str(object_row[2]))
                if isinstance(object_row[2], str)
                else None
            ),
        )
        current_shape = (
            "index",
            table_name,
            expected_unique,
            expected_origin,
            expected_partial,
            expected_xinfo,
            self._normalize_schema_sql(expected_sql),
        )
        if actual_shape == current_shape:
            return

        legacy_v2_shape = (
            "index",
            table_name,
            0,
            expected_origin,
            expected_partial,
            expected_xinfo,
            self._normalize_schema_sql(
                "CREATE INDEX IDX_AGUI_EVENTS_RUN_SEQ "
                "ON AGUI_EVENTS(RUN_ID, SEQ)"
            ),
        )
        if current_version != 2 or actual_shape != legacy_v2_shape:
            raise RuntimeError(
                "AG-UI EventStore legacy run sequence index is malformed"
            )

        self._conn.execute("DROP INDEX idx_agui_events_run_seq")
        self._conn.execute(create_sql)

    def _index_xinfo_signature(
        self,
        index_name: str,
    ) -> tuple[tuple[int, int, Optional[str], int, str, int], ...]:
        rows = self._conn.execute(
            """
            SELECT seqno, cid, name, desc, coll, key
            FROM pragma_index_xinfo(?)
            ORDER BY seqno
            """,
            (index_name,),
        )
        return tuple(
            (
                int(row[0]),
                int(row[1]),
                None if row[2] is None else str(row[2]),
                int(row[3]),
                str(row[4]).upper(),
                int(row[5]),
            )
            for row in rows
        )

    def _validate_table_schema(self, table_name: str) -> None:
        rows = list(
            self._conn.execute(
                "SELECT * FROM pragma_table_xinfo(?) ORDER BY cid",
                (table_name,),
            )
        )
        actual = tuple(
            (
                str(row[1]),
                str(row[2]).upper(),
                int(row[3]),
                int(row[5]),
                int(row[6]),
            )
            for row in rows
        )
        expected = _EVENT_STORE_TABLE_SIGNATURES[table_name]
        actual_defaults = {
            str(row[1]): None if row[4] is None else str(row[4])
            for row in rows
        }
        expected_defaults = {
            str(row[1]): _EVENT_STORE_COLUMN_DEFAULTS[table_name].get(str(row[1]))
            for row in rows
        }
        if actual != expected or actual_defaults != expected_defaults:
            raise RuntimeError(
                f"AG-UI EventStore schema table is malformed: {table_name}"
            )

    def _validate_named_index(self, index_name: str) -> None:
        (
            table_name,
            expected_unique,
            expected_origin,
            expected_partial,
            expected_xinfo,
            expected_sql,
        ) = _EVENT_STORE_NAMED_INDEXES[index_name]
        indexes = {
            str(row[1]): row
            for row in self._conn.execute(
                "SELECT * FROM pragma_index_list(?)",
                (table_name,),
            )
        }
        index = indexes.get(index_name)
        if index is None:
            raise RuntimeError(
                f"AG-UI EventStore schema index is missing: {index_name}"
            )
        sql_row = self._conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index' AND name = ? AND tbl_name = ?
            """,
            (index_name, table_name),
        ).fetchone()
        actual_sql = (
            self._normalize_schema_sql(str(sql_row[0]))
            if sql_row is not None and isinstance(sql_row[0], str)
            else None
        )
        if (
            int(index[2]) != expected_unique
            or str(index[3]) != expected_origin
            or int(index[4]) != expected_partial
            or self._index_xinfo_signature(index_name) != expected_xinfo
            or actual_sql != self._normalize_schema_sql(expected_sql)
        ):
            raise RuntimeError(
                f"AG-UI EventStore schema index is malformed: {index_name}"
            )

    def _validate_automatic_unique_indexes(self, table_name: str) -> None:
        named_indexes = {
            name
            for name, definition in _EVENT_STORE_NAMED_INDEXES.items()
            if definition[0] == table_name
        }
        actual = []
        for index in self._conn.execute(
            "SELECT * FROM pragma_index_list(?)",
            (table_name,),
        ):
            name = str(index[1])
            if name in named_indexes or not int(index[2]):
                continue
            sql_row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (name,),
            ).fetchone()
            if sql_row is None or sql_row[0] is not None:
                raise RuntimeError(
                    f"AG-UI EventStore schema has an unexpected unique index: {name}"
                )
            actual.append(
                (
                    str(index[3]),
                    int(index[4]),
                    self._index_xinfo_signature(name),
                )
            )
        expected = list(_EVENT_STORE_AUTOMATIC_UNIQUE_INDEXES[table_name])
        if sorted(actual, key=repr) != sorted(expected, key=repr):
            raise RuntimeError(
                "AG-UI EventStore schema unique indexes are malformed: "
                f"{table_name}"
            )

    def _validate_current_schema(self) -> None:
        actual_objects = {}
        for object_type, name, table_name, rootpage, sql in self._conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master"
        ):
            object_name = str(name)
            if object_name in actual_objects:
                raise RuntimeError(
                    "AG-UI EventStore schema has duplicate sqlite_master objects"
                )
            object_rootpage = int(rootpage)
            actual_objects[object_name] = (
                str(object_type),
                str(table_name),
                self._normalize_schema_sql(str(sql)) if isinstance(sql, str) else None,
                object_rootpage,
            )
        expected_objects = {
            name: (
                object_type,
                table_name,
                self._normalize_schema_sql(sql) if isinstance(sql, str) else None,
            )
            for name, (object_type, table_name, sql) in (
                _EVENT_STORE_SQLITE_MASTER.items()
            )
        }
        stat_tables = validate_sqlite_optimizer_statistics(
            self._conn,
            actual_objects,
            owner="AG-UI EventStore",
        )
        actual_names = set(actual_objects)
        if actual_names - set(stat_tables) != set(expected_objects):
            raise RuntimeError(
                "AG-UI EventStore schema has unapproved sqlite_master objects"
            )
        if any(
            actual_objects[name][:3] != expected
            for name, expected in expected_objects.items()
        ):
            raise RuntimeError(
                "AG-UI EventStore schema has malformed sqlite_master objects"
            )
        discard_validated_sqlite_optimizer_statistics(self._conn, stat_tables)
        remaining_names = {
            str(row[0])
            for row in self._conn.execute("SELECT name FROM sqlite_master")
        }
        if remaining_names != set(expected_objects):
            raise RuntimeError(
                "AG-UI EventStore schema normalization left unexpected objects"
            )
        for table_name in _EVENT_STORE_TABLE_SIGNATURES:
            self._validate_table_schema(table_name)
        for index_name in _EVENT_STORE_NAMED_INDEXES:
            self._validate_named_index(index_name)
        for table_name in _EVENT_STORE_TABLE_SIGNATURES:
            self._validate_automatic_unique_indexes(table_name)

    def reserve_workflow_run(
        self,
        run_id: str,
        run_incarnation: str,
        session_id: str,
        workflow_name: str,
    ) -> bool:
        """Atomically reserve a one-shot workflow run identifier."""
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        run_incarnation = require_canonical_nonblank(
            run_incarnation,
            field_name="run_incarnation",
        )
        session_id = require_canonical_nonblank(session_id, field_name="session_id")
        workflow_name = require_canonical_nonblank(
            workflow_name,
            field_name="workflow_name",
        )
        created_at_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO workflow_run_invocations
                        (run_id, run_incarnation, session_id, workflow_name, created_at_ms)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        run_incarnation,
                        session_id,
                        workflow_name,
                        created_at_ms,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return cur.rowcount == 1

    @staticmethod
    def _stored_run_from_row(row: tuple[Any, ...]) -> StoredRun:
        roles_value = json.loads(str(row[4]))
        if not isinstance(roles_value, list):
            raise ValueError("stored run roles must be a JSON list")
        return StoredRun(
            run_id=str(row[0]),
            thread_id=str(row[1]),
            owner_subject=str(row[2]),
            tenant_id=str(row[3]),
            roles=frozenset(str(role) for role in roles_value),
            created_at_ms=int(row[5]),
            run_kind=str(row[6]),
            status=str(row[7]),
            terminal_reason=None if row[8] is None else str(row[8]),
            worker_pid=None if row[9] is None else int(row[9]),
            updated_at_ms=int(row[10]),
            finished_at_ms=None if row[11] is None else int(row[11]),
            result_seq=None if row[12] is None else int(row[12]),
            idempotency_key=None if row[13] is None else str(row[13]),
            request_fingerprint=None if row[14] is None else str(row[14]),
            queued_at_ms=None if row[15] is None else int(row[15]),
            deadline_at_ms=None if row[16] is None else int(row[16]),
            supervisor_id=None if row[17] is None else str(row[17]),
            worker_lease_expires_at_ms=(
                None if row[18] is None else int(row[18])
            ),
            worker_heartbeat_at_ms=None if row[19] is None else int(row[19]),
            cancel_requested_at_ms=None if row[20] is None else int(row[20]),
            attempt_started_at_ms=None if row[21] is None else int(row[21]),
            worker_pid_started_at_ms=None if row[22] is None else int(row[22]),
        )

    def _get_run_row_locked(self, run_id: str) -> Optional[tuple[Any, ...]]:
        return self._conn.execute(
            """
            SELECT run_id, thread_id, owner_subject, tenant_id, roles,
                   created_at_ms, run_kind, status, terminal_reason,
                   worker_pid, updated_at_ms, finished_at_ms, result_seq,
                   idempotency_key, request_fingerprint, queued_at_ms,
                   deadline_at_ms, supervisor_id, worker_lease_expires_at_ms,
                   worker_heartbeat_at_ms, cancel_requested_at_ms,
                   attempt_started_at_ms, worker_pid_started_at_ms
            FROM agui_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    def create_run(
        self,
        run_id: str,
        thread_id: str,
        principal: Principal,
        *,
        run_kind: str = "agui",
        status: str = "pending",
        idempotency_key: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
    ) -> StoredRun:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        thread_id = require_canonical_nonblank(thread_id, field_name="thread_id")
        run_kind = _require_run_kind(run_kind)
        status = _require_run_status(status)
        if idempotency_key is not None:
            idempotency_key = _require_idempotency_key(idempotency_key)
            if request_fingerprint is None:
                raise ValueError(
                    "request_fingerprint is required with idempotency_key"
                )
        if request_fingerprint is not None:
            request_fingerprint = _require_request_fingerprint(
                request_fingerprint
            )
        record = principal_to_record(principal)
        owner_subject = require_canonical_nonblank(
            record["subject"],
            field_name="owner_subject",
        )
        tenant_id = require_canonical_nonblank(
            record["tenant_id"],
            field_name="tenant_id",
        )
        created_at_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    """
                    INSERT INTO agui_runs
                        (run_id, thread_id, owner_subject, tenant_id, roles,
                         created_at_ms, run_kind, status, updated_at_ms,
                         idempotency_key, request_fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        thread_id,
                        owner_subject,
                        tenant_id,
                        json.dumps(record["roles"]),
                        created_at_ms,
                        run_kind,
                        status,
                        created_at_ms,
                        idempotency_key,
                        request_fingerprint,
                    ),
                )
                row = self._get_run_row_locked(run_id)
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ValueError(f"run_id already exists: {run_id}") from exc
            except Exception:
                self._conn.rollback()
                raise
        assert row is not None
        return self._stored_run_from_row(row)

    def create_or_get_run(
        self,
        *,
        principal: Principal,
        run_kind: str,
        idempotency_key: Optional[str],
        request_fingerprint: Optional[str],
        proposed_run_id: str,
        thread_id: str,
    ) -> tuple[StoredRun, bool]:
        proposed_run_id = require_canonical_nonblank(
            proposed_run_id,
            field_name="proposed_run_id",
        )
        thread_id = require_canonical_nonblank(thread_id, field_name="thread_id")
        run_kind = _require_run_kind(run_kind)
        if idempotency_key is not None:
            idempotency_key = _require_idempotency_key(idempotency_key)
            if request_fingerprint is None:
                raise ValueError(
                    "request_fingerprint is required with idempotency_key"
                )
        if request_fingerprint is not None:
            request_fingerprint = _require_request_fingerprint(
                request_fingerprint
            )
        record = principal_to_record(principal)
        owner_subject = require_canonical_nonblank(
            record["subject"],
            field_name="owner_subject",
        )
        tenant_id = require_canonical_nonblank(
            record["tenant_id"],
            field_name="tenant_id",
        )
        created_at_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = None
                if idempotency_key is not None:
                    existing = self._conn.execute(
                        """
                        SELECT run_id, thread_id, owner_subject, tenant_id,
                               roles, created_at_ms, run_kind, status,
                               terminal_reason, worker_pid, updated_at_ms,
                               finished_at_ms, result_seq, idempotency_key,
                               request_fingerprint, queued_at_ms,
                               deadline_at_ms, supervisor_id,
                               worker_lease_expires_at_ms,
                               worker_heartbeat_at_ms, cancel_requested_at_ms,
                               attempt_started_at_ms, worker_pid_started_at_ms
                        FROM agui_runs
                        WHERE tenant_id = ? AND owner_subject = ?
                          AND run_kind = ? AND idempotency_key = ?
                        """,
                        (
                            tenant_id,
                            owner_subject,
                            run_kind,
                            idempotency_key,
                        ),
                    ).fetchone()
                if existing is not None:
                    stored = self._stored_run_from_row(existing)
                    if stored.request_fingerprint != request_fingerprint:
                        raise ValueError(
                            "idempotency_key was already used for a different request"
                        )
                    self._conn.commit()
                    return stored, False
                try:
                    self._conn.execute(
                        """
                        INSERT INTO agui_runs
                            (run_id, thread_id, owner_subject, tenant_id, roles,
                             created_at_ms, run_kind, status, updated_at_ms,
                             idempotency_key, request_fingerprint)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                        """,
                        (
                            proposed_run_id,
                            thread_id,
                            owner_subject,
                            tenant_id,
                            json.dumps(record["roles"]),
                            created_at_ms,
                            run_kind,
                            created_at_ms,
                            idempotency_key,
                            request_fingerprint,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(
                        f"run_id already exists: {proposed_run_id}"
                    ) from exc
                row = self._get_run_row_locked(proposed_run_id)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        assert row is not None
        return self._stored_run_from_row(row), True

    def record_run(self, run_id: str, thread_id: str, principal: Principal) -> None:
        self.create_run(run_id, thread_id, principal)

    def get_run(self, run_id: str) -> Optional[StoredRun]:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        with self._lock:
            row = self._get_run_row_locked(run_id)
        return None if row is None else self._stored_run_from_row(row)

    def _load_raw_work_spec_locked(self, run_id: str) -> Optional[WorkflowRunSpec]:
        row = self._conn.execute(
            """
            SELECT run_id, spec_version, spec_json, spec_sha256, created_at_ms
            FROM workflow_run_specs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return WorkflowRunSpec(
            run_id=str(row[0]),
            spec_version=int(row[1]),
            spec_json=str(row[2]),
            spec_sha256=str(row[3]),
            created_at_ms=int(row[4]),
        )

    def _load_work_spec_locked(self, run_id: str) -> Optional[WorkflowRunSpec]:
        stored = self._load_raw_work_spec_locked(run_id)
        if stored is None:
            return None
        stored.to_mapping()
        return stored

    def load_work_spec(self, run_id: str) -> Optional[WorkflowRunSpec]:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        with self._lock:
            return self._load_work_spec_locked(run_id)

    def has_work_spec(self, run_id: str) -> bool:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM workflow_run_specs WHERE run_id = ?",
                (run_id,),
            ).fetchone() is not None

    def enqueue_run(
        self,
        run_id: str,
        work_spec: Mapping[str, Any],
        deadline_at_ms: int,
        queue_limit: int,
    ) -> StoredRun:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        deadline_at_ms = _require_positive_integer(
            deadline_at_ms, field_name="deadline_at_ms"
        )
        queue_limit = _require_positive_integer(queue_limit, field_name="queue_limit")
        _normalized, spec_json, spec_sha256 = _validate_work_spec(
            work_spec,
            deadline_at_ms=deadline_at_ms,
        )
        now_ms = int(time.time() * 1000)
        if deadline_at_ms <= now_ms:
            raise ValueError("deadline_at_ms must be in the future")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                queued_count = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM agui_runs WHERE status = 'queued'"
                    ).fetchone()[0]
                )
                if queued_count >= queue_limit:
                    raise WorkflowAdmissionRejected(
                        f"workflow queue limit reached: {queue_limit}"
                    )
                run_row = self._get_run_row_locked(run_id)
                if run_row is None:
                    raise ValueError(f"run not found: {run_id}")
                if self._stored_run_from_row(run_row).status != "pending":
                    raise ValueError("only pending runs can be enqueued")
                self._conn.execute(
                    """
                    INSERT INTO workflow_run_specs
                        (run_id, spec_version, spec_json, spec_sha256, created_at_ms)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        WORKFLOW_RUN_SPEC_VERSION,
                        spec_json,
                        spec_sha256,
                        now_ms,
                    ),
                )
                cur = self._conn.execute(
                    """
                    UPDATE agui_runs
                    SET status = 'queued', queued_at_ms = ?, deadline_at_ms = ?,
                        updated_at_ms = ?
                    WHERE run_id = ? AND status = 'pending'
                    """,
                    (now_ms, deadline_at_ms, now_ms, run_id),
                )
                if cur.rowcount != 1:
                    raise ValueError("run was no longer pending during enqueue")
                row = self._get_run_row_locked(run_id)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        assert row is not None
        return self._stored_run_from_row(row)

    def claim_next_queued(
        self,
        supervisor_id: str,
        global_limit: int,
        per_tenant_limit: int,
        per_owner_limit: int,
        lease_expires_at_ms: int,
    ) -> Optional[ClaimedWorkflowRun]:
        supervisor_id = require_canonical_nonblank(
            supervisor_id, field_name="supervisor_id"
        )
        global_limit = _require_positive_integer(
            global_limit, field_name="global_limit"
        )
        per_tenant_limit = _require_positive_integer(
            per_tenant_limit, field_name="per_tenant_limit"
        )
        per_owner_limit = _require_positive_integer(
            per_owner_limit, field_name="per_owner_limit"
        )
        lease_expires_at_ms = _require_positive_integer(
            lease_expires_at_ms, field_name="lease_expires_at_ms"
        )
        now_ms = int(time.time() * 1000)
        if lease_expires_at_ms <= now_ms:
            raise ValueError("lease_expires_at_ms must be in the future")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                active_count = int(
                    self._conn.execute(
                        """
                        SELECT COUNT(*) FROM agui_runs
                        WHERE status = 'running'
                          AND worker_lease_expires_at_ms > ?
                        """,
                        (now_ms,),
                    ).fetchone()[0]
                )
                if active_count >= global_limit:
                    self._conn.commit()
                    return None
                candidates = list(
                    self._conn.execute(
                        """
                        SELECT run_id, tenant_id, owner_subject,
                               attempt_started_at_ms
                        FROM agui_runs
                        WHERE status = 'queued'
                          AND cancel_requested_at_ms IS NULL
                          AND deadline_at_ms > ?
                        ORDER BY queued_at_ms, created_at_ms, run_id
                        """,
                        (now_ms,),
                    )
                )
                selected_run_id: Optional[str] = None
                previous_generation: Optional[int] = None
                for (
                    candidate_run_id,
                    tenant_id,
                    owner_subject,
                    candidate_generation,
                ) in candidates:
                    tenant_count = int(
                        self._conn.execute(
                            """
                            SELECT COUNT(*) FROM agui_runs
                            WHERE status = 'running'
                              AND worker_lease_expires_at_ms > ?
                              AND tenant_id = ?
                            """,
                            (now_ms, tenant_id),
                        ).fetchone()[0]
                    )
                    if tenant_count >= per_tenant_limit:
                        continue
                    owner_count = int(
                        self._conn.execute(
                            """
                            SELECT COUNT(*) FROM agui_runs
                            WHERE status = 'running'
                              AND worker_lease_expires_at_ms > ?
                              AND tenant_id = ? AND owner_subject = ?
                            """,
                            (now_ms, tenant_id, owner_subject),
                        ).fetchone()[0]
                    )
                    if owner_count < per_owner_limit:
                        selected_run_id = str(candidate_run_id)
                        previous_generation = (
                            None
                            if candidate_generation is None
                            else int(candidate_generation)
                        )
                        break
                if selected_run_id is None:
                    self._conn.commit()
                    return None
                attempt_generation = max(
                    now_ms,
                    1 if previous_generation is None else previous_generation + 1,
                )
                cur = self._conn.execute(
                    """
                    UPDATE agui_runs
                    SET status = 'running', supervisor_id = ?,
                        worker_lease_expires_at_ms = ?,
                        worker_heartbeat_at_ms = ?, attempt_started_at_ms = ?,
                        updated_at_ms = ?
                    WHERE run_id = ? AND status = 'queued'
                      AND cancel_requested_at_ms IS NULL
                      AND deadline_at_ms > ?
                    """,
                    (
                        supervisor_id,
                        lease_expires_at_ms,
                        now_ms,
                        attempt_generation,
                        now_ms,
                        selected_run_id,
                        now_ms,
                    ),
                )
                if cur.rowcount != 1:
                    self._conn.rollback()
                    return None
                row = self._get_run_row_locked(selected_run_id)
                work_spec = self._load_raw_work_spec_locked(selected_run_id)
                invalid_work_spec = work_spec is None
                if work_spec is not None:
                    try:
                        work_spec.to_mapping()
                    except (TypeError, ValueError):
                        invalid_work_spec = True
                        work_spec = None
                if row is None:
                    raise ValueError("claimed run disappeared")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return ClaimedWorkflowRun(
            run=self._stored_run_from_row(row),
            work_spec=work_spec,
            claim=WorkflowClaimEnvelope(
                supervisor_id=supervisor_id,
                attempt_generation=attempt_generation,
            ),
            invalid_work_spec=invalid_work_spec,
        )

    def renew_worker_lease(
        self,
        run_id: str,
        supervisor_id: str,
        attempt_generation: int,
        lease_expires_at_ms: int,
    ) -> bool:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        supervisor_id = require_canonical_nonblank(
            supervisor_id, field_name="supervisor_id"
        )
        attempt_generation = _require_positive_integer(
            attempt_generation, field_name="attempt_generation"
        )
        lease_expires_at_ms = _require_positive_integer(
            lease_expires_at_ms, field_name="lease_expires_at_ms"
        )
        now_ms = int(time.time() * 1000)
        if lease_expires_at_ms <= now_ms:
            raise ValueError("lease_expires_at_ms must be in the future")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    """
                    UPDATE agui_runs
                    SET worker_lease_expires_at_ms = ?,
                        worker_heartbeat_at_ms = ?, updated_at_ms = ?
                    WHERE run_id = ? AND status = 'running'
                      AND supervisor_id = ?
                      AND attempt_started_at_ms = ?
                      AND worker_lease_expires_at_ms > ?
                      AND cancel_requested_at_ms IS NULL
                    """,
                    (
                        lease_expires_at_ms,
                        now_ms,
                        now_ms,
                        run_id,
                        supervisor_id,
                        attempt_generation,
                        now_ms,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return cur.rowcount == 1

    def request_cancel(self, run_id: str) -> str:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        now_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._get_run_row_locked(run_id)
                if row is None:
                    raise ValueError(f"run not found: {run_id}")
                stored = self._stored_run_from_row(row)
                if stored.status in _TERMINAL_RUN_STATUSES:
                    self._conn.commit()
                    return stored.status
                if stored.status in {"pending", "queued"} and stored.run_kind != (
                    "text_to_sql"
                ):
                    self._conn.execute(
                        """
                        UPDATE agui_runs
                        SET status = 'cancelled', terminal_reason = 'CANCEL_REQUESTED',
                            cancel_requested_at_ms = COALESCE(
                                cancel_requested_at_ms, ?
                            ),
                            updated_at_ms = ?, finished_at_ms = ?
                        WHERE run_id = ? AND status = ?
                        """,
                        (now_ms, now_ms, now_ms, run_id, stored.status),
                    )
                    self._conn.execute(
                        "DELETE FROM workflow_run_specs WHERE run_id = ?",
                        (run_id,),
                    )
                    self._conn.commit()
                    return "cancelled"
                self._conn.execute(
                    """
                    UPDATE agui_runs
                    SET cancel_requested_at_ms = COALESCE(
                            cancel_requested_at_ms, ?
                        ),
                        updated_at_ms = ?
                    WHERE run_id = ? AND status = ?
                    """,
                    (now_ms, now_ms, run_id, stored.status),
                )
                self._conn.commit()
                return stored.status
            except Exception:
                self._conn.rollback()
                raise

    def fence_worker_attempt(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        lease_expires_at_ms: int,
    ) -> Optional[int]:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        expected_supervisor_id = require_canonical_nonblank(
            expected_supervisor_id,
            field_name="expected_supervisor_id",
        )
        expected_attempt_generation = _require_positive_integer(
            expected_attempt_generation,
            field_name="expected_attempt_generation",
        )
        lease_expires_at_ms = _require_positive_integer(
            lease_expires_at_ms,
            field_name="lease_expires_at_ms",
        )
        now_ms = int(time.time() * 1000)
        if lease_expires_at_ms <= now_ms:
            raise ValueError("lease_expires_at_ms must be in the future")
        next_generation = max(now_ms, expected_attempt_generation + 1)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    """
                    UPDATE agui_runs
                    SET attempt_started_at_ms = ?,
                        worker_lease_expires_at_ms = ?,
                        worker_heartbeat_at_ms = ?, updated_at_ms = ?
                    WHERE run_id = ? AND status = 'running'
                      AND supervisor_id = ?
                      AND attempt_started_at_ms = ?
                    """,
                    (
                        next_generation,
                        lease_expires_at_ms,
                        now_ms,
                        now_ms,
                        run_id,
                        expected_supervisor_id,
                        expected_attempt_generation,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return next_generation if cur.rowcount == 1 else None

    def mark_worker_result_pending(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
    ) -> bool:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        expected_supervisor_id = require_canonical_nonblank(
            expected_supervisor_id,
            field_name="expected_supervisor_id",
        )
        expected_attempt_generation = _require_positive_integer(
            expected_attempt_generation,
            field_name="expected_attempt_generation",
        )
        now_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    """
                    UPDATE agui_runs
                    SET status = 'result_pending', worker_pid = NULL,
                        worker_pid_started_at_ms = NULL,
                        worker_lease_expires_at_ms = NULL,
                        worker_heartbeat_at_ms = NULL, updated_at_ms = ?
                    WHERE run_id = ? AND status = 'running'
                      AND supervisor_id = ?
                      AND attempt_started_at_ms = ?
                    """,
                    (
                        now_ms,
                        run_id,
                        expected_supervisor_id,
                        expected_attempt_generation,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return cur.rowcount == 1

    def finish_worker(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        terminal_outcome: Mapping[str, Any],
    ) -> bool:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        expected_supervisor_id = require_canonical_nonblank(
            expected_supervisor_id, field_name="expected_supervisor_id"
        )
        expected_attempt_generation = _require_positive_integer(
            expected_attempt_generation,
            field_name="expected_attempt_generation",
        )
        if not isinstance(terminal_outcome, dict) or not set(terminal_outcome) <= {
            "status",
            "reason",
        }:
            raise ValueError("terminal_outcome must contain status and optional reason")
        terminal_status = _require_run_status(terminal_outcome.get("status"))
        if terminal_status not in _TERMINAL_RUN_STATUSES:
            raise ValueError("terminal_outcome status must be terminal")
        reason = terminal_outcome.get("reason")
        if reason is not None:
            reason = require_canonical_nonblank(reason, field_name="reason")
        now_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._get_run_row_locked(run_id)
                if row is None:
                    raise ValueError(f"run not found: {run_id}")
                if self._stored_run_from_row(row).run_kind == "text_to_sql":
                    raise ValueError(
                        "Text-to-SQL terminal transitions require "
                        "finalize_run_with_event"
                    )
                cur = self._conn.execute(
                    """
                    UPDATE agui_runs
                    SET status = ?, terminal_reason = ?, updated_at_ms = ?,
                        finished_at_ms = ?, worker_lease_expires_at_ms = NULL,
                        worker_heartbeat_at_ms = NULL
                    WHERE run_id = ? AND status = 'running'
                      AND supervisor_id = ?
                      AND attempt_started_at_ms = ?
                    """,
                    (
                        terminal_status,
                        reason,
                        now_ms,
                        now_ms,
                        run_id,
                        expected_supervisor_id,
                        expected_attempt_generation,
                    ),
                )
                if cur.rowcount == 1:
                    self._conn.execute(
                        "DELETE FROM workflow_run_specs WHERE run_id = ?",
                        (run_id,),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return cur.rowcount == 1

    def finalize_worker_with_event(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        payload: dict[str, Any],
    ) -> int:
        """Atomically adopt a claimed generic worker result and its lifecycle."""
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        expected_supervisor_id = require_canonical_nonblank(
            expected_supervisor_id,
            field_name="expected_supervisor_id",
        )
        expected_attempt_generation = _require_positive_integer(
            expected_attempt_generation,
            field_name="expected_attempt_generation",
        )
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        identity = workflow_result_identity_from_payload(
            payload,
            expected_run_id=run_id,
        )
        legacy_status = payload.get("status")
        success = payload.get("success")
        if legacy_status == "completed" and success is True:
            terminal_status = "succeeded"
        elif legacy_status == "cancelled" and success is False:
            terminal_status = "cancelled"
        elif legacy_status == "failed" and success is False:
            terminal_status = "failed"
        else:
            raise ValueError("generic WORKFLOW_RESULT has inconsistent status")
        terminal_reason = "WORKFLOW_RESULT"
        payload_json = _canonical_payload(payload)
        now_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._get_run_row_locked(run_id)
                if row is None:
                    raise ValueError(f"run not found: {run_id}")
                stored = self._stored_run_from_row(row)
                if stored.run_kind == "text_to_sql":
                    raise ValueError("run is a Text-to-SQL lifecycle")
                exact_claim = (
                    stored.supervisor_id == expected_supervisor_id
                    and stored.attempt_started_at_ms
                    == expected_attempt_generation
                )
                if stored.status in _TERMINAL_RUN_STATUSES:
                    if not exact_claim or stored.result_seq is None:
                        raise WorkflowSupervisorOwnershipConflictError(
                            "terminal lifecycle claim ownership changed"
                        )
                    winner = self._conn.execute(
                        "SELECT event_type, payload FROM agui_events "
                        "WHERE run_id = ? AND seq = ?",
                        (run_id, stored.result_seq),
                    ).fetchone()
                    if winner is None or str(winner[0]) != "WORKFLOW_RESULT":
                        raise TerminalWorkflowResultConflictError(
                            "terminal lifecycle winner is not WORKFLOW_RESULT"
                        )
                    if (
                        stored.status != terminal_status
                        or _canonical_payload(json.loads(str(winner[1])))
                        != payload_json
                    ):
                        raise TerminalWorkflowResultConflictError(
                            "terminal lifecycle winner conflicts with WORKFLOW_RESULT"
                        )
                    self._conn.commit()
                    return stored.result_seq
                if stored.status != "running" or not exact_claim:
                    raise WorkflowSupervisorOwnershipConflictError(
                        "lifecycle claim ownership changed"
                    )
                seq = self._insert_workflow_result_locked(
                    run_id=run_id,
                    payload=payload,
                    payload_json=payload_json,
                    created_at_ms=now_ms,
                    run_incarnation=identity.run_incarnation,
                    event_key=identity.event_key,
                )
                cur = self._conn.execute(
                    """
                    UPDATE agui_runs
                    SET status = ?, terminal_reason = ?, updated_at_ms = ?,
                        finished_at_ms = ?, result_seq = ?, worker_pid = NULL,
                        worker_pid_started_at_ms = NULL,
                        worker_lease_expires_at_ms = NULL,
                        worker_heartbeat_at_ms = NULL
                    WHERE run_id = ? AND status = 'running'
                      AND supervisor_id = ?
                      AND attempt_started_at_ms = ?
                    """,
                    (
                        terminal_status,
                        terminal_reason,
                        now_ms,
                        now_ms,
                        seq,
                        run_id,
                        expected_supervisor_id,
                        expected_attempt_generation,
                    ),
                )
                if cur.rowcount != 1:
                    raise WorkflowSupervisorOwnershipConflictError(
                        "lifecycle claim ownership changed"
                    )
                self._conn.execute(
                    "DELETE FROM workflow_run_specs WHERE run_id = ?",
                    (run_id,),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return seq

    def expire_queued_and_stale_leases(
        self,
        now_ms: int,
        reaper_supervisor_id: str,
        reaper_lease_expires_at_ms: int,
    ) -> WorkflowExpiryResult:
        now_ms = _require_positive_integer(now_ms, field_name="now_ms")
        reaper_supervisor_id = require_canonical_nonblank(
            reaper_supervisor_id,
            field_name="reaper_supervisor_id",
        )
        reaper_lease_expires_at_ms = _require_positive_integer(
            reaper_lease_expires_at_ms,
            field_name="reaper_lease_expires_at_ms",
        )
        if reaper_lease_expires_at_ms <= now_ms:
            raise ValueError("reaper_lease_expires_at_ms must be after now_ms")
        changed = 0
        terminal_decisions: list[WorkflowExpiryDecision] = []
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                expired_queued = list(
                    self._conn.execute(
                        """
                        SELECT run_id, run_kind, deadline_at_ms,
                               cancel_requested_at_ms
                        FROM agui_runs
                        WHERE status = 'queued'
                          AND (
                              deadline_at_ms <= ?
                              OR cancel_requested_at_ms IS NOT NULL
                          )
                        """,
                        (now_ms,),
                    )
                )
                for run_id, run_kind, deadline_at_ms, cancel_requested_at_ms in (
                    expired_queued
                ):
                    if cancel_requested_at_ms is not None:
                        terminal_status = "cancelled"
                        terminal_reason = "CANCEL_REQUESTED"
                    else:
                        assert deadline_at_ms is not None
                        terminal_status = "timed_out"
                        terminal_reason = "DEADLINE_EXCEEDED"
                    if str(run_kind) == "text_to_sql":
                        cur = self._conn.execute(
                            """
                            UPDATE agui_runs
                            SET updated_at_ms = ?
                            WHERE run_id = ? AND status = 'queued'
                            """,
                            (now_ms, run_id),
                        )
                        terminal_decisions.append(
                            WorkflowExpiryDecision(
                                run_id=str(run_id),
                                run_kind="text_to_sql",
                                action="terminalize",
                                terminal_status=terminal_status,
                                terminal_reason=terminal_reason,
                                worker_pid=None,
                                supervisor_id=None,
                                attempt_generation=None,
                                worker_lease_expires_at_ms=None,
                                worker_pid_started_at_ms=None,
                            )
                        )
                    else:
                        cur = self._conn.execute(
                            """
                            UPDATE agui_runs
                            SET status = ?, terminal_reason = ?,
                                updated_at_ms = ?, finished_at_ms = ?
                            WHERE run_id = ? AND status = 'queued'
                            """,
                            (
                                terminal_status,
                                terminal_reason,
                                now_ms,
                                now_ms,
                                run_id,
                            ),
                        )
                        if cur.rowcount == 1:
                            self._conn.execute(
                                "DELETE FROM workflow_run_specs WHERE run_id = ?",
                                (run_id,),
                            )
                    changed += int(cur.rowcount)
                stale_running = list(
                    self._conn.execute(
                        """
                        SELECT run_id, run_kind, deadline_at_ms,
                               cancel_requested_at_ms, worker_pid,
                               supervisor_id, worker_lease_expires_at_ms,
                               attempt_started_at_ms, worker_pid_started_at_ms
                        FROM agui_runs
                        WHERE status = 'running'
                          AND worker_lease_expires_at_ms <= ?
                        """,
                        (now_ms,),
                    )
                )
                for (
                    run_id,
                    run_kind,
                    deadline_at_ms,
                    cancel_requested_at_ms,
                    worker_pid,
                    supervisor_id,
                    lease_expires_at_ms,
                    attempt_generation,
                    worker_pid_started_at_ms,
                ) in stale_running:
                    terminal_status = None
                    terminal_reason = None
                    if cancel_requested_at_ms is not None:
                        terminal_status = "cancelled"
                        terminal_reason = "CANCEL_REQUESTED"
                    elif deadline_at_ms is not None and int(deadline_at_ms) <= now_ms:
                        terminal_status = "timed_out"
                        terminal_reason = "DEADLINE_EXCEEDED"
                    next_generation = max(
                        now_ms,
                        1
                        if attempt_generation is None
                        else int(attempt_generation) + 1,
                    )
                    cur = self._conn.execute(
                        """
                        UPDATE agui_runs
                        SET supervisor_id = ?, attempt_started_at_ms = ?,
                            worker_lease_expires_at_ms = ?,
                            worker_heartbeat_at_ms = ?, updated_at_ms = ?
                        WHERE run_id = ? AND status = 'running'
                          AND supervisor_id IS ?
                          AND attempt_started_at_ms IS ?
                          AND worker_pid IS ?
                          AND worker_lease_expires_at_ms = ?
                          AND worker_lease_expires_at_ms <= ?
                        """,
                        (
                            reaper_supervisor_id,
                            next_generation,
                            reaper_lease_expires_at_ms,
                            now_ms,
                            now_ms,
                            run_id,
                            supervisor_id,
                            attempt_generation,
                            worker_pid,
                            lease_expires_at_ms,
                            now_ms,
                        ),
                    )
                    if cur.rowcount != 1:
                        continue
                    changed += 1
                    terminal_decisions.append(
                        WorkflowExpiryDecision(
                            run_id=str(run_id),
                            run_kind=str(run_kind),
                            action=(
                                "terminalize"
                                if terminal_status is not None
                                else "reap_then_requeue"
                            ),
                            terminal_status=terminal_status,
                            terminal_reason=terminal_reason,
                            worker_pid=(
                                None if worker_pid is None else int(worker_pid)
                            ),
                            supervisor_id=reaper_supervisor_id,
                            attempt_generation=next_generation,
                            worker_lease_expires_at_ms=(
                                reaper_lease_expires_at_ms
                            ),
                            worker_pid_started_at_ms=(
                                None
                                if worker_pid_started_at_ms is None
                                else int(worker_pid_started_at_ms)
                            ),
                        )
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return WorkflowExpiryResult(
            changed_count=changed,
            terminal_decisions=tuple(terminal_decisions),
        )

    def requeue_stale_worker(
        self,
        run_id: str,
        expected_supervisor_id: str,
        expected_attempt_generation: int,
        expected_worker_pid: Optional[int],
        now_ms: int,
    ) -> bool:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        expected_supervisor_id = require_canonical_nonblank(
            expected_supervisor_id,
            field_name="expected_supervisor_id",
        )
        expected_attempt_generation = _require_positive_integer(
            expected_attempt_generation,
            field_name="expected_attempt_generation",
        )
        if expected_worker_pid is not None:
            expected_worker_pid = _require_positive_integer(
                expected_worker_pid,
                field_name="expected_worker_pid",
            )
        now_ms = _require_positive_integer(now_ms, field_name="now_ms")
        worker_clause = (
            "worker_pid IS NULL"
            if expected_worker_pid is None
            else "worker_pid = ?"
        )
        worker_parameters: tuple[Any, ...] = (
            () if expected_worker_pid is None else (expected_worker_pid,)
        )
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    f"""
                    UPDATE agui_runs
                    SET status = 'queued', supervisor_id = NULL,
                        worker_lease_expires_at_ms = NULL,
                        worker_heartbeat_at_ms = NULL,
                        worker_pid = NULL, worker_pid_started_at_ms = NULL,
                        updated_at_ms = ?
                    WHERE run_id = ? AND status = 'running'
                      AND supervisor_id = ?
                      AND attempt_started_at_ms = ?
                      AND {worker_clause}
                    """,
                    (
                        now_ms,
                        run_id,
                        expected_supervisor_id,
                        expected_attempt_generation,
                        *worker_parameters,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return cur.rowcount == 1

    def get_run_by_idempotency(
        self,
        *,
        principal: Principal,
        run_kind: str,
        idempotency_key: str,
    ) -> Optional[StoredRun]:
        run_kind = _require_run_kind(run_kind)
        idempotency_key = _require_idempotency_key(idempotency_key)
        record = principal_to_record(principal)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT run_id, thread_id, owner_subject, tenant_id, roles,
                       created_at_ms, run_kind, status, terminal_reason,
                       worker_pid, updated_at_ms, finished_at_ms, result_seq,
                       idempotency_key, request_fingerprint, queued_at_ms,
                       deadline_at_ms, supervisor_id,
                       worker_lease_expires_at_ms, worker_heartbeat_at_ms,
                       cancel_requested_at_ms, attempt_started_at_ms,
                       worker_pid_started_at_ms
                FROM agui_runs
                WHERE tenant_id = ? AND owner_subject = ?
                  AND run_kind = ? AND idempotency_key = ?
                """,
                (
                    record["tenant_id"],
                    record["subject"],
                    run_kind,
                    idempotency_key,
                ),
            ).fetchone()
        return None if row is None else self._stored_run_from_row(row)

    def transition_run(
        self,
        run_id: str,
        expected_statuses: Iterable[str],
        new_status: str,
        *,
        reason: Optional[str] = None,
        result_seq: Optional[int] = None,
    ) -> bool:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        expected = tuple(
            dict.fromkeys(
                _require_run_status(status, allow_legacy=True)
                for status in expected_statuses
            )
        )
        if not expected:
            raise ValueError("expected_statuses must not be empty")
        new_status = _require_run_status(new_status)
        if reason is not None:
            reason = require_canonical_nonblank(reason, field_name="reason")
        if result_seq is not None and (
            isinstance(result_seq, bool)
            or not isinstance(result_seq, int)
            or result_seq < 1
        ):
            raise ValueError("result_seq must be a positive integer")
        now_ms = int(time.time() * 1000)
        terminal = new_status in _TERMINAL_RUN_STATUSES
        placeholders = ", ".join("?" for _status in expected)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                lifecycle = self._conn.execute(
                    "SELECT run_kind FROM agui_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if (
                    terminal
                    and lifecycle is not None
                    and str(lifecycle[0]) == "text_to_sql"
                    and not (
                        new_status == "failed"
                        and reason == "SERVER_RESTARTED"
                        and result_seq is None
                    )
                ):
                    raise ValueError(
                        "Text-to-SQL terminal transitions require "
                        "finalize_run_with_event"
                    )
                cur = self._conn.execute(
                    f"""
                    UPDATE agui_runs
                    SET status = ?, terminal_reason = ?, updated_at_ms = ?,
                        finished_at_ms = ?, result_seq = ?
                    WHERE run_id = ? AND status IN ({placeholders})
                    """,
                    (
                        new_status,
                        reason if terminal else None,
                        now_ms,
                        now_ms if terminal else None,
                        result_seq,
                        run_id,
                        *expected,
                    ),
                )
                if terminal and cur.rowcount == 1:
                    self._conn.execute(
                        "DELETE FROM workflow_run_specs WHERE run_id = ?",
                        (run_id,),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return cur.rowcount == 1

    def set_worker_pid(
        self,
        run_id: str,
        worker_pid: int,
        expected_supervisor_id: Optional[str] = None,
        expected_attempt_generation: Optional[int] = None,
        worker_pid_started_at_ms: Optional[int] = None,
    ) -> bool:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        if (
            isinstance(worker_pid, bool)
            or not isinstance(worker_pid, int)
            or worker_pid < 1
        ):
            raise ValueError("worker_pid must be a positive integer")
        if worker_pid_started_at_ms is not None and (
            isinstance(worker_pid_started_at_ms, bool)
            or not isinstance(worker_pid_started_at_ms, int)
            or worker_pid_started_at_ms < 1
        ):
            raise ValueError(
                "worker_pid_started_at_ms must be a positive integer"
            )
        expected_supervisor_id = _require_optional_canonical_string(
            expected_supervisor_id,
            field_name="expected_supervisor_id",
        )
        if (expected_supervisor_id is None) != (
            expected_attempt_generation is None
        ):
            raise ValueError(
                "supervisor and attempt generation must be provided together"
            )
        if expected_attempt_generation is not None:
            expected_attempt_generation = _require_positive_integer(
                expected_attempt_generation,
                field_name="expected_attempt_generation",
            )
        now_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if expected_supervisor_id is None:
                    cur = self._conn.execute(
                        """
                        UPDATE agui_runs
                        SET worker_pid = ?, worker_pid_started_at_ms = ?,
                            status = 'running', updated_at_ms = ?
                        WHERE run_id = ?
                          AND status IN ('pending', 'queued', 'running')
                          AND (worker_pid IS NULL OR worker_pid = ?)
                          AND NOT EXISTS (
                              SELECT 1 FROM workflow_run_specs
                              WHERE workflow_run_specs.run_id = agui_runs.run_id
                          )
                        """,
                        (
                            worker_pid,
                            worker_pid_started_at_ms,
                            now_ms,
                            run_id,
                            worker_pid,
                        ),
                    )
                else:
                    cur = self._conn.execute(
                        """
                        UPDATE agui_runs
                        SET worker_pid = ?, worker_pid_started_at_ms = ?,
                            updated_at_ms = ?
                        WHERE run_id = ? AND status = 'running'
                          AND supervisor_id = ?
                          AND attempt_started_at_ms = ?
                          AND (worker_pid IS NULL OR worker_pid = ?)
                        """,
                        (
                            worker_pid,
                            worker_pid_started_at_ms,
                            now_ms,
                            run_id,
                            expected_supervisor_id,
                            expected_attempt_generation,
                            worker_pid,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return cur.rowcount == 1

    def list_non_terminal_runs(self) -> list[StoredRun]:
        placeholders = ", ".join("?" for _status in _NON_TERMINAL_RUN_STATUSES)
        with self._lock:
            rows = list(
                self._conn.execute(
                    f"""
                    SELECT run_id, thread_id, owner_subject, tenant_id, roles,
                           created_at_ms, run_kind, status, terminal_reason,
                           worker_pid, updated_at_ms, finished_at_ms, result_seq,
                           idempotency_key, request_fingerprint, queued_at_ms,
                           deadline_at_ms, supervisor_id,
                           worker_lease_expires_at_ms, worker_heartbeat_at_ms,
                           cancel_requested_at_ms, attempt_started_at_ms,
                           worker_pid_started_at_ms
                    FROM agui_runs
                    WHERE status IN ({placeholders})
                    ORDER BY created_at_ms, run_id
                    """,
                    tuple(sorted(_NON_TERMINAL_RUN_STATUSES)),
                )
            )
        return [self._stored_run_from_row(row) for row in rows]

    def reconcile_non_terminal_runs(
        self,
        *,
        reason: str = "SERVER_RESTARTED",
        exclude_run_ids: Iterable[str] = (),
    ) -> int:
        reason = require_canonical_nonblank(reason, field_name="reason")
        excluded = frozenset(
            require_canonical_nonblank(run_id, field_name="exclude_run_id")
            for run_id in exclude_run_ids
        )
        candidates = [
            stored
            for stored in self.list_non_terminal_runs()
            if stored.run_id not in excluded
        ]
        if not candidates:
            return 0
        now_ms = int(time.time() * 1000)
        reconciled = 0
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for stored in candidates:
                    if stored.worker_pid is None:
                        worker_clause = "worker_pid IS NULL"
                        worker_parameters: tuple[Any, ...] = ()
                    else:
                        worker_clause = "worker_pid = ?"
                        worker_parameters = (stored.worker_pid,)
                    cur = self._conn.execute(
                        f"""
                        UPDATE agui_runs
                        SET status = 'failed', terminal_reason = ?,
                            updated_at_ms = ?, finished_at_ms = ?
                        WHERE run_id = ? AND status = ? AND {worker_clause}
                        """,
                        (
                            reason,
                            now_ms,
                            now_ms,
                            stored.run_id,
                            stored.status,
                            *worker_parameters,
                        ),
                    )
                    if cur.rowcount == 1:
                        self._conn.execute(
                            "DELETE FROM workflow_run_specs WHERE run_id = ?",
                            (stored.run_id,),
                        )
                    reconciled += int(cur.rowcount)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return reconciled

    def _insert_workflow_result_locked(
        self,
        *,
        run_id: str,
        payload: dict[str, Any],
        payload_json: str,
        created_at_ms: int,
        run_incarnation: str,
        event_key: str,
    ) -> int:
        reservation = self._conn.execute(
            """
            SELECT run_incarnation
            FROM workflow_run_invocations
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if reservation is None:
            raise ValueError(
                "keyed WORKFLOW_RESULT requires a reserved invocation"
            )
        if str(reservation[0]) != run_incarnation:
            raise ValueError(
                "WORKFLOW_RESULT incarnation does not match reservation"
            )
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO agui_events
                (run_id, seq, event_type, payload, created_at_ms,
                 run_incarnation, event_key)
            SELECT ?, COALESCE(MAX(seq), 0) + 1, 'WORKFLOW_RESULT', ?, ?, ?, ?
            FROM agui_events WHERE run_id = ?
            """,
            (
                run_id,
                payload_json,
                created_at_ms,
                run_incarnation,
                event_key,
                run_id,
            ),
        )
        if cur.rowcount == 1:
            seq_row = self._conn.execute(
                "SELECT seq FROM agui_events WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            assert seq_row is not None
            return int(seq_row[0])
        existing = self._conn.execute(
            """
            SELECT run_id, run_incarnation, event_type, payload, seq
            FROM agui_events
            WHERE event_key = ?
            """,
            (event_key,),
        ).fetchone()
        if existing is None:
            raise ValueError(
                "event_key insertion was ignored without an existing event"
            )
        try:
            existing_payload = _canonical_payload(json.loads(str(existing[3])))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "event_key collision has an invalid stored payload"
            ) from exc
        if (
            str(existing[0]) != run_id
            or existing[1] != run_incarnation
            or str(existing[2]) != "WORKFLOW_RESULT"
            or existing_payload != payload_json
        ):
            raise ValueError(
                "event_key collision does not match the original event"
            )
        return int(existing[4])

    def finalize_run_with_event(
        self,
        run_id: str,
        payload: dict[str, Any],
        *,
        expected_supervisor_id: Optional[str] | object = (
            _NO_SUPERVISOR_PRECONDITION
        ),
        expected_attempt_generation: Optional[int] | object = (
            _NO_ATTEMPT_GENERATION_PRECONDITION
        ),
    ) -> int:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        if (
            expected_supervisor_id is not _NO_SUPERVISOR_PRECONDITION
            and expected_supervisor_id is not None
        ):
            expected_supervisor_id = require_canonical_nonblank(
                expected_supervisor_id,
                field_name="expected_supervisor_id",
            )
        supervisor_precondition = (
            expected_supervisor_id is not _NO_SUPERVISOR_PRECONDITION
        )
        generation_precondition = (
            expected_attempt_generation
            is not _NO_ATTEMPT_GENERATION_PRECONDITION
        )
        if supervisor_precondition != generation_precondition:
            raise ValueError(
                "supervisor and attempt generation preconditions must be provided together"
            )
        if expected_attempt_generation is not _NO_ATTEMPT_GENERATION_PRECONDITION:
            if expected_attempt_generation is not None:
                expected_attempt_generation = _require_positive_integer(
                    expected_attempt_generation,
                    field_name="expected_attempt_generation",
                )
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        identity = workflow_result_identity_from_payload(
            payload,
            expected_run_id=run_id,
        )
        terminal_outcome = payload.get("terminal_outcome")
        if not isinstance(terminal_outcome, dict):
            raise ValueError(
                "Text-to-SQL WORKFLOW_RESULT requires terminal_outcome"
            )
        try:
            terminal_result = TextToSqlTerminalResult.from_mapping(
                terminal_outcome
            )
            terminal_outcome = terminal_result.to_mapping()
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Text-to-SQL WORKFLOW_RESULT has an invalid terminal_outcome"
            ) from exc
        if terminal_outcome["run_id"] != run_id:
            raise ValueError("terminal_outcome run_id does not match run_id")
        terminal_status = _require_run_status(terminal_outcome["status"])
        if terminal_status not in _TERMINAL_RUN_STATUSES:
            raise ValueError("WORKFLOW_RESULT status must be terminal")
        expected_legacy_status = (
            "completed"
            if terminal_status == "succeeded"
            else "cancelled"
            if terminal_status == "cancelled"
            else "failed"
        )
        if payload.get("status") != expected_legacy_status:
            raise ValueError(
                "WORKFLOW_RESULT status is inconsistent with terminal_outcome"
            )
        expected_success = terminal_status == "succeeded"
        if payload.get("success") is not expected_success:
            raise ValueError(
                "WORKFLOW_RESULT success is inconsistent with terminal_outcome"
            )
        artifacts = payload.get("artifacts")
        terminal_copies = [
            ("result", payload.get("result"), False),
            (
                "artifacts.terminal_outcome",
                artifacts.get("terminal_outcome")
                if isinstance(artifacts, dict)
                else None,
                True,
            ),
            (
                "artifacts.final_output",
                artifacts.get("final_output")
                if isinstance(artifacts, dict)
                else None,
                False,
            ),
        ]
        for field_name, value, reject_invalid in terminal_copies:
            if value is None:
                continue
            try:
                terminal_copy = TextToSqlTerminalResult.from_mapping(
                    value
                ).to_mapping()
            except (TypeError, ValueError) as exc:
                if reject_invalid:
                    raise ValueError(
                        f"{field_name} must be a valid terminal outcome"
                    ) from exc
                continue
            if terminal_copy != terminal_outcome:
                raise ValueError(f"{field_name} does not match terminal_outcome")
        raw_reason = terminal_outcome.get("reason_code")
        terminal_reason = (
            None
            if raw_reason is None or raw_reason == ""
            else require_canonical_nonblank(raw_reason, field_name="reason_code")
        )
        payload_json = _canonical_payload(payload)
        history_dialect, history_profile_name = _text_to_sql_history_dimensions(
            payload,
            terminal_result,
        )
        now_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._get_run_row_locked(run_id)
                if row is None:
                    raise ValueError(f"run not found: {run_id}")
                stored = self._stored_run_from_row(row)
                if stored.run_kind != "text_to_sql":
                    raise ValueError("run is not a Text-to-SQL lifecycle")
                if stored.status in _TERMINAL_RUN_STATUSES:
                    if supervisor_precondition and (
                        expected_supervisor_id is None
                        or expected_attempt_generation is None
                        or stored.supervisor_id != expected_supervisor_id
                        or stored.attempt_started_at_ms
                        != expected_attempt_generation
                    ):
                        raise WorkflowSupervisorOwnershipConflictError(
                            "terminal lifecycle claim ownership changed"
                        )
                    if stored.result_seq is None:
                        raise ValueError(
                            "terminal lifecycle has no primary WORKFLOW_RESULT"
                        )
                    winner = self._conn.execute(
                        """
                        SELECT event_type, payload, created_at_ms
                        FROM agui_events
                        WHERE run_id = ? AND seq = ?
                        """,
                        (run_id, stored.result_seq),
                    ).fetchone()
                    if winner is None or str(winner[0]) != "WORKFLOW_RESULT":
                        raise ValueError(
                            "terminal lifecycle result_seq is not WORKFLOW_RESULT"
                        )
                    try:
                        winner_payload = _canonical_payload(
                            json.loads(str(winner[1]))
                        )
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            "terminal lifecycle winner has an invalid payload"
                        ) from exc
                    if winner_payload == payload_json:
                        self._upsert_text_to_sql_history_locked(
                            tenant_id=stored.tenant_id,
                            owner_subject=stored.owner_subject,
                            terminal_result=terminal_result,
                            created_at_ms=int(winner[2]),
                            dialect=history_dialect,
                            profile_name=history_profile_name,
                        )
                        self._conn.execute(
                            "DELETE FROM workflow_run_specs WHERE run_id = ?",
                            (run_id,),
                        )
                        self._conn.commit()
                        return stored.result_seq
                    raise TerminalWorkflowResultConflictError(
                        "terminal lifecycle winner conflicts with WORKFLOW_RESULT"
                    )
                if supervisor_precondition:
                    if stored.status in {"running", "result_pending"}:
                        if (
                            expected_supervisor_id is None
                            or stored.supervisor_id != expected_supervisor_id
                            or expected_attempt_generation is None
                            or stored.attempt_started_at_ms
                            != expected_attempt_generation
                        ):
                            raise WorkflowSupervisorOwnershipConflictError(
                                "lifecycle claim ownership changed"
                            )
                    elif stored.status in {"pending", "queued"}:
                        if (
                            expected_supervisor_id is not None
                            or stored.supervisor_id is not None
                            or expected_attempt_generation is not None
                        ):
                            raise WorkflowSupervisorOwnershipConflictError(
                                "unclaimed lifecycle requires null supervisor"
                            )
                    else:
                        raise WorkflowSupervisorOwnershipConflictError(
                            "lifecycle is not eligible for supervisor finalization"
                        )
                seq = self._insert_workflow_result_locked(
                    run_id=run_id,
                    payload=payload,
                    payload_json=payload_json,
                    created_at_ms=now_ms,
                    run_incarnation=identity.run_incarnation,
                    event_key=identity.event_key,
                )
                if stored.status not in _NON_TERMINAL_RUN_STATUSES:
                    raise ValueError(
                        f"run cannot transition from status {stored.status}"
                    )
                cur = self._conn.execute(
                    """
                    UPDATE agui_runs
                    SET status = ?, terminal_reason = ?, updated_at_ms = ?,
                        finished_at_ms = ?, result_seq = ?
                    WHERE run_id = ? AND status = ?
                    """,
                    (
                        terminal_status,
                        terminal_reason,
                        now_ms,
                        now_ms,
                        seq,
                        run_id,
                        stored.status,
                    ),
                )
                if cur.rowcount != 1:
                    raise TerminalWorkflowResultConflictError(
                        "terminal lifecycle CAS lost"
                    )
                self._upsert_text_to_sql_history_locked(
                    tenant_id=stored.tenant_id,
                    owner_subject=stored.owner_subject,
                    terminal_result=terminal_result,
                    created_at_ms=now_ms,
                    dialect=history_dialect,
                    profile_name=history_profile_name,
                )
                self._conn.execute(
                    "DELETE FROM workflow_run_specs WHERE run_id = ?",
                    (run_id,),
                )
                self._conn.commit()
                return seq
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _text_to_sql_history_entry_from_row(
        row: tuple[Any, ...],
    ) -> TextToSqlHistoryEntry:
        tenant_id = require_canonical_nonblank(row[0], field_name="tenant_id")
        owner_subject = require_canonical_nonblank(
            row[1],
            field_name="owner_subject",
        )
        run_id = require_canonical_nonblank(row[2], field_name="run_id")
        created_at_ms = _require_non_negative_integer(
            int(row[3]),
            field_name="created_at_ms",
        )
        try:
            status = TextToSqlTerminalStatus(str(row[4]))
        except ValueError as exc:
            raise ValueError("stored history status is invalid") from exc
        dialect = _require_history_dimension(row[5], field_name="dialect")
        profile_name = _require_history_dimension(
            row[6],
            field_name="profile_name",
        )
        try:
            snapshot = json.loads(str(row[7]))
            terminal_result = TextToSqlTerminalResult.from_mapping(snapshot)
        except (TypeError, ValueError) as exc:
            raise ValueError("stored terminal history snapshot is invalid") from exc
        if terminal_result.run_id != run_id:
            raise ValueError("stored history run_id does not match terminal snapshot")
        if terminal_result.status is not status:
            raise ValueError("stored history status does not match terminal snapshot")
        return TextToSqlHistoryEntry(
            tenant_id=tenant_id,
            owner_subject=owner_subject,
            run_id=run_id,
            created_at_ms=created_at_ms,
            status=status,
            dialect=dialect,
            profile_name=profile_name,
            terminal_result=terminal_result,
        )

    @staticmethod
    def _history_principal_scope(principal: Principal) -> tuple[str, str]:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        record = principal_to_record(principal)
        tenant_id = require_canonical_nonblank(
            record["tenant_id"],
            field_name="tenant_id",
        )
        owner_subject = require_canonical_nonblank(
            record["subject"],
            field_name="owner_subject",
        )
        return tenant_id, owner_subject

    @staticmethod
    def _require_history_admin(principal: Principal) -> None:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not principal.has_role("admin"):
            raise PermissionError("admin role is required for cross-owner history")

    @staticmethod
    def _admin_history_scope(
        tenant_id: Optional[str],
        owner_subject: Optional[str],
    ) -> tuple[str, tuple[str, ...]]:
        if owner_subject is not None and tenant_id is None:
            raise ValueError("tenant_id is required with owner_subject")
        clauses: list[str] = []
        parameters: list[str] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            parameters.append(
                require_canonical_nonblank(tenant_id, field_name="tenant_id")
            )
        if owner_subject is not None:
            clauses.append("owner_subject = ?")
            parameters.append(
                require_canonical_nonblank(
                    owner_subject,
                    field_name="owner_subject",
                )
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return where, tuple(parameters)

    def _upsert_text_to_sql_history_locked(
        self,
        *,
        tenant_id: str,
        owner_subject: str,
        terminal_result: TextToSqlTerminalResult,
        created_at_ms: int,
        dialect: str,
        profile_name: str,
    ) -> tuple[TextToSqlHistoryEntry, bool]:
        snapshot_json = _canonical_payload(terminal_result.to_mapping())
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO text_to_sql_history (
                tenant_id, owner_subject, run_id, created_at_ms, status,
                dialect, profile_name, terminal_snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                owner_subject,
                terminal_result.run_id,
                created_at_ms,
                terminal_result.status.value,
                dialect,
                profile_name,
                snapshot_json,
            ),
        )
        row = self._conn.execute(
            """
            SELECT tenant_id, owner_subject, run_id, created_at_ms, status,
                   dialect, profile_name, terminal_snapshot_json
            FROM text_to_sql_history
            WHERE tenant_id = ? AND owner_subject = ? AND run_id = ?
            """,
            (tenant_id, owner_subject, terminal_result.run_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("history upsert did not produce a row")
        stored = self._text_to_sql_history_entry_from_row(row)
        expected = TextToSqlHistoryEntry(
            tenant_id=tenant_id,
            owner_subject=owner_subject,
            run_id=terminal_result.run_id,
            created_at_ms=created_at_ms,
            status=terminal_result.status,
            dialect=dialect,
            profile_name=profile_name,
            terminal_result=terminal_result,
        )
        if stored.to_mapping() != expected.to_mapping():
            raise TextToSqlHistoryConflictError(
                "history key already contains a different terminal projection"
            )
        return stored, cursor.rowcount == 1

    def upsert_text_to_sql_history(
        self,
        principal: Principal,
        terminal_result: TextToSqlTerminalResult,
        *,
        created_at_ms: int,
        dialect: str,
        profile_name: str,
    ) -> TextToSqlHistoryEntry:
        tenant_id, owner_subject = self._history_principal_scope(principal)
        if not isinstance(terminal_result, TextToSqlTerminalResult):
            raise TypeError("terminal_result must be a TextToSqlTerminalResult")
        created_at_ms = _require_non_negative_integer(
            created_at_ms,
            field_name="created_at_ms",
        )
        dialect = _require_history_dimension(dialect, field_name="dialect")
        profile_name = _require_history_dimension(
            profile_name,
            field_name="profile_name",
        )
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                owner_row = self._conn.execute(
                    """
                    SELECT tenant_id, owner_subject, run_kind, status
                    FROM agui_runs
                    WHERE run_id = ?
                    """,
                    (terminal_result.run_id,),
                ).fetchone()
                if owner_row is None:
                    raise ValueError(
                        f"run not found: {terminal_result.run_id}"
                    )
                if (
                    str(owner_row[0]) != tenant_id
                    or str(owner_row[1]) != owner_subject
                ):
                    raise PermissionError(
                        "history run owner does not match principal owner"
                    )
                if str(owner_row[2]) != "text_to_sql":
                    raise ValueError("history run is not a Text-to-SQL lifecycle")
                if str(owner_row[3]) != terminal_result.status.value:
                    raise ValueError(
                        "history terminal status does not match authoritative run"
                    )
                entry, _inserted = self._upsert_text_to_sql_history_locked(
                    tenant_id=tenant_id,
                    owner_subject=owner_subject,
                    terminal_result=terminal_result,
                    created_at_ms=created_at_ms,
                    dialect=dialect,
                    profile_name=profile_name,
                )
                self._conn.commit()
                return entry
            except Exception:
                self._conn.rollback()
                raise

    def _list_text_to_sql_history_locked(
        self,
        where: str,
        parameters: tuple[Any, ...],
        *,
        limit: int,
        offset: int,
    ) -> list[TextToSqlHistoryEntry]:
        rows = self._conn.execute(
            """
            SELECT tenant_id, owner_subject, run_id, created_at_ms, status,
                   dialect, profile_name, terminal_snapshot_json
            FROM text_to_sql_history
            """
            + where
            + """
            ORDER BY created_at_ms DESC, tenant_id ASC, owner_subject ASC,
                     run_id DESC
            LIMIT ? OFFSET ?
            """,
            (*parameters, limit, offset),
        ).fetchall()
        return [self._text_to_sql_history_entry_from_row(row) for row in rows]

    def list_text_to_sql_history(
        self,
        principal: Principal,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TextToSqlHistoryEntry]:
        tenant_id, owner_subject = self._history_principal_scope(principal)
        limit, offset = _require_history_page(limit, offset)
        with self._lock:
            return self._list_text_to_sql_history_locked(
                " WHERE tenant_id = ? AND owner_subject = ?",
                (tenant_id, owner_subject),
                limit=limit,
                offset=offset,
            )

    def admin_list_text_to_sql_history(
        self,
        principal: Principal,
        *,
        tenant_id: Optional[str] = None,
        owner_subject: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TextToSqlHistoryEntry]:
        self._require_history_admin(principal)
        limit, offset = _require_history_page(limit, offset)
        where, parameters = self._admin_history_scope(
            tenant_id,
            owner_subject,
        )
        with self._lock:
            return self._list_text_to_sql_history_locked(
                where,
                parameters,
                limit=limit,
                offset=offset,
            )

    def _delete_text_to_sql_history(
        self,
        where: str,
        parameters: tuple[Any, ...],
    ) -> int:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cursor = self._conn.execute(
                    "DELETE FROM text_to_sql_history" + where,
                    parameters,
                )
                self._conn.commit()
                return int(cursor.rowcount)
            except Exception:
                self._conn.rollback()
                raise

    def clear_text_to_sql_history(self, principal: Principal) -> int:
        tenant_id, owner_subject = self._history_principal_scope(principal)
        return self._delete_text_to_sql_history(
            " WHERE tenant_id = ? AND owner_subject = ?",
            (tenant_id, owner_subject),
        )

    def admin_clear_text_to_sql_history(
        self,
        principal: Principal,
        *,
        tenant_id: Optional[str] = None,
        owner_subject: Optional[str] = None,
    ) -> int:
        self._require_history_admin(principal)
        where, parameters = self._admin_history_scope(
            tenant_id,
            owner_subject,
        )
        return self._delete_text_to_sql_history(where, parameters)

    def _text_to_sql_history_analytics_locked(
        self,
        where: str,
        parameters: tuple[Any, ...],
    ) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT status, dialect, profile_name FROM text_to_sql_history"
            + where,
            parameters,
        ).fetchall()

        def counts(index: int) -> dict[str, int]:
            values: dict[str, int] = {}
            for row in rows:
                key = str(row[index])
                values[key] = values.get(key, 0) + 1
            return {key: values[key] for key in sorted(values)}

        return {
            "total": len(rows),
            "statuses": counts(0),
            "dialects": counts(1),
            "profiles": counts(2),
        }

    def text_to_sql_history_analytics(
        self,
        principal: Principal,
    ) -> dict[str, Any]:
        tenant_id, owner_subject = self._history_principal_scope(principal)
        with self._lock:
            return self._text_to_sql_history_analytics_locked(
                " WHERE tenant_id = ? AND owner_subject = ?",
                (tenant_id, owner_subject),
            )

    def admin_text_to_sql_history_analytics(
        self,
        principal: Principal,
        *,
        tenant_id: Optional[str] = None,
        owner_subject: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require_history_admin(principal)
        where, parameters = self._admin_history_scope(
            tenant_id,
            owner_subject,
        )
        with self._lock:
            return self._text_to_sql_history_analytics_locked(
                where,
                parameters,
            )

    def prune_text_to_sql_history(
        self,
        principal: Principal,
        *,
        before_created_at_ms: int,
    ) -> int:
        tenant_id, owner_subject = self._history_principal_scope(principal)
        cutoff = _require_non_negative_integer(
            before_created_at_ms,
            field_name="before_created_at_ms",
        )
        return self._delete_text_to_sql_history(
            " WHERE tenant_id = ? AND owner_subject = ? AND created_at_ms < ?",
            (tenant_id, owner_subject, cutoff),
        )

    def admin_prune_text_to_sql_history(
        self,
        principal: Principal,
        *,
        before_created_at_ms: int,
        tenant_id: Optional[str] = None,
        owner_subject: Optional[str] = None,
    ) -> int:
        self._require_history_admin(principal)
        cutoff = _require_non_negative_integer(
            before_created_at_ms,
            field_name="before_created_at_ms",
        )
        where, parameters = self._admin_history_scope(
            tenant_id,
            owner_subject,
        )
        separator = " AND " if where else " WHERE "
        return self._delete_text_to_sql_history(
            where + separator + "created_at_ms < ?",
            (*parameters, cutoff),
        )

    def _quarantine_text_to_sql_history_locked(
        self,
        reason: str,
        raw_payload: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO text_to_sql_history_quarantine
                (reason, raw_payload, quarantined_at_ms)
            VALUES (?, ?, ?)
            """,
            (reason, raw_payload, int(time.time() * 1000)),
        )

    def _import_legacy_text_to_sql_history_locked(
        self,
        lines: Iterable[str],
    ) -> LegacyTextToSqlHistoryImportResult:
        imported = 0
        duplicates = 0
        malformed = 0
        unowned = 0
        invalid = 0
        for line in lines:
            raw_payload = line if isinstance(line, str) else "<non-string>"
            try:
                record = json.loads(raw_payload)
                if not isinstance(record, dict):
                    raise ValueError("legacy history row must be an object")
                run_id = require_canonical_nonblank(
                    record.get("run_id"),
                    field_name="run_id",
                )
            except (TypeError, ValueError):
                malformed += 1
                self._quarantine_text_to_sql_history_locked(
                    "MALFORMED_JSON",
                    raw_payload,
                )
                continue
            owner_row = self._conn.execute(
                """
                SELECT tenant_id, owner_subject, run_kind, status
                FROM agui_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if (
                owner_row is None
                or str(owner_row[2]) != "text_to_sql"
                or str(owner_row[3]) not in _TERMINAL_RUN_STATUSES
            ):
                unowned += 1
                self._quarantine_text_to_sql_history_locked(
                    "UNOWNED_RUN",
                    raw_payload,
                )
                continue
            try:
                terminal_result = TextToSqlTerminalResult.from_mapping(
                    record.get("terminal_snapshot")
                )
                if terminal_result.run_id != run_id:
                    raise ValueError("terminal snapshot run_id mismatch")
                if terminal_result.status.value != str(owner_row[3]):
                    raise ValueError("terminal snapshot status mismatch")
                created_at_ms = _require_non_negative_integer(
                    record.get("created_at_ms"),
                    field_name="created_at_ms",
                )
                dialect = _require_history_dimension(
                    record.get("dialect"),
                    field_name="dialect",
                )
                profile_name = _require_history_dimension(
                    record.get("profile_name"),
                    field_name="profile_name",
                )
                _entry, inserted = self._upsert_text_to_sql_history_locked(
                    tenant_id=str(owner_row[0]),
                    owner_subject=str(owner_row[1]),
                    terminal_result=terminal_result,
                    created_at_ms=created_at_ms,
                    dialect=dialect,
                    profile_name=profile_name,
                )
            except (TypeError, ValueError):
                invalid += 1
                self._quarantine_text_to_sql_history_locked(
                    "INVALID_RECORD",
                    raw_payload,
                )
                continue
            if inserted:
                imported += 1
            else:
                duplicates += 1
        return LegacyTextToSqlHistoryImportResult(
            imported=imported,
            duplicates=duplicates,
            quarantined_malformed=malformed,
            quarantined_unowned=unowned,
            quarantined_invalid=invalid,
        )

    def import_legacy_text_to_sql_history(
        self,
        lines: Iterable[str],
    ) -> LegacyTextToSqlHistoryImportResult:
        if isinstance(lines, (str, bytes)):
            raise TypeError("legacy history lines must be an iterable of lines")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                result = self._import_legacy_text_to_sql_history_locked(lines)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return result

    def import_legacy_text_to_sql_history_once(
        self,
        lines: Iterable[str],
        *,
        source_identity: str,
        source_path: str,
        content_sha256: str,
    ) -> LegacyTextToSqlHistoryImportResult:
        if isinstance(lines, (str, bytes)):
            raise TypeError("legacy history lines must be an iterable of lines")
        source_identity = _require_sha256_digest(
            source_identity,
            field_name="source_identity",
        )
        source_path = _require_legacy_source_path(source_path)
        content_sha256 = _require_sha256_digest(
            content_sha256,
            field_name="content_sha256",
        )
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                completed = self._conn.execute(
                    """
                    SELECT source_path, content_sha256, imported, duplicates,
                           quarantined_malformed, quarantined_unowned,
                           quarantined_invalid
                    FROM text_to_sql_history_imports
                    WHERE source_identity = ?
                    """,
                    (source_identity,),
                ).fetchone()
                if completed is not None:
                    if (
                        str(completed[0]) != source_path
                        or str(completed[1]) != content_sha256
                    ):
                        raise LegacyTextToSqlHistoryImportConflictError(
                            "completed legacy history source identity changed"
                        )
                    result = LegacyTextToSqlHistoryImportResult(
                        imported=int(completed[2]),
                        duplicates=int(completed[3]),
                        quarantined_malformed=int(completed[4]),
                        quarantined_unowned=int(completed[5]),
                        quarantined_invalid=int(completed[6]),
                        already_completed=True,
                    )
                    self._conn.commit()
                    return result
                result = self._import_legacy_text_to_sql_history_locked(lines)
                self._conn.execute(
                    """
                    INSERT INTO text_to_sql_history_imports (
                        source_identity, source_path, content_sha256,
                        completed_at_ms, imported, duplicates,
                        quarantined_malformed, quarantined_unowned,
                        quarantined_invalid
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_identity,
                        source_path,
                        content_sha256,
                        int(time.time() * 1000),
                        result.imported,
                        result.duplicates,
                        result.quarantined_malformed,
                        result.quarantined_unowned,
                        result.quarantined_invalid,
                    ),
                )
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def admin_text_to_sql_history_quarantine_counts(
        self,
        principal: Principal,
    ) -> dict[str, int]:
        self._require_history_admin(principal)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT reason, COUNT(*)
                FROM text_to_sql_history_quarantine
                GROUP BY reason
                ORDER BY reason ASC
                """
            ).fetchall()
        return {str(reason): int(count) for reason, count in rows}

    @staticmethod
    def _operational_retention_state_from_row(
        row: tuple[Any, ...],
    ) -> OperationalRetentionState:
        try:
            counters = json.loads(str(row[8]))
        except (TypeError, ValueError) as exc:
            raise ValueError("stored retention counters are invalid") from exc
        if not isinstance(counters, dict):
            raise ValueError("stored retention counters must be an object")
        _require_json_primitives(counters, field_name="retention counters")
        status = str(row[7])
        if status not in {"never", "running", "succeeded", "failed"}:
            raise ValueError("stored retention status is invalid")
        return OperationalRetentionState(
            scope=require_canonical_nonblank(row[0], field_name="scope"),
            lease_owner=None if row[1] is None else str(row[1]),
            lease_generation=_require_non_negative_integer(
                int(row[2]),
                field_name="lease_generation",
            ),
            lease_expires_at_ms=None if row[3] is None else int(row[3]),
            last_attempt_at_ms=None if row[4] is None else int(row[4]),
            last_success_at_ms=None if row[5] is None else int(row[5]),
            next_due_at_ms=_require_non_negative_integer(
                int(row[6]),
                field_name="next_due_at_ms",
            ),
            status=status,
            counters=dict(counters),
            last_error=None if row[9] is None else str(row[9]),
            updated_at_ms=_require_non_negative_integer(
                int(row[10]),
                field_name="updated_at_ms",
            ),
        )

    def _get_operational_retention_state_locked(
        self,
        scope: str,
    ) -> Optional[OperationalRetentionState]:
        row = self._conn.execute(
            """
            SELECT scope, lease_owner, lease_generation, lease_expires_at_ms,
                   last_attempt_at_ms, last_success_at_ms, next_due_at_ms,
                   status, counters_json, last_error, updated_at_ms
            FROM operational_retention_state
            WHERE scope = ?
            """,
            (scope,),
        ).fetchone()
        return (
            None
            if row is None
            else self._operational_retention_state_from_row(row)
        )

    def get_operational_retention_state(
        self,
        scope: str,
    ) -> Optional[OperationalRetentionState]:
        scope = _require_history_dimension(scope, field_name="scope")
        with self._lock:
            return self._get_operational_retention_state_locked(scope)

    def claim_operational_retention(
        self,
        scope: str,
        owner_id: str,
        *,
        now_ms: int,
        lease_duration_ms: int,
    ) -> Optional[OperationalRetentionLease]:
        scope = _require_history_dimension(scope, field_name="scope")
        owner_id = _require_history_dimension(owner_id, field_name="owner_id")
        now_ms = _require_non_negative_integer(now_ms, field_name="now_ms")
        lease_duration_ms = _require_positive_integer(
            lease_duration_ms,
            field_name="lease_duration_ms",
        )
        expires_at_ms = now_ms + lease_duration_ms
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO operational_retention_state
                        (scope, next_due_at_ms, status, counters_json,
                         updated_at_ms)
                    VALUES (?, 0, 'never', '{}', ?)
                    """,
                    (scope, now_ms),
                )
                cursor = self._conn.execute(
                    """
                    UPDATE operational_retention_state
                    SET lease_owner = ?,
                        lease_generation = lease_generation + 1,
                        lease_expires_at_ms = ?,
                        last_attempt_at_ms = ?,
                        status = 'running',
                        last_error = NULL,
                        updated_at_ms = ?
                    WHERE scope = ?
                      AND next_due_at_ms <= ?
                      AND (
                          lease_owner IS NULL
                          OR lease_expires_at_ms IS NULL
                          OR lease_expires_at_ms <= ?
                      )
                    """,
                    (
                        owner_id,
                        expires_at_ms,
                        now_ms,
                        now_ms,
                        scope,
                        now_ms,
                        now_ms,
                    ),
                )
                if cursor.rowcount != 1:
                    self._conn.commit()
                    return None
                state = self._get_operational_retention_state_locked(scope)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        assert state is not None
        return OperationalRetentionLease(
            scope=scope,
            owner_id=owner_id,
            generation=state.lease_generation,
            expires_at_ms=expires_at_ms,
        )

    def complete_operational_retention(
        self,
        lease: OperationalRetentionLease,
        *,
        now_ms: int,
        next_due_at_ms: int,
        success: bool,
        counters: dict[str, Any],
        error: Optional[str] = None,
    ) -> OperationalRetentionState:
        if not isinstance(lease, OperationalRetentionLease):
            raise TypeError("lease must be an OperationalRetentionLease")
        now_ms = _require_non_negative_integer(now_ms, field_name="now_ms")
        next_due_at_ms = _require_positive_integer(
            next_due_at_ms,
            field_name="next_due_at_ms",
        )
        if next_due_at_ms <= now_ms:
            raise ValueError("next_due_at_ms must be after now_ms")
        if type(success) is not bool:
            raise TypeError("success must be a boolean")
        if not isinstance(counters, dict):
            raise TypeError("counters must be an object")
        _require_json_primitives(counters, field_name="retention counters")
        counters_json = _canonical_payload(counters)
        if success:
            if error is not None:
                raise ValueError("successful retention cannot contain error")
            status = "succeeded"
        else:
            error = require_canonical_nonblank(error, field_name="error")
            status = "failed"
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cursor = self._conn.execute(
                    """
                    UPDATE operational_retention_state
                    SET lease_owner = NULL,
                        lease_expires_at_ms = NULL,
                        last_success_at_ms = CASE
                            WHEN ? THEN ? ELSE last_success_at_ms END,
                        next_due_at_ms = ?,
                        status = ?,
                        counters_json = ?,
                        last_error = ?,
                        updated_at_ms = ?
                    WHERE scope = ?
                      AND lease_owner = ?
                      AND lease_generation = ?
                      AND lease_expires_at_ms > ?
                    """,
                    (
                        int(success),
                        now_ms,
                        next_due_at_ms,
                        status,
                        counters_json,
                        error,
                        now_ms,
                        lease.scope,
                        lease.owner_id,
                        lease.generation,
                        now_ms,
                    ),
                )
                if cursor.rowcount != 1:
                    raise OperationalRetentionLeaseConflictError(
                        "retention lease ownership changed or expired"
                    )
                state = self._get_operational_retention_state_locked(
                    lease.scope
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        assert state is not None
        return state

    def renew_operational_retention(
        self,
        lease: OperationalRetentionLease,
        *,
        now_ms: int,
        lease_duration_ms: int,
    ) -> OperationalRetentionLease:
        if not isinstance(lease, OperationalRetentionLease):
            raise TypeError("lease must be an OperationalRetentionLease")
        now_ms = _require_non_negative_integer(now_ms, field_name="now_ms")
        lease_duration_ms = _require_positive_integer(
            lease_duration_ms,
            field_name="lease_duration_ms",
        )
        requested_expires_at_ms = now_ms + lease_duration_ms
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cursor = self._conn.execute(
                    """
                    UPDATE operational_retention_state
                    SET lease_expires_at_ms = MAX(
                            lease_expires_at_ms,
                            ?
                        ),
                        updated_at_ms = ?
                    WHERE scope = ?
                      AND lease_owner = ?
                      AND lease_generation = ?
                      AND lease_expires_at_ms > ?
                    """,
                    (
                        requested_expires_at_ms,
                        now_ms,
                        lease.scope,
                        lease.owner_id,
                        lease.generation,
                        now_ms,
                    ),
                )
                if cursor.rowcount != 1:
                    raise OperationalRetentionLeaseConflictError(
                        "retention lease ownership changed or expired"
                    )
                state = self._get_operational_retention_state_locked(
                    lease.scope
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        assert state is not None and state.lease_expires_at_ms is not None
        return OperationalRetentionLease(
            scope=lease.scope,
            owner_id=lease.owner_id,
            generation=lease.generation,
            expires_at_ms=state.lease_expires_at_ms,
        )

    def get_run_principal(self, run_id: str) -> Optional[Principal]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT owner_subject, tenant_id, roles
                FROM agui_runs
                WHERE run_id = ?
                """,
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        subject, tenant_id, roles_json = row
        roles = json.loads(roles_json)
        return principal_from_record(
            {"subject": subject, "tenant_id": tenant_id, "roles": roles}
        )

    def get_workflow_run_invocation(
        self,
        run_id: str,
    ) -> Optional[WorkflowRunInvocation]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT run_id, run_incarnation, session_id, workflow_name
                FROM workflow_run_invocations
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkflowRunInvocation(
            run_id=str(row[0]),
            run_incarnation=str(row[1]),
            session_id=str(row[2]),
            workflow_name=str(row[3]),
        )

    def get_run_thread_id(self, run_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT thread_id FROM agui_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def append(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_type must be a non-empty string")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        created_at_ms = int(time.time() * 1000)
        payload_json = _canonical_payload(payload)

        workflow_identity = None
        if event_type == "WORKFLOW_RESULT":
            workflow_identity = workflow_result_identity_from_payload(
                payload,
                expected_run_id=run_id,
            )
            run_incarnation = workflow_identity.run_incarnation
            event_key = workflow_identity.event_key
        else:
            raw_run_incarnation = payload.get("run_incarnation")
            if raw_run_incarnation is None:
                run_incarnation = None
            else:
                run_incarnation = require_canonical_nonblank(
                    raw_run_incarnation,
                    field_name="run_incarnation",
                )
            raw_event_key = payload.get("event_key")
            if raw_event_key is None:
                event_key = None
            else:
                event_key = require_canonical_nonblank(
                    raw_event_key,
                    field_name="event_key",
                )

        if workflow_identity is not None:
            with self._lock:
                lifecycle_row = self._conn.execute(
                    "SELECT run_kind FROM agui_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            if lifecycle_row is not None and str(lifecycle_row[0]) == "text_to_sql":
                return self.finalize_run_with_event(run_id, payload)

        if event_key is not None and event_type != "WORKFLOW_RESULT":
            try:
                parse_workflow_result_event_key(event_key)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "canonical workflow result event_key is reserved for "
                    "WORKFLOW_RESULT"
                )
            if run_incarnation is None:
                raise ValueError("event_key requires run_incarnation")
            payload_run_id = payload.get("run_id")
            if payload_run_id is not None and payload_run_id != run_id:
                raise ValueError("event_key run_id does not match append run_id")
        with self._lock:
            # Атомарный INSERT с вычислением seq внутри SQL: SELECT+INSERT в
            # одной транзакции, без окна гонки между чтением max(seq) и записью.
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if workflow_identity is not None:
                    reservation = self._conn.execute(
                        """
                        SELECT run_incarnation
                        FROM workflow_run_invocations
                        WHERE run_id = ?
                        """,
                        (run_id,),
                    ).fetchone()
                    if reservation is None:
                        raise ValueError(
                            "keyed WORKFLOW_RESULT requires a reserved invocation"
                        )
                    if str(reservation[0]) != workflow_identity.run_incarnation:
                        raise ValueError(
                            "WORKFLOW_RESULT incarnation does not match reservation"
                        )
                insert_verb = "INSERT OR IGNORE" if event_key is not None else "INSERT"
                cur = self._conn.execute(
                    f"""
                    {insert_verb} INTO agui_events
                        (run_id, seq, event_type, payload, created_at_ms,
                         run_incarnation, event_key)
                    SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?
                    FROM agui_events WHERE run_id = ?
                    """,
                    (
                        run_id,
                        event_type,
                        payload_json,
                        created_at_ms,
                        run_incarnation,
                        event_key,
                        run_id,
                    ),
                )
                if event_key is not None and cur.rowcount == 0:
                    existing = self._conn.execute(
                        """
                        SELECT run_id, run_incarnation, event_type, payload, seq
                        FROM agui_events
                        WHERE event_key = ?
                        """,
                        (event_key,),
                    ).fetchone()
                    if existing is None:
                        raise ValueError(
                            "event_key insertion was ignored without an existing event"
                        )
                    try:
                        existing_payload = _canonical_payload(
                            json.loads(str(existing[3]))
                        )
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            "event_key collision has an invalid stored payload"
                        ) from exc
                    if (
                        str(existing[0]) != run_id
                        or existing[1] != run_incarnation
                        or str(existing[2]) != event_type
                        or existing_payload != payload_json
                    ):
                        raise ValueError(
                            "event_key collision does not match the original event"
                        )
                    seq_row = (existing[4],)
                else:
                    seq_row = self._conn.execute(
                        "SELECT seq FROM agui_events WHERE id = ?",
                        (cur.lastrowid,),
                    ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return int(seq_row[0])

    def list_after(
        self,
        run_id: str,
        after_seq: int = 0,
        *,
        run_incarnation: Optional[str] = None,
    ) -> Iterable[StoredEvent]:
        with self._lock:
            if run_incarnation is None:
                cur = self._conn.execute(
                    """
                    SELECT seq, run_id, event_type, payload, created_at_ms,
                           run_incarnation, event_key
                    FROM agui_events
                    WHERE run_id = ? AND seq > ?
                    ORDER BY seq ASC
                    """,
                    (run_id, after_seq),
                )
            else:
                cur = self._conn.execute(
                    """
                    SELECT seq, run_id, event_type, payload, created_at_ms,
                           run_incarnation, event_key
                    FROM agui_events
                    WHERE run_id = ? AND seq > ? AND run_incarnation = ?
                    ORDER BY seq ASC
                    """,
                    (run_id, after_seq, run_incarnation),
                )
            rows = list(cur.fetchall())
        for (
            seq,
            run_id_value,
            event_type,
            payload,
            created_at_ms,
            stored_incarnation,
            event_key,
        ) in rows:
            yield StoredEvent(
                seq=seq,
                run_id=run_id_value,
                event_type=event_type,
                payload=json.loads(payload),
                created_at_ms=created_at_ms,
                run_incarnation=stored_incarnation,
                event_key=event_key,
            )

    def get_event(self, run_id: str, seq: int) -> Optional[StoredEvent]:
        run_id = require_canonical_nonblank(run_id, field_name="run_id")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise ValueError("seq must be a positive integer")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT seq, run_id, event_type, payload, created_at_ms,
                       run_incarnation, event_key
                FROM agui_events
                WHERE run_id = ? AND seq = ?
                """,
                (run_id, seq),
            ).fetchone()
        if row is None:
            return None
        return StoredEvent(
            seq=int(row[0]),
            run_id=str(row[1]),
            event_type=str(row[2]),
            payload=json.loads(str(row[3])),
            created_at_ms=int(row[4]),
            run_incarnation=None if row[5] is None else str(row[5]),
            event_key=None if row[6] is None else str(row[6]),
        )

    def latest_seq(self, run_id: str) -> Optional[int]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT MAX(seq) FROM agui_events WHERE run_id = ?",
                (run_id,),
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._connection_closed:
                assert self._conn is not None
                self._conn.close()
                self._connection_closed = True
            if not self._lease_released:
                release_sqlite_bundle(self._bundle_lease)
                self._lease_released = True
            self._closed = True
