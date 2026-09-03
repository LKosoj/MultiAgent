import json
from types import SimpleNamespace

import pytest
from smolagents import ChatMessage, MessageRole


@pytest.fixture(autouse=True)
def _reset_response_log_handler():
    """Синглтон хендлера логов — модульный глобал; сбрасываем до/после каждого теста,
    иначе один тест может унаследовать хендлер (и его путь/лимиты) от другого."""
    import retry_openai_model

    retry_openai_model._response_log_handler = None
    yield
    retry_openai_model._response_log_handler = None


def _patch_create_model(monkeypatch, model_cls):
    from retry_openai_model import RetryOpenAIServerModel

    def fake_create_model(self, model_id, client_kwargs):
        return model_cls()

    monkeypatch.setattr(RetryOpenAIServerModel, "_create_model", fake_create_model, raising=True)


def _read_log_lines(tmp_path):
    log_path = tmp_path / "logs" / "llm_responses" / "responses.jsonl"
    if not log_path.exists():
        return []
    with open(log_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_debug_logging_disabled_by_default_and_env_overrides(monkeypatch):
    from retry_openai_model import RetryOpenAIServerModel

    class DummyModel:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("not used")

    _patch_create_model(monkeypatch, DummyModel)

    monkeypatch.delenv("LLM_RESPONSE_LOGGING_ENABLED", raising=False)
    m_default = RetryOpenAIServerModel(
        model_id="primary", max_retries=0, api_base="http://example.local", api_key="test"
    )
    assert m_default.debug_logging is False

    monkeypatch.setenv("LLM_RESPONSE_LOGGING_ENABLED", "1")
    m_env = RetryOpenAIServerModel(
        model_id="primary", max_retries=0, api_base="http://example.local", api_key="test"
    )
    assert m_env.debug_logging is True

    monkeypatch.delenv("LLM_RESPONSE_LOGGING_ENABLED", raising=False)
    m_explicit = RetryOpenAIServerModel(
        model_id="primary",
        max_retries=0,
        api_base="http://example.local",
        api_key="test",
        debug_logging=True,
    )
    assert m_explicit.debug_logging is True


def test_disabled_logging_writes_no_file(monkeypatch, tmp_path):
    import retry_openai_model

    monkeypatch.setattr(retry_openai_model, "__file__", str(tmp_path / "retry_openai_model.py"))
    monkeypatch.delenv("LLM_RESPONSE_LOGGING_ENABLED", raising=False)

    class DummyModel:
        def __call__(self, *args, **kwargs):
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content="ok",
                token_usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    _patch_create_model(monkeypatch, DummyModel)

    m = retry_openai_model.RetryOpenAIServerModel(
        model_id="primary", max_retries=0, api_base="http://example.local", api_key="test"
    )
    response = m([ChatMessage(role=MessageRole.USER, content="hi")])
    assert response is not None
    assert not (tmp_path / "logs" / "llm_responses" / "responses.jsonl").exists()


def test_success_log_contains_latency_usage_run_id_step_name(monkeypatch, tmp_path):
    import retry_openai_model
    from llm_call_context import llm_call_context

    monkeypatch.setattr(retry_openai_model, "__file__", str(tmp_path / "retry_openai_model.py"))

    class DummyModel:
        def __call__(self, *args, **kwargs):
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content="ok",
                token_usage=SimpleNamespace(input_tokens=11, output_tokens=22),
            )

    _patch_create_model(monkeypatch, DummyModel)

    m = retry_openai_model.RetryOpenAIServerModel(
        model_id="primary",
        max_retries=0,
        api_base="http://example.local",
        api_key="test",
        debug_logging=True,
    )
    with llm_call_context(run_id="run-123", step_name="unit-test-step"):
        response = m([ChatMessage(role=MessageRole.USER, content="hi")])
    assert response is not None

    entries = _read_log_lines(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["status"] == "success"
    assert entry["run_id"] == "run-123"
    assert entry["step_name"] == "unit-test-step"
    assert entry["model_id"] == "primary"
    assert entry["attempt"] == 1
    assert entry["error"] is None
    assert isinstance(entry["latency_ms"], (int, float)) and entry["latency_ms"] >= 0
    assert entry["usage"] == {"input_tokens": 11, "output_tokens": 22}


def test_usage_is_none_when_provider_omits_it(monkeypatch, tmp_path):
    import retry_openai_model

    monkeypatch.setattr(retry_openai_model, "__file__", str(tmp_path / "retry_openai_model.py"))

    class DummyModel:
        def __call__(self, *args, **kwargs):
            return ChatMessage(role=MessageRole.ASSISTANT, content="ok", token_usage=None)

    _patch_create_model(monkeypatch, DummyModel)

    m = retry_openai_model.RetryOpenAIServerModel(
        model_id="primary",
        max_retries=0,
        api_base="http://example.local",
        api_key="test",
        debug_logging=True,
    )
    response = m([ChatMessage(role=MessageRole.USER, content="hi")])
    assert response is not None

    entries = _read_log_lines(tmp_path)
    assert len(entries) == 1
    # provider вернул None usage -> лог должен содержать null, а не нули
    # (нули подставляются позже, _inject_usage_defaults, уже после логирования)
    assert entries[0]["usage"] is None


def test_failure_logged_before_successful_retry(monkeypatch, tmp_path):
    import retry_openai_model

    monkeypatch.setattr(retry_openai_model, "__file__", str(tmp_path / "retry_openai_model.py"))

    calls = []

    class DummyModel:
        def __call__(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise Exception("simulated timeout on first attempt")
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content="ok",
                token_usage=SimpleNamespace(input_tokens=5, output_tokens=6),
            )

    _patch_create_model(monkeypatch, DummyModel)

    m = retry_openai_model.RetryOpenAIServerModel(
        model_id="primary",
        max_retries=1,
        retry_delay_base=0.0,
        api_base="http://example.local",
        api_key="test",
        debug_logging=True,
    )
    response = m([ChatMessage(role=MessageRole.USER, content="hi")])
    assert response is not None

    entries = _read_log_lines(tmp_path)
    assert len(entries) == 2
    assert entries[0]["status"] == "failure"
    assert entries[0]["attempt"] == 1
    assert entries[0]["model_id"] == "primary"
    assert "timeout" in entries[0]["error"]
    assert entries[1]["status"] == "success"
    assert entries[1]["attempt"] == 2


def test_failure_logged_on_fallback_switch(monkeypatch, tmp_path):
    import retry_openai_model

    monkeypatch.setattr(retry_openai_model, "__file__", str(tmp_path / "retry_openai_model.py"))

    calls = []

    class DummyModel:
        def __call__(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise Exception("HTTP 429 rate limit exceeded")
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content="ok",
                token_usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    _patch_create_model(monkeypatch, DummyModel)

    m = retry_openai_model.RetryOpenAIServerModel(
        model_id="primary",
        fallback_models="fb1",
        max_retries=0,
        retry_delay_base=0.0,
        api_base="http://example.local",
        api_key="test",
        debug_logging=True,
    )
    response = m([ChatMessage(role=MessageRole.USER, content="hi")])
    assert response is not None

    entries = _read_log_lines(tmp_path)
    assert len(entries) == 2
    assert entries[0]["status"] == "failure"
    assert entries[0]["model_id"] == "primary"
    assert "429" in entries[0]["error"]
    assert entries[1]["status"] == "success"
    assert entries[1]["model_id"] == "fb1"


def test_rotation_limits_log_file_growth(monkeypatch, tmp_path):
    import retry_openai_model

    monkeypatch.setattr(retry_openai_model, "__file__", str(tmp_path / "retry_openai_model.py"))
    monkeypatch.setenv("LLM_RESPONSE_LOG_MAX_BYTES", "2000")
    monkeypatch.setenv("LLM_RESPONSE_LOG_BACKUPS", "2")

    class DummyModel:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("not used")

    _patch_create_model(monkeypatch, DummyModel)

    m = retry_openai_model.RetryOpenAIServerModel(
        model_id="primary",
        max_retries=0,
        api_base="http://example.local",
        api_key="test",
        debug_logging=True,
    )

    large_response = ChatMessage(role=MessageRole.ASSISTANT, content="x" * 500)
    for _ in range(30):
        m._write_response_log(
            status="success",
            model_id="primary",
            attempt=1,
            latency_ms=1.0,
            response=large_response,
            usage=None,
            error=None,
        )

    log_dir = tmp_path / "logs" / "llm_responses"
    main_log = log_dir / "responses.jsonl"
    assert main_log.exists()
    # Основной файл не растёт бесконечно: ротация держит его в разумных пределах
    # относительно настроенного maxBytes.
    assert main_log.stat().st_size <= 2000 * 2

    backups = sorted(log_dir.glob("responses.jsonl.*"))
    assert 1 <= len(backups) <= 2
