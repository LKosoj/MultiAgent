"""EPIC 7 (pii + api blocks) — unit-тесты для переработанного поведения.

Покрывает:
- 7.15: ``PII_MASK_SALT`` env-соль и SHA-256 хэширование;
- 7.16: DI ``call_openai_api`` через keyword-only kwarg в ``_pii.pii_masking``;
- 7.17: расширенный return-контракт ``pii_masking`` (``pii_detected``,
  ``masked_columns``, ``reason``) и duplicate-column-name безопасность;
- 7.18: ``session_id`` проброс в ``NLUProcessor.process_text/extract_intent``;
- 7.19: валидация ``top_k > 0`` в ``vector_db_search``;
- 7.20: ``Optional[str]`` сигнатуры в фасадных функциях;
- 7.28: ``is_sqlglot_enabled`` вычисляется один раз в ``code_formatter``;
- 7.29: ``__all__`` фасадного package.
"""
import inspect
import json
from typing import Optional

import pytest

from custom_tools.text_to_sql import core as core_module
from custom_tools.text_to_sql.core import _pii as pii_module
from custom_tools.text_to_sql.core._pii import pii_masking as pii_masking_impl
from custom_tools.text_to_sql.core._rag_api import vector_db_search as rag_search_impl


# ---------------------------------------------------------------------------
# 7.15 — PII_MASK_SALT
# ---------------------------------------------------------------------------

def _setup_env_for_real_masking(monkeypatch, *, salt: Optional[str]):
    """Стандартная подготовка env для тестов реального маскирования."""
    monkeypatch.setenv("PII_MASKING_ENABLED", "1")
    if salt is None:
        monkeypatch.delenv("PII_MASK_SALT", raising=False)
    else:
        monkeypatch.setenv("PII_MASK_SALT", salt)


def test_pii_mask_uses_salt(monkeypatch):
    """Хэш одного и того же значения зависит от соли."""
    data = [["alice@example.com"]]
    column_names = ["email"]

    _setup_env_for_real_masking(monkeypatch, salt="salt-A")
    result_a = pii_masking_impl(data, ["email"], column_names=column_names)

    _setup_env_for_real_masking(monkeypatch, salt="salt-B")
    result_b = pii_masking_impl(data, ["email"], column_names=column_names)

    masked_a = result_a["masked_data"][0][0]
    masked_b = result_b["masked_data"][0][0]
    assert masked_a.startswith("***")
    assert masked_b.startswith("***")
    assert masked_a != masked_b, "Salt change must alter hash"


def test_pii_mask_missing_salt_raises(monkeypatch):
    """PII_MASK_SALT unset → fail-fast при реальном маскировании."""
    _setup_env_for_real_masking(monkeypatch, salt=None)

    with pytest.raises(RuntimeError, match="PII_MASK_SALT"):
        pii_masking_impl(
            [["x@y.com"]],
            ["email"],
            column_names=["email"],
        )


def test_pii_mask_empty_salt_raises(monkeypatch):
    """PII_MASK_SALT="" / "   " тоже fail-fast (review CRITICAL #1)."""
    for bad_salt in ("", "   ", "\t\n"):
        _setup_env_for_real_masking(monkeypatch, salt=bad_salt)
        with pytest.raises(RuntimeError, match="PII_MASK_SALT"):
            pii_masking_impl(
                [["x@y.com"]],
                ["email"],
                column_names=["email"],
            )


def test_pii_mask_disabled_does_not_require_salt(monkeypatch):
    """PII_MASKING_ENABLED=0 короткозамыкает раньше проверки соли."""
    monkeypatch.setenv("PII_MASKING_ENABLED", "0")
    monkeypatch.delenv("PII_MASK_SALT", raising=False)

    # Не должно бросать.
    result = pii_masking_impl([["x@y.com"]], ["email"], column_names=["email"])
    assert result["masked_data"] == [["x@y.com"]]
    assert result["pii_detected"] is False
    assert result["reason"] == "masking_disabled"


def test_pii_mask_uses_sha256_not_md5(monkeypatch):
    """Маска должна быть SHA-256-производной (стабильна по соли + значению)."""
    import hashlib

    _setup_env_for_real_masking(monkeypatch, salt="known-salt")
    result = pii_masking_impl(
        [["foo@bar.com"]],
        ["email"],
        column_names=["email"],
    )
    masked = result["masked_data"][0][0]
    expected = "***" + hashlib.sha256(b"known-salt:foo@bar.com").hexdigest()[:8]
    assert masked == expected


# ---------------------------------------------------------------------------
# 7.16 — DI call_openai_api в pii_masking
# ---------------------------------------------------------------------------

