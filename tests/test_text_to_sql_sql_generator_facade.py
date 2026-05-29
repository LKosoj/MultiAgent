"""EPIC 8.1: Contract-pinning тест для backward-compat shims SQLGenerator.

После декомпозиции `sql_generator.py` приватные методы должны остаться
доступными как instance/class методы — тесты дёргают их напрямую.
"""
from custom_tools.text_to_sql.sql_generator import SQLGenerator


def test_facade_exposes_structured_builder_shims():
    gen = SQLGenerator()
    assert callable(gen._generate_from_linked_entities)
    assert callable(gen._metric_aggregate)
    assert callable(gen._build_filter_clauses)
    assert callable(gen._filter_value_conditions)
    assert callable(gen._sql_literal)


def test_facade_exposes_postprocess_shims():
    gen = SQLGenerator()
    assert callable(gen._apply_dialect_quoting)
    assert callable(gen._apply_manual_quoting)
    assert callable(gen._quote_via_ast)
    # _should_quote_name доступен и как classmethod, и через instance.
    assert callable(SQLGenerator._should_quote_name)
    assert callable(SQLGenerator._is_reserved_keyword)


def test_facade_metric_aggregate_delegates_correctly():
    gen = SQLGenerator()
    assert gen._metric_aggregate({"aggregation": "count"}) == "COUNT"
    assert gen._metric_aggregate({"aggregation": "average"}) == "AVG"


def test_facade_filter_value_conditions_delegates_correctly():
    gen = SQLGenerator()
    info = {"operator": "="}
    assert gen._filter_value_conditions('"t"."c"', None, info) == ['"t"."c" IS NULL']


def test_facade_sql_literal_delegates_correctly():
    gen = SQLGenerator()
    assert gen._sql_literal(None) == "NULL"
    assert gen._sql_literal(42) == "42"


def test_facade_should_quote_name_classmethod_delegates():
    aliases = {"t1"}
    # Совпадение с alias — не квотируем.
    assert SQLGenerator._should_quote_name("t1", "ansi", set(), set(), aliases) is False
    # Известная колонка — квотируем.
    assert SQLGenerator._should_quote_name("amount", "ansi", set(), {"amount"}, set()) is True


def test_facade_preserves_call_openai_api_at_module_level():
    """tests/test_text_to_sql_core_contracts.py monkeypatch'ит этот атрибут.

    Если он переедет с module-level — тесты сломаются молча.
    """
    import custom_tools.text_to_sql.sql_generator as sg
    assert hasattr(sg, "call_openai_api")
    assert callable(sg.call_openai_api)
