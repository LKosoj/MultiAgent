from __future__ import annotations

import asyncio
import copy
import importlib
import json
import types
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from workflow.models import (
    TextToSqlTerminalReasonCode,
    TextToSqlTerminalResult,
    TextToSqlTerminalStatus,
)


def _successful_payload(**overrides):
    payload = {
        "run_id": "run-1",
        "status": "succeeded",
        "reason_code": "",
        "sql": "SELECT 1",
        "generated": True,
        "approved": True,
        "executed": True,
        "dry_run": False,
        "audited": True,
        "data": [[1]],
        "columns": ["value"],
        "rows_affected": 1,
        "error": None,
        "execution": {
            "success": True,
            "data": [[1]],
            "columns": ["value"],
            "rows_affected": 1,
            "execution_time_ms": 7,
            "dry_run_only": False,
            "skipped_execution": False,
            "sql_query": "SELECT 1",
            "applied_row_limit": 10,
        },
        "audit": {"status": "logged", "log_id": "audit-1"},
        "persistence": {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
        },
    }
    payload.update(overrides)
    execution = payload.get("execution")
    if (
        isinstance(execution, dict)
        and execution
        and payload.get("reason_code") != "EXECUTOR_CONTRACT_INVALID"
    ):
        execution.setdefault("sql_query", payload["sql"])
        execution.setdefault("applied_row_limit", 10)
    return payload


def _executor_result(**overrides):
    result = {
        "success": True,
        "data": [[1]],
        "columns": ["value"],
        "rows_affected": 1,
        "execution_time_ms": 1,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": "SELECT 1",
        "applied_row_limit": 10,
    }
    result.update(overrides)
    return result


def _deep_merge_mapping(base, overrides):
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and set(value) == {"$replace"}:
            merged[key] = copy.deepcopy(value["$replace"])
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_mapping(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


_TERMINAL_VECTOR_SET = json.loads(
    (Path(__file__).parent / "fixtures" / "text_to_sql_terminal_contract_vectors.json")
    .read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "case",
    _TERMINAL_VECTOR_SET["cases"],
    ids=lambda case: case["name"],
)
def test_terminal_contract_shared_cross_language_vectors(case):
    payload = _deep_merge_mapping(
        _TERMINAL_VECTOR_SET["base"],
        case["overrides"],
    )

    if case["valid"]:
        TextToSqlTerminalResult.from_mapping(payload)
    else:
        with pytest.raises((TypeError, ValueError)):
            TextToSqlTerminalResult.from_mapping(payload)


_NON_FAILED_REASON_CODES = {
    TextToSqlTerminalReasonCode.VERIFIER_REJECTED.value,
    TextToSqlTerminalReasonCode.SCHEMA_CLARIFICATION_REQUIRED.value,
    TextToSqlTerminalReasonCode.SCHEMA_GROUNDING_FAILED.value,
    TextToSqlTerminalReasonCode.SCHEMA_CONTEXT_BUDGET_EXCEEDED.value,
    TextToSqlTerminalReasonCode.CANCELLED.value,
    TextToSqlTerminalReasonCode.TIMED_OUT.value,
}


def test_terminal_reason_evidence_matrix_covers_every_failed_reason():
    actual = {
        case["reason_code"]
        for case in _TERMINAL_VECTOR_SET["reason_evidence_cases"]
    }
    expected = {
        reason.value
        for reason in TextToSqlTerminalReasonCode
        if reason.value not in _NON_FAILED_REASON_CODES
    }

    assert actual == expected


@pytest.mark.parametrize(
    "case",
    _TERMINAL_VECTOR_SET["reason_evidence_cases"],
    ids=lambda case: case["reason_code"],
)
def test_terminal_reason_evidence_matrix_shared_cross_language_vectors(case):
    valid_payload = _deep_merge_mapping(
        _TERMINAL_VECTOR_SET["base"],
        case["valid_overrides"],
    )
    invalid_payload = _deep_merge_mapping(
        valid_payload,
        case["invalid_overrides"],
    )

    TextToSqlTerminalResult.from_mapping(valid_payload)
    with pytest.raises((TypeError, ValueError)):
        TextToSqlTerminalResult.from_mapping(invalid_payload)


@pytest.mark.parametrize(
    ("executor_result", "error_fragment"),
    [
        (_executor_result(safety_issues={}), "must be a list"),
        (
            _executor_result(
                safety_issues=[
                    {"issue_type": "X", "description": "blocked", "extra": True}
                ]
            ),
            "exactly issue_type and description",
        ),
        (
            _executor_result(
                safety_issues=[{"issue_type": "X", "description": "blocked"}]
            ),
            "cannot contain safety_issues",
        ),
        (
            _executor_result(explain_result={"plan": "SCAN", "issues": []}),
            "must contain exactly",
        ),
        (
            _executor_result(
                explain_result={
                    "plan": "SCAN",
                    "estimated_cost": True,
                    "rows_to_scan": None,
                    "issues": [],
                }
            ),
            "estimated_cost",
        ),
        (
            _executor_result(
                explain_result={
                    "plan": "SCAN",
                    "estimated_cost": None,
                    "rows_to_scan": True,
                    "issues": [],
                }
            ),
            "rows_to_scan",
        ),
        (
            _executor_result(
                explain_result={
                    "plan": "SCAN",
                    "estimated_cost": None,
                    "rows_to_scan": None,
                    "issues": [],
                }
            ),
            "plan must match",
        ),
    ],
)
def test_finalizer_executor_contract_rejects_hostile_optional_evidence(
    executor_result,
    error_fragment,
):
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")

    error = terminal._executor_contract_error(
        executor_result,
        expected_dry_run_only=False,
        expected_sql_query="SELECT 1",
        expected_row_limit=10,
    )

    assert error is not None
    assert error_fragment in error


def test_finalizer_executor_contract_accepts_shaped_optional_evidence():
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    safety_failure = _executor_result(
        success=False,
        data=[],
        columns=[],
        rows_affected=0,
        error_message="Unsafe query.",
        safety_issues=[{"issue_type": "UNSAFE", "description": "blocked"}],
    )
    explain_success = _executor_result(
        data=[["SCAN"]],
        columns=["Plan"],
        explain_result={
            "plan": "SCAN",
            "estimated_cost": 10.0,
            "rows_to_scan": None,
            "issues": [{"issue_type": "FULL_SCAN", "description": "scan"}],
        },
    )

    assert terminal._executor_contract_error(
        safety_failure,
        expected_dry_run_only=False,
        expected_sql_query="SELECT 1",
        expected_row_limit=10,
    ) is None
    assert terminal._executor_contract_error(
        explain_success,
        expected_dry_run_only=False,
        expected_sql_query="SELECT 1",
        expected_row_limit=10,
    ) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generated", False),
        ("approved", False),
        ("audited", False),
        ("executed", False),
        ("dry_run", True),
    ],
)
def test_terminal_success_requires_all_mandatory_gates(field, value):
    with pytest.raises(ValueError):
        TextToSqlTerminalResult.from_mapping(
            _successful_payload(**{field: value})
        )


def test_terminal_dry_run_success_is_not_executed():
    outcome = TextToSqlTerminalResult.from_mapping(
        _successful_payload(
            executed=False,
            dry_run=True,
            data=[],
            columns=[],
            rows_affected=0,
            execution={
                "success": True,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "execution_time_ms": 0,
                "dry_run_only": True,
                "skipped_execution": True,
            },
            persistence={"status": "not_attempted"},
        )
    )

    assert outcome.status is TextToSqlTerminalStatus.SUCCEEDED
    assert outcome.executed is False
    assert outcome.dry_run is True


@pytest.mark.parametrize("value", ["true", 1, None])
def test_terminal_model_does_not_coerce_non_boolean_flags(value):
    with pytest.raises(TypeError):
        TextToSqlTerminalResult.from_mapping(
            _successful_payload(generated=value)
        )


def test_terminal_model_rejects_missing_and_unknown_fields():
    missing = _successful_payload()
    missing.pop("audit")
    with pytest.raises(ValueError, match="missing"):
        TextToSqlTerminalResult.from_mapping(missing)

    unknown = _successful_payload(unexpected="value")
    with pytest.raises(ValueError, match="unknown"):
        TextToSqlTerminalResult.from_mapping(unknown)


def test_abstained_requires_reason_and_cannot_claim_runtime_gates():
    payload = _successful_payload(
        status="abstained",
        reason_code="VERIFIER_REJECTED",
        approved=False,
        executed=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
    )

    outcome = TextToSqlTerminalResult.from_mapping(payload)
    assert outcome.status is TextToSqlTerminalStatus.ABSTAINED

    with pytest.raises(ValueError):
        replace(outcome, reason_code="")
    with pytest.raises(ValueError):
        replace(outcome, audited=True)


def test_cancelled_and_timed_out_cannot_claim_execution_success():
    for status in ("cancelled", "timed_out"):
        with pytest.raises(ValueError):
            TextToSqlTerminalResult.from_mapping(
                _successful_payload(status=status)
            )


@pytest.mark.parametrize("status", ["abstained", "failed", "cancelled", "timed_out"])
def test_every_non_success_terminal_requires_reason_code(status):
    with pytest.raises(ValueError, match="reason_code"):
        TextToSqlTerminalResult.from_mapping(
            _successful_payload(
                status=status,
                reason_code="",
                generated=False,
                approved=False,
                executed=False,
                dry_run=False,
                audited=False,
                data=[],
                columns=[],
                rows_affected=0,
                error=None,
                execution={},
                audit={},
                persistence={"status": "not_attempted"},
            )
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"approved": True, "generated": False},
        {"executed": True, "approved": False},
        {"dry_run": True, "approved": False},
        {"audited": True, "approved": False},
    ],
)
def test_terminal_runtime_evidence_requires_generation_and_approval(overrides):
    payload = _successful_payload(
        status="failed",
        reason_code="RUNTIME_FAILED",
        executed=False,
        dry_run=False,
        audited=False,
        error="failed",
    )
    payload.update(overrides)
    with pytest.raises(ValueError):
        TextToSqlTerminalResult.from_mapping(payload)


def test_terminal_error_is_bounded():
    with pytest.raises(ValueError, match="4096"):
        TextToSqlTerminalResult.from_mapping(
            _successful_payload(
                status="failed",
                reason_code="EXECUTION_FAILED",
                error="x" * 4097,
            )
        )


