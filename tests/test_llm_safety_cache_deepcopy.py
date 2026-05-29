"""W8-T11: унификация clone в LLM safety cache через _clone_cached_response (deepcopy).

Контракт:
  * Мутация возвращённого из cache dict НЕ мутирует cache-запись.
  * _clone_cached_response — единый helper, используемый и при put, и при get.
  * Глубокий копир: мутация вложенных list/dict тоже изолирована.
"""
from __future__ import annotations

import pytest

from custom_tools.text_to_sql.core._sql_generation_api import (
    _clear_llm_safety_cache,
    _clone_cached_response,
    _llm_safety_cache_get,
    _llm_safety_cache_put,
)


@pytest.fixture(autouse=True)
def _isolate_safety_cache():
    """Чистим module-level кеш между тестами."""
    _clear_llm_safety_cache()
    yield
    _clear_llm_safety_cache()


def test_clone_returns_independent_top_level_dict():
    """Мутация верхнего уровня не затрагивает оригинал."""
    src = {"is_safe": True, "safety_status": "safe"}
    cloned = _clone_cached_response(src)
    cloned["is_safe"] = False
    assert src["is_safe"] is True


def test_clone_returns_independent_nested_list():
    """W8-T11: deepcopy — мутация вложенного list тоже изолирована."""
    src = {"is_safe": True, "issues": [{"issue_type": "X", "description": "y"}]}
    cloned = _clone_cached_response(src)
    cloned["issues"].append({"issue_type": "INJECTED", "description": "evil"})
    cloned["issues"][0]["issue_type"] = "MUTATED"
    assert len(src["issues"]) == 1
    assert src["issues"][0]["issue_type"] == "X"


def test_cache_hit_mutation_does_not_affect_stored_value():
    """W8-T11: cache hit возвращает клон → caller-мутация не отравляет cache."""
    key = "k1"
    payload = {
        "is_safe": True,
        "safety_status": "safe",
        "issues": [{"issue_type": "X", "description": "y"}],
        "llm_audit": "ok",
    }
    _llm_safety_cache_put(key, payload)

    first_hit = _llm_safety_cache_get(key)
    assert first_hit is not None
    # Мутируем результат как caller.
    first_hit["is_safe"] = False
    first_hit["issues"].append({"issue_type": "TAMPERED", "description": "x"})

    # Повторный hit должен вернуть НЕМУТИРОВАННЫЙ payload.
    second_hit = _llm_safety_cache_get(key)
    assert second_hit is not None
    assert second_hit["is_safe"] is True
    assert len(second_hit["issues"]) == 1
    assert second_hit["issues"][0]["issue_type"] == "X"


def test_cache_put_isolates_input_mutation():
    """W8-T11: put клонирует payload → последующая мутация входа не отравляет cache."""
    key = "k2"
    payload = {
        "is_safe": True,
        "safety_status": "safe",
        "issues": [{"issue_type": "X", "description": "y"}],
    }
    _llm_safety_cache_put(key, payload)
    # Мутируем payload ПОСЛЕ put.
    payload["is_safe"] = False
    payload["issues"].append({"issue_type": "TAMPERED", "description": "x"})

    hit = _llm_safety_cache_get(key)
    assert hit is not None
    assert hit["is_safe"] is True
    assert len(hit["issues"]) == 1
    assert hit["issues"][0]["issue_type"] == "X"


def test_clone_preserves_non_json_native_types():
    """W8-T11: deepcopy (в отличие от json round-trip) сохраняет non-JSON-native типы.

    Это важно потому, что safety_result теоретически может содержать tuple
    или set, которые json.loads(json.dumps(...)) превращает в list. deepcopy
    сохраняет точный тип.
    """
    src = {"is_safe": True, "tuple_field": (1, 2, 3), "set_field": {"a", "b"}}
    cloned = _clone_cached_response(src)
    assert isinstance(cloned["tuple_field"], tuple)
    assert isinstance(cloned["set_field"], set)
