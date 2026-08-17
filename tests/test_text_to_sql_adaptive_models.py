"""Контракты typed-состояния Text-to-SQL должны отвергать неполные факты."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    CheckKind,
    CheckFailureCode,
    CheckRepair,
    CheckResult,
    CheckStatus,
    ColumnRef,
    DerivedExpressionBinding,
    DiscriminatorValueBinding,
    DocumentRef,
    DocumentRuleBinding,
    EvidenceCost,
    EvidenceRecord,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExecutionResult,
    ExpressionRef,
    ExpectedResultShape,
    MissingEvidenceRequest,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    RepairKind,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    ResearchStopReason,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    SolverState,
    SolverAction,
    SolverActionKind,
    SolverStopReason,
    AstNodeAnnotation,
    AstSemanticCoverage,
    SqlCandidate,
    TableRef,
    VerticalAttributeBinding,
)


RUN_ID = "run-1"
QUERY_ID = "query-1"
SCHEMA_VERSION = "schema:0123456789abcdef"
DIGEST = "sha256:0123456789abcdef"
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def table(name: str = "orders") -> TableRef:
    return TableRef(namespace="main", schema=None, table=name)


def column(name: str = "id", table_name: str = "orders") -> ColumnRef:
    return ColumnRef(table=table(table_name), column=name)


def predicate() -> PredicateRef:
    return PredicateRef(
        left=column("status"),
        operator=PredicateOperator.EQ,
        right="paid",
    )


def item(
    *,
    source_id: str = "source-1",
    status: SemanticItemStatus = SemanticItemStatus.UNRESOLVED,
    binding_ids: tuple[str, ...] = (),
) -> SemanticItem:
    return SemanticItem(
        source_id=source_id,
        kind=SemanticItemKind.FILTER,
        source_text="sales",
        normalized_meaning="sales",
        required=True,
        operator=PredicateOperator.EQ,
        literal_or_reference="paid",
        status=status,
        binding_ids=binding_ids,
    )


def query_spec(*, semantic_items: tuple[SemanticItem, ...] | None = None) -> QuerySpec:
    return QuerySpec(
        run_id=RUN_ID,
        run_incarnation="a1b2c3d4e5f60718293a4b5c6d7e8f90",
        revision=0,
        schema_namespace_version=None,
        query_id=QUERY_ID,
        original_text="sales",
        semantic_items=semantic_items or (item(),),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )


def evidence(*, evidence_id: str = "evidence-1") -> EvidenceRecord:
    return EvidenceRecord(
        run_id=RUN_ID,
        run_incarnation="a1b2c3d4e5f60718293a4b5c6d7e8f90",
        revision=1,
        schema_namespace_version=SCHEMA_VERSION,
        evidence_id=evidence_id,
        source_kind=EvidenceSourceKind.SCHEMA,
        target=table(),
        action_digest=DIGEST,
        observation="orders table exists",
        validity_scope=EvidenceValidityScope.SCHEMA_VERSION,
        data_snapshot_token=None,
        observed_at=NOW,
        strength=1.0,
        created_at=NOW,
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=0,
            bytes=0,
        ),
    )


def binding(
    *, source_id: str = "source-1", evidence_ids: tuple[str, ...] = ("evidence-1",)
) -> PhysicalColumnBinding:
    return PhysicalColumnBinding(
        binding_id="binding-1",
        source_id=source_id,
        tables=(table(),),
        columns=(column("status"),),
        predicates=(predicate(),),
        join_path=(),
        evidence_ids=evidence_ids,
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="schema-check",
        physical_column=column("status"),
    )


def solver_state(*, revision: int = 2) -> SolverState:
    query = query_spec().model_copy(
        update={
            "revision": 1,
            "schema_namespace_version": SCHEMA_VERSION,
        }
    )
    candidate = SqlCandidate(
        candidate_id="candidate-1",
        sql="SELECT status FROM orders",
        normalized_ast_digest=DIGEST,
        revision=1,
    )
    return SolverState(
        run_id=RUN_ID,
        run_incarnation="a1b2c3d4e5f60718293a4b5c6d7e8f90",
        revision=revision,
        schema_namespace_version=SCHEMA_VERSION,
        query_spec=query,
        sql_candidates=(candidate,),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(
            SolverAction(
                action_id="action-1",
                kind=SolverActionKind.SQL_CANDIDATE,
                base_revision=revision - 1,
                candidate_id=candidate.candidate_id,
                missing_evidence_request_id=None,
            ),
        ),
        selected_candidate_id=None,
        stop_reason=None,
    )


def test_closed_enums_have_only_the_normative_values() -> None:
    assert {member.value for member in SemanticItemKind} == {
        "metric",
        "dimension",
        "filter",
        "ordering",
        "limit",
        "time",
        "formula",
    }
    assert {member.value for member in SemanticItemStatus} == {
        "unresolved",
        "partially_resolved",
        "resolved",
        "ambiguous",
        "unsupported",
    }
    assert {member.value for member in ResearchActionKind} == {
        "inspect_catalog",
        "inspect_table",
        "inspect_column",
        "inspect_relationships",
        "profile_column",
        "sample_rows",
        "search_value",
        "distinct_values",
        "execute_probe",
        "read_document",
    }
    assert {member.value for member in CheckKind} == {
        "safety",
        "schema",
        "semantic",
        "explain",
        "execution",
    }
    assert {member.value for member in CheckStatus} == {
        "passed",
        "failed",
        "inconclusive",
    }
    assert {member.value for member in RepairKind} == {
        "REVISE_SQL",
        "REQUEST_EVIDENCE",
    }
    assert {member.value for member in CheckFailureCode} == {
        "AST_DIALECT_UNSUPPORTED",
        "AST_PARSE_TIMEOUT",
        "AST_PARSE_FAILED",
        "AST_MULTI_STATEMENT",
        "AST_SHAPE_UNSUPPORTED",
        "CHECK_INPUT_INVALID",
        "CHECK_TIMEOUT",
        "CHECK_MALFORMED",
        "MISSING_FILTER",
        "MISSING_METRIC",
        "GROUPING_MISMATCH",
        "ORDERING_MISMATCH",
        "LIMIT_MISMATCH",
        "RESULT_SHAPE_MISMATCH",
        "UNAUTHORIZED_TABLE",
        "UNAUTHORIZED_COLUMN",
        "UNAUTHORIZED_LITERAL",
        "UNAUTHORIZED_JOIN",
        "EAV_CATALOG_PREDICATE_MISSING",
        "EAV_VALUE_PREDICATE_MISSING",
        "EAV_JOIN_MISMATCH",
        "SAFETY_REJECTED",
        "SCHEMA_REJECTED",
        "EXPLAIN_REJECTED",
        "EXECUTION_REJECTED",
    }
    assert {member.value for member in BindingStatus} == {
        "candidate",
        "supported",
        "rejected",
        "stale",
    }
    assert {member.value for member in ExpectedResultShape} == {
        "scalar",
        "rows",
        "grouped_rows",
        "ranked_rows",
        "time_series",
    }
    assert {member.value for member in EvidenceSourceKind} == {
        "schema",
        "catalog",
        "profile",
        "sample",
        "value_search",
        "probe",
        "document",
    }
    assert {member.value for member in EvidenceValidityScope} == {
        "schema_version",
        "data_snapshot",
        "run_only",
    }
    assert {member.value for member in PredicateOperator} == {
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "not_in",
        "between",
        "like",
        "is_null",
        "is_not_null",
    }


def test_check_result_status_matrix_requires_one_explicit_repair_form() -> None:
    repair = CheckRepair(
        kind=RepairKind.REQUEST_EVIDENCE,
        source_ids=("source-1",),
    )
    failed = CheckResult(
        check_id="check-1",
        candidate_id="candidate-1",
        check_kind=CheckKind.SEMANTIC,
        status=CheckStatus.FAILED,
        failure_code=CheckFailureCode.MISSING_FILTER,
        affected_source_ids=("source-1",),
        affected_ast_node_ids=(),
        observed_error=None,
        repair=repair,
    )
    assert failed.repair == repair

    with pytest.raises(ValidationError, match="exactly one repair"):
        CheckResult(**(failed.model_dump() | {"required_change": "legacy prose"}))
    with pytest.raises(ValidationError, match="affected IDs"):
        CheckResult(**(failed.model_dump() | {"affected_source_ids": ()}))


@pytest.mark.parametrize(
    "failure_code",
    (
        CheckFailureCode.CHECK_TIMEOUT,
        CheckFailureCode.CHECK_MALFORMED,
        CheckFailureCode.CHECK_INPUT_INVALID,
    ),
)
def test_failed_check_rejects_inconclusive_only_codes(
    failure_code: CheckFailureCode,
) -> None:
    with pytest.raises(ValidationError, match="inconclusive-only"):
        CheckResult(
            check_id="check-1",
            candidate_id="candidate-1",
            check_kind=CheckKind.SEMANTIC,
            status=CheckStatus.FAILED,
            failure_code=failure_code,
            affected_source_ids=(),
            affected_ast_node_ids=(),
            observed_error="check did not finish",
            repair=CheckRepair(kind=RepairKind.REVISE_SQL),
        )


@pytest.mark.parametrize(
    "failure_code",
    (
        CheckFailureCode.MISSING_FILTER,
        CheckFailureCode.GROUPING_MISMATCH,
        CheckFailureCode.UNAUTHORIZED_JOIN,
        CheckFailureCode.EAV_JOIN_MISMATCH,
    ),
)
def test_semantic_failures_require_source_or_plan_attribution(
    failure_code: CheckFailureCode,
) -> None:
    with pytest.raises(ValidationError, match="attributed"):
        CheckResult(
            check_id="check-1",
            candidate_id="candidate-1",
            check_kind=CheckKind.SEMANTIC,
            status=CheckStatus.FAILED,
            failure_code=failure_code,
            affected_source_ids=(),
            affected_ast_node_ids=(),
            observed_error=None,
            repair=CheckRepair(kind=RepairKind.REVISE_SQL),
        )


def test_global_failure_may_have_empty_attribution() -> None:
    result = CheckResult(
        check_id="check-1",
        candidate_id="candidate-1",
        check_kind=CheckKind.SAFETY,
        status=CheckStatus.FAILED,
        failure_code=CheckFailureCode.SAFETY_REJECTED,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=None,
        repair=CheckRepair(kind=RepairKind.REVISE_SQL),
    )
    assert result.affected_source_ids == ()
    assert {member.value for member in ResearchStopReason} == {
        "COMPLETE",
        "AMBIGUOUS",
        "UNSUPPORTED",
        "STAGNATED",
        "BUDGET_EXHAUSTED",
        "DEADLINE_EXCEEDED",
        "CANCELLED",
        "TOOL_FAILURE",
        "PROTOCOL_FAILURE",
    }
    assert {member.value for member in SolverStopReason} == {
        "SOLVED",
        "MISSING_EVIDENCE",
        "NO_SAFE_CANDIDATE",
        "STAGNATED",
        "BUDGET_EXHAUSTED",
        "DEADLINE_EXCEEDED",
        "CANCELLED",
        "TOOL_FAILURE",
        "PROTOCOL_FAILURE",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "not valid"),
        ("schema_namespace_version", "not-a-digest"),
        ("query_id", ""),
    ],
)
def test_query_spec_rejects_invalid_ids_and_digest(field: str, value: str) -> None:
    values = {
        "run_id": RUN_ID,
        "run_incarnation": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "revision": 0,
        "schema_namespace_version": None,
        "query_id": QUERY_ID,
        "original_text": "sales",
        "semantic_items": (item(),),
        "expected_result_shape": ExpectedResultShape.ROWS,
        "global_constraints": (),
    }
    values[field] = value
    if field == "schema_namespace_version":
        values["schema_namespace_version"] = value
    with pytest.raises(ValidationError):
        QuerySpec(**values)


def test_run_incarnation_is_a_canonical_uuid_or_hex_identifier() -> None:
    assert query_spec().run_incarnation == "a1b2c3d4e5f60718293a4b5c6d7e8f90"

    values = query_spec().model_dump()
    values["run_incarnation"] = 1
    with pytest.raises(ValidationError, match="run_incarnation"):
        QuerySpec(**values)


def test_query_spec_checks_resolved_binding() -> None:
    with pytest.raises(ValidationError, match="binding_ids"):
        item(status=SemanticItemStatus.RESOLVED)


def test_semantic_item_does_not_require_source_span() -> None:
    semantic_item = SemanticItem(
        source_id="source-unanchored",
        kind=SemanticItemKind.METRIC,
        source_text="shortest player",
        normalized_meaning="minimum player height",
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )

    assert semantic_item.source_text == "shortest player"
    assert not hasattr(semantic_item, "source_span")


def test_evidence_requires_utc_timestamp_and_validity_snapshot_pair() -> None:
    values = evidence().model_dump()
    values["observed_at"] = datetime(2026, 7, 30, 12, 0)
    with pytest.raises(ValidationError, match="UTC"):
        EvidenceRecord(**values)

    values = evidence().model_dump()
    values["validity_scope"] = EvidenceValidityScope.DATA_SNAPSHOT
    values["data_snapshot_token"] = None
    with pytest.raises(ValidationError, match="data_snapshot_token"):
        EvidenceRecord(**values)


def test_all_binding_variants_are_discriminated_and_supported_needs_evidence_and_rule() -> (
    None
):
    vertical = VerticalAttributeBinding(
        binding_id="binding-vertical",
        source_id="source-1",
        tables=(table("customers"), table("property_types"), table("property_values")),
        columns=(column("id", "customers"),),
        predicates=(predicate(),),
        join_path=(),
        evidence_ids=("evidence-1",),
        confidence=0.9,
        status=BindingStatus.SUPPORTED,
        validator_rule="vertical-schema-check",
        entity_table=table("customers"),
        entity_key=column("id", "customers"),
        attribute_catalog_table=table("property_types"),
        attribute_catalog_key=column("id", "property_types"),
        attribute_name_predicate=predicate(),
        value_table=table("property_values"),
        value_entity_key=column("customer_id", "property_values"),
        value_attribute_key=column("property_type_id", "property_values"),
        value_predicate=predicate(),
    )
    assert vertical.kind == "vertical_attribute"

    discriminator = DiscriminatorValueBinding(
        binding_id="binding-discriminator",
        source_id="source-1",
        tables=(table(),),
        columns=(column("status"),),
        predicates=(predicate(),),
        join_path=(),
        evidence_ids=("evidence-1",),
        confidence=0.9,
        status=BindingStatus.SUPPORTED,
        validator_rule="schema-check",
        discriminator_column=column("status"),
        discriminator_predicate=predicate(),
    )
    derived = DerivedExpressionBinding(
        binding_id="binding-derived",
        source_id="source-1",
        tables=(table(),),
        columns=(column("status"), column("previous_status")),
        predicates=(),
        join_path=(),
        evidence_ids=("evidence-1",),
        confidence=0.9,
        status=BindingStatus.SUPPORTED,
        validator_rule="expression-check",
        expression=ExpressionRef(
            expression_id="expression-1", expression="status - previous_status"
        ),
        input_columns=(column("status"), column("previous_status")),
    )
    document = DocumentRuleBinding(
        binding_id="binding-document",
        source_id="source-1",
        tables=(),
        columns=(),
        predicates=(),
        join_path=(),
        evidence_ids=("evidence-1",),
        confidence=0.9,
        status=BindingStatus.SUPPORTED,
        validator_rule="document-check",
        document=DocumentRef(document_id="document-1", namespace="main"),
        rule_id="rule-1",
        rule_text="Status is documented here.",
    )
    assert {discriminator.kind, derived.kind, document.kind} == {
        "discriminator_value",
        "derived_expression",
        "document_rule",
    }

    values = binding().model_dump()
    values["evidence_ids"] = ()
    with pytest.raises(ValidationError, match="evidence_ids"):
        PhysicalColumnBinding(**values)

    values = binding().model_dump()
    values["validator_rule"] = None
    with pytest.raises(ValidationError, match="validator_rule"):
        PhysicalColumnBinding(**values)


@pytest.mark.parametrize(
    "input_names",
    ((), ("gross", "gross")),
)
def test_derived_expression_binding_requires_nonempty_distinct_ordered_inputs(
    input_names: tuple[str, ...],
) -> None:
    inputs = tuple(column(name) for name in input_names)

    with pytest.raises(ValidationError, match="input_columns"):
        DerivedExpressionBinding(
            binding_id="binding-derived",
            source_id="source-1",
            tables=(table(),),
            columns=inputs,
            predicates=(),
            join_path=(),
            evidence_ids=("evidence-1",),
            confidence=0.9,
            status=BindingStatus.SUPPORTED,
            validator_rule="expression-check",
            expression=ExpressionRef(
                expression_id="expression-1", expression="gross - cost"
            ),
            input_columns=inputs,
        )


@pytest.mark.parametrize(
    ("input_names", "expression"),
    (
        (("gross",), "ABS(gross)"),
        (("gross", "cost", "tax"), "(gross - cost) * tax"),
    ),
)
def test_derived_expression_binding_accepts_nonempty_distinct_ordered_inputs(
    input_names: tuple[str, ...],
    expression: str,
) -> None:
    inputs = tuple(column(name) for name in input_names)
    binding = DerivedExpressionBinding(
        binding_id="binding-derived",
        source_id="source-1",
        tables=(table(),),
        columns=inputs,
        predicates=(),
        join_path=(),
        evidence_ids=("evidence-1",),
        confidence=0.9,
        status=BindingStatus.SUPPORTED,
        validator_rule="expression-check",
        expression=ExpressionRef(
            expression_id="expression-1", expression=expression
        ),
        input_columns=inputs,
    )

    replayed = DerivedExpressionBinding.model_validate_json(binding.model_dump_json())

    assert replayed.input_columns == inputs


def test_research_state_checks_references_action_deduplication_and_budget() -> None:
    # Fixture reconciliation: the supported binding must resolve its source item.
    state = ResearchState(
        run_id=RUN_ID,
        run_incarnation="a1b2c3d4e5f60718293a4b5c6d7e8f90",
        revision=1,
        schema_namespace_version=SCHEMA_VERSION,
        query_spec=query_spec(
            semantic_items=(
                item(
                    status=SemanticItemStatus.RESOLVED,
                    binding_ids=("binding-1",),
                ),
            )
        ),
        hypotheses=(),
        evidence=(evidence(),),
        bindings=(binding(),),
        join_candidates=(),
        unresolved_items=(),
        action_history=(
            ResearchAction(
                action_id="action-1",
                kind=ResearchActionKind.INSPECT_TABLE,
                hypothesis_id=None,
                target=table(),
                parameters=(),
                action_digest=DIGEST,
                expected_revision=0,
            ),
        ),
        result_expectations=(),
        budget_state={
            "initial_wall_clock_ms": 10,
            "used_wall_clock_ms": 1,
            "remaining_wall_clock_ms": 9,
            "initial_model_calls": 2,
            "used_model_calls": 1,
            "remaining_model_calls": 1,
            "initial_model_tokens": 3,
            "used_model_tokens": 1,
            "remaining_model_tokens": 2,
            "initial_db_probe_ms": 4,
            "used_db_probe_ms": 1,
            "remaining_db_probe_ms": 3,
            "initial_rows": 5,
            "used_rows": 1,
            "remaining_rows": 4,
            "initial_bytes": 6,
            "used_bytes": 1,
            "remaining_bytes": 5,
        },
        stop_reason=None,
    )
    assert state.bindings[0].binding_id == "binding-1"

    values = state.model_dump()
    values["unresolved_items"] = ("unknown-source",)
    with pytest.raises(ValidationError, match="unresolved_items"):
        ResearchState(**values)

    values = state.model_dump()
    values["action_history"] = values["action_history"] + (
        {**values["action_history"][0], "action_id": "action-2"},
    )
    with pytest.raises(ValidationError, match="action_digest"):
        ResearchState(**values)

    values = state.model_dump()
    values["action_history"] = values["action_history"] + (
        {
            **values["action_history"][0],
            "action_id": "action-2",
            "action_digest": "sha256:abcdef0123456789",
        },
    )
    values["revision"] = 2
    with pytest.raises(ValidationError, match="action_history"):
        ResearchState(**values)

    values = state.model_dump()
    values["budget_state"]["remaining_rows"] = 5
    with pytest.raises(ValidationError, match="remaining_rows"):
        ResearchState(**values)


def test_ast_semantic_coverage_requires_unique_authorized_annotations() -> None:
    annotation = AstNodeAnnotation(
        node_id="node-1",
        source_ids=("source-1",),
        evidence_ids=("evidence-1",),
    )
    coverage = AstSemanticCoverage(
        requirements_digest=DIGEST,
        required_source_ids=("source-1",),
        evidence_ids=("evidence-1",),
        annotations=(annotation,),
    )
    assert coverage.annotations == (annotation,)

    with pytest.raises(ValidationError, match="unique addresses"):
        AstSemanticCoverage(
            requirements_digest=DIGEST,
            required_source_ids=("source-1",),
            evidence_ids=("evidence-1",),
            annotations=(annotation, annotation),
        )

    with pytest.raises(ValidationError, match="exceeds semantic coverage"):
        AstSemanticCoverage(
            requirements_digest=DIGEST,
            required_source_ids=("source-1",),
            evidence_ids=("evidence-1",),
            annotations=(
                annotation.model_copy(update={"source_ids": ("source-2",)}),
            ),
        )


def test_missing_evidence_and_solver_state_link_to_known_contract_items() -> None:
    request = MissingEvidenceRequest(
        run_id=RUN_ID,
        run_incarnation="a1b2c3d4e5f60718293a4b5c6d7e8f90",
        revision=1,
        schema_namespace_version=SCHEMA_VERSION,
        missing_evidence_request_id="request-1",
        source_id="source-1",
        question="Which table stores the status?",
        candidate_targets=(table(),),
        required_evidence_kind=EvidenceSourceKind.SCHEMA,
        reason="no supported binding",
    )
    candidate = SqlCandidate(
        candidate_id="candidate-1",
        sql="SELECT status FROM orders",
        normalized_ast_digest=DIGEST,
        revision=1,
    )
    state = SolverState(
        run_id=RUN_ID,
        run_incarnation="a1b2c3d4e5f60718293a4b5c6d7e8f90",
        revision=2,
        schema_namespace_version=SCHEMA_VERSION,
        query_spec=query_spec().model_copy(
            update={"schema_namespace_version": SCHEMA_VERSION}
        ),
        sql_candidates=(candidate,),
        check_results=(
            CheckResult(
                check_id="check-1",
                candidate_id="candidate-1",
                check_kind=CheckKind.SEMANTIC,
                status=CheckStatus.FAILED,
                failure_code=CheckFailureCode.MISSING_FILTER,
                affected_source_ids=("source-1",),
                affected_ast_node_ids=("node-1",),
                observed_error="status binding is missing",
                required_change="research status",
            ),
        ),
        execution_results=(),
        missing_evidence_requests=(request,),
        action_history=(
            SolverAction(
                action_id="action-1",
                kind=SolverActionKind.SQL_CANDIDATE,
                base_revision=0,
                candidate_id=candidate.candidate_id,
                missing_evidence_request_id=None,
            ),
            SolverAction(
                action_id="action-2",
                kind=SolverActionKind.MISSING_EVIDENCE,
                base_revision=1,
                candidate_id=None,
                missing_evidence_request_id=request.missing_evidence_request_id,
            ),
        ),
        selected_candidate_id=None,
        stop_reason=SolverStopReason.MISSING_EVIDENCE,
    )
    assert state.stop_reason is SolverStopReason.MISSING_EVIDENCE

    values = state.model_dump()
    values["selected_candidate_id"] = "unknown-candidate"
    with pytest.raises(ValidationError, match="selected_candidate_id"):
        SolverState(**values)

    passed = state.check_results[0].model_copy(
        update={
            "status": CheckStatus.PASSED,
            "failure_code": CheckFailureCode.MISSING_FILTER,
        }
    )
    with pytest.raises(ValidationError, match="failure details"):
        CheckResult(**passed.model_dump())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("run_id", "foreign-run", "query_spec"),
        ("run_incarnation", "foreign-incarnation", "query_spec"),
        (
            "schema_namespace_version",
            "schema:fedcba9876543210",
            "schema_namespace_version",
        ),
        ("revision", 3, "revision"),
    ),
)
def test_solver_state_rejects_foreign_or_future_query(
    field: str,
    value: object,
    message: str,
) -> None:
    values = solver_state().model_dump()
    values["query_spec"][field] = value
    with pytest.raises(ValidationError, match=message):
        SolverState(**values)


def test_solver_state_candidate_cannot_be_in_the_future() -> None:
    values = solver_state().model_dump()
    values["sql_candidates"][0]["revision"] = 3
    with pytest.raises(ValidationError, match="revision"):
        SolverState(**values)


def test_solver_state_rejects_duplicate_ast_candidates() -> None:
    state = solver_state()
    second_candidate = state.sql_candidates[0].model_copy(
        update={"candidate_id": "candidate-2"}
    )
    values = state.model_dump()
    values["sql_candidates"] = (
        *values["sql_candidates"],
        second_candidate.model_dump(),
    )

    with pytest.raises(ValidationError, match="normalized_ast_digest"):
        SolverState(**values)


def test_solver_state_rejects_more_than_eight_candidates() -> None:
    state = solver_state()
    values = state.model_dump()
    candidates = []
    for index in range(9):
        digest = f"sha256:{index:016x}"
        candidates.append(
            state.sql_candidates[0]
            .model_copy(
                update={
                    "candidate_id": f"candidate-{index}",
                    "normalized_ast_digest": digest,
                }
            )
            .model_dump()
    )
    values.update(
        sql_candidates=tuple(candidates),
        action_history=(),
    )

    with pytest.raises(ValidationError, match="at most 8"):
        SolverState(**values)


def test_solver_state_rejects_second_execution_for_one_candidate() -> None:
    state = solver_state()
    values = state.model_dump()
    result = ExecutionResult(
        execution_id="execution-1",
        candidate_id="candidate-1",
        success=True,
        row_count=1,
        elapsed_ms=1,
        error_code=None,
    )
    values["execution_results"] = (
        result.model_dump(),
        result.model_copy(update={"execution_id": "execution-2"}).model_dump(),
    )

    with pytest.raises(ValidationError, match="one ExecutionResult"):
        SolverState(**values)


def test_solver_state_action_history_allows_gate_gaps_and_references_subjects() -> None:
    values = solver_state().model_dump()
    values["revision"] = 3
    assert SolverState(**values).action_history[0].base_revision == 1

    values = solver_state().model_dump()
    values["action_history"][0]["candidate_id"] = "unknown-candidate"
    with pytest.raises(ValidationError, match="candidate_id"):
        SolverState(**values)

    state = solver_state()
    request = MissingEvidenceRequest(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=3,
        schema_namespace_version=state.schema_namespace_version,
        missing_evidence_request_id="request-1",
        source_id="source-1",
        question="Which source is authoritative?",
        candidate_targets=(),
        required_evidence_kind=EvidenceSourceKind.SCHEMA,
        reason="Missing evidence.",
    )
    complete_values = state.model_dump()
    complete_values.update(
        revision=3,
        missing_evidence_requests=(request.model_dump(),),
        action_history=(
            *complete_values["action_history"],
            SolverAction(
                action_id="action-2",
                kind=SolverActionKind.MISSING_EVIDENCE,
                base_revision=2,
                candidate_id=None,
                missing_evidence_request_id=request.missing_evidence_request_id,
            ).model_dump(),
        ),
    )
    assert SolverState(**complete_values).revision == 3

    values = SolverState(**complete_values).model_dump()
    values["action_history"][1]["missing_evidence_request_id"] = "unknown-request"
    with pytest.raises(ValidationError, match="missing_evidence_request_id"):
        SolverState(**values)

    values = SolverState(**complete_values).model_dump()
    values["action_history"] = values["action_history"][:1]
    with pytest.raises(ValidationError, match="exactly cover MissingEvidenceRequest"):
        SolverState(**values)

    values = SolverState(**complete_values).model_dump()
    values["revision"] = 4
    values["action_history"] = (
        *values["action_history"],
        SolverAction(
            action_id="action-3",
            kind=SolverActionKind.MISSING_EVIDENCE,
            base_revision=3,
            candidate_id=None,
            missing_evidence_request_id=request.missing_evidence_request_id,
        ).model_dump(),
    )
    with pytest.raises(ValidationError, match="exactly cover MissingEvidenceRequest"):
        SolverState(**values)


@pytest.mark.parametrize(
    ("base_revision", "message"),
    ((1, "strictly increasing"), (0, "strictly increasing"), (3, "precede")),
)
def test_solver_state_rejects_duplicate_out_of_order_or_future_action_revision(
    base_revision: int,
    message: str,
) -> None:
    state = solver_state()
    request = MissingEvidenceRequest(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=3,
        schema_namespace_version=state.schema_namespace_version,
        missing_evidence_request_id="request-1",
        source_id="source-1",
        question="Which source is authoritative?",
        candidate_targets=(),
        required_evidence_kind=EvidenceSourceKind.SCHEMA,
        reason="Missing evidence.",
    )
    values = state.model_dump()
    values.update(
        revision=3,
        missing_evidence_requests=(request.model_dump(),),
        action_history=(
            *values["action_history"],
            SolverAction(
                action_id="action-2",
                kind=SolverActionKind.MISSING_EVIDENCE,
                base_revision=base_revision,
                candidate_id=None,
                missing_evidence_request_id=request.missing_evidence_request_id,
            ).model_dump(),
        ),
    )

    with pytest.raises(ValidationError, match=message):
        SolverState(**values)


def test_solver_state_rejects_missing_or_duplicate_sql_action_subject() -> None:
    values = solver_state().model_dump()
    values["action_history"] = ()
    with pytest.raises(ValidationError, match="exactly cover SqlCandidate"):
        SolverState(**values)

    values = solver_state().model_dump()
    values["revision"] = 3
    values["action_history"] = (
        *values["action_history"],
        SolverAction(
            action_id="action-2",
            kind=SolverActionKind.SQL_CANDIDATE,
            base_revision=2,
            candidate_id="candidate-1",
            missing_evidence_request_id=None,
        ).model_dump(),
    )
    with pytest.raises(ValidationError, match="exactly cover SqlCandidate"):
        SolverState(**values)


@pytest.mark.parametrize(
    ("kind", "candidate_id", "request_id"),
    (
        (SolverActionKind.SQL_CANDIDATE, None, None),
        (SolverActionKind.SQL_CANDIDATE, "candidate-1", "request-1"),
        (SolverActionKind.MISSING_EVIDENCE, "candidate-1", "request-1"),
        (SolverActionKind.MISSING_EVIDENCE, None, None),
    ),
)
def test_solver_action_requires_exact_kind_subject(
    kind: SolverActionKind,
    candidate_id: str | None,
    request_id: str | None,
) -> None:
    with pytest.raises(ValidationError, match="requires"):
        SolverAction(
            action_id="action-1",
            kind=kind,
            base_revision=0,
            candidate_id=candidate_id,
            missing_evidence_request_id=request_id,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("run_id", "foreign-run", "same run"),
        ("run_incarnation", "foreign-incarnation", "same run"),
        (
            "schema_namespace_version",
            "schema:fedcba9876543210",
            "schema_namespace_version",
        ),
        ("revision", 3, "revision"),
        ("source_id", "foreign-source", "source_id"),
    ),
)
def test_solver_missing_evidence_request_matches_state_authority(
    field: str,
    value: object,
    message: str,
) -> None:
    state = solver_state()
    request = MissingEvidenceRequest(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=1,
        schema_namespace_version=state.schema_namespace_version,
        missing_evidence_request_id="request-1",
        source_id="source-1",
        question="Which binding is authoritative?",
        candidate_targets=(table(),),
        required_evidence_kind=EvidenceSourceKind.SCHEMA,
        reason="missing evidence",
    )
    values = state.model_dump()
    raw_request = request.model_dump()
    raw_request[field] = value
    values["missing_evidence_requests"] = (raw_request,)

    with pytest.raises(ValidationError, match=message):
        SolverState(**values)


def test_contracts_are_frozen_and_reject_unknown_fields() -> None:
    spec = query_spec()
    with pytest.raises(ValidationError):
        spec.revision = 2  # type: ignore[misc]

    with pytest.raises(ValidationError):
        TableRef(namespace="main", schema=None, table="orders", unknown=True)