def test_terminal_model_defensively_isolates_nested_json_values():
    source = _successful_payload()
    source["data"] = [[{"labels": ["before"]}]]
    source["execution"]["data"] = copy.deepcopy(source["data"])

    outcome = TextToSqlTerminalResult.from_mapping(source)
    source["data"][0][0]["labels"].append("source-mutated")
    source["execution"]["data"][0][0]["labels"].append("source-mutated")

    first = outcome.to_mapping()
    first["data"][0][0]["labels"].append("mapping-mutated")
    first["execution"]["data"][0][0]["labels"].append("mapping-mutated")
    second = outcome.to_mapping()

    assert second["data"] == [[{"labels": ["before"]}]]
    assert second["execution"]["data"] == [[{"labels": ["before"]}]]


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("data", [[{"not_json": {1, 2}}]]),
        ("execution", {"success": True, "not_json": object()}),
        ("audit", {"status": "logged", "not_json": float("nan")}),
        ("persistence", {"status": "saved", 1: "non-string-key"}),
    ],
)
def test_terminal_model_rejects_non_json_nested_values(field, bad_value):
    payload = _successful_payload(**{field: bad_value})
    if field == "data":
        payload["execution"]["data"] = copy.deepcopy(bad_value)

    with pytest.raises((TypeError, ValueError), match="JSON|serializable|finite|string keys"):
        TextToSqlTerminalResult.from_mapping(payload)


def test_terminal_model_rejects_circular_nested_values():
    circular = []
    circular.append(circular)
    payload = _successful_payload(data=circular)
    payload["execution"]["data"] = circular

    with pytest.raises(ValueError, match="circular JSON"):
        TextToSqlTerminalResult.from_mapping(payload)


@pytest.mark.parametrize(
    "payload",
    [
        _successful_payload(execution={"success": True}),
        _successful_payload(
            execution={
                "success": False,
                "data": [[1]],
                "columns": ["value"],
                "rows_affected": 1,
                "dry_run_only": False,
                "skipped_execution": False,
            }
        ),
        _successful_payload(audit={"status": "error"}),
        _successful_payload(audit={"status": "logged", "error": "sink failed"}),
    ],
)
def test_terminal_model_rejects_nested_evidence_that_contradicts_top_level(payload):
    with pytest.raises((TypeError, ValueError)):
        TextToSqlTerminalResult.from_mapping(payload)


def test_terminal_model_accepts_empty_success_error_fields():
    for empty_error in (None, ""):
        payload = _successful_payload()
        payload["execution"]["error_message"] = empty_error
        outcome = TextToSqlTerminalResult.from_mapping(payload)
        assert outcome.status is TextToSqlTerminalStatus.SUCCEEDED


def test_terminal_model_rejects_success_with_nonempty_execution_error():
    payload = _successful_payload()
    payload["execution"]["error_message"] = "contradictory executor error"

    with pytest.raises(ValueError, match="successful execution"):
        TextToSqlTerminalResult.from_mapping(payload)


@pytest.mark.parametrize("error_message", [None, "", "   ", "x" * 4097])
def test_terminal_model_requires_bounded_failed_execution_error(error_message):
    payload = _successful_payload(
        status="failed",
        reason_code="EXECUTION_FAILED",
        executed=True,
        data=[],
        columns=[],
        rows_affected=0,
        error="execution failed",
        execution={
            "success": False,
            "data": [],
                "columns": [],
                "rows_affected": 0,
                "execution_time_ms": 0,
                "error_message": error_message,
            "dry_run_only": False,
            "skipped_execution": False,
        },
        persistence={"status": "not_attempted"},
    )

    with pytest.raises((TypeError, ValueError), match="error_message"):
        TextToSqlTerminalResult.from_mapping(payload)


def test_terminal_model_rejects_failed_execution_with_partial_rows():
    payload = _successful_payload(
        status="failed",
        reason_code="EXECUTION_FAILED",
        executed=True,
        data=[[1]],
        columns=["value"],
        rows_affected=1,
        error="execution failed",
        execution={
            "success": False,
            "data": [[1]],
                "columns": ["value"],
                "rows_affected": 1,
                "execution_time_ms": 0,
                "error_message": "execution failed",
            "dry_run_only": False,
            "skipped_execution": False,
        },
        persistence={"status": "not_attempted"},
    )

    with pytest.raises(ValueError, match="failed execution"):
        TextToSqlTerminalResult.from_mapping(payload)


@pytest.mark.parametrize(
    "audit",
    [
        {"status": "banana", "error": "audit failed"},
        {"status": "error"},
        {"status": "error", "error": None},
        {"status": "error", "error": ""},
        {"status": "error", "error": "   "},
        {"status": "error", "error": "x" * 4097},
    ],
)
def test_terminal_model_rejects_invalid_audit_error_evidence(audit):
    payload = _successful_payload(
        status="failed",
        reason_code="AUDIT_FAILED",
        audited=False,
        error="audit failed",
        audit=audit,
    )

    with pytest.raises((TypeError, ValueError), match="audit"):
        TextToSqlTerminalResult.from_mapping(payload)


def test_terminal_model_requires_complete_trusted_execution_markers():
    for missing_marker in (
        "dry_run_only",
        "skipped_execution",
        "sql_query",
        "applied_row_limit",
    ):
        payload = _successful_payload()
        payload["execution"].pop(missing_marker)
        with pytest.raises(ValueError, match="missing fields"):
            TextToSqlTerminalResult.from_mapping(payload)


@pytest.mark.parametrize("execution_time_ms", [True, False, -1, 1.5, "1", None])
def test_terminal_model_requires_exact_nonnegative_execution_time(execution_time_ms):
    payload = _successful_payload()
    payload["execution"]["execution_time_ms"] = execution_time_ms

    with pytest.raises((TypeError, ValueError), match="execution_time_ms"):
        TextToSqlTerminalResult.from_mapping(payload)


def test_terminal_model_rejects_missing_execution_time():
    payload = _successful_payload()
    payload["execution"].pop("execution_time_ms")

    with pytest.raises(ValueError, match="missing fields"):
        TextToSqlTerminalResult.from_mapping(payload)


def test_terminal_authoritative_nested_state_cannot_be_mutated():
    outcome = TextToSqlTerminalResult.from_mapping(_successful_payload())
    baseline = outcome.to_mapping()

    try:
        outcome.data[0][0] = 999
    except (AttributeError, TypeError):
        pass
    try:
        outcome.execution["data"][0][0] = 999
    except (AttributeError, TypeError):
        pass
    try:
        outcome.audit["status"] = "error"
    except (AttributeError, TypeError):
        pass
    try:
        outcome.persistence["status"] = "error"
    except (AttributeError, TypeError):
        pass

    assert outcome.to_mapping() == baseline


@pytest.mark.parametrize(
    ("top_data", "nested_data"),
    [
        ([[True]], [[1]]),
        ([[False]], [[0]]),
        ([[1]], [[True]]),
        ([[0]], [[False]]),
        ([[{"flag": True}]], [[{"flag": 1}]]),
    ],
)
def test_terminal_evidence_equality_is_json_type_aware(top_data, nested_data):
    payload = _successful_payload(
        data=top_data,
        rows_affected=1,
    )
    payload["execution"]["data"] = nested_data

    with pytest.raises(ValueError, match="execution.data"):
        TextToSqlTerminalResult.from_mapping(payload)


@pytest.mark.parametrize(
    ("status", "reason_code"),
    [
        ("failed", "BANANA_REASON"),
        ("abstained", "EXECUTION_FAILED"),
        ("cancelled", "TIMED_OUT"),
        ("timed_out", "CANCELLED"),
    ],
)
def test_terminal_reason_codes_are_closed_and_status_coupled(status, reason_code):
    payload = _successful_payload(
        status=status,
        reason_code=reason_code,
        generated=False,
        approved=False,
        executed=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        error="terminal failure",
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
    )

    with pytest.raises(ValueError, match="reason_code"):
        TextToSqlTerminalResult.from_mapping(payload)


@pytest.mark.parametrize(
    "reason_code",
    [
        "SCHEMA_CLARIFICATION_REQUIRED",
        "SCHEMA_GROUNDING_FAILED",
        "SCHEMA_CONTEXT_BUDGET_EXCEEDED",
    ],
)
def test_schema_linking_reasons_are_closed_abstained_outcomes(reason_code):
    payload = _successful_payload(
        status="abstained",
        reason_code=reason_code,
        sql="",
        generated=False,
        approved=False,
        executed=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        error=None,
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
    )

    outcome = TextToSqlTerminalResult.from_mapping(payload)

    assert outcome.status is TextToSqlTerminalStatus.ABSTAINED
    assert outcome.reason_code == reason_code


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("audit", {"status": "logged", "log_id": "audit-1", "extra": True}),
        ("audit", {"status": "logged"}),
        ("audit", {"status": "error", "error": "disk full", "extra": True}),
        ("persistence", {"status": "saved"}),
        (
            "persistence",
            {
                "status": "saved",
                "filename": "query.md",
                "path": "/tmp/query.md",
                "extra": True,
            },
        ),
        ("persistence", {"status": "banana"}),
    ],
)
def test_terminal_audit_and_persistence_schemas_are_closed(field, value):
    payload = _successful_payload(**{field: value})

    with pytest.raises((TypeError, ValueError), match=field):
        TextToSqlTerminalResult.from_mapping(payload)


