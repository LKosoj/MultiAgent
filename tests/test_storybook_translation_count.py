import json

import pytest

import utils


@pytest.mark.parametrize('texts', [['first paragraph\nsecond paragraph'], ['first', 'second']])
def test_translation_preserves_input_count_and_multiline_boundaries(monkeypatch, texts):
    requests = []
    def translate(**kwargs):
        requests.append(kwargs)
        return json.dumps({'texts': texts})
    monkeypatch.setattr(utils, 'call_openai_api', translate)
    assert utils._translate_texts_batch(texts, 'en') == texts
    request = requests[0]
    assert json.loads(request['prompt'])['texts'] == texts
    assert f'РОВНО {len(texts)} строк' in request['system_prompt']
    assert 'Не разбивай её на элементы списка' in request['system_prompt']
    expected_example = {'texts': [f'переведенный_текст{i + 1}' for i in range(len(texts))]}
    assert json.dumps(expected_example, ensure_ascii=False) in request['system_prompt']


def test_split_translation_is_rejected_instead_of_silently_joined(monkeypatch):
    monkeypatch.setattr(utils, 'call_openai_api', lambda **kwargs: '{"texts": ["part one", "part two"]}')
    with pytest.raises(RuntimeError, match='TRANSLATION MISMATCH'):
        utils._translate_texts_batch(['one input\nwith two paragraphs'], 'en')
