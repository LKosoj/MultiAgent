"""One bounded model review of a successful Typed SQL result."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Literal

from dataclasses import asdict

from pydantic import model_validator

from ._semantic_coverage_boundary import evidence_has_state_authority
from ._sql_ast_identity import semantic_candidate_digest, source_sql_digest
from ._sql_ast_models import (
    ParsedSqlCandidate,
    QueryRole,
)
from .models import (
    CheckFailureCode,
    Digest,
    DiscriminatorValueBinding,
    HypothesisStatus,
    Id,
    NonEmptyText,
    NonNegativeInt,
    PhysicalColumnBinding,
    PredicateOperator,
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
from .semantic_plan import (
    build_semantic_ast,
    direct_physical_projection_column,
)
from .serialization import canonical_json_bytes


RESULT_REVIEW_RUNTIME_KEY = "text_to_sql_result_review"
RESULT_REVIEW_REQUIRED_RUNTIME_KEY = "_text_to_sql_result_review_required"
_RESULT_REVIEW_CAPABILITY_MARKER = object()
class ResultReviewReceipt(StrictModel):
    """Durable outcome from the one result-review turn."""

    record_kind: Literal["text2sql_result_review"] = "text2sql_result_review"
    review_kind: Literal["terminal", "conflict_arbitration"] = "terminal"
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
        CheckFailureCode.RESULT_SHAPE_MISMATCH,
        CheckFailureCode.FORMULA_SEMANTICS_MISMATCH,
    ] | None
    repair_kind: Literal["semantic_binding_mismatch"] | None = None
    repair_binding_id: Id | None = None
    predicate_authority: PredicateRef | None = None
    row_grain_requirement: Literal[
        "preserve_qualifying_rows", "deduplicate_entity"
    ] | None = None

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
        if self.row_grain_requirement is not None and self.verdict != "contradicted":
            raise ValueError("row grain requirement requires a contradiction")
        if self.review_kind == "conflict_arbitration" and self.verdict != "consistent":
            raise ValueError("arbitration review must be consistent")
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
    source_handle: Id | None = None
    source_id: Id | None = None
    repair_kind: Literal["semantic_binding_mismatch"] | None = None
    repair_binding_id: Id | None = None
    predicate_authority: PredicateRef | None = None
    row_grain_requirement: Literal[
        "preserve_qualifying_rows", "deduplicate_entity"
    ] | None = None


class _ArbitrationResponse(StrictModel):
    status: Literal["resolve", "unresolved"]
    candidate_id: Id | None = None

    @model_validator(mode="after")
    def validate_choice(self) -> "_ArbitrationResponse":
        if (self.status == "resolve") != (self.candidate_id is not None):
            raise ValueError("arbitration choice must match its status")
        return self


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


@dataclass(frozen=True, slots=True)
class _ResultReviewArbitrationCapability:
    state: ResearchState
    requirements: CoverageRequirements
    candidates: tuple[SqlCandidate, SqlCandidate]
    receipts: tuple[ResultReviewReceipt, ResultReviewReceipt]
    documents: tuple[object, ...]
    model: Callable[[str], str | bytes]
    schema: dict[str, object]


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


def create_result_review_arbitration_capability(
    *,
    state: ResearchState,
    requirements: CoverageRequirements,
    candidates: tuple[SqlCandidate, SqlCandidate],
    receipts: tuple[ResultReviewReceipt, ResultReviewReceipt],
    documents: tuple[object, ...],
    model: Callable[[str], str | bytes],
    schema: dict[str, object] | None = None,
) -> object:
    if (
        type(candidates) is not tuple
        or type(receipts) is not tuple
        or len(candidates) != 2
        or len(receipts) != 2
        or not callable(model)
        or (schema is not None and type(schema) is not dict)
    ):
        raise TypeError("result review arbitration inputs are invalid")
    first, second = receipts
    if (
        first.verdict != "contradicted"
        or second.verdict != "contradicted"
        or first.row_grain_requirement is None
        or second.row_grain_requirement is None
        or first.row_grain_requirement == second.row_grain_requirement
        or first.research_state_revision != second.research_state_revision
        or first.requirements_digest != second.requirements_digest
        or tuple(item.candidate_id for item in candidates)
        != (first.candidate_id, second.candidate_id)
    ):
        raise ValueError("result review arbitration pair is not a grain conflict")
    return _ResultReviewArbitrationCapability(
        state=state,
        requirements=requirements,
        candidates=candidates,
        receipts=receipts,
        documents=documents,
        model=model,
        schema={} if schema is None else schema,
    )


def evaluate_result_review_arbitration_capability(value: object) -> ResultReviewReceipt | None:
    if type(value) is not _ResultReviewArbitrationCapability:
        raise TypeError("result review arbitration capability is invalid")
    prompt = canonical_json_bytes(
        {
            "instruction": (
                "Resolve only this proven conflict between two executed SQL candidates. Use trusted "
                "context to choose one supplied candidate_id or unresolved. Do not generate, rewrite, "
                "execute SQL, create bindings, or change any formula."
            ),
            "question": value.state.query_spec.original_text,
            "query_spec": value.state.query_spec.model_dump(mode="json"),
            "candidates": [
                {"candidate_id": candidate.candidate_id, "sql": candidate.sql,
                 "receipt": receipt.model_dump(mode="json"), "execution": receipt.execution}
                for candidate, receipt in zip(value.candidates, value.receipts, strict=True)
            ],
            "documents": [str(document) for document in value.documents],
            "schema": value.schema,
        }
    ).decode("utf-8")
    try:
        response = _ArbitrationResponse.model_validate_json(value.model(prompt))
    except Exception:
        return None
    if response.status == "unresolved":
        return None
    assert response.candidate_id is not None
    selected = next(
        (
            (candidate, receipt)
            for candidate, receipt in zip(value.candidates, value.receipts, strict=True)
            if candidate.candidate_id == response.candidate_id
        ),
        None,
    )
    if selected is None:
        return None
    candidate, receipt = selected
    return ResultReviewReceipt(
        run_id=receipt.run_id,
        run_incarnation=receipt.run_incarnation,
        research_state_revision=receipt.research_state_revision,
        candidate_id=candidate.candidate_id,
        normalized_ast_digest=candidate.normalized_ast_digest,
        requirements_digest=receipt.requirements_digest,
        source_id=None,
        evidence_id=None,
        verdict="consistent",
        reason="independent arbitration selected the executed candidate",
        execution=receipt.execution,
        deterministic_failure_code=None,
        review_kind="conflict_arbitration",
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
                source_handle=_source_handle_for_source_id(requirements, source_id),
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
    if source_id := _direct_formula_projection_mismatch_source_id(
        state, requirements, parsed_ast
    ):
        reason = "requested formula is replaced by a direct physical input column"
        source_id, evidence_id, repair_binding_id = _response_target(
            state,
            requirements,
            _ModelReviewResponse(
                status="contradicted",
                reason=reason,
                source_handle=_source_handle_for_source_id(requirements, source_id),
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
            deterministic_failure_code=CheckFailureCode.FORMULA_SEMANTICS_MISMATCH,
            repair_binding_id=repair_binding_id,
        )
    if source_id := _deterministic_owner_mismatch_source_id(
        state, requirements, candidate, parsed_ast, value.schema
    ):
        reason = "candidate projects a same-named column from a table other than its confirmed owner"
        source_id, evidence_id, repair_binding_id = _response_target(
            state,
            requirements,
            _ModelReviewResponse(
                status="contradicted",
                reason=reason,
                source_handle=_source_handle_for_source_id(requirements, source_id),
                repair_kind="semantic_binding_mismatch",
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
            repair_kind="semantic_binding_mismatch",
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
    if response.repair_kind is not None and response.predicate_authority is not None:
        response = response.model_copy(update={"predicate_authority": None})
    response = _normalize_unsupported_discriminator_contradiction(
        state, requirements, candidate, parsed_ast, response
    )
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
        row_grain_requirement=response.row_grain_requirement,
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
    root_projections = tuple(
        projection
        for projection in parsed_ast.projections
        if projection.scope_id in root_scope_ids
    )
    root_projection_annotations = {
        projection.node_id: annotations_by_node.get(projection.node_id, set())
        for projection in root_projections
    }
    semantic_items_by_source_id = {
        item.source_id: item for item in state.query_spec.semantic_items
    }
    if (
        len(requested_source_ids) == 1
        and len(root_projections) > 1
        and any(
        semantic_items_by_source_id[source_id].kind is SemanticItemKind.FORMULA
        for source_id in requested_source_ids
        )
    ):
        for projection in root_projections:
            source_ids = root_projection_annotations[projection.node_id]
            unexpected_source_ids = source_ids - requested
            if unexpected_source_ids:
                return min(unexpected_source_ids)
        return requested_source_ids[0]
    if not requested.issubset(set().union(*root_projection_annotations.values())):
        return None
    for source_ids in root_projection_annotations.values():
        if source_ids and source_ids.isdisjoint(requested):
            return min(source_ids)
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


def _direct_formula_projection_mismatch_source_id(
    state: ResearchState,
    requirements: CoverageRequirements,
    parsed_ast: ParsedSqlCandidate,
) -> str | None:
    requested_source_ids = state.query_spec.requested_output_source_ids
    if len(requested_source_ids) != 1:
        return None
    source_id = requested_source_ids[0]
    item = next(
        item
        for item in state.query_spec.semantic_items
        if item.source_id == source_id
    )
    if item.kind is not SemanticItemKind.FORMULA:
        return None
    physical_columns = {
        binding.physical_column
        for binding in requirements.selected_bindings
        if binding.source_id == source_id
        and isinstance(binding, PhysicalColumnBinding)
    }
    if not physical_columns:
        return None
    namespaces = {table.namespace for table in requirements.allowed_tables}
    if len(namespaces) != 1:
        return None
    root_projections = tuple(
        projection
        for projection in parsed_ast.projections
        if any(
            scope.scope_id == projection.scope_id
            and scope.parent_scope_id is None
            and scope.query_role is QueryRole.ROOT
            for scope in parsed_ast.scopes
        )
    )
    if len(root_projections) != 1:
        return None
    direct_column = direct_physical_projection_column(
        parsed_ast,
        root_projections[0].expression,
        next(iter(namespaces)),
        requirements.allowed_tables,
        requirements.allowed_columns,
    )
    return source_id if direct_column in physical_columns else None


def _deterministic_owner_mismatch_source_id(
    state: ResearchState,
    requirements: CoverageRequirements,
    candidate: SqlCandidate,
    parsed_ast: ParsedSqlCandidate,
    schema: dict[str, object],
) -> str | None:
    item_by_source_id = {
        item.source_id: item for item in state.query_spec.semantic_items
    }
    namespaces = {table.namespace for table in requirements.allowed_tables}
    if len(namespaces) != 1:
        return None
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
    projected_source_ids = {
        source_id
        for annotation in semantic_ast.coverage.annotations
        if annotation.node_id
        in {
            projection.node_id
            for projection in parsed_ast.projections
            if projection.scope_id in root_scope_ids
        }
        for source_id in annotation.source_ids
    }
    for source_id in state.query_spec.requested_output_source_ids:
        item = item_by_source_id[source_id]
        if item.owner_source_id is None or source_id not in projected_source_ids:
            continue
        bindings = tuple(
            binding
            for binding in requirements.selected_bindings
            if binding.source_id == source_id
        )
        owner_bindings = tuple(
            binding
            for binding in requirements.selected_bindings
            if binding.source_id == item.owner_source_id
        )
        if (
            len(bindings) != 1
            or len(owner_bindings) != 1
            or not isinstance(bindings[0], PhysicalColumnBinding)
            or not isinstance(owner_bindings[0], PhysicalColumnBinding)
        ):
            continue
        selected = bindings[0].physical_column
        owner_table = owner_bindings[0].physical_column.table
        if selected.table != owner_table and _schema_has_column(
            schema, owner_table, selected.column
        ):
            return source_id
    return None


def _schema_has_column(schema: dict[str, object], table, column: str) -> bool:
    key = ".".join(
        part
        for part in (table.namespace, table.schema_name, table.table)
        if part is not None
    )
    value = schema.get(key)
    return (
        type(value) is dict
        and type(columns := value.get("columns")) is dict
        and column in columns
    )


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
        if response.source_handle is not None or response.source_id is not None:
            raise ValueError("consistent review must not name a repair source")
        return "review", "review", None
    if response.source_handle is not None and response.source_id is not None:
        raise ValueError("review must name either a source handle or legacy source")
    if response.source_handle is not None:
        source_id = _source_handle_mapping(requirements).get(response.source_handle)
        if source_id is None:
            raise ValueError("review source handle is not allowed")
        response = response.model_copy(update={"source_id": source_id})
    if response.source_id is None:
        raise ValueError("non-consistent review must name one trusted source")
    allowed_source_ids = {
        item.source_id for item in requirements.selected_bindings
    }
    if response.source_id not in allowed_source_ids:
        if len(allowed_source_ids) == 1:
            response = response.model_copy(
                update={"source_id": next(iter(allowed_source_ids))}
            )
        elif response.source_id.startswith("semantic:"):
            matches = tuple(
                source_id
                for source_id in allowed_source_ids
                if _is_single_edit_apart(response.source_id, source_id)
            )
            if len(matches) == 1:
                response = response.model_copy(update={"source_id": matches[0]})
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


def _normalize_unsupported_discriminator_contradiction(
    state: ResearchState,
    requirements: CoverageRequirements,
    candidate: SqlCandidate,
    parsed_ast: ParsedSqlCandidate,
    response: _ModelReviewResponse,
) -> _ModelReviewResponse:
    """Keep a certified discriminator when only its zero-row alternative differs."""

    if (
        response.status != "contradicted"
        or response.repair_kind is not None
        or response.repair_binding_id is not None
        or response.predicate_authority is not None
        or response.row_grain_requirement is not None
        or (response.source_handle is None) == (response.source_id is None)
    ):
        return response
    source_id = (
        response.source_id
        if response.source_id is not None
        else _source_handle_mapping(requirements).get(response.source_handle)
    )
    if source_id is None:
        return response
    bindings = tuple(
        binding
        for binding in requirements.selected_bindings
        if binding.source_id == source_id
        and isinstance(binding, DiscriminatorValueBinding)
    )
    if len(bindings) != 1:
        return response
    binding = bindings[0]
    item = next(
        (item for item in state.query_spec.semantic_items if item.source_id == source_id),
        None,
    )
    selected_values = _discriminator_members(
        binding.discriminator_predicate.operator,
        binding.discriminator_predicate.right,
    )
    alternative_values = _discriminator_members(
        None if item is None else item.operator,
        None if item is None else item.literal_or_reference,
    )
    if (
        item is None
        or item.operator is not binding.discriminator_predicate.operator
        or selected_values is None
        or alternative_values is None
        or alternative_values == selected_values
        or not _candidate_covers_selected_binding(
            state, requirements, candidate, parsed_ast, source_id
        )
    ):
        return response
    if not all(
        any(
            _exact_search_rows(evidence, binding.discriminator_column, value)
            for evidence in state.evidence
            if evidence.evidence_id in binding.evidence_ids
            and evidence_has_state_authority(evidence, state)
        )
        for value in selected_values
    ):
        return response
    for value in alternative_values:
        searches = tuple(
            result
            for evidence in state.evidence
            if (
                result := _exact_search_rows(
                    evidence, binding.discriminator_column, value
                )
            )
            is not None
            and evidence_has_state_authority(evidence, state)
        )
        if not any(not rows for rows in searches) or any(searches):
            return response
    return response.model_copy(
        update={
            "status": "consistent",
            "reason": "selected discriminator remains consistent with durable evidence",
            "source_handle": None,
            "source_id": None,
        }
    )


def _candidate_covers_selected_binding(
    state: ResearchState,
    requirements: CoverageRequirements,
    candidate: SqlCandidate,
    parsed_ast: ParsedSqlCandidate,
    source_id: str,
) -> bool:
    namespaces = {table.namespace for table in requirements.allowed_tables}
    if len(namespaces) != 1:
        return False
    semantic_ast = build_semantic_ast(
        candidate,
        parsed_ast,
        state.query_spec,
        requirements,
        next(iter(namespaces)),
    )
    return any(source_id in annotation.source_ids for annotation in semantic_ast.coverage.annotations)


def _discriminator_members(
    operator: PredicateOperator | None, right: object
) -> tuple[object, ...] | None:
    if operator is PredicateOperator.EQ and not isinstance(right, tuple):
        return (right,)
    if (
        operator is PredicateOperator.IN
        and isinstance(right, tuple)
        and right
    ):
        return right
    return None


def _exact_search_rows(
    evidence: object,
    column: object,
    value: object,
) -> tuple[object, ...] | None:
    if getattr(evidence, "target", None) != column:
        return None
    try:
        observation = json.loads(evidence.observation)
    except (AttributeError, TypeError, json.JSONDecodeError):
        return None
    payload = observation.get("payload") if type(observation) is dict else None
    if (
        type(payload) is not dict
        or observation.get("truncated") is not False
        or observation.get("provenance", {}).get("probe_kind") != "search_value"
        or payload.get("columns") != [column.column]
        or payload.get("requested_value") != value
        or type(payload.get("rows")) is not list
    ):
        return None
    return tuple(payload["rows"])


def _source_handle_mapping(requirements: CoverageRequirements) -> dict[str, str]:
    source_ids = tuple(
        dict.fromkeys(binding.source_id for binding in requirements.selected_bindings)
    )
    return {f"r{index}": source_id for index, source_id in enumerate(source_ids, 1)}


def _source_handle_for_source_id(
    requirements: CoverageRequirements, source_id: str
) -> str:
    for handle, mapped_source_id in _source_handle_mapping(requirements).items():
        if mapped_source_id == source_id:
            return handle
    raise ValueError("deterministic review source is not an allowed binding")


def _is_single_edit_apart(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) == 1
    shorter, longer = sorted((left, right), key=len)
    return any(longer[:index] + longer[index + 1 :] == shorter for index in range(len(longer)))


def _prompt(value: _ResultReviewCapability, execution: dict[str, object]) -> str:
    selected_source_ids = {
        binding.source_id for binding in value.requirements.selected_bindings
    }
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
    supported_hypotheses = [
        item.model_dump(mode="json")
        for item in value.state.hypotheses
        if item.status is HypothesisStatus.SUPPORTED
        and selected_source_ids.intersection(item.source_ids)
    ]
    return canonical_json_bytes(
        {
            "instruction": (
                "First determine the requested result grain. When the question requires an "
                "entity-level computation over a period, a candidate that selects an extremal "
                "raw observation without computing that requested grain cannot be consistent: "
                "return contradicted when trusted evidence resolves the mismatch, otherwise "
                "return ambiguous targeting the relevant supplied binding. Trusted evidence "
                "resolves the mismatch when it establishes multiple subperiod rows for each "
                "entity inside the period asked about; the question need not name an aggregate "
                "explicitly for one raw subperiod row to be the wrong grain. This does not "
                "require a particular SQL aggregation or grouping syntax; an explicitly "
                "requested single record/entity-time extremum may be consistent. "
                "Do not infer a period or aggregation solely because the selected attribute is stored in multiple historical rows. "
                "When the question specifies no period, snapshot, or aggregation, do not add one during review; "
                "preserve its requested ordering and limit unless trusted context positively contradicts them. "
                "When the original question uses a condition only to define the reference population of an "
                "aggregate used for comparison, independently compare the returned population from the original "
                "question against the aggregate reference population. The reference-only condition must stay "
                "within the aggregate reference scope; a filtered CTE or equivalent outer restriction that also "
                "narrows the returned population is contradicted. Target the supplied formula or filter binding "
                "for that condition and use the existing repair path. Do not apply this when the question "
                "independently requires that condition for the returned population. "
                "An explicit universal child quantifier must be enforced at the requested root grain; "
                "grouping or checking each returned root+child group does not prove all children, and "
                "child groups cannot substitute for the root result. Return contradicted when SQL merely "
                "selects or retains qualifying children without proving absence of violating observed children, "
                "whether result rows are at root or child grain. Do not apply this when the question "
                "explicitly requests child groups or pairs. "
                "When the question asks for root entities that do not contain any matching child, "
                "test absence of that child separately for each root entity. A document formula "
                "that counts matching child rows does not replace this explicit root-entity grain "
                "unless the question explicitly asks for a proportion of child rows. Physical "
                "child columns or row expressions in such a formula do not by themselves state a "
                "child-row result grain. Apply exact-formula preservation after respecting this "
                "question-defined grain unless the trusted formula states a different result grain "
                "or counting unit in semantic terms, rather than merely naming physical child "
                "columns or row expressions. "
                "When an explicit universal child quantifier has a required FILTER or TIME on that same "
                "child relation, that qualifying filter forms its observed child universe; do not require "
                "children outside it unless the question, QuerySpec, or trusted context explicitly requests "
                "the full universe. "
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
                "For a direct matching entity label on a same-identity relation that supplies "
                "a required condition or formula, trusted table and identity semantics are "
                "sufficient row-local authority; its column description does not need to call "
                "the label full or official. Do not name an alternative as a replacement unless "
                "trusted schema or documents establish that it belongs to the same entity at the "
                "same qualifying identity; a generic label in an unrelated joined relation is not enough. "
                "An external current, canonical, or persistent "
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
                "A selected SUPPORTED discriminator_value with an exact physical predicate that the AST follows "
                "confirms the stored physical representation; conceptual or document aliases alone do not contradict it. "
                "For that same physical column and operator, a case-only difference in that literal does not "
                "override the selected observed physical spelling. "
                "Punctuation or formatting alone does not override the selected observed physical spelling when both "
                "forms denote the same value; use the selected binding's observed literal in that case. "
                "An alternative literal may contradict the selected exact discriminator only when separate durable exact DB evidence "
                "positively contains that alternative for the same physical column and operator. QuerySpec/document wording and an "
                "untruncated exact search with rows=[] do not prove the stored literal. "
                "Use only this trusted context. Inspect the question, SQL, AST, "
                "bindings, evidence, documents, columns and data. Never generate, "
                "rewrite or execute SQL. Return only JSON object with exactly these keys: status, reason, "
                "source_handle, repair_kind, repair_binding_id, predicate_authority, row_grain_requirement. status must be exactly one of consistent, "
                "contradicted, ambiguous. "
                "source_handle must be null for consistent and one supplied "
                "binding source_handle for contradicted or ambiguous. Check the exact answer "
                "form and projection requested by the question and documents. "
                "row_grain_requirement must be null unless contradicted solely because the candidate must "
                "preserve qualifying rows or deduplicate an entity; then use preserve_qualifying_rows or "
                "deduplicate_entity respectively. "
                "Where a required FORMULA and trusted document explicitly specify aggregate operations "
                "and arguments, you may return contradicted for an incorrect scope or operation order, "
                "but row_grain_requirement must remain null unless the question, QuerySpec, or exact "
                "formula explicitly requires unique, distinct, or entity-once counting. A one-to-many "
                "relationship, historical rows, result magnitude, or audit advisory does not authorize "
                "DISTINCT or a row_grain_requirement. "
                "When execution includes a substantive audit finding that the SQL computes a "
                "different metric or makes a formula term constant, verify it against the trusted "
                "formula, AST, and returned data rather than ignoring it because it is advisory. "
                "Return contradicted when that independent comparison confirms the required "
                "computation is not preserved; the audit finding alone is not trusted authority. "
                "For a required fractional division or ratio, when the SQL dialect and trusted "
                "schema establish integer operands, bare division without real coercion computes "
                "a different result. Return contradicted targeting a supplied formula or input "
                "binding. Casting the numerator to a real type preserves the exact operands, "
                "division operator, order, and scale. "
                "Within one already-qualified WHERE/JOIN group, HAVING COUNT(*) = COUNT(DISTINCT "
                "grain_key) certifies exactly one observed non-NULL row per grain_key; do not reject "
                "it using hypothetical rows outside that qualifying rowset. It proves neither external "
                "or global completeness, another key, nor an explicitly different QuerySpec/document "
                "grain or distinct requirement. "
                "Compare the SQL with every required semantic item in QuerySpec. "
                "Only semantic items listed in requested_output_source_ids must be projected. "
                "Every other required item must still be used in its required semantic role, "
                "such as filtering, joining, grouping, aggregation or ordering, but is not "
                "required to be projected solely because it is required. "
                "When a required DIMENSION is an entity label or identifier that is not a "
                "requested output, and a trusted document defines the selected entity row "
                "through another confirmed predicate that the SQL applies, do not require its "
                "label or identifier column or its relation merely to repeat that identity. "
                "This does not waive any required filter, join, grain, ordering, aggregation, "
                "or other semantic condition. "
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
                "When the question requests the values of an attribute themselves as a set, "
                "whether bounded or unbounded, and repeated source rows carry the same value, "
                "return each value once before applying a bound. When the question requests rows "
                "or entities and merely displays that attribute, preserve separate rows. This "
                "distinction does not apply to counts, metrics, formulas, or ordinary detail output. "
                "When the requested output is a proven unique identity of a root entity and a "
                "joined child relation is used only to qualify that entity, with no child output "
                "requested, return each qualifying root identity once. This does not apply to "
                "counts, metrics, formulas, or requested child rows. "
                "A requested conditional entity output that preserves surrounding rows "
                "must be implemented in the SELECT projection with CASE or IIF; its condition "
                "must use a textual absence marker rather than SQL NULL, must not be moved to "
                "WHERE or replaced by projecting the status or predicate. "
                "A required FORMULA is not satisfied by a different physical column "
                "or precomputed value merely because it appears to have the same unit "
                "or meaning; schema descriptions do not override the required computation. "
                "When an exact trusted formula applies arithmetic to its confirmed inputs, "
                "the AST must retain that operator and those inputs. Do not replace it with a "
                "domain calculation, duration conversion, date-difference helper, unit normalization, "
                "or rounding unless the trusted formula explicitly requires that operation; return "
                "contradicted with repair_kind null. "
                "When a numeric metric is requested with a fixed number of decimal places, keep "
                "the SQL result numeric and use numeric rounding. Do not use text formatting or "
                "append a display suffix unless the question explicitly requests textual output "
                "or that suffix as part of the returned value; otherwise return contradicted. "
                "When a required FORMULA specifies how to compute a metric, it takes precedence over a selected "
                "physical binding for that metric. Do not contradict SQL that follows the formula merely because "
                "it computes the metric from a different trusted input column. "
                "Silence about a named aggregate in a trusted formula is not positive "
                "contradiction to an aggregate already used by an otherwise correct required FORMULA. "
                "Return contradicted for that aggregate only when trusted context positively conflicts: "
                "an exact formula whose AST does not follow it, an explicit raw-row or single "
                "entity-time grain, or an explicitly incompatible aggregate. This does not authorize "
                "adding aggregation absent a required FORMULA, question, or trusted context that requires it. "
                "When the question requests a ranked top N by the lowest or highest aggregated metric, "
                "a trusted MIN or MAX description of that extremum defines the ranking direction; "
                "do not require an additional outer MIN or MAX that collapses the N result rows. "
                "Preserve the ORDER BY and LIMIT N over the computed metric. "
                "Preserve every component unit named by trusted context. A textual suffix identified as integer milliseconds "
                "must be divided by 1000, even when observed values omit leading zeroes; treating it as decimal fractional digits "
                "changes the documented unit and must be contradicted. "
                "If the SQL substitutes a physical column for an unbound required FORMULA, "
                "return contradicted and use the supplied binding whose column substituted "
                "for the formula as source_handle. In that case repair_kind must be null because "
                "the computation, not the physical binding, is wrong. "
                "If the SQL omits or fails to apply a required semantic item while the selected physical binding is correct, "
                "return contradicted targeting that binding; repair_kind must be null because the SQL, not the binding, is wrong. "
                "An expression.expression in a derived binding is a model hypothesis copied "
                "from expression_claim, not evidence that the SQL performs that computation; "
                "never use it to resolve a conflict with the question, "
                "required semantic roles, documents, AST or returned rows. When the "
                "selected supported derived binding may complete a documented shorthand by adding a required reference input; "
                "the added input alone is not a contradiction; require trusted context that positively contradicts it. "
                "For a required age or duration semantic role, a candidate AST using confirmed birth and "
                "event/reference inputs or a confirmed full-date computation may complete a narrower one-input "
                "calendar shorthand in normalized meaning, expression_claim, or document. A stale one-input "
                "binding or shorthand alone does not contradict it; require an independent positive trusted contradiction. "
                "When the "
                "candidate projects an auxiliary computation solely because it is needed "
                "for ordering or grouping, do not treat it as requested unless the question "
                "or documents explicitly request it; mark that extra projection contradicted. "
                "A technical physical key used only for JOIN, GROUP BY, ORDER BY, window partition, "
                "or dedup may be used internally but must not be root SELECT unless QuerySpec or "
                "trusted context explicitly requests that identifier or label output; mark that extra "
                "projection contradicted. "
                "When a requested human-readable name is replaced by a selected reference, key, code, slug, or handle "
                "while trusted schema descriptions distinguish human-readable name components, return contradicted "
                "with semantic_binding_mismatch targeting that selected binding. Do not apply this when the question "
                "or QuerySpec explicitly requests an identifier or reference. "
                "For a required position-sensitive FORMULA, compare each fixed-position "
                "expression in the AST with the confirmed physical representation of its "
                "supplied input binding. If the positions select a separator or a different "
                "component, return contradicted targeting that supplied input binding, even "
                "when execution returned NULL or no rows. A trusted document's exact "
                "computation remains authoritative only while compatible with the confirmed "
                "physical representation of its input. This does not make NULL or an empty "
                "result contradictory when the expression is compatible. "
                "When a required FILTER or TIME expresses a calendar component, age, or duration from a "
                "DATE or TIME source, SQL must apply an explicit transformation stated by the question, "
                "documents, or normalized meaning, or a confirmed full temporal boundary. A raw DATE or "
                "TIME column cannot be directly compared with a dimensionless numeric threshold. A "
                "SUPPORTED binding proves source authority, not transformation correctness. For this "
                "violation, return contradicted targeting the supplied binding and leave repair_kind null. "
                "Calendar-part extraction is allowed only when the question, documents, or normalized "
                "meaning requests that calendar part. Age or duration must use a reference, event, or "
                "current time, or compare full dates. Extracting only the year from one birth or date "
                "source is not age or duration. A matching expression, binding, or document cannot "
                "override this. "
                "Do not reject a full-date-to-full-date boundary, timestamp-to-timestamp boundary, "
                "dialect-supported calendar-part extraction when that calendar part is requested, or "
                "explicit age or duration computation. "
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
                "A selected relationship binding proves its physical path, not that the path "
                "represents the requested population. Even if selected bindings no longer retain "
                "the earlier independent child-to-parent paths, the question or trusted formula "
                "together with schema roles can establish a measured-child population of qualifying "
                "parents. When the AST changes that population to direct association participants, "
                "return contradicted. This population rule takes precedence over later exact-formula "
                "multiset preservation; preserve that multiset only after the requested population "
                "is retained. "
                "While the AST still uses an association path that substitutes the measured population "
                "with direct participants, this remains the primary contradicted reason and keeps the "
                "same source_handle; DISTINCT, a unique subquery, multiplicity, or exact-formula multiset "
                "preservation neither fixes nor replaces it; evaluate those only after restoring the "
                "requested population. "
                "A matching audit issue is additional evidence to examine, not semantic "
                "authority. This does not contradict explicitly requested direct participation or "
                "relationship/detail rows. "
                "For a ratio or percentage over entities, deduplicate only when the question, QuerySpec, "
                "or trusted formula explicitly requires unique, distinct, or entity-once counting. A "
                "one-to-many relationship or an entity name alone does not add that requirement. "
                "COUNT(input) in an exact trusted formula counts non-null input occurrences in the "
                "qualifying relational row set and does not implicitly add DISTINCT; binding input to a "
                "base-entity identifier does not change that operation. Preserve that count when the AST "
                "follows the formula unless unique, distinct, or entity-once counting is explicit. "
                "When no exact trusted formula fixes the count operation, treat the entity population explicitly named by the question "
                "as the denominator population; do not replace it with rows of the table that stores "
                "a qualifying attribute. When that attribute exists only in a related table, require "
                "the relationship back to the named base entity. If the AST instead computes the ratio "
                "over related attribute rows, return contradicted. "
                "For one required formula, each required FILTER or TIME predicate that defines its population "
                "must apply to every numerator and denominator term; if only one term applies it, return "
                "contradicted. Use separate populations only when the question or trusted formula explicitly "
                "requires separate populations. "
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
                "For counts as well as ratios, do not infer DISTINCT, a unique subquery, or EXISTS unless "
                "the question, QuerySpec, or trusted context explicitly requires unique, distinct, or "
                "entity-once counting. A named entity, identifier, or one-to-many join alone does not prove deduplication. "
                "When a required METRIC or FORMULA and a trusted document explicitly specify the exact "
                "AVG or SUM/COUNT formula and its counting unit, and the AST follows that exact "
                "formula, "
                "preserve the qualifying join-row multiset. An entity repeated by a join alone "
                "does not authorize DISTINCT, a unique subquery, or EXISTS that would change "
                "that multiset; permit such a change only when the question, QuerySpec, or exact "
                "trusted formula explicitly requests unique, distinct, or entity-once counting. "
                "When separate operands of one required formula come from different one-to-many "
                "child relations of the same parent, joining those children before aggregation "
                "multiplies both row populations and therefore does not preserve the formula. "
                "Compute each aggregate in its own child scope under the shared parent filters, "
                "then combine the aggregate results. Return contradicted when the AST instead "
                "aggregates over the multiplied child join; this correction does not add DISTINCT. "
                "When an aggregate measures rows of one child relation and a separate child relation "
                "only qualifies their common parent, preserve the measured child population. Return "
                "contradicted when the AST joins qualifying child rows before aggregation and thereby "
                "multiplies measured rows; it must use existence filtering or a deduplicated "
                "qualifying-parent set. Do not reject that join when the question or trusted exact "
                "formula explicitly requests relationship or detail rows as its counting unit. "
                "COUNT(identifier) with a predicate from a related relation preserves those stated "
                "operations but does not itself establish physical joined-row multiplicity. When "
                "that count measures a child relation and a sibling relation only qualifies their "
                "common parent, return contradicted if the AST joins qualifying sibling rows and "
                "multiplies the measured population. A multiplied relationship/detail-row population "
                "is valid only when the question or trusted document explicitly names those rows as "
                "its counting unit. Within an already established population, COUNT(identifier) "
                "remains non-DISTINCT unless unique, distinct, or entity-once counting is explicit. "
                "A scalar or yes/no answer form alone does not prove a single-row result and "
                "does not authorize aggregation; preserve the formula's row scope unless the "
                "question or trusted context explicitly requires another grain or aggregate. "
                "Multiple returned rows do not by themselves make the answer ambiguous. When "
                "the SQL follows all required bindings and the question and documents specify "
                "no tie-break or limit, preserve all matches and do not return ambiguous solely "
                "because multiple rows were returned. "
                "When the question asks for the group or groups attaining an extreme aggregate, "
                "return every group tied at the requested extreme but exclude groups whose aggregate "
                "is not at that extreme. Preserving tied winners does not by itself require LIMIT 1; "
                "merely ordering and returning every group does not answer the requested extremum. "
                "For other requested entity or time semantics, "
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
                "When a required FILTER or TIME on an event, detail, history, audit, or log row selects "
                "its qualifying parent/entity, that row's actor, author, owner, or updater foreign key "
                "does not become an independently requested role merely by association. Preserve a "
                "selected supported parent/entity-role relationship that the SQL follows. Do not apply "
                "this preservation when the question or a trusted document explicitly requests the "
                "filter-row actor, author, owner, or updater role. "
                "A selected binding's supported status proves authority, not that its "
                "business meaning matches the question, and must not override a "
                "conflicting trusted schema description. "
                "A relevant supported_hypothesis may resolve that mismatch only when it "
                "explicitly establishes that no direct requested attribute exists, exactly one "
                "plausible entity-owned proxy remains after exhaustive inspection, and the proxy "
                "is not literal equivalence. When the selected binding and AST follow that exact "
                "supported conclusion, do not reopen the literal-label mismatch. This exception "
                "does not authorize a merely related or correlated attribute without such a "
                "supported hypothesis. "
                "A selected SUPPORTED binding may also preserve that best-available proxy conclusion "
                "without a separate hypothesis only when the complete trusted schema and documents "
                "show that the direct requested attribute is absent, the selected column belongs to "
                "the requested entity, its described role is a plausible answer proxy, and no second "
                "plausible entity-owned proxy remains. Do not reopen the literal-label mismatch in "
                "that case. A merely related or correlated attribute still fails this rule. "
                "When an event or detail measure is selected for a requested summary, standing, or cumulative "
                "measure, return contradicted with semantic_binding_mismatch targeting that selected binding; "
                "supported status does not override this role conflict. Do not apply this when the question or "
                "QuerySpec explicitly requests the event or operation. "
                "When a request asks which entity has the minimum or maximum of a measure, that measure must "
                "have the entity's grain when trusted schema distinguishes same-named detail and "
                "summary, standing, or cumulative fields; selecting the detail field is contradicted with "
                "semantic_binding_mismatch targeting that selected binding. Do not apply this when the "
                "question or QuerySpec explicitly requests the event, operation, or detail row. "
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
                "question or QuerySpec requests an aggregate of a quantity computed separately "
                "for each entity, require the inner value once per entity and the outer aggregate "
                "over those entity rows. For a matching-child count, include zero matching children "
                "for every entity in the qualifying parent set. A trusted exact formula that names "
                "only a row expression but omits the entity grain or counting unit does not erase "
                "that explicit entity grain. This does not override a trusted formula that explicitly "
                "states another row scope or counting unit. "
                "When the "
                "trusted document explicitly specifies the exact computation, return contradicted "
                "when the AST adds, removes or reorders an aggregation so that it computes a "
                "different formula; do not request more schema or data evidence merely to justify "
                "an undocumented alternative computation. repair_kind must be null when the selected "
                "physical bindings are correct and only the computation differs. When an unbound required FORMULA is "
                "computed incorrectly, target one supplied input binding used by that formula as source_handle. "
                "When the SQL exactly follows a computation "
                "explicitly specified by a trusted document and that document explicitly states "
                "its row scope or counting unit, the reviewer must not replace that "
                "computation with an inferred business interpretation from schema descriptions. "
                "When a trusted document explicitly defines a percentage or ratio with "
                "a conditioned aggregate numerator and the same named input aggregate as "
                "its denominator, preserve that named aggregate input; do not infer a different "
                "counting unit merely from an entity noun in the question. "
                "When the "
                "question compares a finite set of explicitly described alternatives and "
                "asks which alternative wins an extreme metric, the result must return "
                "the winning alternative label or role, not an inner entity, unless the "
                "question or documents explicitly request that identity or attribute. "
                "When alternatives are defined by MIN(...) and MAX(...), use Min and Max "
                "respectively as their exact result labels. "
                "Mark a wrong answer form or projection contradicted. "
                "Final mandatory cardinality rule: expected_result_shape never constrains "
                "the number of returned rows. Never return contradicted or ambiguous merely "
                "because execution returned multiple rows; when no independent trusted conflict "
                "exists, return consistent. "
                "Final mandatory owner checklist, which takes precedence over earlier row-local and "
                "full-label rules: (1) Determine the owner of every requested output "
                "from the question and normalized meaning. (2) Determine the owner table from table "
                "identity and description; a column description need not repeat the owner. (3) If a "
                "same-named event or record column is selected instead of the direct column of the named "
                "entity, return contradicted with semantic_binding_mismatch. (4) Conversely, do not reject "
                "a direct named-entity column because an event or record has a same-named column. "
                "A qualifying relation that trusted schema describes as another representation of the same "
                "named entity at the same identity key is not an event or record for this checklist: prefer "
                "its direct qualifying-row label over adding a separate master or entity join solely for "
                "another label, unless the question explicitly requests a current, canonical, master, "
                "persistent, or independent attribute. Before accepting a requested entity label as "
                "consistent, compare every direct matching label already visible on same-identity "
                "representations that supply a required condition or formula. Do not stop at the first "
                "plausible master label. "
                "Final mandatory population and time check: an ordinary requested count of named "
                "base entities must preserve the base-entity population when no trusted exact "
                "formula explicitly establishes another counting unit. A related table may qualify "
                "those entities through EXISTS or a deduplicated qualifying-key set without creating "
                "DISTINCT or entity-once semantics. Explicit relationship/detail-row counting units "
                "and exact formula counting units, including their existing plain COUNT behavior, "
                "remain exceptions. Return contradicted for a base-population violation or when a "
                "full date/time value is directly compared with a bare calendar-year literal. A full "
                "date/time value directly compared with a bare calendar-year literal cannot be "
                "consistent. Calendar-year extraction or a trusted full-date boundary remains valid. "
                "An advisory issue is only supporting evidence, not authority."
            ),
            "question": value.state.query_spec.original_text,
            "query_spec": value.state.query_spec.model_dump(mode="json"),
            "sql": value.candidate.sql,
            "ast": asdict(value.parsed_ast),
            "bindings": [
                {
                    **item.model_dump(mode="json"),
                    "source_handle": _source_handle_for_source_id(
                        value.requirements, item.source_id
                    ),
                }
                for item in value.requirements.selected_bindings
            ],
            "supported_hypotheses": supported_hypotheses,
            "evidence": evidence,
            "documents": documents,
            "schema": value.schema,
            "columns": execution["columns"],
            "data": execution["data"],
            "advisory_issues": execution.get("advisory_issues", []),
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
        and parsed.get("repair_kind") == "semantic_binding_mismatch"
    ):
        parsed = {**parsed, "predicate_authority": None}
    if (
        type(parsed) is dict
        and parsed.get("status") == "contradicted"
        and parsed.get("repair_kind") in {
        "preserve_qualifying_rows",
        "deduplicate_entity",
        }
    ):
        repair_kind = parsed["repair_kind"]
        if (
            parsed.get("row_grain_requirement") in {None, repair_kind}
            and parsed.get("repair_binding_id") is None
        ):
            parsed = {
                **parsed,
                "repair_kind": None,
                "row_grain_requirement": repair_kind,
            }
    if (
        type(parsed) is dict
        and parsed.get("status") == "consistent"
        and "reason" in parsed
        and parsed["reason"] is None
    ):
        parsed = {**parsed, "reason": "result is consistent"}
    if type(parsed) is dict and "short_reason" in parsed and "reason" not in parsed:
        normalized = dict(parsed)
        normalized["reason"] = normalized.pop("short_reason")
        parsed = normalized
    if type(parsed) is dict:
        response = _ModelReviewResponse.model_validate_json(canonical_json_bytes(parsed))
    else:
        response = _ModelReviewResponse.model_validate_json(value)
    if response.predicate_authority is not None and response.status != "contradicted":
        raise ValueError("predicate authority requires a contradiction")
    return response


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
    row_grain_requirement=None,
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
            row_grain_requirement=row_grain_requirement,
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
        row_grain_requirement=row_grain_requirement,
    )


__all__ = [
    "RESULT_REVIEW_RUNTIME_KEY",
    "RESULT_REVIEW_REQUIRED_RUNTIME_KEY",
    "ResultReviewReceipt",
    "create_result_review_arbitration_capability",
    "create_result_review_capability",
    "evaluate_result_review_arbitration_capability",
    "evaluate_result_review_capability",
]
