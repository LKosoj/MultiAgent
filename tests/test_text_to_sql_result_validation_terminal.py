"""RED terminal boundary for post-execution result contradictions."""

from __future__ import annotations

import importlib
import json

import pytest

from custom_tools.text_to_sql import core
from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
from custom_tools.text_to_sql.adaptive.models import (
    ResearchActionKind,
    ResultExpectation,
    ResultExpectationKind,
    SqlCandidate,
)
from custom_tools.text_to_sql.adaptive.result_validation import (
    RESULT_VALIDATION_RUNTIME_KEY,
    create_result_validation_capability,
)
from custom_tools.text_to_sql.adaptive.result_review import (
    RESULT_REVIEW_REQUIRED_RUNTIME_KEY,
    RESULT_REVIEW_RUNTIME_KEY,
    create_result_review_capability,
    ResultReviewReceipt,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from test_text_to_sql_result_expectations import _action_and_evidence, _column, _state_for
from text_to_sql_semantic_checks_helpers import POSTGRES_DSN
from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context
from workflow.deadline import WorkflowDeadlineExceeded


SQL = "SELECT o.status FROM orders o"


def _case():
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.INSPECT_COLUMN,
        column,
        {
            "status": "matched",
            "column": column.model_dump(mode="json", by_alias=True),
            "metadata": {"not_null": "True", "is_primary_key": False},
        },
        evidence_id="terminal-result-validation-not-null",
    )
    expectation = ResultExpectation(
        source_id="source-1",
        evidence_id=evidence.evidence_id,
        kind=ResultExpectationKind.DIRECT_OUTPUT_NOT_NULL,
        column=column,
    )
    state = _state_for(action, evidence)
    state = state.model_validate(
        {
            **state.model_dump(mode="python"),
            "result_expectations": (expectation,),
        }
    )
    freshness = FreshnessContext(
        evaluated_at=evidence.observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    parsed = parse_sql_candidate(SQL, POSTGRES_DSN, "terminal-candidate")
    candidate = SqlCandidate(
        candidate_id="terminal-candidate",
        sql=SQL,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    capability = create_result_validation_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
    )
    return state, requirements, candidate, capability


def _executor_result(data):
    return {
        "success": True,
        "data": data,
        "columns": ["status"],
        "rows_affected": len(data),
        "execution_time_ms": 1,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": SQL,
        "applied_row_limit": 10,
    }


def _terminal_side_effects(monkeypatch, data, *, persistence_allowed):
    calls = []
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(terminal, "_pre_execution_gate_allowed", lambda **_kwargs: True)

    def executor(sql_query, **_kwargs):
        assert sql_query == SQL
        calls.append("executor")
        return _executor_result(data)

    def audit(_entry):
        calls.append("audit")
        return {"status": "logged", "log_id": "terminal-audit"}

    def persist(**_kwargs):
        if not persistence_allowed:
            pytest.fail("result contradiction must not persist successful SQL")
        calls.append("persistence")
        return {"status": "saved", "filename": "query.md", "path": "/tmp/query.md"}

    monkeypatch.setattr(core, "secure_db_executor", executor)
    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(core, "save_successful_sql", persist)
    return calls


def _finalize(run_id):
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    return terminal.finalize_text_to_sql_run(
        SQL,
        "order status",
        POSTGRES_DSN,
        10,
        False,
        "terminal-session",
        run_id,
    )


def test_result_validation_terminal_capability_is_explicit() -> None:
    assert RESULT_VALIDATION_RUNTIME_KEY == "text_to_sql_result_validation"
    assert callable(create_result_validation_capability)


def test_terminal_returns_contradiction_receipt_without_persistence(monkeypatch) -> None:
    state, requirements, candidate, capability = _case()
    calls = _terminal_side_effects(monkeypatch, [[None]], persistence_allowed=False)
    token = set_tool_runtime_context({RESULT_VALIDATION_RUNTIME_KEY: capability})
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["record_kind"] == "text2sql_result_contradiction"
    assert result["run_id"] == state.run_id
    assert result["run_incarnation"] == state.run_incarnation
    assert result["research_state_revision"] == state.revision
    assert result["candidate_id"] == candidate.candidate_id
    assert result["normalized_ast_digest"] == candidate.normalized_ast_digest
    assert result["requirements_digest"] == requirements.requirements_digest
    assert result["finding"]["expectation"]["source_id"] == "source-1"
    assert result["finding"]["output_index"] == 0
    assert result["execution"] == _executor_result([[None]])
    assert calls == ["executor", "audit"]


def test_terminal_persists_when_result_has_no_contradiction(monkeypatch) -> None:
    state, _, _, capability = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=True)
    token = set_tool_runtime_context({RESULT_VALIDATION_RUNTIME_KEY: capability})
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "succeeded"
    assert calls == ["executor", "audit", "persistence"]


