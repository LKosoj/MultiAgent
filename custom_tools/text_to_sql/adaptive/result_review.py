"""One bounded model review of a successful Typed SQL result."""

from __future__ import annotations

import json
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
    EvidenceSourceKind,
    Id,
    NonEmptyText,
    NonNegativeInt,
    PhysicalColumnBinding,
    PredicateRef,
    ResearchState,
    SemanticItemKind,
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
    repair_binding_id: Id | None = None
    predicate_authority: PredicateRef | None = None

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
        if self.repair_binding_id is not None and self.repair_kind is None:
            raise ValueError("review binding requires semantic repair")
        if self.predicate_authority is not None and (
            self.verdict != "contradicted"
            or target[0] is None
            or target[1] is None
            or self.repair_kind is not None
            or self.deterministic_failure_code is not None
        ):
            raise ValueError("predicate authority requires one contradiction target")
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
    repair_binding_id: Id | None = None
    predicate_authority: PredicateRef | None = None


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
    schema: dict[str, object] = field(default_factory=dict)


def create_result_review_capability(
    *,
    state: ResearchState,
    requirements: CoverageRequirements,
    freshness_context: FreshnessContext,
    candidate: SqlCandidate,
    parsed_ast: ParsedSqlCandidate,
    documents: tuple[object, ...],
    model: Callable[[str], str | bytes],
    schema: dict[str, object] | None = None,
) -> object:
    """Bind exactly one trusted candidate and one deterministic repair target."""

    state, requirements, freshness_context, candidate, parsed_ast = _validated_inputs(
        state, requirements, freshness_context, candidate, parsed_ast
    )
    if (
        type(documents) is not tuple
        or not callable(model)
        or (schema is not None and type(schema) is not dict)
    ):
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
        schema={} if schema is None else schema,
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
    if source_id := _root_projection_shape_mismatch_source_id(
        state, requirements, candidate, parsed_ast
    ):
        reason = (
            "candidate does not project each requested confirmed value separately"
            if source_id in state.query_spec.requested_output_source_ids
            else "candidate projects a confirmed value not requested in the output"
        )
        source_id, evidence_id, repair_binding_id = _response_target(
            state,
            requirements,
            _ModelReviewResponse(
                status="contradicted",
                reason=reason,
                source_id=source_id,
            ),
        )
        return _receipt(
            state,
            requirements,
            candidate,
            "contradicted",
            reason,
            canonical_execution,
            source_id,
            evidence_id,
            deterministic_failure_code=CheckFailureCode.RESULT_SHAPE_MISMATCH,
            repair_binding_id=repair_binding_id,
        )
    try:
        response = _parse_response(value.model(_prompt(value, canonical_execution)))
    except Exception as exc:
        return _receipt(
            state, requirements, candidate, "timeout"
            if exc.__class__.__name__ == "WorkflowDeadlineExceeded"
            else "malformed", "result review did not return a valid verdict", canonical_execution,
        )
    if response.repair_kind is None and response.repair_binding_id is not None:
        response = response.model_copy(update={"repair_binding_id": None})
    source_id, evidence_id, repair_binding_id = _response_target(
        state, requirements, response
    )
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
        repair_binding_id=repair_binding_id,
        predicate_authority=response.predicate_authority,
    )


