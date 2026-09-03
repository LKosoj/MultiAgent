"""Acceptance tests for the pure W3 semantic reducer."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import subprocess
import sys

import pytest

from custom_tools.text_to_sql.adaptive.freshness import (
    DocumentSourceAvailability,
    DocumentSourceState,
    FreshnessContext,
)
from custom_tools.text_to_sql.adaptive.models import (
    BudgetState,
    BindingStatus,
    ColumnRef,
    DerivedExpressionBinding,
    DiscriminatorValueBinding,
    DocumentRef,
    DocumentRuleBinding,
    EvidenceCost,
    ExpressionRef,
    ExpectedResultShape,
    Hypothesis,
    HypothesisStatus,
    JoinCandidate,
    JoinCandidateStatus,
    JoinEdge,
    JoinType,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    QuerySpec,
    ResearchAction,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    ResearchActionKind,
    ResearchState,
    TableRef,
    VerticalAttributeBinding,
)
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.evidence import probe_result_to_evidence
from custom_tools.text_to_sql.adaptive.probes import (
    ProbeArtifactError,
    ProbeStatus,
    build_probe_result,
)
from custom_tools.text_to_sql.adaptive.research_decision import ResearchDecisionV1
from custom_tools.text_to_sql.adaptive.serialization import (
    ArtifactReference,
    SerializationLimits,
    canonical_json_bytes,
)
from custom_tools.text_to_sql.adaptive._semantic_value_certificate import (
    evidence_observes_exact_value,
)
from custom_tools.text_to_sql.adaptive.state import apply_research_transition

from custom_tools.text_to_sql.adaptive.semantic_reducer import (
    SemanticReducerError,
    TrustedSemanticBatch,
    TrustedToolClaim,
    ResolvedColumn,
    ResolvedTable,
    admit_semantic_turn,
    commit_semantic_turn,
    _derived_expression_certificate,
    _assess_binding,
    _cited_records,
    _declared_join_certificate,
    _document_rule_certificate,
    _fresh_evidence,
    _negative_hypothesis_certificate,
    _physical_column_certificate,
    _discriminator_certificate,
    _vertical_certificate,
)


RUN = "run-1"
INCARNATION = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SCHEMA = "sha256:" + "a" * 64


def test_semantic_reducer_import_does_not_load_data_probe_runtime() -> None:
    script = """
import json
import sys

import custom_tools.text_to_sql.adaptive.semantic_reducer

forbidden = (
    "custom_tools.text_to_sql.adaptive.data_probes",
    "custom_tools.text_to_sql.core._db_exec",
)
print(json.dumps({"forbidden": [name for name in forbidden if name in sys.modules]}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env={},
    )

    observed = json.loads(completed.stdout.splitlines()[-1])
    assert observed == {"forbidden": []}


def _budget() -> BudgetState:
    return BudgetState(
        **{
            f"{stage}_{name}": value
            for name in (
                "wall_clock_ms",
                "model_calls",
                "model_tokens",
                "db_probe_ms",
                "rows",
                "bytes",
            )
            for stage, value in (("initial", 10), ("used", 0), ("remaining", 10))
        }
    )


def _state() -> ResearchState:
    query = QuerySpec(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        query_id="query-1",
        original_text="orders",
        semantic_items=(
            SemanticItem(
                source_id="source-1",
                kind=SemanticItemKind.FILTER,
                source_text="orders",
                normalized_meaning="orders",
                required=True,
                operator=None,
                literal_or_reference=None,
                status=SemanticItemStatus.UNRESOLVED,
                binding_ids=(),
            ),
        ),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )
    return ResearchState(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        query_spec=query,
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=("source-1",),
        action_history=(),
        budget_state=_budget(),
        stop_reason=None,
        **(
            {"result_expectations": ()}
            if "result_expectations" in ResearchState.model_fields
            else {}
        ),
    )


def _context() -> FreshnessContext:
    return FreshnessContext(
        evaluated_at=datetime(2026, 7, 31, tzinfo=UTC),
        run_id=RUN,
        run_incarnation=INCARNATION,
        schema_namespace_version=SCHEMA,
    )


def _table(name: str) -> TableRef:
    return TableRef(namespace="main", schema=None, table=name)


def _column(table: str, column: str) -> ColumnRef:
    return ColumnRef(table=_table(table), column=column)


def _evidence(
    kind: ResearchActionKind,
    target: TableRef | ColumnRef | DocumentRef,
    payload: object,
    *,
    evidence_id: str,
    truncated: bool = False,
):
    digest = canonical_action_digest(
        kind=kind,
        hypothesis_id=None,
        target=target,
        parameters=(),
        expected_revision=0,
    )
    action = ResearchAction(
        action_id=f"action-{evidence_id}",
        kind=kind,
        hypothesis_id=None,
        target=target,
        parameters=(),
        action_digest=digest,
        expected_revision=0,
    )
    raw = canonical_json_bytes(payload)
    result = build_probe_result(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        invocation_id=evidence_id,
        action_digest=digest,
        probe_kind=kind,
        status=ProbeStatus.SUCCESS,
        target=target,
        started_at=datetime(2026, 7, 31, tzinfo=UTC),
        completed_at=datetime(2026, 7, 31, tzinfo=UTC),
        summary="certificate",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(raw),
        ),
        row_count=1,
        truncated=truncated,
        payload=payload,
    )
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    return evidence


def _column_evidence(column: ColumnRef, evidence_id: str):
    return _evidence(
        ResearchActionKind.INSPECT_COLUMN,
        column,
        {"status": "matched", "column": column.model_dump(mode="json", by_alias=True)},
        evidence_id=evidence_id,
    )


def _value_evidence(column: ColumnRef, value: object, evidence_id: str):
    return _evidence(
        ResearchActionKind.SEARCH_VALUE,
        column,
        {"columns": [column.column], "rows": [[value]]},
        evidence_id=evidence_id,
    )


@pytest.mark.parametrize(
    "payload",
    (
        {"columns": ["status"], "rows": [["active"]]},
        {
            "columns": ["status"],
            "rows": [["active"]],
            "requested_value": "inactive",
        },
    ),
)
def test_search_value_exact_return_remains_a_value_certificate(
    payload: dict[str, object],
) -> None:
    column = _column("events", "status")

    assert evidence_observes_exact_value(
        _evidence(
            ResearchActionKind.SEARCH_VALUE,
            column,
            payload,
            evidence_id="search-value-exact-return",
        ),
        column,
        "active",
    )


def _document_evidence(
    document: DocumentRef,
    content: str,
    evidence_id: str,
    *,
    valid_until: str | None = None,
):
    return _evidence(
        ResearchActionKind.READ_DOCUMENT,
        document,
        {
            "status": "matched",
            "document": {
                "document_id": document.document_id,
                "namespace": document.namespace,
                "schema_namespace_version": SCHEMA,
                "source_version": "v1",
                "valid_until": valid_until,
                "title": "rule",
                "content": content,
                "target": None,
            },
        },
        evidence_id=evidence_id,
    )


