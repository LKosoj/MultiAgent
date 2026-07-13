from __future__ import annotations

import sqlite3
from copy import deepcopy

import pytest

from custom_tools.text_to_sql import schema_metadata
from custom_tools.text_to_sql.join_builder import JoinBuilder
from custom_tools.text_to_sql.join_path import build_join_path
from custom_tools.text_to_sql.schema_linking.join_validation import JoinValidator
from custom_tools.text_to_sql.schema_namespace import canonical_schema_fingerprint
from custom_tools.text_to_sql.sql_builder import build_sql_from_linked_entities
from db_plugins.duckdb import DuckDBPlugin
from db_plugins.impala import ImpalaPlugin
from db_plugins.mysql import MySQLPlugin
from db_plugins.postgres import PostgresPlugin
from db_plugins.sapiq import SAPIQPlugin
from db_plugins.sqlite import SQLitePlugin


def _column(*, type_: str = "INTEGER", reference: str = "") -> dict[str, str]:
    return {
        "type": type_,
        "constraint_type": "FK" if reference else "",
        "references": reference,
    }


def _composite_schema() -> dict[str, object]:
    return {
        "orders": {
            "columns": {
                "tenant_id": _column(reference="headers.tenant_id"),
                "order_id": _column(reference="headers.order_id"),
                "line_id": _column(reference="headers.line_id"),
                "amount": _column(),
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "orders:orders_header_fk",
                        "to_table": "headers",
                        "column_pairs": [
                            {"from_column": "tenant_id", "to_column": "tenant_id"},
                            {"from_column": "order_id", "to_column": "order_id"},
                            {"from_column": "line_id", "to_column": "line_id"},
                        ],
                    }
                ],
            },
        },
        "headers": {
            "columns": {
                "tenant_id": _column(),
                "order_id": _column(),
                "line_id": _column(),
                "label": _column(type_="TEXT"),
            },
            "foreign_keys": {"complete": True, "constraints": []},
        },
    }


def _composite_join(*, join_type: str = "INNER") -> dict[str, object]:
    return {
        "from_table": "orders",
        "from_column": "tenant_id",
        "to_table": "headers",
        "to_column": "tenant_id",
        "join_type": join_type,
        "constraint_id": "orders:orders_header_fk",
        "column_pairs": [
            {"from_column": "tenant_id", "to_column": "tenant_id"},
            {"from_column": "order_id", "to_column": "order_id"},
            {"from_column": "line_id", "to_column": "line_id"},
        ],
    }


def test_foreign_key_envelope_has_three_fail_closed_states() -> None:
    schema = _composite_schema()
    get_constraints = schema_metadata.get_foreign_key_constraints

    constraints = get_constraints("orders", schema)
    assert constraints == [
        {
            "constraint_id": "orders:orders_header_fk",
            "to_table": "headers",
            "column_pairs": [
                {"from_column": "tenant_id", "to_column": "tenant_id"},
                {"from_column": "order_id", "to_column": "order_id"},
                {"from_column": "line_id", "to_column": "line_id"},
            ],
        }
    ]

    unavailable = deepcopy(schema)
    unavailable["orders"]["foreign_keys"] = {  # type: ignore[index]
        "complete": False,
        "constraints": [],
    }
    assert get_constraints("orders", unavailable) == []

    legacy = deepcopy(schema)
    del legacy["orders"]["foreign_keys"]  # type: ignore[index]
    legacy_constraints = get_constraints("orders", legacy)
    assert len(legacy_constraints) == 3
    assert [item["column_pairs"] for item in legacy_constraints] == [
        [{"from_column": "tenant_id", "to_column": "tenant_id"}],
        [{"from_column": "order_id", "to_column": "order_id"}],
        [{"from_column": "line_id", "to_column": "line_id"}],
    ]


