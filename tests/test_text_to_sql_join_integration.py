"""Интеграционные тесты врезки граф-джойна/containment в JoinValidator (T4).

Проверяем диспетчер ``JoinValidator.build_joins`` (path_algo greedy|graph) и
приватные ``_to_candidate_edges`` / ``_apply_containment`` / ``_build_joins_graph``
из ``custom_tools/text_to_sql/schema_linking/join_validation.py``.

Ключевая инвариантность: дефолт (greedy/off) — БАЙТ-идентичен прежнему
поведению; graph на простых однопутевых схемах эквивалентен greedy; все
откаты логируются (AGENTS.md: no silent fallback); containment не трогает БД
при off и не сэмплирует FK/bridge-рёбра.

Стиль — как tests/test_text_to_sql_join_path.py / tests/test_join_containment.py.
"""
import logging
import warnings

import pytest

from custom_tools.text_to_sql import join_config
from custom_tools.text_to_sql.join_builder import JoinBuilder
from custom_tools.text_to_sql.schema_linking.join_validation import JoinValidator

# Все env-переменные join-конфига, которые надо сбросить перед каждым тестом.
_JOIN_ENV_VARS = (
    "TEXT_TO_SQL_JOINS_PATH",
    "TEXT_TO_SQL_JOINS_PROFILE",
    "TEXT_TO_SQL_JOIN_PATH_ALGO",
    "TEXT_TO_SQL_JOIN_CONTAINMENT",
    "TEXT_TO_SQL_JOIN_MIN_CONTAINMENT",
    "TEXT_TO_SQL_JOIN_PATH_MAX_TERMINALS",
    "TEXT_TO_SQL_CONTAINMENT_SAMPLE",
)

_LOGGER_NAME = "custom_tools.text_to_sql.schema_linking.join_validation"


@pytest.fixture(autouse=True)
def _reset_join_state(monkeypatch):
    """Чистый кэш конфига и отсутствие join-env до/после каждого теста."""
    for var in _JOIN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    join_config.reset_cache()
    warnings.simplefilter("ignore")
    yield
    join_config.reset_cache()


# ---------------------------------------------------------------------------
# Схемы-фикстуры
# ---------------------------------------------------------------------------

def _fk(ref, type_="integer"):
    return {"type": type_, "constraint_type": "FK", "references": ref}


def _pk(type_="integer"):
    return {"type": type_, "constraint_type": "PK"}


def _chain_schema():
    """Однопутевая FK-схема A.b_id→B.id, B.c_id→C.id."""
    return {
        "A": {"columns": {"id": _pk(), "b_id": _fk("B(id)")}},
        "B": {"columns": {"id": _pk(), "c_id": _fk("C(id)")}},
        "C": {"columns": {"id": _pk()}},
    }


def _entities(*tables):
    return [{"table": t} for t in tables]


# ---------------------------------------------------------------------------
# 1. graph == greedy на простой однопутевой схеме A→B→C
# ---------------------------------------------------------------------------

def test_graph_equals_greedy_on_single_path(monkeypatch):
    schema = _chain_schema()
    metrics = _entities("A", "B", "C")

    # greedy (default)
    greedy = JoinValidator().build_joins(metrics, [], {}, schema, main_table="A")

    # graph через env-оверрайд
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "graph")
    join_config.reset_cache()
    graph = JoinValidator().build_joins(metrics, [], {}, schema, main_table="A")

    assert graph["joins"] == greedy["joins"]
    assert graph["success"] == greedy["success"] is True
    assert set(graph["unconnected_tables"]) == set(greedy["unconnected_tables"]) == set()
    assert graph["main_table"] == greedy["main_table"] == "A"


# ---------------------------------------------------------------------------
# 2. happy-path graph: нет warning "falling back"
# ---------------------------------------------------------------------------

def test_graph_happy_path_no_fallback_warning(monkeypatch, caplog):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "graph")
    join_config.reset_cache()
    schema = _chain_schema()
    metrics = _entities("A", "B", "C")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = JoinValidator().build_joins(metrics, [], {}, schema, main_table="A")

    assert result["success"] is True
    assert "falling back" not in caplog.text


def test_join_semantics_reports_cardinality_sidecar():
    schema = _chain_schema()
    metrics = _entities("A", "B")

    result = JoinValidator().build_joins(metrics, [], {}, schema, main_table="A")

    assert result["join_semantics"][0]["cardinality"] == "many_to_one"
    assert result["join_semantics"][0]["join_type"] == "INNER"
    assert result["join_warnings"] == []


# ---------------------------------------------------------------------------
# 3. fallback too-many-terminals: max_terminals=1 + ≥2 required
# ---------------------------------------------------------------------------

