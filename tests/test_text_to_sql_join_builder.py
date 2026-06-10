"""Тесты инверсии join_type при reverse-edge в JoinBuilder.

EPIC 1.6: при reverse-edge (b in used_tables, а подключаем a) join_type
должен инвертироваться: LEFT<->RIGHT, симметричные (INNER/FULL/FULL OUTER/
CROSS/NATURAL) остаются без изменений. Неизвестный join_type — fail-fast.
"""
import logging

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


# ---------------------------------------------------------------------------
# T9: multi-FK warning diagnostics
# ---------------------------------------------------------------------------

def test_multi_fk_warns_on_dropped_edge(caplog):
    """Два FK-ребра A->B (author_id->id и reviewer_id->id).

    Ожидание: ровно один JOIN в результате И ровно один WARNING
    с упоминанием A, B и отброшенной колонки reviewer_id.
    """
    builder = JoinBuilder(db_schema={})
    edges = [
        {
            "from_table": "A",
            "from_column": "author_id",
            "to_table": "B",
            "to_column": "id",
            "join_type": "LEFT",
        },
        {
            "from_table": "A",
            "from_column": "reviewer_id",
            "to_table": "B",
            "to_column": "id",
            "join_type": "LEFT",
        },
    ]
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.join_builder"):
        result = builder.build_joins(
            main_table="A",
            required_tables={"A", "B"},
            joins_from_schema=edges,
        )

    assert result["success"] is True
    assert len(result["join_clauses"]) == 1

    multi_fk_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Multiple FK" in r.message
    ]
    assert len(multi_fk_warnings) == 1, (
        f"Expected 1 multi-FK warning, got {len(multi_fk_warnings)}: "
        f"{[r.message for r in multi_fk_warnings]}"
    )
    warn_msg = multi_fk_warnings[0].message
    assert "A" in warn_msg
    assert "B" in warn_msg
    assert "reviewer_id" in warn_msg


def test_single_fk_no_multi_fk_warning(caplog):
    """Одиночный FK — никакого multi-FK warning быть не должно."""
    builder = JoinBuilder(db_schema={})
    with caplog.at_level(logging.WARNING, logger="custom_tools.text_to_sql.join_builder"):
        result = builder.build_joins(
            main_table="A",
            required_tables={"A", "B"},
            joins_from_schema=[
                {
                    "from_table": "A",
                    "from_column": "author_id",
                    "to_table": "B",
                    "to_column": "id",
                    "join_type": "LEFT",
                }
            ],
        )

    assert result["success"] is True
    assert len(result["join_clauses"]) == 1

    multi_fk_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Multiple FK" in r.message
    ]
    assert len(multi_fk_warnings) == 0, (
        f"Unexpected multi-FK warning(s): {[r.message for r in multi_fk_warnings]}"
    )
