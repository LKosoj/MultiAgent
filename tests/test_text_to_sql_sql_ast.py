from __future__ import annotations

import builtins
from dataclasses import FrozenInstanceError
from hashlib import sha256
import sqlite3

import pytest

from custom_tools.text_to_sql.adaptive import sql_ast
from custom_tools.text_to_sql.adaptive.sql_ast import (
    PredicateLocation,
    QueryRole,
    SetOperationKind,
    SqlAstError,
    SqlAstErrorCode,
    parse_sql_candidate,
)


POSTGRES_DSN = "postgresql://user:password@localhost/example"
SQLITE_DSN = "sqlite:///:memory:"


def _parse(sql: str, *, candidate_id: str = "candidate-1"):
    return parse_sql_candidate(sql, POSTGRES_DSN, candidate_id)


def _attributes(expression) -> dict[str, str]:
    return dict(expression.attributes)


def _walk_expression(expression):
    yield expression
    for _argument, _ordinal, child in expression.children:
        yield from _walk_expression(child)


def _semantic_facts(parsed):
    return (
        parsed.scopes,
        parsed.ctes,
        parsed.table_scans,
        parsed.cte_references,
        parsed.derived_relations,
        parsed.set_operations,
        parsed.subquery_refs,
        parsed.joins,
        parsed.projections,
        parsed.predicates,
        parsed.aggregates,
        parsed.groupings,
        parsed.orderings,
        parsed.limits,
    )


def _node_ids(parsed):
    node_ids = []
    for collection in _semantic_facts(parsed):
        for fact in collection:
            node_ids.append(fact.node_id)
            if hasattr(fact, "atoms"):
                node_ids.extend(atom.node_id for atom in fact.atoms)
    return tuple(node_ids)


def _assert_error(sql: str, code: SqlAstErrorCode) -> None:
    with pytest.raises(SqlAstError) as exc_info:
        _parse(sql)
    assert exc_info.value.code is code
    assert str(exc_info.value)


def test_formatting_comments_and_quotes_are_canonical_but_relation_aliases_are_not() -> None:
    first = _parse(
        "SELECT o.id AS picked FROM public.orders o WHERE o.kind = 'FROM hidden; --'"
    )
    second = _parse(
        'select "o"."id" as "renamed" from "public"."orders" as "o" '
        'where "o"."kind"=\'FROM hidden; --\' /* ignored comment */'
    )
    renamed = _parse(
        "SELECT x.id AS picked FROM public.orders x WHERE x.kind = 'FROM hidden; --'"
    )

    assert first.candidate_digest == second.candidate_digest
    assert first.source_sql_digest != second.source_sql_digest
    assert _semantic_facts(first) == _semantic_facts(second)
    assert first.candidate_digest != renamed.candidate_digest


def test_source_sql_digest_is_exact_bytes_but_semantic_digest_is_replay_stable() -> (
    None
):
    sql = "SELECT o.id FROM orders o"
    replay = _parse(sql, candidate_id="candidate-replay")
    equivalent = _parse('select "o"."id" from "orders" as "o"')
    parsed = _parse(sql)

    assert parsed.source_sql_digest == f"sha256:{sha256(sql.encode()).hexdigest()}"
    assert replay.source_sql_digest == parsed.source_sql_digest
    assert replay.candidate_digest == parsed.candidate_digest
    assert equivalent.source_sql_digest != parsed.source_sql_digest
    assert equivalent.candidate_digest == parsed.candidate_digest


def test_projection_alias_reference_is_rewritten_to_stable_output_id() -> None:
    first = _parse("SELECT SUM(o.amount) AS total FROM orders o ORDER BY total DESC")
    second = _parse(
        "SELECT SUM(o.amount) AS renamed FROM orders o ORDER BY renamed DESC"
    )
    renamed_source = _parse(
        "SELECT SUM(x.amount) AS total FROM orders x ORDER BY total DESC"
    )

    assert first.candidate_digest == second.candidate_digest
    assert first.candidate_digest != renamed_source.candidate_digest
    assert first.orderings[0].expression.kind == "output_ref"
    assert _attributes(first.orderings[0].expression)["output_id"] == "scope:0:output:0"


def test_same_physical_table_aliases_remain_distinct_relation_instances() -> None:
    parsed = _parse(
        "SELECT av1.entity_id FROM attribute_value av1 "
        "JOIN attribute_value av2 ON av1.entity_id = av2.entity_id"
    )

    assert [scan.table.name for scan in parsed.table_scans] == [
        "attribute_value",
        "attribute_value",
    ]
    assert [scan.relation_id for scan in parsed.table_scans] == [
        "scope:0:relation:0",
        "scope:0:relation:1",
    ]
    relation_ids = {
        attrs["relation_id"]
        for node in _walk_expression(parsed.joins[0].condition)
        if node.kind == "column" and (attrs := _attributes(node)).get("relation_id")
    }
    assert relation_ids == {"scope:0:relation:0", "scope:0:relation:1"}


def test_cte_reference_is_not_a_physical_scan_but_cte_scan_is_preserved() -> None:
    parsed = _parse(
        "WITH selected AS ("
        "SELECT o.id FROM catalog.orders o WHERE o.active IS TRUE"
        ") SELECT s.id FROM selected s"
    )

    assert len(parsed.ctes) == 1
    assert len(parsed.cte_references) == 1
    assert len(parsed.table_scans) == 1
    assert parsed.table_scans[0].table.schema == "catalog"
    assert parsed.table_scans[0].table.name == "orders"
    assert parsed.cte_references[0].cte_id == parsed.ctes[0].cte_id


