"""W5-04 edge cases stay deterministic and fail closed."""

from dataclasses import replace

import pytest

from custom_tools.text_to_sql.adaptive.checks import SemanticCheckInput
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckStatus,
    PredicateOperator,
    RepairKind,
    SemanticItemKind,
)
from custom_tools.text_to_sql.adaptive.semantic_checks import evaluate_semantic_authority_checks
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageInputError,
    CoverageInputErrorCode,
)
from text_to_sql_semantic_checks_helpers import (
    POSTGRES_DSN,
    ItemSpec,
    build_case,
    inner_join,
)


def _item(
    source_id: str,
    kind: SemanticItemKind,
    column: str,
    *,
    table: str = "orders",
    operator: PredicateOperator | None = None,
    literal: object = None,
    join_path=(),
) -> ItemSpec:
    return ItemSpec(
        source_id=source_id,
        kind=kind,
        table=table,
        column=column,
        operator=operator,
        literal=literal,
        join_path=join_path,
    )


def _evaluate(case, *, query_spec=None, state=None, requirements=None):
    check_input = SemanticCheckInput(
        semantic_ast=case.check_input.semantic_ast,
        query_spec=query_spec or case.query_spec,
        requirements=requirements or case.requirements,
    )
    return evaluate_semantic_authority_checks(
        check_input,
        state or case.state,
        POSTGRES_DSN,
    )


def test_reversed_and_different_inner_join_edges_are_not_pre_execution_gates() -> None:
    allowed = inner_join("orders", "customer_id", "customers", "id")
    items = (
        _item("order-id", SemanticItemKind.DIMENSION, "id"),
        _item(
            "customer-id",
            SemanticItemKind.DIMENSION,
            "id",
            table="customers",
            join_path=(allowed,),
        ),
    )
    reversed_case = build_case(
        "SELECT o.id, c.id FROM orders o JOIN customers c ON c.id = o.customer_id",
        items,
    )
    wrong_case = build_case(
        "SELECT o.id, c.id FROM orders o JOIN customers c ON c.id = o.id",
        items,
    )

    assert _evaluate(reversed_case).status is CheckStatus.PASSED
    assert _evaluate(wrong_case).status is CheckStatus.PASSED


def test_correlated_column_comparison_is_not_an_implicit_join() -> None:
    case = build_case(
        "SELECT o.status FROM orders o WHERE EXISTS "
        "(SELECT 1 FROM orders x WHERE x.status = o.status)",
        (_item("status_dimension", SemanticItemKind.DIMENSION, "status"),),
    )

    result = _evaluate(case)

    assert result.status is CheckStatus.PASSED
    assert result.failure_code is None
    assert result.affected_source_ids == ()
    assert result.affected_ast_node_ids == ()


def test_mismatched_query_spec_and_coverage_identity_are_inconclusive() -> None:
    case = build_case(
        "SELECT o.id FROM orders o",
        (_item("id", SemanticItemKind.DIMENSION, "id"),),
    )
    foreign_spec = case.query_spec.model_copy(update={"query_id": "foreign-query"})

    result = _evaluate(case, query_spec=foreign_spec)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID
    assert result.observed_error == CheckFailureCode.CHECK_INPUT_INVALID.value
    assert result.repair is not None
    assert result.repair.kind is RepairKind.REVISE_SQL


def test_candidate_sql_bytes_must_match_the_authenticated_ast() -> None:
    case = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
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
    candidate = case.check_input.candidate.model_copy(
        update={"sql": "SELECT o.status FROM orders o WHERE o.status = 'inactive'"}
    )
    tampered = SemanticCheckInput(
        semantic_ast=replace(case.check_input.semantic_ast, candidate=candidate),
        query_spec=case.query_spec,
        requirements=case.requirements,
    )

    result = evaluate_semantic_authority_checks(
        tampered,
        case.state,
        POSTGRES_DSN,
    )

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


