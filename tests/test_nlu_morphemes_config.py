"""Тесты для T4.1: морфемы NLU грузятся из yaml-конфига, в .py хардкода нет."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from custom_tools.text_to_sql import nlu, nlu_config
from custom_tools.text_to_sql.nlu import NLUProcessor


REPO_ROOT = Path(__file__).resolve().parents[1]
NLU_PY = REPO_ROOT / "custom_tools" / "text_to_sql" / "nlu.py"


@pytest.fixture(autouse=True)
def _reset_morphemes_cache():
    nlu_config.reset_cache()
    yield
    nlu_config.reset_cache()


def _write_minimal_config(path: Path, *, intent_morpheme: str = "xyzmetric") -> None:
    path.write_text(
        f"""
version: 1
language: ru
enabled: true
intents:
  - canonical: revenue
    morphemes: ["{intent_morpheme}"]
dimensions: []
relative_date:
  triggers: []
  periods: []
  days_pattern: '(\\d+)\\s*(?:dd)'
patterns:
  date_iso: []
  region: []
  amount_greater: []
  amount_less: []
  amount_between: []
  top_n: []
order:
  triggers: []
  desc_triggers: []
intent_rules: []
default_intent: query
top_n_intent: top_n
tokenizer:
  adpositions: []
""",
        encoding="utf-8",
    )


def test_nlu_loads_morphemes_from_yaml(tmp_path, monkeypatch):
    """Подмена пути к yaml через env должна полностью переопределять словарь."""
    cfg_path = tmp_path / "nlu_morphemes.yaml"
    _write_minimal_config(cfg_path, intent_morpheme="xyzmetric")

    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(cfg_path))
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    result = processor.extract_intent("custom text with xyzmetric inside")

    assert result["intent"] == "query"
    assert result["entities"]["metrics"] == ["revenue"]
    assert result["entities"]["dimensions"] == []
    assert result["entities"]["filters"] == {}


def test_nlu_fails_fast_when_yaml_missing(tmp_path, monkeypatch):
    """Если конфиг не найден, fallback обязан падать FileNotFoundError."""
    missing_path = tmp_path / "does_not_exist.yaml"
    assert not missing_path.exists()

    monkeypatch.setenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", str(missing_path))
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    with pytest.raises(FileNotFoundError, match="NLU morphemes config not found"):
        processor.extract_intent("любой текст")


def test_nlu_no_russian_morphemes_in_python_code():
    """Closed-world эвристика не должна возвращаться в .py: проверяем grep-инвариант."""
    content = NLU_PY.read_text(encoding="utf-8")
    forbidden = re.compile(r"выруч|сумм|доход|регион(?:е|а)?|месяц|неделя|год|больше|меньше|между|топ|первы[еих]|агрегир|сравни")
    matches = forbidden.findall(content)
    assert matches == [], (
        f"Найдены вшитые морфемы в custom_tools/text_to_sql/nlu.py: {matches!r}. "
        "Все морфемы и regex должны жить только в config/text_to_sql/nlu_morphemes.yaml (см. T4.1)."
    )


def test_muni_ru_profile_preserves_legacy_revenue_extraction(monkeypatch):
    """Регресс: профиль muni_ru хранит ту же морфему 'выруч', что и старый код.

    W3-T1: RU-морфемы переехали в ``profiles.muni_ru``; default-профиль
    стал нейтральным. Поэтому regression-кейс активирует muni_ru явно
    через env ``TEXT_TO_SQL_NLU_PROFILE``.
    """
    monkeypatch.delenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", raising=False)
    monkeypatch.setenv("TEXT_TO_SQL_NLU_PROFILE", "muni_ru")
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    result = processor.extract_intent("покажи выручку по регионам")

    assert result["entities"]["metrics"] == ["revenue"]
    assert "region" in result["entities"]["dimensions"]


def test_muni_ru_profile_preserves_top_n_and_order(monkeypatch):
    """Регресс: regex-паттерны top_n / order переехали в profiles.muni_ru
    без потери поведения (W3-T1)."""
    monkeypatch.delenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", raising=False)
    monkeypatch.setenv("TEXT_TO_SQL_NLU_PROFILE", "muni_ru")
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    result = processor.extract_intent("топ 5 клиентов по убыванию")

    assert result["entities"]["filters"].get("limit") == 5
    assert result["entities"]["filters"].get("order") == "desc"
    assert result["intent"] == "top_n"


def test_default_profile_blocks_ru_fallback(monkeypatch):
    """W3-T1: без активации muni_ru дефолтный profile-aware yaml держит
    fallback закрытым — non-RU инсталляция не получает RU интентов."""
    import pytest

    monkeypatch.delenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", raising=False)
    monkeypatch.delenv("TEXT_TO_SQL_NLU_PROFILE", raising=False)
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)

    processor = NLUProcessor()
    with pytest.raises(RuntimeError, match="disabled by config"):
        processor.extract_intent("покажи выручку по регионам")


# ---------------------------------------------------------------------------
# T10-nlu: тесты для исправлений багфиксов корректности
# ---------------------------------------------------------------------------

@pytest.fixture
def _muni_ru_fallback_processor(monkeypatch):
    """Процессор с профилем muni_ru, fallback включён, без LLM."""
    monkeypatch.delenv("TEXT_TO_SQL_NLU_MORPHEMES_PATH", raising=False)
    monkeypatch.setenv("TEXT_TO_SQL_NLU_PROFILE", "muni_ru")
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "1")
    monkeypatch.setattr(nlu, "call_openai_api", None)
    return NLUProcessor()


def test_relative_date_month_with_count(_muni_ru_fallback_processor):
    """T10-nlu #25: 'за последние 3 месяца' → period=month, count=3 (не 1)."""
    result = _muni_ru_fallback_processor.extract_intent("за последние 3 месяца")
    rd = result["entities"]["filters"].get("relative_date")
    assert rd is not None, "relative_date filter должен быть установлен"
    assert rd["period"] == "month"
    assert rd["count"] == 3


