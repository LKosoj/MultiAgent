from __future__ import annotations

import pytest

from db_plugins.base import BaseDBPlugin, ForeignKeyRow


def _row(**overrides) -> ForeignKeyRow:
    defaults = dict(
        table_key="public.orders",
        column="tenant_id",
        constraint_schema="public",
        constraint_name="orders_header_fk",
        ordinal_position=1,
        references="public.headers.tenant_id",
    )
    defaults.update(overrides)
    return ForeignKeyRow(**defaults)


def test_group_foreign_key_rows_orders_and_sorts_constraints() -> None:
    plugin = BaseDBPlugin()
    rows = [
        _row(
            column="order_id",
            ordinal_position=2,
            references="public.headers.order_id",
        ),
        _row(
            column="tenant_id",
            ordinal_position=1,
            references="public.headers.tenant_id",
        ),
        _row(
            column="warehouse_id",
            constraint_name="orders_warehouse_fk",
            ordinal_position=1,
            references="public.warehouses.id",
        ),
        # Row without a native constraint (e.g. plain column) is skipped, not grouped.
        _row(
            column="unrelated",
            constraint_schema=None,
            constraint_name=None,
            ordinal_position=None,
            references="",
        ),
    ]

    grouped = plugin._group_foreign_key_rows(rows, dialect_label="PostgreSQL")

    assert grouped == {
        "public.orders": [
            {
                "constraint_id": "public.orders:orders_header_fk",
                "to_table": "public.headers",
                "column_pairs": [
                    {"from_column": "tenant_id", "to_column": "tenant_id"},
                    {"from_column": "order_id", "to_column": "order_id"},
                ],
            },
            {
                "constraint_id": "public.orders:orders_warehouse_fk",
                "to_table": "public.warehouses",
                "column_pairs": [
                    {"from_column": "warehouse_id", "to_column": "id"},
                ],
            },
        ]
    }


@pytest.mark.parametrize("dialect_label", ["PostgreSQL", "MySQL"])
def test_group_foreign_key_rows_raises_for_incomplete_metadata(dialect_label: str) -> None:
    plugin = BaseDBPlugin()
    rows = [_row(constraint_schema=None)]

    with pytest.raises(RuntimeError) as excinfo:
        plugin._group_foreign_key_rows(rows, dialect_label=dialect_label)

    assert str(excinfo.value) == f"{dialect_label} FK metadata is incomplete for public.orders.tenant_id"


@pytest.mark.parametrize("dialect_label", ["PostgreSQL", "MySQL"])
def test_group_foreign_key_rows_raises_for_incomplete_target(dialect_label: str) -> None:
    plugin = BaseDBPlugin()
    rows = [_row(references="headers_without_column")]

    with pytest.raises(RuntimeError) as excinfo:
        plugin._group_foreign_key_rows(rows, dialect_label=dialect_label)

    assert str(excinfo.value) == f"{dialect_label} FK target is incomplete for public.orders.tenant_id"


@pytest.mark.parametrize("dialect_label", ["PostgreSQL", "MySQL"])
def test_group_foreign_key_rows_raises_for_non_contiguous_ordinals(dialect_label: str) -> None:
    plugin = BaseDBPlugin()
    rows = [
        _row(ordinal_position=1),
        _row(
            column="order_id",
            ordinal_position=3,
            references="public.headers.order_id",
        ),
    ]

    with pytest.raises(RuntimeError) as excinfo:
        plugin._group_foreign_key_rows(rows, dialect_label=dialect_label)

    assert str(excinfo.value) == (
        f"{dialect_label} FK ordinals are not contiguous for public.orders:orders_header_fk"
    )


@pytest.mark.parametrize("dialect_label", ["PostgreSQL", "MySQL"])
def test_group_foreign_key_rows_raises_for_inconsistent_target_table(dialect_label: str) -> None:
    plugin = BaseDBPlugin()
    rows = [
        _row(ordinal_position=1),
        _row(
            column="order_id",
            ordinal_position=2,
            references="public.other_table.order_id",
        ),
    ]

    with pytest.raises(RuntimeError) as excinfo:
        plugin._group_foreign_key_rows(rows, dialect_label=dialect_label)

    assert str(excinfo.value) == (
        f"{dialect_label} FK target table is inconsistent for public.orders:orders_header_fk"
    )