@pytest.mark.parametrize(
    "payload",
    [
        _successful_payload(
            executed=False,
            dry_run=True,
            data=[],
            columns=[],
            rows_affected=0,
            execution={
                "success": True,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "execution_time_ms": 1,
                "dry_run_only": True,
                "skipped_execution": True,
            },
            persistence={"status": "error", "error": "store unavailable"},
        ),
        _successful_payload(persistence={"status": "not_attempted"}),
        _successful_payload(
            status="failed",
            reason_code="RESULT_AGGREGATION_FAILED",
            executed=False,
            audited=False,
            data=[],
            columns=[],
            rows_affected=0,
            error="aggregation failed",
            execution={},
            audit={},
            persistence={
                "status": "saved",
                "filename": "query.md",
                "path": "/tmp/query.md",
            },
        ),
        _successful_payload(
            status="abstained",
            reason_code="VERIFIER_REJECTED",
            generated=True,
            approved=False,
            executed=False,
            audited=False,
            data=[],
            columns=[],
            rows_affected=0,
            error=None,
            execution={},
            audit={},
            persistence={"status": "error", "error": "unexpected"},
        ),
        _successful_payload(
            status="cancelled",
            reason_code="CANCELLED",
            generated=False,
            approved=False,
            executed=False,
            audited=False,
            sql="",
            data=[],
            columns=[],
            rows_affected=0,
            error="cancelled",
            execution={},
            audit={},
            persistence={"status": "error", "error": "unexpected"},
        ),
        _successful_payload(
            status="timed_out",
            reason_code="TIMED_OUT",
            generated=False,
            approved=False,
            executed=False,
            audited=False,
            sql="",
            data=[],
            columns=[],
            rows_affected=0,
            error="timed out",
            execution={},
            audit={},
            persistence={"status": "error", "error": "unexpected"},
        ),
    ],
)
def test_terminal_persistence_must_match_runtime_state(payload):
    with pytest.raises(ValueError, match="persistence"):
        TextToSqlTerminalResult.from_mapping(payload)


@pytest.mark.parametrize(
    "payload",
    [
        _successful_payload(
            persistence={"status": "error", "error": "index unavailable"},
        ),
        _successful_payload(
            status="failed",
            reason_code="RESULT_AGGREGATION_FAILED",
            error="aggregation failed",
        ),
        _successful_payload(
            status="failed",
            reason_code="RESULT_PERSISTENCE_FAILED",
            executed=False,
            dry_run=True,
            data=[],
            columns=[],
            rows_affected=0,
            error="result persistence failed",
            execution={
                "success": True,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "execution_time_ms": 1,
                "dry_run_only": True,
                "skipped_execution": True,
            },
            persistence={"status": "error", "error": "result persistence failed"},
        ),
    ],
)
def test_terminal_accepts_coherent_persistence_runtime_states(payload):
    TextToSqlTerminalResult.from_mapping(payload)


def test_terminal_model_preserves_coherent_failure_evidence():
    execution_failed = TextToSqlTerminalResult.from_mapping(
        _successful_payload(
            status="failed",
            reason_code="EXECUTION_FAILED",
            executed=True,
            data=[],
            columns=[],
            rows_affected=0,
            error="database execution failed",
            execution={
                "success": False,
                "data": [],
                "columns": [],
                "rows_affected": 0,
                "execution_time_ms": 0,
                "dry_run_only": False,
                "skipped_execution": False,
                "error_message": "database execution failed",
            },
            persistence={"status": "not_attempted"},
        )
    )
    audit_failed = TextToSqlTerminalResult.from_mapping(
        _successful_payload(
            status="failed",
            reason_code="AUDIT_FAILED",
            audited=False,
            error="audit sink failed",
            audit={"status": "error", "error": "audit sink failed"},
            persistence={"status": "not_attempted"},
        )
    )

    assert execution_failed.executed is True
    assert execution_failed.audited is True
    assert audit_failed.executed is True
    assert audit_failed.audited is False


def test_terminal_model_preserves_untrusted_executor_contract_evidence():
    outcome = TextToSqlTerminalResult.from_mapping(
        _successful_payload(
            status="failed",
            reason_code="EXECUTOR_CONTRACT_INVALID",
            executed=False,
            data=[],
            columns=[],
            rows_affected=0,
            error="executor returned malformed evidence",
            execution={"raw_success": "yes", "rows_affected": "one"},
            persistence={"status": "not_attempted"},
        )
    )

    assert outcome.status is TextToSqlTerminalStatus.FAILED
    assert outcome.execution == {"raw_success": "yes", "rows_affected": "one"}

    with pytest.raises(ValueError, match="trusted execution"):
        TextToSqlTerminalResult.from_mapping(
            _successful_payload(
                status="failed",
                reason_code="EXECUTOR_CONTRACT_INVALID",
                error="executor returned malformed evidence",
            )
        )


