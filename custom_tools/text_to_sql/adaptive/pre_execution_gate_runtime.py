"""Build the pre-execution capability from a verified Typed runtime."""

from __future__ import annotations

from workflow.deadline import DeadlineBudget

from .models import ResearchState
from .pre_execution_gate import _create_capturing_pre_execution_gate_capability
from .semantic_coverage import validate_coverage_inputs


INVALID_PRE_EXECUTION_GATE_RUNTIME = object()


def _default_table_namespace(dsn: str) -> str:
    import db_plugins

    plugin = db_plugins.get_plugin(dsn)
    namespace = plugin.parse_schema_from_dsn(dsn)
    if isinstance(namespace, str) and namespace.strip():
        return namespace
    namespace = plugin.get_default_schema()
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("database plugin returned no default schema")
    return namespace


def build_pre_execution_gate_runtime(
    runtime: object,
    *,
    sql_query: object,
) -> object:
    """Return a bound capability or a present invalid sentinel."""

    try:
        from custom_tools.text_to_sql.schema_loader import LoadedSchema
        from workflow.text_to_sql_typed_runtime import TextToSqlTypedRuntime

        from .models import ResearchStopReason

        outcome = getattr(runtime, "verified_research_outcome", None)
        state = getattr(runtime, "verified_research_state", None)
        dsn = getattr(runtime, "dsn", None)
        deadline = getattr(runtime, "deadline", None)
        is_cancelled = getattr(runtime, "is_cancelled", None)
        loaded_schema = getattr(runtime, "loaded_schema", None)
        if (
            not isinstance(runtime, TextToSqlTypedRuntime)
            or getattr(outcome, "stop_reason", None) is not ResearchStopReason.COMPLETE
            or type(state) is not ResearchState
            or type(dsn) is not str
            or not dsn.strip()
            or type(sql_query) is not str
            or not sql_query.strip()
            or not isinstance(deadline, DeadlineBudget)
            or not callable(is_cancelled)
            or type(loaded_schema) is not LoadedSchema
        ):
            raise TypeError("verified Typed runtime is incomplete")
        from workflow._text_to_sql_document_authority import (
            live_solver_document_freshness_context,
        )

        context = live_solver_document_freshness_context(runtime, state)
        requirements = validate_coverage_inputs(
            state,
            context,
            state.run_id,
            state.run_incarnation,
        )
        return _create_capturing_pre_execution_gate_capability(
            runtime,
            state=state,
            requirements=requirements,
            dsn=dsn,
            table_namespace=_default_table_namespace(dsn),
            expected_sql=sql_query,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            deadline=deadline,
            is_cancelled=is_cancelled,
        )
    except Exception:
        return INVALID_PRE_EXECUTION_GATE_RUNTIME


__all__ = [
    "INVALID_PRE_EXECUTION_GATE_RUNTIME",
    "build_pre_execution_gate_runtime",
]
