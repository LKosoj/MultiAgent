"""EPIC 1.3: fail-fast при multi-statement парсинге в slq_generator.

_apply_dialect_quoting и _apply_manual_quoting должны бросать RuntimeError,
если sqlglot.parse вернул >1 statement — silent corruption недопустим.
end-to-end: generate_sql должен вернуть error-структуру.
"""
from unittest.mock import patch

import pytest

from custom_tools.text_to_sql.sql_generator import SQLGenerator


def _linked():
    return {
        "metrics": [{"name": "total", "table": "orders", "column": "amount"}],
        "dimensions": [],
    }


def test_apply_dialect_quoting_fails_fast_on_multi_statement():
    gen = SQLGenerator()

    class _Stmt:
        def sql(self, dialect=None, identify=False):
            return "SELECT 1"

    with patch("sqlglot.parse", return_value=[_Stmt(), _Stmt()]):
        with pytest.raises(RuntimeError, match="multiple statements"):
            gen._apply_dialect_quoting("SELECT 1; SELECT 2", _linked())


def test_apply_manual_quoting_fails_fast_on_multi_statement(monkeypatch):
    """_apply_manual_quoting тоже должна fail-fast — silent corruption недопустим."""
    monkeypatch.setenv("USE_SQLGLOT", "0")  # отключаем dialect-путь
    gen = SQLGenerator()

    class _Stmt:
        def find_all(self, *_args, **_kwargs):
            return []

        def sql(self, dialect=None):
            return "SELECT 1"

    with patch("sqlglot.parse", return_value=[_Stmt(), _Stmt()]):
        with pytest.raises(RuntimeError, match="multiple statements"):
            gen._apply_manual_quoting("SELECT 1; SELECT 2", _linked())


def test_generate_sql_returns_error_when_multi_statement_detected(monkeypatch):
    """End-to-end: generate_sql должен вернуть error при multi-statement."""
    monkeypatch.setenv("TEXT_TO_SQL_VALIDATE_SCHEMA", "0")
    monkeypatch.setenv("SQL_GENERATION_USE_STRUCTURED_BUILDER", "0")

    gen = SQLGenerator()
    gen.max_retries = 1

    class _Stmt:
        def sql(self, dialect=None, identify=False):
            return "SELECT 1"

    with patch.object(
        gen,
        "_llm_generation_direct",
        return_value={"sql_query": "SELECT 1; SELECT 2"},
    ), patch("sqlglot.parse", return_value=[_Stmt(), _Stmt()]):
        # Подсунем минимальный structured_context с linked_entities,
        # чтобы _apply_dialect_quoting был вызван.
        context = '{"linked_entities": {"metrics": [{"name":"t","table":"orders","column":"amount"}]}}'
        result = gen.generate_sql(context, "stub")

    assert "error" in result
    assert "multiple statements" in result["error"]
