"""One bounded model review of a successful Typed SQL result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

from dataclasses import asdict

from pydantic import model_validator

from ._semantic_coverage_boundary import evidence_has_state_authority
from ._sql_ast_identity import semantic_candidate_digest, source_sql_digest
from ._sql_ast_models import ParsedSqlCandidate, QueryRole
from .models import (
    CheckFailureCode,
    Digest,
    EvidenceRecord,
    Id,
    NonEmptyText,
    NonNegativeInt,
    ResearchState,
    SqlCandidate,
    StrictModel,
)
from .result_validation import (
    _requirements_match_persisted_freshness,
    _validated_executed_result,
)
from .freshness import FreshnessContext
from .semantic_coverage import CoverageRequirements
from .semantic_plan import build_semantic_ast
from .serialization import canonical_json_bytes


RESULT_REVIEW_RUNTIME_KEY = "text_to_sql_result_review"
RESULT_REVIEW_REQUIRED_RUNTIME_KEY = "_text_to_sql_result_review_required"
_RESULT_REVIEW_CAPABILITY_MARKER = object()
class ResultReviewReceipt(StrictModel):
    """Durable outcome from the one result-review turn."""

    record_kind: Literal["text2sql_result_review"] = "text2sql_result_review"
    run_id: Id
    run_incarnation: Id
    research_state_revision: NonNegativeInt
    candidate_id: Id
    normalized_ast_digest: Digest
    requirements_digest: Digest
    source_id: Id | None
    evidence_id: Id | None
    verdict: Literal["consistent", "contradicted", "ambiguous", "malformed", "timeout"]
    reason: NonEmptyText
    execution: dict[str, object]
    deterministic_failure_code: Literal[
        CheckFailureCode.RESULT_SHAPE_MISMATCH
    ] | None
    repair_kind: Literal["semantic_binding_mismatch"] | None = None

    @model_validator(mode="after")
    def validate_reentry_target(self) -> "ResultReviewReceipt":
        target = (self.source_id, self.evidence_id)
        if self.deterministic_failure_code is not None and (
            self.verdict != "contradicted" or target[0] is None or target[1] is None
        ):
            raise ValueError("deterministic result review repair requires contradiction target")
        if self.repair_kind is not None and (
            self.verdict not in {"contradicted", "ambiguous"}
            or target[0] is None
            or target[1] is None
            or self.deterministic_failure_code is not None
        ):
            raise ValueError("semantic binding repair requires one model review target")
        if self.verdict == "consistent":
            if target != (None, None):
                raise ValueError("consistent review must not have a repair target")
        elif self.verdict in {"contradicted", "ambiguous"}:
            if self.source_id is None or self.evidence_id is None:
                raise ValueError("review reentry verdict requires a repair target")
        elif target != (None, None):
            raise ValueError("malformed review must not have a repair target")
        return self


class _ModelReviewResponse(StrictModel):
    status: Literal["consistent", "contradicted", "ambiguous"]
    reason: NonEmptyText
    source_id: Id | None = None
    repair_kind: Literal["semantic_binding_mismatch"] | None = None


@dataclass(frozen=True, slots=True)
class _ResultReviewCapability:
    _marker: object = field(repr=False, compare=False)
    state: ResearchState
    requirements: CoverageRequirements
    freshness_context: FreshnessContext
    candidate: SqlCandidate
    parsed_ast: ParsedSqlCandidate
    documents: tuple[object, ...]
    model: Callable[[str], str | bytes]


def create_result_review_capability(
    *,
    state: ResearchState,
    requirements: CoverageRequirements,
    freshness_context: FreshnessContext,
    candidate: SqlCandidate,
    parsed_ast: ParsedSqlCandidate,
    documents: tuple[object, ...],
    model: Callable[[str], str | bytes],
) -> object:
    """Bind exactly one trusted candidate and one deterministic repair target."""

    state, requirements, freshness_context, candidate, parsed_ast = _validated_inputs(
        state, requirements, freshness_context, candidate, parsed_ast
    )
    if type(documents) is not tuple or not callable(model):
        raise TypeError("result review inputs are invalid")
    return _ResultReviewCapability(
        _marker=_RESULT_REVIEW_CAPABILITY_MARKER,
        state=state,
        requirements=requirements,
        freshness_context=freshness_context,
        candidate=candidate,
        parsed_ast=parsed_ast,
        documents=documents,
        model=model,
    )


def evaluate_result_review_capability(
    value: object,
    *,
    expected_run_id: str,
    expected_sql: str,
    execution: object,
) -> ResultReviewReceipt:
    """Return the one bound review outcome for the executed result."""

    if (
        type(value) is not _ResultReviewCapability
        or value._marker is not _RESULT_REVIEW_CAPABILITY_MARKER
        or type(expected_run_id) is not str
        or not expected_run_id
        or type(expected_sql) is not str
        or not expected_sql.strip()
    ):
        raise TypeError("result review capability inputs are invalid")
    state, requirements, _, candidate, parsed_ast = _validated_inputs(
        value.state,
        value.requirements,
        value.freshness_context,
        value.candidate,
        value.parsed_ast,
    )
    if state.run_id != expected_run_id or candidate.sql != expected_sql:
        raise ValueError("result review capability identity does not match finalizer")
    canonical_execution = _validated_executed_result(execution)
    if source_id := _unrequested_root_projection_source_id(
        state, requirements, candidate, parsed_ast
    ):
        source_id, evidence_id = _response_target(
            state,
            requirements,
            _ModelReviewResponse(
                status="contradicted",
                reason="candidate projects a confirmed value not requested in the output",
                source_id=source_id,
            ),
        )
        return _receipt(
            state,
            requirements,
            candidate,
            "contradicted",
            "candidate projects a confirmed value not requested in the output",
            canonical_execution,
            source_id,
            evidence_id,
            deterministic_failure_code=CheckFailureCode.RESULT_SHAPE_MISMATCH,
        )
    try:
        response = _parse_response(value.model(_prompt(value, canonical_execution)))
    except Exception as exc:
        return _receipt(
            state, requirements, candidate, "timeout"
            if exc.__class__.__name__ == "WorkflowDeadlineExceeded"
            else "malformed", "result review did not return a valid verdict", canonical_execution,
        )
    source_id, evidence_id = _response_target(state, requirements, response)
    return _receipt(
        state,
        requirements,
        candidate,
        response.status,
        response.reason,
        canonical_execution,
        source_id,
        evidence_id,
        repair_kind=response.repair_kind,
    )


def _unrequested_root_projection_source_id(
    state: ResearchState,
    requirements: CoverageRequirements,
    candidate: SqlCandidate,
    parsed_ast: ParsedSqlCandidate,
) -> str | None:
    namespaces = {table.namespace for table in requirements.allowed_tables}
    if len(namespaces) != 1:
        raise ValueError("candidate table namespace is invalid")
    semantic_ast = build_semantic_ast(
        candidate,
        parsed_ast,
        state.query_spec,
        requirements,
        next(iter(namespaces)),
    )
    root_scope_ids = {
        scope.scope_id
        for scope in parsed_ast.scopes
        if scope.parent_scope_id is None and scope.query_role is QueryRole.ROOT
    }
    requested = set(state.query_spec.requested_output_source_ids)
    annotations_by_node: dict[str, set[str]] = {}
    for annotation in semantic_ast.coverage.annotations:
        annotations_by_node.setdefault(annotation.node_id, set()).update(
            annotation.source_ids
        )
    root_projection_annotations = {
        projection.node_id: annotations_by_node.get(projection.node_id, set())
        for projection in parsed_ast.projections
        if projection.scope_id in root_scope_ids
    }
    if not requested.issubset(set().union(*root_projection_annotations.values())):
        return None
    for source_ids in root_projection_annotations.values():
        if source_ids and source_ids.isdisjoint(requested):
            return min(source_ids)
    return None


def _validated_inputs(state, requirements, freshness_context, candidate, parsed_ast):
    if (
        type(state) is not ResearchState
        or type(requirements) is not CoverageRequirements
        or type(freshness_context) is not FreshnessContext
        or type(candidate) is not SqlCandidate
        or type(parsed_ast) is not ParsedSqlCandidate
    ):
        raise TypeError("result review inputs require exact contract types")
    if not _requirements_match_persisted_freshness(requirements, freshness_context):
        raise ValueError("result review requirements do not match research authority")
    if (
        candidate.revision != requirements.state_revision
        or parsed_ast.candidate_id != candidate.candidate_id
        or parsed_ast.source_sql_digest != source_sql_digest(candidate.sql)
        or parsed_ast.candidate_digest != semantic_candidate_digest(parsed_ast)
        or candidate.normalized_ast_digest != parsed_ast.candidate_digest
    ):
        raise ValueError("result review candidate and AST identities contradict")
    return state, requirements, freshness_context, candidate, parsed_ast


def _response_target(
    state: ResearchState,
    requirements: CoverageRequirements,
    response: _ModelReviewResponse,
) -> tuple[str, str]:
    if response.status == "consistent":
        if response.source_id is not None:
            raise ValueError("consistent review must not name a repair source")
        return "review", "review"
    if response.source_id is None:
        raise ValueError("non-consistent review must name one trusted source")
    bindings = tuple(
        item for item in requirements.selected_bindings if item.source_id == response.source_id
    )
    if not bindings:
        raise ValueError("review source is not an allowed binding")
    for binding in bindings:
        for evidence_id in binding.evidence_ids:
            evidence = next(
                (item for item in state.evidence if item.evidence_id == evidence_id), None
            )
            if evidence is not None and evidence_has_state_authority(evidence, state):
                return response.source_id, evidence_id
    raise ValueError("review source has no trusted evidence")


def _prompt(value: _ResultReviewCapability, execution: dict[str, object]) -> str:
    selected_ids = {
        evidence_id
        for binding in value.requirements.selected_bindings
        for evidence_id in binding.evidence_ids
    }
    evidence = [
        item.model_dump(mode="json")
        for item in value.state.evidence
        if item.evidence_id in selected_ids
    ]
    documents = [
        item.model_dump(mode="json")
        if hasattr(item, "model_dump")
        else str(item)
        for item in value.documents
    ]
    return canonical_json_bytes(
        {
            "instruction": (
                "Use only this trusted context. Inspect the question, SQL, AST, "
                "bindings, evidence, documents, columns and data. Never generate, "
                "rewrite or execute SQL. Return only JSON with status, short reason, "
                "source_id and repair_kind. source_id must be null for consistent and one supplied "
                "binding source_id for contradicted or ambiguous. Check the exact answer "
                "form and projection requested by the question and documents. An "
                "expression.expression in a derived binding is a model hypothesis copied "
                "from expression_claim, not evidence that the SQL performs that computation; "
                "never use it to resolve a conflict with the question, "
                "required semantic roles, documents, AST or returned rows. When the "
                "candidate projects an auxiliary computation solely because it is needed "
                "for ordering or grouping, do not treat it as requested unless the question "
                "or documents explicitly request it; mark that extra projection contradicted. "
                "An empty result does not by itself contradict the question or a required "
                "FILTER/TIME binding. If the SQL uses the confirmed physical column, operator, "
                "literals and representation, zero matching rows may be consistent; return "
                "contradicted only when trusted context proves one of those is wrong. "
                "When the "
                "question requires an entity-level computation over a period, a candidate "
                "that selects an extremal raw observation without computing that requested "
                "grain cannot be consistent: return contradicted when trusted evidence "
                "resolves the mismatch, otherwise return ambiguous targeting the relevant "
                "supplied binding. This does not require a particular SQL aggregation or "
                "grouping syntax; an explicitly requested single record/entity-time "
                "extremum may be consistent. For other requested entity or time semantics, "
                "compare them with the table/data grain and aggregation and grouping in the AST. "
                "Check every required binding in its requested semantic role, not merely whether "
                "its columns appear somewhere in the SQL. A required grouping dimension must "
                "participate in the computation at that grouping grain; its column appearing only "
                "in a filter or another role does not satisfy it. "
                "A related or correlated attribute is not the requested attribute unless trusted "
                "schema or documents explicitly establish their exact semantic equivalence. A "
                "selected binding and its status do not prove that semantic "
                "equivalence by themselves; when cited evidence describes only the proxy, return "
                "ambiguous targeting that supplied binding and set repair_kind to "
                "semantic_binding_mismatch. Otherwise repair_kind must be null. "
                "Do not infer that a metric is already aggregated at that required grain from "
                "table or column names or from one period value. Unless trusted evidence explicitly "
                "proves that grain, treat an AST that does not compute it as ambiguous. "
                "The order and scope of nested operations may change their meaning. Compare "
                "aggregates, extrema, filters and groupings with the exact computation requested "
                "by the question and documents instead of treating the presence of the same "
                "operations as proof of equivalence. Return contradicted only when trusted context "
                "proves the mismatch, otherwise ambiguous. When the "
                "question compares a finite set of explicitly described alternatives and "
                "asks which alternative wins an extreme metric, the result must return "
                "the winning alternative label or role, not an inner entity, unless the "
                "question or documents explicitly request that identity or attribute. "
                "Mark a wrong answer form or projection contradicted."
            ),
            "question": value.state.query_spec.original_text,
            "sql": value.candidate.sql,
            "ast": asdict(value.parsed_ast),
            "bindings": [
                item.model_dump(mode="json") for item in value.requirements.selected_bindings
            ],
            "evidence": evidence,
            "documents": documents,
            "columns": execution["columns"],
            "data": execution["data"],
        }
    ).decode("utf-8")


def _parse_response(value: object) -> _ModelReviewResponse:
    if type(value) is bytes:
        value = value.decode("utf-8", errors="strict")
    if type(value) is not str:
        raise TypeError("result review response is not text")
    return _ModelReviewResponse.model_validate_json(value)


def _receipt(
    state,
    requirements,
    candidate,
    verdict,
    reason,
    execution,
    source_id=None,
    evidence_id=None,
    deterministic_failure_code=None,
    repair_kind=None,
) -> ResultReviewReceipt:
    if verdict in {"consistent", "malformed", "timeout"}:
        return ResultReviewReceipt(
            run_id=state.run_id, run_incarnation=state.run_incarnation,
            research_state_revision=state.revision, candidate_id=candidate.candidate_id,
            normalized_ast_digest=candidate.normalized_ast_digest,
            requirements_digest=requirements.requirements_digest,
            source_id=None, evidence_id=None, verdict=verdict,
            reason=reason, execution=execution,
            deterministic_failure_code=deterministic_failure_code,
            repair_kind=repair_kind,
        )
    if source_id is None or evidence_id is None:
        raise ValueError("review reentry verdict requires a trusted repair target")
    return ResultReviewReceipt(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        research_state_revision=state.revision,
        candidate_id=candidate.candidate_id,
        normalized_ast_digest=candidate.normalized_ast_digest,
        requirements_digest=requirements.requirements_digest,
        source_id=source_id,
        evidence_id=evidence_id,
        verdict=verdict,
        reason=reason,
        execution=execution,
        deterministic_failure_code=deterministic_failure_code,
        repair_kind=repair_kind,
    )


__all__ = [
    "RESULT_REVIEW_RUNTIME_KEY",
    "RESULT_REVIEW_REQUIRED_RUNTIME_KEY",
    "ResultReviewReceipt",
    "create_result_review_capability",
    "evaluate_result_review_capability",
]
