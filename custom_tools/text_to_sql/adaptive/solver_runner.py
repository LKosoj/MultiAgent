"""Dormant synchronous runner for one solver candidate's deterministic gates."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import fields, is_dataclass
import math
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from custom_tools.text_to_sql import core
from custom_tools.text_to_sql.schema_loader import LoadedSchema
from custom_tools.text_to_sql.schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from custom_tools.text_to_sql.validators import (
    SQLSchemaValidator,
    TextToSqlSafetyPolicy,
)
from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded

from ..core._db_exec import (
    QueryExecutionRequest,
    QueryExecutionResult,
    QueryExecutor,
    QueryPurpose,
)
from ._exact_contract import exact_value, revalidate_exact_model
from .checks import (
    SemanticCheckInput,
    adapt_execution_check_result,
    adapt_explain_check_result,
    adapt_safety_check_result,
    adapt_schema_check_result,
    adapt_semantic_authority_check_result,
)
from .models import (
    CheckFailureCode,
    CheckKind,
    CheckRepair,
    CheckResult,
    CheckStatus,
    Id,
    ExecutionResult,
    RepairKind,
    ResearchState,
    SolverState,
    SolverStopReason,
)
from .semantic_checks import evaluate_semantic_authority_checks
from .semantic_coverage import CoverageRequirements
from .semantic_plan import build_semantic_ast
from .sql_ast import parse_sql_candidate
from .solver_loop import (
    SolverConflictError,
    SolverReducerError,
)
from .solver_results import (
    SolverGateTransitionResult,
    _validate_all_gate_histories,
    append_solver_check_result,
    append_solver_execution_result,
)

_GATE_ORDER = (
    CheckKind.SAFETY,
    CheckKind.SCHEMA,
    CheckKind.SEMANTIC,
    CheckKind.EXPLAIN,
    CheckKind.EXECUTION,
)
_ID_ADAPTER = TypeAdapter(Id)


class SolverRunnerError(SolverReducerError):
    """Base error for the dormant gate runner."""


class SolverRunnerValidationError(SolverRunnerError):
    """Trusted runner inputs do not belong to one exact authority."""


class SolverCommitError(SolverRunnerError):
    """The mandatory durable commit did not confirm the proposed state."""


class FinalQueryExecutor(Protocol):
    def execute(self, request: QueryExecutionRequest) -> QueryExecutionResult: ...


CommitTransition = Callable[[SolverGateTransitionResult], SolverState]


def run_solver_candidate_gates(
    state: SolverState,
    candidate_id: str,
    *,
    research_state: ResearchState,
    requirements: CoverageRequirements,
    loaded_schema: LoadedSchema,
    dsn: str,
    safety_policy: TextToSqlSafetyPolicy,
    row_limit: int,
    dry_run_only: bool,
    deadline: DeadlineBudget,
    is_cancelled: Callable[[], bool],
    id_factory: Callable[[], str],
    commit_transition: CommitTransition,
    executor: FinalQueryExecutor | None = None,
) -> SolverState:
    """Run the first missing gate, committing each result before continuing."""

    current = _run_solver_candidate_pre_execution_gates(
        state,
        candidate_id,
        research_state=research_state,
        requirements=requirements,
        loaded_schema=loaded_schema,
        dsn=dsn,
        safety_policy=safety_policy,
        row_limit=row_limit,
        dry_run_only=dry_run_only,
        deadline=deadline,
        is_cancelled=is_cancelled,
        commit_transition=commit_transition,
    )
    return _run_execution_gate(
        current,
        candidate_id,
        dsn=dsn,
        safety_policy=safety_policy,
        row_limit=row_limit,
        dry_run_only=dry_run_only,
        deadline=deadline,
        is_cancelled=is_cancelled,
        id_factory=id_factory,
        commit_transition=commit_transition,
        executor=executor,
    )


def run_solver_candidate_pre_execution_gates(
    state: SolverState,
    candidate_id: str,
    *,
    research_state: ResearchState,
    requirements: CoverageRequirements,
    loaded_schema: LoadedSchema,
    dsn: str,
    safety_policy: TextToSqlSafetyPolicy,
    row_limit: int,
    dry_run_only: bool,
    deadline: DeadlineBudget,
    is_cancelled: Callable[[], bool],
    commit_transition: CommitTransition,
    parsed_candidate: object | None = None,
) -> SolverState:
    """Commit safety through EXPLAIN without invoking the final executor."""

    return _run_solver_candidate_pre_execution_gates(
        state,
        candidate_id,
        research_state=research_state,
        requirements=requirements,
        loaded_schema=loaded_schema,
        dsn=dsn,
        safety_policy=safety_policy,
        row_limit=row_limit,
        dry_run_only=dry_run_only,
        deadline=deadline,
        is_cancelled=is_cancelled,
        commit_transition=commit_transition,
        parsed_candidate=parsed_candidate,
    )


def _run_solver_candidate_pre_execution_gates(
    state: SolverState,
    candidate_id: str,
    *,
    research_state: ResearchState,
    requirements: CoverageRequirements,
    loaded_schema: LoadedSchema,
    dsn: str,
    safety_policy: TextToSqlSafetyPolicy,
    row_limit: int,
    dry_run_only: bool,
    deadline: DeadlineBudget,
    is_cancelled: Callable[[], bool],
    commit_transition: CommitTransition,
    parsed_candidate: object | None = None,
) -> SolverState:

    current, research, authority = _validate_runner_inputs(
        state,
        candidate_id,
        research_state,
        requirements,
        loaded_schema,
        dsn,
        safety_policy,
        row_limit,
        dry_run_only,
        deadline,
        is_cancelled,
        commit_transition,
    )
    try:
        _validate_all_gate_histories(current)
    except SolverConflictError as exc:
        raise SolverRunnerValidationError(
            "SolverState gate history is invalid"
        ) from exc
    next_index, blocked = _gate_progress(current, candidate_id)
    if _has_valid_execution_evidence(current, candidate_id, next_index):
        return current
    _validate_open_gate_state(current)
    if blocked or next_index == len(_GATE_ORDER):
        return current

    semantic_input = (
        None
        if next_index > _GATE_ORDER.index(CheckKind.SEMANTIC)
        else _rebuild_semantic_input(
            current,
            candidate_id,
            research,
            authority,
            dsn,
            parsed_candidate,
        )
    )
    return _run_pre_execution_stages(
        current,
        next_index,
        semantic_input,
        research,
        loaded_schema,
        dsn,
        safety_policy,
        dry_run_only,
        deadline,
        is_cancelled,
        commit_transition,
    )


def _has_valid_execution_evidence(
    state: SolverState,
    candidate_id: str,
    next_index: int,
) -> bool:
    execution = next(
        (
            item for item in state.execution_results if item.candidate_id == candidate_id
        ),
        None,
    )
    if execution is None:
        return False
    checks = tuple(
        item for item in state.check_results if item.candidate_id == candidate_id
    )
    if (
        next_index != len(_GATE_ORDER)
        or not checks
        or checks[-1].check_kind is not CheckKind.EXECUTION
        or execution.success != (checks[-1].status is CheckStatus.PASSED)
        or execution.success
        != (
            state.stop_reason is SolverStopReason.SOLVED
            and state.selected_candidate_id == candidate_id
        )
    ):
        raise SolverRunnerValidationError(
            "execution evidence contradicts candidate gate history"
        )
    return True


def _validate_open_gate_state(state: SolverState) -> None:
    if state.stop_reason is not None:
        raise SolverConflictError("stopped SolverState cannot run candidate gates")
    if state.selected_candidate_id is not None:
        raise SolverRunnerValidationError("open SolverState cannot select a candidate")


def _run_pre_execution_stages(
    state: SolverState,
    next_index: int,
    semantic_input: SemanticCheckInput | None,
    research_state: ResearchState,
    loaded_schema: LoadedSchema,
    dsn: str,
    safety_policy: TextToSqlSafetyPolicy,
    dry_run_only: bool,
    deadline: DeadlineBudget,
    is_cancelled: Callable[[], bool],
    commit_transition: CommitTransition,
) -> SolverState:
    candidate = state.sql_candidates[-1]

    def semantic_stage() -> CheckResult:
        if semantic_input is None:
            raise SolverRunnerValidationError("semantic input is missing")
        deterministic_result = evaluate_semantic_authority_checks(
            semantic_input, research_state, dsn
        )
        return adapt_semantic_authority_check_result(semantic_input, deterministic_result)

    stages = (
        (
            CheckKind.SAFETY,
            lambda: adapt_safety_check_result(
                candidate,
                core.sql_safety_check(
                    candidate.sql,
                    dsn=dsn,
                    safety_policy=safety_policy,
                    static_only=True,
                ),
            ),
        ),
        (
            CheckKind.SCHEMA,
            lambda: adapt_schema_check_result(
                candidate,
                SQLSchemaValidator().validate_sql_against_schema(
                    candidate.sql,
                    loaded_schema.schema,
                    dsn=dsn,
                ),
            ),
        ),
        (
            CheckKind.SEMANTIC,
            semantic_stage,
        ),
        (
            CheckKind.EXPLAIN,
            lambda: adapt_explain_check_result(
                candidate,
                core.sql_explain(
                    candidate.sql,
                    dsn=dsn,
                    safety_policy=safety_policy,
                    dry_run_only=dry_run_only,
                    deadline=deadline,
                ),
            ),
        ),
    )
    current = state
    for index, (kind, checker) in enumerate(stages):
        if index < next_index:
            continue
        current, passed = _run_checker_stage(
            current,
            kind,
            deadline,
            is_cancelled,
            commit_transition,
            checker,
        )
        if not passed:
            return current
    return current


def _run_execution_gate(
    current: SolverState,
    candidate_id: str,
    *,
    dsn: str,
    safety_policy: TextToSqlSafetyPolicy,
    row_limit: int,
    dry_run_only: bool,
    deadline: DeadlineBudget,
    is_cancelled: Callable[[], bool],
    id_factory: Callable[[], str],
    commit_transition: CommitTransition,
    executor: FinalQueryExecutor | None,
) -> SolverState:
    _validate_execution_inputs(id_factory, executor)
    next_index, blocked = _gate_progress(current, candidate_id)
    if current.execution_results:
        return current
    if blocked or next_index != len(_GATE_ORDER) - 1:
        return current
    candidate = current.sql_candidates[-1]
    _require_active(deadline, is_cancelled, CheckKind.EXECUTION)
    execution_id = _new_execution_id(current, id_factory)
    final_executor = executor if executor is not None else QueryExecutor()
    request = QueryExecutionRequest(
        sql_query=candidate.sql,
        purpose=QueryPurpose.FINAL,
        row_limit=row_limit,
        dsn=dsn,
        dry_run_only=dry_run_only,
        safety_policy=safety_policy,
        deadline=deadline,
    )
    try:
        native_result = final_executor.execute(request)
    except (asyncio.CancelledError, WorkflowDeadlineExceeded):
        _require_active(deadline, is_cancelled, CheckKind.EXECUTION)
        raise
    except Exception:
        _require_active(deadline, is_cancelled, CheckKind.EXECUTION)
        return _commit_gate_result(
            current,
            _malformed_check(candidate_id, CheckKind.EXECUTION),
            None,
            deadline,
            is_cancelled,
            commit_transition,
        )
    _require_active(deadline, is_cancelled, CheckKind.EXECUTION)
    try:
        evidence = adapt_execution_check_result(
            candidate,
            native_result,
            execution_id=execution_id,
            expected_row_limit=row_limit,
            expected_dry_run_only=dry_run_only,
        )
    except (asyncio.CancelledError, WorkflowDeadlineExceeded):
        raise
    except Exception:
        return _commit_gate_result(
            current,
            _malformed_check(candidate_id, CheckKind.EXECUTION),
            None,
            deadline,
            is_cancelled,
            commit_transition,
        )
    return _commit_gate_result(
        current,
        evidence.check_result,
        evidence.execution_result,
        deadline,
        is_cancelled,
        commit_transition,
    )


def _validate_runner_inputs(
    state: SolverState,
    candidate_id: object,
    research_state: ResearchState,
    requirements: CoverageRequirements,
    loaded_schema: LoadedSchema,
    dsn: object,
    safety_policy: TextToSqlSafetyPolicy,
    row_limit: object,
    dry_run_only: object,
    deadline: DeadlineBudget,
    is_cancelled: object,
    commit_transition: object,
) -> tuple[SolverState, ResearchState, CoverageRequirements]:
    current = revalidate_exact_model(
        state,
        SolverState,
        SolverRunnerValidationError,
        "state",
    )
    research = revalidate_exact_model(
        research_state,
        ResearchState,
        SolverRunnerValidationError,
        "research_state",
    )
    authority = revalidate_exact_model(
        requirements,
        CoverageRequirements,
        SolverRunnerValidationError,
        "requirements",
    )
    _validate_candidate_id(current, candidate_id)
    _validate_runner_options(
        dsn,
        safety_policy,
        row_limit,
        dry_run_only,
        deadline,
        is_cancelled,
        commit_transition,
    )
    fingerprint, namespace_fingerprint, schema_version = _validated_loaded_schema(
        loaded_schema
    )
    _validate_schema_authority(
        fingerprint,
        namespace_fingerprint,
        schema_version,
        current,
        research,
        authority,
    )
    _validate_runner_authority_identity(current, research, authority)
    return current, research, authority


def _validate_candidate_id(state: SolverState, candidate_id: object) -> None:
    if type(candidate_id) is not str or not candidate_id:
        raise SolverRunnerValidationError("candidate_id must be non-empty text")
    if (
        not state.sql_candidates
        or state.sql_candidates[-1].candidate_id != candidate_id
    ):
        raise SolverRunnerValidationError(
            "candidate must be the latest SolverState candidate"
        )


def _validate_runner_options(
    dsn: object,
    safety_policy: TextToSqlSafetyPolicy,
    row_limit: object,
    dry_run_only: object,
    deadline: DeadlineBudget,
    is_cancelled: object,
    commit_transition: object,
) -> None:
    if type(dsn) is not str or not dsn:
        raise SolverRunnerValidationError("dsn must be non-empty text")
    if type(safety_policy) is not TextToSqlSafetyPolicy:
        raise SolverRunnerValidationError("safety_policy must be exact")
    if not _exact_dataclass_tree(safety_policy):
        raise SolverRunnerValidationError("safety_policy has forged fields")
    try:
        checked_policy = TextToSqlSafetyPolicy.from_mapping(safety_policy.to_mapping())
    except (TypeError, ValueError) as exc:
        raise SolverRunnerValidationError("safety_policy is invalid") from exc
    if checked_policy != safety_policy:
        raise SolverRunnerValidationError("safety_policy is not canonical")
    if type(row_limit) is not int or row_limit <= 0:
        raise SolverRunnerValidationError("row_limit must be a positive integer")
    if type(dry_run_only) is not bool:
        raise SolverRunnerValidationError("dry_run_only must be a boolean")
    _validate_deadline(deadline)
    callbacks = (is_cancelled, commit_transition)
    if not all(callable(value) for value in callbacks):
        raise SolverRunnerValidationError("runner callbacks must be callable")


def _validated_loaded_schema(loaded_schema: LoadedSchema) -> tuple[str, str, str]:
    if type(loaded_schema) is not LoadedSchema:
        raise SolverRunnerValidationError("loaded_schema must be exact LoadedSchema")
    if not _exact_dataclass_tree(loaded_schema) or (
        type(loaded_schema.namespace) is not SchemaNamespace or not loaded_schema.source
    ):
        raise SolverRunnerValidationError("loaded_schema has invalid exact fields")
    try:
        fingerprint = canonical_schema_fingerprint(loaded_schema.schema)
        checked_scope = SchemaScope.from_mapping(
            loaded_schema.namespace.scope.to_mapping()
        )
        checked_namespace = SchemaNamespace(
            scope=checked_scope,
            schema_fingerprint=loaded_schema.namespace.schema_fingerprint,
        )
    except (TypeError, ValueError) as exc:
        raise SolverRunnerValidationError("loaded schema is invalid") from exc
    if not exact_value(loaded_schema.namespace, checked_namespace):
        raise SolverRunnerValidationError("schema namespace is not canonical")
    schema_version = f"sha256:{loaded_schema.namespace.version_key}"
    return fingerprint, loaded_schema.namespace.schema_fingerprint, schema_version


def _validate_schema_authority(
    fingerprint: str,
    namespace_fingerprint: str,
    schema_version: str,
    state: SolverState,
    research_state: ResearchState,
    requirements: CoverageRequirements,
) -> None:
    if (
        fingerprint != namespace_fingerprint
        or schema_version != state.schema_namespace_version
        or schema_version != research_state.schema_namespace_version
        or schema_version != requirements.schema_namespace_version
    ):
        raise SolverRunnerValidationError("schema namespace or fingerprint mismatch")


def _validate_runner_authority_identity(
    state: SolverState,
    research_state: ResearchState,
    requirements: CoverageRequirements,
) -> None:
    if (
        state.run_id != research_state.run_id
        or state.run_id != requirements.run_id
        or state.run_incarnation != research_state.run_incarnation
        or state.run_incarnation != requirements.run_incarnation
        or research_state.revision != requirements.state_revision
        or state.query_spec != research_state.query_spec
        or requirements.expected_result_shape != state.query_spec.expected_result_shape
    ):
        raise SolverRunnerValidationError("runner authority identity mismatch")


def _validate_execution_inputs(id_factory: object, executor: object) -> None:
    if not callable(id_factory):
        raise SolverRunnerValidationError("runner callbacks must be callable")
    if executor is not None and not callable(getattr(executor, "execute", None)):
        raise SolverRunnerValidationError("executor must implement execute(request)")


def _gate_progress(state: SolverState, candidate_id: str) -> tuple[int, bool]:
    checks = tuple(
        item for item in state.check_results if item.candidate_id == candidate_id
    )
    if len(checks) > len(_GATE_ORDER):
        raise SolverRunnerValidationError("candidate gate history is too long")
    for index, check in enumerate(checks):
        if check.check_kind is not _GATE_ORDER[index]:
            raise SolverRunnerValidationError("candidate gate history is not a prefix")
        if check.status is not CheckStatus.PASSED:
            if index != len(checks) - 1:
                raise SolverRunnerValidationError("checks follow a blocking gate")
            return len(checks), True
    return len(checks), False


def _rebuild_semantic_input(
    state: SolverState,
    candidate_id: str,
    research_state: ResearchState,
    requirements: CoverageRequirements,
    dsn: str,
    parsed_candidate: object | None = None,
) -> SemanticCheckInput:
    candidate = state.sql_candidates[-1]
    table_namespaces = {table.namespace for table in requirements.allowed_tables}
    if len(table_namespaces) != 1:
        raise SolverRunnerValidationError("candidate table namespace is invalid")
    try:
        parsed_ast = (
            parse_sql_candidate(candidate.sql, dsn, candidate_id)
            if parsed_candidate is None
            else parsed_candidate
        )
        semantic_ast = build_semantic_ast(
            candidate,
            parsed_ast,
            state.query_spec,
            requirements,
            next(iter(table_namespaces)),
        )
    except Exception as exc:
        raise SolverRunnerValidationError(
            "candidate semantic AST cannot be rebuilt"
        ) from exc
    if (
        research_state.query_spec != state.query_spec
        or semantic_ast.coverage.requirements_digest != requirements.requirements_digest
    ):
        raise SolverRunnerValidationError("saved candidate semantic authority mismatch")
    return SemanticCheckInput(
        semantic_ast=semantic_ast,
        query_spec=state.query_spec,
        requirements=requirements,
    )


def _run_checker_stage(
    state: SolverState,
    kind: CheckKind,
    deadline: DeadlineBudget,
    is_cancelled: Callable[[], bool],
    commit_transition: CommitTransition,
    call: Callable[[], CheckResult],
) -> tuple[SolverState, bool]:
    _require_active(deadline, is_cancelled, kind)
    try:
        check = call()
    except (asyncio.CancelledError, WorkflowDeadlineExceeded):
        _require_active(deadline, is_cancelled, kind)
        raise
    except Exception:
        _require_active(deadline, is_cancelled, kind)
        check = _malformed_check(state.sql_candidates[-1].candidate_id, kind)
    else:
        _require_active(deadline, is_cancelled, kind)
    confirmed = _commit_gate_result(
        state,
        check,
        None,
        deadline,
        is_cancelled,
        commit_transition,
    )
    return confirmed, check.status is CheckStatus.PASSED


def _commit_gate_result(
    state: SolverState,
    check: CheckResult,
    execution: ExecutionResult | None,
    deadline: DeadlineBudget,
    is_cancelled: Callable[[], bool],
    commit_transition: CommitTransition,
) -> SolverState:
    _require_active(deadline, is_cancelled, check.check_kind)
    if execution is None:
        transition = append_solver_check_result(
            state,
            check,
            base_revision=state.revision,
        )
    else:
        transition = append_solver_execution_result(
            state,
            check,
            execution,
            base_revision=state.revision,
        )
    _require_active(deadline, is_cancelled, check.check_kind)
    return _commit(transition, commit_transition)


def _require_active(
    deadline: DeadlineBudget,
    is_cancelled: Callable[[], bool],
    kind: CheckKind,
) -> None:
    cancelled = is_cancelled()
    if type(cancelled) is not bool:
        raise SolverRunnerValidationError("is_cancelled must return a boolean")
    if cancelled:
        raise asyncio.CancelledError
    deadline.require_remaining(f"solver {kind.value} gate")


def _new_execution_id(state: SolverState, id_factory: Callable[[], str]) -> str:
    try:
        raw = id_factory()
        if type(raw) is not str:
            raise TypeError("execution ID must be an exact str")
        execution_id = _ID_ADAPTER.validate_python(raw, strict=True)
    except (ValidationError, TypeError, ValueError) as exc:
        raise SolverRunnerValidationError("id_factory returned an invalid Id") from exc
    existing = {
        *(item.execution_id for item in state.execution_results),
        *(item.check_id for item in state.check_results),
        *(item.candidate_id for item in state.sql_candidates),
        *(item.action_id for item in state.action_history),
        *(item.missing_evidence_request_id for item in state.missing_evidence_requests),
    }
    if execution_id in existing:
        raise SolverConflictError("execution ID collides with SolverState")
    return execution_id


def _validate_deadline(deadline: object) -> None:
    if type(deadline) is not DeadlineBudget:
        raise SolverRunnerValidationError("deadline must be exact DeadlineBudget")
    if (
        type(deadline.deadline_monotonic) not in {int, float}
        or not math.isfinite(deadline.deadline_monotonic)
        or type(deadline.deadline_at_ms) is not int
        or deadline.deadline_at_ms < 0
        or not callable(deadline.monotonic)
        or not callable(deadline.wall_time)
    ):
        raise SolverRunnerValidationError("deadline has invalid exact fields")
    try:
        checked = DeadlineBudget(
            deadline_monotonic=deadline.deadline_monotonic,
            deadline_at_ms=deadline.deadline_at_ms,
            monotonic=deadline.monotonic,
            wall_time=deadline.wall_time,
        )
    except (TypeError, ValueError) as exc:
        raise SolverRunnerValidationError("deadline is invalid") from exc
    if not exact_value(deadline, checked):
        raise SolverRunnerValidationError("deadline is not canonical")


def _exact_dataclass_tree(value: object) -> bool:
    """Accept exact dataclasses containing exact built-in values only."""

    if type(value) is LoadedSchema:
        allowed_dataclasses = {LoadedSchema, SchemaNamespace, SchemaScope}
    elif type(value) is TextToSqlSafetyPolicy:
        allowed_dataclasses = {TextToSqlSafetyPolicy}
    else:
        return False
    return _exact_dataclass_value(value, set(), allowed_dataclasses)


def _exact_dataclass_value(
    value: object,
    active: set[int],
    allowed_dataclasses: set[type],
) -> bool:
    if value is None or type(value) in {str, int, bool}:
        return True
    if type(value) is float:
        return math.isfinite(value)

    is_model = (
        type(value) in allowed_dataclasses
        and is_dataclass(value)
        and not isinstance(value, type)
    )
    if not is_model and type(value) not in {dict, list, tuple, frozenset}:
        return False
    identity = id(value)
    if identity in active:
        return False
    active.add(identity)
    try:
        if is_model:
            declared = tuple(field.name for field in fields(value))
            model_dict = getattr(value, "__dict__", None)
            if type(model_dict) is not dict or set(model_dict) != set(declared):
                return False
            return all(
                _exact_dataclass_value(
                    getattr(value, name),
                    active,
                    allowed_dataclasses,
                )
                for name in declared
            )
        if type(value) is dict:
            return all(
                _exact_dataclass_value(key, active, allowed_dataclasses)
                and _exact_dataclass_value(item, active, allowed_dataclasses)
                for key, item in value.items()
            )
        return all(
            _exact_dataclass_value(item, active, allowed_dataclasses) for item in value
        )
    finally:
        active.remove(identity)


def _malformed_check(candidate_id: str, kind: CheckKind) -> CheckResult:
    return CheckResult(
        check_id=f"{kind.value}:{candidate_id}:check_malformed",
        candidate_id=candidate_id,
        check_kind=kind,
        status=CheckStatus.INCONCLUSIVE,
        failure_code=CheckFailureCode.CHECK_MALFORMED,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=f"invalid {kind.value} checker result",
        repair=CheckRepair(kind=RepairKind.REVISE_SQL),
    )


def _commit(
    transition: SolverGateTransitionResult,
    commit_transition: CommitTransition,
) -> SolverState:
    try:
        confirmed = commit_transition(transition)
    except (asyncio.CancelledError, WorkflowDeadlineExceeded):
        raise
    except Exception as exc:
        raise SolverCommitError("solver gate commit failed") from exc
    confirmed = revalidate_exact_model(
        confirmed,
        SolverState,
        SolverCommitError,
        "committed state",
    )
    if not exact_value(confirmed, transition.state):
        raise SolverCommitError("committed state does not confirm transition")
    return confirmed


__all__ = [
    "CommitTransition",
    "FinalQueryExecutor",
    "SolverCommitError",
    "SolverRunnerError",
    "SolverRunnerValidationError",
    "run_solver_candidate_gates",
]
