"""
Слой доставки результатов workflow (result-delivery)
=====================================================

Выделено из ``workflow/streamlit_api.py`` (декомпозиция god-модуля, T9):
терминализация supervised-запусков, построение/трансформация payload'ов
WORKFLOW_RESULT, PII/DSN-редакция, durable outbox (постановка в очередь,
дренаж, шедулинг ретраев) и поддерживающие DSN/redaction-хелперы.

Публичное поведение не меняется относительно исходного кода в
streamlit_api.py — модуль лишь физически перенесён. ``streamlit_api.py``
ре-экспортирует все публичные имена из этого модуля для обратной
совместимости внешних импортов и monkeypatch в тестах.

Несколько функций, патчащихся тестами через
``monkeypatch.setattr(streamlit_api, "name", ...)`` (в т.ч. в light-harness
``tests/test_text_to_sql_agui_workflow_contract.py::_load_light_workflow_streamlit_api``,
который присваивает атрибуты модуля напрямую), вызываются здесь не по
голому имени, а через ``_streamlit_api()`` — ленивый лукап модуля
``workflow.streamlit_api`` по ``sys.modules``, вычисляемый в момент вызова.
Это нужно потому, что имя функции внутри её собственного тела всегда
резолвится через ``__globals__`` модуля, где она ОПРЕДЕЛЕНА (т.е. этого
модуля), а не через модуль, из которого её импортировали/патчили —
поэтому патч атрибута на streamlit_api иначе не был бы виден вызовам
между функциями внутри этого модуля.

Особый случай: ``redact_pii_in_payload`` определена НЕ здесь и НЕ в
streamlit_api, а в ``custom_tools.text_to_sql.redaction``; здесь она тоже
вызывается только через ``_streamlit_api().redact_pii_in_payload`` —
патчить её нужно на ``workflow.streamlit_api`` (патч атрибута на этом
модуле не подействует).
"""

import os
import errno
import logging
import sqlite3
import time
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass
import threading
import json
import re
import hashlib
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._result_common import _validate_private_claim
from .models import (
    TextToSqlTerminalResult, TextToSqlTerminalStatus,
    bound_text_to_sql_error, is_text_to_sql_workflow_name,
)
from .result_repository import (
    WorkflowResultCollisionError,
    load_reconciled_workflow_result,
)
from custom_tools.text_to_sql.redaction import (
    _is_sensitive_key,
    _normalize_sensitive_key,
    _redact_payload as _agui_redact_payload,
    redact_pii_in_payload,
)


def _streamlit_api():
    """Ленивый хендл на ``workflow.streamlit_api``.

    Несколько функций этого модуля патчатся тестами на ``streamlit_api``
    напрямую (см. module docstring); внутренние вызовы между функциями
    здесь идут через эту индирекцию, чтобы такие патчи были видны
    независимо от того, какая функция какую вызывает.
    """
    from . import streamlit_api as _module

    return _module


logger = logging.getLogger(__name__)
_OUTBOX_DRAIN_BATCH_LIMIT = 100
_OUTBOX_DRAIN_TIME_BUDGET_SECONDS = 0.1
_OUTBOX_DRAIN_RETRY_DELAY_SECONDS = 0.05
_OUTBOX_DRAIN_SCHEDULE_LOCK = threading.Lock()
_OUTBOX_DRAIN_SCHEDULED: Dict[str, Tuple[int, int]] = {}
_RESULT_PERSISTENCE_DIAGNOSTIC_KEY = "result_persistence_diagnostic"


def _reset_outbox_drain_scheduler_after_fork() -> None:
    global _OUTBOX_DRAIN_SCHEDULE_LOCK, _OUTBOX_DRAIN_SCHEDULED
    _OUTBOX_DRAIN_SCHEDULE_LOCK = threading.Lock()
    _OUTBOX_DRAIN_SCHEDULED = {}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_outbox_drain_scheduler_after_fork)