def test_empty_authoritative_fk_envelope_blocks_convention_fallback() -> None:
    schema = {
        "orders": {
            "columns": {
                "customer_id": _column(reference="customer.id"),
            },
            "foreign_keys": {"complete": True, "constraints": []},
        },
        "customer": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
            },
            "foreign_keys": {"complete": True, "constraints": []},
        },
    }

    assert JoinBuilder(schema, inflector=[]).infer_joins_by_convention(
        {"orders", "customer"}
    ) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda envelope: envelope.update(complete="yes"),
        lambda envelope: envelope.update(constraints={}),
        lambda envelope: envelope["constraints"].append(
            deepcopy(envelope["constraints"][0])
        ),
        lambda envelope: envelope["constraints"][0]["column_pairs"].append(
            {"from_column": "tenant_id", "to_column": "line_id"}
        ),
    ],
)
def test_invalid_authoritative_envelope_rejects_whole_constraint(mutate) -> None:
    schema = _composite_schema()
    envelope = schema["orders"]["foreign_keys"]  # type: ignore[index]
    mutate(envelope)

    with pytest.raises((TypeError, ValueError)):
        schema_metadata.get_foreign_key_constraints("orders", schema)


def test_unresolvable_fk_target_table_is_skipped_with_warning(caplog) -> None:
    """T14: одна битая FK-запись (нерезолвящийся to_table) не должна ронять

    весь дамп метаданных — она пропускается с warning, остальные валидные
    constraints по-прежнему возвращаются.
    """
    schema = _composite_schema()
    envelope = schema["orders"]["foreign_keys"]  # type: ignore[index]
    envelope["constraints"].append(
        {
            "constraint_id": "orders:broken_fk",
            "to_table": "missing_table",
            "column_pairs": [{"from_column": "amount", "to_column": "amount"}],
        }
    )

    with caplog.at_level("WARNING"):
        constraints = schema_metadata.get_foreign_key_constraints("orders", schema)

    assert [c["constraint_id"] for c in constraints] == ["orders:orders_header_fk"]
    assert "orders:broken_fk" in caplog.text
    assert "missing_table" in caplog.text


def test_direct_and_bridge_extraction_keep_constraints_atomic() -> None:
    schema = _composite_schema()
    validator = JoinValidator()

    direct = validator._extract_fk_joins({"orders", "headers"}, schema)
    assert direct == [_composite_join()]

    bridge_schema = deepcopy(schema)
    bridge_schema["products"] = {
        "columns": {"tenant_id": _column(), "product_id": _column()},
        "foreign_keys": {"complete": True, "constraints": []},
    }
    bridge_schema["order_products"] = {
        "columns": {
            "tenant_id": _column(reference="headers.tenant_id"),
            "order_id": _column(reference="headers.order_id"),
            "line_id": _column(reference="headers.line_id"),
            "product_id": _column(reference="products.product_id"),
        },
        "foreign_keys": {
            "complete": True,
            "constraints": [
                {
                    "constraint_id": "order_products:header_fk",
                    "to_table": "headers",
                    "column_pairs": deepcopy(
                        schema["orders"]["foreign_keys"]["constraints"][0][  # type: ignore[index]
                            "column_pairs"
                        ]
                    ),
                },
                {
                    "constraint_id": "order_products:product_fk",
                    "to_table": "products",
                    "column_pairs": [
                        {
                            "from_column": "product_id",
                            "to_column": "product_id",
                        }
                    ],
                },
            ],
        },
    }

    bridge = validator._extract_fk_joins({"headers", "products"}, bridge_schema)
    assert len(bridge) == 2
    assert all(item["via_bridge"] is True for item in bridge)
    header_edge = next(
        item
        for item in bridge
        if item["constraint_id"] == "order_products:header_fk"
    )
    assert len(header_edge["column_pairs"]) == 3