def test_cte_source_alias_changes_semantic_digest() -> None:
    first = _parse(
        "WITH selected AS (SELECT o.id FROM orders o) SELECT s.id FROM selected s"
    )
    second = _parse(
        "WITH selected AS (SELECT o.id FROM orders o) SELECT z.id FROM selected z"
    )

    assert first.candidate_digest != second.candidate_digest
    assert _semantic_facts(first) != _semantic_facts(second)


def test_relation_facts_preserve_sqlglot_source_aliases() -> None:
    parsed = _parse(
        "WITH cte_source AS (SELECT base_source.id FROM base_table AS base_source) "
        "SELECT cte_role.id FROM cte_source AS cte_role "
        "JOIN (SELECT derived_source.id FROM derived_table AS derived_source) "
        "AS derived_role ON cte_role.id = derived_role.id"
    )

    assert {fact.table.name: fact.source_alias for fact in parsed.table_scans} == {
        "base_table": "base_source",
        "derived_table": "derived_source",
    }
    assert parsed.cte_references[0].source_alias == "cte_role"
    assert parsed.derived_relations[0].source_alias == "derived_role"


def test_cte_body_can_reference_only_an_earlier_local_cte() -> None:
    parsed = _parse(
        "WITH first_cte AS (SELECT p.id FROM physical_source p), "
        "second_cte AS (SELECT f.id FROM first_cte f) "
        "SELECT s.id FROM second_cte s"
    )

    assert [scan.table.name for scan in parsed.table_scans] == ["physical_source"]
    assert len(parsed.cte_references) == 2
    assert [reference.cte_id for reference in parsed.cte_references] == [
        parsed.ctes[1].cte_id,
        parsed.ctes[0].cte_id,
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "WITH first_cte AS (SELECT * FROM later_cte), "
        "later_cte AS (SELECT * FROM physical_source) SELECT * FROM first_cte",
        "WITH self_cte AS (SELECT * FROM self_cte) SELECT * FROM self_cte",
    ],
)
def test_forward_and_self_cte_references_fail_closed(sql: str) -> None:
    _assert_error(sql, SqlAstErrorCode.SHAPE_UNSUPPORTED)


def test_nested_with_can_shadow_an_inherited_cte_name() -> None:
    parsed = _parse(
        "WITH shared AS (SELECT p.id FROM physical_source p), "
        "container AS ("
        "WITH shared AS (SELECT inherited.id FROM shared inherited) "
        "SELECT local.id FROM shared local"
        ") SELECT result.id FROM container result"
    )

    assert [scan.table.name for scan in parsed.table_scans] == ["physical_source"]
    assert len(parsed.ctes) == 3
    referenced_cte_ids = [reference.cte_id for reference in parsed.cte_references]
    assert parsed.ctes[0].cte_id in referenced_cte_ids
    assert parsed.ctes[1].cte_id in referenced_cte_ids
    assert parsed.ctes[2].cte_id in referenced_cte_ids


def test_future_cte_name_is_not_silently_treated_as_a_physical_table() -> None:
    _assert_error(
        "WITH first_cte AS (SELECT * FROM physical_or_future), "
        "physical_or_future AS (SELECT * FROM actual_source) "
        "SELECT * FROM first_cte",
        SqlAstErrorCode.SHAPE_UNSUPPORTED,
    )

    parsed = _parse(
        "WITH first_cte AS (SELECT * FROM public.physical_or_future), "
        "physical_or_future AS (SELECT * FROM actual_source) "
        "SELECT * FROM first_cte"
    )
    assert [(scan.table.schema, scan.table.name) for scan in parsed.table_scans] == [
        ("public", "physical_or_future"),
        (None, "actual_source"),
    ]


def test_cte_visibility_is_lexical_across_multiple_levels() -> None:
    parsed = _parse(
        "WITH level_one AS (SELECT p.id FROM physical_source p), "
        "level_two AS ("
        "WITH level_three AS (SELECT one.id FROM level_one one) "
        "SELECT three.id FROM level_three three"
        ") SELECT two.id FROM level_two two"
    )

    assert [scan.table.name for scan in parsed.table_scans] == ["physical_source"]
    assert len(parsed.ctes) == 3
    assert len(parsed.cte_references) == 3


def test_predicates_keep_location_atoms_and_boolean_structure() -> None:
    parsed = _parse(
        "SELECT a.kind, COUNT(*) FROM alpha a "
        "LEFT JOIN beta b ON a.id = b.alpha_id AND b.deleted_at IS NULL "
        "WHERE a.enabled IS TRUE OR NOT (a.score <= 0) "
        "GROUP BY a.kind HAVING COUNT(*) > 2"
    )

    assert [predicate.location for predicate in parsed.predicates] == [
        PredicateLocation.JOIN_ON,
        PredicateLocation.WHERE,
        PredicateLocation.HAVING,
    ]
    assert len(parsed.predicates[0].atoms) == 2
    assert len(parsed.predicates[1].atoms) == 2
    assert parsed.predicates[1].expression.kind == "or"
    assert any(
        node.kind == "not" for node in _walk_expression(parsed.predicates[1].expression)
    )
    assert any(
        node.kind == "null"
        for node in _walk_expression(parsed.predicates[0].expression)
    )


