"""Shared SQLite mechanics for probe and model budget event lifecycles."""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
import time
from typing import Any

from custom_tools.text_to_sql.adaptive.model_budget import (
    SQLITE_SIGNED_INTEGER_MAX,
)
from custom_tools.text_to_sql.adaptive.policy import (
    BudgetAdmissionError,
    BudgetConflictError,
)


_SQL_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


@dataclass(frozen=True, slots=True)
class _LedgerNamespace:
    event_table: str
    claim_table: str
    identity_columns: tuple[str, ...]
    phases: tuple[str, ...]
    label: str

    def __post_init__(self) -> None:
        identifiers = (self.event_table, self.claim_table, *self.identity_columns)
        if not all(_SQL_IDENTIFIER_RE.fullmatch(value) for value in identifiers):
            raise ValueError("ledger namespace contains an invalid SQL identifier")
        if not self.identity_columns or not self.phases or not self.label:
            raise ValueError("ledger namespace is incomplete")


@dataclass(frozen=True, slots=True)
class _ClaimAcquisition:
    """Result of one atomic claim attempt and its durable fencing number."""

    acquired: bool
    generation: int


def _insert_event(
    connection: sqlite3.Connection,
    namespace: _LedgerNamespace,
    identity_values: tuple[Any, ...],
    phase: str,
    payload: bytes,
    identity_digest: str,
) -> None:
    if len(identity_values) != len(namespace.identity_columns):
        raise ValueError("ledger event identity arity is invalid")
    if phase not in namespace.phases:
        raise ValueError(f"{namespace.label} event phase is invalid")
    columns = (
        *namespace.identity_columns,
        "phase",
        "payload",
        "identity_digest",
        "created_at_ns",
    )
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {namespace.event_table} ({', '.join(columns)}) "
        f"VALUES ({placeholders})",
        (*identity_values, phase, payload, identity_digest, time.time_ns()),
    )


def _existing_phase_or_conflict(existing: Any, proposed: Any, label: str) -> Any | None:
    if existing is None:
        return None
    if existing == proposed:
        return existing
    raise BudgetConflictError(f"{label} is conflicting")


def _claim_execution(
    connection: sqlite3.Connection,
    namespace: _LedgerNamespace,
    identity_values: tuple[Any, ...],
    reservation_digest: str,
    owner_token: str,
    now_ns: int,
    lease_ns: int,
) -> _ClaimAcquisition:
    _validate_claim_input(owner_token, now_ns)
    if len(identity_values) != len(namespace.identity_columns):
        raise ValueError("ledger claim identity arity is invalid")
    lease_expires_ns = now_ns + lease_ns
    if lease_expires_ns > SQLITE_SIGNED_INTEGER_MAX:
        raise ValueError("execution claim expiry exceeds SQLite integer range")
    where = " AND ".join(f"{column} = ?" for column in namespace.identity_columns)
    row = connection.execute(
        f"SELECT reservation_digest, owner_token, lease_expires_ns, generation "
        f"FROM {namespace.claim_table} WHERE {where}",
        identity_values,
    ).fetchone()
    if row is None:
        columns = (
            *namespace.identity_columns,
            "reservation_digest",
            "owner_token",
            "claimed_at_ns",
            "lease_expires_ns",
            "generation",
        )
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {namespace.claim_table} ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            (
                *identity_values,
                reservation_digest,
                owner_token,
                now_ns,
                lease_expires_ns,
                0,
            ),
        )
        return _ClaimAcquisition(acquired=True, generation=0)
    if row["reservation_digest"] != reservation_digest:
        raise BudgetConflictError(f"{namespace.label} claim reservation is conflicting")
    current_generation = row["generation"]
    if (
        type(current_generation) is not int
        or current_generation < 0
        or current_generation > SQLITE_SIGNED_INTEGER_MAX
    ):
        raise BudgetConflictError(f"{namespace.label} claim generation is invalid")
    if now_ns < row["lease_expires_ns"]:
        return _ClaimAcquisition(acquired=False, generation=current_generation)
    if current_generation == SQLITE_SIGNED_INTEGER_MAX:
        raise BudgetConflictError(f"{namespace.label} claim generation is exhausted")
    next_generation = current_generation + 1
    connection.execute(
        f"UPDATE {namespace.claim_table} "
        f"SET owner_token = ?, claimed_at_ns = ?, lease_expires_ns = ?, generation = ? "
        f"WHERE {where}",
        (
            owner_token,
            now_ns,
            lease_expires_ns,
            next_generation,
            *identity_values,
        ),
    )
    return _ClaimAcquisition(acquired=True, generation=next_generation)