@pytest.mark.parametrize("invalid", ["true", "false", 1, 0, None])
def test_finalizer_rejects_non_exact_dry_run_bool_before_side_effects(
    monkeypatch,
    invalid,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid dry_run_only must fail before side effects")

    monkeypatch.setattr(core, "secure_db_executor", forbidden)
    monkeypatch.setattr(core, "audit_logger", forbidden)
    monkeypatch.setattr(core, "save_successful_sql", forbidden)

    with pytest.raises(TypeError, match="dry_run_only"):
        terminal.finalize_text_to_sql_run(
            "SELECT 1",
            "one",
            "Approved",
            "sqlite:///unused.db",
            10,
            invalid,
            "session-1",
            "run-1",
        )


def test_verifier_rejection_is_abstained_without_executor_call(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")

    def forbidden(*args, **kwargs):
        raise AssertionError("rejected SQL must not invoke a runtime side effect")

    monkeypatch.setattr(core, "secure_db_executor", forbidden)
    monkeypatch.setattr(core, "audit_logger", forbidden)
    monkeypatch.setattr(core, "save_successful_sql", forbidden)

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Rejected",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "abstained"
    assert result["reason_code"] == "VERIFIER_REJECTED"
    assert result["executed"] is False
    assert result["audited"] is False


@pytest.mark.parametrize("verification_status", ["approved", "Unknown", "Rejected ", ""])
def test_unknown_verifier_status_fails_contract_without_side_effects(
    monkeypatch,
    verification_status,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid verifier status must stop before side effects")

    monkeypatch.setattr(core, "secure_db_executor", forbidden)
    monkeypatch.setattr(core, "audit_logger", forbidden)
    monkeypatch.setattr(core, "save_successful_sql", forbidden)

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        verification_status,
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "VERIFIER_CONTRACT_INVALID"
    assert result["generated"] is True
    assert result["approved"] is False
    assert result["executed"] is False
    assert result["audited"] is False
    assert result["error"]
    assert len(result["error"]) <= 4096


def test_real_executor_explicit_dry_run_never_opens_connection_or_mutates_env(
    monkeypatch,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    monkeypatch.delenv("TEXT_TO_SQL_DRY_RUN_ONLY", raising=False)
    monkeypatch.setattr(
        core,
        "sql_safety_check",
        lambda sql_query, dsn=None: {"is_safe": True, "issues": []},
    )

    def forbidden_plugin(_dsn):
        raise AssertionError("explicit dry-run must not resolve a DB plugin")

    monkeypatch.setattr(core, "get_plugin", forbidden_plugin)
    result = core.secure_db_executor(
        "SELECT 1",
        row_limit=10,
        dsn="sqlite:///unused.db",
        dry_run_only=True,
    )

    assert result["success"] is True
    assert result["dry_run_only"] is True
    assert result["skipped_execution"] is True
    assert result["sql_query"] == "SELECT 1"
    assert result["applied_row_limit"] == 10
    assert "TEXT_TO_SQL_DRY_RUN_ONLY" not in importlib.import_module("os").environ


def test_executor_operator_dry_run_override_remains_fail_safe(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "true")
    monkeypatch.setattr(
        core,
        "sql_safety_check",
        lambda sql_query, dsn=None: {"is_safe": True, "issues": []},
    )
    monkeypatch.setattr(
        core,
        "get_plugin",
        lambda _dsn: pytest.fail("operator dry-run override must prevent DB access"),
    )

    result = core.secure_db_executor(
        "SELECT 1",
        row_limit=10,
        dsn="sqlite:///unused.db",
        dry_run_only=False,
    )

    assert result["dry_run_only"] is True
    assert result["skipped_execution"] is True
    assert importlib.import_module("os").environ["TEXT_TO_SQL_DRY_RUN_ONLY"] == "true"


@pytest.mark.parametrize("invalid", ["true", "false", 1, 0])
def test_executor_boundaries_reject_non_exact_dry_run_before_work(monkeypatch, invalid):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    db_exec = importlib.import_module("custom_tools.text_to_sql.core._db_exec")

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid dry_run_only reached executor work")

    monkeypatch.setattr(core, "sql_safety_check", forbidden)
    monkeypatch.setattr(core, "_get_core_singleton", forbidden)

    with pytest.raises(TypeError, match="dry_run_only"):
        core.secure_db_executor(
            "SELECT 1",
            row_limit=1,
            dsn="sqlite:///unused.db",
            dry_run_only=invalid,
        )
    with pytest.raises(TypeError, match="dry_run_only"):
        db_exec.secure_db_executor(
            "SELECT 1",
            row_limit=1,
            dsn="sqlite:///unused.db",
            dry_run_only=invalid,
            sql_validator=object(),
        )


def test_dry_run_succeeds_with_executed_false_and_audited_true(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    calls = []

    def executor(sql_query, row_limit=None, dsn=None, *, dry_run_only=None):
        calls.append(("execute", sql_query, row_limit, dsn, dry_run_only))
        return {
            "success": True,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": 1,
            "error_message": None,
            "dry_run_only": True,
            "skipped_execution": True,
            "sql_query": sql_query,
            "applied_row_limit": row_limit,
        }

    def audit(entry):
        calls.append(("audit", entry))
        return {"status": "logged", "log_id": "audit-1"}

    monkeypatch.setattr(core, "secure_db_executor", executor)
    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("dry-run must not persist SQL"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        True,
        "session-1",
        "run-1",
    )

    assert calls[0] == (
        "execute",
        "SELECT 1",
        10,
        "sqlite:///unused.db",
        True,
    )
    assert calls[1][0] == "audit"
    assert result["status"] == "succeeded"
    assert result["dry_run"] is True
    assert result["executed"] is False
    assert result["audited"] is True
    assert result["persistence"] == {"status": "not_attempted"}


def test_successful_executor_allows_empty_error_message(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: _executor_result(error_message=""),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-1"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
        },
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "succeeded"
    assert result["reason_code"] == ""


@pytest.mark.parametrize("execution_time_ms", [None, True, -1, 1.5])
def test_finalizer_rejects_invalid_executor_execution_time(
    monkeypatch,
    execution_time_ms,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    raw = {
        "success": True,
        "data": [[1]],
        "columns": ["value"],
        "rows_affected": 1,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
    }
    if execution_time_ms is not None:
        raw["execution_time_ms"] = execution_time_ms
    monkeypatch.setattr(core, "secure_db_executor", lambda *args, **kwargs: raw)
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-1"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("invalid execution contract must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1", "one", "Approved", "sqlite:///unused.db", 10, False,
        "session-1", "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTOR_CONTRACT_INVALID"


def test_finalizer_caught_executor_exception_records_elapsed_without_execution_claim(
    monkeypatch,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    ticks = iter([10_000_000_000, 10_125_000_000])
    monkeypatch.setattr(terminal, "monotonic_ns", lambda: next(ticks), raising=False)
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database down")),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-1"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("failed execution must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1", "one", "Approved", "sqlite:///unused.db", 10, False,
        "session-1", "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTION_FAILED"
    assert result["executed"] is False
    assert result["execution"]["success"] is False
    assert result["persistence"] == {"status": "not_attempted"}
    assert result["execution"]["execution_time_ms"] == 125


def test_finalizer_deadline_exceeded_propagates_not_swallowed(monkeypatch):
    """T14: WorkflowDeadlineExceeded из core.secure_db_executor не должен

    тихо превращаться в обычный EXECUTION_FAILED — он обязан всплыть наружу,
    как и другие deadline-guards в кодовой базе.
    """
    from workflow.deadline import WorkflowDeadlineExceeded

    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            WorkflowDeadlineExceeded("deadline exceeded during execution")
        ),
    )

    with pytest.raises(WorkflowDeadlineExceeded):
        terminal.finalize_text_to_sql_run(
            "SELECT 1", "one", "Approved", "sqlite:///unused.db", 10, False,
            "session-1", "run-1",
        )


def test_executor_failure_with_partial_data_is_contract_invalid(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    audited = []
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: {
            "success": False,
            "data": [["partial"]],
            "columns": ["value"],
            "rows_affected": 1,
            "error_message": "execution failed",
            "dry_run_only": False,
            "skipped_execution": False,
        },
    )

    def audit(entry):
        audited.append(entry)
        return {"status": "logged", "log_id": "audit-1"}

    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("failed execution must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert len(audited) == 1
    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTOR_CONTRACT_INVALID"
    assert result["audited"] is True
    assert result["data"] == []


def test_requested_dry_run_rejects_executor_execution_claim(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: _executor_result(),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-1"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("requested dry-run must never persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        True,
        "session-1",
        "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTOR_CONTRACT_INVALID"
    assert result["executed"] is False
    assert result["dry_run"] is False


def test_operator_dry_run_response_is_compatible_with_false_request(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "true")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: _executor_result(
            data=[],
            columns=[],
            rows_affected=0,
            dry_run_only=True,
            skipped_execution=True,
        ),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-1"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("operator dry-run must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "succeeded"
    assert result["dry_run"] is True
    assert result["executed"] is False


@pytest.mark.parametrize(
    "executor_result",
    [
        {
            "success": True,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "error_message": "contradictory error",
            "dry_run_only": False,
            "skipped_execution": False,
        },
        {
            "success": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "error_message": None,
            "dry_run_only": False,
            "skipped_execution": False,
        },
        {
            "success": True,
            "data": [[1]],
            "columns": ["value"],
            "rows_affected": 1,
            "execution_time_ms": 1,
            "error_message": None,
            "dry_run_only": True,
            "skipped_execution": True,
        },
    ],
)
def test_executor_success_error_and_dry_run_data_must_be_consistent(
    monkeypatch,
    executor_result,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(core, "secure_db_executor", lambda *args, **kwargs: executor_result)
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-1"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("invalid executor result must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTOR_CONTRACT_INVALID"


def test_audit_error_is_failed_after_successful_execution(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: _executor_result(),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "error", "error": "disk full"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("audit failure must stop persistence"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "AUDIT_FAILED"
    assert result["executed"] is True
    assert result["audited"] is False


@pytest.mark.parametrize(
    "audit_result",
    [
        {"status": "banana", "error": "unknown status"},
        {"status": "error"},
        {"status": "error", "error": ""},
    ],
)
def test_invalid_audit_evidence_is_normalized_to_audit_failure(
    monkeypatch,
    audit_result,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: _executor_result(),
    )
    monkeypatch.setattr(core, "audit_logger", lambda entry: audit_result)
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("invalid audit evidence must stop persistence"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "AUDIT_CONTRACT_INVALID"
    assert result["audit"]["status"] == "error"
    assert isinstance(result["audit"]["error"], str)
    assert result["audit"]["error"].strip()


def _cyclic_mapping(status):
    value = {"status": status}
    value["cycle"] = value
    return value


def _hostile_executor_result(case):
    result = {
        "success": True,
        "data": [[1]],
        "columns": ["value"],
        "rows_affected": 1,
        "execution_time_ms": 1,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": "SELECT 1",
        "applied_row_limit": 10,
    }
    if case == "cycle":
        result["cycle"] = result
    elif case == "nan":
        result["data"] = [[float("nan")]]
    elif case == "infinity":
        result["data"] = [[float("inf")]]
    elif case == "non_string_key":
        result[1] = "invalid key"
    elif case == "non_json_data":
        result["data"] = [[object()]]
    elif case == "set_data":
        result["data"] = {1}
    elif case == "extra":
        result["unexpected"] = True
    else:
        raise AssertionError(f"unknown hostile executor case: {case}")
    return result


@pytest.mark.parametrize(
    "case",
    [
        "cycle",
        "nan",
        "infinity",
        "non_string_key",
        "non_json_data",
        "set_data",
        "extra",
    ],
)
def test_hostile_executor_result_is_normalized_before_access_and_never_throws(
    monkeypatch,
    case,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    calls = []
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: calls.append("execute")
        or _hostile_executor_result(case),
    )

    def audit(entry):
        calls.append("audit")
        assert entry["execution"]["status"] == "failed"
        assert entry["execution"]["success"] is False
        return {"status": "logged", "log_id": "audit-hostile"}

    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("invalid executor result must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1", "one", "Approved", "sqlite:///unused.db", 10, False,
        "session-1", "run-1",
    )

    assert calls == ["execute", "audit"]
    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTOR_CONTRACT_INVALID"
    assert result["executed"] is False
    assert result["audited"] is True
    assert result["persistence"] == {"status": "not_attempted"}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda result: result.update(sql_query="SELECT 2"), "sql_query"),
        (lambda result: result.pop("sql_query"), "missing fields"),
        (lambda result: result.update(applied_row_limit=999), "applied_row_limit"),
        (lambda result: result.update(applied_row_limit=-1), "applied_row_limit"),
        (lambda result: result.update(applied_row_limit="1"), "applied_row_limit"),
        (lambda result: result.pop("applied_row_limit"), "missing fields"),
        (
            lambda result: result.update(data=[[1], [2]], rows_affected=2),
            "row limit",
        ),
        (
            lambda result: result.update(data=[[1, 2]], columns=["value"]),
            "column count",
        ),
        (
            lambda result: result.update(data=[{"other": 1}], columns=["value"]),
            "column names",
        ),
    ],
)
def test_executor_runtime_proof_mismatch_is_typed_before_audit_and_never_persists(
    monkeypatch,
    mutation,
    expected_error,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    executor_result = {
        "success": True,
        "data": [[1]],
        "columns": ["value"],
        "rows_affected": 1,
        "execution_time_ms": 1,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": "SELECT 1",
        "applied_row_limit": 1,
    }
    mutation(executor_result)
    calls = []
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: calls.append("execute") or executor_result,
    )

    def audit(entry):
        calls.append("audit")
        assert entry["execution"]["success"] is False
        assert entry["execution"]["status"] == "failed"
        return {"status": "logged", "log_id": "audit-proof-mismatch"}

    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("untrusted runtime proof must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1", "one", "Approved", "sqlite:///unused.db", 1, False,
        "session-1", "run-1",
    )

    assert calls == ["execute", "audit"]
    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTOR_CONTRACT_INVALID"
    assert expected_error in result["error"]
    assert result["executed"] is False
    assert result["data"] == []
    assert result["columns"] == []
    assert result["rows_affected"] == 0
    assert result["audit"] == {
        "status": "logged",
        "log_id": "audit-proof-mismatch",
    }
    assert result["persistence"] == {"status": "not_attempted"}


@pytest.mark.parametrize(
    ("dry_run_only", "data", "columns", "expected_executed", "persisted"),
    [
        (True, [], [], False, False),
        (False, [], ["value"], True, True),
    ],
)
def test_executor_runtime_proof_accepts_dry_run_and_empty_result_paths(
    monkeypatch,
    dry_run_only,
    data,
    columns,
    expected_executed,
    persisted,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: {
            "success": True,
            "data": data,
            "columns": columns,
            "rows_affected": 0,
            "execution_time_ms": 1,
            "error_message": None,
            "dry_run_only": dry_run_only,
            "skipped_execution": dry_run_only,
            "sql_query": "SELECT 1",
            "applied_row_limit": 1,
        },
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-empty"},
    )
    persistence_calls = []
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda **kwargs: persistence_calls.append(kwargs) or {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
        },
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1", "one", "Approved", "sqlite:///unused.db", 1,
        dry_run_only, "session-1", "run-1",
    )

    assert result["status"] == "succeeded"
    assert result["executed"] is expected_executed
    assert result["dry_run"] is dry_run_only
    assert bool(persistence_calls) is persisted


@pytest.mark.parametrize(
    "audit_factory",
    [
        lambda: {"status": "banana"},
        lambda: {"status": "logged", "log_id": "audit-1", "extra": True},
        lambda: {"status": "error", "error": float("nan")},
        lambda: {"status": "error", "error": {"not-json"}},
        lambda: _cyclic_mapping("logged"),
    ],
)
def test_audit_adapter_contract_violations_are_typed_and_never_throw(
    monkeypatch,
    audit_factory,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    calls = []
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: calls.append("execute")
        or _executor_result(execution_time_ms=4),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: calls.append("audit") or audit_factory(),
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("audit contract failure must stop persistence"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1", "one", "Approved", "sqlite:///unused.db", 10, False,
        "session-1", "run-1",
    )

    assert calls == ["execute", "audit"]
    assert result["status"] == "failed"
    assert result["reason_code"] == "AUDIT_CONTRACT_INVALID"
    assert result["audit"]["status"] == "error"
    assert result["audit"]["error"]
    assert result["persistence"] == {"status": "not_attempted"}


@pytest.mark.parametrize(
    "persistence_factory",
    [
        lambda: "not-an-object",
        lambda: {"status": "banana"},
        lambda: {"status": "saved", "filename": "query.md"},
        lambda: {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
            "extra": True,
        },
        lambda: {"status": "error", "error": float("nan")},
        lambda: {"status": "error", "error": {"not-json"}},
        lambda: _cyclic_mapping("saved"),
    ],
)
def test_persistence_adapter_contract_violations_are_typed_after_side_effect(
    monkeypatch,
    persistence_factory,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    calls = []
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: calls.append("execute")
        or _executor_result(execution_time_ms=4),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: calls.append("audit") or {
            "status": "logged",
            "log_id": "audit-1",
        },
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: calls.append("persist") or persistence_factory(),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1", "one", "Approved", "sqlite:///unused.db", 10, False,
        "session-1", "run-1",
    )

    assert calls == ["execute", "audit", "persist"]
    assert result["status"] == "failed"
    assert result["reason_code"] == "PERSISTENCE_CONTRACT_INVALID"
    assert result["executed"] is True
    assert result["audited"] is True
    assert result["persistence"]["status"] == "error"
    assert result["persistence"]["error"]


def test_successful_real_execution_attempts_memory_write_only_after_success(
    monkeypatch,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    calls = []

    def executor(*args, **kwargs):
        calls.append("execute")
        return _executor_result()

    def audit(entry):
        calls.append("audit")
        return {"status": "logged", "log_id": "audit-1"}

    def persist(*args, **kwargs):
        calls.append("persist")
        return {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
        }

    monkeypatch.setattr(core, "secure_db_executor", executor)
    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(core, "save_successful_sql", persist)

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert calls == ["execute", "audit", "persist"]
    assert result["status"] == "succeeded"
    assert result["persistence"]["status"] == "saved"


def test_terminal_success_passes_trusted_namespace_and_evidence_to_writer(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    captured = {}
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: _executor_result(),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-1"},
    )

    def persist(**kwargs):
        captured.update(kwargs)
        return {
            "status": "saved",
            "filename": "query.md",
            "path": "/tmp/query.md",
        }

    monkeypatch.setattr(core, "save_successful_sql", persist)

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
        namespace_version_key="a" * 64,
    )

    assert result["status"] == "succeeded"
    assert captured["namespace_version_key"] == "a" * 64
    assert captured["run_id"] == "run-1"
    assert captured["approved"] is True
    assert captured["executed"] is True
    assert captured["execution_success"] is True
    assert captured["audited"] is True


def test_audit_and_persistence_receive_same_bounded_execution_summary(monkeypatch):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    marker = "huge-cell-marker-" + ("x" * 20_000)
    sql = "SELECT secret_column FROM orders"
    user_query = "show every secret order"
    captured = {}

    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: _executor_result(
            sql_query=sql,
            data=[[marker]],
            columns=["secret_column"],
            execution_time_ms=123,
        ),
    )

    def audit(entry):
        captured["audit"] = entry
        return {"status": "logged", "log_id": "audit-1"}

    def persist(**kwargs):
        captured["persistence"] = kwargs
        return {"status": "saved"}

    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(core, "save_successful_sql", persist)

    result = terminal.finalize_text_to_sql_run(
        sql,
        user_query,
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["data"] == [[marker]]
    audit_json = json.dumps(captured["audit"], ensure_ascii=False)
    assert len(audit_json) < 4096
    assert marker not in audit_json
    assert sql not in audit_json
    assert user_query not in audit_json
    summary = captured["audit"]["execution"]
    assert summary["row_count"] == 1
    assert summary["row_limit"] == 10
    assert summary["run_id"] == "run-1"
    assert summary["session_id"] == "session-1"
    assert summary["sql_length"] == len(sql)
    assert summary["query_length"] == len(user_query)
    assert summary["execution_time_ms"] == 123
    persisted = captured["persistence"]
    assert json.loads(persisted["execution_result"]) == summary
    assert persisted["sql_query"] == sql
    assert persisted["user_query"] == user_query
    assert sql not in persisted["execution_result"]
    assert user_query not in persisted["execution_result"]
    assert marker not in json.dumps(persisted, ensure_ascii=False)


def test_finalizer_writes_real_query_and_bounded_summary_to_sqlrag_artifact(
    monkeypatch,
    tmp_path,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    audit_module = importlib.import_module("custom_tools.text_to_sql.core._audit")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    marker = "row-payload-marker-" + ("x" * 20_000)
    sql = "SELECT COUNT(*) AS order_count FROM orders"
    user_query = "How many orders are there?"
    monkeypatch.setattr(audit_module, "get_facade_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: _executor_result(
            sql_query=sql,
            data=[[marker]],
            columns=["order_count"],
            execution_time_ms=321,
        ),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-artifact"},
    )

    result = terminal.finalize_text_to_sql_run(
        sql,
        user_query,
        "Approved",
        "sqlite:///artifact.db",
        10,
        False,
        "session-artifact",
        "run-artifact",
    )

    artifact = Path(result["persistence"]["path"])
    content = artifact.read_text(encoding="utf-8")
    assert result["status"] == "succeeded"
    assert sql in content
    assert user_query in content
    assert "Время выполнения: 321ms" in content
    assert marker not in content
    assert artifact.stat().st_size < 4096


@pytest.mark.parametrize(
    "executor_result",
    [
        "not-an-object",
        {"success": True},
        {
            "success": "true",
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "error_message": None,
            "dry_run_only": False,
            "skipped_execution": False,
        },
        {
            "success": True,
            "data": [],
            "columns": "value",
            "rows_affected": 0,
            "error_message": None,
            "dry_run_only": False,
            "skipped_execution": False,
        },
    ],
)
def test_malformed_executor_result_is_audited_and_fails_closed(
    monkeypatch,
    executor_result,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    audits = []
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: executor_result,
    )

    def audit(entry):
        audits.append(entry)
        return {"status": "logged", "log_id": "audit-1"}

    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("malformed execution must not persist"),
    )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert len(audits) == 1
    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTOR_CONTRACT_INVALID"
    assert result["executed"] is False


@pytest.mark.parametrize(
    ("persistence_result", "expected_status"),
    [
        (RuntimeError("storage unavailable"), "error"),
        ({"status": "error", "error": "index unavailable"}, "error"),
    ],
)
def test_persistence_failure_is_visible_without_replacing_runtime_success(
    monkeypatch,
    persistence_result,
    expected_status,
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: _executor_result(),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-1"},
    )

    def persist(*args, **kwargs):
        if isinstance(persistence_result, Exception):
            raise persistence_result
        return persistence_result

    monkeypatch.setattr(core, "save_successful_sql", persist)

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-1",
        "run-1",
    )

    assert result["status"] == "succeeded"
    assert result["executed"] is True
    assert result["audited"] is True
    assert result["persistence"]["status"] == expected_status


def _light_enhanced_engine_and_models():
    epic6 = importlib.import_module("tests.test_workflow_engine_epic6")
    engine = epic6._enhanced_engine_instance()
    return engine, epic6._workflow_models()


@pytest.mark.parametrize(
    ("terminal_payload", "expected_executed", "expected_audited"),
    [
        (
            _successful_payload(
                status="failed",
                reason_code="EXECUTION_FAILED",
                executed=True,
                audited=True,
                data=[],
                columns=[],
                rows_affected=0,
                error="database execution failed",
                execution={
                    "success": False,
                    "data": [],
                    "columns": [],
                    "rows_affected": 0,
                    "execution_time_ms": 0,
                    "dry_run_only": False,
                    "skipped_execution": False,
                    "error_message": "database execution failed",
                },
                persistence={"status": "not_attempted"},
            ),
            True,
            True,
        ),
        (
                _successful_payload(
                    status="failed",
                    reason_code="AUDIT_FAILED",
                    audited=False,
                    error="audit sink failed",
                    audit={"status": "error", "error": "audit sink failed"},
                    persistence={"status": "not_attempted"},
                ),
            True,
            False,
        ),
    ],
)
def test_enhanced_tool_step_preserves_valid_failed_terminal_evidence(
    terminal_payload,
    expected_executed,
    expected_audited,
):
    engine, models = _light_enhanced_engine_and_models()
    step = models.WorkflowStep(
        id="db_audit",
        step_type="tool",
        tool_name="finalize_text_to_sql_run",
        task="finalize",
    )
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
        step_outputs={
            "sql_generation": {"sql": "SELECT 1"},
            "sql_verification": {"verification_status": "Approved"},
        },
    )
    engine.policy_engine = types.SimpleNamespace(get_budget=lambda *args: None)
    engine._step_with_substituted_metadata = lambda current, _context: current
    engine._format_task_with_variables = lambda task, _context, _step_id: task

    async def execute_tool(_step, _context, _task):
        return dict(terminal_payload)

    engine._execute_tool_step = execute_tool
    step_result = asyncio.run(
        engine._execute_step_with_policy(step, context, plan=None, attempt=1)
    )

    assert step_result.status is models.StepStatus.COMPLETED
    generation_result = models.StepResult(
        step_id="sql_generation",
        status=models.StepStatus.COMPLETED,
        output={"sql": "SELECT 1"},
    )
    verification_result = models.StepResult(
        step_id="sql_verification",
        status=models.StepStatus.COMPLETED,
        output={"verification_status": "Approved"},
    )
    outcome = engine._derive_text_to_sql_terminal_outcome(
        models.WorkflowDefinition(name="text_to_sql_pipeline"),
        context,
        {
            "sql_generation": generation_result,
            "sql_verification": verification_result,
            "db_audit": step_result,
        },
    )
    assert outcome.status is models.TextToSqlTerminalStatus.FAILED
    assert outcome.executed is expected_executed
    assert outcome.audited is expected_audited


@pytest.mark.parametrize(
    ("name", "category", "expected"),
    [
        ("text_to_sql_pipeline", None, True),
        ("generic_pipeline", "text_to_sql", False),
        ("text_to_sql_pipeline", "generic", True),
    ],
)
def test_text_to_sql_identity_is_exact_name_only(name, category, expected):
    engine, models = _light_enhanced_engine_and_models()
    metadata = {} if category is None else {"category": category}
    workflow = models.WorkflowDefinition(name=name, metadata=metadata)

    assert engine._is_text_to_sql_workflow(workflow) is expected


def test_skipped_verifier_gate_is_abstained_through_real_condition():
    engine, models = _light_enhanced_engine_and_models()
    workflow = models.WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    db_audit = next(step for step in workflow.steps if step.id == "db_audit")
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
        step_outputs={
            "sql_generation": {"sql": "SELECT 1", "description": "one"},
            "sql_generation.sql": "SELECT 1",
            "sql_verification": {"verification_status": "Rejected"},
            "sql_verification.verification_status": "Rejected",
        },
    )

    assert engine._should_skip_step_by_condition(db_audit, context) is True
    skipped = models.StepResult(
        step_id="db_audit",
        status=models.StepStatus.SKIPPED,
        output=context.step_outputs["db_audit"],
    )
    outcome = engine._derive_text_to_sql_terminal_outcome(
        workflow,
        context,
        {
            "sql_generation": models.StepResult(
                step_id="sql_generation",
                status=models.StepStatus.COMPLETED,
                output={"sql": "SELECT 1", "description": "one"},
            ),
            "sql_verification": models.StepResult(
                step_id="sql_verification",
                status=models.StepStatus.COMPLETED,
                output={"verification_status": "Rejected"},
            ),
            "db_audit": skipped,
        },
    )

    assert outcome.status.value == TextToSqlTerminalStatus.ABSTAINED.value
    assert outcome.executed is False
    assert outcome.audited is False


@pytest.mark.parametrize(
    ("sql", "expected_generated"),
    [("", False), ("SELECT 1", True)],
)
def test_skipped_verifier_gate_derives_generation_from_actual_step(sql, expected_generated):
    engine, models = _light_enhanced_engine_and_models()
    workflow = models.WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    db_audit = next(step for step in workflow.steps if step.id == "db_audit")
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
        step_outputs={
            "sql_generation": {"sql": sql, "description": "query"},
            "sql_generation.sql": sql,
            "sql_verification": {"verification_status": "Rejected"},
            "sql_verification.verification_status": "Rejected",
        },
    )

    assert engine._should_skip_step_by_condition(db_audit, context) is True
    steps = {
        "sql_generation": models.StepResult(
            step_id="sql_generation",
            status=models.StepStatus.COMPLETED,
            output={"sql": sql, "description": "query"},
        ),
        "sql_verification": models.StepResult(
            step_id="sql_verification",
            status=models.StepStatus.COMPLETED,
            output={"verification_status": "Rejected"},
        ),
        "db_audit": models.StepResult(
            step_id="db_audit",
            status=models.StepStatus.SKIPPED,
            output=context.step_outputs["db_audit"],
        ),
    }

    outcome = engine._derive_text_to_sql_terminal_outcome(workflow, context, steps)

    assert outcome.status is models.TextToSqlTerminalStatus.ABSTAINED
    assert outcome.sql == sql
    assert outcome.generated is expected_generated
    assert outcome.approved is False


def test_missing_or_malformed_db_audit_cannot_complete():
    engine, models = _light_enhanced_engine_and_models()
    workflow = models.WorkflowDefinition(
        name="text_to_sql_pipeline",
        metadata={"category": "text_to_sql"},
    )
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
    )

    missing = engine._derive_text_to_sql_terminal_outcome(workflow, context, {})
    malformed = engine._derive_text_to_sql_terminal_outcome(
        workflow,
        context,
        {
            "db_audit": models.StepResult(
                step_id="db_audit",
                status=models.StepStatus.COMPLETED,
                output="agent claims success",
            )
        },
    )

    assert missing.status.value == TextToSqlTerminalStatus.FAILED.value
    assert malformed.status.value == TextToSqlTerminalStatus.FAILED.value


@pytest.mark.parametrize("generated_sql", ["", "   ", None, 42])
@pytest.mark.parametrize("audit_case", ["missing", "failed", "invalid"])
def test_failure_derivation_never_throws_for_invalid_generated_sql(
    generated_sql,
    audit_case,
):
    engine, models = _light_enhanced_engine_and_models()
    workflow = models.WorkflowDefinition(name="text_to_sql_pipeline")
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
        step_outputs={
            "sql_generation": {"sql": generated_sql},
            "sql_verification": {"verification_status": "Approved"},
        },
    )
    if audit_case == "missing":
        steps = {}
    elif audit_case == "failed":
        steps = {
            "db_audit": models.StepResult(
                step_id="db_audit",
                status=models.StepStatus.FAILED,
                output=None,
                error="audit step failed",
            )
        }
    else:
        steps = {
            "db_audit": models.StepResult(
                step_id="db_audit",
                status=models.StepStatus.COMPLETED,
                output={"forged": "terminal"},
            )
        }

    outcome = engine._derive_text_to_sql_terminal_outcome(
        workflow,
        context,
        steps,
    )

    assert outcome.status is models.TextToSqlTerminalStatus.FAILED
    assert outcome.generated is False
    assert outcome.approved is False
    assert outcome.sql == ""


@pytest.mark.parametrize("audit_case", ["missing", "failed", "invalid"])
def test_failure_derivation_uses_completed_step_results_not_forged_context(audit_case):
    engine, models = _light_enhanced_engine_and_models()
    workflow = models.WorkflowDefinition(name="text_to_sql_pipeline")
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
        step_outputs={
            "sql_generation": {"sql": "SELECT forged"},
            "sql_verification": {"verification_status": "Approved"},
        },
    )
    steps = {
        "sql_generation": models.StepResult(
            step_id="sql_generation",
            status=models.StepStatus.COMPLETED,
            output={"sql": "SELECT actual"},
        ),
        "sql_verification": models.StepResult(
            step_id="sql_verification",
            status=models.StepStatus.COMPLETED,
            output={"verification_status": "Rejected"},
        ),
    }
    if audit_case == "failed":
        steps["db_audit"] = models.StepResult(
            step_id="db_audit",
            status=models.StepStatus.FAILED,
            output=None,
            error="audit step failed",
        )
    elif audit_case == "invalid":
        steps["db_audit"] = models.StepResult(
            step_id="db_audit",
            status=models.StepStatus.COMPLETED,
            output={"forged": "terminal"},
        )

    outcome = engine._derive_text_to_sql_terminal_outcome(workflow, context, steps)

    assert outcome.status is models.TextToSqlTerminalStatus.FAILED
    assert outcome.sql == "SELECT actual"
    assert outcome.generated is True
    assert outcome.approved is False


@pytest.mark.parametrize("verifier_case", ["missing", "failed", "invalid"])
def test_skipped_abstention_requires_completed_exact_rejected_verifier(verifier_case):
    engine, models = _light_enhanced_engine_and_models()
    workflow = models.WorkflowDefinition(name="text_to_sql_pipeline")
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
        step_outputs={
            "sql_generation": {"sql": "SELECT forged"},
            "sql_verification": {"verification_status": "Rejected"},
        },
    )
    steps = {
        "sql_generation": models.StepResult(
            step_id="sql_generation",
            status=models.StepStatus.COMPLETED,
            output={"sql": "SELECT actual"},
        ),
        "db_audit": models.StepResult(
            step_id="db_audit",
            status=models.StepStatus.SKIPPED,
            output=_abstained_payload(),
        ),
    }
    if verifier_case == "failed":
        steps["sql_verification"] = models.StepResult(
            step_id="sql_verification",
            status=models.StepStatus.FAILED,
            output={"verification_status": "Rejected"},
            error="verifier failed",
        )
    elif verifier_case == "invalid":
        steps["sql_verification"] = models.StepResult(
            step_id="sql_verification",
            status=models.StepStatus.COMPLETED,
            output={"verification_status": "Banana"},
        )

    outcome = engine._derive_text_to_sql_terminal_outcome(workflow, context, steps)

    assert outcome.status is models.TextToSqlTerminalStatus.FAILED