def _root_projection_shape_mismatch_source_id(
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
    requested_source_ids = state.query_spec.requested_output_source_ids
    requested = set(requested_source_ids)
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
    semantic_items_by_source_id = {
        item.source_id: item for item in state.query_spec.semantic_items
    }
    requested_bindings_by_source_id = {
        source_id: tuple(
            binding
            for binding in requirements.selected_bindings
            if binding.source_id == source_id
        )
        for source_id in requested_source_ids
    }
    if (
        len(requested_source_ids) > 1
        and all(
            semantic_items_by_source_id[source_id].kind is SemanticItemKind.DIMENSION
            for source_id in requested_source_ids
        )
        and all(
            len(bindings) == 1 and isinstance(bindings[0], PhysicalColumnBinding)
            for bindings in requested_bindings_by_source_id.values()
        )
        and len(
            {
                bindings[0].physical_column
                for bindings in requested_bindings_by_source_id.values()
            }
        )
        == len(requested_source_ids)
        and any(
            len(source_ids & requested) > 1
            for source_ids in root_projection_annotations.values()
        )
    ):
        return requested_source_ids[0]

    def contains_expression(expression, target) -> bool:
        return expression == target or any(
            contains_expression(child, target) for _, _, child in expression.children
        )

    if (
        requested_source_ids
        and all(
            semantic_items_by_source_id[source_id].kind is SemanticItemKind.DIMENSION
            for source_id in requested_source_ids
        )
        and not any(
            item.required
            and item.kind in {SemanticItemKind.METRIC, SemanticItemKind.FORMULA}
            for item in state.query_spec.semantic_items
        )
        and any(
            not root_projection_annotations[projection.node_id]
            for projection in parsed_ast.projections
            if projection.scope_id in root_scope_ids
            if any(
                aggregate.scope_id in root_scope_ids
                and contains_expression(projection.expression, aggregate.expression)
                for aggregate in parsed_ast.aggregates
            )
        )
    ):
        return requested_source_ids[0]
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
) -> tuple[str, str, str | None]:
    if response.status == "consistent":
        if response.source_id is not None:
            raise ValueError("consistent review must not name a repair source")
        return "review", "review", None
    if response.source_id is None:
        raise ValueError("non-consistent review must name one trusted source")
    bindings = tuple(
        item for item in requirements.selected_bindings if item.source_id == response.source_id
    )
    if not bindings:
        raise ValueError("review source is not an allowed binding")
    if response.repair_kind is None:
        if response.repair_binding_id is not None:
            raise ValueError("review binding requires semantic repair")
    else:
        if len(bindings) != 1:
            raise ValueError("semantic repair must name one selected binding")
        response = response.model_copy(
            update={"repair_binding_id": bindings[0].binding_id}
        )
    for binding in bindings:
        for evidence_id in binding.evidence_ids:
            evidence = next(
                (item for item in state.evidence if item.evidence_id == evidence_id), None
            )
            if evidence is not None and evidence_has_state_authority(evidence, state):
                return response.source_id, evidence_id, response.repair_binding_id
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
        and item.source_kind is not EvidenceSourceKind.PROBE
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
                "Before using binding status or execution success as support, independently compare the requested attribute owner "
                "with the selected table and column descriptions, and inspect the supplied schema for an attribute whose described owner and meaning match more directly. "
                "Before returning consistent for each requested DIMENSION label, compare the selected "
                "label against label columns on relations already used by the candidate AST. When trusted "
                "descriptions show the selected label is partial or nullable and another label is full for "
                "the same qualifying rows, return contradicted targeting the supplied binding and set "
                "repair_kind to semantic_binding_mismatch. The alternative must be a semantically matching "
                "full requested label, not merely any full label in a joined relation. It can be on another "
                "joined relation but must be within the existing qualifying join scope. Exclude external "
                "current, canonical, persistent, or master labels unless the question explicitly requests "
                "them or trusted schema or documents prove equivalence at the qualifying row scope. "
                "Do not repair a NULL or partial selected output by filtering out qualifying rows when a "
                "semantically matching full or official label exists on a relation already used by the "
                "candidate AST; preserve those rows and return contradicted targeting the supplied binding "
                "with repair_kind semantic_binding_mismatch. "
                "Within the existing qualifying join scope, a label explicitly described as full or official "
                "for the row supplying a required condition or formula is the row-local output. Do not name "
                "an alternative as a replacement unless its trusted description explicitly establishes a full "
                "or official matching label for the same qualifying row; a generic entity name is not enough. "
                "When an output is requested as an attribute of a named entity, preserve the output "
                "described in the qualifying row scope. An external current, canonical, or persistent "
                "named-entity attribute replaces it only when the question explicitly requests a current, "
                "canonical, or persistent attribute or trusted schema or documents prove the attributes "
                "equivalent at that qualifying row scope. "
                "When a trusted document defines a row role through a physical representation, "
                "treat that definition as the qualifying row scope. Do not replace it with a "
                "conventional domain interpretation and do not add an aggregation solely to force "
                "those matches into one row. Do not infer an entity or period grain, "
                "aggregation, tie-break or single-row result solely from expected_result_shape. "
                "expected_result_shape is only an answer-format hint; singular grammar does not "
                "require a tie-break, LIMIT or one-row result. Preserve all matches unless "
                "the question or trusted context explicitly requires another result grain or aggregation. "
                "An entity or relationship role name alone does not authorize an exact discriminator predicate. "
                "Do not contradict its omission unless the question, a trusted document, or an already selected binding explicitly requires "
                "that exact column, operator, and value. "
                "Use only this trusted context. Inspect the question, SQL, AST, "
                "bindings, evidence, documents, columns and data. Never generate, "
                "rewrite or execute SQL. Return only JSON object with exactly these keys: status, reason, "
                "source_id, repair_kind, repair_binding_id, predicate_authority. status must be exactly one of consistent, "
                "contradicted, ambiguous. "
                "source_id must be null for consistent and one supplied "
                "binding source_id for contradicted or ambiguous. Check the exact answer "
                "form and projection requested by the question and documents. "
                "Compare the SQL with every required semantic item in QuerySpec. "
                "Only semantic items listed in requested_output_source_ids must be projected. "
                "Every other required item must still be used in its required semantic role, "
                "such as filtering, joining, grouping, aggregation or ordering, but is not "
                "required to be projected solely because it is required. "
                "When QuerySpec separately requests multiple required output METRIC items, "
                "each requires its own returned result value. If the SQL and execution return "
                "exactly one combined value, do not let it satisfy multiple such METRIC items "
                "merely because it combines their conditions. This does not reject one value "
                "per group returned as multiple rows. A QuerySpec that explicitly requests one "
                "combined metric still requires only one result value. "
                "When QuerySpec requested outputs are only DIMENSION items, an aggregate projection "
                "or GROUP BY added to count, collapse, or force values into one row contradicts the "
                "requested output unless QuerySpec or trusted context explicitly requires that aggregate "
                "projection or GROUP BY; return contradicted. A FORMULA used only as a filter or condition "
                "does not authorize root aggregation or grouping. Use root DISTINCT only when the "
                "question or QuerySpec explicitly requests unique or distinct, or trusted evidence "
                "proves the entire root projection is one-to-one at the required result grain, for "
                "example because the projected entity identity is unique; otherwise preserve all "
                "qualifying rows. "
                "A requested conditional entity output that preserves surrounding rows "
                "must be implemented in the SELECT projection with CASE or IIF; its condition "
                "must use a textual absence marker rather than SQL NULL, must not be moved to "
                "WHERE or replaced by projecting the status or predicate. "
                "A required FORMULA is not satisfied by a different physical column "
                "or precomputed value merely because it appears to have the same unit "
                "or meaning; schema descriptions do not override the required computation. "
                "When a required FORMULA specifies how to compute a metric, it takes precedence over a selected "
                "physical binding for that metric. Do not contradict SQL that follows the formula merely because "
                "it computes the metric from a different trusted input column. "
                "When the question requests a ranked top N by the lowest or highest aggregated metric, "
                "a trusted MIN or MAX description of that extremum defines the ranking direction; "
                "do not require an additional outer MIN or MAX that collapses the N result rows. "
                "Preserve the ORDER BY and LIMIT N over the computed metric. "
                "Preserve every component unit named by trusted context. A textual suffix identified as integer milliseconds "
                "must be divided by 1000, even when observed values omit leading zeroes; treating it as decimal fractional digits "
                "changes the documented unit and must be contradicted. "
                "If the SQL substitutes a physical column for an unbound required FORMULA, "
                "return contradicted and use the supplied binding whose column substituted "
                "for the formula as source_id. In that case repair_kind must be null because "
                "the computation, not the physical binding, is wrong. "
                "If the SQL omits or fails to apply a required semantic item while the selected physical binding is correct, "
                "return contradicted targeting that binding; repair_kind must be null because the SQL, not the binding, is wrong. "
                "An expression.expression in a derived binding is a model hypothesis copied "
                "from expression_claim, not evidence that the SQL performs that computation; "
                "never use it to resolve a conflict with the question, "
                "required semantic roles, documents, AST or returned rows. When the "
                "selected supported derived binding may complete a documented shorthand by adding a required reference input; "
                "the added input alone is not a contradiction; require trusted context that positively contradicts it. "
                "When the "
                "candidate projects an auxiliary computation solely because it is needed "
                "for ordering or grouping, do not treat it as requested unless the question "
                "or documents explicitly request it; mark that extra projection contradicted. "
                "A technical physical key used only for JOIN, GROUP BY, ORDER BY, window partition, "
                "or dedup may be used internally but must not be root SELECT unless QuerySpec or "
                "trusted context explicitly requests that identifier or label output; mark that extra "
                "projection contradicted. "
                "An empty result does not by itself contradict the question or a required "
                "FILTER/TIME binding. If the SQL uses the confirmed physical column, operator, "
                "literals and representation, zero matching rows may be consistent; return "
                "contradicted only when trusted context proves one of those is wrong. "
                "An auxiliary probe over a different physical column or predicate cannot "
                "contradict an exact physical predicate confirmed by a selected binding or "
                "document. Judge the candidate against the confirmed predicate itself. "
                "When a selected binding supplies the confirmed relationship used to combine "
                "a metric and a condition, row multiplication or surprising result magnitude "
                "alone does not prove that relationship wrong. Still mark the candidate "
                "contradicted when trusted context independently establishes the required result "
                "grain and the AST and data prove that the SQL violates it. "
                "For a ratio or percentage over entities, when a one-to-many join can repeat one "
                "entity, deduplicate the same entity identity in both numerator and "
                "denominator. If the AST instead counts multiplied join rows, return contradicted; "
                "do not apply this rule when the requested grain is the joined rows themselves. "
                "When trusted schema or evidence confirms that alternative endpoint rows are "
                "directional representations of the same relationship for one entity, count each "
                "entity-relationship pair once. If the AST counts both directional rows as separate "
                "relationships, return contradicted unless the question explicitly requests directions "
                "or endpoint rows. "
                "When trusted schema or evidence confirms that alternative endpoints are directional "
                "representations of the same relationship, one confirmed endpoint is sufficient for "
                "one requested shared attribute; do not require the other endpoint to be joined or "
                "projected unless the question or documents explicitly request endpoint-specific or "
                "both-role output. "
                "For a count of base entities, when a one-to-many join repeats an entity, count "
                "each entity identity once. If the AST instead counts the multiplied join rows, "
                "return contradicted; do not apply this rule when the question requests joined or detail rows. "
                "A scalar or yes/no answer form alone does not prove a single-row result and "
                "does not authorize aggregation; preserve the formula's row scope unless the "
                "question or trusted context explicitly requires another grain or aggregate. "
                "Multiple returned rows do not by themselves make the answer ambiguous. When "
                "the SQL follows all required bindings and the question and documents specify "
                "no tie-break or limit, preserve all matches and do not return ambiguous solely "
                "because multiple rows were returned. "
                "When the "
                "question requires an entity-level computation over a period, a candidate "
                "that selects an extremal raw observation without computing that requested "
                "grain cannot be consistent: return contradicted when trusted evidence "
                "resolves the mismatch, otherwise return ambiguous targeting the relevant "
                "supplied binding. This does not require a particular SQL aggregation or "
                "grouping syntax; an explicitly requested single record/entity-time "
                "extremum may be consistent. For other requested entity or time semantics, "
                "compare them with the table/data grain and aggregation and grouping in the AST. "
                "For an aggregate per entity, a display attribute is not proof of the entity grain. "
                "If the AST groups by that attribute, return consistent only when trusted context "
                "proves that attribute is unique per entity; return contradicted when trusted context "
                "or the result proves that distinct entities were merged, otherwise return ambiguous. "
                "Check every required binding in its requested semantic role, not merely whether "
                "its columns appear somewhere in the SQL. A required grouping dimension must "
                "participate in the computation at that grouping grain; its column appearing only "
                "in a filter or another role does not satisfy it. "
                "When trusted schema or a document explicitly maps a required semantic item to "
                "one physical column but the selected binding and SQL use a different physical "
                "column, return contradicted targeting that supplied binding and set repair_kind "
                "to semantic_binding_mismatch. "
                "Do not return semantic_binding_mismatch merely because a selected physical column "
                "is described as missing or different when the exact supplied binding, its cited "
                "trusted evidence and the SQL AST identify the same normalized table and column; an "
                "unqualified table and main.table are the same physical table. semantic_binding_mismatch "
                "requires a positive trusted fact that a different physical attribute carries the "
                "requested role. "
                "For semantic_binding_mismatch, copy repair_binding_id from the exact supplied "
                "binding being contradicted. Otherwise repair_binding_id must be null. "
                "predicate_authority must be null unless a contradicted DIMENSION needs one exact "
                "discriminator value search; then provide its typed PredicateRef and leave repair_kind null. "
                "The absence of repeated business wording in trusted text does not by itself "
                "contradict a selected supported relationship that the SQL follows. Return "
                "semantic_binding_mismatch only when trusted schema, documents, AST or returned "
                "data positively contradict that binding's requested role. A related or correlated "
                "attribute is not the requested attribute when trusted schema or documents "
                "explicitly describe it as a different attribute; return ambiguous targeting that supplied "
                "binding and set repair_kind to semantic_binding_mismatch. Otherwise repair_kind "
                "must be null. "
                "A selected binding's supported status proves authority, not that its "
                "business meaning matches the question, and must not override a "
                "conflicting trusted schema description. "
                "Distinguish selected bindings from columns actually referenced by the SQL AST. "
                "Never claim that SQL uses a selected binding's physical column unless the AST references that column. "
                "Do not infer that a metric is already aggregated at that required grain from "
                "table or column names or from one period value. Unless trusted evidence explicitly "
                "proves that grain, treat an AST that does not compute it as ambiguous. "
                "The order and scope of nested operations may change their meaning. Compare "
                "aggregates, extrema, filters and groupings with the exact computation requested "
                "by the question and documents instead of treating the presence of the same "
                "operations as proof of equivalence. Return contradicted only when trusted context "
                "proves the mismatch, otherwise ambiguous. When the "
                "trusted document explicitly specifies the exact computation, return contradicted "
                "when the AST adds, removes or reorders an aggregation so that it computes a "
                "different formula; do not request more schema or data evidence merely to justify "
                "an undocumented alternative computation. repair_kind must be null when the selected "
                "physical bindings are correct and only the computation differs. When an unbound required FORMULA is "
                "computed incorrectly, target one supplied input binding used by that formula as source_id. "
                "When the SQL exactly follows a computation "
                "explicitly specified by a trusted document, the reviewer must not replace that "
                "computation with an inferred business interpretation from schema descriptions. "
                "When the "
                "question compares a finite set of explicitly described alternatives and "
                "asks which alternative wins an extreme metric, the result must return "
                "the winning alternative label or role, not an inner entity, unless the "
                "question or documents explicitly request that identity or attribute. "
                "Mark a wrong answer form or projection contradicted. "
                "Final mandatory cardinality rule: expected_result_shape never constrains "
                "the number of returned rows. Never return contradicted or ambiguous merely "
                "because execution returned multiple rows; when no independent trusted conflict "
                "exists, return consistent."
            ),
            "question": value.state.query_spec.original_text,
            "query_spec": value.state.query_spec.model_dump(mode="json"),
            "sql": value.candidate.sql,
            "ast": asdict(value.parsed_ast),
            "bindings": [
                item.model_dump(mode="json") for item in value.requirements.selected_bindings
            ],
            "evidence": evidence,
            "documents": documents,
            "schema": value.schema,
            "columns": execution["columns"],
            "data": execution["data"],
        }
    ).decode("utf-8")


def _parse_response(value: object) -> _ModelReviewResponse:
    if type(value) is bytes:
        value = value.decode("utf-8", errors="strict")
    if type(value) is not str:
        raise TypeError("result review response is not text")
    parsed = json.loads(value)
    if (
        type(parsed) is dict
        and parsed.get("status") == "consistent"
        and "reason" in parsed
        and parsed["reason"] is None
    ):
        parsed = {**parsed, "reason": "result is consistent"}
        return _ModelReviewResponse.model_validate(parsed)
    if type(parsed) is dict and "short_reason" in parsed and "reason" not in parsed:
        normalized = dict(parsed)
        normalized["reason"] = normalized.pop("short_reason")
        return _ModelReviewResponse.model_validate(normalized)
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
    repair_binding_id=None,
    predicate_authority=None,
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
            repair_binding_id=repair_binding_id,
            predicate_authority=predicate_authority,
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
        repair_binding_id=repair_binding_id,
        predicate_authority=predicate_authority,
    )


__all__ = [
    "RESULT_REVIEW_RUNTIME_KEY",
    "RESULT_REVIEW_REQUIRED_RUNTIME_KEY",
    "ResultReviewReceipt",
    "create_result_review_capability",
    "evaluate_result_review_capability",
]
