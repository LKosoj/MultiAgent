"""SQLite-backed event store for AG-UI replay support."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    run_id: str
    event_type: str
    payload: dict[str, Any]
    created_at_ms: int


class EventStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        # WAL — безопасно при нескольких читателях/одном писателе и
        # уменьшает блокировки при параллельных replay-запросах.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            # Некоторые ФС (например, сетевые) не поддерживают WAL — оставляем дефолт.
            pass
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
        # UNIQUE гарантирует отсутствие дубликатов seq внутри run_id — защита на случай
        # одновременной записи через разные процессы, где in-process lock не помогает.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_agui_events_run_seq ON agui_events(run_id, seq)"
        )
        self._conn.commit()

    def append(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        created_at_ms = int(time.time() * 1000)
        payload_json = json.dumps(payload)
        with self._lock:
            # Атомарный INSERT с вычислением seq внутри SQL: SELECT+INSERT в
            # одной транзакции, без окна гонки между чтением max(seq) и записью.
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    """
                    INSERT INTO agui_events (run_id, seq, event_type, payload, created_at_ms)
                    SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?
                    FROM agui_events WHERE run_id = ?
                    """,
                    (run_id, event_type, payload_json, created_at_ms, run_id),
                )
                seq_row = self._conn.execute(
                    "SELECT seq FROM agui_events WHERE id = ?",
                    (cur.lastrowid,),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return int(seq_row[0])

    def list_after(self, run_id: str, after_seq: int = 0) -> Iterable[StoredEvent]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT seq, run_id, event_type, payload, created_at_ms
                FROM agui_events
                WHERE run_id = ? AND seq > ?
                ORDER BY seq ASC
                """,
                (run_id, after_seq),
            )
            rows = list(cur.fetchall())
        for seq, run_id_value, event_type, payload, created_at_ms in rows:
            yield StoredEvent(
                seq=seq,
                run_id=run_id_value,
                event_type=event_type,
                payload=json.loads(payload),
                created_at_ms=created_at_ms,
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
            self._conn.close()
