"""Typed authority checks return one closed deterministic result."""

from dataclasses import replace

import pytest

from custom_tools.text_to_sql.adaptive.checks import SemanticCheckInput
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    CheckStatus,
    ExpectedResultShape,
    JoinCandidate,
    JoinCandidateStatus,
    JoinEdge,
    PredicateOperator,
    RepairKind,
    ResearchState,
    SemanticItemKind,
    SemanticItemStatus,
    SqlCandidate,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.semantic_checks import (
    _validate_requirements_state_membership,
    evaluate_semantic_authority_checks,
)
from custom_tools.text_to_sql.adaptive._semantic_coverage_footprint import (
    requirements_digest,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageRequirements,
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.semantic_plan import (
    build_semantic_ast,
    collect_ast_columns,
)
from custom_tools.text_to_sql.adaptive._sql_ast_identity import semantic_candidate_digest
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from text_to_sql_semantic_checks_helpers import (
POSTGRES_DSN,
    ItemSpec,
    build_case,
    build_state,
    build_vertical_state,
    column,
    inner_join,
)
from text_to_sql_semantic_coverage_helpers import (
    INCARNATION,
    RUN_ID,
    _context,
    _value_evidence,
)


SQLITE_DSN = "sqlite:////tmp/text2sql-semantic-checks.db"


def _evaluate(case):
    return evaluate_semantic_authority_checks(
        case.check_input,
        case.state,
        POSTGRES_DSN,
    )


def _item(
    source_id: str,
    kind: SemanticItemKind,
    column: str,
    *,
    table: str = "orders",
    schema: str | None = None,
    operator: PredicateOperator | None = None,
    literal: object = None,
    join_path=(),
) -> ItemSpec:
    return ItemSpec(
        source_id=source_id,
        kind=kind,
        table=table,
        column=column,
        schema=schema,
        operator=operator,
        literal=literal,
        join_path=join_path,
    )


def test_root_transform_required_limit_fails_closed_without_rewrite() -> None:
    sql = "PIVOT sales ON category USING SUM(amount)"
    base = build_case(
        "SELECT 1 LIMIT 1",
        (
            _item("category", SemanticItemKind.DIMENSION, "category", table="sales"),
            _item("amount", SemanticItemKind.METRIC, "amount", table="sales"),
            _item("limit", SemanticItemKind.LIMIT, "amount", table="sales", literal=1),
        ),
    )
    query_spec = base.query_spec.model_copy(
        update={
            "semantic_items": tuple(
                item.model_copy(
                    update={"status": SemanticItemStatus.RESOLVED, "binding_ids": ()}
                )
                if item.source_id == "limit"
                else item
                for item in base.query_spec.semantic_items
            )
        }
    )
    payload = base.state.model_dump(mode="python")
    payload.update(
        query_spec=query_spec,
        bindings=tuple(binding for binding in base.state.bindings if binding.source_id != "limit"),
        evidence=tuple(
            evidence
            for evidence in base.state.evidence
            if evidence.evidence_id != "evidence-limit-schema"
        ),
        unresolved_items=(),
    )
    state = type(base.state).model_validate(payload)
    requirements = validate_coverage_inputs(state, _context(), RUN_ID, INCARNATION)
    parsed = parse_sql_candidate(sql, "duckdb://", "root-pivot-limit")
    candidate = SqlCandidate(
        candidate_id="root-pivot-limit",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    result = evaluate_semantic_authority_checks(
        SemanticCheckInput(
            semantic_ast=build_semantic_ast(
                candidate, parsed, query_spec, requirements, "main"
            ),
            query_spec=query_spec,
            requirements=requirements,
        ),
        state,
        "duckdb://",
    )

    assert result.failure_code is CheckFailureCode.LIMIT_MISMATCH
    assert candidate.sql == sql


ALLOWED_JOIN = inner_join("orders", "customer_id", "customers", "id")


@pytest.mark.parametrize(
    "sql",
    [
        "WITH named(x) AS (SELECT o.id FROM orders o) SELECT n.x FROM named n",
        "SELECT d.x FROM (SELECT o.id FROM orders o) AS d(x)",
        "SELECT d.x FROM orders o CROSS JOIN LATERAL (SELECT o.id) AS d(x)",
    ],
)
def test_relation_column_aliases_preserve_underlying_physical_authority(sql: str) -> None:
    case = build_case(sql, (_item("id", SemanticItemKind.DIMENSION, "id"),))

    assert _evaluate(case).status is CheckStatus.PASSED


def test_qualify_generic_option_keeps_literal_authority():
    case = build_case(
        "SELECT o.status, ROW_NUMBER() OVER (ORDER BY o.status) AS rn FROM orders o "
        "QUALIFY o.status = 'forged'",
        (
            _item(
                "status",
                SemanticItemKind.FILTER,
                "status",
                operator=PredicateOperator.EQ,
                literal="active",
            ),
        ),
    )

    result = _evaluate(case)

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL


def test_generic_join_option_keeps_literal_authority():
    case = build_case(
        "SELECT o.status FROM orders AS o CROSS JOIN orders AS b",
        (_item("status", SemanticItemKind.DIMENSION, "status"),),
    )
    option = parse_sql_candidate(
        "SELECT o.status, ROW_NUMBER() OVER (ORDER BY o.status) AS rn "
        "FROM orders AS o QUALIFY o.status = 'forged'",
        POSTGRES_DSN,
        "join-option",
    ).scopes[0].options
    assert option is not None
    parsed_ast = replace(
        case.check_input.parsed_ast,
        joins=(replace(case.check_input.parsed_ast.joins[0], options=option),),
    )
    parsed_ast = replace(
        parsed_ast,
        candidate_digest=semantic_candidate_digest(parsed_ast),
    )
    candidate = case.check_input.candidate.model_copy(
        update={"normalized_ast_digest": parsed_ast.candidate_digest}
    )
    semantic_ast = build_semantic_ast(
        candidate,
        parsed_ast,
        case.query_spec,
        case.requirements,
        "main",
    )

    result = evaluate_semantic_authority_checks(
        SemanticCheckInput(
            semantic_ast=semantic_ast,
            query_spec=case.query_spec,
            requirements=case.requirements,
        ),
        case.state,
        POSTGRES_DSN,
    )

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL
    assert result.affected_ast_node_ids == (parsed_ast.joins[0].node_id,)


def test_pivot_input_columns_remain_in_physical_authority_collection() -> None:
    parsed_ast = parse_sql_candidate(
        "SELECT p.a FROM sales PIVOT "
        "(SUM(amount) FOR category IN ('A' AS a)) AS p",
        POSTGRES_DSN,
        "pivot-authority",
    )
    relation = parsed_ast.expression_relations[0]
    input_relation_id = relation.input_relation_ids[0]
    columns = collect_ast_columns(
        parsed_ast,
        {input_relation_id: TableRef(namespace="main", schema=None, table="sales")},
    )

    assert {(item.column.table.table, item.column.column) for item in columns.occurrences} == {
        ("sales", "amount"),
        ("sales", "category"),
    }


def test_root_unpivot_declared_outputs_are_not_physical_columns() -> None:
    parsed_ast = parse_sql_candidate(
        "UNPIVOT monthly_sales ON jan, feb INTO NAME month VALUE sales",
        "duckdb://",
        "unpivot-authority",
    )
    relation = parsed_ast.expression_relations[0]
    input_relation_id = relation.input_relation_ids[0]
    columns = collect_ast_columns(
        parsed_ast,
        {
            input_relation_id: TableRef(
                namespace="main", schema=None, table="monthly_sales"
            )
        },
    )

    assert relation.column_aliases == ("month", "sales")
    assert {(item.column.table.table, item.column.column) for item in columns.occurrences} == {
        ("monthly_sales", "jan"),
        ("monthly_sales", "feb"),
    }


def test_root_unpivot_physical_inputs_pass_semantic_authority() -> None:
    case = build_case(
        "UNPIVOT monthly_sales ON jan, feb INTO NAME month VALUE sales",
        (
            _item("jan", SemanticItemKind.DIMENSION, "jan", table="monthly_sales"),
            _item("feb", SemanticItemKind.DIMENSION, "feb", table="monthly_sales"),
        ),
        dsn="duckdb://",
    )

    result = evaluate_semantic_authority_checks(
        case.check_input,
        case.state,
        "duckdb://",
    )

    assert result.status is CheckStatus.PASSED


@pytest.mark.parametrize(
    "sql, failure_code",
    [
        (
            "SELECT o.id FROM orders o WHERE o.id = ANY "
            "(SELECT s.order_id FROM secrets s WHERE s.order_id = o.id)",
            CheckFailureCode.UNAUTHORIZED_TABLE,
        ),
        (
            "SELECT o.id FROM orders o WHERE o.id = ANY "
            "(SELECT i.secret FROM orders i WHERE i.id = o.id)",
            CheckFailureCode.UNAUTHORIZED_COLUMN,
        ),
        (
            "SELECT o.id FROM orders o GROUP BY o.secret",
            CheckFailureCode.UNAUTHORIZED_COLUMN,
        ),
    ],
)
def test_authority_checks_traverse_quantified_subqueries_and_generic_grouping(
    sql: str, failure_code: CheckFailureCode
) -> None:
    case = build_case(sql, (_item("id", SemanticItemKind.DIMENSION, "id"),))

    result = _evaluate(case)

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is failure_code
    if "GROUP BY" in sql:
        assert result.affected_ast_node_ids == (case.check_input.parsed_ast.groupings[0].node_id,)


def test_unique_authority_schema_resolves_an_unqualified_table() -> None:
    case = build_case(
        "SELECT items.name FROM items LIMIT 5",
        (
            _item(
                "name",
                SemanticItemKind.DIMENSION,
                "name",
                table="items",
                schema="main",
            ),
            _item(
                "limit",
                SemanticItemKind.LIMIT,
                "name",
                table="items",
                schema="main",
                literal=5,
            ),
        ),
    )

    result = _evaluate(case)

    assert result.status is CheckStatus.PASSED
    annotation = next(
        item
        for item in case.check_input.semantic_ast.coverage.annotations
        if item.source_ids == ("name",)
    )
    assert annotation.evidence_ids == ("evidence-name-schema",)


def test_sqlite_unique_casefold_table_matches_authorized_spelling() -> None:
    case = build_case(
        "SELECT Player.player_name FROM Player LIMIT 1",
        (
            _item("name", SemanticItemKind.DIMENSION, "player_name", table="Player"),
            _item(
                "limit",
                SemanticItemKind.LIMIT,
                "player_name",
                table="Player",
                literal=1,
            ),
        ),
        dsn=SQLITE_DSN,
    )

    result = evaluate_semantic_authority_checks(case.check_input, case.state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED


def test_sqlite_unqualified_intermediate_join_table_is_authorized() -> None:
    customer_join = JoinEdge(
        left=column("transactions_1k", "CustomerID", schema="main"),
        right=column("yearmonth", "CustomerID", schema="main"),
    )
    product_join = JoinEdge(
        left=column("products", "ProductID", schema="main"),
        right=column("transactions_1k", "ProductID", schema="main"),
    )
    case = build_case(
        "SELECT products.Description FROM yearmonth "
        "JOIN transactions_1k "
        "ON transactions_1k.CustomerID = yearmonth.CustomerID "
        "JOIN products ON products.ProductID = transactions_1k.ProductID",
        (
            _item(
                "description",
                SemanticItemKind.DIMENSION,
                "Description",
                table="products",
                schema="main",
                join_path=(product_join, customer_join),
            ),
        ),
        dsn=SQLITE_DSN,
    )

    result = evaluate_semantic_authority_checks(case.check_input, case.state, SQLITE_DSN)

    assert result.status is CheckStatus.PASSED


def test_sqlite_ambiguous_casefold_table_stays_unauthorized() -> None:
    ambiguous_join = inner_join("Player", "player_api_id", "pLaYeR", "player_api_id")
    case = build_case(
        "SELECT PLAYER.player_name FROM PLAYER LIMIT 1",
        (
            _item("first", SemanticItemKind.DIMENSION, "player_name", table="Player"),
            _item(
                "second",
                SemanticItemKind.DIMENSION,
                "player_name",
                table="pLaYeR",
                join_path=(ambiguous_join,),
            ),
            _item(
                "limit",
                SemanticItemKind.LIMIT,
                "player_name",
                table="Player",
                literal=1,
            ),
        ),
        dsn=SQLITE_DSN,
    )

    result = evaluate_semantic_authority_checks(case.check_input, case.state, SQLITE_DSN)

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.UNAUTHORIZED_TABLE


def test_postgres_case_mismatch_table_stays_unauthorized() -> None:
    case = build_case(
        "SELECT player.player_name FROM player LIMIT 1",
        (
            _item("name", SemanticItemKind.DIMENSION, "player_name", table="Player"),
            _item(
                "limit",
                SemanticItemKind.LIMIT,
                "player_name",
                table="Player",
                literal=1,
            ),
        ),
    )

    result = _evaluate(case)

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.UNAUTHORIZED_TABLE


def test_physical_formula_binding_is_authorized() -> None:
    case = build_case(
        "SELECT p.height FROM players AS p",
        (
            _item(
                "height",
                SemanticItemKind.FORMULA,
                "height",
                table="players",
            ),
        ),
    )

    result = _evaluate(case)

    assert result.status is CheckStatus.PASSED


def _formula_predicate_binding_case(*, vertical: bool):
    if vertical:
        state = build_vertical_state()
        sql = (
            "SELECT v.value FROM customers AS c "
            "JOIN attribute_values AS v ON c.id = v.customer_id "
            "JOIN attributes AS a ON a.id = v.attribute_id "
            "WHERE a.name = 'membership_level' AND v.value = 'premium'"
        )
    else:
        state = build_state(
            (
                _item(
                    "formula",
                    SemanticItemKind.FILTER,
                    "status",
                    operator=PredicateOperator.EQ,
                    literal="active",
                ),
            )
        )
        sql = "SELECT SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) FROM orders"
    formula = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "operator": None,
            "literal_or_reference": None,
        }
    )
    state = state.model_copy(
        update={"query_spec": state.query_spec.model_copy(update={"semantic_items": (formula,)})}
    )
    requirements = validate_coverage_inputs(state, _context(), RUN_ID, INCARNATION)
    parsed_ast = parse_sql_candidate(sql, POSTGRES_DSN, "formula-predicate")
    candidate = SqlCandidate(
        candidate_id="formula-predicate",
        sql=sql,
        normalized_ast_digest=parsed_ast.candidate_digest,
        revision=state.revision,
    )
    semantic_ast = build_semantic_ast(
        candidate, parsed_ast, state.query_spec, requirements, "main"
    )
    return SemanticCheckInput(
        semantic_ast=semantic_ast,
        query_spec=state.query_spec,
        requirements=requirements,
    ), state


@pytest.mark.parametrize("vertical", (False, True), ids=("discriminator", "vertical"))
def test_formula_allows_confirmed_predicate_binding(vertical: bool) -> None:
    check_input, state = _formula_predicate_binding_case(vertical=vertical)

    result = evaluate_semantic_authority_checks(check_input, state, POSTGRES_DSN)

    assert result.status is CheckStatus.PASSED


def _non_predicate_binding_case(
    state: ResearchState,
    sql: str,
    kind: SemanticItemKind,
    candidate_id: str,
) -> tuple[SemanticCheckInput, ResearchState]:
    item = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": kind,
            "operator": None,
            "literal_or_reference": None,
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            )
        }
    )
    requirements = validate_coverage_inputs(state, _context(), RUN_ID, INCARNATION)
    parsed_ast = parse_sql_candidate(sql, POSTGRES_DSN, candidate_id)
    candidate = SqlCandidate(
        candidate_id=candidate_id,
        sql=sql,
        normalized_ast_digest=parsed_ast.candidate_digest,
        revision=state.revision,
    )
    semantic_ast = build_semantic_ast(
        candidate, parsed_ast, state.query_spec, requirements, "main"
    )
    return (
        SemanticCheckInput(
            semantic_ast=semantic_ast,
            query_spec=state.query_spec,
            requirements=requirements,
        ),
        state,
    )


