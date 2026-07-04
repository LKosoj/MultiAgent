"""M-24: невалидный ответ black-screen классификатора не валит сцену.

- Ответ без поля is_black_screen -> мягкий fallback False + предупреждение,
  результат НЕ кэшируется (следующий вызов пробует заново).
- Валидный ответ кэшируется (повторный вызов без обращения к LLM).
"""

from __future__ import annotations

import custom_tools.storybook.screenplay_shots_generator_utils.shared_utils as su


def test_invalid_json_returns_false_and_is_not_cached(monkeypatch):
    calls = {"n": 0}

    def fake_api(**kwargs):
        calls["n"] += 1
        return '{"unexpected": true}'  # нет поля is_black_screen

    monkeypatch.setattr(su, "call_openai_api", fake_api)
    su._BLACK_SCREEN_DETECTION_CACHE.clear()

    r1 = su.black_screen_storyboard_shot("MEDIUM SHOT", {"must_show": ["door"]})
    r2 = su.black_screen_storyboard_shot("MEDIUM SHOT", {"must_show": ["door"]})

    assert r1 is False
    assert r2 is False
    # Фолбэк не кэшируется -> LLM вызван оба раза.
    assert calls["n"] == 2


def test_valid_response_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake_api(**kwargs):
        calls["n"] += 1
        return '{"is_black_screen": true}'

    monkeypatch.setattr(su, "call_openai_api", fake_api)
    su._BLACK_SCREEN_DETECTION_CACHE.clear()

    r1 = su.black_screen_storyboard_shot("BLACKOUT", {})
    r2 = su.black_screen_storyboard_shot("BLACKOUT", {})

    assert r1 is True
    assert r2 is True
    # Успешный результат кэшируется -> LLM вызван один раз.
    assert calls["n"] == 1
