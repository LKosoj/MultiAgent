"""W8-T5: hot-reload safety-конфига.

Проверяет, что:

* ``reload_safety_config()`` сбрасывает кэш модульного загрузчика и
  следующий ``load_safety_profile()`` подхватывает новый env-профиль
  без рестарта процесса.

* ``SQLSafetyValidator.reload()`` пере-создаёт internal helpers и
  валидация после reload использует уже свежий профиль (видно по
  ``max_query_length`` / ``max_in_list_size``, у default и extended
  значения различаются: 10000/1000 vs 4000/200).
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("USE_SQLGLOT", "1")

from custom_tools.text_to_sql.validators import SQLSafetyValidator  # noqa: E402
from custom_tools.text_to_sql.validators import safety_config  # noqa: E402
from custom_tools.text_to_sql.validators.safety_config import (  # noqa: E402
    load_safety_profile,
    reload_safety_config,
)
from custom_tools.text_to_sql.core import _sql_generation_api  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_config_cache():
    safety_config.reset_cache()
    yield
    safety_config.reset_cache()


def test_reload_safety_config_picks_up_new_env_profile(monkeypatch):
    """Меняем env-профиль → reload_safety_config() → новый профиль активен."""
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    safety_config.reset_cache()

    default_profile = load_safety_profile()
    assert default_profile.profile_name == "default"
    assert default_profile.max_query_length == 10000

    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "extended")
    # БЕЗ reload — кеш до сих пор отдаёт default.
    cached = load_safety_profile()
    assert cached.profile_name == "extended", (
        "load_safety_profile должен учитывать env_profile в cache-ключе"
    )
    # Тем не менее reload_safety_config() — explicit API — должен явно
    # сбрасывать кеш, и повторная загрузка обязана выдавать профиль
    # extended даже после ручного reset env↔reset.
    reload_safety_config()
    reloaded = load_safety_profile()
    assert reloaded.profile_name == "extended"
    assert reloaded.max_query_length == 4000


def test_reload_safety_config_clears_llm_safety_cache(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    _sql_generation_api._clear_llm_safety_cache()
    _sql_generation_api._llm_safety_cache_put(
        "cache-key",
        {"is_safe": True, "issues": [], "llm_audit": "ok"},
    )

    assert _sql_generation_api._llm_safety_cache_get("cache-key") is not None

    reload_safety_config()

    assert _sql_generation_api._llm_safety_cache_get("cache-key") is None


def test_llm_safety_cache_key_includes_active_safety_profile(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    _sql_generation_api._clear_llm_safety_cache()
    calls: list[str | None] = []

    class _ProfileAwareValidator:
        def validate(self, sql_query, dsn=None):
            active_profile = os.getenv("TEXT_TO_SQL_SAFETY_PROFILE")
            calls.append(active_profile)
            if active_profile == "extended":
                return {
                    "is_safe": False,
                    "issues": [{"issue_type": "STRICT_PROFILE_BLOCK", "description": "blocked"}],
                }
            return {"is_safe": True, "issues": []}

    monkeypatch.setattr(
        _sql_generation_api,
        "_run_llm_safety_audit_with_timeout",
        lambda sql_query, dsn=None: {"is_safe": True, "issues": []},
    )

    first = _sql_generation_api.sql_safety_check(
        "SELECT 1",
        sql_validator=_ProfileAwareValidator(),
    )
    assert first["is_safe"] is True

    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "extended")
    second = _sql_generation_api.sql_safety_check(
        "SELECT 1",
        sql_validator=_ProfileAwareValidator(),
    )

    assert second["is_safe"] is False
    assert calls == [None, "extended"]


def test_llm_safety_cache_does_not_skip_static_validation(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    _sql_generation_api._clear_llm_safety_cache()
    calls = 0

    class _FlippingValidator:
        def validate(self, sql_query, dsn=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"is_safe": True, "issues": []}
            return {
                "is_safe": False,
                "issues": [{"issue_type": "STATIC_DENY", "description": "blocked"}],
            }

    monkeypatch.setattr(
        _sql_generation_api,
        "_run_llm_safety_audit_with_timeout",
        lambda sql_query, dsn=None: {"is_safe": True, "issues": []},
    )

    validator = _FlippingValidator()
    first = _sql_generation_api.sql_safety_check("SELECT 1", sql_validator=validator)
    second = _sql_generation_api.sql_safety_check("SELECT 1", sql_validator=validator)

    assert first["is_safe"] is True
    assert first["llm_audit"] == "ok"
    assert second["is_safe"] is False
    assert second["llm_audit"] == "skipped_static_unsafe"
    assert calls == 2


def test_sql_safety_validator_reload_rebuilds_helpers(monkeypatch):
    """SQLSafetyValidator.reload() должен пере-создать _RegexValidator/_SqlglotValidator."""
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    safety_config.reset_cache()

    validator = SQLSafetyValidator()
    assert validator.max_query_length == 10000
    assert validator.max_in_list_size == 1000
    old_regex = validator._regex
    old_sqlglot = validator._sqlglot

    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "extended")
    validator.reload()

    assert validator.max_query_length == 4000
    assert validator.max_in_list_size == 200
    # Helpers должны быть пере-созданы (это и есть точка интеграции с reload).
    assert validator._regex is not old_regex
    assert validator._sqlglot is not old_sqlglot


def test_sql_safety_validator_reload_visible_in_validate(monkeypatch):
    """После reload validate() видит новые лимиты — практический smoke."""
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_PROFILE", raising=False)
    safety_config.reset_cache()

    validator = SQLSafetyValidator()

    # Запрос длиной 5000 — в default (10000) проходит по длине, в extended (4000) — нет.
    long_select = "SELECT " + ", ".join(f"c{i}" for i in range(700)) + " FROM t"
    assert len(long_select) > 4000
    assert len(long_select) < 10000

    res_default = validator.validate(long_select)
    # Лимит длины 10000 → ошибки по длине быть не должно.
    assert not any(
        issue.get("issue_type") == "QUERY_TOO_LARGE"
        for issue in res_default.get("issues", [])
    ), res_default

    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_PROFILE", "extended")
    validator.reload()

    res_extended = validator.validate(long_select)
    assert any(
        issue.get("issue_type") == "QUERY_TOO_LARGE"
        for issue in res_extended.get("issues", [])
    ), (
        "после reload в extended-профиле запрос > 4000 символов должен "
        f"триггерить QUERY_TOO_LARGE: {res_extended}"
    )
