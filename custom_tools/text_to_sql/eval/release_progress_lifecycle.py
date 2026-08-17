"""Governance and finalization operations for release progress."""

from __future__ import annotations

from typing import Mapping

from .release_governance import governance_event_paths
from .release_progress_types import (
    CompletedLeg,
    PendingPostRepeatLeg,
    ReleasePhase,
    ReleaseProgressError,
)


class ReleaseProgressLifecycleMixin:
    def continue_active_leg(
        self, *, candidate_sha256: str, decision_sha256: str
    ) -> None:
        with self._connect() as connection:
            self._transaction(connection)
            row = connection.execute(
                "SELECT * FROM release_progress WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise ReleaseProgressError("continued prefix authority is invalid")
            governance = connection.execute(
                """
                SELECT candidate_sha256, decision_sha256, result_sha256
                FROM governance_events
                WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                  AND event_kind = 'mid_repeat' AND completed_case_count = ?
                """,
                (
                    row["bundle_id"], row["active_benchmark"],
                    row["active_repeat_ordinal"],
                    row["prefix_case_count"],
                ),
            ).fetchone()
            if (
                row["phase"] != ReleasePhase.AWAITING_REPAIR_DECISION.value
                or row["candidate_sha256"] != candidate_sha256
                or row["decision_sha256"] != decision_sha256
                or not isinstance(row["prefix_case_count"], int)
                or not row["prefix_chain_sha256"]
                or governance is None
                or governance["candidate_sha256"] != candidate_sha256
                or governance["decision_sha256"] != decision_sha256
                or not governance["result_sha256"]
            ):
                raise ReleaseProgressError("continued prefix authority is invalid")
            connection.execute(
                "UPDATE release_progress SET phase = ? WHERE singleton = 1",
                (ReleasePhase.CONTINUING_ACTIVE_LEG.value,),
            )
            connection.commit()

    def register_governance_event(self, event: Mapping[str, object]) -> None:
        required = {
            "event_kind", "benchmark", "repeat_ordinal", "completed_case_count",
            "candidate_path",
            "candidate_sha256", "decision_path", "decision_sha256",
            "result_path", "result_sha256",
        }
        if set(event) != required:
            raise ReleaseProgressError("release governance event schema is invalid")
        event_kind = event.get("event_kind")
        benchmark = event.get("benchmark")
        repeat_ordinal = event.get("repeat_ordinal")
        completed_case_count = event.get("completed_case_count")
        if (
            not isinstance(event_kind, str)
            or not isinstance(benchmark, str)
            or not isinstance(repeat_ordinal, int)
            or isinstance(repeat_ordinal, bool)
            or not isinstance(completed_case_count, int)
            or isinstance(completed_case_count, bool)
            or completed_case_count <= 0
        ):
            raise ReleaseProgressError("release governance event identity is invalid")
        try:
            expected_paths = governance_event_paths(event_kind, completed_case_count)
        except ValueError as exc:
            raise ReleaseProgressError(str(exc)) from exc
        if any(event.get(name) != value for name, value in expected_paths.items()):
            raise ReleaseProgressError("release governance event paths are invalid")
        digest_fields = ("candidate_sha256", "decision_sha256", "result_sha256")
        if any(
            not isinstance(event.get(name), str)
            or not str(event[name]).startswith("sha256:")
            for name in digest_fields
        ):
            raise ReleaseProgressError("release governance event digests are invalid")
        values = (
            event_kind, benchmark, repeat_ordinal, completed_case_count,
            expected_paths["candidate_path"], str(event["candidate_sha256"]),
            expected_paths["decision_path"], str(event["decision_sha256"]),
            expected_paths["result_path"], str(event["result_sha256"]),
        )
        with self._connect() as connection:
            self._transaction(connection)
            progress = connection.execute(
                "SELECT * FROM release_progress WHERE singleton = 1"
            ).fetchone()
            valid_phase = {
                "mid_repeat": {
                    ReleasePhase.AWAITING_REPAIR_DECISION.value,
                    ReleasePhase.CONTINUING_ACTIVE_LEG.value,
                },
                "post_repeat": {
                    ReleasePhase.AWAITING_POST_REPEAT_REPAIR_DECISION.value,
                },
            }[event_kind]
            if (
                progress is None
                or progress["phase"] not in valid_phase
                or progress["active_benchmark"] != benchmark
                or progress["active_repeat_ordinal"] != repeat_ordinal
                or progress["candidate_sha256"] != event["candidate_sha256"]
            ):
                connection.rollback()
                raise ReleaseProgressError("release governance authority is invalid")
            existing = connection.execute(
                """
                SELECT event_kind, benchmark, repeat_ordinal, completed_case_count,
                       candidate_path,
                       candidate_sha256, decision_path, decision_sha256,
                       result_path, result_sha256
                FROM governance_events
                WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                  AND event_kind = ? AND completed_case_count = ?
                """,
                (
                    progress["bundle_id"], benchmark, repeat_ordinal, event_kind,
                    completed_case_count,
                ),
            ).fetchone()
            if existing is not None and tuple(existing) != values:
                self._invalidate_transaction(
                    connection, "registered release governance event changed"
                )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO governance_events (
                        bundle_id, event_kind, benchmark, repeat_ordinal,
                        completed_case_count,
                        candidate_path, candidate_sha256, decision_path,
                        decision_sha256, result_path, result_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (progress["bundle_id"], *values),
                )
            connection.execute(
                "UPDATE release_progress SET decision_sha256 = ? WHERE singleton = 1",
                (event["decision_sha256"],),
            )
            connection.commit()

    def governance_events(
        self, *, benchmark: str, repeat_ordinal: int
    ) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_kind, benchmark, repeat_ordinal, completed_case_count,
                       candidate_path,
                       candidate_sha256, decision_path, decision_sha256,
                       result_path, result_sha256
                FROM governance_events
                WHERE bundle_id = (
                    SELECT bundle_id FROM release_progress WHERE singleton = 1
                ) AND benchmark = ? AND repeat_ordinal = ?
                ORDER BY completed_case_count, CASE event_kind
                    WHEN 'mid_repeat' THEN 0 WHEN 'post_repeat' THEN 1 ELSE 2 END
                """,
                (benchmark, repeat_ordinal),
            ).fetchall()
        events = [dict(row) for row in rows]
        if any(
            event.get("event_kind") not in {"mid_repeat", "post_repeat"}
            for event in events
        ):
            raise ReleaseProgressError("release governance inventory is invalid")
        return events

    def seal_terminal_state(
        self,
        *,
        expected: ReleasePhase,
        target: ReleasePhase,
        artifact_sha256: Mapping[str, str],
    ) -> None:
        if target not in {
            ReleasePhase.DIAGNOSTIC_PARTIAL_STOP,
            ReleasePhase.DIAGNOSTIC_POST_REPEAT_STOP,
            ReleasePhase.COMPLETE,
        }:
            raise ReleaseProgressError("terminal release phase is invalid")
        if not artifact_sha256 or any(
            not isinstance(name, str)
            or not name
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            for name, digest in artifact_sha256.items()
        ):
            raise ReleaseProgressError("terminal artifact inventory is invalid")
        with self._connect() as connection:
            self._transaction(connection)
            row = connection.execute(
                "SELECT bundle_id, phase FROM release_progress WHERE singleton = 1"
            ).fetchone()
            if row is None or row["phase"] != expected.value:
                raise ReleaseProgressError("terminal release transition is invalid")
            for name, digest in artifact_sha256.items():
                existing = connection.execute(
                    "SELECT sha256 FROM terminal_artifacts WHERE bundle_id = ? AND name = ?",
                    (row["bundle_id"], name),
                ).fetchone()
                if existing is not None and existing[0] != digest:
                    raise ReleaseProgressError("terminal artifact digest changed")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO terminal_artifacts (bundle_id, name, sha256)
                    VALUES (?, ?, ?)
                    """,
                    (row["bundle_id"], name, digest),
                )
            connection.execute(
                "UPDATE release_progress SET phase = ? WHERE singleton = 1",
                (target.value,),
            )
            connection.commit()

    def terminal_artifact_digests(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT name, sha256 FROM terminal_artifacts
                WHERE bundle_id = (
                    SELECT bundle_id FROM release_progress WHERE singleton = 1
                ) ORDER BY name
                """
            ).fetchall()
        digests = {str(row["name"]): str(row["sha256"]) for row in rows}
        if not digests:
            raise ReleaseProgressError("terminal artifact inventory is missing")
        return digests

    def defer_leg_for_post_repeat(self, *, return_code: int) -> None:
        if not isinstance(return_code, int) or isinstance(return_code, bool):
            raise ReleaseProgressError("release leg return code is invalid")
        with self._connect() as connection:
            self._transaction(connection)
            row = self._complete_active_leg_row(connection)
            connection.execute(
                """
                UPDATE leg_progress SET status = 'awaiting_post_repeat',
                    return_code = ?, artifact_handshake_sha256 = NULL
                WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                """,
                (
                    return_code, row["bundle_id"], row["active_benchmark"],
                    row["active_repeat_ordinal"],
                ),
            )
            connection.execute(
                """
                UPDATE release_progress SET phase = ?, candidate_sha256 = NULL,
                    decision_sha256 = NULL, prefix_case_count = NULL,
                    prefix_chain_sha256 = NULL WHERE singleton = 1
                """,
                (ReleasePhase.AWAITING_POST_REPEAT_EVALUATION.value,),
            )
            connection.commit()

    def complete_leg(
        self,
        *,
        return_code: int,
        artifact_handshake_sha256: str,
        awaiting_post_repeat_evaluation: bool = False,
    ) -> None:
        if awaiting_post_repeat_evaluation:
            raise ReleaseProgressError(
                "post-repeat release leg must defer its final handshake"
            )
        if not isinstance(return_code, int) or isinstance(return_code, bool):
            raise ReleaseProgressError("release leg return code is invalid")
        if not artifact_handshake_sha256.startswith("sha256:"):
            raise ReleaseProgressError("release leg handshake digest is invalid")
        with self._connect() as connection:
            self._transaction(connection)
            row = self._complete_active_leg_row(connection)
            connection.execute(
                """
                UPDATE leg_progress SET status = 'complete', return_code = ?,
                    artifact_handshake_sha256 = ?
                WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                """,
                (
                    return_code, artifact_handshake_sha256, row["bundle_id"],
                    row["active_benchmark"], row["active_repeat_ordinal"],
                ),
            )
            self._clear_active_leg(connection)
            connection.commit()

    def complete_post_repeat_evaluation(
        self, *, artifact_handshake_sha256: str
    ) -> None:
        if not artifact_handshake_sha256.startswith("sha256:"):
            raise ReleaseProgressError("release leg handshake digest is invalid")
        with self._connect() as connection:
            self._transaction(connection)
            row = connection.execute(
                "SELECT * FROM release_progress WHERE singleton = 1"
            ).fetchone()
            if row is None or row["phase"] not in {
                ReleasePhase.AWAITING_POST_REPEAT_EVALUATION.value,
                ReleasePhase.AWAITING_POST_REPEAT_REPAIR_DECISION.value,
            }:
                raise ReleaseProgressError("post-repeat evaluation completion is invalid")
            leg = connection.execute(
                """
                SELECT status, return_code FROM leg_progress
                WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                """,
                (
                    row["bundle_id"], row["active_benchmark"],
                    row["active_repeat_ordinal"],
                ),
            ).fetchone()
            if leg is None or leg["status"] != "awaiting_post_repeat" or (
                leg["return_code"] is None
            ):
                raise ReleaseProgressError("post-repeat release leg is incomplete")
            if row["phase"] == ReleasePhase.AWAITING_POST_REPEAT_REPAIR_DECISION.value:
                governance = connection.execute(
                    """
                    SELECT candidate_sha256, decision_sha256, result_sha256
                    FROM governance_events
                    WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                      AND event_kind = 'post_repeat'
                    """,
                    (
                        row["bundle_id"], row["active_benchmark"],
                        row["active_repeat_ordinal"],
                    ),
                ).fetchone()
                if (
                    governance is None
                    or governance["candidate_sha256"] != row["candidate_sha256"]
                    or governance["decision_sha256"] != row["decision_sha256"]
                    or not governance["result_sha256"]
                ):
                    raise ReleaseProgressError(
                        "post-repeat CONTINUE governance is incomplete"
                    )
            connection.execute(
                """
                UPDATE leg_progress SET status = 'complete',
                    artifact_handshake_sha256 = ?
                WHERE bundle_id = ? AND benchmark = ? AND repeat_ordinal = ?
                """,
                (
                    artifact_handshake_sha256, row["bundle_id"],
                    row["active_benchmark"], row["active_repeat_ordinal"],
                ),
            )
            self._clear_active_leg(connection)
            connection.commit()

    @staticmethod
    def _clear_active_leg(connection: object) -> None:
        connection.execute(
            """
            UPDATE release_progress SET phase = ?, active_benchmark = NULL,
                active_repeat_ordinal = NULL, active_seed = NULL,
                candidate_sha256 = NULL, decision_sha256 = NULL,
                prefix_case_count = NULL, prefix_chain_sha256 = NULL
            WHERE singleton = 1
            """,
            (ReleasePhase.BETWEEN_LEGS.value,),
        )

    def completed_legs(self) -> list[CompletedLeg]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT benchmark, repeat_ordinal, seed, return_code,
                       artifact_handshake_sha256
                FROM leg_progress
                WHERE bundle_id = (
                    SELECT bundle_id FROM release_progress WHERE singleton = 1
                ) AND status = 'complete' ORDER BY rowid
                """
            ).fetchall()
        return [
            CompletedLeg(
                benchmark=str(row["benchmark"]),
                repeat_ordinal=int(row["repeat_ordinal"]),
                seed=int(row["seed"]),
                return_code=int(row["return_code"]),
                artifact_handshake_sha256=str(row["artifact_handshake_sha256"]),
            )
            for row in rows
        ]

    def pending_post_repeat_leg(self) -> PendingPostRepeatLeg:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT benchmark, repeat_ordinal, seed, return_code
                FROM leg_progress
                WHERE bundle_id = (
                    SELECT bundle_id FROM release_progress WHERE singleton = 1
                ) AND status = 'awaiting_post_repeat'
                  AND benchmark = (
                    SELECT active_benchmark FROM release_progress WHERE singleton = 1
                  ) AND repeat_ordinal = (
                    SELECT active_repeat_ordinal FROM release_progress WHERE singleton = 1
                  )
                """
            ).fetchone()
        if row is None or row["return_code"] is None:
            raise ReleaseProgressError("post-repeat pending leg is missing")
        return PendingPostRepeatLeg(
            benchmark=str(row["benchmark"]),
            repeat_ordinal=int(row["repeat_ordinal"]),
            seed=int(row["seed"]),
            return_code=int(row["return_code"]),
        )
