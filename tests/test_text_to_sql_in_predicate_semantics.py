"""IN-list and BETWEEN mapping retain the semantic authority contract."""

import pytest

from custom_tools.text_to_sql.adaptive._semantic_coverage_footprint import (
    canonical_binding,
)
from custom_tools.text_to_sql.adaptive.checks import SemanticCheckInput
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckStatus,
    ExpectedResultShape,
    PredicateOperator,
    ResearchState,
    SemanticItemKind,
    SqlCandidate,
)
from custom_tools.text_to_sql.adaptive.semantic_checks import evaluate_semantic_authority_checks
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.semantic_plan import build_semantic_ast
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from text_to_sql_semantic_checks_helpers import (
    INCARNATION,
    POSTGRES_DSN,
    RUN_ID,
    ItemSpec,
    _context,
    _value_evidence,
    build_state,
)


def _in_case(
    sql: str,
    *,
    operator: PredicateOperator = PredicateOperator.IN,
    literal: object = ("gold", "silver"),
    evidence_values: tuple[object, ...] = ("gold", "silver"),
    kind: SemanticItemKind = SemanticItemKind.FILTER,
    include_semantic_ast: bool = False,
):
    item = ItemSpec(
        "tier",
        SemanticItemKind.FILTER,
        "orders",
        "tier",
        operator=operator,
        literal=literal,
    )
    seed = build_state((item,), shape=ExpectedResultShape.ROWS)
    binding = seed.bindings[0]
    values = tuple(
        _value_evidence(
            f"evidence-tier-{value}", binding.discriminator_column, value
        )
        for value in evidence_values
    )
    binding = canonical_binding(
        binding.model_copy(
            update={
                "evidence_ids": tuple(
                    sorted(
                        (
                            "evidence-tier-schema",
                            *(value.evidence_id for value in values),
                        )
                    )
                )
            }
        )
    )
    schema_evidence = next(
        evidence
        for evidence in seed.evidence
        if evidence.evidence_id == "evidence-tier-schema"
    )
    state = ResearchState.model_validate(
        {
            **seed.model_dump(mode="python"),
            "evidence": (schema_evidence, *values),
            "bindings": (binding,),
        }
    )
    if kind is not SemanticItemKind.FILTER:
        state = state.model_copy(
            update={
                "query_spec": state.query_spec.model_copy(
                    update={
                        "semantic_items": (
                            state.query_spec.semantic_items[0].model_copy(
                                update={"kind": kind}
                            ),
                        )
                    }
                )
            }
        )
    requirements = validate_coverage_inputs(state, _context(), RUN_ID, INCARNATION)
    parsed_ast = parse_sql_candidate(sql, POSTGRES_DSN, "candidate-in")
    candidate = SqlCandidate(
        candidate_id="candidate-in",
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
        SemanticCheckInput(semantic_ast, state.query_spec, requirements),
        state,
        POSTGRES_DSN,
    )
    return (result, semantic_ast) if include_semantic_ast else result


def test_in_list_reordering_and_duplicates_preserve_authority() -> None:
    accepted = _in_case(
        "SELECT o.tier FROM orders o WHERE o.tier IN ('silver', 'gold', 'silver')"
    )
    rejected = _in_case(
        "SELECT o.tier FROM orders o WHERE o.tier IN ('silver', 'gold', 'bronze')"
    )

    assert accepted.status is CheckStatus.PASSED
    assert rejected.status is CheckStatus.FAILED
    assert rejected.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL


def test_generic_transform_expression_keeps_in_literal_authority() -> None:
    result, semantic_ast = _in_case(
        "SELECT p.gold FROM orders PIVOT "
        "(COUNT(*) FOR tier IN ('gold', 'silver')) AS p",
        include_semantic_ast=True,
    )

    assert result.status is CheckStatus.PASSED
    assert semantic_ast.coverage.annotations[0].node_id == (
        semantic_ast.parsed_ast.expression_relations[0].node_id
    )
    assert semantic_ast.coverage.annotations[0].expression_field == "expression"


def test_between_symmetric_literal_bounds_are_authorized_in_either_order() -> None:
    accepted = _in_case(
        "SELECT o.tier FROM orders o WHERE o.tier BETWEEN 1 AND 2",
        operator=PredicateOperator.BETWEEN,
        literal=(1, 2),
        evidence_values=(1, 2),
    )
    reversed_bounds = _in_case(
        "SELECT o.tier FROM orders o WHERE o.tier BETWEEN SYMMETRIC 2 AND 1",
        operator=PredicateOperator.BETWEEN,
        literal=(1, 2),
        evidence_values=(1, 2),
    )

    assert accepted.status is CheckStatus.PASSED
    assert reversed_bounds.status is CheckStatus.PASSED
    assert reversed_bounds.failure_code is None

    ordinary_reversed = _in_case(
        "SELECT o.tier FROM orders o WHERE o.tier BETWEEN 2 AND 1",
        operator=PredicateOperator.BETWEEN,
        literal=(1, 2),
        evidence_values=(1, 2),
    )
    assert ordinary_reversed.status is CheckStatus.FAILED
    assert ordinary_reversed.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL


