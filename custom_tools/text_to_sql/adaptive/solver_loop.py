"""Pure host-owned transition for one transient solver proposal."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from ._semantic_coverage_boundary import evidence_has_state_authority
from .models import (
    Binding,
    BindingStatus,
    DocumentRuleBinding,
    EvidenceSourceKind,
    HypothesisStatus,
    Id,
    MissingEvidenceRequest,
    QueryProbeRef,
    ResearchReentryRecord,
    ResearchReentryStatus,
    ResearchState,
    SemanticItemKind,
    SolverAction,
    SolverActionKind,
    SolverState,
    SolverStopReason,
    StrictModel,
    TargetRef,
    is_binding_free_semantic_item,
)
from .freshness import (
    FreshnessContext,
    FreshnessStatus,
    evaluate_evidence_freshness,
)
from .semantic_coverage import CoverageRequirements, validate_coverage_inputs
from .serialization import canonical_json_bytes
from .solver_protocol import (
    MissingEvidenceProposal,
    SolverProposalV1,
    SqlCandidateProposal,
)

if TYPE_CHECKING:
    from .replay_inputs import (
        SolverMissingEvidenceReplayInput,
        SolverSqlProposalReplayInput,
    )


class SolverReducerError(ValueError):
    """Base error for rejected host-side solver transitions."""


class SolverValidationError(SolverReducerError):
    """The current durable state is not an exact validated contract."""


class SolverRevisionError(SolverReducerError):
    """The proposal was based on a stale SolverState revision."""


class SolverConflictError(SolverReducerError):
    """The proposal conflicts with an already durable solver fact."""


class SolverCandidateLimitError(SolverReducerError):
    """The solver candidate cap was reached before SQL mapping."""


class SolverReferenceError(SolverReducerError):
    """A proposal refers to a fact outside the current SolverState."""


class SolverProtocolError(SolverReducerError):
    """An untrusted proposal or its trusted coverage authority is invalid."""


@dataclass(frozen=True, slots=True)
class SolverTransitionResult:
    state: SolverState
    action: SolverAction
    replay_input: (
        SolverSqlProposalReplayInput | SolverMissingEvidenceReplayInput | None
    ) = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class ResearchReentryTransitionResult:
    state: SolverState
    record: ResearchReentryRecord


_ModelT = TypeVar("_ModelT", bound=StrictModel)
_ID_ADAPTER = TypeAdapter(Id)


def map_sql_candidate(
    sql: str,
    dsn: str,
    candidate_id: str,
    context: object = None,
):
    """Keep the parser seam while SQL remains represented by its one AST."""

    from .sql_ast import parse_sql_candidate

    return parse_sql_candidate(sql, dsn, candidate_id)


def apply_solver_proposal(
    state: SolverState,
    proposal: SolverProposalV1,
    *,
    base_revision: int,
    dsn: str,
    table_namespace: str,
    requirements: CoverageRequirements,
    id_factory: Callable[[], str],
    trusted_semantic_repair: bool = False,
    trusted_predicate_authority: bool = False,
) -> SolverTransitionResult:
    """Accept exactly one solver proposal without running checks or SQL."""

    state = _revalidate_exact(state, SolverState, SolverValidationError, "state")
    proposal = _revalidate_exact(
        proposal, SolverProposalV1, SolverProtocolError, "proposal"
    )
    requirements = _revalidate_exact(
        requirements, CoverageRequirements, SolverProtocolError, "requirements"
    )
    _validate_transition_inputs(
        state,
        base_revision,
        dsn,
        table_namespace,
        requirements,
        id_factory,
    )

    if state.stop_reason is not None:
        raise SolverConflictError("SolverState is already stopped")
    if (
        type(proposal.proposal) is MissingEvidenceProposal
        and proposal.proposal.repair_kind is not None
        and not trusted_semantic_repair
    ):
        raise SolverProtocolError(
            "semantic binding repair requires a trusted result review"
        )
    if (
        type(proposal.proposal) is MissingEvidenceProposal
        and proposal.proposal.predicate_authority is not None
        and not trusted_predicate_authority
    ):
        raise SolverProtocolError(
            "predicate authority requires a trusted result review"
        )
    if len(state.sql_candidates) >= 8 and isinstance(
        proposal.proposal, SqlCandidateProposal
    ):
        raise SolverCandidateLimitError("SolverState supports at most 8 SQL candidates")

    if type(proposal.proposal) is SqlCandidateProposal:
        from .replay_inputs import (
            ParsedSqlCandidateReplayValue,
            SolverSqlProposalReplayInput,
        )

        generated_ids = _new_ids(state, id_factory, 2)
        parsed_candidate = map_sql_candidate(
            proposal.proposal.sql,
            dsn,
            generated_ids[0],
        )
        transition = apply_solver_proposal_from_parsed(
            state,
            proposal,
            base_revision=base_revision,
            parsed_candidate=parsed_candidate,
            requirements=requirements,
            generated_ids=generated_ids,
        )
        return SolverTransitionResult(
            transition.state,
            transition.action,
            SolverSqlProposalReplayInput(
                proposal=proposal,
                parsed_candidate=ParsedSqlCandidateReplayValue.from_candidate(
                    parsed_candidate
                ),
                requirements=requirements,
                generated_ids=generated_ids,
            ),
        )
    if type(proposal.proposal) is MissingEvidenceProposal:
        from .replay_inputs import SolverMissingEvidenceReplayInput

        generated_ids = _new_ids(state, id_factory, 2)
        transition = apply_solver_proposal_from_parsed(
            state,
            proposal,
            base_revision=base_revision,
            parsed_candidate=None,
            requirements=requirements,
            generated_ids=generated_ids,
        )
        return SolverTransitionResult(
            transition.state,
            transition.action,
            SolverMissingEvidenceReplayInput(
                proposal=proposal,
                requirements=requirements,
                generated_ids=generated_ids,
            ),
        )
    raise SolverProtocolError("proposal kind is not supported")


def apply_solver_proposal_from_parsed(
    state: SolverState,
    proposal: SolverProposalV1,
    *,
    base_revision: int,
    parsed_candidate: object | None,
    requirements: CoverageRequirements,
    generated_ids: tuple[str, ...],
) -> SolverTransitionResult:
    """Apply one proposal from already verified parser output without I/O."""

    from ._sql_ast_models import ParsedSqlCandidate

    state = _revalidate_exact(state, SolverState, SolverValidationError, "state")
    proposal = _revalidate_exact(
        proposal, SolverProposalV1, SolverProtocolError, "proposal"
    )
    requirements = _revalidate_exact(
        requirements, CoverageRequirements, SolverProtocolError, "requirements"
    )
    _validate_transition_authority(state, base_revision, requirements)
    if state.stop_reason is not None:
        raise SolverConflictError("SolverState is already stopped")

    if type(proposal.proposal) is SqlCandidateProposal:
        if len(state.sql_candidates) >= 8:
            raise SolverCandidateLimitError(
                "SolverState supports at most 8 SQL candidates"
            )
        ids = _validated_generated_ids(state, generated_ids, 2)
        if type(parsed_candidate) is not ParsedSqlCandidate:
            raise SolverProtocolError("SQL proposal requires a parsed candidate")
        return _apply_sql_proposal(
            state,
            proposal.proposal,
            parsed_candidate,
            requirements,
            ids,
        )
    if type(proposal.proposal) is MissingEvidenceProposal:
        if parsed_candidate is not None:
            raise SolverProtocolError(
                "missing-evidence proposal cannot carry parsed SQL"
            )
        return _apply_missing_evidence_proposal(
            state,
            proposal.proposal,
            _validated_generated_ids(state, generated_ids, 2),
        )
    raise SolverProtocolError("proposal kind is not supported")


def stop_solver(
    state: SolverState,
    reason: SolverStopReason,
    *,
    base_revision: int,
) -> SolverState:
    """Close one open solver with an existing non-proposal stop reason."""

    state = _revalidate_exact(state, SolverState, SolverValidationError, "state")
    _validate_solver_revision(state, base_revision)
    if type(reason) is not SolverStopReason or reason in {
        SolverStopReason.SOLVED,
        SolverStopReason.MISSING_EVIDENCE,
    }:
        raise SolverProtocolError("reason is not an orchestration stop reason")
    if state.stop_reason is not None:
        raise SolverConflictError("SolverState is already stopped")
    return SolverState.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": state.revision + 1,
            "stop_reason": reason,
        }
    )


def admit_targeted_reentry(
    state: SolverState,
    research_state: ResearchState,
    missing_evidence_request_id: str,
    *,
    base_revision: int,
    id_factory: Callable[[], str],
) -> ResearchReentryTransitionResult:
    """Admit one logical research attempt without creating a SolverAction."""

    state = _revalidate_exact(state, SolverState, SolverValidationError, "state")
    research_state = _revalidate_exact(
        research_state,
        ResearchState,
        SolverValidationError,
        "research_state",
    )
    _validate_solver_revision(state, base_revision)
    if type(missing_evidence_request_id) is not str:
        raise SolverReferenceError("missing_evidence_request_id must be an exact str")
    if not callable(id_factory):
        raise SolverProtocolError("id_factory must be callable")
    if state.stop_reason is not SolverStopReason.MISSING_EVIDENCE:
        raise SolverConflictError("targeted re-entry requires MISSING_EVIDENCE")
    if (
        research_state.run_id != state.run_id
        or research_state.run_incarnation != state.run_incarnation
        or research_state.schema_namespace_version != state.schema_namespace_version
    ):
        raise SolverValidationError("research_state does not match SolverState")
    request = next(
        (
            item
            for item in state.missing_evidence_requests
            if item.missing_evidence_request_id == missing_evidence_request_id
        ),
        None,
    )
    if request is None:
        raise SolverReferenceError("missing-evidence request is not in SolverState")
    if any(
        item.status is ResearchReentryStatus.ADMITTED
        for item in state.research_reentries
    ):
        raise SolverConflictError("a targeted re-entry is already ADMITTED")
    prior = tuple(
        item
        for item in state.research_reentries
        if item.missing_evidence_request_id == missing_evidence_request_id
    )
    if len(prior) >= 3:
        raise SolverCandidateLimitError("targeted re-entry supports at most 3 attempts")
    (research_reentry_id,) = _new_ids(state, id_factory, 1)
    next_revision = state.revision + 1
    record = ResearchReentryRecord(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=next_revision,
        schema_namespace_version=state.schema_namespace_version,
        research_reentry_id=research_reentry_id,
        missing_evidence_request_id=missing_evidence_request_id,
        source_id=request.source_id,
        ordinal=len(prior) + 1,
        research_base_revision=research_state.revision,
        baseline_evidence_ids=tuple(
            sorted(item.evidence_id for item in research_state.evidence)
        ),
        status=ResearchReentryStatus.ADMITTED,
        research_result_revision=None,
        evidence_ids=(),
    )
    next_state = SolverState.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": next_revision,
            "research_reentries": (*state.research_reentries, record),
        }
    )
    return ResearchReentryTransitionResult(next_state, record)


def finalize_targeted_reentry(
    state: SolverState,
    research_reentry_id: str,
    status: ResearchReentryStatus,
    *,
    base_revision: int,
    research_state: ResearchState | None = None,
    freshness_context: FreshnessContext | None = None,
    requirements: CoverageRequirements | None = None,
) -> ResearchReentryTransitionResult:
    """Terminalize one admitted attempt and resume only on fresh W4 authority."""

    state = _revalidate_exact(state, SolverState, SolverValidationError, "state")
    _validate_solver_revision(state, base_revision)
    if type(research_reentry_id) is not str:
        raise SolverReferenceError("research_reentry_id must be an exact str")
    if (
        type(status) is not ResearchReentryStatus
        or status is ResearchReentryStatus.ADMITTED
    ):
        raise SolverProtocolError("status must be a terminal ResearchReentryStatus")
    record_index = next(
        (
            index
            for index, item in enumerate(state.research_reentries)
            if item.research_reentry_id == research_reentry_id
        ),
        None,
    )
    if record_index is None:
        raise SolverReferenceError("research re-entry is not in SolverState")
    current = state.research_reentries[record_index]
    if current.status is not ResearchReentryStatus.ADMITTED:
        raise SolverConflictError("research re-entry is already terminal")
    request = next(
        item
        for item in state.missing_evidence_requests
        if item.missing_evidence_request_id == current.missing_evidence_request_id
    )
    semantic_repair = request.repair_kind == "semantic_binding_mismatch"
    request_item = next(
        item
        for item in state.query_spec.semantic_items
        if item.source_id == request.source_id
    )
    formula_continuation = (
        request.repair_kind is None
        and request_item.kind is SemanticItemKind.FORMULA
        and not request_item.binding_ids
    )
    research_continuation = (
        semantic_repair
        or formula_continuation
        or request.predicate_authority is not None
    )

    result_revision: int | None = None
    evidence_ids: tuple[str, ...] = ()
    next_stop_reason = SolverStopReason.MISSING_EVIDENCE
    next_query_spec = state.query_spec
    if status is ResearchReentryStatus.COMPLETED:
        if research_state is None or freshness_context is None or requirements is None:
            raise SolverProtocolError(
                "COMPLETED re-entry requires research state and W4 authority"
            )
        research_state = _revalidate_exact(
            research_state,
            ResearchState,
            SolverValidationError,
            "research_state",
        )
        freshness_context = _revalidate_exact(
            freshness_context,
            FreshnessContext,
            SolverProtocolError,
            "freshness_context",
        )
        requirements = _revalidate_exact(
            requirements,
            CoverageRequirements,
            SolverProtocolError,
            "requirements",
        )
        if (
            research_state.run_id != state.run_id
            or research_state.run_incarnation != state.run_incarnation
            or research_state.schema_namespace_version != state.schema_namespace_version
            or (
                research_state.revision <= current.research_base_revision
                if research_continuation
                else research_state.revision != current.research_base_revision + 1
            )
        ):
            raise SolverValidationError(
                "research result is stale or has wrong identity"
            )
        try:
            rebuilt = validate_coverage_inputs(
                research_state,
                freshness_context,
                state.run_id,
                state.run_incarnation,
            )
        except (TypeError, ValueError) as exc:
            raise SolverProtocolError(
                "research result cannot rebuild canonical W4 authority"
            ) from exc
        if not _exact_value(requirements, rebuilt):
            raise SolverProtocolError(
                "requirements are not canonical rebuilt W4 authority"
            )
        selected = tuple(
            item
            for item in rebuilt.selected_bindings
            if item.source_id == current.source_id
        )
        if not selected:
            raise SolverProtocolError("W4 authority does not select the request source")
        if semantic_repair:
            stale = tuple(
                binding
                for binding in research_state.bindings
                if binding.binding_id == request.repair_binding_id
                and binding.source_id == current.source_id
                and binding.status is BindingStatus.STALE
            )
            if len(stale) != 1 or any(
                binding.binding_id == request.repair_binding_id
                for binding in selected
            ):
                raise SolverProtocolError(
                    "semantic repair did not replace the stale binding"
                )
        baseline = set(current.baseline_evidence_ids)
        new_evidence = tuple(
            item for item in research_state.evidence if item.evidence_id not in baseline
        )
        evidence_pool = (
            research_state.evidence if research_continuation else new_evidence
        )
        source_targets = _source_reentry_targets(
            research_state,
            selected,
            current.source_id,
        )
        selected_evidence_ids = {
            evidence_id for binding in selected for evidence_id in binding.evidence_ids
        }
        eligible_new_evidence = tuple(
            item
            for item in evidence_pool
            if (
                not research_continuation
                or item.evidence_id in selected_evidence_ids
            )
            and (
                research_continuation
                or item.source_kind is request.required_evidence_kind
            )
            and evidence_has_state_authority(item, research_state)
            and item.observed_at <= freshness_context.evaluated_at
            and item.created_at <= freshness_context.evaluated_at
            and evaluate_evidence_freshness(item, freshness_context).status
            is FreshnessStatus.FRESH
            and (
                research_continuation
                or (
                    (
                        request.required_evidence_kind is EvidenceSourceKind.PROBE
                        and type(item.target) is QueryProbeRef
                    )
                    or any(
                        _exact_value(item.target, target) for target in source_targets
                    )
                )
            )
        )
        if not eligible_new_evidence or (
            not research_continuation
            and len(eligible_new_evidence) != len(new_evidence)
        ):
            raise SolverValidationError(
                "research result has no eligible fresh evidence for the request source"
            )
        evidence_ids = tuple(sorted(item.evidence_id for item in eligible_new_evidence))
        result_revision = research_state.revision
        next_stop_reason = None
        next_query_spec = research_state.query_spec
    elif any(
        value is not None for value in (research_state, freshness_context, requirements)
    ):
        raise SolverProtocolError("failed re-entry cannot carry completion authority")

    finalized = ResearchReentryRecord.model_validate(
        {
            **current.model_dump(mode="python"),
            "status": status,
            "research_result_revision": result_revision,
            "evidence_ids": evidence_ids,
        }
    )
    records = list(state.research_reentries)
    records[record_index] = finalized
    next_state = SolverState.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": state.revision + 1,
            "query_spec": next_query_spec,
            "research_reentries": tuple(records),
            "stop_reason": next_stop_reason,
        }
    )
    return ResearchReentryTransitionResult(next_state, finalized)


def _source_reentry_targets(
    research_state: ResearchState,
    bindings: tuple[Binding, ...],
    source_id: str,
) -> tuple[TargetRef, ...]:
    targets = [
        target
        for binding in bindings
        for target in (*binding.tables, *binding.columns)
    ]
    targets.extend(
        binding.document
        for binding in bindings
        if type(binding) is DocumentRuleBinding
    )
    for hypothesis in research_state.hypotheses:
        if source_id in hypothesis.source_ids and hypothesis.status in {
            HypothesisStatus.PROPOSED,
            HypothesisStatus.TESTING,
            HypothesisStatus.SUPPORTED,
        }:
            targets.extend(hypothesis.candidate_targets)
    unique = {canonical_json_bytes(target): target for target in targets}
    return tuple(unique[key] for key in sorted(unique))


def _apply_sql_proposal(
    state: SolverState,
    proposal: SqlCandidateProposal,
    parsed_candidate: object,
    requirements: CoverageRequirements,
    generated_ids: tuple[str, str],
) -> SolverTransitionResult:
    from ._sql_ast_identity import semantic_candidate_digest, source_sql_digest
    from ._sql_ast_models import ParsedSqlCandidate
    from .models import CheckStatus, SqlCandidate

    candidate_id, action_id = generated_ids
    if (
        type(parsed_candidate) is not ParsedSqlCandidate
        or parsed_candidate.candidate_id != candidate_id
        or parsed_candidate.source_sql_digest != source_sql_digest(proposal.sql)
        or parsed_candidate.candidate_digest != semantic_candidate_digest(parsed_candidate)
    ):
        raise SolverProtocolError("parsed SQL candidate does not match proposal")
    candidate = SqlCandidate(
        candidate_id=candidate_id,
        sql=proposal.sql,
        normalized_ast_digest=parsed_candidate.candidate_digest,
        revision=requirements.state_revision,
    )
    matching_candidate = next(
        (
            item
            for item in reversed(state.sql_candidates)
            if item.normalized_ast_digest == candidate.normalized_ast_digest
        ),
        None,
    )
    exact_candidate = next(
        (item for item in reversed(state.sql_candidates) if item.sql == candidate.sql),
        None,
    )
    exact_retry_after_research = (
        exact_candidate is not None
        and any(
            check.candidate_id == exact_candidate.candidate_id
            and check.status is CheckStatus.FAILED
            for check in state.check_results
        )
        and any(
            reentry.status is ResearchReentryStatus.COMPLETED
            and reentry.research_result_revision is not None
            and candidate.revision
            >= reentry.research_result_revision
            > exact_candidate.revision
            for reentry in state.research_reentries
        )
    )
    if (exact_candidate is not None and not exact_retry_after_research) or (
        matching_candidate is not None
        and not any(
            check.candidate_id == matching_candidate.candidate_id
            and check.status is CheckStatus.FAILED
            for check in state.check_results
        )
    ):
        raise SolverConflictError("SQL candidate normalized_ast_digest already exists")
    action = SolverAction(
        action_id=action_id,
        kind=SolverActionKind.SQL_CANDIDATE,
        base_revision=state.revision,
        candidate_id=candidate_id,
        missing_evidence_request_id=None,
    )
    next_state = SolverState.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": state.revision + 1,
            "sql_candidates": (*state.sql_candidates, candidate),
            "action_history": (*state.action_history, action),
        }
    )
    return SolverTransitionResult(next_state, action)


def _apply_missing_evidence_proposal(
    state: SolverState,
    proposal: MissingEvidenceProposal,
    generated_ids: tuple[str, str],
) -> SolverTransitionResult:
    if proposal.source_id not in {
        item.source_id for item in state.query_spec.semantic_items
    }:
        raise SolverReferenceError("missing evidence source_id is not in query_spec")
    request_id, action_id = generated_ids
    next_revision = state.revision + 1
    request = MissingEvidenceRequest(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=next_revision,
        schema_namespace_version=state.schema_namespace_version,
        missing_evidence_request_id=request_id,
        source_id=proposal.source_id,
        question=proposal.question,
        candidate_targets=(),
        required_evidence_kind=proposal.required_evidence_kind,
        reason=proposal.reason,
        repair_kind=proposal.repair_kind,
        repair_binding_id=proposal.repair_binding_id,
        predicate_authority=proposal.predicate_authority,
    )
    action = SolverAction(
        action_id=action_id,
        kind=SolverActionKind.MISSING_EVIDENCE,
        base_revision=state.revision,
        candidate_id=None,
        missing_evidence_request_id=request_id,
    )
    next_state = SolverState.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": next_revision,
            "missing_evidence_requests": (*state.missing_evidence_requests, request),
            "action_history": (*state.action_history, action),
            "stop_reason": SolverStopReason.MISSING_EVIDENCE,
        }
    )
    return SolverTransitionResult(next_state, action)


def _validate_transition_inputs(
    state: SolverState,
    base_revision: int,
    dsn: str,
    table_namespace: str,
    requirements: CoverageRequirements,
    id_factory: Callable[[], str],
) -> None:
    _validate_transition_authority(state, base_revision, requirements)
    if type(dsn) is not str or not dsn:
        raise SolverProtocolError("dsn must be non-empty text")
    if type(table_namespace) is not str or not table_namespace:
        raise SolverProtocolError("table_namespace must be non-empty text")
    if not callable(id_factory):
        raise SolverProtocolError("id_factory must be callable")


def _validate_transition_authority(
    state: SolverState,
    base_revision: int,
    requirements: CoverageRequirements,
) -> None:
    _validate_solver_revision(state, base_revision)
    if (
        requirements.run_id != state.run_id
        or requirements.run_incarnation != state.run_incarnation
        or requirements.schema_namespace_version != state.schema_namespace_version
        or requirements.state_revision < state.query_spec.revision
        or requirements.expected_result_shape != state.query_spec.expected_result_shape
        or requirements.required_source_ids
        != tuple(
            sorted(
                item.source_id
                for item in state.query_spec.semantic_items
                if item.required
            )
        )
    ):
        raise SolverProtocolError("requirements do not match SolverState authority")
    if tuple(sorted({item.source_id for item in requirements.selected_bindings})) != tuple(
        sorted(
            item.source_id
            for item in state.query_spec.semantic_items
            if item.required and not is_binding_free_semantic_item(item)
        )
    ):
        raise SolverProtocolError("requirements selected bindings do not match authority")


def _validate_solver_revision(state: SolverState, base_revision: int) -> None:
    if type(base_revision) is not int or base_revision < 0:
        raise SolverRevisionError("base_revision must be a non-negative integer")
    if base_revision != state.revision:
        raise SolverRevisionError("base_revision does not match SolverState revision")


def _new_ids(
    state: SolverState,
    id_factory: Callable[[], str],
    count: int,
) -> tuple[str, ...]:
    values: list[str] = []
    for _ in range(count):
        try:
            raw = id_factory()
            if type(raw) is not str:
                raise TypeError("generated Id must be an exact str")
            value = _ID_ADAPTER.validate_python(raw, strict=True)
        except (ValidationError, TypeError, ValueError) as exc:
            raise SolverProtocolError("id_factory returned an invalid Id") from exc
        values.append(value)
    return _validated_generated_ids(state, tuple(values), count)


def _validated_generated_ids(
    state: SolverState,
    values: tuple[str, ...],
    count: int,
) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) != count:
        raise SolverProtocolError("generated IDs have invalid cardinality")
    checked: list[str] = []
    for raw in values:
        try:
            if type(raw) is not str:
                raise TypeError("generated Id must be an exact str")
            checked.append(_ID_ADAPTER.validate_python(raw, strict=True))
        except (ValidationError, TypeError, ValueError) as exc:
            raise SolverProtocolError("generated IDs contain an invalid Id") from exc
    existing = {
        *(item.candidate_id for item in state.sql_candidates),
        *(item.action_id for item in state.action_history),
        *(item.missing_evidence_request_id for item in state.missing_evidence_requests),
        *(item.research_reentry_id for item in state.research_reentries),
    }
    result = tuple(checked)
    if len(result) != len(set(result)) or existing.intersection(result):
        raise SolverConflictError("host-generated ID collides with SolverState")
    return result


def _revalidate_exact(
    value: object,
    model_type: type[_ModelT],
    error_type: type[SolverReducerError],
    label: str,
) -> _ModelT:
    if type(value) is not model_type:
        raise error_type(f"{label} must be an exact {model_type.__name__}")
    try:
        checked = model_type.model_validate_json(
            canonical_json_bytes(_declared_payload(value)),
            strict=True,
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise error_type(f"{label} does not round-trip as a valid contract") from exc
    if not _exact_value(value, checked):
        raise error_type(f"{label} is not exactly canonical")
    return checked


def _declared_payload(value: object) -> object:
    if isinstance(value, BaseModel):
        field_names = tuple(type(value).model_fields)
        model_dict = value.__dict__
        if (
            type(model_dict) is not dict
            or set(dict.keys(model_dict)) != set(field_names)
            or value.__pydantic_extra__ is not None
            or value.__pydantic_private__ is not None
        ):
            raise ValueError("model internals must contain exact declared fields")
        return {name: _declared_payload(getattr(value, name)) for name in field_names}
    if isinstance(value, tuple):
        return tuple(_declared_payload(item) for item in value)
    if isinstance(value, list):
        return [_declared_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _declared_payload(item) for key, item in value.items()}
    return value


def _exact_value(value: object, canonical: object) -> bool:
    if type(value) is not type(canonical):
        return False
    if isinstance(canonical, BaseModel):
        if (
            value.__pydantic_extra__ is not None
            or canonical.__pydantic_extra__ is not None
            or value.__pydantic_private__ is not None
            or canonical.__pydantic_private__ is not None
        ):
            return False
        return all(
            _exact_value(getattr(value, name), getattr(canonical, name))
            for name in type(canonical).model_fields
        )
    if isinstance(canonical, tuple):
        return len(value) == len(canonical) and all(
            _exact_value(item, expected)
            for item, expected in zip(value, canonical, strict=True)
        )
    if isinstance(canonical, dict):
        return value.keys() == canonical.keys() and all(
            _exact_value(value[key], expected) for key, expected in canonical.items()
        )
    return value == canonical
