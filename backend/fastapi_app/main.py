"""FastAPI gateway implementing the AG-UI HTTP/SSE protocol."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from logging_setup import setup_comprehensive_logging

from backend.fastapi_app.agui.auth import (
    Principal,
    authenticate_request,
    principal_to_record,
    validate_auth_configuration,
)
from backend.fastapi_app.agui.connection_registry import (
    ConnectionRef,
    ConnectionTargetPolicyError,
)
from backend.fastapi_app.agui.encoder import EventEncoder
from backend.fastapi_app.agui.events import EventType, RunErrorEvent
from backend.fastapi_app.agui.models import RunAgentInput
from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload
from backend.fastapi_app.agui.run_manager import (
    RequestReplayView,
    RunManager,
    is_terminal_event,
)
from backend.fastapi_app.agui.retention import (
    create_operational_retention_coordinator,
)
from backend.fastapi_app.agui.store import EventStore
from backend.fastapi_app.readiness import (
    ReadinessReport,
    collect_text2sql_readiness,
    runtime_for_application,
)
from workflow.state_files import (
    LegacyDatabaseUnrecoverable,
    default_state_database_path,
)
from workflow.streamlit_api import configure_workflow_process_supervisor

APP_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = APP_ROOT / "data"

log_level_str = os.getenv("SMOLAGENTS_LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)
setup_comprehensive_logging(log_level=log_level)

_LEGACY_AGUI_EVENTS_DB_PATH = DATA_DIR / "agui_events.db"
try:
    DB_PATH = default_state_database_path(
        APP_ROOT,
        "agui_events.db",
        legacy_path=_LEGACY_AGUI_EVENTS_DB_PATH,
    )
except LegacyDatabaseUnrecoverable as exc:
    logging.getLogger(__name__).critical(
        "Legacy AGUI event store database is unrecoverable "
        "(reason_code=%s): %s",
        exc.reason_code,
        _LEGACY_AGUI_EVENTS_DB_PATH,
    )
    raise
LEGACY_TEXT_TO_SQL_HISTORY_PATH = APP_ROOT / "logs" / "sql_history.jsonl"

store = EventStore(str(DB_PATH))
run_manager = RunManager(store)


def _import_legacy_text_to_sql_history_once():
    try:
        raw_content = LEGACY_TEXT_TO_SQL_HISTORY_PATH.read_bytes()
    except FileNotFoundError:
        return None
    source_path = str(LEGACY_TEXT_TO_SQL_HISTORY_PATH.resolve())
    content_sha256 = hashlib.sha256(raw_content).hexdigest()
    source_identity = hashlib.sha256(source_path.encode("utf-8")).hexdigest()
    lines = [
        line
        for line in raw_content.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    result = store.import_legacy_text_to_sql_history_once(
        lines,
        source_identity=source_identity,
        source_path=source_path,
        content_sha256=content_sha256,
    )
    logging.getLogger(__name__).info(
        "Legacy Text-to-SQL history import %s: imported=%d duplicates=%d "
        "quarantined=%d",
        "already completed" if result.already_completed else "completed",
        result.imported,
        result.duplicates,
        result.quarantined,
    )
    return result


def _drain_workflow_results_before_reconcile() -> frozenset[str] | None:
    from workflow.result_outbox import WorkflowResultOutbox
    from workflow.streamlit_api import (
        _drain_workflow_result_outbox_batch,
        _schedule_workflow_result_outbox_drain,
        _workflow_result_outbox_path,
    )

    try:
        path = _workflow_result_outbox_path()
        if not path.exists():
            return frozenset()
    except Exception:
        logging.getLogger(__name__).exception(
            "Unable to locate WORKFLOW_RESULT outbox before reconciliation"
        )
        return None
    def schedule_retry() -> None:
        try:
            _schedule_workflow_result_outbox_drain(path=path)
        except Exception:
            logging.getLogger(__name__).exception(
                "Unable to schedule WORKFLOW_RESULT outbox drain"
            )

    try:
        deadline = time.monotonic() + 5
        while True:
            delivered, _retryable = _drain_workflow_result_outbox_batch(
                limit=1000,
                time_budget_seconds=0.25,
                path=path,
            )
            outbox = WorkflowResultOutbox(str(path))
            try:
                pending_run_ids = outbox.pending_run_ids()
            finally:
                outbox.close()
            if not pending_run_ids:
                return frozenset()
            if delivered == 0 or time.monotonic() >= deadline:
                schedule_retry()
                return pending_run_ids
    except Exception:
        logging.getLogger(__name__).exception(
            "Unable to inspect WORKFLOW_RESULT outbox before reconciliation"
        )
        schedule_retry()
        return None


def _durable_supervised_run_ids() -> frozenset[str]:
    return frozenset(
        stored.run_id
        for stored in store.list_non_terminal_runs()
        if store.has_work_spec(stored.run_id)
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    validate_auth_configuration()
    _import_legacy_text_to_sql_history_once()
    supervisor = configure_workflow_process_supervisor(store)
    retention = create_operational_retention_coordinator(
        store, executor=run_manager.store_executor
    )
    if _app is not None:
        _app.state.workflow_supervisor = supervisor
        _app.state.operational_retention = retention
    pending_run_ids = _drain_workflow_results_before_reconcile()
    if pending_run_ids is None or pending_run_ids is False:
        logging.getLogger(__name__).warning(
            "Skipping orphan reconciliation because WORKFLOW_RESULT outbox "
            "identities could not be inspected"
        )
    else:
        store.reconcile_non_terminal_runs(
            reason="SERVER_RESTARTED",
            exclude_run_ids=pending_run_ids | _durable_supervised_run_ids(),
        )
    supervisor.start()
    try:
        await retention.start()
        yield
    finally:
        try:
            await retention.stop()
        finally:
            try:
                supervisor.stop()
            finally:
                # run_manager.close() намеренно НЕ вызывается здесь: тесты
                # используют _lifespan как bootstrap-фазу и продолжают дергать
                # роуты после выхода из него; в проде процесс завершается
                # сразу после teardown, executor освобождается вместе с ним.
                if _app is not None:
                    _app.state.workflow_supervisor = None
                    _app.state.operational_retention = None


app = FastAPI(title="AG-UI Gateway", lifespan=_lifespan)

cors_raw = os.getenv(
    "AG_UI_CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173",
)
cors_origins = [origin.strip() for origin in cors_raw.split(",") if origin.strip()]
allow_all = "*" in cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if allow_all else cors_origins,
    allow_credentials=not allow_all,
    allow_methods=["*"],
    allow_headers=["*"],
)


_CONNECTION_METADATA_FIELDS = (
    "connection_ref",
    "display_name",
    "owner_subject",
    "tenant_id",
    "target_kind",
    "dialect",
    "target_description",
    "created_at",
    "enabled_for_user",
)
_CONNECTION_REGISTRATION_VALIDATION_ERRORS = frozenset(
    {
        "owner_subject is required; use null for tenant-wide scope",
        "tenant_id is required",
        "display_name is required",
        "dsn is required",
        "display_name must be a non-empty trimmed string",
        "owner_subject must be a non-empty trimmed string",
        "tenant_id must be a non-empty trimmed string",
        "enabled_for_user must be boolean",
    }
)


def _is_connection_registration_validation_error(exc: ValueError) -> bool:
    return isinstance(exc, ConnectionTargetPolicyError) or str(exc) in (
        _CONNECTION_REGISTRATION_VALIDATION_ERRORS
    )


def _connection_metadata_record(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=500, detail="connection metadata is invalid")
    connection_ref = value.get("connection_ref")
    if not isinstance(connection_ref, str) or not connection_ref:
        raise HTTPException(status_code=500, detail="connection metadata is invalid")
    try:
        ConnectionRef(connection_ref)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail="connection metadata is invalid",
        ) from exc
    return {field: value[field] for field in _CONNECTION_METADATA_FIELDS if field in value}


def _connection_metadata_response(
    result: Mapping[str, object],
    *,
    field: str,
) -> dict[str, object]:
    value = result.get(field)
    if field == "connection":
        return {field: _connection_metadata_record(value)}
    if not isinstance(value, list):
        raise HTTPException(status_code=500, detail="connection metadata is invalid")
    return {field: [_connection_metadata_record(item) for item in value]}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def application_readiness(current_app: FastAPI) -> ReadinessReport:
    runtime = runtime_for_application(
        current_app,
        project_root=APP_ROOT,
        event_store_path=DB_PATH,
    )
    return collect_text2sql_readiness(runtime)


@app.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    report = await asyncio.to_thread(application_readiness, request.app)
    return JSONResponse(status_code=report.status_code, content=report.body)


@app.get("/v1/auth/me")
async def auth_me(request: Request) -> dict[str, object]:
    return principal_to_record(authenticate_request(request))


@app.get("/v1/text-to-sql/connections")
async def list_text_to_sql_connections(request: Request) -> dict[str, object]:
    principal = authenticate_request(request)
    from backend.fastapi_app.agui.service import handle_service_action

    try:
        result = await asyncio.to_thread(
            handle_service_action,
            "db.connections.list",
            {},
            principal,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return _connection_metadata_response(result, field="connections")


@app.post("/v1/text-to-sql/connections")
async def register_text_to_sql_connection(
    payload: dict[str, Any],
    request: Request,
) -> dict[str, object]:
    principal = authenticate_request(request)
    from backend.fastapi_app.agui.service import handle_service_action

    try:
        result = await asyncio.to_thread(
            handle_service_action,
            "db.connections.register",
            payload,
            principal,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        if _is_connection_registration_validation_error(exc):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise
    return _connection_metadata_response(result, field="connection")


@app.post("/agent")
async def agent_endpoint(input_data: RunAgentInput, request: Request) -> StreamingResponse:
    principal = authenticate_request(request)
    input_data = _with_server_run_id(input_data)
    accept_header = request.headers.get("accept")
    encoder = run_manager.encoder(accept_header)

    try:
        info, replay_view = await run_manager.start_run_request_view(
            input_data,
            principal=principal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return StreamingResponse(
        _stream_agent_events(
            info.run_id,
            request,
            encoder,
            request_view=replay_view,
        ),
        media_type=encoder.get_content_type(),
    )


@app.post("/v1/runs")
async def create_run(input_data: RunAgentInput, request: Request) -> dict[str, str]:
    principal = authenticate_request(request)
    input_data = _with_server_run_id(input_data)
    try:
        info = await run_manager.start_run(input_data, principal=principal)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "threadId": info.thread_id,
        "runId": info.run_id,
        "eventsUrl": str(request.url_for("v1_run_events", run_id=info.run_id)),
        "cancelUrl": str(request.url_for("v1_run_cancel", run_id=info.run_id)),
        "statusUrl": str(request.url_for("v1_run_status", run_id=info.run_id)),
        "resultUrl": str(request.url_for("v1_run_result", run_id=info.run_id)),
    }


def _encode_payload(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


_AGUI_EVENT_TYPES = {event_type.value for event_type in EventType}
_AGUI_TERMINAL_EVENT_TYPES = {
    EventType.RUN_FINISHED.value,
    EventType.RUN_ERROR.value,
}


def _is_text_to_sql_transport_projection(
    event_type: str,
    payload: dict,
) -> bool:
    if event_type in _AGUI_TERMINAL_EVENT_TYPES:
        return True
    return (
        event_type == EventType.CUSTOM.value
        and payload.get("name") == "workflow.result"
    )


def _redact_gateway_payload(payload):
    return redact_pii_in_payload(_redact_payload(payload))


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:16]}"


def _with_server_run_id(input_data: RunAgentInput) -> RunAgentInput:
    return input_data.model_copy(update={"run_id": _new_run_id()})


async def _call_store(func, *args, **kwargs):
    """Run a blocking EventStore call off the event loop thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        run_manager.store_executor, functools.partial(func, *args, **kwargs)
    )