def test_aggregates_group_order_alias_ordinal_limit_and_offset_are_facts() -> None:
    parsed = _parse(
        "SELECT t.category, SUM(t.amount) AS total FROM totals t "
        "GROUP BY 1 ORDER BY total DESC, 1 ASC LIMIT 5 OFFSET 2"
    )

    assert [
        (aggregate.function, aggregate.distinct) for aggregate in parsed.aggregates
    ] == [("sum", False)]
    grouping = parsed.groupings[0].expression
    assert grouping.kind == "group"
    assert grouping.children == (("expressions", 0, grouping.children[0][2]),)
    assert grouping.children[0][2].kind == "ordinal_ref"
    assert parsed.orderings[0].expression.kind == "output_ref"
    assert parsed.orderings[0].descending is True
    assert parsed.orderings[1].expression.kind == "ordinal_ref"
    assert parsed.limits[0].count == 5
    assert parsed.limits[0].offset == 2


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t(id) VALUES (1)",
        "UPDATE t SET value = 1",
        "DELETE FROM t",
        "CREATE TABLE t(id INTEGER)",
        "DROP TABLE t",
        "SELECT 1 INTO created_table",
    ],
)
def test_dml_ddl_and_select_into_are_unsupported(sql: str) -> None:
    _assert_error(sql, SqlAstErrorCode.SHAPE_UNSUPPORTED)


@pytest.mark.parametrize(
    ("sql", "operation", "distinct"),
    [
        ("SELECT 1 UNION SELECT 2", SetOperationKind.UNION, True),
        ("SELECT 1 UNION ALL SELECT 2", SetOperationKind.UNION, False),
        ("SELECT 1 INTERSECT SELECT 1", SetOperationKind.INTERSECT, True),
        ("SELECT 1 EXCEPT SELECT 2", SetOperationKind.EXCEPT, True),
    ],
)
def test_set_operations_have_closed_roles_and_child_scopes(
    sql: str,
    operation: SetOperationKind,
    distinct: bool,
) -> None:
    parsed = _parse(sql)

    fact = parsed.set_operations[0]
    assert (fact.parent_scope_id, fact.query_role) == (None, QueryRole.ROOT)
    assert (fact.operation, fact.distinct) == (operation, distinct)
    scopes = {scope.scope_id: scope for scope in parsed.scopes}
    assert scopes[fact.left_scope_id].parent_scope_id == fact.scope_id
    assert scopes[fact.left_scope_id].query_role is QueryRole.SET_LEFT
    assert scopes[fact.right_scope_id].parent_scope_id == fact.scope_id
    assert scopes[fact.right_scope_id].query_role is QueryRole.SET_RIGHT


def test_cte_declared_for_a_set_operation_is_visible_to_both_operands() -> None:
    parsed = _parse(
        "WITH shared AS (SELECT 1 AS id) "
        "SELECT id FROM shared UNION ALL SELECT id FROM shared"
    )

    operation = parsed.set_operations[0]
    assert len(parsed.ctes) == 1
    assert parsed.ctes[0].declaring_scope_id == operation.scope_id
    assert len(parsed.cte_references) == 2
    assert not parsed.table_scans


def test_derived_relation_links_parent_and_child_scope() -> None:
    parsed = _parse("SELECT d.id FROM (SELECT o.id FROM orders o) d")

    root = next(scope for scope in parsed.scopes if scope.scope_id == "scope:0")
    derived = parsed.derived_relations[0]
    child = next(
        scope for scope in parsed.scopes if scope.scope_id == derived.query_scope_id
    )
    assert (root.parent_scope_id, root.query_role) == (None, QueryRole.ROOT)
    assert derived.scope_id == root.scope_id
    assert derived.lateral is False
    assert (child.parent_scope_id, child.query_role) == (
        root.scope_id,
        QueryRole.DERIVED,
    )


@pytest.mark.parametrize(
    ("sql", "role"),
    [
        (
            "SELECT (SELECT MAX(a.id) FROM alpha a) FROM beta b",
            QueryRole.SCALAR_SUBQUERY,
        ),
        (
            "SELECT b.id FROM beta b WHERE EXISTS "
            "(SELECT 1 FROM alpha a WHERE a.id = b.id)",
            QueryRole.EXISTS_SUBQUERY,
        ),
        (
            "SELECT b.id FROM beta b WHERE b.id IN (SELECT a.id FROM alpha a)",
            QueryRole.IN_SUBQUERY,
        ),
    ],
)
def test_expression_subqueries_are_explicit_child_scope_references(
    sql: str,
    role: QueryRole,
) -> None:
    parsed = _parse(sql)

    reference = parsed.subquery_refs[0]
    child = next(
        scope for scope in parsed.scopes if scope.scope_id == reference.child_scope_id
    )
    expressions = [projection.expression for projection in parsed.projections] + [
        predicate.expression for predicate in parsed.predicates
    ]
    assert reference.query_role is role
    assert (child.parent_scope_id, child.query_role) == (reference.scope_id, role)
    assert any(
        node.kind == "subquery_ref"
        and _attributes(node)["scope_id"] == reference.child_scope_id
        for expression in expressions
        for node in _walk_expression(expression)
    )


def test_qualified_correlated_column_is_an_explicit_outer_reference() -> None:
    parsed = _parse(
        "SELECT o.id FROM orders o WHERE EXISTS "
        "(SELECT 1 FROM items i WHERE i.order_id = o.id)"
    )

    child_scope_id = parsed.subquery_refs[0].child_scope_id
    child_predicate = next(
        predicate
        for predicate in parsed.predicates
        if predicate.scope_id == child_scope_id
    )
    outer = next(
        node
        for node in _walk_expression(child_predicate.expression)
        if node.kind == "outer_column"
    )
    assert _attributes(outer) == {
        "name": "id",
        "outer_scope_id": "scope:0",
        "relation_id": "scope:0:relation:0",
    }


