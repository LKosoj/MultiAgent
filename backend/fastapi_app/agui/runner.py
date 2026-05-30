"""Adapter that executes the local MultiAgent system and emits AG-UI events."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional, Tuple

from agent_system import DynamicAgentSystem

from .events import (
    EventType,
    CustomEvent,
    MessagesSnapshotEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from .models import RunAgentInput, UserMessage
from .redaction import _redact_payload, redact_pii_in_payload
from .errors import ForbiddenWorkflowNameError
from .workflow_metadata import workflow_agui_entrypoint
from unified_logging import get_logging_manager, run_id_context
from utils import call_openai_api_streaming


logger = logging.getLogger(__name__)

# Интервал delta-polling для workflow.progress.
# WorkflowManager хранит активные запуски в дочернем процессе, поэтому
# progress_callback из родителя недоступен; используется опрос
# get_workflow_status с эмиссией событий только при изменении.
_WORKFLOW_POLL_INTERVAL_SECONDS = 1.0

# Таймаут, в течение которого мы ждём завершения дочернего процесса workflow
# после вызова cancel_workflow. cancel_workflow внутри уже выполняет
# SIGTERM(+SIGKILL) с собственными join'ами; этот таймаут — финальная проверка
# что процесс действительно мёртв.
_WORKFLOW_CANCEL_JOIN_TIMEOUT_SECONDS = 5.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _extract_task(input_data: RunAgentInput) -> str:
    for message in reversed(input_data.messages):
        if isinstance(message, UserMessage):
            if isinstance(message.content, str):
                return message.content
            parts: list[str] = []
            for chunk in message.content:
                if getattr(chunk, "type", None) == "text":
                    parts.append(chunk.text)
            return "\n".join([part for part in parts if part])
    forwarded = input_data.forwarded_props
    if isinstance(forwarded, dict) and isinstance(forwarded.get("task"), str):
        return forwarded["task"]
    return ""


def _chunk_text(text: str, chunk_size: int = 800) -> Iterable[str]:
    if not text:
        return []
    return (text[i : i + chunk_size] for i in range(0, len(text), chunk_size))


def _should_use_workflow(forwarded: dict[str, Any]) -> bool:
    if forwarded.get("execution_mode") == "workflow":
        return True
    if forwarded.get("workflow", False) is True:
        return True
    if "workflow_name" in forwarded:
        return True
    return False


def _should_use_dialog_utils(forwarded: dict[str, Any]) -> bool:
    return forwarded.get("dialog_mode") == "utils"


def _resolve_workflow_name(
    forwarded: dict[str, Any],
) -> Tuple[Optional[str], Optional[Path], Optional[str]]:
    if "workflow_name" in forwarded:
        return str(forwarded["workflow_name"]).strip(), Path("workflow_pipelines"), None
    return None, None, "workflow_name not provided"


def _workflow_agui_entrypoint(workflow_name: str, pipelines_dir: Path) -> Optional[str]:
    return workflow_agui_entrypoint(workflow_name, pipelines_dir)


class ServicePayloadInvalidError(ValueError):
    pass


_TEXT_TO_SQL_SERVICE_ACTION = "presets.text_to_sql.generate"


class TextToSqlServiceActionRequiredError(ValueError):
    """Поднимается, когда forwardedProps обходит text-to-sql service action."""

    pass


def _normalize_service_payload(raw_payload: Any) -> dict[str, Any]:
    if raw_payload is None:
        return {}
    if not isinstance(raw_payload, dict):
        raise ServicePayloadInvalidError("service_payload must be an object")
    return raw_payload


def _service_result_envelope(
    service_action: str,
    ok: bool,
    data: Any,
    request_id: Any,
) -> dict[str, Any]:
    return {
        "action": service_action,
        "ok": ok,
        "data": data,
        "__request_id": request_id,
    }


def _build_workflow_envelope(
    workflow_run_id: str,
    workflow_name: str,
    status: str,
    final_output: Any,
    artifacts_ref: Optional[str],
    code: Optional[str] = None,
) -> dict[str, Any]:
    """Формирует service envelope для workflow-результата.

    Соответствует контракту RUN_FINISHED.result для service actions (см.
    doc/AG_UI_SERVICE_ACTIONS.md, секция «Workflow execution via forwardedProps»).
    Все значения вытаскиваются из реальных полей WorkflowManager.* (status,
    artifacts), без хардкода и без подмены None пустой строкой.

    Параметр ``code`` — опциональный код результата (например, ``"cancelled"`` для
    cancel-ветки). Если ``None``, поле в envelope не добавляется (additive-only).
    """
    envelope: dict[str, Any] = {
        "type": "workflow_result",
        "workflow_run_id": workflow_run_id,
        "workflow_name": workflow_name,
        "status": status,
        "final_output": final_output,
        "artifacts_ref": artifacts_ref,
    }
    if code is not None:
        envelope["code"] = code
    return envelope


def _resolve_artifacts_ref(artifacts: Any) -> Optional[str]:
    """Возвращает URL/путь к артефактам, если manager его сообщил, иначе None.

    Не выдумывает значения: используется только то, что вернул
    `WorkflowManager.get_workflow_artifacts` (`logs_path`/`traces_path`).
    """
    if artifacts is None:
        return None
    logs_path = getattr(artifacts, "logs_path", None)
    if isinstance(logs_path, str) and logs_path:
        return logs_path
    traces_path = getattr(artifacts, "traces_path", None)
    if isinstance(traces_path, str) and traces_path:
        return traces_path
    return None


async def _run_workflow(
    input_data: RunAgentInput,
    task: str,
    forwarded: dict[str, Any],
) -> AsyncIterator[Any]:
    """Запускает workflow и поэтапно эмитит AG-UI события.

    Yield порядка:
        1. CustomEvent ``workflow.started`` сразу после успешного старта workflow,
           несёт ``workflow_run_id``/``session_id`` для последующего cancel/result.
        2. CustomEvent ``workflow.progress`` при изменении наблюдаемого среза
           (status / current_step / progress_percentage).
        3. Финальный envelope dict (`type=workflow_result`) — последний yield.
           Содержит status, final_output и artifacts_ref. Вызывающий код
           использует `final_output` для текстового стриминга и сам envelope
           для RunFinishedEvent.result.
        4. При asyncio.CancelledError — эмитится CustomEvent
           ``workflow.result`` с envelope (status=cancelled) перед re-raise,
           так как RunFinishedEvent в cancel-ветке не достигается.
    """
    from workflow.streamlit_api import WorkflowManager

    def _final_envelope_from_status(final_status_obj: Any) -> dict[str, Any]:
        artifacts = manager.get_workflow_artifacts(workflow_run_id)
        final_status = final_status_obj.status if final_status_obj else "unknown"
        if final_status == "failed":
            final_output: Any = final_status_obj.error_message if final_status_obj else None
        elif final_status == "cancelled":
            final_output = None
            artifacts = None
        else:
            final_output = artifacts.final_output if artifacts is not None else None
        return _build_workflow_envelope(
            workflow_run_id=workflow_run_id,
            workflow_name=workflow_name,
            status=final_status,
            final_output=final_output,
            artifacts_ref=_resolve_artifacts_ref(artifacts),
        )

    def _cancelled_envelope() -> dict[str, Any]:
        return _build_workflow_envelope(
            workflow_run_id=workflow_run_id,
            workflow_name=workflow_name,
            status="cancelled",
            final_output=None,
            artifacts_ref=None,
            code="cancelled",
        )

    workflow_name, pipelines_dir, error = _resolve_workflow_name(forwarded)
    if workflow_name is None or pipelines_dir is None:
        raise ValueError(error or "workflow name not resolved")

    agui_entrypoint = _workflow_agui_entrypoint(workflow_name, pipelines_dir)
    if agui_entrypoint == _TEXT_TO_SQL_SERVICE_ACTION:
        raise TextToSqlServiceActionRequiredError(
            "Use presets.text_to_sql.generate service action instead of forwardedProps. "
            "See doc/AG_UI_SERVICE_ACTIONS.md for the full request schema and examples."
        )
    if agui_entrypoint is not None:
        raise ForbiddenWorkflowNameError(
            f"workflow_name='{workflow_name}' is not allowed via forwardedProps. "
            f"Use {agui_entrypoint} service action instead."
        )

    use_enhanced = forwarded.get("use_enhanced_engine")
    if use_enhanced is None:
        use_enhanced = os.getenv("USE_ENHANCED_ENGINE", "true").lower() == "true"

    enable_telemetry = bool(forwarded.get("enable_telemetry", False))

    manager = WorkflowManager(use_enhanced=use_enhanced, pipelines_dir=str(pipelines_dir))

    parameters: dict[str, Any] = {"task": task, "topic": task}
    extra_vars = forwarded.get("variables") or forwarded.get("parameters")
    if isinstance(extra_vars, dict):
        parameters.update(extra_vars)

    from ._t2s_requests import PIPELINE_VALIDATORS

    validator = PIPELINE_VALIDATORS.get(workflow_name)
    if validator is not None:
        from pydantic import ValidationError as _PydValidationError

        try:
            parameters = validator.model_validate(parameters).model_dump()
        except _PydValidationError as exc:
            errors = exc.errors()
            if errors:
                first = errors[0]
                ctx_err = (
                    first.get("ctx", {}).get("error")
                    if isinstance(first.get("ctx"), dict)
                    else None
                )
                msg = str(ctx_err) if ctx_err else first.get("msg") or "invalid parameters"
                loc = first.get("loc") or ()
                loc_text = ".".join(str(part) for part in loc)
                if loc_text and loc_text.lower() not in msg.lower():
                    msg = f"{loc_text}: {msg}"
            else:
                msg = "invalid parameters"
            raise ValueError(
                f"forwardedProps parameters invalid for '{workflow_name}': {msg}"
            ) from exc

    available_names = {wf.name for wf in manager.list_workflows()}
    if workflow_name not in available_names:
        available_list = ", ".join(sorted(available_names)) or "none"
        raise ValueError(
            f"workflow_name not found: {workflow_name}. Available: {available_list}"
        )

    # T3.1: AG-UI run_id и workflow run_id должны быть разными идентификаторами.
    # Формат согласован с presets.text_to_sql.generate в service.py.
    workflow_run_id = f"run-{uuid.uuid4().hex[:16]}"

    started_run_id = manager.start_workflow(
        workflow_name=workflow_name,
        parameters=parameters,
        session_id=input_data.run_id,
        client_id=forwarded.get("client_id"),
        use_enhanced=use_enhanced,
        enable_telemetry=enable_telemetry,
        run_id=workflow_run_id,
    )
    if started_run_id != workflow_run_id:
        raise ValueError(
            f"workflow manager returned unexpected run_id: {started_run_id} != {workflow_run_id}"
        )

    yield CustomEvent(
        type=EventType.CUSTOM,
        name="workflow.started",
        value={
            "workflow_run_id": workflow_run_id,
            "workflow_name": workflow_name,
            "session_id": input_data.run_id,
        },
        timestamp=_now_ms(),
    )

    # T3.2: delta-polling вместо busy-poll. WorkflowManager выполняет workflow
    # в дочернем процессе, поэтому progress_callback, регистрируемый в
    # родителе, не вызывается отсюда (callbacks живут в child-процессе).
    # Опрашиваем get_workflow_status с разумным интервалом и эмитим
    # CustomEvent workflow.progress только при изменении наблюдаемого среза
    # (status / current_step / progress_percentage).
    status = None
    last_progress_key: Optional[Tuple[str, Optional[str], float]] = None
    try:
        while True:
            status = manager.get_workflow_status(workflow_run_id)
            if status is None:
                raise ValueError(f"workflow run not found: {workflow_run_id}")
            progress_key = (
                status.status,
                status.current_step,
                float(status.progress_percentage or 0.0),
            )
            if progress_key != last_progress_key:
                yield CustomEvent(
                    type=EventType.CUSTOM,
                    name="workflow.progress",
                    value={
                        "workflow_run_id": workflow_run_id,
                        "status": status.status,
                        "current_step": status.current_step,
                        "progress_percentage": progress_key[2],
                    },
                    timestamp=_now_ms(),
                )
                last_progress_key = progress_key
            if status.status in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(_WORKFLOW_POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        final_status_before_cancel = manager.get_workflow_status(workflow_run_id)
        if (
            final_status_before_cancel is not None
            and final_status_before_cancel.status in {"completed", "failed"}
        ):
            yield _final_envelope_from_status(final_status_before_cancel)
            return
        # cancel_workflow синхронный (SIGTERM/SIGKILL + proc.join),
        # выносим в executor чтобы не блокировать event loop.
        loop = asyncio.get_running_loop()
        cancel_requested = await loop.run_in_executor(
            None, manager.cancel_workflow, workflow_run_id
        )
        final_status_after_cancel = manager.get_workflow_status(workflow_run_id)
        if (
            final_status_after_cancel is not None
            and final_status_after_cancel.status in {"completed", "failed"}
        ):
            yield _final_envelope_from_status(final_status_after_cancel)
            return
        if (
            final_status_after_cancel is not None
            and final_status_after_cancel.status != "cancelled"
        ):
            raise RuntimeError(
                f"workflow cancel did not reach terminal status: {final_status_after_cancel.status}"
            )
        if cancel_requested is False and not (
            final_status_after_cancel is not None
            and final_status_after_cancel.status == "cancelled"
        ):
            raise RuntimeError("workflow cancel was not accepted")
        # Финальная проверка: cancel_workflow обязан был дождаться завершения
        # дочернего процесса и снять его из реестра. Если процесс всё ещё жив —
        # это баг (SIGKILL не сработал); поднимаем исключение, не глотаем.
        try:
            from workflow.streamlit_api import _GLOBAL_WORKFLOW_PROCESSES

            proc = _GLOBAL_WORKFLOW_PROCESSES.get(workflow_run_id)
        except Exception:
            proc = None
        if proc is not None and proc.is_alive():
            # Дадим ещё один shot на join через executor (не блокируем loop).
            await loop.run_in_executor(
                None, proc.join, _WORKFLOW_CANCEL_JOIN_TIMEOUT_SECONDS
            )
            if proc.is_alive():
                logger.warning(
                    "workflow child process %s still alive after cancel_workflow",
                    workflow_run_id,
                )
                raise RuntimeError(
                    f"workflow child process {workflow_run_id} did not terminate"
                )
        # T3.3: envelope для cancelled workflow. RunFinishedEvent в этой ветке
        # не достигается (run_manager поймает CancelledError и эмитит
        # RunErrorEvent), поэтому кладём envelope в CustomEvent service.result,
        # чтобы клиент мог реконструировать состояние без дополнительного
        # `workflows.result`.
        cancel_envelope = _cancelled_envelope()
        # W1-review: cancel-envelope тоже проходит DSN+PII redaction перед
        # emit'ом — final_output/artifacts могли успеть попасть в envelope
        # из частично выполненного workflow.
        cancel_envelope = redact_pii_in_payload(_redact_payload(cancel_envelope))
        yield CustomEvent(
            type=EventType.CUSTOM,
            name="workflow.result",
            value=cancel_envelope,
            timestamp=_now_ms(),
        )
        raise

    # T3.3: финальный envelope. final_output берётся из реальных полей
    # artifacts/status, без подмены None пустой строкой и без хардкода.
    if status is not None and status.status == "cancelled":
        cancel_envelope = redact_pii_in_payload(_redact_payload(_cancelled_envelope()))
        yield CustomEvent(
            type=EventType.CUSTOM,
            name="workflow.result",
            value=cancel_envelope,
            timestamp=_now_ms(),
        )
        raise asyncio.CancelledError
    yield _final_envelope_from_status(status)


async def _stream_logs(
    run_id: str,
    duration_seconds: float,
) -> AsyncIterator[CustomEvent]:
    logging_manager = get_logging_manager()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _callback(cb_run_id: str, level: str, message: str, timestamp: str) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {
                "run_id": cb_run_id,
                "level": level,
                "message": message,
                "timestamp": timestamp,
            },
        )

    if run_id == "*":
        logging_manager.subscribe_all_logs(_callback)
    else:
        logging_manager.subscribe_run_logs(run_id, _callback)

    deadline = time.time() + duration_seconds
    try:
        while time.time() < deadline:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            yield CustomEvent(
                type=EventType.CUSTOM,
                name="service.log",
                value=redact_pii_in_payload(_redact_payload(item)),
                timestamp=_now_ms(),
            )
    finally:
        if run_id == "*":
            logging_manager.unsubscribe_all_logs(_callback)
        else:
            logging_manager.unsubscribe_run_logs(run_id, _callback)


async def _stream_progress(
    run_id: str,
    duration_seconds: float,
) -> AsyncIterator[CustomEvent]:
    logging_manager = get_logging_manager()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _callback(cb_run_id: str, event_type: str, data: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {
                "run_id": cb_run_id,
                "event_type": event_type,
                "data": data,
            },
        )

    if run_id == "*":
        logging_manager.subscribe_all_progress(_callback)
    else:
        logging_manager.subscribe_run_progress(run_id, _callback)

    deadline = time.time() + duration_seconds
    try:
        while time.time() < deadline:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            yield CustomEvent(
                type=EventType.CUSTOM,
                name="service.progress",
                value=redact_pii_in_payload(_redact_payload(item)),
                timestamp=_now_ms(),
            )
    finally:
        if run_id == "*":
            logging_manager.unsubscribe_all_progress(_callback)
        else:
            logging_manager.unsubscribe_run_progress(run_id, _callback)


async def run_agent(input_data: RunAgentInput) -> AsyncIterator[Any]:
    task = _extract_task(input_data)
    forwarded = input_data.forwarded_props if isinstance(input_data.forwarded_props, dict) else {}
    service_action = forwarded.get("service_action")
    get_logging_manager(str(Path(__file__).resolve().parents[3] / "logs"))
    run_started = RunStartedEvent(
        type=EventType.RUN_STARTED,
        thread_id=input_data.thread_id,
        run_id=input_data.run_id,
        parent_run_id=input_data.parent_run_id,
        input=None,
        timestamp=_now_ms(),
    )
    yield run_started

    if input_data.state is not None:
        yield StateSnapshotEvent(
            type=EventType.STATE_SNAPSHOT,
            snapshot=input_data.state,
            timestamp=_now_ms(),
        )

    if input_data.messages:
        yield MessagesSnapshotEvent(
            type=EventType.MESSAGES_SNAPSHOT,
            messages=input_data.messages,
            timestamp=_now_ms(),
        )

    with run_id_context(input_data.run_id):
        if service_action:
            payload: dict[str, Any] = {}
            try:
                payload = _normalize_service_payload(forwarded.get("service_payload", {}))
                request_id = payload.get("__request_id")
                if service_action == "logs.stream":
                    stream_run_id = payload.get("run_id", "*")
                    duration = float(payload.get("duration_seconds", 30))
                    async for event in _stream_logs(stream_run_id, duration):
                        yield event
                    yield RunFinishedEvent(
                        type=EventType.RUN_FINISHED,
                        thread_id=input_data.thread_id,
                        run_id=input_data.run_id,
                        result=None,
                        timestamp=_now_ms(),
                    )
                    return
                if service_action == "progress.stream":
                    stream_run_id = payload.get("run_id", "*")
                    duration = float(payload.get("duration_seconds", 30))
                    async for event in _stream_progress(stream_run_id, duration):
                        yield event
                    yield RunFinishedEvent(
                        type=EventType.RUN_FINISHED,
                        thread_id=input_data.thread_id,
                        run_id=input_data.run_id,
                        result=None,
                        timestamp=_now_ms(),
                    )
                    return
                from .service import handle_service_action

                result = await asyncio.to_thread(handle_service_action, service_action, payload)
                result_envelope = redact_pii_in_payload(_redact_payload(_service_result_envelope(
                    service_action,
                    True,
                    result,
                    request_id,
                )))
                yield CustomEvent(
                    type=EventType.CUSTOM,
                    name="service.result",
                    value=result_envelope,
                    timestamp=_now_ms(),
                )
                yield RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    thread_id=input_data.thread_id,
                    run_id=input_data.run_id,
                    result=result_envelope,
                    timestamp=_now_ms(),
                )
            except Exception as exc:  # noqa: BLE001
                # W1-review: error message может содержать PII (email/phone/INN)
                # из перехваченного исключения. Сначала DSN-redact, затем PII.
                message = redact_pii_in_payload(_redact_payload(str(exc)))
                code = "service_action_error"
                if isinstance(exc, ServicePayloadInvalidError):
                    code = "service_payload_invalid"
                elif isinstance(exc, ForbiddenWorkflowNameError):
                    code = "forbidden_workflow_name"
                elif isinstance(exc, ValueError) and (
                    message.startswith("workflow_name not found")
                    or message.lower().startswith("workflow not found")
                    or message.startswith("Пайплайн ") and " не найден" in message
                ):
                    code = "workflow_not_found"
                elif message.startswith("Unknown service action"):
                    code = "service_action_invalid"
                request_id = payload.get("__request_id")
                if request_id:
                    result_envelope = redact_pii_in_payload(_redact_payload(_service_result_envelope(
                        service_action,
                        False,
                        {"error": message},
                        request_id,
                    )))
                    yield CustomEvent(
                        type=EventType.CUSTOM,
                        name="service.result",
                        value=result_envelope,
                        timestamp=_now_ms(),
                    )
                yield RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=message,
                    code=code,
                    timestamp=_now_ms(),
                )
            return

        try:
            workflow_envelope: Optional[dict[str, Any]] = None
            if _should_use_workflow(forwarded):
                # _run_workflow поэтапно эмитит CustomEvent (workflow.started,
                # workflow.progress), а финальным yield отдаёт envelope dict
                # вида {type: "workflow_result", workflow_run_id, ...}.
                result = None
                async for item in _run_workflow(input_data, task, forwarded):
                    if isinstance(item, dict) and item.get("type") == "workflow_result":
                        workflow_envelope = item
                        # final_output может быть строкой, dict'ом или None.
                        # Для текстового стриминга берём только то, что реально
                        # было отдано workflow'ом; None оставляем как None
                        # (никакого fallback на "" — это нарушит контракт).
                        result = item.get("final_output")
                    else:
                        yield item
                if workflow_envelope is None:
                    raise ValueError("workflow did not yield a final envelope")
            elif _should_use_dialog_utils(forwarded):
                result = call_openai_api_streaming(
                    prompt=task,
                    system_prompt=forwarded.get("dialog_system_prompt"),
                    model_key=forwarded.get("dialog_model_key"),
                )
            else:
                system = DynamicAgentSystem()
                result = await system.coordinate(initial_task=task, session_id=input_data.thread_id, show=False)
            message_id = str(uuid.uuid4())
            yield TextMessageStartEvent(
                type=EventType.TEXT_MESSAGE_START,
                message_id=message_id,
                role="assistant",
                timestamp=_now_ms(),
            )
            # Если workflow вернул None final_output — стримим пустой контент:
            # это честно отражает «workflow закончился без вывода», без подмены.
            text_payload = "" if result is None else str(result)
            for chunk in _chunk_text(text_payload):
                await asyncio.sleep(0)
                yield TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT,
                    message_id=message_id,
                    delta=chunk,
                    timestamp=_now_ms(),
                )
            yield TextMessageEndEvent(
                type=EventType.TEXT_MESSAGE_END,
                message_id=message_id,
                timestamp=_now_ms(),
            )
            # T3.3: для workflow-пути result = envelope (тот же формат, что и
            # service.result для service actions). Дополнительно публикуем
            # CustomEvent service.result, чтобы фронт мог обработать его
            # унифицированно с другими actions.
            if workflow_envelope is not None:
                redacted_envelope = redact_pii_in_payload(_redact_payload(workflow_envelope))
                yield CustomEvent(
                    type=EventType.CUSTOM,
                    name="workflow.result",
                    value=redacted_envelope,
                    timestamp=_now_ms(),
                )
                yield RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    thread_id=input_data.thread_id,
                    run_id=input_data.run_id,
                    result=redacted_envelope,
                    timestamp=_now_ms(),
                )
            else:
                yield RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    thread_id=input_data.thread_id,
                    run_id=input_data.run_id,
                    result=None,
                    timestamp=_now_ms(),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            code = "execution_error"
            # W1-review: error message → DSN+PII redact перед emit.
            message = redact_pii_in_payload(_redact_payload(str(exc)))
            if isinstance(exc, TextToSqlServiceActionRequiredError):
                # T3.4: стабильный код для клиента — переключиться на
                # service action `presets.text_to_sql.generate`.
                code = "text_to_sql_must_use_service_action"
            elif isinstance(exc, ForbiddenWorkflowNameError):
                code = "forbidden_workflow_name"
            elif isinstance(exc, ValueError) and message.startswith("workflow_name not found"):
                code = "workflow_not_found"
            yield RunErrorEvent(
                type=EventType.RUN_ERROR,
                message=message,
                code=code,
                timestamp=_now_ms(),
            )