async def _ensure_run_access(run_id: str, principal: Principal) -> None:
    if not await run_manager.can_access(run_id, principal):
        raise HTTPException(status_code=404, detail="run not found")


async def _cancel_if_orphaned(run_id: str) -> None:
    """Cancel live `/agent` runs when the disconnect leaves no subscribers."""
    await run_manager.cancel_if_orphaned(run_id)


async def _check_disconnected(request: Request) -> bool:
    """Return True if the client has disconnected; treat exceptions as disconnect."""
    try:
        return await request.is_disconnected()
    except Exception:
        return True


async def _stream_agent_events(
    run_id: str,
    request: Request,
    encoder: EventEncoder,
    *,
    after: int = 0,
    request_view: RequestReplayView | None = None,
) -> AsyncIterator[str]:
    stream = run_manager.stream_live(
        run_id,
        after=after,
        request_view=request_view,
    )
    terminal_seen = False
    try:
        async for event in stream:
            if await _check_disconnected(request):
                break
            if is_terminal_event(event):
                terminal_seen = True
            yield encoder.encode(event)
    finally:
        await stream.aclose()
        if not terminal_seen:
            await _cancel_if_orphaned(run_id)


@app.get("/agent/{run_id}/events")
async def replay_events(
    run_id: str,
    request: Request,
    after: int = 0,
    follow: bool = True,
) -> StreamingResponse:
    principal = authenticate_request(request)
    await _ensure_run_access(run_id, principal)
    accept_header = request.headers.get("accept")
    encoder = EventEncoder(accept=accept_header)
    info = run_manager.get_info(run_id)

    async def replay_stream() -> AsyncIterator[str]:
        last_seq = after
        terminal_view = await run_manager.durable_terminal_view(run_id)
        authoritative_projection_emitted = False
        workflow_projection_emitted = False
        terminal_projection_emitted = False
        stored_events = await _call_store(
            lambda: list(store.list_after(run_id, after))
        )
        for stored in stored_events:
            last_seq = stored.seq
            if (
                terminal_view is not None
                and stored.seq == terminal_view[0]
                and after < terminal_view[0]
            ):
                for event in terminal_view[1]:
                    if await _check_disconnected(request):
                        return
                    yield encoder.encode(event)
                authoritative_projection_emitted = True
                workflow_projection_emitted = True
                terminal_projection_emitted = True
            if stored.event_type not in _AGUI_EVENT_TYPES:
                continue
            if (
                terminal_view is not None
                and _is_text_to_sql_transport_projection(
                    stored.event_type,
                    stored.payload,
                )
            ):
                if after < terminal_view[0]:
                    continue
                if (
                    stored.event_type == EventType.CUSTOM.value
                    and stored.payload.get("name") == "workflow.result"
                ):
                    if not workflow_projection_emitted:
                        if await _check_disconnected(request):
                            return
                        yield encoder.encode(terminal_view[1][0])
                        workflow_projection_emitted = True
                elif not terminal_projection_emitted:
                    if await _check_disconnected(request):
                        return
                    yield encoder.encode(terminal_view[1][1])
                    terminal_projection_emitted = True
                continue
            if await _check_disconnected(request):
                return
            if stored.event_type in _AGUI_TERMINAL_EVENT_TYPES:
                terminal_projection_emitted = True
            yield _encode_payload(_redact_gateway_payload(stored.payload))
        if follow and info is not None:
            stream = run_manager.stream_live(run_id, after=last_seq)
            try:
                async for event in stream:
                    if await _check_disconnected(request):
                        break
                    current_view = await run_manager.durable_terminal_view(run_id)
                    event_payload = event.model_dump(
                        by_alias=True,
                        exclude_none=True,
                    )
                    if (
                        current_view is not None
                        and _is_text_to_sql_transport_projection(
                            event.type.value,
                            event_payload,
                        )
                    ):
                        if after < current_view[0]:
                            continue
                        if (
                            event.type == EventType.CUSTOM
                            and event_payload.get("name") == "workflow.result"
                        ):
                            if not workflow_projection_emitted:
                                yield encoder.encode(current_view[1][0])
                                workflow_projection_emitted = True
                        elif not terminal_projection_emitted:
                            yield encoder.encode(current_view[1][1])
                            terminal_projection_emitted = True
                        continue
                    if event.type.value in _AGUI_TERMINAL_EVENT_TYPES:
                        terminal_projection_emitted = True
                    yield encoder.encode(event)
            finally:
                await stream.aclose()

        terminal_view = await run_manager.durable_terminal_view(run_id)
        if (
            terminal_view is not None
            and after < terminal_view[0]
            and not authoritative_projection_emitted
        ):
            for event in terminal_view[1]:
                if await _check_disconnected(request):
                    return
                yield encoder.encode(event)
            return

        stored_run = await _call_store(store.get_run, run_id)
        if (
            not terminal_projection_emitted
            and stored_run is not None
            and stored_run.run_kind == "text_to_sql"
            and stored_run.status == "failed"
            and stored_run.result_seq is None
            and stored_run.terminal_reason == "SERVER_RESTARTED"
        ):
            yield encoder.encode(
                RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message="Text-to-SQL run was interrupted by server restart",
                    code="text_to_sql_server_restarted",
                    timestamp=stored_run.finished_at_ms,
                )
            )

    return StreamingResponse(replay_stream(), media_type=encoder.get_content_type())