def _formula_assessment_admission(
    *,
    item_kind: SemanticItemKind = SemanticItemKind.FORMULA,
    required: bool = True,
    normalized_meaning: str | None = "revenue - cost",
    binding_source_id: str = "formula-1",
    expression: str = "revenue - cost",
    include_cost_evidence: bool = True,
):
    revenue = _column("ledger", "revenue")
    cost = _column("ledger", "cost")
    evidence = [_column_evidence(revenue, "revenue-evidence")]
    if include_cost_evidence:
        evidence.append(_column_evidence(cost, "cost-evidence"))
    formula_binding_ids = (
        ("binding-expression",) if binding_source_id == "formula-1" else ()
    )
    other_binding_ids = (
        ("binding-expression",) if binding_source_id == "other-1" else ()
    )
    formula_item = SemanticItem(
        source_id="formula-1",
        kind=item_kind,
        source_text="margin",
        normalized_meaning=normalized_meaning,
        required=required,
        operator=None,
        literal_or_reference=None,
        status=(
            SemanticItemStatus.PARTIALLY_RESOLVED
            if formula_binding_ids
            else SemanticItemStatus.UNRESOLVED
        ),
        binding_ids=formula_binding_ids,
    )
    other_item = SemanticItem(
        source_id="other-1",
        kind=SemanticItemKind.FILTER,
        source_text="other",
        normalized_meaning="other",
        required=False,
        operator=None,
        literal_or_reference=None,
        status=(
            SemanticItemStatus.PARTIALLY_RESOLVED
            if other_binding_ids
            else SemanticItemStatus.UNRESOLVED
        ),
        binding_ids=other_binding_ids,
    )
    query_spec = QuerySpec(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        query_id="query-formula",
        original_text="margin other",
        semantic_items=(formula_item, other_item),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )
    binding = DerivedExpressionBinding(
        binding_id="binding-expression",
        source_id=binding_source_id,
        tables=(revenue.table,),
        columns=(revenue, cost),
        predicates=(),
        join_path=(),
        evidence_ids=tuple(item.evidence_id for item in evidence),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        expression=ExpressionRef(
            expression_id="expression-1",
            expression=expression,
        ),
        document=DocumentRef(document_id="doc-formula", namespace="main"),
        rule_excerpt="revenue - cost",
        input_columns=(revenue, cost),
    )
    state = ResearchState(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        query_spec=query_spec,
        hypotheses=(),
        evidence=tuple(evidence),
        bindings=(binding,),
        join_candidates=(),
        unresolved_items=(("formula-1",) if required else ()),
        action_history=(),
        budget_state=_budget(),
        stop_reason=None,
        **(
            {"result_expectations": ()}
            if "result_expectations" in ResearchState.model_fields
            else {}
        ),
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": binding.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": tuple(
                        item.evidence_id for item in evidence
                    ),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "ledger"},
                },
            },
        }
    )
    return admit_semantic_turn(
        state,
        decision,
        batch=TrustedSemanticBatch(
            schema_namespace_version=SCHEMA,
            tables=(),
            columns=(
                ResolvedColumn(
                    logical_table="ledger",
                    logical_column="revenue",
                    physical_column=revenue,
                ),
                ResolvedColumn(
                    logical_table="ledger",
                    logical_column="cost",
                    physical_column=cost,
                ),
            ),
        ),
        freshness_context=_context(),
        tool_claim=TrustedToolClaim(
            action_id="formula-action",
            tool_name="inspect_table",
            target=_table("ledger"),
            parameters=(),
        ),
    )


def test_required_formula_requires_document_certificate() -> None:
    with pytest.raises(
        SemanticReducerError,
        match="binding assessment lacks the required semantic certificate",
    ):
        _formula_assessment_admission()


class _ArtifactStore:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def write(self, payload: bytes) -> ArtifactReference:
        reference = ArtifactReference(
            artifact_id=f"artifact-{len(self.data) + 1}",
            digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            byte_count=len(payload),
        )
        self.data[reference.artifact_id] = payload
        return reference

    def read(self, reference: ArtifactReference) -> bytes:
        return self.data[reference.artifact_id]


def _artifact_probe(admission, store: _ArtifactStore):
    assert admission.action is not None
    payload = {"status": "matched", "blob": "x" * 70_000}
    return build_probe_result(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        invocation_id="artifact-evidence",
        action_digest=admission.action.action_digest,
        probe_kind=admission.action.kind,
        status=ProbeStatus.SUCCESS,
        target=admission.action.target,
        started_at=datetime(2026, 7, 31, tzinfo=UTC),
        completed_at=datetime(2026, 7, 31, tzinfo=UTC),
        summary="artifact",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
        limits=SerializationLimits(max_state_bytes=200_000, max_inline_rows=10),
        write_artifact=store.write,
        read_artifact=store.read,
    )


def _relationship_evidence(
    left: ColumnRef,
    right: ColumnRef,
    evidence_id: str,
):
    return _evidence(
        ResearchActionKind.INSPECT_RELATIONSHIPS,
        left.table,
        {
            "status": "connected",
            "depth": 1,
            "relationships": [
                {
                    "relationship_kind": "declared",
                    "from_table": left.table.table,
                    "to_table": right.table.table,
                    "column_pairs": [
                        {
                            "from_column": left.column,
                            "to_column": right.column,
                        }
                    ],
                }
            ],
        },
        evidence_id=evidence_id,
    )