def test_metric_discriminator_binding_annotates_conditional_count() -> None:
    check_input, state = _non_predicate_binding_case(
        build_state(
            (
                _item(
                    "active",
                    SemanticItemKind.FILTER,
                    "status",
                    operator=PredicateOperator.EQ,
                    literal="active",
                ),
            )
        ),
        "SELECT COUNT(CASE WHEN o.status = 'active' THEN 1 END) FROM orders AS o",
        SemanticItemKind.METRIC,
        "metric-discriminator-conditional-count",
    )

    (aggregate,) = check_input.parsed_ast.aggregates
    assert {
        (
            annotation.node_id,
            annotation.expression_field,
            annotation.expression_index,
            tuple(
                (segment.argument, segment.ordinal)
                for segment in annotation.expression_path
            ),
        )
        for annotation in check_input.semantic_ast.coverage.annotations
        if "active" in annotation.source_ids
    } == {
        (
            aggregate.node_id,
            "expression",
            0,
            (("this", 0), ("ifs", 0), ("this", 0)),
        )
    }
    assert (
        evaluate_semantic_authority_checks(check_input, state, POSTGRES_DSN).status
        is CheckStatus.PASSED
    )


def test_dimension_vertical_binding_annotates_existing_predicates() -> None:
    check_input, state = _non_predicate_binding_case(
        build_vertical_state(),
        "SELECT v.value FROM customers AS c "
        "JOIN attribute_values AS v ON c.id = v.customer_id "
        "JOIN attributes AS a ON a.id = v.attribute_id "
        "WHERE a.name = 'membership_level' AND v.value = 'premium'",
        SemanticItemKind.DIMENSION,
        "dimension-vertical-predicates",
    )

    atom_ids_by_literal = {
        next(
            dict(child.attributes)["value"]
            for argument, _, child in atom.expression.children
            if argument == "expression" and child.kind == "literal"
        ): atom.node_id
        for predicate in check_input.parsed_ast.predicates
        for atom in predicate.atoms
        if any(
            argument == "expression" and child.kind == "literal"
            for argument, _, child in atom.expression.children
        )
    }
    assert {
        annotation.node_id: (
            annotation.expression_field,
            annotation.expression_index,
            tuple(
                (segment.argument, segment.ordinal)
                for segment in annotation.expression_path
            ),
        )
        for annotation in check_input.semantic_ast.coverage.annotations
        if "membership" in annotation.source_ids
    } == {
        atom_ids_by_literal["membership_level"]: ("expression", 0, ()),
        atom_ids_by_literal["premium"]: ("expression", 0, ()),
    }
    assert (
        evaluate_semantic_authority_checks(check_input, state, POSTGRES_DSN).status
        is CheckStatus.PASSED
    )


