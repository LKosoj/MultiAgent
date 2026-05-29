"""EPIC 7 (sql_generation_api / utils) — тесты для:
- 7.3: mask_dsn helper и применение в sql_explain;
- 7.4: LLM safety timeout + TTL-кеш;
- 7.27: parse_with_timeout (length-cap + timeout).
"""
import os
import time

import pytest

from custom_tools.text_to_sql import core as core_module
from custom_tools.text_to_sql.core import sql_explain
from custom_tools.text_to_sql.core._sql_generation_api import (
    _clear_llm_safety_cache,
    _get_llm_safety_timeout_s,
    _LLM_SAFETY_CACHE,
    sql_safety_check,
)
from custom_tools.text_to_sql.utils import (
    mask_dsn,
    mask_dsn_value,
    parse_with_timeout,
)


# === 7.3: mask_dsn ===


def test_mask_dsn_strips_credentials_from_dsn_substring():
    s = "could not connect to postgresql://alice:s3cret@db.example.com:5432/mydb"
    masked = mask_dsn(s)
    assert "alice" not in masked
    assert "s3cret" not in masked
    assert "postgresql://***:***@db.example.com:5432/mydb" in masked


def test_mask_dsn_handles_dsn_without_password():
    masked = mask_dsn("postgresql://alice@host/db")
    assert "alice" not in masked
    assert "postgresql://***:***@host/db" in masked


def test_mask_dsn_value_idempotent_on_already_masked():
    assert mask_dsn_value("postgresql://***:***@host/db") == "postgresql://***:***@host/db"


def test_mask_dsn_empty_input_safe():
    assert mask_dsn("") == ""
    assert mask_dsn(None) == ""


def test_mask_dsn_substitutes_env_dsn_literal(monkeypatch):
    monkeypatch.setenv("DB_DSN", "postgresql://u:p@host/db")
    err = "driver could not connect using postgresql://u:p@host/db (timeout)"
    masked = mask_dsn(err)
    assert "u:p" not in masked
    assert "postgresql://***:***@host/db" in masked


def test_sql_explain_dsn_credentials_not_leaked(monkeypatch):
    """sql_explain ловит исключение и должен маскировать DSN-креды (EPIC 7.3)."""
    dsn = "postgresql://alice:s3cret@db.example.com/mydb"
    monkeypatch.setenv("DB_DSN", dsn)
    monkeypatch.setattr(
        core_module, "call_openai_api", lambda **kw: '{"issues": []}'
    )

    class _BoomPlugin:
        def connect(self, dsn):
            raise RuntimeError(f"refused: {dsn}")

        def close(self, conn):
            pass

    monkeypatch.setattr(core_module, "get_plugin", lambda dsn: _BoomPlugin())

    result = sql_explain("SELECT 1", dsn=dsn)
    issues_text = " ".join(str(i.get("description", "")) for i in result.get("issues", []))
    assert "s3cret" not in issues_text
    assert "alice" not in issues_text


# === 7.4: LLM safety timeout + TTL-кеш ===


def test_sql_safety_check_caches_successful_audit(monkeypatch):
    """Повторный вызов с тем же SQL не зовёт call_openai_api повторно."""
    _clear_llm_safety_cache()
    monkeypatch.setenv("TEXT_TO_SQL_LLM_SAFETY_TTL_S", "300")
    calls = {"n": 0}

    def fake(**kwargs):
        calls["n"] += 1
        return '{"issues": []}'

    monkeypatch.setattr(core_module, "call_openai_api", fake)
    sql_validator = core_module.sql_validator

    r1 = sql_safety_check("SELECT 1 FROM dual_cache_test_a", sql_validator=sql_validator)
    r2 = sql_safety_check("SELECT 1 FROM dual_cache_test_a", sql_validator=sql_validator)
    assert r1["llm_audit"] == "ok"
    assert r2["llm_audit"] == "ok"
    assert calls["n"] == 1


def test_sql_safety_check_does_not_cache_failure(monkeypatch):
    """Сбой LLM не кешируется, чтобы повторный успешный вызов не залип в unsafe."""
    _clear_llm_safety_cache()
    monkeypatch.setenv("TEXT_TO_SQL_LLM_SAFETY_TTL_S", "300")
    state = {"mode": "fail"}

    def fake(**kwargs):
        if state["mode"] == "fail":
            raise RuntimeError("boom")
        return '{"issues": []}'

    monkeypatch.setattr(core_module, "call_openai_api", fake)
    sql_validator = core_module.sql_validator

    r1 = sql_safety_check("SELECT 1 FROM dual_cache_test_fail", sql_validator=sql_validator)
    assert r1["llm_audit"] == "failed"
    state["mode"] = "ok"
    r2 = sql_safety_check("SELECT 1 FROM dual_cache_test_fail", sql_validator=sql_validator)
    assert r2["llm_audit"] == "ok"


def test_sql_safety_check_cache_ttl_zero_disables_cache(monkeypatch):
    _clear_llm_safety_cache()
    monkeypatch.setenv("TEXT_TO_SQL_LLM_SAFETY_TTL_S", "0")
    calls = {"n": 0}

    def fake(**kwargs):
        calls["n"] += 1
        return '{"issues": []}'

    monkeypatch.setattr(core_module, "call_openai_api", fake)
    sql_validator = core_module.sql_validator

    sql_safety_check("SELECT 1 FROM dual_cache_test_b", sql_validator=sql_validator)
    sql_safety_check("SELECT 1 FROM dual_cache_test_b", sql_validator=sql_validator)
    assert calls["n"] == 2


