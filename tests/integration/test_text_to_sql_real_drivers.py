from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from custom_tools.text_to_sql.eval.release import (
    CandidateIdentity,
    load_protected_lane_evidence,
    probe_required_driver_lanes,
)


pytestmark = pytest.mark.db_integration


def test_required_real_driver_lanes_emit_nonzero_unavailable_evidence(tmp_path) -> None:
    lanes = tuple(
        lane.strip()
        for lane in os.getenv(
            "TEXT2SQL_REQUIRED_REAL_DRIVER_LANES",
            "postgres,mysql",
        ).split(",")
        if lane.strip()
    )
    evidence_path = Path(
        os.getenv(
            "TEXT2SQL_REAL_DRIVER_EVIDENCE",
            str(tmp_path / "real-driver-evidence.json"),
        )
    )
    candidate = CandidateIdentity(
        commit=os.environ["TEXT2SQL_EVAL_COMMIT"],
        artifact_digest=os.environ["TEXT2SQL_EVAL_CANDIDATE_DIGEST"],
        lock_digest=os.environ["TEXT2SQL_EVAL_LOCK_DIGEST"],
    )

    evidence = probe_required_driver_lanes(
        lanes,
        evidence_path=evidence_path,
        commit=candidate.commit,
        artifact_digest=candidate.artifact_digest,
        lock_digest=candidate.lock_digest,
    )

    assert evidence_path.exists()
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == evidence
    indexed = load_protected_lane_evidence(
        [evidence_path],
        expected_candidate=candidate,
        required_driver_lanes={f"driver:{lane}" for lane in lanes},
    )
    assert set(indexed) == {f"driver:{lane}" for lane in lanes}
    unavailable = [lane for lane in evidence["lanes"] if not lane["production_ready"]]
    if unavailable:
        pytest.fail(
            "protected real-driver lanes unavailable: "
            + ", ".join(
                f"{lane['dialect']}:{lane['reason_code']}" for lane in unavailable
            )
        )
