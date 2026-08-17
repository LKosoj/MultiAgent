"""Coordinator facade for canonical public benchmark releases."""

from __future__ import annotations

from typing import Mapping, Sequence

from .release_progress import (
    CompletedLeg,
    PendingPostRepeatLeg,
    ReleasePhase,
    ReleaseProgress,
    ReleaseProgressError,
    ReleaseProgressStore,
    active_leg_key,
    active_leg_matches_plan,
    awaiting_repair_leg,
)


__all__ = [
    "CompletedLeg",
    "PendingPostRepeatLeg",
    "ReleaseBundleCoordinator",
    "ReleasePhase",
    "ReleaseProgress",
    "ReleaseProgressError",
    "ReleaseProgressStore",
    "active_leg_key",
    "active_leg_matches_plan",
    "awaiting_repair_leg",
]


class ReleaseBundleCoordinator:
    """Derive public bundle state exclusively from committed SQLite progress."""

    def __init__(
        self,
        store: ReleaseProgressStore,
        *,
        bundle_id: str,
        release_plan: Sequence[Mapping[str, object]],
    ) -> None:
        self.store = store
        self.bundle_id = bundle_id
        self.release_plan = [dict(item) for item in release_plan]

    @staticmethod
    def _completed_payload(completed: Sequence[CompletedLeg]) -> list[dict[str, object]]:
        return [
            {
                "benchmark": item.benchmark,
                "repeat_ordinal": item.repeat_ordinal,
                "seed": item.seed,
                "return_code": item.return_code,
                "artifact_handshake_sha256": item.artifact_handshake_sha256,
            }
            for item in completed
        ]

    def reconcile_public_state(
        self, existing: Mapping[str, object]
    ) -> dict[str, object]:
        if (
            existing.get("record_kind")
            != "text2sql_public_benchmark_bundle_state"
            or existing.get("bundle_id") != self.bundle_id
            or existing.get("release_plan") != self.release_plan
            or self.store.bundle_id() != self.bundle_id
        ):
            raise ReleaseProgressError("bundle state identity is invalid")
        progress = self.store.progress()
        if progress.phase is ReleasePhase.INVALID:
            raise ReleaseProgressError(
                progress.invalid_reason or "release progress is invalid"
            )
        if progress.in_flight_case_key is not None:
            self.store.mark_invalid(
                "ambiguous in-flight case requires a new release experiment"
            )
            raise ReleaseProgressError(
                "ambiguous in-flight case requires a new release experiment"
            )
        completed = self._completed_payload(self.store.completed_legs())
        completed_identity = [
            (item["benchmark"], item["repeat_ordinal"], item["seed"])
            for item in completed
        ]
        planned_identity = [
            (item.get("benchmark"), item.get("repeat_ordinal"), item.get("seed"))
            for item in self.release_plan[: len(completed)]
        ]
        if completed_identity != planned_identity:
            raise ReleaseProgressError(
                "committed release legs are not a canonical plan prefix"
            )
        existing_completed = existing.get("completed_legs")
        if not isinstance(existing_completed, list) or (
            existing_completed != completed[: len(existing_completed)]
        ):
            raise ReleaseProgressError(
                "bundle state completed legs are not a committed prefix"
            )
        state: dict[str, object] = {
            "schema_version": 1,
            "record_kind": "text2sql_public_benchmark_bundle_state",
            "bundle_id": self.bundle_id,
            "release_plan": self.release_plan,
            "active_leg": None,
            "completed_legs": completed,
            "status": "running",
        }
        active: dict[str, object] | None = None
        if progress.active_benchmark is not None:
            active = {
                "benchmark": progress.active_benchmark,
                "repeat_ordinal": progress.active_repeat_ordinal,
                "seed": progress.active_seed,
            }
        if progress.phase in {
            ReleasePhase.AWAITING_REPAIR_DECISION,
            ReleasePhase.FINALIZING_PARTIAL_STOP,
            ReleasePhase.DIAGNOSTIC_PARTIAL_STOP,
        }:
            if active is None or not progress.candidate_sha256:
                raise ReleaseProgressError("partial stop authority is incomplete")
            active["candidate_sha256"] = progress.candidate_sha256
            state["active_leg"] = active
            state["status"] = (
                "diagnostic_early_stop"
                if progress.phase is ReleasePhase.DIAGNOSTIC_PARTIAL_STOP
                else "AWAITING_REPAIR_DECISION"
            )
        elif progress.phase in {
            ReleasePhase.AWAITING_POST_REPEAT_EVALUATION,
            ReleasePhase.AWAITING_POST_REPEAT_REPAIR_DECISION,
            ReleasePhase.FINALIZING_POST_REPEAT_STOP,
            ReleasePhase.DIAGNOSTIC_POST_REPEAT_STOP,
        }:
            if active is None:
                raise ReleaseProgressError("post-repeat authority is incomplete")
            pending = self.store.pending_post_repeat_leg()
            if (
                pending.benchmark != active["benchmark"]
                or pending.repeat_ordinal != active["repeat_ordinal"]
                or pending.seed != active["seed"]
            ):
                raise ReleaseProgressError("post-repeat pending leg is invalid")
            evaluation_leg = {
                **active,
                "return_code": pending.return_code,
            }
            if progress.candidate_sha256 is not None:
                evaluation_leg["candidate_sha256"] = progress.candidate_sha256
            state["evaluation_leg"] = evaluation_leg
            if progress.phase is ReleasePhase.AWAITING_POST_REPEAT_EVALUATION:
                state["status"] = "AWAITING_POST_REPEAT_EVALUATION"
            elif progress.phase is ReleasePhase.DIAGNOSTIC_POST_REPEAT_STOP:
                state["status"] = "diagnostic_post_repeat_stop"
            else:
                state["status"] = "AWAITING_POST_REPEAT_REPAIR_DECISION"
        elif progress.phase in {
            ReleasePhase.ACTIVE_LEG,
            ReleasePhase.CONTINUING_ACTIVE_LEG,
        }:
            if active is None:
                raise ReleaseProgressError("active release leg is incomplete")
            state["active_leg"] = active
            if progress.phase is ReleasePhase.CONTINUING_ACTIVE_LEG:
                state["resume_active_leg"] = True
        elif progress.phase is ReleasePhase.COMPLETE:
            state["status"] = "complete"
            state["return_code"] = int(
                any(item["return_code"] != 0 for item in completed)
            )
        elif progress.phase is not ReleasePhase.BETWEEN_LEGS:
            raise ReleaseProgressError("release progress phase is unsupported")
        return state
