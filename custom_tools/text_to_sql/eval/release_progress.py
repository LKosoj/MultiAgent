"""Transactional state machine for canonical public benchmark releases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence

from .release_materialization import ReleaseMaterializationMixin
from .release_progress_lifecycle import ReleaseProgressLifecycleMixin
from .release_progress_types import (
    CompletedLeg,
    PendingPostRepeatLeg,
    ReleasePhase,
    ReleaseProgress,
    ReleaseProgressError,
    SCHEMA_VERSION,
    active_leg_key,
    active_leg_matches_plan,
    awaiting_repair_leg,
)


__all__ = [
    "CompletedLeg",
    "PendingPostRepeatLeg",
    "ReleasePhase",
    "ReleaseProgress",
    "ReleaseProgressError",
    "ReleaseProgressStore",
    "active_leg_key",
    "active_leg_matches_plan",
    "awaiting_repair_leg",
]


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class ReleaseProgressStore(
    ReleaseProgressLifecycleMixin,
    ReleaseMaterializationMixin,
):
    """SQLite is the sole authority for release-bundle progress.

    The rollback journal plus synchronous=FULL avoids untracked WAL sidecars.
    Each mutation uses BEGIN IMMEDIATE and commits all related state together.
    """

    error_type = ReleaseProgressError

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ReleaseProgressError("release progress path is unsafe")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA journal_mode = DELETE")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, SCHEMA_VERSION):
                raise ReleaseProgressError("release progress schema is unsupported")
            connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS release_progress (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    bundle_id TEXT NOT NULL UNIQUE,
                    release_lock_digest TEXT NOT NULL,
                    release_plan_digest TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    active_benchmark TEXT,
                    active_repeat_ordinal INTEGER,
                    active_seed INTEGER,
                    in_flight_case_key TEXT,
                    candidate_sha256 TEXT,
                    decision_sha256 TEXT,
                    prefix_case_count INTEGER,
                    prefix_chain_sha256 TEXT,
                    invalid_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS leg_progress (
                    bundle_id TEXT NOT NULL,
                    benchmark TEXT NOT NULL,
                    repeat_ordinal INTEGER NOT NULL,
                    seed INTEGER NOT NULL,
                    run_manifest_sha256 TEXT,
                    case_manifest_sha256 TEXT,
                    ordered_case_keys_json BLOB,
                    status TEXT NOT NULL,
                    return_code INTEGER,
                    artifact_handshake_sha256 TEXT,
                    PRIMARY KEY (bundle_id, benchmark, repeat_ordinal),
                    FOREIGN KEY (bundle_id) REFERENCES release_progress(bundle_id)
                );
                CREATE TABLE IF NOT EXISTS case_commits (
                    bundle_id TEXT NOT NULL,
                    benchmark TEXT NOT NULL,
                    repeat_ordinal INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    case_key TEXT NOT NULL,
                    observation_json BLOB NOT NULL,
                    observation_sha256 TEXT NOT NULL,
                    history_receipt_json BLOB NOT NULL,
                    history_receipt_sha256 TEXT NOT NULL,
                    previous_chain_sha256 TEXT NOT NULL,
                    chain_sha256 TEXT NOT NULL,
                    PRIMARY KEY (bundle_id, benchmark, repeat_ordinal, ordinal),
                    UNIQUE (bundle_id, benchmark, repeat_ordinal, case_key),
                    FOREIGN KEY (bundle_id, benchmark, repeat_ordinal)
                        REFERENCES leg_progress(bundle_id, benchmark, repeat_ordinal)
                );
                CREATE TABLE IF NOT EXISTS terminal_artifacts (
                    bundle_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY (bundle_id, name),
                    FOREIGN KEY (bundle_id) REFERENCES release_progress(bundle_id)
                );
                CREATE TABLE IF NOT EXISTS governance_events (
                    bundle_id TEXT NOT NULL,
                    benchmark TEXT NOT NULL,
                    repeat_ordinal INTEGER NOT NULL,
                    event_kind TEXT NOT NULL,
                    completed_case_count INTEGER NOT NULL,
                    candidate_path TEXT NOT NULL,
                    candidate_sha256 TEXT NOT NULL,
                    decision_path TEXT NOT NULL,
                    decision_sha256 TEXT NOT NULL,
                    result_path TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL,
                    PRIMARY KEY (
                        bundle_id, benchmark, repeat_ordinal, event_kind,
                        completed_case_count
                    ),
                    FOREIGN KEY (bundle_id, benchmark, repeat_ordinal)
                        REFERENCES leg_progress(bundle_id, benchmark, repeat_ordinal)
                );
                PRAGMA user_version = {SCHEMA_VERSION};
                COMMIT;
                """
            )

    def _transaction(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def bind_bundle(
        self,
        *,
        bundle_id: str,
        release_lock_digest: str,
        release_plan_digest: str,
    ) -> None:
        with self._connect() as connection:
            self._transaction(connection)
            row = connection.execute(
                "SELECT * FROM release_progress WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO release_progress (
                        singleton, bundle_id, release_lock_digest,
                        release_plan_digest, phase
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (
                        bundle_id,
                        release_lock_digest,
                        release_plan_digest,
                        ReleasePhase.BETWEEN_LEGS.value,
                    ),
                )
            elif (
                row["bundle_id"] != bundle_id
                or row["release_lock_digest"] != release_lock_digest
                or row["release_plan_digest"] != release_plan_digest
            ):
                connection.rollback()
                raise ReleaseProgressError("release progress identity mismatch")
            connection.commit()

    def progress(self) -> ReleaseProgress:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM release_progress WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise ReleaseProgressError("release progress is not bound")
        try:
            phase = ReleasePhase(row["phase"])
        except ValueError as exc:
            raise ReleaseProgressError("release progress phase is invalid") from exc
        return ReleaseProgress(
            phase=phase,
            active_benchmark=row["active_benchmark"],
            active_repeat_ordinal=row["active_repeat_ordinal"],
            active_seed=row["active_seed"],
            in_flight_case_key=row["in_flight_case_key"],
            candidate_sha256=row["candidate_sha256"],
            decision_sha256=row["decision_sha256"],
            prefix_case_count=row["prefix_case_count"],
            invalid_reason=row["invalid_reason"],
        )

    def bundle_id(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT bundle_id FROM release_progress WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise ReleaseProgressError("release progress is not bound")
        return str(row[0])

    def start_leg(self, *, benchmark: str, repeat_ordinal: int, seed: int) -> None:
        with self._connect() as connection:
            self._transaction(connection)
            row = connection.execute(
                "SELECT phase, active_benchmark, active_repeat_ordinal, active_seed "
                "FROM release_progress WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise ReleaseProgressError("release progress is not bound")
            requested = (benchmark, repeat_ordinal, seed)
            active = (
                row["active_benchmark"], row["active_repeat_ordinal"], row["active_seed"]
            )
            if row["phase"] in {
                ReleasePhase.ACTIVE_LEG.value,
                ReleasePhase.CONTINUING_ACTIVE_LEG.value,
            }:
                if active != requested:
                    connection.rollback()
                    raise ReleaseProgressError("different release leg is already active")
                connection.commit()
                return
            if row["phase"] != ReleasePhase.BETWEEN_LEGS.value:
                connection.rollback()
                raise ReleaseProgressError("release phase cannot start a leg")
            existing_leg = connection.execute(
                """
                SELECT seed, status FROM leg_progress
                WHERE bundle_id = (
                    SELECT bundle_id FROM release_progress WHERE singleton = 1
                ) AND benchmark = ? AND repeat_ordinal = ?
                """,
                (benchmark, repeat_ordinal),
            ).fetchone()
            if existing_leg is not None and (
                existing_leg["status"] == "complete"
                or existing_leg["seed"] != seed
            ):
                connection.rollback()
                raise ReleaseProgressError("release leg cannot be run again")
            connection.execute(
                """
                INSERT INTO leg_progress (
                    bundle_id, benchmark, repeat_ordinal, seed, status
                )
                SELECT bundle_id, ?, ?, ?, 'active'
                FROM release_progress WHERE singleton = 1
                ON CONFLICT(bundle_id, benchmark, repeat_ordinal)
                DO UPDATE SET status = 'active'
                """,
                requested,
            )
            connection.execute(
                """
                UPDATE release_progress
                SET phase = ?, active_benchmark = ?, active_repeat_ordinal = ?,
                    active_seed = ?, invalid_reason = NULL
                WHERE singleton = 1
                """,
                (ReleasePhase.ACTIVE_LEG.value, *requested),
            )
            connection.commit()

    def bind_leg_inputs(
        self,
        *,
        benchmark: str,
        repeat_ordinal: int,
        run_manifest_sha256: str,
        case_manifest_sha256: str,
        ordered_case_keys: Sequence[str],
    ) -> None:
        encoded_keys = _json_bytes(list(ordered_case_keys))
        with self._connect() as connection:
            self._transaction(connection)
            row = connection.execute(
                """
                SELECT run_manifest_sha256, case_manifest_sha256,
                       ordered_case_keys_json
                FROM leg_progress
                WHERE bundle_id = (SELECT bundle_id FROM release_progress WHERE singleton = 1)
                  AND benchmark = ? AND repeat_ordinal = ?
                """,
                (benchmark, repeat_ordinal),
            ).fetchone()
            if row is None:
                raise ReleaseProgressError("active leg progress is missing")
            existing = (
                row["run_manifest_sha256"],
                row["case_manifest_sha256"],
                row["ordered_case_keys_json"],
            )
            requested = (run_manifest_sha256, case_manifest_sha256, encoded_keys)
            if existing[0] is not None and existing != requested:
                connection.rollback()
                raise ReleaseProgressError("active leg input binding mismatch")
            connection.execute(
                """
                UPDATE leg_progress
                SET run_manifest_sha256 = ?, case_manifest_sha256 = ?,
                    ordered_case_keys_json = ?
                WHERE bundle_id = (SELECT bundle_id FROM release_progress WHERE singleton = 1)
                  AND benchmark = ? AND repeat_ordinal = ?
                """,
                (*requested, benchmark, repeat_ordinal),
            )
            connection.commit()

    def authenticate_leg_inputs(
        self,
        *,
        benchmark: str,
        repeat_ordinal: int,
        run_manifest_sha256: str,
        case_manifest_sha256: str,
        ordered_case_keys: Sequence[str],
    ) -> None:
        encoded_keys = _json_bytes(list(ordered_case_keys))
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_manifest_sha256, case_manifest_sha256,
                       ordered_case_keys_json
                FROM leg_progress
                WHERE bundle_id = (SELECT bundle_id FROM release_progress WHERE singleton = 1)
                  AND benchmark = ? AND repeat_ordinal = ?
                """,
                (benchmark, repeat_ordinal),
            ).fetchone()
        if row is None or (
            row["run_manifest_sha256"],
            row["case_manifest_sha256"],
            row["ordered_case_keys_json"],
        ) != (run_manifest_sha256, case_manifest_sha256, encoded_keys):
            raise ReleaseProgressError("active leg inputs cannot be authenticated")

    def begin_case(
        self,
        *,
        benchmark: str,
        repeat_ordinal: int,
        case_key: str,
    ) -> None:
        with self._connect() as connection:
            self._transaction(connection)
            progress = connection.execute(
                "SELECT * FROM release_progress WHERE singleton = 1"
            ).fetchone()
            if progress is not None and progress["phase"] == ReleasePhase.INVALID.value:
                raise ReleaseProgressError(
                    progress["invalid_reason"] or "release progress is invalid"
                )
            if progress is None or progress["phase"] not in {
                ReleasePhase.ACTIVE_LEG.value,
                ReleasePhase.CONTINUING_ACTIVE_LEG.value,
            }:
                raise ReleaseProgressError("release phase cannot start a case")
            if (
                progress["active_benchmark"] != benchmark
                or progress["active_repeat_ordinal"] != repeat_ordinal
                or progress["in_flight_case_key"] is not None
            ):
                raise ReleaseProgressError("release case admission is invalid")
            duplicate = connection.execute(
                """
                SELECT 1 FROM case_commits
                WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                  AND case_key = ?
                """,
                (progress["bundle_id"], benchmark, repeat_ordinal, case_key),
            ).fetchone()
            if duplicate is not None:
                raise ReleaseProgressError("committed release case cannot run again")
            connection.execute(
                "UPDATE release_progress SET in_flight_case_key = ? WHERE singleton = 1",
                (case_key,),
            )
            connection.commit()

    def commit_case(
        self,
        *,
        benchmark: str,
        repeat_ordinal: int,
        ordinal: int,
        case_key: str,
        observation: Mapping[str, object],
        history_receipt: Mapping[str, object],
    ) -> None:
        observation_bytes = _json_bytes(observation)
        receipt_bytes = _json_bytes(history_receipt)
        with self._connect() as connection:
            self._transaction(connection)
            progress = connection.execute(
                "SELECT * FROM release_progress WHERE singleton = 1"
            ).fetchone()
            if progress is None or progress["in_flight_case_key"] != case_key:
                raise ReleaseProgressError("release case result has no matching admission")
            leg = connection.execute(
                """
                SELECT ordered_case_keys_json FROM leg_progress
                WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                """,
                (progress["bundle_id"], benchmark, repeat_ordinal),
            ).fetchone()
            if leg is None or leg["ordered_case_keys_json"] is None:
                raise ReleaseProgressError("release leg inputs are not bound")
            ordered_keys = json.loads(bytes(leg["ordered_case_keys_json"]))
            committed = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM case_commits
                    WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                    """,
                    (progress["bundle_id"], benchmark, repeat_ordinal),
                ).fetchone()[0]
            )
            if (
                committed >= len(ordered_keys)
                or ordinal != committed
                or ordered_keys[committed] != case_key
            ):
                raise ReleaseProgressError("release case is not the canonical next case")
            if observation.get("case_key") != case_key:
                raise ReleaseProgressError("release observation case identity mismatch")
            if history_receipt.get("case_key") != case_key:
                raise ReleaseProgressError("release history receipt identity mismatch")
            preexisting = history_receipt.get("preexisting_history_items")
            if preexisting != 0:
                self._invalidate_transaction(
                    connection,
                    "release history receipt is not empty",
                )
            previous_row = connection.execute(
                """
                SELECT chain_sha256 FROM case_commits
                WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                ORDER BY ordinal DESC LIMIT 1
                """,
                (progress["bundle_id"], benchmark, repeat_ordinal),
            ).fetchone()
            previous_chain = previous_row[0] if previous_row else "sha256:" + "0" * 64
            observation_digest = _sha256(observation_bytes)
            receipt_digest = _sha256(receipt_bytes)
            chain_digest = _sha256(
                (previous_chain + observation_digest + receipt_digest).encode("ascii")
            )
            connection.execute(
                """
                INSERT INTO case_commits (
                    bundle_id, benchmark, repeat_ordinal, ordinal, case_key,
                    observation_json, observation_sha256, history_receipt_json,
                    history_receipt_sha256, previous_chain_sha256, chain_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    progress["bundle_id"], benchmark, repeat_ordinal, ordinal,
                    case_key, observation_bytes, observation_digest, receipt_bytes,
                    receipt_digest, previous_chain, chain_digest,
                ),
            )
            connection.execute(
                "UPDATE release_progress SET in_flight_case_key = NULL WHERE singleton = 1"
            )
            connection.commit()

    @staticmethod
    def _invalidate_transaction(
        connection: sqlite3.Connection,
        reason: str,
    ) -> None:
        connection.execute(
            "UPDATE release_progress SET phase = ?, invalid_reason = ? WHERE singleton = 1",
            (ReleasePhase.INVALID.value, reason),
        )
        connection.commit()
        raise ReleaseProgressError(reason)

    def _authenticate_commits_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        benchmark: str,
        repeat_ordinal: int,
    ) -> tuple[
        list[tuple[dict[str, object], dict[str, object]]],
        list[sqlite3.Row],
    ]:
        progress = connection.execute(
            "SELECT * FROM release_progress WHERE singleton = 1"
        ).fetchone()
        if progress is None:
            connection.rollback()
            raise ReleaseProgressError("release progress is not bound")
        if progress["phase"] == ReleasePhase.INVALID.value:
            connection.rollback()
            raise ReleaseProgressError(
                progress["invalid_reason"] or "release is invalid"
            )
        if progress["in_flight_case_key"] is not None:
            self._invalidate_transaction(
                connection,
                "ambiguous in-flight case requires a new release experiment",
            )
        leg = connection.execute(
            """
            SELECT ordered_case_keys_json FROM leg_progress
            WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
            """,
            (progress["bundle_id"], benchmark, repeat_ordinal),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT * FROM case_commits
            WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
            ORDER BY ordinal
            """,
            (progress["bundle_id"], benchmark, repeat_ordinal),
        ).fetchall()
        if leg is None or leg["ordered_case_keys_json"] is None:
            self._invalidate_transaction(connection, "release leg inputs are not bound")
        try:
            ordered_keys = json.loads(bytes(leg["ordered_case_keys_json"]))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            self._invalidate_transaction(
                connection, "committed release case inventory is invalid"
            )
        if (
            not isinstance(ordered_keys, list)
            or any(not isinstance(item, str) for item in ordered_keys)
            or len(set(ordered_keys)) != len(ordered_keys)
        ):
            self._invalidate_transaction(
                connection, "committed release case inventory is invalid"
            )
        results: list[tuple[dict[str, object], dict[str, object]]] = []
        previous = "sha256:" + "0" * 64
        for expected_ordinal, row in enumerate(rows):
            observation_bytes = bytes(row["observation_json"])
            receipt_bytes = bytes(row["history_receipt_json"])
            observation_digest = _sha256(observation_bytes)
            receipt_digest = _sha256(receipt_bytes)
            chain = _sha256(
                (previous + observation_digest + receipt_digest).encode("ascii")
            )
            if (
                row["ordinal"] != expected_ordinal
                or expected_ordinal >= len(ordered_keys)
                or row["case_key"] != ordered_keys[expected_ordinal]
                or row["observation_sha256"] != observation_digest
                or row["history_receipt_sha256"] != receipt_digest
                or row["previous_chain_sha256"] != previous
                or row["chain_sha256"] != chain
            ):
                self._invalidate_transaction(
                    connection, "committed release case chain is invalid"
                )
            try:
                observation = json.loads(observation_bytes)
                receipt = json.loads(receipt_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._invalidate_transaction(
                    connection, "committed release case JSON is invalid"
                )
            if (
                not isinstance(observation, dict)
                or observation.get("case_key") != row["case_key"]
            ):
                self._invalidate_transaction(
                    connection, "committed observation key is invalid"
                )
            if (
                not isinstance(receipt, dict)
                or receipt.get("case_key") != row["case_key"]
                or receipt.get("preexisting_history_items") != 0
            ):
                self._invalidate_transaction(
                    connection, "committed history receipt is invalid"
                )
            results.append((observation, receipt))
            previous = chain
        return results, rows

    def authenticate_commits(
        self, *, benchmark: str, repeat_ordinal: int
    ) -> list[tuple[dict[str, object], dict[str, object]]]:
        with self._connect() as connection:
            self._transaction(connection)
            results, _rows = self._authenticate_commits_in_transaction(
                connection,
                benchmark=benchmark,
                repeat_ordinal=repeat_ordinal,
            )
            connection.commit()
        return results

    def authenticate_candidate_prefix(
        self,
        *,
        benchmark: str,
        repeat_ordinal: int,
        candidate_sha256: str,
        completed_case_keys: Sequence[str],
        completed_case_count: int,
        observations_sha256: str,
    ) -> None:
        """Bind a sealed candidate to the exact authenticated SQLite prefix."""

        with self._connect() as connection:
            self._transaction(connection)
            results, rows = self._authenticate_commits_in_transaction(
                connection,
                benchmark=benchmark,
                repeat_ordinal=repeat_ordinal,
            )
            progress = connection.execute(
                "SELECT * FROM release_progress WHERE singleton = 1"
            ).fetchone()
            valid_count = (
                isinstance(completed_case_count, int)
                and not isinstance(completed_case_count, bool)
                and 0 < completed_case_count <= len(rows)
            )
            prefix_results = results[:completed_case_count] if valid_count else []
            prefix_rows = rows[:completed_case_count] if valid_count else []
            keys = [
                str(observation["case_key"])
                for observation, _receipt in prefix_results
            ]
            observation_bytes = b"".join(
                bytes(row["observation_json"]) for row in prefix_rows
            )
            prefix_chain = (
                prefix_rows[-1]["chain_sha256"] if prefix_rows else None
            )
            stored_candidate = progress["candidate_sha256"]
            stored_prefix_count = progress["prefix_case_count"]
            stored_prefix_chain = progress["prefix_chain_sha256"]
            stored_binding_matches = (
                (
                    progress["phase"] == ReleasePhase.ACTIVE_LEG.value
                    and stored_candidate is None
                    and stored_prefix_count is None
                    and stored_prefix_chain is None
                    and completed_case_count == len(rows)
                )
                or (
                    stored_candidate == candidate_sha256
                    and stored_prefix_count == completed_case_count
                    and stored_prefix_chain == prefix_chain
                )
            )
            if (
                not isinstance(candidate_sha256, str)
                or not candidate_sha256.startswith("sha256:")
                or not valid_count
                or keys != list(completed_case_keys)
                or len(keys) != completed_case_count
                or _sha256(observation_bytes) != observations_sha256
                or not stored_binding_matches
            ):
                self._invalidate_transaction(
                    connection,
                    "committed prefix does not match sealed candidate",
                )
            connection.commit()

    def committed_case_keys(self, *, benchmark: str, repeat_ordinal: int) -> list[str]:
        return [
            str(observation["case_key"])
            for observation, _receipt in self.authenticate_commits(
                benchmark=benchmark, repeat_ordinal=repeat_ordinal
            )
        ]

    def observation_bytes(self, *, benchmark: str, repeat_ordinal: int) -> bytes:
        return b"".join(
            _json_bytes(observation)
            for observation, _receipt in self.authenticate_commits(
                benchmark=benchmark, repeat_ordinal=repeat_ordinal
            )
        )

    def history_receipts(
        self, *, benchmark: str, repeat_ordinal: int
    ) -> list[dict[str, object]]:
        return [
            receipt
            for _observation, receipt in self.authenticate_commits(
                benchmark=benchmark, repeat_ordinal=repeat_ordinal
            )
        ]
    def pause_for_candidate(
        self,
        *,
        candidate_sha256: str,
        prefix_case_count: int,
    ) -> None:
        with self._connect() as connection:
            self._transaction(connection)
            progress = connection.execute(
                "SELECT * FROM release_progress WHERE singleton = 1"
            ).fetchone()
            if progress is None or progress["phase"] not in {
                ReleasePhase.ACTIVE_LEG.value,
                ReleasePhase.CONTINUING_ACTIVE_LEG.value,
            }:
                raise ReleaseProgressError("release phase cannot pause for candidate")
            chain_row = connection.execute(
                """
                SELECT chain_sha256 FROM case_commits
                WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                ORDER BY ordinal DESC LIMIT 1
                """,
                (
                    progress["bundle_id"], progress["active_benchmark"],
                    progress["active_repeat_ordinal"],
                ),
            ).fetchone()
            committed_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM case_commits
                    WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                    """,
                    (
                        progress["bundle_id"], progress["active_benchmark"],
                        progress["active_repeat_ordinal"],
                    ),
                ).fetchone()[0]
            )
            if (
                chain_row is None
                or prefix_case_count <= 0
                or prefix_case_count != committed_count
            ):
                raise ReleaseProgressError("candidate has no committed prefix")
            connection.execute(
                """
                UPDATE release_progress
                SET phase = ?, candidate_sha256 = ?, prefix_case_count = ?,
                    prefix_chain_sha256 = ?
                WHERE singleton = 1
                """,
                (
                    ReleasePhase.AWAITING_REPAIR_DECISION.value,
                    candidate_sha256, prefix_case_count, chain_row[0],
                ),
            )
            connection.commit()

    def transition(
        self,
        *,
        expected: Sequence[ReleasePhase],
        target: ReleasePhase,
        candidate_sha256: str | None = None,
        decision_sha256: str | None = None,
        prefix_case_count: int | None = None,
    ) -> None:
        with self._connect() as connection:
            self._transaction(connection)
            row = connection.execute(
                "SELECT phase FROM release_progress WHERE singleton = 1"
            ).fetchone()
            if row is None or row[0] not in {item.value for item in expected}:
                raise ReleaseProgressError("release state transition is invalid")
            connection.execute(
                """
                UPDATE release_progress SET phase = ?,
                    candidate_sha256 = COALESCE(?, candidate_sha256),
                    decision_sha256 = COALESCE(?, decision_sha256),
                    prefix_case_count = COALESCE(?, prefix_case_count)
                WHERE singleton = 1
                """,
                (
                    target.value,
                    candidate_sha256,
                    decision_sha256,
                    prefix_case_count,
                ),
            )
            connection.commit()

    def _complete_active_leg_row(
        self, connection: sqlite3.Connection
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM release_progress WHERE singleton = 1"
        ).fetchone()
        if row is None or row["phase"] not in {
            ReleasePhase.ACTIVE_LEG.value,
            ReleasePhase.CONTINUING_ACTIVE_LEG.value,
        } or row["in_flight_case_key"] is not None:
            raise ReleaseProgressError("release leg cannot complete")
        results, _rows = self._authenticate_commits_in_transaction(
            connection,
            benchmark=str(row["active_benchmark"]),
            repeat_ordinal=int(row["active_repeat_ordinal"]),
        )
        leg = connection.execute(
            """
            SELECT ordered_case_keys_json FROM leg_progress
            WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
            """,
            (
                row["bundle_id"], row["active_benchmark"],
                row["active_repeat_ordinal"],
            ),
        ).fetchone()
        if leg is None or leg["ordered_case_keys_json"] is None:
            raise ReleaseProgressError("release leg inputs are not bound")
        if len(results) != len(json.loads(bytes(leg["ordered_case_keys_json"]))):
            raise ReleaseProgressError("release leg case inventory is incomplete")
        return row

    def mark_invalid(self, reason: str) -> None:
        with self._connect() as connection:
            self._transaction(connection)
            connection.execute(
                "UPDATE release_progress SET phase = ?, invalid_reason = ? WHERE singleton = 1",
                (ReleasePhase.INVALID.value, reason),
            )
            connection.commit()
