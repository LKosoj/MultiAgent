"""EPIC 8.1: тесты `sql_postprocess` — диалект-специфичное квотирование."""
import pytest
import sqlglot

from custom_tools.text_to_sql import sql_postprocess


def test_should_quote_name_preserves_alias():
    aliases = {"t1"}
    assert sql_postprocess.should_quote_name("t1", "ansi", set(), set(), aliases) is False


def test_should_quote_name_known_column():
    assert sql_postprocess.should_quote_name(
        "amount", "ansi", set(), {"amount"}, set()
    ) is True


def test_should_quote_name_non_ascii():
    assert sql_postprocess.should_quote_name(
        "тип", "ansi", set(), set(), set()
    ) is True


def test_should_quote_name_postgres_mixed_case():
    assert sql_postprocess.should_quote_name(
        "Amount", "postgres", set(), set(), set()
    ) is True


def test_should_quote_name_safe_ascii_lowercase_not_quoted():
    # Reserved keyword check может всё ещё триггерить, но "foo_bar_unique" — точно нет.
    # Используем postgres как реальный sqlglot-диалект (ansi — внутренняя метка).
    assert sql_postprocess.should_quote_name(
        "foo_bar_unique", "postgres", set(), set(), set()
    ) is False


def test_is_reserved_keyword_select():
    # `SELECT` — keyword во всех sqlglot-диалектах.
    assert sql_postprocess.is_reserved_keyword("SELECT", "postgres") is True


def test_is_reserved_keyword_unknown():
    assert sql_postprocess.is_reserved_keyword("foo_unknown_word", "postgres") is False


def test_apply_dialect_quoting_multi_statement_fail_fast(monkeypatch):
    """multi-statement → RuntimeError (silent corruption не допускается)."""
    # USE_SQLGLOT=1 чтобы выбрать AST-путь, а не legacy manual.
    monkeypatch.setenv("USE_SQLGLOT", "1")
    sql = "SELECT 1; SELECT 2"
    with pytest.raises(RuntimeError):
        sql_postprocess.apply_dialect_quoting(sql, {})


def test_apply_dialect_quoting_null_ast_fail_fast(monkeypatch):
    """sqlglot.parse вернул [None] → fail-fast, не исходный SQL."""
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setattr(sqlglot, "parse", lambda *_args, **_kwargs: [None])

    with pytest.raises(RuntimeError, match="empty AST"):
        sql_postprocess.apply_dialect_quoting("SELECT 1", {})


def test_apply_dialect_quoting_quotes_known_columns(monkeypatch):
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.setenv("TEXT_TO_SQL_SQL_DIALECT", "ansi")
    linked = {
        "metrics": [{"table": "orders", "column": "amount"}],
        "dimensions": [],
    }
    sql = "SELECT amount FROM orders"
    result = sql_postprocess.apply_dialect_quoting(sql, linked)
    # ANSI диалект — двойные кавычки на known column.
    assert '"amount"' in result
    assert '"orders"' in result


def test_apply_manual_quoting_unparseable_raises(monkeypatch):
    """Парсер AST упал → SQLPostprocessError.

    Изначально контракт допускал silent return исходного SQL (best-effort),
    но это нарушало AGENTS.md «no silent fallbacks»: caller считал результат
    уже квотированным. Теперь manual quoting тоже fail-fast — поведение
    зафиксировано в ``tests/test_text_to_sql_fail_fast_W1.py`` (B1).
    """
    monkeypatch.setenv("USE_SQLGLOT", "0")
    bad_sql = "@@@ not really sql @@@"
    with pytest.raises(sql_postprocess.SQLPostprocessError):
        sql_postprocess.apply_manual_quoting(bad_sql, {})
