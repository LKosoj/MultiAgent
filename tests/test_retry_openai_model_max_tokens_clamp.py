import pytest


def test_clamps_max_tokens_when_context_remaining_is_smaller(monkeypatch):
    from retry_openai_model import RetryOpenAIServerModel

    # Подменяем создание модели, чтобы не дергать реальный HTTP
    class DummyModel:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("not used directly")

    def fake_create_model(self, model_id, client_kwargs):
        return DummyModel()

    monkeypatch.setattr(RetryOpenAIServerModel, "_create_model", fake_create_model, raising=True)

    m = RetryOpenAIServerModel(
        model_id="primary",
        fallback_models=None,
        max_retries=0,
        api_base="http://example.local",
        api_key="test",
    )

    # Сообщение в формате, который вы видите в проде
    err = Exception(
        "Ответ сервера: {\"error\":{\"message\":\"'max_tokens' or 'max_completion_tokens' is too large: 32768. "
        "This model's maximum context length is 262144 tokens and your request has 233483 input tokens "
        "(32768 > 262144 - 233483). None\",\"type\":\"BadRequestError\",\"param\":null,\"code\":400}}"
    )

    call_kwargs = {"max_tokens": 32768}
    adjusted = m._maybe_clamp_max_tokens_from_context_error(err, call_kwargs)
    assert adjusted is True
    # allowed = 262144 - 233483 - 256 = 283? (проверяем, что кламп произошёл и стал меньше исходного)
    assert call_kwargs["max_tokens"] < 32768
    assert call_kwargs["max_tokens"] >= 1


def test_does_not_clamp_when_no_room_left(monkeypatch):
    from retry_openai_model import RetryOpenAIServerModel

    class DummyModel:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("not used directly")

    def fake_create_model(self, model_id, client_kwargs):
        return DummyModel()

    monkeypatch.setattr(RetryOpenAIServerModel, "_create_model", fake_create_model, raising=True)

    m = RetryOpenAIServerModel(
        model_id="primary",
        fallback_models=None,
        max_retries=0,
        api_base="http://example.local",
        api_key="test",
    )

    # input_tokens >= max_context -> кламп не спасает
    err = Exception(
        "'max_tokens' or 'max_completion_tokens' is too large: 10. "
        "This model's maximum context length is 100 tokens and your request has 120 input tokens (10 > 100 - 120)."
    )
    call_kwargs = {"max_tokens": 10}
    assert m._maybe_clamp_max_tokens_from_context_error(err, call_kwargs) is False
    assert call_kwargs["max_tokens"] == 10