def test_join_on_subquery_sees_left_and_own_rhs_but_not_later_relations() -> None:
    own_rhs = _parse(
        "SELECT a.id FROM alpha a JOIN beta b ON EXISTS "
        "(SELECT 1 WHERE a.id = b.alpha_id)"
    )
    child_scope_id = own_rhs.subquery_refs[0].child_scope_id
    child_predicate = next(
        predicate
        for predicate in own_rhs.predicates
        if predicate.scope_id == child_scope_id
    )
    assert {
        _attributes(node)["relation_id"]
        for node in _walk_expression(child_predicate.expression)
        if node.kind == "outer_column"
    } == {"scope:0:relation:0", "scope:0:relation:1"}

    _assert_error(
        "SELECT a.id FROM alpha a "
        "JOIN beta b ON EXISTS (SELECT 1 WHERE c.id = b.id) "
        "JOIN gamma c ON TRUE",
        SqlAstErrorCode.SHAPE_UNSUPPORTED,
    )

    full_from = _parse(
        "SELECT a.id FROM alpha a JOIN beta b ON TRUE JOIN gamma c ON TRUE "
        "WHERE EXISTS (SELECT 1 WHERE c.id = a.id)"
    )
    assert full_from.subquery_refs


def test_semi_join_on_subquery_can_see_hidden_rhs_only_at_its_owner() -> None:
    parsed = _parse(
        "SELECT a.id FROM a LEFT SEMI JOIN b "
        "ON EXISTS (SELECT 1 WHERE b.id = a.id)"
    )
    child_scope_id = parsed.subquery_refs[0].child_scope_id
    child_predicate = next(
        predicate for predicate in parsed.predicates if predicate.scope_id == child_scope_id
    )
    assert {
        _attributes(node)["relation_id"]
        for node in _walk_expression(child_predicate.expression)
        if node.kind == "outer_column"
    } == {"scope:0:relation:0", "scope:0:relation:1"}

    for sql in (
        "SELECT a.id, (SELECT b.id) FROM a LEFT SEMI JOIN b ON a.id = b.id",
        "SELECT a.id FROM a LEFT SEMI JOIN b ON a.id = b.id "
        "WHERE a.id IN (SELECT b.id)",
        "SELECT a.id FROM a LEFT SEMI JOIN b ON a.id = b.id "
        "WHERE EXISTS (SELECT 1 WHERE b.id = a.id)",
        "SELECT a.id FROM a LEFT SEMI JOIN b ON a.id = b.id "
        "CROSS JOIN LATERAL (SELECT b.id AS id) d",
    ):
        _assert_error(sql, SqlAstErrorCode.SHAPE_UNSUPPORTED)


def test_lateral_can_see_prior_relation_but_cte_and_plain_derived_table_cannot() -> (
    None
):
    lateral = _parse(
        "SELECT o.id FROM orders o CROSS JOIN LATERAL (SELECT o.id AS id) d"
    )
    outer = next(
        node
        for projection in lateral.projections
        if projection.scope_id != "scope:0"
        for node in _walk_expression(projection.expression)
        if node.kind == "outer_column"
    )
    assert _attributes(outer)["relation_id"] == "scope:0:relation:0"
    assert lateral.derived_relations[0].lateral is True

    _assert_error(
        "SELECT o.id FROM orders o JOIN (SELECT o.id) d ON TRUE",
        SqlAstErrorCode.SHAPE_UNSUPPORTED,
    )
    _assert_error(
        "WITH c AS (SELECT o.id) SELECT o.id FROM orders o",
        SqlAstErrorCode.SHAPE_UNSUPPORTED,
    )


def test_window_and_anonymous_udf_are_preserved_as_generic_expressions() -> None:
    parsed = _parse(
        "SELECT ROW_NUMBER() OVER (PARTITION BY o.kind ORDER BY o.id), "
        "my_udf(o.value) FROM orders o"
    )

    kinds = {
        node.kind
        for projection in parsed.projections
        for node in _walk_expression(projection.expression)
    }
    anonymous = next(
        node
        for projection in parsed.projections
        for node in _walk_expression(projection.expression)
        if node.kind == "anonymous"
    )
    assert "window" in kinds
    assert _attributes(anonymous)["this"] == "my_udf"


@pytest.mark.parametrize(
    "operator",
    ["ANY", "SOME", "ALL"],
)
def test_quantified_subqueries_are_generic_expressions_with_query_scope(
    operator: str,
) -> None:
    parsed = _parse(
        "SELECT o.id FROM orders o WHERE o.id = "
        f"{operator} (SELECT i.order_id FROM items i WHERE i.order_id = o.id)"
    )

    assert parsed.subquery_refs[0].query_role is QueryRole.QUANTIFIED_SUBQUERY
    assert any(
        node.kind in {"any", "all"}
        for node in _walk_expression(parsed.predicates[0].expression)
    )
    assert any(
        node.kind == "outer_column"
        for node in _walk_expression(parsed.predicates[1].expression)
    )


@pytest.mark.parametrize(
    "sql, expected_child_field",
    [
        (
            "SELECT a, b, SUM(c) FROM totals GROUP BY a, ROLLUP(b)",
            "rollup",
        ),
        ("SELECT a, b, SUM(c) FROM totals GROUP BY CUBE(a, b)", "cube"),
        (
            "SELECT a, b, SUM(c) FROM totals "
            "GROUP BY GROUPING SETS ((a, b), (a), ())",
            "grouping_sets",
        ),
    ],
)
def test_advanced_grouping_is_one_generic_group_expression(
    sql: str, expected_child_field: str
) -> None:
    parsed = _parse(sql)

    grouping = parsed.groupings[0].expression
    assert grouping.kind == "group"
    assert any(field == expected_child_field for field, _, _ in grouping.children)


