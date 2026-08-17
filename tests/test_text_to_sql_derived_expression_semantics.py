"""D1 contract for one safe derived-expression semantic item."""

import pytest

import custom_tools.text_to_sql.adaptive.semantic_plan as semantic_plan
from custom_tools.text_to_sql.adaptive._semantic_coverage_footprint import (
    canonical_binding,
)
from custom_tools.text_to_sql.adaptive.checks import SemanticCheckInput
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    CheckFailureCode,
    CheckStatus,
    ColumnRef,
    DocumentRef,
    DerivedExpressionBinding,
    ExpectedResultShape,
    ExpressionRef,
    JoinCandidate,
    JoinEdge,
    JoinType,
    PhysicalColumnBinding,
    PredicateOperator,
    RepairKind,
    ResearchActionKind,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    SqlCandidate,
)
from custom_tools.text_to_sql.adaptive.semantic_checks import (
    evaluate_semantic_authority_checks,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import validate_coverage_inputs
from custom_tools.text_to_sql.adaptive.semantic_plan import build_semantic_ast
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from custom_tools.text_to_sql.adaptive.freshness import (
    DocumentSourceAvailability,
    DocumentSourceState,
)
from tests.text_to_sql_semantic_coverage_helpers import (
    INCARNATION,
    RUN_ID,
    _column,
    _context,
    _document_evidence,
    _join_candidate,
    _schema_evidence,
    _state,
)


SQLITE_DSN = "sqlite://"


def _derived_case(
    sql: str,
    *,
    candidate_id: str = "derived-candidate",
    require_gross_metric: bool = False,
    expression: str = "gross_value - expense_value",
    physical_operand_names: tuple[str, ...] = ("gross_value", "expense_value"),
    physical_input_columns: tuple[ColumnRef, ...] | None = None,
    join_path: tuple[JoinEdge, ...] = (),
    joins: tuple[JoinCandidate, ...] = (),
    expected_result_shape: ExpectedResultShape = ExpectedResultShape.ROWS,
    preserve_binding_evidence_order: bool = False,
    semantic_kind: SemanticItemKind = SemanticItemKind.FORMULA,
    literal_or_reference: object = None,
):
    input_columns = physical_input_columns or tuple(
        _column("measure_record", name) for name in physical_operand_names
    )
    evidence_ids = tuple(
        f"evidence-{column.table.table}-{column.column}" for column in input_columns
    )
    relationship_evidence = tuple(
        _schema_evidence(
            f"evidence-{join.join_id}-relationship",
            join.left.table,
            kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        )
        for join in joins
    )
    rule_excerpt = "Documented formula rule."
    document_evidence = _document_evidence(
        "evidence-derived-rule",
        content=rule_excerpt,
    )
    validated_joins = tuple(
        join.model_copy(update={"evidence_ids": (evidence.evidence_id,)})
        for join, evidence in zip(joins, relationship_evidence, strict=True)
    )
    gross = input_columns[0]
    binding = canonical_binding(
        DerivedExpressionBinding(
            binding_id="binding-net-contribution",
            source_id="net-contribution",
            tables=tuple(dict.fromkeys(column.table for column in input_columns)),
            columns=input_columns,
            predicates=(),
            join_path=join_path,
            evidence_ids=(*evidence_ids, document_evidence.evidence_id),
            confidence=1.0,
            status=BindingStatus.SUPPORTED,
            validator_rule="derived-expression",
            expression=ExpressionRef(
                expression_id="net-contribution-expression",
                expression=expression,
            ),
            document=DocumentRef(document_id="coverage-document", namespace="main"),
            rule_excerpt=rule_excerpt,
            input_columns=input_columns,
        )
    )
    if preserve_binding_evidence_order:
        binding = binding.model_copy(
            update={"evidence_ids": tuple(reversed(binding.evidence_ids))}
        )
    gross_metric_binding = PhysicalColumnBinding(
        binding_id="binding-gross-metric",
        source_id="gross-metric",
        tables=(gross.table,),
        columns=(gross,),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence_ids[0],),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="physical-column",
        physical_column=gross,
    )
    state = _state(
        item_specs=(
            (
                "net-contribution",
                True,
                SemanticItemStatus.RESOLVED,
                (binding.binding_id,),
            ),
        ),
        bindings=(binding, gross_metric_binding)
        if require_gross_metric
        else (binding,),
        evidence=(
            *(
                _schema_evidence(evidence_id, column)
                for evidence_id, column in zip(
                    evidence_ids,
                    input_columns,
                    strict=True,
                )
            ),
            *relationship_evidence,
            document_evidence,
        ),
        joins=validated_joins,
    )
    formula = SemanticItem(
        source_id="net-contribution",
        kind=semantic_kind,
        source_text="net contribution",
        normalized_meaning="gross value minus expense value",
        required=True,
        operator=None,
        literal_or_reference=literal_or_reference,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=(binding.binding_id,),
    )
    metric = SemanticItem(
        source_id="gross-metric",
        kind=SemanticItemKind.METRIC,
        source_text="gross",
        normalized_meaning="gross value",
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=(gross_metric_binding.binding_id,),
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "net contribution gross"
                        if require_gross_metric
                        else "net contribution"
                    ),
                    "semantic_items": (
                        (formula, metric) if require_gross_metric else (formula,)
                    ),
                    "expected_result_shape": expected_result_shape,
                }
            )
        }
    )
    requirements = validate_coverage_inputs(
        state,
        _context(
            documents=(
                DocumentSourceState(
                    document_id="coverage-document",
                    availability=DocumentSourceAvailability.AVAILABLE,
                    source_version="v1",
                ),
            )
        ),
        RUN_ID,
        INCARNATION,
    )
    parsed_ast = parse_sql_candidate(sql, SQLITE_DSN, candidate_id)
    candidate = SqlCandidate(
        candidate_id=candidate_id,
        sql=sql,
        normalized_ast_digest=parsed_ast.candidate_digest,
        revision=state.revision,
    )
    semantic_ast = build_semantic_ast(
        candidate,
        parsed_ast,
        state.query_spec,
        requirements,
        "main",
    )
    return SemanticCheckInput(
        semantic_ast=semantic_ast,
        query_spec=state.query_spec,
        requirements=requirements,
    ), state


