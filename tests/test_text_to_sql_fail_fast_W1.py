"""Pin-тесты на fail-fast поведение Волны 1 (Группа B): запрет silent fallbacks.

Покрывают четыре места, где AGENTS.md ранее нарушался:

* B1: ``sql_postprocess.apply_dialect_quoting`` / ``apply_manual_quoting`` —
  падение AST-парсера теперь даёт ``SQLPostprocessError``, а не silent return.
* B2: ``schema_filtering._try_load_morphemes_index`` — отсутствующий yaml
  не превращается молча в substring-only фильтрацию.
* B3: ``schema_loader.SchemaFilter.filter_schema_by_include_list`` — ошибка
  обработки whitelist не возвращает полную схему, а поднимает исключение.
* B4: ``core._db_exec.secure_db_executor`` — без явного dsn и без opt-in env
  поднимается ``MissingDSNError`` вместо использования ``DB_DSN``.
* B5: ``core._sql_generation_api.sql_explain`` — без явного dsn и без opt-in env
  возвращает ``EXPLAIN_ERROR`` и не обращается к ``DB_DSN``.
"""
import pytest
import sqlglot  # noqa: F401

from custom_tools.text_to_sql.sql_postprocess import (  # noqa: E402
    SQLPostprocessError,
    apply_dialect_quoting,
    apply_manual_quoting,
)
from custom_tools.text_to_sql.schema_filtering import (  # noqa: E402
    MorphemesIndexUnavailable,
    _try_load_morphemes_index,
)
from custom_tools.text_to_sql.schema_loader import (  # noqa: E402
    SchemaFilter,
    SchemaIncludeFilterError,
)
from custom_tools.text_to_sql.core._db_exec import (  # noqa: E402
    MissingDSNError,
    secure_db_executor,
)


