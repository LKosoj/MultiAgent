"""RED runtime handoff contract for R4c-2 result validation."""

from __future__ import annotations

import builtins

import pytest
from custom_tools.text_to_sql.adaptive._policy_config import (
    load_adaptive_policy_config,
)
from custom_tools.text_to_sql.adaptive.result_review import (
    evaluate_result_review_capability,
)
from custom_tools.text_to_sql.adaptive.result_review_runtime import (
    INVALID_RESULT_REVIEW_RUNTIME,
    build_result_review_runtime,
)
from custom_tools.text_to_sql.adaptive.result_validation import (
    RESULT_VALIDATION_RUNTIME_KEY,
    evaluate_result_validation_capability,
)
from custom_tools.text_to_sql.adaptive.result_validation_runtime import (
    INVALID_RESULT_VALIDATION_RUNTIME,
    build_result_validation_runtime,
)
from test_text_to_sql_pre_execution_gate import VALID_SQL, _on_runtime
from workflow.deadline import DeadlineBudget
from workflow.enhanced_engine import EnhancedWorkflowEngine


def test_result_validation_runtime_exports_closed_factory_boundary() -> None:
    assert RESULT_VALIDATION_RUNTIME_KEY == "text_to_sql_result_validation"
    assert callable(build_result_validation_runtime)
    assert callable(evaluate_result_validation_capability)


def test_invalid_result_validation_runtime_is_present_named_sentinel() -> None:
    assert INVALID_RESULT_VALIDATION_RUNTIME is not None
    assert INVALID_RESULT_VALIDATION_RUNTIME is not object()


def _successful_execution():
    return {
        "success": True,
        "data": [["paid"]],
        "columns": ["status"],
        "rows_affected": 1,
        "execution_time_ms": 1,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": VALID_SQL,
        "applied_row_limit": 10,
    }


def test_exact_runtime_builds_capability_accepted_for_canonical_execution() -> None:
    runtime = _on_runtime(terminal=True)

    capability = build_result_validation_runtime(runtime, sql_query=VALID_SQL)
    receipt = evaluate_result_validation_capability(
        capability,
        expected_run_id=runtime.run_id,
        expected_sql=VALID_SQL,
        execution=_successful_execution(),
    )

    assert capability is not INVALID_RESULT_VALIDATION_RUNTIME
    assert receipt is None


def test_runtime_builder_rejects_sql_or_verified_candidate_mismatch() -> None:
    sql_runtime = _on_runtime(terminal=True)
    candidate_runtime = _on_runtime(terminal=True)
    candidate_runtime.verified_solver_candidate_id = "foreign-candidate"

    assert (
        build_result_validation_runtime(sql_runtime, sql_query="SELECT 1")
        is INVALID_RESULT_VALIDATION_RUNTIME
    )
    assert (
        build_result_validation_runtime(candidate_runtime, sql_query=VALID_SQL)
        is INVALID_RESULT_VALIDATION_RUNTIME
    )


def test_runtime_builder_does_not_depend_on_solver_runtime_import(monkeypatch) -> None:
    runtime = _on_runtime(terminal=True)
    real_import = builtins.__import__

    def deny_solver_runtime(name, *args, **kwargs):
        if name == "workflow.text_to_sql_adaptive_solver":
            raise AssertionError("result validation must not import solver runtime")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_solver_runtime)
    capability = build_result_validation_runtime(runtime, sql_query=VALID_SQL)

    assert capability is not INVALID_RESULT_VALIDATION_RUNTIME
    assert evaluate_result_validation_capability(
        capability,
        expected_run_id=runtime.run_id,
        expected_sql=VALID_SQL,
        execution=_successful_execution(),
    ) is None


def test_result_review_runtime_forwards_remaining_deadline_and_skips_expired_call(
    monkeypatch,
) -> None:
    import agent_command
    import utils

    clock = {"now": 100.0}
    deadline = DeadlineBudget.from_duration(
        5,
        monotonic=lambda: clock["now"],
        wall_time=lambda: clock["now"],
    )
    runtime = _on_runtime(terminal=True, deadline=deadline)
    runtime.verified_research_policy = load_adaptive_policy_config()
    provider_calls: list[dict[str, object]] = []
    review_calls: list[object] = []

    monkeypatch.setattr(
        agent_command,
        "create_text_to_sql_model",
        lambda _name, **kwargs: provider_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(
        utils,
        "call_openai_api",
        lambda **_kwargs: review_calls.append(_kwargs) or '{"status":"consistent","reason":"matches"}',
    )

    capability = build_result_review_runtime(runtime, sql_query=VALID_SQL)
    assert capability is not INVALID_RESULT_REVIEW_RUNTIME
    assert provider_calls == []
    clock["now"] = 102.0
    receipt = evaluate_result_review_capability(
        capability,
        expected_run_id=runtime.run_id,
        expected_sql=VALID_SQL,
        execution=_successful_execution(),
    )

    assert receipt.verdict == "consistent"
    assert provider_calls == [
        {
            "max_tokens": runtime.verified_research_policy.model_budget.output_tokens_per_call,
            "temperature": 0.3,
            "timeout_seconds": 3.0,
            "client_max_retries": 0,
        }
    ]
    assert len(review_calls) == 1
    response_format = review_calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "ModelReviewResponse"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["required"] == [
        "status",
        "reason",
        "source_id",
    ]

    expired_runtime = _on_runtime(
        terminal=True,
        deadline=DeadlineBudget.from_duration(
            5,
            monotonic=lambda: clock["now"],
            wall_time=lambda: clock["now"],
        ),
    )
    expired_runtime.verified_research_policy = load_adaptive_policy_config()
    expired_capability = build_result_review_runtime(
        expired_runtime, sql_query=VALID_SQL
    )
    assert expired_capability is not INVALID_RESULT_REVIEW_RUNTIME
    clock["now"] = 108.0
    expired_receipt = evaluate_result_review_capability(
        expired_capability,
        expected_run_id=expired_runtime.run_id,
        expected_sql=VALID_SQL,
        execution=_successful_execution(),
    )
    assert expired_receipt.verdict == "timeout"
    assert expired_receipt.source_id is None
    assert expired_receipt.evidence_id is None
    assert len(provider_calls) == 1
    assert len(review_calls) == 1


def test_enhanced_engine_installs_result_validation_metadata_fail_closed() -> None:
    runtime = _on_runtime(terminal=True)
    metadata: dict[str, object] = {}

    EnhancedWorkflowEngine._install_result_validation_runtime_metadata(
        metadata,
        runtime,
        sql_query=VALID_SQL,
        executor_dsn=runtime.dsn,
    )
    assert RESULT_VALIDATION_RUNTIME_KEY in metadata
    assert metadata[RESULT_VALIDATION_RUNTIME_KEY] is not INVALID_RESULT_VALIDATION_RUNTIME

    mismatch_metadata: dict[str, object] = {}
    EnhancedWorkflowEngine._install_result_validation_runtime_metadata(
        mismatch_metadata,
        runtime,
        sql_query="SELECT 1",
        executor_dsn=runtime.dsn,
    )
    assert mismatch_metadata[RESULT_VALIDATION_RUNTIME_KEY] is INVALID_RESULT_VALIDATION_RUNTIME

    direct_metadata: dict[str, object] = {}
    EnhancedWorkflowEngine._install_result_validation_runtime_metadata(
        direct_metadata,
        object(),
        sql_query=VALID_SQL,
        executor_dsn="sqlite:///unused.db",
    )
    assert RESULT_VALIDATION_RUNTIME_KEY not in direct_metadata