def test_terminal_returns_model_review_receipt_without_persistence(monkeypatch) -> None:
    state, requirements, candidate, validator = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=False)
    prompts: list[str] = []

    def reviewer(prompt: str) -> str:
        prompts.append(prompt)
        return '{"status":"contradicted","reason":"result conflicts with trusted evidence","source_id":"source-1"}'

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=("Return the winning alternative label, not an inner entity.",),
        model=reviewer,
    )
    token = set_tool_runtime_context(
        {
            RESULT_VALIDATION_RUNTIME_KEY: validator,
            RESULT_REVIEW_RUNTIME_KEY: review,
        }
    )
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["record_kind"] == "text2sql_result_review"
    assert result["verdict"] == "contradicted"
    assert result["candidate_id"] == candidate.candidate_id
    assert result["execution"] == _executor_result([["paid"]])
    assert calls == ["executor", "audit"]
    assert len(prompts) == 1
    assert "benchmark" not in prompts[0].lower()
    assert "function" not in prompts[0].lower()
    prompt = json.loads(prompts[0])
    assert prompt["documents"] == [
        "Return the winning alternative label, not an inner entity."
    ]
    assert (
        "check the exact answer form and projection requested by the question and documents"
        in prompt["instruction"].lower()
    )
    assert "winning alternative label or role" in prompt["instruction"].lower()


def test_terminal_durably_binds_consistent_model_review(monkeypatch) -> None:
    state, requirements, candidate, validator = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=True)
    reason = "r" * 542
    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=lambda _prompt: json.dumps({"status": "consistent", "reason": reason}),
    )
    token = set_tool_runtime_context(
        {RESULT_VALIDATION_RUNTIME_KEY: validator, RESULT_REVIEW_RUNTIME_KEY: review}
    )
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "succeeded"
    assert result["result_review"]["verdict"] == "consistent"
    assert result["result_review"]["reason"] == reason
    assert result["result_review"]["candidate_id"] == candidate.candidate_id
    assert calls == ["executor", "audit", "persistence"]


def test_typed_terminal_fails_closed_when_result_review_is_missing(monkeypatch) -> None:
    state, _, _, _ = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=False)
    token = set_tool_runtime_context({RESULT_REVIEW_REQUIRED_RUNTIME_KEY: True})
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["reason_code"] == "RESULT_REVIEW_FAILED"
    assert calls == ["executor", "audit"]


def test_malformed_result_review_returns_durable_no_target_receipt(monkeypatch) -> None:
    state, requirements, candidate, validator = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=False)
    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=lambda _prompt: "not json",
    )
    token = set_tool_runtime_context(
        {RESULT_VALIDATION_RUNTIME_KEY: validator, RESULT_REVIEW_RUNTIME_KEY: review}
    )
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["record_kind"] == "text2sql_result_review"
    assert result["verdict"] == "malformed"
    assert result["source_id"] is None
    assert result["evidence_id"] is None
    assert calls == ["executor", "audit"]


def test_expired_result_review_returns_durable_no_target_receipt(
    monkeypatch,
) -> None:
    state, requirements, candidate, validator = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=False)
    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=lambda _prompt: (_ for _ in ()).throw(
            WorkflowDeadlineExceeded("expired before result review call")
        ),
    )
    token = set_tool_runtime_context(
        {RESULT_VALIDATION_RUNTIME_KEY: validator, RESULT_REVIEW_RUNTIME_KEY: review}
    )
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["record_kind"] == "text2sql_result_review"
    assert result["verdict"] == "timeout"
    assert result["source_id"] is None
    assert result["evidence_id"] is None
    assert calls == ["executor", "audit"]


@pytest.mark.parametrize(
    ("verdict", "source_id", "evidence_id"),
    (
        ("consistent", "source-1", "terminal-result-validation-not-null"),
        ("contradicted", None, None),
        ("ambiguous", "source-1", None),
        ("malformed", "source-1", "terminal-result-validation-not-null"),
        ("timeout", None, "terminal-result-validation-not-null"),
    ),
)
def test_result_review_receipt_rejects_forged_reentry_targets(
    verdict, source_id, evidence_id
) -> None:
    state, requirements, candidate, _ = _case()

    with pytest.raises(ValueError):
        ResultReviewReceipt(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            research_state_revision=state.revision,
            candidate_id=candidate.candidate_id,
            normalized_ast_digest=candidate.normalized_ast_digest,
            requirements_digest=requirements.requirements_digest,
            source_id=source_id,
            evidence_id=evidence_id,
            verdict=verdict,
            reason="review outcome",
            execution=_executor_result([["paid"]]),
        )




def test_terminal_fails_closed_for_invalid_result_validation_capability(monkeypatch) -> None:
    state, _, _, _ = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=False)
    token = set_tool_runtime_context({RESULT_VALIDATION_RUNTIME_KEY: object()})
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "failed"
    assert result["reason_code"] == "RESULT_RECONCILIATION_FAILED"
    assert result["persistence"]["status"] == "error"
    assert len(result["persistence"]["error"]) <= 512
    assert calls == ["executor", "audit"]
