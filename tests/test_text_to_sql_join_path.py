"""Тесты граф-алгоритма join-пути (KMB Steiner-аппроксимация).

T2: проверяем build_join_path (custom_tools/text_to_sql/join_path.py):
- одиночная таблица / тривиальный short-circuit;
- direct A-B forward — БАЙТ-идентичность с JoinBuilder.build_joins;
- reverse edge (LEFT→RIGHT; INNER/FULL OUTER без изменений);
- multi-hop A-bridge-C: выбор минимального из двух bridge по весу;
- disconnected: success=False, недостижимый в unconnected_tables;
- cycle: лишнее ребро отброшено MST;
- дубль-рёбра между парой: минимальное по весу;
- >max_terminals → JoinPathTooManyTerminals;
- детерминизм при равных весах (перестановки входа → идентичный результат);
- weight beats hop-count.

Стиль — как tests/test_text_to_sql_join_builder.py. dsn=None → ANSI-кавычки.
"""
import pytest

from custom_tools.text_to_sql.join_builder import JoinBuilder
from custom_tools.text_to_sql.join_path import (
    JoinPathTooManyTerminals,
    build_join_path,
)


def _edge(a, b, *, a_col=None, b_col=None, jt="LEFT", weight=1.0, source="fk"):
    """Ребро-кандидат для build_join_path.

    a_col по умолчанию '<b>_id', b_col по умолчанию 'id' — как convention.
    """
    return {
        "a": a,
        "b": b,
        "a_col": a_col if a_col is not None else f"{b.lower()}_id",
        "b_col": b_col if b_col is not None else "id",
        "jt": jt,
        "weight": weight,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Тривиальные случаи
# ---------------------------------------------------------------------------

def test_single_table():
    result = build_join_path(
        main_table="A",
        required_tables={"A"},
        candidate_edges=[],
        max_terminals=8,
    )
    assert result["success"] is True
    assert result["join_clauses"] == []
    assert result["joins"] == []
    assert result["used_tables"] == {"A"}
    assert result["unconnected_tables"] == set()


def test_short_circuit_required_subset_of_main():
    # required ⊆ {main} → тривиально success, пустые clauses.
    result = build_join_path(
        main_table="A",
        required_tables=set(),
        candidate_edges=[_edge("A", "B")],
        max_terminals=8,
    )
    assert result["success"] is True
    assert result["join_clauses"] == []
    assert result["used_tables"] == {"A"}


# ---------------------------------------------------------------------------
# Direct A-B forward: байт-идентичность с greedy JoinBuilder
# ---------------------------------------------------------------------------

def test_direct_forward_byte_identical_to_greedy():
    main = "A"
    required = {"A", "B"}
    # graph-вход
    edge = _edge("A", "B", a_col="b_id", b_col="id", jt="LEFT")
    graph_result = build_join_path(
        main_table=main,
        required_tables=required,
        candidate_edges=[edge],
        max_terminals=8,
    )
    # эквивалентный greedy-вход
    builder = JoinBuilder(db_schema={})
    greedy_result = builder.build_joins(
        main_table=main,
        required_tables=required,
        joins_from_schema=[{
            "from_table": "A",
            "from_column": "b_id",
            "to_table": "B",
            "to_column": "id",
            "join_type": "LEFT",
        }],
    )
    assert graph_result["join_clauses"] == greedy_result["join_clauses"]
    assert graph_result["joins"] == greedy_result["joins"]
    assert graph_result["success"] == greedy_result["success"] is True
    assert graph_result["used_tables"] == greedy_result["used_tables"]
    assert graph_result["unconnected_tables"] == greedy_result["unconnected_tables"]
    # явная проверка ANSI-формата
    assert graph_result["join_clauses"][0] == (
        'LEFT JOIN "B" ON "A"."b_id" = "B"."id"'
    )


# ---------------------------------------------------------------------------
# Reverse edge: main=A, ребро описано B->A
# ---------------------------------------------------------------------------

def _reverse_graph(jt):
    # ребро a=B, b=A; main=A → parent==b → reverse-ветка, подключаем a=B
    return build_join_path(
        main_table="A",
        required_tables={"A", "B"},
        candidate_edges=[_edge("B", "A", a_col="a_id", b_col="id", jt=jt)],
        max_terminals=8,
    )


def _reverse_greedy(jt):
    builder = JoinBuilder(db_schema={})
    return builder.build_joins(
        main_table="A",
        required_tables={"A", "B"},
        joins_from_schema=[{
            "from_table": "B",
            "from_column": "a_id",
            "to_table": "A",
            "to_column": "id",
            "join_type": jt,
        }],
    )


def test_reverse_left_becomes_right_byte_identical():
    g = _reverse_graph("LEFT")
    greedy = _reverse_greedy("LEFT")
    assert g["join_clauses"] == greedy["join_clauses"]
    assert g["joins"] == greedy["joins"]
    assert g["join_clauses"][0].startswith('RIGHT JOIN "B"')
    assert g["joins"][0]["join_type"] == "RIGHT"


def test_reverse_inner_stays_inner():
    g = _reverse_graph("INNER")
    assert g["success"] is True
    assert g["join_clauses"][0].startswith('INNER JOIN "B"')
    assert g["joins"][0]["join_type"] == "INNER"


def test_reverse_full_outer_stays_full_outer():
    g = _reverse_graph("FULL OUTER")
    assert g["success"] is True
    assert g["join_clauses"][0].startswith('FULL OUTER JOIN "B"')
    assert g["joins"][0]["join_type"] == "FULL OUTER"


# ---------------------------------------------------------------------------
# Multi-hop: A - bridge - C, выбор минимального bridge по весу
# ---------------------------------------------------------------------------

def test_multi_hop_picks_cheaper_bridge():
    # Два пути A→C: через B1 (вес 1+1=2) и через B2 (вес 1+5=6).
    # required={A,C}, B1/B2 — нетерминалы (bridge).
    edges = [
        _edge("A", "B1", a_col="b1_id", weight=1.0),
        _edge("B1", "C", a_col="c_id", weight=1.0),
        _edge("A", "B2", a_col="b2_id", weight=1.0),
        _edge("B2", "C", a_col="c_id", weight=5.0),
    ]
    result = build_join_path(
        main_table="A",
        required_tables={"A", "C"},
        candidate_edges=edges,
        max_terminals=8,
    )
    assert result["success"] is True
    assert result["used_tables"] == {"A", "B1", "C"}
    assert "B2" not in result["used_tables"]
    assert len(result["join_clauses"]) == 2
    # цепочка корректна: A→B1, B1→C
    joined_tables = {j["to_table"] for j in result["joins"]} | {
        j["from_table"] for j in result["joins"]
    }
    assert joined_tables == {"A", "B1", "C"}


# ---------------------------------------------------------------------------
# Disconnected: C недостижим, B соединён
# ---------------------------------------------------------------------------

def test_disconnected_terminal():
    # A-B соединены; C изолирован.
    edges = [_edge("A", "B", a_col="b_id", weight=1.0)]
    result = build_join_path(
        main_table="A",
        required_tables={"A", "B", "C"},
        candidate_edges=edges,
        max_terminals=8,
    )
    assert result["success"] is False
    assert "C" in result["unconnected_tables"]
    assert "B" in result["used_tables"]
    assert "C" not in result["used_tables"]
    # B всё равно соединён (как greedy)
    assert len(result["join_clauses"]) == 1


# ---------------------------------------------------------------------------
# Cycle: лишнее ребро отброшено MST
# ---------------------------------------------------------------------------

def test_cycle_drops_extra_edge():
    # Треугольник A-B-C-A. required все три. MST оставит 2 ребра.
    edges = [
        _edge("A", "B", a_col="b_id", weight=1.0),
        _edge("B", "C", a_col="c_id", weight=1.0),
        _edge("A", "C", a_col="c_id", weight=1.0),
    ]
    result = build_join_path(
        main_table="A",
        required_tables={"A", "B", "C"},
        candidate_edges=edges,
        max_terminals=8,
    )
    assert result["success"] is True
    assert result["used_tables"] == {"A", "B", "C"}
    assert len(result["join_clauses"]) == 2  # дерево на 3 узлах = 2 ребра


# ---------------------------------------------------------------------------
# Дубль-рёбра между парой: минимальное по весу
# ---------------------------------------------------------------------------

def test_duplicate_edges_pick_min_weight():
    # Два ребра A-B с разными колонками/весами. Берётся минимальное (вес 1).
    edges = [
        _edge("A", "B", a_col="cheap_id", b_col="id", weight=1.0),
        _edge("A", "B", a_col="pricey_id", b_col="id", weight=5.0),
    ]
    result = build_join_path(
        main_table="A",
        required_tables={"A", "B"},
        candidate_edges=edges,
        max_terminals=8,
    )
    assert result["success"] is True
    assert len(result["join_clauses"]) == 1
    assert result["joins"][0]["from_column"] == "cheap_id"


# ---------------------------------------------------------------------------
# >max_terminals → JoinPathTooManyTerminals
# ---------------------------------------------------------------------------

def test_too_many_terminals_raises():
    required = {"A", "B", "C", "D"}
    edges = [
        _edge("A", "B", weight=1.0),
        _edge("B", "C", weight=1.0),
        _edge("C", "D", weight=1.0),
    ]
    with pytest.raises(JoinPathTooManyTerminals):
        build_join_path(
            main_table="A",
            required_tables=required,
            candidate_edges=edges,
            max_terminals=2,
        )


def test_too_many_terminals_short_circuit_first():
    # required⊆{main} → short-circuit ДО проверки max_terminals.
    result = build_join_path(
        main_table="A",
        required_tables={"A"},
        candidate_edges=[],
        max_terminals=0,
    )
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Детерминизм при равных весах
# ---------------------------------------------------------------------------

def test_determinism_equal_weights():
    import itertools

    edges = [
        _edge("A", "B", a_col="b_id", weight=1.0),
        _edge("B", "C", a_col="c_id", weight=1.0),
        _edge("A", "C", a_col="c_id", weight=1.0),
        _edge("C", "D", a_col="d_id", weight=1.0),
    ]
    required = {"A", "B", "C", "D"}

    baseline = build_join_path(
        main_table="A",
        required_tables=required,
        candidate_edges=list(edges),
        max_terminals=8,
    )
    # несколько перестановок входа → идентичный результат
    for perm in list(itertools.permutations(edges))[:12]:
        result = build_join_path(
            main_table="A",
            required_tables=required,
            candidate_edges=list(perm),
            max_terminals=8,
        )
        assert result["join_clauses"] == baseline["join_clauses"]
        assert result["joins"] == baseline["joins"]
        assert result["used_tables"] == baseline["used_tables"]
        assert result["unconnected_tables"] == baseline["unconnected_tables"]


# ---------------------------------------------------------------------------
# Weight beats hop-count: 2-хоп вес 2 предпочтён 1-хопу вес 5
# ---------------------------------------------------------------------------

def test_weight_beats_hop_count():
    # Прямое A→C вес 5 (1 хоп) vs A→B→C вес 1+1=2 (2 хопа).
    # KMB по весу выберет 2-хоповый путь.
    edges = [
        _edge("A", "C", a_col="c_id", weight=5.0),
        _edge("A", "B", a_col="b_id", weight=1.0),
        _edge("B", "C", a_col="c_id", weight=1.0),
    ]
    result = build_join_path(
        main_table="A",
        required_tables={"A", "C"},
        candidate_edges=edges,
        max_terminals=8,
    )
    assert result["success"] is True
    assert result["used_tables"] == {"A", "B", "C"}
    assert len(result["join_clauses"]) == 2
