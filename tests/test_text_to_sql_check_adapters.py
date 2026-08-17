"""W5-04 strict adapters for existing deterministic checker outputs."""

from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
import subprocess
import sys
from typing import Literal

import pytest
from text_to_sql_semantic_checks_helpers import (
    POSTGRES_DSN,
    ItemSpec,
    build_case,
)

from custom_tools.text_to_sql.adaptive.checks import (
    DeterministicCheckInput,
    DeterministicCheckInputError,
    ExecutionCheckEvidence,
    SemanticCheckInput,
    adapt_execution_check_result,
    adapt_explain_check_result,
    adapt_safety_check_result,
    adapt_schema_check_result,
    adapt_semantic_authority_check_result,
    require_authenticated_semantic_input,
    require_typed_check_result,
)
from custom_tools.text_to_sql.adaptive._exact_contract import (
    ExactContractError,
    require_exact_dataclass,
)
from custom_tools.text_to_sql.adaptive._check_contract import (
    FormulaSemanticCertificate,
)
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    CheckRepair,
    CheckResult,
    CheckStatus,
    PredicateOperator,
    RepairKind,
    SemanticItemKind,
    SqlCandidate,
)
from custom_tools.text_to_sql.adaptive.semantic_checks import (
    evaluate_semantic_authority_checks,
)
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from custom_tools.text_to_sql.core._db_exec import (
    QueryExecutionResult,
    QueryPurpose,
)


class _SneakyStr(str):
    pass