def _vertical_fixture(
    *,
    name_operator: PredicateOperator = PredicateOperator.EQ,
    value_operator: PredicateOperator = PredicateOperator.EQ,
    observed_name: str = "opaque-attribute",
    observed_value: int | None = 17,
    include_value_evidence: bool = True,
):
    entity_key = _column("f02_a", "c01")
    catalog_key = _column("f02_b", "c02")
    name_column = _column("f02_b", "c03")
    value_entity_key = _column("f02_c", "c04")
    value_attribute_key = _column("f02_c", "c05")
    value_column = _column("f02_c", "c06")
    columns = (
        entity_key,
        catalog_key,
        name_column,
        value_entity_key,
        value_attribute_key,
        value_column,
    )
    schema_evidence = tuple(
        _column_evidence(column, f"schema-{index}")
        for index, column in enumerate(columns, start=1)
    )
    name_evidence = _value_evidence(name_column, observed_name, "name-value")
    witness_evidence = (
        _value_evidence(value_column, observed_value, "value-witness")
        if include_value_evidence
        else None
    )
    entity_join_evidence = _relationship_evidence(
        entity_key,
        value_entity_key,
        "entity-join-proof",
    )
    attribute_join_evidence = _relationship_evidence(
        catalog_key,
        value_attribute_key,
        "attribute-join-proof",
    )
    entity_edge = JoinEdge(
        left=entity_key,
        right=value_entity_key,
        join_type=JoinType.INNER,
    )
    attribute_edge = JoinEdge(
        left=catalog_key,
        right=value_attribute_key,
        join_type=JoinType.INNER,
    )
    joins = {
        "entity": JoinCandidate(
            join_id="join-entity",
            left=entity_key,
            right=value_entity_key,
            join_type=JoinType.INNER,
            path=(entity_edge,),
            status=JoinCandidateStatus.VALIDATED,
            evidence_ids=(entity_join_evidence.evidence_id,),
        ),
        "attribute": JoinCandidate(
            join_id="join-attribute",
            left=catalog_key,
            right=value_attribute_key,
            join_type=JoinType.INNER,
            path=(attribute_edge,),
            status=JoinCandidateStatus.VALIDATED,
            evidence_ids=(attribute_join_evidence.evidence_id,),
        ),
    }
    name_predicate = PredicateRef(
        left=name_column,
        operator=name_operator,
        right="opaque-attribute"
        if name_operator is not PredicateOperator.IS_NULL
        else None,
    )
    value_predicate = PredicateRef(
        left=value_column,
        operator=value_operator,
        right=17 if value_operator is not PredicateOperator.IS_NULL else None,
    )
    cited = (
        *schema_evidence,
        name_evidence,
        *((witness_evidence,) if witness_evidence is not None else ()),
        entity_join_evidence,
        attribute_join_evidence,
    )
    binding = VerticalAttributeBinding(
        binding_id="binding-eav",
        source_id="source-1",
        tables=(entity_key.table, catalog_key.table, value_entity_key.table),
        columns=columns,
        predicates=(name_predicate, value_predicate),
        join_path=(entity_edge, attribute_edge),
        evidence_ids=tuple(item.evidence_id for item in cited),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        entity_table=entity_key.table,
        entity_key=entity_key,
        attribute_catalog_table=catalog_key.table,
        attribute_catalog_key=catalog_key,
        attribute_name_predicate=name_predicate,
        value_table=value_entity_key.table,
        value_entity_key=value_entity_key,
        value_attribute_key=value_attribute_key,
        value_predicate=value_predicate,
    )
    return binding, cited, joins


def _tool_decision() -> ResearchDecisionV1:
    return ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "orders"},
                },
            },
        }
    )


def _stop_decision(*proposals: object) -> ResearchDecisionV1:
    return ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": proposals,
            "next": {
                "next_kind": "stop",
                "reason": "complete",
                "source_ids": (),
                "citation_evidence_ids": ("evidence-1",),
            },
        }
    )


def _join_candidate(
    left: tuple[str, str],
    right: tuple[str, str],
    join_type: str,
    path: tuple[tuple[tuple[str, str], tuple[str, str]], ...],
    declared_join_ids: tuple[str, ...] = (),
    return_admission: bool = False,
) -> JoinCandidate | object:
    typed_join_type = JoinType(join_type)
    proposal = {
        "proposal_type": "new_join",
        "proposal_key": "proposal:join",
        "left": {"table": left[0], "column": left[1]},
        "right": {"table": right[0], "column": right[1]},
        "join_type": typed_join_type,
        "path": tuple(
            {
                "left": {"table": edge_left[0], "column": edge_left[1]},
                "right": {"table": edge_right[0], "column": edge_right[1]},
                "join_type": typed_join_type,
            }
            for edge_left, edge_right in path
        ),
        "citation_evidence_ids": ("evidence-1",),
    }
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (proposal,),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "orders"},
                },
            },
        }
    )
    logical_columns = {left, right}
    logical_columns.update(edge for pair in path for edge in pair)
    batch = TrustedSemanticBatch(
        schema_namespace_version=SCHEMA,
        tables=(),
        columns=tuple(
            ResolvedColumn(
                logical_table=table,
                logical_column=column,
                physical_column=_column(table, column),
            )
            for table, column in sorted(logical_columns)
        ),
        declared_join_ids=declared_join_ids,
    )
    state = _state()
    if return_admission:
        evidence = _column_evidence(_column(*left), "evidence-1")
        certificate_action = ResearchAction(
            action_id="action-evidence-1",
            kind=ResearchActionKind.INSPECT_COLUMN,
            hypothesis_id=None,
            target=evidence.target,
            parameters=(),
            action_digest=canonical_action_digest(
                kind=ResearchActionKind.INSPECT_COLUMN,
                hypothesis_id=None,
                target=evidence.target,
                parameters=(),
                expected_revision=0,
            ),
            expected_revision=0,
        )
        state = apply_research_transition(
            state, certificate_action, evidence=(evidence,)
        ).state
    admission = admit_semantic_turn(
        state,
        decision,
        batch=batch,
        freshness_context=_context(),
        tool_claim=TrustedToolClaim(
            action_id="next-action",
            tool_name="inspect_table",
            target=_table("orders"),
            parameters=(),
        ),
    )
    return admission if return_admission else admission.join_candidates[0]


def test_new_join_between_existing_columns_is_validated_without_assessment() -> None:
    candidate = _join_candidate(
        ("orders", "customer_id"),
        ("customers", "id"),
        "inner",
        (),
    )
    declared = _join_candidate(
        ("orders", "customer_id"),
        ("customers", "id"),
        "inner",
        (),
        (candidate.join_id,),
    )

    assert candidate.status is JoinCandidateStatus.VALIDATED
    assert declared.status is JoinCandidateStatus.VALIDATED

    admission = _join_candidate(
        ("orders", "customer_id"),
        ("customers", "id"),
        "inner",
        (),
        (candidate.join_id,),
        return_admission=True,
    )
    assert commit_semantic_turn(admission).state.join_candidates[0].status is (
        JoinCandidateStatus.VALIDATED
    )


def test_recovery_commits_prepared_validated_join_without_transient_ids() -> None:
    from dataclasses import replace

    candidate = _join_candidate(
        ("orders", "customer_id"),
        ("customers", "id"),
        "inner",
        (),
    )
    admission = _join_candidate(
        ("orders", "customer_id"),
        ("customers", "id"),
        "inner",
        (),
        (candidate.join_id,),
        return_admission=True,
    )

    recovered = commit_semantic_turn(
        replace(admission, declared_join_ids=())
    ).state

    assert recovered.join_candidates[0].status is JoinCandidateStatus.VALIDATED


def test_semantic_reducer_module_exports_closed_admission_api() -> None:
    """The reducer is an explicit pure boundary, not a second state builder."""

    assert TrustedSemanticBatch.__name__ == "TrustedSemanticBatch"
    assert TrustedToolClaim.__name__ == "TrustedToolClaim"
    assert callable(admit_semantic_turn)
    assert callable(commit_semantic_turn)
    assert issubclass(SemanticReducerError, ValueError)


def test_one_admission_commits_exactly_one_action_and_stop_is_inert() -> None:
    state = _state()
    claim = TrustedToolClaim(
        action_id="action-1",
        tool_name="inspect_table",
        target=TableRef(namespace="main", schema=None, table="orders"),
        parameters=(),
    )
    admission = admit_semantic_turn(
        state,
        _tool_decision(),
        batch=TrustedSemanticBatch(
            schema_namespace_version=SCHEMA, tables=(), columns=()
        ),
        freshness_context=_context(),
        tool_claim=claim,
    )

    committed = commit_semantic_turn(admission)

    assert committed.state.revision == 1
    assert len(committed.state.action_history) == committed.state.revision
    assert committed.state.action_history[0].action_id == "action-1"

    stop = _stop_decision()
    stopped = commit_semantic_turn(
        admit_semantic_turn(
            state,
            stop,
            batch=TrustedSemanticBatch(
                schema_namespace_version=SCHEMA, tables=(), columns=()
            ),
            freshness_context=_context(),
        )
    )
    assert stopped.state == state
    assert stopped.state is not state
    assert stopped.transition is None