def test_recursive_cte_and_relation_column_aliases_are_preserved() -> None:
    parsed = _parse(
        "WITH RECURSIVE walk(n) AS "
        "(SELECT 1 UNION ALL SELECT n + 1 FROM walk WHERE n < 3) "
        "SELECT w.n FROM walk AS w"
    )
    derived = _parse("SELECT d.x FROM (SELECT o.id FROM orders o) AS d(x)")
    lateral = _parse(
        "SELECT d.x FROM orders o CROSS JOIN LATERAL "
        "(SELECT o.id) AS d(x)"
    )
    physical = _parse("SELECT o.x FROM orders AS o(x)")

    assert parsed.ctes[0].recursive is True
    assert parsed.ctes[0].column_aliases == ("n",)
    assert parsed.cte_references[0].column_aliases == ()
    assert derived.derived_relations[0].column_aliases == ("x",)
    assert lateral.derived_relations[0].column_aliases == ("x",)
    assert physical.table_scans[0].column_aliases == ("x",)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT g.value FROM generate_series(1, 3) AS g(value)",
        "SELECT u.value FROM UNNEST(ARRAY[1, 2]) AS u(value)",
    ],
)
def test_generic_expression_relations_preserve_source_and_aliases(sql: str) -> None:
    parsed = _parse(sql)

    relation = parsed.expression_relations[0]
    assert relation.source_alias in {"g", "u"}
    assert relation.column_aliases == ("value",)
    assert relation.expression.children


@pytest.mark.parametrize(
    "sql, input_kind",
    [
        (
            "SELECT p.a FROM sales PIVOT "
            "(SUM(amount) FOR category IN ('A' AS a)) AS p",
            "table",
        ),
        (
            "SELECT l.id, p.a FROM lefts AS l RIGHT JOIN sales PIVOT "
            "(SUM(amount) FOR category IN ('A' AS a)) AS p ON l.id = p.id",
            "table",
        ),
        (
            "WITH source AS (SELECT id, category, amount FROM sales) "
            "SELECT p.a FROM source PIVOT "
            "(SUM(amount) FOR category IN ('A' AS a)) AS p",
            "cte",
        ),
        (
            "WITH source AS (SELECT id, category, amount FROM sales) "
            "SELECT p.a FROM source AS s PIVOT "
            "(SUM(s.amount) FOR s.category IN ('A' AS a)) AS p",
            "cte",
        ),
        (
            "SELECT p.a FROM sales AS s PIVOT "
            "(SUM(amount) FOR category IN ('A' AS a)) AS p",
            "table",
        ),
        (
            "SELECT a FROM sales PIVOT "
            "(SUM(amount) FOR category IN ('A' AS a))",
            "table",
        ),
    ],
)
def test_source_pivot_preserves_internal_input_and_declared_output(
    sql: str, input_kind: str
) -> None:
    parsed = _parse(sql)

    relation = parsed.expression_relations[-1]
    assert relation.source_alias in {"p", "sales"}
    assert relation.column_aliases == ("a",)
    assert len(relation.input_relation_ids) == 1
    assert relation.expression.kind == "pivot"
    if input_kind == "table":
        assert parsed.table_scans[-1].relation_id == relation.input_relation_ids[0]
    else:
        assert parsed.cte_references[-1].relation_id == relation.input_relation_ids[0]


@pytest.mark.parametrize(
    "sql, table, columns, column_aliases",
    [
        (
            "PIVOT sales ON category USING SUM(amount)",
            "sales",
            {"category", "amount"},
            (),
        ),
        (
            "PIVOT sales ON category USING SUM(amount) GROUP BY region",
            "sales",
            {"category", "amount", "region"},
            (),
        ),
        (
            "UNPIVOT monthly_sales ON jan, feb INTO NAME month VALUE sales",
            "monthly_sales",
            {"jan", "feb", "month", "sales"},
            ("month", "sales"),
        ),
    ],
)
def test_root_duckdb_transform_uses_synthetic_select_scope(
    sql: str, table: str, columns: set[str], column_aliases: tuple[str, ...]
) -> None:
    parsed = parse_sql_candidate(sql, "duckdb://", "root-transform")

    assert len(parsed.scopes) == 1
    assert parsed.scopes[0].query_role is QueryRole.ROOT
    assert len(parsed.table_scans) == len(parsed.expression_relations) == 1
    transform = parsed.expression_relations[0]
    assert parsed.table_scans[0].table.name == table
    assert transform.input_relation_ids == (parsed.table_scans[0].relation_id,)
    assert transform.expression.kind == "pivot"
    assert transform.column_aliases == column_aliases
    assert len(parsed.projections) == 1
    assert parsed.projections[0].expression.kind == "star"
    assert not parsed.limits
    assert not parsed.aggregates
    assert not parsed.groupings
    assert {
        _attributes(node)["name"]
        for node in _walk_expression(transform.expression)
        if node.kind == "column"
    } == columns


def test_outer_join_direction_is_preserved() -> None:
    left = _parse("SELECT a.id FROM a LEFT JOIN b ON a.id = b.id")
    right = _parse("SELECT a.id FROM a RIGHT JOIN b ON a.id = b.id")
    full = _parse("SELECT a.id FROM a FULL JOIN b ON a.id = b.id")

    assert dict(left.joins[0].options.attributes)["side"] == "LEFT"
    assert dict(right.joins[0].options.attributes)["side"] == "RIGHT"
    assert dict(full.joins[0].options.attributes)["side"] == "FULL"
    assert (
        len({left.candidate_digest, right.candidate_digest, full.candidate_digest}) == 3
    )


def test_schema_and_catalog_qualified_physical_name_is_preserved() -> None:
    parsed = _parse("SELECT o.id FROM analytics.sales.orders o WHERE o.id = 1")

    assert parsed.table_scans[0].table.catalog == "analytics"
    assert parsed.table_scans[0].table.schema == "sales"
    assert parsed.table_scans[0].table.name == "orders"


def test_multi_statement_is_a_closed_typed_error() -> None:
    _assert_error("SELECT 1; SELECT 2", SqlAstErrorCode.MULTI_STATEMENT)