@pytest.fixture(autouse=True)
def _clear_relevant_envs(monkeypatch):
    for var in (
        "SQL_GENERATION_ALLOW_MANUAL_QUOTING_FALLBACK",
        "TEXT_TO_SQL_MORPHEMES_REQUIRED",
        "SCHEMA_INCLUDE_TABLES",
        "SECURE_DB_EXECUTOR_ALLOW_ENV_DSN",
        "DB_DSN",
        "TEXT_TO_SQL_DRY_RUN_ONLY",
        "USE_SQLGLOT",
        "SQL_POSTPROCESS_STRICT",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# B1. sql_postprocess: AST-парсер fail → SQLPostprocessError
# ---------------------------------------------------------------------------


_GARBAGE_SQL = "%%% NOT A SQL @@@ #####"


def test_b1_apply_dialect_quoting_raises_on_unparseable_default():
    """Default-ветка (USE_SQLGLOT=1, без opt-in fallback) → SQLPostprocessError."""
    with pytest.raises(SQLPostprocessError):
        apply_dialect_quoting(_GARBAGE_SQL, {"metrics": [], "dimensions": []})


def test_b1_apply_manual_quoting_raises_on_unparseable():
    """Manual quoting тоже fail-fast: silent return исходного SQL больше нельзя."""
    with pytest.raises(SQLPostprocessError):
        apply_manual_quoting(_GARBAGE_SQL, {"metrics": [], "dimensions": []})


def test_b1_apply_dialect_quoting_with_allow_manual_still_fails_fast(monkeypatch):
    """Opt-in fallback не делает silent: manual_quoting сам fail-fast'ит."""
    monkeypatch.setenv("SQL_GENERATION_ALLOW_MANUAL_QUOTING_FALLBACK", "1")
    with pytest.raises(SQLPostprocessError):
        apply_dialect_quoting(_GARBAGE_SQL, {"metrics": [], "dimensions": []})


def test_b1_apply_dialect_quoting_happy_path():
    """Корректный SQL не падает и возвращает строку с тем же SELECT.

    Подсовываем linked_entities с известными колонкой/таблицей: предикат
    ``should_quote_name`` ловит их по точному совпадению и не доходит до
    ``is_reserved_keyword``, чей путь в ansi-диалекте sqlglot 27.x
    непригоден без диалект-specific tokenizer.
    """
    sql = "SELECT id FROM users"
    linked = {
        "metrics": [{"table": "users", "column": "id"}],
        "dimensions": [],
    }
    result = apply_dialect_quoting(sql, linked)
    assert isinstance(result, str)
    assert "SELECT" in result.upper()
    assert "id" in result.lower()


# ---------------------------------------------------------------------------
# B2. schema_filtering._try_load_morphemes_index: required by default
# ---------------------------------------------------------------------------


def test_b2_morphemes_required_default_raises(monkeypatch):
    """Без yaml в default-режиме поднимается MorphemesIndexUnavailable."""
    from custom_tools.text_to_sql import schema_filtering as sf

    def _broken_loader():
        raise FileNotFoundError("nlu_morphemes.yaml missing in test sandbox")

    monkeypatch.setattr(
        "custom_tools.text_to_sql.nlu_config.load_nlu_morphemes",
        _broken_loader,
    )

    with pytest.raises(MorphemesIndexUnavailable):
        sf._try_load_morphemes_index()


def test_b2_morphemes_required_true_string_raises(monkeypatch):
    from custom_tools.text_to_sql import schema_filtering as sf

    monkeypatch.setenv("TEXT_TO_SQL_MORPHEMES_REQUIRED", "true")

    def _broken_loader():
        raise FileNotFoundError("nlu_morphemes.yaml missing in test sandbox")

    monkeypatch.setattr(
        "custom_tools.text_to_sql.nlu_config.load_nlu_morphemes",
        _broken_loader,
    )

    with pytest.raises(MorphemesIndexUnavailable):
        sf._try_load_morphemes_index()


def test_b2_morphemes_required_invalid_bool_fails_fast(monkeypatch):
    from custom_tools.text_to_sql import schema_filtering as sf

    monkeypatch.setenv("TEXT_TO_SQL_MORPHEMES_REQUIRED", "maybe")

    def _broken_loader():
        raise FileNotFoundError("nlu_morphemes.yaml missing")

    monkeypatch.setattr(
        "custom_tools.text_to_sql.nlu_config.load_nlu_morphemes",
        _broken_loader,
    )

    with pytest.raises(ValueError, match="TEXT_TO_SQL_MORPHEMES_REQUIRED"):
        sf._try_load_morphemes_index()


def test_b2_morphemes_disabled_falls_back_with_warning(monkeypatch, caplog):
    """Явный opt-out TEXT_TO_SQL_MORPHEMES_REQUIRED=0 возвращает None + warning."""
    from custom_tools.text_to_sql import schema_filtering as sf

    monkeypatch.setenv("TEXT_TO_SQL_MORPHEMES_REQUIRED", "0")

    def _broken_loader():
        raise FileNotFoundError("nlu_morphemes.yaml missing")

    monkeypatch.setattr(
        "custom_tools.text_to_sql.nlu_config.load_nlu_morphemes",
        _broken_loader,
    )

    caplog.set_level("WARNING", logger=sf.logger.name)
    result = sf._try_load_morphemes_index()
    assert result is None
    assert any("DEGRADED" in rec.message for rec in caplog.records), (
        "Expected explicit DEGRADED warning when opting out of fail-fast"
    )


def test_b2_morphemes_present_happy_path(monkeypatch):
    """Если yaml загружается и enabled — возвращается dict-индекс."""
    from custom_tools.text_to_sql import schema_filtering as sf

    class _StubCfg:
        enabled = True
        intents = [{"canonical": "Count", "morphemes": ["count", "Counter"]}]
        dimensions = [{"canonical": "Region", "morphemes": ["region"]}]

    monkeypatch.setattr(
        "custom_tools.text_to_sql.nlu_config.load_nlu_morphemes",
        lambda: _StubCfg(),
    )

    idx = sf._try_load_morphemes_index()
    assert isinstance(idx, dict)
    assert "count" in idx
    assert "region" in idx


# ---------------------------------------------------------------------------
# B3. SchemaFilter.filter_schema_by_include_list: silent return removed
# ---------------------------------------------------------------------------


def test_b3_filter_silent_fallback_removed(monkeypatch):
    """Если внутри возникает ошибка обработки whitelist, поднимается исключение
    вместо silent return полной схемы."""
    monkeypatch.setenv("SCHEMA_INCLUDE_TABLES", "users,orders")

    class _BadKey:
        def split(self, *_a, **_kw):
            raise RuntimeError("simulated string mishandling")

        def casefold(self):
            raise RuntimeError("simulated string mishandling")

    db_schema = {_BadKey(): {"columns": {}}}

    with pytest.raises(SchemaIncludeFilterError):
        SchemaFilter.filter_schema_by_include_list(db_schema)


def test_b3_filter_empty_env_returns_unchanged():
    """Пустая SCHEMA_INCLUDE_TABLES → схема возвращается без изменений."""
    db_schema = {"users": {"columns": {}}, "orders": {"columns": {}}}
    result = SchemaFilter.filter_schema_by_include_list(db_schema)
    assert result is db_schema or result == db_schema


def test_b3_filter_normal_case_insensitive_works(monkeypatch):
    """Whitelist отрабатывает по-прежнему case-insensitive."""
    monkeypatch.setenv("SCHEMA_INCLUDE_TABLES", "Users")
    db_schema = {
        "public.users": {"columns": {}},
        "public.orders": {"columns": {}},
    }
    result = SchemaFilter.filter_schema_by_include_list(db_schema)
    assert "public.users" in result
    assert "public.orders" not in result


# ---------------------------------------------------------------------------
# B4. secure_db_executor: MissingDSNError, без silent env-fallback
# ---------------------------------------------------------------------------


class _SafePassthrough:
    """Stub sql_validator: не используется в B4-сценариях (safety_check замокана)."""


class _StubConn:
    closed = False


class _StubPlugin:
    """Минимальный плагин: connect возвращает stub, execute_select даёт пустой результат."""

    def __init__(self, dsn_log):
        self.dsn_log = dsn_log

    def connect(self, dsn):
        self.dsn_log.append(dsn)
        return _StubConn()

    def close(self, conn):
        conn.closed = True

    def execute_select(self, conn, sql, *, row_limit):
        return {
            "success": True,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": 0,
            "error_message": None,
        }


def _install_safe_facade(monkeypatch, dsn_log):
    """Подменяет фасадные sql_safety_check и get_plugin на безопасные stubs.

    ``sql_safety_check`` принимает ``**kwargs`` чтобы быть совместимым с обоими
    сигнатурами вызывающего кода: legacy ``sql_safety_check(q)`` и текущий
    ``sql_safety_check(q, dsn=...)``.
    """
    from custom_tools.text_to_sql import core as core_facade

    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda q, **_kw: {"is_safe": True, "issues": []},
    )
    monkeypatch.setattr(
        core_facade,
        "get_plugin",
        lambda dsn: _StubPlugin(dsn_log),
    )


