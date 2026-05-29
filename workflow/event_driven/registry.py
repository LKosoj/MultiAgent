"""Глобальная регистрация event-driven компонентов."""

from __future__ import annotations

from typing import Optional

from ..events import EventBus, EventStore

_event_bus: Optional[EventBus] = None
_event_store: Optional[EventStore] = None


def set_event_bus(bus: EventBus) -> None:
    global _event_bus
    _event_bus = bus


def get_event_bus() -> Optional[EventBus]:
    return _event_bus


def set_event_store(store: EventStore) -> None:
    global _event_store
    _event_store = store


def get_event_store() -> Optional[EventStore]:
    return _event_store


def clear_event_resources() -> None:
    global _event_bus, _event_store
    _event_bus = None
    _event_store = None


