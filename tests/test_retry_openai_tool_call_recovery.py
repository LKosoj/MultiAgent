"""Тесты восстановления tool_calls из текстового ответа шлюза."""
from smolagents.models import ChatMessage

from retry_openai_model import (
    RetryOpenAIServerModel,
    recover_tool_calls_from_text,
    should_wrap_plain_text_as_final_answer,
    wrap_plain_text_as_final_answer,
)


SAMPLE_GATEWAY_OUTPUT = """Now let me search for more articles from Medium, TechCrunch, arXiv, etc. that fit the date range.
Calling tools:
[{'id': 'call_019f102ed8b97d83e2b67dfc', 'type': 'function', 'function': {'name': 'web_search', 'arguments': {'query': 'Towards Data Science AI data engineering article 2026-06-22 2026-06-28'}}}, {'id': 'call_019f102ed8b97d83e2b67e0a', 'type': 'function', 'function': {'name': 'web_search', 'arguments': {'query': 'TechCrunch AI data pipeline engineering June 2026'}}}]"""


def test_recovers_multiple_tool_calls_from_calling_tools_block():
    cleaned, tool_calls = recover_tool_calls_from_text(SAMPLE_GATEWAY_OUTPUT)

    assert cleaned.startswith("Now let me search for more articles")
    assert "Calling tools" not in (cleaned or "")
    assert len(tool_calls) == 2
    assert tool_calls[0].function.name == "web_search"
    assert "Towards Data Science" in tool_calls[0].function.arguments["query"]
    assert tool_calls[1].function.arguments["query"].startswith("TechCrunch")


def test_normalize_response_content_applies_recovery():
    model = RetryOpenAIServerModel.__new__(RetryOpenAIServerModel)
    response = ChatMessage(role="assistant", content=SAMPLE_GATEWAY_OUTPUT)

    normalized = model._normalize_response_content(response)

    assert normalized.tool_calls is not None
    assert len(normalized.tool_calls) == 2
    assert normalized.content.startswith("Now let me search")


def test_leaves_plain_text_without_calling_tools_unchanged():
    text = "Hello! How can I help you today?"
    cleaned, tool_calls = recover_tool_calls_from_text(text)

    assert cleaned == text
    assert tool_calls == []


def test_should_wrap_long_plain_text_without_json():
    text = "A" * 500
    assert should_wrap_plain_text_as_final_answer(text) is True


def test_should_not_wrap_short_plain_text():
    assert should_wrap_plain_text_as_final_answer("ok") is False


def test_apply_tool_call_recovery_wraps_plain_text_as_final_answer():
    model = RetryOpenAIServerModel.__new__(RetryOpenAIServerModel)
    long_answer = "### 1. Task outcome\n" + ("Найдены статьи. " * 40)
    response = ChatMessage(role="assistant", content=long_answer)

    recovered = model._apply_tool_call_recovery(
        response,
        {"tools_to_call_from": ["web_search"]},
    )

    assert recovered.tool_calls is not None
    assert len(recovered.tool_calls) == 1
    assert recovered.tool_calls[0].function.name == "final_answer"
    assert "Найдены статьи" in recovered.tool_calls[0].function.arguments["answer"]


def test_wrap_plain_text_as_final_answer_shape():
    tc = wrap_plain_text_as_final_answer("итоговый ответ")
    assert tc.function.name == "final_answer"
    assert tc.function.arguments == {"answer": "итоговый ответ"}
