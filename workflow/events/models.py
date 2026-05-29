"""Модели событий для event-driven расширения workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class EventPriority(Enum):
    """Приоритет обработки события."""

    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Event:
    """Базовое событие в системе."""

    event_id: str
    event_type: str
    source: str
    timestamp: datetime = field(default_factory=_now_utc)
    priority: EventPriority = EventPriority.NORMAL
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None
    aggregate_id: Optional[str] = None
    sequence_number: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Сериализация события в словарь."""

        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
            "priority": self.priority.value,
            "payload": self.payload,
            "metadata": self.metadata,
            "correlation_id": self.correlation_id,
            "aggregate_id": self.aggregate_id,
            "sequence_number": self.sequence_number,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        """Создание события из словаря."""

        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            source=data["source"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            priority=EventPriority(data.get("priority", EventPriority.NORMAL.value)),
            payload=data.get("payload", {}),
            metadata=data.get("metadata", {}),
            correlation_id=data.get("correlation_id"),
            aggregate_id=data.get("aggregate_id"),
            sequence_number=data.get("sequence_number"),
        )


@dataclass(slots=True)
class EventPattern:
    """Паттерн для подписки на события."""

    pattern: str
    priority_filter: Optional[EventPriority] = None
    source_filter: Optional[str] = None

    def matches(self, event: Event) -> bool:
        """Проверка, подходит ли событие под заданный паттерн."""

        import fnmatch

        if not fnmatch.fnmatch(event.event_type, self.pattern):
            return False

        if self.priority_filter and event.priority != self.priority_filter:
            return False

        if self.source_filter and event.source != self.source_filter:
            return False

        return True


