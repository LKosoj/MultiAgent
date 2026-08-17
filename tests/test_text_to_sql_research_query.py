"""Strict admission tests for model-authored bounded research SQL."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import math
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.data_probes import (
    DataProbeRuntime,
    execute_raw_research_query,
)
from custom_tools.text_to_sql.adaptive.models import (
    EvidenceCost,
    ExpectedResultShape,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
)
from custom_tools.text_to_sql.adaptive.policy import (
    MAX_ACTIONS,
    MAX_DB_PROBE_MS,
    MAX_DB_PROBES,
    MAX_INLINE_BYTES,
    MAX_MODEL_DECISIONS,
    MAX_MODEL_TOKENS,
    MAX_RETURNED_ROWS,
    MAX_SAMPLE_ROWS,
    MAX_WALL_CLOCK_SECONDS,
    AdaptivePolicyConfig,
    OperationCountBudget,
    PerActionBudget,
    ResourceBudget,
    ResultVolumeBudget,
    WallClockBudget,
    canonical_action_digest,
    initial_budget_state,
)
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, read_probe_payload
from custom_tools.text_to_sql.adaptive.research_query import (
    RawResearchQuery,
    ResearchQueryAdmissionError,
    admit_research_query,
    derive_research_query_identity,
    dialect_for_plugin,
)
from custom_tools.text_to_sql.adaptive.schema_probes import SchemaProbeBudgetRuntime
from custom_tools.text_to_sql.schema_loader import LoadedSchema, SchemaLoader
from custom_tools.text_to_sql.schema_namespace import SchemaNamespace, SchemaScope
from db_plugins.sqlite import SQLitePlugin
from tests.fixtures.text_to_sql_adaptive.sqlite import create_sqlite_adaptive_fixture
from tool_runtime_context import (
    SupervisorExecutionEvidence,
    reset_tool_runtime_context,
    set_tool_runtime_context,
)
from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.deadline import DeadlineBudget


SCHEMA_VERSION = "sha256:" + "a" * 64
SCHEMA = {
    "main.orders": {
        "columns": {
            "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
            "customer_id": {"type": "INTEGER"},
            "status": {"type": "TEXT"},
        }
    },
    "main.customers": {
        "columns": {
            "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
            "name": {"type": "TEXT"},
        }
    },
    "main.line_items": {
        "columns": {
            "order_id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
            "line_no": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
            "sku": {"type": "TEXT"},
        }
    },
}
RUN_ID = "raw-run"
INCARNATION = "raw-incarnation"


def _admit(
    sql: str,
    parameters=(),
    *,
    dialect: str = "sqlite",
    maximum: int = 10,
    schema=SCHEMA,
):
    return admit_research_query(
        RawResearchQuery(sql=sql, parameters=parameters),
        schema=schema,
        dialect=dialect,
        namespace="main",
        schema_namespace_version=SCHEMA_VERSION,
        maximum_row_limit=maximum,
    )


def _reject(
    sql: str,
    code: str,
    parameters=(),
    *,
    dialect: str = "sqlite",
    schema=SCHEMA,
) -> None:
    with pytest.raises(ResearchQueryAdmissionError) as raised:
        _admit(sql, parameters, dialect=dialect, schema=schema)
    assert raised.value.failure_code == code


def test_public_query_contract_has_only_sql_and_exact_immutable_json_scalars() -> None:
    query = RawResearchQuery(
        sql="SELECT id FROM main.orders ORDER BY id LIMIT 1",
        parameters=(None, True, 1, 1.5, "x"),
    )
    assert query.parameters == (None, True, 1, 1.5, "x")

    for invalid in ([1], ({"nested": True},), ([1],), (math.nan,), (math.inf,)):
        with pytest.raises(ValidationError):
            RawResearchQuery(sql=query.sql, parameters=invalid)
    with pytest.raises(ValidationError):
        RawResearchQuery(sql=query.sql, parameters=(), probe_id="public")
    with pytest.raises(ValidationError):
        RawResearchQuery(sql=query.sql, parameters=(), output_columns=("id",))


def test_formatting_equivalent_sql_has_one_identity_and_parameters_stay_separate() -> None:
    first = _admit(
        """
        SELECT o.id AS order_id, o.status
        FROM main.orders AS o
        WHERE o.status = ?
        ORDER BY o.id
        LIMIT 5
        """,
        ("open",),
    )
    second = _admit(
        "SELECT o.id AS order_id,o.status FROM main.orders o "
        "WHERE o.status=? ORDER BY o.id LIMIT 5",
        ("closed",),
    )

    assert first.target == second.target
    assert first.normalized_sql == second.normalized_sql
    assert first.output_columns == ("order_id", "status")
    assert first.row_limit == 5
    assert first.action_parameters == (("parameter_000", "open"),)
    assert second.action_parameters == (("parameter_000", "closed"),)
    assert canonical_action_digest(
        kind=ResearchActionKind.EXECUTE_PROBE,
        hypothesis_id=None,
        target=first.target,
        parameters=first.action_parameters,
        expected_revision=0,
    ) != canonical_action_digest(
        kind=ResearchActionKind.EXECUTE_PROBE,
        hypothesis_id=None,
        target=second.target,
        parameters=second.action_parameters,
        expected_revision=0,
    )


def test_preclaim_identity_needs_no_schema_and_matches_live_admission() -> None:
    query = RawResearchQuery(
        sql="SELECT ID AS ORDER_ID FROM MAIN.ORDERS ORDER BY ID LIMIT 2"
    )
    identity = derive_research_query_identity(
        query,
        dialect="sqlite",
        namespace="main",
        schema_namespace_version=SCHEMA_VERSION,
    )
    admitted = admit_research_query(
        query,
        schema=SCHEMA,
        dialect="sqlite",
        namespace="main",
        schema_namespace_version=SCHEMA_VERSION,
        maximum_row_limit=2,
    )

    assert identity.target == admitted.target
    assert identity.normalized_sql == admitted.normalized_sql
    assert admitted.output_columns == ("order_id",)
    assert identity.action_parameters == admitted.action_parameters


def test_identity_canonicalizes_unquoted_text_but_preserves_quoted_aliases() -> None:
    variants = (
        "SELECT ID AS VALUE FROM MAIN.ORDERS ORDER BY ID LIMIT 2",
        "select id as value from main.orders order by id limit 2",
        "/* note */ SELECT id AS value FROM main.orders ORDER BY id LIMIT 2",
    )
    identities = [
        derive_research_query_identity(
            RawResearchQuery(sql=sql),
            dialect="postgres",
            namespace="main",
            schema_namespace_version=SCHEMA_VERSION,
        )
        for sql in variants
    ]
    quoted_upper = derive_research_query_identity(
        RawResearchQuery(
            sql='SELECT id AS "Value" FROM main.orders ORDER BY id LIMIT 2'
        ),
        dialect="postgres",
        namespace="main",
        schema_namespace_version=SCHEMA_VERSION,
    )
    quoted_lower = derive_research_query_identity(
        RawResearchQuery(
            sql='SELECT id AS "value" FROM main.orders ORDER BY id LIMIT 2'
        ),
        dialect="postgres",
        namespace="main",
        schema_namespace_version=SCHEMA_VERSION,
    )

    assert {identity.target for identity in identities} == {identities[0].target}
    assert {identity.normalized_sql for identity in identities} == {
        identities[0].normalized_sql
    }
    assert quoted_upper.target != quoted_lower.target


def test_preclaim_identity_keeps_distinct_raw_actions_distinct() -> None:
    queries = (
        "SELECT * FROM main.orders LIMIT 2",
        "SELECT id FROM main.orders LIMIT 2",
        "SELECT id FROM main.orders",
        "DELETE FROM main.orders",
    )
    targets = {
        derive_research_query_identity(
            RawResearchQuery(sql=sql),
            dialect="sqlite",
            namespace="main",
            schema_namespace_version=SCHEMA_VERSION,
        ).target
        for sql in queries
    }

    assert len(targets) == len(queries)


def test_quoted_schema_identifiers_keep_exact_case_semantics() -> None:
    admitted = _admit(
        'SELECT id AS "Value" FROM main.orders ORDER BY id LIMIT 2',
        dialect="postgres",
    )

    assert admitted.output_columns == ("Value",)
    _reject(
        'SELECT "ID" FROM main.orders ORDER BY "ID" LIMIT 2',
        "research_query_column",
        dialect="postgres",
    )
    _reject(
        'SELECT id FROM "main"."ORDERS" ORDER BY id LIMIT 2',
        "research_query_scope",
        dialect="postgres",
    )
    quoted_schema = {
        "main.ITEMS": {
            "columns": {
                "ID": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"}
            }
        }
    }
    quoted = _admit(
        'SELECT "ID" FROM main."ITEMS" ORDER BY "ID" LIMIT 2',
        dialect="postgres",
        schema=quoted_schema,
    )

    assert quoted.output_columns == ("ID",)
    assert quoted.normalized_sql == 'SELECT "ID" FROM main."ITEMS" ORDER BY "ID" LIMIT 2'


@pytest.mark.parametrize(
    ("sql", "code", "parameters"),
    [
        ("SELECT * FROM main.orders LIMIT 1", "research_query_star", ()),
        ("DELETE FROM main.orders", "research_query_not_select", ()),
        (
            "WITH changed AS (DELETE FROM main.orders RETURNING id) "
            "SELECT id FROM changed ORDER BY id LIMIT 1",
            "research_query_not_select",
            (),
        ),
        (
            "SELECT id INTO copied FROM main.orders ORDER BY id LIMIT 1",
            "research_query_not_select",
            (),
        ),
        (
            "SELECT id FROM main.orders ORDER BY id LIMIT 1 FOR UPDATE",
            "research_query_not_select",
            (),
        ),
        ("SELECT id FROM main.orders; SELECT id FROM main.orders", "research_query_statement_count", ()),
        ("SELECT id FROM main.orders ORDER BY id", "research_query_limit", ()),
        ("SELECT id FROM main.orders ORDER BY id LIMIT ?", "research_query_limit", (1,)),
        ("SELECT id FROM main.orders ORDER BY id LIMIT 0", "research_query_limit", ()),
        ("SELECT id FROM main.orders ORDER BY id LIMIT 11", "research_query_limit", ()),
        ("SELECT id FROM main.orders ORDER BY id LIMIT 1 OFFSET 1", "research_query_limit", ()),
        ("SELECT id FROM main.orders WHERE status = ? ORDER BY id LIMIT 1", "research_query_parameters", ()),
        ("SELECT id FROM main.orders ORDER BY id LIMIT 1", "research_query_parameters", ("extra",)),
        ("SELECT id FROM main.orders WHERE status = :status ORDER BY id LIMIT 1", "research_query_parameters", ("open",)),
    ],
)
def test_statement_limit_star_and_placeholder_contracts_fail_closed(sql, code, parameters) -> None:
    _reject(sql, code, parameters)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH selected AS (SELECT id INTO copied FROM main.orders) "
        "SELECT id FROM selected ORDER BY id LIMIT 1",
        "WITH selected AS (SELECT id FROM main.orders FOR UPDATE) "
        "SELECT id FROM selected ORDER BY id LIMIT 1",
        "SELECT id FROM (SELECT id FROM main.orders FOR SHARE) selected "
        "ORDER BY id LIMIT 1",
        "SELECT id FROM (SELECT id FROM main.orders FOR KEY SHARE) selected "
        "ORDER BY id LIMIT 1",
    ],
)
def test_nested_cte_and_derived_into_or_locks_fail_closed(sql) -> None:
    _reject(sql, "research_query_not_select", dialect="postgres")


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT id FROM missing ORDER BY id LIMIT 1", "research_query_table"),
        ("SELECT id FROM other.orders ORDER BY id LIMIT 1", "research_query_scope"),
        ("SELECT table_name FROM information_schema.tables ORDER BY table_name LIMIT 1", "research_query_scope"),
        ("SELECT name FROM sqlite_master ORDER BY name LIMIT 1", "research_query_scope"),
        ("SELECT value FROM read_csv('file.csv') ORDER BY value LIMIT 1", "research_query_row_source"),
        (
            "SELECT id FROM main.orders TABLESAMPLE BERNOULLI (10) "
            "ORDER BY id LIMIT 1",
            "research_query_row_source",
        ),
        ("SELECT missing FROM main.orders ORDER BY id LIMIT 1", "research_query_column"),
        (
            "SELECT id FROM main.orders o JOIN main.customers c ON c.id = o.customer_id "
            "ORDER BY o.id, c.id LIMIT 1",
            "research_query_column_ambiguous",
        ),
    ],
)
def test_scope_row_sources_and_columns_are_resolved_against_trusted_schema(sql, code) -> None:
    _reject(sql, code)


@pytest.mark.parametrize("table", ["shadow", "tables"])
def test_unqualified_system_table_is_rejected_after_canonical_resolution(table) -> None:
    schema = {
        "pg_catalog.shadow": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"}
            }
        },
        "information_schema.tables": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"}
            }
        },
    }

    _reject(
        f"SELECT id FROM {table} ORDER BY id LIMIT 1",
        "research_query_scope",
        schema=schema,
    )


@pytest.mark.parametrize("column", ["main.o.id", "catalog.main.o.id"])
def test_db_and_catalog_column_qualifiers_are_never_ignored(column) -> None:
    _reject(
        f"SELECT {column} FROM main.orders AS o ORDER BY {column} LIMIT 1",
        "research_query_column",
        dialect="postgres",
    )


def test_cte_subquery_and_join_aliases_are_distinguished_from_physical_tables() -> None:
    cte = _admit(
        "WITH selected AS ("
        "SELECT o.id, o.status FROM main.orders o WHERE o.status = ?"
        ") SELECT DISTINCT s.id, s.status FROM selected s "
        "ORDER BY s.id, s.status LIMIT 5",
        ("open",),
    )
    subquery = _admit(
        "SELECT DISTINCT q.id, q.status FROM ("
        "SELECT o.id, o.status FROM main.orders o"
        ") q ORDER BY q.id, q.status LIMIT 5"
    )
    joined = _admit(
        "SELECT o.id AS order_id, c.id AS customer_id "
        "FROM main.orders o JOIN main.customers c ON c.id = o.customer_id "
        "ORDER BY o.id, c.id LIMIT 5"
    )

    assert cte.output_columns == subquery.output_columns == ("id", "status")
    assert joined.output_columns == ("order_id", "customer_id")
    _reject(
        "WITH selected AS (SELECT o.missing FROM main.orders o) "
        "SELECT DISTINCT missing FROM selected ORDER BY missing LIMIT 1",
        "research_query_column",
    )


def test_db_supported_function_without_catalog_is_admitted() -> None:
    admitted = _admit(
        "SELECT substr(o.status, 1, 1) AS initial FROM main.orders AS o "
        "ORDER BY o.id LIMIT 2"
    )

    assert admitted.output_columns == ("initial",)


def test_safe_predicate_subquery_is_admitted() -> None:
    admitted = _admit(
        "SELECT o.id FROM main.orders AS o "
        "WHERE o.customer_id IN (SELECT c.id FROM main.customers AS c) "
        "ORDER BY o.id LIMIT 2"
    )

    assert admitted.output_columns == ("id",)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT id AS value, status AS value FROM main.orders ORDER BY id LIMIT 1", "research_query_output"),
        ("SELECT id + 1 FROM main.orders ORDER BY id LIMIT 1", "research_query_output"),
    ],
)
def test_output_shape_fail_closed(sql, code) -> None:
    _reject(sql, code)


@pytest.mark.parametrize(
    ("sql", "output_columns"),
    (
        (
            "SELECT id FROM main.orders "
            "WHERE id IN (SELECT id FROM main.orders LIMIT 1) "
            "ORDER BY id LIMIT 5",
            ("id",),
        ),
        (
            "SELECT id, (SELECT MAX(id) FROM main.orders) AS maximum_id "
            "FROM main.orders ORDER BY id LIMIT 5",
            ("id", "maximum_id"),
        ),
        (
            "SELECT id, ROW_NUMBER() OVER (ORDER BY id) "
            "FROM main.orders ORDER BY id LIMIT 5",
            ("id", "ROW_NUMBER() OVER (ORDER BY id)"),
        ),
    ),
)
def test_nested_select_shapes_do_not_fail_research_query_admission(
    sql: str,
    output_columns: tuple[str, ...],
) -> None:
    admitted = _admit(sql)

    assert admitted.output_columns == output_columns
    assert admitted.row_limit == 5


def test_window_projection_does_not_admit_another_unnamed_computed_output() -> None:
    _reject(
        "SELECT id + 1, ROW_NUMBER() OVER (ORDER BY id) "
        "FROM main.orders ORDER BY id LIMIT 5",
        "research_query_output",
    )


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT id FROM main.orders ORDER BY status LIMIT 5",
        "SELECT id, status FROM main.orders LIMIT 5",
        "SELECT status, COUNT(*) AS n FROM main.orders GROUP BY status LIMIT 5",
    ),
)
def test_admission_accepts_non_stable_or_absent_ordering(sql: str) -> None:
    admitted = _admit(sql)

    assert admitted.normalized_sql == sql


def test_plain_column_output_keeps_its_model_sql_label() -> None:
    admitted = _admit(
        "SELECT ID FROM main.orders ORDER BY ID LIMIT 5",
    )

    assert admitted.output_columns == ("ID",)


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT id FROM main.orders "
        "WHERE EXISTS (SELECT id + 1 FROM main.orders) ORDER BY id LIMIT 5",
        "SELECT id FROM main.orders "
        "WHERE id IN (SELECT id + 1 FROM main.orders) ORDER BY id LIMIT 5",
    ),
)
def test_predicate_subquery_allows_unnamed_computed_output(sql: str) -> None:
    admitted = _admit(sql)

    assert admitted.output_columns == ("id",)
    assert admitted.row_limit == 5


@pytest.mark.parametrize(
    "sql",
    (
        "WITH selected AS (SELECT id + 1 FROM main.orders) "
        "SELECT id FROM selected ORDER BY id LIMIT 5",
        "SELECT selected.value FROM (SELECT id + 1 FROM main.orders) AS selected "
        "ORDER BY selected.value LIMIT 5",
    ),
)
def test_named_nested_row_source_requires_computed_output_alias(sql: str) -> None:
    _reject(sql, "research_query_output")


@pytest.mark.parametrize(
("sql", "dialect"),
[
        (
            "SELECT COUNT(id) AS id FROM main.orders "
            "HAVING COUNT(id) > 0 ORDER BY id LIMIT 5",
            "sqlite",
        ),
        (
            "SELECT status, MAX(id) OVER () AS id FROM main.orders "
            "QUALIFY id > 0 ORDER BY id LIMIT 5",
            "duckdb",
        ),
        (
            "SELECT status, COUNT(id) AS n FROM main.orders "
            "ORDER BY status, n LIMIT 5",
            "sqlite",
        ),
        ("SELECT 1 AS value ORDER BY value LIMIT 5", "sqlite"),
    ],
)
def test_non_row_preserving_research_shapes_are_admitted(
    sql,
    dialect,
) -> None:
    admitted = _admit(sql, dialect=dialect)

    assert admitted.row_limit == 5


def test_alias_shadowing_is_admitted_without_an_explicit_distinct_order_key() -> None:
    implicit_sql = (
        "SELECT status AS id, id AS actual FROM main.orders ORDER BY id LIMIT 5"
    )
    explicit_sql = (
        "SELECT status AS id, id AS actual FROM main.orders "
        "ORDER BY id, actual LIMIT 5"
    )
    implicit = _admit(implicit_sql)
    explicit = _admit(explicit_sql)

    assert implicit.normalized_sql == implicit_sql
    assert explicit.normalized_sql == explicit_sql


@pytest.mark.parametrize(
    ("implicit_sql", "explicit_sql"),
    [
        (
            "SELECT id FROM main.orders ORDER BY status LIMIT 5",
            "SELECT id FROM main.orders ORDER BY status, id LIMIT 5",
        ),
        (
            "SELECT order_id, line_no FROM main.line_items ORDER BY order_id LIMIT 5",
            "SELECT order_id, line_no FROM main.line_items "
            "ORDER BY order_id, line_no LIMIT 5",
        ),
        (
            "SELECT o.id AS order_id, c.name FROM main.orders o "
            "JOIN main.customers c ON c.id = o.customer_id "
            "ORDER BY o.id LIMIT 5",
            "SELECT o.id AS order_id, c.name FROM main.orders o "
            "JOIN main.customers c ON c.id = o.customer_id "
            "ORDER BY o.id, c.name LIMIT 5",
        ),
        (
            "SELECT DISTINCT status, customer_id FROM main.orders "
            "ORDER BY status LIMIT 5",
            "SELECT DISTINCT status, customer_id FROM main.orders "
            "ORDER BY status, customer_id LIMIT 5",
        ),
        (
            "SELECT lower(status) AS normalized_status FROM main.orders "
            "ORDER BY id LIMIT 5",
            "SELECT lower(status) AS normalized_status FROM main.orders "
            "ORDER BY id, normalized_status LIMIT 5",
        ),
        (
            "SELECT id AS first_id, id AS second_id FROM main.orders LIMIT 5",
            "SELECT id AS first_id, id AS second_id FROM main.orders "
            "ORDER BY first_id LIMIT 5",
        ),
    ],
)
def test_safe_root_selects_are_admitted_without_tie_free_ordering(
    implicit_sql: str,
    explicit_sql: str,
) -> None:
    implicit = _admit(implicit_sql)
    explicit = _admit(explicit_sql)

    assert implicit.normalized_sql == derive_research_query_identity(
        RawResearchQuery(sql=implicit_sql),
        dialect="sqlite",
        namespace="main",
        schema_namespace_version=SCHEMA_VERSION,
    ).normalized_sql
    assert explicit.normalized_sql == derive_research_query_identity(
        RawResearchQuery(sql=explicit_sql),
        dialect="sqlite",
        namespace="main",
        schema_namespace_version=SCHEMA_VERSION,
    ).normalized_sql


def test_output_column_count_uses_the_existing_closed_probe_bound() -> None:
    projections = ", ".join(f"id AS id_{index}" for index in range(21))
    _reject(
        f"SELECT {projections} FROM main.orders ORDER BY id LIMIT 1",
        "research_query_output",
    )


def test_varied_closed_research_query_shapes_are_admitted() -> None:
    aggregate = _admit("SELECT COUNT(id) AS row_count FROM main.orders LIMIT 1")
    distinct = _admit(
        "SELECT DISTINCT status, customer_id FROM main.orders "
        "ORDER BY status, customer_id LIMIT 5"
    )
    line_items = _admit(
        "SELECT order_id, line_no, sku FROM main.line_items "
        "ORDER BY order_id, line_no LIMIT 5"
    )
    safe_scalar = _admit(
        "SELECT id, lower(status) AS normalized_status FROM main.orders "
        "ORDER BY id LIMIT 5"
    )
    aliased_primary_key = _admit(
        "SELECT id AS order_id, status FROM main.orders "
        "ORDER BY order_id LIMIT 5"
    )

    assert aggregate.output_columns == ("row_count",)
    assert distinct.output_columns == ("status", "customer_id")
    assert line_items.output_columns == ("order_id", "line_no", "sku")
    assert safe_scalar.output_columns == ("id", "normalized_status")
    assert aliased_primary_key.output_columns == ("order_id", "status")


@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_bare_count_star_is_an_admitted_single_row_aggregate(dialect: str) -> None:
    admitted = _admit(
        "SELECT COUNT(*) AS row_count FROM main.orders LIMIT 1",
        dialect=dialect,
    )

    assert admitted.output_columns == ("row_count",)


@pytest.mark.parametrize(
    ("sql", "dialect"),
    (
        ("SELECT * FROM main.orders LIMIT 1", "sqlite"),
        ("SELECT o.* FROM main.orders AS o LIMIT 1", "sqlite"),
        ("SELECT COUNT(o.*) AS n FROM main.orders AS o LIMIT 1", "sqlite"),
        ("SELECT SUM(*) AS n FROM main.orders LIMIT 1", "sqlite"),
        ("SELECT COUNT(DISTINCT *) AS n FROM main.orders LIMIT 1", "sqlite"),
        ("SELECT * EXCEPT (status) FROM main.orders LIMIT 1", "bigquery"),
    ),
)
def test_non_count_stars_remain_outside_the_research_contract(
    sql: str,
    dialect: str,
) -> None:
    _reject(sql, "research_query_star", dialect=dialect)


@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_grouped_aggregate_is_admitted(
    dialect: str,
) -> None:
    one_key = _admit(
        "SELECT status, COUNT(*) AS n FROM main.orders "
        "GROUP BY status ORDER BY status LIMIT 10",
        dialect=dialect,
    )
    two_keys = _admit(
        "SELECT status, customer_id, COUNT(*) AS n FROM main.orders "
        "GROUP BY status, customer_id ORDER BY status, customer_id LIMIT 10",
        dialect=dialect,
    )
    ranked = _admit(
        "SELECT status AS grouped_status, COUNT(*) AS n FROM main.orders "
        "GROUP BY status ORDER BY n DESC, status LIMIT 10",
        dialect=dialect,
    )

    assert one_key.output_columns == ("status", "n")
    assert two_keys.output_columns == ("status", "customer_id", "n")
    assert ranked.output_columns == ("grouped_status", "n")
    assert "ORDER BY status LIMIT 10" in one_key.normalized_sql
    assert "ORDER BY status, customer_id LIMIT 10" in two_keys.normalized_sql
    assert "ORDER BY n DESC, status LIMIT 10" in ranked.normalized_sql


@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_grouped_query_is_admitted_without_explicit_order(dialect: str) -> None:
    unordered = _admit(
        "SELECT status, COUNT(*) AS n FROM main.orders GROUP BY status LIMIT 10",
        dialect=dialect,
    )
    admitted = _admit(
        "SELECT status, COUNT(*) AS n FROM main.orders "
        "GROUP BY status ORDER BY status LIMIT 10",
        dialect=dialect,
    )

    assert unordered.normalized_sql == (
        "SELECT status, COUNT(*) AS n FROM main.orders GROUP BY status LIMIT 10"
    )
    assert admitted.normalized_sql == (
        "SELECT status, COUNT(*) AS n FROM main.orders "
        "GROUP BY status ORDER BY status LIMIT 10"
    )


@pytest.mark.parametrize("dialect", ("sqlite", "postgres", "mysql"))
def test_group_expression_with_order_is_admitted(dialect: str) -> None:
    sql = (
        "SELECT LOWER(status) AS normalized_status, COUNT(*) AS n "
        "FROM main.orders GROUP BY LOWER(status) ORDER BY LOWER(status) LIMIT 10"
    )
    admitted = _admit(sql, dialect=dialect)
    alias_order = _admit(
        "SELECT LOWER(status) AS normalized_status, COUNT(*) AS n "
        "FROM main.orders GROUP BY LOWER(status) "
        "ORDER BY normalized_status LIMIT 10",
        dialect=dialect,
    )

    assert "ORDER BY LOWER(status) LIMIT 10" in admitted.normalized_sql
    assert alias_order.normalized_sql.count("ORDER BY") == 1
    assert "ORDER BY normalized_status LIMIT 10" in alias_order.normalized_sql


def test_case_inside_aggregate_is_structural_sql_not_a_function_call() -> None:
    admitted = _admit(
        "SELECT "
        "SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_orders, "
        "SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_orders "
        "FROM main.orders LIMIT 1"
    )

    assert admitted.output_columns == ("open_orders", "closed_orders")


@pytest.mark.parametrize("dialect", ["sqlite", "postgres", "mysql"])
def test_dialect_specific_parsers_admit_the_same_closed_query_shape(dialect) -> None:
    admitted = _admit(
        "SELECT o.id, o.status FROM main.orders o "
        "WHERE o.status = ? ORDER BY o.id LIMIT 2",
        ("open",),
        dialect=dialect,
    )
    assert admitted.output_columns == ("id", "status")
    assert admitted.row_limit == 2


def test_plugin_dialect_is_derived_from_trusted_plugin_metadata() -> None:
    assert dialect_for_plugin(type("Plugin", (), {"dialect": "sqlite"})()) == "sqlite"
    assert dialect_for_plugin(
        type("Plugin", (), {"dialect": "impala"})()
    ) == "hive"
    assert dialect_for_plugin(
        type("Plugin", (), {"dialect": "ignored", "sqlglot_dialect": "postgres"})()
    ) == "postgres"


class _CountingLoader:
    def __init__(self, loaded: LoadedSchema) -> None:
        self.loaded = loaded
        self.calls = 0

    def load_scoped_schema(self, _schema_info, _dsn, _scope):
        self.calls += 1
        return self.loaded


class _CountingSQLitePlugin(SQLitePlugin):
    def __init__(self) -> None:
        super().__init__()
        self.executions = 0
        self.bound_calls = []

    def execute_select_bound(self, conn, sql, parameters, row_limit):
        self.executions += 1
        self.bound_calls.append((sql, parameters, row_limit))
        return super().execute_select_bound(conn, sql, parameters, row_limit)


class _CountingLedger(AdaptiveBudgetLedger):
    def __init__(self, db_path: Path) -> None:
        self.claims = 0
        super().__init__(db_path)

    def claim_execution(self, reservation, owner_token, *, now_ns):
        self.claims += 1
        return super().claim_execution(
            reservation,
            owner_token,
            now_ns=now_ns,
        )


def _scope() -> SchemaScope:
    return SchemaScope.from_mapping(
        {
            "serialization_version": 1,
            "tenant_id": "tenant-raw",
            "access_scope_id": "scope-raw",
            "connection_view_id": "connection-raw",
            "transient": True,
        }
    )


def _config() -> AdaptivePolicyConfig:
    return AdaptivePolicyConfig(
        policy_version=1,
        wall_clock=WallClockBudget(wall_clock_seconds=MAX_WALL_CLOCK_SECONDS),
        resource_limits=ResourceBudget(
            model_tokens=MAX_MODEL_TOKENS,
            db_probe_ms=MAX_DB_PROBE_MS,
        ),
        operation_counts=OperationCountBudget(
            actions=MAX_ACTIONS,
            model_decisions=MAX_MODEL_DECISIONS,
            db_probes=MAX_DB_PROBES,
        ),
        result_volume=ResultVolumeBudget(
            returned_rows=MAX_RETURNED_ROWS,
            inline_bytes=MAX_INLINE_BYTES,
        ),
        per_action=PerActionBudget(sample_rows=MAX_SAMPLE_ROWS),
    )


def _state(namespace: SchemaNamespace, config: AdaptivePolicyConfig) -> ResearchState:
    schema_version = f"sha256:{namespace.version_key}"
    return ResearchState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=schema_version,
        query_spec=QuerySpec(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=schema_version,
            query_id="raw-query",
            original_text="bounded raw research query",
            semantic_items=(),
            expected_result_shape=ExpectedResultShape.ROWS,
            global_constraints=(),
        ),
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=(),
        action_history=(),
        result_expectations=(),
        budget_state=initial_budget_state(config),
        stop_reason=None,
    )


def _budget(
    path: Path,
    namespace: SchemaNamespace,
    admission,
    *,
    suffix: str,
) -> tuple[SchemaProbeBudgetRuntime, _CountingLedger]:
    config = _config()
    action = ResearchAction(
        action_id=f"action-{suffix}",
        kind=ResearchActionKind.EXECUTE_PROBE,
        hypothesis_id=None,
        target=admission.target,
        parameters=admission.action_parameters,
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.EXECUTE_PROBE,
            hypothesis_id=None,
            target=admission.target,
            parameters=admission.action_parameters,
            expected_revision=0,
        ),
        expected_revision=0,
    )
    ledger = _CountingLedger(path / f"{suffix}-budget.sqlite")
    return (
        SchemaProbeBudgetRuntime(
            state=_state(namespace, config),
            action=action,
            maximum_cost=EvidenceCost(
                wall_clock_ms=1_000,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=1_000,
                rows=2,
                bytes=200_000,
            ),
            config=config,
            ledger=ledger,
            invocation_id=f"invocation-{suffix}",
            utc_now=lambda: datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
        ),
        ledger,
    )


def _sqlite_execution_fixture(tmp_path: Path):
    database = create_sqlite_adaptive_fixture(
        "F01_CONVENTIONAL_STAR",
        tmp_path / "raw-research.sqlite",
    )
    dsn = f"sqlite://{database}"
    loaded = SchemaLoader(tmp_path / "initial-schema").load_scoped_schema(
        {},
        dsn,
        _scope(),
    )
    loader = _CountingLoader(loaded)
    plugin = _CountingSQLitePlugin()
    runtime = DataProbeRuntime(
        loader=loader,
        dsn=dsn,
        scope=loaded.namespace.scope,
        namespace=loaded.namespace,
        table_namespace="main",
        deadline=DeadlineBudget.from_duration(30),
        get_plugin=lambda _dsn: plugin,
    )
    return loaded, loader, plugin, runtime


def _supervised(call):
    token = set_tool_runtime_context(
        {"supervisor_evidence": SupervisorExecutionEvidence("raw-query-test", 1)}
    )
    try:
        return call()
    finally:
        reset_tool_runtime_context(token)


def test_raw_query_reuses_one_schema_load_claim_and_real_sqlite_execution(tmp_path) -> None:
    loaded, loader, plugin, runtime = _sqlite_execution_fixture(tmp_path)
    query = RawResearchQuery(
        sql=(
            "SELECT f.sale_id, f.sale_value FROM sales_fact f "
            "WHERE f.sale_value > ? ORDER BY f.sale_id LIMIT 2"
        ),
        parameters=(5.0,),
    )
    admission = derive_research_query_identity(
        query,
        dialect="sqlite",
        namespace="main",
        schema_namespace_version=f"sha256:{loaded.namespace.version_key}",
    )
    budget, ledger = _budget(tmp_path, loaded.namespace, admission, suffix="success")
    try:
        result = _supervised(
            lambda: execute_raw_research_query(query, runtime=runtime, budget=budget)
        )
        payload = read_probe_payload(result)

        assert result.status is ProbeStatus.SUCCESS
        assert payload["columns"] == ["sale_id", "sale_value"]
        assert payload["rows"] == [[1, 12.0], [2, 7.0]]
        assert loader.calls == 1
        assert ledger.claims == 1
        assert plugin.executions == 1
        assert plugin.bound_calls == [
            (
                "SELECT f.sale_id, f.sale_value FROM sales_fact AS f "
                "WHERE f.sale_value > ? ORDER BY f.sale_id LIMIT 2",
                (5.0,),
                2,
            )
        ]
        records = ledger.load_records(RUN_ID, INCARNATION)
        assert len(records) == 1
        assert records[0].result == result
        assert records[0].reconciliation is not None
    finally:
        ledger.close()


def test_raw_query_accepts_the_admitted_budget_row_limit(tmp_path) -> None:
    loaded, _loader, plugin, runtime = _sqlite_execution_fixture(tmp_path)
    query = RawResearchQuery(
        sql=(
            "SELECT sale_id, sale_value FROM sales_fact "
            "WHERE sale_value > ? ORDER BY sale_id LIMIT 100"
        ),
        parameters=(0.0,),
    )
    identity = derive_research_query_identity(
        query,
        dialect="sqlite",
        namespace="main",
        schema_namespace_version=f"sha256:{loaded.namespace.version_key}",
    )
    budget, ledger = _budget(tmp_path, loaded.namespace, identity, suffix="limit-100")
    budget = replace(
        budget,
        maximum_cost=EvidenceCost(
            wall_clock_ms=budget.maximum_cost.wall_clock_ms,
            model_calls=budget.maximum_cost.model_calls,
            model_tokens=budget.maximum_cost.model_tokens,
            db_probe_ms=budget.maximum_cost.db_probe_ms,
            rows=100,
            bytes=budget.maximum_cost.bytes,
        ),
    )
    try:
        result = _supervised(
            lambda: execute_raw_research_query(query, runtime=runtime, budget=budget)
        )

        assert result.status is ProbeStatus.SUCCESS
        assert plugin.executions == 1
        assert plugin.bound_calls == [
            (
                "SELECT sale_id, sale_value FROM sales_fact "
                "WHERE sale_value > ? ORDER BY sale_id LIMIT 100",
                (0.0,),
                100,
            )
        ]
    finally:
        ledger.close()


def test_raw_grouped_query_executes_with_explicit_order(tmp_path) -> None:
    loaded, loader, plugin, runtime = _sqlite_execution_fixture(tmp_path)
    query = RawResearchQuery(
        sql=(
            "SELECT sale_value, COUNT(*) AS n FROM sales_fact "
            "WHERE sale_value > ? GROUP BY sale_value ORDER BY sale_value LIMIT 2"
        ),
        parameters=(0.0,),
    )
    identity = derive_research_query_identity(
        query,
        dialect="sqlite",
        namespace="main",
        schema_namespace_version=f"sha256:{loaded.namespace.version_key}",
    )
    budget, ledger = _budget(
        tmp_path,
        loaded.namespace,
        identity,
        suffix="group-order",
    )
    try:
        result = _supervised(
            lambda: execute_raw_research_query(query, runtime=runtime, budget=budget)
        )

        assert result.status is ProbeStatus.SUCCESS
        assert loader.calls == 1
        assert ledger.claims == 1
        assert plugin.executions == 1
        assert plugin.bound_calls == [
            (
                "SELECT sale_value, COUNT(*) AS n FROM sales_fact "
                "WHERE sale_value > ? GROUP BY sale_value ORDER BY sale_value LIMIT 2",
                (0.0,),
                2,
            )
        ]
    finally:
        ledger.close()


def test_unquoted_uppercase_sqlite_columns_execute_with_canonical_shape(tmp_path) -> None:
    database = tmp_path / "uppercase.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE ITEMS (ID INTEGER PRIMARY KEY, STATUS TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO ITEMS(ID, STATUS) VALUES (?, ?)",
            [(1, "OPEN"), (2, "CLOSED")],
        )
    dsn = f"sqlite://{database}"
    loaded = SchemaLoader(tmp_path / "uppercase-schema").load_scoped_schema(
        {},
        dsn,
        _scope(),
    )
    loader = _CountingLoader(loaded)
    plugin = _CountingSQLitePlugin()
    runtime = DataProbeRuntime(
        loader=loader,
        dsn=dsn,
        scope=loaded.namespace.scope,
        namespace=loaded.namespace,
        table_namespace="main",
        deadline=DeadlineBudget.from_duration(30),
        get_plugin=lambda _dsn: plugin,
    )
    query = RawResearchQuery(
        sql=(
            "SELECT ID, STATUS FROM ITEMS WHERE STATUS <> ? "
            "ORDER BY ID LIMIT 2"
        ),
        parameters=("MISSING",),
    )
    identity = derive_research_query_identity(
        query,
        dialect="sqlite",
        namespace="main",
        schema_namespace_version=f"sha256:{loaded.namespace.version_key}",
    )
    budget, ledger = _budget(
        tmp_path,
        loaded.namespace,
        identity,
        suffix="uppercase-columns",
    )
    try:
        result = _supervised(
            lambda: execute_raw_research_query(query, runtime=runtime, budget=budget)
        )
        payload = read_probe_payload(result)

        assert result.status is ProbeStatus.SUCCESS
        assert payload["columns"] == ["ID", "STATUS"]
        assert payload["rows"] == [[1, "OPEN"], [2, "CLOSED"]]
        assert loader.calls == 1
        assert ledger.claims == 1
        assert plugin.executions == 1
        assert plugin.bound_calls == [
            (
                "SELECT id, status FROM items WHERE status <> ? ORDER BY id LIMIT 2",
                ("MISSING",),
                2,
            )
        ]
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("sql", "failure_code", "suffix"),
    [
        ("SELECT * FROM sales_fact LIMIT 2", "research_query_star", "star"),
        ("DELETE FROM sales_fact", "research_query_not_select", "mutation"),
        (
            "SELECT sale_id FROM sales_fact ORDER BY sale_id",
            "research_query_limit",
            "limit",
        ),
    ],
)
def test_same_unsafe_raw_query_is_one_typed_reconciled_failure_without_execution(
    tmp_path,
    sql,
    failure_code,
    suffix,
) -> None:
    loaded, loader, plugin, runtime = _sqlite_execution_fixture(tmp_path)
    rejected = RawResearchQuery(sql=sql)
    identity = derive_research_query_identity(
        rejected,
        dialect="sqlite",
        namespace="main",
        schema_namespace_version=f"sha256:{loaded.namespace.version_key}",
    )
    budget, ledger = _budget(tmp_path, loaded.namespace, identity, suffix=suffix)
    try:
        result = execute_raw_research_query(rejected, runtime=runtime, budget=budget)

        assert result.status is ProbeStatus.FAILED
        assert result.failure_code == failure_code
        assert loader.calls == 1
        assert ledger.claims == 1
        assert plugin.executions == 0
        records = ledger.load_records(RUN_ID, INCARNATION)
        assert len(records) == 1
        assert records[0].result == result
        assert records[0].reconciliation is not None
    finally:
        ledger.close()


def test_live_admission_reconciles_preclaim_target_mismatch_once(tmp_path) -> None:
    loaded, loader, plugin, runtime = _sqlite_execution_fixture(tmp_path)
    preclaimed = derive_research_query_identity(
        RawResearchQuery(
            sql="SELECT sale_id FROM sales_fact ORDER BY sale_id LIMIT 2"
        ),
        dialect="sqlite",
        namespace="main",
        schema_namespace_version=f"sha256:{loaded.namespace.version_key}",
    )
    different_query = RawResearchQuery(
        sql="SELECT sale_value FROM sales_fact ORDER BY sale_id LIMIT 2"
    )
    budget, ledger = _budget(
        tmp_path,
        loaded.namespace,
        preclaimed,
        suffix="target-mismatch",
    )
    try:
        result = execute_raw_research_query(
            different_query,
            runtime=runtime,
            budget=budget,
        )

        assert result.status is ProbeStatus.FAILED
        assert result.failure_code == "action_mismatch"
        assert loader.calls == 1
        assert ledger.claims == 1
        assert plugin.executions == 0
        records = ledger.load_records(RUN_ID, INCARNATION)
        assert len(records) == 1
        assert records[0].result == result
        assert records[0].reconciliation is not None
    finally:
        ledger.close()