def test_time_series_authority_does_not_depend_on_result_shape() -> None:
    case = build_case(
        "SELECT o.id FROM orders AS o",
        (_item("id", SemanticItemKind.DIMENSION, "id"),),
        shape=ExpectedResultShape.TIME_SERIES,
    )

    result = _evaluate(case)

    assert result.status is CheckStatus.PASSED


def test_cross_join_without_required_path_is_not_unresolved() -> None:
    case = build_case(
        "SELECT a.id, b.id FROM alpha AS a CROSS JOIN beta AS b",
        (_item("alpha-id", SemanticItemKind.DIMENSION, "id", table="alpha"),),
    )

    assert _evaluate(case).failure_code is CheckFailureCode.UNAUTHORIZED_TABLE


def test_cross_join_with_authorized_endpoints_is_not_blocked_by_join_path() -> None:
    required_join = inner_join("alpha", "id", "beta", "id")
    case = build_case(
        "SELECT a.id, b.id FROM alpha AS a CROSS JOIN beta AS b",
        (
            _item("alpha-id", SemanticItemKind.DIMENSION, "id", table="alpha"),
            _item(
                "beta-id",
                SemanticItemKind.DIMENSION,
                "id",
                table="beta",
                join_path=(required_join,),
            ),
        ),
    )

    result = _evaluate(case)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_reversed_join_evidence_ids_ignore_unrelated_candidate() -> None:
    case = build_case(
        "SELECT o.customer_id, c.id FROM orders AS o "
        "JOIN customers AS c ON o.customer_id = c.id",
        (
            _item("customer-fk", SemanticItemKind.DIMENSION, "customer_id"),
            _item(
                "customer-id",
                SemanticItemKind.DIMENSION,
                "id",
                table="customers",
                join_path=(ALLOWED_JOIN,),
            ),
        ),
    )
    raw_join = case.state.join_candidates[0].model_copy(
        update={
            "evidence_ids": (
                "evidence-join-1",
                "evidence-customer-id-schema",
            )
        }
    )
    validated_state = case.state.model_copy(update={"join_candidates": (raw_join,)})
    requirements = CoverageRequirements.model_validate(
        validate_coverage_inputs(
            validated_state, _context(), RUN_ID, INCARNATION
        ).model_dump(
            mode="python"
        )
    )
    unrelated_candidate = JoinCandidate(
        join_id="unrelated-candidate",
        left=ALLOWED_JOIN.left,
        right=ALLOWED_JOIN.right,
        join_type=ALLOWED_JOIN.join_type,
        path=(),
        status=JoinCandidateStatus.CANDIDATE,
        evidence_ids=(),
    )
    state = validated_state.model_copy(
        update={"join_candidates": (raw_join, unrelated_candidate)}
    )

    _validate_requirements_state_membership(
        tuple(item for item in state.query_spec.semantic_items if item.required),
        requirements,
        state,
    )