def test_empty_or_comment_only_candidate_is_parse_failed() -> None:
    _assert_error("/* no statement */", SqlAstErrorCode.PARSE_FAILED)


def test_unknown_explicit_dialect_is_a_closed_typed_error() -> None:
    with pytest.raises(SqlAstError) as exc_info:
        parse_sql_candidate("SELECT 1", "unknown://server/database", "candidate-1")
    assert exc_info.value.code is SqlAstErrorCode.DIALECT_UNSUPPORTED


def test_parse_error_does_not_echo_candidate_literals() -> None:
    secret = "reviewer-secret-literal"

    with pytest.raises(SqlAstError) as exc_info:
        _parse(f"SELECT '{secret}' FROM")

    assert exc_info.value.code is SqlAstErrorCode.PARSE_FAILED
    assert secret not in str(exc_info.value)


def test_sql_length_and_ast_depth_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEXT_TO_SQL_MAX_SQL_LENGTH", "20")
    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT " + ", ".join(f"column_{index}" for index in range(20)))
    assert exc_info.value.code is SqlAstErrorCode.SHAPE_UNSUPPORTED

    monkeypatch.setenv("TEXT_TO_SQL_MAX_SQL_LENGTH", "50000")
    monkeypatch.setattr(sql_ast, "MAX_AST_DEPTH", 4)
    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT (((((value + 1) + 1) + 1) + 1) + 1) FROM values_table")
    assert exc_info.value.code is SqlAstErrorCode.SHAPE_UNSUPPORTED


def test_parser_never_connects_executes_or_evaluates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql import core

    calls = {"connect": 0, "executor": 0, "eval": 0, "exec": 0}

    def forbidden(name):
        def fail(*_args, **_kwargs):
            calls[name] += 1
            raise AssertionError(f"unexpected {name} call")

        return fail

    monkeypatch.setattr(sqlite3, "connect", forbidden("connect"))
    monkeypatch.setattr(core, "secure_db_executor", forbidden("executor"))
    monkeypatch.setattr(builtins, "eval", forbidden("eval"))
    monkeypatch.setattr(builtins, "exec", forbidden("exec"))

    parsed = _parse(
        "SELECT o.id FROM orders o WHERE o.note = 'SELECT * FROM secret; -- not SQL'"
    )

    assert parsed.table_scans[0].table.name == "orders"
    assert calls == {"connect": 0, "executor": 0, "eval": 0, "exec": 0}


def test_node_ids_are_stable_unique_and_candidate_id_is_not_semantics() -> None:
    sql = "SELECT a.id, a.kind FROM alpha a WHERE a.id = 1 AND a.kind IS NOT NULL"
    first = parse_sql_candidate(sql, SQLITE_DSN, "candidate-a")
    second = parse_sql_candidate(sql, SQLITE_DSN, "candidate-b")

    assert first.candidate_digest == second.candidate_digest
    assert _semantic_facts(first) == _semantic_facts(second)
    node_ids = _node_ids(first)
    assert node_ids
    assert len(node_ids) == len(set(node_ids))
    assert all(node_id.startswith("ast:") for node_id in node_ids)


def test_facts_are_immutable() -> None:
    parsed = _parse("SELECT o.id FROM orders o")

    with pytest.raises(FrozenInstanceError):
        parsed.table_scans[0].relation_id = "changed"
    with pytest.raises(FrozenInstanceError):
        parsed.projections[0].expression.kind = "changed"


def test_parser_does_not_apply_wave5_04_semantic_equivalence() -> None:
    original = _parse("SELECT a.id FROM alpha a WHERE a.id = 1 AND a.kind = 'x'")
    reordered = _parse("SELECT a.id FROM alpha a WHERE a.kind = 'x' AND a.id = 1")
    commuted = _parse("SELECT a.id FROM alpha a WHERE 1 = a.id AND a.kind = 'x'")
    in_form = _parse("SELECT a.id FROM alpha a WHERE a.id IN (1, 2)")
    or_form = _parse("SELECT a.id FROM alpha a WHERE a.id = 1 OR a.id = 2")

    assert original.candidate_digest != reordered.candidate_digest
    assert original.candidate_digest != commuted.candidate_digest
    assert in_form.candidate_digest != or_form.candidate_digest


def test_distinct_aggregate_and_count_star_are_explicit_facts() -> None:
    parsed = _parse(
        "SELECT COUNT(DISTINCT a.id), COUNT(*) FROM alpha a "
        "HAVING COUNT(DISTINCT a.id) > 1"
    )

    assert [aggregate.function for aggregate in parsed.aggregates] == [
        "count",
        "count",
        "count",
    ]
    assert [aggregate.distinct for aggregate in parsed.aggregates] == [
        True,
        False,
        True,
    ]
    assert any(
        node.kind == "star"
        for node in _walk_expression(parsed.aggregates[1].expression)
    )


def test_null_sensitive_operators_remain_structurally_different() -> None:
    parsed = _parse(
        "SELECT a.id FROM alpha a WHERE "
        "a.deleted_at IS NULL OR a.kind NOT IN ('x', NULL)"
    )
    not_null = _parse("SELECT a.id FROM alpha a WHERE a.deleted_at IS NOT NULL")

    kinds = {node.kind for node in _walk_expression(parsed.predicates[0].expression)}
    assert {"or", "is", "not", "in", "null"} <= kinds
    assert parsed.candidate_digest != not_null.candidate_digest