def test_skipped_abstention_ignores_forged_context_when_actual_verifier_rejected():
    engine, models = _light_enhanced_engine_and_models()
    workflow = models.WorkflowDefinition(name="text_to_sql_pipeline")
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
        step_outputs={
            "sql_generation": {"sql": "SELECT forged"},
            "sql_verification": {"verification_status": "Approved"},
        },
    )
    steps = {
        "sql_generation": models.StepResult(
            step_id="sql_generation",
            status=models.StepStatus.COMPLETED,
            output={"sql": "SELECT actual"},
        ),
        "sql_verification": models.StepResult(
            step_id="sql_verification",
            status=models.StepStatus.COMPLETED,
            output={"verification_status": "Rejected"},
        ),
        "db_audit": models.StepResult(
            step_id="db_audit",
            status=models.StepStatus.SKIPPED,
            output=_abstained_payload(),
        ),
    }

    outcome = engine._derive_text_to_sql_terminal_outcome(workflow, context, steps)

    assert outcome.status is models.TextToSqlTerminalStatus.ABSTAINED
    assert outcome.sql == "SELECT actual"
    assert outcome.approved is False


@pytest.mark.parametrize(
    ("generated_sql", "verification_status"),
    [
        ("SELECT 2", "Approved"),
        ("SELECT 1", "Rejected"),
    ],
)
def test_forged_succeeded_terminal_must_match_actual_step_outputs(
    generated_sql,
    verification_status,
):
    engine, models = _light_enhanced_engine_and_models()
    workflow = models.WorkflowDefinition(
        name="text_to_sql_pipeline",
        metadata={"category": "text_to_sql"},
    )
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
    )
    terminal_output = _successful_payload()
    steps = {
        "sql_generation": models.StepResult(
            step_id="sql_generation",
            status=models.StepStatus.COMPLETED,
            output={"sql": generated_sql, "description": "query"},
        ),
        "sql_verification": models.StepResult(
            step_id="sql_verification",
            status=models.StepStatus.COMPLETED,
            output={
                "verification_status": verification_status,
                "safety_check": {},
                "performance_check": {},
                "recommendations": [],
            },
        ),
        "db_audit": models.StepResult(
            step_id="db_audit",
            status=models.StepStatus.COMPLETED,
            output=terminal_output,
        ),
    }

    outcome = engine._derive_text_to_sql_terminal_outcome(
        workflow,
        context,
        steps,
    )

    assert outcome.status.value == "failed"
    assert outcome.reason_code in {
        "SQL_GENERATION_OUTPUT_MISMATCH",
        "SQL_VERIFICATION_OUTPUT_MISMATCH",
    }


