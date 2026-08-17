"""Typed HTTP client used by the Streamlit Text-to-SQL page."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any, Callable, Mapping, Optional
from urllib import error, request
import uuid

from backend.fastapi_app.agui.connection_registry import ConnectionRef
from workflow.text_to_sql_contract import (
    TEXT_TO_SQL_MAX_ERROR_LENGTH,
    TEXT_TO_SQL_TERMINAL_REQUIRED_FIELDS,
    TextToSqlTerminalReasonCode,
    TextToSqlTerminalStatus,
)


JsonMapping = Mapping[str, Any]
Transport = Callable[..., JsonMapping]


class TextToSqlApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class TextToSqlRunRequest:
    query: str
    connection_ref: str
    context_documents: tuple[str, ...] = ()
    idempotency_key: Optional[str] = None
    max_rows: int = 100
    safety_level: str = "strict"
    include_explanation: bool = True
    validate_schema: bool = True
    dry_run_only: bool = False
    enable_telemetry: bool = False

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query is required")
        if not self.connection_ref.strip():
            raise ValueError("connection_ref is required")
        if any(not isinstance(item, str) or not item.strip() for item in self.context_documents):
            raise ValueError("context_documents entries must be non-empty text strings")


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    thread_id: str
    status_url: str
    result_url: str
    cancel_url: str


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    status: str
    thread_id: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class TextToSqlResult:
    run_id: str
    status: str
    reason_code: str
    sql: str
    generated: bool
    approved: bool
    executed: bool
    dry_run: bool
    audited: bool
    rows: list[Any]
    columns: list[str]
    rows_affected: int
    error: Optional[str]
    execution: Mapping[str, Any]
    audit: Mapping[str, Any]
    persistence: Mapping[str, Any]
    raw: Mapping[str, Any] = field(default_factory=dict)
    final_output: Any = None
    reason_code_recognized: bool = True


# Boundary: only connection references returned by trusted connection endpoints
# may feed future requests; public event and snapshot values are display-only.
@dataclass(frozen=True)
class ConnectionSummary:
    connection_ref: str
    display_name: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


def _connection_summary(value: object) -> ConnectionSummary:
    if not isinstance(value, Mapping):
        raise TextToSqlApiError("connection metadata must be an object")
    connection_ref = value.get("connection_ref")
    if not isinstance(connection_ref, str) or not connection_ref:
        raise TextToSqlApiError("connection metadata has an invalid connection_ref")
    try:
        ConnectionRef(connection_ref)
    except ValueError as exc:
        raise TextToSqlApiError(
            "connection metadata has an invalid connection_ref"
        ) from exc
    return ConnectionSummary(
        connection_ref=connection_ref,
        display_name=str(value.get("display_name") or value.get("name") or ""),
        raw=dict(value),
    )


@dataclass(frozen=True)
class SchemaResult:
    tables: list[dict[str, Any]]
    raw: Mapping[str, Any] = field(default_factory=dict)


def _contract_error(message: str) -> TextToSqlApiError:
    return TextToSqlApiError(f"invalid Text-to-SQL terminal outcome: {message}")


def _validate_terminal_outcome(
    value: Mapping[str, Any],
    *,
    expected_run_id: str,
    final_output: Any = None,
) -> TextToSqlResult:
    """Shape-check a terminal outcome against the shared Text-to-SQL contract.

    Unknown top-level fields are forward-compatible and therefore allowed.
    Deep business-rule evidence (execution/audit/persistence invariants) is
    the responsibility of ``workflow.text_to_sql_contract`` on the producer
    side; this client only verifies required fields and basic wire types.
    """
    if not isinstance(value, Mapping):
        raise _contract_error("terminal outcome must be an object")
    missing = TEXT_TO_SQL_TERMINAL_REQUIRED_FIELDS - set(value)
    if missing:
        raise _contract_error(
            "missing required fields: " + ", ".join(sorted(missing))
        )
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _contract_error("evidence must be finite JSON") from exc

    run_id = value["run_id"]
    if not isinstance(run_id, str) or not run_id.strip() or run_id != expected_run_id:
        raise _contract_error("run_id must match the requested run")

    raw_status = value["status"]
    if not isinstance(raw_status, str):
        raise _contract_error("status must be a string")
    try:
        TextToSqlTerminalStatus(raw_status)
    except ValueError as exc:
        raise _contract_error(f"status is unsupported: {raw_status!r}") from exc

    reason_code = value["reason_code"]
    sql = value["sql"]
    if not isinstance(reason_code, str) or not isinstance(sql, str):
        raise _contract_error("reason_code and sql must be strings")
    reason_code_recognized = reason_code == "" or reason_code in (
        item.value for item in TextToSqlTerminalReasonCode
    )
    for name in ("generated", "approved", "executed", "dry_run", "audited"):
        if type(value[name]) is not bool:
            raise _contract_error(f"{name} must be a boolean")

    data = value["data"]
    columns = value["columns"]
    rows_affected = value["rows_affected"]
    error_message = value["error"]
    execution = value["execution"]
    audit = value["audit"]
    persistence = value["persistence"]
    if not isinstance(data, list):
        raise _contract_error("data must be a list")
    if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
        raise _contract_error("columns must be a list of strings")
    if type(rows_affected) is not int or rows_affected < 0:
        raise _contract_error("rows_affected must be a non-negative integer")
    if error_message is not None and (
        not isinstance(error_message, str)
        or len(error_message) > TEXT_TO_SQL_MAX_ERROR_LENGTH
    ):
        raise _contract_error("error must be a bounded string or null")
    if not all(isinstance(item, Mapping) for item in (execution, audit, persistence)):
        raise _contract_error("execution, audit and persistence must be objects")

    return TextToSqlResult(
        run_id=run_id,
        status=raw_status,
        reason_code=reason_code,
        sql=sql,
        generated=value["generated"],
        approved=value["approved"],
        executed=value["executed"],
        dry_run=value["dry_run"],
        audited=value["audited"],
        rows=list(data),
        columns=list(columns),
        rows_affected=rows_affected,
        error=error_message,
        execution=dict(execution),
        audit=dict(audit),
        persistence=dict(persistence),
        raw=dict(value),
        final_output=final_output,
        reason_code_recognized=reason_code_recognized,
    )


TERMINAL_RUN_STATUSES = frozenset({"finished", "errored", "cancelled"})


class TextToSqlApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        auth_headers: Callable[[], Mapping[str, str]],
        transport: Optional[Transport] = None,
        poll_interval_seconds: float = 0.25,
        max_poll_attempts: int = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth_headers = auth_headers
        self._transport = transport or self._urllib_transport
        self.poll_interval_seconds = poll_interval_seconds
        self.max_poll_attempts = max_poll_attempts

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        headers = dict(self._auth_headers())
        if not headers:
            raise TextToSqlApiError("authenticated API session is required")
        response = self._transport(
            method,
            path,
            json_body=json_body,
            headers=headers,
        )
        if not isinstance(response, Mapping):
            raise TextToSqlApiError("API response must be an object")
        return response

    def _urllib_transport(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]],
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        payload = None
        request_headers = {"Accept": "application/json", **headers}
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_url = (
            path
            if path.startswith(("http://", "https://"))
            else f"{self.base_url}{path}"
        )
        api_request = request.Request(
            request_url,
            data=payload,
            headers=request_headers,
            method=method,
        )
        try:
            with request.urlopen(api_request, timeout=30) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TextToSqlApiError(f"API request failed: {exc}") from exc
        if not isinstance(decoded, Mapping):
            raise TextToSqlApiError("API response must be an object")
        return decoded

    @staticmethod
    def _run_body(
        *,
        action: str,
        payload: Mapping[str, Any],
        query: str,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        resolved_run_id = run_id or str(uuid.uuid4())
        return {
            "threadId": f"streamlit-{resolved_run_id}",
            "runId": resolved_run_id,
            "state": {},
            "messages": [
                {
                    "id": str(uuid.uuid4()),
                    "role": "user",
                    "content": query,
                }
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {
                "service_action": action,
                "service_payload": dict(payload),
            },
        }

    def start(self, run_request: TextToSqlRunRequest) -> RunHandle:
        payload = {
            "query": run_request.query,
            "connection_ref": run_request.connection_ref,
            "max_rows": run_request.max_rows,
            "safety_level": run_request.safety_level,
            "include_explanation": run_request.include_explanation,
            "validate_schema": run_request.validate_schema,
            "dry_run_only": run_request.dry_run_only,
            "enable_telemetry": run_request.enable_telemetry,
        }
        if run_request.context_documents:
            payload["context_documents"] = list(run_request.context_documents)
        if run_request.idempotency_key:
            payload["idempotency_key"] = run_request.idempotency_key
        response = self._request(
            "POST",
            "/v1/runs",
            self._run_body(
                action="presets.text_to_sql.generate",
                payload=payload,
                query=run_request.query,
            ),
        )
        return self._parse_handle(response)

    def get_me(self) -> Mapping[str, Any]:
        return self._request("GET", "/v1/auth/me")

    @staticmethod
    def _parse_handle(response: Mapping[str, Any]) -> RunHandle:
        try:
            run_id = str(response["runId"])
            thread_id = str(response["threadId"])
        except KeyError as exc:
            raise TextToSqlApiError(f"run handle missing {exc.args[0]}") from exc
        return RunHandle(
            run_id=run_id,
            thread_id=thread_id,
            status_url=str(response.get("statusUrl") or f"/v1/runs/{run_id}"),
            result_url=str(
                response.get("resultUrl") or f"/v1/runs/{run_id}/result"
            ),
            cancel_url=str(
                response.get("cancelUrl") or f"/v1/runs/{run_id}/cancel"
            ),
        )

    def get_run(self, run_id: str) -> RunStatus:
        response = self._request("GET", f"/v1/runs/{run_id}")
        return RunStatus(
            run_id=str(response.get("runId") or run_id),
            thread_id=(
                str(response["threadId"]) if response.get("threadId") is not None else None
            ),
            status=str(response.get("status") or "unknown"),
            error=(str(response["error"]) if response.get("error") else None),
        )

    def get_result(self, run_id: str) -> TextToSqlResult:
        response = self._request("GET", f"/v1/runs/{run_id}/result")
        result_envelope = response.get("result", response)
        if not isinstance(result_envelope, Mapping):
            raise TextToSqlApiError("run result must be an object")
        artifacts = result_envelope.get("artifacts", {})
        if not isinstance(artifacts, Mapping):
            raise TextToSqlApiError("run result artifacts must be an object")
        result = result_envelope.get("terminal_outcome", result_envelope)
        if not isinstance(result, Mapping):
            raise TextToSqlApiError("terminal outcome must be an object")
        return _validate_terminal_outcome(
            result,
            expected_run_id=run_id,
            final_output=artifacts.get("final_output"),
        )

    def cancel(self, run_id: str) -> RunStatus:
        response = self._request("POST", f"/v1/runs/{run_id}/cancel", {})
        response_status = response.get("status")
        if response_status is None:
            response_status = (
                "cancelled"
                if response.get("cancelled") is True
                else "not_cancelled"
            )
        return RunStatus(
            run_id=str(response.get("runId") or run_id),
            status=str(response_status),
            thread_id=(
                str(response["threadId"]) if response.get("threadId") is not None else None
            ),
        )

    def _run_service_action(
        self,
        action: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        response = self._request(
            "POST",
            "/v1/runs",
            self._run_body(action=action, payload=payload, query=action),
        )
        if "action" in response and "ok" in response:
            envelope = response
        else:
            handle = self._parse_handle(response)
            for _ in range(self.max_poll_attempts):
                status = self.get_run(handle.run_id)
                if status.status in TERMINAL_RUN_STATUSES:
                    result_response = self._request("GET", handle.result_url)
                    envelope = result_response.get("result", result_response)
                    break
                time.sleep(self.poll_interval_seconds)
            else:
                raise TextToSqlApiError(f"service action '{action}' timed out")
        if not isinstance(envelope, Mapping):
            raise TextToSqlApiError("service action result must be an object")
        if envelope.get("ok") is False:
            raise TextToSqlApiError(str(envelope.get("error") or action))
        data = envelope.get("data", envelope)
        if not isinstance(data, Mapping):
            raise TextToSqlApiError("service action data must be an object")
        return data

    def list_connections(self) -> list[ConnectionSummary]:
        data = self._request("GET", "/v1/text-to-sql/connections")
        connections = data.get("connections") or []
        if not isinstance(connections, list):
            raise TextToSqlApiError("connection list must be an array")
        return [_connection_summary(item) for item in connections]

    def register_connection(
        self,
        *,
        display_name: str,
        dsn: str,
        owner_subject: str | None,
        tenant_id: str,
        enabled_for_user: bool = True,
    ) -> ConnectionSummary:
        data = self._request(
            "POST",
            "/v1/text-to-sql/connections",
            {
                "display_name": display_name,
                "dsn": dsn,
                "owner_subject": owner_subject,
                "tenant_id": tenant_id,
                "enabled_for_user": enabled_for_user,
            },
        )
        connection = data.get("connection")
        return _connection_summary(connection)

    def load_schema(self, connection_ref: str) -> SchemaResult:
        if not connection_ref.strip():
            raise ValueError("connection_ref is required")
        data = self._run_service_action(
            "text_to_sql.schema.load",
            {"connection_ref": connection_ref},
        )
        schema = data.get("schema", data)
        if not isinstance(schema, Mapping):
            raise TextToSqlApiError("schema result must be an object")
        return SchemaResult(
            tables=[dict(table) for table in schema.get("tables") or []],
            raw=dict(schema),
        )

    def list_history(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        data = self._run_service_action(
            "text_to_sql.history.list",
            {"limit": limit, "offset": offset},
        )
        entries = data.get("entries")
        if not isinstance(entries, list) or not all(
            isinstance(entry, Mapping) for entry in entries
        ):
            raise TextToSqlApiError("history entries must be a list of objects")
        return [dict(entry) for entry in entries]

    def history_analytics(self) -> dict[str, Any]:
        data = self._run_service_action("text_to_sql.history.analytics", {})
        result = data.get("result")
        if not isinstance(result, Mapping):
            raise TextToSqlApiError("history analytics must be an object")
        return dict(result)

    def clear_history(self) -> int:
        data = self._run_service_action(
            "text_to_sql.history.clear",
            {"confirm": True},
        )
        cleared = data.get("cleared")
        if type(cleared) is not int or cleared < 0:
            raise TextToSqlApiError("history clear count must be a non-negative integer")
        return cleared