def test_pii_masking_uses_injected_llm(monkeypatch):
    """call_openai_api инъектируется через kwarg; facade при этом не дёргается."""
    _setup_env_for_real_masking(monkeypatch, salt="t")
    # Делаем facade.call_openai_api заведомо «бросающим», чтобы убедиться,
    # что injected llm используется первым.
    def boom(**_):
        raise AssertionError("facade call_openai_api must not be invoked when injected")

    monkeypatch.setattr(core_module, "call_openai_api", boom)

    called = {"n": 0}

    def fake_llm(**_):
        called["n"] += 1
        return json.dumps({"columns": ["email"]})

    result = pii_masking_impl(
        [["a@b.com", "x"]],
        ["AUTO"],
        column_names=["email", "name"],
        call_openai_api=fake_llm,
    )

    assert called["n"] == 1
    assert result["pii_detected"] is True
    assert result["masked_columns"] == ["email"]


def test_pii_masking_auto_without_llm_raises(monkeypatch):
    """AUTO + неинъектированный LLM + facade.call_openai_api=None → RuntimeError."""
    _setup_env_for_real_masking(monkeypatch, salt="t")
    monkeypatch.setattr(core_module, "call_openai_api", None)

    with pytest.raises(RuntimeError, match="call_openai_api is unavailable"):
        pii_masking_impl(
            [["a@b.com"]],
            ["AUTO"],
            column_names=["email"],
        )


def test_pii_masking_facade_passes_llm_from_module(monkeypatch):
    """Фасадный wrapper core.pii_masking прокидывает текущий core.call_openai_api."""
    _setup_env_for_real_masking(monkeypatch, salt="t")

    captured = {"prompt": None}

    def fake_llm(**kwargs):
        captured["prompt"] = kwargs.get("prompt")
        return json.dumps({"columns": ["email"]})

    monkeypatch.setattr(core_module, "call_openai_api", fake_llm)

    result = core_module.pii_masking(
        [["a@b.com", "Alice"]],
        ["AUTO"],
        column_names=["email", "name"],
    )

    assert captured["prompt"] is not None, "Facade must forward monkeypatched llm"
    assert result["pii_detected"] is True


# ---------------------------------------------------------------------------
# 7.17 — расширенный return-контракт + duplicate columns
# ---------------------------------------------------------------------------

def test_pii_duplicate_column_names_all_masked(monkeypatch):
    """Все вхождения duplicate column в column_names должны быть замаскированы."""
    _setup_env_for_real_masking(monkeypatch, salt="t")

    data = [["row1-a", "row1-b@x.com", "row1-c", "row1-d@y.com"]]
    column_names = ["id", "email", "name", "email"]  # email встречается дважды

    result = pii_masking_impl(data, ["email"], column_names=column_names)

    row = result["masked_data"][0]
    # Индексы 1 и 3 — оба "email" — должны быть замаскированы.
    assert row[1].startswith("***"), f"Index 1 not masked: {row[1]!r}"
    assert row[3].startswith("***"), f"Index 3 not masked: {row[3]!r}"
    # Индексы 0 и 2 — не PII — должны остаться.
    assert row[0] == "row1-a"
    assert row[2] == "row1-c"
    assert result["pii_detected"] is True
    assert result["masked_columns"] == ["email"]


def test_pii_no_detection_returns_reason(monkeypatch):
    """AUTO + LLM вернул пустой список → reason=auto_detected_none."""
    _setup_env_for_real_masking(monkeypatch, salt="t")

    result = pii_masking_impl(
        [["x", "y"]],
        ["AUTO"],
        column_names=["a", "b"],
        call_openai_api=lambda **_: json.dumps({"columns": []}),
    )

    assert result["pii_detected"] is False
    assert result["reason"] == "auto_detected_none"
    assert result["masked_columns"] == []
    # Данные не изменены.
    assert result["masked_data"] == [["x", "y"]]


def test_pii_requires_column_names_when_columns_specified(monkeypatch):
    """Непустой columns_to_mask + column_names=None → RuntimeError (no silent skip)."""
    _setup_env_for_real_masking(monkeypatch, salt="t")

    with pytest.raises(RuntimeError, match="column_names is missing"):
        pii_masking_impl(
            [["alice@x.com"]],
            ["email"],
            column_names=None,
        )


def test_pii_success_marks_detected(monkeypatch):
    """Реальное маскирование → pii_detected=True, masked_columns заполнен."""
    _setup_env_for_real_masking(monkeypatch, salt="t")

    result = pii_masking_impl(
        [["a@b.com", "Alice"]],
        ["email"],
        column_names=["email", "name"],
    )

    assert result["pii_detected"] is True
    assert result["masked_columns"] == ["email"]
    assert result["reason"] is None