def test_ast_facts_must_still_match_their_semantic_digest() -> None:
    case = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
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
    inactive = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'inactive'",
        (
            _item(
                "status",
                SemanticItemKind.FILTER,
                "status",
                operator=PredicateOperator.EQ,
                literal="inactive",
            ),
        ),
    )
    active_fact = case.check_input.parsed_ast.predicates[0]
    inactive_fact = inactive.check_input.parsed_ast.predicates[0]
    forged_fact = replace(
        active_fact,
        expression=inactive_fact.expression,
        atoms=tuple(
            replace(active, expression=changed.expression)
            for active, changed in zip(
                active_fact.atoms,
                inactive_fact.atoms,
                strict=True,
            )
        ),
    )
    forged_ast = replace(case.check_input.parsed_ast, predicates=(forged_fact,))
    tampered = SemanticCheckInput(
        semantic_ast=replace(case.check_input.semantic_ast, parsed_ast=forged_ast),
        query_spec=case.query_spec,
        requirements=case.requirements,
    )

    result = evaluate_semantic_authority_checks(
        tampered,
        case.state,
        POSTGRES_DSN,
    )

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


@pytest.mark.parametrize("field", ("source", "evidence"))
def test_coverage_provenance_must_match_the_authenticated_semantic_ast(
    field: str,
) -> None:
    case = build_case(
        "SELECT o.id FROM orders o",
        (_item("id", SemanticItemKind.DIMENSION, "id"),),
    )
    coverage = case.check_input.semantic_ast.coverage
    if field == "source":
        annotation_update = {"source_ids": ("forged-source",)}
        coverage_update = {"required_source_ids": ("forged-source",)}
    else:
        annotation_update = {"evidence_ids": ("forged-evidence",)}
        coverage_update = {"evidence_ids": ("forged-evidence",)}
    coverage_update["annotations"] = tuple(
        annotation.model_copy(update=annotation_update)
        if annotation == coverage.annotations[0]
        else annotation
        for annotation in coverage.annotations
    )
    tampered = SemanticCheckInput(
        semantic_ast=replace(
            case.check_input.semantic_ast,
            coverage=coverage.model_copy(update=coverage_update),
        ),
        query_spec=case.query_spec,
        requirements=case.requirements,
    )

    result = _evaluate(
        type(case)(
            check_input=tampered,
            query_spec=case.query_spec,
            state=case.state,
            requirements=case.requirements,
        )
    )

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


def test_evaluator_rejects_mysql_ast_under_postgres_runtime_dsn() -> None:
    case = build_case(
        "SELECT `o`.`id` FROM `orders` AS `o`",
        (_item("id", SemanticItemKind.DIMENSION, "id"),),
        dsn="mysql://user:password@localhost/example",
    )

    result = evaluate_semantic_authority_checks(
        case.check_input,
        case.state,
        POSTGRES_DSN,
    )

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


@pytest.mark.parametrize("dsn", (None, "", "unknown://localhost/example"))
def test_evaluator_fails_closed_for_invalid_runtime_dsn(dsn: object) -> None:
    case = build_case(
        "SELECT o.id FROM orders o",
        (_item("id", SemanticItemKind.DIMENSION, "id"),),
    )

    result = evaluate_semantic_authority_checks(
        case.check_input,
        case.state,
        dsn,
    )

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


def test_each_equivalent_sql_form_is_authenticated_independently() -> None:
    items = (
        _item(
            "status",
            SemanticItemKind.FILTER,
            "status",
            operator=PredicateOperator.EQ,
            literal="active",
        ),
    )
    first = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        items,
    )
    second = build_case(
        'select "x"."status" from "orders" as "x" where "x"."status" = \'active\'',
        items,
    )

    assert first.check_input.parsed_ast.candidate_digest != (
        second.check_input.parsed_ast.candidate_digest
    )
    assert first.check_input.parsed_ast.source_sql_digest != (
        second.check_input.parsed_ast.source_sql_digest
    )
    assert _evaluate(first).status is CheckStatus.PASSED
    assert _evaluate(second).status is CheckStatus.PASSED


def test_time_requirement_with_physical_binding_remains_invalid() -> None:
    with pytest.raises(CoverageInputError) as raised:
        build_case(
            "SELECT o.id FROM orders o "
            "WHERE o.created_at BETWEEN '2024-01-01' AND '2024-01-31'",
            (
                _item(
                    "time",
                    SemanticItemKind.TIME,
                    "created_at",
                    operator=PredicateOperator.BETWEEN,
                    literal=("2024-01-01", "2024-01-31"),
                ),
            ),
        )

    assert raised.value.code is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE
