"""Строгие неизменяемые контракты состояния adaptive Text-to-SQL.

Этот модуль намеренно не содержит сериализацию, reducer или runtime-интеграцию.
Он фиксирует только допустимую форму фактов, с которыми будут работать
последующие этапы pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
import json
import math
from typing import Annotated, Literal, TypeAlias

from pydantic import BeforeValidator, Field, model_validator

from ._sql_ast_models import ExpressionFact
from ._check_contract import (
    CheckFailureCode as CheckFailureCode,
    CheckKind as CheckKind,
    CheckRepair as CheckRepair,
    CheckResult,
    CheckStatus as CheckStatus,
    RepairKind as RepairKind,
)
from ._model_primitives import (
    CONTRACT_VERSION as CONTRACT_VERSION,
    ContractModel,
    Digest,
    Id,
    NonEmptyText,
    NonNegativeInt,
    Probability,
    StrictModel,
    require_canonical_ids,
    unique_ids,
)
from ._semantic_resolution import derive_semantic_resolution


def _decode_expression_fact(value: object) -> ExpressionFact:
    if type(value) is ExpressionFact:
        _require_exact_expression_fact(value)
        return value
    return _decode_serialized_expression_fact(value)


def _require_exact_expression_fact(value: ExpressionFact) -> None:
    if type(value.kind) is not str:
        raise ValueError("executable ExpressionFact kind must be an exact string")
    if type(value.attributes) is not tuple:
        raise ValueError("executable ExpressionFact attributes must be a tuple")
    for attribute in value.attributes:
        if (
            type(attribute) is not tuple
            or len(attribute) != 2
            or type(attribute[0]) is not str
            or not _is_expression_scalar(attribute[1])
        ):
            raise ValueError("executable ExpressionFact attributes are malformed")
    if type(value.children) is not tuple:
        raise ValueError("executable ExpressionFact children must be a tuple")
    for child in value.children:
        if (
            type(child) is not tuple
            or len(child) != 3
            or type(child[0]) is not str
            or type(child[1]) is not int
            or type(child[2]) is not ExpressionFact
        ):
            raise ValueError("executable ExpressionFact children are malformed")
        _require_exact_expression_fact(child[2])


def _decode_serialized_expression_fact(value: object) -> ExpressionFact:
    if type(value) is not dict or set(value) != {"kind", "attributes", "children"}:
        raise ValueError("executable expressions must be exact ExpressionFact values")
    kind = value["kind"]
    if type(kind) is not str:
        raise ValueError("executable ExpressionFact kind must be an exact string")
    attributes = _decode_serialized_expression_attributes(value["attributes"])
    children = _decode_serialized_expression_children(value["children"])
    return ExpressionFact(kind=kind, attributes=attributes, children=children)


def _decode_serialized_expression_attributes(
    value: object,
) -> tuple[tuple[str, str | int | float | bool | None], ...]:
    if type(value) not in {list, tuple}:
        raise ValueError("executable ExpressionFact attributes must be an array")
    attributes: list[tuple[str, str | int | float | bool | None]] = []
    for attribute in value:
        if (
            type(attribute) not in {list, tuple}
            or len(attribute) != 2
            or type(attribute[0]) is not str
            or not _is_expression_scalar(attribute[1])
        ):
            raise ValueError("executable ExpressionFact attributes are malformed")
        attributes.append((attribute[0], attribute[1]))
    return tuple(attributes)


def _decode_serialized_expression_children(
    value: object,
) -> tuple[tuple[str, int, ExpressionFact], ...]:
    if type(value) not in {list, tuple}:
        raise ValueError("executable ExpressionFact children must be an array")
    children: list[tuple[str, int, ExpressionFact]] = []
    for child in value:
        if (
            type(child) not in {list, tuple}
            or len(child) != 3
            or type(child[0]) is not str
            or type(child[1]) is not int
        ):
            raise ValueError("executable ExpressionFact children are malformed")
        children.append(
            (child[0], child[1], _decode_serialized_expression_fact(child[2]))
        )
    return tuple(children)


def _is_expression_scalar(value: object) -> bool:
    if type(value) is float:
        return math.isfinite(value)
    return value is None or type(value) in {str, int, bool}


ExecutableExpression: TypeAlias = Annotated[
    ExpressionFact,
    BeforeValidator(_decode_expression_fact),
]


class SemanticItemKind(StrEnum):
    METRIC = "metric"
    DIMENSION = "dimension"
    FILTER = "filter"
    ORDERING = "ordering"
    LIMIT = "limit"
    TIME = "time"
    FORMULA = "formula"


class SemanticItemStatus(StrEnum):
    UNRESOLVED = "unresolved"
    PARTIALLY_RESOLVED = "partially_resolved"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


class ExpectedResultShape(StrEnum):
    SCALAR = "scalar"
    ROWS = "rows"
    GROUPED_ROWS = "grouped_rows"
    RANKED_ROWS = "ranked_rows"
    TIME_SERIES = "time_series"


class PredicateOperator(StrEnum):
    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    BETWEEN = "between"
    LIKE = "like"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class JoinType(StrEnum):
    INNER = "inner"
    LEFT = "left"


class TableRef(StrictModel):
    namespace: NonEmptyText
    schema_name: NonEmptyText | None = Field(alias="schema")
    table: NonEmptyText


class ColumnRef(StrictModel):
    table: TableRef
    column: NonEmptyText


class DocumentRef(StrictModel):
    document_id: Id
    namespace: NonEmptyText


class QueryProbeRef(StrictModel):
    probe_id: Id
    namespace: NonEmptyText


class ExpressionRef(StrictModel):
    expression_id: Id
    expression: NonEmptyText


class LiteralValue(StrictModel):
    value: str | int | float | bool | None


TargetRef: TypeAlias = TableRef | ColumnRef | DocumentRef | QueryProbeRef
PredicateOperand: TypeAlias = (
    LiteralValue | ColumnRef | ExpressionRef | str | int | float | bool
)


class PredicateRef(StrictModel):
    left: ColumnRef | ExpressionRef
    operator: PredicateOperator
    right: PredicateOperand | tuple[LiteralValue | str | int | float | bool, ...] | None

    @model_validator(mode="after")
    def validate_null_operator(self) -> PredicateRef:
        if self.operator in {PredicateOperator.IS_NULL, PredicateOperator.IS_NOT_NULL}:
            if self.right is not None:
                raise ValueError(
                    "is_null and is_not_null predicates require right=None"
                )
        elif self.right is None:
            raise ValueError("non-null predicates require right")
        return self


class JoinEdge(StrictModel):
    left: ColumnRef
    right: ColumnRef
    operator: Literal[PredicateOperator.EQ] = PredicateOperator.EQ
    join_type: JoinType = JoinType.INNER


class SemanticItem(StrictModel):
    source_id: Id
    kind: SemanticItemKind
    source_text: NonEmptyText
    normalized_meaning: NonEmptyText | None
    required: bool
    exact_physical_predicate: bool = Field(
        default=False,
        exclude_if=lambda value: value is False,
    )
    operator: PredicateOperator | None
    literal_or_reference: (
        PredicateOperand | tuple[LiteralValue | str | int | float | bool, ...] | None
    )
    status: SemanticItemStatus
    binding_ids: tuple[Id, ...]

    @model_validator(mode="after")
    def validate_status(self) -> SemanticItem:
        if (
            self.status is SemanticItemStatus.RESOLVED
            and not self.binding_ids
            and not is_structurally_resolved_limit(self)
        ):
            raise ValueError("resolved SemanticItem requires binding_ids")
        return self


def is_structurally_resolved_limit(item: SemanticItem) -> bool:
    return (
        item.required
        and item.kind is SemanticItemKind.LIMIT
        and type(item.literal_or_reference) is int
        and item.literal_or_reference > 0
    )


def is_binding_free_structural_limit(
    item: SemanticItem,
    bindings: tuple[BindingBase, ...] | None = None,
) -> bool:
    if (
        item.required
        and item.kind is SemanticItemKind.LIMIT
        and item.literal_or_reference is None
    ):
        return True
    if not is_structurally_resolved_limit(item):
        return False
    if bindings is None:
        return not item.binding_ids
    return not any(binding.source_id == item.source_id for binding in bindings)


def is_binding_free_semantic_item(
    item: SemanticItem,
    bindings: tuple[BindingBase, ...] | None = None,
) -> bool:
    return (
        item.required
        and item.kind is SemanticItemKind.FORMULA
        and item.status
        in {SemanticItemStatus.UNRESOLVED, SemanticItemStatus.RESOLVED}
        and not item.binding_ids
    ) or is_binding_free_structural_limit(item, bindings)


class QuerySpec(ContractModel):
    contract_name: Literal["query_spec"] = "query_spec"
    query_id: Id
    original_text: NonEmptyText
    semantic_items: tuple[SemanticItem, ...]
    requested_output_source_ids: tuple[Id, ...]
    expected_result_shape: ExpectedResultShape
    global_constraints: tuple[PredicateRef, ...]

    @model_validator(mode="after")
    def validate_semantic_items(self) -> QuerySpec:
        source_ids: set[str] = set()
        for item in self.semantic_items:
            if item.source_id in source_ids:
                raise ValueError("semantic_items source_id must be unique")
            source_ids.add(item.source_id)
        require_canonical_ids(
            self.requested_output_source_ids,
            "QuerySpec requested_output_source_ids",
        )
        required_source_ids = {
            item.source_id for item in self.semantic_items if item.required
        }
        if not set(self.requested_output_source_ids).issubset(required_source_ids):
            raise ValueError(
                "requested_output_source_ids must reference required semantic items"
            )
        return self


class EvidenceSourceKind(StrEnum):
    SCHEMA = "schema"
    CATALOG = "catalog"
    PROFILE = "profile"
    SAMPLE = "sample"
    VALUE_SEARCH = "value_search"
    PROBE = "probe"
    DOCUMENT = "document"


class EvidenceValidityScope(StrEnum):
    SCHEMA_VERSION = "schema_version"
    DATA_SNAPSHOT = "data_snapshot"
    RUN_ONLY = "run_only"


class EvidenceCost(StrictModel):
    wall_clock_ms: NonNegativeInt
    model_calls: NonNegativeInt
    model_tokens: NonNegativeInt
    db_probe_ms: NonNegativeInt
    rows: NonNegativeInt
    bytes: NonNegativeInt


class EvidenceRecord(ContractModel):
    contract_name: Literal["evidence_record"] = "evidence_record"
    schema_namespace_version: Digest
    evidence_id: Id
    source_kind: EvidenceSourceKind
    target: TargetRef
    action_digest: Digest
    observation: NonEmptyText
    validity_scope: EvidenceValidityScope
    data_snapshot_token: NonEmptyText | None
    observed_at: datetime
    strength: Probability
    created_at: datetime
    cost: EvidenceCost

    @model_validator(mode="after")
    def validate_freshness(self) -> EvidenceRecord:
        for field_name in ("observed_at", "created_at"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
                raise ValueError(f"{field_name} must be a UTC timestamp")
        if (
            self.validity_scope is EvidenceValidityScope.DATA_SNAPSHOT
            and self.data_snapshot_token is None
        ):
            raise ValueError(
                "data_snapshot_token is required for data_snapshot evidence"
            )
        if (
            self.validity_scope is not EvidenceValidityScope.DATA_SNAPSHOT
            and self.data_snapshot_token is not None
        ):
            raise ValueError(
                "data_snapshot_token is only valid for data_snapshot evidence"
            )
        return self


class BindingStatus(StrEnum):
    CANDIDATE = "candidate"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    STALE = "stale"


class BindingBase(StrictModel):
    binding_id: Id
    source_id: Id
    tables: tuple[TableRef, ...]
    columns: tuple[ColumnRef, ...]
    predicates: tuple[PredicateRef, ...]
    join_path: tuple[JoinEdge, ...]
    evidence_ids: tuple[Id, ...]
    confidence: Probability
    status: BindingStatus
    validator_rule: NonEmptyText | None

    @model_validator(mode="after")
    def validate_supported_binding(self) -> BindingBase:
        if self.status is BindingStatus.SUPPORTED:
            if not self.evidence_ids:
                raise ValueError("supported Binding requires evidence_ids")
            if self.validator_rule is None:
                raise ValueError("supported Binding requires validator_rule")
        return self


class PhysicalColumnBinding(BindingBase):
    kind: Literal["physical_column"] = "physical_column"
    physical_column: ColumnRef


class VerticalAttributeBinding(BindingBase):
    kind: Literal["vertical_attribute"] = "vertical_attribute"
    entity_table: TableRef
    entity_key: ColumnRef
    attribute_catalog_table: TableRef
    attribute_catalog_key: ColumnRef
    attribute_name_predicate: PredicateRef
    value_table: TableRef
    value_entity_key: ColumnRef
    value_attribute_key: ColumnRef
    value_predicate: PredicateRef


class DiscriminatorValueBinding(BindingBase):
    kind: Literal["discriminator_value"] = "discriminator_value"
    discriminator_column: ColumnRef
    discriminator_predicate: PredicateRef


class DerivedExpressionBinding(BindingBase):
    kind: Literal["derived_expression"] = "derived_expression"
    expression: ExpressionRef
    document: DocumentRef
    rule_excerpt: NonEmptyText
    input_columns: Annotated[tuple[ColumnRef, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_input_columns(self) -> DerivedExpressionBinding:
        if len(set(self.input_columns)) != len(self.input_columns):
            raise ValueError("derived expression input_columns must be unique")
        return self


class DocumentRuleBinding(BindingBase):
    kind: Literal["document_rule"] = "document_rule"
    document: DocumentRef
    rule_id: Id
    rule_text: NonEmptyText


Binding: TypeAlias = Annotated[
    PhysicalColumnBinding
    | VerticalAttributeBinding
    | DiscriminatorValueBinding
    | DerivedExpressionBinding
    | DocumentRuleBinding,
    Field(discriminator="kind"),
]


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    TESTING = "testing"
    SUPPORTED = "supported"
    REJECTED = "rejected"


class Hypothesis(StrictModel):
    hypothesis_id: Id
    source_ids: tuple[Id, ...]
    claim: NonEmptyText
    candidate_targets: tuple[TargetRef, ...]
    status: HypothesisStatus
    evidence_ids: tuple[Id, ...]

    @model_validator(mode="after")
    def validate_evidence_for_terminal_status(self) -> Hypothesis:
        if (
            self.status in {HypothesisStatus.SUPPORTED, HypothesisStatus.REJECTED}
            and not self.evidence_ids
        ):
            raise ValueError("supported/rejected Hypothesis requires evidence_ids")
        return self


class ResearchActionKind(StrEnum):
    SEMANTIC_COMMIT = "semantic_commit"
    INSPECT_CATALOG = "inspect_catalog"
    INSPECT_TABLE = "inspect_table"
    INSPECT_COLUMN = "inspect_column"
    INSPECT_RELATIONSHIPS = "inspect_relationships"
    PROFILE_COLUMN = "profile_column"
    SAMPLE_ROWS = "sample_rows"
    SEARCH_VALUE = "search_value"
    DISTINCT_VALUES = "distinct_values"
    EXECUTE_PROBE = "execute_probe"
    READ_DOCUMENT = "read_document"


class ResearchAction(StrictModel):
    action_id: Id
    kind: ResearchActionKind
    hypothesis_id: Id | None
    target: TargetRef | None
    parameters: tuple[tuple[NonEmptyText, str | int | float | bool | None], ...]
    action_digest: Digest
    expected_revision: NonNegativeInt

    @model_validator(mode="after")
    def validate_parameter_keys(self) -> ResearchAction:
        keys = [key for key, _ in self.parameters]
        if len(keys) != len(set(keys)):
            raise ValueError("ResearchAction parameters keys must be unique")
        if self.kind is ResearchActionKind.SEMANTIC_COMMIT:
            if (
                self.target is not None
                or self.hypothesis_id is not None
                or self.parameters
            ):
                raise ValueError(
                    "semantic_commit requires null target and no parameters or hypothesis"
                )
        elif self.target is None:
            raise ValueError("research probe action requires a non-null target")
        return self


class ResultExpectationKind(StrEnum):
    """Closed result facts derived from standard research probes."""

    FILTER_MATCH_ABSENT = "filter_match_absent"
    DIRECT_OUTPUT_NOT_NULL = "direct_output_not_null"
    DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN = "direct_output_non_null_value_domain"
    DIRECT_OUTPUT_PRIMARY_KEY_UNIQUE = "direct_output_primary_key_unique"


ResultExpectationValue: TypeAlias = str | int | float | bool | None


class ResultExpectation(StrictModel):
    """One immutable result assertion tied to one source and evidence record."""

    source_id: Id
    evidence_id: Id
    kind: ResultExpectationKind
    column: ColumnRef
    allowed_values: tuple[ResultExpectationValue, ...] = ()

    @model_validator(mode="after")
    def validate_domain(self) -> ResultExpectation:
        if self.kind is ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN:
            if not self.allowed_values:
                raise ValueError("value-domain expectation requires allowed_values")
            if any(value is None for value in self.allowed_values):
                raise ValueError("value-domain expectation cannot contain null")
            encoded = tuple(_result_expectation_value_bytes(value) for value in self.allowed_values)
            if len(encoded) != len(set(encoded)):
                raise ValueError("value-domain expectation values must be unique")
            if encoded != tuple(sorted(encoded)):
                raise ValueError("value-domain expectation values must be canonical")
        elif self.allowed_values:
            raise ValueError("only value-domain expectations may have allowed_values")
        return self


def _result_expectation_value_bytes(value: ResultExpectationValue) -> bytes:
    if type(value) is float and not math.isfinite(value):
        raise ValueError("result expectation value must be finite")
    if value is not None and type(value) not in {str, int, float, bool}:
        raise ValueError("result expectation value must be a JSON scalar")
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("result expectation value must be canonical JSON") from exc


class JoinCandidateStatus(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    REJECTED = "rejected"


class JoinCandidate(StrictModel):
    join_id: Id
    left: ColumnRef
    right: ColumnRef
    join_type: JoinType
    path: tuple[JoinEdge, ...]
    status: JoinCandidateStatus
    evidence_ids: tuple[Id, ...]

    @model_validator(mode="after")
    def validate_validated_join(self) -> JoinCandidate:
        if self.status is JoinCandidateStatus.VALIDATED:
            if not self.path or not self.evidence_ids:
                raise ValueError(
                    "validated JoinCandidate requires path and evidence_ids"
                )
        return self


class BudgetState(StrictModel):
    initial_wall_clock_ms: NonNegativeInt
    used_wall_clock_ms: NonNegativeInt
    remaining_wall_clock_ms: NonNegativeInt
    initial_model_calls: NonNegativeInt
    used_model_calls: NonNegativeInt
    remaining_model_calls: NonNegativeInt
    initial_model_tokens: NonNegativeInt
    used_model_tokens: NonNegativeInt
    remaining_model_tokens: NonNegativeInt
    initial_db_probe_ms: NonNegativeInt
    used_db_probe_ms: NonNegativeInt
    remaining_db_probe_ms: NonNegativeInt
    initial_rows: NonNegativeInt
    used_rows: NonNegativeInt
    remaining_rows: NonNegativeInt
    initial_bytes: NonNegativeInt
    used_bytes: NonNegativeInt
    remaining_bytes: NonNegativeInt

    @model_validator(mode="after")
    def validate_remaining_budgets(self) -> BudgetState:
        for name in (
            "wall_clock_ms",
            "model_calls",
            "model_tokens",
            "db_probe_ms",
            "rows",
            "bytes",
        ):
            initial = getattr(self, f"initial_{name}")
            used = getattr(self, f"used_{name}")
            remaining = getattr(self, f"remaining_{name}")
            if initial - used != remaining:
                raise ValueError(
                    f"remaining_{name} must equal initial_{name} minus used_{name}"
                )
        return self


class ResearchStopReason(StrEnum):
    COMPLETE = "COMPLETE"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"
    STAGNATED = "STAGNATED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CANCELLED = "CANCELLED"
    TOOL_FAILURE = "TOOL_FAILURE"
    PROTOCOL_FAILURE = "PROTOCOL_FAILURE"


class ResearchState(ContractModel):
    contract_name: Literal["research_state"] = "research_state"
    schema_namespace_version: Digest
    query_spec: QuerySpec
    hypotheses: tuple[Hypothesis, ...]
    evidence: tuple[EvidenceRecord, ...]
    bindings: tuple[Binding, ...]
    join_candidates: tuple[JoinCandidate, ...]
    unresolved_items: tuple[Id, ...]
    action_history: tuple[ResearchAction, ...]
    result_expectations: tuple[ResultExpectation, ...]
    budget_state: BudgetState
    stop_reason: ResearchStopReason | None

    @model_validator(mode="after")
    def validate_references(self) -> ResearchState:
        source_ids = {item.source_id for item in self.query_spec.semantic_items}
        evidence_ids = unique_ids(self.evidence, "evidence_id", "evidence")
        evidence_by_id = {item.evidence_id: item for item in self.evidence}
        binding_ids = unique_ids(self.bindings, "binding_id", "bindings")
        hypothesis_ids = unique_ids(self.hypotheses, "hypothesis_id", "hypotheses")
        unique_ids(self.join_candidates, "join_id", "join_candidates")
        unique_ids(self.action_history, "action_id", "action_history")
        action_digests = [action.action_digest for action in self.action_history]
        if len(action_digests) != len(set(action_digests)):
            raise ValueError("ResearchState action_digest must be unique")
        expected_revisions = [
            action.expected_revision for action in self.action_history
        ]
        if expected_revisions != list(range(self.revision)):
            raise ValueError(
                "ResearchState action_history must contain exactly one action "
                "for every revision from zero"
            )
        if self.query_spec.revision > self.revision:
            raise ValueError(
                "ResearchState query_spec revision cannot be in the future"
            )
        expectation_keys = [
            (
                item.source_id,
                item.evidence_id,
                item.kind,
                item.column.table.namespace,
                item.column.table.schema_name,
                item.column.table.table,
                item.column.column,
            )
            for item in self.result_expectations
        ]
        if len(expectation_keys) != len(set(expectation_keys)):
            raise ValueError("ResearchState result expectations must be unique")
        for expectation in self.result_expectations:
            if expectation.source_id not in source_ids:
                raise ValueError(
                    "ResultExpectation source_id must reference query semantic item"
                )
            if expectation.evidence_id not in evidence_ids:
                raise ValueError(
                    "ResultExpectation evidence_id must reference state evidence"
                )
        for hypothesis in self.hypotheses:
            if not set(hypothesis.source_ids).issubset(source_ids):
                raise ValueError(
                    "Hypothesis source_ids must reference query semantic items"
                )
            if not set(hypothesis.evidence_ids).issubset(evidence_ids):
                raise ValueError(
                    "Hypothesis evidence_ids must reference state evidence"
                )
        for binding in self.bindings:
            if binding.source_id not in source_ids:
                raise ValueError("Binding source_id must reference query semantic item")
            if not set(binding.evidence_ids).issubset(evidence_ids):
                raise ValueError("Binding evidence_ids must reference state evidence")
            source_item = next(
                item
                for item in self.query_spec.semantic_items
                if item.source_id == binding.source_id
            )
            if source_item.required and not binding.evidence_ids:
                raise ValueError(
                    "Binding for required semantic item requires evidence_ids"
                )
            if binding.status is not BindingStatus.STALE and any(
                evidence_by_id[evidence_id].schema_namespace_version
                != self.schema_namespace_version
                for evidence_id in binding.evidence_ids
            ):
                raise ValueError(
                    "Binding with stale schema evidence must have status=stale"
                )
        bindings_by_source: dict[str, tuple[Binding, ...]] = {
            source_id: tuple(
                binding for binding in self.bindings if binding.source_id == source_id
            )
            for source_id in source_ids
        }
        bindings_by_id = {binding.binding_id: binding for binding in self.bindings}
        for item in self.query_spec.semantic_items:
            if not set(item.binding_ids).issubset(binding_ids):
                raise ValueError(
                    "SemanticItem binding_ids must reference state bindings"
                )
            if any(
                bindings_by_id[binding_id].source_id != item.source_id
                for binding_id in item.binding_ids
            ):
                raise ValueError(
                    "SemanticItem binding_ids must belong to the same source"
                )
            if is_structurally_resolved_limit(item) and not bindings_by_source[
                item.source_id
            ]:
                expected_status, expected_binding_ids = (
                    SemanticItemStatus.RESOLVED.value,
                    (),
                )
            else:
                expected_status, expected_binding_ids = derive_semantic_resolution(
                    item.status.value,
                    bindings_by_source[item.source_id],
                )
            if (
                item.status.value != expected_status
                or item.binding_ids != expected_binding_ids
            ):
                raise ValueError(
                    "SemanticItem status and binding_ids must match binding resolution"
                )
        expected_unresolved = tuple(
            sorted(
                item.source_id
                for item in self.query_spec.semantic_items
                if item.required
                and not is_binding_free_semantic_item(item, self.bindings)
                and not any(
                    binding.status is BindingStatus.SUPPORTED
                    for binding in bindings_by_source[item.source_id]
                )
            )
        )
        if self.unresolved_items != expected_unresolved:
            raise ValueError(
                "ResearchState unresolved_items must exactly identify required "
                "sources without supported bindings"
            )
        for join in self.join_candidates:
            if not set(join.evidence_ids).issubset(evidence_ids):
                raise ValueError(
                    "JoinCandidate evidence_ids must reference state evidence"
                )
        for action in self.action_history:
            if (
                action.hypothesis_id is not None
                and action.hypothesis_id not in hypothesis_ids
            ):
                raise ValueError(
                    "ResearchAction hypothesis_id must reference a state hypothesis"
                )
            if action.expected_revision >= self.revision:
                raise ValueError(
                    "ResearchAction expected_revision must precede ResearchState revision"
                )
        if (
            self.query_spec.run_id != self.run_id
            or self.query_spec.run_incarnation != self.run_incarnation
        ):
            raise ValueError("ResearchState query_spec must belong to the same run")
        return self


class AstExpressionPathSegment(StrictModel):
    """One deterministic child address inside an immutable AST expression."""

    argument: NonEmptyText
    ordinal: NonNegativeInt


class AstNodeAnnotation(StrictModel):
    """Trusted source/evidence attached to one existing AST fact or expression."""

    node_id: Id
    expression_field: NonEmptyText | None = None
    expression_index: NonNegativeInt | None = None
    expression_path: tuple[AstExpressionPathSegment, ...] = ()
    source_ids: tuple[Id, ...] = ()
    evidence_ids: tuple[Id, ...] = ()

    @model_validator(mode="after")
    def validate_address_and_authority(self) -> AstNodeAnnotation:
        require_canonical_ids(self.source_ids, "AstNodeAnnotation source_ids")
        require_canonical_ids(self.evidence_ids, "AstNodeAnnotation evidence_ids")
        if (self.expression_field is None) != (self.expression_index is None):
            raise ValueError("AST expression field and index must be specified together")
        if self.expression_field is None and self.expression_path:
            raise ValueError("AST expression path requires an expression address")
        return self


class AstSemanticCoverage(StrictModel):
    """Minimal trusted overlay; it never copies or rewrites the SQL AST."""

    requirements_digest: Digest
    required_source_ids: tuple[Id, ...]
    evidence_ids: tuple[Id, ...]
    annotations: tuple[AstNodeAnnotation, ...]

    @model_validator(mode="after")
    def validate_annotations(self) -> AstSemanticCoverage:
        require_canonical_ids(
            self.required_source_ids, "AstSemanticCoverage required_source_ids"
        )
        require_canonical_ids(self.evidence_ids, "AstSemanticCoverage evidence_ids")
        addresses = tuple(
            (
                item.node_id,
                item.expression_field,
                item.expression_index,
                tuple((part.argument, part.ordinal) for part in item.expression_path),
            )
            for item in self.annotations
        )
        if len(addresses) != len(set(addresses)):
            raise ValueError("AstSemanticCoverage annotations must have unique addresses")
        if any(
            not set(item.source_ids).issubset(self.required_source_ids)
            or not set(item.evidence_ids).issubset(self.evidence_ids)
            for item in self.annotations
        ):
            raise ValueError("AST annotation authority exceeds semantic coverage")
        return self


class SqlCandidate(StrictModel):
    candidate_id: Id
    sql: NonEmptyText
    normalized_ast_digest: Digest
    revision: NonNegativeInt


class ExecutionResult(StrictModel):
    execution_id: Id
    candidate_id: Id
    success: bool
    row_count: NonNegativeInt
    elapsed_ms: NonNegativeInt
    error_code: NonEmptyText | None

    @model_validator(mode="after")
    def validate_error_code(self) -> ExecutionResult:
        if self.success and self.error_code is not None:
            raise ValueError("successful ExecutionResult cannot have error_code")
        if not self.success and self.error_code is None:
            raise ValueError("failed ExecutionResult requires error_code")
        return self


class MissingEvidenceRequest(ContractModel):
    contract_name: Literal["missing_evidence_request"] = "missing_evidence_request"
    schema_namespace_version: Digest
    missing_evidence_request_id: Id
    source_id: Id
    question: NonEmptyText
    candidate_targets: tuple[TargetRef, ...]
    required_evidence_kind: EvidenceSourceKind
    reason: NonEmptyText
    repair_kind: Literal["semantic_binding_mismatch"] | None = None
    repair_binding_id: Id | None = None

    @model_validator(mode="after")
    def validate_repair_target(self) -> MissingEvidenceRequest:
        if (self.repair_kind is None) != (self.repair_binding_id is None):
            raise ValueError("semantic repair kind and binding ID must be provided together")
        return self


class ResearchReentryStatus(StrEnum):
    ADMITTED = "ADMITTED"
    COMPLETED = "COMPLETED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CANCELLED = "CANCELLED"
    TOOL_FAILURE = "TOOL_FAILURE"
    PROTOCOL_FAILURE = "PROTOCOL_FAILURE"


class ResearchReentryRecord(ContractModel):
    contract_name: Literal["research_reentry_record"] = "research_reentry_record"
    schema_namespace_version: Digest
    research_reentry_id: Id
    missing_evidence_request_id: Id
    source_id: Id
    ordinal: Annotated[int, Field(ge=1, le=3)]
    research_base_revision: NonNegativeInt
    baseline_evidence_ids: tuple[Id, ...]
    status: ResearchReentryStatus
    research_result_revision: NonNegativeInt | None
    evidence_ids: tuple[Id, ...]

    @model_validator(mode="after")
    def validate_lifecycle_shape(self) -> ResearchReentryRecord:
        for values, label in (
            (self.baseline_evidence_ids, "baseline evidence IDs"),
            (self.evidence_ids, "result evidence IDs"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{label} must be sorted and unique")
        if self.status is ResearchReentryStatus.ADMITTED:
            if self.research_result_revision is not None or self.evidence_ids:
                raise ValueError("ADMITTED re-entry cannot contain a result")
        elif self.status is ResearchReentryStatus.COMPLETED:
            if (
                self.research_result_revision is None
                or self.research_result_revision <= self.research_base_revision
                or not self.evidence_ids
            ):
                raise ValueError(
                    "COMPLETED re-entry requires a newer research revision and evidence"
                )
        elif self.research_result_revision is not None or self.evidence_ids:
            raise ValueError("failed re-entry cannot contain a research result")
        return self


class SolverStopReason(StrEnum):
    SOLVED = "SOLVED"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    NO_SAFE_CANDIDATE = "NO_SAFE_CANDIDATE"
    STAGNATED = "STAGNATED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CANCELLED = "CANCELLED"
    TOOL_FAILURE = "TOOL_FAILURE"
    PROTOCOL_FAILURE = "PROTOCOL_FAILURE"


class SolverActionKind(StrEnum):
    SQL_CANDIDATE = "sql_candidate"
    MISSING_EVIDENCE = "missing_evidence"


class SolverAction(StrictModel):
    action_id: Id
    kind: SolverActionKind
    base_revision: NonNegativeInt
    candidate_id: Id | None
    missing_evidence_request_id: Id | None

    @model_validator(mode="after")
    def validate_subject(self) -> SolverAction:
        has_candidate = self.candidate_id is not None
        has_request = self.missing_evidence_request_id is not None
        if self.kind is SolverActionKind.SQL_CANDIDATE:
            if not has_candidate or has_request:
                raise ValueError(
                    "sql_candidate SolverAction requires candidate_id only"
                )
        elif has_candidate or not has_request:
            raise ValueError(
                "missing_evidence SolverAction requires missing_evidence_request_id only"
            )
        return self


class SolverState(ContractModel):
    contract_name: Literal["solver_state"] = "solver_state"
    schema_namespace_version: Digest
    query_spec: QuerySpec
    sql_candidates: tuple[SqlCandidate, ...]
    check_results: tuple[CheckResult, ...]
    execution_results: tuple[ExecutionResult, ...]
    missing_evidence_requests: tuple[MissingEvidenceRequest, ...]
    research_reentries: tuple[ResearchReentryRecord, ...] = ()
    action_history: tuple[SolverAction, ...]
    selected_candidate_id: Id | None
    stop_reason: SolverStopReason | None

    @model_validator(mode="after")
    def validate_solver_references(self) -> SolverState:
        if (
            self.query_spec.run_id != self.run_id
            or self.query_spec.run_incarnation != self.run_incarnation
        ):
            raise ValueError("SolverState query_spec must belong to the same run")
        if self.query_spec.schema_namespace_version != self.schema_namespace_version:
            raise ValueError(
                "SolverState query_spec schema_namespace_version must match state"
            )
        if self.query_spec.revision > self.revision:
            raise ValueError("SolverState query_spec revision cannot be in the future")
        source_ids = {item.source_id for item in self.query_spec.semantic_items}
        candidate_ids = unique_ids(
            self.sql_candidates, "candidate_id", "sql_candidates"
        )
        unique_ids(self.action_history, "action_id", "action_history")
        unique_ids(self.check_results, "check_id", "check_results")
        unique_ids(self.execution_results, "execution_id", "execution_results")
        unique_ids(
            self.missing_evidence_requests,
            "missing_evidence_request_id",
            "missing_evidence_requests",
        )
        unique_ids(
            self.research_reentries,
            "research_reentry_id",
            "research_reentries",
        )
        candidate_digests = tuple(
            candidate.normalized_ast_digest for candidate in self.sql_candidates
        )
        if len(candidate_digests) != len(set(candidate_digests)):
            raise ValueError(
                "SolverState normalized_ast_digest must be globally unique"
            )
        if len(self.sql_candidates) > 8:
            raise ValueError("SolverState supports at most 8 SQL candidates")
        execution_candidate_ids = tuple(
            item.candidate_id for item in self.execution_results
        )
        if len(execution_candidate_ids) != len(set(execution_candidate_ids)):
            raise ValueError(
                "SolverState allows at most one ExecutionResult per candidate"
            )
        for candidate in self.sql_candidates:
            if candidate.revision > self.revision:
                raise ValueError("SqlCandidate revision cannot be in the future")
        for check in self.check_results:
            if check.candidate_id not in candidate_ids:
                raise ValueError("CheckResult candidate_id must reference SqlCandidate")
            if not set(check.affected_source_ids).issubset(source_ids):
                raise ValueError(
                    "CheckResult affected_source_ids must reference query semantic items"
                )
        for execution in self.execution_results:
            if execution.candidate_id not in candidate_ids:
                raise ValueError(
                    "ExecutionResult candidate_id must reference SqlCandidate"
                )
        for request in self.missing_evidence_requests:
            if (
                request.run_id != self.run_id
                or request.run_incarnation != self.run_incarnation
            ):
                raise ValueError("MissingEvidenceRequest must belong to the same run")
            if request.schema_namespace_version != self.schema_namespace_version:
                raise ValueError(
                    "MissingEvidenceRequest schema_namespace_version must match SolverState"
                )
            if request.revision > self.revision:
                raise ValueError(
                    "MissingEvidenceRequest revision cannot be in the future"
                )
            if request.source_id not in source_ids:
                raise ValueError(
                    "MissingEvidenceRequest source_id must reference query semantic item"
                )
        request_ids = {
            request.missing_evidence_request_id
            for request in self.missing_evidence_requests
        }
        requests_by_id = {
            request.missing_evidence_request_id: request
            for request in self.missing_evidence_requests
        }
        ordinals_by_request: dict[str, list[int]] = {}
        admitted_count = 0
        for reentry in self.research_reentries:
            request = requests_by_id.get(reentry.missing_evidence_request_id)
            if request is None:
                raise ValueError(
                    "ResearchReentryRecord must reference MissingEvidenceRequest"
                )
            if (
                reentry.run_id != self.run_id
                or reentry.run_incarnation != self.run_incarnation
            ):
                raise ValueError("ResearchReentryRecord must belong to the same run")
            if reentry.schema_namespace_version != self.schema_namespace_version:
                raise ValueError(
                    "ResearchReentryRecord schema_namespace_version must match SolverState"
                )
            if reentry.revision > self.revision:
                raise ValueError(
                    "ResearchReentryRecord revision cannot be in the future"
                )
            if reentry.source_id != request.source_id:
                raise ValueError(
                    "ResearchReentryRecord source_id must match its request"
                )
            ordinals_by_request.setdefault(
                reentry.missing_evidence_request_id, []
            ).append(reentry.ordinal)
            if reentry.status is ResearchReentryStatus.ADMITTED:
                admitted_count += 1
        if admitted_count > 1:
            raise ValueError("SolverState allows at most one ADMITTED re-entry")
        for ordinals in ordinals_by_request.values():
            if ordinals != list(range(1, len(ordinals) + 1)):
                raise ValueError("ResearchReentryRecord ordinals must be contiguous")
        base_revisions = tuple(action.base_revision for action in self.action_history)
        if base_revisions != tuple(sorted(base_revisions)) or len(
            base_revisions
        ) != len(set(base_revisions)):
            raise ValueError(
                "SolverState action_history base_revision must be strictly increasing"
            )
        if any(base_revision >= self.revision for base_revision in base_revisions):
            raise ValueError(
                "SolverAction base_revision must precede SolverState revision"
            )
        for action in self.action_history:
            if action.kind is SolverActionKind.SQL_CANDIDATE:
                if action.candidate_id not in candidate_ids:
                    raise ValueError(
                        "SolverAction candidate_id must reference SqlCandidate"
                    )
            elif action.missing_evidence_request_id not in request_ids:
                raise ValueError(
                    "SolverAction missing_evidence_request_id must reference MissingEvidenceRequest"
                )
        action_candidate_ids = tuple(
            action.candidate_id
            for action in self.action_history
            if action.kind is SolverActionKind.SQL_CANDIDATE
        )
        action_request_ids = tuple(
            action.missing_evidence_request_id
            for action in self.action_history
            if action.kind is SolverActionKind.MISSING_EVIDENCE
        )
        if (
            len(action_candidate_ids) != len(set(action_candidate_ids))
            or set(action_candidate_ids) != candidate_ids
        ):
            raise ValueError(
                "SolverState SQL SolverAction subjects must exactly cover SqlCandidate"
            )
        if (
            len(action_request_ids) != len(set(action_request_ids))
            or set(action_request_ids) != request_ids
        ):
            raise ValueError(
                "SolverState missing-evidence SolverAction subjects must exactly cover MissingEvidenceRequest"
            )
        if (
            self.selected_candidate_id is not None
            and self.selected_candidate_id not in candidate_ids
        ):
            raise ValueError("selected_candidate_id must reference SqlCandidate")
        if (
            self.stop_reason is SolverStopReason.SOLVED
            and self.selected_candidate_id is None
        ):
            raise ValueError("SOLVED SolverState requires selected_candidate_id")
        if (
            self.stop_reason is SolverStopReason.MISSING_EVIDENCE
            and not self.missing_evidence_requests
        ):
            raise ValueError(
                "MISSING_EVIDENCE SolverState requires MissingEvidenceRequest"
            )
        return self