def test_pii_empty_data_returns_reason(monkeypatch):
    """Пустой data → reason=empty_data, pii_detected=False."""
    _setup_env_for_real_masking(monkeypatch, salt="t")

    result = pii_masking_impl([], ["email"], column_names=["email"])
    assert result["pii_detected"] is False
    assert result["reason"] == "empty_data"


def test_pii_empty_columns_to_mask_returns_reason(monkeypatch):
    """Пустой columns_to_mask → reason=no_columns_to_mask."""
    _setup_env_for_real_masking(monkeypatch, salt="t")

    result = pii_masking_impl([["x"]], [], column_names=["a"])
    assert result["pii_detected"] is False
    assert result["reason"] == "no_columns_to_mask"


# ---------------------------------------------------------------------------
# 7.18 — session_id проброс
# ---------------------------------------------------------------------------

def test_nlu_natural_language_processing_threads_session_id(monkeypatch):
    """natural_language_processing передаёт session_id в nlu_processor.process_text."""
    received = {}

    class FakeProcessor:
        def process_text(self, text, session_id=None):
            received["text"] = text
            received["session_id"] = session_id
            return {"tokens": [], "pos_tags": []}

        def extract_intent(self, text, session_id=None):
            received["text"] = text
            received["session_id"] = session_id
            return {"intent": "query", "entities": {}}

    monkeypatch.setattr(core_module, "nlu_processor", FakeProcessor())

    core_module.natural_language_processing("какой-то текст", session_id="sid-123")
    assert received["session_id"] == "sid-123"


def test_nlu_intent_extraction_threads_session_id(monkeypatch):
    """intent_extraction передаёт session_id в nlu_processor.extract_intent."""
    received = {}

    class FakeProcessor:
        def process_text(self, text, session_id=None):
            return {"tokens": [], "pos_tags": []}

        def extract_intent(self, text, session_id=None):
            received["text"] = text
            received["session_id"] = session_id
            return {"intent": "query", "entities": {}}

    monkeypatch.setattr(core_module, "nlu_processor", FakeProcessor())

    core_module.intent_extraction("текст", session_id="abc")
    assert received["session_id"] == "abc"


def test_nlu_session_id_optional_none_when_omitted(monkeypatch):
    """Если session_id не передан, в process_text приходит None — без фабрикации."""
    received = {}

    class FakeProcessor:
        def process_text(self, text, session_id=None):
            received["session_id"] = session_id
            return {"tokens": [], "pos_tags": []}

        def extract_intent(self, text, session_id=None):
            received["session_id"] = session_id
            return {"intent": "query", "entities": {}}

    monkeypatch.setattr(core_module, "nlu_processor", FakeProcessor())

    core_module.natural_language_processing("текст")
    assert received["session_id"] is None


# ---------------------------------------------------------------------------
# 7.19 — top_k validation
# ---------------------------------------------------------------------------

class _StubRagSearcher:
    def __init__(self, result=None):
        self.result = result if result is not None else []
        self.calls = []

    def search_examples_by_query(self, query, top_k):
        self.calls.append((query, top_k))
        return self.result


def test_vector_db_search_accepts_positive_int():
    """top_k=1 — допустимый позитивный int."""
    rag = _StubRagSearcher(result=[{"id": 1}])
    result = rag_search_impl("q", 1, rag_searcher=rag)
    assert rag.calls == [("q", 1)]
    assert result == [{"id": 1}]


def test_vector_db_search_rejects_zero_top_k():
    rag = _StubRagSearcher()
    with pytest.raises(ValueError, match="positive int"):
        rag_search_impl("q", 0, rag_searcher=rag)


def test_vector_db_search_rejects_negative_top_k():
    rag = _StubRagSearcher()
    with pytest.raises(ValueError, match="positive int"):
        rag_search_impl("q", -3, rag_searcher=rag)


def test_vector_db_search_rejects_non_int():
    rag = _StubRagSearcher()
    with pytest.raises(ValueError, match="positive int"):
        rag_search_impl("q", 2.5, rag_searcher=rag)


def test_vector_db_search_rejects_bool_top_k():
    """bool — subclass int в Python; явно отсекаем, чтобы True/False не «прошли»."""
    rag = _StubRagSearcher()
    with pytest.raises(ValueError, match="positive int"):
        rag_search_impl("q", True, rag_searcher=rag)


# ---------------------------------------------------------------------------
# 7.20 — Optional[str] сигнатуры
# ---------------------------------------------------------------------------

