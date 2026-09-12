from __future__ import annotations

from types import SimpleNamespace

import pytest

from workflow.intelligence.decision import DecisionEngine
from workflow.models import (
    DecisionType,
    StepResult,
    StepStatus,
    ValidationResult,
    WorkflowContext,
    WorkflowStep,
)


def _engine() -> DecisionEngine:
    engine = DecisionEngine.__new__(DecisionEngine)
    engine.policy_engine = SimpleNamespace(
        should_escalate=lambda _result, _step, _context: (False, "")
    )
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("validation_passed", "error_class", "retry_count", "expected"),
    [
        (False, "validation_failed", 0, DecisionType.RETRY.value),
        (True, "", 0, DecisionType.PROCEED.value),
        (False, "security_violation", 0, DecisionType.STOP.value),
        (False, "validation_failed", 3, DecisionType.ESCALATE.value),
    ],
)
async def test_validation_result_controls_high_score_decision(
    validation_passed: bool,
    error_class: str,
    retry_count: int,
    expected: str,
) -> None:
    engine = _engine()
    step = WorkflowStep(id="validate", task="check result")
    analysis = await engine._analyze_situation(
        ValidationResult(
            step_id=step.id,
            overall_score=0.92,
            validation_passed=validation_passed,
            validator_results=[],
            error_class=error_class,
        ),
        StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            retry_count=retry_count,
        ),
        step,
        WorkflowContext(workflow_id="workflow", session_id="session"),
    )

    assert analysis["validation_passed"] is validation_passed
    assert await engine._determine_action(analysis, step) == expected
