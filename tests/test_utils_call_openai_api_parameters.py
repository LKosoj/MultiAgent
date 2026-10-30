from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from retry_openai_model import RetryOpenAIServerModel
from utils import call_openai_api


def _model(captured):
    model = RetryOpenAIServerModel('offline-model', api_base='http://localhost:0/v1', api_key='dummy',
                                   max_tokens=65536, temperature=0.6, max_retries=0)
    def offline_retryer(func, **kwargs):
        captured.append((kwargs['max_tokens'], kwargs['temperature']))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            role='assistant', content='{"ok":true}', tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    model.model.retryer = offline_retryer
    return model


def test_per_call_limits_override_defaults_without_mutating_model():
    captured = []
    model = _model(captured)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda pair: call_openai_api('test', model=model, max_tokens=pair[0],
                       temperature=pair[1], max_retries=0), [(4000, 0.1), (256, 0.2)]))
    assert len(results) == 2
    assert sorted(captured) == [(256, 0.2), (4000, 0.1)]
    assert model.kwargs['max_tokens'] == model.model.kwargs['max_tokens'] == 65536
    assert model.kwargs['temperature'] == model.model.kwargs['temperature'] == 0.6


def test_retry_uses_fresh_copy_and_preserves_defaults(monkeypatch):
    import utils
    calls = []
    class Model:
        def __init__(self):
            self.kwargs = {'max_tokens': 65536, 'temperature': 0.6}
        def __call__(self, messages, **kwargs):
            calls.append((self, dict(self.kwargs)))
            content = 'not json' if len(calls) == 1 else '{"ok":true}'
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                                           finish_reason='length' if len(calls) == 1 else 'stop')])
    model = Model()
    monkeypatch.setattr(utils.time, 'sleep', lambda _: None)
    monkeypatch.setattr(utils, '_repair_truncated_json', lambda _: 'invalid')
    result = call_openai_api('test', model=model, max_tokens=4000, temperature=0.1,
                             response_format={'type': 'json_object'}, max_retries=1)
    assert result == '{"ok":true}' and len(calls) == 2
    assert calls[0][0] is not calls[1][0] and all(call[0] is not model for call in calls)
    assert all(call[1] == {'max_tokens': 4000, 'temperature': 0.1} for call in calls)
    assert model.kwargs == {'max_tokens': 65536, 'temperature': 0.6}


def test_multimodal_content_reaches_sdk_without_flattening():
    captured = []
    model = _model([])
    def offline_retryer(func, **kwargs):
        captured.append(kwargs['messages'])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            role='assistant', content='{"ok":true}', tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    model.model.retryer = offline_retryer
    content = [{'type': 'text', 'text': 'VIDEO.0'},
               {'type': 'image_url', 'image_url': {'url': 'https://example.com/frame0.jpg'}},
               {'type': 'text', 'text': 'VIDEO.1'},
               {'type': 'image_url', 'image_url': {'url': 'https://example.com/frame1.jpg'}}]
    assert call_openai_api(content, model=model, max_retries=0) == '{"ok":true}'
    assert captured[0][-1]['content'] == content
