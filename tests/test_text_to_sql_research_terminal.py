from __future__ import annotations

import pytest

from custom_tools.text_to_sql.adaptive.models import ResearchStopReason
from custom_tools.text_to_sql.adaptive.ambiguity import AmbiguityReport
from custom_tools.text_to_sql.adaptive.terminal import research_stop_terminal_result
from tests.text_to_sql_semantic_coverage_helpers import RUN_ID


_STOP_TERMINALS = [
    (ResearchStopReason.AMBIGUOUS, "abstained", "RESEARCH_AMBIGUOUS"),
    (ResearchStopReason.UNSUPPORTED, "abstained", "RESEARCH_UNSUPPORTED"),
    (ResearchStopReason.STAGNATED, "abstained", "RESEARCH_STAGNATED"),
    (
        ResearchStopReason.BUDGET_EXHAUSTED,
        "abstained",
        "RESEARCH_BUDGET_EXHAUSTED",
    ),
    (ResearchStopReason.DEADLINE_EXCEEDED, "timed_out", "TIMED_OUT"),
    (ResearchStopReason.CANCELLED, "cancelled", "CANCELLED"),
    (ResearchStopReason.TOOL_FAILURE, "failed", "RESEARCH_TOOL_FAILURE"),
    (ResearchStopReason.PROTOCOL_FAILURE, "failed", "RESEARCH_PROTOCOL_FAILURE"),
]


@pytest.mark.parametrize(("reason", "status", "reason_code"), _STOP_TERMINALS)
def test_every_terminal_research_stop_has_one_public_terminal(
    reason: ResearchStopReason,
    status: str,
    reason_code: str,
) -> None:
    terminal = research_stop_terminal_result(
        RUN_ID,
        reason,
        AmbiguityReport(
            interpretations=("First reading.", "Second reading."),
            citation_evidence_ids=("evidence-1",),
            missing_distinguishing_fact="The definition is absent.",
        )
        if reason is ResearchStopReason.AMBIGUOUS
        else None,
    )

    assert terminal is not None
    assert terminal.status.value == status
    assert terminal.reason_code == reason_code
    assert terminal.sql == ""
    assert terminal.generated is False
    assert terminal.approved is False
    assert terminal.executed is False
    assert terminal.dry_run is False
    assert terminal.audited is False
    assert terminal.execution == {}
    assert terminal.audit == {}
    assert terminal.persistence == {"status": "not_attempted"}


def test_complete_research_has_no_early_terminal() -> None:
    assert research_stop_terminal_result(
        RUN_ID,
        ResearchStopReason.COMPLETE,
    ) is None


@pytest.mark.parametrize("malformed", [None, "unknown", object()])
def test_malformed_stop_reason_fails_closed(malformed: object) -> None:
    terminal = research_stop_terminal_result(RUN_ID, malformed)

    assert terminal is not None
    assert terminal.status.value == "failed"
    assert terminal.reason_code == "RESEARCH_PROTOCOL_FAILURE"
