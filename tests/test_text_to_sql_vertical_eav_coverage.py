"""W5-03 validates exact vertical/EAV predicates and validated joins."""

from dataclasses import replace

import pytest

from custom_tools.text_to_sql.adaptive import semantic_coverage
from custom_tools.text_to_sql.adaptive.checks import SemanticCheckInput
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckStatus,
    VerticalAttributeBinding,
)
from custom_tools.text_to_sql.adaptive.semantic_checks import evaluate_semantic_authority_checks
from text_to_sql_semantic_checks_helpers import (
    POSTGRES_DSN,
    build_vertical_case,
)


VALID_SQL = """
    SELECT c.id
    FROM customers c
    JOIN attribute_values v ON c.id = v.customer_id
    JOIN attributes a ON a.id = v.attribute_id
    WHERE a.name = 'membership_level' AND v.value = 'premium'
"""


def _evaluate(sql: str):
    case = build_vertical_case(sql)
    return case, evaluate_semantic_authority_checks(case.check_input, case.state, POSTGRES_DSN)


@pytest.mark.parametrize(
    ("sql", "code"),
    (
        (
            "SELECT c.id FROM customers c "
            "JOIN attribute_values v ON c.id = v.customer_id "
            "JOIN attributes a ON a.id = v.attribute_id "
            "WHERE a.name = 'wrong_attribute' AND v.value = 'premium'",
            CheckFailureCode.UNAUTHORIZED_LITERAL,
        ),
        (
            "SELECT c.id FROM customers c "
            "JOIN attribute_values v ON c.id = v.customer_id "
            "JOIN attributes a ON a.id = v.attribute_id "
            "WHERE a.name = 'membership_level' AND v.value = 'basic'",
            CheckFailureCode.UNAUTHORIZED_LITERAL,
        ),
    ),
)
def test_vertical_failures_are_specific(sql: str, code: CheckFailureCode) -> None:
    _, result = _evaluate(sql)

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is code


@pytest.mark.parametrize(
    ("sql", "code"),
    (
        (
            "SELECT c.id FROM customers c "
            "JOIN attribute_values v ON c.id = v.customer_id "
            "JOIN attributes a ON a.id = v.attribute_id "
            "JOIN secrets s ON s.id = c.id",
            CheckFailureCode.UNAUTHORIZED_TABLE,
        ),
    ),
)
def test_unauthorized_scope_precedes_vertical_failures(
    sql: str,
    code: CheckFailureCode,
) -> None:
    _, result = _evaluate(sql)

    assert result.failure_code is code


@pytest.mark.parametrize(
    "sql",
    (
        VALID_SQL,
        "SELECT customer_alias.id "
        "FROM attribute_values AS value_alias "
        "INNER JOIN attributes AS catalog_alias "
        "ON value_alias.attribute_id = catalog_alias.id "
        "INNER JOIN customers AS customer_alias "
        "ON value_alias.customer_id = customer_alias.id "
        "WHERE value_alias.value = 'premium' "
        "AND catalog_alias.name = 'membership_level'",
    ),
)
def test_vertical_alias_format_and_reversed_inner_edges_are_equivalent(
    sql: str,
) -> None:
    case, result = _evaluate(sql)

    assert result.status is CheckStatus.PASSED
    assert tuple(
        join.join_id for join in case.requirements.eligible_validated_joins
    ) == (
        "join-vertical-catalog",
        "join-vertical-entity",
    )


def test_no_fk_vertical_path_uses_only_validated_join_evidence() -> None:
    case, result = _evaluate(VALID_SQL)

    assert result.status is CheckStatus.PASSED
    assert {
        evidence_id
        for join in case.requirements.eligible_validated_joins
        for evidence_id in join.evidence_ids
    } == {
        "evidence-vertical-catalog-join",
        "evidence-vertical-entity-join",
    }


@pytest.mark.parametrize(
    "constraint",
    (
        "v.customer_id = c.id",
        "a.id = v.attribute_id",
    ),
)
def test_allowed_join_edge_as_reversed_column_predicate_passes(
    constraint: str,
) -> None:
    _, result = _evaluate(
        "SELECT c.id FROM customers c "
        "JOIN attribute_values v ON c.id = v.customer_id "
        "JOIN attributes a ON a.id = v.attribute_id "
        "WHERE a.name = 'membership_level' AND v.value = 'premium' "
        f"AND {constraint}"
    )

    assert result.status is CheckStatus.PASSED


def test_vertical_overlay_forgery_is_check_input_invalid() -> None:
    case = build_vertical_case(VALID_SQL)
    binding = case.requirements.selected_bindings[0]
    assert type(binding) is VerticalAttributeBinding
    coverage = case.check_input.semantic_ast.coverage
    catalog_annotation = next(
        annotation
        for annotation in coverage.annotations
        if "membership" in annotation.source_ids
    )
    forged_coverage = coverage.model_copy(
        update={
            "annotations": tuple(
                annotation.model_copy(update={"source_ids": ()})
                if annotation == catalog_annotation
                else annotation
                for annotation in coverage.annotations
            )
        }
    )
    forged_input = SemanticCheckInput(
        semantic_ast=replace(case.check_input.semantic_ast, coverage=forged_coverage),
        query_spec=case.query_spec,
        requirements=case.requirements,
    )

    result = evaluate_semantic_authority_checks(forged_input, case.state, POSTGRES_DSN)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


def test_vertical_evaluation_does_not_rederive_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = build_vertical_case(VALID_SQL)

    monkeypatch.setattr(
        semantic_coverage,
        "validate_coverage_inputs",
        lambda *_args, **_kwargs: pytest.fail("coverage authority was rederived"),
    )

    result = evaluate_semantic_authority_checks(case.check_input, case.state, POSTGRES_DSN)

    assert result.status is CheckStatus.PASSED


@pytest.mark.parametrize(
    ("sql", "status"),
    (
        (
            "WITH chosen AS ("
            "SELECT c.id FROM customers c "
            "JOIN attribute_values v ON c.id = v.customer_id "
            "JOIN attributes a ON a.id = v.attribute_id "
            "WHERE a.name = 'membership_level' AND v.value = 'premium'"
            ") SELECT id FROM chosen",
            CheckStatus.PASSED,
        ),
        (
            "SELECT c.id FROM customers c "
            "JOIN attribute_values v ON c.id = v.customer_id "
            "JOIN attribute_values v2 ON c.id = v2.customer_id "
            "JOIN attributes a ON a.id = v.attribute_id "
            "WHERE a.name = 'membership_level' AND v.value = 'premium'",
            CheckStatus.PASSED,
        ),
    ),
)
def test_full_ast_forms_are_checked_semantically(
    sql: str,
    status: CheckStatus,
) -> None:
    _, result = _evaluate(sql)

    assert result.status is status
