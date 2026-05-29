"""W8-T8: расширение forbidden_functions в extended/strict профилях.

Проверяет, что валидатор реально отвергает SELECT-запросы, использующие новые
запрещённые PostgreSQL admin / cross-DB и ClickHouse table-функции.

Два режима тестирования:
1. ``extended_validator`` — USE_SQLGLOT=0 + SQL_SAFETY_ALLOW_LEGACY=1: legacy
   regex-режим (исходные тесты W8-T8, покрывают legacy-путь).
2. ``extended_validator_sqlglot`` — USE_SQLGLOT=1: прод-режим через
   AST-проверку check_forbidden_functions_ast (фикс #1/#9). Существующие
   legacy-тесты НЕ удалены (они проверяют отдельный код-путь).

Также добавлены тесты для SELECT ... INTO (фикс #2): создание таблицы через
SELECT INTO запрещено в любом режиме с USE_SQLGLOT=1.
"""
from __future__ import annotations

import os

import pytest

from custom_tools.text_to_sql.validators import SQLSafetyValidator  # noqa: E402
from custom_tools.text_to_sql.validators import safety_config  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_safety_cache():
    safety_config.reset_cache()
    yield
    safety_config.reset_cache()


@pytest.fixture
def extended_validator(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "extended")
    # legacy regex-маршрут: устойчиво ловит forbidden_functions через
    # raw upper(sql) (см. docstring модуля выше).
    monkeypatch.setenv("USE_SQLGLOT", "0")
    monkeypatch.setenv("SQL_SAFETY_ALLOW_LEGACY", "1")
    safety_config.reset_cache()
    return SQLSafetyValidator()


@pytest.mark.parametrize(
    "sql, expected_fn",
    [
        # PostgreSQL admin / cross-DB
        ("SELECT pg_terminate_backend(123)", "pg_terminate_backend"),
        ("SELECT pg_cancel_backend(456)", "pg_cancel_backend"),
        ("SELECT * FROM dblink('host=other', 'SELECT 1')", "dblink"),
        ("SELECT dblink_exec('h', 'DROP TABLE x')", "dblink_exec"),
        ("SELECT dblink_send_query('h', 'SELECT 1')", "dblink_send_query"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT pg_read_binary_file('/etc/passwd')", "pg_read_binary_file"),
        ("SELECT pg_ls_dir('/etc')", "pg_ls_dir"),
        ("SELECT lo_import('/etc/passwd')", "lo_import"),
        ("SELECT lo_export(1, '/tmp/out')", "lo_export"),
        # ClickHouse table-функции
        ("SELECT * FROM file('/etc/passwd', 'CSV')", "file"),
        ("SELECT * FROM url('http://attacker/x', 'CSV')", "url"),
        ("SELECT * FROM s3('s3://bucket/x', 'CSV')", "s3"),
        ("SELECT * FROM hdfs('hdfs://x', 'CSV')", "hdfs"),
        ("SELECT * FROM mysql('h', 'db', 't', 'u', 'p')", "mysql"),
        ("SELECT * FROM postgresql('h', 'db', 't', 'u', 'p')", "postgresql"),
        ("SELECT * FROM remote('h', db.t)", "remote"),
        ("SELECT * FROM remoteSecure('h', db.t)", "remoteSecure"),
        ("SELECT * FROM cluster('c', db.t)", "cluster"),
        ("SELECT * FROM clusterAllReplicas('c', db.t)", "clusterAllReplicas"),
        ("SELECT system.shutdown()", "system.shutdown"),
        ("SELECT system.kill('q')", "system.kill"),
    ],
)
def test_extended_profile_rejects_new_forbidden_functions(extended_validator, sql, expected_fn):
    res = extended_validator.validate(sql)
    assert res["is_safe"] is False, f"sql={sql!r} should be rejected"
    issue_types = {i["issue_type"] for i in res["issues"]}
    assert "FORBIDDEN_FUNCTION" in issue_types, (
        f"FORBIDDEN_FUNCTION must be reported for {sql!r}, got {res['issues']}"
    )
    # Конкретная функция упомянута в description (защита от ложно-широкого совпадения).
    descriptions = " ".join(i.get("description", "") for i in res["issues"])
    assert expected_fn.lower() in descriptions.lower(), (
        f"Expected fn {expected_fn!r} mention, got {descriptions!r}"
    )


def test_strict_profile_has_same_extended_functions(monkeypatch):
    """`strict` — alias к `extended`. Новые функции должны быть и там."""
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "strict")
    monkeypatch.setenv("USE_SQLGLOT", "0")
    monkeypatch.setenv("SQL_SAFETY_ALLOW_LEGACY", "1")
    safety_config.reset_cache()
    v = SQLSafetyValidator()
    new_fns = {
        "pg_terminate_backend", "pg_cancel_backend", "dblink",
        "dblink_exec", "dblink_send_query", "pg_read_binary_file",
        "file", "url", "s3", "hdfs", "remote", "remoteSecure",
        "cluster", "clusterAllReplicas", "system.shutdown", "system.kill",
    }
    have = set(v.forbidden_functions)
    missing = new_fns - have
    assert not missing, f"strict profile missing: {missing}"


