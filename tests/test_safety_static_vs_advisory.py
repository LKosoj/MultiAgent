"""W9-A10: тесты разделения static_safety и llm_advisory.

Проверяют:

1. ``SQLStaticSafetyValidator.validate()`` НЕ зовёт LLM ни при каких
   условиях; возвращает ``layer="static"``.
2. ``SQLLLMAdvisor.audit()`` возвращает ``layer="llm_advisory"`` и
   ``blocking=False``; non-blocking даже при ошибке LLM.
3. Orchestrator ``sql_safety_check`` при static is_safe=False НЕ вызывает
   LLM (early-return, ``llm_audit="skipped_static_unsafe"``).
4. Orchestrator при static is_safe=True вызывает LLM (legacy-контракт).
"""
from __future__ import annotations

import os

os.environ.setdefault("USE_SQLGLOT", "1")

import pytest  # noqa: E402

from custom_tools.text_to_sql.validators import (  # noqa: E402
    SQLLLMAdvisor,
    SQLSafetyValidator,
    SQLStaticSafetyValidator,
)


# === 1. STATIC LAYER не зовёт LLM ============================================
def test_static_validator_alias_is_safety_validator() -> None:
    """W9-A10: SQLStaticSafetyValidator — это явный alias для SQLSafetyValidator."""
    assert SQLStaticSafetyValidator is SQLSafetyValidator


def test_static_validate_returns_layer_marker() -> None:
    """validate() должен помечать результат как 'static'."""
    v = SQLStaticSafetyValidator()
    result = v.validate("SELECT 1")
    assert result.get("layer") == "static"
    assert "is_safe" in result


def test_static_validate_accepts_simple_select() -> None:
    v = SQLStaticSafetyValidator()
    result = v.validate("SELECT 1")
    assert result["is_safe"] is True


def test_static_validate_rejects_drop_without_llm(monkeypatch) -> None:
    """Static слой ловит DROP БЕЗ обращения к LLM (важная инвариантa W9-A10).

    Подменяем call_openai_api на стenв, который сразу падает — если static
    случайно вызовет LLM, тест упадёт. После validate() утверждаем, что
    static вернул is_safe=False по статическим правилам.
    """
    from custom_tools.text_to_sql import core as core_facade

    calls = {"n": 0}

    def boom(**kwargs):
        calls["n"] += 1
        raise RuntimeError("static layer must NOT call LLM")

    monkeypatch.setattr(core_facade, "call_openai_api", boom)

    v = SQLStaticSafetyValidator()
    result = v.validate("DROP TABLE users")
    assert result["is_safe"] is False
    assert result.get("layer") == "static"
    assert calls["n"] == 0, "static validator must not invoke LLM"


def test_static_validate_independent_of_llm_unavailability(monkeypatch) -> None:
    """Static слой даёт ответ даже если call_openai_api недоступен."""
    from custom_tools.text_to_sql import core as core_facade

    monkeypatch.setattr(core_facade, "call_openai_api", None)
    v = SQLStaticSafetyValidator()
    # Любой валидный SELECT
    result = v.validate("SELECT count(*) FROM t")
    assert result["is_safe"] is True
    assert result.get("layer") == "static"


# === 2. LLM ADVISORY слой =====================================================
def test_llm_advisor_returns_layer_marker_and_blocking_false(monkeypatch) -> None:
    """audit() возвращает layer='llm_advisory', blocking=False (контракт)."""
    from custom_tools.text_to_sql import core as core_facade

    def ok(**kwargs):
        return '{"issues": []}'

    monkeypatch.setattr(core_facade, "call_openai_api", ok)
    from custom_tools.text_to_sql.core._sql_generation_api import _clear_llm_safety_cache

    _clear_llm_safety_cache()
    advisor = SQLLLMAdvisor()
    result = advisor.audit("SELECT 1 FROM advisor_test_a")
    assert result["layer"] == "llm_advisory"
    assert result["blocking"] is False
    assert result["status"] == "ok"
    assert result["advisory"] == []


def test_llm_advisor_collects_issues_from_llm(monkeypatch) -> None:
    """advisor извлекает issues из LLM-ответа."""
    from custom_tools.text_to_sql import core as core_facade
    from custom_tools.text_to_sql.core._sql_generation_api import _clear_llm_safety_cache

    _clear_llm_safety_cache()

    def with_issues(**kwargs):
        return (
            '{"issues": [{"issue_type": "PERF_WARNING",'
            ' "description": "Full table scan likely"}]}'
        )

    monkeypatch.setattr(core_facade, "call_openai_api", with_issues)
    advisor = SQLLLMAdvisor()
    result = advisor.audit("SELECT * FROM advisor_test_b")
    assert result["status"] == "ok"
    assert len(result["advisory"]) == 1
    assert result["advisory"][0]["issue_type"] == "PERF_WARNING"


def test_llm_advisor_non_blocking_on_runtime_failure(monkeypatch) -> None:
    """LLM-ошибка не пробрасывается; advisor возвращает status='failed', blocking=False."""
    from custom_tools.text_to_sql import core as core_facade
    from custom_tools.text_to_sql.core._sql_generation_api import _clear_llm_safety_cache

    _clear_llm_safety_cache()

    def failing(**kwargs):
        raise RuntimeError("LLM endpoint down")

    monkeypatch.setattr(core_facade, "call_openai_api", failing)
    advisor = SQLLLMAdvisor()
    result = advisor.audit("SELECT 1 FROM advisor_test_failing")
    assert result["status"] == "failed"
    assert result["blocking"] is False
    assert result["advisory"] == []
    assert "LLM endpoint down" in result["error"]


# === 3. Orchestrator: static False -> LLM не вызывается ======================
def test_orchestrator_skips_llm_when_static_unsafe(monkeypatch) -> None:
    """W9-A10: при static is_safe=False orchestrator НЕ вызывает LLM."""
    from custom_tools.text_to_sql.core._sql_generation_api import (
        _clear_llm_safety_cache,
        sql_safety_check,
    )
    from custom_tools.text_to_sql import core as core_facade

    _clear_llm_safety_cache()
    calls = {"n": 0}

    def fake_llm(**kwargs):
        calls["n"] += 1
        return '{"issues": []}'

    monkeypatch.setattr(core_facade, "call_openai_api", fake_llm)

    sql_validator = core_facade.sql_validator
    result = sql_safety_check("DROP TABLE users", sql_validator=sql_validator)

    assert result["is_safe"] is False
    assert result.get("llm_audit") == "skipped_static_unsafe"
    assert result.get("safety_status") == "unsafe"
    assert calls["n"] == 0, "orchestrator must NOT call LLM when static unsafe"


# === 4. Orchestrator: static True -> LLM вызывается (контракт сохранён) ======
def test_orchestrator_calls_llm_when_static_safe(monkeypatch) -> None:
    """Backward-compat: при static is_safe=True orchestrator зовёт LLM."""
    from custom_tools.text_to_sql.core._sql_generation_api import (
        _clear_llm_safety_cache,
        sql_safety_check,
    )
    from custom_tools.text_to_sql import core as core_facade

    _clear_llm_safety_cache()
    calls = {"n": 0}

    def fake_llm(**kwargs):
        calls["n"] += 1
        return '{"issues": []}'

    monkeypatch.setattr(core_facade, "call_openai_api", fake_llm)

    sql_validator = core_facade.sql_validator
    result = sql_safety_check(
        "SELECT 1 FROM orchestrator_safe_test", sql_validator=sql_validator
    )

    assert result["is_safe"] is True
    assert result["llm_audit"] == "ok"
    assert calls["n"] == 1, "orchestrator must call LLM when static safe"
