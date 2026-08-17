"""W5-04 canonical matching is deterministic and alias-free."""

from custom_tools.text_to_sql.adaptive._semantic_matching import (
    canonical_literal_key,
    canonical_predicate_key,
    join_path_matches,
    predicate_matches,
)
from custom_tools.text_to_sql.adaptive.models import (
    JoinEdge,
    JoinType,
    LiteralValue,
    PredicateOperator,
    PredicateRef,
)
from text_to_sql_semantic_checks_helpers import column, inner_join


def test_literal_keys_preserve_types_and_unwrap_literal_values() -> None:
    assert canonical_literal_key(LiteralValue(value=1)) == canonical_literal_key(1)
    assert canonical_literal_key(True) != canonical_literal_key(1)
    assert canonical_literal_key(1) != canonical_literal_key(1.0)
    assert canonical_literal_key("1") != canonical_literal_key(1)


def test_in_predicate_matching_is_order_and_duplicate_insensitive() -> None:
    required = PredicateRef(
        left=column("orders", "status"),
        operator=PredicateOperator.IN,
        right=("active", "pending", "active"),
    )
    actual = PredicateRef(
        left=column("orders", "status"),
        operator=PredicateOperator.IN,
        right=("pending", "active"),
    )

    assert canonical_predicate_key(required) == canonical_predicate_key(actual)
    assert predicate_matches(required, actual)


def test_predicate_matching_rejects_operator_column_and_literal_changes() -> None:
    required = PredicateRef(
        left=column("orders", "status"),
        operator=PredicateOperator.EQ,
        right="active",
    )

    assert not predicate_matches(
        required,
        required.model_copy(update={"operator": PredicateOperator.NEQ}),
    )
    assert not predicate_matches(
        required,
        required.model_copy(update={"left": column("orders", "kind")}),
    )
    assert not predicate_matches(
        required,
        required.model_copy(update={"right": "inactive"}),
    )


def test_inner_join_path_accepts_edge_permutation_and_orientation() -> None:
    first = inner_join("orders", "customer_id", "customers", "id")
    second = inner_join("customers", "region_id", "regions", "id")
    reversed_first = JoinEdge(left=first.right, right=first.left)
    reversed_second = JoinEdge(left=second.right, right=second.left)

    assert join_path_matches((first, second), (reversed_second, reversed_first))


def test_left_join_path_preserves_order_and_orientation() -> None:
    first = JoinEdge(
        left=column("orders", "customer_id"),
        right=column("customers", "id"),
        join_type=JoinType.LEFT,
    )
    second = JoinEdge(
        left=column("customers", "region_id"),
        right=column("regions", "id"),
        join_type=JoinType.LEFT,
    )

    assert join_path_matches((first, second), (first, second))
    assert not join_path_matches((first, second), (second, first))
    assert not join_path_matches(
        (first, second),
        (JoinEdge(left=first.right, right=first.left, join_type=JoinType.LEFT), second),
    )


def test_self_join_matches_exact_columns_without_alias_guessing() -> None:
    required = inner_join("employee", "manager_id", "employee", "id")
    reversed_edge = JoinEdge(left=required.right, right=required.left)
    wrong = inner_join("employee", "mentor_id", "employee", "id")

    assert join_path_matches((required,), (reversed_edge,))
    assert not join_path_matches((required,), (wrong,))