def test_b4_secure_db_executor_no_dsn_no_env_raises(monkeypatch):
    """Нет dsn-аргумента, нет DB_DSN env, нет opt-in → MissingDSNError."""
    dsn_log: list = []
    _install_safe_facade(monkeypatch, dsn_log)

    with pytest.raises(MissingDSNError):
        secure_db_executor(
            "SELECT 1",
            sql_validator=_SafePassthrough(),
            schema_limiter=None,
        )
    assert dsn_log == [], "Plugin не должен был быть получен без DSN"


def test_b4_secure_db_executor_no_dsn_with_env_default_raises(monkeypatch):
    """DB_DSN установлен, но opt-in выключен → всё равно MissingDSNError."""
    dsn_log: list = []
    _install_safe_facade(monkeypatch, dsn_log)
    monkeypatch.setenv("DB_DSN", "postgresql://user:pwd@host:5432/db")

    with pytest.raises(MissingDSNError):
        secure_db_executor(
            "SELECT 1",
            sql_validator=_SafePassthrough(),
            schema_limiter=None,
        )
    assert dsn_log == [], "Silent env-fallback на DB_DSN должен быть запрещён"


def test_b4_secure_db_executor_no_dsn_with_env_and_optin_warns_and_uses(
    monkeypatch, caplog
):
    """Opt-in SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1 разрешает env-fallback с warning."""
    from custom_tools.text_to_sql.core import _db_exec as db_exec_mod

    dsn_log: list = []
    _install_safe_facade(monkeypatch, dsn_log)
    monkeypatch.setenv("SECURE_DB_EXECUTOR_ALLOW_ENV_DSN", "1")
    monkeypatch.setenv("DB_DSN", "postgresql://user:pwd@host:5432/db")

    caplog.set_level("WARNING", logger=db_exec_mod.logger.name)
    result = secure_db_executor(
        "SELECT 1",
        sql_validator=_SafePassthrough(),
        schema_limiter=None,
    )
    assert isinstance(result, dict)
    assert result.get("success") is True
    assert dsn_log == ["postgresql://user:pwd@host:5432/db"]
    assert any("MISSING" in rec.message for rec in caplog.records), (
        "Ожидаем явный warning при opt-in env-fallback"
    )