def _terminalize_supervised_workflow(
    run_id: str,
    supervisor_id: Optional[str],
    attempt_generation: Optional[int],
    outcome: Dict[str, Any],
) -> bool:
    """Persist one supervisor outcome without bypassing Text-to-SQL CAS."""
    from backend.fastapi_app.agui.store import (
        EventStore,
        TerminalWorkflowResultConflictError,
        WorkflowSupervisorOwnershipConflictError,
    )

    supervisor_id, attempt_generation = _validate_private_claim(
        supervisor_id,
        attempt_generation,
    )
    worker_exited = outcome.get("reason") in {
        "WORKER_EXITED",
        "WORKER_CRASHED",
    }
    outbox_path: Optional[Path] = None
    exact_result_seen = False
    pending_exact_result = False
    if worker_exited:
        try:
            from .result_outbox import WorkflowResultOutbox

            outbox_path = _streamlit_api()._workflow_result_outbox_path()
            if outbox_path.exists():
                outbox = WorkflowResultOutbox(str(outbox_path))
                try:
                    exact_result_seen = outbox.pending_for_claim(
                        run_id,
                        supervisor_id=supervisor_id,
                        attempt_generation=attempt_generation,
                    )
                finally:
                    outbox.close()
                _streamlit_api()._drain_workflow_result_outbox_batch(path=outbox_path)
                outbox = WorkflowResultOutbox(str(outbox_path))
                try:
                    pending_exact_result = outbox.pending_for_claim(
                        run_id,
                        supervisor_id=supervisor_id,
                        attempt_generation=attempt_generation,
                    )
                finally:
                    outbox.close()
        except Exception:
            _streamlit_api()._schedule_workflow_result_outbox_drain()
            return False

    try:
        store = EventStore(str(_streamlit_api()._agui_event_store_path()))
    except Exception as exc:
        if _is_retryable_outbox_error(exc):
            return False
        raise
    try:
        stored = store.get_run(run_id)
        if stored is None:
            return False
        if stored.run_kind != "text_to_sql":
            if stored.status in {
                "succeeded",
                "abstained",
                "failed",
                "cancelled",
                "timed_out",
            }:
                if pending_exact_result and outbox_path is not None:
                    _streamlit_api()._schedule_workflow_result_outbox_drain(path=outbox_path)
                return True
            if worker_exited and (exact_result_seen or pending_exact_result):
                assert outbox_path is not None
                _streamlit_api()._schedule_workflow_result_outbox_drain(path=outbox_path)
                return False
            if supervisor_id is None or attempt_generation is None:
                return False
            return store.finish_worker(
                run_id,
                supervisor_id,
                attempt_generation,
                outcome,
            )
        if stored.status in {
            "succeeded",
            "abstained",
            "failed",
            "cancelled",
            "timed_out",
        }:
            return True
        if worker_exited and pending_exact_result:
            assert outbox_path is not None
            if stored.status == "running":
                try:
                    marked = store.mark_worker_result_pending(
                        run_id,
                        supervisor_id,
                        attempt_generation,
                    )
                except WorkflowSupervisorOwnershipConflictError:
                    return False
                except Exception as exc:
                    if _is_retryable_outbox_error(exc):
                        return False
                    raise
                if not marked:
                    current = store.get_run(run_id)
                    if current is None:
                        return False
                    if current.status in {
                        "succeeded",
                        "abstained",
                        "failed",
                        "cancelled",
                        "timed_out",
                    }:
                        return True
                    if (
                        current.status != "result_pending"
                        or current.supervisor_id != supervisor_id
                        or current.attempt_started_at_ms
                        != attempt_generation
                    ):
                        return False
            elif (
                stored.status != "result_pending"
                or stored.supervisor_id != supervisor_id
                or stored.attempt_started_at_ms != attempt_generation
            ):
                return False
            _streamlit_api()._schedule_workflow_result_outbox_drain(path=outbox_path)
            return True
        if stored.status == "result_pending":
            _streamlit_api()._schedule_workflow_result_outbox_drain()
            return True
        invocation = store.get_workflow_run_invocation(run_id)
        if invocation is None:
            raise ValueError("supervised Text-to-SQL run has no invocation")
        requested_status = str(outcome.get("status") or "failed").lower()
        if requested_status == "cancelled":
            status = "cancelled"
            reason_code = "CANCELLED"
            error = "Workflow was cancelled"
        elif requested_status == "timed_out":
            status = "timed_out"
            reason_code = "TIMED_OUT"
            error = "Workflow deadline exceeded"
        else:
            status = "failed"
            reason_code = "MANDATORY_STEP_NOT_COMPLETED"
            error = "Workflow worker did not produce a terminal result"
        terminal = _build_text_to_sql_no_runtime_terminal(
            run_id=run_id,
            status=status,
            reason_code=reason_code,
            error=error,
        )
        legacy_status, _success = _terminal_legacy_fields(terminal)
        payload = _build_workflow_result_event_payload(
            run_id,
            terminal,
            legacy_status,
            error=error,
            artifacts={
                "final_output": terminal,
                "terminal_outcome": terminal,
            },
            snapshot={
                "workflow_name": invocation.workflow_name,
                "session_id": invocation.session_id,
            },
            terminal_outcome=terminal,
            run_incarnation=invocation.run_incarnation,
        )
        try:
            store.finalize_run_with_event(
                run_id,
                payload,
                expected_supervisor_id=supervisor_id,
                expected_attempt_generation=attempt_generation,
            )
        except WorkflowSupervisorOwnershipConflictError:
            return False
        except TerminalWorkflowResultConflictError:
            current = store.get_run(run_id)
            return bool(
                current is not None
                and current.status
                in {"succeeded", "abstained", "failed", "cancelled", "timed_out"}
            )
        # Глобальные реестры активных ранов остаются жить в streamlit_api
        # (другие места патчат/читают их напрямую) — берём их лениво, чтобы
        # избежать циклического импорта и сохранить идентичность объекта.
        from .streamlit_api import (
            _GLOBAL_WORKFLOW_ACTIVE_RUNS,
            _GLOBAL_WORKFLOW_RUNS_LOCK,
        )

        with _GLOBAL_WORKFLOW_RUNS_LOCK:
            live = _GLOBAL_WORKFLOW_ACTIVE_RUNS.get(run_id)
            if isinstance(live, dict):
                _merge_workflow_result_payload(live, payload)
                live["end_time"] = datetime.now()
        return True
    finally:
        store.close()


def _is_retryable_outbox_error(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.OperationalError):
        error_code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(error_code, int):
            base_code = error_code & 0xFF
            return base_code in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
                sqlite3.SQLITE_IOERR,
                sqlite3.SQLITE_FULL,
            }
        message = str(exc).lower()
        return "locked" in message or "busy" in message
    return isinstance(exc, OSError) and exc.errno in {
        errno.EAGAIN,
        errno.EBUSY,
        errno.EIO,
        errno.ENOSPC,
        errno.ETIMEDOUT,
    }

_SENSITIVE_DSN_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "key",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
}
_SENSITIVE_PAYLOAD_KEYS = {
    "connection_string",
    "database_dsn",
    "database_url",
    "db_dsn",
    "db_url",
    "dsn",
}
_SENSITIVE_SCALAR_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "key",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
}
_URL_LIKE_PAYLOAD_KEYS = {"url"}
_DSN_TEXT_RE = re.compile(r"(?P<dsn>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+)")
_SECRET_KEY_PATTERN = r"[A-Za-z0-9_%+\-.\[\]]+"
_SENSITIVE_TEXT_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>\b(?P<key>{_SECRET_KEY_PATTERN})\s*[:=]\s*)"
    r"(?P<secret>[^\s,;&]+)",
    re.IGNORECASE,
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _agui_event_store_path() -> Path:
    from .state_files import default_state_database_path

    project_root = _streamlit_api()._project_root()
    return default_state_database_path(
        project_root,
        "agui_events.db",
        legacy_path=project_root / "data" / "agui_events.db",
    )


def _workflow_result_outbox_path() -> Path:
    from .state_files import default_state_database_path

    project_root = _streamlit_api()._project_root()
    return default_state_database_path(
        project_root,
        "workflow_result_outbox.db",
        legacy_path=project_root / "data" / "workflow_result_outbox.db",
    )


