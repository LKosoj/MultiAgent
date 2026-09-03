"""Build the one post-execution result-review capability from Typed runtime."""

from __future__ import annotations

from llm_call_context import llm_call_context

from workflow.deadline import DeadlineBudget

from custom_tools.text_to_sql.schema_loader import LoadedSchema
from custom_tools.text_to_sql.schema_metadata import get_foreign_key_constraints
from custom_tools.text_to_sql.validators import SchemaLimiter

from .result_review import (
    RESULT_REVIEW_RUNTIME_KEY,
    _ModelReviewResponse,
    create_result_review_capability,
)
from .semantic_coverage import CoverageRequirements
from .result_validation_runtime import _persisted_sql_proposal_requirements


INVALID_RESULT_REVIEW_RUNTIME = object()


def _bounded_review_schema(
    loaded: LoadedSchema,
    requirements: CoverageRequirements,
    question: str,
) -> dict[str, object]:
    schema = loaded.schema
    if type(schema) is not dict:
        raise TypeError("loaded schema is invalid")

    selected: list[str] = []
    for table in requirements.allowed_tables:
        matches = [
            name
            for name in schema
            if name.split(".")[-1] == table.table
            and (
                table.schema_name is None
                or (
                    len(name.split(".")) > 1
                    and name.split(".")[-2] == table.schema_name
                )
            )
        ]
        if len(matches) != 1:
            raise ValueError("allowed table does not resolve uniquely in loaded schema")
        if matches[0] not in selected:
            selected.append(matches[0])

    neighbors: set[str] = set()
    selected_set = set(selected)
    for source_name in schema:
        for constraint in get_foreign_key_constraints(source_name, schema):
            target_name = constraint["to_table"]
            if source_name in selected_set and target_name not in selected_set:
                neighbors.add(target_name)
            if target_name in selected_set and source_name not in selected_set:
                neighbors.add(source_name)

    relevant = {name: schema[name] for name in (*selected, *sorted(neighbors))}
    return SchemaLimiter(priority_strategy="insertion").limit_schema_for_prompt(
        relevant,
        query_terms=question.split(),
    )


def build_result_review_runtime(runtime: object, *, sql_query: object) -> object:
    """Return the bound reviewer, or an explicit invalid sentinel."""

    try:
        from agent_command import create_text_to_sql_model
        from utils import call_openai_api
        from workflow._text_to_sql_document_authority import (
            solver_document_freshness_reference,
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
        from .sql_ast import parse_sql_candidate

        state = getattr(runtime, "verified_research_state", None)
        solver_state = getattr(runtime, "verified_solver_state", None)
        outcome = getattr(runtime, "verified_research_outcome", None)
        candidate_id = getattr(runtime, "verified_solver_candidate_id", None)
        policy = getattr(runtime, "verified_research_policy", None)
        limits = getattr(policy, "model_budget", None)
        output_tokens = getattr(limits, "output_tokens_per_call", None)
        input_tokens = getattr(limits, "input_tokens_per_call", None)
        dsn = getattr(runtime, "dsn", None)
        deadline = getattr(runtime, "deadline", None)
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
            or type(deadline) is not DeadlineBudget
            or type(output_tokens) is not int
            or output_tokens <= 0
            or type(input_tokens) is not int
            or input_tokens <= 0
            or runtime.run_id != state.run_id
            or runtime.run_incarnation != state.run_incarnation
            or runtime.query != state.query_spec.original_text
        ):
            raise TypeError("verified Typed result review runtime is incomplete")
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
            tuple(check.check_kind for check in checks) != expected_checks
            or any(check.status is not CheckStatus.PASSED for check in checks)
            or candidate.candidate_id != candidate_id
            or candidate.revision != state.revision
            or candidate.sql != sql_query
        ):
            raise ValueError("verified solver candidate does not match finalizer")
        requirements = _persisted_sql_proposal_requirements(
            runtime,
            state,
            solver_state,
            candidate,
        )
        freshness = solver_document_freshness_reference(runtime, state)
        parsed_ast = parse_sql_candidate(candidate.sql, dsn, candidate.candidate_id)
        schema = _bounded_review_schema(
            runtime.loaded_schema,
            requirements,
            runtime.query,
        )
        review_schema = _ModelReviewResponse.model_json_schema()
        review_schema["required"] = list(review_schema["properties"])
        review_schema["properties"]["source_id"]["anyOf"][0] = {
            "enum": sorted(
                {binding.source_id for binding in requirements.selected_bindings}
            ),
            "type": "string",
        }
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "ModelReviewResponse",
                "strict": True,
                "schema": review_schema,
            },
        }

        def review(prompt: str) -> str:
            if len(prompt.encode("utf-8")) > input_tokens * 4:
                raise ValueError("result review context exceeds configured model input")
            timeout_seconds = deadline.require_remaining(
                "text_to_sql_result_review_call"
            )
            provider = create_text_to_sql_model(
                "model_hard",
                max_tokens=output_tokens,
                temperature=0.3,
                timeout_seconds=timeout_seconds,
                client_max_retries=0,
            )
            with llm_call_context(
                run_id=runtime.run_id, step_name=RESULT_REVIEW_RUNTIME_KEY
            ):
                response = call_openai_api(
                    prompt=prompt,
                    model=provider,
                    max_tokens=output_tokens,
                    temperature=0.3,
                    max_retries=0,
                    response_format=response_format,
                )
            deadline.require_remaining("text_to_sql_result_review_response")
            if type(response) is bytes:
                response = response.decode("utf-8", errors="strict")
            if type(response) is not str:
                raise TypeError("result review response is not text")
            if len(response.encode("utf-8")) > max(1_024, output_tokens * 8):
                raise ValueError("result review response exceeds its safety envelope")
            return response

        return create_result_review_capability(
            state=state,
            requirements=requirements,
            freshness_context=freshness,
            candidate=candidate,
            parsed_ast=parsed_ast,
            documents=runtime.document_snapshot,
            model=review,
            schema=schema,
        )
    except Exception:
        return INVALID_RESULT_REVIEW_RUNTIME


__all__ = ["INVALID_RESULT_REVIEW_RUNTIME", "build_result_review_runtime"]