def _failed_execution_payload(**overrides):
    payload = _successful_payload(
        status="failed",
        reason_code="EXECUTION_FAILED",
        executed=True,
        data=[],
        columns=[],
        rows_affected=0,
        error="execution failed",
        execution={
            "success": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": 0,
            "error_message": "execution failed",
            "dry_run_only": False,
            "skipped_execution": False,
        },
        persistence={"status": "not_attempted"},
    )
    payload.update(overrides)
    return payload


def _abstained_payload(**overrides):
    payload = _successful_payload(
        status="abstained",
        reason_code="VERIFIER_REJECTED",
        approved=False,
        executed=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        error=None,
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
    )
    payload.update(overrides)
    return payload


def _executor_contract_invalid_payload(**overrides):
    payload = _failed_execution_payload(
        reason_code="EXECUTOR_CONTRACT_INVALID",
        executed=False,
        error="executor contract invalid",
        execution={"raw_success": "yes"},
    )
    payload.update(overrides)
    return payload


def _audit_failed_payload(**overrides):
    payload = _successful_payload(
        status="failed",
        reason_code="AUDIT_FAILED",
        audited=False,
        error="audit failed",
        audit={"status": "error", "error": "audit failed"},
        persistence={"status": "not_attempted"},
    )
    payload.update(overrides)
    return payload


