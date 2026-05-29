import os


def test_allowed_artifacts_always_allows_general(monkeypatch):
    """Даже если general отсутствует в allowed_artifacts, он должен быть разрешен по умолчанию."""
    from memory import tools as mt

    # Подменяем policy запрашивающего агента
    monkeypatch.setattr(mt, "_get_requesting_agent_policy", lambda name: {
        "inter_agent_visibility": "readonly",
        "allowed_artifacts": ["sql_query"],  # general отсутствует
    })

    records = [
        {"agent_name": "a", "step": 1, "data": {"cache_kind": "agent_step", "artifact_type": "general", "agent_response": "x"}},
        {"agent_name": "a", "step": 2, "data": {"cache_kind": "agent_step", "artifact_type": "sql_query", "agent_response": "select 1"}},
        {"agent_name": "a", "step": 3, "data": {"cache_kind": "agent_step", "artifact_type": "summary", "agent_response": "s"}},
    ]

    out = mt._apply_policy_filters(records, requesting_agent="any_agent")
    types = {r["data"].get("artifact_type") for r in out}

    assert "general" in types
    assert "sql_query" in types
    assert "summary" not in types  # не разрешено профилем


def test_default_routing_excludes_schema_and_cache(monkeypatch):
    """Если cache_kind не задан, по умолчанию исключаем schema_table и vector_db_search."""
    from memory import tools as mt

    monkeypatch.setenv("RAG_DEFAULT_EXCLUDE_CACHE_KINDS", "schema_table,vector_db_search")

    records = [
        {"agent_name": "x", "step": 1, "data": {"cache_kind": "schema_table", "artifact_type": "schema_info"}},
        {"agent_name": "x", "step": 2, "data": {"cache_kind": "vector_db_search", "artifact_type": "cache"}},
        {"agent_name": "x", "step": 3, "data": {"cache_kind": "agent_step", "artifact_type": "general"}},
        {"agent_name": "x", "step": 4, "data": {"cache_kind": "agent_summary", "artifact_type": "summary"}},
    ]

    out = mt._apply_default_cache_kind_routing(records, cache_kind=None)
    out_kinds = [r["data"].get("cache_kind") for r in out]

    assert "schema_table" not in out_kinds
    assert "vector_db_search" not in out_kinds
    assert "agent_step" in out_kinds
    assert "agent_summary" in out_kinds


def test_explicit_cache_kind_disables_default_routing(monkeypatch):
    """Если cache_kind задан явно, дефолтные исключения не применяются."""
    from memory import tools as mt

    monkeypatch.setenv("RAG_DEFAULT_EXCLUDE_CACHE_KINDS", "schema_table,vector_db_search")

    records = [
        {"agent_name": "x", "step": 1, "data": {"cache_kind": "schema_table", "artifact_type": "schema_info"}},
    ]

    out = mt._apply_default_cache_kind_routing(records, cache_kind="schema_table")
    assert len(out) == 1


