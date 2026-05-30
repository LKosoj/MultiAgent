"""FastAPI gateway implementing the AG-UI HTTP/SSE protocol."""

from __future__ import annotations

import json
import os
import logging
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from logging_setup import setup_comprehensive_logging

from backend.fastapi_app.agui.encoder import EventEncoder
from backend.fastapi_app.agui.events import EventType
from backend.fastapi_app.agui.models import RunAgentInput
from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload
from backend.fastapi_app.agui.run_manager import RunManager, is_terminal_event
from backend.fastapi_app.agui.store import EventStore

APP_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "agui_events.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)

log_level_str = os.getenv("SMOLAGENTS_LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)
setup_comprehensive_logging(log_level=log_level)

store = EventStore(str(DB_PATH))
run_manager = RunManager(store)

app = FastAPI(title="AG-UI Gateway")

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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent")
async def agent_endpoint(input_data: RunAgentInput, request: Request) -> StreamingResponse:
    accept_header = request.headers.get("accept")
    encoder = run_manager.encoder(accept_header)

    try:
        info = await run_manager.start_run(input_data)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return StreamingResponse(
        _stream_agent_events(info.run_id, request, encoder),
        media_type=encoder.get_content_type(),
    )


@app.post("/v1/runs")
async def create_run(input_data: RunAgentInput, request: Request) -> dict[str, str]:
    try:
        info = await run_manager.start_run(input_data)
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


def _redact_gateway_payload(payload):
    return redact_pii_in_payload(_redact_payload(payload))


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
) -> AsyncIterator[str]:
    stream = run_manager.stream_live(run_id)
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
    accept_header = request.headers.get("accept")
    encoder = EventEncoder(accept=accept_header)
    info = run_manager.get_info(run_id)

    async def replay_stream() -> AsyncIterator[str]:
        last_seq = after
        for stored in store.list_after(run_id, after):
            last_seq = stored.seq
            if stored.event_type not in _AGUI_EVENT_TYPES:
                continue
            if await _check_disconnected(request):
                return
            yield _encode_payload(_redact_gateway_payload(stored.payload))
        if follow and info is not None:
            stream = run_manager.stream_live(run_id, after=last_seq)
            try:
                async for event in stream:
                    if await _check_disconnected(request):
                        break
                    yield encoder.encode(event)
            finally:
                await stream.aclose()

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
async def run_status(run_id: str) -> dict[str, str | int | None]:
    info = run_manager.get_info(run_id)
    if info is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "run_id": info.run_id,
        "thread_id": info.thread_id,
        "status": info.status.value,
        "started_at_ms": info.started_at_ms,
        "finished_at_ms": info.finished_at_ms,
    }


@app.get("/v1/runs/{run_id}", name="v1_run_status")
async def run_status_v1(run_id: str) -> dict[str, str | int | None]:
    info = run_manager.get_info(run_id)
    if info is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "runId": info.run_id,
        "threadId": info.thread_id,
        "status": info.status.value,
        "startedAtMs": info.started_at_ms,
        "finishedAtMs": info.finished_at_ms,
    }


@app.post("/agent/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, bool]:
    cancelled = await run_manager.cancel(run_id)
    if not cancelled:
        info = run_manager.get_info(run_id)
        if info is None:
            raise HTTPException(status_code=404, detail="run not found")
    return {"cancelled": cancelled}


@app.post("/v1/runs/{run_id}/cancel", name="v1_run_cancel")
async def cancel_run_v1(run_id: str) -> dict[str, bool]:
    return await cancel_run(run_id=run_id)


@app.get("/v1/runs/{run_id}/result", name="v1_run_result")
async def run_result_v1(run_id: str) -> dict[str, object | None]:
    latest_result = None
    latest_service_result = None
    latest_workflow_result = None
    for stored in store.list_after(run_id, 0):
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
        info = run_manager.get_info(run_id)
        if info is None:
            raise HTTPException(status_code=404, detail="run not found")
    return {"result": _redact_gateway_payload(latest_result)}
