"""One logical, targeted research re-entry for a missing-evidence request."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import inspect
from typing import Protocol

from ._exact_contract import exact_value, revalidate_exact_model
from ._policy_common import BudgetAdmissionError, BudgetExhaustedError
from .freshness import FreshnessContext
from .model_budget import ModelUsageBudgetError
from .models import (
    ColumnRef,
    DocumentRef,
    DocumentRuleBinding,
    HypothesisStatus,
    QueryProbeRef,
    ResearchReentryRecord,
    ResearchReentryStatus,
    ResearchState,
    SolverState,
    StrictModel,
    TableRef,
)
from .research_decision import (
    ResearchDecisionV1 as _CANONICAL_RESEARCH_DECISION_TYPE,
    ToolIntent as _CANONICAL_TOOL_INTENT_TYPE,
)
from .probes import ProbeStatus
from .semantic_coverage import CoverageRequirements, validate_coverage_inputs
from .serialization import canonical_json_bytes
from .solver_loop import (
    ResearchReentryTransitionResult,
    admit_targeted_reentry,
    finalize_targeted_reentry,
)


class ResearchReentryError(ValueError):
    """Trusted coordinator input or a W3 result violates the closed flow."""


class ResearchReentryDeadline(Protocol):
    def require_remaining(self, operation: str) -> float: ...


W3Boundary = Callable[..., object]
SolverAdmissionCommit = Callable[[ResearchReentryTransitionResult], SolverState]


@dataclass(frozen=True, slots=True)
class ResearchReentryOutcome:
    solver_state: SolverState
    research_state: ResearchState
    record: ResearchReentryRecord
    freshness_context: FreshnessContext | None = None
    requirements: CoverageRequirements | None = None


_TARGET_TYPES = (TableRef, ColumnRef, DocumentRef, QueryProbeRef)
_ACTIVE_HYPOTHESIS_STATUSES = {
    HypothesisStatus.PROPOSED,
    HypothesisStatus.TESTING,
    HypothesisStatus.SUPPORTED,
}


async def run_targeted_research_reentry(
    solver_state: SolverState,
    research_state: ResearchState,
    missing_evidence_request_id: str,
    *,
    requirements: CoverageRequirements,
    freshness_context: FreshnessContext,
    loaded_schema: object,
    registry: object,
    decision_model_type: type[StrictModel],
    propose_decision: W3Boundary,
    resolve_decision: W3Boundary,
    execute_probe: W3Boundary,
    commit_research_turn: W3Boundary,
    deadline: ResearchReentryDeadline | None,
    is_cancelled: Callable[[], bool],
    id_factory: Callable[[], str],
    commit_solver_admission: SolverAdmissionCommit | None = None,
) -> ResearchReentryOutcome:
    """Run exactly one W3 decision and keep every failure logically terminal."""

    admitted = admit_targeted_reentry(
        solver_state,
        research_state,
        missing_evidence_request_id,
        base_revision=solver_state.revision,
        id_factory=id_factory,
    )
    _commit_admission(admitted, commit_solver_admission)
    current_research = research_state
    try:
        if not all(
            callable(value)
            for value in (
                propose_decision,
                resolve_decision,
                execute_probe,
                commit_research_turn,
                is_cancelled,
            )
        ):
            raise ResearchReentryError("W3 boundaries must be callable")
        if deadline is not None and not callable(
            getattr(deadline, "require_remaining", None)
        ):
            raise ResearchReentryError("deadline must expose require_remaining")
        if decision_model_type is not _CANONICAL_RESEARCH_DECISION_TYPE:
            raise ResearchReentryError(
                "decision_model_type must be the exact W3 decision model"
            )
        current_research = revalidate_exact_model(
            research_state,
            ResearchState,
            ResearchReentryError,
            "research_state",
        )
        freshness = revalidate_exact_model(
            freshness_context,
            FreshnessContext,
            ResearchReentryError,
            "freshness_context",
        )
        authority = revalidate_exact_model(
            requirements,
            CoverageRequirements,
            ResearchReentryError,
            "requirements",
        )
        rebuilt = validate_coverage_inputs(
            current_research,
            freshness,
            current_research.run_id,
            current_research.run_incarnation,
        )
        if not exact_value(authority, rebuilt):
            raise ResearchReentryError(
                "requirements must be exact current W4 authority"
            )
        request = next(
            item
            for item in admitted.state.missing_evidence_requests
            if item.missing_evidence_request_id == missing_evidence_request_id
        )
        trusted_targets = _trusted_targets(
            request.source_id, current_research, authority
        )
        if request.candidate_targets and not all(
            _is_allowed_target(candidate, trusted_targets)
            for candidate in request.candidate_targets
        ):
            raise ResearchReentryError(
                "request candidate targets exceed trusted research scope"
            )

        boundary = _boundary_status(deadline, is_cancelled, "research decision")
        if boundary is not None:
            return _terminal(admitted, current_research, boundary)
        _require_model_capacity(current_research)
        decision = propose_decision(
            task=request.question,
            research_context=_research_context(request, trusted_targets),
        )
        if inspect.isawaitable(decision):
            decision = await decision
        decision = revalidate_exact_model(
            decision,
            _CANONICAL_RESEARCH_DECISION_TYPE,
            ResearchReentryError,
            "research decision",
        )
        if not _is_exact_tool_decision(decision):
            raise ResearchReentryError(
                "targeted re-entry requires no proposals and exactly one ToolIntent"
            )

        boundary = _boundary_status(deadline, is_cancelled, "research resolution")
        if boundary is not None:
            return _terminal(admitted, current_research, boundary)
        resolved = resolve_decision(
            current_research,
            decision,
            loaded_schema=loaded_schema,
            freshness_context=freshness,
            registry=registry,
            deadline=deadline,
        )
        claim = getattr(resolved, "tool_claim", None)
        if (
            claim is None
            or getattr(resolved, "invocation", None) is None
            or not _is_allowed_target(claim.target, trusted_targets)
        ):
            raise ResearchReentryError(
                "resolved W3 target is outside trusted research scope"
            )

        boundary = _boundary_status(deadline, is_cancelled, "research probe")
        if boundary is not None:
            return _terminal(admitted, current_research, boundary)
        _require_probe_capacity(current_research)
        try:
            probe_result = execute_probe(resolved, registry)
        except (TimeoutError, BudgetAdmissionError, ModelUsageBudgetError):
            raise
        except Exception:
            return _terminal(
                admitted,
                current_research,
                ResearchReentryStatus.TOOL_FAILURE,
            )
        if probe_result is None:
            raise ResearchReentryError("ToolIntent did not produce a probe result")
        committed = commit_research_turn(
            resolved.admission,
            probe_result=probe_result,
        )
        current_research = revalidate_exact_model(
            committed.state,
            ResearchState,
            ResearchReentryError,
            "committed research state",
        )
        if current_research.revision != research_state.revision + 1:
            raise ResearchReentryError(
                "one targeted W3 turn must produce exactly one research revision"
            )
        probe_status = getattr(probe_result, "status", None)
        if probe_status is ProbeStatus.CANCELLED:
            return _terminal(
                admitted, current_research, ResearchReentryStatus.CANCELLED
            )
        if probe_status is ProbeStatus.TIMED_OUT:
            return _terminal(
                admitted,
                current_research,
                ResearchReentryStatus.DEADLINE_EXCEEDED,
            )
        if probe_status is not ProbeStatus.SUCCESS:
            return _terminal(
                admitted,
                current_research,
                ResearchReentryStatus.TOOL_FAILURE,
            )

        boundary = _boundary_status(deadline, is_cancelled, "research finalization")
        if boundary is not None:
            return _terminal(admitted, current_research, boundary)
        freshness = FreshnessContext(
            evaluated_at=datetime.now(UTC),
            run_id=freshness.run_id,
            run_incarnation=freshness.run_incarnation,
            schema_namespace_version=freshness.schema_namespace_version,
            document_sources=freshness.document_sources,
            data_snapshots=freshness.data_snapshots,
        )
        fresh_authority = validate_coverage_inputs(
            current_research,
            freshness,
            current_research.run_id,
            current_research.run_incarnation,
        )
        finalized = finalize_targeted_reentry(
            admitted.state,
            admitted.record.research_reentry_id,
            ResearchReentryStatus.COMPLETED,
            base_revision=admitted.state.revision,
            research_state=current_research,
            freshness_context=freshness,
            requirements=fresh_authority,
        )
        return ResearchReentryOutcome(
            finalized.state,
            current_research,
            finalized.record,
            freshness,
            fresh_authority,
        )
    except asyncio.CancelledError:
        return _terminal(admitted, current_research, ResearchReentryStatus.CANCELLED)
    except TimeoutError:
        return _terminal(
            admitted,
            current_research,
            ResearchReentryStatus.DEADLINE_EXCEEDED,
        )
    except (BudgetAdmissionError, ModelUsageBudgetError):
        return _terminal(
            admitted,
            current_research,
            ResearchReentryStatus.BUDGET_EXHAUSTED,
        )
    except Exception:
        return _terminal(
            admitted,
            current_research,
            ResearchReentryStatus.PROTOCOL_FAILURE,
        )


def _commit_admission(
    admitted: ResearchReentryTransitionResult,
    commit: SolverAdmissionCommit | None,
) -> None:
    if commit is None:
        return
    committed = commit(admitted)
    if type(committed) is not SolverState or not exact_value(
        committed,
        admitted.state,
    ):
        raise ResearchReentryError(
            "solver admission commit did not confirm the exact state"
        )


def _is_exact_tool_decision(decision: object) -> bool:
    next_step = getattr(decision, "next", None)
    proposals = getattr(decision, "proposals", None)
    return (
        type(decision) is _CANONICAL_RESEARCH_DECISION_TYPE
        and type(proposals) is tuple
        and proposals == ()
        and type(next_step) is _CANONICAL_TOOL_INTENT_TYPE
    )


def _require_model_capacity(state: ResearchState) -> None:
    budget = state.budget_state
    if budget.remaining_model_calls < 1 or budget.remaining_model_tokens < 1:
        raise BudgetExhaustedError("research model budget is exhausted")


def _require_probe_capacity(state: ResearchState) -> None:
    budget = state.budget_state
    if any(
        getattr(budget, field) < 1
        for field in (
            "remaining_wall_clock_ms",
            "remaining_db_probe_ms",
            "remaining_rows",
            "remaining_bytes",
        )
    ):
        raise BudgetExhaustedError("research probe budget is exhausted")


def _trusted_targets(
    source_id: str,
    research_state: ResearchState,
    requirements: CoverageRequirements,
) -> tuple[TableRef | ColumnRef | DocumentRef | QueryProbeRef, ...]:
    selected = tuple(
        binding
        for binding in requirements.selected_bindings
        if binding.source_id == source_id
    )
    if not selected:
        raise ResearchReentryError(
            "request source must have a selected W4 binding"
        )
    targets = [target for binding in selected for target in _binding_targets(binding)]
    for hypothesis in research_state.hypotheses:
        if (
            source_id in hypothesis.source_ids
            and hypothesis.status in _ACTIVE_HYPOTHESIS_STATUSES
        ):
            targets.extend(hypothesis.candidate_targets)
    unique = {
        canonical_json_bytes(target): target
        for target in targets
        if type(target) in _TARGET_TYPES
    }
    return tuple(unique[key] for key in sorted(unique))


def _binding_targets(binding: StrictModel):
    yield from binding.tables
    yield from binding.columns
    if type(binding) is DocumentRuleBinding:
        yield binding.document


def _is_allowed_target(candidate, trusted_targets) -> bool:
    return any(exact_value(candidate, trusted) for trusted in trusted_targets)


def _research_context(request, trusted_targets) -> str:
    return canonical_json_bytes(
        {
            "missing_evidence_request_id": request.missing_evidence_request_id,
            "required_evidence_kind": request.required_evidence_kind,
            "source_id": request.source_id,
            "trusted_targets": trusted_targets,
        }
    ).decode("utf-8")


def _boundary_status(
    deadline: ResearchReentryDeadline | None,
    is_cancelled: Callable[[], bool],
    label: str,
) -> ResearchReentryStatus | None:
    cancelled = is_cancelled()
    if type(cancelled) is not bool:
        raise ResearchReentryError("is_cancelled must return a boolean")
    if cancelled:
        return ResearchReentryStatus.CANCELLED
    if deadline is not None:
        deadline.require_remaining(label)
    return None


def _terminal(
    admitted: ResearchReentryTransitionResult,
    research_state: ResearchState,
    status: ResearchReentryStatus,
) -> ResearchReentryOutcome:
    finalized = finalize_targeted_reentry(
        admitted.state,
        admitted.record.research_reentry_id,
        status,
        base_revision=admitted.state.revision,
    )
    return ResearchReentryOutcome(finalized.state, research_state, finalized.record)