def test_reversed_symmetric_between_keeps_exact_coverage_annotation_and_evidence() -> None:
    forward, forward_ast = _in_case(
        "SELECT o.tier FROM orders o WHERE o.tier BETWEEN SYMMETRIC 1 AND 2",
        operator=PredicateOperator.BETWEEN,
        literal=(1, 2),
        evidence_values=(1, 2),
        include_semantic_ast=True,
    )
    reversed_bounds, reversed_ast = _in_case(
        "SELECT o.tier FROM orders o WHERE o.tier BETWEEN SYMMETRIC 2 AND 1",
        operator=PredicateOperator.BETWEEN,
        literal=(1, 2),
        evidence_values=(1, 2),
        include_semantic_ast=True,
    )

    assert forward.status is CheckStatus.PASSED
    assert reversed_bounds.status is CheckStatus.PASSED
    expected_evidence_ids = (
        "evidence-tier-1",
        "evidence-tier-2",
        "evidence-tier-schema",
    )
    assert forward_ast.coverage.evidence_ids == expected_evidence_ids
    assert reversed_ast.coverage.evidence_ids == expected_evidence_ids
    assert len(forward_ast.coverage.annotations) == 1
    assert len(reversed_ast.coverage.annotations) == 1
    forward_annotation = forward_ast.coverage.annotations[0]
    reversed_annotation = reversed_ast.coverage.annotations[0]
    assert (
        forward_annotation.expression_field,
        forward_annotation.expression_index,
        forward_annotation.expression_path,
        forward_annotation.source_ids,
        forward_annotation.evidence_ids,
    ) == (
        reversed_annotation.expression_field,
        reversed_annotation.expression_index,
        reversed_annotation.expression_path,
        reversed_annotation.source_ids,
        reversed_annotation.evidence_ids,
    ) == ("expression", 0, (), ("tier",), expected_evidence_ids)
    assert (
        forward_annotation.node_id
        == forward_ast.parsed_ast.predicates[0].atoms[0].node_id
    )
    assert (
        reversed_annotation.node_id
        == reversed_ast.parsed_ast.predicates[0].atoms[0].node_id
    )


def test_symmetric_between_rejects_a_literal_without_exact_evidence() -> None:
    result = _in_case(
        "SELECT o.tier FROM orders o WHERE o.tier BETWEEN SYMMETRIC 1 AND 3",
        operator=PredicateOperator.BETWEEN,
        literal=(1, 2),
        evidence_values=(1, 2),
    )

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL


def test_time_between_requires_exact_discriminator_predicate_match() -> None:
    accepted = _in_case(
        "SELECT o.tier FROM orders o "
        "WHERE o.tier BETWEEN '2024-01-01' AND '2024-01-31'",
        operator=PredicateOperator.BETWEEN,
        literal=("2024-01-01", "2024-01-31"),
        evidence_values=("2024-01-01", "2024-01-31"),
        kind=SemanticItemKind.TIME,
    )
    wrong_endpoint = _in_case(
        "SELECT o.tier FROM orders o "
        "WHERE o.tier BETWEEN '2024-01-01' AND '2024-01-30'",
        operator=PredicateOperator.BETWEEN,
        literal=("2024-01-01", "2024-01-31"),
        evidence_values=("2024-01-01", "2024-01-31"),
        kind=SemanticItemKind.TIME,
    )

    assert accepted.status is CheckStatus.PASSED
    assert wrong_endpoint.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL


@pytest.mark.parametrize(
    ("operator", "sql", "wrong_sql"),
    (
        (PredicateOperator.GT, "o.tier > 1", "o.tier > 2"),
        (PredicateOperator.GTE, "o.tier >= 1", "o.tier >= 2"),
        (PredicateOperator.LT, "o.tier < 1", "o.tier < 2"),
        (PredicateOperator.LTE, "o.tier <= 1", "o.tier <= 2"),
    ),
)
def test_ordered_predicates_do_not_require_observed_values(
    operator: PredicateOperator,
    sql: str,
    wrong_sql: str,
) -> None:
    accepted = _in_case(
        f"SELECT o.tier FROM orders o WHERE {sql}",
        operator=operator,
        literal=1,
        evidence_values=(),
    )
    wrong_literal = _in_case(
        f"SELECT o.tier FROM orders o WHERE {wrong_sql}",
        operator=operator,
        literal=1,
        evidence_values=(),
    )

    assert accepted.status is CheckStatus.PASSED
    assert wrong_literal.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL


def test_time_between_does_not_require_bounds_as_observed_values() -> None:
    accepted = _in_case(
        "SELECT o.tier FROM orders o "
        "WHERE o.tier BETWEEN '2024-01-01' AND '2024-01-31'",
        operator=PredicateOperator.BETWEEN,
        literal=("2024-01-01", "2024-01-31"),
        evidence_values=(),
        kind=SemanticItemKind.TIME,
    )
    wrong_endpoint = _in_case(
        "SELECT o.tier FROM orders o "
        "WHERE o.tier BETWEEN '2024-01-01' AND '2024-01-30'",
        operator=PredicateOperator.BETWEEN,
        literal=("2024-01-01", "2024-01-31"),
        evidence_values=(),
        kind=SemanticItemKind.TIME,
    )

    assert accepted.status is CheckStatus.PASSED
    assert wrong_endpoint.failure_code is CheckFailureCode.UNAUTHORIZED_LITERAL


def test_between_column_bound_remains_an_unauthorized_column() -> None:
    result = _in_case(
        "SELECT o.tier FROM orders o WHERE o.tier BETWEEN 1 AND o.other_tier",
        operator=PredicateOperator.BETWEEN,
        literal=(1, 2),
        evidence_values=(1, 2),
    )

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.UNAUTHORIZED_COLUMN


def test_time_between_column_bound_remains_an_unauthorized_column() -> None:
    result = _in_case(
        "SELECT o.tier FROM orders o "
        "WHERE o.tier BETWEEN '2024-01-01' AND o.other_tier",
        operator=PredicateOperator.BETWEEN,
        literal=("2024-01-01", "2024-01-31"),
        evidence_values=("2024-01-01", "2024-01-31"),
        kind=SemanticItemKind.TIME,
    )

    assert result.status is CheckStatus.FAILED
    assert result.failure_code is CheckFailureCode.UNAUTHORIZED_COLUMN