@app.get("/v1/runs/{run_id}/events", name="v1_run_events")
async def replay_events_v1(
    run_id: str,
    request: Request,
    after: int = 0,
    follow: bool = True,
) -> StreamingResponse:
    return await replay_events(run_id=run_id, request=request, after=after, follow=follow)


@app.get("/agent/{run_id}")
async def run_status(run_id: str, request: Request) -> dict[str, str | int | None]:
    principal = authenticate_request(request)
    await _ensure_run_access(run_id, principal)
    stored_run = await _call_store(store.get_run, run_id)
    if stored_run is None:
        cached_run = run_manager.get_info(run_id)
        if cached_run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "run_id": cached_run.run_id,
            "thread_id": cached_run.thread_id,
            "status": cached_run.status.value,
            "started_at_ms": cached_run.started_at_ms,
            "finished_at_ms": cached_run.finished_at_ms,
        }
    status = {
        "pending": "pending",
        "queued": "queued",
        "running": "running",
        "result_pending": "running",
        "succeeded": "finished",
        "abstained": "finished",
        "failed": "errored",
        "cancelled": "cancelled",
        "timed_out": "errored",
        "legacy": "pending",
    }[stored_run.status]
    return {
        "run_id": stored_run.run_id,
        "thread_id": stored_run.thread_id,
        "status": status,
        "started_at_ms": stored_run.created_at_ms,
        "finished_at_ms": stored_run.finished_at_ms,
    }


