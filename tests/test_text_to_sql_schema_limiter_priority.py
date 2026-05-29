"""Тесты для приоритезации таблиц в SchemaLimiter (EPIC 2.15)."""
import pytest

from custom_tools.text_to_sql.validators import SchemaLimiter


def _table(columns=None, **extra):
    schema = {"columns": columns or {}}
    schema.update(extra)
    return schema


def test_priority_relevance_uses_weight_field(monkeypatch):
    """relevance: явные веса определяют топ, не порядок вставки."""
    monkeypatch.delenv("SCHEMA_PRIORITY_STRATEGY", raising=False)
    schema = {
        "a_first": _table({"id": {"type": "INT"}}, relevance=0.1),
        "b_middle": _table({"id": {"type": "INT"}}, relevance=0.95),
        "c_last": _table({"id": {"type": "INT"}}, relevance=0.5),
    }
    limiter = SchemaLimiter(priority_strategy="relevance", max_tables=2)

    limited = limiter.limit_schema_for_prompt(schema)

    assert list(limited.keys()) == ["b_middle", "c_last"]


def test_priority_relevance_falls_back_to_fk_centrality_when_no_weights(monkeypatch):
    """Без weight/relevance/score — fallback на fk_centrality, hub побеждает."""
    monkeypatch.delenv("SCHEMA_PRIORITY_STRATEGY", raising=False)
    # leaf вставлен ПЕРВЫМ; users — hub (на него ссылаются orders и payments)
    schema = {
        "leaf": _table({"id": {"type": "INT"}}),
        "users": _table({"id": {"type": "INT"}}),
        "orders": _table(
            {
                "id": {"type": "INT"},
                "user_id": {
                    "type": "INT",
                    "constraint_type": "FK",
                    "references": "users(id)",
                },
            }
        ),
        "payments": _table(
            {
                "id": {"type": "INT"},
                "user_id": {
                    "type": "INT",
                    "constraint_type": "FK",
                    "references": "users(id)",
                },
            }
        ),
    }
    limiter = SchemaLimiter(priority_strategy="relevance", max_tables=1)

    limited = limiter.limit_schema_for_prompt(schema)

    # users — hub: inbound=2 (orders, payments). leaf=0. Выбран должен быть users.
    assert list(limited.keys()) == ["users"]


def test_priority_fk_centrality_explicit(monkeypatch):
    """fk_centrality: считает in/out-degree корректно."""
    monkeypatch.delenv("SCHEMA_PRIORITY_STRATEGY", raising=False)
    schema = {
        "isolated": _table({"id": {"type": "INT"}}),
        "hub": _table({"id": {"type": "INT"}}),
        "child_a": _table(
            {
                "id": {"type": "INT"},
                "hub_id": {
                    "type": "INT",
                    "constraint_type": "FK",
                    "references": "hub(id)",
                },
            }
        ),
        "child_b": _table(
            {
                "id": {"type": "INT"},
                "hub_id": {
                    "type": "INT",
                    "constraint_type": "FK",
                    "references": "hub(id)",
                },
            }
        ),
    }
    limiter = SchemaLimiter(priority_strategy="fk_centrality", max_tables=3)

    limited = limiter.limit_schema_for_prompt(schema)
    order = list(limited.keys())

    # hub: inbound=2, outbound=0 → 2
    # child_a: outbound=1 → 1; child_b: outbound=1 → 1; isolated: 0
    assert order[0] == "hub"
    # Tie-break by insertion order: child_a раньше child_b
    assert order[1] == "child_a"
    assert order[2] == "child_b"
    assert "isolated" not in order


def test_priority_insertion_preserves_legacy_behavior(monkeypatch):
    """insertion: воспроизводит старое поведение list(db_schema)[:max_tables]."""
    monkeypatch.delenv("SCHEMA_PRIORITY_STRATEGY", raising=False)
    schema = {
        "alpha": _table({"id": {"type": "INT"}}, relevance=0.01),
        "beta": _table({"id": {"type": "INT"}}, relevance=0.99),
        "gamma": _table({"id": {"type": "INT"}}, relevance=0.5),
    }
    limiter = SchemaLimiter(priority_strategy="insertion", max_tables=2)

    limited = limiter.limit_schema_for_prompt(schema)

    assert list(limited.keys()) == ["alpha", "beta"]


def test_priority_strategy_invalid_raises(monkeypatch):
    """Неизвестная стратегия → ValueError (fail-fast)."""
    monkeypatch.delenv("SCHEMA_PRIORITY_STRATEGY", raising=False)
    with pytest.raises(ValueError):
        SchemaLimiter(priority_strategy="alphabetical")


def test_priority_strategy_env_override(monkeypatch):
    """SCHEMA_PRIORITY_STRATEGY=fk_centrality применяется без явного аргумента."""
    monkeypatch.setenv("SCHEMA_PRIORITY_STRATEGY", "fk_centrality")
    schema = {
        "leaf": _table({"id": {"type": "INT"}}),
        "hub": _table({"id": {"type": "INT"}}),
        "child": _table(
            {
                "id": {"type": "INT"},
                "hub_id": {
                    "type": "INT",
                    "constraint_type": "FK",
                    "references": "hub(id)",
                },
            }
        ),
    }
    limiter = SchemaLimiter(max_tables=1)

    assert limiter.priority_strategy == "fk_centrality"

    limited = limiter.limit_schema_for_prompt(schema)

    assert list(limited.keys()) == ["hub"]