def test_greedy_and_graph_emit_composite_predicates_atomically() -> None:
    join = _composite_join(join_type="LEFT")
    expected = (
        'LEFT JOIN "headers" ON "orders"."tenant_id" = "headers"."tenant_id"'
        ' AND "orders"."order_id" = "headers"."order_id"'
        ' AND "orders"."line_id" = "headers"."line_id"'
    )
    greedy = JoinBuilder(_composite_schema()).build_joins(
        "orders", {"orders", "headers"}, [join]
    )
    assert greedy["join_clauses"] == [expected]
    assert greedy["joins"] == [join]

    edge = {
        "a": "orders",
        "b": "headers",
        "a_col": "tenant_id",
        "b_col": "tenant_id",
        "column_pairs": deepcopy(join["column_pairs"]),
        "constraint_id": join["constraint_id"],
        "jt": "LEFT",
        "weight": 1.0,
        "source": "fk",
    }
    graph = build_join_path(
        "orders", {"orders", "headers"}, [edge], max_terminals=4
    )
    assert graph["join_clauses"] == [expected]
    assert graph["joins"] == [join]

    reverse = build_join_path(
        "headers", {"orders", "headers"}, [edge], max_terminals=4
    )
    assert reverse["join_clauses"] == [
        expected.replace('LEFT JOIN "headers"', 'RIGHT JOIN "orders"', 1)
    ]
    assert reverse["joins"][0]["column_pairs"] == join["column_pairs"]


def test_parallel_named_constraints_remain_distinct_and_never_conjoin(caplog) -> None:
    joins = [
        {
            "from_table": "posts",
            "from_column": "author_id",
            "to_table": "users",
            "to_column": "id",
            "join_type": "INNER",
            "constraint_id": "posts:author_fk",
            "column_pairs": [{"from_column": "author_id", "to_column": "id"}],
        },
        {
            "from_table": "posts",
            "from_column": "reviewer_id",
            "to_table": "users",
            "to_column": "id",
            "join_type": "INNER",
            "constraint_id": "posts:reviewer_fk",
            "column_pairs": [{"from_column": "reviewer_id", "to_column": "id"}],
        },
    ]
    validator = JoinValidator()
    assert validator._is_duplicate_join(joins[0], [joins[1]]) is False

    result = JoinBuilder({}).build_joins("posts", {"posts", "users"}, joins)
    assert len(result["join_clauses"]) == 1
    assert " AND " not in result["join_clauses"][0]
    assert "Multiple FK edges" in caplog.text


def test_llm_join_expands_unique_composite_and_rejects_partial_pairs() -> None:
    validator = JoinValidator()
    schema = _composite_schema()

    expanded = validator.validate_llm_joins(
        [
            {
                "from_table": "orders",
                "from_column": "order_id",
                "to_table": "headers",
                "to_column": "order_id",
                "join_type": "INNER",
            }
        ],
        schema,
    )
    assert expanded == [_composite_join()]

    partial = deepcopy(_composite_join())
    partial["column_pairs"] = partial["column_pairs"][:1]
    assert validator.validate_llm_joins([partial], schema) == []


class _RowsCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql: str, params=None):
        self.executed.append((sql, params))
        return self

    def fetchall(self):
        return self.rows

    def close(self) -> None:
        pass


class _RowsConn:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.cursor_obj = _RowsCursor(rows)

    def cursor(self):
        return self.cursor_obj