def test_required_source_without_selected_binding_cannot_build_semantic_ast() -> None:
    case = build_case(
        "SELECT o.amount FROM orders AS o",
        (_item("amount", SemanticItemKind.METRIC, "amount"),),
    )
    payload = case.requirements.model_dump(mode="python")
    payload.update(
        {
            "selected_bindings": (),
            "eligible_validated_joins": (),
            "eligible_evidence_ids": (),
            "allowed_tables": (),
            "allowed_columns": (),
            "allowed_predicates": (),
            "allowed_join_paths": (),
        }
    )
    unsigned = CoverageRequirements.model_construct(**payload)
    payload["requirements_digest"] = requirements_digest(unsigned)
    empty_requirements = CoverageRequirements.model_validate(payload)
    semantic_ast = case.check_input.semantic_ast

    with pytest.raises(ValueError, match="selected bindings do not exactly cover"):
        build_semantic_ast(
            semantic_ast.candidate,
            semantic_ast.parsed_ast,
            case.query_spec,
            empty_requirements,
            "main",
        )


def test_ambiguous_authority_schemas_leave_an_unqualified_table_unauthorized() -> None:
    authority_join = JoinEdge(
        left=column("items", "name", schema="main"),
        right=column("items", "name", schema="archive"),
    )
    case = build_case(
        "SELECT items.name FROM items LIMIT 5",
        (
            _item(
                "main-name",
                SemanticItemKind.DIMENSION,
                "name",
                table="items",
                schema="main",
            ),
            _item(
                "archive-name",
                SemanticItemKind.DIMENSION,
                "name",
                table="items",
                schema="archive",
                join_path=(authority_join,),
            ),
            _item(
                "limit",
                SemanticItemKind.LIMIT,
                "name",
                table="items",
                schema="main",
                literal=5,
            ),
        ),
    )

    assert _evaluate(case).failure_code is CheckFailureCode.UNAUTHORIZED_TABLE