def test_multiple_ctes_keep_physical_scans_and_references_separate() -> None:
    parsed = _parse(
        "WITH first_cte AS (SELECT a.id FROM alpha a), "
        "second_cte AS (SELECT b.id FROM beta b) "
        "SELECT f.id FROM first_cte f "
        "JOIN second_cte s ON f.id = s.id"
    )

    assert len(parsed.ctes) == 2
    assert [scan.table.name for scan in parsed.table_scans] == ["alpha", "beta"]
    assert len(parsed.cte_references) == 2
    assert {reference.cte_id for reference in parsed.cte_references} == {
        cte.cte_id for cte in parsed.ctes
    }


def test_inner_and_cross_join_shapes_are_distinct() -> None:
    inner = _parse("SELECT a.id FROM alpha a JOIN beta b ON a.id = b.id")
    cross = _parse("SELECT a.id FROM alpha a CROSS JOIN beta b")

    assert inner.joins[0].condition is not None
    assert inner.joins[0].output_visible is True
    assert inner.joins[0].options is None
    assert cross.joins[0].output_visible is True
    assert cross.joins[0].options is not None
    assert cross.joins[0].condition is None
    assert inner.candidate_digest != cross.candidate_digest


def test_natural_and_using_joins_preserve_their_normalized_shape() -> None:
    natural = _parse("SELECT * FROM left_table NATURAL JOIN right_table")
    using = _parse("SELECT * FROM left_table JOIN right_table USING (ID, Name)")

    assert natural.joins[0].options is not None
    assert natural.joins[0].condition is None
    assert natural.joins[0].using_columns == ()
    assert using.joins[0].options is None
    assert using.joins[0].condition is None
    assert using.joins[0].using_columns == ("id", "name")
    assert natural.candidate_digest != using.candidate_digest


def test_natural_outer_join_sides_are_preserved_without_a_fake_on() -> None:
    candidates = tuple(
        _parse(sql)
        for sql in (
            "SELECT * FROM left_table NATURAL LEFT JOIN right_table",
            "SELECT * FROM left_table NATURAL RIGHT JOIN right_table",
            "SELECT * FROM left_table NATURAL FULL JOIN right_table",
        )
    )
    joins = tuple(candidate.joins[0] for candidate in candidates)

    assert all(join.options is not None for join in joins)
    assert all(join.condition is None and join.using_columns == () for join in joins)
    assert len({candidate.candidate_digest for candidate in candidates}) == 3


@pytest.mark.parametrize(
    ("sql", "option_fields", "output_visible"),
    [
        (
            "SELECT a.id FROM alpha a ASOF JOIN beta b ON a.id = b.id",
            ("method",),
            True,
        ),
        (
            "SELECT a.id FROM alpha a LEFT SEMI JOIN beta b ON a.id = b.id",
            ("kind", "side"),
            False,
        ),
        (
            "SELECT a.id FROM alpha a LEFT ANTI JOIN beta b ON a.id = b.id",
            ("kind", "side"),
            False,
        ),
    ],
)
def test_generic_join_options_keep_sqlglot_shape_and_output_visibility(
    sql: str, option_fields: tuple[str, ...], output_visible: bool
) -> None:
    parsed = _parse(sql)
    join = parsed.joins[0]

    assert join.output_visible is output_visible
    assert join.condition is not None
    assert join.options is not None
    assert tuple(field for field, _ in join.options.attributes) == option_fields


def test_exists_cannot_reference_hidden_semi_join_relation() -> None:
    _assert_error(
        "SELECT a.id FROM alpha AS a LEFT SEMI JOIN beta AS b "
        "ON a.id = b.alpha_id WHERE EXISTS "
        "(SELECT 1 FROM gamma AS g WHERE g.id = b.id)",
        SqlAstErrorCode.SHAPE_UNSUPPORTED,
    )


def test_semi_join_keeps_right_relation_for_on_but_not_downstream_projection() -> None:
    parsed = _parse(
        "SELECT a.id FROM alpha a LEFT SEMI JOIN beta b ON a.id = b.alpha_id"
    )

    assert parsed.joins[0].condition is not None
    _assert_error(
        "SELECT b.id FROM alpha a LEFT SEMI JOIN beta b ON a.id = b.alpha_id",
        SqlAstErrorCode.SHAPE_UNSUPPORTED,
    )


def test_multiple_joins_emit_one_fact_each_in_source_order() -> None:
    parsed = _parse(
        "SELECT a.id FROM alpha a "
        "JOIN beta b ON a.id = b.alpha_id "
        "JOIN gamma c ON b.id = c.beta_id"
    )

    assert [join.relation_id for join in parsed.joins] == [
        "scope:0:relation:1",
        "scope:0:relation:2",
    ]
    assert [join.left_relation_ids for join in parsed.joins] == [
        ("scope:0:relation:0",),
        ("scope:0:relation:0", "scope:0:relation:1"),
    ]


def test_mixed_physical_and_derived_sources_keep_sql_order_and_join_visibility() -> (
    None
):
    parsed = _parse(
        "SELECT a.id FROM alpha a "
        "JOIN (SELECT id FROM beta) d ON EXISTS "
        "(SELECT 1 WHERE a.id = d.id) "
        "JOIN gamma g ON g.id = d.id"
    )

    assert [(scan.table.name, scan.relation_id) for scan in parsed.table_scans] == [
        ("alpha", "scope:0:relation:0"),
        ("gamma", "scope:0:relation:2"),
        ("beta", "scope:1:relation:0"),
    ]
    assert parsed.derived_relations[0].relation_id == "scope:0:relation:1"
    assert [join.relation_id for join in parsed.joins] == [
        "scope:0:relation:1",
        "scope:0:relation:2",
    ]
    assert [join.left_relation_ids for join in parsed.joins] == [
        ("scope:0:relation:0",),
        ("scope:0:relation:0", "scope:0:relation:1"),
    ]

    _assert_error(
        "SELECT a.id FROM alpha a "
        "JOIN (SELECT id FROM beta) d ON EXISTS "
        "(SELECT 1 WHERE g.id = d.id) "
        "JOIN gamma g ON TRUE",
        SqlAstErrorCode.SHAPE_UNSUPPORTED,
    )


