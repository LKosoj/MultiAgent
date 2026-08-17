"""Fail-closed safety and semantic gate for one parsed SQL candidate."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from threading import RLock
from weakref import ReferenceType, ref

from pydantic import model_validator

from custom_tools.text_to_sql import core
from custom_tools.text_to_sql.validators import SQLSchemaValidator
from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded

from ._sql_ast_identity import source_sql_digest
from .checks import (
    SemanticCheckInput,
    adapt_schema_check_result,
    adapt_safety_check_result,
    adapt_semantic_authority_check_result,
)
from .models import (
    AstSemanticCoverage,
    CheckFailureCode,
    CheckKind,
    CheckRepair,
    CheckResult,
    CheckStatus,
    Digest,
    Id,
    NonNegativeInt,
    RepairKind,
    ResearchState,
    SqlCandidate,
    StrictModel,
)
from .semantic_checks import evaluate_semantic_authority_checks
from .semantic_coverage import CoverageRequirements
from .semantic_plan import build_semantic_ast
from .sql_ast import parse_sql_candidate


PRE_EXECUTION_GATE_RUNTIME_KEY = "_text_to_sql_pre_execution_gate"
PRE_EXECUTION_GATE_REQUIRED_RUNTIME_KEY = "_text_to_sql_pre_execution_gate_required"


class PreExecutionGateReceipt(StrictModel):
    """Closed result for one exact SQL candidate and its minimal AST coverage."""

    run_id: Id
    run_incarnation: Id | None
    state_revision: NonNegativeInt | None
    candidate_id: Id
    sql_source_digest: Digest
    normalized_ast_digest: Digest | None
    requirements_digest: Digest | None
    semantic_coverage: AstSemanticCoverage | None
    source_coverage_available: bool
    check_results: tuple[CheckResult, ...]
    primary_check_id: Id | None
    allowed: bool

    @model_validator(mode="after")
    def validate_gate_result(self) -> PreExecutionGateReceipt:
        if not self.check_results or any(
            result.candidate_id != self.candidate_id for result in self.check_results
        ):
            raise ValueError("pre-execution receipt check results are invalid")
        kinds = tuple(result.check_kind for result in self.check_results)
        if self.normalized_ast_digest is None:
            if self.semantic_coverage is not None or self.source_coverage_available or kinds != (CheckKind.SEMANTIC,):
                raise ValueError("unparsed candidate cannot carry semantic coverage")
        elif kinds not in {
            (CheckKind.SAFETY,),
            (CheckKind.SAFETY, CheckKind.SCHEMA),
            (CheckKind.SAFETY, CheckKind.SEMANTIC),
            (CheckKind.SAFETY, CheckKind.SCHEMA, CheckKind.SEMANTIC),
        }:
            raise ValueError("parsed checks must be ordered safety, schema, then semantic")
        elif self.source_coverage_available != (self.semantic_coverage is not None):
            raise ValueError("semantic coverage availability contradicts receipt")
        elif self.semantic_coverage is not None and self.requirements_digest != self.semantic_coverage.requirements_digest:
            raise ValueError("semantic coverage digest contradicts receipt")
        elif self.source_coverage_available and kinds not in {
            (CheckKind.SAFETY, CheckKind.SEMANTIC),
            (CheckKind.SAFETY, CheckKind.SCHEMA, CheckKind.SEMANTIC),
        }:
            raise ValueError("semantic coverage requires the semantic stage")
        blocking = tuple(
            result for result in self.check_results if result.status is not CheckStatus.PASSED
        )
        if self.allowed:
            if (
                self.run_incarnation is None
                or self.state_revision is None
                or self.requirements_digest is None
                or self.normalized_ast_digest is None
                or self.semantic_coverage is None
                or kinds not in {
                    (CheckKind.SAFETY, CheckKind.SEMANTIC),
                    (CheckKind.SAFETY, CheckKind.SCHEMA, CheckKind.SEMANTIC),
                }
                or blocking
                or self.primary_check_id is not None
            ):
                raise ValueError("allowed receipt requires two passed authenticated checks")
        elif (
            len(blocking) != 1
            or self.primary_check_id != blocking[0].check_id
            or self.check_results[-1] != blocking[0]
        ):
            raise ValueError("blocked receipt requires one final primary failure")
        return self


def _build_capability_api():
    origins: dict[int, tuple[ReferenceType[object], object]] = {}
    captures: dict[int, tuple[object, PreExecutionGateReceipt | None]] = {}
    lock = RLock()
    marker = object()

    @dataclass(frozen=True, slots=True, weakref_slot=True)
    class _Capability:
        state: ResearchState
        requirements: CoverageRequirements
        dsn: str
        table_namespace: str
        expected_sql: str
        run_id: str
        run_incarnation: str
        candidate_id: str
        deadline: DeadlineBudget
        is_cancelled: Callable[[], bool] = field(repr=False, compare=False)
        runtime: object | None = field(default=None, repr=False, compare=False)

    def register(value: _Capability) -> None:
        identity = id(value)

        def discard(reference: ReferenceType[object]) -> None:
            current = origins.get(identity)
            if current is not None and current[0] is reference:
                origins.pop(identity, None)

        origins[identity] = (ref(value, discard), marker)

    def registered(value: object) -> bool:
        current = origins.get(id(value))
        return bool(current is not None and current[1] is marker and current[0]() is value)

    def make(
        *,
        state: ResearchState,
        requirements: CoverageRequirements,
        dsn: str,
        table_namespace: str,
        expected_sql: str,
        run_id: str,
        run_incarnation: str,
        deadline: DeadlineBudget,
        is_cancelled: Callable[[], bool],
        runtime: object | None,
    ) -> object:
        _validate_capability_inputs(
            state, requirements, dsn, table_namespace, expected_sql, run_id,
            run_incarnation, deadline, is_cancelled,
        )
        identity = _candidate_identity(run_id, run_incarnation, state.revision, expected_sql)
        capability = _Capability(
            state=state,
            requirements=requirements,
            dsn=dsn,
            table_namespace=table_namespace,
            expected_sql=expected_sql,
            run_id=run_id,
            run_incarnation=run_incarnation,
            candidate_id=f"candidate:{identity}",
            deadline=deadline,
            is_cancelled=is_cancelled,
            runtime=runtime,
        )
        register(capability)
        return capability

    def create_pre_execution_gate_capability(**kwargs: object) -> object:
        return make(**kwargs, runtime=None)

    def _create_capturing_pre_execution_gate_capability(
        runtime: object, **kwargs: object
    ) -> object:
        return make(**kwargs, runtime=runtime)

    def evaluate_pre_execution_gate_capability(
        value: object,
        *,
        expected_run_id: str,
        expected_sql: str,
        safety_policy: object,
    ) -> PreExecutionGateReceipt:
        candidate_id = _fallback_candidate_id(expected_run_id, expected_sql)
        digest = source_sql_digest(expected_sql if type(expected_sql) is str else "")
        if (
            type(value) is not _Capability
            or not registered(value)
            or type(expected_run_id) is not str
            or not expected_run_id
            or type(expected_sql) is not str
            or not expected_sql.strip()
            or value.run_id != expected_run_id
            or value.expected_sql != expected_sql
        ):
            return _input_failure_receipt(expected_run_id, candidate_id, digest)
        try:
            _require_active(value)
            parsed_ast = parse_sql_candidate(value.expected_sql, value.dsn, value.candidate_id)
            candidate = SqlCandidate(
                candidate_id=value.candidate_id,
                sql=value.expected_sql,
                normalized_ast_digest=parsed_ast.candidate_digest,
                revision=value.state.revision,
            )
        except (asyncio.CancelledError, WorkflowDeadlineExceeded):
            raise
        except Exception:
            return _capture(value, _ast_failure_receipt(value, digest))
        try:
            _require_active(value)
            safety = adapt_safety_check_result(
                candidate,
                core.sql_safety_check(
                    value.expected_sql,
                    dsn=value.dsn,
                    safety_policy=safety_policy,
                    static_only=True,
                ),
            )
        except (asyncio.CancelledError, WorkflowDeadlineExceeded):
            raise
        except Exception:
            safety = _inconclusive(candidate.candidate_id, CheckKind.SAFETY, CheckFailureCode.CHECK_MALFORMED)
        if safety.status is not CheckStatus.PASSED:
            return _capture(value, _receipt(value, candidate, digest, (safety,), None))
        loaded_schema = getattr(value.runtime, "loaded_schema", None)
        if loaded_schema is not None:
            try:
                _require_active(value)
                schema = adapt_schema_check_result(
                    candidate,
                    SQLSchemaValidator().validate_sql_against_schema(
                        candidate.sql,
                        loaded_schema.schema,
                        dsn=value.dsn,
                    ),
                )
            except (asyncio.CancelledError, WorkflowDeadlineExceeded):
                raise
            except Exception:
                schema = _inconclusive(
                    candidate.candidate_id,
                    CheckKind.SCHEMA,
                    CheckFailureCode.CHECK_MALFORMED,
                )
            if schema.status is not CheckStatus.PASSED:
                return _capture(value, _receipt(value, candidate, digest, (safety, schema), None))
        else:
            schema = None
        try:
            semantic_ast = build_semantic_ast(
                candidate,
                parsed_ast,
                value.state.query_spec,
                value.requirements,
                value.table_namespace,
            )
            semantic = adapt_semantic_authority_check_result(
                SemanticCheckInput(
                    semantic_ast=semantic_ast,
                    query_spec=value.state.query_spec,
                    requirements=value.requirements,
                ),
                evaluate_semantic_authority_checks(
                    SemanticCheckInput(
                        semantic_ast=semantic_ast,
                        query_spec=value.state.query_spec,
                        requirements=value.requirements,
                    ),
                    value.state,
                    value.dsn,
                ),
            )
        except (asyncio.CancelledError, WorkflowDeadlineExceeded):
            raise
        except Exception:
            semantic_ast = None
            semantic = _inconclusive(candidate.candidate_id, CheckKind.SEMANTIC, CheckFailureCode.CHECK_INPUT_INVALID)
        results = (safety, semantic) if schema is None else (safety, schema, semantic)
        return _capture(value, _receipt(value, candidate, digest, results, semantic_ast))

    def _capture(value: _Capability, receipt: PreExecutionGateReceipt) -> PreExecutionGateReceipt:
        if value.runtime is not None:
            with lock:
                captures[id(value.runtime)] = (value.runtime, receipt)
        return receipt

    def take_pre_execution_gate_receipt(runtime: object) -> PreExecutionGateReceipt | None:
        with lock:
            captured = captures.pop(id(runtime), None)
        return captured[1] if captured is not None and captured[0] is runtime else None

    def release_pre_execution_gate_capture(runtime: object) -> None:
        with lock:
            captures.pop(id(runtime), None)

    return (
        create_pre_execution_gate_capability,
        _create_capturing_pre_execution_gate_capability,
        evaluate_pre_execution_gate_capability,
        take_pre_execution_gate_receipt,
        release_pre_execution_gate_capture,
    )


(
    create_pre_execution_gate_capability,
    _create_capturing_pre_execution_gate_capability,
    evaluate_pre_execution_gate_capability,
    take_pre_execution_gate_receipt,
    release_pre_execution_gate_capture,
) = _build_capability_api()
del _build_capability_api


def _validate_capability_inputs(
    state: object,
    requirements: object,
    dsn: object,
    table_namespace: object,
    expected_sql: object,
    run_id: object,
    run_incarnation: object,
    deadline: object,
    is_cancelled: object,
) -> None:
    if (
        type(state) is not ResearchState
        or type(requirements) is not CoverageRequirements
        or type(dsn) is not str
        or not dsn.strip()
        or type(table_namespace) is not str
        or not table_namespace.strip()
        or type(expected_sql) is not str
        or not expected_sql.strip()
        or type(run_id) is not str
        or not run_id
        or type(run_incarnation) is not str
        or not run_incarnation
        or type(deadline) is not DeadlineBudget
        or not callable(is_cancelled)
        or state.run_id != run_id
        or state.run_incarnation != run_incarnation
        or requirements.run_id != run_id
        or requirements.run_incarnation != run_incarnation
        or requirements.state_revision != state.revision
    ):
        raise TypeError("pre-execution capability inputs are invalid")


def _require_active(capability: object) -> None:
    if capability.is_cancelled() is True:
        raise asyncio.CancelledError
    capability.deadline.require_remaining("text_to_sql_pre_execution_gate")


def _candidate_identity(run_id: str, run_incarnation: str, revision: int, sql: str) -> str:
    return sha256("\0".join((run_id, run_incarnation, str(revision), source_sql_digest(sql))).encode()).hexdigest()


def _fallback_candidate_id(run_id: object, sql: object) -> str:
    return f"candidate:{sha256(f'{run_id!s}\0{sql!s}'.encode()).hexdigest()}"


def _receipt(
    capability: object,
    candidate: SqlCandidate,
    sql_digest: str,
    results: tuple[CheckResult, ...],
    semantic_ast: object | None,
) -> PreExecutionGateReceipt:
    blocking = next((item for item in results if item.status is not CheckStatus.PASSED), None)
    coverage = getattr(semantic_ast, "coverage", None)
    return PreExecutionGateReceipt(
        run_id=capability.run_id,
        run_incarnation=capability.run_incarnation,
        state_revision=capability.state.revision,
        candidate_id=candidate.candidate_id,
        sql_source_digest=sql_digest,
        normalized_ast_digest=candidate.normalized_ast_digest,
        requirements_digest=capability.requirements.requirements_digest,
        semantic_coverage=coverage,
        source_coverage_available=coverage is not None,
        check_results=results,
        primary_check_id=blocking.check_id if blocking is not None else None,
        allowed=blocking is None,
    )


def _ast_failure_receipt(capability: object, sql_digest: str) -> PreExecutionGateReceipt:
    result = CheckResult(
        check_id=f"semantic:{capability.candidate_id}:ast_shape_unsupported",
        candidate_id=capability.candidate_id,
        check_kind=CheckKind.SEMANTIC,
        status=CheckStatus.FAILED,
        failure_code=CheckFailureCode.AST_SHAPE_UNSUPPORTED,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=None,
        repair=CheckRepair(kind=RepairKind.REVISE_SQL),
    )
    return PreExecutionGateReceipt(
        run_id=capability.run_id,
        run_incarnation=capability.run_incarnation,
        state_revision=capability.state.revision,
        candidate_id=capability.candidate_id,
        sql_source_digest=sql_digest,
        normalized_ast_digest=None,
        requirements_digest=capability.requirements.requirements_digest,
        semantic_coverage=None,
        source_coverage_available=False,
        check_results=(result,),
        primary_check_id=result.check_id,
        allowed=False,
    )


def _input_failure_receipt(run_id: object, candidate_id: str, sql_digest: str) -> PreExecutionGateReceipt:
    result = _inconclusive(candidate_id, CheckKind.SEMANTIC, CheckFailureCode.CHECK_INPUT_INVALID)
    return PreExecutionGateReceipt(
        run_id=run_id if type(run_id) is str and run_id else "invalid-run",
        run_incarnation=None,
        state_revision=None,
        candidate_id=candidate_id,
        sql_source_digest=sql_digest,
        normalized_ast_digest=None,
        requirements_digest=None,
        semantic_coverage=None,
        source_coverage_available=False,
        check_results=(result,),
        primary_check_id=result.check_id,
        allowed=False,
    )


def _inconclusive(candidate_id: str, kind: CheckKind, code: CheckFailureCode) -> CheckResult:
    return CheckResult(
        check_id=f"{kind.value}:{candidate_id}:{code.value.lower()}",
        candidate_id=candidate_id,
        check_kind=kind,
        status=CheckStatus.INCONCLUSIVE,
        failure_code=code,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=code.value,
        repair=CheckRepair(kind=RepairKind.REVISE_SQL),
    )


__all__ = [
    "PRE_EXECUTION_GATE_REQUIRED_RUNTIME_KEY",
    "PRE_EXECUTION_GATE_RUNTIME_KEY",
    "PreExecutionGateReceipt",
    "create_pre_execution_gate_capability",
    "evaluate_pre_execution_gate_capability",
    "release_pre_execution_gate_capture",
    "take_pre_execution_gate_receipt",
]
