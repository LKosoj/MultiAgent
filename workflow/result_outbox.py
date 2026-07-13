"""Durable local outbox for terminal workflow results."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ._result_common import _canonical_payload, _validate_private_claim
from .result_identity import (
    validate_legacy_workflow_result_identity,
    validate_workflow_result_identity,
)
from .state_files import (
    acquire_sqlite_bundle,
    ensure_private_directory,
    release_sqlite_bundle,
)


logger = logging.getLogger(__name__)

WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION = 3


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _decode_canonical_payload_json(
    payload_json: str,
) -> tuple[dict[str, Any], str]:
    try:
        payload = json.loads(
            payload_json,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(payload, dict):
            raise ValueError("outbox payload JSON must be an object")
        canonical = _canonical_payload(payload)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("outbox payload JSON is invalid") from exc
    return payload, canonical


@dataclass(frozen=True)
class PendingWorkflowResult:
    enqueue_seq: int
    event_key: str
    run_id: str
    run_incarnation: str
    supervisor_id: Optional[str]
    attempt_generation: Optional[int]
    payload: dict[str, Any]
    created_at_ms: int


class _CorruptOutboxRow(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _OversizedOutboxRow(ValueError):
    """A structurally valid row exceeds the currently configured byte limit."""

    def __init__(self, enqueue_seq: Any, payload_bytes: Any) -> None:
        super().__init__("payload_bytes_exceeds_limit")
        self.enqueue_seq = enqueue_seq
        self.payload_bytes = payload_bytes


def _validate_stored_workflow_result_identity(
    *,
    event_key: Any,
    run_id: Any,
    run_incarnation: Any,
):
    try:
        return validate_workflow_result_identity(
            event_key=event_key,
            run_id=run_id,
            run_incarnation=run_incarnation,
        )
    except ValueError as v2_error:
        try:
            return validate_legacy_workflow_result_identity(
                event_key=event_key,
                run_id=run_id,
                run_incarnation=run_incarnation,
            )
        except ValueError:
            raise v2_error


class WorkflowResultOutbox:
    """Small independent SQLite queue for WORKFLOW_RESULT delivery."""

    def __init__(
        self,
        db_path: str,
        *,
        max_entries: int = 512,
        max_payload_bytes: int = 1_048_576,
        max_total_bytes: int = 16_777_216,
        max_dead_letters: int = 128,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if max_payload_bytes < 1 or max_total_bytes < 1:
            raise ValueError("outbox byte limits must be positive")
        if max_payload_bytes > max_total_bytes:
            raise ValueError("max_payload_bytes cannot exceed max_total_bytes")
        if max_dead_letters < 1:
            raise ValueError("max_dead_letters must be positive")
        self._db_path = Path(db_path)
        self._max_entries = max_entries
        self._max_payload_bytes = max_payload_bytes
        self._max_total_bytes = max_total_bytes
        self._max_dead_letters = max_dead_letters
        self._lock = threading.Lock()
        ensure_private_directory(
            self._db_path.parent,
            create=True,
            tighten_existing=False,
        )
        self._closed = False
        self._connection_closed = False
        self._lease_released = False
        self._bundle_lease = acquire_sqlite_bundle(self._db_path)
        self._conn: sqlite3.Connection | None = None
        try:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                timeout=30.0,
            )
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._migrate_schema()
            page_size = int(self._conn.execute("PRAGMA page_size").fetchone()[0])
            max_database_bytes = max_total_bytes + 2_097_152
            max_page_count = max(
                64,
                (max_database_bytes + page_size - 1) // page_size,
            )
            self._conn.execute(f"PRAGMA max_page_count={max_page_count}")
            self._conn.commit()
        except Exception as primary_error:
            cleanup_errors: list[Exception] = []
            if self._conn is not None:
                for _attempt in range(2):
                    try:
                        self._conn.close()
                    except Exception as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                    else:
                        self._connection_closed = True
                        break
            else:
                self._connection_closed = True
            if self._connection_closed:
                for _attempt in range(2):
                    try:
                        release_sqlite_bundle(self._bundle_lease)
                    except Exception as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                    else:
                        self._lease_released = True
                        break
            self._closed = self._connection_closed and self._lease_released
            if cleanup_errors:
                primary_error.add_note(
                    "WorkflowResultOutbox initialization cleanup encountered errors"
                )
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(
                        f"cleanup {type(cleanup_error).__name__}: {cleanup_error}"
                    )
                raise primary_error from cleanup_errors[-1]
            raise

    def _migrate_schema(self) -> None:
        """Create or migrate the queue under one cross-process lock."""
        try:
            self._conn.execute("BEGIN EXCLUSIVE")
            current_version = int(
                self._conn.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version > WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION:
                raise RuntimeError(
                    "workflow result outbox schema is newer than this application"
                )
            if current_version == WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION:
                self._validate_claimed_schema(
                    outbox_schema_validator=self._outbox_schema_is_v3,
                )
                self._conn.commit()
                return
            if current_version == 2:
                self._validate_claimed_schema(
                    outbox_schema_validator=self._outbox_schema_is_v2,
                )
                columns = {
                    str(row[1])
                    for row in self._conn.execute(
                        "PRAGMA table_info(workflow_result_outbox)"
                    )
                }
                self._migrate_legacy_outbox(columns)
            table_exists = self._conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'workflow_result_outbox'
                """
            ).fetchone()
            if table_exists is None:
                self._create_outbox_table("workflow_result_outbox")
            elif current_version != 2:
                columns = {
                    str(row[1])
                    for row in self._conn.execute(
                        "PRAGMA table_info(workflow_result_outbox)"
                    )
                }
                if "enqueue_seq" not in columns:
                    self._migrate_legacy_outbox(columns)
                elif not self._outbox_schema_is_v3():
                    self._migrate_legacy_outbox(columns)
            if not self._outbox_schema_is_v3():
                raise RuntimeError("workflow result outbox schema is malformed")
            self._ensure_index(
                table_name="workflow_result_outbox",
                index_name="idx_workflow_result_outbox_run",
                columns=("run_id", "run_incarnation", "enqueue_seq"),
                unique=False,
            )
            self._ensure_index(
                table_name="workflow_result_outbox",
                index_name="idx_workflow_result_outbox_incarnation",
                columns=("run_id", "run_incarnation"),
                unique=True,
            )
            dead_letter_exists = self._conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'workflow_result_outbox_dead_letter'
                """
            ).fetchone()
            if dead_letter_exists is None:
                self._conn.execute(
                    """
                    CREATE TABLE workflow_result_outbox_dead_letter (
                    dead_letter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    quarantined_at_ms INTEGER NOT NULL
                )
                    """
                )
            if not self._dead_letter_schema_is_v2():
                raise RuntimeError(
                    "workflow result outbox dead-letter schema is malformed"
                )
            self._ensure_index(
                table_name="workflow_result_outbox_dead_letter",
                index_name="idx_workflow_result_outbox_dlq_created",
                columns=("quarantined_at_ms", "dead_letter_id"),
                unique=False,
            )
            self._reject_outbox_triggers()
            self._conn.execute(
                f"PRAGMA user_version={WORKFLOW_RESULT_OUTBOX_SCHEMA_VERSION}"
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _create_outbox_table(self, table_name: str) -> None:
        if table_name not in {
            "workflow_result_outbox",
            "workflow_result_outbox_v3",
        }:
            raise ValueError("unsupported outbox table name")
        self._conn.execute(
            f"""
            CREATE TABLE {table_name} (
                enqueue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                run_incarnation TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL,
                supervisor_id TEXT,
                attempt_generation INTEGER,
                CHECK (
                    (supervisor_id IS NULL AND attempt_generation IS NULL)
                    OR (
                        supervisor_id IS NOT NULL
                        AND length(supervisor_id) BETWEEN 1 AND 512
                        AND trim(supervisor_id) = supervisor_id
                        AND attempt_generation > 0
                    )
                )
            )
            """
        )

    def _validate_claimed_schema(
        self,
        *,
        outbox_schema_validator,
    ) -> None:
        if not outbox_schema_validator():
            raise RuntimeError("workflow result outbox schema is malformed")
        if not self._dead_letter_schema_is_v2():
            raise RuntimeError(
                "workflow result outbox dead-letter schema is malformed"
            )
        self._ensure_index(
            table_name="workflow_result_outbox",
            index_name="idx_workflow_result_outbox_run",
            columns=("run_id", "run_incarnation", "enqueue_seq"),
            unique=False,
            create_missing=False,
        )
        self._ensure_index(
            table_name="workflow_result_outbox",
            index_name="idx_workflow_result_outbox_incarnation",
            columns=("run_id", "run_incarnation"),
            unique=True,
            create_missing=False,
        )
        self._ensure_index(
            table_name="workflow_result_outbox_dead_letter",
            index_name="idx_workflow_result_outbox_dlq_created",
            columns=("quarantined_at_ms", "dead_letter_id"),
            unique=False,
            create_missing=False,
        )
        self._reject_outbox_triggers()

    def _outbox_schema_is_v3(self) -> bool:
        expected_types = {
            "enqueue_seq": "INTEGER",
            "event_key": "TEXT",
            "run_id": "TEXT",
            "run_incarnation": "TEXT",
            "payload": "TEXT",
            "payload_bytes": "INTEGER",
            "created_at_ms": "INTEGER",
            "supervisor_id": "TEXT",
            "attempt_generation": "INTEGER",
        }
        if not self._table_schema_matches(
            "workflow_result_outbox",
            expected_types=expected_types,
            primary_key="enqueue_seq",
            nullable={"supervisor_id", "attempt_generation"},
        ):
            return False
        return self._has_binary_event_key_uniqueness()

    def _outbox_schema_is_v2(self) -> bool:
        expected_types = {
            "enqueue_seq": "INTEGER",
            "event_key": "TEXT",
            "run_id": "TEXT",
            "run_incarnation": "TEXT",
            "payload": "TEXT",
            "payload_bytes": "INTEGER",
            "created_at_ms": "INTEGER",
        }
        if not self._table_schema_matches(
            "workflow_result_outbox",
            expected_types=expected_types,
            primary_key="enqueue_seq",
        ):
            return False
        return self._has_binary_event_key_uniqueness()

    def _has_binary_event_key_uniqueness(self) -> bool:
        for index in self._conn.execute(
            "PRAGMA index_list(workflow_result_outbox)"
        ):
            if not int(index[2]) or int(index[4]):
                continue
            if self._index_key_signature(str(index[1])) == (
                ("event_key", "BINARY", 0),
            ):
                return True
        return False

    def _dead_letter_schema_is_v2(self) -> bool:
        return self._table_schema_matches(
            "workflow_result_outbox_dead_letter",
            expected_types={
                "dead_letter_id": "INTEGER",
                "event_key": "TEXT",
                "reason_code": "TEXT",
                "metadata_json": "TEXT",
                "quarantined_at_ms": "INTEGER",
            },
            primary_key="dead_letter_id",
        )

    def _table_schema_matches(
        self,
        table_name: str,
        *,
        expected_types: dict[str, str],
        primary_key: str,
        nullable: set[str] | None = None,
    ) -> bool:
        """Check column names/types/nullability and the primary key.

        This intentionally does not compare the table's raw CREATE TABLE SQL
        (for example CHECK constraint text): SQLite enforces those
        constraints regardless of what this check re-verifies, so a full
        text comparison would only be extra paranoia, not extra safety.
        """
        nullable = nullable or set()
        table_info = list(
            self._conn.execute(
                "SELECT * FROM pragma_table_info(?) ORDER BY cid",
                (table_name,),
            )
        )
        columns = {str(row[1]): row for row in table_info}
        if set(columns) != set(expected_types):
            return False
        if any(
            str(columns[name][2]).upper() != expected_type
            for name, expected_type in expected_types.items()
        ):
            return False
        if int(columns[primary_key][5]) != 1 or sum(
            int(row[5]) > 0 for row in table_info
        ) != 1:
            return False
        if any(
            int(columns[name][3]) != (0 if name in nullable else 1)
            for name in expected_types
            if name != primary_key
        ):
            return False
        return True

    def _index_key_signature(
        self,
        index_name: str,
    ) -> tuple[tuple[str, str, int], ...]:
        rows = self._conn.execute(
            "SELECT * FROM pragma_index_xinfo(?) ORDER BY seqno",
            (index_name,),
        )
        return tuple(
            (str(row[2]), str(row[4]).upper(), int(row[3]))
            for row in rows
            if int(row[5])
        )

    def _ensure_index(
        self,
        *,
        table_name: str,
        index_name: str,
        columns: tuple[str, ...],
        unique: bool,
        create_missing: bool = True,
    ) -> None:
        existing = {
            str(row[1]): row
            for row in self._conn.execute(f"PRAGMA index_list({table_name})")
        }.get(index_name)
        expected_signature = tuple(
            (column, "BINARY", 0) for column in columns
        )
        if existing is not None:
            if (
                bool(existing[2]) != unique
                or bool(existing[4])
                or self._index_key_signature(index_name) != expected_signature
            ):
                raise RuntimeError(
                    f"workflow result outbox index is malformed: {index_name}"
                )
            return
        if not create_missing:
            raise RuntimeError(
                f"workflow result outbox index is missing: {index_name}"
            )
        unique_sql = "UNIQUE " if unique else ""
        self._conn.execute(
            f"CREATE {unique_sql}INDEX {index_name} "
            f"ON {table_name}({', '.join(columns)})"
        )
        if self._index_key_signature(index_name) != expected_signature:
            raise RuntimeError(
                f"workflow result outbox index is malformed: {index_name}"
            )

    def _reject_outbox_triggers(self) -> None:
        """Reject any trigger attached to the outbox or dead-letter tables.

        The outbox no longer enumerates every sqlite_master object (extra
        tables/views/indexes sharing the file are none of its business); a
        trigger is the one object kind that could silently rewrite rows out
        from under the claim/fencing protocol, so it stays a hard failure.
        """
        tables = (
            "workflow_result_outbox",
            "workflow_result_outbox_dead_letter",
        )
        triggers = list(
            self._conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND tbl_name IN (?, ?)
                """,
                tables,
            )
        )
        if triggers:
            raise RuntimeError("workflow result outbox trigger is not allowed")

    def _quarantine_duplicate_incarnations(self, columns: set[str]) -> None:
        """Drop superseded rows before a legacy outbox is copied to v3.

        Schemas older than v3 (and even v2, which lacked a live application
        invariant to prevent it) could accumulate more than one row for the
        same run_id, for example when a supervisor restart minted a new
        incarnation while an older one's result was still queued. Only the
        row with the highest enqueue_seq (or rowid, for the pre-enqueue_seq
        v1 shape) survives migration; the rest move to dead_letter so the
        v3 (run_id, run_incarnation) uniqueness invariant always holds.

        Runs as flat statements inside the caller's existing EXCLUSIVE
        transaction; it does not open one of its own.
        """
        order_column = "enqueue_seq" if "enqueue_seq" in columns else "rowid"
        losers = list(
            self._conn.execute(
                f"""
                SELECT event_key, run_id, run_incarnation, payload,
                       created_at_ms
                FROM workflow_result_outbox
                WHERE {order_column} NOT IN (
                    SELECT MAX({order_column})
                    FROM workflow_result_outbox
                    GROUP BY run_id
                )
                ORDER BY {order_column} ASC
                """
            )
        )
        if not losers:
            return
        dead_letter_exists = self._conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'workflow_result_outbox_dead_letter'
            """
        ).fetchone()
        if dead_letter_exists is None:
            self._conn.execute(
                """
                CREATE TABLE workflow_result_outbox_dead_letter (
                    dead_letter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    quarantined_at_ms INTEGER NOT NULL
                )
                """
            )
        quarantined_at_ms = int(time.time() * 1000)
        for event_key, run_id, run_incarnation, payload, created_at_ms in losers:
            metadata = {
                "event_key": self._bounded_metadata_text(event_key),
                "run_id": self._bounded_metadata_text(run_id),
                "run_incarnation": self._bounded_metadata_text(run_incarnation),
                "payload_bytes": len(str(payload).encode("utf-8")),
                "created_at_ms": (
                    int(created_at_ms) if isinstance(created_at_ms, int) else None
                ),
            }
            metadata_json = json.dumps(
                metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self._conn.execute(
                """
                INSERT INTO workflow_result_outbox_dead_letter
                    (event_key, reason_code, metadata_json, quarantined_at_ms)
                VALUES (?, ?, ?, ?)
                """,
                (
                    self._bounded_metadata_text(event_key),
                    "legacy_duplicate_incarnation",
                    metadata_json,
                    quarantined_at_ms,
                ),
            )
        self._conn.execute(
            f"""
            DELETE FROM workflow_result_outbox
            WHERE {order_column} NOT IN (
                SELECT MAX({order_column})
                FROM workflow_result_outbox
                GROUP BY run_id
            )
            """
        )

    def _migrate_legacy_outbox(self, columns: set[str]) -> None:
        # The v2 path already runs _validate_claimed_schema (which calls
        # _reject_outbox_triggers) before migrating, but the v1 path (no
        # enqueue_seq column) invokes this method directly. Without this
        # check, the DROP TABLE below would silently destroy any trigger
        # attached to the legacy table instead of hard-failing per
        # _reject_outbox_triggers' contract.
        self._reject_outbox_triggers()
        required_columns = {
            "event_key",
            "run_id",
            "run_incarnation",
            "payload",
            "created_at_ms",
        }
        if not required_columns <= columns:
            raise RuntimeError("legacy workflow result outbox schema is incomplete")
        has_supervisor = "supervisor_id" in columns
        has_generation = "attempt_generation" in columns
        if has_supervisor != has_generation:
            raise RuntimeError(
                "legacy workflow result outbox claim columns are incomplete"
            )
        self._quarantine_duplicate_incarnations(columns)
        sequence_exists = self._conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'sqlite_sequence'
            """
        ).fetchone()
        sequence_row = (
            self._conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?",
                ("workflow_result_outbox",),
            ).fetchone()
            if sequence_exists is not None
            else None
        )
        previous_high_water = 0 if sequence_row is None else int(sequence_row[0])
        self._conn.execute("DROP TABLE IF EXISTS workflow_result_outbox_v3")
        self._create_outbox_table("workflow_result_outbox_v3")
        payload_bytes = (
            "payload_bytes"
            if "payload_bytes" in columns
            else "length(CAST(payload AS BLOB))"
        )
        supervisor_id = "supervisor_id" if has_supervisor else "NULL"
        attempt_generation = (
            "attempt_generation" if has_generation else "NULL"
        )
        if "enqueue_seq" in columns:
            self._conn.execute(
                f"""
                INSERT INTO workflow_result_outbox_v3
                    (enqueue_seq, event_key, run_id, run_incarnation, payload,
                     payload_bytes, created_at_ms, supervisor_id,
                     attempt_generation)
                SELECT enqueue_seq, event_key, run_id, run_incarnation, payload,
                       {payload_bytes}, created_at_ms, {supervisor_id},
                       {attempt_generation}
                FROM workflow_result_outbox
                ORDER BY enqueue_seq ASC
                """
            )
        else:
            self._conn.execute(
                f"""
                INSERT INTO workflow_result_outbox_v3
                    (event_key, run_id, run_incarnation, payload,
                     payload_bytes, created_at_ms, supervisor_id,
                     attempt_generation)
                SELECT event_key, run_id, run_incarnation, payload,
                       {payload_bytes}, created_at_ms, {supervisor_id},
                       {attempt_generation}
                FROM workflow_result_outbox
                ORDER BY rowid ASC
                """
            )
        self._conn.execute("DROP TABLE workflow_result_outbox")
        self._conn.execute(
            "ALTER TABLE workflow_result_outbox_v3 "
            "RENAME TO workflow_result_outbox"
        )
        migrated_high_water = int(
            self._conn.execute(
                "SELECT COALESCE(MAX(enqueue_seq), 0) "
                "FROM workflow_result_outbox"
            ).fetchone()[0]
        )
        preserved_high_water = max(previous_high_water, migrated_high_water)
        updated = self._conn.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
            (preserved_high_water, "workflow_result_outbox"),
        )
        if updated.rowcount == 0:
            self._conn.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                ("workflow_result_outbox", preserved_high_water),
            )

    def enqueue(
        self,
        payload: dict[str, Any],
        *,
        supervisor_id: Optional[str] = None,
        attempt_generation: Optional[int] = None,
    ) -> str:
        if not isinstance(payload, dict):
            raise TypeError("outbox payload must be an object")
        supervisor_id, attempt_generation = _validate_private_claim(
            supervisor_id,
            attempt_generation,
        )
        identity = validate_workflow_result_identity(
            event_key=payload.get("event_key"),
            run_id=payload.get("run_id"),
            run_incarnation=payload.get("run_incarnation"),
        )
        event_key = identity.event_key
        run_id = identity.run_id
        run_incarnation = identity.run_incarnation
        payload_json = _canonical_payload(payload)
        payload_bytes = len(payload_json.encode("utf-8"))
        if payload_bytes > self._max_payload_bytes:
            raise ValueError("outbox payload exceeds max_payload_bytes")
        created_at_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                event_key_owner = self._conn.execute(
                    """
                    SELECT enqueue_seq, event_key, run_id, run_incarnation,
                           supervisor_id, attempt_generation, payload,
                           payload_bytes, created_at_ms
                    FROM workflow_result_outbox
                    WHERE event_key = ?
                    """,
                    (event_key,),
                ).fetchone()
                if event_key_owner is not None:
                    try:
                        existing_entry = self._decode_row(event_key_owner)
                        _, existing_payload_json = (
                            _decode_canonical_payload_json(
                                str(event_key_owner[6])
                            )
                        )
                    except (_CorruptOutboxRow, _OversizedOutboxRow, ValueError) as exc:
                        raise ValueError(
                            "outbox event_key belongs to a corrupt stored row"
                        ) from exc
                    if (
                        existing_entry.run_id != run_id
                        or existing_entry.run_incarnation != run_incarnation
                    ):
                        raise ValueError(
                            "outbox event_key belongs to another run incarnation"
                        )
                    if existing_payload_json != payload_json:
                        raise ValueError(
                            "outbox event_key belongs to a different payload"
                        )
                    if (
                        existing_entry.supervisor_id != supervisor_id
                        or existing_entry.attempt_generation
                        != attempt_generation
                    ):
                        raise ValueError(
                            "outbox event_key belongs to a different claim"
                        )
                existing = self._conn.execute(
                    """
                    SELECT event_key
                    FROM workflow_result_outbox
                    WHERE run_id = ? AND run_incarnation = ?
                    """,
                    (run_id, run_incarnation),
                ).fetchone()
                if existing is not None and str(existing[0]) != event_key:
                    raise ValueError(
                        "outbox run incarnation belongs to a different event_key"
                    )
                if event_key_owner is None:
                    count, total_bytes = self._conn.execute(
                        """
                        SELECT COUNT(*),
                               COALESCE(SUM(length(CAST(payload AS BLOB))), 0)
                        FROM workflow_result_outbox
                        """
                    ).fetchone()
                    if int(count) >= self._max_entries:
                        raise OverflowError("workflow result outbox capacity exceeded")
                    projected_bytes = int(total_bytes) + payload_bytes
                    if projected_bytes > self._max_total_bytes:
                        raise OverflowError(
                            "workflow result outbox byte capacity exceeded"
                        )
                    self._conn.execute(
                        """
                        INSERT INTO workflow_result_outbox
                            (event_key, run_id, run_incarnation, payload,
                             payload_bytes, created_at_ms, supervisor_id,
                             attempt_generation)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_key,
                            run_id,
                            run_incarnation,
                            payload_json,
                            payload_bytes,
                            created_at_ms,
                            supervisor_id,
                            attempt_generation,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return event_key

    def list_pending(self, *, limit: Optional[int] = None) -> list[PendingWorkflowResult]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        sql = (
            "SELECT enqueue_seq, event_key, run_id, run_incarnation, "
            "supervisor_id, attempt_generation, payload, payload_bytes, "
            "created_at_ms "
            "FROM workflow_result_outbox "
            "ORDER BY enqueue_seq ASC"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = list(self._conn.execute(sql, params).fetchall())
        return self._decode_rows(rows)

    def pending_run_ids(self) -> frozenset[str]:
        """Return every run whose terminal result is still queued."""
        return frozenset(entry.run_id for entry in self.list_pending())

    def pending_for_claim(
        self,
        run_id: str,
        *,
        supervisor_id: Optional[str],
        attempt_generation: Optional[int],
    ) -> bool:
        supervisor_id, attempt_generation = _validate_private_claim(
            supervisor_id,
            attempt_generation,
        )
        with self._lock:
            rows = list(
                self._conn.execute(
                    """
                    SELECT enqueue_seq, event_key, run_id, run_incarnation,
                           supervisor_id, attempt_generation, payload,
                           payload_bytes, created_at_ms
                    FROM workflow_result_outbox
                    WHERE run_id = ? AND supervisor_id IS ?
                      AND attempt_generation IS ?
                    ORDER BY enqueue_seq ASC
                    """,
                    (run_id, supervisor_id, attempt_generation),
                )
            )
        return bool(self._decode_rows(rows))

    @staticmethod
    def _bounded_metadata_text(value: Any, max_bytes: int = 256) -> str:
        text = str(value)
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    def _decode_row(self, row: tuple[Any, ...]) -> PendingWorkflowResult:
        (
            enqueue_seq,
            event_key,
            run_id,
            run_incarnation,
            supervisor_id,
            attempt_generation,
            payload_json,
            payload_bytes,
            created_at_ms,
        ) = row
        if not isinstance(enqueue_seq, int) or enqueue_seq < 1:
            raise _CorruptOutboxRow("enqueue_seq_invalid")
        try:
            row_identity = _validate_stored_workflow_result_identity(
                event_key=event_key,
                run_id=run_id,
                run_incarnation=run_incarnation,
            )
        except ValueError as exc:
            raise _CorruptOutboxRow("row_identity_invalid") from exc
        if not isinstance(payload_json, str) or not payload_json:
            raise _CorruptOutboxRow("row_identity_invalid")
        actual_payload_bytes = len(payload_json.encode("utf-8"))
        if (
            not isinstance(payload_bytes, int)
            or payload_bytes != actual_payload_bytes
        ):
            raise _CorruptOutboxRow("payload_size_invalid")
        if payload_bytes > self._max_payload_bytes:
            raise _OversizedOutboxRow(enqueue_seq, payload_bytes)
        if not isinstance(created_at_ms, int):
            raise _CorruptOutboxRow("created_at_invalid")
        try:
            supervisor_id, attempt_generation = _validate_private_claim(
                supervisor_id,
                attempt_generation,
            )
        except ValueError as exc:
            raise _CorruptOutboxRow("claim_identity_invalid") from exc
        try:
            payload, _ = _decode_canonical_payload_json(payload_json)
        except ValueError as exc:
            raise _CorruptOutboxRow("payload_json_invalid") from exc
        try:
            payload_identity = _validate_stored_workflow_result_identity(
                event_key=payload.get("event_key"),
                run_id=payload.get("run_id"),
                run_incarnation=payload.get("run_incarnation"),
            )
        except ValueError as exc:
            raise _CorruptOutboxRow("payload_identity_mismatch") from exc
        if payload_identity != row_identity:
            raise _CorruptOutboxRow("payload_identity_mismatch")
        return PendingWorkflowResult(
            enqueue_seq=enqueue_seq,
            event_key=event_key,
            run_id=run_id,
            run_incarnation=run_incarnation,
            supervisor_id=supervisor_id,
            attempt_generation=attempt_generation,
            payload=payload,
            created_at_ms=int(created_at_ms),
        )

    def _decode_rows(self, rows: list[tuple[Any, ...]]) -> list[PendingWorkflowResult]:
        pending = []
        for row in rows:
            try:
                pending.append(self._decode_row(row))
            except _CorruptOutboxRow as exc:
                self._quarantine_row(row, reason_code=exc.reason_code)
            except _OversizedOutboxRow as exc:
                # row layout: (enqueue_seq, event_key, run_id, run_incarnation,
                # supervisor_id, attempt_generation, payload, payload_bytes,
                # created_at_ms) -- see list_pending/pending_for_claim/
                # latest_payload, the only callers of _decode_rows.
                supervisor_id = row[4]
                if supervisor_id is None:
                    # Unclaimed: the payload is structurally valid, just past
                    # the currently configured byte limit (e.g. after a
                    # config change). Quarantine it like a corrupt row so the
                    # event_key is freed for a new enqueue() instead of
                    # staying a permanent poison pill.
                    self._quarantine_row(row, reason_code="oversized_payload")
                    logger.warning(
                        "outbox row enqueue_seq=%s exceeds payload size "
                        "limit (%s bytes); quarantined to dead_letter",
                        exc.enqueue_seq,
                        exc.payload_bytes,
                    )
                else:
                    # Claimed: a supervisor may rely on pending_for_claim()
                    # to detect that its own attempt already produced a
                    # terminal result before treating a worker exit as a
                    # real failure. Quarantining here would erase that
                    # evidence (dead_letter only stores metadata, not the
                    # payload itself), which could make the supervisor
                    # believe no result was ever produced and write a
                    # conflicting/duplicate outcome. Leave the row in place
                    # and only skip it with a warning; it stays a poison
                    # pill for the owning claim until max_payload_bytes is
                    # raised again.
                    logger.warning(
                        "outbox row enqueue_seq=%s exceeds payload size "
                        "limit (%s bytes); claimed by a supervisor, "
                        "skipping without quarantine to preserve the "
                        "claim/fencing protocol",
                        exc.enqueue_seq,
                        exc.payload_bytes,
                    )
        return pending

    def _quarantine_row(
        self,
        row: tuple[Any, ...],
        *,
        reason_code: str,
    ) -> None:
        (
            enqueue_seq,
            event_key,
            run_id,
            run_incarnation,
            supervisor_id,
            attempt_generation,
            payload,
            payload_bytes,
            created_at_ms,
        ) = row
        metadata = {
            "enqueue_seq": (
                int(enqueue_seq) if isinstance(enqueue_seq, int) else None
            ),
            "event_key": self._bounded_metadata_text(event_key),
            "run_id": self._bounded_metadata_text(run_id),
            "run_incarnation": self._bounded_metadata_text(run_incarnation),
            "supervisor_id": (
                self._bounded_metadata_text(supervisor_id)
                if supervisor_id is not None
                else None
            ),
            "attempt_generation": (
                int(attempt_generation)
                if isinstance(attempt_generation, int)
                and not isinstance(attempt_generation, bool)
                else None
            ),
            "payload_bytes": (
                int(payload_bytes)
                if isinstance(payload_bytes, int) and payload_bytes >= 0
                else len(str(payload).encode("utf-8"))
            ),
            "created_at_ms": (
                int(created_at_ms) if isinstance(created_at_ms, int) else None
            ),
        }
        metadata_json = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                deleted = self._conn.execute(
                    """
                    DELETE FROM workflow_result_outbox
                    WHERE enqueue_seq = ? AND event_key = ? AND payload = ?
                      AND supervisor_id IS ? AND attempt_generation IS ?
                    """,
                    (
                        enqueue_seq,
                        event_key,
                        payload,
                        supervisor_id,
                        attempt_generation,
                    ),
                )
                if deleted.rowcount:
                    self._conn.execute(
                        """
                        INSERT INTO workflow_result_outbox_dead_letter
                            (event_key, reason_code, metadata_json, quarantined_at_ms)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            self._bounded_metadata_text(event_key),
                            reason_code,
                            metadata_json,
                            int(time.time() * 1000),
                        ),
                    )
                    self._conn.execute(
                        """
                        DELETE FROM workflow_result_outbox_dead_letter
                        WHERE dead_letter_id IN (
                            SELECT dead_letter_id
                            FROM workflow_result_outbox_dead_letter
                            ORDER BY quarantined_at_ms DESC, dead_letter_id DESC
                            LIMIT -1 OFFSET ?
                        )
                        """,
                        (self._max_dead_letters,),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def latest_payload(
        self,
        run_id: str,
        run_incarnation: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        sql = (
            "SELECT enqueue_seq, event_key, run_id, run_incarnation, "
            "supervisor_id, attempt_generation, payload, payload_bytes, "
            "created_at_ms FROM workflow_result_outbox "
            "WHERE run_id = ?"
        )
        params: tuple[Any, ...] = (run_id,)
        if run_incarnation is not None:
            sql += " AND run_incarnation = ?"
            params = (run_id, run_incarnation)
        sql += " ORDER BY enqueue_seq DESC"
        with self._lock:
            rows = list(self._conn.execute(sql, params).fetchall())
        pending = self._decode_rows(rows)
        return pending[0].payload if pending else None

    def delete(
        self,
        event_key: str,
        *,
        enqueue_seq: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
        supervisor_id: Optional[str] = None,
        attempt_generation: Optional[int] = None,
    ) -> bool:
        if (enqueue_seq is None) != (payload is None):
            raise ValueError(
                "enqueue_seq and payload must be provided together"
            )
        if enqueue_seq is not None:
            if not isinstance(enqueue_seq, int) or enqueue_seq < 1:
                raise ValueError("enqueue_seq must be a positive integer")
            if not isinstance(payload, dict):
                raise TypeError("payload must be an object")
            payload_json = _canonical_payload(payload)
            supervisor_id, attempt_generation = _validate_private_claim(
                supervisor_id,
                attempt_generation,
            )
        elif supervisor_id is not None or attempt_generation is not None:
            raise ValueError(
                "claim metadata requires enqueue_seq and payload"
            )
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                deleted = 0
                if enqueue_seq is None:
                    cur = self._conn.execute(
                        """
                        DELETE FROM workflow_result_outbox
                        WHERE event_key = ? AND supervisor_id IS NULL
                          AND attempt_generation IS NULL
                        """,
                        (event_key,),
                    )
                    deleted = cur.rowcount
                else:
                    stored = self._conn.execute(
                        """
                        SELECT payload, supervisor_id, attempt_generation
                        FROM workflow_result_outbox
                        WHERE enqueue_seq = ? AND event_key = ?
                        """,
                        (enqueue_seq, event_key),
                    ).fetchone()
                    stored_payload = None
                    if (
                        stored is not None
                        and isinstance(stored[0], str)
                        and stored[1] == supervisor_id
                        and stored[2] == attempt_generation
                    ):
                        try:
                            _, stored_canonical = (
                                _decode_canonical_payload_json(stored[0])
                            )
                            if stored_canonical == payload_json:
                                stored_payload = stored[0]
                        except ValueError:
                            pass
                    if stored_payload is not None:
                        cur = self._conn.execute(
                            """
                            DELETE FROM workflow_result_outbox
                            WHERE enqueue_seq = ? AND event_key = ? AND payload = ?
                              AND supervisor_id IS ?
                              AND attempt_generation IS ?
                            """,
                            (
                                enqueue_seq,
                                event_key,
                                stored_payload,
                                supervisor_id,
                                attempt_generation,
                            ),
                        )
                        deleted = cur.rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return deleted > 0

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM workflow_result_outbox"
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._connection_closed:
                assert self._conn is not None
                self._conn.close()
                self._connection_closed = True
            if not self._lease_released:
                release_sqlite_bundle(self._bundle_lease)
                self._lease_released = True
            self._closed = True
