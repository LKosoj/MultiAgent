"""Build the post-execution capability from a verified Typed runtime."""

from __future__ import annotations

from .result_validation import create_result_validation_capability


INVALID_RESULT_VALIDATION_RUNTIME = object()


def build_result_validation_runtime(
    runtime: object,
    *,
    sql_query: object,
) -> object:
    """Return a bound capability or a present invalid value on bridge failure."""

    try:
        from workflow._text_to_sql_document_authority import (
            live_solver_document_freshness_context,
        )
        from workflow.text_to_sql_typed_runtime import (
            TextToSqlTypedRuntime,
            _ADMISSION_CAPABILITY,
        )

        from .models import (
            CheckKind,
            CheckStatus,
            ResearchState,
            ResearchStopReason,
            SolverState,
        )
        from .semantic_coverage import validate_coverage_inputs
        from .sql_ast import parse_sql_candidate

        state = getattr(runtime, "verified_research_state", None)
        solver_state = getattr(runtime, "verified_solver_state", None)
        outcome = getattr(runtime, "verified_research_outcome", None)
        candidate_id = getattr(runtime, "verified_solver_candidate_id", None)
        dsn = getattr(runtime, "dsn", None)
        if (
            type(runtime) is not TextToSqlTypedRuntime
            or runtime._capability is not _ADMISSION_CAPABILITY
            or getattr(outcome, "stop_reason", None) is not ResearchStopReason.COMPLETE
            or type(state) is not ResearchState
            or type(solver_state) is not SolverState
            or type(candidate_id) is not str
            or not candidate_id
            or type(sql_query) is not str
            or not sql_query.strip()
            or type(dsn) is not str
            or not dsn.strip()
            or runtime.run_id != state.run_id
            or runtime.run_incarnation != state.run_incarnation
            or runtime.query != state.query_spec.original_text
            or solver_state.run_id != state.run_id
            or solver_state.run_incarnation != state.run_incarnation
            or solver_state.schema_namespace_version != state.schema_namespace_version
            or solver_state.query_spec != state.query_spec
        ):
            raise TypeError("verified Typed result validation runtime is incomplete")
        if not solver_state.sql_candidates or solver_state.stop_reason is not None:
            raise ValueError("verified solver candidate is not ready")
        candidate = solver_state.sql_candidates[-1]
        checks = tuple(
            check
            for check in solver_state.check_results
            if check.candidate_id == candidate.candidate_id
        )
        expected_checks = (
            CheckKind.SAFETY,
            CheckKind.SCHEMA,
            CheckKind.SEMANTIC,
            CheckKind.EXPLAIN,
        )
        if (
            len(checks) != len(expected_checks)
            or any(
                check.check_kind is not kind or check.status is not CheckStatus.PASSED
                for check, kind in zip(checks, expected_checks, strict=True)
            )
            or candidate.candidate_id != candidate_id
            or candidate.revision != state.revision
            or candidate.sql != sql_query
        ):
            raise ValueError("verified solver candidate does not match finalizer")
        freshness_context = live_solver_document_freshness_context(runtime, state)
        requirements = validate_coverage_inputs(
            state,
            freshness_context,
            state.run_id,
            state.run_incarnation,
        )
        parsed_ast = parse_sql_candidate(candidate.sql, dsn, candidate.candidate_id)
        return create_result_validation_capability(
            state=state,
            requirements=requirements,
            freshness_context=freshness_context,
            candidate=candidate,
            parsed_ast=parsed_ast,
        )
    except Exception:
        return INVALID_RESULT_VALIDATION_RUNTIME


__all__ = [
    "INVALID_RESULT_VALIDATION_RUNTIME",
    "build_result_validation_runtime",
]