def test_derived_ordering_expression_reaches_authority_checks() -> None:
    check_input, state = _derived_case(
        "SELECT gross_value FROM measure_record "
        "ORDER BY AVG(gross_value) DESC",
        expression="average gross value",
        physical_operand_names=("gross_value",),
        semantic_kind=SemanticItemKind.ORDERING,
        literal_or_reference="desc",
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)
    ordering_node_ids = {
        ordering.node_id for ordering in check_input.parsed_ast.orderings
    }
    ordering_annotations = tuple(
        annotation
        for annotation in check_input.semantic_ast.coverage.annotations
        if annotation.node_id in ordering_node_ids
        and annotation.source_ids == ("net-contribution",)
    )

    assert result.status is CheckStatus.PASSED
    assert len(ordering_annotations) == 1
    assert set(ordering_annotations[0].evidence_ids) == set(
        check_input.requirements.eligible_evidence_ids
    )


def test_derived_ordering_keeps_unconfirmed_columns_unauthorized() -> None:
    check_input, state = _derived_case(
        "SELECT gross_value FROM measure_record ORDER BY AVG(secret) DESC",
        expression="average gross value",
        physical_operand_names=("gross_value",),
        semantic_kind=SemanticItemKind.ORDERING,
        literal_or_reference="desc",
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.UNAUTHORIZED_COLUMN


def _root_formula_annotations(check_input: SemanticCheckInput):
    root_scopes = {
        scope.scope_id
        for scope in check_input.parsed_ast.scopes
        if scope.parent_scope_id is None
    }
    root_projections = {
        projection.node_id
        for projection in check_input.parsed_ast.projections
        if projection.scope_id in root_scopes
    }
    return tuple(
        annotation
        for annotation in check_input.semantic_ast.coverage.annotations
        if annotation.node_id in root_projections
        and annotation.expression_field == "expression"
        and annotation.expression_index == 0
        and annotation.expression_path == ()
        and annotation.source_ids == ("net-contribution",)
    )


def test_derived_dimension_expression_reaches_authority_checks() -> None:
    check_input, state = _derived_case(
        "SELECT gross_value - expense_value FROM measure_record",
        semantic_kind=SemanticItemKind.DIMENSION,
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert len(_root_formula_annotations(check_input)) == 1


def test_derived_expression_binding_can_authorize_filter_or_time() -> None:
    check_input, state = _derived_case(
        "SELECT gross_value - expense_value FROM measure_record"
    )
    filter_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FILTER,
            "operator": PredicateOperator.EQ,
            "literal_or_reference": "net contribution",
        }
    )
    for item in (filter_item, filter_item.model_copy(update={"kind": SemanticItemKind.TIME})):
        query_spec = state.query_spec.model_copy(update={"semantic_items": (item,)})
        state = state.model_copy(update={"query_spec": query_spec})
        requirements = validate_coverage_inputs(
            state,
            _context(
                documents=(
                    DocumentSourceState(
                        document_id="coverage-document",
                        availability=DocumentSourceAvailability.AVAILABLE,
                        source_version="v1",
                    ),
                )
            ),
            RUN_ID,
            INCARNATION,
        )
        semantic_ast = build_semantic_ast(
            check_input.candidate,
            check_input.parsed_ast,
            query_spec,
            requirements,
            "main",
        )

        result = evaluate_semantic_authority_checks(
            SemanticCheckInput(
                semantic_ast=semantic_ast,
                query_spec=query_spec,
                requirements=requirements,
            ),
            state,
            SQLITE_DSN,
        )

        assert result.status is CheckStatus.PASSED


