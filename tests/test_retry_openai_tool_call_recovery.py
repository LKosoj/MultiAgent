"""Тесты восстановления tool_calls из текстового ответа шлюза."""
from smolagents.models import ChatMessage

from retry_openai_model import (
    RetryOpenAIServerModel,
    recover_tool_calls_from_text,
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