def test_relative_date_week_with_count(_muni_ru_fallback_processor):
    """T10-nlu #25: 'за последние 2 недели' → period=week, count=2."""
    result = _muni_ru_fallback_processor.extract_intent("за последние 2 недели")
    rd = result["entities"]["filters"].get("relative_date")
    assert rd is not None, "relative_date filter должен быть установлен"
    assert rd["period"] == "week"
    assert rd["count"] == 2


def test_relative_date_day_with_count(_muni_ru_fallback_processor):
    """T10-nlu #25 регресс: 'за последние 7 дней' → period=day, count=7."""
    result = _muni_ru_fallback_processor.extract_intent("за последние 7 дней")
    rd = result["entities"]["filters"].get("relative_date")
    assert rd is not None, "relative_date filter должен быть установлен"
    assert rd["period"] == "day"
    assert rd["count"] == 7


def test_relative_date_year_no_count(_muni_ru_fallback_processor):
    """T10-nlu: 'за последний год' без числа → period=year, count=1."""
    result = _muni_ru_fallback_processor.extract_intent("за последний год")
    rd = result["entities"]["filters"].get("relative_date")
    assert rd is not None, "relative_date filter должен быть установлен"
    assert rd["period"] == "year"
    assert rd["count"] == 1


def test_extract_intent_empty_string_no_llm(monkeypatch):
    """T10-nlu: extract_intent('') возвращает дефолтный ответ без LLM-вызова."""
    called = []

    def fake_llm(*args, **kwargs):
        called.append(True)
        return "{}"

    monkeypatch.setattr(nlu, "call_openai_api", fake_llm)

    processor = NLUProcessor()
    result = processor.extract_intent("")
    assert called == [], "LLM не должен вызываться для пустого ввода"
    assert result == {"intent": "query", "entities": {"metrics": [], "dimensions": [], "filters": {}}}


def test_extract_intent_whitespace_string_no_llm(monkeypatch):
    """T10-nlu: extract_intent('   ') возвращает дефолтный ответ без LLM-вызова."""
    called = []

    def fake_llm(*args, **kwargs):
        called.append(True)
        return "{}"

    monkeypatch.setattr(nlu, "call_openai_api", fake_llm)

    processor = NLUProcessor()
    result = processor.extract_intent("   ")
    assert called == [], "LLM не должен вызываться для пробельного ввода"
    assert result == {"intent": "query", "entities": {"metrics": [], "dimensions": [], "filters": {}}}


def test_process_text_empty_string_no_llm(monkeypatch):
    """T10-nlu: process_text('') возвращает пустые списки без LLM-вызова."""
    called = []

    def fake_llm(*args, **kwargs):
        called.append(True)
        return "{}"

    monkeypatch.setattr(nlu, "call_openai_api", fake_llm)

    processor = NLUProcessor()
    result = processor.process_text("")
    assert called == [], "LLM не должен вызываться для пустого ввода"
    assert result == {"tokens": [], "pos_tags": []}


def test_relative_date_not_set_without_period_unit(_muni_ru_fallback_processor):
    """T10-nlu регресс: запрос с триггером 'последн*' без явной единицы периода
    НЕ должен устанавливать relative_date.

    Ранее 'дн' в morphemes[day] давало ложное срабатывание: 'последние' содержит
    подстроку 'дн' ('по-с-л-е-д-н-и-е'). Убрали 'дн' из periods[day].
    """
    for query in [
        "показать последние данные",
        "последние 10 записей",
        "последний отчёт",
    ]:
        result = _muni_ru_fallback_processor.extract_intent(query)
        rd = result["entities"]["filters"].get("relative_date")
        assert rd is None, (
            f"relative_date не должен устанавливаться без явной единицы периода: {query!r}, "
            f"получено: {rd!r}"
        )


def test_extract_intent_llm_entities_as_string_raises_valueerror(monkeypatch):
    """T10-nlu: если LLM вернёт entities как строку — ValueError с ясным сообщением."""
    import json

    bad_response = json.dumps({"intent": "query", "entities": "bad_string"})

    monkeypatch.setattr(nlu, "call_openai_api", lambda **kw: bad_response)
    # Отключаем fallback, чтобы ValueError прорастал наружу.
    monkeypatch.setenv("TEXT_TO_SQL_NLU_ALLOW_FALLBACKS", "0")

    processor = NLUProcessor()
    with pytest.raises(RuntimeError, match="unavailable or returned invalid data"):
        processor.extract_intent("покажи выручку")