@pytest.mark.parametrize(
    "state",
    (
        _state().model_copy(update={"revision": 1}),
        _state().model_copy(update={"budget_state": "not-a-budget"}),
    ),
)
def test_stop_revalidates_state_before_return(state: ResearchState) -> None:
    with pytest.raises(SemanticReducerError):
        admit_semantic_turn(
            state,
            _stop_decision(),
            batch=TrustedSemanticBatch(
                schema_namespace_version=SCHEMA,
                tables=(),
                columns=(),
            ),
            freshness_context=_context(),
        )


def test_stop_discards_proposals_without_resolving_or_persisting_them() -> None:
    proposal = {
        "proposal_type": "new_binding",
        "proposal_key": "proposal:ignored",
        "source_id": "source-1",
        "candidate": {
            "kind": "physical_column",
            "physical_column": {"table": "missing", "column": "missing"},
        },
        "join_references": (),
        "citation_evidence_ids": ("missing-evidence",),
    }

    committed = commit_semantic_turn(
        admit_semantic_turn(
            _state(),
            _stop_decision(proposal),
            batch=TrustedSemanticBatch(
                schema_namespace_version=SCHEMA,
                tables=(),
                columns=(),
            ),
            freshness_context=_context(),
        )
    )

    assert committed.state.bindings == ()
    assert committed.state.revision == 0
    assert committed.state.action_history == ()


def test_failed_probe_never_becomes_evidence() -> None:
    state = _state()
    claim = TrustedToolClaim(
        action_id="action-1",
        tool_name="inspect_table",
        target=TableRef(namespace="main", schema=None, table="orders"),
        parameters=(),
    )
    admission = admit_semantic_turn(
        state,
        _tool_decision(),
        batch=TrustedSemanticBatch(
            schema_namespace_version=SCHEMA, tables=(), columns=()
        ),
        freshness_context=_context(),
        tool_claim=claim,
    )
    assert admission.action is not None
    failed = build_probe_result(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        invocation_id="failed-1",
        action_digest=admission.action.action_digest,
        probe_kind=ResearchActionKind.INSPECT_TABLE,
        status=ProbeStatus.FAILED,
        target=admission.action.target,
        started_at=datetime(2026, 7, 31, tzinfo=UTC),
        completed_at=datetime(2026, 7, 31, tzinfo=UTC),
        summary="probe failed",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=0,
        ),
        row_count=0,
        failure_code="failed",
    )

    committed = commit_semantic_turn(admission, probe_result=failed)

    assert committed.state.revision == 1
    assert committed.state.evidence == ()


def test_artifact_backed_success_is_verified_before_atomic_commit() -> None:
    admission = admit_semantic_turn(
        _state(),
        _tool_decision(),
        batch=TrustedSemanticBatch(
            schema_namespace_version=SCHEMA,
            tables=(),
            columns=(),
        ),
        freshness_context=_context(),
        tool_claim=TrustedToolClaim(
            action_id="action-1",
            tool_name="inspect_table",
            target=_table("orders"),
            parameters=(),
        ),
    )
    store = _ArtifactStore()
    result = _artifact_probe(admission, store)
    assert result.artifact_reference is not None

    committed = commit_semantic_turn(
        admission,
        probe_result=result,
        read_artifact=store.read,
    )

    assert committed.state.revision == 1
    assert [item.evidence_id for item in committed.state.evidence] == [
        "artifact-evidence"
    ]


def test_artifact_failure_has_no_partial_transition() -> None:
    admission = admit_semantic_turn(
        _state(),
        _tool_decision(),
        batch=TrustedSemanticBatch(
            schema_namespace_version=SCHEMA,
            tables=(),
            columns=(),
        ),
        freshness_context=_context(),
        tool_claim=TrustedToolClaim(
            action_id="action-1",
            tool_name="inspect_table",
            target=_table("orders"),
            parameters=(),
        ),
    )
    store = _ArtifactStore()
    result = _artifact_probe(admission, store)

    with pytest.raises(ProbeArtifactError, match="requires artifact reader"):
        commit_semantic_turn(admission, probe_result=result)
    with pytest.raises(ProbeArtifactError, match="verification failed"):
        commit_semantic_turn(
            admission,
            probe_result=result,
            read_artifact=lambda reference: b"X" * reference.byte_count,
        )
    with pytest.raises(ProbeArtifactError, match="verification failed"):
        commit_semantic_turn(
            admission,
            probe_result=result,
            read_artifact=lambda _reference: b"short",
        )

    assert admission.state.revision == 0
    assert admission.state.evidence == ()
    assert admission.state.action_history == ()


@pytest.mark.parametrize(
    ("run_id", "revision"),
    (("other-run", 0), (RUN, 1)),
)
def test_cross_run_and_future_revision_results_do_not_partially_commit(
    run_id: str,
    revision: int,
) -> None:
    admission = admit_semantic_turn(
        _state(),
        _tool_decision(),
        batch=TrustedSemanticBatch(
            schema_namespace_version=SCHEMA,
            tables=(),
            columns=(),
        ),
        freshness_context=_context(),
        tool_claim=TrustedToolClaim(
            action_id="action-1",
            tool_name="inspect_table",
            target=_table("orders"),
            parameters=(),
        ),
    )
    assert admission.action is not None
    payload = {"status": "matched"}
    result = build_probe_result(
        run_id=run_id,
        run_incarnation=INCARNATION,
        revision=revision,
        schema_namespace_version=SCHEMA,
        invocation_id="result-1",
        action_digest=admission.action.action_digest,
        probe_kind=admission.action.kind,
        status=ProbeStatus.SUCCESS,
        target=admission.action.target,
        started_at=datetime(2026, 7, 31, tzinfo=UTC),
        completed_at=datetime(2026, 7, 31, tzinfo=UTC),
        summary="result",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=1,
        payload=payload,
    )

    with pytest.raises(ValueError):
        commit_semantic_turn(admission, probe_result=result)

    assert admission.state.revision == 0
    assert admission.state.evidence == ()


