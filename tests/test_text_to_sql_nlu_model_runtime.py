from __future__ import annotations

import httpx


def test_structured_query_understanding_models_are_deterministic(monkeypatch) -> None:
    import agent_command
    import custom_tools.text_to_sql.nlu as nlu_module

    calls: list[tuple[str, float]] = []

    def fake_create_text_to_sql_model(
        name: str,
        *,
        max_tokens: int,
        temperature: float,
    ) -> object:
        del max_tokens
        calls.append((name, temperature))
        return object()

    monkeypatch.setattr(
        agent_command,
        "create_text_to_sql_model",
        fake_create_text_to_sql_model,
    )
    nlu_module._nlu_model("nlu_query_understanding", max_tokens=16_000)
    nlu_module._nlu_model("nlu_completeness", max_tokens=16_000)
    nlu_module._nlu_model("nlu_intent", max_tokens=800)

    assert calls == [
        ("model_hard", 0.0),
        ("model_hard", 0.0),
        ("model_code", 0.7),
    ]


def test_nlu_model_uses_text_to_sql_timeout_without_client_retries(
    monkeypatch,
) -> None:
    import agent_command
    import custom_tools.text_to_sql.nlu as nlu_module

    class DummyModel:
        def __init__(self, client_kwargs):
            self.client = type("Client", (), {"_client": client_kwargs["http_client"]})()

    monkeypatch.setenv("OPENAI_API_KEY_DB", "test")
    monkeypatch.setattr(
        agent_command.RetryOpenAIServerModel,
        "_create_model",
        lambda self, _model_id, client_kwargs: DummyModel(client_kwargs),
    )

    model = nlu_module._nlu_model(
        "nlu_query_understanding",
        max_tokens=16_000,
    )

    assert model.max_retries == 0
    assert model.timeout_seconds == 1200.0
    assert model.model.client._client.timeout == httpx.Timeout(1200.0)
