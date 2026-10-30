import json

import pytest

from custom_tools.storybook.screenplay_shots_generator_utils import shared_utils as su


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(su, '_BLACK_SCREEN_DETECTION_CACHE', {})


@pytest.mark.parametrize('field', ['visible_characters', 'start_state_spec', 'end_state_spec'])
def test_visible_character_prevents_black_screen_even_after_cached_true(monkeypatch, field):
    calls = []
    monkeypatch.setattr(su, 'call_openai_api', lambda **kwargs: calls.append(kwargs) or
                        json.dumps({'is_black_screen': True}))
    spec = {'camera_anchor': 'A transition', 'must_show': []}
    assert su.black_screen_storyboard_shot('DISSOLVE', spec) is True
    if field == 'visible_characters':
        spec[field] = ['The traveller']
    else:
        spec[field] = {'visible_characters': ['The traveller']}
    assert su.black_screen_storyboard_shot('DISSOLVE', spec) is False
    assert len(calls) == 1


@pytest.mark.parametrize('value', ['false', 'true', 0, 1, None])
def test_non_boolean_classification_is_not_black_or_cached(monkeypatch, value):
    calls = []
    def response(**kwargs):
        calls.append(kwargs)
        return json.dumps({'is_black_screen': value if len(calls) == 1 else True})
    monkeypatch.setattr(su, 'call_openai_api', response)
    spec = {'camera_anchor': 'An empty dark frame', 'must_show': []}
    assert su.black_screen_storyboard_shot('BLACK SCREEN', spec) is False
    assert su.black_screen_storyboard_shot('BLACK SCREEN', spec) is True
    assert len(calls) == 2