def test_derived_expression_certificate_accepts_exact_excerpt_in_document_context() -> None:
    document = DocumentRef(document_id="doc-1", namespace="main")
    evidence = _document_evidence(
        document,
        "context\nrevenue - cost\nmore context",
        "doc-evidence",
    )
    revenue = _column("facts", "revenue")
    cost = _column("facts", "cost")
    expression = DerivedExpressionBinding(
        binding_id="binding-expression",
        source_id="source-1",
        tables=(revenue.table,),
        columns=(revenue, cost),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        expression=ExpressionRef(
            expression_id="expression-1",
            expression="revenue - cost",
        ),
        document=document,
        rule_excerpt="revenue - cost",
        input_columns=(revenue, cost),
    )
    rule = DocumentRuleBinding(
        binding_id="binding-rule",
        source_id="source-1",
        tables=(),
        columns=(),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        document=document,
        rule_id="rule-1",
        rule_text="revenue - cost",
    )

    column_evidence = (
        _column_evidence(revenue, "revenue-evidence"),
        _column_evidence(cost, "cost-evidence"),
    )

    assert _derived_expression_certificate(
        expression, (*column_evidence, evidence)
    ) is True
    assert _derived_expression_certificate(
        expression,
        (evidence,),
        schema_columns=(revenue, cost),
    ) is True
    assert _document_rule_certificate(rule, (evidence,)) is False


def test_document_certificates_allow_only_safe_full_text_normalization() -> None:
    document = DocumentRef(document_id="doc-1", namespace="main")
    evidence = _document_evidence(document, "  revenue\r\n- cost\r\n", "doc-evidence")
    rule = DocumentRuleBinding(
        binding_id="binding-rule",
        source_id="source-1",
        tables=(),
        columns=(),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        document=document,
        rule_id="rule-1",
        rule_text="revenue\n- cost",
    )

    assert _document_rule_certificate(rule, (evidence,)) is True


def test_inner_join_id_is_symmetric_but_left_join_is_directed() -> None:
    forward = _join_candidate(
        ("opaque_a", "x1"),
        ("opaque_b", "y1"),
        "inner",
        ((("opaque_a", "x1"), ("opaque_b", "y1")),),
    )
    reverse = _join_candidate(
        ("opaque_b", "y1"),
        ("opaque_a", "x1"),
        "inner",
        ((("opaque_b", "y1"), ("opaque_a", "x1")),),
    )
    left_forward = _join_candidate(
        ("opaque_a", "x1"),
        ("opaque_b", "y1"),
        "left",
        ((("opaque_a", "x1"), ("opaque_b", "y1")),),
    )
    left_reverse = _join_candidate(
        ("opaque_b", "y1"),
        ("opaque_a", "x1"),
        "left",
        ((("opaque_b", "y1"), ("opaque_a", "x1")),),
    )

    assert forward.join_id == reverse.join_id
    assert forward.path == reverse.path
    assert left_forward.join_id != left_reverse.join_id


def test_inner_composite_reversal_preserves_pair_order() -> None:
    forward_path = (
        (("opaque_a", "x1"), ("opaque_b", "y1")),
        (("opaque_a", "x2"), ("opaque_b", "y2")),
    )
    reverse_path = tuple((right, left) for left, right in forward_path)
    reordered_reverse_path = tuple(reversed(reverse_path))

    forward = _join_candidate(
        ("opaque_a", "x1"),
        ("opaque_b", "y1"),
        "inner",
        forward_path,
    )
    reverse = _join_candidate(
        ("opaque_b", "y1"),
        ("opaque_a", "x1"),
        "inner",
        reverse_path,
    )
    reordered = _join_candidate(
        ("opaque_b", "y1"),
        ("opaque_a", "x1"),
        "inner",
        reordered_reverse_path,
    )

    assert forward.join_id == reverse.join_id
    assert [edge.left.column for edge in forward.path] == ["x1", "x2"]
    assert reordered.join_id != forward.join_id


def test_inner_multi_hop_reverse_has_one_id_and_canonical_path() -> None:
    forward_path = (
        (("opaque_a", "a1"), ("opaque_b", "b1")),
        (("opaque_b", "b2"), ("opaque_c", "c1")),
    )
    reverse_path = (
        (("opaque_c", "c1"), ("opaque_b", "b2")),
        (("opaque_b", "b1"), ("opaque_a", "a1")),
    )

    forward = _join_candidate(
        ("opaque_a", "a1"),
        ("opaque_c", "c1"),
        "inner",
        forward_path,
    )
    reverse = _join_candidate(
        ("opaque_c", "c1"),
        ("opaque_a", "a1"),
        "inner",
        reverse_path,
    )
    different_order = _join_candidate(
        ("opaque_a", "a1"),
        ("opaque_c", "c1"),
        "inner",
        tuple(reversed(forward_path)),
    )

    assert forward.join_id == reverse.join_id
    assert forward.path == reverse.path
    assert [
        (edge.left.table.table, edge.right.table.table) for edge in forward.path
    ] == [("opaque_a", "opaque_b"), ("opaque_b", "opaque_c")]
    assert different_order.join_id != forward.join_id


def test_inner_multi_hop_mixed_edge_orientation_has_canonical_path() -> None:
    forward_path = (
        (("opaque_alpha", "alpha_id"), ("opaque_beta", "alpha_id")),
        (("opaque_beta", "beta_id"), ("opaque_gamma", "beta_id")),
    )
    mixed_path = (
        (("opaque_alpha", "alpha_id"), ("opaque_beta", "alpha_id")),
        (("opaque_gamma", "beta_id"), ("opaque_beta", "beta_id")),
    )

    forward = _join_candidate(
        ("opaque_alpha", "alpha_id"),
        ("opaque_gamma", "beta_id"),
        "inner",
        forward_path,
    )
    mixed = _join_candidate(
        ("opaque_alpha", "alpha_id"),
        ("opaque_gamma", "beta_id"),
        "inner",
        mixed_path,
    )

    assert mixed.path == forward.path
    assert mixed.join_id == forward.join_id


def test_vertical_certificate_requires_the_two_exact_opaque_eav_joins() -> None:
    binding, cited, joins = _vertical_fixture()

    assert _vertical_certificate(binding, cited, joins) is True
    assert _vertical_certificate(binding, cited, {"entity": joins["entity"]}) is False
    assert (
        _vertical_certificate(binding, cited, {"attribute": joins["attribute"]})
        is False
    )

    unrelated_edge = JoinEdge(
        left=binding.entity_key,
        right=binding.value_attribute_key,
        join_type=JoinType.INNER,
    )
    unrelated = joins["entity"].model_copy(
        update={
            "join_id": "join-unrelated",
            "right": binding.value_attribute_key,
            "path": (unrelated_edge,),
        }
    )
    assert (
        _vertical_certificate(
            binding,
            cited,
            {"unrelated": unrelated, "attribute": joins["attribute"]},
        )
        is False
    )


def test_vertical_certificate_accepts_exact_null_value_proof() -> None:
    binding, cited, joins = _vertical_fixture(
        value_operator=PredicateOperator.IS_NULL,
        observed_value=None,
    )

    assert _vertical_certificate(binding, cited, joins) is True


@pytest.mark.parametrize(
    ("observed_value", "include_value_evidence"),
    (("not-null", True), (0, True), (None, False)),
)
def test_vertical_null_certificate_requires_exact_null_value_proof(
    observed_value: str | int | None,
    include_value_evidence: bool,
) -> None:
    binding, cited, joins = _vertical_fixture(
        value_operator=PredicateOperator.IS_NULL,
        observed_value=observed_value,
        include_value_evidence=include_value_evidence,
    )

    assert _vertical_certificate(binding, cited, joins) is False


