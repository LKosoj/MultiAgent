"""Pure admission of resolved semantic research decisions.

The research model supplies only :mod:`research_decision` proposals.  This
module gives those proposals stable physical identities and accepts status
changes only when already-recorded, fresh probe observations prove them.
It deliberately has no registry, database, model, or persistence dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping

from pydantic import ValidationError

from ._semantic_value_certificate import (
    ExactValueCertificateError,
    evidence_observes_exact_column,
    evidence_observes_exact_value,
    predicate_has_exact_value_certificate,
    predicate_has_valid_literal,
)
from .evidence import probe_result_to_evidence
from .freshness import FreshnessContext, FreshnessStatus, evaluate_evidence_freshness
from .models import (
    BindingBase,
    BindingStatus,
    BudgetState,
    ColumnRef,
    Digest,
    DerivedExpressionBinding,
    DiscriminatorValueBinding,
    DocumentRef,
    DocumentRuleBinding,
    EvidenceRecord,
    ExpressionRef,
    Hypothesis,
    HypothesisStatus,
    JoinCandidate,
    JoinCandidateStatus,
    JoinEdge,
    JoinType,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    TableRef,
    TargetRef,
    VerticalAttributeBinding,
    Id,
    NonEmptyText,
    StrictModel,
)
from .policy import canonical_action_digest
from .probes import ArtifactReader, ProbeResult
from .provenance import parse_probe_observation, read_evidence_provenance
from .result_expectations import (
    ResultExpectationCertificateError,
    derive_result_expectations as _derive_result_expectations,
)
from .research_decision import (
    BindingAssessment,
    DerivedExpressionCandidate,
    DiscriminatorValueCandidate,
    DocumentRuleCandidate,
    ExistingHypothesisRef,
    ExistingJoinRef,
    HypothesisAssessment,
    LogicalColumnRef,
    LogicalPredicate,
    LogicalTableRef,
    NewBindingProposal,
    NewHypothesisProposal,
    NewJoinProposal,
    ResearchDecisionV1,
    SemanticCommitRequest,
    StopRequest,
    ToolIntent,
    VerticalAttributeCandidate,
    PhysicalColumnCandidate,
    JoinAssessment,
)
from .state import ResearchNovelty, ResearchTransitionResult, apply_research_transition


class SemanticReducerError(ValueError):
    """A semantic proposal is not admissible from the supplied trusted facts."""


def derive_result_expectations(
    state: ResearchState,
    action: ResearchAction,
    evidence: EvidenceRecord,
):
    """Expose closed result-certificate derivation through reducer errors."""

    try:
        return _derive_result_expectations(state, action, evidence)
    except ResultExpectationCertificateError as exc:
        raise SemanticReducerError(str(exc)) from exc


class ResolvedTable(StrictModel):
    """One exact logical-to-physical table resolution made before this reducer."""

    logical_table: NonEmptyText
    physical_table: TableRef


class ResolvedColumn(StrictModel):
    """One exact logical-to-physical column resolution made before this reducer."""

    logical_table: NonEmptyText
    logical_column: NonEmptyText
    physical_column: ColumnRef


class TrustedSemanticBatch(StrictModel):
    """Resolved physical names available to a single reducer turn."""

    schema_namespace_version: Digest
    tables: tuple[ResolvedTable, ...]
    columns: tuple[ResolvedColumn, ...]
    documents: tuple[DocumentRef, ...] = ()
    schema_columns: tuple[ColumnRef, ...] = ()
    declared_join_ids: tuple[Id, ...] = ()

    def model_post_init(self, __context: object) -> None:
        table_keys = [item.logical_table for item in self.tables]
        column_keys = [
            (item.logical_table, item.logical_column) for item in self.columns
        ]
        document_ids = [item.document_id for item in self.documents]
        if len(table_keys) != len(set(table_keys)):
            raise ValueError("resolved logical tables must be unique")
        if len(column_keys) != len(set(column_keys)):
            raise ValueError("resolved logical columns must be unique")
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("resolved documents must be unique")


class TrustedToolClaim(StrictModel):
    """Trusted physical action claim; it is not authored by the research model."""

    action_id: Id
    tool_name: NonEmptyText
    target: TargetRef
    parameters: tuple[tuple[NonEmptyText, str | int | float | bool | None], ...]

    def model_post_init(self, __context: object) -> None:
        keys = [key for key, _ in self.parameters]
        if len(keys) != len(set(keys)):
            raise ValueError("trusted tool parameters must have unique keys")


@dataclass(frozen=True, slots=True)
class SemanticTurnAdmission:
    """Prepared pure write set for exactly one action, or an inert stop turn."""

    state: ResearchState
    action: ResearchAction | None
    hypotheses: tuple[Hypothesis, ...]
    bindings: tuple[BindingBase, ...]
    join_candidates: tuple[JoinCandidate, ...]
    budget_state: BudgetState | None
    declared_join_ids: tuple[Id, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticCommitResult:
    state: ResearchState
    novelty: ResearchNovelty
    transition: ResearchTransitionResult | None


_TOOL_KINDS = {
    "search_schema_catalog": ResearchActionKind.INSPECT_CATALOG,
    "inspect_table": ResearchActionKind.INSPECT_TABLE,
    "inspect_column": ResearchActionKind.INSPECT_COLUMN,
    "inspect_relationships": ResearchActionKind.INSPECT_RELATIONSHIPS,
    "profile_column": ResearchActionKind.PROFILE_COLUMN,
    "sample_rows": ResearchActionKind.SAMPLE_ROWS,
    "search_value": ResearchActionKind.SEARCH_VALUE,
    "get_distinct_values": ResearchActionKind.DISTINCT_VALUES,
    "execute_research_probe": ResearchActionKind.EXECUTE_PROBE,
    "read_schema_evidence": ResearchActionKind.READ_DOCUMENT,
}


def _revalidate_state(state: ResearchState) -> ResearchState:
    if not isinstance(state, ResearchState):
        raise SemanticReducerError("state must satisfy its strict contract")
    try:
        checked = ResearchState.model_validate(
            state.model_dump(mode="python", round_trip=True, warnings="error")
        )
    except (TypeError, ValueError, ValidationError):
        raise SemanticReducerError("state must satisfy its strict contract") from None
    if len(checked.action_history) != checked.revision:
        raise SemanticReducerError("action history length must equal state revision")
    return checked


def admit_semantic_turn(
    state: ResearchState,
    decision: ResearchDecisionV1,
    *,
    batch: TrustedSemanticBatch,
    freshness_context: FreshnessContext,
    tool_claim: TrustedToolClaim | None = None,
    budget_state: BudgetState | None = None,
) -> SemanticTurnAdmission:
    """Prepare IDs, candidates, assessments, and the one trusted next action.

    Citations must reference evidence from *before* the new action.  This is
    what prevents a probe result from proving a proposal made in its own turn.
    """

    if not isinstance(decision, ResearchDecisionV1):
        raise SemanticReducerError("decision must satisfy its strict contract")
    current = _revalidate_state(state)
    if batch.schema_namespace_version != current.schema_namespace_version:
        raise SemanticReducerError("resolved batch schema version does not match state")
    if (
        freshness_context.run_id != current.run_id
        or freshness_context.run_incarnation != current.run_incarnation
    ):
        raise SemanticReducerError("freshness context run does not match state")
    if freshness_context.schema_namespace_version != current.schema_namespace_version:
        raise SemanticReducerError(
            "freshness context schema version does not match state"
        )
    if isinstance(decision.next, StopRequest):
        if tool_claim is not None:
            raise SemanticReducerError("a stop turn cannot carry a tool claim")
        return SemanticTurnAdmission(current, None, (), (), (), budget_state)
    if isinstance(decision.next, ToolIntent) and tool_claim is None:
        raise SemanticReducerError("a tool turn requires one trusted tool claim")
    if isinstance(decision.next, SemanticCommitRequest) and tool_claim is not None:
        raise SemanticReducerError("a semantic commit cannot carry a tool claim")

    resolver = _Resolver(batch)
    evidence = {item.evidence_id: item for item in current.evidence}
    fresh = _fresh_evidence(evidence, freshness_context)
    hypotheses_by_id = {item.hypothesis_id: item for item in current.hypotheses}
    bindings_by_id = {item.binding_id: item for item in current.bindings}
    joins_by_id = {item.join_id: item for item in current.join_candidates}

    new_hypotheses: list[Hypothesis] = []
    new_joins: list[JoinCandidate] = []
    new_bindings: list[BindingBase] = []
    proposal_ids: dict[str, str] = {}
    for proposal in decision.proposals:
        if isinstance(proposal, NewHypothesisProposal):
            item = _new_hypothesis(proposal, resolver, current.schema_namespace_version)
            proposal_ids[proposal.proposal_key] = item.hypothesis_id
            new_hypotheses.append(item)
        elif isinstance(proposal, NewJoinProposal):
            item = _new_join(
                proposal,
                resolver,
                current.schema_namespace_version,
            )
            if item.join_id in joins_by_id:
                raise SemanticReducerError("new join already exists")
            proposal_ids[proposal.proposal_key] = item.join_id
            new_joins.append(item)
    _require_unique_ids(
        (item.hypothesis_id for item in new_hypotheses),
        "hypothesis",
    )
    _require_unique_ids((item.join_id for item in new_joins), "join")
    all_joins = {**joins_by_id, **{item.join_id: item for item in new_joins}}
    for proposal in decision.proposals:
        if isinstance(proposal, NewBindingProposal):
            item = _new_binding(
                proposal,
                resolver,
                all_joins,
                proposal_ids,
                current.schema_namespace_version,
            )
            proposal_ids[proposal.proposal_key] = item.binding_id
            new_bindings.append(item)
    _require_unique_ids((item.binding_id for item in new_bindings), "binding")

    updates_hypotheses: list[Hypothesis] = []
    updates_bindings: list[BindingBase] = []
    updates_joins: list[JoinCandidate] = []
    for proposal in decision.proposals:
        if isinstance(proposal, HypothesisAssessment):
            subject = _existing_id(proposal.subject, "hypothesis")
            old = hypotheses_by_id.get(subject)
            if old is None:
                raise SemanticReducerError(
                    "hypothesis assessment subject does not exist"
                )
            cited = _cited_records(proposal.citation_evidence_ids, evidence, fresh)
            updates_hypotheses.append(
                _assess_hypothesis(old, proposal.certificate, cited)
            )
        elif isinstance(proposal, BindingAssessment):
            subject = _existing_id(proposal.subject, "binding")
            old = bindings_by_id.get(subject)
            if old is None:
                raise SemanticReducerError("binding assessment subject does not exist")
            cited = _cited_records(proposal.citation_evidence_ids, evidence, fresh)
            updates_bindings.append(
                _assess_binding(
                    old,
                    proposal.certificate,
                    cited,
                    joins_by_id,
                    schema_columns=batch.schema_columns,
                )
            )
        elif isinstance(proposal, JoinAssessment):
            subject = _existing_id(proposal.subject, "join")
            old = joins_by_id.get(subject)
            if old is None:
                raise SemanticReducerError("join assessment subject does not exist")
            cited = _cited_records(proposal.citation_evidence_ids, evidence, fresh)
            updates_joins.append(
                _assess_join(
                    old,
                    proposal.certificate,
                    cited,
                    declared_join_ids=batch.declared_join_ids,
                )
            )

    if isinstance(decision.next, SemanticCommitRequest):
        action = _action_for_semantic_commit(decision, current)
    else:
        if tool_claim is None:
            raise SemanticReducerError("a tool turn requires one trusted tool claim")
        action = _action_for_tool_turn(decision.next, tool_claim, proposal_ids, current)
    return SemanticTurnAdmission(
        current,
        action,
        tuple(
            sorted(
                (*new_hypotheses, *updates_hypotheses),
                key=lambda item: item.hypothesis_id,
            )
        ),
        tuple(
            sorted((*new_bindings, *updates_bindings), key=lambda item: item.binding_id)
        ),
        tuple(sorted((*new_joins, *updates_joins), key=lambda item: item.join_id)),
        budget_state,
        batch.declared_join_ids,
    )


def _require_unique_ids(identifiers, label: str) -> None:
    values = tuple(identifiers)
    if len(values) != len(set(values)):
        raise SemanticReducerError(
            f"resolved proposals produce duplicate {label} identifiers"
        )


def commit_semantic_turn(
    admission: SemanticTurnAdmission,
    *,
    probe_result: ProbeResult | None = None,
    read_artifact: ArtifactReader | None = None,
) -> SemanticCommitResult:
    """Atomically commit one prepared action and, only on success, its evidence."""

    if not isinstance(admission, SemanticTurnAdmission):
        raise SemanticReducerError("admission must be produced by admit_semantic_turn")
    if admission.action is None:
        if probe_result is not None:
            raise SemanticReducerError("a stop turn cannot have a probe result")
        return SemanticCommitResult(
            admission.state,
            ResearchNovelty(
                False,
                "",
                "",
                (),
                (),
                (),
                (),
                (),
                admission.state.unresolved_items,
                None,
            ),
            None,
        )
    if admission.action.kind is ResearchActionKind.SEMANTIC_COMMIT and probe_result is not None:
        raise SemanticReducerError("a semantic commit cannot have a probe result")
    result_evidence: tuple[EvidenceRecord, ...] = ()
    result_expectations = ()
    if probe_result is not None:
        created = probe_result_to_evidence(
            probe_result,
            admission.action,
            read_artifact=read_artifact,
        )
        if created is not None:
            result_evidence = (created,)
            result_expectations = derive_result_expectations(
                admission.state,
                admission.action,
                created,
            )
    declared_join_ids = tuple(
        sorted(
            set(admission.declared_join_ids).union(
                item.join_id
                for item in admission.join_candidates
                if item.status is JoinCandidateStatus.VALIDATED
                and item.join_id
                not in {candidate.join_id for candidate in admission.state.join_candidates}
            )
        )
    )
    transition = apply_research_transition(
        admission.state,
        admission.action,
        evidence=result_evidence,
        result_expectations=result_expectations,
        hypotheses=admission.hypotheses,
        bindings=admission.bindings,
        join_candidates=admission.join_candidates,
        budget_state=admission.budget_state,
        declared_join_ids=declared_join_ids,
    )
    return SemanticCommitResult(transition.state, transition.novelty, transition)


class _Resolver:
    def __init__(self, batch: TrustedSemanticBatch) -> None:
        self.tables = {item.logical_table: item.physical_table for item in batch.tables}
        self.columns = {
            (item.logical_table, item.logical_column): item.physical_column
            for item in batch.columns
        }
        self.documents = {item.document_id: item for item in batch.documents}

    def table(self, value: LogicalTableRef) -> TableRef:
        try:
            return self.tables[value.table]
        except KeyError as exc:
            raise SemanticReducerError(
                "logical table has no exact trusted resolution"
            ) from exc

    def column(self, value: LogicalColumnRef) -> ColumnRef:
        try:
            return self.columns[(value.table, value.column)]
        except KeyError as exc:
            raise SemanticReducerError(
                "logical column has no exact trusted resolution"
            ) from exc

    def document(self, document_id: str) -> DocumentRef:
        try:
            return self.documents[document_id]
        except KeyError as exc:
            raise SemanticReducerError(
                "document has no exact trusted resolution"
            ) from exc

    def predicate(self, value: LogicalPredicate) -> PredicateRef:
        return PredicateRef(
            left=self.column(value.left), operator=value.operator, right=value.right
        )


def _stable_id(prefix: str, value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def _target_data(target: TargetRef) -> object:
    return target.model_dump(mode="json", by_alias=True)


def _edge_data(edge: JoinEdge) -> object:
    return edge.model_dump(mode="json", by_alias=True)


def _new_hypothesis(
    proposal: NewHypothesisProposal, resolver: _Resolver, schema: str
) -> Hypothesis:
    targets = tuple(
        _resolve_target(target, resolver) for target in proposal.candidate_targets
    )
    targets = tuple(
        sorted(targets, key=lambda item: json.dumps(_target_data(item), sort_keys=True))
    )
    identity = {
        "schema": schema,
        "source_ids": proposal.source_ids,
        "claim": proposal.claim,
        "targets": [_target_data(item) for item in targets],
    }
    return Hypothesis(
        hypothesis_id=_stable_id("hypothesis", identity),
        source_ids=proposal.source_ids,
        claim=proposal.claim,
        candidate_targets=targets,
        status=HypothesisStatus.PROPOSED,
        evidence_ids=tuple(sorted(proposal.citation_evidence_ids)),
    )


def _new_join(
    proposal: NewJoinProposal,
    resolver: _Resolver,
    schema: str,
) -> JoinCandidate:
    left, right = resolver.column(proposal.left), resolver.column(proposal.right)
    path = tuple(
        JoinEdge(
            left=resolver.column(edge.left),
            right=resolver.column(edge.right),
            join_type=edge.join_type,
        )
        for edge in proposal.path
    )
    if not path:
        path = (JoinEdge(left=left, right=right, join_type=proposal.join_type),)
    endpoints_match = path[0].left == left and path[0].right == right
    if proposal.join_type is JoinType.INNER and _target_key(right) < _target_key(left):
        left, right = right, left
        oriented_path = path if _is_composite_relation(path) else tuple(reversed(path))
        path = tuple(
            JoinEdge(
                left=edge.right,
                right=edge.left,
                join_type=edge.join_type,
            )
            for edge in oriented_path
        )
    identity = {
        "schema": schema,
        "left": _target_data(left),
        "right": _target_data(right),
        "join_type": proposal.join_type.value,
        "path": [_edge_data(edge) for edge in path],
    }
    return JoinCandidate(
        join_id=_stable_id("join", identity),
        left=left,
        right=right,
        join_type=proposal.join_type,
        path=path,
        status=(
            JoinCandidateStatus.VALIDATED
            if endpoints_match
            else JoinCandidateStatus.CANDIDATE
        ),
        evidence_ids=tuple(sorted(proposal.citation_evidence_ids)),
    )


def _is_composite_relation(path: tuple[JoinEdge, ...]) -> bool:
    table_pairs = {
        frozenset((_target_key(edge.left.table), _target_key(edge.right.table)))
        for edge in path
    }
    return len(table_pairs) == 1


def _target_key(target: TargetRef) -> str:
    return json.dumps(
        _target_data(target),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _new_binding(
    proposal: NewBindingProposal,
    resolver: _Resolver,
    joins: Mapping[str, JoinCandidate],
    proposal_ids: Mapping[str, str],
    schema: str,
) -> BindingBase:
    candidate = proposal.candidate
    referenced = tuple(
        _join_reference_id(reference, proposal_ids)
        for reference in proposal.join_references
    )
    try:
        join_path = tuple(
            edge for join_id in referenced for edge in joins[join_id].path
        )
    except KeyError as exc:
        raise SemanticReducerError("binding references an unknown join") from exc
    common = {
        "source_id": proposal.source_id,
        "join_path": join_path,
        "evidence_ids": tuple(sorted(proposal.citation_evidence_ids)),
        "confidence": 0.0,
        "status": BindingStatus.CANDIDATE,
        "validator_rule": None,
    }
    if isinstance(candidate, PhysicalColumnCandidate):
        column = resolver.column(candidate.physical_column)
        payload = {
            "schema": schema,
            "kind": candidate.kind,
            "source": proposal.source_id,
            "column": _target_data(column),
        }
        return PhysicalColumnBinding(
            binding_id=_stable_id("binding", payload),
            tables=(column.table,),
            columns=(column,),
            predicates=(),
            physical_column=column,
            **common,
        )
    if isinstance(candidate, VerticalAttributeCandidate):
        entity_table, catalog_table, value_table = (
            resolver.table(candidate.entity_table),
            resolver.table(candidate.attribute_catalog_table),
            resolver.table(candidate.value_table),
        )
        entity_key, catalog_key = (
            resolver.column(candidate.entity_key),
            resolver.column(candidate.attribute_catalog_key),
        )
        value_entity, value_attribute = (
            resolver.column(candidate.value_entity_key),
            resolver.column(candidate.value_attribute_key),
        )
        name_predicate, value_predicate = (
            resolver.predicate(candidate.attribute_name_predicate),
            resolver.predicate(candidate.value_predicate),
        )
        columns = (
            entity_key,
            catalog_key,
            name_predicate.left,
            value_entity,
            value_attribute,
            value_predicate.left,
        )
        payload = {
            "schema": schema,
            "kind": candidate.kind,
            "source": proposal.source_id,
            "tables": [
                _target_data(item)
                for item in (entity_table, catalog_table, value_table)
            ],
            "columns": [_target_data(item) for item in columns],
            "predicates": [
                name_predicate.model_dump(mode="json"),
                value_predicate.model_dump(mode="json"),
            ],
            "joins": [_edge_data(edge) for edge in join_path],
        }
        return VerticalAttributeBinding(
            binding_id=_stable_id("binding", payload),
            tables=(entity_table, catalog_table, value_table),
            columns=columns,
            predicates=(name_predicate, value_predicate),
            entity_table=entity_table,
            entity_key=entity_key,
            attribute_catalog_table=catalog_table,
            attribute_catalog_key=catalog_key,
            attribute_name_predicate=name_predicate,
            value_table=value_table,
            value_entity_key=value_entity,
            value_attribute_key=value_attribute,
            value_predicate=value_predicate,
            **common,
        )
    if isinstance(candidate, DiscriminatorValueCandidate):
        column = resolver.column(candidate.discriminator_column)
        predicates = (
            resolver.predicate(candidate.discriminator_predicate),
            *(
                resolver.predicate(predicate)
                for predicate in candidate.additional_predicates
            ),
        )
        if predicates[0].left != column:
            raise SemanticReducerError(
                "discriminator predicate must use its discriminator column"
            )
        if len(set(predicates)) != len(predicates):
            raise SemanticReducerError("discriminator predicates must be unique")
        columns = tuple(dict.fromkeys(predicate.left for predicate in predicates))
        tables = tuple(dict.fromkeys(item.table for item in columns))
        payload = {
            "schema": schema,
            "kind": candidate.kind,
            "source": proposal.source_id,
        }
        if len(predicates) == 1:
            payload["column"] = _target_data(column)
            payload["predicate"] = predicates[0].model_dump(mode="json")
        else:
            payload["columns"] = [_target_data(item) for item in columns]
            payload["predicates"] = [
                item.model_dump(mode="json") for item in predicates
            ]
        return DiscriminatorValueBinding(
            binding_id=_stable_id("binding", payload),
            tables=tables,
            columns=columns,
            predicates=predicates,
            discriminator_column=column,
            discriminator_predicate=predicates[0],
            **common,
        )
    if isinstance(candidate, DerivedExpressionCandidate):
        inputs = tuple(resolver.column(item) for item in candidate.input_columns)
        document = resolver.document(candidate.document_id)
        expression = ExpressionRef(
            expression_id=_stable_id(
                "expression",
                {
                    "schema": schema,
                    "claim": candidate.expression_claim,
                    "inputs": [_target_data(item) for item in inputs],
                },
            ),
            expression=candidate.expression_claim,
        )
        payload = {
            "schema": schema,
            "kind": candidate.kind,
            "source": proposal.source_id,
            "expression": expression.model_dump(mode="json"),
            "document": _target_data(document),
            "rule_excerpt": candidate.rule_excerpt,
            "inputs": [_target_data(item) for item in inputs],
        }
        return DerivedExpressionBinding(
            binding_id=_stable_id("binding", payload),
            tables=tuple(
                sorted(
                    {item.table for item in inputs},
                    key=lambda item: json.dumps(_target_data(item), sort_keys=True),
                )
            ),
            columns=inputs,
            predicates=(),
            expression=expression,
            document=document,
            rule_excerpt=candidate.rule_excerpt,
            input_columns=inputs,
            **common,
        )
    if isinstance(candidate, DocumentRuleCandidate):
        document = resolver.document(candidate.document_id)
        rule_id = _stable_id(
            "rule",
            {
                "schema": schema,
                "document": _target_data(document),
                "rule_text": candidate.rule_text,
            },
        )
        payload = {
            "schema": schema,
            "kind": candidate.kind,
            "source": proposal.source_id,
            "document": _target_data(document),
            "rule_id": rule_id,
            "rule_text": candidate.rule_text,
        }
        return DocumentRuleBinding(
            binding_id=_stable_id("binding", payload),
            tables=(),
            columns=(),
            predicates=(),
            document=document,
            rule_id=rule_id,
            rule_text=candidate.rule_text,
            **common,
        )
    raise SemanticReducerError("unsupported binding candidate")


def _resolve_target(value: object, resolver: _Resolver) -> TargetRef:
    kind = getattr(value, "target_kind", None)
    if kind == "table":
        return resolver.table(LogicalTableRef(table=value.table))
    if kind == "column":
        return resolver.column(LogicalColumnRef(table=value.table, column=value.column))
    if kind == "document":
        return resolver.document(value.document_id)
    raise SemanticReducerError("unknown logical target")


def _join_reference_id(reference: object, proposal_ids: Mapping[str, str]) -> str:
    if isinstance(reference, ExistingJoinRef):
        return reference.join_id
    key = getattr(reference, "proposal_key", None)
    identifier = proposal_ids.get(key)
    if identifier is None:
        raise SemanticReducerError("new binding join reference is unresolved")
    return identifier


def _existing_id(reference: object, label: str) -> str:
    if getattr(reference, "reference_kind", None) != "existing":
        raise SemanticReducerError(
            f"new {label} cannot be assessed in the same decision"
        )
    field = f"{label}_id"
    return getattr(reference, field)


def _fresh_evidence(
    records: Mapping[str, EvidenceRecord], context: FreshnessContext
) -> dict[str, EvidenceRecord]:
    return {
        key: item
        for key, item in records.items()
        if evaluate_evidence_freshness(item, context).status is FreshnessStatus.FRESH
    }


def _cited_records(
    ids: tuple[str, ...],
    records: Mapping[str, EvidenceRecord],
    fresh: Mapping[str, EvidenceRecord],
) -> tuple[EvidenceRecord, ...]:
    if not ids:
        raise SemanticReducerError("assessment requires cited evidence")
    result: list[EvidenceRecord] = []
    for evidence_id in ids:
        if evidence_id not in records:
            raise SemanticReducerError(
                "citation does not exist in prior state evidence"
            )
        if evidence_id not in fresh:
            raise SemanticReducerError("assessment evidence is not fresh")
        if read_evidence_provenance(records[evidence_id]) is None:
            raise SemanticReducerError(
                "assessment evidence lacks trusted probe provenance"
            )
        result.append(records[evidence_id])
    return tuple(result)


def _payload(record: EvidenceRecord) -> tuple[object, object]:
    observation = parse_probe_observation(record.observation)
    if observation is None:
        raise SemanticReducerError("semantic certificate requires v1 probe observation")
    return observation.provenance, observation.payload


def _observation_truncated(record: EvidenceRecord) -> bool:
    observation = parse_probe_observation(record.observation)
    if observation is None:
        raise SemanticReducerError("semantic certificate requires v1 probe observation")
    return observation.truncated


def _matched(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("status") == "matched"


def _append_evidence(
    previous: tuple[str, ...], cited: tuple[EvidenceRecord, ...]
) -> tuple[str, ...]:
    return previous + tuple(
        item.evidence_id for item in cited if item.evidence_id not in previous
    )


def _assess_hypothesis(
    item: Hypothesis, certificate: str, cited: tuple[EvidenceRecord, ...]
) -> Hypothesis:
    if certificate == "insufficient":
        return item
    evidence_ids = _append_evidence(item.evidence_ids, cited)
    if certificate == "consistent" and any(
        record.target in item.candidate_targets and _matched(_payload(record)[1])
        for record in cited
    ):
        return item.model_copy(
            update={"status": HypothesisStatus.SUPPORTED, "evidence_ids": evidence_ids}
        )
    if certificate == "contradicted" and _negative_hypothesis_certificate(item, cited):
        return item.model_copy(
            update={"status": HypothesisStatus.REJECTED, "evidence_ids": evidence_ids}
        )
    raise SemanticReducerError(
        "hypothesis assessment lacks the required exact certificate"
    )


def _negative_hypothesis_certificate(
    item: Hypothesis, cited: tuple[EvidenceRecord, ...]
) -> bool:
    missing = set(item.candidate_targets)
    for record in cited:
        provenance, payload = _payload(record)
        if (
            provenance.probe_kind is ResearchActionKind.READ_DOCUMENT
            and not _observation_truncated(record)
        ):
            document = payload.get("document") if isinstance(payload, dict) else None
            if (
                isinstance(document, dict)
                and isinstance(document.get("content"), str)
                and item.claim not in document["content"]
            ):
                return True
        if (
            record.target in missing
            and isinstance(payload, dict)
            and payload.get("status") == "missing"
            and not _observation_truncated(record)
        ):
            missing.discard(record.target)
    return not missing


def _assess_binding(
    item: BindingBase,
    certificate: str,
    cited: tuple[EvidenceRecord, ...],
    joins: Mapping[str, JoinCandidate],
    *,
    schema_columns: tuple[ColumnRef, ...] = (),
) -> BindingBase:
    if certificate == "insufficient":
        return item
    if certificate == "contradicted":
        # No sampled/top-k absence is ever a rejection certificate.
        raise SemanticReducerError("binding rejection has no permitted certificate")
    kind = item.kind
    valid = (
        (
            isinstance(item, PhysicalColumnBinding)
            and _physical_column_certificate(item, cited, schema_columns)
        )
        or (
            isinstance(item, DiscriminatorValueBinding)
            and _discriminator_certificate(
                item,
                cited,
                schema_columns=schema_columns,
            )
        )
        or (
            isinstance(item, DerivedExpressionBinding)
            and _derived_expression_certificate(
                item,
                cited,
                schema_columns=schema_columns,
            )
        )
        or (
            isinstance(item, DocumentRuleBinding)
            and _document_rule_certificate(item, cited)
        )
        or (
            isinstance(item, VerticalAttributeBinding)
            and _vertical_certificate(
                item,
                cited,
                joins,
                schema_columns=schema_columns,
            )
        )
    )
    if not valid:
        raise SemanticReducerError(
            "binding assessment lacks the required semantic certificate"
        )
    return item.model_copy(
        update={
            "status": BindingStatus.SUPPORTED,
            "confidence": 1.0,
            "validator_rule": f"semantic-certificate:v1:{kind}",
            "evidence_ids": _append_evidence(item.evidence_ids, cited),
        }
    )


def _exact_column(record: EvidenceRecord, column: ColumnRef) -> bool:
    try:
        return evidence_observes_exact_column(record, column)
    except ExactValueCertificateError:
        raise SemanticReducerError(
            "semantic certificate requires v1 probe observation"
        ) from None


def _physical_column_certificate(
    item: PhysicalColumnBinding,
    cited: tuple[EvidenceRecord, ...],
    schema_columns: tuple[ColumnRef, ...] = (),
) -> bool:
    return (
        item.physical_column in schema_columns
        or any(_exact_column(record, item.physical_column) for record in cited)
    )


def _discriminator_certificate(
    item: DiscriminatorValueBinding,
    cited: tuple[EvidenceRecord, ...],
    *,
    schema_columns: tuple[ColumnRef, ...] = (),
) -> bool:
    return all(
        _schema_or_evidence_column(
            predicate.left,
            cited,
            schema_columns,
        )
        and predicate_has_valid_literal(predicate)
        for predicate in item.predicates
    )


def _document_payload(
    record: EvidenceRecord, document: DocumentRef
) -> dict[str, object] | None:
    provenance, payload = _payload(record)
    if (
        provenance.probe_kind is not ResearchActionKind.READ_DOCUMENT
        or _observation_truncated(record)
        or record.target != document
    ):
        return None
    raw = payload.get("document") if isinstance(payload, dict) else None
    return raw if isinstance(raw, dict) else None


def _derived_expression_certificate(
    item: DerivedExpressionBinding,
    cited: tuple[EvidenceRecord, ...],
    *,
    schema_columns: tuple[ColumnRef, ...] = (),
) -> bool:
    if item.document is None or item.rule_excerpt is None:
        return False
    if not all(
        _schema_or_evidence_column(column, cited, schema_columns)
        for column in item.input_columns
    ):
        return False
    return any(
        isinstance(raw.get("content"), str)
        and item.rule_excerpt in raw["content"]
        for record in cited
        for raw in [_document_payload(record, item.document)]
        if raw is not None
    )


def derived_expression_certificate(
    item: DerivedExpressionBinding,
    cited: tuple[EvidenceRecord, ...],
    *,
    schema_columns: tuple[ColumnRef, ...] = (),
) -> bool:
    """Return whether one formula binding has its exact document authority."""

    return _derived_expression_certificate(
        item,
        cited,
        schema_columns=schema_columns,
    )


def _document_rule_certificate(
    item: DocumentRuleBinding, cited: tuple[EvidenceRecord, ...]
) -> bool:
    return any(
        isinstance(raw.get("content"), str)
        and _normalized_document_text(raw["content"])
        == _normalized_document_text(item.rule_text)
        for record in cited
        for raw in [_document_payload(record, item.document)]
        if raw is not None
    )


def _normalized_document_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _vertical_certificate(
    item: VerticalAttributeBinding,
    cited: tuple[EvidenceRecord, ...],
    joins: Mapping[str, JoinCandidate],
    *,
    schema_columns: tuple[ColumnRef, ...] = (),
) -> bool:
    if (
        item.attribute_name_predicate.operator
        not in {PredicateOperator.EQ, PredicateOperator.IN, PredicateOperator.IS_NULL}
        or item.value_predicate.operator
        not in {PredicateOperator.EQ, PredicateOperator.IN, PredicateOperator.IS_NULL}
    ):
        return False
    facts = (
        item.entity_key,
        item.attribute_catalog_key,
        item.attribute_name_predicate.left,
        item.value_entity_key,
        item.value_attribute_key,
        item.value_predicate.left,
    )
    if not all(
        _schema_or_evidence_column(column, cited, schema_columns) for column in facts
    ):
        return False
    available_evidence = {
        *item.evidence_ids,
        *(record.evidence_id for record in cited),
    }
    validated = [
        join
        for join in joins.values()
        if join.status is JoinCandidateStatus.VALIDATED
        and join.evidence_ids
        and set(join.evidence_ids).issubset(available_evidence)
        and _path_is_within(join.path, item.join_path)
    ]
    entity_joins = [
        join
        for join in validated
        if _join_has_pair(join, item.entity_key, item.value_entity_key)
    ]
    attribute_joins = [
        join
        for join in validated
        if _join_has_pair(
            join,
            item.attribute_catalog_key,
            item.value_attribute_key,
        )
    ]
    if not any(
        entity.join_id != attribute.join_id
        for entity in entity_joins
        for attribute in attribute_joins
    ):
        return False
    return _positive_predicate_certificate(
        item.attribute_name_predicate,
        cited,
    ) and _positive_predicate_certificate(
        item.value_predicate,
        cited,
    )


def _path_is_within(
    required: tuple[JoinEdge, ...],
    footprint: tuple[JoinEdge, ...],
) -> bool:
    if not required or len(required) > len(footprint):
        return False
    return any(
        footprint[start : start + len(required)] == required
        for start in range(len(footprint) - len(required) + 1)
    )


def _join_has_pair(join: JoinCandidate, first: ColumnRef, second: ColumnRef) -> bool:
    return any({edge.left, edge.right} == {first, second} for edge in join.path)


def _positive_predicate_certificate(
    predicate: PredicateRef,
    cited: tuple[EvidenceRecord, ...],
) -> bool:
    try:
        return predicate_has_exact_value_certificate(predicate, cited)
    except ExactValueCertificateError:
        raise SemanticReducerError(
            "semantic certificate requires v1 probe observation"
        ) from None


def _schema_or_evidence_column(
    column: ColumnRef,
    cited: tuple[EvidenceRecord, ...],
    schema_columns: tuple[ColumnRef, ...],
) -> bool:
    return column in schema_columns or any(
        _exact_column(record, column) for record in cited
    )


def _evidence_observes_exact_value(
    record: EvidenceRecord,
    column: ColumnRef,
    value: object,
) -> bool:
    try:
        return evidence_observes_exact_value(record, column, value)
    except ExactValueCertificateError:
        raise SemanticReducerError(
            "semantic certificate requires v1 probe observation"
        ) from None


def _assess_join(
    item: JoinCandidate,
    certificate: str,
    cited: tuple[EvidenceRecord, ...],
    *,
    declared_join_ids: tuple[Id, ...] = (),
) -> JoinCandidate:
    if certificate == "insufficient":
        return item
    if certificate == "consistent" and (
        item.join_id in declared_join_ids
        or any(_declared_join_certificate(item, record) for record in cited)
    ):
        return item.model_copy(
            update={
                "status": JoinCandidateStatus.VALIDATED,
                "evidence_ids": _append_evidence(item.evidence_ids, cited),
            }
        )
    if certificate == "contradicted" and any(
        _missing_direct_hop_certificate(item, record) for record in cited
    ):
        return item.model_copy(
            update={
                "status": JoinCandidateStatus.REJECTED,
                "evidence_ids": _append_evidence(item.evidence_ids, cited),
            }
        )
    raise SemanticReducerError(
        "join assessment lacks the required declared relationship certificate"
    )


def _declared_join_certificate(item: JoinCandidate, record: EvidenceRecord) -> bool:
    provenance, payload = _payload(record)
    if provenance.probe_kind is not ResearchActionKind.INSPECT_RELATIONSHIPS:
        return False
    if not isinstance(payload, dict) or not isinstance(
        payload.get("relationships"), list
    ):
        return False
    expected = [(edge.left.column, edge.right.column) for edge in item.path]
    reverse_expected = [(edge.right.column, edge.left.column) for edge in item.path]
    for relationship in payload["relationships"]:
        if (
            not isinstance(relationship, dict)
            or relationship.get("relationship_kind") != "declared"
        ):
            continue
        pairs = relationship.get("column_pairs")
        if not isinstance(pairs, list):
            continue
        actual = [
            (pair.get("from_column"), pair.get("to_column"))
            for pair in pairs
            if isinstance(pair, dict)
        ]
        forward = (
            actual == expected
            and relationship.get("from_table")
            == _qualified_table(item.path[0].left.table)
            and relationship.get("to_table")
            == _qualified_table(item.path[0].right.table)
            and all(
                edge.left.table == item.path[0].left.table
                and edge.right.table == item.path[0].right.table
                for edge in item.path
            )
        )
        reverse = (
            item.join_type is JoinType.INNER
            and actual == reverse_expected
            and relationship.get("from_table")
            == _qualified_table(item.path[0].right.table)
            and relationship.get("to_table")
            == _qualified_table(item.path[0].left.table)
            and all(
                edge.right.table == item.path[0].right.table
                and edge.left.table == item.path[0].left.table
                for edge in item.path
            )
        )
        if len(pairs) == len(expected) and (forward or reverse):
            return True
    return False


def _qualified_table(table: TableRef) -> str:
    return (
        table.table
        if table.schema_name is None
        else f"{table.schema_name}.{table.table}"
    )


def _missing_direct_hop_certificate(
    item: JoinCandidate, record: EvidenceRecord
) -> bool:
    provenance, payload = _payload(record)
    if (
        provenance.probe_kind is not ResearchActionKind.INSPECT_RELATIONSHIPS
        or _observation_truncated(record)
        or not isinstance(payload, dict)
    ):
        return False
    if payload.get("depth") != 1 or not isinstance(payload.get("relationships"), list):
        return False
    return not _declared_join_certificate(item, record)


def _action_for_tool_turn(
    next_step: ToolIntent,
    claim: TrustedToolClaim,
    proposal_ids: Mapping[str, str],
    state: ResearchState,
) -> ResearchAction:
    tool_name = next_step.intent.tool_name
    if claim.tool_name != tool_name:
        raise SemanticReducerError(
            "trusted tool claim name does not match decision intent"
        )
    try:
        kind = _TOOL_KINDS[tool_name]
    except KeyError as exc:
        raise SemanticReducerError("decision tool is not a research action") from exc
    hypothesis_id = None
    if next_step.hypothesis_ref is not None:
        if isinstance(next_step.hypothesis_ref, ExistingHypothesisRef):
            hypothesis_id = next_step.hypothesis_ref.hypothesis_id
        else:
            hypothesis_id = proposal_ids.get(next_step.hypothesis_ref.proposal_key)
            if hypothesis_id is None:
                raise SemanticReducerError("tool hypothesis reference is unresolved")
    digest = canonical_action_digest(
        kind=kind,
        hypothesis_id=hypothesis_id,
        target=claim.target,
        parameters=claim.parameters,
        expected_revision=state.revision,
    )
    return ResearchAction(
        action_id=claim.action_id,
        kind=kind,
        hypothesis_id=hypothesis_id,
        target=claim.target,
        parameters=claim.parameters,
        action_digest=digest,
        expected_revision=state.revision,
    )


def _action_for_semantic_commit(
    decision: ResearchDecisionV1,
    state: ResearchState,
) -> ResearchAction:
    digest = canonical_action_digest(
        kind=ResearchActionKind.SEMANTIC_COMMIT,
        hypothesis_id=None,
        target=None,
        parameters=(),
        expected_revision=state.revision,
    )
    identity = json.dumps(
        {
            "run_id": state.run_id,
            "run_incarnation": state.run_incarnation,
            "revision": state.revision,
            "schema_namespace_version": state.schema_namespace_version,
            "decision": decision.model_dump(mode="json", by_alias=True),
            "action_digest": digest,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ResearchAction(
        action_id="semantic:" + hashlib.sha256(identity).hexdigest(),
        kind=ResearchActionKind.SEMANTIC_COMMIT,
        hypothesis_id=None,
        target=None,
        parameters=(),
        action_digest=digest,
        expected_revision=state.revision,
    )


__all__ = [
    "ResolvedColumn",
    "ResolvedTable",
    "SemanticCommitResult",
    "SemanticReducerError",
    "SemanticTurnAdmission",
    "TrustedSemanticBatch",
    "TrustedToolClaim",
    "admit_semantic_turn",
    "commit_semantic_turn",
]