@app.get("/v1/runs/{run_id}", name="v1_run_status")
async def run_status_v1(run_id: str, request: Request) -> dict[str, str | int | None]:
    status_payload = await run_status(run_id=run_id, request=request)
    return {
        "runId": status_payload["run_id"],
        "threadId": status_payload["thread_id"],
        "status": status_payload["status"],
        "startedAtMs": status_payload["started_at_ms"],
        "finishedAtMs": status_payload["finished_at_ms"],
    }


@app.post("/agent/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict[str, bool]:
    principal = authenticate_request(request)
    await _ensure_run_access(run_id, principal)
    cached_cancelled = await run_manager.cancel_cached(run_id)
    cancelled = (
        await run_manager.cancel_uncached_text_to_sql(run_id, principal)
        if cached_cancelled is None
        else cached_cancelled
    )
    if not cancelled:
        if await _call_store(store.get_run, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
    return {"cancelled": cancelled}


@app.post("/v1/runs/{run_id}/cancel", name="v1_run_cancel")
async def cancel_run_v1(run_id: str, request: Request) -> dict[str, bool]:
    return await cancel_run(run_id=run_id, request=request)


@app.get("/v1/runs/{run_id}/result", name="v1_run_result")
async def run_result_v1(run_id: str, request: Request) -> dict[str, object | None]:
    principal = authenticate_request(request)
    await _ensure_run_access(run_id, principal)
    latest_result = None
    latest_service_result = None
    latest_workflow_result = None
    stored_run = await _call_store(store.get_run, run_id)
    if stored_run is not None and stored_run.result_seq is not None:
        result_event = await _call_store(
            store.get_event, run_id, stored_run.result_seq
        )
        if result_event is None or result_event.event_type != "WORKFLOW_RESULT":
            raise HTTPException(
                status_code=500,
                detail="run result reference is invalid",
            )
        latest_result = result_event.payload
    stored_events = await _call_store(lambda: list(store.list_after(run_id, 0)))
    for stored in stored_events:
        if stored_run is not None and stored_run.run_kind == "text_to_sql":
            continue
        if stored.event_type == EventType.RUN_FINISHED.value:
            latest_result = stored.payload.get("result")
        elif stored.event_type == EventType.CUSTOM.value:
            if stored.payload.get("name") == "service.result":
                latest_service_result = stored.payload.get("value")
            elif stored.payload.get("name") == "workflow.result":
                latest_workflow_result = stored.payload.get("value")
    if latest_result is None and latest_service_result is not None:
        latest_result = latest_service_result
    if latest_result is None and latest_workflow_result is not None:
        latest_result = latest_workflow_result
    if latest_result is None:
        if stored_run is None:
            raise HTTPException(status_code=404, detail="run not found")
    public_result = _redact_gateway_payload(latest_result)
    if (
        stored_run is not None
        and stored_run.run_kind == "text_to_sql"
        and isinstance(public_result, dict)
        and isinstance(public_result.get("terminal_outcome"), dict)
    ):
        public_result["terminal_outcome"]["run_id"] = run_id
    return {"result": public_result}