def test_vertical_null_certificate_requires_all_schema_facts_and_joins() -> None:
    binding, cited, joins = _vertical_fixture(
        value_operator=PredicateOperator.IS_NULL,
        observed_value=None,
    )
    missing_schema = tuple(
        record for record in cited if record.evidence_id != "schema-6"
    )

    assert _vertical_certificate(binding, missing_schema, joins) is False
    assert _vertical_certificate(binding, cited, {"entity": joins["entity"]}) is False


@pytest.mark.parametrize(
    ("name_operator", "value_operator"),
    (
        (PredicateOperator.NEQ, PredicateOperator.EQ),
        (PredicateOperator.LIKE, PredicateOperator.EQ),
        (PredicateOperator.EQ, PredicateOperator.GT),
        (PredicateOperator.IS_NULL, PredicateOperator.EQ),
    ),
)
def test_vertical_certificate_rejects_non_positive_operators(
    name_operator: PredicateOperator,
    value_operator: PredicateOperator,
) -> None:
    binding, cited, joins = _vertical_fixture(
        name_operator=name_operator,
        value_operator=value_operator,
    )

    assert _vertical_certificate(binding, cited, joins) is False


@pytest.mark.parametrize("right", ("opaque-value", ()))
def test_discriminator_certificate_rejects_scalar_or_empty_in_predicate(
    right: object,
) -> None:
    column = _column("f03_x", "k9")
    binding = DiscriminatorValueBinding(
        binding_id="binding-discriminator",
        source_id="source-1",
        tables=(column.table,),
        columns=(column,),
        predicates=(
            PredicateRef(
                left=column,
                operator=PredicateOperator.IN,
                right=right,
            ),
        ),
        join_path=(),
        evidence_ids=("schema",),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=column,
        discriminator_predicate=PredicateRef(
            left=column,
            operator=PredicateOperator.IN,
            right=right,
        ),
    )

    assert (
        _discriminator_certificate(binding, (_column_evidence(column, "schema"),))
        is False
    )