def _verifier_contract_invalid_payload(**overrides):
    payload = _successful_payload(
        status="failed",
        reason_code="VERIFIER_CONTRACT_INVALID",
        approved=False,
        executed=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        error="verification status is invalid",
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
    )
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("terminal_payload", "generated_sql", "verification_status", "expected_reason"),
    [
        (
            _failed_execution_payload(),
            "SELECT 2",
            "Approved",
            "SQL_GENERATION_OUTPUT_MISMATCH",
        ),
        (
            _failed_execution_payload(),
            "SELECT 1",
            "Rejected",
            "SQL_VERIFICATION_OUTPUT_MISMATCH",
        ),
        (
            _abstained_payload(),
            "SELECT 2",
            "Rejected",
            "SQL_GENERATION_OUTPUT_MISMATCH",
        ),
        (
            _abstained_payload(),
            "SELECT 1",
            "Approved",
            "SQL_VERIFICATION_OUTPUT_MISMATCH",
        ),
        (
            _executor_contract_invalid_payload(),
            "SELECT 2",
            "Approved",
            "SQL_GENERATION_OUTPUT_MISMATCH",
        ),
        (
            _executor_contract_invalid_payload(),
            "SELECT 1",
            "Rejected",
            "SQL_VERIFICATION_OUTPUT_MISMATCH",
        ),
        (
            _verifier_contract_invalid_payload(),
            "SELECT 1",
            "Rejected",
            "SQL_VERIFICATION_OUTPUT_MISMATCH",
        ),
        (
            _abstained_payload(),
            "SELECT 1",
            "Banana",
            "SQL_VERIFICATION_OUTPUT_MISMATCH",
        ),
    ],
)
def test_all_completed_terminal_outcomes_match_actual_generation_and_verifier(
    terminal_payload,
    generated_sql,
    verification_status,
    expected_reason,
):
    engine, models = _light_enhanced_engine_and_models()
    workflow = models.WorkflowDefinition(name="text_to_sql_pipeline")
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
        step_outputs={
            "sql_generation": {"sql": generated_sql},
            "sql_verification": {"verification_status": verification_status},
        },
    )
    steps = {
        "sql_generation": models.StepResult(
            step_id="sql_generation",
            status=models.StepStatus.COMPLETED,
            output={"sql": generated_sql},
        ),
        "sql_verification": models.StepResult(
            step_id="sql_verification",
            status=models.StepStatus.COMPLETED,
            output={"verification_status": verification_status},
        ),
        "db_audit": models.StepResult(
            step_id="db_audit",
            status=models.StepStatus.COMPLETED,
            output=terminal_payload,
        ),
    }

    outcome = engine._derive_text_to_sql_terminal_outcome(workflow, context, steps)

    assert outcome.status is models.TextToSqlTerminalStatus.FAILED
    assert outcome.reason_code == expected_reason


def test_registered_finalizer_executes_through_real_engine_tool_path(
    monkeypatch,
):
    epic6 = importlib.import_module("tests.test_workflow_engine_epic6")
    engine = epic6._engine_instance()
    models = epic6._workflow_models()
    workflow = models.WorkflowDefinition.from_yaml(
        Path("workflow_pipelines/text_to_sql_pipeline.yaml")
    )
    step = next(item for item in workflow.steps if item.id == "db_audit")

    definition = yaml.safe_load(
        Path("tool_definitions/finalize_text_to_sql_run.yaml").read_text(
            encoding="utf-8"
        )
    )
    verification_parameter = next(
        item for item in definition["parameters"]
        if item["name"] == "verification_status"
    )
    assert verification_parameter["enum"] == ["Approved", "Rejected"]
    module_name, function_name = definition["implementation_source"].rsplit(".", 1)
    implementation = getattr(importlib.import_module(module_name), function_name)
    wrapped_tool = importlib.import_module("smolagents").tool(implementation)
    assert definition["name"] == step.tool_name == wrapped_tool.name

    engine.factory = types.SimpleNamespace(
        _create_tool=lambda name: wrapped_tool if name == step.tool_name else None
    )
    engine.resource_manager = types.SimpleNamespace(record_api_call=lambda run_id: None)

    class _DirectToolManager:
        def run_tool(self, *, tool_function, session_id=None, **kwargs):
            kwargs.pop("tool_name", None)
            kwargs.pop("task_description", None)
            kwargs.pop("trace_run_id", None)
            kwargs.pop("workflow_run_id", None)
            if session_id is not None:
                kwargs["session_id"] = session_id
            return tool_function(**kwargs)

    tool_manager = importlib.import_module("tool_manager")
    monkeypatch.setattr(tool_manager, "get_tool_manager", lambda: _DirectToolManager())
    core = importlib.import_module("custom_tools.text_to_sql.core")
    monkeypatch.setattr(
        core,
        "secure_db_executor",
        lambda *args, **kwargs: _executor_result(
            data=[],
            columns=[],
            rows_affected=0,
            execution_time_ms=0,
            dry_run_only=True,
            skipped_execution=True,
        ),
    )
    monkeypatch.setattr(
        core,
        "audit_logger",
        lambda entry: {"status": "logged", "log_id": "audit-1"},
    )
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda *args, **kwargs: pytest.fail("dry-run must not persist"),
    )
    from custom_tools.text_to_sql.validators import resolve_safety_policy

    safety_policy = resolve_safety_policy("strict")
    namespace_version_key = "a" * 64
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={
            **workflow.inputs,
            "query": "one",
            "dsn": "sqlite:///unused.db",
            "max_rows": 10,
            "dry_run_only": True,
            "run_id": "run-1",
            "session_id": "session-1",
            "safety_policy": safety_policy,
        },
        step_outputs={
            "schema_linking_step": {
                "namespace_version_key": namespace_version_key,
            },
            "schema_linking_step.namespace_version_key": namespace_version_key,
            "sql_generation": {"sql": "SELECT 1", "description": "one"},
            "sql_generation.sql": "SELECT 1",
            "sql_verification": {"verification_status": "Approved"},
            "sql_verification.verification_status": "Approved",
        },
    )
    step = engine._step_with_substituted_metadata(step, context)

    result = asyncio.run(engine._execute_tool_step(step, context, step.task))

    assert result["status"] == "succeeded"
    assert result["dry_run"] is True
    assert result["executed"] is False


_USE_TERMINAL_PAYLOAD = object()


def _run_enhanced_terminal_scenario(
    terminal_payload,
    *,
    cancelled=False,
    aggregation_error=None,
    real_aggregator_fallback=False,
    generated_sql="SELECT 1",
    verification_status="Approved",
    db_audit_status=None,
    aggregated_final=_USE_TERMINAL_PAYLOAD,
    workflow_name="text_to_sql_pipeline",
):
    engine, models = _light_enhanced_engine_and_models()
    events = []
    checkpoints = []
    engine.metrics_collector = types.SimpleNamespace(
        record_workflow_start=lambda *args: None
    )
    engine.budget_manager = types.SimpleNamespace(
        create_workflow_budget=lambda *args: object()
    )

    async def started(*args, **kwargs):
        return object()

    async def released(*args, **kwargs):
        events.append("released")

    async def completed(*args, **kwargs):
        events.append("completed")

    engine._on_workflow_started = started
    engine._release_workflow_resources = released
    engine._on_workflow_completed = completed
    context = models.WorkflowContext(
        workflow_id="workflow-1",
        session_id="session-1",
        variables={"run_id": "run-1"},
    )
    step_results = {
        "sql_generation": models.StepResult(
            step_id="sql_generation",
            status=models.StepStatus.COMPLETED,
            output={"sql": generated_sql, "description": "one"},
        ),
        "sql_verification": models.StepResult(
            step_id="sql_verification",
            status=models.StepStatus.COMPLETED,
            output={"verification_status": verification_status},
        ),
        "db_audit": models.StepResult(
            step_id="db_audit",
            status=(
                models.StepStatus(db_audit_status)
                if isinstance(db_audit_status, str)
                else db_audit_status or models.StepStatus.COMPLETED
            ),
            output=copy.deepcopy(terminal_payload),
        ),
    }

    async def execute_steps(*args, **kwargs):
        events.append("steps")
        context.step_outputs.update({
            step_id: result.output for step_id, result in step_results.items()
        })
        return step_results

    async def is_cancelled(*args, **kwargs):
        events.append("cancel-check")
        return cancelled

    engine._execute_enhanced_steps = execute_steps
    engine._is_workflow_cancelled = is_cancelled
    original_derive = engine._derive_text_to_sql_terminal_outcome

    def derive(*args, **kwargs):
        events.append("derive")
        return original_derive(*args, **kwargs)

    engine._derive_text_to_sql_terminal_outcome = derive

    class Aggregator:
        async def aggregate_final_result(self, *args, **kwargs):
            events.append("aggregate")
            if aggregation_error is not None:
                raise aggregation_error
            raw_final = (
                terminal_payload
                if aggregated_final is _USE_TERMINAL_PAYLOAD
                else aggregated_final
            )
            return {
                "type": "workflow_outputs",
                "workflow_name": workflow_name,
                "final": copy.deepcopy(raw_final),
                "outputs": {"final": copy.deepcopy(raw_final)},
            }

    class StateManager:
        async def save_checkpoint(self, **kwargs):
            checkpoints.append(kwargs)

    if real_aggregator_fallback:
        aggregator_module = importlib.import_module(
            "workflow.intelligence.aggregator"
        )
        aggregator = aggregator_module.FinalAggregator()
        real_aggregate = aggregator.aggregate_final_result

        async def tracked_aggregate(*args, **kwargs):
            events.append("aggregate")
            return await real_aggregate(*args, **kwargs)

        async def broken_outputs_mapping(*args, **kwargs):
            raise RuntimeError("real aggregator exploded")

        aggregator.aggregate_final_result = tracked_aggregate
        aggregator._aggregate_outputs_mapping = broken_outputs_mapping
        engine.aggregator = aggregator
    else:
        engine.aggregator = Aggregator()
    engine.state_manager = StateManager()
    workflow = models.WorkflowDefinition(
        name=workflow_name,
        outputs={"final": {"from_step": "db_audit", "field": "output"}},
        steps=[
            models.WorkflowStep(id="sql_generation", task="generate"),
            models.WorkflowStep(id="sql_verification", task="verify"),
            models.WorkflowStep(id="db_audit", task="finalize"),
        ],
    )
    result = asyncio.run(
        engine._execute_enhanced_workflow(workflow, context=context)
    )
    return result, events, checkpoints


