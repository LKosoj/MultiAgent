"""Build the post-execution capability from a verified Typed runtime."""

from __future__ import annotations

from .result_validation import create_result_validation_capability


INVALID_RESULT_VALIDATION_RUNTIME = object()


def _persisted_sql_proposal_requirements(
    runtime: object,
    state: object,
    solver_state: object,
    candidate: object,
):
    """Load the one replayed authority that created the selected SQL candidate."""

    from workflow.adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore

    from ._semantic_coverage_footprint import normalized_state_digest
    from .models import ResearchState, SolverState, SqlCandidate
    from .replay_inputs import SolverSqlProposalReplayInput
    from .serialization import canonical_digest
    from .solver_protocol import SqlCandidateProposal

    store = getattr(runtime, "solver_checkpoint_store", None)
    if (
        type(state) is not ResearchState
        or type(solver_state) is not SolverState
        or type(candidate) is not SqlCandidate
        or type(store) is not AdaptiveSolverCheckpointStore
    ):
        raise TypeError("selected solver proposal replay authority is unavailable")
    chain = store.load_replay_chain(state.run_id, state.run_incarnation)
    if (
        chain is None
        or chain.run_id != state.run_id
        or chain.run_incarnation != state.run_incarnation
        or chain.state_revision != solver_state.revision
        or chain.state_digest != canonical_digest(solver_state)
        or not chain.snapshots
        or chain.snapshots[-1].state != solver_state
        or candidate not in solver_state.sql_candidates
    ):
        raise ValueError("selected solver proposal replay state is invalid")
    matches = []
    for action in chain.actions:
        replay = store.load_transition_replay_input(
            state.run_id,
            state.run_incarnation,
            action.action_revision,
        )
        if type(replay) is not SolverSqlProposalReplayInput:
            continue
        parsed = replay.parsed_candidate.to_candidate()
        proposal = replay.proposal.proposal
        requirements = replay.requirements
        if (
            parsed.candidate_id == candidate.candidate_id
            and parsed.candidate_digest == candidate.normalized_ast_digest
            and replay.generated_ids[0] == candidate.candidate_id
            and type(proposal) is SqlCandidateProposal
            and proposal.sql == candidate.sql
            and requirements.run_id == state.run_id
            and requirements.run_incarnation == state.run_incarnation
            and requirements.schema_namespace_version == state.schema_namespace_version
            and requirements.state_revision == candidate.revision
            and requirements.state_digest == normalized_state_digest(state)
        ):
            matches.append(requirements)
    if len(matches) != 1:
        raise ValueError("selected solver proposal replay authority is invalid")
    return matches[0]


def build_result_validation_runtime(
    runtime: object,
    *,
    sql_query: object,
) -> object:
    """Return a bound capability or a present invalid value on bridge failure."""

    try:
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
        requirements = _persisted_sql_proposal_requirements(
            runtime,
            state,
            solver_state,
            candidate,
        )
        freshness_context = solver_document_freshness_reference(runtime, state)
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