def test_explicit_schema_is_not_remapped_to_authorized_schema() -> None:
    case = build_case(
        "SELECT other.items.name FROM other.items LIMIT 5",
        (
            _item(
                "name",
                SemanticItemKind.DIMENSION,
                "name",
                table="items",
                schema="main",
            ),
            _item(
                "limit",
                SemanticItemKind.LIMIT,
                "name",
                table="items",
                schema="main",
                literal=5,
            ),
        ),
    )

    assert _evaluate(case).failure_code is CheckFailureCode.UNAUTHORIZED_TABLE


def test_sqlite_mixed_case_physical_and_filter_columns_are_authorized() -> None:
    case = build_case(
        "SELECT Currency FROM customers WHERE Currency = 'EUR'",
        (
            _item("currency", SemanticItemKind.DIMENSION, "Currency", table="customers"),
            _item(
                "currency-filter",
                SemanticItemKind.FILTER,
                "Currency",
                table="customers",
                operator=PredicateOperator.EQ,
                literal="EUR",
            ),
        ),
        dsn="sqlite://",
    )

    result = evaluate_semantic_authority_checks(case.check_input, case.state, "sqlite://")

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_sqlite_casefolded_validated_join_endpoint_is_authorized() -> None:
    join = inner_join("customers", "CustomerID", "yearmonth", "CustomerID")
    case = build_case(
        "SELECT ym.Consumption FROM customers AS c JOIN yearmonth AS ym "
        "ON c.CustomerID = ym.customerid",
        (
            _item("segment", SemanticItemKind.DIMENSION, "Segment", table="customers"),
            _item(
                "consumption",
                SemanticItemKind.METRIC,
                "Consumption",
                table="yearmonth",
                join_path=(join,),
            ),
        ),
        dsn="sqlite://",
    )

    result = evaluate_semantic_authority_checks(case.check_input, case.state, "sqlite://")

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_sqlite_casefolding_does_not_authorize_unrelated_join_column() -> None:
    join = inner_join("customers", "CustomerID", "yearmonth", "CustomerID")
    case = build_case(
        "SELECT ym.Consumption FROM customers AS c JOIN yearmonth AS ym "
        "ON c.otherid = ym.CustomerID",
        (
            _item("segment", SemanticItemKind.DIMENSION, "Segment", table="customers"),
            _item(
                "consumption",
                SemanticItemKind.METRIC,
                "Consumption",
                table="yearmonth",
                join_path=(join,),
            ),
        ),
        dsn="sqlite://",
    )

    result = evaluate_semantic_authority_checks(case.check_input, case.state, "sqlite://")

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.UNAUTHORIZED_COLUMN


