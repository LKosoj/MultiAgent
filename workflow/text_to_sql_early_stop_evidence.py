"""Closed semantic evidence for adaptive Text-to-SQL early stops."""

from __future__ import annotations

from typing import Any

from custom_tools.text_to_sql.adaptive.models import (
    MissingEvidenceRequest,
    QuerySpec,
    ResearchState,
    ResearchStopReason,
    SemanticItemKind,
    SolverState,
    SolverStopReason,
)
from custom_tools.text_to_sql.adaptive.research_loop import ResearchLoopOutcome
from custom_tools.text_to_sql.adaptive.serialization import canonical_digest


_REQUIREMENT_BY_KIND = {
    SemanticItemKind.METRIC: "required_metric",
    SemanticItemKind.DIMENSION: "required_dimension",
    SemanticItemKind.FILTER: "required_filter",
    SemanticItemKind.ORDERING: "required_ordering",
    SemanticItemKind.LIMIT: "required_limit",
    SemanticItemKind.TIME: "required_time",
    SemanticItemKind.FORMULA: "required_formula",
}

_RESEARCH_ROOTS = {
    ResearchStopReason.AMBIGUOUS: (
        "RESEARCH_AMBIGUOUS",
        "ambiguous",
        "ambiguous_requirement",
    ),
    ResearchStopReason.UNSUPPORTED: (
        "RESEARCH_UNSUPPORTED",
        "unsupported",
        "unsupported_requirement",
    ),
}


def _typed_requirement(
    query_spec: object,
    source_ids: object,
) -> str | None:
    if type(query_spec) is not QuerySpec or type(source_ids) is not tuple:
        return None
    if not source_ids or any(type(source_id) is not str for source_id in source_ids):
        return None
    items_by_source_id = {item.source_id: item for item in query_spec.semantic_items}
    requirements: set[str] = set()
    for source_id in source_ids:
        item = items_by_source_id.get(source_id)
        if item is None or item.required is not True:
            return None
        requirement = _REQUIREMENT_BY_KIND.get(item.kind)
        if requirement is None:
            return None
        requirements.add(requirement)
    if len(requirements) != 1:
        return None
    return requirements.pop()


def _receipt(
    *,
    terminal_source: str,
    root_mechanism: str,
    error_class: str,
    violated_typed_requirement: str,
    pipeline_component: str,
    state: ResearchState | SolverState,
) -> dict[str, Any] | None:
    try:
        state_sha256 = canonical_digest(state)
    except (TypeError, ValueError):
        return None
    return {
        "schema_version": 1,
        "record_kind": "text2sql_adaptive_early_stop_evidence",
        "terminal_source": terminal_source,
        "root_mechanism": root_mechanism,
        "error_class": error_class,
        "violated_typed_requirement": violated_typed_requirement,
        "pipeline_component": pipeline_component,
        "state_sha256": state_sha256,
    }


def build_text_to_sql_early_stop_evidence(
    *,
    research_outcome: object,
    solver_state: object,
    terminal_status: object,
    terminal_reason_code: object,
) -> dict[str, Any] | None:
    """Derive a non-case-specific receipt from verified Typed state."""

    if terminal_status != "abstained" or type(terminal_reason_code) is not str:
        return None

    if terminal_reason_code in {"RESEARCH_AMBIGUOUS", "RESEARCH_UNSUPPORTED"}:
        if type(research_outcome) is not ResearchLoopOutcome:
            return None
        state = research_outcome.final_state
        if type(state) is not ResearchState:
            return None
        root = _RESEARCH_ROOTS.get(research_outcome.stop_reason)
        if root is None or root[0] != terminal_reason_code:
            return None
        if state.stop_reason not in {None, research_outcome.stop_reason}:
            return None
        requirement = _typed_requirement(
            state.query_spec,
            research_outcome.affected_source_ids,
        )
        if requirement is None:
            return None
        return _receipt(
            terminal_source="research",
            root_mechanism=root[1],
            error_class=root[2],
            violated_typed_requirement=requirement,
            pipeline_component="adaptive_schema_research",
            state=state,
        )

    if terminal_reason_code != "SCHEMA_CLARIFICATION_REQUIRED":
        return None
    if (
        type(solver_state) is not SolverState
        or solver_state.stop_reason is not SolverStopReason.MISSING_EVIDENCE
        or not solver_state.missing_evidence_requests
    ):
        return None
    request = solver_state.missing_evidence_requests[-1]
    if type(request) is not MissingEvidenceRequest:
        return None
    requirement = _typed_requirement(solver_state.query_spec, (request.source_id,))
    if requirement is None:
        return None
    return _receipt(
        terminal_source="solver",
        root_mechanism="missing_evidence",
        error_class="missing_evidence",
        violated_typed_requirement=requirement,
        pipeline_component="adaptive_sql_solver",
        state=solver_state,
    )


def build_text_to_sql_stagnation_evidence(
    *,
    research_outcome: object,
    terminal_status: object,
    terminal_reason_code: object,
) -> dict[str, Any] | None:
    """Publish only the verified rejection pairs that ended research."""

    if (
        terminal_status != "abstained"
        or terminal_reason_code != "RESEARCH_STAGNATED"
        or type(research_outcome) is not ResearchLoopOutcome
        or research_outcome.stop_reason is not ResearchStopReason.STAGNATED
        or type(research_outcome.final_state) is not ResearchState
        or not research_outcome.rejection_signatures
        or research_outcome.rejection_signatures
        != tuple(sorted(set(research_outcome.rejection_signatures)))
    ):
        return None
    try:
        state_sha256 = canonical_digest(research_outcome.final_state)
    except (TypeError, ValueError):
        return None
    return {
        "schema_version": 1,
        "record_kind": "text2sql_research_stagnation_evidence",
        "terminal_source": "research",
        "terminal_reason_code": "RESEARCH_STAGNATED",
        "rejection_signatures": [
            list(item) for item in research_outcome.rejection_signatures
        ],
        "state_sha256": state_sha256,
    }


__all__ = [
    "build_text_to_sql_early_stop_evidence",
    "build_text_to_sql_stagnation_evidence",
]
