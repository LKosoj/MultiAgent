"""Инструменты для работы с event-driven workflow."""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from workflow.event_driven.registry import get_event_bus, get_event_store
from workflow.events import Event, EventPriority


def _resolve_priority(value: Any) -> EventPriority:
    if isinstance(value, EventPriority):
        return value

    if isinstance(value, str):
        key = value.strip().upper()
        try:
            return EventPriority[key]
        except KeyError as exc:
            raise ValueError(f"Unknown priority '{value}'. Use one of: {[p.name.lower() for p in EventPriority]}") from exc

    if isinstance(value, int):
        try:
            return EventPriority(value)
        except ValueError as exc:
            raise ValueError(f"Unknown priority level '{value}'.") from exc

    raise ValueError("Priority must be str, int or EventPriority")


def emit_event(
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    priority: Any = EventPriority.NORMAL,
    metadata: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    aggregate_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Публикует событие в EventBus и сохраняет его в EventStore.

    Args:
        event_type: Тип события (например, "workflow.completed").
        payload: Полезная нагрузка события.
        priority: Приоритет обработки события (str|int|EventPriority).
        metadata: Дополнительные метаданные события.
        correlation_id: Корреляционный идентификатор цепочки событий.
        aggregate_id: Идентификатор агрегата для event sourcing.
        session_id: Идентификатор сессии (передается автоматически менеджером инструментов).

    Returns:
        Dict с информацией о созданном событии.
    """

    if not event_type or not isinstance(event_type, str):
        raise ValueError("event_type must be a non-empty string")

    bus = get_event_bus()
    if bus is None:
        raise RuntimeError("Event bus is not available. Ensure EventDrivenWorkflowEngine.start_event_layer() was called")

    store = get_event_store()

    priority_enum = _resolve_priority(priority)
    event_payload = payload.copy() if payload else {}
    event_metadata = metadata.copy() if metadata else {}

    event = Event(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        source=session_id or "emit_event_tool",
        priority=priority_enum,
        payload=event_payload,
        metadata=event_metadata,
        correlation_id=correlation_id,
        aggregate_id=aggregate_id,
    )

    bus.publish_sync(event)

    if store is not None:
        store.append_sync(event)

    return {
        "status": "published",
        "event_id": event.event_id,
        "event_type": event.event_type,
        "priority": event.priority.name.lower(),
    }