def test_fallback_too_many_terminals(monkeypatch, caplog):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "graph")
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_MAX_TERMINALS", "1")
    join_config.reset_cache()
    schema = _chain_schema()
    metrics = _entities("A", "B", "C")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        graph = JoinValidator().build_joins(metrics, [], {}, schema, main_table="A")

    # эталонный greedy: тот же вход, path_algo greedy
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "greedy")
    join_config.reset_cache()
    greedy_ref = JoinValidator().build_joins(metrics, [], {}, schema, main_table="A")

    assert graph["joins"] == greedy_ref["joins"]
    assert graph["success"] == greedy_ref["success"]
    assert "too many terminals" in caplog.text
    assert "falling back" in caplog.text


# ---------------------------------------------------------------------------
# 4. fallback exception: build_join_path → raise
# ---------------------------------------------------------------------------

def test_fallback_on_exception(monkeypatch, caplog):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "graph")
    join_config.reset_cache()
    schema = _chain_schema()
    metrics = _entities("A", "B", "C")

    import custom_tools.text_to_sql.schema_linking.join_validation as jv

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(jv, "build_join_path", _boom)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        graph = JoinValidator().build_joins(metrics, [], {}, schema, main_table="A")

    # эталон greedy
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "greedy")
    join_config.reset_cache()
    greedy_ref = JoinValidator().build_joins(metrics, [], {}, schema, main_table="A")

    assert graph["joins"] == greedy_ref["joins"]
    assert graph["success"] == greedy_ref["success"] is True
    assert "Graph join-path failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "falling back" in caplog.text


# ---------------------------------------------------------------------------
# 5. containment validate отбрасывает ложное convention-ребро
#    (тестируем приватный _apply_containment напрямую — гранулярно)
# ---------------------------------------------------------------------------

def _validator_with_dsn(dsn="postgresql://u@h/db"):
    # Валидная (по схеме) dsn нужна, чтобы (а) _apply_containment видел dsn
    # truthy, (б) quote_identifier в build_join_path не падал на резолве
    # плагина. estimate_join_containment в тестах monkeypatched — реального
    # коннекта к БД нет.
    v = JoinValidator()
    v.join_builder = JoinBuilder(db_schema={}, dsn=dsn)
    return v


def test_containment_validate_drops_false_convention_edge(monkeypatch, caplog):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_CONTAINMENT", "validate")
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_MIN_CONTAINMENT", "0.1")
    join_config.reset_cache()

    import custom_tools.text_to_sql.join_containment as jc

    calls = []

    def _estimate(dsn, a, a_col, b, b_col, *, sample_size=None):
        calls.append((a, a_col, b, b_col))
        return 0.0  # ниже порога → ребро отбрасывается

    monkeypatch.setattr(jc, "estimate_join_containment", _estimate)

    edges = [
        {"a": "A", "b": "B", "a_col": "b_id", "b_col": "id",
         "jt": "INNER", "weight": 1.0, "source": "fk"},
        {"a": "X", "b": "B", "a_col": "b_id", "b_col": "id",
         "jt": "INNER", "weight": 1.0, "source": "bridge"},
        {"a": "A", "b": "C", "a_col": "c_id", "b_col": "id",
         "jt": "LEFT", "weight": 3.0, "source": "convention_fk"},
    ]
    v = _validator_with_dsn()

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        out = v._apply_containment(edges)

    # convention-ребро отброшено, FK/bridge остались.
    sources = [e["source"] for e in out]
    assert sources == ["fk", "bridge"]
    assert "Dropping low-containment join edge" in caplog.text
    # FK/bridge НЕ сэмплируются — estimate вызван только для convention-ребра.
    assert calls == [("A", "c_id", "C", "id")]


def test_containment_validate_keeps_edge_when_none(monkeypatch):
    # c is None (неопределимо) → fail-open, ребро остаётся.
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_CONTAINMENT", "validate")
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_MIN_CONTAINMENT", "0.1")
    join_config.reset_cache()

    import custom_tools.text_to_sql.join_containment as jc
    monkeypatch.setattr(
        jc, "estimate_join_containment",
        lambda *a, **k: None,
    )

    edges = [
        {"a": "A", "b": "C", "a_col": "c_id", "b_col": "id",
         "jt": "LEFT", "weight": 3.0, "source": "convention_legacy"},
    ]
    v = _validator_with_dsn()
    out = v._apply_containment(edges)
    assert len(out) == 1
    assert out[0]["source"] == "convention_legacy"


# ---------------------------------------------------------------------------
# 6. off/greedy не трогает БД и не зовёт join_path
# ---------------------------------------------------------------------------

