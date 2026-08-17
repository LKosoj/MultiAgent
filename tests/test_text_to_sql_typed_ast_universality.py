"""The public Typed gate accepts supported sqlglot AST shapes."""

import pytest

from custom_tools.text_to_sql import core
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    CheckStatus,
    ExpectedResultShape,
    PredicateOperator,
    SemanticItemKind,
)
from custom_tools.text_to_sql.adaptive.pre_execution_gate import (
    create_pre_execution_gate_capability,
    evaluate_pre_execution_gate_capability,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    validate_coverage_inputs,
)
from text_to_sql_semantic_checks_helpers import (
    INCARNATION,
    POSTGRES_DSN,
    RUN_ID,
    ItemSpec,
    _context,
    build_state,
)
from workflow.deadline import DeadlineBudget


def _safe_result() -> dict[str, object]:
    return {
        "is_safe": True,
        "issues": [],
        "advisory_issues": [],
        "safety_status": "safe",
        "llm_audit": "skipped_static_only",
    }


def _capability(
    sql: str,
    shape: ExpectedResultShape,
    items: tuple[ItemSpec, ...],
) -> object:
    state = build_state(items, shape=shape)
    requirements = validate_coverage_inputs(state, _context(), RUN_ID, INCARNATION)
    return create_pre_execution_gate_capability(
        state=state,
        requirements=requirements,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        expected_sql=sql,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        deadline=DeadlineBudget.from_duration(10),
        is_cancelled=lambda: False,
    )


@pytest.mark.parametrize(
    ("sql", "shape", "items", "expected_failure"),
    (
        (
            "SELECT CASE WHEN CAST(o.status AS TEXT) = 'active' "
            "THEN 1 + LENGTH(o.status) ELSE 0 END FROM orders o "
            "WHERE o.status = 'active' "
            "GROUP BY CASE WHEN CAST(o.status AS TEXT) = 'active' "
            "THEN 1 + LENGTH(o.status) ELSE 0 END "
            "ORDER BY CASE WHEN CAST(o.status AS TEXT) = 'active' "
            "THEN 1 + LENGTH(o.status) ELSE 0 END",
            ExpectedResultShape.GROUPED_ROWS,
            (
                ItemSpec(
                    "status_dimension",
                    SemanticItemKind.DIMENSION,
                    "orders",
                    "status",
                ),
                ItemSpec(
                    "status_filter",
                    SemanticItemKind.FILTER,
                    "orders",
                    "status",
                    operator=PredicateOperator.EQ,
                    literal="active",
                ),
            ),
                None,
        ),
        (
            "SELECT COUNT(DISTINCT o.status) FROM orders o "
            "WHERE o.status = 'active'",
            ExpectedResultShape.SCALAR,
            (
                ItemSpec(
                    "status_metric",
                    SemanticItemKind.METRIC,
                    "orders",
                    "status",
                ),
                ItemSpec(
                    "status_filter",
                    SemanticItemKind.FILTER,
                    "orders",
                    "status",
                    operator=PredicateOperator.EQ,
                    literal="active",
                ),
            ),
            None,
        ),
        (
            "WITH active AS (SELECT o.status FROM orders o "
            "WHERE o.status = 'active') "
            "SELECT (SELECT i.status FROM orders i WHERE i.status = a.status) "
            "FROM active a WHERE EXISTS "
            "(SELECT 1 FROM orders e WHERE e.status = a.status) "
            "UNION SELECT o.status FROM orders o WHERE o.status = 'active'",
            ExpectedResultShape.ROWS,
            (
                ItemSpec(
                    "status_filter",
                    SemanticItemKind.FILTER,
                    "orders",
                    "status",
                    operator=PredicateOperator.EQ,
                    literal="active",
                ),
            ),
            None,
        ),
    ),
    ids=("expression_group_order", "distinct_aggregate", "nested_query_shapes"),
)
def test_supported_full_ast_reaches_authenticated_gate(
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
    shape: ExpectedResultShape,
    items: tuple[ItemSpec, ...],
    expected_failure: CheckFailureCode | None,
) -> None:
    monkeypatch.setattr(core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result())

    receipt = evaluate_pre_execution_gate_capability(
        _capability(sql, shape, items),
        expected_run_id=RUN_ID,
        expected_sql=sql,
        safety_policy=None,
    )

    assert tuple(result.check_kind for result in receipt.check_results) == (
        CheckKind.SAFETY,
        CheckKind.SEMANTIC,
    ), receipt.check_results
    if expected_failure is None:
        assert receipt.allowed is True
        assert all(
            result.status is CheckStatus.PASSED for result in receipt.check_results
        )
    else:
        assert receipt.allowed is False
        assert tuple(result.status for result in receipt.check_results) == (
            CheckStatus.PASSED,
            CheckStatus.FAILED,
        )
        assert receipt.check_results[-1].failure_code is expected_failure


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT o.amount FROM orders o WHERE o.amount = "
        "(SELECT MAX(i.amount) FROM orders i)",
        "SELECT o.amount FROM orders o WHERE o.amount IN "
        "(SELECT MAX(i.amount) FROM orders i UNION "
        "SELECT MIN(j.amount) FROM orders j)",
    ),
    ids=("scalar_subquery", "in_union_subquery"),
)
def test_predicate_subqueries_reach_authority_gate(
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
) -> None:
    monkeypatch.setattr(core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result())

    receipt = evaluate_pre_execution_gate_capability(
        _capability(
            sql,
            ExpectedResultShape.ROWS,
            (ItemSpec("amount", SemanticItemKind.METRIC, "orders", "amount"),),
        ),
        expected_run_id=RUN_ID,
        expected_sql=sql,
        safety_policy=None,
    )

    assert receipt.allowed is True
    assert tuple(result.check_kind for result in receipt.check_results) == (
        CheckKind.SAFETY,
        CheckKind.SEMANTIC,
    )
    assert all(result.status is CheckStatus.PASSED for result in receipt.check_results)


def test_opaque_predicate_atom_reaches_authority_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = "SELECT o.amount FROM orders o WHERE ABS(o.amount) > 0"
    monkeypatch.setattr(core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result())

    receipt = evaluate_pre_execution_gate_capability(
        _capability(
            sql,
            ExpectedResultShape.ROWS,
            (ItemSpec("amount", SemanticItemKind.METRIC, "orders", "amount"),),
        ),
        expected_run_id=RUN_ID,
        expected_sql=sql,
        safety_policy=None,
    )

    assert receipt.allowed is True
    assert tuple(result.status for result in receipt.check_results) == (
        CheckStatus.PASSED,
        CheckStatus.PASSED,
    )
    assert receipt.primary_check_id is None
