"""Простое хранилище событий на SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional

from .models import Event, EventPriority

logger = logging.getLogger(__name__)


class EventStore:
    """Сохранение и запросы событий (event sourcing light)."""

    def __init__(self, db_path: str = "workflow_state.db") -> None:
        self._db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    correlation_id TEXT,
                    aggregate_id TEXT,
                    sequence_number INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_type
                ON workflow_events(event_type)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_source
                ON workflow_events(source)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_aggregate
                ON workflow_events(aggregate_id, sequence_number)
                """
            )

    def _write_event(self, event: Event) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO workflow_events (
                    event_id, event_type, source, timestamp, priority,
                    payload, metadata, correlation_id, aggregate_id, sequence_number
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.source,
                    event.timestamp.isoformat(),
                    event.priority.value,
                    json.dumps(event.payload or {}),
                    json.dumps(event.metadata or {}),
                    event.correlation_id,
                    event.aggregate_id,
                    event.sequence_number,
                ),
            )

        logger.debug("Event %s stored", event.event_id)

    async def append(self, event: Event) -> None:
        """Сохранить событие (async совместимость)."""

        self._write_event(event)

    def append_sync(self, event: Event) -> None:
        """Сохранить событие из синхронного контекста."""

        self._write_event(event)

    async def get_by_aggregate(self, aggregate_id: str) -> List[Event]:
        """Получить события агрегата в порядке sequence_number."""

        query = (
            "SELECT event_id, event_type, source, timestamp, priority, payload, metadata,"
            " correlation_id, aggregate_id, sequence_number"
            " FROM workflow_events WHERE aggregate_id = ?"
            " ORDER BY sequence_number ASC"
        )

        return await self._fetch(query, (aggregate_id,))

    async def query(
        self,
        *,
        event_type: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 100,
    ) -> List[Event]:
        """Запрос событий с фильтрами."""

        query = (
            "SELECT event_id, event_type, source, timestamp, priority, payload, metadata,"
            " correlation_id, aggregate_id, sequence_number"
            " FROM workflow_events WHERE 1=1"
        )
        params: List[object] = []

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        if source:
            query += " AND source = ?"
            params.append(source)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        return await self._fetch(query, tuple(params))

    async def _fetch(self, query: str, params: Iterable[object]) -> List[Event]:
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

        events = []
        for row in rows:
            event = Event(
                event_id=row[0],
                event_type=row[1],
                source=row[2],
                timestamp=datetime_from_iso(row[3]),
                priority=EventPriority(row[4]),
                payload=json.loads(row[5] or "{}"),
                metadata=json.loads(row[6] or "{}"),
                correlation_id=row[7],
                aggregate_id=row[8],
                sequence_number=row[9],
            )
            events.append(event)

        return events


def datetime_from_iso(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


