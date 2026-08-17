"""Closed path contract for release CONTINUE governance events."""

from __future__ import annotations

def governance_event_paths(
    event_kind: str, completed_case_count: int
) -> dict[str, str]:
    if event_kind not in {"mid_repeat", "post_repeat"}:
        raise ValueError("unknown release governance event kind")
    if (
        not isinstance(completed_case_count, int)
        or isinstance(completed_case_count, bool)
        or completed_case_count <= 0
    ):
        raise ValueError("release governance completed case count is invalid")
    prefix = f"governance/{event_kind}/{completed_case_count:06d}"
    return {
        "candidate_path": f"{prefix}/early_stop_candidate.json",
        "decision_path": f"{prefix}/repair_decision.json",
        "result_path": f"{prefix}/early_stop.json",
    }