def test_postgres_casefolded_validated_join_endpoint_stays_unauthorized() -> None:
    join = inner_join("customers", "CustomerID", "yearmonth", "CustomerID")
    case = build_case(
        "SELECT ym.Consumption FROM customers AS c JOIN yearmonth AS ym "
        "ON c.CustomerID = ym.customerid",
        (
            _item("segment", SemanticItemKind.DIMENSION, "Segment", table="customers"),
            _item(
                "consumption",
                SemanticItemKind.METRIC,
                "Consumption",
                table="yearmonth",
                join_path=(join,),
            ),
        ),
    )

    assert _evaluate(case).failure_code is CheckFailureCode.UNAUTHORIZED_COLUMN


def test_postgres_mixed_case_physical_column_remains_unauthorized() -> None:
    case = build_case(
        "SELECT currency FROM customers",
        (_item("currency", SemanticItemKind.DIMENSION, "Currency", table="customers"),),
    )

    assert _evaluate(case).failure_code is CheckFailureCode.UNAUTHORIZED_COLUMN


def test_authority_checks_literals_in_conditional_aggregate() -> None:
    case = build_case(
        "SELECT COUNT(CASE WHEN o.status = 'active' THEN o.id END), "
        "COUNT(CASE WHEN o.status = 'inactive' THEN o.id END) FROM orders AS o",
        (
            _item(
                "active",
                SemanticItemKind.FILTER,
                "status",
                operator=PredicateOperator.EQ,
                literal="active",
            ),
            _item(
                "inactive",
                SemanticItemKind.FILTER,
                "status",
                operator=PredicateOperator.EQ,
                literal="inactive",
            ),
            _item("id", SemanticItemKind.METRIC, "id"),
        ),
        shape=ExpectedResultShape.SCALAR,
        dsn="sqlite://",
    )

    result = evaluate_semantic_authority_checks(case.check_input, case.state, "sqlite://")

    assert case.check_input.parsed_ast.predicates == ()
    assert {source_id for annotation in case.check_input.semantic_ast.coverage.annotations for source_id in annotation.source_ids}.issuperset({"active", "inactive"})
    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None