def test_terminal_is_derived_before_result_aggregation():
    result, events, _ = _run_enhanced_terminal_scenario(_successful_payload())

    assert result.status.value == "completed"
    assert events.index("derive") < events.index("aggregate")
    assert events.index("derive") < events.index("completed")


def test_late_cancel_after_finalizer_preserves_actual_terminal_evidence():
    result, events, _ = _run_enhanced_terminal_scenario(
        _successful_payload(),
        cancelled=True,
    )

    assert result.status.value == "completed"
    assert result.terminal_outcome.status.value == "succeeded"
    assert result.terminal_outcome.executed is True
    assert result.terminal_outcome.audited is True
    assert "aggregate" in events


def test_text_to_sql_public_final_is_canonical_for_skipped_rejected_gate():
    abstained = _successful_payload(
        status="abstained",
        reason_code="VERIFIER_REJECTED",
        approved=False,
        executed=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
    )
    result, _, _ = _run_enhanced_terminal_scenario(
        abstained,
        verification_status="Rejected",
        db_audit_status="skipped",
        aggregated_final=_successful_payload(),
    )

    assert result.terminal_outcome.status.value == TextToSqlTerminalStatus.ABSTAINED.value
    assert result.final_output["final"] == result.terminal_outcome.to_mapping()
    assert result.final_output["outputs"]["final"] == result.terminal_outcome.to_mapping()


@pytest.mark.parametrize(
    "reason_code",
    [
        "SCHEMA_CLARIFICATION_REQUIRED",
        "SCHEMA_GROUNDING_FAILED",
        "SCHEMA_CONTEXT_BUDGET_EXCEEDED",
    ],
)
def test_enhanced_engine_preserves_schema_abstention_reason(reason_code):
    abstained = _successful_payload(
        status="abstained",
        reason_code=reason_code,
        sql="",
        generated=False,
        approved=False,
        executed=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
    )

    result, _, _ = _run_enhanced_terminal_scenario(
        abstained,
        generated_sql="",
        verification_status="Rejected",
        db_audit_status="skipped",
    )

    assert (
        result.terminal_outcome.status.value
        == TextToSqlTerminalStatus.ABSTAINED.value
    )
    assert result.terminal_outcome.reason_code == reason_code


@pytest.mark.parametrize(
    ("terminal_payload", "generated_sql", "expected_reason"),
    [
        ("agent claims success", "SELECT 1", "DB_AUDIT_OUTPUT_INVALID"),
        (_successful_payload(), "SELECT 2", "SQL_GENERATION_OUTPUT_MISMATCH"),
    ],
)
def test_text_to_sql_public_final_replaces_malformed_or_forged_db_audit(
    terminal_payload,
    generated_sql,
    expected_reason,
):
    result, _, _ = _run_enhanced_terminal_scenario(
        terminal_payload,
        generated_sql=generated_sql,
        aggregated_final=_successful_payload(),
    )

    assert result.status.value == "failed"
    assert result.terminal_outcome.reason_code == expected_reason
    assert result.final_output["final"] == result.terminal_outcome.to_mapping()
    assert result.final_output["outputs"]["final"] == result.terminal_outcome.to_mapping()


@pytest.mark.parametrize(
    ("terminal_payload", "generated_sql", "verification_status", "expected_reason"),
    [
        (
            _failed_execution_payload(),
            "SELECT 2",
            "Approved",
            "SQL_GENERATION_OUTPUT_MISMATCH",
        ),
        (
            _failed_execution_payload(),
            "SELECT 1",
            "Rejected",
            "SQL_VERIFICATION_OUTPUT_MISMATCH",
        ),
    ],
)
def test_full_engine_canonicalizes_forged_failed_terminal_outcome(
    terminal_payload,
    generated_sql,
    verification_status,
    expected_reason,
):
    result, _, _ = _run_enhanced_terminal_scenario(
        terminal_payload,
        generated_sql=generated_sql,
        verification_status=verification_status,
        aggregated_final=terminal_payload,
    )

    canonical = result.terminal_outcome.to_mapping()
    assert result.status.value == "failed"
    assert result.terminal_outcome.reason_code == expected_reason
    assert result.final_output["final"] == canonical
    assert result.final_output["outputs"]["final"] == canonical


@pytest.mark.parametrize(
    ("terminal_payload", "verification_status"),
    [
        (_failed_execution_payload(), "Approved"),
        (_audit_failed_payload(), "Approved"),
        (_verifier_contract_invalid_payload(), "Banana"),
    ],
)
def test_full_engine_preserves_legitimate_failed_terminal_outcome(
    terminal_payload,
    verification_status,
):
    result, _, _ = _run_enhanced_terminal_scenario(
        terminal_payload,
        verification_status=verification_status,
    )

    canonical = result.terminal_outcome.to_mapping()
    assert result.status.value == "failed"
    assert result.terminal_outcome.reason_code == terminal_payload["reason_code"]
    assert result.final_output["final"] == canonical
    assert result.final_output["outputs"]["final"] == canonical


@pytest.mark.parametrize("generated_sql", ["", None, 42])
def test_full_engine_fail_closed_path_handles_invalid_sql_without_throwing(generated_sql):
    result, _, _ = _run_enhanced_terminal_scenario(
        None,
        generated_sql=generated_sql,
        verification_status="Approved",
        db_audit_status="failed",
        aggregated_final=_successful_payload(),
    )

    assert result.status.value == "failed"
    assert result.terminal_outcome.status.value == TextToSqlTerminalStatus.FAILED.value
    assert result.terminal_outcome.generated is False
    assert result.terminal_outcome.approved is False
    assert result.terminal_outcome.sql == ""
    assert result.final_output["final"] == result.terminal_outcome.to_mapping()


def test_text_to_sql_public_final_mappings_do_not_share_nested_aliases():
    result, _, _ = _run_enhanced_terminal_scenario(_successful_payload())

    public_final = result.final_output["final"]
    output_final = result.final_output["outputs"]["final"]
    public_final["execution"]["data"][0][0] = 99
    public_final["audit"]["status"] = "mutated"

    assert output_final["execution"]["data"] == [[1]]
    assert output_final["audit"]["status"] == "logged"
    assert result.terminal_outcome.execution["data"] == [[1]]
    assert result.terminal_outcome.audit["status"] == "logged"


def test_generic_workflow_public_final_remains_aggregator_owned():
    raw_final = {"custom": {"answer": 42}}
    result, _, _ = _run_enhanced_terminal_scenario(
        raw_final,
        aggregated_final=raw_final,
        workflow_name="generic_pipeline",
    )

    assert result.status.value == "completed"
    assert result.terminal_outcome is None
    assert result.final_output["final"] == raw_final


@pytest.mark.parametrize(
    ("terminal_payload", "expected_reason"),
    [
        (_successful_payload(), "RESULT_AGGREGATION_FAILED"),
        (
            _successful_payload(
                status="failed",
                reason_code="EXECUTION_FAILED",
                executed=True,
                data=[],
                columns=[],
                rows_affected=0,
                error="database failed",
                execution={
                    "success": False,
                    "data": [],
                    "columns": [],
                    "rows_affected": 0,
                    "execution_time_ms": 0,
                    "dry_run_only": False,
                    "skipped_execution": False,
                    "error_message": "database failed",
                },
                persistence={"status": "not_attempted"},
            ),
            "EXECUTION_FAILED",
        ),
    ],
)
def test_aggregation_failure_preserves_runtime_evidence_and_checkpoints(
    terminal_payload,
    expected_reason,
):
    result, events, checkpoints = _run_enhanced_terminal_scenario(
        terminal_payload,
        aggregation_error=RuntimeError("aggregator exploded"),
    )

    assert result.status.value == "failed"
    assert result.terminal_outcome.reason_code == expected_reason
    assert result.terminal_outcome.audited is terminal_payload["audited"]
    assert result.terminal_outcome.execution == terminal_payload["execution"]
    assert result.final_output["final"] == result.terminal_outcome.to_mapping()
    assert checkpoints
    assert "completed" not in events


def test_real_aggregator_fallback_is_terminal_aggregation_failure():
    result, events, checkpoints = _run_enhanced_terminal_scenario(
        _successful_payload(),
        real_aggregator_fallback=True,
    )

    assert result.status.value == "failed"
    assert result.terminal_outcome.reason_code == "RESULT_AGGREGATION_FAILED"
    assert result.terminal_outcome.executed is True
    assert result.terminal_outcome.audited is True
    assert result.final_output["type"] == "workflow_outputs"
    assert result.final_output["final"] == result.terminal_outcome.to_mapping()
    assert checkpoints
    assert events.index("derive") < events.index("aggregate")
    assert "completed" not in events


def test_terminal_payload_is_json_serializable():
    outcome = TextToSqlTerminalResult.from_mapping(_successful_payload())
    assert json.loads(json.dumps(outcome.to_mapping()))["status"] == "succeeded"