def test_b4_secure_db_executor_explicit_dsn_wins_over_env(monkeypatch):
    """Явный dsn-параметр имеет приоритет над DB_DSN и не требует opt-in."""
    dsn_log: list = []
    _install_safe_facade(monkeypatch, dsn_log)
    monkeypatch.setenv("DB_DSN", "postgresql://env_user@host/env_db")

    result = secure_db_executor(
        "SELECT 1",
        dsn="postgresql://explicit@host/explicit_db",
        sql_validator=_SafePassthrough(),
        schema_limiter=None,
    )
    assert result.get("success") is True
    assert dsn_log == ["postgresql://explicit@host/explicit_db"]


def test_b4_secure_db_executor_dry_run_no_dsn_does_not_pass_none_to_safety(monkeypatch):
    """Dry-run без dsn не должен передавать None в dialect/safety layer."""
    from custom_tools.text_to_sql import core as core_facade

    dsn_log: list = []
    safety_calls: list = []
    _install_safe_facade(monkeypatch, dsn_log)
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda q, **kw: safety_calls.append((q, kw)) or {"is_safe": True, "issues": []},
    )
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "1")
    monkeypatch.setenv("DB_DSN", "postgresql://must:notbeused@host/db")

    result = secure_db_executor(
        "SELECT 1",
        sql_validator=_SafePassthrough(),
        schema_limiter=None,
    )

    assert result["dry_run_only"] is True
    assert dsn_log == []
    assert safety_calls == [("SELECT 1", {"dsn": ""})]


# ---------------------------------------------------------------------------
# B5. sql_explain: EXPLAIN_ERROR, без silent env-fallback
# ---------------------------------------------------------------------------


def test_b5_sql_explain_no_dsn_with_env_default_does_not_use_db(monkeypatch):
    """DB_DSN установлен, но opt-in выключен → plugin не вызывается."""
    from custom_tools.text_to_sql.core import _sql_generation_api as sql_gen_mod
    from custom_tools.text_to_sql import core as core_facade

    dsn_log: list = []
    safety_calls: list = []
    _install_safe_facade(monkeypatch, dsn_log)
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda q, **kw: safety_calls.append((q, kw)) or {"is_safe": True, "issues": []},
    )
    monkeypatch.setenv("DB_DSN", "postgresql://user:pwd@host:5432/db")

    result = sql_gen_mod.sql_explain("SELECT 1", sql_validator=_SafePassthrough())

    assert dsn_log == [], "Silent env-fallback на DB_DSN должен быть запрещён"
    assert safety_calls == [], "Без real DSN sql_explain не должен доходить до safety/LLM"
    issue_types = {issue.get("issue_type") for issue in result.get("issues", [])}
    assert "EXPLAIN_ERROR" in issue_types
    descriptions = " ".join(issue.get("description", "") for issue in result.get("issues", []))
    assert "DSN required" in descriptions


def test_b5_sql_explain_dry_run_no_dsn_does_not_pass_none_to_safety(monkeypatch):
    """Dry-run без dsn не должен передавать None в dialect/safety layer."""
    from custom_tools.text_to_sql.core import _sql_generation_api as sql_gen_mod
    from custom_tools.text_to_sql import core as core_facade

    dsn_log: list = []
    safety_calls: list = []
    _install_safe_facade(monkeypatch, dsn_log)
    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda q, **kw: safety_calls.append((q, kw)) or {"is_safe": True, "issues": []},
    )
    monkeypatch.setenv("TEXT_TO_SQL_DRY_RUN_ONLY", "1")
    monkeypatch.setenv("DB_DSN", "postgresql://must:notbeused@host/db")

    result = sql_gen_mod.sql_explain("SELECT 1", sql_validator=_SafePassthrough())

    assert result["dry_run_only"] is True
    assert dsn_log == []
    assert safety_calls == [("SELECT 1", {"dsn": ""})]