def test_sql_safety_check_timeout_marks_unsafe(monkeypatch):
    """Таймаут на LLM-аудите → is_safe=False, issue_type=LLM_AUDIT_TIMEOUT."""
    _clear_llm_safety_cache()
    monkeypatch.setenv("TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_S", "0.3")

    def slow(**kwargs):
        time.sleep(2.0)
        return '{"issues": []}'

    monkeypatch.setattr(core_module, "call_openai_api", slow)
    sql_validator = core_module.sql_validator

    result = sql_safety_check("SELECT 7 FROM dual_timeout_test", sql_validator=sql_validator)
    assert result["is_safe"] is False
    assert result["llm_audit"] == "timeout"
    issue_types = {i.get("issue_type") for i in result.get("issues", [])}
    assert "LLM_AUDIT_TIMEOUT" in issue_types


def test_sql_safety_check_timeout_not_cached(monkeypatch):
    _clear_llm_safety_cache()
    monkeypatch.setenv("TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_S", "0.3")

    state = {"slow": True}

    def maybe_slow(**kwargs):
        if state["slow"]:
            time.sleep(2.0)
        return '{"issues": []}'

    monkeypatch.setattr(core_module, "call_openai_api", maybe_slow)
    sql_validator = core_module.sql_validator

    sql_safety_check("SELECT 7 FROM dual_timeout_recovery", sql_validator=sql_validator)
    state["slow"] = False
    monkeypatch.setenv("TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_S", "30")
    result = sql_safety_check("SELECT 7 FROM dual_timeout_recovery", sql_validator=sql_validator)
    assert result["llm_audit"] == "ok"


# === 7.27: parse_with_timeout ===


def test_parse_with_timeout_rejects_too_long_sql(monkeypatch):
    monkeypatch.setenv("TEXT_TO_SQL_MAX_SQL_LENGTH", "100")
    huge = "SELECT " + ", ".join([f"col_{i}" for i in range(1000)]) + " FROM t"
    with pytest.raises(ValueError, match="SQL exceeds max length"):
        parse_with_timeout(huge)


def test_parse_with_timeout_parses_valid_sql():
    statements = parse_with_timeout("SELECT 1 AS x")
    assert len(statements) == 1


def test_parse_with_timeout_times_out_when_parse_hangs(monkeypatch):
    """Если sqlglot.parse зависает, parse_with_timeout должен бросить TimeoutError."""
    import sqlglot

    def fake_parse(sql, read=None):
        time.sleep(5)
        return []

    monkeypatch.setattr(sqlglot, "parse", fake_parse)
    with pytest.raises(TimeoutError):
        parse_with_timeout("SELECT 1", timeout_s=0.2)


# === #10 MEDIUM: _format_sql_legacy сохраняет содержимое строковых литералов ===


def test_format_sql_legacy_preserves_string_literal_content():
    """Содержимое строковых литералов не должно претерпевать uppercase или newline-замены."""
    from custom_tools.text_to_sql.core._sql_generation_api import _format_sql_legacy

    sql = "SELECT 'order by name' AS x"
    result = _format_sql_legacy(sql)
    formatted = result["formatted_sql_query"]
    # Литерал 'order by name' не должен превратиться в 'ORDER BY name' или содержать \n
    assert "'order by name'" in formatted, (
        f"Строковый литерал изменён форматтером: {formatted!r}"
    )


def test_format_sql_legacy_preserves_multiple_literals():
    """Несколько строковых литералов в запросе — все должны быть сохранены."""
    from custom_tools.text_to_sql.core._sql_generation_api import _format_sql_legacy

    sql = "SELECT 'order by name' AS x WHERE a = 'b and c'"
    result = _format_sql_legacy(sql)
    formatted = result["formatted_sql_query"]
    assert "'order by name'" in formatted, f"Первый литерал изменён: {formatted!r}"
    assert "'b and c'" in formatted, f"Второй литерал изменён: {formatted!r}"


def test_format_sql_legacy_uppercases_keywords_outside_literals():
    """Ключевые слова вне литералов должны переводиться в верхний регистр."""
    from custom_tools.text_to_sql.core._sql_generation_api import _format_sql_legacy

    sql = "select id from users where active = 1"
    result = _format_sql_legacy(sql)
    formatted = result["formatted_sql_query"]
    assert "SELECT" in formatted
    assert "FROM" in formatted
    assert "WHERE" in formatted


def test_format_sql_legacy_preserves_escaped_quote_in_literal():
    """Одинарные кавычки ('' escape) внутри литерала сохраняются корректно."""
    from custom_tools.text_to_sql.core._sql_generation_api import _format_sql_legacy

    sql = "SELECT 'it''s a select' AS col"
    result = _format_sql_legacy(sql)
    formatted = result["formatted_sql_query"]
    # Литерал с escaped quote должен остаться нетронутым
    assert "'it''s a select'" in formatted, f"Escaped-quote литерал изменён: {formatted!r}"


# === W2-T7: code_formatter error-ветка sqlglot не эхоирует SQL ===


def test_code_formatter_sqlglot_error_returns_error_without_sql_body(monkeypatch):
    """W2-T7: при ошибке sqlglot в enabled-режиме — только 'error', без 'formatted_sql_query'."""
    from custom_tools.text_to_sql import core as core_module
    import sqlglot

    monkeypatch.setattr("custom_tools.text_to_sql.dialects.is_sqlglot_enabled", lambda: True)

    def boom(*a, **kw):
        raise RuntimeError("parse failed")

    monkeypatch.setattr(sqlglot, "parse", boom)

    result = core_module.code_formatter("SELECT 1")

    assert "error" in result, "Ожидается ключ 'error' при ошибке sqlglot"
    assert "formatted_sql_query" not in result, (
        "W2-T7: SQL-тело не должно эхоироваться в formatted_sql_query при ошибке sqlglot"
    )
