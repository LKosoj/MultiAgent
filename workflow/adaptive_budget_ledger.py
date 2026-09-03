"""Dedicated append-only SQLite ledger for adaptive research budgets."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from typing import Iterator
import uuid

from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.policy import (
    MAX_WALL_CLOCK_SECONDS,
    BudgetAdmissionError,
    BudgetConflictError,
    BudgetLedgerRecord,
    BudgetReconciliation,
    BudgetReservation,
    ResearchPolicyError,
    reconcile_model_call_usage,
)
from custom_tools.text_to_sql.adaptive.model_budget import (
    ModelBudgetLedgerRecord,
    ModelCallReconciliation,
    ModelCallReservation,
    ModelCallResult,
    ModelCallStarted,
)
from custom_tools.text_to_sql.adaptive.probes import ProbeResult
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)

from .sqlite_schema_signature import (
    SQLiteSchemaSignatureError,
    owned_sqlite_schema_signature,
)
from ._adaptive_budget_ledger_common import (
    _ClaimAcquisition,
    _LedgerNamespace,
    _claim_execution,
    _existing_phase_or_conflict,
    _insert_event as _insert_common_event,
    _require_claim_owner,
    _validate_claim_rows,
)
from .state_files import prepare_sqlite_file, secure_sqlite_sidecars


ADAPTIVE_BUDGET_SCHEMA_VERSION = 2
EXECUTION_CLAIM_LEASE_NS = MAX_WALL_CLOCK_SECONDS * 1_000_000_000
_META_TABLE = "adaptive_budget_meta"
_EVENT_TABLE = "adaptive_budget_events"
_CLAIM_TABLE = "adaptive_budget_execution_claims"
_MODEL_EVENT_TABLE = "adaptive_model_budget_events"
_MODEL_CLAIM_TABLE = "adaptive_model_budget_execution_claims"
_SCHEMA_PREFIX = "adaptive_budget_"
_MODEL_SCHEMA_PREFIX = "adaptive_model_budget_"
_VERSION_KEY = "schema_version"
_PHASES = ("reservation", "result", "reconciliation")
_PROBE_LEDGER_NAMESPACE = _LedgerNamespace(
    event_table=_EVENT_TABLE,
    claim_table=_CLAIM_TABLE,
    identity_columns=("run_id", "run_incarnation", "revision"),
    phases=_PHASES,
    label="adaptive budget",
)
_MODEL_LEDGER_NAMESPACE = _LedgerNamespace(
    event_table=_MODEL_EVENT_TABLE,
    claim_table=_MODEL_CLAIM_TABLE,
    identity_columns=("run_id", "run_incarnation", "call_id"),
    phases=("reservation", "started", "result", "reconciliation"),
    label="adaptive model budget",
)
_V1_SCHEMA_SQL = (
    f"""
    CREATE TABLE {_EVENT_TABLE} (
        run_id TEXT NOT NULL,
        run_incarnation TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 0),
        phase TEXT NOT NULL CHECK (phase IN ('reservation', 'result', 'reconciliation')),
        payload BLOB NOT NULL CHECK (typeof(payload) = 'blob'),
        identity_digest TEXT NOT NULL,
        created_at_ns INTEGER NOT NULL CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, revision, phase)
    )
    """,
    f"""
    CREATE TABLE {_CLAIM_TABLE} (
        run_id TEXT NOT NULL,
        run_incarnation TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 0),
        reservation_digest TEXT NOT NULL,
        owner_token TEXT NOT NULL,
        claimed_at_ns INTEGER NOT NULL CHECK (typeof(claimed_at_ns) = 'integer' AND claimed_at_ns >= 0),
        lease_expires_ns INTEGER NOT NULL CHECK (typeof(lease_expires_ns) = 'integer' AND lease_expires_ns >= 0),
        generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 0),
        PRIMARY KEY (run_id, run_incarnation, revision)
    )
    """,
    f"""
    CREATE TABLE {_META_TABLE} (
        key TEXT PRIMARY KEY,
        value INTEGER NOT NULL
    )
    """,
    f"""
    CREATE TRIGGER adaptive_budget_events_no_update
    BEFORE UPDATE ON {_EVENT_TABLE}
    BEGIN SELECT RAISE(ABORT, 'adaptive budget events are immutable'); END
    """,
    f"""
    CREATE TRIGGER adaptive_budget_events_no_delete
    BEFORE DELETE ON {_EVENT_TABLE}
    BEGIN SELECT RAISE(ABORT, 'adaptive budget events are immutable'); END
    """,
)
_MODEL_SCHEMA_SQL = (
    f"""
    CREATE TABLE {_MODEL_EVENT_TABLE} (
        run_id TEXT NOT NULL,
        run_incarnation TEXT NOT NULL,
        call_id TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN ('reservation', 'started', 'result', 'reconciliation')),
        payload BLOB NOT NULL CHECK (typeof(payload) = 'blob'),
        identity_digest TEXT NOT NULL,
        created_at_ns INTEGER NOT NULL CHECK (typeof(created_at_ns) = 'integer' AND created_at_ns >= 0),
        PRIMARY KEY (run_id, run_incarnation, call_id, phase)
    )
    """,
    f"""
    CREATE TABLE {_MODEL_CLAIM_TABLE} (
        run_id TEXT NOT NULL,
        run_incarnation TEXT NOT NULL,
        call_id TEXT NOT NULL,
        reservation_digest TEXT NOT NULL,
        owner_token TEXT NOT NULL,
        claimed_at_ns INTEGER NOT NULL CHECK (typeof(claimed_at_ns) = 'integer' AND claimed_at_ns >= 0),
        lease_expires_ns INTEGER NOT NULL CHECK (typeof(lease_expires_ns) = 'integer' AND lease_expires_ns >= 0),
        generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 0),
        PRIMARY KEY (run_id, run_incarnation, call_id)
    )
    """,
    f"""
    CREATE TRIGGER adaptive_model_budget_events_no_update
    BEFORE UPDATE ON {_MODEL_EVENT_TABLE}
    BEGIN SELECT RAISE(ABORT, 'adaptive model budget events are immutable'); END
    """,
    f"""
    CREATE TRIGGER adaptive_model_budget_events_no_delete
    BEFORE DELETE ON {_MODEL_EVENT_TABLE}
    BEGIN SELECT RAISE(ABORT, 'adaptive model budget events are immutable'); END
    """,
)
_SCHEMA_SQL = (*_V1_SCHEMA_SQL, *_MODEL_SCHEMA_SQL)


class AdaptiveBudgetLedger:
    """Budget-only store; controller checkpoints use a different namespace."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        commit_fence: object | None = None,
    ) -> None:
        if commit_fence is not None and not callable(
            getattr(commit_fence, "require_open_in_transaction", None)
        ):
            raise TypeError("commit_fence must provide require_open_in_transaction")
        self.db_path = str(db_path)
        self._commit_fence = commit_fence
        self._memory_uri: str | None = None
        self._memory_anchor: sqlite3.Connection | None = None
        self._closed = False
        self._lifecycle_lock = threading.Lock()
        if self.db_path == ":memory:":
            self._memory_uri = (
                f"file:adaptive-budget-{uuid.uuid4().hex}?mode=memory&cache=shared"
            )
            self._memory_anchor = sqlite3.connect(self._memory_uri, uri=True)
        else:
            prepare_sqlite_file(self.db_path, tighten_existing=True)
        try:
            self._migrate()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            anchor = self._memory_anchor
            self._memory_anchor = None
        if anchor is not None:
            anchor.close()

    def load_records(
        self,
        run_id: str,
        run_incarnation: str,
    ) -> tuple[BudgetLedgerRecord, ...]:
        _validate_lookup_identity(run_id, run_incarnation)
        with self._connection() as connection:
            return _load_records(connection, run_id, run_incarnation)

    def load_model_records(
        self,
        run_id: str,
        run_incarnation: str,
    ) -> tuple[ModelBudgetLedgerRecord, ...]:
        _validate_lookup_identity(run_id, run_incarnation)
        with self._connection() as connection:
            return _load_model_records(connection, run_id, run_incarnation)

    def list_model_run_incarnations(self, run_id: str) -> tuple[str, ...]:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id is required")
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT run_incarnation
                FROM {_MODEL_EVENT_TABLE}
                WHERE run_id = ?
                ORDER BY run_incarnation
                """,
                (run_id,),
            ).fetchall()
        return tuple(row["run_incarnation"] for row in rows)

    def record_model_reservation(
        self,
        reservation: ModelCallReservation,
    ) -> ModelCallReservation:
        checked = _validate_model(
            reservation,
            ModelCallReservation,
            "model budget reservation",
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            records = _load_model_records(
                connection, checked.run_id, checked.run_incarnation
            )
            for record in records:
                if record.reservation.call_id != checked.call_id:
                    continue
                if record.reservation == checked:
                    return checked
                raise BudgetConflictError("model call already has a reservation")
            if any(record.reconciliation is None for record in records):
                raise BudgetConflictError(
                    "outstanding model reservation blocks next call"
                )
            _insert_model_event(
                connection,
                checked,
                "reservation",
                canonical_json_bytes(checked),
                checked.reservation_digest,
            )
        self._secure_sidecars()
        return checked

    def claim_model_execution(
        self,
        reservation: ModelCallReservation,
        owner_token: str,
        *,
        now_ns: int,
    ) -> _ClaimAcquisition:
        checked = _validate_model(
            reservation,
            ModelCallReservation,
            "model budget reservation",
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = _find_model_record(connection, checked)
            if record.result is not None:
                return _ClaimAcquisition(
                    acquired=False,
                    generation=record.result.claim_generation,
                )
            return _claim_execution(
                connection,
                _MODEL_LEDGER_NAMESPACE,
                (checked.run_id, checked.run_incarnation, checked.call_id),
                checked.reservation_digest,
                owner_token,
                now_ns,
                EXECUTION_CLAIM_LEASE_NS,
            )

    def record_model_started(
        self,
        started: ModelCallStarted,
        *,
        owner_token: str,
    ) -> tuple[ModelCallStarted, bool]:
        checked = _validate_model(started, ModelCallStarted, "model call started")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = _find_model_record(connection, checked.reservation)
            _require_model_claim_owner(
                connection,
                checked.reservation,
                owner_token,
                checked.claim_generation,
            )
            if (
                record.started is not None
                and record.started.claim_generation < checked.claim_generation
            ):
                return record.started, False
            existing = _existing_phase_or_conflict(
                record.started, checked, "durable model started event"
            )
            if existing is not None:
                return existing, False
            _insert_model_event(
                connection,
                checked.reservation,
                "started",
                canonical_json_bytes(checked),
                checked.started_digest,
            )
        self._secure_sidecars()
        return checked, True

    def record_model_result(
        self,
        result: ModelCallResult,
        *,
        owner_token: str,
    ) -> ModelCallResult:
        checked = _validate_model(result, ModelCallResult, "model call result")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = _find_model_record(connection, checked.reservation)
            _require_model_claim_owner(
                connection,
                checked.reservation,
                owner_token,
                checked.claim_generation,
            )
            existing = _existing_phase_or_conflict(
                record.result, checked, "durable model result"
            )
            if existing is not None:
                return existing
            if (
                record.started is None
                or record.started.invocation_id != checked.invocation_id
            ):
                raise BudgetConflictError("model result has no matching started event")
            if checked.started_claim_generation != record.started.claim_generation:
                raise BudgetConflictError(
                    "model result started claim generation is conflicting"
                )
            if checked.claim_generation < record.started.claim_generation:
                raise BudgetConflictError("model result claim generation is stale")
            if checked.claim_generation > record.started.claim_generation and (
                checked.usage.input_tokens is not None
                or checked.usage.output_tokens is not None
            ):
                raise BudgetConflictError(
                    "takeover model result usage must be conservative"
                )
            _insert_model_event(
                connection,
                checked.reservation,
                "result",
                canonical_json_bytes(checked),
                checked.result_digest,
            )
        self._secure_sidecars()
        return checked

    def record_model_reconciliation(
        self,
        reconciliation: ModelCallReconciliation,
        result: ModelCallResult,
    ) -> ModelCallReconciliation:
        checked_reconciliation = _validate_model(
            reconciliation,
            ModelCallReconciliation,
            "model budget reconciliation",
        )
        checked_result = _validate_model(result, ModelCallResult, "model call result")
        if checked_reconciliation != reconcile_model_call_usage(checked_result):
            raise BudgetConflictError("model reconciliation is not canonical")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = _find_model_record(connection, checked_reconciliation.reservation)
            if record.result != checked_result:
                raise BudgetConflictError("durable model result is conflicting")
            existing = _existing_phase_or_conflict(
                record.reconciliation,
                checked_reconciliation,
                "durable model reconciliation",
            )
            if existing is not None:
                return existing
            ModelBudgetLedgerRecord(
                reservation=checked_reconciliation.reservation,
                started=record.started,
                result=checked_result,
                reconciliation=checked_reconciliation,
            )
            _insert_model_event(
                connection,
                checked_reconciliation.reservation,
                "reconciliation",
                canonical_json_bytes(checked_reconciliation),
                checked_reconciliation.reconciliation_digest,
            )
        self._secure_sidecars()
        return checked_reconciliation

    def record_reservation(
        self,
        reservation: BudgetReservation,
    ) -> BudgetReservation:
        checked = _validate_model(
            reservation,
            BudgetReservation,
            "budget reservation",
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            records = _load_records(
                connection,
                checked.run_id,
                checked.run_incarnation,
            )
            for record in records:
                if record.reservation.revision != checked.revision:
                    continue
                if record.reservation == checked:
                    return checked
                raise BudgetConflictError("budget revision already has a reservation")
            if any(record.reconciliation is None for record in records):
                raise BudgetConflictError(
                    "outstanding reservation blocks the next budget action"
                )
            if records and checked.revision <= records[-1].reservation.revision:
                raise BudgetConflictError("budget reservation revision is not next")
            _insert_event(
                connection,
                checked,
                "reservation",
                canonical_json_bytes(checked),
                checked.reservation_digest,
            )
        self._secure_sidecars()
        return checked

    def claim_execution(
        self,
        reservation: BudgetReservation,
        owner_token: str,
        *,
        now_ns: int,
    ) -> bool:
        checked = _validate_model(
            reservation,
            BudgetReservation,
            "budget reservation",
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = _find_record(connection, checked)
            if record.result is not None:
                return False
            claim = _claim_execution(
                connection,
                _PROBE_LEDGER_NAMESPACE,
                (checked.run_id, checked.run_incarnation, checked.revision),
                checked.reservation_digest,
                owner_token,
                now_ns,
                EXECUTION_CLAIM_LEASE_NS,
            )
            return claim.acquired

    def record_result(
        self,
        reservation: BudgetReservation,
        result: ProbeResult,
        *,
        owner_token: str,
    ) -> ProbeResult:
        checked_reservation = _validate_model(
            reservation,
            BudgetReservation,
            "budget reservation",
        )
        checked_result = _validate_model(result, ProbeResult, "probe result")
        BudgetLedgerRecord(
            reservation=checked_reservation,
            result=checked_result,
            reconciliation=None,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = _find_record(connection, checked_reservation)
            existing = _existing_phase_or_conflict(
                record.result, checked_result, "durable probe result"
            )
            if existing is not None:
                return existing
            _require_claim_owner(
                connection,
                _PROBE_LEDGER_NAMESPACE,
                (
                    checked_reservation.run_id,
                    checked_reservation.run_incarnation,
                    checked_reservation.revision,
                ),
                owner_token,
            )
            _insert_event(
                connection,
                checked_reservation,
                "result",
                canonical_json_bytes(checked_result),
                canonical_digest(checked_result),
            )
        self._secure_sidecars()
        return checked_result

    def record_reconciliation(
        self,
        reconciliation: BudgetReconciliation,
        result: ProbeResult,
    ) -> BudgetReconciliation:
        checked_reconciliation = _validate_model(
            reconciliation,
            BudgetReconciliation,
            "budget reconciliation",
        )
        checked_result = _validate_model(result, ProbeResult, "probe result")
        BudgetLedgerRecord(
            reservation=checked_reconciliation.reservation,
            result=checked_result,
            reconciliation=checked_reconciliation,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = _find_record(connection, checked_reconciliation.reservation)
            if record.result != checked_result:
                raise BudgetConflictError("durable probe result is conflicting")
            existing = _existing_phase_or_conflict(
                record.reconciliation,
                checked_reconciliation,
                "durable probe reconciliation",
            )
            if existing is not None:
                return existing
            _insert_event(
                connection,
                checked_reconciliation.reservation,
                "reconciliation",
                canonical_json_bytes(checked_reconciliation),
                checked_reconciliation.reconciliation_digest,
            )
        self._secure_sidecars()
        return checked_reconciliation

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = _read_schema_version(connection)
            if version is None:
                if _owned_schema_signature(connection):
                    raise BudgetAdmissionError("adaptive budget schema is incomplete")
                for statement in _SCHEMA_SQL:
                    connection.execute(statement)
                connection.execute(
                    f"INSERT INTO {_META_TABLE} (key, value) VALUES (?, ?)",
                    (_VERSION_KEY, ADAPTIVE_BUDGET_SCHEMA_VERSION),
                )
            elif version == 1:
                if _owned_model_schema_signature(connection):
                    raise BudgetAdmissionError(
                        "adaptive model budget v2 schema is partial or conflicting"
                    )
                if (
                    _owned_v1_schema_signature(connection)
                    != _canonical_v1_schema_signature()
                ):
                    raise BudgetAdmissionError("adaptive budget schema is incompatible")
                for statement in _MODEL_SCHEMA_SQL:
                    connection.execute(statement)
                connection.execute(
                    f"UPDATE {_META_TABLE} SET value = ? WHERE key = ?",
                    (ADAPTIVE_BUDGET_SCHEMA_VERSION, _VERSION_KEY),
                )
            elif version != ADAPTIVE_BUDGET_SCHEMA_VERSION:
                raise BudgetAdmissionError(
                    "adaptive budget schema version is unsupported"
                )
            if _owned_schema_signature(connection) != _canonical_schema_signature():
                raise BudgetAdmissionError("adaptive budget schema is incompatible")
            meta_rows = connection.execute(
                f"SELECT key, value FROM {_META_TABLE} ORDER BY key"
            ).fetchall()
            if len(meta_rows) != 1 or (
                meta_rows[0]["key"],
                meta_rows[0]["value"],
            ) != (_VERSION_KEY, ADAPTIVE_BUDGET_SCHEMA_VERSION):
                raise BudgetAdmissionError("adaptive budget metadata is incompatible")
            for row in connection.execute(
                f"""
                SELECT run_id, run_incarnation FROM {_EVENT_TABLE}
                GROUP BY run_id, run_incarnation
                """
            ):
                _load_records(connection, row["run_id"], row["run_incarnation"])
            _validate_claims(connection)
            for row in connection.execute(
                f"""
                SELECT run_id, run_incarnation FROM {_MODEL_EVENT_TABLE}
                GROUP BY run_id, run_incarnation
                """
            ):
                _load_model_records(connection, row["run_id"], row["run_incarnation"])
            _validate_model_claims(connection)
        self._secure_sidecars()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._memory_uri or self.db_path,
            timeout=30,
            isolation_level=None,
            uri=self._memory_uri is not None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lifecycle_lock:
            if self._closed:
                raise BudgetAdmissionError("adaptive budget ledger is closed")
            connection = self._connect()
        changes_before = connection.total_changes
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            try:
                if (
                    connection.total_changes != changes_before
                    and self._commit_fence is not None
                ):
                    self._commit_fence.require_open_in_transaction(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()

    def _secure_sidecars(self) -> None:
        if self.db_path != ":memory:":
            secure_sqlite_sidecars(self.db_path, tighten_existing=True)


def _insert_event(
    connection: sqlite3.Connection,
    reservation: BudgetReservation,
    phase: str,
    payload: bytes,
    identity_digest: str,
) -> None:
    _insert_common_event(
        connection,
        _PROBE_LEDGER_NAMESPACE,
        (
            reservation.run_id,
            reservation.run_incarnation,
            reservation.revision,
        ),
        phase,
        payload,
        identity_digest,
    )


def _insert_model_event(
    connection: sqlite3.Connection,
    reservation: ModelCallReservation,
    phase: str,
    payload: bytes,
    identity_digest: str,
) -> None:
    _insert_common_event(
        connection,
        _MODEL_LEDGER_NAMESPACE,
        (
            reservation.run_id,
            reservation.run_incarnation,
            reservation.call_id,
        ),
        phase,
        payload,
        identity_digest,
    )


def _load_records(
    connection: sqlite3.Connection,
    run_id: str,
    run_incarnation: str,
) -> tuple[BudgetLedgerRecord, ...]:
    rows = connection.execute(
        f"""
        SELECT revision, phase, payload, identity_digest
        FROM {_EVENT_TABLE}
        WHERE run_id = ? AND run_incarnation = ?
        ORDER BY revision,
                 CASE phase
                     WHEN 'reservation' THEN 0
                     WHEN 'result' THEN 1
                     ELSE 2
                 END
        """,
        (run_id, run_incarnation),
    ).fetchall()
    grouped: dict[int, dict[str, sqlite3.Row]] = {}
    for row in rows:
        revision = row["revision"]
        phases = grouped.setdefault(revision, {})
        if row["phase"] in phases:
            raise BudgetAdmissionError("adaptive budget event phase is duplicated")
        phases[row["phase"]] = row
    records: list[BudgetLedgerRecord] = []
    for revision in sorted(grouped):
        phases = grouped[revision]
        if set(phases) not in (
            {"reservation"},
            {"reservation", "result"},
            {"reservation", "result", "reconciliation"},
        ):
            raise BudgetAdmissionError("adaptive budget event sequence is incomplete")
        reservation = _decode_event(
            phases["reservation"],
            BudgetReservation,
            "reservation",
        )
        if reservation.revision != revision:
            raise BudgetAdmissionError("stored budget revision is invalid")
        result = (
            None
            if "result" not in phases
            else _decode_event(phases["result"], ProbeResult, "result")
        )
        reconciliation = (
            None
            if "reconciliation" not in phases
            else _decode_event(
                phases["reconciliation"],
                BudgetReconciliation,
                "reconciliation",
            )
        )
        records.append(
            BudgetLedgerRecord(
                reservation=reservation,
                result=result,
                reconciliation=reconciliation,
            )
        )
    return tuple(records)


def _load_model_records(
    connection: sqlite3.Connection,
    run_id: str,
    run_incarnation: str,
) -> tuple[ModelBudgetLedgerRecord, ...]:
    rows = connection.execute(
        f"""
        SELECT call_id, phase, payload, identity_digest, created_at_ns
        FROM {_MODEL_EVENT_TABLE}
        WHERE run_id = ? AND run_incarnation = ?
        ORDER BY created_at_ns, call_id,
                 CASE phase
                     WHEN 'reservation' THEN 0
                     WHEN 'started' THEN 1
                     WHEN 'result' THEN 2
                     ELSE 3
                 END
        """,
        (run_id, run_incarnation),
    ).fetchall()
    grouped: dict[str, dict[str, sqlite3.Row]] = {}
    call_order: list[str] = []
    for row in rows:
        call_id = row["call_id"]
        phases = grouped.setdefault(call_id, {})
        if not phases:
            call_order.append(call_id)
        if row["phase"] in phases:
            raise BudgetAdmissionError(
                "adaptive model budget event phase is duplicated"
            )
        phases[row["phase"]] = row

    records: list[ModelBudgetLedgerRecord] = []
    for call_id in call_order:
        phases = grouped[call_id]
        if set(phases) not in (
            {"reservation"},
            {"reservation", "started"},
            {"reservation", "started", "result"},
            {"reservation", "started", "result", "reconciliation"},
        ):
            raise BudgetAdmissionError(
                "adaptive model budget event sequence is incomplete"
            )
        reservation = _decode_model_event(
            phases["reservation"], ModelCallReservation, "reservation"
        )
        if (
            reservation.run_id != run_id
            or reservation.run_incarnation != run_incarnation
            or reservation.call_id != call_id
        ):
            raise BudgetAdmissionError("stored model reservation identity is invalid")
        started = (
            None
            if "started" not in phases
            else _decode_model_event(phases["started"], ModelCallStarted, "started")
        )
        result = (
            None
            if "result" not in phases
            else _decode_model_event(phases["result"], ModelCallResult, "result")
        )
        reconciliation = (
            None
            if "reconciliation" not in phases
            else _decode_model_event(
                phases["reconciliation"], ModelCallReconciliation, "reconciliation"
            )
        )
        try:
            record = ModelBudgetLedgerRecord(
                reservation=reservation,
                started=started,
                result=result,
                reconciliation=reconciliation,
            )
        except ValidationError as exc:
            raise BudgetAdmissionError(
                "adaptive model budget record is invalid"
            ) from exc
        if reconciliation is not None and result is not None:
            if reconciliation != reconcile_model_call_usage(result):
                raise BudgetAdmissionError("adaptive model reconciliation is invalid")
        records.append(record)
    return tuple(records)


def _validate_claims(connection: sqlite3.Connection) -> None:
    reservations: dict[tuple[str, str, int], BudgetReservation] = {}
    for row in connection.execute(
        f"""
        SELECT run_id, run_incarnation, revision, phase, payload, identity_digest
        FROM {_EVENT_TABLE}
        WHERE phase = 'reservation'
        ORDER BY run_id, run_incarnation, revision
        """
    ):
        reservation = _decode_event(row, BudgetReservation, "reservation")
        key = (row["run_id"], row["run_incarnation"], row["revision"])
        if key != (
            reservation.run_id,
            reservation.run_incarnation,
            reservation.revision,
        ):
            raise BudgetAdmissionError(
                "adaptive budget reservation storage identity is invalid"
            )
        if key in reservations:
            raise BudgetAdmissionError("adaptive budget reservation is duplicated")
        reservations[key] = reservation

    _validate_claim_rows(
        connection,
        _PROBE_LEDGER_NAMESPACE,
        reservations,
        EXECUTION_CLAIM_LEASE_NS,
    )


def _validate_model_claims(connection: sqlite3.Connection) -> None:
    reservations: dict[tuple[str, str, str], ModelCallReservation] = {}
    for row in connection.execute(
        f"""
        SELECT run_id, run_incarnation, call_id, phase, payload, identity_digest
        FROM {_MODEL_EVENT_TABLE}
        WHERE phase = 'reservation'
        ORDER BY run_id, run_incarnation, call_id
        """
    ):
        reservation = _decode_model_event(row, ModelCallReservation, "reservation")
        key = (row["run_id"], row["run_incarnation"], row["call_id"])
        if (
            key
            != (
                reservation.run_id,
                reservation.run_incarnation,
                reservation.call_id,
            )
            or key in reservations
        ):
            raise BudgetAdmissionError("adaptive model reservation storage is invalid")
        reservations[key] = reservation
    claim_generations = _validate_claim_rows(
        connection,
        _MODEL_LEDGER_NAMESPACE,
        reservations,
        EXECUTION_CLAIM_LEASE_NS,
    )
    for row in connection.execute(
        f"""
        SELECT run_id, run_incarnation, call_id, phase, payload, identity_digest
        FROM {_MODEL_EVENT_TABLE}
        WHERE phase IN ('started', 'result')
        ORDER BY run_id, run_incarnation, call_id, phase
        """
    ):
        key = (row["run_id"], row["run_incarnation"], row["call_id"])
        generation = claim_generations.get(key)
        event_type = ModelCallStarted if row["phase"] == "started" else ModelCallResult
        event = _decode_model_event(row, event_type, row["phase"])
        if generation is None or event.claim_generation > generation:
            raise BudgetAdmissionError(
                "adaptive model event claim generation is invalid"
            )
        if row["phase"] == "result" and event.claim_generation != generation:
            raise BudgetAdmissionError(
                "adaptive model result claim generation is stale"
            )


def _require_model_claim_owner(
    connection: sqlite3.Connection,
    reservation: ModelCallReservation,
    owner_token: str,
    claim_generation: int,
) -> None:
    _require_claim_owner(
        connection,
        _MODEL_LEDGER_NAMESPACE,
        (reservation.run_id, reservation.run_incarnation, reservation.call_id),
        owner_token,
        claim_generation,
    )


def _decode_event(row: sqlite3.Row, model_type, phase: str):
    payload = row["payload"]
    if not isinstance(payload, bytes) or row["phase"] != phase:
        raise BudgetAdmissionError("adaptive budget event storage type is invalid")
    try:
        decoded = model_type.model_validate_json(payload)
    except (ResearchPolicyError, ValidationError, ValueError, TypeError) as exc:
        raise BudgetAdmissionError("adaptive budget event payload is invalid") from exc
    expected_identity = (
        decoded.reservation_digest
        if phase == "reservation"
        else decoded.reconciliation_digest
        if phase == "reconciliation"
        else canonical_digest(decoded)
    )
    if (
        canonical_json_bytes(decoded) != payload
        or row["identity_digest"] != expected_identity
    ):
        raise BudgetAdmissionError("adaptive budget event identity is invalid")
    return decoded


def _decode_model_event(row: sqlite3.Row, model_type, phase: str):
    payload = row["payload"]
    if not isinstance(payload, bytes) or row["phase"] != phase:
        raise BudgetAdmissionError(
            "adaptive model budget event storage type is invalid"
        )
    try:
        decoded = model_type.model_validate_json(payload)
    except (ValidationError, ValueError, TypeError) as exc:
        raise BudgetAdmissionError(
            "adaptive model budget event payload is invalid"
        ) from exc
    expected_identity = (
        decoded.reservation_digest
        if phase == "reservation"
        else decoded.started_digest
        if phase == "started"
        else decoded.result_digest
        if phase == "result"
        else decoded.reconciliation_digest
    )
    if (
        canonical_json_bytes(decoded) != payload
        or row["identity_digest"] != expected_identity
    ):
        raise BudgetAdmissionError("adaptive model budget event identity is invalid")
    return decoded


def _find_record(
    connection: sqlite3.Connection,
    reservation: BudgetReservation,
) -> BudgetLedgerRecord:
    records = _load_records(
        connection,
        reservation.run_id,
        reservation.run_incarnation,
    )
    matches = [
        record
        for record in records
        if record.reservation.revision == reservation.revision
    ]
    if len(matches) != 1 or matches[0].reservation != reservation:
        raise BudgetConflictError(
            "durable budget reservation is missing or conflicting"
        )
    return matches[0]


def _find_model_record(
    connection: sqlite3.Connection,
    reservation: ModelCallReservation,
) -> ModelBudgetLedgerRecord:
    records = _load_model_records(
        connection,
        reservation.run_id,
        reservation.run_incarnation,
    )
    matches = [
        record
        for record in records
        if record.reservation.call_id == reservation.call_id
    ]
    if len(matches) != 1 or matches[0].reservation != reservation:
        raise BudgetConflictError(
            "durable model budget reservation is missing or conflicting"
        )
    return matches[0]


def _read_schema_version(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_META_TABLE,),
    ).fetchone()
    if row is None:
        return None
    version = connection.execute(
        f"SELECT value FROM {_META_TABLE} WHERE key = ?",
        (_VERSION_KEY,),
    ).fetchone()
    if version is None or type(version["value"]) is not int:
        raise BudgetAdmissionError("adaptive budget schema version is invalid")
    return int(version["value"])


def _canonical_schema_signature() -> tuple[
    tuple[str, str, str, tuple[tuple[str, str], ...] | None], ...
]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.row_factory = sqlite3.Row
        for statement in _SCHEMA_SQL:
            connection.execute(statement)
        return _owned_schema_signature(connection)
    finally:
        connection.close()


def _canonical_v1_schema_signature() -> tuple[
    tuple[str, str, str, tuple[tuple[str, str], ...] | None], ...
]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.row_factory = sqlite3.Row
        for statement in _V1_SCHEMA_SQL:
            connection.execute(statement)
        return _owned_v1_schema_signature(connection)
    finally:
        connection.close()


def _owned_schema_signature(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, tuple[tuple[str, str], ...] | None], ...]:
    try:
        probe_signature = owned_sqlite_schema_signature(
            connection,
            prefix=_SCHEMA_PREFIX,
            table_names=(_EVENT_TABLE, _CLAIM_TABLE, _META_TABLE),
        )
        return tuple(
            sorted(
                {*probe_signature, *_owned_model_schema_signature(connection)},
                key=lambda item: (item[0], item[1]),
            )
        )
    except SQLiteSchemaSignatureError as exc:
        raise BudgetAdmissionError(
            "adaptive budget schema definition is invalid"
        ) from exc


def _owned_model_schema_signature(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, tuple[tuple[str, str], ...] | None], ...]:
    try:
        return owned_sqlite_schema_signature(
            connection,
            prefix=_MODEL_SCHEMA_PREFIX,
            table_names=(_MODEL_EVENT_TABLE, _MODEL_CLAIM_TABLE),
        )
    except SQLiteSchemaSignatureError as exc:
        raise BudgetAdmissionError(
            "adaptive model budget schema definition is invalid"
        ) from exc


def _owned_v1_schema_signature(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, tuple[tuple[str, str], ...] | None], ...]:
    try:
        return owned_sqlite_schema_signature(
            connection,
            prefix=_SCHEMA_PREFIX,
            table_names=(_EVENT_TABLE, _CLAIM_TABLE, _META_TABLE),
        )
    except SQLiteSchemaSignatureError as exc:
        raise BudgetAdmissionError(
            "adaptive budget schema definition is invalid"
        ) from exc


def _validate_lookup_identity(run_id: str, run_incarnation: str) -> None:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id is required")
    if not isinstance(run_incarnation, str) or not run_incarnation:
        raise ValueError("run_incarnation is required")


def _validate_model(value, model_type, label: str):
    if not isinstance(value, model_type):
        raise TypeError(f"{label} has an invalid type")
    try:
        return model_type.model_validate(
            value.model_dump(mode="python", round_trip=True)
        )
    except (ResearchPolicyError, ValidationError, ValueError, TypeError) as exc:
        raise BudgetAdmissionError(f"{label} is invalid") from exc