def test_authority_gate_rejects_incomplete_filter_but_rejects_unknown_literal() -> None:
    missing_filter = build_case(
        "SELECT o.status FROM orders AS o",
        (
            _item(
                "active",
                SemanticItemKind.FILTER,
                "status",
                operator=PredicateOperator.EQ,
                literal="active",
            ),
        ),
    )
    unknown_literal = build_case(
        "SELECT o.status FROM orders AS o WHERE o.status = 'inactive'",
        (
            _item(
                "active",
                SemanticItemKind.FILTER,
                "status",
                operator=PredicateOperator.EQ,
                literal="active",
            ),
        ),
    )

    assert _evaluate(missing_filter).status is CheckStatus.INCONCLUSIVE
    assert _evaluate(missing_filter).failure_code is CheckFailureCode.CHECK_INPUT_INVALID
    assert (
        _evaluate(unknown_literal).failure_code
        is CheckFailureCode.UNAUTHORIZED_LITERAL
    )


def test_authority_gate_checks_literals_inside_any_expression() -> None:
    items = (
        _item(
            "eur",
            SemanticItemKind.FILTER,
            "currency",
            operator=PredicateOperator.EQ,
            literal="EUR",
        ),
        _item(
            "czk",
            SemanticItemKind.FILTER,
            "currency",
            operator=PredicateOperator.EQ,
            literal="CZK",
        ),
        _item("amount", SemanticItemKind.METRIC, "amount"),
    )
    allowed = build_case(
        "SELECT SUM(CASE WHEN o.currency = 'EUR' THEN o.amount "
        "WHEN o.currency = 'CZK' THEN o.amount ELSE 0 END) FROM orders AS o",
        items,
    )
    unknown = build_case(
        "SELECT SUM(CASE WHEN o.currency = 'UNKNOWN' THEN o.amount "
        "WHEN o.currency = 'CZK' THEN o.amount ELSE 0 END) FROM orders AS o",
        items,
    )

    assert _evaluate(allowed).status is CheckStatus.PASSED
    assert (
        _evaluate(unknown).failure_code
        is CheckFailureCode.UNAUTHORIZED_LITERAL
    )