def test_default_profile_has_no_forbidden_functions(monkeypatch):
    """default остаётся пустым (контракт W3-T3): движково-специфики не должно быть."""
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    monkeypatch.setenv("USE_SQLGLOT", "0")
    monkeypatch.setenv("SQL_SAFETY_ALLOW_LEGACY", "1")
    safety_config.reset_cache()
    v = SQLSafetyValidator()
    assert list(v.forbidden_functions) == [], (
        "default profile must NOT contain engine-specific forbidden_functions"
    )


# ---------------------------------------------------------------------------
# Фикс #1/#9: тесты прод-режима (USE_SQLGLOT=1, AST-проверка)
# ---------------------------------------------------------------------------

@pytest.fixture
def extended_validator_sqlglot(monkeypatch):
    """Валидатор в прод-режиме: USE_SQLGLOT=1, профиль extended.

    Проверяет, что AST-проверка check_forbidden_functions_ast реально
    блокирует запрещённые функции при USE_SQLGLOT=1 (фикс #1/#9).
    """
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "extended")
    monkeypatch.setenv("USE_SQLGLOT", "1")
    monkeypatch.delenv("SQL_SAFETY_ALLOW_LEGACY", raising=False)
    safety_config.reset_cache()
    return SQLSafetyValidator()


@pytest.mark.parametrize(
    "sql, expected_fn",
    [
        # PostgreSQL admin / cross-DB
        ("SELECT pg_terminate_backend(123)", "pg_terminate_backend"),
        ("SELECT pg_cancel_backend(456)", "pg_cancel_backend"),
        ("SELECT * FROM dblink('host=other', 'SELECT 1')", "dblink"),
        ("SELECT dblink_exec('h', 'DROP TABLE x')", "dblink_exec"),
        ("SELECT dblink_send_query('h', 'SELECT 1')", "dblink_send_query"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT pg_read_binary_file('/etc/passwd')", "pg_read_binary_file"),
        ("SELECT pg_ls_dir('/etc')", "pg_ls_dir"),
        ("SELECT lo_import('/etc/passwd')", "lo_import"),
        ("SELECT lo_export(1, '/tmp/out')", "lo_export"),
        ("SELECT pg_sleep(10)", "pg_sleep"),
        ("SELECT load_file('/x')", "load_file"),
        # ClickHouse table-функции
        ("SELECT * FROM url('http://attacker/x', 'CSV')", "url"),
        ("SELECT * FROM s3('s3://bucket/x', 'CSV')", "s3"),
        ("SELECT * FROM hdfs('hdfs://x', 'CSV')", "hdfs"),
        ("SELECT * FROM mysql('h', 'db', 't', 'u', 'p')", "mysql"),
        ("SELECT * FROM postgresql('h', 'db', 't', 'u', 'p')", "postgresql"),
        ("SELECT * FROM remote('h', db.t)", "remote"),
        ("SELECT * FROM cluster('c', db.t)", "cluster"),
        # Qualified: system.shutdown, system.kill
        ("SELECT system.shutdown()", "system.shutdown"),
        ("SELECT system.kill('q')", "system.kill"),
        # information_schema — блокируется через Table.db
        ("SELECT * FROM information_schema.columns", "information_schema"),
        ("SELECT * FROM pg_catalog.pg_tables", "pg_catalog"),
        # Keyword-функции с выделенным sqlglot-классом (НЕ Anonymous):
        # current_user парсится как exp.CurrentUser и раньше обходил
        # check_forbidden_functions_ast (фикс step 3 / sql_names()).
        ("SELECT current_user", "current_user"),
        ("SELECT current_user()", "current_user"),
    ],
)
def test_extended_profile_rejects_forbidden_functions_sqlglot_mode(
    extended_validator_sqlglot, sql, expected_fn
):
    """Прод-режим USE_SQLGLOT=1: AST-проверка должна блокировать forbidden_functions."""
    res = extended_validator_sqlglot.validate(sql)
    assert res["is_safe"] is False, f"sql={sql!r} should be rejected in sqlglot mode"
    issue_types = {i["issue_type"] for i in res["issues"]}
    assert "FORBIDDEN_FUNCTION" in issue_types, (
        f"FORBIDDEN_FUNCTION must be reported for {sql!r}, got {res['issues']}"
    )
    descriptions = " ".join(i.get("description", "") for i in res["issues"])
    assert expected_fn.lower() in descriptions.lower(), (
        f"Expected fn {expected_fn!r} mention, got {descriptions!r}"
    )


def test_legitimate_select_passes_sqlglot_mode(extended_validator_sqlglot):
    """Легитимный SELECT не должен блокироваться extended-профилем при USE_SQLGLOT=1."""
    res = extended_validator_sqlglot.validate("SELECT id, name FROM users WHERE id = 1")
    assert res["is_safe"] is True, f"Legitimate query should pass, got {res['issues']}"


# ---------------------------------------------------------------------------
# Фикс #2: SELECT ... INTO (создание таблицы) блокируется при USE_SQLGLOT=1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * INTO newtable FROM users",
        "SELECT id, name INTO backup_users FROM users WHERE active = 1",
    ],
)
def test_select_into_rejected_sqlglot(extended_validator_sqlglot, sql):
    """SELECT ... INTO создаёт таблицу — должно блокироваться при USE_SQLGLOT=1."""
    res = extended_validator_sqlglot.validate(sql)
    assert res["is_safe"] is False, f"SELECT INTO should be rejected: {sql!r}"
    issue_types = {i["issue_type"] for i in res["issues"]}
    assert "FORBIDDEN_STATEMENT" in issue_types, (
        f"FORBIDDEN_STATEMENT expected for SELECT INTO, got {res['issues']}"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * INTO newtable FROM users",
        "SELECT id, name INTO backup_users FROM users WHERE active = 1",
    ],
)
def test_select_into_rejected_legacy(extended_validator, sql):
    """Фикс #2: SELECT ... INTO должен блокироваться и на legacy-пути
    (USE_SQLGLOT=0) — раньше is_valid_select_or_cte пропускал его как обычный
    SELECT, т.к. не проверял args['into']."""
    res = extended_validator.validate(sql)
    assert res["is_safe"] is False, f"SELECT INTO must be rejected (legacy): {sql!r}"