@pytest.mark.parametrize(
    ("operator", "right"),
    (
        (PredicateOperator.GT, 1),
        (PredicateOperator.GTE, 1),
        (PredicateOperator.LT, 1),
        (PredicateOperator.LTE, 1),
        (PredicateOperator.BETWEEN, (1, 2)),
    ),
)
def test_discriminator_certificate_accepts_exact_ordered_values(
    operator: PredicateOperator,
    right: object,
) -> None:
    column = _column("f03_x", "k9")
    predicate = PredicateRef(left=column, operator=operator, right=right)
    values = right if type(right) is tuple else (right,)
    cited = (
        _column_evidence(column, "schema"),
        *(
            _value_evidence(column, value, f"value-{index}")
            for index, value in enumerate(values, start=1)
        ),
    )
    binding = DiscriminatorValueBinding(
        binding_id="binding-discriminator",
        source_id="source-1",
        tables=(column.table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=tuple(record.evidence_id for record in cited),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=column,
        discriminator_predicate=predicate,
    )

    assert _discriminator_certificate(binding, cited) is True


def test_discriminator_assessment_does_not_require_an_observed_matching_row() -> None:
    column = _column("events", "occurred_on")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.LIKE,
        right="2024-06%",
    )
    schema = _column_evidence(column, "schema")
    binding = DiscriminatorValueBinding(
        binding_id="binding-time",
        source_id="source-time",
        tables=(column.table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=(schema.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=column,
        discriminator_predicate=predicate,
    )

    assessed = _assess_binding(
        binding,
        "consistent",
        (schema,),
        {},
    )

    assert assessed.status is BindingStatus.SUPPORTED


@pytest.mark.parametrize("right", (float("nan"), float("inf"), float("-inf")))
def test_discriminator_certificate_rejects_non_finite_literal(right: float) -> None:
    column = _column("events", "measurement")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right=right,
    )
    binding = DiscriminatorValueBinding(
        binding_id="binding-measurement",
        source_id="source-measurement",
        tables=(column.table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=("schema",),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=column,
        discriminator_predicate=predicate,
    )

    assert _discriminator_certificate(
        binding,
        (_column_evidence(column, "schema"),),
    ) is False


@pytest.mark.parametrize(
    ("operator", "right", "expected"),
    (
        (PredicateOperator.EQ, "open", False),
        (PredicateOperator.IN, ("open", "closed"), False),
        (PredicateOperator.IS_NULL, None, False),
        (PredicateOperator.GT, 10, True),
        (PredicateOperator.BETWEEN, (10, 20), True),
    ),
)
def test_discriminator_certificate_requires_value_evidence_only_for_discrete_operators(
    operator: PredicateOperator,
    right: object,
    expected: bool,
) -> None:
    column = _column("events", "measurement")
    predicate = PredicateRef(left=column, operator=operator, right=right)
    binding = DiscriminatorValueBinding(
        binding_id="binding-measurement",
        source_id="source-measurement",
        tables=(column.table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=("schema",),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=column,
        discriminator_predicate=predicate,
    )

    assert _discriminator_certificate(
        binding,
        (),
        schema_columns=(column,),
    ) is expected


def test_discriminator_certificate_rejects_schema_or_probe_literal_without_value_search() -> None:
    column = _column("events", "status")
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right="active",
    )
    binding = DiscriminatorValueBinding(
        binding_id="binding-status",
        source_id="source-status",
        tables=(column.table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=("schema", "probe", "value"),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=column,
        discriminator_predicate=predicate,
    )
    schema = _column_evidence(column, "schema")
    probe = _evidence(
        ResearchActionKind.EXECUTE_PROBE,
        column,
        {"columns": [column.column], "rows": [["active"]]},
        evidence_id="probe",
    )
    value = _value_evidence(column, "active", "value")

    assert _discriminator_certificate(binding, (schema,)) is False
    assert _discriminator_certificate(binding, (schema, probe)) is False
    assert _discriminator_certificate(binding, (schema, value)) is True


@pytest.mark.parametrize(
    ("observed_name", "observed_value"),
    (("wrong", 17), ("opaque-attribute", 99)),
)
def test_vertical_certificate_rejects_wrong_literals(
    observed_name: str,
    observed_value: int,
) -> None:
    binding, cited, joins = _vertical_fixture(
        observed_name=observed_name,
        observed_value=observed_value,
    )

    assert _vertical_certificate(binding, cited, joins) is False


def test_other_four_binding_certificates_use_exact_physical_evidence() -> None:
    column = _column("f03_x", "k9")
    schema = _column_evidence(column, "schema-physical")
    expense_column = _column("f03_x", "k10")
    expense_schema = _column_evidence(expense_column, "schema-expense")
    value = _value_evidence(column, "opaque-value", "exact-value")
    physical = PhysicalColumnBinding(
        binding_id="binding-physical",
        source_id="source-1",
        tables=(column.table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(schema.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )
    predicate = PredicateRef(
        left=column,
        operator=PredicateOperator.EQ,
        right="opaque-value",
    )
    discriminator = DiscriminatorValueBinding(
        binding_id="binding-discriminator",
        source_id="source-1",
        tables=(column.table,),
        columns=(column,),
        predicates=(predicate,),
        join_path=(),
        evidence_ids=(schema.evidence_id, value.evidence_id),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        discriminator_column=column,
        discriminator_predicate=predicate,
    )
    document = DocumentRef(document_id="doc-1", namespace="main")
    expression_evidence = _document_evidence(
        document, "k9 + k10", "expression-doc"
    )
    expression = DerivedExpressionBinding(
        binding_id="binding-expression",
        source_id="source-1",
        tables=(column.table,),
        columns=(column, expense_column),
        predicates=(),
        join_path=(),
        evidence_ids=(
            schema.evidence_id,
            expense_schema.evidence_id,
            expression_evidence.evidence_id,
        ),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
            validator_rule=None,
            expression=ExpressionRef(expression_id="expression-1", expression="k9 + k10"),
            document=document,
            rule_excerpt="k9 + k10",
            input_columns=(column, expense_column),
        )
    rule_evidence = _document_evidence(document, "opaque rule", "rule-doc")
    rule = DocumentRuleBinding(
        binding_id="binding-rule",
        source_id="source-1",
        tables=(),
        columns=(),
        predicates=(),
        join_path=(),
        evidence_ids=(rule_evidence.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        document=document,
        rule_id="rule-1",
        rule_text="opaque rule",
    )

    assert _physical_column_certificate(physical, (schema,)) is True
    assert _discriminator_certificate(discriminator, (schema, value)) is True
    assert (
        _derived_expression_certificate(
            expression, (schema, expense_schema, expression_evidence)
        )
        is True
    )
    assert _document_rule_certificate(rule, (rule_evidence,)) is True


@pytest.mark.parametrize(
    "context",
    (
        FreshnessContext(
            evaluated_at=datetime(2026, 7, 31, tzinfo=UTC),
            run_id=RUN,
            run_incarnation=INCARNATION,
            schema_namespace_version=SCHEMA,
            document_sources=(
                DocumentSourceState(
                    document_id="doc-1",
                    availability=DocumentSourceAvailability.AVAILABLE,
                    source_version="v2",
                ),
            ),
        ),
        FreshnessContext(
            evaluated_at=datetime(2026, 7, 31, tzinfo=UTC),
            run_id=RUN,
            run_incarnation=INCARNATION,
            schema_namespace_version=SCHEMA,
            document_sources=(
                DocumentSourceState(
                    document_id="doc-1",
                    availability=DocumentSourceAvailability.UNAVAILABLE,
                    source_version=None,
                ),
            ),
        ),
        FreshnessContext(
            evaluated_at=datetime(2026, 7, 31, tzinfo=UTC),
            run_id=RUN,
            run_incarnation=INCARNATION,
            schema_namespace_version=SCHEMA,
            document_sources=(
                DocumentSourceState(
                    document_id="doc-1",
                    availability=DocumentSourceAvailability.AVAILABLE,
                    source_version="v1",
                ),
            ),
        ),
    ),
)
def test_assessment_rejects_stale_unavailable_and_expired_document_evidence(
    context: FreshnessContext,
) -> None:
    document = DocumentRef(document_id="doc-1", namespace="main")
    evidence = _document_evidence(
        document,
        "exact rule",
        "doc-evidence",
        valid_until="2026-07-30T00:00:00Z",
    )
    records = {evidence.evidence_id: evidence}

    with pytest.raises(SemanticReducerError, match="not fresh"):
        _cited_records(
            (evidence.evidence_id,),
            records,
            _fresh_evidence(records, context),
        )


def test_cross_run_freshness_context_is_rejected_before_tool_admission() -> None:
    context = _context().model_copy(update={"run_id": "other-run"})

    with pytest.raises(SemanticReducerError, match="run does not match"):
        admit_semantic_turn(
            _state(),
            _tool_decision(),
            batch=TrustedSemanticBatch(
                schema_namespace_version=SCHEMA,
                tables=(),
                columns=(),
            ),
            freshness_context=context,
            tool_claim=TrustedToolClaim(
                action_id="action-1",
                tool_name="inspect_table",
                target=_table("orders"),
                parameters=(),
            ),
        )


def test_truncated_structural_absence_is_not_negative_evidence() -> None:
    target = _table("missing")
    evidence = _evidence(
        ResearchActionKind.INSPECT_TABLE,
        target,
        {"status": "missing"},
        evidence_id="truncated-missing",
        truncated=True,
    )
    hypothesis = Hypothesis(
        hypothesis_id="hypothesis-1",
        source_ids=("source-1",),
        claim="missing table",
        candidate_targets=(target,),
        status=HypothesisStatus.PROPOSED,
        evidence_ids=(),
    )

    assert _negative_hypothesis_certificate(hypothesis, (evidence,)) is False


def test_sample_absence_cannot_reject_a_binding() -> None:
    column = _column("orders", "status")
    sample = _evidence(
        ResearchActionKind.SAMPLE_ROWS,
        column.table,
        {"columns": [column.column], "rows": []},
        evidence_id="empty-sample",
    )
    binding = PhysicalColumnBinding(
        binding_id="binding-1",
        source_id="source-1",
        tables=(column.table,),
        columns=(column,),
        predicates=(),
        join_path=(),
        evidence_ids=(sample.evidence_id,),
        confidence=0.0,
        status=BindingStatus.CANDIDATE,
        validator_rule=None,
        physical_column=column,
    )

    with pytest.raises(SemanticReducerError, match="no permitted certificate"):
        _assess_binding(binding, "contradicted", (sample,), {})


def test_join_certificate_requires_exact_declared_ordered_composite_pairs() -> None:
    left_one = _column("parent", "a")
    left_two = _column("parent", "b")
    right_one = _column("child", "x")
    right_two = _column("child", "y")
    join = JoinCandidate(
        join_id="join-composite",
        left=left_one,
        right=right_one,
        join_type=JoinType.INNER,
        path=(
            JoinEdge(left=left_one, right=right_one, join_type=JoinType.INNER),
            JoinEdge(left=left_two, right=right_two, join_type=JoinType.INNER),
        ),
        status=JoinCandidateStatus.CANDIDATE,
        evidence_ids=(),
    )

    def relationship(
        evidence_id: str,
        kind: str,
        pairs: list[dict[str, str]],
        *,
        from_table: str = "parent",
        to_table: str = "child",
    ):
        return _evidence(
            ResearchActionKind.INSPECT_RELATIONSHIPS,
            left_one.table,
            {
                "status": "connected",
                "depth": 1,
                "relationships": [
                    {
                        "relationship_kind": kind,
                        "from_table": from_table,
                        "to_table": to_table,
                        "column_pairs": pairs,
                    }
                ],
            },
            evidence_id=evidence_id,
        )

    exact_pairs = [
        {"from_column": "a", "to_column": "x"},
        {"from_column": "b", "to_column": "y"},
    ]
    exact = relationship("declared-exact", "declared", exact_pairs)
    inferred = relationship("inferred", "inferred", exact_pairs)
    reordered = relationship(
        "declared-reordered", "declared", list(reversed(exact_pairs))
    )
    reversed_pairs = [
        {"from_column": "x", "to_column": "a"},
        {"from_column": "y", "to_column": "b"},
    ]
    reversed_inner = relationship(
        "declared-reversed-inner",
        "declared",
        reversed_pairs,
        from_table="child",
        to_table="parent",
    )
    reversed_reordered = relationship(
        "declared-reversed-reordered",
        "declared",
        list(reversed(reversed_pairs)),
        from_table="child",
        to_table="parent",
    )
    reversed_mixed = relationship(
        "declared-reversed-mixed",
        "declared",
        [reversed_pairs[0], exact_pairs[1]],
        from_table="child",
        to_table="parent",
    )
    left_join = JoinCandidate(
        join_id="join-composite-left",
        left=left_one,
        right=right_one,
        join_type=JoinType.LEFT,
        path=(
            JoinEdge(left=left_one, right=right_one, join_type=JoinType.LEFT),
            JoinEdge(left=left_two, right=right_two, join_type=JoinType.LEFT),
        ),
        status=JoinCandidateStatus.CANDIDATE,
        evidence_ids=(),
    )

    assert _declared_join_certificate(join, exact) is True
    assert _declared_join_certificate(join, inferred) is False
    assert _declared_join_certificate(join, reordered) is False
    assert _declared_join_certificate(join, reversed_inner) is True
    assert _declared_join_certificate(join, reversed_reordered) is False
    assert _declared_join_certificate(join, reversed_mixed) is False
    assert _declared_join_certificate(left_join, reversed_inner) is False


def test_new_binding_cannot_be_assessed_in_the_same_decision() -> None:
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:new-binding",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {"table": "logical", "column": "value"},
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "proposed",
                        "proposal_key": "proposal:new-binding",
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": ("evidence-1",),
                },
            ),
            "next": _tool_decision().next.model_dump(mode="python"),
        }
    )

    with pytest.raises(SemanticReducerError, match="same decision"):
        admit_semantic_turn(
            _state(),
            decision,
            batch=TrustedSemanticBatch(
                schema_namespace_version=SCHEMA,
                tables=(),
                columns=(
                    ResolvedColumn(
                        logical_table="logical",
                        logical_column="value",
                        physical_column=_column("opaque", "c1"),
                    ),
                ),
            ),
            freshness_context=_context(),
            tool_claim=TrustedToolClaim(
                action_id="action-1",
                tool_name="inspect_table",
                target=_table("orders"),
                parameters=(),
            ),
        )


def test_new_discriminator_binding_preserves_exact_query_predicate() -> None:
    state = _state()
    semantic_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "exact_physical_predicate": True,
            "operator": PredicateOperator.EQ,
            "literal_or_reference": "2042-03-04 05:06:07.0",
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (semantic_item,)}
            )
        }
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:exact-filter",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "logical",
                            "column": "recorded_at",
                        },
                        "discriminator_predicate": {
                            "left": {"table": "logical", "column": "recorded_at"},
                            "operator": PredicateOperator.EQ,
                            "right": "2042-03-04 05:06:07",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            ),
            "next": _tool_decision().next.model_dump(mode="python"),
        }
    )

    admission = admit_semantic_turn(
        state,
        decision,
        batch=TrustedSemanticBatch(
            schema_namespace_version=SCHEMA,
            tables=(),
            columns=(
                ResolvedColumn(
                    logical_table="logical",
                    logical_column="recorded_at",
                    physical_column=_column("records", "recorded_at"),
                ),
            ),
        ),
        freshness_context=_context(),
        tool_claim=TrustedToolClaim(
            action_id="action-1",
            tool_name="inspect_table",
            target=_table("orders"),
            parameters=(),
        ),
    )

    binding = admission.bindings[0]
    assert isinstance(binding, DiscriminatorValueBinding)
    assert binding.discriminator_predicate.right == "2042-03-04 05:06:07.0"
    assert binding.predicates[0] == binding.discriminator_predicate