def test_supported_plugins_publish_native_ordered_constraints() -> None:
    postgres_rows = [
        (
            "public",
            "orders",
            "tenant_id",
            "integer",
            "",
            "FK",
            "public.headers.tenant_id",
            "public",
            "orders_header_fk",
            1,
        ),
        (
            "public",
            "orders",
            "order_id",
            "integer",
            "",
            "FK",
            "public.headers.order_id",
            "public",
            "orders_header_fk",
            2,
        ),
    ]
    pg_schema = PostgresPlugin().introspect_schema(_RowsConn(postgres_rows))
    assert pg_schema["public.orders"]["foreign_keys"] == {
        "complete": True,
        "constraints": [
            {
                "constraint_id": "public.orders:orders_header_fk",
                "to_table": "public.headers",
                "column_pairs": [
                    {"from_column": "tenant_id", "to_column": "tenant_id"},
                    {"from_column": "order_id", "to_column": "order_id"},
                ],
            }
        ],
    }
    assert pg_schema["public.orders"]["columns"]["order_id"]["references"] == (
        "public.headers.order_id"
    )

    mysql_rows = [
        (
            "shop",
            "orders",
            "tenant_id",
            "int",
            "",
            "NO",
            None,
            "FOREIGN KEY",
            "shop.headers.tenant_id",
            "shop",
            "orders_header_fk",
            1,
        ),
        (
            "shop",
            "orders",
            "order_id",
            "int",
            "",
            "NO",
            None,
            "FOREIGN KEY",
            "shop.headers.order_id",
            "shop",
            "orders_header_fk",
            2,
        ),
    ]
    mysql_schema = MySQLPlugin().introspect_schema(_RowsConn(mysql_rows))
    assert mysql_schema["shop.orders"]["foreign_keys"]["constraints"][0][
        "column_pairs"
    ] == [
        {"from_column": "tenant_id", "to_column": "tenant_id"},
        {"from_column": "order_id", "to_column": "order_id"},
    ]

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE headers (
            tenant_id INTEGER,
            order_id INTEGER,
            PRIMARY KEY (tenant_id, order_id)
        );
        CREATE TABLE orders (
            tenant_id INTEGER,
            order_id INTEGER,
            FOREIGN KEY (tenant_id, order_id)
                REFERENCES headers (tenant_id, order_id)
        );
        """
    )
    sqlite_schema = SQLitePlugin().introspect_schema(conn)
    assert sqlite_schema["orders"]["foreign_keys"]["constraints"] == [
        {
            "constraint_id": "orders:sqlite-fk-0",
            "to_table": "headers",
            "column_pairs": [
                {"from_column": "tenant_id", "to_column": "tenant_id"},
                {"from_column": "order_id", "to_column": "order_id"},
            ],
        }
    ]


def test_sqlite_implicit_fk_target_uses_ordered_parent_primary_key() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE headers (
            tenant_id INTEGER,
            order_id INTEGER,
            PRIMARY KEY (tenant_id, order_id)
        );
        CREATE TABLE orders (
            tenant_id INTEGER,
            order_id INTEGER,
            FOREIGN KEY (tenant_id, order_id) REFERENCES headers
        );
        """
    )

    schema = SQLitePlugin().introspect_schema(conn)

    assert schema["orders"]["columns"]["tenant_id"]["references"] == (
        "headers.tenant_id"
    )
    assert schema["orders"]["columns"]["order_id"]["references"] == (
        "headers.order_id"
    )
    assert schema["orders"]["foreign_keys"] == {
        "complete": True,
        "constraints": [
            {
                "constraint_id": "orders:sqlite-fk-0",
                "to_table": "headers",
                "column_pairs": [
                    {"from_column": "tenant_id", "to_column": "tenant_id"},
                    {"from_column": "order_id", "to_column": "order_id"},
                ],
            }
        ],
    }


