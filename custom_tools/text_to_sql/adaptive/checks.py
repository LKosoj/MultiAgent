"""Strict typed inputs and pure adapters for deterministic SQL checks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import TypeAdapter, ValidationError

from workflow.text_to_sql_contract import text_to_sql_executor_contract_error

from ._exact_contract import ExactContractError, require_exact_dataclass, revalidate_exact_model
from ._sql_ast_identity import semantic_candidate_digest, source_sql_digest
from ._sql_ast_models import ParsedSqlCandidate
from .models import (
    CheckFailureCode,
    CheckKind,
    CheckRepair,
    CheckResult,
    CheckStatus,
    ExecutionResult,
    Id,
    QuerySpec,
    RepairKind,
    SqlCandidate,
)
from .semantic_coverage import CoverageRequirements
from .semantic_plan import (
    AuthenticatedSemanticAst,
    authenticate_semantic_ast,
)

if TYPE_CHECKING:
    from ..core._db_exec import QueryExecutionResult


class DeterministicCheckInputError(ValueError):
    """Закрытый отказ входной границы deterministic check."""

    def __init__(self, code: CheckFailureCode) -> None:
        if code is not CheckFailureCode.CHECK_INPUT_INVALID:
            raise TypeError("code must be a deterministic input failure code")
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class DeterministicCheckInput:
    candidate: SqlCandidate
    parsed_ast: ParsedSqlCandidate

    def __post_init__(self) -> None:
        if (
            type(self.candidate) is not SqlCandidate
            or type(self.parsed_ast) is not ParsedSqlCandidate
        ):
            raise DeterministicCheckInputError(CheckFailureCode.CHECK_INPUT_INVALID)
        _exact_contract(self.candidate, SqlCandidate, "candidate")
        _exact_dataclass_contract(
            self.parsed_ast,
            ParsedSqlCandidate,
            "parsed_ast",
        )
        if (
            self.parsed_ast.candidate_id != self.candidate.candidate_id
            or self.parsed_ast.source_sql_digest != source_sql_digest(self.candidate.sql)
            or self.parsed_ast.candidate_digest != semantic_candidate_digest(self.parsed_ast)
            or self.candidate.normalized_ast_digest != self.parsed_ast.candidate_digest
        ):
            raise DeterministicCheckInputError(
                CheckFailureCode.CHECK_INPUT_INVALID
            )


@dataclass(frozen=True, slots=True)
class SemanticCheckInput:
    """Typed semantic authority, separate from the structural W5-01 seam."""

    semantic_ast: AuthenticatedSemanticAst
    query_spec: QuerySpec
    requirements: CoverageRequirements

    def __post_init__(self) -> None:
        if (
            type(self.semantic_ast) is not AuthenticatedSemanticAst
            or type(self.query_spec) is not QuerySpec
            or type(self.requirements) is not CoverageRequirements
        ):
            raise DeterministicCheckInputError(CheckFailureCode.CHECK_INPUT_INVALID)

    @property
    def candidate(self) -> SqlCandidate:
        return self.semantic_ast.candidate

    @property
    def parsed_ast(self) -> ParsedSqlCandidate:
        return self.semantic_ast.parsed_ast


@dataclass(frozen=True, slots=True)
class ExecutionCheckEvidence:
    """A final execution check plus evidence when its identity is trusted."""

    check_result: CheckResult
    execution_result: ExecutionResult | None

    def __post_init__(self) -> None:
        if type(self.check_result) is not CheckResult or (
            self.execution_result is not None
            and type(self.execution_result) is not ExecutionResult
        ):
            raise TypeError("ExecutionCheckEvidence requires exact contract types")
        _exact_contract(self.check_result, CheckResult, "check_result")
        if self.execution_result is not None:
            try:
                revalidate_exact_model(
                    self.execution_result,
                    ExecutionResult,
                    ExactContractError,
                    "execution_result",
                )
            except ExactContractError as exc:
                raise TypeError(
                    "ExecutionCheckEvidence requires exact contract types"
                ) from exc


class DeterministicCheck(Protocol):
    def __call__(self, value: DeterministicCheckInput) -> CheckResult: ...


def require_authenticated_semantic_input(
    value: SemanticCheckInput,
) -> SemanticCheckInput:
    """Authenticate the overlay exactly without parsing or deriving W4 again."""

    if type(value) is not SemanticCheckInput:
        raise DeterministicCheckInputError(CheckFailureCode.CHECK_INPUT_INVALID)
    try:
        _exact_dataclass_contract(value, SemanticCheckInput, "semantic input")
        authenticate_semantic_ast(
            value.semantic_ast,
            value.query_spec,
            value.requirements,
        )
    except Exception as exc:
        raise DeterministicCheckInputError(
            CheckFailureCode.CHECK_INPUT_INVALID
        ) from exc
    return value


def require_typed_check_result(result: CheckResult) -> CheckResult:
    """Не пропустить legacy free-text repair на новую typed seam."""

    checked = _exact_contract(result, CheckResult, "check_result")
    if checked.required_change is not None:
        raise DeterministicCheckInputError(CheckFailureCode.CHECK_INPUT_INVALID)
    return checked


_ISSUE_KEYS = frozenset({"issue_type", "description"})
_ADVISORY_ISSUE_KEYS = frozenset({"issue_type", "description", "blocking"})
_TIMEOUT_ISSUES = frozenset(
    {"LLM_AUDIT_TIMEOUT", "SQL_PARSE_TIMEOUT", "EXPLAIN_TIMEOUT"}
)


def adapt_safety_check_result(
    candidate: SqlCandidate,
    native_result: object,
) -> CheckResult:
    candidate_id = _candidate_id(candidate)
    try:
        result = _exact_mapping(
            native_result,
            required={
                "is_safe",
                "issues",
                "advisory_issues",
                "safety_status",
                "llm_audit",
            },
            optional={
                "llm_audit_error",
                "layer",
                "profile_name",
                "policy_version",
            },
            paired_optional={"layer", "profile_name", "policy_version"},
        )
        is_safe = result["is_safe"]
        if type(is_safe) is not bool:
            raise TypeError("is_safe must be a boolean")
        issue_types = _issue_types(result["issues"])
        advisory_issue_types = _issue_types(result["advisory_issues"], advisory=True)
        status = result["safety_status"]
        audit = result["llm_audit"]
        if type(status) is not str or type(audit) is not str:
            raise TypeError("safety status fields must be text")
        has_audit_error = "llm_audit_error" in result
        if has_audit_error != (audit in {"failed", "timeout"}) or (
            has_audit_error
            and (
                type(result["llm_audit_error"]) is not str
                or not result["llm_audit_error"].strip()
            )
        ):
            raise ValueError("llm_audit_error contradicts llm_audit")
        if "layer" in result and (
            type(result["layer"]) is not str
            or result["layer"] != "static"
            or type(result["profile_name"]) is not str
            or not result["profile_name"].strip()
            or type(result["policy_version"]) is not str
            or not result["policy_version"].strip()
        ):
            raise ValueError("static safety metadata is invalid")
        # W0-0.5: продюсер (_sql_generation_api.py::sql_safety_check) устроен
        # по принципу fail-open — сбой самого LLM-аудита живёт ТОЛЬКО как
        # non-blocking LLM_AUDIT_FAILED/LLM_AUDIT_TIMEOUT в advisory_issues,
        # а не как блокирующий issue.
        advisory_llm_types = set(advisory_issue_types).intersection(
            {"LLM_AUDIT_FAILED", "LLM_AUDIT_TIMEOUT"}
        )
        expected_advisory_llm_types = {
            "failed": {"LLM_AUDIT_FAILED"},
            "timeout": {"LLM_AUDIT_TIMEOUT"},
        }.get(audit, set())
        if advisory_llm_types != expected_advisory_llm_types:
            raise ValueError("LLM audit state contradicts its advisory issue type")
        if is_safe:
            if (
                issue_types
                or status != "safe"
                or audit
                not in {
                    "ok",
                    "skipped_static_only",
                    "failed",
                    "timeout",
                }
            ):
                raise ValueError("safe result carries a blocking failure")
            return _passed(candidate_id, CheckKind.SAFETY)
        if (
            not issue_types
            or (status, audit) != ("unsafe", "skipped_static_unsafe")
            or set(issue_types).intersection(
                {"LLM_AUDIT_FAILED", "LLM_AUDIT_TIMEOUT"}
            )
        ):
            raise ValueError("unsafe result lacks the native static-reject state")
        return _failed(
            candidate_id,
            CheckKind.SAFETY,
            CheckFailureCode.SAFETY_REJECTED,
            _summary(issue_types),
        )
    except (TypeError, ValueError):
        return _malformed(candidate_id, CheckKind.SAFETY)


def adapt_schema_check_result(
    candidate: SqlCandidate,
    native_result: object,
) -> CheckResult:
    candidate_id = _candidate_id(candidate)
    try:
        result = _exact_mapping(
            native_result,
            required={"is_valid", "issues"},
            optional={"schema_check_skipped", "skip_reason"},
            paired_optional={"schema_check_skipped", "skip_reason"},
        )
        is_valid = result["is_valid"]
        if type(is_valid) is not bool:
            raise TypeError("is_valid must be a boolean")
        issue_types = _issue_types(result["issues"])
        if "schema_check_skipped" in result:
            if (
                result["schema_check_skipped"] is not True
                or type(result["skip_reason"]) is not str
                or not result["skip_reason"].strip()
                or not is_valid
                or issue_types
            ):
                raise ValueError("schema skip fields are contradictory")
            return _inconclusive(
                candidate_id,
                CheckKind.SCHEMA,
                CheckFailureCode.CHECK_MALFORMED,
                f"SCHEMA_CHECK_SKIPPED:{result['skip_reason']}",
            )
        if is_valid:
            if issue_types:
                raise ValueError("valid schema result carries issues")
            return _passed(candidate_id, CheckKind.SCHEMA)
        if not issue_types:
            raise ValueError("invalid schema result lacks issues")
        if _timeout_only(issue_types):
            return _inconclusive(
                candidate_id,
                CheckKind.SCHEMA,
                CheckFailureCode.CHECK_TIMEOUT,
                _summary(issue_types),
            )
        return _failed(
            candidate_id,
            CheckKind.SCHEMA,
            CheckFailureCode.SCHEMA_REJECTED,
            _summary(issue_types),
        )
    except (TypeError, ValueError):
        return _malformed(candidate_id, CheckKind.SCHEMA)


def adapt_explain_check_result(
    candidate: SqlCandidate,
    native_result: object,
) -> CheckResult:
    candidate_id = _candidate_id(candidate)
    try:
        result = _exact_mapping(
            native_result,
            required={
                "plan",
                "estimated_cost",
                "rows_to_scan",
                "issues",
                "profile_name",
                "policy_version",
            },
            optional={"dry_run_only", "skipped_execution", "sql_query"},
            paired_optional={"dry_run_only", "skipped_execution", "sql_query"},
        )
        plan = result["plan"]
        if plan is not None and (type(plan) is not str or not plan.strip()):
            raise TypeError("plan must be non-empty text or None")
        _optional_non_negative_number(result["estimated_cost"], "estimated_cost")
        _optional_non_negative_int(result["rows_to_scan"], "rows_to_scan")
        for name in ("profile_name", "policy_version"):
            if type(result[name]) is not str or not result[name].strip():
                raise TypeError(f"{name} must be non-empty text")
        issue_types = _issue_types(result["issues"])
        if "dry_run_only" in result:
            if (
                result["dry_run_only"] is not True
                or result["skipped_execution"] is not True
                or type(result["sql_query"]) is not str
                or not result["sql_query"].strip()
                or plan is not None
                or result["estimated_cost"] is not None
                or result["rows_to_scan"] is not None
                or issue_types
            ):
                raise ValueError("dry-run EXPLAIN result is contradictory")
            return _inconclusive(
                candidate_id,
                CheckKind.EXPLAIN,
                CheckFailureCode.CHECK_MALFORMED,
                "EXPLAIN_SKIPPED_DRY_RUN",
            )
        if plan is None:
            if not issue_types:
                raise ValueError("missing EXPLAIN plan lacks issues")
            substantive = tuple(item for item in issue_types if item != "UNSAFE")
            if substantive and _timeout_only(substantive):
                return _inconclusive(
                    candidate_id,
                    CheckKind.EXPLAIN,
                    CheckFailureCode.CHECK_TIMEOUT,
                    _summary(issue_types),
                )
            return _failed(
                candidate_id,
                CheckKind.EXPLAIN,
                CheckFailureCode.EXPLAIN_REJECTED,
                _summary(issue_types),
            )
        if set(issue_types).intersection(
            {"EXPLAIN_ERROR", "EXPLAIN_UNSUPPORTED", "UNSAFE"}
        ):
            return _failed(
                candidate_id,
                CheckKind.EXPLAIN,
                CheckFailureCode.EXPLAIN_REJECTED,
                _summary(issue_types),
            )
        return _passed(candidate_id, CheckKind.EXPLAIN)
    except (TypeError, ValueError):
        return _malformed(candidate_id, CheckKind.EXPLAIN)


def adapt_semantic_authority_check_result(
    check_input: SemanticCheckInput,
    native_result: object,
) -> CheckResult:
    """Authenticate the authority-only gate without a formula certificate."""

    candidate_id = _safe_semantic_candidate_id(check_input)
    try:
        checked_input = require_authenticated_semantic_input(check_input)
        checked_result = require_typed_check_result(native_result)
        candidate_id = _candidate_id(checked_input.candidate)
        if (
            checked_result.candidate_id != candidate_id
            or checked_result.check_kind is not CheckKind.SEMANTIC
            or checked_result.check_id != _expected_check_id(checked_result)
            or checked_result.formula_certificate is not None
            or not set(checked_result.affected_source_ids).issubset(
                checked_input.requirements.required_source_ids
            )
            or not set(checked_result.affected_ast_node_ids).issubset(
                _semantic_ast_node_ids(checked_input.parsed_ast)
            )
        ):
            raise ValueError("semantic authority result contradicts its authority")
        return checked_result
    except Exception:
        return _malformed(candidate_id, CheckKind.SEMANTIC)


def adapt_execution_check_result(
    candidate: SqlCandidate,
    native_result: QueryExecutionResult,
    *,
    execution_id: str,
    expected_row_limit: int,
    expected_dry_run_only: bool,
) -> ExecutionCheckEvidence:
    """Convert one FINAL executor envelope without retaining database errors."""

    candidate_id = _candidate_id(candidate)
    execution_id = _execution_id(execution_id)
    if type(expected_row_limit) is not int or expected_row_limit <= 0:
        raise DeterministicCheckInputError(CheckFailureCode.CHECK_INPUT_INVALID)
    if type(expected_dry_run_only) is not bool:
        raise DeterministicCheckInputError(CheckFailureCode.CHECK_INPUT_INVALID)

    if not _exact_final_execution_result(native_result):
        return ExecutionCheckEvidence(
            _malformed(candidate_id, CheckKind.EXECUTION),
            None,
        )
    return _adapt_final_execution_mapping(
        candidate,
        native_result.to_mapping(),
        candidate_id=candidate_id,
        execution_id=execution_id,
        expected_row_limit=expected_row_limit,
        expected_dry_run_only=expected_dry_run_only,
    )


def adapt_final_execution_mapping(
    candidate: SqlCandidate,
    outcome: object,
    *,
    execution_id: str,
    expected_row_limit: int,
    expected_dry_run_only: bool,
) -> ExecutionCheckEvidence:
    """Adapt one exact FINAL execution mapping without importing core runtime."""

    candidate_id = _candidate_id(candidate)
    execution_id = _execution_id(execution_id)
    if type(expected_row_limit) is not int or expected_row_limit <= 0:
        raise DeterministicCheckInputError(CheckFailureCode.CHECK_INPUT_INVALID)
    if type(expected_dry_run_only) is not bool:
        raise DeterministicCheckInputError(CheckFailureCode.CHECK_INPUT_INVALID)
    if type(outcome) is not dict or not _exact_json_value(outcome):
        return ExecutionCheckEvidence(
            _malformed(candidate_id, CheckKind.EXECUTION),
            None,
        )
    return _adapt_final_execution_mapping(
        candidate,
        outcome,
        candidate_id=candidate_id,
        execution_id=execution_id,
        expected_row_limit=expected_row_limit,
        expected_dry_run_only=expected_dry_run_only,
    )


def _adapt_final_execution_mapping(
    candidate: SqlCandidate,
    outcome: dict[str, object],
    *,
    candidate_id: str,
    execution_id: str,
    expected_row_limit: int,
    expected_dry_run_only: bool,
) -> ExecutionCheckEvidence:
    contract_error = text_to_sql_executor_contract_error(
        outcome,
        expected_dry_run_only=expected_dry_run_only,
        expected_sql_query=candidate.sql,
        expected_row_limit=expected_row_limit,
    )
    if contract_error is not None:
        execution = _trusted_malformed_failure(
            candidate_id,
            candidate.sql,
            execution_id,
            outcome,
        )
        return ExecutionCheckEvidence(
            _malformed(candidate_id, CheckKind.EXECUTION),
            execution,
        )

    execution = ExecutionResult(
        execution_id=execution_id,
        candidate_id=candidate_id,
        success=outcome["success"],
        row_count=outcome["rows_affected"],
        elapsed_ms=outcome["execution_time_ms"],
        error_code=None if outcome["success"] else "EXECUTION_REJECTED",
    )
    if outcome["success"]:
        check = _passed(candidate_id, CheckKind.EXECUTION)
    else:
        check = _failed(
            candidate_id,
            CheckKind.EXECUTION,
            CheckFailureCode.EXECUTION_REJECTED,
            CheckFailureCode.EXECUTION_REJECTED.value,
        )
    return ExecutionCheckEvidence(check, execution)


def _execution_id(value: object) -> str:
    if type(value) is not str:
        raise DeterministicCheckInputError(CheckFailureCode.CHECK_INPUT_INVALID)
    try:
        return TypeAdapter(Id).validate_python(value, strict=True)
    except (ValidationError, TypeError, ValueError) as exc:
        raise DeterministicCheckInputError(
            CheckFailureCode.CHECK_INPUT_INVALID
        ) from exc


def _exact_final_execution_result(value: object) -> bool:
    from ..core._db_exec import QueryExecutionResult, QueryPurpose

    if (
        type(value) is not QueryExecutionResult
        or value.purpose is not QueryPurpose.FINAL
        or type(value.outcome) is not dict
        or not _exact_json_value(value.outcome)
    ):
        return False
    try:
        checked = QueryExecutionResult(value.purpose, value.to_mapping())
    except (TypeError, ValueError):
        return False
    return (
        type(checked.outcome) is dict
        and checked.purpose is value.purpose
        and checked.outcome == value.outcome
    )


def _exact_json_value(value: object) -> bool:
    if value is None or type(value) in {str, int, float, bool}:
        return type(value) is not float or math.isfinite(value)
    if type(value) is list:
        return all(_exact_json_value(item) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _exact_json_value(item) for key, item in value.items()
        )
    return False


def _trusted_malformed_failure(
    candidate_id: str,
    candidate_sql: str,
    execution_id: str,
    outcome: dict[str, object],
) -> ExecutionResult | None:
    """Trust identity only for an exact failed envelope tied to saved SQL."""

    if (
        outcome.get("sql_query") != candidate_sql
        or outcome.get("success") is not False
        or type(outcome.get("rows_affected")) is not int
        or outcome["rows_affected"] < 0
        or type(outcome.get("execution_time_ms")) is not int
        or outcome["execution_time_ms"] < 0
    ):
        return None
    return ExecutionResult(
        execution_id=execution_id,
        candidate_id=candidate_id,
        success=False,
        row_count=outcome["rows_affected"],
        elapsed_ms=outcome["execution_time_ms"],
        error_code="EXECUTION_REJECTED",
    )


def _candidate_id(candidate: SqlCandidate) -> str:
    return _exact_contract(candidate, SqlCandidate, "candidate").candidate_id


def _exact_contract(value, model_type, label):
    try:
        return revalidate_exact_model(
            value,
            model_type,
            ExactContractError,
            label,
        )
    except ExactContractError as exc:
        raise DeterministicCheckInputError(
            CheckFailureCode.CHECK_INPUT_INVALID
        ) from exc


def _exact_dataclass_contract(value, model_type, label):
    try:
        return require_exact_dataclass(
            value,
            model_type,
            ExactContractError,
            label,
        )
    except ExactContractError as exc:
        raise DeterministicCheckInputError(
            CheckFailureCode.CHECK_INPUT_INVALID
        ) from exc


def _safe_semantic_candidate_id(
    check_input: object,
) -> str:
    try:
        return _candidate_id(check_input.candidate)
    except Exception:
        return "invalid-candidate"


def _exact_mapping(
    value: object,
    *,
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
    paired_optional: set[str] | frozenset[str] = frozenset(),
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError("native checker result must be a dict")
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise ValueError("native checker result has an invalid shape")
    present_optional = keys.intersection(paired_optional)
    if present_optional and present_optional != paired_optional:
        raise ValueError("native checker optional fields must be complete")
    return value


def _issue_types(value: object, *, advisory: bool = False) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError("checker issues must be a list")
    expected_keys = _ADVISORY_ISSUE_KEYS if advisory else _ISSUE_KEYS
    values: list[str] = []
    for issue in value:
        if type(issue) is not dict or set(issue) != expected_keys:
            raise ValueError("checker issue has an invalid shape")
        for name in ("issue_type", "description"):
            if type(issue[name]) is not str or not issue[name].strip():
                raise TypeError("checker issue fields must be non-empty text")
        if advisory and issue["blocking"] is not False:
            raise ValueError("advisory issue cannot be blocking")
        values.append(issue["issue_type"])
    return tuple(sorted(set(values)))


def _optional_non_negative_number(value: object, name: str) -> None:
    if value is None:
        return
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise TypeError(f"{name} must be a non-negative number or None")


def _optional_non_negative_int(value: object, name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise TypeError(f"{name} must be a non-negative integer or None")


def _timeout_only(issue_types: tuple[str, ...]) -> bool:
    return bool(issue_types) and set(issue_types).issubset(_TIMEOUT_ISSUES)


def _summary(issue_types: tuple[str, ...]) -> str:
    return ",".join(issue_types)


def _passed(candidate_id: str, kind: CheckKind) -> CheckResult:
    return CheckResult(
        check_id=f"{kind.value}:{candidate_id}:passed",
        candidate_id=candidate_id,
        check_kind=kind,
        status=CheckStatus.PASSED,
        failure_code=None,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=None,
    )


def _failed(
    candidate_id: str,
    kind: CheckKind,
    code: CheckFailureCode,
    summary: str,
) -> CheckResult:
    return CheckResult(
        check_id=f"{kind.value}:{candidate_id}:{code.value.lower()}",
        candidate_id=candidate_id,
        check_kind=kind,
        status=CheckStatus.FAILED,
        failure_code=code,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=summary,
        repair=CheckRepair(kind=RepairKind.REVISE_SQL),
    )


def _inconclusive(
    candidate_id: str,
    kind: CheckKind,
    code: CheckFailureCode,
    summary: str,
) -> CheckResult:
    return CheckResult(
        check_id=f"{kind.value}:{candidate_id}:{code.value.lower()}",
        candidate_id=candidate_id,
        check_kind=kind,
        status=CheckStatus.INCONCLUSIVE,
        failure_code=code,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=summary,
        repair=CheckRepair(kind=RepairKind.REVISE_SQL),
    )


def _malformed(candidate_id: str, kind: CheckKind) -> CheckResult:
    return _inconclusive(
        candidate_id,
        kind,
        CheckFailureCode.CHECK_MALFORMED,
        f"invalid {kind.value} checker result",
    )


def _expected_check_id(result: CheckResult) -> str:
    suffix = (
        "passed"
        if result.status is CheckStatus.PASSED
        else result.failure_code.value.lower()
    )
    return f"semantic:{result.candidate_id}:{suffix}"


def _semantic_ast_node_ids(parsed_ast: ParsedSqlCandidate) -> set[str]:
    return {
        *(
            item.node_id
            for items in (
                parsed_ast.scopes,
                parsed_ast.ctes,
                parsed_ast.table_scans,
                parsed_ast.cte_references,
                parsed_ast.derived_relations,
                parsed_ast.expression_relations,
                parsed_ast.set_operations,
                parsed_ast.subquery_refs,
                parsed_ast.joins,
                parsed_ast.projections,
                parsed_ast.predicates,
                parsed_ast.aggregates,
                parsed_ast.groupings,
                parsed_ast.orderings,
                parsed_ast.limits,
            )
            for item in items
        ),
        *(atom.node_id for predicate in parsed_ast.predicates for atom in predicate.atoms),
    }


__all__ = [
    "DeterministicCheck",
    "DeterministicCheckInput",
    "DeterministicCheckInputError",
    "ExecutionCheckEvidence",
    "SemanticCheckInput",
    "adapt_execution_check_result",
    "adapt_explain_check_result",
    "adapt_safety_check_result",
    "adapt_schema_check_result",
    "adapt_semantic_authority_check_result",
    "require_authenticated_semantic_input",
    "require_typed_check_result",
]