def test_keyword_function_classes_not_silently_bypassed(extended_validator_sqlglot):
    """W2 forward-guard: при апгрейде sqlglot некоторые Anonymous-функции могут
    получить выделенный класс (как current_user -> exp.CurrentUser) и обойти
    AST-проверку. Проверяем, что каждая forbidden-функция, которую sqlglot
    парсит как НЕ-Anonymous function-класс, всё равно ловится по sql_names()."""
    import sqlglot
    from sqlglot import exp

    forbidden = set(extended_validator_sqlglot.forbidden_functions)
    checked = 0
    for fn in sorted(forbidden):
        # Только однословные идентификаторы без точки (квалифицированные и
        # многословные формы покрываются другими путями: Table.db / keywords).
        if " " in fn or "." in fn:
            continue
        try:
            parsed = sqlglot.parse_one(f"SELECT {fn}")
        except Exception:
            continue
        func_nodes = [
            n for n in parsed.find_all(exp.Func)
            if not isinstance(n, exp.Anonymous)
        ]
        # Нас интересуют только те, что распарсились в выделенный (не Anonymous)
        # function-класс — именно они и есть риск bypass.
        if not func_nodes:
            continue
        res = extended_validator_sqlglot.validate(f"SELECT {fn}")
        assert res["is_safe"] is False, (
            f"forbidden keyword-function {fn!r} parsed as dedicated sqlglot "
            f"class but was NOT blocked — silent deny-list bypass"
        )
        checked += 1
    # Должны были проверить хотя бы current_user (иначе тест деградировал).
    assert checked >= 1, "forward-guard не проверил ни одной keyword-функции"