def test_resolved_physical_id_collision_is_rejected_deterministically() -> None:
    proposals = tuple(
        {
            "proposal_type": "new_binding",
            "proposal_key": f"proposal:binding-{index}",
            "source_id": "source-1",
            "candidate": {
                "kind": "physical_column",
                "physical_column": {"table": f"logical-{index}", "column": "value"},
            },
            "join_references": (),
            "citation_evidence_ids": (f"evidence-{index}",),
        }
        for index in (1, 2)
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": proposals,
            "next": _tool_decision().next.model_dump(mode="python"),
        }
    )
    physical = _column("opaque", "c1")
    batch = TrustedSemanticBatch(
        schema_namespace_version=SCHEMA,
        tables=(),
        columns=tuple(
            ResolvedColumn(
                logical_table=f"logical-{index}",
                logical_column="value",
                physical_column=physical,
            )
            for index in (1, 2)
        ),
    )

    with pytest.raises(SemanticReducerError, match="duplicate binding"):
        admit_semantic_turn(
            _state(),
            decision,
            batch=batch,
            freshness_context=_context(),
            tool_claim=TrustedToolClaim(
                action_id="action-1",
                tool_name="inspect_table",
                target=_table("orders"),
                parameters=(),
            ),
        )


def test_semantic_commit_prepares_one_action_without_a_probe() -> None:
    certificate_target = _column("orders", "id")
    certificate_action = ResearchAction(
        action_id="action-evidence-1",
        kind=ResearchActionKind.INSPECT_COLUMN,
        hypothesis_id=None,
        target=certificate_target,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_COLUMN,
            hypothesis_id=None,
            target=certificate_target,
            parameters=(),
            expected_revision=0,
        ),
        expected_revision=0,
    )
    predecessor = apply_research_transition(
        _state(), certificate_action, evidence=(_column_evidence(certificate_target, "evidence-1"),)
    ).state
    decision = ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (
                {
                    "proposal_type": "new_hypothesis",
                    "proposal_key": "proposal:orders",
                    "source_ids": ("source-1",),
                    "claim": "orders are relevant",
                    "candidate_targets": ({"target_kind": "table", "table": "orders"},),
                    "citation_evidence_ids": ("evidence-1",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    admission = admit_semantic_turn(
        predecessor,
        decision,
        batch=TrustedSemanticBatch(
            schema_namespace_version=SCHEMA,
            tables=(ResolvedTable(logical_table="orders", physical_table=_table("orders")),),
            columns=(),
        ),
        freshness_context=_context(),
    )
    committed = commit_semantic_turn(admission)

    assert admission.action is not None
    assert admission.action.kind is ResearchActionKind.SEMANTIC_COMMIT
    assert admission.action.target is None
    assert committed.state.revision == predecessor.revision + 1
    assert committed.transition is not None
