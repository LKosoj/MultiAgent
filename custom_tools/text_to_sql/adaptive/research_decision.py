"""Strict transient semantic decisions produced by the research model.

The models in this module are proposals, not durable research state.  They
intentionally contain no trusted runtime identity, persistent IDs, statuses,
action digests, evidence objects, or terminal result.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Literal, TypeAlias, TypeVar

from pydantic import Field, field_validator, model_validator

from .models import (
    Id,
    JoinType,
    PredicateOperator,
    StrictModel,
)
from .ambiguity import AmbiguityReport
from .serialization import _register_internal_decode_models
from .research_tool_contracts import (
    ExecuteResearchProbeArguments,
    GetDistinctValuesArguments,
    InspectColumnArguments,
    InspectRelationshipsArguments,
    InspectTableArguments,
    ProfileColumnArguments,
    ReadSchemaEvidenceArguments,
    SampleRowsArguments,
    SearchSchemaCatalogArguments,
    SearchValueArguments,
)


DECISION_VERSION = 1
MAX_RESEARCH_DECISION_BYTES = 256 * 1024
MAX_DECISION_PROPOSALS = 32
MAX_SOURCE_IDS = 16
MAX_EVIDENCE_CITATIONS = 32
MAX_CANDIDATE_TARGETS = 16
MAX_JOIN_REFERENCES = 8
MAX_JOIN_PATH_EDGES = 8
MAX_PREDICATE_VALUES = 50

ProposalKey: TypeAlias = Annotated[
    str,
    Field(
        min_length=10,
        max_length=128,
        pattern=r"^proposal:[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
LogicalName: TypeAlias = Annotated[str, Field(min_length=1, max_length=65_536)]
SemanticText: TypeAlias = Annotated[str, Field(min_length=1, max_length=4_096)]
SourceIds: TypeAlias = Annotated[
    tuple[Id, ...],
    Field(min_length=1, max_length=MAX_SOURCE_IDS),
]
CitationEvidenceIds: TypeAlias = Annotated[
    tuple[Id, ...],
    Field(min_length=1, max_length=MAX_EVIDENCE_CITATIONS),
]
JsonScalar: TypeAlias = str | int | float | bool
PredicateRight: TypeAlias = (
    JsonScalar
    | Annotated[
        tuple[JsonScalar, ...],
        Field(min_length=1, max_length=MAX_PREDICATE_VALUES),
    ]
    | None
)
Certificate: TypeAlias = Literal["consistent", "contradicted", "insufficient"]
_ModelT = TypeVar("_ModelT", bound=StrictModel)


def _canonical_key(value: StrictModel) -> str:
    return json.dumps(
        value.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _proposal_sort_key(value: DecisionProposal) -> tuple[str, str]:
    identity = getattr(value, "proposal_key", None)
    if identity is None:
        identity = _canonical_key(value.subject)
    return value.proposal_type, identity


def _semantic_proposal_key(value: StrictModel) -> str:
    semantic = value.model_dump(mode="json", by_alias=True)
    semantic.pop("proposal_key", None)
    semantic.pop("citation_evidence_ids", None)
    return json.dumps(
        semantic,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_scalar_key(value: JsonScalar) -> str:
    if type(value) is float and not math.isfinite(value):
        raise ValueError("predicate values must be finite JSON scalars")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sorted_unique_ids(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return tuple(sorted(values))


def _sorted_unique_models(
    values: tuple[_ModelT, ...],
    label: str,
) -> tuple[_ModelT, ...]:
    keyed = [(_canonical_key(value), value) for value in values]
    keys = [key for key, _ in keyed]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must be unique")
    return tuple(value for _, value in sorted(keyed, key=lambda item: item[0]))


def _unique_models_in_order(
    values: tuple[_ModelT, ...],
    label: str,
) -> tuple[_ModelT, ...]:
    keys = [_canonical_key(value) for value in values]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must be unique")
    return values


class LogicalTableRef(StrictModel):
    table: LogicalName


class LogicalColumnRef(StrictModel):
    table: LogicalName
    column: LogicalName


class LogicalTableTarget(StrictModel):
    target_kind: Literal["table"] = "table"
    table: LogicalName


class LogicalColumnTarget(StrictModel):
    target_kind: Literal["column"] = "column"
    table: LogicalName
    column: LogicalName


class LogicalDocumentTarget(StrictModel):
    target_kind: Literal["document"] = "document"
    document_id: Id


LogicalTarget: TypeAlias = Annotated[
    LogicalTableTarget | LogicalColumnTarget | LogicalDocumentTarget,
    Field(discriminator="target_kind"),
]


class LogicalPredicate(StrictModel):
    left: LogicalColumnRef
    operator: PredicateOperator
    right: PredicateRight

    @model_validator(mode="after")
    def validate_right(self) -> LogicalPredicate:
        null_operators = {
            PredicateOperator.IS_NULL,
            PredicateOperator.IS_NOT_NULL,
        }
        set_operators = {PredicateOperator.IN, PredicateOperator.NOT_IN}
        if self.operator in null_operators:
            if self.right is not None:
                raise ValueError("null predicates require right=null")
            return self
        if self.right is None:
            raise ValueError("non-null predicates require right")

        if self.operator in set_operators:
            if not isinstance(self.right, tuple):
                raise ValueError("in/not_in predicates require an array")
            keyed = [(_canonical_scalar_key(value), value) for value in self.right]
            keys = [key for key, _ in keyed]
            if len(keys) != len(set(keys)):
                raise ValueError("in/not_in predicate values must be unique")
            ordered = tuple(
                value for _, value in sorted(keyed, key=lambda item: item[0])
            )
            return self.model_copy(update={"right": ordered})

        if self.operator is PredicateOperator.BETWEEN:
            if not isinstance(self.right, tuple) or len(self.right) != 2:
                raise ValueError(
                    "between predicates require exactly two ordered values"
                )
            values = self.right
        else:
            if isinstance(self.right, tuple):
                raise ValueError("this predicate requires one scalar value")
            if self.operator is PredicateOperator.LIKE and type(self.right) is not str:
                raise ValueError("like predicates require text")
            values = (self.right,)

        for value in values:
            _canonical_scalar_key(value)
        return self


class LogicalJoinEdge(StrictModel):
    left: LogicalColumnRef
    right: LogicalColumnRef
    operator: Literal[PredicateOperator.EQ] = PredicateOperator.EQ
    join_type: JoinType


class ExistingHypothesisRef(StrictModel):
    reference_kind: Literal["existing"] = "existing"
    hypothesis_id: Id


class ProposedHypothesisRef(StrictModel):
    reference_kind: Literal["proposed"] = "proposed"
    proposal_key: ProposalKey


HypothesisReference: TypeAlias = Annotated[
    ExistingHypothesisRef | ProposedHypothesisRef,
    Field(discriminator="reference_kind"),
]


class ExistingBindingRef(StrictModel):
    reference_kind: Literal["existing"] = "existing"
    binding_id: Id


class ProposedBindingRef(StrictModel):
    reference_kind: Literal["proposed"] = "proposed"
    proposal_key: ProposalKey


BindingReference: TypeAlias = Annotated[
    ExistingBindingRef | ProposedBindingRef,
    Field(discriminator="reference_kind"),
]


class ExistingJoinRef(StrictModel):
    reference_kind: Literal["existing"] = "existing"
    join_id: Id


class ProposedJoinRef(StrictModel):
    reference_kind: Literal["proposed"] = "proposed"
    proposal_key: ProposalKey


JoinReference: TypeAlias = Annotated[
    ExistingJoinRef | ProposedJoinRef,
    Field(discriminator="reference_kind"),
]


class PhysicalColumnCandidate(StrictModel):
    kind: Literal["physical_column"] = "physical_column"
    physical_column: LogicalColumnRef


class VerticalAttributeCandidate(StrictModel):
    kind: Literal["vertical_attribute"] = "vertical_attribute"
    entity_table: LogicalTableRef
    entity_key: LogicalColumnRef
    attribute_catalog_table: LogicalTableRef
    attribute_catalog_key: LogicalColumnRef
    attribute_name_predicate: LogicalPredicate
    value_table: LogicalTableRef
    value_entity_key: LogicalColumnRef
    value_attribute_key: LogicalColumnRef
    value_predicate: LogicalPredicate


class DiscriminatorValueCandidate(StrictModel):
    kind: Literal["discriminator_value"] = "discriminator_value"
    discriminator_column: LogicalColumnRef
    discriminator_predicate: LogicalPredicate
    additional_predicates: tuple[LogicalPredicate, ...] = ()

    @field_validator("additional_predicates")
    @classmethod
    def normalize_additional_predicates(
        cls,
        value: tuple[LogicalPredicate, ...],
    ) -> tuple[LogicalPredicate, ...]:
        return _sorted_unique_models(value, "additional_predicates")


class DerivedExpressionCandidate(StrictModel):
    """Untrusted semantic claim; it is not executable SQL at this boundary."""

    kind: Literal["derived_expression"] = "derived_expression"
    expression_claim: SemanticText = Field(
        description=(
            "Untrusted semantic claim preserved as text; decision resolution "
            "does not admit or execute it."
        )
    )
    document_id: Id
    rule_excerpt: SemanticText
    input_columns: Annotated[
        tuple[LogicalColumnRef, ...],
        Field(min_length=1),
    ]

    @field_validator("input_columns")
    @classmethod
    def validate_input_columns(
        cls,
        value: tuple[LogicalColumnRef, ...],
    ) -> tuple[LogicalColumnRef, ...]:
        return _unique_models_in_order(value, "input_columns")


class DocumentRuleCandidate(StrictModel):
    kind: Literal["document_rule"] = "document_rule"
    document_id: Id
    rule_id: Id
    rule_text: SemanticText


CandidateBinding: TypeAlias = Annotated[
    PhysicalColumnCandidate
    | VerticalAttributeCandidate
    | DiscriminatorValueCandidate
    | DerivedExpressionCandidate
    | DocumentRuleCandidate,
    Field(discriminator="kind"),
]


class _CitedProposal(StrictModel):
    citation_evidence_ids: CitationEvidenceIds

    @field_validator("citation_evidence_ids")
    @classmethod
    def normalize_citations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_ids(value, "citation_evidence_ids")


class NewHypothesisProposal(_CitedProposal):
    proposal_type: Literal["new_hypothesis"] = "new_hypothesis"
    proposal_key: ProposalKey
    source_ids: SourceIds
    claim: SemanticText
    candidate_targets: Annotated[
        tuple[LogicalTarget, ...],
        Field(min_length=1, max_length=MAX_CANDIDATE_TARGETS),
    ]

    @field_validator("source_ids")
    @classmethod
    def normalize_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_ids(value, "source_ids")

    @field_validator("candidate_targets")
    @classmethod
    def normalize_candidate_targets(
        cls,
        value: tuple[LogicalTarget, ...],
    ) -> tuple[LogicalTarget, ...]:
        return _sorted_unique_models(value, "candidate_targets")


class HypothesisAssessment(_CitedProposal):
    proposal_type: Literal["hypothesis_assessment"] = "hypothesis_assessment"
    subject: HypothesisReference
    certificate: Certificate


class NewBindingProposal(_CitedProposal):
    proposal_type: Literal["new_binding"] = "new_binding"
    proposal_key: ProposalKey
    source_id: Id
    candidate: CandidateBinding
    join_references: Annotated[
        tuple[JoinReference, ...],
        Field(max_length=MAX_JOIN_REFERENCES),
    ]

    @field_validator("join_references")
    @classmethod
    def validate_join_references(
        cls,
        value: tuple[JoinReference, ...],
    ) -> tuple[JoinReference, ...]:
        keys = [_canonical_key(item) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("join_references must be unique")
        return value


class BindingAssessment(_CitedProposal):
    proposal_type: Literal["binding_assessment"] = "binding_assessment"
    subject: BindingReference
    certificate: Certificate


class NewJoinProposal(_CitedProposal):
    proposal_type: Literal["new_join"] = "new_join"
    proposal_key: ProposalKey
    left: LogicalColumnRef
    right: LogicalColumnRef
    join_type: JoinType
    path: Annotated[
        tuple[LogicalJoinEdge, ...],
        Field(max_length=MAX_JOIN_PATH_EDGES),
    ]


class JoinAssessment(_CitedProposal):
    proposal_type: Literal["join_assessment"] = "join_assessment"
    subject: JoinReference
    certificate: Certificate


DecisionProposal: TypeAlias = Annotated[
    NewHypothesisProposal
    | HypothesisAssessment
    | NewBindingProposal
    | BindingAssessment
    | NewJoinProposal
    | JoinAssessment,
    Field(discriminator="proposal_type"),
]


class SearchSchemaCatalogIntent(StrictModel):
    tool_name: Literal["search_schema_catalog"] = "search_schema_catalog"
    arguments: SearchSchemaCatalogArguments


class InspectTableIntent(StrictModel):
    tool_name: Literal["inspect_table"] = "inspect_table"
    arguments: InspectTableArguments


class InspectColumnIntent(StrictModel):
    tool_name: Literal["inspect_column"] = "inspect_column"
    arguments: InspectColumnArguments


class InspectRelationshipsIntent(StrictModel):
    tool_name: Literal["inspect_relationships"] = "inspect_relationships"
    arguments: InspectRelationshipsArguments


class ProfileColumnIntent(StrictModel):
    tool_name: Literal["profile_column"] = "profile_column"
    arguments: ProfileColumnArguments


class SampleRowsIntent(StrictModel):
    tool_name: Literal["sample_rows"] = "sample_rows"
    arguments: SampleRowsArguments


class SearchValueIntent(StrictModel):
    tool_name: Literal["search_value"] = "search_value"
    arguments: SearchValueArguments


class GetDistinctValuesIntent(StrictModel):
    tool_name: Literal["get_distinct_values"] = "get_distinct_values"
    arguments: GetDistinctValuesArguments


class ExecuteResearchProbeIntent(StrictModel):
    tool_name: Literal["execute_research_probe"] = "execute_research_probe"
    arguments: ExecuteResearchProbeArguments


class ReadSchemaEvidenceIntent(StrictModel):
    tool_name: Literal["read_schema_evidence"] = "read_schema_evidence"
    arguments: ReadSchemaEvidenceArguments


TypedToolIntent: TypeAlias = Annotated[
    SearchSchemaCatalogIntent
    | InspectTableIntent
    | InspectColumnIntent
    | InspectRelationshipsIntent
    | ProfileColumnIntent
    | SampleRowsIntent
    | SearchValueIntent
    | GetDistinctValuesIntent
    | ExecuteResearchProbeIntent
    | ReadSchemaEvidenceIntent,
    Field(discriminator="tool_name"),
]


class ToolIntent(StrictModel):
    next_kind: Literal["tool"] = "tool"
    hypothesis_ref: HypothesisReference | None
    intent: TypedToolIntent


class StopRequest(StrictModel):
    next_kind: Literal["stop"] = "stop"
    reason: Literal["complete", "ambiguous", "unsupported"]
    source_ids: Annotated[tuple[Id, ...], Field(max_length=MAX_SOURCE_IDS)]
    citation_evidence_ids: CitationEvidenceIds
    ambiguity: AmbiguityReport | None = None

    @field_validator("source_ids", mode="before")
    @classmethod
    def normalize_source_ids(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, list):
            value = tuple(value)
        return _sorted_unique_ids(value, "source_ids")

    @field_validator("citation_evidence_ids", mode="before")
    @classmethod
    def normalize_citations(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, list):
            value = tuple(value)
        return _sorted_unique_ids(value, "citation_evidence_ids")

    @model_validator(mode="after")
    def require_affected_source(self) -> StopRequest:
        if self.reason in {"ambiguous", "unsupported"} and not self.source_ids:
            raise ValueError("ambiguous/unsupported stop requires source_ids")
        if (self.reason == "ambiguous") != (self.ambiguity is not None):
            raise ValueError("ambiguity report is required only for ambiguous stop")
        if (
            self.ambiguity is not None
            and self.ambiguity.citation_evidence_ids != self.citation_evidence_ids
        ):
            raise ValueError("ambiguity citations must equal stop citations")
        return self


class SemanticCommitRequest(StrictModel):
    """Request one reducer-only revision for an already validated proposal batch."""

    next_kind: Literal["semantic_commit"] = "semantic_commit"


NextRequest: TypeAlias = Annotated[
    ToolIntent | StopRequest | SemanticCommitRequest,
    Field(discriminator="next_kind"),
]


class ResearchDecisionV1(StrictModel):
    """One transient semantic proposal batch and exactly one next request."""

    decision_version: Literal[1] = DECISION_VERSION
    proposals: Annotated[
        tuple[DecisionProposal, ...],
        Field(max_length=MAX_DECISION_PROPOSALS),
    ]
    next: NextRequest

    @model_validator(mode="after")
    def validate_and_normalize_references(self) -> ResearchDecisionV1:
        if isinstance(self.next, SemanticCommitRequest) and not self.proposals:
            raise ValueError("semantic_commit requires a nonempty proposal batch")
        local_types: dict[str, str] = {}
        semantic_proposals: set[tuple[str, str]] = set()
        for proposal in self.proposals:
            proposal_key = getattr(proposal, "proposal_key", None)
            if proposal_key is None:
                continue
            if proposal_key in local_types:
                raise ValueError("proposal_key must be unique within one decision")
            local_types[proposal_key] = {
                "new_hypothesis": "hypothesis",
                "new_binding": "binding",
                "new_join": "join",
            }[proposal.proposal_type]
            semantic_identity = (
                proposal.proposal_type,
                _semantic_proposal_key(proposal),
            )
            if semantic_identity in semantic_proposals:
                raise ValueError("new semantic proposals must be unique")
            semantic_proposals.add(semantic_identity)

        assessment_subjects: set[tuple[str, str]] = set()
        for proposal in self.proposals:
            if isinstance(proposal, HypothesisAssessment):
                self._validate_subject(
                    proposal.subject,
                    "hypothesis",
                    local_types,
                )
                self._add_assessment_subject(
                    assessment_subjects,
                    "hypothesis",
                    proposal.subject,
                )
            elif isinstance(proposal, BindingAssessment):
                self._validate_subject(proposal.subject, "binding", local_types)
                self._add_assessment_subject(
                    assessment_subjects,
                    "binding",
                    proposal.subject,
                )
            elif isinstance(proposal, JoinAssessment):
                self._validate_subject(proposal.subject, "join", local_types)
                self._add_assessment_subject(
                    assessment_subjects,
                    "join",
                    proposal.subject,
                )
            elif isinstance(proposal, NewBindingProposal):
                for reference in proposal.join_references:
                    self._validate_subject(reference, "join", local_types)

        if isinstance(self.next, ToolIntent) and self.next.hypothesis_ref is not None:
            self._validate_subject(
                self.next.hypothesis_ref,
                "hypothesis",
                local_types,
            )

        ordered = tuple(sorted(self.proposals, key=_proposal_sort_key))
        return self.model_copy(update={"proposals": ordered})

    @staticmethod
    def _validate_subject(
        reference: StrictModel,
        expected_type: str,
        local_types: dict[str, str],
    ) -> None:
        if getattr(reference, "reference_kind", None) != "proposed":
            return
        proposal_key = getattr(reference, "proposal_key")
        if local_types.get(proposal_key) != expected_type:
            raise ValueError(
                "local proposal reference is missing or has the wrong type"
            )

    @staticmethod
    def _add_assessment_subject(
        seen: set[tuple[str, str]],
        expected_type: str,
        reference: StrictModel,
    ) -> None:
        identity = (expected_type, _canonical_key(reference))
        if identity in seen:
            raise ValueError("an object can have only one assessment per decision")
        seen.add(identity)


_register_internal_decode_models(ResearchDecisionV1)


def parse_research_decision(payload: bytes | str) -> ResearchDecisionV1:
    """Parse raw JSON while duplicate object keys are still observable."""

    from .serialization import SerializationLimits, deserialize_as

    return deserialize_as(
        payload,
        ResearchDecisionV1,
        limits=SerializationLimits(
            max_state_bytes=MAX_RESEARCH_DECISION_BYTES,
            max_inline_rows=0,
        ),
    )