def _require_claim_owner(
    connection: sqlite3.Connection,
    namespace: _LedgerNamespace,
    identity_values: tuple[Any, ...],
    owner_token: str,
    claim_generation: int | None = None,
) -> None:
    if not isinstance(owner_token, str) or not owner_token:
        raise TypeError("owner_token must be a non-empty string")
    if claim_generation is not None and (
        type(claim_generation) is not int
        or claim_generation < 0
        or claim_generation > SQLITE_SIGNED_INTEGER_MAX
    ):
        raise ValueError("claim_generation is outside the SQLite integer range")
    where = " AND ".join(f"{column} = ?" for column in namespace.identity_columns)
    claim = connection.execute(
        f"SELECT owner_token, generation FROM {namespace.claim_table} WHERE {where}",
        identity_values,
    ).fetchone()
    if claim is None or claim["owner_token"] != owner_token:
        raise BudgetConflictError(
            f"{namespace.label} executor does not own the durable claim"
        )
    if claim_generation is not None and claim["generation"] != claim_generation:
        raise BudgetConflictError(
            f"{namespace.label} executor claim generation is stale"
        )


def _validate_claim_rows(
    connection: sqlite3.Connection,
    namespace: _LedgerNamespace,
    reservations: dict[tuple[Any, ...], Any],
    lease_ns: int,
) -> dict[tuple[Any, ...], int]:
    identity_select = ", ".join(namespace.identity_columns)
    identity_types = ", ".join(
        f"typeof({column}) AS {column}_type" for column in namespace.identity_columns
    )
    claims = connection.execute(
        f"SELECT {identity_select}, reservation_digest, owner_token, claimed_at_ns, "
        f"lease_expires_ns, generation, {identity_types}, "
        "typeof(reservation_digest) AS reservation_digest_type, "
        "typeof(owner_token) AS owner_token_type, "
        "typeof(claimed_at_ns) AS claimed_at_ns_type, "
        "typeof(lease_expires_ns) AS lease_expires_ns_type, "
        "typeof(generation) AS generation_type "
        f"FROM {namespace.claim_table} ORDER BY {identity_select}"
    ).fetchall()
    seen_digests: set[str] = set()
    generations: dict[tuple[Any, ...], int] = {}
    for claim in claims:
        key = tuple(claim[column] for column in namespace.identity_columns)
        if not _claim_row_is_valid(claim, namespace, lease_ns):
            raise BudgetAdmissionError(f"{namespace.label} claim value is invalid")
        if claim["reservation_digest"] in seen_digests:
            raise BudgetAdmissionError(f"{namespace.label} claim is duplicated")
        seen_digests.add(claim["reservation_digest"])
        reservation = reservations.get(key)
        if reservation is None or (
            reservation.reservation_digest != claim["reservation_digest"]
        ):
            raise BudgetAdmissionError(
                f"{namespace.label} claim reservation is missing or conflicting"
            )
        generations[key] = claim["generation"]
    return generations


def _claim_row_is_valid(
    claim: sqlite3.Row,
    namespace: _LedgerNamespace,
    lease_ns: int,
) -> bool:
    for column in namespace.identity_columns:
        expected_type = "integer" if column == "revision" else "text"
        value = claim[column]
        if claim[f"{column}_type"] != expected_type:
            return False
        if expected_type == "integer" and value < 0:
            return False
        if expected_type == "text" and not value:
            return False
    return bool(
        claim["reservation_digest_type"] == "text"
        and claim["owner_token_type"] == "text"
        and claim["claimed_at_ns_type"] == "integer"
        and claim["lease_expires_ns_type"] == "integer"
        and claim["generation_type"] == "integer"
        and claim["reservation_digest"]
        and claim["owner_token"]
        and claim["claimed_at_ns"] >= 0
        and claim["lease_expires_ns"] == claim["claimed_at_ns"] + lease_ns
        and claim["generation"] >= 0
        and claim["generation"] <= SQLITE_SIGNED_INTEGER_MAX
    )


def _validate_claim_input(owner_token: str, now_ns: int) -> None:
    if not isinstance(owner_token, str) or not owner_token:
        raise TypeError("owner_token must be a non-empty string")
    if type(now_ns) is not int or now_ns < 0:
        raise ValueError("now_ns must be a non-negative integer")