def test_default_profile_no_db_no_join_path(monkeypatch):
    # default-профиль: greedy + containment off. Ни estimate, ни build_join_path
    # не должны вызываться; результат == прямой JoinBuilder.build_joins.
    import custom_tools.text_to_sql.join_containment as jc
    import custom_tools.text_to_sql.schema_linking.join_validation as jv

    def _explode(*args, **kwargs):
        raise AssertionError("should not be called under default profile")

    monkeypatch.setattr(jc, "estimate_join_containment", _explode)
    monkeypatch.setattr(jv, "build_join_path", _explode)

    schema = _chain_schema()
    metrics = _entities("A", "B", "C")

    result = JoinValidator().build_joins(metrics, [], {}, schema, main_table="A")

    # эталон: прямой JoinBuilder.build_joins на тех же merged_joins.
    direct = JoinBuilder(schema).build_joins(
        "A",
        {"A", "B", "C"},
        [
            {"from_table": "A", "from_column": "b_id", "to_table": "B",
             "to_column": "id", "join_type": "INNER"},
            {"from_table": "B", "from_column": "c_id", "to_table": "C",
             "to_column": "id", "join_type": "INNER"},
        ],
    )
    assert result["joins"] == direct["joins"]
    assert result["success"] == direct["success"] is True


# ---------------------------------------------------------------------------
# 7. weight mode: ложный мост получает выше вес → graph выбирает истинный
# ---------------------------------------------------------------------------

def test_weight_mode_picks_true_bridge(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "graph")
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_CONTAINMENT", "weight")
    join_config.reset_cache()

    import custom_tools.text_to_sql.join_containment as jc

    # Два альтернативных bridge между A и C: через br_true (containment 1.0) и
    # через br_false (containment 0.0). Оба — convention-рёбра (сэмплятся).
    # weight = base * (1 + (1 - c)): br_true → *1.0, br_false → *2.0.
    def _estimate(dsn, a, a_col, b, b_col, *, sample_size=None):
        if "br_false" in (a, b):
            return 0.0
        return 1.0

    monkeypatch.setattr(jc, "estimate_join_containment", _estimate)

    # merged_joins: convention-рёбра (через _source) для обоих мостов.
    merged = [
        {"from_table": "A", "from_column": "t_id", "to_table": "br_true",
         "to_column": "id", "join_type": "LEFT", "_source": "convention_fk"},
        {"from_table": "br_true", "from_column": "c_id", "to_table": "C",
         "to_column": "id", "join_type": "LEFT", "_source": "convention_fk"},
        {"from_table": "A", "from_column": "f_id", "to_table": "br_false",
         "to_column": "id", "join_type": "LEFT", "_source": "convention_fk"},
        {"from_table": "br_false", "from_column": "c_id", "to_table": "C",
         "to_column": "id", "join_type": "LEFT", "_source": "convention_fk"},
    ]
    v = _validator_with_dsn()
    result = v._build_joins_graph("A", {"A", "C"}, merged)

    assert result["success"] is True
    assert "br_true" in result["used_tables"]
    assert "br_false" not in result["used_tables"]


def test_graph_rejects_non_required_shared_parent_fanout(monkeypatch, caplog):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "graph")
    join_config.reset_cache()

    merged = [
        {
            "from_table": "A",
            "from_column": "parent_id",
            "to_table": "Y",
            "to_column": "id",
            "join_type": "LEFT",
        },
        {
            "from_table": "B",
            "from_column": "parent_id",
            "to_table": "Y",
            "to_column": "id",
            "join_type": "LEFT",
        },
    ]
    v = _validator_with_dsn()

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = v._build_joins_graph("A", {"A", "B"}, merged)

    assert result["success"] is False
    assert result["joins"] == []
    assert "unsafe shared-parent fan-out" in caplog.text
    assert "falling back" in caplog.text


def test_graph_allows_explicit_bridge_steiner(monkeypatch, caplog):
    monkeypatch.setenv("TEXT_TO_SQL_JOIN_PATH_ALGO", "graph")
    join_config.reset_cache()

    merged = [
        {
            "from_table": "Y",
            "from_column": "a_id",
            "to_table": "A",
            "to_column": "id",
            "join_type": "LEFT",
            "via_bridge": True,
        },
        {
            "from_table": "Y",
            "from_column": "b_id",
            "to_table": "B",
            "to_column": "id",
            "join_type": "LEFT",
            "via_bridge": True,
        },
    ]
    v = _validator_with_dsn()

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = v._build_joins_graph("A", {"A", "B"}, merged)

    assert result["success"] is True
    assert result["used_tables"] == {"A", "B", "Y"}
    assert "unsafe shared-parent fan-out" not in caplog.text