def test_facade_uses_optional_str_annotations():
    """Сигнатуры фасадных функций должны использовать Optional[...] вместо str=None."""
    sig_nlp = inspect.signature(core_module.natural_language_processing)
    assert sig_nlp.parameters["session_id"].annotation == Optional[str]

    sig_intent = inspect.signature(core_module.intent_extraction)
    assert sig_intent.parameters["session_id"].annotation == Optional[str]

    sig_sl = inspect.signature(core_module.schema_linking)
    assert sig_sl.parameters["session_id"].annotation == Optional[str]
    assert sig_sl.parameters["schema_info"].annotation == Optional[dict]

    sig_pii = inspect.signature(core_module.pii_masking)
    assert sig_pii.parameters["column_names"].annotation == Optional[list]

    sig_exec = inspect.signature(core_module.secure_db_executor)
    assert sig_exec.parameters["row_limit"].annotation == Optional[int]


# ---------------------------------------------------------------------------
# 7.28 — единичное чтение is_sqlglot_enabled() в code_formatter
# ---------------------------------------------------------------------------

def test_code_formatter_evaluates_sqlglot_enabled_once(monkeypatch):
    """В пределах одного вызова code_formatter не должно быть >1 чтения env-флага.

    Это страхует от регрессии: try-блок читал значение однажды, except —
    повторно, что могло приводить к рассинхрону при monkeypatch внутри
    обработчика исключений.
    """
    import custom_tools.text_to_sql.core._sql_generation_api as sga
    from custom_tools.text_to_sql import dialects

    counter = {"n": 0}
    original = dialects.is_sqlglot_enabled

    def counting():
        counter["n"] += 1
        return True

    monkeypatch.setattr(sga, "is_sqlglot_enabled", counting, raising=False)
    # У импорта `from ..dialects import is_sqlglot_enabled` внутри функции
    # будет своя ссылка, поэтому подменяем источник.
    monkeypatch.setattr(dialects, "is_sqlglot_enabled", counting)

    # Парсинг должен пройти штатно (валидный SELECT).
    core_module.code_formatter("SELECT 1")

    assert counter["n"] == 1, (
        f"is_sqlglot_enabled must be evaluated exactly once per call; "
        f"got {counter['n']}"
    )


def test_code_formatter_consistent_when_env_flips_in_except(monkeypatch):
    """Даже если sqlglot.parse падает, решение основано на cached значении флага."""
    from custom_tools.text_to_sql import dialects
    import sqlglot

    # Стартуем с включённым sqlglot — решение должно остаться "вернуть error",
    # даже если внутри except что-то «переключит» env. Имитируем эту ситуацию
    # тем, что после первого чтения env меняется на "0", но логика обязана
    # использовать cached значение.
    state = {"reads": 0}

    def flaky_enabled():
        state["reads"] += 1
        # Первый вызов = True (вход в try); если бы код перечитывал env в
        # except, второй вызов вернул бы False и логика свалилась бы в legacy.
        return state["reads"] == 1

    monkeypatch.setattr(dialects, "is_sqlglot_enabled", flaky_enabled)

    def boom(*a, **kw):
        raise RuntimeError("simulated parse error")

    monkeypatch.setattr(sqlglot, "parse", boom)

    result = core_module.code_formatter("SELECT 1")

    # W2-T7: при ошибке sqlglot в enabled-режиме возвращается только 'error'
    # без 'formatted_sql_query' — SQL-тело не эхоируется в результат.
    assert "error" in result
    assert "formatted_sql_query" not in result, (
        "W2-T7: formatted_sql_query не должен присутствовать в error-ветке sqlglot"
    )
    assert state["reads"] == 1, "is_sqlglot_enabled must be read only once"


# ---------------------------------------------------------------------------
# 7.29 — __all__
# ---------------------------------------------------------------------------

def test_core_all_contains_public_functions():
    """Все 13 публичных функций должны быть в core.__all__."""
    public_functions = [
        "natural_language_processing",
        "intent_extraction",
        "vector_db_search",
        "schema_linking",
        "sql_generation_plugin",
        "code_formatter",
        "sql_safety_check",
        "sql_explain",
        "secure_db_executor",
        "pii_masking",
        "audit_logger",
        "save_successful_sql",
        "purge_schema_linking_rag_cache",
    ]
    assert hasattr(core_module, "__all__")
    for name in public_functions:
        assert name in core_module.__all__, f"{name} missing from core.__all__"


def test_core_all_contains_singletons():
    """Singletons должны быть в core.__all__."""
    for name in ("nlu_processor", "rag_searcher", "sql_validator", "schema_limiter"):
        assert name in core_module.__all__, f"{name} missing from core.__all__"


def test_core_all_contains_monkeypatch_hooks():
    """Module-level зависимости (для monkeypatch) должны быть в __all__."""
    for name in ("call_openai_api", "get_plugin", "memory_manager"):
        assert name in core_module.__all__, f"{name} missing from core.__all__"


def test_core_all_excludes_privates():
    """Приватные `_*` имена не должны попасть в __all__."""
    for name in core_module.__all__:
        assert not name.startswith("_"), f"Private name leaked into __all__: {name}"