class _AdapterEnum(str, Enum):
    INNER = "inner"


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_checks_import_does_not_load_dormant_solver_modules():
    script = """
import sys
import types
from pathlib import Path

core_name = "custom_tools.text_to_sql.core"
core_package = types.ModuleType(core_name)
core_package.__path__ = [str(Path.cwd() / "custom_tools/text_to_sql/core")]
sys.modules[core_name] = core_package
import custom_tools.text_to_sql.adaptive.checks

for module_name in (
    "custom_tools.text_to_sql.adaptive.solver_loop",
    "custom_tools.text_to_sql.adaptive.solver_protocol",
):
    assert module_name not in sys.modules, module_name
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _candidate():
    return _parsed_candidate("SELECT o.id FROM orders o", "candidate-1")[0]


def _parsed_candidate(sql: str, candidate_id: str):
    parsed_ast = parse_sql_candidate(sql, POSTGRES_DSN, candidate_id)
    return (
        SqlCandidate(
            candidate_id=candidate_id,
            sql=sql,
            normalized_ast_digest=parsed_ast.candidate_digest,
            revision=1,
        ),
        parsed_ast,
    )


def _join_candidate():
    return _parsed_candidate(
        "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id",
        "candidate-join",
    )


def _forged_str_enum(member):
    forged = str.__new__(type(member), member.value)
    forged._name_ = member.name
    forged._value_ = member.value
    assert forged is not member
    assert forged == member
    assert hash(forged) == hash(member)
    return forged


def _forged_str_enum_without_name(member):
    forged = str.__new__(type(member), member.value)
    forged._value_ = member.value
    assert not hasattr(forged, "_name_")
    return forged


@dataclass(frozen=True, slots=True)
class _EnumAuthority:
    value: _AdapterEnum


@dataclass(frozen=True, slots=True)
class _LiteralEnumAuthority:
    value: Literal[_AdapterEnum.INNER]


_HOSTILE_CALLBACKS = {"eq": 0, "hash": 0}


class _HostileLiteralEnum(str, Enum):
    MEMBER = "member"

    def __eq__(self, other):
        _HOSTILE_CALLBACKS["eq"] += 1
        raise AssertionError("enum equality callback must not run")

    def __hash__(self):
        _HOSTILE_CALLBACKS["hash"] += 1
        return str.__hash__(self)


@dataclass(frozen=True, slots=True)
class _HostileLiteralAuthority:
    value: Literal[_HostileLiteralEnum.MEMBER]


def _safety_result(**changes):
    result = {
        "is_safe": True,
        "issues": [],
        "advisory_issues": [],
        "safety_status": "safe",
        "llm_audit": "ok",
    }
    result.update(changes)
    return result


def _schema_result(**changes):
    result = {"is_valid": True, "issues": []}
    result.update(changes)
    return result


def _explain_result(**changes):
    result = {
        "plan": "SCAN orders",
        "estimated_cost": 10.0,
        "rows_to_scan": None,
        "issues": [],
        "profile_name": "default",
        "policy_version": "v1",
    }
    result.update(changes)
    return result


def _execution_result(**changes):
    outcome = {
        "success": True,
        "data": [[1]],
        "columns": ["id"],
        "rows_affected": 1,
        "execution_time_ms": 7,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": _candidate().sql,
        "applied_row_limit": 10,
    }
    outcome.update(changes)
    return QueryExecutionResult(QueryPurpose.FINAL, outcome)


def test_execution_adapter_maps_valid_success_and_failure_without_raw_error():
    passed = adapt_execution_check_result(
        _candidate(),
        _execution_result(),
        execution_id="execution-1",
        expected_row_limit=10,
        expected_dry_run_only=False,
    )
    failed = adapt_execution_check_result(
        _candidate(),
        _execution_result(
            success=False,
            data=[],
            columns=[],
            rows_affected=0,
            error_message="customer secret must not cross the adapter",
        ),
        execution_id="execution-2",
        expected_row_limit=10,
        expected_dry_run_only=False,
    )

    assert isinstance(passed, ExecutionCheckEvidence)
    assert passed.check_result.status is CheckStatus.PASSED
    assert passed.execution_result is not None
    assert passed.execution_result.success is True
    assert passed.execution_result.row_count == 1
    assert passed.execution_result.elapsed_ms == 7
    assert failed.check_result.status is CheckStatus.FAILED
    assert failed.check_result.failure_code is CheckFailureCode.EXECUTION_REJECTED
    assert failed.execution_result is not None
    assert failed.execution_result.error_code == "EXECUTION_REJECTED"
    assert "customer secret" not in repr(failed)


def test_execution_adapter_fail_closes_malformed_or_forged_native_identity():
    wrong_purpose = QueryExecutionResult(
        QueryPurpose.EXPLAIN,
        _execution_result().to_mapping(),
    )
    wrong_sql = _execution_result(sql_query="SELECT secret FROM other_table")

    for native in (wrong_purpose, wrong_sql):
        evidence = adapt_execution_check_result(
            _candidate(),
            native,
            execution_id="execution-1",
            expected_row_limit=10,
            expected_dry_run_only=False,
        )
        assert evidence.check_result.status is CheckStatus.INCONCLUSIVE
        assert evidence.check_result.failure_code is CheckFailureCode.CHECK_MALFORMED
        assert evidence.execution_result is None


def test_execution_adapter_accepts_contract_valid_dry_run_success():
    evidence = adapt_execution_check_result(
        _candidate(),
        _execution_result(
            data=[],
            columns=[],
            rows_affected=0,
            dry_run_only=True,
            skipped_execution=True,
        ),
        execution_id="execution-1",
        expected_row_limit=10,
        expected_dry_run_only=True,
    )

    assert evidence.check_result.status is CheckStatus.PASSED
    assert evidence.execution_result is not None
    assert evidence.execution_result.success is True


@pytest.mark.parametrize("forgery", ("scalar_subclass", "hidden_attribute"))
def test_candidate_adapter_rejects_model_copy_and_hidden_forgery(forgery):
    candidate = _candidate()
    if forgery == "scalar_subclass":
        candidate = candidate.model_copy(
            update={"candidate_id": _SneakyStr(candidate.candidate_id)}
        )
    else:
        candidate.__dict__["hidden"] = "forged"

    with pytest.raises(DeterministicCheckInputError):
        adapt_safety_check_result(candidate, _safety_result())


@pytest.mark.parametrize("forgery", ("scalar_subclass", "hidden_attribute"))
def test_typed_check_rejects_model_copy_and_hidden_forgery(forgery):
    result = CheckResult(
        check_id="semantic:candidate-1:passed",
        candidate_id="candidate-1",
        check_kind=CheckKind.SEMANTIC,
        status=CheckStatus.PASSED,
        failure_code=None,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=None,
    )
    if forgery == "scalar_subclass":
        result = result.model_copy(update={"check_id": _SneakyStr(result.check_id)})
    else:
        result.__dict__["hidden"] = "forged"

    with pytest.raises(DeterministicCheckInputError):
        require_typed_check_result(result)


def test_safety_adapter_preserves_pass_and_advisory_semantics_without_mutation():
    native = _safety_result(
        advisory_issues=[
            {
                "issue_type": "LLM_STYLE",
                "description": "Consider a smaller query",
                "blocking": False,
            }
        ]
    )
    before = deepcopy(native)

    first = adapt_safety_check_result(_candidate(), native)
    second = adapt_safety_check_result(_candidate(), native)

    assert first == second
    assert first.status is CheckStatus.PASSED
    assert first.check_id == "safety:candidate-1:passed"
    assert native == before


def test_safety_adapter_accepts_only_complete_static_native_metadata():
    static_result = _safety_result(
        layer="static",
        profile_name="default",
        policy_version="v1",
    )

    assert adapt_safety_check_result(_candidate(), static_result).status is CheckStatus.PASSED

    malformed = (
        _safety_result(
            layer="static",
            profile_name="default",
            policy_version="v1",
            unknown=True,
        ),
        _safety_result(layer="static"),
        _safety_result(layer="static", profile_name="default"),
        _safety_result(
            layer="dynamic",
            profile_name="default",
            policy_version="v1",
        ),
    )
    for native in malformed:
        result = adapt_safety_check_result(_candidate(), native)
        assert result.status is CheckStatus.INCONCLUSIVE
        assert result.failure_code is CheckFailureCode.CHECK_MALFORMED


def test_safety_adapter_maps_confirmed_blocking_failure():
    native = _safety_result(
        is_safe=False,
        safety_status="unsafe",
        llm_audit="skipped_static_unsafe",
        issues=[
            {
                "issue_type": "FORBIDDEN_COMMAND",
                "description": "DROP is forbidden",
            }
        ],
    )

    result = adapt_safety_check_result(_candidate(), native)

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.SAFETY_REJECTED
    assert result.observed_error == "FORBIDDEN_COMMAND"
    assert result.repair == CheckRepair(kind=RepairKind.REVISE_SQL)


def test_safety_adapter_maps_structured_timeout_only_to_inconclusive():
    native = _safety_result(
        is_safe=False,
        safety_status="failed",
        llm_audit="timeout",
        llm_audit_error="deadline",
        issues=[
            {
                "issue_type": "LLM_AUDIT_TIMEOUT",
                "description": "deadline",
            }
        ],
    )

    result = adapt_safety_check_result(_candidate(), native)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_TIMEOUT
    assert result.observed_error == "LLM_AUDIT_TIMEOUT"


def test_safety_adapter_maps_canonical_llm_failure_to_rejected():
    native = _safety_result(
        is_safe=False,
        safety_status="failed",
        llm_audit="failed",
        llm_audit_error="unavailable",
        issues=[
            {
                "issue_type": "LLM_AUDIT_FAILED",
                "description": "unavailable",
            }
        ],
    )

    result = adapt_safety_check_result(_candidate(), native)

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.SAFETY_REJECTED
    assert result.observed_error == "LLM_AUDIT_FAILED"


@pytest.mark.parametrize(
    ("audit", "issue_type"),
    (
        ("failed", "LLM_AUDIT_TIMEOUT"),
        ("timeout", "LLM_AUDIT_FAILED"),
    ),
)
def test_safety_adapter_rejects_contradictory_llm_audit_issue_pair(
    audit: str,
    issue_type: str,
):
    native = _safety_result(
        is_safe=False,
        safety_status="failed",
        llm_audit=audit,
        llm_audit_error="runtime failure",
        issues=[{"issue_type": issue_type, "description": "runtime failure"}],
    )

    result = adapt_safety_check_result(_candidate(), native)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_MALFORMED


def test_safety_adapter_does_not_hide_blocking_issue_behind_timeout():
    native = _safety_result(
        is_safe=False,
        safety_status="failed",
        llm_audit="timeout",
        llm_audit_error="deadline",
        issues=[
            {"issue_type": "LLM_AUDIT_TIMEOUT", "description": "deadline"},
            {"issue_type": "FORBIDDEN_COMMAND", "description": "blocked"},
        ],
    )

    result = adapt_safety_check_result(_candidate(), native)

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.SAFETY_REJECTED
    assert result.observed_error == "FORBIDDEN_COMMAND,LLM_AUDIT_TIMEOUT"


@pytest.mark.parametrize("value", (0, 1, "false"))
def test_safety_adapter_rejects_truthy_or_falsey_non_boolean(value):
    result = adapt_safety_check_result(
        _candidate(),
        _safety_result(is_safe=value),
    )

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_MALFORMED
    assert result.observed_error == "invalid safety checker result"


def test_schema_adapter_maps_pass_failure_skip_and_timeout():
    passed = adapt_schema_check_result(_candidate(), _schema_result())
    failed = adapt_schema_check_result(
        _candidate(),
        _schema_result(
            is_valid=False,
            issues=[{"issue_type": "UNKNOWN_TABLE", "description": "missing"}],
        ),
    )
    skipped = adapt_schema_check_result(
        _candidate(),
        _schema_result(
            schema_check_skipped=True,
            skip_reason="empty_schema",
        ),
    )
    timeout = adapt_schema_check_result(
        _candidate(),
        _schema_result(
            is_valid=False,
            issues=[{"issue_type": "SQL_PARSE_TIMEOUT", "description": "deadline"}],
        ),
    )

    assert passed.status is CheckStatus.PASSED
    assert failed.failure_code is CheckFailureCode.SCHEMA_REJECTED
    assert failed.observed_error == "UNKNOWN_TABLE"
    assert skipped.status is CheckStatus.INCONCLUSIVE
    assert skipped.failure_code is CheckFailureCode.CHECK_MALFORMED
    assert skipped.observed_error == "SCHEMA_CHECK_SKIPPED:empty_schema"
    assert timeout.failure_code is CheckFailureCode.CHECK_TIMEOUT


@pytest.mark.parametrize(
    "native",
    (
        {"is_valid": 1, "issues": []},
        {"is_valid": True, "issues": ()},
        {"is_valid": True, "issues": [], "unknown": True},
        {
            "is_valid": True,
            "issues": [{"issue_type": "UNKNOWN_TABLE", "description": "missing"}],
        },
    ),
)
def test_schema_adapter_rejects_malformed_native_shapes(native):
    result = adapt_schema_check_result(_candidate(), native)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_MALFORMED


def test_explain_adapter_keeps_full_scan_advisory_non_blocking():
    native = _explain_result(
        issues=[{"issue_type": "FULL_SCAN", "description": "scan"}]
    )
    before = deepcopy(native)

    result = adapt_explain_check_result(_candidate(), native)

    assert result.status is CheckStatus.PASSED
    assert native == before


def test_explain_adapter_maps_blocking_failure_and_timeout():
    failed = adapt_explain_check_result(
        _candidate(),
        _explain_result(
            plan=None,
            estimated_cost=None,
            issues=[
                {"issue_type": "EXPLAIN_UNSUPPORTED", "description": "unsupported"}
            ],
        ),
    )
    timeout = adapt_explain_check_result(
        _candidate(),
        _explain_result(
            plan=None,
            estimated_cost=None,
            issues=[{"issue_type": "EXPLAIN_TIMEOUT", "description": "deadline"}],
        ),
    )

    assert failed.status is CheckStatus.FAILED
    assert failed.failure_code is CheckFailureCode.EXPLAIN_REJECTED
    assert timeout.status is CheckStatus.INCONCLUSIVE
    assert timeout.failure_code is CheckFailureCode.CHECK_TIMEOUT


def test_explain_adapter_does_not_treat_dry_run_skip_as_passed():
    native = _explain_result(
        plan=None,
        estimated_cost=None,
        dry_run_only=True,
        skipped_execution=True,
        sql_query="SELECT o.id FROM orders o",
    )

    result = adapt_explain_check_result(_candidate(), native)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_MALFORMED
    assert result.observed_error == "EXPLAIN_SKIPPED_DRY_RUN"


@pytest.mark.parametrize(
    "native",
    (
        {"plan": "SCAN", "issues": []},
        _explain_result(estimated_cost=True),
        _explain_result(rows_to_scan=True),
        _explain_result(plan=None, estimated_cost=None),
    ),
)
def test_explain_adapter_rejects_malformed_native_shapes(native):
    result = adapt_explain_check_result(_candidate(), native)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_MALFORMED


def _semantic_case(sql: str, *, filter_required: bool = False):
    item = ItemSpec(
        source_id="status" if filter_required else "id",
        kind=(
            SemanticItemKind.FILTER if filter_required else SemanticItemKind.DIMENSION
        ),
        table="orders",
        column="status" if filter_required else "id",
        operator=PredicateOperator.EQ if filter_required else None,
        literal="active" if filter_required else None,
    )
    return build_case(sql, (item,))


@pytest.mark.parametrize("authority", ("candidate", "parsed_ast"))
def test_deterministic_input_rejects_nested_exact_contract_forgery(authority):
    case = _semantic_case("SELECT o.id FROM orders o")
    semantic_ast = case.check_input.semantic_ast
    candidate = semantic_ast.candidate
    parsed_ast = semantic_ast.parsed_ast
    if authority == "candidate":
        candidate = candidate.model_copy(
            update={"candidate_id": _SneakyStr(candidate.candidate_id)}
        )
    else:
        scan = parsed_ast.table_scans[0]
        parsed_ast = replace(
            parsed_ast,
            table_scans=(
                replace(scan, node_id=_SneakyStr(scan.node_id)),
                *parsed_ast.table_scans[1:],
            ),
        )

    with pytest.raises(DeterministicCheckInputError):
        DeterministicCheckInput(
            candidate=candidate,
            parsed_ast=parsed_ast,
        )


def test_deterministic_input_rejects_forged_nested_dataclass_enum():
    candidate, parsed_ast = _join_candidate()
    join = parsed_ast.joins[0]
    parsed_ast = replace(
        parsed_ast,
        joins=(
            replace(join, output_visible=_forged_str_enum(_AdapterEnum.INNER)),
            *parsed_ast.joins[1:],
        ),
    )

    with pytest.raises(DeterministicCheckInputError):
        DeterministicCheckInput(
            candidate=candidate,
            parsed_ast=parsed_ast,
        )


def test_deterministic_input_rejects_forged_enum_without_name():
    candidate, parsed_ast = _join_candidate()
    join = parsed_ast.joins[0]
    parsed_ast = replace(
        parsed_ast,
        joins=(
            replace(
                join,
                output_visible=_forged_str_enum_without_name(_AdapterEnum.INNER),
            ),
            *parsed_ast.joins[1:],
        ),
    )

    with pytest.raises(DeterministicCheckInputError):
        DeterministicCheckInput(
            candidate=candidate,
            parsed_ast=parsed_ast,
        )


def test_exact_enum_rejects_mutated_canonical_value_and_restores_member():
    member = _AdapterEnum.INNER
    original_value = member._value_
    object.__setattr__(member, "_value_", "mutated-inner")
    try:
        with pytest.raises(ExactContractError):
            require_exact_dataclass(
                _EnumAuthority(member),
                _EnumAuthority,
                ExactContractError,
                "enum authority",
            )
    finally:
        object.__setattr__(member, "_value_", original_value)

    assert (
        require_exact_dataclass(
            _EnumAuthority(member),
            _EnumAuthority,
            ExactContractError,
            "enum authority",
        ).value
        is member
    )


def test_literal_enum_rejects_forged_exact_type_member():
    with pytest.raises(ExactContractError):
        require_exact_dataclass(
            _LiteralEnumAuthority(_forged_str_enum(_AdapterEnum.INNER)),
            _LiteralEnumAuthority,
            ExactContractError,
            "literal enum authority",
        )


def test_literal_enum_rejects_without_equality_or_hash_callbacks():
    forged = str.__new__(_HostileLiteralEnum, "member")
    forged._name_ = "MEMBER"
    forged._value_ = "member"
    _HOSTILE_CALLBACKS.update(eq=0, hash=0)

    with pytest.raises(ExactContractError):
        require_exact_dataclass(
            _HostileLiteralAuthority(forged),
            _HostileLiteralAuthority,
            ExactContractError,
            "hostile literal enum authority",
        )

    assert _HOSTILE_CALLBACKS == {"eq": 0, "hash": 0}


@pytest.mark.parametrize(
    "authority",
    ("semantic_ast", "query_spec", "requirements"),
)
def test_semantic_input_rejects_nested_exact_contract_forgery(authority):
    case = _semantic_case("SELECT o.id FROM orders o")
    semantic_ast = case.check_input.semantic_ast
    query_spec = case.query_spec
    requirements = case.requirements
    if authority == "semantic_ast":
        annotation = semantic_ast.coverage.annotations[0]
        coverage = semantic_ast.coverage.model_copy(
            update={
                "annotations": (
                    annotation.model_copy(
                        update={"node_id": _SneakyStr(annotation.node_id)}
                    ),
                    *semantic_ast.coverage.annotations[1:],
                )
            }
        )
        semantic_ast = replace(semantic_ast, coverage=coverage)
    elif authority == "query_spec":
        item = query_spec.semantic_items[0]
        query_spec = query_spec.model_copy(
            update={
                "semantic_items": (
                    item.model_copy(update={"source_id": _SneakyStr(item.source_id)}),
                )
            }
        )
    else:
        binding = requirements.selected_bindings[0]
        requirements = requirements.model_copy(
            update={
                "selected_bindings": (
                    binding.model_copy(
                        update={"source_id": _SneakyStr(binding.source_id)}
                    ),
                )
            }
        )

    forged = SemanticCheckInput(
        semantic_ast=semantic_ast,
        query_spec=query_spec,
        requirements=requirements,
    )

    with pytest.raises(DeterministicCheckInputError):
        require_authenticated_semantic_input(forged)


def test_execution_evidence_rejects_nested_execution_result_forgery():
    evidence = adapt_execution_check_result(
        _candidate(),
        _execution_result(),
        execution_id="execution-1",
        expected_row_limit=10,
        expected_dry_run_only=False,
    )
    assert evidence.execution_result is not None
    forged_execution = evidence.execution_result.model_copy(
        update={"execution_id": _SneakyStr(evidence.execution_result.execution_id)}
    )

    with pytest.raises(TypeError):
        ExecutionCheckEvidence(evidence.check_result, forged_execution)


def test_semantic_authority_adapter_rejects_recursive_model_copy_forgery():
    case = _semantic_case(
        "SELECT o.status FROM orders o WHERE o.status = 'forged'",
        filter_required=True,
    )
    native = evaluate_semantic_authority_checks(case.check_input, case.state, POSTGRES_DSN)
    assert native.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL
    assert native.repair is not None
    forged = native.model_copy(
        update={"repair": native.repair.model_copy(update={"kind": "forged"})}
    )

    result = adapt_semantic_authority_check_result(case.check_input, forged)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_MALFORMED
    with pytest.raises(DeterministicCheckInputError):
        require_typed_check_result(forged)


def test_semantic_authority_adapter_rejects_forged_authenticated_input():
    case = _semantic_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active' AND o.status = 'forged'",
        filter_required=True,
    )
    native = evaluate_semantic_authority_checks(case.check_input, case.state, POSTGRES_DSN)
    assert native.status is CheckStatus.FAILED
    assert native.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL
    assert adapt_semantic_authority_check_result(case.check_input, native) == native
    semantic_ast = case.check_input.semantic_ast
    annotation = semantic_ast.coverage.annotations[0]
    forged_coverage = semantic_ast.coverage.model_copy(
        update={
            "annotations": (
                annotation.model_copy(update={"source_ids": ()}),
                *semantic_ast.coverage.annotations[1:],
            )
        }
    )
    forged_input = SemanticCheckInput(
        semantic_ast=replace(semantic_ast, coverage=forged_coverage),
        query_spec=case.query_spec,
        requirements=case.requirements,
    )

    result = adapt_semantic_authority_check_result(forged_input, native)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_MALFORMED


def test_semantic_authority_adapter_rejects_formula_certificate():
    case = _semantic_case("SELECT o.id FROM orders o")
    native = evaluate_semantic_authority_checks(case.check_input, case.state, POSTGRES_DSN)
    forged = native.model_copy(
        update={
            "formula_certificate": FormulaSemanticCertificate(
                candidate_digest=case.check_input.candidate.normalized_ast_digest,
                entries=(),
            )
        }
    )

    result = adapt_semantic_authority_check_result(case.check_input, forged)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_MALFORMED


def test_semantic_authority_adapter_preserves_expression_relation_failure():
    case = _semantic_case(
        "SELECT u.value FROM orders o CROSS JOIN "
        "UNNEST(ARRAY[o.secret]) AS u(value)"
    )
    native = evaluate_semantic_authority_checks(case.check_input, case.state, POSTGRES_DSN)

    assert native.status is CheckStatus.FAILED
    assert native.failure_code is CheckFailureCode.UNAUTHORIZED_COLUMN
    assert native.affected_ast_node_ids == (
        case.check_input.parsed_ast.expression_relations[0].node_id,
    )

    result = adapt_semantic_authority_check_result(case.check_input, native)

    assert result == native