@pytest.mark.parametrize(
    ("literal", "expected_status", "expected_failure"),
    (
        ("small_business", CheckStatus.PASSED, None),
        (
            "enterprise",
            CheckStatus.FAILED,
            CheckFailureCode.UNAUTHORIZED_LITERAL,
        ),
    ),
)
def test_eligible_exact_value_evidence_authorizes_equality_literal(
    literal: str,
    expected_status: CheckStatus,
    expected_failure: CheckFailureCode | None,
) -> None:
    state = build_state(
        (
            _item(
                "active",
                SemanticItemKind.FILTER,
                "status",
                operator=PredicateOperator.EQ,
                literal="active",
            ),
            _item("tier", SemanticItemKind.DIMENSION, "tier"),
        )
    )
    evidence_id = "evidence-tier-exact-value"
    tier_column = column("orders", "tier")
    payload = state.model_dump(mode="python")
    payload.update(
        evidence=(*state.evidence, _value_evidence(evidence_id, tier_column, "small_business")),
        bindings=tuple(
            binding.model_copy(
                update={"evidence_ids": (*binding.evidence_ids, evidence_id)}
            )
            if binding.source_id == "tier"
            else binding
            for binding in state.bindings
        ),
    )
    state = ResearchState.model_validate(payload)
    requirements = validate_coverage_inputs(state, _context(), RUN_ID, INCARNATION)
    sql = (
        "SELECT o.tier FROM orders AS o "
        f"WHERE o.status = 'active' AND o.tier = '{literal}'"
    )
    parsed_ast = parse_sql_candidate(sql, POSTGRES_DSN, f"exact-value-{literal}")
    candidate = SqlCandidate(
        candidate_id=f"exact-value-{literal}",
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

    result = evaluate_semantic_authority_checks(
        SemanticCheckInput(
            semantic_ast=semantic_ast,
            query_spec=state.query_spec,
            requirements=requirements,
        ),
        state,
        POSTGRES_DSN,
    )

    assert evidence_id in requirements.eligible_evidence_ids
    assert result.status is expected_status
    assert result.failure_code is expected_failure


def test_exact_value_evidence_does_not_authorize_join_only_column() -> None:
    join = inner_join("accounts", "AccountID", "events", "AccountID")
    state = build_state(
        (
            _item("account", SemanticItemKind.DIMENSION, "name", table="accounts"),
            _item(
                "amount",
                SemanticItemKind.METRIC,
                "amount",
                table="events",
                join_path=(join,),
            ),
        )
    )
    evidence_id = "evidence-account-id-exact-value"
    account_id = column("accounts", "AccountID")
    payload = state.model_dump(mode="python")
    payload.update(
        evidence=(*state.evidence, _value_evidence(evidence_id, account_id, 123)),
        bindings=tuple(
            binding.model_copy(
                update={"evidence_ids": (*binding.evidence_ids, evidence_id)}
            )
            if binding.source_id == "account"
            else binding
            for binding in state.bindings
        ),
    )
    state = ResearchState.model_validate(payload)
    requirements = validate_coverage_inputs(state, _context(), RUN_ID, INCARNATION)
    sql = (
        "SELECT e.amount FROM accounts AS a JOIN events AS e "
        "ON a.AccountID = e.AccountID WHERE a.AccountID = 123"
    )
    parsed_ast = parse_sql_candidate(sql, "sqlite://", "join-only-exact-value")
    candidate = SqlCandidate(
        candidate_id="join-only-exact-value",
        sql=sql,
        normalized_ast_digest=parsed_ast.candidate_digest,
        revision=state.revision,
    )

    result = evaluate_semantic_authority_checks(
        SemanticCheckInput(
            semantic_ast=build_semantic_ast(
                candidate,
                parsed_ast,
                state.query_spec,
                requirements,
                "main",
            ),
            query_spec=state.query_spec,
            requirements=requirements,
        ),
        state,
        "sqlite://",
    )

    assert account_id not in requirements.allowed_columns
    assert evidence_id in requirements.eligible_evidence_ids
    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL


@pytest.mark.parametrize(
    ("sql", "items", "shape", "failure_code"),
    (
        pytest.param(
            "SELECT o.id FROM orders o JOIN secrets s ON o.id = s.order_id",
            (_item("id", SemanticItemKind.DIMENSION, "id"),),
            ExpectedResultShape.ROWS,
            CheckFailureCode.UNAUTHORIZED_TABLE,
            id="unauthorized-table",
        ),
        pytest.param(
            "SELECT o.secret FROM orders o",
            (_item("id", SemanticItemKind.DIMENSION, "id"),),
            ExpectedResultShape.ROWS,
            CheckFailureCode.UNAUTHORIZED_COLUMN,
            id="unauthorized-column",
        ),
        pytest.param(
            "SELECT o.status FROM orders o WHERE o.status = 'inactive'",
            (
                _item(
                    "status",
                    SemanticItemKind.FILTER,
                    "status",
                    operator=PredicateOperator.EQ,
                    literal="active",
                ),
            ),
            ExpectedResultShape.ROWS,
            CheckFailureCode.UNAUTHORIZED_LITERAL,
            id="unauthorized-literal",
        ),
    ),
)
def test_each_authority_failure_code_is_exact_and_typed(
    sql: str,
    items: tuple[ItemSpec, ...],
    shape: ExpectedResultShape,
    failure_code: CheckFailureCode,
) -> None:
    case = build_case(sql, items, shape=shape)

    result = _evaluate(case)

    assert result.check_kind is CheckKind.SEMANTIC
    assert result.status is CheckStatus.FAILED
    assert result.failure_code is failure_code
    assert result.repair is not None
    assert result.repair.kind is RepairKind.REVISE_SQL
    assert result.required_change is None
    assert result.affected_source_ids or result.affected_ast_node_ids
    expected_sources = ()
    ast = case.check_input.parsed_ast
    nodes_by_kind = {
        "scan": tuple(sorted(item.node_id for item in ast.table_scans)),
        "join": tuple(sorted(item.node_id for item in ast.joins)),
        "projection": tuple(sorted(item.node_id for item in ast.projections)),
        "predicate": tuple(sorted(item.node_id for item in ast.predicates)),
        "aggregate": tuple(sorted(item.node_id for item in ast.aggregates)),
        "ordering": tuple(sorted(item.node_id for item in ast.orderings)),
        "limit": tuple(sorted(item.node_id for item in ast.limits)),
    }
    if failure_code is CheckFailureCode.UNAUTHORIZED_TABLE:
        expected_nodes = (
            next(
                fact.node_id
                for fact in case.check_input.parsed_ast.table_scans
                if fact.table.name == "secrets"
            ),
        )
    else:
        expected_nodes = {
            CheckFailureCode.UNAUTHORIZED_COLUMN: nodes_by_kind["projection"],
            CheckFailureCode.UNAUTHORIZED_LITERAL: nodes_by_kind["predicate"],
        }.get(failure_code, ())
    assert result.affected_source_ids == expected_sources
    assert result.affected_ast_node_ids == expected_nodes
    assert result.repair.source_ids == expected_sources
    assert result.repair.ast_node_ids == expected_nodes