def test_unknown_limit_uses_positive_outer_candidate_limit_without_binding() -> None:
    _, state = _derived_case("SELECT 1 LIMIT 1")
    limit = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.LIMIT,
            "literal_or_reference": None,
        }
    )
    query_spec = state.query_spec.model_copy(update={"semantic_items": (limit,)})
    state = state.model_copy(update={"query_spec": query_spec})
    requirements = validate_coverage_inputs(
        state,
        _context(
            documents=(
                DocumentSourceState(
                    document_id="coverage-document",
                    availability=DocumentSourceAvailability.AVAILABLE,
                    source_version="v1",
                ),
            )
        ),
        RUN_ID,
        INCARNATION,
    )
    sql = "SELECT 1 LIMIT 1"
    parsed_ast = parse_sql_candidate(sql, SQLITE_DSN, "unknown-limit-candidate")
    candidate = SqlCandidate(
        candidate_id="unknown-limit-candidate",
        sql=sql,
        normalized_ast_digest=parsed_ast.candidate_digest,
        revision=state.revision,
    )

    semantic_ast = build_semantic_ast(
        candidate,
        parsed_ast,
        query_spec,
        requirements,
        "main",
    )

    assert requirements.selected_bindings == ()
    assert (
        evaluate_semantic_authority_checks(
            SemanticCheckInput(
                semantic_ast=semantic_ast,
                query_spec=query_spec,
                requirements=requirements,
            ),
            state,
            SQLITE_DSN,
        ).status
        is CheckStatus.PASSED
    )


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT gross_value - expense_value FROM measure_record",
        "SELECT ( gross_value - expense_value ) AS net FROM measure_record",
        "SELECT m.gross_value - m.expense_value FROM measure_record AS m",
    ),
)
def test_formula_binding_accepts_equivalent_single_table_subtraction(sql: str) -> None:
    check_input, state = _derived_case(sql)

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_formula_binding_accepts_sqlite_mixed_case_input_columns() -> None:
    input_columns = (
        _column("measure_record", "GrossValue"),
        _column("measure_record", "ExpenseValue"),
    )
    check_input, state = _derived_case(
        "SELECT GrossValue - ExpenseValue FROM measure_record",
        expression="GrossValue - ExpenseValue",
        physical_input_columns=input_columns,
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_formula_binding_accepts_same_evidence_in_state_order() -> None:
    check_input, state = _derived_case(
        "SELECT gross_value - expense_value FROM measure_record",
        preserve_binding_evidence_order=True,
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_formula_candidate_accepts_qualified_confirmed_input_columns() -> None:
    left_amount = _column("left_record", "amount")
    right_amount = _column("right_record", "amount")
    join_edge = JoinEdge(
        left=_column("left_record", "id"),
        right=_column("right_record", "id"),
        join_type=JoinType.INNER,
    )
    join = _join_candidate(
        "join-left-right",
        (join_edge,),
        "evidence-left_record-amount",
    )
    exact_input, _ = _derived_case(
        "SELECT a.amount - b.amount FROM left_record AS a "
        "JOIN right_record AS b ON a.id = b.id",
        expression="a.amount - b.amount",
        physical_input_columns=(left_amount, right_amount),
        join_path=(join_edge,),
        joins=(join,),
    )
    reversed_input, _ = _derived_case(
        "SELECT b.amount - a.amount FROM left_record AS a "
        "JOIN right_record AS b ON a.id = b.id",
        expression="a.amount - b.amount",
        physical_input_columns=(left_amount, right_amount),
        join_path=(join_edge,),
        joins=(join,),
    )

    assert len(_root_formula_annotations(reversed_input)) == 1
    assert len(_root_formula_annotations(exact_input)) == 1


@pytest.mark.parametrize(
    "sql",
    (
        "WITH values_source AS ("
        "SELECT gross_value, expense_value FROM measure_record"
        ") SELECT gross_value - expense_value FROM values_source",
        "SELECT gross_value - expense_value FROM ("
        "SELECT gross_value, expense_value FROM measure_record"
        ") AS values_source",
    ),
    ids=("cte", "derived_relation"),
)
def test_formula_binding_accepts_exact_relation_output_formula(sql: str) -> None:
    check_input, _ = _derived_case(sql)

    assert len(_root_formula_annotations(check_input)) == 1


def test_formula_binding_accepts_sqlite_normalized_cte_output_names() -> None:
    input_columns = (
        _column("measure_record", "GrossValue"),
        _column("measure_record", "ExpenseValue"),
    )
    check_input, state = _derived_case(
        "WITH values_source AS ("
        "SELECT GrossValue, ExpenseValue FROM measure_record"
        ") SELECT grossvalue - expensevalue FROM values_source",
        expression="GrossValue - ExpenseValue",
        physical_input_columns=input_columns,
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_formula_candidate_accepts_confirmed_self_join_input_columns() -> None:
    debit = _column("employee", "debit")
    credit = _column("employee", "credit")
    join_edge = JoinEdge(
        left=_column("employee", "id"),
        right=_column("employee", "parent_id"),
        join_type=JoinType.INNER,
    )
    join = _join_candidate(
        "join-employee-self",
        (join_edge,),
        "evidence-employee-debit",
    )
    exact_input, _ = _derived_case(
        "SELECT a.debit - b.credit FROM employee AS a "
        "JOIN employee AS b ON a.id = b.parent_id",
        expression="a.debit - b.credit",
        physical_input_columns=(debit, credit),
        join_path=(join_edge,),
        joins=(join,),
    )
    swapped_input, _ = _derived_case(
        "SELECT b.debit - a.credit FROM employee AS a "
        "JOIN employee AS b ON a.id = b.parent_id",
        expression="a.debit - b.credit",
        physical_input_columns=(debit, credit),
        join_path=(join_edge,),
        joins=(join,),
    )

    assert len(_root_formula_annotations(exact_input)) == 1
    assert len(_root_formula_annotations(swapped_input)) == 1


def test_formula_candidate_accepts_reordered_self_join_sources() -> None:
    debit = _column("employee", "debit")
    credit = _column("employee", "credit")
    join_edge = JoinEdge(
        left=_column("employee", "id"),
        right=_column("employee", "parent_id"),
        join_type=JoinType.INNER,
    )
    join = _join_candidate(
        "join-employee-reordered",
        (join_edge,),
        "evidence-employee-relationship",
    )
    exact_input, _ = _derived_case(
        "SELECT a.debit - b.credit FROM employee AS b "
        "JOIN employee AS a ON a.id = b.parent_id",
        expression="a.debit - b.credit",
        physical_input_columns=(debit, credit),
        join_path=(join_edge,),
        joins=(join,),
    )
    swapped_input, _ = _derived_case(
        "SELECT b.debit - a.credit FROM employee AS b "
        "JOIN employee AS a ON a.id = b.parent_id",
        expression="a.debit - b.credit",
        physical_input_columns=(debit, credit),
        join_path=(join_edge,),
        joins=(join,),
    )

    assert len(_root_formula_annotations(exact_input)) == 1
    assert len(_root_formula_annotations(swapped_input)) == 1


def test_formula_candidate_leaves_correlated_alias_meaning_to_verifier() -> None:
    expression = (
        "a.debit - (SELECT b.credit FROM employee AS b "
        "WHERE b.credit = a.credit)"
    )
    exact_input, _ = _derived_case(
        f"SELECT {expression} FROM employee AS a",
        expression=expression,
        physical_input_columns=(
            _column("employee", "debit"),
            _column("employee", "credit"),
        ),
    )
    wrong_alias_input, _ = _derived_case(
        "SELECT c.debit - (SELECT b.credit FROM employee AS b "
        "WHERE b.credit = c.credit) FROM employee AS c",
        expression=expression,
        physical_input_columns=(
            _column("employee", "debit"),
            _column("employee", "credit"),
        ),
    )
    uncorrelated_input, _ = _derived_case(
        "SELECT a.debit - (SELECT b.credit FROM employee AS b "
        "WHERE b.credit = b.credit) FROM employee AS a",
        expression=expression,
        physical_input_columns=(
            _column("employee", "debit"),
            _column("employee", "credit"),
        ),
    )

    assert len(_root_formula_annotations(exact_input)) == 1
    assert len(_root_formula_annotations(wrong_alias_input)) == 1
    assert len(_root_formula_annotations(uncorrelated_input)) == 1


def test_unrelated_nested_self_join_does_not_change_root_formula_role_matching() -> None:
    check_input, _ = _derived_case(
        "SELECT gross_value - expense_value FROM measure_record "
        "WHERE EXISTS (SELECT 1 FROM measure_record AS x "
        "JOIN measure_record AS y ON x.id = y.parent_id)",
    )

    assert len(_root_formula_annotations(check_input)) == 1


def test_formula_binding_has_no_second_parser_symbol() -> None:
    assert not hasattr(semantic_plan, "parse_and_build_candidate")


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT (gross_value - expense_value) + 1 FROM measure_record",
        "SELECT (gross_value - expense_value) * 0 FROM measure_record",
    ),
)
def test_formula_candidate_allows_nested_expression_for_verifier(sql: str) -> None:
    check_input, state = _derived_case(sql)

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


@pytest.mark.parametrize(
    ("sql", "status", "code"),
    (
        (
            "SELECT gross_value + expense_value FROM measure_record",
            CheckStatus.PASSED,
            None,
        ),
        (
            "SELECT gross_value - 1 FROM measure_record",
            CheckStatus.PASSED,
            None,
        ),
        (
            "SELECT ABS(gross_value) - expense_value FROM measure_record",
            CheckStatus.PASSED,
            None,
        ),
        (
            "SELECT CASE WHEN gross_value > 0 THEN gross_value ELSE expense_value END "
            "FROM measure_record",
            CheckStatus.PASSED,
            None,
        ),
        (
            "SELECT gross_value - (SELECT expense_value FROM measure_record) "
            "FROM measure_record",
            CheckStatus.PASSED,
            None,
        ),
        (
            "SELECT m.gross_value - m.expense_value FROM measure_record AS m "
            "JOIN other_record AS o ON m.record_id = o.record_id",
            CheckStatus.FAILED,
            CheckFailureCode.UNAUTHORIZED_TABLE,
        ),
    ),
)
def test_formula_candidate_checks_confirmed_columns_not_expression_claim(
    sql: str,
    status: CheckStatus,
    code: CheckFailureCode | None,
) -> None:
    check_input, state = _derived_case(sql)

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is status
    assert result.failure_code is code
    if status is CheckStatus.FAILED:
        assert result.repair is not None
        assert result.repair.kind is RepairKind.REVISE_SQL


def test_formula_candidate_leaves_operand_order_to_verifier() -> None:
    check_input, state = _derived_case(
        "SELECT expense_value - gross_value FROM measure_record"
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_formula_binding_accepts_matching_scalar_subquery_expression() -> None:
    expression = "gross_value - (SELECT MAX(expense_value) FROM measure_record)"
    check_input, state = _derived_case(
        f"SELECT {expression} FROM measure_record",
        expression=expression,
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_formula_binding_accepts_matching_derived_relation_subquery_expression() -> None:
    expression = (
        "gross_value - (SELECT d.expense_value FROM ("
        "SELECT expense_value FROM measure_record) AS d)"
    )
    check_input, _ = _derived_case(
        f"SELECT {expression} FROM measure_record",
        expression=expression,
    )

    assert len(_root_formula_annotations(check_input)) == 1


def test_scalar_subquery_candidate_leaves_where_meaning_to_verifier() -> None:
    expression = (
        "gross_value - (SELECT MAX(expense_value) FROM measure_record "
        "WHERE expense_value > 0)"
    )
    exact_input, _ = _derived_case(
        f"SELECT {expression} FROM measure_record",
        expression=expression,
    )
    changed_input, _ = _derived_case(
        "SELECT gross_value - (SELECT MAX(expense_value) FROM measure_record "
        "WHERE expense_value > 1) FROM measure_record",
        expression=expression,
    )

    exact_root_scopes = {
        scope.scope_id
        for scope in exact_input.parsed_ast.scopes
        if scope.parent_scope_id is None
    }
    exact_root_projections = {
        projection.node_id
        for projection in exact_input.parsed_ast.projections
        if projection.scope_id in exact_root_scopes
    }
    changed_root_scopes = {
        scope.scope_id
        for scope in changed_input.parsed_ast.scopes
        if scope.parent_scope_id is None
    }
    changed_root_projections = {
        projection.node_id
        for projection in changed_input.parsed_ast.projections
        if projection.scope_id in changed_root_scopes
    }

    exact_annotations = tuple(
        annotation
        for annotation in exact_input.semantic_ast.coverage.annotations
        if annotation.node_id in exact_root_projections
        and annotation.expression_field == "expression"
        and annotation.expression_index == 0
        and annotation.expression_path == ()
        and annotation.source_ids == ("net-contribution",)
    )
    changed_annotations = tuple(
        annotation
        for annotation in changed_input.semantic_ast.coverage.annotations
        if annotation.node_id in changed_root_projections
        and annotation.expression_field == "expression"
        and annotation.expression_index == 0
        and annotation.expression_path == ()
        and annotation.source_ids == ("net-contribution",)
    )

    assert len(exact_annotations) == 1
    assert len(changed_annotations) == 1


def test_formula_binding_accepts_matching_group_and_order_expression() -> None:
    expression = (
        "CASE WHEN CAST(gross_value AS TEXT) = 'active' "
        "THEN 1 + LENGTH(expense_value) ELSE 0 END"
    )
    matching_input, matching_state = _derived_case(
        f"SELECT {expression} FROM measure_record "
        f"GROUP BY {expression} ORDER BY {expression}",
        expression=expression,
        expected_result_shape=ExpectedResultShape.GROUPED_ROWS,
    )

    matching = evaluate_semantic_authority_checks(matching_input, matching_state, SQLITE_DSN)

    assert matching.status is CheckStatus.PASSED
    assert matching.failure_code is None


def test_formula_binding_leaves_changed_group_expression_to_result_review() -> None:
    expression = (
        "CASE WHEN CAST(gross_value AS TEXT) = 'active' "
        "THEN 1 + LENGTH(expense_value) ELSE 0 END"
    )
    changed_expression = expression.replace("ELSE 0", "ELSE 1")
    check_input, state = _derived_case(
        f"SELECT {expression} FROM measure_record "
        f"GROUP BY {changed_expression} ORDER BY {expression}",
        expression=expression,
        expected_result_shape=ExpectedResultShape.GROUPED_ROWS,
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_formula_binding_leaves_second_changed_group_expression_to_result_review() -> None:
    expression = (
        "CASE WHEN CAST(gross_value AS TEXT) = 'active' "
        "THEN 1 + LENGTH(expense_value) ELSE 0 END"
    )
    changed_expression = expression.replace("ELSE 0", "ELSE 1")
    check_input, state = _derived_case(
        f"SELECT {expression} FROM measure_record "
        f"GROUP BY {expression}, {changed_expression} ORDER BY {expression}",
        expression=expression,
        expected_result_shape=ExpectedResultShape.GROUPED_ROWS,
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_formula_binding_leaves_changed_order_expression_to_result_review() -> None:
    expression = (
        "CASE WHEN CAST(gross_value AS TEXT) = 'active' "
        "THEN 1 + LENGTH(expense_value) ELSE 0 END"
    )
    changed_expression = expression.replace("ELSE 0", "ELSE 1")
    check_input, state = _derived_case(
        f"SELECT {expression} FROM measure_record "
        f"GROUP BY {expression} ORDER BY {changed_expression}",
        expression=expression,
        expected_result_shape=ExpectedResultShape.GROUPED_ROWS,
    )

    result = evaluate_semantic_authority_checks(check_input, state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_formula_candidate_leaves_opaque_operand_order_to_verifier() -> None:
    physical_operand_names = ("c_gross", "c_expense")
    matching_input, matching_state = _derived_case(
        "SELECT c_gross - c_expense FROM measure_record",
        physical_operand_names=physical_operand_names,
    )
    reversed_input, reversed_state = _derived_case(
        "SELECT c_expense - c_gross FROM measure_record",
        physical_operand_names=physical_operand_names,
    )

    matching = evaluate_semantic_authority_checks(matching_input, matching_state, SQLITE_DSN)
    reversed_result = evaluate_semantic_authority_checks(
        reversed_input,
        reversed_state,
        SQLITE_DSN,
    )

    assert matching.status is CheckStatus.PASSED
    assert matching.failure_code is None
    assert reversed_result.status is CheckStatus.PASSED
    assert reversed_result.failure_code is None
