import pytest


def test_switch_to_fallback_wraps_to_primary(monkeypatch):
    """
    Проверяем ключевое требование:
    при достижении максимального индекса fallback-моделей переключаемся на основную (index=0) и продолжаем цикл.
    """
    from retry_openai_model import RetryOpenAIServerModel

    class DummyModel:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("not used in this test")

    def fake_create_model(self, model_id, client_kwargs):
        return DummyModel()

    monkeypatch.setattr(RetryOpenAIServerModel, "_create_model", fake_create_model, raising=True)

    m = RetryOpenAIServerModel(
        model_id="primary",
        fallback_models="fb1,fb2",
        max_retries=0,
        api_base="http://example.local",
        api_key="test",
    )

    # Ставим текущую модель на последнюю в списке
    assert m.model_ids == ["primary", "fb1", "fb2"]
    m.current_model_index = 2

    ok = m._switch_to_fallback()
    assert ok is True
    assert m.current_model_index == 0


def test_switch_to_fallback_returns_false_when_no_fallbacks(monkeypatch):
    from retry_openai_model import RetryOpenAIServerModel

    class DummyModel:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("not used in this test")

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

    assert m.model_ids == ["primary"]
    assert m._switch_to_fallback() is False



