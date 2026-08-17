"""Тесты для приоритезации таблиц в SchemaLimiter (EPIC 2.15)."""
import pytest

from custom_tools.text_to_sql.validators import SchemaLimiter
from custom_tools.text_to_sql.validators.schema_limiter import (
    SchemaContextBudgetExceeded,
)


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


def test_query_relevant_column_after_soft_prefix_is_retained(monkeypatch):
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "20")
    columns = {
        f"irrelevant_{index:02d}": {"type": "TEXT"}
        for index in range(20)
    }
    columns["revenue"] = {
        "type": "DECIMAL",
        "description": "canonical revenue metric",
    }
    diagnostics = {}

    limited = SchemaLimiter(priority_strategy="insertion").limit_schema_for_prompt(
        {"orders": _table(columns)},
        query_terms=["revenue"],
        diagnostics=diagnostics,
    )

    selected = list(limited["orders"]["columns"])
    assert "revenue" in selected
    assert len(selected) == 20
    assert "irrelevant_19" not in selected
    assert diagnostics["selected_columns"] == 20
    assert diagnostics["omitted_columns"] == 1


def test_table_name_and_raw_substring_do_not_make_columns_mandatory(monkeypatch):
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "1")
    diagnostics = {}

    limited = SchemaLimiter(priority_strategy="insertion").limit_schema_for_prompt(
        {
            "revenue": _table(
                {
                    "ordinary": {"type": "TEXT"},
                    "prerevenue": {"type": "TEXT"},
                    "tail": {"type": "TEXT"},
                }
            )
        },
        query_terms=["Revenue"],
        diagnostics=diagnostics,
    )

    assert list(limited["revenue"]["columns"]) == ["ordinary"]
    assert diagnostics["tables"]["revenue"]["mandatory_columns"] == 0
    assert diagnostics["soft_limit_overflow"] is False


def test_key_and_query_columns_overflow_soft_budget_explicitly(monkeypatch):
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "2")
    schema = {
        "orders": _table(
            {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "customer_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "customers(id)",
                },
                "revenue": {
                    "type": "DECIMAL",
                    "description": "requested revenue metric",
                },
                "notes": {"type": "TEXT"},
            }
        )
    }
    diagnostics = {}

    limited = SchemaLimiter(priority_strategy="insertion").limit_schema_for_prompt(
        schema,
        query_terms=["revenue"],
        diagnostics=diagnostics,
    )

    assert list(limited["orders"]["columns"]) == [
        "id",
        "customer_id",
        "revenue",
    ]
    table_diagnostics = diagnostics["tables"]["orders"]
    assert table_diagnostics["mandatory_columns"] == 3
    assert table_diagnostics["soft_limit_overflow"] == 1
    assert diagnostics["soft_limit_overflow"] is True


@pytest.mark.parametrize(
    "key_metadata",
    [
        {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
        {"type": "BIGINT", "is_primary_key": True},
        {"type": "INTEGER", "references": "parent(id)"},
    ],
    ids=["postgres", "mysql", "sqlite"],
)
def test_normalized_database_key_shapes_survive_budget(
    monkeypatch,
    key_metadata,
):
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "1")
    schema = {
        "records": _table(
            {
                "ordinary": {"type": "TEXT"},
                "key_column": key_metadata,
                "requested": {"type": "TEXT", "description": "target value"},
            }
        )
    }

    limited = SchemaLimiter(priority_strategy="insertion").limit_schema_for_prompt(
        schema,
        query_terms=["target"],
    )

    assert list(limited["records"]["columns"]) == ["key_column", "requested"]


def test_mandatory_only_schema_over_hard_cap_raises_with_diagnostics(monkeypatch):
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "1")
    schema = {
        "orders": _table(
            {
                f"primary_key_{index}": {
                    "type": "INTEGER",
                    "constraint_type": "PK",
                    "description": "required key column with long description",
                }
                for index in range(4)
            }
        )
    }
    diagnostics = {}

    with pytest.raises(SchemaContextBudgetExceeded) as exc_info:
        SchemaLimiter(priority_strategy="insertion").build_schema_summary(
            schema,
            hard_max_chars=32,
            diagnostics=diagnostics,
        )

    assert exc_info.value.reason_code == "SCHEMA_CONTEXT_BUDGET_EXCEEDED"
    assert diagnostics["hard_limit_exceeded"] is True
    assert diagnostics["mandatory_chars"] > diagnostics["hard_max_chars"]


