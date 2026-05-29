"""Тесты инверсии join_type при reverse-edge в JoinBuilder.

EPIC 1.6: при reverse-edge (b in used_tables, а подключаем a) join_type
должен инвертироваться: LEFT<->RIGHT, симметричные (INNER/FULL/FULL OUTER/
CROSS/NATURAL) остаются без изменений. Неизвестный join_type — fail-fast.
"""
import pytest

from custom_tools.text_to_sql.join_builder import JoinBuilder


def _edge(jt: str, from_table: str = "B", to_table: str = "A"):
    return {
        "from_table": from_table,
        "from_column": "a_id",
        "to_table": to_table,
        "to_column": "id",
        "join_type": jt,
    }


def _build_reverse(jt: str):
    """Строит JOIN для случая reverse-edge: main=A, ребро описано B->A."""
    builder = JoinBuilder(db_schema={})
    return builder.build_joins(
        main_table="A",
        required_tables={"A", "B"},
        joins_from_schema=[_edge(jt)],
    )


def test_reverse_edge_left_becomes_right():
    # main=A, ребро B->A LEFT: reverse-ветка подключает таблицу B,
    # join_type LEFT инвертируется в RIGHT.
    result = _build_reverse("LEFT")
    assert result["success"] is True
    assert len(result["join_clauses"]) == 1
    assert result["join_clauses"][0].startswith('RIGHT JOIN "B"')
    assert result["joins"][0]["join_type"] == "RIGHT"


def test_reverse_edge_right_becomes_left():
    result = _build_reverse("RIGHT")
    assert result["success"] is True
    assert result["join_clauses"][0].startswith('LEFT JOIN "B"')
    assert result["joins"][0]["join_type"] == "LEFT"


def test_reverse_edge_inner_stays_inner():
    result = _build_reverse("INNER")
    assert result["success"] is True
    assert result["join_clauses"][0].startswith('INNER JOIN "B"')
    assert result["joins"][0]["join_type"] == "INNER"


def test_reverse_edge_full_outer_stays_full_outer():
    result = _build_reverse("FULL OUTER")
    assert result["success"] is True
    assert result["join_clauses"][0].startswith('FULL OUTER JOIN "B"')
    assert result["joins"][0]["join_type"] == "FULL OUTER"


def test_reverse_edge_cross_natural_noop():
    for jt in ("CROSS", "NATURAL"):
        result = _build_reverse(jt)
        assert result["success"] is True, f"failed for {jt}"
        assert result["join_clauses"][0].startswith(f'{jt} JOIN "B"'), (
            f"clause for {jt}: {result['join_clauses'][0]!r}"
        )
        assert result["joins"][0]["join_type"] == jt


def test_forward_edge_unchanged():
    """Регрессия: forward-edge (a in used_tables, подключаем b) не инвертируется."""
    builder = JoinBuilder(db_schema={})
    # main=A, ребро A->B LEFT — forward: должно остаться LEFT JOIN B
    result = builder.build_joins(
        main_table="A",
        required_tables={"A", "B"},
        joins_from_schema=[_edge("LEFT", from_table="A", to_table="B")],
    )
    assert result["success"] is True
    assert result["join_clauses"][0].startswith('LEFT JOIN "B"')
    assert result["joins"][0]["join_type"] == "LEFT"


def test_unknown_join_type_raises():
    builder = JoinBuilder(db_schema={})
    with pytest.raises(ValueError, match="unsupported join_type"):
        builder.build_joins(
            main_table="A",
            required_tables={"A", "B"},
            joins_from_schema=[_edge("SIDEWAYS")],
        )