def test_mixed_physical_and_lateral_sources_keep_sql_order_and_prior_visibility() -> (
    None
):
    parsed = _parse(
        "SELECT a.id FROM alpha a CROSS JOIN LATERAL "
        "(SELECT a.id AS id) d JOIN gamma g ON TRUE"
    )

    assert parsed.derived_relations[0].relation_id == "scope:0:relation:1"
    assert [join.relation_id for join in parsed.joins] == [
        "scope:0:relation:1",
        "scope:0:relation:2",
    ]
    _assert_error(
        "SELECT a.id FROM alpha a CROSS JOIN LATERAL "
        "(SELECT g.id AS id) d JOIN gamma g ON TRUE",
        SqlAstErrorCode.SHAPE_UNSUPPORTED,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "WITH first_cte AS (SELECT id FROM second_cte), "
        "second_cte AS (SELECT id FROM alpha) SELECT id FROM first_cte",
        "SELECT id FROM alpha LIMIT ALL",
        "SELECT id FROM alpha LIMIT $1",
    ],
)
def test_advanced_unmodelled_shapes_fail_closed(sql: str) -> None:
    _assert_error(sql, SqlAstErrorCode.SHAPE_UNSUPPORTED)


@pytest.mark.parametrize(
    ("sql", "owner"),
    [
        (
            "SELECT o.id, ROW_NUMBER() OVER (ORDER BY o.id) AS rn "
            "FROM orders o QUALIFY o.status = 'active'",
            "scope",
        ),
        ("SELECT o.id FROM orders o FOR UPDATE", "scope"),
        ("SELECT o.id FROM orders o TABLESAMPLE BERNOULLI (10)", "table"),
        ("SELECT o.id FROM orders o ORDER BY o.id WITH FILL", "ordering"),
        ("SELECT o.id FROM orders o UNION BY NAME SELECT i.id FROM invoices i", "set"),
    ],
)
def test_generic_options_preserve_unmodelled_sqlglot_arguments(sql: str, owner: str) -> None:
    parsed = _parse(sql)
    if owner == "scope":
        option = parsed.scopes[0].options
    elif owner == "table":
        option = parsed.table_scans[0].options
    elif owner == "ordering":
        option = parsed.orderings[0].options
    else:
        option = parsed.set_operations[0].options

    assert option is not None


def test_set_operation_with_clause_is_stored_only_as_cte() -> None:
    parsed = _parse(
        "WITH source AS (SELECT id FROM alpha) "
        "SELECT id FROM source UNION SELECT id FROM gamma"
    )

    assert len(parsed.ctes) == 1
    assert parsed.set_operations[0].options is None


def test_set_operation_preserves_new_sqlglot_argument_without_allowlist() -> None:
    import sqlglot
    from sqlglot import exp

    from custom_tools.text_to_sql.adaptive._sql_ast_builder import (
        _FactBuilder,
        _validate_query_shape,
    )

    query = sqlglot.parse_one("SELECT id FROM alpha UNION SELECT id FROM beta")
    assert isinstance(query, exp.SetOperation)
    query.set("future_option", exp.Literal.number(1))

    _validate_query_shape(query)
    builder = _FactBuilder()
    builder.build(query)

    assert builder.set_operations[0].options is not None
    assert builder.set_operations[0].options.children[0][0] == "future_option"


def test_ast_node_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sql_ast, "MAX_AST_NODES", 8)

    with pytest.raises(SqlAstError) as exc_info:
        _parse("SELECT a, b, c, d FROM alpha WHERE a = 1")
    assert exc_info.value.code is SqlAstErrorCode.SHAPE_UNSUPPORTED


def test_identifier_case_and_physical_qualification_are_canonical() -> None:
    lower = _parse("SELECT orders.id FROM public.orders")
    unquoted_upper = _parse("SELECT Orders.ID FROM Public.Orders")
    quoted_mixed = _parse('SELECT "Orders"."ID" FROM "Public"."Orders"')
    catalog_forms = tuple(
        _parse(sql)
        for sql in (
            "SELECT id FROM warehouse.public.orders",
            "SELECT orders.id FROM warehouse.public.orders",
            "SELECT public.orders.id FROM warehouse.public.orders",
            "SELECT warehouse.public.orders.id FROM warehouse.public.orders",
        )
    )
    wrong_catalog = _parse("SELECT other.public.orders.id FROM warehouse.public.orders")

    assert lower.candidate_digest == unquoted_upper.candidate_digest
    assert lower.candidate_digest != quoted_mixed.candidate_digest
    assert len({item.candidate_digest for item in catalog_forms}) == 1
    assert wrong_catalog.candidate_digest != catalog_forms[0].candidate_digest
    assert _attributes(wrong_catalog.projections[0].expression) == {
        "catalog": "other",
        "name": "id",
        "schema": "public",
        "table": "orders",
    }


@pytest.mark.parametrize(
    "changed_sql",
    [
        "SELECT a.kind, a.id FROM alpha a WHERE a.id = 1 LIMIT 5",
        "SELECT a.id, a.kind FROM alpha a WHERE a.id = 2 LIMIT 5",
        "SELECT a.id, a.kind FROM alpha a WHERE a.id = 1 LIMIT 6",
    ],
)
def test_meaningful_candidate_changes_change_digest(changed_sql: str) -> None:
    original = _parse("SELECT a.id, a.kind FROM alpha a WHERE a.id = 1 LIMIT 5")
    changed = _parse(changed_sql)

    assert original.candidate_digest != changed.candidate_digest