def test_hard_cap_trim_recomputes_rendered_column_counts(monkeypatch):
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "20")
    schema = {
        "orders": _table(
            {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "notes": {"type": "TEXT"},
                "status": {"type": "TEXT"},
            }
        )
    }
    diagnostics = {}
    mandatory_summary = "orders(id:INTEGER)"

    summary = SchemaLimiter(priority_strategy="insertion").build_schema_summary(
        schema,
        hard_max_chars=len(mandatory_summary),
        diagnostics=diagnostics,
    )

    assert summary == mandatory_summary
    assert diagnostics["hard_limit_exceeded"] is False
    assert diagnostics["selected_columns"] == 1
    assert diagnostics["omitted_columns"] == 2
    assert diagnostics["tables"]["orders"]["selected_columns"] == 1
    assert diagnostics["tables"]["orders"]["omitted_columns"] == 2
    assert diagnostics["tables"]["orders"]["mandatory_columns"] == 1
    assert diagnostics["tables"]["orders"]["soft_limit_overflow"] == 0


def test_include_all_tables_bypasses_only_table_soft_limit(monkeypatch):
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "1")
    schema = {
        "first": _table({"ordinary": {"type": "TEXT"}}),
        "second": _table({"ordinary": {"type": "TEXT"}}),
        "third": _table({"ordinary": {"type": "TEXT"}}),
    }
    limiter = SchemaLimiter(priority_strategy="insertion", max_tables=1)

    limited_default = limiter.limit_schema_for_prompt(schema)
    limited_all = limiter.limit_schema_for_prompt(
        schema,
        include_all_tables=True,
    )

    assert list(limited_default) == ["first"]
    assert list(limited_all) == ["first", "second", "third"]
    assert all(
        list(table_schema["columns"]) == ["ordinary"]
        for table_schema in limited_all.values()
    )


def test_include_all_tables_preserves_mandatory_columns_in_late_table(monkeypatch):
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "1")
    schema = {
        "first": _table({"ordinary": {"type": "TEXT"}}),
        "orders": _table(
            {
                "ordinary": {"type": "TEXT"},
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "customer_id": {
                    "type": "INTEGER",
                    "constraint_type": "FK",
                    "references": "customers(id)",
                },
                "revenue": {
                    "type": "DECIMAL",
                    "description": "requested revenue metric",
                },
            }
        ),
    }

    limited = SchemaLimiter(
        priority_strategy="insertion",
        max_tables=1,
    ).limit_schema_for_prompt(
        schema,
        query_terms=["revenue"],
        include_all_tables=True,
    )

    assert list(limited) == ["first", "orders"]
    assert list(limited["orders"]["columns"]) == [
        "id",
        "customer_id",
        "revenue",
    ]


def test_include_all_tables_fails_closed_when_mandatory_context_exceeds_hard_cap(
    monkeypatch,
):
    monkeypatch.setenv("SCHEMA_MAX_COLUMNS", "1")
    schema = {
        "first": _table(
            {"first_id": {"type": "INTEGER", "constraint_type": "PK"}}
        ),
        "second": _table(
            {"second_id": {"type": "INTEGER", "constraint_type": "PK"}}
        ),
    }
    diagnostics = {}

    with pytest.raises(SchemaContextBudgetExceeded) as exc_info:
        SchemaLimiter(
            priority_strategy="insertion",
            max_tables=1,
        ).build_schema_summary(
            schema,
            hard_max_chars=10,
            diagnostics=diagnostics,
            include_all_tables=True,
        )

    assert exc_info.value.reason_code == "SCHEMA_CONTEXT_BUDGET_EXCEEDED"
    assert diagnostics["hard_limit_exceeded"] is True
    assert set(diagnostics["tables"]) == {"first", "second"}