def test_sqlite_implicit_fk_target_cardinality_mismatch_fails_closed() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE headers (
            tenant_id INTEGER,
            order_id INTEGER,
            PRIMARY KEY (tenant_id, order_id)
        );
        CREATE TABLE orders (
            order_id INTEGER,
            FOREIGN KEY (order_id) REFERENCES headers
        );
        """
    )

    with pytest.raises(RuntimeError, match="cardinality"):
        SQLitePlugin().introspect_schema(conn)


@pytest.mark.parametrize(
    ("plugin", "rows", "table"),
    [
        (
            DuckDBPlugin(),
            [("main", "orders", "id", "INTEGER", "NO", None, "", "")],
            "main.orders",
        ),
        (
            SAPIQPlugin(),
            [("DBA", "orders", "id", "INTEGER", "NO", None, "", "")],
            "DBA.orders",
        ),
        (
            ImpalaPlugin(),
            [("analytics", "orders", "id", "INT", "NO", None)],
            "analytics.orders",
        ),
    ],
)
def test_plugins_without_ordered_native_identity_mark_grouping_unavailable(
    plugin, rows, table
) -> None:
    schema = plugin.introspect_schema(_RowsConn(rows))

    assert schema[table]["foreign_keys"] == {
        "complete": False,
        "constraints": [],
    }


class _NullSchemaValidator:
    def validate_sql_against_schema(self, sql, schema, dsn=None):
        return {"is_valid": True, "issues": []}


def test_structured_sql_and_filtered_columns_keep_all_pairs() -> None:
    join = _composite_join()
    result = build_sql_from_linked_entities(
        {
            "linked_entities": {
                "metrics": [
                    {
                        "name": "total",
                        "table": "orders",
                        "column": "amount",
                        "aggregation": "sum",
                    }
                ],
                "dimensions": [
                    {"name": "label", "table": "headers", "column": "label"}
                ],
                "filters": {},
            },
            "joins": [join],
        },
        schema_validator=_NullSchemaValidator(),
    )
    assert result["sql_query"].count(" = ") == 3

    linked = schema_metadata.ColumnMetadataHelper.extract_directly_linked_columns(
        "orders", [], [], {}, [join]
    )
    assert linked == {"tenant_id", "order_id", "line_id"}

    invalid = deepcopy(join)
    invalid["join_type"] = "CROSS"
    invalid_result = build_sql_from_linked_entities(
        {
            "linked_entities": {
                "metrics": [
                    {
                        "name": "total",
                        "table": "orders",
                        "column": "amount",
                        "aggregation": "sum",
                    }
                ],
                "dimensions": [],
                "filters": {},
            },
            "joins": [invalid],
        },
        schema_validator=_NullSchemaValidator(),
    )
    assert "error" in invalid_result


def test_fingerprint_v2_tracks_constraint_identity_and_state() -> None:
    composite = _composite_schema()
    reordered = {
        "headers": composite["headers"],
        "orders": deepcopy(composite["orders"]),
    }
    reordered["orders"]["foreign_keys"]["constraints"] = list(  # type: ignore[index]
        reversed(reordered["orders"]["foreign_keys"]["constraints"])  # type: ignore[index]
    )
    assert canonical_schema_fingerprint(composite) == canonical_schema_fingerprint(
        reordered
    )

    independent = deepcopy(composite)
    independent["orders"]["foreign_keys"] = {  # type: ignore[index]
        "complete": True,
        "constraints": [
            {
                "constraint_id": f"orders:fk-{index}",
                "to_table": "headers",
                "column_pairs": [pair],
            }
            for index, pair in enumerate(
                composite["orders"]["foreign_keys"]["constraints"][0][  # type: ignore[index]
                    "column_pairs"
                ]
            )
        ],
    }
    assert canonical_schema_fingerprint(composite) != canonical_schema_fingerprint(
        independent
    )

    legacy = deepcopy(composite)
    del legacy["orders"]["foreign_keys"]  # type: ignore[index]
    assert canonical_schema_fingerprint(composite) != canonical_schema_fingerprint(legacy)


def test_composite_join_prevents_cross_tenant_matches() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE headers (tenant_id INTEGER, order_id INTEGER, amount INTEGER);
        CREATE TABLE orders (tenant_id INTEGER, order_id INTEGER);
        INSERT INTO headers VALUES (1, 7, 10), (2, 7, 20);
        INSERT INTO orders VALUES (1, 7), (2, 7);
        """
    )
    scalar = conn.execute(
        "SELECT SUM(headers.amount) FROM orders "
        "JOIN headers ON orders.order_id = headers.order_id"
    ).fetchone()[0]
    composite = conn.execute(
        "SELECT SUM(headers.amount) FROM orders JOIN headers "
        "ON orders.tenant_id = headers.tenant_id "
        "AND orders.order_id = headers.order_id"
    ).fetchone()[0]

    assert scalar == 60
    assert composite == 30
