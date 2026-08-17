"""Thin runner adapter for transactional release-leg progress."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .release_progress import ReleasePhase, ReleaseProgressStore
from .sandbox import SandboxError


@dataclass
class ReleaseLegProgress:
    store: ReleaseProgressStore | None
    benchmark: str
    repeat_ordinal: int
    seed: int
    initial_phase: ReleasePhase | None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "ReleaseLegProgress":
        progress_path = getattr(args, "release_progress_path", None)
        store = (
            ReleaseProgressStore(progress_path)
            if isinstance(progress_path, Path)
            else None
        )
        if store is None and getattr(args, "release_bundle_mode", False):
            raise SandboxError("canonical release progress store is not configured")
        return cls(
            store=store,
            benchmark=str(args.dataset),
            repeat_ordinal=int(getattr(args, "repeat_ordinal", 1)),
            seed=int(getattr(args, "seed", 0) or 0),
            initial_phase=store.progress().phase if store is not None else None,
        )

    @property
    def enabled(self) -> bool:
        return self.store is not None

    @property
    def should_evaluate_resumed_prefix(self) -> bool:
        return self.initial_phase is ReleasePhase.ACTIVE_LEG

    def start(self) -> None:
        if self.store is not None:
            self.store.start_leg(
                benchmark=self.benchmark,
                repeat_ordinal=self.repeat_ordinal,
                seed=self.seed,
            )

    def bind_inputs_and_materialize(
        self,
        *,
        partial_resume: bool,
        run_manifest_sha256: str,
        case_manifest_sha256: str,
        ordered_case_keys: Sequence[str],
        observations_path: Path,
    ) -> None:
        if self.store is None:
            return
        binding = {
            "benchmark": self.benchmark,
            "repeat_ordinal": self.repeat_ordinal,
            "run_manifest_sha256": run_manifest_sha256,
            "case_manifest_sha256": case_manifest_sha256,
            "ordered_case_keys": ordered_case_keys,
        }
        if partial_resume:
            self.store.authenticate_leg_inputs(**binding)
        else:
            self.store.bind_leg_inputs(**binding)
        self.store.materialize_observations(
            observations_path,
            benchmark=self.benchmark,
            repeat_ordinal=self.repeat_ordinal,
        )

    def history_receipts(self) -> list[dict[str, object]]:
        if self.store is None:
            return []
        return self.store.history_receipts(
            benchmark=self.benchmark,
            repeat_ordinal=self.repeat_ordinal,
        )

    def authenticated_failure_count(self) -> int:
        if self.store is None:
            return 0
        return sum(
            observation.get("observation_status") != "completed"
            for observation, _receipt in self.store.authenticate_commits(
                benchmark=self.benchmark,
                repeat_ordinal=self.repeat_ordinal,
            )
        )

    def governance_events(self) -> list[dict[str, object]]:
        if self.store is None:
            return []
        return self.store.governance_events(
            benchmark=self.benchmark,
            repeat_ordinal=self.repeat_ordinal,
        )

    def materialize_history(
        self,
        path: Path,
        *,
        render: Callable[[Sequence[Mapping[str, object]]], bytes],
    ) -> None:
        if self.store is None:
            return
        prefixes = self.store.history_view_receipt_prefixes(
            benchmark=self.benchmark,
            repeat_ordinal=self.repeat_ordinal,
        )
        self.store.materialize_snapshot(
            path,
            committed_views=[render(prefix) for prefix in prefixes],
            label="empty-history evidence view",
        )

    def begin_case(self, case_key: str) -> None:
        if self.store is not None:
            self.store.begin_case(
                benchmark=self.benchmark,
                repeat_ordinal=self.repeat_ordinal,
                case_key=case_key,
            )

    def commit_case(
        self,
        *,
        ordinal: int,
        case_key: str,
        observation: Mapping[str, object],
        history_receipt: Mapping[str, object],
        observations_path: Path,
    ) -> None:
        if self.store is None:
            return
        self.store.commit_case(
            benchmark=self.benchmark,
            repeat_ordinal=self.repeat_ordinal,
            ordinal=ordinal,
            case_key=case_key,
            observation=observation,
            history_receipt=history_receipt,
        )
        self.store.materialize_observations(
            observations_path,
            benchmark=self.benchmark,
            repeat_ordinal=self.repeat_ordinal,
        )

    def pause(self, *, candidate_sha256: str, prefix_case_count: int) -> None:
        if self.store is not None:
            self.store.pause_for_candidate(
                candidate_sha256=candidate_sha256,
                prefix_case_count=prefix_case_count,
            )