def _workflow_result_payload_from_store(
    run_id: str,
    *,
    strict: bool = False,
    run_incarnation: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    try:
        result = load_reconciled_workflow_result(
            run_id=run_id,
            primary_path=_streamlit_api()._agui_event_store_path(),
            outbox_path=_streamlit_api()._workflow_result_outbox_path(),
            strict=strict,
            run_incarnation=run_incarnation,
        )
    except (WorkflowResultCollisionError, PermissionError, RuntimeError):
        raise
    except Exception as exc:
        logger.warning(
            "Failed to load reconciled workflow result for run_id=%s: %s",
            run_id,
            exc,
        )
        return None
    return result.payload if result is not None else None


def _authoritative_workflow_result_payload(
    run_id: str,
    *,
    owner_subject: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Read the exact terminal winner selected by the run lifecycle row."""
    from backend.fastapi_app.agui.store import EventStore

    store = EventStore(str(_streamlit_api()._agui_event_store_path()))
    try:
        stored = store.get_run(run_id)
        if stored is None:
            return None
        if owner_subject is not None and (
            stored.owner_subject != owner_subject or stored.tenant_id != tenant_id
        ):
            raise ValueError("run not found")
        if stored.result_seq is None:
            return None
        event = store.get_event(run_id, stored.result_seq)
        if event is None or event.event_type != "WORKFLOW_RESULT":
            raise ValueError("terminal lifecycle result_seq is not WORKFLOW_RESULT")
        payload = event.payload
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            raise ValueError("terminal WORKFLOW_RESULT payload identity is invalid")
        return payload
    finally:
        store.close()


def _safe_serialize_result(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


def _dsn_fingerprint(dsn: str) -> str:
    return hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]


def _is_sensitive_dsn_query_key(key: Any) -> bool:
    normalized = _normalize_sensitive_key(key)
    return normalized == "odbc_connect" or normalized in _SENSITIVE_DSN_QUERY_KEYS or _is_sensitive_key(key)


def _is_sensitive_dsn_payload_key(key: Any) -> bool:
    return _normalize_sensitive_key(key) in _SENSITIVE_PAYLOAD_KEYS


def _is_url_like_payload_key(key: Any) -> bool:
    return _normalize_sensitive_key(key) in _URL_LIKE_PAYLOAD_KEYS


def _redact_sensitive_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    return f"{match.group('prefix')}***"


def _redact_dsn(dsn: Any) -> Any:
    if not isinstance(dsn, str):
        return dsn
    try:
        parts = urlsplit(dsn)
        if not parts.scheme:
            return "<redacted>"
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, hostinfo = netloc.rsplit("@", 1)
            netloc = f"***:***@{hostinfo}" if ":" in userinfo else f"***@{hostinfo}"
        query_items = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query_items.append((key, "***" if _is_sensitive_dsn_query_key(key) else value))
        query = urlencode(query_items, doseq=True).replace("%2A%2A%2A", "***")
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except Exception:
        return "<redacted>"


def _looks_like_dsn(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.scheme and (parts.netloc or parts.scheme in {"sqlite", "duckdb"}))
    except Exception:
        return False


def _redact_query_string(value: str) -> str:
    try:
        items = parse_qsl(value, keep_blank_values=True)
    except Exception:
        return value
    if not items or not any(_is_sensitive_dsn_query_key(key) for key, _ in items):
        return value
    return urlencode(
        [(key, "***" if _is_sensitive_dsn_query_key(key) else item) for key, item in items],
        doseq=True,
    )


def _redact_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        candidate = match.group("dsn")
        return _redact_dsn(candidate) if _looks_like_dsn(candidate) else candidate

    redacted = _DSN_TEXT_RE.sub(replace, value)
    return _SENSITIVE_TEXT_ASSIGNMENT_RE.sub(_redact_sensitive_assignment, redacted)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if _is_sensitive_dsn_payload_key(key) or (_is_url_like_payload_key(key) and _looks_like_dsn(item)):
                redacted[key] = _redact_dsn(item)
                if isinstance(item, str):
                    redacted.setdefault(f"{key}_fingerprint", _dsn_fingerprint(item))
            elif _is_sensitive_key(key) or _normalize_sensitive_key(key) in _SENSITIVE_SCALAR_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_error_text(error: Any) -> str:
    return str(_streamlit_api().redact_pii_in_payload(_agui_redact_payload(str(error))))


def _redact_public_payload(value: Any) -> Any:
    return _streamlit_api().redact_pii_in_payload(_agui_redact_payload(value))


def _public_workflow_parameters(parameters: Dict[str, Any]) -> Dict[str, Any]:
    public_parameters = dict(parameters)
    public_parameters.pop("dsn", None)
    public_parameters.pop("safety_policy", None)
    public_parameters.pop("schema_scope", None)
    return _redact_public_payload(public_parameters)


def _terminal_outcome_mapping(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, TextToSqlTerminalResult):
        return value.to_mapping()
    if isinstance(value, dict):
        return TextToSqlTerminalResult.from_mapping(value).to_mapping()
    raise TypeError("terminal_outcome must be a TextToSqlTerminalResult or object")


def _validated_terminal_outcome(value: Any) -> Optional[Dict[str, Any]]:
    try:
        return _terminal_outcome_mapping(value)
    except (TypeError, ValueError):
        return None


def _terminal_legacy_fields(outcome: Dict[str, Any]) -> Tuple[str, bool]:
    status = outcome.get("status")
    if status == TextToSqlTerminalStatus.SUCCEEDED.value:
        return "completed", True
    if status == TextToSqlTerminalStatus.CANCELLED.value:
        return "cancelled", False
    return "failed", False


def _apply_text_to_sql_terminal_failure(
    run_data: Dict[str, Any],
    *,
    run_id: str,
    reason_code: str,
    error: str,
    source_terminal: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Apply one coherent failed state and return its typed terminal, if applicable."""
    public_error = _redact_error_text(error)
    terminal_outcome = None
    if is_text_to_sql_workflow_name(run_data.get("workflow_name")):
        public_error = bound_text_to_sql_error(public_error)
        source = _validated_terminal_outcome(source_terminal)
        if (
            reason_code == "RESULT_PERSISTENCE_FAILED"
            and source is not None
            and source["status"] in {"failed", "abstained", "cancelled", "timed_out"}
        ):
            # A terminal result already proves the workflow outcome. A delivery
            # failure is only secondary evidence and must not rewrite that proof.
            terminal_outcome = source
            run_data[_RESULT_PERSISTENCE_DIAGNOSTIC_KEY] = {
                "reason_code": "RESULT_PERSISTENCE_FAILED",
                "error": public_error,
            }
        else:
            source = source or {}
            terminal_outcome = TextToSqlTerminalResult.from_mapping({
                "run_id": str(run_data.get("run_id") or run_id),
                "status": TextToSqlTerminalStatus.FAILED.value,
                "reason_code": reason_code,
                "sql": source.get("sql", ""),
                "generated": source.get("generated", False),
                "approved": source.get("approved", False),
                "executed": source.get("executed", False),
                "dry_run": source.get("dry_run", False),
                "audited": source.get("audited", False),
                "data": source.get("data", []),
                "columns": source.get("columns", []),
                "rows_affected": source.get("rows_affected", 0),
                "error": public_error,
                "execution": source.get("execution", {}),
                "audit": source.get("audit", {}),
                "result_review": source.get("result_review", {}),
                "persistence": (
                    {"status": "error", "error": public_error}
                    if reason_code == "RESULT_PERSISTENCE_FAILED"
                    else {"status": "not_attempted"}
                ),
                "ambiguity": None,
            }).to_mapping()
        status, _success = _terminal_legacy_fields(terminal_outcome)
        public_error = terminal_outcome.get("error")
    updates = {
        "status": status if terminal_outcome is not None else "failed",
        "end_time": datetime.now(),
        "error": public_error,
    }
    if terminal_outcome is not None:
        updates.update({
            "terminal_outcome": terminal_outcome,
            "final_output": _terminal_outcome_mapping(terminal_outcome),
            "execution": terminal_outcome.get("execution"),
        })
    run_data.update(updates)
    return terminal_outcome


def _validated_config_versions_metadata(
    value: Any,
) -> Dict[str, Dict[str, Optional[str]]]:
    if not isinstance(value, Mapping):
        raise ValueError("config_versions must be an object")
    normalized: Dict[str, Dict[str, Optional[str]]] = {}
    for registry_key, raw_version in value.items():
        if (
            not isinstance(registry_key, str)
            or not registry_key
            or registry_key != registry_key.strip()
            or not isinstance(raw_version, Mapping)
            or set(raw_version) != {"source_path", "content_sha256", "profile"}
        ):
            raise ValueError("config_versions has an invalid entry")
        source_path = raw_version.get("source_path")
        content_sha256 = raw_version.get("content_sha256")
        profile = raw_version.get("profile")
        if (
            not isinstance(source_path, str)
            or not source_path
            or source_path != source_path.strip()
            or not isinstance(content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
            or (profile is not None and not isinstance(profile, str))
        ):
            raise ValueError("config_versions has an invalid version")
        normalized[registry_key] = {
            "source_path": source_path,
            "content_sha256": content_sha256,
            "profile": profile,
        }
    return normalized


def _transform_workflow_result_event_payload(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    from .result_identity import (
        require_canonical_nonblank,
        workflow_result_identity_from_payload,
    )

    run_id = require_canonical_nonblank(payload.get("run_id"), field_name="run_id")
    thread_id = require_canonical_nonblank(
        payload.get("thread_id"),
        field_name="thread_id",
    )
    has_incarnation = payload.get("run_incarnation") is not None
    has_event_key = payload.get("event_key") is not None
    if has_incarnation != has_event_key:
        raise ValueError("run_incarnation and event_key must be provided together")
    identity = (
        workflow_result_identity_from_payload(payload, expected_run_id=run_id)
        if has_incarnation
        else None
    )

    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("workflow result snapshot must be an object")
    snapshot_incarnation = snapshot.get("run_incarnation")
    if identity is None:
        if snapshot_incarnation is not None:
            raise ValueError("snapshot run_incarnation requires canonical identity")
    elif snapshot_incarnation != identity.run_incarnation:
        raise ValueError("snapshot run_incarnation does not match event identity")

    terminal_paths: list[tuple[str, ...]] = []
    authoritative_terminal = _validated_terminal_outcome(
        payload.get("terminal_outcome")
    )
    validate_terminal_copies = (
        payload.get("terminal_outcome") is not None
        or is_text_to_sql_workflow_name(snapshot.get("workflow_name"))
    )
    if payload.get("terminal_outcome") is not None:
        if authoritative_terminal is None:
            raise ValueError("terminal_outcome must be a valid terminal outcome")
        if authoritative_terminal["run_id"] != run_id:
            raise ValueError("terminal_outcome run_id does not match outer run_id")
        terminal_paths.append(("terminal_outcome",))

    def validate_terminal_copy(
        value: Any,
        *,
        field_name: str,
        reject_invalid_if_present: bool,
        path: tuple[str, ...],
    ) -> None:
        if value is None:
            return
        if not validate_terminal_copies:
            return
        terminal = _validated_terminal_outcome(value)
        if terminal is None:
            if reject_invalid_if_present:
                raise ValueError(f"{field_name} must be a valid terminal outcome")
            return
        if terminal["run_id"] != run_id:
            raise ValueError(f"{field_name} run_id does not match outer run_id")
        if authoritative_terminal is None:
            raise ValueError(
                f"{field_name} terminal copy requires terminal_outcome"
            )
        if terminal != authoritative_terminal:
            raise ValueError(f"{field_name} does not match terminal_outcome")
        terminal_paths.append(path)

    validate_terminal_copy(
        payload.get("result"),
        field_name="result",
        reject_invalid_if_present=False,
        path=("result",),
    )
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("workflow result artifacts must be an object")
    artifact_metadata = artifacts.get("metadata")
    has_config_versions = (
        isinstance(artifact_metadata, dict)
        and "config_versions" in artifact_metadata
    )
    config_versions = (
        _validated_config_versions_metadata(
            artifact_metadata.get("config_versions")
        )
        if has_config_versions
        else None
    )
    validate_terminal_copy(
        artifacts.get("terminal_outcome"),
        field_name="artifacts.terminal_outcome",
        reject_invalid_if_present=True,
        path=("artifacts", "terminal_outcome"),
    )
    validate_terminal_copy(
        artifacts.get("final_output"),
        field_name="artifacts.final_output",
        reject_invalid_if_present=False,
        path=("artifacts", "final_output"),
    )

    public_payload = dict(payload)
    for field_name in ("result", "error", "artifacts", "snapshot"):
        public_payload[field_name] = _redact_payload(public_payload.get(field_name))
    transformed = _streamlit_api().redact_pii_in_payload(_agui_redact_payload(public_payload))
    if not isinstance(transformed, dict):
        raise RuntimeError("workflow result transform returned a non-object")

    transformed["run_id"] = run_id
    transformed["thread_id"] = thread_id
    if identity is not None:
        transformed["run_incarnation"] = identity.run_incarnation
        transformed["event_key"] = identity.event_key
        transformed_snapshot = transformed.get("snapshot")
        if not isinstance(transformed_snapshot, dict):
            raise RuntimeError("workflow result transform changed snapshot shape")
        transformed_snapshot["run_incarnation"] = identity.run_incarnation

    for path in terminal_paths:
        container = transformed
        for component in path[:-1]:
            nested = container.get(component)
            if not isinstance(nested, dict):
                raise RuntimeError(
                    "workflow result transform changed terminal copy shape"
                )
            container = nested
        terminal_copy = container.get(path[-1])
        if not isinstance(terminal_copy, dict):
            raise RuntimeError("workflow result transform changed terminal copy shape")
        terminal_copy["run_id"] = run_id
    if has_config_versions:
        transformed_artifacts = transformed.get("artifacts")
        if not isinstance(transformed_artifacts, dict):
            raise RuntimeError("workflow result transform changed artifacts shape")
        transformed_metadata = transformed_artifacts.get("metadata")
        if not isinstance(transformed_metadata, dict):
            raise RuntimeError("workflow result transform changed metadata shape")
        transformed_metadata["config_versions"] = config_versions
    return transformed


def _build_workflow_result_event_payload(
    run_id: str,
    result: Any,
    status: str,
    error: Optional[str] = None,
    artifacts: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    terminal_outcome: Optional[Dict[str, Any]] = None,
    run_incarnation: Optional[str] = None,
) -> Dict[str, Any]:
    if terminal_outcome is not None:
        terminal_outcome = _terminal_outcome_mapping(terminal_outcome)
        status, success = _terminal_legacy_fields(terminal_outcome)
    else:
        success = status == "completed"
    snapshot_payload = dict(snapshot or {})
    if run_incarnation is not None:
        snapshot_payload["run_incarnation"] = run_incarnation
    payload = {
        "run_id": run_id,
        "thread_id": run_id,
        "status": status,
        "success": success,
        "terminal_outcome": terminal_outcome,
        "result": _safe_serialize_result(result),
        "error": error,
        "artifacts": _safe_serialize_result(artifacts or {}),
        "snapshot": _safe_serialize_result(snapshot_payload),
    }
    if run_incarnation is not None:
        from .result_identity import workflow_result_event_key

        payload.update({
            "run_incarnation": run_incarnation,
            "event_key": workflow_result_event_key(run_id, run_incarnation),
        })
    return _transform_workflow_result_event_payload(payload)


def _build_text_to_sql_no_runtime_terminal(
    *,
    run_id: str,
    status: str,
    reason_code: str,
    error: str,
) -> Dict[str, Any]:
    return TextToSqlTerminalResult.from_mapping(
        {
            "run_id": run_id,
            "status": status,
            "reason_code": reason_code,
            "sql": "",
            "generated": False,
            "approved": False,
            "executed": False,
            "dry_run": False,
            "audited": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "error": _redact_error_text(error),
            "execution": {},
            "audit": {},
            "persistence": {"status": "not_attempted"},
            "result_review": {},
            "ambiguity": None,
        }
    ).to_mapping()


def _append_workflow_result_payload_to_primary(
    payload: Dict[str, Any],
    *,
    supervisor_id: Optional[str] = None,
    attempt_generation: Optional[int] = None,
) -> None:
    from backend.fastapi_app.agui.store import EventStore

    supervisor_id, attempt_generation = _validate_private_claim(
        supervisor_id,
        attempt_generation,
    )
    db_path = _streamlit_api()._agui_event_store_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(str(db_path))
    try:
        run_id = str(payload["run_id"])
        get_run = getattr(store, "get_run", None)
        stored = get_run(run_id) if callable(get_run) else None
        if stored is not None and stored.run_kind == "text_to_sql":
            store.finalize_run_with_event(
                run_id,
                payload,
                expected_supervisor_id=supervisor_id,
                expected_attempt_generation=attempt_generation,
            )
        elif supervisor_id is not None:
            store.finalize_worker_with_event(
                run_id,
                supervisor_id,
                attempt_generation,
                payload,
            )
        else:
            store.append(run_id, "WORKFLOW_RESULT", payload)
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def _enqueue_workflow_result_payload(
    payload: Dict[str, Any],
    *,
    supervisor_id: Optional[str] = None,
    attempt_generation: Optional[int] = None,
) -> None:
    from backend.fastapi_app.agui.store import (
        EventStore,
        WorkflowSupervisorOwnershipConflictError,
    )
    from .result_outbox import WorkflowResultOutbox

    supervisor_id, attempt_generation = _validate_private_claim(
        supervisor_id,
        attempt_generation,
    )
    path = _streamlit_api()._workflow_result_outbox_path()
    outbox = WorkflowResultOutbox(str(path))
    try:
        if supervisor_id is None:
            outbox.enqueue(payload)
        else:
            outbox.enqueue(
                payload,
                supervisor_id=supervisor_id,
                attempt_generation=attempt_generation,
            )
    finally:
        outbox.close()
    if supervisor_id is not None and attempt_generation is not None:
        store = EventStore(str(_streamlit_api()._agui_event_store_path()))
        try:
            stored = store.get_run(str(payload["run_id"]))
            if stored is not None and stored.run_kind == "text_to_sql":
                marked = store.mark_worker_result_pending(
                    stored.run_id,
                    supervisor_id,
                    attempt_generation,
                )
                if not marked:
                    current = store.get_run(stored.run_id)
                    exact_terminal = False
                    if (
                        current is not None
                        and current.result_seq is not None
                        and current.status
                        in {
                            "succeeded",
                            "abstained",
                            "failed",
                            "cancelled",
                            "timed_out",
                        }
                    ):
                        event = store.get_event(
                            current.run_id,
                            current.result_seq,
                        )
                        exact_terminal = bool(
                            event is not None
                            and event.event_type == "WORKFLOW_RESULT"
                            and event.payload == payload
                        )
                    if not exact_terminal and not (
                        current is not None
                        and current.status == "result_pending"
                        and current.supervisor_id == supervisor_id
                        and current.attempt_started_at_ms
                        == attempt_generation
                    ):
                        raise WorkflowSupervisorOwnershipConflictError(
                            "workflow result outbox claim ownership changed"
                        )
        finally:
            store.close()
    _streamlit_api()._schedule_workflow_result_outbox_drain(path=path)


@dataclass(frozen=True)
class _WorkflowResultResolution:
    persistence_succeeded: bool
    candidate_won: bool
    resolved_payload: Optional[Dict[str, Any]]


def _coerce_workflow_result_resolution(
    value: Any,
    candidate_payload: Dict[str, Any],
) -> _WorkflowResultResolution:
    if isinstance(value, _WorkflowResultResolution):
        return value
    succeeded = value is True
    return _WorkflowResultResolution(
        persistence_succeeded=succeeded,
        candidate_won=succeeded,
        resolved_payload=candidate_payload if succeeded else None,
    )


def _append_workflow_result_event(
    run_id: str,
    result: Any,
    status: str,
    error: Optional[str] = None,
    artifacts: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    terminal_outcome: Optional[Dict[str, Any]] = None,
    run_incarnation: Optional[str] = None,
    supervisor_id: Optional[str] = None,
    attempt_generation: Optional[int] = None,
) -> _WorkflowResultResolution:
    from backend.fastapi_app.agui.store import (
        TerminalWorkflowResultConflictError,
        WorkflowSupervisorOwnershipConflictError,
    )

    payload = _build_workflow_result_event_payload(
        run_id,
        result,
        status,
        error,
        artifacts,
        snapshot,
        terminal_outcome,
        run_incarnation,
    )
    supervisor_id, attempt_generation = _validate_private_claim(
        supervisor_id,
        attempt_generation,
    )
    last_error: Optional[Exception] = None
    for _attempt in range(2):
        try:
            if supervisor_id is None:
                _streamlit_api()._append_workflow_result_payload_to_primary(payload)
            else:
                _streamlit_api()._append_workflow_result_payload_to_primary(
                    payload,
                    supervisor_id=supervisor_id,
                    attempt_generation=attempt_generation,
                )
            return _WorkflowResultResolution(True, True, payload)
        except TerminalWorkflowResultConflictError:
            logger.info(
                "Existing terminal WORKFLOW_RESULT won for workflow %s",
                run_id,
            )
            try:
                winner = _streamlit_api()._authoritative_workflow_result_payload(run_id)
            except Exception as exc:
                logger.warning(
                    "Unable to resolve terminal WORKFLOW_RESULT winner for %s: %s",
                    run_id,
                    _redact_error_text(exc),
                )
                winner = None
            return _WorkflowResultResolution(True, False, winner)
        except WorkflowSupervisorOwnershipConflictError as exc:
            try:
                winner = _streamlit_api()._authoritative_workflow_result_payload(run_id)
            except Exception:
                winner = None
            if winner is not None:
                return _WorkflowResultResolution(
                    True,
                    winner == payload,
                    winner,
                )
            last_error = exc
            break
        except Exception as exc:
            last_error = exc
            if not _is_retryable_outbox_error(exc):
                break
    if run_incarnation is not None and last_error is not None and (
        _is_retryable_outbox_error(last_error)
    ):
        try:
            if supervisor_id is None:
                _streamlit_api()._enqueue_workflow_result_payload(payload)
            else:
                _streamlit_api()._enqueue_workflow_result_payload(
                    payload,
                    supervisor_id=supervisor_id,
                    attempt_generation=attempt_generation,
                )
            logger.warning(
                "Primary WORKFLOW_RESULT store unavailable for %s; queued durable outbox entry",
                run_id,
            )
            return _WorkflowResultResolution(True, True, payload)
        except Exception as outbox_error:
            last_error = outbox_error
    logger.warning(
        "⚠️ Не удалось записать WORKFLOW_RESULT для workflow %s: %s",
        run_id,
        _redact_error_text(last_error),
    )
    return _WorkflowResultResolution(False, False, None)


def _persist_workflow_result(
    run_id: str,
    result: Any,
    status: str,
    error: Optional[str] = None,
    *,
    artifacts: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    terminal_outcome: Optional[Dict[str, Any]] = None,
    run_incarnation: Optional[str] = None,
    supervisor_id: Optional[str] = None,
    attempt_generation: Optional[int] = None,
) -> _WorkflowResultResolution:
    supervisor_id, attempt_generation = _validate_private_claim(
        supervisor_id,
        attempt_generation,
    )
    candidate = _build_workflow_result_event_payload(
        run_id,
        result,
        status,
        error,
        artifacts,
        snapshot,
        terminal_outcome,
        run_incarnation,
    )
    append_kwargs = {
        "artifacts": artifacts,
        "snapshot": snapshot,
        "terminal_outcome": terminal_outcome,
        "run_incarnation": run_incarnation,
    }
    if supervisor_id is not None:
        append_kwargs.update(
            {
                "supervisor_id": supervisor_id,
                "attempt_generation": attempt_generation,
            }
        )
    return _coerce_workflow_result_resolution(
        _streamlit_api()._append_workflow_result_event(
            run_id,
            result,
            status,
            error,
            **append_kwargs,
        ),
        candidate,
    )


def _drain_workflow_result_outbox_batch(
    *,
    limit: int = _OUTBOX_DRAIN_BATCH_LIMIT,
    time_budget_seconds: float = _OUTBOX_DRAIN_TIME_BUDGET_SECONDS,
    path: Optional[Path] = None,
) -> Tuple[int, bool]:
    from backend.fastapi_app.agui.store import (
        TerminalWorkflowResultConflictError,
        WorkflowSupervisorOwnershipConflictError,
    )
    from .result_identity import (
        upgrade_legacy_workflow_result_payload,
        workflow_result_identity_from_payload,
    )

    if limit < 1:
        raise ValueError("outbox drain limit must be positive")
    if time_budget_seconds <= 0:
        raise ValueError("outbox drain time budget must be positive")
    try:
        from .result_outbox import WorkflowResultOutbox

        path = Path(path) if path is not None else _streamlit_api()._workflow_result_outbox_path()
        if not path.exists():
            return 0, True
        outbox = WorkflowResultOutbox(str(path))
    except Exception as exc:
        logger.warning(
            "Unable to open WORKFLOW_RESULT outbox: %s",
            _redact_error_text(exc),
        )
        return 0, _is_retryable_outbox_error(exc)
    delivered = 0
    retryable = True
    deadline = time.monotonic() + time_budget_seconds
    try:
        entries = outbox.list_pending(limit=limit)
        for entry in entries:
            if time.monotonic() >= deadline:
                break
            delivery_payload = entry.payload
            try:
                workflow_result_identity_from_payload(delivery_payload)
            except ValueError:
                delivery_payload = upgrade_legacy_workflow_result_payload(
                    delivery_payload
                )
            appended = False
            for _attempt in range(2):
                try:
                    if entry.supervisor_id is None:
                        _streamlit_api()._append_workflow_result_payload_to_primary(
                            delivery_payload
                        )
                    else:
                        _streamlit_api()._append_workflow_result_payload_to_primary(
                            delivery_payload,
                            supervisor_id=entry.supervisor_id,
                            attempt_generation=entry.attempt_generation,
                        )
                    appended = True
                    break
                except TerminalWorkflowResultConflictError:
                    appended = True
                    break
                except WorkflowSupervisorOwnershipConflictError:
                    logger.info(
                        "Discarding fenced WORKFLOW_RESULT outbox entry for %s",
                        entry.run_id,
                    )
                    appended = True
                    break
                except Exception as exc:
                    retryable = _is_retryable_outbox_error(exc)
                    if not retryable:
                        break
            if not appended:
                break
            try:
                acknowledged = outbox.delete(
                    entry.event_key,
                    enqueue_seq=entry.enqueue_seq,
                    payload=entry.payload,
                    supervisor_id=entry.supervisor_id,
                    attempt_generation=entry.attempt_generation,
                )
            except Exception as exc:
                retryable = _is_retryable_outbox_error(exc)
                logger.warning(
                    "WORKFLOW_RESULT outbox acknowledgement failed for %s: %s",
                    entry.run_id,
                    _redact_error_text(exc),
                )
                break
            if not acknowledged:
                logger.warning(
                    "WORKFLOW_RESULT outbox acknowledgement became stale for %s",
                    entry.run_id,
                )
                break
            delivered += 1
    except Exception as exc:
        retryable = _is_retryable_outbox_error(exc)
        logger.warning(
            "Unable to read WORKFLOW_RESULT outbox: %s",
            _redact_error_text(exc),
        )
    finally:
        try:
            outbox.close()
        except Exception as exc:
            retryable = retryable and _is_retryable_outbox_error(exc)
            logger.warning(
                "Unable to close WORKFLOW_RESULT outbox: %s",
                _redact_error_text(exc),
            )
    return delivered, retryable


def _drain_workflow_result_outbox(
    *,
    limit: int = _OUTBOX_DRAIN_BATCH_LIMIT,
    time_budget_seconds: float = _OUTBOX_DRAIN_TIME_BUDGET_SECONDS,
    path: Optional[Path] = None,
) -> int:
    delivered, _retryable = _streamlit_api()._drain_workflow_result_outbox_batch(
        limit=limit,
        time_budget_seconds=time_budget_seconds,
        path=path,
    )
    return delivered


def _workflow_result_outbox_pending_state(path: Path) -> Tuple[bool, bool]:
    try:
        from .result_outbox import WorkflowResultOutbox

        if not path.exists():
            return False, False
        outbox = WorkflowResultOutbox(str(path))
        try:
            return outbox.count() > 0, False
        finally:
            outbox.close()
    except Exception as exc:
        logger.warning(
            "Unable to inspect WORKFLOW_RESULT outbox: %s",
            _redact_error_text(exc),
        )
        return False, _is_retryable_outbox_error(exc)


def _start_scheduled_workflow_result_outbox_drain(
    *,
    key: str,
    path: Optional[Path],
    pid: int,
    generation: int,
    delay_seconds: float,
) -> None:
    worker = threading.Thread(
        target=_scheduled_workflow_result_outbox_drain,
        kwargs={
            "key": key,
            "path": path,
            "pid": pid,
            "generation": generation,
            "delay_seconds": delay_seconds,
        },
        name=f"workflow-outbox-drain-{abs(hash(key)) & 0xFFFF:x}",
        daemon=True,
    )
    worker.start()


def _scheduled_workflow_result_outbox_drain(
    *,
    key: str,
    path: Optional[Path],
    pid: int,
    generation: int,
    delay_seconds: float,
) -> None:
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    delivered = 0
    retryable = False
    pending = False
    inspection_retryable = False
    resolved_path: Optional[Path] = None
    try:
        resolved_path = (
            Path(path) if path is not None else _streamlit_api()._workflow_result_outbox_path()
        )
        delivered, retryable = _streamlit_api()._drain_workflow_result_outbox_batch(
            path=resolved_path
        )
        pending, inspection_retryable = (
            _workflow_result_outbox_pending_state(resolved_path)
        )
    except Exception as exc:
        retryable = _is_retryable_outbox_error(exc)
        inspection_retryable = retryable
        logger.warning(
            "Scheduled WORKFLOW_RESULT outbox drain failed: %s",
            _redact_error_text(exc),
        )
    next_generation = generation
    continue_drain = (pending and retryable) or inspection_retryable
    with _OUTBOX_DRAIN_SCHEDULE_LOCK:
        state = _OUTBOX_DRAIN_SCHEDULED.get(key)
        if state is None or state[0] != pid or pid != os.getpid():
            return
        next_generation = state[1]
        continue_drain = continue_drain or next_generation != generation
        if not continue_drain:
            _OUTBOX_DRAIN_SCHEDULED.pop(key, None)
    if continue_drain:
        _streamlit_api()._start_scheduled_workflow_result_outbox_drain(
            key=key,
            path=resolved_path if resolved_path is not None else path,
            pid=pid,
            generation=next_generation,
            delay_seconds=(
                0.0 if delivered else _OUTBOX_DRAIN_RETRY_DELAY_SECONDS
            ),
        )


def _schedule_workflow_result_outbox_drain(
    *,
    path: Optional[Path] = None,
) -> bool:
    outbox_path = Path(path) if path is not None else None
    key = (
        str(outbox_path.absolute())
        if outbox_path is not None
        else "<default-workflow-result-outbox>"
    )
    pid = os.getpid()
    generation = 1
    with _OUTBOX_DRAIN_SCHEDULE_LOCK:
        state = _OUTBOX_DRAIN_SCHEDULED.get(key)
        if state is not None and state[0] == pid:
            _OUTBOX_DRAIN_SCHEDULED[key] = (pid, state[1] + 1)
            return False
        _OUTBOX_DRAIN_SCHEDULED[key] = (pid, generation)
    try:
        _streamlit_api()._start_scheduled_workflow_result_outbox_drain(
            key=key,
            path=outbox_path,
            pid=pid,
            generation=generation,
            delay_seconds=0.0,
        )
    except Exception:
        with _OUTBOX_DRAIN_SCHEDULE_LOCK:
            if _OUTBOX_DRAIN_SCHEDULED.get(key) == (pid, generation):
                _OUTBOX_DRAIN_SCHEDULED.pop(key, None)
        raise
    return True


def _reserve_workflow_run_invocation(
    run_id: str,
    run_incarnation: str,
    session_id: str,
    workflow_name: str,
) -> bool:
    from backend.fastapi_app.agui.store import EventStore

    db_path = _streamlit_api()._agui_event_store_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(str(db_path))
    try:
        return store.reserve_workflow_run(
            run_id,
            run_incarnation,
            session_id,
            workflow_name,
        )
    finally:
        store.close()


def _workflow_artifacts_from_run_data(run_data: Dict[str, Any]) -> Dict[str, Any]:
    metadata = {
        "workflow_id": run_data.get("workflow_id"),
        "workflow_name": run_data.get("workflow_name"),
        "run_incarnation": run_data.get("run_incarnation"),
    }
    if run_data.get("execution") is not None:
        metadata["execution"] = run_data.get("execution")
    if run_data.get("config_versions") is not None:
        metadata["config_versions"] = run_data.get("config_versions")
    diagnostic = run_data.get(_RESULT_PERSISTENCE_DIAGNOSTIC_KEY)
    if isinstance(diagnostic, dict) and set(diagnostic) == {"reason_code", "error"}:
        reason_code = diagnostic.get("reason_code")
        error = diagnostic.get("error")
        if reason_code == "RESULT_PERSISTENCE_FAILED" and isinstance(error, str):
            metadata[_RESULT_PERSISTENCE_DIAGNOSTIC_KEY] = {
                "reason_code": reason_code,
                "error": bound_text_to_sql_error(_redact_error_text(error)),
            }
    return {
        "run_id": run_data.get("run_id"),
        "final_output": run_data.get("final_output"),
        "step_outputs": run_data.get("step_outputs") or {},
        "step_results": run_data.get("step_results") or {},
        "terminal_outcome": run_data.get("terminal_outcome"),
        "metadata": metadata,
    }


def _merge_workflow_result_payload(run_data: Dict[str, Any], payload: Dict[str, Any]) -> None:
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    if "status" in payload:
        run_data["status"] = payload.get("status")
    if "error" in payload:
        run_data["error"] = payload.get("error")
    if "final_output" in artifacts:
        run_data["final_output"] = artifacts.get("final_output")
    elif "result" in payload:
        run_data["final_output"] = payload.get("result")
    if "step_outputs" in artifacts:
        run_data["step_outputs"] = artifacts.get("step_outputs")
    if "step_results" in artifacts:
        run_data["step_results"] = artifacts.get("step_results")
    terminal_candidate = payload.get("terminal_outcome")
    if terminal_candidate is None:
        terminal_candidate = artifacts.get("terminal_outcome")
    terminal_outcome = _validated_terminal_outcome(terminal_candidate)
    if terminal_outcome is not None:
        run_data["terminal_outcome"] = terminal_outcome
    else:
        run_data.pop("terminal_outcome", None)
    metadata = artifacts.get("metadata") if isinstance(artifacts.get("metadata"), dict) else {}
    if terminal_outcome is not None:
        run_data["status"], _ = _terminal_legacy_fields(terminal_outcome)
        run_data["execution"] = terminal_outcome.get("execution")
    elif "execution" in metadata:
        run_data["execution"] = metadata.get("execution")
    if "config_versions" in metadata:
        run_data["config_versions"] = metadata.get("config_versions")
    diagnostic = metadata.get(_RESULT_PERSISTENCE_DIAGNOSTIC_KEY)
    if isinstance(diagnostic, dict) and set(diagnostic) == {"reason_code", "error"}:
        if (
            diagnostic.get("reason_code") == "RESULT_PERSISTENCE_FAILED"
            and isinstance(diagnostic.get("error"), str)
        ):
            run_data[_RESULT_PERSISTENCE_DIAGNOSTIC_KEY] = dict(diagnostic)
    if "workflow_name" in snapshot and snapshot.get("workflow_name"):
        run_data["workflow_name"] = snapshot.get("workflow_name")
    if "parameters" in snapshot:
        snapshot_parameters = snapshot.get("parameters")
        run_data["parameters"] = (
            _public_workflow_parameters(snapshot_parameters)
            if isinstance(snapshot_parameters, dict)
            else {}
        )
    resolved_status = str(run_data.get("status") or "").lower()
    if resolved_status in {"completed", "failed", "cancelled"}:
        run_data["progress_percentage"] = 100.0 if resolved_status == "completed" else 0.0
        run_data["current_step"] = None
        run_data["end_time"] = datetime.now()
        run_data.pop("cancel_requested", None)
        for event_type in ("completed", "failed", "cancelled"):
            run_data.pop(f"last_{event_type}", None)


def _workflow_run_store_merge_version(run_data: Dict[str, Any]) -> Any:
    """Return a detached version of fields a store merge is allowed to replace."""
    return _safe_serialize_result({
        "run_incarnation": run_data.get("run_incarnation"),
        "status": run_data.get("status"),
        "end_time": run_data.get("end_time"),
        "cancel_requested": bool(run_data.get("cancel_requested")),
        "error": run_data.get("error"),
        "final_output": run_data.get("final_output"),
        "terminal_outcome": run_data.get("terminal_outcome"),
    })
